"""User sessions for aiohttp.web."""

__version__ = "2.12.1"

import abc
import json
import time
from collections.abc import Callable, Iterator, MutableMapping
from typing import Any, TypedDict, cast

from aiohttp import web
from aiohttp.typedefs import Handler, Middleware


class _CookieParams(TypedDict, total=False):
    domain: str | None
    max_age: int | None
    path: str
    secure: bool | None
    httponly: bool
    samesite: str | None
    expires: str


class SessionData(TypedDict, total=False):
    created: int
    last_used: int
    absolute_deadline: int
    session: dict[str, Any]


class Session(MutableMapping[str, Any]):

    """Session dict-like object.

    A session has two independent lifetime limits:

    * ``max_age`` is an idle timeout.  It is measured from the last time
      the session was changed (or created).
    * ``absolute_max_age`` is an absolute lifetime, measured from session
      creation and persisted as ``absolute_deadline``.  Changing the
      session, including renewing the idle timeout, never moves this
      deadline.
    """

    def __init__(
        self,
        identity: Any | None,
        *,
        data: SessionData | None,
        new: bool,
        max_age: int | None = None,
        absolute_max_age: int | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._changed: bool = False
        self._mapping: dict[str, Any] = {}
        self._identity = identity if data != {} else None
        self._new = new if data != {} else True
        self._max_age = max_age
        self._absolute_max_age = absolute_max_age
        self._clock = clock
        self._expired = False
        self._idle_expired = False
        self._absolute_expired = False
        now = self._now()

        created = data.get("created", None) if data else None
        if created is None and data:
            # Legacy payload without a creation timestamp: start its
            # absolute lifetime when it is first seen by this version.
            created = now
        last_used = data.get("last_used", created) if data else None
        session_data = data.get("session", None) if data else None

        configured_deadline = (
            created + self._absolute_max_age
            if created is not None and self._absolute_max_age is not None
            else None
        )
        stored_deadline = data.get("absolute_deadline", None) if data else None
        if stored_deadline is not None and configured_deadline is not None:
            self._absolute_deadline = min(stored_deadline, configured_deadline)
        elif stored_deadline is not None:
            self._absolute_deadline = stored_deadline
        else:
            self._absolute_deadline = configured_deadline

        idle_expired = (
            self._max_age is not None
            and last_used is not None
            and now - last_used >= self._max_age
        )
        absolute_expired = (
            self._absolute_deadline is not None and now >= self._absolute_deadline
        )

        self._expired = idle_expired or absolute_expired
        self._idle_expired = idle_expired
        self._absolute_expired = absolute_expired

        if absolute_expired:
            # A save after the absolute deadline must never revive the
            # session.  Keep the original deadline so the save path can
            # detect it even if the handler writes new data.
            self._changed = True
            self._new = True
            session_data = None
            self._mapping.clear()
            self._created = created if created is not None else now
            self._last_used = last_used if last_used is not None else now
        elif idle_expired:
            # Idle expiry discards the data but keeps the absolute
            # lineage.  A read-only request leaves the session empty and
            # the save path removes the stale cookie/backend record; a
            # handler that writes new data renews the idle window under
            # the original absolute deadline.
            self._changed = True
            self._new = True
            session_data = None
            self._mapping.clear()
            self._created = created if created is not None else now
            self._last_used = now
        else:
            if self._new or created is None:
                self._created = now
            else:
                self._created = created

            if self._new:
                self._last_used = now
                if self._absolute_max_age is not None:
                    self._absolute_deadline = now + self._absolute_max_age
            elif created is None:
                self._last_used = now
            else:
                self._last_used = last_used if last_used is not None else created

        if session_data is not None:
            self._mapping.update(session_data)

    def _now(self) -> int:
        clock = self._clock if self._clock is not None else time.time
        return int(clock())

    def _touch(self) -> None:
        self._changed = True
        # A backwards clock jump must not shorten the recorded idle age
        # (and hence must not cause an earlier idle expiry later).
        self._last_used = max(self._last_used, self._now())

    def __repr__(self) -> str:
        return "<{} [new:{}, changed:{}, created:{}] {!r}>".format(
            self.__class__.__name__,
            self.new,
            self._changed,
            self.created,
            self._mapping,
        )

    @property
    def new(self) -> bool:
        return self._new

    @property
    def identity(self) -> Any | None:
        return self._identity

    @property
    def created(self) -> int:
        return self._created

    @property
    def last_used(self) -> int:
        return self._last_used

    @property
    def absolute_max_age(self) -> int | None:
        return self._absolute_max_age

    @property
    def absolute_deadline(self) -> int | None:
        return self._absolute_deadline

    @property
    def expired(self) -> bool:
        return self._expired

    @property
    def idle_expired(self) -> bool:
        return self._idle_expired

    @property
    def absolute_expired(self) -> bool:
        return self._absolute_expired

    @property
    def cookie_max_age(self) -> int | None:
        """Effective lifetime for this save, in seconds.

        It is the smaller of the idle allowance and the remaining time
        until the absolute deadline.  The idle allowance is measured
        from ``last_used``, not from the current clock, so moving the
        clock backwards cannot shorten a session's recorded lifetime.
        """
        now = self._now()
        limits: list[int] = []
        if self._max_age is not None:
            # Cap the idle allowance at max_age as well: after a
            # backwards clock jump the arithmetic remainder can look
            # longer than one full idle window, and a cookie must never
            # be issued for longer than the configured idle timeout.
            limits.append(
                min(self._max_age, self._last_used + self._max_age - now)
            )
        if self._absolute_deadline is not None:
            remaining = self._absolute_deadline - now
            if self._absolute_max_age is not None:
                # A backwards clock jump must not grant more than one
                # full absolute lifetime either.
                remaining = min(remaining, self._absolute_max_age)
            limits.append(remaining)
        if not limits:
            return None
        return max(0, min(limits))

    def _save_should_clear(self) -> bool:
        """Whether this save must delete the session instead of writing."""
        if self._absolute_expired:
            return True
        return self._expired and self.empty

    @property
    def empty(self) -> bool:
        return not bool(self._mapping)

    @property
    def max_age(self) -> int | None:
        return self._max_age

    @max_age.setter
    def max_age(self, value: int | None) -> None:
        self._max_age = value

    def changed(self) -> None:
        self._touch()

    def invalidate(self) -> None:
        self._changed = True
        self._mapping = {}

    def set_new_identity(self, identity: Any | None) -> None:
        if not self._new:
            raise RuntimeError("Can't change identity for a session which is not new")

        self._identity = identity

    def __len__(self) -> int:
        return len(self._mapping)

    def __iter__(self) -> Iterator[str]:
        return iter(self._mapping)

    def __contains__(self, key: object) -> bool:
        return key in self._mapping

    def __getitem__(self, key: str) -> Any:
        return self._mapping[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._mapping[key] = value
        self._touch()

    def __delitem__(self, key: str) -> None:
        del self._mapping[key]
        self._touch()


SESSION_KEY = "aiohttp_session"
STORAGE_KEY = "aiohttp_session_storage"


async def get_session(request: web.Request) -> Session:
    session = request.get(SESSION_KEY)
    if session is None:
        storage = request.get(STORAGE_KEY)
        if storage is None:
            raise RuntimeError(
                "Install aiohttp_session middleware " "in your aiohttp.web.Application"
            )

        session = await storage.load_session(request)
        if not isinstance(session, Session):
            raise RuntimeError(
                "Installed {!r} storage should return session instance "
                "on .load_session() call, got {!r}.".format(storage, session)
            )
        request[SESSION_KEY] = session
    return session


async def new_session(request: web.Request) -> Session:
    storage = request.get(STORAGE_KEY)
    if storage is None:
        raise RuntimeError(
            "Install aiohttp_session middleware " "in your aiohttp.web.Application"
        )

    session = await storage.new_session()
    if not isinstance(session, Session):
        raise RuntimeError(
            "Installed {!r} storage should return session instance "
            "on .load_session() call, got {!r}.".format(storage, session)
        )
    request[SESSION_KEY] = session
    return session


def session_middleware(storage: "AbstractStorage") -> Middleware:
    if not isinstance(storage, AbstractStorage):
        raise RuntimeError(f"Expected AbstractStorage got {storage}")

    @web.middleware
    async def factory(request: web.Request, handler: Handler) -> web.StreamResponse:
        request[STORAGE_KEY] = storage
        raise_response = False
        # TODO aiohttp 4:
        # Remove Union from response, and drop the raise_response variable
        response: web.StreamResponse | web.HTTPException
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc
            raise_response = True
        if not isinstance(response, (web.StreamResponse, web.HTTPException)):
            raise RuntimeError(f"Expect response, not {type(response)!r}")
        if not isinstance(response, (web.Response, web.HTTPException)):
            # likely got websocket or streaming
            return response
        if response.prepared:
            raise RuntimeError("Cannot save session data into prepared response")
        session = request.get(SESSION_KEY)
        if session is not None:
            if session._changed:
                await storage.save_session(request, response, session)
        if raise_response:
            raise cast(web.HTTPException, response)
        return response

    return factory


def setup(app: web.Application, storage: "AbstractStorage") -> None:
    """Setup the library in aiohttp fashion."""

    app.middlewares.append(session_middleware(storage))


class AbstractStorage(metaclass=abc.ABCMeta):
    def __init__(
        self,
        *,
        cookie_name: str = "AIOHTTP_SESSION",
        domain: str | None = None,
        max_age: int | None = None,
        path: str = "/",
        secure: bool | None = None,
        httponly: bool = True,
        samesite: str | None = None,
        encoder: Callable[[object], str] = json.dumps,
        decoder: Callable[[str], Any] = json.loads,
        absolute_max_age: int | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._cookie_name = cookie_name
        self._cookie_params = _CookieParams(
            domain=domain,
            max_age=max_age,
            path=path,
            secure=secure,
            httponly=httponly,
            samesite=samesite,
        )
        self._max_age = max_age
        self._absolute_max_age = absolute_max_age
        self._clock = clock
        self._encoder = encoder
        self._decoder = decoder

    @property
    def cookie_name(self) -> str:
        return self._cookie_name

    @property
    def max_age(self) -> int | None:
        return self._max_age

    @property
    def absolute_max_age(self) -> int | None:
        return self._absolute_max_age

    @property
    def cookie_params(self) -> _CookieParams:
        return self._cookie_params

    def _now(self) -> int:
        clock = self._clock if self._clock is not None else time.time
        return int(clock())

    def _deadline_passed(self, session: Session) -> bool:
        deadline = session.absolute_deadline
        return deadline is not None and self._now() >= deadline

    def _should_expire(self, session: Session) -> bool:
        if session._save_should_clear() or self._deadline_passed(session):
            return True
        # A handler that straddles the idle deadline computes a zero
        # TTL at save time.  Clear instead of writing a zero-lifetime
        # cookie or backend entry.
        return session.cookie_max_age == 0

    def _new_session(self) -> Session:
        return Session(
            None,
            data=None,
            new=True,
            max_age=self.max_age,
            absolute_max_age=self.absolute_max_age,
            clock=self._clock,
        )

    def _load_session(self, identity: Any | None, data: SessionData | None) -> Session:
        return Session(
            identity,
            data=data,
            new=False,
            max_age=self.max_age,
            absolute_max_age=self.absolute_max_age,
            clock=self._clock,
        )

    def _get_session_data(self, session: Session) -> SessionData:
        if session.empty:
            return {}

        data: SessionData = {
            "created": session.created,
            "last_used": session.last_used,
            "session": session._mapping,
        }
        if session.absolute_deadline is not None:
            data["absolute_deadline"] = session.absolute_deadline
        return data

    async def new_session(self) -> Session:
        return self._new_session()

    def _ttl(self, session: Session) -> int | None:
        """Backend TTL for this save, capped by the absolute deadline."""
        return session.cookie_max_age

    @abc.abstractmethod
    async def load_session(self, request: web.Request) -> Session:
        pass

    @abc.abstractmethod
    async def save_session(
        self, request: web.Request, response: web.StreamResponse, session: Session
    ) -> None:
        pass

    def load_cookie(self, request: web.Request) -> str | None:
        return request.cookies.get(self._cookie_name)

    def save_cookie(
        self,
        response: web.StreamResponse,
        cookie_data: str,
        *,
        max_age: int | None = None,
    ) -> None:
        params = self._cookie_params.copy()
        if max_age is not None:
            params["max_age"] = max_age
            t = time.gmtime(self._now() + max_age)
            params["expires"] = time.strftime("%a, %d-%b-%Y %T GMT", t)
        if not cookie_data:
            response.del_cookie(
                self._cookie_name, domain=params["domain"], path=params["path"]
            )
        else:
            response.set_cookie(self._cookie_name, cookie_data, **params)


class SimpleCookieStorage(AbstractStorage):
    """Simple JSON storage.

    Doesn't any encryption/validation, use it for tests only"""

    def __init__(
        self,
        *,
        cookie_name: str = "AIOHTTP_SESSION",
        domain: str | None = None,
        max_age: int | None = None,
        path: str = "/",
        secure: bool | None = None,
        httponly: bool = True,
        samesite: str | None = None,
        encoder: Callable[[object], str] = json.dumps,
        decoder: Callable[[str], Any] = json.loads,
        absolute_max_age: int | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(
            cookie_name=cookie_name,
            domain=domain,
            max_age=max_age,
            path=path,
            secure=secure,
            httponly=httponly,
            samesite=samesite,
            encoder=encoder,
            decoder=decoder,
            absolute_max_age=absolute_max_age,
            clock=clock,
        )

    async def load_session(self, request: web.Request) -> Session:
        cookie = self.load_cookie(request)
        if cookie is None:
            return self._new_session()

        data = self._decoder(cookie)
        return self._load_session(None, data)

    async def save_session(
        self, request: web.Request, response: web.StreamResponse, session: Session
    ) -> None:
        if self._should_expire(session):
            self.save_cookie(response, "")
            return
        cookie_data = self._encoder(self._get_session_data(session))
        self.save_cookie(response, cookie_data, max_age=session.cookie_max_age)
