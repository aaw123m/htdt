#!/usr/bin/env python3
"""Actually test the pinned source's *discrete impulse* (q[0]=1; others zero).

Separate experimental wave method: exact sloped cut-cell Neumann FV vs MFEM P2,
same 40/80Hz, same dt, actual sampled RHS, no Gaussian or fitted filtering.
The canonical original PFFDTD PPW8-44 original-impulse gate remains FAILED.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
from scipy.sparse.linalg import LinearOperator,cg

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
sys.path.insert(0,str(ROOT/"scripts"))

from htdt.r130d_embedded_neumann_fv import (
    build_sloped_embedded_neumann,discrete_source_complex_transfer,
)
from run_r130d_mfem_ref4_same_drive import matrix_from_csr
from run_r130d_fv_mfem_same_drive_comparison import validate_mfem_system,metrics

HASHES={
  1:"758dff6aeafeaa5c902e905aa797c2e5f8250e4b81822723e968bb842187161b",
  2:"e61410d4a69000c39975762b9986008974353953f7d8d9f100f9953a33c5ecb4",
  3:"6a43a624223fa512152059d103c4d841723fa1266ea8a22842322fb50db42511",
  4:"e1c67d02db77a6e8775a0a59d8e75fa996434d58f8efae077f4cec2cf2c831ed",
}
DOFS={1:125,2:729,3:4913,4:35937}


def compute_mfem_reference(plan:dict,ref:int,file:Path,q:np.ndarray)->dict:
    """No FV stiffness/mass or receiver weights reused by this FEM reference."""
    raw=gzip.decompress(file.read_bytes())
    digest=hashlib.sha256(raw).hexdigest()
    if digest!=HASHES[ref] or len(raw)>plan["limits"]["max_fem_raw_json_bytes"]:
        raise ValueError("independent pinned MFEM sparse spatial authority changed")
    doc=json.loads(raw)
    n=DOFS[ref]
    g=plan["geometry"]
    validate_mfem_system(
        doc,refinement=ref,ndofs=n,
        plan={"source_xyz_m":g["source_xyz_m"],
              "receiver_xyz_m":g["receiver_xyz_m"]},
    )
    M=matrix_from_csr(doc["mass_matrix"],ndofs=n,max_nnz=3_000_000)
    K=matrix_from_csr(doc["stiffness_c2_matrix"],ndofs=n,max_nnz=3_000_000)
    b=np.asarray(doc["source_functional"],dtype=np.float64)
    r=np.asarray(doc["receiver_functional"],dtype=np.float64)
    if not np.all(np.isfinite(b)) or not np.all(np.isfinite(r)):
        raise ValueError("MFEM source/receiver contain nonfinite weights")
    h=plan["source"]["dt_s"]
    a=M+(h*h/4)*K
    b_op=M-(h*h/4)*K
    d=a.diagonal()
    if np.min(d)<=0 or not np.all(np.isfinite(d)):
        raise ValueError("non-positive FEM midpoint diagonal")
    pre=LinearOperator((n,n),matvec=lambda x:x/d,dtype=np.float64)
    phi=np.zeros(n)
    vel=np.zeros(n)
    pressure=np.zeros(len(q))
    worst_residual=0.0
    max_iters=0
    for i,x in enumerate(q):
        force=g["sound_speed_m_s"]**2*b*x
        rhs=b_op@phi+h*(M@vel)+(h*h/2)*force
        count=[0]
        def callback(_):
            count[0]+=1
        phi1,status=cg(
            a,rhs,x0=phi+h*vel,
            M=pre,rtol=plan["linear_solver"]["rtol"],
            atol=plan["linear_solver"]["atol"],
            maxiter=plan["linear_solver"]["maxiter"],
            callback=callback,
        )
        if status!=0:
            raise RuntimeError(f"independent P2 FEM impulse linear solve failed at ref {ref} step {i}: {status}")
        true=float(np.linalg.norm(a@phi1-rhs)/max(np.linalg.norm(rhs),1e-15))
        if true>plan["linear_solver"]["true_linear_relative_residual_max"]:
            raise RuntimeError(f"independent MFEM true linear residual failed at r{ref} step {i}: {true}")
        worst_residual=max(worst_residual,true)
        max_iters=max(max_iters,count[0])
        v1=2*(phi1-phi)/h-vel
        pressure[i]=g["density_kg_m3"]*float(r@(vel+v1))/2
        phi,vel=phi1,v1
    t=(np.arange(len(q))+.5)*h
    fourier=np.exp(2j*np.pi*np.array(plan["source"]["frequency_bins_hz"])[:,None]*t[None,:])
    P=h*fourier@pressure
    Q=h*fourier@q
    transfer=P/Q
    if not np.all(np.isfinite(transfer)):
        raise ValueError("nonfinite FEM impulse complex transfer")
    print("ACTUAL_MFEM_UNIT_IMPULSE",ref,"DOF",n,"true residual",worst_residual,
          "maxCG",max_iters,"transfer",transfer,flush=True)
    return {"r":ref,"dofs":n,"export_sha256":digest,
            "actual_time_domain_discrete_unit_impulse":True,
            "complex_40_80_hz":[[float(x.real),float(x.imag)] for x in transfer],
            "worst_true_relative_linear_residual":worst_residual,
            "maximum_cg_iterations":max_iters}


def bin_metrics(a,b):
    return metrics(a,b)


def gate(result:dict,plan:dict)->dict:
    # Fail closed if any physical source, observation bin, refinement,
    # unmodified numeric threshold or original production authority changed.
    lim=plan["same_frozen_criteria"]
    expected={
      "fv_last_two_adjacent_strictly_decreasing_complex_magnitude_phase":True,
      "mfem_last_two_adjacent_strictly_decreasing_complex_magnitude_phase":True,
      "fv_finest_complex_l2_max":.2,"fv_finest_max_magnitude_relative":.25,
      "fv_finest_max_phase_deg":15,
      "mfem_finest_complex_l2_max":.05,"mfem_finest_max_magnitude_relative":.08,
      "mfem_finest_max_phase_deg":5,
      "cross_fv_n32_vs_mfem_r4_complex_l2_max":.35,
      "cross_max_magnitude_relative":.4,
      "cross_max_magnitude_db":3,"cross_max_phase_deg":25,
    }
    if (lim!=expected or
        plan["authority"]["original_PFFDTD"]!="SELF_CONVERGENCE_FAILED"
        or plan["authority"]["production_ready"] is not False
        or result.get("production_ready") is not False
        or result.get("actual_time_domain_impulse_injection") is not True):
        raise ValueError("original impulse numeric threshold / production authority changed")
    fv=result["fv_levels"]
    fem=result["mfem_levels"]
    if ([x["n"] for x in fv]!=[12,16,20,24,28,32]
         or [x["r"] for x in fem]!=[1,2,3,4]):
        raise ValueError("missing original impulse refinement level")
    for row in fv+fem:
        bins=np.asarray(row["complex_40_80_hz"],dtype=float)
        if bins.shape!=(2,2) or not np.all(np.isfinite(bins)):
            raise ValueError("missing/nonfinite 40/80-Hz complex observation bin")
    for row in fem:
        if (row["export_sha256"]!=HASHES[row["r"]]
            or row["dofs"]!=DOFS[row["r"]]
            or row["worst_true_relative_linear_residual"]>1e-8):
            raise ValueError("independent impulse P2 spatial or linear solver evidence changed")
    adj_fv=[bin_metrics(x["complex_40_80_hz"],y["complex_40_80_hz"]) for x,y in zip(fv,fv[1:])]
    adj_mfem=[bin_metrics(x["complex_40_80_hz"],y["complex_40_80_hz"]) for x,y in zip(fem,fem[1:])]
    x=bin_metrics(fv[-1]["complex_40_80_hz"],fem[-1]["complex_40_80_hz"])
    def scalar_max(m,k):
        return float(max(m[k]))
    def decreasing(last,prev):
        return (last["normalized_complex_l2"]<prev["normalized_complex_l2"]
            and scalar_max(last,"magnitude_relative_by_hz")<
                scalar_max(prev,"magnitude_relative_by_hz")
            and scalar_max(last,"phase_difference_deg_by_hz")<
                scalar_max(prev,"phase_difference_deg_by_hz"))
    def self_pass(m,typ):
        return (m["normalized_complex_l2"]<=lim[typ+"_finest_complex_l2_max"] and
            scalar_max(m,"magnitude_relative_by_hz")<=lim[typ+"_finest_max_magnitude_relative"] and
            scalar_max(m,"phase_difference_deg_by_hz")<=lim[typ+"_finest_max_phase_deg"])
    fv_ok=decreasing(adj_fv[-1],adj_fv[-2]) and self_pass(adj_fv[-1],"fv")
    mfem_ok=decreasing(adj_mfem[-1],adj_mfem[-2]) and self_pass(adj_mfem[-1],"mfem")
    aa=np.array([complex(*z) for z in fv[-1]["complex_40_80_hz"]])
    bb=np.array([complex(*z) for z in fem[-1]["complex_40_80_hz"]])
    db=float(max(abs(20*np.log10(abs(aa)/abs(bb)))))
    cross_ok=(x["normalized_complex_l2"]<=lim["cross_fv_n32_vs_mfem_r4_complex_l2_max"] and
              scalar_max(x,"magnitude_relative_by_hz")<=lim["cross_max_magnitude_relative"] and
              db<=lim["cross_max_magnitude_db"] and
              scalar_max(x,"phase_difference_deg_by_hz")<=lim["cross_max_phase_deg"])
    return {
        "fv_adjacent":adj_fv,"mfem_adjacent":adj_mfem,
        "cross_n32_r4":x,"cross_magnitude_db_max":db,
        "fv_self_pass":bool(fv_ok),"mfem_self_pass":bool(mfem_ok),
        "cross_method_pass":bool(cross_ok),
        "exact_unit_impulse_candidate_numerical_pass":bool(fv_ok and mfem_ok and cross_ok),
        "original_PFFDTD_impulse":"SELF_CONVERGENCE_FAILED",
        "canonical_original_fullband_impulse":"SELF_CONVERGENCE_FAILED",
        "production_ready":False,
        "physical_validation":"NOT_VALIDATED",
    }


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--plan",type=Path,required=True)
    parser.add_argument("--systems",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    plan=json.loads(args.plan.read_text(encoding="utf-8"))
    if (plan["schema_version"]!="htdt.r130d.exact-unit-discrete-impulse-two-method-diagnostic-plan-1"
        or plan["source"]["waveform"]!="discrete unit volume-velocity impulse q[0]=1m3/s, q[n>0]=0"
        or plan["source"]["no_smoothing"] is not True
        or plan["source"]["dt_s"]!=.00025
        or plan["source"]["steps"]!=1000
        or plan["source"]["frequency_bins_hz"]!=[40,80]
        or plan["spatial_discretizations"]["fv_cells_per_axis"]!=[12,16,20,24,28,32]
        or plan["spatial_discretizations"]["independent_mfem_P2_refinements"]!=[1,2,3,4]
        or plan["authority"]["production_ready"] is not False
        or plan["authority"]["original_PFFDTD"]!="SELF_CONVERGENCE_FAILED"):
        raise ValueError("preregistered original impulse source or scope changed")
    src=plan["source"]
    q=np.zeros(src["steps"],dtype=float)
    q[0]=1.
    result={"schema_version":"htdt.r130d.experimental-original-discrete-impulse-candidate-1",
        "preregistered_plan":plan,
        "fv_levels":[],"mfem_levels":[],
        "source_samples_sha256":hashlib.sha256(q.tobytes()).hexdigest(),
        "actual_time_domain_impulse_injection":True,
        "no_smoothing_no_taper":True,
        "production_ready":False,
        "original_fullband_r130d":"SELF_CONVERGENCE_FAILED",
    }
    g=plan["geometry"]
    for n in plan["spatial_discretizations"]["fv_cells_per_axis"]:
        fv=build_sloped_embedded_neumann(n)
        T=discrete_source_complex_transfer(
            fv,q,source_xyz_m=tuple(g["source_xyz_m"]),
            receiver_xyz_m=tuple(g["receiver_xyz_m"]),
            frequency_hz=tuple(src["frequency_bins_hz"]),
            time_step_s=src["dt_s"],density_kg_m3=g["density_kg_m3"])
        result["fv_levels"].append({"n":n,"dofs":fv.degrees_of_freedom,
            "air_volume_m3":float(fv.cell_volumes_m3.sum()),
            "complex_40_80_hz":[[float(x.real),float(x.imag)] for x in T]})
        print("ACTUAL_FV_UNIT_IMPULSE",n,"DOF",fv.degrees_of_freedom,"TRANSFER",T,flush=True)
    for ref in plan["spatial_discretizations"]["independent_mfem_P2_refinements"]:
        file=args.systems/f"mfem-r{ref}.json.gz"
        result["mfem_levels"].append(compute_mfem_reference(plan,ref,file,q))
    result["numerical_diagnostic"]=gate(result,plan)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,ensure_ascii=True)+"\n",encoding="utf-8")
    print("ACTUAL_UNIT_IMPULSE_VERDICT",json.dumps(result["numerical_diagnostic"],indent=2),flush=True)


if __name__=="__main__":
    main()
