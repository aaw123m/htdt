#!/usr/bin/env python3
"""Independent 3D continuum causal point Green vs actual original q0 geometry.

The signed analytic continuum direct arrival and singly reflected infinite
plane solutions are NOT substitutes for true complete room 250ms PFFDTD.
This diagnoses original eight-node spatial delta consistency using the
exact SHA-pinned real native input HDF5 source/receiver positions and weights.
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
from htdt.r130d_general3d_validation import compare_complex_transfer
from htdt.r130d_retarded_point_green import (
    C,RHO,FREQUENCIES_HZ,ROOM_WALLS,
    green_retarded_signed,original_8node_retarded_signed,
    physical_point_first_reflections)
from run_r130d_original_point_quadratic_pffdtd import PPW,PIN,file_hash
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs

SCHEMA="htdt.r130d.continuum-retarded-point-green-reference-plan-1"


def validate_plan(p):
    orig=p["prospective_original_frozen"]
    ref=p["retarded_reference"]
    a=p["acceptance"];lim=p["limits"]
    if (p.get("schema_version")!=SCHEMA or p["issue"]!=938 or p["pr"]!=1055
        or orig["upstream_sha"]!=PIN or orig["ppw"]!=list(PPW)
        or orig["point_source_xyz_m"]!=[1.5,2,2]
        or orig["point_receiver_xyz_m"]!=[2.5,2,2]
        or not orig["source_original_input_sum_in_sigs"]
        or not orig["source_original_8node_SHA"]
        or not orig["receiver_original_8node_SHA"]
        or not orig["native_Ts_Nt_h_original"]
        or not orig["original_q0_0_1_later_0"]
        or orig["full_actual_record_s"]!=.25 or orig["freq_hz"]!=[40,80]
        or orig["sound_speed_m_s"]!=C or orig["rho_kg_m3"]!=RHO
        or [orig[k] for k in ("original_full_signed_complex_gate",
                               "original_full_magnitude_gate",
                               "original_full_phase_deg_gate")]!=[.2,.25,15]
        or orig["original_original_status"]!="SELF_CONVERGENCE_FAILED"
        or not ref["no_amplitude_fit_or_q0_change"]
        or not ref["no_modal_cutoff_or_frequency_bin_mask"]
        or not all(a.values())
        or lim!={"max_five_grid_cases":5,"max_image_planes":6,
                 "no_native_pffdtd_reruns":0,"no_github_actions_runs":0,
                 "preserve_scratch":True}
        or p["release"]["original_PFFDTD"]!="SELF_CONVERGENCE_FAILED"
        or p["release"]["independent_physics"]!="NOT_VALIDATED"
        or p["release"]["product"]!="NO_GO"):
        raise ValueError("prospective retarded Green true original native q0 plan modified")
    return p


def json_conv(v):
    if isinstance(v,dict):
        return {k:json_conv(x) for k,x in v.items()}
    if isinstance(v,complex):
        return [float(v.real),float(v.imag)]
    if isinstance(v,np.ndarray):
        if np.iscomplexobj(v):
            return pairs(v)
        return v.tolist()
    if isinstance(v,(np.floating,np.integer,np.bool_)):
        return v.item()
    if isinstance(v,(list,tuple)):
        return [json_conv(z) for z in v]
    return v


def original_case(p,ppw,folder,previous,modal,point_images):
    raw=folder/"comms_out.h5"
    geom=folder/"vox_out.h5"
    output=folder/"sim_outs.h5"
    if (file_hash(raw)!=previous["original_native_comm_sha256"]
        or file_hash(geom)!=previous["original_solver_geometry_sha256"]
        or file_hash(output)!=modal["original_native_sim_output_SHA256"]):
        raise ValueError("SHA-pinned ORIGINAL physical native original q0 source/receiver or full wave modified")
    with h5py.File(geom,"r") as h:
        axes=[np.asarray(h[k][...],dtype=float) for k in ("xv","yv","zv")]
    dims=tuple(len(a) for a in axes)
    with h5py.File(raw,"r") as h:
        si=np.asarray(h["in_ixyz"][:],dtype=np.int64)
        ri=np.asarray(h["out_ixyz"][:],dtype=np.int64)
        original_sig=np.asarray(h["in_sigs"][:],dtype=float)
        original_receiver_w=np.asarray(h["out_alpha"][:],dtype=float).ravel()
        nt=int(h["Nt"][()])
        if (si.shape!=(8,) or ri.shape!=(8,)
            or original_receiver_w.shape!=(8,)
            or original_sig.shape!=(8,nt)
            or np.any(original_sig[:,1:]!=0)
            or int(h["diff"][()])!=0
            or abs(float(sum(original_receiver_w))-1)>1e-12):
            raise ValueError("ORIGINAL 8 point input q0 and receiver native HDF5 changed")
        original_source_kick=float(original_sig[:,0].sum())
        sw=original_sig[:,0]/original_source_kick
    with h5py.File(folder/"sim_consts.h5","r") as h:
        dt=float(h["Ts"][()]);h_m=float(h["h"][()])
        speed=float(h["c"][()]);l2=float(h["l2"][()])
    if (abs(speed-C)>1e-12 or abs(h_m-C/(100*ppw))>1e-12
        or abs(dt-modal["original_native_time_step_s"])>1e-12
        or nt!=modal["original_record_samples"]
        or abs(original_source_kick-l2/h_m)>1e-10):
        raise ValueError("original physical q0 native time or strength changed")
    xyz_source=np.column_stack(tuple(axes[i][k] for i,k in enumerate(
        np.unravel_index(si,dims))))
    xyz_receiver=np.column_stack(tuple(axes[i][k] for i,k in enumerate(
        np.unravel_index(ri,dims))))
    weighted=original_8node_retarded_signed(
        xyz_source,sw,xyz_receiver,original_receiver_w)
    source_moment_error=float(np.max(abs(
        weighted["original_native_8node_source_first_moment_m"]-
        np.array([1.5,2,2]))))
    receiver_moment_error=float(np.max(abs(
        weighted["original_native_8node_receiver_first_moment_m"]-
        np.array([2.5,2,2]))))
    if max(source_moment_error,receiver_moment_error)>3e-10:
        raise ValueError("original unmodified HDF5 point interpolation failed affine moment physical coordinates")
    reference=point_images["direct_G_signed"]
    observed=weighted["all_64_original_pairs_retarded_free_space_G_signed"]
    geomrel=float(np.linalg.norm(observed-reference)/np.linalg.norm(reference))
    geommax=float(np.max(np.abs(observed-reference)/np.abs(reference)))
    geom_phase_deg=np.degrees(np.angle(observed/reference))
    # Original *full actual 250ms room* signed transfer is reported
    # unmodified, including ALL room reflections; it is NOT comparable to
    # a 1-bounce Green expression as a physically identical full-room wave.
    full=unpairs(previous["unmodified_original_transfer_pa_per_m3_s"])
    prior=unpairs(modal["exact_original_saved_true_PFFDTD_q0_signed_40_80"])
    if np.linalg.norm(full-prior)/max(np.linalg.norm(full),1e-14)>2e-6:
        raise ValueError("unchanged real native actual q0 full wave and known allmode origin diverged")
    first_8node={}
    for key,r in weighted["original_8node_first_bounce_per_true_wall"].items():
        first_8node[key]={**r,
            "physically_supported_first_bounce_G_signed":pairs(
                r["physically_supported_first_bounce_G_signed"])}
    # The common -i omega rho factor merely gives the 3D continuum
    # pressure derivative for a unit causal volume-velocity impulse;
    # it is NOT fitted to original native room amplitude.
    omega=2*np.pi*np.array(FREQUENCIES_HZ,dtype=float)
    first_pressure= -1j*omega*RHO*reference
    old_pressure= -1j*omega*RHO*observed
    return {
        "ppw":ppw,
        "original_native_original_source_comms_HDF5_SHA256":file_hash(raw),
        "original_native_original_room_voxel_HDF5_SHA256":file_hash(geom),
        "original_native_original_250ms_real_wave_HDF5_SHA256":file_hash(output),
        "native_original_h_m":h_m,"native_original_Ts_s":dt,"native_original_Nt":nt,
        "original_native_source_kick_l2_over_h":original_source_kick,
        "original_native_source_HDF5_exact_eight_flat_indices":si.tolist(),
        "original_native_receiver_HDF5_exact_eight_flat_indices":ri.tolist(),
        "original_native_source_HDF5_normalized_eight_coefficients":sw.tolist(),
        "original_native_receiver_HDF5_eight_coefficients":original_receiver_w.tolist(),
        "original_native_eight_source_xyz_m":xyz_source.tolist(),
        "original_native_eight_receiver_xyz_m":xyz_receiver.tolist(),
        "original_source_first_moment_error_m":source_moment_error,
        "original_receiver_first_moment_error_m":receiver_moment_error,
        "exact_64_original_native_pairs_analytic_retarded_G_40_80":pairs(observed),
        "physical_continuum_single_true_Dirac_point_retarded_G_40_80":pairs(reference),
        "original_native_eightnode_analytic_retarded_vs_physical_point_complex_relative":geomrel,
        "original_native_eightnode_analytic_retarded_vs_physical_point_per_bin_relative":[
            float(v) for v in np.abs(observed-reference)/np.abs(reference)],
        "original_native_eightnode_analytic_retarded_vs_physical_point_max_relative":geommax,
        "original_native_eightnode_analytic_retarded_vs_physical_point_signed_phase_deg":
            geom_phase_deg.tolist(),
        "original_native_eightnode_analytic_direct_pressure_signed_40_80_without_amplitude_fitting":pairs(old_pressure),
        "physical_point_analytic_direct_pressure_signed_40_80_without_amplitude_fitting":pairs(first_pressure),
        "exact_original_full_250ms_PFFDTD_signed_room_P_over_Q_40_80":pairs(full),
        "real_original_allmode_native_full250ms_signed_P_over_Q_40_80":pairs(prior),
        "original_eightnode_analytic_individual_finite_wall_first_reflections":first_8node,
        "original_physical_8node_first_moments_unchanged":True,
        "analytic_retarded_direct_only_is_not_full_250ms_room_response":True,
        "full_original_native_q0_and_all_higher_modes_untouched":True}


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    a=ap.parse_args()
    raw=a.plan.read_bytes()
    p=validate_plan(json.loads(raw.decode("utf-8")))
    orig=p["prospective_original_frozen"]
    prior=json.loads((ROOT/orig["raw_native_HDF5_evidence"]).read_text(encoding="utf-8"))
    modal=json.loads((ROOT/orig["native_actual_full_modes_evidence"]).read_text(encoding="utf-8"))
    rows={z["ppw"]:z for z in prior["actual_native_wave_cases"]}
    allmodal={z["ppw"]:z for z in modal["actual_original_unmodified_all_mode_native_cases"]}
    if (set(rows)!=set(PPW) or set(allmodal)!=set(PPW)
        or modal["canonical_original_point_q0"]!="SELF_CONVERGENCE_FAILED"):
        raise ValueError("real original 5 grid full signed source q0 evidence unavailable")
    dirs={}
    for f in a.original_sims_root.rglob("comms_out.h5"):
        h=file_hash(f)
        found=[j for j in PPW if rows[j]["original_native_comm_sha256"]==h]
        if len(found)==1:
            if found[0] in dirs: raise ValueError("ambiguous original HDF5 source SHA")
            dirs[found[0]]=f.parent
    if set(dirs)!=set(PPW):
        raise ValueError("all five true original native q0 HDF5 files not found")
    physical=physical_point_first_reflections()
    # The physical finite-face specular geometry must be well defined
    # before comparing original signed 40/80 amplitudes.
    result={"schema_version":"htdt.r130d.continuum-retarded-point-green-reference-evidence-1",
        "preregistered_plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":p,
        "original_PFFDTD_q0":"SELF_CONVERGENCE_FAILED",
        "independent_physical_validation":"NOT_VALIDATED","product":"NO_GO",
        "new_native_PFFDTD_wave_runs":0,"new_GitHub_Actions_runs":0,
        "unit_true_point_free_space_and_six_single_wall_image_geometry":json_conv(physical),
        "actual_original_8node_HDF5_5grid_analytic_direct_point_Green_cases":[],
        "native_original_5grid_4pair_scores_and_analytic_direct_8node_diagnostic":[]}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    for ppw in PPW:
        try:case=original_case(p,ppw,dirs[ppw],rows[ppw],allmodal[ppw],physical)
        except Exception as exc:
            result["actual_original_8node_HDF5_5grid_analytic_direct_point_Green_cases"].append({
                "ppw":ppw,"status":"ORIGINAL_POINT_GREEN_REFERENCE_NOT_COMPLETED",
                "exception":type(exc).__name__,"error":str(exc)})
            a.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        result["actual_original_8node_HDF5_5grid_analytic_direct_point_Green_cases"].append(case)
        a.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("ACTUAL_ORIGINAL_8NODE_CONTINUUM_RETARDED_GREEN",ppw,
              "native_source_moment_err",case["original_source_first_moment_error_m"],
              "8node_vs_true_phys_Dirac_relative",case[
                  "original_native_eightnode_analytic_retarded_vs_physical_point_complex_relative"],
              "signed_true_G",case["physical_continuum_single_true_Dirac_point_retarded_G_40_80"],
              flush=True)
    cases=result["actual_original_8node_HDF5_5grid_analytic_direct_point_Green_cases"]
    for co,fi in zip(cases,cases[1:]):
        old=compare_complex_transfer(
            reference=fi["exact_original_full_250ms_PFFDTD_signed_room_P_over_Q_40_80"],
            candidate=co["exact_original_full_250ms_PFFDTD_signed_room_P_over_Q_40_80"],
            frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
        # Independent physical free-space geometric interpolation comparison,
        # NOT acceptable for native room self convergence since reflections
        # and original mode spectrum are physically missing.
        geom=compare_complex_transfer(
            reference=fi["exact_64_original_native_pairs_analytic_retarded_G_40_80"],
            candidate=co["exact_64_original_native_pairs_analytic_retarded_G_40_80"],
            frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
        result["native_original_5grid_4pair_scores_and_analytic_direct_8node_diagnostic"].append({
            "coarse_ppw":co["ppw"],"fine_ppw":fi["ppw"],
            "original_true_FULL_250ms_room_q0_signed_original_frozen_scores":old,
            "original_true_room_original_all_3_gates_pass":bool(
                old["complex_rms_relative"]<=.2 and
                old["magnitude_max_relative"]<=.25 and
                old["phase_max_deg"]<=15),
            "independent_analytic_free_space_eightnode_geometric_only_scores_NOT_250ms_room":geom,
            "physically_full_room_true_original_green_not_known_from_single_bounce":True})
    result["original_real_room_PFFDTD_all_four_pairs_three_gates_pass"]=all(
        r["original_true_room_original_all_3_gates_pass"]
        for r in result["native_original_5grid_4pair_scores_and_analytic_direct_8node_diagnostic"])
    result["analytic_single_bounce_not_used_for_native_fullroom_convergence_acceptance"]=True
    result["all_existing_true_original_q0_unmasked_high_modes_retained"]=True
    a.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("ACTUAL_ORIGINAL_250MS_ROOM_VS_INDEPENDENT_FREEFIELD_ORIGINAL_8NODE",[
        (r["coarse_ppw"],r["fine_ppw"],
         round(r["original_true_FULL_250ms_room_q0_signed_original_frozen_scores"]["complex_rms_relative"],6),
         round(r["independent_analytic_free_space_eightnode_geometric_only_scores_NOT_250ms_room"]["complex_rms_relative"],9))
        for r in result["native_original_5grid_4pair_scores_and_analytic_direct_8node_diagnostic"]],flush=True)
    print("TRUE_DIRECT_POINT_FIRST_REFLECTION_TIME",physical["direct_original_physical_arrival_s"],
          physical["first_supported_reflection_s"],flush=True)
    print("CONTINUUM_GREEN_ORIGINAL_AUTHORITY",result["original_PFFDTD_q0"],"NO_GO",flush=True)

if __name__=="__main__": main()
