"""Contract tests for absolute and idle session lifetime limits.

The tests drive each storage directly with a replaceable clock and
in-memory backends.  This makes cookie TTLs and backend TTLs observable
without HTTP servers or external services.  Every test states the
reference timeline used by its assertions.
"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass
from http.cookies import Morsel
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from aiohttp import web
from cryptography.fernet import Fernet
from redis import asyncio as aioredis
from redis.asyncio.connection import ConnectionPool

from aiohttp_session import Session, SimpleCookieStorage
from aiohttp_session.cookie_storage import EncryptedCookieStorage
from aiohttp_session.redis_storage import RedisStorage

# Memcached is an optional dependency.  The contract tests only need the
# annotation type, so provide a stub when the real client is absent.
try:
    import aiomcache  # noqa: F401
except ImportError:  # pragma: no cover - environment without the extra
    _aiomcache_stub = types.ModuleType("aiomcache")
    _aiomcache_stub.Client = object  # type: ignore[attr-defined]
    sys.modules["aiomcache"] = _aiomcache_stub

from aiohttp_session.memcached_storage import MemcachedStorage  # noqa: E402


IDLE = 60
ABSOLUTE = 300


def _set_idle(storage: Any, value: int) -> None:
    """Set a different idle limit before any save.

    The Session reads the storage's configured limit at load time, so
    adjusting both the attribute and cookie params models a process
    started with that configuration.
    """
    storage._max_age = value
    storage._cookie_params["max_age"] = value


class FrozenClock:
    def __init__(self, now: int = 1000) -> None:
        self.now = now

    def __call__(self) -> float:
        return float(self.now)

    def set(self, value: int) -> None:
        self.now = value


class FakeRedis(aioredis.Redis):
    """Redis stand-in recording key value and requested TTL."""

    def __init__(self) -> None:
        super().__init__(
            connection_pool=ConnectionPool.from_url("redis://localhost:6379/0")
        )
        self.store: dict[str, tuple[bytes | str, int | None]] = {}

    async def get(self, key: str) -> bytes | str | None:
        item = self.store.get(key)
        return None if item is None else item[0]

    async def set(
        self, key: str, value: bytes | str, ex: int | None = None, **kwargs: Any
    ) -> bool:
        if isinstance(value, str):
            value = value.encode("utf-8")
        self.store[key] = (value, ex)
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                removed += 1
        return removed


class FakeMemcached:
    """Memcached stand-in recording value and requested exptime."""

    def __init__(self) -> None:
        self.store: dict[bytes, tuple[bytes, int]] = {}

    async def get(self, key: bytes) -> bytes | None:
        item = self.store.get(key)
        return None if item is None else item[0]

    async def set(self, key: bytes, value: bytes, exptime: int = 0) -> bool:
        self.store[key] = (value, exptime)
        return True

    async def delete(self, key: bytes) -> bool:
        return self.store.pop(key, None) is not None


@dataclass
class Observation:
    session: Session
    cookie_value: str | None
    cookie_max_age: int | None
    payload: dict[str, Any] | None
    backend_ttl: int | None
    backend_present: bool
    saved: bool


class Harness:
    """Load, optionally mutate, then save exactly when middleware would."""

    def __init__(self, storage: Any, clock: FrozenClock) -> None:
        self.storage = storage
        self.clock = clock

    @property
    def backend_storage(self) -> bool:
        return isinstance(self.storage, (RedisStorage,)) or hasattr(
            self.storage, "conn"
        )

    def backend_key(self, key: str) -> Any:
        return self.storage._storage_key(key)

    def _backend(self, key: str) -> tuple[bytes | str | None, int | None, bool]:
        if isinstance(self.storage, RedisStorage):
            item = self.storage._redis.store.get(self.backend_key(key))
        elif hasattr(self.storage, "conn"):
            item = self.storage.conn.store.get(self.backend_key(key))
        else:
            return None, None, False
        if item is None:
            return None, None, False
        return item[0], item[1], True

    def _payload(self, key: str) -> dict[str, Any] | None:
        raw, _, _ = self._backend(key)
        return None if raw is None else json.loads(raw)

    async def run(
        self, cookie_value: str | None, mutate: Callable[..., None] | None = None
    ) -> Observation:
        cookies = (
            {} if cookie_value is None else {self.storage.cookie_name: cookie_value}
        )
        request = SimpleNamespace(cookies=cookies)
        session = await self.storage.load_session(request)
        if mutate is not None:
            mutate(session, self.clock)

        response: web.Response | None = None
        if session._changed:
            response = web.Response()
            await self.storage.save_session(request, response, session)

        outgoing: Morsel[str] | None = None
        if response is not None and self.storage.cookie_name in response.cookies:
            outgoing = response.cookies[self.storage.cookie_name]

        key: str | None = cookie_value
        if key is None and self.backend_storage and outgoing is not None:
            key = outgoing.value or None

        backend_ttl: int | None = None
        backend_present = False
        payload: dict[str, Any] | None = None
        if key is not None and self.backend_storage:
            _, backend_ttl, backend_present = self._backend(key)
            payload = self._payload(key)

        if payload is None and not self.backend_storage:
            if outgoing is not None and outgoing.value:
                payload = _decode_payload(self.storage, outgoing.value)
            elif not response and cookie_value:
                # Read-only observation of the incoming cookie.
                payload = _decode_payload(self.storage, cookie_value)

        cookie_out = (
            None if outgoing is None or not outgoing.value else outgoing.value
        )
        max_age = None if outgoing is None else outgoing["max-age"]
        return Observation(
            session=session,
            cookie_value=cookie_out,
            cookie_max_age=None if max_age in (None, "") else int(max_age),
            payload=payload,
            backend_ttl=backend_ttl,
            backend_present=backend_present,
            saved=response is not None,
        )


def _decode_payload(storage: Any, cookie_value: str) -> dict[str, Any]:
    if isinstance(storage, SimpleCookieStorage):
        return json.loads(cookie_value)
    if isinstance(storage, EncryptedCookieStorage):
        plain = storage._fernet.decrypt(cookie_value.encode("utf-8"))
        return json.loads(plain)
    from nacl.encoding import Base64Encoder

    plain = storage._secretbox.decrypt(
        cookie_value.encode("utf-8"), encoder=Base64Encoder
    )
    return json.loads(plain)


def _factories() -> list[tuple[str, Callable[[FrozenClock], Any]]]:
    factories: list[tuple[str, Callable[[FrozenClock], Any]]] = [
        (
            "simple-cookie",
            lambda clock: SimpleCookieStorage(
                max_age=IDLE, absolute_max_age=ABSOLUTE, clock=clock
            ),
        ),
        (
            "encrypted-cookie",
            lambda clock: EncryptedCookieStorage(
                Fernet.generate_key().decode("utf-8"),
                max_age=IDLE,
                absolute_max_age=ABSOLUTE,
                clock=clock,
            ),
        ),
        (
            "redis",
            lambda clock: RedisStorage(
                FakeRedis(), max_age=IDLE, absolute_max_age=ABSOLUTE, clock=clock
            ),
        ),
    ]
    factories.append(
        (
            "memcached",
            lambda clock: MemcachedStorage(
                FakeMemcached(),
                max_age=IDLE,
                absolute_max_age=ABSOLUTE,
                clock=clock,
            ),
        )
    )
    try:
        import nacl.secret
        import nacl.utils
        from aiohttp_session.nacl_storage import NaClCookieStorage
    except ImportError:  # pragma: no cover - optional dependency
        pass
    else:
        factories.append(
            (
                "nacl-cookie",
                lambda clock: NaClCookieStorage(
                    nacl.utils.random(nacl.secret.SecretBox.KEY_SIZE),
                    max_age=IDLE,
                    absolute_max_age=ABSOLUTE,
                    clock=clock,
                ),
            )
        )
    return factories


def _reconstruct(
    storage: Any,
    clock: FrozenClock,
    max_age: int,
    absolute_max_age: int = ABSOLUTE,
) -> Any:
    if isinstance(storage, RedisStorage):
        return RedisStorage(
            storage._redis,
            max_age=max_age,
            absolute_max_age=absolute_max_age,
            clock=clock,
        )
    if hasattr(storage, "conn"):
        return MemcachedStorage(
            storage.conn,
            max_age=max_age,
            absolute_max_age=absolute_max_age,
            clock=clock,
        )
    if isinstance(storage, SimpleCookieStorage):
        return SimpleCookieStorage(
            max_age=max_age, absolute_max_age=absolute_max_age, clock=clock
        )
    if isinstance(storage, EncryptedCookieStorage):
        return EncryptedCookieStorage(
            storage._fernet,
            max_age=max_age,
            absolute_max_age=absolute_max_age,
            clock=clock,
        )
    from aiohttp_session.nacl_storage import NaClCookieStorage

    key = bytes(storage._secretbox._key)
    return NaClCookieStorage(
        key,
        max_age=max_age,
        absolute_max_age=absolute_max_age,
        clock=clock,
    )


def still_valid(session: Session) -> bool:
    return not session.new and not session.expired and not session.empty


@pytest.mark.parametrize("name,make", _factories(), ids=lambda value: value)
async def test_initial_lifetime_is_capped(
    name: str, make: Callable[[FrozenClock], Any]
) -> None:
    """Timeline: t=1000 create; idle=60s; absolute=300s.

    The first save may advertise at most 60s, and the persisted payload
    records creation t=1000 and absolute deadline t=1300.
    """
    clock = FrozenClock(1000)
    storage = make(clock)
    harness = Harness(storage, clock)
    assert name

    first = await harness.run(
        None, lambda session, _clock: session.__setitem__("role", "admin")
    )

    assert first.payload is not None
    assert first.payload["created"] == 1000
    assert first.payload["last_used"] == 1000
    assert first.payload["absolute_deadline"] == 1300
    assert "max_age" not in first.payload
    assert first.cookie_max_age == IDLE
    if harness.backend_storage:
        assert first.backend_ttl == IDLE


@pytest.mark.parametrize("name,make", _factories(), ids=lambda value: value)
async def test_activity_cannot_move_absolute_deadline(
    name: str, make: Callable[[FrozenClock], Any]
) -> None:
    """Timeline: create t=1000; touch t=1250; deadline remains t=1300.

    Idle is configured at 280s so the touch at t=1250 is inside the
    idle window; the idle renewal offers 280s, but only 50s remain
    before the absolute deadline, so the cookie and backend TTL must
    both be 50.
    """
    clock = FrozenClock(1000)
    storage = make(clock)
    _set_idle(storage, 280)
    harness = Harness(storage, clock)
    assert name

    first = await harness.run(None, lambda session, _c: session.__setitem__("a", 1))
    assert first.cookie_value is not None

    clock.set(1250)
    touched = await harness.run(
        first.cookie_value, lambda session, _c: session.__setitem__("b", 2)
    )

    assert touched.payload is not None
    assert touched.payload["created"] == 1000
    assert touched.payload["last_used"] == 1250
    assert touched.payload["absolute_deadline"] == 1300
    assert touched.cookie_max_age == 50
    if harness.backend_storage:
        assert touched.backend_ttl == 50


@pytest.mark.parametrize("name,make", _factories(), ids=lambda value: value)
async def test_read_without_modification_does_not_renew(
    name: str, make: Callable[[FrozenClock], Any]
) -> None:
    """Timeline: create t=1000; read t=1059; expiry still at t=1060.

    A read-only request performs no save, so no new cookie/TTL is issued
    and the idle deadline is unchanged.
    """
    clock = FrozenClock(1000)
    storage = make(clock)
    harness = Harness(storage, clock)
    assert name

    first = await harness.run(None, lambda session, _c: session.__setitem__("a", 1))
    assert first.cookie_value is not None

    clock.set(1059)
    read = await harness.run(first.cookie_value)
    assert not read.saved
    assert read.cookie_value is None
    assert still_valid(read.session)

    clock.set(1060)
    expired = await harness.run(first.cookie_value)
    assert expired.saved
    assert expired.cookie_value is None
    assert expired.cookie_max_age == 0
    assert expired.session.expired
    assert expired.session.empty


@pytest.mark.parametrize("name,make", _factories(), ids=lambda value: value)
async def test_absolute_deadline_expires_even_with_recent_activity(
    name: str, make: Callable[[FrozenClock], Any]
) -> None:
    """Timeline: create t=1000; touch t=1240; probe t=1300.

    Idle would last until t=1840, but the absolute deadline t=1300 wins.
    """
    clock = FrozenClock(1000)
    storage = make(clock)
    # Idle is long enough that only the absolute deadline (t=1300) can
    # explain the rejection at that boundary.
    _set_idle(storage, 600)
    harness = Harness(storage, clock)
    assert name

    first = await harness.run(None, lambda session, _c: session.__setitem__("a", 1))
    assert first.cookie_value is not None

    # Touch at t=1240, which is still inside the idle window.
    clock.set(1240)
    touched = await harness.run(
        first.cookie_value, lambda session, _c: session.__setitem__("b", 2)
    )
    assert touched.payload is not None
    assert touched.payload["last_used"] == 1240
    assert touched.payload["absolute_deadline"] == 1300
    # Idle offers 600s, absolute offers 60s: the smaller wins.
    assert touched.cookie_max_age == 60

    # Idle alone would permit access past t=1600; the absolute deadline
    # t=1300 is the binding limit.
    clock.set(1300)
    expired = await harness.run(touched.cookie_value)

    assert expired.session.new
    assert expired.session.empty
    assert expired.session.expired
    assert expired.session.absolute_expired
    assert expired.cookie_value is None
    assert expired.cookie_max_age == 0


@pytest.mark.parametrize("name,make", _factories(), ids=lambda value: value)
async def test_idle_expiry_write_renews_within_absolute_limits(
    name: str, make: Callable[[FrozenClock], Any]
) -> None:
    """Timeline: create t=1000 idle=60; no activity; write t=1100.

    The idle window has lapsed, so old data is gone.  A write starts a
    renewed session but retains creation t=1000 and the absolute
    deadline t=1300; its TTL is min(60, 200) = 60.  A later write at
    t=1290 after another idle lapse gets only min(60, 10) = 10.
    """
    clock = FrozenClock(1000)
    storage = make(clock)
    harness = Harness(storage, clock)
    assert name

    first = await harness.run(
        None, lambda session, _c: session.__setitem__("role", "admin")
    )
    assert first.cookie_value is not None

    clock.set(1100)
    renewed = await harness.run(
        first.cookie_value, lambda session, _c: session.__setitem__("role", "viewer")
    )
    assert renewed.payload is not None
    assert renewed.payload["created"] == 1000
    assert renewed.payload["last_used"] == 1100
    assert renewed.payload["absolute_deadline"] == 1300
    assert renewed.payload["session"] == {"role": "viewer"}
    assert renewed.cookie_max_age == IDLE
    if harness.backend_storage:
        assert renewed.backend_ttl == IDLE

    clock.set(1290)
    final = await harness.run(
        renewed.cookie_value, lambda session, _c: session.__setitem__("role", "viewer")
    )
    assert final.payload is not None
    assert final.payload["created"] == 1000
    assert final.payload["absolute_deadline"] == 1300
    assert final.cookie_max_age == 10
    if harness.backend_storage:
        assert final.backend_ttl == 10


@pytest.mark.parametrize("name,make", _factories(), ids=lambda value: value)
async def test_late_write_after_absolute_deadline_is_not_saved(
    name: str, make: Callable[[FrozenClock], Any]
) -> None:
    """Timeline: create t=1000; deadline t=1110; late write t=1111.

    Crossing the absolute deadline must clear the cookie and any backend
    record; a late handler write must not create a valid session.
    """
    clock = FrozenClock(1000)
    storage = _reconstruct(make(clock), clock, max_age=600, absolute_max_age=110)
    harness = Harness(storage, clock)
    assert name

    first = await harness.run(
        None, lambda session, _c: session.__setitem__("role", "viewer")
    )
    assert first.payload is not None
    assert first.payload["absolute_deadline"] == 1110
    key = first.cookie_value

    clock.set(1111)

    def escalate(session: Session, _clock: FrozenClock) -> None:
        session["role"] = "admin"
        session["pending_privilege_elevation"] = True

    late = await harness.run(key, escalate)

    assert late.saved
    assert late.cookie_value is None
    assert late.cookie_max_age == 0
    assert late.session.expired
    assert late.session.absolute_expired
    # The late handler write may live in memory, but it must never be
    # persisted or handed back with a valid cookie.
    assert late.payload is None
    if harness.backend_storage:
        assert not late.backend_present


@pytest.mark.parametrize("name,make", _factories(), ids=lambda value: value)
async def test_backwards_clock_jump_does_not_expire_or_extend(
    name: str, make: Callable[[FrozenClock], Any]
) -> None:
    """Timeline: create t=1000; clock jumps back to t=900; read.

    The session remains valid, but a future save can never advertise
    more than one full idle/absolute window and the absolute deadline
    stays at t=1300.
    """
    clock = FrozenClock(1000)
    storage = make(clock)
    harness = Harness(storage, clock)
    assert name

    first = await harness.run(None, lambda session, _c: session.__setitem__("a", 1))
    assert first.cookie_value is not None

    clock.set(900)
    read = await harness.run(first.cookie_value)

    assert still_valid(read.session)
    # last_used is preserved at t=1000; it is not clamped to t=900.
    # Clamping would silently consume 100s of the idle budget if the
    # clock later returned to t=1000.
    assert read.session.last_used == 1000
    assert read.session.absolute_deadline == 1300
    assert read.session.cookie_max_age is not None
    assert read.session.cookie_max_age <= IDLE


@pytest.mark.parametrize("name,make", _factories(), ids=lambda value: value)
async def test_forward_then_backward_jump_cannot_resurrect(
    name: str, make: Callable[[FrozenClock], Any]
) -> None:
    """Timeline: create t=1000; observe t=1120 (idle expired); back t=1010.

    Expiry observed at t=1120 clears the cookie and deletes backend
    records.  Rolling the clock back may make a replayed cookie
    self-consistent for stateless cookie storages, but it must still
    carry the original absolute deadline t=1300; backend storages must
    report a new session because their record is gone.
    """
    clock = FrozenClock(1000)
    storage = make(clock)
    harness = Harness(storage, clock)
    assert name

    first = await harness.run(None, lambda session, _c: session.__setitem__("a", 1))
    assert first.cookie_value is not None

    clock.set(1120)
    expired = await harness.run(first.cookie_value)
    assert expired.session.expired
    assert expired.cookie_value is None
    if harness.backend_storage:
        assert not expired.backend_present

    clock.set(1010)
    after = await harness.run(first.cookie_value)
    if harness.backend_storage:
        assert after.session.new
        assert after.session.empty
    else:
        assert after.payload is not None
        assert after.payload["absolute_deadline"] == 1300
        assert after.session.absolute_deadline == 1300
        assert after.session.cookie_max_age is not None
        assert after.session.cookie_max_age <= ABSOLUTE


@pytest.mark.parametrize("name,make", _factories(), ids=lambda value: value)
async def test_absolute_deadline_cannot_be_extended_by_reconfiguration(
    name: str, make: Callable[[FrozenClock], Any]
) -> None:
    """Timeline: create t=1000 with absolute=110s (deadline t=1110).

    A later process configured with absolute=600s must still expire the
    stored session at t=1110; the persisted deadline, not the new
    configuration, is authoritative.  Idle is long in both processes so
    only the absolute limit can explain the result.
    """
    clock = FrozenClock(1000)
    storage = make(clock)
    # Build the first process with absolute=110 directly so the saved
    # payload records deadline t=1110.
    first_storage = _reconstruct(
        storage, clock, max_age=600, absolute_max_age=110
    )
    first = Harness(first_storage, clock)
    assert name

    created = await first.run(
        None, lambda session, _c: session.__setitem__("role", "admin")
    )
    assert created.payload is not None
    assert created.payload["absolute_deadline"] == 1110

    wider = Harness(
        _reconstruct(storage, clock, max_age=600, absolute_max_age=600), clock
    )

    clock.set(1109)
    still_valid = await wider.run(created.cookie_value)
    assert not still_valid.session.expired
    assert still_valid.session.absolute_deadline == 1110

    clock.set(1110)
    expired = await wider.run(created.cookie_value)
    assert expired.session.expired
    assert expired.session.absolute_expired
    assert expired.session.empty
    assert expired.cookie_value is None


@pytest.mark.parametrize("name,make", _factories(), ids=lambda value: value)
async def test_absolute_deadline_survives_idle_renewal(
    name: str, make: Callable[[FrozenClock], Any]
) -> None:
    """Timeline: create t=1000; renew at t=1050..1250.

    Every renewal's TTL is the smaller of 60s idle and the remaining
    absolute time, shrinking from 50s at t=1250 and never outliving
    t=1300.
    """
    clock = FrozenClock(1000)
    storage = make(clock)
    harness = Harness(storage, clock)
    assert name

    cookie = (
        await harness.run(None, lambda session, _c: session.__setitem__("n", 0))
    ).cookie_value
    for n in range(1, 6):
        clock.set(1000 + n * 50)
        obs = await harness.run(
            cookie, lambda session, _c, n=n: session.__setitem__("n", n)
        )
        cookie = obs.cookie_value
        expected = min(IDLE, 1300 - clock.now)
        assert obs.payload is not None
        assert obs.payload["absolute_deadline"] == 1300
        assert obs.cookie_max_age == expected
        if harness.backend_storage:
            assert obs.backend_ttl == expected


async def test_redis_and_memcached_ttl_match_cookie() -> None:
    """Backend TTL equals the effective cookie TTL, never the raw idle."""

    clock = FrozenClock(1000)
    redis = RedisStorage(
        FakeRedis(), max_age=400, absolute_max_age=ABSOLUTE, clock=clock
    )
    redis_harness = Harness(redis, clock)
    r = await redis_harness.run(None, lambda s, _c: s.__setitem__("a", 1))
    assert (r.cookie_max_age, r.backend_ttl) == (ABSOLUTE, ABSOLUTE)

    memcached = MemcachedStorage(
        FakeMemcached(), max_age=400, absolute_max_age=ABSOLUTE, clock=clock
    )
    memcached_harness = Harness(memcached, clock)
    m = await memcached_harness.run(None, lambda s, _c: s.__setitem__("a", 1))
    assert (m.cookie_max_age, m.backend_ttl) == (ABSOLUTE, ABSOLUTE)

    clock.set(1290)
    r2 = await redis_harness.run(r.cookie_value, lambda s, _c: s.__setitem__("b", 2))
    m2 = await memcached_harness.run(
        m.cookie_value, lambda s, _c: s.__setitem__("b", 2)
    )
    assert (r2.cookie_max_age, r2.backend_ttl) == (10, 10)
    assert (m2.cookie_max_age, m2.backend_ttl) == (10, 10)


async def test_memcached_long_ttl_is_absolute_unix_timestamp() -> None:
    """Memcached treats exptime > 30 days as an absolute timestamp.

    idle=50 days, absolute=45 days at t=1000: the effective TTL is 45
    days, stored as Unix timestamp 1000 + 45 days, not 0 ("never").
    """

    fifty = 50 * 24 * 60 * 60
    forty_five = 45 * 24 * 60 * 60
    clock = FrozenClock(1000)
    storage = MemcachedStorage(
        FakeMemcached(), max_age=fifty, absolute_max_age=forty_five, clock=clock
    )
    harness = Harness(storage, clock)
    obs = await harness.run(None, lambda s, _c: s.__setitem__("a", 1))

    assert obs.cookie_max_age == forty_five
    assert obs.backend_ttl == 1000 + forty_five


@pytest.mark.parametrize("name,make", _factories(), ids=lambda value: value)
async def test_privilege_narrowing_survives_restart(
    name: str, make: Callable[[FrozenClock], Any]
) -> None:
    """Timeline: role=admin at t=1000; downgrade at t=1050; reload.

    The reduced privilege persists and the original absolute deadline
    t=1300 still governs the session.  A restarted process with the same
    (or wider) configuration cannot recover the admin role.
    """
    clock = FrozenClock(1000)
    storage = make(clock)
    harness = Harness(storage, clock)
    assert name

    admin = await harness.run(
        None,
        lambda s, _c: (
            s.__setitem__("role", "admin"),
            s.__setitem__("pending_privilege_elevation", True),
        ),
    )
    assert admin.cookie_value is not None

    clock.set(1050)

    def downgrade(session: Session, _clock: FrozenClock) -> None:
        session["role"] = "viewer"
        del session["pending_privilege_elevation"]
        session.changed()

    restricted = await harness.run(admin.cookie_value, downgrade)
    assert restricted.payload is not None
    assert restricted.payload["session"] == {"role": "viewer"}
    assert restricted.payload["created"] == 1000
    assert restricted.payload["absolute_deadline"] == 1300

    restarted = _reconstruct(storage, clock, max_age=IDLE)
    restarted_harness = Harness(restarted, clock)
    reread = await restarted_harness.run(restricted.cookie_value)
    assert reread.session.get("role") == "viewer"
    assert "pending_privilege_elevation" not in reread.session
    assert reread.session.absolute_deadline == 1300

    # Once the absolute deadline is crossed, even the narrowed session
    # is gone; the reduced role is not lifetime-extended.
    clock.set(1300)
    gone = await restarted_harness.run(restricted.cookie_value)
    assert gone.session.expired
    assert gone.session.empty
    assert gone.cookie_value is None


async def test_expired_read_only_request_clears_backend_record() -> None:
    """Timeline: create t=1000; probe t=1300 with no handler write.

    Loading an expired session marks it changed, so even a read-only
    request removes the stale backend record and cookie.
    """
    clock = FrozenClock(1000)
    redis = RedisStorage(
        FakeRedis(), max_age=IDLE, absolute_max_age=ABSOLUTE, clock=clock
    )
    harness = Harness(redis, clock)
    first = await harness.run(None, lambda s, _c: s.__setitem__("a", 1))
    assert first.backend_present

    clock.set(1300)
    expired = await harness.run(first.cookie_value)

    assert expired.saved
    assert not expired.backend_present
    assert expired.cookie_value is None


@pytest.mark.parametrize("name,make", _factories(), ids=lambda value: value)
async def test_read_only_request_crossing_absolute_deadline_clears(
    name: str, make: Callable[[FrozenClock], Any]
) -> None:
    """Timeline: create t=1000 with idle=600s, absolute=110s.

    A read-only probe at t=1110 is inside the idle window (until
    t=1600) but past the absolute deadline t=1110.  No handler write
    occurs, yet the stale session must be cleared.
    """
    clock = FrozenClock(1000)
    storage = _reconstruct(make(clock), clock, max_age=600, absolute_max_age=110)
    harness = Harness(storage, clock)
    assert name

    first = await harness.run(
        None, lambda session, _c: session.__setitem__("role", "viewer")
    )
    assert first.cookie_value is not None

    clock.set(1110)
    read = await harness.run(first.cookie_value)

    assert read.saved
    assert read.session.absolute_expired
    assert read.session.idle_expired is False
    assert read.session.empty
    assert read.cookie_value is None
    assert read.cookie_max_age == 0
    if harness.backend_storage:
        assert not read.backend_present
