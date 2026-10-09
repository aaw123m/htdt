#!/usr/bin/env python3
"""Full untruncated x⊗yz 3D eigenbasis original physical point q0 transfer.

Real original native PPW40/44 exact roof FV/Newmark data (not upstream
explicit PFFDTD qualification). All modes, no Gaussian source or taper.
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
    original_native_xy_z_factorization,prove_native_full_kronecker_equal,
    generalized_neumann_modes)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_point_quadratic_pffdtd import PIN,file_hash
from run_r130d_native_exact_roof_xy_z_separable_modal_q0 import (
    source_receiver_tensor_projections,validate_plan as check_previous_plan)
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs,verify_plan as check_roof_plan

SCHEMA="htdt.r130d.native-exact-roof-full-x-yz-eigenspectrum-finite-window-q0-plan-1"
PPW=(40,44)
FREQ=np.array([40.,80.])
BANDS=((0,100),(100,200),(200,400),(400,800),(800,1_000_000_000))
def validate_plan(p):
    if (p.get("schema_version")!=SCHEMA
        or p["pinned"]["native_PPW"]!=list(PPW)
        or p["pinned"]["upstream_sha"]!=PIN
        or p["pinned"]["source_xyz_m"]!=[1.5,2,2]
        or p["pinned"]["receiver_xyz_m"]!=[2.5,2,2]
        or p["pinned"]["frequency_bins_hz"]!=[40,80]
        or p["pinned"]["full_record_s"]!=.25
        or p["numerical"]["low_384_relative_max"]!=2e-9
        or p["numerical"]["full_exact_modal_vs_earlier_true_CG_wave_relative_max"]!=2e-5
        or p["numerical"]["yz_solver"]!="scipy.linalg.eigh full symmetric normalized mass-inverted Kyz dense; no eigenvalue truncation"
        or p["decomposition"]["semidiscrete_frequency_bands_hz"]!=[list(b) for b in BANDS]
        or p["decomposition"]["absolute_original_acceptance"]!={"complex":.2,"magnitude":.25,"phase_deg":15}
        or p["limits"]["run_github_actions"] is not False
        or p["release"]["canonical_original_q0_self_convergence"]!="SELF_CONVERGENCE_FAILED"
        or p["release"]["product"]!="NO_GO"):
        raise ValueError("prior-to-spectrum complete modal plan changed")
    return p

def stable_q_progression(m,beta):
    """sum_{n=1}^m exp(i*n*beta); stable even at beta=0."""
    beta=np.asarray(beta,dtype=float)
    return (m*np.exp(.5j*(m+1)*beta)*np.sinc(m*beta/(2*np.pi))/
            np.sinc(beta/(2*np.pi)))

def stable_sin_ratio(m,theta):
    """sin(m theta)/sin(theta), stable at exact Neumann zero mode."""
    theta=np.asarray(theta,dtype=float)
    return m*np.sinc(m*theta/np.pi)/np.sinc(theta/np.pi)

def entire_original_finite_record_signed_modes(lamb,amplitudes,dt,nt,rho):
    """Closed-form EXACT original derivative + rectangular finite q0 P_T/Q_T.

    phi[n]=amp sin(n theta)/sin(theta) where theta is native Newmark angle.
    p0=rho amp/dt (2-cosθ)
    p1..N-2=rho amp/dt cos(nθ)
    pN-1=rho amp/dt [R(N-1)+(cosθ-2)R(N-2)]
    N-sample direct frequency transform is the exact original ratio because
    original q[0]=1 so delta_time cancels.
    """
    lam=np.asarray(lamb,dtype=float).ravel()
    amp=np.asarray(amplitudes,dtype=float).ravel()
    if (lam.shape!=amp.shape or nt<3 or nt>2000 or dt<=0
        or np.min(lam)<-5e-7 or not np.all(np.isfinite(amp))):
        raise ValueError("invalid mass Neumann eigenvalues or original point q0")
    theta=2*np.arctan(.5*dt*np.sqrt(np.maximum(lam,0)))
    cos=np.cos(theta)
    end=stable_sin_ratio(nt-1,theta)+(cos-2)*stable_sin_ratio(nt-2,theta)
    result=np.empty((2,len(theta)),dtype=np.complex128)
    for i,f in enumerate(FREQ):
        omega_dt=2*np.pi*f*dt
        plus=stable_q_progression(nt-2,omega_dt+theta)
        minus=stable_q_progression(nt-2,omega_dt-theta)
        terms=(2-cos)+.5*(plus+minus)+np.exp(1j*omega_dt*(nt-1))*end
        result[i,:]=(rho*amp/dt)*terms
    if not np.all(np.isfinite(result)):
        raise ValueError("original full modal finite window signed P_T/Q_T nonfinite")
    return result

def one_case(p,ppw,sim,original,original_full,previous_partial):
    t0=time.perf_counter()
    if (file_hash(sim/"comms_out.h5")!=original["original_native_comm_sha256"]
        or file_hash(sim/"vox_out.h5")!=original["original_solver_geometry_sha256"]):
        raise ValueError("full-spectrum original native source/room SHA changed")
    with h5py.File(sim/"vox_out.h5","r") as h:
        axes=[np.asarray(h[t][...],dtype=float) for t in ("xv","yv","zv")]
    with h5py.File(sim/"comms_out.h5","r") as h:
        si=np.asarray(h["in_ixyz"][:],dtype=np.int64)
        sig=np.asarray(h["in_sigs"][:],dtype=float)
        ri=np.asarray(h["out_ixyz"][:],dtype=np.int64)
        rw=np.asarray(h["out_alpha"][:],dtype=float).ravel()
        nt=int(h["Nt"][()])
        if (si.shape!=(8,) or ri.shape!=(8,) or rw.shape!=(8,)
            or sig.shape!=(8,nt) or nt>2000 or np.any(sig[:,1:]!=0)):
            raise ValueError("full eig original 8node q0 source/receiver time modified")
        sw=sig[:,0]/sum(sig[:,0])
    with h5py.File(sim/"sim_consts.h5","r") as h:
        dt=float(h["Ts"][()]);h_m=float(h["h"][()]);c=float(h["c"][()])
    if (abs(dt-original_full["original_native_dt_s"])>1e-12
        or abs(h_m-original_full["native_original_h_m"])>1e-12
        or abs(c-p["pinned"]["c"])>1e-12
        or nt!=original_full["original_record_samples"]):
        raise ValueError("original full-wave native physical sample setup changed")
    full=build_native_exact_roof_fv(axes,max_nodes=150000)
    sep=original_native_xy_z_factorization(axes)
    identity=prove_native_full_kronecker_equal(full,sep)
    src_receiver=source_receiver_tensor_projections(
        axes,sep,(si,sw),(ri,rw))
    if (len(sep.yz_mass)>p["limits"]["max_xy_cross_section_nodes"]
        or sep.total_cells>p["limits"]["max_total_native_mode_count"]):
        raise ValueError("full 3D eigenbasis resource ceiling exceeded")
    print("FULL_EIGENSPECTRUM_YZ_DENSE_BEGIN",ppw,"cross_section_nodes",len(sep.yz_mass),
          "total_original_room_3D_modes",sep.total_cells,flush=True)
    yl_start=time.perf_counter()
    xlam,xvec,xdiag=generalized_neumann_modes(sep.Mx,sep.Kx)
    ylam,yvec,ydiag=generalized_neumann_modes(sep.Myz,sep.Kyz)
    print("FULL_EIGENSPECTRUM_YZ_DENSE_COMPLETE",ppw,"seconds",time.perf_counter()-yl_start,
          "max mass-orthogonality error",ydiag["maximum_M_orthonormality_error"],
          "max true residual",ydiag["highest_true_mass_normalized_eigen_residual"],flush=True)
    for check in (xdiag,ydiag):
        if (check["maximum_M_orthonormality_error"]>p["numerical"]["true_mass_orthonormality_max"]
            or check["highest_true_mass_normalized_eigen_residual"]>
                p["numerical"]["true_eigen_relative_residual_max"]
            or check["first_zero_neumann_eigen_frequency_hz"]>
                p["numerical"]["zero_neumann_frequency_hz_max"]):
            raise ValueError("full original Neumann generalized eigen decomposition failed")
    sx=xvec.T@src_receiver["source"]["x"]
    rx=xvec.T@src_receiver["receiver"]["x"]
    sy=yvec.T@src_receiver["source"]["yz"]
    ry=yvec.T@src_receiver["receiver"]["yz"]
    coupling=np.outer(sx*rx,sy*ry)
    lam=(xlam[:,None]+ylam[None,:])
    amp=(dt**2*c**2*coupling/(1+dt**2*lam/4))
    signed=entire_original_finite_record_signed_modes(
        lam.ravel(),amp.ravel(),dt,nt,p["pinned"]["rho"])
    oldpartial=unpairs(previous_partial["finite_record_partial_time_transfer_integrity"]["original_250ms_modal_partial_signed_transfer_40_80"])
    slow=signed.reshape(2,len(xlam),len(ylam))[:,:12,:32].sum(axis=(1,2))
    low_err=float(np.linalg.norm(slow-oldpartial)/max(np.linalg.norm(oldpartial),1e-15))
    if low_err>p["numerical"]["low_384_relative_max"]:
        raise ValueError(f"analytical finite rectangular pressure DTFT does not reproduce independently integrated 384 modes, rel={low_err}")
    summed=signed.sum(axis=1)
    oldfull=unpairs(original_full["experimental_signed_P_T_over_Q_T_40_80"])
    error=float(np.linalg.norm(summed-oldfull)/max(np.linalg.norm(oldfull),1e-15))
    if error>p["numerical"]["full_exact_modal_vs_earlier_true_CG_wave_relative_max"]:
        raise ValueError(f"FULL EIGENBASIS failed unchanged true native 250ms Newmark-CG impulse full waveform, error={error}")
    freqs=np.sqrt(np.maximum(lam.ravel(),0))/(2*np.pi)
    groups=[]
    for low,high in BANDS:
        mask=(freqs>=low)&(freqs<high)
        groups.append({
            "frequency_band_hz":[low,high],
            "included_native_3D_mode_count":int(mask.sum()),
            "signed_full_q0_pressure_transfer_40_80":pairs(signed[:,mask].sum(axis=1))})
    if sum(q["included_native_3D_mode_count"] for q in groups)!=sep.total_cells:
        raise ValueError("complete original 3D modal spectrum has unassigned modes")
    tops={}
    for i,f in enumerate(FREQ):
        ranks=np.argsort(np.abs(signed[i]))[-24:][::-1]
        tops[str(int(f))]=[
            {"x_mode_index":int(loc//len(ylam)),
             "yz_mode_index":int(loc%len(ylam)),
             "native_3D_f_hz":float(freqs[int(loc)]),
             "original_signed_q0_250ms_contribution":pairs(
                 np.array([signed[0,int(loc)],signed[1,int(loc)]])),
             "bin_absolute_signed_contribution":float(abs(signed[i,int(loc)]))}
            for loc in ranks]
    return {
        "ppw":ppw,"original_native_voxel_sha256":file_hash(sim/"vox_out.h5"),
        "original_native_comms_sha256":file_hash(sim/"comms_out.h5"),
        "original_record_nt":nt,"original_native_dt_s":dt,
        "source_receiver_fixed_exact_xyz_and_q0":True,
        "original_neumann_Kronecker_identity":identity,
        "total_full_untruncated_3D_native_modes":sep.total_cells,
        "full_x_eigenmode_count":len(xlam),
        "full_yz_eigenmode_count":len(ylam),
        "true_full_1D_x_mode_numerics":xdiag,
        "true_full_2D_yz_mode_numerics":ydiag,
        "first_32_yz_native_eigenfrequencies_hz":list(map(float,np.sqrt(ylam[:32])/(2*np.pi))),
        "first_16_x_native_eigenfrequencies_hz":list(map(float,np.sqrt(xlam[:16])/(2*np.pi))),
        "max_yz_native_eigenfrequency_hz":float(np.sqrt(ylam[-1])/(2*np.pi)),
        "max_full_3D_native_eigenfrequency_hz":float(max(freqs)),
        "archived_first_384_mode_signed_250ms_direct_time":pairs(oldpartial),
        "analytic_first_384_modal_signed_250ms_transfer":pairs(slow),
        "analytic_384_vs_previous_actual_temporal_integration_rel":low_err,
        "all_original_Neumann_3D_modes_full_signed_P_T_over_Q_T":pairs(summed),
        "archived_direct_full_wave_250ms_signed_40_80":pairs(oldfull),
        "all_mode_analytic_vs_archived_true_CG_full_wave_rel":error,
        "every_original_mode_in_one_signed_frequency_band":groups,
        "top24_individual_signed_250ms_wave_modes_each_original_bin":tops,
        "full_3D_neumann_0_1_2_frequency_band_no_mask":True,
        "original_full_waveform_temporal_q0_without_taper":True,
        "experiment_not_the_canonical_PFFDTD_source_solver":True,
        "elapsed_seconds":float(time.perf_counter()-t0)}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args()
    raw=args.plan.read_bytes()
    p=validate_plan(json.loads(raw.decode("utf-8")))
    prior=json.loads((ROOT/p["pinned"]["original_PFFDTD_HDF5_source"]).read_text(encoding="utf-8"))
    originals={a["ppw"]:a for a in prior["actual_native_wave_cases"] if a["ppw"] in PPW}
    roof=json.loads((ROOT/p["pinned"]["unchanged_exact_roof_wave"]).read_text(encoding="utf-8"))
    roof_plan=check_roof_plan(json.loads((ROOT/"benchmarks/acoustics/r130d_native_grid_exact_roof_mass_fv_q0_plan_2026-10-09.json").read_text(encoding="utf-8")))
    if roof["preregistered_plan"]!=roof_plan or roof["product"]!="NO_GO":
        raise ValueError("original actual 8/8 exact roof q0 wave evidence tampered")
    wave={a["ppw"]:a for a in roof["actual_native_grid_point_impulse_exact_roof_cases"] if a["ppw"] in PPW}
    part=json.loads((ROOT/p["pinned"]["precomputed_exact_separability"]).read_text(encoding="utf-8"))
    prevplan=check_previous_plan(json.loads((ROOT/p["pinned"]["previous_spectrum_plan"]).read_text(encoding="utf-8")))
    if (part["preregistered_plan"]!=prevplan
        or part["canonical_original_q0"]!="SELF_CONVERGENCE_FAILED"
        or part["product"]!="NO_GO"):
        raise ValueError("precommit 384 original native exact-roof modes not safely retained")
    partial={a["ppw"]:a for a in part["actual_original_3D_separable_native_cases"] if a["ppw"] in PPW}
    if set(originals)!=set(PPW) or set(wave)!=set(PPW) or set(partial)!=set(PPW):
        raise ValueError("complete original input and first-384 modal controls missing")
    dirs={}
    for f in args.original_sims_root.rglob("comms_out.h5"):
        sha=file_hash(f)
        candidates=[ppw for ppw in PPW if sha==originals[ppw]["original_native_comm_sha256"]]
        if len(candidates)==1:
            if candidates[0] in dirs:raise ValueError("duplicate original q0 source HDF5")
            dirs[candidates[0]]=f.parent
    if set(dirs)!=set(PPW):
        raise ValueError("original external native PPW40 and44 q0 SHA inputs missing")
    output={
        "schema_version":"htdt.r130d.native-exact-roof-full-xy-z-modal-finite-window-q0-evidence-1",
        "plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":p,
        "canonical_original_selfconvergence":"SELF_CONVERGENCE_FAILED",
        "physical_validation":"NOT_VALIDATED","product":"NO_GO",
        "new_github_actions_runs":0,
        "actual_untruncated_3D_native_eigenmode_cases":[]}
    for ppw in PPW:
        try:z=one_case(p,ppw,dirs[ppw],originals[ppw],wave[ppw],partial[ppw])
        except Exception as exc:
            output["actual_untruncated_3D_native_eigenmode_cases"].append({
                "ppw":ppw,"status":"FULL_ORIGINAL_MODAL_RECONSTRUCTION_FAILED",
                "failure_type":type(exc).__name__,"failure_detail":str(exc)})
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        output["actual_untruncated_3D_native_eigenmode_cases"].append(z)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("FULL_ORIGINAL_POINT_Q0_3D_ALL_EIGENMODES",ppw,
              "modes",z["total_full_untruncated_3D_native_modes"],
              "relative reconstruction",z["all_mode_analytic_vs_archived_true_CG_full_wave_rel"],
              "time_s",z["elapsed_seconds"],flush=True)
    first,second=output["actual_untruncated_3D_native_eigenmode_cases"]
    f0=unpairs(first["all_original_Neumann_3D_modes_full_signed_P_T_over_Q_T"])
    f1=unpairs(second["all_original_Neumann_3D_modes_full_signed_P_T_over_Q_T"])
    delta=f0-f1
    full_norm=float(np.linalg.norm(delta))
    bybands=[]
    signed_sum=np.zeros(2,dtype=complex)
    for r,s in zip(first["every_original_mode_in_one_signed_frequency_band"],
                   second["every_original_mode_in_one_signed_frequency_band"]):
        if r["frequency_band_hz"]!=s["frequency_band_hz"]:
            raise ValueError("original five high mode frequency band definitions drifted")
        band=unpairs(r["signed_full_q0_pressure_transfer_40_80"])-unpairs(
            s["signed_full_q0_pressure_transfer_40_80"])
        signed_sum+=band
        bybands.append({
            "physical_semidiscrete_band_hz":r["frequency_band_hz"],
            "PPW40_mode_count":r["included_native_3D_mode_count"],
            "PPW44_mode_count":s["included_native_3D_mode_count"],
            "signed_adjacent_PP40_minus_PP44_complex_40_80":pairs(band),
            "two_bin_difference_norm_relative_to_complete_original":float(
                 np.linalg.norm(band)/max(full_norm,1e-12)),
            "complex_cancellation_means_not_additive_fraction":True})
    if not np.allclose(signed_sum,delta,rtol=2e-10,atol=3e-6):
        raise ValueError("exact original point q0 full high-mode band signed conservation broke")
    outcome=compare_complex_transfer(
        reference=second["all_original_Neumann_3D_modes_full_signed_P_T_over_Q_T"],
        candidate=first["all_original_Neumann_3D_modes_full_signed_P_T_over_Q_T"],
        frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
    if abs(outcome["complex_rms_relative"]-0.8726662923738394)>2e-5:
        raise ValueError("original PPW40/44 canonical failed experimental exact roof q0 transfer changed")
    output["all_original_3D_modes_signed_PP40_minus_PP44_adjacent_fourier_difference_by_band"]=bybands
    output["all_mode_reconstructed_original_PP40_44_exact_roof_full_8_8_complex_metric"]=outcome
    output["exact_full_3D_signed_delta_from_band_sum"]=pairs(signed_sum)
    output["higher_modes_not_truncated_from_final_250ms_q0_result"]=True
    output["original_full_canonical_q0_not_requalified"]=True
    args.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("COMPLETE_ORIGINAL_POINT_Q0_FULL_3D_EIGENBASIS_SIGNED_CAUSAL_ERROR",
          [(a["physical_semidiscrete_band_hz"],a["two_bin_difference_norm_relative_to_complete_original"])
           for a in bybands],
          "original complex self-refinement",outcome["complex_rms_relative"],
          "product NO_GO",flush=True)
if __name__=="__main__":
    main()
