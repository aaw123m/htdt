#!/usr/bin/env python3
"""Execute preregistered convex cut-cell FV grids n24/n28/n32 with actual q(t).

No reparameterization of original R130D impulse or prior limited Gaussian
reference. All original FV n20 values are inherited from immutable evidence
and cross checked against the new room/drive input.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
from htdt.r130d_embedded_neumann_convex import (
    ConvexPlanarRoom,build_convex_embedded_neumann,
)
from htdt.r130d_embedded_neumann_fv import smooth_source_complex_transfer
from run_r130d_convex_multiplane_cutcell_diagnostic import complex_metrics


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--plan",type=Path,required=True)
    p.add_argument("--baseline-evidence",type=Path,required=True)
    p.add_argument("--out",type=Path,required=True)
    a=p.parse_args()
    plan=json.loads(a.plan.read_text(encoding="utf-8"))
    baseline=json.loads(a.baseline_evidence.read_text(encoding="utf-8"))
    if (plan.get("schema_version")!="htdt.r130d.convex-high-resolution-preregistered-followup-1"
        or plan["new_grid_levels"]!=[24,28,32]
        or plan["fv_refinement"]!=[20,24,28,32]
        or plan["Gaussian_q"]!={"center_s":.012,"sigma_s":.003,
           "dt_s":.00025,"steps":1000,"duration_s":.25,
           "frequencies_hz":[40,80],"time_integrated_actual":True}
        or plan["fail_closed"]["production_enabled"] is not False):
        raise ValueError("high resolution prospective physical plan changed")
    if (baseline["schema_version"]!="htdt.r130d.convex-multiplane-cutcell-numerical-study-1"
        or baseline["production_enabled"] is not False):
        raise ValueError("original convex numerical evidence unrecognized")
    case_results=[]
    for item in plan["cases"]:
        name=item["name"]
        prior=next(x for x in baseline["cases"] if x["name"]==name)
        old=prior["levels"][-1]
        if (old["n"]!=20 or abs(old["exact_cut_cell_volume_m3"]-item["volume_m3"])>5e-12*item["volume_m3"]):
            raise ValueError("changed base FV room result")
        normals=tuple(tuple(map(float,p)) for p in item["planes"])
        geom=ConvexPlanarRoom(normals)
        history=[
           {"n":20,"dofs":old["degrees_of_freedom"],
            "complex_transfer_40_80_hz":old["actual_driven_transfer_40_80_hz"],
            "air_volume_m3":old["exact_cut_cell_volume_m3"],
            "previously_executed_frozen_level":True}
        ]
        for n in plan["new_grid_levels"]:
            start=time.perf_counter()
            op=build_convex_embedded_neumann(
                n,geometry=geom,max_cells=plan["max_total_voxels"])
            volume=float(op.cell_volumes_m3.sum())
            if abs(volume-item["volume_m3"])>5e-9*item["volume_m3"]:
                raise ValueError("new fine cut cells do not integrate exact air room")
            z=smooth_source_complex_transfer(
                op,source_xyz_m=tuple(plan["source_xyz_m"]),
                receiver_xyz_m=tuple(plan["receiver_xyz_m"]),
                frequency_hz=tuple(plan["Gaussian_q"]["frequencies_hz"]),
                duration_s=plan["Gaussian_q"]["duration_s"],
                time_step_s=plan["Gaussian_q"]["dt_s"],
                drive_center_s=plan["Gaussian_q"]["center_s"],
                drive_sigma_s=plan["Gaussian_q"]["sigma_s"],
                density_kg_m3=plan["rho_kg_m3"],
            )
            history.append({
                "n":n,"dofs":op.degrees_of_freedom,
                "complex_transfer_40_80_hz":[[float(v.real),float(v.imag)] for v in z],
                "air_volume_m3":volume,
                "minimum_fluid_fraction":float(op.cell_volumes_m3.min()/op.grid_spacing_m**3),
                "normalized_neumann_residual":float(max(abs(op.stiffness@np.ones(op.degrees_of_freedom)))/
                     max(abs(op.stiffness.diagonal()))),
                "seconds":time.perf_counter()-start,
                "previously_executed_frozen_level":False,
            })
            print("ACTUAL_GAUSSIAN_HIGH_FV",name,n,"DOF",op.degrees_of_freedom,
                  "TRANSFERS",history[-1]["complex_transfer_40_80_hz"],flush=True)
        pairs=[
            {"coarse_n":prev["n"],"fine_n":nxt["n"],**complex_metrics(
              prev["complex_transfer_40_80_hz"],
              nxt["complex_transfer_40_80_hz"])}
            for prev,nxt in zip(history,history[1:])
        ]
        case_results.append({"name":name,"history":history,"adjacent":pairs,
                            "complex_l2_monotone":all(
                              x["complex_l2_relative"]>y["complex_l2_relative"]
                              for x,y in zip(pairs,pairs[1:]))})
        print("FROZEN_HIGH_FV_ADJACENT",name,json.dumps(pairs),flush=True)
    obj={
        "schema_version":"htdt.r130d.experimental-convex-high-fv-evidence-1",
        "plan":plan,"original_evidence_name":a.baseline_evidence.name,
        "actual_time_domain_source":True,"cases":case_results,
        "production_enabled":False,
        "original_r130d_impulse":"SELF_CONVERGENCE_FAILED",
        "independent_cross_solver_new_geometries":"PENDING_MFEM_R4",
        "physical_validation":"NOT_VALIDATED",
    }
    a.out.parent.mkdir(parents=True,exist_ok=True)
    a.out.write_text(json.dumps(obj,indent=2)+"\n",encoding="utf-8")


if __name__=="__main__":
    main()
