from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from htdt.main import create_app
from htdt.placement_constraints import (
    ConstraintSetCreate,
    PlacementEvaluationRequest,
    evaluate_constraint_set,
    evaluate_prepared_constraint_set,
    prepare_constraint_context,
    prepare_constraint_evaluation,
    prepare_constraint_spec,
    validate_constraint_set_for_context,
)


def context_payload() -> dict:
    vertices = [
        {'vertex_id': 'v0', 'x_m': 0.0, 'y_m': 0.0},
        {'vertex_id': 'v1', 'x_m': 4.0, 'y_m': 0.0},
        {'vertex_id': 'v2', 'x_m': 4.0, 'y_m': 5.0},
        {'vertex_id': 'v3', 'x_m': 2.5, 'y_m': 5.0},
        {'vertex_id': 'v4', 'x_m': 2.5, 'y_m': 4.0},
        {'vertex_id': 'v5', 'x_m': 1.5, 'y_m': 4.0},
        {'vertex_id': 'v6', 'x_m': 1.5, 'y_m': 5.0},
        {'vertex_id': 'v7', 'x_m': 0.0, 'y_m': 5.0},
    ]
    return {
        'room': {
            'width_m': 4.0, 'depth_m': 5.0, 'height_m': 2.4,
            'geometry_kind': 'polygon_prism', 'footprint_vertices': vertices,
        },
        'speakers': [
            {'speaker_id': 'FL', 'role': 'front_left', 'position': {'x_m': 1.0, 'y_m': 1.0, 'z_m': 1.0}},
            {'speaker_id': 'FR', 'role': 'front_right', 'position': {'x_m': 3.0, 'y_m': 1.0, 'z_m': 1.0}},
            {'speaker_id': 'C', 'role': 'front_center', 'position': {'x_m': 2.0, 'y_m': 0.8, 'z_m': 1.0}},
        ],
        'measurement_point': {
            'point_id': 'MLP', 'label': 'MLP',
            'position': {'x_m': 2.0, 'y_m': 3.5, 'z_m': 1.0},
        },
        'avr': {'manufacturer': 'Yamaha', 'model': 'RX-A4A'},
    }


def constraint_request() -> ConstraintSetCreate:
    front_region = {
        'vertices': [
            {'x_m': 0.3, 'y_m': 0.3}, {'x_m': 3.7, 'y_m': 0.3},
            {'x_m': 3.7, 'y_m': 2.0}, {'x_m': 0.3, 'y_m': 2.0},
        ]
    }
    rack_region = {
        'vertices': [
            {'x_m': 1.8, 'y_m': 0.6}, {'x_m': 2.2, 'y_m': 0.6},
            {'x_m': 2.2, 'y_m': 1.4}, {'x_m': 1.8, 'y_m': 1.4},
        ]
    }
    return ConstraintSetCreate.model_validate({
        'context_id': 'context-placeholder',
        'name': 'Front speakers physical limits',
        'entity_profiles': [
            {'entity_id': 'FL', 'footprint_radius_m': 0.15, 'safety_margin_m': 0.05},
            {'entity_id': 'FR', 'footprint_radius_m': 0.15, 'safety_margin_m': 0.05},
        ],
        'constraints': [
            {'constraint_id': 'front-allowed', 'kind': 'allowed_region', 'entity_ids': ['FL', 'FR'], 'region': front_region},
            {'constraint_id': 'rack-exclusion', 'kind': 'exclusion_region', 'entity_ids': ['FL', 'FR'], 'region': rack_region},
            {'constraint_id': 'front-wall-clearance', 'kind': 'wall_clearance', 'entity_ids': ['FL', 'FR'],
             'edge_id': 'v0->v1', 'min_m': 0.3, 'max_m': 1.5},
            {'constraint_id': 'speaker-height', 'kind': 'axis_range', 'entity_id': 'FL', 'axis': 'z', 'fixed_m': 1.0},
            {'constraint_id': 'fl-move', 'kind': 'movement_budget', 'entity_id': 'FL', 'max_distance_m': 0.3},
            {'constraint_id': 'lr-spacing', 'kind': 'pair_distance', 'entity_a': 'FL', 'entity_b': 'FR',
             'min_m': 2.0, 'max_m': 2.6, 'distance_mode': 'horizontal_xy'},
            {'constraint_id': 'lr-mirror', 'kind': 'linked_placement', 'entity_a': 'FL', 'entity_b': 'FR',
             'relation': 'mirror_x', 'tolerance_m': 1e-6},
            {'constraint_id': 'lr-y', 'kind': 'linked_placement', 'entity_a': 'FL', 'entity_b': 'FR',
             'relation': 'equal_y', 'tolerance_m': 1e-6},
            {'constraint_id': 'lr-delta-y', 'kind': 'linked_placement', 'entity_a': 'FL', 'entity_b': 'FR',
             'relation': 'equal_delta_y', 'tolerance_m': 1e-6},
        ],
    })


def stored_spec() -> dict:
    return validate_constraint_set_for_context(constraint_request(), context_payload())


def test_feasible_candidate_satisfies_all_hard_constraints() -> None:
    result = evaluate_constraint_set(
        context_payload(), stored_spec(),
        PlacementEvaluationRequest.model_validate({'positions': {
            'FL': {'x_m': 0.9, 'y_m': 1.1, 'z_m': 1.0},
            'FR': {'x_m': 3.1, 'y_m': 1.1, 'z_m': 1.0},
        }}),
    )
    assert result['classification'] == 'placement_constraint_evaluation'
    assert result['engine_version'] == 'placement-constraints-1'
    assert result['feasible'] is True
    assert result['rejections'] == []
    assert result['overridden_entity_ids'] == ['FL', 'FR']
    clearance = next(item for item in result['observations'] if item['constraint_id'] == 'front-wall-clearance')
    assert clearance['actual']['clearance_m'] == pytest.approx(0.9)


def test_hard_constraint_rejections_report_ids_and_actual_values() -> None:
    result = evaluate_constraint_set(
        context_payload(), stored_spec(),
        PlacementEvaluationRequest.model_validate({'positions': {
            'FL': {'x_m': 1.95, 'y_m': 1.0, 'z_m': 1.2},
            'FR': {'x_m': 3.0, 'y_m': 1.0, 'z_m': 1.0},
        }}),
    )
    assert result['feasible'] is False
    rejected = {item['constraint_id']: item for item in result['rejections']}
    assert 'rack-exclusion' in rejected
    assert 'speaker-height' in rejected
    assert 'fl-move' in rejected
    assert 'lr-spacing' in rejected
    assert 'lr-mirror' in rejected
    assert rejected['speaker-height']['details']['value_m'] == pytest.approx(1.2)


def test_cabinet_envelope_cannot_cross_room_boundary() -> None:
    result = evaluate_constraint_set(
        context_payload(), stored_spec(),
        PlacementEvaluationRequest.model_validate({'positions': {
            'FL': {'x_m': 0.1, 'y_m': 1.0, 'z_m': 1.0},
            'FR': {'x_m': 3.9, 'y_m': 1.0, 'z_m': 1.0},
        }}),
    )
    assert result['feasible'] is False
    ids = {item['constraint_id'] for item in result['rejections']}
    assert '__room_boundary__:FL' in ids
    assert '__room_boundary__:FR' in ids


def test_constraint_regions_and_wall_ids_are_validated_against_exact_room() -> None:
    request = constraint_request()
    request.constraints[0].region.vertices[0].x_m = -0.1  # type: ignore[union-attr]
    with pytest.raises(ValueError, match='fully inside'):
        validate_constraint_set_for_context(request, context_payload())

    request = constraint_request()
    request.constraints[2].edge_id = 'unknown->wall'  # type: ignore[union-attr]
    with pytest.raises(ValueError, match='unknown wall edge'):
        validate_constraint_set_for_context(request, context_payload())


def test_reference_box_is_not_treated_as_exact_geometry() -> None:
    context = context_payload()
    context['room'] = {'width_m': 4.0, 'depth_m': 5.0, 'height_m': 2.4, 'geometry_kind': 'reference_box'}
    with pytest.raises(ValueError, match='exact room footprint'):
        validate_constraint_set_for_context(constraint_request(), context)


def test_unknown_candidate_entity_is_rejected_not_ignored() -> None:
    with pytest.raises(ValueError, match='Unknown candidate entity_id'):
        evaluate_constraint_set(
            context_payload(), stored_spec(),
            PlacementEvaluationRequest.model_validate({'positions': {'UNKNOWN': {'x_m': 1, 'y_m': 1, 'z_m': 1}}}),
        )


def test_prepared_evaluation_is_identical_to_single_shot_per_candidate() -> None:
    """Prepared context+spec evaluation must equal evaluate_constraint_set.

    REV54 hoisted room geometry/spec validation out of the candidate sweep;
    the batch path must remain bit-identical to the single-shot API.
    """
    context = context_payload()
    spec = stored_spec()
    prepared = prepare_constraint_evaluation(context, spec)
    candidate_positions = [
        {
            'FL': {'x_m': x, 'y_m': y, 'z_m': 1.0},
            'FR': {'x_m': 4.0 - x, 'y_m': y, 'z_m': 1.0},
        }
        for x in (0.5, 0.9, 1.5, 2.0)
        for y in (0.4, 1.0, 1.6, 2.2)
    ]
    for positions in candidate_positions:
        request = PlacementEvaluationRequest.model_validate(
            {'positions': positions}
        )
        assert (
            evaluate_prepared_constraint_set(prepared, request)
            == evaluate_constraint_set(context, spec, request)
        )


def test_prepare_constraint_spec_shares_context_across_specs() -> None:
    """The two-level prepare API lets sweeps vary the spec per candidate
    (topology placement re-derives profiles) while reusing room geometry."""
    context = context_payload()
    spec = stored_spec()
    prepared_context = prepare_constraint_context(context)
    prepared = prepare_constraint_spec(prepared_context, spec)
    request = PlacementEvaluationRequest.model_validate({'positions': {
        'FL': {'x_m': 0.9, 'y_m': 1.1, 'z_m': 1.0},
        'FR': {'x_m': 3.1, 'y_m': 1.1, 'z_m': 1.0},
    }})
    assert (
        evaluate_prepared_constraint_set(prepared, request)
        == evaluate_constraint_set(context, spec, request)
    )
    # A spec whose geometry version does not match the prepared context still
    # fails closed at prepare time.
    stale_spec = dict(spec)
    stale_spec['geometry_version'] = 'room-geometry-stale'
    with pytest.raises(ValueError, match='geometry version'):
        prepare_constraint_spec(prepared_context, stale_spec)


def test_constraint_set_api_persists_immutable_spec_and_evaluates(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project = client.post('/api/projects', json={'name': 'Constrained polygon room'}).json()
    context_response = client.post(f"/api/projects/{project['id']}/contexts", json=context_payload())
    assert context_response.status_code == 201, context_response.text
    context = context_response.json()

    request = constraint_request().model_dump(mode='json')
    request['context_id'] = context['id']
    created = client.post(f"/api/projects/{project['id']}/constraint-sets", json=request)
    assert created.status_code == 201, created.text
    record = created.json()
    assert record['context_id'] == context['id']
    assert record['spec']['engine_version'] == 'placement-constraints-1'

    listed = client.get(f"/api/projects/{project['id']}/constraint-sets?context_id={context['id']}")
    assert listed.status_code == 200
    assert [item['id'] for item in listed.json()] == [record['id']]

    evaluation = client.post(
        f"/api/projects/{project['id']}/constraint-sets/{record['id']}/evaluate",
        json={'positions': {'FL': {'x_m': 0.9, 'y_m': 1.1, 'z_m': 1.0}, 'FR': {'x_m': 3.1, 'y_m': 1.1, 'z_m': 1.0}}},
    )
    assert evaluation.status_code == 200, evaluation.text
    payload = evaluation.json()
    assert payload['feasible'] is True
    assert payload['constraint_set_id'] == record['id']
    assert payload['context_id'] == context['id']

    fetched = client.get(f"/api/projects/{project['id']}/constraint-sets/{record['id']}")
    assert fetched.status_code == 200
    assert fetched.json()['spec'] == record['spec']

    # The retired browser backup/restore endpoints are covered by
    # test_browser_backup_retired.py; here a same-data-dir restart proves the
    # persisted immutable spec still evaluates identically.
    with TestClient(create_app(tmp_path)) as restarted:
        restarted_sets = restarted.get(f"/api/projects/{project['id']}/constraint-sets").json()
        restarted_record = next(item for item in restarted_sets if item['id'] == record['id'])
        assert restarted_record['spec'] == record['spec']
        restarted_evaluation = restarted.post(
            f"/api/projects/{project['id']}/constraint-sets/{record['id']}/evaluate",
            json={'positions': {'FL': {'x_m': 0.9, 'y_m': 1.1, 'z_m': 1.0}, 'FR': {'x_m': 3.1, 'y_m': 1.1, 'z_m': 1.0}}},
        )
        assert restarted_evaluation.status_code == 200
        assert restarted_evaluation.json()['feasible'] is True


def test_pair_distance_can_use_envelope_clearance() -> None:
    request = constraint_request()
    pair = next(item for item in request.constraints if item.constraint_id == 'lr-spacing')
    pair.distance_reference = 'envelope_clearance'  # type: ignore[union-attr]
    pair.min_m = 1.7  # type: ignore[union-attr]
    pair.max_m = 2.0  # type: ignore[union-attr]
    spec = validate_constraint_set_for_context(request, context_payload())
    result = evaluate_constraint_set(
        context_payload(), spec,
        PlacementEvaluationRequest.model_validate({'positions': {
            'FL': {'x_m': 0.9, 'y_m': 1.1, 'z_m': 1.0},
            'FR': {'x_m': 3.1, 'y_m': 1.1, 'z_m': 1.0},
        }}),
    )
    observation = next(item for item in result['observations'] if item['constraint_id'] == 'lr-spacing')
    assert observation['actual']['center_distance_m'] == pytest.approx(2.2)
    assert observation['actual']['distance_m'] == pytest.approx(1.8)
    assert result['feasible'] is True


def test_mirror_relation_supports_custom_axis_for_non_rectangular_room() -> None:
    request = constraint_request()
    mirror = next(item for item in request.constraints if item.constraint_id == 'lr-mirror')
    mirror.mirror_axis_x_m = 1.8  # type: ignore[union-attr]
    spec = validate_constraint_set_for_context(request, context_payload())
    result = evaluate_constraint_set(
        context_payload(), spec,
        PlacementEvaluationRequest.model_validate({'positions': {
            'FL': {'x_m': 0.7, 'y_m': 1.1, 'z_m': 1.0},
            'FR': {'x_m': 2.9, 'y_m': 1.1, 'z_m': 1.0},
        }}),
    )
    mirror_observation = next(item for item in result['observations'] if item['constraint_id'] == 'lr-mirror')
    assert mirror_observation['actual']['target_m'] == pytest.approx(1.8)
    assert mirror_observation['passed'] is True


def test_allowed_region_supports_disconnected_multipolygon_union() -> None:
    payload = constraint_request().model_dump(mode='json')
    allowed = next(item for item in payload['constraints'] if item['constraint_id'] == 'front-allowed')
    allowed['region'] = {'polygons': [
        [
            {'x_m': 0.3, 'y_m': 0.3}, {'x_m': 1.4, 'y_m': 0.3},
            {'x_m': 1.4, 'y_m': 2.0}, {'x_m': 0.3, 'y_m': 2.0},
        ],
        [
            {'x_m': 2.6, 'y_m': 0.3}, {'x_m': 3.7, 'y_m': 0.3},
            {'x_m': 3.7, 'y_m': 2.0}, {'x_m': 2.6, 'y_m': 2.0},
        ],
    ]}
    request = ConstraintSetCreate.model_validate(payload)
    spec = validate_constraint_set_for_context(request, context_payload())
    result = evaluate_constraint_set(
        context_payload(), spec,
        PlacementEvaluationRequest.model_validate({'positions': {
            'FL': {'x_m': 0.9, 'y_m': 1.1, 'z_m': 1.0},
            'FR': {'x_m': 3.1, 'y_m': 1.1, 'z_m': 1.0},
        }}),
    )
    assert result['feasible'] is True


def test_invalid_constraint_numeric_and_distance_semantics_are_rejected() -> None:
    payload = constraint_request().model_dump(mode='python')
    payload['entity_profiles'][0]['safety_margin_m'] = float('inf')
    with pytest.raises(ValueError, match='non-finite'):
        ConstraintSetCreate.model_validate(payload)

    payload = constraint_request().model_dump(mode='python')
    pair = next(item for item in payload['constraints'] if item['constraint_id'] == 'lr-spacing')
    pair['distance_reference'] = 'envelope_clearance'
    pair['distance_mode'] = '3d'
    with pytest.raises(ValueError, match='horizontal_xy'):
        ConstraintSetCreate.model_validate(payload)


def test_custom_mirror_axis_must_remain_inside_reference_width() -> None:
    request = constraint_request()
    mirror = next(item for item in request.constraints if item.constraint_id == 'lr-mirror')
    mirror.mirror_axis_x_m = 5.0  # type: ignore[union-attr]
    with pytest.raises(ValueError, match='mirror axis'):
        validate_constraint_set_for_context(request, context_payload())


def test_constraint_set_spec_hash_is_stable_and_returned_by_evaluation(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project = client.post('/api/projects', json={'name': 'Hash'}).json()
    context = client.post(f"/api/projects/{project['id']}/contexts", json=context_payload()).json()
    request = constraint_request().model_dump(mode='json')
    request['context_id'] = context['id']
    created = client.post(f"/api/projects/{project['id']}/constraint-sets", json=request).json()
    assert len(created['spec_sha256']) == 64
    fetched = client.get(f"/api/projects/{project['id']}/constraint-sets/{created['id']}").json()
    assert fetched['spec_sha256'] == created['spec_sha256']
    evaluation = client.post(
        f"/api/projects/{project['id']}/constraint-sets/{created['id']}/evaluate",
        json={'positions': {}},
    ).json()
    assert evaluation['constraint_set_spec_sha256'] == created['spec_sha256']


def test_context_rejects_duplicate_entity_ids_before_constraints(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project = client.post('/api/projects', json={'name': 'Entity IDs'}).json()
    duplicate_speaker = context_payload()
    duplicate_speaker['speakers'][1]['speaker_id'] = 'FL'
    response = client.post(f"/api/projects/{project['id']}/contexts", json=duplicate_speaker)
    assert response.status_code == 422
    assert 'speaker_id values must be unique' in response.text

    duplicate_mlp = context_payload()
    duplicate_mlp['measurement_point']['point_id'] = 'FL'
    response = client.post(f"/api/projects/{project['id']}/contexts", json=duplicate_mlp)
    assert response.status_code == 422
    assert 'point_id must not duplicate a speaker_id' in response.text


def test_corrupted_constraint_set_is_reported_and_not_evaluated(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    client = TestClient(app)
    project = client.post('/api/projects', json={'name': 'Integrity'}).json()
    context = client.post(f"/api/projects/{project['id']}/contexts", json=context_payload()).json()
    request = constraint_request().model_dump(mode='json')
    request['context_id'] = context['id']
    record = client.post(f"/api/projects/{project['id']}/constraint-sets", json=request).json()
    with app.state.store.connect() as db:
        db.execute("UPDATE constraint_sets SET spec_json = ? WHERE id = ?", ('{"corrupted":true}', record['id']))
        db.commit()
    integrity = client.get('/api/integrity').json()
    assert f"constraint_set_hash_mismatch:{record['id']}" in integrity['problems']
    evaluation = client.post(
        f"/api/projects/{project['id']}/constraint-sets/{record['id']}/evaluate",
        json={'positions': {}},
    )
    assert evaluation.status_code == 409
    assert evaluation.json()['detail'] == 'ConstraintSet integrity check failed'
