#!/usr/bin/env python3
"""R130D #938: same-source, same-time integrator independent MFEM/FV comparison.

Source systems are the *independently compiled pinned MFEM* P2 FEM
mass/stiffness/point-evaluation CSR operators. Actual Gaussian volume velocity
q(t) is injected at each midpoint of the semidiscrete wave equation; it
is neither impulse-response postprocessing nor arbitrary transfer fitting.

The experimental FV response was actually driven in a separate preregistered
experiment and is verified against its immutable evidence and physical spec.
No canonical frozen R130D acceptance state changes.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
from pathlib import Path
import platform
import sys
import time
from typing import Any

import numpy as np
from scipy import sparse
from scipy.sparse.linalg import factorized

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

SCHEMA = "htdt.r130d.fv-mfem-same-drive-comparison-evidence-1"
N = 1000
DT = 0.00025
DURATION = 0.25
FREQ = (40.0, 80.0)
CENTER = 0.012
SIGMA = 0.003
C = 343.2
RHO = 1.2


def read_payload_and_hash(path: Path, *, expected_sha256: str) -> dict:
    raw = path.read_bytes()
    blob = gzip.decompress(raw) if path.suffix == ".gz" else raw
    digest = hashlib.sha256(blob).hexdigest()
    if digest != expected_sha256:
        raise ValueError(f"independent MFEM sparse source digest mismatch: {path.name}")
    return json.loads(blob)


def csr(data: dict, n: int) -> sparse.csr_matrix:
    if (data.get("rows"), data.get("cols")) != (n, n):
        raise ValueError("MFEM matrix shape changed")
    mat = sparse.csr_matrix((
        np.asarray(data["values"], dtype=np.float64),
        np.asarray(data["column_indices"], dtype=np.int32),
        np.asarray(data["row_offsets"], dtype=np.int32),
    ), shape=(n, n))
    if mat.nnz != data["nnz"] or not np.all(np.isfinite(mat.data)):
        raise ValueError("MFEM invalid sparse matrix")
    return mat


def validate_mfem_system(doc: dict, *, refinement: int, ndofs: int, plan: dict) -> None:
    attrs = {
        "schema_version": "htdt.r130d.mfem-sloped-system-1",
        "geometry": "exact-eight-vertex-sloped-polyhedron",
        "boundary_model": "natural_neumann_rigid",
        "primary_field": "velocity_potential_phi",
        "source_normalization": "volume_velocity_m3_s",
        "governing_equation": "M*phi_tt+Kc2*phi=c^2*b*q",
        "uniform_refinements": refinement,
        "ndofs": ndofs,
        "order": 2,
    }
    for k, expected in attrs.items():
        if doc.get(k) != expected:
            raise ValueError(f"MFEM metadata {k} changed from independent pinned source")
    if abs(float(doc["base_volume_m3"]) - 56.0) > 1e-10:
        raise ValueError("MFEM analytic room volume differs")
    for name, expected in (
        ("sound_speed_m_s", C),
        ("density_kg_m3", RHO),
    ):
        if not math.isclose(float(doc[name]), expected, abs_tol=1e-10):
            raise ValueError(f"MFEM {name} changed")
    for name, expected in (
        ("source_position_m", plan["source_xyz_m"]),
        ("receiver_position_m", plan["receiver_xyz_m"]),
    ):
        if not np.array_equal(np.asarray(doc[name]), np.asarray(expected)):
            raise ValueError(f"MFEM physical point coordinate {name} changed")


def solve_mfem_level(system_path: Path, *, refinement: int,
                     plan: dict) -> dict[str, Any]:
    expected = plan["mfem_sparse_export_sha256_by_refinement"][str(refinement)]
    doc = read_payload_and_hash(system_path, expected_sha256=expected)
    ndofs = int(plan["mfem_dofs"][refinement - 1])
    validate_mfem_system(doc, refinement=refinement, ndofs=ndofs, plan=plan)
    mass = csr(doc["mass_matrix"], ndofs)
    stiffness = csr(doc["stiffness_c2_matrix"], ndofs)
    b = np.asarray(doc["source_functional"], dtype=np.float64)
    receiver = np.asarray(doc["receiver_functional"], dtype=np.float64)
    if (b.shape != (ndofs,) or receiver.shape != (ndofs,)
            or not np.all(np.isfinite(b)) or not np.all(np.isfinite(receiver))
            or np.linalg.norm(b) <= 0 or np.linalg.norm(receiver) <= 0):
        raise ValueError("MFEM point functional malformed")
    if (np.max(np.abs((mass-mass.T).data),initial=0.0) > 1e-11
            or np.max(np.abs((stiffness-stiffness.T).data),initial=0.0) > 1e-6):
        raise ValueError("MFEM sparse operators unexpectedly nonsymmetric")
    # Preserve zero normal flux of natural Neumann boundary: stiffness annihilates constants.
    constant_mode = float(np.linalg.norm(stiffness @ np.ones(ndofs)))
    if constant_mode > 1e-5 * max(1.0,float(np.linalg.norm(stiffness.data))):
        raise ValueError("MFEM natural Neumann constant mode residual")
    a = mass + (DT*DT/4.0)*stiffness
    bmat = mass - (DT*DT/4.0)*stiffness
    linear_solve = factorized(a.tocsc())
    u = np.zeros(ndofs, dtype=np.float64)
    vel = np.zeros(ndofs, dtype=np.float64)
    t = (np.arange(N,dtype=np.float64)+0.5)*DT
    q = np.exp(-0.5*((t-CENTER)/SIGMA)**2)
    p = np.empty(N,dtype=np.float64)
    started=time.perf_counter()
    for i in range(N):
        rhs = bmat @ u + DT * (mass @ vel) + 0.5*DT*DT*(C*C*q[i])*b
        u1 = linear_solve(rhs)
        v1 = 2.0*(u1-u)/DT-vel
        p[i] = RHO*float(receiver @ ((vel+v1)/2))
        u,vel = u1,v1
    kernel = np.exp(2j*np.pi*np.asarray(FREQ)[:,None]*t[None,:])
    p_spectrum = DT*(kernel@p)
    q_spectrum = DT*(kernel@q)
    if np.any(abs(q_spectrum)<1e-12):
        raise ValueError("MFEM source spectrum too small")
    transfer = p_spectrum/q_spectrum
    if not np.all(np.isfinite(transfer)):
        raise ValueError("non-finite MFEM transfer")
    return {
        "refinement": refinement,
        "dofs": ndofs,
        "element_count": int(doc["elements"]),
        "operator_sha256": expected,
        "actual_integrated_gaussian_source": True,
        "discrete_natural_neumann_constant_mode_residual_norm":constant_mode,
        "source_complex_spectrum":[[float(x.real),float(x.imag)] for x in q_spectrum],
        "transfer_complex_40_80_hz": [[float(x.real),float(x.imag)] for x in transfer],
        "runtime_seconds": time.perf_counter()-started,
    }


def metrics(candidate: list[list[float]], reference: list[list[float]]) -> dict:
    a = np.asarray([complex(*z) for z in candidate])
    b = np.asarray([complex(*z) for z in reference])
    if a.shape != (2,) or b.shape != (2,) or np.any(abs(b) < 1e-30):
        raise ValueError("invalid complex transfer reference")
    return {
        "normalized_complex_l2": float(np.linalg.norm(a-b)/np.linalg.norm(b)),
        "complex_relative_by_hz": [float(abs(x-y)/abs(y)) for x,y in zip(a,b)],
        "magnitude_relative_by_hz": [float(abs(abs(x)-abs(y))/abs(y)) for x,y in zip(a,b)],
        "phase_difference_deg_by_hz": [float(abs(np.angle(x/y,deg=True))) for x,y in zip(a,b)],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--mfem-systems", type=Path, required=True)
    parser.add_argument("--fv-evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    fv = json.loads(args.fv_evidence.read_text(encoding="utf-8"))
    if plan["schema_version"] != "htdt.r130d.fv-mfem-same-drive-comparison-plan-1":
        raise ValueError("unregistered plan")
    if (np.__version__ != "1.26.4" or plan["time"] != {
        "dt_s":DT,"duration_s":DURATION,"steps":N}
        or plan["frequency_hz"]!=list(FREQ)
        or plan["source"]["waveform"]!=
            "exp(-0.5*((t-0.012)/0.003)^2)"
        or plan["mfem_frozen_refinements"]!=[1,2,3]
        or plan["fv_grid_levels"]!=[20,24,28,32]
        or plan["reference_mfem_source_sha"]!=
            "d964264cdb9a13e94a201b6c236c7721e0c8765f"):
        raise ValueError("numerical plan differs")
    if (fv["schema"] != "htdt.r130d.embedded-fv-actual-bandlimited-source-1"
            or fv["actual_driven_solver_execution"] is not True
            or fv["physical_model_change_from_frozen_run25"] is not True
            or fv["production_ready"] is not False
            or [v["grid_cells_per_axis"] for v in fv["levels"]] != [20,24,28,32]):
        raise ValueError("FV source authority not the preregistered actual driven study")
    cp=fv["physical_contract"]
    if (cp["source_xyz_m"]!=plan["source_xyz_m"]
            or cp["receiver_xyz_m"]!=plan["receiver_xyz_m"]
            or cp["frequency_hz"] != list(FREQ)
            or cp["duration_s"]!=DURATION
            or cp["time_step_s"]!=DT
            or cp["Gaussian_volume_velocity_center_s"]!=CENTER
            or cp["Gaussian_volume_velocity_sigma_s"]!=SIGMA):
        raise ValueError("FV physical/time/source contract differs from independent MFEM")
    levels=[]
    for refinement in (1,2,3):
        print("RUN INDEPENDENT SAME-SOURCE MFEM",refinement,flush=True)
        system = args.mfem_systems/"work"/f"mfem-r{refinement}"/"system.json"
        if not system.exists():
            system = args.mfem_systems/f"mfem-r{refinement}.json.gz"
        row=solve_mfem_level(
            system, refinement=refinement,plan=plan,
        )
        levels.append(row)
        print("DONE",refinement,row["transfer_complex_40_80_hz"],flush=True)
    mfem_pairs = [
        {"coarse_refinement":a["refinement"],"fine_refinement":b["refinement"],
         **metrics(a["transfer_complex_40_80_hz"],b["transfer_complex_40_80_hz"])}
        for a,b in zip(levels,levels[1:])
    ]
    fvl=fv["levels"]
    fv_pairs=[
        {"coarse_n":a["grid_cells_per_axis"],"fine_n":b["grid_cells_per_axis"],
         **metrics(a["transfer_complex_40_80_hz"],b["transfer_complex_40_80_hz"])}
        for a,b in zip(fvl,fvl[1:])
    ]
    between = [
        {"fv_n":v["grid_cells_per_axis"],"mfem_r":3,
         **metrics(v["transfer_complex_40_80_hz"],levels[-1]["transfer_complex_40_80_hz"])}
        for v in fvl
    ]
    output={
        "schema_version":SCHEMA,
        "plan":plan,
        "runtime":{"platform":platform.platform(),"numpy":np.__version__,
                   "python":sys.version.split()[0]},
        "mfem_source":"actual midpoint-driven PINNED independent MFEM P2 exported CSR",
        "fv_source":"pre-registered actual midpoint-driven cut-cell FV frozen evidence",
        "mfem_levels":levels,
        "fv_levels":fvl,
        "mfem_adjacent":mfem_pairs,
        "fv_adjacent":fv_pairs,
        "cross_solver_vs_mfem_r3":between,
        "gate":{
            "same_source_cross_solver_diagnostic_executed":True,
            "canonical_r130d_self_convergence":"SELF_CONVERGENCE_FAILED",
            "original_mfem_impulse_self_convergence":"SELF_CONVERGENCE_FAILED",
            "production_ready":False,"physical_validation":"NOT_VALIDATED",
            "cross_solver_production_eligible":False,
        },
    }
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(output,indent=2)+"\n",encoding="utf-8")
    print("MFEM ADJACENT",json.dumps(mfem_pairs),flush=True)
    print("FV ADJACENT",json.dumps(fv_pairs),flush=True)
    print("CROSS MFEM r3",json.dumps(between),flush=True)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
