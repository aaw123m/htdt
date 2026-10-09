#!/usr/bin/env python3
"""Original q0 impulse projected onto 12, 24, 48 independently solved eigenmodes.

Keep the source and receiver point functionals unchanged. Modal truncation is a
causal diagnostic, NOT a replacement for the fullband point-impulse acceptance.
"""
from __future__ import annotations
import argparse
import gzip
import hashlib
import json
import math
from pathlib import Path
import sys
import time
import numpy as np
from scipy.sparse.linalg import eigsh

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/"backend"/"src"),str(ROOT/"scripts")]
from htdt.r130d_embedded_neumann_fv import build_sloped_embedded_neumann,interior_point_stencil
from run_r130d_exact_discrete_impulse_candidate import HASHES,DOFS,matrix_from_csr
from run_r130d_fv_mfem_same_drive_comparison import validate_mfem_system,metrics
from run_r130d_impulse_modal_projection import simulate_modal

EXPECTED_COUNTS=(12,24,48)
REFERENCE_SHA="fa2a4aae2c6b00f4e1eccda9d0e9bf9271c420eb0a5f971b26927d9ed76e3cf2"

def validate_plan(p):
    if (p.get("schema_version")!="htdt.r130d.original-q0-high-mode-count-diagnostic-plan-1"
        or p["mode_counts_including_zero"]!=list(EXPECTED_COUNTS)
        or p["source"]["temporal"]!="q[0]=1 m3/s, q[n>0]=0"
        or p["source"]["dt_s"]!=.00025 or p["source"]["steps"]!=1000
        or p["source"]["frequencies_hz"]!=[40,80]
        or p["full_reference"]["sha256"]!=REFERENCE_SHA
        or p["eigsolve"]["true_generalized_relative_residual_max"]!=1e-7
        or p["limits"]["max_modes"]!=48
        or p["authority"]["original_fullband_impulse"]!="SELF_CONVERGENCE_FAILED"
        or p["authority"]["production_ready"] is not False):
        raise ValueError("frozen high-modal original impulse plan changed")
    return p
def operator(case,systems,p):
    method=case["kind"]
    if method=="FV_exact_cutcell":
        fv=build_sloped_embedded_neumann(case["n"])
        M,K=fv.mass,fv.stiffness
        b=interior_point_stencil(fv,tuple(p["source_xyz_m"]))
        r=interior_point_stencil(fv,tuple(p["receiver_xyz_m"]))
        meta={"method":method,"n":case["n"],"dofs":fv.degrees_of_freedom}
    elif method=="independent_MFEM_P2":
        ref=case["r"]
        raw=gzip.decompress((systems/f"mfem-r{ref}.json.gz").read_bytes())
        sha=hashlib.sha256(raw).hexdigest()
        if (sha!=HASHES[ref] or sha!=case["raw_system_sha256"]
            or len(raw)>p["limits"]["max_mfem_raw_json_bytes"]):
            raise ValueError("independent pinned MFEM spatial matrix changed")
        data=json.loads(raw)
        validate_mfem_system(data,refinement=ref,ndofs=DOFS[ref],
            plan={"source_xyz_m":p["source_xyz_m"],
                  "receiver_xyz_m":p["receiver_xyz_m"]})
        M=matrix_from_csr(data["mass_matrix"],ndofs=DOFS[ref],max_nnz=3_000_000)
        K=matrix_from_csr(data["stiffness_c2_matrix"],ndofs=DOFS[ref],max_nnz=3_000_000)
        b=np.asarray(data["source_functional"],dtype=float)
        r=np.asarray(data["receiver_functional"],dtype=float)
        meta={"method":method,"r":ref,"dofs":DOFS[ref],
              "original_sparse_sha256":sha}
    else:raise ValueError("unsupported spatial modal operator")
    if (M.shape!=(case["dofs"],case["dofs"]) or K.shape!=M.shape
        or b.shape!=(case["dofs"],) or r.shape!=(case["dofs"],)
        or not np.all(np.isfinite(b)) or not np.all(np.isfinite(r))
        or abs(float(np.sum(b))-1)>1e-9
        or abs(float(np.sum(r))-1)>1e-9):
        raise ValueError("original point-source/receiver operator changed")
    return M,K,b,r,meta

def modal_case(case,systems,p,original):
    started=time.perf_counter()
    M,K,b,r,meta=operator(case,systems,p)
    dim=M.shape[0]
    v0=np.sin((np.arange(dim,dtype=float)+1)*.0333)
    lam,vec=eigsh(K,k=48,M=M,sigma=-1,which="LM",tol=1e-9,
                  maxiter=1000,v0=v0)
    order=np.argsort(lam)
    lam=np.asarray(lam[order],dtype=float)
    vec=np.asarray(vec[:,order],dtype=float)
    if np.min(lam)<-1e-6:
        raise RuntimeError("independent Neumann eigenvalues below PSD tolerance")
    max_diag=max(1.,float(max(abs(K.diagonal()))))
    rows=[]
    for mode,(value,x) in enumerate(zip(lam,vec.T)):
        x=x/math.sqrt(float(x@(M@x)))
        Kx=K@x; Mx=M@x
        denominator=(max_diag*np.linalg.norm(x) if mode==0 else
                     max(np.linalg.norm(Kx)+abs(value)*np.linalg.norm(Mx),
                         1e-12*max_diag*np.linalg.norm(x)))
        true=float(np.linalg.norm(Kx-value*Mx)/denominator)
        if true>p["eigsolve"]["true_generalized_relative_residual_max"]:
            raise RuntimeError(f"true eigensolve residual unacceptable mode {mode} {true}")
        rows.append({"mode":mode,"lambda_radians2_s2":float(value),
            "natural_frequency_hz":math.sqrt(max(float(value),0.))/(2*math.pi),
            "generalized_eigen_relative_residual":true,
            "mass_normalization":float(x@(M@x)),
            "signed_source_receiver_modal_product":float((b@x)*(r@x))})
    elapsed=time.perf_counter()-started
    if elapsed>p["limits"]["max_single_eigsolve_wall_seconds"]:
        raise RuntimeError(f"eigen resource limit exceeded: {elapsed}")
    key=("n" if "n" in case else "r")
    reference=next(entry for entry in
                   (original["fv_levels"] if key=="n" else original["mfem_levels"])
                   if entry[key]==case[key])
    actual=reference["complex_40_80_hz"]
    projections=[]
    for count in EXPECTED_COUNTS:
        T=simulate_modal({"c_m_s":343.2,"rho_kg_m3":1.2},
                         rows[:count],.00025,1000,required_modes=count)
        comparison=metrics(actual,T)
        projections.append({"modes_including_zero":count,
            "finite_time_original_q0_transfer_40_80_hz":T,
            "complex_magnitude_phase_relative_to_actual_full_wave":comparison})
        print("ORIGINAL_Q0_MODAL",meta["method"],case[key],count,
              "complex relative full wave",comparison["normalized_complex_l2"],
              "T",T,flush=True)
    return {**meta,"solved_eigenmodes":rows,
            "actual_full_state_original_q0_transfer_40_80_hz":actual,
            "truncations":projections,"eigensolve_wall_seconds":elapsed,
            "temporal_q0_was_unchanged":True,
            "same_spatial_point_functionals_as_full_state":True}
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--systems",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args()
    raw=args.plan.read_bytes()
    p=validate_plan(json.loads(raw))
    original_path=ROOT/p["full_reference"]["path"]
    if hashlib.sha256(original_path.read_bytes()).hexdigest()!=REFERENCE_SHA:
        raise ValueError("original full-state q0 impulse baseline changed")
    baseline=json.loads(original_path.read_text(encoding="utf-8"))
    if (baseline["original_fullband_r130d"]!="SELF_CONVERGENCE_FAILED"
        or baseline["production_ready"] is not False):
        raise ValueError("original impulse numerical gate changed")
    result={"schema_version":"htdt.r130d.original-q0-48-mode-diagnostic-1",
        "plan_sha256":hashlib.sha256(raw).hexdigest(),
        "original_full_state_evidence_sha256":REFERENCE_SHA,
        "preregistered_plan":p,"cases":[],
        "original_fullband_impulse":"SELF_CONVERGENCE_FAILED",
        "point_impulse_candidate":"NOT_QUALIFIED",
        "physical_validation":"NOT_VALIDATED","production_ready":False}
    for case in p["methods"]:
        row=modal_case(case,args.systems,p,baseline)
        result["cases"].append(row)
        temp=args.output.with_name(args.output.stem+"_partial.json")
        temp.parent.mkdir(parents=True,exist_ok=True)
        temp.write_text(json.dumps({"partial":True,"cases":result["cases"]},
                                   indent=2,allow_nan=False)+"\n",encoding="utf-8")
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("ORIGINAL Q0 12/24/48 MODES COMPLETE; CANONICAL STILL FAIL",flush=True)

if __name__=="__main__":
    main()
