#!/usr/bin/env python3
"""R130D stencil-discretization sensitivity diagnostic — PFFDTD leg.

Executes the frozen stencil-sensitivity sweep declared by
``benchmarks/acoustics/r130d_stencil_sensitivity_diagnostic_plan.json``:

* re-executes the three pinned PFFDTD PPW levels under the frozen solver
  contract and binds the canonical stencil cell to the persisted run-76
  records by the frozen semantic sha256 pins (``run76_record_binding``);
* for each frozen source-stencil weight variant, re-runs the pinned SimEngine
  on a byte-copied sim asset set with only the ``in_sigs`` weight rows
  renormalized (``in_sigs_variant[k,n] = alpha_variant[k] * sum_j
  in_sigs[j,n]`` — the total injected signal is invariant by construction and
  is sha256-verified per run);
* applies each frozen receiver-stencil weight variant post-hoc by recombining
  the executed raw ``u_out`` node traces;
* evaluates every cell of the 3x3 (source variant x receiver variant) matrix
  on the bound parent dense diagnostic's 34-frequency grid with the same
  metric, pairs and thresholds, and classifies whether the sub-band worsening
  pattern moves;
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
    _restore_pinned_pffdtd_checkout,
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
    apply_pffdtd_runtime_compatibility_patches,
    finite_record_pressure_transfer as pffdtd_finite_record_pressure_transfer,
    pffdtd_velocity_potential_to_pressure_trace,
    recombine_pffdtd_receiver_traces,
)
from htdt.acoustic_pffdtd_polyhedral_geometry import register_r120b_polyhedral_authorities
from htdt.r130d_general3d_validation import (
    STENCIL_SENSITIVITY_CANONICAL_CELL_ID,
    STENCIL_SENSITIVITY_VARIANT_IDS,
    classify_dense_frequency_neighborhood,
    classify_stencil_sensitivity,
    dense_frequency_grid,
    load_dense_frequency_diagnostic_plan,
    load_spatial_representation_diagnostic_plan,
    load_stencil_sensitivity_diagnostic_plan,
    load_target_window_diagnostic_plan,
    load_validation_plan,
    normalized_complex_difference,
    semantic_hash,
    stencil_variant_weights,
    validate_dense_frequency_diagnostic_binding,
    validate_exact_binding,
    validate_physical_observable_contract,
    validate_refinement_schedule,
    validate_spatial_representation_diagnostic_binding,
    validate_stencil_sensitivity_diagnostic_binding,
)
from htdt.acoustics.services.acoustic_pffdtd_polyhedral_executor import PffdtdPolyhedralCandidateWaveExecutor


EVIDENCE_SCHEMA = 'htdt.r130d.stencil-sensitivity-diagnostic-committed-evidence-1'
SUMMARY_SCHEMA = 'htdt.r130d.stencil-sensitivity-diagnostic-committed-summary-1'
SIM_ASSET_NAMES = ('vox_out.h5', 'comms_out.h5', 'sim_consts.h5', 'sim_mats.h5')


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Run the frozen R130D stencil-discretization sensitivity diagnostic'
    )
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--diagnostic-plan', required=True, type=Path)
    parser.add_argument('--spatial-diagnostic-plan', required=True, type=Path)
    parser.add_argument('--dense-diagnostic-plan', required=True, type=Path)
    parser.add_argument('--stencil-diagnostic-plan', required=True, type=Path)
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
        in_sigs = np.asarray(handle['in_sigs'][...], dtype=np.float64)
        out_alpha = np.asarray(handle['out_alpha'][...], dtype=np.float64)
        nt = int(handle['Nt'][()])
    with h5py.File(consts_path, 'r') as handle:
        time_step_s = float(handle['Ts'][()])
    return {
        'u_out': u_out,
        'in_sigs': in_sigs,
        'out_alpha': out_alpha,
        'nt': nt,
        'time_step_s': time_step_s,
    }


def _run_source_variant_leg(
    *,
    canonical_sim_dir: Path,
    variant_dir: Path,
    source_weights: np.ndarray,
    solver_threads: int,
    sim_engine_cls,
) -> dict[str, Any]:
    """Re-run the pinned SimEngine on a copied asset set with renormalized rows."""
    import h5py

    if variant_dir.exists():
        shutil.rmtree(variant_dir)
    variant_sim_dir = variant_dir / 'sim'
    variant_sim_dir.mkdir(parents=True)
    for name in SIM_ASSET_NAMES:
        shutil.copy2(canonical_sim_dir / name, variant_sim_dir / name)
    comms_path = variant_sim_dir / 'comms_out.h5'
    with h5py.File(comms_path, 'r+') as handle:
        in_sigs = np.asarray(handle['in_sigs'][...], dtype=np.float64)
        total_injected = np.sum(in_sigs, axis=0)
        handle['in_sigs'][...] = source_weights[:, None] * total_injected[None, :]
        nt = int(handle['Nt'][()])
    engine = sim_engine_cls(
        variant_sim_dir,
        energy_on=False,
        nthreads=solver_threads,
    )
    engine.load_h5_data()
    engine.setup_mask()
    engine.allocate_mem()
    engine.set_coeffs()
    engine.checks()
    engine.run_all(nsteps=int(engine.Nt))
    if int(engine.Nt) != nt:
        raise ValidationBlocked(
            'PFFDTD stencil-variant Nt differs from the canonical comms asset'
        )
    return {
        'u_out': np.asarray(engine.u_out, dtype=np.float64).copy(),
        'time_step_s': float(engine.Ts),
        'nt': int(engine.Nt),
        'total_injected_sha256': semantic_hash(
            [float(x) for x in total_injected]
        ),
    }


def _cell_transfer(
    *,
    u_out: np.ndarray,
    receiver_weights: np.ndarray,
    time_step_s: float,
    nt: int,
    density_kg_m3: float,
    frequency_hz: np.ndarray,
) -> tuple[np.ndarray, str, np.ndarray]:
    receiver_potential = recombine_pffdtd_receiver_traces(
        u_out,
        np.asarray([receiver_weights], dtype=np.float64),
        receiver_count=1,
        nt=nt,
    )[0]
    pressure_trace = pffdtd_velocity_potential_to_pressure_trace(
        receiver_potential,
        time_step_s=time_step_s,
        density_kg_m3=density_kg_m3,
    )
    source_trace = np.zeros(nt, dtype=np.float64)
    source_trace[0] = 1.0
    transfer = pffdtd_finite_record_pressure_transfer(
        pressure_trace,
        source_trace,
        time_step_s=time_step_s,
        frequency_hz=frequency_hz,
    )
    return (
        transfer,
        semantic_hash([float(x) for x in pressure_trace]),
        pressure_trace,
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
            stencil_diagnostic['canonical_reproduction'][
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
    stencil_block = stencil_diagnostic['stencil_sensitivity']
    variant_rules = {
        str(item['variant_id']): str(item['rule'])
        for item in stencil_block['stencil_variants']
    }
    source_variants = [
        str(v) for v in stencil_block['cell_matrix']['source_variants']
    ]
    receiver_variants = [
        str(v) for v in stencil_block['cell_matrix']['receiver_variants']
    ]
    if tuple(source_variants) != STENCIL_SENSITIVITY_VARIANT_IDS or tuple(
        receiver_variants
    ) != STENCIL_SENSITIVITY_VARIANT_IDS:
        raise ValidationBlocked('stencil cell matrix does not match the frozen set')
    canonical_cell_id = stencil_block['cell_matrix']['canonical_cell_id']
    if canonical_cell_id != STENCIL_SENSITIVITY_CANONICAL_CELL_ID:
        raise ValidationBlocked('stencil canonical cell id is not frozen')

    # Variant weight vectors per level/endpoint, derived from the recorded
    # executed stencils (same node order as in_sigs / u_out rows).
    weight_table: dict[tuple[float, str, str], np.ndarray] = {}
    variant_stencil_records: list[dict[str, Any]] = []
    for level in pffdtd_levels:
        ppw = float(level['points_per_wavelength'])
        for endpoint, stencil_key, position in (
            (
                'source',
                'source_interpolation_stencil',
                plan.fixture.source_position_m,
            ),
            (
                'receiver',
                'receiver_interpolation_stencil',
                plan.fixture.receiver_position_m,
            ),
        ):
            stencil = level[stencil_key]
            canonical_weights = np.asarray(
                stencil['interpolation_weights'], dtype=np.float64
            )
            node_positions = np.asarray(
                stencil['node_positions_m'], dtype=np.float64
            )
            for variant_id in STENCIL_SENSITIVITY_VARIANT_IDS:
                weights = stencil_variant_weights(
                    variant_id=variant_id,
                    canonical_weights=canonical_weights,
                    node_positions_m=node_positions,
                    exact_position_m=position,
                )
                weight_table[(ppw, endpoint, variant_id)] = weights
                record: dict[str, Any] = {
                    'points_per_wavelength': ppw,
                    'endpoint': endpoint,
                    'variant_id': variant_id,
                    'weights': [float(x) for x in weights],
                    'weight_sum': float(np.sum(weights)),
                    'stencil_sha256': semantic_hash(
                        {
                            'variant_id': variant_id,
                            'surrounding_linear_indices': stencil[
                                'surrounding_linear_indices'
                            ],
                            'weights': [float(x) for x in weights],
                        }
                    ),
                }
                if variant_id == 'nearest_node':
                    record['nearest_node_row'] = int(np.argmax(weights))
                    record['nearest_node_grid_index'] = stencil[
                        'surrounding_grid_indices'
                    ][int(np.argmax(weights))]
                variant_stencil_records.append(record)

    # Read back the canonical executed assets per level.
    level_assets: dict[float, dict[str, Any]] = {}
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
        assets['sim_dir'] = sim_dir
        level_assets[ppw] = assets

    # Source-variant re-executions on copied asset sets.
    upstream_python = args.pffdtd_root / 'python'
    if not (upstream_python / 'sim_setup.py').is_file():
        raise ValidationBlocked('PFFDTD Python runtime is missing from exact checkout')
    _restore_pinned_pffdtd_checkout(fixture['executor'])
    apply_pffdtd_runtime_compatibility_patches(args.pffdtd_root)
    sys.path.insert(0, str(upstream_python))
    try:
        from fdtd.sim_fdtd import SimEngine
    except Exception as exc:
        raise ValidationBlocked(
            f'PFFDTD runtime import failed: {type(exc).__name__}: {exc}'
        ) from exc

    noncanonical_source_variants = [
        variant for variant in source_variants if variant != 'canonical_trilinear'
    ]
    source_variant_runs: dict[tuple[float, str], dict[str, Any]] = {}
    source_variant_run_records: list[dict[str, Any]] = []
    canonical_total_injected_sha256: dict[float, str] = {}
    for level in pffdtd_levels:
        ppw = float(level['points_per_wavelength'])
        canonical_total_injected_sha256[ppw] = semantic_hash(
            [float(x) for x in np.sum(level_assets[ppw]['in_sigs'], axis=0)]
        )
    try:
        for level in pffdtd_levels:
            ppw = float(level['points_per_wavelength'])
            canonical_assets = level_assets[ppw]
            for source_variant in noncanonical_source_variants:
                weights = weight_table[(ppw, 'source', source_variant)]
                run = _run_source_variant_leg(
                    canonical_sim_dir=canonical_assets['sim_dir'],
                    variant_dir=(
                        args.work_root
                        / 'stencil-variants'
                        / f'ppw{ppw:g}'
                        / source_variant
                    ),
                    source_weights=weights,
                    solver_threads=int(plan.pffdtd.solver_threads),
                    sim_engine_cls=SimEngine,
                )
                invariant_holds = bool(
                    run['total_injected_sha256']
                    == canonical_total_injected_sha256[ppw]
                )
                if not invariant_holds:
                    raise ValidationBlocked(
                        'stencil source-variant run changed the total injected '
                        f'signal at {ppw:g} PPW'
                    )
                if run['nt'] != canonical_assets['nt']:
                    raise ValidationBlocked(
                        'stencil source-variant Nt differs from canonical leg'
                    )
                source_variant_runs[(ppw, source_variant)] = run
                source_variant_run_records.append(
                    {
                        'points_per_wavelength': ppw,
                        'source_variant': source_variant,
                        'total_injected_signal_sha256': run[
                            'total_injected_sha256'
                        ],
                        'canonical_total_injected_signal_sha256': (
                            canonical_total_injected_sha256[ppw]
                        ),
                        'total_injected_signal_invariant': invariant_holds,
                        'nt': run['nt'],
                    }
                )
    finally:
        _restore_pinned_pffdtd_checkout(fixture['executor'])

    # Evaluate every cell of the 3x3 matrix on the dense grid.
    run76_binding = stencil_diagnostic['run76_record_binding']
    identical_label = run76_binding['identical_label']
    record_binding_state = (
        identical_label
        if all(
            level['run76_trace_binding'] == identical_label
            for level in pffdtd_levels
        )
        else run76_binding['nonidentical_label']
    )
    density = float(plan.fixture.density_kg_m3)
    cell_results: list[dict[str, Any]] = []
    for source_variant in source_variants:
        for receiver_variant in receiver_variants:
            cell_id = f'{source_variant}|{receiver_variant}'
            is_canonical = cell_id == canonical_cell_id
            level_records: list[dict[str, Any]] = []
            transfer_by_ppw: dict[float, np.ndarray] = {}
            for level in pffdtd_levels:
                ppw = float(level['points_per_wavelength'])
                if is_canonical:
                    transfer = np.asarray(
                        [
                            complex(float(pair[0]), float(pair[1]))
                            for pair in level[
                                'dense_neighborhood_transfer_pa_per_m3_s'
                            ]
                        ],
                        dtype=np.complex128,
                    )
                    pressure_trace_sha256 = level['diagnostic_raw_trace'][
                        'pressure_trace_sha256'
                    ]
                else:
                    if source_variant == 'canonical_trilinear':
                        u_out = level_assets[ppw]['u_out']
                        time_step_s = float(level['time_step_s'])
                        nt = int(level['time_step_count'])
                    else:
                        run = source_variant_runs[(ppw, source_variant)]
                        u_out = run['u_out']
                        time_step_s = run['time_step_s']
                        nt = run['nt']
                    transfer, pressure_trace_sha256, _ = _cell_transfer(
                        u_out=u_out,
                        receiver_weights=weight_table[
                            (ppw, 'receiver', receiver_variant)
                        ],
                        time_step_s=time_step_s,
                        nt=nt,
                        density_kg_m3=density,
                        frequency_hz=dense_frequencies,
                    )
                transfer_by_ppw[ppw] = transfer
                level_record: dict[str, Any] = {
                    'points_per_wavelength': ppw,
                    'transfer_pa_per_m3_s': _complex_pairs(transfer),
                    'transfer_sha256': semantic_hash(_complex_pairs(transfer)),
                    'pressure_trace_sha256': pressure_trace_sha256,
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
                    'source_variant': source_variant,
                    'receiver_variant': receiver_variant,
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

    pattern_classification = classify_stencil_sensitivity(
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
            'classification': stencil_block['evaluation']['classification'][
                'not_evaluated'
            ],
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
        'dense_diagnostic_sha256': semantic_hash(dense_diagnostic),
        'stencil_diagnostic_id': stencil_diagnostic['diagnostic_id'],
        'stencil_diagnostic_sha256': semantic_hash(stencil_diagnostic),
        'pffdtd_source_commit_sha': plan.pffdtd.source_commit_sha,
        'ppw': [8.0, 10.0, 12.0],
    }
    stencil_result = {
        'axis': stencil_block['axis'],
        'canonical_cell_id': canonical_cell_id,
        'frequency_hz': list(dense_frequency_hz),
        'normalized_complex_difference_formula': dense_block[
            'normalized_complex_difference_formula'
        ],
        'fixed_floor': fixed_floor,
        'pairs': list(dense_block['pairs']),
        'variants': [
            {
                'variant_id': variant_id,
                'rule': variant_rules[variant_id],
            }
            for variant_id in STENCIL_SENSITIVITY_VARIANT_IDS
        ],
        'variant_weight_records': variant_stencil_records,
        'source_variant_runs': source_variant_run_records,
        'cells': cell_results,
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
        'stencil_worsening_pattern': pattern_classification['classification'],
    }

    evidence = {
        'schema_version': EVIDENCE_SCHEMA,
        'task_start_main_sha': stencil_diagnostic['task_start_main_sha'],
        'repository_head': repository_head,
        'authoritative_run': {
            'runner': 'local',
            'note': (
                'executed on a REV37-R130D2 Devin VM with the pinned '
                'numpy/scipy/numba runtime; canonical-cell raw records bound '
                'bit-identical to authoritative run #76 before evaluation; '
                'source-stencil variants re-executed on byte-copied sim assets '
                'with sha256-verified invariant total injected signal'
            ),
        },
        'frozen_authority': frozen_authority,
        'canonical_pr295_reproduction': canonical_reproduction,
        'stencil_sensitivity': stencil_result,
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
            'stencil_sensitivity': {
                'canonical_cell_id': canonical_cell_id,
                'cells': [
                    {
                        'cell_id': cell['cell_id'],
                        'worsening_count': cell['worsening_count'],
                        'classification': cell['classification'],
                        'hamming_distance_to_canonical': cell[
                            'hamming_distance_to_canonical'
                        ],
                    }
                    for cell in cell_results
                ],
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
