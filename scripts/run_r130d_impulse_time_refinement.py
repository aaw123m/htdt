#!/usr/bin/env python3
"""Actual unit-sampled-impulse dt-halving FV/MFEM P2, with archived dt=.25ms.

Preregistered .125ms run changes only timestep and first midpoint
placement; DOES NOT claim to separate those two effects. Production
and legacy fullband impulse authority remain blocked.
"""
from __future__ import annotations
import argparse
import copy
import json
import math
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


def _complex(row,field="complex_40_80_hz"):
    a=np.asarray(row[field],dtype=float)
    if a.shape!=(2,2) or not np.all(np.isfinite(a)):
        raise ValueError("incomplete 40/80Hz observation")
    return a[:,0]+1j*a[:,1]


def _pair(a,b):
    return metrics(a,b)


def evaluate(plan,original,high):
    if (plan["schema_version"]!="htdt.r130d.original-unit-impulse-time-refinement-1"
        or plan["timestep_s"]!=[.00025,.000125]
        or plan["new_executions_only_dt_s"]!=.000125
        or plan["source"]["q_samples"]!="q[0]=1 m3/s, q[n>0]=0"
        or plan["interpretation"]["production_ready"] is not False
        or plan["interpretation"]["original_fullband_impulse_self"]!="SELF_CONVERGENCE_FAILED"):
        raise ValueError("prospective dt-only physical contract altered")
    assert original["original_fullband_r130d"]=="SELF_CONVERGENCE_FAILED"
    if [x["n"] for x in original["fv_levels"]]!=[12,16,20,24,28,32] or [
        x["r"] for x in original["mfem_levels"]] != [1,2,3,4]:
        raise ValueError("original dt=.25ms full spatial evidence missing")
    if (plan["reference_previous_gates"]!={"fv_self_limit":.2,
            "mfem_self_limit":.05,"cross_limit":.35}
        or plan["interpretation"]["new_candidate_impulse_numeric"]!="NOT_QUALIFIED"):
        raise ValueError("original impulse failure or numerical limits weakened")
    required=[("exact_sloped_cutcell_neumann_FV",20),
              ("exact_sloped_cutcell_neumann_FV",28),
              ("exact_sloped_cutcell_neumann_FV",32),
              ("pinned_independent_MFEM_P2",3),
              ("pinned_independent_MFEM_P2",4)]
    if [(x["method"],x["level"]) for x in plan["spatial_cases"]]!=required:
        raise ValueError("changed prospective FV/P2 grid set")
    if [(x["method"],x["level"]) for x in high]!=required:
        raise ValueError("missing dt-half true wave solve")
    cases=[]
    for item in high:
        method,level=item["method"],item["level"]
        old=(next(x for x in original["fv_levels"] if x["n"]==level)
             if method=="exact_sloped_cutcell_neumann_FV" else
             next(x for x in original["mfem_levels"] if x["r"]==level))
        t0=_complex(old)
        t1=_complex(item)
        cases.append({
            "method":method,"level":level,
            "record_legacy_dt_s":.00025,"new_dt_s":.000125,
            "coarse_dt_transfer_40_80_hz":old["complex_40_80_hz"],
            "fine_dt_transfer_40_80_hz":item["complex_40_80_hz"],
            "fixed_grid_halved_dt_metrics":_pair(old["complex_40_80_hz"],
                                                item["complex_40_80_hz"]),
            "source_moments_not_identical_due_midpoint_shift":True,
            "actual_fine_result":item,
        })
    by={(a["method"],a["level"]):a for a in cases}
    spatial=[]
    for method,a,b in [
        ("exact_sloped_cutcell_neumann_FV",20,28),
        ("exact_sloped_cutcell_neumann_FV",28,32),
        ("pinned_independent_MFEM_P2",3,4),
    ]:
        coarse=by[(method,a)]["fine_dt_transfer_40_80_hz"]
        fine=by[(method,b)]["fine_dt_transfer_40_80_hz"]
        spatial.append({"method":method,"coarse":a,"fine":b,
                        "dt_s":.000125,"metrics":_pair(coarse,fine)})
    cross=_pair(by[("exact_sloped_cutcell_neumann_FV",32)]["fine_dt_transfer_40_80_hz"],
                by[("pinned_independent_MFEM_P2",4)]["fine_dt_transfer_40_80_hz"])
    fine_fv=next(x for x in spatial if x["method"]=="exact_sloped_cutcell_neumann_FV" and x["fine"]==32)
    fine_fem=next(x for x in spatial if x["method"]=="pinned_independent_MFEM_P2")
    # No new acceptance criteria, only report the actual old frozen limits.
    status={
       "FV_n28_to_32_new_dt_complex":fine_fv["metrics"]["normalized_complex_l2"],
       "FEM_r3_to_r4_new_dt_complex":fine_fem["metrics"]["normalized_complex_l2"],
       "FV_n32_to_FEM_r4_new_dt_complex":cross["normalized_complex_l2"],
       "FV_self_under_frozen_complex_limit":bool(
           fine_fv["metrics"]["normalized_complex_l2"] <= plan["reference_previous_gates"]["fv_self_limit"]),
       "FEM_self_under_frozen_complex_limit":bool(
           fine_fem["metrics"]["normalized_complex_l2"] <= plan["reference_previous_gates"]["mfem_self_limit"]),
       "cross_under_frozen_complex_limit":bool(
           cross["normalized_complex_l2"] <= plan["reference_previous_gates"]["cross_limit"]),
       "overall_original_fullband_impulse":"SELF_CONVERGENCE_FAILED",
       "not_full_spatial_monotonicity_gate":True,
       "not_temporal_only_because_midpoint_source_move":True,
       "production_ready":False
    }
    return {
       "schema_version":"htdt.r130d.original-unit-impulse-time-refinement-evidence-1",
       "preregistered_plan":plan,
       "original_evidence_name":"r130d_exact_discrete_impulse_candidate_evidence_2026-10-09.json",
       "cases":cases,"spatial_at_halved_dt":spatial,
       "fv_n32_mfem_r4_halved_dt_cross":cross,
       "diagnostic":status,
       "source_waveform":"q[0]=1, others zero for both dt values",
       "physical_validation":"NOT_VALIDATED",
       "production_ready":False
    }


def main():
    a=argparse.ArgumentParser()
    a.add_argument("--plan",type=Path,required=True)
    a.add_argument("--original",type=Path,required=True)
    a.add_argument("--systems",type=Path,required=True)
    a.add_argument("--output",type=Path,required=True)
    a=a.parse_args()
    plan=json.loads(a.plan.read_text(encoding="utf-8"))
    original=json.loads(a.original.read_text(encoding="utf-8"))
    if (plan.get("schema_version")!="htdt.r130d.original-unit-impulse-time-refinement-1"
        or plan["sample_counts"]!=[1000,2000]
        or plan["timestep_s"]!=[.00025,.000125]
        or plan["record_length_s"]!=.25
        or plan["source"]["frequency_hz"]!=[40,80]
        or plan["source"]["q_samples"]!="q[0]=1 m3/s, q[n>0]=0"
        or plan["interpretation"]["production_ready"] is not False):
        raise ValueError("preregistered time-refinement setup changed")
    q=np.zeros(2000,dtype=float);q[0]=1.0
    fine=[]
    g=original["preregistered_plan"]["geometry"]
    for case in plan["spatial_cases"]:
        start=time.perf_counter()
        typ,n=case["method"],case["level"]
        if typ=="exact_sloped_cutcell_neumann_FV":
            sys_fv=build_sloped_embedded_neumann(n)
            z=discrete_source_complex_transfer(
                sys_fv,q,
                source_xyz_m=tuple(g["source_xyz_m"]),
                receiver_xyz_m=tuple(g["receiver_xyz_m"]),
                frequency_hz=(40.,80.),time_step_s=.000125,
                density_kg_m3=g["density_kg_m3"],
            )
            row={"method":typ,"level":n,
                 "dofs":sys_fv.degrees_of_freedom,
                 "air_volume_m3":float(sys_fv.cell_volumes_m3.sum()),
                 "complex_40_80_hz":[[float(x.real),float(x.imag)] for x in z],
                 "source_samples":[1,0],"step_count":2000,
                 "elapsed_s":time.perf_counter()-start}
        elif typ=="pinned_independent_MFEM_P2":
            p=copy.deepcopy(original["preregistered_plan"])
            p["source"]["dt_s"]=.000125
            p["source"]["steps"]=2000
            row=compute_mfem_reference(
                p,n,a.systems/("mfem-r"+str(n)+".json.gz"),q)
            row.update({"method":typ,"level":n,"step_count":2000,
                        "elapsed_s":time.perf_counter()-start})
        else:
            raise ValueError("unsupported spatial method")
        if row["elapsed_s"]>plan["limits"]["max_runtime_per_case_s"]:
            raise RuntimeError("prospective per-case resource bound exceeded")
        fine.append(row)
        print("ACTUAL_HALF_DT_IMPULSE",typ,n,
              row["complex_40_80_hz"],"elapsed",row["elapsed_s"],flush=True)
        # Preserve an explicitly incomplete audit trail in scratch if later
        # high-Dof case fails; never confuse partial record with gate evidence.
        partial=a.output.with_name(a.output.stem+"_partial.json")
        partial.parent.mkdir(parents=True,exist_ok=True)
        partial.write_text(json.dumps({"partial":True,"cases":fine},indent=2)+"\n",
                           encoding="utf-8")
    result=evaluate(plan,original,fine)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,indent=2,ensure_ascii=True)+"\n",encoding="utf-8")
    print("FIXED_SPATIAL_DT_HALVING",json.dumps([
         {"method":z["method"],"level":z["level"],
         "error":z["fixed_grid_halved_dt_metrics"]["normalized_complex_l2"]}
         for z in result["cases"]]),flush=True)
    print("HALF_DT_SPATIAL",json.dumps(result["diagnostic"]),flush=True)


if __name__=="__main__":
    main()
