"""Negative full native-grid original point q0 cutcell FV evidence integrity.

Do not promote an experimental time+geometry replacement to canonical
PFFDTD or change the preregistered complex/phase/magnitude acceptance.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys
import math
import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_native_exact_roof_fv_q0 import (
    verify_plan,pairs,unpairs,PPW)
from htdt.r130d_general3d_validation import compare_complex_transfer

PLAN=ROOT/"benchmarks/acoustics/r130d_native_grid_exact_roof_mass_fv_q0_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_native_grid_exact_roof_mass_fv_q0_evidence_2026-10-09.json"

def frozen():
    raw=PLAN.read_bytes()
    p=verify_plan(json.loads(raw))
    d=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert d["plan_sha256_lf"]==hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest()
    assert d["preregistered_plan"]==p
    return p,d

def test_all_five_native_grid_exact_roof_wave_cases_are_present_and_original_no_go():
    p,d=frozen()
    assert d["schema_version"]=="htdt.r130d.native-exact-roof-mass-fv-q0-evidence-1"
    assert [x["ppw"] for x in d["actual_native_grid_point_impulse_exact_roof_cases"]]==list(PPW)
    assert d["canonical_original_point_q0"]=="SELF_CONVERGENCE_FAILED"
    assert d["physical_validation"]=="NOT_VALIDATED"
    assert d["product"]=="NO_GO"
    assert d["actions_launched"]==0
    assert p["limits"]["no_github_actions_runs"] is True
    assert p["comparisons"]["baseline_original_run8_10_12_selfconvergence_failed"] is True

def test_native_original_source_q0_exact_volume_spatial_operators_and_CG():
    p,d=frozen()
    earlier=json.loads((ROOT/p["native_original"]["original_controls"]).read_text(encoding="utf-8"))
    frozen_native_wave=json.loads((ROOT/"benchmarks/acoustics/r130d_original_native_sparse_fullstate_evidence_2026-10-09.json").read_text(encoding="utf-8"))
    for case,source,original_raw in zip(d["actual_native_grid_point_impulse_exact_roof_cases"],
                           earlier["actual_native_wave_cases"],
                           frozen_native_wave["native_full_state_comparisons"]):
        assert case["ppw"]==source["ppw"]
        assert case["original_native_comms_sha256"]==source["original_native_comm_sha256"]
        assert case["original_native_geometry_sha256"]==source["original_solver_geometry_sha256"]
        assert case["ppw"]==original_raw["ppw"]
        assert case["original_native_raw_wave_sha256"]==original_raw["original_exact_raw_native_wave_sha256"]
        # Original full 8-node raw HDF5 is pinned; Q2 experimental
        # numerical operator HDF5 is deliberately NOT the original control.
        assert case["original_native_raw_wave_sha256"]!=source["native_solver"]["sim_outs_sha256"]
        assert case["original_8node_full_signed_40_80"]==source["unmodified_original_transfer_pa_per_m3_s"]
        assert case["original_source_receiver_and_discrete_q0_unchanged"] is True
        assert case["original_pffdtd_engine_code_untouched"] is True
        assert case["original_full_wave_unchanged"] is True
        assert case["experimental_numerical_time_scheme"].startswith("Newmark beta1/4")
        assert case["experimental_numerical_spatial_scheme"].startswith("exact dual cut")
        assert case["exact_physical_room_volume_m3"]==pytest.approx(56,abs=p["algorithm"]["fluid_volume_tolerance_m3"])
        assert case["original_nodal_active_cutcell_count"]<=p["limits"]["max_total_nodes"]
        assert 0<case["cutcell_minimum_original_full_voxel_volume_fraction"]<1
        assert case["native_exact_roof_stiffness_nonzeros"]>case["original_nodal_active_cutcell_count"]
        assert np.asarray(case["experimental_signed_P_T_over_Q_T_40_80"],dtype=float).shape==(2,2)
        assert np.all(np.isfinite(case["experimental_signed_P_T_over_Q_T_40_80"]))
        assert case["original_record_samples"]==source["native_solver"]["sample_count"]
        solver=case["solver"]
        assert solver["maximum_CG_iterations"]<=p["algorithm"]["max_CG_iterations"]
        assert solver["maximum_true_linear_relative_residual"]<=p["algorithm"]["max_true_residual_rel"]
        assert solver["relative_energy_drift_after_source"]<=p["algorithm"]["energy_drift_max"]
        assert solver["original_discrete_q0_unchanged"] is True
        assert solver["no_point_source_smoothing_or_taper"] is True
        assert solver["linear_solve_count"]==case["original_record_samples"]
        assert len(solver["newmark_midpoint_homogeneous_energy_probes"])==4
        assert case["native_exact_roof_actual_integration_wall_seconds"]>0

def test_retains_failures_at_ppw32_36_and_40_44_without_silent_acceptance_changes():
    p,d=frozen()
    gate=d["new_scheme_adjacent_original_q0_point_transfers"]
    assert gate["new_scheme_changes_numerical_spatial_AND_temporal_solver"] is True
    assert gate["original_8_10_12_PFFDTD_approval_still_FAILED"] is True
    assert gate["cross_solver_requalification_not_possible"] is True
    assert gate["experimental_provisional_numerical_refinement_pass"] is False
    assert gate["all_three_errors_strictly_decreasing"] is False
    assert gate["experimental_last_pair_meets_frozen_thresholds"] is False
    assert gate["each_pair_below_original_numeric_limits"]==[True,False,True,False]
    levels=d["actual_native_grid_point_impulse_exact_roof_cases"]
    frozen_pairs=gate["signed_all_four_adjacent_pairs"]
    for i,(coarse,fine) in enumerate(zip(levels,levels[1:])):
        row=frozen_pairs[i]
        actual=compare_complex_transfer(
            reference=fine["experimental_signed_P_T_over_Q_T_40_80"],
            candidate=coarse["experimental_signed_P_T_over_Q_T_40_80"],
            frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
        assert row["coarse_ppw"]==coarse["ppw"]
        assert row["fine_ppw"]==fine["ppw"]
        for k in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
            assert actual[k]==pytest.approx(row[k],rel=5e-12,abs=5e-12)
        assert row["compared_frequency_count"]==2
    assert frozen_pairs[3]["complex_rms_relative"]>.8
    assert frozen_pairs[3]["magnitude_max_relative"]>3
    assert frozen_pairs[3]["phase_max_deg"]>170-1
