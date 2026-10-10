import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "benchmarks/acoustics/r130d_original_q0_bandlimited_modal_convergence_evidence_2026-10-11.json"


def _load():
    return json.loads(EVIDENCE.read_text(encoding="utf8"))


def test_verdicts_and_authority_honesty():
    e = _load()
    assert "BANDLIMITED_CONVERGENCE_ACHIEVED_TRUE_GEOMETRY" in e["verdicts"]
    assert "ORIGINAL_NATIVE_LOWBAND_NOT_CONVERGENT" in e["verdicts"]
    assert e["canonical_full_band_original_authority"] == "SELF_CONVERGENCE_FAILED"
    assert e["independent_physics"] == "NOT_VALIDATED"
    assert e["product"] == "NO_GO"
    assert e["new_solver_runs"] == 0
    assert e["band_limited_scores_are_diagnostics_not_authority"] is True


def test_band_sums_conserve_frozen_full_transfers():
    e = _load()
    for rel in e["exact_geometry_hybrid_eigensystem"]["conservation_rel"].values():
        assert rel <= 1e-10
    for rel in e["original_native_staircase_eigensystem"]["conservation_rel"].values():
        assert rel <= 1e-10


def test_hybrid_low_band_passes_all_gates_all_pairs():
    e = _load()
    for cutoff in ("100", "200"):
        cut = e["exact_geometry_hybrid_eigensystem"]["cutoffs"][cutoff]
        assert cut["all_pairs_all_gates"] is True
        assert len(cut["pairs"]) == 4
        for p in cut["pairs"]:
            assert p["complex_rms_relative"] <= 0.2
            assert p["magnitude_max_relative"] <= 0.25
            assert p["phase_max_deg"] <= 15.0


def test_original_staircase_lowest_band_fails():
    e = _load()
    cut = e["original_native_staircase_eigensystem"]["cutoffs"]["100"]
    assert cut["all_pairs_all_gates"] is False
    assert any(p["complex_rms_relative"] > 0.2 for p in cut["pairs"])
