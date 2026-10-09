#!/usr/bin/env python3
"""Original SHA HDF5 8node q0, Cartesian interior flux + exact true roof.

All original PPW28,32,36,40,44 native 250ms source strengths and time clocks.
Full physical consistent Q1 mass, weak true sloped natural Neumann stiffness,
and original 8node trilinear point source/receiver, all generalized modes.
Never silently promote an experimental Q1 solver to original PFFDTD PASS.
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
from htdt.r130d_conforming_roof_p1_fem import build_original_native_conforming_roof_p1
from htdt.r130d_conforming_roof_p1_consistent_mass import (
    consistent_physical_P1_tensored_operators,
    generalized_symmetric_P1_consistent_modes,
    true_full_3d_consistent_P1_MK)
from htdt.r130d_native_cut_roof_Q1_galerkin import (
    cut_roof_native_original_Q1_galerkin_yz,
    original_eightnode_native_HDF5_Q1_source_receiver,
    generalized_native_original_Q1_full_physical_neumann_modes)
from htdt.r130d_cartesian_true_roof_hybrid_flux import build_native_cartesian_true_roof_hybrid_flux
from htdt.r130d_causal_first_roof_echo import (ROOF_CENTER_S, ROOF_RADIUS_S, ROOF_WIDTHS_S, analytic_native_64point_physical_roof_echo_weak)
from htdt.r130d_causal_prefirst_weak import compact_odd_witness
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_native_exact_roof_full_xy_z_modal_q0 import entire_original_finite_record_signed_modes
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs
from run_r130d_original_point_quadratic_pffdtd import PIN,PPW,file_hash

SCHEMA="htdt.r130d.original-q0-cartesian-interior-fivepoint-true-cut-Q1-boundary-flux-plan-1"
ARMS=("preobserved_conforming_P1_consistent_mass_original_8node",
      "new_native_cartesian_interior_fivepoint_true_roof_hybrid_original_8node")

def validate_plan(p):
    o=p["original"];n=p["new_operator"];i=p["independent"];caps=p["caps"];rel=p["release"]
    if (p["schema_version"]!=SCHEMA or p["issue"]!=938 or p["pr"]!=1055
        or o["upstream_PFFDTD_sha"]!=PIN or o["ppw"]!=list(PPW)
        or o["original_source_xyz"]!=[1.5,2,2] or o["original_receiver_xyz"]!=[2.5,2,2]
        or not o["original_SHA_native_eight_source_receiver"]
        or o["c_m_s"]!=343.2 or o["rho_kg_m3"]!=1.2
        or o["record_s"]!=.25 or o["frequency_signed_hz"]!=[40,80]
        or not o["original_native_dt_h_Nt_per_grid"]
        or not o["original_mass_and_pressure_observer_unchanged"]
        or o["acceptance"]!={"complex_rms_relative_max":.2,"magnitude_max_relative":.25,"phase_max_deg":15}
        or not o["require_all_four_adjacent_pairs"]
        or not o["require_strict_monotonicity_all_three_metrics"]
        or "c^2/3" not in n["yz_stiffness"] or "FULL wet" not in n["yz_stiffness"]
        or not n["no_high_mode_cut_sliver_drop_source_smoothing_damping_or_time_window"]
        or not all(v is True for k,v in i.items() if isinstance(v,bool))
        or caps!={"max_active_yz_nodes":3300,"max_3d_modes":180000,"only_ppw":5,
                   "run_original_PFFDTD":0,"new_github_actions_runs":0,"preserve_scratch":True}
        or rel!={"original_PFFDTD":"SELF_CONVERGENCE_FAILED",
                 "independent_physics":"NOT_VALIDATED","product":"NO_GO",
                 "draft_pr":True,"issue_open":True}):
        raise ValueError("hybrid true roof original q0 preregistered plan changed")
    return p

def fixed_first_roof_weak_full_modes(lam,kick,dt,nt,axes,native_dims,src,sw,rec,rw):
    """Fixed first-roof weak witness from ALL untruncated Newmark modes.

    This is the original interior pressure convention. The numerical
    witness can contain the earlier direct-pulse dispersive tail.
    """
    t=np.arange(nt,dtype=float)*dt
    indices=np.flatnonzero(abs(t-ROOF_CENTER_S)<ROOF_RADIUS_S)
    if len(indices)<8 or indices[0]<1 or indices[-1]>=nt-1:
        raise ValueError("native 250ms q0 does not cover frozen roof window")
    theta=2*np.arctan(.5*dt*np.sqrt(np.maximum(lam,0)))
    pressure=np.zeros(len(indices))
    for start in range(0,len(theta),4096):
        end=min(len(theta),start+4096)
        pressure+=np.cos(np.outer(indices,theta[start:end]))@kick[start:end]
    pressure*=1.2/dt
    source_xyz=np.column_stack([axes[k][v] for k,v in enumerate(
        np.unravel_index(src,native_dims))])
    receiver_xyz=np.column_stack([axes[k][v] for k,v in enumerate(
        np.unravel_index(rec,native_dims))])
    result=[]
    for width in ROOF_WIDTHS_S:
        witness=compact_odd_witness(t[indices],width,
            center_s=ROOF_CENTER_S,radius_s=ROOF_RADIUS_S)
        observed=float(pressure@witness)
        analytical=analytic_native_64point_physical_roof_echo_weak(
            source_xyz,sw,receiver_xyz,rw,width_s=width)
        expected=analytical["original_64pair_finite_roof_single_bounce_signed_weak_analytic"]
        result.append({
            "physical_roof_echo_witness_width_s":width,
            "full_allmode_hybrid_native_pressure_weak":observed,
            "independent_true_roof_64_pair_single_bounce_weak":expected,
            "signed_hybrid_over_analytic_roof_ratio":float(observed/expected),
            "relative_hybrid_vs_analytic_roof":float(abs(observed/expected-1)),
            "number_frozen_witness_samples":int(np.count_nonzero(witness)),
            "all_original_high_modes_retained":True,
            "cannot_isolate_first_roof_from_numerical_direct_tail":True,
            "not_full_250ms_original_acceptance":True})
    return result

def one_case(p,ppw,folder,original,prior):
    t=time.perf_counter()
    if (file_hash(folder/"comms_out.h5")!=original["original_native_comm_sha256"]
        or file_hash(folder/"vox_out.h5")!=original["original_solver_geometry_sha256"]):
        raise ValueError("true original eightnode source and Neumann raw HDF5 SHA mismatch")
    with h5py.File(folder/"vox_out.h5","r") as h:
        axes=[np.asarray(h[k][...],dtype=float) for k in ("xv","yv","zv")]
    native_dims=tuple(len(a) for a in axes)
    with h5py.File(folder/"comms_out.h5","r") as h:
        src=np.asarray(h["in_ixyz"][:],dtype=np.int64)
        rec=np.asarray(h["out_ixyz"][:],dtype=np.int64)
        sig=np.asarray(h["in_sigs"][:],dtype=float)
        rw=np.asarray(h["out_alpha"][:],dtype=float).ravel()
        nt=int(h["Nt"][()])
        if (src.shape!=(8,) or rec.shape!=(8,) or rw.shape!=(8,)
            or sig.shape!=(8,nt) or np.any(sig[:,1:]!=0)
            or abs(rw.sum()-1)>1e-12 or int(h["diff"][()])!=0):
            raise ValueError("native PFFDTD true original q0/receiver eightnode coefficients changed")
        strength=float(sig[:,0].sum())
        sw=sig[:,0]/strength
    with h5py.File(folder/"sim_consts.h5","r") as h:
        dt=float(h["Ts"][()]);c=float(h["c"][()])
        l2=float(h["l2"][()]);h_m=float(h["h"][()])
    if (abs(c-343.2)>1e-12 or abs(h_m-c/(100*ppw))>1e-12
        or abs(strength-l2/h_m)>1e-10
        or nt!=prior["native_Nt"] or abs(dt-prior["native_Ts_s"])>1e-12):
        raise ValueError("true original q0 native impulse h/dt/Nt changed")
    # Preserve original x physical FE nodal coordinates. The y/z P1
    # triangulation here is used ONLY to locate matching physical x vertices
    # and precommitted comparator; NEW y/z uses original Cartesian Q1.
    fem=build_original_native_conforming_roof_p1(
        axes,max_yz_nodes=3000,max_3d_nodes=180000)
    if fem.total_cells!=prior["all_true_nonfiltered_consistent_FEM_3D_modes"]:
        raise ValueError("prior true original physically conforming P1 baseline drift")
    mx,_,kx,_=consistent_physical_P1_tensored_operators(fem)
    hybrid=build_native_cartesian_true_roof_hybrid_flux(
        axes[1],axes[2],max_active_yz_nodes=p["caps"]["max_active_yz_nodes"])
    q1=hybrid.cut_q1
    ycoords=q1.physical_active_yz_node_positions_m
    tangent=ycoords[:,0]-.25*ycoords[:,1]
    hnative=float(axes[1][1]-axes[1][0])
    # All roof-only Q1 basis nodes, including positive-support exterior slivers.
    roof_separation=4.-.25*ycoords[:,0]-ycoords[:,1]
    roof_local=(ycoords[:,0]>2*hnative)&(ycoords[:,0]<4-2*hnative)&(
        ycoords[:,1]>2*hnative)&(abs(roof_separation)<2*hnative)
    if np.count_nonzero(roof_local)<5:
        raise ValueError("new hybrid roof manufactured test has insufficient rows")
    hybrid_affine_peak=float(np.max(abs((hybrid.stiffness@tangent)[roof_local])))
    if hybrid_affine_peak>1e-7:
        raise ValueError(f"new hybrid fails true inclined-Neumann affine roof test: {hybrid_affine_peak}")
    sx,sy,srcerr=original_eightnode_native_HDF5_Q1_source_receiver(
        native_dims,fem.x_native_map,q1,src,sw,len(fem.x_positions_m))
    rx,ry,recerr=original_eightnode_native_HDF5_Q1_source_receiver(
        native_dims,fem.x_native_map,q1,rec,rw,len(fem.x_positions_m))
    if max(srcerr,recerr)>1e-12:
        raise ValueError("original 8-point native Cartesian source is not Q1 consistent")
    lambdax,vx,xproof=generalized_symmetric_P1_consistent_modes(mx,kx)
    lambday,vy,yproof=generalized_native_original_Q1_full_physical_neumann_modes(hybrid.mass,hybrid.stiffness)
    totalmodes=len(lambdax)*len(lambday)
    if totalmodes>p["caps"]["max_3d_modes"]:
        raise ValueError("full native Cartesian Q1 roof modes above predeclared compute limit; cannot delete modes")
    M,K=true_full_3d_consistent_P1_MK(mx,hybrid.mass,kx,hybrid.stiffness)
    if M.shape[0]!=totalmodes:
        raise ValueError("true hybrid physical cut elements omitted generalized modes")
    lam=(lambdax[:,None]+lambday[None,:]).ravel()
    coupling=np.outer((sx@vx)*(rx@vx),(sy@vy)*(ry@vy)).ravel()
    kick=dt*dt*c*c*coupling/(1+dt*dt*lam/4)
    transfer=entire_original_finite_record_signed_modes(
        lam,kick,dt,nt,1.2).sum(axis=1)
    if not np.isfinite(transfer).all():
        raise ValueError("new Cartesian+true-roof hybrid full original q0 signed transfer invalid")
    # Manufactured full physical 3D affine u=x+z tests independently
    # verify geometry, P1x-Q1yz consistent volume mass and physical flux.
    ux=fem.x_positions_m[:,None]+q1.physical_active_yz_node_positions_m[None,:,1]
    field=ux.ravel()
    mass2=float(field@(M@field))
    stiff2=float(field@(K@field))
    norm_mass=2780/3
    norm_stiff=112*c*c
    massrel=abs(mass2-norm_mass)/norm_mass
    stiffrel=abs(stiff2-norm_stiff)/norm_stiff
    if massrel>2e-9 or stiffrel>2e-9:
        raise ValueError(f"true original-grid cut domain Q1 3D manufactured affine weak form failed: {massrel} {stiffrel}")
    constrel=float(np.max(abs(K@np.ones(totalmodes)))/
                   max(float(np.max(abs(K.diagonal()))),1.))
    if constrel>1e-10:
        raise ValueError("true physical Cartesian Q1 Neumann rigid constant failed")
    old=unpairs(prior["new_full_consistent_P1_250ms_original_q0_signed_40_80"])
    echo=fixed_first_roof_weak_full_modes(lam,kick,dt,nt,axes,native_dims,src,sw,rec,rw)
    return {"ppw":ppw,"original_SHA256_native_comms":file_hash(folder/"comms_out.h5"),
        "hybrid_first_roof_echo_frozen_allmode_3width_diagnostic":echo,
        "original_SHA256_native_voxel":file_hash(folder/"vox_out.h5"),
        "original_native_full_Nt":nt,"original_native_Ts_s":dt,
        "original_native_q0_sum_eightpoint_coeff":strength,
        "real_original_native_cartesian_yz_Q1_physical_support_modes":q1.native_yz_modes,
        "real_original_native_x_P1_physical_modes":len(lambdax),
        "real_original_native_3D_full_true_Q1_modes":totalmodes,
        "real_original_native_2D_cut_Q1_cell_count":q1.polygon_intersecting_native_cells,
        "real_min_original_native_positive_cut_Q1_polygon_m2":q1.minimum_strict_positive_cut_polygon_m2,
        "real_native_Q1_exterior_nodes_with_positive_physical_support_kept":q1.occupied_cartesian_original_exterior_nodes,
        "hybrid_full_cartesian_axis_flux_rectangles":hybrid.cartesian_full_rectangle_count,
        "hybrid_true_cut_Q1_rectangles":hybrid.exact_roof_cut_rectangle_count,
        "hybrid_exact_roof_affine_weak_peak":hybrid_affine_peak,
        "hybrid_c_squared_over_three_penalty":hybrid.full_rectangle_coefficient_c_squared_over_three,
        "hybrid_correction_nnz":int(hybrid.cartesian_full_rectangle_correction.nnz),
        "true_physical_exact_cross_section_area_m2":q1.physical_area_m2,
        "true_full_3D_original_roof_volume_m3":float(M.sum()),
        "new_true_physical_Q1_3D_offdiagonal_mass_nnz":int(M.nnz),
        "new_true_physical_hybrid_3D_stiffness_nnz":int(K.nnz),
        "true_3D_Q1_manufactured_affine_mass_rel":massrel,
        "true_3D_Q1_manufactured_affine_stiffness_rel":stiffrel,
        "true_3D_Q1_constant_neumann_relative":constrel,
        "all_x_physical_M_generalized_eigencheck":xproof,
        "all_yz_cartesian_cut_Q1_physical_M_generalized_eigencheck":yproof,
        "native_original_eight_src_Q1_factorization_error":srcerr,
        "native_original_eight_receiver_Q1_factorization_error":recerr,
        "true_original_Q1_modes_above_native_Nyquist_included":int(np.count_nonzero(dt*np.sqrt(lam)>np.pi)),
        "new_native_hybrid_true_roof_q0_full_original_250ms_signed_40_80":pairs(transfer),
        "prior_physically_conforming_P1_original_q0_full_signed_40_80":pairs(old),
        "same_original_eight_native_source_8_receiver_weights_and_full_q0":True,
        "no_high_mode_cut_or_sliver_point_removal":True,
        "seconds":float(time.perf_counter()-t)}


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    a=ap.parse_args()
    raw=a.plan.read_bytes()
    p=validate_plan(json.loads(raw.decode("utf-8")))
    o=p["original"]
    original=json.loads((ROOT/o["original_native_evidence"]).read_text(encoding="utf-8"))
    earlier=json.loads((ROOT/"benchmarks/acoustics/r130d_original_q0_conforming_p1_consistent_mass_evidence_2026-10-09.json").read_text(encoding="utf-8"))
    orig={r["ppw"]:r for r in original["actual_native_wave_cases"]}
    old={r["ppw"]:r for r in earlier["actual_conforming_P1_consistent_mass_original_q0_cases"]}
    if set(orig)!=set(PPW) or set(old)!=set(PPW):
        raise ValueError("original full five SHA HDF5 and pre-observed P1 consistent mass evidence absent")
    folders={}
    for c in a.original_sims_root.rglob("comms_out.h5"):
        h=file_hash(c)
        matches=[i for i in PPW if orig[i]["original_native_comm_sha256"]==h]
        if len(matches)==1:
            if matches[0] in folders:raise ValueError("duplicate original native PFFDTD comms source")
            folders[matches[0]]=c.parent
    if set(folders)!=set(PPW):
        raise ValueError("real original q0 five grids original SHA source unavailable")
    output={"schema_version":"htdt.r130d.original-q0-cartesian-flux-true-roof-hybrid-fullmode-evidence-1",
        "preregistered_plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":p,
        "canonical_upstream_original_PFFDTD_q0":"SELF_CONVERGENCE_FAILED",
        "independent_physical_validation":"NOT_VALIDATED","product":"NO_GO",
        "new_native_PFFDTD_waves":0,"new_GitHub_Actions_runs":0,
        "actual_native_original_point_q0_cartesian_hybrid_full_modes_cases":[],
        "original_signed_all_four_adjacent_three_gate_results":[]}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    for k in PPW:
        try:r=one_case(p,k,folders[k],orig[k],old[k])
        except Exception as exc:
            output["actual_native_original_point_q0_cartesian_hybrid_full_modes_cases"].append({
                "ppw":k,"status":"NEW_CARTESIAN_TRUE_ROOF_HYBRID_NOT_COMPLETED",
                "failure_type":type(exc).__name__,"failure_description":str(exc)})
            a.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        output["actual_native_original_point_q0_cartesian_hybrid_full_modes_cases"].append(r)
        a.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("TRUE_ROOF_ORIGINAL_8NODE_CARTESIAN_HYBRID_NATIVE_Q0",k,
              "yzDOF",r["real_original_native_cartesian_yz_Q1_physical_support_modes"],
              "3Dmodes",r["real_original_native_3D_full_true_Q1_modes"],
              "minWetArea",r["real_min_original_native_positive_cut_Q1_polygon_m2"],
              "signed",r["new_native_hybrid_true_roof_q0_full_original_250ms_signed_40_80"],
              flush=True)
    for coarse,fine in zip(output["actual_native_original_point_q0_cartesian_hybrid_full_modes_cases"],
                           output["actual_native_original_point_q0_cartesian_hybrid_full_modes_cases"][1:]):
        arms={}
        for arm,key in (
            ("preobserved_conforming_P1_consistent_mass_original_8node","prior_physically_conforming_P1_original_q0_full_signed_40_80"),
            ("new_native_cartesian_interior_fivepoint_true_roof_hybrid_original_8node","new_native_hybrid_true_roof_q0_full_original_250ms_signed_40_80")):
            c=coarse[key];f=fine[key]
            m=compare_complex_transfer(reference=f,candidate=c,
                frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
            arms[arm]={"signed_original_q0_40_80_coarse_minus_fine":pairs(unpairs(c)-unpairs(f)),
                "original_frozen_complex_magnitude_phase_and_frequency_bins":m,
                "all_three_original_frozen_gates_pass":bool(
                    m["complex_rms_relative"]<=.2 and m["magnitude_max_relative"]<=.25
                    and m["phase_max_deg"]<=15)}
        output["original_signed_all_four_adjacent_three_gate_results"].append({
            "coarse_ppw":coarse["ppw"],"fine_ppw":fine["ppw"],"arms":arms})
    verdict={}
    for arm in ARMS:
        scores=[q["arms"][arm] for q in output["original_signed_all_four_adjacent_three_gate_results"]]
        keys=("complex_rms_relative","magnitude_max_relative","phase_max_deg")
        strictly_mono=all(all(scores[j]["original_frozen_complex_magnitude_phase_and_frequency_bins"][k]<
                              scores[j-1]["original_frozen_complex_magnitude_phase_and_frequency_bins"][k]
                              for k in keys) for j in range(1,4))
        verdict[arm]={"all_four_pairs_pass_original_three_gates":all(
            z["all_three_original_frozen_gates_pass"] for z in scores),
            "all_three_metric_strictly_monotone":strictly_mono,
            "not_original_native_PFFDTD_qualification":True}
    output["full_original_q0_five_grid_cartesian_hybrid_convergence_verdict"]=verdict
    output["original_PFFDTD_canonical_still_failed"]=True
    a.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("CARTESIAN_HYBRID_ORIGINAL_8NODE_FULL_GRID_FROZEN_SCORING",[
        (x["coarse_ppw"],x["fine_ppw"],{
            arm:[round(x["arms"][arm]["original_frozen_complex_magnitude_phase_and_frequency_bins"][k],6)
                 for k in ("complex_rms_relative","magnitude_max_relative","phase_max_deg")]
            for arm in ARMS})
        for x in output["original_signed_all_four_adjacent_three_gate_results"]],flush=True)
    print("CARTESIAN_HYBRID_ORIGINAL_8NODE_ALL_FIVE_GRID_VERDICTS",verdict,flush=True)
if __name__=="__main__":
    main()
