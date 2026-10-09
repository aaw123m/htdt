#!/usr/bin/env python3
"""Precommitted amplitude/units erratum + NEW causal witness widths.

This runner deliberately keeps the FIRST, ALREADY OBSERVED, INVALID
c²-multiplied analytic results immutable in their original evidence JSON.
The physics correction derives from [ (1/c²) ∂tt − Δ ] G = δ:
the native q0 sum c² dt²/h³ produces φ≈dt*G for q_n=1 native pulse.
Thus after dividing by Q_T=dt the expected pressure weak functional
is -rho * sum s_i r_j w'(rij/c)/(4πrij), without a fitted scale.
Original entire 250ms signed 40/80 score remains a separate FAIL.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_point_quadratic_pffdtd import PPW,PIN,file_hash
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs
from run_r130d_original_q0_causal_prefirst_weak import one_case
from htdt.r130d_causal_prefirst_weak import TAU,HALF_SUPPORT_S
from htdt.r130d_retarded_point_green import C,RHO

SCHEMA="htdt.r130d.original-q0-causal-prefirst-green-amplitude-correction-plan-1"
NEW_WIDTHS=(.00065,.001,.0015)


def validate_plan(p):
    pre=p["original_preobserved_first_experiment"]
    theory=p["theoretical_correction"]
    n=p["fixed_experiment"]
    checks=p["tests"]
    caps=p["limits"]
    if (p.get("schema_version")!=SCHEMA or p["issue"]!=938 or p["pr"]!=1055
        or pre["preregistered_commit"]!="0c405ce1233d060d343756a4c9771c958ec02a99"
        or pre["already_computed_widths_s"]!=[.0005,.0008,.0012]
        or not pre["already_observed_old_normalized_per_grid_values"]
        or not pre["old_analytic_absolute_comparison_INVALID"]
        or not theory["no_fitted_amplitude"]
        or not theory["not_a_native_solver_kernel_change"]
        or not np.isclose(1/(C*C),1/(343.2**2),rtol=0,atol=0)
        or n["new_not_previously_computed_widths_s"]!=list(NEW_WIDTHS)
        or not n["previous_widths_must_be_treated_as_exploratory"]
        or n["original_source_m"]!=[1.5,2,2]
        or n["original_receiver_m"]!=[2.5,2,2]
        or n["PPW"]!=list(PPW)
        or not n["native_original_q0_HDF5_8node_weights_and_indices_unchanged"]
        or not n["original_full250ms_time_all_modes_preserved"]
        or n["physical_witness_center_s"]!=TAU
        or n["physical_compact_support_radius_s"]!=HALF_SUPPORT_S
        or not n["original_sampled_forward_center_backward_pressure_unchanged"]
        or n["original_rho"]!=RHO or n["original_c_m_s"]!=C
        or [v for v in n["original_complete_signed_40_80_frozen_three_gates"]]!=[.2,.25,15]
        or not n["all_four_pairs_original_still_scored"]
        or not n["all_previous_lowband_and_highband_original_full_time_metrics_kept"]
        or not all(checks.values())
        or caps!={"new_original_pffdtd_wave_runs":0,
                  "new_github_actions_runs":0,"preserve_scratch":True,
                  "max_cases":5,"max_new_witness_widths":3}
        or p["release"]["original_PFFDTD"]!="SELF_CONVERGENCE_FAILED"
        or p["release"]["physical_reference"]!="NOT_VALIDATED"
        or p["release"]["product"]!="NO_GO"):
        raise ValueError("preregistered physical amplitude Green erratum or new width drift")
    return p


def main():
    a=argparse.ArgumentParser()
    a.add_argument("--plan",type=Path,required=True)
    a.add_argument("--original-sims-root",type=Path,required=True)
    a.add_argument("--output",type=Path,required=True)
    args=a.parse_args()
    raw=args.plan.read_bytes()
    p=validate_plan(json.loads(raw.decode("utf-8")))
    n=p["fixed_experiment"]
    original=json.loads((ROOT/n["original_real_HDF5_sha_and_fullwave"]).read_text(encoding="utf-8"))
    modal=json.loads((ROOT/n["previous_exact_all_original_modal_waves"]).read_text(encoding="utf-8"))
    green=json.loads((ROOT/n["prior_true_single_point_green"]).read_text(encoding="utf-8"))
    legacy_file=ROOT/p["original_preobserved_first_experiment"]["first_evidence_keep_filename"]
    legacy_raw=legacy_file.read_bytes()
    legacy=json.loads(legacy_raw.decode("utf-8"))
    if (legacy["original_authority"]!="SELF_CONVERGENCE_FAILED"
        or [z["ppw"] for z in legacy["actual_original_five_grid_pre_first_echo_weak_distribution_cases"]]!=list(PPW)):
        raise ValueError("original already observed invalid analytic c² reference missing; cannot silently rewrite")
    source={x["ppw"]:x for x in original["actual_native_wave_cases"]}
    old={x["ppw"]:x for x in modal["actual_original_unmodified_all_mode_native_cases"]}
    first={x["ppw"]:x for x in green["actual_original_8node_HDF5_5grid_analytic_direct_point_Green_cases"]}
    if set(source)!=set(PPW) or set(old)!=set(PPW) or set(first)!=set(PPW):
        raise ValueError("all five original SHA-pinned source, full modes and full physical Green archives needed")
    dirs={}
    for f in args.original_sims_root.rglob("comms_out.h5"):
        h=file_hash(f)
        m=[k for k in PPW if source[k]["original_native_comm_sha256"]==h]
        if len(m)==1:
            if m[0] in dirs:raise ValueError("ambiguous original SHA HDF5")
            dirs[m[0]]=f.parent
    if set(dirs)!=set(PPW):
        raise ValueError("all original native SHA 8node q0 waves required")
    e={"schema_version":"htdt.r130d.original-q0-causal-prefirst-corrected-Green-evidence-1",
       "preobserved_original_wrong_analytic_evidence_SHA256":hashlib.sha256(legacy_raw).hexdigest(),
       "preobserved_wrong_analytic_absolute_comparison_INVALID":True,
       "prior_preobserved_three_widths_s":[.0005,.0008,.0012],
       "new_prospectively_registered_three_widths_s":list(NEW_WIDTHS),
       "preregistered_plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
       "preregistered_plan":p,
       "continuum_operator_injection_normalization":"[(1/c²)∂tt−Δ]G=δ; native source sums c²dt²/h³; physical expected normalized witness = −ρ Σ64 s_i r_j w_prime(rij/c)/(4πrij); NO c²",
       "corrected_divisor_of_previous_analytic_amplitude":float(C*C),
       "fitted_numerical_gain":None,
       "actual_new_weak_results_original_SHA_native_5grid":[],
       "original_canonical_full250ms_all_adjacent":[],"authority_original_PFFDTD_q0":"SELF_CONVERGENCE_FAILED",
       "independent_physical":"NOT_VALIDATED","product":"NO_GO",
       "new_upstream_PFFDTD_wave_runs":0,"new_GitHub_Actions_runs":0}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    for ppw in PPW:
        try:z=one_case(p,ppw,dirs[ppw],source[ppw],old[ppw],first[ppw],
                       widths_s=NEW_WIDTHS,
                       continuum_analytic_correction_factor=1/(C*C))
        except Exception as exc:
            e["actual_new_weak_results_original_SHA_native_5grid"].append({
                "ppw":ppw,"status":"NEW_PHYSICAL_SOURCE_NORMALIZATION_FAILED",
                "error":str(exc),"type":type(exc).__name__})
            args.output.write_text(json.dumps(e,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            raise
        e["actual_new_weak_results_original_SHA_native_5grid"].append(z)
        args.output.write_text(json.dumps(e,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("ORIGINAL_Q0_NEW_HOLDOUT_WEAK_CORRECTED_GREEN",ppw,
              "new widths",[w["physical_weak_test_width_s"] for w in z["three_predeclared_compact_weak_distribution_tests"]],
              "native_over_analytic_correct_unit",[round(w["actual_native_over_assumed_continuum_source_model_signed_ratio"],6) for w in z["three_predeclared_compact_weak_distribution_tests"]],
              flush=True)
    cases=e["actual_new_weak_results_original_SHA_native_5grid"]
    for co,fi in zip(cases,cases[1:]):
        s=compare_complex_transfer(
            reference=fi["unchanged_original_full_250ms_signed_40_80_recomputed"],
            candidate=co["unchanged_original_full_250ms_signed_40_80_recomputed"],
            frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
        e["original_canonical_full250ms_all_adjacent"].append({
            "coarse_ppw":co["ppw"],"fine_ppw":fi["ppw"],
            "original_unchanged_three_gate_signed_full250ms":s,
            "original_full250ms_pass":bool(s["complex_rms_relative"]<=.2 and
                                            s["magnitude_max_relative"]<=.25 and
                                            s["phase_max_deg"]<=15),
            "auxiliary_correctly_normalized_weak_diagnostic_not_original_release":True})
    e["original_full250ms_all_ppw_pass"]=all(z["original_full250ms_pass"] for z in e["original_canonical_full250ms_all_adjacent"])
    e["original_upstream_nonconvergence_unchanged"]=True
    args.output.write_text(json.dumps(e,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("NATIVE_FULL250MS_ORIGINAL_Q0_STILL_FAILED",not e["original_full250ms_all_ppw_pass"],
          "ALL_FROZEN_SCORES",[round(z["original_unchanged_three_gate_signed_full250ms"]["complex_rms_relative"],6) for z in e["original_canonical_full250ms_all_adjacent"]],flush=True)

if __name__=="__main__": main()
