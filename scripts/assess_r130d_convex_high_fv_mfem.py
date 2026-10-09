#!/usr/bin/env python3
"""Prereferenced two-bin high FV n20/24/28/32 vs actual independent MFEM P2 r4.

Supplemental nonproduction diagnostic to a prospectively fixed
n20 FV vs MFEM r4 candidate acceptance; NEVER change the older gate.
All high-grid magnitude nonmonotonicity is recorded without omissions.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_fv_mfem_same_drive_comparison import metrics


def convert(v):
    return np.asarray([complex(*x) for x in v],dtype=complex)


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--high-fv",type=Path,required=True)
    p.add_argument("--actual-mfem",type=Path,required=True)
    p.add_argument("--candidate-verdict",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    fv=json.loads(a.high_fv.read_text(encoding="utf-8"))
    mfem=json.loads(a.actual_mfem.read_text(encoding="utf-8"))
    verdict=json.loads(a.candidate_verdict.read_text(encoding="utf-8"))
    if (fv.get("schema_version")!="htdt.r130d.experimental-convex-high-fv-evidence-1"
        or verdict["plan"]["schema_version"]!="htdt.r130d.convex-independent-mfem-plan-1"
        or verdict["schema_version"]!="htdt.r130d.convex-independent-same-source-evidence-1"
        or set(mfem)!=set(x["room"] for x in verdict["cases"])
        or fv["production_enabled"] is not False
        or verdict["production_enabled"] is not False):
        raise ValueError("independent high resolution evidence authority changed")
    out=[]
    for case in fv["cases"]:
        name=case["name"]
        m=mfem[name]
        v=next(x for x in verdict["cases"] if x["room"]==name)
        if [x["uniform_refinements"] for x in m] != [1,2,3,4]:
            raise ValueError("independent MFEM refinement missing")
        if v["experimental_numeric_candidate_pass"] is not True:
            raise ValueError("earlier n20 acceptance not actually complete")
        ref=m[-1]["transfer_complex_40_80_hz"]
        comparisons=[]
        for level in case["history"]:
            row=level["complex_transfer_40_80_hz"]
            cmp=metrics(row,ref)
            aa=convert(row)
            bb=convert(ref)
            db=float(max(abs(20*np.log10(abs(aa)/abs(bb)))))
            comparisons.append({"fv_n":level["n"],"mfem_r":4,
                 "complex_relative":cmp["normalized_complex_l2"],
                 "relative_magnitude_by_hz":cmp["magnitude_relative_by_hz"],
                 "phase_abs_deg_by_hz":cmp["phase_difference_deg_by_hz"],
                 "magnitude_max_db":db})
        prev=v["cross_fv_n20_vs_mfem_r4"]["normalized_complex_l2"]
        if abs(comparisons[0]["complex_relative"]-prev)>1e-12:
            raise ValueError("changed prospectively accepted n20 cross-method metric")
        maxmag=[z["magnitude_max_relative"] for z in case["adjacent"]]
        out.append({
            "room":name,"fine_reference_source":"pinned independent P2 MFEM r4 actually driven in separate CI",
            "comparisons":comparisons,
            "fv_complex_l2_strict_decrease":case["complex_l2_monotone"],
            "fv_magnitude_max_strict_decrease":all(
               x>y for x,y in zip(maxmag,maxmag[1:])),
            "fv_magnitude_adjacent":maxmag,
            "n20_registered_candidate_pass":True,
            "n32_supplemental_cross_validation_only":True,
        })
    payload={
        "schema_version":"htdt.r130d.convex-fv-n32-vs-independent-mfem-r4-supplemental-1",
        "cases":out,
        "original_r130d_impulse":"SELF_CONVERGENCE_FAILED",
        "physical_validation":"NOT_VALIDATED",
        "production_ready":False,
        "new_geometries_high_resolution_full_monotonicity_gate":"NOT_QUALIFIED",
        "comment":"Recorded high-FV n32 comparison is exploratory; wedge/diagonal max-magnitude sequences are nonmonotone and must not be silently passed."
    }
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(payload,indent=2)+"\n",encoding="utf-8")
    for row in out:
        print(row["room"],"fine comparison",row["comparisons"][-1],
              "magnitude strictly decreasing",row["fv_magnitude_max_strict_decrease"],flush=True)


if __name__=="__main__":
    main()
