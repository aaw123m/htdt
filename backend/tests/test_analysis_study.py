from pathlib import Path

import pytest

from htdt.cad_analysis_study import (
    StudyAnalysisOperation,
    StudyAuthorityRef,
    StudySpecItem,
    build_analysis_study,
    duplicate_analysis_study,
    evaluate_study_state,
    make_study_note,
)
from htdt.cad_analysis_study_repository import (
    AnalysisStudyConflictError,
    CadAnalysisStudyRepository,
)
from htdt.cad_repository import SceneRepository


def _repository(tmp_path: Path) -> CadAnalysisStudyRepository:
    return CadAnalysisStudyRepository(
        SceneRepository(tmp_path / 'scene.sqlite3')
    )


def _study(document_id: str = 'doc-1', **kwargs):
    return build_analysis_study(
        document_id=document_id,
        title='Seat comparison',
        study_kind='measurement_comparison',
        bound_refs=(
            StudyAuthorityRef(
                kind='measurement_dataset',
                ref_id='meas-1',
                ref_sha256='a' * 64,
            ),
            StudyAuthorityRef(
                kind='target_curve',
                ref_id='curve-1',
                ref_sha256='b' * 64,
            ),
        ),
        presentation_spec=(
            StudySpecItem(key='range', value='20-20000'),
            StudySpecItem(key='overlay', value='target'),
        ),
        analysis_operations=(
            StudyAnalysisOperation(
                operation='smoothing',
                version='octave-1/6-v1',
                parameters=(StudySpecItem(key='fraction', value='1/6'),),
            ),
        ),
        notes=(
            make_study_note(
                kind='decision',
                text='Candidate B rejected: seat-2 dip beyond tolerance',
                created_at_utc='2026-09-24T00:00:00+00:00',
            ),
        ),
        scene_revision_id='rev-1',
        system_variant_id='var-1',
        created_at_utc='2026-09-24T00:00:00+00:00',
        **kwargs,
    )


def test_study_roundtrip_and_duplicate_refs_rejected() -> None:
    study = _study()
    assert len(study.study_sha256) == 64
    restored = type(study).model_validate_json(study.model_dump_json())
    assert restored == study
    with pytest.raises(ValueError):
        build_analysis_study(
            document_id='doc-1',
            title='dup',
            bound_refs=(
                StudyAuthorityRef(kind='other', ref_id='r'),
                StudyAuthorityRef(kind='other', ref_id='r'),
            ),
            created_at_utc='2026-09-24T00:00:00+00:00',
        )


def test_study_repository_append_only_and_chain(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    study = _study()
    repository.save_study(study)
    assert repository.get_study(study.study_id) == study
    with pytest.raises(AnalysisStudyConflictError):
        repository.save_study(study)

    clone = duplicate_analysis_study(
        study, created_at_utc='2026-09-24T01:00:00+00:00'
    )
    assert clone.duplicated_from_study_id == study.study_id
    assert clone.study_sha256 != study.study_sha256
    repository.save_study(clone)
    assert [item.study_id for item in repository.list_studies('doc-1')] == [
        study.study_id,
        clone.study_id,
    ]


def test_study_duplicate_requires_stored_prior(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    study = _study()
    clone = duplicate_analysis_study(
        study, created_at_utc='2026-09-24T01:00:00+00:00'
    )
    with pytest.raises(ValueError):
        repository.save_study(clone)


def test_study_state_reproducible_not_current_broken() -> None:
    study = _study()
    report = evaluate_study_state(
        study,
        resolve_sha256={
            ('measurement_dataset', 'meas-1'): 'a' * 64,
            ('target_curve', 'curve-1'): 'b' * 64,
        },
        current_scene_revision_id='rev-1',
        current_system_variant_id='var-1',
    )
    assert report.state == 'reproducible'

    report = evaluate_study_state(
        study,
        resolve_sha256={
            ('measurement_dataset', 'meas-1'): 'c' * 64,
            ('target_curve', 'curve-1'): 'b' * 64,
        },
        current_scene_revision_id='rev-1',
        current_system_variant_id='var-1',
    )
    assert report.state == 'not_current'
    assert any(ref.state == 'stale' for ref in report.refs)

    report = evaluate_study_state(
        study,
        resolve_sha256={
            ('target_curve', 'curve-1'): 'b' * 64,
        },
    )
    assert report.state == 'broken_reference'
    assert any(ref.state == 'missing' for ref in report.refs)

    report = evaluate_study_state(
        study,
        resolve_sha256={
            ('measurement_dataset', 'meas-1'): 'a' * 64,
            ('target_curve', 'curve-1'): 'b' * 64,
        },
        current_scene_revision_id='rev-2',
        current_system_variant_id='var-1',
    )
    assert report.state == 'not_current'
    assert any('historical' in reason for reason in report.reasons)
