"""Startup Recovery / Safe Mode launch-state authority (#739).

HTDT has strong crash diagnostics (#311), unclean-session evidence
(#604 ``previous_session_unexpected_end``), preference fallback (#591)
and upgrade protection (#606), but until now no product-level decision
about *when* to offer a Recovery Launch and *what* Safe Mode means.

This module is the CI-testable, read-only half of that feature:

- :func:`classify_startup_failure` sorts a launch failure into an honest
  class so recovery guidance distinguishes renderer/config crashes from
  real project-data problems — a backup restore is recommended only when
  evidence says data recovery is relevant.
- :class:`RecoveryMetadata` is a bounded, privacy-safe app-local record
  of recent launches (build id, launch mode, project ref, workspace,
  crash correlation id, clean/unclean exit) — never raw project data.
- :func:`decide_launch` turns startup evidence (unclean previous session,
  repeated failure for the same build/data root, an explicit Safe Mode
  request, a renderer-init failure) into a typed ``LaunchDecision`` —
  ``normal`` / ``recovery_offered`` / ``safe_mode`` with the recovery
  surface choices and whether a safe-mode policy applies.
- :class:`SafeModePolicy` declares what Safe Mode skips (auto-open of the
  last project, auto-run jobs, live integrations, saved-layout restore)
  while keeping project authority strictly read-only.
- ``RESET_SCOPES`` enumerates the narrowly scoped, previewable reset
  actions (window layout, preferences, optional integrations); none of
  them touches SceneRevision, Measurements, Capture evidence,
  SystemVariants or any canonical authority.

Recovery drafts (#610) are never discarded by anything here.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .export_io import write_text_atomic
from .diagnostics_support import diagnostics_dir


RECOVERY_SCHEMA_VERSION = 1
RECOVERY_METADATA_NAME = 'recovery-launch-metadata.json'
# Bounded operational metadata: a short rolling window of launches, never
# raw project data and never unbounded history.
MAX_RECOVERY_RECORDS = 10
# Two or more consecutive unclean exits of the same build is the
# "repeated startup failure" trigger from the issue's trigger policy.
REPEATED_FAILURE_THRESHOLD = 2


StartupFailureClass = Literal[
    'renderer_initialization',
    'preference_state',
    'integration_initialization',
    'project_data',
    'schema_incompatibility',
    'migration_failure',
    'unknown',
]

#: Failure classes where a backup/data restore is actually relevant.
DATA_RELEVANT_FAILURES: frozenset[str] = frozenset(
    {'project_data', 'schema_incompatibility', 'migration_failure'}
)

LaunchMode = Literal['normal', 'recovery_offered', 'safe_mode']

RecoveryChoice = Literal[
    'open_normal',
    'open_safe_mode',
    'open_diagnostics',
    'restore_backup',
    'verify_data',
    'choose_another_project',
]


def classify_startup_failure(evidence: str | BaseException) -> StartupFailureClass:
    """Classify a launch failure from its text — honest, never guessed.

    Only an explicit keyword match earns a specific class; anything else
    stays ``unknown`` rather than being blamed on the wrong subsystem.
    """

    text = str(evidence).lower()
    if any(
        token in text
        for token in (
            'vtk', 'opengl', 'renderer', 'render window', 'gpu', 'mesa',
        )
    ):
        return 'renderer_initialization'
    if 'incompatible_newer' in text or (
        'schema v' in text and 'newer' in text
    ):
        return 'schema_incompatibility'
    if any(
        token in text
        for token in (
            'quarantine', 'migration', 'upgrade', 'verification failed',
            'committed',
        )
    ):
        return 'migration_failure'
    if any(
        token in text
        for token in ('preference', 'settings file', 'layout state')
    ):
        return 'preference_state'
    if any(
        token in text
        for token in (
            'rew', 'capture receiver', 'device adapter',
            'integration',
        )
    ):
        return 'integration_initialization'
    if any(
        token in text
        for token in (
            'scenerevision', 'scene_revision', 'authority graph',
            'document_id', 'corrupt project', 'project data',
        )
    ):
        return 'project_data'
    return 'unknown'


# ---------------------------------------------------------------------------
# Bounded recovery metadata


class RecoveryLaunchRecord(BaseModel):
    """One bounded launch-metadata record (#739 §8)."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = RECOVERY_SCHEMA_VERSION
    launch_id: str = Field(min_length=1)
    build_id: str | None = None
    launch_mode: LaunchMode
    started_at_utc: str = Field(min_length=1)
    #: None = still running / ended without cleanup evidence.
    clean_exit: bool | None = None
    last_project_ref: str | None = None
    last_workspace: str | None = None
    crash_correlation_id: str | None = None
    failure_class: StartupFailureClass | None = None


class RecoveryMetadata(BaseModel):
    """The rolling bounded launch history for one data root."""

    model_config = ConfigDict(frozen=True)

    records: tuple[RecoveryLaunchRecord, ...] = ()

    def repeated_startup_failures(self, build_id: str | None) -> int:
        """Consecutive non-clean launches of the same build, newest first."""

        count = 0
        for record in reversed(self.records):
            if record.build_id != build_id:
                break
            if record.clean_exit:
                break
            count += 1
        return count

    def last_failure_class(self, build_id: str | None) -> StartupFailureClass | None:
        """Most recent classified failure for THIS build's launch streak.

        Same scope rule as ``repeated_startup_failures``: a clean exit or a
        record from another build ends the streak — a different build's
        failure class must not be attributed to this launch's decision.
        """

        for record in reversed(self.records):
            if record.clean_exit or record.build_id != build_id:
                return None
            if record.failure_class is not None:
                return record.failure_class
        return None


def _metadata_path(data_dir) -> 'Path':
    from pathlib import Path

    return diagnostics_dir(Path(data_dir)) / RECOVERY_METADATA_NAME


def load_recovery_metadata(data_dir) -> RecoveryMetadata:
    path = _metadata_path(data_dir)
    try:
        return RecoveryMetadata.model_validate_json(
            path.read_text(encoding='utf-8')
        )
    except (OSError, ValueError):
        return RecoveryMetadata()


def _store_metadata(data_dir, metadata: RecoveryMetadata) -> None:
    path = _metadata_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, metadata.model_dump_json())


def record_launch(
    data_dir,
    *,
    build_id: str | None,
    launch_mode: LaunchMode,
    started_at_utc: str,
    project_ref: str | None = None,
    workspace: str | None = None,
    crash_correlation_id: str | None = None,
) -> RecoveryLaunchRecord:
    """Persist a bounded launch record BEFORE risky init runs."""

    record = RecoveryLaunchRecord(
        launch_id=uuid4().hex,
        build_id=build_id,
        launch_mode=launch_mode,
        started_at_utc=started_at_utc,
        last_project_ref=project_ref,
        last_workspace=workspace,
        crash_correlation_id=crash_correlation_id,
    )
    metadata = load_recovery_metadata(data_dir)
    _store_metadata(
        data_dir,
        RecoveryMetadata(
            records=(metadata.records + (record,))[-MAX_RECOVERY_RECORDS:]
        ),
    )
    return record


def annotate_launch(
    data_dir,
    launch_id: str,
    *,
    project_ref: str | None = None,
    workspace: str | None = None,
) -> None:
    """Attach the project/workspace this launch went on to open.

    ``record_launch`` runs before the project library resolves, so the
    record starts without identity context; annotating once the project is
    known lets the next recovery surface name the project that was being
    opened when the session died.
    """

    metadata = load_recovery_metadata(data_dir)
    _store_metadata(
        data_dir,
        RecoveryMetadata(
            records=tuple(
                (
                    record.model_copy(
                        update={
                            'last_project_ref': project_ref
                            or record.last_project_ref,
                            'last_workspace': workspace
                            or record.last_workspace,
                        }
                    )
                    if record.launch_id == launch_id
                    else record
                )
                for record in metadata.records
            )
        ),
    )


def complete_launch(
    data_dir,
    launch_id: str,
    *,
    clean: bool,
    failure_class: StartupFailureClass | None = None,
) -> None:
    """Mark a recorded launch finished — clean or classified-failed."""

    metadata = load_recovery_metadata(data_dir)
    _store_metadata(
        data_dir,
        RecoveryMetadata(
            records=tuple(
                (
                    record.model_copy(
                        update={
                            'clean_exit': clean,
                            'failure_class': failure_class,
                        }
                    )
                    if record.launch_id == launch_id
                    else record
                )
                for record in metadata.records
            )
        ),
    )


# ---------------------------------------------------------------------------
# Safe-mode policy and reset scopes


class SafeModePolicy(BaseModel):
    """Declarative Safe Mode boundaries (#739 §2).

    Everything listed here is skipped, not merely deferred to a background
    job; project authority stays read-only until the user explicitly opens
    normal editing.
    """

    model_config = ConfigDict(frozen=True)

    auto_open_last_project: Literal[False] = False
    auto_run_prediction_search_import_jobs: Literal[False] = False
    live_integrations: Literal[False] = False
    restore_saved_layout: Literal[False] = False
    #: 'unchanged' keeps the normal path; 'reduced' prefers the minimal
    #: Room rendering path; 'skip_3d' avoids viewport init when a renderer
    #: failure is on record. Never fakes a software renderer the stack
    #: cannot guarantee.
    renderer: Literal['unchanged', 'reduced', 'skip_3d'] = 'unchanged'
    project_authority: Literal['read_only'] = 'read_only'
    keep_recovery_drafts: Literal[True] = True


class ResetScope(BaseModel):
    """One narrowly scoped, previewable reset action (#739 §5)."""

    model_config = ConfigDict(frozen=True)

    scope: Literal[
        'window_layout',
        'application_preferences',
        'optional_integrations',
    ]
    label: str = Field(min_length=1)
    description: str = Field(min_length=1)
    #: Every offered reset is previewable and reversible where practical;
    #: none ever touches canonical project authority.
    touches_project_data: Literal[False] = False
    reversible: bool = True


RESET_SCOPES: tuple[ResetScope, ...] = (
    ResetScope(
        scope='window_layout',
        label='Reset window/layout/view state',
        description=(
            'Clears persisted window geometry, panel layout and saved '
            'viewport/view state so the next open uses defaults.'
        ),
    ),
    ResetScope(
        scope='application_preferences',
        label='Reset application preferences to defaults',
        description=(
            'Returns app-local preferences to factory defaults; project '
            'documents and measurements are untouched.'
        ),
    ),
    ResetScope(
        scope='optional_integrations',
        label='Disable optional integrations',
        description=(
            'Disables REW/Capture/device-adapter initialization until it '
            'is explicitly re-enabled.'
        ),
    ),
)


# ---------------------------------------------------------------------------
# Launch decision


class LaunchDecision(BaseModel):
    """What the launcher should do this attempt."""

    model_config = ConfigDict(frozen=True)

    mode: LaunchMode
    reasons: tuple[str, ...]
    #: The recovery surface actions to present when mode is not 'normal'.
    choices: tuple[RecoveryChoice, ...] = ()
    failure_class: StartupFailureClass | None = None
    #: True only when the evidence class makes data recovery relevant —
    #: never for renderer/config/integration crashes (#739 §4).
    restore_recommended: bool = False
    #: True only when a restorable archive actually exists on disk
    #: (round9 #11) — the 'restore_backup' choice is never offered
    #: without one.
    backup_restore_available: bool = False
    safe_mode_policy: SafeModePolicy | None = None


def decide_launch(
    *,
    unclean_previous_session: bool,
    metadata: RecoveryMetadata,
    build_id: str | None,
    explicit_safe_mode: bool = False,
    renderer_failure_detected: bool = False,
    failure_class: StartupFailureClass | None = None,
    backup_restore_available: bool = False,
) -> LaunchDecision:
    """Choose the launch mode from concrete evidence (#739 §1).

    Safe Mode is never entered merely because a log exists: it needs an
    explicit user choice or evidence strong enough to *offer* recovery —
    an unclean previous session, repeated startup failure for this
    build/data root, or a known renderer-init failure.
    """

    repeated = metadata.repeated_startup_failures(build_id)
    known_class = (
        failure_class
        if failure_class is not None
        else metadata.last_failure_class(build_id)
    )
    restore = known_class in DATA_RELEVANT_FAILURES if known_class else False

    if explicit_safe_mode:
        return LaunchDecision(
            mode='safe_mode',
            reasons=('ユーザーがセーフモードを明示指定しました',),
            choices=(
                'open_diagnostics',
                'choose_another_project',
                'open_normal',
            ),
            failure_class=known_class,
            restore_recommended=restore,
            backup_restore_available=backup_restore_available,
            safe_mode_policy=SafeModePolicy(
                renderer='skip_3d' if renderer_failure_detected else 'reduced'
            ),
        )

    reasons: list[str] = []
    if unclean_previous_session:
        reasons.append('前回のセッションが予期せず終了しました')
    if repeated >= REPEATED_FAILURE_THRESHOLD:
        reasons.append(
            f'このビルドで連続 {repeated} 回の起動失敗'
        )
    if renderer_failure_detected:
        reasons.append('レンダラー初期化失敗の記録があります')

    if not reasons:
        return LaunchDecision(
            mode='normal',
            reasons=(),
            failure_class=known_class,
            restore_recommended=restore,
            backup_restore_available=backup_restore_available,
        )

    choices: list[RecoveryChoice] = ['open_normal', 'open_safe_mode']
    choices.append('open_diagnostics')
    if restore:
        if backup_restore_available:
            # Offered only when an automatic/pre-upgrade generation
            # actually exists — the restore flow still runs the full
            # validate-preview-confirm journey in Data Management.
            choices.append('restore_backup')
        choices.append('verify_data')
    choices.append('choose_another_project')

    return LaunchDecision(
        mode='recovery_offered',
        reasons=tuple(reasons),
        choices=tuple(choices),
        failure_class=known_class,
        restore_recommended=restore,
        backup_restore_available=backup_restore_available,
        safe_mode_policy=SafeModePolicy(
            renderer='skip_3d' if renderer_failure_detected else 'reduced'
        ),
    )


def plan_safe_mode_launch(
    decision: LaunchDecision,
) -> SafeModePolicy | None:
    """The declarative policy to apply, or None for normal launch."""

    return decision.safe_mode_policy


def recovery_metadata_token(data_dir) -> str:
    """Short correlation seed for support bundles (privacy-safe)."""

    return sha256(
        str(_metadata_path(data_dir)).encode('utf-8')
    ).hexdigest()[:8]


__all__ = [
    'DATA_RELEVANT_FAILURES',
    'LaunchDecision',
    'LaunchMode',
    'MAX_RECOVERY_RECORDS',
    'RECOVERY_METADATA_NAME',
    'RECOVERY_SCHEMA_VERSION',
    'REPEATED_FAILURE_THRESHOLD',
    'RESET_SCOPES',
    'RecoveryChoice',
    'RecoveryLaunchRecord',
    'RecoveryMetadata',
    'ResetScope',
    'SafeModePolicy',
    'StartupFailureClass',
    'annotate_launch',
    'classify_startup_failure',
    'complete_launch',
    'decide_launch',
    'load_recovery_metadata',
    'plan_safe_mode_launch',
    'record_launch',
    'recovery_metadata_token',
]
