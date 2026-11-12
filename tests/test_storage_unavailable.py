from __future__ import annotations

import base64
import socket
import uuid
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from cryptography import fernet
from redis import asyncio as aioredis

from aiohttp_session import (
    AbstractStorage,
    Session,
    SessionData,
    StorageUnavailableError,
    get_session,
    new_session,
    session_middleware,
)
from aiohttp_session.cookie_storage import EncryptedCookieStorage
from aiohttp_session.redis_storage import RedisStorage

from .typedefs import AiohttpClient


class FlakyStorage(AbstractStorage):
    """In-memory storage whose backend connection can be toggled."""

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[str, SessionData] = {}
        self.available = True

    def _check_available(self) -> None:
        if not self.available:
            raise StorageUnavailableError(self)

    async def load_session(self, request: web.Request) -> Session:
        self._check_available()
        cookie = self.load_cookie(request)
        if cookie is None:
            return Session(None, data=None, new=True, max_age=self.max_age)
        data = self.store.get(cookie)
        if data is None:
            return Session(None, data=None, new=True, max_age=self.max_age)
        return Session(cookie, data=data, new=False, max_age=self.max_age)

    async def save_session(
        self, request: web.Request, response: web.StreamResponse, session: Session
    ) -> None:
        self._check_available()
        key = session.identity
        if key is None:
            key = uuid.uuid4().hex
        if session.empty:
            self.store.pop(key, None)
            self.save_cookie(response, "", max_age=session.max_age)
        else:
            self.store[key] = self._get_session_data(session)
            self.save_cookie(response, key, max_age=session.max_age)


async def public_handler(request: web.Request) -> web.Response:
    session = await get_session(request)
    if session.unavailable:
        # Temporary anonymous state, must never be persisted.
        session["temp_anon"] = True
    user = session.get("user_id", "anonymous")
    return web.Response(text=f"public:{user}")


async def protected_handler(request: web.Request) -> web.Response:
    session = await get_session(request)
    if session.unavailable:
        raise web.HTTPServiceUnavailable(text="Service unavailable")
    if "user_id" not in session:
        raise web.HTTPUnauthorized(text="Login required")
    return web.Response(text=f"protected:{session['user_id']}")


async def login_handler(request: web.Request) -> web.Response:
    session = await new_session(request)
    session["user_id"] = "alice"
    return web.Response(text="logged in")


async def state_handler(request: web.Request) -> web.Response:
    session = await get_session(request)
    return web.json_response(
        {
            "new": session.new,
            "invalid": session.invalid,
            "unavailable": session.unavailable,
        }
    )


def create_app(storage: AbstractStorage) -> web.Application:
    app = web.Application(middlewares=[session_middleware(storage)])
    app.router.add_get("/", public_handler)
    app.router.add_get("/protected", protected_handler)
    app.router.add_post("/login", login_handler)
    app.router.add_get("/state", state_handler)
    return app


@pytest.fixture
def storage() -> FlakyStorage:
    return FlakyStorage()


def session_cookie(client: Any) -> str:
    cookies = client.session.cookie_jar.filter_cookies(client.make_url("/"))
    return cookies["AIOHTTP_SESSION"].value


async def test_normal_flow(
    aiohttp_client: AiohttpClient, storage: FlakyStorage
) -> None:
    client = await aiohttp_client(create_app(storage))

    resp = await client.post("/login")
    assert resp.status == 200
    key = session_cookie(client)
    assert storage.store[key]["session"] == {"user_id": "alice"}

    resp = await client.get("/protected")
    assert resp.status == 200
    assert await resp.text() == "protected:alice"

    resp = await client.get("/")
    assert resp.status == 200
    assert await resp.text() == "public:alice"


async def test_public_route_accessible_during_outage(
    aiohttp_client: AiohttpClient, storage: FlakyStorage
) -> None:
    client = await aiohttp_client(create_app(storage))
    await client.post("/login")
    key = session_cookie(client)

    storage.available = False
    resp = await client.get("/")
    assert resp.status == 200
    assert await resp.text() == "public:anonymous"
    # The degraded session must not touch the client cookie.
    assert "AIOHTTP_SESSION" not in resp.cookies
    assert session_cookie(client) == key


async def test_protected_route_stopped_during_outage(
    aiohttp_client: AiohttpClient, storage: FlakyStorage
) -> None:
    client = await aiohttp_client(create_app(storage))
    await client.post("/login")

    storage.available = False
    resp = await client.get("/protected")
    assert resp.status == 503


async def test_login_stopped_during_outage(
    aiohttp_client: AiohttpClient, storage: FlakyStorage
) -> None:
    client = await aiohttp_client(create_app(storage))
    stored = dict(storage.store)

    storage.available = False
    resp = await client.post("/login")
    assert resp.status == 503
    # No new session was persisted and no cookie was issued.
    assert storage.store == stored
    assert "AIOHTTP_SESSION" not in resp.cookies


async def test_recovery_does_not_overwrite_original_session(
    aiohttp_client: AiohttpClient, storage: FlakyStorage
) -> None:
    client = await aiohttp_client(create_app(storage))
    await client.post("/login")
    key = session_cookie(client)
    original = dict(storage.store[key])

    storage.available = False
    resp = await client.get("/")
    assert resp.status == 200

    storage.available = True
    resp = await client.get("/protected")
    assert resp.status == 200
    assert await resp.text() == "protected:alice"
    # The temporary anonymous state was never written back.
    assert storage.store[key] == original
    assert "temp_anon" not in storage.store[key]["session"]


async def test_load_results_are_distinguishable(
    aiohttp_client: AiohttpClient, storage: FlakyStorage
) -> None:
    client = await aiohttp_client(create_app(storage))

    # No session at all: a fresh one.
    resp = await client.get("/state")
    assert await resp.json() == {"new": True, "invalid": False, "unavailable": False}

    # Existing session.
    await client.post("/login")
    resp = await client.get("/state")
    assert await resp.json() == {"new": False, "invalid": False, "unavailable": False}

    # Session not found in the store: a fresh one, not an error.
    client.session.cookie_jar.update_cookies({"AIOHTTP_SESSION": "unknown-key"})
    resp = await client.get("/state")
    assert await resp.json() == {"new": True, "invalid": False, "unavailable": False}

    # Storage failure: degraded temporary session.
    storage.available = False
    resp = await client.get("/state")
    assert await resp.json() == {"new": True, "invalid": False, "unavailable": True}


async def test_invalid_credentials_are_distinguishable(
    aiohttp_client: AiohttpClient,
) -> None:
    fernet_key = fernet.Fernet.generate_key()
    secret_key = base64.urlsafe_b64decode(fernet_key)
    app = create_app(EncryptedCookieStorage(secret_key))
    client = await aiohttp_client(app)

    client.session.cookie_jar.update_cookies({"AIOHTTP_SESSION": "forged-value"})
    resp = await client.get("/state")
    assert resp.status == 200
    assert await resp.json() == {"new": True, "invalid": True, "unavailable": False}


def closed_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_redis_storage_failure_is_distinguishable_and_sanitized() -> None:
    port = closed_port()
    redis = aioredis.Redis(host="127.0.0.1", port=port, socket_connect_timeout=1)
    storage = RedisStorage(redis)
    try:
        request = make_mocked_request(
            "GET", "/", headers={"Cookie": "AIOHTTP_SESSION=somekey"}
        )
        with pytest.raises(StorageUnavailableError) as exc_info:
            await storage.load_session(request)
        # The error must not leak connection details.
        message = str(exc_info.value)
        assert "127.0.0.1" not in message
        assert str(port) not in message

        session = Session(
            None, data={"created": 1, "session": {"k": "v"}}, new=True
        )
        with pytest.raises(StorageUnavailableError) as exc_info:
            await storage.save_session(request, web.Response(), session)
        assert str(port) not in str(exc_info.value)
    finally:
        await redis.aclose()


async def test_redis_backed_app_degrades_and_recovers(
    aiohttp_client: AiohttpClient,
) -> None:
    port = closed_port()
    redis = aioredis.Redis(host="127.0.0.1", port=port, socket_connect_timeout=1)
    client = await aiohttp_client(create_app(RedisStorage(redis)))
    try:
        # Public read-only route keeps working while Redis is down.
        resp = await client.get("/")
        assert resp.status == 200
        assert await resp.text() == "public:anonymous"

        # Login state changes are stopped with a sanitized error.
        resp = await client.post("/login")
        assert resp.status == 503
        body = await resp.text()
        assert "127.0.0.1" not in body
        assert str(port) not in body
        assert "AIOHTTP_SESSION" not in resp.cookies
    finally:
        await redis.aclose()
