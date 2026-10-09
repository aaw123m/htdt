#!/usr/bin/env python3
"""Independent quadratic MFEM tetra wave solver, same source as convex FV.

Actual midpoint-injected Gaussian source; no modal band truncation and no
use of the candidate FV wave operator. The FEM spatial matrices are built
with separately compiled pinned MFEM C++ from independent convex tetra mesh.
All acceptance limits come from the preregistered plan; product gate immutable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time

import numpy as np
from scipy import sparse
from scipy.sparse.linalg import LinearOperator,cg

from run_r130d_fv_mfem_same_drive_comparison import metrics as two_bin_metrics


def _csr(doc:dict, n:int, *, max_nnz:int) -> sparse.csr_matrix:
    if doc["rows"]!=n or doc["cols"]!=n or doc["nnz"]>max_nnz:
        raise ValueError("FEM system matrix exceeds registered bounds")
    return sparse.csr_matrix((
        np.asarray(doc["values"],dtype=float),
        np.asarray(doc["column_indices"],dtype=np.int32),
        np.asarray(doc["row_offsets"],dtype=np.int32)),
        shape=(n,n),
    )


def _complex(row):
    # Independent MFEM and independently executed FV evidence intentionally
    # carry different schema keys; never silently fabricate a missing bin.
    key=("actual_driven_transfer_40_80_hz" if
         "actual_driven_transfer_40_80_hz" in row else
         "transfer_complex_40_80_hz")
    arr=np.asarray(row[key],dtype=float)
    if arr.shape!=(2,2) or not np.all(np.isfinite(arr)):
        raise ValueError("missing or nonfinite frozen 40/80-Hz complex bins")
    return arr[:,0]+1j*arr[:,1]


def solve_one(plan:dict, name:str, level:int, system_path:Path,
              mesh_provenance:dict) -> dict:
    if plan["schema_version"]!="htdt.r130d.convex-independent-mfem-plan-1":
        raise ValueError("invalid independent FEM preregistration")
    room=next((x for x in plan["geometry_cases"] if x["name"]==name),None)
    if room is None or level not in plan["refinements"]:
        raise ValueError("unexpected independent geometry or refinement")
    mesh=mesh_provenance["rooms"][name]
    if mesh_provenance["mfem_pin"]!=plan["mfem_pin"]:
        raise ValueError("wrong independent MFEM pinned source")
    blob=system_path.read_bytes()
    if len(blob)>plan["resource_limits"]["max_tet_export_json_bytes"]:
        raise ValueError("FEM CSR export byte cap")
    doc=json.loads(blob)
    n=int(doc["ndofs"])
    if (doc["schema_version"]!="htdt.r130d.independent-convex-mfem-system-1"
        or doc["geometry"]!="independent-convex-tetrahedral-polyhedron"
        or doc["order"]!=2 or doc["uniform_refinements"]!=level
        or doc["elements"]!=mesh["tetrahedra"]*8**level
        or n>plan["resource_limits"]["max_dofs_ref4"]
        or abs(doc["base_volume_m3"]-room["exact_volume_m3"])>1e-8
        or doc["boundary_model"]!="natural_neumann_rigid"
        or doc["sound_speed_m_s"]!=plan["physical"]["c_m_s"]
        or doc["density_kg_m3"]!=plan["physical"]["rho_kg_m3"]
        or doc["source_position_m"]!=plan["physical"]["source_xyz_m"]
        or doc["receiver_position_m"]!=plan["physical"]["receiver_xyz_m"]):
        raise ValueError("independently compiled MFEM physical authority mismatch")
    M=_csr(doc["mass_matrix"],n,max_nnz=plan["resource_limits"]["max_nnz_per_matrix"])
    K=_csr(doc["stiffness_c2_matrix"],n,max_nnz=plan["resource_limits"]["max_nnz_per_matrix"])
    if max(abs((K-K.T).data),default=0)>1e-5:
        raise ValueError("MFEM stiffness not symmetric")
    if max(abs((M-M.T).data),default=0)>1e-11:
        raise ValueError("MFEM mass not symmetric")
    if np.linalg.norm(K@np.ones(n))>1e-5*max(1,np.linalg.norm(K.data)):
        raise ValueError("Neumann constant mode not conserved")
    src=np.asarray(doc["source_functional"],dtype=float)
    rec=np.asarray(doc["receiver_functional"],dtype=float)
    if (src.shape!=(n,) or rec.shape!=(n,) or
        not np.all(np.isfinite(src)) or not np.all(np.isfinite(rec))
        or np.linalg.norm(src)<=0 or np.linalg.norm(rec)<=0):
        raise ValueError("invalid source/receiver FEM delta assembly")
    h=plan["waveform"]["dt_s"]
    steps=plan["waveform"]["steps"]
    if (h!=0.00025 or steps!=1000 or
        plan["waveform"]["bins_hz"]!=[40,80]):
        raise ValueError("source/time authority changed")
    T=(np.arange(steps)+0.5)*h
    q=np.exp(-0.5*((T-0.012)/0.003)**2)
    A=M+h*h/4*K
    B=M-h*h/4*K
    diag=A.diagonal()
    if np.any(diag<=0):
        raise ValueError("nonpositive MFEM midpoint diagonal")
    J=LinearOperator((n,n),matvec=lambda x:x/diag)
    phi=np.zeros(n)
    vel=np.zeros(n)
    pressure=np.zeros(steps)
    count_max=0
    true_residual_max=0.
    started=time.perf_counter()
    for i in range(steps):
        rhs=B@phi+h*(M@vel)+h*h/2*plan["physical"]["c_m_s"]**2*src*q[i]
        count=[0]
        def track(_):count[0]+=1
        phi1,status=cg(A,rhs,x0=phi+h*vel,M=J,
                       rtol=plan["linear_solver"]["rtol"],
                       atol=plan["linear_solver"]["atol"],
                       maxiter=plan["linear_solver"]["maxiter"],callback=track)
        if status!=0:
            raise RuntimeError(f"independent convex FEM CG failure {name} ref={level} step={i},status={status}")
        residual=float(np.linalg.norm(A@phi1-rhs)/max(1e-15,np.linalg.norm(rhs)))
        if residual>plan["linear_solver"]["true_residual_relative_max"]:
            raise RuntimeError(f"independent convex FEM true residual violated: {residual}")
        true_residual_max=max(true_residual_max,residual)
        count_max=max(count_max,count[0])
        vel1=2*(phi1-phi)/h-vel
        pressure[i]=plan["physical"]["rho_kg_m3"]*float(rec@((vel+vel1)/2))
        phi,vel=phi1,vel1
    kernel=np.exp(2j*np.pi*np.array([40,80])[:,None]*T[None,:])
    Q=h*kernel@q
    P=h*kernel@pressure
    z=P/Q
    if not np.all(np.isfinite(z)):
        raise ValueError("invalid same-source P/Q")
    return {"room":name,"uniform_refinements":level,
            "dofs":n,"elements":doc["elements"],
            "system_sha256":hashlib.sha256(blob).hexdigest(),
            "tetra_mesh_sha256":mesh["sha256"],
            "transfer_complex_40_80_hz":[[float(v.real),float(v.imag)] for v in z],
            "true_linear_relative_residual_max":true_residual_max,
            "max_cg_iterations":count_max,
            "elapsed_s":time.perf_counter()-started,
            "actual_midpoint_injected_source":True,"production_enabled":False}


def evaluate(plan,mesh,levels:dict, fv:dict) -> dict:
    if (plan['unchanged_self_limits']!={
           'complex_rms_relative_max':.05,
           'magnitude_max_relative':.08,'phase_max_deg':5}
        or plan['unchanged_cross_limits']!={
           'complex_rms_relative_max':.35,
           'magnitude_max_relative':.4,'magnitude_max_db':3,
           'phase_max_deg':25}
        or plan['frozen']['production_enabled'] is not False
        or plan['frozen']['canonical_original_impulse']!='SELF_CONVERGENCE_FAILED'):
        raise ValueError('independent convex numeric limits or frozen authority changed')
    if [x["name"] for x in fv["cases"]]!=["baseline_sloped","planar_wedge","three_axis_diagonal"]:
        raise ValueError("FV geometries changed")
    rows=[]
    for p in plan["geometry_cases"]:
        name=p["name"]
        cases=levels[name]
        if [x["uniform_refinements"] for x in cases]!=plan["refinements"]:
            raise ValueError("missing independently driven MFEM levels")
        adjacent=[]
        for a,b in zip(cases,cases[1:]):
            adjacent.append({"r0":a["uniform_refinements"],"r1":b["uniform_refinements"],
                     **two_bin_metrics(a["transfer_complex_40_80_hz"],
                                       b["transfer_complex_40_80_hz"])})
        ref=cases[-1]["transfer_complex_40_80_hz"]
        fvrow=next(x for x in fv["cases"] if x["name"]==name)["levels"][-1]
        cross=two_bin_metrics(fvrow["actual_driven_transfer_40_80_hz"],ref)
        strict=plan["unchanged_self_limits"]
        cross_strict=plan["unchanged_cross_limits"]
        # These per-bin phase/magnitude metrics are checked individually,
        # with P_T/Q_T complex L2 normalized to the fine independent MFEM.
        mfem_last=adjacent[-1]
        limit_ok=(
          mfem_last["normalized_complex_l2"]<=strict["complex_rms_relative_max"] and
          max(mfem_last["magnitude_relative_by_hz"])<=strict["magnitude_max_relative"] and
          max(mfem_last["phase_difference_deg_by_hz"])<=strict["phase_max_deg"])
        descending=all(
          adjacent[-1][k]<adjacent[-2][k] for k in
          ("normalized_complex_l2",)
        ) and all(
          max(adjacent[-1][k])<max(adjacent[-2][k]) for k in
          ("magnitude_relative_by_hz","phase_difference_deg_by_hz")
        )
        fvdiag=next(x for x in fv["cases"] if x["name"]==name)["adjacent_transfer"][-1]
        fv_pass=(fvdiag["complex_l2_relative"]<=.2 and
                 fvdiag["magnitude_max_relative"]<=.25 and
                 fvdiag["phase_max_deg"]<=15.)
        cross_pass=(cross["normalized_complex_l2"]<=cross_strict["complex_rms_relative_max"] and
            max(cross["magnitude_relative_by_hz"])<=cross_strict["magnitude_max_relative"] and
            max(cross["phase_difference_deg_by_hz"])<=cross_strict["phase_max_deg"])
        # dB absolute of finest FV/FEM magnitude, not inferred from relative
        cross_db=float(max(abs(20*np.log10(abs(_complex(fvrow))/abs(np.array(
                          [complex(*x) for x in ref]))))))
        cross_pass=cross_pass and cross_db<=cross_strict["magnitude_max_db"]
        rows.append({
            "room":name,"mfem_refinements":cases,
            "mfem_adjacent":adjacent,"mfem_self_pass":bool(limit_ok and descending),
            "fv_last_adjacent_pass":bool(fv_pass),
            "cross_fv_n20_vs_mfem_r4":cross,
            "cross_max_magnitude_db":cross_db,
            "cross_pass":bool(cross_pass),
            "experimental_numeric_candidate_pass":bool(limit_ok and descending and fv_pass and cross_pass),
        })
    return {"schema_version":"htdt.r130d.convex-independent-same-source-evidence-1",
            "authority":"EXPERIMENTAL_ONLY","plan":plan,"mesh_provenance":mesh,
            "cases":rows,"production_enabled":False,
            "original_r130d_impulse":"SELF_CONVERGENCE_FAILED",
            "physical_validation":"NOT_VALIDATED"}


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--plan",type=Path,required=True)
    p.add_argument("--mesh-provenance",type=Path,required=True)
    p.add_argument("--room")
    p.add_argument("--refinement",type=int)
    p.add_argument("--system",type=Path)
    p.add_argument("--fv-evidence",type=Path)
    p.add_argument("--levels",type=Path)
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    plan=json.loads(a.plan.read_text())
    mesh=json.loads(a.mesh_provenance.read_text())
    if a.system:
        result=solve_one(plan,a.room,a.refinement,a.system,mesh)
    else:
        obj=json.loads(a.levels.read_text())
        fv=json.loads(a.fv_evidence.read_text())
        result=evaluate(plan,mesh,obj,fv)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps({k:v for k,v in result.items() if k not in
                      ("plan","mesh_provenance","cases")},indent=2),flush=True)


if __name__=="__main__":
    main()
