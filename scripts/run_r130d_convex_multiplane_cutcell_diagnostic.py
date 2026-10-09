"""Prospectively fixed exact-convex cut-cell geometry + actually driven wave study.

The standalone finite-volume plane clipping geometry supports bounded convex
polyhedral CAD solids only. Not canonical PFFDTD or physical qualification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys
import time

import numpy as np
from scipy.sparse.linalg import eigsh

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
from htdt.r130d_embedded_neumann_convex import (
    ConvexPlanarRoom,build_convex_embedded_neumann,
)
from htdt.r130d_embedded_neumann_fv import (
    build_sloped_embedded_neumann,smooth_source_complex_transfer,
)

SCHEMA="htdt.r130d.convex-multiplane-cutcell-numerical-study-1"


def complex_metrics(coarse:list[list[float]],fine:list[list[float]]) -> dict:
    a=np.asarray([complex(*v) for v in coarse])
    b=np.asarray([complex(*v) for v in fine])
    return {
       "complex_l2_relative":float(np.linalg.norm(a-b)/np.linalg.norm(b)),
       "magnitude_max_relative":float(np.max(abs(abs(a)-abs(b))/abs(b))),
       "phase_max_deg":float(np.max(abs(np.angle(a/b,deg=True)))),
    }


def execute(plan:dict) -> dict:
    if plan["schema_version"]!="htdt.r130d.experimental-convex-multiplane-cutcell-plan-1":
        raise ValueError("unregistered geometry diagnostic plan")
    if (plan["numerical_levels"]!=[6,8,12,16,20]
        or plan["drive"]!={"gaussian_center_s":0.012,"sigma_s":0.003,
            "actual_injected":True,"dt_s":0.00025,
            "duration_s":0.25,"frequencies_hz":[40,80]}
        or plan["frozen_gate"]["production_enabled"] is not False):
        raise ValueError("physical/observation/authority plan changed")
    cases=[]
    for item in plan["geometry_cases"]:
        g=ConvexPlanarRoom(tuple(
            (*map(float,v["normal"]),float(v["offset"]))
            for v in item["additional_planes"]
        ))
        exact=float(item["exact_volume_m3"])
        rows=[]
        for n in plan["numerical_levels"]:
            start=time.perf_counter()
            s=build_convex_embedded_neumann(n,geometry=g)
            volume=float(s.cell_volumes_m3.sum())
            if abs(volume-exact)>5e-9*exact:
                raise ValueError("analytic independent geometry volume failed")
            a=np.ones(s.degrees_of_freedom)
            max_null=float(np.max(abs(s.stiffness@a)))
            max_diag=float(np.max(abs(s.stiffness.diagonal())))
            max_sym=float(np.max(abs((s.stiffness-s.stiffness.T).data),initial=0))
            test_p=np.cos(np.pi*s.cell_center_xyz_m[:,0]/g.length_m)
            test_v=np.zeros_like(test_p)
            E=s.energy(test_p,test_v)
            integ=s.midpoint_integrator(0.005)
            peak_drift=0
            for _ in range(60):
                test_p,test_v=integ.step(test_p,test_v)
                peak_drift=max(peak_drift,abs(s.energy(test_p,test_v)-E)/E)
            if peak_drift>1e-9:
                raise ValueError("cut-cell energy conservation failed")
            first_frequencies=eigsh(
                s.stiffness,k=4,M=s.mass,sigma=-1,
                which="LM",return_eigenvectors=False,
            )
            freqs=sorted(float(v) for v in
                         np.sqrt(np.maximum(0,first_frequencies))/(2*np.pi)
                         if v>1e-5)
            actual=smooth_source_complex_transfer(
                s,
                source_xyz_m=tuple(plan["source"]),
                receiver_xyz_m=tuple(plan["receiver"]),
            )
            row={
                "n":n,"degrees_of_freedom":s.degrees_of_freedom,
                "exact_cut_cell_volume_m3":volume,
                "minimum_cut_volume_fraction":float(s.cell_volumes_m3.min()/s.grid_spacing_m**3),
                "fluid_interface_count":len(s.fluid_face_areas_m2),
                "null_max_normalized_by_diagonal":max_null/max_diag,
                "max_abs_stiffness_asymmetry":max_sym,
                "relative_energy_drift_60_steps":peak_drift,
                "smallest_positive_modal_frequency_hz":freqs[0],
                "actual_driven_transfer_40_80_hz":[[float(z.real),float(z.imag)] for z in actual],
                "wall_seconds":time.perf_counter()-start,
            }
            if item["existing_legacy_analytical_builder_cross_check"]:
                old=build_sloped_embedded_neumann(n)
                if not np.array_equal(s.cell_coordinates_ijk,old.cell_coordinates_ijk):
                    raise ValueError("legacy exact sloped coordinate incompatibility")
                row["legacy_sloped_volume_max_abs_difference"]=float(
                    max(abs(s.cell_volumes_m3-old.cell_volumes_m3)))
                row["legacy_sloped_stiffness_fro_relative"]=float(
                    np.linalg.norm((s.stiffness-old.stiffness).data)/
                    np.linalg.norm(old.stiffness.data))
                if row["legacy_sloped_stiffness_fro_relative"]>1e-12:
                    raise ValueError("generic operator changed old sloped physics")
            rows.append(row)
            print("ACTUAL_CONVEX",item["name"],n,"DOF",s.degrees_of_freedom,
                  "VOLUME",volume,"TRANSFER",row["actual_driven_transfer_40_80_hz"],
                  "DRIFT",peak_drift,flush=True)
        pairs=[
            {"coarse_n":a["n"],"fine_n":b["n"],
             **complex_metrics(a["actual_driven_transfer_40_80_hz"],
                               b["actual_driven_transfer_40_80_hz"])}
            for a,b in zip(rows,rows[1:])
        ]
        print("PAIRS",item["name"],json.dumps(pairs),flush=True)
        cases.append({"name":item["name"],"analytic_volume_m3":exact,
                      "levels":rows,"adjacent_transfer":pairs})
    return {
        "schema_version":SCHEMA,
        "method":"exact convex planar cut cell mass/face aperture + conservative rigid Neumann and implicit-midpoint actual source injection",
        "physical_authority":"EXPERIMENTAL_ONLY",
        "original_solver_modified":False,
        "candidate_sloped_r130d_previous_success_untouched":True,
        "general_cad_qualified":False,
        "new_geometries_cross_solver_qualified":False,
        "production_enabled":False,
        "plan":plan,
        "runtime":{"numpy":np.__version__,"platform":platform.platform(),
                   "python":sys.version.split()[0]},
        "cases":cases,
    }


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--plan",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    plan=json.loads(a.plan.read_text(encoding="utf-8"))
    evidence=execute(plan)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(evidence,indent=2)+"\n",encoding="utf-8")


if __name__=="__main__":
    main()
