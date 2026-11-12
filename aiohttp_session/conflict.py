"""Configurable conflict-handling contract for server-side session saves.

Two concurrent requests can load the same session, change different parts
of it, and save in any order.  A plain last-write-wins store lets the
late save silently resurrect data the other request deleted -- for
example a permission that was just revoked.  The contract defined here
splits session keys into two explicit groups with different rules:

* permission fields (``permission_fields``) are merged recursively with
  *deletion wins* semantics: a removal made by either request, at any
  nesting depth, is never undone by a concurrent stale save;
* every other (normal) field is merged at top-level field granularity:
  a save only overwrites the fields it actually changed, so
  non-conflicting updates from concurrent requests are both kept.

The merge is a three-way merge against the snapshot taken when the
session was loaded, and is only meaningful when the storage can guard
the read/modify/write cycle with a conditional (compare-and-swap) write.
Storages that cannot do that refuse the contract instead of silently
falling back to overwrite semantics.
"""

import dataclasses
from collections.abc import Mapping
from typing import Any


class ConflictError(RuntimeError):
    """Raised when a session save keeps losing the conditional-write race.

    The storage retried the read/merge/write cycle up to the contract's
    ``max_retries`` and the stored version changed every single time, so
    the save was aborted instead of overwriting someone else's update.
    """


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


MISSING: Any = _Missing()


@dataclasses.dataclass(frozen=True)
class ConflictContract:
    """Declarative rules for resolving concurrent session saves.

    ``permission_fields``
        Top-level session keys that hold authorization data.  They are
        merged recursively and a deletion on either side always wins, so
        a late save can never restore a revoked permission.

    ``max_retries``
        How many times a save may re-read, re-merge and retry after a
        conditional-write conflict before giving up with
        :class:`ConflictError`.
    """

    permission_fields: frozenset[str] = frozenset()
    max_retries: int = 3

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "permission_fields", frozenset(self.permission_fields)
        )
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")


def _merge_permission_field(base: Any, local: Any, stored: Any) -> Any:
    """Three-way merge of one permission field, deletion wins."""
    if local is MISSING:
        # Deleted locally (or never present): the removal always wins.
        return MISSING
    if local == base:
        # Not touched locally: take the stored value (possibly a deletion).
        return stored
    if stored is MISSING:
        if base is MISSING:
            # Brand new field added locally, nothing stored to conflict with.
            return local
        # Stored side deleted it after we loaded: deletion wins.
        return MISSING
    if stored == base:
        # Only the local side changed it.
        return local
    # Both sides changed the field relative to the loaded snapshot.
    if isinstance(base, dict) and isinstance(local, dict) and isinstance(stored, dict):
        merged: dict[str, Any] = {}
        for key in base.keys() | local.keys() | stored.keys():
            value = _merge_permission_field(
                base.get(key, MISSING),
                local.get(key, MISSING),
                stored.get(key, MISSING),
            )
            if value is not MISSING:
                merged[key] = value
        return merged
    # Conflicting scalar replacement: the save being applied wins.
    return local


def merge_session_data(
    contract: ConflictContract,
    *,
    base: Mapping[str, Any],
    local: Mapping[str, Any],
    stored: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge a stale session save into the currently stored session.

    ``base`` is the snapshot taken when the session was loaded, ``local``
    is the session mapping the current request wants to save and
    ``stored`` is the mapping currently in the storage (which may already
    include a concurrent request's changes).
    """
    permission_fields = contract.permission_fields
    merged = dict(stored)
    for key in base.keys() | local.keys():
        if key in permission_fields:
            continue
        local_value = local.get(key, MISSING)
        if local_value is MISSING:
            # Deleted locally: remove it even if the stored side still has it.
            merged.pop(key, None)
        elif key not in base or local_value != base[key]:
            # Changed locally (top-level set or in-place nested mutation):
            # field-level last writer wins.
            merged[key] = local_value
        # Unchanged locally: keep the stored value already in ``merged``.
    for key in permission_fields:
        value = _merge_permission_field(
            base.get(key, MISSING),
            local.get(key, MISSING),
            stored.get(key, MISSING),
        )
        if value is MISSING:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged
