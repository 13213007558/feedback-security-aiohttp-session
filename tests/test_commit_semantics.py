"""Commit-stage semantics for the session middleware.

The middleware must let an application distinguish three outcomes when a
session change is saved:

* the storage backend was never reached (retryable, nothing durable);
* the backend committed the change but the response cookie could not be
  written (server-side state already exists, no blind retry);
* the response was already prepared and cannot carry a cookie at all.

A fake storage plus real ``web.Response`` objects are used to inject the
failures deterministically, including one that prepares the response inside
the handler.
"""

from collections.abc import Callable
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from aiohttp_session import (
    SESSION_KEY,
    STORAGE_KEY,
    AbstractStorage,
    CommitStage,
    Session,
    SessionError,
    SessionNotSavedError,
    SessionPersistedError,
    SessionResponseLockedError,
    session_middleware,
)


class FakeStorage(AbstractStorage):
    """Storage whose save behaviour is scripted for the tests."""

    def __init__(
        self,
        *,
        backend_error: Exception | None = None,
        cookie_error: Exception | None = None,
    ) -> None:
        super().__init__()
        self.backend_error = backend_error
        self.cookie_error = cookie_error
        self.backend_calls = 0
        self.cookie_calls = 0
        self.records: dict[str, dict[str, Any]] = {}

    async def load_session(self, request: web.Request) -> Session:
        return Session(None, data=None, new=True, max_age=self.max_age)

    async def save_session(
        self,
        request: web.Request,
        response: web.StreamResponse,
        session: Session,
    ) -> None:
        # Phase 1: the "server-side" commit.
        self.backend_calls += 1
        if self.backend_error is not None:
            raise self.backend_error

        key = session.identity or "fake-key"
        self.records[key] = dict(session._mapping)

        # Phase 2: hand the identity to the client through the response.
        self.cookie_calls += 1
        if self.cookie_error is not None:
            raise self.cookie_error
        self.save_cookie(response, key, max_age=session.max_age)


def _changed_session() -> Session:
    session = Session(None, data=None, new=True)
    session["user_id"] = 42
    return session


def _request(session: Session | None = None) -> web.Request:
    request = make_mocked_request("GET", "/")
    if session is not None:
        request[SESSION_KEY] = session
    return request


async def _run_middleware(
    storage: AbstractStorage,
    handler: Callable[[web.Request], Any],
) -> Any:
    middleware = session_middleware(storage)
    request = make_mocked_request("GET", "/")
    return await middleware(request, handler)


async def test_successful_save_attaches_cookie_and_returns_response() -> None:
    storage = FakeStorage()

    async def handler(request: web.Request) -> web.Response:
        request[SESSION_KEY] = _changed_session()
        return web.Response(text="ok")

    response = await _run_middleware(storage, handler)

    assert response.status == 200
    assert storage.backend_calls == 1
    assert storage.cookie_calls == 1
    assert "AIOHTTP_SESSION" in response.cookies
    assert storage.records == {"fake-key": {"user_id": 42}}


async def test_backend_failure_before_save_is_not_saved_and_retryable() -> None:
    storage = FakeStorage(backend_error=ConnectionError("redis down"))

    async def handler(request: web.Request) -> web.Response:
        request[SESSION_KEY] = _changed_session()
        return web.Response(text="ok")

    with pytest.raises(SessionNotSavedError) as exc_info:
        await _run_middleware(storage, handler)

    error = exc_info.value
    assert isinstance(error, SessionError)
    assert error.stage is CommitStage.NOT_SAVED
    assert error.retryable is True
    assert error.persisted is False
    assert error.response_mutable is True
    # The original backend failure is preserved, not swallowed.
    assert isinstance(error.__cause__, ConnectionError)
    # Nothing reached the response phase.
    assert storage.backend_calls == 1
    assert storage.cookie_calls == 0
    assert storage.records == {}


async def test_storage_typed_not_saved_error_passes_through_unchanged() -> None:
    original = SessionNotSavedError("explicitly not saved")
    storage = FakeStorage(backend_error=original)

    async def handler(request: web.Request) -> web.Response:
        request[SESSION_KEY] = _changed_session()
        return web.Response(text="ok")

    with pytest.raises(SessionNotSavedError) as exc_info:
        await _run_middleware(storage, handler)
    assert exc_info.value is original


async def test_failure_after_backend_commit_is_persisted_and_not_retryable() -> None:
    # The storage knows its backend write succeeded, so it reports the
    # cookie-phase failure with the typed "persisted" error rather than a
    # generic exception (which would mean "no persistence guarantee").
    storage = FakeStorage(
        cookie_error=SessionPersistedError("record stored, cookie write lost")
    )

    async def handler(request: web.Request) -> web.Response:
        request[SESSION_KEY] = _changed_session()
        return web.Response(text="ok")

    with pytest.raises(SessionPersistedError) as exc_info:
        await _run_middleware(storage, handler)

    error = exc_info.value
    assert error.stage is CommitStage.PERSISTED
    assert error.retryable is False
    assert error.persisted is True
    assert error.response_mutable is True
    assert str(error) == "record stored, cookie write lost"
    # The backend commit happened exactly once; retrying the request would
    # not roll this record back.
    assert storage.backend_calls == 1
    assert storage.cookie_calls == 1
    assert storage.records == {"fake-key": {"user_id": 42}}


async def test_generic_cookie_failure_without_typed_contract_is_not_saved() -> None:
    # A storage that cannot prove its backend write succeeded must let the
    # generic exception bubble up; the middleware then classifies it as
    # retryable rather than assuming the identity was persisted.
    storage = FakeStorage(cookie_error=RuntimeError("lost connection mid-save"))

    async def handler(request: web.Request) -> web.Response:
        request[SESSION_KEY] = _changed_session()
        return web.Response(text="ok")

    with pytest.raises(SessionNotSavedError) as exc_info:
        await _run_middleware(storage, handler)
    assert isinstance(exc_info.value.__cause__, RuntimeError)


async def test_prepared_response_fails_before_storage_is_called() -> None:
    storage = FakeStorage()

    async def handler(request: web.Request) -> web.Response:
        request[SESSION_KEY] = _changed_session()
        response = web.Response(text="ok")
        # Simulate a streaming/earlier prepare: headers are already sent.
        await response.prepare(request)
        return response

    with pytest.raises(SessionResponseLockedError) as exc_info:
        await _run_middleware(storage, handler)

    error = exc_info.value
    assert error.stage is CommitStage.COMMITTED
    assert error.retryable is False
    assert error.persisted is True
    assert error.response_mutable is False
    # The middleware must not even attempt the save on a locked response.
    assert storage.backend_calls == 0
    assert storage.cookie_calls == 0
    assert storage.records == {}


async def test_unchanged_session_never_invokes_storage() -> None:
    storage = FakeStorage()

    async def handler(request: web.Request) -> web.Response:
        request[SESSION_KEY] = Session(None, data=None, new=True)
        return web.Response(text="ok")

    response = await _run_middleware(storage, handler)

    assert response.status == 200
    assert storage.backend_calls == 0
    assert storage.cookie_calls == 0


async def test_no_session_in_request_skips_save_entirely() -> None:
    storage = FakeStorage()

    async def handler(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    response = await _run_middleware(storage, handler)

    assert response.status == 200
    assert storage.backend_calls == 0


async def test_each_stage_is_distinct_exception_type() -> None:
    not_saved = SessionNotSavedError()
    persisted = SessionPersistedError()
    locked = SessionResponseLockedError()

    assert not_saved.stage != persisted.stage != locked.stage
    assert isinstance(not_saved, SessionError)
    assert isinstance(persisted, SessionError)
    assert isinstance(locked, SessionError)
    # The old contract raised RuntimeError; keep backwards compatibility.
    assert isinstance(not_saved, RuntimeError)
    assert isinstance(persisted, RuntimeError)
    assert isinstance(locked, RuntimeError)


async def test_request_with_explicit_storage_key_still_works() -> None:
    storage = FakeStorage()
    request = _request(_changed_session())
    request[STORAGE_KEY] = storage
    middleware = session_middleware(storage)

    async def handler(inner_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    response = await middleware(request, handler)
    assert storage.backend_calls == 1
    assert "AIOHTTP_SESSION" in response.cookies
