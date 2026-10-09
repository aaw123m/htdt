#!/usr/bin/env python3
"""Actual point-q0 exact-roof 3D wave A/B with predeclared x-grid phase only.

Original continuous physical source and receiver positions are fixed.
Original q0=1 impulse, native dt/Nt, 40/80Hz no taper, actual conservative
cut-volume FV/Newmark and all negative signed outcomes are preserved.
"""
from __future__ import annotations
import argparse
import hashlib
import itertools
import json
from pathlib import Path
import sys
import time

import h5py
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
from htdt.r130d_native_grid_exact_roof_fv import (
    build_native_exact_roof_fv,implicit_newmark_original_q0)
from htdt.acoustic_pffdtd_adapter import (
    finite_record_pressure_transfer,pffdtd_velocity_potential_to_pressure_trace)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_native_exact_roof_fv_q0 import verify_plan as verify_roof_plan,pairs,unpairs
from run_r130d_original_point_quadratic_pffdtd import file_hash,PIN

SCHEMA="htdt.r130d.exact-roof-original-point-q0-x-grid-phase-plan-1"
PPW=(40,44)
PHASES=(-.25,.25)
def verify_plan(p):
    if (p.get("schema_version")!=SCHEMA
        or p["reference_data"]["native_ppw"]!=list(PPW)
        or p["reference_data"]["pffdtd_pin"]!=PIN
        or p["controlled_perturbation"]["x_grid_offset_in_native_h"]!=list(PHASES)
        or p["controlled_perturbation"]["unchanged_yz_node_coordinates"] is not True
        or p["controlled_perturbation"]["source_and_receiver_at_exact_same_physical_xyz"] is not True
        or p["reference_data"]["physical_point_source_m"]!=[1.5,2,2]
        or p["reference_data"]["physical_point_receiver_m"]!=[2.5,2,2]
        or p["reference_data"]["analysis_frequencies_hz"]!=[40,80]
        or p["reference_data"]["record_s"]!=.25
        or p["analysis"]["canonical_acceptance_original_complex_max"]!=.2
        or p["analysis"]["canonical_acceptance_original_magnitude_max"]!=.25
        or p["analysis"]["canonical_acceptance_original_phase_deg_max"]!=15
        or p["limits"]["max_cases"]!=4
        or p["limits"]["no_github_actions_runs"] is not True
        or p["qualification"]["original_PFFDTD_point_q0"]!="SELF_CONVERGENCE_FAILED"
        or p["qualification"]["product"]!="NO_GO"):
        raise ValueError("pre-observation point q0 grid-phase physical plan changed")
    return p

def trilinear_physical_point(
    axes:tuple[np.ndarray,...]|list[np.ndarray],xyz:list[float]|tuple[float,...],
)->tuple[np.ndarray,np.ndarray,dict]:
    """Original PFFDTD 8-node linear evaluation of the SAME physical point.

    Sort-independent stencil, exact constant/first coordinate moments.
    No Gaussian changes or source pressure waveform manipulations.
    """
    if len(axes)!=3 or len(xyz)!=3:raise ValueError("3D axes and point required")
    a=[np.asarray(v,dtype=float) for v in axes]
    if any(v.ndim!=1 or len(v)<3 or np.any(~np.isfinite(v))
           or np.any(np.diff(v)<=0) for v in a):
        raise ValueError("invalid native point interpolation grid axes")
    if any(not np.isfinite(z) for z in xyz):
        raise ValueError("point coordinate nonfinite")
    lo=[]
    t=[]
    for coord,axis in zip(xyz,a):
        i=int(np.searchsorted(axis,coord,side="right")-1)
        if not (0<=i<len(axis)-1):
            raise ValueError("original physical point is outside native grid")
        u=float((coord-axis[i])/(axis[i+1]-axis[i]))
        if not (-1e-12<=u<=1+1e-12):
            raise ValueError("original trilinear fractional point outside native support")
        lo.append(i);t.append(u)
    idx=[]
    weights=[]
    for z in itertools.product((0,1),repeat=3):
        xyz_idx=tuple(lo[j]+z[j] for j in range(3))
        idx.append(int(np.ravel_multi_index(xyz_idx,tuple(len(v) for v in a))))
        weights.append(float(np.prod([t[j] if z[j] else 1-t[j] for j in range(3)])))
    idx=np.asarray(idx,dtype=np.int64)
    weights=np.asarray(weights,dtype=float)
    if (len(set(map(int,idx)))!=8 or abs(weights.sum()-1)>1e-13
        or np.min(weights)<-1e-13):
        raise ValueError("invalid original physical eight-node volume-point distribution")
    physical=np.asarray([[a[j][unravel[j]] for j in range(3)]
             for unravel in (np.unravel_index(i,tuple(len(v) for v in a)) for i in idx)])
    first=np.dot(weights,physical)
    if not np.allclose(first,xyz,atol=1e-12,rtol=0):
        raise ValueError("original physical point's first Cartesian moment drifted")
    variance=np.dot(weights,np.sum((physical-np.asarray(xyz))**2,axis=1))
    return idx,weights,{
        "exact_original_physical_xyz_m":list(map(float,xyz)),
        "fractional_grid_coordinates":t,
        "zeroth_moment":float(np.sum(weights)),
        "reconstructed_first_moment_xyz_m":list(map(float,first)),
        "stencil_spatial_RMS_radius_m":float(np.sqrt(max(variance,0))),
        "support_node_count":8,
        "all_weights_nonnegative":True,
    }

def exact_same_original_stencil(
    reconstructed_ix,reconstructed_w,original_ix,original_w):
    """Original 8-node weights must equal recomputed physical trilinear operator."""
    a=dict(zip(map(int,reconstructed_ix),map(float,reconstructed_w)))
    b=dict(zip(map(int,original_ix),map(float,original_w)))
    return set(a)==set(b) and all(abs(a[k]-b[k])<5e-11 for k in a)

def evaluate_signed_error(x,y):
    return compare_complex_transfer(
        reference=y,candidate=x,frequency_hz=[40,80],
        magnitude_mask_relative_db=-50).model_dump(mode="json")

def one(p,ppw,phase,sim,prior,roof_prior):
    t0=time.perf_counter()
    if (file_hash(sim/"comms_out.h5")!=prior["original_native_comm_sha256"]
        or file_hash(sim/"vox_out.h5")!=prior["original_solver_geometry_sha256"]):
        raise ValueError("native original physical point setup SHA mismatch")
    with h5py.File(sim/"vox_out.h5","r") as h:
        axes=[np.asarray(h[n][...],dtype=float) for n in ("xv","yv","zv")]
    with h5py.File(sim/"comms_out.h5","r") as h:
        original_src=np.asarray(h["in_ixyz"][...],dtype=np.int64)
        original_in=np.asarray(h["in_sigs"][...],dtype=float)
        original_recv=np.asarray(h["out_ixyz"][...],dtype=np.int64)
        original_receiver_w=np.asarray(h["out_alpha"][...],dtype=float).ravel()
        nt=int(h["Nt"][()])
        if (original_src.shape!=(8,) or original_recv.shape!=(8,)
            or original_in.shape!=(8,nt) or original_receiver_w.shape!=(8,)
            or np.any(original_in[:,1:]!=0)
            or int(h["diff"][()])!=0 or nt>p["limits"]["max_record_samples"]):
            raise ValueError("native original q0 time samples/8-point source or receiver altered")
    with h5py.File(sim/"sim_consts.h5","r") as h:
        dt=float(h["Ts"][()]);h_m=float(h["h"][()]);c=float(h["c"][()])
    src0,src0w,src0info=trilinear_physical_point(
        axes,p["reference_data"]["physical_point_source_m"])
    recv0,recv0w,recv0info=trilinear_physical_point(
        axes,p["reference_data"]["physical_point_receiver_m"])
    original_src_w=original_in[:,0]/original_in[:,0].sum()
    if (not exact_same_original_stencil(src0,src0w,original_src,original_src_w)
        or not exact_same_original_stencil(recv0,recv0w,original_recv,original_receiver_w)):
        raise ValueError("physical trilinear implementation failed original 8-node PFFDTD point reproduction")
    if (nt!=roof_prior["original_record_samples"] or
        abs(dt-roof_prior["original_native_dt_s"])>1e-12 or
        abs(h_m-roof_prior["native_original_h_m"])>1e-12):
        raise ValueError("native time/source/pressure sampling changed")
    shifted=[axes[0]+phase*h_m,axes[1],axes[2]]
    src,src_w,src_info=trilinear_physical_point(
        shifted,p["reference_data"]["physical_point_source_m"])
    recv,recv_w,recv_info=trilinear_physical_point(
        shifted,p["reference_data"]["physical_point_receiver_m"])
    system=build_native_exact_roof_fv(
        shifted,max_nodes=p["limits"]["max_active_cut_cells"])
    if (abs(system.room_fluid_volume_m3-56)>2e-8 or
        system.number_of_cells>p["limits"]["max_active_cut_cells"]):
        raise ValueError("offset grid changed exact physical room volume or exceeded resource limit")
    phi,stats=implicit_newmark_original_q0(
        system,native_dt_s=dt,
        native_source_ix=src,native_source_q0_weights=src_w,
        native_receiver_ix=recv,native_receiver_weights=recv_w,
        native_record_samples=nt,sound_speed_m_s=c,
        solver_rtol=1e-10,solver_atol=1e-12,max_cg_iter=500,
        max_true_relative_residual=p["limits"]["max_CG_relative_residual"])
    pressure=pffdtd_velocity_potential_to_pressure_trace(
        phi,time_step_s=dt,density_kg_m3=p["reference_data"]["rho_kg_m3"])
    q=np.zeros(nt);q[0]=1
    transfer=finite_record_pressure_transfer(
        pressure,q,time_step_s=dt,frequency_hz=np.array([40.,80.]))
    if (stats["relative_energy_drift_after_source"]>
        p["limits"]["max_undriven_energy_drift"]):
        raise ValueError("shifted-grid homogeneous Neumann energy drift exceeded predeclared limit")
    return {
        "ppw":ppw,"native_grid_x_shift_fraction_h":phase,
        "original_pinned_native_comms_sha256":file_hash(sim/"comms_out.h5"),
        "original_pinned_native_vox_sha256":file_hash(sim/"vox_out.h5"),
        "native_unmodified_original_q0_stencil_confirmed":True,
        "source_original_unshifted_trilinear":src0info,
        "receiver_original_unshifted_trilinear":recv0info,
        "shifted_same_physical_source_trilinear":src_info,
        "shifted_same_physical_receiver_trilinear":recv_info,
        "unmodified_native_dt_s":dt,
        "unmodified_native_sample_count":nt,
        "physical_volume_m3":system.room_fluid_volume_m3,
        "active_exact_cutcells":system.number_of_cells,
        "minimum_cell_volume_fraction_h3":system.min_volume_fraction,
        "symmetric_neumann_stiffness_nonzeros":int(system.stiffness_matrix.nnz),
        "new_actual_unmodified_physical_point_q0_signed_40_80":pairs(transfer),
        "saved_prior_unshifted_control_signed_40_80":roof_prior["experimental_signed_P_T_over_Q_T_40_80"],
        "within_same_ppw_phase_vs_unshifted_full_complex":evaluate_signed_error(
            pairs(transfer),roof_prior["experimental_signed_P_T_over_Q_T_40_80"]),
        "source_receiver_xyz_and_q0_unchanged":True,
        "discrete_8node_weights_changed_due_only_to_node_shift":True,
        "native_solver_original_code_and_hdf5_not_modified":True,
        "solver":stats,
        "elapsed_wall_seconds":float(time.perf_counter()-t0)}
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    a=ap.parse_args()
    raw=a.plan.read_bytes();p=verify_plan(json.loads(raw.decode("utf-8")))
    previous=json.loads((ROOT/p["reference_data"]["original_native_exact_comms"]).read_text(encoding="utf-8"))
    roof=json.loads((ROOT/p["reference_data"]["original_exact_roof_control"]).read_text(encoding="utf-8"))
    roofplan=verify_roof_plan(json.loads((ROOT/p["reference_data"]["original_exact_roof_plan"]).read_text(encoding="utf-8")))
    if roof["preregistered_plan"]!=roofplan or roof["product"]!="NO_GO":
        raise ValueError("original native exact roof unshifted frozen controls altered")
    prior={r["ppw"]:r for r in previous["actual_native_wave_cases"] if r["ppw"] in PPW}
    base={r["ppw"]:r for r in roof["actual_native_grid_point_impulse_exact_roof_cases"] if r["ppw"] in PPW}
    if set(prior)!=set(PPW) or set(base)!=set(PPW):
        raise ValueError("missing original native PPW40/44 full experimental controls")
    dirs={}
    for f in a.original_sims_root.rglob("comms_out.h5"):
        sha=file_hash(f)
        matching=[ppw for ppw in PPW if sha==prior[ppw]["original_native_comm_sha256"]]
        if len(matching)==1:
            if matching[0] in dirs:raise ValueError("duplicate original native PPW comms")
            dirs[matching[0]]=f.parent
    if set(dirs)!=set(PPW):raise ValueError("original raw source native PPW40/44 HDF5 missing")
    evidence={
        "schema_version":"htdt.r130d.exact-roof-original-point-q0-x-grid-phase-evidence-1",
        "plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":p,"pffdtd_pin":PIN,
        "original_canonical":"SELF_CONVERGENCE_FAILED",
        "physical_validation":"NOT_VALIDATED","product":"NO_GO",
        "new_github_actions_started":0,
        "actual_shifted_x_grid_full_original_q0_wave_cases":[]}
    for phase in PHASES:
        for ppw in PPW:
            try:row=one(p,ppw,phase,dirs[ppw],prior[ppw],base[ppw])
            except Exception as exc:
                evidence["actual_shifted_x_grid_full_original_q0_wave_cases"].append({
                    "ppw":ppw,"phase":phase,"status":"EXPERIMENT_FAILED",
                    "exception":type(exc).__name__,"detail":str(exc)})
                a.output.parent.mkdir(parents=True,exist_ok=True)
                a.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
                raise
            evidence["actual_shifted_x_grid_full_original_q0_wave_cases"].append(row)
            a.output.parent.mkdir(parents=True,exist_ok=True)
            a.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            print("GRID_X_PHASE_NATIVE_ORIGINAL_POINT_Q0",ppw,"phase",phase,
                  "minV/h3",row["minimum_cell_volume_fraction_h3"],
                  "signed",row["new_actual_unmodified_physical_point_q0_signed_40_80"],
                  "vs_unshifted",row["within_same_ppw_phase_vs_unshifted_full_complex"]["complex_rms_relative"],
                  "CGmax",row["solver"]["maximum_CG_iterations"],flush=True)
    comparison=[]
    for phase in PHASES:
        row40=next(r for r in evidence["actual_shifted_x_grid_full_original_q0_wave_cases"]
                   if r["ppw"]==40 and r["native_grid_x_shift_fraction_h"]==phase)
        row44=next(r for r in evidence["actual_shifted_x_grid_full_original_q0_wave_cases"]
                   if r["ppw"]==44 and r["native_grid_x_shift_fraction_h"]==phase)
        score=evaluate_signed_error(row40["new_actual_unmodified_physical_point_q0_signed_40_80"],
                                    row44["new_actual_unmodified_physical_point_q0_signed_40_80"])
        comparison.append({
            "phase":phase,"ppw_pair":[40,44],**score,
            "point_source_xyz_unchanged":True,
            "point_receiver_xyz_unchanged":True,
            "same_original_q0_native_sampling":True,
            "meets_original_40_44_thresholds":bool(
                score["complex_rms_relative"]<=.2
                and score["magnitude_max_relative"]<=.25
                and score["phase_max_deg"]<=15)})
    old=evaluate_signed_error(
        base[40]["experimental_signed_P_T_over_Q_T_40_80"],
        base[44]["experimental_signed_P_T_over_Q_T_40_80"])
    evidence["two_grid_phase_sensitivity"]=comparison
    evidence["unshifted_ppw40_44_original_exact_roof_control"]=old
    evidence["phase_study_not_full_5_grid_original_point_qualification"]=True
    evidence["cannot_claim_any_production_approval"]=True
    a.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("NATIVE_40_44_X_GRID_PHASE_DONE",
          [(r["phase"],r["complex_rms_relative"],r["magnitude_max_relative"],r["phase_max_deg"])
            for r in comparison],"unshifted_complex",old["complex_rms_relative"],
          "PFFDTD_original_canonical_STILL_FAILED",flush=True)
if __name__=="__main__":
    main()
