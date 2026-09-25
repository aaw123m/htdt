"""Backend coverage for the #488 portable project bundle.

Export writes a hash-verified .htdtproject containing the document-scoped
authority graph plus reachable shared authority (managed assets, capture
evidence); import validates manifest/hashes/closure, commits atomically and
enforces the collision contract (identical -> reuse, same-id-diff-content
-> reject, document collision -> explicit import-as-copy).
"""

from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile

import pytest

from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import normalize_rew_text
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    F1_DOCUMENT_ID,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.project_bundle import (
    BundleImportConflictError,
    BundleManifestInvalidError,
    ProjectBundleError,
    export_project_bundle,
    import_project_bundle,
)
from htdt.project_library_repository import ProjectLibraryRepository


def _scene(document_id: str, *, speaker_x: float = 1.0) -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                position=Position3(x_m=speaker_x, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.22, y_m=0.28, z_m=0.42),
                speaker_role='FL',
            ),
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )


def _seed_project(tmp_path: Path):
    """One document with two revisions and one raw-backed measurement."""

    repository = SceneRepository(tmp_path / 'source' / 'cad-scenes.sqlite3')
    head = repository.save(_scene('doc-a'), parent_revision_id=None).revision
    head = repository.save(
        _scene('doc-a', speaker_x=2.0), parent_revision_id=head.revision_id
    ).revision
    measurements = CadMeasurementRepository(repository)
    raw = b'Frequency SPL\n20 70.0\n40 71.5\n80 69.0\n'
    record, dataset, filename, source = normalize_rew_text(
        head,
        'point-mlp',
        raw,
        filename='mlp-fl.txt',
        evidence_type='measured',
        channel_role='front_left',
        source_speaker_ids=('speaker-fl',),
        routing_evidence='verified',
        imported_at='2026-09-20T09:30:00+00:00',
    )
    measurements.save(record, dataset, raw_filename=filename, raw_bytes=source)
    # An unrelated project in the same data root must never leak in.
    repository.save(_scene('doc-b'), parent_revision_id=None)
    return repository, head, record, dataset


def _export(tmp_path: Path) -> tuple[Path, object]:
    repository, _head, _record, _dataset = _seed_project(tmp_path)
    archive = tmp_path / 'out' / 'doc-a.htdtproject'
    result = export_project_bundle(repository, 'doc-a', archive)
    return archive, result


def test_export_writes_manifest_and_exact_closure(tmp_path):
    archive, result = _export(tmp_path)

    assert result.document_id == 'doc-a'
    assert result.row_count > 0
    assert result.asset_count == 1

    with ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read('manifest.json'))
    assert manifest['schema'] == 'htdt.project-bundle'
    assert manifest['schema_version'] == '1.0.0'
    assert manifest['root']['document_id'] == 'doc-a'
    assert manifest['manifest_sha256']
    # The unrelated doc-b rows are not in the bundle.
    with ZipFile(archive) as bundle:
        for name in bundle.namelist():
            if name.startswith('db/'):
                assert b'doc-b' not in bundle.read(name)
    # And the manifest records the privacy-visible omissions.
    assert manifest['omissions']


def test_export_requires_scene_content(tmp_path):
    repository = SceneRepository(tmp_path / 'empty' / 'cad-scenes.sqlite3')
    with pytest.raises(ProjectBundleError):
        export_project_bundle(
            repository, 'never-saved', tmp_path / 'x.htdtproject'
        )


def test_export_fails_closed_on_missing_asset(tmp_path):
    repository, _head, _record, dataset = _seed_project(tmp_path)
    digest = dataset.source_sha256
    asset = (
        tmp_path / 'source' / 'measurement-assets' / digest
    )
    assert asset.is_file()
    asset.unlink()

    with pytest.raises(ProjectBundleError, match='integrity error'):
        export_project_bundle(
            repository, 'doc-a', tmp_path / 'broken.htdtproject'
        )


def test_import_restores_project_with_assets(tmp_path):
    archive, _result = _export(tmp_path)
    target = SceneRepository(tmp_path / 'target' / 'cad-scenes.sqlite3')

    imported = import_project_bundle(target, archive)

    assert imported.document_id == 'doc-a'
    assert imported.import_mode == 'straight'
    assert imported.imported_rows > 0
    assert imported.imported_assets == 1

    head = target.current_head('doc-a')
    assert head is not None
    assert head.document.entities[0].position.x_m == 2.0
    # The project is registered in the library.
    library = ProjectLibraryRepository(target)
    assert library.get_by_document_id('doc-a') is not None
    # The managed asset was installed and is readable.
    asset_rows = sqlite3.connect(target.path)
    asset_rows.row_factory = sqlite3.Row
    row = asset_rows.execute(
        'SELECT * FROM cad_measurement_assets'
    ).fetchone()
    asset_rows.close()
    assert row is not None
    installed = tmp_path / 'target' / 'measurement-assets' / row['sha256']
    assert installed.is_file()


def test_reimport_is_idempotent(tmp_path):
    archive, _result = _export(tmp_path)
    target = SceneRepository(tmp_path / 'target' / 'cad-scenes.sqlite3')
    first = import_project_bundle(target, archive)

    second = import_project_bundle(target, archive)

    assert second.import_mode == 'reimport'
    assert second.imported_rows == 0
    assert second.reused_rows == first.imported_rows
    assert second.reused_assets == 1


def test_import_rejects_tampered_manifest(tmp_path):
    archive, _result = _export(tmp_path)
    forged = tmp_path / 'forged.htdtproject'
    with ZipFile(archive) as source, ZipFile(forged, 'w') as dest:
        for name in source.namelist():
            body = source.read(name)
            if name == 'manifest.json':
                manifest = json.loads(body)
                manifest['root']['display_name'] = 'forged'
                body = json.dumps(manifest).encode()
            dest.writestr(name, body)

    with pytest.raises(BundleManifestInvalidError, match='manifest hash'):
        import_project_bundle(
            SceneRepository(tmp_path / 't' / 'cad-scenes.sqlite3'), forged
        )


def test_import_rejects_tampered_asset(tmp_path):
    archive, _result = _export(tmp_path)
    forged = tmp_path / 'forged.htdtproject'
    with ZipFile(archive) as source, ZipFile(forged, 'w') as dest:
        for name in source.namelist():
            body = source.read(name)
            if name.startswith('assets/'):
                body = body + b'tampered'
            dest.writestr(name, body)

    with pytest.raises(
        BundleManifestInvalidError, match='hash/size mismatch'
    ):
        import_project_bundle(
            SceneRepository(tmp_path / 't' / 'cad-scenes.sqlite3'), forged
        )


def test_import_rejects_traversal_member(tmp_path):
    archive, _result = _export(tmp_path)
    evil = tmp_path / 'evil.htdtproject'
    with ZipFile(archive) as source, ZipFile(evil, 'w') as dest:
        for name in source.namelist():
            dest.writestr(name, source.read(name))
        dest.writestr('../escape.txt', b'nope')

    with pytest.raises(BundleManifestInvalidError, match='unexpected'):
        import_project_bundle(
            SceneRepository(tmp_path / 't' / 'cad-scenes.sqlite3'), evil
        )


def test_document_collision_requires_import_as_copy(tmp_path):
    repository, _head, _record, _dataset = _seed_project(tmp_path)
    archive = tmp_path / 'doc-a.htdtproject'
    export_project_bundle(repository, 'doc-a', archive)
    # A different project now owns 'doc-a' on the target.
    target = SceneRepository(tmp_path / 'target' / 'cad-scenes.sqlite3')
    target.save(_scene('doc-a', speaker_x=9.9), parent_revision_id=None)

    with pytest.raises(BundleImportConflictError):
        import_project_bundle(target, archive)


def test_import_as_copy_remapped_identity(tmp_path):
    repository, _head, _record, _dataset = _seed_project(tmp_path)
    archive = tmp_path / 'doc-a.htdtproject'
    export_project_bundle(repository, 'doc-a', archive)
    target = SceneRepository(tmp_path / 'target' / 'cad-scenes.sqlite3')
    target.save(_scene('doc-a', speaker_x=9.9), parent_revision_id=None)

    result = import_project_bundle(target, archive, import_as_copy=True)

    assert result.document_id != 'doc-a'
    assert result.import_mode == 'copy'
    head = target.current_head(result.document_id)
    assert head is not None
    assert head.document.document_id == result.document_id
    assert head.document.entities[0].position.x_m == 2.0
    # Original project untouched.
    assert (
        target.current_head('doc-a').document.entities[0].position.x_m == 9.9
    )
    # Provenance event recorded.
    connection = sqlite3.connect(target.path)
    connection.row_factory = sqlite3.Row
    events = connection.execute(
        'SELECT * FROM htdt_project_imports'
    ).fetchall()
    connection.close()
    assert len(events) == 1
    assert events[0]['import_mode'] == 'copy'
    assert events[0]['source_document_id'] == 'doc-a'
    assert events[0]['imported_document_id'] == result.document_id
    # The copied project is registered and openable.
    library = ProjectLibraryRepository(target)
    entry = library.get_by_document_id(result.document_id)
    assert entry is not None


def test_same_bundle_imported_twice_as_copy_rejects_record_collision(
    tmp_path,
):
    repository, _head, _record, _dataset = _seed_project(tmp_path)
    archive = tmp_path / 'doc-a.htdtproject'
    export_project_bundle(repository, 'doc-a', archive)
    target = SceneRepository(tmp_path / 'target' / 'cad-scenes.sqlite3')
    target.save(_scene('doc-a', speaker_x=9.9), parent_revision_id=None)
    import_project_bundle(target, archive, import_as_copy=True)

    # A second copy remaps record ids rather than rebinding history.
    second = import_project_bundle(target, archive, import_as_copy=True)
    assert second.document_id != 'doc-a'
    head = target.current_head(second.document_id)
    assert head is not None
