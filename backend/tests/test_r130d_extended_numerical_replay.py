"""R130D extended PFFDTD and independent MFEM replay contract guards (#938)."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from htdt.r130d_general3d_validation import load_validation_plan
from run_r130d_extended_ppw_diagnostic import SCHEDULE, analyze, check_spec
from run_r130d_mfem_reference_replay import summarize as mfem_summarize


def _json(name: str) -> dict:
    return json.loads((ROOT / "benchmarks" / "acoustics" / name).read_text(encoding="utf-8"))


def _plan():
    return load_validation_plan(ROOT / "benchmarks" / "acoustics" / "r130d_general3d_validation_plan.json")


def test_extended_plan_preserves_frozen_observable_and_thresholds() -> None:
    parent = _plan()
    spec = _json("r130d_extended_ppw_diagnostic_plan.json")
    assert SCHEDULE == (8, 10, 12, 16, 20, 24)
    check_spec(spec, parent)
    for key, changed in [
        ("levels_ppw", [8, 10, 12, 16, 20]),
        ("duration_s", 0.3),
        ("phase_mask_db", -35),
        ("pffdtd_commit", "other"),
        ("unchanged_thresholds", {"complex_rms_relative_max": 999}),
    ]:
        broken = copy.deepcopy(spec)
        broken[key] = changed
        with pytest.raises(ValueError):
            check_spec(broken, parent)


def test_extended_committed_result_reproduces_frozen_levels_and_remains_failed() -> None:
    evidence = _json("r130d_extended_ppw_diagnostic_evidence_2026-10-09.json")
    assert tuple(x["ppw"] for x in evidence["levels"]) == SCHEDULE
    assert evidence["analysis"]["canonical_8_10_12_replay"] == "PASS"
    assert all(x["max_abs_component_error"] <= 1e-9
               for x in evidence["analysis"]["canonical_replay_errors"])
    assert evidence["analysis"]["self_convergence_accepted"] is False
    assert evidence["analysis"]["cross_solver_eligible"] is False
    assert evidence["analysis"]["extended_final_pair_below_frozen_limits"] is False
    assert evidence["decision"]["canonical_run25_status"] == "SELF_CONVERGENCE_FAILED"
    rerun = analyze(evidence["levels"], _plan(),
                    _json("r130d_general3d_self_convergence_run25_summary.json"),
                    tolerance=1e-9)
    assert rerun["adjacent_pairs"] == evidence["analysis"]["adjacent_pairs"]


def test_extended_replay_rejects_one_perturbed_historical_level() -> None:
    evidence = _json("r130d_extended_ppw_diagnostic_evidence_2026-10-09.json")
    levels = copy.deepcopy(evidence["levels"])
    levels[0]["transfer_pa_per_m3_s"][0][0] += 1e-5
    with pytest.raises(RuntimeError, match="reproduction mismatch"):
        analyze(levels, _plan(),
                _json("r130d_general3d_self_convergence_run25_summary.json"),
                tolerance=1e-9)


def test_mfem_analysis_does_not_promote_archived_failed_reference() -> None:
    parent = _plan()
    original = _json("r130d_general3d_self_convergence_run25_summary.json")
    actual = copy.deepcopy(original["mfem_levels"])
    report = mfem_summarize(original=original, actual=actual, plan=parent)
    assert report["baseline_replay_state"] == "MATCH"
    assert report["mfem_self_convergence"] == "SELF_CONVERGENCE_FAILED"
    assert report["cross_solver_eligible"] is False
    assert report["general_3d_physical_validation"] == "NOT_VALIDATED"
