#!/usr/bin/env python3
"""Fixed five-arm original-q0 conservative exact-roof heldout PPW28/32/36."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
from htdt.r130d_tensor_directional_dispersion import ARMS
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_point_quadratic_pffdtd import file_hash,PIN
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs
from run_r130d_exact_roof_q0_tensor_directional_dispersion import (
    one_grid,validate_plan as check_original_two_grid)


def validate_plan(p):
    pre=p["status_of_preexisting_observations"]
    frozen=p["frozen"]
    n=p["numerics"]
    e=p["evaluation"]
    if (p["schema_version"]!="htdt.r130d.exact-roof-directional-q0-heldout-multigrid-plan-1"
        or p["issue"]!=938 or p["pr"]!=1055
        or pre["new_heldout_ppw"]!=[28,32,36]
        or pre["entire_refinement_grid"]!=[28,32,36,40,44]
        or pre["all_arms"]!=list(ARMS)
        or not pre["ppw40_and_44_were_observed_before_this_plan"]
        or not pre["holdout_grid_outcomes_not_observed_before_plan"]
        or frozen["upstream_sha"]!=PIN
        or frozen["physical_source_xyz"]!=[1.5,2,2]
        or frozen["physical_receiver_xyz"]!=[2.5,2,2]
        or not frozen["original_eight_node_q0_from_raw_hdf5"]
        or not frozen["native_original_Ts_Nt"]
        or frozen["roof_exact_volume_m3"]!=56
        or frozen["record_s"]!=.25 or frozen["frequency_hz"]!=[40,80]
        or frozen["sound_speed_m_s"]!=343.2 or frozen["rho"]!=1.2
        or n["alpha_fraction"]!=1/12
        or not n["use_exact_unmodified_original_point_coupling"]
        or not n["use_previous_prospectively_frozen_five_operators_without_changing_alpha"]
        or not n["all_native_eigenmodes_for_each_grid"]
        or not n["actual_true_3d_sparse_neumann_operator_validation"]
        or not n["no_truncation_damping_smoothing_taper_or_mask"]
        or n["archived_true_CG_baseline_reconstruction_relative_limit"]!=2e-5
        or n["original_previous_40_44_transfers_match_relative_limit"]!=2e-8
        or [e[k] for k in ("original_complex_limit","original_magnitude_limit",
             "original_phase_limit_deg") ]!=[.2,.25,15]
        or not e["report_all_four_pairs_for_each_of_five_arms"]
        or not e["monotonic_complex_magnitude_phase_required_for_refinement"]
        or not e["save_all_unfavorable_raw_values"]
        or not e["heldout_not_original_pffdtd_or_bras_qualification"]
        or p["limits"]!={"new_heldout_cases":3,"new_original_pffdtd_wave_runs":0,
                         "new_GitHub_Actions_runs":0,"clean_scratch":False}
        or p["authority"]["original_PFFDTD_q0"]!="SELF_CONVERGENCE_FAILED"
        or p["authority"]["independent_physical"]!="NOT_VALIDATED"
        or p["authority"]["product"]!="NO_GO"):
        raise ValueError("heldout preregistered original point-q0 plan changed")
    return p


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args()
    raw=args.plan.read_bytes()
    p=validate_plan(json.loads(raw.decode("utf-8")))
    st=p["status_of_preexisting_observations"]
    fr=p["frozen"]
    prev=json.loads((ROOT/st["previous_two_grid_evidence"]).read_text(encoding="utf-8"))
    initial_plan=check_original_two_grid(prev["preregistered_plan"])
    if (prev["original_PFFDTD_q0"]!="SELF_CONVERGENCE_FAILED"
        or prev["product"]!="NO_GO"
        or [r["ppw"] for r in prev["true_fullmode_original_native_cases"]]!=[40,44]):
        raise ValueError("original known pre-heldout 40/44 q0 negative scores missing")
    previous={x["ppw"]:x for x in prev["true_fullmode_original_native_cases"]}
    native=json.loads((ROOT/fr["original_native_full_sha_evidence"]).read_text(encoding="utf-8"))
    baseline=json.loads((ROOT/fr["archived_true_baseline_all5"]).read_text(encoding="utf-8"))
    original={x["ppw"]:x for x in native["actual_native_wave_cases"]}
    controls={x["ppw"]:x for x in baseline["actual_native_grid_point_impulse_exact_roof_cases"]}
    if set(original)!={28,32,36,40,44} or set(controls)!={28,32,36,40,44}:
        raise ValueError("all original real five-grid HDF5 and exact-roof CG archives required")
    dirs={}
    for f in args.original_sims_root.rglob("comms_out.h5"):
        checksum=file_hash(f)
        found=[i for i in (28,32,36) if checksum==original[i]["original_native_comm_sha256"]]
        if len(found)==1:
            if found[0] in dirs: raise ValueError("duplicate originally frozen HDF5 comms")
            dirs[found[0]]=f.parent
    if set(dirs)!={28,32,36}:
        raise ValueError("heldout original native SHA-locked raw q0 simulation assets missing")
    output={"schema_version":"htdt.r130d.exact-roof-directional-q0-heldout-multigrid-evidence-1",
        "pre_observation_plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":p,
        "original_PFFDTD_q0":"SELF_CONVERGENCE_FAILED",
        "physical_validation":"NOT_VALIDATED","product":"NO_GO",
        "new_upstream_pffdtd_wave_runs":0,"new_github_actions_runs":0,
        "newly_calculated_heldout_cases":[],"previously_saved_PP40_PP44_cases":[],"all_five_grid_adjacent":[]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    for ppw in (28,32,36):
        saved=controls[ppw]
        adapted={"original_native_dt_s":saved["original_native_dt_s"],
            "original_record_nt":saved["original_record_samples"],
            "total_full_untruncated_3D_native_modes":saved["original_nodal_active_cutcell_count"],
            "archived_direct_full_wave_250ms_signed_40_80":saved["experimental_signed_P_T_over_Q_T_40_80"]}
        try:result=one_grid(initial_plan,ppw,dirs[ppw],original[ppw],adapted)
        except Exception as exc:
            output["newly_calculated_heldout_cases"].append({
                "ppw":ppw,"status":"HELDOUT_ALL_MODE_NUMERICAL_FAILED",
                "error_type":type(exc).__name__,"error_message":str(exc)})
            args.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        if result["full_kmk_arm_vs_prior_KmkK_full_mode_relative"] is not None:
            raise ValueError("previous new KmkK PPW28/32/36 incorrectly claimed to exist")
        output["newly_calculated_heldout_cases"].append(result)
        args.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("HELDOUT_NATIVE_Q0_KMK_ALL_ARMS",ppw,
              "ALL 3D modes",result["true_all_eigenmodes_count"],
              "prior true CG baseline relative",
              result["native_newmark_base_vs_prior_true_original_CG_wave_relative"],flush=True)
    output["previously_saved_PP40_PP44_cases"]=list(previous.values())
    cases=output["newly_calculated_heldout_cases"]+output["previously_saved_PP40_PP44_cases"]
    if [q["ppw"] for q in cases]!=[28,32,36,40,44]:
        raise ValueError("actual native five-grid order inconsistent")
    for coarse,fine in zip(cases,cases[1:]):
        results={}
        for arm in ARMS:
            c=coarse["every_full_mode_250ms_signed_transfer_40_80"][arm]
            f=fine["every_full_mode_250ms_signed_transfer_40_80"][arm]
            score=compare_complex_transfer(reference=f,candidate=c,
                frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
            results[arm]={
                "original_full_PP_coarse_minus_fine_signed_40_80":pairs(unpairs(c)-unpairs(f)),
                "original_unchanged_three_gate_metrics":score,
                "passes_all_original_limits":bool(score["complex_rms_relative"]<=.2
                    and score["magnitude_max_relative"]<=.25
                    and score["phase_max_deg"]<=15)}
        output["all_five_grid_adjacent"].append({
            "coarse_ppw":coarse["ppw"],"fine_ppw":fine["ppw"],"arms":results})
    arm_assessment={}
    for arm in ARMS:
        points=[pair["arms"][arm] for pair in output["all_five_grid_adjacent"]]
        cols=["complex_rms_relative","magnitude_max_relative","phase_max_deg"]
        mono=all(all(points[j]["original_unchanged_three_gate_metrics"][k]<
                    points[j-1]["original_unchanged_three_gate_metrics"][k]
                    for k in cols) for j in range(1,4))
        arm_assessment[arm]={
            "all_four_original_adjacent_pairs_below_limits":all(z["passes_all_original_limits"] for z in points),
            "original_complex_magnitude_phase_all_strictly_decrease":mono,
            "any_pair_that_fails":[[output["all_five_grid_adjacent"][j]["coarse_ppw"],
                                    output["all_five_grid_adjacent"][j]["fine_ppw"]]
                                     for j,z in enumerate(points) if not z["passes_all_original_limits"]],
            "original_canonical_not_requalified":True}
    output["heldout_refinement_all_arm_verdicts"]=arm_assessment
    output["not_original_PFFDTD_canonical_or_independent_physics"]=True
    args.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    for pair in output["all_five_grid_adjacent"]:
        print("HELDOUT_ORIGINAL_Q0_PAIR",pair["coarse_ppw"],pair["fine_ppw"],
              {arm:round(pair["arms"][arm]["original_unchanged_three_gate_metrics"]
                  ["complex_rms_relative"],6) for arm in ARMS},flush=True)
    print("HELDOUT_ALL_FIVE_ARM_OUTCOMES",
          {arm:[v["all_four_original_adjacent_pairs_below_limits"],
                v["original_complex_magnitude_phase_all_strictly_decrease"]]
           for arm,v in arm_assessment.items()},"PRODUCT_NO_GO",flush=True)
if __name__=="__main__":
    main()
