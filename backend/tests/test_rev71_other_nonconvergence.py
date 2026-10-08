"""REV71 other-nonconvergence diagnostic contract tests.

The pure-Python modal reference reproduces the audited MFEM mathematics
(exact tet integration, P2 basis, Gauss-Lobatto hex assembly, modal
impulse reconstruction).  These tests pin the frozen plan, the committed
evidence verdicts, dof counts, the rigid-body mode, and assembly
invariants.  Refs #809, #938.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from htdt import nonconvergence_modal_reference as nmr

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN_PATH = (
    REPO_ROOT
    / 'benchmarks'
    / 'acoustics'
    / 'rev71_other_nonconvergence_diagnostic_plan.json'
)
EVIDENCE_PATH = (
    REPO_ROOT
    / 'benchmarks'
    / 'acoustics'
    / 'rev71_other_nonconvergence_diagnostic_evidence.json'
)
SUMMARY_PATH = (
    REPO_ROOT
    / 'benchmarks'
    / 'acoustics'
    / 'rev71_other_nonconvergence_diagnostic_summary.json'
)

EXPECTED_VERDICTS = {
    'sloped_modal_run7_replica': 'REPRODUCED_RUN7_REFERENCE_FAILURE_CLASS',
    'sloped_modal_v2_schedule': 'V2_SCHEDULE_STILL_FAILS',
    'rectangular_modal_replica': 'REPRODUCED_R100B_SPATIAL_VALUES_MATCH',
    'lroom_modal_coupled_replica': 'EXACT_EVOLUTION_STRICTLY_DECREASING',
    'lroom_modal_h_refinement': 'EXACT_EVOLUTION_NON_MONOTONE',
    'pole_localization': 'MEASURED',
    'mfem_transient_blocked_register': 'UNRESOLVED_ENVIRONMENT_BLOCKED',
}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def test_plan_freezes_audited_fixtures() -> None:
    plan = _load(PLAN_PATH)
    assert plan['schema_version'] == (
        'htdt.rev71.other-nonconvergence-diagnostic-plan-1'
    )
    assert plan['issue'] == 938
    sloped = plan['frozen_constants']['sloped_fixture']
    assert sloped['expected_dofs_per_refinement'] == [27, 125, 729, 4913]
    assert sloped['base_tetrahedra'] == [
        [0, 1, 2, 6], [0, 2, 3, 6], [0, 3, 7, 6],
        [0, 7, 4, 6], [0, 4, 5, 6], [0, 5, 1, 6],
    ]
    assert sloped['comparison_frequencies_hz'] == [40.0, 80.0]
    assert sloped['run7_executed']['pffdtd_ppw'] == [6.0, 8.0, 10.0]
    rect = plan['frozen_constants']['rectangular_fixture']
    assert rect['expected_dofs_per_refinement'] == [27, 125, 729]
    assert rect['sound_speed_m_s'] == 343.0
    lroom = plan['frozen_constants']['lroom_fixture']
    assert lroom['base_hex_cells_plan_xy'] == [
        [0, 0], [1, 0], [2, 0], [0, 1], [2, 1]
    ]
    assert lroom['expected_dofs']['h0_p2'] == 99
    assert lroom['expected_dofs']['h1_p2'] == 525


def test_evidence_and_summary_axes_consistent() -> None:
    evidence = _load(EVIDENCE_PATH)
    summary = _load(SUMMARY_PATH)
    assert evidence['axes'].keys() == summary['axis_verdicts'].keys()
    for axis, verdict in EXPECTED_VERDICTS.items():
        assert summary['axis_verdicts'][axis] == verdict, axis
    # pffdtd verdict is environment-dependent: either the pinned replay
    # matched committed values or it was honestly blocked.
    assert summary['axis_verdicts']['pffdtd_run7_replica'] in (
        'REPRODUCED_RUN7_PFFDTD_VALUES_MATCH',
        'REPRODUCED_RUN7_PFFDTD_VALUES_DIFFER',
        'UNRESOLVED_ENVIRONMENT_BLOCKED',
    )
    assert set(summary['hypothesis_verdicts']) == {
        'pole_proximity_observable',
        'voxel_staircase_geometry_drift',
        'temporal_integrator_dominates_r100b_concave',
        'non_coherent_window_contributes',
    }


def test_committed_reproduction_target_values() -> None:
    evidence = _load(EVIDENCE_PATH)
    a1 = evidence['axes']['sloped_modal_run7_replica']
    med = a1['adjacent_pairs'][-1]
    # the medium->fine pair of the modal leg must itself fail the reference
    # thresholds at O(1) magnitude — the class signature of run7.
    assert med['complex_rms_relative'] > 0.2
    assert med['phase_max_deg'] > 15.0
    b1 = evidence['axes']['rectangular_modal_replica']
    assert all(b1['matched'].values())
    b2 = evidence['axes']['lroom_modal_coupled_replica']
    adj = b2['adjacent_rel']
    assert all(adj[i] > adj[i + 1] for i in range(len(adj) - 1))


def test_sloped_tet_dof_counts_and_rigid_mode() -> None:
    plan = _load(PLAN_PATH)
    expected = plan['frozen_constants']['sloped_fixture'][
        'expected_dofs_per_refinement'
    ]
    for ref in (0, 1):
        system = nmr.assemble_sloped_tet_system(ref, [1.5, 2, 2], [2.5, 2, 2])
        assert len(system.mass) == expected[ref]
        # Natural-Neumann pure-pressure problem: exactly one rigid-body mode.
        from scipy.linalg import eigh
        eig_m = np.linalg.eigvalsh(system.mass)
        assert eig_m.min() > 0  # SPD mass
        vals = eigh(system.stiffness, system.mass, eigvals_only=True)
        n_rigid = int(np.sum(vals < 1e-8 * max(1.0, vals.max())))
        assert n_rigid == 1, vals[:5]
        # Point functionals integrate to 1 (partition of unity on the
        # containing element) — each point is counted exactly once.
        assert system.source.sum() == pytest.approx(1.0, rel=1e-12)
        assert system.receiver.sum() == pytest.approx(1.0, rel=1e-12)


def test_hex_p2_lagrange_partition_of_unity() -> None:
    pts = nmr.gauss_lobatto_points(2)
    m1, s1 = nmr._lagrange_matrix_and_derivative(pts)
    # On Gauss-Lobatto quadrature the basis masses must be exact:
    # sum of basis functions = 1 everywhere -> row sums of M = nodal weights.
    for xi in (-1.0, 0.0, 1.0):
        phi = nmr._lagrange_eval(pts, xi)
        assert phi.sum() == pytest.approx(1.0, rel=1e-14)
        dphi = nmr._lagrange_deriv_eval(pts, xi)
        assert dphi.sum() == pytest.approx(0.0, abs=1e-14)
    assert m1.diagonal().min() > 0


def test_rect_and_lroom_dof_counts() -> None:
    rect0 = nmr.assemble_hex_system(
        nmr.rectangular_room_cells(0), 2, [1, 1, 1], [5, 3, 1]
    )
    assert len(rect0.mass) == 27
    assert rect0.elements == 1
    rect1 = nmr.assemble_hex_system(
        nmr.rectangular_room_cells(1), 2, [1, 1, 1], [5, 3, 1]
    )
    assert len(rect1.mass) == 125
    lroom0 = nmr.assemble_hex_system(
        nmr.lroom_hex_cells(0), 2, [1, 1, 1], [5, 1, 1]
    )
    assert len(lroom0.mass) == 99
    assert lroom0.elements == 5
    lroom1 = nmr.assemble_hex_system(
        nmr.lroom_hex_cells(1), 2, [1, 1, 1], [5, 1, 1]
    )
    assert len(lroom1.mass) == 525
    assert lroom1.elements == 40
    # L-room point functionals: unique global count.
    assert lroom0.source.sum() == pytest.approx(1.0, rel=1e-12)
    assert lroom0.receiver.sum() == pytest.approx(1.0, rel=1e-12)


def test_modal_impulse_transfer_structure() -> None:
    system = nmr.assemble_hex_system(
        nmr.rectangular_room_cells(0), 2, [1, 1, 1], [5, 3, 1]
    )
    c = 343.0
    rec = nmr.modal_impulse_transfer(
        system.mass,
        system.stiffness * c * c,
        system.source,
        system.receiver,
        density_kg_m3=1.2,
        sound_speed_m_s=c,
        dt=1.0 / 12000.0,
        duration_s=0.02,
        frequencies_hz=[40.0, 80.0],
    )
    assert rec.transfer.shape == (2,)
    assert np.all(np.isfinite(rec.transfer))
    assert rec.modal_frequencies_hz[0] < 1e-9  # rigid-body mode
    assert rec.mass_orthonormality_max_abs < 1e-8
    assert rec.eigen_residual_relative_max < 1e-8
    # Analytic pole check: single hex p2 approximates the room's lowest
    # modes; first nonzero mode must sit near c*sqrt((pi/6)^2+(pi/4)^2+..)
    # /2pi ~ 40-90 Hz, just sanity-bound it.
    assert 30.0 < rec.modal_frequencies_hz[1] < 120.0


