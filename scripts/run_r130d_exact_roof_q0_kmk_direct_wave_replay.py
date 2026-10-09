#!/usr/bin/env python3
"""Independent *actual full-state* original-q0 replay of experimental KM^-1K FV."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import h5py
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
from htdt.r130d_native_grid_exact_roof_fv import (
    build_native_exact_roof_fv,implicit_newmark_original_q0)
from htdt.r130d_conservative_dispersion_correction import (
    dispersion_corrected_exact_roof_system)
from htdt.acoustic_pffdtd_adapter import (
    pffdtd_velocity_potential_to_pressure_trace,finite_record_pressure_transfer)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_point_quadratic_pffdtd import PIN,file_hash
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs


def validate_plan(p):
    f=p["frozen"];s=p["numerical"];a=p["acceptance"];limits=p["limits"]
    if (p["schema_version"]!="htdt.r130d.exact-roof-q0-kmk-direct-cg-replay-plan-1"
        or p["issue"]!=938 or p["pr"]!=1055
        or f["ppw"]!=[40,44] or f["frozen_upstream_sha"]!=PIN
        or f["source_xyz_m"]!=[1.5,2,2] or f["receiver_xyz_m"]!=[2.5,2,2]
        or f["record_s"]!=.25 or f["scored_frequency_hz"]!=[40,80]
        or f["roof_exact_volume"]!=56 or f["rho"]!=1.2 or f["c"]!=343.2
        or not f["original_raw_eight_point_source_receiver_hdf5_sha_checks"]
        or not f["original_native_each_dt_Nt"]
        or [s[x] for x in ("cg_rtol","cg_atol","cg_maxiter",
                           "max_true_relative_residual","max_energy_drift",
                           "compare_independent_spectral_exact_all_modes_relative_complex_max")]
             !=[1e-10,1e-12,500,5e-10,1e-6,2e-5]
        or not s["full_state_all_cells"] or not s["save_both_fullwave_complex_bins_and_failures"]
        or a["original_complex_limit"]!=.2 or a["original_magnitude_limit"]!=.25
        or a["original_phase_deg_limit"]!=15 or not a["report_all_metrics_and_failure"]
        or limits!={"max_fullwave_cases":2,"max_additional_original_native_PFFDTD_waves":0,
                     "new_github_actions_runs":0,"do_not_delete_or_clean_scratch":True}
        or p["authority"]["original_pffdtd_q0"]!="SELF_CONVERGENCE_FAILED"
        or p["authority"]["physics"]!="NOT_VALIDATED"
        or p["authority"]["product"]!="NO_GO"):
        raise ValueError("frozen full-state corrected q0 replay plan altered")
    return p


def true_full_state_case(ppw,folder,original,modal,plan):
    clock=time.perf_counter()
    if file_hash(folder/"comms_out.h5")!=original["original_native_comm_sha256"] or (
        file_hash(folder/"vox_out.h5")!=original["original_solver_geometry_sha256"]):
        raise ValueError("frozen raw original point q0 geometry or source SHA changed")
    with h5py.File(folder/"vox_out.h5","r") as h:
        axes=[np.asarray(h[k][...],dtype=np.float64) for k in ("xv","yv","zv")]
    with h5py.File(folder/"comms_out.h5","r") as h:
        src=np.asarray(h["in_ixyz"][:],dtype=np.int64)
        recv=np.asarray(h["out_ixyz"][:],dtype=np.int64)
        sig=np.asarray(h["in_sigs"][:],dtype=np.float64)
        rw=np.asarray(h["out_alpha"][:],dtype=np.float64).ravel()
        nt=int(h["Nt"][()])
        if (src.shape!=(8,) or recv.shape!=(8,) or sig.shape!=(8,nt)
            or rw.shape!=(8,) or np.any(sig[:,1:]!=0)
            or int(h["diff"][()])!=0):
            raise ValueError("original eight point q0 pressure simulation changed")
        sw=sig[:,0]/sum(sig[:,0])
        strength=float(sum(sig[:,0]))
    with h5py.File(folder/"sim_consts.h5","r") as h:
        dt=float(h["Ts"][()]);h_m=float(h["h"][()])
        c=float(h["c"][()]);l2=float(h["l2"][()])
    if (abs(h_m-c/(100*ppw))>1e-12
        or abs(dt-modal["native_dt_s"])>1e-12
        or nt!=modal["native_nt"] or abs(c-343.2)>1e-12
        or abs(strength-l2/h_m)>1e-10
        or abs(sum(sw)-1)>1e-12 or abs(sum(rw)-1)>1e-12):
        raise ValueError("original native q0 sample clock and source scaling changed")
    full=build_native_exact_roof_fv(axes,max_nodes=150000)
    corrected=dispersion_corrected_exact_roof_system(full,sound_speed_m_s=c)
    if (corrected.number_of_cells!=modal["all_true_3D_native_roof_modes_count"]
        or corrected.stiffness_matrix.nnz!=modal["actual_true_KmkK_3D_sparse_nnz"]
        or abs(corrected.room_fluid_volume_m3-56)>2e-8):
        raise ValueError("true 3D sparse corrected operator changed")
    trace,solver=implicit_newmark_original_q0(
        corrected,native_dt_s=dt,native_source_ix=src,
        native_source_q0_weights=sw,native_receiver_ix=recv,
        native_receiver_weights=rw,native_record_samples=nt,
        sound_speed_m_s=c,solver_rtol=plan["numerical"]["cg_rtol"],
        solver_atol=plan["numerical"]["cg_atol"],
        max_cg_iter=plan["numerical"]["cg_maxiter"],
        max_true_relative_residual=plan["numerical"]["max_true_relative_residual"])
    pressure=pffdtd_velocity_potential_to_pressure_trace(
        trace,time_step_s=dt,density_kg_m3=1.2)
    source=np.zeros(nt,dtype=float);source[0]=1
    transfer=finite_record_pressure_transfer(
        pressure,source,time_step_s=dt,frequency_hz=np.array([40.,80.]))
    ref=unpairs(modal["full_untruncated_source_receiver_q0_signed_two_bin_by_arm"]
                ["conservative_kmk_dispersion_newmark"])
    relative=float(np.linalg.norm(transfer-ref)/max(np.linalg.norm(ref),1e-14))
    if relative>plan["numerical"]["compare_independent_spectral_exact_all_modes_relative_complex_max"]:
        raise ValueError(f"full native q0 CG wave does not agree with independent ALL modes {relative}")
    if solver["relative_energy_drift_after_source"]>plan["numerical"]["max_energy_drift"]:
        raise ValueError("corrected full time wave energy drift")
    return {"ppw":ppw,"original_comms_sha256":file_hash(folder/"comms_out.h5"),
        "original_voxel_sha256":file_hash(folder/"vox_out.h5"),
        "native_timestep_s":dt,"native_samples":nt,
        "true_active_wave_dofs":full.number_of_cells,
        "original_q0_source_strength":strength,"exact_roof_volume_m3":full.room_fluid_volume_m3,
        "true_new_full_state_250ms_pressure_signed_P_over_Q_40_80":pairs(transfer),
        "previous_independent_ALL_mode_signed_40_80":pairs(ref),
        "actual_full_state_vs_independent_modal_complex_relative":relative,
        "CG_solver_and_energy_diagnostics":solver,
        "original_physical_source_receiver_q0_unmodified":True,
        "wall_seconds":float(time.perf_counter()-clock)}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--plan",type=Path,required=True)
    parser.add_argument("--original-sims-root",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    arg=parser.parse_args()
    raw=arg.plan.read_bytes()
    plan=validate_plan(json.loads(raw.decode("utf-8")))
    modal=json.loads((ROOT/plan["frozen"]["full_mode_candidate_evidence"]).read_text(encoding="utf-8"))
    orig=json.loads((ROOT/plan["frozen"]["prior_original_pffdtd_sha_evidence"]).read_text(encoding="utf-8"))
    spectrum={x["ppw"]:x for x in modal["actual_full_mode_exact_roof_kmk_cases"]}
    controls={x["ppw"]:x for x in orig["actual_native_wave_cases"]}
    if set(spectrum)!={40,44} or not {40,44}.issubset(controls):
        raise ValueError("source/modal evidence missing")
    dirs={}
    for file in arg.original_sims_root.rglob("comms_out.h5"):
        sha=file_hash(file)
        matches=[i for i in (40,44) if sha==controls[i]["original_native_comm_sha256"]]
        if len(matches)==1:
            if matches[0] in dirs:raise ValueError("duplicate original source HDF5")
            dirs[matches[0]]=file.parent
    if set(dirs)!={40,44}:
        raise ValueError("raw source HDF5 missing")
    out={"schema_version":"htdt.r130d.exact-roof-q0-kmk-direct-wave-replay-evidence-1",
         "plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
         "preregistered_plan":plan,"original_PFFDTD_q0":"SELF_CONVERGENCE_FAILED",
         "physical_validation":"NOT_VALIDATED","product":"NO_GO",
         "new_PFFDTD_wave_runs":0,"new_Actions_runs":0,
         "actual_true_corrected_KmkK_fv_direct_wave_cases":[]}
    arg.output.parent.mkdir(parents=True,exist_ok=True)
    for ppw in (40,44):
        try:result=true_full_state_case(ppw,dirs[ppw],controls[ppw],spectrum[ppw],plan)
        except Exception as exc:
            out["actual_true_corrected_KmkK_fv_direct_wave_cases"].append({
                "ppw":ppw,"status":"INDEPENDENT_FULL_STATE_WAVE_FAILED",
                "failure_type":type(exc).__name__,"failure_detail":str(exc)})
            arg.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        out["actual_true_corrected_KmkK_fv_direct_wave_cases"].append(result)
        arg.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("CORRECTED_EXACT_ROOF_ACTUAL_DIRECT_WAVE",ppw,
              "true-CG-versus-allmodes",result["actual_full_state_vs_independent_modal_complex_relative"],
              "CGiter",result["CG_solver_and_energy_diagnostics"]["maximum_CG_iterations"],
              "energy",result["CG_solver_and_energy_diagnostics"]["relative_energy_drift_after_source"],
              "seconds",round(result["wall_seconds"],1),flush=True)
    a,b=out["actual_true_corrected_KmkK_fv_direct_wave_cases"]
    metrics=compare_complex_transfer(
        reference=b["true_new_full_state_250ms_pressure_signed_P_over_Q_40_80"],
        candidate=a["true_new_full_state_250ms_pressure_signed_P_over_Q_40_80"],
        frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
    out["actual_corrected_full_state_PP40_44_original_three_gate_metrics"]=metrics
    out["full_state_pair_passes_frozen_original_gate"]=bool(
        metrics["complex_rms_relative"]<=.2 and
        metrics["magnitude_max_relative"]<=.25 and metrics["phase_max_deg"]<=15)
    out["experimental_wave_not_original_pffdtd_qualification"]=True
    arg.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("TRUE_FULL_STATE_CORRECTED_FV_PP40_44",metrics["complex_rms_relative"],
          metrics["magnitude_max_relative"],metrics["phase_max_deg"],
          "PASS",out["full_state_pair_passes_frozen_original_gate"],flush=True)
if __name__=="__main__":
    main()
