#!/usr/bin/env python3
"""Prospectively fixed exact-roof FV q0 conservative KM^-1 K dispersion A/B.

New true symmetric FV spatial operator; *not* native PFFDTD qualification.
Each original full-wave q0 and finite pressure DTFT is computed from ALL
original x×yz modes, including every high-frequency mode.
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
from htdt.r130d_conservative_dispersion_correction import (
    dispersion_corrected_exact_roof_system, corrected_eigenvalues)
from htdt.r130d_native_grid_exact_roof_fv import build_native_exact_roof_fv
from htdt.r130d_native_exact_roof_separable import (
    original_native_xy_z_factorization, prove_native_full_kronecker_equal,
    generalized_neumann_modes)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_native_exact_roof_full_xy_z_modal_q0 import (
    entire_original_finite_record_signed_modes, PPW)
from run_r130d_native_exact_roof_xy_z_separable_modal_q0 import source_receiver_tensor_projections
from run_r130d_original_point_quadratic_pffdtd import PIN, file_hash
from run_r130d_native_exact_roof_fv_q0 import pairs, unpairs

SCHEMA="htdt.r130d.exact-roof-q0-kmk-dispersion-plan-1"
def validate_plan(p):
    a=p["frozen_input"];s=p["scheme"];e=p["precommitted_tests"]
    if (p["schema_version"]!=SCHEMA or p["issue"]!=938 or p["pr"]!=1055
        or a["ppw"]!=list(PPW) or a["original_PFFDTD_sha"]!=PIN
        or a["source_xyz_m"]!=[1.5,2,2] or a["receiver_xyz_m"]!=[2.5,2,2]
        or a["physical_sloped_volume_m3"]!=56 or a["record_seconds"]!=.25
        or a["original_signed_pressure_transfer_hz"]!=[40,80]
        or a["rho_kg_m3"]!=1.2 or a["c_m_s"]!=343.2
        or s["alpha_multiplier_h2_over_c2"]!=1/12
        or not s["original_source_q0_unchanged"]
        or not s["mass_and_wall_geometry_unchanged"]
        or not s["original_native_full_mode_eigenvectors_unchanged"]
        or e["baseline_all_modes_reproduce_prior_real_newmark_cg_complex_relative_max"]!=2e-5
        or [e[x] for x in ("frozen_complex_limit","frozen_magnitude_limit","frozen_phase_deg_limit")]!=[.2,.25,15]
        or not e["all_original_modal_nodes_no_truncation"]
        or not e["record_full_adverse_metrics"]
        or p["limits"]!={"max_grid_cases":2,"new_actual_native_PFFDTD_wave_runs":0,
                          "new_github_actions_runs":0}
        or p["release"]["canonical_original_q0"]!="SELF_CONVERGENCE_FAILED"
        or p["release"]["external_physics"]!="NOT_VALIDATED"
        or p["release"]["product"]!="NO_GO"):
        raise ValueError("prospective KM^-1K dispersion A/B plan changed")
    return p


def one_case(p,ppw,sim,original,prior):
    start=time.perf_counter()
    if (file_hash(sim/"comms_out.h5")!=original["original_native_comm_sha256"]
        or file_hash(sim/"vox_out.h5")!=original["original_solver_geometry_sha256"]):
        raise ValueError("original native 8 point q0 or geometry HDF5 drift")
    with h5py.File(sim/"vox_out.h5","r") as h:
        axes=[np.asarray(h[t][...],dtype=float) for t in ("xv","yv","zv")]
    with h5py.File(sim/"comms_out.h5","r") as h:
        si=np.asarray(h["in_ixyz"][:],dtype=np.int64)
        sig=np.asarray(h["in_sigs"][:],dtype=float)
        ri=np.asarray(h["out_ixyz"][:],dtype=np.int64)
        rw=np.asarray(h["out_alpha"][:],dtype=float).ravel()
        nt=int(h["Nt"][()])
        if (si.shape!=(8,) or ri.shape!=(8,) or sig.shape!=(8,nt)
            or rw.shape!=(8,) or nt<3 or nt>2000 or np.any(sig[:,1:]!=0)
            or int(h["diff"][()])!=0):
            raise ValueError("original 8/8 q0 temporal forcing/original observation changed")
        strength=float(sig[:,0].sum())
        sw=sig[:,0]/strength
    with h5py.File(sim/"sim_consts.h5","r") as h:
        dt=float(h["Ts"][()]);h_m=float(h["h"][()]);c=float(h["c"][()])
    if (abs(c-343.2)>1e-12 or abs(h_m-c/(100*ppw))>1e-12
        or abs(dt-prior["original_native_dt_s"])>1e-12
        or nt!=prior["original_record_nt"] or abs(sum(sw)-1)>1e-12):
        raise ValueError("original native clock, eight point source, strength drift")
    full=build_native_exact_roof_fv(axes,max_nodes=150000)
    if abs(full.room_fluid_volume_m3-56)>2e-8:
        raise ValueError("original continuous sloped volume changed")
    sep=original_native_xy_z_factorization(axes)
    matrix_match=prove_native_full_kronecker_equal(full,sep)
    if sep.total_cells!=prior["total_full_untruncated_3D_native_modes"]:
        raise ValueError("previous original full mode count changed")
    projections=source_receiver_tensor_projections(
        axes,sep,(si,sw),(ri,rw))
    xlam,xvec,xdiag=generalized_neumann_modes(sep.Mx,sep.Kx)
    ylam,yvec,ydiag=generalized_neumann_modes(sep.Myz,sep.Kyz)
    sx=xvec.T@projections["source"]["x"]
    rx=xvec.T@projections["receiver"]["x"]
    sy=yvec.T@projections["source"]["yz"]
    ry=yvec.T@projections["receiver"]["yz"]
    coupling=np.outer(sx*rx,sy*ry)
    lam=(xlam[:,None]+ylam[None,:])
    alpha=h_m**2/(12*c*c)
    modes={}
    for arm,values in (("native_exact_roof_newmark",np.maximum(lam,0)),
                       ("conservative_kmk_dispersion_newmark",
                         corrected_eigenvalues(lam,h_m=h_m,sound_speed_m_s=c))):
        amp=(dt*dt*c*c*coupling/(1+dt*dt*values/4)).ravel()
        signed=entire_original_finite_record_signed_modes(
            values.ravel(),amp,dt,nt,p["frozen_input"]["rho_kg_m3"])
        modes[arm]=pairs(signed.sum(axis=1))
    baseline=unpairs(modes["native_exact_roof_newmark"])
    previous=unpairs(prior["archived_direct_full_wave_250ms_signed_40_80"])
    relative=float(np.linalg.norm(baseline-previous)/max(np.linalg.norm(previous),1e-15))
    if relative>p["precommitted_tests"]["baseline_all_modes_reproduce_prior_real_newmark_cg_complex_relative_max"]:
        raise ValueError(f"ALL native roof q0 baseline no longer reproduces true CG full wave {relative}")
    candidate=dispersion_corrected_exact_roof_system(full,sound_speed_m_s=c)
    K=candidate.stiffness_matrix;M=full.mass_matrix
    n=full.number_of_cells
    nullscale=float(np.abs(K@np.ones(n)).max()/max(abs(K.diagonal()).max(),1))
    if nullscale>1e-8 or (K-K.T).nnz and abs((K-K.T).data).max()>1e-7:
        raise ValueError("conservative 3D correction invalid")
    # Operator identity vs independent all-mode generalized eigensystem, not
    # just a reshaped list of analytical eigenvalues.
    v=np.outer(xvec[:,1],yvec[:,1]).ravel(order="C")
    analytical=float(corrected_eigenvalues(np.array([xlam[1]+ylam[1]]),h_m=h_m,sound_speed_m_s=c)[0])
    op_relative=float(np.linalg.norm(K@v-analytical*(M@v))/
                      max(np.linalg.norm(analytical*(M@v)),1e-24))
    if op_relative>2e-5:
        raise ValueError(f"true sparse 3D corrected Neumann mode residual {op_relative}")
    return {"ppw":ppw,"all_true_3D_native_roof_modes_count":sep.total_cells,
        "original_native_voxel_sha256":file_hash(sim/"vox_out.h5"),
        "original_native_comms_sha256":file_hash(sim/"comms_out.h5"),
        "native_source_q0_total_in_sig":strength,
        "native_dt_s":dt,"native_nt":nt,"volume_m3":full.room_fluid_volume_m3,
        "original_exact_roof_kronecker_identity":matrix_match,
        "actual_true_KmkK_3D_sparse_nnz":int(K.nnz),
        "actual_true_corrected_sparse_symmetry":True,
        "actual_true_corrected_sparse_constant_neumann_relative_residual":nullscale,
        "actual_true_corrected_sparse_eigenmode_relative_residual":op_relative,
        "full_untruncated_source_receiver_q0_signed_two_bin_by_arm":modes,
        "native_newmark_control_vs_archived_true_wave_complex_relative":relative,
        "native_original_q0_and_250ms_and_40_80_unchanged":True,
        "new_native_PFFDTD_full_wave_runs":0,
        "wall_seconds":float(time.perf_counter()-start)}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--plan",type=Path,required=True)
    parser.add_argument("--original-sims-root",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    raw=args.plan.read_bytes()
    p=validate_plan(json.loads(raw.decode("utf-8")))
    prior=json.loads((ROOT/p["frozen_input"]["original_exact_roof_full_modal_baseline"]).read_text(encoding="utf-8"))
    original=json.loads((ROOT/p["frozen_input"]["original_true_native_geometry_source_evidence"]).read_text(encoding="utf-8"))
    ppw={q["ppw"]:q for q in prior["actual_untruncated_3D_native_eigenmode_cases"]}
    cases={q["ppw"]:q for q in original["actual_native_wave_cases"]}
    if set(ppw)!=set(PPW) or not set(PPW).issubset(cases):
        raise ValueError("frozen real exact roof baseline / original PFFDTD raw assets missing")
    dirs={}
    for f in args.original_sims_root.rglob("comms_out.h5"):
        sha=file_hash(f)
        matches=[i for i in PPW if sha==cases[i]["original_native_comm_sha256"]]
        if len(matches)==1:
            if matches[0] in dirs:raise ValueError("ambiguous raw q0 HDF5")
            dirs[matches[0]]=f.parent
    if set(dirs)!=set(PPW):raise ValueError("original raw native q0 cases absent")
    result={"schema_version":"htdt.r130d.exact-roof-q0-kmk-dispersion-evidence-1",
        "plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":p,"original_PFFDTD_q0":"SELF_CONVERGENCE_FAILED",
        "physical_validation":"NOT_VALIDATED","product":"NO_GO",
        "new_native_PFFDTD_wave_runs":0,"new_GitHub_Actions_runs":0,
        "actual_full_mode_exact_roof_kmk_cases":[]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    for i in PPW:
        try:q=one_case(p,i,dirs[i],cases[i],ppw[i])
        except Exception as ex:
            result["actual_full_mode_exact_roof_kmk_cases"].append({
                "ppw":i,"status":"DIAGNOSTIC_FAILED","error":type(ex).__name__,
                "detail":str(ex)})
            args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        result["actual_full_mode_exact_roof_kmk_cases"].append(q)
        args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("TRUE_EXACT_ROOF_KMK_ALLMODES",i,q["all_true_3D_native_roof_modes_count"],
            "old true CG relative",q["native_newmark_control_vs_archived_true_wave_complex_relative"],
            "new sparse K4 nonzeros",q["actual_true_KmkK_3D_sparse_nnz"],flush=True)
    coarse,fine=result["actual_full_mode_exact_roof_kmk_cases"]
    comparisons={}
    for arm in ("native_exact_roof_newmark","conservative_kmk_dispersion_newmark"):
        c=coarse["full_untruncated_source_receiver_q0_signed_two_bin_by_arm"][arm]
        f=fine["full_untruncated_source_receiver_q0_signed_two_bin_by_arm"][arm]
        metrics=compare_complex_transfer(reference=f,candidate=c,
            frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
        comparisons[arm]={"ppw40_minus_44_signed_transfer":pairs(unpairs(c)-unpairs(f)),
            "unchanged_two_bin_original_metrics":metrics,
            "passes_frozen_all_limits":bool(metrics["complex_rms_relative"]<=.2
                and metrics["magnitude_max_relative"]<=.25
                and metrics["phase_max_deg"]<=15)}
    if abs(comparisons["native_exact_roof_newmark"]["unchanged_two_bin_original_metrics"]["complex_rms_relative"]-.8726662923738394)>2e-5:
        raise ValueError("native original exact-roof q0 baseline changed")
    result["all_original_ppw40_44_signed_comparisons"]=comparisons
    result["candidate_is_experimental_not_original_native_pffdtd"]=True
    args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("CONSERVATIVE_EXACT_ROOF_KMK_FULL_Q0_REFINEMENT",
          {k:(round(v["unchanged_two_bin_original_metrics"]["complex_rms_relative"],6),
              round(v["unchanged_two_bin_original_metrics"]["magnitude_max_relative"],6),
              round(v["unchanged_two_bin_original_metrics"]["phase_max_deg"],3),
              v["passes_frozen_all_limits"]) for k,v in comparisons.items()},flush=True)
if __name__=="__main__":
    main()
