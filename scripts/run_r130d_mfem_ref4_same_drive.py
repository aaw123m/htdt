#!/usr/bin/env python3
"""R130D independently compiled MFEM P2 refinement 4 under fixed Gaussian q(t).

Pre-registered solver choice: no dense generalized eigendecomposition at
35,937 DOFs. The physical PDE and implicit-midpoint time quadrature are
identical to the existing actual-driven FV and independent r1/r2/r3 runs.
Conjugate gradients with predeclared Jacobi preconditioning and checked
residuals solve the SPD implicit-midpoint matrix.

Never marks the original R130D impulse production gates as passed.
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

import numpy as np
from scipy import sparse
from scipy.sparse.linalg import cg, LinearOperator

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from run_r130d_fv_mfem_same_drive_comparison import metrics, validate_mfem_system

PLAN_SCHEMA = "htdt.r130d.same-source-independent-mfem-r4-experiment-1"
PLAN_SCHEMA_V2 = "htdt.r130d.same-source-independent-mfem-r4-solver-revision-2"
RESULT_SCHEMA = "htdt.r130d.mfem-r4-same-source-independent-result-1"


def matrix_from_csr(doc: dict, *, ndofs: int, max_nnz: int) -> sparse.csr_matrix:
    if (int(doc["rows"]), int(doc["cols"])) != (ndofs, ndofs):
        raise ValueError("source MFEM matrix shape changed")
    if int(doc["nnz"]) > max_nnz:
        raise ValueError("pinned memory/nnz bound exceeded")
    x = sparse.csr_matrix(
        (np.asarray(doc["values"], dtype=np.float64),
         np.asarray(doc["column_indices"], dtype=np.int32),
         np.asarray(doc["row_offsets"], dtype=np.int32)),
        shape=(ndofs, ndofs),
    )
    if x.nnz != int(doc["nnz"]) or not np.all(np.isfinite(x.data)):
        raise ValueError("malformed MFEM sparse operator")
    return x


def run(*, plan: dict, mfem_system_path: Path, previously_measured: dict) -> dict:
    if plan.get("schema_version") not in (PLAN_SCHEMA, PLAN_SCHEMA_V2):
        raise ValueError("MFEM r4 solver experiment was not preregistered")
    if (plan["mfem_git_source_pin"] !=
            "d964264cdb9a13e94a201b6c236c7721e0c8765f"):
        raise ValueError("MFEM source pin changed")
    if (plan["dt_s"], plan["duration_s"], plan["steps"]) != (
        0.00025, 0.25, 1000,
    ):
        raise ValueError("time/record contract changed")
    if (plan["frequencies_hz"] != [40, 80]
            or plan["source_q_m3_s"]["waveform"] !=
               "exp(-0.5*((t-0.012)/0.003)^2)"
            or plan["new_mfem_uniform_refinements"] != 4
            or plan["expected_tetra_elements"] != 24576
            or plan["expected_dofs"] != 35937
            or plan["polynomial_order"] != 2):
        raise ValueError("MFEM physics/mesh/drive plan altered")
    gate = plan["gate"]
    if (gate["production_ready"] is not False
            or gate["independent_cross_solver_qualified"] is not False
            or gate["canonical_pffdtd"] != "SELF_CONVERGENCE_FAILED"
            or gate["canonical_mfem_impulse"] != "SELF_CONVERGENCE_FAILED"):
        raise ValueError("MFEM r4 plan must fail closed")
    linspec = plan["linear_solver"]
    expected_atol = 0 if plan["schema_version"] == PLAN_SCHEMA_V2 else 1e-12
    if linspec != {
        "rtol":1e-11, "atol":expected_atol, "maxiter":350,
        "preconditioner":"Jacobi diagonal A=M+dt^2/4K",
        "true_residual_relative_max":1e-8,
    }:
        raise ValueError("MFEM linear numerical solver contract changed")
    resources = plan["resources"]
    file_bytes = mfem_system_path.read_bytes()
    blob_bytes = (gzip.decompress(file_bytes)
                  if mfem_system_path.suffix == '.gz' else file_bytes)
    if len(blob_bytes) > resources["max_json_bytes"]:
        raise ValueError("independent MFEM system exceeds predeclared JSON limit")
    system_digest = hashlib.sha256(blob_bytes).hexdigest()
    if plan["schema_version"] == PLAN_SCHEMA_V2 and (
        system_digest != plan.get("independent_ref4_system_sha256")
    ):
        raise ValueError("independent MFEM r4 matrix does not match preregistered source hash")
    doc = json.loads(blob_bytes)
    ndofs = plan["expected_dofs"]
    base_plan = {"source_xyz_m":plan["source_xyz_m"],
                 "receiver_xyz_m":plan["receiver_xyz_m"]}
    validate_mfem_system(doc, refinement=4,ndofs=ndofs,plan=base_plan)
    if doc["elements"] != plan["expected_tetra_elements"]:
        raise ValueError("unexpected FEM element count")
    mass = matrix_from_csr(
        doc["mass_matrix"],ndofs=ndofs,
        max_nnz=resources["max_nnz_per_matrix"],
    )
    stiff = matrix_from_csr(
        doc["stiffness_c2_matrix"],ndofs=ndofs,
        max_nnz=resources["max_nnz_per_matrix"],
    )
    if (np.max(np.abs((mass-mass.T).data),initial=0) > 1e-11
            or np.max(np.abs((stiff-stiff.T).data),initial=0) > 1e-5):
        raise ValueError("independent pinned FEM matrices asymmetric")
    b = np.asarray(doc["source_functional"],dtype=np.float64)
    r = np.asarray(doc["receiver_functional"],dtype=np.float64)
    if b.shape != (ndofs,) or r.shape != (ndofs,):
        raise ValueError("invalid delta functionals")
    if not all(np.all(np.isfinite(x)) and np.linalg.norm(x)>0 for x in (b,r)):
        raise ValueError("empty source/receiver functional")
    # Independent source assembly and natural Neumann constant wave mode.
    constant_residual = np.linalg.norm(stiff @ np.ones(ndofs))
    if constant_residual > 1e-5*max(1,np.linalg.norm(stiff.data)):
        raise ValueError("MFEM rigid-wall constant mode not conserved")
    delta_t = plan["dt_s"]
    a = mass + (delta_t**2 / 4.0)*stiff
    op_b = mass - (delta_t**2 / 4.0)*stiff
    diag = a.diagonal()
    if np.any(diag <= 0) or not np.all(np.isfinite(diag)):
        raise ValueError("FEM midpoint matrix diagonal nonpositive")
    inv = 1 / diag
    preconditioner = LinearOperator(
        (ndofs,ndofs),matvec=lambda x:inv*x,dtype=np.float64,
    )
    steps = plan["steps"]
    tm = (np.arange(steps,dtype=np.float64) + 0.5)*delta_t
    q = np.exp(-0.5*((tm-0.012)/0.003)**2)
    phi = np.zeros(ndofs,dtype=np.float64)
    vel = np.zeros_like(phi)
    pressure = np.empty(steps,dtype=np.float64)
    iterations = []
    worst_residual = 0.0
    begin = time.perf_counter()
    for i in range(steps):
        force = (plan["c_m_s"]**2 * q[i])*b
        rhs = op_b @ phi + delta_t*(mass @ vel) + (delta_t**2/2)*force
        steps_counter = [0]
        def count_step(_x):
            steps_counter[0] += 1
        next_phi, status = cg(
            a,rhs,x0=(phi+delta_t*vel),M=preconditioner,
            rtol=linspec["rtol"],atol=linspec["atol"],
            maxiter=linspec["maxiter"],callback=count_step,
        )
        if status != 0 or not np.all(np.isfinite(next_phi)):
            raise RuntimeError(f"MFEM r4 preconditioned CG nonconverged at {i}: {status}")
        residual = float(np.linalg.norm(a @ next_phi-rhs) /
                         max(np.linalg.norm(rhs),1e-15))
        if residual > linspec["true_residual_relative_max"]:
            raise RuntimeError(f"MFEM r4 true residual exceeds contract: {residual}")
        worst_residual = max(residual,worst_residual)
        iterations.append(steps_counter[0])
        next_vel = 2*(next_phi-phi)/delta_t - vel
        pressure[i] = plan["density_kg_m3"]*float(r @ ((vel+next_vel)/2))
        phi,vel = next_phi,next_vel
        if i % 250 == 249:
            print("MFEM r4",i+1,"of",steps,"CG max",max(iterations),
                  "elapsed seconds",round(time.perf_counter()-begin,2),flush=True)
    hz=np.asarray(plan["frequencies_hz"],dtype=np.float64)
    kernel=np.exp(2j*np.pi*hz[:,None]*tm[None,:])
    Q = delta_t * kernel@q
    P = delta_t * kernel@pressure
    transfer = P/Q
    if not np.all(np.isfinite(transfer)) or np.any(abs(Q)<1e-12):
        raise RuntimeError("invalid r4 source-normalized finite time spectrum")
    old=previously_measured
    if (old["schema_version"] !=
        "htdt.r130d.fv-mfem-same-drive-comparison-evidence-1"
        or old["gate"]["production_ready"] is not False
        or old["mfem_levels"][-1]["dofs"]!=4913
        or old["mfem_levels"][-1]["operator_sha256"]!=
            plan["baseline_mfem_r3_json_sha256"]
        or old["fv_levels"][-1]["grid_cells_per_axis"]!=32):
        raise ValueError("r3 and FV32 independent comparison authority changed")
    pair = [[float(z.real),float(z.imag)] for z in transfer]
    r3 = old["mfem_levels"][-1]["transfer_complex_40_80_hz"]
    fv = old["fv_levels"][-1]["transfer_complex_40_80_hz"]
    return {
        "schema_version":RESULT_SCHEMA,
        "plan_schema_version":plan["schema_version"],
        "mfem_source_sha":plan["mfem_git_source_pin"],
        "mfem_sparse_export_sha256":system_digest,
        "mfem_refinement":4,
        "mfem_element_count":doc["elements"],
        "mfem_dofs":ndofs,
        "physical_source":"actual 3ms Gaussian volume velocity via c^2 b q(t)",
        "source_normalization":"midpoint finite-record P_T/Q_T",
        "transfer_complex_40_80_hz":pair,
        "source_complex_spectrum":[[float(x.real),float(x.imag)] for x in Q],
        "fem_r3_to_r4_metrics":metrics(r3,pair),
        "fv_n32_to_mfem_r4_metrics":metrics(fv,pair),
        "reference_fv_n32_to_mfem_r3_metrics":old["cross_solver_vs_mfem_r3"][-1],
        "actual_sparse_solver":{
            "algorithm":"sparse symmetric Jacobi-PCG implicit midpoint",
            "max_conjugate_gradient_iterations":max(iterations),
            "mean_conjugate_gradient_iterations":float(np.mean(iterations)),
            "max_true_relative_linear_residual":worst_residual,
            "elapsed_s":time.perf_counter()-begin,
        },
        "runtime":{"platform":platform.platform(),"python":sys.version.split()[0],
                   "numpy":np.__version__},
        "gate":{
            "candidate_gaussian_source_evidence_only":True,
            "canonical_pffdtd":"SELF_CONVERGENCE_FAILED",
            "canonical_mfem_impulse":"SELF_CONVERGENCE_FAILED",
            "cross_solver_production_eligible":False,
            "production_ready":False,
            "physical_validation":"NOT_VALIDATED",
        },
    }


def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--plan",type=Path,required=True)
    parser.add_argument("--ref4-system",type=Path,required=True)
    parser.add_argument("--comparison-evidence",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    a=parser.parse_args()
    if np.__version__!="1.26.4":
        raise ValueError("pinned NumPy for independent MFEM r4 has changed")
    plan=json.loads(a.plan.read_text(encoding="utf-8"))
    reference=json.loads(a.comparison_evidence.read_text(encoding="utf-8"))
    result=run(plan=plan,mfem_system_path=a.ref4_system,
               previously_measured=reference)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n",
                        encoding="utf-8")
    print("FINAL MFEM r3→r4",json.dumps(result["fem_r3_to_r4_metrics"]),flush=True)
    print("FINAL FV32→MFEM r4",json.dumps(result["fv_n32_to_mfem_r4_metrics"]),flush=True)


if __name__=="__main__":
    main()
