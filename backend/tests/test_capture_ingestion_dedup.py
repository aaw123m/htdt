from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import struct
import tracemalloc

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_fixture_support as support  # noqa: E402

from htdt.cad_repository import SceneRepository
from htdt.cad_schema import NATIVE_SCHEMA_VERSION, read_native_schema_version
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


BUNDLE_DIGEST = support.BUNDLE_DIGEST
SERIES_ID = support.SERIES_ID
REVISION_ID = support.REVISION_ID
SESSION_ID = support.SESSION_ID
SPACE_ID = support.SPACE_ID
ANCHOR_ID = support.ANCHOR_ID
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
    return support.meshbin(vertex_count, face_count)


def _mesh_counts(payload: bytes) -> tuple[int, int]:
    vertex_count = struct.unpack_from('<I', payload, 16)[0]
    face_count = struct.unpack_from('<I', payload, 20)[0]
    return vertex_count, face_count


CORRECTED_REVISION_ID = '10000000-0000-4000-8000-00000000000a'


def _plan_and_payloads(
    payloads: dict[str, bytes] | None = None,
    mesh_specs: tuple[tuple[str, str, int, int], ...] | None = None,
    bundle_digest: str = support.BUNDLE_DIGEST,
    manifest_overrides: dict | None = None,
    tmp_path: Path | None = None,
) -> tuple[dict, dict[str, bytes]]:
    """Build a contract-valid ingestion plan fixture.

    ``payloads`` supplies caller-owned payload bytes (meshbins get real
    anchor records synthesized from their headers; unknown JSON paths are
    declared as imported_reference derived payloads); ``mesh_specs``
    declares (anchor_id, geometry_path, vertex_count, face_count) mesh
    records; ``bundle_digest`` set to anything other than the default
    produces a distinct bundle via a manifest timestamp override.
    """
    import tempfile
    workdir = (
        tmp_path if tmp_path is not None else Path(tempfile.mkdtemp())
    )

    files = {
        path: spec
        for path, spec in support.default_file_specs().items()
        if path.startswith('session/') or path.startswith('quality/')
    }

    if payloads is not None:
        for path, payload in payloads.items():
            if path == 'mesh/anchors.json':
                continue
            if path.endswith('.meshbin'):
                continue
            meta = support.fixture_meta().get(path)
            if meta is None:
                meta = {
                    'media_type': (
                        'application/json'
                        if path.endswith('.json')
                        else 'application/octet-stream'
                    ),
                    'producer': 'test_fixture',
                    'provenance_class': 'imported_reference',
                    'role': 'derived',
                }
            files[path] = {'bytes': payload, **meta}

    if mesh_specs is None:
        mesh_paths = [
            path
            for path in (payloads or {})
            if path.endswith('.meshbin')
        ]
        if mesh_paths:
            specs = []
            for path in mesh_paths:
                vc, fc = _mesh_counts((payloads or {})[path])
                anchor_id = path.rsplit('/', 1)[-1].removesuffix('.meshbin')
                specs.append((anchor_id, path, vc, fc))
        else:
            specs = ((
                support.ANCHOR_ID,
                f'mesh/geometry/{support.ANCHOR_ID}.meshbin',
                3,
                1,
            ),)
        mesh_specs = tuple(specs)

    mesh_payloads = {
        path: payload
        for path, payload in (payloads or {}).items()
        if path.endswith('.meshbin')
    }
    files.update(
        support.mesh_specs_files(mesh_specs, mesh_payloads=mesh_payloads)
    )

    if bundle_digest != support.BUNDLE_DIGEST:
        manifest_overrides = {
            **(manifest_overrides or {}),
            'created_at': '2026-09-21T00:00:00Z',
            'finalized_at': '2026-09-21T00:00:01Z',
        }

    plan, payload_map, _manifest = support.plan_and_payloads(
        workdir,
        files=files,
        manifest_overrides=manifest_overrides,
    )
    return plan, payload_map



def _repository(tmp_path: Path) -> CaptureIngestionRepository:
    return CaptureIngestionRepository(SceneRepository(tmp_path / 'cad.sqlite3'))


def _empty_schema_bytes(tmp_path: Path, name: str = 'empty.sqlite3') -> int:
    """Bytes of a migrated database holding schema pages only (#302).

    The versioned migration installs every canonical table eagerly, so
    payload-size assertions must discount this constant schema footprint
    rather than comparing absolute file size directly.
    """
    path = tmp_path / name
    SceneRepository(path)
    return path.stat().st_size


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
    shared = b'shared-binary-payload'
    payloads = {
        'mesh/anchors.json': b'{"fixture":"anchors"}',
        f'mesh/geometry/{ANCHOR_ID}.meshbin': _meshbin(),
        'derived/a.bin': shared,
        'derived/b.bin': shared,
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
    assert repository.source_evidence_count() == 8


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

    # A corrected capture carries the same payloads under a new revision
    # whose parent is the original — the registry allows it while the
    # content-addressed blob store still deduplicates.
    plan2, payloads2 = _plan_and_payloads(
        dict(payloads),
        manifest_overrides={
            'capture_revision_id': CORRECTED_REVISION_ID,
            'parent_revision_id': REVISION_ID,
            'created_at': '2026-09-21T00:00:00Z',
            'finalized_at': '2026-09-21T00:00:01Z',
        },
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
        assert not repository.list_ingestion_runs(
            lineage_digest=typed.lineage_digest
        )

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
    assert not repository.list_ingestion_runs(
            lineage_digest=typed.lineage_digest
        )


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
    # payload itself, not the ~7x old JSON/Base64 expansion. The retained
    # canonical manifest and validated-quality evidence add a small constant
    # per bundle on top of that.
    database_bytes = repository.path.stat().st_size
    payload_bytes = database_bytes - _empty_schema_bytes(tmp_path)
    assert payload_bytes < len(asset) * 1.5
    assert payload_bytes < len(legacy_payload_json) // 3

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
    binding_id = repository.mesh_binding_ids_for_lineage(
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
                binding_id, handoff_id, payload_json,
                anchor_index_source_evidence_id,
                geometry_source_evidence_id
            ) VALUES (?, ?, ?, ?, ?)
            ''',
            (
                binding.binding_id,
                handoff.raw_visual_mesh_handoff_id,
                legacy_payload_json,
                handoff.anchor_index_source_evidence_id,
                handoff.geometry_source_evidence_id,
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

    assert read_native_schema_version(path) == NATIVE_SCHEMA_VERSION
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

    binding_id = repository.mesh_binding_ids_for_lineage(
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
    # Freed duplicated pages shrink the payload section below the legacy
    # size; the versioned schema pages added by #302 are discounted
    # because the legacy fixture predates the eager canonical schema.
    assert path.stat().st_size - _empty_schema_bytes(
        tmp_path, 'schema.sqlite3'
    ) < legacy_size


def test_v3_binding_rows_gain_normalized_source_authority_columns(
    tmp_path: Path,
) -> None:
    """Legacy binding rows backfill the exact source authorities on open."""
    path = tmp_path / 'cad.sqlite3'
    plan, payloads = _plan_and_payloads()
    typed = CaptureIngestionPlan.model_validate(plan)
    _legacy_database(path, plan, payloads)

    repository = CaptureIngestionRepository(SceneRepository(path))

    handoff = typed.raw_visual_mesh_handoffs[0]
    rows = _query(
        path,
        'SELECT binding_id, anchor_index_source_evidence_id, '
        'geometry_source_evidence_id FROM capture_raw_visual_mesh_bindings',
    )
    assert len(rows) == 1
    assert (
        rows[0]['anchor_index_source_evidence_id']
        == handoff.anchor_index_source_evidence_id
    )
    assert (
        rows[0]['geometry_source_evidence_id']
        == handoff.geometry_source_evidence_id
    )

    # The normalized edges are real foreign keys into capture_source_evidence.
    foreign_keys = _query(
        path,
        'PRAGMA foreign_key_list(capture_raw_visual_mesh_bindings)',
    )
    edges = {
        (row['from'], row['table'], row['to']) for row in foreign_keys
    }
    assert (
        'anchor_index_source_evidence_id',
        'capture_source_evidence',
        'source_evidence_id',
    ) in edges
    assert (
        'geometry_source_evidence_id',
        'capture_source_evidence',
        'source_evidence_id',
    ) in edges

    # Migration is deterministic and idempotent: reopening revalidates.
    reopened = CaptureIngestionRepository(SceneRepository(path))
    binding = reopened.get_mesh_binding(rows[0]['binding_id'])
    assert binding is not None
    assert binding.handoff == handoff
    assert repository.get_mesh_binding(rows[0]['binding_id']) == binding


def test_v3_binding_with_unpersisted_source_authority_fails_migration(
    tmp_path: Path,
) -> None:
    """A legacy row whose payload names missing evidence fails closed."""
    path = tmp_path / 'cad.sqlite3'
    plan, payloads = _plan_and_payloads()
    _legacy_database(path, plan, payloads)

    with closing(sqlite3.connect(path)) as connection, connection:
        row = connection.execute(
            'SELECT binding_id, payload_json '
            'FROM capture_raw_visual_mesh_bindings'
        ).fetchone()
        data = json.loads(row[1])
        data['handoff']['geometry_source_evidence_id'] = 'f' * 64
        connection.execute(
            'UPDATE capture_raw_visual_mesh_bindings '
            'SET payload_json=? WHERE binding_id=?',
            (_canonical_json(data), row[0]),
        )

    with pytest.raises(
        CaptureIngestionTransactionError,
        match='source evidence that is not persisted',
    ):
        CaptureIngestionRepository(SceneRepository(path))


def test_v3_binding_with_invalid_handoff_fails_migration(
    tmp_path: Path,
) -> None:
    """A legacy payload that cannot produce the canonical pair fails closed."""
    path = tmp_path / 'cad.sqlite3'
    plan, payloads = _plan_and_payloads()
    _legacy_database(path, plan, payloads)

    with closing(sqlite3.connect(path)) as connection, connection:
        row = connection.execute(
            'SELECT binding_id, payload_json '
            'FROM capture_raw_visual_mesh_bindings'
        ).fetchone()
        data = json.loads(row[1])
        del data['handoff']['anchor_index_source_evidence_id']
        connection.execute(
            'UPDATE capture_raw_visual_mesh_bindings '
            'SET payload_json=? WHERE binding_id=?',
            (_canonical_json(data), row[0]),
        )

    with pytest.raises(
        CaptureIngestionTransactionError,
        match='handoff record is invalid',
    ):
        CaptureIngestionRepository(SceneRepository(path))


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

    binding_id = reopened.mesh_binding_ids_for_lineage(
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
