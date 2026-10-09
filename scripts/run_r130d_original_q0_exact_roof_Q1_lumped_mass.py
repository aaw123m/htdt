#!/usr/bin/env python3
"""Actual original q0 8node full native 250ms Q1 row-sum lump Neumann 5grid.

Experimental physical true cut-roof Q1 stiffness; positive diagonal physical
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
from htdt.r130d_native_cut_Q1_positive_lumped import (
    physical_positive_row_sum_lump,true_full_3d_lumped_roof_MK,
    allmode_native_newmark_roof_causal_pressure_weak)
from htdt.r130d_causal_first_roof_echo import (
    ROOF_CENTER_S,ROOF_RADIUS_S,ROOF_WIDTHS_S,
    analytic_native_64point_physical_roof_echo_weak)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_native_exact_roof_full_xy_z_modal_q0 import entire_original_finite_record_signed_modes
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs
from run_r130d_original_point_quadratic_pffdtd import PIN,PPW,file_hash

SCHEMA="htdt.r130d.original-q0-exact-roof-cut-Q1-positive-row-lump-plan-1"
FROZEN=(.2,.25,15.)


def validate_plan(p):
    o=p["frozen_original"]
    n=p["new_physical_scheme"]
    v=p["independent_invariants"]
    limits=p["limits"]
    if (p.get("schema_version")!=SCHEMA or p["issue"]!=938 or p["pr"]!=1055
        or o["upstream_sha"]!=PIN or o["ppw"]!=list(PPW)
        or o["original_exact_native_source_xyz_m"]!=[1.5,2,2]
        or o["original_exact_native_receiver_xyz_m"]!=[2.5,2,2]
        or not o["original_SHA_pinned_8node_in_ixyz_in_sigs_out_ixyz_out_alpha_preserved"]
        or not o["native_original_h_dt_Nt_all_grids_preserved"]
        or o["full_record_s"]!=.25 or o["original_signed_hz"]!=[40,80]
        or [o["complex_gate"],o["magnitude_gate"],o["phase_deg_gate"]]!=list(FROZEN)
        or o["c_m_s"]!=343.2 or o["rho_kg_m3"]!=1.2
        or not o["zero_high_mode_cutoff"] or not o["zero_taper_or_wave_smoothing"]
        or n["physical_volume_exact"]!=56 or n["physical_yz_area_exact"]!=14
        or not n["no_mode_eigenvalue_truncation_damping_or_shift"]
        or not all(v.values())
        or not p["preobserved_baselines"]["preobserved_cut_Q1_all_four_pairs_FAIL"]
        or not p["preobserved_baselines"]["preobserved_original_PFFDTD_all_four_pairs_FAIL"]
        or limits!={"max_yz_modes":3300,"max_total_dofs":180000,
                    "max_cases":5,"new_native_pffdtd_waves":0,
                    "new_github_actions":0,"retain_scratch":True}
        or p["release"]["original_upstream_PFFDTD_q0"]!="SELF_CONVERGENCE_FAILED"
        or p["release"]["independent_physics"]!="NOT_VALIDATED"
        or p["release"]["product"]!="NO_GO"):
        raise ValueError("prospectively frozen Q1 physical lumped roof original q0 settings changed")
    return p


def one_case(ppw,folder,original,consistent,prior_roof,p):
    tic=time.perf_counter()
    if (file_hash(folder/"comms_out.h5")!=original["original_native_comm_sha256"]
        or file_hash(folder/"vox_out.h5")!=original["original_solver_geometry_sha256"]
        or file_hash(folder/"sim_outs.h5")!=prior_roof["original_true_native_entire_real_q0_HDF5_SHA256"]):
        raise ValueError("original SHA exact HDF5 source, physical room or original real wave tampered")
    with h5py.File(folder/"vox_out.h5","r") as f:
        axes=[np.asarray(f[k][:],dtype=float) for k in ("xv","yv","zv")]
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
    lump=physical_positive_row_sum_lump(mx,cross.mass,kx,cross.stiffness)
    M,K=true_full_3d_lumped_roof_MK(lump)
    lx,vx,xproof=generalized_symmetric_P1_consistent_modes(lump.Mx,lump.Kx)
    ly,vy,yproof=generalized_native_original_Q1_full_physical_neumann_modes(
        lump.Myz,lump.Kyz)
    lamb=(lx[:,None]+ly[None,:]).ravel()
    if len(lamb)!=M.shape[0] or len(lamb)>p["limits"]["max_total_dofs"]:
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
            "NEW_lumped_true_roof_original_native_allmode_window":w,
            "unchanged_true_64node_physical_roof_image_analytic":image,
            "new_native_Q1_lumped_weak_roof_vs_original_roof_image_signed_ratio":float(v/y),
            "new_native_Q1_lumped_weak_roof_vs_image_relative":float(abs(v/y-1)),
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
    # Distinguish mass-lump interpolation error from consistent Galerkin:
    physical_lumped_mass=float(u@(M@u))
    physical_consistent_affine_mass=2780/3
    prev=unpairs(consistent["new_native_Q1_true_roof_q0_full_original_250ms_signed_40_80"])
    original_full=unpairs(original["unmodified_original_transfer_pa_per_m3_s"])
    return {"ppw":ppw,
      "original_SHA256_comms":file_hash(folder/"comms_out.h5"),
      "original_SHA256_voxel":file_hash(folder/"vox_out.h5"),
      "original_SHA256_full_native_real_wave":file_hash(folder/"sim_outs.h5"),
      "native_original_h_m":h_m,"native_original_Ts_s":dt,"native_original_Nt":Nt,
      "native_original_all_8_input_sum":sigsum,
      "true_original_cut_Q1_physical_full_3D_nodes_all_modes":len(lamb),
      "true_original_cut_Q1_2D_nodes":cross.native_yz_modes,
      "physical_exact_3D_volume_m3":lump.total_original_physical_3d_volume_m3,
      "positive_yz_cut_Q1_lump_min_mass_m2":float(min(lump.physical_yz_rowsum_m2)),
      "original_true_positive_outside_support_ghost_nodes_retained":
          cross.occupied_cartesian_original_exterior_nodes,
      "Q1_new_true_lumped_3D_M_diagonal_nnz":int(M.nnz),
      "Q1_new_true_lumped_3D_K_nnz":int(K.nnz),
      "Neumann_constant_rel_residual":neumann,
      "exact_affine_x_plus_z_physical_Q1_gradient_stiffness_rel_error":relative_energy,
      "exact_affine_x_plus_z_lumped_mass_value":physical_lumped_mass,
      "prior_true_consistent_mass_affine_value":physical_consistent_affine_mass,
      "x_all_modes_generalized_eigencheck":xproof,
      "yz_all_modes_generalized_eigencheck":yproof,
      "num_true_high_modes_above_native_nyquist_retained":
          int(np.count_nonzero(dt*np.sqrt(np.maximum(lamb,0))>np.pi)),
      "unchanged_original_native_original_8src_rec_HDF5_factorization_error":[srcproof,recproof],
      "new_true_roof_Q1_positive_lumped_mass_allmode_full_250ms_original_signed_40_80":pairs(signed),
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
    base=p["preobserved_baselines"]
    o=json.loads((ROOT/base["true_original_PFFDTD_source_reference"]).read_text(encoding="utf-8"))
    prev=json.loads((ROOT/base["physical_cut_Q1_consistent_mass_reference"]).read_text(encoding="utf-8"))
    roof=json.loads((ROOT/base["original_first_roof_reflection_real_reference"]).read_text(encoding="utf-8"))
    source={x["ppw"]:x for x in o["actual_native_wave_cases"]}
    consistent={x["ppw"]:x for x in prev["actual_native_original_point_q0_Q1_cutroof_full_modes_cases"]}
    pastroof={x["ppw"]:x for x in roof["actual_original_q0_five_grid_real_first_roof_echo_weak_cases"]}
    if set(source)!=set(PPW) or set(consistent)!=set(PPW) or set(pastroof)!=set(PPW):
        raise ValueError("all original five real wave controls and previous Q1+roof references required")
    dirs={}
    for f in args.original_sims_root.rglob("comms_out.h5"):
        h=file_hash(f)
        matches=[k for k in PPW if source[k]["original_native_comm_sha256"]==h]
        if len(matches)==1:
            if matches[0] in dirs:raise ValueError("ambiguous original source native HDF5")
            dirs[matches[0]]=f.parent
    if set(dirs)!=set(PPW):raise ValueError("all original 5-grid real HDF5 sources missing")
    evidence={"schema_version":"htdt.r130d.original-q0-physical-cut-Q1-row-lump-full-evidence-1",
       "frozen_plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
       "precommitted_plan":p,
       "unchanged_original_PFFDTD_self_convergence":"SELF_CONVERGENCE_FAILED",
       "independent_physical_validation":"NOT_VALIDATED","product":"NO_GO",
       "new_original_native_PFFDTD_runs":0,"new_GitHub_Actions_runs":0,
       "actual_all_five_original_native_q0_positive_cut_Q1_lump_cases":[],
       "complete_original_signed_4adjacent_3gates_vs_true_cut_Q1_consistent_control":[]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    for k in PPW:
        try:r=one_case(k,dirs[k],source[k],consistent[k],pastroof[k],p)
        except Exception as ex:
            evidence["actual_all_five_original_native_q0_positive_cut_Q1_lump_cases"].append(
                {"ppw":k,"status":"FAIL_CLOSED_MASS_LUMP_EXPERIMENT_INCOMPLETE",
                 "error_type":type(ex).__name__,"error":str(ex)})
            args.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        evidence["actual_all_five_original_native_q0_positive_cut_Q1_lump_cases"].append(r)
        args.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("NEW_ORIGINAL_Q0_TRUE_NEUMANN_ROOF_POSITIVE_Q1_LUMP",k,
           "modes",r["true_original_cut_Q1_physical_full_3D_nodes_all_modes"],
           "signed",r["new_true_roof_Q1_positive_lumped_mass_allmode_full_250ms_original_signed_40_80"],
           "roof_ratios",[round(x["new_native_Q1_lumped_weak_roof_vs_original_roof_image_signed_ratio"],5)
              for x in r["new_original_q0_Q1_lumped_first_roof_echo_causal_weak_all_widths"]],
           flush=True)
    cases=evidence["actual_all_five_original_native_q0_positive_cut_Q1_lump_cases"]
    for coarse,fine in zip(cases,cases[1:]):
        arms={}
        for arm,key in [
          ("new_positive_exact_mass_lumped_cut_Q1",
           "new_true_roof_Q1_positive_lumped_mass_allmode_full_250ms_original_signed_40_80"),
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
        evidence["complete_original_signed_4adjacent_3gates_vs_true_cut_Q1_consistent_control"].append({
            "coarse_ppw":coarse["ppw"],"fine_ppw":fine["ppw"],"arms":arms})
    verdict={}
    for arm in ("new_positive_exact_mass_lumped_cut_Q1","preobserved_consistent_mass_cut_Q1",
                "original_unmodified_PFFDTD_q0"):
        blocks=[x["arms"][arm] for x in evidence["complete_original_signed_4adjacent_3gates_vs_true_cut_Q1_consistent_control"]]
        keys=("complex_rms_relative","magnitude_max_relative","phase_max_deg")
        verdict[arm]={"all_4adjacent_original_three_frozen_gates_pass":all(
            v["all_original_three_frozen_gates_PASS"] for v in blocks),
            "strict_original_three_metric_monotone":all(all(
                blocks[i]["unchanged_original_full_250ms_40_80_signed_scores"][k]<
                blocks[i-1]["unchanged_original_full_250ms_40_80_signed_scores"][k]
                for k in keys) for i in range(1,4)),
            "no_canonical_upstream_qualification":True}
    evidence["complete_full5grid_native_q0_still_unqualified_verdict"]=verdict
    evidence["no_production_go_or_independent_physical_validation"]=True
    args.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("EXACT_Q1_LUMP_ORIGINAL_SIGNED_FROZEN_3GATE_FULL5GRID",[
       (r["coarse_ppw"],r["fine_ppw"],{
           a:[round(r["arms"][a]["unchanged_original_full_250ms_40_80_signed_scores"][z],6)
            for z in ("complex_rms_relative","magnitude_max_relative","phase_max_deg")]
           for a in r["arms"]})
       for r in evidence["complete_original_signed_4adjacent_3gates_vs_true_cut_Q1_consistent_control"]],
       flush=True)
    print("EXACT_Q1_LUMP_ORIGINAL_VERDICTS",verdict,flush=True)
if __name__=="__main__":main()
