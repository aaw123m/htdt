#!/usr/bin/env python3
"""Time-MATCHED original q0 source: staircase native PFFDTD spatial graph vs exact roof cutcells.

This is a full independent wave solver A/B; original eight-node point
source, q0, receiver, native timestamps and Newmark step are identical.
Only M and K differ (geometry). No PFFDTD files or source kernels changed.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import numpy as np
import h5py
from scipy import sparse

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
from htdt.r130d_native_grid_exact_roof_fv import (
    NativeExactRoofCutcell,implicit_newmark_original_q0)
from htdt.acoustic_pffdtd_adapter import (
    finite_record_pressure_transfer,pffdtd_velocity_potential_to_pressure_trace)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_native_modal_drift import native_room_laplacian
from run_r130d_original_pffdtd_neumann_graph_audit import validate_plan as validate_graph_plan
from run_r130d_original_point_quadratic_pffdtd import PPW,PIN,file_hash
from run_r130d_native_exact_roof_fv_q0 import unpairs,pairs,verify_plan as verify_treatment_plan

SCHEMA="htdt.r130d.original-point-q0-matched-newmark-spatial-ab-plan-1"
def validate_plan(p):
    if (p.get("schema_version")!=SCHEMA
        or p["native_control"]["ppw"]!=list(PPW)
        or p["native_control"]["pinned_upstream"]!=PIN
        or p["native_control"]["source_xyz_m"]!=[1.5,2,2]
        or p["native_control"]["receiver_xyz_m"]!=[2.5,2,2]
        or p["native_control"]["compare_bins_hz"]!=[40,80]
        or p["matched_time"]["rtol"]!=1e-10
        or p["matched_time"]["maxiter"]!=500
        or p["acceptance"]["frozen_adjacent_original_complex_max"]!=.2
        or p["acceptance"]["frozen_adjacent_original_magnitude_max"]!=.25
        or p["acceptance"]["frozen_adjacent_original_phase_deg_max"]!=15
        or p["acceptance"]["adjacent"]!=[[28,32],[32,36],[36,40],[40,44]]
        or p["limits"]["run_github_actions"] is not False
        or p["authority"]["original_point_q0"]!="SELF_CONVERGENCE_FAILED"
        or p["authority"]["product"]!="NO_GO"):
        raise ValueError("time-matched numerical spatial A/B plan changed")
    return p

def build_native_staircase_fv(p,axes,room_source_index_set,original_graph_plan,
                              c,h):
    # Use exactly the ORIGINAL native boundary mask graph - never infer a
    # 'staircase' room independently from an approximate voxelization.
    with h5py.File(axes,"r") as geometry:
        mini={"source_authority":{"physical_source_xyz_m":p["native_control"]["source_xyz_m"]},
              "limits":{"max_graph_nodes":p["limits"]["max_active_native_graph_nodes"]}}
        A,lookup,visited,_=native_room_laplacian(mini,geometry,original_graph_plan)
        dims=tuple(int(geometry[name][()]) for name in ("Nx","Ny","Nz"))
    if A.shape[0]!=len(room_source_index_set):
        raise ValueError("original native graph changed from frozen prior original control")
    if set(visited)!=room_source_index_set:
        raise ValueError("original staircase support set changed")
    n=A.shape[0]
    masses=np.full(n,h**3,dtype=np.float64)
    stiffness=(c*c*h*A).tocsr()
    return NativeExactRoofCutcell(
        native_dimensions=dims,native_grid_spacing_m=h,
        original_native_flat_indices=np.asarray(visited,dtype=np.int64),
        fluid_volume_m3=masses,mass_matrix=sparse.diags(masses,format="csr"),
        stiffness_matrix=stiffness,
        exact_flux_face_open_area_m2=np.full((A.nnz-n)//2,h**2,dtype=np.float64),
        room_fluid_volume_m3=float(n*h**3),
        min_volume_fraction=1.)

def one(p,ppw,sim,original_graph,original_wave,treatment,original_graph_plan):
    start=time.perf_counter()
    if (file_hash(sim/"comms_out.h5")!=original_wave["original_native_comm_sha256"]
        or file_hash(sim/"vox_out.h5")!=original_graph["original_exact_voxel_sha256"]):
        raise ValueError("original pinned source or exact native staircase geometry SHA mismatch")
    with h5py.File(sim/"sim_consts.h5","r") as h:
        dt=float(h["Ts"][()]);h_m=float(h["h"][()]);c=float(h["c"][()])
    with h5py.File(sim/"comms_out.h5","r") as h:
        source=np.asarray(h["in_ixyz"][...],dtype=np.int64)
        in_sigs=np.asarray(h["in_sigs"][...],dtype=float)
        receiver=np.asarray(h["out_ixyz"][...],dtype=np.int64)
        out_weights=np.asarray(h["out_alpha"][...],dtype=float)
        nt=int(h["Nt"][()])
        if (source.shape!=(8,) or receiver.shape!=(8,)
            or in_sigs.shape!=(8,nt) or np.any(in_sigs[:,1:]!=0)
            or out_weights.shape!=(1,8) or int(h["diff"][()])!=0):
            raise ValueError("original q0 or physical source/receiver sampling drift")
    unit_src=in_sigs[:,0]/float(in_sigs[:,0].sum())
    with h5py.File(sim/"vox_out.h5","r") as h:
        graph_plan_scope=original_graph_plan
        mini={"source_authority":{"physical_source_xyz_m":p["native_control"]["source_xyz_m"]},
              "limits":{"max_graph_nodes":p["limits"]["max_active_native_graph_nodes"]}}
        A,_,visited,_=native_room_laplacian(mini,h,graph_plan_scope)
    scheme=build_native_staircase_fv(
        p,sim/"vox_out.h5",set(visited),original_graph_plan,c,h_m)
    if abs(scheme.room_fluid_volume_m3-
           original_graph["source_connected_room_nodes"]*h_m**3)>1e-8:
        raise ValueError("original staircase graph volume mismatch")
    assert nt<=p["limits"]["max_native_steps"]
    phi,stats=implicit_newmark_original_q0(
        scheme,native_dt_s=dt,native_source_ix=source,
        native_source_q0_weights=unit_src,
        native_receiver_ix=receiver,native_receiver_weights=out_weights.ravel(),
        native_record_samples=nt,sound_speed_m_s=c,
        solver_rtol=p["matched_time"]["rtol"],solver_atol=p["matched_time"]["atol"],
        max_cg_iter=p["matched_time"]["maxiter"],
        max_true_relative_residual=p["matched_time"]["true_relative_residual_max"])
    pressure=pffdtd_velocity_potential_to_pressure_trace(
        phi,time_step_s=dt,density_kg_m3=p["native_control"]["density_kg_m3"])
    q=np.zeros(nt);q[0]=1
    complex_spectra=finite_record_pressure_transfer(
        pressure,q,time_step_s=dt,frequency_hz=np.array([40.,80.]))
    if (nt!=treatment["original_record_samples"]
        or abs(dt-treatment["original_native_dt_s"])>1e-12
        or source.size!=8 or receiver.size!=8):
        raise ValueError("the two new numerical spatial arms do not share original point/q0/sample-time")
    data={"ppw":int(ppw),
        "original_exact_native_voxel_sha256":file_hash(sim/"vox_out.h5"),
        "original_exact_native_comms_sha256":file_hash(sim/"comms_out.h5"),
        "original_staircase_native_graph_volume_m3":scheme.room_fluid_volume_m3,
        "treatment_exact_cutcell_volume_m3":treatment["exact_physical_room_volume_m3"],
        "control_staircase_graph_room_nodes":scheme.number_of_cells,
        "treatment_exact_roof_room_nodes":treatment["original_nodal_active_cutcell_count"],
        "control_staircase_graph_K_nnz":int(scheme.stiffness_matrix.nnz),
        "original_native_full_baseline_P_T_Q_40_80":original_wave["unmodified_original_transfer_pa_per_m3_s"],
        "control_same_newmark_native_staircase_P_T_Q_40_80":pairs(complex_spectra),
        "treatment_same_newmark_exact_roof_P_T_Q_40_80":treatment["experimental_signed_P_T_over_Q_T_40_80"],
        "per_grid_signed_spatial_only_complex_relative_change":float(
            np.linalg.norm(complex_spectra-unpairs(treatment["experimental_signed_P_T_over_Q_T_40_80"]))/
            max(np.linalg.norm(unpairs(treatment["experimental_signed_P_T_over_Q_T_40_80"])),1e-12)),
        "time_source_receiver_sampling_newmark_matched":True,
        "original_physical_point_source_q0_unchanged":True,
        "control_implicit_solver":stats,
        "control_local_wall_seconds":float(time.perf_counter()-start)}
    print("MATCHED_TIME_SPATIAL_AB",ppw,
          "stair_m3",round(data["original_staircase_native_graph_volume_m3"],5),
          "exact_m3",round(data["treatment_exact_cutcell_volume_m3"],5),
          "complex_spatial_change",data["per_grid_signed_spatial_only_complex_relative_change"],
          "stair P_T/Q_T",data["control_same_newmark_native_staircase_P_T_Q_40_80"],
          flush=True)
    return data

def score_adjacent(rows,field):
    scored=[]
    for x,y in zip(rows,rows[1:]):
        result=compare_complex_transfer(
            reference=y[field],candidate=x[field],
            frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
        scored.append({"coarse_ppw":x["ppw"],"fine_ppw":y["ppw"],**result})
    return scored

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    a=ap.parse_args()
    raw=a.plan.read_bytes()
    p=validate_plan(json.loads(raw))
    tplan=verify_treatment_plan(json.loads((ROOT/p["preregistered_treatment"]["plan"]).read_text(encoding="utf-8")))
    tr=json.loads((ROOT/p["preregistered_treatment"]["evidence"]).read_text(encoding="utf-8"))
    if tr["preregistered_plan"]!=tplan or tr["new_scheme_adjacent_original_q0_point_transfers"]["experimental_provisional_numerical_refinement_pass"] is not False:
        raise ValueError("must preserve prior prospective failed original q0 exact-roof treatment")
    treatments={x["ppw"]:x for x in tr["actual_native_grid_point_impulse_exact_roof_cases"]}
    gplan=validate_graph_plan(json.loads((ROOT/"benchmarks/acoustics/r130d_original_pffdtd_neumann_graph_plan_2026-10-09.json").read_text(encoding="utf-8")))
    g=json.loads((ROOT/p["native_control"]["geometry"]).read_text(encoding="utf-8"))
    w=json.loads((ROOT/p["native_control"]["source_original"]).read_text(encoding="utf-8"))
    graphs={x["ppw"]:x for x in g["actual_original_voxel_grid_audits"]}
    orig={x["ppw"]:x for x in w["actual_native_wave_cases"]}
    if set(treatments)!=set(PPW) or set(graphs)!=set(PPW) or set(orig)!=set(PPW):
        raise ValueError("five original identical-grid treatment/controls missing")
    sims={}
    for candidate in a.original_sims_root.rglob("comms_out.h5"):
        sha=file_hash(candidate)
        ms=[i for i in PPW if orig[i]["original_native_comm_sha256"]==sha]
        if len(ms)==1:
            if ms[0] in sims:raise ValueError("duplicate original native q0 source")
            sims[ms[0]]=candidate.parent
    if set(sims)!=set(PPW):raise ValueError("original SHA-bound eight-node q0 source HDF5 missing")
    out={"schema_version":"htdt.r130d.original-point-q0-matched-newmark-spatial-ab-evidence-1",
         "plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
         "preregistered_plan":p,
         "original_canonical":"SELF_CONVERGENCE_FAILED",
         "physical_validation":"NOT_VALIDATED","product":"NO_GO",
         "github_actions_run_count":0,
         "actual_matched_time_spatial_AB_cases":[]}
    for ppw in PPW:
        try:r=one(p,ppw,sims[ppw],graphs[ppw],orig[ppw],treatments[ppw],gplan)
        except Exception as e:
            out["actual_matched_time_spatial_AB_cases"].append(
                {"ppw":ppw,"status":"INCOMPLETE","exception":type(e).__name__,"detail":str(e)})
            a.output.parent.mkdir(parents=True,exist_ok=True)
            a.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n")
            raise
        out["actual_matched_time_spatial_AB_cases"].append(r)
        a.output.parent.mkdir(parents=True,exist_ok=True)
        a.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n")
    records=out["actual_matched_time_spatial_AB_cases"]
    stair=score_adjacent(records,"control_same_newmark_native_staircase_P_T_Q_40_80")
    roof=score_adjacent(records,"treatment_same_newmark_exact_roof_P_T_Q_40_80")
    prev=tr["new_scheme_adjacent_original_q0_point_transfers"]["signed_all_four_adjacent_pairs"]
    for current,earlier in zip(roof,prev):
        if abs(current["complex_rms_relative"]-earlier["complex_rms_relative"])>1e-12:
            raise ValueError("original physical q0 roof treatment changed after original prior test")
    gate=p["acceptance"]
    def flags(a):return [
        r["complex_rms_relative"]<=gate["frozen_adjacent_original_complex_max"]
        and r["magnitude_max_relative"]<=gate["frozen_adjacent_original_magnitude_max"]
        and r["phase_max_deg"]<=gate["frozen_adjacent_original_phase_deg_max"] for r in a]
    out["matched_time_spatial_adjacent"]={
        "native_staircase_control":stair,"exact_roof_treatment":roof,
        "original_staircase_threshold_flags":flags(stair),
        "exact_roof_threshold_flags":flags(roof),
        "final_pair_experimental_staircase_pass":flags(stair)[-1],
        "final_pair_experimental_exact_roof_pass":flags(roof)[-1],
        "same_exact_original_source_receiver_q0_and_native_dt":True,
        "original_PFFDTD_8node_8_10_12_selfconvergence_still_FAILED":True,
        "not_a_production_solver_promotion":True}
    a.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n")
    print("MATCHED_TIME_SPATIAL_AB_DONE",[(a["coarse_ppw"],a["fine_ppw"],a["complex_rms_relative"],b["complex_rms_relative"]) for a,b in zip(stair,roof)],
          "original canonical still FAILED",flush=True)
if __name__=="__main__":main()
