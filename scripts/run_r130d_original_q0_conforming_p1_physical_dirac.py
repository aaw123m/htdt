#!/usr/bin/env python3
"""Physical Galerkin Dirac-point weak source + point receiver vs original 8node.

Same previously frozen true-roof consistent-mass P1 M/K and all original
native q[0], native h/Ts/Nt, full 250ms and fixed signed 40/80 gates.
The physical Dirac P1-shape coupling is an EXPERIMENT, not canonical upstream.
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
from htdt.r130d_conforming_roof_p1_fem import (
    build_original_native_conforming_roof_p1,
    original_eightnode_FEM_source_receiver)
from htdt.r130d_conforming_roof_p1_consistent_mass import (
    consistent_physical_P1_tensored_operators,
    generalized_symmetric_P1_consistent_modes)
from htdt.r130d_conforming_p1_physical_dirac import (
    physical_roof_P1_point_shape,
    physical_P1_point_tensor_modal_projection)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_point_quadratic_pffdtd import PIN,PPW,file_hash
from run_r130d_native_exact_roof_full_xy_z_modal_q0 import entire_original_finite_record_signed_modes
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs

SCHEMA="htdt.r130d.original-q0-conforming-p1-exact-dirac-shape-source-receiver-plan-1"
ARMS=("preobserved_original_eightnode_consistent_mass_P1",
      "true_physical_P1_weak_dirac_source_point_receiver")


def validate_plan(p):
    f=p["frozen_original"];q=p["preobserved_control"]
    v=p["verification"];m=p["physical_numerical_operator"]
    if (p.get("schema_version")!=SCHEMA or p["issue"]!=938 or p["pr"]!=1055
        or f["upstream_PFFDTD_SHA"]!=PIN
        or f["ppw"]!=list(PPW)
        or f["original_source_xyz_m"]!=[1.5,2,2]
        or f["original_receiver_xyz_m"]!=[2.5,2,2]
        or not f["true_original_native_8point_in_sigs_and_out_alpha_preserved_and_SHA_verified"]
        or not f["original_native_record_h_dt_Nt_preserved"]
        or f["record_s"]!=.25 or f["signed_hz"]!=[40,80]
        or f["rho"]!=1.2 or f["c"]!=343.2
        or not q["preobserved_full_5grid_all_signed_baseline"]
        or not q["preobserved_isolated_32_36_three_gate_PASS"]
        or not q["preobserved_full_5grid_FAIL"]
        or q["preobserved_baseline_complex_errors"]!=[
            .08734577514010763,.01594774802765642,
            .3774688964998303,.4982317979175555]
        or not m["no_raising_or_clamping_high_modes"]
        or not m["all_401630_physical_true_mass_generalized_eigenmodes_in_full_5grid"]
        or not m["no_smoothing_damping_fitting_point_change_or_modal_cut"]
        or [v[k] for k in ("canonical_original_three_gates_complex",
                           "magnitude","phase_deg")]!=[.2,.25,15]
        or v["original_8node_preobserved_signed_reconstructed_relative_max"]!=2e-10
        or v["exact_shape_partition_and_affine_moment_error_max"]!=2e-10
        or not all(v[k] for k in ("same_original_phys_point","same_native_hdf5_sha",
            "all_five_ppw_28_32_36_40_44",
            "full_sparse_true_3D_FEM_mass_and_stiffness_unchanged",
            "full_galerkin_all_mode_M_orthogonal",
            "all_four_adjacent_grid_comparisons_and_all_three_gates",
            "strict_all_three_metric_monotonicity_required",
            "retain_original_canonical_8point_arm_and_original_PFFDTD_FAIL",
            "report_all_unfavorable_signed_complex_40_80_and_per_bin_scores",
            "independent_manufactured_weak_dirac_shape_function_tests"))
        or p["limits"]!={"max_3d_dofs":180000,"max_yz_nodes":3000,
                         "max_grid_cases":5,"new_native_pffdtd_wave_runs":0,
                         "new_github_actions":0,"keep_scratch":True}
        or p["authority"]["original_PFFDTD_q0"]!="SELF_CONVERGENCE_FAILED"
        or p["authority"]["physical_validation"]!="NOT_VALIDATED"
        or p["authority"]["product"]!="NO_GO"):
        raise ValueError("true original q0 physical P1 weak Dirac source/receiver preregistration drift")
    return p


def one_case(p,ppw,folder,original,old):
    start=time.perf_counter()
    if (file_hash(folder/"comms_out.h5")!=original["original_native_comm_sha256"]
        or file_hash(folder/"vox_out.h5")!=original["original_solver_geometry_sha256"]):
        raise ValueError("original source and voxel SHA HDF5 drift")
    with h5py.File(folder/"vox_out.h5","r") as h:
        axes=[np.asarray(h[k][...],dtype=float) for k in ("xv","yv","zv")]
    with h5py.File(folder/"comms_out.h5","r") as h:
        src=np.asarray(h["in_ixyz"][:],dtype=np.int64)
        rec=np.asarray(h["out_ixyz"][:],dtype=np.int64)
        sig=np.asarray(h["in_sigs"][:],dtype=float)
        rw=np.asarray(h["out_alpha"][:],dtype=float).ravel()
        nt=int(h["Nt"][()])
        if (src.shape!=(8,) or rec.shape!=(8,) or rw.shape!=(8,)
            or sig.shape!=(8,nt) or np.any(sig[:,1:]!=0)
            or int(h["diff"][()])!=0 or abs(rw.sum()-1)>1e-12):
            raise ValueError("original PFFDTD native eightnode source/receiver or q0 changed")
        strength=float(sig[:,0].sum())
        sw=sig[:,0]/strength
    with h5py.File(folder/"sim_consts.h5","r") as h:
        dt=float(h["Ts"][()]);grid_h=float(h["h"][()])
        c=float(h["c"][()]);l2=float(h["l2"][()])
    if (abs(c-343.2)>1e-12 or abs(grid_h-c/(100*ppw))>1e-12
        or nt!=old["native_Nt"] or abs(dt-old["native_Ts_s"])>1e-12
        or abs(strength-l2/grid_h)>1e-10 or abs(sw.sum()-1)>1e-12):
        raise ValueError("true original native q0 source strength and time changed")
    fem=build_original_native_conforming_roof_p1(axes,max_yz_nodes=3000,
                                                  max_3d_nodes=180000)
    if (fem.total_cells!=old["all_true_nonfiltered_consistent_FEM_3D_modes"]
        or len(fem.yz_triangles)!=old["physical_true_FEM_yz_triangles"]):
        raise ValueError("true physical P1 mesh no longer matches preregistered consistent-mass baseline")
    sx,sy,source_orig_error=original_eightnode_FEM_source_receiver(fem,src,sw)
    rx,ry,receiver_orig_error=original_eightnode_FEM_source_receiver(fem,rec,rw)
    if max(source_orig_error,receiver_orig_error)>1e-12:
        raise ValueError("original eightnode tensor physical source changed")
    mx,my,kx,ky=consistent_physical_P1_tensored_operators(fem)
    lamx,vx,dx=generalized_symmetric_P1_consistent_modes(mx,kx)
    lamy,vy,dy=generalized_symmetric_P1_consistent_modes(my,ky)
    if len(lamx)*len(lamy)!=fem.total_cells:
        raise ValueError("true physical eigenmodes truncated")
    src_phys=physical_roof_P1_point_shape(
        fem,p["frozen_original"]["original_source_xyz_m"])
    recv_phys=physical_roof_P1_point_shape(
        fem,p["frozen_original"]["original_receiver_xyz_m"])
    usx,usy=physical_P1_point_tensor_modal_projection(fem,src_phys,vx,vy)
    urx,ury=physical_P1_point_tensor_modal_projection(fem,recv_phys,vx,vy)
    lam=(lamx[:,None]+lamy[None,:]).ravel()
    amplitude_orig=(dt*dt*c*c*np.outer(
        (sx@vx)*(rx@vx),(sy@vy)*(ry@vy))).ravel()/(1+dt*dt*lam/4)
    amplitude_physical=(dt*dt*c*c*np.outer(
        usx*urx,usy*ury)).ravel()/(1+dt*dt*lam/4)
    orig=entire_original_finite_record_signed_modes(
        lam,amplitude_orig,dt,nt,1.2).sum(axis=1)
    physical=entire_original_finite_record_signed_modes(
        lam,amplitude_physical,dt,nt,1.2).sum(axis=1)
    prior=unpairs(old["new_full_consistent_P1_250ms_original_q0_signed_40_80"])
    diff=float(np.linalg.norm(prior-orig)/max(np.linalg.norm(prior),1e-14))
    if diff>p["verification"]["original_8node_preobserved_signed_reconstructed_relative_max"]:
        raise ValueError(f"preobserved original point q0 eightnode control not exactly reproduced: {diff}")
    if not np.isfinite(physical).all():
        raise ValueError("new exact physical P1 point weak delta pressure nonfinite")
    return {"ppw":int(ppw),"native_original_q0_source_hdf5_SHA256":file_hash(folder/"comms_out.h5"),
        "native_original_voxel_hdf5_SHA256":file_hash(folder/"vox_out.h5"),
        "native_Ts_s":dt,"native_Nt":nt,
        "original_native_eight_source_coefficients_sum":strength,
        "true_physical_P1_all_mass_modes_count":int(fem.total_cells),
        "true_physical_3D_exact_roof_volume_m3":float(mx.sum()*my.sum()),
        "true_physical_P1_yz_triangles":int(len(fem.yz_triangles)),
        "native_rigid_Neumann_physical_zero_x_yz":bool(lamx[0]==0 and lamy[0]==0),
        "true_consistent_x_all_eigenmodes_proof":dx,
        "true_consistent_yz_all_eigenmodes_proof":dy,
        "new_P1_weak_dirac_shape_source":{
            "point_xyz_m":list(src_phys.physical_xyz_m),
            "original_physical_point_unchanged":True,
            "true_P1_x_nonzero_support":src_phys.x_nnz,
            "true_P1_yz_nonzero_support":src_phys.yz_nnz,
            "true_P1_support_3d_vertices":src_phys.support_native_3d_nodes,
            "global_P1_yz_triangle":src_phys.triangle_index,
            "true_P1_x_full_basis_weights":src_phys.x_weights.tolist(),
            "true_P1_yz_nonzero_indices":np.flatnonzero(src_phys.yz_weights).tolist(),
            "true_P1_yz_nonzero_weights":src_phys.yz_weights[src_phys.yz_weights>0].tolist(),
            "exact_dirac_affine_coordinate_error_m":src_phys.max_moment_error_m,
            "P1_partition_error":src_phys.partition_error},
        "new_P1_weak_dirac_point_receiver":{
            "point_xyz_m":list(recv_phys.physical_xyz_m),
            "original_physical_point_unchanged":True,
            "true_P1_x_nonzero_support":recv_phys.x_nnz,
            "true_P1_yz_nonzero_support":recv_phys.yz_nnz,
            "true_P1_support_3d_vertices":recv_phys.support_native_3d_nodes,
            "global_P1_yz_triangle":recv_phys.triangle_index,
            "true_P1_x_nonzero_indices":np.flatnonzero(recv_phys.x_weights).tolist(),
            "true_P1_x_nonzero_weights":recv_phys.x_weights[recv_phys.x_weights>0].tolist(),
            "true_P1_yz_nonzero_indices":np.flatnonzero(recv_phys.yz_weights).tolist(),
            "true_P1_yz_nonzero_weights":recv_phys.yz_weights[recv_phys.yz_weights>0].tolist(),
            "exact_dirac_affine_coordinate_error_m":recv_phys.max_moment_error_m,
            "P1_partition_error":recv_phys.partition_error},
        "original_PFFDTD_eightnode_vs_preobserved_signed_relative":diff,
        "all_actual_physical_FEM_modes_retained_including_high_freq":True,
        "same_real_native_q0_250ms_40_80_unchanged":True,
        "original_8node_consistent_mass_250ms_signed_40_80":pairs(orig),
        "true_P1_dirac_weak_source_point_receiver_250ms_signed_40_80":pairs(physical),
        "physical_point_dirac_minus_original_8node_signed_40_80":pairs(physical-orig),
        "wall_seconds":float(time.perf_counter()-start)}


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    a=ap.parse_args()
    raw=a.plan.read_bytes()
    p=validate_plan(json.loads(raw.decode("utf-8")))
    source=json.loads((ROOT/p["frozen_original"]["native_real_HDF5_source_evidence"]).read_text(encoding="utf-8"))
    prior=json.loads((ROOT/p["preobserved_control"]["existing_true_consistent_mass_p1_evidence"]).read_text(encoding="utf-8"))
    originals={x["ppw"]:x for x in source["actual_native_wave_cases"]}
    old={x["ppw"]:x for x in prior["actual_conforming_P1_consistent_mass_original_q0_cases"]}
    if (set(originals)!=set(PPW) or set(old)!=set(PPW)
        or prior["original_native_PFFDTD_q0"]!="SELF_CONVERGENCE_FAILED"):
        raise ValueError("original raw 8node native q0 and mass consistent old baseline not all available")
    dirs={}
    for f in a.original_sims_root.rglob("comms_out.h5"):
        sha=file_hash(f)
        match=[i for i in PPW if originals[i]["original_native_comm_sha256"]==sha]
        if len(match)==1:
            if match[0] in dirs:raise ValueError("duplicate original input SHA")
            dirs[match[0]]=f.parent
    if set(dirs)!=set(PPW):raise ValueError("all five original real q0 HDF5 required")
    evidence={"schema_version":"htdt.r130d.original-q0-conforming-P1-exact-dirac-point-evidence-1",
        "preregistered_plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":p,
        "canonical_original_PFFDTD_q0":"SELF_CONVERGENCE_FAILED",
        "external_independent_physical":"NOT_VALIDATED",
        "product":"NO_GO","new_original_upstream_PFFDTD_waves":0,
        "new_GitHub_Actions_runs":0,
        "point_shape_is_experimental_not_original_eightnode_observation":True,
        "actual_original_input_full_5grid_P1_exact_weak_dirac_cases":[],
        "all_four_complete_native_refinement_pairs":[]}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    for i in PPW:
        try:r=one_case(p,i,dirs[i],originals[i],old[i])
        except Exception as exc:
            evidence["actual_original_input_full_5grid_P1_exact_weak_dirac_cases"].append({
                "ppw":i,"status":"TRUE_P1_POINT_SHAPE_INTEGRATOR_FAILED",
                "failure_type":type(exc).__name__,"failure_detail":str(exc)})
            a.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        evidence["actual_original_input_full_5grid_P1_exact_weak_dirac_cases"].append(r)
        a.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("P1_EXACT_PHYSICAL_DIRAC_NATIVE_Q0",i,"all_modes",r["true_physical_P1_all_mass_modes_count"],
              "point_support",(r["new_P1_weak_dirac_shape_source"]["true_P1_support_3d_vertices"],
                               r["new_P1_weak_dirac_point_receiver"]["true_P1_support_3d_vertices"]),
              "true_signed",r["true_P1_dirac_weak_source_point_receiver_250ms_signed_40_80"],
              "old_control_rel",r["original_PFFDTD_eightnode_vs_preobserved_signed_relative"],flush=True)
    cases=evidence["actual_original_input_full_5grid_P1_exact_weak_dirac_cases"]
    for coarse,fine in zip(cases,cases[1:]):
        arms={}
        for arm,key in (
            ("preobserved_original_eightnode_consistent_mass_P1","original_8node_consistent_mass_250ms_signed_40_80"),
            ("true_physical_P1_weak_dirac_source_point_receiver","true_P1_dirac_weak_source_point_receiver_250ms_signed_40_80")):
            c=coarse[key];f=fine[key]
            metrics=compare_complex_transfer(reference=f,candidate=c,
                frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
            arms[arm]={"signed_original_40_80_coarse_minus_fine":pairs(unpairs(c)-unpairs(f)),
                "unchanged_original_three_metrics_and_per_bin":metrics,
                "all_three_original_frozen_gates_pass":bool(
                    metrics["complex_rms_relative"]<=.2 and
                    metrics["magnitude_max_relative"]<=.25 and
                    metrics["phase_max_deg"]<=15)}
        evidence["all_four_complete_native_refinement_pairs"].append({
            "coarse_ppw":coarse["ppw"],"fine_ppw":fine["ppw"],"arms":arms})
    verdict={}
    for arm in ARMS:
        compare=[x["arms"][arm] for x in evidence["all_four_complete_native_refinement_pairs"]]
        k=("complex_rms_relative","magnitude_max_relative","phase_max_deg")
        mono=all(all(compare[j]["unchanged_original_three_metrics_and_per_bin"][key]<
                     compare[j-1]["unchanged_original_three_metrics_and_per_bin"][key]
                     for key in k) for j in range(1,4))
        verdict[arm]={"all_four_original_three_gate_pass":all(
            x["all_three_original_frozen_gates_pass"] for x in compare),
            "three_metric_strict_monotone":mono,
            "no_original_native_PFFDTD_requalification":True}
    evidence["entire_original_native_5grid_three_gate_convergence_verdicts"]=verdict
    evidence["no_qualified_original_or_product_PFFDTD"]=True
    a.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("P1_PHYSICAL_WEAK_DIRAC_ALL_FIVE_GRID_SCORES",[
        (x["coarse_ppw"],x["fine_ppw"],{
            arm:[round(x["arms"][arm]["unchanged_original_three_metrics_and_per_bin"][k],6)
                 for k in ("complex_rms_relative","magnitude_max_relative","phase_max_deg")]
            for arm in ARMS})
        for x in evidence["all_four_complete_native_refinement_pairs"]],flush=True)
    print("P1_PHYSICAL_WEAK_DIRAC_ORIGINAL_5GRID_VERDICT",verdict,flush=True)
if __name__=="__main__":
    main()
