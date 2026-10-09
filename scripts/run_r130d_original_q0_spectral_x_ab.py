#!/usr/bin/env python3
"""Prospectively frozen ORIGINAL native PFFDTD q0 full-eigenspectrum x-axis solver A/B.

Unlike the unmodified upstream PFFDTD control, Newmark/continuous-x arms are
experimental spectral propagators and have no production-qualification authority.
All native 8-point HDF5 source, q0, room y-z graph, native clocks and 250ms bins
remain frozen. Every x and y-z mode is retained; no modal frequency masking.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import h5py
import numpy as np
from scipy import linalg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_point_quadratic_pffdtd import PPW, PIN, file_hash
from run_r130d_original_native_modal_drift import native_room_laplacian, pairs, unpairs
from run_r130d_original_pffdtd_neumann_graph_audit import validate_plan as check_graph_plan
from run_r130d_original_pffdtd_native_full_modal_q0 import (
    graph_from_original_6_neighbors_ppw,
    original_native_tensor_point_source_receiver,
    true_native_leapfrog_exact_finite_signed_transfer,
    validate_plan as validate_original_plan,
)

SCHEMA = "htdt.r130d.original-native-q0-x-spectral-ab-plan-1"
ARMS = ("native_leapfrog", "native_newmark", "x_frequency_newmark",
        "x_coupling_newmark", "both_x_newmark")


def validate_plan(plan):
    inp = plan["input"]
    method = plan["method"]
    ev = plan["evaluation"]
    if (plan["schema_version"] != SCHEMA
        or plan["issue"] != 938 or plan["pr"] != 1055
        or inp["upstream_sha"] != PIN or inp["ppw"] != list(PPW)
        or inp["room_x_interval_m"] != [0,4]
        or inp["source_xyz_m"] != [1.5,2,2]
        or inp["receiver_xyz_m"] != [2.5,2,2]
        or inp["record_s"] != 0.25 or inp["frequency_hz"] != [40,80]
        or [a["id"] for a in method["arms"]] != list(ARMS)
        or ev["compare_original_frozen_native_wave_to_native_leapfrog"] != 2e-6
        or [ev[k] for k in ("complex_limit","magnitude_limit","phase_deg_limit")] != [.2,.25,15]
        or not method["source_q0_initial_discrete_kick_frozen_in_all_arms"]
        or not method["no_yz_geometry_fix_or_bras_claim"]
        or not ev["no_result_dependent_fitting_or_truncation"]
        or not ev["save_all_unfavorable_results"]
        or plan["limits"] != {"max_original_cases":5,"max_new_native_pffdtd_wave_runs":0,
                              "no_github_actions_runs":True}
        or plan["authority"]["original_point_q0"] != "SELF_CONVERGENCE_FAILED"
        or plan["authority"]["physical_validation"] != "NOT_VALIDATED"
        or plan["authority"]["product"] != "NO_GO"):
        raise ValueError("prospective original native q0 x spectral A/B plan changed")
    return plan


def continuum_neumann_point_x_coupling(nx, source_x=1.5, receiver_x=2.5, length=4.0):
    """Continuous Neumann point-evaluation modal overlap in native Nx normalization."""
    if nx < 3 or length != 4 or source_x != 1.5 or receiver_x != 2.5:
        raise ValueError("fixed original room/source/receiver x changed")
    m = np.arange(nx, dtype=float)
    weights = (2.0/nx)*np.cos(np.pi*m*source_x/length)*np.cos(
        np.pi*m*receiver_x/length)
    weights[0] = 1.0/nx
    if not np.isclose(weights[0],1/nx):
        raise ValueError("constant mode analytic coupling mismatch")
    return weights


def one_case(plan, ppw, folder, saved, audited, graph_plan):
    if (file_hash(folder/"comms_out.h5")!=saved["original_native_comm_sha256"]
        or file_hash(folder/"vox_out.h5")!=audited["original_exact_voxel_sha256"]):
        raise ValueError("original native SHA changed")
    with h5py.File(folder/"vox_out.h5", "r") as h:
        dims=tuple(int(h[k][()]) for k in ("Nx","Ny","Nz"))
        A,loc,visited,deg=native_room_laplacian(
            {"source_authority":{"physical_source_xyz_m":[1.5,2,2]},
             "limits":{"max_graph_nodes":300_000}},h,graph_plan)
    Ax,Ayz,xids,yzids,identity=graph_from_original_6_neighbors_ppw(A,visited,dims)
    if (identity["original_native_Kronecker_neumann_matrix_max_abs"]!=0
        or len(visited)!=audited["source_connected_room_nodes"]):
        raise ValueError("original graph no longer matches frozen connected room")
    with h5py.File(folder/"comms_out.h5","r") as h:
        si=np.asarray(h["in_ixyz"][:], dtype=np.int64)
        sig=np.asarray(h["in_sigs"][:],dtype=float)
        ri=np.asarray(h["out_ixyz"][:],dtype=np.int64)
        rw=np.asarray(h["out_alpha"][:],dtype=float).ravel()
        nt=int(h["Nt"][()])
        if (si.shape!=(8,) or ri.shape!=(8,) or sig.shape!=(8,nt)
            or np.any(sig[:,1:]!=0) or nt>2000 or int(h["diff"][()])!=0):
            raise ValueError("original q0/8-point native HDF5 changed")
        strength=float(sig[:,0].sum())
        if strength<=0: raise ValueError("q0 source missing")
        sw=sig[:,0]/strength
    with h5py.File(folder/"sim_consts.h5","r") as h:
        dt=float(h["Ts"][()]);grid_h=float(h["h"][()])
        c=float(h["c"][()]);l2=float(h["l2"][()])
    if (abs(c-343.2)>1e-12 or abs(grid_h-c/(100*ppw))>1e-12
        or abs(l2-(c*dt/grid_h)**2)>1e-12
        or abs(strength-l2/grid_h)>1e-10
        or abs(dt-saved["native_solver"]["dt_s"])>1e-12):
        raise ValueError("native fixed timestep or source strength changed")
    sx,syz,sr=original_native_tensor_point_source_receiver(dims,xids,yzids,si,sw)
    rx,ryz,rr=original_native_tensor_point_source_receiver(dims,xids,yzids,ri,rw)
    lx,vx=linalg.eigh(Ax.toarray(),check_finite=True)
    ly,vy=linalg.eigh(Ayz.toarray(),check_finite=True)
    if min(lx[0],ly[0])<-1e-9:
        raise ValueError("negative native Neumann eigenvalue")
    lx=np.maximum(lx,0.0);ly=np.maximum(ly,0.0)
    resid=max(float(np.max(abs(Ax@vx-vx*lx))),float(np.max(abs(Ayz@vy-vy*ly))))
    if resid>3e-7:raise ValueError("original spectral modes inaccurate")
    x_native=(sx@vx)*(rx@vx)
    yz_native=(syz@vy)*(ryz@vy)
    x_analytic=continuum_neumann_point_x_coupling(len(lx))
    m=np.arange(len(lx),dtype=float)
    lx_corrected=(grid_h*np.pi*m/4.0)**2
    # Frozen ORIGINAL unit q0 phi[1] source amplitude for all FIVE arms.
    # Update theta changes numerical propagation operator, not the pulse.
    graph_lam=lx[:,None]+ly[None,:]
    corrected_lam=lx_corrected[:,None]+ly[None,:]
    graph_omega=(c/grid_h)*np.sqrt(graph_lam)
    corrected_omega=(c/grid_h)*np.sqrt(corrected_lam)
    if l2*float(graph_lam.max())>4+1e-10:raise ValueError("original explicit CFL exceeded")
    theta_leapfrog=2*np.arcsin(.5*np.sqrt(np.clip(l2*graph_lam,0,4)))
    theta_native_newmark=2*np.arctan(.5*dt*graph_omega)
    theta_x_newmark=2*np.arctan(.5*dt*corrected_omega)
    modes={
        "native_leapfrog":(theta_leapfrog,x_native),
        "native_newmark":(theta_native_newmark,x_native),
        "x_frequency_newmark":(theta_x_newmark,x_native),
        "x_coupling_newmark":(theta_native_newmark,x_analytic),
        "both_x_newmark":(theta_x_newmark,x_analytic),
    }
    control=unpairs(saved["unmodified_original_transfer_pa_per_m3_s"])
    outputs={}
    for name,(theta,x_cpl) in modes.items():
        amplitude=(strength*np.outer(x_cpl,yz_native)).ravel()
        signed=true_native_leapfrog_exact_finite_signed_transfer(
            theta.ravel(),amplitude,dt,nt,1.2)
        response=signed.sum(axis=1)
        outputs[name]=pairs(response)
    native=unpairs(outputs["native_leapfrog"])
    rel=float(np.linalg.norm(native-control)/max(np.linalg.norm(control),1e-14))
    if rel>plan["evaluation"]["compare_original_frozen_native_wave_to_native_leapfrog"]:
        raise ValueError(f"ORIGINAL native eigenspectral control differs from raw wave: {rel}")
    return {"ppw":int(ppw),"native_original_full_mode_count":int(len(visited)),
        "original_native_comms_sha256":file_hash(folder/"comms_out.h5"),
        "original_native_voxels_sha256":file_hash(folder/"vox_out.h5"),
        "original_native_q0_full_wave_signed_40_80":pairs(control),
        "full_modes_against_native_saved_wave_relative":rel,
        "native_dt_s":dt,"native_Nt":nt,"native_source_strength":strength,
        "x_native_mode_count":len(lx),"yz_native_mode_count":len(ly),
        "first_nonzero_original_x_graph_frequency_hz":float(c*np.sqrt(lx[1])/(2*np.pi*grid_h)),
        "first_nonzero_corrected_x_Neumann_frequency_hz":float(c/8),
        "x_original_source_receiver_constant_mode":float(x_native[0]),
        "x_continuum_source_receiver_constant_mode":float(x_analytic[0]),
        "all_signed_q0_full_250ms_40_80_P_over_Q_by_arm":outputs,
        "all_original_3d_modes_retained":True,
        "experimental_arms_not_native_pffdtd":True}


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    arg=ap.parse_args()
    raw=arg.plan.read_bytes()
    plan=validate_plan(json.loads(raw.decode("utf-8")))
    orig=validate_original_plan(json.loads(
        (ROOT/plan["input"]["original_full_modal_preregistered_plan"]).read_text(encoding="utf-8")))
    original_evidence=json.loads(
        (ROOT/plan["input"]["original_full_modal_evidence"]).read_text(encoding="utf-8"))
    if original_evidence["canonical_original_point_q0"]!="SELF_CONVERGENCE_FAILED":
        raise ValueError("original upstream status changed")
    prev={x["ppw"]:x for x in original_evidence["actual_original_unmodified_all_mode_native_cases"]}
    waves=json.loads((ROOT/orig["original_authority"]["raw_native_wave_SHA_evidence"]).read_text(encoding="utf-8"))
    wave_by_ppw={x["ppw"]:x for x in waves["actual_native_wave_cases"]}
    audit=json.loads((ROOT/orig["original_authority"]["native_true_rigid_graph_audit"]).read_text(encoding="utf-8"))
    graph_by_ppw={x["ppw"]:x for x in audit["actual_original_voxel_grid_audits"]}
    graph_plan=check_graph_plan(json.loads((ROOT/"benchmarks/acoustics/r130d_original_pffdtd_neumann_graph_plan_2026-10-09.json").read_text(encoding="utf-8")))
    if set(prev)!=set(PPW) or set(wave_by_ppw)!=set(PPW) or set(graph_by_ppw)!=set(PPW):
        raise ValueError("original five-grid SHA-pinned material missing")
    dirs={}
    for path in arg.original_sims_root.rglob("comms_out.h5"):
        match=[i for i in PPW if file_hash(path)==wave_by_ppw[i]["original_native_comm_sha256"]]
        if len(match)==1:
            if match[0] in dirs:raise ValueError("duplicate true original native sim")
            dirs[match[0]]=path.parent
    if set(dirs)!=set(PPW):raise ValueError("SHA exact original five q0 native simulation assets absent")
    evidence={"schema_version":"htdt.r130d.original-native-q0-x-spectral-ab-evidence-1",
        "prospective_plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":plan,"original_PFFDTD_q0":"SELF_CONVERGENCE_FAILED",
        "physical_validation":"NOT_VALIDATED","product":"NO_GO",
        "new_native_PFFDTD_wave_runs":0,"new_GitHub_Actions_runs":0,
        "cases":[],"adjacent_ppw":[]}
    arg.output.parent.mkdir(parents=True,exist_ok=True)
    for ppw in PPW:
        x=one_case(plan,ppw,dirs[ppw],wave_by_ppw[ppw],graph_by_ppw[ppw],graph_plan)
        saved=unpairs(prev[ppw]["exact_original_saved_true_PFFDTD_q0_signed_40_80"])
        current=unpairs(x["original_native_q0_full_wave_signed_40_80"])
        if not np.allclose(current,saved,rtol=1e-10,atol=5e-7):
            raise ValueError("original complete true native five-grid mode evidence drift")
        evidence["cases"].append(x)
        arg.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("ALL_ORIGINAL_Q0_MODES",ppw,x["native_original_full_mode_count"],
              "native signed wave relative",x["full_modes_against_native_saved_wave_relative"],flush=True)
    for coarse,fine in zip(evidence["cases"],evidence["cases"][1:]):
        scores={}
        for arm in ARMS:
            c=coarse["all_signed_q0_full_250ms_40_80_P_over_Q_by_arm"][arm]
            f=fine["all_signed_q0_full_250ms_40_80_P_over_Q_by_arm"][arm]
            result=compare_complex_transfer(reference=f,candidate=c,
                frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
            pass_limits=(result["complex_rms_relative"]<=.2
                and result["magnitude_max_relative"]<=.25
                and result["phase_max_deg"]<=15)
            scores[arm]={"metrics":result,
                "signed_complex_coarse_minus_fine":pairs(unpairs(c)-unpairs(f)),
                "frozen_three_limits_met":bool(pass_limits)}
        evidence["adjacent_ppw"].append({"coarse_ppw":coarse["ppw"],
            "fine_ppw":fine["ppw"],"all_arms":scores})
    arg.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    for x in evidence["adjacent_ppw"]:
        print("FROZEN_NATIVE_Q0_AB_PAIR",x["coarse_ppw"],x["fine_ppw"],
              {k:round(v["metrics"]["complex_rms_relative"],6) for k,v in x["all_arms"].items()},
              "all gate flags",{k:v["frozen_three_limits_met"] for k,v in x["all_arms"].items()},
              flush=True)
    print("DIAGNOSTIC_ONLY_NATIVE_PFFDTD_NOT_REPAIRED_NO_GO",flush=True)


if __name__=="__main__":
    main()
