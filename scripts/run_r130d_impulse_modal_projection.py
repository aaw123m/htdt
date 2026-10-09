#!/usr/bin/env python3
"""Exact discrete unit impulse propagated on only 12 mass-orthonormal modes.

Reduced scalar midpoint integration is assembled from frozen independently
solved FV/real MFEM P2 generalized eigenpairs, with original point
source/receiver signed modal product. Full-space original q[0]=1
wave evidence is never recalibrated, truncated, filtered or replaced.
"""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
import sys
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_fv_mfem_same_drive_comparison import metrics


def z(vals):
    a=np.asarray(vals,dtype=float)
    if a.shape!=(2,2) or not np.all(np.isfinite(a)):
        raise ValueError("required finite two-bin signed complex spectrum missing")
    return a[:,0]+1j*a[:,1]


def simulate_modal(geometry:dict,modes:list,dt:float,steps:int,*,required_modes:int=12):
    if len(modes)!=required_modes or [x["mode"] for x in modes]!=list(range(required_modes)):
        raise ValueError("modal basis truncated or missing genuine null mode")
    lambdas=np.array([x["lambda_radians2_s2"] for x in modes],dtype=float)
    product=np.array([x["signed_source_receiver_modal_product"] for x in modes],dtype=float)
    if (not np.all(np.isfinite(lambdas)) or np.min(lambdas)<-1e-6
        or not np.all(np.isfinite(product))
        or np.any([x["generalized_eigen_relative_residual"]>1e-7 for x in modes])):
        raise ValueError("invalid independent Neumann basis or actual source functional")
    c=geometry["c_m_s"];rho=geometry["rho_kg_m3"]
    if dt!=.00025 or steps!=1000 or c!=343.2 or rho!=1.2:
        raise ValueError("source temporal sampling changed after preregistration")
    phi=np.zeros(required_modes);v=np.zeros(required_modes)
    pressure=np.zeros(steps)
    den=1.+dt*dt*lambdas/4
    numerator=1.-dt*dt*lambdas/4
    for t in range(steps):
        q=1.0 if t==0 else 0.0
        # Mass-normalized modal ODE: phi_j''+lambda_j phi_j =
        # (c^2*b_j) q. Store phi for unit source b_j=1,
        # multiply receiver by the sign-invariant b_j*r_j product.
        phi1=(numerator*phi+dt*v+dt*dt*c*c*q/2)/den
        v1=2*(phi1-phi)/dt-v
        pressure[t]=rho*float(product@((v+v1)*.5))
        phi,v=phi1,v1
    t=(np.arange(steps)+.5)*dt
    ff=np.array([40.,80.])
    fft=np.exp(2j*np.pi*ff[:,None]*t[None,:])
    Q=dt*fft[:,0]
    P=dt*fft@pressure
    T=P/Q
    if not np.all(np.isfinite(T)):
        raise ValueError("nonfinite 12-modal original impulse P_T/Q_T")
    return [[float(x.real),float(x.imag)] for x in T]


def evaluate(plan,modal_evidence,full):
    if (plan["schema_version"]!="htdt.r130d.original-impulse-12-neumann-mode-projection-plan-1"
        or plan["source"]["q"]!="q[0]=1m3/s, q[n>0]=0"
        or plan["source"]["dt_s"]!=.00025
        or plan["source"]["steps"]!=1000
        or plan["source"]["frequencies_hz"]!=[40,80]
        or plan["spatial_modes"]["count_including_zero"]!=12
        or plan["gate"]["production_ready"] is not False
        or plan["gate"]["original_fullband_impulse"]!="SELF_CONVERGENCE_FAILED"):
        raise ValueError("precommitted original source or modal physics authority mutated")
    assert modal_evidence["original_R130D_impulse"]=="SELF_CONVERGENCE_FAILED"
    assert full["original_fullband_r130d"]=="SELF_CONVERGENCE_FAILED"
    rows=[]
    for case in modal_evidence["cases"]:
        method=case["method"]
        level=case.get("n",case.get("r"))
        if method=="embedded_neumann_FV":
            raw=next(x for x in full["fv_levels"] if x["n"]==level)
        elif method=="independent_pinned_MFEM_P2":
            raw=next(x for x in full["mfem_levels"] if x["r"]==level)
        else:
            raise ValueError("unexpected independently assembled modal method")
        part=simulate_modal(plan["source"],case["modes"],.00025,1000)
        comparison=metrics(raw["complex_40_80_hz"],part)
        rows.append({
          "method":method,"level":level,"dofs_full":case["degrees_of_freedom"],
          "number_of_reconstructed_modes_including_zero":12,
          "first_twelve_modal_original_impulse_transfer_40_80_hz":part,
          "true_fullspace_original_impulse_transfer_40_80_hz":raw["complex_40_80_hz"],
          "fullspace_vs_12mode_relative_error":comparison,
          "modal_source_method":"mass-orthonormal signed source-receiver coupling of independently solved eigenpairs",
          "fullwave_actual_midpoint_execution_reused":True,
        })
        print("ACTUAL_12MODE_ORIGINAL_IMPULSE",method,level,"T12",part,
              "full_vs_modal",comparison["normalized_complex_l2"],flush=True)
    required=[("embedded_neumann_FV",x) for x in (12,20,32)]+[
        ("independent_pinned_MFEM_P2",x) for x in (2,3,4)]
    if [(x["method"],x["level"]) for x in rows]!=required:
        raise ValueError("incomplete independent six-reference reduced experiment")
    adjacent=[]
    for pair in (("embedded_neumann_FV",20,32),
                 ("independent_pinned_MFEM_P2",3,4)):
        method,a,b=pair
        coarse=next(x for x in rows if x["method"]==method and x["level"]==a)
        fine=next(x for x in rows if x["method"]==method and x["level"]==b)
        adjacent.append({"method":method,"coarse":a,"fine":b,
          "fullspace_adjacent":metrics(
            coarse["true_fullspace_original_impulse_transfer_40_80_hz"],
            fine["true_fullspace_original_impulse_transfer_40_80_hz"]),
          "12modal_adjacent":metrics(
            coarse["first_twelve_modal_original_impulse_transfer_40_80_hz"],
            fine["first_twelve_modal_original_impulse_transfer_40_80_hz"])})
    return {
      "schema_version":"htdt.r130d.original-impulse-low-modal-projection-diagnostic-1",
      "plan":plan,"cases":rows,"modal_vs_full_grid_pairs":adjacent,
      "same_original_unsmoothed_discrete_impulse":True,
      "low_modal_truncation_is_NOT_a_production_solver":True,
      "low_mode_spectrum_not_a_causal_proof_of_highband_failure":True,
      "canonical_fullband_impulse":"SELF_CONVERGENCE_FAILED",
      "physical_measurements":"NOT_VALIDATED",
      "production_ready":False
    }


def main():
    a=argparse.ArgumentParser()
    a.add_argument("--plan",type=Path,required=True)
    a.add_argument("--modes",type=Path,required=True)
    a.add_argument("--full-impulse",type=Path,required=True)
    a.add_argument("--out",type=Path,required=True)
    a=a.parse_args()
    p=json.loads(a.plan.read_text(encoding="utf-8"))
    m=json.loads(a.modes.read_text(encoding="utf-8"))
    w=json.loads(a.full_impulse.read_text(encoding="utf-8"))
    result=evaluate(p,m,w)
    a.out.parent.mkdir(parents=True,exist_ok=True)
    a.out.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print("ACTUAL_MODAL12_VS_FULL",json.dumps([
       {"method":x["method"],"full":x["fullspace_adjacent"]["normalized_complex_l2"],
        "modal":x["12modal_adjacent"]["normalized_complex_l2"]}
       for x in result["modal_vs_full_grid_pairs"]]),flush=True)


if __name__=="__main__":
    main()
