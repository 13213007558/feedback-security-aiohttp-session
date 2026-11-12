"""Authorization data model and the invariant at its heart.

Two schema versions share one session store during the transition:

* **v1 (legacy)** -- flat string scopes, e.g. ``"repo:read"``.
* **v2 (new)**    -- structured grants. A grant is an action on a resource,
  optionally carrying constraints (time/IP window, ...). Constraints only
  *restrict* a grant, never extend it.

The core safety rule, checked in :func:`decision_subset_of_original`, is:

    every request served at any point of the migration must be authorized by
    a subset of the permissions originally granted -- never a superset.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, FrozenSet, Mapping, Sequence

from authz_versioned_store.errors import IncompatibleRecordError

V1 = 1
V2 = 2
SUPPORTED_VERSIONS = (V1, V2)


@dataclass(frozen=True)
class TimeWindow:
    """Inclusive validity window used as a hashable constraint value."""

    after: float
    before: float

    def contains(self, value: object) -> bool:
        return isinstance(value, (int, float)) and self.after <= value <= self.before

    def to_wire(self) -> dict[str, float]:
        return {"after": self.after, "before": self.before}

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> "TimeWindow":
        return cls(after=raw["after"], before=raw["before"])


@dataclass(frozen=True)
class Grant:
    """One v2 permission: ``action`` on ``resource`` plus optional limits.

    ``constraints`` narrows the grant (validity window, source network,
    approval id...). A grant with empty constraints is an unconditional
    grant and is the *only* shape v1 can express.
    """

    action: str
    resource: str
    constraints: FrozenSet[tuple[str, Any]] = frozenset()

    @property
    def scope(self) -> str:
        return f"{self.resource}:{self.action}"

    @property
    def is_unconditional(self) -> bool:
        return not self.constraints

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "action": self.action,
            "resource": self.resource,
        }
        if self.constraints:
            out["constraints"] = sorted(
                (key, _jsonable(value)) for key, value in self.constraints
            )
        return out

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> "Grant":
        return cls(
            action=str(raw["action"]),
            resource=str(raw["resource"]),
            constraints=frozenset(
                (str(key), _freeze(value))
                for key, value in raw.get("constraints", ())
            ),
        )

@dataclass(frozen=True)
class Envelope:
    """Versioned record envelope stored in the shared session backend.

    ``payload`` is the version-specific authorization document.
    ``unknown`` preserves fields a reader's schema does not understand, so
    they can round-trip and be inspected instead of being silently dropped.
    """

    version: int
    payload: Mapping[str, Any]
    unknown: Mapping[str, Any] = field(default_factory=dict)
    revision: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "revision": self.revision,
            "payload": _jsonable(self.payload),
            "unknown": _jsonable(self.unknown),
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> "Envelope":
        if not isinstance(raw, Mapping) or "version" not in raw:
            raise IncompatibleRecordError(f"malformed envelope: {raw!r}")
        version = raw["version"]
        if not isinstance(version, int):
            raise IncompatibleRecordError("version must be an integer")
        return cls(
            version=version,
            payload=dict(raw.get("payload", {})),
            unknown=dict(raw.get("unknown", {})),
            revision=int(raw.get("revision", 0)),
        )


# --------------------------------------------------------------------------- #
# Authorization semantics
# --------------------------------------------------------------------------- #

# A "permission" for subset-checking purposes is a (resource, action) pair.
# Constraints further restrict *when* it applies but never enlarge it.
Permission = tuple[str, str]


def v1_permissions(scopes: Sequence[str]) -> FrozenSet[Permission]:
    out: set[Permission] = set()
    for scope in scopes:
        resource, _, action = scope.partition(":")
        out.add((resource, action))
    return frozenset(out)


def v2_permissions(grants: Sequence[Grant]) -> FrozenSet[Permission]:
    return frozenset((grant.resource, grant.action) for grant in grants)


def decide(
    grants: Sequence[Grant],
    action: str,
    resource: str,
    context: Mapping[str, Any] | None = None,
) -> bool:
    """Authorization decision used by both adapters.

    A request is allowed only when a matching grant exists *and* every
    constraint on that grant is satisfied by the request context. An
    unconditional grant is allowed in every context.
    """

    context = context or {}
    for grant in grants:
        if grant.action != action or grant.resource != resource:
            continue
        if all(_satisfies(key, value, context) for key, value in grant.constraints):
            return True
    return False


def _satisfies(key: str, required: Any, context: Mapping[str, Any]) -> bool:
    # A missing context attribute can never satisfy a constraint: deny
    # rather than raise, so a malformed request never turns into an allow.
    if key not in context:
        return False
    actual = context[key]
    if isinstance(required, TimeWindow):
        return required.contains(actual)
    if isinstance(required, (list, tuple, set, frozenset)):
        return actual in required
    return actual == required


def decision_subset_of_original(
    original_grants: Sequence[Grant],
    served_grants: Sequence[Grant],
    probe_contexts: Sequence[Mapping[str, Any]],
) -> bool:
    """Invariant: the served view may never allow more than the original.

    For every (resource, action) the original document mentions and for
    every probe context, an allow decision from the served view must also
    be an allow decision from the original. A ``False`` result means the
    migration/downgrade would have *escalated* privileges and must be
    rejected.
    """

    pairs = v2_permissions(original_grants)
    for resource, action in pairs:
        for context in probe_contexts:
            if decide(served_grants, action, resource, context) and not decide(
                original_grants, action, resource, context
            ):
                return False
    return True


def _freeze(value: Any) -> Any:
    """Coerce wire-format containers into hashable values for frozenset."""

    if isinstance(value, Mapping):
        if set(value) == {"after", "before"}:
            return TimeWindow.from_wire(value)
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, TimeWindow):
        return value.to_wire()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value
