from __future__ import annotations

"""REV19 worker-offload contract tests: cancel seams, commit points, stop-busy.

Covers the D1/D2/D3 round-13 deferrals: cooperative cancellation through
the data-management jobs (cancel exceptions, CANCEL_UNTIL_COMMIT boundary,
registry CANCELLED bookkeeping), the REW client's multi-request cancel
seam, ``NativeWorkerPool.stop_all`` leaving the pool usable, and the
``stop_busy`` deactivation escape's prompt contract.
"""

import os
import sqlite3
import time
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from htdt.activity_center import ActivityCenter, OperationState
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
from htdt.data_management import (
    ApplicationDataLifecycle,
    DataManagementBackend,
    DataManagementController,
)
from htdt.data_relocation import (
    DataRelocationCancelledError,
    execute_data_relocation,
)
from htdt.native_backup import (
    BackupCancelledError,
    create_backup,
    restore_backup,
    validate_backup,
)
from htdt.native_worker import NativeWorkerPool
from htdt.rew_api import RewApiCancelledError, RewApiClient
from htdt.storage_maintenance import (
    StorageMaintenanceCancelledError,
    scan_storage,
)
from htdt.workspace_dirty_state import dirty_state_prompt


@pytest.fixture(scope="module")
def app() -> QApplication:
    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
    return instance


def _pump_until(predicate, timeout_s: float = 5.0) -> bool:
    app = QApplication.instance() or QApplication([])
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _seed_data(data_dir: Path) -> None:
    repository = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    CadMeasurementRepository(repository)

    raw = b'rev19-worker-contract-asset\n'
    digest = sha256(raw).hexdigest()
    relative_path = f'measurement-assets/{digest}'
    asset = data_dir / relative_path
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(raw)
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            '''INSERT INTO cad_measurement_assets(
                sha256, filename, relative_path, size_bytes
            ) VALUES (?, ?, ?, ?)''',
            (digest, 'fixture.txt', relative_path, len(raw)),
        )


# ----------------------------------------------------------------------
# D3 — REW client cancel seam between sequential requests
# ----------------------------------------------------------------------


def test_rew_snapshot_cancel_raises_before_any_request() -> None:
    """is_cancelled is consulted before the first of the four requests."""
    client = RewApiClient()
    with pytest.raises(RewApiCancelledError):
        client.get_frequency_response_snapshot(
            'any-uuid', is_cancelled=lambda: True
        )
    with pytest.raises(RewApiCancelledError):
        client.list_measurements(is_cancelled=lambda: True)


# ----------------------------------------------------------------------
# D2 — backup / storage cancel seams
# ----------------------------------------------------------------------


def test_create_backup_honours_cancel(tmp_path: Path) -> None:
    data_dir = tmp_path / 'data'
    _seed_data(data_dir)
    destination = tmp_path / 'out.htdt-backup'
    with pytest.raises(BackupCancelledError):
        create_backup(data_dir, destination, is_cancelled=lambda: True)
    assert not destination.exists()


def test_validate_backup_honours_cancel(tmp_path: Path) -> None:
    data_dir = tmp_path / 'data'
    _seed_data(data_dir)
    archive = tmp_path / 'in.htdt-backup'
    create_backup(data_dir, archive)
    with pytest.raises(BackupCancelledError):
        validate_backup(archive, is_cancelled=lambda: True)


def test_restore_backup_cancels_before_journal_commit(tmp_path: Path) -> None:
    data_dir = tmp_path / 'data'
    _seed_data(data_dir)
    archive = tmp_path / 'in.htdt-backup'
    create_backup(data_dir, archive)
    commit_calls: list[str] = []
    with pytest.raises(BackupCancelledError):
        restore_backup(
            data_dir,
            archive,
            is_cancelled=lambda: True,
            on_commit_point=lambda: commit_calls.append('commit'),
        )
    assert commit_calls == []
    # The live store was never swapped — the seeded DB still opens cleanly.
    repository = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    assert repository.latest(F1_DOCUMENT_ID) is not None


def test_restore_backup_commit_point_fires_once(tmp_path: Path) -> None:
    data_dir = tmp_path / 'data'
    _seed_data(data_dir)
    archive = tmp_path / 'in.htdt-backup'
    create_backup(data_dir, archive)
    commit_calls: list[str] = []
    manifest, _pre = restore_backup(
        data_dir,
        archive,
        on_commit_point=lambda: commit_calls.append('commit'),
    )
    assert commit_calls == ['commit']
    assert manifest.files


def test_scan_storage_honours_cancel(tmp_path: Path) -> None:
    data_dir = tmp_path / 'data'
    _seed_data(data_dir)
    with pytest.raises(StorageMaintenanceCancelledError):
        scan_storage(data_dir, is_cancelled=lambda: True)


def test_relocation_cancels_before_cutover(tmp_path: Path) -> None:
    source = tmp_path / 'source-data'
    _seed_data(source)
    destination = tmp_path / 'moved-data'
    commit_calls: list[str] = []
    with pytest.raises(DataRelocationCancelledError):
        execute_data_relocation(
            source,
            destination,
            is_cancelled=lambda: True,
            on_commit_point=lambda: commit_calls.append('commit'),
        )
    assert commit_calls == []
    # Source untouched; nothing promoted at the destination.
    assert (source / 'cad-scenes.sqlite3').is_file()
    assert not destination.exists()


# ----------------------------------------------------------------------
# D1 — stop_busy pool drain keeps the pool usable
# ----------------------------------------------------------------------


def test_stop_all_drains_but_pool_stays_usable(app: QApplication) -> None:
    pool = NativeWorkerPool(shutdown_timeout_ms=200)
    results: list[object] = []
    errors: list[object] = []

    def first(cancel_event: Event) -> str:
        while not cancel_event.is_set():
            time.sleep(0.01)
        return 'first'

    report = None
    thread, _worker = pool.start('first', first, lambda _k, r, e: None)
    report = pool.stop_all()
    # A well-behaved worker honoring its cancel flag drains inside the
    # budget — the pool must still accept follow-up work either way.
    completed = Event()

    def second(_cancel_event: Event) -> str:
        return 'second-result'

    pool.start(
        'second',
        second,
        lambda _key, result, error: (
            results.append(result),
            errors.append(error),
            completed.set(),
        ),
    )
    assert _pump_until(completed.is_set)
    assert errors == [None]
    assert results == ['second-result']
    pool.shutdown()


def test_busy_dirty_state_offers_stop_busy_resolution() -> None:
    """The wedged-worker veto now resolves: 'busy' is no longer unresolvable."""
    prompt = dirty_state_prompt('busy', 'navigate')
    assert prompt is not None
    assert prompt.resolvable
    actions = [choice.action for choice in prompt.choices]
    assert actions == ['stop_busy']
    assert prompt.choices[0].destructive


# ----------------------------------------------------------------------
# D2 — controller-level cancel reaches the activity registry
# ----------------------------------------------------------------------


def _slow_backup(
    data_dir: Path,
    destination: Path,
    *,
    allow_stale: bool = False,
    is_cancelled=None,
):
    """Worker-side stand-in that only finishes via the cancel seam."""
    for _ in range(500):
        if is_cancelled is not None and is_cancelled():
            raise BackupCancelledError('stopped by test')
        time.sleep(0.01)
    raise AssertionError('cancel flag was never observed')


def test_data_management_cancel_reaches_registry(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / 'data'
    _seed_data(data_dir)
    backend = DataManagementBackend(data_dir)
    lifecycle = ApplicationDataLifecycle(
        freeze_mutations=lambda: None,
        release_data_handles=lambda: None,
        reopen_data_handles=lambda: None,
        thaw_mutations=lambda: None,
    )
    center = ActivityCenter()
    controller = DataManagementController(
        backend, lifecycle, activity_center=center
    )
    monkeypatch.setattr(
        'htdt.data_management.native_create_backup', _slow_backup
    )
    cancelled: list[object] = []
    failures: list[object] = []
    controller.operation_cancelled.connect(cancelled.append)
    controller.operation_failed.connect(failures.append)

    operation_id = controller.create_backup(tmp_path / 'out.htdt-backup')
    assert controller.is_busy
    assert controller.request_cancel() is True
    assert _pump_until(lambda: not controller.is_busy)

    assert failures == []
    assert len(cancelled) == 1
    assert cancelled[0].operation_id == operation_id
    snapshot = center.get(operation_id)
    assert snapshot is not None
    assert snapshot.state == OperationState.CANCELLED
    controller.deleteLater()
