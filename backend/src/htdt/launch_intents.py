"""Windows document-open routing (#612).

One routing authority for every way a file reaches HTDT: Windows Explorer
file associations, the command line, an in-app Open action, drag-and-drop,
and single-instance forwarding. All of them produce an ``HTDTLaunchIntent``
and all of them are dispatched through the same intent router.

File types (registered per-user by the installer, ``installer/HTDT.iss``):

- ``.htdtproject`` — overloaded (#736): either a small JSON
  ``htdt-project-ref`` descriptor that switches to an existing project, or
  a #488 ZIP project bundle that is imported through the bundle authority
  and then opened. The router decides by file content, not extension.
- ``.htdtcapture`` — a Capture bundle reference or descriptor; staged for
  review (never silently imported as evidence).
- ``.htdt-backup`` — a validated backup archive; opened as a *preview* with
  an explicit Restore choice. Double-clicking a backup must never restore
  it by itself.

Forwarding is lock-free: a second process that cannot take the instance
lock drops the intent as its own JSON file under
``<data_dir>/launch-intents/incoming/`` and exits; the running instance
drains the directory on a timer and routes the intents itself.

Queue durability (#736): a queue file is retired only AFTER its semantic
dispatch completed — ``drain_launch_intents`` leaves valid files in
``incoming/`` and ``complete_queued_intent`` moves each dispatched file to
``done/`` (or ``failed/``). A crash between drain and completion redelivers
the intent on the next drain, so forwarding is at-least-once; the routed
actions (project switch, capture staging, restore preview) are idempotent,
so re-delivery is safe.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import time
import logging
import os
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


_LOGGER = logging.getLogger('htdt.native')

INTENTS_DIRNAME = 'launch-intents'
INTENT_INCOMING_DIRNAME = 'incoming'
INTENT_DEAD_DIRNAME = 'dead'
INTENT_DONE_DIRNAME = 'done'
INTENT_FAILED_DIRNAME = 'failed'
LAUNCH_INTENT_SCHEMA_VERSION = 1

# Intent descriptor files are small JSON references — bound the read so a
# hostile or corrupt drop never expands into memory.
MAX_INTENT_DESCRIPTOR_BYTES = 256 * 1024

HTDT_PROJECT_SUFFIX = '.htdtproject'
HTDT_CAPTURE_SUFFIX = '.htdtcapture'
HTDT_BACKUP_SUFFIX = '.htdt-backup'

LaunchIntentKind = Literal[
    'open_project',
    'preview_capture',
    'preview_backup',
    'unknown',
]
LaunchIntentSource = Literal[
    'command_line',
    'file_association',
    'menu',
    'drop',
    'forwarded',
]
# One outcome contract (#736) for every kind of intent. The router reports
# exactly one of these; the shell maps it to copy — it may never print a
# success message for an outcome that is not a success.
LaunchIntentOutcome = Literal[
    'routed_and_opened',
    'staged_for_review',
    'already_staged',
    'preview_opened',
    'user_action_required',
    'blocked_dirty_state',
    'invalid_or_unsupported',
    'failed',
]


class LaunchIntentResult(BaseModel):
    """The exact semantic outcome of dispatching one intent (#736).

    ``document_id``/``inbox_item_id`` carry the target the shell should
    focus when the outcome implies navigation.
    """

    model_config = ConfigDict(frozen=True)

    intent_id: str = Field(min_length=1)
    kind: LaunchIntentKind
    path: str
    outcome: LaunchIntentOutcome
    detail: str = ''
    document_id: str | None = None
    inbox_item_id: str | None = None


class HTDTLaunchIntent(BaseModel):
    """A single document-open intent, transport-agnostic."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = LAUNCH_INTENT_SCHEMA_VERSION
    intent_id: str = Field(min_length=1)
    kind: LaunchIntentKind
    path: str = Field(min_length=1)
    source: LaunchIntentSource
    received_at_utc: str = Field(min_length=1)
    # Extracted from the file when it is a recognized HTDT descriptor;
    # otherwise None and the router resolves by inspection.
    document_id: str | None = None
    detail: str | None = None


class QueuedLaunchIntent(BaseModel):
    """A drained intent plus the durable queue file that records it."""

    model_config = ConfigDict(frozen=True)

    intent: HTDTLaunchIntent
    queue_path: str = Field(min_length=1)


class HTDTProjectFile(BaseModel):
    """``.htdtproject`` descriptor: a portable pointer to a project."""

    model_config = ConfigDict(frozen=True)

    kind: Literal['htdt-project-ref']
    schema_version: int = Field(ge=1)
    project_id: str | None = None
    document_id: str | None = None
    display_name: str | None = None

    @field_validator('kind', mode='before')
    @classmethod
    def _kind_required(cls, value: object) -> object:
        return value


class HTDTCaptureFile(BaseModel):
    """``.htdtcapture`` descriptor accompanying a capture bundle."""

    model_config = ConfigDict(frozen=True)

    kind: Literal['htdt-capture-ref']
    schema_version: int = Field(ge=1)
    capture_revision_id: str | None = None
    bundle_path: str | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify_launch_path(path: Path) -> LaunchIntentKind:
    suffix = Path(path).suffix.lower()
    if suffix == HTDT_PROJECT_SUFFIX:
        return 'open_project'
    if suffix == HTDT_CAPTURE_SUFFIX:
        return 'preview_capture'
    if suffix == HTDT_BACKUP_SUFFIX:
        return 'preview_backup'
    return 'unknown'


def _read_bounded_json(path: Path) -> dict[str, object] | None:
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > MAX_INTENT_DESCRIPTOR_BYTES:
        return None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def build_launch_intent(
    path: Path,
    *,
    source: LaunchIntentSource = 'command_line',
) -> HTDTLaunchIntent:
    """Classify and lightly inspect one file-open request."""

    path = Path(path)
    kind = classify_launch_path(path)
    document_id: str | None = None
    detail: str | None = None

    payload = _read_bounded_json(path)
    if payload is not None:
        if kind == 'open_project' and payload.get('kind') == 'htdt-project-ref':
            try:
                ref = HTDTProjectFile.model_validate(payload)
                document_id = ref.document_id
                detail = ref.display_name
            except ValueError:
                detail = 'unrecognized .htdtproject descriptor'
        elif (
            kind == 'preview_capture'
            and payload.get('kind') == 'htdt-capture-ref'
        ):
            try:
                HTDTCaptureFile.model_validate(payload)
            except ValueError:
                detail = 'unrecognized .htdtcapture descriptor'

    return HTDTLaunchIntent(
        intent_id=uuid4().hex,
        kind=kind,
        path=str(path),
        source=source,
        received_at_utc=_utc_now(),
        document_id=document_id,
        detail=detail,
    )


def intents_dir(data_dir: Path) -> Path:
    return Path(data_dir) / INTENTS_DIRNAME


def _incoming_dir(data_dir: Path) -> Path:
    return intents_dir(data_dir) / INTENT_INCOMING_DIRNAME


_arrival_seq = 0


def forward_launch_intent(data_dir: Path, intent: HTDTLaunchIntent) -> Path:
    """Hand an intent to the running instance via the drop directory.

    Each intent is its own file written atomically, so a contending second
    process never needs to hold a lock and a crash can never leave a torn
    queue file.
    """

    global _arrival_seq
    _arrival_seq += 1
    incoming = _incoming_dir(Path(data_dir))
    incoming.mkdir(parents=True, exist_ok=True)
    # Filename order is the drain order: wall-clock tick first (cross-
    # process FIFO), then a per-process sequence (same-tick drops).
    arrival = f'{time.time_ns():020d}-{_arrival_seq:06d}'
    target = incoming / f'{arrival}-{intent.intent_id}.json'
    temp = incoming / f'.{arrival}-{intent.intent_id}.{os.getpid()}.tmp'
    temp.write_text(
        json.dumps(intent.model_dump(mode='json'), sort_keys=True),
        encoding='utf-8',
    )
    os.replace(temp, target)
    return target


def drain_launch_intents(data_dir: Path) -> tuple[QueuedLaunchIntent, ...]:
    """Read every queued intent; malformed drops go to ``dead/``.

    Valid queue files are NOT removed here (#736): each drained entry keeps
    its ``queue_path`` and the caller retires it via
    ``complete_queued_intent`` only after the semantic dispatch produced an
    outcome, so a crash can never silently lose an intent.
    """

    incoming = _incoming_dir(Path(data_dir))
    if not incoming.is_dir():
        return ()
    queued: list[QueuedLaunchIntent] = []
    dead_dir = intents_dir(data_dir) / INTENT_DEAD_DIRNAME
    for candidate in sorted(incoming.iterdir()):
        if not candidate.is_file() or candidate.suffix != '.json':
            continue
        try:
            intent = HTDTLaunchIntent.model_validate_json(
                candidate.read_text(encoding='utf-8')
            )
        except (OSError, ValueError) as exc:
            _LOGGER.warning('unreadable launch intent %s: %s', candidate, exc)
            try:
                dead_dir.mkdir(parents=True, exist_ok=True)
                os.replace(candidate, dead_dir / candidate.name)
            except OSError:
                pass
            continue
        queued.append(
            QueuedLaunchIntent(intent=intent, queue_path=str(candidate))
        )
    return tuple(queued)


def complete_queued_intent(
    queued: QueuedLaunchIntent,
    *,
    succeeded: bool,
) -> None:
    """Retire a dispatched queue file to ``done/`` or ``failed/`` (#736).

    Called exactly once per drained intent, after its semantic dispatch
    completed. A file that is already gone (retired by a racing drain in an
    earlier session) is a no-op; a dispatch that never reached completion
    leaves the file in ``incoming/`` so the next drain redelivers it.
    """

    source = Path(queued.queue_path)
    if not source.exists():
        return
    target_dir = source.parent.parent / (
        INTENT_DONE_DIRNAME if succeeded else INTENT_FAILED_DIRNAME
    )
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        os.replace(source, target_dir / source.name)
    except OSError:
        _LOGGER.warning(
            'could not retire launch intent queue file %s', source
        )


def describe_launch_intent(intent: HTDTLaunchIntent) -> str:
    """Short user-facing description for dialogs and logs."""

    name = Path(intent.path).name
    if intent.kind == 'open_project':
        return f'project "{intent.detail or name}"'
    if intent.kind == 'preview_capture':
        return f'capture package "{name}"'
    if intent.kind == 'preview_backup':
        return f'backup archive "{name}"'
    return f'file "{name}"'


__all__ = [
    'HTDTCaptureFile',
    'HTDTLaunchIntent',
    'HTDTProjectFile',
    'LaunchIntentOutcome',
    'LaunchIntentResult',
    'QueuedLaunchIntent',
    'build_launch_intent',
    'classify_launch_path',
    'complete_queued_intent',
    'describe_launch_intent',
    'drain_launch_intents',
    'forward_launch_intent',
    'intents_dir',
]
