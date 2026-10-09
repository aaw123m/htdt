"""Source-moment-controlled exact impulse diagnostic; NEVER a new impulse gate."""
from __future__ import annotations
from pathlib import Path
import copy
import json
import sys

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_impulse_source_moment_probe import evaluate


def load(name):
    return json.loads((ROOT/"benchmarks"/"acoustics"/name).read_text(encoding="utf-8"))


def _load():
    return (
      load("r130d_impulse_source_moment_matching_plan_2026-10-09.json"),
      load("r130d_exact_discrete_impulse_candidate_evidence_2026-10-09.json"),
      load("r130d_impulse_time_refinement_evidence_2026-10-09.json"),
      load("r130d_impulse_source_moment_evidence_2026-10-09.json")
    )


def z(v):
    a=np.asarray(v,dtype=float)
    assert a.shape==(2,2)
    return a[:,0]+1j*a[:,1]


def test_preregistered_source_moments_exact_and_orig_impulse_not_promoted():
    p,old,coarse,e=_load()
    assert e["preregistered_plan"]==p
    assert e["schema_version"]=="htdt.r130d.impulse-source-moment-placement-probe-evidence-1"
    assert e["changed_source_is_not_original_unit_impulse"] is True
    assert e["temporal_integrator_error_purely_isolated"] is False
    assert e["production_enabled"] is False
    assert e["original_fullband_impulse"]=="SELF_CONVERGENCE_FAILED"
    assert old["numerical_diagnostic"]["exact_unit_impulse_candidate_numerical_pass"] is False
    assert coarse["production_ready"] is False
    q=np.zeros(2000,dtype=float);q[:2]=1.
    dt=.000125
    tm=(np.arange(2000)+.5)*dt
    assert dt*q.sum()==pytest.approx(.00025,abs=1e-15)
    assert float(tm@q/q.sum())==pytest.approx(.000125,abs=1e-15)


def test_all_3_actual_matched_source_wave_solves_and_recomputed_comparisons():
    p,original,half,e=_load()
    assert [(x["method"],x["level"]) for x in e["cases"]]==[
       ("exact_sloped_cutcell_neumann_FV",20),
       ("exact_sloped_cutcell_neumann_FV",32),
       ("pinned_independent_MFEM_P2",4)]
    replica=evaluate(p,original,half,[x["matched_solver_row"] for x in e["cases"]])
    for x,y in zip(replica["cases"],e["cases"]):
        assert x["method"]==y["method"] and x["level"]==y["level"]
        assert x["matched_solver_row"]["matched_discrete_source_first_two_samples"]==[1,1]
        assert x["matched_solver_row"]["steps"]==2000
        assert x["matched_solver_row"]["elapsed_s"]>0
        if x["method"]=="pinned_independent_MFEM_P2":
            assert x["matched_solver_row"]["export_sha256"]==p["mfem_authority"]["r4_hash_sha256"]
            assert x["matched_solver_row"]["worst_true_relative_linear_residual"]<=1e-8
        else:
            assert x["matched_solver_row"]["exact_room_volume_m3"]==pytest.approx(56,abs=1e-9)
        for key in ("coarse_raw_vs_fine_raw",
                    "coarse_raw_vs_fine_moment_aligned",
                    "fine_raw_vs_fine_moment_aligned"):
            assert x[key]["normalized_complex_l2"]==pytest.approx(y[key]["normalized_complex_l2"],abs=1e-12)
        assert y["coarse_raw_vs_fine_raw"]["normalized_complex_l2"]==pytest.approx(
              np.linalg.norm(z(y["coarse_original_complex"])-z(y["fine_raw_complex"]))/
              np.linalg.norm(z(y["fine_raw_complex"])),abs=1e-12)
        assert y["coarse_raw_vs_fine_moment_aligned"]["normalized_complex_l2"]==pytest.approx(
              np.linalg.norm(z(y["coarse_original_complex"])-z(y["fine_moment_aligned_complex"]))/
              np.linalg.norm(z(y["fine_moment_aligned_complex"])),abs=1e-12)


def test_source_shape_mutation_and_missing_method_fail_closed():
    p,old,half,e=_load()
    wrong=copy.deepcopy(p)
    wrong["source_authority"]["matched_fine"]["samples"]=[2,0]
    with pytest.raises(ValueError,match="preregistered"):
        evaluate(wrong,old,half,[x["matched_solver_row"] for x in e["cases"]])
    with pytest.raises(ValueError,match="missing"):
        evaluate(p,old,half,[x["matched_solver_row"] for x in e["cases"][:-1]])
