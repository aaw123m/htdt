#!/usr/bin/env python3
"""Frozen full five native-HDF5 cut-roof CFL eigenvector/Rayleigh localization.

No new solver arm, q0 wave, PFFDTD source modification, mode deletion, or
unfavorable-grid exclusion. Preregistered before any real five-grid values.
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
from htdt.r130d_conforming_roof_p1_fem import build_original_native_conforming_roof_p1
from htdt.r130d_conforming_roof_p1_consistent_mass import (
    consistent_physical_P1_tensored_operators,generalized_symmetric_P1_consistent_modes)
from htdt.r130d_cartesian_true_roof_hybrid_flux import build_native_cartesian_true_roof_hybrid_flux
from htdt.r130d_cartesian_true_roof_lumped_sevenpoint import build_cartesian_sevenpoint_true_roof
from htdt.r130d_native_cut_roof_Q1_galerkin import (
    original_eightnode_native_HDF5_Q1_source_receiver,
    generalized_native_original_Q1_full_physical_neumann_modes)
from htdt.r130d_true_roof_cfl_eigenvector_localization import (
    fixed_physical_CFL_localization,CUT_THRESHOLDS,ROOF_MULTIPLIERS,
    TOP_EIGENCOUNT,TOP_NODECOUNT)
from run_r130d_original_point_quadratic_pffdtd import PIN,PPW,file_hash

PLAN_PATH="benchmarks/acoustics/r130d_original_q0_true_roof_sevenpoint_CFL_eigenvector_localization_plan_2026-10-10.json"
PLAN_SHA="34547c88234695b7eadc96b191531ef9fba2ae9f"
BRANCH="feat/r130d-embedded-neumann-fv-20261009"
PREVIOUS="benchmarks/acoustics/r130d_original_q0_true_roof_sevenpoint_native_leapfrog_stability_evidence_2026-10-10.json"


def frozen_plan(p):
    a=p["original"];b=p["operator"];c=p["predeclared_analysis"]
    if (p["schema_version"]!="htdt.r130d.original-q0-true-roof-sevenpoint-CFL-eigenvector-localization-plan-1"
        or p["repo"]!="aaw123m/htdt" or p["issue"]!=53 or p["draft_pr"]!=118
        or a["pffdtd_sha"]!=PIN or a["ppw"]!=list(PPW)
        or a["source_xyz_m"]!=[1.5,2,2] or a["receiver_xyz_m"]!=[2.5,2,2]
        or a["full_record_seconds"]!=.25 or a["signed_frequency_hz"]!=[40,80]
        or a["original_acceptance"]!={"complex_rms_relative":.2,
                "magnitude_max_relative":.25,"phase_max_deg":15,"strict_monotonicity":True}
        or not a["native_h_dt_Nt_and_8node_q0_and_8node_receiver_from_sha"]
        or not a["unchanged_native_original_graph_and_raw_hdf5"]
        or b["same_previous_7point_experiment_commit"]!="902f7b4a7a21d2500abb8db91b49b7bf426be0c9"
        or b["same_previous_full_CFL_evidence_commit"]!="63d74e7d85f4495b8e948ee97344eaf627d6e83d"
        or b["physical_volume_m3"]!=56 or not b["full_generalized_eigenspectra_no_cut"]
        or c["diagnostic_cumulative_cut_mass_thresholds"]!=list(CUT_THRESHOLDS)
        or c["geometric_roof_distance_abs_le_h_multiplier"]!=list(map(int,ROOF_MULTIPLIERS))
        or c["highest_yz_eigenvector_count"]!=TOP_EIGENCOUNT
        or c["highest_diagonal_Rayleigh_yz_nodes_count"]!=TOP_NODECOUNT
        or c["cut_yz_node_area_ratio_bins_to_native_h2"]!=[0,.01,.05,.10,.50,1.,2.]
        or p["caps"]!={"five_grids_only":True,"all_native_original_modes":True,
                        "max_yz_modes":3300,"max_3d_modes":180000,
                        "new_original_pffdtd_wave_runs":0,"new_github_actions":0,
                        "keep_scratch":True}
        or p["release"]!={"original_PFFDTD":"SELF_CONVERGENCE_FAILED",
                          "independent_physics":"NOT_VALIDATED","product":"NO_GO",
                          "issue_open":True,"PR_draft":True}):
        raise ValueError("FROZEN_ROOF_LOCALIZATION_PLAN_DRIFT")
    return p


def preregistration_on_live_GitHub():
    def git(*a):
        return subprocess.run(["git","-C",str(ROOT),*a],stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,check=False)
    record=git("show",PLAN_SHA+":"+PLAN_PATH)
    ok=git("merge-base","--is-ancestor",PLAN_SHA,"origin/"+BRANCH)
    branch=git("rev-parse","origin/"+BRANCH)
    live=git("ls-remote","origin","refs/heads/"+BRANCH)
    link=live.stdout.decode(errors="replace").split()
    data=(ROOT/PLAN_PATH).read_bytes().replace(b"\r\n",b"\n")
    if (record.returncode or ok.returncode or branch.returncode or live.returncode
        or len(link)!=2 or branch.stdout.decode().strip()!=link[0]
        or record.stdout.replace(b"\r\n",b"\n")!=data):
        raise RuntimeError("PREREGISTRATION_NOT_ON_LIVE_GITHUB_BEFORE_HDF5")
    return True


def diagnose_original_grid(ppw,folder,native,old,p):
    clock=time.perf_counter()
    if file_hash(folder/"comms_out.h5")!=native["original_native_comm_sha256"]:
        raise ValueError("original input/receiver q0 comms SHA drift")
    if file_hash(folder/"vox_out.h5")!=native["original_solver_geometry_sha256"]:
        raise ValueError("original geometry/roof voxel SHA drift")
    with h5py.File(folder/"vox_out.h5","r") as h:
        axes=[np.asarray(h[k][:],float) for k in ("xv","yv","zv")]
    dimensions=tuple(len(v) for v in axes)
    with h5py.File(folder/"comms_out.h5","r") as h:
        src=np.asarray(h["in_ixyz"][:],int)
        recv=np.asarray(h["out_ixyz"][:],int)
        sig=np.asarray(h["in_sigs"][:],float)
        rw=np.asarray(h["out_alpha"][:],float).ravel()
        Nt=int(h["Nt"][()])
        if (src.shape!=(8,) or recv.shape!=(8,) or sig.shape!=(8,Nt)
            or rw.shape!=(8,) or np.any(sig[:,1:]!=0)
            or abs(rw.sum()-1)>1e-12 or int(h["diff"][()])!=0):
            raise ValueError("original HDF5 8x8 point q0 native observer drift")
        source_strength=float(sig[:,0].sum())
        sw=sig[:,0]/source_strength
    with h5py.File(folder/"sim_consts.h5","r") as h:
        dt=float(h["Ts"][()]);spacing=float(h["h"][()])
        speed=float(h["c"][()]);l2=float(h["l2"][()])
    if (abs(speed-343.2)>1e-12 or abs(spacing-speed/(100*ppw))>1e-12
        or abs(source_strength-l2/spacing)>1e-10
        or Nt!=old["original_native_Nt"]
        or abs(dt-old["original_native_dt_s"])>1e-12):
        raise ValueError("native physical sampling/source normalization changed")
    fem=build_original_native_conforming_roof_p1(
        axes,max_yz_nodes=3000,max_3d_nodes=180000)
    mx,_,kx,_=consistent_physical_P1_tensored_operators(fem)
    hybrid=build_native_cartesian_true_roof_hybrid_flux(
        axes[1],axes[2],max_active_yz_nodes=p["caps"]["max_yz_modes"])
    z=build_cartesian_sevenpoint_true_roof(
        [fem.x_positions_m,axes[1],axes[2]],mx,kx,hybrid,c_m_s=speed)
    sx,sy,source_error=original_eightnode_native_HDF5_Q1_source_receiver(
        dimensions,fem.x_native_map,hybrid.cut_q1,src,sw,
        len(fem.x_positions_m))
    rx,ry,recv_error=original_eightnode_native_HDF5_Q1_source_receiver(
        dimensions,fem.x_native_map,hybrid.cut_q1,recv,rw,
        len(fem.x_positions_m))
    if max(source_error,recv_error)>1e-12:
        raise ValueError("native original 8node point q0 basis no longer exact")
    lx,ux,xproof=generalized_symmetric_P1_consistent_modes(z.physical.Mx,z.physical.Kx)
    ly,uy,yproof=generalized_native_original_Q1_full_physical_neumann_modes(
        z.physical.Myz,z.physical.Kyz)
    count=len(lx)*len(ly)
    if count>p["caps"]["max_3d_modes"] or len(ly)>p["caps"]["max_yz_modes"]:
        raise ValueError("full physical eigenbasis over frozen resource ceiling")
    lam=(lx[:,None]+ly[None,:]).ravel()
    unstable=int(np.count_nonzero(dt**2*lam>=4.))
    measured=float(dt**2*lam.max())
    if (count!=old["full_original_modal_count"] or
        unstable!=old["unstable_mode_count"] or
        abs(measured-old["native_dt2_lambda_max"])>2e-9):
        raise ValueError("previous published all-grid complete CFL evidence changed")
    local=fixed_physical_CFL_localization(
        z.physical.Mx,z.physical.Kx,z.physical.Myz,z.physical.Kyz,
        lx,ly,uy,hybrid.cut_q1.physical_active_yz_node_positions_m,
        spacing,dt)
    if abs(local["full_native_dt2_max_lambda"]-measured)>2e-9:
        raise ValueError("localization Rayleigh full eigenvalue not physical")
    return {
        "ppw":ppw,
        "native_comms_SHA":native["original_native_comm_sha256"],
        "native_voxel_SHA":native["original_solver_geometry_sha256"],
        "native_original_h_m":spacing,
        "native_original_dt_s":dt,
        "native_original_Nt":Nt,
        "original_8_source_and_8_receiver_interpolation_errors":[source_error,recv_error],
        "original_native_total_8point_q0_strength":source_strength,
        "all_original_full_modes_instability_count":unstable,
        "max_original_native_dt2_lambda":measured,
        "original_frozen_explicit_native_CFL_stable":False,
        "preobserved_complete_cfl_verified":True,
        "x_exact_generalized_mode_proof":xproof,
        "yz_exact_generalized_mode_proof":yproof,
        "full_raw_mass_sliver_and_roof_localization":local,
        "new_eigensolver_seconds":float(time.perf_counter()-clock)
    }


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--plan",type=Path,required=True)
    parser.add_argument("--original-sims-root",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    if args.plan.resolve()!=(ROOT/PLAN_PATH).resolve():
        raise ValueError("different post hoc preregistration plan")
    p=frozen_plan(json.loads(args.plan.read_text(encoding="utf8")))
    preregistration_on_live_GitHub()
    native=json.loads((ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json").read_text(encoding="utf8"))
    previous=json.loads((ROOT/PREVIOUS).read_text(encoding="utf8"))
    n={q["ppw"]:q for q in native["actual_native_wave_cases"]}
    old={q["ppw"]:q for q in previous["all_five_native_original_grid_CFL"]}
    if set(n)!=set(PPW) or set(old)!=set(PPW):
        raise ValueError("previous complete five original HDF5 SHA list missing")
    folders={}
    for f in args.original_sims_root.rglob("comms_out.h5"):
        digest=file_hash(f)
        matches=[k for k in PPW if n[k]["original_native_comm_sha256"]==digest]
        if len(matches)==1:
            if matches[0] in folders:raise ValueError("duplicate original SHA fixture")
            folders[matches[0]]=f.parent
    if set(folders)!=set(PPW):
        raise ValueError("all five pinned native original HDF5 source files not found")
    raw=args.plan.read_bytes().replace(b"\r\n",b"\n")
    out={"schema_version":"htdt.r130d.original-q0-true-roof-sevenpoint-CFL-eigenvector-localization-evidence-1",
         "preregistered_plan_sha256_lf":hashlib.sha256(raw).hexdigest(),
         "preregistered_plan":p,"preregistered_remote_commit":PLAN_SHA,
         "original_PFFDTD":"SELF_CONVERGENCE_FAILED",
         "independent_physics":"NOT_VALIDATED","product":"NO_GO",
         "new_original_pffdtd_waves":0,"new_github_actions_runs":0,
         "original_native_unmodified_250ms_q0_convergence_still_failed":True,
         "full_original_native_five_grid_cfl_eigenvector_results":[]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    for ppw in PPW:
        try:case=diagnose_original_grid(ppw,folders[ppw],n[ppw],old[ppw],p)
        except Exception as e:
            out["full_original_native_five_grid_cfl_eigenvector_results"].append({
                "ppw":ppw,"status":"INCOMPLETE","type":type(e).__name__,"detail":str(e)})
            args.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n",encoding="utf8")
            raise
        out["full_original_native_five_grid_cfl_eigenvector_results"].append(case)
        args.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n",encoding="utf8")
        f=case["full_raw_mass_sliver_and_roof_localization"]
        print("ORIGINAL_TRUE_ROOF_CFL_RAYLEIGH",ppw,
              "CFL",case["max_original_native_dt2_lambda"],
              "basisCFL",f["full_dt2_single_basis_Rayleigh_lowerbound"],
              "x_fraction",f["maximum_x_eigenvalue_fraction_of_full_max"],
              "roof1h",f["top_three_true_generalized_yz_eigenvector_localization"][0]["fractions"]["abs_true_roof_distance_le_1h"]["eigenvector_M_mass_fraction"],
              "minAreaRatio",f["min_yz_area_fraction_of_h2"],flush=True)
    out["entire_complete_eigenbasis_no_truncation"]=True
    out["original_native_all_five_unstable_no_unqualified_250ms_leapfrog_scores"]=True
    args.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n",encoding="utf8")
    print("ORIGINAL_TRUE_ROOF_CFL_ALL_FIVE_LOCALIZATION_COMPLETE",flush=True)

if __name__=="__main__":
    main()
