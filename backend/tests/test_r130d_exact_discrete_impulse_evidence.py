"""Prospectively frozen exact q[0]=1 impulse FV/MFEM numerical gate replay."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))

from run_r130d_exact_discrete_impulse_candidate import gate,HASHES,DOFS


def _read(filename):
    return json.loads((ROOT/"benchmarks"/"acoustics"/filename).read_text(encoding="utf-8"))


def test_original_impulse_waveform_provenance_preregistered_and_original_gate_untouched():
    p=_read("r130d_exact_discrete_impulse_new_solver_plan_2026-10-09.json")
    e=_read("r130d_exact_discrete_impulse_candidate_evidence_2026-10-09.json")
    assert e["preregistered_plan"]==p
    assert p["source"]["waveform"]=="discrete unit volume-velocity impulse q[0]=1m3/s, q[n>0]=0"
    assert p["source"]["no_smoothing"] is True
    assert p["source"]["dt_s"]==.00025
    assert p["source"]["steps"]==1000
    assert p["source"]["frequency_bins_hz"]==[40,80]
    impulse=np.zeros(1000,dtype=float)
    impulse[0]=1
    assert e["source_samples_sha256"]==hashlib.sha256(impulse.tobytes()).hexdigest()
    assert e["actual_time_domain_impulse_injection"] is True
    assert e["no_smoothing_no_taper"] is True
    assert e["production_ready"] is False
    assert e["original_fullband_r130d"]=="SELF_CONVERGENCE_FAILED"


def test_all_levels_same_geometry_source_receiver_and_independent_hashed_P2():
    e=_read("r130d_exact_discrete_impulse_candidate_evidence_2026-10-09.json")
    assert [v["n"] for v in e["fv_levels"]]==[12,16,20,24,28,32]
    assert [v["r"] for v in e["mfem_levels"]]==[1,2,3,4]
    for f in e["fv_levels"]:
        assert f["air_volume_m3"]==pytest.approx(56,abs=1e-10)
        assert len(f["complex_40_80_hz"])==2
        assert np.all(np.isfinite(np.asarray(f["complex_40_80_hz"])))
    for f in e["mfem_levels"]:
        assert f["dofs"]==DOFS[f["r"]]
        assert f["export_sha256"]==HASHES[f["r"]]
        assert f["actual_time_domain_discrete_unit_impulse"] is True
        assert f["worst_true_relative_linear_residual"]<=1e-8
        assert f["maximum_cg_iterations"]<=350
        assert len(f["complex_40_80_hz"])==2
        assert np.all(np.isfinite(np.asarray(f["complex_40_80_hz"])))


def test_independent_exact_impulse_metrics_recomputed_no_false_pass():
    e=_read("r130d_exact_discrete_impulse_candidate_evidence_2026-10-09.json")
    p=e["preregistered_plan"]
    result=e["numerical_diagnostic"]
    other=gate(e,p)
    def same(a,b):
        if isinstance(a,dict):
            assert isinstance(b,dict)
            assert set(a)==set(b)
            for k in a:same(a[k],b[k])
        elif isinstance(a,list):
            assert isinstance(b,list)
            assert len(a)==len(b)
            for x,y in zip(a,b):same(x,y)
        elif isinstance(a,float):
            assert a==pytest.approx(b,rel=1e-11,abs=1e-11)
        else:
            assert a==b
    same(result,other)
    assert result["original_PFFDTD_impulse"]=="SELF_CONVERGENCE_FAILED"
    assert result["canonical_original_fullband_impulse"]=="SELF_CONVERGENCE_FAILED"
    assert result["production_ready"] is False
    assert result["physical_validation"]=="NOT_VALIDATED"
    assert result["exact_unit_impulse_candidate_numerical_pass"] is (
        result["fv_self_pass"] and result["mfem_self_pass"]
        and result["cross_method_pass"]
    )


def test_missing_frequency_refinement_still_fail_closed():
    e=_read("r130d_exact_discrete_impulse_candidate_evidence_2026-10-09.json")
    p=e["preregistered_plan"]
    wrong=json.loads(json.dumps(e))
    wrong["mfem_levels"][-1]["complex_40_80_hz"]=[[1,0]]
    with pytest.raises((ValueError,IndexError,TypeError)):
        gate(wrong,p)
