"""Row/payload semantic integrity: duplicated columns vs canonical payloads (#313)."""

from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
import uuid
from zipfile import ZipFile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_fixture_support as support  # noqa: E402
from test_cad_search import _build as _build_search_spec  # noqa: E402
from test_native_backup import _seed_data  # noqa: E402

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import SceneDocument
from htdt.canonical_json import canonical_sha256
from htdt.capture_connected_space import CONNECTED_DOC_DOMAIN
from htdt.cad_search_repository import CadSearchRepository
from htdt.capture_ingestion_transaction import (
    CaptureAuthorityRecord,
    CaptureIngestionPlan,
    CaptureIngestionRepository,
    CaptureIngestionTransactionError,
)
from htdt.capture_semantic_promotion import (
    CapturePromotionReplayError,
    CaptureSemanticPromotionRepository,
)
from htdt.native_backup import (
    BackupManifest,
    create_backup,
    validate_backup,
    _manifest_hash,
)
from htdt.native_row_integrity import (
    NativeRowIntegrityError,
    assert_row_integrity_registry_complete,
    canonical_payload_tables,
    scan_native_row_integrity,
    verify_native_row_integrity,
)


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _ingest_fixture(tmp_path: Path) -> CaptureIngestionRepository:
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads, _manifest = support.plan_and_payloads(tmp_path)
    result = repository.ingest(plan, payloads)
    assert result.created
    return repository


def _tamper(path: Path, sql: str, args: tuple = ()) -> None:
    with _connect(path) as connection:
        connection.execute(sql, args)
        connection.commit()


def test_scene_revision_document_id_drift_fails_closed(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision, _spec, _estimate = _build_search_spec(repository)

    _tamper(
        repository.path,
        "UPDATE scene_revisions SET document_id='forged' WHERE revision_id=?",
        (revision.revision_id,),
    )

    with pytest.raises(ValueError, match='document mismatch'):
        repository.get(revision.revision_id)


def test_search_spec_indexed_column_drift_fails_closed(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    _revision, spec, _estimate = _build_search_spec(repository)
    search_repository = CadSearchRepository(repository)
    search_repository.save(spec)

    _tamper(
        repository.path,
        "UPDATE cad_search_specs SET document_id='forged' "
        'WHERE search_spec_id=?',
        (spec.search_spec_id,),
    )

    with pytest.raises(ValueError, match='disagrees with its payload'):
        search_repository.get(spec.search_spec_id)
    with pytest.raises(ValueError, match='disagrees with its payload'):
        search_repository.list_specs('forged')


def test_capture_run_plan_drift_fails_closed(tmp_path: Path) -> None:
    repository = _ingest_fixture(tmp_path)
    plan, _payloads, _manifest = support.plan_and_payloads(tmp_path)
    typed = CaptureIngestionPlan.model_validate(plan)
    forged = 'f' * 64
    run_id = repository.list_ingestion_runs()[0].ingestion_run_id

    _tamper(
        repository.path,
        'UPDATE capture_ingestion_runs SET lineage_digest=?',
        (forged,),
    )

    with pytest.raises(
        CaptureIngestionTransactionError, match='disagrees with its plan'
    ):
        repository.get_ingestion(forged)
    with pytest.raises(
        CaptureIngestionTransactionError, match='disagrees with its plan'
    ):
        repository.get_ingestion_run_plan(run_id)
    with pytest.raises(
        CaptureIngestionTransactionError, match='disagrees with its plan'
    ):
        repository.get_ingestion_run(run_id)


def test_capture_mesh_binding_handoff_drift_fails_closed(
    tmp_path: Path,
) -> None:
    repository = _ingest_fixture(tmp_path)
    with _connect(repository.path) as connection:
        binding_id = connection.execute(
            'SELECT binding_id FROM capture_raw_visual_mesh_bindings'
        ).fetchone()['binding_id']

    _tamper(
        repository.path,
        'UPDATE capture_raw_visual_mesh_bindings SET handoff_id=?',
        ('e' * 64,),
    )

    with pytest.raises(
        CaptureIngestionTransactionError, match='disagrees with its payload'
    ):
        repository.get_mesh_binding(binding_id)


def test_capture_authority_record_source_drift_fails_closed(
    tmp_path: Path,
) -> None:
    repository = _ingest_fixture(tmp_path)
    with _connect(repository.path) as connection:
        evidence_id = connection.execute(
            'SELECT source_evidence_id FROM capture_source_evidence LIMIT 1'
        ).fetchone()['source_evidence_id']
        record = CaptureAuthorityRecord(
            authority_record_handoff_id='a' * 64,
            record_kind='annotation',
            record_id='10000000-0000-4000-8000-000000000001',
            record_locator='note://fixture',
            provenance_class='user_annotation',
            source_evidence_id=evidence_id,
            source_payload_sha256='b' * 64,
        )
        repository._upsert_authority_record(connection, record)
        connection.commit()

    loaded = repository.get_authority_record(record.authority_record_handoff_id)
    assert loaded == record

    _tamper(
        repository.path,
        'UPDATE capture_authority_records SET source_evidence_id=?',
        ('c' * 64,),
    )
    with pytest.raises(
        CaptureIngestionTransactionError, match='disagrees with its payload'
    ):
        repository.get_authority_record(record.authority_record_handoff_id)


def test_capture_promotion_row_request_drift_fails_closed(
    tmp_path: Path,
) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision, _spec, _estimate = _build_search_spec(scene_repository)
    repository = CaptureIngestionRepository(scene_repository)
    plan, payloads, _manifest = support.plan_and_payloads(tmp_path)
    repository.ingest(plan, payloads)
    with _connect(scene_repository.path) as connection:
        run_id = connection.execute(
            'SELECT ingestion_run_id FROM capture_ingestion_runs'
        ).fetchone()['ingestion_run_id']
        binding_id = connection.execute(
            'SELECT binding_id FROM capture_raw_visual_mesh_bindings'
        ).fetchone()['binding_id']
        promotion_id = 'capture-semantic-promotion:' + 'd' * 64
        request = {
            'promotion_id': promotion_id,
            'ingestion_run_id': run_id,
            'raw_mesh_binding_id': binding_id,
            'source_scene_revision_id': revision.revision_id,
        }
        connection.execute(
            '''INSERT INTO capture_semantic_promotions(
                promotion_id, ingestion_run_id, raw_mesh_binding_id,
                source_scene_revision_id, scene_revision_id,
                semantic_geometry_id, request_json, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                promotion_id,
                run_id,
                binding_id,
                revision.revision_id,
                revision.revision_id,
                'geo-1',
                json.dumps(request, sort_keys=True),
                '2026-01-01T00:00:00+00:00',
            ),
        )
        connection.commit()

    promotions = CaptureSemanticPromotionRepository(
        scene_repository, repository
    )
    record = promotions.get_promotion(promotion_id)
    assert record is not None
    assert record.source_scene_revision_id == revision.revision_id

    _tamper(
        scene_repository.path,
        "UPDATE capture_semantic_promotions "
        "SET source_scene_revision_id='forged' WHERE promotion_id=?",
        (promotion_id,),
    )
    with pytest.raises(
        CapturePromotionReplayError,
        match='disagrees with its request',
    ):
        promotions.get_promotion(promotion_id)


def test_latest_eligible_rejects_payload_with_forced_gate(
    tmp_path: Path,
) -> None:
    from test_cad_model_validation_repository_full import _fixture

    record, repository, _measurement_repo = _fixture(tmp_path)
    repository.save(record)
    assert (
        repository.latest_eligible_for_search_spec(record.search_spec_id)
        is not None
    )

    payload = json.loads(record.model_dump_json())
    payload['recommendation_gate'] = 'disabled'
    _tamper(
        repository.path,
        'UPDATE cad_model_validations SET payload_json=? '
        'WHERE validation_id=?',
        (json.dumps(payload, sort_keys=True), record.validation_id),
    )

    with pytest.raises(ValueError):
        repository.latest_eligible_for_search_spec(record.search_spec_id)


def test_scan_reports_drift_and_clean_database(tmp_path: Path) -> None:
    repository = _ingest_fixture(tmp_path)
    scene_repository = SceneRepository(repository.path)
    revision, spec, _estimate = _build_search_spec(scene_repository)
    CadSearchRepository(scene_repository).save(spec)

    with _connect(repository.path) as connection:
        assert scan_native_row_integrity(connection) == ()

    _tamper(
        repository.path,
        "UPDATE scene_revisions SET document_id='forged' WHERE revision_id=?",
        (revision.revision_id,),
    )
    _tamper(
        repository.path,
        "UPDATE cad_search_specs SET scene_revision_id='forged' "
        'WHERE search_spec_id=?',
        (spec.search_spec_id,),
    )
    _tamper(
        repository.path,
        "UPDATE capture_raw_visual_mesh_bindings SET handoff_id=?",
        ('e' * 64,),
    )

    with _connect(repository.path) as connection:
        drifts = scan_native_row_integrity(connection)
        tables = {drift.table for drift in drifts}
        assert tables == {
            'scene_revisions',
            'cad_search_specs',
            'capture_raw_visual_mesh_bindings',
        }
        with pytest.raises(NativeRowIntegrityError):
            verify_native_row_integrity(connection)


def test_backup_creation_rejects_semantic_drift(tmp_path: Path) -> None:
    data_dir = tmp_path / 'data'
    repository, _first, _digest, _raw = _seed_data(data_dir)

    _tamper(
        repository.path,
        "UPDATE scene_revisions SET document_id='forged'",
    )

    with pytest.raises(
        ValueError, match='semantic integrity|disagrees|payload'
    ):
        create_backup(data_dir, tmp_path / 'backup.zip')


def test_backup_archive_validation_rejects_semantic_drift(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / 'data'
    _repository, _first, _digest, _raw = _seed_data(data_dir)
    archive = tmp_path / 'backup.zip'
    manifest = create_backup(data_dir, archive)

    # Rebuild the archive with a semantically-drifted database whose row
    # columns disagree with canonical payloads — the manifest is recomputed
    # so only the semantic scan can catch it.
    staged = tmp_path / 'staged'
    staged.mkdir()
    with ZipFile(archive) as source:
        source.extractall(staged)
    database = staged / 'cad-scenes.sqlite3'
    _tamper(
        database,
        "UPDATE scene_revisions SET document_id='forged'",
    )
    forged_files = tuple(
        entry.model_copy(update={'sha256': sha256(database.read_bytes()).hexdigest()})
        if entry.kind == 'database'
        else entry
        for entry in manifest.files
    )
    forged = manifest.model_copy(update={'files': forged_files})
    forged = forged.model_copy(
        update={'manifest_sha256': _manifest_hash(forged.identity_payload())}
    )
    forged_archive = tmp_path / 'forged.zip'
    with ZipFile(archive) as source, ZipFile(forged_archive, 'w') as target:
        for name in source.namelist():
            if name == 'manifest.json':
                continue
            member = database if name == 'cad-scenes.sqlite3' else staged / name
            target.write(member, name)
        target.writestr(
            'manifest.json', json.dumps(forged.model_dump(mode='json'))
        )

    with pytest.raises(
        ValueError, match='semantic integrity|disagrees|payload'
    ):
        validate_backup(forged_archive)


def test_row_integrity_registry_complete() -> None:
    """#763: every canonical-payload table is bound or audited — CI gate."""

    assert_row_integrity_registry_complete()
    # Sanity: the registry actually covers the DDL it claims to cover.
    assert 'scene_revisions' in canonical_payload_tables()


def test_family_binding_drift_detected(tmp_path: Path) -> None:
    """#763: a drifted duplicated column in a newly-bound family surfaces."""

    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    preset_payload = {
        'preset_id': 'preset-1',
        'document_id': 'doc-1',
        'category': 'playback',
        'preset_sha256': 'a' * 64,
        'created_at_utc': '2026-01-01T00:00:00+00:00',
    }
    with _connect(repository.path) as connection:
        connection.execute(
            '''INSERT INTO cad_operating_presets(
                preset_id, document_id, category, preset_sha256,
                created_at_utc, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)''',
            (
                preset_payload['preset_id'],
                preset_payload['document_id'],
                preset_payload['category'],
                preset_payload['preset_sha256'],
                preset_payload['created_at_utc'],
                json.dumps(preset_payload, sort_keys=True),
            ),
        )
        connection.commit()
        assert scan_native_row_integrity(connection) == ()

    _tamper(
        repository.path,
        "UPDATE cad_operating_presets SET preset_sha256='forged' "
        'WHERE preset_id=?',
        ('preset-1',),
    )
    with _connect(repository.path) as connection:
        drifts = scan_native_row_integrity(connection)
        assert [drift.column for drift in drifts] == ['preset_sha256']


def test_unbound_ledger_payloads_are_still_parsed(tmp_path: Path) -> None:
    """#313: ledger membership is inventory — unbound payloads are parsed."""

    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    with _connect(repository.path) as connection:
        connection.execute(
            '''INSERT INTO editor_camera_states(
                document_id, payload_json, updated_at_utc
            ) VALUES (?, ?, ?)''',
            ('doc-1', '{"camera": {"zoom": 1}}', '2026-01-01T00:00:00+00:00'),
        )
        connection.commit()
        assert scan_native_row_integrity(connection) == ()

    _tamper(
        repository.path,
        "UPDATE editor_camera_states SET payload_json='{corrupt'",
    )
    with _connect(repository.path) as connection:
        drifts = scan_native_row_integrity(connection)
        assert [drift.table for drift in drifts] == ['editor_camera_states']
        assert drifts[0].column == 'payload_json'
        with pytest.raises(NativeRowIntegrityError):
            verify_native_row_integrity(connection)


def _insert(connection: sqlite3.Connection, table: str, row: dict) -> None:
    columns = ', '.join(row)
    placeholders = ', '.join('?' for _ in row)
    connection.execute(
        f'INSERT INTO {table}({columns}) VALUES ({placeholders})',
        tuple(row.values()),
    )


def _convergence_rows() -> dict[str, dict]:
    """One coherent row per convergence/migration-installed payload table."""

    observation = {
        'observation_id': 'obs-1',
        'extended_search_id': 'xs-1',
        'candidate_id': 'cand-1',
        'objective_id': 'obj-1',
        'observation_sha256': 'a' * 64,
        'supersedes_observation_sha256': None,
        'created_at_utc': '2026-01-01T00:00:00+00:00',
    }
    plan = {
        'plan_id': 'plan-1',
        'document_id': 'doc-1',
        'extended_search_id': 'xs-1',
        'validation_id': 'val-1',
        'execution_scope': 'scope-1',
        'selected_candidate_id': 'cand-1',
        'adaptive_extended_sha256': 'b' * 64,
        'created_at_utc': '2026-01-01T00:00:00+00:00',
    }
    roomplan_record = {
        'kind': 'floorplan',
        'source_evidence_id': 'ev-1',
        'path': 'captures/r1/floorplan.json',
        'payload_sha256': 'c' * 64,
        'provenance_class': 'file',
        'source_refs': [],
        'roomplan_capture_metadata': None,
    }
    physical_model = {
        'physical_space_model_id': 'psm-1',
        'document_id': 'doc-1',
        'revision': 1,
        'parent_model_id': None,
        'source_connected_document_id': 'capture-connected-space:abc',
        'world_to_scene_authority_id': 'w2s-1',
        'created_at_utc': '2026-01-01T00:00:00+00:00',
        'reason': 'initial',
    }
    repair_bundle = {
        'repaired_mesh': {
            'repaired_mesh_id': 'rm-1',
            'semantic_hash_sha256': 'd' * 64,
        },
        'source_raw_mesh': {'mesh_id': 'raw-1', 'provenance': 'capture'},
        'repair_plan': {
            'plan_id': 'rp-1',
            'semantic_hash_sha256': 'e' * 64,
        },
        'post_repair_diagnostic': {
            'diagnostic_id': 'diag-1',
            'semantic_hash_sha256': 'f' * 64,
        },
        'raw_mesh_semantic_hash': '0' * 64,
        'created_at_utc': '2026-01-01T00:00:00+00:00',
    }
    return {
        'cad_adaptive_extended_observations': {
            **observation,
            'payload_json': json.dumps(observation, sort_keys=True),
        },
        'cad_adaptive_extended_plans': {
            **plan,
            'payload_json': json.dumps(plan, sort_keys=True),
        },
        'capture_roomplan_records': {
            'ingestion_run_id': 'run-1',
            'kind': roomplan_record['kind'],
            'source_evidence_id': roomplan_record['source_evidence_id'],
            'payload_json': json.dumps(roomplan_record, sort_keys=True),
        },
        'physical_space_models': {
            **physical_model,
            'payload_json': json.dumps(physical_model, sort_keys=True),
        },
        'cad_raw_mesh_repair_bundles': {
            'repaired_mesh_id': 'rm-1',
            'repaired_mesh_semantic_hash': 'd' * 64,
            'raw_mesh_id': 'raw-1',
            'raw_mesh_semantic_hash': '0' * 64,
            'repair_plan_id': 'rp-1',
            'repair_plan_semantic_hash': 'e' * 64,
            'post_diagnostic_id': 'diag-1',
            'post_diagnostic_semantic_hash': 'f' * 64,
            'created_at_utc': '2026-01-01T00:00:00+00:00',
            'payload_json': json.dumps(repair_bundle, sort_keys=True),
        },
    }


def test_convergence_installed_table_drift_fails_closed(
    tmp_path: Path,
) -> None:
    """REV24: payload tables installed outside NATIVE_BASELINE_DDL are bound.

    The completeness invariant used to derive its declared set from the
    baseline DDL only, so these six tables escaped both registries — a
    tampered duplicated column raised nothing.
    """

    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    rows = _convergence_rows()
    with _connect(repository.path) as connection:
        for table, row in rows.items():
            _insert(connection, table, row)
        connection.commit()
        assert scan_native_row_integrity(connection) == ()

    tampered = (
        ('cad_adaptive_extended_observations', 'candidate_id', "'forged'"),
        ('cad_adaptive_extended_plans', 'selected_candidate_id', "'forged'"),
        ('capture_roomplan_records', 'source_evidence_id', "'forged'"),
        ('physical_space_models', 'source_connected_document_id', "'x'"),
        ('cad_raw_mesh_repair_bundles', 'raw_mesh_id', "'forged'"),
    )
    for table, column, value in tampered:
        _tamper(repository.path, f'UPDATE {table} SET {column}={value}')
    with _connect(repository.path) as connection:
        drifts = scan_native_row_integrity(connection)
        assert {
            (drift.table, drift.column) for drift in drifts
        } == {(table, column) for table, column, _v in tampered}
        with pytest.raises(NativeRowIntegrityError):
            verify_native_row_integrity(connection)


def test_connected_space_document_identity_drift_fails_closed(
    tmp_path: Path,
) -> None:
    """REV24: connected_document_id re-derives from the stored payload."""

    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    payload = {'model': {'spaces': []}, 'schema': 'v1'}
    document_id = 'capture-connected-space:' + canonical_sha256(
        {'domain': CONNECTED_DOC_DOMAIN, 'payload': payload}
    )
    with _connect(repository.path) as connection:
        _insert(
            connection,
            'capture_connected_space_documents',
            {
                'connected_document_id': document_id,
                'lineage_digest': 'a' * 64,
                'document_sha256': 'b' * 64,
                'payload_json': json.dumps(payload, sort_keys=True),
                'staged_at_utc': '2026-01-01T00:00:00+00:00',
            },
        )
        connection.commit()
        assert scan_native_row_integrity(connection) == ()

    _tamper(
        repository.path,
        "UPDATE capture_connected_space_documents "
        "SET connected_document_id='capture-connected-space:forged'",
    )
    with _connect(repository.path) as connection:
        drifts = scan_native_row_integrity(connection)
        assert [drift.column for drift in drifts] == ['connected_document_id']


def test_scan_flags_unregistered_live_payload_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REV24: a declared payload table missing from registries is drift.

    Row integrity must fail closed on coverage gaps the way it does on
    column drift — a future canonical-payload table that lands without a
    registry entry surfaces immediately instead of scanning nothing.
    Foreign (non-declared) tables stay the authority audit's problem.
    """

    import htdt.native_row_integrity as integrity

    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    with _connect(repository.path) as connection:
        connection.execute(
            'CREATE TABLE htdt_gap_probe(payload_json TEXT NOT NULL)'
        )
        connection.execute(
            "INSERT INTO htdt_gap_probe(payload_json) VALUES ('{}')"
        )
        connection.commit()
        # Foreign tables never declared in NATIVE_SCHEMA_TABLES are the
        # audit's unclassified-table domain, not drift here.
        assert scan_native_row_integrity(connection) == ()

        monkeypatch.setattr(
            integrity,
            'NATIVE_SCHEMA_TABLES',
            tuple(list(integrity.NATIVE_SCHEMA_TABLES) + ['htdt_gap_probe']),
        )
        drifts = scan_native_row_integrity(connection)
        assert [
            (drift.table, drift.column) for drift in drifts
        ] == [('htdt_gap_probe', '<registry coverage>')]
