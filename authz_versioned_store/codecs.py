"""Schema codecs and the lossless upgrade / lossy-downgrade boundary.

* v1 payload: ``{"scopes": ["resource:action", ...]}``
* v2 payload: ``{"grants": [{"action", "resource", "constraints"?}, ...]}``

Unknown keys are never interpreted: they are split into an ``unknown`` map
that round-trips untouched. Strict readers (the default for authorization)
raise :class:`UnknownFieldError` rather than guess.
"""

from __future__ import annotations

from typing import Any, Mapping

from authz_versioned_store.errors import (
    DowngradeNotLosslessError,
    IncompatibleRecordError,
    UnknownFieldError,
)
from authz_versioned_store.model import V1, V2, Grant

_V1_KNOWN = frozenset({"scopes"})
_V2_KNOWN = frozenset({"grants"})


# --------------------------------------------------------------------------- #
# v1
# --------------------------------------------------------------------------- #

def decode_v1(payload: Mapping[str, Any], *, strict: bool) -> tuple[list[str], dict[str, Any]]:
    unknown = {key: val for key, val in payload.items() if key not in _V1_KNOWN}
    if unknown and strict:
        raise UnknownFieldError(f"v1 reader cannot interpret keys: {sorted(unknown)}")
    scopes = payload.get("scopes", [])
    if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
        raise IncompatibleRecordError("v1 'scopes' must be a list of strings")
    return list(scopes), unknown


def encode_v1(scopes: list[str]) -> dict[str, Any]:
    return {"scopes": list(scopes)}


# --------------------------------------------------------------------------- #
# v2
# --------------------------------------------------------------------------- #

def decode_v2(
    payload: Mapping[str, Any], *, strict: bool
) -> tuple[list[Grant], dict[str, Any]]:
    unknown = {key: val for key, val in payload.items() if key not in _V2_KNOWN}
    if unknown and strict:
        raise UnknownFieldError(f"v2 reader cannot interpret keys: {sorted(unknown)}")
    raw_grants = payload.get("grants", [])
    if not isinstance(raw_grants, list):
        raise IncompatibleRecordError("v2 'grants' must be a list")
    grants = [Grant.from_wire(raw) for raw in raw_grants]
    return grants, unknown


def encode_v2(grants: list[Grant]) -> dict[str, Any]:
    return {"grants": [grant.to_wire() for grant in grants]}


# --------------------------------------------------------------------------- #
# Cross-version conversion
# --------------------------------------------------------------------------- #

def upgrade_v1_to_v2(scopes: list[str]) -> list[Grant]:
    """Always lossless: a flat scope is an unconditional v2 grant."""

    grants: list[Grant] = []
    for scope in scopes:
        resource, _, action = scope.partition(":")
        grants.append(Grant(action=action, resource=resource))
    return grants


def downgrade_v2_to_v1(grants: list[Grant]) -> list[str]:
    """Return v1 scopes only when *every* grant survives losslessly.

    A constrained grant cannot be expressed in v1. Emitting its bare scope
    would silently delete the constraint and *expand* access, so the whole
    downgrade is refused -- no truncation, no partial flattening.
    """

    blocking = [
        (grant.scope, sorted(grant.constraints))
        for grant in grants
        if not grant.is_unconditional
    ]
    if blocking:
        raise DowngradeNotLosslessError(
            "refusing downgrade: constrained grants have no v1 representation: "
            f"{blocking}"
        )
    return [grant.scope for grant in grants]


def readable_by_v1(envelope_version: int) -> bool:
    """Explicit statement of the old process's readable range.

    v1 reads its own version natively; v2 is readable only through the
    lossless downgrade path (``downgrade_v2_to_v1`` still refuses
    conditional grants at call time); anything newer is unreadable.
    """

    return envelope_version in (V1, V2)
