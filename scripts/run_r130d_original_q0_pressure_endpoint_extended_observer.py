#!/usr/bin/env python3
"""Prospectively frozen original native PFFDTD q0 one-extra-wave-step pressure A/B.

Keep ALL original 8node q0, Neumann graph, native dt,Nt, all modes, full 250ms
signed 40/80Hz. Only hypothetical differentiation at existing last sample
changes: use the freely evolved u[N] to center the last pressure stencil.
An anti-causal both-end centered arm is diagnostic-only, not a source solver.
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
from run_r130d_original_point_quadratic_pffdtd import PIN,PPW,file_hash
from run_r130d_original_native_modal_drift import native_room_laplacian,pairs,unpairs
from run_r130d_original_pffdtd_neumann_graph_audit import validate_plan as check_graph_plan
from run_r130d_original_pffdtd_native_full_modal_q0 import (
    graph_from_original_6_neighbors_ppw,
    original_native_tensor_point_source_receiver,
    stable_sin_ratio,
)
from run_r130d_original_q0_finite_window_endpoint_leakage import (
    true_original_pressure_time_parts,TERMS)

SCHEMA="htdt.r130d.original-native-q0-pressure-extended-end-operator-plan-1"
ARMS=("original_one_sided_end","extended_centered_end","hypothetical_all_centered")


def validate_plan(p):
    f=p["original"];n=p["numerical"];e=p["evaluation"]
    if (p.get("schema_version")!=SCHEMA or p["issue"]!=938 or p["pr"]!=1055
        or f["upstream_sha"]!=PIN or f["ppw"]!=list(PPW)
        or f["physical_source_xyz"]!=[1.5,2,2]
        or f["physical_receiver_xyz"]!=[2.5,2,2]
        or not f["actual_original_native_eight_point_q0_hdf5_sha"]
        or not f["original_native_Ts_Nt_h"]
        or not f["original_neumann_graph_without_change"]
        or f["full_record_s"]!=.25 or f["scored_freq_hz"]!=[40,80]
        or f["sound_speed_m_s"]!=343.2 or f["rho"]!=1.2
        or [x["id"] for x in p["arms"]]!=list(ARMS)
        or not all(n[x] for x in ("native_original_explicit_leapfrog_per_mode",
            "source_discrete_original_q0","all_original_native_3d_modes_complete",
            "original_sample_count_unchanged","pressure_full_250ms_true_native_40_80_without_taper",
            "phi_N_extra_state_only_end_derivative",
            "no_q0_change_no_smoothing_no_damping_no_mode_cut"))
        or n["preserve_original_native_transfer_complex_relative_max"]!=2e-6
        or n["original_three_part_reconstruction_relative_max"]!=2e-10
        or n["previous_original_40_80_timepartition_match_relative_max"]!=2e-10
        or [e[k] for k in ("original_complex_limit","original_magnitude_limit",
            "original_phase_deg_limit")]!=[.2,.25,15]
        or not e["all_four_adjacent_grid_pairs"]
        or not e["retain_all_original_unfavorable_results"]
        or not e["record_signed_both_bin_per_grid_every_arm"]
        or not e["require_all_components_and_monotonic_for_any_experimental_numerical_refinement"]
        or not e["not_original_native_pffdtd_requalification"]
        or not e["anti_causal_both_centered_never_physical_candidate"]
        or p["limits"]!={"max_cases":5,"max_arms":3,"new_native_pffdtd_waves":0,
                         "new_github_actions_runs":0,"clean_scratch":False}
        or p["authority"]["original_PFFDTD"]!="SELF_CONVERGENCE_FAILED"
        or p["authority"]["physical_validation"]!="NOT_VALIDATED"
        or p["authority"]["product"]!="NO_GO"):
        raise ValueError("preregistered original q0 new pressure endpoint physical/numerical gates changed")
    return p


def extended_end_pressure_changes(theta,amp,dt,nt,rho):
    """Extra true homogeneous phi_N: centered last pressure derivative.

    Returned shape (2,2,Nmodes) follows [end_delta, hypothetical_start_delta]
    then [40,80Hz]. Extra phi[N] never enters returned 250ms sample vector.
    """
    th=np.asarray(theta,dtype=float).ravel()
    a=np.asarray(amp,dtype=float).ravel()
    if th.shape!=a.shape or len(a)==0 or dt<=0 or nt<3 or nt>2000:
        raise ValueError("original full mode temporal derivative data invalid")
    c=np.cos(th)
    q=rho*a/dt
    # Derived by substituting the unmodified source-free recurrence
    # phi[n+1]=2cos(theta)phi[n]-phi[n-1] into original backward derivative.
    old_end=q*(stable_sin_ratio(nt-1,th)+(c-2)*stable_sin_ratio(nt-2,th))
    new_end=q*np.cos((nt-1)*th)
    start_change=q*(c-1)
    result=np.empty((2,2,len(a)),dtype=np.complex128)
    for index,freq in enumerate((40.,80.)):
        result[0,index,:]=np.exp(2j*np.pi*freq*dt*(nt-1))*(new_end-old_end)
        result[1,index,:]=start_change
    return result


def one_case(p,ppw,folder,control,previous,graph_control,graph_plan):
    t=time.perf_counter()
    if (file_hash(folder/"comms_out.h5")!=control["original_native_comm_sha256"]
        or file_hash(folder/"vox_out.h5")!=graph_control["original_exact_voxel_sha256"]
        or file_hash(folder/"sim_outs.h5")!=previous["original_native_sim_output_SHA256"]):
        raise ValueError("original native PFFDTD raw source/geometry/wave SHA drift")
    with h5py.File(folder/"vox_out.h5","r") as h:
        shape=tuple(int(h[n][()]) for n in ("Nx","Ny","Nz"))
        graph,_,visited,_=native_room_laplacian(
            {"source_authority":{"physical_source_xyz_m":[1.5,2,2]},
             "limits":{"max_graph_nodes":300000}},h,graph_plan)
    Ax,Ayz,xids,yzids,identity=graph_from_original_6_neighbors_ppw(graph,visited,shape)
    if (len(visited)!=previous["actual_native_full_3D_modes_count"]
        or identity["original_native_Kronecker_neumann_matrix_max_abs"]!=0):
        raise ValueError("original 6-neighbor native room graph changed")
    with h5py.File(folder/"comms_out.h5","r") as h:
        si=np.asarray(h["in_ixyz"][:],dtype=np.int64)
        ri=np.asarray(h["out_ixyz"][:],dtype=np.int64)
        sig=np.asarray(h["in_sigs"][:],dtype=float)
        rw=np.asarray(h["out_alpha"][:],dtype=float).ravel()
        nt=int(h["Nt"][()])
        if (si.shape!=(8,) or ri.shape!=(8,) or rw.shape!=(8,)
            or sig.shape!=(8,nt) or np.any(sig[:,1:]!=0)
            or int(h["diff"][()])!=0 or abs(rw.sum()-1)>1e-12):
            raise ValueError("original 8-point q0 input/observation changed")
        strength=float(sig[:,0].sum())
        sw=sig[:,0]/strength
    with h5py.File(folder/"sim_consts.h5","r") as h:
        dt=float(h["Ts"][()]);h_m=float(h["h"][()])
        c=float(h["c"][()]);l2=float(h["l2"][()])
    if (abs(dt-previous["original_native_time_step_s"])>1e-12
        or nt!=previous["original_record_samples"]
        or abs(c-343.2)>1e-12 or abs(h_m-c/(100*ppw))>1e-12
        or abs(strength-l2/h_m)>1e-10 or abs(l2-(c*dt/h_m)**2)>1e-12):
        raise ValueError("true original native clock/source impulse strength changed")
    sx,sy,_=original_native_tensor_point_source_receiver(shape,xids,yzids,si,sw)
    rx,ry,_=original_native_tensor_point_source_receiver(shape,xids,yzids,ri,rw)
    lx,vx=linalg.eigh(Ax.toarray())
    ly,vy=linalg.eigh(Ayz.toarray())
    if min(lx[0],ly[0])<-1e-9:
        raise ValueError("original native PFFDTD spectrum invalid")
    lx=np.maximum(lx,0)
    ly=np.maximum(ly,0)
    lam=(lx[:,None]+ly[None,:]).ravel()
    if l2*lam.max()>4+1e-10:
        raise ValueError("original explicit Neumann CFL violated")
    theta=2*np.arcsin(.5*np.sqrt(np.clip(l2*lam,0,4)))
    amp=(strength*np.outer((sx@vx)*(rx@vx),(sy@vy)*(ry@vy))).ravel()
    baseparts=true_original_pressure_time_parts(theta,amp,dt,nt,1.2)
    base=baseparts.sum(axis=(0,2))
    diff=extended_end_pressure_changes(theta,amp,dt,nt,1.2).sum(axis=2)
    out={
        "original_one_sided_end":pairs(base),
        "extended_centered_end":pairs(base+diff[0]),
        "hypothetical_all_centered":pairs(base+diff[0]+diff[1]),
    }
    raw=unpairs(previous["exact_original_saved_true_PFFDTD_q0_signed_40_80"])
    check=float(np.linalg.norm(base-raw)/max(np.linalg.norm(raw),1e-14))
    if check>p["numerical"]["preserve_original_native_transfer_complex_relative_max"]:
        raise ValueError(f"original full-q0 native wave failed {check}")
    return {"ppw":ppw,"original_native_3d_full_modes":int(len(visited)),
        "native_record_nt":nt,"native_dt_s":dt,
        "original_source_comms_sha256":file_hash(folder/"comms_out.h5"),
        "original_room_voxel_sha256":file_hash(folder/"vox_out.h5"),
        "original_solver_unchanged_baseline_vs_saved_real_wave_relative":check,
        "one_true_homogeneous_post_record_extra_state_without_extra_scored_sample":True,
        "original_record_full_250ms_signed_40_80_by_pressure_arm":out,
        "difference_extended_end_minus_original_signed_40_80":pairs(diff[0]),
        "difference_hypothetical_start_center_minus_original_signed_40_80":pairs(diff[1]),
        "original_graph_exact_tensor_check":identity,
        "original_q0_and_all_mode_no_taper_no_cut":True,
        "elapsed_seconds":float(time.perf_counter()-t)}


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args()
    raw=args.plan.read_bytes()
    p=validate_plan(json.loads(raw.decode("utf-8")))
    x=p["original"]
    source=json.loads((ROOT/x["original_wave_evidence"]).read_text(encoding="utf-8"))
    prior=json.loads((ROOT/x["original_full_modal_evidence"]).read_text(encoding="utf-8"))
    audit=json.loads((ROOT/x["graph_audit"]).read_text(encoding="utf-8"))
    prev_parts=json.loads((ROOT/x["last_exact_time_partition"]).read_text(encoding="utf-8"))
    gplan=check_graph_plan(json.loads((ROOT/x["graph_plan"]).read_text(encoding="utf-8")))
    orig={z["ppw"]:z for z in source["actual_native_wave_cases"]}
    modal={z["ppw"]:z for z in prior["actual_original_unmodified_all_mode_native_cases"]}
    graphs={z["ppw"]:z for z in audit["actual_original_voxel_grid_audits"]}
    old={z["ppw"]:z for z in prev_parts["actual_original_full_3D_all_modal_time_parts"]}
    if (set(orig)!=set(PPW) or set(modal)!=set(PPW)
        or set(graphs)!=set(PPW) or set(old)!=set(PPW)
        or prev_parts["original_PFFDTD_q0"]!="SELF_CONVERGENCE_FAILED"):
        raise ValueError("original SHA-pinned q0 five-grid native wave endpoint source missing")
    folders={}
    for f in args.original_sims_root.rglob("comms_out.h5"):
        sha=file_hash(f)
        match=[i for i in PPW if orig[i]["original_native_comm_sha256"]==sha]
        if len(match)==1:
            if match[0] in folders:raise ValueError("duplicate original q0 comms HDF5")
            folders[match[0]]=f.parent
    if set(folders)!=set(PPW):raise ValueError("not all original HDF5 grids available")
    output={"schema_version":"htdt.r130d.original-q0-pressure-extended-end-evidence-1",
        "pre_observation_plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":p,"canonical_original_PFFDTD_q0":"SELF_CONVERGENCE_FAILED",
        "physical_validation":"NOT_VALIDATED","product":"NO_GO",
        "new_PFFDTD_waves":0,"new_GitHub_Actions_runs":0,
        "original_real_five_grid_pressure_observer_cases":[],
        "all_four_original_adjacent_pressure_observer_comparisons":[]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    for ppw in PPW:
        try:r=one_case(p,ppw,folders[ppw],orig[ppw],modal[ppw],graphs[ppw],gplan)
        except Exception as e:
            output["original_real_five_grid_pressure_observer_cases"].append({
                "ppw":ppw,"status":"ORIGINAL_Q0_POST_RECORD_PRESSURE_EVALUATION_FAILED",
                "failure_type":type(e).__name__,"failure_detail":str(e)})
            args.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        saved=unpairs(old[ppw]["original_native_full_250ms_signed_transfer_40_80"])
        current=unpairs(r["original_record_full_250ms_signed_40_80_by_pressure_arm"]["original_one_sided_end"])
        if np.linalg.norm(current-saved)/max(np.linalg.norm(saved),1e-15)>p["numerical"][
                "previous_original_40_80_timepartition_match_relative_max"]:
            raise ValueError("new original q0 wave is inconsistent with already precommitted endpoint analysis")
        output["original_real_five_grid_pressure_observer_cases"].append(r)
        args.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("ORIGINAL_FULL_NATIVE_PRESSURE_OBSERVER_AB",ppw,
              "modes",r["original_native_3d_full_modes"],
              "original raw relative",r["original_solver_unchanged_baseline_vs_saved_real_wave_relative"],
              flush=True)
    for coarse,fine in zip(output["original_real_five_grid_pressure_observer_cases"],
                           output["original_real_five_grid_pressure_observer_cases"][1:]):
        per={}
        for arm in ARMS:
            src=coarse["original_record_full_250ms_signed_40_80_by_pressure_arm"][arm]
            dst=fine["original_record_full_250ms_signed_40_80_by_pressure_arm"][arm]
            score=compare_complex_transfer(reference=dst,candidate=src,
                frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
            per[arm]={"signed_coarse_minus_fine_40_80":pairs(unpairs(src)-unpairs(dst)),
                "all_frozen_original_complex_magnitude_phase":score,
                "passes_original_three_gates":bool(score["complex_rms_relative"]<=.2
                    and score["magnitude_max_relative"]<=.25 and score["phase_max_deg"]<=15)}
        output["all_four_original_adjacent_pressure_observer_comparisons"].append({
            "coarse_ppw":coarse["ppw"],"fine_ppw":fine["ppw"],"arms":per})
    verdict={}
    for arm in ARMS:
        scored=[q["arms"][arm] for q in output["all_four_original_adjacent_pressure_observer_comparisons"]]
        metrics=("complex_rms_relative","magnitude_max_relative","phase_max_deg")
        mono=all(all(scored[j]["all_frozen_original_complex_magnitude_phase"][k]<
                     scored[j-1]["all_frozen_original_complex_magnitude_phase"][k]
                     for k in metrics) for j in range(1,4))
        verdict[arm]={"all_four_pairs_pass_three_gates":all(z["passes_original_three_gates"] for z in scored),
            "three_error_types_monotone_decrease":mono,
            "valid_for_original_canonical_requalification":False}
    output["original_and_diagnostic_pressure_observer_verdicts"]=verdict
    output["hypothetical_both_sided_centered_first_sample_is_not_causal"]=True
    args.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    for row in output["all_four_original_adjacent_pressure_observer_comparisons"]:
        print("ORIGINAL_Q0_PRESSURE_STENCIL_PAIR",row["coarse_ppw"],row["fine_ppw"],{
            a:[round(row["arms"][a]["all_frozen_original_complex_magnitude_phase"][k],6)
               for k in ("complex_rms_relative","magnitude_max_relative","phase_max_deg")]
            for a in ARMS},flush=True)
    print("ORIGINAL_Q0_PRESSURE_EXTENDED_END_DIAGNOSTIC_VERDICTS",verdict,flush=True)
if __name__=="__main__":
    main()
