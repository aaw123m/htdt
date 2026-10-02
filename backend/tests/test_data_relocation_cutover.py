"""#751 relocation cutover integrity: durable journal + external lock."""

from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from htdt import data_relocation
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene
from htdt.data_relocation import (
    DataRelocationBlockedError,
    DataRelocationError,
    ManagedDataUnavailableError,
    RelocationJournal,
    assert_managed_root_available,
    execute_data_relocation,
    load_bootstrap_config,
    recover_interrupted_relocation,
)
from htdt.native_backup import DATABASE_NAME


def _seed_data_dir(data_dir: Path, document_id: str = 'doc-1') -> None:
    repository = SceneRepository(data_dir / DATABASE_NAME)
    repository.save(make_empty_scene(document_id), parent_revision_id=None)


def _journal(
    source: Path,
    destination: Path,
    staged: Path,
    parked: Path,
    phase: str,
) -> RelocationJournal:
    return RelocationJournal(
        transaction_id='txn-test',
        phase=phase,  # type: ignore[arg-type]
        source_dir=str(source),
        destination_dir=str(destination),
        staged_dir=str(staged),
        parked_dir=str(parked),
        created_at_utc='2026-01-01T00:00:00Z',
        updated_at_utc='2026-01-01T00:00:00Z',
    )


def _write_journal(
    journal: RelocationJournal, bootstrap_path: Path
) -> None:
    data_relocation._write_relocation_journal(
        journal, data_relocation._relocation_journal_path(bootstrap_path)
    )


def _journal_exists(bootstrap_path: Path) -> bool:
    return data_relocation._read_relocation_journal(
        data_relocation._relocation_journal_path(bootstrap_path)
    )


def test_journal_absent_after_successful_relocation(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    _seed_data_dir(source)
    destination = tmp_path / 'moved' / 'data'
    bootstrap = tmp_path / 'boot.json'

    execute_data_relocation(source, destination, bootstrap_path=bootstrap)

    assert _journal_exists(bootstrap) is None
    assert (destination / DATABASE_NAME).is_file()


def test_live_journal_blocks_source_and_destination(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    _seed_data_dir(source)
    destination = tmp_path / 'dest'
    bootstrap = tmp_path / 'boot.json'
    journal = _journal(
        source,
        destination,
        tmp_path / 'staged',
        tmp_path / 'source.relocated-x',
        'PREPARED',
    )
    _write_journal(journal, bootstrap)
    lock = data_relocation._RelocationLock(bootstrap)
    assert lock.acquire()
    try:
        with pytest.raises(ManagedDataUnavailableError):
            assert_managed_root_available(
                source, 'default', bootstrap_path=bootstrap
            )
        with pytest.raises(ManagedDataUnavailableError):
            assert_managed_root_available(
                destination, 'explicit', bootstrap_path=bootstrap
            )
        # An unrelated root is unaffected by the in-flight transaction.
        assert_managed_root_available(
            tmp_path / 'elsewhere', 'explicit', bootstrap_path=bootstrap
        )
    finally:
        lock.release()


def test_second_relocation_blocked_by_external_lock(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    _seed_data_dir(source)
    bootstrap = tmp_path / 'boot.json'
    lock = data_relocation._RelocationLock(bootstrap)
    assert lock.acquire()
    try:
        with pytest.raises(DataRelocationBlockedError):
            execute_data_relocation(
                source, tmp_path / 'dest', bootstrap_path=bootstrap
            )
    finally:
        lock.release()
    assert (source / DATABASE_NAME).is_file()


def test_recovery_promotes_staged_verified(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    _seed_data_dir(source)
    destination = tmp_path / 'dest'
    staged = tmp_path / 'staged'
    parked = tmp_path / 'source.relocated-x'
    bootstrap = tmp_path / 'boot.json'
    shutil.copytree(source, staged)
    _write_journal(
        _journal(source, destination, staged, parked, 'STAGED_VERIFIED'),
        bootstrap,
    )

    events = recover_interrupted_relocation(bootstrap_path=bootstrap)

    assert events
    assert (destination / DATABASE_NAME).is_file()
    assert not source.exists()
    assert (parked / DATABASE_NAME).is_file()
    config = load_bootstrap_config(bootstrap)
    assert config is not None
    assert Path(config.data_dir) == destination
    assert _journal_exists(bootstrap) is None


def test_recovery_parks_source_after_promotion(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    _seed_data_dir(source)
    destination = tmp_path / 'dest'
    parked = tmp_path / 'source.relocated-x'
    bootstrap = tmp_path / 'boot.json'
    shutil.copytree(source, destination)
    _write_journal(
        _journal(
            source,
            destination,
            tmp_path / 'gone-staged',
            parked,
            'DESTINATION_PROMOTED',
        ),
        bootstrap,
    )

    recover_interrupted_relocation(bootstrap_path=bootstrap)

    assert not source.exists()
    assert (parked / DATABASE_NAME).is_file()
    config = load_bootstrap_config(bootstrap)
    assert config is not None
    assert Path(config.data_dir) == destination


def test_recovery_writes_bootstrap_after_source_parked(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    _seed_data_dir(source)
    destination = tmp_path / 'dest'
    parked = tmp_path / 'source.relocated-x'
    bootstrap = tmp_path / 'boot.json'
    shutil.copytree(source, destination)
    shutil.move(source, parked)
    _write_journal(
        _journal(
            source,
            destination,
            tmp_path / 'gone-staged',
            parked,
            'SOURCE_PARKED',
        ),
        bootstrap,
    )

    recover_interrupted_relocation(bootstrap_path=bootstrap)

    config = load_bootstrap_config(bootstrap)
    assert config is not None
    assert Path(config.data_dir) == destination
    assert _journal_exists(bootstrap) is None


def test_recovery_aborts_pre_promotion_cleanly(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    _seed_data_dir(source)
    staged = tmp_path / 'staged'
    staged.mkdir()
    (staged / 'partial').write_bytes(b'x')
    bootstrap = tmp_path / 'boot.json'
    _write_journal(
        _journal(
            source,
            tmp_path / 'dest',
            staged,
            tmp_path / 'source.relocated-x',
            'PREPARED',
        ),
        bootstrap,
    )

    recover_interrupted_relocation(bootstrap_path=bootstrap)

    assert not staged.exists()
    assert (source / DATABASE_NAME).is_file()
    assert _journal_exists(bootstrap) is None


def _stage_database_copy(source: Path, staged: Path) -> None:
    """Reproduce the staged-copy prefix of a mid-copy crash: the SQLite
    backup has landed but nothing else has (kill -9 inside the carried-
    component copy phase, still journaled PREPARED)."""
    staged.mkdir(parents=True)
    with closing(sqlite3.connect(source / DATABASE_NAME)) as src, closing(
        sqlite3.connect(staged / DATABASE_NAME)
    ) as dst:
        src.backup(dst)
        dst.commit()


def test_recovery_never_promotes_an_unverified_prepared_stage(
    tmp_path: Path,
) -> None:
    """A PREPARED journal means the copy phase was interrupted: the staged
    root is unverified residue, never promotable — even when it happens to
    contain a complete-looking database. Promoting it would silently lose
    every carried component (commissioning plans, backup policy, upgrade
    recovery generations, diagnostics) the copy never reached."""
    source = tmp_path / 'source'
    _seed_data_dir(source)
    (source / 'commissioning-plans.json').write_text(
        json.dumps({'doc-1': ['step-a']}), encoding='utf-8'
    )
    destination = tmp_path / 'dest'
    staged = tmp_path / 'staged'
    _stage_database_copy(source, staged)
    bootstrap = tmp_path / 'boot.json'
    _write_journal(
        _journal(
            source,
            destination,
            staged,
            tmp_path / 'source.relocated-x',
            'PREPARED',
        ),
        bootstrap,
    )

    events = recover_interrupted_relocation(bootstrap_path=bootstrap)

    assert [event.action for event in events] == ['aborted_before_promotion']
    assert not staged.exists()
    assert not destination.exists()
    assert (source / DATABASE_NAME).is_file()
    assert (source / 'commissioning-plans.json').is_file()
    assert _journal_exists(bootstrap) is None


def test_recovery_aborts_a_torn_prepared_stage_instead_of_bricking(
    tmp_path: Path,
) -> None:
    """Kill -9 mid ``sqlite3.backup`` leaves a PREPARED journal over a torn
    staged database. Verification must not run on it at all: re-verifying
    threw through recovery and every subsequent startup, bricking an
    intact source behind a journal that could never clear."""
    source = tmp_path / 'source'
    _seed_data_dir(source)
    destination = tmp_path / 'dest'
    staged = tmp_path / 'staged'
    staged.mkdir()
    raw = (source / DATABASE_NAME).read_bytes()
    (staged / DATABASE_NAME).write_bytes(b'\x00' * 4096 + raw[4096:])
    bootstrap = tmp_path / 'boot.json'
    _write_journal(
        _journal(
            source,
            destination,
            staged,
            tmp_path / 'source.relocated-x',
            'PREPARED',
        ),
        bootstrap,
    )

    events = recover_interrupted_relocation(bootstrap_path=bootstrap)

    assert [event.action for event in events] == ['aborted_before_promotion']
    assert not staged.exists()
    assert not destination.exists()
    assert (source / DATABASE_NAME).is_file()
    assert _journal_exists(bootstrap) is None
    # The next launch sees a usable root, not a stale-journal brick.
    assert_managed_root_available(
        source, 'default', bootstrap_path=bootstrap
    )


def test_recovery_restores_source_when_destination_missing(
    tmp_path: Path,
) -> None:
    source = tmp_path / 'source'
    _seed_data_dir(source)
    parked = tmp_path / 'source.relocated-x'
    bootstrap = tmp_path / 'boot.json'
    shutil.move(source, parked)
    _write_journal(
        _journal(
            source,
            tmp_path / 'dest',
            tmp_path / 'gone-staged',
            parked,
            'SOURCE_PARKED',
        ),
        bootstrap,
    )

    with pytest.raises(DataRelocationError):
        recover_interrupted_relocation(bootstrap_path=bootstrap)
    # Rolled back: the pre-relocation generation is live at its old name.
    assert (source / DATABASE_NAME).is_file()
    assert not parked.exists()


def test_bootstrap_write_failure_leaves_recoverable_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'source'
    _seed_data_dir(source)
    destination = tmp_path / 'dest'
    bootstrap = tmp_path / 'boot.json'

    def _boom(*args: object, **kwargs: object) -> Path:
        raise OSError('simulated bootstrap write failure')

    monkeypatch.setattr(data_relocation, 'save_bootstrap_config', _boom)
    with pytest.raises(DataRelocationError):
        execute_data_relocation(
            source, destination, bootstrap_path=bootstrap
        )
    monkeypatch.undo()

    # Both generations intact; the journal records SOURCE_PARKED.
    assert (destination / DATABASE_NAME).is_file()
    journal = _journal_exists(bootstrap)
    assert journal is not None
    assert journal.phase == 'SOURCE_PARKED'

    # Next startup settles the switch instead of opening a blank default.
    assert_managed_root_available(
        source, 'default', bootstrap_path=bootstrap
    )
    config = load_bootstrap_config(bootstrap)
    assert config is not None
    assert Path(config.data_dir) == destination
    assert _journal_exists(bootstrap) is None


def test_root_local_file_classification(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    _seed_data_dir(source)
    # Root-coupled canonical state must move; operational residue must not.
    (source / 'automatic-backup-policy.json').write_text(
        json.dumps({'enabled': True}), encoding='utf-8'
    )
    (source / 'automatic-backup-state.json').write_text(
        json.dumps({'last': 'x'}), encoding='utf-8'
    )
    (source / 'htdt-native.log').write_text('noise', encoding='utf-8')
    (source / 'runtime.json').write_text('{}', encoding='utf-8')
    destination = tmp_path / 'dest'
    bootstrap = tmp_path / 'boot.json'

    _, parked = execute_data_relocation(
        source, destination, bootstrap_path=bootstrap
    )

    assert (destination / 'automatic-backup-policy.json').is_file()
    assert (destination / 'automatic-backup-state.json').is_file()
    # Operational residue stays in the parked generation.
    assert not (destination / 'htdt-native.log').exists()
    assert not (destination / 'runtime.json').exists()
    assert (parked / 'htdt-native.log').is_file()


def test_relocated_project_ids_unchanged(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    _seed_data_dir(source, 'doc-preserved')
    destination = tmp_path / 'dest'
    bootstrap = tmp_path / 'boot.json'
    head_before = SceneRepository(source / DATABASE_NAME).current_head(
        'doc-preserved'
    )

    execute_data_relocation(source, destination, bootstrap_path=bootstrap)

    repository = SceneRepository(destination / DATABASE_NAME)
    assert repository.current_head('doc-preserved') == head_before


def test_execute_releases_pooled_read_handles_on_source(tmp_path: Path) -> None:
    """A live repository's pooled read handle inside the source tree must
    not wedge the cutover's source rename (WinError 32 on Windows) — the
    documented caller-quiesce contract cannot see in-process handles."""
    source = tmp_path / 'source'
    repository = SceneRepository(source / DATABASE_NAME)
    repository.save(make_empty_scene('doc-1'), parent_revision_id=None)
    assert repository.latest('doc-1') is not None  # arm the pooled read conn
    destination = tmp_path / 'dest'

    _, parked = execute_data_relocation(
        source, destination, bootstrap_path=tmp_path / 'boot.json'
    )

    assert (destination / DATABASE_NAME).is_file()
    assert (parked / DATABASE_NAME).is_file()
    repository.close()


def test_recovery_releases_pooled_read_handles_on_journal_dirs(
    tmp_path: Path,
) -> None:
    """Settling a stale journal renames the source directory; a pooled
    read handle inside it must not wedge recovery either."""
    source = tmp_path / 'source'
    repository = SceneRepository(source / DATABASE_NAME)
    repository.save(make_empty_scene('doc-1'), parent_revision_id=None)
    assert repository.latest('doc-1') is not None
    destination = tmp_path / 'dest'
    parked = tmp_path / 'source.relocated-x'
    bootstrap = tmp_path / 'boot.json'
    shutil.copytree(source, destination)
    _write_journal(
        _journal(
            source,
            destination,
            tmp_path / 'gone-staged',
            parked,
            'DESTINATION_PROMOTED',
        ),
        bootstrap,
    )

    recover_interrupted_relocation(bootstrap_path=bootstrap)

    assert not source.exists()
    assert (parked / DATABASE_NAME).is_file()
    config = load_bootstrap_config(bootstrap)
    assert config is not None and Path(config.data_dir) == destination
    repository.close()
