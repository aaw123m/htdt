#!/usr/bin/env python3
"""Diagnostic-only sampled impulse moment matching on already frozen two dt levels.

Original coarse: q[0]=1 at h=.00025; fine raw q[0]=1 at h=.000125;
fine matched: q[0]=q[1]=1 at h=.000125, conserving source integral
and temporal centroid vs coarse. The changed matched source is NOT a
qualifying replay of the original one-sample impulse.
"""
from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path
import sys
import time
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
sys.path.insert(0,str(ROOT/"scripts"))

from htdt.r130d_embedded_neumann_fv import (
    build_sloped_embedded_neumann,discrete_source_complex_transfer,
)
from run_r130d_exact_discrete_impulse_candidate import compute_mfem_reference
from run_r130d_fv_mfem_same_drive_comparison import metrics


def evaluate(plan,old,time,matched):
    if (plan["schema_version"]!="htdt.r130d.impulse-source-moment-placement-probe-plan-1"
        or plan["source_authority"]["matched_fine"]!={"dt_s":.000125,
           "samples":[1,1],"steps":2000,
           "total_dt_weighted_volume_velocity":.00025,
           "time_centroid_s":.000125}
        or plan["source_authority"]["original_fine"]["samples"]!=[1,0]
        or plan["source_authority"]["original_coarse"]["samples"]!=[1,0]
        or plan["source_authority"]["original_coarse"]["time_centroid_s"]!=.000125
        or plan["gate"]["production_enabled"] is not False
        or plan["gate"]["original_fullband_impulse"]!="SELF_CONVERGENCE_FAILED"):
        raise ValueError("preregistered source moment authority altered")
    prospective=[("exact_sloped_cutcell_neumann_FV",20),
                 ("exact_sloped_cutcell_neumann_FV",32),
                 ("pinned_independent_MFEM_P2",4)]
    if [(x["method"],x["level"]) for x in plan["cases"]]!=prospective:
        raise ValueError("source-moment grid/solver changed")
    if [(x["method"],x["level"]) for x in matched]!=prospective:
        raise ValueError("missing actual matched-source solve")
    if time["preregistered_plan"]["schema_version"]!="htdt.r130d.original-unit-impulse-time-refinement-1":
        raise ValueError("earlier actual dt-half numeric witness unavailable")
    results=[]
    for row in matched:
        method,level=row["method"],row["level"]
        baseline=(next(x for x in old["fv_levels"] if x["n"]==level)
             if method=="exact_sloped_cutcell_neumann_FV" else
             next(x for x in old["mfem_levels"] if x["r"]==level))
        raw=next(x for x in time["cases"] if x["method"]==method and x["level"]==level)
        r1=metrics(baseline["complex_40_80_hz"],raw["fine_dt_transfer_40_80_hz"])
        r2=metrics(baseline["complex_40_80_hz"],row["complex_40_80_hz"])
        r3=metrics(raw["fine_dt_transfer_40_80_hz"],row["complex_40_80_hz"])
        results.append({
          "method":method,"level":level,
          "coarse_raw_vs_fine_raw":r1,
          "coarse_raw_vs_fine_moment_aligned":r2,
          "fine_raw_vs_fine_moment_aligned":r3,
          "coarse_original_complex":baseline["complex_40_80_hz"],
          "fine_raw_complex":raw["fine_dt_transfer_40_80_hz"],
          "fine_moment_aligned_complex":row["complex_40_80_hz"],
          "matched_solver_row":row,
          "centroid_aligned_reduces_complex_difference":bool(
               r2["normalized_complex_l2"]<r1["normalized_complex_l2"]),
        })
    return {
      "schema_version":"htdt.r130d.impulse-source-moment-placement-probe-evidence-1",
      "preregistered_plan":plan,"cases":results,
      "changed_source_is_not_original_unit_impulse":True,
      "temporal_integrator_error_purely_isolated":False,
      "original_fullband_impulse":"SELF_CONVERGENCE_FAILED",
      "physical_validation":"NOT_VALIDATED",
      "production_enabled":False
    }


def main():
    a=argparse.ArgumentParser()
    a.add_argument("--plan",type=Path,required=True)
    a.add_argument("--original",type=Path,required=True)
    a.add_argument("--time-evidence",type=Path,required=True)
    a.add_argument("--systems",type=Path,required=True)
    a.add_argument("--output",type=Path,required=True)
    a=a.parse_args()
    plan=json.loads(a.plan.read_text(encoding="utf-8"))
    old=json.loads(a.original.read_text(encoding="utf-8"))
    time_evidence=json.loads(a.time_evidence.read_text(encoding="utf-8"))
    src=plan["source_authority"]["matched_fine"]
    if (plan["schema_version"]!="htdt.r130d.impulse-source-moment-placement-probe-plan-1"
        or src["samples"]!=[1,1] or src["dt_s"]!=.000125
        or src["steps"]!=2000 or src["time_centroid_s"]!=.000125
        or src["total_dt_weighted_volume_velocity"]!=.00025
        or plan["gate"]["production_enabled"] is not False):
        raise ValueError("prospective moment matching source changed")
    q=np.zeros(2000);q[:2]=1.
    midpoint_times=(np.arange(2000)+.5)*.000125
    if (abs(.000125*q.sum()-.00025)>1e-15 or
        abs(float(midpoint_times@q/q.sum())-.000125)>1e-15):
        raise ValueError("exact impulse first two time moments not matched")
    rows=[]
    geom=old["preregistered_plan"]["geometry"]
    for x in plan["cases"]:
        method,n=x["method"],x["level"]
        start=time.perf_counter()
        if method=="exact_sloped_cutcell_neumann_FV":
            op=build_sloped_embedded_neumann(n)
            v=discrete_source_complex_transfer(
                op,q,source_xyz_m=tuple(geom["source_xyz_m"]),
                receiver_xyz_m=tuple(geom["receiver_xyz_m"]),
                frequency_hz=(40,80),time_step_s=.000125,
                density_kg_m3=geom["density_kg_m3"])
            row={"method":method,"level":n,"steps":2000,
                  "degrees_of_freedom":op.degrees_of_freedom,
                  "exact_room_volume_m3":float(op.cell_volumes_m3.sum()),
                  "complex_40_80_hz":[[float(z.real),float(z.imag)] for z in v],
                  "matched_discrete_source_first_two_samples":[1,1]}
        elif method=="pinned_independent_MFEM_P2":
            p=copy.deepcopy(old["preregistered_plan"])
            p["source"]["dt_s"]=.000125
            p["source"]["steps"]=2000
            row=compute_mfem_reference(
                p,n,a.systems/("mfem-r"+str(n)+".json.gz"),q)
            row.update({"method":method,"level":n,"steps":2000,
                        "matched_discrete_source_first_two_samples":[1,1]})
        else:
            raise ValueError("new method not preapproved")
        elapsed=time.perf_counter()-start
        if elapsed>plan["limits"]["max_case_runtime_s"]:
            raise ValueError("preregistered resource bound violated")
        row["elapsed_s"]=elapsed
        rows.append(row)
        print("ACTUAL_MATCHED_MOMENT",method,n,
              row["complex_40_80_hz"],"elapsed",elapsed,flush=True)
        a.output.parent.mkdir(parents=True,exist_ok=True)
        a.output.with_name(a.output.stem+"_partial.json").write_text(
            json.dumps({"partial":True,"cases":rows},indent=2)+"\n")
    result=evaluate(plan,old,time_evidence,rows)
    a.output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    for row in result["cases"]:
        print("MOMENT_COMPARISON",row["method"],row["level"],
              "raw",row["coarse_raw_vs_fine_raw"]["normalized_complex_l2"],
              "matched",row["coarse_raw_vs_fine_moment_aligned"]["normalized_complex_l2"],
              "placement",row["fine_raw_vs_fine_moment_aligned"]["normalized_complex_l2"],
              flush=True)


if __name__=="__main__":
    main()
