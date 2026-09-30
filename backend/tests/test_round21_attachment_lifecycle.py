"""REV21-ATTACH — attachment/file lifecycle truth regression tests.

Covers the verified gaps from round 21:

* the two measurement attachment pickers bound reads by the attachment
  contract (``MAX_ATTACHMENT_BYTES``), not the REW-text file bound;
* ``read_attachment`` fails closed on an externally deleted managed file
  instead of silently returning ``None``;
* bundle export streams asset bytes (constant memory) and still fails
  closed when a referenced file's content does not match its registry
  digest;
* bundle import installs one member at a time and no longer rejects
  legitimate highly-compressible assets on a compression-ratio heuristic;
* a floor-plan underlay whose blob vanished from the content store is
  flagged ``missing_source`` instead of rendering silently empty.
"""
from __future__ import annotations

import ast
import sqlite3
import tracemalloc
from contextlib import closing
from hashlib import sha256
from pathlib import Path

import pytest

from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import normalize_rew_text
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.limits import MAX_ATTACHMENT_BYTES, MAX_NATIVE_REW_TEXT_FILE_BYTES
from htdt.managed_assets import MANAGED_ASSETS_DIRNAME, ManagedAssetError
from htdt.project_bundle import (
    ProjectBundleError,
    export_project_bundle,
    import_project_bundle,
)
from htdt.room_workspace import RoomWorkspaceController


def _repositories(tmp_path: Path):
    repository = SceneRepository(tmp_path / 'cad-scenes.sqlite3')
    revision = repository.save(make_f1_scene(), parent_revision_id=None).revision
    return repository, revision, CadMeasurementRepository(repository)


def _measurement(repository: CadMeasurementRepository, revision, raw: bytes):
    record, dataset, filename, source = normalize_rew_text(
        revision,
        'point-mlp',
        raw,
        filename='mlp.txt',
        evidence_type='measured',
        imported_at='2026-09-30T00:00:00+00:00',
    )
    repository.save(record, dataset, raw_filename=filename, raw_bytes=source)
    return record, dataset


# ---------------------------------------------------------------------------
# ADD — the attachment picker honors the attachment byte limit, not the
# 32 MiB REW-text bound (equipment library already used it correctly).
# ---------------------------------------------------------------------------


def test_attachment_pickers_use_attachment_byte_limit() -> None:
    """AST-level check: both ``ソース添付`` read_file_bounded calls must be
    bounded by MAX_ATTACHMENT_BYTES, never the REW text limit."""
    source_path = (
        Path(__file__).resolve().parents[1]
        / 'src' / 'htdt' / 'measurement_page_workspace.py'
    )
    tree = ast.parse(source_path.read_text(encoding='utf-8'))
    attachment_reads: list[ast.Call] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == 'read_file_bounded'
        ):
            continue
        label = next(
            (
                kw.value.value
                for kw in node.keywords
                if kw.arg == 'label' and isinstance(kw.value, ast.Constant)
            ),
            None,
        )
        if label == 'ソース添付':
            attachment_reads.append(node)
    assert attachment_reads, 'attachment reads not found'
    for call in attachment_reads:
        bound = call.args[1]
        assert isinstance(bound, ast.Name) and bound.id == (
            'MAX_ATTACHMENT_BYTES'
        ), 'attachment picker must read with MAX_ATTACHMENT_BYTES'
    assert MAX_ATTACHMENT_BYTES > MAX_NATIVE_REW_TEXT_FILE_BYTES


# ---------------------------------------------------------------------------
# MISSING — an externally deleted attachment fails closed, like every other
# authoritative read path.
# ---------------------------------------------------------------------------


def test_read_attachment_fails_closed_on_missing_file(tmp_path: Path) -> None:
    repository, revision, mrepo = _repositories(tmp_path)
    record, _dataset = _measurement(mrepo, revision, b'Frequency SPL\n20 70.0\n40 71.5\n')

    attachment = mrepo.save_attachment(
        measurement_id=record.measurement_id,
        kind='notes',
        filename='notes.txt',
        raw_bytes=b'attachment payload',
    )
    assert mrepo.read_attachment(attachment) == b'attachment payload'

    (mrepo.assets_dir / attachment.sha256).unlink()

    with pytest.raises(ManagedAssetError, match='missing or not a regular file'):
        mrepo.read_attachment(attachment)


def test_identical_attachment_content_dedupes_to_one_file(tmp_path: Path) -> None:
    repository, revision, mrepo = _repositories(tmp_path)
    record, _dataset = _measurement(mrepo, revision, b'Frequency SPL\n20 70.0\n40 71.5\n')
    raw = b'shared-payload' * 4096

    first = mrepo.save_attachment(
        measurement_id=record.measurement_id,
        kind='notes', filename='a.bin', raw_bytes=raw,
    )
    second = mrepo.save_attachment(
        measurement_id=record.measurement_id,
        kind='notes', filename='b.bin', raw_bytes=raw,
    )

    assert first.sha256 == second.sha256
    files = [
        path for path in mrepo.assets_dir.iterdir()
        if path.name == first.sha256
    ]
    assert len(files) == 1


# ---------------------------------------------------------------------------
# SIZE — bundle export/import must not hold every asset in memory at once.
# ---------------------------------------------------------------------------


def _seed_bundle_project(tmp_path: Path, asset_blobs: list[bytes]):
    repository, revision, mrepo = _repositories(tmp_path / 'src')
    record, _dataset = _measurement(mrepo, revision, b'Frequency SPL\n20 70.0\n40 71.5\n')
    for index, blob in enumerate(asset_blobs):
        mrepo.save_attachment(
            measurement_id=record.measurement_id,
            kind='evidence',
            filename=f'asset-{index}.bin',
            raw_bytes=blob,
        )
    return repository, revision.document_id


def test_bundle_export_streams_assets_in_bounded_memory(tmp_path: Path) -> None:
    blob_a = bytes(range(256)) * 32768  # 8 MiB, incompressible enough
    blob_b = bytes(reversed(range(256))) * 32768  # 8 MiB
    repository, document_id = _seed_bundle_project(
        tmp_path, [blob_a, blob_b]
    )
    archive = tmp_path / 'out' / 'project.htdtproject'

    tracemalloc.start()
    try:
        result = export_project_bundle(repository, document_id, archive)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.asset_count == 3  # raw dataset asset + two attachments
    # 16 MiB of assets export in well under their total size: the previous
    # implementation held every payload in a dict and peaked >3x this.
    assert peak < 8 * 1024 * 1024

    # Round-trip still verifies.
    dest = SceneRepository(tmp_path / 'dst' / 'cad-scenes.sqlite3')
    imported = import_project_bundle(dest, archive)
    assert imported.document_id == document_id


def test_bundle_import_installs_assets_one_member_at_a_time(
    tmp_path: Path,
) -> None:
    blob_a = bytes(range(256)) * 32768
    blob_b = bytes(reversed(range(256))) * 32768
    repository, document_id = _seed_bundle_project(
        tmp_path, [blob_a, blob_b]
    )
    archive = tmp_path / 'out' / 'project.htdtproject'
    export_project_bundle(repository, document_id, archive)

    dest = SceneRepository(tmp_path / 'dst' / 'cad-scenes.sqlite3')
    tracemalloc.start()
    try:
        import_project_bundle(dest, archive)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # Peak is a bounded read chunk, not the sum of members — the previous
    # code held every asset payload concurrently (~24 MiB here).
    assert peak < 12 * 1024 * 1024


def test_bundle_import_accepts_highly_compressible_asset(
    tmp_path: Path,
) -> None:
    """A repeated-pattern asset compresses past any ratio bound — the
    archive-level bound was a false 'implausible ratio' rejection, while
    bounded streaming reads already cap real decompression output."""
    compressible = b'\x00' * (6 * 1024 * 1024)
    repository, document_id = _seed_bundle_project(tmp_path, [compressible])
    archive = tmp_path / 'out' / 'project.htdtproject'
    export_project_bundle(repository, document_id, archive)

    dest = SceneRepository(tmp_path / 'dst' / 'cad-scenes.sqlite3')
    imported = import_project_bundle(dest, archive)
    assert imported.document_id == document_id
    digest = sha256(compressible).hexdigest()
    assert (tmp_path / 'dst' / MANAGED_ASSETS_DIRNAME / digest).is_file()


def test_bundle_export_fails_closed_when_asset_corrupts_on_disk(
    tmp_path: Path,
) -> None:
    """Same-length corruption must abort the streamed write and leave no
    destination archive."""
    repository, document_id = _seed_bundle_project(
        tmp_path, [b'payload-' * 1024]
    )
    assets = tmp_path / 'src' / MANAGED_ASSETS_DIRNAME
    target = assets / sha256(b'payload-' * 1024).hexdigest()
    target.write_bytes(b'corrupt!' * 1024)

    archive = tmp_path / 'out' / 'project.htdtproject'
    with pytest.raises(
        ProjectBundleError, match='failed hash/size verification'
    ):
        export_project_bundle(repository, document_id, archive)
    assert not archive.exists()
    assert not list(archive.parent.iterdir()) or all(
        not p.name.endswith('.tmp') for p in archive.parent.iterdir()
    )


# ---------------------------------------------------------------------------
# MISSING — an underlay whose blob vanished flags itself, not a silent blank.
# ---------------------------------------------------------------------------


def _tiny_png(tmp_path: Path) -> Path:
    from PySide6.QtGui import QImage

    target = tmp_path / 'tiny.png'
    image = QImage(2, 2, QImage.Format.Format_RGBA8888)
    image.fill(0xFF204060)
    assert image.save(str(target), 'PNG')
    return target


def test_underlay_missing_blob_is_flagged_not_silent(tmp_path: Path) -> None:
    repository, revision, _mrepo = _repositories(tmp_path)
    controller = RoomWorkspaceController(repository, revision.document_id)

    underlay = controller.import_underlay(_tiny_png(tmp_path))
    items = controller.underlay_render_items()
    assert len(items) == 1
    assert items[0].image is not None
    assert items[0].missing_source is False
    assert controller.underlay_missing_source(underlay) is False

    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            'DELETE FROM htdt_content_blobs WHERE payload_sha256=?',
            (underlay.render_blob_sha256,),
        )

    assert controller.underlay_missing_source(underlay) is True
    items = controller.underlay_render_items()
    assert len(items) == 1
    assert items[0].image is None
    assert items[0].missing_source is True
