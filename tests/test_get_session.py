import asyncio
import time
from collections import defaultdict
from typing import Any, cast

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from aiohttp_session import (
    SESSION_KEY,
    STORAGE_KEY,
    AbstractStorage,
    Session,
    _SESSION_STATE_KEY,
    get_session,
    new_session,
    session_middleware,
)


def _set_storage(request: web.Request, storage: AbstractStorage) -> None:
    # Bypass Request.__setitem__: aiohttp 3.14+ warns for plain string
    # keys and warnings are errors in this test suite.
    cast(Any, request)._state[STORAGE_KEY] = storage


def _make_request(gate: str = "default") -> web.Request:
    return make_mocked_request("GET", "/", headers={"X-Gate": gate})


async def _settle(*tasks: asyncio.Task[Any]) -> None:
    """Await background tasks the middleware may have cancelled."""
    for task in tasks:
        try:
            await asyncio.wait_for(asyncio.shield(task), 1)
        except (asyncio.CancelledError, Exception):
            pass
    for _ in range(3):
        await asyncio.sleep(0)


class ControlledStorage(AbstractStorage):
    """Storage double that lets a test order load/new_session/save."""

    def __init__(
        self,
        *,
        fail_loads: int = 0,
        fail_news: int = 0,
    ) -> None:
        super().__init__()
        self.fail_loads = fail_loads
        self.fail_news = fail_news
        self.load_calls = 0
        self.new_calls = 0
        self.loaded: list[Session] = []
        self.saved: list[Session] = []
        self.load_gates: dict[str, asyncio.Event] = defaultdict(asyncio.Event)
        self.new_gates: dict[str, asyncio.Event] = defaultdict(asyncio.Event)
        self.load_entered: dict[str, asyncio.Event] = defaultdict(asyncio.Event)
        self.new_entered: dict[str, asyncio.Event] = defaultdict(asyncio.Event)

    def release_load(self, gate: str = "default") -> None:
        self.load_gates[gate].set()

    def release_new(self, gate: str = "default") -> None:
        self.new_gates[gate].set()

    async def load_session(self, request: web.Request) -> Session:
        gate = request.headers.get("X-Gate", "default")
        self.load_calls += 1
        self.load_entered[gate].set()
        await self.load_gates[gate].wait()
        if self.fail_loads > 0:
            self.fail_loads -= 1
            raise RuntimeError("load failed")
        session = Session(
            f"old-{gate}",
            data={"session": {"old": gate}, "created": int(time.time())},
            new=False,
        )
        self.loaded.append(session)
        return session

    async def new_session(self) -> Session:
        self.new_calls += 1
        self.new_entered["default"].set()
        await self.new_gates["default"].wait()
        if self.fail_news > 0:
            self.fail_news -= 1
            raise RuntimeError("new failed")
        return Session("new", data=None, new=True)

    async def save_session(
        self, request: web.Request, response: web.StreamResponse, session: Session
    ) -> None:
        self.saved.append(session)


async def test_concurrent_reads_share_single_load() -> None:
    storage = ControlledStorage()
    req = _make_request()
    _set_storage(req, storage)

    first_reader = asyncio.create_task(get_session(req))
    await storage.load_entered["default"].wait()
    second_reader = asyncio.create_task(get_session(req))
    await asyncio.sleep(0)

    assert storage.load_calls == 1
    storage.release_load()
    first, second = await asyncio.gather(first_reader, second_reader)
    assert first is second


async def test_new_session_wins_over_inflight_old_load() -> None:
    storage = ControlledStorage()
    req = _make_request()
    _set_storage(req, storage)

    old_reader = asyncio.create_task(get_session(req))
    await storage.load_entered["default"].wait()

    storage.new_gates["default"].set()
    session = await new_session(req)
    session["fresh"] = True
    session.changed()
    storage.release_load()

    # The pending reader joins the new identity instead of publishing the
    # result of the superseded load.
    assert session["fresh"] is True
    assert await old_reader is session
    assert req.get(SESSION_KEY) is session
    assert session.new
    assert session.identity == "new"


async def test_old_load_does_not_overwrite_new_identity() -> None:
    storage = ControlledStorage()
    req = _make_request()
    _set_storage(req, storage)

    first_reader = asyncio.create_task(get_session(req))
    await storage.load_entered["default"].wait()
    second_reader = asyncio.create_task(get_session(req))
    await asyncio.sleep(0)

    storage.release_new()
    session = await new_session(req)
    session["fresh"] = True
    storage.release_load()
    await asyncio.gather(first_reader, second_reader)

    old_sessions = storage.loaded
    assert len(old_sessions) == 1
    assert old_sessions[0].identity == "old-default"
    assert req.get(SESSION_KEY) is session


async def test_save_does_not_merge_old_and_new_state() -> None:
    storage = ControlledStorage()
    middleware = session_middleware(storage)

    async def handler(request: web.Request) -> web.Response:
        old_reader = asyncio.create_task(get_session(request))
        await storage.load_entered["default"].wait()

        new_task = asyncio.create_task(new_session(request))
        await storage.new_entered["default"].wait()
        storage.release_new()
        session = await new_task
        session["fresh"] = True
        session.changed()

        # Let the superseded load finish, then mutate the discarded
        # object.  It must never contribute to the saved state.
        storage.release_load()
        assert await old_reader is session
        storage.loaded[0]["legacy"] = "stale"
        return web.Response()

    req = _make_request()
    await middleware(req, handler)

    assert len(storage.saved) == 1
    assert storage.saved[0]._mapping == {"fresh": True}
    assert storage.saved[0].new
    assert storage.loaded[0]._mapping == {"old": "default", "legacy": "stale"}


async def test_cancelled_reader_does_not_cancel_shared_load() -> None:
    storage = ControlledStorage()
    req = _make_request()
    _set_storage(req, storage)

    cancelled_reader = asyncio.create_task(get_session(req))
    surviving_reader = asyncio.create_task(get_session(req))

    await storage.load_entered["default"].wait()
    cancelled_reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_reader

    storage.release_load()
    session = await surviving_reader
    assert session.identity == "old-default"
    assert storage.load_calls == 1


async def test_load_failure_reaches_all_waiters_and_can_be_retried() -> None:
    storage = ControlledStorage(fail_loads=1)
    req = _make_request()
    _set_storage(req, storage)

    first_reader = asyncio.create_task(get_session(req))
    second_reader = asyncio.create_task(get_session(req))
    await storage.load_entered["default"].wait()
    storage.release_load()

    results = await asyncio.gather(
        first_reader, second_reader, return_exceptions=True
    )
    assert all(isinstance(result, RuntimeError) for result in results)
    assert req.get(SESSION_KEY) is None

    # A failure is not cached: a later caller may retry the load.
    assert (await get_session(req)).identity == "old-default"


async def test_new_session_failure_does_not_resurrect_old_load() -> None:
    storage = ControlledStorage(fail_news=1)
    req = _make_request()
    _set_storage(req, storage)

    old_reader = asyncio.create_task(get_session(req))
    await storage.load_entered["default"].wait()

    new_call = asyncio.create_task(new_session(req))
    await storage.new_entered["default"].wait()
    storage.release_load()
    storage.release_new()
    old_result = await asyncio.gather(old_reader, return_exceptions=True)

    # The new identity invalidated the old load.  When new_session fails,
    # readers waiting for it see the failure rather than resurrect the
    # superseded session.
    assert isinstance(old_result[0], RuntimeError)
    with pytest.raises(RuntimeError, match="new failed"):
        await new_call
    assert req.get(SESSION_KEY) is None

    # A failed new_session is not cached either.
    assert (await new_session(req)).identity == "new"


async def test_superseded_load_failure_does_not_mask_new_session() -> None:
    storage = ControlledStorage(fail_loads=1)
    req = _make_request()
    _set_storage(req, storage)

    old_reader = asyncio.create_task(get_session(req))
    await storage.load_entered["default"].wait()

    new_task = asyncio.create_task(new_session(req))
    await storage.new_entered["default"].wait()
    storage.release_load()
    storage.release_new()

    session = await new_task
    # The old load raised, but it was superseded; the caller joins the
    # new identity rather than receiving a stale failure.
    assert await old_reader is session
    assert req.get(SESSION_KEY) is session


async def test_cancelled_new_session_waiter_does_not_cancel_identity() -> None:
    storage = ControlledStorage()
    req = _make_request()
    _set_storage(req, storage)

    cancelled_waiter = asyncio.create_task(new_session(req))
    surviving_waiter = asyncio.create_task(new_session(req))
    await storage.new_entered["default"].wait()

    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter

    storage.release_new()
    session = await surviving_waiter
    assert session.new
    assert req.get(SESSION_KEY) is session
    assert storage.new_calls == 1


async def test_unawaited_inflight_load_is_not_saved() -> None:
    storage = ControlledStorage()
    middleware = session_middleware(storage)
    pending: list[asyncio.Task[Session]] = []

    async def handler(request: web.Request) -> web.Response:
        # Fire and forget; the handler does not await the lazy load.
        pending.append(asyncio.create_task(get_session(request)))
        return web.Response()

    req = _make_request()
    await middleware(req, handler)
    assert storage.saved == []
    storage.release_load()
    await _settle(*pending)


async def test_middleware_does_not_block_on_pending_load() -> None:
    storage = ControlledStorage()
    middleware = session_middleware(storage)
    pending: list[asyncio.Task[Session]] = []

    async def handler(request: web.Request) -> web.Response:
        pending.append(asyncio.create_task(get_session(request)))
        return web.Response()

    req = _make_request()
    # Gate is never released.  The middleware must still finish promptly
    # and must not save the uncommitted session.
    response = await asyncio.wait_for(middleware(req, handler), 1)
    assert response.status == 200
    assert storage.saved == []
    storage.release_load()
    await _settle(*pending)


async def test_http_exception_after_login_saves_new_session_only() -> None:
    storage = ControlledStorage()
    middleware = session_middleware(storage)

    async def handler(request: web.Request) -> web.Response:
        old_reader = asyncio.create_task(get_session(request))
        await storage.load_entered["default"].wait()
        new_task = asyncio.create_task(new_session(request))
        await storage.new_entered["default"].wait()
        storage.release_load()
        storage.release_new()
        session = await new_task
        await old_reader
        session["user"] = "alice"
        session.changed()
        raise web.HTTPUnauthorized()

    req = _make_request()
    with pytest.raises(web.HTTPUnauthorized):
        await middleware(req, handler)

    assert len(storage.saved) == 1
    assert storage.saved[0]._mapping == {"user": "alice"}
    assert storage.saved[0].new


async def test_handler_cancellation_abandons_pending_load() -> None:
    storage = ControlledStorage()
    middleware = session_middleware(storage)
    pending: list[asyncio.Task[Session]] = []

    async def handler(request: web.Request) -> web.Response:
        pending.append(asyncio.create_task(get_session(request)))
        await asyncio.sleep(0)
        raise asyncio.CancelledError()

    req = _make_request()
    with pytest.raises(asyncio.CancelledError):
        await middleware(req, handler)

    storage.release_load()
    await _settle(*pending)
    state = cast(Any, req)._state[_SESSION_STATE_KEY]
    assert state.load_task is None
    assert storage.saved == []


async def test_reader_after_abandoned_load_can_retry() -> None:
    storage = ControlledStorage()
    middleware = session_middleware(storage)
    pending: list[asyncio.Task[Session]] = []

    async def handler(request: web.Request) -> web.Response:
        pending.append(asyncio.create_task(get_session(request)))
        await asyncio.sleep(0)
        return web.Response()

    req = _make_request()
    await middleware(req, handler)
    storage.release_load()
    await _settle(*pending)

    # The abandoned lazy load did not commit, but a fresh call may load
    # again without hanging on the cancelled shared task.  The gate is
    # already open, so the retry completes immediately.
    session = await asyncio.wait_for(get_session(req), 1)
    assert session.identity == "old-default"


async def test_two_parallel_reads_then_login_save_one_identity() -> None:
    storage = ControlledStorage()
    middleware = session_middleware(storage)

    async def handler(request: web.Request) -> web.Response:
        readers = [asyncio.create_task(get_session(request)) for _ in range(2)]
        await storage.load_entered["default"].wait()
        new_task = asyncio.create_task(new_session(request))
        await storage.new_entered["default"].wait()
        storage.release_new()
        storage.release_load()

        session = await new_task
        session["user"] = "alice"
        session.changed()
        loaded = await asyncio.gather(*readers)
        assert all(item is session for item in loaded)
        return web.Response()

    req = _make_request()
    await asyncio.wait_for(middleware(req, handler), 1)

    assert len(storage.saved) == 1
    saved = storage.saved[0]
    assert saved._mapping == {"user": "alice"}
    assert saved.new and saved.identity == "new"


async def test_concurrent_new_session_calls_coalesce() -> None:
    storage = ControlledStorage()
    req = _make_request()
    _set_storage(req, storage)

    first = asyncio.create_task(new_session(req))
    second = asyncio.create_task(new_session(req))
    await storage.new_entered["default"].wait()
    storage.release_new()

    sessions = await asyncio.gather(first, second)
    assert sessions[0] is sessions[1]
    assert storage.new_calls == 1


async def test_loads_are_independent_across_requests() -> None:
    storage = ControlledStorage()
    blocked_req = _make_request("blocked")
    fast_req = _make_request("fast")
    _set_storage(blocked_req, storage)
    _set_storage(fast_req, storage)

    blocked_reader = asyncio.create_task(get_session(blocked_req))
    await storage.load_entered["blocked"].wait()

    storage.release_load("fast")
    fast_session = await get_session(fast_req)
    assert fast_session.identity == "old-fast"

    storage.release_load("blocked")
    blocked_session = await blocked_reader
    assert blocked_session.identity == "old-blocked"


async def test_load_after_committed_new_session_reuses_it() -> None:
    storage = ControlledStorage()
    req = _make_request()
    _set_storage(req, storage)

    async def reader() -> Session:
        return await get_session(req)

    storage.release_new()
    new = await new_session(req)
    assert await reader() is new
    assert storage.load_calls == 0


async def test_get_stored_session() -> None:
    req = make_mocked_request("GET", "/")
    session = Session("identity", data=None, new=False)
    cast(Any, req)._state[SESSION_KEY] = session

    ret = await get_session(req)
    assert session is ret


async def test_session_is_not_stored() -> None:
    req = make_mocked_request("GET", "/")

    with pytest.raises(RuntimeError):
        await get_session(req)


async def test_storage_returns_not_session_on_load_session() -> None:
    req = make_mocked_request("GET", "/")

    class Storage:
        async def load_session(self, request: web.Request) -> None:
            return None

    _set_storage(req, cast(AbstractStorage, Storage()))

    with pytest.raises(RuntimeError):
        await get_session(req)


async def test_get_new_session() -> None:
    req = make_mocked_request("GET", "/")
    session = Session("identity", data=None, new=False)

    class Storage(AbstractStorage):
        async def load_session(self, request: web.Request) -> Session:  # type: ignore[empty-body]  # noqa: E501
            """Dummy"""

        async def save_session(
            self, request: web.Request, response: web.StreamResponse, session: Session
        ) -> None:
            """Dummy"""

    cast(Any, req)._state[SESSION_KEY] = session
    _set_storage(req, Storage())

    ret = await new_session(req)
    assert ret is not session


async def test_get_new_session_no_storage() -> None:
    req = make_mocked_request("GET", "/")
    session = Session("identity", data=None, new=False)
    cast(Any, req)._state[SESSION_KEY] = session

    with pytest.raises(RuntimeError):
        await new_session(req)


async def test_get_new_session_bad_return() -> None:
    req = make_mocked_request("GET", "/")

    class Storage(AbstractStorage):
        async def new_session(self) -> Session:
            return ""  # type: ignore[return-value]

        async def load_session(self, request: web.Request) -> Session:  # type: ignore[empty-body]  # noqa: E501
            """Dummy"""

        async def save_session(
            self, request: web.Request, response: web.StreamResponse, session: Session
        ) -> None:
            """Dummy"""

    _set_storage(req, Storage())

    with pytest.raises(RuntimeError):
        await new_session(req)


async def test_middleware_saves_committed_session_only() -> None:
    storage = ControlledStorage()

    async def handler(request: web.Request) -> web.Response:
        storage.release_load()
        session = await get_session(request)
        session["value"] = 1
        return web.Response()

    async def other_handler(request: web.Request) -> web.Response:
        return web.Response()

    middleware = session_middleware(storage)
    await middleware(_make_request(), handler)
    assert len(storage.saved) == 1
    assert storage.saved[0]._mapping == {"old": "default", "value": 1}

    await middleware(_make_request("fast"), other_handler)
    assert len(storage.saved) == 1

