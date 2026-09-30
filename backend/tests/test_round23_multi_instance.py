"""Round-23 multi-instance / multi-window concurrency verification.

Proves the contracts audited in docs/reviews/round23-multi-instance.md:

* the legacy API store serializes computed sequence numbers
  (``create_context`` MAX(revision)+1) across concurrent connections;
* the capture-receiver delivery ledger serializes its dedup check with
  the ledger insert, so a racing re-delivery dedups instead of crashing;
* a failed import never deletes a content-addressed asset file that a
  concurrent importer may have already adopted;
* the JSON side-stores (application preferences, library meta) detect an
  external modification of their file and merge onto the fresh document
  instead of silently overwriting it.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from htdt.application_preferences import (
    ApplicationPreferenceStore,
    IncompatiblePreferencesError,
    PreferenceLoadState,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite
from htdt.capture_receiver import CaptureReceiverService, ReceiverPairing
from htdt.database import Store
from htdt.reference_libraries import LibraryMetaStore


_MEASUREMENT_RAW = b'20 70\n40 71\n80 72\n160 73\n'


def _store(tmp_path: Path, name: str = 'root') -> Store:
    return Store(tmp_path / name)


def test_create_context_waits_for_external_writer(tmp_path: Path) -> None:
    """A create racing a held write transaction must wait, then compute the
    next revision — never collide on UNIQUE(project_id, revision_number)."""

    store = _store(tmp_path)
    project = store.create_project('room')
    blocker = connect_sqlite(store.db_path)
    try:
        blocker.execute('BEGIN IMMEDIATE')
        blocker.execute(
            'INSERT INTO contexts(id, project_id, revision_number, '
            'parent_context_id, created_at, payload_json) '
            'VALUES (?, ?, ?, NULL, ?, ?)',
            ('held-ctx', project['id'], 1, '2026-01-01T00:00:00Z', '{}'),
        )
        results: list[dict] = []
        errors: list[Exception] = []

        def worker() -> None:
            try:
                results.append(
                    store.create_context(project['id'], {'notes': 'w'}, None)
                )
            except Exception as exc:  # noqa: BLE001 - record any failure
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        # Give the worker time to reach its transaction begin while the
        # blocker still holds the write lock.
        time.sleep(0.4)
        blocker.commit()
        thread.join(10)
        assert not thread.is_alive()
        assert errors == []
        assert results[0]['revision_number'] == 2
    finally:
        blocker.close()


def test_create_context_concurrent_requests_get_distinct_revisions(
    tmp_path: Path,
) -> None:
    """N simultaneous create_context calls each commit a distinct revision."""

    store = _store(tmp_path)
    project = store.create_project('room')
    worker_count = 8
    barrier = threading.Barrier(worker_count)
    results: list[dict] = []
    errors: list[Exception] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(10)
            results.append(
                store.create_context(project['id'], {'i': index}, None)
            )
        except Exception as exc:  # noqa: BLE001 - record any failure
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(i,))
        for i in range(worker_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    assert sorted(c['revision_number'] for c in results) == list(
        range(1, worker_count + 1)
    )


def _receiver_service(tmp_path: Path) -> CaptureReceiverService:
    repository = SceneRepository(tmp_path / 'cad-scenes.sqlite3')
    return CaptureReceiverService(repository, data_dir=tmp_path / 'recv')


def _pairing() -> ReceiverPairing:
    return ReceiverPairing(
        pairing_id='pair-1',
        pairing_token='token-1',
        receiver_instance_id='receiver-1',
        project_ref=None,
        endpoint_url='https://receiver.example',
        capability_endpoint_url=None,
        missions_endpoint_url=None,
        pinned_identity='sha256:' + '0' * 64,
        confirmation_code='123456',
        state='active',
        created_at_utc='2026-01-01T00:00:00Z',
        confirmed_at_utc=None,
        expires_at_utc=None,
    )


def _delivery_kwargs(pairing: ReceiverPairing) -> dict:
    return dict(
        pairing=pairing,
        delivery_id='delivery-1',
        artifact_kind='capture_bundle',
        artifact_id=None,
        artifact_digest=None,
        capture_revision_id='rev-1',
        bundle_digest=None,
        archive_sha256='a' * 64,
        archive_bytes=10,
        outcome='accepted',
        staging_ref=None,
        lineage_digest=None,
        detail='',
    )


def test_record_delivery_dedup_serializes_concurrent_insert(
    tmp_path: Path,
) -> None:
    """A re-delivery racing a held writer dedups instead of colliding on
    the delivery_key primary key."""

    service = _receiver_service(tmp_path)
    pairing = _pairing()
    # The deliveries table has a FK on pairings: register the parent row
    # first so raw ledger inserts satisfy foreign_keys=ON.
    with closing(connect_sqlite(service.path)) as connection:
        connection.execute(
            'INSERT INTO capture_receiver_pairings('
            'pairing_id, pairing_token, receiver_instance_id, project_ref, '
            'endpoint_url, capability_endpoint_url, missions_endpoint_url, '
            'pinned_identity, confirmation_code, state, created_at_utc, '
            'confirmed_at_utc, expires_at_utc, capture_instance_id'
            ') VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                pairing.pairing_id,
                pairing.pairing_token,
                pairing.receiver_instance_id,
                pairing.project_ref,
                pairing.endpoint_url,
                pairing.capability_endpoint_url,
                pairing.missions_endpoint_url,
                pairing.pinned_identity,
                pairing.confirmation_code,
                pairing.state,
                pairing.created_at_utc,
                pairing.confirmed_at_utc,
                pairing.expires_at_utc,
                pairing.capture_instance_id,
            ),
        )
        connection.commit()
    blocker = connect_sqlite(service.path)
    try:
        blocker.execute('BEGIN IMMEDIATE')
        blocker.execute(
            'INSERT INTO capture_receiver_deliveries('
            'delivery_key, pairing_id, artifact_kind, artifact_id, '
            'artifact_digest, capture_revision_id, bundle_digest, '
            'archive_sha256, archive_bytes, outcome, staging_ref, '
            'lineage_digest, detail, received_at_utc'
            ') VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                'pair-1:delivery-1',
                'pair-1',
                'capture_bundle',
                None,
                None,
                'rev-1',
                None,
                'a' * 64,
                10,
                'accepted',
                None,
                None,
                '',
                '2026-01-01T00:00:00Z',
            ),
        )
        results: list = []
        errors: list[Exception] = []

        def worker() -> None:
            try:
                results.append(
                    service._record_delivery(**_delivery_kwargs(pairing))
                )
            except Exception as exc:  # noqa: BLE001 - record any failure
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        time.sleep(0.4)
        blocker.commit()
        thread.join(10)
        assert not thread.is_alive()
        assert errors == []
        # Dedup: the recorded row is returned, not a PK collision.
        assert results[0].delivery_key == 'pair-1:delivery-1'
        assert results[0].outcome == 'accepted'
    finally:
        blocker.close()


def test_failed_import_keeps_content_addressed_asset(
    tmp_path: Path,
) -> None:
    """A transaction failure after the asset file was placed leaves a
    benign unreferenced file — it is never unlinked."""

    store = _store(tmp_path)
    project = store.create_project('room')
    digest = hashlib.sha256(_MEASUREMENT_RAW).hexdigest()
    with pytest.raises(KeyError):
        store.import_measurement(
            project['id'],
            'missing-context',
            'm.txt',
            _MEASUREMENT_RAW,
            'front_left',
            'measured',
            ['fl'],
            'single',
            None,
            None,
        )
    assert (store.assets_dir / f'{digest}.txt').is_file()
    assert store.integrity_problems() == []


def test_failed_import_never_removes_adopted_asset(tmp_path: Path) -> None:
    """The real race: importer A places the content-addressed file, importer
    B commits rows referencing it, then A's transaction fails — B's file
    must survive."""

    root = tmp_path / 'root'
    store_ok = Store(root)
    store_bad = Store(root)
    project = store_ok.create_project('room')
    context = store_ok.create_context(project['id'], {'notes': 'ok'}, None)
    digest = hashlib.sha256(_MEASUREMENT_RAW).hexdigest()

    placed = threading.Event()
    release = threading.Event()
    original = store_bad._store_asset

    def wrapped(filename: str, data: bytes) -> object:
        result = original(filename, data)
        placed.set()
        release.wait(10)
        return result

    store_bad._store_asset = wrapped  # type: ignore[method-assign]
    errors: list[Exception] = []

    def bad_importer() -> None:
        try:
            store_bad.import_measurement(
                project['id'],
                'missing-context',
                'm.txt',
                _MEASUREMENT_RAW,
                'front_left',
                'measured',
                ['fl'],
                'single',
                None,
                None,
            )
        except KeyError:
            pass
        except Exception as exc:  # noqa: BLE001 - record any failure
            errors.append(exc)

    thread = threading.Thread(target=bad_importer)
    thread.start()
    assert placed.wait(5)
    # B commits rows referencing the file A just placed.
    store_ok.import_measurement(
        project['id'],
        context['id'],
        'm.txt',
        _MEASUREMENT_RAW,
        'front_left',
        'measured',
        ['fl'],
        'single',
        None,
        None,
    )
    release.set()
    thread.join(10)
    assert errors == []
    assert (root / 'assets' / f'{digest}.txt').is_file()
    assert store_ok.integrity_problems() == []


def test_preferences_external_edit_survives_set(tmp_path: Path) -> None:
    """An external writer's changes to other keys are merged in, not
    overwritten by a stale in-memory document."""

    path = tmp_path / 'prefs.json'
    store = ApplicationPreferenceStore(path)
    store.set('display_input.theme', 'dark')
    document = json.loads(path.read_text(encoding='utf-8'))
    document['values']['general.language'] = 'ja'
    document['values']['future.key'] = 'future'
    path.write_text(json.dumps(document), encoding='utf-8')

    store.set('display_input.length_unit', 'cm')

    written = json.loads(path.read_text(encoding='utf-8'))
    assert written['values']['general.language'] == 'ja'
    assert written['values']['future.key'] == 'future'
    assert written['values']['display_input.length_unit'] == 'cm'
    assert written['values']['display_input.theme'] == 'dark'


def test_preferences_external_delete_then_set_recreates(
    tmp_path: Path,
) -> None:
    """A file deleted externally refreshes to MISSING; the next write
    recreates an honest document rather than restoring the stale one."""

    path = tmp_path / 'prefs.json'
    store = ApplicationPreferenceStore(path)
    store.set('display_input.theme', 'dark')
    path.unlink()

    store.set('display_input.length_unit', 'cm')

    document = json.loads(path.read_text(encoding='utf-8'))
    assert document['values'] == {'display_input.length_unit': 'cm'}
    assert store.load_state == PreferenceLoadState.MISSING


def test_preferences_external_corruption_refuses_and_preserves(
    tmp_path: Path,
) -> None:
    """A file corrupted externally flips the store into the refused state:
    writes fail honestly and the foreign document is left untouched."""

    path = tmp_path / 'prefs.json'
    store = ApplicationPreferenceStore(path)
    store.set('display_input.theme', 'dark')
    path.write_text('{ not json', encoding='utf-8')

    with pytest.raises(IncompatiblePreferencesError):
        store.set('display_input.length_unit', 'cm')

    assert path.read_text(encoding='utf-8') == '{ not json'
    assert store.load_state == PreferenceLoadState.CORRUPT


def test_library_meta_external_edit_merged(tmp_path: Path) -> None:
    """Archive flags written by another writer survive this store's
    later set_archived call."""

    path = tmp_path / 'library-meta.json'
    store = LibraryMetaStore(path)
    store.set_archived('lib-a@1.0#h1', True)
    document = json.loads(path.read_text(encoding='utf-8'))
    document['archived'].append('lib-b@2.0#h2')
    path.write_text(json.dumps(document), encoding='utf-8')

    store.set_archived('lib-c@3.0#h3', True)

    written = json.loads(path.read_text(encoding='utf-8'))
    assert set(written['archived']) == {
        'lib-a@1.0#h1',
        'lib-b@2.0#h2',
        'lib-c@3.0#h3',
    }
