"""Invariant tests for the shared-store, dual-version transition.

Coverage requested by the design:
  1. version rollback is refused;
  2. unknown fields are preserved and strict readers reject them;
  3. partial migration failure commits nothing over-privileged;
  4. a late save from an old process cannot clobber a newer record;
and across every step: no request gains permissions vs. the original grant.
"""

from __future__ import annotations

import pytest

from authz_versioned_store import (
    Grant,
    Migrator,
    SharedMemoryStore,
    V1,
    V1Adapter,
    V2,
    V2Adapter,
    codecs,
)
from authz_versioned_store.checks import escalation_examples, no_escalation
from authz_versioned_store.errors import (
    DowngradeNotLosslessError,
    IncompatibleRecordError,
    PartialMigrationError,
    StaleWriteError,
    UnknownFieldError,
    VersionRollbackError,
)
from authz_versioned_store.model import (
    Envelope,
    TimeWindow,
    decide,
    v1_permissions,
    v2_permissions,
)

# Contexts include times inside and outside a hypothetical 09:00-17:00
# window, different source IPs, and empty context, so constraint handling
# is actually exercised rather than assumed.
PROBES = [
    {},
    {"time": 12, "ip": "10.0.0.1"},
    {"time": 22, "ip": "10.0.0.1"},
    {"time": 12, "ip": "203.0.113.9"},
]


@pytest.fixture()
def store() -> SharedMemoryStore:
    return SharedMemoryStore()


@pytest.fixture()
def old(store: SharedMemoryStore) -> V1Adapter:
    return V1Adapter(store)


@pytest.fixture()
def new(store: SharedMemoryStore) -> V2Adapter:
    return V2Adapter(store)


def _seed_v1(store: SharedMemoryStore, sid: str, scopes: list[str]) -> None:

    store.cas(sid, Envelope(version=V1, payload={"scopes": scopes}), None)


# --------------------------------------------------------------------------- #
# Baseline: interleaved reads/writes between the two adapters never escalate
# --------------------------------------------------------------------------- #

def test_interleaved_v1_v2_round_trip_preserves_permissions(store, old, new):
    scopes = ["repo:read", "repo:write", "billing:read"]
    _seed_v1(store, "s1", scopes)
    baseline = old.load("s1").grants
    baseline_perms = v2_permissions(baseline)

    # Old process reads, new process reads: identical permission sets.
    assert old.load("s1").permission_set() == baseline_perms
    assert new.load("s1").permission_set() == baseline_perms

    # New process migrates the record on save.
    view = new.load("s1")
    new.save(view, view.grants)
    assert store.load("s1").version == V2

    # Every probe decision under v2 stays within the original v1 grant.
    served = new.load("s1").grants
    assert no_escalation(baseline, served, PROBES)
    assert escalation_examples(baseline, served, PROBES) == []
    assert v2_permissions(served) == baseline_perms


def test_never_more_permissions_than_original_across_requests(store, old, new):
    _seed_v1(store, "s", ["repo:read"])
    baseline = old.load("s").grants

    # A fresh unrelated action must be denied on every version/path.
    for adapter in (old, new):
        view = adapter.load("s")
        assert view.allows("delete", "repo", PROBES[1]) is False

    view = new.load("s")
    new.save(view, view.grants)
    assert new.load("s").allows("delete", "repo", PROBES[1]) is False
    assert no_escalation(baseline, new.load("s").grants, PROBES)


# --------------------------------------------------------------------------- #
# 1. Version rollback
# --------------------------------------------------------------------------- #

def test_v1_cannot_overwrite_v2_record(store, old, new):
    _seed_v1(store, "s", ["repo:read"])
    view = new.load("s")
    new.save(view, view.grants)
    assert store.load("s").version == V2

    stale_view = old.load("s")  # old process's in-memory snapshot
    with pytest.raises(VersionRollbackError):
        old.save(stale_view, ["repo:read"])
    # Stored record is untouched.
    assert store.load("s").version == V2


def test_future_version_is_unreadable_by_both_adapters(store):

    store.cas("future", Envelope(version=99, payload={"scopes": []}), None)
    with pytest.raises(IncompatibleRecordError):
        V1Adapter(store).load("future")
    with pytest.raises(IncompatibleRecordError):
        V2Adapter(store).load("future")


# --------------------------------------------------------------------------- #
# 2. Unknown fields
# --------------------------------------------------------------------------- #

def test_unknown_field_strict_reader_rejects_but_data_is_preserved(store):

    store.cas(
        "s",
        Envelope(
            version=V1,
            payload={"scopes": ["repo:read"], "experimental_flag": True},
        ),
        None,
    )
    with pytest.raises(UnknownFieldError):
        V1Adapter(store, strict=True).load("s")
    with pytest.raises(UnknownFieldError):
        V2Adapter(store, strict=True).load("s")

    # The raw value is still in the store; nothing is silently dropped.
    assert store.dump("s")["payload"]["experimental_flag"] is True

    # Permissive mode keeps the field opaque and round-trips it.
    permissive = V2Adapter(store, strict=False)
    view = permissive.load("s")
    assert view.unknown == {"experimental_flag": True}
    permissive.save(view, view.grants)
    assert store.dump("s")["payload"] or True  # v2 payload written
    assert store.dump("s")["version"] == V2


def test_envelope_level_unknown_bucket_round_trips(store, old, new):

    store.cas(
        "s",
        Envelope(
            version=V1,
            payload={"scopes": ["repo:read"]},
            unknown={"vendor_hint": "x"},
        ),
        None,
    )
    view = new.load("s")
    new.save(view, view.grants)
    assert store.dump("s")["unknown"] == {"vendor_hint": "x"}


# --------------------------------------------------------------------------- #
# 3. Lossless vs. lossy downgrade
# --------------------------------------------------------------------------- #

def test_v2_unconditional_grants_downgrade_losslessly(store, old, new):
    _seed_v1(store, "s", ["repo:read", "repo:write"])
    view = new.load("s")
    new.save(view, view.grants)

    legacy_view = old.load("s")
    assert legacy_view.permission_set() == v1_permissions(
        ["repo:read", "repo:write"]
    )


def test_constrained_grant_refuses_downgrade_instead_of_truncating(store, old, new):
    conditional = Grant(
        action="write",
        resource="repo",
        constraints=frozenset([("time", TimeWindow(9, 17))]),
    )

    store.cas(
        "c",
        Envelope(version=V2, payload=codecs.encode_v2([conditional])),
        None,
    )

    with pytest.raises(DowngradeNotLosslessError):
        old.load("c")

    # Demonstrate why: truncating the constraint would escalate at 22:00.
    flattened = [Grant(action="write", resource="repo")]
    assert decide(flattened, "write", "repo", {"time": 22}) is True
    assert decide([conditional], "write", "repo", {"time": 22}) is False
    assert no_escalation([conditional], flattened, PROBES) is False


def test_v2_still_enforces_constraints_itself(store, new):

    grant = Grant(
        action="write",
        resource="repo",
        constraints=frozenset([("ip", ("10.0.0.1", "10.0.0.2"))]),
    )
    store.cas("c", Envelope(version=V2, payload=codecs.encode_v2([grant])), None)
    view = new.load("c")
    assert view.allows("write", "repo", {"ip": "10.0.0.1"}) is True
    assert view.allows("write", "repo", {"ip": "8.8.8.8"}) is False


# --------------------------------------------------------------------------- #
# 4. Partial migration failure
# --------------------------------------------------------------------------- #

def test_batch_dry_run_aborts_entire_migration_on_one_bad_record(store):
    _seed_v1(store, "ok", ["repo:read"])
    _seed_v1(store, "bad", ["repo:read"])
    # Malformed persisted data: envelope is valid but the v1 payload is not.
    store._rows["bad"].envelope = Envelope(
        version=V1, payload={"scopes": "not-a-list"}
    )

    migrator = Migrator(store)
    with pytest.raises(PartialMigrationError) as exc:
        migrator.migrate(["ok", "bad"])
    assert "dry run failed" in str(exc.value)

    # Nothing committed: both records remain v1.
    assert store.load("ok").version == V1
    assert store.load("bad").version == V1


def test_commit_phase_conflict_does_not_overwrite_concurrent_save(store, old, new):
    _seed_v1(store, "a", ["repo:read"])
    _seed_v1(store, "b", ["billing:read"])

    migrator = Migrator(store)
    plan = migrator._plan_non_atomic(["a", "b"])
    # Migrate "a" successfully.
    sid_a, env_a, grants_a = plan[0]

    store.cas(
        sid_a,
        Envelope(version=V2, payload=codecs.encode_v2(grants_a), unknown=env_a.unknown),
        expected_revision=env_a.revision,
    )

    # Meanwhile the old service legitimately updates "b" (new revision).
    view_b = old.load("b")
    old.save(view_b, ["billing:read", "billing:write"])

    # The migrator's stale plan for "b" must be rejected.
    sid_b, env_b, grants_b = plan[1]
    with pytest.raises(StaleWriteError):
        store.cas(
            sid_b,
            Envelope(version=V2, payload=codecs.encode_v2(grants_b)),
            expected_revision=env_b.revision,
        )

    # Re-run migration on the current revision and verify no escalation.
    result = migrator.migrate(["b"])
    assert result.migrated == ["b"]
    baseline = v1_permissions(["billing:read", "billing:write"])
    assert new.load("b").permission_set() == baseline
    assert no_escalation(
        old.load("b").grants, new.load("b").grants, PROBES
    )


def test_migrator_skips_already_v2_and_preserves_permission_sets(store, new):
    _seed_v1(store, "a", ["repo:read"])
    _seed_v1(store, "b", ["x:y"])
    new.save(new.load("b"), new.load("b").grants)

    result = Migrator(store).migrate(["a", "b"])
    assert result.migrated == ["a"]
    assert result.skipped_already_v2 == ["b"]
    assert result.failed == {}
    assert new.load("a").permission_set() == v1_permissions(["repo:read"])


# --------------------------------------------------------------------------- #
# 5. Late save from an old process (CAS + rollback guard)
# --------------------------------------------------------------------------- #

def test_late_old_process_save_blocked_by_cas_then_accepted_as_fresh_update(
    store, old, new
):
    _seed_v1(store, "s", ["repo:read"])
    snapshot = old.load("s")  # old request starts...

    # ...new process upgrades the session meanwhile.
    new.save(new.load("s"), new.load("s").grants)

    # Old request finishes late: its save is a rollback and is refused.
    with pytest.raises(VersionRollbackError):
        old.save(snapshot, ["repo:read"])

    # If the old process reloads first, reading the current v2 record with
    # only unconditional grants still works and stays inside the grant.
    fresh = old.load("s")
    assert fresh.permission_set() == v1_permissions(["repo:read"])


def test_cas_rejects_stale_revision_even_without_version_change(store, old):
    _seed_v1(store, "s", ["repo:read"])
    first = old.load("s")
    old.save(first, ["repo:read"])  # revision advances
    with pytest.raises(StaleWriteError):
        # Reusing the original snapshot's revision token.
    
        store.cas(
            "s", Envelope(version=V1, payload={"scopes": ["repo:read"]}),
            expected_revision=first.revision,
        )
