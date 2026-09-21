from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from htdt.cad_document import WorkingDocument
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, make_f1_scene
import htdt.native_backup as native_backup
from htdt.native_backup import (
    RestoreRecoveryError,
    create_backup,
    recover_interrupted_restore,
    restore_backup,
    validate_backup,
)


def _seed_data(data_dir: Path):
    repository = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None).revision
    measurements = CadMeasurementRepository(repository)

    raw = b'owned raw measurement fixture\n'
    digest = sha256(raw).hexdigest()
    relative_path = f'measurement-assets/{digest}'
    asset = data_dir / relative_path
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(raw)
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            '''INSERT INTO cad_measurement_assets(
                sha256, filename, relative_path, size_bytes
            ) VALUES (?, ?, ?, ?)''',
            (digest, 'fixture.txt', relative_path, len(raw)),
        )
    return repository, first, digest, raw


def _mutate_scene(repository: SceneRepository, source_revision_id: str):
    source = repository.get(source_revision_id)
    assert source is not None
    working = WorkingDocument(
        source.document,
        source_revision_id=source.revision_id,
        saved_content_hash=source.content_hash,
    )
    working.move_entity(
        'speaker-fl',
        Position3(x_m=1.75, y_m=0.75, z_m=1.05),
    )
    return repository.save(
        working.committed_document,
        parent_revision_id=source.revision_id,
    ).revision


def _rewrite_zip(source: Path, destination: Path, replacements: dict[str, bytes]) -> None:
    with ZipFile(source, 'r') as original, ZipFile(
        destination,
        'w',
        compression=ZIP_DEFLATED,
    ) as rewritten:
        for info in original.infolist():
            payload = replacements.get(info.filename, original.read(info.filename))
            rewritten.writestr(info.filename, payload)


def test_native_backup_round_trip_restores_database_and_content_addressed_assets(tmp_path: Path):
    data_dir = tmp_path / 'data'
    repository, first, digest, raw = _seed_data(data_dir)
    backup_path = tmp_path / 'baseline.htdt-backup'

    manifest = create_backup(data_dir, backup_path)
    validated = validate_backup(backup_path)

    assert validated == manifest
    assert {entry.path for entry in manifest.files} == {
        'cad-scenes.sqlite3',
        f'measurement-assets/{digest}',
    }

    second = _mutate_scene(repository, first.revision_id)
    assert second.revision_id != first.revision_id
    assert SceneRepository(repository.path).latest(first.document_id).revision_id == second.revision_id

    restored, pre_restore = restore_backup(data_dir, backup_path)

    assert restored == manifest
    assert pre_restore is not None and pre_restore.is_file()
    validate_backup(pre_restore)
    reopened = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    assert reopened.latest(first.document_id).revision_id == first.revision_id
    assert (data_dir / 'measurement-assets' / digest).read_bytes() == raw


def test_backup_validation_rejects_asset_content_tampering(tmp_path: Path):
    data_dir = tmp_path / 'data'
    _repository, _first, digest, _raw = _seed_data(data_dir)
    valid = tmp_path / 'valid.htdt-backup'
    corrupt = tmp_path / 'corrupt.htdt-backup'
    create_backup(data_dir, valid)

    _rewrite_zip(
        valid,
        corrupt,
        {f'measurement-assets/{digest}': b'tampered payload'},
    )

    with pytest.raises(ValueError, match='size mismatch|SHA-256 mismatch'):
        validate_backup(corrupt)


def test_backup_validation_rejects_path_traversal_member(tmp_path: Path):
    data_dir = tmp_path / 'data'
    _seed_data(data_dir)
    valid = tmp_path / 'valid.htdt-backup'
    malicious = tmp_path / 'malicious.htdt-backup'
    create_backup(data_dir, valid)

    with ZipFile(valid, 'r') as original, ZipFile(
        malicious,
        'w',
        compression=ZIP_DEFLATED,
    ) as rewritten:
        for info in original.infolist():
            rewritten.writestr(info.filename, original.read(info.filename))
        rewritten.writestr('../escape.txt', b'escape')

    with pytest.raises(ValueError, match='unsafe backup archive path'):
        validate_backup(malicious)
    assert not (tmp_path / 'escape.txt').exists()


def test_failed_restore_leaves_current_native_data_unchanged(tmp_path: Path):
    data_dir = tmp_path / 'data'
    repository, first, digest, _raw = _seed_data(data_dir)
    baseline = tmp_path / 'baseline.htdt-backup'
    corrupt = tmp_path / 'corrupt.htdt-backup'
    create_backup(data_dir, baseline)

    second = _mutate_scene(repository, first.revision_id)
    _rewrite_zip(
        baseline,
        corrupt,
        {f'measurement-assets/{digest}': b'corrupt'},
    )

    with pytest.raises(ValueError):
        restore_backup(data_dir, corrupt)

    reopened = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    assert reopened.latest(first.document_id).revision_id == second.revision_id


def test_backup_normalizes_existing_windows_asset_relative_paths(tmp_path: Path):
    data_dir = tmp_path / 'data'
    repository = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    CadMeasurementRepository(repository)

    raw = b'windows-separator-fixture'
    digest = sha256(raw).hexdigest()
    asset = data_dir / 'measurement-assets' / digest
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(raw)
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            '''INSERT INTO cad_measurement_assets(
                sha256, filename, relative_path, size_bytes
            ) VALUES (?, ?, ?, ?)''',
            (
                digest,
                'fixture.txt',
                f'measurement-assets\\{digest}',
                len(raw),
            ),
        )

    archive = tmp_path / 'windows-path.htdt-backup'
    manifest = create_backup(data_dir, archive)

    assert f'measurement-assets/{digest}' in {entry.path for entry in manifest.files}
    validate_backup(archive)


def test_restore_swap_failure_rolls_back_original_database_and_assets(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / 'data'
    repository, first, digest, raw = _seed_data(data_dir)
    baseline = tmp_path / 'baseline.htdt-backup'
    create_backup(data_dir, baseline)

    second = _mutate_scene(repository, first.revision_id)
    original_replace = native_backup.os.replace
    failed = False

    def fail_staged_asset_swap(source, destination):
        nonlocal failed
        source_path = Path(source)
        if (
            not failed
            and source_path.name == 'measurement-assets'
            and 'htdt-restore-stage-' in str(source_path.parent)
        ):
            failed = True
            raise OSError('injected staged asset swap failure')
        return original_replace(source, destination)

    monkeypatch.setattr(native_backup.os, 'replace', fail_staged_asset_swap)

    with pytest.raises(OSError, match='injected staged asset swap failure'):
        restore_backup(data_dir, baseline)

    reopened = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    assert reopened.latest(first.document_id).revision_id == second.revision_id
    assert (data_dir / 'measurement-assets' / digest).read_bytes() == raw


def test_backup_validation_rejects_excessive_expanded_size(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_dir = tmp_path / 'data'
    _seed_data(data_dir)
    archive = tmp_path / 'bounded.htdt-backup'
    create_backup(data_dir, archive)

    monkeypatch.setattr(native_backup, 'MAX_NATIVE_BACKUP_EXPANDED_BYTES', 1)

    with pytest.raises(ValueError, match='expanded size exceeds limit'):
        validate_backup(archive)


def test_backup_rejects_destination_equal_to_live_database(tmp_path: Path):
    data_dir = tmp_path / 'data'
    _repository, _first, _digest, _raw = _seed_data(data_dir)
    database = data_dir / 'cad-scenes.sqlite3'
    before = database.read_bytes()
    names_before = {entry.name for entry in data_dir.iterdir()}

    with pytest.raises(ValueError, match='overlaps the live native database'):
        create_backup(data_dir, database)

    assert database.read_bytes() == before
    assert {entry.name for entry in data_dir.iterdir()} == names_before


def test_backup_rejects_destination_aliasing_live_database_via_dotdot(tmp_path: Path):
    data_dir = tmp_path / 'data'
    _repository, _first, _digest, _raw = _seed_data(data_dir)
    database = data_dir / 'cad-scenes.sqlite3'
    before = database.read_bytes()

    alias = data_dir / 'exports' / '..' / 'cad-scenes.sqlite3'
    with pytest.raises(ValueError, match='overlaps the live native database'):
        create_backup(data_dir, alias)

    assert database.read_bytes() == before
    assert not (data_dir / 'exports').exists()


def test_backup_rejects_destination_aliasing_database_through_symlinked_directory(
    tmp_path: Path,
):
    data_dir = tmp_path / 'data'
    _repository, _first, _digest, _raw = _seed_data(data_dir)
    link = tmp_path / 'data-link'
    try:
        link.symlink_to(data_dir, target_is_directory=True)
    except OSError:
        pytest.skip('directory symlinks are not supported on this platform')
    database = data_dir / 'cad-scenes.sqlite3'
    before = database.read_bytes()

    with pytest.raises(ValueError, match='overlaps the live native database'):
        create_backup(data_dir, link / 'cad-scenes.sqlite3')

    assert database.read_bytes() == before


def test_backup_rejects_destination_aliasing_assets_through_windows_junction(
    tmp_path: Path,
):
    if os.name != 'nt':
        pytest.skip('junction aliasing is Windows-specific')

    data_dir = tmp_path / 'data'
    _repository, _first, digest, raw = _seed_data(data_dir)
    junction = tmp_path / 'assets-junction'
    result = subprocess.run(
        ['cmd.exe', '/c', 'mklink', '/J', str(junction), str(data_dir)],
        capture_output=True,
    )
    if result.returncode != 0 or not junction.exists():
        pytest.skip('directory junctions are not supported in this environment')
    asset = data_dir / 'measurement-assets' / digest

    with pytest.raises(ValueError, match='measurement-assets'):
        create_backup(data_dir, junction / 'measurement-assets' / digest)

    assert asset.read_bytes() == raw


def test_backup_rejects_destination_overlapping_managed_measurement_asset(
    tmp_path: Path,
):
    data_dir = tmp_path / 'data'
    _repository, _first, digest, raw = _seed_data(data_dir)
    asset = data_dir / 'measurement-assets' / digest

    with pytest.raises(ValueError, match='measurement-assets'):
        create_backup(data_dir, asset)

    assert asset.read_bytes() == raw


def test_backup_rejects_new_destination_inside_measurement_assets_subtree(
    tmp_path: Path,
):
    data_dir = tmp_path / 'data'
    _repository, _first, _digest, _raw = _seed_data(data_dir)
    nested = data_dir / 'measurement-assets' / 'exports' / 'nested.htdt-backup'

    with pytest.raises(ValueError, match='measurement-assets'):
        create_backup(data_dir, nested)

    assert not (data_dir / 'measurement-assets' / 'exports').exists()


def test_backup_rejects_hardlink_alias_of_managed_measurement_asset(tmp_path: Path):
    data_dir = tmp_path / 'data'
    _repository, _first, digest, raw = _seed_data(data_dir)
    asset = data_dir / 'measurement-assets' / digest
    alias = tmp_path / f'{digest}.htdt-backup'
    try:
        os.link(asset, alias)
    except OSError:
        pytest.skip('hard links are not supported on this platform')

    with pytest.raises(ValueError, match='managed measurement asset'):
        create_backup(data_dir, alias)

    assert asset.read_bytes() == raw
    assert alias.read_bytes() == raw


def test_backup_allows_destination_inside_data_dir_outside_managed_data(
    tmp_path: Path,
):
    data_dir = tmp_path / 'data'
    _repository, _first, digest, raw = _seed_data(data_dir)
    archive = data_dir / 'exports' / 'nested.htdt-backup'

    manifest = create_backup(data_dir, archive)

    assert validate_backup(archive) == manifest
    assert (data_dir / 'measurement-assets' / digest).read_bytes() == raw


def test_backup_rejects_destination_that_is_an_existing_directory(tmp_path: Path):
    data_dir = tmp_path / 'data'
    _repository, _first, _digest, _raw = _seed_data(data_dir)
    directory = tmp_path / 'existing-dir'
    directory.mkdir()

    with pytest.raises(ValueError, match='is a directory'):
        create_backup(data_dir, directory)

    assert not list(directory.iterdir())


class _SimulatedCrash(BaseException):
    """Stands in for process termination: bypasses ``except Exception`` handlers."""


def _rollback_dirs(data_dir: Path) -> list[Path]:
    parent = data_dir.parent
    return sorted(
        child
        for child in parent.iterdir()
        if child.is_dir() and child.name.startswith(f'.{data_dir.name}-restore-rollback-')
    )


def _stage_dirs(data_dir: Path) -> list[Path]:
    return sorted(
        child
        for child in data_dir.parent.iterdir()
        if child.is_dir() and child.name.startswith('htdt-restore-stage-')
    )


def _keep_stage_dir(monkeypatch) -> None:
    """Make restore staging directories survive a simulated process kill.

    tempfile.TemporaryDirectory only cleans up on orderly interpreter exit;
    a real crash strands the directory, and recovery depends on the staged
    payload still being there.
    """
    real_temporary_directory = tempfile.TemporaryDirectory

    class _PersistentStageDirectory(real_temporary_directory):
        def cleanup(self) -> None:
            if os.path.basename(self.name).startswith('htdt-restore-stage-'):
                self._finalizer.detach()
                return
            super().cleanup()

    monkeypatch.setattr(
        native_backup.tempfile, 'TemporaryDirectory', _PersistentStageDirectory
    )


def _inject_swap_crash(
    monkeypatch,
    *,
    before_move: int | None = None,
    after_move: int | None = None,
) -> None:
    """Raise _SimulatedCrash around the Nth live/staged os.replace.

    Swap moves are identified by destination name; journal writes and the
    pre-restore archive publish use different names. The injection fires once,
    so recovery-time replaces are unaffected even while still patched.
    """
    original_replace = native_backup.os.replace
    moves: list[tuple[Path, Path]] = []
    fired = False

    def crashing_replace(source, destination):
        nonlocal fired
        is_swap_move = Path(destination).name in {
            native_backup.DATABASE_NAME,
            native_backup.MEASUREMENT_ASSETS_NAME,
        }
        if (
            is_swap_move
            and not fired
            and before_move is not None
            and len(moves) + 1 == before_move
        ):
            fired = True
            raise _SimulatedCrash(f'crash before swap move {before_move}')
        result = original_replace(source, destination)
        if is_swap_move:
            moves.append((Path(source), Path(destination)))
            if not fired and after_move is not None and len(moves) == after_move:
                fired = True
                raise _SimulatedCrash(f'crash after swap move {after_move}')
        return result

    monkeypatch.setattr(native_backup.os, 'replace', crashing_replace)


def _crash_after_validated(monkeypatch) -> None:
    """Crash after the journal records live validation, before cleanup."""
    original_journal_phase = native_backup._journal_phase

    def crashing_journal_phase(rollback_root, journal, phase):
        original_journal_phase(rollback_root, journal, phase)
        if phase == 'validated':
            raise _SimulatedCrash('crash after live validation')

    monkeypatch.setattr(native_backup, '_journal_phase', crashing_journal_phase)


_SWAP_BOUNDARIES = {
    'before_swap': {'before_move': 1},
    'after_db_evac': {'after_move': 1},
    'after_assets_evac': {'after_move': 2},
    'after_staged_db': {'after_move': 3},
    'after_staged_assets': {'after_move': 4},
}


def _crash_restore(
    tmp_path: Path,
    monkeypatch,
    *,
    boundary: str,
    keep_stage: bool = True,
):
    """Run a restore that dies mid-swap; returns fixture context."""
    data_dir = tmp_path / 'data'
    repository, first, digest, raw = _seed_data(data_dir)
    baseline = tmp_path / 'baseline.htdt-backup'
    create_backup(data_dir, baseline)
    second = _mutate_scene(repository, first.revision_id)

    if keep_stage:
        _keep_stage_dir(monkeypatch)
    if boundary == 'after_validated':
        _crash_after_validated(monkeypatch)
    else:
        _inject_swap_crash(monkeypatch, **_SWAP_BOUNDARIES[boundary])

    with pytest.raises(_SimulatedCrash):
        restore_backup(data_dir, baseline)
    # A crashed process takes its patches with it: recovery runs against the
    # filesystem exactly as the next launch would see it.
    monkeypatch.undo()
    return data_dir, repository, first, second, digest, raw


def _assert_live_state(data_dir: Path, document_id: str, revision_id: str, digest: str, raw: bytes) -> None:
    reopened = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    latest = reopened.latest(document_id)
    assert latest is not None
    assert latest.revision_id == revision_id
    assert (data_dir / 'measurement-assets' / digest).read_bytes() == raw


def _assert_no_restore_artifacts(data_dir: Path) -> None:
    assert _rollback_dirs(data_dir) == []
    assert _stage_dirs(data_dir) == []


# Whether recovery completes the swap or rolls back to the pre-restore
# generation depends only on which payload survived the crash.
@pytest.mark.parametrize('boundary', [*_SWAP_BOUNDARIES, 'after_validated'])
@pytest.mark.parametrize('keep_stage', [True, False])
def test_interrupted_restore_recovers_a_valid_generation(
    tmp_path: Path, monkeypatch, boundary: str, keep_stage: bool
):
    data_dir, _repository, first, second, digest, raw = _crash_restore(
        tmp_path, monkeypatch, boundary=boundary, keep_stage=keep_stage
    )

    # A lost staging directory makes completion impossible only while the
    # staged payload is still needed; an already-installed restored state
    # completes without it.
    stage_needed = boundary in {
        'before_swap',
        'after_db_evac',
        'after_assets_evac',
        'after_staged_db',
    }
    expect_completed = keep_stage or not stage_needed

    events = recover_interrupted_restore(data_dir)

    assert [event.action for event in events] == [
        'completed' if expect_completed else 'rolled_back'
    ]
    expected_revision = (
        first.revision_id if expect_completed else second.revision_id
    )
    _assert_live_state(data_dir, first.document_id, expected_revision, digest, raw)
    _assert_no_restore_artifacts(data_dir)


def test_interrupted_restore_recovers_through_repository_startup(
    tmp_path: Path, monkeypatch, caplog
):
    """Opening the native DB resolves the pending swap instead of seeding."""
    data_dir, _repository, first, _second, digest, raw = _crash_restore(
        tmp_path, monkeypatch, boundary='after_db_evac'
    )
    assert not (data_dir / 'cad-scenes.sqlite3').exists()

    # Startup path: SceneRepository -> ensure_native_schema -> recovery.
    with caplog.at_level(logging.WARNING, logger='htdt.native'):
        reopened = SceneRepository(data_dir / 'cad-scenes.sqlite3')

    latest = reopened.latest(first.document_id)
    assert latest is not None and latest.revision_id == first.revision_id
    assert (data_dir / 'measurement-assets' / digest).read_bytes() == raw
    _assert_no_restore_artifacts(data_dir)
    # The recovery decision is reported through the durable diagnostics log.
    assert any(
        'interrupted restore recovered' in record.message
        and 'action=completed' in record.message
        for record in caplog.records
    )


def test_interrupted_restore_rolls_back_through_repository_startup(
    tmp_path: Path, monkeypatch
):
    data_dir, _repository, first, second, digest, raw = _crash_restore(
        tmp_path, monkeypatch, boundary='after_db_evac', keep_stage=False
    )
    assert not (data_dir / 'cad-scenes.sqlite3').exists()

    reopened = SceneRepository(data_dir / 'cad-scenes.sqlite3')

    latest = reopened.latest(first.document_id)
    assert latest is not None and latest.revision_id == second.revision_id
    assert (data_dir / 'measurement-assets' / digest).read_bytes() == raw
    _assert_no_restore_artifacts(data_dir)


def test_recovery_replaces_a_fresh_seeded_database_with_restored_state(
    tmp_path: Path, monkeypatch
):
    """A foreign DB appearing during an interrupted restore is superseded."""
    data_dir, _repository, first, _second, digest, raw = _crash_restore(
        tmp_path, monkeypatch, boundary='after_db_evac'
    )
    # Simulate the legacy failure mode: something seeded an unrelated fresh
    # database where the swapped-out live database used to be.
    with closing(sqlite3.connect(data_dir / 'cad-scenes.sqlite3')) as connection:
        connection.execute('CREATE TABLE stray(id INTEGER PRIMARY KEY)')
        connection.commit()

    events = recover_interrupted_restore(data_dir)

    assert [event.action for event in events] == ['completed']
    _assert_live_state(data_dir, first.document_id, first.revision_id, digest, raw)
    _assert_no_restore_artifacts(data_dir)


def test_recovery_falls_back_to_pre_restore_archive_when_payloads_are_lost(
    tmp_path: Path, monkeypatch
):
    data_dir, _repository, first, second, digest, raw = _crash_restore(
        tmp_path, monkeypatch, boundary='after_db_evac'
    )
    # Lose the staged payload and the preserved live database: only the
    # journaled pre-restore archive still holds a valid generation.
    shutil.rmtree(_stage_dirs(data_dir)[0])
    rollback_root = _rollback_dirs(data_dir)[0]
    (rollback_root / 'cad-scenes.sqlite3').unlink()

    events = recover_interrupted_restore(data_dir)

    assert [event.action for event in events] == ['restored_from_archive']
    _assert_live_state(data_dir, first.document_id, second.revision_id, digest, raw)
    _assert_no_restore_artifacts(data_dir)


def test_recovery_refuses_to_seed_when_no_valid_generation_remains(
    tmp_path: Path, monkeypatch
):
    data_dir, _repository, first, _second, _digest, _raw = _crash_restore(
        tmp_path, monkeypatch, boundary='after_db_evac'
    )
    shutil.rmtree(_stage_dirs(data_dir)[0])
    rollback_root = _rollback_dirs(data_dir)[0]
    (rollback_root / 'cad-scenes.sqlite3').unlink()
    for archive in tmp_path.glob('*-pre-restore-*.htdt-backup'):
        archive.unlink()

    with pytest.raises(RestoreRecoveryError, match='could not be recovered'):
        recover_interrupted_restore(data_dir)

    # The failed recovery must not let startup seed an unrelated database.
    with pytest.raises(RestoreRecoveryError):
        SceneRepository(data_dir / 'cad-scenes.sqlite3')
    assert not (data_dir / 'cad-scenes.sqlite3').exists()
    assert _rollback_dirs(data_dir) != []


def test_empty_orphan_rollback_dir_is_removed(tmp_path: Path):
    data_dir = tmp_path / 'data'
    _repository, first, _digest, _raw = _seed_data(data_dir)
    orphan = tmp_path / f'.{data_dir.name}-restore-rollback-orphan'
    orphan.mkdir()

    events = recover_interrupted_restore(data_dir)

    assert [event.action for event in events] == ['orphan_removed']
    assert not orphan.exists()
    reopened = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    assert reopened.latest(first.document_id).revision_id == first.revision_id


def test_orphan_rollback_dir_with_payload_is_preserved_when_live_is_valid(
    tmp_path: Path,
):
    data_dir = tmp_path / 'data'
    _repository, _first, _digest, _raw = _seed_data(data_dir)
    orphan = tmp_path / f'.{data_dir.name}-restore-rollback-orphan'
    orphan.mkdir()
    (orphan / 'cad-scenes.sqlite3').write_bytes(b'payload')

    events = recover_interrupted_restore(data_dir)

    assert [event.action for event in events] == ['orphan_preserved']
    assert (orphan / 'cad-scenes.sqlite3').read_bytes() == b'payload'


def test_orphan_rollback_dir_recovers_missing_live_database(tmp_path: Path):
    data_dir = tmp_path / 'data'
    _repository, first, digest, raw = _seed_data(data_dir)
    orphan = tmp_path / f'.{data_dir.name}-restore-rollback-orphan'
    orphan.mkdir()
    os.replace(data_dir / 'cad-scenes.sqlite3', orphan / 'cad-scenes.sqlite3')
    os.replace(
        data_dir / 'measurement-assets', orphan / 'measurement-assets'
    )

    events = recover_interrupted_restore(data_dir)

    assert [event.action for event in events] == ['rolled_back']
    _assert_live_state(data_dir, first.document_id, first.revision_id, digest, raw)
    _assert_no_restore_artifacts(data_dir)


def test_restore_recovers_a_pending_swap_before_restoring(
    tmp_path: Path, monkeypatch
):
    """A new restore resolves leftover crash state before touching live data."""
    data_dir, _repository, first, _second, _digest, _raw = _crash_restore(
        tmp_path, monkeypatch, boundary='after_assets_evac'
    )
    other_dir = tmp_path / 'other-data'
    other_repository = SceneRepository(other_dir / 'cad-scenes.sqlite3')
    other_revision = other_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    later = tmp_path / 'later.htdt-backup'
    create_backup(other_dir, later)

    manifest, _pre = restore_backup(data_dir, later)

    reopened = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    latest = reopened.latest(first.document_id)
    assert latest is not None
    assert latest.revision_id == other_revision.revision_id
    _assert_no_restore_artifacts(data_dir)


def test_successful_restore_leaves_no_journal_or_rollback_artifacts(tmp_path: Path):
    data_dir = tmp_path / 'data'
    repository, first, _digest, _raw = _seed_data(data_dir)
    baseline = tmp_path / 'baseline.htdt-backup'
    create_backup(data_dir, baseline)
    _mutate_scene(repository, first.revision_id)

    restore_backup(data_dir, baseline)

    _assert_no_restore_artifacts(data_dir)
    assert not list(tmp_path.glob('**/restore-journal.json'))
