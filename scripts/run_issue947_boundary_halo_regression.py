"""Issue #947 boundary-halo regression driver.

Re-runs the #938 analytic-box probe under four combinations —
wall_offset_cells in {1 (halo-adjacent), 2 (canonical separated)} x
{untreated engine, halo-separation treatment applied} — and records the
frozen metrics:

* ``tail_head_rms_ratio`` (same quarter convention as the committed
  probe; ring threshold 0.5, declared by the frozen plan)
* ``pressure_trace_sha256``

Expected outcomes, all recorded honestly (never forced):
* untreated wall@1 -> ratio < 0.5 (pre-fix anomaly reproduced)
* treated wall@1 -> ratio >= 0.5 (defect removed)
* treated wall@2 bit-identical to untreated wall@2 (treatment inert on
  an already-separated mask)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
sys.path.insert(0, str(_SCRIPT_DIR.parent / 'backend' / 'src'))

from htdt.acoustic_pffdtd_adapter import (  # noqa: E402
    apply_pffdtd_runtime_compatibility_patches,
    pffdtd_git_head,
    pffdtd_velocity_potential_to_pressure_trace,
    recombine_pffdtd_receiver_traces,
)
from htdt.pffdtd_boundary_halo import (  # noqa: E402
    apply_boundary_halo_separation,
    assess_boundary_halo_adjacency,
)
from run_r130d_reproduction_isolation_diagnostic import (  # noqa: E402
    _write_analytic_box_assets,
)

# Frozen probe contract (r130d_reproduction_isolation_diagnostic_plan.json
# analytic_rigid_box.halo_separation_probe) — do not retune after seeing
# results.
PROBE = {
    'room_dimensions_m': {'x': 2.4, 'y': 1.6, 'z': 1.4},
    'grid_spacing_m': 0.2,
    'record_seconds': 0.1,
    'source_xyz_m': (0.7, 0.7, 0.65),
    'receiver_xyz_m': (1.9, 1.1, 0.9),
    'courant_number': 0.5,
    'speed_of_sound_m_s': 343.2,
    'density_kg_m3': 1.2,
    'tail_head_rms_ratio_min': 0.5,
    'solver_threads': 1,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pffdtd-root', required=True, type=Path)
    parser.add_argument('--work-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument(
        '--expected-commit',
        default='aa319f6c86517cb95aabfae8656277da62c3ead5',
    )
    return parser


def _run_leg(
    *,
    sim_engine_cls,
    work_dir: Path,
    wall_offset_cells: int,
    treat: bool,
) -> dict[str, Any]:
    import h5py

    sim_dir = work_dir / 'sim'
    meta = _write_analytic_box_assets(
        sim_dir=sim_dir,
        box_dims_m=PROBE['room_dimensions_m'],
        grid_h=PROBE['grid_spacing_m'],
        time_step_s=(
            PROBE['courant_number']
            * PROBE['grid_spacing_m']
            / PROBE['speed_of_sound_m_s']
        ),
        record_duration_s=PROBE['record_seconds'],
        source_position_m=PROBE['source_xyz_m'],
        receiver_position_m=PROBE['receiver_xyz_m'],
        speed_of_sound_m_s=PROBE['speed_of_sound_m_s'],
        wall_offset_cells=wall_offset_cells,
    )
    with h5py.File(sim_dir / 'vox_out.h5', 'r') as handle:
        bn_ixyz = handle['bn_ixyz'][...]
        mat_bn = handle['mat_bn'][...]
        dims = (
            int(handle['Nx'][()]),
            int(handle['Ny'][()]),
            int(handle['Nz'][()]),
        )
    assessment = assess_boundary_halo_adjacency(
        bn_ixyz=bn_ixyz, mat_bn=mat_bn, dimensions=dims
    )
    engine = sim_engine_cls(
        sim_dir, energy_on=False, nthreads=PROBE['solver_threads']
    )
    engine.load_h5_data()
    engine.setup_mask()
    treatment = None
    if treat:
        treatment = apply_boundary_halo_separation(engine)
    engine.allocate_mem()
    engine.set_coeffs()
    engine.checks()
    engine.run_all(nsteps=int(engine.Nt))
    u_out = np.asarray(engine.u_out, dtype=np.float64)
    potential = recombine_pffdtd_receiver_traces(
        u_out, meta['out_alpha'], receiver_count=1, nt=int(engine.Nt)
    )[0]
    pressure = pffdtd_velocity_potential_to_pressure_trace(
        potential,
        time_step_s=float(engine.Ts),
        density_kg_m3=PROBE['density_kg_m3'],
    )
    quarter = max(8, pressure.size // 4)
    head_rms = float(np.sqrt(np.mean(pressure[:quarter] ** 2)))
    tail_rms = float(np.sqrt(np.mean(pressure[-quarter:] ** 2)))
    return {
        'wall_offset_cells': int(wall_offset_cells),
        'treatment_applied': bool(treat),
        'assessment': assessment.model_dump(mode='json'),
        'treatment': (
            treatment.model_dump(mode='json') if treatment is not None else None
        ),
        'pressure_trace_sha256': hashlib.sha256(
            pressure.tobytes()
        ).hexdigest(),
        'tail_head_rms_ratio': (
            tail_rms / head_rms if head_rms > 0 else 0.0
        ),
        'time_step_s': float(engine.Ts),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    started = time.perf_counter()
    if pffdtd_git_head(args.pffdtd_root) != args.expected_commit:
        raise RuntimeError('pffdtd checkout is not the pinned commit')
    compatibility = apply_pffdtd_runtime_compatibility_patches(
        args.pffdtd_root
    )
    sys.path.insert(0, str(args.pffdtd_root / 'python'))
    from fdtd.sim_fdtd import SimEngine  # noqa: E402

    legs: dict[str, dict[str, Any]] = {}
    for offset in (1, 2):
        for treat in (False, True):
            label = f'wall@{offset}' + ('_treated' if treat else '_untreated')
            legs[label] = _run_leg(
                sim_engine_cls=SimEngine,
                work_dir=args.work_root / label,
                wall_offset_cells=offset,
                treat=treat,
            )
    ring_min = PROBE['tail_head_rms_ratio_min']
    verdicts = {
        'prefix_anomaly_reproduced': legs['wall@1_untreated'][
            'tail_head_rms_ratio'
        ]
        < ring_min,
        'fix_removes_anomaly': legs['wall@1_treated']['tail_head_rms_ratio']
        >= ring_min,
        'treatment_inert_on_separated_mask': (
            legs['wall@2_treated']['pressure_trace_sha256']
            == legs['wall@2_untreated']['pressure_trace_sha256']
        ),
        'excluded_count_matches_assessment': (
            legs['wall@1_treated']['treatment']['excluded_node_count']
            == legs['wall@1_treated']['assessment'][
                'abc_ring_boundary_node_count'
            ]
        ),
    }
    evidence = {
        'schema_version': 'htdt.issue947.boundary-halo-regression-evidence-1',
        'issue': 947,
        'frozen_probe': PROBE,
        'pffdtd_commit': args.expected_commit,
        'compatibility_patch': compatibility,
        'legs': legs,
        'verdicts': verdicts,
        'runtime_wall_clock_s': float(time.perf_counter() - started),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, indent=1) + '\n', encoding='utf-8', newline='\n'
    )
    print(json.dumps(verdicts, indent=1))
    return 0 if all(verdicts.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
