"""Precommitted independent full-state Newmark replay of actual corrected FV."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_native_exact_roof_fv_q0 import unpairs
from run_r130d_exact_roof_q0_kmk_direct_wave_replay import validate_plan

PLAN=ROOT/"benchmarks/acoustics/r130d_exact_roof_q0_kmk_direct_wave_replay_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_exact_roof_q0_kmk_direct_wave_replay_evidence_2026-10-09.json"
MODAL=ROOT/"benchmarks/acoustics/r130d_exact_roof_q0_kmk_dispersion_evidence_2026-10-09.json"


def test_independent_full_wave_plan_unchanged():
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    assert validate_plan(p)==p
    assert p["frozen"]["ppw"]==[40,44]
    assert p["frozen"]["source_xyz_m"]==[1.5,2,2]
    assert p["frozen"]["receiver_xyz_m"]==[2.5,2,2]
    assert p["frozen"]["record_s"]==.25
    assert p["frozen"]["scored_frequency_hz"]==[40,80]
    assert p["limits"]["max_additional_original_native_PFFDTD_waves"]==0
    assert p["limits"]["new_github_actions_runs"]==0


@pytest.mark.parametrize("key,value",[
    ("compare_independent_spectral_exact_all_modes_relative_complex_max",.25),
    ("cg_rtol",1e-3),
    ("max_energy_drift",1e-1)])
def test_mutated_numeric_consistency_gate_rejected(key,value):
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    p["numerical"][key]=value
    with pytest.raises(ValueError,match="plan altered"):
        validate_plan(p)


def test_true_two_original_native_grid_full_wave_signed_frozen_evidence():
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    e=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    prior=json.loads(MODAL.read_text(encoding="utf-8"))
    assert e["preregistered_plan"]==p
    assert e["plan_sha256_lf"]==hashlib.sha256(PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert e["original_PFFDTD_q0"]=="SELF_CONVERGENCE_FAILED"
    assert e["physical_validation"]=="NOT_VALIDATED"
    assert e["product"]=="NO_GO"
    assert e["new_PFFDTD_wave_runs"]==e["new_Actions_runs"]==0
    cases=e["actual_true_corrected_KmkK_fv_direct_wave_cases"]
    assert [x["ppw"] for x in cases]==[40,44]
    previous={x["ppw"]:x for x in prior["actual_full_mode_exact_roof_kmk_cases"]}
    for case in cases:
        ppw=case["ppw"]
        assert case["original_physical_source_receiver_q0_unmodified"]
        assert case["true_active_wave_dofs"]==previous[ppw]["all_true_3D_native_roof_modes_count"]
        assert abs(case["exact_roof_volume_m3"]-56)<2e-8
        assert case["native_samples"]==previous[ppw]["native_nt"]
        assert case["native_timestep_s"]==previous[ppw]["native_dt_s"]
        assert case["actual_full_state_vs_independent_modal_complex_relative"]<2e-5
        signed=unpairs(case["true_new_full_state_250ms_pressure_signed_P_over_Q_40_80"])
        modal=unpairs(case["previous_independent_ALL_mode_signed_40_80"])
        assert np.allclose(signed,modal,rtol=2e-5,atol=1e-3)
        s=case["CG_solver_and_energy_diagnostics"]
        assert s["maximum_CG_iterations"]<=500
        assert s["maximum_true_linear_relative_residual"]<=5e-10
        assert s["relative_energy_drift_after_source"]<=1e-6
        assert s["original_discrete_q0_unchanged"]
        assert s["no_point_source_smoothing_or_taper"]
    assert e["experimental_wave_not_original_pffdtd_qualification"]
    from_data=compare_complex_transfer(
        reference=cases[1]["true_new_full_state_250ms_pressure_signed_P_over_Q_40_80"],
        candidate=cases[0]["true_new_full_state_250ms_pressure_signed_P_over_Q_40_80"],
        frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
    actual=e["actual_corrected_full_state_PP40_44_original_three_gate_metrics"]
    for key in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
        assert abs(from_data[key]-actual[key])<1e-11
    frozen_gate=(actual["complex_rms_relative"]<=.2 and
                 actual["magnitude_max_relative"]<=.25 and
                 actual["phase_max_deg"]<=15)
    assert frozen_gate is e["full_state_pair_passes_frozen_original_gate"]
    assert not e["full_state_pair_passes_frozen_original_gate"]
