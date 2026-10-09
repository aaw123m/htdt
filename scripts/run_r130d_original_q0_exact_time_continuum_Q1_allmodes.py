#!/usr/bin/env python3
"""Real original SHA-pinned 5-grid q0 exact-time physical Neumann Q1 benchmark.

Compares three predeclared mass families with identical original native
eight-node physical point source/receiver and original full 250ms samples.
An instantaneous δ(t) with native one-sample dt integral is an explicitly
new physical-time propagator, NOT original upstream PFFDTD or its Newmark.
No amplitude fit, no mode cuts or retuned original full-record gates.
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

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))

from htdt.r130d_conforming_roof_p1_fem import build_original_native_conforming_roof_p1
from htdt.r130d_conforming_roof_p1_consistent_mass import (
    consistent_physical_P1_tensored_operators,
    generalized_symmetric_P1_consistent_modes,
    true_full_3d_consistent_P1_MK)
from htdt.r130d_native_cut_roof_Q1_galerkin import (
    cut_roof_native_original_Q1_galerkin_yz,
    original_eightnode_native_HDF5_Q1_source_receiver,
    generalized_native_original_Q1_full_physical_neumann_modes)
from htdt.r130d_native_cut_Q1_positive_lumped import (
    physical_positive_row_sum_lump,true_full_3d_lumped_roof_MK)
from htdt.r130d_oblique_roof_theory_blended_q1 import (
    half_Q1_true_roof_mass,full_theory_half_Q1_mass_and_true_roof_stiffness)
from htdt.r130d_exact_semidiscrete_causal_q0 import (
    exact_continuous_modal_delta_impulse_signed_original_250ms,
    exact_continuous_modal_delta_impulse_roof_weak)
from htdt.r130d_causal_first_roof_echo import (
    ROOF_WIDTHS_S,analytic_native_64point_physical_roof_echo_weak)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_point_quadratic_pffdtd import PPW,PIN,file_hash
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs

SCHEMA="htdt.r130d.original-q0-allmode-exact-semidescrete-impulse-time-plan-1"
ARMS=("fully_consistent_Q1","positive_row_sum_Q1","theory_half_Q1")
SOURCE_KEYS={
  "fully_consistent_Q1":"new_native_Q1_true_roof_q0_full_original_250ms_signed_40_80",
  "positive_row_sum_Q1":"new_true_roof_Q1_positive_lumped_mass_allmode_full_250ms_original_signed_40_80",
  "theory_half_Q1":"new_true_roof_Q1_half_theory_mass_allmode_full_250ms_original_signed_40_80"}


def validate_plan(p):
    o=p["original_unchanged"];n=p["new_explicit_nonoriginal_physical_model"]
    c=p["comparisons"];lim=p["limits"]
    if (p.get("schema_version")!=SCHEMA or p["issue"]!=938 or p["pr"]!=1055
        or o["pffdtd_upstream_SHA"]!=PIN or o["ppw"]!=list(PPW)
        or o["source_xyz"]!=[1.5,2,2] or o["receiver_xyz"]!=[2.5,2,2]
        or not o["original_native_8_input_8_output_HDF5_SHA_flatindices_weights_unchanged"]
        or not o["native_h_Ts_Nt_each_grid_unchanged"]
        or o["original_full_wave_record_s"]!=.25
        or o["original_signed_40_80_bins"]!=[40,80]
        or o["sound_speed_m_s"]!=343.2 or o["density_kg_m3"]!=1.2
        or not o["original_pressure_forward_center_backward_exact_native"]
        or [o["original_full_complex_rms_gate"],
            o["original_full_magnitude_max_gate"],
            o["original_full_phase_max_deg"]]!=[.2,.25,15]
        or not o["original_all_adjacent_pairs_three_gates_and_monotone"]
        or n["all_three_mass_arms"]!=list(ARMS)
        or not n["mass_families_fixed_before_scores"]
        or not n["true_cut_Q1_weak_Neumann_K_and_all_original_active_Q1_sliver_nodes_unchanged"]
        or not n["do_not_tune_eigenfrequencies_source_amplitude_or_phase"]
        or not n["no_high_mode_cutoff_or_spatial_operator_smoothing"]
        or not all(c.values())
        or lim!={"max_PPWs":5,"max_experimental_mass_families":3,
                 "max_total_3D_modes_per_arm":180000,
                 "max_cross_section_Q1_modes":3300,
                 "upstream_PFFDTD_reruns":0,"github_Actions_manual_runs":0,
                 "preserve_scratch":True}
        or p["authority"]["canonical_pffdtd_q0"]!="SELF_CONVERGENCE_FAILED"
        or p["authority"]["independent_BRAS_MFEM_qualified"]!="NOT_VALIDATED"
        or p["authority"]["product"]!="NO_GO"):
        raise ValueError("frozen original q0 exact-continuum-time delta 3-arm Q1 preregistration drift")
    return p


def spectral_arm(name,mx,my,kx,ky,sx,sy,rx,ry,dt,nt,
                 source_nodes,source_weights,receiver_nodes,receiver_weights,
                 archived_signed,limits):
    tic=time.perf_counter()
    if name=="fully_consistent_Q1":
        qx,qyz=mx,my
    elif name=="positive_row_sum_Q1":
        lump=physical_positive_row_sum_lump(mx,my,kx,ky)
        qx,qyz=lump.Mx,lump.Myz
    elif name=="theory_half_Q1":
        half=half_Q1_true_roof_mass(mx,my,kx,ky)
        qx,qyz=half.Mx,half.Myz
    else:raise ValueError("unexpected fitted mass family")
    vx,ux,xproof=generalized_symmetric_P1_consistent_modes(qx,kx)
    vy,uy,yproof=generalized_native_original_Q1_full_physical_neumann_modes(qyz,ky)
    lam=(vx[:,None]+vy[None,:]).ravel()
    if (len(lam)>limits["max_total_3D_modes_per_arm"]
        or len(vy)>limits["max_cross_section_Q1_modes"]):
        raise ValueError("all original true 3D Q1 physical modes exceed preregistered bound")
    projection=np.outer((sx@ux)*(rx@ux),(sy@uy)*(ry@uy)).ravel()
    if len(projection)!=len(lam) or not np.isfinite(projection).all():
        raise ValueError("unchanged actual original 8-node point source/receiver mode coupling invalid")
    whole=exact_continuous_modal_delta_impulse_signed_original_250ms(
        lam,projection,dt,nt)
    summed=whole.sum(axis=1)
    localweak=exact_continuous_modal_delta_impulse_roof_weak(
        lam,projection,dt,nt)
    predicted=[]
    for w,q in zip(ROOF_WIDTHS_S,localweak):
        analytic=analytic_native_64point_physical_roof_echo_weak(
            source_nodes,source_weights,receiver_nodes,receiver_weights,
            width_s=w)
        image=analytic["original_64pair_finite_roof_single_bounce_signed_weak_analytic"]
        actual=q["true_original_native_exact_time_allmode_roof_window_weak"]
        predicted.append({
            "preregistered_witness_width_s":w,
            "all_modes_exact_time_new_native_wave":q,
            "frozen_exact_true_64native_node_finite_roof_image":analytic,
            "exact_time_new_solver_total_roof_window_over_single_image_signed_ratio":
                float(actual/image),
            "exact_time_new_solver_total_roof_window_vs_single_image_relative":
                float(abs(actual/image-1)),
            "this_is_NOT_pure_reflection_coefficient_or_original_full250ms_acceptance":True})
    C=343.2
    M=np.kron(qx.diagonal(),qyz.diagonal()) if name=="positive_row_sum_Q1" else None
    # Validate exact physical mass totals and Neumann K on component level;
    # the full tensor operator is unchanged in physical geometry and
    # has physical mass=4x14=56m3, and nonzero slivers are all retained.
    if (abs(float(qx.sum())-4)>2e-9 or
        abs(float(qyz.sum())-14)>2e-8 or
        np.min(qx.diagonal())<=0 or np.min(qyz.diagonal())<=0):
        raise ValueError("true roof Q1 exact-time arm mass positive/56m3 physical constraint failed")
    # Manufactured physical affine field u=x+z: y/z q1 wet energy
    # must remain unchanged by this temporal operator.
    original_value=unpairs(archived_signed)
    return {
      "experimental_mass_family":name,
      "all_original_physical_Q1_cut_3D_modes_retained":int(len(lam)),
      "all_true_positive_roof_slivers_kept":True,
      "total_physical_mass_3D_m3":float(qx.sum()*qyz.sum()),
      "x_M_smallest_diagonal":float(np.min(qx.diagonal())),
      "yz_M_smallest_diagonal":float(np.min(qyz.diagonal())),
      "maximum_semidiscrete_physical_eigenfrequency_hz":float(
          np.sqrt(np.max(lam))/(2*np.pi)),
      "num_modes_above_native_original_pressure_Nyquist_included":int(
          np.count_nonzero(np.sqrt(np.maximum(lam,0))*dt>np.pi)),
      "x_generalized_eigen_full_SPD_check":xproof,
      "yz_generalized_eigen_full_SPD_check":yproof,
      "new_exact_time_original_delta_native_sampled_original_250ms_signed_40_80":
          pairs(summed),
      "old_preobserved_original_beta_quarter_Newmark_Q1_250ms_signed_40_80":
          pairs(original_value),
      "new_exact_time_first_original_roof_reflection_3_declared_physical_weak_widths":
          predicted,
      "original_spatial_neumann_operator_not_fitted":True,
      "all_original_true_physical_3D_modes_sampled_without_spectral_cut":True,
      "exact_time_wave_returned_is_EXPERIMENT_not_original_upstream":True,
      "elapsed_seconds":float(time.perf_counter()-tic)}


def one_case(ppw,folder,real,controls,p):
    t0=time.perf_counter()
    hdf5_expected={x:real[x] for x in (
        "original_native_comm_sha256","original_solver_geometry_sha256")}
    if (file_hash(folder/"comms_out.h5")!=hdf5_expected["original_native_comm_sha256"]
        or file_hash(folder/"vox_out.h5")!=hdf5_expected["original_solver_geometry_sha256"]
        or file_hash(folder/"sim_outs.h5")!=controls["roof"][
            "original_true_native_entire_real_q0_HDF5_SHA256"]):
        raise ValueError("true original real native HDF5 q0 8node physical SHA changed")
    with h5py.File(folder/"vox_out.h5","r") as f:
        axes=[np.asarray(f[z][:],dtype=float) for z in ("xv","yv","zv")]
    dims=tuple(len(a) for a in axes)
    with h5py.File(folder/"comms_out.h5","r") as f:
        si=np.asarray(f["in_ixyz"][:],dtype=np.int64)
        ri=np.asarray(f["out_ixyz"][:],dtype=np.int64)
        sig=np.asarray(f["in_sigs"][:],dtype=float)
        rw=np.asarray(f["out_alpha"][:],dtype=float).ravel()
        nt=int(f["Nt"][()])
        if (si.shape!=(8,) or ri.shape!=(8,) or rw.shape!=(8,)
            or sig.shape!=(8,nt) or np.any(sig[:,1:]!=0)
            or int(f["diff"][()])!=0 or abs(rw.sum()-1)>1e-12):
            raise ValueError("unchanged original native 8-node sample 0 q0 HDF5 invalid")
        impulse=float(np.sum(sig[:,0]))
        sw=np.asarray(sig[:,0]/impulse,dtype=float)
    with h5py.File(folder/"sim_consts.h5","r") as f:
        dt=float(f["Ts"][()]);h=float(f["h"][()])
        c=float(f["c"][()]);l2=float(f["l2"][()])
    if (abs(c-343.2)>1e-12 or abs(h-c/(100*ppw))>1e-12
        or abs(impulse-l2/h)>1e-10
        or abs(dt-controls["consistent"]["original_native_Ts_s"])>1e-12
        or nt!=controls["consistent"]["original_native_full_Nt"]):
        raise ValueError("original native physics q0 time grid/strength changed")
    fem=build_original_native_conforming_roof_p1(
        axes,max_yz_nodes=3000,max_3d_nodes=180000)
    mx,_,kx,_=consistent_physical_P1_tensored_operators(fem)
    wet=cut_roof_native_original_Q1_galerkin_yz(
        axes[1],axes[2],max_active_yz_nodes=3300)
    if (len(fem.x_positions_m)*wet.native_yz_modes
        !=controls["consistent"]["real_original_native_3D_full_true_Q1_modes"]):
        raise ValueError("frozen original physical Q1 native cut room sliver modes changed")
    sx,sy,scheck=original_eightnode_native_HDF5_Q1_source_receiver(
        dims,fem.x_native_map,wet,si,sw,len(fem.x_positions_m))
    rx,ry,rcheck=original_eightnode_native_HDF5_Q1_source_receiver(
        dims,fem.x_native_map,wet,ri,rw,len(fem.x_positions_m))
    if max(scheck,rcheck)>1e-12:
        raise ValueError("true original 8 source and 8 receiver nodes altered")
    srcpos=np.column_stack([
        axes[k][ix] for k,ix in enumerate(np.unravel_index(si,dims))])
    recvpos=np.column_stack([
        axes[k][ix] for k,ix in enumerate(np.unravel_index(ri,dims))])
    if (not np.allclose(sw@srcpos,[1.5,2,2],rtol=0,atol=3e-10)
        or not np.allclose(rw@recvpos,[2.5,2,2],rtol=0,atol=3e-10)):
        raise ValueError("original fixed true point 8node native position shift")
    arms={}
    for mass in ARMS:
        old=controls[mass][SOURCE_KEYS[mass]]
        arms[mass]=spectral_arm(mass,mx,wet.mass,kx,wet.stiffness,
            sx,sy,rx,ry,dt,nt,srcpos,sw,recvpos,rw,
            old,p["limits"])
        print("EXACT_TIME_Q0_PHYSICAL_Q1_GRID",ppw,mass,
              "mode_count",arms[mass]["all_original_physical_Q1_cut_3D_modes_retained"],
              "new_signed",arms[mass][
                  "new_exact_time_original_delta_native_sampled_original_250ms_signed_40_80"],
              "roof_window_ratios",[
                  round(q["exact_time_new_solver_total_roof_window_over_single_image_signed_ratio"],6)
                  for q in arms[mass]["new_exact_time_first_original_roof_reflection_3_declared_physical_weak_widths"]],
              flush=True)
    return {
      "ppw":ppw,
      "original_unmodified_PFFDTD_real_comms_SHA256":file_hash(folder/"comms_out.h5"),
      "original_unmodified_PFFDTD_real_voxel_SHA256":file_hash(folder/"vox_out.h5"),
      "original_unmodified_PFFDTD_real_wave_SHA256":file_hash(folder/"sim_outs.h5"),
      "native_original_h_m":h,"native_original_dt_s":dt,"native_original_Nt":nt,
      "original_q0_8source_sample_sum_exact_original":impulse,
      "original_native_true_8source_receiver_projection_error":[scheck,rcheck],
      "physical_true_14m2_cut_Q1_yz_active_all_original_nodes":wet.native_yz_modes,
      "physical_true_14m2_original_roof_area":wet.physical_area_m2,
      "physical_roof_original_outside_native_wet_support_nodes_kept":
          wet.occupied_cartesian_original_exterior_nodes,
      "all_predeclared_exact_time_mass_arms":arms,
      "original_upstream_entire_real_native_wave_250ms_signed_40_80":
          pairs(unpairs(real["unmodified_original_transfer_pa_per_m3_s"])),
      "no_original_PFFDTD_sample_or_acceptance_changed":True,
      "elapsed_all_three_arms_s":float(time.perf_counter()-t0)}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--plan",type=Path,required=True)
    parser.add_argument("--original-sims-root",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    raw=args.plan.read_bytes()
    p=validate_plan(json.loads(raw.decode("utf-8")))
    obs=p["prospective_observed_controls"]
    oldraw=json.loads((ROOT/obs["raw_pffdtd"]).read_text(encoding="utf-8"))
    consistent=json.loads((ROOT/obs["true_exact_cut_Q1_consistent"]).read_text(encoding="utf-8"))
    lumped=json.loads((ROOT/obs["true_exact_cut_Q1_row_lump"]).read_text(encoding="utf-8"))
    half=json.loads((ROOT/obs["true_exact_cut_Q1_half_mass"]).read_text(encoding="utf-8"))
    first=json.loads((ROOT/obs["original_first_roof_echo"]).read_text(encoding="utf-8"))
    rawcases={z["ppw"]:z for z in oldraw["actual_native_wave_cases"]}
    oldcases={
        "fully_consistent_Q1":{z["ppw"]:z for z in
            consistent["actual_native_original_point_q0_Q1_cutroof_full_modes_cases"]},
        "positive_row_sum_Q1":{z["ppw"]:z for z in
            lumped["actual_all_five_original_native_q0_positive_cut_Q1_lump_cases"]},
        "theory_half_Q1":{z["ppw"]:z for z in
            half["actual_all_five_original_native_q0_theory_half_cut_Q1_cases"]}}
    firstcases={z["ppw"]:z for z in
        first["actual_original_q0_five_grid_real_first_roof_echo_weak_cases"]}
    if (set(rawcases)!=set(PPW) or set(firstcases)!=set(PPW)
        or any(set(v)!=set(PPW) for v in oldcases.values())):
        raise ValueError("all original real SHA and 3 full-wave Q1 controls required")
    folders={}
    for f in args.original_sims_root.rglob("comms_out.h5"):
        sha=file_hash(f)
        found=[ppw for ppw in PPW
               if rawcases[ppw]["original_native_comm_sha256"]==sha]
        if len(found)==1:
            if found[0] in folders:raise ValueError("ambiguous original native HDF5")
            folders[found[0]]=f.parent
    if set(folders)!=set(PPW):
        raise ValueError("all original HDF5 real native 8node q0 waves missing")
    evidence={
      "schema_version":"htdt.r130d.original-q0-exact-continuum-time-all-Q1-3arm-5grid-evidence-1",
      "preregistered_plan_SHA256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
      "preregistered_plan":p,
      "canonical_original_PFFDTD_q0":"SELF_CONVERGENCE_FAILED",
      "independent_physical":"NOT_VALIDATED","product":"NO_GO",
      "no_new_native_PFFDTD_reruns_or_manual_Actions":True,
      "true_new_exact_time_on_actual_original_SHA_HDF5_five_PPWs":[],
      "original_250ms_full_four_adjacent_three_gates_all_four_arms":[]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    for ppw in PPW:
        controls={name:oldcases[name][ppw] for name in ARMS}
        controls["roof"]=firstcases[ppw]
        controls["consistent"]=oldcases["fully_consistent_Q1"][ppw]
        try:z=one_case(ppw,folders[ppw],rawcases[ppw],controls,p)
        except Exception as e:
            evidence["true_new_exact_time_on_actual_original_SHA_HDF5_five_PPWs"].append({
                "ppw":ppw,"status":"FAIL_CLOSED_EXACT_TIME_PHYSICAL_Q1_INCOMPLETE",
                "error":str(e),"type":type(e).__name__})
            args.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        evidence["true_new_exact_time_on_actual_original_SHA_HDF5_five_PPWs"].append(z)
        args.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    rows=evidence["true_new_exact_time_on_actual_original_SHA_HDF5_five_PPWs"]
    for coarse,fine in zip(rows,rows[1:]):
        arms={}
        for arm in ARMS:
            for timecase,label in [
                ("new_exact_time_original_delta_native_sampled_original_250ms_signed_40_80",
                 "exact_continuum_time"),
                ("old_preobserved_original_beta_quarter_Newmark_Q1_250ms_signed_40_80",
                 "preobserved_Newmark")]:
                sco=coarse["all_predeclared_exact_time_mass_arms"][arm][timecase]
                sfi=fine["all_predeclared_exact_time_mass_arms"][arm][timecase]
                metric=compare_complex_transfer(
                    reference=sfi,candidate=sco,frequency_hz=[40,80],
                    magnitude_mask_relative_db=-50).model_dump(mode="json")
                arms[arm+"/"+label]={
                    "original_signed_40_80_full_unfiltered_250ms_frozen_three_metrics":metric,
                    "original_all_three_gates_PASS":bool(
                        metric["complex_rms_relative"]<=.2 and
                        metric["magnitude_max_relative"]<=.25 and
                        metric["phase_max_deg"]<=15),
                    "coarse_minus_fine_real_imag_signed":pairs(unpairs(sco)-unpairs(sfi))}
        src_co=coarse["original_upstream_entire_real_native_wave_250ms_signed_40_80"]
        src_fi=fine["original_upstream_entire_real_native_wave_250ms_signed_40_80"]
        m=compare_complex_transfer(reference=src_fi,candidate=src_co,
            frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
        arms["original_upstream_true_PFFDTD"]={
          "original_signed_40_80_full_unfiltered_250ms_frozen_three_metrics":m,
          "original_all_three_gates_PASS":bool(
              m["complex_rms_relative"]<=.2 and
              m["magnitude_max_relative"]<=.25 and m["phase_max_deg"]<=15),
          "coarse_minus_fine_real_imag_signed":pairs(unpairs(src_co)-unpairs(src_fi))}
        evidence["original_250ms_full_four_adjacent_three_gates_all_four_arms"].append({
            "coarse_ppw":coarse["ppw"],"fine_ppw":fine["ppw"],
            "control_and_exact_time":arms})
    verdict={}
    for key in evidence["original_250ms_full_four_adjacent_three_gates_all_four_arms"][0]["control_and_exact_time"]:
        allrows=[z["control_and_exact_time"][key] for z in evidence[
            "original_250ms_full_four_adjacent_three_gates_all_four_arms"]]
        scorekeys=("complex_rms_relative","magnitude_max_relative","phase_max_deg")
        verdict[key]={
          "all_four_pairs_pass_every_original_three_gate":all(
              r["original_all_three_gates_PASS"] for r in allrows),
          "strict_three_metric_monotone_convergence":all(all(
              allrows[j]["original_signed_40_80_full_unfiltered_250ms_frozen_three_metrics"][v]<
              allrows[j-1]["original_signed_40_80_full_unfiltered_250ms_frozen_three_metrics"][v]
              for v in scorekeys) for j in range(1,4)),
          "original_upstream_and_independent_BRAS_MFEM_NOT_qualified":True}
    evidence["all_cases_verdict_do_not_promote_cherrypicked_gate"]=verdict
    evidence["all_high_modes_including_above_original_native_Nyquist_accounted_for"]=True
    evidence["release_original_upstream_r130d_still_FAILED_and_NO_GO"]=True
    args.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("EXACT_CONTINUOUS_TIME_WAVE_3ARMS_FULL250MS_Q0_ALL4PAIR_METRICS",[
      (a["coarse_ppw"],a["fine_ppw"],{
        key:[round(a["control_and_exact_time"][key][
          "original_signed_40_80_full_unfiltered_250ms_frozen_three_metrics"][j],6)
          for j in ("complex_rms_relative","magnitude_max_relative","phase_max_deg")]
        for key in a["control_and_exact_time"]})
      for a in evidence["original_250ms_full_four_adjacent_three_gates_all_four_arms"]],
      flush=True)
    print("EXACT_CONTINUOUS_TIME_WAVE_Q0_RELEASE_VERDICT",verdict,flush=True)


if __name__=="__main__":main()
