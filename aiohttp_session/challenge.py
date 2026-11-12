"""Atomic claim/completion contract for one-time session challenges.

Applications sometimes keep a one-time confirmation challenge in the
session.  A plain session mapping is not enough for that use case: two
requests may load the same session data, both observe an unused
challenge, and both perform the sensitive operation.  The mapping is
also read/write data, so a failed session save cannot reliably remember
that a challenge was already spent.

This module provides an optional :class:`AbstractChallengeStore`
contract and an in-memory implementation for single-process
deployments.  The session continues to hold the challenge identifier as
an ordinary session key; only the authoritative one-time state lives in
the challenge store.  That keeps ordinary session keys working exactly
as before while giving confirmation challenges an atomic
claim/completion lifecycle::

    AVAILABLE --claim--> CLAIMED --complete(succeeded)--> COMPLETED
                                  |
                                  +--complete(failed)--> FAILED
                                                     or RETRYABLE

``FAILED`` and ``COMPLETED`` are terminal.  With
:attr:`RetryPolicy.SAME_CLAIMANT` a failed execution makes the
challenge ``RETRYABLE`` for the original claimant only; it never turns
back into a challenge any request can claim.  The retry decision is
bound to the first successful claim and cannot be changed by a later
claim.

Completion should be recorded before the session is saved.  A failed
session save must not undo a completion; the challenge store, not the
session response, is the authority for whether an operation was
authorized.
"""

from __future__ import annotations

import abc
import hmac
import secrets
import threading
import time
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeVar, cast


class ChallengeState(str, Enum):
    """Lifecycle state of a one-time challenge."""

    AVAILABLE = "available"
    CLAIMED = "claimed"
    RETRYABLE = "retryable"
    COMPLETED = "completed"
    FAILED = "failed"


class ChallengeOutcome(str, Enum):
    """Result of executing the operation guarded by the challenge."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RetryPolicy(str, Enum):
    """Policy for a failed execution after a successful claim.

    The policy is chosen explicitly when the challenge is claimed for
    the first time.  The default, :attr:`NEVER`, makes every failed
    execution terminal.  :attr:`SAME_CLAIMANT` permits the original
    claimant to claim again, while all other claimants remain denied.
    """

    NEVER = "never"
    SAME_CLAIMANT = "same_claimant"


class ClaimDeniedReason(str, Enum):
    """Reason a claim was not granted.

    Reasons are deliberately coarse so a denial does not disclose the
    challenge's owner or whether a previous operation succeeded.
    """

    UNKNOWN = "unknown"
    BUSY = "busy"
    USED = "used"
    EXPIRED = "expired"


class CompletionDeniedReason(str, Enum):
    """Reason a completion could not be recorded."""

    UNKNOWN = "unknown"
    NOT_CLAIMED = "not_claimed"
    TOKEN_MISMATCH = "token_mismatch"
    EXPIRED = "expired"


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of an atomic challenge claim.

    ``claim_token`` is present only when ``granted`` is true.  Keep it
    private to the request that performed the claim; it is required to
    complete the challenge and cannot be reconstructed from the
    challenge identifier.
    """

    granted: bool
    challenge_id: str
    claim_token: str | None = None
    reason: ClaimDeniedReason | None = None


@dataclass(frozen=True)
class CompletionResult:
    """Outcome of a challenge completion.

    A denied completion never changes the stored state.  ``state`` is
    the challenge state after recording the outcome and is therefore
    explicit about whether the operation is terminal or retryable.
    """

    completed: bool
    challenge_id: str
    outcome: ChallengeOutcome | None = None
    state: ChallengeState | None = None
    reason: CompletionDeniedReason | None = None


@dataclass(frozen=True)
class ChallengeSnapshot:
    """Read-only view of a challenge record.

    This is intended for diagnostics and tests.  Authorization decisions
    must use :meth:`AbstractChallengeStore.claim_challenge` and
    :meth:`AbstractChallengeStore.complete_challenge`, not a snapshot
    followed by a separate mutation.
    """

    challenge_id: str
    state: ChallengeState
    claimant: str | None
    retry_policy: RetryPolicy
    claim_count: int


class AbstractChallengeStore(abc.ABC):
    """Optional persistent contract for one-time challenge state.

    Implementations must make each individual method atomic.  In
    particular, :meth:`claim_challenge` and
    :meth:`complete_challenge` must be compare-and-set style operations
    at the storage backend; loading a record, checking it in application
    code, and saving it afterwards is not a valid implementation of this
    contract.

    The challenge identifier is normally stored in the session as an
    ordinary key.  The store only owns the one-time state associated
    with that identifier.
    """

    @abc.abstractmethod
    async def issue_challenge(
        self, challenge_id: str, *, ttl: float | None = None
    ) -> bool:
        """Create an AVAILABLE challenge.

        Return ``True`` when the challenge was created.  If the
        identifier already exists, return ``False`` without changing
        its state.  In particular, issuing must never reset a claimed,
        completed, or failed challenge.
        """

    @abc.abstractmethod
    async def claim_challenge(
        self,
        challenge_id: str,
        *,
        claimant: str,
        retry_policy: RetryPolicy = RetryPolicy.NEVER,
    ) -> ClaimResult:
        """Atomically claim a challenge for ``claimant``.

        At most one caller can receive ``granted=True`` for an
        AVAILABLE challenge.  A caller that already owns a RETRYABLE
        challenge may claim it again only when the recorded policy is
        :attr:`RetryPolicy.SAME_CLAIMANT`.  A caller that does not own a
        RETRYABLE challenge is denied.

        ``retry_policy`` is recorded on the first successful claim and
        is ignored by retries, so a later request cannot loosen the
        policy chosen at claim time.
        """

    @abc.abstractmethod
    async def complete_challenge(
        self,
        challenge_id: str,
        claim_token: str,
        *,
        outcome: ChallengeOutcome,
    ) -> CompletionResult:
        """Record the result of the claimed operation.

        Completion must be idempotency-safe: a stale, missing, or
        mismatched token cannot change state.  A successful outcome is
        terminal regardless of the retry policy.  A failed outcome is
        terminal under :attr:`RetryPolicy.NEVER`; under
        :attr:`RetryPolicy.SAME_CLAIMANT` it becomes RETRYABLE for the
        original claimant only.
        """

    @abc.abstractmethod
    async def snapshot(self, challenge_id: str) -> ChallengeSnapshot | None:
        """Return diagnostic state for ``challenge_id``."""

    @abc.abstractmethod
    async def discard_challenge(self, challenge_id: str) -> bool:
        """Remove a challenge record during explicit cleanup.

        This is for application-driven cancellation and retention
        cleanup.  It must never be called as part of failure handling to
        make a consumed challenge available again; challenge
        identifiers must not be reused.
        """


def _new_claim_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass
class _ChallengeRecord:
    challenge_id: str
    state: ChallengeState
    claimant: str | None = None
    claim_token: str | None = None
    retry_policy: RetryPolicy = RetryPolicy.NEVER
    claim_count: int = 0
    expires_at: float | None = None
    expired: bool = False


class MemoryChallengeStore(AbstractChallengeStore):
    """In-memory challenge store for a single process.

    The implementation serializes state transitions with a thread lock,
    so it is atomic for coroutines in one event loop and for threads in
    one process.  It does **not** coordinate separate processes or
    hosts.  A multi-process deployment must use a store backed by a
    shared atomic primitive, for example a Redis Lua script or a
    compare-and-set database operation.

    The store is a plain request-local service, not an
    :class:`~aiohttp_session.AbstractStorage`.  Pass one instance to
    every request handler; creating a store per request would defeat
    the one-time guarantee.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._clock = clock if clock is not None else time.monotonic
        self._token_factory = (
            token_factory if token_factory is not None else _new_claim_token
        )
        self._lock = threading.Lock()
        self._records: dict[str, _ChallengeRecord] = {}
        self._discarded: set[str] = set()

    async def issue_challenge(
        self, challenge_id: str, *, ttl: float | None = None
    ) -> bool:
        _validate_challenge_id(challenge_id)
        if ttl is not None and ttl <= 0:
            raise ValueError("ttl must be positive")
        with self._lock:
            if challenge_id in self._records or challenge_id in self._discarded:
                return False
            now = self._clock()
            self._records[challenge_id] = _ChallengeRecord(
                challenge_id=challenge_id,
                state=ChallengeState.AVAILABLE,
                expires_at=now + ttl if ttl is not None else None,
            )
            return True

    async def claim_challenge(
        self,
        challenge_id: str,
        *,
        claimant: str,
        retry_policy: RetryPolicy = RetryPolicy.NEVER,
    ) -> ClaimResult:
        _validate_challenge_id(challenge_id)
        _validate_claimant(claimant)
        with self._lock:
            record = self._records.get(challenge_id)
            if record is None:
                return ClaimResult(
                    granted=False,
                    challenge_id=challenge_id,
                    reason=ClaimDeniedReason.UNKNOWN,
                )
            now = self._clock()
            if self._expire(record, now):
                return ClaimResult(
                    granted=False,
                    challenge_id=challenge_id,
                    reason=ClaimDeniedReason.EXPIRED,
                )

            if record.state is ChallengeState.CLAIMED:
                return ClaimResult(
                    granted=False,
                    challenge_id=challenge_id,
                    reason=ClaimDeniedReason.BUSY,
                )
            if record.state is ChallengeState.RETRYABLE:
                if record.claimant != claimant:
                    return ClaimResult(
                        granted=False,
                        challenge_id=challenge_id,
                        reason=ClaimDeniedReason.USED,
                    )
            elif record.state is not ChallengeState.AVAILABLE:
                return ClaimResult(
                    granted=False,
                    challenge_id=challenge_id,
                    reason=ClaimDeniedReason.USED,
                )
            else:
                record.retry_policy = RetryPolicy(retry_policy)

            record.state = ChallengeState.CLAIMED
            record.claimant = claimant
            record.claim_token = self._token_factory()
            record.claim_count += 1
            return ClaimResult(
                granted=True,
                challenge_id=challenge_id,
                claim_token=record.claim_token,
            )

    async def complete_challenge(
        self,
        challenge_id: str,
        claim_token: str,
        *,
        outcome: ChallengeOutcome,
    ) -> CompletionResult:
        _validate_challenge_id(challenge_id)
        if not claim_token:
            raise ValueError("claim_token must not be empty")
        with self._lock:
            record = self._records.get(challenge_id)
            if record is None:
                return CompletionResult(
                    completed=False,
                    challenge_id=challenge_id,
                    reason=CompletionDeniedReason.UNKNOWN,
                )
            now = self._clock()
            if self._expire(record, now):
                return CompletionResult(
                    completed=False,
                    challenge_id=challenge_id,
                    outcome=outcome,
                    reason=CompletionDeniedReason.EXPIRED,
                )
            if record.state is not ChallengeState.CLAIMED:
                return CompletionResult(
                    completed=False,
                    challenge_id=challenge_id,
                    outcome=outcome,
                    reason=CompletionDeniedReason.NOT_CLAIMED,
                )
            if record.claim_token is None or not hmac.compare_digest(
                record.claim_token, claim_token
            ):
                return CompletionResult(
                    completed=False,
                    challenge_id=challenge_id,
                    outcome=outcome,
                    reason=CompletionDeniedReason.TOKEN_MISMATCH,
                )

            outcome = ChallengeOutcome(outcome)
            record.claim_token = None
            if outcome is ChallengeOutcome.SUCCEEDED:
                record.state = ChallengeState.COMPLETED
            elif record.retry_policy is RetryPolicy.SAME_CLAIMANT:
                record.state = ChallengeState.RETRYABLE
            else:
                record.state = ChallengeState.FAILED
            return CompletionResult(
                completed=True,
                challenge_id=challenge_id,
                outcome=outcome,
                state=record.state,
            )

    async def snapshot(self, challenge_id: str) -> ChallengeSnapshot | None:
        _validate_challenge_id(challenge_id)
        with self._lock:
            record = self._records.get(challenge_id)
            if record is None:
                return None
            self._expire(record, self._clock())
            return ChallengeSnapshot(
                challenge_id=record.challenge_id,
                state=record.state,
                claimant=record.claimant,
                retry_policy=record.retry_policy,
                claim_count=record.claim_count,
            )

    async def discard_challenge(self, challenge_id: str) -> bool:
        _validate_challenge_id(challenge_id)
        with self._lock:
            if self._records.pop(challenge_id, None) is None:
                return False
            self._discarded.add(challenge_id)
            return True

    def _expire(self, record: _ChallengeRecord, now: float) -> bool:
        if record.expires_at is None or now < record.expires_at:
            return False
        if record.state in (ChallengeState.COMPLETED, ChallengeState.FAILED):
            return record.expired
        record.state = ChallengeState.FAILED
        record.claim_token = None
        record.claimant = None
        record.expired = True
        return True


def _validate_challenge_id(challenge_id: str) -> None:
    if not challenge_id:
        raise ValueError("challenge_id must not be empty")


def _validate_claimant(claimant: str) -> None:
    if not claimant:
        raise ValueError("claimant must not be empty")


T = TypeVar("T")


@dataclass(frozen=True)
class ChallengeExecution(Generic[T]):
    """Explicit result of :func:`run_session_challenge`.

    ``authorized`` distinguishes a denied claim from an operation that
    legitimately returned ``None``.  ``state`` is the challenge state
    after completion, or ``None`` when the claim was denied.  Inspect
    ``state`` if the application must distinguish a terminal completion
    from a completion the store refused to record.
    """

    authorized: bool
    state: ChallengeState | None = None
    result: T | None = None
    reason: ClaimDeniedReason | None = None


def challenge_expired(result: ClaimResult | CompletionResult) -> bool:
    """Return whether a denied result was caused by challenge expiry."""
    return result.reason is not None and result.reason.value == "expired"


class ChallengeLease:
    """A granted right to execute one confirmation operation.

    A lease is obtained from :meth:`AbstractChallengeStore.claim_challenge`
    or from :func:`claim_session_challenge`.  It is not reusable after
    :meth:`complete` succeeds.  The ``state`` attribute mirrors the
    authoritative store state after completion.
    """

    def __init__(
        self,
        store: AbstractChallengeStore,
        claim: ClaimResult,
    ) -> None:
        if not claim.granted or claim.claim_token is None:
            raise ValueError("A lease requires a granted claim with a token")
        self._store = store
        self._claim = claim
        self._completed = False
        self._state = ChallengeState.CLAIMED

    @property
    def challenge_id(self) -> str:
        return self._claim.challenge_id

    @property
    def claim_token(self) -> str:
        return cast(str, self._claim.claim_token)

    @property
    def state(self) -> ChallengeState:
        return self._state

    @property
    def completed(self) -> bool:
        return self._completed

    async def complete(self, outcome: ChallengeOutcome) -> CompletionResult:
        """Record the operation outcome before the session is saved."""
        if self._completed:
            raise RuntimeError("Challenge lease has already been completed")
        result = await self._store.complete_challenge(
            self.challenge_id,
            self.claim_token,
            outcome=outcome,
        )
        if result.completed:
            self._completed = True
            if result.state is not None:
                self._state = result.state
        return result

    async def execute(self, operation: Callable[[], Awaitable[T]]) -> T:
        """Run ``operation`` and record success or failure.

        Exceptions from the operation mark the challenge as failed
        before propagating.  A failure means the operation was not
        authorized to succeed; whether the same claimant may retry is
        governed by the store's recorded retry policy, never by turning
        the challenge back into an AVAILABLE one.
        """
        if self._completed:
            raise RuntimeError("Challenge lease has already been completed")
        try:
            result = await operation()
        except Exception as exc:
            try:
                await self.complete(ChallengeOutcome.FAILED)
            except Exception as completion_error:
                _add_completion_note(exc, completion_error)
            raise
        await self.complete(ChallengeOutcome.SUCCEEDED)
        return result

    async def snapshot(self) -> ChallengeSnapshot | None:
        return await self._store.snapshot(self.challenge_id)


async def claim_session_challenge(
    session: MutableMapping[str, Any],
    store: AbstractChallengeStore,
    key: str,
    *,
    claimant: str,
    retry_policy: RetryPolicy = RetryPolicy.NEVER,
) -> ChallengeLease | None:
    """Claim the challenge identifier stored under an ordinary session key.

    The session mapping is only read here; claiming does not mutate
    ordinary session keys.  Return ``None`` when the key is absent or
    when the store denies the claim, which callers should treat as a
    denial of the sensitive operation.

    ``claimant`` must be chosen by the application to identify the
    executing principal or request and must not be attacker-controlled.
    """
    challenge_id = session.get(key)
    if not isinstance(challenge_id, str) or not challenge_id:
        return None
    result = await store.claim_challenge(
        challenge_id,
        claimant=claimant,
        retry_policy=retry_policy,
    )
    if not result.granted or result.claim_token is None:
        return None
    return ChallengeLease(store, result)


async def complete_session_challenge(
    lease: ChallengeLease,
    session: MutableMapping[str, Any],
    key: str,
    outcome: ChallengeOutcome,
) -> CompletionResult:
    """Complete a challenge, then update the ordinary session key.

    The challenge store records the outcome first.  Only after that is
    the ordinary session key removed, and only when the challenge is
    terminal and the session still holds this challenge identifier.
    If the session save later fails, the old session value may
    reappear, but the store still denies it: a failed save cannot make
    a consumed challenge available again.

    A RETRYABLE challenge keeps the key because the same claimant may
    retry; other claimants are denied by the store.
    """
    result = await lease.complete(outcome)
    if result.completed and result.state is not None:
        if (
            result.state is not ChallengeState.RETRYABLE
            and session.get(key) == lease.challenge_id
        ):
            session.pop(key, None)
            _mark_changed(session)
    return result


def _add_completion_note(exc: Exception, completion_error: Exception) -> None:
    add_note = getattr(exc, "add_note", None)
    if add_note is not None:
        add_note(
            "Challenge completion also failed: "
            f"{type(completion_error).__name__}: {completion_error}"
        )


def _mark_changed(session: MutableMapping[str, Any]) -> None:
    changed = getattr(session, "changed", None)
    if changed is not None:
        changed()


async def run_session_challenge(
    session: MutableMapping[str, Any],
    store: AbstractChallengeStore,
    key: str,
    *,
    claimant: str,
    operation: Callable[[], Awaitable[T]],
    retry_policy: RetryPolicy = RetryPolicy.NEVER,
) -> ChallengeExecution[T]:
    """Claim the challenge, execute ``operation``, and complete it.

    Return a :class:`ChallengeExecution` whose ``authorized`` flag is
    true only when this request received the claim.  A denied claim
    leaves ordinary session keys untouched and never executes
    ``operation``.  A successful operation marks the challenge
    COMPLETED.  An exception marks the challenge FAILED or RETRYABLE
    before propagating, so the caller cannot accidentally leave a
    claimed challenge undecided.  A cancellation is not treated as an
    execution failure; the challenge stays CLAIMED and must be handled
    by application cleanup.
    """
    challenge_id = session.get(key)
    if not isinstance(challenge_id, str) or not challenge_id:
        return ChallengeExecution(
            authorized=False,
            reason=ClaimDeniedReason.UNKNOWN,
        )
    claim = await store.claim_challenge(
        challenge_id,
        claimant=claimant,
        retry_policy=retry_policy,
    )
    if not claim.granted or claim.claim_token is None:
        return ChallengeExecution(authorized=False, reason=claim.reason)
    lease = ChallengeLease(store, claim)
    try:
        result = await operation()
    except Exception as exc:
        try:
            await complete_session_challenge(
                lease,
                session,
                key,
                ChallengeOutcome.FAILED,
            )
        except Exception as completion_error:
            _add_completion_note(exc, completion_error)
        raise
    completion = await complete_session_challenge(
        lease,
        session,
        key,
        ChallengeOutcome.SUCCEEDED,
    )
    return ChallengeExecution(
        authorized=True,
        state=completion.state,
        result=result,
    )


async def issue_session_challenge(
    session: MutableMapping[str, Any],
    store: AbstractChallengeStore,
    key: str,
    *,
    challenge_id: str | None = None,
    ttl: float | None = None,
) -> str | None:
    """Issue a challenge and store its identifier under a normal session key.

    Return the new identifier, or ``None`` when the session key is
    already occupied or the store refuses to create the identifier.
    The identifier is ordinary session data; no other session key
    changes behaviour.
    """
    if key in session:
        return None
    challenge_id = (
        challenge_id if challenge_id is not None else secrets.token_urlsafe(32)
    )
    if not await store.issue_challenge(challenge_id, ttl=ttl):
        return None
    session[key] = challenge_id
    return challenge_id
