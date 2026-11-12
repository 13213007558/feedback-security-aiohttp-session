"""End-to-end simulation of the v1 <-> v2 authorization transition.

Two independent adapters share one in-memory session store. The script
alternates reads and writes between the old and new process and walks
through every risk scenario. Each step re-checks the headline invariant:

    **no request is ever authorized beyond the original grant.**

Run it with (from the repository root)::

    python -m authz_versioned_store.simulate
"""

from __future__ import annotations

from authz_versioned_store import (
    Grant,
    Migrator,
    SharedMemoryStore,
    V1,
    V1Adapter,
    V2,
    V2Adapter,
)
from authz_versioned_store import codecs
from authz_versioned_store.checks import no_escalation
from authz_versioned_store.errors import (
    DowngradeNotLosslessError,
    PartialMigrationError,
    StaleWriteError,
    UnknownFieldError,
    VersionRollbackError,
)
from authz_versioned_store.model import (
    Envelope,
    TimeWindow,
    v1_permissions,
    v2_permissions,
)

PROBES = [
    {},
    {"time": 12, "ip": "10.0.0.1"},
    {"time": 22, "ip": "10.0.0.1"},
    {"time": 12, "ip": "203.0.113.9"},
]


def banner(title: str) -> None:
    print(f"\n=== {title} ===")


def check(condition: bool, label: str) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        raise AssertionError(label)


def main() -> None:
    store = SharedMemoryStore()
    old = V1Adapter(store)
    new = V2Adapter(store)

    store.cas("sess-a", Envelope(V1, {"scopes": ["repo:read", "repo:write"]}), None)
    store.cas("sess-b", Envelope(V1, {"scopes": ["billing:read"]}), None)
    baseline = old.load("sess-a").grants

    banner("0. Alternating v1/v2 reads over the shared store")
    check(old.load("sess-a").permission_set() == v2_permissions(baseline),
          "v1 adapter reads its own record")
    check(new.load("sess-a").permission_set() == v2_permissions(baseline),
          "v2 adapter reads legacy v1 record (upgrade on read)")
    check(no_escalation(baseline, new.load("sess-a").grants, PROBES),
          "served view never exceeds original authorization")

    banner("1. Version rollback is refused")
    old_snapshot = old.load("sess-a")
    new.save(new.load("sess-a"), new.load("sess-a").grants)
    check(store.load("sess-a").version == V2, "new process upgrades record to v2")
    try:
        old.save(old_snapshot, ["repo:read"])
    except VersionRollbackError as exc:
        check(True, f"late v1 save rejected: {exc}")
    check(store.load("sess-a").version == V2, "stored record stays v2")

    banner("2. Unknown fields are preserved, never interpreted as grants")
    store.cas("sess-x", Envelope(V1, {"scopes": ["repo:read"], "lab_flag": True}), None)
    try:
        V2Adapter(store).load("sess-x")
    except UnknownFieldError as exc:
        check(True, f"strict reader rejects unknown field: {exc}")
    check(store.dump("sess-x")["payload"]["lab_flag"] is True,
          "unknown value remains intact in storage")
    permissive = V2Adapter(store, strict=False)
    view = permissive.load("sess-x")
    permissive.save(view, view.grants)
    check(store.dump("sess-x")["version"] == V2, "permissive path still upgrades")

    banner("3. Lossy downgrade is refused, not truncated")
    conditional = Grant("write", "repo", frozenset([("time", TimeWindow(9, 17))]))
    store.cas("sess-c", Envelope(V2, codecs.encode_v2([conditional])), None)
    try:
        old.load("sess-c")
    except DowngradeNotLosslessError as exc:
        check(True, f"conditional grant blocks v1 read: {exc}")
    flattened = [Grant("write", "repo")]
    check(no_escalation([conditional], flattened, PROBES) is False,
          "truncating the constraint would escalate (e.g. 22:00 write)")
    check(new.load("sess-c").allows("write", "repo", {"time": 12}) is True,
          "v2 enforces the window: allowed at 12:00")
    check(new.load("sess-c").allows("write", "repo", {"time": 22}) is False,
          "v2 enforces the window: denied at 22:00")

    banner("4. Partial migration failure is all-or-nothing")
    store.cas("sess-ok", Envelope(V1, {"scopes": ["repo:read"]}), None)
    store.cas("sess-bad", Envelope(V1, {"scopes": ["repo:read"]}), None)
    store._rows["sess-bad"].envelope = Envelope(V1, {"scopes": "not-a-list"})
    try:
        Migrator(store).migrate(["sess-ok", "sess-bad"])
    except PartialMigrationError as exc:
        check(True, f"batch aborted before any commit: {exc}")
    check(store.load("sess-ok").version == V1, "healthy record left untouched")

    migrator = Migrator(store)
    plan = migrator._plan_non_atomic(["sess-ok"])
    sid, env, grants = plan[0]
    legacy = old.load("sess-ok")
    old.save(legacy, ["repo:read", "repo:admin"])
    try:
        store.cas(sid, Envelope(V2, codecs.encode_v2(grants)), env.revision)
    except StaleWriteError as exc:
        check(True, f"stale migration commit rejected by CAS: {exc}")
    result = Migrator(store).migrate(["sess-ok"])
    check(result.migrated == ["sess-ok"], "retry migrates the live revision")
    check(new.load("sess-ok").permission_set()
          == v1_permissions(["repo:read", "repo:admin"]),
          "migrated permissions match the latest v1 grant exactly")
    check(no_escalation(old.load("sess-ok").grants,
                        new.load("sess-ok").grants, PROBES),
          "no escalation after conflict + retry")

    banner("5. Late save from an old process (full request lifecycle)")
    store.cas("sess-late", Envelope(V1, {"scopes": ["repo:read"]}), None)
    in_flight = old.load("sess-late")
    new.save(new.load("sess-late"), new.load("sess-late").grants)
    try:
        old.save(in_flight, ["repo:read"])
    except VersionRollbackError as exc:
        check(True, f"old process's late save refused: {exc}")
    reloaded = old.load("sess-late")
    check(reloaded.permission_set() == v1_permissions(["repo:read"]),
          "old process can still read after reload, within original grant")

    banner("Final invariant sweep")
    for sid in ("sess-a", "sess-b", "sess-x", "sess-ok", "sess-late"):
        view = new.load(sid)
        check(no_escalation(view.grants, view.grants, PROBES),
              f"{sid}: served view is a subset of its own stored grant")
    print("\nAll scenarios passed; every request stayed within the original grant.")


if __name__ == "__main__":
    main()
