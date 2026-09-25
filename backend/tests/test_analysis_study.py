from pathlib import Path

import pytest

from htdt.cad_analysis_study import (
    STUDY_OPERATION_CONTRACTS,
    StudyAnalysisOperation,
    StudyAuthorityRef,
    StudySpecItem,
    build_analysis_study,
    duplicate_analysis_study,
    evaluate_operation_support,
    evaluate_study_state,
    make_study_note,
)
from htdt.cad_analysis_study_repository import (
    AnalysisStudyConflictError,
    CadAnalysisStudyRepository,
)
from htdt.cad_authority_resolver import ResolvedAuthority
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import normalize_rew_text
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository


DOC = F1_DOCUMENT_ID


def _seed(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'scene.sqlite3')
    saved = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    )
    variant_repository = CadSystemVariantRepository(scene_repository)
    variant = build_system_variant(
        baseline=saved.revision,
        name='study variant',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front Left'),
        ),
        proposed_entities=(),
        created_at_utc='2026-09-24T00:00:00+00:00',
    )
    variant_repository.save_variant(variant)
    measurement_repository = CadMeasurementRepository(scene_repository)
    record, dataset, filename, raw = normalize_rew_text(
        saved.revision,
        'point-mlp',
        b'20 70\n40 71\n80 69\n',
        filename='mlp.txt',
        imported_at='2026-09-17T09:30:00+00:00',
    )
    measurement_repository.save(
        record, dataset, raw_filename=filename, raw_bytes=raw
    )
    repository = CadAnalysisStudyRepository(
        scene_repository,
        system_variant_repository=variant_repository,
        measurement_repository=measurement_repository,
        kind_resolvers={
            'target_curve': lambda ref_id: ResolvedAuthority(
                kind='target_curve',
                ref_id=ref_id,
                document_id=DOC,
                semantic_sha256='b' * 64,
            )
        },
    )
    return repository, saved, variant, dataset


def _study(saved, variant, dataset, document_id: str = DOC, **kwargs):
    return build_analysis_study(
        document_id=document_id,
        title='Seat comparison',
        study_kind='measurement_comparison',
        bound_refs=kwargs.pop(
            'bound_refs',
            (
                StudyAuthorityRef(
                    kind='measurement_dataset',
                    ref_id=dataset.dataset_id,
                    ref_sha256=dataset.dataset_sha256,
                ),
                StudyAuthorityRef(
                    kind='target_curve',
                    ref_id='curve-1',
                    ref_sha256='b' * 64,
                ),
                StudyAuthorityRef(
                    kind='system_variant',
                    ref_id=variant.variant_id,
                    ref_sha256=variant.variant_sha256,
                ),
            ),
        ),
        presentation_spec=(
            StudySpecItem(key='range', value='20-20000'),
            StudySpecItem(key='overlay', value='target'),
        ),
        analysis_operations=kwargs.pop(
            'analysis_operations',
            (
                StudyAnalysisOperation(
                    operation='smoothing',
                    version='octave-1/6-v1',
                    parameters=(
                        StudySpecItem(key='fraction', value='1/6'),
                    ),
                ),
            ),
        ),
        notes=(
            make_study_note(
                kind='decision',
                text='Candidate B rejected: seat-2 dip beyond tolerance',
                created_at_utc='2026-09-24T00:00:00+00:00',
            ),
        ),
        scene_revision_id=kwargs.pop(
            'scene_revision_id', saved.revision.revision_id
        ),
        scene_content_hash=kwargs.pop(
            'scene_content_hash', saved.revision.content_hash
        ),
        system_variant_id=kwargs.pop(
            'system_variant_id', variant.variant_id
        ),
        system_variant_sha256=kwargs.pop(
            'system_variant_sha256', variant.variant_sha256
        ),
        created_at_utc='2026-09-24T00:00:00+00:00',
        **kwargs,
    )


def test_study_roundtrip_and_duplicate_refs_rejected(tmp_path: Path) -> None:
    _repository, saved, variant, dataset = _seed(tmp_path)
    study = _study(saved, variant, dataset)
    assert len(study.study_sha256) == 64
    restored = type(study).model_validate_json(study.model_dump_json())
    assert restored == study
    with pytest.raises(ValueError):
        build_analysis_study(
            document_id=DOC,
            title='dup',
            bound_refs=(
                StudyAuthorityRef(
                    kind='other', ref_id='r', ref_sha256='a' * 64
                ),
                StudyAuthorityRef(
                    kind='other', ref_id='r', ref_sha256='a' * 64
                ),
            ),
            created_at_utc='2026-09-24T00:00:00+00:00',
        )
    with pytest.raises(ValueError):
        StudyAuthorityRef(kind='other', ref_id='r')


def test_study_repository_append_only_and_chain(tmp_path: Path) -> None:
    repository, saved, variant, dataset = _seed(tmp_path)
    study = _study(saved, variant, dataset)
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
    assert [item.study_id for item in repository.list_studies(DOC)] == [
        study.study_id,
        clone.study_id,
    ]


def test_study_duplicate_requires_stored_prior(tmp_path: Path) -> None:
    repository, saved, variant, dataset = _seed(tmp_path)
    study = _study(saved, variant, dataset)
    clone = duplicate_analysis_study(
        study, created_at_utc='2026-09-24T01:00:00+00:00'
    )
    with pytest.raises(ValueError):
        repository.save_study(clone)


def test_study_save_resolves_exact_authorities(tmp_path: Path) -> None:
    repository, saved, variant, dataset = _seed(tmp_path)

    stale_pin = _study(
        saved,
        variant,
        dataset,
        bound_refs=(
            StudyAuthorityRef(
                kind='measurement_dataset',
                ref_id=dataset.dataset_id,
                ref_sha256='f' * 64,
            ),
        ),
    )
    with pytest.raises(ValueError):
        repository.save_study(stale_pin)

    ghost_ref = _study(
        saved,
        variant,
        dataset,
        bound_refs=(
            StudyAuthorityRef(
                kind='measurement_dataset',
                ref_id='ghost-dataset',
                ref_sha256='f' * 64,
            ),
        ),
    )
    with pytest.raises(ValueError):
        repository.save_study(ghost_ref)

    wrong_doc_variant = _study(
        saved,
        variant,
        dataset,
        system_variant_sha256='f' * 64,
    )
    with pytest.raises(ValueError):
        repository.save_study(wrong_doc_variant)

    no_variant_repo = CadAnalysisStudyRepository(
        SceneRepository(tmp_path / 'bare.sqlite3')
    )
    happy = _study(saved, variant, dataset)
    repository.save_study(happy)
    assert repository.get_study(happy.study_id) == happy
    assert no_variant_repo.get_study(happy.study_id) is None


def test_study_state_reproducible_not_current_broken(tmp_path: Path) -> None:
    _repository, saved, variant, dataset = _seed(tmp_path)
    study = _study(saved, variant, dataset)
    resolve_sha256 = {
        ('measurement_dataset', dataset.dataset_id): (
            dataset.dataset_sha256
        ),
        ('target_curve', 'curve-1'): 'b' * 64,
        ('system_variant', variant.variant_id): variant.variant_sha256,
    }
    report = evaluate_study_state(
        study,
        resolve_sha256=resolve_sha256,
        current_scene_revision_id=saved.revision.revision_id,
        current_system_variant_id=variant.variant_id,
    )
    assert report.state == 'reproducible'

    report = evaluate_study_state(
        study,
        resolve_sha256={
            **resolve_sha256,
            ('measurement_dataset', dataset.dataset_id): 'c' * 64,
        },
        current_scene_revision_id=saved.revision.revision_id,
        current_system_variant_id=variant.variant_id,
    )
    assert report.state == 'not_current'
    assert any(ref.state == 'stale' for ref in report.refs)

    report = evaluate_study_state(
        study,
        resolve_sha256={('target_curve', 'curve-1'): 'b' * 64},
    )
    assert report.state == 'broken_reference'
    assert any(ref.state == 'missing' for ref in report.refs)

    report = evaluate_study_state(
        study,
        resolve_sha256=resolve_sha256,
        current_scene_revision_id='rev-later',
        current_system_variant_id=variant.variant_id,
    )
    assert report.state == 'not_current'
    assert any('historical' in reason for reason in report.reasons)


def test_study_save_rejects_foreign_document_ref(tmp_path: Path) -> None:
    repository, saved, variant, dataset = _seed(tmp_path)
    foreign_repo = CadAnalysisStudyRepository(
        SceneRepository(tmp_path / 'foreign.sqlite3'),
        kind_resolvers={
            'target_curve': lambda ref_id: ResolvedAuthority(
                kind='target_curve',
                ref_id=ref_id,
                document_id='other-document',
                semantic_sha256='b' * 64,
            )
        },
    )
    study = _study(
        saved,
        variant,
        dataset,
        bound_refs=(
            StudyAuthorityRef(
                kind='target_curve',
                ref_id='curve-1',
                ref_sha256='b' * 64,
            ),
        ),
    )
    # The ref resolves but belongs to another project document — the
    # binding cannot satisfy this document's study.
    with pytest.raises(ValueError):
        foreign_repo.save_study(study)
    repository.save_study(study)


def test_operation_support_marks_unregistered_versions(
    tmp_path: Path,
) -> None:
    _repository, saved, variant, dataset = _seed(tmp_path)
    study = _study(saved, variant, dataset)

    report = evaluate_operation_support(
        study,
        supported_contracts={'smoothing': {'octave-1/6-v1'}},
    )
    assert report.regenerable is True
    assert report.operations[0].support == 'regenerable'

    report = evaluate_operation_support(
        study,
        supported_contracts={'smoothing': {'octave-1/3-v2'}},
    )
    assert report.regenerable is False
    assert report.operations[0].support == 'unsupported'
    assert 'unavailable' in report.operations[0].reason

    # The shipped registry is empty by default — no recorded operation is
    # silently regenerated under a newer implementation.
    assert STUDY_OPERATION_CONTRACTS == {}
    assert (
        evaluate_operation_support(study).regenerable is False
    )


def test_study_without_operations_is_regenerable(tmp_path: Path) -> None:
    _repository, saved, variant, dataset = _seed(tmp_path)
    study = _study(
        saved, variant, dataset, analysis_operations=()
    )
    assert evaluate_operation_support(study).regenerable is True
    assert evaluate_operation_support(study).operations == ()
