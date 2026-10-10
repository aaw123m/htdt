#!/usr/bin/env python3
"""Prospective five-grid uniform physical x-P1 + true cut-roof yz q0 arm."""
from __future__ import annotations
import argparse, hashlib, json, subprocess, sys, time
from pathlib import Path
import h5py
import numpy as np
from scipy import sparse

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend/src"))
from htdt.r130d_uniform_boundary_fitted_x_p1 import (
    build_uniform_boundary_fitted_x_p1,project_original_8point_HDF5_to_fitted_x)
from htdt.r130d_cartesian_true_roof_hybrid_flux import build_native_cartesian_true_roof_hybrid_flux
from htdt.r130d_conforming_roof_p1_consistent_mass import generalized_symmetric_P1_consistent_modes
from htdt.r130d_native_cut_roof_Q1_galerkin import generalized_native_original_Q1_full_physical_neumann_modes
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_q0_cartesian_flux_true_roof_hybrid import fixed_first_roof_weak_full_modes
from run_r130d_native_exact_roof_full_xy_z_modal_q0 import entire_original_finite_record_signed_modes
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs
from run_r130d_original_point_quadratic_pffdtd import PIN,PPW,file_hash

PLAN_PATH="benchmarks/acoustics/r130d_original_q0_boundary_fitted_uniform_x_true_roof_plan_2026-10-10.json"
PLAN_SHA="61c0de9d664280b42cf16f97eb579d0d976dc741"
BRANCH="feat/r130d-embedded-neumann-fv-20261009"
OLD_CASE_PATH="benchmarks/acoustics/r130d_original_q0_cartesian_roof_row_lumped_mass_evidence_2026-10-10.json"
NATIVE_CASE_PATH="benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json"

def validate_plan(p):
    orig=p["original"];a=p["new_operator"];c=p["predeclared_checks"]
    if (p["schema_version"]!="htdt.r130d.original-q0-boundary-fitted-uniform-x-true-roof-plan-1"
        or p["repo"]!="aaw123m/htdt" or p["issue"]!=53 or p["draft_pr"]!=118
        or orig["pffdtd_sha"]!=PIN or orig["native_ppw"]!=list(PPW)
        or orig["source_xyz_m"]!=[1.5,2,2] or orig["receiver_xyz_m"]!=[2.5,2,2]
        or orig["full_record_s"]!=.25 or orig["frequencies_signed_hz"]!=[40,80]
        or orig["c_m_s"]!=343.2 or orig["rho"]!=1.2
        or orig["acceptance"]!={"complex_rms_relative":.2,"magnitude_max_relative":.25,"phase_max_deg":15}
        or orig["all_four_ppw_pairs"]!=[[28,32],[32,36],[36,40],[40,44]]
        or not orig["strict_monotone_all_three"] or not orig["native_h_Ts_Nt_each_grid"]
        or not orig["original_runnable_wave_file_SHA_required"]
        or a["name"]!="fixed_uniform_boundary_fitted_P1_x_plus_exact_original_native_true_roof_cut_Q1_5point_yz_positive_mass"
        or a["roof_volume_m3"]!=56 or "N=ceil(4/native_h)" not in a["x_segments_rule"]
        or not a["manufactured_neumann_constant"] or not a["exact_affine_x_and_roof_weak_energy"]
        or a["forbidden"]!=["high_mode_cut","mass_floor","rooftop_sliver_dropping",
                              "original_signal_edit","original_native_HDF5_edit",
                              "time_window_edit","phase_fit","level_fit",
                              "grid_specific_x_N_tuning","post_hoc_better_grid_select"]
        or not all(c.values())
        or p["resource"]!={"max_yz_modes":3300,"max_total_modes":180000,
                           "new_upstream_PFFDTD_waves":0,"github_actions":0,
                           "retain_scratch":True}
        or p["authority"]!={"original_PFFDTD":"SELF_CONVERGENCE_FAILED",
                            "independent_physics":"NOT_VALIDATED","product":"NO_GO",
                            "issue_open":True,"PR_draft":True}):
        raise ValueError("FROZEN_UNIFORM_X_TRUE_ROOF_PLAN_DRIFT")
    return p

def ensure_GitHub_preregistered():
    def git(*args):return subprocess.run(
        ["git","-C",str(ROOT),*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
    local=git("show",PLAN_SHA+":"+PLAN_PATH)
    ances=git("merge-base","--is-ancestor",PLAN_SHA,"origin/"+BRANCH)
    head=git("rev-parse","origin/"+BRANCH)
    live=git("ls-remote","origin","refs/heads/"+BRANCH)
    parsed=live.stdout.decode(errors="replace").split()
    file=(ROOT/PLAN_PATH).read_bytes().replace(b"\r\n",b"\n")
    if (local.returncode or ances.returncode or head.returncode
        or live.returncode or len(parsed)!=2
        or parsed[0]!=head.stdout.decode().strip()
        or local.stdout.replace(b"\r\n",b"\n")!=file):
        raise RuntimeError("PREREGISTRATION_NOT_PUSHED_BEFORE_REAL_NATIVE_HDF5")
    return True

def one_grid(ppw,folder,native,old,plan):
    t=time.perf_counter()
    if (file_hash(folder/"comms_out.h5")!=native["original_native_comm_sha256"]
        or file_hash(folder/"vox_out.h5")!=native["original_solver_geometry_sha256"]):
        raise ValueError("original SHA native 8node q0 or roof drift")
    with h5py.File(folder/"vox_out.h5","r") as f:
        axes=[np.asarray(f[k][:],float) for k in ("xv","yv","zv")]
    dims=tuple(map(len,axes))
    with h5py.File(folder/"comms_out.h5","r") as f:
        si=np.asarray(f["in_ixyz"][:],int)
        ri=np.asarray(f["out_ixyz"][:],int)
        sig=np.asarray(f["in_sigs"][:],float)
        rw=np.asarray(f["out_alpha"][:],float).ravel()
        nt=int(f["Nt"][()])
        if (si.shape!=(8,) or ri.shape!=(8,) or rw.shape!=(8,)
            or sig.shape!=(8,nt) or np.any(sig[:,1:]!=0)
            or int(f["diff"][()])!=0 or abs(rw.sum()-1)>1e-12):
            raise ValueError("native original q0 / 8 source+8 receiver modified")
        source_strength=float(sig[:,0].sum())
        sw=sig[:,0]/source_strength
    with h5py.File(folder/"sim_consts.h5","r") as f:
        dt=float(f["Ts"][()]);h=float(f["h"][()])
        c=float(f["c"][()]);l2=float(f["l2"][()])
    if (abs(c-343.2)>1e-12 or abs(h-c/(100*ppw))>1e-12
        or abs(source_strength-l2/h)>1e-10
        or nt!=old["native_original_full_Nt"]
        or abs(dt-old["native_original_Ts_s"])>1e-12):
        raise ValueError("native original physical h/Ts/Nt/source impulse changed")
    fitted=build_uniform_boundary_fitted_x_p1(h)
    hy=build_native_cartesian_true_roof_hybrid_flux(
        axes[1],axes[2],max_active_yz_nodes=plan["resource"]["max_yz_modes"])
    y=hy.cut_q1
    wy=np.asarray(hy.mass.sum(axis=1)).ravel()
    if np.min(wy)<=0 or abs(wy.sum()-14)>1e-8:
        raise ValueError("physical native positive yz roof cut supports not retained")
    My=sparse.diags(wy,format="csr")
    sx,sy,srcproof=project_original_8point_HDF5_to_fitted_x(
        axes,si,sw,fitted,y,(1.5,2.,2.))
    rx,ry,recproof=project_original_8point_HDF5_to_fitted_x(
        axes,ri,rw,fitted,y,(2.5,2.,2.))
    # Manufactured continuous Neumann tangent to true roof
    yzcoord=y.physical_active_yz_node_positions_m
    u=yzcoord[:,0]-.25*yzcoord[:,1]
    dist=np.abs(4.-.25*yzcoord[:,0]-yzcoord[:,1])
    near=(dist<2*h)&(yzcoord[:,0]>2*h)&(yzcoord[:,0]<4-2*h)&(yzcoord[:,1]>2*h)
    roof_weak=float(np.max(abs((hy.stiffness@u)[near])))
    if roof_weak>1e-7:raise ValueError("true inclined-roof Neumann affine residual changed")
    u_x=fitted.nodes_m
    x_affine=float(u_x@(fitted.Kx@u_x))
    x_expected=4*c*c
    if abs(x_affine-x_expected)/x_expected>2e-9:
        raise ValueError("uniform true physical Neumann x affine energy mismatch")
    lx,vx,xproof=generalized_symmetric_P1_consistent_modes(fitted.Mx,fitted.Kx)
    ly,vy,yproof=generalized_native_original_Q1_full_physical_neumann_modes(My,hy.stiffness)
    modes=len(lx)*len(ly)
    if (modes>plan["resource"]["max_total_modes"] or len(ly)!=old["all_positive_native_yz_Q1_support_nodes"]):
        raise ValueError("original native physically supported sliver mode removed")
    lam=(lx[:,None]+ly[None,:]).ravel()
    coupling=np.outer((sx@vx)*(rx@vx),(sy@vy)*(ry@vy)).ravel()
    kick=dt**2*c*c*coupling/(1+dt**2*lam/4.)
    q=entire_original_finite_record_signed_modes(lam,kick,dt,nt,1.2).sum(axis=1)
    if not np.isfinite(q).all():raise ValueError("original full q0 signed Newmark nonfinite")
    early=fixed_first_roof_weak_full_modes(
        lam,kick,dt,nt,axes,dims,si,sw,ri,rw)
    prior=unpairs(old["full_original_250ms_q0_40_80_signed_positive_lumped_cartesian_7pt_true_roof"])
    full_CFL=float(dt**2*max(lam))
    original_mass_x=float(np.min(fitted.Mx.diagonal()))
    return {
        "ppw":ppw,"original_source_SHA":native["original_native_comm_sha256"],
        "original_geometry_SHA":native["original_solver_geometry_sha256"],
        "native_h":h,"native_dt":dt,"native_Nt":nt,"original_native_8_q0_strength":source_strength,
        "uniform_x_segment_count":fitted.segments,
        "uniform_x_node_count":len(fitted.nodes_m),
        "uniform_x_pitch":fitted.dx_m,
        "uniform_endpoint_min_physical_mass_length":original_mass_x,
        "exact_uniform_x_end_half_segment_support":abs(original_mass_x-fitted.dx_m/2)<1e-12,
        "unchanged_native_yz_Q1_positive_physical_nodes":len(ly),
        "full_3d_allmode_count":modes,
        "true_total_room_volume_m3":float(fitted.Mx.sum())*float(My.sum()),
        "x_affine_true_Neumann_relative":abs(x_affine-x_expected)/x_expected,
        "roof_manufactured_tangent_weak_absolute":roof_weak,
        "original_8_source_physical_moment_projection":srcproof,
        "original_8_receiver_physical_moment_projection":recproof,
        "all_x_generalized_eigensystem_proof":xproof,
        "all_yz_generalized_eigensystem_proof":yproof,
        "full_native_dt2_lambda_max":full_CFL,
        "native_full_explicit_leapfrog_stable":bool(full_CFL<4),
        "unstable_complete_mode_count":int(np.count_nonzero(dt**2*lam>=4.)),
        "x_native_dt2_lambda_max":float(dt**2*lx[-1]),
        "yz_native_dt2_lambda_max":float(dt**2*ly[-1]),
        "previous_x_end_lumped_mass_native_CFL":old["all_original_native_modes_above_nyquist_kept"],
        "complete_original_signed_250ms_40_80":pairs(q),
        "previous_original_row_lumped_native_x_signed_250ms_40_80":pairs(prior),
        "independent_first_true_roof_echo_analytic_3width":early,
        "all_spatial_modes_and_true_physical_support_kept":True,
        "experimental_x_P1_reprojection_changes_discrete_operator_not_original_authority":True,
        "elapsed_s":float(time.perf_counter()-t)}

def main():
    a=argparse.ArgumentParser()
    a.add_argument("--plan",type=Path,required=True)
    a.add_argument("--original-sims-root",type=Path,required=True)
    a.add_argument("--output",type=Path,required=True)
    args=a.parse_args()
    if args.plan.resolve()!=(ROOT/PLAN_PATH).resolve():
        raise ValueError("only live remotely frozen plan valid")
    p=validate_plan(json.loads(args.plan.read_text(encoding="utf8")))
    ensure_GitHub_preregistered()
    native=json.loads((ROOT/NATIVE_CASE_PATH).read_text(encoding="utf8"))
    old=json.loads((ROOT/OLD_CASE_PATH).read_text(encoding="utf8"))
    originals={r["ppw"]:r for r in native["actual_native_wave_cases"]}
    previous={r["ppw"]:r for r in old["real_original_native_8node_q0_sevenpoint_lumped_mass_cases"]}
    if set(originals)!=set(PPW) or set(previous)!=set(PPW):
        raise ValueError("native SHA/previous five-grid frozen evidence incomplete")
    folders={}
    for f in args.original_sims_root.rglob("comms_out.h5"):
        hashed=file_hash(f)
        hit=[ppw for ppw in PPW if hashed==originals[ppw]["original_native_comm_sha256"]]
        if len(hit)==1:
            if hit[0] in folders:raise ValueError("duplicate original HDF5 SHA")
            folders[hit[0]]=f.parent
    if set(folders)!=set(PPW):
        raise ValueError("five original native HDF5 not present")
    payload={"schema_version":"htdt.r130d.original-q0-uniform-physical-x-P1-true-roof-fivegrid-evidence-1",
        "frozen_remote_plan_sha":PLAN_SHA,"preregistered_plan":p,
        "plan_SHA256_LF":hashlib.sha256(args.plan.read_bytes().replace(b"\r\n",b"\n")).hexdigest(),
        "original_PFFDTD":"SELF_CONVERGENCE_FAILED",
        "independent_physics":"NOT_VALIDATED","product":"NO_GO",
        "new_native_original_PFFDTD_waves":0,"new_github_actions":0,
        "five_native_original_sha_fitted_x_cases":[],
        "four_adjacent_complete_original_250ms_three_gate_scores":[]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    def save():args.output.write_text(json.dumps(payload,indent=2,allow_nan=False)+"\n",encoding="utf8")
    for ppw in PPW:
        try:cur=one_grid(ppw,folders[ppw],originals[ppw],previous[ppw],p)
        except Exception as ex:
            payload["five_native_original_sha_fitted_x_cases"].append({
                "ppw":ppw,"status":"INCOMPLETE","error_type":type(ex).__name__,"error":str(ex)})
            save()
            raise
        payload["five_native_original_sha_fitted_x_cases"].append(cur);save()
        print("UNIFORM_X_TRUE_ROOF_ORIGINAL_Q0",ppw,
              "x_nodes",cur["uniform_x_node_count"],
              "dt2lambda",cur["full_native_dt2_lambda_max"],
              "signed",cur["complete_original_signed_250ms_40_80"],flush=True)
    cs=payload["five_native_original_sha_fitted_x_cases"]
    for x,y in zip(cs,cs[1:]):
        c=unpairs(x["complete_original_signed_250ms_40_80"])
        f=unpairs(y["complete_original_signed_250ms_40_80"])
        z=compare_complex_transfer(reference=pairs(f),candidate=pairs(c),
             frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
        gate=bool(z["complex_rms_relative"]<=.2 and z["magnitude_max_relative"]<=.25
                  and z["phase_max_deg"]<=15)
        payload["four_adjacent_complete_original_250ms_three_gate_scores"].append({
            "coarse_ppw":x["ppw"],"fine_ppw":y["ppw"],"metrics":z,
            "all_original_three_gates_pass":gate})
        save()
        print("UNIFORM_X_TRUE_ROOF_Q0_GATE",x["ppw"],y["ppw"],
              z["complex_rms_relative"],z["magnitude_max_relative"],
              z["phase_max_deg"],gate,flush=True)
    gate=payload["four_adjacent_complete_original_250ms_three_gate_scores"]
    metrics=["complex_rms_relative","magnitude_max_relative","phase_max_deg"]
    mono=all(all(gate[k]["metrics"][m]<gate[k-1]["metrics"][m]
                 for m in metrics) for k in range(1,len(gate)))
    payload["frozen_full_fivegrid_acceptance_experimental_only"]={
        "all_four_adjacent_three_gates":all(t["all_original_three_gates_pass"] for t in gate),
        "all_three_metrics_strict_monotone":mono,
        "experiment_only_all_acceptance_pass":all(t["all_original_three_gates_pass"] for t in gate) and mono,
        "original_upstream_PFFDTD_requalified":False}
    save()
    print("UNIFORM_X_TRUE_ROOF_ORIGINAL_Q0_VERDICT",
          payload["frozen_full_fivegrid_acceptance_experimental_only"],flush=True)

if __name__=="__main__":main()
