#!/usr/bin/env python3
"""Joint exact physical sloped-roof conservative FV + exact causal q0 time.

Real SHA-pinned original 8point HDF5 source/receiver and native clocks.
Every mass-orthonormal Neumann roof mode, including physical sliver-high modes,
is retained. This is an EXPERIMENTAL FV solver, not original upstream PFFDTD.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import time
from pathlib import Path
import sys

import h5py
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
from htdt.r130d_native_grid_exact_roof_fv import build_native_exact_roof_fv
from htdt.r130d_native_exact_roof_separable import (
    original_native_xy_z_factorization,prove_native_full_kronecker_equal,
    generalized_neumann_modes)
from htdt.r130d_exact_semidiscrete_q0 import (
    exact_semidiscrete_velocity_impulse_signed,
    exact_semidiscrete_one_sample_hold_signed)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_point_quadratic_pffdtd import PPW,PIN,file_hash
from run_r130d_native_exact_roof_xy_z_separable_modal_q0 import source_receiver_tensor_projections
from run_r130d_native_exact_roof_full_xy_z_modal_q0 import entire_original_finite_record_signed_modes
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs

SCHEMA="htdt.r130d.exact-roof-q0-exact-causal-time-multigrid-plan-1"
ARMS=("existing_exact_roof_newmark","exact_roof_velocity_impulse",
      "exact_roof_one_native_sample_hold")


def validate_plan(p):
    f=p["frozen"];m=p["operator"];v=p["observer"];e=p["predeclared_verification"]
    if (p.get("schema_version")!=SCHEMA or p["issue"]!=938 or p["pr"]!=1055
        or f["upstream_original_native_sha"]!=PIN or f["ppw"]!=list(PPW)
        or f["source_m"]!=[1.5,2,2] or f["receiver_m"]!=[2.5,2,2]
        or not f["true_original_eight_point_q0_input_hdf5"]
        or not f["actual_native_each_dt_nt_h"]
        or f["exact_sloped_roof_volume_m3"]!=56
        or f["record_s"]!=.25 or f["signed_hz"]!=[40,80]
        or f["c_m_s"]!=343.2 or f["rho_kg_m3"]!=1.2
        or [r["id"] for r in p["arms"]]!=list(ARMS)
        or not m["all_true_modes_no_truncation"]
        or not m["sliver_cell_high_eigenfrequencies_are_NOT_clamped_to_Nyquist"]
        or not m["one_grid_no_fitting_or_amplitude_normalization"]
        or not all(v[k] for k in ("original_second_order_pressure_start_interior_end_native_samples",
            "all_scored_original_full_250ms_40_80",
            "do_not_extend_scored_sample_count",
            "no_frequency_masks_no_modal_truncation_no_damping_no_smoothing_no_taper",
            "allow_all_extremely_high_cutcell_eigenfrequencies_without_clamp",
            "full_signed_real_imag_by_grid_by_arm"))
        or e["old_newmark_fullmode_vs_archived_real_CG_wave_rel_max"]!=2e-5
        or not e["full_true_3d_neumann_matrix_tensor_identity_and_exact_volume"]
        or not e["true_original_8node_input_sha_per_grid"]
        or not e["all_3d_eigenmodes_retained"]
        or not e["stable_geometric_phase_progression_with_modulo_2pi"]
        or not e["zero_mode_exact_limits"]
        or [e[k] for k in ("original_frozen_complex_relative_limit",
                            "original_frozen_magnitude_relative_limit",
                            "original_frozen_max_phase_deg_limit")]!=[.2,.25,15]
        or not e["all_four_adjacent_pairs_and_three_metrics"]
        or not e["strict_monotonic_three_metrics_requirement"]
        or not e["record_all_adverse_signed_bin_results"]
        or not e["no_original_PFFDTD_native_requalification"]
        or p["limits"]!={"max_cases":5,"max_arms":3,
                         "new_native_pffdtd_full_wave_runs":0,
                         "new_github_actions_runs":0,"scratch_cleanup":False}
        or p["release"]["canonical_original_PFFDTD_q0"]!="SELF_CONVERGENCE_FAILED"
        or p["release"]["external_physical"]!="NOT_VALIDATED"
        or p["release"]["product"]!="NO_GO"):
        raise ValueError("new joint roof and exact temporal q0 experiment plan changed")
    return p


def one_case(p,ppw,sim,original,control):
    start=time.perf_counter()
    if (file_hash(sim/"comms_out.h5")!=original["original_native_comm_sha256"]
        or file_hash(sim/"vox_out.h5")!=original["original_solver_geometry_sha256"]):
        raise ValueError("true original HDF5 point q0 and roof input SHA changed")
    with h5py.File(sim/"vox_out.h5","r") as h:
        axes=[np.asarray(h[k][...],dtype=float) for k in ("xv","yv","zv")]
    with h5py.File(sim/"comms_out.h5","r") as h:
        src=np.asarray(h["in_ixyz"][:],dtype=np.int64)
        rec=np.asarray(h["out_ixyz"][:],dtype=np.int64)
        sig=np.asarray(h["in_sigs"][:],dtype=float)
        rw=np.asarray(h["out_alpha"][:],dtype=float).ravel()
        nt=int(h["Nt"][()])
        if (src.shape!=(8,) or rec.shape!=(8,) or rw.shape!=(8,)
            or sig.shape!=(8,nt) or np.any(sig[:,1:]!=0)
            or abs(rw.sum()-1)>1e-12 or int(h["diff"][()])!=0):
            raise ValueError("true original 8node q0 point observation changed")
        strength=float(sig[:,0].sum())
        sw=sig[:,0]/strength
    with h5py.File(sim/"sim_consts.h5","r") as h:
        dt=float(h["Ts"][()]);h_m=float(h["h"][()])
        c=float(h["c"][()]);l2=float(h["l2"][()])
    if (abs(c-343.2)>1e-12 or abs(h_m-c/(100*ppw))>1e-12
        or abs(dt-control["original_native_dt_s"])>1e-12
        or nt!=control["original_record_samples"]
        or abs(strength-l2/h_m)>1e-10
        or abs(l2-(c*dt/h_m)**2)>1e-12
        or abs(sum(sw)-1)>1e-12):
        raise ValueError("actual native input/time source kick changed")
    system=build_native_exact_roof_fv(axes,max_nodes=150000)
    sep=original_native_xy_z_factorization(axes)
    match=prove_native_full_kronecker_equal(system,sep)
    if (abs(system.room_fluid_volume_m3-56)>2e-8
        or system.number_of_cells!=control["original_nodal_active_cutcell_count"]):
        raise ValueError("true geometric exact roof volumes changed")
    projection=source_receiver_tensor_projections(
        axes,sep,(src,sw),(rec,rw))
    lx,vx,_=generalized_neumann_modes(sep.Mx,sep.Kx)
    ly,vy,_=generalized_neumann_modes(sep.Myz,sep.Kyz)
    coupling=np.outer(
        (vx.T@projection["source"]["x"])*(vx.T@projection["receiver"]["x"]),
        (vy.T@projection["source"]["yz"])*(vy.T@projection["receiver"]["yz"]))
    lam=(lx[:,None]+ly[None,:]).ravel()
    A=(dt*dt*c*c*coupling).ravel()
    newmark_amp=A/(1+dt*dt*lam/4)
    old=entire_original_finite_record_signed_modes(
        lam,newmark_amp,dt,nt,1.2).sum(axis=1)
    control_wave=unpairs(control["experimental_signed_P_T_over_Q_T_40_80"])
    rel=float(np.linalg.norm(old-control_wave)/max(np.linalg.norm(control_wave),1e-14))
    if rel>p["predeclared_verification"]["old_newmark_fullmode_vs_archived_real_CG_wave_rel_max"]:
        raise ValueError(f"old physical exact roof original q0 CG full wave changed: {rel}")
    th=dt*np.sqrt(np.maximum(lam,0.))
    vel=exact_semidiscrete_velocity_impulse_signed(
        th,A,native_dt_s=dt,native_nt=nt).sum(axis=1)
    hold=exact_semidiscrete_one_sample_hold_signed(
        th,A,native_dt_s=dt,native_nt=nt).sum(axis=1)
    return {"ppw":int(ppw),
        "original_eight_source_comm_SHA256":file_hash(sim/"comms_out.h5"),
        "original_native_voxel_SHA256":file_hash(sim/"vox_out.h5"),
        "original_q0_native_strength":strength,
        "native_Ts_s":dt,"native_Nt":nt,
        "all_exact_roof_native_modes_count":int(sep.total_cells),
        "actual_physical_exact_room_volume_m3":system.room_fluid_volume_m3,
        "actual_physical_3D_sparse_K_Neumann_tensor_proof":match,
        "all_sliver_cell_modes_retained_above_nyquist":int(np.count_nonzero(th>np.pi)),
        "highest_semidiscrete_native_omega_dt":float(np.max(th)),
        "previous_archived_direct_true_Newmark_CG_wave_relative":rel,
        "original_raw_q0_source_receiver_and_scored_full_window_unchanged":True,
        "full_250ms_unmasked_signed_40_80_by_numerical_operator":{
            "existing_exact_roof_newmark":pairs(old),
            "exact_roof_velocity_impulse":pairs(vel),
            "exact_roof_one_native_sample_hold":pairs(hold)},
        "wall_seconds":float(time.perf_counter()-start)}


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args()
    raw=args.plan.read_bytes()
    p=validate_plan(json.loads(raw.decode("utf-8")))
    f=p["frozen"]
    original=json.loads((ROOT/f["raw_original_source_receiver_and_voxel_sha_evidence"]).read_text(encoding="utf-8"))
    prior=json.loads((ROOT/f["previous_real_exact_roof_CG_evidence"]).read_text(encoding="utf-8"))
    sources={x["ppw"]:x for x in original["actual_native_wave_cases"]}
    controls={x["ppw"]:x for x in prior["actual_native_grid_point_impulse_exact_roof_cases"]}
    if set(sources)!=set(PPW) or set(controls)!=set(PPW):
        raise ValueError("original five SHA-checked waves and actual original-clock FV CG required")
    dirs={}
    for f in args.original_sims_root.rglob("comms_out.h5"):
        sha=file_hash(f)
        matches=[i for i in PPW if sha==sources[i]["original_native_comm_sha256"]]
        if len(matches)==1:
            if matches[0] in dirs:raise ValueError("duplicated true original q0 comms HDF5")
            dirs[matches[0]]=f.parent
    if set(dirs)!=set(PPW):raise ValueError("original five true HDF5 source assets absent")
    result={"schema_version":"htdt.r130d.exact-roof-q0-exact-causal-time-multigrid-evidence-1",
        "pre_observation_plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":p,"original_PFFDTD_q0":"SELF_CONVERGENCE_FAILED",
        "physical_validation":"NOT_VALIDATED","product":"NO_GO",
        "new_pffdtd_wave_runs":0,"new_github_actions_runs":0,
        "actual_joint_spacetime_allmode_cases":[],"adjacent_full_signed_40_80":[]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    for ppw in PPW:
        try:row=one_case(p,ppw,dirs[ppw],sources[ppw],controls[ppw])
        except Exception as e:
            result["actual_joint_spacetime_allmode_cases"].append({
                "ppw":ppw,"status":"NEW_JOINT_SPACETIME_NUMERICAL_FAILED",
                "failure_type":type(e).__name__,"failure_detail":str(e)})
            args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        result["actual_joint_spacetime_allmode_cases"].append(row)
        args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("JOINT_ROOF_CAUSAL_Q0_ALLMODES",ppw,
              "modes",row["all_exact_roof_native_modes_count"],
              "original Newmark CG relative",row["previous_archived_direct_true_Newmark_CG_wave_relative"],
              "above Nyquist true modes",row["all_sliver_cell_modes_retained_above_nyquist"],
              "omega dt max",row["highest_semidiscrete_native_omega_dt"],flush=True)
    rows=result["actual_joint_spacetime_allmode_cases"]
    for first,second in zip(rows,rows[1:]):
        arms={}
        for arm in ARMS:
            cval=first["full_250ms_unmasked_signed_40_80_by_numerical_operator"][arm]
            fval=second["full_250ms_unmasked_signed_40_80_by_numerical_operator"][arm]
            m=compare_complex_transfer(reference=fval,candidate=cval,
                frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
            arms[arm]={"signed_coarse_minus_fine_P_T_over_Q_T_40_80":pairs(unpairs(cval)-unpairs(fval)),
                "original_frozen_complex_magnitude_phase_metrics":m,
                "all_original_three_limits_pass":bool(
                    m["complex_rms_relative"]<=.2 and m["magnitude_max_relative"]<=.25 and
                    m["phase_max_deg"]<=15)}
        result["adjacent_full_signed_40_80"].append({
            "coarse_ppw":first["ppw"],"fine_ppw":second["ppw"],"arms":arms})
    conclusions={}
    for arm in ARMS:
        scores=[row["arms"][arm] for row in result["adjacent_full_signed_40_80"]]
        keys=("complex_rms_relative","magnitude_max_relative","phase_max_deg")
        monotone=all(all(scores[i]["original_frozen_complex_magnitude_phase_metrics"][k]<
                         scores[i-1]["original_frozen_complex_magnitude_phase_metrics"][k]
                         for k in keys) for i in range(1,4))
        conclusions[arm]={"all_four_original_adjacent_pairs_pass_all_gates":all(
            a["all_original_three_limits_pass"] for a in scores),
            "all_three_metric_strict_monotone_decrease":monotone,
            "not_native_original_pffdtd_qualification":True}
    result["all_three_joint_spacetime_arm_5grid_verdicts"]=conclusions
    result["original_canonical_not_requalified"]=True
    args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("JOINT_ROOF_EXACT_CAUSAL_ALL_5_GRID_FROZEN_RESULTS",[
        (x["coarse_ppw"],x["fine_ppw"],{
            arm:(round(x["arms"][arm]["original_frozen_complex_magnitude_phase_metrics"]["complex_rms_relative"],6),
                 round(x["arms"][arm]["original_frozen_complex_magnitude_phase_metrics"]["magnitude_max_relative"],6),
                 round(x["arms"][arm]["original_frozen_complex_magnitude_phase_metrics"]["phase_max_deg"],3),
                 x["arms"][arm]["all_original_three_limits_pass"])
            for arm in ARMS}) for x in result["adjacent_full_signed_40_80"]],flush=True)
    print("JOINT_ROOF_EXACT_CAUSAL_OVERALL",conclusions,"ORIGINAL_PFFDTD_NO_GO",flush=True)
if __name__=="__main__":
    main()
