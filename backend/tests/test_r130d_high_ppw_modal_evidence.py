"""#938 high PPW and MFEM low-mode numerical diagnostics are fail-closed."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import sys

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

from htdt.r130d_general3d_validation import load_validation_plan
from run_r130d_high_ppw_diagnostic import ADDITIONAL_PPW, analyze, check_plan
from run_r130d_mfem_lowmode_diagnostic import CUTOFFS_HZ, COMPARISON_HZ


def _j(name):
    return json.loads((REPO / "benchmarks" / "acoustics" / name).read_text(encoding="utf-8"))


def _parent():
    return load_validation_plan(
        REPO / "benchmarks" / "acoustics" / "r130d_general3d_validation_plan.json"
    )


def _inputs():
    return (
        _j("r130d_high_ppw_diagnostic_plan.json"),
        _parent(),
        _j("r130d_extended_ppw_diagnostic_evidence_2026-10-09.json"),
        _j("r130d_extended_ppw_diagnostic_plan.json"),
    )


def test_high_ppw_plan_and_anchor_identity_frozen():
    assert ADDITIONAL_PPW == (28, 32, 36, 40, 44)
    spec, parent, previous, previous_plan = _inputs()
    check_plan(spec, parent, previous, previous_plan)
    for field, value in (
        ("additional_ppw", [28, 32, 36, 40]),
        ("duration_s", 0.30),
        ("frequency_hz", [45, 80]),
        ("frozen_acceptance", {"complex_rms_relative_max": 100}),
        ("solver_commit", "unverified"),
        ("previous_level_ppw", 20),
    ):
        changed = copy.deepcopy(spec)
        changed[field] = value
        with pytest.raises(ValueError):
            check_plan(changed, parent, previous, previous_plan)


def test_high_ppw_actual_evidence_remains_nonconvergent():
    actual = _j("r130d_high_ppw_diagnostic_evidence_2026-10-09.json")
    parent = _parent()
    assert actual["schema_version"] == "htdt.r130d.high-ppw-independent-diagnostic-evidence-1"
    assert [x["ppw"] for x in actual["levels"]] == [24, 28, 32, 36, 40, 44]
    replay = analyze(actual["levels"], parent)
    assert replay["pairs"] == actual["analysis"]["pairs"]
    assert replay["final_pair_within_frozen_limits"] is False
    assert replay["all_adjacent_metric_errors_strictly_decrease"] is False
    assert not any(replay["each_pair_below_frozen_limits"])
    assert actual["decision"]["production"] == "NO_GO_NOT_VALIDATED"


def test_high_ppw_rejects_partial_or_reordered_series():
    actual = _j("r130d_high_ppw_diagnostic_evidence_2026-10-09.json")
    with pytest.raises(ValueError):
        analyze(actual["levels"][:-1], _parent())
    reordered = list(actual["levels"])
    reordered[-1], reordered[-2] = reordered[-2], reordered[-1]
    with pytest.raises(ValueError):
        analyze(reordered, _parent())


def test_mfem_reference_real_replay_does_not_claim_numeric_success():
    a = _j("r130d_mfem_reference_replay_evidence_2026-10-09.json")
    assert a["source_commit"] == "d964264cdb9a13e94a201b6c236c7721e0c8765f"
    assert a["analysis"]["baseline_replay_state"] == "MATCH"
    assert a["analysis"]["mfem_self_convergence"] == "SELF_CONVERGENCE_FAILED"
    assert a["analysis"]["cross_solver_eligible"] is False
    assert a["analysis"]["reference_adjacent_metrics"][-1]["phase_max_deg"] > 170


def test_exploratory_lowmode_cannot_override_canonical_failure():
    a = _j("r130d_mfem_lowmode_diagnostic_evidence_2026-10-09.json")
    assert tuple(a["contract"]["modal_cutoffs_hz"]) == CUTOFFS_HZ
    assert tuple(a["contract"]["canonical_comparison_bins_hz"]) == COMPARISON_HZ
    assert a["contract"]["physics_changed_from_canonical_full_basis"] is True
    assert a["decision"]["canonical_mfem_self_convergence"] == "SELF_CONVERGENCE_FAILED"
    assert a["decision"]["cross_solver_eligible"] is False
    assert a["decision"]["production_ready"] is False
    assert all(
        a["cutoff_comparisons"][i]["pairs"][-1]["complex_rms_relative"] > 0.05
        for i in range(len(CUTOFFS_HZ))
    )
