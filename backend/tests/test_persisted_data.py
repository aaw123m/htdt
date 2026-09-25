"""Persisted-data lifecycle registry tests (#769)."""

from __future__ import annotations

from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3

import pytest

from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.data_management import DataManagementBackend
from htdt.data_relocation import execute_data_relocation
from htdt.native_backup import create_backup, restore_backup
from htdt.persisted_data import (
    PERSISTED_DATA_REGISTRY,
    BackupPolicy,
    PersistedDataLifecycle,
    RelocationPolicy,
    auxiliary_archive_path,
    backup_excluded_names,
    backup_included_components,
    component_for_path,
    relocation_carried_components,
)


# Every HTDT-owned file/store a data directory can hold. The inventory test
# exists so new persisted files fail CI until their lifecycle is declared.
_KNOWN_PERSISTED_PATHS = {
    'cad-scenes.sqlite3',
    'measurement-assets',
    'commissioning-plans.json',
    'application_preferences.json',
    'reference_library_meta.json',
    'activity_history.json',
    'automatic-backup-policy.json',
    'automatic-backup-state.json',
    'upgrade-events.jsonl',
    'upgrade-recovery',
    'diagnostics',
    'runtime.json',
    '.instance.lock',
    '.htdt-instance.lock',
}


def _seed_data(data_dir: Path) -> None:
    repository = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    CadMeasurementRepository(repository)  # creates the asset table
    raw = b'persisted-data fixture\n'
    digest = sha256(raw).hexdigest()
    asset = data_dir / 'measurement-assets' / digest
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(raw)
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            '''INSERT INTO cad_measurement_assets(
                sha256, filename, relative_path, size_bytes
            ) VALUES (?, ?, ?, ?)''',
            (digest, 'fixture.txt', f'measurement-assets/{digest}', len(raw)),
        )


def test_registry_classifies_every_htdt_owned_persisted_file() -> None:
    """Inventory: every file the app can persist has a declared lifecycle."""
    registered = {component.path for component in PERSISTED_DATA_REGISTRY}
    assert registered == _KNOWN_PERSISTED_PATHS
    assert len({component.name for component in PERSISTED_DATA_REGISTRY}) == len(
        PERSISTED_DATA_REGISTRY
    )
    for path in _KNOWN_PERSISTED_PATHS:
        assert component_for_path(path) is not None


def test_commissioning_plans_survive_migration_and_transfer() -> None:
    """The commissioning plan is project auxiliary data: backup, relocation
    and portable export must all carry it — never silently lost (#769)."""
    component = component_for_path('commissioning-plans.json')
    assert component is not None
    assert component.lifecycle is PersistedDataLifecycle.PROJECT_AUXILIARY
    assert component.backup is BackupPolicy.INCLUDE
    assert component.relocation is RelocationPolicy.CARRY
    assert component in backup_included_components()
    assert auxiliary_archive_path(component) == 'auxiliary/commissioning-plans.json'


def test_transient_components_are_never_copied_or_restored() -> None:
    for path in ('runtime.json', '.instance.lock', '.htdt-instance.lock'):
        component = component_for_path(path)
        assert component is not None
        assert component.lifecycle is PersistedDataLifecycle.TRANSIENT
        assert component.backup is BackupPolicy.EXCLUDE
        assert component.relocation is RelocationPolicy.NEVER_COPY
        assert component not in relocation_carried_components()


def test_backup_carries_commissioning_plans_and_restore_recovers_them(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / 'data'
    _seed_data(data_dir)
    plans = data_dir / 'commissioning-plans.json'
    plans.write_text('{"plan": {"phases": 3}}', encoding='utf-8')

    manifest = create_backup(data_dir, tmp_path / 'with-plans.htdt-backup')
    auxiliary = [e for e in manifest.files if e.kind == 'auxiliary']
    assert {e.path for e in auxiliary} == {'auxiliary/commissioning-plans.json'}

    plans.write_text('{"plan": {"phases": 1}}', encoding='utf-8')
    restored, _ = restore_backup(data_dir, tmp_path / 'with-plans.htdt-backup')
    assert restored == manifest
    assert plans.read_text(encoding='utf-8') == '{"plan": {"phases": 3}}'


def test_restore_removes_live_auxiliary_file_absent_from_backup(
    tmp_path: Path,
) -> None:
    """Whole-data restore owns auxiliary state exactly: a live file the
    archive does not carry must not survive the restore."""
    data_dir = tmp_path / 'data'
    _seed_data(data_dir)
    backup_path = tmp_path / 'without-plans.htdt-backup'
    create_backup(data_dir, backup_path)

    plans = data_dir / 'commissioning-plans.json'
    plans.write_text('{"plan": {}}', encoding='utf-8')

    restore_backup(data_dir, backup_path)
    assert not plans.exists()


def test_relocation_carries_root_tied_components_but_not_transients(
    tmp_path: Path,
) -> None:
    source = tmp_path / 'source-data'
    destination = tmp_path / 'moved-data'
    _seed_data(source)
    (source / 'commissioning-plans.json').write_text('{"plan":{}}')
    (source / 'application_preferences.json').write_text('{"theme":"dark"}')
    (source / 'automatic-backup-policy.json').write_text('{"enabled":true}')
    recovery = source / 'upgrade-recovery' / 'gen-1'
    recovery.mkdir(parents=True)
    (recovery / 'snapshot.json').write_text('{}')
    runtime = source / 'runtime.json'
    runtime.write_text('{"pid": 1}')

    execute_data_relocation(
        source, destination, bootstrap_path=tmp_path / 'htdt-bootstrap.json'
    )

    assert (destination / 'commissioning-plans.json').is_file()
    assert (destination / 'application_preferences.json').is_file()
    assert (destination / 'automatic-backup-policy.json').is_file()
    assert (destination / 'upgrade-recovery' / 'gen-1' / 'snapshot.json').is_file()
    assert not (destination / 'runtime.json').exists()
    # The relocation's own instance lock must never travel either.
    assert not (destination / '.instance.lock').exists()


def test_backup_preview_declares_included_and_excluded_categories(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / 'data'
    _seed_data(data_dir)
    (data_dir / 'commissioning-plans.json').write_text('{"plan":{}}')
    backend = DataManagementBackend(data_dir)

    result = backend.create_backup(tmp_path / 'preview.htdt-backup')
    preview = backend.preview_restore(result.metadata.backup_path)

    assert 'auxiliary/commissioning-plans.json' in (
        preview.metadata.auxiliary_components
    )
    excluded = preview.metadata.excluded_categories
    assert excluded == backup_excluded_names()
    assert 'application_preferences' in excluded
    assert 'commissioning_plans' not in excluded
    # Transient files are not even a "declared exclusion" — they are
    # outside backup semantics entirely.
    assert 'runtime_state' not in excluded
