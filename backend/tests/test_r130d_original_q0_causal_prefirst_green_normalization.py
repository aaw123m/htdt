"""R130D real original native q0 causal weak Green source normalization erratum.

Old invalid analytic c² amplitude is preserved (previous widths observed);
new physical widths were preregistered before obtaining new results. The
original complete full 250ms signed 40/80Hz q0 verdict remains FAIL.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys
import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from htdt.r130d_causal_prefirst_weak import (
    TAU,HALF_SUPPORT_S,WIDTHS_S,compact_odd_witness,
    original_native_64node_analytic_weak_reference)
from htdt.r130d_retarded_point_green import C,RHO
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_q0_causal_prefirst_green_normalization import (
    NEW_WIDTHS,validate_plan)
from run_r130d_original_point_quadratic_pffdtd import PPW
from run_r130d_native_exact_roof_fv_q0 import unpairs

PLAN=ROOT/"benchmarks/acoustics/r130d_original_q0_causal_prefirst_green_normalization_erratum_plan_2026-10-09.json"
OLD=ROOT/"benchmarks/acoustics/r130d_original_q0_causal_prefirst_weak_evidence_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_q0_causal_prefirst_green_normalization_evidence_2026-10-09.json"
ORIGINAL=ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json"


@pytest.mark.parametrize("sigma",NEW_WIDTHS)
def test_exact_C_infinity_witness_derivative_and_compact_support(sigma):
    x=np.array([TAU-1.1*HALF_SUPPORT_S,TAU-HALF_SUPPORT_S,
                TAU-.00025,TAU,TAU+.00035,
                TAU+HALF_SUPPORT_S,TAU+1.1*HALF_SUPPORT_S])
    w=compact_odd_witness(x,sigma)
    dw=compact_odd_witness(x,sigma,derivative=True)
    assert np.all(w[[0,1,5,6]]==0)
    assert np.all(dw[[0,1,5,6]]==0)
    assert w[3]==0
    assert abs(dw[3]-1/sigma)<1e-9
    eps=3e-9
    for t in x[2:5]:
        center=np.array([t])
        a=(compact_odd_witness(center+eps,sigma)[0]-
           compact_odd_witness(center-eps,sigma)[0])/(2*eps)
        b=compact_odd_witness(center,sigma,derivative=True)[0]
        assert abs(a-b)/max(abs(b),1.)<2e-8
    assert TAU+HALF_SUPPORT_S < .0089668767033651


def test_prospective_new_physical_widths_and_original_frozen_gates():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    assert p["original_preobserved_first_experiment"]["already_computed_widths_s"]==list(WIDTHS_S)
    assert p["fixed_experiment"]["new_not_previously_computed_widths_s"]==list(NEW_WIDTHS)
    assert set(NEW_WIDTHS).isdisjoint(WIDTHS_S)
    assert p["theoretical_correction"]["no_fitted_amplitude"]
    assert p["fixed_experiment"]["original_complete_signed_40_80_frozen_three_gates"]==[.2,.25,15]
    assert p["limits"]["new_original_pffdtd_wave_runs"]==0


@pytest.mark.parametrize("key,val",[
    ("physical_witness_center_s",.002),
    ("physical_compact_support_radius_s",.004),
    ("original_rho",2.)])
def test_fail_closed_original_causal_green_physical_parameters(key,val):
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    p["fixed_experiment"][key]=val
    with pytest.raises(ValueError,match="preregistered physical amplitude Green erratum"):
        validate_plan(p)


def test_corrected_continuum_green_pressure_follows_wave_PDE_source_units_no_c2_fit():
    # Fundamental solution for [(1/c²) d²/dt² - Δ] phi = delta(x) q(t)
    # is G(t)=delta(t-r/c)/(4πr). Discrete native source input
    # sum(in_sigs)=c²*dt²/h³ is a one-sample approximation of q(t) with
    # time integral dt. Normalized receiver pressure weak functional is:
    # -rho * w'(r/c)/(4πr). There is NO factor c².
    point=np.array([1.5,2.,2.])
    receiver=np.array([2.5,2.,2.])
    for sigma in NEW_WIDTHS:
        analytic=-RHO/(4*np.pi)*compact_odd_witness(
            np.array([1/C]),sigma,derivative=True)[0]
        old_wrong=-RHO*C*C/(4*np.pi)*compact_odd_witness(
            np.array([1/C]),sigma,derivative=True)[0]
        assert abs(analytic+RHO/(4*np.pi*sigma))<1e-10
        assert abs(old_wrong/analytic-C*C)<2e-11
        # Independent 8x8 control with all 8 coordinates equal for both
        # distinct physical points, no grid-specific wave numerical fit.
        src=np.repeat(point[None,:],8,axis=0)
        rec=np.repeat(receiver[None,:],8,axis=0)
        weights=np.ones(8)/8
        full=original_native_64node_analytic_weak_reference(
            src,weights,rec,weights,width_s=sigma)
        assert abs(full[
            "original_64_native_pairs_continuum_causal_green_weak_distribution"]/(C*C)-analytic)<1e-8
        assert full["earliest_real_original_64node_single_wall_reflection_s"]>TAU+HALF_SUPPORT_S


def test_preobserved_invalid_c2_comparison_kept_and_corrected_new_evidence_exact():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    e=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    old_raw=OLD.read_bytes()
    previous=json.loads(old_raw.decode("utf-8"))
    original=json.loads(ORIGINAL.read_text(encoding="utf-8"))
    assert e["preregistered_plan"]==p
    assert e["preregistered_plan_sha256_lf"]==hashlib.sha256(
        PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert e["preobserved_original_wrong_analytic_evidence_SHA256"]==hashlib.sha256(old_raw).hexdigest()
    assert e["preobserved_wrong_analytic_absolute_comparison_INVALID"]
    assert e["corrected_divisor_of_previous_analytic_amplitude"]==C*C
    assert e["fitted_numerical_gain"] is None
    assert e["authority_original_PFFDTD_q0"]=="SELF_CONVERGENCE_FAILED"
    assert e["independent_physical"]=="NOT_VALIDATED" and e["product"]=="NO_GO"
    assert e["new_upstream_PFFDTD_wave_runs"]==e["new_GitHub_Actions_runs"]==0
    assert e["original_upstream_nonconvergence_unchanged"]
    assert not e["original_full250ms_all_ppw_pass"]
    rows=e["actual_new_weak_results_original_SHA_native_5grid"]
    old_rows=previous["actual_original_five_grid_pre_first_echo_weak_distribution_cases"]
    real={r["ppw"]:r for r in original["actual_native_wave_cases"]}
    assert [r["ppw"] for r in rows]==list(PPW)
    assert [r["ppw"] for r in old_rows]==list(PPW)
    assert e["new_prospectively_registered_three_widths_s"]==list(NEW_WIDTHS)
    for row in rows:
        k=row["ppw"]
        assert row["original_sha256_comms"]==real[k]["original_native_comm_sha256"]
        assert row["original_sha256_voxels"]==real[k]["original_solver_geometry_sha256"]
        assert row["original_native_original_full_250ms_PFFDTD_all_modes_unfiltered"]
        assert row["causal_pre_first_reflection_auxiliary_test_not_original_acceptance"]
        assert row["unchanged_original_full_250ms_vs_preobserved_relative"]<2e-6
        np.testing.assert_allclose(
            unpairs(row["unchanged_original_full_250ms_signed_40_80_recomputed"]),
            unpairs(real[k]["unmodified_original_transfer_pa_per_m3_s"]),rtol=2e-6,atol=1e-5)
        values=row["three_predeclared_compact_weak_distribution_tests"]
        assert [r["physical_weak_test_width_s"] for r in values]==list(NEW_WIDTHS)
        for record in values:
            actual=record["native_unmodified_original_weak_result"][
                "original_q0_native_weak_pressure_over_unit_input"]
            analytic=record["analytic_all_original_64pair_continuum_direct"]
            corrected=analytic["original_64_native_pairs_continuum_causal_green_weak_distribution"]
            prior_wrong=analytic["historical_old_analytic_expected_with_extra_c_squared_INVALID"]
            assert corrected!=0
            assert analytic["pre_observed_analytic_c_squared_units_error_corrected"]
            assert analytic["analytic_absolute_source_scale_fixed_by_PDE_not_fitted"]
            assert abs(prior_wrong/corrected-C*C)/(C*C)<1e-14
            assert abs(actual/corrected-record[
                "actual_native_over_assumed_continuum_source_model_signed_ratio"])<1e-10
            assert abs(abs(actual/corrected-1)-record[
                "actual_native_vs_assumed_continuum_weak_relative"])<1e-10
            assert analytic["earliest_real_original_64node_single_wall_reflection_s"]>TAU+HALF_SUPPORT_S
            assert record["conditional_source_normalization_not_fitted_or_verified"]
            assert np.isfinite(actual) and np.isfinite(corrected)
    scores=e["original_canonical_full250ms_all_adjacent"]
    assert [(r["coarse_ppw"],r["fine_ppw"]) for r in scores]==[
        (28,32),(32,36),(36,40),(40,44)]
    for j,rec in enumerate(scores):
        s=compare_complex_transfer(
            reference=rows[j+1]["unchanged_original_full_250ms_signed_40_80_recomputed"],
            candidate=rows[j]["unchanged_original_full_250ms_signed_40_80_recomputed"],
            frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
        for name in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
            assert abs(s[name]-rec["original_unchanged_three_gate_signed_full250ms"][name])<1e-10
        assert not rec["original_full250ms_pass"]
        assert rec["auxiliary_correctly_normalized_weak_diagnostic_not_original_release"]
