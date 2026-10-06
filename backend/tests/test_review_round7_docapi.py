"""Round-7 doc↔code truth + API-consistency review regression tests.

Covers the items implemented in this round (docs/reviews/round7-docapi.md):

- ``GET /measurements`` and ``GET /attachments`` accept the entity filters the
  other list endpoints already expose (``context_id``, plus ``session_id`` for
  measurements and ``measurement_id``/``kind`` for attachments) so the legacy
  UI does not have to fetch every row and filter client-side.
- Filter params that reference an entity outside the project return 404 — the
  same convention ``constraint-sets``/``search-specs`` already follow — instead
  of silently returning ``[]``.
- The Capture receiver's ``missions/{id}/receipt`` route requires the
  ``X-HTDT-Capture-Instance-ID`` header like the other mission routes, so a
  receipt can no longer skip the pairing's device binding.
- ``capabilities`` and ``deliveries`` reject trailing path segments (404)
  instead of silently ignoring them, matching the strict ``missions`` routes.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
import ssl
import urllib.error
import urllib.request
import uuid

import pytest
from fastapi.testclient import TestClient

from htdt.cad_repository import SceneRepository
from htdt.capture_ingestion_transaction import CaptureIngestionRepository
from htdt.capture_inbox import CaptureInboxRepository
from htdt.capture_receiver import CaptureReceiverService
from htdt.main import create_app


def _context_payload() -> dict:
    return {
        'room': {'width_m': 4.0, 'depth_m': 5.0, 'height_m': 2.4},
        'speakers': [
            {'speaker_id': 'fl', 'role': 'front_left', 'position': {'x_m': 1.0, 'y_m': 1.0, 'z_m': 1.0}},
        ],
        'measurement_point': {'point_id': 'mlp', 'label': 'MLP', 'position': {'x_m': 2.0, 'y_m': 3.5, 'z_m': 1.0}},
    }


def _project_and_context(client: TestClient, name: str = 'Room') -> tuple[str, str]:
    project = client.post('/api/projects', json={'name': name}).json()
    context = client.post(f"/api/projects/{project['id']}/contexts", json=_context_payload()).json()
    return project['id'], context['id']


def _import_measurement(client: TestClient, project_id: str, context_id: str, session_id: str | None = None,
                        filename: str = 'a.txt') -> dict:
    payload = {
        'filename': filename,
        'raw_base64': base64.b64encode(b'20 70\n40 71\n80 72\n').decode(),
        'context_id': context_id,
        'channel_role': 'front_left',
        'evidence_type': 'measured',
        'source_speaker_ids': [],
        'radiation_scope': 'single',
    }
    if session_id is not None:
        payload['session_id'] = session_id
    response = client.post(f'/api/projects/{project_id}/measurements', json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _attach(client: TestClient, project_id: str, **kwargs) -> dict:
    payload = {
        'filename': kwargs.pop('filename', 'note.txt'),
        'raw_base64': base64.b64encode(b'attachment-bytes').decode(),
    }
    payload.update(kwargs)
    response = client.post(f'/api/projects/{project_id}/attachments', json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_list_measurements_filters_by_context_and_session(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project_id, context_id = _project_and_context(client)
    second_context = client.post(
        f'/api/projects/{project_id}/contexts', json=_context_payload()
    ).json()
    session = client.post(f'/api/projects/{project_id}/sessions', json={'purpose': 'sweep'}).json()

    first = _import_measurement(client, project_id, context_id, session_id=session['id'])
    second = _import_measurement(client, project_id, context_id)
    third = _import_measurement(client, project_id, second_context['id'], filename='c.txt')

    all_rows = client.get(f'/api/projects/{project_id}/measurements').json()
    assert {row['id'] for row in all_rows} == {
        first['measurement_id'], second['measurement_id'], third['measurement_id']
    }

    by_context = client.get(f'/api/projects/{project_id}/measurements?context_id={context_id}').json()
    assert {row['id'] for row in by_context} == {first['measurement_id'], second['measurement_id']}
    by_session = client.get(f'/api/projects/{project_id}/measurements?session_id={session["id"]}').json()
    assert [row['id'] for row in by_session] == [first['measurement_id']]
    by_both = client.get(
        f'/api/projects/{project_id}/measurements?context_id={context_id}&session_id={session["id"]}'
    ).json()
    assert [row['id'] for row in by_both] == [first['measurement_id']]


def test_list_measurements_filter_404_for_foreign_entities(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project_id, _ = _project_and_context(client)
    other_project, foreign_context = _project_and_context(client, name='Foreign')
    foreign_session = client.post(f'/api/projects/{other_project}/sessions', json={}).json()

    response = client.get(f'/api/projects/{project_id}/measurements?context_id={foreign_context}')
    assert response.status_code == 404
    response = client.get(f'/api/projects/{project_id}/measurements?session_id={foreign_session["id"]}')
    assert response.status_code == 404
    response = client.get(f'/api/projects/{project_id}/measurements?context_id=nope')
    assert response.status_code == 404


def test_list_attachments_filters(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project_id, context_id = _project_and_context(client)
    measurement = _import_measurement(client, project_id, context_id)

    note = _attach(client, project_id, kind='measurement_note', measurement_id=measurement["measurement_id"],
                   context_id=context_id, filename='note.txt')
    _attach(client, project_id, kind='image', filename='pic.png')

    all_rows = client.get(f'/api/projects/{project_id}/attachments').json()
    assert len(all_rows) == 2
    by_context = client.get(f'/api/projects/{project_id}/attachments?context_id={context_id}').json()
    assert [row['id'] for row in by_context] == [note['id']]
    by_measurement = client.get(
        f'/api/projects/{project_id}/attachments?measurement_id={measurement["measurement_id"]}'
    ).json()
    assert [row['id'] for row in by_measurement] == [note['id']]
    by_kind = client.get(f'/api/projects/{project_id}/attachments?kind=image').json()
    assert [row['kind'] for row in by_kind] == ['image']


def test_list_attachments_filter_404_and_kind_validation(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project_id, _ = _project_and_context(client)
    other_project, foreign_context = _project_and_context(client, name='Foreign')
    foreign_measurement = _import_measurement(client, other_project, foreign_context)

    assert client.get(f'/api/projects/{project_id}/attachments?context_id={foreign_context}').status_code == 404
    assert client.get(
        f'/api/projects/{project_id}/attachments?measurement_id={foreign_measurement["measurement_id"]}'
    ).status_code == 404
    # an unknown kind value is a validation error, not an empty list
    assert client.get(f'/api/projects/{project_id}/attachments?kind=bogus').status_code == 422


# -- Capture receiver ---------------------------------------------------------


def _receiver_service(tmp_path: Path) -> tuple[CaptureReceiverService, object]:
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    ingestion = CaptureIngestionRepository(scene)
    inbox = CaptureInboxRepository(scene, ingestion)
    service = CaptureReceiverService(
        scene, inbox, ingestion, bundle_reader=None, data_dir=tmp_path / 'receiver'
    )
    pairing, _payload = service.begin_pairing()
    service.confirm_pairing(pairing.pairing_id)
    return service, pairing


def _receipt_body(package, pairing, device: str = 'dev-1') -> bytes:
    return json.dumps({
        'schema': 'htdt.capture.mission-receipt',
        'schema_version': '1.0.0',
        'receipt_id': str(uuid.uuid4()),
        'package_id': package.package_id,
        'package_sha256': package.package_sha256,
        'capture_instance_id': device,
        'paired_destination_id': pairing.pairing_id,
        'receiver_instance_id': pairing.receiver_instance_id,
        'mission_record_id': None,
        'received_at': '2026-01-01T00:00:00+00:00',
        'validation_result': 'imported',
        'detail': None,
    }).encode()


_MISSION_PAYLOAD = json.dumps(
    {'schema': 'htdt.capture-mission', 'schema_version': '1.0.0'}
).encode()


def test_mission_receipt_requires_capture_instance_header(tmp_path: Path) -> None:
    service, pairing = _receiver_service(tmp_path)
    package = service.queue_mission_package('pkg-1', _MISSION_PAYLOAD)
    status, body = service.handle_mission_receipt(
        pairing.pairing_token, 'pkg-1', _receipt_body(package, pairing), None
    )
    assert status == 400
    assert 'X-HTDT-Capture-Instance-ID' in body['detail']


def test_mission_receipt_rejects_device_mismatch(tmp_path: Path) -> None:
    service, pairing = _receiver_service(tmp_path)
    package = service.queue_mission_package('pkg-2', _MISSION_PAYLOAD)
    status, body = service.handle_mission_receipt(
        pairing.pairing_token,
        'pkg-2',
        _receipt_body(package, pairing, device='dev-9'),
        'dev-1',
    )
    assert status == 400
    assert body['detail'] == 'receipt device mismatch'


def test_singleton_routes_reject_path_suffixes(tmp_path: Path) -> None:
    service, pairing = _receiver_service(tmp_path)
    port = service.start(host='127.0.0.1', port=0)
    try:
        base = f'https://127.0.0.1:{port}/htdt-capture/v1/{pairing.pairing_token}'
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        def _status(request: urllib.request.Request) -> int:
            try:
                with urllib.request.urlopen(request, context=context) as response:
                    return response.status
            except urllib.error.HTTPError as exc:
                return exc.code

        # GET /capabilities with a trailing segment must not serve the document
        assert _status(urllib.request.Request(f'{base}/capabilities/junk')) == 404
        # POST /deliveries with a trailing segment must not ingest a bundle
        assert _status(
            urllib.request.Request(f'{base}/deliveries/junk', data=b'x', method='POST')
        ) == 404
        # and the exact routes still work
        assert _status(urllib.request.Request(f'{base}/capabilities')) == 200
    finally:
        service.stop()
