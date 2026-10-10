#!/usr/bin/env python3
"""R130D receiver-position sensitivity diagnostic — PFFDTD leg.

Executes the frozen receiver-position sensitivity sweep declared by
``benchmarks/acoustics/r130d_receiver_position_sensitivity_diagnostic_plan.json``:

* re-executes the three pinned PFFDTD PPW levels under the frozen solver
  contract and binds the canonical receiver cell to the persisted run-76
  records by the frozen semantic sha256 pins (``run76_record_binding``);
* for each frozen whole-cell receiver offset of the frozen 13-cell
  lattice, re-runs the pinned SimEngine on a byte-copied sim asset set with
  only the ``comms_out.h5`` ``out_ixyz`` node set replaced by the moved
  trilinear stencil — a rigid whole-cell translation of the canonical
  eight-node set with the canonical ``out_alpha`` weight row unchanged;
  the source comms authority, the boundary/staircase representation, the
  cartesian grid, and the solver constants are sha256-verified identical
  to the canonical leg, and every moved node must stay in-grid, off the
  outer absorbing planes, and air-connected under the frozen six-neighbor
  BFS of the canonical boundary (fail-closed);
* evaluates every cell of the frozen offset axis on the bound parent dense
  diagnostic's 34-frequency grid with the same metric, pairs and
  thresholds, and classifies whether the sub-band worsening pattern moves
  with the receiver position;
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
    RECEIVER_POSITION_CANONICAL_CELL_ID,
    RECEIVER_POSITION_CELL_IDS,
    RECEIVER_POSITION_OFFSET_CELLS,
    classify_dense_frequency_neighborhood,
    classify_receiver_position_sensitivity,
    connected_air_domain_node_metrics,
    load_dense_frequency_diagnostic_plan,
    load_receiver_position_sensitivity_diagnostic_plan,
    load_spatial_representation_diagnostic_plan,
    load_stencil_sensitivity_diagnostic_plan,
    load_target_window_diagnostic_plan,
    load_time_gate_localization_diagnostic_plan,
    load_validation_plan,
    load_voxel_staircase_sensitivity_diagnostic_plan,
    normalized_complex_difference,
    receiver_position_offset_stencil,
    semantic_hash,
    validate_dense_frequency_diagnostic_binding,
    validate_exact_binding,
    validate_physical_observable_contract,
    validate_receiver_position_sensitivity_diagnostic_binding,
    validate_refinement_schedule,
    validate_spatial_representation_diagnostic_binding,
    validate_stencil_sensitivity_diagnostic_binding,
    validate_time_gate_localization_diagnostic_binding,
    validate_voxel_staircase_sensitivity_diagnostic_binding,
)
from htdt.acoustics.services.acoustic_pffdtd_polyhedral_executor import PffdtdPolyhedralCandidateWaveExecutor


EVIDENCE_SCHEMA = (
    'htdt.r130d.receiver-position-sensitivity-diagnostic-committed-evidence-1'
)
SUMMARY_SCHEMA = (
    'htdt.r130d.receiver-position-sensitivity-diagnostic-committed-summary-1'
)
SIM_ASSET_NAMES = ('vox_out.h5', 'comms_out.h5', 'sim_consts.h5', 'sim_mats.h5')


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Run the frozen R130D receiver-position sensitivity diagnostic'
    )
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--diagnostic-plan', required=True, type=Path)
    parser.add_argument('--spatial-diagnostic-plan', required=True, type=Path)
    parser.add_argument('--dense-diagnostic-plan', required=True, type=Path)
    parser.add_argument('--stencil-diagnostic-plan', required=True, type=Path)
    parser.add_argument('--voxel-diagnostic-plan', required=True, type=Path)
    parser.add_argument('--time-gate-diagnostic-plan', required=True, type=Path)
    parser.add_argument('--receiver-diagnostic-plan', required=True, type=Path)
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
    vox_path = sim_dir / 'vox_out.h5'
    comms_path = sim_dir / 'comms_out.h5'
    consts_path = sim_dir / 'sim_consts.h5'
    if not all(
        path.is_file()
        for path in (raw_output_path, vox_path, comms_path, consts_path)
    ):
        raise ValidationBlocked(
            f'PFFDTD canonical sim assets are missing under {sim_dir}'
        )
    with h5py.File(raw_output_path, 'r') as handle:
        u_out = np.asarray(handle['u_out'][...], dtype=np.float64)
    with h5py.File(vox_path, 'r') as handle:
        bn_ixyz = np.asarray(handle['bn_ixyz'][...], dtype=np.int64)
        adj_bn = np.asarray(handle['adj_bn'][...], dtype=bool)
        xv = np.asarray(handle['xv'][...], dtype=np.float64).ravel()
        yv = np.asarray(handle['yv'][...], dtype=np.float64).ravel()
        zv = np.asarray(handle['zv'][...], dtype=np.float64).ravel()
        grid_h = float(handle['h'][()])
        dimensions = (
            int(handle['Nx'][()]),
            int(handle['Ny'][()]),
            int(handle['Nz'][()]),
        )
        vox_static = {
            'xv': [float(x) for x in xv],
            'yv': [float(x) for x in yv],
            'zv': [float(x) for x in zv],
            'h': grid_h,
            'Nx': dimensions[0],
            'Ny': dimensions[1],
            'Nz': dimensions[2],
        }
    with h5py.File(comms_path, 'r') as handle:
        in_sigs = np.asarray(handle['in_sigs'][...], dtype=np.float64)
        out_alpha = np.asarray(handle['out_alpha'][...], dtype=np.float64)
        in_ixyz = np.asarray(handle['in_ixyz'][...], dtype=np.int64)
        out_ixyz = np.asarray(handle['out_ixyz'][...], dtype=np.int64)
        nt = int(handle['Nt'][()])
        comms_contract = {
            'in_ixyz': [int(x) for x in in_ixyz],
            'in_sigs': [
                [float(v) for v in row] for row in np.asarray(handle['in_sigs'])
            ],
            'out_alpha': [
                [float(v) for v in row]
                for row in np.asarray(handle['out_alpha'])
            ],
            'Ns': int(handle['Ns'][()]),
            'Nr': int(handle['Nr'][()]),
            'Nt': nt,
        }
    with h5py.File(consts_path, 'r') as handle:
        time_step_s = float(handle['Ts'][()])
        consts_authority = {
            'c': float(handle['c'][()]),
            'h': float(handle['h'][()]),
            'Ts': time_step_s,
            'l': float(handle['l'][()]),
            'l2': float(handle['l2'][()]),
        }
    return {
        'u_out': u_out,
        'in_sigs': in_sigs,
        'out_alpha': out_alpha,
        'in_ixyz': in_ixyz,
        'out_ixyz': out_ixyz,
        'nt': nt,
        'time_step_s': time_step_s,
        'bn_ixyz': bn_ixyz,
        'adj_bn': adj_bn,
        'xv': xv,
        'yv': yv,
        'zv': zv,
        'grid_h': grid_h,
        'dimensions': dimensions,
        'vox_static_sha256': semantic_hash(vox_static),
        'comms_contract_sha256': semantic_hash(comms_contract),
        'consts_authority_sha256': semantic_hash(consts_authority),
        'canonical_out_ixyz_sha256': semantic_hash(
            [int(x) for x in out_ixyz]
        ),
    }


def _variant_contract_invariants(variant_sim_dir: Path) -> dict[str, str]:
    """Semantic sha256 of the frozen contract datasets in a variant asset set.

    The receiver-position axis moves ``out_ixyz`` deliberately, so the
    comms contract digest covers every comms dataset except ``out_ixyz``;
    the moved ``out_ixyz`` is digested separately and verified against the
    frozen moved stencil.
    """
    import h5py

    with h5py.File(variant_sim_dir / 'vox_out.h5', 'r') as handle:
        vox_static = {
            'xv': [float(x) for x in np.asarray(handle['xv'][...]).ravel()],
            'yv': [float(x) for x in np.asarray(handle['yv'][...]).ravel()],
            'zv': [float(x) for x in np.asarray(handle['zv'][...]).ravel()],
            'h': float(handle['h'][()]),
            'Nx': int(handle['Nx'][()]),
            'Ny': int(handle['Ny'][()]),
            'Nz': int(handle['Nz'][()]),
        }
    with h5py.File(variant_sim_dir / 'comms_out.h5', 'r') as handle:
        comms_contract = {
            'in_ixyz': [
                int(x) for x in np.asarray(handle['in_ixyz'][...])
            ],
            'in_sigs': [
                [float(v) for v in row] for row in np.asarray(handle['in_sigs'])
            ],
            'out_alpha': [
                [float(v) for v in row]
                for row in np.asarray(handle['out_alpha'])
            ],
            'Ns': int(handle['Ns'][()]),
            'Nr': int(handle['Nr'][()]),
            'Nt': int(handle['Nt'][()]),
        }
        moved_out_ixyz = [
            int(x) for x in np.asarray(handle['out_ixyz'][...])
        ]
    with h5py.File(variant_sim_dir / 'sim_consts.h5', 'r') as handle:
        consts_authority = {
            'c': float(handle['c'][()]),
            'h': float(handle['h'][()]),
            'Ts': float(handle['Ts'][()]),
            'l': float(handle['l'][()]),
            'l2': float(handle['l2'][()]),
        }
    return {
        'vox_static_sha256': semantic_hash(vox_static),
        'comms_contract_sha256': semantic_hash(comms_contract),
        'consts_authority_sha256': semantic_hash(consts_authority),
        'out_ixyz_sha256': semantic_hash(moved_out_ixyz),
        'moved_out_ixyz': moved_out_ixyz,
    }


def _run_receiver_offset_leg(
    *,
    canonical_sim_dir: Path,
    variant_dir: Path,
    canonical_assets: dict[str, Any],
    cell_id: str,
    stencil: dict[str, Any],
    reachable_mask: np.ndarray,
    solver_threads: int,
    sim_engine_cls,
) -> dict[str, Any]:
    """Re-run the pinned SimEngine on a copied asset set with only the
    comms ``out_ixyz`` node set replaced by the frozen moved stencil."""
    import h5py

    moved_ixyz = np.asarray(stencil['moved_linear_indices'], dtype=np.int64)
    if not np.all(reachable_mask[moved_ixyz]):
        raise ValidationBlocked(
            f'receiver-position cell {cell_id!r} places a stencil node '
            'outside the canonical air-connected domain'
        )
    if variant_dir.exists():
        shutil.rmtree(variant_dir)
    variant_sim_dir = variant_dir / 'sim'
    variant_sim_dir.mkdir(parents=True)
    for name in SIM_ASSET_NAMES:
        shutil.copy2(canonical_sim_dir / name, variant_sim_dir / name)
    comms_path = variant_sim_dir / 'comms_out.h5'
    with h5py.File(comms_path, 'r+') as handle:
        canonical_shape = np.asarray(handle['out_ixyz'][...]).shape
        if canonical_shape != moved_ixyz.shape:
            raise ValidationBlocked(
                'canonical out_ixyz shape differs from the moved stencil'
            )
        del handle['out_ixyz']
        handle.create_dataset('out_ixyz', data=moved_ixyz)

    invariants = _variant_contract_invariants(variant_sim_dir)
    for key in (
        'vox_static_sha256',
        'comms_contract_sha256',
        'consts_authority_sha256',
    ):
        if invariants[key] != canonical_assets[key]:
            raise ValidationBlocked(
                f'receiver-position variant changed frozen contract {key} '
                f'at {cell_id}'
            )
    if invariants['moved_out_ixyz'] != stencil['moved_linear_indices']:
        raise ValidationBlocked(
            f'receiver-position variant out_ixyz differs from the frozen '
            f'moved stencil at {cell_id}'
        )

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
    if int(engine.Nt) != int(canonical_assets['nt']):
        raise ValidationBlocked(
            'PFFDTD receiver-offset variant Nt differs from the canonical '
            'comms asset'
        )
    return {
        'u_out': np.asarray(engine.u_out, dtype=np.float64).copy(),
        'time_step_s': float(engine.Ts),
        'nt': int(engine.Nt),
        'vox_static_sha256': invariants['vox_static_sha256'],
        'comms_contract_sha256': invariants['comms_contract_sha256'],
        'consts_authority_sha256': invariants['consts_authority_sha256'],
        'out_ixyz_sha256': invariants['out_ixyz_sha256'],
    }


def _cell_transfer(
    *,
    u_out: np.ndarray,
    receiver_weights: np.ndarray,
    time_step_s: float,
    nt: int,
    density_kg_m3: float,
    frequency_hz: np.ndarray,
) -> tuple[np.ndarray, str]:
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
    receiver_diagnostic = load_receiver_position_sensitivity_diagnostic_plan(
        args.receiver_diagnostic_plan
    )
    validate_receiver_position_sensitivity_diagnostic_binding(
        plan, receiver_diagnostic, dense_diagnostic
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
    if semantic_hash(time_gate_diagnostic) != receiver_diagnostic[
        'parent_time_gate_localization_diagnostic'
    ]['semantic_sha256']:
        raise ValidationBlocked(
            'bound time-gate diagnostic file does not match the receiver '
            'plan pin'
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

    receiver_block = receiver_diagnostic['receiver_position_sensitivity']
    canonical_reproduction = _validate_pr295_canonical_reproduction(
        args.pr295_summary,
        reference_levels=[],
        pffdtd_levels=pffdtd_levels,
        max_abs_tolerance=float(
            receiver_diagnostic['canonical_reproduction'][
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
    offset_rules = {
        str(item['cell_id']): str(item['rule'])
        for item in receiver_block['receiver_offsets']
    }
    cell_ids = [str(v) for v in receiver_block['cell_axis']['cells']]
    if tuple(cell_ids) != RECEIVER_POSITION_CELL_IDS:
        raise ValidationBlocked(
            'receiver-position cell axis does not match the frozen set'
        )
    canonical_cell_id = receiver_block['cell_axis']['canonical_cell_id']
    if canonical_cell_id != RECEIVER_POSITION_CANONICAL_CELL_ID:
        raise ValidationBlocked('receiver-position canonical cell id is not frozen')

    # Read back the canonical executed assets per level and compute the
    # frozen moved stencils on each level's own cartesian grid.
    receiver_point_m = tuple(
        float(x) for x in plan.fixture.receiver_position_m
    )
    directions = spatial_diagnostic['spatial_representation'][
        'cartesian_neighbor_order'
    ]
    level_assets: dict[float, dict[str, Any]] = {}
    level_stencils: dict[float, dict[str, dict[str, Any]]] = {}
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
        try:
            domain = connected_air_domain_node_metrics(
                dimensions=assets['dimensions'],
                boundary_linear_indices=assets['bn_ixyz'],
                boundary_adjacency=assets['adj_bn'],
                source_linear_indices=assets['in_ixyz'],
                neighbor_directions=directions,
            )
        except ValueError as exc:
            raise ValidationBlocked(
                f'canonical leg at PPW {ppw:g} fails the air-domain '
                f'connectivity check: {exc}'
            ) from exc
        assets['reachable_mask'] = np.asarray(
            domain['reachable_mask'], dtype=bool
        )
        assets['reachable_air_node_count'] = int(
            domain['reachable_air_node_count']
        )
        level_assets[ppw] = assets
        stencils: dict[str, dict[str, Any]] = {}
        for cell_id in cell_ids:
            if cell_id == canonical_cell_id:
                continue
            stencils[cell_id] = receiver_position_offset_stencil(
                cell_id=cell_id,
                xv=assets['xv'],
                yv=assets['yv'],
                zv=assets['zv'],
                canonical_linear_indices=assets['out_ixyz'],
                canonical_weights=assets['out_alpha'][0],
                grid_spacing_m=assets['grid_h'],
                canonical_position_m=receiver_point_m,
            )
        level_stencils[ppw] = stencils

    # Receiver-offset variant re-executions on copied asset sets.
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

    noncanonical_cells_ids = [
        cell_id
        for cell_id in cell_ids
        if cell_id != RECEIVER_POSITION_CANONICAL_CELL_ID
    ]
    offset_variant_runs: dict[tuple[float, str], dict[str, Any]] = {}
    offset_variant_run_records: list[dict[str, Any]] = []
    try:
        for level in pffdtd_levels:
            ppw = float(level['points_per_wavelength'])
            canonical_assets = level_assets[ppw]
            for cell_id in noncanonical_cells_ids:
                stencil = level_stencils[ppw][cell_id]
                run = _run_receiver_offset_leg(
                    canonical_sim_dir=canonical_assets['sim_dir'],
                    variant_dir=(
                        args.work_root
                        / 'receiver-variants'
                        / f'ppw{ppw:g}'
                        / cell_id
                    ),
                    canonical_assets=canonical_assets,
                    cell_id=cell_id,
                    stencil=stencil,
                    reachable_mask=canonical_assets['reachable_mask'],
                    solver_threads=int(plan.pffdtd.solver_threads),
                    sim_engine_cls=SimEngine,
                )
                if run['nt'] != canonical_assets['nt']:
                    raise ValidationBlocked(
                        'receiver-offset variant Nt differs from canonical leg'
                    )
                offset_variant_runs[(ppw, cell_id)] = run
                offset_variant_run_records.append(
                    {
                        'points_per_wavelength': ppw,
                        'cell_id': cell_id,
                        'nt': run['nt'],
                        'moved_stencil_sha256': stencil['stencil_sha256'],
                        'moved_position_m': stencil['moved_position_m'],
                        'reconstruction_error_m': stencil[
                            'reconstruction_error_m'
                        ],
                        'vox_static_sha256': run['vox_static_sha256'],
                        'comms_contract_sha256': run['comms_contract_sha256'],
                        'consts_authority_sha256': run[
                            'consts_authority_sha256'
                        ],
                        'out_ixyz_sha256': run['out_ixyz_sha256'],
                    }
                )
    finally:
        _restore_pinned_pffdtd_checkout(fixture['executor'])

    # Evaluate every cell of the frozen axis on the dense grid.
    run76_binding = receiver_diagnostic['run76_record_binding']
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
    for cell_id in cell_ids:
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
                run = offset_variant_runs[(ppw, cell_id)]
                transfer, pressure_trace_sha256 = _cell_transfer(
                    u_out=run['u_out'],
                    receiver_weights=level_assets[ppw]['out_alpha'][0],
                    time_step_s=run['time_step_s'],
                    nt=run['nt'],
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
            else:
                stencil = level_stencils[ppw][cell_id]
                level_record['moved_stencil'] = {
                    'offset_cells': stencil['offset_cells'],
                    'moved_position_m': stencil['moved_position_m'],
                    'moved_grid_indices': stencil['moved_grid_indices'],
                    'reconstruction_error_m': stencil[
                        'reconstruction_error_m'
                    ],
                    'stencil_sha256': stencil['stencil_sha256'],
                    'out_ixyz_sha256': offset_variant_runs[(ppw, cell_id)][
                        'out_ixyz_sha256'
                    ],
                }
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
                'offset_cells': list(RECEIVER_POSITION_OFFSET_CELLS[cell_id]),
                'offset_rule': offset_rules[cell_id],
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

    pattern_classification = classify_receiver_position_sensitivity(
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
            'classification': receiver_block['evaluation']['classification'][
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
        'voxel_staircase_diagnostic_id': voxel_diagnostic['diagnostic_id'],
        'voxel_staircase_diagnostic_sha256': semantic_hash(voxel_diagnostic),
        'time_gate_diagnostic_id': time_gate_diagnostic['diagnostic_id'],
        'time_gate_diagnostic_sha256': semantic_hash(time_gate_diagnostic),
        'receiver_position_diagnostic_id': receiver_diagnostic['diagnostic_id'],
        'receiver_position_diagnostic_sha256': semantic_hash(receiver_diagnostic),
        'pffdtd_source_commit_sha': plan.pffdtd.source_commit_sha,
        'ppw': [8.0, 10.0, 12.0],
    }
    receiver_result = {
        'axis': receiver_block['axis'],
        'offset_lattice_convention': receiver_block[
            'offset_lattice_convention'
        ],
        'canonical_cell_id': canonical_cell_id,
        'frequency_hz': list(dense_frequency_hz),
        'normalized_complex_difference_formula': dense_block[
            'normalized_complex_difference_formula'
        ],
        'fixed_floor': fixed_floor,
        'pairs': list(dense_block['pairs']),
        'receiver_offsets': [
            {
                'cell_id': str(item['cell_id']),
                'offset_cells': [int(v) for v in item['offset_cells']],
                'control': bool(item.get('control', False)),
                'rule': str(item['rule']),
            }
            for item in receiver_block['receiver_offsets']
        ],
        'receiver_offset_runs': offset_variant_run_records,
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
        'receiver_position_worsening_pattern': pattern_classification[
            'classification'
        ],
    }

    evidence = {
        'schema_version': EVIDENCE_SCHEMA,
        'task_start_main_sha': receiver_diagnostic['task_start_main_sha'],
        'repository_head': repository_head,
        'authoritative_run': {
            'runner': 'local',
            'note': (
                'executed on a REV39-R130D5 Devin VM with the pinned '
                'numpy/scipy/numba runtime; canonical-cell raw records bound '
                'bit-identical to authoritative run #76 before evaluation; '
                'receiver-offset variants re-executed on byte-copied sim '
                'assets with only the comms out_ixyz node set replaced by '
                'the frozen moved stencil and sha256-verified frozen '
                'vox/consts/source-comms authority plus fail-closed '
                'in-grid/off-halo/air-connectivity gates'
            ),
        },
        'frozen_authority': frozen_authority,
        'canonical_pr295_reproduction': canonical_reproduction,
        'receiver_position_sensitivity': receiver_result,
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
            'receiver_position_sensitivity': {
                'canonical_cell_id': canonical_cell_id,
                'cells': [
                    {
                        'cell_id': cell['cell_id'],
                        'offset_cells': cell['offset_cells'],
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
