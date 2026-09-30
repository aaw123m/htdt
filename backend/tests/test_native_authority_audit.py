from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_synthetic_demo import seed_synthetic_optimization_demo
from htdt.native_authority_audit import (
    AuthorityAuditError,
    assert_native_authority_graph,
    audit_native_authority_graph,
)
import htdt.native_backup as native_backup
from htdt.native_backup import create_backup, restore_backup, validate_backup
from htdt.native_row_integrity import NativeRowIntegrityError


def _seeded_data(tmp_path: Path):
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    scene_repository = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    result = seed_synthetic_optimization_demo(scene_repository)
    return data_dir, scene_repository, result


def _update_payload(
    data_dir: Path,
    table: str,
    where: str,
    key: str,
    mutate,
) -> None:
    """Rewrite one modeled payload field without touching stored hashes."""
    database = data_dir / 'cad-scenes.sqlite3'
    with closing(sqlite3.connect(database)) as connection, connection:
        row = connection.execute(
            f'SELECT rowid, payload_json FROM {table} WHERE {where}=?',
            (key,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row[1])
        mutate(payload)
        connection.execute(
            f'UPDATE {table} SET payload_json=? WHERE rowid=?',
            (json.dumps(payload), row[0]),
        )


def _archive_with_database(
    archive: Path, database_path: Path, data_dir: Path
) -> Path:
    """Write a structurally valid backup archive around a crafted database.

    The manifest keeps the database's declared asset rows consistent so the
    archive passes every structural gate and reaches the semantic audit.
    """
    database_bytes = database_path.read_bytes()
    entries = [
        {
            'path': native_backup.DATABASE_NAME,
            'kind': 'database',
            'size_bytes': len(database_bytes),
            'sha256': sha256(database_bytes).hexdigest(),
        }
    ]
    assets: list[tuple[str, bytes]] = []
    with closing(sqlite3.connect(database_path)) as connection:
        rows = connection.execute(
            'SELECT sha256, relative_path, size_bytes '
            'FROM cad_measurement_assets ORDER BY sha256'
        ).fetchall()
    for digest, relative_path, size_bytes in rows:
        relative_path = str(relative_path).replace('\\', '/')
        raw = (data_dir / relative_path).read_bytes()
        entries.append(
            {
                'path': relative_path,
                'kind': 'measurement_asset',
                'size_bytes': int(size_bytes),
                'sha256': digest,
            }
        )
        assets.append((relative_path, raw))
    payload = {
        'schema_version': native_backup.BACKUP_SCHEMA_VERSION,
        'application_version': '0.0.0-test',
        'created_at_utc': '2026-09-21T00:00:00+00:00',
        'files': entries,
    }
    manifest = native_backup.BackupManifest(
        **payload,
        manifest_sha256=native_backup._manifest_hash(payload),
    )
    with ZipFile(archive, 'w', compression=ZIP_DEFLATED) as zipped:
        zipped.writestr(
            native_backup.MANIFEST_NAME,
            native_backup._canonical_json(
                manifest.model_dump(mode='json')
            ).encode('utf-8'),
        )
        zipped.writestr(native_backup.DATABASE_NAME, database_bytes)
        for relative_path, raw in assets:
            zipped.writestr(relative_path, raw)
    return archive


def _tampered_database_bytes(data_dir: Path, tmp_path: Path) -> Path:
    clone = tmp_path / 'tampered.sqlite3'
    clone.write_bytes((data_dir / 'cad-scenes.sqlite3').read_bytes())
    with closing(sqlite3.connect(clone)) as connection, connection:
        row = connection.execute(
            'SELECT rowid, payload_json FROM scene_revisions ORDER BY seq LIMIT 1'
        ).fetchone()
        payload = json.loads(row[1])
        payload['room']['width_m'] = float(payload['room']['width_m']) + 0.01
        connection.execute(
            'UPDATE scene_revisions SET payload_json=? WHERE rowid=?',
            (json.dumps(payload), row[0]),
        )
    return clone


def test_audit_passes_on_persisted_synthetic_fixture(tmp_path: Path):
    data_dir, _scene_repository, _result = _seeded_data(tmp_path)

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')

    assert report.ok, report.summary()
    checked = dict(report.checked)
    assert checked['scene_revision'] == 5
    assert checked['objective_evaluation'] == 10
    assert checked['adaptive_extended_observation'] == 18
    assert checked['managed_asset:cad_measurement_assets'] == 5


def test_audit_rejects_tampered_scene_revision(tmp_path: Path):
    data_dir, _scene_repository, result = _seeded_data(tmp_path)
    _update_payload(
        data_dir,
        'scene_revisions',
        'revision_id',
        result.source_revision_id,
        lambda payload: payload['room'].update(
            width_m=float(payload['room']['width_m']) + 0.01
        ),
    )

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')

    assert not report.ok
    diagnostics = [
        item for item in report.diagnostics if item.authority == 'scene_revision'
    ]
    assert diagnostics
    assert diagnostics[0].record_ref == result.source_revision_id


def test_audit_rejects_tampered_objective_evaluation(tmp_path: Path):
    data_dir, _scene_repository, _result = _seeded_data(tmp_path)
    with closing(sqlite3.connect(data_dir / 'cad-scenes.sqlite3')) as connection:
        evaluation_id = connection.execute(
            'SELECT evaluation_id FROM cad_objective_evaluations LIMIT 1'
        ).fetchone()[0]
    _update_payload(
        data_dir,
        'cad_objective_evaluations',
        'evaluation_id',
        evaluation_id,
        lambda payload: payload.update(candidate_id='tampered-candidate'),
    )

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')

    assert not report.ok
    assert any(
        item.authority == 'objective_evaluation'
        for item in report.diagnostics
    )


def test_audit_rejects_deleted_upstream_authority(tmp_path: Path):
    data_dir, _scene_repository, _result = _seeded_data(tmp_path)
    with closing(sqlite3.connect(data_dir / 'cad-scenes.sqlite3')) as connection, connection:
        connection.execute('DELETE FROM cad_search_specs')

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')

    assert not report.ok
    assert any(
        item.authority in {'search_spec', 'objective_evaluation', 'roomsim_batch_spec'}
        for item in report.diagnostics
    )
    assert any(
        item.failure_class in {'missing_evidence', 'stale_authority', 'noncanonical_derivation'}
        for item in report.diagnostics
    )


def test_audit_rejects_missing_and_tampered_managed_assets(tmp_path: Path):
    data_dir, _scene_repository, _result = _seeded_data(tmp_path)
    assets = data_dir / 'measurement-assets'
    victim = next(assets.iterdir())
    victim.unlink()

    missing = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')
    assert not missing.ok
    assert any(
        item.authority == 'managed_asset' and 'missing' in item.message
        for item in missing.diagnostics
    )

    _seeded_data2 = tmp_path / 'data2'
    _seeded_data2.mkdir()
    scene2 = SceneRepository(_seeded_data2 / 'cad-scenes.sqlite3')
    seed_synthetic_optimization_demo(scene2)
    assets2 = _seeded_data2 / 'measurement-assets'
    victim2 = next(assets2.iterdir())
    victim2.write_bytes(victim2.read_bytes() + b'tampered')

    tampered = audit_native_authority_graph(_seeded_data2 / 'cad-scenes.sqlite3')
    assert not tampered.ok
    assert any(
        item.authority == 'managed_asset' and 'mismatch' in item.message
        for item in tampered.diagnostics
    )


def test_audit_rejects_unknown_capture_ingestion_run(tmp_path: Path):
    data_dir, scene_repository, _result = _seeded_data(tmp_path)
    # Constructing the repository creates the capture tables on the DB.
    from htdt.capture_ingestion_transaction import CaptureIngestionRepository

    CaptureIngestionRepository(scene_repository)
    with closing(sqlite3.connect(data_dir / 'cad-scenes.sqlite3')) as connection, connection:
        connection.execute(
            '''INSERT INTO capture_ingestion_runs(
                ingestion_run_id, lineage_digest, plan_sha256,
                bundle_digest, capture_revision_id, capture_series_id,
                parent_revision_id, capture_session_ids_json,
                coordinate_space_ids_json, ingestor_name,
                ingestor_version, configuration_digest,
                plan_json, recorded_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                'capture-ingestion-run:' + 'e' * 64,
                'a' * 64,
                'd' * 64,
                'b' * 64,
                'rev-1',
                'series-1',
                None,
                '[]',
                '[]',
                'fixture-ingestor',
                '1',
                'c' * 64,
                '{}',
                '2026-09-21T00:00:00+00:00',
            ),
        )

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')

    assert not report.ok
    assert any(
        item.authority == 'capture_ingestion_run' for item in report.diagnostics
    )


def test_assert_raises_typed_diagnostics(tmp_path: Path):
    data_dir, _scene_repository, result = _seeded_data(tmp_path)
    _update_payload(
        data_dir,
        'scene_revisions',
        'revision_id',
        result.source_revision_id,
        lambda payload: payload['room'].update(
            width_m=float(payload['room']['width_m']) + 0.01
        ),
    )

    with pytest.raises(AuthorityAuditError) as caught:
        assert_native_authority_graph(data_dir / 'cad-scenes.sqlite3')

    report = caught.value.report
    assert not report.ok
    for diagnostic in report.diagnostics:
        assert diagnostic.failure_class in {
            'structural',
            'missing_evidence',
            'stale_authority',
            'noncanonical_derivation',
        }
        assert diagnostic.authority
        assert diagnostic.record_ref


def test_create_backup_rejects_semantically_corrupt_live_data(tmp_path: Path):
    data_dir, _scene_repository, result = _seeded_data(tmp_path)
    _update_payload(
        data_dir,
        'scene_revisions',
        'revision_id',
        result.source_revision_id,
        lambda payload: payload['room'].update(
            width_m=float(payload['room']['width_m']) + 0.01
        ),
    )

    # #313: payload-level corruption is now rejected by the row-integrity
    # gate before the authority graph audit runs; either layer failing
    # closed satisfies the contract.
    with pytest.raises((AuthorityAuditError, NativeRowIntegrityError)):
        create_backup(data_dir, tmp_path / 'must-not-exist.htdt-backup')

    assert not (tmp_path / 'must-not-exist.htdt-backup').exists()


def test_validate_backup_rejects_semantically_corrupt_archive(tmp_path: Path):
    data_dir, _scene_repository, _result = _seeded_data(tmp_path)
    tampered = _tampered_database_bytes(data_dir, tmp_path)
    archive = _archive_with_database(
        tmp_path / 'corrupt.htdt-backup', tampered, data_dir
    )

    with pytest.raises(
        ValueError,
        match='authority graph audit failed|native semantic integrity check failed',
    ):
        validate_backup(archive)


def test_restore_rejects_semantically_corrupt_archive(tmp_path: Path):
    data_dir, scene_repository, result = _seeded_data(tmp_path)
    live_digest = sha256(
        (data_dir / 'cad-scenes.sqlite3').read_bytes()
    ).hexdigest()
    tampered = _tampered_database_bytes(data_dir, tmp_path)
    archive = _archive_with_database(
        tmp_path / 'corrupt.htdt-backup', tampered, data_dir
    )

    with pytest.raises(
        ValueError,
        match='authority graph audit failed|native semantic integrity check failed',
    ):
        restore_backup(data_dir, archive)

    # Live data is untouched: staging validation failed before any swap.
    assert sha256(
        (data_dir / 'cad-scenes.sqlite3').read_bytes()
    ).hexdigest() == live_digest
    assert scene_repository.get(result.source_revision_id) is not None


def test_backup_round_trip_passes_with_audit_enabled(tmp_path: Path):
    data_dir, scene_repository, result = _seeded_data(tmp_path)
    archive = tmp_path / 'valid.htdt-backup'

    manifest = create_backup(data_dir, archive)
    assert manifest.files
    validate_backup(archive)

    target = tmp_path / 'restored'
    restored_manifest, _pre = restore_backup(target, archive)
    assert restored_manifest == manifest
    restored_scene = SceneRepository(target / 'cad-scenes.sqlite3')
    assert restored_scene.get(result.source_revision_id) is not None


def test_table_policy_registry_has_no_duplicate_or_malformed_entries() -> None:
    """REV24: silent shadowing — duplicate keys keep only the last entry.

    The capture_* and cad_* blocks were re-registered further down the
    same literal, leaving earlier OPERATIONAL_METADATA entries (and one
    malformed one-tuple) as dead code the audit never saw.
    """

    import ast
    import htdt.native_authority_audit as audit_module

    source = Path(audit_module.__file__).read_text(encoding='utf-8')
    literal = None
    for node in ast.walk(ast.parse(source)):
        target = getattr(node, 'target', None)
        if (
            isinstance(node, ast.AnnAssign)
            and getattr(target, 'id', '') == '_TABLE_POLICY'
        ):
            literal = node.value
    assert literal is not None

    keys = [ast.literal_eval(key) for key in literal.keys]
    assert len(keys) == len(set(keys))
    for entry in literal.values:
        assert isinstance(entry, ast.Tuple) and len(entry.elts) == 2
