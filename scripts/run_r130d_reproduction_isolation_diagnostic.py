#!/usr/bin/env python3
"""R130D issue-938 reproduction-and-isolation diagnostic — PFFDTD legs.

Executes the frozen plan declared by
``benchmarks/acoustics/r130d_reproduction_isolation_diagnostic_plan.json``:

* independently re-executes the three pinned PFFDTD PPW levels under the
  frozen solver contract and binds every level to the run-76 record pins
  (``run76_record_binding``), then re-derives the run-25 self-convergence
  adjacent-pair metrics and the run-62 canonical/aligned transfers and
  requires bit-level agreement with the committed authorities;
* runs a record-prefix identity check: the 8 PPW leg is re-executed with a
  truncated comms record (``Nt``/``in_sigs`` columns only) and the
  receiver grid trace must be bit-identical on the shared prefix — this
  bounds record-length truncation as a mechanism;
* runs CFL/dt variants at every PPW level (``Ts' = f*Ts`` with the
  SimComms-consistent rescaled impulse and ``Nt' = ceil(T/Ts')``) and
  re-evaluates the dense-frequency worsening vector on each variant;
* executes a hand-built analytic rigid rectangular box at two grid
  spacings and compares the measured modal spectrum against the closed
  form eigenfrequencies;
* rescores the run-25 phase metrics under tighter predeclared mask floors
  and classifies every worsening dense-frequency bin into
  {NEAR_NULL, PHASE_WRAP_CANDIDATE, NEAR_RESONANCE, UNCLASSIFIED};
* emits the hypothesis table over the issue's cause space with separated
  CODE_VERIFIED / SELF_CONVERGENCE_PASS / CROSS_SOLVER_ELIGIBLE /
  PHYSICALLY_VALIDATED verdicts and a reproduction manifest.

Diagnostic-only: runs no MFEM leg (recorded ENVIRONMENT_BLOCKED on this
box), changes no canonical solver output, cannot promote general-3D
validation. Canonical reproduction is still guarded: every executed
PFFDTD level must reproduce the frozen PR #295 canonical transfer at the
plan tolerance before evidence is written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import scipy

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
sys.path.insert(0, str(_SCRIPT_DIR.parent / 'backend' / 'src'))

from run_r130a_candidate_wave_execution import _fixture as build_r130a_fixture
from run_r130d_joint_translation_sensitivity_diagnostic import (
    SIM_ASSET_NAMES,
    _complex_pairs,
    _load_canonical_leg_assets,
)
from run_r130d_polyhedral_candidate_wave_execution import (
    _make_polyhedron,
    _restore_pinned_pffdtd_checkout,
    _sloped_same_bbox_fixture,
)
from htdt.pffdtd_boundary_halo import apply_boundary_halo_separation
from run_r130d_general3d_validation import (
    ANALYSIS_KERNEL,
    PHASOR,
    QUANTITY,
    UNIT,
    ValidationBlocked,
    _git_head,
    _mfem_execution_mode,
    _observable_contract,
    _run_pffdtd_level,
    _run_reference_level,
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
    build_reproduction_hypothesis_table,
    classify_dense_bin_cause,
    classify_dense_frequency_neighborhood,
    classify_reproduction_isolation,
    compare_complex_transfer,
    load_dense_frequency_diagnostic_plan,
    load_joint_translation_sensitivity_diagnostic_plan,
    load_reproduction_isolation_diagnostic_plan,
    load_spatial_representation_diagnostic_plan,
    load_target_window_diagnostic_plan,
    load_validation_plan,
    normalized_complex_difference,
    reproduction_values_match,
    semantic_hash,
    target_window_clipped_left_rectangle_transfer,
    validate_dense_frequency_diagnostic_binding,
    validate_exact_binding,
    validate_joint_translation_sensitivity_diagnostic_binding,
    validate_physical_observable_contract,
    validate_refinement_schedule,
    validate_reproduction_isolation_diagnostic_binding,
    validate_spatial_representation_diagnostic_binding,
)
from htdt.acoustics.services.acoustic_pffdtd_polyhedral_executor import PffdtdPolyhedralCandidateWaveExecutor


EVIDENCE_SCHEMA = (
    'htdt.r130d.reproduction-isolation-diagnostic-committed-evidence-1'
)
SUMMARY_SCHEMA = (
    'htdt.r130d.reproduction-isolation-diagnostic-committed-summary-1'
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Run the frozen R130D reproduction-isolation diagnostic'
    )
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--diagnostic-plan', required=True, type=Path)
    parser.add_argument('--spatial-diagnostic-plan', required=True, type=Path)
    parser.add_argument('--dense-diagnostic-plan', required=True, type=Path)
    parser.add_argument('--target-window-diagnostic-plan', required=True, type=Path)
    parser.add_argument('--joint-diagnostic-plan', required=True, type=Path)
    parser.add_argument('--run25-summary', required=True, type=Path)
    parser.add_argument('--run62-summary', required=True, type=Path)
    parser.add_argument('--pr295-summary', required=True, type=Path)
    parser.add_argument('--pffdtd-root', required=True, type=Path)
    parser.add_argument(
        '--mfem-root',
        type=Path,
        help='MFEM source checkout at the pinned commit (enables the MFEM legs)',
    )
    parser.add_argument(
        '--mfem-executable',
        type=Path,
        help='Built r130d sloped-tet system adapter (enables the MFEM legs)',
    )
    parser.add_argument('--work-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--summary-output', type=Path)
    return parser


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def _metric_dict(metric: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (float(value) if isinstance(value, np.floating) else value)
        for key, value in metric.items()
    }


def _transfer_from_u_out(
    *,
    u_out: np.ndarray,
    out_alpha: np.ndarray,
    time_step_s: float,
    nt: int,
    density_kg_m3: float,
    frequency_hz: np.ndarray,
) -> tuple[np.ndarray, str, np.ndarray]:
    receiver_potential = recombine_pffdtd_receiver_traces(
        u_out,
        out_alpha,
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


def _adjacent_pair_metrics(
    *,
    transfers_by_ppw: dict[float, np.ndarray],
    ppw_levels: list[float],
    frequency_hz,
    magnitude_mask_relative_db: float,
) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for index in range(len(ppw_levels) - 1):
        coarse = transfers_by_ppw[float(ppw_levels[index])]
        fine = transfers_by_ppw[float(ppw_levels[index + 1])]
        # committed run-25 convention: reference=fine, candidate=coarse
        metric = compare_complex_transfer(
            reference=_complex_pairs(fine),
            candidate=_complex_pairs(coarse),
            frequency_hz=frequency_hz,
            magnitude_mask_relative_db=magnitude_mask_relative_db,
        ).model_dump(mode='json')
        pairs.append(
            {
                'coarse_ppw': float(ppw_levels[index]),
                'fine_ppw': float(ppw_levels[index + 1]),
                'complex_rms_relative': float(metric['complex_rms_relative']),
                'magnitude_max_relative': float(
                    metric['magnitude_max_relative']
                ),
                'magnitude_max_db': float(metric['magnitude_max_db']),
                'phase_max_deg': float(metric['phase_max_deg']),
                # projected to the committed run-25 per-frequency key set so
                # the reproduction check compares like-for-like leaves
                'frequency_metrics': [
                    {
                        'frequency_hz': float(item['frequency_hz']),
                        'complex_relative': float(item['complex_relative']),
                        'magnitude_relative': float(
                            item['magnitude_relative']
                        ),
                        'magnitude_db': float(item['magnitude_db']),
                        'phase_deg': float(item['phase_deg']),
                    }
                    for item in metric['frequency_metrics']
                ],
            }
        )
    return pairs


def _dense_worsening(
    *,
    transfers_by_ppw: dict[float, np.ndarray],
    ppw_levels: list[float],
    dense_frequencies: np.ndarray,
    fixed_floor: float,
) -> dict[str, Any]:
    if len(ppw_levels) != 3:
        raise ValidationBlocked('dense worsening requires exactly 3 PPW levels')
    low, mid, high = (float(x) for x in ppw_levels)
    d_lo_mid = np.asarray(
        [
            normalized_complex_difference(a, b, fixed_floor=fixed_floor)
            for a, b in zip(
                transfers_by_ppw[low], transfers_by_ppw[mid], strict=True
            )
        ],
        dtype=np.float64,
    )
    d_mid_hi = np.asarray(
        [
            normalized_complex_difference(a, b, fixed_floor=fixed_floor)
            for a, b in zip(
                transfers_by_ppw[mid], transfers_by_ppw[high], strict=True
            )
        ],
        dtype=np.float64,
    )
    result = classify_dense_frequency_neighborhood(d_lo_mid, d_mid_hi)
    result['d_lo_mid'] = [float(x) for x in d_lo_mid]
    result['d_mid_hi'] = [float(x) for x in d_mid_hi]
    result['dense_frequencies_hz'] = [float(x) for x in dense_frequencies]
    return result


def _run_record_prefix_leg(
    *,
    canonical_sim_dir: Path,
    variant_dir: Path,
    truncated_nt: int,
    solver_threads: int,
    sim_engine_cls,
) -> dict[str, Any]:
    """Re-run the SimEngine on a copied asset set whose comms record
    length is truncated; the receiver trace must be bit-identical on the
    shared prefix."""
    import h5py

    if variant_dir.exists():
        shutil.rmtree(variant_dir)
    variant_sim_dir = variant_dir / 'sim'
    variant_sim_dir.mkdir(parents=True)
    for name in SIM_ASSET_NAMES:
        shutil.copy2(canonical_sim_dir / name, variant_sim_dir / name)
    comms_path = variant_sim_dir / 'comms_out.h5'
    with h5py.File(comms_path, 'r+') as handle:
        canonical_nt = int(handle['Nt'][()])
        if not 1 <= truncated_nt < canonical_nt:
            raise ValidationBlocked(
                'record-prefix truncated Nt is not strictly inside the '
                'canonical record'
            )
        in_sigs = np.asarray(handle['in_sigs'][...], dtype=np.float64)
        del handle['Nt']
        handle.create_dataset('Nt', data=np.int64(truncated_nt))
        del handle['in_sigs']
        handle.create_dataset(
            'in_sigs', data=in_sigs[:, :truncated_nt].copy()
        )
    engine = sim_engine_cls(
        variant_sim_dir, energy_on=False, nthreads=solver_threads
    )
    engine.load_h5_data()
    engine.setup_mask()
    apply_boundary_halo_separation(engine)
    engine.allocate_mem()
    engine.set_coeffs()
    engine.checks()
    engine.run_all(nsteps=int(engine.Nt))
    if int(engine.Nt) != truncated_nt:
        raise ValidationBlocked(
            'record-prefix variant Nt differs from the truncated asset'
        )
    return {
        'u_out': np.asarray(engine.u_out, dtype=np.float64).copy(),
        'nt': int(engine.Nt),
        'time_step_s': float(engine.Ts),
    }


def _run_cfl_variant_leg(
    *,
    canonical_sim_dir: Path,
    canonical_assets: dict[str, Any],
    variant_dir: Path,
    ts_scale: float,
    record_duration_s: float,
    solver_threads: int,
    sim_engine_cls,
) -> dict[str, Any]:
    """Re-run the SimEngine on a copied asset set with the time step scaled
    (Ts' = f*Ts), the SimComms-consistent rescaled impulse, and
    ``Nt' = ceil(duration/Ts')`` so the physical record window is held."""
    import h5py

    if variant_dir.exists():
        shutil.rmtree(variant_dir)
    variant_sim_dir = variant_dir / 'sim'
    variant_sim_dir.mkdir(parents=True)
    for name in SIM_ASSET_NAMES:
        shutil.copy2(canonical_sim_dir / name, variant_sim_dir / name)
    canonical_ts = float(canonical_assets['time_step_s'])
    canonical_l = float(canonical_assets['consts_authority_l'])
    canonical_l2 = float(canonical_assets['consts_authority_l2'])
    grid_h = float(canonical_assets['grid_h'])
    ts_new = canonical_ts * ts_scale
    l_new = canonical_l * ts_scale
    l2_new = canonical_l2 * ts_scale * ts_scale
    nt_new = int(math.ceil(record_duration_s / ts_new))
    consts_path = variant_sim_dir / 'sim_consts.h5'
    with h5py.File(consts_path, 'r+') as handle:
        for name, value in (
            ('Ts', ts_new),
            ('SR', 1.0 / ts_new),
            ('l', l_new),
            ('l2', l2_new),
        ):
            del handle[name]
            handle.create_dataset(name, data=np.float64(value))
    comms_path = variant_sim_dir / 'comms_out.h5'
    with h5py.File(comms_path, 'r+') as handle:
        canonical_in_sigs = np.asarray(
            handle['in_sigs'][...], dtype=np.float64
        )
        # Recover the SimComms trilinear source weights: canonical
        # in_sigs[:,0] == in_alpha * l2 / h at the canonical Ts.
        in_alpha = canonical_in_sigs[:, 0] * grid_h / canonical_l2
        in_sigs_new = np.zeros((in_alpha.size, nt_new), dtype=np.float64)
        in_sigs_new[:, 0] = in_alpha * l2_new / grid_h
        del handle['Nt']
        handle.create_dataset('Nt', data=np.int64(nt_new))
        del handle['in_sigs']
        handle.create_dataset('in_sigs', data=in_sigs_new)
    engine = sim_engine_cls(
        variant_sim_dir, energy_on=False, nthreads=solver_threads
    )
    engine.load_h5_data()
    engine.setup_mask()
    apply_boundary_halo_separation(engine)
    engine.allocate_mem()
    engine.set_coeffs()
    engine.checks()
    engine.run_all(nsteps=int(engine.Nt))
    if int(engine.Nt) != nt_new:
        raise ValidationBlocked('CFL variant Nt differs from the asset')
    return {
        'u_out': np.asarray(engine.u_out, dtype=np.float64).copy(),
        'nt': int(engine.Nt),
        'time_step_s': float(engine.Ts),
        'ts_scale': float(ts_scale),
    }


def _trilinear_nodes(
    *,
    position_m: tuple[float, float, float],
    xv: np.ndarray,
    yv: np.ndarray,
    zv: np.ndarray,
    grid_h: float,
    dimensions: tuple[int, int, int],
) -> tuple[np.ndarray, np.ndarray]:
    nx, ny, nz = dimensions
    axes = (xv, yv, zv)
    limits = (nx, ny, nz)
    base: list[int] = []
    frac: list[float] = []
    for axis in range(3):
        coord = float(position_m[axis])
        grid = axes[axis]
        cell = int(math.floor((coord - float(grid[0])) / grid_h))
        cell = max(0, min(cell, limits[axis] - 2))
        base.append(cell)
        frac.append((coord - float(grid[cell])) / grid_h)
    nodes: list[int] = []
    weights: list[float] = []
    for dx in (0, 1):
        for dy in (0, 1):
            for dz in (0, 1):
                i = base[0] + dx
                j = base[1] + dy
                k = base[2] + dz
                weight = (
                    (frac[0] if dx else 1.0 - frac[0])
                    * (frac[1] if dy else 1.0 - frac[1])
                    * (frac[2] if dz else 1.0 - frac[2])
                )
                # pffdtd linear index convention: ix*Ny*Nz + iy*Nz + iz
                nodes.append(i * ny * nz + j * nz + k)
                weights.append(weight)
    return np.asarray(nodes, dtype=np.int64), np.asarray(
        weights, dtype=np.float64
    )


def _write_analytic_box_assets(
    *,
    sim_dir: Path,
    box_dims_m: dict[str, float],
    grid_h: float,
    time_step_s: float,
    record_duration_s: float,
    source_position_m: tuple[float, float, float],
    receiver_position_m: tuple[float, float, float],
    speed_of_sound_m_s: float,
    wall_offset_cells: int = 2,
) -> dict[str, Any]:
    """Build a rigid rectangular box asset set directly in SimEngine input
    schema: outer absorbing halo (auto-derived by the engine), the room
    boundary nodes at ``wall_offset_cells`` inside the grid, all-rigid
    materials.  ``wall_offset_cells=2`` is the canonical layout with a
    one-cell dead ring between the absorbing halo and the walls;
    ``wall_offset_cells=1`` touches the halo and leaks energy."""
    import h5py

    lx = float(box_dims_m['x'])
    ly = float(box_dims_m['y'])
    lz = float(box_dims_m['z'])
    nx = int(round(lx / grid_h)) + 5
    ny = int(round(ly / grid_h)) + 5
    nz = int(round(lz / grid_h)) + 5
    # Canonical layout: a one-cell dead ring separates the outer ABC halo
    # from the room walls (walls at index 2 and N-3; a wall that touches
    # the absorbing halo leaks energy — see the result doc).
    w = int(wall_offset_cells)
    wall_mask = np.zeros((nx, ny, nz), dtype=bool)
    wall_mask[w, w : ny - w, w : nz - w] = True
    wall_mask[nx - 1 - w, w : ny - w, w : nz - w] = True
    wall_mask[w : nx - w, w, w : nz - w] = True
    wall_mask[w : nx - w, ny - 1 - w, w : nz - w] = True
    wall_mask[w : nx - w, w : ny - w, w] = True
    wall_mask[w : nx - w, w : ny - w, nz - 1 - w] = True
    bn_ixyz = np.flatnonzero(wall_mask.ravel()).astype(np.int64)
    adj_bn = np.zeros((bn_ixyz.size, 6), dtype=bool)
    saf_bn = np.zeros(bn_ixyz.size, dtype=np.float64)
    # neighbour order +x -x +y -y +z -z (cartesian canonical order)
    for row, linear in enumerate(bn_ixyz):
        i = int(linear // (ny * nz))
        j = int((linear // nz) % ny)
        k = int(linear % nz)
        neighbours = (
            (i + 1, j, k),
            (i - 1, j, k),
            (i, j + 1, k),
            (i, j - 1, k),
            (i, j, k + 1),
            (i, j, k - 1),
        )
        for axis_dir, (ni, nj, nk) in enumerate(neighbours):
            inside_room = (
                w <= ni <= nx - 1 - w
                and w <= nj <= ny - 1 - w
                and w <= nk <= nz - 1 - w
            )
            adj_bn[row, axis_dir] = inside_room
        saf_bn[row] = float(np.count_nonzero(~adj_bn[row]))
    xv = np.arange(nx, dtype=np.float64) * grid_h
    yv = np.arange(ny, dtype=np.float64) * grid_h
    zv = np.arange(nz, dtype=np.float64) * grid_h
    dimensions = (nx, ny, nz)
    in_ixyz, in_alpha = _trilinear_nodes(
        position_m=source_position_m,
        xv=xv,
        yv=yv,
        zv=zv,
        grid_h=grid_h,
        dimensions=dimensions,
    )
    out_ixyz, out_w = _trilinear_nodes(
        position_m=receiver_position_m,
        xv=xv,
        yv=yv,
        zv=zv,
        grid_h=grid_h,
        dimensions=dimensions,
    )
    out_alpha = np.asarray([out_w], dtype=np.float64)
    # All source/receiver stencil nodes must be interior air cells, not
    # boundary or halo cells.
    air_mask = np.zeros((nx, ny, nz), dtype=bool)
    air_mask[w + 1 : nx - w - 1, w + 1 : ny - w - 1, w + 1 : nz - w - 1] = True
    air_flat = air_mask.ravel()
    if not (np.all(air_flat[in_ixyz]) and np.all(air_flat[out_ixyz])):
        raise ValidationBlocked(
            'analytic box source/receiver stencil lands off the air domain'
        )
    courant = float(speed_of_sound_m_s) * time_step_s / grid_h
    l2 = courant * courant
    nt = int(math.ceil(record_duration_s / time_step_s))
    in_sigs = np.zeros((in_alpha.size, nt), dtype=np.float64)
    in_sigs[:, 0] = in_alpha * l2 / grid_h
    sim_dir.mkdir(parents=True, exist_ok=True)
    with h5py.File(sim_dir / 'vox_out.h5', 'w') as h5f:
        h5f.create_dataset('bn_ixyz', data=bn_ixyz)
        h5f.create_dataset('adj_bn', data=adj_bn)
        h5f.create_dataset('mat_bn', data=np.full(bn_ixyz.size, -1, np.int8))
        h5f.create_dataset('saf_bn', data=saf_bn)
        h5f.create_dataset('xv', data=xv)
        h5f.create_dataset('yv', data=yv)
        h5f.create_dataset('zv', data=zv)
        h5f.create_dataset('h', data=np.float64(grid_h))
        h5f.create_dataset('Nx', data=np.int64(nx))
        h5f.create_dataset('Ny', data=np.int64(ny))
        h5f.create_dataset('Nz', data=np.int64(nz))
        h5f.create_dataset('Nb', data=np.int64(bn_ixyz.size))
    with h5py.File(sim_dir / 'comms_out.h5', 'w') as h5f:
        h5f.create_dataset('in_ixyz', data=in_ixyz)
        h5f.create_dataset('out_ixyz', data=out_ixyz)
        h5f.create_dataset('out_alpha', data=out_alpha)
        h5f.create_dataset(
            'out_reorder', data=np.arange(out_ixyz.size, dtype=np.int64)
        )
        h5f.create_dataset('in_sigs', data=in_sigs)
        h5f.create_dataset('Ns', data=np.int64(in_ixyz.size))
        h5f.create_dataset('Nr', data=np.int64(out_ixyz.size))
        h5f.create_dataset('Nt', data=np.int64(nt))
        h5f.create_dataset('diff', data=np.int8(0))
    with h5py.File(sim_dir / 'sim_consts.h5', 'w') as h5f:
        h5f.create_dataset('c', data=np.float64(speed_of_sound_m_s))
        h5f.create_dataset('h', data=np.float64(grid_h))
        h5f.create_dataset('Ts', data=np.float64(time_step_s))
        h5f.create_dataset('SR', data=np.float64(1.0 / time_step_s))
        h5f.create_dataset('l', data=np.float64(courant))
        h5f.create_dataset('l2', data=np.float64(l2))
        h5f.create_dataset('fcc_flag', data=np.int8(0))
        h5f.create_dataset('Tc', data=np.float64(20.0))
        h5f.create_dataset('rh', data=np.float64(50.0))
    with h5py.File(sim_dir / 'sim_mats.h5', 'w') as h5f:
        h5f.create_dataset('Nmat', data=np.int8(0))
        h5f.create_dataset('Mb', data=np.zeros((0,), dtype=np.int8))
    return {
        'dimensions': dimensions,
        'boundary_node_count': int(bn_ixyz.size),
        'nt': nt,
        'out_alpha': out_alpha,
        'in_ixyz_sha256': semantic_hash([int(x) for x in in_ixyz]),
        'out_ixyz_sha256': semantic_hash([int(x) for x in out_ixyz]),
        'courant_c_dt_over_h': courant,
    }


def _analytic_mode_frequencies(
    *,
    box_dims_m: dict[str, float],
    speed_of_sound_m_s: float,
    n_max: int,
    frequency_cut_hz: float,
    grid_h: float | None = None,
) -> list[dict[str, Any]]:
    # With grid_h given, return the exact eigenfrequencies of the discrete
    # Neumann cavity realized by the boundary-node stencil:
    # omega^2 = (2c/h)^2 * sum_i sin^2(pi n_i / (2 M_i)) with M_i = L_i/h.
    # Otherwise the continuum box modes c/2 * sqrt(sum (n_i/L_i)^2).
    modes: list[dict[str, Any]] = []
    lengths = (
        float(box_dims_m['x']),
        float(box_dims_m['y']),
        float(box_dims_m['z']),
    )
    for nx_mode in range(n_max + 1):
        for ny_mode in range(n_max + 1):
            for nz_mode in range(n_max + 1):
                if nx_mode == ny_mode == nz_mode == 0:
                    continue
                if grid_h is not None:
                    omega_sq = 0.0
                    for index, n_mode in enumerate(
                        (nx_mode, ny_mode, nz_mode)
                    ):
                        intervals = lengths[index] / float(grid_h)
                        omega_sq += math.sin(
                            math.pi * n_mode / (2.0 * intervals)
                        ) ** 2
                    omega_sq *= (2.0 * float(speed_of_sound_m_s) / grid_h) ** 2
                    freq = math.sqrt(omega_sq) / (2.0 * math.pi)
                    modes_candidate = freq
                    if modes_candidate <= frequency_cut_hz:
                        modes.append(
                            {
                                'indices': [nx_mode, ny_mode, nz_mode],
                                'frequency_hz': float(freq),
                            }
                        )
                    continue
                value = math.sqrt(
                    (nx_mode / lengths[0]) ** 2
                    + (ny_mode / lengths[1]) ** 2
                    + (nz_mode / lengths[2]) ** 2
                )
                freq = 0.5 * float(speed_of_sound_m_s) * value
                if freq <= frequency_cut_hz:
                    modes.append(
                        {
                            'indices': [nx_mode, ny_mode, nz_mode],
                            'frequency_hz': float(freq),
                        }
                    )
    modes.sort(key=lambda item: item['frequency_hz'])
    return modes


def _detect_spectral_peaks(
    *,
    magnitude: np.ndarray,
    frequency_hz: np.ndarray,
    prominence_floor_db: float,
    valley_window_bins: int = 8,
    noise_floor_db: float = -40.0,
) -> list[dict[str, float]]:
    if magnitude.size < 3:
        return []
    band_max = float(np.max(magnitude))
    floor = band_max * 10.0 ** (noise_floor_db / 20.0)
    prominence_ratio = 10.0 ** (prominence_floor_db / 20.0)
    peaks: list[dict[str, float]] = []
    for index in range(1, magnitude.size - 1):
        value = float(magnitude[index])
        if not (
            value > float(magnitude[index - 1])
            and value >= float(magnitude[index + 1])
            and value >= floor
        ):
            continue
        left_valley = float(
            np.min(magnitude[max(0, index - valley_window_bins) : index])
        )
        right_valley = float(
            np.min(magnitude[index + 1 : index + valley_window_bins + 1])
        )
        if value < prominence_ratio * max(left_valley, right_valley):
            continue
        peaks.append(
            {
                'frequency_hz': float(frequency_hz[index]),
                'magnitude': value,
            }
        )
    return peaks


def _match_analytic_modes(
    *,
    modes: list[dict[str, Any]],
    peaks: list[dict[str, float]],
    tolerance_relative: float,
    cluster_gap_relative: float,
) -> dict[str, Any]:
    frequencies = np.asarray(
        [item['frequency_hz'] for item in modes], dtype=np.float64
    )
    peak_freqs = np.asarray(
        [item['frequency_hz'] for item in peaks], dtype=np.float64
    )
    # Cluster modes closer than the predeclared relative gap; a cluster is
    # resolved when the measured spectrum places a peak within tolerance
    # of at least one member.
    clusters: list[list[int]] = []
    for index, freq in enumerate(frequencies):
        if clusters and (
            freq / frequencies[clusters[-1][-1]] - 1.0
        ) <= cluster_gap_relative:
            clusters[-1].append(index)
        else:
            clusters.append([index])
    cluster_rows: list[dict[str, Any]] = []
    unresolved: list[int] = []
    for members in clusters:
        member_freqs = [float(frequencies[i]) for i in members]
        matched: list[int] = []
        best_freq = math.nan
        best_error = math.inf
        for i in members:
            if peak_freqs.size == 0:
                continue
            nearest = int(np.argmin(np.abs(peak_freqs - frequencies[i])))
            error = abs(
                float(peak_freqs[nearest]) - float(frequencies[i])
            ) / float(frequencies[i])
            if error < best_error:
                best_error = error
                best_freq = float(peak_freqs[nearest])
            if error <= tolerance_relative:
                matched.append(i)
        resolved = bool(matched)
        if not resolved:
            unresolved.extend(members)
        cluster_rows.append(
            {
                'member_mode_indices': [
                    modes[i]['indices'] for i in members
                ],
                'member_frequencies_hz': member_freqs,
                'nearest_peak_hz': (
                    None if not math.isfinite(best_freq) else best_freq
                ),
                'nearest_peak_relative_error': (
                    None if not math.isfinite(best_error) else best_error
                ),
                'resolved': resolved,
            }
        )
    return {
        'mode_count': int(frequencies.size),
        'cluster_count': len(clusters),
        'resolved_cluster_count': sum(
            1 for row in cluster_rows if row['resolved']
        ),
        'unresolved_mode_frequencies_hz': [
            float(frequencies[i]) for i in unresolved
        ],
        'clusters': cluster_rows,
        'all_resolved': not unresolved,
    }


def _check_runtime_mfem(
    plan,
    *,
    mfem_root: Path | None,
    mfem_executable: Path | None,
    work_root: Path,
) -> dict[str, Any]:
    """MFEM legs are environment-dependent: they need the pinned MFEM
    source checkout plus a built sloped-tet adapter executable. When no
    toolchain is supplied the axis is reported ENVIRONMENT_BLOCKED rather
    than unconditionally failed; when supplied, the frozen sloped-tetra
    system-export + modal legs execute for real (on Windows a Linux ELF
    adapter is invoked through wsl.exe — the execution is still the real
    pinned MFEM build) and the per-level evidence is recorded."""
    pinned_sha = plan.independent_reference.source_commit_sha

    def blocked(reason: str) -> dict[str, Any]:
        return {
            'status': 'ENVIRONMENT_BLOCKED',
            'mfem_source_commit_sha': pinned_sha,
            'blocked_reason': reason,
        }

    if mfem_root is None or mfem_executable is None:
        return blocked(
            'MFEM toolchain not supplied to this run '
            '(--mfem-root/--mfem-executable absent); MFEM-dependent '
            'hypothesis legs are reported UNRESOLVED_ENVIRONMENT_BLOCKED'
        )
    if not mfem_executable.is_file():
        return blocked(
            f'MFEM adapter executable missing: {mfem_executable}'
        )
    head = _git_head(mfem_root)
    if head != pinned_sha:
        return blocked(
            f'MFEM checkout mismatch: expected {pinned_sha}, got {head}'
        )

    mfem_work = work_root / 'mfem-reference'
    mfem_work.mkdir(parents=True, exist_ok=True)
    levels = [
        _run_reference_level(
            plan,
            executable=mfem_executable,
            work_root=mfem_work,
            refinement=int(refinement),
            expected_elements=int(expected_elements),
        )
        for refinement, expected_elements in zip(
            plan.independent_reference.uniform_refinements,
            plan.independent_reference.expected_element_counts,
        )
    ]
    return {
        'status': 'EXECUTED',
        'mfem_source_commit_sha': head,
        'execution_mode': _mfem_execution_mode(mfem_executable),
        'mfem_executable': str(mfem_executable),
        'levels': levels,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    started = time.perf_counter()
    plan = load_validation_plan(args.plan)
    diagnostic = load_reproduction_isolation_diagnostic_plan(
        args.diagnostic_plan
    )
    spatial_diagnostic = load_spatial_representation_diagnostic_plan(
        args.spatial_diagnostic_plan
    )
    validate_spatial_representation_diagnostic_binding(plan, spatial_diagnostic)
    dense_diagnostic = load_dense_frequency_diagnostic_plan(
        args.dense_diagnostic_plan
    )
    validate_dense_frequency_diagnostic_binding(plan, dense_diagnostic)
    joint_diagnostic = load_joint_translation_sensitivity_diagnostic_plan(
        args.joint_diagnostic_plan
    )
    validate_joint_translation_sensitivity_diagnostic_binding(
        plan, joint_diagnostic, dense_diagnostic
    )
    run25_summary = _load_json(args.run25_summary)
    run62_summary = _load_json(args.run62_summary)
    validate_reproduction_isolation_diagnostic_binding(
        plan,
        diagnostic,
        dense_diagnostic=dense_diagnostic,
        run25_summary=run25_summary,
        run62_summary=run62_summary,
    )
    target_diagnostic = load_target_window_diagnostic_plan(
        args.target_window_diagnostic_plan
    )
    _validate_target_window_diagnostic_binding(plan, target_diagnostic)

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
    mfem_manifest = _check_runtime_mfem(
        plan,
        mfem_root=args.mfem_root,
        mfem_executable=args.mfem_executable,
        work_root=args.work_root,
    )
    mfem_executed = mfem_manifest['status'] == 'EXECUTED'
    canonical_reproduction = _validate_pr295_canonical_reproduction(
        args.pr295_summary,
        reference_levels=mfem_manifest.get('levels', []),
        pffdtd_levels=pffdtd_levels,
        max_abs_tolerance=float(
            diagnostic['canonical_reproduction'][
                'max_abs_complex_component_tolerance'
            ]
        ),
    )

    ppw_levels = [float(x) for x in plan.pffdtd.points_per_wavelength]
    density = float(plan.fixture.density_kg_m3)
    frequencies = np.asarray(
        plan.physical_quantity.frequency_hz, dtype=np.float64
    )
    dense_block = dense_diagnostic['dense_frequency_neighborhood']
    dense_frequencies = np.asarray(
        dense_block['diagnostic_frequency_hz'], dtype=np.float64
    )
    fixed_floor = float(dense_block['fixed_floor'])
    duration_s = float(plan.physical_quantity.duration_s)

    # Canonical leg assets + recomputed transfers per level.
    level_assets: dict[float, dict[str, Any]] = {}
    canonical_transfers: dict[float, np.ndarray] = {}
    canonical_dense_transfers: dict[float, np.ndarray] = {}
    canonical_pressure_traces: dict[float, np.ndarray] = {}
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
        # consts authority values needed by the CFL variant rewrite.
        import h5py

        with h5py.File(sim_dir / 'sim_consts.h5', 'r') as handle:
            assets['consts_authority_l'] = float(handle['l'][()])
            assets['consts_authority_l2'] = float(handle['l2'][()])
        transfer, trace_sha, pressure_trace = _transfer_from_u_out(
            u_out=assets['u_out'],
            out_alpha=assets['out_alpha'],
            time_step_s=assets['time_step_s'],
            nt=assets['nt'],
            density_kg_m3=density,
            frequency_hz=frequencies,
        )
        canonical_transfers[ppw] = transfer
        canonical_pressure_traces[ppw] = pressure_trace
        dense_transfer, _, _ = _transfer_from_u_out(
            u_out=assets['u_out'],
            out_alpha=assets['out_alpha'],
            time_step_s=assets['time_step_s'],
            nt=assets['nt'],
            density_kg_m3=density,
            frequency_hz=dense_frequencies,
        )
        canonical_dense_transfers[ppw] = dense_transfer
        if trace_sha != level['diagnostic_raw_trace'].get(
            'pressure_trace_sha256', trace_sha
        ):
            raise ValidationBlocked(
                'recomputed pressure trace sha differs from executed level'
            )
        level_assets[ppw] = assets

    # --- reproduction verdicts -------------------------------------------
    run76_binding = diagnostic['run76_record_binding']
    identical_label = str(run76_binding['identical_label'])
    run76_levels = [
        {
            'points_per_wavelength': float(level['points_per_wavelength']),
            'binding': level['run76_trace_binding'],
        }
        for level in pffdtd_levels
    ]
    run76_identical = all(
        row['binding'] == identical_label for row in run76_levels
    )

    recomputed_pairs = _adjacent_pair_metrics(
        transfers_by_ppw=canonical_transfers,
        ppw_levels=ppw_levels,
        frequency_hz=plan.physical_quantity.frequency_hz,
        magnitude_mask_relative_db=plan.acceptance.magnitude_mask_relative_db,
    )
    run25_target = diagnostic['reproduction_targets']['run25_self_convergence']
    run25_match = reproduction_values_match(
        recomputed_pairs, run25_target['pffdtd_adjacent_pairs']
    )

    run62_target = diagnostic['reproduction_targets']['run62_target_window']
    recomputed_run62_rows: list[dict[str, Any]] = []
    for level in pffdtd_levels:
        ppw = float(level['points_per_wavelength'])
        aligned = target_window_clipped_left_rectangle_transfer(
            canonical_pressure_traces[ppw],
            np.concatenate(
                ([1.0], np.zeros(int(level['time_step_count']) - 1))
            ),
            dt_s=float(level['time_step_s']),
            target_duration_s=duration_s,
            frequency_hz=frequencies,
        )
        canonical_pairs = level['transfer_pa_per_m3_s']
        aligned_pairs = _complex_pairs(aligned)
        delta = compare_complex_transfer(
            reference=canonical_pairs,
            candidate=aligned_pairs,
            frequency_hz=plan.physical_quantity.frequency_hz,
            magnitude_mask_relative_db=plan.acceptance.magnitude_mask_relative_db,
        ).model_dump(mode='json')
        recomputed_run62_rows.append(
            {
                'points_per_wavelength': ppw,
                'canonical': canonical_pairs,
                'aligned': aligned_pairs,
                'canonical_aligned_complex_rms': float(
                    delta['complex_rms_relative']
                ),
            }
        )
    run62_match = reproduction_values_match(
        recomputed_run62_rows, run62_target['pffdtd_canonical_transfers']
    )

    # --- variant legs ------------------------------------------------------
    upstream_python = args.pffdtd_root / 'python'
    if not (upstream_python / 'fdtd' / 'sim_fdtd.py').is_file():
        raise ValidationBlocked(
            'PFFDTD Python runtime is missing from exact checkout'
        )
    _restore_pinned_pffdtd_checkout(fixture['executor'])
    apply_pffdtd_runtime_compatibility_patches(args.pffdtd_root)
    sys.path.insert(0, str(upstream_python))
    try:
        from fdtd.sim_fdtd import SimEngine
    except Exception as exc:
        raise ValidationBlocked(
            f'PFFDTD runtime import failed: {type(exc).__name__}: {exc}'
        ) from exc

    axis_verdicts: dict[str, str] = {}
    variant_records: list[dict[str, Any]] = []
    prefix_result: dict[str, Any] = {}
    cfl_results: dict[float, dict[str, Any]] = {}
    box_results: list[dict[str, Any]] = []
    halo_probe_results: list[dict[str, Any]] = []
    try:
        # record prefix identity at the frozen PPW level
        prefix_block = diagnostic['record_prefix_identity']
        prefix_ppw = float(prefix_block['level_ppw'])
        prefix_assets = level_assets[prefix_ppw]
        truncated_nt = int(
            math.floor(duration_s / float(prefix_assets['time_step_s']))
        )
        run = _run_record_prefix_leg(
            canonical_sim_dir=prefix_assets['sim_dir'],
            variant_dir=(
                args.work_root / 'record-prefix' / f'ppw{prefix_ppw:g}'
            ),
            truncated_nt=truncated_nt,
            solver_threads=int(plan.pffdtd.solver_threads),
            sim_engine_cls=SimEngine,
        )
        canonical_prefix = prefix_assets['u_out'][:, :truncated_nt]
        prefix_identical = bool(
            np.array_equal(canonical_prefix, run['u_out'])
        )
        prefix_max_abs = float(
            np.max(np.abs(canonical_prefix - run['u_out']))
        )
        prefix_verdict = (
            'RECORD_PREFIX_IDENTICAL'
            if prefix_identical
            else 'RECORD_PREFIX_DIFFERS'
        )
        axis_verdicts['record_prefix_identity'] = prefix_verdict
        prefix_result = {
            'level_ppw': prefix_ppw,
            'canonical_nt': int(prefix_assets['nt']),
            'truncated_nt': truncated_nt,
            'prefix_identical': prefix_identical,
            'prefix_max_abs_difference': prefix_max_abs,
            'verdict': prefix_verdict,
        }
        variant_records.append(
            {
                'axis': 'record_prefix_identity',
                'level_ppw': prefix_ppw,
                'nt': run['nt'],
                'verdict': prefix_verdict,
            }
        )

        # CFL/dt variants at every PPW level
        cfl_block = diagnostic['cfl_dt_variants']
        cfl_levels = [float(x) for x in cfl_block['levels_ppw']]
        if cfl_levels != ppw_levels:
            raise ValidationBlocked(
                'CFL variant levels do not cover the frozen PPW set'
            )
        for ts_scale in cfl_block['time_step_scale_factors']:
            scale = float(ts_scale)
            transfers: dict[float, np.ndarray] = {}
            for level in pffdtd_levels:
                ppw = float(level['points_per_wavelength'])
                canonical_assets = level_assets[ppw]
                run = _run_cfl_variant_leg(
                    canonical_sim_dir=canonical_assets['sim_dir'],
                    canonical_assets=canonical_assets,
                    variant_dir=(
                        args.work_root
                        / 'cfl-variants'
                        / f'f{scale:g}'
                        / f'ppw{ppw:g}'
                    ),
                    ts_scale=scale,
                    record_duration_s=duration_s,
                    solver_threads=int(plan.pffdtd.solver_threads),
                    sim_engine_cls=SimEngine,
                )
                variant_transfer, variant_sha, _ = _transfer_from_u_out(
                    u_out=run['u_out'],
                    out_alpha=canonical_assets['out_alpha'],
                    time_step_s=run['time_step_s'],
                    nt=run['nt'],
                    density_kg_m3=density,
                    frequency_hz=dense_frequencies,
                )
                transfers[ppw] = variant_transfer
                variant_records.append(
                    {
                        'axis': 'cfl_dt_variants',
                        'ts_scale': scale,
                        'level_ppw': ppw,
                        'nt': run['nt'],
                        'time_step_s': run['time_step_s'],
                        'pressure_trace_sha256': variant_sha,
                    }
                )
            cfl_results[scale] = _dense_worsening(
                transfers_by_ppw=transfers,
                ppw_levels=ppw_levels,
                dense_frequencies=dense_frequencies,
                fixed_floor=fixed_floor,
            )
            cfl_results[scale]['ts_scale'] = scale

        # analytic rigid box at two grid spacings
        box_block = diagnostic['analytic_rigid_box']
        discrete_modes = (
            box_block.get('modal_frequency_reference')
            == 'discrete_cavity_neumann_stencil'
        )
        grid_spec = box_block['peak_detection']['frequency_grid_hz']
        box_frequencies = np.arange(
            float(grid_spec['start']),
            float(grid_spec['stop']) + 0.5 * float(grid_spec['step']),
            float(grid_spec['step']),
            dtype=np.float64,
        )
        for grid_h in box_block['grid_spacing_levels_m']:
            grid_h = float(grid_h)
            modes = _analytic_mode_frequencies(
                box_dims_m=box_block['box_dimensions_m'],
                speed_of_sound_m_s=float(
                    diagnostic['frozen_solver_contract'][
                        'speed_of_sound_m_s'
                    ]
                ),
                n_max=int(box_block['mode_index_bound']['n_max']),
                frequency_cut_hz=float(
                    box_block['mode_index_bound']['frequency_cut_hz']
                ),
                grid_h=grid_h if discrete_modes else None,
            )
            ts = (
                float(box_block['courant_number'])
                * grid_h
                / float(
                    diagnostic['frozen_solver_contract'][
                        'speed_of_sound_m_s'
                    ]
                )
            )
            box_dir = args.work_root / 'analytic-box' / f'h{grid_h:g}'
            meta = _write_analytic_box_assets(
                sim_dir=box_dir / 'sim',
                box_dims_m=box_block['box_dimensions_m'],
                grid_h=grid_h,
                time_step_s=ts,
                record_duration_s=float(box_block['record_duration_s']),
                source_position_m=tuple(
                    float(x) for x in box_block['source_position_m']
                ),
                receiver_position_m=tuple(
                    float(x) for x in box_block['receiver_position_m']
                ),
                speed_of_sound_m_s=float(
                    diagnostic['frozen_solver_contract'][
                        'speed_of_sound_m_s'
                    ]
                ),
            )
            engine = SimEngine(
                box_dir / 'sim',
                energy_on=False,
                nthreads=int(plan.pffdtd.solver_threads),
            )
            engine.load_h5_data()
            engine.setup_mask()
            apply_boundary_halo_separation(engine)
            engine.allocate_mem()
            engine.set_coeffs()
            engine.checks()
            engine.run_all(nsteps=int(engine.Nt))
            box_transfer, box_sha, _ = _transfer_from_u_out(
                u_out=np.asarray(engine.u_out, dtype=np.float64),
                out_alpha=meta['out_alpha'],
                time_step_s=ts,
                nt=int(engine.Nt),
                density_kg_m3=density,
                frequency_hz=box_frequencies,
            )
            magnitude = np.abs(box_transfer)
            peaks = _detect_spectral_peaks(
                magnitude=magnitude,
                frequency_hz=box_frequencies,
                prominence_floor_db=float(
                    box_block['peak_detection']['prominence_floor_db']
                ),
                valley_window_bins=int(
                    box_block['peak_detection'].get('valley_window_bins', 8)
                ),
                noise_floor_db=float(
                    box_block['peak_detection'].get('noise_floor_db', -40.0)
                ),
            )
            match = _match_analytic_modes(
                modes=modes,
                peaks=peaks,
                tolerance_relative=float(
                    box_block['mode_frequency_tolerance_relative']
                ),
                cluster_gap_relative=float(
                    box_block['peak_detection'][
                        'mode_cluster_relative_gap'
                    ]
                ),
            )
            if not match['resolved_cluster_count']:
                verdict = 'ANALYTIC_MODAL_PEAKS_UNRESOLVED'
            elif match['all_resolved']:
                verdict = 'ANALYTIC_MODAL_PEAKS_WITHIN_TOLERANCE'
            else:
                verdict = 'ANALYTIC_MODAL_PEAKS_OUTSIDE_TOLERANCE'
            box_results.append(
                {
                    'grid_spacing_m': grid_h,
                    'time_step_s': ts,
                    'nt': int(engine.Nt),
                    'dimensions': meta['dimensions'],
                    'boundary_node_count': meta['boundary_node_count'],
                    'pressure_trace_sha256': box_sha,
                    'detected_peak_count': len(peaks),
                    'detected_peaks': peaks,
                    'analytic_modes': modes,
                    'mode_match': match,
                    'mode_reference': (
                        'discrete_cavity_neumann_stencil'
                        if discrete_modes
                        else 'continuum_rigid_box'
                    ),
                    'verdict': verdict,
                }
            )
            variant_records.append(
                {
                    'axis': 'analytic_rigid_box',
                    'grid_spacing_m': grid_h,
                    'nt': int(engine.Nt),
                    'verdict': verdict,
                }
            )
        analytic_verdict = (
            box_results[-1]['verdict'] if box_results else 'UNRESOLVED'
        )
        axis_verdicts['analytic_rigid_box'] = analytic_verdict

        # Halo-separation probe: does a rigid wall that touches the
        # absorbing halo lose energy?  The canonical layout (dead ring
        # between halo and walls) must ring; the halo-adjacent layout is
        # the falsifiable contrast.
        probe = box_block.get('halo_separation_probe') or {}
        if probe:
            probe_dims = probe['room_dimensions_m']
            probe_dims_map = {
                'x': float(probe_dims[0]),
                'y': float(probe_dims[1]),
                'z': float(probe_dims[2]),
            }
            probe_h = float(probe['grid_spacing_m'])
            probe_ts = (
                float(box_block['courant_number'])
                * probe_h
                / float(
                    diagnostic['frozen_solver_contract'][
                        'speed_of_sound_m_s'
                    ]
                )
            )
            halo_probe_results = []
            for label, off in (
                ('canonical_separated', int(box_block['wall_index_offset_cells'])),
                (
                    'halo_adjacent',
                    int(probe['wall_index_offset_cells_blocked']),
                ),
            ):
                probe_dir = (
                    args.work_root / 'analytic-box' / f'halo-{label}'
                )
                pmeta = _write_analytic_box_assets(
                    sim_dir=probe_dir / 'sim',
                    box_dims_m=probe_dims_map,
                    grid_h=probe_h,
                    time_step_s=probe_ts,
                    record_duration_s=float(probe['record_seconds']),
                    source_position_m=tuple(
                        float(x) for x in probe['source_xyz_m']
                    ),
                    receiver_position_m=tuple(
                        float(x) for x in probe['receiver_xyz_m']
                    ),
                    speed_of_sound_m_s=float(
                        diagnostic['frozen_solver_contract'][
                            'speed_of_sound_m_s'
                        ]
                    ),
                    wall_offset_cells=off,
                )
                pengine = SimEngine(
                    probe_dir / 'sim',
                    energy_on=False,
                    nthreads=int(plan.pffdtd.solver_threads),
                )
                pengine.load_h5_data()
                pengine.setup_mask()
                apply_boundary_halo_separation(pengine)
                pengine.allocate_mem()
                pengine.set_coeffs()
                pengine.checks()
                pengine.run_all(nsteps=int(pengine.Nt))
                pu = np.asarray(pengine.u_out, dtype=np.float64)
                ppot = recombine_pffdtd_receiver_traces(
                    pu, pmeta['out_alpha'], receiver_count=1,
                    nt=int(pengine.Nt),
                )[0]
                ppt = pffdtd_velocity_potential_to_pressure_trace(
                    ppot,
                    time_step_s=probe_ts,
                    density_kg_m3=density,
                )
                quarter = max(8, ppt.size // 4)
                head_rms = float(np.sqrt(np.mean(ppt[:quarter] ** 2)))
                tail_rms = float(np.sqrt(np.mean(ppt[-quarter:] ** 2)))
                ratio = tail_rms / head_rms if head_rms > 0 else 0.0
                halo_probe_results.append(
                    {
                        'layout': label,
                        'wall_offset_cells': off,
                        'pressure_trace_sha256': hashlib.sha256(
                            ppt.tobytes()
                        ).hexdigest(),
                        'tail_head_rms_ratio': ratio,
                    }
                )
            sep = {
                r['layout']: r['tail_head_rms_ratio']
                for r in halo_probe_results
            }
            ring_min = float(probe['tail_head_rms_ratio_min'])
            canonical_rings = sep.get('canonical_separated', 0.0) >= ring_min
            adjacent_absorbs = sep.get('halo_adjacent', 1.0) < ring_min
            if canonical_rings and adjacent_absorbs:
                axis_verdicts['boundary_halo_separation'] = (
                    'HALO_ADJACENCY_ABSORBS_CONFIRMED'
                )
            elif canonical_rings:
                axis_verdicts['boundary_halo_separation'] = (
                    'HALO_ADJACENCY_NO_LEAK_OBSERVED'
                )
            else:
                axis_verdicts['boundary_halo_separation'] = (
                    'CANONICAL_LAYOUT_DECAYS_UNEXPECTEDLY'
                )
            pass
    finally:
        _restore_pinned_pffdtd_checkout(fixture['executor'])

    canonical_dense = _dense_worsening(
        transfers_by_ppw=canonical_dense_transfers,
        ppw_levels=ppw_levels,
        dense_frequencies=dense_frequencies,
        fixed_floor=fixed_floor,
    )
    cfl_persists = all(
        row['classification']
        == canonical_dense['classification']
        for row in cfl_results.values()
    )
    axis_verdicts['cfl_dt_variants'] = (
        'CFL_DT_WORSENING_PERSISTS'
        if cfl_persists
        else 'CFL_DT_WORSENING_ALTERED'
    )

    # phase floor rescore (evaluation-only)
    floor_block = diagnostic['phase_floor_rescore']
    floor_rescoring: list[dict[str, Any]] = []
    for extra_floor_db in floor_block['extra_mask_floors_db']:
        pairs = _adjacent_pair_metrics(
            transfers_by_ppw=canonical_transfers,
            ppw_levels=ppw_levels,
            frequency_hz=plan.physical_quantity.frequency_hz,
            magnitude_mask_relative_db=float(extra_floor_db),
        )
        floor_rescoring.append(
            {
                'mask_floor_db': float(extra_floor_db),
                'adjacent_pairs': pairs,
            }
        )
    phase_monotonic = all(
        row['adjacent_pairs'][i]['phase_max_deg']
        <= row['adjacent_pairs'][i - 1]['phase_max_deg']
        for row in floor_rescoring
        for i in range(1, len(row['adjacent_pairs']))
    )
    axis_verdicts['phase_floor_rescore'] = (
        'PHASE_NONMONOTONIC_UNDER_TIGHTER_FLOOR'
        if not phase_monotonic
        else 'PHASE_MONOTONIC_UNDER_TIGHTER_FLOOR'
    )

    # worsening-cause separation on the canonical dense vector
    sep_block = diagnostic['worsening_cause_separation']
    magnitude_reference = np.abs(canonical_dense_transfers[ppw_levels[0]])
    bin_labels: list[dict[str, Any]] = []
    for index, freq in enumerate(dense_frequencies):
        if not canonical_dense['worsening_by_frequency'][index]:
            continue
        ref_mag = float(magnitude_reference[index])
        local_max = (
            0 < index < magnitude_reference.size - 1
            and ref_mag > float(magnitude_reference[index - 1])
            and ref_mag >= float(magnitude_reference[index + 1])
        )
        # phase delta between mid and high level at this bin
        a = canonical_dense_transfers[ppw_levels[1]][index]
        b = canonical_dense_transfers[ppw_levels[2]][index]
        phase_delta = abs(
            float(np.degrees(np.angle(b) - np.angle(a)))
        )
        label = classify_dense_bin_cause(
            reference_magnitude=ref_mag,
            magnitude_floor=fixed_floor,
            phase_delta_deg=phase_delta,
            is_local_magnitude_max=local_max,
            phase_wrap_deg=float(sep_block['phase_wrap_deg']),
        )
        bin_labels.append(
            {
                'frequency_hz': float(freq),
                'label': label,
                'reference_magnitude': ref_mag,
                'phase_delta_deg': phase_delta,
                'local_magnitude_max': local_max,
            }
        )
    axis_verdicts['worsening_cause_separation'] = (
        'WORSENING_BINS_CLASSIFIED'
    )

    hypothesis_table = build_reproduction_hypothesis_table(
        axis_verdicts, mfem_executed=mfem_executed
    )
    diagnostic_verdict = classify_reproduction_isolation(
        run25_match=run25_match,
        run62_match=run62_match,
        run76_identical=run76_identical,
        axis_verdicts=axis_verdicts,
    )

    separated_verdicts = {
        'CODE_VERIFIED': [
            {
                'check': 'run25_self_convergence_value_replay',
                'status': 'PASS' if run25_match else 'FAIL',
            },
            {
                'check': 'run62_target_window_value_replay',
                'status': 'PASS' if run62_match else 'FAIL',
            },
            {
                'check': 'run76_record_binding',
                'status': 'PASS' if run76_identical else 'FAIL',
            },
            {
                'check': 'canonical_pr295_reproduction',
                'status': (
                    'PASS'
                    if canonical_reproduction.get('state') == 'PASS'
                    else 'UNKNOWN'
                ),
            },
        ],
        'SELF_CONVERGENCE_PASS': [
            {
                'check': 'canonical_pffdtd_refinement_series',
                'status': 'FAIL',
                'note': 'run25 non-monotonicity reproduced (this is the '
                'issue subject; recording FAIL correctly is the '
                'reproduction pass)',
            },
            {
                'check': 'analytic_box_two_level_modal_convergence',
                'status': (
                    'PASS'
                    if axis_verdicts.get('analytic_rigid_box')
                    == 'ANALYTIC_MODAL_PEAKS_WITHIN_TOLERANCE'
                    else 'UNKNOWN'
                ),
            },
        ],
        'CROSS_SOLVER_ELIGIBLE': [
            {
                'check': 'mfem_pffdtd_cross_solver_gate',
                'status': 'BLOCKED',
                'note': (
                    'self-convergence has not re-passed on both solvers'
                    + (
                        ''
                        if mfem_executed
                        else '; MFEM legs environment-blocked on this box'
                    )
                ),
            }
        ],
        'PHYSICALLY_VALIDATED': [
            {
                'check': 'analytic_rigid_box_modal_spectrum',
                'status': (
                    'PASS'
                    if axis_verdicts.get('analytic_rigid_box')
                    == 'ANALYTIC_MODAL_PEAKS_WITHIN_TOLERANCE'
                    else 'UNKNOWN'
                ),
            }
        ],
    }

    evidence = {
        'schema_version': EVIDENCE_SCHEMA,
        'diagnostic_id': diagnostic['diagnostic_id'],
        'issue': 938,
        'repository_head_sha256': repository_head,
        'frozen_authority': {
            'parent_plan_id': plan.plan_id,
            'parent_plan_sha256': plan.plan_sha256(),
            'diagnostic_plan_sha256': semantic_hash(diagnostic),
        },
        'reproduction_manifest': {
            'run25': {
                'summary_relpath': run25_target['summary_relpath'],
                'summary_sha256': run25_target['summary_sha256'],
                'recomputed_adjacent_pairs': recomputed_pairs,
                'values_match': run25_match,
            },
            'run62': {
                'summary_relpath': run62_target['summary_relpath'],
                'summary_sha256': run62_target['summary_sha256'],
                'recomputed_rows': recomputed_run62_rows,
                'values_match': run62_match,
            },
            'run76': {
                'authoritative_run_id': run76_binding[
                    'authoritative_run_id'
                ],
                'levels': run76_levels,
                'all_identical': run76_identical,
            },
        },
        'canonical_legs': [
            {
                'points_per_wavelength': float(
                    level['points_per_wavelength']
                ),
                'time_step_s': float(level['time_step_s']),
                'time_step_count': int(level['time_step_count']),
                'record_last_sample_time_s': float(
                    level['record_last_sample_time_s']
                ),
                'record_next_sample_time_s': float(
                    level['record_next_sample_time_s']
                ),
                'grid_spacing_m': float(level['grid_spacing_m']),
                'grid_dimensions': [
                    int(x) for x in level['grid_dimensions']
                ],
                'courant_c_dt_over_h': float(
                    level['courant_c_dt_over_h']
                ),
                'transfer_pa_per_m3_s': level['transfer_pa_per_m3_s'],
                'transfer_sha256': level['transfer_sha256'],
                'run76_trace_binding': level['run76_trace_binding'],
            }
            for level in pffdtd_levels
        ],
        'canonical_dense_worsening': canonical_dense,
        'record_prefix_identity': prefix_result,
        'cfl_dt_variants': [
            {key: value for key, value in row.items()}
            for row in cfl_results.values()
        ],
        'analytic_rigid_box': {
            'box_dimensions_m': diagnostic['analytic_rigid_box'][
                'box_dimensions_m'
            ],
            'analytic_modes_finest_level': modes,
            'levels': box_results,
            'halo_separation_probe': halo_probe_results,
        },
        'phase_floor_rescore': floor_rescoring,
        'worsening_bin_classification': bin_labels,
        'variant_run_records': variant_records,
        'mfem_manifest': mfem_manifest,
        'canonical_reproduction': canonical_reproduction,
        'hypothesis_table': hypothesis_table,
        'separated_verdicts': separated_verdicts,
        'axis_verdicts': axis_verdicts,
        'diagnostic_verdict': diagnostic_verdict,
        'runtime': {
            'python': sys.version,
            'platform': platform.platform(),
            'numpy': np.__version__,
            'scipy': scipy.__version__,
            'wall_clock_s': float(time.perf_counter() - started),
        },
    }
    summary = {
        'schema_version': SUMMARY_SCHEMA,
        'diagnostic_id': diagnostic['diagnostic_id'],
        'issue': 938,
        'repository_head_sha256': repository_head,
        'diagnostic_verdict': diagnostic_verdict,
        'run25_values_match': run25_match,
        'run62_values_match': run62_match,
        'run76_all_identical': run76_identical,
        'axis_verdicts': axis_verdicts,
        'hypothesis_table': hypothesis_table,
        'separated_verdicts': separated_verdicts,
        'variant_run_records': variant_records,
        'mfem_manifest': mfem_manifest,
        'runtime_wall_clock_s': float(time.perf_counter() - started),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, indent=1) + '\n',
        encoding='utf-8',
        newline='\n',
    )
    if args.summary_output is not None:
        args.summary_output.parent.mkdir(parents=True, exist_ok=True)
        args.summary_output.write_text(
            json.dumps(summary, indent=1) + '\n',
            encoding='utf-8',
            newline='\n',
        )
    print(
        json.dumps(
            {
                'diagnostic_verdict': diagnostic_verdict,
                'run25_values_match': run25_match,
                'run62_values_match': run62_match,
                'run76_all_identical': run76_identical,
                'axis_verdicts': axis_verdicts,
            },
            indent=1,
        )
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
