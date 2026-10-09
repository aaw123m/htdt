#!/usr/bin/env python3
"""Preregistered actual HDF5 allmode sevenpoint + original explicit q0 CFL audit.

Never score a truncated stable subset of modes or grids. One original native
timestep failing the CFL 4 bound aborts ALL new leapfrog signed-bin scoring.
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
from htdt.r130d_original_q0_leapfrog_modal_observer import original_native_leapfrog_full_modal
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_point_quadratic_pffdtd import PIN, PPW, file_hash
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs

PLAN_PATH="benchmarks/acoustics/r130d_original_q0_true_roof_sevenpoint_native_leapfrog_stability_plan_2026-10-10.json"
PLAN_SHA="98913220e22d50fe8f0a19454a2e26749e5f758f"
BRANCH="feat/r130d-embedded-neumann-fv-20261009"

def verify_plan(plan:dict):
    p=plan["frozen"]; o=plan["operator"];t=plan["temporal"]; v=plan["verification"]
    if (plan["schema_version"]!="htdt.r130d.original-q0-true-roof-sevenpoint-native-leapfrog-stability-plan-1"
        or plan["repo"]!="aaw123m/htdt" or plan["issue"]!=53 or plan["pr"]!=118
        or p["upstream_pffdtd_sha"]!=PIN or p["ppw"]!=list(PPW)
        or p["source_xyz"]!=[1.5,2,2] or p["receiver_xyz"]!=[2.5,2,2]
        or p["record_seconds"]!=.25 or p["frequency_hz"]!=[40,80]
        or p["c_m_s"]!=343.2 or p["rho_kg_m3"]!=1.2
        or p["acceptance"]!={"complex_rms_relative":.2,"magnitude_max_relative":.25,"phase_max_deg":15}
        or not p["all_four_adjacent_pairs"] or not p["strict_monotone_all_three"]
        or o["spatial_same_previous_7point_commit"]!="902f7b4a7a21d2500abb8db91b49b7bf426be0c9"
        or o["volume_m3"]!=56 or not o["full_native_3d_eigenmodes_no_cut"]
        or not o["all_positive_physical_support_nodes"]
        or "strictly below 4" not in t["cfl"] or "ANY grid fails" not in t["cfl"]
        or "no high-mode truncation" not in t["nyquist_rule"]
        or not all(v.values())
        or plan["limits"]!={"max_yz_modes":3300,"max_total_3d_modes":180000,
                            "new_pffdtd_waves":0,"new_github_actions":0,
                            "preserve_scratch":True}
        or plan["authority"]!={"original_PFFDTD":"SELF_CONVERGENCE_FAILED",
                                 "independent_physics":"NOT_VALIDATED",
                                 "product":"NO_GO","PR_draft":True,"issue_open":True}):
        raise ValueError("FROZEN_CFL_EXPERIMENT_PLAN_CHANGED")
    return plan

def require_remote_prereg():
    def run(*args):
        return subprocess.run(["git","-C",str(ROOT),*args],
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
    ref="origin/"+BRANCH
    show=run("show",PLAN_SHA+":"+PLAN_PATH)
    ancestor=run("merge-base","--is-ancestor",PLAN_SHA,ref)
    cached=run("rev-parse",ref)
    live=run("ls-remote","origin","refs/heads/"+BRANCH)
    advertised=live.stdout.decode(errors="replace").split()
    here=(ROOT/PLAN_PATH).read_bytes().replace(b"\r\n",b"\n")
    if (show.returncode or ancestor.returncode or cached.returncode
        or live.returncode or len(advertised)!=2
        or advertised[0]!=cached.stdout.decode().strip()
        or here!=show.stdout.replace(b"\r\n",b"\n")):
        raise RuntimeError("PREREGISTRATION_NOT_ON_GITHUB")
    return True

def inspect_grid(ppw,folder,native,baseline,plan):
    tick=time.perf_counter()
    if (file_hash(folder/"comms_out.h5")!=native["original_native_comm_sha256"]
        or file_hash(folder/"vox_out.h5")!=native["original_solver_geometry_sha256"]):
        raise ValueError("original native SHA mismatch")
    with h5py.File(folder/"vox_out.h5","r") as f:
        axes=[np.asarray(f[k][:],float) for k in ("xv","yv","zv")]
    dims=tuple(len(v) for v in axes)
    with h5py.File(folder/"comms_out.h5","r") as f:
        sidx=np.asarray(f["in_ixyz"][:],int);ridx=np.asarray(f["out_ixyz"][:],int)
        sig=np.asarray(f["in_sigs"][:],float);rw=np.asarray(f["out_alpha"][:],float).ravel()
        nt=int(f["Nt"][()])
        if (sidx.shape!=(8,) or ridx.shape!=(8,) or sig.shape!=(8,nt)
            or rw.shape!=(8,) or np.any(sig[:,1:]!=0)
            or int(f["diff"][()])!=0 or abs(rw.sum()-1)>1e-12):
            raise ValueError("original 8node/8node source q0 pressure observer mutated")
        strength=sig[:,0].sum();sw=sig[:,0]/strength
    with h5py.File(folder/"sim_consts.h5","r") as f:
        dt=float(f["Ts"][()]);h=float(f["h"][()])
        l2=float(f["l2"][()]);c=float(f["c"][()])
    if (abs(c-343.2)>1e-12 or abs(h-c/(100*ppw))>1e-12
        or abs(strength-l2/h)>1e-10
        or nt!=baseline["native_original_full_Nt"]
        or abs(dt-baseline["native_original_Ts_s"])>1e-12):
        raise ValueError("original native h,dt,Nt or q0 normalization drift")
    fem=build_original_native_conforming_roof_p1(
        axes,max_yz_nodes=3000,max_3d_nodes=180000)
    mx,_,kx,_=consistent_physical_P1_tensored_operators(fem)
    hy=build_native_cartesian_true_roof_hybrid_flux(
        axes[1],axes[2],max_active_yz_nodes=plan["limits"]["max_yz_modes"])
    z=build_cartesian_sevenpoint_true_roof(
        [fem.x_positions_m,axes[1],axes[2]],mx,kx,hy,c_m_s=c)
    source_x,source_y,serr=original_eightnode_native_HDF5_Q1_source_receiver(
        dims,fem.x_native_map,hy.cut_q1,sidx,sw,len(fem.x_positions_m))
    recv_x,recv_y,rerr=original_eightnode_native_HDF5_Q1_source_receiver(
        dims,fem.x_native_map,hy.cut_q1,ridx,rw,len(fem.x_positions_m))
    if max(serr,rerr)>1e-12 or abs(z.total_true_room_m3-56)>2e-8:
        raise ValueError("unchanged original physical source/receiver/roof volume")
    lx,ux,xproof=generalized_symmetric_P1_consistent_modes(z.physical.Mx,z.physical.Kx)
    ly,uy,yproof=generalized_native_original_Q1_full_physical_neumann_modes(
        z.physical.Myz,z.physical.Kyz)
    count=len(lx)*len(ly)
    if (count>plan["limits"]["max_total_3d_modes"]
        or count!=baseline["all_3D_original_native_physical_modes"]):
        raise ValueError("full original 3D modes or tiny roof sliver basis removed")
    eig=(lx[:,None]+ly[None,:]).ravel()
    coupling=np.outer((source_x@ux)*(recv_x@ux),
                      (source_y@uy)*(recv_y@uy)).ravel()
    if len(coupling)!=count:raise ValueError("source receiver full spectral dual lost")
    cfl_values=dt*dt*eig
    lammax=float(np.max(eig))
    largest=float(np.max(cfl_values))
    below=bool(largest<4.)
    return ({
        "ppw":ppw,"full_original_modal_count":count,
        "original_hdf5_comms_sha256":native["original_native_comm_sha256"],
        "original_hdf5_vox_sha256":native["original_solver_geometry_sha256"],
        "original_native_dt_s":dt,"original_native_h_m":h,"original_native_Nt":nt,
        "original_original_q0_total_strength":float(strength),
        "roof_volume_m3":z.total_true_room_m3,
        "original_7point_interior_relative":z.seven_neighbor_stencil_max_relative,
        "true_neumann_constant_relative":float(max(abs(z.stiffness@np.ones(count)))/
            max(1.,max(abs(z.stiffness.diagonal())))),
        "x_complete_mode_proof":xproof,"yz_complete_mode_proof":yproof,
        "largest_3d_semidiscrete_eigenvalue_per_s2":lammax,
        "native_dt2_lambda_max":largest,
        "native_explicit_leapfrog_threshold":4.,
        "native_explicit_leapfrog_stable_all_modes":below,
        "unstable_mode_count":int(np.count_nonzero(cfl_values>=4.)),
        "slowest_mode_lambda":float(np.min(eig)),
        "highest_original_native_leapfrog_timestep_safe_s":
            float(2./np.sqrt(lammax)),
        "relative_original_native_dt_to_CFL_dt_max":
            float(dt*np.sqrt(lammax)/2),
        "original_unmodified_source_and_observer":True,
        "seconds":float(time.perf_counter()-tick)
    },eig,coupling,dt,nt)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    a=ap.parse_args()
    if a.plan.resolve()!=(ROOT/PLAN_PATH).resolve():
        raise ValueError("not previously pushed fixed plan")
    p=verify_plan(json.loads(a.plan.read_text(encoding="utf8")))
    require_remote_prereg()
    original=json.loads((ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json").read_text())
    prior=json.loads((ROOT/"benchmarks/acoustics/r130d_original_q0_cartesian_roof_row_lumped_mass_evidence_2026-10-10.json").read_text())
    native={q["ppw"]:q for q in original["actual_native_wave_cases"]}
    old={q["ppw"]:q for q in prior["real_original_native_8node_q0_sevenpoint_lumped_mass_cases"]}
    if set(native)!=set(PPW) or set(old)!=set(PPW):
        raise ValueError("missing 5 native SHA-pinned cases")
    dirs={}
    for filename in a.original_sims_root.rglob("comms_out.h5"):
        sha=file_hash(filename)
        pp=[k for k in PPW if native[k]["original_native_comm_sha256"]==sha]
        if len(pp)==1:
            if pp[0] in dirs:raise ValueError("duplicate original SHA source")
            dirs[pp[0]]=filename.parent
    if set(dirs)!=set(PPW):raise ValueError("missing 5 untouched native source HDF5")
    raw=a.plan.read_bytes().replace(b"\r\n",b"\n")
    e={"schema_version":"htdt.r130d.original-q0-true-roof-sevenpoint-native-leapfrog-cfl-evidence-1",
       "preregistered_plan":p,"preregistered_GitHub_commit":PLAN_SHA,
       "plan_sha256_LF":hashlib.sha256(raw).hexdigest(),
       "canonical_original_PFFDTD":"SELF_CONVERGENCE_FAILED",
       "independent_physics":"NOT_VALIDATED","product":"NO_GO",
       "new_original_PFFDTD_waves":0,"github_actions_runs":0,
       "all_five_native_original_grid_CFL":[],"full_signed_leapfrog_40_80_if_ALL_stable":[],
       "all_original_four_adjacent_refinement_scores_if_ALL_stable":[]}
    cache={}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    for ppw in PPW:
        try:
            case,lam,coupling,dt,nt=inspect_grid(ppw,dirs[ppw],native[ppw],old[ppw],p)
        except Exception as ex:
            e["all_five_native_original_grid_CFL"].append(
                {"ppw":ppw,"status":"INCOMPLETE",
                 "exception_type":type(ex).__name__,"exception":str(ex)})
            a.output.write_text(json.dumps(e,indent=2,allow_nan=False)+"\n",encoding="utf8")
            raise
        cache[ppw]=(lam,coupling,dt,nt)
        e["all_five_native_original_grid_CFL"].append(case)
        a.output.write_text(json.dumps(e,indent=2,allow_nan=False)+"\n",encoding="utf8")
        print("ORIGINAL_NATIVE_LEAPFROG_SEVENPOINT_CFL",ppw,
              "dt2lambda",case["native_dt2_lambda_max"],
              "stable",case["native_explicit_leapfrog_stable_all_modes"],
              "unstableModes",case["unstable_mode_count"],
              "allModes",case["full_original_modal_count"],flush=True)
    all_stable=all(x["native_explicit_leapfrog_stable_all_modes"]
                   for x in e["all_five_native_original_grid_CFL"])
    if not all_stable:
        e["five_grid_native_leapfrog_status"]="UNSTABLE_AT_ORIGINAL_DT"
        e["no_unstable_high_mode_omission"]=True
        e["no_unstable_alternate_full_250ms_scoring"]=True
        e["original_frozen_convergence_still_failed"]=True
        a.output.write_text(json.dumps(e,indent=2,allow_nan=False)+"\n",encoding="utf8")
        print("ORIGINAL_NATIVE_LEAPFROG_CFL_VERDICT",
              "UNSTABLE_AT_ORIGINAL_DT; NO 250ms CHERRY PICKING",flush=True)
        return
    e["five_grid_native_leapfrog_status"]="STABLE_ALL_FIVE_AT_ORIGINAL_DT"
    for ppw in PPW:
        lam,coup,dt,nt=cache[ppw]
        spectrum=original_native_leapfrog_full_modal(lam,coup,dt,nt)
        value=spectrum.sum(axis=1)
        e["full_signed_leapfrog_40_80_if_ALL_stable"].append({
            "ppw":ppw,"original_signed_full_250ms_40_80":pairs(value),
            "unchanged_native_Nt":nt,"all_modes_retained":len(lam)})
    for coarse,fine in zip(e["full_signed_leapfrog_40_80_if_ALL_stable"],
                           e["full_signed_leapfrog_40_80_if_ALL_stable"][1:]):
        c=unpairs(coarse["original_signed_full_250ms_40_80"])
        f=unpairs(fine["original_signed_full_250ms_40_80"])
        metrics=compare_complex_transfer(
            reference=pairs(f),candidate=pairs(c),frequency_hz=[40,80],
            magnitude_mask_relative_db=-50).model_dump(mode="json")
        e["all_original_four_adjacent_refinement_scores_if_ALL_stable"].append({
            "ppw_coarse":coarse["ppw"],"ppw_fine":fine["ppw"],
            "original_frozen_40_80_full_record_three_metrics":metrics,
            "three_original_gates_pass":bool(metrics["complex_rms_relative"]<=.2
                and metrics["magnitude_max_relative"]<=.25 and metrics["phase_max_deg"]<=15)})
    metrics=("complex_rms_relative","magnitude_max_relative","phase_max_deg")
    scores=e["all_original_four_adjacent_refinement_scores_if_ALL_stable"]
    monotonic=all(all(scores[i]["original_frozen_40_80_full_record_three_metrics"][m]<
                       scores[i-1]["original_frozen_40_80_full_record_three_metrics"][m]
                       for m in metrics) for i in range(1,len(scores)))
    e["experimental_native_leapfrog_full_q0_acceptance"]={
        "all_four_three_gates":all(s["three_original_gates_pass"] for s in scores),
        "strict_monotone_three_metrics":monotonic,
        "original_PFFDTD_requalified":False}
    e["original_frozen_convergence_still_failed"]=True
    a.output.write_text(json.dumps(e,indent=2,allow_nan=False)+"\n",encoding="utf8")
    print("ORIGINAL_NATIVE_LEAPFROG_CFL_VERDICT",e["experimental_native_leapfrog_full_q0_acceptance"],flush=True)

if __name__=="__main__":main()
