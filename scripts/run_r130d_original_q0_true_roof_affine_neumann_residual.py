#!/usr/bin/env python3
"""Actual original PFFDTD manufactured harmonic true-oblique-roof Neumann audit.

Prerecorded prospective plan MUST exist and be pushed before computations.
All original native SHA HDF5 8-point q0 and complete signed 250ms 40/80Hz
remain untouched and independently replayed. Compare original native
boundary *strong* action vs exact physical cut-Q1 Neumann *weak* residual,
never equate their different units or imply unique q0 convergence cause.
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
from htdt.r130d_native_cut_roof_Q1_galerkin import (
    cut_roof_native_original_Q1_galerkin_yz)
from htdt.r130d_true_roof_manufactured_neumann import (
    true_roof_affine_local_neumann_residual)
from htdt.acoustic_pffdtd_adapter import (
    pffdtd_velocity_potential_to_pressure_trace,
    finite_record_pressure_transfer)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_point_quadratic_pffdtd import PPW,PIN,file_hash
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs

SCHEMA="htdt.r130d.original-native-oblique-roof-affine-harmonic-operator-consistency-plan-1"


def validate_plan(p):
    o=p["original"];a=p["manufactured"]
    v=p["validation"];l=p["limits"];r=p["release"]
    if (p.get("schema_version")!=SCHEMA
        or p["issue"]!=938 or p["pr"]!=1055
        or o["pffdtd_pin"]!=PIN or o["ppw"]!=list(PPW)
        or o["original_40_80_full250ms_frozen_gates"]!=[.2,.25,15]
        or not o["unchanged_real_8_source_8_receiver_native_weights_indices_SHA"]
        or not o["unchanged_full_250ms_q0_40_80_signed"]
        or not o["preserve_original_physical_room_56m3"]
        or not o["no_native_pffdtd_rerun"]
        or a["original_native_6direction_adjacency_order"]!=[
            "+x","-x","+y","-y","+z","-z"]
        or not a["report_original_native_strong_laplacian_residual_RMS_peak_and_1over_h_scaled"]
        or not a["report_exact_cutQ1_manufactured_weak_residual_abs_RMS_peak"]
        or not a["report_no_canonical_actual_full_wave_CAUSAL_PROOF"]
        or not all(v.values())
        or l!={"max_ppws":5,"max_active_yz_nodes":3300,
                 "no_new_pffdtd_wave_runs":0,"no_github_actions":0,
                 "preserve_scratch":True}
        or r["canonical_original"]!="SELF_CONVERGENCE_FAILED"
        or r["independent_physics"]!="NOT_VALIDATED"
        or r["product"]!="NO_GO"
        or not r["PR_draft"] or not r["issue_open"]):
        raise ValueError("preregistered physical roof manufactured original PFFDTD audit changed")
    return p


def one_case(ppw,dir,original,roof,consistent,plan):
    if (file_hash(dir/"vox_out.h5")!=original["original_solver_geometry_sha256"]
        or file_hash(dir/"comms_out.h5")!=original["original_native_comm_sha256"]
        or file_hash(dir/"sim_outs.h5")!=roof["original_true_native_entire_real_q0_HDF5_SHA256"]):
        raise ValueError("original unmodified PFFDTD room/source/wave SHA changed")
    with h5py.File(dir/"vox_out.h5","r") as f:
        axes=[np.asarray(f[q][:],dtype=float) for q in ("xv","yv","zv")]
        boundary=np.asarray(f["bn_ixyz"][:],dtype=np.int64)
        adjacency=np.asarray(f["adj_bn"][:],dtype=np.int64)
    dims=tuple(len(a) for a in axes)
    with h5py.File(dir/"comms_out.h5","r") as f:
        source=np.asarray(f["in_ixyz"][:],dtype=np.int64)
        receiver=np.asarray(f["out_ixyz"][:],dtype=np.int64)
        signal=np.asarray(f["in_sigs"][:],dtype=float)
        alpha=np.asarray(f["out_alpha"][:],dtype=float).ravel()
        nt=int(f["Nt"][()])
        if (source.shape!=(8,) or receiver.shape!=(8,)
            or alpha.shape!=(8,) or signal.shape!=(8,nt)
            or np.any(signal[:,1:]!=0) or abs(float(alpha.sum())-1)>1e-12
            or int(f["diff"][()])!=0):
            raise ValueError("actual original raw 8in/8out q0 impulse changed")
        amplitude=float(signal[:,0].sum())
        srcpos=np.column_stack([
            axes[k][v] for k,v in enumerate(np.unravel_index(source,dims))])
        recpos=np.column_stack([
            axes[k][v] for k,v in enumerate(np.unravel_index(receiver,dims))])
        if (not np.allclose((signal[:,0]/amplitude)@srcpos,[1.5,2,2],
                             atol=2e-11,rtol=0)
            or not np.allclose(alpha@recpos,[2.5,2,2],atol=2e-11,rtol=0)):
            raise ValueError("actual original receiver/source trilinear HDF5 position shifted")
    with h5py.File(dir/"sim_consts.h5","r") as f:
        dt=float(f["Ts"][()]);h=float(f["h"][()])
        c=float(f["c"][()]);l2=float(f["l2"][()])
    if (abs(c-343.2)>1e-12 or abs(h-c/(100*ppw))>1e-12
        or abs(amplitude-l2/h)>1e-10
        or abs(dt-consistent["original_native_Ts_s"])>1e-12
        or nt!=consistent["original_native_full_Nt"]):
        raise ValueError("original native physical q0 sample clock or strength changed")
    yz=cut_roof_native_original_Q1_galerkin_yz(
        axes[1],axes[2],max_active_yz_nodes=plan["limits"]["max_active_yz_nodes"])
    if yz.native_yz_modes!=consistent[
        "real_original_native_cartesian_yz_Q1_physical_support_modes"]:
        raise ValueError("old full active native sliver basis count changed")
    audit=true_roof_affine_local_neumann_residual(
        axes[1],axes[2],boundary,adjacency,dims,yz)
    if (abs(yz.physical_area_m2-14)>2e-8
        or abs(4*yz.physical_area_m2-56)>8e-8):
        raise ValueError("true physical cut-Q1 integration changed original room volume")
    with h5py.File(dir/"sim_outs.h5","r") as f:
        full_raw=np.asarray(f["u_out"][:],dtype=float)
    if full_raw.shape!=(8,nt) or not np.isfinite(full_raw).all():
        raise ValueError("original 250ms real HDF5 native 8-channel pressure missing")
    pressure=pffdtd_velocity_potential_to_pressure_trace(
        alpha@full_raw,time_step_s=dt,density_kg_m3=1.2)
    q=np.zeros(nt);q[0]=1.
    signed=finite_record_pressure_transfer(
        pressure,q,time_step_s=dt,frequency_hz=np.array([40.,80.]))
    expected=unpairs(original["unmodified_original_transfer_pa_per_m3_s"])
    err=float(np.linalg.norm(signed-expected)/max(np.linalg.norm(expected),1e-14))
    if err>2e-6:raise ValueError("original full 250ms signed 40/80 q0 changed")
    return {
        "ppw":ppw,
        "source_HDF5_sha256":file_hash(dir/"comms_out.h5"),
        "room_HDF5_sha256":file_hash(dir/"vox_out.h5"),
        "original_full_wave_HDF5_sha256":file_hash(dir/"sim_outs.h5"),
        "original_native_h_m":h,"original_native_dt_s":dt,"original_Nt":nt,
        "real_original_input_strength_8nodes":amplitude,
        "physical_true_Q1_all_positive_support_yz_basis_modes":yz.native_yz_modes,
        "physical_true_wet_area_yz_m2":yz.physical_area_m2,
        "physical_true_room_volume_m3":4*yz.physical_area_m2,
        "original_real_PFFDTD_vs_true_neumann_Q1_affine_roof_manufactured":audit,
        "unchanged_original_250ms_signed_40_80_P_T_over_Q_T":pairs(signed),
        "canonical_real_wave_vs_historical_relative":err,
        "original_true_wave_and_q0_source_untouched":True}


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args()
    raw=args.plan.read_bytes()
    p=validate_plan(json.loads(raw.decode("utf-8")))
    o=p["original"]
    src=json.loads((ROOT/o["original_HDF5_evidence"]).read_text(encoding="utf-8"))
    roof=json.loads((ROOT/o["original_roof_first_reflection_evidence"]).read_text(encoding="utf-8"))
    cut=json.loads((ROOT/o["original_full_Q1_cut_evidence"]).read_text(encoding="utf-8"))
    old={r["ppw"]:r for r in src["actual_native_wave_cases"]}
    rr={r["ppw"]:r for r in roof["actual_original_q0_five_grid_real_first_roof_echo_weak_cases"]}
    qc={r["ppw"]:r for r in cut["actual_native_original_point_q0_Q1_cutroof_full_modes_cases"]}
    if set(old)!=set(PPW) or set(rr)!=set(PPW) or set(qc)!=set(PPW):
        raise ValueError("all five actual SHA original wave and Q1 physical controls required")
    folders={}
    for file in args.original_sims_root.rglob("comms_out.h5"):
        sha=file_hash(file)
        ks=[k for k in PPW if old[k]["original_native_comm_sha256"]==sha]
        if len(ks)==1:
            if ks[0] in folders:raise ValueError("duplicate actual original SHA q0 folder")
            folders[ks[0]]=file.parent
    if set(folders)!=set(PPW):
        raise ValueError("all actual original 5grid PFFDTD raw HDF5 missing")
    output={
        "schema_version":"htdt.r130d.original-q0-oblique-roof-affine-manufactured-strong-vs-weak-evidence-1",
        "preregistered_plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":p,
        "canonical_original_q0":"SELF_CONVERGENCE_FAILED",
        "physical_validation":"NOT_VALIDATED","product":"NO_GO",
        "new_PFFDTD_runs":0,"new_GitHub_actions":0,
        "actual_original_genuine_6neigh_vs_true_roof_Q1_five_grid_manufactured":[],
        "unchanged_original_full250ms_signed_4adjacent_gate_scores":[]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    for k in PPW:
        try: v=one_case(k,folders[k],old[k],rr[k],qc[k],p)
        except Exception as ex:
            output["actual_original_genuine_6neigh_vs_true_roof_Q1_five_grid_manufactured"].append({
                "ppw":k,"state":"INCOMPLETE_FAIL_CLOSED",
                "exception":type(ex).__name__,"message":str(ex)})
            args.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        output["actual_original_genuine_6neigh_vs_true_roof_Q1_five_grid_manufactured"].append(v)
        args.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        z=v["original_real_PFFDTD_vs_true_neumann_Q1_affine_roof_manufactured"]
        print("REAL_NATIVE_ROOF_AFFINE_STRONG_VS_WEAK",k,
              "original_stair_RMS_1pm",z["original_strong_laplacian_affine_harmonic_residual_RMS_inverse_m"],
              "h_scaled",z["original_h_times_strong_laplacian_roof_residual_RMS_dimensionless"],
              "exact_Q1_max_Ku",z["true_cut_Q1_roof_only_affine_weak_K_u_peak_in_Ku_units"],
              "true_roof_stencil_rows",z["actual_original_native_genuine_roof_stencil_rows"],
              flush=True)
    rows=output["actual_original_genuine_6neigh_vs_true_roof_Q1_five_grid_manufactured"]
    for co,fi in zip(rows,rows[1:]):
        val=compare_complex_transfer(
            reference=fi["unchanged_original_250ms_signed_40_80_P_T_over_Q_T"],
            candidate=co["unchanged_original_250ms_signed_40_80_P_T_over_Q_T"],
            frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
        output["unchanged_original_full250ms_signed_4adjacent_gate_scores"].append({
            "coarse_ppw":co["ppw"],"fine_ppw":fi["ppw"],
            "real_original_full250ms_canonical_signed_three_metrics":val,
            "all_original_three_gates_pass":bool(
                val["complex_rms_relative"]<=.2
                and val["magnitude_max_relative"]<=.25
                and val["phase_max_deg"]<=15)})
    output["original_q0_full250ms_still_all4_nonconvergent"]=not all(
        r["all_original_three_gates_pass"] for r in output[
            "unchanged_original_full250ms_signed_4adjacent_gate_scores"])
    output["manufactured_strong_and_weak_residual_UNITS_not_comparable"]=True
    output["not_proof_sole_causing_original_250ms_nonconvergence"]=True
    args.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("UNCHANGED_ORIGINAL_NATIVE_FULL_250MS_GATES",[
        (r["coarse_ppw"],r["fine_ppw"],r[
            "real_original_full250ms_canonical_signed_three_metrics"]["complex_rms_relative"],
         r["all_original_three_gates_pass"])
        for r in output["unchanged_original_full250ms_signed_4adjacent_gate_scores"]],flush=True)

if __name__=="__main__":main()
