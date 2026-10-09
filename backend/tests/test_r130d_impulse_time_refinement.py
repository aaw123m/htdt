"""Immutable original-unit-impulse dt-halving study across pinned FEM/FV grids.

Numerical interpolation, first-midpoint placement and frozen spatial gates
must never be relabelled as successfully validated production physics.
"""
from __future__ import annotations
import copy
import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_impulse_time_refinement import evaluate


def load(filename):
    return json.loads((ROOT/"benchmarks"/"acoustics"/filename).read_text(encoding="utf-8"))


def _all():
    return (
       load("r130d_impulse_time_refinement_plan_2026-10-09.json"),
       load("r130d_exact_discrete_impulse_candidate_evidence_2026-10-09.json"),
       load("r130d_impulse_time_refinement_evidence_2026-10-09.json")
    )


def _complex(v):
    arr=np.asarray(v,dtype=float)
    assert arr.shape==(2,2)
    return arr[:,0]+1j*arr[:,1]


def test_exact_preregistered_sources_and_both_refinements_archived():
    plan,old,e=_all()
    assert e["preregistered_plan"]==plan
    assert e["original_evidence_name"]=="r130d_exact_discrete_impulse_candidate_evidence_2026-10-09.json"
    assert plan["timestep_s"]==[.00025,.000125]
    assert plan["sample_counts"]==[1000,2000]
    assert plan["source"]["q_samples"]=="q[0]=1 m3/s, q[n>0]=0"
    assert plan["source"]["frequency_hz"]==[40,80]
    assert plan["source"]["no_Gaussian_no_taper"] is True
    assert e["source_waveform"]=="q[0]=1, others zero for both dt values"
    assert e["production_ready"] is False
    assert e["physical_validation"]=="NOT_VALIDATED"
    assert e["diagnostic"]["overall_original_fullband_impulse"]=="SELF_CONVERGENCE_FAILED"
    assert len(e["cases"])==5


def test_actual_all_five_dt_half_runs_have_finite_observations_and_resource_bounds():
    plan,old,e=_all()
    for row in e["cases"]:
        fine=row["actual_fine_result"]
        assert row["new_dt_s"]==.000125
        assert row["record_legacy_dt_s"]==.00025
        assert fine["step_count"]==2000
        assert 0<fine["elapsed_s"]<=plan["limits"]["max_runtime_per_case_s"]
        assert len(fine["complex_40_80_hz"])==2
        assert np.all(np.isfinite(_complex(fine["complex_40_80_hz"])))
        assert row["source_moments_not_identical_due_midpoint_shift"] is True
        if row["method"]=="exact_sloped_cutcell_neumann_FV":
            assert fine["air_volume_m3"]==pytest.approx(56,abs=1e-9)
        else:
            assert len(fine["export_sha256"])==64
            assert fine["worst_true_relative_linear_residual"]<=1e-8
            assert fine["maximum_cg_iterations"]<=350


def test_spatial_and_temporal_metrics_recomputed_from_true_complex_numbers():
    plan,old,e=_all()
    for row in e["cases"]:
        a=_complex(row["coarse_dt_transfer_40_80_hz"])
        b=_complex(row["fine_dt_transfer_40_80_hz"])
        z=row["fixed_grid_halved_dt_metrics"]
        assert z["normalized_complex_l2"]==pytest.approx(
            np.linalg.norm(a-b)/np.linalg.norm(b),abs=1e-12)
    for row in e["spatial_at_halved_dt"]:
        method=row["method"]
        coarse=next(x for x in e["cases"] if x["method"]==method and x["level"]==row["coarse"])
        fine=next(x for x in e["cases"] if x["method"]==method and x["level"]==row["fine"])
        a=_complex(coarse["fine_dt_transfer_40_80_hz"])
        b=_complex(fine["fine_dt_transfer_40_80_hz"])
        assert row["metrics"]["normalized_complex_l2"]==pytest.approx(
            np.linalg.norm(a-b)/np.linalg.norm(b),abs=1e-12)
    result=evaluate(plan,old,[x["actual_fine_result"] for x in e["cases"]])
    assert result["diagnostic"]==e["diagnostic"]
    for x,y in zip(result["cases"],e["cases"]):
        assert x["fixed_grid_halved_dt_metrics"]["normalized_complex_l2"]==pytest.approx(
            y["fixed_grid_halved_dt_metrics"]["normalized_complex_l2"],abs=1e-12)


def test_preregistered_timing_and_missing_level_fail_closed():
    plan,old,e=_all()
    fewer=[x["actual_fine_result"] for x in e["cases"][:-1]]
    with pytest.raises(ValueError,match="missing"):
        evaluate(plan,old,fewer)
    changed=copy.deepcopy(plan)
    changed["timestep_s"][1]=.0002
    with pytest.raises(ValueError,match="prospective"):
        evaluate(changed,old,[x["actual_fine_result"] for x in e["cases"]])


def test_unchanged_original_failing_impulse_and_production_authority():
    _,old,e=_all()
    assert old["numerical_diagnostic"]["fv_self_pass"] is False
    assert old["numerical_diagnostic"]["mfem_self_pass"] is False
    assert old["numerical_diagnostic"]["exact_unit_impulse_candidate_numerical_pass"] is False
    assert old["original_fullband_r130d"]=="SELF_CONVERGENCE_FAILED"
    assert e["diagnostic"]["not_full_spatial_monotonicity_gate"] is True
    assert e["diagnostic"]["not_temporal_only_because_midpoint_source_move"] is True
    assert e["diagnostic"]["production_ready"] is False
