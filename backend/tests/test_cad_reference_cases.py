from __future__ import annotations

import pytest

from htdt.cad_reference_cases import (
    build_case_manifest,
    build_initial_reference_library,
    build_case_library,
    open_reference_case,
    validate_library_readiness,
    CaseExpectation,
    TutorialStep,
)

NOW = '2026-09-24T00:00:00+00:00'


def test_initial_library_satisfies_minimum_contract() -> None:
    library = build_initial_reference_library(
        created_at_utc=NOW, htdt_version='0.2.0'
    )
    gaps = validate_library_readiness(library)
    assert gaps == (), gaps
    families = {c.family for c in library.cases}
    assert 'golden_path' in families
    assert len([c for c in library.cases if c.family == 'negative']) >= 3


def test_golden_path_case_has_tutorial_and_expectations() -> None:
    library = build_initial_reference_library(
        created_at_utc=NOW, htdt_version='0.2.0'
    )
    golden = next(c for c in library.cases if c.family == 'golden_path')
    assert golden.tutorial_steps
    assert golden.expectations
    assert golden.expected_result_summaries


def test_golden_path_requires_tutorial_steps() -> None:
    with pytest.raises(Exception):
        build_case_manifest(
            case_id='reference-case:bad',
            case_version='1',
            family='golden_path',
            purpose='x',
            htdt_min_version='0.2.0',
            license='internal',
            tutorial_steps=(),
            case_owner='me',
            review_date_utc=NOW,
        )


def test_working_copy_gets_fresh_identity() -> None:
    library = build_initial_reference_library(
        created_at_utc=NOW, htdt_version='0.2.0'
    )
    case = library.cases[0]
    result = open_reference_case(
        case, mode='working_copy', working_project_id='user-proj:abc'
    )
    assert result.working_project_id == 'user-proj:abc'
    inspect = open_reference_case(case, mode='inspect')
    assert inspect.working_project_id is None
    with pytest.raises(ValueError):
        open_reference_case(case, mode='working_copy')


def test_archived_cases_inspect_only() -> None:
    library = build_initial_reference_library(
        created_at_utc=NOW, htdt_version='0.2.0'
    )
    case = library.cases[0]
    archived = build_case_manifest(
        case_id='reference-case:old',
        case_version='1',
        family='negative',
        purpose='x',
        htdt_min_version='0.1.0',
        license='internal',
        drift_state='ARCHIVED',
        case_owner='me',
        review_date_utc=NOW,
        expectations=(
            CaseExpectation(
                kind='reason_code', subject='s', expected='e'
            ),
        ),
        tutorial_steps=(
            TutorialStep(step_index=0, title='t', instruction='i'),
        ),
    )
    with pytest.raises(ValueError):
        open_reference_case(archived, mode='working_copy')
    _ = case  # silence lint


def test_readiness_reports_gaps() -> None:
    case = build_case_manifest(
        case_id='reference-case:one',
        case_version='1',
        family='optimization',
        purpose='x',
        htdt_min_version='0.2.0',
        license='internal',
        case_owner='me',
        review_date_utc=NOW,
        expectations=(
            CaseExpectation(kind='reason_code', subject='s', expected='e'),
        ),
    )
    library = build_case_library(
        library_id='reference-library:thin',
        created_at_utc=NOW,
        cases=(case,),
    )
    gaps = validate_library_readiness(library)
    assert any('golden_path' in g for g in gaps)
    assert any('negative' in g for g in gaps)
