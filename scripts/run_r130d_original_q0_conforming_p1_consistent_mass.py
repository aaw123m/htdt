#!/usr/bin/env python3
"""Prospectively frozen true conforming P1 consistent-mass full point-q0 PPW run."""
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
    generalized_symmetric_P1_consistent_modes,
    true_full_3d_consistent_P1_MK)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_point_quadratic_pffdtd import PIN,PPW,file_hash
from run_r130d_native_exact_roof_full_xy_z_modal_q0 import entire_original_finite_record_signed_modes
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs

SCHEMA="htdt.r130d.original-q0-conforming-roof-p1-consistent-mass-plan-1"
ARMS=("preobserved_P1_lumped_mass","new_P1_true_consistent_mass")


def validate_plan(p):
    q=p["preobserved_baseline"]
    f=p["frozen"]
    m=p["new_operator"]
    v=p["verification"]
    if (p.get("schema_version")!=SCHEMA or p["issue"]!=938 or p["pr"]!=1055
        or f["upstream_PFFDTD_sha"]!=PIN or f["ppw"]!=list(PPW)
        or f["physical_source_xyz"]!=[1.5,2,2]
        or f["physical_receiver_xyz"]!=[2.5,2,2]
        or not f["original_eight_input_weights_and_indices"]
        or not f["original_q0_sample_one_then_zero"]
        or not f["original_native_individual_dt_nt_h"]
        or f["room_m3"]!=56 or f["roof_z_y0"]!=4 or f["roof_slope_per_y"]!=.25
        or f["record_s"]!=.25 or f["frequency_hz"]!=[40,80]
        or f["c_m_s"]!=343.2 or f["rho_kg_m3"]!=1.2
        or not q["known_lumped_PPW28_32_36_40_44_results_before_this_plan"]
        or not q["prior_FEM_mesh_and_source_coupling_unchanged"]
        or not q["new_consistent_mass_outcomes_NOT_known_before_plan"]
        or q["prior_FEM_yz_nodes"]!=[1081,1359,1716,2101,2497]
        or q["prior_3D_FEM_modes"]!=[37835,53001,75504,102949,132341]
        or q["prior_FEM_mass_lumped_relative_complex_by_pair"]!=[
            .15229048259476563,.1787136237508971,
            .40717874888166433,.5785506308265708]
        or not m["no_high_mode_filter_or_smoothing_taper_damping_source_reposition"]
        or not m["all_3D_true_FE_modes_complete"]
        or not all(v[k] for k in (
            "exact_consistent_x_mass_row_sums_equal_prior_lumped_x",
            "exact_consistent_yz_mass_row_sums_equal_prior_lumped_yz",
            "true_full_3D_offdiagonal_M_positive_definite",
            "true_full_operator_neumann_constant_null",
            "true_all_generalized_modes_M_orthonormal_and_residual",
            "source_weight_identity_and_original_true_SHA",
            "all_4_adjacent_original_complex_mag_phase",
            "no_favorable_bin_or_pair_selection",
            "save_all_complex_signed_per_grid_and_unfavorable_bins",
            "independent_manufactured_variational_mass_energy_test"))
        or v["exact_total_true_3D_mass_volume_m3"]!=56
        or v["baseline_lumped_signed_from_preobserved_evidence_exact_relative_max"]!=2e-10
        or [v[k] for k in ("original_complex_gate","original_magnitude_gate",
                          "original_phase_deg_gate")]!=[.2,.25,15]
        or p["limits"]!={"max_true_3D_FEM_DOF":180000,"max_yz_vertices":3000,
            "max_ppw_cases":5,"new_upstream_PFFDTD_wave_runs":0,
            "new_github_actions_runs":0,"retain_scratch":True}
        or p["authority"]["original_PFFDTD_q0"]!="SELF_CONVERGENCE_FAILED"
        or p["authority"]["independent_physics"]!="NOT_VALIDATED"
        or p["authority"]["product"]!="NO_GO"):
        raise ValueError("full physical P1 consistent mass original q0 preregistration drift")
    return p


def one_case(p,ppw,sim,source,observed):
    start=time.perf_counter()
    if (file_hash(sim/"comms_out.h5")!=source["original_native_comm_sha256"]
        or file_hash(sim/"vox_out.h5")!=source["original_solver_geometry_sha256"]):
        raise ValueError("original PFFDTD q0 exact eight source/voxel HDF5 SHA modified")
    with h5py.File(sim/"vox_out.h5","r") as h:
        axes=[np.asarray(h[k][...],dtype=float) for k in ("xv","yv","zv")]
    with h5py.File(sim/"comms_out.h5","r") as h:
        si=np.asarray(h["in_ixyz"][:],dtype=np.int64)
        ri=np.asarray(h["out_ixyz"][:],dtype=np.int64)
        waves=np.asarray(h["in_sigs"][:],dtype=float)
        rw=np.asarray(h["out_alpha"][:],dtype=float).ravel()
        nt=int(h["Nt"][()])
        if (si.shape!=(8,) or ri.shape!=(8,) or rw.shape!=(8,)
            or waves.shape!=(8,nt) or np.any(waves[:,1:]!=0)
            or abs(float(sum(rw))-1)>1e-12 or int(h["diff"][()])!=0):
            raise ValueError("true original 8node point q0 receiver and source altered")
        strength=float(waves[:,0].sum())
        sw=waves[:,0]/strength
    with h5py.File(sim/"sim_consts.h5","r") as h:
        dt=float(h["Ts"][()]);h_m=float(h["h"][()])
        c=float(h["c"][()]);l2=float(h["l2"][()])
    if (abs(c-343.2)>1e-12 or abs(h_m-c/(100*ppw))>1e-12
        or abs(strength-l2/h_m)>1e-10
        or nt!=observed["original_native_Nt"]
        or abs(dt-observed["original_native_Ts_s"])>1e-12):
        raise ValueError("original q[0]=1 250ms native clock or source normalization changed")
    fem=build_original_native_conforming_roof_p1(
        axes,max_yz_nodes=p["limits"]["max_yz_vertices"],
        max_3d_nodes=p["limits"]["max_true_3D_FEM_DOF"])
    if (len(fem.yz_positions_m)!=observed["true_FEM_yz_nodes"]
        or fem.total_cells!=observed["full_original_native_grid_spanning_all_FEM_modes"]
        or len(fem.yz_triangles)!=observed["true_FEM_yz_triangles"]):
        raise ValueError("preobserved physically exact P1 triangles changed")
    sx,sy,srcerr=original_eightnode_FEM_source_receiver(fem,si,sw)
    rx,ry,recerr=original_eightnode_FEM_source_receiver(fem,ri,rw)
    if max(srcerr,recerr)>1e-12:
        raise ValueError("unchanged original eight native source and receiver P1 tensor rank changed")
    mx,my,kx,ky=consistent_physical_P1_tensored_operators(fem)
    lx,vx,xproof=generalized_symmetric_P1_consistent_modes(mx,kx)
    ly,vy,yproof=generalized_symmetric_P1_consistent_modes(my,ky)
    mass,stiffness=true_full_3d_consistent_P1_MK(mx,my,kx,ky)
    if fem.total_cells!=len(lx)*len(ly):
        raise ValueError("true physical complete consistent FEM modes artificially truncated")
    lam=(lx[:,None]+ly[None,:]).ravel()
    original_point_coupling=np.outer(
        (sx@vx)*(rx@vx),(sy@vy)*(ry@vy)).ravel()
    A=dt*dt*c*c*original_point_coupling/(1+dt*dt*lam/4)
    signal=entire_original_finite_record_signed_modes(
        lam,A,dt,nt,1.2).sum(axis=1)
    prior=unpairs(observed["new_FEM_full_250ms_original_point_q0_signed_40_80"])
    eig=np.outer(vx[:,1],vy[:,1]).ravel()
    rel=float(np.linalg.norm(stiffness@eig-(lx[1]+ly[1])*(mass@eig))/
              max(np.linalg.norm(stiffness@eig),1e-14))
    kzero=float(max(abs(stiffness@np.ones(fem.total_cells)))/max(
        float(np.max(abs(stiffness.diagonal()))),1.))
    # Actual full sparse 3D energy with exact positive off-diagonal
    # consistent Galerkin mass and original eight-node delta weak source.
    if (rel>2e-7 or kzero>1e-10
        or abs(float(np.sum(mass@np.ones(fem.total_cells)))-56)>2e-8):
        raise ValueError("true 3D Galerkin consistent FE energy/conservative mode rejected")
    if not np.isfinite(signal).all():
        raise ValueError("consistent P1 full native q0 40/80 signed transfer invalid")
    return {"ppw":ppw,
        "source_original_comm_sha256":file_hash(sim/"comms_out.h5"),
        "geometry_original_voxel_sha256":file_hash(sim/"vox_out.h5"),
        "native_Ts_s":dt,"native_Nt":nt,"native_original_total_8node_q0_strength":strength,
        "physical_true_FEM_x_nodes":len(lx),"physical_true_FEM_yz_nodes":len(ly),
        "physical_true_FEM_yz_triangles":len(fem.yz_triangles),
        "all_true_nonfiltered_consistent_FEM_3D_modes":fem.total_cells,
        "consistent_full_3D_mass_volume_m3":float(np.sum(mass@np.ones(fem.total_cells))),
        "consistent_true_3D_mass_nnz":int(mass.nnz),
        "consistent_true_3D_stiffness_nnz":int(stiffness.nnz),
        "strict_true_3D_FEM_eigenpair_residual":rel,
        "strict_true_3D_Neumann_rigid_zero_residual":kzero,
        "all_true_M_consistent_P1_x_modes_check":xproof,
        "all_true_M_consistent_P1_yz_modes_check":yproof,
        "original_source_tensor_8node_residual":srcerr,
        "original_receiver_tensor_8node_residual":recerr,
        "highest_native_omega_dt":float(np.max(dt*np.sqrt(lam))),
        "true_modal_count_above_nyquist":int(np.count_nonzero(dt*np.sqrt(lam)>np.pi)),
        "preobserved_full_lumped_P1_250ms_original_q0_signed_40_80":pairs(prior),
        "new_full_consistent_P1_250ms_original_q0_signed_40_80":pairs(signal),
        "consistent_vs_lumped_original_full_two_signed_bin_relative":float(
            np.linalg.norm(signal-prior)/max(np.linalg.norm(prior),1e-14)),
        "all_original_eight_point_q0_causal_full_record_unchanged":True,
        "all_added_mesh_roof_boundary_nodes_have_zero_original_q0_load":True,
        "all_high_frequency_physical_modes_retained":True,
        "seconds":float(time.perf_counter()-start)}


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args()
    raw=args.plan.read_bytes()
    p=validate_plan(json.loads(raw.decode("utf-8")))
    q=p["preobserved_baseline"]
    source=json.loads((ROOT/p["frozen"]["original_raw_source_evidence"]).read_text(encoding="utf-8"))
    prior=json.loads((ROOT/q["prior_mass_lumped_FEM_evidence"]).read_text(encoding="utf-8"))
    orig={row["ppw"]:row for row in source["actual_native_wave_cases"]}
    earlier={row["ppw"]:row for row in prior["actual_original_q0_conforming_roof_P1_FEM_cases"]}
    if (set(orig)!=set(PPW) or set(earlier)!=set(PPW)
        or prior["canonical_original_PFFDTD_q0"]!="SELF_CONVERGENCE_FAILED"
        or prior["product"]!="NO_GO"):
        raise ValueError("all five original SHA q0 and preobserved lumped FEM nonconvergence missing")
    dirs={}
    for f in args.original_sims_root.rglob("comms_out.h5"):
        check=file_hash(f)
        k=[ppw for ppw in PPW if orig[ppw]["original_native_comm_sha256"]==check]
        if len(k)==1:
            if k[0] in dirs:raise ValueError("ambiguous original comms SHA P1 reference")
            dirs[k[0]]=f.parent
    if set(dirs)!=set(PPW):
        raise ValueError("five original SHA eightnode q0 real HDF5 cases absent")
    out={"schema_version":"htdt.r130d.original-q0-conforming-roof-p1-consistent-mass-evidence-1",
        "prospective_plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":p,
        "original_native_PFFDTD_q0":"SELF_CONVERGENCE_FAILED",
        "external_physical_validation":"NOT_VALIDATED","product":"NO_GO",
        "new_upstream_pffdtd_waves":0,"new_github_actions_runs":0,
        "actual_conforming_P1_consistent_mass_original_q0_cases":[],
        "full_frozen_40_80_adjacent":[]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    for ppw in PPW:
        try:row=one_case(p,ppw,dirs[ppw],orig[ppw],earlier[ppw])
        except Exception as exc:
            out["actual_conforming_P1_consistent_mass_original_q0_cases"].append({
                "ppw":ppw,"status":"NEW_CONSISTENT_MASS_P1_NUMERICAL_FAILED",
                "error_type":type(exc).__name__,"error_detail":str(exc)})
            args.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        out["actual_conforming_P1_consistent_mass_original_q0_cases"].append(row)
        args.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("ORIGINAL_Q0_TRUE_P1_CONSISTENT_MASS_ALLMODES",ppw,
              row["all_true_nonfiltered_consistent_FEM_3D_modes"],
              "mass_nnz",row["consistent_true_3D_mass_nnz"],
              "real_signed",row["new_full_consistent_P1_250ms_original_q0_signed_40_80"],
              "seconds",round(row["seconds"],1),flush=True)
    rows=out["actual_conforming_P1_consistent_mass_original_q0_cases"]
    for co,fi in zip(rows,rows[1:]):
        results={}
        for arm,key in (
            ("preobserved_P1_lumped_mass","preobserved_full_lumped_P1_250ms_original_q0_signed_40_80"),
            ("new_P1_true_consistent_mass","new_full_consistent_P1_250ms_original_q0_signed_40_80")):
            c=co[key];f=fi[key]
            met=compare_complex_transfer(reference=f,candidate=c,frequency_hz=[40,80],
                magnitude_mask_relative_db=-50).model_dump(mode="json")
            results[arm]={"true_original_two_bin_signed_coarse_minus_fine":pairs(unpairs(c)-unpairs(f)),
                "full_frozen_original_complex_mag_phase":met,
                "original_three_gates_all_pass":bool(met["complex_rms_relative"]<=.2
                    and met["magnitude_max_relative"]<=.25 and met["phase_max_deg"]<=15)}
        out["full_frozen_40_80_adjacent"].append({"coarse_ppw":co["ppw"],
            "fine_ppw":fi["ppw"],"arms":results})
    verdict={}
    for arm in ARMS:
        data=[q["arms"][arm] for q in out["full_frozen_40_80_adjacent"]]
        keys=("complex_rms_relative","magnitude_max_relative","phase_max_deg")
        mono=all(all(data[i]["full_frozen_original_complex_mag_phase"][k]<
                     data[i-1]["full_frozen_original_complex_mag_phase"][k]
                     for k in keys) for i in range(1,4))
        verdict[arm]={"all_four_pairs_original_three_gate_pass":all(
            item["original_three_gates_all_pass"] for item in data),
            "strict_three_metric_monotonicity":mono,
            "cannot_requalify_original_upstream_PFFDTD":True}
    # Guard that this new score calculation does not change the previously
    # saved unfavorable lumped FEM PPW comparisons.
    old_scores=[x["arms"]["new_conforming_roof_P1_FEM"]["full_original_frozen_complex_mag_phase"][
        "complex_rms_relative"] for x in prior["all_four_frozen_original_three_gate_adjacent"]]
    observed=[x["arms"]["preobserved_P1_lumped_mass"]["full_frozen_original_complex_mag_phase"][
        "complex_rms_relative"] for x in out["full_frozen_40_80_adjacent"]]
    if max(abs(a-b) for a,b in zip(old_scores,observed))>p["verification"][
            "baseline_lumped_signed_from_preobserved_evidence_exact_relative_max"]:
        raise ValueError("previously observed physically original lumped P1 wave evidence mutated")
    out["all_five_original_q0_consistent_mass_comparison_verdicts"]=verdict
    out["preobserved_original_point_q0_lumped_FEM_baseline_frozen"]=True
    args.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("P1_TRUE_CONSISTENT_MASS_ORIGINAL_Q0_ALL_FOUR_RESULTS",[
        (x["coarse_ppw"],x["fine_ppw"],{
            arm:[round(x["arms"][arm]["full_frozen_original_complex_mag_phase"][k],6)
                 for k in ("complex_rms_relative","magnitude_max_relative","phase_max_deg")]
            for arm in ARMS}) for x in out["full_frozen_40_80_adjacent"]],flush=True)
    print("P1_TRUE_CONSISTENT_MASS_FULL_FIVE_GRID_VERDICT",verdict,flush=True)
if __name__=="__main__":
    main()
