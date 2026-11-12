"""Integration tests: challenge completion survives a failed session save."""

from __future__ import annotations

import asyncio
import json
import time
import warnings
from http.cookies import SimpleCookie
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from aiohttp_session import (
    Session,
    SimpleCookieStorage,
    get_session,
    session_middleware,
)
from aiohttp_session.challenge import (
    ChallengeOutcome,
    ChallengeState,
    MemoryChallengeStore,
    RetryPolicy,
    claim_session_challenge,
    complete_session_challenge,
    run_session_challenge,
)


class FlakyCookieStorage(SimpleCookieStorage):
    """Save session data, then fail as a flaky backend would."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_save = False

    async def save_session(
        self, request: web.Request, response: web.StreamResponse, session: Session
    ) -> None:
        await super().save_session(request, response, session)
        if self.fail_save:
            raise OSError("session backend unavailable")


def _cookie_header(data: dict[str, Any]) -> str:
    session_data = {"session": data, "created": int(time.time())}
    cookie: SimpleCookie[str] = SimpleCookie()
    cookie["AIOHTTP_SESSION"] = json.dumps(session_data)
    return cookie.output(header="", sep="").strip()


async def _dispatch_with_claimant(
    storage: FlakyCookieStorage,
    handler: Any,
    cookie_data: dict[str, Any],
    claimant: str,
) -> web.Response:
    headers = {
        "Cookie": _cookie_header(cookie_data),
        "X-Claimant": claimant,
    }
    return await _run_middleware(storage, handler, headers)


async def _dispatch(
    storage: FlakyCookieStorage,
    handler: Any,
    cookie_data: dict[str, Any],
) -> web.Response:
    return await _run_middleware(
        storage, handler, {"Cookie": _cookie_header(cookie_data)}
    )


async def _run_middleware(
    storage: FlakyCookieStorage,
    handler: Any,
    headers: dict[str, str],
) -> web.Response:
    request = make_mocked_request("GET", "/", headers=headers)
    middleware = session_middleware(storage)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", web.NotAppKeyWarning)
        response = await middleware(request, handler)
    assert isinstance(response, web.Response)
    return response


async def test_middleware_save_failure_does_not_reopen_completed_challenge() -> None:
    store = MemoryChallengeStore()
    assert await store.issue_challenge("challenge-1")
    storage = FlakyCookieStorage()
    persisted = {"confirm": "challenge-1", "user": "u1"}

    async def first_handler(request: web.Request) -> web.Response:
        session = await get_session(request)
        result = await run_session_challenge(
            session,
            store,
            "confirm",
            claimant="request-a",
            operation=_succeed,
        )
        assert result.authorized
        assert result.result == "done"
        return web.Response(body=b"approved")

    storage.fail_save = True
    with pytest.raises(OSError):
        await _dispatch(storage, first_handler, persisted)

    snapshot = await store.snapshot("challenge-1")
    assert snapshot is not None
    assert snapshot.state is ChallengeState.COMPLETED

    seen: list[bool] = []

    async def second_handler(request: web.Request) -> web.Response:
        session = await get_session(request)
        lease = await claim_session_challenge(
            session, store, "confirm", claimant="request-b"
        )
        seen.append(lease is not None)
        return web.Response(body=b"approved" if lease else b"denied")

    storage.fail_save = False
    response = await _dispatch(storage, second_handler, persisted)
    assert response.body == b"denied"
    assert seen == [False]
    snapshot = await store.snapshot("challenge-1")
    assert snapshot is not None
    assert snapshot.state is ChallengeState.COMPLETED
    assert snapshot.claim_count == 1


async def test_middleware_save_failure_after_operation_failure_stays_terminal() -> None:
    store = MemoryChallengeStore()
    assert await store.issue_challenge("challenge-1")
    storage = FlakyCookieStorage()
    persisted = {"confirm": "challenge-1", "user": "u1"}

    async def failing_handler(request: web.Request) -> web.Response:
        session = await get_session(request)
        with pytest.raises(RuntimeError):
            await run_session_challenge(
                session,
                store,
                "confirm",
                claimant="request-a",
                operation=_fail,
            )
        return web.Response(body=b"failed")

    storage.fail_save = True
    with pytest.raises(OSError):
        await _dispatch(storage, failing_handler, persisted)

    snapshot = await store.snapshot("challenge-1")
    assert snapshot is not None
    assert snapshot.state is ChallengeState.FAILED

    async def second_handler(request: web.Request) -> web.Response:
        session = await get_session(request)
        lease = await claim_session_challenge(
            session, store, "confirm", claimant="request-a"
        )
        return web.Response(body=b"approved" if lease else b"denied")

    storage.fail_save = False
    response = await _dispatch(storage, second_handler, persisted)
    assert response.body == b"denied"


async def test_middleware_save_failure_keeps_retry_bound_to_claimant() -> None:
    store = MemoryChallengeStore()
    assert await store.issue_challenge("challenge-1")
    storage = FlakyCookieStorage()
    persisted = {"confirm": "challenge-1", "user": "u1"}

    async def failing_handler(request: web.Request) -> web.Response:
        session = await get_session(request)
        with pytest.raises(RuntimeError):
            await run_session_challenge(
                session,
                store,
                "confirm",
                claimant="request-a",
                operation=_fail,
                retry_policy=RetryPolicy.SAME_CLAIMANT,
            )
        # An application may change its own session keys on this same
        # request; the middleware then attempts to save them.
        session["last_attempt"] = "failed"
        return web.Response(body=b"failed")

    storage.fail_save = True
    with pytest.raises(OSError):
        await _dispatch(storage, failing_handler, persisted)

    snapshot = await store.snapshot("challenge-1")
    assert snapshot is not None
    assert snapshot.state is ChallengeState.RETRYABLE

    async def same_claimant_handler(request: web.Request) -> web.Response:
        session = await get_session(request)
        lease = await claim_session_challenge(
            session, store, "confirm", claimant="request-a"
        )
        return web.Response(body=b"approved" if lease else b"denied")

    async def other_claimant_handler(request: web.Request) -> web.Response:
        session = await get_session(request)
        lease = await claim_session_challenge(
            session, store, "confirm", claimant="request-b"
        )
        return web.Response(body=b"approved" if lease else b"denied")

    storage.fail_save = False
    assert (
        await _dispatch(storage, other_claimant_handler, persisted)
    ).body == b"denied"
    response = await _dispatch(storage, same_claimant_handler, persisted)
    assert response.body == b"approved"

    snapshot = await store.snapshot("challenge-1")
    assert snapshot is not None
    assert snapshot.state is ChallengeState.CLAIMED
    assert snapshot.claimant == "request-a"
    assert snapshot.claim_count == 2


async def test_two_concurrent_requests_only_one_gets_approval() -> None:
    store = MemoryChallengeStore()
    assert await store.issue_challenge("challenge-1")
    storage = FlakyCookieStorage()
    persisted = {"confirm": "challenge-1", "user": "u1"}
    barrier = asyncio.Barrier(2)
    approved: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        claimant = request.headers["X-Claimant"]
        await barrier.wait()
        session = await get_session(request)
        lease = await claim_session_challenge(
            session, store, "confirm", claimant=claimant
        )
        if lease is None:
            return web.Response(body=b"denied")
        approved.append(claimant)
        await asyncio.sleep(0)
        await complete_session_challenge(
            lease, session, "confirm", ChallengeOutcome.SUCCEEDED
        )
        return web.Response(body=b"approved")

    first = _dispatch_with_claimant(storage, handler, persisted, "request-a")
    second = _dispatch_with_claimant(storage, handler, persisted, "request-b")
    responses = await asyncio.gather(first, second)

    assert len(approved) == 1
    assert sorted(response.body for response in responses) == [
        b"approved",
        b"denied",
    ]
    snapshot = await store.snapshot("challenge-1")
    assert snapshot is not None
    assert snapshot.state is ChallengeState.COMPLETED
    assert snapshot.claim_count == 1


async def test_ordinary_session_keys_keep_existing_middleware_behavior() -> None:
    storage = SimpleCookieStorage()
    persisted = {"ordinary": 1, "user": "u1"}

    async def handler(request: web.Request) -> web.Response:
        session = await get_session(request)
        session["extra"] = 2
        return web.Response(body=b"OK")

    response = await _dispatch(storage, handler, persisted)  # type: ignore[arg-type]
    assert response.body == b"OK"
    assert "AIOHTTP_SESSION" in response.cookies
    stored = json.loads(response.cookies["AIOHTTP_SESSION"].value)
    assert stored["session"] == {"ordinary": 1, "user": "u1", "extra": 2}


async def test_session_save_failure_after_completion_returns_state() -> None:
    store = MemoryChallengeStore()
    assert await store.issue_challenge("challenge-1")
    session = Session("id", data={"session": {"confirm": "challenge-1"}}, new=False)
    lease = await claim_session_challenge(
        session, store, "confirm", claimant="request-a"
    )
    assert lease is not None
    result = await complete_session_challenge(
        lease, session, "confirm", ChallengeOutcome.SUCCEEDED
    )
    assert result.completed
    assert result.state is ChallengeState.COMPLETED


async def _succeed() -> str:
    return "done"


async def _fail() -> None:
    raise RuntimeError("operation failed")
