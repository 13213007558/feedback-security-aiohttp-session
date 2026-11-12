"""User sessions for aiohttp.web."""

__version__ = "2.12.1"

import abc
import enum
import json
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator, MutableMapping
from dataclasses import dataclass
from typing import Any, TypedDict, cast

from aiohttp import web
from aiohttp.typedefs import Handler, Middleware

from .log import log


class LimitKind(str, enum.Enum):
    """Kind of resource limit that was exceeded while loading a session."""

    BYTES = "bytes"
    DEPTH = "depth"
    CONTAINERS = "containers"


@dataclass(frozen=True)
class LimitViolation:
    """Details of a rejected session payload.

    *kind* identifies the breached limit, *limit* the configured ceiling
    and *observed* the actual measured value.
    """

    kind: LimitKind
    limit: int
    observed: int


# A security policy invoked by the middleware when a storage rejects a
# session payload because it exceeds a configured resource limit.
LimitExceededHandler = Callable[
    [web.Request, "AbstractStorage", LimitViolation], Awaitable[None]
]


async def default_limit_exceeded_handler(
    request: web.Request,
    storage: "AbstractStorage",
    violation: LimitViolation,
) -> None:
    """Default security policy: log the rejection and refuse the request.

    The offending payload has already been discarded by the storage, so no
    untrusted data is ever persisted. Byte budget violations are reported as
    ``413 Payload Too Large``, structural violations as ``400 Bad Request``.
    """
    log.warning(
        "Rejected session cookie %r: %s limit %s exceeded (observed %s)",
        storage.cookie_name,
        violation.kind.value,
        violation.limit,
        violation.observed,
    )
    if violation.kind is LimitKind.BYTES:
        raise web.HTTPRequestEntityTooLarge(
            max_size=violation.limit, actual_size=violation.observed
        )
    raise web.HTTPBadRequest(
        reason=(
            f"Session {violation.kind.value} limit {violation.limit} "
            f"exceeded (observed {violation.observed})"
        )
    )


def measure_json_complexity(value: Any) -> tuple[int, int]:
    """Measure decoded data complexity without recursion.

    Returns a ``(depth, container_count)`` tuple. *depth* counts the nesting
    levels (scalars have depth 1, an empty container has depth 1 as well) and
    *container_count* is the total number of ``dict``/``list``/``tuple``
    containers encountered. The traversal is iterative so a deeply nested or
    hostile payload cannot exhaust the Python recursion limit.
    """
    stack: list[tuple[Any, int]] = [(value, 1)]
    max_depth = 0
    container_count = 0
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            max_depth = depth
        if isinstance(current, (dict, list, tuple)):
            container_count += 1
            if isinstance(current, dict):
                children: Iterable[Any] = current.values()
            else:
                children = current
            for child in children:
                stack.append((child, depth + 1))
    return max_depth, container_count


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


def session_middleware(
    storage: "AbstractStorage",
    on_limit_exceeded: LimitExceededHandler | None = None,
) -> Middleware:
    if not isinstance(storage, AbstractStorage):
        raise RuntimeError(f"Expected AbstractStorage got {storage}")

    if on_limit_exceeded is not None:
        storage.on_limit_exceeded = on_limit_exceeded

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


def setup(
    app: web.Application,
    storage: "AbstractStorage",
    on_limit_exceeded: LimitExceededHandler | None = None,
) -> None:
    """Setup the library in aiohttp fashion."""

    app.middlewares.append(session_middleware(storage, on_limit_exceeded))


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
        max_blob_size: int | None = None,
        max_session_depth: int | None = None,
        max_session_containers: int | None = None,
        on_limit_exceeded: LimitExceededHandler = default_limit_exceeded_handler,
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
        for name, value in (
            ("max_blob_size", max_blob_size),
            ("max_session_depth", max_session_depth),
            ("max_session_containers", max_session_containers),
        ):
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be a positive integer or None")
        self._max_blob_size = max_blob_size
        self._max_session_depth = max_session_depth
        self._max_session_containers = max_session_containers
        self.on_limit_exceeded = on_limit_exceeded

    @property
    def cookie_name(self) -> str:
        return self._cookie_name

    @property
    def max_age(self) -> int | None:
        return self._max_age

    @property
    def cookie_params(self) -> _CookieParams:
        return self._cookie_params

    @property
    def max_blob_size(self) -> int | None:
        return self._max_blob_size

    @property
    def max_session_depth(self) -> int | None:
        return self._max_session_depth

    @property
    def max_session_containers(self) -> int | None:
        return self._max_session_containers

    def _check_blob_size(self, blob: str | bytes) -> LimitViolation | None:
        """Reject the raw, still-encoded payload before any decoding."""
        if self._max_blob_size is None:
            return None
        observed = len(blob.encode("utf-8")) if isinstance(blob, str) else len(blob)
        if observed > self._max_blob_size:
            return LimitViolation(
                LimitKind.BYTES, self._max_blob_size, observed
            )
        return None

    def _check_structure(self, data: Any) -> LimitViolation | None:
        """Reject decoded payloads that are too deeply nested or too wide."""
        depth, container_count = measure_json_complexity(data)
        if (
            self._max_session_depth is not None
            and depth > self._max_session_depth
        ):
            return LimitViolation(
                LimitKind.DEPTH, self._max_session_depth, depth
            )
        if (
            self._max_session_containers is not None
            and container_count > self._max_session_containers
        ):
            return LimitViolation(
                LimitKind.CONTAINERS,
                self._max_session_containers,
                container_count,
            )
        return None

    async def _handle_violation(
        self,
        request: web.Request,
        violation: LimitViolation,
    ) -> None:
        await self.on_limit_exceeded(request, self, violation)

    async def _reject_oversized_blob(
        self, request: web.Request, blob: str | bytes
    ) -> Session | None:
        """Enforce the byte budget on the raw transport representation.

        Returns a fresh empty session when the blob is rejected, otherwise
        ``None``. An oversized blob never reaches the decoder.
        """
        violation = self._check_blob_size(blob)
        if violation is None:
            return None
        await self._handle_violation(request, violation)
        return await self.new_session()

    async def _blob_violation(
        self, request: web.Request, blob: str | bytes
    ) -> LimitViolation | None:
        """Return the byte-budget violation for *blob*, running the policy.

        Like :meth:`_reject_oversized_blob` but without constructing a
        session, so key-based backends (Redis/Memcached) can preserve their
        own identity semantics.
        """
        violation = self._check_blob_size(blob)
        if violation is None:
            return None
        await self._handle_violation(request, violation)
        return violation

    async def _decode_session_text(self, request: web.Request, blob: str) -> Session:
        """Decode JSON text and enforce the structural complexity limits."""
        try:
            data = self._decoder(blob)
        except ValueError:
            return await self.new_session()

        violation = self._check_structure(data)
        if violation is not None:
            await self._handle_violation(request, violation)
            return await self.new_session()

        return Session(None, data=data, new=False, max_age=self.max_age)

    async def _load_decoded_session(
        self, request: web.Request, blob: str | bytes
    ) -> Session:
        """Load a plaintext JSON payload, enforcing every resource limit.

        The byte budget is checked on the raw transport representation first,
        so an oversized blob never reaches the decoder. Structural limits are
        measured on the decoded value. On any violation the payload is
        discarded and the configured security policy runs; this method then
        returns a fresh empty session so rejected data can never be saved
        back.
        """
        rejected = await self._reject_oversized_blob(request, blob)
        if rejected is not None:
            return rejected

        if isinstance(blob, bytes):
            blob = blob.decode("utf-8")
        return await self._decode_session_text(request, blob)

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
        max_blob_size: int | None = None,
        max_session_depth: int | None = None,
        max_session_containers: int | None = None,
        on_limit_exceeded: LimitExceededHandler = default_limit_exceeded_handler,
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
            max_blob_size=max_blob_size,
            max_session_depth=max_session_depth,
            max_session_containers=max_session_containers,
            on_limit_exceeded=on_limit_exceeded,
        )

    async def load_session(self, request: web.Request) -> Session:
        cookie = self.load_cookie(request)
        if cookie is None:
            return Session(None, data=None, new=True, max_age=self.max_age)

        return await self._load_decoded_session(request, cookie)

    async def save_session(
        self, request: web.Request, response: web.StreamResponse, session: Session
    ) -> None:
        cookie_data = self._encoder(self._get_session_data(session))
        self.save_cookie(response, cookie_data, max_age=session.max_age)
