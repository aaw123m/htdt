"""Production .htdtcapture import path tests (#333)."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_fixture_support as support  # noqa: E402

from htdt.cad_repository import SceneRepository
from htdt.capture_ingestion_transaction import (
    CaptureIngestionRepository,
)
from htdt.capture_import import (
    CaptureImportError,
    import_capture_artifact,
)


BUNDLE_DIGEST = support.BUNDLE_DIGEST
REVISION_ID = support.REVISION_ID


def _repository(tmp_path: Path) -> CaptureIngestionRepository:
    return CaptureIngestionRepository(SceneRepository(tmp_path / 'cad.sqlite3'))


def _write_zip(
    bundle_dir: Path, dest: Path
) -> Path:
    with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(bundle_dir.rglob('*')):
            if path.is_file():
                zf.write(path, path.relative_to(bundle_dir).as_posix())
    return dest


def test_import_directory_commits_and_reports(tmp_path: Path) -> None:
    bundle_dir, manifest = support.write_bundle(
        tmp_path / 'bundle', support.default_file_specs()
    )
    repository = _repository(tmp_path)
    result = import_capture_artifact(bundle_dir, repository)
    assert result.stage == 'committed'
    assert result.created is True
    assert result.bundle_digest == sha256(manifest).hexdigest()
    assert result.capture_revision_id == REVISION_ID
    assert result.quality_state == 'validated'
    assert result.quality_ruleset_version == '1.0.0'
    assert result.app_name == 'HTDT-Capture'
    assert result.app_version == '0.0.0-phase6'
    assert result.app_build == 'phase6-fixture'
    assert result.source_evidence_count == 10
    assert result.raw_mesh_binding_count == 1
    assert result.authority_record_count == 2
    assert result.roomplan_record_count == 2


def test_import_zip_wrapper_identical_to_directory(
    tmp_path: Path,
) -> None:
    bundle_dir, _ = support.write_bundle(
        tmp_path / 'bundle', support.default_file_specs()
    )
    zip_path = _write_zip(bundle_dir, tmp_path / 'fixture.htdtcapture')
    repository = _repository(tmp_path)
    result = import_capture_artifact(zip_path, repository)
    assert result.created is True
    assert result.bundle_digest == BUNDLE_DIGEST


def test_reimport_is_idempotent_verified(tmp_path: Path) -> None:
    bundle_dir, _ = support.write_bundle(
        tmp_path / 'bundle', support.default_file_specs()
    )
    repository = _repository(tmp_path)
    first = import_capture_artifact(bundle_dir, repository)
    second = import_capture_artifact(bundle_dir, repository)
    assert second.created is False
    assert second.stage == 'verified'
    assert second.lineage_digest == first.lineage_digest
    assert repository.source_evidence_count() == 10


def test_import_failure_reports_stage(tmp_path: Path) -> None:
    bundle_dir, _ = support.write_bundle(
        tmp_path / 'bundle', support.default_file_specs()
    )
    # Corrupt a declared payload byte: fails at archive read/validate.
    (bundle_dir / 'session' / 'timing.json').write_bytes(b'{}')
    repository = _repository(tmp_path)
    with pytest.raises(CaptureImportError) as excinfo:
        import_capture_artifact(bundle_dir, repository)
    assert excinfo.value.stage == 'read'
    assert 'timing.json' in excinfo.value.reason

    # Tampered manifest identity (non-canonical) fails at manifest stage.
    bundle_dir2, manifest = support.write_bundle(
        tmp_path / 'bundle2', support.default_file_specs()
    )
    (bundle_dir2 / 'manifest.json').write_bytes(manifest + b'\n')
    with pytest.raises(CaptureImportError) as excinfo:
        import_capture_artifact(bundle_dir2, repository)
    assert excinfo.value.stage == 'read'


def test_import_cli_roundtrip(tmp_path: Path) -> None:
    from htdt.capture_import import main

    bundle_dir, _ = support.write_bundle(
        tmp_path / 'bundle', support.default_file_specs()
    )
    db = tmp_path / 'cad.sqlite3'
    assert main([str(bundle_dir), '--db', str(db)]) == 0
    repository = _repository(tmp_path)
    assert repository.source_evidence_count() == 10
    # second CLI import is a no-op verified path
    assert main([str(bundle_dir), '--db', str(db)]) == 0


def test_import_deeply_nested_manifest_reports_stage(tmp_path: Path) -> None:
    """A manifest json.loads reports as RecursionError stays a staged failure."""
    bundle_dir = tmp_path / 'bundle'
    bundle_dir.mkdir()
    deep = ('[' * 3000 + ']' * 3000).encode('utf-8')
    (bundle_dir / 'manifest.json').write_bytes(deep)
    with pytest.raises(CaptureImportError) as excinfo:
        import_capture_artifact(bundle_dir, _repository(tmp_path))
    assert excinfo.value.stage == 'read'


def test_import_zip_deeply_nested_manifest_reports_stage(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / 'bundle'
    bundle_dir.mkdir()
    deep = ('[' * 3000 + ']' * 3000).encode('utf-8')
    (bundle_dir / 'manifest.json').write_bytes(deep)
    wrapped = _write_zip(bundle_dir, tmp_path / 'bundle.htdtcapture')
    with pytest.raises(CaptureImportError) as excinfo:
        import_capture_artifact(wrapped, _repository(tmp_path))
    assert excinfo.value.stage == 'read'


def test_validate_bundle_deeply_nested_manifest_is_bundle_error(
    tmp_path: Path,
) -> None:
    from htdt.capture_bundle import CaptureBundleError, validate_bundle

    bundle_dir = tmp_path / 'bundle'
    bundle_dir.mkdir()
    deep = ('[' * 3000 + ']' * 3000).encode('utf-8')
    (bundle_dir / 'manifest.json').write_bytes(deep)
    with pytest.raises(CaptureBundleError):
        validate_bundle(bundle_dir)
