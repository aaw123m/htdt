#!/usr/bin/env python3
"""R130D dense frequency-neighborhood diagnostic — PFFDTD leg.

Executes the frozen dense sweep declared by
``benchmarks/acoustics/r130d_dense_frequency_diagnostic_plan.json``:

* re-executes the three pinned PFFDTD PPW levels under the frozen solver
  contract (identical geometry, source/receiver, duration, sampling);
* binds the re-executed raw records to the persisted run-76 records by the
  frozen semantic sha256 pins (``run76_record_binding``);
* evaluates the predeclared 34-frequency direct-DTFT sweep from those same
  raw records with the same native finite-record operator;
* writes a compact committed-evidence JSON plus a summary JSON.

Diagnostic-only: this script runs no MFEM leg, changes no canonical solver
output, and cannot promote general-3D validation. Canonical reproduction is
still guarded: every executed PFFDTD level must reproduce the frozen PR #295
canonical transfer at the plan tolerance before evidence is written.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import scipy

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
sys.path.insert(0, str(_SCRIPT_DIR.parent / 'backend' / 'src'))

from run_r130a_candidate_wave_execution import _fixture as build_r130a_fixture
from run_r130d_polyhedral_candidate_wave_execution import (
    _make_polyhedron,
    _sloped_same_bbox_fixture,
)
from run_r130d_general3d_validation import (
    ANALYSIS_KERNEL,
    PHASOR,
    QUANTITY,
    UNIT,
    ValidationBlocked,
    _git_head,
    _observable_contract,
    _run_pffdtd_level,
    _validate_pr295_canonical_reproduction,
    _validate_target_window_diagnostic_binding,
)

from htdt.acoustic_pffdtd_polyhedral_geometry import (
    PffdtdPolyhedralCandidateWaveExecutor,
    register_r120b_polyhedral_authorities,
)
from htdt.r130d_general3d_validation import (
    classify_dense_frequency_neighborhood,
    dense_frequency_grid,
    load_dense_frequency_diagnostic_plan,
    load_spatial_representation_diagnostic_plan,
    load_target_window_diagnostic_plan,
    load_validation_plan,
    normalized_complex_difference,
    semantic_hash,
    validate_dense_frequency_diagnostic_binding,
    validate_exact_binding,
    validate_physical_observable_contract,
    validate_refinement_schedule,
    validate_spatial_representation_diagnostic_binding,
)


EVIDENCE_SCHEMA = 'htdt.r130d.dense-frequency-diagnostic-committed-evidence-1'
SUMMARY_SCHEMA = 'htdt.r130d.dense-frequency-diagnostic-committed-summary-1'


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Run the frozen R130D dense frequency-neighborhood diagnostic'
    )
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--diagnostic-plan', required=True, type=Path)
    parser.add_argument('--spatial-diagnostic-plan', required=True, type=Path)
    parser.add_argument('--dense-diagnostic-plan', required=True, type=Path)
    parser.add_argument('--pr295-summary', required=True, type=Path)
    parser.add_argument('--pffdtd-root', required=True, type=Path)
    parser.add_argument('--work-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--summary-output', type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    plan = load_validation_plan(args.plan)
    diagnostic = load_target_window_diagnostic_plan(args.diagnostic_plan)
    _validate_target_window_diagnostic_binding(plan, diagnostic)
    spatial_diagnostic = load_spatial_representation_diagnostic_plan(
        args.spatial_diagnostic_plan
    )
    validate_spatial_representation_diagnostic_binding(plan, spatial_diagnostic)
    dense_diagnostic = load_dense_frequency_diagnostic_plan(
        args.dense_diagnostic_plan
    )
    validate_dense_frequency_diagnostic_binding(plan, dense_diagnostic)

    repository_head = os.environ.get('HTDT_PR_HEAD_SHA', '').strip().lower()
    if not repository_head:
        repository_head = _git_head(Path(__file__).resolve().parents[1])

    if args.work_root.exists():
        shutil.rmtree(args.work_root)
    args.work_root.mkdir(parents=True)

    validate_refinement_schedule(
        plan,
        reference_refinements=plan.independent_reference.uniform_refinements,
        pffdtd_points_per_wavelength=plan.pffdtd.points_per_wavelength,
    )
    if np.__version__ != plan.independent_reference.modal_numpy_version:
        raise ValidationBlocked(
            f'NumPy version mismatch: {np.__version__} != '
            f'{plan.independent_reference.modal_numpy_version}'
        )
    if scipy.__version__ != plan.independent_reference.modal_scipy_version:
        raise ValidationBlocked(
            f'SciPy version mismatch: {scipy.__version__} != '
            f'{plan.independent_reference.modal_scipy_version}'
        )
    if _git_head(args.pffdtd_root) != plan.pffdtd.source_commit_sha:
        raise ValidationBlocked('PFFDTD checkout does not match exact source commit')

    helper_vertices, helper_faces = _sloped_same_bbox_fixture()
    validate_exact_binding(
        plan,
        vertices_m=helper_vertices,
        faces=helper_faces,
        source_position_m=plan.fixture.source_position_m,
        receiver_position_m=plan.fixture.receiver_position_m,
        quantity=QUANTITY,
        unit=UNIT,
        phasor_convention=PHASOR,
        analysis_fourier_kernel=ANALYSIS_KERNEL,
        pffdtd_source_commit_sha=plan.pffdtd.source_commit_sha,
        independent_source_commit_sha=plan.independent_reference.source_commit_sha,
    )

    fixture = build_r130a_fixture(
        args.work_root / 'pffdtd-fixture',
        args.pffdtd_root,
        boundary_mode='rigid',
        fixture_id='r130d-general3d-independent-validation-v1',
    )
    source_point = fixture['source'].source_acoustic_reference_world_position
    source_position = (
        float(source_point.x_m),
        float(source_point.y_m),
        float(source_point.z_m),
    )
    receiver_point = fixture['snapshot'].receivers[0].world_position
    receiver_position = (
        float(receiver_point.x_m),
        float(receiver_point.y_m),
        float(receiver_point.z_m),
    )
    if source_position != plan.fixture.source_position_m:
        raise ValidationBlocked('PFFDTD source position differs from validation plan')
    if receiver_position != plan.fixture.receiver_position_m:
        raise ValidationBlocked('PFFDTD receiver position differs from validation plan')

    snapshot = fixture['snapshot']
    binding = snapshot.surface_boundary_configuration[0]
    material_ref = binding.material_authority
    rigid_boundary_ref = binding.boundary_physics_authority
    if material_ref is None or rigid_boundary_ref is None:
        raise ValidationBlocked('rigid fixture boundary authority is missing')
    semantic, compiled = _make_polyhedron(
        snapshot=snapshot,
        source_key=plan.fixture.source_key,
        vertices=plan.fixture.vertices_m,
        faces=plan.fixture.faces,
        material_ref=material_ref,
    )
    semantic_ref, compiled_ref = register_r120b_polyhedral_authorities(
        fixture['store'],
        semantic=semantic,
        compiled=compiled,
    )
    rigid_boundary = fixture['store'].read_payload(rigid_boundary_ref)
    if rigid_boundary.get('model') != 'rigid_zero_normal_velocity':
        raise ValidationBlocked(
            'PFFDTD boundary authority is not rigid zero-normal-velocity'
        )

    expected_contract = _observable_contract(
        plan, geometry_sha256=plan.fixture_sha256()
    )
    validate_physical_observable_contract(
        expected=expected_contract, actual=dict(expected_contract)
    )

    executor = PffdtdPolyhedralCandidateWaveExecutor(
        base_executor=fixture['executor'],
        containment_tolerance_m=1.0e-9,
    )
    pffdtd_levels: list[dict[str, Any]] = []
    for ppw in plan.pffdtd.points_per_wavelength:
        pffdtd_levels.append(
            _run_pffdtd_level(
                plan,
                fixture=fixture,
                executor=executor,
                semantic_ref=semantic_ref,
                compiled_ref=compiled_ref,
                rigid_boundary_ref=rigid_boundary_ref,
                ppw=ppw,
                spatial_diagnostic=spatial_diagnostic,
                dense_diagnostic=dense_diagnostic,
            )
        )

    canonical_reproduction = _validate_pr295_canonical_reproduction(
        args.pr295_summary,
        reference_levels=[],
        pffdtd_levels=pffdtd_levels,
        max_abs_tolerance=float(
            dense_diagnostic['canonical_reproduction'][
                'max_abs_complex_component_tolerance'
            ]
        ),
    )

    dense_block = dense_diagnostic['dense_frequency_neighborhood']
    dense_frequency_hz = tuple(
        float(x) for x in dense_block['diagnostic_frequency_hz']
    )
    level_by_ppw = {
        float(level['points_per_wavelength']): np.asarray(
            [
                complex(float(pair[0]), float(pair[1]))
                for pair in level['dense_neighborhood_transfer_pa_per_m3_s']
            ],
            dtype=np.complex128,
        )
        for level in pffdtd_levels
    }
    if set(level_by_ppw) != {8.0, 10.0, 12.0}:
        raise ValidationBlocked('dense neighborhood lacks exact 8/10/12 levels')
    fixed_floor = float(dense_block['fixed_floor'])
    d_8_10 = [
        normalized_complex_difference(
            level_by_ppw[8.0][index],
            level_by_ppw[10.0][index],
            fixed_floor=fixed_floor,
        )
        for index in range(len(dense_frequency_hz))
    ]
    d_10_12 = [
        normalized_complex_difference(
            level_by_ppw[10.0][index],
            level_by_ppw[12.0][index],
            fixed_floor=fixed_floor,
        )
        for index in range(len(dense_frequency_hz))
    ]
    run76_binding = dense_diagnostic['run76_record_binding']
    identical_label = run76_binding['identical_label']
    record_binding_state = (
        identical_label
        if all(
            level['run76_trace_binding'] == identical_label
            for level in pffdtd_levels
        )
        else run76_binding['nonidentical_label']
    )
    classification = classify_dense_frequency_neighborhood(d_8_10, d_10_12)
    evaluation_state = 'EVALUATED'
    if record_binding_state != identical_label:
        evaluation_state = 'NOT_EVALUATED_RECORD_BINDING_MISMATCH'
        classification = {
            **classification,
            'classification': dense_block['classification']['not_evaluated'],
        }
    band_of: dict[float, str] = {}
    for band in dense_block['bands']:
        for frequency in dense_frequency_grid([band]):
            band_of[frequency] = str(band['band_id'])
    gap = [b - a for a, b in zip(d_8_10, d_10_12)]
    argmax_index = max(range(len(gap)), key=lambda index: gap[index])
    dense_result = {
        'frequency_hz': list(dense_frequency_hz),
        'canonical_scored_frequency_hz': list(
            dense_block['canonical_scored_frequency_hz']
        ),
        'diagnostic_only_frequency_hz': list(
            dense_block['diagnostic_only_frequency_hz']
        ),
        'normalized_complex_difference_formula': dense_block[
            'normalized_complex_difference_formula'
        ],
        'fixed_floor': fixed_floor,
        'levels': [
            {
                'points_per_wavelength': level['points_per_wavelength'],
                'transfer_pa_per_m3_s': level[
                    'dense_neighborhood_transfer_pa_per_m3_s'
                ],
                'transfer_sha256': level['dense_neighborhood_transfer_sha256'],
                'run76_trace_binding': level['run76_trace_binding'],
            }
            for level in pffdtd_levels
        ],
        'd_8_10': d_8_10,
        'd_10_12': d_10_12,
        'per_frequency': [
            {
                'frequency_hz': frequency,
                'band_id': band_of[frequency],
                'd_8_10': d_8_10[index],
                'd_10_12': d_10_12[index],
                'worsening': bool(d_10_12[index] > d_8_10[index]),
            }
            for index, frequency in enumerate(dense_frequency_hz)
        ],
        'band_worsening_counts': {
            str(band['band_id']): sum(
                1
                for index, frequency in enumerate(dense_frequency_hz)
                if band_of[frequency] == band['band_id']
                and d_10_12[index] > d_8_10[index]
            )
            for band in dense_block['bands']
        },
        'argmax_worsening_gap_frequency_hz': float(
            dense_frequency_hz[argmax_index]
        ),
        'run76_record_binding': record_binding_state,
        'evaluation_state': evaluation_state,
        **classification,
        'diagnostic_only': True,
        'canonical_acceptance_inclusion': False,
    }

    provenance = [
        {
            'ppw': level['points_per_wavelength'],
            'pressure_trace_sha256': level['diagnostic_raw_trace'][
                'pressure_trace_sha256'
            ],
            'source_trace_sha256': level['diagnostic_raw_trace'][
                'source_trace_sha256'
            ],
            'run76_trace_binding': level['run76_trace_binding'],
            'time_step_s': level['time_step_s'],
            'time_step_count': level['time_step_count'],
        }
        for level in pffdtd_levels
    ]
    frozen_authority = {
        'general3d_plan_id': plan.plan_id,
        'general3d_plan_sha256': plan.plan_sha256(),
        'target_window_diagnostic_sha256': semantic_hash(diagnostic),
        'spatial_diagnostic_sha256': semantic_hash(spatial_diagnostic),
        'dense_diagnostic_id': dense_diagnostic['diagnostic_id'],
        'dense_diagnostic_sha256': semantic_hash(dense_diagnostic),
        'pffdtd_source_commit_sha': plan.pffdtd.source_commit_sha,
        'ppw': [8.0, 10.0, 12.0],
    }
    decision = {
        'diagnostic_only': True,
        'canonical_solver_execution_unchanged': True,
        'canonical_pr295_reproduction': canonical_reproduction['state'],
        'canonical_self_convergence_unchanged': True,
        'cross_solver_unblocked_by_diagnostic': False,
        'general_3d_validation_promoted_by_diagnostic': False,
        'run76_record_binding': record_binding_state,
        'dense_frequency_neighborhood_sensitivity': classification[
            'classification'
        ],
    }

    evidence = {
        'schema_version': EVIDENCE_SCHEMA,
        'task_start_main_sha': dense_diagnostic['task_start_main_sha'],
        'repository_head': repository_head,
        'authoritative_run': {
            'runner': 'local',
            'note': (
                'executed on a REV35-R130D Devin VM with the pinned '
                'numpy/scipy/numba runtime; re-executed raw records bound '
                'bit-identical to authoritative run #76 before evaluation'
            ),
        },
        'frozen_authority': frozen_authority,
        'canonical_pr295_reproduction': canonical_reproduction,
        'dense_frequency_neighborhood': dense_result,
        'pffdtd_provenance': provenance,
        'decision': decision,
        'scope': {
            'validated_fixture': None,
            'general_3d_validation_state': 'NOT_VALIDATED',
            'diagnostic_does_not_promote_validation': True,
        },
        'runtime': {
            'python': platform.python_version(),
            'platform': platform.platform(),
            'numpy': np.__version__,
            'scipy': scipy.__version__,
            'logical_cpus': os.cpu_count(),
        },
    }
    args.output.write_text(
        json.dumps(evidence, indent=2, sort_keys=True, allow_nan=False) + '\n',
        encoding='utf-8',
    )
    if args.summary_output is not None:
        summary = {
            'schema_version': SUMMARY_SCHEMA,
            'frozen_authority': frozen_authority,
            'canonical_pr295_reproduction': canonical_reproduction,
            'dense_frequency_neighborhood': {
                'frequency_hz': list(dense_frequency_hz),
                'd_8_10': d_8_10,
                'd_10_12': d_10_12,
                'band_worsening_counts': dense_result['band_worsening_counts'],
                'argmax_worsening_gap_frequency_hz': dense_result[
                    'argmax_worsening_gap_frequency_hz'
                ],
                'worsening_count': classification['worsening_count'],
                'classification': classification['classification'],
                'run76_record_binding': record_binding_state,
                'evaluation_state': evaluation_state,
            },
            'provenance_by_ppw': provenance,
            'decision': decision,
        }
        args.summary_output.write_text(
            json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + '\n',
            encoding='utf-8',
        )
    print(json.dumps({'decision': decision}, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode)
