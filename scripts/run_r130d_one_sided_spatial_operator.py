#!/usr/bin/env python3
"""Preregistered mixed point/Gaussian source and receiver q0 wave experiment.

This is a nonproduction causal diagnostic only; never promotes changed
spatial source/receiver into original PFFDTD or point-q0 qualification.
"""
from __future__ import annotations
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sys
import time
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/"backend"/"src"),str(ROOT/"scripts")]
from htdt.r130d_embedded_neumann_fv import (
    build_sloped_embedded_neumann, interior_point_stencil)
from run_r130d_fixed_spatial_kernel import fv_functionals,impulse_transfer
from run_r130d_exact_discrete_impulse_candidate import (
    HASHES,DOFS,matrix_from_csr)
from run_r130d_fv_mfem_same_drive_comparison import validate_mfem_system,metrics

POINT_SHA="fa2a4aae2c6b00f4e1eccda9d0e9bf9271c420eb0a5f971b26927d9ed76e3cf2"
BOTH_SHA="627881b9f216f229ce0c2fa9c735e6a5f6ff956e2effd43192494004a5b6650b"
WIDTHS=(0.35,0.7)
FV_NS=(20,32)
P2_RS=(3,4)
OPERATORS=("gaussian_source_point_receiver","point_source_gaussian_receiver")
def validate_plan(p):
    if (p.get("schema_version")!="htdt.r130d.one-sided-spatial-operator-q0-plan-1"
        or p["spatial_kernel"]["sigma_m"]!=list(WIDTHS)
        or p["grid"]["fv_n"]!=list(FV_NS)
        or p["grid"]["p2_mfem_refinement"]!=list(P2_RS)
        or p["operator_cases"]!=["point_source_point_receiver",*OPERATORS,"gaussian_source_gaussian_receiver"]
        or p["source_waveform"]["dt_s"]!=.00025
        or p["source_waveform"]["steps"]!=1000
        or p["source_waveform"]["frequencies_hz"]!=[40,80]
        or p["source_waveform"]["q0"]!=1 or p["source_waveform"]["q_after_0"]!=0
        or not p["source_waveform"]["no_time_smoothing"]
        or p["resource_limits"]["max_mixed_time_integrations"]!=16
        or p["geometry"]["source_xyz_m"]!=[1.5,2,2]
        or p["geometry"]["receiver_xyz_m"]!=[2.5,2,2]
        or p["solver"]["true_relative_linear_residual_max"]!=1e-8
        or p["authority"]["original_pffdtd_fullband"]!="SELF_CONVERGENCE_FAILED"
        or p["authority"]["production_ready"] is not False):
        raise ValueError("preregistered mixed spatial q0 experimental contract changed")
    return p

def pinned_baselines(p):
    paths=[ROOT/p["numerical_evidence"]["baseline_pointpoint"],
           ROOT/p["numerical_evidence"]["baseline_bothgaussian"]]
    raws=[q.read_bytes() for q in paths]
    if [hashlib.sha256(x).hexdigest() for x in raws]!=[POINT_SHA,BOTH_SHA]:
        raise ValueError("original point or preobserved Gaussian baseline hash changed")
    point,both=(json.loads(x) for x in raws)
    if (point["original_fullband_r130d"]!="SELF_CONVERGENCE_FAILED"
        or point["production_ready"] is not False
        or both["original_fullband_PFFDTD"]!="SELF_CONVERGENCE_FAILED"
        or both["production_ready"] is not False):
        raise ValueError("canonical baseline provenance invalid")
    return point,both

def reference_case(point,both,method,level,sigma):
    key="n" if method=="fv" else "r"
    point_case=next(x for x in point["fv_levels" if method=="fv" else "mfem_levels"] if x[key]==level)
    both_case=next(x for x in both["fv_levels" if method=="fv" else "mfem_levels"][str(sigma)] if x[key]==level)
    return {"point_source_point_receiver":point_case["complex_40_80_hz"],
            "gaussian_source_gaussian_receiver":both_case["complex_40_80_hz"]}
def wave_plan(p):
    # Reuse the already audited original-q0 wave integrator. No source fitting.
    return {
      "geometry":p["geometry"],
      "temporal_source":{"dt_s":p["source_waveform"]["dt_s"],
                         "steps":p["source_waveform"]["steps"],
                         "frequency_bins_hz":p["source_waveform"]["frequencies_hz"]},
      "linear_solver":{"rtol":p["solver"]["rtol"],
                       "atol":p["solver"]["atol"],
                       "maxiter":p["solver"]["maxiter"],
                       "true_relative_residual_max":p["solver"]["true_relative_linear_residual_max"]},
    }

def mfem_operators(p,systems,functionals,ref):
    archived=systems/f"mfem-r{ref}.json.gz"
    original=gzip.decompress(archived.read_bytes())
    digest=hashlib.sha256(original).hexdigest()
    if (digest!=HASHES[ref] or
        len(original)>p["resource_limits"]["max_raw_reference_bytes"]):
        raise ValueError("pinned independent P2 operator or byte limit mismatch")
    data=json.loads(original)
    n=DOFS[ref]
    if n>p["resource_limits"]["max_mfem_dofs"]:
        raise ValueError("outside preregistered FEM DOF limit")
    validate_mfem_system(data,refinement=ref,ndofs=n,
       plan={"source_xyz_m":p["geometry"]["source_xyz_m"],
             "receiver_xyz_m":p["geometry"]["receiver_xyz_m"]})
    mass=matrix_from_csr(data["mass_matrix"],ndofs=n,max_nnz=3_000_000)
    stiffness=matrix_from_csr(data["stiffness_c2_matrix"],ndofs=n,max_nnz=3_000_000)
    point_source=np.asarray(data["source_functional"],dtype=float)
    point_receiver=np.asarray(data["receiver_functional"],dtype=float)
    source_file=functionals/f"mfem-gaussian-r{ref}.json"
    raw=source_file.read_bytes()
    saved=json.loads(raw)
    if (saved.get("schema_version")!="htdt.r130d.independent-mfem-spatial-gaussian-functionals-1"
        or saved.get("order")!=2 or saved.get("refinement")!=ref
        or saved.get("dofs")!=n or saved.get("quadrature_order")!=10
        or [x["sigma_m"] for x in saved["cases"]]!=list(WIDTHS)):
        raise ValueError("independent P2 source/receiver quadrature contract changed")
    by_width={}
    for case in saved["cases"]:
        sigma=case["sigma_m"]
        src=np.asarray(case["source_functional"],dtype=float)
        recv=np.asarray(case["receiver_functional"],dtype=float)
        by_width[str(sigma)]=(src,recv)
    for weights in (point_source,point_receiver,*[a for pair in by_width.values() for a in pair]):
        if (weights.shape!=(n,) or not np.all(np.isfinite(weights))
            or abs(float(np.sum(weights))-1)>1e-9):
            raise ValueError("MFEM source/receiver did not integrate to unity")
    return mass,stiffness,point_source,point_receiver,by_width,{
        "original_p2_sparse_sha256":digest,
        "independent_gaussian_source_functionals_sha256":hashlib.sha256(raw).hexdigest(),
        "p2_ndofs":n}

def fv_operators(p,n):
    sys=build_sloped_embedded_neumann(n)
    b=interior_point_stencil(sys,tuple(p["geometry"]["source_xyz_m"]))
    r=interior_point_stencil(sys,tuple(p["geometry"]["receiver_xyz_m"]))
    # Pass exact physical properties to previously verified FV quadrature code.
    gaussian_plan={"geometry":p["geometry"],
                   "spatial_kernel":{"sigma_m":list(WIDTHS)}}
    weights,geometry=fv_functionals(sys,gaussian_plan)
    return sys.mass,sys.stiffness,b,r,{
        str(sigma):weights[j] for j,sigma in enumerate(WIDTHS)},{
        "fv_dofs":sys.degrees_of_freedom,"independent_cutcell_quadrature":geometry}
def actually_run_case(p,point,both,systems,functionals,method,level):
    start=time.perf_counter()
    if method=="fv":
        M,K,bs,br,kernels,meta=fv_operators(p,level)
    elif method=="mfem":
        M,K,bs,br,kernels,meta=mfem_operators(p,systems,functionals,level)
    else:raise ValueError("unknown spatial method")
    data={"method":method,"level":level,"operator_provenance":meta,"widths":{}}
    for sigma in WIDTHS:
        key=str(sigma)
        gaussian_src,gaussian_recv=kernels[key]
        controls=reference_case(point,both,method,level,sigma)
        variants=dict(controls)
        details={}
        for name,b,r in (
            ("gaussian_source_point_receiver",gaussian_src,br),
            ("point_source_gaussian_receiver",bs,gaussian_recv)):
            started=time.perf_counter()
            wave=impulse_transfer(M,K,b,r,wave_plan(p),use_pcg=(method=="mfem"))
            if wave["worst_true_relative_linear_residual"]>1e-8:
                raise RuntimeError("mixed spatial actual wave residual failed")
            variants[name]=wave["complex_40_80_hz"]
            details[name]={**wave,"actual_wave_wall_seconds":time.perf_counter()-started}
            print("ONE_SIDED_Q0",method,level,"sigma",sigma,name,
                  wave["complex_40_80_hz"],"linear_res",
                  wave["worst_true_relative_linear_residual"],flush=True)
        impacts={v:metrics(variants["point_source_point_receiver"], variants[v])
                 for v in (
                    "gaussian_source_point_receiver",
                    "point_source_gaussian_receiver",
                    "gaussian_source_gaussian_receiver")}
        data["widths"][key]={"control_point_point_complex_40_80_hz":
                                variants["point_source_point_receiver"],
                            "control_gaussian_gaussian_complex_40_80_hz":
                                variants["gaussian_source_gaussian_receiver"],
                            "mixed_wave":details,"regularization_impact_vs_original_point":impacts}
    data["wall_seconds"]=time.perf_counter()-start
    return data

def compare_adjacent(results):
    comparison={}
    for method in ("fv","mfem"):
        two=sorted((x for x in results if x["method"]==method),key=lambda x:x["level"])
        if len(two)!=2:
            raise ValueError("missing or reordered FV/P2 refinement pair")
        out={}
        for sigma in WIDTHS:
            key=str(sigma)
            a,b=(x["widths"][key] for x in two)
            point_a,point_b=(x["control_point_point_complex_40_80_hz"] for x in (a,b))
            both_a,both_b=(x["control_gaussian_gaussian_complex_40_80_hz"] for x in (a,b))
            out[key]={
              "point_source_point_receiver":metrics(point_a,point_b),
              "gaussian_source_gaussian_receiver":metrics(both_a,both_b)}
            for variant in OPERATORS:
                out[key][variant]=metrics(a["mixed_wave"][variant]["complex_40_80_hz"],
                                           b["mixed_wave"][variant]["complex_40_80_hz"])
        comparison[method]=out
    return comparison

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--systems",type=Path,required=True)
    ap.add_argument("--functionals",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    arg=ap.parse_args()
    plan_bytes=arg.plan.read_bytes()
    p=validate_plan(json.loads(plan_bytes))
    point,both=pinned_baselines(p)
    result={"schema_version":"htdt.r130d.one-sided-q0-independent-operator-evidence-1",
            "plan_sha256_lf":hashlib.sha256(plan_bytes.replace(b"\r\n",b"\n")).hexdigest(),
            "pinned_point_baseline_sha256":POINT_SHA,
            "pinned_double_gaussian_baseline_sha256":BOTH_SHA,
            "preregistered_plan":p,"actual_mixed_cases":[]}
    for method,levels in (("fv",FV_NS),("mfem",P2_RS)):
        for level in levels:
            row=actually_run_case(p,point,both,arg.systems,arg.functionals,
                                 method,level)
            result["actual_mixed_cases"].append(row)
            partial=arg.output.with_name(arg.output.stem+"_partial.json")
            partial.parent.mkdir(parents=True,exist_ok=True)
            partial.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    result["adjacent_metrics"]=compare_adjacent(result["actual_mixed_cases"])
    result.update({"canonical_original_point_q0":"SELF_CONVERGENCE_FAILED",
                   "source_receiver_spatial_operator_was_changed":True,
                   "physical_validation":"NOT_VALIDATED",
                   "production_ready":False})
    arg.output.parent.mkdir(parents=True,exist_ok=True)
    arg.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("ONE_SIDED COMPLETE, canonical still FAIL; evidence",arg.output,flush=True)
    for method,data in result["adjacent_metrics"].items():
        for sigma,variants in data.items():
            print("COMPARISON",method,sigma,[(name,round(v["normalized_complex_l2"],6))
                  for name,v in variants.items()],flush=True)

if __name__=="__main__":
    main()
