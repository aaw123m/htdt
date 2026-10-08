"""Issue #938 reproduction-isolation diagnostic contract tests.

The committed plan JSON is pinned by semantic hash inside
``r130d_general3d_validation``; the diagnostic driver re-executes PFFDTD
legs and classifies the non-convergence cause.  These tests guard the
loader pin, the value-replay comparator, the bin cause classifier, the
hypothesis table, and the verdict state machine.  Refs #938.
"""
from __future__ import annotations

import cmath
import copy
import json
import math
from pathlib import Path

import pytest

from htdt.r130d_general3d_validation import (
    REPRODUCTION_ISOLATION_DIAGNOSTIC_PLAN_SCHEMA,
    build_reproduction_hypothesis_table,
    classify_dense_bin_cause,
    classify_reproduction_isolation,
    load_reproduction_isolation_diagnostic_plan,
    reproduction_values_match,
    wrapped_phase_separation_deg,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN_PATH = (
    REPO_ROOT
    / 'benchmarks'
    / 'acoustics'
    / 'r130d_reproduction_isolation_diagnostic_plan.json'
)
SUMMARY_PATH = (
    REPO_ROOT
    / 'benchmarks'
    / 'acoustics'
    / 'r130d_reproduction_isolation_diagnostic_summary.json'
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def test_plan_loader_pins_frozen_authority() -> None:
    plan = load_reproduction_isolation_diagnostic_plan(PLAN_PATH)
    assert plan['schema_version'] == (
        REPRODUCTION_ISOLATION_DIAGNOSTIC_PLAN_SCHEMA
    )
    assert plan['issue'] == 938
    assert plan['analytic_rigid_box']['wall_index_offset_cells'] == 2
    assert 'halo_separation_probe' in plan['analytic_rigid_box']


def test_plan_loader_rejects_mutated_plan(tmp_path: Path) -> None:
    plan = copy.deepcopy(_load(PLAN_PATH))
    plan['analytic_rigid_box']['record_duration_s'] = 0.5
    mutated = tmp_path / 'mutated_plan.json'
    mutated.write_text(json.dumps(plan), encoding='utf-8')
    with pytest.raises(ValueError, match='frozen pre-run authority'):
        load_reproduction_isolation_diagnostic_plan(mutated)


def test_committed_summary_schema_and_verdicts() -> None:
    summary = _load(SUMMARY_PATH)
    assert summary['schema_version'] == (
        'htdt.r130d.reproduction-isolation-diagnostic-committed-summary-1'
    )
    assert summary['diagnostic_verdict'] == 'REPRODUCED_AND_ISOLATED'
    assert summary['run25_values_match'] is True
    assert summary['run62_values_match'] is True
    assert summary['run76_all_identical'] is True
    assert summary['axis_verdicts']['record_prefix_identity'] == (
        'RECORD_PREFIX_IDENTICAL'
    )
    assert summary['axis_verdicts']['boundary_halo_separation'] == (
        'HALO_ADJACENCY_ABSORBS_CONFIRMED'
    )
    assert summary['axis_verdicts']['cfl_dt_variants'] == (
        'CFL_DT_WORSENING_PERSISTS'
    )
    assert summary['axis_verdicts']['phase_floor_rescore'] == (
        'PHASE_NONMONOTONIC_UNDER_TIGHTER_FLOOR'
    )
    assert summary['axis_verdicts']['worsening_cause_separation'] == (
        'WORSENING_BINS_CLASSIFIED'
    )
    hypotheses = {
        row['hypothesis']: row['status']
        for row in summary['hypothesis_table']
    }
    assert hypotheses['stencil_dispersion'] == 'SUPPORTED'
    assert hypotheses['boundary_impedance'] == 'SUPPORTED'
    assert hypotheses['time_window_discrete_dtft'] == (
        'REJECTED_AS_PRINCIPAL'
    )
    assert hypotheses['fem_conditioning_pollution'] == (
        'UNRESOLVED_ENVIRONMENT_BLOCKED'
    )


def test_reproduction_values_match_tolerance() -> None:
    a = {'x': 1.0, 'list': [1.0, 2.0], 'nested': {'y': 'tag'}}
    b = {'x': 1.0 + 5e-13, 'list': [1.0, 2.0], 'nested': {'y': 'tag'}}
    c = {'x': 1.0 + 2e-12, 'list': [1.0, 2.0], 'nested': {'y': 'tag'}}
    assert reproduction_values_match(a, b) is True
    assert reproduction_values_match(a, c) is False


def test_wrapped_phase_difference_avoids_false_near_360_degree_jump() -> None:
    # A true two-degree separation must never become 358 degrees because
    # the principal branch changes sign at +/-pi.
    a = cmath.rect(1.0, math.radians(179.0))
    b = cmath.rect(2.0, math.radians(-179.0))
    delta = wrapped_phase_separation_deg(a, b)
    assert delta == pytest.approx(2.0)
    assert classify_dense_bin_cause(
        reference_magnitude=1.0, magnitude_floor=0.01,
        phase_delta_deg=delta, is_local_magnitude_max=False,
        phase_wrap_deg=150.0,
    ) == 'UNCLASSIFIED'
    assert wrapped_phase_separation_deg(b, a) == pytest.approx(2.0)
    assert wrapped_phase_separation_deg(1 + 0j, -1 + 0j) == pytest.approx(180.0)
    assert wrapped_phase_separation_deg(
        cmath.rect(1.0, math.radians(100)),
        cmath.rect(1.0, math.radians(-100)),
    ) == pytest.approx(160.0)


@pytest.mark.parametrize('invalid', [0j, complex(float('nan'), 1), complex(float('inf'), 0)])
def test_wrapped_phase_difference_rejects_invalid_samples(invalid: complex) -> None:
    with pytest.raises(ValueError, match='phase separation'):
        wrapped_phase_separation_deg(1 + 0j, invalid)


def test_classify_dense_bin_cause_labels() -> None:
    assert (
        classify_dense_bin_cause(
            reference_magnitude=0.001,
            magnitude_floor=0.01,
            phase_delta_deg=10.0,
            is_local_magnitude_max=False,
            phase_wrap_deg=150.0,
        )
        == 'NEAR_NULL'
    )
    assert (
        classify_dense_bin_cause(
            reference_magnitude=1.0,
            magnitude_floor=0.01,
            phase_delta_deg=160.0,
            is_local_magnitude_max=False,
            phase_wrap_deg=150.0,
        )
        == 'PHASE_WRAP_CANDIDATE'
    )
    assert (
        classify_dense_bin_cause(
            reference_magnitude=1.0,
            magnitude_floor=0.01,
            phase_delta_deg=10.0,
            is_local_magnitude_max=True,
            phase_wrap_deg=150.0,
        )
        == 'NEAR_RESONANCE'
    )
    assert (
        classify_dense_bin_cause(
            reference_magnitude=1.0,
            magnitude_floor=0.01,
            phase_delta_deg=10.0,
            is_local_magnitude_max=False,
            phase_wrap_deg=150.0,
        )
        == 'UNCLASSIFIED'
    )


def test_hypothesis_table_statuses() -> None:
    axis_verdicts = {
        'record_prefix_identity': 'RECORD_PREFIX_IDENTICAL',
        'analytic_rigid_box': 'ANALYTIC_MODAL_PEAKS_OUTSIDE_TOLERANCE',
        'boundary_halo_separation': 'HALO_ADJACENCY_ABSORBS_CONFIRMED',
        'cfl_dt_variants': 'CFL_DT_WORSENING_PERSISTS',
        'phase_floor_rescore': 'PHASE_NONMONOTONIC_UNDER_TIGHTER_FLOOR',
        'worsening_cause_separation': 'WORSENING_BINS_CLASSIFIED',
    }
    table = build_reproduction_hypothesis_table(
        axis_verdicts, mfem_executed=False
    )
    statuses = {row['hypothesis']: row['status'] for row in table}
    assert statuses['stencil_dispersion'] == 'SUPPORTED'
    assert statuses['fem_conditioning_pollution'] == (
        'UNRESOLVED_ENVIRONMENT_BLOCKED'
    )
    assert all(row['status'] != 'FAIL' for row in table)


def test_classify_reproduction_isolation_state_machine() -> None:
    clean = {'axis_a': 'PASS'}
    blocked = {'axis_a': 'UNRESOLVED_ENVIRONMENT_BLOCKED'}
    assert (
        classify_reproduction_isolation(
            run25_match=True,
            run62_match=True,
            run76_identical=True,
            axis_verdicts=clean,
        )
        == 'REPRODUCED_AND_ISOLATED'
    )
    assert (
        classify_reproduction_isolation(
            run25_match=True,
            run62_match=True,
            run76_identical=True,
            axis_verdicts=blocked,
        )
        == 'REPRODUCED_PARTIAL_ENVIRONMENT_BLOCKED'
    )
    assert (
        classify_reproduction_isolation(
            run25_match=False,
            run62_match=True,
            run76_identical=True,
            axis_verdicts=clean,
        )
        == 'REPRODUCTION_FAILED_VALUE_MISMATCH'
    )
