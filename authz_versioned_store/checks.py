"""Reusable privilege-escalation guard used throughout the simulation.

The migration contract is: *no request, at any step, may gain permissions
relative to the original authorization*. This module turns that contract
into an executable check against a battery of request contexts.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from authz_versioned_store.model import (
    Grant,
    decide,
    decision_subset_of_original,
    v2_permissions,
)


def no_escalation(
    baseline: Sequence[Grant],
    served: Sequence[Grant],
    probe_contexts: Sequence[Mapping[str, object]],
) -> bool:
    return decision_subset_of_original(baseline, served, probe_contexts)


def escalation_examples(
    baseline: Sequence[Grant],
    served: Sequence[Grant],
    probe_contexts: Sequence[Mapping[str, object]],
) -> list[tuple[str, str, Mapping[str, object]]]:
    """Return concrete (resource, action, context) triples where the served
    view would allow something the original authorization denies."""

    found: list[tuple[str, str, Mapping[str, object]]] = []
    for resource, action in v2_permissions(baseline):
        for context in probe_contexts:
            if decide(served, action, resource, context) and not decide(
                baseline, action, resource, context
            ):
                found.append((resource, action, context))
    return found
