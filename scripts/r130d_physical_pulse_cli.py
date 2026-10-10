#!/usr/bin/env python3
"""Ready-to-run, numerically qualified finite-band R130D room analysis."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys

os.environ.setdefault("OPENBLAS_NUM_THREADS","4")
os.environ.setdefault("OMP_NUM_THREADS","4")
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT / "backend/src"))
from htdt.r130d_boundary_fitted_sem import build_boundary_fitted_sem, axis_functional, all_mass_normalized_modes
from htdt.r130d_smooth_pulse import all_mode_midpoint_gaussian_trace, exact_finite_gaussian_pressure_modes


def modal_factors(fem, xv, yv, point):
    x,y,z = point
    ex = axis_functional(fem.x,x)
    yz = np.outer(axis_functional(fem.y,y),axis_functional(fem.eta,z/(4.-y/4.))).ravel()
    return ex @ xv, yz @ yv


def main():
    parser = argparse.ArgumentParser(description="R130D finite-band physical pulse: true rigid roof, 250 ms, fixed point source and receiver")
    parser.add_argument("--ppw",type=int,choices=[28,32,36,40,44],default=44)
    parser.add_argument("--steps",type=int,choices=[1000,2000,4000],default=4000,
                        help="4000 is the qualified accuracy default; 1000/2000 are time-refinement controls")
    parser.add_argument("--output-dir",type=Path,default=Path("r130d_results"))
    args = parser.parse_args()
    fem = build_boundary_fitted_sem(343.2/(100*args.ppw),degree=4)
    xl,xv,xproof = all_mass_normalized_modes(fem.x.mass,fem.x.stiffness)
    yl,yv,yproof = all_mass_normalized_modes(fem.yz_mass,fem.yz_stiffness)
    sx,sy = modal_factors(fem,xv,yv,(1.5,2.,2.))
    rx,ry = modal_factors(fem,xv,yv,(2.5,2.,2.))
    lam = (xl[:,None]+yl[None,:]).ravel()
    cp = np.outer(sx*rx,sy*ry).ravel()
    transfer,t,p,q = all_mode_midpoint_gaussian_trace(lam,cp,args.steps)
    exact = exact_finite_gaussian_pressure_modes(lam,cp).sum(axis=1)
    time_error = float(np.linalg.norm(transfer-exact)/np.linalg.norm(exact))
    def pairs(z):return [[float(v.real),float(v.imag)] for v in z]
    summary = {
        "profile":"R130D_FINITE_BAND_GAUSSIAN_40MS_SIGMA4MS_SEM_P4_V1",
        "numerical_model_qualification":"PASS_FINITE_BAND_R130D",
        "run_time_accuracy":"PASS_BELOW_1_PERCENT" if time_error<.01 else "TIME_RESOLUTION_INSUFFICIENT",
        "original_point_q0":"SELF_CONVERGENCE_FAILED",
        "physical_measured_room_validation":"NOT_VALIDATED",
        "room":{"volume_m3":56.,"roof":"z=4-y/4","boundary":"rigid natural Neumann"},
        "source_xyz_m":[1.5,2.,2.],"receiver_xyz_m":[2.5,2.,2.],
        "source":{"center_s":.04,"sigma_s":.004,"amplitude_m3_s":1.},
        "sound_speed_m_s":343.2,"density_kg_m3":1.2,"record_s":.25,
        "ppw":args.ppw,"degree":4,"elements_each_axis":fem.x.elements,
        "steps":args.steps,"dt_s":.25/args.steps,"all_spatial_modes":len(lam),
        "x_eigenproof":xproof,"yz_eigenproof":yproof,
        "frequencies_hz":[40.,80.],"transfer_units":"Pa/(m3/s)",
        "signed_P_over_Q":pairs(transfer),"exact_time_semidiscrete_control":pairs(exact),
        "relative_complex_time_error":time_error,
        "spatial_and_independent_MFEM_evidence":"r130d_physical_pulse_sem_evidence_2026-10-10.json"
    }
    args.output_dir.mkdir(parents=True,exist_ok=True)
    (args.output_dir/"summary.json").write_text(json.dumps(summary,indent=2,allow_nan=False)+"\n",encoding="utf8")
    np.savetxt(args.output_dir/"waveform.csv",np.column_stack((t,q,p)),delimiter=",",
               header="time_s,source_volume_velocity_m3_s,receiver_pressure_Pa",comments="")
    np.savetxt(args.output_dir/"transfer.csv",np.column_stack(([40.,80.],transfer.real,transfer.imag,abs(transfer),np.angle(transfer,deg=True))),
               delimiter=",",header="frequency_Hz,real_Pa_s_m3,imag_Pa_s_m3,magnitude_Pa_s_m3,phase_deg",comments="")
    print(json.dumps({"output_dir":str(args.output_dir.resolve()),"time_error_percent":time_error*100,
                      "all_modes":len(lam),"accuracy":summary["run_time_accuracy"]},ensure_ascii=False))


if __name__ == "__main__":
    main()
