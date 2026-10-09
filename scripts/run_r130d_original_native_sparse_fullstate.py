#!/usr/bin/env python3
"""Complete independent CSR time evolution for pinned original 8-node PFFDTD.

No upstream PFFDTD engine called. No source/receiver/geometry/physics altered.
Exact native 8-node raw recordings are independently checked sample-by-sample.
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
from scipy.sparse import csr_matrix

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
from htdt.acoustic_pffdtd_adapter import (
    pffdtd_velocity_potential_to_pressure_trace,
    finite_record_pressure_transfer,
    recombine_pffdtd_receiver_traces)
from run_r130d_original_point_quadratic_pffdtd import PPW,PIN,file_hash
from run_r130d_original_native_modal_drift import native_room_laplacian,unpairs,pairs
from run_r130d_original_pffdtd_neumann_graph_audit import validate_plan as graph_plan_validate

SCHEMA="htdt.r130d.original-native-sparse-fullstate-independent-plan-1"

def validate_plan(p):
    if not (
        p.get("schema_version")==SCHEMA
        and p["original_inputs"]["ppw"]==list(PPW)
        and p["original_inputs"]["pffdtd_git_pin"]==PIN
        and p["original_inputs"]["physical_source_receiver_xyz_m"]==[[1.5,2,2],[2.5,2,2]]
        and p["original_inputs"]["frequencies_hz"]==[40,80]
        and p["independent_solver"]["method"]==
            "fully untruncated CSR matrix-vector leapfrog u_next=2*u_current-u_previous-l2*A@u_current; inject exact native in_sigs[:,n] after update; raw receiver node samples u_current before injection each n"
        and p["independent_solver"]["max_raw_trace_relative_l2"]==1e-8
        and p["independent_solver"]["max_discrete_energy_relative_drift"]==1e-7
        and p["scope"]["github_actions_runs"]==0
        and p["release"]["original_point_q0_self_convergence"]=="SELF_CONVERGENCE_FAILED"
        and p["release"]["production"]=="NO_GO"):
        raise ValueError("prospective five original grid full sparse PFFDTD experiment changed")
    return p

def relative_l2(actual,expected):
    a=np.asarray(actual);b=np.asarray(expected)
    if a.shape!=b.shape or np.any(~np.isfinite(a)) or np.any(~np.isfinite(b)):
        raise ValueError("independently computed native full trace missing/nonfinite")
    return float(np.linalg.norm(a-b)/max(float(np.linalg.norm(b)),1e-15))

def independent_native_csr_leapfrog(A,dt,nt,l2,source_local,source_first,
                                   recv_local,energy_at):
    if not isinstance(A,csr_matrix) or A.shape[0]!=A.shape[1]:
        raise ValueError("native source-connected graph must be CSR square")
    n=A.shape[0]
    if nt<3 or nt>2000 or len(source_local)!=8 or len(recv_local)!=8:
        raise ValueError("native recurrence source/receiver/time invalid")
    if l2<=0 or l2>=1/3:
        raise ValueError("nonphysical or unstable native Cartesian CFL^2")
    u_prev=np.zeros(n,dtype=np.float64)
    u_current=np.zeros(n,dtype=np.float64)
    src=np.zeros(n,dtype=np.float64)
    np.add.at(src,np.asarray(source_local,dtype=np.int64),np.asarray(source_first,dtype=np.float64))
    receiver=np.empty((8,nt),dtype=np.float64)
    energies=[]
    checkpoints=set(int(t) for t in energy_at)
    start=time.perf_counter()
    for step in range(nt):
        receiver[:,step]=u_current[recv_local]
        lap=A@u_current
        if step in checkpoints:
            E=float(np.dot(u_current-u_prev,u_current-u_prev)/l2
                 +np.dot(u_prev,lap))
            energies.append({"sample_n":int(step),"native_discrete_energy":E})
        u_next=2*u_current-u_prev-l2*lap
        if step==0:
            u_next+=src
        u_prev,u_current=u_current,u_next
    if len(energies)!=len(checkpoints):
        raise RuntimeError("independent source-free energy probes incomplete")
    vals=[x["native_discrete_energy"] for x in energies]
    drift=float((max(vals)-min(vals))/max(abs(vals[0]),1e-30))
    if not np.isfinite(drift):raise ValueError("original native discrete energy nonfinite")
    return receiver,energies,drift,float(time.perf_counter()-start)

def one(p,ppw,sim,graph_row,wave_row,graph_plan):
    if (file_hash(sim/"vox_out.h5")!=graph_row["original_exact_voxel_sha256"]
        or file_hash(sim/"comms_out.h5")!=wave_row["original_native_comm_sha256"]):
        raise ValueError("native original graph/source SHA authority drift")
    with h5py.File(sim/"vox_out.h5","r") as h:
        modal_spec={"source_authority":{"physical_source_xyz_m":p["original_inputs"]["physical_source_receiver_xyz_m"][0]},
                    "limits":{"max_graph_nodes":p["resource"]["max_active_room_nodes"]}}
        A,local,visited,degree=native_room_laplacian(modal_spec,h,graph_plan)
    if (A.shape[0]!=graph_row["source_connected_room_nodes"]
        or graph_row["native_halo_ghost_edge_count_excluded_from_room_graph"]!=0):
        raise ValueError("nonzero native halo edge or original native room graph changed")
    with h5py.File(sim/"comms_out.h5","r") as h:
        native_in=np.asarray(h["in_ixyz"][...],dtype=np.int64)
        native_out=np.asarray(h["out_ixyz"][...],dtype=np.int64)
        in_sigs=np.asarray(h["in_sigs"][...],dtype=np.float64)
        out_w=np.asarray(h["out_alpha"][...],dtype=np.float64)
        order=np.asarray(h["out_reorder"][...],dtype=np.int64)
        nt=int(h["Nt"][()])
        if (native_in.shape!=(8,) or native_out.shape!=(8,)
            or in_sigs.shape!=(8,nt) or out_w.shape!=(1,8)
            or not np.array_equal(in_sigs[:,1:],np.zeros_like(in_sigs[:,1:]))
            or not np.array_equal(order,np.arange(8))
            or int(h["diff"][()])!=0):
            raise ValueError("native eight-node point q0 source or receiver waveform drifted")
        try:
            ins=np.array([local[int(x)] for x in native_in],dtype=np.int64)
            outs=np.array([local[int(x)] for x in native_out],dtype=np.int64)
        except KeyError as exc:
            raise ValueError("original source/receiver outside source-connected room") from exc
    with h5py.File(sim/"sim_consts.h5","r") as h:
        native_dt=float(h["Ts"][()]);native_l2=float(h["l2"][()])
        h_m=float(h["h"][()]);c=float(h["c"][()])
    if (nt>p["resource"]["max_wave_steps"]
        or A.shape[0]>p["resource"]["max_active_room_nodes"]
        or abs(h_m-343.2/(100*ppw))>1e-12
        or abs(native_dt-wave_row["native_solver"]["dt_s"])>1e-12
        or abs(native_l2-(native_dt*c/h_m)**2)>1e-12):
        raise ValueError("native original discrete room CFL/duration/geometry drifted")
    with h5py.File(sim/"sim_outs.h5","r") as h:
        raw=np.asarray(h["u_out"][...],dtype=np.float64)
        if raw.shape!=(8,nt):raise ValueError("original native raw 8-node full record changed")
    cpts=[1,nt//4,nt//2,nt-1]
    sparse_out,energies,drift,seconds=independent_native_csr_leapfrog(
        A,native_dt,nt,native_l2,ins,in_sigs[:,0],outs,cpts)
    waveform_rel=relative_l2(sparse_out,raw)
    raw_max=float(np.max(np.abs(sparse_out-raw)))
    phi_original=recombine_pffdtd_receiver_traces(raw,out_w,receiver_count=1,nt=nt)[0]
    phi_independent=recombine_pffdtd_receiver_traces(
        sparse_out,out_w,receiver_count=1,nt=nt)[0]
    phi_rel=relative_l2(phi_independent,phi_original)
    rho=float(p["original_inputs"].get("density_kg_m3",1.2))
    pa_orig=pffdtd_velocity_potential_to_pressure_trace(
        phi_original,time_step_s=native_dt,density_kg_m3=rho)
    pa_sparse=pffdtd_velocity_potential_to_pressure_trace(
        phi_independent,time_step_s=native_dt,density_kg_m3=rho)
    src=np.zeros(nt,dtype=np.float64);src[0]=1
    freq=np.array([40.,80.])
    h_orig=finite_record_pressure_transfer(pa_orig,src,time_step_s=native_dt,frequency_hz=freq)
    h_sparse=finite_record_pressure_transfer(pa_sparse,src,time_step_s=native_dt,frequency_hz=freq)
    raw_transfer_rel=relative_l2(h_sparse,h_orig)
    if not np.allclose(h_orig,unpairs(wave_row["unmodified_original_transfer_pa_per_m3_s"]),
                           rtol=1e-10,atol=1e-8):
        raise ValueError("native original recorded q0 full transfer reference did not reproduce")
    outcome={
        "ppw":ppw,
        "original_exact_vox_sha256":file_hash(sim/"vox_out.h5"),
        "original_exact_source_comms_sha256":file_hash(sim/"comms_out.h5"),
        "original_exact_raw_native_wave_sha256":file_hash(sim/"sim_outs.h5"),
        "original_graph_room_nodes":len(visited),
        "native_full_stencil_nonzeros":int(A.nnz),
        "original_native_record_sample_count":nt,
        "native_time_step_s":native_dt,
        "native_courant_squared":native_l2,
        "unmodified_native_input_total_q0":float(in_sigs[:,0].sum()),
        "independent_full_room_wave_wall_seconds":seconds,
        "all_eight_original_node_raw_trace_relative_l2":waveform_rel,
        "all_eight_original_node_raw_trace_max_abs":raw_max,
        "original_weighted_receiver_potential_relative_l2":phi_rel,
        "original_40_80_complex_pressure_transfer_relative_l2":raw_transfer_rel,
        "native_exact_original_P_T_over_Q_T_40_80":pairs(h_orig),
        "independent_full_state_CSR_P_T_over_Q_T_40_80":pairs(h_sparse),
        "discrete_homogeneous_energy_probes":energies,
        "discrete_energy_relative_drift":drift,
        "independent_pinned_numba_kernels_not_called":True,
        "native_source_receiver_and_wall_unchanged":True,
        "original_8node_q0_diagnostic_no_promotion":True}
    bounds=p["independent_solver"]
    outcome["independent_full_state_reproduction_within_frozen_bounds"]=bool(
        waveform_rel<=bounds["max_raw_trace_relative_l2"]
        and phi_rel<=bounds["max_recombined_trace_relative_l2"]
        and raw_transfer_rel<=bounds["max_signed_transfer_relative_l2"]
        and drift<=bounds["max_discrete_energy_relative_drift"])
    print("ORIGINAL_SPARSE_FULLSTATE",ppw,"native",A.shape[0],"wall_s",round(seconds,2),
          "raw_trace_rel",waveform_rel,"transfer_rel",raw_transfer_rel,
          "discrete_energy_rel_drift",drift,"PASS",outcome["independent_full_state_reproduction_within_frozen_bounds"],flush=True)
    return outcome

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    a=ap.parse_args()
    raw=a.plan.read_bytes()
    p=validate_plan(json.loads(raw))
    graph_plan=graph_plan_validate(json.loads(
        (ROOT/"benchmarks/acoustics/r130d_original_pffdtd_neumann_graph_plan_2026-10-09.json").read_text(encoding="utf-8")))
    graph=json.loads((ROOT/p["original_inputs"]["graph_evidence"]).read_text(encoding="utf-8"))
    wave=json.loads((ROOT/p["original_inputs"]["full_original_wave_control_evidence"]).read_text(encoding="utf-8"))
    graphs={x["ppw"]:x for x in graph["actual_original_voxel_grid_audits"]}
    waves={x["ppw"]:x for x in wave["actual_native_wave_cases"]}
    if set(graphs)!=set(PPW) or set(waves)!=set(PPW):
        raise ValueError("missing preregistered original PPW rigid graph and PFFDTD full wave")
    sims={}
    for f in a.original_sims_root.rglob("comms_out.h5"):
        sha=file_hash(f)
        ppw_candidates=[i for i in PPW if sha==waves[i]["original_native_comm_sha256"]]
        if len(ppw_candidates)==1:
            ppw=ppw_candidates[0]
            if ppw in sims:raise ValueError("duplicate exact original source HDF5 PPW")
            sims[ppw]=f.parent
    if set(sims)!=set(PPW):
        raise ValueError("all five exact original SHA-pinned native source setups required")
    result={
        "schema_version":"htdt.r130d.original-native-sparse-fullstate-independent-evidence-1",
        "plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":p,
        "pffdtd_original_upstream":PIN,
        "original_point_q0":"SELF_CONVERGENCE_FAILED",
        "physical_validation":"NOT_VALIDATED",
        "production":"NO_GO",
        "native_full_state_comparisons":[]}
    for ppw in PPW:
        try:
            case=one(p,ppw,sims[ppw],graphs[ppw],waves[ppw],graph_plan)
        except Exception as exc:
            result["native_full_state_comparisons"].append(
                {"ppw":ppw,"status":"INDEPENDENT_FULLSTATE_FAILED",
                 "error_type":type(exc).__name__,"error_detail":str(exc)})
            a.output.parent.mkdir(parents=True,exist_ok=True)
            a.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        result["native_full_state_comparisons"].append(case)
        a.output.parent.mkdir(parents=True,exist_ok=True)
        a.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    result["all_5_original_native_PFFDTD_8node_full_raw_waveforms_reproduced"]=all(
        x["independent_full_state_reproduction_within_frozen_bounds"] for x in result["native_full_state_comparisons"])
    result["source_receiver_geometry_time_resolution_unchanged"]=True
    result["github_actions_launched_for_experiment"]=0
    result["canonical_original_selfconvergence_still_failed"]=True
    a.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("ALL_5_NATIVE_SPARSE_FULLSTATE_INDEPENDENT_DONE",
          result["all_5_original_native_PFFDTD_8node_full_raw_waveforms_reproduced"],
          "original numerical convergence NOT_QUALIFIED",flush=True)

if __name__=="__main__":
    main()
