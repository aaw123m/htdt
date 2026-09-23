from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient

from htdt.geometry import polygon_from_vertices, summarize_polygon
from htdt.main import create_app
from htdt.models import ContextCreate


EIGHT_VERTEX_ROOM = [
    {'vertex_id': 'v0', 'x_m': 0.0, 'y_m': 0.0},
    {'vertex_id': 'v1', 'x_m': 4.0, 'y_m': 0.0},
    {'vertex_id': 'v2', 'x_m': 4.0, 'y_m': 5.0},
    {'vertex_id': 'v3', 'x_m': 2.5, 'y_m': 5.0},
    {'vertex_id': 'v4', 'x_m': 2.5, 'y_m': 4.0},
    {'vertex_id': 'v5', 'x_m': 1.5, 'y_m': 4.0},
    {'vertex_id': 'v6', 'x_m': 1.5, 'y_m': 5.0},
    {'vertex_id': 'v7', 'x_m': 0.0, 'y_m': 5.0},
]


def context_payload(*, mlp_x: float = 2.0, mlp_y: float = 3.5) -> dict:
    return {
        'room': {
            'width_m': 4.0, 'depth_m': 5.0, 'height_m': 2.4,
            'geometry_kind': 'polygon_prism', 'footprint_vertices': deepcopy(EIGHT_VERTEX_ROOM),
        },
        'speakers': [
            {'speaker_id': 'FL', 'role': 'front_left', 'position': {'x_m': 1.0, 'y_m': 1.0, 'z_m': 1.0}},
            {'speaker_id': 'FR', 'role': 'front_right', 'position': {'x_m': 3.0, 'y_m': 1.0, 'z_m': 1.0}},
        ],
        'measurement_point': {
            'point_id': 'MLP', 'label': 'MLP',
            'position': {'x_m': mlp_x, 'y_m': mlp_y, 'z_m': 1.0},
        },
        'avr': {'manufacturer': 'Yamaha', 'model': 'RX-A4A'},
    }


def test_eight_vertex_concave_polygon_summary() -> None:
    polygon = polygon_from_vertices([(item['x_m'], item['y_m']) for item in EIGHT_VERTEX_ROOM])
    summary = summarize_polygon(polygon)
    assert summary.vertex_count == 8
    assert summary.area_m2 == 19.0
    assert summary.bounds_m == (0.0, 0.0, 4.0, 5.0)
    assert summary.convex is False


def test_polygon_context_round_trips_and_exposes_wall_ids(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project = client.post('/api/projects', json={'name': '8-corner room'}).json()
    created = client.post(f"/api/projects/{project['id']}/contexts", json=context_payload())
    assert created.status_code == 201, created.text
    saved = created.json()
    assert saved['payload']['room']['geometry_kind'] == 'polygon_prism'
    assert [item['vertex_id'] for item in saved['payload']['room']['footprint_vertices']] == [f'v{i}' for i in range(8)]

    geometry = client.get(f"/api/projects/{project['id']}/contexts/{saved['id']}/geometry")
    assert geometry.status_code == 200, geometry.text
    payload = geometry.json()
    assert payload['geometry_version'] == 'room-geometry-2'
    assert payload['exact_footprint_available'] is True
    assert payload['polygon_summary']['area_m2'] == 19.0
    assert payload['polygon_summary']['convex'] is False
    assert payload['wall_edges'][0]['edge_id'] == 'v0->v1'
    assert payload['wall_edges'][-1]['edge_id'] == 'v7->v0'


def test_reference_box_inside_but_polygon_outside_position_is_rejected() -> None:
    try:
        ContextCreate.model_validate(context_payload(mlp_x=2.0, mlp_y=4.5))
    except ValueError as exc:
        assert 'outside the room polygon' in str(exc)
    else:
        raise AssertionError('Point in the rear notch must be rejected')


def test_polygon_boundary_position_is_accepted() -> None:
    context = ContextCreate.model_validate(context_payload(mlp_x=2.5, mlp_y=4.5))
    assert context.measurement_point.position.x_m == 2.5


def test_invalid_polygon_and_duplicate_vertex_ids_are_rejected() -> None:
    bow_tie = context_payload()
    bow_tie['room']['footprint_vertices'] = [
        {'vertex_id': 'a', 'x_m': 0.0, 'y_m': 0.0},
        {'vertex_id': 'b', 'x_m': 4.0, 'y_m': 5.0},
        {'vertex_id': 'c', 'x_m': 0.0, 'y_m': 5.0},
        {'vertex_id': 'd', 'x_m': 4.0, 'y_m': 0.0},
    ]
    try:
        ContextCreate.model_validate(bow_tie)
    except ValueError as exc:
        assert 'Invalid room polygon' in str(exc)
    else:
        raise AssertionError('Self-intersecting polygon must be rejected')

    duplicate_id = context_payload()
    duplicate_id['room']['footprint_vertices'][7] = {
        **duplicate_id['room']['footprint_vertices'][7], 'vertex_id': 'v6',
    }
    try:
        ContextCreate.model_validate(duplicate_id)
    except ValueError as exc:
        assert 'vertex_id values must be unique' in str(exc)
    else:
        raise AssertionError('Duplicate vertex IDs must be rejected')


def test_rectangular_acoustics_endpoint_rejects_polygon_room(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project = client.post('/api/projects', json={'name': 'Polygon'}).json()
    context = client.post(f"/api/projects/{project['id']}/contexts", json=context_payload())
    assert context.status_code == 201, context.text
    response = client.get(f"/api/projects/{project['id']}/contexts/{context.json()['id']}/acoustics")
    assert response.status_code == 422
    assert 'requires a rectangular room' in response.json()['detail']
