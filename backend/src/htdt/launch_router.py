"""Semantic document-open routing (#736).

``launch_intents`` classifies and durably queues each document-open
request; this module performs the semantic action per kind —

- ``.htdtproject``: a #488 ZIP project bundle is imported through the
  bundle authority (which registers the project); a JSON
  ``htdt-project-ref`` descriptor names a document that must already exist
  in this data root's project library. Both end in ``routed_and_opened``
  carrying the ``document_id`` the shell must switch to.
- ``.htdtcapture``: the bundle is parsed and ingested through the canonical
  Capture contract (``capture_import``/``capture_reference``) and staged
  into the Capture Inbox — staged for review, never silently promoted as
  evidence. Re-delivery of an identical capture reports
  ``already_staged``.
- ``.htdt-backup``: the archive is validated through the native backup
  authority; the shell then opens the Restore preview surface — a backup
  open never restores anything by itself.

Every route returns one ``LaunchIntentResult`` carrying the exact outcome
contract value; these functions are Qt-free so the whole semantic contract
is testable headless. GUI-only steps (the project switch, the inbox deep
link, the restore preview dialog) are applied by the shell on top of these
results and may downgrade an outcome to ``blocked_dirty_state`` or
``user_action_required`` when the composition refuses.
"""

from __future__ import annotations

import logging
from pathlib import Path
import zipfile

from .capture_bundle import FrozenBundle
from .capture_import import CaptureImportError, import_capture_artifact
from .capture_inbox import (
    CAPTURE_INBOX_UNASSIGNED_SCOPE,
    CaptureInboxRepository,
)
from .capture_ingestion_transaction import (
    CaptureIngestionPlan,
    CaptureIngestionRepository,
)
from .capture_reference import build_ingestion_plan
from .cad_repository import SceneRepository
from .launch_intents import (
    HTDTCaptureFile,
    HTDTProjectFile,
    HTDTLaunchIntent,
    LaunchIntentOutcome,
    LaunchIntentResult,
    _read_bounded_json,
)
from .native_backup import inspect_backup
from .project_bundle import import_project_bundle
from .project_library_repository import ProjectLibraryRepository


_LOGGER = logging.getLogger('htdt.native')

# The Inbox scope every document-open capture lands in: routing never
# guesses a project — promotion into a Scene stays an explicit review act.
CAPTURE_STAGE_SCOPE = CAPTURE_INBOX_UNASSIGNED_SCOPE
CAPTURE_ARRIVAL_SOURCE = 'document_open'


def _result(
    intent: HTDTLaunchIntent,
    outcome: LaunchIntentOutcome,
    detail: str,
    *,
    document_id: str | None = None,
    inbox_item_id: str | None = None,
) -> LaunchIntentResult:
    return LaunchIntentResult(
        intent_id=intent.intent_id,
        kind=intent.kind,
        path=intent.path,
        outcome=outcome,
        detail=detail,
        document_id=document_id,
        inbox_item_id=inbox_item_id,
    )


def route_launch_intent(
    intent: HTDTLaunchIntent,
    *,
    repository: SceneRepository,
) -> LaunchIntentResult:
    """Dispatch one intent through its semantic route; never raises."""

    try:
        if intent.kind == 'open_project':
            return route_open_project_intent(intent, repository=repository)
        if intent.kind == 'preview_capture':
            return route_capture_intent(intent, repository=repository)
        if intent.kind == 'preview_backup':
            return route_backup_intent(intent)
    except Exception as exc:  # pragma: no cover - last-resort guard
        _LOGGER.exception('launch intent routing raised: %s', intent.path)
        return _result(intent, 'failed', f'unexpected routing error: {exc}')
    return _result(
        intent,
        'invalid_or_unsupported',
        f'unsupported file type: {Path(intent.path).name}',
    )


def route_open_project_intent(
    intent: HTDTLaunchIntent,
    *,
    repository: SceneRepository,
) -> LaunchIntentResult:
    """``.htdtproject`` is decided by content, not extension (#736)."""

    path = Path(intent.path)
    if not path.is_file():
        return _result(
            intent, 'invalid_or_unsupported', 'file not found'
        )

    if zipfile.is_zipfile(path):
        try:
            imported = import_project_bundle(repository, path)
        except Exception as exc:
            return _result(
                intent, 'failed', f'project bundle import failed: {exc}'
            )
        _LOGGER.info(
            'project bundle imported: %s -> document %s (mode=%s)',
            path,
            imported.document_id,
            imported.import_mode,
        )
        return _result(
            intent,
            'routed_and_opened',
            'project bundle imported',
            document_id=imported.document_id,
        )

    payload = _read_bounded_json(path)
    if payload is None:
        return _result(
            intent,
            'invalid_or_unsupported',
            '.htdtproject must be a project bundle archive or a JSON '
            'project descriptor',
        )
    try:
        descriptor = HTDTProjectFile.model_validate(payload)
    except ValueError:
        return _result(
            intent,
            'invalid_or_unsupported',
            'unrecognized .htdtproject descriptor',
        )
    document_id = descriptor.document_id or intent.document_id
    if not document_id:
        return _result(
            intent,
            'invalid_or_unsupported',
            'project descriptor carries no document identity',
        )
    entry = ProjectLibraryRepository(repository).get_by_document_id(
        document_id
    )
    if entry is None:
        return _result(
            intent,
            'user_action_required',
            'project is not registered in this data root — import its '
            'project bundle first',
            document_id=document_id,
        )
    return _result(
        intent,
        'routed_and_opened',
        f'project "{entry.display_name}"',
        document_id=document_id,
    )


def route_capture_intent(
    intent: HTDTLaunchIntent,
    *,
    repository: SceneRepository,
) -> LaunchIntentResult:
    """``.htdtcapture`` ingests + stages to the Capture Inbox (#736)."""

    path = Path(intent.path)
    if not path.exists():
        return _result(
            intent, 'invalid_or_unsupported', 'file not found'
        )

    bundle_path = path
    descriptor: HTDTCaptureFile | None = None
    payload = _read_bounded_json(path)
    if payload is not None:
        try:
            descriptor = HTDTCaptureFile.model_validate(payload)
        except ValueError:
            descriptor = None
        if descriptor is not None:
            if not descriptor.bundle_path:
                return _result(
                    intent,
                    'user_action_required',
                    'capture descriptor names no bundle — place the bundle '
                    'beside it or stage it from the Inbox',
                )
            bundle_path = Path(descriptor.bundle_path)
            if not bundle_path.is_absolute():
                bundle_path = path.parent / bundle_path
            if not bundle_path.exists():
                return _result(
                    intent,
                    'invalid_or_unsupported',
                    'capture bundle not found: '
                    f'{descriptor.bundle_path}',
                )

    ingestion = CaptureIngestionRepository(repository)
    try:
        import_capture_artifact(bundle_path, ingestion)
    except CaptureImportError as exc:
        return _result(
            intent,
            'failed',
            f'capture import rejected at {exc.stage}: {exc}',
        )
    except (ValueError, OSError) as exc:
        return _result(
            intent, 'failed', f'capture import failed: {exc}'
        )

    try:
        plan = CaptureIngestionPlan.model_validate(
            build_ingestion_plan(FrozenBundle(bundle_path))
        )
    except (ValueError, OSError) as exc:
        return _result(
            intent,
            'failed',
            f'capture ingestion plan could not be rebuilt: {exc}',
        )
    if (
        descriptor is not None
        and descriptor.capture_revision_id
        and descriptor.capture_revision_id
        != plan.bundle.capture_revision_id
    ):
        return _result(
            intent,
            'invalid_or_unsupported',
            'capture descriptor revision does not match the bundle',
        )

    try:
        staged = CaptureInboxRepository(repository, ingestion).stage(
            plan,
            arrival_source=CAPTURE_ARRIVAL_SOURCE,
            scope=CAPTURE_STAGE_SCOPE,
            source_detail=str(path),
        )
    except Exception as exc:
        return _result(
            intent, 'failed', f'capture inbox staging failed: {exc}'
        )

    outcome: LaunchIntentOutcome = (
        'staged_for_review' if staged.created else 'already_staged'
    )
    return _result(
        intent,
        outcome,
        f'capture revision {plan.bundle.capture_revision_id} staged '
        'for Inbox review — nothing was promoted to evidence',
        inbox_item_id=staged.item.inbox_item_id,
    )


def route_backup_intent(
    intent: HTDTLaunchIntent,
) -> LaunchIntentResult:
    """``.htdt-backup`` validates the archive; the shell opens preview."""

    path = Path(intent.path)
    if not path.is_file():
        return _result(
            intent, 'invalid_or_unsupported', 'file not found'
        )
    try:
        manifest, staged_schema = inspect_backup(path)
    except Exception as exc:
        return _result(
            intent,
            'invalid_or_unsupported',
            f'not a valid .htdt-backup archive: {exc}',
        )
    return _result(
        intent,
        'preview_opened',
        f'backup created {manifest.created_at_utc} '
        f'(schema {staged_schema}, {len(manifest.files)} files)',
    )


__all__ = [
    'CAPTURE_ARRIVAL_SOURCE',
    'CAPTURE_STAGE_SCOPE',
    'route_backup_intent',
    'route_capture_intent',
    'route_launch_intent',
    'route_open_project_intent',
]
