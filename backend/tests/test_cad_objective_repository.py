from __future__ import annotations

from contextlib import closing
import sqlite3

import pytest

from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_objective_models import (
    CadObjectiveEvaluation,
    CadObjectiveInputRef,
    CadParetoEvaluationRef,
    CadParetoSet,
    canonical_objective_sha256,
)
from htdt.cad_objectives import build_objective_evaluation, build_pareto_set
from htdt.cad_objective_repository import CadObjectiveRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    scene_content_hash,
)
from htdt.cad_search import build_cad_search_spec, generate_cad_candidates
from htdt.cad_search_models import CadSearchAxis
from htdt.cad_search_repository import CadSearchRepository
from htdt.optimization_objectives import (
    ObjectiveDefinition,
    ObjectiveMetric,
    ObjectiveValidDomain,
    ObjectiveVector,
)


DOCUMENT_ID = 'objective-repository-fixture'


def _scene() -> SceneDocument:
    return SceneDocument(
        document_id=DOCUMENT_ID,
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.22, y_m=0.28, z_m=0.42),
                speaker_role='FL',
            ),
        ),
    )


def _fixture(tmp_path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(_scene(), parent_revision_id=None).revision
    spec, _estimate = build_cad_search_spec(
        revision,
        CadConstraintSet(document_id=DOCUMENT_ID, constraints=()),
        (CadSearchAxis(entity_id='speaker-fl', axis='x', min_m=1.0, max_m=3.0, step_m=1.0),),
        candidate_limit=10,
        name='objective fixture',
    )
    search_repository = CadSearchRepository(scene_repository)
    search_repository.save(spec)
    candidates = generate_cad_candidates(scene_repository, spec).candidates
    objective_repository = CadObjectiveRepository(scene_repository, search_repository)
    return scene_repository, revision, spec, candidates, objective_repository


def _vector(candidate_id: str, response: float, movement: float) -> ObjectiveVector:
    return ObjectiveVector(
        candidate_id=candidate_id,
        metrics=(
            ObjectiveMetric(objective_id='response.shape_rms_db', value=response, unit='dB'),
            ObjectiveMetric(objective_id='movement.total_m', value=movement, unit='m'),
        ),
    )


def _evaluation(revision, spec, candidate_id: str, response: float, movement: float):
    return build_objective_evaluation(
        revision,
        spec,
        candidate_id,
        _vector(candidate_id, response, movement),
        evaluation_spec={
            'algorithm_version': 'objective-vector-1',
            'objectives': ['response.shape_rms_db', 'movement.total_m'],
            'response_band_hz': [20.0, 160.0],
        },
        input_refs=(
            CadObjectiveInputRef(
                evidence_class='derived',
                source_kind='candidate_geometry',
                source_id=candidate_id,
            ),
            CadObjectiveInputRef(
                evidence_class='predicted',
                source_kind='prediction_fixture',
                source_id=f'prediction:{candidate_id}',
            ),
        ),
    )


def test_objective_evaluation_round_trip_is_bound_to_scene_and_search_spec(tmp_path) -> None:
    _scene_repo, revision, spec, candidates, repository = _fixture(tmp_path)
    candidate = candidates[0]
    evaluation = _evaluation(revision, spec, candidate.candidate_id, 2.0, 1.0)

    repository.save_evaluation(evaluation)

    assert repository.get_evaluation(evaluation.evaluation_id) == evaluation
    assert repository.list_evaluations(spec.search_spec_id) == (evaluation,)
    assert evaluation.vector.candidate_id == candidate.candidate_id
    assert evaluation.evaluation_spec_sha256
    assert evaluation.evaluation_sha256


def test_pareto_set_round_trip_recomputes_from_immutable_evaluations(tmp_path) -> None:
    _scene_repo, revision, spec, candidates, repository = _fixture(tmp_path)
    evaluations = (
        _evaluation(revision, spec, candidates[0].candidate_id, 1.0, 3.0),
        _evaluation(revision, spec, candidates[1].candidate_id, 2.0, 2.0),
        _evaluation(revision, spec, candidates[2].candidate_id, 3.0, 3.0),
    )
    for evaluation in evaluations:
        repository.save_evaluation(evaluation)

    pareto_set = build_pareto_set(
        evaluations,
        ('response.shape_rms_db', 'movement.total_m'),
    )
    repository.save_pareto_set(pareto_set)

    assert pareto_set.result.non_dominated_candidate_ids == (
        candidates[0].candidate_id,
        candidates[1].candidate_id,
    )
    assert repository.get_pareto_set(pareto_set.pareto_set_id) == pareto_set
    assert repository.find_pareto_set_by_sha(spec.search_spec_id, pareto_set.pareto_sha256) == pareto_set
    assert repository.list_pareto_sets(spec.search_spec_id) == (pareto_set,)


def test_objective_repository_rejects_tampered_search_binding(tmp_path) -> None:
    _scene_repo, revision, spec, candidates, repository = _fixture(tmp_path)
    evaluation = _evaluation(revision, spec, candidates[0].candidate_id, 1.0, 1.0)
    tampered = evaluation.model_copy(update={'search_spec_sha256': '0' * 64})

    with pytest.raises(ValueError, match='SearchSpec hash mismatch'):
        repository.save_evaluation(tampered)


def test_pareto_repository_rejects_result_not_matching_evaluations(tmp_path) -> None:
    _scene_repo, revision, spec, candidates, repository = _fixture(tmp_path)
    evaluations = (
        _evaluation(revision, spec, candidates[0].candidate_id, 1.0, 3.0),
        _evaluation(revision, spec, candidates[1].candidate_id, 2.0, 2.0),
    )
    for evaluation in evaluations:
        repository.save_evaluation(evaluation)

    pareto_set = build_pareto_set(
        evaluations,
        ('response.shape_rms_db', 'movement.total_m'),
    )
    wrong_result = pareto_set.result.model_copy(
        update={'non_dominated_candidate_ids': (candidates[0].candidate_id,)}
    )
    tampered_payload = pareto_set.identity_payload()
    tampered_payload['result'] = wrong_result.model_dump(mode='json')

    tampered = pareto_set.model_copy(update={
        'result': wrong_result,
        'pareto_sha256': canonical_objective_sha256(tampered_payload),
    })
    with pytest.raises(ValueError, match='does not match referenced objective evaluations'):
        repository.save_pareto_set(tampered)


def test_objective_input_ref_order_is_canonical(tmp_path) -> None:
    _scene_repo, revision, spec, candidates, _repository = _fixture(tmp_path)
    candidate_id = candidates[0].candidate_id
    vector = _vector(candidate_id, 1.0, 1.0)
    refs = (
        CadObjectiveInputRef(
            evidence_class='predicted',
            source_kind='prediction_fixture',
            source_id='prediction-a',
        ),
        CadObjectiveInputRef(
            evidence_class='derived',
            source_kind='candidate_geometry',
            source_id=candidate_id,
        ),
    )
    first = build_objective_evaluation(
        revision,
        spec,
        candidate_id,
        vector,
        evaluation_spec={'objectives': ['response.shape_rms_db', 'movement.total_m']},
        input_refs=refs,
    )
    second = build_objective_evaluation(
        revision,
        spec,
        candidate_id,
        vector,
        evaluation_spec={'objectives': ['response.shape_rms_db', 'movement.total_m']},
        input_refs=tuple(reversed(refs)),
    )

    assert first.input_refs == second.input_refs
    assert first.evaluation_sha256 == second.evaluation_sha256



def test_explicit_objective_definition_identity_survives_repository_reopen(tmp_path) -> None:
    scene_repository, revision, spec, candidates, repository = _fixture(tmp_path)
    candidate_id = candidates[0].candidate_id
    definition = ObjectiveDefinition(
        objective_id='fixture.coverage',
        quantity='coverage_fraction',
        unit='1',
        direction='maximize',
        valid_domain=ObjectiveValidDomain(
            kind='bounded_real',
            minimum=0.0,
            maximum=1.0,
        ),
        comparison_model_id='fixture-coverage-model',
        comparison_model_version='1',
    )
    vector = ObjectiveVector(
        candidate_id=candidate_id,
        metrics=(
            ObjectiveMetric(
                objective_id=definition.objective_id,
                value=0.75,
                unit=definition.unit,
                direction=definition.direction,
                definition=definition,
            ),
        ),
    )
    refs = (
        CadObjectiveInputRef(
            evidence_class='predicted',
            source_kind='prediction_fixture',
            source_id=f'prediction:{candidate_id}',
        ),
    )
    first = build_objective_evaluation(
        revision,
        spec,
        candidate_id,
        vector,
        evaluation_spec={
            'algorithm_version': 'fixture-maximize-1',
            'objectives': ['fixture.coverage'],
        },
        input_refs=refs,
    )
    second = build_objective_evaluation(
        revision,
        spec,
        candidate_id,
        vector,
        evaluation_spec={
            'algorithm_version': 'fixture-maximize-1',
            'objectives': ['fixture.coverage'],
        },
        input_refs=refs,
    )
    assert first.evaluation_sha256 == second.evaluation_sha256

    repository.save_evaluation(first)
    reopened_repository = CadObjectiveRepository(
        scene_repository,
        CadSearchRepository(scene_repository),
    )
    reopened = reopened_repository.get_evaluation(first.evaluation_id)

    assert reopened == first
    assert reopened is not None
    reopened_metric = reopened.vector.metric(definition.objective_id)
    assert reopened_metric.definition is not None
    assert reopened_metric.definition.definition_id == definition.definition_id
    assert reopened_metric.direction == 'maximize'


def _persisted_pareto(tmp_path):
    scene_repository, revision, spec, candidates, repository = _fixture(tmp_path)
    evaluations = (
        _evaluation(revision, spec, candidates[0].candidate_id, 1.0, 3.0),
        _evaluation(revision, spec, candidates[1].candidate_id, 2.0, 2.0),
        _evaluation(revision, spec, candidates[2].candidate_id, 3.0, 3.0),
    )
    for evaluation in evaluations:
        repository.save_evaluation(evaluation)
    pareto_set = build_pareto_set(
        evaluations,
        ('response.shape_rms_db', 'movement.total_m'),
    )
    repository.save_pareto_set(pareto_set)
    return (
        scene_repository,
        revision,
        spec,
        candidates,
        repository,
        evaluations,
        pareto_set,
    )


def _rehashed_pareto_set(pareto_set: CadParetoSet, **updates) -> CadParetoSet:
    """Coherently recompute the self hash of a modified Pareto set."""
    candidate = pareto_set.model_copy(update=updates)
    digest = canonical_objective_sha256(candidate.identity_payload())
    return CadParetoSet.model_validate(
        candidate.model_copy(
            update={'pareto_sha256': digest}
        ).model_dump(mode='python')
    )


def _rehashed_evaluation(
    evaluation: CadObjectiveEvaluation,
    **updates,
) -> CadObjectiveEvaluation:
    """Coherently recompute the self hash of a modified evaluation."""
    candidate = evaluation.model_copy(update=updates)
    digest = canonical_objective_sha256(candidate.identity_payload())
    return CadObjectiveEvaluation.model_validate(
        candidate.model_copy(
            update={'evaluation_sha256': digest}
        ).model_dump(mode='python')
    )


def _replace_persisted_pareto_set(
    path,
    pareto_set_id: str,
    persisted: CadParetoSet,
) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            '''
            UPDATE cad_pareto_sets
            SET pareto_set_id=?, document_id=?, scene_revision_id=?,
                scene_content_hash=?, search_spec_id=?, search_spec_sha256=?,
                pareto_sha256=?, payload_json=?, created_at_utc=?
            WHERE pareto_set_id=?
            ''',
            (
                persisted.pareto_set_id,
                persisted.document_id,
                persisted.scene_revision_id,
                persisted.scene_content_hash,
                persisted.search_spec_id,
                persisted.search_spec_sha256,
                persisted.pareto_sha256,
                persisted.model_dump_json(),
                persisted.created_at_utc,
                pareto_set_id,
            ),
        )


def _replace_persisted_evaluation(
    path,
    evaluation_id: str,
    persisted: CadObjectiveEvaluation,
) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            '''
            UPDATE cad_objective_evaluations
            SET evaluation_id=?, evaluation_sha256=?, candidate_id=?, payload_json=?
            WHERE evaluation_id=?
            ''',
            (
                persisted.evaluation_id,
                persisted.evaluation_sha256,
                persisted.candidate_id,
                persisted.model_dump_json(),
                evaluation_id,
            ),
        )


def test_pareto_read_apis_replay_valid_sets_unchanged_after_reopen(tmp_path) -> None:
    scene_repository, _revision, spec, _candidates, _repository, _evaluations, pareto_set = (
        _persisted_pareto(tmp_path)
    )
    reopened_repository = CadObjectiveRepository(
        scene_repository,
        CadSearchRepository(scene_repository),
    )

    assert reopened_repository.get_pareto_set(pareto_set.pareto_set_id) == pareto_set
    assert reopened_repository.find_pareto_set_by_sha(
        spec.search_spec_id, pareto_set.pareto_sha256
    ) == pareto_set
    assert reopened_repository.list_pareto_sets(spec.search_spec_id) == (pareto_set,)


def test_pareto_read_apis_replay_and_reject_rehashed_result_tampering(tmp_path) -> None:
    scene_repository, _revision, spec, candidates, repository, _evaluations, pareto_set = (
        _persisted_pareto(tmp_path)
    )
    assert repository.get_pareto_set(pareto_set.pareto_set_id) == pareto_set

    # Promote a dominated candidate into the non-dominated set, then rehash.
    tampered_result = pareto_set.result.model_copy(update={
        'non_dominated_candidate_ids': (
            *pareto_set.result.non_dominated_candidate_ids,
            candidates[2].candidate_id,
        ),
    })
    tampered = _rehashed_pareto_set(pareto_set, result=tampered_result)
    assert tampered.pareto_sha256 != pareto_set.pareto_sha256
    _replace_persisted_pareto_set(
        scene_repository.path, pareto_set.pareto_set_id, tampered
    )

    with pytest.raises(ValueError, match='does not match referenced objective evaluations'):
        repository.get_pareto_set(pareto_set.pareto_set_id)
    with pytest.raises(ValueError, match='does not match referenced objective evaluations'):
        repository.list_pareto_sets(spec.search_spec_id)
    with pytest.raises(ValueError, match='does not match referenced objective evaluations'):
        repository.find_pareto_set_by_sha(spec.search_spec_id, tampered.pareto_sha256)
    assert (
        repository.find_pareto_set_by_sha(spec.search_spec_id, pareto_set.pareto_sha256)
        is None
    )


def test_pareto_read_apis_reject_real_but_incompatible_evaluation_ref(tmp_path) -> None:
    scene_repository, revision, spec, candidates, repository, _evaluations, pareto_set = (
        _persisted_pareto(tmp_path)
    )
    # A real persisted O30 record bound to a different SearchSpec.
    search_repository = CadSearchRepository(scene_repository)
    other_spec, _estimate = build_cad_search_spec(
        revision,
        CadConstraintSet(document_id=DOCUMENT_ID, constraints=()),
        (
            CadSearchAxis(
                entity_id='speaker-fl', axis='x', min_m=1.0, max_m=2.0, step_m=1.0
            ),
        ),
        candidate_limit=10,
        name='incompatible fixture',
    )
    search_repository.save(other_spec)
    other_candidates = generate_cad_candidates(scene_repository, other_spec).candidates
    other_evaluation = _evaluation(
        revision, other_spec, other_candidates[0].candidate_id, 0.5, 0.5
    )
    repository.save_evaluation(other_evaluation)
    assert other_evaluation.candidate_id not in {
        ref.candidate_id for ref in pareto_set.evaluations
    }

    refs = list(pareto_set.evaluations)
    refs[2] = CadParetoEvaluationRef(
        evaluation_id=other_evaluation.evaluation_id,
        evaluation_sha256=other_evaluation.evaluation_sha256,
        candidate_id=other_evaluation.candidate_id,
    )
    dominated_by = dict(pareto_set.result.dominated_by)
    dominated_by[other_evaluation.candidate_id] = dominated_by.pop(
        candidates[2].candidate_id
    )
    tampered = _rehashed_pareto_set(
        pareto_set,
        evaluations=tuple(refs),
        result=pareto_set.result.model_copy(update={'dominated_by': dominated_by}),
    )
    _replace_persisted_pareto_set(
        scene_repository.path, pareto_set.pareto_set_id, tampered
    )

    with pytest.raises(ValueError, match='objective evaluation binding mismatch'):
        repository.get_pareto_set(pareto_set.pareto_set_id)
    with pytest.raises(ValueError, match='objective evaluation binding mismatch'):
        repository.list_pareto_sets(spec.search_spec_id)
    with pytest.raises(ValueError, match='objective evaluation binding mismatch'):
        repository.find_pareto_set_by_sha(spec.search_spec_id, tampered.pareto_sha256)
    # The shared validator rejects the same payload on save.
    with pytest.raises(ValueError, match='objective evaluation binding mismatch'):
        repository.save_pareto_set(tampered)


def test_pareto_read_apis_fail_closed_when_objective_evaluation_disappears(tmp_path) -> None:
    scene_repository, _revision, spec, _candidates, repository, evaluations, pareto_set = (
        _persisted_pareto(tmp_path)
    )
    with closing(sqlite3.connect(scene_repository.path)) as connection, connection:
        connection.execute('PRAGMA foreign_keys=OFF')
        connection.execute(
            'DELETE FROM cad_objective_evaluations WHERE evaluation_id=?',
            (evaluations[0].evaluation_id,),
        )

    with pytest.raises(ValueError, match='Pareto objective evaluation does not exist'):
        repository.get_pareto_set(pareto_set.pareto_set_id)
    with pytest.raises(ValueError, match='Pareto objective evaluation does not exist'):
        repository.list_pareto_sets(spec.search_spec_id)
    with pytest.raises(ValueError, match='Pareto objective evaluation does not exist'):
        repository.find_pareto_set_by_sha(spec.search_spec_id, pareto_set.pareto_sha256)


def test_pareto_read_apis_fail_closed_when_objective_evaluation_is_rewritten(tmp_path) -> None:
    scene_repository, _revision, spec, _candidates, repository, evaluations, pareto_set = (
        _persisted_pareto(tmp_path)
    )
    original = evaluations[0]
    rewritten = _rehashed_evaluation(
        original,
        vector=_vector(original.candidate_id, 9.9, 0.1),
    )
    assert rewritten.evaluation_sha256 != original.evaluation_sha256
    _replace_persisted_evaluation(
        scene_repository.path, original.evaluation_id, rewritten
    )

    with pytest.raises(ValueError, match='Pareto objective evaluation hash mismatch'):
        repository.get_pareto_set(pareto_set.pareto_set_id)
    with pytest.raises(ValueError, match='Pareto objective evaluation hash mismatch'):
        repository.list_pareto_sets(spec.search_spec_id)
    with pytest.raises(ValueError, match='Pareto objective evaluation hash mismatch'):
        repository.find_pareto_set_by_sha(spec.search_spec_id, pareto_set.pareto_sha256)


def test_pareto_read_apis_reject_row_that_disagrees_with_payload(tmp_path) -> None:
    scene_repository, _revision, spec, _candidates, repository, _evaluations, pareto_set = (
        _persisted_pareto(tmp_path)
    )
    with closing(sqlite3.connect(scene_repository.path)) as connection, connection:
        connection.execute(
            'UPDATE cad_pareto_sets SET pareto_sha256=? WHERE pareto_set_id=?',
            ('0' * 64, pareto_set.pareto_set_id),
        )

    with pytest.raises(ValueError, match='row disagrees with its payload'):
        repository.get_pareto_set(pareto_set.pareto_set_id)
    with pytest.raises(ValueError, match='row disagrees with its payload'):
        repository.list_pareto_sets(spec.search_spec_id)
    with pytest.raises(ValueError, match='row disagrees with its payload'):
        repository.find_pareto_set_by_sha(spec.search_spec_id, '0' * 64)
    assert (
        repository.find_pareto_set_by_sha(spec.search_spec_id, pareto_set.pareto_sha256)
        is None
    )


def test_pareto_read_apis_fail_closed_when_scene_authority_is_rewritten(tmp_path) -> None:
    scene_repository, revision, spec, _candidates, repository, _evaluations, pareto_set = (
        _persisted_pareto(tmp_path)
    )
    mutated = _scene().model_copy(
        update={'room': RoomPrism(width_m=7.0, depth_m=5.0, height_m=3.0)}
    )
    with closing(sqlite3.connect(scene_repository.path)) as connection, connection:
        connection.execute(
            'UPDATE scene_revisions SET payload_json=?, content_hash=? WHERE revision_id=?',
            (
                mutated.model_dump_json(),
                scene_content_hash(mutated),
                revision.revision_id,
            ),
        )

    with pytest.raises(ValueError, match='content hash does not match revision'):
        repository.get_pareto_set(pareto_set.pareto_set_id)
    with pytest.raises(ValueError, match='content hash does not match revision'):
        repository.list_pareto_sets(spec.search_spec_id)
    with pytest.raises(ValueError, match='content hash does not match revision'):
        repository.find_pareto_set_by_sha(spec.search_spec_id, pareto_set.pareto_sha256)
