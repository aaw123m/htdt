#!/usr/bin/env python3
"""Native original PFFDTD Neumann graph: low modes and exact original q0 coupling.

No amended upstream wave solver, no source changes, no CI trigger. Original
Neumann graph is constructed from SHA-pinned original voxelized rigid walls.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback

import h5py
import numpy as np
from scipy import sparse
from scipy.sparse.linalg import eigsh

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
from htdt.acoustic_pffdtd_adapter import (
    pffdtd_velocity_potential_to_pressure_trace,finite_record_pressure_transfer)
from run_r130d_original_point_quadratic_pffdtd import PPW,PIN,file_hash
from run_r130d_original_pffdtd_neumann_graph_audit import reconstruct_graph,validate_plan as validate_graph_plan

PLAN_SCHEMA="htdt.r130d.original-native-neumann-q0-modal-drift-plan-1"
FREQ=np.array([40.0,80.0])
def validate_plan(p):
    if (p.get("schema_version")!=PLAN_SCHEMA
        or p["source_authority"]["pffdtd_upstream_git_sha"]!=PIN
        or p["source_authority"]["original_ppw"]!=list(PPW)
        or p["source_authority"]["physical_source_xyz_m"]!=[1.5,2,2]
        or p["source_authority"]["physical_receiver_xyz_m"]!=[2.5,2,2]
        or p["source_authority"]["original_full_transfer_frequencies_hz"]!=[40,80]
        or p["spectral_problem"]["number_eigenpairs"]!=20
        or p["spectral_problem"]["solve"]!="scipy.sparse.linalg.eigsh(A,k=20,which=SM,tol=1e-9,maxiter=6000,ncv=60) CPU sparse symmetric Krylov; zero constant mode included"
        or p["spectral_problem"]["modal_count_not_sufficient_to_qualify"] is not True
        or p["limits"]["run_locally_only_no_github_actions"] is not True
        or p["limits"]["max_cases"]!=5
        or p["comparisons"]["original_state"]!="SELF_CONVERGENCE_FAILED"
        or p["release"]["production"]!="NO_GO"):
        raise ValueError("precommitted original native spectral plan changed")
    return p

def pairs(z):
    a=np.asarray(z,dtype=np.complex128)
    if a.shape!=(2,) or not np.all(np.isfinite(a)):
        raise ValueError("modal output requires two finite original signed bins")
    return [[float(v.real),float(v.imag)] for v in a]

def unpairs(a):
    x=np.asarray(a,dtype=float)
    if x.shape!=(2,2):raise ValueError("two-bin complex pair invalid")
    return x[:,0]+1j*x[:,1]

def native_room_laplacian(p,vox,graph_plan):
    _,_,_,_,_,visited,_,_,missing,rowsum,graph_edges=reconstruct_graph(
        graph_plan,vox,p["source_authority"]["physical_source_xyz_m"])
    if missing or rowsum!=0:raise ValueError("native Neumann room matrix asymmetric or not constant-conserving")
    n=len(visited)
    if not (20<n<=p["limits"]["max_graph_nodes"]):
        raise ValueError("room graph native geometry exceeds prereg resource ceiling")
    loc={i:j for j,i in enumerate(visited)}
    rr=[];cc=[]
    for native_index in visited:
        i=loc[native_index]
        for _,neighbor in graph_edges(native_index):
            if neighbor not in loc:raise ValueError("source connected native graph closure violated")
            j=loc[neighbor]
            if i<j:rr.append(i);cc.append(j)
    rr=np.asarray(rr,dtype=np.int32)
    cc=np.asarray(cc,dtype=np.int32)
    deg=np.bincount(np.concatenate([rr,cc]),minlength=n).astype(np.float64)
    indices=np.concatenate([rr,cc,np.arange(n,dtype=np.int32)])
    columns=np.concatenate([cc,rr,np.arange(n,dtype=np.int32)])
    values=np.concatenate([-np.ones(2*len(rr)),deg])
    A=sparse.coo_matrix((values,(indices,columns)),shape=(n,n)).tocsr()
    if A.nnz!=2*len(rr)+n:
        raise ValueError("native original graph matrix duplicate or diagonal inconsistency")
    if (A-A.T).nnz or not np.all(A.diagonal()==deg) or np.max(np.abs(np.asarray(A.sum(axis=1))))>1e-12:
        raise ValueError("original native Neumann algebraic symmetry failed")
    return A,loc,visited,deg

def native_q0_modal_approximation(lam,vecs,source_ix,source_w,receiver_ix,receiver_w,
                                    native_total_source,dt,h,c,rho,nt):
    source_proj=vecs[source_ix,:].T@np.asarray(source_w)
    receiver_proj=vecs[receiver_ix,:].T@np.asarray(receiver_w)
    coupling=source_proj*receiver_proj
    courant=(c*dt/h)**2
    theta=2*np.arcsin(np.sqrt(np.maximum(lam,0)*courant)/2)
    if np.any(~np.isfinite(theta)) or np.max(lam*courant)>4:
        raise ValueError("native leapfrog Laplacian eigenvalue exceeds stability")
    n=np.arange(nt,dtype=np.float64)
    mode_temporal=np.empty((len(theta),nt),dtype=np.float64)
    for i,t in enumerate(theta):
        mode_temporal[i,:]=n if abs(t)<1e-7 else np.sin(n*t)/np.sin(t)
    factors=np.asarray(coupling)*native_total_source
    spectrum=[]
    for i in range(len(theta)):
        phi=mode_temporal[i,:]*factors[i]
        pressure=pffdtd_velocity_potential_to_pressure_trace(
            phi,time_step_s=dt,density_kg_m3=rho)
        source=np.zeros(nt,dtype=np.float64);source[0]=1
        transfer=finite_record_pressure_transfer(
            pressure,source,time_step_s=dt,frequency_hz=FREQ)
        spectrum.append(transfer)
    spectrum=np.asarray(spectrum)
    summed=np.sum(spectrum,axis=0)
    # Independent direct pressure superposition must match the mode-by-mode P_T
    pressure_direct=pffdtd_velocity_potential_to_pressure_trace(
        factors@mode_temporal,time_step_s=dt,density_kg_m3=rho)
    unit=np.zeros(nt,dtype=float);unit[0]=1
    check=finite_record_pressure_transfer(
        pressure_direct,unit,time_step_s=dt,frequency_hz=FREQ)
    if not np.allclose(check,summed,rtol=1e-10,atol=1e-9):
        raise ValueError("mode-resolved native finite impulse signed transfer superposition violation")
    return theta,spectrum,summed,coupling

def eigensolve_native_room(A,k):
    start=time.perf_counter()
    lam,vecs=eigsh(A,k=k,which="SM",tol=1e-9,maxiter=6000,ncv=60)
    ix=np.argsort(lam,kind="stable")
    lam=lam[ix];vecs=vecs[:,ix]
    residual=np.asarray([np.linalg.norm(A@vecs[:,i]-lam[i]*vecs[:,i])
        /max(1.0,abs(lam[i]),np.linalg.norm(A@vecs[:,i]))
        for i in range(k)])
    return lam,vecs,residual,time.perf_counter()-start

def one_case(p,ppw,sim,prior_graph,prior_wave,graph_plan):
    for name,expected in (
        ("comms_out.h5",prior_wave["original_native_comm_sha256"]),
        ("vox_out.h5",prior_graph["original_exact_voxel_sha256"])):
        if file_hash(sim/name)!=expected:raise ValueError("original native input authority SHA drift "+name)
    with h5py.File(sim/"vox_out.h5","r") as vox:
        A,local,visited,deg=native_room_laplacian(p,vox,graph_plan)
    if (len(visited)!=prior_graph["source_connected_room_nodes"]
        or 2*int(np.sum(deg)/2)!=prior_graph["connected_non_ghost_directed_edges"]
        or A.shape[0]!=len(local)):
        raise ValueError("original source-connected native room graph differs from archived audit")
    with h5py.File(sim/"comms_out.h5","r") as comms:
        source_ix=np.asarray(comms["in_ixyz"][...],dtype=np.int64)
        source_raw=np.asarray(comms["in_sigs"][...],dtype=np.float64)
        recv_ix=np.asarray(comms["out_ixyz"][...],dtype=np.int64)
        recv_w=np.asarray(comms["out_alpha"][...],dtype=np.float64)
        nt=int(comms["Nt"][()])
        if (source_ix.shape!=(8,) or recv_ix.shape!=(8,)
            or recv_w.shape!=(1,8) or source_raw.shape!=(8,nt)
            or int(comms["diff"][()])!=0 or nt>p["limits"]["max_time_steps"]
            or np.any(source_raw[:,1:]!=0)):
            raise ValueError("original native eight-node q0 source or receiver mutated")
        scale=float(np.sum(source_raw[:,0]))
        src_w=source_raw[:,0]/scale
        recv_w=recv_w[0]
        if (abs(src_w.sum()-1)>1e-12 or abs(recv_w.sum()-1)>1e-12):
            raise ValueError("native source/receiver spatial quadrature not unit mass")
        try:
            source_local=np.array([local[int(i)] for i in source_ix],dtype=np.int64)
            recv_local=np.array([local[int(i)] for i in recv_ix],dtype=np.int64)
        except KeyError as exc:
            raise ValueError("original source/receiver 8-node stencil not contained in native room") from exc
    with h5py.File(sim/"sim_consts.h5","r") as h:
        grid_h=float(h["h"][()]);dt=float(h["Ts"][()]);native_l2=float(h["l2"][()])
        c=float(h["c"][()])
    if not (abs(grid_h-343.2/(100*ppw))<1e-11
        and abs(dt-prior_wave["native_solver"]["dt_s"])<1e-12
        and abs(c-p["source_authority"]["sound_speed_m_s"])<1e-12
        and abs(native_l2-(c*dt/grid_h)**2)<1e-11
        and abs(scale-native_l2/grid_h)<1e-10):
        raise ValueError("original native q0 time/space scaling differs from audited FDTD")
    k=int(p["spectral_problem"]["number_eigenpairs"])
    print("NATIVE_GRAPH_EIGENSOLVE_BEGIN",ppw,A.shape[0],"nnz",A.nnz,flush=True)
    lam,V,residual,seconds=eigensolve_native_room(A,k)
    if (np.max(residual)>p["limits"]["max_eigen_relative_residual"]
        or lam[0]<-1e-9 or abs(lam[0])>1e-8
        or not np.all(np.diff(lam)>=-1e-12)
        or not np.all(np.isfinite(V))):
        raise ValueError("frozen original native Laplacian spectral residual/order violated")
    lam=np.maximum(lam,0.0)
    theta,bins,sum_bins,coupling=native_q0_modal_approximation(
        lam,V,source_local,src_w,recv_local,recv_w,
        scale,dt,grid_h,c,p["source_authority"]["density_kg_m3"],nt)
    f_cont=c/(2*np.pi*grid_h)*np.sqrt(lam)
    f_native=theta/(2*np.pi*dt)
    if f_native[0]>p["spectral_problem"]["zero_mode_max_abs_hz"]:
        raise ValueError("Neumann no-pressure zero mode frequency unexpectedly nonzero")
    full=unpairs(prior_wave["unmodified_original_transfer_pa_per_m3_s"])
    actual_modal=[{"mode_index":i,
       "dimensionless_laplacian_lambda":float(lam[i]),
       "frequency_continuous_time_hz":float(f_cont[i]),
       "frequency_native_leapfrog_hz":float(f_native[i]),
       "leapfrog_minus_continuous_hz":float(f_native[i]-f_cont[i]),
       "true_original_neumann_eigen_relative_residual":float(residual[i]),
       "unit_eigenvector_source_receiver_signed_coupling":float(coupling[i]),
       "finite_250ms_signed_original_q0_pressure_transfer_40_80":pairs(bins[i])}
       for i in range(k)]
    nearest=[]
    for f in FREQ:
        idx=int(np.argmin(abs(f_native-f)))
        nearest.append({"target_frequency_hz":float(f),
           "nearest_native_mode_index":idx,
           "nearest_native_eigenfrequency_hz":float(f_native[idx]),
           "native_mode_frequency_offset_from_target_hz":float(f_native[idx]-f),
           "mode_signed_source_receiver_coupling":float(coupling[idx])})
    err=float(np.linalg.norm(sum_bins-full)/max(np.linalg.norm(full),1e-12))
    result={
        "ppw":int(ppw),"exact_original_native_voxel_sha256":file_hash(sim/"vox_out.h5"),
        "exact_original_native_comms_sha256":file_hash(sim/"comms_out.h5"),
        "exact_original_full_wave_sim_outs_sha256":file_hash(sim/"sim_outs.h5"),
        "grid_h_m":grid_h,"native_dt_s":dt,"native_cfl_squared":native_l2,
        "original_q0_native_source_total_injection":scale,
        "native_room_reachable_vertices":int(A.shape[0]),
        "native_neumann_laplacian_nnz":int(A.nnz),
        "eigensolver_wall_seconds":float(seconds),
        "max_true_relative_eigen_residual":float(max(residual)),
        "eigenpair_count":k,"eigenmodes":actual_modal,
        "nearest_to_40_80_hz":nearest,
        "original_full_native_signed_40_80":pairs(full),
        "first_20_modal_signed_40_80":pairs(sum_bins),
        "lowmode_vs_original_full_native_complex_relative_norm":err,
        "q0_and_spatial_trilinear_source_receiver_unchanged":True,
        "all_original_rigid_boundary_neumann_edges_unchanged":True,
        "low_modes_diagnostic_not_full_band_qualification":True}
    print("NATIVE_EIGENSOLVE_COMPLETE",ppw,"seconds",round(seconds,2),
          "nearest",[(round(x["nearest_native_eigenfrequency_hz"],3),x["nearest_native_mode_index"]) for x in nearest],
          "max eig residual",max(residual),"20mode-vs-full",err,flush=True)
    return result

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args()
    raw=args.plan.read_bytes()
    p=validate_plan(json.loads(raw))
    gp=validate_graph_plan(json.loads((ROOT/"benchmarks/acoustics/r130d_original_pffdtd_neumann_graph_plan_2026-10-09.json").read_text()))
    graph=json.loads((ROOT/p["source_authority"]["previous_original_graph_evidence"]).read_text())
    waves=json.loads((ROOT/p["source_authority"]["previous_original_wave_evidence"]).read_text())
    gmap={x["ppw"]:x for x in graph["actual_original_voxel_grid_audits"]}
    wmap={x["ppw"]:x for x in waves["actual_native_wave_cases"]}
    if set(gmap)!=set(PPW) or set(wmap)!=set(PPW):
        raise ValueError("original five PPW native rigid graph or wave cases missing")
    sims={}
    for path in args.original_sims_root.rglob("comms_out.h5"):
        sha=file_hash(path)
        match=[i for i in PPW if sha==wmap[i]["original_native_comm_sha256"]]
        if len(match)==1:
            if match[0] in sims:raise ValueError("duplicate raw native PPW source authority")
            sims[match[0]]=path.parent
    if set(sims)!=set(PPW):raise ValueError("native exact original pinned eight-node source asset missing")
    output={
        "schema_version":"htdt.r130d.original-native-neumann-q0-modal-drift-evidence-1",
        "plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":p,
        "pinned_exact_upstream_sha":PIN,
        "original_q0_fullband_state":"SELF_CONVERGENCE_FAILED",
        "physical_validation":"NOT_VALIDATED","product":"NO_GO",
        "actual_original_native_modal_levels":[]}
    for ppw in PPW:
        try:
            case=one_case(p,ppw,sims[ppw],gmap[ppw],wmap[ppw],gp)
        except Exception as exc:
            output["actual_original_native_modal_levels"].append(
                {"ppw":ppw,"status":"EIGEN_DIAGNOSTIC_INCOMPLETE",
                 "failure_type":type(exc).__name__,"failure_detail":str(exc)})
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        output["actual_original_native_modal_levels"].append(case)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    prev=output["actual_original_native_modal_levels"]
    output["same_mode_index_native_frequency_hz_drift_from_ppw44"]=[
        {"ppw":r["ppw"],"frequencies_k1_to_k19_delta_hz":[
            float(r["eigenmodes"][i]["frequency_native_leapfrog_hz"]-
                  prev[-1]["eigenmodes"][i]["frequency_native_leapfrog_hz"])
            for i in range(1,20)],
         "warning":"index matching not mode-shape tracking: near degeneracies may reorder"}
        for r in prev]
    output["diagnostic_only_did_not_rerun_or_change_original_native_wave"]=True
    args.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("ORIGINAL_NATIVE_20_EIGENPAIRS_FIVE_GRIDS_COMPLETED; ORIGINAL_SELF_CONVERGENCE_FAILED",flush=True)
if __name__=="__main__":
    main()
