"""Versioned authorization-field transition simulation.

Two service generations share one session store while the authorization
field moves from flat v1 string scopes to structured v2 grants. See
``simulate.py`` for the end-to-end scenario runner and ``tests/`` for the
invariant tests.
"""

from authz_versioned_store.adapters import SessionView, V1Adapter, V2Adapter
from authz_versioned_store.memory_store import SharedMemoryStore
from authz_versioned_store.migration import Migrator, MigrationResult
from authz_versioned_store.model import (
    V1,
    V2,
    Grant,
    decide,
    decision_subset_of_original,
)

__all__ = [
    "V1",
    "V2",
    "Grant",
    "Migrator",
    "MigrationResult",
    "SessionView",
    "SharedMemoryStore",
    "V1Adapter",
    "V2Adapter",
    "decide",
    "decision_subset_of_original",
]
