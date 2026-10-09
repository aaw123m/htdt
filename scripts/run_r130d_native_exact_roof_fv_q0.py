#!/usr/bin/env python3
"""Experimental exact roof cutcell FV: original pinned 8-node q0 at native PPW28-44.

Changes geometric approximation and temporal solver only, explicitly isolated
from canonical original PFFDTD point-source qualification.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import numpy as np
import h5py

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
from htdt.r130d_native_grid_exact_roof_fv import (
    build_native_exact_roof_fv,implicit_newmark_original_q0)
from htdt.acoustic_pffdtd_adapter import (
    pffdtd_velocity_potential_to_pressure_trace,finite_record_pressure_transfer)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_point_quadratic_pffdtd import file_hash,PPW,PIN

SCHEMA="htdt.r130d.native-grid-exact-roof-mass-fv-q0-plan-1"
def verify_plan(p):
    if (p.get("schema_version")!=SCHEMA
        or p["native_original"]["ppw"]!=list(PPW)
        or p["native_original"]["upstream_pin"]!=PIN
        or p["native_original"]["source_xyz_m"]!=[1.5,2,2]
        or p["native_original"]["receiver_xyz_m"]!=[2.5,2,2]
        or p["native_original"]["frequencies_hz"]!=[40,80]
        or p["native_original"]["original_record_s"]!=[0,.25]
        or p["algorithm"]["max_CG_iterations"]!=500
        or p["algorithm"]["CG_tolerance_rtol"]!=1e-10
        or p["algorithm"]["energy_drift_max"]!=1e-6
        or p["comparisons"]["complex_relative_threshold"]!=.2
        or p["comparisons"]["magnitude_relative_threshold"]!=.25
        or p["comparisons"]["phase_deg_threshold"]!=15
        or p["comparisons"]["pairs"]!=[[28,32],[32,36],[36,40],[40,44]]
        or p["limits"]["no_github_actions_runs"] is not True
        or p["authority"]["original_eight_node_q0"]!="SELF_CONVERGENCE_FAILED"
        or p["authority"]["product"]!="NO_GO"):
        raise ValueError("prospective exact roof original point q0 plan changed")
    return p

def pairs(z):
    x=np.asarray(z,dtype=complex)
    if x.shape!=(2,) or not np.all(np.isfinite(x)):
        raise ValueError("expected exactly 40/80Hz signed complex transfer")
    return [[float(q.real),float(q.imag)] for q in x]

def unpairs(q):
    a=np.asarray(q,dtype=float)
    if a.shape!=(2,2):raise ValueError("invalid two native pressure transfer bins")
    return a[:,0]+1j*a[:,1]

def one(p,ppw,sim,original):
    t0=time.perf_counter()
    if (file_hash(sim/"comms_out.h5")!=original["original_native_comm_sha256"]
        or file_hash(sim/"vox_out.h5")!=original["original_solver_geometry_sha256"]):
        raise ValueError("frozen original physical source/geometry SHA mismatch")
    with h5py.File(sim/"vox_out.h5","r") as h:
        axis=[np.asarray(h[n][...],dtype=float) for n in ("xv","yv","zv")]
    with h5py.File(sim/"comms_out.h5","r") as h:
        src=np.asarray(h["in_ixyz"][...],dtype=np.int64)
        receiver=np.asarray(h["out_ixyz"][...],dtype=np.int64)
        sig=np.asarray(h["in_sigs"][...],dtype=np.float64)
        receiver_alpha=np.asarray(h["out_alpha"][...],dtype=np.float64)
        nt=int(h["Nt"][()])
        if (src.shape!=(8,) or receiver.shape!=(8,)
            or sig.shape!=(8,nt) or receiver_alpha.shape!=(1,8)
            or np.any(sig[:,1:]!=0) or int(h["diff"][()])!=0):
            raise ValueError("native original eight-point q0 waveform changed")
        sigsum=float(np.sum(sig[:,0]))
        src_w=sig[:,0]/sigsum
        if abs(sum(src_w)-1)>1e-12 or abs(sum(receiver_alpha.ravel())-1)>1e-12:
            raise ValueError("native original source/receiver physical unit weights changed")
    with h5py.File(sim/"sim_consts.h5","r") as h:
        dt=float(h["Ts"][()]); h_m=float(h["h"][()])
        c=float(h["c"][()]);native_l2=float(h["l2"][()])
    if not (nt<=p["limits"]["max_steps"]
        and abs(h_m-343.2/(100*ppw))<1e-12
        and abs(dt-original["native_solver"]["dt_s"])<1e-12
        and abs(native_l2-(c*dt/h_m)**2)<1e-12
        and abs(sigsum-native_l2/h_m)<1e-10):
        raise ValueError("native original q0 PPW/time/source scale drift")
    system=build_native_exact_roof_fv(axis,max_nodes=p["limits"]["max_total_nodes"])
    if (system.number_of_cells>p["limits"]["max_total_nodes"]
        or abs(system.room_fluid_volume_m3-56)>p["algorithm"]["fluid_volume_tolerance_m3"]):
        raise ValueError("exact roof cutcell volume or memory contract breached")
    # Verify exact original native eight-point kick would be reproduced
    # by the same new mass operator in the equal-volume cell limit.
    if not np.isclose(dt*dt*c*c/h_m**3,sigsum,rtol=1e-11,atol=1e-12):
        raise ValueError("native point source forcing volume vs original discrete q0 altered")
    receiver_trace,stats=implicit_newmark_original_q0(
        system,native_dt_s=dt,native_source_ix=src,
        native_source_q0_weights=src_w,native_receiver_ix=receiver,
        native_receiver_weights=receiver_alpha.ravel(),
        native_record_samples=nt,sound_speed_m_s=c,
        solver_rtol=p["algorithm"]["CG_tolerance_rtol"],
        solver_atol=p["algorithm"]["CG_tolerance_atol"],
        max_cg_iter=p["algorithm"]["max_CG_iterations"],
        max_true_relative_residual=p["algorithm"]["max_true_residual_rel"])
    pressure=pffdtd_velocity_potential_to_pressure_trace(
        receiver_trace,time_step_s=dt,density_kg_m3=1.2)
    q0=np.zeros(nt);q0[0]=1.
    signed=finite_record_pressure_transfer(
        pressure,q0,time_step_s=dt,frequency_hz=np.array([40.,80.]))
    if not np.all(np.isfinite(signed)):raise ValueError("cutcell q0 signed full transfer invalid")
    out={"ppw":ppw,
         "original_native_comms_sha256":file_hash(sim/"comms_out.h5"),
         "original_native_geometry_sha256":file_hash(sim/"vox_out.h5"),
         "original_native_raw_wave_sha256":file_hash(sim/"sim_outs.h5"),
         "original_8node_full_signed_40_80":original["unmodified_original_transfer_pa_per_m3_s"],
         "native_original_grid_dimensions":list(system.native_dimensions),
         "native_original_h_m":h_m,"original_native_dt_s":dt,
         "original_record_samples":nt,
         "original_native_unit_q0_total_source_kick":sigsum,
         "experimental_numerical_spatial_scheme":"exact dual cut volumes and fluid-face apertures of sloped rigid room, conservative Neumann FV",
         "experimental_numerical_time_scheme":"Newmark beta1/4 implicit midpoint, original q0 sample clock, no Gaussian",
         "exact_physical_room_volume_m3":system.room_fluid_volume_m3,
         "original_nodal_active_cutcell_count":system.number_of_cells,
         "cutcell_minimum_original_full_voxel_volume_fraction":system.min_volume_fraction,
         "native_exact_roof_stiffness_nonzeros":int(system.stiffness_matrix.nnz),
         "native_exact_roof_total_interior_face_count":len(system.exact_flux_face_open_area_m2),
         "experimental_signed_P_T_over_Q_T_40_80":pairs(signed),
         "original_source_receiver_and_discrete_q0_unchanged":True,
         "original_pffdtd_engine_code_untouched":True,
         "original_full_wave_unchanged":True,
         "solver":stats,
         "native_exact_roof_actual_integration_wall_seconds":float(time.perf_counter()-t0)}
    print("EXACT_NATIVE_ROOF_Q0",ppw,"cells",system.number_of_cells,
          "minV/h3",system.min_volume_fraction,
          "vol",system.room_fluid_volume_m3,
          "CG maxiter",stats["maximum_CG_iterations"],
          "CG maxrel",stats["maximum_true_linear_relative_residual"],
          "energy_drift",stats["relative_energy_drift_after_source"],
          "full40/80",out["experimental_signed_P_T_over_Q_T_40_80"],flush=True)
    return out

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    a=ap.parse_args()
    raw=a.plan.read_bytes()
    p=verify_plan(json.loads(raw))
    controls=json.loads((ROOT/p["native_original"]["original_controls"]).read_text())
    originals={x["ppw"]:x for x in controls["actual_native_wave_cases"]}
    if set(originals)!=set(PPW):raise ValueError("all five original rigid native sources required")
    dirs={}
    for f in a.original_sims_root.rglob("comms_out.h5"):
        sha=file_hash(f)
        matches=[i for i in PPW if sha==originals[i]["original_native_comm_sha256"]]
        if len(matches)==1:
            if matches[0] in dirs:raise ValueError("duplicate original native source case")
            dirs[matches[0]]=f.parent
    if set(dirs)!=set(PPW):raise ValueError("original source SHA-provenance incomplete")
    result={"schema_version":"htdt.r130d.native-exact-roof-mass-fv-q0-evidence-1",
            "plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
            "preregistered_plan":p,
            "actual_native_grid_point_impulse_exact_roof_cases":[],
            "canonical_original_point_q0":"SELF_CONVERGENCE_FAILED",
            "physical_validation":"NOT_VALIDATED","product":"NO_GO",
            "actions_launched":0}
    for ppw in PPW:
        try:
            case=one(p,ppw,dirs[ppw],originals[ppw])
        except Exception as e:
            result["actual_native_grid_point_impulse_exact_roof_cases"].append(
                {"ppw":ppw,"status":"EXPERIMENT_INCOMPLETE",
                 "failure_type":type(e).__name__,"failure_detail":str(e)})
            a.output.parent.mkdir(parents=True,exist_ok=True)
            a.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n")
            raise
        result["actual_native_grid_point_impulse_exact_roof_cases"].append(case)
        a.output.parent.mkdir(parents=True,exist_ok=True)
        a.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n")
    adjacent=[]
    for left,right in zip(result["actual_native_grid_point_impulse_exact_roof_cases"][:-1],
                          result["actual_native_grid_point_impulse_exact_roof_cases"][1:]):
        stats=compare_complex_transfer(
            reference=right["experimental_signed_P_T_over_Q_T_40_80"],
            candidate=left["experimental_signed_P_T_over_Q_T_40_80"],
            frequency_hz=[40,80],
            magnitude_mask_relative_db=-50).model_dump(mode="json")
        adjacent.append({"coarse_ppw":left["ppw"],"fine_ppw":right["ppw"],**stats})
    gate=p["comparisons"]
    qualifying=[x["complex_rms_relative"]<=gate["complex_relative_threshold"]
           and x["magnitude_max_relative"]<=gate["magnitude_relative_threshold"]
           and x["phase_max_deg"]<=gate["phase_deg_threshold"]
           for x in adjacent]
    monotone=all(adjacent[i]["complex_rms_relative"]<
                 adjacent[i-1]["complex_rms_relative"]
                 and adjacent[i]["magnitude_max_relative"]<
                 adjacent[i-1]["magnitude_max_relative"]
                 and adjacent[i]["phase_max_deg"]<
                 adjacent[i-1]["phase_max_deg"]
                 for i in range(1,len(adjacent)))
    result["new_scheme_adjacent_original_q0_point_transfers"]={
        "signed_all_four_adjacent_pairs":adjacent,
        "each_pair_below_original_numeric_limits":qualifying,
        "all_three_errors_strictly_decreasing":monotone,
        "experimental_last_pair_meets_frozen_thresholds":qualifying[-1],
        "experimental_provisional_numerical_refinement_pass":bool(qualifying[-1] and monotone),
        "original_8_10_12_PFFDTD_approval_still_FAILED":True,
        "new_scheme_changes_numerical_spatial_AND_temporal_solver":True,
        "cross_solver_requalification_not_possible":True}
    a.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n")
    print("EXACT_NATIVE_ROOF_FULL_5_DONE",
          [(x["coarse_ppw"],x["fine_ppw"],x["complex_rms_relative"]) for x in adjacent],
          "provisional",result["new_scheme_adjacent_original_q0_point_transfers"]["experimental_provisional_numerical_refinement_pass"],
          "original NOT_QUALIFIED",flush=True)
if __name__=="__main__":
    main()
