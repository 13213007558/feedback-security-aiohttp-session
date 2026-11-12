"""Self-checking tests for the revocation prototype, runnable under pytest.

The prototype lives outside the package's ``tests/`` path on purpose; run it
with its own config so the root ``pytest.ini`` testpaths do not hide it::

    .venv/bin/pytest -c prototype/pytest.ini prototype
"""

from scenarios import (
    scenario_denylist,
    scenario_generation,
    scenario_global_rotation_is_not_per_account,
    scenario_short_ttl,
    scenario_sqlite_durability,
    scenario_stateless_baseline,
)
from core import Clock

SCENARIOS = [
    scenario_stateless_baseline,
    scenario_short_ttl,
    scenario_generation,
    scenario_denylist,
    scenario_global_rotation_is_not_per_account,
    scenario_sqlite_durability,
]


def test_all_scenario_checks_pass() -> None:
    for scenario in SCENARIOS:
        checks = scenario(Clock())
        failed = [check.name for check in checks if not check.ok]
        assert not failed, f"{scenario.__name__} failed: {failed}"


def test_generation_is_account_scoped() -> None:
    checks = {check.name: check for check in scenario_generation(Clock())}
    assert checks["generation: other account (carol) is unaffected"].ok
    assert checks["generation: laptop rejected immediately after bump"].ok
    assert checks["generation: phone rejected immediately after bump"].ok


def test_global_rotation_blocks_everyone() -> None:
    checks = {
        check.name: check
        for check in scenario_global_rotation_is_not_per_account(Clock())
    }
    assert checks["key-rotation: target alice is blocked"].ok
    assert checks["key-rotation: unrelated carol is ALSO blocked"].ok
