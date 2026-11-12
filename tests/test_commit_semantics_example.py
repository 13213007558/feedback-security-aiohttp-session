"""Application-level handling of the typed session commit errors.

These tests drive the middleware/handlers from
``examples/commit_semantics_login.py`` directly (no network sockets) to prove
that:

* each failure stage produces a distinct HTTP response instead of one generic
  "save failed" 500;
* the login side effects never run before the storage commit;
* a retry after a not-saved failure runs the effects exactly once;
* a persisted failure runs the effects once and suppresses any duplicate.
"""

import importlib.util
import json
import pathlib
import sys
from collections.abc import Awaitable, Callable
from typing import cast

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from aiohttp_session import (
    SESSION_KEY,
    Session,
    SessionNotSavedError,
    SessionPersistedError,
    SimpleCookieStorage,
    session_middleware,
)

_EXAMPLE_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "examples"
    / "commit_semantics_login.py"
)
_spec = importlib.util.spec_from_file_location("commit_semantics_login", _EXAMPLE_PATH)
assert _spec is not None and _spec.loader is not None
example = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = example
_spec.loader.exec_module(example)


class _ScriptedStorage(SimpleCookieStorage):
    """Storage that fails at a chosen stage and records save attempts."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        super().__init__()
        self.fail_with = fail_with
        self.save_calls = 0

    async def save_session(
        self, request: web.Request, response: web.StreamResponse, session: Session
    ) -> None:
        self.save_calls += 1
        if self.fail_with is not None:
            if isinstance(self.fail_with, SessionPersistedError):
                self.fail_with.identity = session.identity
            raise self.fail_with
        await super().save_session(request, response, session)


def _post_credentials(request: web.Request) -> None:
    async def _post() -> dict[str, str]:
        return {"name": "alice", "password": "wonderland"}

    request.post = _post  # type: ignore[assignment,method-assign]


def _build_chain(
    storage: _ScriptedStorage,
    terminal: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
    """Mirror make_app()'s middleware order: error -> effects -> session."""
    session_layer = session_middleware(storage)
    effects_layer = cast(
        Callable[[web.Request, Callable[[web.Request], Awaitable[web.StreamResponse]]],
                 Awaitable[web.StreamResponse]],
        example.post_commit_side_effects,
    )
    error_layer = cast(
        Callable[[web.Request, Callable[[web.Request], Awaitable[web.StreamResponse]]],
                 Awaitable[web.StreamResponse]],
        example.session_error_middleware,
    )

    async def chain(request: web.Request) -> web.StreamResponse:
        async def with_session(req: web.Request) -> web.StreamResponse:
            return await session_layer(req, terminal)

        async def with_effects(req: web.Request) -> web.StreamResponse:
            return await effects_layer(req, with_session)

        return await error_layer(request, with_effects)

    return chain


async def _invoke_login(storage: _ScriptedStorage) -> web.StreamResponse:
    app = web.Application()
    app["locked_failures"] = 0
    request = make_mocked_request("POST", "/login", app=app)
    _post_credentials(request)
    login_handler = cast(
        Callable[[web.Request], Awaitable[web.StreamResponse]], example.login
    )
    return await _build_chain(storage, login_handler)(request)


def _body(response: web.StreamResponse) -> dict[str, str]:
    text = cast(web.Response, response).text
    assert text is not None
    return cast(dict[str, str], json.loads(text))


def _reset_ledgers() -> None:
    example.audit_log.clear()
    example.emails_sent.clear()
    example.processed_identities.clear()


async def test_not_saved_failure_is_503_and_runs_no_side_effects() -> None:
    _reset_ledgers()
    storage = _ScriptedStorage(fail_with=ConnectionError("redis down"))

    response = await _invoke_login(storage)

    assert response.status == 503
    body = _body(response)
    assert body["error"] == "session_not_saved"
    assert body["stage"] == "not_saved"
    assert body["retryable"] == "True"
    assert body["persisted"] == "False"
    assert storage.save_calls == 1
    # The commit never happened, so no one-shot effect may have fired.
    assert example.audit_log == []
    assert example.emails_sent == []


async def test_retry_after_not_saved_performs_effects_exactly_once() -> None:
    _reset_ledgers()
    storage = _ScriptedStorage(fail_with=SessionNotSavedError("backend unreachable"))

    first = await _invoke_login(storage)
    assert first.status == 503
    assert example.emails_sent == []
    assert storage.save_calls == 1

    # Same logical login retried with a healthy backend. The identity is
    # regenerated per request (fresh session), but only the successful commit
    # produces an effect, and it happens exactly once.
    storage.fail_with = None
    try:
        await _invoke_login(storage)
    except web.HTTPFound as redirect:
        assert redirect.status == 302

    assert storage.save_calls == 2
    assert len(example.audit_log) == 1
    assert len(example.emails_sent) == 1


async def test_persisted_failure_is_409_and_effects_run_once_with_identity() -> None:
    _reset_ledgers()
    storage = _ScriptedStorage(
        fail_with=SessionPersistedError("cookie header rejected")
    )

    response = await _invoke_login(storage)

    assert response.status == 409
    body = _body(response)
    assert body["error"] == "session_persisted_cookie_lost"
    assert body["stage"] == "persisted"
    assert body["retryable"] == "False"
    assert storage.save_calls == 1
    # The backend commit succeeded, so the one-shot effects do run...
    assert len(example.audit_log) == 1
    assert len(example.emails_sent) == 1

    # ...and a redriven attempt for the same durable identity is suppressed
    # instead of sending a second welcome e-mail.
    assert "cookie header rejected" in body["detail"]
    stored_identity = next(iter(example.processed_identities))
    duplicate = await example.emit_login_side_effects(stored_identity, "alice")
    assert duplicate is None
    assert len(example.emails_sent) == 1
    assert example.audit_log[-1] == f"duplicate suppressed for {stored_identity}"


async def test_locked_response_failure_is_500_without_storage_call() -> None:
    _reset_ledgers()
    storage = _ScriptedStorage()
    app = web.Application()
    app["locked_failures"] = 0

    async def handler(request: web.Request) -> web.Response:
        session = Session(None, data=None, new=True)
        session["user"] = "alice"
        request[SESSION_KEY] = session
        response = web.Response(text="streaming")
        await response.prepare(request)
        return response

    request = make_mocked_request("GET", "/", app=app)
    response = await _build_chain(storage, handler)(request)

    assert response.status == 500
    body = _body(response)
    assert body["stage"] == "committed"
    assert body["response_mutable"] == "False"
    assert storage.save_calls == 0
    assert app["locked_failures"] == 1
    assert example.emails_sent == []


async def test_successful_login_runs_effects_and_redirects() -> None:
    _reset_ledgers()
    storage = _ScriptedStorage()

    try:
        await _invoke_login(storage)
    except web.HTTPFound as redirect:
        assert redirect.status == 302
        assert redirect.headers["Location"] == "/profile"
    else:  # pragma: no cover - the login view always redirects
        raise AssertionError("login should redirect")

    assert storage.save_calls == 1
    assert len(example.audit_log) == 1
    assert len(example.emails_sent) == 1
