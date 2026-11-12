"""Two independent storage adapters sharing one backend.

``V1Adapter`` and ``V2Adapter`` deliberately keep their own encoding,
unknown-field policy and readable range. Neither inherits the other's
behavior; the only thing they share is the :class:`SharedMemoryStore`
instance passed in at construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from authz_versioned_store import codecs
from authz_versioned_store.errors import (
    IncompatibleRecordError,
    VersionRollbackError,
)
from authz_versioned_store.memory_store import SharedMemoryStore
from authz_versioned_store.model import (
    Envelope,
    V1,
    V2,
    Grant,
    decide,
    v2_permissions,
)


@dataclass
class SessionView:
    """What an adapter hands to request handlers.

    ``grants`` is the authorization view used for decisions; ``unknown``
    carries opaque fields that must survive a write-back.
    """

    session_id: str
    version: int
    revision: int
    grants: list[Grant]
    unknown: dict[str, Any] = field(default_factory=dict)

    def allows(
        self, action: str, resource: str, context: Mapping[str, Any] | None = None
    ) -> bool:
        return decide(self.grants, action, resource, context)

    def permission_set(self) -> frozenset[tuple[str, str]]:
        return v2_permissions(self.grants)


class _BaseAdapter:
    version: int

    def __init__(self, store: SharedMemoryStore, *, strict: bool = True) -> None:
        self.store = store
        self.strict = strict
        # Revision token captured by _guard_no_rollback and consumed by cas.
        self._expected: int | None = None

    def load(self, session_id: str) -> SessionView | None:  # pragma: no cover - interface
        raise NotImplementedError

    # -- shared rollback guard used by every save path ------------------- #
    def _guard_no_rollback(self, session_id: str, new_version: int) -> None:
        current = self.store.load(session_id)
        if current is not None and current.version > new_version:
            raise VersionRollbackError(
                f"session={session_id!r}: refusing to write schema v{new_version} "
                f"over stored v{current.version}"
            )
        self._expected = None if current is None else current.revision


class V1Adapter(_BaseAdapter):
    """Legacy service: understands only flat scopes."""

    version = V1

    def load(self, session_id: str) -> SessionView | None:
        envelope = self.store.load(session_id)
        if envelope is None:
            return None
        if envelope.version == V1:
            scopes, unknown = codecs.decode_v1(envelope.payload, strict=self.strict)
            grants = codecs.upgrade_v1_to_v2(scopes)
        elif envelope.version == V2:
            # Readable range for v1: a v2 record is readable *only* if it
            # downgrades losslessly. Conditional grants raise here.
            v2_grants, unknown = codecs.decode_v2(
                envelope.payload, strict=self.strict
            )
            scopes = codecs.downgrade_v2_to_v1(v2_grants)
            grants = codecs.upgrade_v1_to_v2(scopes)
        else:
            raise IncompatibleRecordError(
                f"v1 reader cannot read schema v{envelope.version}"
            )
        return SessionView(
            session_id=session_id,
            version=envelope.version,
            revision=envelope.revision,
            grants=grants,
            unknown=dict(envelope.unknown, **unknown),
        )

    def save(self, view: SessionView, scopes: list[str]) -> SessionView:
        self._guard_no_rollback(view.session_id, self.version)
        payload = codecs.encode_v1(scopes)

        saved = self.store.cas(
            view.session_id,
            Envelope(version=V1, payload=payload, unknown=view.unknown),
            expected_revision=self._expected,
        )
        return SessionView(
            session_id=view.session_id,
            version=V1,
            revision=saved.revision,
            grants=codecs.upgrade_v1_to_v2(scopes),
            unknown=dict(view.unknown),
        )


class V2Adapter(_BaseAdapter):
    """New service: structured grants, native reader of both versions."""

    version = V2

    def load(self, session_id: str) -> SessionView | None:
        envelope = self.store.load(session_id)
        if envelope is None:
            return None
        if envelope.version == V2:
            grants, unknown = codecs.decode_v2(envelope.payload, strict=self.strict)
        elif envelope.version == V1:
            scopes, unknown = codecs.decode_v1(envelope.payload, strict=self.strict)
            grants = codecs.upgrade_v1_to_v2(scopes)
        else:
            raise IncompatibleRecordError(
                f"v2 reader cannot read schema v{envelope.version}"
            )
        return SessionView(
            session_id=session_id,
            version=envelope.version,
            revision=envelope.revision,
            grants=grants,
            unknown=dict(envelope.unknown, **unknown),
        )

    def save(self, view: SessionView, grants: list[Grant]) -> SessionView:
        self._guard_no_rollback(view.session_id, self.version)

        saved = self.store.cas(
            view.session_id,
            Envelope(
                version=V2,
                payload=codecs.encode_v2(grants),
                unknown=view.unknown,
            ),
            expected_revision=self._expected,
        )
        return SessionView(
            session_id=view.session_id,
            version=V2,
            revision=saved.revision,
            grants=list(grants),
            unknown=dict(view.unknown),
        )
