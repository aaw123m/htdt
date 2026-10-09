#!/usr/bin/env python3
"""Pinned, genuinely independent low-mode Neumann acoustic room spectrum.

Nonproduction modal analysis of the ORIGINAL sloped 56m^3 room, not a
changed-source time-record P_T/Q_T or canonical PFFDTD qualification.
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
from scipy.sparse import csr_matrix
from scipy.sparse.linalg import eigsh

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
sys.path.insert(0,str(ROOT/"scripts"))
from htdt.r130d_embedded_neumann_fv import (
    build_sloped_embedded_neumann,interior_point_stencil,
)
from run_r130d_exact_discrete_impulse_candidate import (
    HASHES,DOFS,matrix_from_csr
)


def load_plan(path:Path):
    p=json.loads(path.read_text(encoding="utf-8"))
    if (p.get("schema_version")!="htdt.r130d.sloped-neumann-modal-spectral-diagnostic-plan-1"
        or p["operators"]["smallest_modes_including_zero"]!=12
        or p["operators"]["shift_invert_sigma"]!=-1
        or p["operators"]["relative_residual_max"]!=1e-7
        or p["geometry"]["source"]!=[1.5,2,2]
        or p["geometry"]["receiver"]!=[2.5,2,2]):
        raise ValueError("modal spectral plan authority corrupted")
    if (p["geometry"]!={"room":"4m box roof z+.25y<=4, rigid Neumann 56 m3",
            "source":[1.5,2,2],"receiver":[2.5,2,2],
            "sound_speed_m_s":343.2,"density_kg_m3":1.2}
        or p["mfem"]["pinned_commit"]!="d964264cdb9a13e94a201b6c236c7721e0c8765f"
        or p["mfem"]["system_sha256_by_ref"]!={str(i):HASHES[i] for i in (2,3,4)}
        or p["operators"]["eigensolver"]!="scipy.sparse.linalg.eigsh(A=K,M=M,k=12,sigma=-1,which='LM',tol=1e-9,maxiter=1000)"
        or p["operators"]["frequency_hz"]!="sqrt(max(lambda,0))/(2*pi)"
        or p["operators"]["normalization"]!="x.T*M*x=1"
        or p["operators"]["compare_bins_hz"]!=[40,80]
        or p["spatial_levels"]!=[
          {"method":"embedded_neumann_FV","n":12},
          {"method":"embedded_neumann_FV","n":20},
          {"method":"embedded_neumann_FV","n":32},
          {"method":"independent_pinned_MFEM_P2","r":2},
          {"method":"independent_pinned_MFEM_P2","r":3},
          {"method":"independent_pinned_MFEM_P2","r":4}]
        or p["interpretation"]["original_R130D_impulse"]!="SELF_CONVERGENCE_FAILED"
        or p["interpretation"]["production_NO_GO"] is not True):
        raise ValueError("modal spatial levels or original physical authority changed")
    return p


def spectral_case(plan:dict,case:dict,systems:Path)->dict:
    clock=time.perf_counter()
    if case["method"]=="embedded_neumann_FV":
        n=case["n"]
        op=build_sloped_embedded_neumann(n)
        M,K=op.mass,op.stiffness
        b=interior_point_stencil(op,tuple(plan["geometry"]["source"]))
        r=interior_point_stencil(op,tuple(plan["geometry"]["receiver"]))
        details={"method":"embedded_neumann_FV","n":n,
             "degrees_of_freedom":op.degrees_of_freedom,
             "fluid_volume_m3":float(np.sum(op.cell_volumes_m3)),
             "independent_P2_matrix_reused":False}
        if abs(details["fluid_volume_m3"]-56)>1e-8:
            raise ValueError("FV modal source room not exact analytic 56m^3")
    elif case["method"]=="independent_pinned_MFEM_P2":
        n=case["r"]
        raw=gzip.decompress((systems/("mfem-r"+str(n)+".json.gz")).read_bytes())
        expected=plan["mfem"]["system_sha256_by_ref"][str(n)]
        h=hashlib.sha256(raw).hexdigest()
        if h!=expected or h!=HASHES[n] or len(raw)>plan["limits"]["max_raw_json_bytes"]:
            raise ValueError("independent P2 room matrix hash/size modified")
        data=json.loads(raw)
        if (data["ndofs"]!=DOFS[n] or data["order"]!=2
            or data["source_position_m"]!=plan["geometry"]["source"]
            or data["receiver_position_m"]!=plan["geometry"]["receiver"]):
            raise ValueError("independent P2 geometry/source reference changed")
        M=matrix_from_csr(data["mass_matrix"],ndofs=DOFS[n],max_nnz=3_000_000)
        K=matrix_from_csr(data["stiffness_c2_matrix"],ndofs=DOFS[n],max_nnz=3_000_000)
        b=np.asarray(data["source_functional"],dtype=float)
        r=np.asarray(data["receiver_functional"],dtype=float)
        details={"method":"independent_pinned_MFEM_P2","r":n,
           "degrees_of_freedom":DOFS[n],
           "original_pinned_system_sha256":h,
           "independent_P2_matrix_reused":True}
    else:
        raise ValueError("unregistered modal method")
    dim=M.shape[0]
    if (dim>plan["limits"]["max_dofs"]
        or K.shape!=(dim,dim) or len(b)!=dim or len(r)!=dim
        or np.min(M.diagonal())<=0):
        raise ValueError("modal operator dimensions/physical mass invalid")
    modes=plan["operators"]["smallest_modes_including_zero"]
    v0=np.sin((np.arange(dim,dtype=float)+1)*.0333)
    evals,evecs=eigsh(
      K,k=modes,M=M,sigma=-1,which="LM",tol=1e-9,
      maxiter=1000,v0=v0,
    )
    ordering=np.argsort(evals)
    evals=np.asarray(evals[ordering],dtype=float)
    evecs=np.asarray(evecs[:,ordering],dtype=float)
    if (np.min(evals)<plan["operators"]["negative_lambda_tolerance"]
        or not np.all(np.isfinite(evecs))):
        raise ValueError("positive semidefinite Neumann room spectral property violated")
    rows=[]
    scale=max(float(np.max(abs(K.diagonal()))),1.0)
    for idx,(lam,vec) in enumerate(zip(evals,evecs.T)):
        norm=float(vec@(M@vec))
        if norm<=0:raise ValueError("non-positive eigensystem mass norm")
        vec=vec/math.sqrt(norm)
        Kv=K@vec
        Mv=M@vec
        # The Neumann constant mode is at lambda~=0, where ||Kv|| and
        # ||lambda Mv|| both vanish: divide by operator scale instead of
        # amplifying sparse-direct 1e-11 roundoff into an apparent failure.
        # Nonzero modes retain true backward relative generalized residual.
        denominator=(scale*np.linalg.norm(vec) if idx==0 else
              max(np.linalg.norm(Kv)+abs(lam)*np.linalg.norm(Mv),
                  1e-12*scale*np.linalg.norm(vec)))
        residual=float(np.linalg.norm(Kv-lam*Mv)/denominator)
        if residual>plan["operators"]["relative_residual_max"]:
            raise ValueError(f"eigenfrequency generalized true residual too high {residual} mode {idx}")
        freq=math.sqrt(max(lam,0.0))/(2*math.pi)
        rows.append({
          "mode":idx,"lambda_radians2_s2":float(lam),
          "natural_frequency_hz":freq,
          "generalized_eigen_relative_residual":residual,
          "mass_normalization":float(vec@(M@vec)),
          "source_modal_weight":float(b@vec),
          "receiver_modal_weight":float(r@vec),
          "signed_source_receiver_modal_product":float((b@vec)*(r@vec)),
        })
    if rows[0]["natural_frequency_hz"]>plan["operators"]["zero_mode_frequency_hz_max"]:
        raise ValueError("natural Neumann constant zero-frequency mode missing")
    result={**details,"modes":rows,"time_seconds":time.perf_counter()-clock,
       "closest_mode_to_40hz":min(rows[1:],key=lambda x:abs(x["natural_frequency_hz"]-40))["mode"],
       "closest_mode_to_80hz":min(rows[1:],key=lambda x:abs(x["natural_frequency_hz"]-80))["mode"]}
    if result["time_seconds"]>plan["limits"]["max_per_case_wall_seconds"]:
        raise RuntimeError("modal solve exceeds pre-registered budget")
    print("ACTUAL_NEUMANN_SPECTRUM",details["method"],n,
        "f",[round(x["natural_frequency_hz"],6) for x in rows],
        "seconds",result["time_seconds"],flush=True)
    return result


def analyze(plan,cases):
    if [(x["method"],x.get("n",x.get("r"))) for x in cases]!=[
         (x["method"],x.get("n",x.get("r"))) for x in plan["spatial_levels"]]:
        raise ValueError("missing/reordered independent modal reference cases")
    by={k:[x for x in cases if x["method"]==k]
        for k in ("embedded_neumann_FV","independent_pinned_MFEM_P2")}
    drift=[]
    for method,group in by.items():
        for a,b in zip(group,group[1:]):
            a_freq=np.asarray([x["natural_frequency_hz"] for x in a["modes"][1:]])
            b_freq=np.asarray([x["natural_frequency_hz"] for x in b["modes"][1:]])
            drift.append({
                "method":method,
                "coarse_grid":a.get("n",a.get("r")),
                "fine_grid":b.get("n",b.get("r")),
                "largest_relative_ordered_mode_frequency_difference":
                      float(max(abs(a_freq-b_freq)/b_freq)),
                "per_mode_relative_frequency_difference":
                      (abs(a_freq-b_freq)/b_freq).tolist(),
            })
    return {
       "schema_version":"htdt.r130d.sloped-neumann-modal-spectral-evidence-1",
       "preregistered_plan":plan,"cases":cases,"adjacent_modal_frequency_drift":drift,
       "mode_eigenvectors_degenerate_groups_can_rotate":True,
       "not_fullband_impulse_time_transfer":True,
       "not_causal_proof":True,
       "original_R130D_impulse":"SELF_CONVERGENCE_FAILED",
       "production_ready":False,"physical_validation":"NOT_VALIDATED",
    }


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--systems",type=Path,required=True)
    ap.add_argument("--out",type=Path,required=True)
    a=ap.parse_args()
    plan=load_plan(a.plan)
    case_results=[]
    for item in plan["spatial_levels"]:
        row=spectral_case(plan,item,a.systems)
        case_results.append(row)
        path=a.out.with_name(a.out.stem+"_partial.json")
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps({"partial":True,"cases":case_results},indent=2)+"\n")
    result=analyze(plan,case_results)
    a.out.parent.mkdir(parents=True,exist_ok=True)
    a.out.write_text(json.dumps(result,indent=2)+"\n")
    print("MODAL_DIAGNOSTIC",json.dumps(result["adjacent_modal_frequency_drift"]),flush=True)


if __name__=="__main__":
    main()
