#!/usr/bin/env python3
"""Original q0 fixed-M/K one-sided physical-point 8/Q2 and x-Neumann mode audit.

All 250ms native original PFFDTD sample times. Experimental exact-roof FV
rather than original explicit PFFDTD; no Gaussian source or changed q0.
"""
from __future__ import annotations
import argparse
import hashlib
import itertools
import json
from pathlib import Path
import sys
import time
import numpy as np
import h5py
from scipy import linalg
from scipy.sparse.linalg import cg, LinearOperator
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
from htdt.r130d_native_grid_exact_roof_fv import build_native_exact_roof_fv
from htdt.acoustic_pffdtd_adapter import (
    pffdtd_velocity_potential_to_pressure_trace, finite_record_pressure_transfer)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_exact_roof_point_q0_x_grid_phase import trilinear_physical_point
from run_r130d_original_point_quadratic_pffdtd import file_hash,PIN
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs,verify_plan as verify_roof_plan

SCHEMA="htdt.r130d.exact-roof-original-physical-point-q0-one-sided-q2-and-x-mode-plan-1"
PPW=(40,44)
COMBINATIONS=("8/8","Q2/8","8/Q2","Q2/Q2")
OFFSETS=(-.25,0.,.25)

def validate_plan(p):
    if (p.get("schema_version")!=SCHEMA
        or p["immutable_original"]["ppw"]!=list(PPW)
        or p["immutable_original"]["upstream_git_pin"]!=PIN
        or p["immutable_original"]["original_source_xyz_m"]!=[1.5,2,2]
        or p["immutable_original"]["original_receiver_xyz_m"]!=[2.5,2,2]
        or p["immutable_original"]["original_frequencies_hz"]!=[40,80]
        or p["immutable_original"]["original_record_s"]!=.25
        or p["spatial_operator"]["number_of_full_wave_integrations"]!=4
        or p["spatial_operator"]["max_true_relative_residual"]!=5e-10
        or p["x_spectral"]["grid_phase_offsets_h"]!=list(OFFSETS)
        or p["comparison"]["frozen_complex_rms_relative_max"]!=.2
        or p["comparison"]["frozen_magnitude_relative_max"]!=.25
        or p["comparison"]["frozen_phase_deg_max"]!=15
        or p["limits"]["no_github_actions_runs"] is not True
        or p["release"]["original_eight_node_q0"]!="SELF_CONVERGENCE_FAILED"
        or p["release"]["product"]!="NO_GO"):
        raise ValueError("frozen Q2 one-sided point impulse/x-mode plan mutated")
    return p

def cardinal_q2_point(axes,xyz):
    """Original physical Dirac delta: 3 nearest nodes, unique polynomial Q2.

    Weights can be negative. Sum and 1st/2nd raw moments are exact:
    polynomial reproduction is a different spatial discrete point functional
    for the SAME physical delta, not a smoothed finite-width source.
    """
    if len(axes)!=3 or len(xyz)!=3:raise ValueError("Q2 interpolation requires 3D")
    dims=tuple(len(a) for a in axes)
    loc=[]
    coeff=[]
    for axis,z in zip(axes,xyz):
        ax=np.asarray(axis,dtype=float)
        if (ax.ndim!=1 or len(ax)<4 or not np.all(np.isfinite(ax))
            or not np.all(np.diff(ax)>0) or not ax[0]<z<ax[-1]):
            raise ValueError("Q2 physical original point outside valid axes")
        ids=np.sort(np.argsort(abs(ax-z),kind="stable")[:3])
        pts=ax[ids]
        weights=np.asarray([
          np.prod([(z-pts[j])/(pts[i]-pts[j]) for j in range(3) if j!=i])
          for i in range(3)])
        if not np.isclose(weights.sum(),1.,rtol=0,atol=1e-13):
            raise ValueError("quadratic Lagrange normalization failed")
        if not np.allclose([weights@pts**order for order in (1,2)],
            [z,z*z],rtol=0,atol=1e-12):
            raise ValueError("quadratic Lagrange physical point polynomial moment failed")
        loc.append(ids)
        coeff.append(weights)
    indices=[];weights=[];position=[]
    for xyz_id in itertools.product((0,1,2),repeat=3):
        grid=tuple(int(loc[i][xyz_id[i]]) for i in range(3))
        indices.append(int(np.ravel_multi_index(grid,dims)))
        weights.append(float(np.prod([coeff[i][xyz_id[i]] for i in range(3)])))
        position.append([float(axes[i][grid[i]]) for i in range(3)])
    idx=np.asarray(indices,dtype=np.int64)
    wt=np.asarray(weights,dtype=float)
    pt=np.asarray(position,dtype=float)
    if len(set(map(int,idx)))!=27 or abs(wt.sum()-1)>1e-12:
        raise ValueError("Q2 original delta requires 27 unique native nodes")
    actual_first=wt@pt
    second=wt@(pt**2)
    if not np.allclose(actual_first,xyz,rtol=0,atol=1e-12) or not np.allclose(
        second,np.asarray(xyz)**2,rtol=0,atol=1e-12):
        raise ValueError("original physical delta 0/1/2 polynomial tensor moments fail")
    return idx,wt,{
        "physical_point_xyz_m":list(map(float,xyz)),
        "node_count":27,
        "sum_signed_weights":float(wt.sum()),
        "negative_weight_count":int(np.sum(wt<0)),
        "reconstructed_first_moment_xyz_m":list(map(float,actual_first)),
        "reconstructed_second_raw_moment_xyz2_m2":list(map(float,second)),
        "sum_absolute_weights":float(np.sum(abs(wt))),
        "no_gaussian_or_spatial_point_move":True}

def exact_original_eight_point(axes,xyz,original_native_ix,original_native_weights):
    ix,w,meta=trilinear_physical_point(axes,xyz)
    actual={int(i):float(v) for i,v in zip(ix,w)}
    frozen={int(i):float(v) for i,v in zip(original_native_ix,original_native_weights)}
    if (len(frozen)!=8 or actual.keys()!=frozen.keys()
        or any(abs(actual[i]-frozen[i])>5e-11 for i in actual)):
        raise ValueError("original physical 8 point native PFFDTD HDF5 operator not reproduced")
    return ix,w,meta

def native_x_sep_mode(axes_x,phase,h,c,src_xyz,rec_xyz):
    """Check exact x-separable slab mode; full yz modes not approximated."""
    xx=np.asarray(axes_x,dtype=float)+float(phase)*h
    lo=np.maximum(xx-h/2,0.);hi=np.minimum(xx+h/2,4.)
    active=np.flatnonzero(hi-lo>1e-14)
    wx=(hi-lo)[active]
    edge=[]
    for j in range(len(active)-1):
        i=int(active[j]);k=int(active[j+1])
        if k!=i+1:raise ValueError("original native x Neumann strip not contiguous")
        area=(xx[i]+xx[k])/2
        edge.append(float(0.<area<4.))
    if not all(edge):raise ValueError("native physical Neumann x endwall cut incorrectly")
    n=len(active)
    K=np.zeros((n,n),dtype=float)
    for j in range(n-1):
        t=c*c/h
        K[j,j]+=t;K[j+1,j+1]+=t;K[j,j+1]-=t;K[j+1,j]-=t
    xM=wx**-.5
    ev,vec=linalg.eigh(xM[:,None]*K*xM[None,:],subset_by_index=(0,3))
    ev=np.maximum(ev,0.)
    f=np.sqrt(ev)/(2*np.pi)
    vv=xM[:,None]*vec
    def physical_one_dim(x):
        ind=int(np.searchsorted(xx,x,side="right")-1)
        if not 0<=ind<len(xx)-1:raise ValueError("x physics point outside")
        loc={int(i):j for j,i in enumerate(active)}
        if ind not in loc or ind+1 not in loc:raise ValueError("x physical point outside active room")
        t=(x-xx[ind])/(xx[ind+1]-xx[ind])
        w=np.zeros(n);w[loc[ind]]=1-t;w[loc[ind+1]]=t
        if abs(w@xx[active]-x)>1e-12 or abs(sum(w)-1)>1e-12:
            raise ValueError("x source/receiver moment altered")
        return w
    s=physical_one_dim(float(src_xyz[0]))
    r=physical_one_dim(float(rec_xyz[0]))
    product=np.asarray((r@vv)*(s@vv))
    analytical=c/(2*4)
    return {
        "grid_phase_h":float(phase),
        "x_active_node_count":int(n),
        "mass_length_x_m":float(sum(wx)),
        "smallest_x_mass_fraction_of_h":float(min(wx)/h),
        "continuum_flat_x_Neumann_first_frequency_hz":float(analytical),
        "finite_volume_neumann_1d_first_three_positive_modes_hz":list(map(float,f[1:4])),
        "native_x_mode1_minus_continuum_hz":float(f[1]-analytical),
        "first_three_signed_original_point_x_mode_couplings":list(map(float,product[1:4])),
        "native_constant_Neumann_x_mode_zero_frequency_hz":float(f[0]),
        "roof_yz_operator_not_in_x_1d_diagnostic":True}

def full_native_impulse_fixed_MK_dual_receivers(system,dt,nt,
                                            source_ix,source_weights,
                                            receivers,
                                            source_strength_c2):
    """One full q0 original wave; simultaneously sample both original 8 and Q2.

    Same K,M and same midpoint Newmark recurrence as the original prior
    exact roof experiment. Generalizes signed source/receiver point weights.
    """
    if nt>2000 or not 3<=nt or dt<=0 or len(receivers)!=2:
        raise ValueError("native q0 time/receiver controls invalid")
    n=system.number_of_cells
    lut=np.full(int(np.prod(system.native_dimensions)),-1,dtype=np.int32)
    lut[system.original_native_flat_indices]=np.arange(n,dtype=np.int32)
    src_ix=np.asarray(source_ix,dtype=np.int64)
    src_w=np.asarray(source_weights,dtype=np.float64)
    if (src_ix.shape!=src_w.shape or src_w.ndim!=1 or
        src_w.size not in (8,27) or abs(src_w.sum()-1)>1e-12
        or np.any(lut[src_ix]<0) or not np.all(np.isfinite(src_w))):
        raise ValueError("same physical point source weights invalid")
    receiver_idx=[];receiver_w=[]
    for ix,wt in receivers:
        ix=np.asarray(ix,dtype=np.int64);wt=np.asarray(wt,dtype=float)
        if (ix.ndim!=1 or wt.shape!=ix.shape or len(ix) not in (8,27)
            or np.any(lut[ix]<0) or abs(wt.sum()-1)>1e-12):
            raise ValueError("same physical receiver outside original exact room")
        receiver_idx.append(lut[ix])
        receiver_w.append(wt)
    B=(system.mass_matrix+(dt**2/4)*system.stiffness_matrix).tocsr()
    A=system.stiffness_matrix
    m=system.fluid_volume_m3
    pre=1/np.asarray(B.diagonal(),dtype=float)
    prec=LinearOperator((n,n),matvec=lambda z:pre*z,dtype=np.float64)
    peak_it=0;peak_rel=0.;solves=0
    def solve(rhs,x0=None):
        nonlocal peak_it,peak_rel,solves
        iter_count=[0]
        def cb(_):iter_count[0]+=1
        answer,code=cg(B,rhs,x0=x0,M=prec,rtol=1e-10,atol=1e-12,
                       maxiter=500,callback=cb)
        relative=float(np.linalg.norm(B@answer-rhs)/max(np.linalg.norm(rhs),1e-30))
        if code!=0 or relative>5e-10:raise ValueError(
            f"original native q0 generalized solver did not converge {code} relative={relative}")
        peak_it=max(peak_it,iter_count[0]);peak_rel=max(peak_rel,relative);solves+=1
        return answer
    forcing=np.zeros(n,dtype=np.float64)
    np.add.at(forcing,lut[src_ix],source_strength_c2*src_w)
    kick=dt**2*solve(forcing)
    u0=np.zeros(n,dtype=np.float64)
    u1=np.zeros(n,dtype=np.float64)
    out=np.empty((2,nt),dtype=np.float64)
    check_at={1,nt//4,nt//2,nt-1}
    energies=[]
    last_accel=np.zeros(n,dtype=np.float64)
    for step in range(nt):
        for j,(loc,weight) in enumerate(zip(receiver_idx,receiver_w)):
            out[j,step]=weight@u1[loc]
        if step in check_at:
            diff=u1-u0
            midpoint=(u1+u0)/2
            energy=float(midpoint@(A@midpoint)+np.dot(m,diff*diff)/dt**2)
            energies.append({"sample_index":int(step),
                             "homogeneous_midpoint_native_energy":energy})
        if step==0:
            new=kick
        else:
            accel=solve(A@u1,x0=last_accel)
            new=2*u1-u0-dt**2*accel
            last_accel=accel
        u0,u1=u1,new
    e=[z["homogeneous_midpoint_native_energy"] for z in energies]
    drift=float((max(e)-min(e))/max(abs(e[0]),1e-30))
    if drift>1e-6:raise ValueError("native original q0 Newmark homogeneous energy failed")
    return out,{
        "solver":"exact native Newmark beta=1/4, same frozen MK, jacobi CG",
        "number_of_linear_solves":solves,"max_cg_iterations":peak_it,
        "max_true_CG_relative_residual":peak_rel,
        "source_free_energy_probe_values":energies,
        "source_free_energy_relative_drift":drift,
        "no_physical_source_reposition_or_temporal_smoothing":True}

def original_transfer(pressure_traces,dt):
    nt=pressure_traces.shape[-1]
    q=np.zeros(nt,dtype=float);q[0]=1
    return [
        finite_record_pressure_transfer(
            pffdtd_velocity_potential_to_pressure_trace(phi,time_step_s=dt,density_kg_m3=1.2),
            q,time_step_s=dt,frequency_hz=np.array([40.,80.]))
        for phi in pressure_traces]

def one_full(ppw,sim,prior,roof,src_control,source_type):
    start=time.perf_counter()
    if (file_hash(sim/"vox_out.h5")!=prior["original_solver_geometry_sha256"]
        or file_hash(sim/"comms_out.h5")!=prior["original_native_comm_sha256"]):
        raise ValueError("original SHA-pinned native sources not authentic")
    with h5py.File(sim/"vox_out.h5","r") as h:
        axes=[np.asarray(h[k][...],dtype=float) for k in ("xv","yv","zv")]
    with h5py.File(sim/"comms_out.h5","r") as h:
        source_original=np.asarray(h["in_ixyz"][:],dtype=np.int64)
        sig=np.asarray(h["in_sigs"][:],dtype=float)
        recv_original=np.asarray(h["out_ixyz"][:],dtype=np.int64)
        recv_weights=np.asarray(h["out_alpha"][:],dtype=float).ravel()
        nt=int(h["Nt"][()])
        if (sig.shape!=(8,nt) or np.any(sig[:,1:]!=0)
            or source_original.shape!=(8,) or recv_original.shape!=(8,)
            or recv_weights.shape!=(8,) or nt>2000
            or int(h["diff"][()])!=0):
            raise ValueError("original native q0 not unit single discrete pulse")
    with h5py.File(sim/"sim_consts.h5","r") as h:
        dt=float(h["Ts"][()]);c=float(h["c"][()]);hh=float(h["h"][()])
    if abs(hh-roof["native_original_h_m"])>1e-12 or abs(dt-roof["original_native_dt_s"])>1e-12:
        raise ValueError("native q0 experiment source sample time changed")
    s8,s8w,s8meta=exact_original_eight_point(
        axes,[1.5,2,2],source_original,sig[:,0]/sum(sig[:,0]))
    r8,r8w,r8meta=exact_original_eight_point(
        axes,[2.5,2,2],recv_original,recv_weights)
    s2,s2w,s2meta=cardinal_q2_point(axes,[1.5,2,2])
    r2,r2w,r2meta=cardinal_q2_point(axes,[2.5,2,2])
    system=build_native_exact_roof_fv(axes,max_nodes=150000)
    if (abs(system.room_fluid_volume_m3-56)>2e-8
        or system.number_of_cells!=roof["original_nodal_active_cutcell_count"]
        or int(system.stiffness_matrix.nnz)!=roof["native_exact_roof_stiffness_nonzeros"]):
        raise ValueError("fixed exact roof M/K changed from original intervention controls")
    source_ix,source_w=(s8,s8w) if source_type=="8" else (s2,s2w)
    raw,solver=full_native_impulse_fixed_MK_dual_receivers(
        system,dt,nt,source_ix,source_w,[(r8,r8w),(r2,r2w)],c*c)
    bins=original_transfer(raw,dt)
    raw8=pairs(bins[0])
    if source_type=="8":
        old=unpairs(roof["experimental_signed_P_T_over_Q_T_40_80"])
        diff=float(np.linalg.norm(bins[0]-old)/max(np.linalg.norm(old),1e-14))
        if diff>5e-8:raise ValueError(
            f"full fixed M/K original 8/8 waveform did not independently reproduce frozen exact roof baseline ({diff})")
    else:
        diff=None
    print("ORIGINAL_POINT_SOURCE_OPERATOR_FULL_WAVE",ppw,source_type,
          "8recv",raw8,"Q2recv",pairs(bins[1]),
          "Q2signedweights",s2meta["sum_absolute_weights"],
          "energy",solver["source_free_energy_relative_drift"],
          "elapsed",time.perf_counter()-start,flush=True)
    return {
        "ppw":ppw,"discrete_source_operator":source_type,
        "frozen_original_geometry_sha256":file_hash(sim/"vox_out.h5"),
        "frozen_original_8node_source_sha256":file_hash(sim/"comms_out.h5"),
        "fixed_MK_exact_roof_room_volume_m3":system.room_fluid_volume_m3,
        "fixed_MK_active_cells":system.number_of_cells,
        "fixed_MK_stiffness_nnz":int(system.stiffness_matrix.nnz),
        "source_original_8node_control":s8meta,
        "receiver_original_8node_control":r8meta,
        "source_Q2_original_physical_point":s2meta,
        "receiver_Q2_original_physical_point":r2meta,
        "time_step_s":dt,"original_sample_count":nt,
        "spatial_Q2_source_vs_eight_difference_only":source_type=="Q2",
        "signed_original_40_80_receivers":{
           "receiver8":pairs(bins[0]),"receiverQ2":pairs(bins[1])},
        "original_8_by_8_baseline_signed_complex_relative_reproduction":diff,
        "solver":solver,
        "elapsed_seconds":float(time.perf_counter()-start),
        "source_receiver_physical_xyz_unchanged":True,
        "all_original_temporal_q0_unchanged":True,
        "experimental_not_original_PFFDTD_qualification":True}

def score_pair(coarse,fine):
    return compare_complex_transfer(
        reference=fine,candidate=coarse,frequency_hz=[40,80],
        magnitude_mask_relative_db=-50).model_dump(mode="json")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args()
    raw=args.plan.read_bytes()
    p=validate_plan(json.loads(raw.decode("utf-8")))
    prior=json.loads((ROOT/p["immutable_original"]["original_comms_control"]).read_text(encoding="utf-8"))
    base=json.loads((ROOT/p["immutable_original"]["unshifted_prior_evidence"]).read_text(encoding="utf-8"))
    base_plan=verify_roof_plan(json.loads((ROOT/"benchmarks/acoustics/r130d_native_grid_exact_roof_mass_fv_q0_plan_2026-10-09.json").read_text(encoding="utf-8")))
    shift=json.loads((ROOT/p["immutable_original"]["previous_x_shift_evidence"]).read_text(encoding="utf-8"))
    if (base["preregistered_plan"]!=base_plan or base["product"]!="NO_GO"
        or shift["product"]!="NO_GO" or shift["original_canonical"]!="SELF_CONVERGENCE_FAILED"):
        raise ValueError("preexisting exact roof / x grid point q0 control immutable evidence modified")
    sources={r["ppw"]:r for r in prior["actual_native_wave_cases"] if r["ppw"] in PPW}
    baselines={r["ppw"]:r for r in base["actual_native_grid_point_impulse_exact_roof_cases"] if r["ppw"] in PPW}
    if set(sources)!=set(PPW) or set(baselines)!=set(PPW):
        raise ValueError("both original source q0 PPW40/44 controls missing")
    dirs={}
    for path in args.original_sims_root.rglob("comms_out.h5"):
        sha=file_hash(path)
        ppw=[k for k in PPW if sha==sources[k]["original_native_comm_sha256"]]
        if len(ppw)==1:
            if ppw[0] in dirs:raise ValueError("duplicate original q0 HDF5")
            dirs[ppw[0]]=path.parent
    if set(dirs)!=set(PPW):raise ValueError("original q0 PPW40/44 external SHA HDF5 not available")
    evidence={
        "schema_version":"htdt.r130d.original-exact-roof-fixed-mk-operator-and-x-mode-evidence-1",
        "plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":p,
        "original_canonical_point_q0":"SELF_CONVERGENCE_FAILED",
        "physical_validation":"NOT_VALIDATED","product":"NO_GO",
        "new_github_actions_started":0,
        "actual_1D_native_x_modes_and_point_overlap":[],
        "actual_3D_original_q0_full_wave_fixed_MK_operator_cases":[]}
    for ppw in PPW:
        with h5py.File(dirs[ppw]/"vox_out.h5","r") as h:
            x=np.asarray(h["xv"][:],dtype=float)
            native_h=float(h["h"][()])
        records=[]
        for phase in OFFSETS:
            records.append(native_x_sep_mode(
                x,phase,native_h,p["immutable_original"].get("sound_speed_m_s",343.2),
                p["immutable_original"]["original_source_xyz_m"],
                p["immutable_original"]["original_receiver_xyz_m"]))
        evidence["actual_1D_native_x_modes_and_point_overlap"].append({
            "ppw":ppw,
            "original_pffdtd_native_vox_sha256":file_hash(dirs[ppw]/"vox_out.h5"),
            "phase_modes":records})
        print("NATIVE_EXACT_X_SEPARABLE_NEUMANN_MODE",ppw,
              [(z["grid_phase_h"],z["finite_volume_neumann_1d_first_three_positive_modes_hz"][0],
                z["first_three_signed_original_point_x_mode_couplings"][0]) for z in records],
              flush=True)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    for ppw in PPW:
        for source_type in ("8","Q2"):
            try:row=one_full(ppw,dirs[ppw],sources[ppw],baselines[ppw],p,source_type)
            except Exception as exc:
                evidence["actual_3D_original_q0_full_wave_fixed_MK_operator_cases"].append({
                    "ppw":ppw,"discrete_source_operator":source_type,
                    "status":"NUMERICAL_EXPERIMENT_FAILED",
                    "failure_type":type(exc).__name__,"failure_detail":str(exc)})
                args.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
                raise
            evidence["actual_3D_original_q0_full_wave_fixed_MK_operator_cases"].append(row)
            args.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    results={}
    for ppw in PPW:
        records={z["discrete_source_operator"]:z
             for z in evidence["actual_3D_original_q0_full_wave_fixed_MK_operator_cases"]
             if z["ppw"]==ppw}
        if set(records)!={"8","Q2"}:raise ValueError("missing fixed spatial MK one-sided point operators")
        a=records["8"]["signed_original_40_80_receivers"]
        b=records["Q2"]["signed_original_40_80_receivers"]
        results[ppw]={
            "8/8":a["receiver8"],"Q2/8":b["receiver8"],
            "8/Q2":a["receiverQ2"],"Q2/Q2":b["receiverQ2"]}
    adjacent=[]
    for label in COMBINATIONS:
        score=score_pair(results[40][label],results[44][label])
        adjacent.append({
            "source_receiver_numerical_point_operator":label,**score,
            "meets_frozen_original_40_44_gates":bool(
                score["complex_rms_relative"]<=.2
                and score["magnitude_max_relative"]<=.25
                and score["phase_max_deg"]<=15)})
    each={}
    for ppw in PPW:
        full=unpairs(results[ppw]["8/8"])
        each[ppw]={
          label:float(np.linalg.norm(unpairs(signed)-full)/max(np.linalg.norm(full),1e-14))
          for label,signed in results[ppw].items() if label!="8/8"}
    if any(x["meets_frozen_original_40_44_gates"] for x in adjacent):
        print("EXPERIMENTAL_SUBGATE_PASS_DOES_NOT_QUALIFY_CANONICAL",flush=True)
    old=score_pair(baselines[40]["experimental_signed_P_T_over_Q_T_40_80"],
                   baselines[44]["experimental_signed_P_T_over_Q_T_40_80"])
    if abs(adjacent[0]["complex_rms_relative"]-old["complex_rms_relative"])>5e-8:
        raise ValueError("8/8 fixed M/K frozen baseline signed PPW comparison drift")
    evidence["fixed_MK_signed_40_80_operator_transfer_by_ppw"]=results
    evidence["spatial_operator_ab_original_q0_ppw40_44_adjacent_scores"]=adjacent
    evidence["each_ppw_one_sided_operator_relative_complex_effect_vs_original8_8"]=each
    evidence["original_exact_roof_prior8_8_ppw40_44_frozen_score"]=old
    evidence["only_original_ppw40_44_not_full_canonical_8_10_12"]=True
    evidence["no_changed_native_original_PFFDTD_wave_solver"]=True
    args.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("ORIGINAL_POINT_Q0_FIXED_MK_ONE_SIDED_DONE",
      [(a["source_receiver_numerical_point_operator"],
        a["complex_rms_relative"],a["magnitude_max_relative"],a["phase_max_deg"],
        a["meets_frozen_original_40_44_gates"]) for a in adjacent],
      "ORIGINAL_SELF_CONVERGENCE_FAILED",flush=True)
if __name__=="__main__":
    main()
