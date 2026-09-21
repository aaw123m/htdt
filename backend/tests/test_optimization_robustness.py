from __future__ import annotations

from types import SimpleNamespace

import pytest

from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_extended_search_repository import CadExtendedSearchRepository
from htdt.cad_objective_authority import ResolvedObjectiveInput
from htdt.cad_objective_models import CadObjectiveInputRef, canonical_objective_sha256
from htdt.cad_objective_repository import CadObjectiveRepository
from htdt.cad_objectives import build_objective_evaluation
from htdt.cad_repository import SceneRepository
from htdt.cad_robustness_repository import CadRobustnessRepository
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_search import build_cad_search_spec, generate_cad_candidates
from htdt.cad_search_models import CadCandidate, CadSearchAxis
from htdt.cad_search_repository import CadSearchRepository
from htdt.optimization_objectives import (
    ObjectiveDefinition,
    ObjectiveMetric,
    ObjectiveValidDomain,
    ObjectiveVector,
)
from htdt.optimization_robustness import (
    PerturbationObjectiveResult,
    PerturbationSample,
    RobustnessEvaluation,
    RobustnessSpec,
    UncertaintyAxis,
    build_local_stencil,
    build_robustness_spec,
    canonical_robustness_sha256,
    evaluate_local_robustness,
)


DOCUMENT_ID = 'o90a-fixture'


def _spec_metrics_evaluator(context):
    """Evaluator-owned fixture: replay objective metrics from the versioned spec."""

    metrics = context.spec.get('metrics')
    if not isinstance(metrics, list) or not metrics:
        raise ValueError('fixture metrics spec requires a non-empty metrics list')
    resolved = []
    for entry in metrics:
        definition = entry.get('definition')
        resolved.append(
            ObjectiveMetric(
                objective_id=str(entry['objective_id']),
                value=float(entry['value']),
                unit=str(entry['unit']),
                direction=str(entry.get('direction', 'minimize')),
                definition=(
                    ObjectiveDefinition.model_validate(definition)
                    if isinstance(definition, dict)
                    else definition
                ),
            )
        )
    return ObjectiveVector(
        candidate_id=context.evaluation.candidate_id,
        metrics=tuple(resolved),
    )


def _prediction_fixture_resolver(context, ref):
    """Evaluator-owned fixture evidence pinned by exact source identity."""

    return ResolvedObjectiveInput(
        ref=ref,
        source_sha256=canonical_objective_sha256(
            {'source_kind': ref.source_kind, 'source_id': ref.source_id}
        ),
    )


def _objective_repository(scene_repository, search_repository):
    """O30 repository wired with the evaluator-owned fixture authorities."""

    return CadObjectiveRepository(
        scene_repository,
        search_repository,
        input_resolvers={
            'prediction_fixture': _prediction_fixture_resolver,
            'fixture_prediction': _prediction_fixture_resolver,
        },
        vector_evaluators={'fixture-metrics-1': _spec_metrics_evaluator},
    )


def _scene() -> SceneDocument:
    return SceneDocument(
        document_id=DOCUMENT_ID,
        schema_version=3,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.22, y_m=0.28, z_m=0.42),
                speaker_role='FL',
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
            ),
            SceneEntity(
                entity_id='listener-main',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )


def _authorities(tmp_path) -> SimpleNamespace:
    """Wire the exact native authorities O90 persistence now requires."""

    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    search_repository = CadSearchRepository(scene_repository)
    objective_repository = _objective_repository(
        scene_repository,
        search_repository,
    )
    robustness_repository = CadRobustnessRepository(
        scene_repository=scene_repository,
        search_repository=search_repository,
        objective_repository=objective_repository,
        extended_search_repository=CadExtendedSearchRepository(
            search_repository
        ),
    )
    return SimpleNamespace(
        scene_repository=scene_repository,
        search_repository=search_repository,
        objective_repository=objective_repository,
        robustness_repository=robustness_repository,
    )


def _fixture(tmp_path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(_scene(), parent_revision_id=None).revision
    constraints = CadConstraintSet(document_id=DOCUMENT_ID, constraints=())
    search_spec, _ = build_cad_search_spec(
        revision,
        constraints,
        (
            CadSearchAxis(
                entity_id='speaker-fl',
                axis='x',
                min_m=1.0,
                max_m=1.0,
                step_m=0.1,
            ),
        ),
        candidate_limit=10,
        name='O90A exact candidate',
    )
    search_repository = CadSearchRepository(scene_repository)
    search_repository.save(search_spec)
    page = generate_cad_candidates(scene_repository, search_spec)
    candidate = page.candidates[0]
    prediction_ref = f'prediction:{candidate.candidate_id}'
    nominal = build_objective_evaluation(
        revision,
        search_spec,
        candidate.candidate_id,
        ObjectiveVector(
            candidate_id=candidate.candidate_id,
            metrics=(
                ObjectiveMetric(
                    objective_id='response.shape_rms_db',
                    value=2.0,
                    unit='dB',
                ),
            ),
        ),
        evaluation_spec={
            'algorithm_version': 'objective-vector-1',
            'objective_method': 'fixture-metrics-1',
            'objectives': ['response.shape_rms_db'],
            'metrics': [
                {
                    'objective_id': 'response.shape_rms_db',
                    'value': 2.0,
                    'unit': 'dB',
                    'direction': 'minimize',
                },
            ],
        },
        input_refs=(
            CadObjectiveInputRef(
                evidence_class='derived',
                source_kind='candidate_geometry',
                source_id=candidate.candidate_id,
            ),
            CadObjectiveInputRef(
                evidence_class='predicted',
                source_kind='prediction_fixture',
                source_id=prediction_ref,
            ),
        ),
    )
    objective_repository = _objective_repository(
        scene_repository,
        search_repository,
    )
    objective_repository.save_evaluation(nominal)
    axes = (
        UncertaintyAxis(
            axis_id='speaker-x',
            entity_id='speaker-fl',
            parameter='speaker_x_m',
            unit='m',
            nominal_value=1.0,
            minus_delta=0.02,
            plus_delta=0.02,
        ),
        UncertaintyAxis(
            axis_id='listener-x',
            entity_id='listener-main',
            parameter='listener_x_m',
            unit='m',
            nominal_value=3.0,
            minus_delta=0.05,
            plus_delta=0.05,
        ),
        UncertaintyAxis(
            axis_id='aim-yaw',
            entity_id='speaker-fl',
            parameter='aim_yaw_deg',
            unit='deg',
            nominal_value=0.0,
            minus_delta=2.0,
            plus_delta=2.0,
        ),
        UncertaintyAxis(
            axis_id='aim-pitch',
            entity_id='speaker-fl',
            parameter='aim_pitch_deg',
            unit='deg',
            nominal_value=0.0,
            minus_delta=1.0,
            plus_delta=1.0,
        ),
        UncertaintyAxis(
            axis_id='body-yaw',
            entity_id='speaker-fl',
            parameter='body_yaw_deg',
            unit='deg',
            nominal_value=0.0,
            minus_delta=2.0,
            plus_delta=2.0,
        ),
    )
    spec = build_robustness_spec(
        source_revision=revision,
        search_spec=search_spec,
        candidate=candidate,
        candidate_set_sha256=page.candidate_set_sha256,
        nominal_objective=nominal,
        nominal_prediction_result_ref=prediction_ref,
        model_id='synthetic-model',
        model_version='1',
        prediction_provider_id='test-provider',
        fidelity='development_synthetic',
        axes=axes,
        software_version='test',
        created_at_utc='2026-09-19T00:00:00+00:00',
    )
    return revision, constraints, search_spec, nominal, spec


def test_o90a_spec_and_local_stencil_are_immutable_and_deterministic(tmp_path) -> None:
    _revision, _constraints, _search_spec, _nominal, spec = _fixture(tmp_path)

    plans = build_local_stencil(spec)

    assert len(plans) == 1 + 2 * len(spec.axes)
    assert plans[0].step == 'nominal'
    assert [item.axis_id for item in plans[1:3]] == ['aim-pitch', 'aim-pitch']
    assert [item.step for item in plans[1:3]] == ['minus', 'plus']
    assert spec.sampling_strategy == 'deterministic_local_stencil'
    assert all(axis.uncertainty_model == 'bounded_interval' for axis in spec.axes)
    with pytest.raises(Exception):
        spec.axes = ()


def test_o90a_rechecks_constraints_keeps_infeasible_sample_and_builds_sensitivity(
    tmp_path,
) -> None:
    revision, constraints, search_spec, nominal, spec = _fixture(tmp_path)
    # Make one declared bounded perturbation infeasible without inventing a
    # probability model. Other axes remain scoreable.
    axes = tuple(
        axis.model_copy(update={'allowed_min': 0.99})
        if axis.axis_id == 'speaker-x'
        else axis
        for axis in spec.axes
    )
    spec = build_robustness_spec(
        source_revision=revision,
        search_spec=search_spec,
        candidate=CadCandidate.model_validate_json(spec.candidate_payload_json),
        candidate_set_sha256=spec.candidate_set_sha256,
        nominal_objective=nominal,
        nominal_prediction_result_ref=spec.nominal_prediction_result_ref,
        model_id=spec.model_id,
        model_version=spec.model_version,
        prediction_provider_id=spec.prediction_provider_id,
        fidelity=spec.fidelity,
        axes=axes,
        software_version='test',
        created_at_utc='2026-09-19T00:00:00+00:00',
    )

    def evaluator(document: SceneDocument, sample_id: str) -> PerturbationObjectiveResult:
        speaker = document.entity('speaker-fl')
        listener = document.entity('listener-main')
        value = (
            2.0
            + abs(speaker.position.x_m - 1.0) * 10.0
            + abs(listener.position.x_m - 3.0) * 2.0
        )
        return PerturbationObjectiveResult(
            prediction_result_ref=f'prediction:{sample_id}',
            objective_vector=ObjectiveVector(
                candidate_id=sample_id,
                metrics=(
                    ObjectiveMetric(
                        objective_id='response.shape_rms_db',
                        value=value,
                        unit='dB',
                    ),
                ),
            ),
        )

    samples, evaluations = evaluate_local_robustness(
        source_revision=revision,
        search_spec=search_spec,
        spec=spec,
        constraint_set=constraints,
        nominal_objective=nominal,
        evaluator=evaluator,
        created_at_utc='2026-09-19T00:01:00+00:00',
    )

    infeasible = next(
        item
        for item in samples
        if item.axis_id == 'speaker-x' and item.step == 'minus'
    )
    assert not infeasible.feasible
    assert infeasible.objective_vector is None
    assert infeasible.domain_rejection_ids == (
        '__uncertainty_bound__:speaker-x:min',
    )
    assert len(evaluations) == 1
    evaluation = evaluations[0]
    assert evaluation.sampled_worst_semantics == 'sampled_worst'
    assert infeasible.sample_id in evaluation.infeasible_sample_ids
    speaker_sensitivity = next(
        item
        for item in evaluation.local_sensitivities
        if item.axis_id == 'speaker-x'
    )
    assert speaker_sensitivity.state == 'infeasible_or_failed'
    listener_sensitivity = next(
        item
        for item in evaluation.local_sensitivities
        if item.axis_id == 'listener-x'
    )
    assert listener_sensitivity.state == 'available'
    assert listener_sensitivity.central_slope_per_unit == pytest.approx(0.0)


def test_o90a_persistence_round_trips_exact_provenance(tmp_path) -> None:
    revision, constraints, search_spec, nominal, spec = _fixture(tmp_path)

    def evaluator(_document: SceneDocument, sample_id: str) -> PerturbationObjectiveResult:
        return PerturbationObjectiveResult(
            prediction_result_ref=f'prediction:{sample_id}',
            objective_vector=ObjectiveVector(
                candidate_id=sample_id,
                metrics=(
                    ObjectiveMetric(
                        objective_id='response.shape_rms_db',
                        value=2.1,
                        unit='dB',
                    ),
                ),
            ),
        )

    samples, evaluations = evaluate_local_robustness(
        source_revision=revision,
        search_spec=search_spec,
        spec=spec,
        constraint_set=constraints,
        nominal_objective=nominal,
        evaluator=evaluator,
        created_at_utc='2026-09-19T00:01:00+00:00',
    )
    repository = _authorities(tmp_path).robustness_repository
    repository.save_spec(spec)
    repository.save_samples(samples)
    repository.save_evaluations(evaluations)

    assert repository.get_spec(spec.robustness_spec_id) == spec
    assert repository.list_samples(spec.robustness_spec_id) == samples
    assert repository.list_evaluations(spec.robustness_spec_id) == evaluations
    assert repository.list_specs_for_candidate(
        document_id=spec.document_id,
        scene_revision_id=spec.scene_revision_id,
        candidate_id=spec.candidate_id,
    ) == (spec,)


def _reforged_spec(spec: RobustnessSpec, **updates) -> RobustnessSpec:
    """Return a self-hash-consistent spec carrying tampered fields."""

    forged = spec.model_copy(update=updates)
    digest = canonical_robustness_sha256(forged.identity_payload())
    return RobustnessSpec.model_validate(
        forged.model_copy(
            update={
                'robustness_spec_sha256': digest,
                'robustness_spec_id': f'rob-{digest[:24]}',
            }
        ).model_dump(mode='python')
    )


def _reforged_sample(
    sample: PerturbationSample,
    **updates,
) -> PerturbationSample:
    """Return a self-hash-consistent sample carrying tampered fields."""

    forged = sample.model_copy(update=updates)
    digest = canonical_robustness_sha256(forged.identity_payload())
    return PerturbationSample.model_validate(
        forged.model_copy(update={'sample_sha256': digest}).model_dump(
            mode='python'
        )
    )


def _reforged_evaluation(
    evaluation: RobustnessEvaluation,
    **updates,
) -> RobustnessEvaluation:
    """Return a self-hash-consistent evaluation carrying tampered fields."""

    forged = evaluation.model_copy(update=updates)
    digest = canonical_robustness_sha256(forged.identity_payload())
    return RobustnessEvaluation.model_validate(
        forged.model_copy(
            update={
                'evaluation_sha256': digest,
                'evaluation_id': f're-{digest[:24]}',
            }
        ).model_dump(mode='python')
    )


def test_o90_repository_rejects_forged_spec_authority_bindings(tmp_path) -> None:
    _revision, _constraints, _search_spec, _nominal, spec = _fixture(tmp_path)
    repository = _authorities(tmp_path).robustness_repository

    missing_revision = _reforged_spec(spec, scene_revision_id='rev:missing')
    with pytest.raises(
        ValueError, match='SceneRevision authority does not exist'
    ):
        repository.save_spec(missing_revision)

    stale_hash = _reforged_spec(spec, scene_content_hash='0' * 64)
    with pytest.raises(ValueError, match='SceneRevision authority mismatch'):
        repository.save_spec(stale_hash)

    missing_search = _reforged_spec(spec, search_spec_id='search:missing')
    with pytest.raises(ValueError, match='SearchSpec authority does not exist'):
        repository.save_spec(missing_search)

    stale_search = _reforged_spec(spec, search_spec_sha256='0' * 64)
    with pytest.raises(ValueError, match='SearchSpec authority mismatch'):
        repository.save_spec(stale_search)

    missing_objective = _reforged_spec(
        spec,
        nominal_objective_evaluation_id='evaluation:missing',
    )
    with pytest.raises(
        ValueError, match='nominal O30 evaluation does not exist'
    ):
        repository.save_spec(missing_objective)

    stale_objective = _reforged_spec(
        spec,
        nominal_objective_evaluation_sha256='0' * 64,
    )
    with pytest.raises(
        ValueError, match='nominal O30 evaluation authority mismatch'
    ):
        repository.save_spec(stale_objective)

    foreign_prediction = _reforged_spec(
        spec,
        nominal_prediction_result_ref='prediction:external-forgery',
    )
    with pytest.raises(ValueError, match='prediction ref'):
        repository.save_spec(foreign_prediction)

    # The honest spec still persists and reads back.
    assert repository.save_spec(spec) == spec
    assert repository.get_spec(spec.robustness_spec_id) == spec


def test_o90_repository_rejects_spec_bound_to_foreign_search(tmp_path) -> None:
    revision, constraints, _search_spec, _nominal, spec = _fixture(tmp_path)
    authorities = _authorities(tmp_path)

    changed_revision = authorities.scene_repository.save(
        _scene().model_copy(
            update={
                'room': RoomPrism(width_m=7.0, depth_m=4.0, height_m=2.4),
            }
        ),
        parent_revision_id=revision.revision_id,
    ).revision
    foreign_spec, _ = build_cad_search_spec(
        changed_revision,
        constraints,
        (
            CadSearchAxis(
                entity_id='speaker-fl',
                axis='x',
                min_m=1.0,
                max_m=1.0,
                step_m=0.1,
            ),
        ),
        candidate_limit=10,
        name='foreign search',
    )
    authorities.search_repository.save(foreign_spec)

    forged = _reforged_spec(
        spec,
        search_spec_id=foreign_spec.search_spec_id,
        search_spec_sha256=foreign_spec.search_spec_sha256,
    )
    with pytest.raises(ValueError, match='SearchSpec authority mismatch'):
        authorities.robustness_repository.save_spec(forged)


def test_o90_repository_rejects_spec_candidate_set_forgery(tmp_path) -> None:
    revision, _constraints, search_spec, nominal, spec = _fixture(tmp_path)
    repository = _authorities(tmp_path).robustness_repository

    wrong_set = _reforged_spec(spec, candidate_set_sha256='0' * 64)
    with pytest.raises(ValueError, match='candidate-set authority mismatch'):
        repository.save_spec(wrong_set)

    candidate = CadCandidate.model_validate_json(spec.candidate_payload_json)
    shifted = candidate.model_copy(update={'feasible_index': 1})
    forged = build_robustness_spec(
        source_revision=revision,
        search_spec=search_spec,
        candidate=shifted,
        candidate_set_sha256=spec.candidate_set_sha256,
        nominal_objective=nominal,
        nominal_prediction_result_ref=spec.nominal_prediction_result_ref,
        model_id=spec.model_id,
        model_version=spec.model_version,
        prediction_provider_id=spec.prediction_provider_id,
        fidelity=spec.fidelity,
        axes=spec.axes,
        software_version=spec.software_version,
        created_at_utc='2026-09-19T00:40:00+00:00',
    )
    with pytest.raises(
        ValueError, match='not the persisted candidate-set member'
    ):
        repository.save_spec(forged)


def test_o90_repository_derived_spec_requires_persisted_parent(tmp_path) -> None:
    from htdt.optimization_robustness_multidimensional import (
        derive_multidimensional_robustness_spec,
    )

    _revision, _constraints, _search_spec, _nominal, base_spec = _fixture(
        tmp_path
    )
    derived = derive_multidimensional_robustness_spec(
        base_spec,
        sample_count=5,
        seed=21,
        created_at_utc='2026-09-19T00:41:00+00:00',
    )
    repository = _authorities(tmp_path).robustness_repository

    with pytest.raises(ValueError, match='persisted local parent'):
        repository.save_spec(derived)

    repository.save_spec(base_spec)
    forged_parent = _reforged_spec(
        derived,
        parent_robustness_spec_sha256='0' * 64,
    )
    with pytest.raises(ValueError, match='parent hash mismatch'):
        repository.save_spec(forged_parent)

    assert repository.save_spec(derived) == derived
    assert repository.get_spec(derived.robustness_spec_id) == derived
    assert repository.list_specs_for_candidate(
        document_id=derived.document_id,
        scene_revision_id=derived.scene_revision_id,
        candidate_id=derived.candidate_id,
    ) == (base_spec, derived)


def test_o90_repository_rejects_forged_and_off_schedule_samples(
    tmp_path,
) -> None:
    revision, constraints, search_spec, nominal, spec = _fixture(tmp_path)
    samples, _evaluations = evaluate_local_robustness(
        source_revision=revision,
        search_spec=search_spec,
        spec=spec,
        constraint_set=constraints,
        nominal_objective=nominal,
        evaluator=_o90b_linear_evaluator,
        created_at_utc='2026-09-19T00:42:00+00:00',
    )
    repository = _authorities(tmp_path).robustness_repository
    repository.save_spec(spec)

    target = samples[1]
    assert target.step == 'minus'
    assert target.axis_id is not None
    off_schedule = _reforged_sample(
        target,
        step='plus',
        parameter_deltas={
            target.axis_id: -float(target.parameter_deltas[target.axis_id]),
        },
    )
    with pytest.raises(ValueError, match='deterministic sampling plan'):
        repository.save_sample(off_schedule)

    fabricated_index = _reforged_sample(target, sample_index=99)
    with pytest.raises(ValueError, match='deterministic sampling plan'):
        repository.save_sample(fabricated_index)

    tampered_hash = _reforged_sample(
        target,
        perturbed_scene_content_hash='0' * 64,
    )
    with pytest.raises(ValueError, match='deterministic scene evidence'):
        repository.save_sample(tampered_hash)

    flipped = _reforged_sample(
        target,
        feasible=False,
        objective_vector=None,
        prediction_result_ref=None,
        failure_reason='hard_constraint_violation',
    )
    with pytest.raises(ValueError, match='deterministic scene evidence'):
        repository.save_sample(flipped)

    tampered_nominal = _reforged_sample(
        samples[0],
        prediction_result_ref='prediction:forged',
    )
    with pytest.raises(ValueError, match='nominal evidence ref'):
        repository.save_sample(tampered_nominal)

    repository.save_samples(samples)
    assert repository.list_samples(spec.robustness_spec_id) == samples


def test_o90_repository_rejects_evaluation_not_derived_from_evidence(
    tmp_path,
) -> None:
    revision, constraints, search_spec, nominal, spec = _fixture(tmp_path)
    samples, evaluations = evaluate_local_robustness(
        source_revision=revision,
        search_spec=search_spec,
        spec=spec,
        constraint_set=constraints,
        nominal_objective=nominal,
        evaluator=_o90b_linear_evaluator,
        created_at_utc='2026-09-19T00:43:00+00:00',
    )
    repository = _authorities(tmp_path).robustness_repository
    repository.save_spec(spec)

    repository.save_samples(samples[:2])
    with pytest.raises(ValueError, match='deterministic local stencil'):
        repository.save_evaluation(evaluations[0])

    repository.save_samples(samples[2:])
    forged = _reforged_evaluation(
        evaluations[0],
        sampled_worst_value=evaluations[0].sampled_worst_value + 0.5,
    )
    with pytest.raises(ValueError, match='does not reproduce'):
        repository.save_evaluation(forged)

    repository.save_evaluations(evaluations)
    assert repository.list_evaluations(spec.robustness_spec_id) == evaluations


def test_o90_repository_reads_fail_closed_after_direct_sqlite_tamper(
    tmp_path,
) -> None:
    import sqlite3

    revision, constraints, search_spec, nominal, spec = _fixture(tmp_path)
    samples, evaluations = evaluate_local_robustness(
        source_revision=revision,
        search_spec=search_spec,
        spec=spec,
        constraint_set=constraints,
        nominal_objective=nominal,
        evaluator=_o90b_linear_evaluator,
        created_at_utc='2026-09-19T00:44:00+00:00',
    )
    repository = _authorities(tmp_path).robustness_repository
    repository.save_spec(spec)
    repository.save_samples(samples)
    repository.save_evaluations(evaluations)
    db_path = tmp_path / 'cad.sqlite3'

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE cad_robustness_specs SET model_id='forged-model' "
            'WHERE robustness_spec_id=?',
            (spec.robustness_spec_id,),
        )
    with pytest.raises(ValueError, match='row disagrees with its payload'):
        repository.get_spec(spec.robustness_spec_id)
    with pytest.raises(ValueError, match='row disagrees with its payload'):
        repository.list_specs_for_candidate(
            document_id=spec.document_id,
            scene_revision_id=spec.scene_revision_id,
            candidate_id=spec.candidate_id,
        )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            'UPDATE cad_robustness_specs SET model_id=? '
            'WHERE robustness_spec_id=?',
            (spec.model_id, spec.robustness_spec_id),
        )
    assert repository.get_spec(spec.robustness_spec_id) == spec

    forged_sample = _reforged_sample(
        samples[1],
        feasible=False,
        objective_vector=None,
        prediction_result_ref=None,
        failure_reason='hard_constraint_violation',
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            'UPDATE cad_perturbation_samples SET payload_json=?, '
            'sample_sha256=?, feasible=? WHERE sample_id=?',
            (
                forged_sample.model_dump_json(),
                forged_sample.sample_sha256,
                int(forged_sample.feasible),
                forged_sample.sample_id,
            ),
        )
    with pytest.raises(ValueError, match='deterministic scene evidence'):
        repository.list_samples(spec.robustness_spec_id)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            'UPDATE cad_perturbation_samples SET payload_json=?, '
            'sample_sha256=?, feasible=? WHERE sample_id=?',
            (
                samples[1].model_dump_json(),
                samples[1].sample_sha256,
                int(samples[1].feasible),
                samples[1].sample_id,
            ),
        )
    assert repository.list_samples(spec.robustness_spec_id) == samples

    forged_evaluation = _reforged_evaluation(
        evaluations[0],
        nominal_value=evaluations[0].nominal_value + 5.0,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            'UPDATE cad_robustness_evaluations SET payload_json=?, '
            'evaluation_sha256=?, evaluation_id=? WHERE evaluation_id=?',
            (
                forged_evaluation.model_dump_json(),
                forged_evaluation.evaluation_sha256,
                forged_evaluation.evaluation_id,
                evaluations[0].evaluation_id,
            ),
        )
    with pytest.raises(ValueError, match='does not reproduce'):
        repository.list_evaluations(spec.robustness_spec_id)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            'UPDATE cad_robustness_evaluations SET payload_json=?, '
            'evaluation_sha256=?, evaluation_id=? WHERE evaluation_id=?',
            (
                evaluations[0].model_dump_json(),
                evaluations[0].evaluation_sha256,
                evaluations[0].evaluation_id,
                forged_evaluation.evaluation_id,
            ),
        )
        connection.execute(
            'DELETE FROM cad_perturbation_samples WHERE sample_id=?',
            (samples[1].sample_id,),
        )
    with pytest.raises(ValueError, match='do not match'):
        repository.list_evaluations(spec.robustness_spec_id)


def test_o90_repository_round_trips_uncertainty_evidence(tmp_path) -> None:
    from htdt.optimization_robustness_uncertainty import (
        derive_uncertainty_robustness_spec,
        evaluate_uncertainty_robustness,
    )

    revision, constraints, search_spec, nominal, base_spec = _fixture(tmp_path)
    spec = derive_uncertainty_robustness_spec(
        base_spec,
        uncertainty_model=_o90b_distribution_model(base_spec),
        sample_count=5,
        seed=31,
        created_at_utc='2026-09-19T00:45:00+00:00',
    )
    result = evaluate_uncertainty_robustness(
        source_revision=revision,
        search_spec=search_spec,
        spec=spec,
        constraint_set=constraints,
        nominal_objective=nominal,
        evaluator=_o90b_linear_evaluator,
        created_at_utc='2026-09-19T00:46:00+00:00',
    )
    assert result.status == 'completed'

    repository = _authorities(tmp_path).robustness_repository
    repository.save_spec(base_spec)
    repository.save_spec(spec)
    repository.save_samples(result.samples)
    repository.save_evaluations(result.evaluations)

    assert repository.get_spec(spec.robustness_spec_id) == spec
    assert repository.list_samples(spec.robustness_spec_id) == result.samples
    assert (
        repository.list_evaluations(spec.robustness_spec_id)
        == result.evaluations
    )


def test_o90b_multidimensional_sampling_is_reproducible_and_linked(tmp_path) -> None:
    from htdt.optimization_robustness import LinkedPerturbationGroup
    from htdt.optimization_robustness_multidimensional import (
        build_multidimensional_sampling_plan,
        derive_multidimensional_robustness_spec,
    )

    _revision, _constraints, _search_spec, _nominal, base_spec = _fixture(tmp_path)
    linked = LinkedPerturbationGroup(
        group_id='front-listener-x',
        axis_multipliers={
            'speaker-x': 1.0,
            'listener-x': -0.5,
        },
    )

    spec_a = derive_multidimensional_robustness_spec(
        base_spec,
        sample_count=8,
        seed=1701,
        linked_groups=(linked,),
        created_at_utc='2026-09-19T00:02:00+00:00',
    )
    spec_b = derive_multidimensional_robustness_spec(
        base_spec,
        sample_count=8,
        seed=1701,
        linked_groups=(linked,),
        created_at_utc='2026-09-19T00:02:00+00:00',
    )
    assert spec_a == spec_b
    assert spec_a.parent_robustness_spec_id == base_spec.robustness_spec_id
    assert (
        spec_a.parent_robustness_spec_sha256
        == base_spec.robustness_spec_sha256
    )

    plans_a = build_multidimensional_sampling_plan(spec_a)
    plans_b = build_multidimensional_sampling_plan(spec_b)
    assert plans_a == plans_b
    assert len(plans_a) == 8
    assert plans_a[0].step == 'nominal'
    assert all(
        item.step == 'multidimensional'
        for item in plans_a[1:]
    )
    assert all(
        set(item.parameter_deltas) == {axis.axis_id for axis in spec_a.axes}
        for item in plans_a[1:]
    )

    linked_plan = plans_a[3]
    speaker_norm = linked_plan.parameter_deltas['speaker-x'] / 0.02
    listener_norm = linked_plan.parameter_deltas['listener-x'] / 0.05
    assert listener_norm == pytest.approx(-0.5 * speaker_norm)


def test_o90b_keeps_infeasible_samples_builds_envelope_and_round_trips(
    tmp_path,
    monkeypatch,
) -> None:
    from htdt.cad_search_models import CadCandidate
    from htdt.optimization_objectives import ObjectiveMetric, ObjectiveVector
    from htdt.optimization_robustness import (
        PerturbationObjectiveResult,
        build_robustness_spec,
    )
    import htdt.optimization_robustness_multidimensional as o90b

    revision, constraints, search_spec, nominal, original = _fixture(tmp_path)
    axes = tuple(
        axis.model_copy(update={'allowed_min': 0.99})
        if axis.axis_id == 'speaker-x'
        else axis
        for axis in original.axes
        if axis.axis_id in {'speaker-x', 'listener-x'}
    )
    base_spec = build_robustness_spec(
        source_revision=revision,
        search_spec=search_spec,
        candidate=CadCandidate.model_validate_json(original.candidate_payload_json),
        candidate_set_sha256=original.candidate_set_sha256,
        nominal_objective=nominal,
        nominal_prediction_result_ref=original.nominal_prediction_result_ref,
        model_id=original.model_id,
        model_version=original.model_version,
        prediction_provider_id=original.prediction_provider_id,
        fidelity=original.fidelity,
        axes=axes,
        software_version='test',
        created_at_utc='2026-09-19T00:00:00+00:00',
    )
    spec = o90b.derive_multidimensional_robustness_spec(
        base_spec,
        sample_count=7,
        seed=9,
        created_at_utc='2026-09-19T00:02:00+00:00',
    )

    calls = {'g10': 0, 'o80': 0}
    real_g10 = o90b.evaluate_cad_constraints
    real_o80 = o90b.orientation_constraint_rejections

    def counted_g10(*args, **kwargs):
        calls['g10'] += 1
        return real_g10(*args, **kwargs)

    def counted_o80(*args, **kwargs):
        calls['o80'] += 1
        return real_o80(*args, **kwargs)

    monkeypatch.setattr(o90b, 'evaluate_cad_constraints', counted_g10)
    monkeypatch.setattr(o90b, 'orientation_constraint_rejections', counted_o80)

    def evaluator(document, sample_id: str) -> PerturbationObjectiveResult:
        speaker = document.entity('speaker-fl')
        listener = document.entity('listener-main')
        value = (
            2.0
            + abs(speaker.position.x_m - 1.0) * 10.0
            + abs(listener.position.x_m - 3.0) * 2.0
        )
        return PerturbationObjectiveResult(
            prediction_result_ref=f'prediction:{sample_id}',
            objective_vector=ObjectiveVector(
                candidate_id=sample_id,
                metrics=(
                    ObjectiveMetric(
                        objective_id='response.shape_rms_db',
                        value=value,
                        unit='dB',
                    ),
                ),
            ),
        )

    samples, evaluations = o90b.evaluate_multidimensional_robustness(
        source_revision=revision,
        search_spec=search_spec,
        spec=spec,
        constraint_set=constraints,
        nominal_objective=nominal,
        evaluator=evaluator,
        created_at_utc='2026-09-19T00:03:00+00:00',
    )

    assert calls == {'g10': len(samples), 'o80': len(samples)}
    assert len(samples) == spec.sample_count
    assert samples[1].step == 'multidimensional'
    assert not samples[1].feasible
    assert '__uncertainty_bound__:speaker-x:min' in samples[1].domain_rejection_ids
    assert samples[1].objective_vector is None

    evaluation = evaluations[0]
    assert evaluation.sampled_worst_semantics == 'sampled_worst'
    assert samples[1].sample_id in evaluation.infeasible_sample_ids
    assert evaluation.feasible_fraction == pytest.approx(
        sum(1 for item in samples if item.feasible) / len(samples)
    )
    assert evaluation.sampled_envelope is not None
    assert evaluation.sampled_envelope.percentile_values is None
    assert evaluation.percentile_semantics == 'not_available_bounded_interval'
    assert evaluation.mean_value is None
    assert evaluation.constraint_violation_probability is None
    assert evaluation.probability_semantics is None
    assert evaluation.sampling_provenance_sha256 is not None

    repository = _authorities(tmp_path).robustness_repository
    repository.save_spec(base_spec)
    repository.save_spec(spec)
    repository.save_samples(samples)
    repository.save_evaluations(evaluations)
    assert repository.get_spec(spec.robustness_spec_id) == spec
    assert repository.list_samples(spec.robustness_spec_id) == samples
    assert repository.list_evaluations(spec.robustness_spec_id) == evaluations


def test_o90b_uses_o40_for_separate_nominal_and_sampled_worst_pareto() -> None:
    from htdt.optimization_objectives import ObjectiveMetric, ObjectiveVector
    from htdt.optimization_robustness import (
        RobustnessEvaluation,
        SampledObjectiveEnvelope,
        canonical_robustness_sha256,
    )
    from htdt.optimization_robustness_multidimensional import (
        RobustParetoSelection,
        robust_pareto_front,
    )

    objective_id = 'response.shape_rms_db'

    def evaluation(candidate_id: str, nominal: float, sampled_worst: float):
        envelope = SampledObjectiveEnvelope(
            sampled_min_sample_id=f'{candidate_id}-s0',
            sampled_min_value=min(nominal, sampled_worst),
            sampled_max_sample_id=f'{candidate_id}-s0',
            sampled_max_value=max(nominal, sampled_worst),
            percentile_values=None,
        )
        identity = {
            'schema_version': 1,
            'robustness_spec_id': f'rob-{candidate_id}',
            'robustness_spec_sha256': '1' * 64,
            'candidate_id': candidate_id,
            'objective_id': objective_id,
            'objective_unit': 'dB',
            'direction': 'minimize',
            'nominal_sample_id': f'{candidate_id}-s0',
            'nominal_value': nominal,
            'local_sensitivities': [],
            'sampled_worst_semantics': 'sampled_worst',
            'sampled_worst_sample_id': f'{candidate_id}-s0',
            'sampled_worst_value': sampled_worst,
            'sample_ids': [f'{candidate_id}-s0'],
            'infeasible_sample_ids': [],
            'failed_sample_ids': [],
            'sampled_envelope': envelope.model_dump(mode='json'),
            'feasible_fraction': 1.0,
            'sampling_provenance_sha256': '2' * 64,
            'percentile_semantics': 'not_available_bounded_interval',
        }
        digest = canonical_robustness_sha256(identity)
        return RobustnessEvaluation(
            **identity,
            evaluation_id=f're-{digest[:24]}',
            evaluation_sha256=digest,
            created_at_utc='2026-09-19T00:04:00+00:00',
        )

    nominal_a = ObjectiveVector(
        candidate_id='a',
        metrics=(
            ObjectiveMetric(objective_id=objective_id, value=1.0, unit='dB'),
        ),
    )
    nominal_b = ObjectiveVector(
        candidate_id='b',
        metrics=(
            ObjectiveMetric(objective_id=objective_id, value=2.0, unit='dB'),
        ),
    )
    selection = RobustParetoSelection(
        nominal_objective_ids=(objective_id,),
        robustness_objective_ids=(objective_id,),
    )
    result = robust_pareto_front(
        (
            (nominal_a, (evaluation('a', 1.0, 4.0),)),
            (nominal_b, (evaluation('b', 2.0, 2.0),)),
        ),
        selection,
    )

    assert result.objective_ids == (
        f'nominal::{objective_id}',
        f'robust.sampled_worst::{objective_id}',
    )
    assert result.non_dominated_candidate_ids == ('a', 'b')
    assert result.dominated_by == {'a': (), 'b': ()}


def test_o90b_requires_nominal_and_both_corner_anchors(tmp_path) -> None:
    from htdt.optimization_robustness_multidimensional import (
        derive_multidimensional_robustness_spec,
    )

    _revision, _constraints, _search_spec, _nominal, base_spec = _fixture(tmp_path)

    with pytest.raises(ValueError, match='at least three samples'):
        derive_multidimensional_robustness_spec(
            base_spec,
            sample_count=2,
            seed=1,
            created_at_utc='2026-09-19T00:02:00+00:00',
        )



def _o90b_distribution_model(base_spec):
    from htdt.optimization_robustness import (
        DistributionAxisUncertainty,
        DistributionUncertaintyModel,
    )

    return DistributionUncertaintyModel(
        model_id='known-uniform-inputs',
        axes=tuple(
            DistributionAxisUncertainty(
                axis_id=axis.axis_id,
                distribution='uniform',
                min_delta=-float(axis.minus_delta),
                max_delta=float(axis.plus_delta),
            )
            for axis in base_spec.axes
        ),
    )


def _o90b_linear_evaluator(document, sample_id: str) -> PerturbationObjectiveResult:
    speaker = document.entity('speaker-fl')
    value = 2.0 + (speaker.position.x_m - 1.0) * 10.0
    return PerturbationObjectiveResult(
        prediction_result_ref=f'prediction:{sample_id}',
        objective_vector=ObjectiveVector(
            candidate_id=sample_id,
            metrics=(
                ObjectiveMetric(
                    objective_id='response.shape_rms_db',
                    value=value,
                    unit='dB',
                ),
            ),
        ),
    )


def test_o90b_explicit_distribution_recovers_known_mean_and_percentile(
    tmp_path,
) -> None:
    from htdt.optimization_robustness_uncertainty import (
        build_uncertainty_sampling_plan,
        derive_uncertainty_robustness_spec,
        evaluate_uncertainty_robustness,
    )

    revision, constraints, search_spec, nominal, base_spec = _fixture(tmp_path)
    model = _o90b_distribution_model(base_spec)
    spec = derive_uncertainty_robustness_spec(
        base_spec,
        uncertainty_model=model,
        sample_count=65,
        seed=1701,
        created_at_utc='2026-09-19T00:10:00+00:00',
    )
    replay_spec = derive_uncertainty_robustness_spec(
        base_spec,
        uncertainty_model=model,
        sample_count=65,
        seed=1701,
        created_at_utc='2026-09-19T00:10:01+00:00',
    )
    changed_seed_spec = derive_uncertainty_robustness_spec(
        base_spec,
        uncertainty_model=model,
        sample_count=65,
        seed=1702,
        created_at_utc='2026-09-19T00:10:01+00:00',
    )
    assert replay_spec.robustness_spec_id == spec.robustness_spec_id
    assert replay_spec.robustness_spec_sha256 == spec.robustness_spec_sha256
    assert build_uncertainty_sampling_plan(replay_spec) == (
        build_uncertainty_sampling_plan(spec)
    )
    assert changed_seed_spec.robustness_spec_sha256 != spec.robustness_spec_sha256
    assert tuple(
        item.sample_id for item in build_uncertainty_sampling_plan(changed_seed_spec)
    ) != tuple(item.sample_id for item in build_uncertainty_sampling_plan(spec))

    result = evaluate_uncertainty_robustness(
        source_revision=revision,
        search_spec=search_spec,
        spec=spec,
        constraint_set=constraints,
        nominal_objective=nominal,
        evaluator=_o90b_linear_evaluator,
        created_at_utc='2026-09-19T00:11:00+00:00',
    )

    assert result.status == 'completed'
    evaluation = result.evaluations[0]
    assert evaluation.probability_semantics == 'explicit_distribution'
    assert evaluation.percentile_semantics == 'explicit_probability_model'
    assert evaluation.mean_value == pytest.approx(2.0, abs=1e-12)
    assert evaluation.sampled_envelope is not None
    assert evaluation.sampled_envelope.percentile_values is not None
    assert evaluation.sampled_envelope.percentile_values['p95'] == pytest.approx(
        2.18,
        abs=0.005,
    )
    assert evaluation.constraint_violation_probability == pytest.approx(0.0)
    assert evaluation.feasible_fraction == pytest.approx(1.0)


def test_o90b_empirical_samples_remain_non_probabilistic_without_weights(
    tmp_path,
) -> None:
    from htdt.optimization_robustness import (
        EmpiricalUncertaintyModel,
        ExplicitPerturbationState,
    )
    from htdt.optimization_robustness_uncertainty import (
        build_uncertainty_sampling_plan,
        derive_uncertainty_robustness_spec,
        evaluate_uncertainty_robustness,
    )

    revision, constraints, search_spec, nominal, base_spec = _fixture(tmp_path)
    model = EmpiricalUncertaintyModel(
        model_id='installation-observations',
        samples=(
            ExplicitPerturbationState(
                state_id='z-observation',
                parameter_deltas={'speaker-x': 0.01},
            ),
            ExplicitPerturbationState(
                state_id='a-observation',
                parameter_deltas={'speaker-x': -0.01},
            ),
        ),
    )
    spec = derive_uncertainty_robustness_spec(
        base_spec,
        uncertainty_model=model,
        created_at_utc='2026-09-19T00:12:00+00:00',
    )
    plans = build_uncertainty_sampling_plan(spec)
    assert [item.uncertainty_item_id for item in plans[1:]] == [
        'a-observation',
        'z-observation',
    ]
    assert all(item.probability_weight is None for item in plans[1:])

    result = evaluate_uncertainty_robustness(
        source_revision=revision,
        search_spec=search_spec,
        spec=spec,
        constraint_set=constraints,
        nominal_objective=nominal,
        evaluator=_o90b_linear_evaluator,
        created_at_utc='2026-09-19T00:13:00+00:00',
    )
    evaluation = result.evaluations[0]
    assert evaluation.percentile_semantics == 'not_available_empirical_unweighted'
    assert evaluation.sampled_envelope is not None
    assert evaluation.sampled_envelope.percentile_values is None
    assert evaluation.mean_value is None
    assert evaluation.constraint_violation_probability is None
    assert evaluation.probability_semantics is None


def test_o90b_explicit_discrete_weights_produce_violation_probability(
    tmp_path,
) -> None:
    from htdt.cad_search_models import CadCandidate
    from htdt.optimization_robustness import (
        DiscreteUncertaintyModel,
        ExplicitPerturbationState,
        build_robustness_spec,
    )
    from htdt.optimization_robustness_uncertainty import (
        derive_uncertainty_robustness_spec,
        evaluate_uncertainty_robustness,
    )

    revision, constraints, search_spec, nominal, original = _fixture(tmp_path)
    axes = tuple(
        axis.model_copy(update={'allowed_min': 0.99})
        if axis.axis_id == 'speaker-x'
        else axis
        for axis in original.axes
    )
    base_spec = build_robustness_spec(
        source_revision=revision,
        search_spec=search_spec,
        candidate=CadCandidate.model_validate_json(original.candidate_payload_json),
        candidate_set_sha256=original.candidate_set_sha256,
        nominal_objective=nominal,
        nominal_prediction_result_ref=original.nominal_prediction_result_ref,
        model_id=original.model_id,
        model_version=original.model_version,
        prediction_provider_id=original.prediction_provider_id,
        fidelity=original.fidelity,
        axes=axes,
        software_version='test',
        created_at_utc='2026-09-19T00:14:00+00:00',
    )
    model = DiscreteUncertaintyModel(
        model_id='mounting-alternatives',
        states=(
            ExplicitPerturbationState(
                state_id='negative',
                parameter_deltas={'speaker-x': -0.02},
                probability_weight=0.25,
            ),
            ExplicitPerturbationState(
                state_id='positive',
                parameter_deltas={'speaker-x': 0.01},
                probability_weight=0.75,
            ),
        ),
    )
    spec = derive_uncertainty_robustness_spec(
        base_spec,
        uncertainty_model=model,
        created_at_utc='2026-09-19T00:15:00+00:00',
    )

    result = evaluate_uncertainty_robustness(
        source_revision=revision,
        search_spec=search_spec,
        spec=spec,
        constraint_set=constraints,
        nominal_objective=nominal,
        evaluator=_o90b_linear_evaluator,
        created_at_utc='2026-09-19T00:16:00+00:00',
    )
    evaluation = result.evaluations[0]
    assert evaluation.probability_semantics == 'explicit_discrete_weights'
    assert evaluation.constraint_violation_probability == pytest.approx(0.25)
    assert evaluation.feasible_fraction == pytest.approx(2.0 / 3.0)
    assert evaluation.feasible_fraction != pytest.approx(
        1.0 - evaluation.constraint_violation_probability
    )
    assert any(not sample.feasible for sample in result.samples[1:])


def test_o90b_cancel_cache_resume_and_stale_reuse_protection(tmp_path) -> None:
    from htdt.optimization_robustness_uncertainty import (
        derive_uncertainty_robustness_spec,
        evaluate_uncertainty_robustness,
    )

    revision, constraints, search_spec, nominal, base_spec = _fixture(tmp_path)
    model = _o90b_distribution_model(base_spec)
    spec = derive_uncertainty_robustness_spec(
        base_spec,
        uncertainty_model=model,
        sample_count=9,
        seed=77,
        created_at_utc='2026-09-19T00:17:00+00:00',
    )
    repository = _authorities(tmp_path).robustness_repository
    repository.save_spec(base_spec)
    cancel_calls = {'count': 0}

    def cancel_after_one_perturbation() -> bool:
        cancel_calls['count'] += 1
        return cancel_calls['count'] > 2

    cancelled = evaluate_uncertainty_robustness(
        source_revision=revision,
        search_spec=search_spec,
        spec=spec,
        constraint_set=constraints,
        nominal_objective=nominal,
        evaluator=_o90b_linear_evaluator,
        cache=repository,
        cancel_requested=cancel_after_one_perturbation,
        created_at_utc='2026-09-19T00:18:00+00:00',
    )
    assert cancelled.status == 'cancelled'
    assert len(cancelled.samples) == 2
    assert repository.list_samples(spec.robustness_spec_id) == cancelled.samples

    replay_spec = derive_uncertainty_robustness_spec(
        base_spec,
        uncertainty_model=model,
        sample_count=9,
        seed=77,
        created_at_utc='2026-09-19T00:18:30+00:00',
    )
    assert replay_spec.robustness_spec_sha256 == spec.robustness_spec_sha256
    assert replay_spec.created_at_utc != spec.created_at_utc

    resumed = evaluate_uncertainty_robustness(
        source_revision=revision,
        search_spec=search_spec,
        spec=replay_spec,
        constraint_set=constraints,
        nominal_objective=nominal,
        evaluator=_o90b_linear_evaluator,
        cache=repository,
        created_at_utc='2026-09-19T00:18:00+00:00',
    )
    assert resumed.status == 'completed'
    assert resumed.reused_sample_ids == tuple(
        sample.sample_id for sample in cancelled.samples
    )
    assert len(resumed.computed_sample_ids) == spec.sample_count - len(cancelled.samples)
    assert repository.get_spec(spec.robustness_spec_id) == spec
    assert repository.list_samples(spec.robustness_spec_id) == resumed.samples
    assert repository.list_evaluations(spec.robustness_spec_id) == resumed.evaluations

    changed_spec = derive_uncertainty_robustness_spec(
        base_spec,
        uncertainty_model=model,
        sample_count=9,
        seed=78,
        created_at_utc='2026-09-19T00:19:00+00:00',
    )
    with pytest.raises(ValueError, match='stale or incompatible'):
        evaluate_uncertainty_robustness(
            source_revision=revision,
            search_spec=search_spec,
            spec=changed_spec,
            constraint_set=constraints,
            nominal_objective=nominal,
            evaluator=_o90b_linear_evaluator,
            completed_samples=resumed.samples,
            created_at_utc='2026-09-19T00:20:00+00:00',
        )

    from dataclasses import replace

    changed_revision = replace(
        revision,
        revision_id=f'{revision.revision_id}-changed',
    )
    with pytest.raises(ValueError, match='SceneRevision authority mismatch'):
        evaluate_uncertainty_robustness(
            source_revision=changed_revision,
            search_spec=search_spec,
            spec=spec,
            constraint_set=constraints,
            nominal_objective=nominal,
            evaluator=_o90b_linear_evaluator,
            created_at_utc='2026-09-19T00:20:00+00:00',
        )



def test_o90b_bounded_cancel_resume_reuses_pr149_samples(tmp_path) -> None:
    from htdt.optimization_robustness_multidimensional import (
        derive_multidimensional_robustness_spec,
        execute_multidimensional_robustness,
    )

    revision, constraints, search_spec, nominal, base_spec = _fixture(tmp_path)
    spec = derive_multidimensional_robustness_spec(
        base_spec,
        sample_count=5,
        seed=91,
        created_at_utc='2026-09-19T00:21:00+00:00',
    )
    repository = _authorities(tmp_path).robustness_repository
    repository.save_spec(base_spec)
    cancel_calls = {'count': 0}

    def cancel_after_one_perturbation() -> bool:
        cancel_calls['count'] += 1
        return cancel_calls['count'] > 2

    cancelled = execute_multidimensional_robustness(
        source_revision=revision,
        search_spec=search_spec,
        spec=spec,
        constraint_set=constraints,
        nominal_objective=nominal,
        evaluator=_o90b_linear_evaluator,
        cache=repository,
        cancel_requested=cancel_after_one_perturbation,
        created_at_utc='2026-09-19T00:22:00+00:00',
    )
    assert cancelled.status == 'cancelled'
    assert len(cancelled.samples) == 2

    replay_spec = derive_multidimensional_robustness_spec(
        base_spec,
        sample_count=5,
        seed=91,
        created_at_utc='2026-09-19T00:22:30+00:00',
    )
    assert replay_spec.robustness_spec_sha256 == spec.robustness_spec_sha256

    resumed = execute_multidimensional_robustness(
        source_revision=revision,
        search_spec=search_spec,
        spec=replay_spec,
        constraint_set=constraints,
        nominal_objective=nominal,
        evaluator=_o90b_linear_evaluator,
        cache=repository,
        created_at_utc='2026-09-19T00:22:00+00:00',
    )
    assert resumed.status == 'completed'
    assert resumed.reused_sample_ids == tuple(
        sample.sample_id for sample in cancelled.samples
    )
    assert len(resumed.computed_sample_ids) == spec.sample_count - len(
        cancelled.samples
    )
    assert repository.list_samples(spec.robustness_spec_id) == resumed.samples

def _o90_changed_constraint_set(document_id: str, kind: str) -> CadConstraintSet:
    from htdt.cad_constraint_models import (
        CadAllowedRegionConstraint,
        CadConstraintPoint2D,
        CadExclusionRegionConstraint,
        CadWallClearanceConstraint,
    )

    if kind == 'allowed':
        constraint = CadAllowedRegionConstraint(
            constraint_id='changed-allowed',
            name='Changed allowed region',
            entity_ids=('speaker-fl',),
            vertices=(
                CadConstraintPoint2D(x_m=4.0, y_m=0.0),
                CadConstraintPoint2D(x_m=5.0, y_m=0.0),
                CadConstraintPoint2D(x_m=5.0, y_m=2.0),
                CadConstraintPoint2D(x_m=4.0, y_m=2.0),
            ),
        )
    elif kind == 'exclusion':
        constraint = CadExclusionRegionConstraint(
            constraint_id='changed-exclusion',
            name='Changed exclusion region',
            entity_ids=('speaker-fl',),
            vertices=(
                CadConstraintPoint2D(x_m=0.5, y_m=0.5),
                CadConstraintPoint2D(x_m=1.5, y_m=0.5),
                CadConstraintPoint2D(x_m=1.5, y_m=1.5),
                CadConstraintPoint2D(x_m=0.5, y_m=1.5),
            ),
        )
    elif kind == 'wall':
        constraint = CadWallClearanceConstraint(
            constraint_id='changed-wall',
            name='Changed wall clearance',
            entity_ids=('speaker-fl',),
            wall_id='left',
            min_m=2.0,
        )
    else:
        raise AssertionError(kind)
    return CadConstraintSet(document_id=document_id, constraints=(constraint,))


@pytest.mark.parametrize('constraint_kind', ('allowed', 'exclusion', 'wall'))
def test_o90a_rejects_changed_constraint_snapshot_before_evaluation(
    tmp_path,
    constraint_kind,
) -> None:
    revision, _constraints, search_spec, nominal, spec = _fixture(tmp_path)
    changed_constraints = _o90_changed_constraint_set(DOCUMENT_ID, constraint_kind)
    evaluator_called = False

    def evaluator(_document: SceneDocument, sample_id: str) -> PerturbationObjectiveResult:
        nonlocal evaluator_called
        evaluator_called = True
        return _o90b_linear_evaluator(_document, sample_id)

    with pytest.raises(ValueError, match='constraint workspace authority mismatch'):
        evaluate_local_robustness(
            source_revision=revision,
            search_spec=search_spec,
            spec=spec,
            constraint_set=changed_constraints,
            nominal_objective=nominal,
            evaluator=evaluator,
        )

    assert not evaluator_called


class _O90CacheMustNotBeTouched:
    def save_spec(self, _spec):
        raise AssertionError('stale authority touched cache.save_spec')

    def list_reusable_samples(self, _spec):
        raise AssertionError('stale authority touched cache.list_reusable_samples')

    def save_sample(self, _sample):
        raise AssertionError('stale authority touched cache.save_sample')

    def save_evaluations(self, _evaluations):
        raise AssertionError('stale authority touched cache.save_evaluations')


def test_o90b_all_sampling_models_reject_stale_constraints_before_cache_access(
    tmp_path,
) -> None:
    from htdt.optimization_robustness import (
        DiscreteUncertaintyModel,
        EmpiricalUncertaintyModel,
        ExplicitPerturbationState,
    )
    from htdt.optimization_robustness_multidimensional import (
        derive_multidimensional_robustness_spec,
        execute_multidimensional_robustness,
    )
    from htdt.optimization_robustness_uncertainty import (
        derive_uncertainty_robustness_spec,
        evaluate_uncertainty_robustness,
    )

    revision, _constraints, search_spec, nominal, base_spec = _fixture(tmp_path)
    changed_constraints = _o90_changed_constraint_set(DOCUMENT_ID, 'allowed')
    cache = _O90CacheMustNotBeTouched()

    bounded = derive_multidimensional_robustness_spec(
        base_spec,
        sample_count=5,
        seed=501,
        created_at_utc='2026-09-19T00:30:00+00:00',
    )
    with pytest.raises(ValueError, match='constraint workspace authority mismatch'):
        execute_multidimensional_robustness(
            source_revision=revision,
            search_spec=search_spec,
            spec=bounded,
            constraint_set=changed_constraints,
            nominal_objective=nominal,
            evaluator=_o90b_linear_evaluator,
            cache=cache,
        )

    explicit_models = (
        (
            _o90b_distribution_model(base_spec),
            {'sample_count': 5, 'seed': 502},
        ),
        (
            EmpiricalUncertaintyModel(
                model_id='stale-empirical',
                samples=(
                    ExplicitPerturbationState(
                        state_id='observed',
                        parameter_deltas={'speaker-x': 0.01},
                    ),
                ),
            ),
            {},
        ),
        (
            DiscreteUncertaintyModel(
                model_id='stale-discrete',
                states=(
                    ExplicitPerturbationState(
                        state_id='mount-a',
                        parameter_deltas={'speaker-x': -0.01},
                    ),
                ),
            ),
            {},
        ),
    )
    for index, (model, kwargs) in enumerate(explicit_models):
        uncertainty_spec = derive_uncertainty_robustness_spec(
            base_spec,
            uncertainty_model=model,
            created_at_utc=f'2026-09-19T00:31:0{index}+00:00',
            **kwargs,
        )
        with pytest.raises(ValueError, match='constraint workspace authority mismatch'):
            evaluate_uncertainty_robustness(
                source_revision=revision,
                search_spec=search_spec,
                spec=uncertainty_spec,
                constraint_set=changed_constraints,
                nominal_objective=nominal,
                evaluator=_o90b_linear_evaluator,
                cache=cache,
            )


def test_o90b_stale_constraint_resume_leaves_cached_evidence_immutable(tmp_path) -> None:
    from htdt.optimization_robustness_uncertainty import (
        derive_uncertainty_robustness_spec,
        evaluate_uncertainty_robustness,
    )

    revision, constraints, search_spec, nominal, base_spec = _fixture(tmp_path)
    spec = derive_uncertainty_robustness_spec(
        base_spec,
        uncertainty_model=_o90b_distribution_model(base_spec),
        sample_count=5,
        seed=512,
        created_at_utc='2026-09-19T00:32:00+00:00',
    )
    repository = _authorities(tmp_path).robustness_repository
    repository.save_spec(base_spec)
    cancel_calls = {'count': 0}

    def cancel_after_one_perturbation() -> bool:
        cancel_calls['count'] += 1
        return cancel_calls['count'] > 2

    cancelled = evaluate_uncertainty_robustness(
        source_revision=revision,
        search_spec=search_spec,
        spec=spec,
        constraint_set=constraints,
        nominal_objective=nominal,
        evaluator=_o90b_linear_evaluator,
        cache=repository,
        cancel_requested=cancel_after_one_perturbation,
        created_at_utc='2026-09-19T00:33:00+00:00',
    )
    assert cancelled.status == 'cancelled'
    before = repository.list_samples(spec.robustness_spec_id)
    assert before == cancelled.samples

    with pytest.raises(ValueError, match='constraint workspace authority mismatch'):
        evaluate_uncertainty_robustness(
            source_revision=revision,
            search_spec=search_spec,
            spec=spec,
            constraint_set=_o90_changed_constraint_set(DOCUMENT_ID, 'allowed'),
            nominal_objective=nominal,
            evaluator=_o90b_linear_evaluator,
            cache=repository,
            created_at_utc='2026-09-19T00:34:00+00:00',
        )

    assert repository.list_samples(spec.robustness_spec_id) == before
    assert repository.list_evaluations(spec.robustness_spec_id) == ()


def test_cad_robustness_repository_closes_every_connection_without_gc(
    tmp_path,
    monkeypatch,
) -> None:
    import sqlite3

    revision, constraints, search_spec, nominal, spec = _fixture(tmp_path)
    samples, evaluations = evaluate_local_robustness(
        source_revision=revision,
        search_spec=search_spec,
        spec=spec,
        constraint_set=constraints,
        nominal_objective=nominal,
        evaluator=_o90b_linear_evaluator,
        created_at_utc='2026-09-19T00:35:00+00:00',
    )
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    search_repository = CadSearchRepository(scene_repository)
    objective_repository = _objective_repository(
        scene_repository,
        search_repository,
    )
    extended_repository = CadExtendedSearchRepository(search_repository)
    real_connect = CadRobustnessRepository._connect
    opened = []

    def tracked_connect(self):
        connection = real_connect(self)
        opened.append(connection)
        return connection

    monkeypatch.setattr(CadRobustnessRepository, '_connect', tracked_connect)
    repository = CadRobustnessRepository(
        scene_repository=scene_repository,
        search_repository=search_repository,
        objective_repository=objective_repository,
        extended_search_repository=extended_repository,
    )
    repository.save_spec(spec)
    repository.save_samples(samples)
    repository.save_evaluations(evaluations)
    assert repository.get_spec(spec.robustness_spec_id) == spec
    assert repository.list_samples(spec.robustness_spec_id) == samples
    assert repository.list_evaluations(spec.robustness_spec_id) == evaluations
    assert repository.list_specs_for_candidate(
        document_id=spec.document_id,
        scene_revision_id=spec.scene_revision_id,
        candidate_id=spec.candidate_id,
    ) == (spec,)

    conflicting = spec.model_copy(
        update={'robustness_spec_sha256': '0' * 64},
    )
    with pytest.raises(ValueError, match='identity hash mismatch'):
        repository.save_spec(conflicting)

    assert opened
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            connection.execute('SELECT 1')


@pytest.mark.parametrize('case', ('future', 'invalid_metadata', 'unrelated'))
def test_cad_robustness_repository_rejects_incompatible_native_db_without_mutation(
    tmp_path,
    case,
) -> None:
    import sqlite3

    from htdt.cad_schema import NATIVE_SCHEMA_VERSION, NativeSchemaError

    path = tmp_path / f'{case}.sqlite3'
    with sqlite3.connect(path) as connection:
        if case == 'future':
            connection.execute(
                'CREATE TABLE native_schema_metadata ('
                'singleton INTEGER PRIMARY KEY CHECK(singleton=1), '
                'schema_version INTEGER NOT NULL)'
            )
            connection.execute(
                'INSERT INTO native_schema_metadata(singleton, schema_version) '
                'VALUES (1, ?)',
                (NATIVE_SCHEMA_VERSION + 1,),
            )
            connection.execute('CREATE TABLE sentinel(value TEXT NOT NULL)')
            connection.execute("INSERT INTO sentinel VALUES ('keep')")
        elif case == 'invalid_metadata':
            connection.execute(
                'CREATE TABLE native_schema_metadata ('
                'singleton INTEGER PRIMARY KEY CHECK(singleton=1), '
                'schema_version INTEGER NOT NULL)'
            )
            connection.execute('CREATE TABLE sentinel(value TEXT NOT NULL)')
            connection.execute("INSERT INTO sentinel VALUES ('keep')")
        else:
            connection.execute('CREATE TABLE unrelated(value TEXT NOT NULL)')
            connection.execute("INSERT INTO unrelated VALUES ('keep')")

    def construct() -> CadRobustnessRepository:
        scene_repository = SceneRepository(path)
        search_repository = CadSearchRepository(scene_repository)
        return CadRobustnessRepository(
            scene_repository=scene_repository,
            search_repository=search_repository,
            objective_repository=CadObjectiveRepository(
                scene_repository,
                search_repository,
            ),
        )

    before = path.read_bytes()
    with pytest.raises(NativeSchemaError):
        construct()
    assert path.read_bytes() == before


def test_cad_robustness_repository_uses_native_schema_authority_for_new_and_legacy_db(
    tmp_path,
) -> None:
    import sqlite3

    from htdt.cad_schema import NATIVE_SCHEMA_VERSION, read_native_schema_version

    def construct(db_path):
        scene_repository = SceneRepository(db_path)
        search_repository = CadSearchRepository(scene_repository)
        return CadRobustnessRepository(
            scene_repository=scene_repository,
            search_repository=search_repository,
            objective_repository=CadObjectiveRepository(
                scene_repository,
                search_repository,
            ),
        )

    new_path = tmp_path / 'new-robust.sqlite3'
    construct(new_path)
    assert read_native_schema_version(new_path) == NATIVE_SCHEMA_VERSION

    legacy_path = tmp_path / 'legacy-robust.sqlite3'
    with sqlite3.connect(legacy_path) as connection:
        connection.execute(
            'CREATE TABLE cad_constraint_workspaces('
            'document_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, '
            'updated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL)'
        )
        connection.execute(
            "INSERT INTO cad_constraint_workspaces VALUES "
            "('doc-1', 1, '2026-09-17T00:00:00+00:00', 'preserve')"
        )

    construct(legacy_path)
    assert read_native_schema_version(legacy_path) == NATIVE_SCHEMA_VERSION
    with sqlite3.connect(legacy_path) as connection:
        assert connection.execute(
            "SELECT payload_json FROM cad_constraint_workspaces WHERE document_id='doc-1'"
        ).fetchone() == ('preserve',)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert 'cad_robustness_specs' in tables
    assert 'cad_perturbation_samples' in tables
    assert 'cad_robustness_evaluations' in tables




def _direction_fixture_definition(
    objective_id: str,
    *,
    quantity: str,
    unit: str,
    direction: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> ObjectiveDefinition:
    domain = (
        ObjectiveValidDomain(kind='finite_real')
        if minimum is None and maximum is None
        else ObjectiveValidDomain(
            kind='bounded_real',
            minimum=minimum,
            maximum=maximum,
        )
    )
    return ObjectiveDefinition(
        objective_id=objective_id,
        quantity=quantity,
        unit=unit,
        direction=direction,
        valid_domain=domain,
        comparison_model_id='direction-fixture-model',
        comparison_model_version='1',
    )


def test_o90a_maximize_sampled_worst_uses_low_side_and_round_trips(tmp_path) -> None:
    revision, constraints, search_spec, _legacy_nominal, base_spec = _fixture(tmp_path)
    candidate = CadCandidate.model_validate_json(base_spec.candidate_payload_json)
    definition = _direction_fixture_definition(
        'fixture.coverage',
        quantity='coverage_fraction',
        unit='1',
        direction='maximize',
        minimum=0.0,
        maximum=1.0,
    )
    prediction_ref = f'prediction:maximize:{candidate.candidate_id}'
    nominal = build_objective_evaluation(
        revision,
        search_spec,
        candidate.candidate_id,
        ObjectiveVector(
            candidate_id=candidate.candidate_id,
            metrics=(
                ObjectiveMetric(
                    objective_id=definition.objective_id,
                    value=0.8,
                    unit=definition.unit,
                    direction=definition.direction,
                    definition=definition,
                ),
            ),
        ),
        evaluation_spec={
            'algorithm_version': 'fixture-maximize-1',
            'objective_method': 'fixture-metrics-1',
            'objectives': [definition.objective_id],
            'metrics': [
                {
                    'objective_id': definition.objective_id,
                    'value': 0.8,
                    'unit': definition.unit,
                    'direction': definition.direction,
                    'definition': definition.model_dump(mode='json'),
                },
            ],
        },
        input_refs=(
            CadObjectiveInputRef(
                evidence_class='derived',
                source_kind='candidate_geometry',
                source_id=candidate.candidate_id,
            ),
            CadObjectiveInputRef(
                evidence_class='predicted',
                source_kind='prediction_fixture',
                source_id=prediction_ref,
            ),
        ),
    )
    spec = build_robustness_spec(
        source_revision=revision,
        search_spec=search_spec,
        candidate=candidate,
        candidate_set_sha256=base_spec.candidate_set_sha256,
        nominal_objective=nominal,
        nominal_prediction_result_ref=prediction_ref,
        model_id=base_spec.model_id,
        model_version=base_spec.model_version,
        prediction_provider_id=base_spec.prediction_provider_id,
        fidelity=base_spec.fidelity,
        axes=base_spec.axes,
        software_version='test',
        created_at_utc='2026-09-19T01:00:00+00:00',
    )

    def evaluator(document: SceneDocument, sample_id: str) -> PerturbationObjectiveResult:
        speaker = document.entity('speaker-fl')
        value = 0.8 + (speaker.position.x_m - 1.0) * 2.0
        return PerturbationObjectiveResult(
            prediction_result_ref=f'prediction:{sample_id}',
            objective_vector=ObjectiveVector(
                candidate_id=sample_id,
                metrics=(
                    ObjectiveMetric(
                        objective_id=definition.objective_id,
                        value=value,
                        unit=definition.unit,
                        direction=definition.direction,
                        definition=definition,
                    ),
                ),
            ),
        )

    samples, evaluations = evaluate_local_robustness(
        source_revision=revision,
        search_spec=search_spec,
        spec=spec,
        constraint_set=constraints,
        nominal_objective=nominal,
        evaluator=evaluator,
        created_at_utc='2026-09-19T01:01:00+00:00',
    )
    evaluation = evaluations[0]
    scored_values = [
        sample.objective_vector.metric(definition.objective_id).comparison_value()
        for sample in samples
        if sample.objective_vector is not None
    ]

    assert evaluation.direction == 'maximize'
    assert evaluation.sampled_worst_value == pytest.approx(min(scored_values))
    assert evaluation.sampled_worst_value < evaluation.nominal_value
    assert evaluation.objective_definition is not None
    assert evaluation.objective_definition.definition_id == definition.definition_id

    authorities = _authorities(tmp_path)
    authorities.objective_repository.save_evaluation(nominal)
    repository = authorities.robustness_repository
    repository.save_spec(spec)
    repository.save_samples(samples)
    repository.save_evaluations(evaluations)
    reopened = _authorities(tmp_path).robustness_repository
    assert reopened.get_spec(spec.robustness_spec_id) == spec
    assert reopened.list_samples(spec.robustness_spec_id) == samples
    assert reopened.list_evaluations(spec.robustness_spec_id) == evaluations


def test_o90_robust_pareto_preserves_mixed_objective_directions() -> None:
    from htdt.optimization_robustness import (
        RobustnessEvaluation,
        SampledObjectiveEnvelope,
        canonical_robustness_sha256,
    )
    from htdt.optimization_robustness_multidimensional import (
        RobustParetoSelection,
        build_nominal_robust_pareto_vector,
        robust_pareto_front,
    )

    error = _direction_fixture_definition(
        'fixture.error',
        quantity='response_error',
        unit='dB',
        direction='minimize',
        minimum=0.0,
    )
    coverage = _direction_fixture_definition(
        'fixture.coverage',
        quantity='coverage_fraction',
        unit='1',
        direction='maximize',
        minimum=0.0,
        maximum=1.0,
    )

    def nominal_vector(
        candidate_id: str,
        error_value: float,
        coverage_value: float,
    ) -> ObjectiveVector:
        return ObjectiveVector(
            candidate_id=candidate_id,
            metrics=(
                ObjectiveMetric(
                    objective_id=error.objective_id,
                    value=error_value,
                    unit=error.unit,
                    direction=error.direction,
                    definition=error,
                ),
                ObjectiveMetric(
                    objective_id=coverage.objective_id,
                    value=coverage_value,
                    unit=coverage.unit,
                    direction=coverage.direction,
                    definition=coverage,
                ),
            ),
        )

    def robust_evaluation(
        candidate_id: str,
        definition: ObjectiveDefinition,
        nominal_value: float,
        worst_value: float,
    ) -> RobustnessEvaluation:
        nominal_sample_id = f'{candidate_id}-{definition.objective_id}-nominal'
        worst_sample_id = f'{candidate_id}-{definition.objective_id}-worst'
        sampled_min_value = min(nominal_value, worst_value)
        sampled_max_value = max(nominal_value, worst_value)
        envelope = SampledObjectiveEnvelope(
            sampled_min_sample_id=(
                worst_sample_id
                if worst_value == sampled_min_value
                else nominal_sample_id
            ),
            sampled_min_value=sampled_min_value,
            sampled_max_sample_id=(
                worst_sample_id
                if worst_value == sampled_max_value
                else nominal_sample_id
            ),
            sampled_max_value=sampled_max_value,
            percentile_values=None,
        )
        identity = {
            'schema_version': 1,
            'robustness_spec_id': f'rob-{candidate_id}-{definition.objective_id}',
            'robustness_spec_sha256': '1' * 64,
            'candidate_id': candidate_id,
            'objective_id': definition.objective_id,
            'objective_unit': definition.unit,
            'direction': definition.direction,
            'objective_definition': definition.model_dump(mode='json'),
            'nominal_sample_id': nominal_sample_id,
            'nominal_value': nominal_value,
            'local_sensitivities': [],
            'sampled_worst_semantics': 'sampled_worst',
            'sampled_worst_sample_id': worst_sample_id,
            'sampled_worst_value': worst_value,
            'sample_ids': [nominal_sample_id, worst_sample_id],
            'infeasible_sample_ids': [],
            'failed_sample_ids': [],
            'sampled_envelope': envelope.model_dump(mode='json'),
            'feasible_fraction': 1.0,
            'sampling_provenance_sha256': '2' * 64,
            'percentile_semantics': 'not_available_bounded_interval',
        }
        digest = canonical_robustness_sha256(identity)
        return RobustnessEvaluation(
            **identity,
            evaluation_id=f're-{digest[:24]}',
            evaluation_sha256=digest,
            created_at_utc='2026-09-19T01:02:00+00:00',
        )

    a = nominal_vector('a', 1.0, 0.70)
    b = nominal_vector('b', 2.0, 0.90)
    a_robust = (
        robust_evaluation('a', error, 1.0, 1.5),
        robust_evaluation('a', coverage, 0.70, 0.60),
    )
    b_robust = (
        robust_evaluation('b', error, 2.0, 2.2),
        robust_evaluation('b', coverage, 0.90, 0.80),
    )
    selection = RobustParetoSelection(
        nominal_objective_ids=(error.objective_id, coverage.objective_id),
        robustness_objective_ids=(error.objective_id, coverage.objective_id),
    )

    derived = build_nominal_robust_pareto_vector(a, a_robust, selection)
    assert derived.metric('nominal::fixture.error').direction == 'minimize'
    assert derived.metric('nominal::fixture.coverage').direction == 'maximize'
    assert derived.metric('robust.sampled_worst::fixture.error').direction == 'minimize'
    assert derived.metric('robust.sampled_worst::fixture.coverage').direction == 'maximize'
    assert (
        derived.metric('robust.sampled_worst::fixture.coverage').value
        == pytest.approx(0.60)
    )

    result = robust_pareto_front(
        ((a, a_robust), (b, b_robust)),
        selection,
    )
    assert result.algorithm_version == 'pareto-front-2'
    assert result.non_dominated_candidate_ids == ('a', 'b')
    assert result.dominated_by == {'a': (), 'b': ()}
