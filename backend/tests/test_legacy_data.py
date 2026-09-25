import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from htdt.cad_repository import SceneRepository
from htdt.database import Store
from htdt.legacy_data import (
    LegacyDataError,
    MIGRATED_DB_NAME,
    MIGRATION_JOURNAL_NAME,
    inspect_legacy_store,
    migrate_legacy_data,
)
from htdt.main import LegacyApiDisabledError, create_app
from htdt.project_library_repository import ProjectLibraryRepository


def _library(tmp_path: Path) -> ProjectLibraryRepository:
    return ProjectLibraryRepository(
        SceneRepository(tmp_path / 'cad-scenes.sqlite3')
    )


def _legacy_project(root: Path, name: str) -> str:
    """Create one project in the legacy store and return its id."""
    client_app = create_app(root)
    client = TestClient(client_app)
    response = client.post('/api/projects', json={'name': name})
    assert response.status_code == 201, response.text
    return str(response.json()['id'])


def test_inspect_absent_store(tmp_path: Path) -> None:
    report = inspect_legacy_store(tmp_path)
    assert report.state == 'absent'
    assert not (tmp_path / 'htdt.sqlite3').exists()


def test_inspect_empty_store(tmp_path: Path) -> None:
    Store(tmp_path)
    report = inspect_legacy_store(tmp_path)
    assert report.state == 'empty'
    assert report.table_counts.get('projects') == 0


def test_inspect_populated_store(tmp_path: Path) -> None:
    _legacy_project(tmp_path, 'Old Room')
    report = inspect_legacy_store(tmp_path)
    assert report.state == 'populated'
    assert report.table_counts['projects'] == 1


def test_inspect_migrated_store(tmp_path: Path) -> None:
    (tmp_path / 'htdt.migrated.sqlite3').write_bytes(b'archive')
    report = inspect_legacy_store(tmp_path)
    assert report.state == 'migrated'


def test_inspect_unreadable_store(tmp_path: Path) -> None:
    (tmp_path / 'htdt.sqlite3').write_bytes(b'not a sqlite database')
    report = inspect_legacy_store(tmp_path)
    assert report.state == 'unreadable'


def test_migrate_populated_store_imports_projects(tmp_path: Path) -> None:
    legacy_id = _legacy_project(tmp_path, 'Old Room')
    assets_dir = tmp_path / 'assets'
    assets_dir.mkdir(exist_ok=True)
    (assets_dir / 'orphan.txt').write_text('legacy asset')
    result = migrate_legacy_data(tmp_path, project_library=_library(tmp_path))
    assert result.state == 'migrated'
    assert result.imported_projects == 1
    assert result.skipped_existing == 0
    assert inspect_legacy_store(tmp_path).state == 'migrated'
    assert (tmp_path / 'htdt.migrated.sqlite3').is_file()
    assert (tmp_path / 'htdt.migrated.assets' / 'orphan.txt').is_file()
    assert not (tmp_path / 'htdt.sqlite3').exists()
    entry = _library(tmp_path).list_projects(include_archived=True)[0]
    assert entry.display_name == 'Old Room（レガシー移行）'
    assert legacy_id in (entry.description or '')


def test_migrate_empty_store_archives_without_projects(tmp_path: Path) -> None:
    Store(tmp_path)
    result = migrate_legacy_data(tmp_path, project_library=_library(tmp_path))
    assert result.state == 'migrated'
    assert result.imported_projects == 0
    assert (tmp_path / 'htdt.migrated.sqlite3').is_file()
    assert _library(tmp_path).list_projects(include_archived=True) == ()


def test_migrate_absent_store_is_noop(tmp_path: Path) -> None:
    result = migrate_legacy_data(tmp_path, project_library=_library(tmp_path))
    assert result.state == 'nothing_to_migrate'


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    _legacy_project(tmp_path, 'Old Room')
    first = migrate_legacy_data(tmp_path, project_library=_library(tmp_path))
    assert first.imported_projects == 1
    second = migrate_legacy_data(tmp_path, project_library=_library(tmp_path))
    assert second.state == 'already_migrated'
    assert len(_library(tmp_path).list_projects(include_archived=True)) == 1


def test_migrate_unreadable_store_fails_closed(tmp_path: Path) -> None:
    (tmp_path / 'htdt.sqlite3').write_bytes(b'not a sqlite database')
    with pytest.raises(LegacyDataError):
        migrate_legacy_data(tmp_path, project_library=_library(tmp_path))


def test_failed_rename_keeps_journal_and_reports_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rename that fails halfway must leave the journal in place so the
    next inspection reports 'interrupted' instead of guessing across a
    torn live/archived boundary (#759)."""

    _legacy_project(tmp_path, 'Old Room')
    assets_dir = tmp_path / 'assets'
    assets_dir.mkdir(exist_ok=True)
    (assets_dir / 'orphan.txt').write_text('legacy asset')

    real_replace = os.replace

    def _fail_db_rename(src, dst):
        if Path(dst).name == MIGRATED_DB_NAME:
            raise OSError('simulated rename failure')
        return real_replace(src, dst)

    monkeypatch.setattr(os, 'replace', _fail_db_rename)
    with pytest.raises(OSError):
        migrate_legacy_data(tmp_path, project_library=_library(tmp_path))
    monkeypatch.setattr(os, 'replace', real_replace)

    # The journal survived the half-applied rename: inspection reports
    # 'interrupted' and re-running migration fails closed.
    assert (tmp_path / MIGRATION_JOURNAL_NAME).is_file()
    assert inspect_legacy_store(tmp_path).state == 'interrupted'
    with pytest.raises(LegacyDataError):
        migrate_legacy_data(tmp_path, project_library=_library(tmp_path))


def test_second_migration_uses_numbered_archive_generation(
    tmp_path: Path,
) -> None:
    """A recreated live store must not clobber the existing archive —
    ``_next_available`` lands it as a numbered generation (#759)."""

    _legacy_project(tmp_path, 'Old Room')
    migrate_legacy_data(tmp_path, project_library=_library(tmp_path))
    first_archive = (tmp_path / MIGRATED_DB_NAME).read_bytes()

    # The retired browser build ran again and left a fresh live store.
    _legacy_project(tmp_path, 'New Session')
    result = migrate_legacy_data(tmp_path, project_library=_library(tmp_path))

    assert result.state == 'migrated'
    assert result.imported_projects == 1
    assert (tmp_path / MIGRATED_DB_NAME).read_bytes() == first_archive
    numbered = tmp_path / f'{MIGRATED_DB_NAME}.1'
    assert numbered.is_file()
    assert result.archived_db_path == str(numbered)


def test_legacy_api_refuses_default_root_without_optin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv('HTDT_LEGACY_API', raising=False)
    with pytest.raises(LegacyApiDisabledError):
        create_app()


def test_legacy_api_explicit_dir_and_optin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert create_app(tmp_path) is not None
    # Point the default root at the isolated dir so the opt-in path never
    # touches the machine's real data directory.
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setenv('HTDT_LEGACY_API', '1')
    app = create_app()
    assert app is not None
    assert (tmp_path / 'HomeTheaterDigitalTwin' / 'htdt.sqlite3').is_file()
