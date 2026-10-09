#!/usr/bin/env python3
"""Original SHA HDF5 8node q0, original Cartesian Q1 cut-domain FEM, true roof.

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
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_native_exact_roof_full_xy_z_modal_q0 import entire_original_finite_record_signed_modes
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs
from run_r130d_original_point_quadratic_pffdtd import PIN,PPW,file_hash

SCHEMA="htdt.r130d.original-q0-exact-roof-Cartesian-cut-Q1-variational-plan-1"
ARMS=("preobserved_conforming_P1_consistent_mass_original_8node",
      "new_native_cartesian_Q1_exact_roof_consistent_Galerkin_original_8node")


def validate_plan(p):
    o=p["original"];n=p["new_operator"];v=p["independent_checks"]
    if (p.get("schema_version")!=SCHEMA or p["issue"]!=938 or p["pr"]!=1055
        or o["upstream_PFFDTD_sha"]!=PIN or o["five_native_PPWs"]!=list(PPW)
        or o["original_source_xyz"]!=[1.5,2,2] or o["original_receiver_xyz"]!=[2.5,2,2]
        or not o["preserve_original_exact_eight_SHA_HDF5_q0_weights_and_native_indices"]
        or not o["native_h_Ts_Nt_by_grid"]
        or o["physical_room_m3"]!=56 or o["full_original_record_s"]!=.25
        or o["signed_freq_hz"]!=[40,80] or o["c"]!=343.2 or o["rho"]!=1.2
        or not all(n[k] for k in (
            "no_sliver_drop_or_ghost_node_deletion_if_positive_support",
            "no_high_modal_cutoff_or_clamp",
            "no_mass_floor_or_fitted_coeffs",
            "no_smoothing_or_taper_or_damping_or_frequency_mask"))
        or not all(v[k] for k in (
            "source_shape_matches_original_eight_native_hdf5_indices",
            "all_original_point_source_observation_coefficients_unaltered",
            "all_modal_true_M_orthonormality_and_K_residual",
            "strict_Neumann_constant_null",
            "exact_gaussian_degree4_quadrature_weights_positive",
            "number_of_all_physical_modes_reported",
            "all_original_5_grids_and_4_adjacent_pairs_required",
            "all_three_metrics_must_pass_all_pairs_and_strict_monotonicity_for_any_valid_solution",
            "record_signed_40_80_all_modes_no_high_filter",
            "report_previous_original_8node_conforming_P1_signed_baseline_without_recompute_or_fit",
            "no_native_upstream_PFFDTD_requalification"))
        or [v[k] for k in ("physical_clipped_integral_yz_area_exact",
                            "physical_true_3D_volume_m3",
                            "independent_affine_mass_u_x_plus_z_squared_exact",
                            "independent_affine_stiffness_u_x_plus_z") ]!=[14,56,2780/3,112]
        or [v[k] for k in ("complex_gate","magnitude_gate","phase_deg_gate")]!=[.2,.25,15]
        or p["caps"]!={"max_yz_Q1_active_nodes":3300,"max_physical_3D_modes":180000,
                       "max_cases":5,"new_native_PFFDTD_wave_runs":0,
                       "new_github_actions_runs":0,"retain_scratch":True}
        or p["authority"]["original_native_PFFDTD_q0"]!="SELF_CONVERGENCE_FAILED"
        or p["authority"]["independent_BRAS_MFEM"]!="NOT_VALIDATED"
        or p["authority"]["product"]!="NO_GO"):
        raise ValueError("true native Q1 original q0 full physical experiment preregistration drift")
    return p


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
    q1=cut_roof_native_original_Q1_galerkin_yz(
        axes[1],axes[2],max_active_yz_nodes=p["caps"]["max_yz_Q1_active_nodes"])
    sx,sy,srcerr=original_eightnode_native_HDF5_Q1_source_receiver(
        native_dims,fem.x_native_map,q1,src,sw,len(fem.x_positions_m))
    rx,ry,recerr=original_eightnode_native_HDF5_Q1_source_receiver(
        native_dims,fem.x_native_map,q1,rec,rw,len(fem.x_positions_m))
    if max(srcerr,recerr)>1e-12:
        raise ValueError("original 8-point native Cartesian source is not Q1 consistent")
    lambdax,vx,xproof=generalized_symmetric_P1_consistent_modes(mx,kx)
    lambday,vy,yproof=generalized_native_original_Q1_full_physical_neumann_modes(q1.mass,q1.stiffness)
    totalmodes=len(lambdax)*len(lambday)
    if totalmodes>p["caps"]["max_physical_3D_modes"]:
        raise ValueError("full native Cartesian Q1 roof modes above predeclared compute limit; cannot delete modes")
    M,K=true_full_3d_consistent_P1_MK(mx,q1.mass,kx,q1.stiffness)
    if M.shape[0]!=totalmodes:
        raise ValueError("true Q1 physical cut elements omitted generalized modes")
    lam=(lambdax[:,None]+lambday[None,:]).ravel()
    coupling=np.outer((sx@vx)*(rx@vx),(sy@vy)*(ry@vy)).ravel()
    kick=dt*dt*c*c*coupling/(1+dt*dt*lam/4)
    transfer=entire_original_finite_record_signed_modes(
        lam,kick,dt,nt,1.2).sum(axis=1)
    if not np.isfinite(transfer).all():
        raise ValueError("new exact Q1 variational full original q0 signed transfer invalid")
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
    return {"ppw":ppw,"original_SHA256_native_comms":file_hash(folder/"comms_out.h5"),
        "original_SHA256_native_voxel":file_hash(folder/"vox_out.h5"),
        "original_native_full_Nt":nt,"original_native_Ts_s":dt,
        "original_native_q0_sum_eightpoint_coeff":strength,
        "real_original_native_cartesian_yz_Q1_physical_support_modes":q1.native_yz_modes,
        "real_original_native_x_P1_physical_modes":len(lambdax),
        "real_original_native_3D_full_true_Q1_modes":totalmodes,
        "real_original_native_2D_cut_Q1_cell_count":q1.polygon_intersecting_native_cells,
        "real_min_original_native_positive_cut_Q1_polygon_m2":q1.minimum_strict_positive_cut_polygon_m2,
        "real_native_Q1_exterior_nodes_with_positive_physical_support_kept":q1.occupied_cartesian_original_exterior_nodes,
        "true_physical_exact_cross_section_area_m2":q1.physical_area_m2,
        "true_full_3D_original_roof_volume_m3":float(M.sum()),
        "new_true_physical_Q1_3D_offdiagonal_mass_nnz":int(M.nnz),
        "new_true_physical_Q1_3D_stiffness_nnz":int(K.nnz),
        "true_3D_Q1_manufactured_affine_mass_rel":massrel,
        "true_3D_Q1_manufactured_affine_stiffness_rel":stiffrel,
        "true_3D_Q1_constant_neumann_relative":constrel,
        "all_x_physical_M_generalized_eigencheck":xproof,
        "all_yz_cartesian_cut_Q1_physical_M_generalized_eigencheck":yproof,
        "native_original_eight_src_Q1_factorization_error":srcerr,
        "native_original_eight_receiver_Q1_factorization_error":recerr,
        "true_original_Q1_modes_above_native_Nyquist_included":int(np.count_nonzero(dt*np.sqrt(lam)>np.pi)),
        "new_native_Q1_true_roof_q0_full_original_250ms_signed_40_80":pairs(transfer),
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
    original=json.loads((ROOT/o["original_native_real_source_and_voxel_HDF5_evidence"]).read_text(encoding="utf-8"))
    earlier=json.loads((ROOT/o["preobserved_P1_consistent_original_eightpoint_control"]).read_text(encoding="utf-8"))
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
    output={"schema_version":"htdt.r130d.original-q0-exact-roof-native-cut-Q1-fullmode-evidence-1",
        "preregistered_plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":p,
        "canonical_upstream_original_PFFDTD_q0":"SELF_CONVERGENCE_FAILED",
        "independent_physical_validation":"NOT_VALIDATED","product":"NO_GO",
        "new_native_PFFDTD_waves":0,"new_GitHub_Actions_runs":0,
        "actual_native_original_point_q0_Q1_cutroof_full_modes_cases":[],
        "original_signed_all_four_adjacent_three_gate_results":[]}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    for k in PPW:
        try:r=one_case(p,k,folders[k],orig[k],old[k])
        except Exception as exc:
            output["actual_native_original_point_q0_Q1_cutroof_full_modes_cases"].append({
                "ppw":k,"status":"NEW_VARIATIONAL_Q1_FULLMODE_NOT_COMPLETED",
                "failure_type":type(exc).__name__,"failure_description":str(exc)})
            a.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        output["actual_native_original_point_q0_Q1_cutroof_full_modes_cases"].append(r)
        a.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("TRUE_ROOF_ORIGINAL_8NODE_CARTESIAN_Q1_NATIVE_Q0",k,
              "yzDOF",r["real_original_native_cartesian_yz_Q1_physical_support_modes"],
              "3Dmodes",r["real_original_native_3D_full_true_Q1_modes"],
              "minWetArea",r["real_min_original_native_positive_cut_Q1_polygon_m2"],
              "signed",r["new_native_Q1_true_roof_q0_full_original_250ms_signed_40_80"],
              flush=True)
    for coarse,fine in zip(output["actual_native_original_point_q0_Q1_cutroof_full_modes_cases"],
                           output["actual_native_original_point_q0_Q1_cutroof_full_modes_cases"][1:]):
        arms={}
        for arm,key in (
            ("preobserved_conforming_P1_consistent_mass_original_8node","prior_physically_conforming_P1_original_q0_full_signed_40_80"),
            ("new_native_cartesian_Q1_exact_roof_consistent_Galerkin_original_8node","new_native_Q1_true_roof_q0_full_original_250ms_signed_40_80")):
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
    output["full_original_q0_five_grid_Q1_variational_convergence_verdict"]=verdict
    output["original_PFFDTD_canonical_still_failed"]=True
    a.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("PHYSICAL_Q1_ORIGINAL_8NODE_FULL_GRID_FROZEN_SCORING",[
        (x["coarse_ppw"],x["fine_ppw"],{
            arm:[round(x["arms"][arm]["original_frozen_complex_magnitude_phase_and_frequency_bins"][k],6)
                 for k in ("complex_rms_relative","magnitude_max_relative","phase_max_deg")]
            for arm in ARMS})
        for x in output["original_signed_all_four_adjacent_three_gate_results"]],flush=True)
    print("PHYSICAL_Q1_ORIGINAL_8NODE_ALL_FIVE_GRID_VERDICTS",verdict,flush=True)
if __name__=="__main__":
    main()
