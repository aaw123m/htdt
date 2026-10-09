#!/usr/bin/env python3
"""Actual original q0 8node source/receiver full P1 conforming roof FE five-grid run.

The original native PFFDTD remains the canonical reference and failed.
This is a new true 3D conservative variational FEM, with exact physical
sloping Neumann boundary and no cutcell/sliver mode manipulation.
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
    original_eightnode_FEM_source_receiver,
    generalized_conforming_p1_neumann_modes)
from htdt.r130d_native_exact_roof_separable import generalized_neumann_modes
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_native_exact_roof_full_xy_z_modal_q0 import entire_original_finite_record_signed_modes
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs
from run_r130d_original_point_quadratic_pffdtd import PIN,PPW,file_hash

SCHEMA="htdt.r130d.original-q0-conforming-roof-p1-fem-multigrid-plan-1"
C=343.2


def validate_plan(p):
    o=p["fixed_original"];n=p["new_spatial_method"];v=p["verification"]
    if (p.get("schema_version")!=SCHEMA or p["issue"]!=938 or p["pr"]!=1055
        or o["upstream_sha"]!=PIN or o["ppw"]!=list(PPW)
        or o["original_source_m"]!=[1.5,2,2]
        or o["original_receiver_m"]!=[2.5,2,2]
        or not o["raw_original_eight_point_source_and_receiver_HDF5_unchanged"]
        or not o["native_each_dt_nt_h_unchanged"]
        or o["original_full_record_seconds"]!=.25
        or o["original_full_signed_frequency_hz"]!=[40,80]
        or o["original_sound_speed_m_s"]!=C or o["original_rho_kg_m3"]!=1.2
        or o["sloped_prism_m"]!={"length":4,"roof_z_y0":4,
                                "roof_slope":.25,"volume":56}
        or not n["all_modes"] or not n["no_modal_truncation_or_clamp"]
        or not n["no_sliver_cutoff_or_mesh_agglomeration"]
        or not n["no_source_or_receiver_reinterpolation_fitting"]
        or not v["true_full_matrix_kronecker_identity"]
        or not v["true_sparse_FEM_symmetric_neumann_constant_mode"]
        or not v["positive_lumped_physical_element_mass"]
        or not v["independent_small_mesh_manufactured_affine_and_constant_field_energy_tests"]
        or not v["all_cartesian_original_source_receiver_indices_preserved"]
        or v["exact_yz_polygon_area_m2"]!=14 or v["exact_full_volume_m3"]!=56
        or v["exact_volume_absolute_tolerance"]!=2e-8
        or v["reproduction_of_reference_old_FV_modal_vs_saved_true_CG_rel_max"]!=2e-5
        or not v["reference_old_FV_all_five_grids_previously_known"]
        or not v["report_min_triangle_area_and_high_frequency_counts"]
        or not v["all_four_adjacent_pair_original_complex_mag_phase"]
        or [v[k] for k in ("frozen_complex_gate","frozen_magnitude_gate",
                           "frozen_phase_deg_gate")]!=[.2,.25,15]
        or not v["no_three_gate_or_monotonic_cherry_pick"]
        or not v["report_full_signed_40_80_all_five_grids"]
        or not v["preserve_unfavorable_results"]
        or p["limits"]!={"max_true_3d_fem_dofs":180000,
                         "max_each_yz_vertices":2200,"max_cases":5,
                         "new_original_PFFDTD_wave_runs":0,
                         "new_github_actions_runs":0,"clean_scratch":False}
        or p["authority"]["original_upstream_PFFDTD_q0"]!="SELF_CONVERGENCE_FAILED"
        or p["authority"]["independent_physics"]!="NOT_VALIDATED"
        or p["authority"]["product"]!="NO_GO"):
        raise ValueError("prospective original point q0 conforming roof FEM frozen plan drift")
    return p


def validate_ppw44_resource_addendum(a,p):
    old=a["resource_only_original_and_new"]
    frozen=a["frozen_original"]
    if (a.get("schema_version")!="htdt.r130d.original-q0-conforming-p1-ppw44-capacity-addendum-1"
        or a["issue"]!=938 or a["pr"]!=1055
        or a["original_prospectively_pushed_plan"]!="benchmarks/acoustics/r130d_original_q0_conforming_roof_p1_fem_multigrid_plan_2026-10-09.json"
        or a["original_precommitted_plan_head"]!="4950dc5bc33754fbb482700972aa01fc111893d2"
        or old["original_max_each_yz_vertices"]!=p["limits"]["max_each_yz_vertices"]
        or old["new_ppw44_only_max_each_yz_vertices"]!=3000
        or old["max_true_3d_fem_dofs_unchanged"]!=p["limits"]["max_true_3d_fem_dofs"]
        or old["max_cases"]!=5 or not old["no_other_parameter_changed"]
        or frozen["ppw"]!=list(PPW) or frozen["source_xyz"]!=[1.5,2,2]
        or frozen["receiver_xyz"]!=[2.5,2,2]
        or frozen["true_roof_volume_m3"]!=56
        or [frozen[k] for k in ("original_complex_gate","original_magnitude_gate",
                                "original_phase_deg_gate")]!=[.2,.25,15]
        or not all(frozen[k] for k in ("original_eight_hdf5_source_receiver",
            "original_q0_unchanged","full_250ms_40_80_unchanged",
            "all_true_FEM_modes_no_drop","unchanged_solver_P1_geometry_integrator_mass",
            "all_four_adjacent_pairs_required","no_metric_fitting_or_frequency_mask"))
        or a["limits"]!={"new_pffdtd_wave_runs":0,"new_github_actions_runs":0,
                         "clean_scratch":False}
        or a["authority"]["original_PFFDTD"]!="SELF_CONVERGENCE_FAILED"
        or a["authority"]["physical"]!="NOT_VALIDATED"
        or a["authority"]["product"]!="NO_GO"):
        raise ValueError("PPW44 prospectively frozen physical mesh resource-only addendum mutated")
    return a


def one_case(p,ppw,sim,original,prior,ppw44_max_nodes):
    t0=time.perf_counter()
    if (file_hash(sim/"comms_out.h5")!=original["original_native_comm_sha256"]
        or file_hash(sim/"vox_out.h5")!=original["original_solver_geometry_sha256"]):
        raise ValueError("original 8node physical q0 or true geometry SHA changed")
    with h5py.File(sim/"vox_out.h5","r") as h:
        axes=[np.asarray(h[k][...],dtype=float) for k in ("xv","yv","zv")]
    with h5py.File(sim/"comms_out.h5","r") as h:
        src=np.asarray(h["in_ixyz"][:],dtype=np.int64)
        rec=np.asarray(h["out_ixyz"][:],dtype=np.int64)
        sig=np.asarray(h["in_sigs"][:],dtype=float)
        rw=np.asarray(h["out_alpha"][:],dtype=float).ravel()
        nt=int(h["Nt"][()])
        if (src.shape!=(8,) or rec.shape!=(8,) or rw.shape!=(8,)
            or sig.shape!=(8,nt) or np.any(sig[:,1:]!=0)
            or int(h["diff"][()])!=0 or abs(float(rw.sum())-1)>1e-12):
            raise ValueError("original 8/8 physically fixed true native point q0 changed")
        strength=float(sig[:,0].sum())
        sw=sig[:,0]/strength
    with h5py.File(sim/"sim_consts.h5","r") as h:
        dt=float(h["Ts"][()]);grid_h=float(h["h"][()])
        c=float(h["c"][()]);l2=float(h["l2"][()])
    if (abs(c-C)>1e-12 or abs(grid_h-C/(100*ppw))>1e-12
        or abs(dt-prior["original_native_dt_s"])>1e-12
        or nt!=prior["original_record_samples"]
        or abs(strength-l2/grid_h)>1e-10
        or abs(sum(sw)-1)>1e-12):
        raise ValueError("original native q0 source clock/source strength changed")
    fem=build_original_native_conforming_roof_p1(
        axes,max_yz_nodes=(ppw44_max_nodes if ppw==44 else p["limits"]["max_each_yz_vertices"]),
        max_3d_nodes=p["limits"]["max_true_3d_fem_dofs"])
    if fem.total_cells>p["limits"]["max_true_3d_fem_dofs"]:
        raise ValueError("real FEM full 3D resource bound exceeded")
    sx,sy,sr=original_eightnode_FEM_source_receiver(fem,src,sw)
    rx,ry,rr=original_eightnode_FEM_source_receiver(fem,rec,rw)
    if max(sr,rr)>1e-12:
        raise ValueError("original physical 8-node source tensor coupling changed")
    lamx,vx,xcheck=generalized_conforming_p1_neumann_modes(fem.Mx,fem.Kx)
    lamy,vy,ycheck=generalized_conforming_p1_neumann_modes(fem.Myz,fem.Kyz)
    full_modes=int(len(lamx)*len(lamy))
    if full_modes!=fem.total_cells:
        raise ValueError("conforming roof P1 FEM artificially truncated modal spectrum")
    lam=(lamx[:,None]+lamy[None,:]).ravel()
    coupling=np.outer(
        (sx@vx)*(rx@vx),
        (sy@vy)*(ry@vy)).ravel()
    A=(dt*dt*c*c*coupling)/(1.+dt*dt*lam/4.)
    signed=entire_original_finite_record_signed_modes(
        lam,A,dt,nt,1.2).sum(axis=1)
    if not np.isfinite(signed).all():
        raise ValueError("full point original q0 FEM signed transfer nonfinite")
    M,K=fem.sparse_full_3d_operators()
    if (M.shape!=(full_modes,full_modes) or K.shape!=(full_modes,full_modes)
        or abs(float(M.diagonal().sum())-56)>2e-8):
        raise ValueError("true conservative full 3D FEM physically wrong mass/volume")
    first=np.outer(vx[:,1],vy[:,1]).ravel()
    residual=float(np.linalg.norm(K@first-(lamx[1]+lamy[1])*(M@first))/
                   max(np.linalg.norm(K@first),1e-14))
    null=float(np.max(abs(K@np.ones(full_modes)))/max(
        float(np.max(abs(K.diagonal()))),1.))
    skew=(K-K.T).tocoo()
    asym=float(max(abs(skew.data),default=0.))/max(
        float(np.max(abs(K.diagonal()))),1.)
    if (residual>3e-7 or null>1e-10 or asym>1e-12):
        raise ValueError(f"physical P1 3D Neumann FEM eigen/constant/symmetry: {residual} {null} {asym}")
    # The old FV direct full-wave archived unmodified raw signed control is
    # retained ONLY as a comparator: spatial FEM differs by construction.
    old=unpairs(prior["experimental_signed_P_T_over_Q_T_40_80"])
    difference=float(np.linalg.norm(signed-old)/max(np.linalg.norm(old),1e-14))
    return {"ppw":ppw,
        "original_native_comm_sha256":file_hash(sim/"comms_out.h5"),
        "original_native_voxel_sha256":file_hash(sim/"vox_out.h5"),
        "original_native_Ts_s":dt,"original_native_Nt":nt,
        "original_source_q0_total_strength":strength,
        "true_FEM_x_nodes":len(lamx),"true_FEM_yz_nodes":len(lamy),
        "true_FEM_yz_triangles":len(fem.yz_triangles),
        "full_original_native_grid_spanning_all_FEM_modes":full_modes,
        "physical_area_yz_m2":fem.exact_cross_section_area_m2,
        "physical_true_volume_m3":float(M.diagonal().sum()),
        "minimum_original_native_P1_triangle_m2":fem.minimum_yz_triangle_area_m2,
        "maximum_original_native_P1_triangle_aspect_ratio":fem.maximum_yz_triangle_aspect_ratio,
        "minimum_FEM_mass_yz_m2":float(np.min(fem.yz_mass)),
        "maximum_FEM_mass_yz_m2":float(np.max(fem.yz_mass)),
        "true_full_FEM_3D_sparse_stiffness_nnz":int(K.nnz),
        "true_full_FEM_symmetric_neumann_eigenpair_rel":residual,
        "true_full_FEM_symmetric_Neumann_constant_null_rel":null,
        "true_full_FEM_stiffness_asym_rel":asym,
        "true_FEM_x_generalized_mode_checks":xcheck,
        "true_FEM_yz_generalized_mode_checks":ycheck,
        "original_source_eightnode_FEM_tensor_residual":sr,
        "original_receiver_eightnode_FEM_tensor_residual":rr,
        "true_FEM_modes_above_native_Nyquist":int(np.count_nonzero(dt*np.sqrt(lam)>np.pi)),
        "true_FEM_highest_omega_dt":float(np.max(dt*np.sqrt(lam))),
        "new_FEM_full_250ms_original_point_q0_signed_40_80":pairs(signed),
        "prior_exact_roof_FV_CG_full_250ms_original_point_q0_signed_40_80":pairs(old),
        "FEM_relative_signed_difference_from_old_CG_cutcell_FV":difference,
        "all_original_eight_input_nodes_and_receiver_nodes_mapped_without_reinterpolation":True,
        "additional_physical_roof_FEM_nodes_receive_no_direct_original_point_q0_force":True,
        "original_frozen_250ms_no_modal_removal_or_frequency_mask":True,
        "wall_seconds":float(time.perf_counter()-t0)}


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--ppw44-resource-addendum",type=Path,required=True)
    args=ap.parse_args()
    raw=args.plan.read_bytes()
    p=validate_plan(json.loads(raw.decode("utf-8")))
    a_raw=args.ppw44_resource_addendum.read_bytes()
    a=validate_ppw44_resource_addendum(json.loads(a_raw.decode("utf-8")),p)
    o=p["fixed_original"]
    original=json.loads((ROOT/o["original_native_hdf5_sha_evidence"]).read_text(encoding="utf-8"))
    cg=json.loads((ROOT/o["reference_real_fullstate_exact_roof_fv"]).read_text(encoding="utf-8"))
    source={row["ppw"]:row for row in original["actual_native_wave_cases"]}
    controls={row["ppw"]:row for row in cg["actual_native_grid_point_impulse_exact_roof_cases"]}
    if set(source)!=set(PPW) or set(controls)!=set(PPW):
        raise ValueError("all five original real archived 8node q0 sources or old real CG evidence absent")
    dirs={}
    for f in args.original_sims_root.rglob("comms_out.h5"):
        sha=file_hash(f)
        found=[i for i in PPW if source[i]["original_native_comm_sha256"]==sha]
        if len(found)==1:
            if found[0] in dirs:raise ValueError("ambiguous original HDF5 source SHA match")
            dirs[found[0]]=f.parent
    if set(dirs)!=set(PPW):
        raise ValueError("original SHA-pinned five-grid native HDF5 sources absent")
    output={"schema_version":"htdt.r130d.original-q0-conforming-roof-p1-fem-multigrid-evidence-1",
        "pre_observation_plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":p,
        "PPW44_resource_only_pre_observation_addendum":a,
        "PPW44_addendum_pre_observation_sha256_lf":hashlib.sha256(a_raw.replace(b"\r\n",b"\n")).hexdigest(),
        "PPW28_32_36_40_data_were_observed_before_PP44_capacity_addendum":True,
        "canonical_original_PFFDTD_q0":"SELF_CONVERGENCE_FAILED",
        "independent_physical_validation":"NOT_VALIDATED","product":"NO_GO",
        "new_original_pffdtd_waves":0,"new_github_actions_runs":0,
        "actual_original_q0_conforming_roof_P1_FEM_cases":[],
        "all_four_frozen_original_three_gate_adjacent":[]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    for ppw in PPW:
        try:case=one_case(p,ppw,dirs[ppw],source[ppw],controls[ppw],
                          a["resource_only_original_and_new"]["new_ppw44_only_max_each_yz_vertices"])
        except Exception as exc:
            output["actual_original_q0_conforming_roof_P1_FEM_cases"].append({
                "ppw":ppw,"status":"CONFORMING_ROOF_FEM_FAILED",
                "error_type":type(exc).__name__,"error_description":str(exc)})
            args.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        output["actual_original_q0_conforming_roof_P1_FEM_cases"].append(case)
        args.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("P1_TRUE_PHYSICAL_ROOF_8NODE_ORIGINAL_Q0",ppw,
              "all_modes",case["full_original_native_grid_spanning_all_FEM_modes"],
              "yz_triangles",case["true_FEM_yz_triangles"],
              "min_elem_m2",case["minimum_original_native_P1_triangle_m2"],
              "time_s",round(case["wall_seconds"],1),
              "signed",case["new_FEM_full_250ms_original_point_q0_signed_40_80"],flush=True)
    cases=output["actual_original_q0_conforming_roof_P1_FEM_cases"]
    for a,b in zip(cases,cases[1:]):
        oldref=a["prior_exact_roof_FV_CG_full_250ms_original_point_q0_signed_40_80"]
        oldcandidate=b["prior_exact_roof_FV_CG_full_250ms_original_point_q0_signed_40_80"]
        newref=a["new_FEM_full_250ms_original_point_q0_signed_40_80"]
        newcandidate=b["new_FEM_full_250ms_original_point_q0_signed_40_80"]
        scores={}
        for arm,coarse,fine in (("old_exact_roof_FV_CG",oldref,oldcandidate),
                                ("new_conforming_roof_P1_FEM",newref,newcandidate)):
            metric=compare_complex_transfer(reference=fine,candidate=coarse,
                frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
            scores[arm]={"true_original_40_80_signed_coarse_minus_fine":pairs(
                unpairs(coarse)-unpairs(fine)),
                "full_original_frozen_complex_mag_phase":metric,
                "all_original_three_limits_pass":bool(metric["complex_rms_relative"]<=.2
                    and metric["magnitude_max_relative"]<=.25
                    and metric["phase_max_deg"]<=15)}
        output["all_four_frozen_original_three_gate_adjacent"].append({
            "coarse_ppw":a["ppw"],"fine_ppw":b["ppw"],"arms":scores})
    verdicts={}
    for arm in ("old_exact_roof_FV_CG","new_conforming_roof_P1_FEM"):
        q=[v["arms"][arm] for v in output["all_four_frozen_original_three_gate_adjacent"]]
        metric_keys=("complex_rms_relative","magnitude_max_relative","phase_max_deg")
        monotone=all(all(q[j]["full_original_frozen_complex_mag_phase"][k]<
                         q[j-1]["full_original_frozen_complex_mag_phase"][k]
                         for k in metric_keys) for j in range(1,4))
        verdicts[arm]={"all_four_original_three_gate_acceptance":all(z["all_original_three_limits_pass"] for z in q),
            "strict_all_three_metric_monotonicity":monotone,
            "no_canonical_upstream_native_requalification":True}
    output["comparison_full_five_grid_verdicts"]=verdicts
    output["original_upstream_PFFDTD_self_convergence_still_failed"]=True
    args.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("CONFORMING_ROOF_Q0_P1_FEM_ORIGINAL_ALL_PAIR_RESULTS",[
        (x["coarse_ppw"],x["fine_ppw"],{
            key:[round(x["arms"][key]["full_original_frozen_complex_mag_phase"][k],6)
                for k in ("complex_rms_relative","magnitude_max_relative","phase_max_deg")]
            for key in x["arms"]})
        for x in output["all_four_frozen_original_three_gate_adjacent"]],flush=True)
    print("CONFORMING_ROOF_Q0_P1_FEM_ALL_FIVE_GRID_VERDICTS",verdicts,flush=True)

if __name__=="__main__":
    main()
