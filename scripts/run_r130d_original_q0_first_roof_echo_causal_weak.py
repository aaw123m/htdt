#!/usr/bin/env python3
"""Original PFFDTD q0 250ms HDF5 vs true first planar Neumann roof image.

The real original q0 wave is SHA-pinned and never modified. A fixed
1.1ms-before/after roof-arrival support isolates analytic *single-roof*
reflection from analytic direct and nonroof FIRST image arrivals, though
native PFFDTD may retain numerical dispersion from earlier direct pulse.
Auxiliary waveform witness never replaces original 250ms 40/80Hz gates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import h5py
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
from htdt.acoustic_pffdtd_adapter import (
    pffdtd_velocity_potential_to_pressure_trace,
    finite_record_pressure_transfer)
from htdt.r130d_causal_prefirst_weak import native_original_wave_weak_transfer
from htdt.r130d_causal_first_roof_echo import (
    ROOF_CENTER_S,ROOF_RADIUS_S,ROOF_END_S,ROOF_WIDTHS_S,
    analytic_native_64point_physical_roof_echo_weak)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs
from run_r130d_original_point_quadratic_pffdtd import PPW,PIN,file_hash

SCHEMA="htdt.r130d.original-q0-first-roof-echo-causal-weak-plan-1"


def validate_plan(p):
    old=p["previously_observed"];o=p["unchanged_original"]
    d=p["diagnostic"];co=p["comparison"];limits=p["limits"]
    if (p.get("schema_version")!=SCHEMA or p["issue"]!=938 or p["pr"]!=1055
        or not old["direct_causal_previously_observed_before_this_plan"]
        or not old["previous_wrong_c_squared_analytic_experiment_invalid"]
        or o["original_PFFDTD_pin"]!=PIN or o["ppw"]!=list(PPW)
        or o["source_xyz"]!=[1.5,2,2] or o["receiver_xyz"]!=[2.5,2,2]
        or not o["original_eight_source_and_eight_receiver_SHA_HDF5_indices_weights_unchanged"]
        or not o["q0_first_native_sample_one_and_all_later_zero"]
        or not o["original_native_h_dt_Nt_and_comms"]
        or o["c"]!=343.2 or o["rho"]!=1.2
        or o["original_full_record_s"]!=.25
        or o["original_signed_bins_hz"]!=[40,80]
        or [o[k] for k in ("full_original_complex_gate","full_original_magnitude_gate",
                          "full_original_phase_deg_gate")]!=[.2,.25,15]
        or not o["unchanged_full_original_upstream_PFFDTD_self_convergence_failed"]
        or d["physical_support_center_s"]!=ROOF_CENTER_S
        or d["true_physical_source_receiver_earliest_roof_echo_s"]!=ROOF_CENTER_S
        or abs(d["unmodified_physical_point_direct_s"]-1/343.2)>1e-15
        or d["nonroof_earliest_point_first_wall_reflection_s"]!=.011655011655011656
        or d["physical_support_radius_s"]!=ROOF_RADIUS_S
        or d["expected_support_upper_s"]!=ROOF_END_S
        or d["new_widths_s"]!=list(ROOF_WIDTHS_S)
        or d["true_neumann_roof_reflection_sign"]!="+1"
        or not d["check_all_other_nonroof_finite_specular_64pair_reflections_arrive_strictly_after_upper_support"]
        or not d["full_unmodified_250ms_original_signed_40_80_replay_2e_minus6"]
        or not d["retain_all_true_high_frequency_native_modes_and_unfavorable_results"]
        or not all(co.values())
        or limits!={"max_ppw_cases":5,"max_roof_weak_widths":3,
                    "new_native_upstream_PFFDTD_wave_runs":0,
                    "new_GitHub_Actions_runs":0,"preserve_scratch":True}
        or p["authority"]["original_UPSTREAM_PFFDTD_q0"]!="SELF_CONVERGENCE_FAILED"
        or p["authority"]["independent_BRAS_MFEM_room"]!="NOT_VALIDATED"
        or p["authority"]["product"]!="NO_GO"):
        raise ValueError("prospective fixed true physical roof echo q0 numerical research plan drift")
    return p


def one_case(ppw,sim,previous,old):
    if (file_hash(sim/"comms_out.h5")!=previous["original_native_comm_sha256"]
        or file_hash(sim/"vox_out.h5")!=previous["original_solver_geometry_sha256"]
        or file_hash(sim/"sim_outs.h5")!=old["original_sha256_full_real_250ms_native_wave"]):
        raise ValueError("original PFFDTD q0 SHA or actual original 250ms real native wave changed")
    with h5py.File(sim/"vox_out.h5","r") as f:
        axes=[np.asarray(f[k][:],dtype=float) for k in ("xv","yv","zv")]
    native_shape=tuple(len(a) for a in axes)
    with h5py.File(sim/"comms_out.h5","r") as f:
        si=np.asarray(f["in_ixyz"][:],dtype=np.int64)
        ri=np.asarray(f["out_ixyz"][:],dtype=np.int64)
        swave=np.asarray(f["in_sigs"][:],dtype=float)
        rw=np.asarray(f["out_alpha"][:],dtype=float).ravel()
        nt=int(f["Nt"][()])
        if (si.shape!=(8,) or ri.shape!=(8,)
            or swave.shape!=(8,nt) or rw.shape!=(8,)
            or abs(rw.sum()-1)>1e-12 or np.any(swave[:,1:]!=0)
            or int(f["diff"][()])!=0):
            raise ValueError("native raw original source-receiver changed from exactly 8 x 8 q0")
        strength=float(swave[:,0].sum())
        sw=swave[:,0]/strength
    with h5py.File(sim/"sim_consts.h5","r") as f:
        dt=float(f["Ts"][()]);h=float(f["h"][()])
        c=float(f["c"][()]);l2=float(f["l2"][()])
    if (abs(c-343.2)>1e-12 or abs(h-343.2/(100*ppw))>1e-12
        or abs(strength-l2/h)>1e-10
        or abs(dt-old["original_native_Ts_s"])>1e-12
        or nt!=old["original_native_Nt"]):
        raise ValueError("original full 250ms h/dt/Nt/source normalization changed")
    with h5py.File(sim/"sim_outs.h5","r") as f:
        raw=np.asarray(f["u_out"][:],dtype=float)
    if raw.shape!=(8,nt):
        raise ValueError("original native eight receiver 250ms waveform channels invalid")
    src=np.column_stack([axes[k][v] for k,v in enumerate(
        np.unravel_index(si,native_shape))])
    rec=np.column_stack([axes[k][v] for k,v in enumerate(
        np.unravel_index(ri,native_shape))])
    np.testing.assert_allclose(sw@src,np.array([1.5,2,2]),rtol=0,atol=3e-10)
    np.testing.assert_allclose(rw@rec,np.array([2.5,2,2]),rtol=0,atol=3e-10)
    p=pffdtd_velocity_potential_to_pressure_trace(
        rw@raw,time_step_s=dt,density_kg_m3=1.2)
    q=np.zeros(nt,dtype=float);q[0]=1.
    signed=finite_record_pressure_transfer(
        p,q,time_step_s=dt,frequency_hz=np.array([40.,80.]))
    prior=unpairs(previous["unmodified_original_transfer_pa_per_m3_s"])
    replay_err=float(np.linalg.norm(signed-prior)/max(np.linalg.norm(prior),1e-14))
    if replay_err>2e-6:
        raise ValueError("original untouched full room signed finite 250ms 40/80 pressure spectrum changed")
    cases=[]
    for width in ROOF_WIDTHS_S:
        actual=native_original_wave_weak_transfer(
            raw,rw,dt_s=dt,width_s=width,center_s=ROOF_CENTER_S,
            radius_s=ROOF_RADIUS_S)
        analytic=analytic_native_64point_physical_roof_echo_weak(
            src,sw,rec,rw,width_s=width)
        v=actual["original_q0_native_weak_pressure_over_unit_input"]
        ref=analytic["original_64pair_finite_roof_single_bounce_signed_weak_analytic"]
        cases.append({
            "physical_original_roof_echo_weak_width_s":width,
            "original_entire_wave_native_roof_window_weak":actual,
            "continuum_true_physical_neumann_roof_64_original_nodes_analytic":analytic,
            "native_total_roof_window_vs_single_roof_analytic_signed_ratio":float(v/ref),
            "native_total_roof_window_vs_single_roof_analytic_relative":float(abs(v/ref-1)),
            "native_window_direct_numeric_dispersive_tail_may_be_present":True,
            "not_original_full_record_acceptance":True})
    return {"ppw":ppw,"original_true_native_comms_HDF5_SHA256":file_hash(sim/"comms_out.h5"),
        "original_true_native_voxel_HDF5_SHA256":file_hash(sim/"vox_out.h5"),
        "original_true_native_entire_real_q0_HDF5_SHA256":file_hash(sim/"sim_outs.h5"),
        "original_native_Ts_s":dt,"original_native_Nt":nt,
        "original_true_source_pulse_l2_over_h":strength,
        "original_full_record_250ms_signed_40_80_replayed":pairs(signed),
        "original_full_record_relative_to_frozen":replay_err,
        "old_observed_causal_DIRECT_weak_green_control_3_widths":old[
            "three_predeclared_compact_weak_distribution_tests"],
        "new_original_q0_roof_echo_fixed_weak_width_cases":cases,
        "full_250ms_native_wave_never_filtered_or_windowed_for_authority":True}


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args()
    data=args.plan.read_bytes()
    p=validate_plan(json.loads(data.decode("utf-8")))
    originals=json.loads((ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json").read_text(encoding="utf-8"))
    observed=json.loads((ROOT/p["previously_observed"]["corrected_direct_causal_evidence"]).read_text(encoding="utf-8"))
    orig={r["ppw"]:r for r in originals["actual_native_wave_cases"]}
    old={r["ppw"]:r for r in observed["actual_new_weak_results_original_SHA_native_5grid"]}
    if set(orig)!=set(PPW) or set(old)!=set(PPW):
        raise ValueError("all original true PFFDTD raw q0 5grid source, waveform and direct controls missing")
    dirs={}
    for f in args.original_sims_root.rglob("comms_out.h5"):
        h=file_hash(f)
        match=[k for k in PPW if orig[k]["original_native_comm_sha256"]==h]
        if len(match)==1:
            if match[0] in dirs:raise ValueError("duplicate SHA original source q0")
            dirs[match[0]]=f.parent
    if set(dirs)!=set(PPW):raise ValueError("all five original SHA pinned native wave folders required")
    e={"schema_version":"htdt.r130d.original-q0-first-roof-echo-weak-evidence-1",
       "precommitted_plan_SHA256_lf":hashlib.sha256(data.replace(b"\r\n",b"\n")).hexdigest(),
       "preregistered_plan":p,
       "native_real_wave_and_previous_direct_weak_SHA_protected":True,
       "original_canonical_fullroom":"SELF_CONVERGENCE_FAILED",
       "independent_physical":"NOT_VALIDATED","product":"NO_GO",
       "new_upstream_original_PFFDTD_waves":0,"new_GitHub_Actions_runs":0,
       "genuine_single_echo_continuum_only_not_exact_native_direct_tail_decomposition":True,
       "actual_original_q0_five_grid_real_first_roof_echo_weak_cases":[],
       "unaltered_original_250ms_all_adjacent_frozen_three_scores":[]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    for k in PPW:
        try:z=one_case(k,dirs[k],orig[k],old[k])
        except Exception as ex:
            e["actual_original_q0_five_grid_real_first_roof_echo_weak_cases"].append({
                "ppw":k,"status":"REAL_FIRST_ROOF_ECHO_DIAGNOSTIC_INCOMPLETE",
                "exception_type":type(ex).__name__,"reason":str(ex)})
            args.output.write_text(json.dumps(e,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        e["actual_original_q0_five_grid_real_first_roof_echo_weak_cases"].append(z)
        args.output.write_text(json.dumps(e,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("REAL_ORIGINAL_PFFDTD_FIRST_ROOF_ECHO_TEST",k,
              "native_over_image",[round(r["native_total_roof_window_vs_single_roof_analytic_signed_ratio"],6) for r in z["new_original_q0_roof_echo_fixed_weak_width_cases"]],
              "prior_direct",[round(r["actual_native_over_assumed_continuum_source_model_signed_ratio"],6) for r in z["old_observed_causal_DIRECT_weak_green_control_3_widths"]],
              flush=True)
    cases=e["actual_original_q0_five_grid_real_first_roof_echo_weak_cases"]
    for co,fi in zip(cases,cases[1:]):
        metrics=compare_complex_transfer(
            reference=fi["original_full_record_250ms_signed_40_80_replayed"],
            candidate=co["original_full_record_250ms_signed_40_80_replayed"],
            frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
        e["unaltered_original_250ms_all_adjacent_frozen_three_scores"].append({
            "coarse_ppw":co["ppw"],"fine_ppw":fi["ppw"],
            "canonical_original_full_unfiltered_250ms_P_T_over_Q_T":metrics,
            "original_full_three_gate_pass":bool(
                metrics["complex_rms_relative"]<=.2 and
                metrics["magnitude_max_relative"]<=.25 and
                metrics["phase_max_deg"]<=15),
            "first_roof_weak_not_canonical_acceptance":True})
    e["canonical_original_all_pairs_accepted"]=all(
        x["original_full_three_gate_pass"]
        for x in e["unaltered_original_250ms_all_adjacent_frozen_three_scores"])
    e["do_not_promote_original_production_or_physical_claim"]=True
    args.output.write_text(json.dumps(e,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("FIRST_ROOF_ECHO_ORIGINAL_250MS_STILL_FAILED",not e["canonical_original_all_pairs_accepted"],
          "complex",[round(x["canonical_original_full_unfiltered_250ms_P_T_over_Q_T"]["complex_rms_relative"],6)
              for x in e["unaltered_original_250ms_all_adjacent_frozen_three_scores"]],flush=True)
if __name__=="__main__":main()
