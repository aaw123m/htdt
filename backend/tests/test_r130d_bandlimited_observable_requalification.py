import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "benchmarks/acoustics/r130d_bandlimited_observable_requalification_evidence_2026-10-11.json"


def _load():
    return json.loads(EVIDENCE.read_text(encoding="utf8"))


def test_requalified_contract_converges():
    e = _load()
    assert "REQUALIFIED_CONTRACT_CONVERGENCE_ACHIEVED" in e["verdicts"]
    assert len(e["pairs"]) == 4
    for p in e["pairs"]:
        assert p["all_three_gates"] is True
        assert p["complex_rms_relative"] <= 0.2
        assert p["magnitude_max_relative"] <= 0.25
        assert p["phase_max_deg"] <= 15.0


def test_original_authority_and_transparency_preserved():
    e = _load()
    assert e["canonical_original_contract"] == "SELF_CONVERGENCE_FAILED"
    assert e["product"] == "NO_GO"
    assert e["new_solver_runs"] == 0
    assert e["requalified_observable_hz"] == 200.0
    assert e["preregistered_plan"]["requalified_contract"]["observable"].startswith("signed 40/80")
    assert "MONOTONICITY_AT_NOISE_FLOOR" in e["verdicts"]


def test_full_sweep_reported_not_cherry_picked():
    e = _load()
    sweep = e["full_cutoff_sweep_pairs"]
    assert set(sweep) == {"100", "200", "400", "800", "1600", "3200"}
    assert all(p["all_three_gates"] for p in sweep["100"])
    assert all(p["all_three_gates"] for p in sweep["200"])
    assert not all(p["all_three_gates"] for p in sweep["400"])


def test_band_sums_conserve_frozen_full_transfers():
    e = _load()
    assert set(e["conservation_rel"]) == {"28", "32", "36", "40", "44"}
    for rel in e["conservation_rel"].values():
        assert rel <= 1e-10
