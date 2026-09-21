from __future__ import annotations

import pytest

from htdt import cad_adaptive_extended
from htdt.cad_adaptive_extended import (
    build_adaptive_extended_observation,
    build_adaptive_extended_plan,
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
from htdt.cad_synthetic_demo import seed_synthetic_optimization_demo


def _seeded(tmp_path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    result = seed_synthetic_optimization_demo(scene_repository)

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
    extended = CadExtendedSearchRepository(search, validation)
    adaptive_extended = CadAdaptiveExtendedRepository(extended, validation)
    return (
        scene_repository,
        result,
        search,
        validation,
        extended,
        adaptive_extended,
    )


def _rebound_plan(plan, **updates):
    """Rehash a tampered plan so only canonical replay can reject it."""

    tampered = plan.model_copy(update=updates)
    return tampered.model_copy(update={
        'adaptive_extended_sha256': cad_adaptive_extended._digest(
            tampered.identity_payload()
        ),
    })


def _supersede_first_predicted(
    adaptive_extended,
    extended_spec,
    candidate_set_sha256,
):
    current = adaptive_extended.current_observations(
        extended_spec.extended_search_id
    )
    target = next(item for item in current if item.measured_value is None)
    replacement = build_adaptive_extended_observation(
        extended_spec=extended_spec,
        candidate_set_sha256=candidate_set_sha256,
        candidate_id=target.candidate_id,
        evidence_scope='synthetic_fixture',
        objective_id=target.objective_id,
        unit=target.unit,
        predicted_value=target.predicted_value,
        prediction_source_kind=target.prediction_source_kind,
        prediction_source_id=target.prediction_source_id,
        measured_value=target.predicted_value + 0.05,
        measurement_source_kind='synthetic_measurement_fixture',
        measurement_source_id=f'synthetic-later:{target.candidate_id}',
        supersedes_observation_sha256=target.observation_sha256,
    )
    adaptive_extended.save_observation(replacement)
    return target, replacement


def test_adaptive_extended_plan_persists_and_reopens_unchanged(tmp_path):
    (
        _scene_repository,
        result,
        _search,
        _validation,
        _extended,
        adaptive_extended,
    ) = _seeded(tmp_path)

    plan = adaptive_extended.get_plan(result.adaptive_extended_plan_id)
    assert plan is not None
    assert plan.execution_scope == 'development_synthetic'
    assert plan.proposal_limit == 20
    assert len(plan.proposals) <= plan.proposal_limit

    assert adaptive_extended.get_plan(plan.plan_id) == plan
    assert (
        adaptive_extended.find_plan_by_sha(
            result.extended_search_id,
            plan.adaptive_extended_sha256,
        )
        == plan
    )
    assert adaptive_extended.list_plans(result.extended_search_id) == (plan,)


def test_save_plan_rejects_superseded_observation_authority(tmp_path):
    (
        scene_repository,
        result,
        search,
        validation_repository,
        extended,
        adaptive_extended,
    ) = _seeded(tmp_path)

    spec = extended.get_spec(result.extended_search_id)
    assert spec is not None
    base = search.get(result.search_spec_id)
    assert base is not None
    capability = extended.get_capability(spec.capability_id)
    assert capability is not None
    validation = validation_repository.get(result.validation_id)
    assert validation is not None
    candidates = generate_extended_candidates(
        scene_repository,
        base,
        spec,
        limit=500,
    ).candidates
    snapshot = adaptive_extended.current_observations(
        result.extended_search_id
    )

    # A plan built honestly from the pre-supersession observation heads.
    stale_plan = build_adaptive_extended_plan(
        base_spec=base,
        base_candidate_set_sha256=spec.base_candidate_set_sha256,
        extended_spec=spec,
        extended_candidate_set_sha256=result.extended_candidate_set_sha256,
        capability_id=capability.capability_id,
        capability_sha256=capability.capability_sha256,
        extended_model_id=capability.model_id,
        extended_model_version=capability.model_version,
        validation=validation,
        candidates=candidates,
        observations=snapshot,
        execution_scope='development_synthetic',
        length_scale_normalized=0.6,
        proposal_limit=15,
    )

    target, _replacement = _supersede_first_predicted(
        adaptive_extended,
        spec,
        result.extended_candidate_set_sha256,
    )
    assert target.observation_sha256 in stale_plan.observation_sha256s

    with pytest.raises(
        ValueError,
        match='observation authority is not current',
    ):
        adaptive_extended.save_plan(stale_plan)


def test_save_plan_rejects_rehashed_fabricated_planner_output(tmp_path):
    (
        _scene_repository,
        result,
        _search,
        _validation,
        _extended,
        adaptive_extended,
    ) = _seeded(tmp_path)

    plan = adaptive_extended.get_plan(result.adaptive_extended_plan_id)
    assert plan is not None
    proposals = plan.proposals

    # Caller-fabricated acquisition score.
    forged_score = _rebound_plan(
        plan,
        proposals=(
            proposals[0].model_copy(update={
                'acquisition_score': proposals[0].acquisition_score + 0.5,
            }),
        )
        + proposals[1:],
    )
    with pytest.raises(ValueError, match='canonical planner replay'):
        adaptive_extended.save_plan(forged_score)

    # Caller-fabricated GP uncertainty/corrected mean.
    estimate = proposals[0].objectives[0]
    forged_estimate = _rebound_plan(
        plan,
        proposals=(
            proposals[0].model_copy(update={
                'objectives': (
                    estimate.model_copy(update={
                        'corrected_mean': estimate.corrected_mean + 1.0,
                        'residual_uncertainty': 0.0,
                    }),
                )
                + proposals[0].objectives[1:],
            }),
        )
        + proposals[1:],
    )
    with pytest.raises(ValueError, match='canonical planner replay'):
        adaptive_extended.save_plan(forged_estimate)

    # Caller-fabricated proposal order and selected candidate.
    reversed_order = tuple(reversed(proposals))
    forged_order = _rebound_plan(
        plan,
        proposals=reversed_order,
        selected_candidate_id=reversed_order[0].candidate_id,
    )
    with pytest.raises(ValueError, match='canonical planner replay'):
        adaptive_extended.save_plan(forged_order)

    # Caller-fabricated feature authority.
    forged_feature = _rebound_plan(
        plan,
        features=(
            plan.features[0].model_copy(update={
                'scale': plan.features[0].scale * 2.0,
            }),
        )
        + plan.features[1:],
    )
    with pytest.raises(ValueError, match='canonical planner replay'):
        adaptive_extended.save_plan(forged_feature)

    # Caller-fabricated training candidate list.
    forged_training = _rebound_plan(
        plan,
        training_candidate_ids=(
            proposals[0].candidate_id,
        )
        + plan.training_candidate_ids[1:],
    )
    with pytest.raises(ValueError, match='canonical planner replay'):
        adaptive_extended.save_plan(forged_training)

    # Dropped consumed observation keeps a current-only set but is not the
    # exact planner output.
    forged_observations = _rebound_plan(
        plan,
        observation_sha256s=plan.observation_sha256s[1:],
    )
    with pytest.raises(ValueError, match='canonical planner replay'):
        adaptive_extended.save_plan(forged_observations)

    # An observation SHA outside the current heads fails closed earlier.
    forged_unknown = _rebound_plan(
        plan,
        observation_sha256s=plan.observation_sha256s + ('f' * 64,),
    )
    with pytest.raises(
        ValueError,
        match='observation authority is not current',
    ):
        adaptive_extended.save_plan(forged_unknown)


def test_authoritative_reads_fail_closed_on_stale_plan(tmp_path):
    (
        _scene_repository,
        result,
        _search,
        validation_repository,
        extended,
        adaptive_extended,
    ) = _seeded(tmp_path)

    stale_plan = adaptive_extended.get_plan(result.adaptive_extended_plan_id)
    assert stale_plan is not None
    spec = extended.get_spec(result.extended_search_id)
    assert spec is not None
    _supersede_first_predicted(
        adaptive_extended,
        spec,
        result.extended_candidate_set_sha256,
    )

    # Every authoritative read fails closed on the stale persisted row.
    with pytest.raises(
        ValueError,
        match='observation authority is not current',
    ):
        adaptive_extended.get_plan(stale_plan.plan_id)
    with pytest.raises(
        ValueError,
        match='observation authority is not current',
    ):
        adaptive_extended.find_plan_by_sha(
            result.extended_search_id,
            stale_plan.adaptive_extended_sha256,
        )
    with pytest.raises(
        ValueError,
        match='observation authority is not current',
    ):
        adaptive_extended.list_plans(result.extended_search_id)

    # The canonical planner rebuild from current heads still persists and
    # reopens unchanged.
    service = CadAdaptiveExtendedPlannerService(
        extended,
        validation_repository,
        adaptive_extended,
    )
    rebuilt = service.build_and_save(
        extended_search_id=result.extended_search_id,
        validation_id=result.validation_id,
        execution_scope='development_synthetic',
        length_scale_normalized=0.6,
        proposal_limit=15,
    )
    assert rebuilt.adaptive_extended_sha256 != stale_plan.adaptive_extended_sha256
    assert adaptive_extended.get_plan(rebuilt.plan_id) == rebuilt
    assert (
        adaptive_extended.find_plan_by_sha(
            result.extended_search_id,
            rebuilt.adaptive_extended_sha256,
        )
        == rebuilt
    )
    # The stale row remains in history, so the listing still fails closed.
    with pytest.raises(
        ValueError,
        match='observation authority is not current',
    ):
        adaptive_extended.list_plans(result.extended_search_id)
