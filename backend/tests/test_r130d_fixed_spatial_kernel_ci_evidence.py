"""Independent CI 12-wave replay evidentiary gate with frozen separate width verdicts."""
import json
from pathlib import Path
import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_fixed_spatial_kernel_independent_fv_mfem_evidence_2026-10-09.json"
PLAN=ROOT/"benchmarks/acoustics/r130d_fixed_spatial_kernel_plan_2026-10-09.json"
def load():
    return json.loads(PLAN.read_text(encoding="utf-8")),json.loads(EVIDENCE.read_text(encoding="utf-8"))

def latest_improves(all_adjacent):
    assert len(all_adjacent)==2
    early,last=all_adjacent
    return (last["normalized_complex_l2"]<early["normalized_complex_l2"]
        and max(last["magnitude_relative_by_hz"])<max(early["magnitude_relative_by_hz"])
        and max(last["phase_difference_deg_by_hz"])<max(early["phase_difference_deg_by_hz"]))

def frozen_diagnostic_verdict(p,x,sigma):
    lim=p["metrics"]["reference_thresholds_unchanged_from_original_impulse"]
    c=x["comparisons"][sigma]
    fv,fem=c["fv_adjacent"][-1],c["mfem_adjacent"][-1]
    cross=c["cross_finest"]
    fz=np.array([complex(*z) for z in x["fv_levels"][sigma][-1]["complex_40_80_hz"]])
    mz=np.array([complex(*z) for z in x["mfem_levels"][sigma][-1]["complex_40_80_hz"]])
    db=float(max(abs(20*np.log10(abs(fz)/abs(mz)))))
    fpass=(latest_improves(c["fv_adjacent"])
        and fv["normalized_complex_l2"]<=lim["fv_complex_l2_max"]
        and max(fv["magnitude_relative_by_hz"])<=lim["fv_max_magnitude_relative"]
        and max(fv["phase_difference_deg_by_hz"])<=lim["fv_max_phase_deg"])
    mpass=(latest_improves(c["mfem_adjacent"])
        and fem["normalized_complex_l2"]<=lim["mfem_complex_l2_max"]
        and max(fem["magnitude_relative_by_hz"])<=lim["mfem_max_magnitude_relative"]
        and max(fem["phase_difference_deg_by_hz"])<=lim["mfem_max_phase_deg"])
    cpass=(cross["normalized_complex_l2"]<=lim["cross_complex_l2_max"]
        and max(cross["magnitude_relative_by_hz"])<=lim["cross_max_magnitude_relative"]
        and max(cross["phase_difference_deg_by_hz"])<=lim["cross_max_phase_deg"]
        and db<=lim["cross_max_magnitude_db"])
    return {"fv":fpass,"mfem":mpass,"cross":cpass,"altered_spatial_model_only":bool(fpass and mpass and cpass),"cross_max_magnitude_db":db}

def test_pinned_gaussian_ci_replay_is_full_and_canonical_failure_preserved():
    p,x=load()
    assert x["plan"]==p
    assert x["mfem_status"]=="ACTUALLY_SOLVED"
    assert x["original_fullband_PFFDTD"]=="SELF_CONVERGENCE_FAILED"
    assert x["original_discrete_impulse_candidate"]=="NOT_QUALIFIED"
    assert x["physical_validation"]=="NOT_VALIDATED"
    assert x["production_ready"] is False
    assert x["spatial_model_changed_from_original_point_source"] is True
    assert sorted(x["fv_levels"])==sorted(x["mfem_levels"])==["0.35","0.7"]
    for sigma in ["0.35","0.7"]:
        assert [a["n"] for a in x["fv_levels"][sigma]]==[12,20,32]
        assert [a["r"] for a in x["mfem_levels"][sigma]]==[2,3,4]
        for a in x["fv_levels"][sigma]+x["mfem_levels"][sigma]:
            assert a["sampled_time_impulse_q0_only"] is True
            assert a["worst_true_relative_linear_residual"]<=1e-8
            assert len(a["complex_40_80_hz"])==2
            assert len(a["magnitude_40_80_hz"])==2
            assert len(a["phase_deg_40_80_hz"])==2
            assert np.all(np.isfinite(a["complex_40_80_hz"]))
            assert np.all(np.isfinite(a["magnitude_40_80_hz"]))
            assert np.all(np.isfinite(a["phase_deg_40_80_hz"]))

@pytest.mark.parametrize("sigma,expected",[
    ("0.35",{"fv":True,"mfem":False,"cross":True,"altered_spatial_model_only":False}),
    ("0.7",{"fv":True,"mfem":True,"cross":True,"altered_spatial_model_only":True})
])
def test_frozen_separate_width_numeric_verdict(sigma,expected):
    p,x=load()
    value=frozen_diagnostic_verdict(p,x,sigma)
    for key,state in expected.items():
        assert value[key] is state,(sigma,key,value)
    assert value["cross_max_magnitude_db"]<3.

def test_original_point_impulse_remains_a_separate_failing_baseline():
    p,x=load()
    old=json.loads((ROOT/"benchmarks/acoustics/r130d_exact_discrete_impulse_candidate_evidence_2026-10-09.json").read_text())
    assert old["original_fullband_r130d"]=="SELF_CONVERGENCE_FAILED"
    assert old["production_ready"] is False
    assert p["authority"]["original_fullband_PFFDTD"]=="SELF_CONVERGENCE_FAILED"
    assert p["authority"]["original_discrete_impulse_candidate"]=="NOT_QUALIFIED"
    assert p["authority"]["production_ready"] is False
