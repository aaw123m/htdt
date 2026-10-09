#!/usr/bin/env python3
"""Original unmodified native PFFDTD 8node q0 pre-echo weak Green test.

The actual existing native 250ms wave is read whole with SHA checks;
the auxiliary compact physical-time distribution witnesses are computed
on copies ONLY, never altering original authority 40/80Hz full pressure.
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
    pffdtd_velocity_potential_to_pressure_trace,finite_record_pressure_transfer)
from htdt.r130d_causal_prefirst_weak import (
    TAU,HALF_SUPPORT_S,WIDTHS_S,
    compact_odd_witness,native_original_wave_weak_transfer,
    original_native_64node_analytic_weak_reference)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_point_quadratic_pffdtd import PIN,PPW,file_hash
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs

SCHEMA="htdt.r130d.original-q0-causal-prefirst-weak-test-plan-1"


def validate_plan(p):
    o=p["original"];w=p["witness"];a=p["assessment"];lim=p["limits"]
    if (p.get("schema_version")!=SCHEMA or p["issue"]!=938 or p["pr"]!=1055
        or o["upstream_sha"]!=PIN or o["ppw"]!=list(PPW)
        or o["source_physical_m"]!=[1.5,2,2]
        or o["receiver_physical_m"]!=[2.5,2,2]
        or not o["true_original_eight_HDF5_weights_indices_and_native_q0_untouched"]
        or not o["native_each_h_Ts_Nt"]
        or o["full_original_record_s"]!=.25
        or o["signed_original_bins_hz"]!=[40,80]
        or o["c_m_s"]!=343.2 or o["rho_kg_m3"]!=1.2
        or [o[k] for k in ("full_original_complex_gate",
                           "full_original_magnitude_gate",
                           "full_original_phase_deg_gate")]!=[.2,.25,15]
        or not o["no_mode_removal_or_signal_change"]
        or w["half_support_radius_s"]!=HALF_SUPPORT_S
        or w["physical_width_s"]!=list(WIDTHS_S)
        or not w["replay_original_full_signed"].startswith("original full record 40/80Hz P_T/Q_T recomputed from SAME")
        or not all(a.values())
        or lim!={"max_cases":5,"max_witnesses":3,
                 "new_native_PFFDTD_wave_runs":0,
                 "new_github_actions_runs":0,"preserve_scratch":True}
        or p["authority"]["original_upstream_q0"]!="SELF_CONVERGENCE_FAILED"
        or p["authority"]["independent_physical"]!="NOT_VALIDATED"
        or p["authority"]["product"]!="NO_GO"):
        raise ValueError("original full q0 and pre-first-bounce physical witness preregistration drift")
    return p


def one_case(p,ppw,sim,original,old_modal,prev_green, *,
             widths_s=WIDTHS_S, continuum_analytic_correction_factor=1.):
    if (file_hash(sim/"comms_out.h5")!=original["original_native_comm_sha256"]
        or file_hash(sim/"vox_out.h5")!=original["original_solver_geometry_sha256"]
        or file_hash(sim/"sim_outs.h5")!=old_modal["original_native_sim_output_SHA256"]):
        raise ValueError("original true native 8node q0 waveform or room SHA changed")
    with h5py.File(sim/"vox_out.h5","r") as f:
        axes=[np.asarray(f[k][:],dtype=float) for k in ("xv","yv","zv")]
    dims=tuple(len(x) for x in axes)
    with h5py.File(sim/"comms_out.h5","r") as f:
        si=np.asarray(f["in_ixyz"][:],dtype=np.int64)
        ri=np.asarray(f["out_ixyz"][:],dtype=np.int64)
        swaves=np.asarray(f["in_sigs"][:],dtype=float)
        rw=np.asarray(f["out_alpha"][:],dtype=float).ravel()
        nt=int(f["Nt"][()])
        if (si.shape!=(8,) or ri.shape!=(8,)
            or swaves.shape!=(8,nt) or rw.shape!=(8,)
            or np.any(swaves[:,1:]!=0)
            or int(f["diff"][()])!=0 or abs(rw.sum()-1)>1e-12):
            raise ValueError("original native source receiver 8node q0 changed")
        strength=float(swaves[:,0].sum())
        sw=swaves[:,0]/strength
    with h5py.File(sim/"sim_consts.h5","r") as f:
        dt=float(f["Ts"][()]);h=float(f["h"][()])
        speed=float(f["c"][()]);l2=float(f["l2"][()])
    if (abs(speed-343.2)>1e-12 or abs(h-speed/(100*ppw))>1e-12
        or nt!=old_modal["original_record_samples"]
        or abs(dt-old_modal["original_native_time_step_s"])>1e-12
        or abs(strength-l2/h)>1e-10):
        raise ValueError("true original physical point native q0 strength/clock changed")
    with h5py.File(sim/"sim_outs.h5","r") as f:
        raw=np.asarray(f["u_out"][:],dtype=float)
    if raw.shape!=(8,nt) or not np.isfinite(raw).all():
        raise ValueError("original full 8 receiver raw native wave invalid")
    srcpos=np.column_stack(tuple(axes[i][q] for i,q in enumerate(np.unravel_index(si,dims))))
    recpos=np.column_stack(tuple(axes[i][q] for i,q in enumerate(np.unravel_index(ri,dims))))
    if (not np.allclose(srcpos,prev_green["original_native_eight_source_xyz_m"],rtol=0,atol=1e-13)
        or not np.allclose(recpos,prev_green["original_native_eight_receiver_xyz_m"],rtol=0,atol=1e-13)
        or not np.allclose(sw,prev_green["original_native_source_HDF5_normalized_eight_coefficients"],rtol=0,atol=1e-13)
        or not np.allclose(rw,prev_green["original_native_receiver_HDF5_eight_coefficients"],rtol=0,atol=1e-13)):
        raise ValueError("original cached first-echo Green 8node HDF5 weights or coordinates changed")
    # Always independently recompute original canonical full waveform:
    # NO truncation, no pressure time witness, and no changing source.
    phi=rw@raw
    pressure=pffdtd_velocity_potential_to_pressure_trace(
        phi,time_step_s=dt,density_kg_m3=1.2)
    q=np.zeros(nt);q[0]=1.
    true_complete=finite_record_pressure_transfer(
        pressure,q,time_step_s=dt,frequency_hz=np.array([40.,80.]))
    old_complete=unpairs(original["unmodified_original_transfer_pa_per_m3_s"])
    true_relative=float(np.linalg.norm(true_complete-old_complete)/
        max(np.linalg.norm(old_complete),1e-14))
    if true_relative>2e-6:
        raise ValueError("replayed ORIGINAL complete 250ms frozen 40/80Hz q0 transfer changed")
    moment=[]
    for width in widths_s:
        calc=native_original_wave_weak_transfer(
            raw,rw,dt_s=dt,width_s=width)
        analytic=original_native_64node_analytic_weak_reference(
            srcpos,sw,recpos,rw,width_s=width)
        # In the first already-observed experiment the analytic reference
        # accidentally had an extra c². Its original JSON is immutable and
        # its absolute-ratio verdict INVALID. For a separately preregistered
        # later experiment, divide ONLY the analytical response by c²,
        # since [(1/c²)∂tt−Δ]G=δ and source q has area dt.
        # No native q0 value or experimental per-sample pressure is changed.
        if continuum_analytic_correction_factor!=1.:
            analytic["historical_old_analytic_expected_with_extra_c_squared_INVALID"]=analytic[
                "original_64_native_pairs_continuum_causal_green_weak_distribution"]
            analytic["original_64_native_pairs_continuum_causal_green_weak_distribution"]*=continuum_analytic_correction_factor
            analytic["true_physical_source_to_receiver_point_continuum_weak_distribution"]*=continuum_analytic_correction_factor
            analytic["pre_observed_analytic_c_squared_units_error_corrected"]=True
            analytic["analytic_absolute_source_scale_fixed_by_PDE_not_fitted"]=True
        v=calc["original_q0_native_weak_pressure_over_unit_input"]
        reference=analytic["original_64_native_pairs_continuum_causal_green_weak_distribution"]
        if reference==0 or not np.isfinite(reference):
            raise ValueError("physical original 8node analytic weak distribution cancelled singularly")
        ratio=v/reference
        rel=abs(ratio-1.)
        moment.append({
            "physical_weak_test_width_s":float(width),
            "native_unmodified_original_weak_result":calc,
            "analytic_all_original_64pair_continuum_direct":analytic,
            "actual_native_over_assumed_continuum_source_model_signed_ratio":float(ratio),
            "actual_native_vs_assumed_continuum_weak_relative":float(rel),
            "conditional_source_normalization_not_fitted_or_verified":True})
    return {
        "ppw":ppw,
        "original_sha256_comms":file_hash(sim/"comms_out.h5"),
        "original_sha256_voxels":file_hash(sim/"vox_out.h5"),
        "original_sha256_full_real_250ms_native_wave":file_hash(sim/"sim_outs.h5"),
        "original_native_h_m":h,"original_native_Ts_s":dt,"original_native_Nt":nt,
        "original_native_q0_in_sigs_sum":strength,
        "unchanged_original_full_250ms_signed_40_80_recomputed":pairs(true_complete),
        "unchanged_original_full_250ms_vs_preobserved_relative":true_relative,
        "original_native_original_full_250ms_PFFDTD_all_modes_unfiltered":True,
        "causal_pre_first_reflection_auxiliary_test_not_original_acceptance":True,
        "three_predeclared_compact_weak_distribution_tests":moment}


def main():
    a=argparse.ArgumentParser()
    a.add_argument("--plan",type=Path,required=True)
    a.add_argument("--original-sims-root",type=Path,required=True)
    a.add_argument("--output",type=Path,required=True)
    args=a.parse_args()
    raw=args.plan.read_bytes()
    p=validate_plan(json.loads(raw.decode("utf-8")))
    o=p["original"]
    source=json.loads((ROOT/o["source_reference"]).read_text(encoding="utf-8"))
    modal=json.loads((ROOT/o["original_complete_modal_reference"]).read_text(encoding="utf-8"))
    green=json.loads((ROOT/o["continuous_physical_G_reference"]).read_text(encoding="utf-8"))
    orig={r["ppw"]:r for r in source["actual_native_wave_cases"]}
    spec={r["ppw"]:r for r in modal["actual_original_unmodified_all_mode_native_cases"]}
    direct={r["ppw"]:r for r in green["actual_original_8node_HDF5_5grid_analytic_direct_point_Green_cases"]}
    if (set(orig)!=set(PPW) or set(spec)!=set(PPW) or set(direct)!=set(PPW)):
        raise ValueError("true original native physical q0 all 5 HDF5 references required")
    sims={}
    for f in args.original_sims_root.rglob("comms_out.h5"):
        sha=file_hash(f)
        m=[k for k in PPW if orig[k]["original_native_comm_sha256"]==sha]
        if len(m)==1:
            if m[0] in sims:raise ValueError("duplicate true native original source HDF5")
            sims[m[0]]=f.parent
    if set(sims)!=set(PPW):
        raise ValueError("all original SHA q0 ppw28/32/36/40/44 native 250ms true waves required")
    evidence={
        "schema_version":"htdt.r130d.original-q0-causal-prefirst-weak-test-evidence-1",
        "prospective_plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":p,
        "original_authority":"SELF_CONVERGENCE_FAILED",
        "independent_physical_validation":"NOT_VALIDATED","product":"NO_GO",
        "new_upstream_PFFDTD_wave_runs":0,"new_GitHub_Actions_runs":0,
        "auxiliary_early_weak_test_does_NOT_change_original_250ms_gate":True,
        "actual_original_five_grid_pre_first_echo_weak_distribution_cases":[],
        "original_true_entire_record_four_adjacent_frozen_scores":[]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    for k in PPW:
        try:r=one_case(p,k,sims[k],orig[k],spec[k],direct[k])
        except Exception as ex:
            evidence["actual_original_five_grid_pre_first_echo_weak_distribution_cases"].append({
                "ppw":k,"status":"EARLY_WEAK_DIAGNOSTIC_INCOMPLETE",
                "exception_type":type(ex).__name__,"error":str(ex)})
            args.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        evidence["actual_original_five_grid_pre_first_echo_weak_distribution_cases"].append(r)
        args.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("NATIVE_ORIGINAL_Q0_PRE_ECHO_WEAK_GREEN",k,
              "widths_ms",[round(t["physical_weak_test_width_s"]*1e3,4) for t in r["three_predeclared_compact_weak_distribution_tests"]],
              "native/continuum_ratios",[round(t["actual_native_over_assumed_continuum_source_model_signed_ratio"],6)
                for t in r["three_predeclared_compact_weak_distribution_tests"]],
              "zero_prearrival",[t["native_unmodified_original_weak_result"]["negative_control_native_abs_p_before_1p5ms_pa"]
                for t in r["three_predeclared_compact_weak_distribution_tests"]],flush=True)
    cases=evidence["actual_original_five_grid_pre_first_echo_weak_distribution_cases"]
    for co,fi in zip(cases,cases[1:]):
        score=compare_complex_transfer(
            reference=fi["unchanged_original_full_250ms_signed_40_80_recomputed"],
            candidate=co["unchanged_original_full_250ms_signed_40_80_recomputed"],
            frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
        fail=bool(score["complex_rms_relative"]<=.2 and
                  score["magnitude_max_relative"]<=.25 and
                  score["phase_max_deg"]<=15)
        evidence["original_true_entire_record_four_adjacent_frozen_scores"].append({
            "coarse_ppw":co["ppw"],"fine_ppw":fi["ppw"],
            "original_UNCHANGED_250ms_40_80_three_gates":score,
            "original_entire_record_three_gate_PASS":fail,
            "auxiliary_pre_echo_weak_used_for_canonical_acceptance":False})
    evidence["original_upstream_original_complete_record_all_pairs_pass"]=all(
        s["original_entire_record_three_gate_PASS"] for s in evidence[
            "original_true_entire_record_four_adjacent_frozen_scores"])
    evidence["no_qualified_original_release_or_physical_room_claim"]=True
    args.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("ORIGINAL_TRUE_250MS_SCORE_STILL",[
        (x["coarse_ppw"],x["fine_ppw"],
         round(x["original_UNCHANGED_250ms_40_80_three_gates"]["complex_rms_relative"],6))
        for x in evidence["original_true_entire_record_four_adjacent_frozen_scores"]],flush=True)
    print("EARLY_DISTRIBUTION_DIAGNOSTIC_NOT_RELEASE",evidence["original_authority"],flush=True)
if __name__=="__main__":main()
