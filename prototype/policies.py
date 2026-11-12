"""Revocation strategies layered on the encrypted-cookie transport.

Every strategy keeps the session *payload* inside the encrypted cookie. They
differ only in what server-side state is consulted while authorizing a
request, and therefore in how immediately and how precisely a session can be
revoked.
"""

import sqlite3
import uuid
from typing import Protocol

from cryptography import fernet

from core import Clock, EncryptedCookieStorage, Session


class GenerationStore(Protocol):
    def get(self, account: str) -> int: ...

    def bump(self, account: str) -> int: ...


class MemoryGenerationStore:
    """Smallest possible per-account state: one integer per account."""

    def __init__(self) -> None:
        self._gen: dict[str, int] = {}

    def get(self, account: str) -> int:
        return self._gen.get(account, 0)

    def bump(self, account: str) -> int:
        nxt = self.get(account) + 1
        self._gen[account] = nxt
        return nxt


class SqliteGenerationStore:
    """Low-dependency durable variant: stdlib sqlite3, no external service."""

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS account_gen "
            "(account TEXT PRIMARY KEY, gen INTEGER NOT NULL)"
        )
        self._conn.commit()

    def get(self, account: str) -> int:
        row = self._conn.execute(
            "SELECT gen FROM account_gen WHERE account = ?", (account,)
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def bump(self, account: str) -> int:
        nxt = self.get(account) + 1
        self._conn.execute(
            "INSERT INTO account_gen(account, gen) VALUES(?, ?) "
            "ON CONFLICT(account) DO UPDATE SET gen = excluded.gen",
            (account, nxt),
        )
        self._conn.commit()
        return nxt

    def close(self) -> None:
        self._conn.close()


class RevocationTable(Protocol):
    def contains(self, session_id: str) -> bool: ...

    def revoke(self, session_id: str) -> None: ...


class MemoryRevocationTable:
    """Central deny-list of individual session ids."""

    def __init__(self) -> None:
        self._revoked: set[str] = set()

    def contains(self, session_id: str) -> bool:
        return session_id in self._revoked

    def revoke(self, session_id: str) -> None:
        self._revoked.add(session_id)


class SqliteRevocationTable:
    """Durable deny-list with an expiry column so entries can be garbage kept."""

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS revoked_sessions "
            "(sid TEXT PRIMARY KEY, expire_at INTEGER NOT NULL)"
        )
        self._conn.commit()

    def contains(self, session_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM revoked_sessions WHERE sid = ?", (session_id,)
        ).fetchone()
        return row is not None

    def revoke(self, session_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO revoked_sessions(sid, expire_at) "
            "VALUES(?, ?)",
            (session_id, 0),
        )
        self._conn.commit()

    def purge(self, now: int) -> int:
        cur = self._conn.execute(
            "DELETE FROM revoked_sessions WHERE expire_at <= ?", (now,)
        )
        self._conn.commit()
        return cur.rowcount

    def close(self) -> None:
        self._conn.close()


def login(session: Session, account: str, token_generation: int) -> None:
    """Handler fragment: establish an authenticated session."""
    session["account"] = account
    session["token_gen"] = token_generation
    # A unique per-session id is only needed by the deny-list strategy.
    session["sid"] = uuid.uuid4().hex


class GenerationCookieStorage(EncryptedCookieStorage):
    """Stateless cookie + one per-account generation lookup per request."""

    def __init__(
        self,
        secret_key: bytes,
        generations: GenerationStore,
        *,
        clock: Clock,
        max_age: int | None = None,
    ) -> None:
        super().__init__(secret_key, max_age=max_age, clock=clock)
        self._generations = generations

    def authorize(self, cookie: str | None) -> tuple[Session, str | None]:
        session = self.load_session(cookie)
        reason = None
        if not session.empty:
            account = session["account"]
            if session["token_gen"] != self._generations.get(account):
                reason = "stale generation"
                session.invalidate()
        return session, reason


class DenyListCookieStorage(EncryptedCookieStorage):
    """Stateless cookie + one deny-list membership lookup per request."""

    def __init__(
        self,
        secret_key: bytes,
        denylist: RevocationTable,
        *,
        clock: Clock,
        max_age: int | None = None,
    ) -> None:
        super().__init__(secret_key, max_age=max_age, clock=clock)
        self._denylist = denylist

    def authorize(self, cookie: str | None) -> tuple[Session, str | None]:
        session = self.load_session(cookie)
        reason = None
        if not session.empty and self._denylist.contains(session["sid"]):
            reason = "session revoked"
            session.invalidate()
        return session, reason


def rotate_global_key() -> tuple[bytes, bytes]:
    """Replacing the global Fernet key invalidates *every* account at once."""
    return fernet.Fernet.generate_key(), fernet.Fernet.generate_key()
