from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from htdt.main import create_app
from htdt.placement_constraints import ConstraintSetCreate, validate_constraint_set_for_context
from htdt.search_space import (
    GridAxis,
    SearchSpecCreate,
    generate_search_space,
    grid_values,
    validate_search_spec,
)


def context_payload() -> dict:
    return {
        'room': {'width_m': 4.0, 'depth_m': 4.0, 'height_m': 2.4, 'geometry_kind': 'rectangular'},
        'speakers': [
            {'speaker_id': 'FL', 'role': 'front_left', 'position': {'x_m': 1.0, 'y_m': 1.0, 'z_m': 1.0}},
            {'speaker_id': 'FR', 'role': 'front_right', 'position': {'x_m': 3.0, 'y_m': 1.0, 'z_m': 1.0}},
        ],
        'measurement_point': {'point_id': 'MLP', 'label': 'MLP', 'position': {'x_m': 2.0, 'y_m': 3.0, 'z_m': 1.0}},
        'avr': {'manufacturer': 'Yamaha', 'model': 'RX-A4A'},
    }


def constraint_request(context_id: str = 'context-placeholder') -> ConstraintSetCreate:
    return ConstraintSetCreate.model_validate({
        'context_id': context_id,
        'name': 'Search fixture limits',
        'constraints': [
            {
                'constraint_id': 'front-allowed', 'kind': 'allowed_region', 'entity_ids': ['FL', 'FR'],
                'region': {'vertices': [
                    {'x_m': 0.5, 'y_m': 0.5}, {'x_m': 3.5, 'y_m': 0.5},
                    {'x_m': 3.5, 'y_m': 2.0}, {'x_m': 0.5, 'y_m': 2.0},
                ]},
            },
            {
                'constraint_id': 'rack', 'kind': 'exclusion_region', 'entity_ids': ['FL'],
                'region': {'vertices': [
                    {'x_m': 0.95, 'y_m': 0.95}, {'x_m': 1.05, 'y_m': 0.95},
                    {'x_m': 1.05, 'y_m': 1.05}, {'x_m': 0.95, 'y_m': 1.05},
                ]},
            },
            {'constraint_id': 'lr-spacing', 'kind': 'pair_distance', 'entity_a': 'FL', 'entity_b': 'FR',
             'min_m': 1.5, 'distance_mode': 'horizontal_xy'},
            {'constraint_id': 'lr-mirror', 'kind': 'linked_placement', 'entity_a': 'FL', 'entity_b': 'FR',
             'relation': 'mirror_x', 'tolerance_m': 1e-9},
            {'constraint_id': 'lr-y', 'kind': 'linked_placement', 'entity_a': 'FL', 'entity_b': 'FR',
             'relation': 'equal_y', 'tolerance_m': 1e-9},
        ],
    })


def search_request(constraint_set_id: str = 'constraint-placeholder') -> SearchSpecCreate:
    return SearchSpecCreate.model_validate({
        'constraint_set_id': constraint_set_id,
        'name': 'Small deterministic grid',
        'axes': [
            {'entity_id': 'FL', 'axis': 'x', 'min_m': 0.8, 'max_m': 1.2, 'step_m': 0.2},
            {'entity_id': 'FL', 'axis': 'y', 'min_m': 0.8, 'max_m': 1.0, 'step_m': 0.2},
        ],
        'linked_derivations': [
            {'constraint_id': 'lr-mirror', 'master_entity_id': 'FL'},
            {'constraint_id': 'lr-y', 'master_entity_id': 'FL'},
        ],
        'candidate_limit': 20,
    })


def stored_constraint_spec() -> dict:
    return validate_constraint_set_for_context(constraint_request(), context_payload())


def validated_search_spec() -> tuple[dict, dict]:
    return validate_search_spec(
        search_request(), context_payload(), context_id='ctx-1', constraint_set_id='cs-1',
        constraint_set_spec=stored_constraint_spec(), constraint_set_spec_sha256='c' * 64,
    )


def test_grid_values_do_not_invent_off_lattice_maximum() -> None:
    axis = GridAxis(entity_id='FL', axis='x', min_m=0.0, max_m=1.0, step_m=0.4)
    assert grid_values(axis) == [0.0, 0.4, 0.8]


def test_preview_counts_hand_computable_raw_grid() -> None:
    spec, estimate = validated_search_spec()
    assert spec['algorithm_version'] == 'search-space-grid-1'
    assert estimate['raw_candidate_count'] == 6
    assert [(item['axis'], item['count']) for item in estimate['axis_counts']] == [('x', 3), ('y', 2)]
    assert estimate['linked_derivation_count'] == 2


def test_generation_filters_hard_rejection_and_is_fully_reproducible() -> None:
    spec, _ = validated_search_spec()
    first = generate_search_space(
        context_payload(), spec, search_spec_sha256='s' * 64,
        constraint_set_spec=stored_constraint_spec(), constraint_set_spec_sha256='c' * 64,
    )
    second = generate_search_space(
        context_payload(), spec, search_spec_sha256='s' * 64,
        constraint_set_spec=stored_constraint_spec(), constraint_set_spec_sha256='c' * 64,
    )
    assert first == second
    assert first['raw_candidate_count'] == 6
    assert first['feasible_candidate_count'] == 5
    assert first['rejected_candidate_count'] == 1
    assert first['rejection_counts']['rack'] == 1
    assert [item['raw_index'] for item in first['candidates']] == [0, 1, 2, 4, 5]
    assert len(first['candidate_set_sha256']) == 64
    assert len({item['candidate_id'] for item in first['candidates']}) == 5

    candidate = first['candidates'][0]
    assert candidate['positions']['FL'] == {'x_m': 0.8, 'y_m': 0.8, 'z_m': 1.0}
    assert candidate['positions']['FR'] == {'x_m': 3.2, 'y_m': 0.8, 'z_m': 1.0}


def test_generation_paginates_coordinates_without_changing_full_set_hash() -> None:
    spec, _ = validated_search_spec()
    full = generate_search_space(
        context_payload(), spec, search_spec_sha256='s' * 64,
        constraint_set_spec=stored_constraint_spec(), constraint_set_spec_sha256='c' * 64,
    )
    page = generate_search_space(
        context_payload(), spec, search_spec_sha256='s' * 64,
        constraint_set_spec=stored_constraint_spec(), constraint_set_spec_sha256='c' * 64,
        offset=1, limit=2,
    )
    assert page['feasible_candidate_count'] == full['feasible_candidate_count'] == 5
    assert page['candidate_set_sha256'] == full['candidate_set_sha256']
    assert page['returned_candidate_count'] == 2
    assert [item['candidate_id'] for item in page['candidates']] == [
        item['candidate_id'] for item in full['candidates'][1:3]
    ]
    assert [item['feasible_index'] for item in page['candidates']] == [1, 2]


def test_candidate_limit_is_a_hard_pre_generation_gate() -> None:
    request = search_request()
    request.candidate_limit = 5
    with pytest.raises(ValueError, match='estimate 6 exceeds'):
        validate_search_spec(
            request, context_payload(), context_id='ctx', constraint_set_id='cs',
            constraint_set_spec=stored_constraint_spec(), constraint_set_spec_sha256='c' * 64,
        )


def test_search_axes_must_reference_known_baselined_entities_and_room_bounds() -> None:
    request = search_request()
    request.axes[0].entity_id = 'UNKNOWN'
    with pytest.raises(ValueError, match='unknown entity_id'):
        validate_search_spec(
            request, context_payload(), context_id='ctx', constraint_set_id='cs',
            constraint_set_spec=stored_constraint_spec(), constraint_set_spec_sha256='c' * 64,
        )

    request = search_request()
    request.axes[0].max_m = 4.1
    with pytest.raises(ValueError, match='outside room reference bounds'):
        validate_search_spec(
            request, context_payload(), context_id='ctx', constraint_set_id='cs',
            constraint_set_spec=stored_constraint_spec(), constraint_set_spec_sha256='c' * 64,
        )


def test_grid_search_requires_context_baseline_and_link_contract() -> None:
    context = context_payload()
    context['speakers'][0]['position'] = None
    with pytest.raises(ValueError, match='baseline position for FL'):
        validate_search_spec(
            search_request(), context, context_id='ctx', constraint_set_id='cs',
            constraint_set_spec=stored_constraint_spec(), constraint_set_spec_sha256='c' * 64,
        )

    request = search_request()
    request.linked_derivations[0].constraint_id = 'missing-link'
    with pytest.raises(ValueError, match='unknown linked constraint'):
        validate_search_spec(
            request, context_payload(), context_id='ctx', constraint_set_id='cs',
            constraint_set_spec=stored_constraint_spec(), constraint_set_spec_sha256='c' * 64,
        )

    request = search_request()
    request.axes.append(GridAxis(entity_id='FR', axis='x', min_m=3.0, max_m=3.0, step_m=0.1))
    with pytest.raises(ValueError, match='both a grid axis and linked derivation target'):
        validate_search_spec(
            request, context_payload(), context_id='ctx', constraint_set_id='cs',
            constraint_set_spec=stored_constraint_spec(), constraint_set_spec_sha256='c' * 64,
        )


def test_search_spec_model_rejects_duplicate_grid_axes() -> None:
    payload = search_request().model_dump(mode='json')
    payload['axes'].append(dict(payload['axes'][0]))
    with pytest.raises(ValueError, match='unique'):
        SearchSpecCreate.model_validate(payload)


def _create_api_fixture(client: TestClient) -> tuple[dict, dict, dict]:
    project = client.post('/api/projects', json={'name': 'O10 search fixture'}).json()
    context_response = client.post(f"/api/projects/{project['id']}/contexts", json=context_payload())
    assert context_response.status_code == 201, context_response.text
    context = context_response.json()
    constraint_payload = constraint_request(context['id']).model_dump(mode='json')
    constraint_response = client.post(f"/api/projects/{project['id']}/constraint-sets", json=constraint_payload)
    assert constraint_response.status_code == 201, constraint_response.text
    return project, context, constraint_response.json()


def test_search_spec_api_preview_persist_generate_and_restart(tmp_path: Path) -> None:
    app = create_app(tmp_path / 'source')
    client = TestClient(app)
    project, context, constraint_set = _create_api_fixture(client)
    request = search_request(constraint_set['id']).model_dump(mode='json')

    preview = client.post(f"/api/projects/{project['id']}/search-specs/preview", json=request)
    assert preview.status_code == 200, preview.text
    assert preview.json()['raw_candidate_count'] == 6
    assert preview.json()['classification'] == 'placement_search_space_preview'

    created = client.post(f"/api/projects/{project['id']}/search-specs", json=request)
    assert created.status_code == 201, created.text
    record = created.json()
    assert record['context_id'] == context['id']
    assert record['constraint_set_id'] == constraint_set['id']
    assert record['estimate']['raw_candidate_count'] == 6
    assert len(record['spec_sha256']) == 64

    listed = client.get(f"/api/projects/{project['id']}/search-specs?context_id={context['id']}")
    assert listed.status_code == 200
    assert [item['id'] for item in listed.json()] == [record['id']]

    first = client.post(f"/api/projects/{project['id']}/search-specs/{record['id']}/generate")
    second = client.post(f"/api/projects/{project['id']}/search-specs/{record['id']}/generate")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()['feasible_candidate_count'] == 5
    assert first.json()['search_spec_sha256'] == record['spec_sha256']

    # The retired browser backup/restore endpoints are covered by
    # test_browser_backup_retired.py; here a same-data-dir restart proves the
    # persisted spec regenerates identical deterministic output.
    restarted_client = TestClient(create_app(tmp_path / 'source'))
    restarted_generate = restarted_client.post(f"/api/projects/{project['id']}/search-specs/{record['id']}/generate")
    assert restarted_generate.status_code == 200, restarted_generate.text
    assert restarted_generate.json() == first.json()


def test_corrupted_search_spec_is_reported_and_generation_is_blocked(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    client = TestClient(app)
    project, _, constraint_set = _create_api_fixture(client)
    request = search_request(constraint_set['id']).model_dump(mode='json')
    record = client.post(f"/api/projects/{project['id']}/search-specs", json=request).json()

    with app.state.store.connect() as db:
        db.execute("UPDATE search_specs SET spec_json = ? WHERE id = ?", ('{"corrupted":true}', record['id']))
        db.commit()

    integrity = client.get('/api/integrity').json()
    assert f"search_spec_hash_mismatch:{record['id']}" in integrity['problems']
    generated = client.post(f"/api/projects/{project['id']}/search-specs/{record['id']}/generate")
    assert generated.status_code == 409
    assert generated.json()['detail'] == 'SearchSpec integrity check failed'


def test_generation_rejects_constraint_hash_drift() -> None:
    spec, _ = validated_search_spec()
    with pytest.raises(ValueError, match='hash no longer matches'):
        generate_search_space(
            context_payload(), spec, search_spec_sha256='s' * 64,
            constraint_set_spec=stored_constraint_spec(), constraint_set_spec_sha256='d' * 64,
        )
