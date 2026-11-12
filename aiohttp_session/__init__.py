"""User sessions for aiohttp.web."""

__version__ = "2.12.1"

import abc
import asyncio
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
    session: dict[str, Any]


class Session(MutableMapping[str, Any]):

    """Session dict-like object."""

    def __init__(
        self,
        identity: Any | None,
        *,
        data: SessionData | None,
        new: bool,
        max_age: int | None = None,
    ) -> None:
        self._changed: bool = False
        self._mapping: dict[str, Any] = {}
        self._identity = identity if data != {} else None
        self._new = new if data != {} else True
        self._max_age = max_age
        created = data.get("created", None) if data else None
        session_data = data.get("session", None) if data else None
        now = int(time.time())
        age = now - created if created else now
        if max_age is not None and age > max_age:
            session_data = None
        if self._new or created is None:
            self._created = now
        else:
            self._created = created

        if session_data is not None:
            self._mapping.update(session_data)

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
    def empty(self) -> bool:
        return not bool(self._mapping)

    @property
    def max_age(self) -> int | None:
        return self._max_age

    @max_age.setter
    def max_age(self, value: int | None) -> None:
        self._max_age = value

    def changed(self) -> None:
        self._changed = True

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
        self._changed = True
        self._created = int(time.time())

    def __delitem__(self, key: str) -> None:
        del self._mapping[key]
        self._changed = True
        self._created = int(time.time())


SESSION_KEY = "aiohttp_session"
STORAGE_KEY = "aiohttp_session_storage"

_SESSION_STATE_KEY = object()


class _SessionState:
    """Request-scoped arbitration state for lazy session loading.

    Only one session may be committed for a request at a time.  A load
    or a ``new_session()`` call in progress is represented by a shared
    task, so concurrent callers join it instead of starting a second
    one.  ``generation`` invalidates results produced before a
    ``new_session()`` call.
    """

    __slots__ = (
        "failure",
        "generation",
        "load_task",
        "new_committed",
        "new_task",
        "session",
    )

    def __init__(self, session: Session | None = None) -> None:
        self.session = session
        self.load_task: asyncio.Task[None] | None = None
        self.new_task: asyncio.Task[None] | None = None
        self.generation = 0
        # True when this generation's identity came from new_session().
        self.new_committed = False
        # An error that made the current generation unusable.  It is
        # re-raised for later callers rather than falling back to a load
        # of a superseded identity.
        self.failure: BaseException | None = None

    def invalidate(self) -> int:
        """Start a new generation and return its number.

        Pending results and captured failures from older generations
        must not be committed.
        """
        self.generation += 1
        self.session = None
        self.new_committed = False
        self.failure = None
        return self.generation


def _get_state(request: web.Request) -> _SessionState | None:
    return cast(
        "_SessionState | None",
        cast(Any, request)._state.get(_SESSION_STATE_KEY),
    )


def _get_or_create_state(request: web.Request) -> _SessionState:
    state = _get_state(request)
    if state is None:
        state = _SessionState(cast("Session | None", request.get(SESSION_KEY)))
        cast(Any, request)._state[_SESSION_STATE_KEY] = state
    return state


def _publish_session(request: web.Request, session: Session) -> None:
    # Do not use ``request[SESSION_KEY] = ...``: aiohttp 3.14+ emits
    # NotAppKeyWarning for string keys there, and warnings may be errors.
    cast(Any, request)._state[SESSION_KEY] = session


def _forget_session(request: web.Request) -> None:
    cast(Any, request)._state.pop(SESSION_KEY, None)


def _consume_task_result(task: asyncio.Task[None]) -> None:
    # Mark exceptions as retrieved even if every waiter was cancelled.
    if not task.cancelled():
        task.exception()


def _abandon_pending(state: _SessionState) -> None:
    """Cancel lazy work that outlived the handler.

    An unawaited load must not commit after the response is built, nor
    be saved.  Cancelling is request-local: it never blocks another
    request or the handler itself.
    """
    for task in (state.load_task, state.new_task):
        if task is not None and not task.done():
            task.cancel()
    # A task cancelled before its coroutine started never runs its
    # ``finally``, so clear the slots here as well.
    state.load_task = None
    state.new_task = None
    state.generation += 1


def _get_storage(request: web.Request) -> "AbstractStorage":
    storage = request.get(STORAGE_KEY)
    if storage is None:
        raise RuntimeError(
            "Install aiohttp_session middleware " "in your aiohttp.web.Application"
        )
    return cast("AbstractStorage", storage)


async def _load_session(
    request: web.Request,
    storage: "AbstractStorage",
    state: _SessionState,
    generation: int,
) -> None:
    task = asyncio.current_task()
    assert task is not None
    try:
        session = await storage.load_session(request)
        if not isinstance(session, Session):
            raise RuntimeError(
                "Installed {!r} storage should return session instance "
                "on .load_session() call, got {!r}.".format(storage, session)
            )
        if state.generation == generation and state.load_task is task:
            state.session = session
            _publish_session(request, session)
    finally:
        if state.load_task is task:
            state.load_task = None


async def _create_new_session(
    request: web.Request,
    storage: "AbstractStorage",
    state: _SessionState,
    generation: int,
) -> None:
    task = asyncio.current_task()
    assert task is not None
    try:
        session = await storage.new_session()
        if not isinstance(session, Session):
            raise RuntimeError(
                "Installed {!r} storage should return session instance "
                "on .load_session() call, got {!r}.".format(storage, session)
            )
        if state.generation == generation and state.new_task is task:
            state.session = session
            state.new_committed = True
            _publish_session(request, session)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # The old identity was already invalidated.  Remember the error
        # so a regular get_session() cannot fall back to loading it again.
        if state.generation == generation and state.new_task is task:
            state.failure = exc
        raise
    finally:
        if state.new_task is task:
            state.new_task = None


async def get_session(request: web.Request) -> Session:
    state = _get_or_create_state(request)

    while True:
        if state.session is not None:
            return state.session

        # A new identity always wins over a load of the old one.
        new_task = state.new_task
        if new_task is not None:
            if not new_task.done():
                try:
                    await asyncio.shield(new_task)
                except asyncio.CancelledError:
                    raise
                continue
            # A cancelled or abandoned attempt is not joined; retry.
            state.new_task = None

        if state.failure is not None:
            raise state.failure

        load_task = state.load_task
        load_generation = state.generation
        if load_task is None or load_task.done():
            # An abandoned task skipped its ``finally``; do not resurrect
            # its result or exception.
            storage = _get_storage(request)
            load_task = asyncio.create_task(
                _load_session(request, storage, state, load_generation)
            )
            load_task.add_done_callback(_consume_task_result)
            state.load_task = load_task

        # shield() keeps the shared load alive if only this waiter is
        # cancelled; every other caller still observes the same result.
        try:
            await asyncio.shield(load_task)
        except asyncio.CancelledError:
            raise
        except Exception:
            if state.generation == load_generation:
                raise
            # new_session() superseded this load.  Its outcome is no
            # longer authoritative; join the new identity instead.
            continue


async def new_session(request: web.Request) -> Session:
    state = _get_or_create_state(request)

    while True:
        if state.session is not None and state.new_committed:
            # A new identity from this request is already committed.
            return state.session

        new_task = state.new_task
        if new_task is not None:
            if not new_task.done():
                # Coalesce concurrent new_session() calls: one new
                # identity per request until the previous one commits.
                try:
                    await asyncio.shield(new_task)
                except asyncio.CancelledError:
                    raise
                continue
            # A completed attempt is not joined again.  This also avoids
            # replaying cancellation into an unrelated caller.
            state.new_task = None
            if state.session is not None and state.new_committed:
                return state.session

        # Invalidate the old identity before awaiting the storage.  An
        # in-flight load of the old session can no longer commit, and a
        # failed attempt must not be retried implicitly as an old load.
        storage = _get_storage(request)
        generation = state.invalidate()
        _forget_session(request)
        new_task = asyncio.create_task(
            _create_new_session(request, storage, state, generation)
        )
        new_task.add_done_callback(_consume_task_result)
        state.new_task = new_task

        # shield() keeps the shared new identity alive if only this
        # caller is cancelled; other new_session() callers still join it.
        try:
            await asyncio.shield(new_task)
        except asyncio.CancelledError:
            raise
        if state.session is not None:
            return state.session
        continue


def session_middleware(storage: "AbstractStorage") -> Middleware:
    if not isinstance(storage, AbstractStorage):
        raise RuntimeError(f"Expected AbstractStorage got {storage}")

    @web.middleware
    async def factory(request: web.Request, handler: Handler) -> web.StreamResponse:
        cast(Any, request)._state[STORAGE_KEY] = storage
        raise_response = False
        # TODO aiohttp 4:
        # Remove Union from response, and drop the raise_response variable
        response: web.StreamResponse | web.HTTPException
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc
            raise_response = True
        except BaseException:
            # Handler failed or was cancelled: do not leave lazy work
            # running past the request lifetime.
            state = _get_state(request)
            if state is not None:
                _abandon_pending(state)
            raise
        if not isinstance(response, (web.StreamResponse, web.HTTPException)):
            raise RuntimeError(f"Expect response, not {type(response)!r}")
        if not isinstance(response, (web.Response, web.HTTPException)):
            # likely got websocket or streaming
            state = _get_state(request)
            if state is not None:
                _abandon_pending(state)
            return response
        if response.prepared:
            raise RuntimeError("Cannot save session data into prepared response")
        state = _get_state(request)
        if state is not None:
            # Only the committed session is saved.  A load that is still
            # in flight, or one superseded by new_session(), is ignored.
            _abandon_pending(state)
            session = state.session
            if session is None:
                # Honour a Session stored directly on the request.
                session = cast("Session | None", request.get(SESSION_KEY))
        else:
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
        self._encoder = encoder
        self._decoder = decoder

    @property
    def cookie_name(self) -> str:
        return self._cookie_name

    @property
    def max_age(self) -> int | None:
        return self._max_age

    @property
    def cookie_params(self) -> _CookieParams:
        return self._cookie_params

    def _get_session_data(self, session: Session) -> SessionData:
        if session.empty:
            return {}

        return {"created": session.created, "session": session._mapping}

    async def new_session(self) -> Session:
        return Session(None, data=None, new=True, max_age=self.max_age)

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
            t = time.gmtime(time.time() + max_age)
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
        )

    async def load_session(self, request: web.Request) -> Session:
        cookie = self.load_cookie(request)
        if cookie is None:
            return Session(None, data=None, new=True, max_age=self.max_age)

        data = self._decoder(cookie)
        return Session(None, data=data, new=False, max_age=self.max_age)

    async def save_session(
        self, request: web.Request, response: web.StreamResponse, session: Session
    ) -> None:
        cookie_data = self._encoder(self._get_session_data(session))
        self.save_cookie(response, cookie_data, max_age=session.max_age)
