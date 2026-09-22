from __future__ import annotations

import json

import pytest

from htdt.cad_adaptive_repository import CadAdaptivePlanRepository
from htdt.cad_adaptive_service import CadAdaptivePlannerService
from htdt.cad_adaptive_extended import (
    build_adaptive_extended_observation,
    synthetic_observation_source_ref,
)
from htdt.cad_adaptive_extended_repository import CadAdaptiveExtendedRepository
from htdt.cad_adaptive_extended_service import CadAdaptiveExtendedPlannerService
from htdt.cad_extended_search import generate_extended_candidates
from htdt.cad_extended_search_repository import CadExtendedSearchRepository
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_model_validation_repository import CadModelValidationRepository
from htdt.cad_objective_repository import CadObjectiveRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_roomsim_repository import CadRoomSimRepository
from htdt.cad_search_repository import CadSearchRepository
from htdt.measurement_editor import measurement_evidence_label, measurement_is_synthetic
from htdt.cad_synthetic_demo import (
    SYNTHETIC_DEMO_DOCUMENT_ID,
    seed_synthetic_optimization_demo,
)


def _repositories(scene_repository):
    search = CadSearchRepository(scene_repository)
    measurements = CadMeasurementRepository(scene_repository)
    roomsim = CadRoomSimRepository(scene_repository, search)
    objectives = CadObjectiveRepository(scene_repository, search)
    validation = CadModelValidationRepository(
        search,
        roomsim,
        measurements,
        objectives,
    )
    adaptive = CadAdaptivePlanRepository(search, validation, objectives)
    extended = CadExtendedSearchRepository(search, validation)
    adaptive_extended = CadAdaptiveExtendedRepository(extended, validation)
    return (
        search,
        measurements,
        roomsim,
        objectives,
        validation,
        adaptive,
        extended,
        adaptive_extended,
    )


def test_synthetic_demo_persists_o10_through_o80_without_owned_room_promotion(tmp_path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')

    result = seed_synthetic_optimization_demo(scene_repository)

    assert result.document_id == SYNTHETIC_DEMO_DOCUMENT_ID
    (
        search,
        measurements,
        _roomsim,
        objectives,
        validation_repository,
        adaptive_repository,
        extended_repository,
        adaptive_extended_repository,
    ) = _repositories(scene_repository)

    spec = search.get(result.search_spec_id)
    assert spec is not None
    assert spec.document_id == SYNTHETIC_DEMO_DOCUMENT_ID

    record = validation_repository.get(result.validation_id)
    assert record is not None
    assert record.evidence_scope == 'synthetic_fixture'
    assert record.recommendation_gate == 'disabled'
    assert record.gate_reasons == (
        'automatic recommendation requires owned-room evidence',
    )
    assert record.residual_gate == 'pass'
    assert all(check.gate == 'pass' for check in record.trend_checks)
    assert all(check.gate == 'pass' for check in record.sensitivity_checks)
    assert all(check.gate == 'pass' for check in record.separation_checks)
    assert all(check.passed for check in record.applicability_checks)
    assert validation_repository.latest_eligible_for_search_spec(
        result.search_spec_id
    ) is None

    plan = adaptive_repository.get(result.adaptive_plan_id)
    assert plan is not None
    assert plan.execution_scope == 'development_synthetic'
    assert plan.source_evidence_scope == 'synthetic_fixture'
    assert plan.validation_recommendation_gate == 'disabled'
    assert plan.selected_candidate_id == result.adaptive_selected_candidate_id
    assert len(plan.proposals) == 2

    all_measurements = measurements.list_measurements(
        SYNTHETIC_DEMO_DOCUMENT_ID
    )
    assert len(all_measurements) == 5
    for measurement in all_measurements:
        provenance = json.loads(measurement.provenance_json)
        assert provenance['validation_scope'] == 'synthetic_fixture'
        assert provenance['physical_measurement'] is False
        assert measurement_is_synthetic(measurement)
        assert measurement_evidence_label(measurement) == 'Synthetic'

    evaluations = objectives.list_evaluations(result.search_spec_id)
    predicted_candidates = {
        evaluation.candidate_id
        for evaluation in evaluations
        if any(
            ref.evidence_class == 'predicted'
            for ref in evaluation.input_refs
        )
    }
    assert len(predicted_candidates) == 6

    extended_spec = extended_repository.get_spec(result.extended_search_id)
    assert extended_spec is not None
    capability = extended_repository.get_capability(
        extended_spec.capability_id
    )
    assert capability is not None
    assert capability.evidence_scope == 'synthetic_fixture'
    assert capability.validation_id is None
    extended_page = generate_extended_candidates(
        scene_repository,
        spec,
        extended_spec,
        limit=50,
    )
    assert extended_page.feasible_candidate_count == 18
    assert (
        extended_page.candidate_set_sha256
        == result.extended_candidate_set_sha256
    )

    adaptive_extended_plan = adaptive_extended_repository.get_plan(
        result.adaptive_extended_plan_id
    )
    assert adaptive_extended_plan is not None
    assert adaptive_extended_plan.execution_scope == 'development_synthetic'
    assert adaptive_extended_plan.source_evidence_scope == 'synthetic_fixture'
    assert adaptive_extended_plan.selected_candidate_id == (
        result.adaptive_extended_selected_candidate_id
    )
    feature_ids = tuple(
        feature.feature_id for feature in adaptive_extended_plan.features
    )
    assert feature_ids == (
        'base:synthetic-fl:x',
        'extended:synthetic-fl:aim_yaw_deg',
    )
    assert tuple(feature.unit for feature in adaptive_extended_plan.features) == (
        'm',
        'deg',
    )
    assert tuple(
        feature.scale for feature in adaptive_extended_plan.features
    ) == pytest.approx((1.0, 30.0))
    measured_ids = set(
        adaptive_extended_plan.excluded_measured_candidate_ids
    )
    assert measured_ids
    assert not measured_ids.intersection(
        proposal.candidate_id
        for proposal in adaptive_extended_plan.proposals
    )
    observations = adaptive_extended_repository.list_observations(
        result.extended_search_id
    )
    assert len(observations) == 18
    assert sum(item.measured_value is not None for item in observations) == 12

    service = CadAdaptivePlannerService(
        search,
        objectives,
        validation_repository,
        adaptive_repository,
    )
    with pytest.raises(
        ValueError,
        match='production adaptive planning requires',
    ):
        service.build_and_save(
            validation_id=result.validation_id,
            execution_scope='production_owned_room',
        )

    adaptive_extended_service = CadAdaptiveExtendedPlannerService(
        extended_repository,
        validation_repository,
        adaptive_extended_repository,
    )
    with pytest.raises(
        ValueError,
        match='production adaptive planning requires',
    ):
        adaptive_extended_service.build_and_save(
            extended_search_id=result.extended_search_id,
            validation_id=result.validation_id,
            execution_scope='production_owned_room',
        )


def test_synthetic_demo_refuses_duplicate_seed_in_same_data_directory(tmp_path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    seed_synthetic_optimization_demo(scene_repository)

    with pytest.raises(ValueError, match='already exists'):
        seed_synthetic_optimization_demo(scene_repository)


def test_adaptive_extended_observation_supersession_excludes_new_measurement(
    tmp_path,
):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    result = seed_synthetic_optimization_demo(scene_repository)
    (
        search,
        _measurements,
        _roomsim,
        _objectives,
        validation_repository,
        _adaptive_repository,
        extended_repository,
        adaptive_extended_repository,
    ) = _repositories(scene_repository)

    extended_spec = extended_repository.get_spec(result.extended_search_id)
    assert extended_spec is not None
    current = adaptive_extended_repository.current_observations(
        result.extended_search_id
    )
    target = next(item for item in current if item.measured_value is None)
    replacement = build_adaptive_extended_observation(
        extended_spec=extended_spec,
        candidate_set_sha256=result.extended_candidate_set_sha256,
        candidate_id=target.candidate_id,
        evidence_scope='synthetic_fixture',
        objective_id=target.objective_id,
        unit=target.unit,
        predicted_value=target.predicted_value,
        prediction_source=target.prediction_source,
        measured_value=target.predicted_value + 0.05,
        measurement_source=synthetic_observation_source_ref(
            f'synthetic-later:{target.candidate_id}',
            {
                'kind': 'synthetic_measurement_fixture',
                'candidate_id': target.candidate_id,
                'objective_id': target.objective_id,
                'measured_value': target.predicted_value + 0.05,
            },
        ),
        supersedes_observation_sha256=target.observation_sha256,
    )
    adaptive_extended_repository.save_observation(replacement)

    assert len(
        adaptive_extended_repository.list_observations(
            result.extended_search_id
        )
    ) == 19
    heads = adaptive_extended_repository.current_observations(
        result.extended_search_id
    )
    assert len(heads) == 18
    current_target = next(
        item
        for item in heads
        if item.candidate_id == target.candidate_id
        and item.objective_id == target.objective_id
    )
    assert current_target.observation_sha256 == replacement.observation_sha256
    assert current_target.measured_value is not None

    service = CadAdaptiveExtendedPlannerService(
        extended_repository,
        validation_repository,
        adaptive_extended_repository,
    )
    plan = service.build_and_save(
        extended_search_id=result.extended_search_id,
        validation_id=result.validation_id,
        execution_scope='development_synthetic',
        length_scale_normalized=0.45,
        proposal_limit=20,
    )
    assert target.candidate_id in plan.excluded_measured_candidate_ids
    assert target.candidate_id not in {
        proposal.candidate_id for proposal in plan.proposals
    }

    repeated = service.build_and_save(
        extended_search_id=result.extended_search_id,
        validation_id=result.validation_id,
        execution_scope='development_synthetic',
        length_scale_normalized=0.45,
        proposal_limit=20,
    )
    assert repeated.plan_id == plan.plan_id
    assert (
        repeated.adaptive_extended_sha256
        == plan.adaptive_extended_sha256
    )
