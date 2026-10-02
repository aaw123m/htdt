"""Affected-test selector contract tests (scripts/select_affected_tests.py)."""

from __future__ import annotations

from pathlib import Path

from scripts import select_affected_tests as sat

TESTS_DIR = Path(__file__).resolve().parent


def _names(result: dict) -> set[str]:
    return {Path(f).name for f in result["files"]}


def test_single_token_module_matches_middle_token_stems() -> None:
    # `acoustics.py` is a single-token module whose dependents carry the token
    # mid-stem (test_cad_geometric_acoustics_*). Word-boundary matching must
    # still select them — missing coverage is worse than extra coverage.
    result = sat._select(["backend/src/htdt/acoustics.py"], TESTS_DIR)
    assert result["mode"] == "subset"
    files = _names(result)
    assert "test_cad_geometric_acoustics_adapter.py" in files
    assert "test_cad_geometric_acoustics_response.py" in files


def test_single_token_module_keeps_word_boundary(tmp_path: Path) -> None:
    # The boundary must not degrade into substring matching: "clockwork" is
    # not the `clock` token and must stay unselected.
    for name in (
        "test_clock.py",
        "test_clockwork_extra.py",
        "test_time_clock.py",
        "test_core_clock_registry.py",
    ):
        (tmp_path / name).write_text("def test_x() -> None:\n    pass\n")

    result = sat._select(["backend/src/htdt/clock.py"], tmp_path)
    assert result["mode"] == "subset"
    assert _names(result) == {
        "test_clock.py",
        "test_time_clock.py",
        "test_core_clock_registry.py",
    }


def test_module_with_no_tests_runs_full(tmp_path: Path) -> None:
    (tmp_path / "test_unrelated.py").write_text("def test_x() -> None:\n    pass\n")
    result = sat._select(["backend/src/htdt/somebrandnew_module.py"], tmp_path)
    assert result["mode"] == "full"


def test_multi_token_module_containment(tmp_path: Path) -> None:
    for name in (
        "test_cad_system_health.py",
        "test_cad_system_health_repository.py",
        "test_unrelated.py",
    ):
        (tmp_path / name).write_text("def test_x() -> None:\n    pass\n")
    result = sat._select(
        ["backend/src/htdt/cad_system_health_repository.py"], tmp_path
    )
    assert result["mode"] == "subset"
    assert "test_cad_system_health.py" in _names(result)
    assert "test_cad_system_health_repository.py" in _names(result)
