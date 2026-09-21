from __future__ import annotations

import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from hashlib import sha256

from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_measurement_loop import (
    CadMeasurementPlan,
    _hash,
    bind_measurement_plan_prediction,
    build_measurement_plan,
    complete_measurement_plan,
)
from htdt.cad_measurement_models import CadFrequencyResponseDataset, CadMeasurementRecord
from htdt.cad_measurement_repository import (
    CadMeasurementRepository,
    MeasurementPlanConflictError,
)
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, RoomPrism, SceneDocument, SceneEntity, Size3
from htdt.cad_search import (
    apply_candidate_positions,
    build_cad_search_spec,
    candidate_preview_document,
    generate_cad_candidates,
)
from htdt.cad_search_models import CadSearchAxis
from htdt.cad_search_repository import CadSearchRepository
from htdt.cad_document import WorkingDocument


def test_measurement_plan_binds_candidate_to_exact_applied_revision(tmp_path):
    scene_repo = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id='o50-fixture', schema_version=2,
        room=RoomPrism(width_m=5, depth_m=4, height_m=2.4),
        entities=(SceneEntity(entity_id='fl', kind='speaker', name='FL',
            position=Position3(x_m=1,y_m=1,z_m=1), size_m=Size3(x_m=.2,y_m=.2,z_m=.4),
            speaker_role='FL'),
            SceneEntity(entity_id='mlp', kind='measurement_point', name='MLP',
                position=Position3(x_m=2.5,y_m=3,z_m=1)),),
    )
    source = scene_repo.save(document, parent_revision_id=None).revision
    spec,_ = build_cad_search_spec(source, CadConstraintSet(document_id='o50-fixture', constraints=()),
        (CadSearchAxis(entity_id='fl',axis='x',min_m=1,max_m=2,step_m=1),), candidate_limit=10)
    search_repo = CadSearchRepository(scene_repo); search_repo.save(spec)
    page = generate_cad_candidates(scene_repo, spec)
    candidate = page.candidates[1]
    working = WorkingDocument(source.document, source_revision_id=source.revision_id)
    constraint_set = CadConstraintSet(document_id='o50-fixture', constraints=())
    apply_candidate_positions(working, candidate, spec=spec, current_constraint_set=constraint_set)
    applied = scene_repo.save(working.committed_document, parent_revision_id=source.revision_id).revision

    plan = build_measurement_plan(scene_repo, search_repo, search_spec_id=spec.search_spec_id,
        candidate_id=candidate.candidate_id, applied_scene_revision_id=applied.revision_id)

    assert plan.candidate_id == candidate.candidate_id
    assert plan.applied_scene_revision_id == applied.revision_id
    assert plan.applied_scene_content_hash == applied.content_hash
    assert plan.candidate_set_sha256 == page.candidate_set_sha256
    assert plan.status == 'planned'

    measurement_repo = CadMeasurementRepository(scene_repo)
    tampered_plan = plan.model_copy(update={'plan_sha256': '0' * 64})
    with pytest.raises(ValueError, match='identity hash mismatch'):
        measurement_repo.save_measurement_plan(tampered_plan)
    measurement_repo.save_measurement_plan(plan)
    raw = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(80.0, 81.0, 79.0),
        phase_status='absent',
    )
    record = CadMeasurementRecord(
        measurement_id='measurement-a',
        document_id=applied.document_id,
        scene_revision_id=applied.revision_id,
        scene_content_hash=applied.content_hash,
        measurement_entity_id='mlp',
        measurement_position=applied.document.entity('mlp').position,
        evidence_type='measured',
        channel_role='FL',
        source_speaker_ids=('fl',),
        radiation_scope='single',
        routing_evidence='manual',
        imported_at='2026-09-18T00:00:00+00:00',
        source_kind='unknown',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id='dataset-a',
        measurement_id=record.measurement_id,
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(80.0, 81.0, 79.0),
        phase_deg=None,
        phase_status='absent',
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    measurement_repo.save(record, dataset, raw_filename='fixture.txt', raw_bytes=raw)
    completed = complete_measurement_plan(plan, measurement_repo, (record.measurement_id,))
    measurement_repo.save_measurement_plan(completed)

    assert completed.status == 'measured'
    assert completed.measurement_ids == ('measurement-a',)
    assert measurement_repo.latest_measurement_plans(spec.search_spec_id) == (completed,)


def test_measurement_plan_rejects_unknown_candidate_and_non_candidate_revision(tmp_path):
    scene_repo = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id='o50-reject', schema_version=2,
        room=RoomPrism(width_m=5, depth_m=4, height_m=2.4),
        entities=(SceneEntity(entity_id='fl', kind='speaker', name='FL',
            position=Position3(x_m=1,y_m=1,z_m=1), size_m=Size3(x_m=.2,y_m=.2,z_m=.4),
            speaker_role='FL'),
            SceneEntity(entity_id='mlp', kind='measurement_point', name='MLP',
                position=Position3(x_m=2.5,y_m=3,z_m=1)),),
    )
    source = scene_repo.save(document, parent_revision_id=None).revision
    constraints = CadConstraintSet(document_id='o50-reject', constraints=())
    spec,_ = build_cad_search_spec(source, constraints,
        (CadSearchAxis(entity_id='fl',axis='x',min_m=1,max_m=2,step_m=1),), candidate_limit=10)
    search_repo = CadSearchRepository(scene_repo)
    search_repo.save(spec)
    candidate = generate_cad_candidates(scene_repo, spec).candidates[1]

    wrong_document = source.document.model_copy(update={'entities': (
        source.document.entity('fl').model_copy(update={'position': Position3(x_m=2,y_m=1.25,z_m=1)}),
    )})
    wrong_revision = scene_repo.save(wrong_document, parent_revision_id=source.revision_id).revision

    with pytest.raises(ValueError, match='candidate does not belong'):
        build_measurement_plan(scene_repo, search_repo, search_spec_id=spec.search_spec_id,
            candidate_id='not-a-candidate', applied_scene_revision_id=wrong_revision.revision_id)
    with pytest.raises(ValueError, match='does not exactly match'):
        build_measurement_plan(scene_repo, search_repo, search_spec_id=spec.search_spec_id,
            candidate_id=candidate.candidate_id, applied_scene_revision_id=wrong_revision.revision_id)


def _lifecycle_fixture(tmp_path, document_id='o50-lifecycle'):
    scene_repo = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id=document_id, schema_version=2,
        room=RoomPrism(width_m=5, depth_m=4, height_m=2.4),
        entities=(SceneEntity(entity_id='fl', kind='speaker', name='FL',
            position=Position3(x_m=1,y_m=1,z_m=1), size_m=Size3(x_m=.2,y_m=.2,z_m=.4),
            speaker_role='FL'),
            SceneEntity(entity_id='mlp', kind='measurement_point', name='MLP',
                position=Position3(x_m=2.5,y_m=3,z_m=1)),),
    )
    source = scene_repo.save(document, parent_revision_id=None).revision
    spec,_ = build_cad_search_spec(source, CadConstraintSet(document_id=document_id, constraints=()),
        (CadSearchAxis(entity_id='fl',axis='x',min_m=1,max_m=2,step_m=1),), candidate_limit=10)
    search_repo = CadSearchRepository(scene_repo); search_repo.save(spec)
    return scene_repo, search_repo, spec, source


def _planned_plan(scene_repo, search_repo, spec, source):
    candidate = generate_cad_candidates(scene_repo, spec).candidates[1]
    applied = scene_repo.save(
        candidate_preview_document(source.document, candidate),
        parent_revision_id=source.revision_id,
    ).revision
    plan = build_measurement_plan(scene_repo, search_repo, search_spec_id=spec.search_spec_id,
        candidate_id=candidate.candidate_id, applied_scene_revision_id=applied.revision_id)
    return plan, applied


def _save_measured_evidence(measurement_repo, applied, measurement_id, dataset_id):
    raw = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(80.0, 81.0, 79.0),
        phase_status='absent',
        processing={'fixture_measurement_id': measurement_id},
    )
    record = CadMeasurementRecord(
        measurement_id=measurement_id,
        document_id=applied.document_id,
        scene_revision_id=applied.revision_id,
        scene_content_hash=applied.content_hash,
        measurement_entity_id='mlp',
        measurement_position=applied.document.entity('mlp').position,
        evidence_type='measured',
        channel_role='FL',
        source_speaker_ids=('fl',),
        radiation_scope='single',
        routing_evidence='manual',
        imported_at='2026-09-18T00:00:00+00:00',
        source_kind='unknown',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=dataset_id,
        measurement_id=record.measurement_id,
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(80.0, 81.0, 79.0),
        phase_deg=None,
        phase_status='absent',
        processing_json=canonical_json({'fixture_measurement_id': measurement_id}),
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    measurement_repo.save(record, dataset, raw_filename=f'{measurement_id}.txt', raw_bytes=raw)
    return record


def _mutate_plan(plan, **updates):
    """Return a hash-valid plan with identity fields overridden."""
    payload = {**plan.identity_payload(), **updates}
    return CadMeasurementPlan(plan_id=plan.plan_id, plan_sha256=_hash(payload), **payload)


def _o50_binding(plan, tag):
    # bind_measurement_plan_prediction only reads these binding attributes; a
    # lightweight stand-in avoids building a full R170A provider fixture.
    return SimpleNamespace(
        consumer_kind='O50_MEASUREMENT_PLAN',
        consumer_id=plan.plan_id,
        required_observables=('frequency_response_magnitude',),
        binding_id='r170a-provider-binding:' + sha256(tag.encode()).hexdigest(),
        semantic_sha256=sha256(f'semantic:{tag}'.encode()).hexdigest(),
    )


def _insert_legacy_row(repository, plan):
    """Persist a pre-supersedes row exactly like the pre-lifecycle writer did.

    Returns the legacy plan_sha256: the identity hash of the same semantic
    payload without the predecessor claim, which is what rows persisted before
    predecessor tracking carried.
    """
    payload = plan.model_dump(mode='json')
    payload.pop('supersedes_plan_sha256', None)
    identity = plan.identity_payload()
    identity.pop('supersedes_plan_sha256', None)
    payload['plan_sha256'] = _hash(identity)
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            '''INSERT INTO cad_measurement_plans(
                plan_id, document_id, search_spec_id, candidate_id,
                applied_scene_revision_id, status, plan_sha256, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (plan.plan_id, plan.document_id, plan.search_spec_id, plan.candidate_id,
             plan.applied_scene_revision_id, plan.status, payload['plan_sha256'],
             json.dumps(payload, sort_keys=True, separators=(',', ':'))),
        )
    return payload['plan_sha256']


def test_measurement_plan_rejects_measured_first_version(tmp_path):
    scene_repo, search_repo, spec, source = _lifecycle_fixture(tmp_path)
    plan, applied = _planned_plan(scene_repo, search_repo, spec, source)
    measurement_repo = CadMeasurementRepository(scene_repo)
    _save_measured_evidence(measurement_repo, applied, 'measurement-a', 'dataset-a')
    measured = complete_measurement_plan(plan, measurement_repo, ('measurement-a',))

    with pytest.raises(MeasurementPlanConflictError, match='must be planned'):
        measurement_repo.save_measurement_plan(measured)
    assert measurement_repo.latest_measurement_plans(spec.search_spec_id) == ()


def test_measurement_plan_rejects_second_completion_from_same_head(tmp_path):
    scene_repo, search_repo, spec, source = _lifecycle_fixture(tmp_path)
    plan, applied = _planned_plan(scene_repo, search_repo, spec, source)
    measurement_repo = CadMeasurementRepository(scene_repo)
    measurement_repo.save_measurement_plan(plan)
    _save_measured_evidence(measurement_repo, applied, 'measurement-a', 'dataset-a')
    _save_measured_evidence(measurement_repo, applied, 'measurement-b', 'dataset-b')

    first = complete_measurement_plan(plan, measurement_repo, ('measurement-a',))
    measurement_repo.save_measurement_plan(first)
    second = complete_measurement_plan(plan, measurement_repo, ('measurement-b',))
    with pytest.raises(MeasurementPlanConflictError, match='terminal'):
        measurement_repo.save_measurement_plan(second)

    assert measurement_repo.latest_measurement_plans(spec.search_spec_id) == (first,)
    assert measurement_repo.list_measurement_plans(spec.search_spec_id) == (plan, first)


def test_measurement_plan_binding_update_extends_head_and_stale_parent_rejected(tmp_path):
    scene_repo, search_repo, spec, source = _lifecycle_fixture(tmp_path)
    plan, applied = _planned_plan(scene_repo, search_repo, spec, source)
    measurement_repo = CadMeasurementRepository(scene_repo)
    measurement_repo.save_measurement_plan(plan)

    bound = bind_measurement_plan_prediction(plan, _o50_binding(plan, 'binding-a'))
    assert bound.supersedes_plan_sha256 == plan.plan_sha256
    measurement_repo.save_measurement_plan(bound)

    # A binding update or completion derived from the stale pre-binding head
    # can no longer advance the lifecycle.
    stale_binding = bind_measurement_plan_prediction(plan, _o50_binding(plan, 'binding-b'))
    with pytest.raises(MeasurementPlanConflictError, match='persisted head'):
        measurement_repo.save_measurement_plan(stale_binding)
    _save_measured_evidence(measurement_repo, applied, 'measurement-a', 'dataset-a')
    stale_completion = complete_measurement_plan(plan, measurement_repo, ('measurement-a',))
    with pytest.raises(MeasurementPlanConflictError, match='persisted head'):
        measurement_repo.save_measurement_plan(stale_completion)

    measured = complete_measurement_plan(bound, measurement_repo, ('measurement-a',))
    measurement_repo.save_measurement_plan(measured)
    assert measurement_repo.latest_measurement_plans(spec.search_spec_id) == (measured,)
    assert measurement_repo.list_measurement_plans(spec.search_spec_id) == (plan, bound, measured)


def test_measurement_plan_rejects_unclaimed_and_unpersisted_predecessors(tmp_path):
    scene_repo, search_repo, spec, source = _lifecycle_fixture(tmp_path)
    plan, _ = _planned_plan(scene_repo, search_repo, spec, source)
    measurement_repo = CadMeasurementRepository(scene_repo)

    # A first-ever version may not claim a predecessor outside the history.
    orphaned = _mutate_plan(plan, supersedes_plan_sha256='d' * 64)
    with pytest.raises(MeasurementPlanConflictError, match='never persisted'):
        measurement_repo.save_measurement_plan(orphaned)

    measurement_repo.save_measurement_plan(plan)
    # Once a head exists a new version must claim it exactly.
    unclaimed = _mutate_plan(
        plan,
        prediction_provider_binding_id='r170a-provider-binding:' + 'e' * 64,
        prediction_provider_binding_sha256='f' * 64,
    )
    with pytest.raises(MeasurementPlanConflictError, match='supersedes_plan_sha256'):
        measurement_repo.save_measurement_plan(unclaimed)


def test_measurement_plan_concurrent_completions_single_winner(tmp_path):
    scene_repo, search_repo, spec, source = _lifecycle_fixture(tmp_path)
    plan, applied = _planned_plan(scene_repo, search_repo, spec, source)
    measurement_repo = CadMeasurementRepository(scene_repo)
    measurement_repo.save_measurement_plan(plan)
    _save_measured_evidence(measurement_repo, applied, 'measurement-a', 'dataset-a')
    _save_measured_evidence(measurement_repo, applied, 'measurement-b', 'dataset-b')
    completion_a = complete_measurement_plan(plan, measurement_repo, ('measurement-a',))
    completion_b = complete_measurement_plan(plan, measurement_repo, ('measurement-b',))

    barrier = threading.Barrier(2)
    outcomes = {}

    def attempt(key, candidate):
        barrier.wait(timeout=10)
        try:
            measurement_repo.save_measurement_plan(candidate)
            outcomes[key] = 'saved'
        except MeasurementPlanConflictError:
            outcomes[key] = 'conflict'

    threads = (
        threading.Thread(target=attempt, args=('a', completion_a)),
        threading.Thread(target=attempt, args=('b', completion_b)),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes.values()) == ['conflict', 'saved']
    winner = completion_a if outcomes['a'] == 'saved' else completion_b
    assert measurement_repo.latest_measurement_plans(spec.search_spec_id) == (winner,)


def test_measurement_plan_revalidates_upstream_authority_at_save(tmp_path):
    scene_repo, search_repo, spec, source = _lifecycle_fixture(tmp_path)
    plan, _ = _planned_plan(scene_repo, search_repo, spec, source)
    measurement_repo = CadMeasurementRepository(scene_repo)

    ghost = _mutate_plan(plan, search_spec_id='ghost-spec')
    with pytest.raises(ValueError, match='SearchSpec does not exist'):
        measurement_repo.save_measurement_plan(ghost)

    wrong_spec_sha = _mutate_plan(plan, search_spec_sha256='0' * 64)
    with pytest.raises(ValueError, match='SearchSpec authority mismatch'):
        measurement_repo.save_measurement_plan(wrong_spec_sha)

    fabricated = _mutate_plan(plan, candidate_id='not-a-candidate')
    with pytest.raises(ValueError, match='candidate does not belong'):
        measurement_repo.save_measurement_plan(fabricated)

    wrong_set = _mutate_plan(plan, candidate_set_sha256='c' * 64)
    with pytest.raises(ValueError, match='candidate-set hash mismatch'):
        measurement_repo.save_measurement_plan(wrong_set)

    measurement_repo.save_measurement_plan(plan)
    assert measurement_repo.latest_measurement_plans(spec.search_spec_id) == (plan,)


def test_measurement_plan_legacy_histories_migrate_and_forks_surface(tmp_path):
    scene_repo, search_repo, spec, source = _lifecycle_fixture(tmp_path)
    plan, applied = _planned_plan(scene_repo, search_repo, spec, source)
    measurement_repo = CadMeasurementRepository(scene_repo)
    _save_measured_evidence(measurement_repo, applied, 'measurement-a', 'dataset-a')
    _save_measured_evidence(measurement_repo, applied, 'measurement-b', 'dataset-b')
    measured = complete_measurement_plan(plan, measurement_repo, ('measurement-a',))

    # Rows persisted before predecessor tracking carry no supersedes claim;
    # a linear legacy chain keeps its semantic identity and reads unchanged.
    _insert_legacy_row(measurement_repo, plan)
    legacy_measured_sha = _insert_legacy_row(measurement_repo, measured)
    history = measurement_repo.list_measurement_plans(spec.search_spec_id)
    assert history[0] == plan
    expected_legacy_measured = CadMeasurementPlan(
        **measured.model_dump(exclude={'supersedes_plan_sha256', 'plan_sha256'}),
        plan_sha256=legacy_measured_sha,
    )
    assert history[1] == expected_legacy_measured
    latest = measurement_repo.latest_measurement_plans(spec.search_spec_id)
    assert latest == (expected_legacy_measured,)
    assert latest[0].supersedes_plan_sha256 is None

    # A second legacy measured row behind the terminal head is a detectable
    # fork and is surfaced rather than silently winning by insertion order.
    forked = complete_measurement_plan(plan, measurement_repo, ('measurement-b',))
    _insert_legacy_row(measurement_repo, forked)
    with pytest.raises(ValueError, match='not a single chain'):
        measurement_repo.list_measurement_plans(spec.search_spec_id)
    with pytest.raises(ValueError, match='not a single chain'):
        measurement_repo.latest_measurement_plans(spec.search_spec_id)
