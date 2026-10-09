#!/usr/bin/env python3
"""Preregistered all-five original q0 hybrid modal/endpoint attribution.

Refuses to execute 5-grid numerical experiment until the prospective plan
commit is verifiably on the remote-tracking branch. No CI reruns.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import h5py
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend/src"))
from htdt.r130d_cartesian_true_roof_hybrid_flux import build_native_cartesian_true_roof_hybrid_flux
from htdt.r130d_conforming_roof_p1_fem import build_original_native_conforming_roof_p1
from htdt.r130d_conforming_roof_p1_consistent_mass import (
    consistent_physical_P1_tensored_operators,generalized_symmetric_P1_consistent_modes)
from htdt.r130d_native_cut_roof_Q1_galerkin import (
    original_eightnode_native_HDF5_Q1_source_receiver,
    generalized_native_original_Q1_full_physical_neumann_modes)
from htdt.r130d_original_q0_modal_endpoint_attribution import (
    BAND_TITLES, ENDPOINT_NAMES,
    partition_original_q0_hybrid_allmodes,signed_real_projection_on_full_delta)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_point_quadratic_pffdtd import PIN,PPW,file_hash
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs

SCHEMA="htdt.r130d.original-q0-hybrid-modal-endpoint-attribution-plan-1"
PLAN_COMMIT="ff0f3b96ffa27aa3dc0fc072450f93f7a8fcb4ac"
PLAN_PATH="benchmarks/acoustics/r130d_original_q0_hybrid_modal_endpoint_attribution_plan_2026-10-10.json"
ORIGIN="origin/feat/r130d-embedded-neumann-fv-20261009"
ARM="new_native_cartesian_interior_fivepoint_true_roof_hybrid_original_8node"


def validate_plan(p):
    o=p["original"];m=p["method"];caps=p["caps"];a=p["authority"]
    if (p["schema_version"]!=SCHEMA or p["issue"]!=938 or p["pr"]!=1055
        or o["upstream_PFFDTD_sha"]!=PIN or o["ppw"]!=list(PPW)
        or o["original_source_xyz_m"]!=[1.5,2,2]
        or o["original_receiver_xyz_m"]!=[2.5,2,2]
        or not o["native_dt_h_Nt_unchanged"]
        or o["record_length_s"]!=.25 or o["signed_bins_hz"]!=[40,80]
        or o["c_m_s"]!=343.2 or o["rho_kg_m3"]!=1.2
        or not o["all_four_pairs_and_strict_monotonicity"]
        or o["three_gates"]!={"complex_rms_relative":.2,
                             "magnitude_max_relative":.25,"phase_max_deg":15}
        or m["frequency_bands_hz"]!=[[0,100],[100,200],[200,400],[400,800],
                                    [800,1600],[1600,3200],[3200,None]]
        or m["endpoint_parts"]!=list(ENDPOINT_NAMES)
        or not all(m[k] for k in (
            "independent_direct_small_mode_fourier_verification",
            "report_all_bands_all_grids_both_signed_bins_all_endpoint_components",
            "no_high_mode_cut_no_source_smoothing_no_extra_window_no_amplitude_phase_fitting",
            "recompute_and_compare_frozen_all_four_three_gates"))
        or [m[k] for k in (
            "all_mode_frozen_signed_reference",
            "all_modal_band_partition_tolerance_relative",
            "all_endpoint_partition_tolerance_relative",
            "all_nyquist_partition_tolerance_relative")]!=[
                "previously committed unchanged full hybrid 40/80Hz signed values; require relative agreement 2e-8",
                2e-8,2e-8,2e-8]
        or caps!={"max_active_yz_nodes":3300,"max_modes":180000,
                  "max_ppw_cases":5,"new_native_PFFDTD_runs":0,
                  "new_GitHub_Actions_runs":0,"retain_scratch":True}
        or a!={"original_PFFDTD":"SELF_CONVERGENCE_FAILED",
                "independent_physics":"NOT_VALIDATED","product":"NO_GO",
                "PR_draft":True,"issue_open":True}):
        raise ValueError("original q0 modal/endpoint attribution frozen prospective plan drift")
    return p


def require_prospective_plan_pushed():
    """Fail CLOSED; no experimental real-HDF5 execution if push was rejected."""
    def git(*args):
        return subprocess.run(["git","-C",str(ROOT),*args],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
    exists=git("cat-file","-e",PLAN_COMMIT+"^{commit}")
    reachable=git("merge-base","--is-ancestor",PLAN_COMMIT,ORIGIN)
    committed=git("show",PLAN_COMMIT+":"+PLAN_PATH)
    local_remote_ref=git("rev-parse",ORIGIN)
    # Confirm the actual GitHub advertised branch, not only a possibly
    # stale locally cached remote-tracking ref. Network/auth denial fails closed.
    real_remote_ref=git("ls-remote","origin",
        "refs/heads/feat/r130d-embedded-neumann-fv-20261009")
    advertised=real_remote_ref.stdout.decode("ascii",errors="replace").split()
    current=(ROOT/PLAN_PATH).read_bytes().replace(b"\r\n",b"\n")
    if (exists.returncode or reachable.returncode or committed.returncode
        or local_remote_ref.returncode or real_remote_ref.returncode
        or len(advertised)!=2 or advertised[0]!=local_remote_ref.stdout.decode().strip()
        or committed.stdout.replace(b"\r\n",b"\n")!=current):
        raise RuntimeError(
            "PREREGISTRATION_NOT_PUSHED: no 5-grid experiment permitted; "
            "push precommitted plan and refresh original remote-tracking branch "
            "before executing; GitHub network/auth denial also blocks the run")
    return True


def one_case(ppw, folder, old, prior, p):
    if (file_hash(folder/"vox_out.h5")!=old["original_solver_geometry_sha256"]
        or file_hash(folder/"comms_out.h5")!=old["original_native_comm_sha256"]):
        raise ValueError("actual original native source/room SHA mismatch")
    with h5py.File(folder/"vox_out.h5","r") as f:
        axes=[np.asarray(f[n][:],dtype=float) for n in ("xv","yv","zv")]
    shape=tuple(len(a) for a in axes)
    with h5py.File(folder/"comms_out.h5","r") as f:
        src=np.asarray(f["in_ixyz"][:],dtype=np.int64)
        rec=np.asarray(f["out_ixyz"][:],dtype=np.int64)
        sig=np.asarray(f["in_sigs"][:],dtype=float)
        rw=np.asarray(f["out_alpha"][:],dtype=float).ravel()
        nt=int(f["Nt"][()])
        if (src.shape!=(8,) or rec.shape!=(8,) or sig.shape!=(8,nt)
            or rw.shape!=(8,) or np.any(sig[:,1:]!=0)
            or abs(float(rw.sum())-1)>1e-12 or int(f["diff"][()])!=0):
            raise ValueError("actual native 8-in/out original q0 altered")
        strength=float(sig[:,0].sum())
        sw=sig[:,0]/strength
    with h5py.File(folder/"sim_consts.h5","r") as f:
        dt=float(f["Ts"][()]);h=float(f["h"][()])
        c=float(f["c"][()]);l2=float(f["l2"][()])
    if (abs(c-343.2)>1e-12 or abs(h-c/(100*ppw))>1e-12
        or abs(strength-l2/h)>1e-10
        or nt!=prior["original_native_full_Nt"]
        or abs(dt-prior["original_native_Ts_s"])>1e-12):
        raise ValueError("native original q0 temporal and source normalization drift")
    fem=build_original_native_conforming_roof_p1(
        axes,max_yz_nodes=3000,max_3d_nodes=180000)
    mx,_,kx,_=consistent_physical_P1_tensored_operators(fem)
    hy=build_native_cartesian_true_roof_hybrid_flux(
        axes[1],axes[2],max_active_yz_nodes=p["caps"]["max_active_yz_nodes"])
    q=hy.cut_q1
    sx,sy,es=original_eightnode_native_HDF5_Q1_source_receiver(
        shape,fem.x_native_map,q,src,sw,len(fem.x_positions_m))
    rx,ry,er=original_eightnode_native_HDF5_Q1_source_receiver(
        shape,fem.x_native_map,q,rec,rw,len(fem.x_positions_m))
    if max(es,er)>1e-12 or abs(float(hy.mass.sum())-14)>2e-8:
        raise ValueError("native 8-point original map or exact physical area changed")
    lx,vx,_=generalized_symmetric_P1_consistent_modes(mx,kx)
    ly,vy,_=generalized_native_original_Q1_full_physical_neumann_modes(
        hy.mass,hy.stiffness)
    n=len(lx)*len(ly)
    if n>p["caps"]["max_modes"] or n!=prior["real_original_native_3D_full_true_Q1_modes"]:
        raise ValueError("original hybrid full 3D physical eigenmode count changed")
    lam=(lx[:,None]+ly[None,:]).ravel()
    coupling=np.outer((sx@vx)*(rx@vx),(sy@vy)*(ry@vy)).ravel()
    kick=dt**2*c**2*coupling/(1+dt**2*lam/4)
    part=partition_original_q0_hybrid_allmodes(lam,kick,dt,nt)
    reference=unpairs(prior["new_native_hybrid_true_roof_q0_full_original_250ms_signed_40_80"])
    rel=float(np.linalg.norm(part["full_signed_40_80"]-reference)/
              max(float(np.linalg.norm(reference)),1.))
    if rel>2e-8:
        raise ValueError(f"full untruncated q0 modal endpoint diagnostic does not reproduce pinned full transfer: {rel}")
    if not np.all(np.isfinite(reference)):
        raise ValueError("nonfinite prior canonical candidate q0")
    modes=[{"frequency_hz":b["band_name"],"frequency_low_hz":b["frequency_low_hz"],
            "frequency_high_hz":b["frequency_high_hz"],
            "mode_count":b["retained_mode_count"],
            "signed_40_80":pairs(b["signed_total_40_80"]),
            "endpoint_signed_40_80":{
                k:pairs(b["signed_three_endpoint_components_40_80"][:,i])
                for i,k in enumerate(ENDPOINT_NAMES)},
            "absolute_modal_contribution_sum_40_80":b["sum_modal_absolute_40_80"].tolist()}
           for b in part["bands"]]
    return {
        "ppw":ppw,"native_comms_sha":file_hash(folder/"comms_out.h5"),
        "native_room_sha":file_hash(folder/"vox_out.h5"),
        "original_native_Nt":nt,"original_native_Ts_s":dt,
        "original_native_source_strength":strength,"physical_room_volume_m3":56.,
        "all_original_Q1_positive_supported_yz_modes":q.native_yz_modes,
        "all_original_3D_modes":part["mode_count"],
        "frozen_full_signed_40_80":pairs(reference),
        "recomputed_unmodified_full_signed_40_80":pairs(part["full_signed_40_80"]),
        "relative_to_previously_pushed_full_hybrid":rel,
        "endpoint_signed_40_80":{
            k:pairs(part["endpoint_signed_40_80"][:,i])
            for i,k in enumerate(ENDPOINT_NAMES)},
        "frequency_bands_signed_all_modes":modes,
        "native_at_or_below_nyquist_mode_count":part["native_nyquist_or_below_mode_count"],
        "native_above_nyquist_mode_count":part["native_above_nyquist_mode_count"],
        "native_at_or_below_nyquist_signed_40_80":pairs(part["native_nyquist_or_below_signed_40_80"]),
        "native_above_nyquist_signed_40_80":pairs(part["native_above_nyquist_signed_40_80"]),
        "sum_abs_modal_signed_40_80":part["full_mode_sum_abs_contributions_40_80"].tolist(),
        "reconstruction_relative":{
            "bands":part["full_band_reconstruction_relative"],
            "pressure_record_endpoints":part["full_endpoint_reconstruction_relative"],
            "nyquist_diagnostic":part["full_nyquist_partition_reconstruction_relative"]},
        "all_high_modes_retained_not_accepted_without_canonical_gates":True
    }


def one_pair(coarse, fine, prior_pair):
    c=unpairs(coarse["recomputed_unmodified_full_signed_40_80"])
    f=unpairs(fine["recomputed_unmodified_full_signed_40_80"])
    now=compare_complex_transfer(
        reference=f,candidate=c,frequency_hz=[40,80],
        magnitude_mask_relative_db=-50).model_dump(mode="json")
    old=prior_pair["arms"][ARM]["original_frozen_complex_magnitude_phase_and_frequency_bins"]
    for key in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
        if abs(now[key]-old[key])>1e-8:
            raise ValueError("original hybrid full 250ms frozen acceptance score changed")
    cb=[unpairs(b["signed_40_80"]) for b in coarse["frequency_bands_signed_all_modes"]]
    fb=[unpairs(b["signed_40_80"]) for b in fine["frequency_bands_signed_all_modes"]]
    names=list(ENDPOINT_NAMES)
    ce=[unpairs(coarse["endpoint_signed_40_80"][k]) for k in names]
    fe=[unpairs(fine["endpoint_signed_40_80"][k]) for k in names]
    band_proj=signed_real_projection_on_full_delta(c,f,cb,fb)
    endpoint_proj=signed_real_projection_on_full_delta(c,f,ce,fe)
    cn=[unpairs(coarse["native_at_or_below_nyquist_signed_40_80"]),
        unpairs(coarse["native_above_nyquist_signed_40_80"])]
    fn=[unpairs(fine["native_at_or_below_nyquist_signed_40_80"]),
        unpairs(fine["native_above_nyquist_signed_40_80"])]
    ny_proj=signed_real_projection_on_full_delta(c,f,cn,fn)
    return {
        "coarse_ppw":coarse["ppw"],"fine_ppw":fine["ppw"],
        "unchanged_full_complex_magnitude_phase":now,
        "canonical_all_three_gates_pass":bool(
            now["complex_rms_relative"]<=.2
            and now["magnitude_max_relative"]<=.25
            and now["phase_max_deg"]<=15),
        "signed_full_delta_40_80":pairs(c-f),
        "by_unfiltered_disjoint_modal_bands":{
            k:np.asarray(v).tolist() for k,v in zip(BAND_TITLES,band_proj)},
        "by_native_pressure_derivative_endpoint_parts":{
            k:np.asarray(v).tolist() for k,v in zip(names,endpoint_proj)},
        "by_native_nyquist_diagnostic":{
            k:np.asarray(v).tolist()
            for k,v in zip(("at_or_below","above"),ny_proj)},
        "signed_projection_sums_to_one_for_each_bin":True,
        "no_frequency_band_or_endpoint_removed_from_original_transfer":True}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--plan",type=Path,required=True)
    parser.add_argument("--original-sims-root",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    validate_plan(json.loads(args.plan.read_text(encoding="utf8")))
    if args.plan.resolve()!=(ROOT/PLAN_PATH).resolve():
        raise ValueError("only frozen preregistered experiment plan permitted")
    require_prospective_plan_pushed()  # MUST happen before any real HDF5 computations
    raw=args.plan.read_bytes()
    plan=validate_plan(json.loads(raw.decode("utf8")))
    o=plan["original"]
    previous=json.loads((ROOT/o["hybrid_evidence"]).read_text(encoding="utf8"))
    native=json.loads((ROOT/o["native_evidence"]).read_text(encoding="utf8"))
    prior={x["ppw"]:x for x in previous["actual_native_original_point_q0_cartesian_hybrid_full_modes_cases"]}
    real={x["ppw"]:x for x in native["actual_native_wave_cases"]}
    if set(prior)!=set(PPW) or set(real)!=set(PPW):
        raise ValueError("five original real q0 native SHA+existing hybrid evidence absent")
    folders={}
    for path in args.original_sims_root.rglob("comms_out.h5"):
        hashes=[ppw for ppw in PPW if file_hash(path)==real[ppw]["original_native_comm_sha256"]]
        if len(hashes)==1:
            if hashes[0] in folders:
                raise ValueError("ambiguous duplicated original HDF5 provenance")
            folders[hashes[0]]=path.parent
    if set(folders)!=set(PPW):
        raise ValueError("all five original PFFDTD q0 SHA comms missing")
    out={"schema_version":"htdt.r130d.original-q0-hybrid-modal-endpoint-evidence-1",
         "preregistered_plan":plan,
         "preregistered_plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
         "preregistered_remote_plan_commit":PLAN_COMMIT,
         "new_native_PFFDTD_runs":0,"new_GitHub_Actions_runs":0,
         "original_PFFDTD":"SELF_CONVERGENCE_FAILED",
         "independent_physics":"NOT_VALIDATED","product":"NO_GO",
         "all_five_original_q0_modal_endpoint_cases":[],
         "all_four_original_full_250ms_signed_three_gates_and_diagnostic_deltas":[]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    original_pairs=previous["original_signed_all_four_adjacent_three_gate_results"]
    if [(q["coarse_ppw"],q["fine_ppw"]) for q in original_pairs]!=[
        (28,32),(32,36),(36,40),(40,44)]:
        raise ValueError("original 4 frozen comparator pairs changed")
    for ppw in PPW:
        data=one_case(ppw,folders[ppw],real[ppw],prior[ppw],plan)
        out["all_five_original_q0_modal_endpoint_cases"].append(data)
        args.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n",encoding="utf8")
        print("PREREGISTERED_Q0_HYBRID_ALL_MODES",ppw,
              "full",data["recomputed_unmodified_full_signed_40_80"],
              "endpoint",data["endpoint_signed_40_80"],flush=True)
    r=out["all_five_original_q0_modal_endpoint_cases"]
    for c,f,orig in zip(r,r[1:],original_pairs):
        pair=one_pair(c,f,orig)
        out["all_four_original_full_250ms_signed_three_gates_and_diagnostic_deltas"].append(pair)
        args.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n",encoding="utf8")
        print("PREREGISTERED_Q0_HYBRID_MODAL_ATTRIBUTION",
              pair["coarse_ppw"],pair["fine_ppw"],
              pair["canonical_all_three_gates_pass"],flush=True)
    out["canonical_full_original_250ms_signed_five_grid_verdict"]={
        "all_four_three_gate_pass":all(x["canonical_all_three_gates_pass"] for x
                                  in out["all_four_original_full_250ms_signed_three_gates_and_diagnostic_deltas"]),
        "no_original_solver_requalification":True}
    args.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n",encoding="utf8")


if __name__=="__main__":
    main()
