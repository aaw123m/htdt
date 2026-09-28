"""Round-13 exported-artifact truth regressions.

Pins the fixes from docs/reviews/round13-exports.md: a writer must either
publish the complete artifact or leave nothing that looks like one.

- the difference-plot PNG export surfaces QImage.save's boolean failure
  instead of claiming success, and publishes via a sibling temp;
- the migration backup zip is staged and hardlink-published so an
  interrupted write can never leave a member-incomplete archive that
  opens cleanly;
- content-addressed / canonical-name writes (treatment CAS, file-adapter
  materialization) go through the atomic writer;
- recovery metadata and window state write atomically;
- the capture receiver's TLS pair is staged/regenerated so a torn
  credential cannot brick the listener forever.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import hashlib
import json
import shutil
import sqlite3
import zipfile
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from htdt.cad_acoustic_treatment_repository import CadAcousticTreatmentRepository
from htdt.cad_calibration import (
    CadCalibrationChannel,
    CadCalibrationPlan,
    CadDeviceCapabilityConstraints,
    build_generic_biquad_export,
)
from htdt.cad_device_adapter import (
    CalibrationAdapterService,
    build_device_binding,
)
from htdt.cad_device_adapter_file import FILE_ADAPTER_ID, FileCalibrationAdapter
from htdt.cad_repository import SceneRepository
from htdt.capture_receiver import CaptureReceiverService
from htdt.capture_ingestion_transaction import CaptureIngestionRepository
from htdt.capture_inbox import CaptureInboxRepository
from htdt.database import SCHEMA_VERSION, Store
from htdt.migration_guard import MigrationOpenError
from htdt.startup_recovery import (
    complete_launch,
    load_recovery_metadata,
    record_launch,
)
from htdt.window_state import (
    PersistedWindowState,
    load_window_state,
    save_window_state,
    window_state_path,
)

NOW = '2026-09-27T00:00:00+00:00'


# -- treatment CAS store --------------------------------------------------------


def test_treatment_source_asset_publishes_exact_bytes(tmp_path: Path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository = CadAcousticTreatmentRepository(scene_repository)
    data = '吸音パネル仕様\r\n密度: 48kg/m3\n'.encode('utf-8')

    digest = repository.save_source_asset(filename='panel.txt', data=data)

    target = repository.assets_dir / digest
    assert target.read_bytes() == data
    # A same-content save is a no-op, and no staging residue survives.
    assert repository.save_source_asset(filename='panel.txt', data=data) == digest
    leftovers = [p.name for p in repository.assets_dir.iterdir() if p != target]
    assert leftovers == []
    # A poisoned slot still fails closed on the next save.
    target.write_bytes(b'torn-partial-write')
    with pytest.raises(ValueError, match='hash collision'):
        repository.save_source_asset(filename='panel.txt', data=data)


# -- file adapter materialization ------------------------------------------------


def _constraints() -> CadDeviceCapabilityConstraints:
    return CadDeviceCapabilityConstraints(
        capability_id='test-device-1',
        capability_version='1',
        supported_sample_rates_hz=(48000,),
        supported_filter_types=('peaking',),
        channel_gain_resolution_db=0.5,
    )


def _plan() -> CadCalibrationPlan:
    channel = CadCalibrationChannel(
        channel_id='ch-1',
        role_id='FL',
        source_entity_id='spk-fl',
        physical_output_id='out-1',
        sample_rate_hz=48000,
        gain_db=1.23,
        delay_s=0.001,
        polarity='normal',
        crossovers=(),
        peq=(),
        routing=('avr-ch1',),
    )
    payload = {
        'plan_id': 'plan-1',
        'plan_version': '1',
        'created_at_utc': NOW,
        'source_kind': 'provided_fixture',
        'document_id': 'doc-1',
        'scene_revision_id': 'rev-1',
        'scene_content_hash': 'a' * 64,
        'system_variant_id': 'var-1',
        'system_variant_sha256': 'b' * 64,
        'source_measurement_id': 'm-1',
        'source_measurement_sha256': 'c' * 64,
        'source_dataset_id': 'd-1',
        'source_dataset_sha256': 'e' * 64,
        'measurement_quality_report_id': 'q-1',
        'measurement_quality_report_sha256': 'f' * 64,
        'sample_rate_hz': 48000,
        'channels': (channel,),
        'target_curve': None,
        'max_boost_db': 6.0,
        'max_cut_db': 10.0,
        'device_constraints': _constraints(),
        'support_state': 'SUPPORTED',
        'unsupported_reasons': (),
        'plan_semantic_sha256': '0' * 64,
    }
    provisional = CadCalibrationPlan.model_construct(**payload)
    return CadCalibrationPlan(
        **{
            **payload,
            'plan_semantic_sha256': hashlib.sha256(
                json.dumps(
                    provisional.semantic_payload(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(',', ':'),
                    allow_nan=False,
                ).encode('utf-8')
            ).hexdigest(),
        }
    )


def test_materialize_file_is_complete_parseable_json(tmp_path: Path) -> None:
    adapter = FileCalibrationAdapter(tmp_path / 'out')
    service = CalibrationAdapterService(adapter)
    export = build_generic_biquad_export(plan=_plan(), created_at_utc=NOW)
    binding = build_device_binding(
        adapter_id=FILE_ADAPTER_ID,
        device_family='avr-family',
        device_model='AVR-X1000',
        device_serial='SN-42',
        firmware_version='1.2.3',
        routing=(('ch-1', 'out-1'),),
        bound_at_utc=NOW,
    )
    materialization = service.materialize_export(
        export, binding, created_at_utc=NOW
    )
    written = tmp_path / 'out' / f'{materialization.materialization_id}.json'
    assert json.loads(written.read_text(encoding='utf-8')) == json.loads(
        materialization.payload_text
    )


# -- pre-migration backup --------------------------------------------------------


def _v1_root(root: Path) -> tuple[Path, bytes]:
    root.mkdir(parents=True)
    assets = root / 'assets'
    assets.mkdir()
    asset_bytes = b'legacy-asset-payload\x00\xff'
    digest = hashlib.sha256(asset_bytes).hexdigest()
    (root / 'assets' / f'{digest}.bin').write_bytes(asset_bytes)
    db = sqlite3.connect(root / 'htdt.sqlite3')
    try:
        db.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata(key, value) VALUES ('schema_version', '1');
            CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE contexts (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, revision_number INTEGER NOT NULL, parent_context_id TEXT, created_at TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE(project_id, revision_number));
            CREATE TABLE assets (sha256 TEXT PRIMARY KEY, relative_path TEXT NOT NULL, original_filename TEXT NOT NULL, size_bytes INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE measurements (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, context_id TEXT NOT NULL, channel_role TEXT NOT NULL, evidence_type TEXT NOT NULL, source_speaker_ids_json TEXT NOT NULL, radiation_scope TEXT NOT NULL, captured_at TEXT, imported_at TEXT NOT NULL, notes TEXT);
            CREATE TABLE datasets (id TEXT PRIMARY KEY, measurement_id TEXT NOT NULL, asset_sha256 TEXT NOT NULL, kind TEXT NOT NULL, frequency_blob BLOB NOT NULL, level_blob BLOB NOT NULL, phase_blob BLOB, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE comparisons (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, dataset_a_id TEXT NOT NULL, dataset_b_id TEXT NOT NULL, spec_json TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL);
            INSERT INTO projects VALUES ('p1', 'Legacy Room', '2026-09-15T00:00:00+00:00');
            INSERT INTO contexts VALUES ('c1', 'p1', 1, NULL, '2026-09-15T00:00:00+00:00', '{}');
            """
        )
        db.execute(
            'INSERT INTO assets VALUES (?, ?, ?, ?, ?)',
            (
                digest,
                f'assets/{digest}.bin',
                'asset.bin',
                len(asset_bytes),
                '2026-09-15T00:00:00+00:00',
            ),
        )
        db.execute(
            "INSERT INTO measurements VALUES ('m1', 'p1', 'c1', 'front_left', 'measured', '[]', 'single', NULL, '2026-09-15T00:00:00+00:00', NULL)"
        )
        db.execute(
            "INSERT INTO datasets VALUES ('d1', 'm1', ?, 'frequency_response', x'00', x'01', NULL, '{}', '2026-09-15T00:00:00+00:00')",
            (digest,),
        )
        db.commit()
    finally:
        db.close()
    return root, asset_bytes


def test_migration_backup_archive_contains_every_member(tmp_path: Path) -> None:
    root, asset_bytes = _v1_root(tmp_path / 'legacy')
    store = Store(root)

    archive_path = store.pre_migration_backup
    with zipfile.ZipFile(archive_path, 'r') as archive:
        names = archive.namelist()
        assert 'htdt.sqlite3' in names
        assert 'manifest.json' in names
        assert 'assets/' in names
        asset_name = next(n for n in names if n != 'assets/' and n.startswith('assets/'))
        assert archive.read(asset_name) == asset_bytes
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['reason'] == 'pre_migration'
        assert manifest['target_schema_version'] == SCHEMA_VERSION


def test_failed_backup_write_leaves_no_archive(tmp_path: Path, monkeypatch) -> None:
    root, _asset_bytes = _v1_root(tmp_path / 'legacy')

    def bombing_zip(*args, **kwargs):
        real = zipfile.ZipFile(*args, **kwargs)
        original_write = real.write
        calls = {'n': 0}

        def write(*write_args, **write_kwargs):
            calls['n'] += 1
            if calls['n'] > 1:
                raise RuntimeError('simulated crash mid-archive')
            return original_write(*write_args, **write_kwargs)

        real.write = write
        return real

    monkeypatch.setattr(
        'htdt.migration_guard.zipfile',
        SimpleNamespace(
            ZipFile=bombing_zip,
            ZIP_DEFLATED=zipfile.ZIP_DEFLATED,
            ZipInfo=zipfile.ZipInfo,
            BadZipFile=zipfile.BadZipFile,
        ),
    )
    with pytest.raises((RuntimeError, MigrationOpenError)):
        Store(root)

    backups_dir = root / 'backups'
    left = list(backups_dir.iterdir()) if backups_dir.is_dir() else []
    # No torn 'looks-valid' archive and no staged residue.
    assert [p.name for p in left] == []


# -- recovery metadata -----------------------------------------------------------


def test_recovery_launch_metadata_is_complete_json(tmp_path: Path) -> None:
    data_dir = tmp_path / 'data'
    record = record_launch(
        data_dir,
        build_id='build-13',
        launch_mode='normal',
        started_at_utc=NOW,
    )
    complete_launch(data_dir, record.launch_id, clean=True)

    metadata = load_recovery_metadata(data_dir)
    assert len(metadata.records) == 1
    assert metadata.records[0].launch_id == record.launch_id
    assert metadata.records[0].clean_exit is True

    # The on-disk file parses as a complete JSON document, not a torn one.
    raw = (data_dir / 'diagnostics' / 'recovery-launch-metadata.json').read_text(
        encoding='utf-8'
    )
    assert json.loads(raw)['records'][0]['launch_id'] == record.launch_id


# -- window state -----------------------------------------------------------------


def test_window_state_file_is_complete_json(tmp_path: Path) -> None:
    state = PersistedWindowState(
        geometry_b64='QUJD',
        workspace='measurement',
        contexts={'measurement': 'compare'},
    )
    save_window_state(tmp_path, state)

    path = window_state_path(tmp_path)
    PersistedWindowState.model_validate_json(path.read_text(encoding='utf-8'))
    assert load_window_state(tmp_path) == state


# -- capture receiver TLS pair -----------------------------------------------------


def _service(tmp_path: Path) -> CaptureReceiverService:
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    ingestion = CaptureIngestionRepository(scene)
    inbox = CaptureInboxRepository(scene, ingestion)
    return CaptureReceiverService(
        scene, inbox, ingestion, data_dir=tmp_path / 'receiver'
    )


@pytest.mark.skipif(shutil.which('openssl') is None, reason='openssl required')
def test_torn_receiver_cert_self_heals_on_start(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.get_config()  # mints the credential pair
    cert_path = tmp_path / 'receiver' / 'receiver-cert.pem'
    key_path = tmp_path / 'receiver' / 'receiver-key.pem'

    cert_path.write_bytes(b'TORN-CERTIFICATE-BODY')

    port = service.start(host='127.0.0.1', port=0)
    try:
        assert port > 0
        assert b'BEGIN CERTIFICATE' in cert_path.read_bytes()
        assert b'PRIVATE KEY' in key_path.read_bytes()
    finally:
        service.stop()


# -- difference-plot PNG export ----------------------------------------------------


def _workspace(tmp_path: Path):
    from htdt.cad_measurement_quality_repository import (
        CadMeasurementQualityRepository,
    )
    from htdt.cad_measurement_repository import CadMeasurementRepository
    from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
    from htdt.measurement_page_workspace import MeasurementPageWorkspace
    from htdt.measurement_workflow import (
        MeasurementAssignment,
        MeasurementWorkflowController,
    )

    app = QApplication.instance() or QApplication([])
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(measurement_repository)
    controller = MeasurementWorkflowController(
        scene_repository,
        revision.document_id,
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
    )
    workspace = MeasurementPageWorkspace(controller)

    def commit(raw: bytes, evidence_type: str = 'measured'):
        controller.stage_rew_text(raw, 'import.txt')
        assignment = MeasurementAssignment(
            measurement_entity_id='point-mlp',
            evidence_type=evidence_type,
            channel_role='front_left',
            source_speaker_ids=('speaker-fl',),
            radiation_scope='single',
            routing_evidence='manual',
        )
        return controller.commit_pending(assignment)

    return app, controller, workspace, commit


def _save_comparison(controller, workspace, commit) -> None:
    measured = commit(b'20 70\n40 71\n80 69\n')
    predicted = commit(b'20 60\n40 62\n80 61\n', evidence_type='predicted')
    workspace.refresh()
    views = {v.measurement_id: v for v in controller.measurement_views()}
    workspace._last_comparison = controller.compare_datasets(
        views[measured.measurement_id].dataset_id,
        views[predicted.measurement_id].dataset_id,
        low_hz=20.0,
        high_hz=80.0,
    )


def test_difference_plot_png_export_writes_complete_image(
    tmp_path: Path, monkeypatch
) -> None:
    app, controller, workspace, commit = _workspace(tmp_path)
    try:
        _save_comparison(controller, workspace, commit)
        target = tmp_path / 'plot.png'
        monkeypatch.setattr(
            'htdt.measurement_page_workspace.file_dialog_memory'
            '.get_save_file_name',
            lambda *args, **kwargs: (str(target), 'PNG (*.png)'),
        )
        workspace._export_difference_plot_png()

        blob = target.read_bytes()
        assert blob[:8] == b'\x89PNG\r\n\x1a\n'
        image = QImage(str(target))
        assert not image.isNull()
        assert image.width() == 1280
        assert '差分プロットをPNGで書き出しました' in workspace.notice.text()
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_difference_plot_png_export_failure_is_not_reported_as_success(
    tmp_path: Path, monkeypatch
) -> None:
    app, controller, workspace, commit = _workspace(tmp_path)
    try:
        _save_comparison(controller, workspace, commit)
        # The parent directory does not exist: the write cannot succeed.
        target = tmp_path / 'no-such-dir' / 'plot.png'
        monkeypatch.setattr(
            'htdt.measurement_page_workspace.file_dialog_memory'
            '.get_save_file_name',
            lambda *args, **kwargs: (str(target), 'PNG (*.png)'),
        )
        workspace._export_difference_plot_png()

        assert not target.exists()
        assert '保存できませんでした' in workspace.notice.text()
        assert '書き出しました' not in workspace.notice.text()
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()
