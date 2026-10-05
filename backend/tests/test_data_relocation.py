from __future__ import annotations

from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene
from htdt.data_relocation import (
    DataRelocationBlockedError,
    HTDTBootstrapConfig,
    ManagedDataUnavailableError,
    assert_managed_root_available,
    execute_data_relocation,
    load_bootstrap_config,
    plan_data_relocation,
    resolve_data_dir,
    save_bootstrap_config,
)
from htdt.native_backup import DATABASE_NAME
from htdt.runtime_instance import SingleInstanceGuard


def _seed_data_dir(data_dir: Path, document_id: str = 'doc-1') -> None:
    repository = SceneRepository(data_dir / DATABASE_NAME)
    repository.save(make_empty_scene(document_id), parent_revision_id=None)
    # A managed asset so the asset-copy phase is exercised; the manifest
    # table is created by the measurement repository authority.
    from htdt.cad_measurement_repository import CadMeasurementRepository

    CadMeasurementRepository(repository)
    payload = b'evidence-bytes'
    digest = sha256(payload).hexdigest()
    assets = data_dir / 'measurement-assets'
    assets.mkdir(parents=True, exist_ok=True)
    (assets / digest).write_bytes(payload)
    with closing(sqlite3.connect(data_dir / DATABASE_NAME)) as c, c:
        c.execute(
            'INSERT INTO cad_measurement_assets('
            'sha256, filename, relative_path, size_bytes'
            ') VALUES (?, ?, ?, ?)',
            (digest, 'evidence.bin', f'measurement-assets/{digest}', len(payload)),
        )


def test_bootstrap_config_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / 'boot' / 'htdt-bootstrap.json'
    config = HTDTBootstrapConfig(
        data_dir=str(tmp_path / 'moved'), written_at_utc='2026-01-01T00:00:00Z'
    )
    save_bootstrap_config(config, path)
    assert load_bootstrap_config(path) == config


def test_resolve_precedence_explicit_wins(tmp_path: Path) -> None:
    boot = tmp_path / 'boot.json'
    save_bootstrap_config(
        HTDTBootstrapConfig(
            data_dir=str(tmp_path / 'from-boot'),
            written_at_utc='2026-01-01T00:00:00Z',
        ),
        boot,
    )
    path, source = resolve_data_dir(
        tmp_path / 'explicit', bootstrap_path=boot, default=tmp_path / 'def'
    )
    assert path == tmp_path / 'explicit'
    assert source == 'explicit'

    path, source = resolve_data_dir(
        None, bootstrap_path=boot, default=tmp_path / 'def'
    )
    assert path == tmp_path / 'from-boot'
    assert source == 'bootstrap'

    path, source = resolve_data_dir(
        None, bootstrap_path=tmp_path / 'missing.json', default=tmp_path / 'def'
    )
    assert path == tmp_path / 'def'
    assert source == 'default'


def test_unavailable_bootstrap_root_fails_closed(tmp_path: Path) -> None:
    missing = tmp_path / 'gone'
    with pytest.raises(ManagedDataUnavailableError) as caught:
        assert_managed_root_available(missing, 'bootstrap')
    assert str(missing) in str(caught.value)
    # The default path is NOT silently substituted — it is never even
    # consulted here.
    assert not (tmp_path / 'default').exists()


def test_available_explicit_root_passes(tmp_path: Path) -> None:
    assert_managed_root_available(tmp_path / 'new', 'explicit')
    assert_managed_root_available(tmp_path, 'bootstrap')


def test_plan_reports_blockers(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    _seed_data_dir(source)
    plan = plan_data_relocation(source, tmp_path / 'dest')
    assert plan.executable
    assert plan.database_bytes > 0
    assert plan.asset_count == 1
    assert plan.asset_bytes == 14
    assert plan.blockers == ()

    # Destination inside source is refused.
    inside = plan_data_relocation(source, source / 'inner' / 'dest')
    kinds = {b.kind for b in inside.blockers}
    assert 'destination_inside_source' in kinds
    # Same location refused.
    same = plan_data_relocation(source, source)
    assert {b.kind for b in same.blockers} >= {'same_location'}
    # Non-empty destination with live data refused.
    other = tmp_path / 'other'
    _seed_data_dir(other, 'doc-2')
    clash = plan_data_relocation(source, other)
    assert 'destination_has_live_data' in {b.kind for b in clash.blockers}
    # Missing source database refused.
    empty_src = tmp_path / 'empty-src'
    empty_src.mkdir()
    missing = plan_data_relocation(empty_src, tmp_path / 'dest2')
    assert 'source_has_no_data' in {b.kind for b in missing.blockers}


def test_execute_relocation_moves_and_verifies(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    _seed_data_dir(source)
    destination = tmp_path / 'moved' / 'data'
    bootstrap = tmp_path / 'boot.json'

    plan, parked = execute_data_relocation(
        source, destination, bootstrap_path=bootstrap
    )

    # Copy phase landed a working data root at the destination.
    assert (destination / DATABASE_NAME).is_file()
    expected_digest = sha256(b'evidence-bytes').hexdigest()
    assert (destination / 'measurement-assets' / expected_digest).is_file()
    # Verification: the relocated DB opens and holds the project.
    repository = SceneRepository(destination / DATABASE_NAME)
    assert repository.current_head('doc-1') is not None
    # Source kept but parked, never silently deleted.
    assert not source.exists()
    assert parked.name.startswith(f'source.relocated-')
    assert (parked / DATABASE_NAME).is_file()
    # Bootstrap now points at the destination.
    config = load_bootstrap_config(bootstrap)
    assert config is not None
    assert Path(config.data_dir) == destination


def test_relocation_blocked_when_source_in_use(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    _seed_data_dir(source)
    guard = SingleInstanceGuard(source)
    assert guard.acquire()
    try:
        with pytest.raises(DataRelocationBlockedError):
            execute_data_relocation(source, tmp_path / 'dest')
    finally:
        guard.release()
    # Source untouched.
    assert (source / DATABASE_NAME).is_file()


def test_blocked_plan_raises_before_moving(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    _seed_data_dir(source)
    with pytest.raises(DataRelocationBlockedError):
        execute_data_relocation(source, source / 'inner' / 'dest')
    assert (source / DATABASE_NAME).is_file()


def test_execute_relocation_into_existing_empty_destination(
    tmp_path: Path,
) -> None:
    """An existing EMPTY destination is allowed by the plan — and the
    cutover must honor it on Windows too, where ``os.replace`` refuses to
    move a directory onto any existing directory, even an empty one
    (WinError 5 → the journal post-STAGED_VERIFIED recovery would retry
    the same rename forever and brick startup)."""
    source = tmp_path / 'source'
    _seed_data_dir(source)
    destination = tmp_path / 'moved' / 'data'
    destination.mkdir(parents=True)
    bootstrap = tmp_path / 'boot.json'

    plan, parked = execute_data_relocation(
        source, destination, bootstrap_path=bootstrap
    )

    assert (destination / DATABASE_NAME).is_file()
    repository = SceneRepository(destination / DATABASE_NAME)
    assert repository.current_head('doc-1') is not None
    config = load_bootstrap_config(bootstrap)
    assert config is not None
    assert Path(config.data_dir) == destination
