"""Shared in-memory session backend with compare-and-swap writes.

Both the v1 and v2 adapters point at the *same* instance, which models two
service versions sharing one session store during the rollout. A real
Redis/DB backend would expose the same ``load`` / ``cas`` contract.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Mapping

from authz_versioned_store.errors import StaleWriteError
from authz_versioned_store.model import Envelope


@dataclass
class _Record:
    envelope: Envelope


class SharedMemoryStore:
    def __init__(self) -> None:
        self._rows: dict[str, _Record] = {}
        self._lock = threading.RLock()
        # Audit trail of writes, useful for proving ordering in tests.
        self.history: list[tuple[str, int, int]] = []

    def load(self, session_id: str) -> Envelope | None:
        with self._lock:
            row = self._rows.get(session_id)
            if row is None:
                return None
            # Hand back a detached copy so callers can't mutate storage.
            return Envelope.from_wire(row.envelope.to_wire())

    def cas(
        self,
        session_id: str,
        envelope: Envelope,
        expected_revision: int | None,
    ) -> Envelope:
        """Commit ``envelope`` only if the stored revision still matches.

        ``expected_revision=None`` means "create" and fails if the key
        already exists. Every successful write increments the revision,
        which is the token a reader must echo back on its next save.
        """

        with self._lock:
            row = self._rows.get(session_id)
            current = None if row is None else row.envelope.revision
            if current != expected_revision:
                raise StaleWriteError(
                    f"session={session_id!r} expected revision "
                    f"{expected_revision}, store has {current}"
                )
            stored = Envelope(
                version=envelope.version,
                payload=envelope.payload,
                unknown=envelope.unknown,
                revision=(expected_revision or 0) + 1,
            )
            self._rows[session_id] = _Record(stored)
            self.history.append((session_id, stored.version, stored.revision))
            return Envelope.from_wire(stored.to_wire())

    def dump(self, session_id: str) -> Mapping[str, Any] | None:
        with self._lock:
            row = self._rows.get(session_id)
            return None if row is None else row.envelope.to_wire()
