#!/usr/bin/env python3
"""True original q0 point 40/80Hz 250ms low-mode source/receiver y-z separation.

Checks exact numerical matrix identity M3=Mx⊗Myz,
K3=Kx⊗Myz+Mx⊗Kyz on actual frozen original PPW40/44 native grids
BEFORE interpreting any modal spectrum.
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
from htdt.r130d_native_grid_exact_roof_fv import build_native_exact_roof_fv
from htdt.r130d_native_exact_roof_separable import (
    original_native_xy_z_factorization, prove_native_full_kronecker_equal,
    generalized_neumann_modes)
from htdt.acoustic_pffdtd_adapter import (
    pffdtd_velocity_potential_to_pressure_trace, finite_record_pressure_transfer)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_native_exact_roof_one_sided_Q2_and_x_mode import exact_original_eight_point
from run_r130d_original_point_quadratic_pffdtd import file_hash,PIN
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs,verify_plan as verify_roof_plan

SCHEMA="htdt.r130d.native-exact-roof-x-yz-separable-modal-q0-plan-1"
PPW=(40,44)
FREQ=np.array([40.,80.])
def validate_plan(p):
    if (p.get("schema_version")!=SCHEMA
        or p["frozen_source"]["native_PPW"]!=list(PPW)
        or p["frozen_source"]["pffdtd_upstream_pin"]!=PIN
        or p["frozen_source"]["source_xyz_m"]!=[1.5,2,2]
        or p["frozen_source"]["receiver_xyz_m"]!=[2.5,2,2]
        or p["frozen_source"]["frequencies_hz"]!=[40,80]
        or p["frozen_source"]["full_record_s"]!=.25
        or p["exact_math"]["max_mass_abs"]!=1e-10
        or p["exact_math"]["max_stiffness_abs"]!=2e-8
        or p["modes"]["yz_modes"]!=32
        or p["modes"]["lowest_x_modes_contributing"]!=12
        or p["evaluation"]["original_complex_limit"]!=.2
        or p["evaluation"]["original_magnitude_limit"]!=.25
        or p["evaluation"]["original_phase_limit_deg"]!=15
        or p["limits"]["avoid_github_actions"] is not True
        or p["release"]["canonical_original_point_q0"]!="SELF_CONVERGENCE_FAILED"
        or p["release"]["product"]!="NO_GO"):
        raise ValueError("prospective exact-roof separable modes q0 plan drift")
    return p

def source_receiver_tensor_projections(axes,sep,original_source,original_receiver):
    """Enforce SOURCE and RECEIVER original 8-node full 3D q0 tensors exactly."""
    ixlookup={int(i):j for j,i in enumerate(sep.x_original_indices)}
    yzlookup={int(i):j for j,i in enumerate(sep.yz_original_flat_indices)}
    result={}
    for label,(native_indices,native_weights,physical) in {
        "source":(*original_source,[1.5,2.,2.]),
        "receiver":(*original_receiver,[2.5,2.,2.])}.items():
        indices,weights,meta=exact_original_eight_point(
            axes,physical,native_indices,native_weights)
        big=np.zeros((len(ixlookup),len(yzlookup)))
        for flat,weight in zip(indices,weights):
            i,j,k=np.unravel_index(int(flat),sep.full_native_dimensions)
            yz=int(j*sep.z_size+k)
            if int(i) not in ixlookup or yz not in yzlookup:
                raise ValueError("original 8point input/output outside native exact room")
            big[ixlookup[int(i)],yzlookup[yz]]+=weight
        wx=big.sum(axis=1)
        wyz=big.sum(axis=0)
        err=float(np.max(np.abs(big-np.outer(wx,wyz))))
        if err>1e-12 or abs(sum(wx)-1)>1e-12 or abs(sum(wyz)-1)>1e-12:
            raise ValueError("original physical point 3D trilinear stencil does not separably factor")
        result[label]={"x":wx,"yz":wyz,
                      "tensor_separability_residual":err,
                      "original_native_xyz_m":physical,
                      "stencil_node_count":len(indices),
                      "reconstructed_original_8_point":meta}
    return result

def full_modal_signed_q0_transfer(xlam,ylam,xvec,yvec,src,recv,dt,nt,c,rho):
    """Exact undamped midpoint Newmark impulse for each tensor eigenmode.

    q[0]=1, u0=0, u1=dt² c² (1+dt²λ/4)⁻¹ source_proj v.
    All subsequent unforced u[n] from sin(nθ)/sinθ, θ=2 atan(dt sqrtλ/2).
    Pressure original centered interior/one-sided endpoints (linearity).
    """
    n_x=12;n_yz=32
    if len(xlam)<n_x or len(ylam)<n_yz or nt>2000:
        raise ValueError("frozen x/yz mode or time resource limit exceeded")
    xs=xvec.T@src["x"];xr=xvec.T@recv["x"]
    ys=yvec.T@src["yz"];yr=yvec.T@recv["yz"]
    coupling=(xs[:n_x]*xr[:n_x])[:,None]*(ys[:n_yz]*yr[:n_yz])[None,:]
    lamb=(xlam[:n_x,None]+ylam[None,:n_yz]).ravel()
    theta=2*np.arctan(.5*dt*np.sqrt(np.maximum(lamb,0)))
    amp=dt**2*c**2*coupling.ravel()/(1+(dt**2/4)*lamb)
    sample=np.arange(nt,dtype=float)
    p=np.empty((len(theta),nt),dtype=float)
    tiny=abs(theta)<1e-8
    factors=np.empty((len(theta),nt),dtype=float)
    factors[tiny]=sample[None,:]
    factors[~tiny]=np.sin(theta[~tiny,None]*sample[None,:])/np.sin(theta[~tiny,None])
    phi=amp[:,None]*factors
    p[:,0]=rho*(-3*phi[:,0]+4*phi[:,1]-phi[:,2])/(2*dt)
    p[:,1:-1]=rho*(phi[:,2:]-phi[:,:-2])/(2*dt)
    p[:,-1]=rho*(3*phi[:,-1]-4*phi[:,-2]+phi[:,-3])/(2*dt)
    transform=np.exp(2j*np.pi*FREQ[:,None]*sample[None,:]*dt)
    signed=transform@p.T
    direct_phi=phi.sum(axis=0)
    direct_pressure=pffdtd_velocity_potential_to_pressure_trace(
        direct_phi,time_step_s=dt,density_kg_m3=rho)
    q=np.zeros(nt,dtype=float);q[0]=1.
    independent=finite_record_pressure_transfer(
        direct_pressure,q,time_step_s=dt,frequency_hz=FREQ)
    if not np.allclose(signed.sum(axis=1),independent,rtol=1e-10,atol=1e-7):
        raise ValueError("individual signed source q0 modal P_T/Q_T do not reconstruct finite record")
    rec=[]
    for i in range(n_x):
        for j in range(n_yz):
            loc=i*n_yz+j
            rec.append({
                "x_mode":i,"yz_mode":j,
                "semidiscrete_eigenfrequency_hz":float(np.sqrt(max(lamb[loc],0))/(2*np.pi)),
                "actual_newmark_discrete_eigenfrequency_hz":float(theta[loc]/(2*np.pi*dt)),
                "original_signed_src_receiver_mass_normalized_coupling":float(coupling[i,j]),
                "signed_original_q0_P_T_over_Q_T_40_80":pairs(signed[:,loc])})
    return signed,rec,{
        "original_250ms_modal_partial_signed_transfer_40_80":pairs(signed.sum(axis=1)),
        "original_250ms_modal_partial_direct_time_domain_conservation":True,
        "partial_x_first_12_modes_and_yz_first_32_modes_only":True,
        "modal_count":int(len(rec))}

def one_case(p,ppw,sim,original,prior):
    start=time.perf_counter()
    for h5,sha_key in [("comms_out.h5","original_native_comm_sha256"),
                       ("vox_out.h5","original_solver_geometry_sha256")]:
        if file_hash(sim/h5)!=original[sha_key]:
            raise ValueError("frozen original point q0 input or voxel source SHA changed")
    with h5py.File(sim/"vox_out.h5","r") as h:
        axes=[np.asarray(h[n][...],dtype=float) for n in ("xv","yv","zv")]
    with h5py.File(sim/"comms_out.h5","r") as h:
        source_ix=np.asarray(h["in_ixyz"][:],dtype=np.int64)
        q=np.asarray(h["in_sigs"][:],dtype=float)
        receiver_ix=np.asarray(h["out_ixyz"][:],dtype=np.int64)
        receiver_w=np.asarray(h["out_alpha"][:],dtype=float).ravel()
        nt=int(h["Nt"][()])
        if (source_ix.shape!=(8,) or receiver_ix.shape!=(8,)
            or q.shape!=(8,nt) or receiver_w.shape!=(8,)
            or np.any(q[:,1:]!=0) or int(h["diff"][()])!=0):
            raise ValueError("original full native point q0 source or receiver changed")
        source_weights=q[:,0]/q[:,0].sum()
    with h5py.File(sim/"sim_consts.h5","r") as h:
        dt=float(h["Ts"][()]);h_m=float(h["h"][()]);c=float(h["c"][()])
    if (nt>p["limits"]["max_record_steps"] or
        abs(dt-prior["original_native_dt_s"])>1e-12 or
        abs(h_m-prior["native_original_h_m"])>1e-12 or
        abs(c-343.2)>1e-12):
        raise ValueError("original native original time/room clock drifted")
    from htdt.r130d_native_grid_exact_roof_fv import build_native_exact_roof_fv
    volume=build_native_exact_roof_fv(
        axes,max_nodes=p["limits"]["max_volume_nodes"])
    sep=original_native_xy_z_factorization(axes)
    identity=prove_native_full_kronecker_equal(
        volume,sep,max_mass_abs=p["exact_math"]["max_mass_abs"],
        max_stiffness_abs=p["exact_math"]["max_stiffness_abs"])
    if (sep.cross_section_cells>p["limits"]["max_yz_nodes"]
        or len(sep.x_mass)>p["limits"]["max_x_nodes"]
        or len(sep.x_mass)*len(sep.yz_mass)>p["limits"]["max_volume_nodes"]):
        raise ValueError("native original separable operator resource exceeded")
    src_recv=source_receiver_tensor_projections(
        axes,sep,(source_ix,source_weights),(receiver_ix,receiver_w))
    xlam,xvec,xcheck=generalized_neumann_modes(sep.Mx,sep.Kx)
    ylam,yvec,ycheck=generalized_neumann_modes(
        sep.Myz,sep.Kyz,nmodes=p["modes"]["yz_modes"])
    for diag in (xcheck,ycheck):
        if (diag["highest_true_mass_normalized_eigen_residual"]>
            p["modes"]["accuracy_true_eigen_residual_relative_max"]
            or diag["first_zero_neumann_eigen_frequency_hz"]>
            p["modes"]["zero_mode_f_hz_max"]):
            raise ValueError("original 3D separable Neumann eigenpair error not within frozen threshold")
    signed,modes,partial=full_modal_signed_q0_transfer(
        xlam,ylam,xvec,yvec,src_recv["source"],src_recv["receiver"],
        dt,nt,c,p["frozen_source"]["rho"])
    full=unpairs(prior["experimental_signed_P_T_over_Q_T_40_80"])
    residual=full-signed.sum(axis=1)
    relative=float(np.linalg.norm(residual)/max(np.linalg.norm(full),1e-15))
    top={}
    for bin_idx,hz in enumerate(FREQ):
        ranked=np.argsort(abs(signed[bin_idx,:]))[::-1][:10]
        top[str(int(hz))]=[
            {"mode_x":int(int(loc)//32),"mode_yz":int(int(loc)%32),
             "semidiscrete_frequency_hz":modes[int(loc)]["semidiscrete_eigenfrequency_hz"],
             "signed_transfer_real":float(signed[bin_idx,int(loc)].real),
             "signed_transfer_imag":float(signed[bin_idx,int(loc)].imag),
             "absolute_individual_transfer":float(abs(signed[bin_idx,int(loc)])),
             "signed_amplitude_cannot_be_additively_attributed":True}
            for loc in ranked]
    yz_families={
        str(iy):pairs(signed[:,np.arange(12)*32+iy].sum(axis=1))
        for iy in range(32)}
    x_families={
        str(ix):pairs(signed[:,ix*32:(ix+1)*32].sum(axis=1))
        for ix in range(12)}
    nearest_modes={
        str(int(f)):[
            {"x_mode":modes[int(i)]["x_mode"],"yz_mode":modes[int(i)]["yz_mode"],
             "frequency_hz":modes[int(i)]["semidiscrete_eigenfrequency_hz"],
             "absolute_modal_delta_from_target_hz":float(abs(
                 modes[int(i)]["semidiscrete_eigenfrequency_hz"]-f)),
             "source_receiver_coupling":modes[int(i)]["original_signed_src_receiver_mass_normalized_coupling"]}
            for i in sorted(range(len(modes)),
                            key=lambda i:abs(modes[i]["semidiscrete_eigenfrequency_hz"]-f))[:8]]
        for f in FREQ}
    result={
        "ppw":ppw,
        "pinned_original_comms_sha256":file_hash(sim/"comms_out.h5"),
        "pinned_original_voxels_sha256":file_hash(sim/"vox_out.h5"),
        "pinned_original_full_wave_sha256":prior["original_native_raw_wave_sha256"],
        "full_original_native_sample_count":nt,
        "native_dt_s":dt,"native_h_m":h_m,
        "actual_exact_original_3D_Kronecker_matrix_identity":identity,
        "original_source_receiver_8node_tensor_factorization":{
            "source":{k:v for k,v in src_recv["source"].items() if not isinstance(v,np.ndarray)},
            "receiver":{k:v for k,v in src_recv["receiver"].items() if not isinstance(v,np.ndarray)}},
        "x_full_original_generalized_Neumann_modes":{
            "modes_count":len(xlam),
            "all_mode_eigenfrequency_hz":list(map(float,np.sqrt(xlam)/(2*np.pi))),
            "all_mode_signed_x_source_receiver_coupling":list(map(float,
                (xvec.T@src_recv["source"]["x"])*(xvec.T@src_recv["receiver"]["x"]))),
            "numerical_accuracy":xcheck},
        "yz_native_roof_generalized_Neumann_modes":{
            "modes_count":len(ylam),
            "all_mode_eigenfrequency_hz":list(map(float,np.sqrt(ylam)/(2*np.pi))),
            "all_mode_signed_yz_source_receiver_coupling":list(map(float,
                (yvec.T@src_recv["source"]["yz"])*(yvec.T@src_recv["receiver"]["yz"]))),
            "numerical_accuracy":ycheck},
        "actual_original_q0_first_384_product_modes":modes,
        "finite_record_partial_time_transfer_integrity":partial,
        "original_8_8_full_3D_signed_40_80":pairs(full),
        "remaining_true_3D_high_mode_signed_complex":pairs(residual),
        "remaining_high_mode_relative_to_full_40_80_L2":relative,
        "top_10_individual_signed_modal_amplitudes_by_bin":top,
        "first_12_x_mode_signed_sums_over_low_32_yz_modes":x_families,
        "first_32_yz_mode_signed_sums_over_low_12_x_modes":yz_families,
        "eight_nearest_3D_product_modes_to_original_two_bins":nearest_modes,
        "no_original_smoothing_or_changed_q0_or_boundary":True,
        "not_entire_3D_eigenspectrum_or_original_PFFDTD_qualification":True,
        "wall_seconds":float(time.perf_counter()-start)}
    print("EXACT_ROOF_KRONECKER_3D_Q0",ppw,"nodes",sep.total_cells,
          "yz",len(ylam),"K_max_err",identity["3D_original_full_stiffness_Kronecker_max_absolute"],
          "yz first modes",[round(float(np.sqrt(v)/(2*np.pi)),5) for v in ylam[1:5]],
          "partial_vs_full_rel",relative,
          "signed_partial",partial["original_250ms_modal_partial_signed_transfer_40_80"],
          "true_full",pairs(full),flush=True)
    return result

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args()
    raw=args.plan.read_bytes()
    p=validate_plan(json.loads(raw.decode("utf-8")))
    prior=json.loads((ROOT/p["frozen_source"]["original_native_comms_evidence"]).read_text(encoding="utf-8"))
    original={i["ppw"]:i for i in prior["actual_native_wave_cases"] if i["ppw"] in PPW}
    baseline=json.loads((ROOT/p["frozen_source"]["unmodified_exact_roof_8_8_full_wave_evidence"]).read_text(encoding="utf-8"))
    roofplan=verify_roof_plan(json.loads((ROOT/"benchmarks/acoustics/r130d_native_grid_exact_roof_mass_fv_q0_plan_2026-10-09.json").read_text(encoding="utf-8")))
    if baseline["preregistered_plan"]!=roofplan or baseline["product"]!="NO_GO":
        raise ValueError("original exact roof 8/8 frozen source control no longer matches plan")
    old={i["ppw"]:i for i in baseline["actual_native_grid_point_impulse_exact_roof_cases"] if i["ppw"] in PPW}
    if set(original)!=set(PPW) or set(old)!=set(PPW):raise ValueError("both original PPW40/44 controls unavailable")
    runs={}
    for file in args.original_sims_root.rglob("comms_out.h5"):
        sha=file_hash(file)
        found=[n for n in PPW if original[n]["original_native_comm_sha256"]==sha]
        if len(found)==1:
            if found[0] in runs:raise ValueError("duplicate original 8node source HDF5")
            runs[found[0]]=file.parent
    if set(runs)!=set(PPW):raise ValueError("frozen PPW40 and44 exact original native HDF5 not present")
    output={"schema_version":"htdt.r130d.native-exact-roof-x-yz-separable-modal-q0-evidence-1",
        "plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":p,"pinned_native_original_PFFDTD":PIN,
        "canonical_original_q0":"SELF_CONVERGENCE_FAILED",
        "physical_validation":"NOT_VALIDATED","product":"NO_GO",
        "new_GitHub_Actions_runs":0,"actual_original_3D_separable_native_cases":[]}
    for ppw in PPW:
        try:result=one_case(p,ppw,runs[ppw],original[ppw],old[ppw])
        except Exception as exc:
            output["actual_original_3D_separable_native_cases"].append({
                "ppw":ppw,"status":"FAILED_NUMERICAL_MODAL_DIAGNOSTIC",
                "exception_type":type(exc).__name__,"exception_detail":str(exc)})
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        output["actual_original_3D_separable_native_cases"].append(result)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    a,b=output["actual_original_3D_separable_native_cases"]
    errors=compare_complex_transfer(
        reference=b["original_8_8_full_3D_signed_40_80"],
        candidate=a["original_8_8_full_3D_signed_40_80"],
        frequency_hz=FREQ,magnitude_mask_relative_db=-50).model_dump(mode="json")
    output["original_frozen_exact_roof_8_8_ppw40_44_adjacent_signed_comparison"]=errors
    if abs(errors["complex_rms_relative"]-0.8726662923738394)>1e-10:
        raise ValueError("original previously archived unfavorable native 8/8 comparison changed")
    output["yz_first32_modes_frequency_drift_ppw40_to_44_hz"]=[
        float(y-x) for x,y in zip(
            a["yz_native_roof_generalized_Neumann_modes"]["all_mode_eigenfrequency_hz"],
            b["yz_native_roof_generalized_Neumann_modes"]["all_mode_eigenfrequency_hz"])]
    output["original_full_3D_point_q0_unchanged_and_still_FAILED"]=True
    output["partial_384_modes_cannot_support_qualification"]=True
    args.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("ORIGINAL_EXACT_ROOF_X_YZ_3D_MODE_DECOMPOSITION_COMPLETE",
          "yz first mode drift",output["yz_first32_modes_frequency_drift_ppw40_to_44_hz"][:8],
          "old full original fails",errors["complex_rms_relative"],flush=True)

if __name__=="__main__":
    main()
