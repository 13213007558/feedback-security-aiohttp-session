"""Tests for the optional one-time challenge claim/completion contract."""

from __future__ import annotations

import asyncio

import pytest

from aiohttp_session import Session, run_session_challenge
from aiohttp_session.challenge import ChallengeExecution
from aiohttp_session.challenge import (
    ChallengeOutcome,
    ChallengeState,
    ClaimDeniedReason,
    CompletionDeniedReason,
    CompletionResult,
    MemoryChallengeStore,
    RetryPolicy,
    claim_session_challenge,
    complete_session_challenge,
    issue_session_challenge,
)


async def test_issue_keeps_ordinary_session_keys() -> None:
    session = Session(None, data=None, new=True)
    session["ordinary"] = {"value": 1}
    store = MemoryChallengeStore()

    challenge_id = await issue_session_challenge(
        session, store, "confirm_challenge", challenge_id="challenge-1"
    )
    assert challenge_id == "challenge-1"
    assert session["confirm_challenge"] == "challenge-1"
    assert session["ordinary"] == {"value": 1}

    assert (
        await issue_session_challenge(
            session, store, "confirm_challenge", challenge_id="challenge-2"
        )
        is None
    )
    assert session["confirm_challenge"] == "challenge-1"


async def test_two_concurrent_claimants_only_one_operation_is_authorized() -> None:
    store = MemoryChallengeStore()
    assert await store.issue_challenge("challenge-1")
    session = {"confirm_challenge": "challenge-1"}
    approved: list[str] = []
    ready = asyncio.Event()
    started = 0

    async def request(claimant: str) -> None:
        nonlocal started
        started += 1
        if started == 2:
            ready.set()
        await ready.wait()
        lease = await claim_session_challenge(
            session, store, "confirm_challenge", claimant=claimant
        )
        if lease is None:
            return
        approved.append(claimant)
        await asyncio.sleep(0)
        await lease.execute(_succeed)

    await asyncio.gather(request("request-a"), request("request-b"))

    assert len(approved) == 1
    snapshot = await store.snapshot("challenge-1")
    assert snapshot is not None
    assert snapshot.state is ChallengeState.COMPLETED
    assert snapshot.claim_count == 1


async def test_losing_claimant_cannot_complete_with_winner_token() -> None:
    store = MemoryChallengeStore()
    assert await store.issue_challenge("challenge-1")
    winner = await store.claim_challenge("challenge-1", claimant="a")
    loser = await store.claim_challenge("challenge-1", claimant="b")
    assert winner.granted
    assert not loser.granted
    assert loser.reason is ClaimDeniedReason.BUSY

    denied = await store.complete_challenge(
        "challenge-1", winner.claim_token or "", outcome=ChallengeOutcome.SUCCEEDED
    )
    assert denied.completed
    assert denied.state is ChallengeState.COMPLETED

    stale = await store.complete_challenge(
        "challenge-1", "not-the-token", outcome=ChallengeOutcome.SUCCEEDED
    )
    assert not stale.completed
    assert stale.reason is CompletionDeniedReason.NOT_CLAIMED
    assert await store.snapshot("challenge-1") == await store.snapshot("challenge-1")


async def test_wrong_claim_token_does_not_change_state() -> None:
    store = MemoryChallengeStore()
    assert await store.issue_challenge("challenge-1")
    claim = await store.claim_challenge("challenge-1", claimant="a")
    assert claim.granted
    result = await store.complete_challenge(
        "challenge-1",
        "wrong-token",
        outcome=ChallengeOutcome.SUCCEEDED,
    )
    assert not result.completed
    assert result.reason is CompletionDeniedReason.TOKEN_MISMATCH
    snapshot = await store.snapshot("challenge-1")
    assert snapshot is not None
    assert snapshot.state is ChallengeState.CLAIMED


async def test_execution_failure_is_terminal_under_default_policy() -> None:
    store = MemoryChallengeStore()
    assert await store.issue_challenge("challenge-1")
    session = {"confirm_challenge": "challenge-1"}
    lease = await claim_session_challenge(
        session,
        store,
        "confirm_challenge",
        claimant="request-a",
        retry_policy=RetryPolicy.NEVER,
    )
    assert lease is not None

    with pytest.raises(RuntimeError):

        async def fail() -> None:
            raise RuntimeError("operation failed")

        await lease.execute(fail)

    snapshot = await store.snapshot("challenge-1")
    assert snapshot is not None
    assert snapshot.state is ChallengeState.FAILED

    retry = await claim_session_challenge(
        {"confirm_challenge": "challenge-1"},
        store,
        "confirm_challenge",
        claimant="request-a",
        retry_policy=RetryPolicy.SAME_CLAIMANT,
    )
    assert retry is None

    other = await claim_session_challenge(
        {"confirm_challenge": "challenge-1"},
        store,
        "confirm_challenge",
        claimant="request-b",
    )
    assert other is None


async def test_failed_execution_retries_only_recorded_claimant() -> None:
    store = MemoryChallengeStore()
    assert await store.issue_challenge("challenge-1")
    first = await store.claim_challenge(
        "challenge-1",
        claimant="request-a",
        retry_policy=RetryPolicy.SAME_CLAIMANT,
    )
    assert first.granted
    failed = await store.complete_challenge(
        "challenge-1", first.claim_token or "", outcome=ChallengeOutcome.FAILED
    )
    assert failed.completed
    assert failed.state is ChallengeState.RETRYABLE

    other = await store.claim_challenge("challenge-1", claimant="request-b")
    assert not other.granted
    assert other.reason is ClaimDeniedReason.USED

    retry = await store.claim_challenge(
        "challenge-1",
        claimant="request-a",
        retry_policy=RetryPolicy.NEVER,
    )
    assert retry.granted
    assert await store.snapshot("challenge-1") is not None

    # The retry used the policy recorded by the first claim; a failed
    # retry is still retryable for this claimant and no one else.
    second_fail = await store.complete_challenge(
        "challenge-1", retry.claim_token or "", outcome=ChallengeOutcome.FAILED
    )
    assert second_fail.completed
    assert second_fail.state is ChallengeState.RETRYABLE
    denied = await store.claim_challenge("challenge-1", claimant="request-b")
    assert not denied.granted

    final = await store.claim_challenge("challenge-1", claimant="request-a")
    assert final.granted
    success = await store.complete_challenge(
        "challenge-1", final.claim_token or "", outcome=ChallengeOutcome.SUCCEEDED
    )
    assert success.completed
    assert success.state is ChallengeState.COMPLETED
    denied = await store.claim_challenge("challenge-1", claimant="request-a")
    assert not denied.granted


async def test_later_request_cannot_loosen_first_claim_policy() -> None:
    store = MemoryChallengeStore()
    assert await store.issue_challenge("challenge-1")
    first = await store.claim_challenge(
        "challenge-1",
        claimant="request-a",
        retry_policy=RetryPolicy.NEVER,
    )
    assert first.granted
    assert (
        await store.complete_challenge(
            "challenge-1",
            first.claim_token or "",
            outcome=ChallengeOutcome.FAILED,
        )
    ).state is ChallengeState.FAILED

    attempted = await store.claim_challenge(
        "challenge-1",
        claimant="request-a",
        retry_policy=RetryPolicy.SAME_CLAIMANT,
    )
    assert not attempted.granted
    assert attempted.reason is ClaimDeniedReason.USED


async def test_execution_result_reports_store_refused_completion() -> None:
    class RefusingStore(MemoryChallengeStore):
        async def complete_challenge(
            self,
            challenge_id: str,
            claim_token: str,
            *,
            outcome: ChallengeOutcome,
        ) -> CompletionResult:
            return CompletionResult(completed=False, challenge_id=challenge_id)

    store = RefusingStore()
    assert await store.issue_challenge("challenge-1")
    session = {"confirm_challenge": "challenge-1"}

    async def succeed() -> str:
        return "ok"

    execution = await run_session_challenge(
        session,
        store,
        "confirm_challenge",
        claimant="request-a",
        operation=succeed,
    )
    assert isinstance(execution, ChallengeExecution)
    assert execution.authorized
    assert execution.state is None
    assert "confirm_challenge" in session


async def test_session_save_failure_does_not_make_challenge_available() -> None:
    store = MemoryChallengeStore()
    assert await store.issue_challenge("challenge-1")
    persisted = {"confirm_challenge": "challenge-1", "user": "u1"}
    session = Session("identity", data={"session": dict(persisted)}, new=False)
    lease = await claim_session_challenge(
        session, store, "confirm_challenge", claimant="request-a"
    )
    assert lease is not None

    completed = await complete_session_challenge(
        lease,
        session,
        "confirm_challenge",
        ChallengeOutcome.SUCCEEDED,
    )
    assert completed.completed
    assert completed.state is ChallengeState.COMPLETED
    assert "confirm_challenge" not in session
    assert session["user"] == "u1"

    async def failing_save() -> None:
        raise OSError("session storage is down")

    with pytest.raises(OSError):
        await failing_save()

    # The failed save leaves the previously persisted session (with the
    # old challenge key) readable.  It still cannot be consumed again.
    old_session = Session("identity", data={"session": dict(persisted)}, new=False)
    again = await claim_session_challenge(
        old_session, store, "confirm_challenge", claimant="request-b"
    )
    assert again is None
    snapshot = await store.snapshot("challenge-1")
    assert snapshot is not None
    assert snapshot.state is ChallengeState.COMPLETED

    # Other ordinary session keys remain readable through this path.
    assert old_session["user"] == "u1"


async def test_save_failure_after_failed_execution_keeps_terminal_state() -> None:
    store = MemoryChallengeStore()
    assert await store.issue_challenge("challenge-1")
    persisted = {"confirm_challenge": "challenge-1", "user": "u1"}
    session = Session("identity", data={"session": dict(persisted)}, new=False)

    async def fail() -> None:
        raise RuntimeError("operation failed")

    with pytest.raises(RuntimeError):
        await run_session_challenge(
            session,
            store,
            "confirm_challenge",
            claimant="request-a",
            operation=fail,
        )

    snapshot = await store.snapshot("challenge-1")
    assert snapshot is not None
    assert snapshot.state is ChallengeState.FAILED
    assert "confirm_challenge" not in session

    async def failing_save() -> None:
        raise OSError("session storage is down")

    with pytest.raises(OSError):
        await failing_save()

    old_session = Session("identity", data={"session": dict(persisted)}, new=False)
    assert (
        await claim_session_challenge(
            old_session, store, "confirm_challenge", claimant="request-a"
        )
        is None
    )
    assert (
        await claim_session_challenge(
            old_session, store, "confirm_challenge", claimant="request-b"
        )
        is None
    )
    assert old_session["user"] == "u1"


async def test_retryable_challenge_stays_bound_after_session_save_failure() -> None:
    store = MemoryChallengeStore()
    assert await store.issue_challenge("challenge-1")
    persisted = {"confirm_challenge": "challenge-1", "user": "u1"}
    session = Session("identity", data={"session": dict(persisted)}, new=False)

    async def fail() -> None:
        raise RuntimeError("operation failed")

    with pytest.raises(RuntimeError):
        await run_session_challenge(
            session,
            store,
            "confirm_challenge",
            claimant="request-a",
            operation=fail,
            retry_policy=RetryPolicy.SAME_CLAIMANT,
        )

    snapshot = await store.snapshot("challenge-1")
    assert snapshot is not None
    assert snapshot.state is ChallengeState.RETRYABLE
    assert session["confirm_challenge"] == "challenge-1"

    async def failing_save() -> None:
        raise OSError("session storage is down")

    with pytest.raises(OSError):
        await failing_save()

    old_session = Session("identity", data={"session": dict(persisted)}, new=False)
    retry = await claim_session_challenge(
        old_session,
        store,
        "confirm_challenge",
        claimant="request-a",
    )
    assert retry is not None

    assert (
        await claim_session_challenge(
            old_session, store, "confirm_challenge", claimant="request-b"
        )
        is None
    )


async def test_expired_challenge_is_denied_and_not_reissued() -> None:
    now = [100.0]
    store = MemoryChallengeStore(clock=lambda: now[0])
    assert await store.issue_challenge("challenge-1", ttl=5)
    claim = await store.claim_challenge("challenge-1", claimant="request-a")
    assert claim.granted

    now[0] = 106.0
    completed = await store.complete_challenge(
        "challenge-1", claim.claim_token or "", outcome=ChallengeOutcome.SUCCEEDED
    )
    assert not completed.completed
    assert completed.reason is CompletionDeniedReason.EXPIRED

    again = await store.claim_challenge("challenge-1", claimant="request-a")
    assert not again.granted
    assert again.reason is ClaimDeniedReason.EXPIRED

    assert not await store.issue_challenge("challenge-1")
    assert await store.snapshot("challenge-1") is not None


async def test_execution_distinguishes_denial_from_none_result() -> None:
    store = MemoryChallengeStore()
    assert await store.issue_challenge("challenge-1")
    session = {"confirm_challenge": "challenge-1"}
    calls: list[str] = []

    async def returns_none() -> None:
        calls.append("executed")

    execution = await run_session_challenge(
        session,
        store,
        "confirm_challenge",
        claimant="request-a",
        operation=returns_none,
    )
    assert execution.authorized
    assert execution.state is ChallengeState.COMPLETED
    assert execution.result is None
    assert calls == ["executed"]

    denied = await run_session_challenge(
        session,
        store,
        "confirm_challenge",
        claimant="request-b",
        operation=returns_none,
    )
    assert not denied.authorized
    assert denied.state is None
    assert calls == ["executed"]


async def test_completion_does_not_remove_a_replaced_session_key() -> None:
    store = MemoryChallengeStore()
    assert await store.issue_challenge("challenge-1")
    session = {"confirm_challenge": "challenge-1"}
    lease = await claim_session_challenge(
        session, store, "confirm_challenge", claimant="request-a"
    )
    assert lease is not None
    session["confirm_challenge"] = "challenge-2"
    result = await complete_session_challenge(
        lease, session, "confirm_challenge", ChallengeOutcome.SUCCEEDED
    )
    assert result.state is ChallengeState.COMPLETED
    assert session["confirm_challenge"] == "challenge-2"


async def test_discard_requires_new_identifier_for_new_operation() -> None:
    store = MemoryChallengeStore()
    assert await store.issue_challenge("challenge-1")
    assert await store.discard_challenge("challenge-1")
    assert not await store.issue_challenge("challenge-1")
    assert await store.issue_challenge("challenge-2")
    assert await store.discard_challenge("challenge-1") is False


async def _succeed() -> str:
    return "ok"


def test_public_api_is_importable_from_package() -> None:
    import aiohttp_session

    assert aiohttp_session.MemoryChallengeStore is MemoryChallengeStore
    assert aiohttp_session.RetryPolicy is RetryPolicy
    assert aiohttp_session.ChallengeState is ChallengeState
