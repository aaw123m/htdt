import json
from pathlib import Path
from urllib.request import Request

import pytest
from fastapi.testclient import TestClient

from htdt.database import REW_API_SNAPSHOT_FORMAT, Store
from htdt.main import create_app
from htdt.rew_api import RewApiClient, RewFrequencyResponseSnapshot, decode_frequency_response, normalize_measurement_summaries


FIXTURES = Path(__file__).parent / 'fixtures'
UUID = '01628624-ee2a-4a0f-99bb-9cf9e1b9c859'


def context_payload() -> dict:
    return {
        'room': {'width_m': 4.0, 'depth_m': 5.0, 'height_m': 2.4, 'geometry_kind': 'rectangular', 'notes': None},
        'speakers': [],
        'measurement_point': {'point_id': 'mlp', 'label': 'MLP', 'position': {'x_m': 2.0, 'y_m': 3.0, 'z_m': 1.0}, 'aim_xyz': None, 'position_precision_m': None},
        'avr': {'manufacturer': 'Yamaha', 'model': 'RX-A4A', 'firmware': None, 'input_name': None, 'volume_db': None, 'processing_mode': None, 'peq_mode': None, 'extra': {}},
        'notes': None,
        'parent_context_id': None,
    }


def real_snapshot() -> RewFrequencyResponseSnapshot:
    summaries = normalize_measurement_summaries(json.loads((FIXTURES / 'rew_5_40_beta135_measurements.json').read_text(encoding='utf-8')))
    raw_fr = json.loads((FIXTURES / 'rew_5_40_beta135_frequency_response.json').read_text(encoding='utf-8'))
    decoded = decode_frequency_response(UUID, raw_fr, requested_unit='SPL', requested_ppo=96)
    return RewFrequencyResponseSnapshot(summaries[0], {'unit': 'SPL', 'ppo': 96}, raw_fr, decoded)


def test_store_rew_snapshot_roundtrip_and_canonical_raw_asset(tmp_path: Path) -> None:
    store = Store(tmp_path / 'store')
    project = store.create_project('REW snapshot')
    context = store.create_context(project['id'], context_payload(), None)
    snapshot = real_snapshot()

    first = store.import_rew_api_snapshot(
        project['id'], context['id'], snapshot, channel_role='front_left', source_speaker_ids=['FL'],
        api_base_url='http://127.0.0.1:4735',
    )
    second = store.import_rew_api_snapshot(
        project['id'], context['id'], snapshot, channel_role='front_left', source_speaker_ids=['FL'],
        api_base_url='http://127.0.0.1:4735',
    )

    assert first['asset_sha256'] == second['asset_sha256']
    assert first['measurement_id'] != second['measurement_id']
    assert first['dataset_id'] != second['dataset_id']
    assert first['duplicate_asset'] is False
    assert second['duplicate_asset'] is True
    assert second['existing_dataset_count'] == 1
    saved = store.get_frequency_response(first['dataset_id'])
    assert saved.frequency_hz == pytest.approx(snapshot.decoded.frequency_hz)
    assert saved.level_db == pytest.approx(snapshot.decoded.magnitude)
    with store.connect() as db:
        dataset = db.execute('SELECT phase_blob, metadata_json FROM datasets WHERE id = ?', (first['dataset_id'],)).fetchone()
        asset = db.execute('SELECT relative_path FROM assets WHERE sha256 = ?', (first['asset_sha256'],)).fetchone()
    assert dataset is not None and dataset['phase_blob'] is None
    metadata = json.loads(dataset['metadata_json'])
    assert metadata['source'] == 'rew_api'
    assert metadata['requested'] == {'unit': 'SPL', 'ppo': 96, 'smoothing': None}
    assert metadata['returned']['ppo'] == pytest.approx(96.0)
    assert metadata['returned']['smoothing'] == '1/48'
    assert metadata['phase_status'] == 'absent'
    assert metadata['raw_asset_sha256'] == first['asset_sha256']

    raw_path = store.root / asset['relative_path']
    raw = raw_path.read_bytes()
    wrapper = json.loads(raw.decode('utf-8'))
    assert wrapper['format'] == REW_API_SNAPSHOT_FORMAT
    assert wrapper['measurement_uuid'] == UUID
    assert wrapper['frequency_response']['magnitude'] == snapshot.raw_frequency_response['magnitude']
    assert raw == json.dumps(wrapper, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')

    measurements = store.list_measurements(project['id'])
    assert len(measurements) == 2
    assert all(item['quality_status'] == 'unknown' for item in measurements)
    assert all(item['quality_source'] == 'unknown' for item in measurements)
    assert all(item['routing_evidence'] == 'unknown' for item in measurements)
    assert all(item['evidence_type'] == 'unknown' for item in measurements)


def test_rew_snapshot_endpoint_validates_context_before_contacting_rew(tmp_path: Path) -> None:
    calls: list[str] = []

    class NoCallRew:
        base_url = 'http://127.0.0.1:4735'

        def get_frequency_response_snapshot(self, *args: object, **kwargs: object) -> RewFrequencyResponseSnapshot:
            calls.append('called')
            raise AssertionError('REW should not be contacted')

    client = TestClient(create_app(tmp_path, rew_client=NoCallRew()))  # type: ignore[arg-type]
    project = client.post('/api/projects', json={'name': 'P'}).json()
    response = client.post(f"/api/projects/{project['id']}/rew-snapshots", json={
        'measurement_uuid': UUID,
        'context_id': 'missing',
        'channel_role': 'front_left',
    })
    assert response.status_code == 404
    assert calls == []


def test_rew_snapshot_endpoint_rejects_unknown_session_before_rew(tmp_path: Path) -> None:
    calls: list[str] = []

    class NoCallRew:
        base_url = 'http://127.0.0.1:4735'

        def get_frequency_response_snapshot(self, *args: object, **kwargs: object) -> RewFrequencyResponseSnapshot:
            calls.append('called')
            raise AssertionError('REW should not be contacted')

    client = TestClient(create_app(tmp_path, rew_client=NoCallRew()))  # type: ignore[arg-type]
    project = client.post('/api/projects', json={'name': 'P'}).json()
    context = client.post(f"/api/projects/{project['id']}/contexts", json=context_payload()).json()
    response = client.post(f"/api/projects/{project['id']}/rew-snapshots", json={
        'measurement_uuid': UUID,
        'context_id': context['id'],
        'session_id': 'missing',
        'channel_role': 'front_left',
    })
    assert response.status_code == 404
    assert calls == []


def test_rew_snapshot_endpoint_saves_fresh_get_only_snapshot(tmp_path: Path) -> None:
    summary = {'uuid': UUID, 'title': 'fixture', 'date': '2026-Sep-16 12:11:10', 'rewVersion': 'V5.40 beta 135'}
    raw_fr = json.loads((FIXTURES / 'rew_5_40_beta135_frequency_response.json').read_text(encoding='utf-8'))
    methods: list[str] = []

    class FakeResponse:
        def __init__(self, payload: object) -> None:
            self.raw = json.dumps(payload).encode('utf-8')
        def __enter__(self) -> 'FakeResponse':
            return self
        def __exit__(self, *args: object) -> None:
            return None
        def read(self, amt: int = -1) -> bytes:
            return self.raw if amt is None or amt < 0 else self.raw[:amt]

    def opener(request: Request, timeout: float) -> FakeResponse:
        methods.append(request.get_method())
        if request.full_url.endswith('/measurements'):
            return FakeResponse({'1': summary})
        if request.full_url.endswith(f'/measurements/{UUID}'):
            return FakeResponse(summary)
        if f'/measurements/{UUID}/frequency-response?' in request.full_url:
            return FakeResponse(raw_fr)
        raise AssertionError(request.full_url)

    client = TestClient(create_app(tmp_path, rew_client=RewApiClient(opener=opener)))
    project = client.post('/api/projects', json={'name': 'P'}).json()
    context = client.post(f"/api/projects/{project['id']}/contexts", json=context_payload()).json()
    response = client.post(f"/api/projects/{project['id']}/rew-snapshots", json={
        'measurement_uuid': UUID,
        'context_id': context['id'],
        'channel_role': 'front_left',
        'source_speaker_ids': ['FL'],
        'ppo': 96,
        'unit': 'SPL',
    })
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload['quality_status'] == 'unknown'
    assert payload['routing_evidence'] == 'unknown'
    assert payload['evidence_type'] == 'unknown'
    assert payload['metadata']['requested']['ppo'] == 96
    assert payload['metadata']['returned']['smoothing'] == '1/48'
    assert methods and set(methods) == {'GET'}

    rows = client.get(f"/api/projects/{project['id']}/measurements").json()
    assert len(rows) == 1
    assert rows[0]['dataset_id'] == payload['dataset_id']
    assert rows[0]['quality_status'] == 'unknown'
