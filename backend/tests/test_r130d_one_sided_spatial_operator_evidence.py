"""Evidence gates for original temporal q0 under separately changed spatial operators.

No mixed-operator numerical result qualifies the original point/point impulse.
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
from run_r130d_one_sided_spatial_operator import (
    validate_plan,pinned_baselines,reference_case,compare_adjacent,
    POINT_SHA,BOTH_SHA,WIDTHS,FV_NS,P2_RS,OPERATORS)
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_one_sided_spatial_operator_evidence_2026-10-09.json"
PLAN=ROOT/"benchmarks/acoustics/r130d_one_sided_spatial_operator_plan_2026-10-09.json"

def freeze():
    raw=PLAN.read_bytes()
    p=validate_plan(json.loads(raw))
    d=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert d["plan_sha256_lf"]==hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest()
    assert d["preregistered_plan"]==p
    return p,d

def test_all_sixteen_mixed_original_temporal_impulses_are_present():
    p,d=freeze()
    assert d["canonical_original_point_q0"]=="SELF_CONVERGENCE_FAILED"
    assert d["physical_validation"]=="NOT_VALIDATED"
    assert d["production_ready"] is False
    assert d["source_receiver_spatial_operator_was_changed"] is True
    assert d["pinned_point_baseline_sha256"]==POINT_SHA
    assert d["pinned_double_gaussian_baseline_sha256"]==BOTH_SHA
    assert [(x["method"],x["level"]) for x in d["actual_mixed_cases"]]==[
        ("fv",20),("fv",32),("mfem",3),("mfem",4)]
    original,full_gauss=pinned_baselines(p)
    observed_count=0
    for case in d["actual_mixed_cases"]:
        assert sorted(case["widths"])==["0.35","0.7"]
        for sigma in WIDTHS:
            row=case["widths"][str(sigma)]
            control=reference_case(original,full_gauss,case["method"],case["level"],sigma)
            np.testing.assert_array_equal(row["control_point_point_complex_40_80_hz"],
                                          control["point_source_point_receiver"])
            np.testing.assert_array_equal(row["control_gaussian_gaussian_complex_40_80_hz"],
                                          control["gaussian_source_gaussian_receiver"])
            assert sorted(row["mixed_wave"])==sorted(OPERATORS)
            for name,wave in row["mixed_wave"].items():
                assert name in OPERATORS
                observed_count+=1
                assert wave["sampled_time_impulse_q0_only"] is True
                assert wave["worst_true_relative_linear_residual"]<=1e-8
                assert len(wave["complex_40_80_hz"])==2
                assert len(wave["magnitude_40_80_hz"])==2
                assert len(wave["phase_deg_40_80_hz"])==2
                assert np.all(np.isfinite(wave["complex_40_80_hz"]))
                assert np.all(np.isfinite(wave["magnitude_40_80_hz"]))
                assert np.all(np.isfinite(wave["phase_deg_40_80_hz"]))
    assert observed_count==16

def test_adjacent_errors_preserve_all_bands_all_four_spatial_variants():
    p,d=freeze()
    recalc=compare_adjacent(d["actual_mixed_cases"])
    assert recalc==d["adjacent_metrics"]
    for method in ("fv","mfem"):
        for sigma in ("0.35","0.7"):
            q=d["adjacent_metrics"][method][sigma]
            assert sorted(q)==sorted(p["operator_cases"])
            for name,v in q.items():
                assert np.isfinite(v["normalized_complex_l2"])
                assert len(v["magnitude_relative_by_hz"])==2
                assert len(v["phase_difference_deg_by_hz"])==2
                assert np.all(np.isfinite(v["magnitude_relative_by_hz"]))
                assert np.all(np.isfinite(v["phase_difference_deg_by_hz"]))
