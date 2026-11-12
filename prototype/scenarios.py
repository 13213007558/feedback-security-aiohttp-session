"""Runnable counter-examples and strategy comparisons.

Run with the project virtualenv::

    .venv/bin/python prototype/scenarios.py

Everything is deterministic: a single :class:`Clock` is advanced manually, so
no real ``sleep`` and no network are involved.
"""

from dataclasses import dataclass

from cryptography import fernet
from cryptography.fernet import InvalidToken

from core import Clock, Client, EncryptedCookieStorage, Session
from policies import (
    DenyListCookieStorage,
    GenerationCookieStorage,
    MemoryGenerationStore,
    MemoryRevocationTable,
    SqliteGenerationStore,
    SqliteRevocationTable,
    login,
    rotate_global_key,
)

KEY = fernet.Fernet.generate_key()


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def authenticated(result: dict) -> bool:
    return bool(result["authenticated"])


def policy_request(client, storage, handler=None):
    """load -> authorize -> optional handler -> conditional save."""

    def apply(session: Session) -> None:
        if handler is not None:
            handler(session)

    session, reason = storage.authorize(client._cookie)
    apply(session)
    if session.changed:
        client._cookie = storage.save_session(session)
    return {
        "client": client.name,
        "authenticated": not session.empty,
        "reason": reason,
        "session": session.as_dict(),
    }


def scenario_stateless_baseline(clock: Clock) -> list[Check]:
    """Pure EncryptedCookieStorage: nothing server-side can kill a token."""
    storage = EncryptedCookieStorage(KEY, max_age=3600, clock=clock)
    alice = Client("alice-laptop", storage)
    bob = Client("alice-phone", storage)

    def do_login(session: Session) -> None:
        login(session, "alice", token_generation=1)

    alice.request(do_login)
    bob.request(do_login)

    # "Logout all devices" on the server has nothing to write to. The two
    # cookies remain valid on their own until the Fernet TTL / max_age.
    before_a = authenticated(alice.request(lambda s: None))
    before_b = authenticated(bob.request(lambda s: None))
    clock.advance(60)
    after_a = authenticated(alice.request(lambda s: None))
    after_b = authenticated(bob.request(lambda s: None))

    return [
        Check(
            "stateless: both devices stay valid after a 'logout-all' event",
            before_a and before_b and after_a and after_b,
            "no server-side record exists to invalidate the cookies",
        )
    ]


def scenario_short_ttl(clock: Clock) -> list[Check]:
    """Short max_age bounds the breach window but is not immediate."""
    ttl = 60
    storage = EncryptedCookieStorage(KEY, max_age=ttl, clock=clock)
    alice = Client("alice-laptop", storage)
    bob = Client("alice-phone", storage)
    alice.request(lambda s: login(s, "alice", 1))
    bob.request(lambda s: login(s, "alice", 1))

    # Issue the "logout all" at t=0 of this window.
    clock.advance(30)
    mid_a = authenticated(alice.request(lambda s: None))
    mid_b = authenticated(bob.request(lambda s: None))
    # Read-only requests do NOT slide the window: created stays at login time.
    clock.advance(31)
    late_a = authenticated(alice.request(lambda s: None))
    late_b = authenticated(bob.request(lambda s: None))

    return [
        Check(
            "short-ttl: token still honored 30s after logout-all",
            mid_a and mid_b,
            f"revocation only takes effect after the {ttl}s window",
        ),
        Check(
            "short-ttl: both devices fail once max_age is exceeded",
            not late_a and not late_b,
            "Fernet TTL raises InvalidToken -> fresh empty session",
        ),
    ]


def scenario_generation(clock: Clock) -> list[Check]:
    """Per-account generation: immediate, account-scoped logout-all."""
    gens = MemoryGenerationStore()
    storage = GenerationCookieStorage(KEY, gens, max_age=3600, clock=clock)
    alice = Client("alice-laptop", storage)
    bob = Client("alice-phone", storage)
    carol = Client("carol-tablet", storage)

    policy_request(alice, storage, lambda s: login(s, "alice", gens.get("alice")))
    policy_request(bob, storage, lambda s: login(s, "alice", gens.get("alice")))
    policy_request(carol, storage, lambda s: login(s, "carol", gens.get("carol")))

    valid_before = all(
        authenticated(policy_request(c, storage)) for c in (alice, bob, carol)
    )

    # One integer write revokes every existing alice token at once.
    gens.bump("alice")
    res_a = policy_request(alice, storage)
    res_b = policy_request(bob, storage)
    res_c = policy_request(carol, storage)

    # A fresh login after the bump picks up the new generation and works.
    policy_request(alice, storage, lambda s: login(s, "alice", gens.get("alice")))
    relogin = authenticated(policy_request(alice, storage))

    return [
        Check("generation: all devices valid before bump", valid_before, ""),
        Check(
            "generation: laptop rejected immediately after bump",
            not authenticated(res_a) and res_a["reason"] == "stale generation",
            res_a["reason"] or "",
        ),
        Check(
            "generation: phone rejected immediately after bump",
            not authenticated(res_b),
            res_b["reason"] or "",
        ),
        Check(
            "generation: other account (carol) is unaffected",
            authenticated(res_c),
            "the bump is scoped to alice only",
        ),
        Check(
            "generation: re-login with the new generation works",
            relogin,
            "",
        ),
    ]


def scenario_denylist(clock: Clock) -> list[Check]:
    """Central revocation table: precise per-session control, more state."""
    denylist = MemoryRevocationTable()
    storage = DenyListCookieStorage(KEY, denylist, max_age=3600, clock=clock)
    alice = Client("alice-laptop", storage)
    bob = Client("alice-phone", storage)
    policy_request(alice, storage, lambda s: login(s, "alice", 1))
    policy_request(bob, storage, lambda s: login(s, "alice", 1))

    laptop_sid = alice._cookie
    # Recover the laptop session id from its encrypted cookie.
    payload = storage._decrypt_raw(laptop_sid)
    sid_to_revoke = payload["session"]["sid"]
    phone_sid_before = storage._decrypt_raw(bob._cookie)["session"]["sid"]

    # Revoke a single device rather than the whole account.
    denylist.revoke(sid_to_revoke)
    res_a = policy_request(alice, storage)
    res_b = policy_request(bob, storage)

    return [
        Check(
            "denylist: only the revoked session id is blocked",
            not authenticated(res_a) and authenticated(res_b),
            f"revoked={sid_to_revoke[:8]} phone={phone_sid_before[:8]}",
        ),
        Check(
            "denylist: reason is an explicit deny-list hit",
            res_a["reason"] == "session revoked",
            res_a["reason"] or "",
        ),
    ]


def scenario_global_rotation_is_not_per_account(clock: Clock) -> list[Check]:
    """Rotating the global Fernet key logs out every account, not one."""
    old_key = fernet.Fernet.generate_key()
    storage_old = EncryptedCookieStorage(old_key, max_age=3600, clock=clock)
    alice = Client("alice-laptop", storage_old)
    carol = Client("carol-tablet", storage_old)
    alice.request(lambda s: login(s, "alice", 1))
    carol.request(lambda s: login(s, "carol", 1))

    new_key, _ = rotate_global_key()
    storage_new = EncryptedCookieStorage(new_key, max_age=3600, clock=clock)
    alice_new = Client("alice-laptop", storage_new)
    carol_new = Client("carol-tablet", storage_new)
    alice_new._cookie = alice._cookie
    carol_new._cookie = carol._cookie

    alice_blocked = False
    carol_blocked = False
    try:
        storage_new._fernet.decrypt_at_time(
            alice._cookie.encode(), ttl=3600, current_time=clock.now()
        )
    except InvalidToken:
        alice_blocked = True
    try:
        storage_new._fernet.decrypt_at_time(
            carol._cookie.encode(), ttl=3600, current_time=clock.now()
        )
    except InvalidToken:
        carol_blocked = True

    return [
        Check(
            "key-rotation: target alice is blocked",
            alice_blocked,
            "her cookie was encrypted under the retired key",
        ),
        Check(
            "key-rotation: unrelated carol is ALSO blocked",
            carol_blocked,
            "global rotation cannot target a single account",
        ),
    ]


def scenario_sqlite_durability(clock: Clock) -> list[Check]:
    """The generation lookup can be durable with stdlib sqlite3 only."""
    gen_db = SqliteGenerationStore()
    deny_db = SqliteRevocationTable()
    gen_storage = GenerationCookieStorage(
        KEY, gen_db, max_age=3600, clock=clock
    )
    deny_storage = DenyListCookieStorage(
        KEY, deny_db, max_age=3600, clock=clock
    )

    alice = Client("alice-laptop", gen_storage)
    policy_request(alice, gen_storage, lambda s: login(s, "alice", gen_db.get("alice")))
    ok_before = authenticated(policy_request(alice, gen_storage))
    gen_db.bump("alice")
    ok_after_bump = not authenticated(policy_request(alice, gen_storage))

    bob = Client("bob-phone", deny_storage)
    policy_request(bob, deny_storage, lambda s: login(s, "bob", 1))
    sid = deny_storage._decrypt_raw(bob._cookie)["session"]["sid"]
    deny_db.revoke(sid)
    ok_after_deny = not authenticated(policy_request(bob, deny_storage))

    gen_db.close()
    deny_db.close()
    return [
        Check(
            "sqlite: generation bump persists and revokes",
            ok_before and ok_after_bump,
            "",
        ),
        Check("sqlite: deny-list entry persists and revokes", ok_after_deny, ""),
    ]


def main() -> None:
    sections = [
        (
            "1. Pure stateless encrypted cookie (counter-example)",
            scenario_stateless_baseline,
        ),
        ("2. Short max_age / TTL", scenario_short_ttl),
        ("3. Per-account generation lookup", scenario_generation),
        ("4. Central revocation table (deny-list)", scenario_denylist),
        (
            "5. Global key rotation is NOT per-account",
            scenario_global_rotation_is_not_per_account,
        ),
        (
            "6. Low-dependency durable state (stdlib sqlite3)",
            scenario_sqlite_durability,
        ),
    ]
    all_ok = True
    for title, fn in sections:
        clock = Clock(start=1_000_000)
        print(f"\n=== {title} ===")
        for check in fn(clock):
            all_ok = all_ok and check.ok
            tag = "PASS" if check.ok else "FAIL"
            extra = f" -- {check.detail}" if check.detail else ""
            print(f"  [{tag}] {check.name}{extra}")
    print("\n" + ("ALL CHECKS PASSED" if all_ok else "SOME CHECKS FAILED"))
    raise SystemExit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
