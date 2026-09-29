"""Round 14 — project bundle round-trip completeness.

Pins the findings this round fixes:

- The machine-local receiver lane (``capture_receiver_pairings``
  including the live ``pairing_token``, deliveries, mission packages)
  and the ``htdt_storage_gc_pending`` ledger were pulled into bundles by
  identity edges (project_ref, lineage digests), contradicting the
  manifest's own 'paired device state' omission and shipping a live
  bearer credential to anyone who received the file. These tables are
  now local-only and can never be exported.
- Keyless tables (``cad_field_evidence_targets`` and the
  ``cad_*_selections`` family) had no natural key at all, so a re-import
  re-inserted every row. Whole-row dedupe now applies when no usable
  key exists.
- Copy import refused every document row whose bound payload embeds
  ``document_id`` next to a semantic hash column
  (``cad_applied_settings.applied_sha256`` and friends) — although the
  owning models derive that hash as ``canonical_sha256(payload minus the
  hash field)``, which import can re-verify and honestly recompute for
  the remapped payload. Self-verifying hashes are now recomputed;
  embedded non-derivable hashes still fail closed.
- A member missing a NOT NULL column stalled the insert loop and
  surfaced as 'unresolved dependency order'; it now fails up front as
  the schema-generation mismatch it is.
- A malformed JSONL member escaped as ``JSONDecodeError`` instead of
  ``BundleManifestInvalidError``.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from zipfile import ZipFile

import pytest

from htdt.canonical_json import canonical_sha256
from htdt.cad_repository import SceneRepository
from htdt.project_bundle import (
    BundleImportConflictError,
    BundleManifestInvalidError,
    export_project_bundle,
    import_project_bundle,
)

from test_project_bundle import _seed_project


NOW = '2026-09-22T12:00:00+00:00'


def _doc_id(repository: SceneRepository) -> str:
    """Register the seeded document like the library does, then return
    the document id (``_seed_project`` uses 'doc-a')."""
    from htdt.project_library_repository import ProjectLibraryRepository
    return 'doc-a' if ProjectLibraryRepository(
        repository).ensure_document_registered('doc-a') else 'doc-a'


def _table_counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    counts = {
        name: conn.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' "
            "AND name NOT LIKE 'native_schema_%'"
        )
    }
    conn.close()
    return counts


def _rewrite_member(bundle: Path, member: str, body: bytes) -> Path:
    out = bundle.with_name(bundle.stem + '-mut' + bundle.suffix)
    with ZipFile(bundle) as zin, ZipFile(out, 'w') as zout:
        for name in zin.namelist():
            zout.writestr(name, body if name == member else zin.read(name))
    return out


def _fix_manifest(bundle: Path, transform) -> Path:
    """Apply *transform* to manifest.json in place and repair the
    self-hash so the bundle stays internally consistent."""
    with ZipFile(bundle) as z:
        manifest = json.loads(z.read('manifest.json'))
    transform(manifest)
    manifest.pop('manifest_sha256', None)
    manifest['manifest_sha256'] = canonical_sha256(manifest)
    return _rewrite_member(
        bundle, 'manifest.json',
        json.dumps(manifest, indent=2, sort_keys=True).encode())


def test_receiver_lane_rows_never_leave_the_machine(tmp_path):
    repository, _head, _record, _dataset = _seed_project(tmp_path)
    document_id = _doc_id(repository)
    pairing_id = str(uuid.uuid4())
    conn = sqlite3.connect(repository.path)
    try:
        project_row = conn.execute(
            'SELECT project_id FROM htdt_project_documents '
            'WHERE document_id=?', (document_id,)
        ).fetchone()
        assert project_row is not None
        project_id = project_row[0]
        conn.execute(
            'INSERT INTO capture_receiver_pairings('
            'pairing_id, pairing_token, receiver_instance_id, '
            'project_ref, endpoint_url, capability_endpoint_url, '
            'missions_endpoint_url, pinned_identity, confirmation_code, '
            'state, created_at_utc, confirmed_at_utc, expires_at_utc, '
            'capture_instance_id) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (pairing_id, 'tok-live-bearer', 'rcv-1',
             project_id, 'http://192.168.0.10/api',
             'http://192.168.0.10/capabilities',
             'http://192.168.0.10/missions',
             'cert-sha', '8317', 'confirmed', NOW, NOW, NOW, 'ci-1'),
        )
        conn.execute(
            'INSERT INTO capture_receiver_deliveries('
            'delivery_key, pairing_id, artifact_kind, artifact_id, '
            'artifact_digest, capture_revision_id, bundle_digest, '
            'archive_sha256, archive_bytes, outcome, staging_ref, '
            'lineage_digest, detail, received_at_utc) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('dlv-1', pairing_id, 'capture_bundle', 'cap-1',
             'd' * 64, str(uuid.uuid4()), 'e' * 64, 'f' * 64, 32,
             'stored', None, 'l1' + '0' * 62, '{}', NOW),
        )
        conn.execute(
            'INSERT INTO capture_mission_packages('
            'package_id, pairing_id, descriptor_json, payload_sha256, '
            'byte_size, status, status_detail, created_at_utc, '
            'updated_at_utc) VALUES (?,?,?,?,?,?,?,?,?)',
            (str(uuid.uuid4()), pairing_id, '{}', 'b' * 64, 2,
             'queued', '待機', NOW, NOW),
        )
        conn.execute(
            'INSERT INTO htdt_storage_gc_pending('
            'sha256, size_bytes, queued_at_utc) VALUES (?,?,?)',
            ('c' * 64, 128, NOW),
        )
        conn.commit()
    finally:
        conn.close()

    archive = tmp_path / 'out' / 'doc.htdtproject'
    export_project_bundle(repository, document_id, archive)
    with ZipFile(archive) as z:
        members = set(z.namelist())
        manifest = json.loads(z.read('manifest.json'))

    for table in (
        'capture_mission_packages',
        'capture_receiver_config',
        'capture_receiver_deliveries',
        'capture_receiver_pairings',
        'htdt_storage_gc_pending',
    ):
        assert f'db/{table}.jsonl' not in members
        assert table not in {t['table'] for t in manifest['tables']}
    # the omission declaration stays truthful, and no live bearer token
    # rides along in any member at all
    assert any(
        'device' in o['subject'] for o in manifest['omissions']
    )
    with ZipFile(archive) as z:
        blob = b'\n'.join(z.read(n) for n in z.namelist())
    assert b'tok-live-bearer' not in blob


def test_reimport_dedupes_keyless_rows(tmp_path):
    repository, head, _record, _dataset = _seed_project(tmp_path)
    document_id = _doc_id(repository)
    evidence_id = str(uuid.uuid4())
    conn = sqlite3.connect(repository.path)
    try:
        # cad_field_evidence_targets has no primary key and no unique
        # index — nothing for natural-key dedupe to hang on.
        conn.execute(
            'INSERT INTO cad_field_evidence('
            'evidence_id, document_id, kind, asset_sha256, '
            'evidence_sha256, created_at_utc, payload_json) '
            'VALUES (?,?,?,?,?,?,?)',
            (evidence_id, document_id, 'photo', None,
             'e' * 64, NOW, '{"note": "壁"}'),
        )
        conn.execute(
            'INSERT INTO cad_field_evidence_targets('
            'evidence_id, target_kind, revision_id, entity_id, ref_id) '
            'VALUES (?,?,?,?,?)',
            (evidence_id, 'entity', head.revision_id, 'speaker-fl',
             None),
        )
        conn.commit()
    finally:
        conn.close()

    archive = tmp_path / 'out' / 'doc.htdtproject'
    export_project_bundle(repository, document_id, archive)
    target = tmp_path / 'target' / 'cad-scenes.sqlite3'
    import_project_bundle(SceneRepository(target), archive)
    again = import_project_bundle(SceneRepository(target), archive)

    assert again.imported_rows == 0
    assert again.reused_rows > 0
    conn = sqlite3.connect(target)
    assert conn.execute(
        'SELECT COUNT(*) FROM cad_field_evidence_targets'
    ).fetchone()[0] == 1
    assert conn.execute(
        'SELECT COUNT(*) FROM cad_field_evidence'
    ).fetchone()[0] == 1
    conn.close()


def test_copy_import_recomputes_semantic_hash(tmp_path):
    repository, _head, _record, _dataset = _seed_project(tmp_path)
    document_id = _doc_id(repository)
    applied_id = str(uuid.uuid4())
    plan_id = str(uuid.uuid4())
    payload = {
        'applied_id': applied_id,
        'document_id': document_id,
        'calibration_plan_id': plan_id,
        'applied_at_utc': NOW,
        'settings': {'target_curve': 'flat'},
    }
    # production pattern: the semantic hash is derived over the payload
    # minus the hash field, so a copy re-derives it honestly.
    payload['applied_sha256'] = canonical_sha256(payload)
    conn = sqlite3.connect(repository.path)
    try:
        conn.execute(
            'INSERT INTO cad_applied_settings('
            'applied_id, document_id, calibration_plan_id, '
            'applied_sha256, applied_at_utc, payload_json) '
            'VALUES (?,?,?,?,?,?)',
            (applied_id, document_id, plan_id,
             payload['applied_sha256'], NOW, json.dumps(
                 payload, ensure_ascii=False, sort_keys=True,
                 separators=(',', ':'))),
        )
        conn.commit()
    finally:
        conn.close()

    archive = tmp_path / 'out' / 'doc.htdtproject'
    export_project_bundle(repository, document_id, archive)
    target = tmp_path / 'target' / 'cad-scenes.sqlite3'
    import_project_bundle(SceneRepository(target), archive)
    copied = import_project_bundle(
        SceneRepository(target), archive, import_as_copy=True)

    assert copied.document_id != document_id
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        'SELECT applied_sha256, payload_json FROM cad_applied_settings '
        'WHERE document_id=?', (copied.document_id,)
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    reparsed = json.loads(rows[0]['payload_json'])
    assert reparsed['document_id'] == copied.document_id
    assert reparsed['applied_sha256'] == rows[0]['applied_sha256']
    assert canonical_sha256(
        {k: v for k, v in reparsed.items() if k != 'applied_sha256'}
    ) == rows[0]['applied_sha256']


def test_bundle_missing_required_column_is_schema_drift(tmp_path):
    archive, _result = _export_and_archive(tmp_path)
    member = 'db/scene_revisions.jsonl'
    with ZipFile(archive) as z:
        body = z.read(member).decode()
    rows = [json.loads(line) for line in body.splitlines() if line]
    assert rows
    for row in rows:
        idx = row['columns'].index('content_hash')
        row['columns'].pop(idx)
        row['values'].pop(idx)
    body_new = ''.join(
        json.dumps(r, ensure_ascii=False) + '\n' for r in rows
    ).encode()
    mutant = _rewrite_member(archive, member, body_new)
    # keep the manifest honest so the failure is the schema drift itself
    import hashlib

    def _sync(m):
        for t in m['tables']:
            if t['table'] == 'scene_revisions':
                t['row_count'] = len(rows)
                t['rows_sha256'] = hashlib.sha256(body_new).hexdigest()
    mutant = _fix_manifest(mutant, _sync)
    with pytest.raises(BundleManifestInvalidError, match='schema generation'):
        import_project_bundle(
            SceneRepository(tmp_path / 'target' / 'cad-scenes.sqlite3'),
            mutant)


def test_bundle_member_must_be_valid_jsonl(tmp_path):
    archive, _result = _export_and_archive(tmp_path)
    body = b'not-json\n{bad}\n'
    mutant = _rewrite_member(archive, 'db/scene_revisions.jsonl', body)
    import hashlib

    def _sync(m):
        for t in m['tables']:
            if t['table'] == 'scene_revisions':
                t['rows_sha256'] = hashlib.sha256(body).hexdigest()
    mutant = _fix_manifest(mutant, _sync)
    with pytest.raises(BundleManifestInvalidError, match='JSONL'):
        import_project_bundle(
            SceneRepository(tmp_path / 'target' / 'cad-scenes.sqlite3'),
            mutant)


def test_copy_of_capture_bundle_fails_closed_and_rolls_back(tmp_path):
    from htdt.capture_ingestion_transaction import (
        CaptureIngestionPlan,
        CaptureIngestionRepository,
    )
    from htdt.capture_inbox import CaptureInboxRepository
    from htdt.semantic_geometry import SemanticCoordinateTransform

    repository, _head, _record, _dataset = _seed_project(tmp_path)
    # scope links are identity edges — only uuid/sha documents pull the
    # capture lane into a bundle
    from test_project_bundle import _scene
    from htdt.project_library_repository import ProjectLibraryRepository
    document_id = str(uuid.uuid4())
    repository.save(_scene(document_id), parent_revision_id=None)
    ProjectLibraryRepository(
        repository).ensure_document_registered(document_id)
    ingestion = CaptureIngestionRepository(repository)
    inbox = CaptureInboxRepository(repository, ingestion)

    from test_capture_inbox import _plan_and_payloads

    def _stage(rev, parent):
        plan, payloads = _plan_and_payloads(
            tmp_path, revision_id=rev, parent_revision_id=parent)
        ingestion.ingest(plan, payloads)
        typed = CaptureIngestionPlan.model_validate(plan)
        return inbox.stage(
            typed,
            arrival_source='file_import',
            source_detail='/captures/a.htdtcapture',
        )

    rev_a, rev_b = str(uuid.uuid4()), str(uuid.uuid4())
    a = _stage(rev_a, None)
    b = _stage(rev_b, rev_a)
    inbox.assign_scope(a.lineage_digest, document_id)
    inbox.assign_scope(b.lineage_digest, document_id)
    inbox.promote(
        a.lineage_digest, ['semantic_geometry'], reason='v1',
        created_authorities={'semantic_geometry': str(uuid.uuid4())},
    )
    inbox.supersede(
        a.lineage_digest, b.lineage_digest, 'semantic_geometry',
        reason='rescan')
    identity = SemanticCoordinateTransform(
        matrix_source_to_scene_m=(
            (1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0),
            (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0),
        ),
        provenance='explicit_user_authority',
        reason='matched door frames',
    )
    inbox.register_cross_revision_alignment(
        a.lineage_digest, b.lineage_digest, identity,
        alignment_method='identity')
    # a third capture left unassigned — machine-local, never exported
    _stage(str(uuid.uuid4()), None)

    archive = tmp_path / 'out' / 'doc.htdtproject'
    export_project_bundle(repository, document_id, archive)
    target = tmp_path / 'target' / 'cad-scenes.sqlite3'
    import_project_bundle(SceneRepository(target), archive)
    before = _table_counts(target)
    # shared capture material cannot be identity-remapped under plain
    # row-copy semantics — the copy must refuse honestly, not corrupt
    with pytest.raises(BundleImportConflictError):
        import_project_bundle(
            SceneRepository(target), archive, import_as_copy=True)
    assert _table_counts(target) == before


def _export_and_archive(tmp_path: Path):
    repository, _head, _record, _dataset = _seed_project(tmp_path)
    document_id = _doc_id(repository)
    archive = tmp_path / 'out' / 'doc.htdtproject'
    result = export_project_bundle(repository, document_id, archive)
    return archive, result
