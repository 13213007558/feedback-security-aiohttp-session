"""Commit semantics for session saves.

A change made to a :class:`~aiohttp_session.Session` goes through three
observable stages before it reaches the client:

1. It lives only in the in-memory session object (nothing has been sent to
   the storage backend yet).
2. The storage backend has committed it (a Redis/Memcached record exists, or
   the cookie payload has been produced) but the response that carries the
   matching cookie has not been handed back yet.
3. The response carrying the cookie is returned (and eventually sent to the
   client).

The exception hierarchy below lets an application tell *where* a save
stopped so it can decide whether retrying is safe instead of treating every
failure as a generic "save failed".
"""

from enum import Enum


class CommitStage(str, Enum):
    """How far a session change got through the save pipeline."""

    #: The change was never handed to the storage backend. Nothing was
    #: persisted, so the whole operation can be retried safely.
    NOT_SAVED = "not_saved"

    #: The storage backend accepted the change, but the response could not be
    #: updated with the matching cookie. The server-side state already
    #: exists, so re-running non-idempotent side effects is unsafe.
    PERSISTED = "persisted"

    #: The response was already prepared (headers sent) and can no longer
    #: carry a session cookie. Neither the response nor the request can be
    #: repaired at this point.
    COMMITTED = "committed"


class SessionError(RuntimeError):
    """Base class for every failure raised by the session machinery.

    Attributes:
        stage: The :class:`CommitStage` the change had reached when the
            failure happened.
        retryable: ``True`` when re-issuing the *whole* request is safe
            because no state has been committed anywhere.
        persisted: ``True`` once the storage backend has committed the
            change.
        response_mutable: ``True`` while the response may still be altered
            to carry the session cookie.
    """

    stage: CommitStage = CommitStage.NOT_SAVED

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.__class__.__doc__)

    @property
    def retryable(self) -> bool:
        return self.stage is CommitStage.NOT_SAVED

    @property
    def persisted(self) -> bool:
        return self.stage in (CommitStage.PERSISTED, CommitStage.COMMITTED)

    @property
    def response_mutable(self) -> bool:
        return self.stage is not CommitStage.COMMITTED


class SessionNotSavedError(SessionError):
    """The session change never reached the storage backend."""

    stage = CommitStage.NOT_SAVED


class SessionPersistedError(SessionError):
    """The backend stored the change but the response could not be updated."""

    stage = CommitStage.PERSISTED

    def __init__(
        self, message: str | None = None, *, identity: str | None = None
    ) -> None:
        super().__init__(message)
        #: Storage key/identity that was committed server-side, when known.
        #: Applications use it as the idempotency key for post-login effects.
        self.identity = identity


class SessionResponseLockedError(SessionError):
    """The response was already prepared and cannot carry a session cookie."""

    stage = CommitStage.COMMITTED
