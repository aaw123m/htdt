"""Original 8-node high PPW q0 finite-time coherent spectral attribution."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys
import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_original_high_ppw_window_partition import (
    PPW,ORIG_SHA,BANDS,validate_plan,partition_one,adjacent_analysis,
    complex_from_pairs)

PLAN=ROOT/"benchmarks/acoustics/r130d_original_high_ppw_native_window_partition_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_high_ppw_native_window_partition_evidence_2026-10-09.json"

def frozen():
    raw=PLAN.read_bytes()
    p=validate_plan(json.loads(raw))
    d=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert d["preregistered_plan"]==p
    assert d["plan_sha256_lf"]==hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest()
    return p,d

def test_original_waveform_and_canonical_nonconvergence_are_unchanged():
    p,d=frozen()
    assert p["original_pffdtd"]["frozen_ppw"]==list(PPW)
    assert p["original_pffdtd"]["record_s"]==[0,0.25]
    assert p["original_pffdtd"]["frequencies_hz"]==[40,80]
    assert p["window"]["physical_intervals_s"]==[list(b) for b in BANDS]
    assert p["original_pffdtd"]["no_taper"] is True
    assert d["original_native_high_ppw_external_sha256"]==ORIG_SHA
    assert d["all_five_original_point_point_waves_unchanged"] is True
    assert d["canonical_original_full_window_point_q0"]=="SELF_CONVERGENCE_FAILED"
    assert d["modified_source_receiver_waveform_or_boundary"] is False
    assert d["diagnostic_partition_is_not_new_qualification"] is True
    assert d["physical_validation"]=="NOT_VALIDATED"
    assert d["production_ready"] is False

@pytest.mark.parametrize("change",[
    lambda p:p["original_pffdtd"].update(frozen_ppw=[28,32]),
    lambda p:p["original_pffdtd"].update(exact_source_xyz_m=[1.6,2,2]),
    lambda p:p["original_pffdtd"].update(frequencies_hz=[40]),
    lambda p:p["window"].update(physical_intervals_s=[[0,.25]]),
    lambda p:p["resource_limits"].update(allowed_segment_count=5),
    lambda p:p["authority"].update(product="GO"),
])
def test_preregistered_partition_rules_cannot_be_tuned_after_observation(change):
    p,_=frozen()
    change(p)
    with pytest.raises(ValueError):validate_plan(p)

def test_exhaustive_original_signed_time_partition_integrity_and_frozen_source():
    p,d=frozen()
    assert [x["ppw"] for x in d["levels"]]==list(PPW)
    original=json.loads((ROOT/p["original_pffdtd"]["source_control_evidence"]).read_text())
    for row,prior in zip(d["levels"],original["actual_native_wave_cases"]):
        ppw=row["ppw"]
        assert ppw==prior["ppw"]
        assert row["original_8node_comms_sha256"]==p["original_pffdtd"]["original_8_node_comms_sha_by_ppw"][str(ppw)]
        assert row["original_solver_voxel_sha256"]==prior["original_solver_geometry_sha256"]
        assert row["source_temporal_unit_q0_and_original_native_points_unchanged"] is True
        assert row["full_original_control_signed_complex"]==prior["unmodified_original_transfer_pa_per_m3_s"]
        obs=row["time_components"]
        assert len(obs["signed_partitions_40_80_complex"])==3
        assert [z["start_s"] for z in obs["segments"]]==[0,0.05,0.15]
        assert [z["end_s"] for z in obs["segments"]]==[0.05,0.15,0.25]
        assert sum(z["sample_count"] for z in obs["segments"])==obs["record_samples"]
        assert 0<obs["record_samples"]<=p["resource_limits"]["max_samples_per_case"]
        assert obs["exact_complex_partition_conservation"] is True
        components=[complex_from_pairs(q) for q in obs["signed_partitions_40_80_complex"]]
        result=np.sum(components,axis=0)
        np.testing.assert_allclose(result,complex_from_pairs(
            obs["full_original_40_80_complex"]),rtol=2e-11,atol=2e-8)
        np.testing.assert_allclose(result,complex_from_pairs(
            row["full_original_control_signed_complex"]),rtol=2e-11,atol=2e-8)
        for seg in obs["segments"]:
            assert seg["pressure_rms_pa"]>=0
            assert seg["pressure_max_absolute_pa"]>=seg["pressure_rms_pa"]

def test_all_four_native_original_signed_adjacent_differences_reconstruct():
    p,d=frozen()
    allrows=d["adjacent_native_8node_original_signed_difference_contributions"]
    assert [(q["coarse_ppw"],q["fine_ppw"]) for q in allrows]==[
        (28,32),(32,36),(36,40),(40,44)]
    recalculated=adjacent_analysis(d["levels"])
    for q,r in zip(allrows,recalculated):
        assert q["coarse_ppw"]==r["coarse_ppw"]
        assert q["fine_ppw"]==r["fine_ppw"]
        assert q["full_delta_norm"]==pytest.approx(r["full_delta_norm"],rel=2e-11,abs=2e-8)
        assert len(q["segments"])==3
        delta=sum((complex_from_pairs(s["complex_delta_40_80"]) for s in q["segments"]),
                  np.zeros(2,dtype=complex))
        np.testing.assert_allclose(delta,complex_from_pairs(q["full_signed_delta_40_80"]),
                                   rtol=2e-11,atol=2e-8)
        for x,y in zip(q["segments"],r["segments"]):
            assert x["segment_s"]==y["segment_s"]
            assert x["relative_to_full_delta_norm"]==pytest.approx(
                y["relative_to_full_delta_norm"],rel=2e-11,abs=2e-8)
    assert all(q["segments"][-1]["relative_to_full_delta_norm"]>1 for q in allrows)

def test_synthetic_exact_segment_partition_refuses_wrong_original_record():
    dt=.001
    t=np.arange(250)*dt
    p=np.cos(2*np.pi*40*t)+.25*np.sin(2*np.pi*80*t)
    full=np.exp(2j*np.pi*np.array([40,80])[:,None]*t[None,:])@p
    d=partition_one(p,dt,[40,80],full)
    assert d["record_samples"]==250
    assert d["exact_complex_partition_conservation"] is True
    assert sum(x["sample_count"] for x in d["segments"])==250
    with pytest.raises(ValueError,match="original full transfer"):
        partition_one(p,dt,[40,80],full+1)
