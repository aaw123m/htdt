from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
import sqlite3

import pytest

from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality import dataset_sha256
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import canonical_json as canonical_measurement_json
from htdt.cad_measurements import measurement_record_for_revision
from htdt.cad_objective_authority import ResolvedObjectiveInput
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
from htdt.cad_search import (
    build_cad_search_spec,
    candidate_preview_document,
    generate_cad_candidates,
)
from htdt.cad_search_models import CadSearchAxis
from htdt.cad_search_repository import CadSearchRepository
from htdt.comparison import FrequencyResponse
from htdt.optimization_objectives import (
    ObjectiveDefinition,
    ObjectiveMetric,
    ObjectiveValidDomain,
    ObjectiveVector,
    ResponseObjectiveSpec,
    movement_objectives,
    target_response_objectives,
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
            SceneEntity(
                entity_id='listener-main',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )


def _fr(offset: float, tilt: float = 0.0) -> FrequencyResponse:
    return FrequencyResponse(
        frequency_hz=(20.0, 40.0, 80.0, 160.0),
        level_db=(
            80.0 + offset,
            81.0 + tilt + offset,
            79.0 + offset,
            80.0 + 0.5 * tilt + offset,
        ),
    )


_TARGET = FrequencyResponse(
    frequency_hz=(20.0, 40.0, 80.0, 160.0),
    level_db=(80.0, 81.0, 79.0, 80.0),
)
_RESPONSE_SPEC = ResponseObjectiveSpec(
    low_hz=20.0,
    high_hz=160.0,
    reference_band_hz=(20.0, 160.0),
)
_TARGET_SPEC = {
    'algorithm_version': 'objective-vector-1',
    'objective_method': 'target_response',
    'objectives': ['response.rms_difference_db', 'response.shape_rms_db'],
    'response_band_hz': [20.0, 160.0],
    'reference_band_hz': [20.0, 160.0],
    'excluded_bands': [],
    'target_response': {
        'frequency_hz': list(_TARGET.frequency_hz),
        'level_db': list(_TARGET.level_db),
    },
}


def _response_sha256(response: FrequencyResponse) -> str:
    return canonical_objective_sha256({
        'frequency_hz': list(response.frequency_hz),
        'level_db': list(response.level_db),
    })


def _prediction_resolver(responses: dict[str, FrequencyResponse]):
    """Test fixture resolver: prediction_fixture ids pin exact responses."""

    def resolve(context, ref: CadObjectiveInputRef) -> ResolvedObjectiveInput:
        if ref.evidence_class != 'predicted':
            raise ValueError('prediction_fixture evidence class must be predicted')
        response = responses.get(ref.source_id)
        if response is None:
            raise ValueError(
                f'prediction fixture evidence does not exist: {ref.source_id}'
            )
        return ResolvedObjectiveInput(
            ref=ref,
            source_sha256=_response_sha256(response),
            response=response,
        )

    return resolve


def _objective_repository(
    scene_repository: SceneRepository,
    search_repository: CadSearchRepository,
    responses: dict[str, FrequencyResponse],
    **kwargs,
) -> CadObjectiveRepository:
    resolvers = {'prediction_fixture': _prediction_resolver(responses)}
    resolvers.update(kwargs.pop('input_resolvers', {}))
    return CadObjectiveRepository(
        scene_repository,
        search_repository,
        input_resolvers=resolvers,
        **kwargs,
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
    responses: dict[str, FrequencyResponse] = {}
    objective_repository = _objective_repository(
        scene_repository,
        search_repository,
        responses,
    )
    return scene_repository, revision, spec, candidates, objective_repository, responses


def _vector(candidate_id: str, response: FrequencyResponse) -> ObjectiveVector:
    full = target_response_objectives(
        candidate_id,
        response,
        _TARGET,
        _RESPONSE_SPEC,
    )
    return ObjectiveVector(
        candidate_id=candidate_id,
        metrics=(
            full.metric('response.rms_difference_db'),
            full.metric('response.shape_rms_db'),
        ),
    )


def _evaluation(
    revision,
    spec,
    responses: dict[str, FrequencyResponse],
    candidate_id: str,
    offset: float,
    tilt: float,
):
    """Build an evaluation whose vector is derived from fixture evidence."""

    response = _fr(offset, tilt)
    source_id = f'prediction:{candidate_id}'
    responses[source_id] = response
    return build_objective_evaluation(
        revision,
        spec,
        candidate_id,
        _vector(candidate_id, response),
        evaluation_spec=_TARGET_SPEC,
        input_refs=(
            CadObjectiveInputRef(
                evidence_class='derived',
                source_kind='candidate_geometry',
                source_id=candidate_id,
            ),
            CadObjectiveInputRef(
                evidence_class='predicted',
                source_kind='prediction_fixture',
                source_id=source_id,
                source_sha256=_response_sha256(response),
            ),
        ),
    )


def _movement_vector(candidate_id: str, revision, candidate) -> ObjectiveVector:
    baseline = {}
    for entity_id in candidate.positions:
        entity = revision.document.entity(entity_id)
        baseline[entity_id] = {
            'x_m': float(entity.position.x_m),
            'y_m': float(entity.position.y_m),
            'z_m': float(entity.position.z_m),
        }
    return movement_objectives(candidate_id, baseline, candidate.positions)


def _movement_evaluation(revision, spec, candidate):
    return build_objective_evaluation(
        revision,
        spec,
        candidate.candidate_id,
        _movement_vector(candidate.candidate_id, revision, candidate),
        evaluation_spec={
            'algorithm_version': 'objective-vector-1',
            'objective_method': 'candidate_movement',
            'objectives': ['movement.total_m', 'movement.max_m'],
        },
        input_refs=(
            CadObjectiveInputRef(
                evidence_class='derived',
                source_kind='candidate_geometry',
                source_id=candidate.candidate_id,
            ),
        ),
    )


def _save_measurement(
    measurement_repository: CadMeasurementRepository,
    revision,
    *,
    measurement_id: str,
    offset: float = 0.0,
    tilt: float = 0.0,
) -> CadFrequencyResponseDataset:
    response = _fr(offset, tilt)
    raw_payload = {
        'measurement_id': measurement_id,
        'response': {
            'frequency_hz': list(response.frequency_hz),
            'level_db': list(response.level_db),
        },
    }
    raw = canonical_measurement_json(raw_payload).encode('utf-8')
    record = measurement_record_for_revision(
        revision,
        'listener-main',
        measurement_id=measurement_id,
        evidence_type='measured',
        channel_role='FL',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
        routing_evidence='verified',
        captured_at='2030-01-01T00:00:00+00:00',
        imported_at='2030-01-01T00:00:00+00:00',
        source_kind='unknown',
        quality_status='synthetic_fixture',
        quality_reasons=('not_physical_measurement',),
        quality_source='objective-repository-test',
        provenance={'validation_scope': 'synthetic_fixture'},
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f'dataset:{measurement_id}',
        measurement_id=measurement_id,
        frequency_hz=response.frequency_hz,
        level_db=response.level_db,
        phase_deg=None,
        phase_status='absent',
        level_reference='synthetic_fixture',
        smoothing='None',
        processing_json='{}',
        source_sha256=sha256(raw).hexdigest(),
        importer_version='objective-repository-test-1',
    )
    measurement_repository.save(
        record,
        dataset,
        raw_filename=f'{measurement_id}.json',
        raw_bytes=raw,
    )
    return dataset


def test_objective_evaluation_round_trip_is_bound_to_scene_and_search_spec(tmp_path) -> None:
    _scene_repo, revision, spec, candidates, repository, responses = _fixture(tmp_path)
    candidate = candidates[0]
    evaluation = _evaluation(revision, spec, responses, candidate.candidate_id, 1.0, 3.0)

    repository.save_evaluation(evaluation)

    assert repository.get_evaluation(evaluation.evaluation_id) == evaluation
    assert repository.list_evaluations(spec.search_spec_id) == (evaluation,)
    assert evaluation.vector.candidate_id == candidate.candidate_id
    assert evaluation.evaluation_spec_sha256
    assert evaluation.evaluation_sha256


def test_objective_evaluation_round_trip_replays_after_reopen(tmp_path) -> None:
    scene_repository, revision, spec, candidates, repository, responses = _fixture(tmp_path)
    evaluation = _evaluation(
        revision, spec, responses, candidates[0].candidate_id, 1.0, 3.0
    )
    repository.save_evaluation(evaluation)

    reopened_repository = _objective_repository(
        scene_repository,
        CadSearchRepository(scene_repository),
        responses,
    )
    assert reopened_repository.get_evaluation(evaluation.evaluation_id) == evaluation
    assert reopened_repository.latest_evaluations_by_candidate(
        spec.search_spec_id
    ) == (evaluation,)


def test_objective_evaluation_rejects_candidate_outside_canonical_set(tmp_path) -> None:
    scene_repository, revision, spec, candidates, repository, responses = _fixture(tmp_path)

    fabricated = _evaluation(
        revision, spec, responses, 'pc-fabricated-member', 1.0, 1.0
    )
    with pytest.raises(ValueError, match='not a member of the SearchSpec'):
        repository.save_evaluation(fabricated)

    # A real member of a different SearchSpec is still a non-member here.
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
        name='other objective fixture',
    )
    search_repository.save(other_spec)
    other_candidates = generate_cad_candidates(scene_repository, other_spec).candidates
    foreign = _evaluation(
        revision, spec, responses, other_candidates[0].candidate_id, 1.0, 1.0
    )
    with pytest.raises(ValueError, match='not a member of the SearchSpec'):
        repository.save_evaluation(foreign)


def test_objective_evaluation_rejects_fabricated_vector_on_save_and_reads(tmp_path) -> None:
    scene_repository, revision, spec, candidates, repository, responses = _fixture(tmp_path)
    evaluation = _evaluation(
        revision, spec, responses, candidates[0].candidate_id, 1.0, 3.0
    )

    # Same ids, altered values, coherent self-hash: still rejected on save.
    forged_vector = evaluation.vector.model_copy(update={
        'metrics': (
            evaluation.vector.metrics[0].model_copy(update={'value': 99.0}),
            evaluation.vector.metrics[1],
        ),
    })
    forged = _rehashed_evaluation(evaluation, vector=forged_vector)
    with pytest.raises(ValueError, match='does not reproduce from persisted evidence'):
        repository.save_evaluation(forged)

    repository.save_evaluation(evaluation)
    assert repository.get_evaluation(evaluation.evaluation_id) == evaluation

    # Coherently rewriting the persisted row fails closed on every read path.
    _replace_persisted_evaluation(
        scene_repository.path, evaluation.evaluation_id, forged
    )
    with pytest.raises(ValueError, match='does not reproduce from persisted evidence'):
        repository.get_evaluation(evaluation.evaluation_id)
    with pytest.raises(ValueError, match='does not reproduce from persisted evidence'):
        repository.list_evaluations(spec.search_spec_id)
    with pytest.raises(ValueError, match='does not reproduce from persisted evidence'):
        repository.latest_evaluations_by_candidate(spec.search_spec_id)
    # The raw diagnostic view still exposes the tampered payload.
    assert repository.inspect_evaluation(evaluation.evaluation_id) == forged


def test_objective_evaluation_rejects_missing_or_mismatched_input_evidence(tmp_path) -> None:
    scene_repository, revision, spec, candidates, repository, responses = _fixture(tmp_path)
    candidate = candidates[0]

    def _forged(**ref_updates) -> CadObjectiveEvaluation:
        ref = CadObjectiveInputRef(
            evidence_class='predicted',
            source_kind='prediction_fixture',
            source_id=f'prediction:{candidate.candidate_id}',
            **ref_updates,
        )
        evaluation = _evaluation(
            revision, spec, responses, candidate.candidate_id, 1.0, 1.0
        )
        return _rehashed_evaluation(
            evaluation,
            input_refs=(
                CadObjectiveInputRef(
                    evidence_class='derived',
                    source_kind='candidate_geometry',
                    source_id=candidate.candidate_id,
                ),
                ref,
            ),
        )

    with pytest.raises(ValueError, match='source hash mismatch'):
        repository.save_evaluation(_forged(source_sha256='0' * 64))

    unknown_measurement = _rehashed_evaluation(
        _evaluation(revision, spec, responses, candidate.candidate_id, 1.0, 1.0),
        input_refs=(
            CadObjectiveInputRef(
                evidence_class='measured',
                source_kind='cad_measurement',
                source_id='meas:missing',
            ),
        ),
    )
    with pytest.raises(ValueError, match='measured evidence does not exist'):
        repository.save_evaluation(unknown_measurement)

    unknown_attempt = _rehashed_evaluation(
        _evaluation(revision, spec, responses, candidate.candidate_id, 1.0, 1.0),
        input_refs=(
            CadObjectiveInputRef(
                evidence_class='predicted',
                source_kind='cad_roomsim_attempt',
                source_id='attempt:missing',
            ),
        ),
    )
    with pytest.raises(ValueError, match='predicted evidence attempt does not exist'):
        repository.save_evaluation(unknown_attempt)

    unknown_kind = _rehashed_evaluation(
        _evaluation(revision, spec, responses, candidate.candidate_id, 1.0, 1.0),
        input_refs=(
            CadObjectiveInputRef(
                evidence_class='predicted',
                source_kind='unregistered_fixture',
                source_id='fixture:1',
            ),
        ),
    )
    with pytest.raises(ValueError, match='no registered authority'):
        repository.save_evaluation(unknown_kind)


def test_objective_evaluation_rejects_unknown_spec_authority(tmp_path) -> None:
    scene_repository, revision, spec, candidates, repository, responses = _fixture(tmp_path)
    candidate = candidates[0]
    response = _fr(1.0)
    responses[f'prediction:{candidate.candidate_id}'] = response
    evaluation = build_objective_evaluation(
        revision,
        spec,
        candidate.candidate_id,
        _vector(candidate.candidate_id, response),
        evaluation_spec={
            'algorithm_version': 'objective-vector-1',
            'objectives': ['response.rms_difference_db'],
        },
        input_refs=(
            CadObjectiveInputRef(
                evidence_class='predicted',
                source_kind='prediction_fixture',
                source_id=f'prediction:{candidate.candidate_id}',
            ),
        ),
    )
    with pytest.raises(ValueError, match='no replayable objective authority'):
        repository.save_evaluation(evaluation)


def test_objective_evaluation_rejects_tampered_authority_columns(tmp_path) -> None:
    scene_repository, revision, spec, candidates, repository, responses = _fixture(tmp_path)
    evaluation = _evaluation(
        revision, spec, responses, candidates[0].candidate_id, 1.0, 3.0
    )
    repository.save_evaluation(evaluation)

    with closing(sqlite3.connect(scene_repository.path)) as connection, connection:
        connection.execute(
            "UPDATE cad_objective_evaluations SET candidate_set_sha256=? WHERE evaluation_id=?",
            ('0' * 64, evaluation.evaluation_id),
        )
    with pytest.raises(ValueError, match='candidate-set authority mismatch'):
        repository.get_evaluation(evaluation.evaluation_id)

    with closing(sqlite3.connect(scene_repository.path)) as connection, connection:
        connection.execute(
            "UPDATE cad_objective_evaluations SET candidate_set_sha256=NULL WHERE evaluation_id=?",
            (evaluation.evaluation_id,),
        )
    with pytest.raises(ValueError, match='non-authoritative'):
        repository.get_evaluation(evaluation.evaluation_id)


def test_objective_evaluation_legacy_row_is_non_authoritative(tmp_path) -> None:
    scene_repository, revision, spec, candidates, repository, responses = _fixture(tmp_path)
    evaluation = _evaluation(
        revision, spec, responses, candidates[0].candidate_id, 1.0, 3.0
    )
    repository.save_evaluation(evaluation)
    with closing(sqlite3.connect(scene_repository.path)) as connection, connection:
        connection.execute(
            'UPDATE cad_objective_evaluations '
            'SET candidate_set_sha256=NULL, input_authorities_json=NULL '
            'WHERE evaluation_id=?',
            (evaluation.evaluation_id,),
        )

    with pytest.raises(ValueError, match='non-authoritative'):
        repository.get_evaluation(evaluation.evaluation_id)
    with pytest.raises(ValueError, match='non-authoritative'):
        repository.list_evaluations(spec.search_spec_id)
    # Raw inspection stays available for diagnostics without attesting.
    assert repository.inspect_evaluation(evaluation.evaluation_id) == evaluation


def test_movement_objective_evaluation_replays_from_candidate_geometry(tmp_path) -> None:
    scene_repository, revision, spec, candidates, repository, _responses = _fixture(tmp_path)
    candidate = candidates[1]
    evaluation = _movement_evaluation(revision, spec, candidate)

    repository.save_evaluation(evaluation)

    assert repository.get_evaluation(evaluation.evaluation_id) == evaluation

    forged_vector = evaluation.vector.model_copy(update={
        'metrics': (
            evaluation.vector.metrics[0].model_copy(update={'value': 0.001}),
            evaluation.vector.metrics[1],
        ),
    })
    forged = _rehashed_evaluation(evaluation, vector=forged_vector)
    with pytest.raises(ValueError, match='does not reproduce from persisted evidence'):
        repository.save_evaluation(forged)


def test_measured_objective_evaluation_replays_from_persisted_dataset(tmp_path) -> None:
    scene_repository, revision, spec, candidates, repository, _responses = _fixture(tmp_path)
    measurement_repository = CadMeasurementRepository(scene_repository)
    candidate = candidates[1]
    applied_revision = scene_repository.save(
        candidate_preview_document(revision.document, candidate),
        parent_revision_id=revision.revision_id,
        allow_branch=True,
    ).revision
    dataset = _save_measurement(
        measurement_repository,
        applied_revision,
        measurement_id='meas:primary',
        offset=0.2,
        tilt=0.5,
    )
    measured_response = FrequencyResponse(
        frequency_hz=dataset.frequency_hz,
        level_db=dataset.level_db,
    )
    evaluation = build_objective_evaluation(
        revision,
        spec,
        candidate.candidate_id,
        _vector(candidate.candidate_id, measured_response),
        evaluation_spec=_TARGET_SPEC,
        input_refs=(
            CadObjectiveInputRef(
                evidence_class='measured',
                source_kind='cad_measurement',
                source_id='meas:primary',
                source_sha256=dataset_sha256(dataset),
            ),
        ),
    )

    repository.save_evaluation(evaluation)
    assert repository.get_evaluation(evaluation.evaluation_id) == evaluation

    # A measurement bound to a different candidate's applied revision is not
    # evidence for this evaluation.
    other_revision = scene_repository.save(
        candidate_preview_document(revision.document, candidates[0]),
        parent_revision_id=revision.revision_id,
        allow_branch=True,
    ).revision
    other_dataset = _save_measurement(
        measurement_repository,
        other_revision,
        measurement_id='meas:other-candidate',
        offset=0.2,
        tilt=0.5,
    )
    foreign = _rehashed_evaluation(
        evaluation,
        input_refs=(
            CadObjectiveInputRef(
                evidence_class='measured',
                source_kind='cad_measurement',
                source_id='meas:other-candidate',
                source_sha256=dataset_sha256(other_dataset),
            ),
        ),
        vector=_vector(candidate.candidate_id, measured_response),
    )
    with pytest.raises(ValueError, match='not bound to the evaluated candidate'):
        repository.save_evaluation(foreign)


def test_pareto_set_round_trip_recomputes_from_immutable_evaluations(tmp_path) -> None:
    _scene_repo, revision, spec, candidates, repository, responses = _fixture(tmp_path)
    evaluations = (
        _evaluation(revision, spec, responses, candidates[0].candidate_id, 1.0, 3.0),
        _evaluation(revision, spec, responses, candidates[1].candidate_id, 2.0, 2.0),
        _evaluation(revision, spec, responses, candidates[2].candidate_id, 3.0, 3.0),
    )
    for evaluation in evaluations:
        repository.save_evaluation(evaluation)

    pareto_set = build_pareto_set(
        evaluations,
        ('response.rms_difference_db', 'response.shape_rms_db'),
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
    _scene_repo, revision, spec, candidates, repository, responses = _fixture(tmp_path)
    evaluation = _evaluation(revision, spec, responses, candidates[0].candidate_id, 1.0, 1.0)
    tampered = evaluation.model_copy(update={'search_spec_sha256': '0' * 64})

    with pytest.raises(ValueError, match='SearchSpec hash mismatch'):
        repository.save_evaluation(tampered)


def test_pareto_repository_rejects_result_not_matching_evaluations(tmp_path) -> None:
    _scene_repo, revision, spec, candidates, repository, responses = _fixture(tmp_path)
    evaluations = (
        _evaluation(revision, spec, responses, candidates[0].candidate_id, 1.0, 3.0),
        _evaluation(revision, spec, responses, candidates[1].candidate_id, 2.0, 2.0),
    )
    for evaluation in evaluations:
        repository.save_evaluation(evaluation)

    pareto_set = build_pareto_set(
        evaluations,
        ('response.rms_difference_db', 'response.shape_rms_db'),
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
    _scene_repo, revision, spec, candidates, _repository, responses = _fixture(tmp_path)
    candidate_id = candidates[0].candidate_id
    response = _fr(1.0)
    vector = _vector(candidate_id, response)
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
        evaluation_spec={'objectives': ['response.rms_difference_db', 'response.shape_rms_db']},
        input_refs=refs,
    )
    second = build_objective_evaluation(
        revision,
        spec,
        candidate_id,
        vector,
        evaluation_spec={'objectives': ['response.rms_difference_db', 'response.shape_rms_db']},
        input_refs=tuple(reversed(refs)),
    )

    assert first.input_refs == second.input_refs
    assert first.evaluation_sha256 == second.evaluation_sha256



def test_explicit_objective_definition_identity_survives_repository_reopen(tmp_path) -> None:
    scene_repository, revision, spec, candidates, _repository, responses = _fixture(tmp_path)
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
            'objective_method': 'fixture-coverage-1',
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
            'objective_method': 'fixture-coverage-1',
            'objectives': ['fixture.coverage'],
        },
        input_refs=refs,
    )
    assert first.evaluation_sha256 == second.evaluation_sha256

    responses[f'prediction:{candidate_id}'] = _fr(1.0)
    fixture_evaluators = {
        'fixture-coverage-1': lambda context: vector,
    }
    repository = _objective_repository(
        scene_repository,
        CadSearchRepository(scene_repository),
        responses,
        vector_evaluators=fixture_evaluators,
    )
    repository.save_evaluation(first)
    reopened_repository = _objective_repository(
        scene_repository,
        CadSearchRepository(scene_repository),
        responses,
        vector_evaluators=fixture_evaluators,
    )
    reopened = reopened_repository.get_evaluation(first.evaluation_id)

    assert reopened == first
    assert reopened is not None
    reopened_metric = reopened.vector.metric(definition.objective_id)
    assert reopened_metric.definition is not None
    assert reopened_metric.definition.definition_id == definition.definition_id
    assert reopened_metric.direction == 'maximize'


def _persisted_pareto(tmp_path):
    scene_repository, revision, spec, candidates, repository, responses = _fixture(tmp_path)
    evaluations = (
        _evaluation(revision, spec, responses, candidates[0].candidate_id, 1.0, 3.0),
        _evaluation(revision, spec, responses, candidates[1].candidate_id, 2.0, 2.0),
        _evaluation(revision, spec, responses, candidates[2].candidate_id, 3.0, 3.0),
    )
    for evaluation in evaluations:
        repository.save_evaluation(evaluation)
    pareto_set = build_pareto_set(
        evaluations,
        ('response.rms_difference_db', 'response.shape_rms_db'),
    )
    repository.save_pareto_set(pareto_set)
    return (
        scene_repository,
        revision,
        spec,
        candidates,
        repository,
        responses,
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
    scene_repository, _revision, spec, _candidates, _repository, responses, _evaluations, pareto_set = (
        _persisted_pareto(tmp_path)
    )
    reopened_repository = _objective_repository(
        scene_repository,
        CadSearchRepository(scene_repository),
        responses,
    )

    assert reopened_repository.get_pareto_set(pareto_set.pareto_set_id) == pareto_set
    assert reopened_repository.find_pareto_set_by_sha(
        spec.search_spec_id, pareto_set.pareto_sha256
    ) == pareto_set
    assert reopened_repository.list_pareto_sets(spec.search_spec_id) == (pareto_set,)


def test_pareto_read_apis_replay_and_reject_rehashed_result_tampering(tmp_path) -> None:
    scene_repository, _revision, spec, candidates, repository, _responses, _evaluations, pareto_set = (
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
    scene_repository, revision, spec, candidates, repository, responses, _evaluations, pareto_set = (
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
        revision, other_spec, responses, other_candidates[0].candidate_id, 0.5, 0.5
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
    scene_repository, _revision, spec, _candidates, repository, _responses, evaluations, pareto_set = (
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
    scene_repository, _revision, spec, _candidates, repository, responses, evaluations, pareto_set = (
        _persisted_pareto(tmp_path)
    )
    original = evaluations[0]
    rewritten = _rehashed_evaluation(
        original,
        vector=_vector(original.candidate_id, _fr(9.9, 0.1)),
    )
    assert rewritten.evaluation_sha256 != original.evaluation_sha256
    _replace_persisted_evaluation(
        scene_repository.path, original.evaluation_id, rewritten
    )

    with pytest.raises(ValueError, match='does not reproduce from persisted evidence'):
        repository.get_pareto_set(pareto_set.pareto_set_id)
    with pytest.raises(ValueError, match='does not reproduce from persisted evidence'):
        repository.list_pareto_sets(spec.search_spec_id)
    with pytest.raises(ValueError, match='does not reproduce from persisted evidence'):
        repository.find_pareto_set_by_sha(spec.search_spec_id, pareto_set.pareto_sha256)
    with pytest.raises(ValueError, match='does not reproduce from persisted evidence'):
        repository.save_pareto_set(pareto_set)


def test_pareto_read_apis_fail_closed_when_evaluation_vector_loses_authority(tmp_path) -> None:
    """O40 cannot outlive O30 authority: mutating the evidence column fails closed."""
    scene_repository, _revision, spec, _candidates, repository, _responses, evaluations, pareto_set = (
        _persisted_pareto(tmp_path)
    )
    with closing(sqlite3.connect(scene_repository.path)) as connection, connection:
        connection.execute(
            'UPDATE cad_objective_evaluations SET input_authorities_json=? '
            'WHERE evaluation_id=?',
            ('[]', evaluations[0].evaluation_id),
        )

    with pytest.raises(ValueError, match='input authority mismatch'):
        repository.get_pareto_set(pareto_set.pareto_set_id)
