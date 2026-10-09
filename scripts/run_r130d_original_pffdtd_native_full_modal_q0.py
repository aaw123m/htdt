#!/usr/bin/env python3
"""Full EXACT original upstream PFFDTD graph eigen-spectrum & native q0 P_T/Q_T.

No upstream solver modification, no source smoothing, actual ORIGINAL voxel
HDF5 and source/receiver 8-node q0 SHA. Factorization is verified, not assumed.
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
from scipy import sparse, linalg

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_point_quadratic_pffdtd import PPW,PIN,file_hash
from run_r130d_original_native_modal_drift import (
    native_room_laplacian,unpairs,pairs)
from run_r130d_original_pffdtd_neumann_graph_audit import validate_plan as check_graph_plan
from run_r130d_native_exact_roof_full_xy_z_modal_q0 import (
    stable_q_progression,stable_sin_ratio)

SCHEMA="htdt.r130d.original-pffdtd-native-full-kronecker-modal-q0-plan-1"
BANDS=((0,100),(100,200),(200,400),(400,800),(800,1_000_000_000))
FREQ=np.array([40.,80.])
def validate_plan(p):
    if (p.get("schema_version")!=SCHEMA
        or p["original_authority"]["frozen_original_native_ppw"]!=list(PPW)
        or p["original_authority"]["pffdtd_sha"]!=PIN
        or p["original_authority"]["original_source_xyz_m"]!=[1.5,2,2]
        or p["original_authority"]["original_receiver_xyz_m"]!=[2.5,2,2]
        or p["original_authority"]["original_full_no_taper_record_s"]!=.25
        or p["original_authority"]["frequencies_hz"]!=[40,80]
        or p["spatial"]["matrix_max_absolute_difference"]!=0
        or p["solver"]["true_original_8node_all_raw_wave_matched_relative_max"]!=2e-6
        or p["bins"]["semidiscrete_frequencies_hz"]!=[list(x) for x in BANDS]
        or p["bins"]["original_frozen_complex_threshold"]!=.2
        or p["bins"]["original_frozen_magnitude_threshold"]!=.25
        or p["bins"]["original_frozen_phase_deg_threshold"]!=15
        or p["limits"]["no_github_actions_runs"] is not True
        or p["release"]["original_run25_8node_PFFDTD_q0"]!="SELF_CONVERGENCE_FAILED"
        or p["release"]["product"]!="NO_GO"):
        raise ValueError("original native full PFFDTD all-mode future experiment plan mutated")
    return p

def graph_from_original_6_neighbors_ppw(native_graph,visited,grid_shape):
    """Fail closed if original native room is NOT an exact product x × yz."""
    source_ids=np.asarray(visited,dtype=np.int64)
    n=int(len(source_ids))
    if native_graph.shape!=(n,n):raise ValueError("original native graph dimensions drift")
    nx,ny,nz=map(int,grid_shape)
    iz,jz,kz=np.unravel_index(source_ids,(nx,ny,nz))
    native_x=np.unique(iz)
    native_yz=np.unique(jz*nz+kz)
    canonical=(native_x[:,None]*ny*nz+native_yz[None,:]).ravel()
    if not np.array_equal(np.sort(source_ids),canonical):
        raise ValueError("original source-connected native stair-stepped room not exact x⊗yz product")
    order=np.argsort(source_ids)
    sorted_graph=native_graph[order,:][:,order].tocsr()
    loc_x={int(i):ii for ii,i in enumerate(native_x)}
    loc_yz={int(ik):ii for ii,ik in enumerate(native_yz)}
    def adjacency_laplacian(count,edges):
        rows=np.array([x for x,y in edges],dtype=np.int32)
        cols=np.array([y for x,y in edges],dtype=np.int32)
        deg=np.bincount(np.r_[rows,cols],minlength=count).astype(float)
        return sparse.coo_matrix(
            (np.r_[deg,-np.ones(2*len(edges))],
             (np.r_[np.arange(count),rows,cols],
              np.r_[np.arange(count),cols,rows])),
             shape=(count,count)).tocsr()
    x_edges=[(i,loc_x[int(j+1)]) for j,i in loc_x.items() if (j+1) in loc_x]
    yz_edges=[]
    for original,j in loc_yz.items():
        iy,iz=divmod(original,nz)
        if iy+1<ny and (iy+1)*nz+iz in loc_yz:
            yz_edges.append((j,loc_yz[(iy+1)*nz+iz]))
        if iz+1<nz and iy*nz+iz+1 in loc_yz:
            yz_edges.append((j,loc_yz[iy*nz+iz+1]))
    Ax=adjacency_laplacian(len(native_x),x_edges)
    Ayz=adjacency_laplacian(len(native_yz),yz_edges)
    predicted=(sparse.kron(Ax,sparse.eye(len(native_yz),format="csr"),format="csr")
              +sparse.kron(sparse.eye(len(native_x),format="csr"),Ayz,format="csr")).tocsr()
    delta=(predicted-sorted_graph).tocsr()
    max_abs=float(max(np.abs(delta.data),default=0.))
    if max_abs!=0 or predicted.shape!=sorted_graph.shape:
        raise ValueError(f"ORIGINAL PFFDTD connected native six-way Neumann graph not separable: max_abs={max_abs}")
    return Ax,Ayz,native_x,native_yz,{
        "original_room_native_flat_C_order_exact":True,
        "original_connected_nodes":n,
        "original_directed_rigid_neumann_edges":int(sorted_graph.nnz-n),
        "original_full_native_staircase_A_equals_kron_Ax_I_plus_I_Ayz":True,
        "original_native_Kronecker_neumann_matrix_max_abs":max_abs,
        "original_x_native_nodes":len(native_x),
        "original_yz_native_nodes":len(native_yz),
        "original_unmodified_voxel_room_mask_used":True}

def original_native_tensor_point_source_receiver(grid_shape,native_x,native_yz,indices,weights):
    """Original archived 8 point HDF5 source/receiver exactly factorized."""
    nx,ny,nz=map(int,grid_shape)
    xloc={int(j):int(i) for i,j in enumerate(native_x)}
    yzloc={int(j):int(i) for i,j in enumerate(native_yz)}
    inds=np.asarray(indices,dtype=np.int64)
    w=np.asarray(weights,dtype=float)
    if (len(inds)!=8 or len(w)!=8 or abs(w.sum()-1)>1e-12):
        raise ValueError("native eight point q0 physical source/receiver mass altered")
    mat=np.zeros((len(native_x),len(native_yz)))
    for flat,weight in zip(inds,w):
        ix,iy,iz=np.unravel_index(int(flat),(nx,ny,nz))
        yz=iy*nz+iz
        if ix not in xloc or yz not in yzloc:
            raise ValueError("native PFFDTD 8point point outside original connected room")
        mat[xloc[ix],yzloc[yz]]+=weight
    wx=mat.sum(axis=1)
    wy=mat.sum(axis=0)
    err=float(np.max(abs(mat-np.outer(wx,wy))))
    if err>1e-12 or abs(sum(wx)-1)>1e-12 or abs(sum(wy)-1)>1e-12:
        raise ValueError("ORIGINAL PFFDTD eight native trilinear point no longer tensor separates")
    return wx,wy,err

def true_native_leapfrog_exact_finite_signed_transfer(theta,amplitude,dt,nt,rho):
    """Exactly original PFFDTD leapfrog q0 and full finite rectangular 2-bin P_T/Q_T.

    phi[0]=0; phi[1]=amplitude, phi[n]=amplitude sin(n theta)/sin(theta).
    p from archived identical 2nd-order centered/endpoint derivative.
    """
    theta=np.asarray(theta,dtype=float).ravel()
    amplitude=np.asarray(amplitude,dtype=float).ravel()
    if theta.shape!=amplitude.shape or theta.size==0 or nt<3 or nt>2000:
        raise ValueError("original native q0 modal amplitudes/time invalid")
    if not np.all(np.isfinite(theta)) or not np.all(np.isfinite(amplitude)):
        raise ValueError("native q0 original modal spectrum nonfinite")
    cos=np.cos(theta)
    end=stable_sin_ratio(nt-1,theta)+(cos-2)*stable_sin_ratio(nt-2,theta)
    signed=np.empty((2,len(theta)),dtype=np.complex128)
    for i,f in enumerate(FREQ):
        shift=2*np.pi*f*dt
        inside=.5*(stable_q_progression(nt-2,shift+theta)+
                   stable_q_progression(nt-2,shift-theta))
        result=(2-cos)+inside+np.exp(1j*shift*(nt-1))*end
        signed[i,:]=rho*amplitude/dt*result
    if not np.all(np.isfinite(signed)):
        raise ValueError("ORIGINAL PFFDTD whole record native mode signed transfer not finite")
    return signed

def one_original_native(p,ppw,sim,original,graph_result,graph_plan,
                        original_partition):
    t0=time.perf_counter()
    if (file_hash(sim/"comms_out.h5")!=original["original_native_comm_sha256"]
        or file_hash(sim/"vox_out.h5")!=graph_result["original_exact_voxel_sha256"]):
        raise ValueError("original unmodified native HDF5 source/room mask SHA drift")
    with h5py.File(sim/"vox_out.h5","r") as voxel:
        dims=tuple(int(voxel[k][()]) for k in ("Nx","Ny","Nz"))
        native_spec={
            "source_authority":{"physical_source_xyz_m":[1.5,2,2]},
            "limits":{"max_graph_nodes":p["limits"]["max_room_nodes"]}}
        A,loc,visited,degree=native_room_laplacian(
            native_spec,voxel,graph_plan)
    if (len(visited)!=graph_result["source_connected_room_nodes"]
        or A.nnz-len(visited)!=graph_result["connected_non_ghost_directed_edges"]):
        raise ValueError("ORIGINAL native wall/graph changed from preregistered graph audit")
    Ax,Ayz,xids,yzids,identity=graph_from_original_6_neighbors_ppw(A,visited,dims)
    if (len(xids)>p["limits"]["max_x_nodes"]
        or len(yzids)>p["limits"]["max_2d_yz_nodes"]):
        raise ValueError("original native PFFDTD cross-section memory too large")
    with h5py.File(sim/"comms_out.h5","r") as h:
        si=np.asarray(h["in_ixyz"][:],dtype=np.int64)
        sig=np.asarray(h["in_sigs"][:],dtype=float)
        ri=np.asarray(h["out_ixyz"][:],dtype=np.int64)
        rw=np.asarray(h["out_alpha"][:],dtype=float).ravel()
        nt=int(h["Nt"][()])
        if (si.shape!=(8,) or ri.shape!=(8,) or sig.shape!=(8,nt)
            or rw.shape!=(8,) or np.any(sig[:,1:]!=0)
            or int(h["diff"][()])!=0 or nt>p["limits"]["max_full_record_samples"]):
            raise ValueError("original EXACT native temporal q0 and eight-point observation altered")
        strength=float(sig[:,0].sum())
        if strength<=0:raise ValueError("original q0 native source energy non-positive")
        sw=sig[:,0]/strength
    with h5py.File(sim/"sim_consts.h5","r") as h:
        dt=float(h["Ts"][()]);grid_h=float(h["h"][()])
        c=float(h["c"][()]);l2=float(h["l2"][()])
    if (abs(dt-original["native_solver"]["dt_s"])>1e-12
        or abs(c-343.2)>1e-12
        or abs(grid_h-343.2/(100*ppw))>1e-12
        or abs(l2-(c*dt/grid_h)**2)>1e-12
        or abs(strength-l2/grid_h)>1e-10):
        raise ValueError("original native time/space/source numerical scaling drift")
    sx,syz,sres=original_native_tensor_point_source_receiver(dims,xids,yzids,si,sw)
    rx,ryz,rres=original_native_tensor_point_source_receiver(dims,xids,yzids,ri,rw)
    time0=time.perf_counter()
    lx,vx=linalg.eigh(Ax.toarray(),check_finite=True)
    ly,vy=linalg.eigh(Ayz.toarray(),check_finite=True)
    print("ORIGINAL_PFFDTD_EIGEN_COMPLETE",ppw,"NyZ",len(ly),"x",len(lx),
          "dense-eigen-seconds",round(time.perf_counter()-time0,2),flush=True)
    if lx[0]<-1e-9 or ly[0]<-1e-9:
        raise ValueError("original native symmetry fails positive Neumann spectrum")
    lx=np.maximum(lx,0.)
    ly=np.maximum(ly,0.)
    xres=np.linalg.norm(Ax@vx-vx*lx[None,:],axis=0)
    yzres=np.linalg.norm(Ayz@vy-vy*ly[None,:],axis=0)
    rel_res=float(max(np.max(xres),np.max(yzres)))
    if rel_res>3e-7:
        raise ValueError(f"original graph eigenmodes true residual {rel_res} exceeded numerical condition")
    interaction=np.outer((sx@vx)*(rx@vx),(syz@vy)*(ryz@vy))
    lam=lx[:,None]+ly[None,:]
    step_squared=l2*lam
    if np.min(step_squared)<-5e-8 or np.max(step_squared)>4+1e-10:
        raise ValueError("actual original PFFDTD q0 leapfrog modal CFL invalid")
    theta=2*np.arcsin(.5*np.sqrt(np.clip(step_squared,0,4)))
    all_signed=true_native_leapfrog_exact_finite_signed_transfer(
        theta.ravel(),(strength*interaction).ravel(),dt,nt,p["original_authority"]["rho_kg_m3"])
    h_model=all_signed.sum(axis=1)
    frozen_h=unpairs(original["unmodified_original_transfer_pa_per_m3_s"])
    earlier_partition=unpairs(original_partition["time_components"]["full_original_40_80_complex"])
    if not np.allclose(frozen_h,earlier_partition,rtol=1e-10,atol=5e-7):
        raise ValueError("frozen exact original q0 both physical pressure full-record controls disagree")
    relative=float(np.linalg.norm(h_model-frozen_h)/max(np.linalg.norm(frozen_h),1e-14))
    if relative>p["solver"]["true_original_8node_all_raw_wave_matched_relative_max"]:
        raise ValueError(f"ALL exact ORIGINAL PFFDTD q0 native spectrum failed frozen raw wave signed P_T/Q_T rel={relative}")
    f_cont=c/(2*np.pi*grid_h)*np.sqrt(lam.ravel())
    f_native=theta.ravel()/(2*np.pi*dt)
    bands=[]
    for low,high in BANDS:
        mask=(f_cont>=low)&(f_cont<high)
        bands.append({"semidiscrete_mode_frequency_band_hz":[low,high],
                      "full_original_native_3D_mode_count":int(mask.sum()),
                      "signed_original_8node_q0_full250ms_P_T_over_Q_T_40_80":pairs(
                          all_signed[:,mask].sum(axis=1))})
    if sum(q["full_original_native_3D_mode_count"] for q in bands)!=len(visited):
        raise ValueError("original PFFDTD all native modes not assigned into disjoint spectral bands")
    tops={}
    for i,f in enumerate(FREQ):
        ranks=np.argsort(abs(all_signed[i]))[::-1][:20]
        tops[str(int(f))]=[
            {"x_eigen_index":int(j//len(ly)),"yz_eigen_index":int(j%len(ly)),
             "semidiscrete_mode_frequency_hz":float(f_cont[int(j)]),
             "native_leapfrog_modified_frequency_hz":float(f_native[int(j)]),
             "individual_signed_full250ms_transfer_40_80":pairs(all_signed[:,int(j)]),
             "absolute_in_original_scored_frequency_bin":float(abs(all_signed[i,int(j)]))}
            for j in ranks]
    source_zero_native=bool(0<=lx[0]<=1e-8 and 0<=ly[0]<=1e-8)
    return {
        "ppw":ppw,
        "original_native_comms_SHA256":file_hash(sim/"comms_out.h5"),
        "original_native_voxels_SHA256":file_hash(sim/"vox_out.h5"),
        "original_native_sim_output_SHA256":file_hash(sim/"sim_outs.h5"),
        "exact_original_connected_staircase_graph_tensor_identity":identity,
        "exact_original_8point_source_tensor_residual":sres,
        "exact_original_8point_receiver_tensor_residual":rres,
        "original_native_time_step_s":dt,
        "original_native_impulse_q0_total_in_sigs_source_strength":strength,
        "original_record_samples":nt,
        "original_native_CFL_squared":l2,
        "original_native_const_neumann_zero_mode_present":source_zero_native,
        "true_full_eigenmode_max_absolute_Av_minus_lambda_v":rel_res,
        "actual_native_full_3D_modes_count":int(len(visited)),
        "full_x_native_graph_modes":len(lx),
        "full_yz_native_original_staircase_graph_modes":len(ly),
        "lowest32_original_yz_native_semidiscrete_mode_hz":list(map(float,c/(2*np.pi*grid_h)*np.sqrt(ly[:32]))),
        "lowest16_original_x_native_semidiscrete_mode_hz":list(map(float,c/(2*np.pi*grid_h)*np.sqrt(lx[:16]))),
        "highest_original_native_FDTD_mode_semidiscrete_frequency_hz":float(max(f_cont)),
        "highest_original_native_leapfrog_modified_mode_frequency_hz":float(max(f_native)),
        "exact_original_saved_true_PFFDTD_q0_signed_40_80":pairs(frozen_h),
        "entire_true_original_native_PFFDTD_3D_mode_sum_signed_40_80":pairs(h_model),
        "all_original_native_PFFDTD_modes_vs_original_saved_full_wave_complex_rel":relative,
        "all_native_actual_original_PFFDTD_modes_signed_spectral_band_transfer":bands,
        "top20_original_native_impulse_coupled_3D_modes_each_40_80_bin":tops,
        "original_pffdtd_source_and_full250ms_40_80_unmodified":True,
        "original_native_modes_not_experimentally_damped_or_removed":True,
        "single_case_wall_seconds":float(time.perf_counter()-t0)}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    a=ap.parse_args()
    raw=a.plan.read_bytes()
    p=validate_plan(json.loads(raw.decode("utf-8")))
    prior=json.loads((ROOT/p["original_authority"]["raw_native_wave_SHA_evidence"]).read_text(encoding="utf-8"))
    waves={r["ppw"]:r for r in prior["actual_native_wave_cases"]}
    gra=json.loads((ROOT/p["original_authority"]["native_true_rigid_graph_audit"]).read_text(encoding="utf-8"))
    graphs={r["ppw"]:r for r in gra["actual_original_voxel_grid_audits"]}
    partition=json.loads((ROOT/p["original_authority"]["native_original_high_ppw_signed_wave_partition"]).read_text(encoding="utf-8"))
    parts={r["ppw"]:r for r in partition["levels"]}
    graph_plan=check_graph_plan(json.loads(
        (ROOT/"benchmarks/acoustics/r130d_original_pffdtd_neumann_graph_plan_2026-10-09.json").read_text(encoding="utf-8")))
    if (set(waves)!=set(PPW) or set(graphs)!=set(PPW) or set(parts)!=set(PPW)
        or gra["original_fullband_q0"]!="SELF_CONVERGENCE_FAILED"):
        raise ValueError("missing original true PFFDTD five-grid native source and graph full q0 evidence")
    dirs={}
    for f in a.original_sims_root.rglob("comms_out.h5"):
        h=file_hash(f)
        match=[i for i in PPW if waves[i]["original_native_comm_sha256"]==h]
        if len(match)==1:
            if match[0] in dirs:raise ValueError("duplicate original native q0 HDF5 case")
            dirs[match[0]]=f.parent
    if set(dirs)!=set(PPW):raise ValueError("exact original five native HDF5 point q0 assets missing")
    output={"schema_version":"htdt.r130d.original-pffdtd-native-full-kronecker-modal-q0-evidence-1",
        "plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":p,"upstream_PIN":PIN,
        "canonical_original_point_q0":"SELF_CONVERGENCE_FAILED",
        "physical_validation":"NOT_VALIDATED","product":"NO_GO",
        "new_GitHub_Actions_launched":0,
        "actual_original_unmodified_all_mode_native_cases":[]}
    for ppw in PPW:
        try:z=one_original_native(p,ppw,dirs[ppw],waves[ppw],graphs[ppw],
                                   graph_plan,parts[ppw])
        except Exception as err:
            output["actual_original_unmodified_all_mode_native_cases"].append({
                "ppw":ppw,"status":"EXACT_ORIGINAL_NATIVE_MODE_DIAGNOSTIC_FAILED",
                "failure_type":type(err).__name__,"failure_detail":str(err)})
            a.output.parent.mkdir(parents=True,exist_ok=True)
            a.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        output["actual_original_unmodified_all_mode_native_cases"].append(z)
        a.output.parent.mkdir(parents=True,exist_ok=True)
        a.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("ORIGINAL_UNMODIFIED_PFFDTD_FULL_NATIVE_MODE_WAVE",ppw,
              "entire 3D native modes",z["actual_native_full_3D_modes_count"],
              "signed original control relative",z["all_original_native_PFFDTD_modes_vs_original_saved_full_wave_complex_rel"],
              "wall_s",round(z["single_case_wall_seconds"],2),flush=True)
    rows=output["actual_original_unmodified_all_mode_native_cases"]
    adjacent=[]
    for first,second in zip(rows,rows[1:]):
        orig=(unpairs(first["exact_original_saved_true_PFFDTD_q0_signed_40_80"])-
             unpairs(second["exact_original_saved_true_PFFDTD_q0_signed_40_80"]))
        reconstructed=(unpairs(first["entire_true_original_native_PFFDTD_3D_mode_sum_signed_40_80"])-
                      unpairs(second["entire_true_original_native_PFFDTD_3D_mode_sum_signed_40_80"]))
        # Signed coarse-to-fine original complex difference may show
        # cancellation across band. Preserve both real and imaginary bins.
        norm=float(np.linalg.norm(orig))
        bands=[]
        check=np.zeros(2,dtype=complex)
        for x,y in zip(first["all_native_actual_original_PFFDTD_modes_signed_spectral_band_transfer"],
                       second["all_native_actual_original_PFFDTD_modes_signed_spectral_band_transfer"]):
            if x["semidiscrete_mode_frequency_band_hz"]!=y["semidiscrete_mode_frequency_band_hz"]:
                raise ValueError("native original case frequency band definitions changed")
            delta=(unpairs(x["signed_original_8node_q0_full250ms_P_T_over_Q_T_40_80"])-
                  unpairs(y["signed_original_8node_q0_full250ms_P_T_over_Q_T_40_80"]))
            check+=delta
            bands.append({
                "original_native_physical_semidiscrete_mode_band_hz":x["semidiscrete_mode_frequency_band_hz"],
                "original_coarse_mode_count":x["full_original_native_3D_mode_count"],
                "original_fine_mode_count":y["full_original_native_3D_mode_count"],
                "original_signed_complex_delta_40_80":pairs(delta),
                "relative_complex_delta_norm_to_full_original":float(np.linalg.norm(delta)/max(norm,1e-12)),
                "not_additive_percentage_due_to_signed_complex_cancellation":True})
        if (not np.allclose(check,reconstructed,rtol=3e-10,atol=3e-6)
            or not np.allclose(reconstructed,orig,rtol=p["solver"]["true_original_8node_all_raw_wave_matched_relative_max"],atol=3e-4)):
            raise ValueError("all signed PFFDTD original five-native PPW full-spectrum decomposition did not reproduce archived original coarse/fine delta")
        same=compare_complex_transfer(
            reference=second["exact_original_saved_true_PFFDTD_q0_signed_40_80"],
            candidate=first["exact_original_saved_true_PFFDTD_q0_signed_40_80"],
            frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
        adjacent.append({
            "original_coarse_ppw":first["ppw"],
            "original_fine_ppw":second["ppw"],
            "original_full_250ms_signed_delta":pairs(orig),
            "original_full_native_PPWave_two_frequency_refinement":same,
            "original_full_signed_band_causal_differences":bands,
            "all_original_3D_mode_signed_reconstruction_conservation":True})
    output["all_four_actual_original_PFFDTD_adjacent_q0_mode_band_differences"]=adjacent
    output["actual_original_point_q0_full_wave_acceptance_still_failed"]=True
    output["original_PFFDTD_native_source_solver_HDF5_unmodified"]=True
    a.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("ALL_FIVE_ORIGINAL_NATIVE_PFFDTD_ALL_MODE_ANALYSIS_COMPLETED",
          [(q["original_coarse_ppw"],q["original_fine_ppw"],
            q["original_full_native_PPWave_two_frequency_refinement"]["complex_rms_relative"],
            [round(v["relative_complex_delta_norm_to_full_original"],4) for v in q["original_full_signed_band_causal_differences"]])
            for q in adjacent],
          "CANONICAL_R130D_FAILED_NO_GO",flush=True)
if __name__=="__main__":
    main()
