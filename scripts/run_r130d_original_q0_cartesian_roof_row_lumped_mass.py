#!/usr/bin/env python3
"""Preregistered original SHA-pinned five-grid q0: complete Cartesian 7pt
true inclined-Neumann roof with positive physical diagonal mass.

Every native 8 source/8 receiver, q0, native time grid, all 3D eigenmodes,
full 250ms 40/80Hz original signed pressure score is preserved.
This is an EXPERIMENTAL alternative solver, not PFFDTD qualification.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import h5py
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend/src"))
from htdt.r130d_cartesian_true_roof_hybrid_flux import build_native_cartesian_true_roof_hybrid_flux
from htdt.r130d_cartesian_true_roof_lumped_sevenpoint import build_cartesian_sevenpoint_true_roof
from htdt.r130d_conforming_roof_p1_fem import build_original_native_conforming_roof_p1
from htdt.r130d_conforming_roof_p1_consistent_mass import (
    consistent_physical_P1_tensored_operators,generalized_symmetric_P1_consistent_modes)
from htdt.r130d_native_cut_roof_Q1_galerkin import (
    original_eightnode_native_HDF5_Q1_source_receiver,
    generalized_native_original_Q1_full_physical_neumann_modes)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_q0_cartesian_flux_true_roof_hybrid import fixed_first_roof_weak_full_modes
from run_r130d_native_exact_roof_full_xy_z_modal_q0 import entire_original_finite_record_signed_modes
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs
from run_r130d_original_point_quadratic_pffdtd import PIN,PPW,file_hash

SCHEMA="htdt.r130d.original-q0-cartesian-roof-row-lumped-mass-plan-1"
PLAN_PATH="benchmarks/acoustics/r130d_original_q0_cartesian_roof_row_lumped_mass_plan_2026-10-10.json"
PLAN_COMMIT="d2ea0a531853988e79ae38f8c9a2922ab9be0ae7"
BRANCH="feat/r130d-embedded-neumann-fv-20261009"
ARM="native_complete_cartesian_7point_true_roof_positive_row_lumped_mass"
OLD_ARM="new_native_cartesian_interior_fivepoint_true_roof_hybrid_original_8node"


def validate_plan(plan:dict)->dict:
    p=plan["original"];n=plan["new_operator"];caps=plan["caps"]
    a=plan["authority"];i=plan["independent"]
    if (plan.get("schema_version")!=SCHEMA
        or plan["repo"]!="aaw123m/htdt"
        or plan["issue_migrated"]!=53 or plan["pr_migrated"]!=118
        or plan["old_issue"]!=938 or plan["old_pr"]!=1055
        or p["upstream_sha"]!=PIN or p["ppw"]!=list(PPW)
        or p["source_xyz"]!=[1.5,2,2] or p["receiver_xyz"]!=[2.5,2,2]
        or p["record_s"]!=.25 or p["sound_speed_m_s"]!=343.2
        or p["density_kg_m3"]!=1.2 or p["frequencies_signed_hz"]!=[40,80]
        or p["all_four_pairs"]!=[[28,32],[32,36],[36,40],[40,44]]
        or p["frozen_acceptance"]!={"complex_rms_relative":.2,
                                    "magnitude_max_relative":.25,
                                    "phase_max_deg":15}
        or not all(p[k] for k in (
            "original_HDF5_SHA_match_required",
            "unmodified_HDF5_eight_source_and_eight_receiver",
            "zero_q0_samples_after_initial",
            "native_dt_h_Nt_each_grid","strict_monotone_all_three"))
        or n["name"]!=ARM or n["mass_volume_m3"]!=56
        or not all(n[k] for k in (
            "all_generalized_3d_modes_retained","positive_physical_sliver_basis_retained",
            "exact_consistent_mass_comparator_unmodified"))
        or n["forbidden"]!=["high-mode-cut","mass-floor","source smoothing",
                            "receiver smoothing","time windowing","phase/level fitting",
                            "any frequency/gate change","post-hoc experiment arm change"]
        or not all(i.values())
        or caps!={"five_grids_only":True,"max_yz_modes":3300,
                  "max_total_3d_modes":180000,"new_original_PFFDTD_waves":0,
                  "new_actions_runs":0,"retain_scratch":True}
        or a!={"PFFDTD_original":"SELF_CONVERGENCE_FAILED",
               "independent_physics":"NOT_VALIDATED","product":"NO_GO",
               "issue_open":True,"PR_draft":True}):
        raise ValueError("preregistered original q0 complete sevenpoint true roof physical mass plan mutated")
    return plan


def require_remote_preregistration():
    def git(*args):
        return subprocess.run(["git","-C",str(ROOT),*args],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
    ref="origin/"+BRANCH
    commit=git("show",PLAN_COMMIT+":"+PLAN_PATH)
    ancestor=git("merge-base","--is-ancestor",PLAN_COMMIT,ref)
    cached=git("rev-parse",ref)
    advertised=git("ls-remote","origin","refs/heads/"+BRANCH)
    remote=advertised.stdout.decode("ascii",errors="replace").split()
    frozen=(ROOT/PLAN_PATH).read_bytes().replace(b"\r\n",b"\n")
    if (commit.returncode or ancestor.returncode or cached.returncode
        or advertised.returncode or len(remote)!=2
        or remote[0]!=cached.stdout.decode().strip()
        or commit.stdout.replace(b"\r\n",b"\n")!=frozen):
        raise RuntimeError("PREREGISTRATION_NOT_ON_GITHUB: no new real fivegrid experiment allowed")
    return True


def one_case(ppw,folder,old_native,prior,p):
    tick=time.perf_counter()
    for filename,sha in (("comms_out.h5","original_native_comm_sha256"),
                         ("vox_out.h5","original_solver_geometry_sha256")):
        if file_hash(folder/filename)!=old_native[sha]:
            raise ValueError("original source or roof HDF5 SHA has drifted")
    with h5py.File(folder/"vox_out.h5","r") as f:
        axes=[np.asarray(f[q][:],dtype=float) for q in ("xv","yv","zv")]
    dims=tuple(len(q) for q in axes)
    with h5py.File(folder/"comms_out.h5","r") as f:
        si=np.asarray(f["in_ixyz"][:],dtype=np.int64)
        ri=np.asarray(f["out_ixyz"][:],dtype=np.int64)
        sig=np.asarray(f["in_sigs"][:],dtype=float)
        rw=np.asarray(f["out_alpha"][:],dtype=float).ravel()
        nt=int(f["Nt"][()])
        if (si.shape!=(8,) or ri.shape!=(8,) or sig.shape!=(8,nt)
            or rw.shape!=(8,) or np.any(sig[:,1:]!=0)
            or int(f["diff"][()])!=0 or abs(float(rw.sum())-1)>1e-12):
            raise ValueError("original real HDF5 eightnode q0 changed")
        strength=float(sig[:,0].sum())
        sw=sig[:,0]/strength
    with h5py.File(folder/"sim_consts.h5","r") as f:
        dt=float(f["Ts"][()]);h=float(f["h"][()])
        l2=float(f["l2"][()]);c=float(f["c"][()])
    if (abs(c-343.2)>1e-12 or abs(h-c/(100*ppw))>1e-12
        or abs(strength-l2/h)>1e-10
        or nt!=prior["original_native_full_Nt"]
        or abs(dt-prior["original_native_Ts_s"])>1e-12):
        raise ValueError("original real native h/dt/Nt/strength changed")
    fem=build_original_native_conforming_roof_p1(
        axes,max_yz_nodes=3000,max_3d_nodes=180000)
    mx,_,kx,_=consistent_physical_P1_tensored_operators(fem)
    hy=build_native_cartesian_true_roof_hybrid_flux(
        axes[1],axes[2],max_active_yz_nodes=p["caps"]["max_yz_modes"])
    q=hy.cut_q1
    roof_tangent=q.physical_active_yz_node_positions_m[:,0]-.25*q.physical_active_yz_node_positions_m[:,1]
    ycoord=q.physical_active_yz_node_positions_m
    roof_d=4.-.25*ycoord[:,0]-ycoord[:,1]
    near=(ycoord[:,0]>2*h)&(ycoord[:,0]<4-2*h)&(ycoord[:,1]>2*h)&(abs(roof_d)<2*h)
    if np.count_nonzero(near)<5:raise ValueError("true inclined roof manufactured witness unavailable")
    roof_peak=float(np.max(abs((hy.stiffness@roof_tangent)[near])))
    if roof_peak>1e-7:
        raise ValueError(f"physical inclined-Neumann roof affine residual {roof_peak}")
    # Physical x P1 excludes original voxel-ghost padding; y/z Q1 retains
    # original native indexing including all supported physical slivers.
    seven=build_cartesian_sevenpoint_true_roof(
        [fem.x_positions_m,axes[1],axes[2]],mx,kx,hy,c_m_s=c)
    if seven.seven_neighbor_stencil_max_relative>1e-8 or seven.total_true_room_m3!=56:
        if abs(seven.total_true_room_m3-56)>2e-8:
            raise ValueError("physical volume/stencil has changed")
    lump=seven.physical
    sx,sy,se=original_eightnode_native_HDF5_Q1_source_receiver(
        dims,fem.x_native_map,q,si,sw,len(fem.x_positions_m))
    rx,ry,re=original_eightnode_native_HDF5_Q1_source_receiver(
        dims,fem.x_native_map,q,ri,rw,len(fem.x_positions_m))
    if max(se,re)>1e-12:raise ValueError("original HDF5 8-point source/receiver Q1 interpolation changed")
    lx,vx,xproof=generalized_symmetric_P1_consistent_modes(lump.Mx,lump.Kx)
    ly,vy,yproof=generalized_native_original_Q1_full_physical_neumann_modes(
        lump.Myz,lump.Kyz)
    allmodes=len(lx)*len(ly)
    if (allmodes>p["caps"]["max_total_3d_modes"]
        or allmodes!=prior["real_original_native_3D_full_true_Q1_modes"]):
        raise ValueError("new full original 3D positive Q1 modes lost")
    lam=(lx[:,None]+ly[None,:]).ravel()
    coupling=np.outer((sx@vx)*(rx@vx),(sy@vy)*(ry@vy)).ravel()
    kick=dt*dt*c*c*coupling/(1+dt*dt*lam/4)
    signed=entire_original_finite_record_signed_modes(lam,kick,dt,nt,1.2).sum(axis=1)
    if not np.all(np.isfinite(signed)):raise ValueError("new native complete sevenpoint q0 nonfinite")
    field=fem.x_positions_m[:,None]+ycoord[None,:,1]
    flat=field.ravel()
    stiffness=float(flat@(seven.stiffness@flat))
    exact_stiff=112*c*c
    stiff_rel=abs(stiffness-exact_stiff)/exact_stiff
    if stiff_rel>2e-9:
        raise ValueError("new physical exact 3D affine stiffness mismatch")
    constant=float(np.max(abs(seven.stiffness@np.ones(allmodes)))/
                   max(float(np.max(abs(seven.stiffness.diagonal()))),1.))
    if constant>1e-10:raise ValueError("allmode physically lumped Neumann constant lost")
    roof_echo=fixed_first_roof_weak_full_modes(
        lam,kick,dt,nt,axes,dims,si,sw,ri,rw)
    old=unpairs(prior["new_native_hybrid_true_roof_q0_full_original_250ms_signed_40_80"])
    return {
        "ppw":ppw,"original_native_comm_sha256":file_hash(folder/"comms_out.h5"),
        "original_native_voxel_sha256":file_hash(folder/"vox_out.h5"),
        "native_original_Ts_s":dt,"native_original_full_Nt":nt,
        "native_original_sum_eightpoint_q0_strength":strength,
        "all_original_x_modes":len(lx),
        "all_positive_native_yz_Q1_support_nodes":q.native_yz_modes,
        "all_3D_original_native_physical_modes":allmodes,
        "original_exact_cut_Q1_positive_sliver_support_kept":q.occupied_cartesian_original_exterior_nodes,
        "original_full_wet_axis_flux_rectangles":hy.cartesian_full_rectangle_count,
        "exact_inclined_roof_cut_rectangles":hy.exact_roof_cut_rectangle_count,
        "exact_true_room_volume_m3":seven.total_true_room_m3,
        "all_true_roof_affine_neumann_weak_max":roof_peak,
        "all_3D_affine_stiffness_relative":stiff_rel,
        "all_3D_neumann_constant_relative":constant,
        "strict_cartesian_sevenpoint_interior_relative":seven.seven_neighbor_stencil_max_relative,
        "strict_cartesian_sevenpoint_interior_diagonal_mass_m3":seven.representative_interior_mass_m3,
        "all_original_native_modes_above_nyquist_kept":int(np.count_nonzero(dt*np.sqrt(lam)>np.pi)),
        "lumped_x_complete_generalized_modes_proof":xproof,
        "lumped_yz_complete_generalized_modes_proof":yproof,
        "original_8source_factorization_error":se,
        "original_8receiver_factorization_error":re,
        "full_original_250ms_q0_40_80_signed_positive_lumped_cartesian_7pt_true_roof":pairs(signed),
        "previous_exact_consistent_Q1_mass_5pt_hybrid_250ms_40_80_signed":pairs(old),
        "independent_true_roof_original_64pair_three_weak_witnesses":roof_echo,
        "time_s":float(time.perf_counter()-tick),
        "original_full_modes_untruncated_and_native_record_unchanged":True}


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    a=ap.parse_args()
    if a.plan.resolve()!=(ROOT/PLAN_PATH).resolve():
        raise ValueError("only frozen pre-pushed plan accepted")
    plan=validate_plan(json.loads(a.plan.read_text(encoding="utf8")))
    require_remote_preregistration()  # before reading/diagonalizing original HDF5
    native=json.loads((ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json").read_text(encoding="utf8"))
    prior=json.loads((ROOT/plan["original"]["prior_hybrid_reference"]).read_text(encoding="utf8"))
    old={x["ppw"]:x for x in prior["actual_native_original_point_q0_cartesian_hybrid_full_modes_cases"]}
    original={x["ppw"]:x for x in native["actual_native_wave_cases"]}
    if set(old)!=set(PPW) or set(original)!=set(PPW):
        raise ValueError("five original HDF5 baseline cases missing")
    folders={}
    for f in a.original_sims_root.rglob("comms_out.h5"):
        ident=file_hash(f)
        matching=[ppw for ppw in PPW if ident==original[ppw]["original_native_comm_sha256"]]
        if len(matching)==1:
            if matching[0] in folders:raise ValueError("duplicate original HDF5 q0 SHA")
            folders[matching[0]]=f.parent
    if set(folders)!=set(PPW):
        raise ValueError("missing five SHA-pinned original native q0 source HDF5")
    raw=a.plan.read_bytes()
    out={"schema_version":"htdt.r130d.original-q0-cartesian-sevenpoint-true-roof-positive-mass-evidence-1",
        "preregistered_plan":plan,"preregistered_plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "live_GitHub_frozen_preregistration_sha":PLAN_COMMIT,
        "original_PFFDTD":"SELF_CONVERGENCE_FAILED",
        "independent_physics":"NOT_VALIDATED","product":"NO_GO",
        "new_native_PFFDTD_waves":0,"new_GitHub_Actions_runs":0,
        "real_original_native_8node_q0_sevenpoint_lumped_mass_cases":[],
        "original_full_q0_250ms_signed_adjacent_gates":[]}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    for ppw in PPW:
        try:q=one_case(ppw,folders[ppw],original[ppw],old[ppw],plan)
        except Exception as exc:
            out["real_original_native_8node_q0_sevenpoint_lumped_mass_cases"].append({
                "ppw":ppw,"status":"NOT_COMPLETED","type":type(exc).__name__,"details":str(exc)})
            a.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n",encoding="utf8")
            raise
        out["real_original_native_8node_q0_sevenpoint_lumped_mass_cases"].append(q)
        a.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n",encoding="utf8")
        print("SEVENPOINT_TRUE_ROOF_ORIGINAL_Q0",ppw,"modes",q["all_3D_original_native_physical_modes"],
              "sevenpt",q["strict_cartesian_sevenpoint_interior_relative"],
              "signed",q["full_original_250ms_q0_40_80_signed_positive_lumped_cartesian_7pt_true_roof"],flush=True)
    cases=out["real_original_native_8node_q0_sevenpoint_lumped_mass_cases"]
    for coarse,fine in zip(cases,cases[1:]):
        c=unpairs(coarse["full_original_250ms_q0_40_80_signed_positive_lumped_cartesian_7pt_true_roof"])
        f=unpairs(fine["full_original_250ms_q0_40_80_signed_positive_lumped_cartesian_7pt_true_roof"])
        score=compare_complex_transfer(reference=pairs(f),candidate=pairs(c),
            frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
        gates=bool(score["complex_rms_relative"]<=.2
                   and score["magnitude_max_relative"]<=.25
                   and score["phase_max_deg"]<=15)
        out["original_full_q0_250ms_signed_adjacent_gates"].append({
            "coarse_ppw":coarse["ppw"],"fine_ppw":fine["ppw"],
            "complete_unchanged_record_signed_40_80_coarse_minus_fine":pairs(c-f),
            "original_frozen_complex_magnitude_phase":score,
            "all_three_original_gates_pass":gates})
        a.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n",encoding="utf8")
        print("SEVENPOINT_TRUE_ROOF_ORIGINAL_Q0_GATE",
              coarse["ppw"],fine["ppw"],score["complex_rms_relative"],
              score["magnitude_max_relative"],score["phase_max_deg"],gates,flush=True)
    scores=out["original_full_q0_250ms_signed_adjacent_gates"]
    keys=("complex_rms_relative","magnitude_max_relative","phase_max_deg")
    mono=all(all(scores[j]["original_frozen_complex_magnitude_phase"][k]<
                 scores[j-1]["original_frozen_complex_magnitude_phase"][k]
                 for k in keys) for j in range(1,4))
    verdict=all(p["all_three_original_gates_pass"] for p in scores) and mono
    out["five_grid_all_three_gates_and_strict_monotonicity"]={
        "all_four_pairs_pass":all(x["all_three_original_gates_pass"] for x in scores),
        "all_three_strictly_monotonic":mono,
        "all_acceptance_pass_experimental_only":verdict,
        "original_PFFDTD_requalification":False}
    a.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n",encoding="utf8")
    print("SEVENPOINT_TRUE_ROOF_ORIGINAL_Q0_VERDICT",out["five_grid_all_three_gates_and_strict_monotonicity"],flush=True)


if __name__=="__main__":
    main()
