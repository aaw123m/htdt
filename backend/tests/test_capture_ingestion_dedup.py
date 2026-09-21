from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import struct
import tracemalloc

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_schema import read_native_schema_version
from htdt.capture_ingestion_transaction import (
    CaptureIngestionBudget,
    CaptureIngestionPlan,
    CaptureIngestionRepository,
    CaptureIngestionTransactionError,
)
from htdt.capture_mesh_ingestion import adapt_capture_mesh_handoff
from htdt.native_backup import create_backup, restore_backup, validate_backup
from htdt.raw_mesh import diagnose_raw_visual_mesh, import_raw_visual_mesh
from htdt.raw_mesh_repair import (
    CorrectConsistentWinding,
    RawMeshRepairBundle,
    RawMeshRepairRepository,
    apply_raw_mesh_repair,
    diagnose_repaired_raw_mesh,
    make_raw_mesh_repair_plan,
    serialize_raw_mesh_repair_bundle,
)


BUNDLE_DIGEST = '2' * 64
SERIES_ID = '10000000-0000-4000-8000-000000000001'
REVISION_ID = '10000000-0000-4000-8000-000000000002'
SESSION_ID = '10000000-0000-4000-8000-000000000003'
SPACE_ID = '10000000-0000-4000-8000-000000000004'
ANCHOR_ID = '10000000-0000-4000-8000-000000000005'
INGESTOR_CONFIG = (
    '3e27eec298714a04fc6b48d94b354168396e2c4eea0cf9aa8284fa552de562b3'
)


def _hash_parts(prefix: str, *parts: str) -> str:
    digest = sha256(prefix.encode('utf-8'))
    for part in parts:
        digest.update(b'\x00')
        digest.update(part.encode('utf-8'))
    return digest.hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _meshbin(vertex_count: int = 3, face_count: int = 1) -> bytes:
    vertices = b''.join(
        struct.pack(
            '<3f',
            float(index),
            float(index % 7) * 0.5,
            float(index % 3) * 0.25,
        )
        for index in range(vertex_count)
    )
    faces = b''.join(
        struct.pack(
            '<3I',
            index % vertex_count,
            (index + 1) % vertex_count,
            (index + 2) % vertex_count,
        )
        for index in range(face_count)
    )
    header = (
        b'HTDTMSH1'
        + struct.pack('<HH', 1, 0)
        + struct.pack('<I', 32)
        + struct.pack('<I', vertex_count)
        + struct.pack('<I', face_count)
        + bytes([4, 0])
        + struct.pack('<H', 0)
        + struct.pack('<I', 0)
    )
    return header + vertices + faces


def _plan_and_payloads(
    payloads: dict[str, bytes] | None = None,
    mesh_specs: tuple[tuple[str, str, int, int], ...] | None = None,
    bundle_digest: str = BUNDLE_DIGEST,
) -> tuple[dict, dict[str, bytes]]:
    """Build a validated ingestion plan fixture.

    mesh_specs entries are (anchor_id, geometry_path, vertex_count,
    face_count); the referenced payload must already exist in payloads.
    """

    if payloads is None:
        payloads = {
            'mesh/anchors.json': b'{"fixture":"anchors"}',
            f'mesh/geometry/{ANCHOR_ID}.meshbin': _meshbin(),
        }
    if mesh_specs is None:
        mesh_specs = ((ANCHOR_ID, f'mesh/geometry/{ANCHOR_ID}.meshbin', 3, 1),)

    source = []
    source_by_path = {}
    for path in sorted(payloads):
        payload = payloads[path]
        digest = sha256(payload).hexdigest()
        record = {
            'source_evidence_id': _hash_parts(
                'htdt.capture.source-evidence.v1',
                bundle_digest,
                path,
                digest,
            ),
            'bundle_digest': bundle_digest,
            'capture_revision_id': REVISION_ID,
            'path': path,
            'payload_sha256': digest,
            'bytes': len(payload),
            'media_type': (
                'application/vnd.htdt.meshbin'
                if path.endswith('.meshbin')
                else 'application/json'
            ),
            'producer': 'mesh_capture',
            'provenance_class': 'arkit_mesh_reconstruction',
            'role': 'canonical',
            'source_refs': [],
        }
        source.append(record)
        source_by_path[path] = record

    handoffs = []
    for anchor_id, geometry_path, vertex_count, face_count in mesh_specs:
        geometry = source_by_path[geometry_path]
        handoffs.append(
            {
                'raw_visual_mesh_handoff_id': _hash_parts(
                    'htdt.capture.raw-visual-mesh-handoff.v1',
                    bundle_digest,
                    anchor_id,
                    geometry['payload_sha256'],
                ),
                'bundle_digest': bundle_digest,
                'anchor_id': anchor_id,
                'anchor_record_locator': (
                    f'mesh/anchors.json#anchor:{anchor_id}'
                ),
                'anchor_index_source_evidence_id': source_by_path[
                    'mesh/anchors.json'
                ]['source_evidence_id'],
                'geometry_source_evidence_id': geometry['source_evidence_id'],
                'geometry_path': geometry_path,
                'geometry_sha256': geometry['payload_sha256'],
                'capture_session_id': SESSION_ID,
                'coordinate_space_id': SPACE_ID,
                'T_world_from_mesh_anchor': {
                    'representation': 'column_major_4x4_f32',
                    'values': [
                        1, 0, 0, 0,
                        0, 1, 0, 0,
                        0, 0, 1, 0,
                        0, 0, 0, 1,
                    ],
                },
                'session_timestamp_s': 1.0,
                'vertex_count': vertex_count,
                'face_count': face_count,
            }
        )

    projection = {
        'bundle_digest': bundle_digest,
        'source_evidence_ids': sorted(
            item['source_evidence_id'] for item in source
        ),
        'raw_visual_mesh_ids': sorted(
            handoff['raw_visual_mesh_handoff_id'] for handoff in handoffs
        ),
        'authority_record_ids': [],
    }
    lineage_digest = sha256(
        json.dumps(
            projection,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
    ).hexdigest()

    plan = {
        'schema': 'htdt.capture.ingestion-plan',
        'schema_version': '1.0.0',
        'ingestor': {
            'name': 'htdt-capture-reference-ingestor',
            'version': '1.0.0',
            'configuration_digest': INGESTOR_CONFIG,
        },
        'bundle': {
            'bundle_digest': bundle_digest,
            'capture_schema': 'htdt.capture.bundle',
            'capture_schema_version': '1.0.0',
            'capture_series_id': SERIES_ID,
            'capture_revision_id': REVISION_ID,
            'parent_revision_id': None,
            'capture_session_ids': [SESSION_ID],
            'coordinate_space_ids': [SPACE_ID],
        },
        'source_evidence': source,
        'roomplan_records': [],
        'raw_visual_mesh_handoffs': handoffs,
        'authority_records': [],
        'lineage_digest': lineage_digest,
    }
    return plan, payloads


def _repository(tmp_path: Path) -> CaptureIngestionRepository:
    return CaptureIngestionRepository(SceneRepository(tmp_path / 'cad.sqlite3'))


def _query(path: Path, sql: str, args: tuple = ()):
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(sql, args).fetchall()


def _binding_row(path: Path, binding_id: str | None = None):
    if binding_id is None:
        rows = _query(
            path,
            'SELECT binding_id, payload_json FROM capture_raw_visual_mesh_bindings',
        )
        assert len(rows) == 1
        return rows[0]
    rows = _query(
        path,
        'SELECT binding_id, payload_json FROM capture_raw_visual_mesh_bindings '
        'WHERE binding_id=?',
        (binding_id,),
    )
    assert len(rows) == 1
    return rows[0]


def test_geometry_payload_is_stored_once_in_content_addressed_store(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    plan, payloads = _plan_and_payloads()
    typed = CaptureIngestionPlan.model_validate(plan)

    result = repository.ingest(plan, payloads)
    assert result.created

    geometry_sha = sha256(
        payloads[f'mesh/geometry/{ANCHOR_ID}.meshbin']
    ).hexdigest()
    blob_rows = _query(
        repository.path,
        'SELECT payload_sha256, payload_blob FROM htdt_content_blobs',
    )
    assert {row['payload_sha256'] for row in blob_rows} == {
        sha256(payload).hexdigest() for payload in payloads.values()
    }
    geometry_blob = next(
        row for row in blob_rows if row['payload_sha256'] == geometry_sha
    )
    assert bytes(geometry_blob['payload_blob']) == payloads[
        f'mesh/geometry/{ANCHOR_ID}.meshbin'
    ]

    # The evidence row is the metadata authority; its inline blob stays empty
    # because the raw bytes now live exactly once in the blob store.
    inline_lengths = _query(
        repository.path,
        'SELECT length(payload_blob) AS n FROM capture_source_evidence',
    )
    assert [row['n'] for row in inline_lengths] == [0] * len(payloads)

    # The binding record references the handoff + canonical hash only.
    binding_row = _binding_row(repository.path)
    record = json.loads(binding_row['payload_json'])
    assert 'raw_mesh' not in record
    assert 'original_asset_base64' not in binding_row['payload_json']
    assert record['schema'] == 'htdt.capture.raw-visual-mesh-binding-record'
    assert record['handoff']['geometry_sha256'] == geometry_sha

    reopened = repository.get_mesh_binding(binding_row['binding_id'])
    handoff = typed.raw_visual_mesh_handoffs[0]
    assert reopened == adapt_capture_mesh_handoff(
        handoff,
        payloads[handoff.geometry_path],
    )
    assert reopened.raw_mesh.original_asset_bytes() == payloads[
        handoff.geometry_path
    ]
    assert reopened.raw_mesh.provenance.original_asset_sha256 == geometry_sha


def test_identical_payload_under_two_paths_stores_one_canonical_blob(
    tmp_path: Path,
) -> None:
    shared = b'{"fixture":"shared-payload"}'
    payloads = {
        'mesh/anchors.json': b'{"fixture":"anchors"}',
        f'mesh/geometry/{ANCHOR_ID}.meshbin': _meshbin(),
        'derived/a.json': shared,
        'derived/b.json': shared,
    }
    repository = _repository(tmp_path)
    plan, payloads = _plan_and_payloads(payloads)

    repository.ingest(plan, payloads)

    shared_rows = _query(
        repository.path,
        'SELECT payload_blob FROM htdt_content_blobs WHERE payload_sha256=?',
        (sha256(shared).hexdigest(),),
    )
    assert len(shared_rows) == 1
    assert bytes(shared_rows[0]['payload_blob']) == shared
    assert repository.source_evidence_count() == 4


def test_reingest_deduplicates_across_ingestions(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    plan, payloads = _plan_and_payloads()
    repository.ingest(plan, payloads)
    blob_count = _query(
        repository.path,
        'SELECT COUNT(*) AS n FROM htdt_content_blobs',
    )[0]['n']

    second = repository.ingest(plan, payloads)

    assert not second.created
    assert (
        _query(
            repository.path,
            'SELECT COUNT(*) AS n FROM htdt_content_blobs',
        )[0]['n']
        == blob_count
    )

    # A different bundle carrying the same payload bytes still reuses the
    # canonical blob rows: content addressing deduplicates across ingestions.
    plan2, payloads2 = _plan_and_payloads(
        dict(payloads),
        bundle_digest='7' * 64,
    )
    third = repository.ingest(plan2, payloads2)
    assert third.created
    assert (
        _query(
            repository.path,
            'SELECT COUNT(*) AS n FROM htdt_content_blobs',
        )[0]['n']
        == blob_count
    )


def test_budget_rejects_each_aggregate_dimension(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    plan, payloads = _plan_and_payloads()
    typed = CaptureIngestionPlan.model_validate(plan)
    total_bytes = sum(len(payload) for payload in payloads.values())

    cases = [
        CaptureIngestionBudget(max_source_evidence_count=1),
        CaptureIngestionBudget(max_source_payload_bytes=total_bytes - 1),
        CaptureIngestionBudget(max_mesh_count=0),
        CaptureIngestionBudget(max_vertex_count=2),
        CaptureIngestionBudget(max_face_count=0),
        CaptureIngestionBudget(max_working_bytes=total_bytes),
    ]
    for budget in cases:
        with pytest.raises(
            CaptureIngestionTransactionError,
            match='capture ingestion budget exceeded',
        ):
            repository.ingest(typed, payloads, budget=budget)
        assert repository.source_evidence_count() == 0
        assert repository.get_ingestion(typed.lineage_digest) is None

    boundary = CaptureIngestionBudget(
        max_source_evidence_count=len(typed.source_evidence),
        max_source_payload_bytes=total_bytes,
        max_mesh_count=1,
        max_vertex_count=3,
        max_face_count=1,
        max_working_bytes=total_bytes + 80 * 4 + 3 * 640 + 1 * 640,
    )
    assert repository.ingest(typed, payloads, budget=boundary).created


def test_oversized_declared_vertex_count_fails_before_mesh_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    plan, payloads = _plan_and_payloads()
    # The handoff identity does not cover declared counts, so an adversarial
    # manifest can claim billions of vertices against a tiny payload.
    plan['raw_visual_mesh_handoffs'][0]['vertex_count'] = 4_000_000_000
    typed = CaptureIngestionPlan.model_validate(plan)

    import htdt.capture_ingestion_transaction as transaction

    def _forbidden(*args, **kwargs):
        raise AssertionError('mesh decode must not be reached')

    monkeypatch.setattr(
        transaction,
        'adapt_capture_mesh_handoff',
        _forbidden,
    )

    with pytest.raises(
        CaptureIngestionTransactionError,
        match='declared vertex count',
    ):
        repository.ingest(typed, payloads)

    assert repository.source_evidence_count() == 0
    assert repository.get_ingestion(typed.lineage_digest) is None


def test_large_mesh_persisted_size_and_ingest_memory_are_bounded(
    tmp_path: Path,
) -> None:
    vertex_count, face_count = 40_000, 40_000
    asset = _meshbin(vertex_count, face_count)
    payloads = {
        'mesh/anchors.json': b'{"fixture":"anchors"}',
        f'mesh/geometry/{ANCHOR_ID}.meshbin': asset,
    }
    repository = _repository(tmp_path)
    plan, payloads = _plan_and_payloads(
        payloads,
        ((ANCHOR_ID, f'mesh/geometry/{ANCHOR_ID}.meshbin', vertex_count, face_count),),
    )
    typed = CaptureIngestionPlan.model_validate(plan)
    handoff = typed.raw_visual_mesh_handoffs[0]

    # What the pre-dedup persisted record looked like: the whole mesh dump,
    # including the raw bytes embedded as Base64 plus expanded arrays.
    legacy_binding = adapt_capture_mesh_handoff(handoff, asset)
    legacy_payload_json = _canonical_json(
        legacy_binding.model_dump(mode='json', by_alias=True)
    )

    tracemalloc.start()
    reference = adapt_capture_mesh_handoff(handoff, asset)
    _, import_peak = tracemalloc.get_traced_memory()
    del reference
    tracemalloc.stop()

    tracemalloc.start()
    result = repository.ingest(plan, payloads)
    _, ingest_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert result.created

    binding_row = _binding_row(repository.path)
    # The compact record is a small constant-size handoff regardless of mesh.
    assert len(binding_row['payload_json']) < 4096
    assert len(binding_row['payload_json']) * 100 < len(legacy_payload_json)

    # The database persists the raw payload exactly once: file size tracks the
    # payload itself, not the ~7x old JSON/Base64 expansion.
    database_bytes = repository.path.stat().st_size
    assert database_bytes < len(asset) * 1.5
    assert database_bytes < len(legacy_payload_json) // 4

    # Peak ingest memory stays on the order of one decoded mesh plus the
    # caller-held payload bytes; no extra serialized copies are materialized.
    total_payload_bytes = sum(len(payload) for payload in payloads.values())
    assert ingest_peak <= import_peak * 2 + total_payload_bytes

    reopened = repository.get_mesh_binding(binding_row['binding_id'])
    assert reopened == legacy_binding


def test_missing_content_blob_fails_closed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    plan, payloads = _plan_and_payloads()
    typed = CaptureIngestionPlan.model_validate(plan)
    repository.ingest(plan, payloads)
    geometry_source = next(
        item for item in typed.source_evidence
        if item.path.endswith('.meshbin')
    )

    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute('DELETE FROM htdt_content_blobs')

    with pytest.raises(
        CaptureIngestionTransactionError,
        match='content blob store|missing or inconsistent',
    ):
        repository.get_source_evidence(geometry_source.source_evidence_id)
    binding_id = repository.mesh_binding_ids_for_ingestion(
        typed.lineage_digest
    )[0]
    with pytest.raises(
        CaptureIngestionTransactionError,
        match='missing or inconsistent',
    ):
        repository.get_mesh_binding(binding_id)


def test_legacy_inline_evidence_row_reads_losslessly(tmp_path: Path) -> None:
    """Rows that still carry inline payload bytes remain readable as-is."""
    repository = _repository(tmp_path)
    payload = b'{"fixture":"legacy-inline"}'
    digest = sha256(payload).hexdigest()
    evidence_id = _hash_parts(
        'htdt.capture.source-evidence.v1',
        BUNDLE_DIGEST,
        'legacy/payload.json',
        digest,
    )
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            '''
            INSERT INTO capture_source_evidence(
                source_evidence_id, bundle_digest, capture_revision_id,
                logical_path, payload_sha256, byte_count, media_type,
                producer, provenance_class, role, source_refs_json,
                payload_blob
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                evidence_id,
                BUNDLE_DIGEST,
                REVISION_ID,
                'legacy/payload.json',
                digest,
                len(payload),
                'application/json',
                'legacy_fixture',
                'imported_reference',
                'canonical',
                '[]',
                payload,
            ),
        )

    persisted = repository.get_source_evidence(evidence_id)
    assert persisted is not None
    assert persisted.payload == payload
    assert persisted.record.payload_sha256 == digest


def test_legacy_embedded_mesh_binding_row_reads_losslessly(
    tmp_path: Path,
) -> None:
    """Pre-dedup binding rows embedded the whole mesh; they still read."""
    repository = _repository(tmp_path)
    plan, payloads = _plan_and_payloads()
    typed = CaptureIngestionPlan.model_validate(plan)
    handoff = typed.raw_visual_mesh_handoffs[0]
    binding = adapt_capture_mesh_handoff(
        handoff,
        payloads[handoff.geometry_path],
    )
    legacy_payload_json = _canonical_json(
        binding.model_dump(mode='json', by_alias=True)
    )
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            '''
            INSERT INTO capture_raw_visual_mesh_bindings(
                binding_id, handoff_id, payload_json
            ) VALUES (?, ?, ?)
            ''',
            (
                binding.binding_id,
                handoff.raw_visual_mesh_handoff_id,
                legacy_payload_json,
            ),
        )

    reopened = repository.get_mesh_binding(binding.binding_id)
    assert reopened == binding
    assert reopened.raw_mesh.original_asset_bytes() == payloads[
        handoff.geometry_path
    ]


def _legacy_database(path: Path, plan: dict, payloads: dict[str, bytes]) -> None:
    """Materialize a v3-era database with pre-dedup capture rows.

    Rows are written exactly as the pre-migration implementation persisted
    them: evidence payloads inline in payload_blob and mesh bindings as a
    full serialized binding that embeds the source bytes as Base64 plus
    expanded vertex/face arrays.
    """

    typed = CaptureIngestionPlan.model_validate(plan)
    plan_json = _canonical_json(typed.model_dump(mode='json', by_alias=True))
    binding = adapt_capture_mesh_handoff(
        typed.raw_visual_mesh_handoffs[0],
        payloads[typed.raw_visual_mesh_handoffs[0].geometry_path],
    )
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute('PRAGMA foreign_keys=ON')
        connection.executescript(
            '''
            CREATE TABLE native_schema_metadata (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                schema_version INTEGER NOT NULL
            );
            INSERT INTO native_schema_metadata VALUES (1, 3);
            CREATE TABLE native_schema_migrations (
                schema_version INTEGER PRIMARY KEY,
                applied_at_utc TEXT NOT NULL,
                description TEXT NOT NULL
            );
            INSERT INTO native_schema_migrations VALUES
                (1, '2026-09-20T00:00:00+00:00', 'baseline'),
                (2, '2026-09-20T00:00:00+00:00', 'v2'),
                (3, '2026-09-20T00:00:00+00:00', 'v3');
            CREATE TABLE capture_ingestion_runs (
                lineage_digest TEXT PRIMARY KEY,
                bundle_digest TEXT NOT NULL,
                capture_revision_id TEXT NOT NULL,
                ingestor_name TEXT NOT NULL,
                ingestor_version TEXT NOT NULL,
                configuration_digest TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                recorded_at_utc TEXT NOT NULL
            );
            CREATE TABLE capture_source_evidence (
                source_evidence_id TEXT PRIMARY KEY,
                bundle_digest TEXT NOT NULL,
                capture_revision_id TEXT NOT NULL,
                logical_path TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                byte_count INTEGER NOT NULL,
                media_type TEXT NOT NULL,
                producer TEXT NOT NULL,
                provenance_class TEXT NOT NULL,
                role TEXT NOT NULL,
                source_refs_json TEXT NOT NULL,
                payload_blob BLOB NOT NULL,
                UNIQUE(bundle_digest, logical_path)
            );
            CREATE TABLE capture_ingestion_source_links (
                lineage_digest TEXT NOT NULL,
                source_evidence_id TEXT NOT NULL,
                PRIMARY KEY(lineage_digest, source_evidence_id)
            );
            CREATE TABLE capture_roomplan_records (
                lineage_digest TEXT NOT NULL,
                kind TEXT NOT NULL,
                source_evidence_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(lineage_digest, kind, source_evidence_id)
            );
            CREATE TABLE capture_raw_visual_mesh_bindings (
                binding_id TEXT PRIMARY KEY,
                handoff_id TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE capture_ingestion_mesh_links (
                lineage_digest TEXT NOT NULL,
                binding_id TEXT NOT NULL,
                PRIMARY KEY(lineage_digest, binding_id)
            );
            CREATE TABLE capture_authority_records (
                authority_record_handoff_id TEXT PRIMARY KEY,
                source_evidence_id TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE capture_ingestion_authority_links (
                lineage_digest TEXT NOT NULL,
                authority_record_handoff_id TEXT NOT NULL,
                PRIMARY KEY(lineage_digest, authority_record_handoff_id)
            );
            '''
        )
        connection.execute(
            '''
            INSERT INTO capture_ingestion_runs(
                lineage_digest, bundle_digest, capture_revision_id,
                ingestor_name, ingestor_version, configuration_digest,
                plan_json, recorded_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                typed.lineage_digest,
                typed.bundle.bundle_digest,
                typed.bundle.capture_revision_id,
                typed.ingestor.name,
                typed.ingestor.version,
                typed.ingestor.configuration_digest,
                plan_json,
                '2026-09-20T00:00:00+00:00',
            ),
        )
        for record in typed.source_evidence:
            connection.execute(
                '''
                INSERT INTO capture_source_evidence(
                    source_evidence_id, bundle_digest, capture_revision_id,
                    logical_path, payload_sha256, byte_count, media_type,
                    producer, provenance_class, role, source_refs_json,
                    payload_blob
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    record.source_evidence_id,
                    record.bundle_digest,
                    record.capture_revision_id,
                    record.path,
                    record.payload_sha256,
                    record.bytes,
                    record.media_type,
                    record.producer,
                    record.provenance_class,
                    record.role,
                    _canonical_json(list(record.source_refs)),
                    payloads[record.path],
                ),
            )
            connection.execute(
                '''
                INSERT INTO capture_ingestion_source_links(
                    lineage_digest, source_evidence_id
                ) VALUES (?, ?)
                ''',
                (typed.lineage_digest, record.source_evidence_id),
            )
        connection.execute(
            '''
            INSERT INTO capture_raw_visual_mesh_bindings(
                binding_id, handoff_id, payload_json
            ) VALUES (?, ?, ?)
            ''',
            (
                binding.binding_id,
                binding.handoff.raw_visual_mesh_handoff_id,
                _canonical_json(binding.model_dump(mode='json', by_alias=True)),
            ),
        )
        connection.execute(
            '''
            INSERT INTO capture_ingestion_mesh_links(
                lineage_digest, binding_id
            ) VALUES (?, ?)
            ''',
            (typed.lineage_digest, binding.binding_id),
        )


def test_v3_database_migrates_capture_evidence_losslessly(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'cad.sqlite3'
    vertex_count, face_count = 2_000, 2_000
    asset = _meshbin(vertex_count, face_count)
    payloads = {
        'mesh/anchors.json': b'{"fixture":"anchors"}',
        f'mesh/geometry/{ANCHOR_ID}.meshbin': asset,
    }
    plan, payloads = _plan_and_payloads(
        payloads,
        ((ANCHOR_ID, f'mesh/geometry/{ANCHOR_ID}.meshbin', vertex_count, face_count),),
    )
    _legacy_database(path, plan, payloads)
    typed = CaptureIngestionPlan.model_validate(plan)
    legacy_size = path.stat().st_size

    repository = CaptureIngestionRepository(SceneRepository(path))

    assert read_native_schema_version(path) == 4
    # Inline payloads were externalized exactly once into the blob store.
    inline_lengths = _query(
        path,
        'SELECT length(payload_blob) AS n FROM capture_source_evidence',
    )
    assert [row['n'] for row in inline_lengths] == [0] * len(payloads)
    blob_rows = _query(path, 'SELECT payload_sha256 FROM htdt_content_blobs')
    assert {row['payload_sha256'] for row in blob_rows} == {
        sha256(payload).hexdigest() for payload in payloads.values()
    }
    total_blob_bytes = _query(
        path,
        'SELECT SUM(length(payload_blob)) AS n FROM htdt_content_blobs',
    )[0]['n']
    assert total_blob_bytes == sum(len(payload) for payload in payloads.values())

    for record in typed.source_evidence:
        persisted = repository.get_source_evidence(record.source_evidence_id)
        assert persisted is not None
        assert persisted.record == record
        assert persisted.payload == payloads[record.path]

    binding_id = repository.mesh_binding_ids_for_ingestion(
        typed.lineage_digest
    )[0]
    binding = repository.get_mesh_binding(binding_id)
    handoff = typed.raw_visual_mesh_handoffs[0]
    assert binding is not None
    assert binding.raw_mesh.original_asset_bytes() == payloads[
        handoff.geometry_path
    ]
    assert binding == adapt_capture_mesh_handoff(
        handoff,
        payloads[handoff.geometry_path],
    )

    # The legacy embedded-mesh row was compacted to a hash-referencing record.
    binding_row = _binding_row(path, binding_id)
    assert 'original_asset_base64' not in binding_row['payload_json']
    assert len(binding_row['payload_json']) < 4096

    # Migration is idempotent for ingestion and frees the duplicated pages;
    # VACUUM then yields a materially smaller database than the legacy form.
    assert not repository.ingest(plan, payloads).created
    freelist = _query(path, 'PRAGMA freelist_count')[0][0]
    assert freelist > 0
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute('VACUUM')
    assert path.stat().st_size * 2 < legacy_size


def test_backup_restore_round_trip_preserves_capture_provenance(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / 'data'
    repository = CaptureIngestionRepository(
        SceneRepository(data_dir / 'cad-scenes.sqlite3')
    )
    plan, payloads = _plan_and_payloads()
    typed = CaptureIngestionPlan.model_validate(plan)
    repository.ingest(plan, payloads)

    backup_path = tmp_path / 'capture.htdt-backup'
    manifest = create_backup(data_dir, backup_path)
    assert {entry.path for entry in manifest.files} == {'cad-scenes.sqlite3'}
    assert validate_backup(backup_path) == manifest

    restored_dir = tmp_path / 'restored'
    restored, pre_restore = restore_backup(restored_dir, backup_path)
    assert restored == manifest
    assert pre_restore is None

    reopened = CaptureIngestionRepository(
        SceneRepository(restored_dir / 'cad-scenes.sqlite3')
    )
    for record in typed.source_evidence:
        persisted = reopened.get_source_evidence(record.source_evidence_id)
        assert persisted is not None
        assert persisted.record == record
        assert persisted.payload == payloads[record.path]
        assert (
            sha256(persisted.payload).hexdigest() == record.payload_sha256
        )

    binding_id = reopened.mesh_binding_ids_for_ingestion(
        typed.lineage_digest
    )[0]
    binding = reopened.get_mesh_binding(binding_id)
    handoff = typed.raw_visual_mesh_handoffs[0]
    assert binding is not None
    assert binding.handoff == handoff
    assert binding.raw_mesh.original_asset_bytes() == payloads[
        handoff.geometry_path
    ]
    assert (
        binding.raw_mesh.provenance.original_asset_sha256
        == sha256(payloads[handoff.geometry_path]).hexdigest()
    )


def _repair_bundle():
    mesh = import_raw_visual_mesh(
        b'v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 0\nf 1 2 3\nf 4 2 3\nf 1 2 3\n',
        source_name='duplicates.obj',
    )
    diagnostic = diagnose_raw_visual_mesh(mesh)
    plan = make_raw_mesh_repair_plan(
        mesh,
        diagnostic,
        operations=(CorrectConsistentWinding(),),
        requested_by='explicit_user_selected',
        request_reason='dedup persistence fixture',
    )
    repaired = apply_raw_mesh_repair(mesh, diagnostic, plan)
    post = diagnose_repaired_raw_mesh(mesh, repaired)
    return mesh, RawMeshRepairBundle(
        source_raw_mesh=mesh,
        source_diagnostic=diagnostic,
        repair_plan=plan,
        repaired_mesh=repaired,
        post_repair_diagnostic=post,
    )


def test_repair_bundle_references_canonical_source_blob(tmp_path: Path) -> None:
    mesh, bundle = _repair_bundle()
    repository = RawMeshRepairRepository(tmp_path / 'cad.sqlite3')

    assert repository.save(bundle) is True
    assert repository.save(bundle) is False

    rows = _query(
        repository.path,
        'SELECT payload_json FROM cad_raw_mesh_repair_bundles',
    )
    record = json.loads(rows[0]['payload_json'])
    assert 'original_asset_base64' not in rows[0]['payload_json']
    assert record['source_raw_mesh'] == {
        'mesh_id': mesh.mesh_id,
        'provenance': mesh.provenance.model_dump(mode='json'),
    }
    blob_rows = _query(
        repository.path,
        'SELECT payload_blob FROM htdt_content_blobs WHERE payload_sha256=?',
        (mesh.provenance.original_asset_sha256,),
    )
    assert len(blob_rows) == 1
    assert bytes(blob_rows[0]['payload_blob']) == mesh.original_asset_bytes()

    reopened = repository.get(bundle.repaired_mesh.repaired_mesh_id)
    assert reopened == bundle
    assert reopened.source_raw_mesh.original_asset_bytes() == (
        mesh.original_asset_bytes()
    )


def test_legacy_embedded_repair_bundle_row_reads_losslessly(
    tmp_path: Path,
) -> None:
    mesh, bundle = _repair_bundle()
    repository = RawMeshRepairRepository(tmp_path / 'cad.sqlite3')
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            '''
            INSERT INTO cad_raw_mesh_repair_bundles(
                repaired_mesh_id,
                repaired_mesh_semantic_hash,
                raw_mesh_id,
                raw_mesh_semantic_hash,
                repair_plan_id,
                repair_plan_semantic_hash,
                post_diagnostic_id,
                post_diagnostic_semantic_hash,
                payload_json,
                created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                bundle.repaired_mesh.repaired_mesh_id,
                bundle.repaired_mesh.semantic_hash(),
                bundle.source_raw_mesh.mesh_id,
                bundle.source_raw_mesh.semantic_hash(),
                bundle.repair_plan.plan_id,
                bundle.repair_plan.semantic_hash(),
                bundle.post_repair_diagnostic.diagnostic_id,
                bundle.post_repair_diagnostic.semantic_hash(),
                serialize_raw_mesh_repair_bundle(bundle),
                '2026-09-20T00:00:00+00:00',
            ),
        )

    reopened = repository.get(bundle.repaired_mesh.repaired_mesh_id)
    assert reopened == bundle
    assert reopened.source_raw_mesh.original_asset_bytes() == (
        mesh.original_asset_bytes()
    )
    # A same-bundle save through the new path deduplicates against the
    # legacy row rather than raising a spurious identity collision.
    assert repository.save(bundle) is False
