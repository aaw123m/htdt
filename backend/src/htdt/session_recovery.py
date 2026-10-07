"""Bounded session journal and crash-safe recovery authority (#883).

One session = one append-only journal directory under
``<data_dir>/session-journal/``:

- ``envelope.json`` — the session's identity: launch/session id, app +
  build + journal + native-schema versions, pid, boot token, project
  binding, timestamps and the clean-close flag. Written atomically;
  rewritten on heartbeat (``last_seen_at_utc``) and on clean close.
- ``journal.jsonl`` — one sha-chained JSON entry per line. Entry 1
  chains to the envelope's identity sha, so rewriting the volatile
  envelope fields never invalidates the chain.
- ``payloads/<sha256>.json`` — content-addressed sidecar for payloads
  larger than ``MAX_INLINE_PAYLOAD_BYTES`` (raw measurement bytes,
  staged-import state). The entry carries ``payload_sha256``; inspection
  re-verifies it.

The journal is *volatile trace*, not canonical truth: a recovered draft
still lives in ``scene_recovery_snapshots``, a resumed commissioning run
still comes from its sealed transitions. The journal only says what was
in flight and where — recovery never fabricates authority state.

Sealed records (native schema v100) capture the durable side: a
detection record per crashed journal (tamper-evidence once files are
retention-collected), append-only operator decisions with the restoring
session's lineage, and reconciliation verdicts for external effects that
may or may not have landed (``RECONCILIATION_REQUIRED`` /
``DEVICE_STATE_UNKNOWN`` / ``ACQUISITION_INCOMPLETE`` /
``WRITE_COMPLETION_UNKNOWN`` — resolved only by sealed evidence or an
explicit machine read-back).

Ending classification is evidence-only: ``clean`` needs the close
marker, ``application_crash`` needs a recorded failure class or crash
correlation, ``os_restart_or_power_loss`` needs a changed boot token,
``forced_termination`` is a dead pid without crash evidence, and foreign
journal/native-schema versions are ``incompatible`` — never restored.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field

from .canonical_json import canonical_sha256, canonicalize_payload
from .clock import utc_now_iso

# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------

SESSION_JOURNAL_DIRNAME = 'session-journal'
ENVELOPE_NAME = 'envelope.json'
JOURNAL_NAME = 'journal.jsonl'
PAYLOADS_DIRNAME = 'payloads'

#: Journal format contract. Foreign (newer) versions are rejected, never
#: interpreted.
SESSION_JOURNAL_SCHEMA_VERSION = 1

#: Per-session bound before the file compacts its tail into a snapshot
#: entry. Entries are tiny; this is generous.
MAX_JOURNAL_ENTRIES = 512

#: Payloads above this live in ``payloads/<sha>.json`` instead of inline.
MAX_INLINE_PAYLOAD_BYTES = 64 * 1024

#: Retention: decided/clean session dirs beyond this count are collected
#: oldest-first. Undecided evidence is never deleted silently — beyond
#: ``MAX_UNRESOLVED_JOURNALS`` the oldest unresolved journal is dropped
#: only with a sealed ``retention_discard`` decision recording why.
MAX_SESSION_JOURNALS = 24
MAX_UNRESOLVED_JOURNALS = 64

_SHA256_PATTERN = r'^[0-9a-f]{64}$'

#: Heartbeat liveness + workspace-state cadence for the GUI wiring.
HEARTBEAT_INTERVAL_MS = 15000


SessionEnding = Literal[
    'clean',
    'application_crash',
    'forced_termination',
    'os_restart_or_power_loss',
    'still_running',
    'incompatible',
]

JournalIntegrity = Literal[
    'intact',
    'tail_torn',
    'corrupt',
    'incompatible',
    'unreadable',
]

RecoverableItemKind = Literal[
    'scene_draft',
    'workspace_state',
    'pending_annotation',
    'pending_import',
    'project_binding',
    'commissioning_run',
    'measurement_campaign',
]

#: What a restorable item resolves to when restored.
ItemAvailability = Literal['available', 'blocked']

ReconciliationKind = Literal[
    'device_apply_transaction',
    'sweep_acquisition',
    'file_write',
]

#: The four honest states the issue mandates — recovery must not infer
#: success.
ReconciliationState = Literal[
    'RECONCILIATION_REQUIRED',
    'DEVICE_STATE_UNKNOWN',
    'ACQUISITION_INCOMPLETE',
    'WRITE_COMPLETION_UNKNOWN',
]

DecisionAction = Literal[
    'restore_accepted',
    'restore_completed',
    'discarded',
    'deferred',
    'evidence_rejected',
    'retention_discard',
    'reconcile_resolved',
    'reconcile_deferred',
]

#: Session-scope actions that settle a crashed session — anything else
#: leaves it on the inspection surface.
TERMINAL_DECISION_ACTIONS: frozenset[str] = frozenset(
    {
        'restore_completed',
        'discarded',
        'evidence_rejected',
        'retention_discard',
    }
)

ReconciliationVerdict = Literal[
    'resolved_confirmed',
    'resolved_rolled_back',
    'resolved_failed',
    'resolved_not_started',
    'remains_unknown',
    'readback_unavailable',
    'rejected',
]

ENTRY_KINDS: frozenset[str] = frozenset(
    {
        'session_opened',
        'project_bound',
        'workspace_snapshot',
        'scene_draft',
        'scene_draft_cleared',
        'pending_annotation',
        'pending_annotation_cleared',
        'pending_import',
        'pending_import_cleared',
        'operation_started',
        'operation_finished',
        'write_intent',
        'write_completed',
        'acquisition_stage',
        'heartbeat',
        'failure_noted',
        'snapshot_compaction',
        'session_close_clean',
    }
)

#: Kinds that declare an in-flight external operation. ``operation_ref``
#: carries the domain identity (plan/transaction/run id).
OPERATION_KINDS: frozenset[str] = frozenset(
    {
        'device_apply_transaction',
        'sweep_acquisition',
        'commissioning_run',
        'measurement_campaign',
    }
)

#: Payload keys matching this never enter a journal (#883: no plaintext
#: credentials in crash artifacts).
_SECRET_KEY_PATTERN = re.compile(
    r'(?i)(password|passwd|secret|api[_-]?key|access[_-]?key|token|'
    r'credential|bearer|private[_-]?key|otp|totp)'
)


class SessionJournalError(RuntimeError):
    """Raised for unusable journal state (existing dir, bad payload)."""


class SessionJournalPayloadError(SessionJournalError, ValueError):
    """A payload key looks like a secret — refused before it lands."""


class RecoveryDecisionConflictError(RuntimeError):
    """A sealed decision row already exists with different content."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _sha256_pattern() -> str:
    return _SHA256_PATTERN


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = canonical_sha256(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{sha_field: digest, id_field: _semantic_id(prefix, digest)},
    )


def _assert_no_secret_keys(node: Any, path: str = '') -> None:
    """Reject payloads that name a credential — anywhere in the tree."""

    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and _SECRET_KEY_PATTERN.search(key):
                raise SessionJournalPayloadError(
                    f'journal payload key looks like a secret: {path}{key}'
                )
            _assert_no_secret_keys(value, f'{path}{key}.')
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            _assert_no_secret_keys(value, f'{path}[{index}].')


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def boot_token() -> str | None:
    """Approximate OS-boot identity — 'unknown' is never evidence."""

    try:
        if os.name == 'nt':
            import ctypes
            import time

            uptime_ms = ctypes.windll.kernel32.GetTickCount64()
            boot_epoch = time.time() - uptime_ms / 1000.0
            return f'boot-{boot_epoch:.0f}'
        stat = Path('/proc/stat')
        if stat.is_file():
            for line in stat.read_text().splitlines():
                if line.startswith('btime '):
                    return f'boot-{line.split()[1]}'
        try:
            import psutil  # type: ignore

            return f'boot-{psutil.boot_time():.0f}'
        except Exception:
            return None
    except Exception:
        return None


def _boot_tokens_match(recorded: str | None, current: str | None) -> bool:
    """Boot tokens compare loosely — wall-clock noise is not a reboot."""

    if not recorded or not current:
        return False
    if recorded == current:
        return True
    if not (recorded.startswith('boot-') and current.startswith('boot-')):
        return False
    try:
        return abs(float(current[5:]) - float(recorded[5:])) <= 300.0
    except (ValueError, TypeError):
        return recorded == current


def _default_pid_alive(pid: int) -> bool:
    from .support_diagnostics import _default_pid_alive as alive

    return alive(pid)


# ---------------------------------------------------------------------------
# Envelope + journal entries
# ---------------------------------------------------------------------------


class SessionEnvelope(BaseModel):
    """One session's identity facts. ``closed_clean`` is the only mutable
    semantics — written once at clean exit (envelope is rewritten
    atomically; the entry chain anchors to the *identity* sha, which
    excludes volatile fields)."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = SESSION_JOURNAL_SCHEMA_VERSION
    session_id: str = Field(min_length=1)
    launch_id: str | None = None
    app_version: str | None = None
    build_id: str | None = None
    native_schema_version: int | None = None
    pid: int = Field(ge=0)
    boot_token: str | None = None
    started_at_utc: str = Field(min_length=1)
    last_seen_at_utc: str = Field(min_length=1)
    project_document_id: str | None = None
    failure_class: str | None = None
    closed_clean: bool = False
    closed_at_utc: str | None = None

    def identity_payload(self) -> dict[str, Any]:
        """The sha-stable identity subset — volatile fields excluded."""

        return {
            'schema_version': self.schema_version,
            'session_id': self.session_id,
            'launch_id': self.launch_id,
            'app_version': self.app_version,
            'build_id': self.build_id,
            'native_schema_version': self.native_schema_version,
            'pid': self.pid,
            'boot_token': self.boot_token,
            'started_at_utc': self.started_at_utc,
        }

    def identity_sha256(self) -> str:
        return canonical_sha256(self.identity_payload())


class JournalEntry(BaseModel):
    """One journal line. ``prev_sha256`` chains to the previous entry's
    ``entry_sha256`` (or the envelope identity sha at seq 1); a
    ``snapshot_compaction`` first line chains to ``compacted_from_sha256``
    in its payload — the folded state entry is its own chain head."""

    model_config = ConfigDict(frozen=True)

    seq: int = Field(ge=1)
    session_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    recorded_at_utc: str
    prev_sha256: str = Field(pattern=_SHA256_PATTERN)
    payload: dict[str, Any] = Field(default_factory=dict)
    entry_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'entry_sha256'}
        )

    @classmethod
    def create(
        cls,
        *,
        seq: int,
        session_id: str,
        kind: str,
        recorded_at_utc: str,
        prev_sha256: str,
        payload: dict[str, Any],
    ) -> 'JournalEntry':
        probe = cls.model_construct(
            seq=seq,
            session_id=session_id,
            kind=kind,
            recorded_at_utc=recorded_at_utc,
            prev_sha256=prev_sha256,
            payload=payload,
        )
        return cls(
            seq=seq,
            session_id=session_id,
            kind=kind,
            recorded_at_utc=recorded_at_utc,
            prev_sha256=prev_sha256,
            payload=payload,
            entry_sha256=canonical_sha256(probe.identity_payload()),
        )


# ---------------------------------------------------------------------------
# Journal writer
# ---------------------------------------------------------------------------


class SessionJournal:
    """Append-only writer for one live session's journal directory.

    The writer never reopens an existing directory — a journal is
    forensic once its session ends. Appends fsync per line; the envelope
    is rewritten atomically on heartbeat/close so ``last_seen_at_utc``
    bounds the crash window.
    """

    def __init__(
        self,
        directory: Path,
        envelope: SessionEnvelope,
        *,
        seq: int = 1,
        head_sha256: str,
    ) -> None:
        self.directory = directory
        self.envelope = envelope
        self._seq = seq
        self._head_sha256 = head_sha256
        self._state_provider: Callable[[], dict[str, Any]] | None = None
        self._closed = False

    @property
    def session_id(self) -> str:
        return self.envelope.session_id

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def head_entry_sha256(self) -> str:
        return self._head_sha256

    @property
    def seq(self) -> int:
        return self._seq

    def set_state_provider(
        self, provider: Callable[[], dict[str, Any]] | None
    ) -> None:
        """Workspace snapshot source for heartbeats (None = header only)."""

        self._state_provider = provider

    # -- lifecycle ------------------------------------------------------

    @classmethod
    def open(
        cls,
        data_dir: Path | str,
        *,
        session_id: str,
        launch_id: str | None = None,
        app_version: str | None = None,
        build_id: str | None = None,
        native_schema_version: int | None = None,
        pid: int | None = None,
        boot: str | None = None,
        started_at_utc: str | None = None,
        project_document_id: str | None = None,
    ) -> 'SessionJournal':
        root = session_journal_root(data_dir)
        directory = root / session_id
        if directory.exists():
            raise SessionJournalError(
                f'session journal already exists: {directory}'
            )
        started = started_at_utc or utc_now_iso()
        envelope = SessionEnvelope(
            session_id=session_id,
            launch_id=launch_id,
            app_version=app_version,
            build_id=build_id,
            native_schema_version=native_schema_version,
            pid=os.getpid() if pid is None else pid,
            boot_token=boot if boot is not None else boot_token(),
            started_at_utc=started,
            last_seen_at_utc=started,
            project_document_id=project_document_id,
        )
        directory.mkdir(parents=True)
        journal = cls(directory, envelope, head_sha256=envelope.identity_sha256())
        journal._write_envelope()
        journal.append(
            'session_opened',
            {
                'pid': envelope.pid,
                'app_version': envelope.app_version,
                'build_id': envelope.build_id,
                'native_schema_version': envelope.native_schema_version,
            },
        )
        return journal

    def _write_envelope(self) -> None:
        from .export_io import write_text_atomic

        write_text_atomic(
            self.directory / ENVELOPE_NAME,
            self.envelope.model_dump_json(indent=2),
        )

    # -- append ---------------------------------------------------------

    def _entry_line(self, entry: JournalEntry) -> bytes:
        return (entry.model_dump_json() + '\n').encode('utf-8')

    def _append_line(self, entry: JournalEntry) -> None:
        line = self._entry_line(entry)
        with open(self.directory / JOURNAL_NAME, 'ab') as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        self._seq = entry.seq + 1
        self._head_sha256 = entry.entry_sha256

    def append(
        self, kind: str, payload: dict[str, Any] | None = None
    ) -> JournalEntry:
        """Append one entry; payloads above the inline cap move to a
        content-addressed sidecar before the entry lands."""

        if self._closed:
            raise SessionJournalError('journal is closed')
        if kind not in ENTRY_KINDS:
            raise SessionJournalError(f'unknown journal entry kind: {kind}')
        body = dict(payload or {})
        _assert_no_secret_keys(body)
        wire = body
        encoded = json.dumps(
            body, sort_keys=True, separators=(',', ':')
        ).encode('utf-8')
        if len(encoded) > MAX_INLINE_PAYLOAD_BYTES:
            body_sha = sha256(encoded).hexdigest()
            payloads = self.directory / PAYLOADS_DIRNAME
            payloads.mkdir(exist_ok=True)
            target = payloads / f'{body_sha}.json'
            if not target.exists():
                from .export_io import write_bytes_atomic

                write_bytes_atomic(target, encoded)
            wire = {
                '_sidecar_sha256': body_sha,
                '_sidecar_size': len(encoded),
            }
        entry = JournalEntry.create(
            seq=self._seq,
            session_id=self.envelope.session_id,
            kind=kind,
            recorded_at_utc=utc_now_iso(),
            prev_sha256=self._head_sha256,
            payload=wire,
        )
        self._append_line(entry)
        if self._seq > MAX_JOURNAL_ENTRIES:
            self._compact()
        return entry

    def heartbeat(self) -> None:
        """Refresh ``last_seen_at_utc``; appends a heartbeat entry when a
        state provider yields a snapshot."""

        if self._closed:
            return
        self.envelope = self.envelope.model_copy(
            update={'last_seen_at_utc': utc_now_iso()}
        )
        self._write_envelope()
        state: dict[str, Any] = {}
        if self._state_provider is not None:
            try:
                provided = self._state_provider()
                if isinstance(provided, dict):
                    state = provided
            except Exception:
                state = {'provider_error': True}
        if state:
            self.append('workspace_snapshot', state)
        else:
            self.append('heartbeat', {})

    # -- semantic recorders ---------------------------------------------

    def record_project_bound(self, document_id: str) -> None:
        self.envelope = self.envelope.model_copy(
            update={'project_document_id': document_id}
        )
        self._write_envelope()
        self.append('project_bound', {'document_id': document_id})

    def record_workspace_state(
        self,
        *,
        destination: str | None = None,
        mounts: Iterable[str] = (),
        dirty_documents: Iterable[str] = (),
        contexts: dict[str, Any] | None = None,
    ) -> None:
        self.append(
            'workspace_snapshot',
            {
                'destination': destination,
                'mounts': sorted(mounts),
                'dirty_documents': sorted(dirty_documents),
                'contexts': dict(contexts or {}),
            },
        )

    def record_scene_draft(
        self,
        *,
        document_id: str,
        content_hash: str,
        source_revision_id: str | None = None,
    ) -> None:
        self.append(
            'scene_draft',
            {
                'document_id': document_id,
                'content_hash': content_hash,
                'source_revision_id': source_revision_id,
            },
        )

    def record_scene_draft_cleared(self, document_id: str) -> None:
        self.append('scene_draft_cleared', {'document_id': document_id})

    def record_pending_annotation(
        self,
        *,
        annotation_id: str,
        document_id: str,
        summary: str,
        body: dict[str, Any],
    ) -> None:
        self.append(
            'pending_annotation',
            {
                'annotation_id': annotation_id,
                'document_id': document_id,
                'summary': summary,
                'body': dict(body),
            },
        )

    def record_pending_annotation_cleared(self, annotation_id: str) -> None:
        self.append(
            'pending_annotation_cleared', {'annotation_id': annotation_id}
        )

    def record_pending_import(self, state: dict[str, Any]) -> None:
        self.append('pending_import', dict(state))

    def record_pending_import_cleared(self) -> None:
        self.append('pending_import_cleared', {})

    def record_operation_started(
        self,
        operation_kind: str,
        operation_ref_id: str,
        *,
        document_id: str = '',
        detail: dict[str, Any] | None = None,
    ) -> None:
        if operation_kind not in OPERATION_KINDS:
            raise SessionJournalError(
                f'unknown operation kind: {operation_kind}'
            )
        self.append(
            'operation_started',
            {
                'operation_kind': operation_kind,
                'operation_ref_id': operation_ref_id,
                'document_id': document_id,
                'detail': dict(detail or {}),
            },
        )

    def record_operation_finished(
        self,
        operation_ref_id: str,
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.append(
            'operation_finished',
            {
                'operation_ref_id': operation_ref_id,
                'detail': dict(detail or {}),
            },
        )

    def record_write_intent(
        self,
        *,
        intent_id: str,
        path: str,
        mode: str = 'replace',
        expected_sha256: str | None = None,
        document_id: str = '',
    ) -> None:
        self.append(
            'write_intent',
            {
                'intent_id': intent_id,
                'path': path,
                'mode': mode,
                'expected_sha256': expected_sha256,
                'document_id': document_id,
            },
        )

    def record_write_completed(
        self, intent_id: str, *, sha256_hex: str | None = None
    ) -> None:
        self.append(
            'write_completed',
            {'intent_id': intent_id, 'sha256': sha256_hex},
        )

    def record_acquisition_stage(
        self,
        *,
        run_id: str,
        stage: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.append(
            'acquisition_stage',
            {'run_id': run_id, 'stage': stage, 'detail': dict(detail or {})},
        )

    def note_failure(
        self, failure_class: str, *, detail: str | None = None
    ) -> None:
        """Record a caught launch/run failure — the difference between
        ``application_crash`` and ``forced_termination`` next launch."""

        self.envelope = self.envelope.model_copy(
            update={'failure_class': failure_class}
        )
        self._write_envelope()
        self.append(
            'failure_noted',
            {'failure_class': failure_class, 'detail': detail},
        )

    def close_clean(self) -> None:
        if self._closed:
            return
        closed_at = utc_now_iso()
        self.append('session_close_clean', {})
        self.envelope = self.envelope.model_copy(
            update={
                'closed_clean': True,
                'closed_at_utc': closed_at,
                'last_seen_at_utc': closed_at,
            }
        )
        self._write_envelope()
        self._closed = True

    def close(self) -> None:
        """Drop the writer without a clean marker — abnormal teardown."""

        self._closed = True

    # -- compaction -----------------------------------------------------

    def _compact(self) -> None:
        """Fold history into a snapshot entry and restart the file.

        The compaction entry chains to the pre-fold head
        (``compacted_from_sha256``); inspection verifies the chain from
        that line forward and reports the folded prefix as trusted.
        """

        journal_path = self.directory / JOURNAL_NAME
        inspection = inspect_journal_dir(
            self.directory, verify_envelope=False
        )
        folded = _fold_entries(
            inspection.entries, inspection.resolved_payloads
        ).as_payload()
        compaction = {
            'entries_truncated': self._seq - 1,
            'compacted_from_sha256': self._head_sha256,
            'state': folded,
        }
        entry = JournalEntry.create(
            seq=self._seq,
            session_id=self.envelope.session_id,
            kind='snapshot_compaction',
            recorded_at_utc=utc_now_iso(),
            prev_sha256=self._head_sha256,
            payload=compaction,
        )
        line = self._entry_line(entry)
        from .export_io import write_bytes_atomic

        write_bytes_atomic(journal_path, line)
        self._seq = entry.seq + 1
        self._head_sha256 = entry.entry_sha256


# ---------------------------------------------------------------------------
# Inspection (journal file → verified entries)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JournalInspection:
    """Verified parse of one journal directory.

    ``resolved_payloads`` aligns 1:1 with ``entries`` — sidecar
    payloads already sha-verified and decoded.
    """

    path: Path
    verdict: JournalIntegrity
    entries: tuple[JournalEntry, ...]
    resolved_payloads: tuple[dict[str, Any], ...]
    envelope: SessionEnvelope | None
    head_entry_sha256: str | None
    last_good_seq: int
    invalid_line_no: int | None
    reason: str | None = None


def _resolve_entry_payload(
    session_dir: Path, entry: JournalEntry
) -> tuple[dict[str, Any], str | None]:
    """Inline payload, or verified sidecar read. Returns (payload, error)."""

    marker = entry.payload.get('_sidecar_sha256')
    if marker is None:
        return dict(entry.payload), None
    if not isinstance(marker, str) or not re.match(
        _SHA256_PATTERN, marker
    ):
        return {}, 'payload sidecar sha is malformed'
    sidecar = session_dir / PAYLOADS_DIRNAME / f'{marker}.json'
    try:
        raw = sidecar.read_bytes()
    except OSError:
        return {}, f'payload sidecar missing: {marker[:16]}'
    if sha256(raw).hexdigest() != marker:
        return {}, f'payload sidecar sha mismatch: {marker[:16]}'
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return {}, 'payload sidecar is not valid JSON'
    if not isinstance(payload, dict):
        return {}, 'payload sidecar is not a JSON object'
    return payload, None


def inspect_journal_dir(
    session_dir: Path, *, verify_envelope: bool = True
) -> JournalInspection:
    """Parse + verify ``envelope.json`` and ``journal.jsonl``.

    A torn last line (the classic crash mid-append) is reported as
    ``tail_torn`` with ``last_good_seq`` — earlier entries remain valid.
    Corruption anywhere else, an unknown kind or a version mismatch fails
    closed.
    """

    session_dir = Path(session_dir)
    envelope_path = session_dir / ENVELOPE_NAME
    journal_path = session_dir / JOURNAL_NAME

    envelope: SessionEnvelope | None = None
    if verify_envelope:
        try:
            raw_envelope = json.loads(
                envelope_path.read_text(encoding='utf-8')
            )
        except (OSError, ValueError):
            return JournalInspection(
                session_dir, 'unreadable', (), (), None, None, 0, None,
                'envelope is missing or not valid JSON',
            )
        if not isinstance(raw_envelope, dict):
            return JournalInspection(
                session_dir, 'unreadable', (), (), None, None, 0, None,
                'envelope is not a JSON object',
            )
        if raw_envelope.get('schema_version') != SESSION_JOURNAL_SCHEMA_VERSION:
            return JournalInspection(
                session_dir, 'incompatible', (), (), None, None, 0, None,
                'journal envelope schema is '
                f"{raw_envelope.get('schema_version')!r}, "
                f'this build reads {SESSION_JOURNAL_SCHEMA_VERSION}',
            )
        try:
            envelope = SessionEnvelope.model_validate(raw_envelope)
        except ValueError as exc:
            return JournalInspection(
                session_dir, 'unreadable', (), (), None, None, 0, None,
                f'envelope failed validation: {exc}',
            )
        if envelope.session_id != session_dir.name:
            return JournalInspection(
                session_dir, 'corrupt', (), (), envelope, None, 0, None,
                'envelope session id disagrees with its directory name',
            )

    try:
        raw = journal_path.read_bytes()
    except OSError:
        return JournalInspection(
            session_dir,
            'intact' if envelope is not None else 'unreadable',
            (),
            (),
            envelope,
            envelope.identity_sha256() if envelope is not None else None,
            0,
            None,
        )

    entries: list[JournalEntry] = []
    payloads: list[dict[str, Any]] = []
    prev_sha = (
        envelope.identity_sha256() if envelope is not None else None
    )
    verdict: JournalIntegrity = 'intact'
    invalid_line: int | None = None
    reason: str | None = None
    last_good_seq = 0

    lines = raw.split(b'\n')
    for index, raw_line in enumerate(lines):
        if not raw_line.strip():
            continue
        line_no = index + 1
        failure: str | None = None
        try:
            decoded = json.loads(raw_line.decode('utf-8'))
            if not isinstance(decoded, dict):
                raise ValueError('journal line is not a JSON object')
            entry = JournalEntry.model_validate(decoded)
            if entry.kind not in ENTRY_KINDS:
                failure = (
                    f'unknown journal entry kind {entry.kind!r} '
                    '(newer journal format?)'
                )
                verdict = 'incompatible'
            elif (
                envelope is not None
                and entry.session_id != envelope.session_id
            ):
                failure = 'entry session id disagrees with the envelope'
            elif canonical_sha256(entry.identity_payload()) != (
                entry.entry_sha256
            ):
                failure = 'entry sha256 does not match its content'
            elif index == 0 and entry.seq != 1:
                if entry.kind != 'snapshot_compaction':
                    failure = (
                        'journal begins at seq '
                        f'{entry.seq} without a compaction head'
                    )
                elif entry.prev_sha256 != entry.payload.get(
                    'compacted_from_sha256'
                ):
                    failure = 'compaction head does not name its folded sha'
            elif index > 0:
                prior = entries[-1]
                if entry.seq != prior.seq + 1:
                    failure = (
                        f'entry seq jumps {prior.seq} -> {entry.seq}'
                    )
                elif entry.prev_sha256 != prior.entry_sha256:
                    failure = 'entry sha chain is broken'
            elif (
                index == 0
                and entry.seq == 1
                and prev_sha is not None
                and entry.prev_sha256 != prev_sha
            ):
                failure = 'first entry does not chain to the envelope'
            if failure is None:
                payload, sidecar_error = _resolve_entry_payload(
                    session_dir, entry
                )
                if sidecar_error is not None:
                    failure = sidecar_error
        except (ValueError, UnicodeDecodeError) as exc:
            failure = f'journal line failed to parse: {exc}'
        if failure is not None:
            # A torn write is only honest at the tail: an invalid line
            # earlier in the file means the journal was damaged after the
            # fact.
            remaining = [
                line for line in lines[index + 1 :] if line.strip()
            ]
            invalid_line = line_no
            if remaining:
                verdict = 'corrupt'
                reason = f'line {line_no}: {failure}'
            else:
                verdict = 'tail_torn' if verdict == 'intact' else verdict
                reason = f'last line {line_no}: {failure}'
            break
        entries.append(entry)
        payloads.append(payload)
        last_good_seq = entry.seq

    return JournalInspection(
        session_dir,
        verdict,
        tuple(entries),
        tuple(payloads),
        envelope,
        entries[-1].entry_sha256 if entries else (
            envelope.identity_sha256() if envelope is not None else None
        ),
        last_good_seq,
        invalid_line,
        reason,
    )


# ---------------------------------------------------------------------------
# Folded journal state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FoldedJournalState:
    """Latest-wins fold of a session's journal entries."""

    project_document_id: str | None
    workspace_state: dict[str, Any] | None
    scene_drafts: dict[str, dict[str, Any]]
    pending_annotations: dict[str, dict[str, Any]]
    pending_import: dict[str, Any] | None
    open_operations: dict[str, dict[str, Any]]
    open_write_intents: dict[str, dict[str, Any]]
    acquisition_stages: dict[str, str]
    failure_class: str | None
    heartbeat_count: int
    entry_count: int

    def as_payload(self) -> dict[str, Any]:
        return {
            'project_document_id': self.project_document_id,
            'workspace_state': self.workspace_state,
            'scene_drafts': self.scene_drafts,
            'pending_annotations': self.pending_annotations,
            'pending_import': self.pending_import,
            'open_operations': self.open_operations,
            'open_write_intents': self.open_write_intents,
            'acquisition_stages': self.acquisition_stages,
            'failure_class': self.failure_class,
            'heartbeat_count': self.heartbeat_count,
            'entry_count': self.entry_count,
        }


def _fold_entries(
    entries: Iterable[JournalEntry],
    resolved_payloads: Iterable[dict[str, Any]] | None = None,
) -> FoldedJournalState:
    """Fold verified entries (and a compaction head) into session state.

    ``resolved_payloads`` — the inspection's decoded payloads aligned to
    ``entries``; without it the wire form is used (sidecar markers stay
    unresolved, which is fine for callers that only need envelope facts).
    """

    payload_list = list(resolved_payloads) if resolved_payloads else None
    project_document_id: str | None = None
    workspace_state: dict[str, Any] | None = None
    scene_drafts: dict[str, dict[str, Any]] = {}
    pending_annotations: dict[str, dict[str, Any]] = {}
    pending_import: dict[str, Any] | None = None
    open_operations: dict[str, dict[str, Any]] = {}
    open_write_intents: dict[str, dict[str, Any]] = {}
    acquisition_stages: dict[str, str] = {}
    failure_class: str | None = None
    heartbeat_count = 0
    count = 0
    for index, entry in enumerate(entries):
        count += 1
        payload = dict(
            payload_list[index]
            if payload_list is not None and index < len(payload_list)
            else entry.payload
        )
        if entry.kind == 'snapshot_compaction':
            folded = FoldedJournalState(
                **{
                    key: value
                    for key, value in payload.get(
                        'state', {}
                    ).items()
                    if key in FoldedJournalState.__dataclass_fields__
                }
            )
            project_document_id = folded.project_document_id
            workspace_state = folded.workspace_state
            scene_drafts.update(folded.scene_drafts)
            pending_annotations.update(folded.pending_annotations)
            pending_import = folded.pending_import
            open_operations.update(folded.open_operations)
            open_write_intents.update(folded.open_write_intents)
            acquisition_stages.update(folded.acquisition_stages)
            failure_class = folded.failure_class or failure_class
            heartbeat_count += folded.heartbeat_count
            continue
        if entry.kind == 'project_bound':
            project_document_id = payload.get('document_id')
        elif entry.kind == 'workspace_snapshot':
            workspace_state = payload
        elif entry.kind == 'scene_draft':
            doc = payload.get('document_id')
            if isinstance(doc, str) and doc:
                scene_drafts[doc] = payload
        elif entry.kind == 'scene_draft_cleared':
            scene_drafts.pop(payload.get('document_id'), None)
        elif entry.kind == 'pending_annotation':
            annotation_id = payload.get('annotation_id')
            if isinstance(annotation_id, str) and annotation_id:
                pending_annotations[annotation_id] = payload
        elif entry.kind == 'pending_annotation_cleared':
            pending_annotations.pop(payload.get('annotation_id'), None)
        elif entry.kind == 'pending_import':
            pending_import = payload
        elif entry.kind == 'pending_import_cleared':
            pending_import = None
        elif entry.kind == 'operation_started':
            ref = payload.get('operation_ref_id')
            if isinstance(ref, str) and ref:
                open_operations[ref] = payload
        elif entry.kind == 'operation_finished':
            open_operations.pop(payload.get('operation_ref_id'), None)
        elif entry.kind == 'write_intent':
            intent_id = payload.get('intent_id')
            if isinstance(intent_id, str) and intent_id:
                open_write_intents[intent_id] = payload
        elif entry.kind == 'write_completed':
            open_write_intents.pop(payload.get('intent_id'), None)
        elif entry.kind == 'acquisition_stage':
            run_id = payload.get('run_id')
            if isinstance(run_id, str) and run_id:
                acquisition_stages[run_id] = str(payload.get('stage'))
        elif entry.kind == 'heartbeat':
            heartbeat_count += 1
        elif entry.kind == 'failure_noted':
            failure_class = payload.get('failure_class')
    return FoldedJournalState(
        project_document_id=project_document_id,
        workspace_state=workspace_state,
        scene_drafts=scene_drafts,
        pending_annotations=pending_annotations,
        pending_import=pending_import,
        open_operations=open_operations,
        open_write_intents=open_write_intents,
        acquisition_stages=acquisition_stages,
        failure_class=failure_class,
        heartbeat_count=heartbeat_count,
        entry_count=count,
    )


# ---------------------------------------------------------------------------
# Ending classification
# ---------------------------------------------------------------------------


def classify_session_ending(
    envelope: SessionEnvelope,
    *,
    entries: Iterable[JournalEntry] = (),
    pid_alive: Callable[[int], bool] | None = None,
    current_boot_token: str | None = None,
    launch_record: Any = None,
    current_native_schema_version: int | None = None,
) -> tuple[SessionEnding, tuple[str, ...]]:
    """Classify how a session ended — strictly from evidence.

    Order matters: explicit clean close beats everything; version
    mismatch is ``incompatible`` before any liveness reasoning (the data
    itself is untrusted); a live pid means the session never ended; a
    changed boot token means the machine restarted; a recorded failure
    class means the app saw its own crash; a dead pid with none of that
    is ``forced_termination`` — kill, power-flicker-in-place, or a crash
    the handlers never saw.
    """

    reasons: list[str] = []
    if envelope.closed_clean:
        return 'clean', ('session wrote its clean-close marker',)
    if envelope.schema_version != SESSION_JOURNAL_SCHEMA_VERSION:
        return 'incompatible', (
            f'journal schema v{envelope.schema_version} is not readable by '
            f'this build (v{SESSION_JOURNAL_SCHEMA_VERSION})',
        )
    if (
        current_native_schema_version is not None
        and envelope.native_schema_version is not None
        and envelope.native_schema_version > current_native_schema_version
    ):
        return 'incompatible', (
            'recovery data was written against native schema v'
            f'{envelope.native_schema_version} — newer than this build '
            f'(v{current_native_schema_version})'
        )
    if pid_alive is None:
        pid_alive = _default_pid_alive
    try:
        alive = pid_alive(envelope.pid)
    except Exception:
        alive = False
    if alive:
        return 'still_running', (
            f'pid {envelope.pid} is still alive — the session did not end',
        )
    if _boot_tokens_match(envelope.boot_token, current_boot_token) is (
        False
    ) and envelope.boot_token is not None and current_boot_token is not None:
        return 'os_restart_or_power_loss', (
            'the machine boot token changed since the session started',
        )
    if envelope.boot_token is not None and current_boot_token is not None:
        reasons.append('same boot token — the OS stayed up')
    failure_class = envelope.failure_class
    noted = [
        e.payload.get('failure_class')
        for e in entries
        if e.kind == 'failure_noted'
    ]
    if failure_class is None and noted:
        failure_class = str(noted[-1])
    record_class = getattr(launch_record, 'failure_class', None)
    record_correlation = getattr(
        launch_record, 'crash_correlation_id', None
    )
    if failure_class is not None or record_class is not None or (
        record_correlation is not None
    ):
        detail = failure_class or record_class or 'crash evidence'
        return 'application_crash', (
            *reasons,
            f'the session recorded failure evidence ({detail})',
        )
    reasons.append(
        'process is gone without clean-close or crash evidence — '
        'forced termination, or a crash nothing observed'
    )
    return 'forced_termination', tuple(reasons)


# ---------------------------------------------------------------------------
# Recovery report + reconciliation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoverableItem:
    """One thing the crashed session's journal can restore or resume.

    ``availability='blocked'`` means the evidence behind the item failed
    verification (e.g. draft hash changed) — it is shown, never used."""

    kind: RecoverableItemKind
    ref_id: str
    document_id: str
    availability: ItemAvailability
    summary: str
    detail: dict[str, Any]


@dataclass(frozen=True)
class ReconciliationItem:
    """An in-flight external effect whose completion is *unknown*.

    ``state`` is one of the mandated honest states; ``resolution``
    describes the sealed/read-back evidence that resolved it, or None
    while it remains open.
    """

    kind: ReconciliationKind
    ref_id: str
    document_id: str
    state: ReconciliationState
    summary: str
    detail: dict[str, Any]
    resolution: str | None = None


@dataclass(frozen=True)
class SessionRecoveryReport:
    """Everything one crashed session leaves for the operator."""

    session_id: str
    launch_id: str | None
    app_version: str | None
    build_id: str | None
    journal_schema_version: int
    native_schema_version: int | None
    pid: int
    started_at_utc: str
    last_seen_at_utc: str
    project_document_id: str | None
    ending: SessionEnding
    ending_reasons: tuple[str, ...]
    integrity: JournalIntegrity
    integrity_reason: str | None
    entry_count: int
    head_entry_sha256: str | None
    envelope_sha256: str | None
    journal_dir: str
    items: tuple[RecoverableItem, ...]
    reconciliations: tuple[ReconciliationItem, ...]
    decisions: tuple['SessionRecoveryDecision', ...]
    resolved: bool


@dataclass(frozen=True)
class RejectedRecoveryData:
    """Recovery data that failed closed — kept on disk, never used."""

    session_id: str | None
    journal_dir: str
    integrity: JournalIntegrity
    reason: str


@dataclass(frozen=True)
class SessionRecoveryInspection:
    reports: tuple[SessionRecoveryReport, ...]
    rejected: tuple[RejectedRecoveryData, ...]


# ---------------------------------------------------------------------------
# Sealed records (schema v100)
# ---------------------------------------------------------------------------


class SessionRecoveryJournalRecord(BaseModel):
    """One sealed detection row per crashed journal — the durable
    tamper-evidence that survives file retention."""

    model_config = ConfigDict(frozen=True)

    journal_id: str = Field(min_length=1)
    journal_sha256: str = Field(pattern=_SHA256_PATTERN)
    session_id: str = Field(min_length=1)
    document_id: str
    detected_by_session_id: str | None = None
    build_id: str | None = None
    app_version: str | None = None
    journal_schema_version: int | None = None
    native_schema_version: int | None = None
    pid: int | None = None
    started_at_utc: str | None = None
    last_seen_at_utc: str | None = None
    ending: SessionEnding
    integrity: JournalIntegrity
    entry_count: int = Field(ge=0)
    head_entry_sha256: str | None = None
    envelope_sha256: str | None = None
    detected_at_utc: str

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'journal_id', 'journal_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'SessionRecoveryJournalRecord':
        return _seal(
            cls, kwargs, 'journal_id', 'journal_sha256', 'srjrn'
        )


class SessionRecoveryDecision(BaseModel):
    """Append-only operator decision on a crashed session or one item."""

    model_config = ConfigDict(frozen=True)

    decision_id: str = Field(min_length=1)
    decision_sha256: str = Field(pattern=_SHA256_PATTERN)
    session_id: str = Field(min_length=1)
    document_id: str
    scope_kind: str = Field(min_length=1)
    scope_ref_id: str | None = None
    action: DecisionAction
    actor: str = Field(min_length=1)
    decided_at_utc: str
    restoring_session_id: str | None = None
    reason: str | None = None

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'decision_id', 'decision_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'SessionRecoveryDecision':
        return _seal(
            cls, kwargs, 'decision_id', 'decision_sha256', 'srdec'
        )


class SessionReconciliationRecord(BaseModel):
    """Sealed outcome for one uncertain external effect."""

    model_config = ConfigDict(frozen=True)

    reconciliation_id: str = Field(min_length=1)
    reconciliation_sha256: str = Field(pattern=_SHA256_PATTERN)
    session_id: str = Field(min_length=1)
    document_id: str
    operation_kind: ReconciliationKind
    operation_ref_id: str = Field(min_length=1)
    verdict: ReconciliationVerdict
    evidence_detail: str | None = None
    decided_by_session_id: str | None = None
    recorded_at_utc: str

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'reconciliation_id', 'reconciliation_sha256'},
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'SessionReconciliationRecord':
        return _seal(
            cls,
            kwargs,
            'reconciliation_id',
            'reconciliation_sha256',
            'srrcn',
        )


# ---------------------------------------------------------------------------
# Item / reconciliation builders
# ---------------------------------------------------------------------------


def _session_documents(
    envelope: SessionEnvelope, state: FoldedJournalState
) -> tuple[str, ...]:
    docs: set[str] = set()
    for value in (
        envelope.project_document_id,
        state.project_document_id,
    ):
        if value:
            docs.add(value)
    for payload in state.scene_drafts.values():
        doc = payload.get('document_id')
        if isinstance(doc, str) and doc:
            docs.add(doc)
    for payload in state.open_operations.values():
        doc = payload.get('document_id')
        if isinstance(doc, str) and doc:
            docs.add(doc)
    for payload in state.open_write_intents.values():
        doc = payload.get('document_id')
        if isinstance(doc, str) and doc:
            docs.add(doc)
    for payload in state.pending_annotations.values():
        doc = payload.get('document_id')
        if isinstance(doc, str) and doc:
            docs.add(doc)
    return tuple(sorted(docs))


def _draft_items(
    state: FoldedJournalState,
    documents: tuple[str, ...],
    scene_repository: Any,
) -> list[RecoverableItem]:
    items: list[RecoverableItem] = []
    seen: set[str] = set()
    for document_id, payload in sorted(state.scene_drafts.items()):
        seen.add(document_id)
        ref = str(payload.get('content_hash') or document_id)
        availability: ItemAvailability = 'available'
        reason = 'draft snapshot is persisted and hash-verified'
        if scene_repository is not None:
            try:
                snapshot = scene_repository.recovery(document_id)
            except Exception as exc:
                availability = 'blocked'
                reason = f'draft snapshot failed integrity: {exc}'
            else:
                if snapshot is None:
                    availability = 'blocked'
                    reason = 'journaled draft has no persisted snapshot'
                elif (
                    snapshot.content_hash
                    != payload.get('content_hash')
                ):
                    availability = 'blocked'
                    reason = (
                        'persisted draft hash no longer matches the '
                        'journaled content'
                    )
        items.append(
            RecoverableItem(
                kind='scene_draft',
                ref_id=ref,
                document_id=document_id,
                availability=availability,
                summary=f'未保存のプロジェクト編集 ({document_id})',
                detail={**payload, 'state': reason},
            )
        )
    if scene_repository is not None:
        for document_id in documents:
            if document_id in seen:
                continue
            try:
                snapshot = scene_repository.recovery(document_id)
            except Exception:
                continue
            if snapshot is None:
                continue
            items.append(
                RecoverableItem(
                    kind='scene_draft',
                    ref_id=snapshot.content_hash,
                    document_id=document_id,
                    availability='available',
                    summary=f'未保存のプロジェクト編集 ({document_id})',
                    detail={
                        'document_id': document_id,
                        'content_hash': snapshot.content_hash,
                        'source_revision_id': getattr(
                            snapshot, 'source_revision_id', None
                        ),
                        'state': (
                            'draft snapshot persisted before the crash '
                            '(not journaled)'
                        ),
                    },
                )
            )
    return items


def _workspace_items(state: FoldedJournalState) -> list[RecoverableItem]:
    if not state.workspace_state:
        return []
    payload = state.workspace_state
    return [
        RecoverableItem(
            kind='workspace_state',
            ref_id=str(payload.get('destination') or 'workspace'),
            document_id=str(
                payload.get('document_id')
                or state.project_document_id
                or ''
            ),
            availability='available',
            summary='画面構成・ナビゲーション状態',
            detail=payload,
        )
    ]


def _annotation_items(state: FoldedJournalState) -> list[RecoverableItem]:
    items: list[RecoverableItem] = []
    for annotation_id, payload in sorted(
        state.pending_annotations.items()
    ):
        items.append(
            RecoverableItem(
                kind='pending_annotation',
                ref_id=annotation_id,
                document_id=str(payload.get('document_id') or ''),
                availability='available',
                summary=str(payload.get('summary') or '未確定の注記'),
                detail=payload,
            )
        )
    return items


def _pending_import_item(state: FoldedJournalState) -> list[RecoverableItem]:
    if not state.pending_import:
        return []
    payload = state.pending_import
    return [
        RecoverableItem(
            kind='pending_import',
            ref_id=str(
                payload.get('source_label')
                or payload.get('source_kind')
                or 'pending-import'
            ),
            document_id=str(payload.get('document_id') or ''),
            availability='available',
            summary=(
                '取り込み途中の測定データ '
                f"({payload.get('source_label') or '不明なソース'})"
            ),
            detail=payload,
        )
    ]


def _project_binding_item(
    envelope: SessionEnvelope, state: FoldedJournalState
) -> list[RecoverableItem]:
    document_id = (
        state.project_document_id or envelope.project_document_id
    )
    if not document_id:
        return []
    dirty = []
    if state.workspace_state:
        dirty = list(state.workspace_state.get('dirty_documents') or [])
    return [
        RecoverableItem(
            kind='project_binding',
            ref_id=document_id,
            document_id=document_id,
            availability='available',
            summary=(
                f'セッションが開いていたプロジェクト: {document_id}'
                + ('（未保存の変更あり）' if dirty else '')
            ),
            detail={
                'document_id': document_id,
                'dirty_documents': dirty,
            },
        )
    ]


def _authority_resume_items(
    documents: tuple[str, ...],
    state: FoldedJournalState,
    orchestrator: Any,
    campaign_repository: Any,
) -> list[RecoverableItem]:
    items: list[RecoverableItem] = []
    if orchestrator is not None:
        for document_id in documents:
            try:
                open_run = orchestrator.get_open_run(document_id)
            except Exception:
                continue
            if open_run is None:
                continue
            run, run_state = open_run
            items.append(
                RecoverableItem(
                    kind='commissioning_run',
                    ref_id=run.run_id,
                    document_id=document_id,
                    availability='available',
                    summary=(
                        'コミッショニング実行を再開できます '
                        f'(最後の確定段階: {run_state.current_stage})'
                    ),
                    detail={
                        'run_id': run.run_id,
                        'current_stage': run_state.current_stage,
                        'transition_count': run_state.transition_count,
                        'state': (
                            'resumes from sealed transitions — no stage '
                            'is skipped'
                        ),
                    },
                )
            )
    for ref_id, payload in sorted(state.open_operations.items()):
        if payload.get('operation_kind') != 'measurement_campaign':
            continue
        items.append(
            RecoverableItem(
                kind='measurement_campaign',
                ref_id=ref_id,
                document_id=str(payload.get('document_id') or ''),
                availability='available',
                summary=f'測定キャンペーンの進捗 ({ref_id})',
                detail={
                    **payload,
                    'state': 'sealed campaign evidence is resumable',
                },
            )
        )
    return items


_TERMINAL_APPLY_VERDICTS: frozenset[str] = frozenset(
    {'fully_applied', 'rolled_back', 'apply_failed', 'not_started'}
)


def _apply_transaction_reconciliations(
    state: FoldedJournalState,
    documents: tuple[str, ...],
    apply_repository: Any,
    readback: Callable[[str, dict[str, Any]], str | None] | None,
) -> list[ReconciliationItem]:
    """Fold journal-declared apply ops + unclosed sealed transactions.

    A declared op that never sealed a transaction row, or a transaction
    sealed mid-flight/non-terminal, is ``DEVICE_STATE_UNKNOWN`` until
    sealed evidence (or an adapter read-back) says otherwise.
    """

    if apply_repository is None:
        return []
    from .cad_apply_transaction import evaluate_apply_state

    items: list[ReconciliationItem] = []
    seen_refs: set[str] = set()

    plans: dict[str, Any] = {}
    transactions: dict[str, Any] = {}
    plans_by_sha: dict[str, Any] = {}
    writes: list[Any] = []
    verifications: list[Any] = []
    for doc in documents or ('',):
        try:
            for plan in apply_repository.plans.list(doc):
                plans[plan.plan_id] = plan
                plans_by_sha[plan.plan_sha256] = plan
            for transaction in apply_repository.transactions.list(doc):
                transactions[transaction.transaction_id] = transaction
            writes.extend(apply_repository.writes.list(doc))
            verifications.extend(apply_repository.verifications.list(doc))
        except Exception:
            continue

    def _derived_verdict(transaction: Any, ref_id: str) -> str | None:
        """Fold sealed plan/writes/verification — never guesses."""

        plan = None
        if transaction is not None:
            plan = plans_by_sha.get(transaction.plan_ref.ref_sha256)
        if plan is None:
            plan = plans.get(ref_id)
        if plan is None:
            return None
        plan_writes = tuple(
            record
            for record in writes
            if record.plan_ref.ref_sha256 == plan.plan_sha256
        )
        verification = next(
            (
                record
                for record in verifications
                if record.plan_ref.ref_sha256 == plan.plan_sha256
            ),
            None,
        )
        try:
            return evaluate_apply_state(plan, plan_writes, verification)
        except Exception:
            return None

    def _emit(
        ref_id: str,
        doc: str,
        transaction: Any,
        payload: dict[str, Any],
        *,
        summary: str,
    ) -> None:
        seen_refs.add(ref_id)
        if transaction is not None and (
            transaction.closed_at_utc is not None
            and transaction.state_verdict in _TERMINAL_APPLY_VERDICTS
        ):
            items.append(
                ReconciliationItem(
                    kind='device_apply_transaction',
                    ref_id=ref_id,
                    document_id=transaction.document_id,
                    state='DEVICE_STATE_UNKNOWN',
                    summary='デバイス適用: 確定済みの記録あり',
                    detail={'state_verdict': transaction.state_verdict},
                    resolution=(
                        f'sealed verdict: {transaction.state_verdict}'
                    ),
                )
            )
            return
        derived = (
            transaction.state_verdict
            if transaction is not None
            else _derived_verdict(transaction, ref_id)
        )
        if derived in _TERMINAL_APPLY_VERDICTS:
            items.append(
                ReconciliationItem(
                    kind='device_apply_transaction',
                    ref_id=ref_id,
                    document_id=(
                        transaction.document_id
                        if transaction is not None
                        else doc
                    ),
                    state='DEVICE_STATE_UNKNOWN',
                    summary='デバイス適用: 記録から確定状態を復元',
                    detail={
                        **payload,
                        'derived_verdict': derived,
                    },
                    resolution=f'sealed evidence fold: {derived}',
                )
            )
            return
        resolution = None
        if readback is not None:
            try:
                outcome = readback(
                    ref_id, {'document_id': doc, **payload}
                )
            except Exception:
                outcome = None
            if outcome == 'confirmed':
                resolution = 'machine read-back confirmed the device state'
            elif outcome == 'rolled_back':
                resolution = 'machine read-back confirmed rollback'
            else:
                resolution = 'readback_unavailable'
        items.append(
            ReconciliationItem(
                kind='device_apply_transaction',
                ref_id=ref_id,
                document_id=(
                    transaction.document_id
                    if transaction is not None
                    else doc
                ),
                state='DEVICE_STATE_UNKNOWN',
                summary=summary,
                detail={
                    **payload,
                    'derived_verdict': derived,
                    'state': 'RECONCILIATION_REQUIRED',
                },
                resolution=resolution,
            )
        )

    for payload in state.open_operations.values():
        if payload.get('operation_kind') != 'device_apply_transaction':
            continue
        ref_id = str(payload.get('operation_ref_id') or '')
        doc = str(payload.get('document_id') or '')
        transaction = transactions.get(ref_id)
        if transaction is None:
            transaction = next(
                (
                    tx
                    for tx in transactions.values()
                    if tx.plan_ref.ref_id == ref_id
                ),
                None,
            )
        _emit(
            ref_id,
            doc,
            transaction,
            payload,
            summary=(
                'デバイス適用: 書込み完了が不確定 — '
                '機器読戻しで確認してください'
            ),
        )
    # Unclosed sealed transactions carry the same uncertainty even when
    # the journal never declared them.
    for transaction in transactions.values():
        if transaction.transaction_id in seen_refs:
            continue
        if transaction.closed_at_utc is not None:
            continue
        _emit(
            transaction.transaction_id,
            transaction.document_id,
            transaction,
            {},
            summary=(
                'デバイス適用トランザクションが未終了のままです — '
                '機器読戻しで確認してください'
            ),
        )
    return items


def _acquisition_reconciliations(
    state: FoldedJournalState,
    sweep_repository: Any,
) -> list[ReconciliationItem]:
    if sweep_repository is None:
        return []
    terminal = {'completed', 'failed', 'cancelled'}
    items: list[ReconciliationItem] = []
    refs: set[str] = set(state.acquisition_stages)
    for payload in state.open_operations.values():
        if payload.get('operation_kind') == 'sweep_acquisition':
            refs.add(str(payload.get('operation_ref_id')))
    for run_id in sorted(refs):
        last_stage = state.acquisition_stages.get(run_id)
        try:
            record = sweep_repository.get_run_by_run_id(run_id)
        except Exception:
            record = None
        if record is not None and (
            record.stage in terminal and record.outcome is not None
        ):
            continue
        items.append(
            ReconciliationItem(
                kind='sweep_acquisition',
                ref_id=run_id,
                document_id=str(
                    getattr(record, 'document_id', '') or ''
                ),
                state='ACQUISITION_INCOMPLETE',
                summary=(
                    '測定取得が中断されました — '
                    '完了した測定としては扱われません'
                ),
                detail={
                    'run_id': run_id,
                    'last_journaled_stage': last_stage,
                    'sealed_stage': getattr(record, 'stage', None),
                    'state': (
                        'sealed run evidence is absent or non-terminal; '
                        'a partial capture never counts as a completed '
                        'measurement'
                    ),
                },
            )
        )
    return items


def _write_intent_reconciliations(
    state: FoldedJournalState,
) -> list[ReconciliationItem]:
    items: list[ReconciliationItem] = []
    for intent_id, payload in sorted(state.open_write_intents.items()):
        path_value = payload.get('path')
        expected = payload.get('expected_sha256')
        resolution: str | None = None
        detail: dict[str, Any] = dict(payload)
        path = Path(str(path_value)) if path_value else None
        if path is None:
            detail['verify'] = 'intent recorded no path'
        elif not path.exists():
            detail['verify'] = 'target path is absent'
        elif expected:
            actual = _file_sha256(path)
            if actual == expected:
                resolution = 'file hash matches the recorded intent'
                detail['verify'] = 'sha256 match'
            else:
                detail['verify'] = (
                    f'file sha {actual[:16]}… differs from intent'
                )
        else:
            detail['verify'] = (
                'no content hash was recorded — completion is unknowable'
            )
        items.append(
            ReconciliationItem(
                kind='file_write',
                ref_id=intent_id,
                document_id=str(payload.get('document_id') or ''),
                state='WRITE_COMPLETION_UNKNOWN',
                summary=(
                    'ファイル書込みの完了が不確定です: '
                    f'{path_value}'
                ),
                detail=detail,
                resolution=resolution,
            )
        )
    return items


# ---------------------------------------------------------------------------
# Top-level inspection
# ---------------------------------------------------------------------------


def session_journal_root(data_dir: Path | str) -> Path:
    return Path(data_dir) / SESSION_JOURNAL_DIRNAME


def inspect_recoverable_sessions(
    data_dir: Path | str,
    *,
    scene_repository: Any = None,
    session_recovery_repository: Any = None,
    sweep_repository: Any = None,
    apply_repository: Any = None,
    orchestrator: Any = None,
    campaign_repository: Any = None,
    launch_metadata: Any = None,
    pid_alive: Callable[[int], bool] | None = None,
    current_boot_token: str | None = None,
    current_native_schema_version: int | None = None,
    current_session_id: str | None = None,
    readback: Callable[[str, dict[str, Any]], str | None] | None = None,
    record_detection: bool = True,
) -> SessionRecoveryInspection:
    """Enumerate crashed session journals and build honest reports.

    Only sessions with real evidence (unclean ending, verified journal)
    produce reports; corrupt/incompatible data is reported as
    ``rejected`` and left untouched.
    """

    root = session_journal_root(data_dir)
    reports: list[SessionRecoveryReport] = []
    rejected: list[RejectedRecoveryData] = []
    if not root.is_dir():
        return SessionRecoveryInspection((), ())
    boot_now = (
        current_boot_token
        if current_boot_token is not None
        else boot_token()
    )
    launch_records: dict[str, Any] = {}
    if launch_metadata is not None:
        for record in getattr(launch_metadata, 'records', ()):
            launch_records[getattr(record, 'launch_id', '')] = record

    for session_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        inspection = inspect_journal_dir(session_dir)
        envelope = inspection.envelope
        if envelope is None:
            rejected.append(
                RejectedRecoveryData(
                    session_id=session_dir.name,
                    journal_dir=str(session_dir),
                    integrity=inspection.verdict,
                    reason=inspection.reason or 'envelope unreadable',
                )
            )
            continue
        if inspection.verdict in ('incompatible', 'corrupt'):
            rejected.append(
                RejectedRecoveryData(
                    session_id=envelope.session_id,
                    journal_dir=str(session_dir),
                    integrity=inspection.verdict,
                    reason=inspection.reason or 'journal failed integrity',
                )
            )
            continue
        if session_dir.name == current_session_id:
            continue
        launch_record = launch_records.get(
            envelope.launch_id or envelope.session_id
        )
        ending, reasons = classify_session_ending(
            envelope,
            entries=inspection.entries,
            pid_alive=pid_alive,
            current_boot_token=boot_now,
            launch_record=launch_record,
            current_native_schema_version=current_native_schema_version,
        )
        if ending == 'clean' or ending == 'still_running':
            continue
        if ending == 'incompatible':
            rejected.append(
                RejectedRecoveryData(
                    session_id=envelope.session_id,
                    journal_dir=str(session_dir),
                    integrity='incompatible',
                    reason='; '.join(reasons),
                )
            )
            continue
        state = _fold_entries(inspection.entries, inspection.resolved_payloads)
        documents = _session_documents(envelope, state)
        items: list[RecoverableItem] = []
        items += _draft_items(state, documents, scene_repository)
        items += _workspace_items(state)
        items += _annotation_items(state)
        items += _pending_import_item(state)
        items += _project_binding_item(envelope, state)
        items += _authority_resume_items(
            documents, state, orchestrator, campaign_repository
        )
        reconciliations: list[ReconciliationItem] = []
        reconciliations += _apply_transaction_reconciliations(
            state, documents, apply_repository, readback
        )
        reconciliations += _acquisition_reconciliations(
            state, sweep_repository
        )
        reconciliations += _write_intent_reconciliations(state)

        decisions: tuple[SessionRecoveryDecision, ...] = ()
        resolved = False
        if session_recovery_repository is not None:
            try:
                decisions = tuple(
                    session_recovery_repository.decisions_for_session(
                        envelope.session_id
                    )
                )
                resolved = any(
                    decision.scope_kind == 'session'
                    and decision.action in TERMINAL_DECISION_ACTIONS
                    for decision in decisions
                )
            except Exception:
                decisions = ()
        report = SessionRecoveryReport(
            session_id=envelope.session_id,
            launch_id=envelope.launch_id,
            app_version=envelope.app_version,
            build_id=envelope.build_id,
            journal_schema_version=envelope.schema_version,
            native_schema_version=envelope.native_schema_version,
            pid=envelope.pid,
            started_at_utc=envelope.started_at_utc,
            last_seen_at_utc=envelope.last_seen_at_utc,
            project_document_id=envelope.project_document_id,
            ending=ending,
            ending_reasons=reasons,
            integrity=inspection.verdict,
            integrity_reason=inspection.reason,
            entry_count=len(inspection.entries),
            head_entry_sha256=inspection.head_entry_sha256,
            envelope_sha256=(
                envelope.identity_sha256() if envelope else None
            ),
            journal_dir=str(session_dir),
            items=tuple(items),
            reconciliations=tuple(reconciliations),
            decisions=decisions,
            resolved=resolved,
        )
        reports.append(report)
        if record_detection and session_recovery_repository is not None:
            try:
                record_journal_detection(
                    session_recovery_repository,
                    report,
                    detected_by_session_id=current_session_id,
                )
            except Exception:
                pass
    return SessionRecoveryInspection(tuple(reports), tuple(rejected))


# ---------------------------------------------------------------------------
# Decisions + reconciliation recording (sealed authority)
# ---------------------------------------------------------------------------


def record_journal_detection(
    repository: Any,
    report: SessionRecoveryReport,
    *,
    detected_by_session_id: str | None = None,
) -> SessionRecoveryJournalRecord:
    """Seal the detection record once — idempotent on session_id."""

    existing = repository.journal_record_for_session(report.session_id)
    if existing is not None:
        return existing
    record = SessionRecoveryJournalRecord.create(
        session_id=report.session_id,
        document_id=report.project_document_id or '',
        detected_by_session_id=detected_by_session_id,
        build_id=report.build_id,
        app_version=report.app_version,
        journal_schema_version=report.journal_schema_version,
        native_schema_version=report.native_schema_version,
        pid=report.pid,
        started_at_utc=report.started_at_utc,
        last_seen_at_utc=report.last_seen_at_utc,
        ending=report.ending,
        integrity=report.integrity,
        entry_count=report.entry_count,
        head_entry_sha256=report.head_entry_sha256,
        envelope_sha256=report.envelope_sha256,
        detected_at_utc=utc_now_iso(),
    )
    repository.save_journal_record(record)
    return record


def decide_session(
    repository: Any,
    session_id: str,
    action: DecisionAction,
    *,
    scope_kind: str = 'session',
    scope_ref_id: str | None = None,
    actor: str = 'operator',
    document_id: str = '',
    restoring_session_id: str | None = None,
    reason: str | None = None,
) -> SessionRecoveryDecision:
    """Append one sealed decision; session-scope terminal actions settle it."""

    decision = SessionRecoveryDecision.create(
        session_id=session_id,
        document_id=document_id,
        scope_kind=scope_kind,
        scope_ref_id=scope_ref_id,
        action=action,
        actor=actor,
        decided_at_utc=utc_now_iso(),
        restoring_session_id=restoring_session_id,
        reason=reason,
    )
    repository.save_decision(decision)
    return decision


def apply_session_restore(
    repository: Any,
    report: SessionRecoveryReport,
    *,
    restoring_session_id: str | None,
    actor: str = 'operator',
) -> tuple[SessionRecoveryDecision, ...]:
    """Seal the restore acceptance for a report.

    Canonical state is untouched — the draft workspace banner, the
    sealed orchestrator log and the reconciliation queue do the real
    work; the decision records the operator's explicit choice and the
    consuming session for lineage.
    """

    decisions: list[SessionRecoveryDecision] = []
    for item in report.items:
        decisions.append(
            decide_session(
                repository,
                report.session_id,
                'restore_accepted',
                scope_kind=item.kind,
                scope_ref_id=item.ref_id,
                actor=actor,
                document_id=item.document_id,
                restoring_session_id=restoring_session_id,
            )
        )
    for item in report.reconciliations:
        decisions.append(
            decide_session(
                repository,
                report.session_id,
                'reconcile_deferred',
                scope_kind=item.kind,
                scope_ref_id=item.ref_id,
                actor=actor,
                document_id=item.document_id,
                restoring_session_id=restoring_session_id,
                reason='left for explicit reconciliation',
            )
        )
    decisions.append(
        decide_session(
            repository,
            report.session_id,
            'restore_completed',
            actor=actor,
            document_id=report.project_document_id or '',
            restoring_session_id=restoring_session_id,
        )
    )
    return tuple(decisions)


def discard_session(
    repository: Any,
    report: SessionRecoveryReport,
    *,
    actor: str = 'operator',
    reason: str | None = None,
    delete_journal: bool = True,
) -> SessionRecoveryDecision:
    decision = decide_session(
        repository,
        report.session_id,
        'discarded',
        actor=actor,
        document_id=report.project_document_id or '',
        reason=reason,
    )
    if delete_journal:
        shutil.rmtree(report.journal_dir, ignore_errors=True)
    return decision


def reject_session_evidence(
    repository: Any,
    rejected: RejectedRecoveryData,
    *,
    actor: str = 'operator',
    reason: str | None = None,
    delete_journal: bool = False,
) -> SessionRecoveryDecision:
    decision = decide_session(
        repository,
        rejected.session_id or Path(rejected.journal_dir).name,
        'evidence_rejected',
        actor=actor,
        reason=reason or rejected.reason,
    )
    if delete_journal:
        shutil.rmtree(rejected.journal_dir, ignore_errors=True)
    return decision


def record_reconciliation(
    repository: Any,
    session_id: str,
    operation_kind: ReconciliationKind,
    operation_ref_id: str,
    verdict: ReconciliationVerdict,
    *,
    document_id: str = '',
    evidence_detail: str | None = None,
    decided_by_session_id: str | None = None,
) -> SessionReconciliationRecord:
    record = SessionReconciliationRecord.create(
        session_id=session_id,
        document_id=document_id,
        operation_kind=operation_kind,
        operation_ref_id=operation_ref_id,
        verdict=verdict,
        evidence_detail=evidence_detail,
        decided_by_session_id=decided_by_session_id,
        recorded_at_utc=utc_now_iso(),
    )
    repository.save_reconciliation(record)
    decide_session(
        repository,
        session_id,
        'reconcile_resolved'
        if verdict != 'remains_unknown'
        else 'reconcile_deferred',
        scope_kind=operation_kind,
        scope_ref_id=operation_ref_id,
        actor='recovery',
        document_id=document_id,
        restoring_session_id=decided_by_session_id,
        reason=verdict,
    )
    return record


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def _session_dir_sort_key(path: Path) -> str:
    try:
        envelope = json.loads(
            (path / ENVELOPE_NAME).read_text(encoding='utf-8')
        )
        return str(
            envelope.get('last_seen_at_utc')
            or envelope.get('started_at_utc')
            or ''
        )
    except (OSError, ValueError):
        return ''


def enforce_retention(
    data_dir: Path | str,
    *,
    session_recovery_repository: Any = None,
    max_sessions: int = MAX_SESSION_JOURNALS,
    max_unresolved: int = MAX_UNRESOLVED_JOURNALS,
    actor: str = 'system',
) -> tuple[str, ...]:
    """Bound the journal directory.

    Resolved (clean / terminally decided) session dirs are collected
    oldest-first beyond ``max_sessions``. Unresolved evidence is only
    dropped beyond ``max_unresolved`` — and always with a sealed
    ``retention_discard`` decision so the loss is auditable.
    """

    root = session_journal_root(data_dir)
    if not root.is_dir():
        return ()
    dirs = sorted(
        (p for p in root.iterdir() if p.is_dir()),
        key=_session_dir_sort_key,
    )
    resolved_dirs: list[Path] = []
    unresolved_dirs: list[Path] = []
    for path in dirs:
        inspection = inspect_journal_dir(path)
        envelope = inspection.envelope
        decided = False
        if session_recovery_repository is not None and envelope is not None:
            try:
                decided = session_recovery_repository.session_is_resolved(
                    envelope.session_id
                )
            except Exception:
                decided = False
        if envelope is not None and envelope.closed_clean:
            decided = True
        (resolved_dirs if decided else unresolved_dirs).append(path)

    removed: list[str] = []
    while len(resolved_dirs) > max_sessions:
        victim = resolved_dirs.pop(0)
        shutil.rmtree(victim, ignore_errors=True)
        removed.append(str(victim))
    total = len(resolved_dirs) + len(unresolved_dirs)
    while total > max_sessions + max_unresolved:
        victim = unresolved_dirs.pop(0)
        session_id = victim.name
        if session_recovery_repository is not None:
            try:
                decide_session(
                    session_recovery_repository,
                    session_id,
                    'retention_discard',
                    actor=actor,
                    reason=(
                        'journal exceeded unresolved retention bound '
                        f'({max_unresolved})'
                    ),
                )
            except Exception:
                pass
        shutil.rmtree(victim, ignore_errors=True)
        removed.append(str(victim))
        total = len(resolved_dirs) + len(unresolved_dirs)
    return tuple(removed)


# ---------------------------------------------------------------------------
# Active-session registry (the in-process seam UI/engine code uses)
# ---------------------------------------------------------------------------


_ACTIVE_JOURNAL: SessionJournal | None = None


def activate_journal(journal: SessionJournal | None) -> None:
    global _ACTIVE_JOURNAL
    _ACTIVE_JOURNAL = journal


def deactivate_journal(journal: SessionJournal | None = None) -> None:
    global _ACTIVE_JOURNAL
    if journal is None or _ACTIVE_JOURNAL is journal:
        _ACTIVE_JOURNAL = None


def current_journal() -> SessionJournal | None:
    return _ACTIVE_JOURNAL


def declare_operation_started(
    operation_kind: str,
    operation_ref_id: str,
    *,
    document_id: str = '',
    detail: dict[str, Any] | None = None,
) -> None:
    """Journal an in-flight external operation — no-op without a session."""

    journal = _ACTIVE_JOURNAL
    if journal is None or journal.closed:
        return
    try:
        journal.record_operation_started(
            operation_kind, operation_ref_id,
            document_id=document_id, detail=detail,
        )
    except SessionJournalError:
        return


def declare_operation_finished(
    operation_ref_id: str, *, detail: dict[str, Any] | None = None
) -> None:
    journal = _ACTIVE_JOURNAL
    if journal is None or journal.closed:
        return
    try:
        journal.record_operation_finished(operation_ref_id, detail=detail)
    except SessionJournalError:
        return


def declare_acquisition_stage(run_id: str, stage: str) -> None:
    journal = _ACTIVE_JOURNAL
    if journal is None or journal.closed:
        return
    try:
        journal.record_acquisition_stage(run_id=run_id, stage=stage)
    except SessionJournalError:
        return


def declare_scene_draft(
    *,
    document_id: str,
    content_hash: str,
    source_revision_id: str | None = None,
) -> None:
    journal = _ACTIVE_JOURNAL
    if journal is None or journal.closed:
        return
    try:
        journal.record_scene_draft(
            document_id=document_id,
            content_hash=content_hash,
            source_revision_id=source_revision_id,
        )
    except SessionJournalError:
        return


def declare_scene_draft_cleared(document_id: str) -> None:
    journal = _ACTIVE_JOURNAL
    if journal is None or journal.closed:
        return
    try:
        journal.record_scene_draft_cleared(document_id)
    except SessionJournalError:
        return


def declare_pending_import(state: dict[str, Any] | None) -> None:
    journal = _ACTIVE_JOURNAL
    if journal is None or journal.closed:
        return
    try:
        if state is None:
            journal.record_pending_import_cleared()
        else:
            journal.record_pending_import(dict(state))
    except SessionJournalError:
        return


def declare_pending_annotation(
    *,
    annotation_id: str,
    document_id: str,
    summary: str,
    body: dict[str, Any] | None = None,
) -> None:
    journal = _ACTIVE_JOURNAL
    if journal is None or journal.closed:
        return
    try:
        journal.record_pending_annotation(
            annotation_id=annotation_id,
            document_id=document_id,
            summary=summary,
            body=dict(body or {}),
        )
    except SessionJournalError:
        return


def declare_pending_annotation_cleared(annotation_id: str) -> None:
    journal = _ACTIVE_JOURNAL
    if journal is None or journal.closed:
        return
    try:
        journal.record_pending_annotation_cleared(annotation_id)
    except SessionJournalError:
        return


def declare_write_intent(
    *,
    intent_id: str,
    path: str,
    mode: str = 'replace',
    expected_sha256: str | None = None,
    document_id: str = '',
) -> None:
    journal = _ACTIVE_JOURNAL
    if journal is None or journal.closed:
        return
    try:
        journal.record_write_intent(
            intent_id=intent_id,
            path=path,
            mode=mode,
            expected_sha256=expected_sha256,
            document_id=document_id,
        )
    except SessionJournalError:
        return


def declare_write_completed(
    intent_id: str, *, sha256_hex: str | None = None
) -> None:
    journal = _ACTIVE_JOURNAL
    if journal is None or journal.closed:
        return
    try:
        journal.record_write_completed(intent_id, sha256_hex=sha256_hex)
    except SessionJournalError:
        return


__all__ = [
    'DecisionAction',
    'ENTRY_KINDS',
    'FoldedJournalState',
    'HEARTBEAT_INTERVAL_MS',
    'ItemAvailability',
    'JournalEntry',
    'JournalInspection',
    'JournalIntegrity',
    'MAX_INLINE_PAYLOAD_BYTES',
    'MAX_JOURNAL_ENTRIES',
    'MAX_SESSION_JOURNALS',
    'MAX_UNRESOLVED_JOURNALS',
    'OPERATION_KINDS',
    'RecoverableItem',
    'RecoverableItemKind',
    'ReconciliationItem',
    'ReconciliationKind',
    'ReconciliationState',
    'ReconciliationVerdict',
    'RejectedRecoveryData',
    'SESSION_JOURNAL_DIRNAME',
    'SESSION_JOURNAL_SCHEMA_VERSION',
    'SessionEnvelope',
    'SessionJournal',
    'SessionJournalError',
    'SessionJournalPayloadError',
    'SessionReconciliationRecord',
    'SessionRecoveryDecision',
    'SessionRecoveryInspection',
    'SessionRecoveryJournalRecord',
    'SessionRecoveryReport',
    'SessionEnding',
    'TERMINAL_DECISION_ACTIONS',
    'activate_journal',
    'apply_session_restore',
    'boot_token',
    'classify_session_ending',
    'current_journal',
    'deactivate_journal',
    'decide_session',
    'declare_acquisition_stage',
    'declare_operation_finished',
    'declare_operation_started',
    'declare_pending_annotation',
    'declare_pending_annotation_cleared',
    'declare_pending_import',
    'declare_scene_draft',
    'declare_scene_draft_cleared',
    'declare_write_completed',
    'declare_write_intent',
    'discard_session',
    'enforce_retention',
    'inspect_journal_dir',
    'inspect_recoverable_sessions',
    'record_journal_detection',
    'record_reconciliation',
    'reject_session_evidence',
    'session_journal_root',
]
