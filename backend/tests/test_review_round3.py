"""Round-3 deferred-item sweep regression tests.

Covers the items implemented in this round (docs/reviews/round3-deferred.md):
legacy-API Swagger/OpenAPI gating, the SOFA hash-vs-parse TOCTOU fix, the
DataManagementController destroyed-detach, and strict canonical-JSON
delegation for the three previously lax helpers.
"""
from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
from threading import Event
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest

from htdt.cad_acoustic_environment import build_acoustic_environment_profile
from htdt.cad_search_models import canonical_search_json
from htdt.application_preferences import (
    PreferenceCategory,
    PreferenceDefinition,
    PreferenceValueError,
    PreferenceValueType,
)
from htdt.main import LEGACY_API_DOCS_ENV_VAR, create_app


def test_api_docs_hidden_unless_opted_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.delenv(LEGACY_API_DOCS_ENV_VAR, raising=False)
    client = TestClient(create_app(tmp_path))
    assert client.get('/api/docs').status_code == 404
    assert client.get('/api/openapi.json').status_code == 404
    assert client.get('/api/health').status_code == 200


def test_api_docs_served_when_opted_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv(LEGACY_API_DOCS_ENV_VAR, '1')
    client = TestClient(create_app(tmp_path))
    assert client.get('/api/docs').status_code == 200
    document = client.get('/api/openapi.json')
    assert document.status_code == 200
    assert '/api/health' in document.json()['paths']


def test_sofa_recorded_hash_covers_parsed_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """h5py must parse the bytes that were hashed, not the path a second time.

    The spy swaps the on-disk file for a rejected convention between the
    bounded read and the parse: a path-based parse would fail, the in-memory
    parse must succeed and record the original bytes' digest.
    """
    import h5py

    from test_cad_spatial_reproduction import NOW, _write_sofa
    from htdt.cad_spatial_reproduction import load_sofa_dataset_profile

    sofa = tmp_path / 'fixture.sofa'
    _write_sofa(sofa)
    original_bytes = sofa.read_bytes()
    brir = tmp_path / 'brir.sofa'
    _write_sofa(brir, convention='SingleRoomDRIR')
    brir_bytes = brir.read_bytes()

    real_file = h5py.File
    captured: list[object] = []

    def spying_file(fileobj, *args, **kwargs):
        captured.append(fileobj)
        # TOCTOU swap: a parser reading the path again would now see a BRIR
        # dataset that must be rejected; parsing the verified bytes succeeds.
        sofa.write_bytes(brir_bytes)
        return real_file(fileobj, *args, **kwargs)

    monkeypatch.setattr(h5py, 'File', spying_file)

    profile = load_sofa_dataset_profile(
        sofa,
        profile_id='spatial-profile:toctou',
        profile_version='1',
        personalization_scope='generic',
        license_kind='cc0_public',
        created_at_utc=NOW,
    )
    assert profile.convention == 'SimpleFreeFieldHRIR'
    assert profile.source_file_sha256 == sha256(original_bytes).hexdigest()
    assert len(captured) == 1
    captured[0].seek(0)
    assert captured[0].read() == original_bytes


def test_number_preference_rejects_non_finite() -> None:
    definition = PreferenceDefinition(
        key='compute.test_number',
        category=PreferenceCategory.COMPUTE,
        value_type=PreferenceValueType.NUMBER,
        default=1.0,
    )
    with pytest.raises(PreferenceValueError):
        definition.validate(float('nan'))
    with pytest.raises(PreferenceValueError):
        definition.validate(float('inf'))
    assert definition.validate(2.5) == 2.5


def test_canonical_search_json_fails_closed_on_nan() -> None:
    with pytest.raises(ValueError):
        canonical_search_json({'value': float('nan')})
    with pytest.raises(ValueError):
        canonical_search_json({'value': float('inf')})


def test_environment_profile_rejects_non_finite_air_state() -> None:
    with pytest.raises(ValueError):
        build_acoustic_environment_profile(
            label='nan humidity',
            sound_speed_source_kind='manual_measured',
            sound_speed_m_s=343.0,
            relative_humidity_percent=float('nan'),
            relative_humidity_source_kind='manual_measured',
            created_at_utc='2026-09-27T00:00:00+00:00',
        )
    with pytest.raises(ValueError):
        build_acoustic_environment_profile(
            label='inf pressure',
            sound_speed_source_kind='manual_measured',
            sound_speed_m_s=343.0,
            air_pressure_pa=float('inf'),
            air_pressure_source_kind='manual_measured',
            created_at_utc='2026-09-27T00:00:00+00:00',
        )
    # finite values still seal normally
    profile = build_acoustic_environment_profile(
        label='valid',
        sound_speed_source_kind='manual_measured',
        sound_speed_m_s=343.0,
        created_at_utc='2026-09-27T00:00:00+00:00',
    )
    assert profile.sound_speed_m_s == 343.0


def _pump_until(predicate, timeout_s: float = 5.0) -> bool:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_controller_destroyed_mid_operation_detaches_thread(tmp_path: Path) -> None:
    """A controller deleted mid-operation must not delete the live QThread."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication

    from htdt.data_management import (
        ApplicationDataLifecycle,
        DataManagementBackend,
        DataManagementController,
        DataOperationKind,
        lingering_op_thread_count,
    )

    app = QApplication.instance() or QApplication([])
    backend = DataManagementBackend(tmp_path)
    lifecycle = ApplicationDataLifecycle(
        freeze_mutations=lambda: None,
        release_data_handles=lambda: None,
        reopen_data_handles=lambda: None,
        thaw_mutations=lambda: None,
    )
    controller = DataManagementController(backend, lifecycle)
    baseline = lingering_op_thread_count()

    started = Event()
    release = Event()

    def blocked_job(_emit):
        started.set()
        release.wait(10)
        return None

    # _start is the shared op entry point; a gated job keeps the thread busy.
    controller._start(
        operation_id='op-detach',
        kind=DataOperationKind.SCAN_STORAGE,
        job=blocked_job,
        lifecycle_mode='none',
    )
    assert started.wait(5)
    thread = controller._active.thread

    controller.deleteLater()
    # deleteLater only posts a DeferredDelete; force-delivery is the
    # deterministic way to destroy the object in a test (no exec loop).
    QCoreApplication.sendPostedEvents(controller, QEvent.DeferredDelete)
    app.processEvents()

    # The controller is gone but the still-running thread survived.
    assert thread.isRunning()
    assert lingering_op_thread_count() == baseline + 1

    # thread.quit lands on the worker loop through a queued call, so the
    # main thread must pump events while the thread winds down.
    release.set()
    assert _pump_until(thread.isFinished)
    assert _pump_until(lambda: lingering_op_thread_count() == baseline)


def test_worker_pool_destroyed_mid_operation_detaches_thread() -> None:
    """The destroyed-bound detach in NativeWorkerPool must actually fire.

    ``destroyed`` connected to a bound method is silently never delivered on
    PySide6 (6.11), which made the safety net dead code — on the old wiring
    this test aborts the process with "QThread: Destroyed while running".
    """
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication

    from htdt.native_worker import NativeWorkerPool, lingering_thread_count

    app = QApplication.instance() or QApplication([])
    pool = NativeWorkerPool()
    baseline = lingering_thread_count()

    started = Event()
    release = Event()

    def stubborn(_cancel: Event) -> None:
        started.set()
        release.wait(10)

    thread, _worker = pool.start('dead-owner', stubborn)
    assert started.wait(5)

    pool.deleteLater()
    QCoreApplication.sendPostedEvents(pool, QEvent.DeferredDelete)
    app.processEvents()

    assert thread.isRunning()
    assert lingering_thread_count() == baseline + 1

    release.set()
    assert _pump_until(thread.isFinished)
    assert _pump_until(lambda: lingering_thread_count() == baseline)
