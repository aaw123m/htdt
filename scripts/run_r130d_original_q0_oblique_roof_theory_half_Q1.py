#!/usr/bin/env python3
"""Actual original q0 8node full native 250ms Q1 row-sum lump Neumann 5grid.

Experimental true cut-roof Q1 stiffness; SPD theory-half blended physical
mass, no tiny-roof-node/mode truncation. Same original HDF5 source/receiver
and q0 / native h,dt,Nt; independently compute full 250ms signed 40/80 and
the previously frozen real first-roof image causal witness on new solver.
Original upstream PFFDTD remains SELF_CONVERGENCE_FAILED regardless of result.
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
    generalized_symmetric_P1_consistent_modes)
from htdt.r130d_native_cut_roof_Q1_galerkin import (
    cut_roof_native_original_Q1_galerkin_yz,
    original_eightnode_native_HDF5_Q1_source_receiver,
    generalized_native_original_Q1_full_physical_neumann_modes)
from htdt.r130d_native_cut_Q1_positive_lumped import allmode_native_newmark_roof_causal_pressure_weak
from htdt.r130d_oblique_roof_theory_blended_q1 import (
    half_Q1_true_roof_mass,full_theory_half_Q1_mass_and_true_roof_stiffness,
    wave_uniform_1d_symbol_ratio,inspect_original_staircase_true_roof_tangential_flux)
from htdt.r130d_causal_first_roof_echo import (
    ROOF_CENTER_S,ROOF_RADIUS_S,ROOF_WIDTHS_S,
    analytic_native_64point_physical_roof_echo_weak)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_native_exact_roof_full_xy_z_modal_q0 import entire_original_finite_record_signed_modes
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs
from run_r130d_original_point_quadratic_pffdtd import PIN,PPW,file_hash

SCHEMA="htdt.r130d.original-q0-true-roof-normal-and-theory-blended-Q1-plan-1"
FROZEN=(.2,.25,15.)


def validate_plan(p):
    o=p["frozen_original"]; n=p["theory_predetermined_new_solver"]
    v=p["checks"];cap=p["limits"];normal=p["normal_diagnostic"]
    if (p.get("schema_version")!=SCHEMA or p["issue"]!=938 or p["pr"]!=1055
        or o["original_pffdtd_sha"]!=PIN or o["native_ppw"]!=list(PPW)
        or o["source_position_m"]!=[1.5,2,2]
        or o["receiver_position_m"]!=[2.5,2,2]
        or not o["actual_source_input_eight_native_flat_ixyz_and_strength_per_node_sha"]
        or not o["actual_receiver_eight_native_flat_ixyz_and_original_out_alpha_sha"]
        or not o["all_native_h_Ts_Nt_exact"]
        or o["full_unmodified_record_s"]!=.25
        or o["signed_frequencies_hz"]!=[40,80]
        or o["c_m_s"]!=343.2 or o["rho_kg_m3"]!=1.2
        or [o["complex_rms_gate"],o["magnitude_max_gate"],o["phase_max_deg_gate"]]!=[.2,.25,15]
        or not o["no_mode_truncation_source_smoothing_taper_frequency_fit_or_grid_shift"]
        or not o["all_three_gates_all_four_pairs_and_monotone_required"]
        or not normal["no_claim_stair_flux_error_alone_equals_original_full_wave_error"]
        or not n["whole_room_gate_not_weak_causal_gate"]
        or not all(v.values())
        or cap!={"max_ppw_cases":5,"max_physical_3D_modes":180000,
                 "max_native_cut_yz_modes":3300,
                 "no_new_upstream_native_PFFDTD_waves":0,
                 "no_GitHub_Actions_runs":0,"preserve_scratch":True}
        or p["authority"]["original_pffdtd_q0"]!="SELF_CONVERGENCE_FAILED"
        or p["authority"]["external_physical"]!="NOT_VALIDATED"
        or p["authority"]["product"]!="NO_GO"):
        raise ValueError("theoretical 50:50 cut Q1 mass original q0 preregistration drift")
    return p


def one_case(ppw,folder,original,consistent,prior_lump,prior_roof,p):
    tic=time.perf_counter()
    if (file_hash(folder/"comms_out.h5")!=original["original_native_comm_sha256"]
        or file_hash(folder/"vox_out.h5")!=original["original_solver_geometry_sha256"]
        or file_hash(folder/"sim_outs.h5")!=prior_roof["original_true_native_entire_real_q0_HDF5_SHA256"]):
        raise ValueError("original SHA exact HDF5 source, physical room or original real wave tampered")
    with h5py.File(folder/"vox_out.h5","r") as f:
        axes=[np.asarray(f[k][:],dtype=float) for k in ("xv","yv","zv")]
        native_boundary_ix=np.asarray(f["bn_ixyz"][:],dtype=np.int64)
        native_boundary_adjacency=np.asarray(f["adj_bn"][:],dtype=bool)
    real_stair_flux=inspect_original_staircase_true_roof_tangential_flux(
        axes[1],axes[2],native_boundary_ix,native_boundary_adjacency,
        tuple(len(a) for a in axes))
    dims=tuple(len(a) for a in axes)
    with h5py.File(folder/"comms_out.h5","r") as f:
        si=np.asarray(f["in_ixyz"][:],dtype=np.int64)
        ri=np.asarray(f["out_ixyz"][:],dtype=np.int64)
        sig=np.asarray(f["in_sigs"][:],dtype=float)
        rw=np.asarray(f["out_alpha"][:],dtype=float).ravel()
        Nt=int(f["Nt"][()])
        if (si.shape!=(8,) or ri.shape!=(8,) or rw.shape!=(8,)
            or sig.shape!=(8,Nt) or np.any(sig[:,1:]!=0)
            or not np.isfinite(sig).all() or abs(rw.sum()-1)>1e-12
            or int(f["diff"][()])!=0):
            raise ValueError("original 8 source / 8 receiver native q0 HDF5 no longer original")
        sigsum=float(sig[:,0].sum())
        sw=sig[:,0]/sigsum
    with h5py.File(folder/"sim_consts.h5","r") as f:
        dt=float(f["Ts"][()]);h_m=float(f["h"][()])
        c=float(f["c"][()]);l2=float(f["l2"][()])
    if (abs(dt-consistent["original_native_Ts_s"])>1e-12
        or Nt!=consistent["original_native_full_Nt"]
        or abs(c-343.2)>1e-12 or abs(h_m-c/(100*ppw))>1e-12
        or abs(sigsum-l2/h_m)>1e-10):
        raise ValueError("original q0 native impulse and 250ms time changed")
    f=build_original_native_conforming_roof_p1(
        axes,max_yz_nodes=3000,max_3d_nodes=180000)
    mx,my,kx,ky=consistent_physical_P1_tensored_operators(f)
    cross=cut_roof_native_original_Q1_galerkin_yz(
        axes[1],axes[2],max_active_yz_nodes=3300)
    if (len(f.x_positions_m)*cross.native_yz_modes!=consistent["real_original_native_3D_full_true_Q1_modes"]
        or cross.native_yz_modes!=consistent["real_original_native_cartesian_yz_Q1_physical_support_modes"]):
        raise ValueError("previous physical cut Q1 exact grid mode geometry not preserved")
    sx,sy,srcproof=original_eightnode_native_HDF5_Q1_source_receiver(
        dims,f.x_native_map,cross,si,sw,len(f.x_positions_m))
    rx,ry,recproof=original_eightnode_native_HDF5_Q1_source_receiver(
        dims,f.x_native_map,cross,ri,rw,len(f.x_positions_m))
    if max(srcproof,recproof)>1e-12:
        raise ValueError("native original 8node source / receiver tensor weights changed")
    lump=half_Q1_true_roof_mass(mx,cross.mass,kx,cross.stiffness)
    M,K=full_theory_half_Q1_mass_and_true_roof_stiffness(lump)
    lx,vx,xproof=generalized_symmetric_P1_consistent_modes(lump.Mx,lump.Kx)
    ly,vy,yproof=generalized_native_original_Q1_full_physical_neumann_modes(
        lump.Myz,lump.Kyz)
    lamb=(lx[:,None]+ly[None,:]).ravel()
    if len(lamb)!=M.shape[0] or len(lamb)>p["limits"]["max_physical_3D_modes"]:
        raise ValueError("positive Q1 all physical original 3D modes not retained")
    src_c=np.outer(sx@vx,sy@vy)
    recv_c=np.outer(rx@vx,ry@vy)
    modal_coupling=(src_c*recv_c).ravel()
    amp=dt*dt*c*c*modal_coupling/(1+dt*dt*lamb/4)
    signed=entire_original_finite_record_signed_modes(
        lamb,amp,dt,Nt,1.2).sum(axis=1)
    if not np.isfinite(signed).all():raise ValueError("true allmode Q1 lump original signed q0 nonfinite")
    windows=allmode_native_newmark_roof_causal_pressure_weak(lamb,amp,dt,Nt)
    srcpos=np.column_stack([axes[k][z] for k,z in enumerate(np.unravel_index(si,dims))])
    recpos=np.column_stack([axes[k][z] for k,z in enumerate(np.unravel_index(ri,dims))])
    ref=[]
    for width,w in zip(ROOF_WIDTHS_S,windows):
        image=analytic_native_64point_physical_roof_echo_weak(
            srcpos,sw,recpos,rw,width_s=width)
        v=w["original_native_q0_same_record_allmode_weak_p_over_Q"]
        y=image["original_64pair_finite_roof_single_bounce_signed_weak_analytic"]
        ref.append({"physical_width_s":width,
            "NEW_half_true_roof_original_native_allmode_window":w,
            "unchanged_true_64node_physical_roof_image_analytic":image,
            "new_native_Q1_half_weak_roof_vs_original_roof_image_signed_ratio":float(v/y),
            "new_native_Q1_half_weak_roof_vs_image_relative":float(abs(v/y-1)),
            "preobserved_original_true_PFFDTD_native_roof_weak":{
               "native_window_to_true_roof_image_signed_ratio":
                prior_roof["new_original_q0_roof_echo_fixed_weak_width_cases"][
                    list(ROOF_WIDTHS_S).index(width)][
                        "native_total_roof_window_vs_single_roof_analytic_signed_ratio"]}})
    u=(f.x_positions_m[:,None]+
       cross.physical_active_yz_node_positions_m[None,:,1]).ravel()
    energy=float(u@(K@u))
    relative_energy=abs(energy/(112*c*c)-1)
    neumann=float(np.max(abs(K@np.ones(K.shape[0])))/max(
        1.,float(np.max(abs(K.diagonal())))))
    if relative_energy>2e-9 or neumann>1e-10:
        raise ValueError("physical exact roof Q1 lump violated affine Neumann weak gradient physics")
    # Theoretical 50:50 mass has partial consistent affine kinetic moment:
    physical_lumped_mass=float(u@(M@u))
    physical_consistent_affine_mass=2780/3
    prev=unpairs(consistent["new_native_Q1_true_roof_q0_full_original_250ms_signed_40_80"])
    original_full=unpairs(original["unmodified_original_transfer_pa_per_m3_s"])
    earlier_lump=unpairs(prior_lump["new_true_roof_Q1_positive_lumped_mass_allmode_full_250ms_original_signed_40_80"])
    return {"ppw":ppw,
      "original_SHA256_comms":file_hash(folder/"comms_out.h5"),
      "original_SHA256_voxel":file_hash(folder/"vox_out.h5"),
      "original_SHA256_full_native_real_wave":file_hash(folder/"sim_outs.h5"),
      "native_original_h_m":h_m,"native_original_Ts_s":dt,"native_original_Nt":Nt,
      "native_original_all_8_input_sum":sigsum,
      "true_original_cut_Q1_physical_full_3D_nodes_all_modes":len(lamb),
      "true_original_cut_Q1_2D_nodes":cross.native_yz_modes,
      "physical_exact_3D_volume_m3":lump.true_volume_m3,
      "original_real_native_staircase_oblique_roof_flux_audit":real_stair_flux,
      "positive_yz_cut_Q1_lump_min_mass_m2":float(min(lump.yz_true_mass_rows)),
      "original_true_positive_outside_support_ghost_nodes_retained":
          cross.occupied_cartesian_original_exterior_nodes,
      "Q1_new_half_true_3D_M_all_nonzero_count":int(M.nnz),
      "Q1_new_true_lumped_3D_K_nnz":int(K.nnz),
      "Neumann_constant_rel_residual":neumann,
      "exact_affine_x_plus_z_physical_Q1_gradient_stiffness_rel_error":relative_energy,
      "exact_affine_x_plus_z_half_consistent_lumped_mass_value":physical_lumped_mass,
      "prior_true_consistent_mass_affine_value":physical_consistent_affine_mass,
      "x_all_modes_generalized_eigencheck":xproof,
      "yz_all_modes_generalized_eigencheck":yproof,
      "num_true_high_modes_above_native_nyquist_retained":
          int(np.count_nonzero(dt*np.sqrt(np.maximum(lamb,0))>np.pi)),
      "unchanged_original_native_original_8src_rec_HDF5_factorization_error":[srcproof,recproof],
      "new_true_roof_Q1_half_theory_mass_allmode_full_250ms_original_signed_40_80":pairs(signed),
      "preobserved_full_positive_lump_Q1_original_signed_40_80":pairs(earlier_lump),
      "preobserved_consistent_Q1_true_roof_allmode_full_250ms_signed_40_80":pairs(prev),
      "original_full250ms_native_real_PFFDTD_q0_signed_40_80":pairs(original_full),
      "new_original_q0_Q1_lumped_first_roof_echo_causal_weak_all_widths":ref,
      "native_impulse_full250ms_source_receiver_unmodified":True,
      "wall_seconds":float(time.perf_counter()-tic)}


def main():
    a=argparse.ArgumentParser()
    a.add_argument("--plan",type=Path,required=True)
    a.add_argument("--original-sims-root",type=Path,required=True)
    a.add_argument("--output",type=Path,required=True)
    args=a.parse_args()
    raw=args.plan.read_bytes()
    p=validate_plan(json.loads(raw.decode("utf-8")))
    base=p["frozen_original"]
    o=json.loads((ROOT/base["raw_native_HDF5_evidence"]).read_text(encoding="utf-8"))
    prev=json.loads((ROOT/base["true_Q1_consistent_5grid_evidence"]).read_text(encoding="utf-8"))
    roof=json.loads((ROOT/base["true_first_roof_original_raw_evidence"]).read_text(encoding="utf-8"))
    baseline_lump=json.loads((ROOT/base["true_Q1_lumped_5grid_evidence"]).read_text(encoding="utf-8"))
    lump_cases={x["ppw"]:x for x in baseline_lump["actual_all_five_original_native_q0_positive_cut_Q1_lump_cases"]}
    source={x["ppw"]:x for x in o["actual_native_wave_cases"]}
    consistent={x["ppw"]:x for x in prev["actual_native_original_point_q0_Q1_cutroof_full_modes_cases"]}
    pastroof={x["ppw"]:x for x in roof["actual_original_q0_five_grid_real_first_roof_echo_weak_cases"]}
    if set(source)!=set(PPW) or set(consistent)!=set(PPW) or set(pastroof)!=set(PPW) or set(lump_cases)!=set(PPW):
        raise ValueError("all original five real wave controls and previous Q1+roof references required")
    dirs={}
    for f in args.original_sims_root.rglob("comms_out.h5"):
        h=file_hash(f)
        matches=[k for k in PPW if source[k]["original_native_comm_sha256"]==h]
        if len(matches)==1:
            if matches[0] in dirs:raise ValueError("ambiguous original source native HDF5")
            dirs[matches[0]]=f.parent
    if set(dirs)!=set(PPW):raise ValueError("all original 5-grid real HDF5 sources missing")
    evidence={"schema_version":"htdt.r130d.original-q0-oblique-normal-fixed-theoretical-half-Q1-full-evidence-1",
       "frozen_plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
       "precommitted_plan":p,
       "unchanged_original_PFFDTD_self_convergence":"SELF_CONVERGENCE_FAILED",
       "independent_physical_validation":"NOT_VALIDATED","product":"NO_GO",
       "new_original_native_PFFDTD_runs":0,"new_GitHub_Actions_runs":0,
       "actual_all_five_original_native_q0_theory_half_cut_Q1_cases":[],
       "complete_original_signed_4adjacent_3gates_vs_all_preobserved_cut_Q1_and_original_control":[]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    for k in PPW:
        try:r=one_case(k,dirs[k],source[k],consistent[k],lump_cases[k],pastroof[k],p)
        except Exception as ex:
            evidence["actual_all_five_original_native_q0_theory_half_cut_Q1_cases"].append(
                {"ppw":k,"status":"FAIL_CLOSED_MASS_LUMP_EXPERIMENT_INCOMPLETE",
                 "error_type":type(ex).__name__,"error":str(ex)})
            args.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        evidence["actual_all_five_original_native_q0_theory_half_cut_Q1_cases"].append(r)
        args.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("NEW_ORIGINAL_Q0_TRUE_NEUMANN_ROOF_THEORY_HALF_Q1",k,
           "modes",r["true_original_cut_Q1_physical_full_3D_nodes_all_modes"],
           "signed",r["new_true_roof_Q1_half_theory_mass_allmode_full_250ms_original_signed_40_80"],
           "roof_ratios",[round(x["new_native_Q1_half_weak_roof_vs_original_roof_image_signed_ratio"],5)
              for x in r["new_original_q0_Q1_lumped_first_roof_echo_causal_weak_all_widths"]],
           flush=True)
    cases=evidence["actual_all_five_original_native_q0_theory_half_cut_Q1_cases"]
    for coarse,fine in zip(cases,cases[1:]):
        arms={}
        for arm,key in [
          ("new_theory_fixed_half_consistent_half_lump_Q1",
           "new_true_roof_Q1_half_theory_mass_allmode_full_250ms_original_signed_40_80"),
          ("preobserved_positive_row_sum_lumped_cut_Q1",
           "preobserved_full_positive_lump_Q1_original_signed_40_80"),
          ("preobserved_consistent_mass_cut_Q1",
           "preobserved_consistent_Q1_true_roof_allmode_full_250ms_signed_40_80"),
          ("original_unmodified_PFFDTD_q0",
           "original_full250ms_native_real_PFFDTD_q0_signed_40_80")
        ]:
            vals=compare_complex_transfer(reference=fine[key],
                candidate=coarse[key],frequency_hz=[40,80],
                magnitude_mask_relative_db=-50).model_dump(mode="json")
            arms[arm]={"unchanged_original_full_250ms_40_80_signed_scores":vals,
                "all_original_three_frozen_gates_PASS":bool(
                     vals["complex_rms_relative"]<=.2
                     and vals["magnitude_max_relative"]<=.25
                     and vals["phase_max_deg"]<=15),
                "original_signed_coarse_minus_fine":pairs(unpairs(coarse[key])-unpairs(fine[key]))}
        evidence["complete_original_signed_4adjacent_3gates_vs_all_preobserved_cut_Q1_and_original_control"].append({
            "coarse_ppw":coarse["ppw"],"fine_ppw":fine["ppw"],"arms":arms})
    verdict={}
    for arm in ("new_theory_fixed_half_consistent_half_lump_Q1","preobserved_positive_row_sum_lumped_cut_Q1","preobserved_consistent_mass_cut_Q1",
                "original_unmodified_PFFDTD_q0"):
        blocks=[x["arms"][arm] for x in evidence["complete_original_signed_4adjacent_3gates_vs_all_preobserved_cut_Q1_and_original_control"]]
        keys=("complex_rms_relative","magnitude_max_relative","phase_max_deg")
        verdict[arm]={"all_4adjacent_original_three_frozen_gates_pass":all(
            v["all_original_three_frozen_gates_PASS"] for v in blocks),
            "strict_original_three_metric_monotone":all(all(
                blocks[i]["unchanged_original_full_250ms_40_80_signed_scores"][k]<
                blocks[i-1]["unchanged_original_full_250ms_40_80_signed_scores"][k]
                for k in keys) for i in range(1,4)),
            "no_canonical_upstream_qualification":True}
    evidence["complete_full5grid_native_q0_still_unqualified_verdict"]=verdict
    kh=np.array([.4,.2,.1,.05,.025],dtype=float)
    evidence["parameter_free_1D_dispersion_theory"]={
        "kh":kh.tolist(),
        "half_M_lambda_over_true_wave_k2":wave_uniform_1d_symbol_ratio(kh,mass_kind="half").tolist(),
        "lumped_M_lambda_over_true_wave_k2":wave_uniform_1d_symbol_ratio(kh,mass_kind="lumped").tolist(),
        "consistent_M_lambda_over_true_wave_k2":wave_uniform_1d_symbol_ratio(kh,mass_kind="consistent").tolist(),
        "fixed_blend_coefficient_without_fitting":.5}
    evidence["no_production_go_or_independent_physical_validation"]=True
    args.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("EXACT_Q1_THEORY_HALF_ORIGINAL_SIGNED_FROZEN_3GATE_FULL5GRID",[
       (r["coarse_ppw"],r["fine_ppw"],{
           a:[round(r["arms"][a]["unchanged_original_full_250ms_40_80_signed_scores"][z],6)
            for z in ("complex_rms_relative","magnitude_max_relative","phase_max_deg")]
           for a in r["arms"]})
       for r in evidence["complete_original_signed_4adjacent_3gates_vs_all_preobserved_cut_Q1_and_original_control"]],
       flush=True)
    print("EXACT_Q1_THEORY_HALF_ORIGINAL_VERDICTS",verdict,flush=True)
if __name__=="__main__":main()
