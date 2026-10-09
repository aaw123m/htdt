"""Persisted-data lifecycle registry (#769).

One canonical classification of every HTDT-owned file/store under the data
root. Backup, relocation, portable-project export, uninstall and support
operations consume this registry instead of maintaining independent path
lists.

Lifecycle classes (issue §1):

* ``CANONICAL_AUTHORITY`` — the SQLite database and managed content-
  addressed evidence assets. Whole-data backup/restore, relocation and
  integrity checks must cover them.
* ``PROJECT_AUXILIARY`` — durable project-scoped metadata stored outside
  the DB (today: ``commissioning-plans.json``). Whole-data backup,
  relocation and portable export must carry it so PC/project migration
  never silently loses setup progress.
* ``PREFERENCES`` — user/machine-local preferences; excluded from whole-
  data backup deliberately, carried on same-PC root relocation.
* ``LIBRARY_METADATA`` — user library presentation state (archive/hide).
* ``OPERATIONAL`` — app/root-local operational state: activity history,
  backup policy/scheduler state, upgrade journal, diagnostics logs.
* ``RECOVERY_GENERATION`` — recovery material; relocation carries it so
  the only recovery generation is never stranded.
* ``TRANSIENT`` — runtime locks/coordination files; never restored or
  imported as live ownership state.

Policies are declared per component for backup, relocation and portable
export so "whole-data backup" has a product-defined meaning rather than
whatever an operation happens to enumerate.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Literal


class PersistedDataLifecycle(StrEnum):
    CANONICAL_AUTHORITY = 'canonical_authority'
    PROJECT_AUXILIARY = 'project_auxiliary'
    PREFERENCES = 'preferences'
    LIBRARY_METADATA = 'library_metadata'
    OPERATIONAL = 'operational'
    RECOVERY_GENERATION = 'recovery_generation'
    TRANSIENT = 'transient'


class BackupPolicy(StrEnum):
    #: Carried inside whole-data backup archives and restored by the swap.
    INCLUDE = 'include'
    #: Deliberately outside the backup contract — a declared omission, not
    #: an accident of enumeration.
    EXCLUDE = 'exclude'


class RelocationPolicy(StrEnum):
    #: Same-PC managed-root move preserves the file/directory.
    CARRY = 'carry'
    #: Transient — must never be copied or restored as live state.
    NEVER_COPY = 'never_copy'


class PortableProjectPolicy(StrEnum):
    #: Project-scoped: exported with the project bundle.
    INCLUDE = 'include'
    #: Project-adjacent but omitted by declared policy (capability loss).
    OMIT_DECLARED = 'omit_declared'
    #: Not project data at all — outside bundle semantics.
    NOT_PROJECT_DATA = 'not_project_data'


@dataclass(frozen=True, slots=True)
class PersistedDataComponent:
    """One HTDT-owned persistent file/store under the managed data root."""

    name: str
    path: str
    lifecycle: PersistedDataLifecycle
    scope: Literal['application', 'project', 'root', 'transient']
    backup: BackupPolicy
    relocation: RelocationPolicy
    portable: PortableProjectPolicy
    is_directory: bool = False
    sensitive: bool = False
    notes: str = ''

    def __post_init__(self) -> None:
        if not self.name or not self.path:
            raise ValueError('persisted-data component requires name and path')
        if self.path.startswith(('/', '\\')) or '..' in self.path.split('/'):
            raise ValueError(f'component path must be root-relative: {self.path}')


PERSISTED_DATA_REGISTRY: tuple[PersistedDataComponent, ...] = (
    PersistedDataComponent(
        name='database',
        path='cad-scenes.sqlite3',
        lifecycle=PersistedDataLifecycle.CANONICAL_AUTHORITY,
        scope='project',
        backup=BackupPolicy.INCLUDE,
        relocation=RelocationPolicy.CARRY,
        portable=PortableProjectPolicy.INCLUDE,
    ),
    PersistedDataComponent(
        name='managed_assets',
        path='measurement-assets',
        lifecycle=PersistedDataLifecycle.CANONICAL_AUTHORITY,
        scope='project',
        backup=BackupPolicy.INCLUDE,
        relocation=RelocationPolicy.CARRY,
        portable=PortableProjectPolicy.INCLUDE,
        is_directory=True,
    ),
    PersistedDataComponent(
        name='commissioning_plans',
        path='commissioning-plans.json',
        lifecycle=PersistedDataLifecycle.PROJECT_AUXILIARY,
        scope='project',
        backup=BackupPolicy.INCLUDE,
        relocation=RelocationPolicy.CARRY,
        portable=PortableProjectPolicy.INCLUDE,
        notes=(
            'Per-project commissioning orchestration keyed by document_id; '
            'backup archives store it as auxiliary/commissioning-plans.json'
        ),
    ),
    PersistedDataComponent(
        name='application_preferences',
        path='application_preferences.json',
        lifecycle=PersistedDataLifecycle.PREFERENCES,
        scope='application',
        backup=BackupPolicy.EXCLUDE,
        relocation=RelocationPolicy.CARRY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
        notes='User/machine-local preferences; deliberately not backup data.',
    ),
    PersistedDataComponent(
        name='reference_library_meta',
        path='reference_library_meta.json',
        lifecycle=PersistedDataLifecycle.LIBRARY_METADATA,
        scope='application',
        backup=BackupPolicy.EXCLUDE,
        relocation=RelocationPolicy.CARRY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
        notes='Library archive/hide presentation state follows the install.',
    ),
    PersistedDataComponent(
        name='window_state_global',
        path='window-state.json',
        lifecycle=PersistedDataLifecycle.PREFERENCES,
        scope='application',
        backup=BackupPolicy.EXCLUDE,
        relocation=RelocationPolicy.CARRY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
        notes=(
            'Global shell geometry/workspace state; the fallback for '
            'projects without their own record.'
        ),
    ),
    PersistedDataComponent(
        name='window_state_projects',
        path='window-state',
        lifecycle=PersistedDataLifecycle.PREFERENCES,
        scope='application',
        backup=BackupPolicy.EXCLUDE,
        relocation=RelocationPolicy.CARRY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
        is_directory=True,
        notes='Per-project window state files keyed by project id (round-9).',
    ),
    PersistedDataComponent(
        name='file_dialog_memory',
        path='file_dialog_dirs.json',
        lifecycle=PersistedDataLifecycle.PREFERENCES,
        scope='application',
        backup=BackupPolicy.EXCLUDE,
        relocation=RelocationPolicy.CARRY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
        notes='Last-used directories for native file dialogs; convenience only.',
    ),
    PersistedDataComponent(
        name='activity_history',
        path='activity_history.json',
        lifecycle=PersistedDataLifecycle.OPERATIONAL,
        scope='application',
        backup=BackupPolicy.EXCLUDE,
        relocation=RelocationPolicy.CARRY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
        notes='Operation history; support-relevant, never project evidence.',
    ),
    PersistedDataComponent(
        name='automatic_backup_policy',
        path='automatic-backup-policy.json',
        lifecycle=PersistedDataLifecycle.OPERATIONAL,
        scope='root',
        backup=BackupPolicy.EXCLUDE,
        relocation=RelocationPolicy.CARRY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
    ),
    PersistedDataComponent(
        name='automatic_backup_state',
        path='automatic-backup-state.json',
        lifecycle=PersistedDataLifecycle.OPERATIONAL,
        scope='root',
        backup=BackupPolicy.EXCLUDE,
        relocation=RelocationPolicy.CARRY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
    ),
    PersistedDataComponent(
        name='restore_drill_journal',
        path='restore-drill-results.jsonl',
        lifecycle=PersistedDataLifecycle.OPERATIONAL,
        scope='root',
        backup=BackupPolicy.EXCLUDE,
        relocation=RelocationPolicy.CARRY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
        notes=(
            'Isolated restore drill journal (#992); operational evidence '
            'about backup restorability, never bundled into backups.'
        ),
    ),
    PersistedDataComponent(
        name='upgrade_events',
        path='upgrade-events.jsonl',
        lifecycle=PersistedDataLifecycle.OPERATIONAL,
        scope='root',
        backup=BackupPolicy.EXCLUDE,
        relocation=RelocationPolicy.CARRY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
        notes='Upgrade journal/provenance.',
    ),
    PersistedDataComponent(
        name='upgrade_state_marker',
        path='.native-upgrade-state.json',
        lifecycle=PersistedDataLifecycle.OPERATIONAL,
        scope='root',
        backup=BackupPolicy.EXCLUDE,
        relocation=RelocationPolicy.CARRY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
        notes=(
            'Durable upgrade-quarantine marker (#750). Dropping it on a '
            'root move would let the destination launch without honoring '
            'an unresolved upgrade — must travel with the root.'
        ),
    ),
    PersistedDataComponent(
        name='upgrade_recovery',
        path='upgrade-recovery',
        lifecycle=PersistedDataLifecycle.RECOVERY_GENERATION,
        scope='root',
        backup=BackupPolicy.EXCLUDE,
        relocation=RelocationPolicy.CARRY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
        is_directory=True,
        notes='Recovery generations must never be stranded by a root move.',
    ),
    PersistedDataComponent(
        name='legacy_migration_journal',
        path='htdt-legacy-migration.journal',
        lifecycle=PersistedDataLifecycle.OPERATIONAL,
        scope='root',
        backup=BackupPolicy.EXCLUDE,
        relocation=RelocationPolicy.CARRY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
        notes=(
            'Legacy archive crash journal; its presence means "interrupted '
            'mid-rename" — stranded at the old root, that signal is lost.'
        ),
    ),
    PersistedDataComponent(
        name='legacy_database',
        path='htdt.sqlite3',
        lifecycle=PersistedDataLifecycle.OPERATIONAL,
        scope='root',
        backup=BackupPolicy.EXCLUDE,
        relocation=RelocationPolicy.CARRY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
        sensitive=True,
        notes=(
            'Retired browser-authority store awaiting --migrate-legacy-data; '
            'a root move must carry it or the user loses that path.'
        ),
    ),
    PersistedDataComponent(
        name='legacy_database_archives',
        path='htdt.migrated.sqlite3',
        lifecycle=PersistedDataLifecycle.OPERATIONAL,
        scope='root',
        backup=BackupPolicy.EXCLUDE,
        relocation=RelocationPolicy.CARRY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
        sensitive=True,
        notes=(
            'Archived migrated stores; numbered siblings '
            '(htdt.migrated.sqlite3.N) are carried by the relocation '
            'sibling rule.'
        ),
    ),
    PersistedDataComponent(
        name='legacy_assets',
        path='assets',
        lifecycle=PersistedDataLifecycle.OPERATIONAL,
        scope='root',
        backup=BackupPolicy.EXCLUDE,
        relocation=RelocationPolicy.CARRY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
        is_directory=True,
        sensitive=True,
        notes='Retired legacy asset directory paired with htdt.sqlite3.',
    ),
    PersistedDataComponent(
        name='legacy_assets_archives',
        path='htdt.migrated.assets',
        lifecycle=PersistedDataLifecycle.OPERATIONAL,
        scope='root',
        backup=BackupPolicy.EXCLUDE,
        relocation=RelocationPolicy.CARRY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
        is_directory=True,
        sensitive=True,
        notes='Archived legacy asset directories (numbered siblings carried).',
    ),
    PersistedDataComponent(
        name='capture_receiver_state',
        path='capture-receiver',
        lifecycle=PersistedDataLifecycle.OPERATIONAL,
        scope='root',
        backup=BackupPolicy.EXCLUDE,
        relocation=RelocationPolicy.CARRY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
        is_directory=True,
        sensitive=True,
        notes=(
            'Capture receiver TLS identity (cert/key) + runtime state; '
            'carried so a root move keeps pairings, excluded from backups '
            'so credentials never enter archives.'
        ),
    ),
    PersistedDataComponent(
        name='diagnostics',
        path='diagnostics',
        lifecycle=PersistedDataLifecycle.OPERATIONAL,
        scope='root',
        backup=BackupPolicy.EXCLUDE,
        relocation=RelocationPolicy.CARRY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
        is_directory=True,
        sensitive=True,
        notes='Logs/support data; preserved on relocation, never bundled.',
    ),
    PersistedDataComponent(
        name='runtime_state',
        path='runtime.json',
        lifecycle=PersistedDataLifecycle.TRANSIENT,
        scope='transient',
        backup=BackupPolicy.EXCLUDE,
        relocation=RelocationPolicy.NEVER_COPY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
    ),
    PersistedDataComponent(
        name='instance_lock',
        path='.instance.lock',
        lifecycle=PersistedDataLifecycle.TRANSIENT,
        scope='transient',
        backup=BackupPolicy.EXCLUDE,
        relocation=RelocationPolicy.NEVER_COPY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
    ),
    PersistedDataComponent(
        name='htdt_instance_lock',
        path='.htdt-instance.lock',
        lifecycle=PersistedDataLifecycle.TRANSIENT,
        scope='transient',
        backup=BackupPolicy.EXCLUDE,
        relocation=RelocationPolicy.NEVER_COPY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
    ),
    PersistedDataComponent(
        name='launch_intents_queue',
        path='launch-intents',
        lifecycle=PersistedDataLifecycle.TRANSIENT,
        scope='transient',
        backup=BackupPolicy.EXCLUDE,
        relocation=RelocationPolicy.NEVER_COPY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
        is_directory=True,
        notes=(
            'Single-instance intent drop queue; undelivered items are '
            'abandoned by design, never moved as live state.'
        ),
    ),
)


#: Numbered-sibling rule: archive helpers keep extra generations beside a
#: component as ``<path>.<n>`` (e.g. ``htdt.migrated.sqlite3.2``); such
#: siblings are HTDT-owned and follow the base component's classification.
_NUMBERED_SUFFIX = re.compile(r'^(?P<base>.+)\.\d+$')


def _component_base_match(normalized: str) -> PersistedDataComponent | None:
    match = _NUMBERED_SUFFIX.match(normalized.split('/')[0])
    if match is None:
        return None
    base = match.group('base')
    for component in PERSISTED_DATA_REGISTRY:
        if base == component.path:
            return component
    return None


def backup_included_components() -> tuple[PersistedDataComponent, ...]:
    """Auxiliary components whose files enter whole-data backup archives.

    Canonical authority (database, managed assets) is handled by the
    dedicated archive pipeline; this returns the *auxiliary* set the
    manifest must carry under ``auxiliary/<component path>``.
    """
    return tuple(
        component
        for component in PERSISTED_DATA_REGISTRY
        if component.backup is BackupPolicy.INCLUDE
        and component.lifecycle is not PersistedDataLifecycle.CANONICAL_AUTHORITY
    )


def auxiliary_archive_path(component: PersistedDataComponent) -> str:
    """Archive member path for an auxiliary backup component."""
    return f'auxiliary/{component.path}'


def auxiliary_component_for_archive_path(
    archive_path: str,
) -> PersistedDataComponent | None:
    """Resolve an ``auxiliary/`` archive member back to its component."""
    for component in backup_included_components():
        if auxiliary_archive_path(component) == archive_path:
            return component
    return None


def relocation_carried_components() -> tuple[PersistedDataComponent, ...]:
    """Root-tied components a same-PC managed-root move preserves.

    Canonical authority is carried by the relocation pipeline itself; this
    returns the additional root-tied files/dirs that must be copied.
    """
    return tuple(
        component
        for component in PERSISTED_DATA_REGISTRY
        if component.relocation is RelocationPolicy.CARRY
        and component.lifecycle is not PersistedDataLifecycle.CANONICAL_AUTHORITY
    )


def backup_excluded_names() -> tuple[str, ...]:
    """Names of components deliberately excluded from whole-data backup.

    Backup previews present these as *declared* exclusions so "whole-data"
    is a defined contract, not whatever the archive happens to enumerate.
    """
    return tuple(
        component.name
        for component in PERSISTED_DATA_REGISTRY
        if component.backup is BackupPolicy.EXCLUDE
        and component.lifecycle
        is not PersistedDataLifecycle.TRANSIENT
    )


def component_for_path(path: str) -> PersistedDataComponent | None:
    """Classify a data-root-relative path; None when HTDT does not own it.

    Plain match: the component's own path or anything below it. Numbered
    sibling: ``<component path>.<n>`` — archive helpers keep extra
    generations beside the canonical name; they inherit the base
    component's policy instead of warning as unclassified.
    """
    normalized = path.replace('\\', '/')
    while normalized.startswith('./'):
        normalized = normalized[2:]
    for component in PERSISTED_DATA_REGISTRY:
        if normalized == component.path:
            return component
        if normalized.startswith(component.path + '/'):
            return component
    return _component_base_match(normalized)


__all__ = [
    'PERSISTED_DATA_REGISTRY',
    'BackupPolicy',
    'PersistedDataComponent',
    'PersistedDataLifecycle',
    'PortableProjectPolicy',
    'RelocationPolicy',
    'auxiliary_archive_path',
    'auxiliary_component_for_archive_path',
    'backup_excluded_names',
    'backup_included_components',
    'component_for_path',
    'relocation_carried_components',
]
