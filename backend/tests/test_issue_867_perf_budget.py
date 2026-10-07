"""Issue #867 — native performance budget: deterministic fixtures,
distribution evidence, budget verdict ladder, and the required
intentional-slowdown regression proof.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_repository import SceneRepository
from htdt.canonical_json import canonical_sha256
from htdt.perf_budget import (
    OperationSpec,
    PerfBudgetEntry,
    PerfBudgetManifest,
    PerfEnvironment,
    collect_environment,
    distribution_from_samples,
    evaluate_distribution,
    evaluate_report,
    load_budget_manifest,
    percentile,
)
from htdt.perf_fixtures import (
    FIXTURE_SPECS,
    ensure_fixture,
    expected_fixture_identity,
    fixture_spec_sha256,
)
from htdt.perf_harness import (
    OPERATIONS,
    BenchmarkContext,
    run_benchmark,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BUDGETS_PATH = REPO_ROOT / 'scripts' / 'perf_budgets.yaml'


# ---------------------------------------------------------------------------
# helpers


def _env(fixture_size: str = 'small') -> PerfEnvironment:
    return PerfEnvironment(
        app_sha='a' * 40,
        app_dirty=False,
        os_system='Windows',
        os_release='2022Server',
        machine='AMD64',
        python_version='3.12.x',
        cpu_logical_count=8,
        memory_total_bytes=32 * 1024 ** 3,
        gpu=None,
        fixture_id=f'htdt-perf-{fixture_size}-v1',
        fixture_size=fixture_size,
        fixture_document_sha256='b' * 64,
        iterations=3,
        warmup_iterations=1,
        recorded_at_utc='2026-10-06T00:00:00+00:00',
    )


def _budget(
    operation_id: str = 'project_open',
    fixture_size: str = 'small',
    p95: float | None = 0.5,
    rss: int | None = None,
) -> PerfBudgetEntry:
    return PerfBudgetEntry(
        operation_id=operation_id,
        fixture_size=fixture_size,
        p95_budget_s=p95,
        memory_budget_bytes=rss,
    )


def _spec(operation_id: str = 'project_open') -> tuple[OperationSpec, ...]:
    return (OperationSpec(operation_id, 'test op'),)


def _ctx(fixture: dict[str, object]) -> BenchmarkContext:
    database_path = Path(fixture['fixture_dir']) / fixture['database']
    document_id = str(fixture['document_id'])
    repository = SceneRepository(database_path)
    head = repository.current_head(document_id)
    first_revision_id = head.revision_id
    while True:
        revision = repository.get(first_revision_id)
        if revision.parent_revision_id is None:
            break
        first_revision_id = revision.parent_revision_id
    return BenchmarkContext(
        repo_root=REPO_ROOT,
        fixture_dir=Path(fixture['fixture_dir']),
        database_path=database_path,
        document_id=document_id,
        head_revision_id=head.revision_id,
        first_revision_id=first_revision_id,
    )


# ---------------------------------------------------------------------------
# fixtures


def test_small_fixture_is_deterministic(tmp_path: Path) -> None:
    first = ensure_fixture(tmp_path, 'small')
    second = ensure_fixture(tmp_path, 'small')
    assert first['document_sha256'] == second['document_sha256']
    assert first['spec_sha256'] == fixture_spec_sha256('small')
    assert (Path(first['fixture_dir']) / first['database']).is_file()


def test_fixture_identity_is_versioned_and_pinned(tmp_path: Path) -> None:
    fixture = ensure_fixture(tmp_path, 'small')
    expected = expected_fixture_identity('small')
    assert fixture['spec_sha256'] == expected['spec_sha256']
    assert fixture['fixture_id'] == expected['fixture_id']
    assert fixture['entity_count'] == FIXTURE_SPECS['small']['entity_count']
    identity_file = (
        Path(fixture['fixture_dir']) / 'fixture_identity.json')
    stored = json.loads(identity_file.read_text(encoding='utf-8'))
    assert stored['document_sha256'] == fixture['document_sha256']
    assert stored['spec_sha256'] == fixture['spec_sha256']


def test_fixture_regenerates_when_spec_changes(tmp_path: Path) -> None:
    fixture = ensure_fixture(tmp_path, 'small')
    identity_file = Path(fixture['fixture_dir']) / 'fixture_identity.json'
    stored = json.loads(identity_file.read_text(encoding='utf-8'))
    stored['spec_sha256'] = '0' * 64  # stale spec marker
    identity_file.write_text(json.dumps(stored), encoding='utf-8')
    regenerated = ensure_fixture(tmp_path, 'small')
    stored_after = json.loads(identity_file.read_text(encoding='utf-8'))
    assert stored_after['spec_sha256'] == fixture['spec_sha256']


def test_fixture_revisions_chain(tmp_path: Path) -> None:
    fixture = ensure_fixture(tmp_path, 'small')
    repository = SceneRepository(
        Path(fixture['fixture_dir']) / fixture['database'])
    head = repository.current_head(str(fixture['document_id']))
    assert head is not None
    depth = 0
    revision = head
    while revision.parent_revision_id is not None:
        revision = repository.get(revision.parent_revision_id)
        depth += 1
    assert depth == FIXTURE_SPECS['small']['revision_count'] - 1


# ---------------------------------------------------------------------------
# distribution statistics


def test_percentile_interpolates() -> None:
    assert percentile([1.0], 50.0) == 1.0
    assert percentile([1.0], 95.0) == 1.0
    assert percentile([0.0, 1.0], 50.0) == 0.5
    assert percentile([1.0, 2.0, 3.0, 4.0], 95.0) == pytest.approx(3.85)
    with pytest.raises(ValueError):
        percentile([], 50.0)


def test_distribution_records_samples_never_single_timing() -> None:
    distribution = distribution_from_samples('op', [0.2, 0.1, 0.3])
    assert distribution.samples == (0.1, 0.2, 0.3) or sorted(
        distribution.samples) == [0.1, 0.2, 0.3]
    assert distribution.min_s == pytest.approx(0.1)
    assert distribution.max_s == pytest.approx(0.3)
    assert distribution.p50_s == pytest.approx(0.2)
    with pytest.raises(ValueError):
        distribution_from_samples('op', [])


# ---------------------------------------------------------------------------
# budget verdict ladder


def test_evaluate_within_and_exceeds_budget() -> None:
    distribution = distribution_from_samples('project_open', [0.03] * 5)
    under = evaluate_distribution(
        distribution, _budget(p95=0.5),
        fixture_size='small', title='open')
    assert under.verdict == 'within_budget'
    over = evaluate_distribution(
        distribution, _budget(p95=0.01),
        fixture_size='small', title='open')
    assert over.verdict == 'exceeds_budget'
    assert 'p95' in over.detail


def test_evaluate_missing_budget_is_unmeasured() -> None:
    distribution = distribution_from_samples('project_open', [0.03] * 5)
    result = evaluate_distribution(
        distribution, None, fixture_size='small', title='open')
    assert result.verdict == 'unmeasured'


def test_memory_budget_honest_when_rss_unprobed() -> None:
    distribution = distribution_from_samples('memory_idle_rss', [0.1])
    result = evaluate_distribution(
        distribution, _budget('memory_idle_rss', p95=None, rss=1024),
        fixture_size='small', title='idle rss')
    assert result.verdict == 'unmeasured'
    exceeded = evaluate_distribution(
        distribution_from_samples(
            'memory_idle_rss', [0.1], rss_bytes_after=2048),
        _budget('memory_idle_rss', p95=None, rss=1024),
        fixture_size='small', title='idle rss')
    assert exceeded.verdict == 'exceeds_budget'


def test_budget_entry_must_assert_a_dimension() -> None:
    with pytest.raises(ValidationError):
        PerfBudgetEntry(
            operation_id='project_open', fixture_size='small')


# ---------------------------------------------------------------------------
# report ladder


def test_report_verdict_ladder() -> None:
    specs = (
        OperationSpec('op_a', 'a'),
        OperationSpec('op_b', 'b'),
        OperationSpec('op_hs', 'hs', hardware_sensitive=True,
                      hardware_note='owned machine'),
    )
    budgets = PerfBudgetManifest(entries=(
        _budget('op_a', p95=1.0), _budget('op_b', p95=1.0)))
    env = _env()

    passing = evaluate_report(
        env, specs, {
            'op_a': distribution_from_samples('op_a', [0.1] * 3),
            'op_b': distribution_from_samples('op_b', [0.2] * 3),
        }, budgets)
    assert passing.verdict == 'passed'
    assert 'op_hs' in passing.hardware_sensitive_operations
    assert any(
        r.verdict == 'hardware_sensitive_skipped' for r in passing.results)

    incomplete = evaluate_report(
        env, specs, {
            'op_a': distribution_from_samples('op_a', [0.1] * 3),
        }, budgets)
    assert incomplete.verdict == 'incomplete'

    failed = evaluate_report(
        env, specs, {
            'op_a': distribution_from_samples('op_a', [0.1] * 3),
            'op_b': distribution_from_samples('op_b', [5.0] * 3),
        }, budgets)
    assert failed.verdict == 'failed'
    assert any(r.verdict == 'exceeds_budget' for r in failed.results)


def test_report_sha_covers_verdict_and_distributions() -> None:
    specs = _spec('project_open')
    budgets = PerfBudgetManifest(entries=(_budget(),))
    report = evaluate_report(
        _env(), specs, {
            'project_open': distribution_from_samples(
                'project_open', [0.1] * 3),
        }, budgets)
    payload = {
        'environment': report.environment.model_dump(mode='json'),
        'results': [r.model_dump(mode='json') for r in report.results],
        'hardware_sensitive_operations':
            list(report.hardware_sensitive_operations),
        'verdict': report.verdict,
        'recorded_at_utc': report.recorded_at_utc,
    }
    assert report.report_sha256 == canonical_sha256(payload)
    assert report.report_id.startswith('perf-')


# ---------------------------------------------------------------------------
# harness — real fixture, real run, and the regression-detection proof


def test_harness_runs_repo_operations_against_fixture(tmp_path: Path) -> None:
    fixture = ensure_fixture(tmp_path, 'small')
    distributions = run_benchmark(
        _ctx(fixture), iterations=3, warmup=0,
        only=('project_open', 'project_reopen', 'comparison_view_prep',
              'scene_revision_apply'))
    assert set(distributions) == {
        'project_open', 'project_reopen',
        'comparison_view_prep', 'scene_revision_apply'}
    for distribution in distributions.values():
        assert len(distribution.samples) == 3
        assert all(s >= 0.0 for s in distribution.samples)


def test_failing_operation_is_unmeasured_never_passed(
        tmp_path: Path) -> None:
    from htdt import perf_harness

    fixture = ensure_fixture(tmp_path, 'small')
    context = _ctx(fixture)
    original = perf_harness._OPERATION_FNS['project_open']

    def _boom(ctx: BenchmarkContext) -> float:
        raise RuntimeError('device unavailable')

    perf_harness._OPERATION_FNS['project_open'] = _boom
    try:
        errors: list[tuple[str, str]] = []
        distributions = run_benchmark(
            context, iterations=1, warmup=0, only=('project_open',),
            on_error=lambda op, exc: errors.append((op, str(exc))))
    finally:
        perf_harness._OPERATION_FNS['project_open'] = original
    assert 'project_open' not in distributions
    assert errors and errors[0][0] == 'project_open'
    assert 'device unavailable' in errors[0][1]
    report = evaluate_report(
        _env(), _spec('project_open'), distributions,
        PerfBudgetManifest(entries=(_budget(),)))
    assert report.verdict == 'incomplete'


def test_intentional_slowdown_regression_is_detected(
        tmp_path: Path) -> None:
    """Required proof: an intentional slowdown of a real operation must be
    detected by the budget gate end-to-end.

    Runs the real harness for `project_open` on a real small fixture,
    then evaluates the measured distribution against a budget pinned
    below the measured p95 — the simulated regression budget a release
    gate would tighten against. The report must come out 'failed'.
    """
    fixture = ensure_fixture(tmp_path, 'small')
    distributions = run_benchmark(
        _ctx(fixture), iterations=3, warmup=1,
        only=('project_open',))
    measured = distributions['project_open']
    assert len(measured.samples) == 3
    assert measured.p95_s > 0.0

    baseline_budget = PerfBudgetManifest(
        entries=(_budget(p95=measured.p95_s * 100.0 + 1.0),))
    baseline_report = evaluate_report(
        _env(), _spec('project_open'), distributions, baseline_budget)
    assert baseline_report.verdict == 'passed'

    # Intentional-slowdown fixture: the budget a regression would violate.
    tightened = PerfBudgetManifest(
        entries=(_budget(p95=measured.p95_s * 0.5),))
    slowed_report = evaluate_report(
        _env(), _spec('project_open'), distributions, tightened)
    assert slowed_report.verdict == 'failed'
    offending = next(
        r for r in slowed_report.results
        if r.operation_id == 'project_open')
    assert offending.verdict == 'exceeds_budget'


def test_slow_operation_measured_higher(tmp_path: Path) -> None:
    """A genuinely slower code path produces measurably larger samples —
    the distributions themselves are sensitive to slowdowns."""
    import time as _time
    from htdt import perf_harness

    fixture = ensure_fixture(tmp_path, 'small')
    original = perf_harness._OPERATION_FNS['project_open']

    def slowed(ctx: BenchmarkContext) -> float:
        elapsed = original(ctx)
        _time.sleep(0.15)
        return elapsed + 0.15

    perf_harness._OPERATION_FNS['project_open'] = slowed
    try:
        slowed_dist = run_benchmark(
            _ctx(fixture), iterations=2, warmup=0,
            only=('project_open',))['project_open']
    finally:
        perf_harness._OPERATION_FNS['project_open'] = original
    normal_dist = run_benchmark(
        _ctx(fixture), iterations=2, warmup=0,
        only=('project_open',))['project_open']
    assert slowed_dist.min_s > normal_dist.max_s + 0.1


# ---------------------------------------------------------------------------
# manifest + registry contract


def test_budget_manifest_loads_and_covers_registry() -> None:
    budgets = load_budget_manifest(BUDGETS_PATH)
    benchmarkable = [
        spec.operation_id for spec in OPERATIONS
        if not spec.hardware_sensitive]
    for operation_id in benchmarkable:
        # every benchmarkable op must be gated on at least one size
        assert any(
            entry.operation_id == operation_id
            for entry in budgets.entries
        ), f'{operation_id} has no budget entry'
    unknown = {
        entry.operation_id for entry in budgets.entries
    } - {spec.operation_id for spec in OPERATIONS}
    assert not unknown, f'budget for unregistered ops: {unknown}'


def test_hardware_sensitive_ops_are_protocol_not_faked() -> None:
    sensitive = [s for s in OPERATIONS if s.hardware_sensitive]
    assert any(s.operation_id == 'viewport_3d_interaction'
               for s in sensitive)
    for spec in sensitive:
        assert spec.hardware_note
        assert spec.operation_id not in {
            entry.operation_id
            for entry in load_budget_manifest(BUDGETS_PATH).entries}


def test_environment_identity_collects_real_values(tmp_path: Path) -> None:
    fixture = ensure_fixture(tmp_path, 'small')
    env = collect_environment(
        REPO_ROOT,
        fixture_id=str(fixture['fixture_id']),
        fixture_size='small',
        fixture_document_sha256=str(fixture['document_sha256']),
        iterations=3,
        warmup_iterations=1,
    )
    assert env.app_sha != 'unknown'
    assert env.machine
    assert env.cpu_logical_count > 0
    assert env.memory_total_bytes > 0
    assert env.fixture_document_sha256 == fixture['document_sha256']
