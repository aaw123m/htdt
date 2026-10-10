#!/usr/bin/env python3
"""R130D time-gate localization diagnostic — PFFDTD leg.

Executes the frozen causal time-gate sweep declared by
``benchmarks/acoustics/r130d_time_gate_localization_diagnostic_plan.json``:

* re-executes the three pinned PFFDTD PPW levels under the frozen solver
  contract and binds the canonical full-record cell to the persisted run-76
  records by the frozen semantic sha256 pins (``run76_record_binding``);
* applies the frozen set of predeclared causal time gates (binary half-open
  interval masks on the bound raw pressure record; the unit-impulse source
  spectrum is never gated) and re-derives the finite-record transfer per gate
  on the unchanged 34-frequency dense grid with the same metric, pairs and
  thresholds;
* verifies the frozen additive partition identity (prefix_50ms +
  band_mid_50_150ms + band_late_150_250ms == full_record within 1e-12) per
  level as a fail-closed contract check;
* classifies whether the dense sub-band worsening is carried by early
  staircase-mediated record energy or is broadband across the record;
* writes a compact committed-evidence JSON plus a summary JSON.

Diagnostic-only: no solver executions beyond the canonical binding legs, no
new solver state is produced, no canonical solver output is changed, and the
diagnostic cannot promote general-3D validation. Canonical reproduction is
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

from htdt.acoustic_pffdtd_adapter import (
    finite_record_pressure_transfer as pffdtd_finite_record_pressure_transfer,
    pffdtd_velocity_potential_to_pressure_trace,
    recombine_pffdtd_receiver_traces,
)
from htdt.acoustic_pffdtd_polyhedral_geometry import register_r120b_polyhedral_authorities
from htdt.r130d_general3d_validation import (
    TIME_GATE_CELL_IDS,
    TIME_GATE_CANONICAL_CELL_ID,
    TIME_GATE_PARTITION_CELL_IDS,
    TIME_GATE_PARTITION_MAX_ABS_TOLERANCE,
    apply_time_gate,
    classify_dense_frequency_neighborhood,
    classify_time_gate_localization,
    dense_frequency_grid,
    load_dense_frequency_diagnostic_plan,
    load_spatial_representation_diagnostic_plan,
    load_stencil_sensitivity_diagnostic_plan,
    load_target_window_diagnostic_plan,
    load_time_gate_localization_diagnostic_plan,
    load_validation_plan,
    load_voxel_staircase_sensitivity_diagnostic_plan,
    normalized_complex_difference,
    semantic_hash,
    validate_dense_frequency_diagnostic_binding,
    validate_exact_binding,
    validate_physical_observable_contract,
    validate_refinement_schedule,
    validate_spatial_representation_diagnostic_binding,
    validate_stencil_sensitivity_diagnostic_binding,
    validate_time_gate_localization_diagnostic_binding,
    validate_voxel_staircase_sensitivity_diagnostic_binding,
)
from htdt.acoustics.services.acoustic_pffdtd_polyhedral_executor import PffdtdPolyhedralCandidateWaveExecutor


EVIDENCE_SCHEMA = (
    'htdt.r130d.time-gate-localization-diagnostic-committed-evidence-1'
)
SUMMARY_SCHEMA = (
    'htdt.r130d.time-gate-localization-diagnostic-committed-summary-1'
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Run the frozen R130D time-gate localization diagnostic'
    )
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--diagnostic-plan', required=True, type=Path)
    parser.add_argument('--spatial-diagnostic-plan', required=True, type=Path)
    parser.add_argument('--dense-diagnostic-plan', required=True, type=Path)
    parser.add_argument('--stencil-diagnostic-plan', required=True, type=Path)
    parser.add_argument('--voxel-diagnostic-plan', required=True, type=Path)
    parser.add_argument('--time-gate-diagnostic-plan', required=True, type=Path)
    parser.add_argument('--pr295-summary', required=True, type=Path)
    parser.add_argument('--pffdtd-root', required=True, type=Path)
    parser.add_argument('--work-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--summary-output', type=Path)
    return parser


def _complex_pairs(values: np.ndarray) -> list[list[float]]:
    return [[float(v.real), float(v.imag)] for v in np.asarray(values).flat]


def _load_canonical_leg_assets(sim_dir: Path) -> dict[str, Any]:
    import h5py

    raw_output_path = sim_dir / 'sim_outs.h5'
    comms_path = sim_dir / 'comms_out.h5'
    consts_path = sim_dir / 'sim_consts.h5'
    if not all(
        path.is_file()
        for path in (raw_output_path, comms_path, consts_path)
    ):
        raise ValidationBlocked(
            f'PFFDTD canonical sim assets are missing under {sim_dir}'
        )
    with h5py.File(raw_output_path, 'r') as handle:
        u_out = np.asarray(handle['u_out'][...], dtype=np.float64)
    with h5py.File(comms_path, 'r') as handle:
        out_alpha = np.asarray(handle['out_alpha'][...], dtype=np.float64)
        nt = int(handle['Nt'][()])
    with h5py.File(consts_path, 'r') as handle:
        time_step_s = float(handle['Ts'][()])
    return {
        'u_out': u_out,
        'out_alpha': out_alpha,
        'nt': nt,
        'time_step_s': time_step_s,
    }


def _bound_pressure_record(
    *,
    u_out: np.ndarray,
    receiver_weights: np.ndarray,
    time_step_s: float,
    nt: int,
    density_kg_m3: float,
) -> np.ndarray:
    receiver_potential = recombine_pffdtd_receiver_traces(
        u_out,
        np.asarray([receiver_weights], dtype=np.float64),
        receiver_count=1,
        nt=nt,
    )[0]
    return pffdtd_velocity_potential_to_pressure_trace(
        receiver_potential,
        time_step_s=time_step_s,
        density_kg_m3=density_kg_m3,
    )


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
    stencil_diagnostic = load_stencil_sensitivity_diagnostic_plan(
        args.stencil_diagnostic_plan
    )
    validate_stencil_sensitivity_diagnostic_binding(
        plan, stencil_diagnostic, dense_diagnostic
    )
    voxel_diagnostic = load_voxel_staircase_sensitivity_diagnostic_plan(
        args.voxel_diagnostic_plan
    )
    validate_voxel_staircase_sensitivity_diagnostic_binding(
        plan, voxel_diagnostic, dense_diagnostic
    )
    time_gate_diagnostic = load_time_gate_localization_diagnostic_plan(
        args.time_gate_diagnostic_plan
    )
    validate_time_gate_localization_diagnostic_binding(
        plan, time_gate_diagnostic, dense_diagnostic
    )
    if semantic_hash(stencil_diagnostic) != voxel_diagnostic[
        'parent_stencil_sensitivity_diagnostic'
    ]['semantic_sha256']:
        raise ValidationBlocked(
            'bound stencil diagnostic file does not match the voxel plan pin'
        )
    if semantic_hash(voxel_diagnostic) != time_gate_diagnostic[
        'parent_voxel_staircase_sensitivity_diagnostic'
    ]['semantic_sha256']:
        raise ValidationBlocked(
            'bound voxel diagnostic file does not match the time-gate plan pin'
        )

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

    time_gate_block = time_gate_diagnostic['time_gate_localization']
    canonical_reproduction = _validate_pr295_canonical_reproduction(
        args.pr295_summary,
        reference_levels=[],
        pffdtd_levels=pffdtd_levels,
        max_abs_tolerance=float(
            time_gate_diagnostic['canonical_reproduction'][
                'max_abs_complex_component_tolerance'
            ]
        ),
    )

    dense_block = dense_diagnostic['dense_frequency_neighborhood']
    dense_frequency_hz = tuple(
        float(x) for x in dense_block['diagnostic_frequency_hz']
    )
    dense_frequencies = np.asarray(dense_frequency_hz, dtype=np.float64)
    fixed_floor = float(dense_block['fixed_floor'])
    gates = list(time_gate_block['gates'])
    gate_intervals = {
        str(item['gate_id']): [float(x) for x in item['interval_s']]
        for item in gates
    }
    gate_rules = {str(item['gate_id']): str(item['rule']) for item in gates}
    gate_kinds = {str(item['gate_id']): str(item['kind']) for item in gates}
    cell_ids = [str(v) for v in time_gate_block['cell_axis']['cells']]
    if tuple(cell_ids) != TIME_GATE_CELL_IDS:
        raise ValidationBlocked(
            'time-gate cell axis does not match the frozen set'
        )
    canonical_cell_id = time_gate_block['cell_axis']['canonical_cell_id']
    if canonical_cell_id != TIME_GATE_CANONICAL_CELL_ID:
        raise ValidationBlocked('time-gate canonical cell id is not frozen')

    # Read back the canonical executed assets per level and re-derive the bound
    # raw pressure record; the record must reproduce the pinned run-76
    # pressure/source digests before any gated evaluation.
    run76_binding = time_gate_diagnostic['run76_record_binding']
    identical_label = run76_binding['identical_label']
    pinned_by_ppw = {
        float(item['points_per_wavelength']): item
        for item in run76_binding['levels']
    }
    density = float(plan.fixture.density_kg_m3)
    bound_records: dict[float, dict[str, Any]] = {}
    for level in pffdtd_levels:
        ppw = float(level['points_per_wavelength'])
        sim_dir = (
            executor.base_executor.work_root
            / str(level['candidate_execution_input_sha256'])
            / 'sim'
        )
        assets = _load_canonical_leg_assets(sim_dir)
        if assets['nt'] != int(level['time_step_count']):
            raise ValidationBlocked(
                'canonical comms Nt differs from execution provenance'
            )
        if abs(assets['time_step_s'] - float(level['time_step_s'])) > 1.0e-15:
            raise ValidationBlocked(
                'canonical consts Ts differs from execution provenance'
            )
        pressure_trace = _bound_pressure_record(
            u_out=assets['u_out'],
            receiver_weights=assets['out_alpha'][0],
            time_step_s=assets['time_step_s'],
            nt=assets['nt'],
            density_kg_m3=density,
        )
        pressure_sha256 = semantic_hash([float(x) for x in pressure_trace])
        source_trace = np.zeros(pressure_trace.size, dtype=np.float64)
        source_trace[0] = 1.0
        source_sha256 = semantic_hash([float(x) for x in source_trace])
        pinned = pinned_by_ppw[ppw]
        if (
            pressure_sha256 != str(pinned['pressure_trace_sha256'])
            or pressure_sha256
            != str(level['diagnostic_raw_trace']['pressure_trace_sha256'])
        ):
            raise ValidationBlocked(
                f're-derived pressure record at PPW {ppw:g} does not '
                'reproduce the pinned run-76 pressure digest'
            )
        if (
            source_sha256 != str(pinned['source_trace_sha256'])
            or source_sha256
            != str(level['diagnostic_raw_trace']['source_trace_sha256'])
        ):
            raise ValidationBlocked(
                f'unit-impulse source record at PPW {ppw:g} does not '
                'reproduce the pinned run-76 source digest'
            )
        bound_records[ppw] = {
            'pressure_trace': pressure_trace,
            'source_trace': source_trace,
            'pressure_trace_sha256': pressure_sha256,
            'source_trace_sha256': source_sha256,
            'time_step_s': assets['time_step_s'],
            'nt': assets['nt'],
        }

    record_binding_state = (
        identical_label
        if all(
            level['run76_trace_binding'] == identical_label
            for level in pffdtd_levels
        )
        else run76_binding['nonidentical_label']
    )

    # Evaluate every frozen gate cell on the dense grid.
    cell_results: list[dict[str, Any]] = []
    for cell_id in cell_ids:
        is_canonical = cell_id == canonical_cell_id
        level_records: list[dict[str, Any]] = []
        transfer_by_ppw: dict[float, np.ndarray] = {}
        for level in pffdtd_levels:
            ppw = float(level['points_per_wavelength'])
            bound = bound_records[ppw]
            if is_canonical:
                gated = bound['pressure_trace']
            else:
                gated = apply_time_gate(
                    bound['pressure_trace'],
                    time_step_s=bound['time_step_s'],
                    interval_s=gate_intervals[cell_id],
                )
            transfer = pffdtd_finite_record_pressure_transfer(
                gated,
                bound['source_trace'],
                time_step_s=bound['time_step_s'],
                frequency_hz=dense_frequencies,
            )
            transfer_by_ppw[ppw] = transfer
            level_record: dict[str, Any] = {
                'points_per_wavelength': ppw,
                'transfer_pa_per_m3_s': _complex_pairs(transfer),
                'transfer_sha256': semantic_hash(_complex_pairs(transfer)),
                'gated_pressure_trace_sha256': semantic_hash(
                    [float(x) for x in gated]
                ),
            }
            if is_canonical:
                level_record['run76_trace_binding'] = level[
                    'run76_trace_binding'
                ]
            level_records.append(level_record)
        d_8_10 = [
            normalized_complex_difference(
                transfer_by_ppw[8.0][index],
                transfer_by_ppw[10.0][index],
                fixed_floor=fixed_floor,
            )
            for index in range(len(dense_frequency_hz))
        ]
        d_10_12 = [
            normalized_complex_difference(
                transfer_by_ppw[10.0][index],
                transfer_by_ppw[12.0][index],
                fixed_floor=fixed_floor,
            )
            for index in range(len(dense_frequency_hz))
        ]
        dense_classification = classify_dense_frequency_neighborhood(
            d_8_10, d_10_12
        )
        cell_results.append(
            {
                'cell_id': cell_id,
                'gate_id': cell_id,
                'gate_kind': gate_kinds[cell_id],
                'gate_interval_s': gate_intervals[cell_id],
                'gate_rule': gate_rules[cell_id],
                'is_canonical_cell': is_canonical,
                'levels': level_records,
                'd_8_10': d_8_10,
                'd_10_12': d_10_12,
                'per_frequency': [
                    {
                        'frequency_hz': frequency,
                        'd_8_10': d_8_10[index],
                        'd_10_12': d_10_12[index],
                        'worsening': bool(
                            d_10_12[index] > d_8_10[index]
                        ),
                    }
                    for index, frequency in enumerate(dense_frequency_hz)
                ],
                **dense_classification,
            }
        )

    canonical_cell = next(
        cell for cell in cell_results if cell['is_canonical_cell']
    )
    noncanonical_cells = [
        cell for cell in cell_results if not cell['is_canonical_cell']
    ]
    for cell in noncanonical_cells:
        cell['hamming_distance_to_canonical'] = int(
            sum(
                bool(a) != bool(b)
                for a, b in zip(
                    cell['worsening_by_frequency'],
                    canonical_cell['worsening_by_frequency'],
                )
            )
        )
    canonical_cell['hamming_distance_to_canonical'] = 0

    # Frozen additive partition identity: the three disjoint partition gates
    # must decompose the control transfer at every level.
    transfer_lookup = {
        (cell['cell_id'], level_record['points_per_wavelength']): np.asarray(
            [
                complex(float(pair[0]), float(pair[1]))
                for pair in level_record['transfer_pa_per_m3_s']
            ],
            dtype=np.complex128,
        )
        for cell in cell_results
        for level_record in cell['levels']
    }
    partition_identity: list[dict[str, Any]] = []
    for level in pffdtd_levels:
        ppw = float(level['points_per_wavelength'])
        partition_sum = sum(
            transfer_lookup[(part_id, ppw)]
            for part_id in TIME_GATE_PARTITION_CELL_IDS
        )
        full = transfer_lookup[(TIME_GATE_CANONICAL_CELL_ID, ppw)]
        max_abs_deviation = float(np.max(np.abs(partition_sum - full)))
        partition_identity.append(
            {
                'points_per_wavelength': ppw,
                'partition_cells': list(TIME_GATE_PARTITION_CELL_IDS),
                'max_abs_complex_component_deviation': max_abs_deviation,
                'tolerance': TIME_GATE_PARTITION_MAX_ABS_TOLERANCE,
                'identity_holds': bool(
                    max_abs_deviation <= TIME_GATE_PARTITION_MAX_ABS_TOLERANCE
                ),
            }
        )
    if not all(item['identity_holds'] for item in partition_identity):
        raise ValidationBlocked(
            'time-gate partition identity failed: disjoint gates do not '
            'decompose the control transfer within 1e-12'
        )

    pattern_classification = classify_time_gate_localization(
        canonical_worsening_by_frequency=canonical_cell[
            'worsening_by_frequency'
        ],
        canonical_classification=canonical_cell['classification'],
        noncanonical_cells=[
            {
                'cell_id': cell['cell_id'],
                'worsening_by_frequency': cell['worsening_by_frequency'],
                'classification': cell['classification'],
            }
            for cell in noncanonical_cells
        ],
    )
    evaluation_state = 'EVALUATED'
    if record_binding_state != identical_label:
        evaluation_state = 'NOT_EVALUATED_RECORD_BINDING_MISMATCH'
        pattern_classification = {
            **pattern_classification,
            'classification': time_gate_block['evaluation']['classification'][
                'not_evaluated'
            ],
        }

    provenance = [
        {
            'ppw': level['points_per_wavelength'],
            'pressure_trace_sha256': bound_records[
                float(level['points_per_wavelength'])
            ]['pressure_trace_sha256'],
            'source_trace_sha256': bound_records[
                float(level['points_per_wavelength'])
            ]['source_trace_sha256'],
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
        'dense_diagnostic_sha256': semantic_hash(dense_diagnostic),
        'stencil_diagnostic_id': stencil_diagnostic['diagnostic_id'],
        'stencil_diagnostic_sha256': semantic_hash(stencil_diagnostic),
        'voxel_staircase_diagnostic_id': voxel_diagnostic['diagnostic_id'],
        'voxel_staircase_diagnostic_sha256': semantic_hash(voxel_diagnostic),
        'time_gate_diagnostic_id': time_gate_diagnostic['diagnostic_id'],
        'time_gate_diagnostic_sha256': semantic_hash(time_gate_diagnostic),
        'pffdtd_source_commit_sha': plan.pffdtd.source_commit_sha,
        'ppw': [8.0, 10.0, 12.0],
    }
    time_gate_result = {
        'axis': time_gate_block['axis'],
        'gating_convention': time_gate_block['gating_convention'],
        'canonical_cell_id': canonical_cell_id,
        'frequency_hz': list(dense_frequency_hz),
        'normalized_complex_difference_formula': dense_block[
            'normalized_complex_difference_formula'
        ],
        'fixed_floor': fixed_floor,
        'pairs': list(dense_block['pairs']),
        'gates': [
            {
                'gate_id': str(item['gate_id']),
                'kind': str(item['kind']),
                'interval_s': [float(x) for x in item['interval_s']],
                'control': bool(item.get('control', False)),
                'rule': str(item['rule']),
            }
            for item in gates
        ],
        'cells': cell_results,
        'partition_identity': partition_identity,
        'run76_record_binding': record_binding_state,
        'evaluation_state': evaluation_state,
        'pattern_classification': pattern_classification,
        'diagnostic_only': True,
        'canonical_acceptance_inclusion': False,
    }
    decision = {
        'diagnostic_only': True,
        'canonical_solver_execution_unchanged': True,
        'canonical_pr295_reproduction': canonical_reproduction['state'],
        'canonical_self_convergence_unchanged': True,
        'cross_solver_unblocked_by_diagnostic': False,
        'general_3d_validation_promoted_by_diagnostic': False,
        'run76_record_binding': record_binding_state,
        'time_gate_worsening_localization': pattern_classification[
            'classification'
        ],
    }

    evidence = {
        'schema_version': EVIDENCE_SCHEMA,
        'task_start_main_sha': time_gate_diagnostic['task_start_main_sha'],
        'repository_head': repository_head,
        'authoritative_run': {
            'runner': 'local',
            'note': (
                'executed on a REV39-R130D4 Devin VM with the pinned '
                'numpy/scipy/numba runtime; canonical-cell raw records bound '
                'bit-identical to authoritative run #76 before evaluation; '
                'every gate cell is a pure re-evaluation of the bound records '
                'with no solver re-execution beyond the canonical binding legs'
            ),
        },
        'frozen_authority': frozen_authority,
        'canonical_pr295_reproduction': canonical_reproduction,
        'time_gate_localization': time_gate_result,
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
            'time_gate_localization': {
                'canonical_cell_id': canonical_cell_id,
                'cells': [
                    {
                        'cell_id': cell['cell_id'],
                        'gate_kind': cell['gate_kind'],
                        'worsening_count': cell['worsening_count'],
                        'classification': cell['classification'],
                        'hamming_distance_to_canonical': cell[
                            'hamming_distance_to_canonical'
                        ],
                    }
                    for cell in cell_results
                ],
                'partition_identity': partition_identity,
                'run76_record_binding': record_binding_state,
                'evaluation_state': evaluation_state,
                'pattern_classification': pattern_classification,
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
