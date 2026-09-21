"""The legacy browser backup/restore endpoints are retired (issue #332).

They remain registered only to answer ``410 Gone`` with a pointer to the
supported native ``.htdt-backup`` authority. These tests prove that every
request — including hostile archives that the old implementation would have
extracted — is rejected before any file is created, extracted, or modified.
"""

from __future__ import annotations

import base64
import io
import warnings
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from htdt.limits import MAX_SMALL_JSON_BODY_BYTES
from htdt.main import BROWSER_BACKUP_RETIRED_DETAIL, create_app
from htdt.security import install_local_request_boundary


def _legacy_archive(members: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members:
            archive.writestr(name, payload)
    return buffer.getvalue()


def _honest_legacy_archive(store_root: Path) -> bytes:
    """Build the legacy ZIP layout (manifest + htdt.sqlite3 + assets/)."""
    members: list[tuple[str, bytes]] = [
        ('manifest.json', b'{"created_at": "2026-01-01T00:00:00+00:00", "schema_version": 5}'),
        ('htdt.sqlite3', (store_root / 'htdt.sqlite3').read_bytes()),
    ]
    assets_dir = store_root / 'assets'
    if assets_dir.is_dir():
        for asset in sorted(assets_dir.iterdir()):
            if asset.is_file():
                members.append((f'assets/{asset.name}', asset.read_bytes()))
    return _legacy_archive(members)


def _post_archive(client: TestClient, archive: bytes):
    return client.post('/api/restore', json={'archive_base64': base64.b64encode(archive).decode('ascii')})


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob('*')) if path.is_file()}


def test_backup_get_is_gone_and_creates_no_files(tmp_path: Path) -> None:
    data_root = tmp_path / 'data'
    client = TestClient(create_app(data_root))
    client.post('/api/projects', json={'name': 'Room'})

    before = _snapshot(data_root)
    response = client.get('/api/backup')
    assert response.status_code == 410
    assert response.json()['detail'] == BROWSER_BACKUP_RETIRED_DETAIL
    assert '.htdt-backup' in response.json()['detail']
    # A safe-method request must not leave persistent state behind: the old
    # handler wrote a timestamped ZIP under backups/ before responding.
    assert _snapshot(data_root) == before
    assert not (data_root / 'backups').exists()


def test_restore_rejects_honest_legacy_archive(tmp_path: Path) -> None:
    data_root = tmp_path / 'data'
    client = TestClient(create_app(data_root))
    project = client.post('/api/projects', json={'name': 'Room'}).json()

    archive = _honest_legacy_archive(data_root)
    before = _snapshot(data_root)
    response = _post_archive(client, archive)
    assert response.status_code == 410
    assert response.json()['detail'] == BROWSER_BACKUP_RETIRED_DETAIL
    assert _snapshot(data_root) == before
    assert client.get('/api/projects').json()[0]['id'] == project['id']


def test_restore_rejects_tampered_asset_archive(tmp_path: Path) -> None:
    """A legacy backup with one asset byte changed is rejected before restore."""
    data_root = tmp_path / 'data'
    client = TestClient(create_app(data_root))
    project = client.post('/api/projects', json={'name': 'Room'}).json()
    context = client.post(f"/api/projects/{project['id']}/contexts", json={
        'room': {'width_m': 4.0, 'depth_m': 5.0, 'height_m': 2.4},
        'speakers': [],
        'measurement_point': {'point_id': 'mlp', 'label': 'MLP', 'position': {'x_m': 1.0, 'y_m': 1.0, 'z_m': 1.0}},
        'avr': {'manufacturer': 'Yamaha', 'model': 'RX-A4A'},
    }).json()
    attachment = client.post(f"/api/projects/{project['id']}/attachments", json={
        'filename': 'note.txt',
        'raw_base64': base64.b64encode(b'original-asset-bytes').decode('ascii'),
        'kind': 'measurement_note',
        'context_id': context['id'],
    })
    assert attachment.status_code == 201, attachment.text

    # Flip the asset payload while keeping member names intact — the old path
    # compared only referenced-asset existence, so this sailed through.
    with zipfile.ZipFile(io.BytesIO(_honest_legacy_archive(data_root))) as source:
        members = [(info.filename, source.read(info.filename)) for info in source.infolist()]
    assert any(name.startswith('assets/') for name, _ in members)
    tampered = _legacy_archive([
        (name, b'TAMPERED-asset-bytes' if name.startswith('assets/') else payload)
        for name, payload in members
    ])

    before = _snapshot(data_root)
    response = _post_archive(client, tampered)
    assert response.status_code == 410
    assert _snapshot(data_root) == before


def test_restore_rejects_zip_slip_and_absolute_members(tmp_path: Path) -> None:
    data_root = tmp_path / 'data'
    client = TestClient(create_app(data_root))
    manifest = b'{"schema_version": 5}'
    malicious_archives = [
        _legacy_archive([('manifest.json', manifest), ('htdt.sqlite3', b'x'), ('../evil.txt', b'pwned')]),
        _legacy_archive([('manifest.json', manifest), ('htdt.sqlite3', b'x'), ('assets/../../evil.txt', b'pwned')]),
        _legacy_archive([('manifest.json', manifest), ('htdt.sqlite3', b'x'), ('/absolute/evil.txt', b'pwned')]),
        _legacy_archive([('manifest.json', manifest), ('htdt.sqlite3', b'x'), ('C:/evil.txt', b'pwned')]),
    ]
    before = _snapshot(data_root)
    for archive in malicious_archives:
        response = _post_archive(client, archive)
        assert response.status_code == 410
    # Nothing is staged or extracted anywhere near the data dir or its parent.
    assert _snapshot(data_root) == before
    assert not (tmp_path / 'evil.txt').exists()
    assert not (data_root / 'absolute').exists()


def test_restore_rejects_duplicate_members_and_zip_bomb(tmp_path: Path) -> None:
    data_root = tmp_path / 'data'
    client = TestClient(create_app(data_root))
    manifest = b'{"schema_version": 5}'
    with warnings.catch_warnings():
        # zipfile warns when the same arcname is written twice; the duplicate
        # member is the point of this fixture.
        warnings.simplefilter('ignore', UserWarning)
        duplicates = _legacy_archive([
            ('manifest.json', manifest),
            ('htdt.sqlite3', b'first'),
            ('htdt.sqlite3', b'second'),
        ])
    # ~8 MiB of zeros compresses to a few KiB: a compressed request can be
    # small while the expanded fan-out is large.
    fan_out = _legacy_archive([
        ('manifest.json', manifest),
        ('htdt.sqlite3', b'x'),
        ('assets/filler.bin', b'0' * (8 * 1024 * 1024)),
    ])
    before = _snapshot(data_root)
    for archive in (duplicates, fan_out):
        assert len(archive) < 1024 * 1024
        response = _post_archive(client, archive)
        assert response.status_code == 410
    assert _snapshot(data_root) == before
    assert not (data_root / 'assets' / 'filler.bin').exists()


def test_restore_accepts_no_body_contract(tmp_path: Path) -> None:
    """The retired endpoint ignores the request body entirely."""
    client = TestClient(create_app(tmp_path / 'data'))
    assert client.post('/api/restore').status_code == 410
    assert client.post('/api/restore', json={}).status_code == 410
    assert client.post('/api/restore', content=b'not-json').status_code == 410


def test_backup_and_restore_under_local_request_boundary(tmp_path: Path) -> None:
    """Through the served boundary: GET creates nothing, POST stays bounded."""
    app = create_app(tmp_path / 'data')
    install_local_request_boundary(app, allow_testserver=True)
    client = TestClient(app)

    assert client.get('/api/backup').status_code == 410
    assert not (tmp_path / 'data' / 'backups').exists()

    # A hostile cross-origin POST is refused by the unsafe-method Origin policy
    # before the retired handler runs.
    cross_origin = client.post('/api/restore', json={'archive_base64': 'eA=='}, headers={'Origin': 'https://evil.example'})
    assert cross_origin.status_code == 403

    # The retired endpoint no longer earns a dedicated multi-hundred-MiB
    # allowance: a declared body beyond the small JSON bound is refused.
    declared = client.post(
        '/api/restore',
        content=b'{}',
        headers={'Content-Length': str(MAX_SMALL_JSON_BODY_BYTES + 1)},
    )
    assert declared.status_code == 413

    assert client.post('/api/restore', json={'archive_base64': 'eA=='}).status_code == 410
