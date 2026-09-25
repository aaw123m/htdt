"""#780: validation coverage dashboard — cells, gaps, version diff."""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_validation_dashboard import (
    ValidationCaseRecord,
    ValidationEvidenceRepository,
    build_coverage_cells,
    compare_provider_versions,
    drilldown_cases,
    find_coverage_gaps,
)


def _case(
    case_id: str,
    *,
    provider_id: str = 'wave-low',
    provider_version: str = 'v1',
    geometry_class: str = 'rectangular',
    source_class: str = 'subwoofer',
    observable: str = 'magnitude_fr',
    evidence_level: str = 'owned_room_holdout',
    verdict: str = 'pass',
    is_holdout: bool = True,
    qualification: str = 'production_qualified',
    band_high_hz: float = 80.0,
) -> ValidationCaseRecord:
    return ValidationCaseRecord(
        case_id=case_id,
        provider_id=provider_id,
        provider_version=provider_version,
        geometry_class=geometry_class,
        source_class=source_class,
        observable=observable,
        band_low_hz=25.0,
        band_high_hz=band_high_hz,
        evidence_level=evidence_level,
        verdict=verdict,
        qualification=qualification,
        is_holdout=is_holdout,
        recorded_at_utc='2026-09-24T00:00:00+00:00',
    )


def test_coverage_cells_explicit_denominator() -> None:
    cases = tuple(
        _case(f'c{i}', verdict='pass' if i < 5 else 'fail')
        for i in range(6)
    )
    cells = build_coverage_cells(cases)
    assert len(cells) == 1
    cell = cells[0]
    assert cell.eligible_cases == 6
    assert cell.passing_cases == 5
    assert cell.failing_cases == 1
    assert cell.strength == 'strong_coverage'


def test_coverage_strength_levels() -> None:
    assert build_coverage_cells(()) == ()
    thin = build_coverage_cells((_case('a', is_holdout=False),))
    assert thin[0].strength == 'thin_coverage'
    limited = build_coverage_cells(
        (_case('a'), _case('b'), _case('c'))
    )
    assert limited[0].strength == 'limited_coverage'


def test_drilldown_exacts() -> None:
    cases = (
        _case('a', geometry_class='concave', verdict='fail'),
        _case('b', geometry_class='concave'),
        _case('c'),
    )
    concave = drilldown_cases(cases, geometry_class='concave')
    assert [c.case_id for c in concave] == ['a', 'b']
    # Failures never hide behind aggregation.
    assert concave[0].verdict == 'fail'


def test_version_comparison_shows_both_directions() -> None:
    cases = (
        _case('p1', provider_version='v1'),
        _case('p2', provider_version='v1', verdict='fail'),
        _case('p3', provider_version='v1'),
        _case('p1', provider_version='v2', verdict='fail'),
        _case('p2', provider_version='v2'),
        # p3 not rerun in v2.
        _case('p4', provider_version='v2'),
    )
    deltas = compare_provider_versions(
        cases, provider_id='wave-low', old_version='v1', new_version='v2'
    )
    by_case = {d.case_id: d for d in deltas}
    assert by_case['p1'].outcome == 'newly_failing'
    assert by_case['p2'].outcome == 'newly_passing'
    assert by_case['p3'].outcome == 'removed_case'
    assert by_case['p4'].outcome == 'new_case'


def test_coverage_gaps_are_explicit() -> None:
    sparse = (_case('only', is_holdout=True, band_high_hz=80.0),)
    gaps = find_coverage_gaps(sparse)
    assert any('concave' in g or 'sloped' in g for g in gaps)
    assert any('directional' in g for g in gaps)
    assert any('one provider' in g for g in gaps)


def test_no_holdout_is_a_gap() -> None:
    cases = (
        _case('a', is_holdout=False, observable='complex_transfer'),
    )
    gaps = find_coverage_gaps(cases)
    assert any('complex_transfer' in g for g in gaps)


def test_repository_round_trip(tmp_path: Path) -> None:
    repo = ValidationEvidenceRepository(tmp_path / 'cad.sqlite3')
    case = _case('c1')
    repo.save_case('ev-1', case)
    repo.save_case('ev-1', case)
    other = _case('c1', provider_version='v2')
    repo.save_case('ev-2', other)
    all_cases = repo.list_cases()
    assert len(all_cases) == 2
    assert [c.provider_version for c in all_cases] == ['v1', 'v2']
    assert repo.list_cases(provider_id='wave-low') == all_cases
