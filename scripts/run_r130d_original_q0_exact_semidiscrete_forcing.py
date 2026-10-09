#!/usr/bin/env python3
"""Real native PFFDTD graph q[0] original / exact causal time-integration A/B.

No upstream solver modification. Every modal spectral coefficient of real SHA-
pinned 8-point source/receiver, native 250ms, two signed score bins retained.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import h5py
import numpy as np
from scipy import linalg

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
from htdt.r130d_general3d_validation import compare_complex_transfer
from htdt.r130d_exact_semidiscrete_q0 import (
    exact_semidiscrete_velocity_impulse_signed,
    exact_semidiscrete_one_sample_hold_signed)
from run_r130d_original_point_quadratic_pffdtd import PIN,PPW,file_hash
from run_r130d_original_native_modal_drift import native_room_laplacian,pairs,unpairs
from run_r130d_original_pffdtd_neumann_graph_audit import validate_plan as check_graph_plan
from run_r130d_original_pffdtd_native_full_modal_q0 import (
    graph_from_original_6_neighbors_ppw,
    original_native_tensor_point_source_receiver,
    true_native_leapfrog_exact_finite_signed_transfer)

SCHEMA="htdt.r130d.original-q0-exact-semidiscrete-forcing-plan-1"
ARMS=("original_native_leapfrog","exact_semidiscrete_velocity_impulse",
      "exact_semidiscrete_one_sample_force_hold")


def validate_plan(p):
    o=p["original"];t=p["temporal_arms"];v=p["observer"];a=p["tests"]
    if (p.get("schema_version")!=SCHEMA or p["issue"]!=938 or p["pr"]!=1055
        or o["pffdtd_sha"]!=PIN or o["ppw"]!=list(PPW)
        or o["physical_source_m"]!=[1.5,2,2]
        or o["physical_receiver_m"]!=[2.5,2,2]
        or not o["source_receiver_eight_point_native_hdf5_sha"]
        or not o["original_spatial_neumann_graph_unmodified"]
        or not o["original_native_dt_nt_and_h"]
        or o["record_s"]!=.25 or o["signed_frequency_hz"]!=[40,80]
        or o["density_kg_m3"]!=1.2 or o["c_m_s"]!=343.2
        or [z["id"] for z in t]!=list(ARMS)
        or not v["no_extra_sample_in_scored_record"]
        or not v["no_modal_cut_or_smoothing_taper_damping_or_amplitude_fit"]
        or not v["zero_mode_analytic_limits_required"]
        or not v["independent_direct_time_oscillator_fixtures_required"]
        or a["original_native_full_eigenwaves_vs_real_sha_hdf5_complex_relative_max"]!=2e-6
        or a["original_eigen_fullsigned_vs_prior_full_modal_relative_max"]!=2e-10
        or [a[k] for k in ("original_frozen_complex","original_frozen_magnitude",
                          "original_frozen_phase_deg")]!=[.2,.25,15]
        or not a["all_four_adjacent_grid_pairs"]
        or not a["all_modes_including_high"]
        or not a["compare_all_bins_and_max_phase_magnitude"]
        or not a["all_three_metrics_strict_monotonic_for_provisional_refinement"]
        or not a["negative_data_not_hidden"]
        or not a["no_convergence_success_without_independent_phys"]
        or p["limits"]!={"max_cases":5,"max_arms":3,"original_native_pffdtd_new_waves":0,
                         "new_github_actions_runs":0,"scratch_cleanup":False}
        or p["authority"]["original_PFFDTD_q0"]!="SELF_CONVERGENCE_FAILED"
        or p["authority"]["physical_validation"]!="NOT_VALIDATED"
        or p["authority"]["product"]!="NO_GO"):
        raise ValueError("original native exact q0 semidiscrete forcing preregistered plan drift")
    return p


def one_case(p,ppw,folder,wave,modal,graphinfo,graph_plan):
    start=time.perf_counter()
    if (file_hash(folder/"comms_out.h5")!=wave["original_native_comm_sha256"]
        or file_hash(folder/"vox_out.h5")!=graphinfo["original_exact_voxel_sha256"]
        or file_hash(folder/"sim_outs.h5")!=modal["original_native_sim_output_SHA256"]):
        raise ValueError("original q0 raw wave, voxel or input SHA-256 changed")
    with h5py.File(folder/"vox_out.h5","r") as h:
        dims=tuple(int(h[k][()]) for k in ("Nx","Ny","Nz"))
        graph,_,visited,_=native_room_laplacian(
            {"source_authority":{"physical_source_xyz_m":[1.5,2,2]},
             "limits":{"max_graph_nodes":300000}},h,graph_plan)
    Ax,Ayz,xids,yzids,proof=graph_from_original_6_neighbors_ppw(graph,visited,dims)
    if (len(visited)!=modal["actual_native_full_3D_modes_count"]
        or proof["original_native_Kronecker_neumann_matrix_max_abs"]!=0):
        raise ValueError("original true original six-neighbor PFFDTD graph altered")
    with h5py.File(folder/"comms_out.h5","r") as h:
        si=np.asarray(h["in_ixyz"][:],dtype=np.int64)
        ri=np.asarray(h["out_ixyz"][:],dtype=np.int64)
        sig=np.asarray(h["in_sigs"][:],dtype=float)
        rw=np.asarray(h["out_alpha"][:],dtype=float).ravel()
        nt=int(h["Nt"][()])
        if (si.shape!=(8,) or ri.shape!=(8,) or rw.shape!=(8,)
            or sig.shape!=(8,nt) or np.any(sig[:,1:]!=0) or
            int(h["diff"][()])!=0 or abs(rw.sum()-1)>1e-12):
            raise ValueError("original q[0]=1 native 8point source/receiver altered")
        strength=float(sig[:,0].sum())
        sw=sig[:,0]/strength
    with h5py.File(folder/"sim_consts.h5","r") as h:
        dt=float(h["Ts"][()]);h_m=float(h["h"][()])
        c=float(h["c"][()]);l2=float(h["l2"][()])
    if (abs(c-343.2)>1e-12 or abs(h_m-c/(100*ppw))>1e-12
        or abs(dt-modal["original_native_time_step_s"])>1e-12
        or nt!=modal["original_record_samples"] or abs(strength-l2/h_m)>1e-10
        or abs(l2-(c*dt/h_m)**2)>1e-12):
        raise ValueError("original q0 source strength/native clock changed")
    sx,sy,_=original_native_tensor_point_source_receiver(dims,xids,yzids,si,sw)
    rx,ry,_=original_native_tensor_point_source_receiver(dims,xids,yzids,ri,rw)
    lx,vx=linalg.eigh(Ax.toarray(),check_finite=True)
    ly,vy=linalg.eigh(Ayz.toarray(),check_finite=True)
    if min(lx[0],ly[0])<-1e-9:
        raise ValueError("original graph eigenvalues negative")
    lx=np.maximum(lx,0.)
    ly=np.maximum(ly,0.)
    residual=float(max(np.max(abs(Ax@vx-vx*lx[None,:])),
                       np.max(abs(Ayz@vy-vy*ly[None,:]))))
    if residual>3e-7:raise ValueError("original modal eigenbasis failed true 3D residual")
    lam=(lx[:,None]+ly[None,:]).ravel()
    if l2*float(lam.max())>4+1e-10:
        raise ValueError("original native PFFDTD explicit CFL invalid")
    th_original=2*np.arcsin(.5*np.sqrt(np.clip(l2*lam,0,4)))
    th_continuous=np.sqrt(np.maximum(l2*lam,0.))
    coupling=np.outer((sx@vx)*(rx@vx),(sy@vy)*(ry@vy))
    amplitude=(strength*coupling).ravel()
    original=true_native_leapfrog_exact_finite_signed_transfer(
        th_original,amplitude,dt,nt,1.2).sum(axis=1)
    expected=unpairs(modal["exact_original_saved_true_PFFDTD_q0_signed_40_80"])
    raw=unpairs(wave["unmodified_original_transfer_pa_per_m3_s"])
    relative=float(np.linalg.norm(original-raw)/max(np.linalg.norm(raw),1e-14))
    prior=float(np.linalg.norm(original-expected)/max(np.linalg.norm(expected),1e-14))
    if (relative>p["tests"]["original_native_full_eigenwaves_vs_real_sha_hdf5_complex_relative_max"]
        or prior>p["tests"]["original_eigen_fullsigned_vs_prior_full_modal_relative_max"]):
        raise ValueError("original 250ms q0 baseline did not match SHA raw wave")
    impulse=exact_semidiscrete_velocity_impulse_signed(
        th_continuous,amplitude,native_dt_s=dt,native_nt=nt).sum(axis=1)
    hold=exact_semidiscrete_one_sample_hold_signed(
        th_continuous,amplitude,native_dt_s=dt,native_nt=nt).sum(axis=1)
    return {"ppw":int(ppw),"native_original_full_graph_3d_modes_count":int(len(visited)),
        "original_source_comm_SHA256":file_hash(folder/"comms_out.h5"),
        "original_room_voxel_SHA256":file_hash(folder/"vox_out.h5"),
        "original_true_raw_wave_SHA256":file_hash(folder/"sim_outs.h5"),
        "original_native_dt_s":dt,"original_Nt":nt,
        "original_true_discrete_q0_source_total_kick":strength,
        "original_graph_tensor_identity":proof,
        "all_original_source_receiver_eigenmodes_untruncated":True,
        "original_control_vs_true_raw_saved_pressure_rel":relative,
        "original_control_vs_prior_untruncated_full_modes_rel":prior,
        "signed_original_full_250ms_P_over_Q_40_80_by_arm":{
            "original_native_leapfrog":pairs(original),
            "exact_semidiscrete_velocity_impulse":pairs(impulse),
            "exact_semidiscrete_one_sample_force_hold":pairs(hold)},
        "new_time_integrators_not_original_PFFDTD":True,
        "wall_seconds":float(time.perf_counter()-start)}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--plan",type=Path,required=True)
    parser.add_argument("--original-sims-root",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    arg=parser.parse_args()
    raw=arg.plan.read_bytes()
    p=validate_plan(json.loads(raw.decode("utf-8")))
    o=p["original"]
    orig=json.loads((ROOT/o["original_true_wave_evidence"]).read_text(encoding="utf-8"))
    modal=json.loads((ROOT/o["original_allmode_evidence"]).read_text(encoding="utf-8"))
    graph=json.loads((ROOT/o["original_graph_evidence"]).read_text(encoding="utf-8"))
    gp=check_graph_plan(json.loads((ROOT/o["original_graph_plan"]).read_text(encoding="utf-8")))
    wave={z["ppw"]:z for z in orig["actual_native_wave_cases"]}
    saved={z["ppw"]:z for z in modal["actual_original_unmodified_all_mode_native_cases"]}
    audits={z["ppw"]:z for z in graph["actual_original_voxel_grid_audits"]}
    if (set(wave)!=set(PPW) or set(saved)!=set(PPW) or set(audits)!=set(PPW)
        or modal["canonical_original_point_q0"]!="SELF_CONVERGENCE_FAILED"):
        raise ValueError("original five full HDF5 q0 wave cases incomplete")
    folders={}
    for f in arg.original_sims_root.rglob("comms_out.h5"):
        sha=file_hash(f)
        found=[i for i in PPW if wave[i]["original_native_comm_sha256"]==sha]
        if len(found)==1:
            if found[0] in folders: raise ValueError("ambiguous original point source SHA")
            folders[found[0]]=f.parent
    if set(folders)!=set(PPW):raise ValueError("original SHA real true waveform assets missing")
    result={"schema_version":"htdt.r130d.original-q0-exact-semidiscrete-forcing-evidence-1",
        "pre_observation_plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":p,"original_PFFDTD_q0":"SELF_CONVERGENCE_FAILED",
        "physical_validation":"NOT_VALIDATED","product":"NO_GO",
        "new_native_pffdtd_waves":0,"new_GitHub_Actions_runs":0,
        "actual_original_native_q0_five_grid_full_modal_cases":[],
        "all_four_adjacent_native_q0_refinements":[]}
    arg.output.parent.mkdir(parents=True,exist_ok=True)
    for i in PPW:
        try:row=one_case(p,i,folders[i],wave[i],saved[i],audits[i],gp)
        except Exception as exc:
            result["actual_original_native_q0_five_grid_full_modal_cases"].append({
                "ppw":i,"status":"EXPERIMENT_INCOMPLETE",
                "failure_type":type(exc).__name__,"failure_detail":str(exc)})
            arg.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        result["actual_original_native_q0_five_grid_full_modal_cases"].append(row)
        arg.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("EXACT_SEMIDISCRETE_NATIVE_Q0_FULL_GRAPH",i,
              "all_modes",row["native_original_full_graph_3d_modes_count"],
              "control_rel",row["original_control_vs_true_raw_saved_pressure_rel"],
              "signed",row["signed_original_full_250ms_P_over_Q_40_80_by_arm"],flush=True)
    cases=result["actual_original_native_q0_five_grid_full_modal_cases"]
    for coarse,fine in zip(cases,cases[1:]):
        per={}
        for arm in ARMS:
            cval=coarse["signed_original_full_250ms_P_over_Q_40_80_by_arm"][arm]
            fval=fine["signed_original_full_250ms_P_over_Q_40_80_by_arm"][arm]
            metrics=compare_complex_transfer(
                reference=fval,candidate=cval,frequency_hz=[40,80],
                magnitude_mask_relative_db=-50).model_dump(mode="json")
            per[arm]={"two_original_frequency_signed_coarse_minus_fine":pairs(
                unpairs(cval)-unpairs(fval)),
                "full_original_three_metric_scoring":metrics,
                "three_frozen_limits_simultaneously_passed":bool(
                    metrics["complex_rms_relative"]<=.2 and
                    metrics["magnitude_max_relative"]<=.25 and
                    metrics["phase_max_deg"]<=15)}
        result["all_four_adjacent_native_q0_refinements"].append({
            "coarse_ppw":coarse["ppw"],"fine_ppw":fine["ppw"],"arms":per})
    summaries={}
    for arm in ARMS:
        comparisons=[x["arms"][arm] for x in result["all_four_adjacent_native_q0_refinements"]]
        keys=["complex_rms_relative","magnitude_max_relative","phase_max_deg"]
        monotone=all(all(comparisons[j]["full_original_three_metric_scoring"][k]<
                         comparisons[j-1]["full_original_three_metric_scoring"][k]
                         for k in keys) for j in range(1,4))
        summaries[arm]={
            "all_four_adjacent_pairs_three_gates_pass":all(
                r["three_frozen_limits_simultaneously_passed"] for r in comparisons),
            "all_three_frozen_metrics_strictly_monotone_decrease":monotone,
            "canonical_original_PFFDTD_status_unchanged":True}
    result["all_arms_full_five_grid_refinement_verdict"]=summaries
    result["new_numerical_time_solvers_not_original_pffdtd_qualification"]=True
    arg.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("EXACT_SEMIDISCRETE_ALL_Q0_NATIVE_PAIRS",[
        [z["coarse_ppw"],z["fine_ppw"],{
            arm:round(z["arms"][arm]["full_original_three_metric_scoring"]["complex_rms_relative"],6)
            for arm in ARMS}]
        for z in result["all_four_adjacent_native_q0_refinements"]],flush=True)
    print("EXACT_SEMIDISCRETE_ALL_NATIVE_Q0_VERDICTS",summaries,flush=True)
if __name__=="__main__":
    main()
