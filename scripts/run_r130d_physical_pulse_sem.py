#!/usr/bin/env python3
"""Qualify the user-selected finite-band R130D physical numerical model.

Conforming degree-four SEM, actual midpoint Gaussian input, complete modal
evolution, independent exact causal convolution, and independently compiled
MFEM P2 matrices. This is a separate contract from original singular q0.
"""
from __future__ import annotations
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
from scipy.sparse.linalg import LinearOperator, cg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend/src"))
from htdt.r130d_boundary_fitted_sem import build_boundary_fitted_sem, axis_functional, all_mass_normalized_modes
from htdt.r130d_smooth_pulse import exact_finite_gaussian_pressure_modes, all_mode_midpoint_gaussian_trace
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_fv_mfem_same_drive_comparison import csr, validate_mfem_system

PLAN = "benchmarks/acoustics/r130d_physical_pulse_sem_plan_2026-10-10.json"
PLAN_COMMIT = "046b964"
HASHES = {2: "e61410d4a69000c39975762b9986008974353953f7d8d9f100f9953a33c5ecb4",
          3: "6a43a624223fa512152059d103c4d841723fa1266ea8a22842322fb50db42511",
          4: "e1c67d02db77a6e8775a0a59d8e75fa996434d58f8efae077f4cec2cf2c831ed"}
DOFS = {2: 729, 3: 4913, 4: 35937}
METRICS = ("complex_rms_relative", "magnitude_max_relative", "phase_max_deg")


def pairs(z):
    return [[float(v.real), float(v.imag)] for v in z]


def unpairs(z):
    a = np.asarray(z, float)
    return a[:,0]+1j*a[:,1]


def scores(coarse, fine):
    return compare_complex_transfer(reference=fine, candidate=coarse,
        frequency_hz=[40,80], magnitude_mask_relative_db=-50).model_dump(mode="json")


def passes(score, limits):
    return all(score[k] <= limits[k] for k in METRICS)


def monotone(rows):
    return all(all(b[k] < a[k] for k in METRICS) for a,b in zip(rows,rows[1:]))


def verify_plan():
    raw = (ROOT / PLAN).read_bytes().replace(b"\r\n", b"\n")
    committed = subprocess.check_output(["git", "-C", str(ROOT), "show", PLAN_COMMIT+":"+PLAN])
    if committed.replace(b"\r\n", b"\n") != raw:
        raise ValueError("finite-band physical plan changed after registration")
    subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", PLAN_COMMIT,
                    "origin/feat/r130d-embedded-neumann-fv-20261009"], check=True)
    plan = json.loads(raw)
    return plan, hashlib.sha256(raw).hexdigest()


def physical_point_coupling(fem, xvectors, yvectors, position):
    x,y,z = position
    ex = axis_functional(fem.x, x)
    ey = axis_functional(fem.y, y)
    ee = axis_functional(fem.eta, z/(4.-y/4.))
    yz = np.outer(ey,ee).ravel()
    # Unit physical Dirac functional, with no spatial blur or grid rounding.
    np.testing.assert_allclose(ex @ fem.x.nodes, x, atol=2e-12, rtol=0)
    np.testing.assert_allclose(yz @ fem.yz_positions, [y,z], atol=2e-12, rtol=0)
    return ex @ xvectors, yz @ yvectors


def sem_case(ppw, plan, cache, traces):
    started = time.perf_counter()
    h = plan["c_m_s"]/(100*ppw)
    fem = build_boundary_fitted_sem(h, degree=plan["degree"])
    xl,xv,xproof = all_mass_normalized_modes(fem.x.mass,fem.x.stiffness)
    yl,yv,yproof = all_mass_normalized_modes(fem.yz_mass,fem.yz_stiffness)
    sx,sy = physical_point_coupling(fem,xv,yv,plan["source_xyz_m"])
    rx,ry = physical_point_coupling(fem,xv,yv,plan["receiver_xyz_m"])
    lam = (xl[:,None]+yl[None,:]).ravel()
    cp = np.outer(sx*rx,sy*ry).ravel()
    source = plan["source"]
    kwargs = dict(record_s=plan["record_s"], center_s=source["center_s"],
                  sigma_s=source["sigma_s"], c_m_s=plan["c_m_s"], rho_kg_m3=plan["rho_kg_m3"])
    steps = int(np.ceil(plan["record_s"]/(h/(np.sqrt(3)*plan["c_m_s"]))))
    actual,t,p,q = all_mode_midpoint_gaussian_trace(lam,cp,steps,**kwargs)
    exact = exact_finite_gaussian_pressure_modes(lam,cp,**kwargs).sum(axis=1)
    cache.mkdir(parents=True,exist_ok=True)
    np.savez_compressed(cache/f"ppw{ppw}.npz",lam=lam,coupling=cp)
    traces.mkdir(parents=True,exist_ok=True)
    np.savetxt(traces/f"ppw{ppw}.csv", np.column_stack((t,q,p)),delimiter=",",
               header="time_s,source_volume_velocity_m3_s,receiver_pressure_Pa",comments="")
    return {"ppw": ppw, "elements_each_axis": fem.x.elements,
            "x_dofs": len(xl), "yz_dofs": len(yl), "all_3d_modes": len(lam),
            "volume_m3": float(fem.x.mass.sum()*fem.yz_mass.sum()),
            "minimum_positive_yz_mass": float(min(fem.yz_mass)),
            "x_eigenproof": xproof, "yz_eigenproof": yproof,
            "steps": steps, "dt_s": plan["record_s"]/steps,
            "actual_midpoint_signed_40_80": pairs(actual), "exact_causal_signed_40_80": pairs(exact),
            "temporal_error_at_this_grid": scores(pairs(actual),pairs(exact)),
            "elapsed_s": time.perf_counter()-started}


def independent_mfem_case(refinement, plan, system_root):
    blob = gzip.decompress((system_root/f"mfem-r{refinement}.json.gz").read_bytes())
    digest = hashlib.sha256(blob).hexdigest()
    if digest != HASHES[refinement]:
        raise ValueError("independently compiled MFEM export SHA changed")
    doc = json.loads(blob)
    n = DOFS[refinement]
    validate_mfem_system(doc, refinement=refinement, ndofs=n, plan=plan)
    M,K = csr(doc["mass_matrix"],n),csr(doc["stiffness_c2_matrix"],n)
    src,rec = np.asarray(doc["source_functional"],float),np.asarray(doc["receiver_functional"],float)
    if abs(M.sum()-56.) > 1e-8 or abs(src.sum()-1.) > 1e-10 or abs(rec.sum()-1.) > 1e-10:
        raise ValueError("independent physical MFEM mass/point normalization invalid")
    cfg = plan["independent_MFEM"]
    steps, T = cfg["steps"],plan["record_s"]
    dt = T/steps
    A,B = M+dt*dt/4*K, M-dt*dt/4*K
    invdiag = 1./A.diagonal()
    J = LinearOperator((n,n),matvec=lambda x:invdiag*x,dtype=float)
    times = (np.arange(steps)+.5)*dt
    q = np.exp(-.5*((times-plan["source"]["center_s"])/plan["source"]["sigma_s"])**2)
    phi,velocity = np.zeros(n),np.zeros(n)
    pressure = np.empty(steps)
    max_residual,max_iterations = 0.,0
    started = time.perf_counter()
    for i in range(steps):
        rhs = B @ phi+dt*(M @ velocity)+dt*dt/2*plan["c_m_s"]**2*src*q[i]
        count = [0]
        def track(_): count[0] += 1
        next_phi,status = cg(A,rhs,x0=phi+dt*velocity,M=J,rtol=cfg["rtol"],atol=cfg["atol"],
                             maxiter=cfg["max_cg_iterations"],callback=track)
        if status != 0:
            raise RuntimeError(f"independent MFEM CG failed at r{refinement} step{i}")
        residual = float(np.linalg.norm(A @ next_phi-rhs)/max(1e-15,np.linalg.norm(rhs)))
        if residual > cfg["max_true_residual"]:
            raise RuntimeError("independent MFEM true linear residual failed")
        max_residual,max_iterations = max(max_residual,residual),max(max_iterations,count[0])
        next_velocity = 2*(next_phi-phi)/dt-velocity
        pressure[i] = plan["rho_kg_m3"]*float(rec @ ((velocity+next_velocity)/2))
        phi,velocity = next_phi,next_velocity
        if (i+1) % 500 == 0:
            print("MFEM_PROGRESS",refinement,i+1,steps,round(time.perf_counter()-started,1),flush=True)
    E = np.exp(2j*np.pi*np.array(plan["frequencies_hz"])[:,None]*times[None,:])
    result = (E @ pressure)/(E @ q)
    return {"refinement": refinement, "dofs": n, "system_sha256": digest,
            "mfem_source_pin": cfg["source_pin"], "steps": steps, "dt_s": dt,
            "signed_40_80": pairs(result), "max_true_residual": max_residual,
            "max_cg_iterations": max_iterations, "elapsed_s": time.perf_counter()-started}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", choices=("all","sem","mfem"),default="all")
    parser.add_argument("--mfem-system-root", type=Path, default=ROOT/"benchmarks/acoustics/r130d_mfem_independent_sparse_systems")
    parser.add_argument("--trace-output", type=Path,default=ROOT/"scratch/physical-pulse-sem/traces")
    args = parser.parse_args()
    plan,sha = verify_plan()
    if args.stage == "mfem":
        result = json.loads(args.output.read_text(encoding="utf8"))
        if result["plan_sha256_lf"] != sha:
            raise ValueError("cannot resume a different physical pulse plan")
    else:
        result = {"schema_version":"htdt.r130d.physical-pulse-sem-evidence-1",
                  "plan_commit":PLAN_COMMIT,"plan_sha256_lf":sha,"preregistered_plan":plan,
                  "original_point_q0":"SELF_CONVERGENCE_FAILED","physical_measured_room_validation":"NOT_VALIDATED",
                  "new_original_PFFDTD_runs":0,"new_github_actions":0,"sem_cases":[],"mfem_cases":[],
                  "numerical_model_qualification":"INCOMPLETE"}
    def save():
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf8")
    cache = ROOT/"scratch/physical-pulse-sem"
    if args.stage in ("sem","all"):
        for ppw in plan["ppw"]:
            row = sem_case(ppw,plan,cache,args.trace_output)
            result["sem_cases"].append(row);save()
            print("PHYSICAL_SEM",ppw,"seconds",round(row["elapsed_s"],1),"signed",row["actual_midpoint_signed_40_80"],flush=True)
        result["sem_adjacent_pairs"] = []
        for a,b in zip(result["sem_cases"],result["sem_cases"][1:]):
            s = scores(a["actual_midpoint_signed_40_80"],b["actual_midpoint_signed_40_80"])
            e = scores(a["exact_causal_signed_40_80"],b["exact_causal_signed_40_80"])
            row = {"coarse_ppw":a["ppw"],"fine_ppw":b["ppw"],"primary_metrics":s,
                   "primary_pass":passes(s,plan["primary_self_acceptance"]),"exact_control_metrics":e,
                   "exact_control_pass":passes(e,plan["exact_spatial_control_acceptance"])}
            result["sem_adjacent_pairs"].append(row)
            print("PHYSICAL_SEM_PAIR",a["ppw"],b["ppw"],{k:s[k] for k in METRICS},row["primary_pass"],flush=True)
        rows = result["sem_adjacent_pairs"]
        result["sem_self_convergence"] = {"all_four_pairs_pass":all(r["primary_pass"] for r in rows),
            "strict_monotone_all_three":monotone([r["primary_metrics"] for r in rows]),
            "all_exact_spatial_controls_pass":all(r["exact_control_pass"] for r in rows)}
        finer = result["sem_cases"][-1]
        with np.load(cache/f'ppw{finer["ppw"]}.npz') as data:
            lam,cp = data["lam"],data["coupling"]
        exact = unpairs(finer["exact_causal_signed_40_80"])
        result["fixed_spatial_time_refinement"] = []
        for steps in plan["temporal_control"]["steps"]:
            actual,_,_,_ = all_mode_midpoint_gaussian_trace(lam,cp,steps)
            s = scores(pairs(actual),pairs(exact))
            result["fixed_spatial_time_refinement"].append({"steps":steps,"dt_s":.25/steps,
                "signed_40_80":pairs(actual),"metrics_vs_exact":s})
        errors = [r["metrics_vs_exact"]["complex_rms_relative"] for r in result["fixed_spatial_time_refinement"]]
        orders = [float(np.log2(a/b)) for a,b in zip(errors,errors[1:])]
        result["time_refinement"] = {"observed_orders":orders,
            "pass":bool(min(orders)>=plan["temporal_control"]["order_min"] and errors[-1]<=plan["temporal_control"]["error_against_exact_continuous_control_max"])}
        save()
        print("PHYSICAL_SEM_SELF",result["sem_self_convergence"],"TIME",result["time_refinement"],flush=True)
    if args.stage in ("mfem","all"):
        result["mfem_cases"] = []
        for refinement in plan["independent_MFEM"]["refinements"]:
            row = independent_mfem_case(refinement,plan,args.mfem_system_root)
            result["mfem_cases"].append(row);save()
            print("PHYSICAL_MFEM",refinement,row["signed_40_80"],flush=True)
        comparison = []
        for a,b in zip(result["mfem_cases"],result["mfem_cases"][1:]):
            comparison.append(scores(a["signed_40_80"],b["signed_40_80"]))
        result["mfem_refinement"] = {"adjacent_metrics":comparison,
            "strict_monotone_all_three":monotone(comparison),
            "finest_pair_pass":passes(comparison[-1],plan["independent_MFEM"]["fine_pair_limits"])}
        same_dt = next(r for r in result["fixed_spatial_time_refinement"] if r["steps"]==plan["independent_MFEM"]["steps"])
        cross = scores(same_dt["signed_40_80"],result["mfem_cases"][-1]["signed_40_80"])
        result["independent_cross_solver"] = {"metrics":cross,
            "pass":passes(cross,plan["independent_MFEM"]["cross_SEM_fine_same_dt_limits"])}
        accepted = (all(result["sem_self_convergence"].values()) and result["time_refinement"]["pass"]
                    and result["mfem_refinement"]["strict_monotone_all_three"]
                    and result["mfem_refinement"]["finest_pair_pass"] and result["independent_cross_solver"]["pass"])
        result["numerical_model_qualification"] = "PASS_FINITE_BAND_R130D" if accepted else "FAIL_FINITE_BAND_R130D"
        save()
        print("PHYSICAL_MODEL_VERDICT",result["numerical_model_qualification"],"CROSS",{k:cross[k] for k in METRICS},flush=True)


if __name__ == "__main__":
    main()
