import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "benchmarks/acoustics/r130d_rigid_box_semidiscrete_spectrum_evidence_2026-10-11.json"


def _load():
    return json.loads(EVIDENCE.read_text(encoding="utf8"))


def test_physics_validated_verdicts():
    e = _load()
    assert "PHYSICS_VALIDATED_SEMIDISCRETE_SPECTRUM" in e["verdicts"]
    assert "OPERATOR_DISCRETIZATION_CONFIRMED" in e["verdicts"]
    assert "RECORD_PEAK_DETECTION_LIMIT_CONFIRMED" in e["verdicts"]
    assert e["canonical_original_contract"] == "SELF_CONVERGENCE_FAILED"
    assert e["product"] == "NO_GO"
    assert e["time_domain_solver_runs"] == 0


def test_both_levels_pass_all_comparisons():
    e = _load()
    assert {l["grid_spacing_m"] for l in e["levels"]} == {0.1, 0.05}
    for l in e["levels"]:
        assert l["pass"] is True
        assert l["discrete_operator_exactness"]["max_relative"] <= 1e-8
        assert l["continuum_agreement"]["max_relative"] <= 0.02
        assert l["dispersion_bound_all_modes"] is True
        assert l["unresolved_cluster_coverage"] == 1.0
        assert len(l["per_mode"]) == e["analytic_mode_count_below_cutoff"]


def test_refinement_scaling_is_o_h2():
    e = _load()
    errs = {l["grid_spacing_m"]: l["continuum_agreement"]["max_relative"] for l in e["levels"]}
    ratio = errs[0.1] / errs[0.05]
    assert 2.5 <= ratio <= 6.0  # O(h^2) predicts 4x


def test_every_analytic_mode_matched():
    e = _load()
    for l in e["levels"]:
        for m in l["per_mode"]:
            assert m["relative_error"] <= 0.02
            assert m["relative_error"] <= m["dispersion_bound"]
