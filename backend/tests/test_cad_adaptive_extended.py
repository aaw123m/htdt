from __future__ import annotations

from contextlib import closing
import sqlite3
import threading

import pytest

from htdt import cad_adaptive_extended
from htdt.cad_adaptive_extended import (
    build_adaptive_extended_observation,
    build_adaptive_extended_plan,
)
from htdt.cad_adaptive_extended_repository import (
    AdaptiveObservationConflictError,
    CadAdaptiveExtendedRepository,
)
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


def _measured_successor(
    extended_spec,
    candidate_set_sha256,
    target,
    *,
    source_tag,
):
    """A second measured child claiming ``target`` as its predecessor."""

    return build_adaptive_extended_observation(
        extended_spec=extended_spec,
        candidate_set_sha256=candidate_set_sha256,
        candidate_id=target.candidate_id,
        evidence_scope='synthetic_fixture',
        objective_id=target.objective_id,
        unit=target.unit,
        predicted_value=target.predicted_value,
        prediction_source_kind=target.prediction_source_kind,
        prediction_source_id=target.prediction_source_id,
        measured_value=target.predicted_value + 0.09,
        measurement_source_kind='synthetic_measurement_fixture',
        measurement_source_id=f'synthetic-{source_tag}:{target.candidate_id}',
        supersedes_observation_sha256=target.observation_sha256,
    )


def _observation_head(adaptive_extended, extended_search_id, target):
    return next(
        item
        for item in adaptive_extended.current_observations(extended_search_id)
        if item.candidate_id == target.candidate_id
        and item.objective_id == target.objective_id
    )


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


def test_save_observation_rejects_stale_head_after_commit(tmp_path):
    (
        _scene_repository,
        result,
        _search,
        _validation,
        extended,
        adaptive_extended,
    ) = _seeded(tmp_path)
    spec = extended.get_spec(result.extended_search_id)
    assert spec is not None

    # H -> A commits; a second child still claiming H is now stale.
    target, replacement = _supersede_first_predicted(
        adaptive_extended,
        spec,
        result.extended_candidate_set_sha256,
    )
    stale = _measured_successor(
        spec,
        result.extended_candidate_set_sha256,
        target,
        source_tag='stale',
    )
    assert stale.observation_sha256 != replacement.observation_sha256
    with pytest.raises(
        AdaptiveObservationConflictError,
        match='must supersede the current record SHA',
    ):
        adaptive_extended.save_observation(stale)

    assert (
        _observation_head(
            adaptive_extended,
            result.extended_search_id,
            target,
        ).observation_sha256
        == replacement.observation_sha256
    )


def test_save_observation_concurrent_writers_single_committed_child(tmp_path):
    (
        _scene_repository,
        result,
        _search,
        _validation,
        extended,
        adaptive_extended,
    ) = _seeded(tmp_path)
    spec = extended.get_spec(result.extended_search_id)
    assert spec is not None
    current = adaptive_extended.current_observations(result.extended_search_id)
    target = next(item for item in current if item.measured_value is None)

    contender_a = _measured_successor(
        spec,
        result.extended_candidate_set_sha256,
        target,
        source_tag='race-a',
    )
    contender_b = _measured_successor(
        spec,
        result.extended_candidate_set_sha256,
        target,
        source_tag='race-b',
    )

    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def attempt(key, observation) -> None:
        barrier.wait(timeout=10)
        try:
            adaptive_extended.save_observation(observation)
            outcomes[key] = 'saved'
        except AdaptiveObservationConflictError:
            outcomes[key] = 'conflict'

    threads = (
        threading.Thread(target=attempt, args=('a', contender_a)),
        threading.Thread(target=attempt, args=('b', contender_b)),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()

    # Two writers racing from one head cannot both advance it.
    assert sorted(outcomes.values()) == ['conflict', 'saved']
    winner = contender_a if outcomes['a'] == 'saved' else contender_b
    loser = contender_b if outcomes['a'] == 'saved' else contender_a
    assert (
        _observation_head(
            adaptive_extended,
            result.extended_search_id,
            target,
        ).observation_sha256
        == winner.observation_sha256
    )

    # The loser's claim against the moved head stays rejected and the
    # chain still reads as a single-head history.
    with pytest.raises(AdaptiveObservationConflictError):
        adaptive_extended.save_observation(loser)
    assert (
        _observation_head(
            adaptive_extended,
            result.extended_search_id,
            target,
        ).observation_sha256
        == winner.observation_sha256
    )


def test_save_observation_root_rules_unchanged(tmp_path):
    (
        _scene_repository,
        result,
        _search,
        _validation,
        extended,
        adaptive_extended,
    ) = _seeded(tmp_path)
    spec = extended.get_spec(result.extended_search_id)
    assert spec is not None
    current = adaptive_extended.current_observations(result.extended_search_id)
    target = next(item for item in current if item.measured_value is None)

    # A second predecessor-free record for a key that already has history
    # is rejected — it does not claim the current head.
    second_root = build_adaptive_extended_observation(
        extended_spec=spec,
        candidate_set_sha256=result.extended_candidate_set_sha256,
        candidate_id=target.candidate_id,
        evidence_scope='synthetic_fixture',
        objective_id=target.objective_id,
        unit=target.unit,
        predicted_value=target.predicted_value,
        prediction_source_kind=target.prediction_source_kind,
        prediction_source_id=f'synthetic-second-root:{target.candidate_id}',
    )
    with pytest.raises(
        AdaptiveObservationConflictError,
        match='must supersede the current record SHA',
    ):
        adaptive_extended.save_observation(second_root)

    # The first record of a fresh key must not claim a predecessor.
    orphan = build_adaptive_extended_observation(
        extended_spec=spec,
        candidate_set_sha256=result.extended_candidate_set_sha256,
        candidate_id=target.candidate_id,
        evidence_scope='synthetic_fixture',
        objective_id='unwritten_objective',
        unit=target.unit,
        predicted_value=target.predicted_value,
        prediction_source_kind=target.prediction_source_kind,
        prediction_source_id=target.prediction_source_id,
        supersedes_observation_sha256='a' * 64,
    )
    with pytest.raises(
        AdaptiveObservationConflictError,
        match='must not supersede another record',
    ):
        adaptive_extended.save_observation(orphan)


def test_observation_history_backfills_predecessor_column(tmp_path):
    (
        _scene_repository,
        result,
        _search,
        validation_repository,
        extended,
        adaptive_extended,
    ) = _seeded(tmp_path)
    spec = extended.get_spec(result.extended_search_id)
    assert spec is not None
    target, replacement = _supersede_first_predicted(
        adaptive_extended,
        spec,
        result.extended_candidate_set_sha256,
    )

    # Simulate a database written before the predecessor column existed.
    with closing(
        sqlite3.connect(adaptive_extended.path)
    ) as connection, connection:
        connection.execute(
            'DROP INDEX idx_adaptive_extended_observation_supersedes'
        )
        connection.execute('DROP INDEX idx_adaptive_extended_observation_root')
        connection.execute(
            'ALTER TABLE cad_adaptive_extended_observations '
            'DROP COLUMN supersedes_observation_sha256'
        )

    reopened = CadAdaptiveExtendedRepository(extended, validation_repository)
    with closing(sqlite3.connect(adaptive_extended.path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            'SELECT supersedes_observation_sha256 '
            'FROM cad_adaptive_extended_observations '
            'WHERE observation_id=?',
            (replacement.observation_id,),
        ).fetchone()
        index_names = {
            entry['name']
            for entry in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
    assert row['supersedes_observation_sha256'] == target.observation_sha256
    assert 'idx_adaptive_extended_observation_supersedes' in index_names
    assert 'idx_adaptive_extended_observation_root' in index_names

    # The reopened chain reads exactly and still appends through the CAS.
    assert (
        _observation_head(
            reopened,
            result.extended_search_id,
            target,
        ).observation_sha256
        == replacement.observation_sha256
    )
    stale = _measured_successor(
        spec,
        result.extended_candidate_set_sha256,
        target,
        source_tag='post-migration-stale',
    )
    with pytest.raises(AdaptiveObservationConflictError):
        reopened.save_observation(stale)


def test_observation_history_reports_existing_fork(tmp_path):
    (
        _scene_repository,
        result,
        _search,
        validation_repository,
        extended,
        adaptive_extended,
    ) = _seeded(tmp_path)
    spec = extended.get_spec(result.extended_search_id)
    assert spec is not None
    target, _replacement = _supersede_first_predicted(
        adaptive_extended,
        spec,
        result.extended_candidate_set_sha256,
    )

    # Inject a second child of the same head directly, like a writer that
    # bypassed the CAS boundary on a pre-fix database.
    fork = _measured_successor(
        spec,
        result.extended_candidate_set_sha256,
        target,
        source_tag='fork',
    )
    with closing(
        sqlite3.connect(adaptive_extended.path)
    ) as connection, connection:
        connection.execute(
            'DROP INDEX idx_adaptive_extended_observation_supersedes'
        )
        connection.execute(
            """
            INSERT INTO cad_adaptive_extended_observations(
                observation_id, extended_search_id, candidate_id,
                objective_id, observation_sha256,
                supersedes_observation_sha256, payload_json, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fork.observation_id,
                fork.extended_search_id,
                fork.candidate_id,
                fork.objective_id,
                fork.observation_sha256,
                fork.supersedes_observation_sha256,
                fork.model_dump_json(),
                fork.created_at_utc,
            ),
        )

    # Reopen reports the persisted fork explicitly instead of picking a
    # winner by insertion order.
    with pytest.raises(ValueError, match='supersession fork'):
        CadAdaptiveExtendedRepository(extended, validation_repository)
    with pytest.raises(ValueError, match='chain is broken'):
        adaptive_extended.current_observations(result.extended_search_id)


def test_list_observations_rejects_column_payload_drift(tmp_path):
    (
        _scene_repository,
        result,
        _search,
        _validation,
        extended,
        adaptive_extended,
    ) = _seeded(tmp_path)
    spec = extended.get_spec(result.extended_search_id)
    assert spec is not None
    _target, replacement = _supersede_first_predicted(
        adaptive_extended,
        spec,
        result.extended_candidate_set_sha256,
    )

    with closing(
        sqlite3.connect(adaptive_extended.path)
    ) as connection, connection:
        connection.execute(
            'UPDATE cad_adaptive_extended_observations '
            'SET supersedes_observation_sha256=? WHERE observation_id=?',
            ('f' * 64, replacement.observation_id),
        )
    with pytest.raises(ValueError, match='disagrees with its payload'):
        adaptive_extended.list_observations(result.extended_search_id)
