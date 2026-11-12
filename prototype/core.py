"""Deterministic, in-memory replica of the aiohttp_session cookie path.

The point of this module is to exercise the *exact* load / expiry / save logic
of :class:`aiohttp_session.cookie_storage.EncryptedCookieStorage` without a
network or a real wall clock, so revocation guarantees can be reasoned about
with two local clients and a controllable clock.

Only the standard library and the project's existing ``cryptography``
dependency are used.
"""

import json
import time
from collections.abc import Callable
from typing import Any

from cryptography import fernet
from cryptography.fernet import InvalidToken


class Clock:
    """A replaceable clock; mirrors ``time.time()`` but advances on demand."""

    def __init__(self, start: int = 1_000_000) -> None:
        self._now = int(start)

    def now(self) -> int:
        return self._now

    def advance(self, seconds: int) -> None:
        self._now += int(seconds)


class Session:
    """Fork of :class:`aiohttp_session.Session` driven by an injected clock."""

    def __init__(
        self,
        *,
        data: dict[str, Any] | None,
        new: bool,
        max_age: int | None,
        clock: Clock,
    ) -> None:
        self._changed = False
        self._mapping: dict[str, Any] = {}
        self._new = new if data != {} else True
        self._max_age = max_age
        self._clock = clock
        created = data.get("created", None) if data else None
        session_data = data.get("session", None) if data else None
        now = clock.now()
        age = now - created if created else now
        if max_age is not None and age > max_age:
            session_data = None
        if self._new or created is None:
            self._created = now
        else:
            self._created = created
        if session_data is not None:
            self._mapping.update(session_data)

    @property
    def new(self) -> bool:
        return self._new

    @property
    def created(self) -> int:
        return self._created

    @property
    def empty(self) -> bool:
        return not bool(self._mapping)

    @property
    def max_age(self) -> int | None:
        return self._max_age

    @property
    def changed(self) -> bool:
        return self._changed

    def changed_mark(self) -> None:
        self._changed = True

    def invalidate(self) -> None:
        self._changed = True
        self._mapping = {}

    def __contains__(self, key: object) -> bool:
        return key in self._mapping

    def __getitem__(self, key: str) -> Any:
        return self._mapping[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._mapping[key] = value
        self._changed = True
        self._created = self._clock.now()

    def __delitem__(self, key: str) -> None:
        del self._mapping[key]
        self._changed = True
        self._created = self._clock.now()

    def as_dict(self) -> dict[str, Any]:
        return dict(self._mapping)


Encoder = Callable[[object], str]
Decoder = Callable[[str], Any]


class EncryptedCookieStorage:
    """Fork of EncryptedCookieStorage with a clock instead of ``time.time``."""

    def __init__(
        self,
        secret_key: bytes | fernet.Fernet,
        *,
        max_age: int | None = None,
        clock: Clock,
        encoder: Encoder = json.dumps,
        decoder: Decoder = json.loads,
        cookie_name: str = "AIOHTTP_SESSION",
    ) -> None:
        self._fernet = (
            secret_key
            if isinstance(secret_key, fernet.Fernet)
            else fernet.Fernet(secret_key)
        )
        self._max_age = max_age
        self._clock = clock
        self._encoder = encoder
        self._decoder = decoder
        self.cookie_name = cookie_name

    @property
    def max_age(self) -> int | None:
        return self._max_age

    def _decrypt_raw(self, cookie: str) -> dict[str, Any] | None:
        """Return the decoded cookie payload, or None if it is unusable."""
        try:
            plaintext = self._fernet.decrypt_at_time(
                cookie.encode("utf-8"),
                ttl=self._max_age,
                current_time=self._clock.now(),
            ).decode("utf-8")
        except InvalidToken:
            return None
        return self._decoder(plaintext)

    def load_session(self, cookie: str | None) -> Session:
        if cookie is None:
            return Session(
                data=None, new=True, max_age=self._max_age, clock=self._clock
            )
        data = self._decrypt_raw(cookie)
        if data is None:
            # Matches the upstream "fresh session" fallback on InvalidToken.
            return Session(
                data=None, new=True, max_age=self._max_age, clock=self._clock
            )
        return Session(
            data=data, new=False, max_age=self._max_age, clock=self._clock
        )

    def _session_payload(self, session: Session) -> dict[str, Any]:
        if session.empty:
            return {}
        return {"created": session.created, "session": session.as_dict()}

    def save_session(self, session: Session) -> str | None:
        """Return the Set-Cookie value; None means "delete", per upstream."""
        if session.empty:
            return None
        payload = self._encoder(self._session_payload(session)).encode("utf-8")
        return self._fernet.encrypt_at_time(
            payload, current_time=self._clock.now()
        ).decode("utf-8")


class Client:
    """A cookie jar + storage pair, standing in for one browser/device."""

    def __init__(self, name: str, storage: EncryptedCookieStorage) -> None:
        self.name = name
        self._storage = storage
        self._cookie: str | None = None

    def request(self, handler: Callable[[Session], None]) -> dict[str, Any]:
        """Run one request through load -> handler -> (conditional) save."""
        session = self._storage.load_session(self._cookie)
        handler(session)
        if session.changed:
            self._cookie = self._storage.save_session(session)
        return {
            "client": self.name,
            "authenticated": not session.empty,
            "session": session.as_dict(),
            "new": session.new,
            "cookie_present": self._cookie is not None,
        }

    def drop_cookie(self) -> None:
        self._cookie = None
