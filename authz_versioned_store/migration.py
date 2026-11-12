"""Batch v1 -> v2 migration with all-or-nothing semantics.

A migration batch runs in two phases:

1. **dry run** -- load and convert every target record in memory. Any
   unreadable record, unknown field in strict mode, or bad revision aborts
   the whole batch before a single write.
2. **commit** -- CAS each converted record using the revision read in
   phase 1. A concurrent writer invalidates a revision and the batch is
   reported as partially failed; records already written in *this* batch
   are still valid v2, and the caller simply re-runs for the rest.

Because v1->v2 only maps flat scopes to unconditional grants, the
permission set is preserved exactly (never enlarged) on every success.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from authz_versioned_store import codecs
from authz_versioned_store.errors import (
    AuthzError,
    PartialMigrationError,
    StaleWriteError,
)
from authz_versioned_store.memory_store import SharedMemoryStore
from authz_versioned_store.model import V1, V2, Envelope, Grant, v1_permissions, v2_permissions


@dataclass
class MigrationResult:
    migrated: list[str]
    skipped_already_v2: list[str]
    failed: dict[str, str]


class Migrator:
    def __init__(self, store: SharedMemoryStore, *, strict: bool = True) -> None:
        self.store = store
        self.strict = strict

    def _plan(self, session_ids: Iterable[str]) -> list[tuple[str, Envelope, list[Grant]]]:
        plan: list[tuple[str, Envelope, list[Grant]]] = []
        problems: dict[str, str] = {}
        for session_id in session_ids:
            envelope = self.store.load(session_id)
            if envelope is None:
                problems[session_id] = "missing session"
                continue
            if envelope.version == V2:
                continue
            if envelope.version != V1:
                problems[session_id] = f"unsupported source version v{envelope.version}"
                continue
            try:
                scopes, _ = codecs.decode_v1(envelope.payload, strict=self.strict)
                grants = codecs.upgrade_v1_to_v2(scopes)
            except AuthzError as exc:
                # One unreadable record poisons the whole batch; nothing is
                # committed until every record converts cleanly.
                problems[session_id] = f"{type(exc).__name__}: {exc}"
                continue
            # Safety assertion: conversion must preserve the permission set.
            if v1_permissions(scopes) != v2_permissions(grants):
                problems[session_id] = "permission set changed during conversion"
                continue
            plan.append((session_id, envelope, grants))
        if problems:
            raise PartialMigrationError(
                f"dry run failed, no records committed: {problems}"
            )
        return plan

    def migrate(
        self, session_ids: Iterable[str], *, atomic_dry_run: bool = True
    ) -> MigrationResult:
        session_ids = list(session_ids)
        already_v2 = [
            sid for sid in session_ids
            if (env := self.store.load(sid)) is not None and env.version == V2
        ]
        targets = [sid for sid in session_ids if sid not in already_v2]

        plan = self._plan(targets) if atomic_dry_run else self._plan_non_atomic(targets)

        migrated: list[str] = []
        failed: dict[str, str] = {}
        for session_id, envelope, grants in plan:
            try:
                self.store.cas(
                    session_id,
                    Envelope(
                        version=V2,
                        payload=codecs.encode_v2(grants),
                        unknown=envelope.unknown,
                    ),
                    expected_revision=envelope.revision,
                )
            except StaleWriteError as exc:
                # Concurrent save moved the record; never overwrite it.
                failed[session_id] = str(exc)
            else:
                migrated.append(session_id)

        if failed:
            raise PartialMigrationError(
                f"commit phase conflict for {sorted(failed)}; "
                f"committed={migrated}"
            )
        return MigrationResult(migrated=migrated, skipped_already_v2=already_v2, failed=failed)

    def _plan_non_atomic(
        self, session_ids: Iterable[str]
    ) -> list[tuple[str, Envelope, list[Grant]]]:
        """Fault-injection variant: convert one at a time, surfacing failures
        per record so tests can prove partial migration never leaves an
        over-privileged state."""

        plan: list[tuple[str, Envelope, list[Grant]]] = []
        for session_id in session_ids:
            envelope = self.store.load(session_id)
            if envelope is None or envelope.version != V1:
                continue
            scopes, _ = codecs.decode_v1(envelope.payload, strict=self.strict)
            grants = codecs.upgrade_v1_to_v2(scopes)
            plan.append((session_id, envelope, grants))
        return plan
