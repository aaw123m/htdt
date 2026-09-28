"""Round-9 regression: preferences & persisted-session-state journey.

Covers the round-9 fixes:

* per-project window state (round-8 deferred item 12): state lands in
  ``window-state/<project_ref>.json`` with the global file as the legacy
  fallback, and project saves never touch the global file;
* Safe Mode completeness: ephemeral file-dialog memory (no restore, no
  persistence), no window-state save on close, and
  ``resolve_startup_document(skip_last_opened=True)`` enforcing
  ``auto_open_last_project=False``;
* persisted-data registry completeness: every HTDT-owned artifact under
  the data root is classified and relocation carries (or deliberately
  drops) each of them.
"""

from __future__ import annotations

import os
from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QLabel

from htdt import file_dialog_memory
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene
from htdt.data_relocation import execute_data_relocation
from htdt.file_dialog_memory import FileDialogMemoryStore
from htdt.native_backup import DATABASE_NAME
from htdt.persisted_data import (
    PERSISTED_DATA_REGISTRY,
    component_for_path,
)
from htdt.project_library_repository import ProjectLibraryRepository
from htdt.window_state import (
    PersistedWindowState,
    load_window_state,
    save_window_state,
    window_state_path,
)
from htdt.workflow_application import WorkflowApplicationComposition
from htdt.workflow_navigation import WorkspaceId
from htdt.workflow_shell import (
    WorkflowShellWindow,
    WorkspaceMount,
    build_canonical_workspace_registrations,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _restore_dialog_store():
    previous = file_dialog_memory._active_store
    yield
    file_dialog_memory._active_store = previous


def _composition(data_dir: Path, **kwargs) -> WorkflowApplicationComposition:
    repository = SceneRepository(data_dir / "cad-scenes.sqlite3")
    return WorkflowApplicationComposition(repository, "document-1", **kwargs)


# ---------------------------------------------------------------------------
# Per-project window state (round-8 deferred item 12)


def test_per_project_state_wins_and_records_ref(tmp_path: Path) -> None:
    global_state = PersistedWindowState(workspace='overview')
    save_window_state(tmp_path, global_state)
    project_state = PersistedWindowState(
        workspace='measurement', contexts={'room': 'layout'}
    )
    save_window_state(tmp_path, project_state, project_ref='proj-a')

    loaded = load_window_state(tmp_path, project_ref='proj-a')
    assert loaded is not None
    assert loaded.workspace == 'measurement'
    assert loaded.project_ref == 'proj-a'
    # The per-project file lives under window-state/, not at the root.
    assert window_state_path(tmp_path, 'proj-a') == (
        tmp_path / 'window-state' / 'proj-a.json'
    )
    # The global file is untouched by project saves.
    assert load_window_state(tmp_path) is not None
    assert load_window_state(tmp_path).workspace == 'overview'


def test_project_without_state_falls_back_to_global(tmp_path: Path) -> None:
    save_window_state(
        tmp_path, PersistedWindowState(workspace='activity')
    )
    loaded = load_window_state(tmp_path, project_ref='never-seen')
    assert loaded is not None
    assert loaded.workspace == 'activity'


def test_corrupt_project_state_falls_back_to_global(tmp_path: Path) -> None:
    save_window_state(
        tmp_path, PersistedWindowState(workspace='room')
    )
    window_state_path(tmp_path, 'proj-b').parent.mkdir(parents=True)
    window_state_path(tmp_path, 'proj-b').write_text(
        '{not json', encoding='utf-8'
    )
    loaded = load_window_state(tmp_path, project_ref='proj-b')
    assert loaded is not None
    assert loaded.workspace == 'room'


def test_project_ref_filename_is_sanitized(tmp_path: Path) -> None:
    save_window_state(
        tmp_path,
        PersistedWindowState(workspace='overview'),
        project_ref='../../evil:ref',
    )
    # Nothing escaped the state directory.
    assert not (tmp_path / 'evil:ref.json').exists()
    files = list((tmp_path / 'window-state').iterdir())
    assert len(files) == 1


def test_composition_close_writes_project_scoped_state(tmp_path: Path) -> None:
    app = _app()
    composition = _composition(tmp_path / 'data')
    shell = composition.shell
    shell.show()
    shell.navigate(WorkspaceId.ROOM)
    app.processEvents()
    shell.close()
    app.processEvents()

    ref = composition.project_entry.project_id
    assert not window_state_path(tmp_path / 'data').exists()
    loaded = load_window_state(tmp_path / 'data', project_ref=ref)
    assert loaded is not None
    assert loaded.workspace == 'room'


# ---------------------------------------------------------------------------
# Safe Mode completeness (#739)


def test_safe_mode_skips_window_state_save(tmp_path: Path) -> None:
    app = _app()
    composition = _composition(tmp_path / 'data', safe_mode=True)
    shell = composition.shell
    shell.show()
    app.processEvents()
    shell.close()
    app.processEvents()
    assert not window_state_path(tmp_path / 'data').exists()
    assert not (tmp_path / 'data' / 'window-state').exists()


def test_safe_mode_binds_ephemeral_dialog_store(tmp_path: Path) -> None:
    _app()
    # Pre-seed a persisted memory file — Safe Mode must not read it.
    seeded = FileDialogMemoryStore.for_data_dir(tmp_path / 'data')
    remembered = tmp_path / 'data' / 'remembered'
    remembered.mkdir(parents=True)
    seeded.remember('k', str(remembered))

    composition = _composition(tmp_path / 'data', safe_mode=True)
    store = file_dialog_memory.active_store()
    assert store.path is None  # ephemeral — bound by the composition
    assert store.recall('k') == ''
    # In-session memory still works, and nothing reaches the disk.
    store.remember('k', str(remembered))
    assert store.recall('k') == str(remembered)
    reloaded = FileDialogMemoryStore.for_data_dir(tmp_path / 'data')
    assert set(reloaded.remembered().values()) == {str(remembered)}
    composition.shell.close()


def test_normal_mode_dialog_store_persists(tmp_path: Path) -> None:
    _app()
    composition = _composition(tmp_path / 'data')
    store = file_dialog_memory.active_store()
    assert store.path == tmp_path / 'data' / 'file_dialog_dirs.json'
    composition.shell.close()


def test_skip_last_opened_opens_second_most_recent(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad-scenes.sqlite3')
    library = ProjectLibraryRepository(repository)
    first = library.create_project('First')
    second = library.create_project('Second')
    # 'First' is the most recently opened (the crash suspect).
    library.open_project(first.project_id)
    library.open_project(second.project_id)
    library.open_project(first.project_id)

    entry = library.resolve_startup_document(None, skip_last_opened=True)
    assert entry.project_id == second.project_id


def test_skip_last_opened_single_project_creates_fresh(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad-scenes.sqlite3')
    library = ProjectLibraryRepository(repository)
    suspect = library.create_project('Suspect')
    library.open_project(suspect.project_id)

    entry = library.resolve_startup_document(None, skip_last_opened=True)
    # The suspect is never auto-opened; a clean default project takes over.
    assert entry.project_id != suspect.project_id
    assert len(library.list_projects()) == 2


def test_skip_last_opened_explicit_document_still_wins(
    tmp_path: Path,
) -> None:
    repository = SceneRepository(tmp_path / 'cad-scenes.sqlite3')
    library = ProjectLibraryRepository(repository)
    entry = library.resolve_startup_document(
        'explicit-doc', skip_last_opened=True
    )
    assert entry.document_id == 'explicit-doc'


# ---------------------------------------------------------------------------
# Persisted-data registry completeness


def test_every_root_artifact_is_classified() -> None:
    """Data-root artifacts must not warn-and-strand on relocation."""
    for path in (
        'application_preferences.json',
        'window-state.json',
        'window-state/abc-123.json',
        'file_dialog_dirs.json',
        '.native-upgrade-state.json',
        'htdt-legacy-migration.journal',
        'htdt.sqlite3',
        'htdt.migrated.sqlite3',
        'htdt.migrated.sqlite3.2',
        'assets/some.bin',
        'htdt.migrated.assets/x.bin',
        'htdt.migrated.assets.3/y.bin',
        'capture-receiver/receiver-cert.pem',
        'launch-intents/pending.json',
        'runtime.json',
        'cad-scenes.sqlite3',
        'measurement-assets/deadbeef',
        'activity_history.json',
        'automatic-backup-policy.json',
        'automatic-backup-state.json',
        'upgrade-events.jsonl',
        'upgrade-recovery/gen-1',
        'diagnostics/log.txt',
        'reference_library_meta.json',
        'commissioning-plans.json',
        '.instance.lock',
        '.htdt-instance.lock',
    ):
        assert component_for_path(path) is not None, path


def _seed_data_dir(data_dir: Path, document_id: str = 'doc-1') -> None:
    repository = SceneRepository(data_dir / DATABASE_NAME)
    repository.save(make_empty_scene(document_id), parent_revision_id=None)
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


def test_relocation_carries_state_and_registry_components(
    tmp_path: Path, caplog
) -> None:
    source = tmp_path / 'source'
    _seed_data_dir(source)
    # Session/preference artifacts that used to warn-and-strand or vanish.
    (source / 'window-state.json').write_text('{}', encoding='utf-8')
    (source / 'window-state').mkdir()
    (source / 'window-state' / 'p1.json').write_text('{}', encoding='utf-8')
    (source / 'file_dialog_dirs.json').write_text('{}', encoding='utf-8')
    (source / '.native-upgrade-state.json').write_text('{}', encoding='utf-8')
    (source / 'htdt-legacy-migration.journal').write_text('{}', encoding='utf-8')
    (source / 'htdt.sqlite3').write_bytes(b'legacy')
    (source / 'htdt.migrated.sqlite3').write_bytes(b'legacy-migrated')
    (source / 'htdt.migrated.sqlite3.2').write_bytes(b'legacy-migrated-2')
    (source / 'assets').mkdir()
    (source / 'assets' / 'a.bin').write_bytes(b'x')
    (source / 'htdt.migrated.assets').mkdir()
    (source / 'capture-receiver').mkdir()
    (source / 'capture-receiver' / 'receiver-cert.pem').write_text(
        'CERT', encoding='utf-8'
    )
    # Transient: deliberately dropped, but classified (no warning).
    (source / 'launch-intents').mkdir()
    (source / 'launch-intents' / 'i.json').write_text('{}', encoding='utf-8')

    destination = tmp_path / 'moved' / 'data'
    plan, _parked = execute_data_relocation(
        source, destination, bootstrap_path=tmp_path / 'boot.json'
    )
    assert not plan.blockers

    for name in (
        'window-state.json',
        'file_dialog_dirs.json',
        '.native-upgrade-state.json',
        'htdt-legacy-migration.journal',
        'htdt.sqlite3',
        'htdt.migrated.sqlite3',
        'htdt.migrated.sqlite3.2',
    ):
        assert (destination / name).is_file(), name
    for name in (
        'window-state/p1.json',
        'assets/a.bin',
        'capture-receiver/receiver-cert.pem',
    ):
        assert (destination / name).exists(), name
    assert (destination / 'htdt.migrated.assets').is_dir()
    # Transient queue is abandoned by design.
    assert not (destination / 'launch-intents').exists()
    # And nothing registered fired the unclassified warning.
    leftover = [
        m for m in caplog.messages if 'left behind during relocation' in m
    ]
    assert leftover == []


def test_relocation_warns_on_foreign_artifacts(
    tmp_path: Path, caplog
) -> None:
    source = tmp_path / 'source'
    _seed_data_dir(source)
    (source / 'stray.txt').write_text('x', encoding='utf-8')
    (source / 'stray-dir').mkdir()
    (source / 'stray-dir' / 'nested.bin').write_bytes(b'x')

    destination = tmp_path / 'dest'
    execute_data_relocation(
        source, destination, bootstrap_path=tmp_path / 'boot.json'
    )
    warned = [
        m for m in caplog.messages if 'left behind during relocation' in m
    ]
    assert any('stray.txt' in m for m in warned)
    # Directories are covered too now — a stray tree can no longer vanish
    # silently.
    assert any('stray-dir' in m for m in warned)
