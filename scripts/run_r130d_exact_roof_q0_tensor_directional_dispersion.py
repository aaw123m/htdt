#!/usr/bin/env python3
"""Prospective exact-roof original point q0 full-mode directional dispersion A/B."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
import sys

import h5py
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
from htdt.r130d_native_grid_exact_roof_fv import build_native_exact_roof_fv
from htdt.r130d_native_exact_roof_separable import (
    original_native_xy_z_factorization, prove_native_full_kronecker_equal,
    generalized_neumann_modes)
from htdt.r130d_tensor_directional_dispersion import (
    ARMS,directional_eigenvalues,directional_stiffness)
from htdt.r130d_conservative_dispersion_correction import corrected_neumann_stiffness
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_native_exact_roof_full_xy_z_modal_q0 import (
    entire_original_finite_record_signed_modes)
from run_r130d_native_exact_roof_xy_z_separable_modal_q0 import source_receiver_tensor_projections
from run_r130d_original_point_quadratic_pffdtd import PIN,file_hash
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs

SCHEMA="htdt.r130d.exact-roof-q0-tensor-directional-kmk-plan-1"
def validate_plan(p):
    i=p["original_provenance"];o=p["operator"];t=p["temporal"]
    e=p["evaluation"]
    if (p.get("schema_version")!=SCHEMA or p["issue"]!=938 or p["pr"]!=1055
        or i["upstream_pffdtd_sha"]!=PIN or i["ppw"]!=[40,44]
        or i["physical_source_m"]!=[1.5,2,2]
        or i["physical_receiver_m"]!=[2.5,2,2]
        or i["physical_roof_volume_m3"]!=56
        or i["original_q0"]!="q0=1; q[n>0]=0, unmodified native HDF5 source weights"
        or not i["original_native_eight_source_receiver"] or not i["original_native_dt_nt"]
        or i["target_frequency_hz"]!=[40,80] or i["full_record_s"]!=.25
        or i["sound_speed_m_s"]!=343.2 or i["density_kg_m3"]!=1.2
        or o["alpha_fraction"]!=1/12 or o["arms"]!=list(ARMS)
        or not all(o[k] for k in ("all_true_modes","original_mass_faces_fixed",
               "original_point_weights_fixed","true_sparse_operator_and_null_eigenresidual_check",
               "full_operator_matches_previous_3d_kmk"))
        or not t["original_q0_unchanged"] or not t["original_dt_nt_unchanged"]
        or not t["no_mode_filter_or_damping_or_taper_or_smoothing"]
        or t["frequency_hz"]!=[40,80]
        or [e[k] for k in ("complex_limit","magnitude_limit","phase_deg_limit",
            "baseline_vs_archived_cg_relative_max","full_kmk_vs_prior_modal_relative_max",
            "true_3d_sparse_generalized_mode_residual_max",
            "constant_neumann_null_relative_max")]!=[.2,.25,15,2e-5,2e-8,2e-5,1e-8]
        or not e["report_every_signed_transfer_and_adverse_bin"]
        or not e["no_continuum_or_canonical_promotion_from_two_grids"]
        or p["limits"]!={"max_cases":2,"max_arms":5,
             "new_original_pffdtd_wave_runs":0,"new_github_actions_runs":0,
             "clean_scratch":False}
        or p["authority"]["original_q0"]!="SELF_CONVERGENCE_FAILED"
        or p["authority"]["external_physics"]!="NOT_VALIDATED"
        or p["authority"]["product"]!="NO_GO"):
        raise ValueError("prospective directional full-q0 dispersion plan changed")
    return p


def one_grid(p, ppw, directory, original, prior_roof, previous_kmk):
    stamp=time.perf_counter()
    if file_hash(directory/"comms_out.h5")!=original["original_native_comm_sha256"] or (
       file_hash(directory/"vox_out.h5")!=original["original_solver_geometry_sha256"]):
        raise ValueError("frozen original native point and voxel SHA changed")
    with h5py.File(directory/"vox_out.h5","r") as h:
        axes=[np.asarray(h[k][...],dtype=np.float64) for k in ("xv","yv","zv")]
    with h5py.File(directory/"comms_out.h5","r") as h:
        si=np.asarray(h["in_ixyz"][:],dtype=np.int64)
        ri=np.asarray(h["out_ixyz"][:],dtype=np.int64)
        sig=np.asarray(h["in_sigs"][:],dtype=float)
        rw=np.asarray(h["out_alpha"][:],dtype=float).ravel()
        nt=int(h["Nt"][()])
        if si.shape!=(8,) or ri.shape!=(8,) or rw.shape!=(8,) or (
            sig.shape!=(8,nt) or np.any(sig[:,1:]!=0) or int(h["diff"][()])!=0
            or abs(sum(rw)-1)>1e-12):
            raise ValueError("original q0 8node point input/observation changed")
        strength=float(sig[:,0].sum())
        sw=sig[:,0]/strength
    with h5py.File(directory/"sim_consts.h5","r") as h:
        dt=float(h["Ts"][()]);hm=float(h["h"][()])
        c=float(h["c"][()]);l2=float(h["l2"][()])
    if (abs(hm-c/(100*ppw))>1e-12 or abs(c-343.2)>1e-12
        or abs(strength-l2/hm)>1e-10 or abs(sum(sw)-1)>1e-12
        or abs(dt-prior_roof["original_native_dt_s"])>1e-12
        or nt!=prior_roof["original_record_nt"]):
        raise ValueError("original q0 clock and PFFDTD source scaling changed")
    full=build_native_exact_roof_fv(axes,max_nodes=150000)
    sep=original_native_xy_z_factorization(axes)
    geomcheck=prove_native_full_kronecker_equal(full,sep)
    if (sep.total_cells!=prior_roof["total_full_untruncated_3D_native_modes"]
        or abs(full.room_fluid_volume_m3-56)>2e-8):
        raise ValueError("real original full exact-roof physical volume/modes changed")
    src_receiver=source_receiver_tensor_projections(
        axes,sep,(si,sw),(ri,rw))
    lx,vx,_=generalized_neumann_modes(sep.Mx,sep.Kx)
    ly,vy,_=generalized_neumann_modes(sep.Myz,sep.Kyz)
    interaction=np.outer(
        (vx.T@src_receiver["source"]["x"])*(vx.T@src_receiver["receiver"]["x"]),
        (vy.T@src_receiver["source"]["yz"])*(vy.T@src_receiver["receiver"]["yz"]))
    lambdas=directional_eigenvalues(lx,ly,h_m=hm,c_m_s=c)
    transfers={}
    numerical={}
    test_mode=np.outer(vx[:,1],vy[:,1]).ravel(order="C")
    refmass=full.mass_matrix@test_mode
    test_scale=max(float(np.linalg.norm(refmass)),1e-20)
    for arm in ARMS:
        spectrum=lambdas[arm]
        amplitude=(dt*dt*c*c*interaction/(1+dt*dt*spectrum/4)).ravel()
        signed=entire_original_finite_record_signed_modes(
            spectrum.ravel(),amplitude,dt,nt,1.2)
        transfer=signed.sum(axis=1)
        transfers[arm]=pairs(transfer)
        stiffness=directional_stiffness(sep,arm=arm,c_m_s=c)
        eig=float(spectrum[1,1])
        res=float(np.linalg.norm(stiffness@test_mode-eig*refmass)/
                  max(eig*test_scale,1e-24))
        null=float(np.max(np.abs(stiffness@np.ones(sep.total_cells)))/
                   max(float(np.abs(stiffness.diagonal()).max()),1.0))
        if (res>p["evaluation"]["true_3d_sparse_generalized_mode_residual_max"]
            or null>p["evaluation"]["constant_neumann_null_relative_max"]):
            raise ValueError(f"true sparse full 3D {arm} failed eig/null checks {res} {null}")
        numerical[arm]={"true_3D_sparse_nonzero_count":int(stiffness.nnz),
            "true_3D_eigenresidual_relative":res,
            "rigid_neumann_constant_null_residual_relative":null}
        if arm=="full_kmk":
            previous=corrected_neumann_stiffness(full.mass_matrix,full.stiffness_matrix,
                h_m=hm,sound_speed_m_s=c)
            diff=(previous-stiffness).tocsr()
            maxdiff=float(max(abs(diff.data),default=0.))
            scale=max(float(np.abs(previous.data).max()),1.0)
            if maxdiff/scale>1e-10:
                raise ValueError(f"full-KmkK physical Kronecker identity rejected {maxdiff/scale}")
            numerical[arm]["matches_independently_assembled_full_3D_KmkK_relative_max"]=maxdiff/scale
    baseline=unpairs(transfers["baseline"])
    prior=unpairs(prior_roof["archived_direct_full_wave_250ms_signed_40_80"])
    baserel=float(np.linalg.norm(baseline-prior)/max(np.linalg.norm(prior),1e-14))
    if baserel>p["evaluation"]["baseline_vs_archived_cg_relative_max"]:
        raise ValueError("original frozen full wave baseline drift")
    actualfull=unpairs(transfers["full_kmk"])
    prevfull=unpairs(previous_kmk["full_untruncated_source_receiver_q0_signed_two_bin_by_arm"][
        "conservative_kmk_dispersion_newmark"])
    relative=float(np.linalg.norm(actualfull-prevfull)/max(np.linalg.norm(prevfull),1e-14))
    if relative>p["evaluation"]["full_kmk_vs_prior_modal_relative_max"]:
        raise ValueError(f"registered full correction no longer reproduces original KmkK {relative}")
    return {"ppw":ppw,"unmodified_original_source_sha256":file_hash(directory/"comms_out.h5"),
        "unmodified_original_voxel_sha256":file_hash(directory/"vox_out.h5"),
        "room_physical_exact_volume_m3":full.room_fluid_volume_m3,
        "true_all_eigenmodes_count":int(sep.total_cells),
        "original_native_Ts_s":dt,"original_Nt":nt,
        "original_full_native_q0_source_strength":strength,
        "true_physical_roof_mass_K_tensor_check":geomcheck,
        "true_independent_sparse_operator_checks":numerical,
        "every_full_mode_250ms_signed_transfer_40_80":transfers,
        "native_newmark_base_vs_prior_true_original_CG_wave_relative":baserel,
        "full_kmk_arm_vs_prior_KmkK_full_mode_relative":relative,
        "all_modes_including_high_and_point_q0_kept":True,
        "time_wall_seconds":float(time.perf_counter()-stamp)}


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args()
    raw=args.plan.read_bytes()
    p=validate_plan(json.loads(raw.decode("utf-8")))
    d=p["original_provenance"]
    original=json.loads((ROOT/d["original_native_wave"]).read_text(encoding="utf-8"))
    roof=json.loads((ROOT/d["prior_full_roof"]).read_text(encoding="utf-8"))
    kmk=json.loads((ROOT/d["prior_full_kmk"]).read_text(encoding="utf-8"))
    native={q["ppw"]:q for q in original["actual_native_wave_cases"]}
    prevroof={q["ppw"]:q for q in roof["actual_untruncated_3D_native_eigenmode_cases"]}
    prevkmk={q["ppw"]:q for q in kmk["actual_full_mode_exact_roof_kmk_cases"]}
    if not set([40,44]).issubset(native) or set(prevroof)!={40,44} or set(prevkmk)!={40,44}:
        raise ValueError("prior original SHA-connected exact roof modes missing")
    dirs={}
    for f in args.original_sims_root.rglob("comms_out.h5"):
        file_sha=file_hash(f)
        found=[i for i in (40,44) if file_sha==native[i]["original_native_comm_sha256"]]
        if len(found)==1:
            if found[0] in dirs:raise ValueError("duplicate original comms HDF5")
            dirs[found[0]]=f.parent
    if set(dirs)!={40,44}:raise ValueError("original real q0 HDF5 missing")
    out={"schema_version":"htdt.r130d.exact-roof-q0-tensor-directional-kmk-evidence-1",
        "prospective_plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":p,"original_PFFDTD_q0":"SELF_CONVERGENCE_FAILED",
        "physical_validation":"NOT_VALIDATED","product":"NO_GO",
        "new_upstream_pffdtd_wave_runs":0,"new_github_actions_runs":0,
        "true_fullmode_original_native_cases":[],"all_arms_40_to_44":[]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    for i in (40,44):
        try:one=one_grid(p,i,dirs[i],native[i],prevroof[i],prevkmk[i])
        except Exception as exc:
            out["true_fullmode_original_native_cases"].append({
                "ppw":i,"status":"DIRECT_TRUE_OPERATOR_CHECK_FAILED",
                "failure_type":type(exc).__name__,"failure_detail":str(exc)})
            args.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        out["true_fullmode_original_native_cases"].append(one)
        args.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("DIRECTIONAL_KMK_FULL_Q0_ALLMODES",i,one["true_all_eigenmodes_count"],
              "prior full KmkK rel",one["full_kmk_arm_vs_prior_KmkK_full_mode_relative"],
              "wall",round(one["time_wall_seconds"],1),flush=True)
    coarse,fine=out["true_fullmode_original_native_cases"]
    for arm in ARMS:
        c=coarse["every_full_mode_250ms_signed_transfer_40_80"][arm]
        f=fine["every_full_mode_250ms_signed_transfer_40_80"][arm]
        m=compare_complex_transfer(reference=f,candidate=c,
            frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
        out["all_arms_40_to_44"].append({
            "arm":arm,"signed_original_PP40_minus_PP44_two_bin":pairs(unpairs(c)-unpairs(f)),
            "frozen_original_complex_magnitude_phase":m,
            "passes_original_three_limits":bool(m["complex_rms_relative"]<=.2
                 and m["magnitude_max_relative"]<=.25 and m["phase_max_deg"]<=15)})
    out["not_an_original_pffdtd_production_or_bras_gate"]=True
    args.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("COMPLETE_DIRECTIONAL_POINT_Q0",{
        q["arm"]:[round(q["frozen_original_complex_magnitude_phase"][v],6)
            for v in ("complex_rms_relative","magnitude_max_relative","phase_max_deg")]
        for q in out["all_arms_40_to_44"]},
        "all_pass_flags",[q["passes_original_three_limits"] for q in out["all_arms_40_to_44"]],
        flush=True)
if __name__=="__main__":
    main()
