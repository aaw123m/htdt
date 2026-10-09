"""Frozen original physical point q0 finite record equal-phase numerical A/B."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys
import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_exact_roof_q0_three_phase_ensemble import (
    PPW,PHASES,verify_plan,unpairs,score)

PLAN=ROOT/"benchmarks/acoustics/r130d_exact_roof_q0_three_phase_numerical_ensemble_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_exact_roof_q0_three_phase_ensemble_evidence_2026-10-09.json"
def frozen():
    raw=PLAN.read_bytes()
    p=verify_plan(json.loads(raw.decode("utf-8")))
    d=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert d["plan_sha256_lf"]==hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest()
    assert d["preregistered_plan"]==p
    return p,d

def test_original_point_q0_and_three_phases_frozen_and_original_NO_GO():
    p,d=frozen()
    assert p["frozen_inputs"]["PPW"]==list(PPW)==[40,44]
    assert p["frozen_inputs"]["x_shift_fraction_of_own_native_h"]==list(PHASES)
    assert p["frozen_inputs"]["physical_source_xyz_m"]==[1.5,2,2]
    assert p["frozen_inputs"]["physical_receiver_xyz_m"]==[2.5,2,2]
    assert p["frozen_inputs"]["record_s"]==.25
    assert p["frozen_inputs"]["frequencies_hz"]==[40,80]
    assert p["estimator"]["weights"]==[1/3]*3
    assert p["limits"]["max_new_wave_runs"]==d["new_full_wave_runs"]==0
    assert p["limits"]["no_github_actions_runs"] is True
    assert d["new_github_actions_runs"]==0
    assert d["original_canonical_PFFDTD_point_q0"]=="SELF_CONVERGENCE_FAILED"
    assert d["physical_validation"]=="NOT_VALIDATED"
    assert d["product"]=="NO_GO"

@pytest.mark.parametrize("change",[
    lambda p:p["frozen_inputs"].update(PPW=[40]),
    lambda p:p["frozen_inputs"].update(x_shift_fraction_of_own_native_h=[0]),
    lambda p:p["frozen_inputs"].update(physical_source_xyz_m=[1.51,2,2]),
    lambda p:p["frozen_inputs"].update(frequencies_hz=[40]),
    lambda p:p["estimator"].update(weights=[.5,.25,.25]),
    lambda p:p["evaluation"].update(compare_frozen_original_threshold_complex=2),
    lambda p:p["authority"].update(product="GO"),
    lambda p:p["limits"].update(no_github_actions_runs=False)
])
def test_post_observation_fitting_or_physics_changes_fail_closed(change):
    p,_=frozen()
    change(p)
    with pytest.raises(ValueError):verify_plan(p)

def test_original_real_shifts_all_six_spectra_and_signed_complex_mean_exact():
    p,d=frozen()
    shift=json.loads((ROOT/p["frozen_inputs"]["actual_x_grid_shifted_full_native_wave"]).read_text(encoding="utf-8"))
    base=json.loads((ROOT/p["frozen_inputs"]["exact_roof_native_grid_unshifted_original"]).read_text(encoding="utf-8"))
    ctrl={q["ppw"]:q for q in base["actual_native_grid_point_impulse_exact_roof_cases"]}
    observed={(q["ppw"],q["native_grid_x_shift_fraction_h"]):q for q in shift["actual_shifted_x_grid_full_original_q0_wave_cases"]}
    rows=d["actual_unmodified_native_q0_phase_ensemble"]
    assert [r["ppw"] for r in rows]==list(PPW)
    for r in rows:
        ppw=r["ppw"]
        assert [x["x_phase_h"] for x in r["three_actual_native_fullwave_phase_signed_transfers"]]==list(PHASES)
        complex_raw=[]
        for k in r["three_actual_native_fullwave_phase_signed_transfers"]:
            phase=k["x_phase_h"]
            if phase==0:
                actual=ctrl[ppw]["experimental_signed_P_T_over_Q_T_40_80"]
            else:
                actual=observed[(ppw,phase)]["new_actual_unmodified_physical_point_q0_signed_40_80"]
            assert k["signed_native_40_80"]==actual
            complex_raw.append(unpairs(actual))
        np.testing.assert_allclose(
            np.mean(complex_raw,axis=0),unpairs(r["original_q0_equal_weight_three_x_phase_signed_ensemble"]),
            rtol=2e-12,atol=2e-8)
        assert r["original_physical_source_receiver_fixed"] is True
        assert r["original_temporal_q0_and_full_250ms_bins_fixed"] is True
        assert r["no_original_native_PFFDTD_fullwave_modification"] is True
        assert r["no_new_wave_computation"] is True

def test_original_three_phase_ensemble_unfavorable_40_and80_hz_metrics_preserved():
    p,d=frozen()
    rows=d["actual_unmodified_native_q0_phase_ensemble"]
    candidate=score(unpairs(rows[0]["original_q0_equal_weight_three_x_phase_signed_ensemble"]),
                    unpairs(rows[1]["original_q0_equal_weight_three_x_phase_signed_ensemble"]))
    actual=d["equal_weight_three_phase_fullwave_PP40_44"]
    for metric in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
        assert candidate[metric]==pytest.approx(actual[metric],abs=5e-11,rel=5e-11)
    assert actual["compared_frequency_count"]==2
    assert actual["complex_rms_relative"]>.5
    assert actual["magnitude_max_relative"]>8
    assert actual["phase_max_deg"]>130
    assert d["equal_weight_three_phase_pair_below_frozen_numerical_limits"] is False
    assert d["one_experimental_pair_not_full_grid_refinement_qualification"] is True
