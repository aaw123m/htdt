#!/usr/bin/env python3
"""Exact original PFFDTD q0 pressure-DTFT three-time-region × five-mode-band audit.

This is a pure decomposition of the UNMODIFIED original 8-node pulse, without
editing any production solver or making an alternate filtered answer.
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
from scipy import linalg

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_point_quadratic_pffdtd import PIN,PPW,file_hash
from run_r130d_original_native_modal_drift import native_room_laplacian,unpairs,pairs
from run_r130d_original_pffdtd_neumann_graph_audit import validate_plan as graph_plan_validator
from run_r130d_original_pffdtd_native_full_modal_q0 import (
    graph_from_original_6_neighbors_ppw,
    original_native_tensor_point_source_receiver,
    true_native_leapfrog_exact_finite_signed_transfer,
    validate_plan as original_plan_validator,
    stable_q_progression,stable_sin_ratio,
    BANDS,FREQ,
)

SCHEMA="htdt.r130d.original-q0-finite-window-endpoint-leakage-plan-1"
TERMS=("start_forward_n0","centered_interior_n1_to_Nminus2","end_backward_nNminus1")


def validate_plan(plan):
    p=plan["frozen"];d=plan["decomposition"];v=plan["verification"]
    if (plan.get("schema_version")!=SCHEMA or plan["issue"]!=938 or plan["pr"]!=1055
        or p["upstream_sha"]!=PIN or p["ppw"]!=list(PPW)
        or p["source_xyz_m"]!=[1.5,2,2] or p["receiver_xyz_m"]!=[2.5,2,2]
        or not p["original_native_eight_point_q0"]
        or not p["native_dt_nt_and_h_from_sha_pinned_hdf5"]
        or p["original_record_seconds"]!=.25 or p["frequency_hz"]!=[40,80]
        or p["sound_speed_m_s"]!=343.2 or p["density_kg_m3"]!=1.2
        or [p[k] for k in ("original_frozen_complex_limit",
                            "original_frozen_magnitude_limit",
                            "original_frozen_phase_deg")]!=[.2,.25,15]
        or d["time_terms"]!=list(TERMS)
        or d["neumann_semidiscrete_frequency_bands_hz"]!=[list(q) for q in BANDS]
        or not d["full_equals_signed_sum_without_taper"]
        or not d["all_3d_original_modes_no_exclusion"]
        or not d["no_other_numerical_scheme_or_source"]
        or v["each_grid_full_signed_mode_sum_matches_archived_raw_native_full_wave_relative_max"]!=2e-6
        or v["each_grid_exact_three_time_terms_recombine_finite_record_relative_max"]!=2e-10
        or v["spectral_band_and_time_term_sum_match_full_relative_max"]!=2e-10
        or not v["all_band_mode_counts_equal_original_connected_graph"]
        or not v["all_four_adjacent_pairs_scored_original_metrics"]
        or not v["store_all_frequency_bin_signed_component_deltas_and_full_metric"]
        or not v["include_per_band_high_freq_contribution_and_complex_cancellation"]
        or not v["do_not_score_time_components_as_alternate_original_solvers"]
        or not v["noncausal_infinite_steady_state_not_claimed"]
        or plan["limits"]!={"new_native_pffdtd_runs":0,
            "new_github_actions_runs":0,"max_grid_cases":5,"clean_scratch":False}
        or plan["authority"]["canonical_original_q0"]!="SELF_CONVERGENCE_FAILED"
        or plan["authority"]["independent_physics"]!="NOT_VALIDATED"
        or plan["authority"]["product"]!="NO_GO"):
        raise ValueError("original PFFDTD endpoint leakage prospective physics/score plan mutated")
    return plan


def true_original_pressure_time_parts(theta,amplitude,dt,nt,rho):
    """Three exact *additive* signed frequency transform contributions, all modes.

    No point q0, record length, physical pressure definition, or bin is modified.
    p[0] = rho*amp/dt*(2-cos theta)
    p[n=1..N-2] = rho*amp/dt*cos(n theta)
    p[N-1] = rho*amp/dt*(sin((N-1)theta)/sin theta
                         +(cos theta-2)*sin((N-2)theta)/sin theta)
    """
    theta=np.asarray(theta,dtype=float).ravel()
    amp=np.asarray(amplitude,dtype=float).ravel()
    if (theta.shape!=amp.shape or theta.size==0 or nt<3 or nt>2000 or dt<=0
        or not np.isfinite(theta).all() or not np.isfinite(amp).all()
        or np.min(theta)<-1e-9 or np.max(theta)>np.pi+1e-9):
        raise ValueError("invalid original 3D exact q0 source/native spectral clocks")
    factor=rho*amp/dt
    cos=np.cos(theta)
    start=factor*(2-cos)
    end=factor*(stable_sin_ratio(nt-1,theta)+(cos-2)*stable_sin_ratio(nt-2,theta))
    result=np.empty((3,len(FREQ),theta.size),dtype=np.complex128)
    result[0,:,:]=start[None,:]
    for i,f in enumerate(FREQ):
        shift=2*np.pi*f*dt
        interior=.5*(stable_q_progression(nt-2,shift+theta)+
                     stable_q_progression(nt-2,shift-theta))
        result[1,i,:]=factor*interior
        result[2,i,:]=np.exp(1j*shift*(nt-1))*end
    if not np.isfinite(result).all():
        raise ValueError("original finite record pressure endpoint contributions nonfinite")
    return result


def grid_case(plan,ppw,sim,wave,prior,graph_audit,graph_plan):
    t0=time.perf_counter()
    if (file_hash(sim/"comms_out.h5")!=wave["original_native_comm_sha256"]
        or file_hash(sim/"vox_out.h5")!=graph_audit["original_exact_voxel_sha256"]
        or file_hash(sim/"sim_outs.h5")!=prior["original_native_sim_output_SHA256"]):
        raise ValueError("original native source, geometry, TRUE frozen raw waveform SHA changed")
    with h5py.File(sim/"vox_out.h5","r") as voxel:
        dims=tuple(int(voxel[k][()]) for k in ("Nx","Ny","Nz"))
        graph,_,visited,_=native_room_laplacian(
            {"source_authority":{"physical_source_xyz_m":[1.5,2,2]},
             "limits":{"max_graph_nodes":300000}},voxel,graph_plan)
    Ax,Ayz,nx,nyz,identity=graph_from_original_6_neighbors_ppw(
        graph,visited,dims)
    if (identity["original_native_Kronecker_neumann_matrix_max_abs"]!=0
        or len(visited)!=prior["actual_native_full_3D_modes_count"]):
        raise ValueError("original physical native connected graph and true modes changed")
    with h5py.File(sim/"comms_out.h5","r") as h:
        si=np.asarray(h["in_ixyz"][:],dtype=np.int64)
        ri=np.asarray(h["out_ixyz"][:],dtype=np.int64)
        sig=np.asarray(h["in_sigs"][:],dtype=float)
        rw=np.asarray(h["out_alpha"][:],dtype=float).ravel()
        nt=int(h["Nt"][()])
        if (si.shape!=(8,) or ri.shape!=(8,) or rw.shape!=(8,)
            or sig.shape!=(8,nt) or np.any(sig[:,1:]!=0)
            or int(h["diff"][()])!=0 or nt>2000 or abs(rw.sum()-1)>1e-12):
            raise ValueError("original HDF5 8/8 native point q0/pressure changed")
        strength=float(sig[:,0].sum())
        sw=sig[:,0]/strength
    with h5py.File(sim/"sim_consts.h5","r") as h:
        dt=float(h["Ts"][()]);grid_h=float(h["h"][()])
        c=float(h["c"][()]);l2=float(h["l2"][()])
    if (abs(dt-prior["original_native_time_step_s"])>1e-12
        or nt!=prior["original_record_samples"]
        or abs(c-343.2)>1e-12 or abs(grid_h-343.2/(100*ppw))>1e-12
        or abs(l2-(c*dt/grid_h)**2)>1e-12
        or abs(strength-l2/grid_h)>1e-10 or abs(sw.sum()-1)>1e-12):
        raise ValueError("original native point q0 time/space and source scaling changed")
    sx,syz,_=original_native_tensor_point_source_receiver(
        dims,nx,nyz,si,sw)
    rx,ryz,_=original_native_tensor_point_source_receiver(
        dims,nx,nyz,ri,rw)
    lx,vx=linalg.eigh(Ax.toarray(),check_finite=True)
    ly,vy=linalg.eigh(Ayz.toarray(),check_finite=True)
    if min(lx[0],ly[0])<-1e-9:
        raise ValueError("original Neumann graph negative eigenvalue")
    if (np.max(np.linalg.norm(Ax@vx-vx*lx[None,:],axis=0))>3e-7
        or np.max(np.linalg.norm(Ayz@vy-vy*ly[None,:],axis=0))>3e-7):
        raise ValueError("original native generalized modes inaccurate")
    lx=np.maximum(lx,0.)
    ly=np.maximum(ly,0.)
    lam=(lx[:,None]+ly[None,:]).ravel()
    if l2*lam.max()>4+1e-10:
        raise ValueError("true original 3D leapfrog CFL invalid")
    theta=2*np.arcsin(.5*np.sqrt(np.clip(l2*lam,0,4)))
    coupling=np.outer((sx@vx)*(rx@vx),(syz@vy)*(ryz@vy))
    amp=(strength*coupling).ravel()
    terms=true_original_pressure_time_parts(theta,amp,dt,nt,1.2)
    exact=true_native_leapfrog_exact_finite_signed_transfer(theta,amp,dt,nt,1.2)
    full=terms.sum(axis=0).sum(axis=1)
    modal=exact.sum(axis=1)
    saved=unpairs(prior["exact_original_saved_true_PFFDTD_q0_signed_40_80"])
    raw=unpairs(wave["unmodified_original_transfer_pa_per_m3_s"])
    deviation=float(np.linalg.norm(full-saved)/max(np.linalg.norm(saved),1e-15))
    closure=float(np.linalg.norm(full-modal)/max(np.linalg.norm(modal),1e-15))
    if (deviation>plan["verification"][
             "each_grid_full_signed_mode_sum_matches_archived_raw_native_full_wave_relative_max"]
        or closure>plan["verification"][
             "each_grid_exact_three_time_terms_recombine_finite_record_relative_max"]
        or np.linalg.norm(raw-saved)>3e-6):
        raise ValueError(f"original PFFDTD true 250ms endpoint decomposition drift: {deviation} {closure}")
    freqs=(c/(2*np.pi*grid_h))*np.sqrt(lam)
    bands=[]
    signed_grid=np.zeros((3,2),dtype=complex)
    counted=0
    for low,high in BANDS:
        mask=(freqs>=low)&(freqs<high)
        count=int(mask.sum())
        counted+=count
        byterm=terms[:,:,mask].sum(axis=2)
        bandfull=byterm.sum(axis=0)
        signed_grid+=byterm
        bands.append({"semidiscrete_mode_band_hz":[low,high],
            "all_original_true_modes_count":count,
            "signed_original_finite_window_time_parts_40_80":{
                key:pairs(byterm[j]) for j,key in enumerate(TERMS)},
            "complete_original_full250ms_signed_40_80":pairs(bandfull)})
    if counted!=len(visited) or np.linalg.norm(signed_grid.sum(axis=0)-modal)>1e-8:
        raise ValueError("all true native original graph modes not summed exactly")
    preexisting=prior["all_native_actual_original_PFFDTD_modes_signed_spectral_band_transfer"]
    for group,archive in zip(bands,preexisting):
        if (group["semidiscrete_mode_band_hz"]!=archive["semidiscrete_mode_frequency_band_hz"]
            or group["all_original_true_modes_count"]!=archive["full_original_native_3D_mode_count"]
            or not np.allclose(unpairs(group["complete_original_full250ms_signed_40_80"]),
                unpairs(archive["signed_original_8node_q0_full250ms_P_T_over_Q_T_40_80"]),
                rtol=2e-10,atol=1e-6)):
            raise ValueError("earlier original actual native true full spectral band changed")
    return {"ppw":ppw,"actual_original_modes_in_full_3D_graph":len(visited),
        "native_Nt":nt,"native_dt_s":dt,"original_source_total_q0_kick":strength,
        "original_native_voxel_SHA256":file_hash(sim/"vox_out.h5"),
        "original_native_comms_SHA256":file_hash(sim/"comms_out.h5"),
        "exact_original_graph_tensor_audit":identity,
        "native_pffdtd_all_modes_vs_true_original_raw_wave_complex_rel":deviation,
        "three_exact_time_terms_vs_previous_original_single_formula_rel":closure,
        "original_native_full_250ms_signed_transfer_40_80":pairs(full),
        "original_full_record_time_terms_signed_40_80":{
            key:pairs(signed_grid[j]) for j,key in enumerate(TERMS)},
        "original_native_five_frequency_bands_all_time_terms":bands,
        "native_original_points_waveform_and_full_record_unchanged":True,
        "elapsed_wall_s":float(time.perf_counter()-t0)}


def main():
    argp=argparse.ArgumentParser()
    argp.add_argument("--plan",type=Path,required=True)
    argp.add_argument("--original-sims-root",type=Path,required=True)
    argp.add_argument("--output",type=Path,required=True)
    arg=argp.parse_args()
    raw=arg.plan.read_bytes()
    p=validate_plan(json.loads(raw.decode("utf-8")))
    original_plan=original_plan_validator(json.loads(
        (ROOT/p["frozen"]["original_full_modal_plan"]).read_text(encoding="utf-8")))
    if original_plan["original_authority"]["pffdtd_sha"]!=PIN:
        raise ValueError("original PFFDTD modal plan invalid")
    graph_plan=graph_plan_validator(json.loads(
        (ROOT/p["frozen"]["original_graph_plan"]).read_text(encoding="utf-8")))
    ref=json.loads((ROOT/p["frozen"]["original_full_modal_evidence"]).read_text(encoding="utf-8"))
    actual=json.loads((ROOT/p["frozen"]["original_real_native_evidence"]).read_text(encoding="utf-8"))
    graph=json.loads((ROOT/p["frozen"]["original_graph_evidence"]).read_text(encoding="utf-8"))
    earlier={r["ppw"]:r for r in ref["actual_original_unmodified_all_mode_native_cases"]}
    waves={r["ppw"]:r for r in actual["actual_native_wave_cases"]}
    audit={r["ppw"]:r for r in graph["actual_original_voxel_grid_audits"]}
    if (set(earlier)!=set(PPW) or set(waves)!=set(PPW) or set(audit)!=set(PPW)
        or ref["canonical_original_point_q0"]!="SELF_CONVERGENCE_FAILED"
        or ref["product"]!="NO_GO"):
        raise ValueError("five original unmodified real wave/full mode/graph audits not complete")
    directories={}
    for f in arg.original_sims_root.rglob("comms_out.h5"):
        sha=file_hash(f)
        match=[i for i in PPW if sha==waves[i]["original_native_comm_sha256"]]
        if len(match)==1:
            if match[0] in directories:
                raise ValueError("ambiguous original 8point native q0 HDF5")
            directories[match[0]]=f.parent
    if set(directories)!=set(PPW):
        raise ValueError("original raw five PFFDTD q0 waves not SHA-pinned on disk")
    out={"schema_version":"htdt.r130d.original-q0-finite-window-endpoint-leakage-evidence-1",
        "pre_observation_plan_sha256_lf":hashlib.sha256(
            raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":p,"original_PFFDTD_q0":"SELF_CONVERGENCE_FAILED",
        "physical_validation":"NOT_VALIDATED","product":"NO_GO",
        "new_upstream_pffdtd_runs":0,"new_github_actions_runs":0,
        "actual_original_full_3D_all_modal_time_parts":[],"adjacent_native_q0_parts":[]}
    arg.output.parent.mkdir(parents=True,exist_ok=True)
    for ppw in PPW:
        try:result=grid_case(p,ppw,directories[ppw],waves[ppw],earlier[ppw],
                             audit[ppw],graph_plan)
        except Exception as e:
            out["actual_original_full_3D_all_modal_time_parts"].append({
                "ppw":ppw,"status":"ORIGINAL_FULL_WAVE_EXACT_TEMPORAL_ATTRIBUTION_FAILED",
                "failure_type":type(e).__name__,"failure_detail":str(e)})
            arg.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        out["actual_original_full_3D_all_modal_time_parts"].append(result)
        arg.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("ORIGINAL_Q0_TRUE_NATIVE_FULL_3D_TIME_PARTS",ppw,
              "modes",result["actual_original_modes_in_full_3D_graph"],
              "original raw signed relative",
              result["native_pffdtd_all_modes_vs_true_original_raw_wave_complex_rel"],
              "term closure",result["three_exact_time_terms_vs_previous_original_single_formula_rel"],
              "seconds",round(result["elapsed_wall_s"],1),flush=True)
    cases=out["actual_original_full_3D_all_modal_time_parts"]
    for coarse,fine in zip(cases,cases[1:]):
        original=unpairs(coarse["original_native_full_250ms_signed_transfer_40_80"])-unpairs(
            fine["original_native_full_250ms_signed_transfer_40_80"])
        norm=max(float(np.linalg.norm(original)),1e-15)
        metric=compare_complex_transfer(
            reference=fine["original_native_full_250ms_signed_transfer_40_80"],
            candidate=coarse["original_native_full_250ms_signed_transfer_40_80"],
            frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
        terms={}
        for term in TERMS:
            delta=unpairs(coarse["original_full_record_time_terms_signed_40_80"][term])-unpairs(
                fine["original_full_record_time_terms_signed_40_80"][term])
            terms[term]={"full_record_original_signed_coarse_minus_fine_40_80":pairs(delta),
                "signed_delta_L2_norm_over_full_original_delta":float(np.linalg.norm(delta)/norm)}
        bands=[]
        for cb,fb in zip(coarse["original_native_five_frequency_bands_all_time_terms"],
                         fine["original_native_five_frequency_bands_all_time_terms"]):
            if cb["semidiscrete_mode_band_hz"]!=fb["semidiscrete_mode_band_hz"]:
                raise ValueError("true native semidiscrete spectral band boundaries changed")
            per={}
            total=np.zeros(2,dtype=complex)
            for term in TERMS:
                delta=unpairs(cb["signed_original_finite_window_time_parts_40_80"][term])-unpairs(
                    fb["signed_original_finite_window_time_parts_40_80"][term])
                total+=delta
                per[term]={"signed_coarse_minus_fine_40_80":pairs(delta),
                    "delta_L2_norm_over_entire_original_complex_delta":float(np.linalg.norm(delta)/norm)}
            bands.append({"original_true_mode_frequency_band_hz":cb["semidiscrete_mode_band_hz"],
                "coarse_original_3D_mode_count":cb["all_original_true_modes_count"],
                "fine_original_3D_mode_count":fb["all_original_true_modes_count"],
                "original_250ms_complete_signed_coarse_minus_fine_40_80":pairs(total),
                "full_band_delta_norm_over_original_total":float(np.linalg.norm(total)/norm),
                "original_signed_time_parts_per_band":per})
        sum_bands=sum((unpairs(b["original_250ms_complete_signed_coarse_minus_fine_40_80"])
            for b in bands),np.zeros(2,dtype=complex))
        sum_times=sum((unpairs(terms[t]["full_record_original_signed_coarse_minus_fine_40_80"])
            for t in TERMS),np.zeros(2,dtype=complex))
        for result in (sum_bands,sum_times):
            if np.linalg.norm(result-original)/norm>2e-10:
                raise ValueError("original exact 250ms signed time/band cancellation partition altered")
        out["adjacent_native_q0_parts"].append({
            "original_coarse_ppw":coarse["ppw"],"original_fine_ppw":fine["ppw"],
            "original_unmodified_full_signed_40_80_coarse_minus_fine":pairs(original),
            "actual_full_original_complex_magnitude_phase_metrics":metric,
            "actual_original_signed_three_time_region_deltas":terms,
            "actual_original_signed_five_band_by_three_time_deltas":bands,
            "all_mode_and_time_terms_exactly_recombine":True,
            "no_alternate_truncated_or_end_modified_score":True})
    out["actual_original_full_nongated_point_q0_retains_same_failure"]=True
    out["diagnostic_components_are_not_alternate_pressure_transfer_calibrations"]=True
    arg.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    for a in out["adjacent_native_q0_parts"]:
        print("EXACT_NATIVE_250MS_ENDPOINT_PARTITION",a["original_coarse_ppw"],
              a["original_fine_ppw"],"complex_error",
              round(a["actual_full_original_complex_magnitude_phase_metrics"]["complex_rms_relative"],6),
              "endpoint_norms",{t:round(a["actual_original_signed_three_time_region_deltas"][t]
                   ["signed_delta_L2_norm_over_full_original_delta"],3) for t in TERMS},
              "highband_norm",round(a["actual_original_signed_five_band_by_three_time_deltas"][-1]
                   ["full_band_delta_norm_over_original_total"],3),
              flush=True)
    print("ORIGINAL_Q0_UNMODIFIED_250MS_NONCONVERGENCE_PRESERVED",flush=True)
if __name__=="__main__":
    main()
