"""Bounded *actual driven solver* study of experimental embedded Neumann FV.

The 3 ms Gaussian source is injected at every midpoint time step in the
semidiscrete wave equation. Not a postprocessed preexisting impulse and
not a frozen R130D PFFDTD acceptance replay.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import platform
import sys

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"backend"/"src"))
from htdt.r130d_embedded_neumann_fv import (
    build_sloped_embedded_neumann,smooth_source_complex_transfer,
)


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--levels",nargs="+",type=int,default=[6,8,12,16])
    args=p.parse_args()
    results=[]
    for n in args.levels:
        s=build_sloped_embedded_neumann(n)
        response=smooth_source_complex_transfer(s)
        row={"grid_cells_per_axis":n,"active_cells":s.degrees_of_freedom,
             "cell_h_m":s.grid_spacing_m,
             "transfer_complex_40_80_hz":[[float(x.real),float(x.imag)] for x in response]}
        results.append(row)
        print("ACTUAL_DRIVEN_FV",n,row["transfer_complex_40_80_hz"],flush=True)
    comparisons=[]
    for a,b in zip(results,results[1:]):
        x=np.array([complex(*v) for v in a["transfer_complex_40_80_hz"]])
        y=np.array([complex(*v) for v in b["transfer_complex_40_80_hz"]])
        relative=float(np.linalg.norm(x-y)/np.linalg.norm(y))
        per_bin=[float(abs(v-w)/abs(w)) for v,w in zip(x,y)]
        phase_deg=[float(abs(np.angle(v/w,deg=True))) for v,w in zip(x,y)]
        comparisons.append({
            "coarse_n":a["grid_cells_per_axis"],
            "fine_n":b["grid_cells_per_axis"],
            "normalized_complex_rms":relative,
            "per_bin_complex_relative":per_bin,
            "per_bin_phase_difference_deg":phase_deg,
        })
        print("PAIR",a["grid_cells_per_axis"],b["grid_cells_per_axis"],
              "RMS",relative,"PHASE",phase_deg,flush=True)
    payload={
        "schema":"htdt.r130d.embedded-fv-actual-bandlimited-source-1",
        "experimental_solver":"embedded-plane-Neumann conservative finite-volume implicit-midpoint",
        "physical_model_change_from_frozen_run25":True,
        "actual_driven_solver_execution":True,
        "pffdtd_canonical_self_convergence":"SELF_CONVERGENCE_FAILED",
        "cross_solver_eligible":False,
        "production_ready":False,
        "physical_contract":{
            "fixture":"x,y=0..4m; z=0..4-y/4m; rigid Neumann; c=343.2m/s",
            "source_xyz_m":[1.5,2.0,2.0],
            "receiver_xyz_m":[2.5,2.0,2.0],
            "frequency_hz":[40.0,80.0],
            "duration_s":0.25,
            "time_step_s":0.00025,
            "Gaussian_volume_velocity_center_s":0.012,
            "Gaussian_volume_velocity_sigma_s":0.003,
            "analysis_kernel":"exp(+i omega t) midpoint left-rectified",
            "source_normalization":"complex P_T/Q_T, no fitted phase or gain",
        },
        "runtime":{"numpy":np.__version__,"platform":platform.platform()},
        "levels":results,"pairs":comparisons,
    }
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(payload,indent=2)+"\n",encoding="utf-8")


if __name__=="__main__":
    main()
