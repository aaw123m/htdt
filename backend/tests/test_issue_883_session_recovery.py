"""#883: crash-safe autosave + session recovery.

Journal + envelope are volatile trace; sealed authorities stay canonical.
These tests pin:

- the sha-chained journal round-trips through inspection, verifies sidecar
  payloads by hash, rejects secret-bearing payloads, tolerates a torn tail
  and fails closed on mid-file corruption;
- ending classification is evidence-ordered: clean marker, schema
  incompatibility, live pid, changed boot token, recorded failure, then
  forced termination;
- reconciliation never infers success: declared-but-unsealed apply ops
  and unclosed sealed transactions read DEVICE_STATE_UNKNOWN +
  RECONCILIATION_REQUIRED, non-terminal acquisition reads
  ACQUISITION_INCOMPLETE, unmatched file intents read
  WRITE_COMPLETION_UNKNOWN;
- decisions are sealed and append-only with restoring-session lineage;
- real crash injection: a killed child process leaves an intact journal
  the next "launch" recovers from.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.cad_document import WorkingDocument
from htdt.cad_scene import Position3
from htdt import session_recovery as sr
from htdt.session_recovery import (
    MAX_INLINE_PAYLOAD_BYTES,
    MAX_JOURNAL_ENTRIES,
    SessionJournal,
    SessionJournalPayloadError,
    activate_journal,
    apply_session_restore,
    boot_token,
    classify_session_ending,
    current_journal,
    deactivate_journal,
    decide_session,
    declare_acquisition_stage,
    declare_operation_finished,
    declare_operation_started,
    declare_pending_import,
    declare_scene_draft,
    declare_write_completed,
    declare_write_intent,
    discard_session,
    enforce_retention,
    inspect_journal_dir,
    inspect_recoverable_sessions,
    record_reconciliation,
    reject_session_evidence,
    session_journal_root,
)
from htdt.session_recovery_repository import (
    SessionRecoveryConflictError,
    SessionRecoveryRepository,
)


def _journal(
    data_dir: Path,
    session_id: str,
    *,
    pid: int = 999_999_999,
    boot: str | None = 'boot-1',
    native_schema_version: int = 100,
    project_document_id: str | None = 'doc-1',
) -> SessionJournal:
    return SessionJournal.open(
        data_dir,
        session_id=session_id,
        launch_id=f'launch-{session_id}',
        app_version='0.2.0.dev0',
        build_id='test-build',
        native_schema_version=native_schema_version,
        pid=pid,
        boot=boot,
        started_at_utc='2026-10-07T00:00:00Z',
        project_document_id=project_document_id,
    )


def _inspect(
    data_dir: Path,
    *,
    repository=None,
    pid_alive=None,
    current_boot_token='boot-1',
    current_native_schema_version: int | None = 100,
    **kwargs,
):
    repository = kwargs.pop('session_recovery_repository', repository)
    return inspect_recoverable_sessions(
        data_dir,
        session_recovery_repository=repository,
        pid_alive=pid_alive or (lambda _pid: False),
        current_boot_token=current_boot_token,
        current_native_schema_version=current_native_schema_version,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# journal + integrity
# ---------------------------------------------------------------------------


def test_journal_roundtrip_and_chain(tmp_path: Path) -> None:
    journal = _journal(tmp_path, 'sess-1')
    journal.record_workspace_state(destination='scene')
    journal.record_scene_draft(
        document_id='doc-1', content_hash='abc', source_revision_id='rev-1'
    )
    inspection = inspect_journal_dir(journal.directory)
    assert inspection.verdict == 'intact'
    assert len(inspection.entries) == 3
    seqs = [e.seq for e in inspection.entries]
    assert seqs == [1, 2, 3]
    for entry, nxt in zip(
        inspection.entries, inspection.entries[1:]
    ):
        assert nxt.prev_sha256 == entry.entry_sha256
    state = sr._fold_entries(
        inspection.entries, inspection.resolved_payloads
    )
    assert state.scene_drafts['doc-1']['content_hash'] == 'abc'
    assert state.workspace_state['destination'] == 'scene'


def test_sidecar_payload_verified_and_resolved(tmp_path: Path) -> None:
    journal = _journal(tmp_path, 'sess-1')
    big = {'blob': 'x' * (MAX_INLINE_PAYLOAD_BYTES + 4096)}
    journal.append('pending_import', big)
    inspection = inspect_journal_dir(journal.directory)
    assert inspection.verdict == 'intact'
    payload = inspection.resolved_payloads[-1]
    assert payload == big
    # the wire form carries only the pointer
    wire = inspection.entries[-1].payload
    assert '_sidecar_sha256' in wire
    sidecar = journal.directory / 'payloads' / f"{wire['_sidecar_sha256']}.json"
    assert sidecar.is_file()
    # tamper the sidecar → the entry can no longer verify
    sidecar.write_text('{"tampered": true}', encoding='utf-8')
    inspection2 = inspect_journal_dir(journal.directory)
    assert inspection2.verdict == 'tail_torn'


def test_secret_keys_are_rejected(tmp_path: Path) -> None:
    journal = _journal(tmp_path, 'sess-1')
    for bad in (
        {'api_key': 'x'},
        {'nested': {'password': 'x'}},
        {'items': [{'access_token': 'x'}]},
    ):
        with pytest.raises(SessionJournalPayloadError):
            journal.append('pending_import', bad)


def test_torn_tail_tolerated_midfile_corruption_fails_closed(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path, 'sess-1')
    journal.record_workspace_state(destination='a')
    journal.record_workspace_state(destination='b')
    path = journal.directory / 'journal.jsonl'
    raw = path.read_bytes()
    # torn tail: a truncated final line
    path.write_bytes(raw + b'{"seq": 4, "sessio')
    inspection = inspect_journal_dir(journal.directory)
    assert inspection.verdict == 'tail_torn'
    assert len(inspection.entries) == 3
    # corruption inside the intact region fails closed
    path2 = journal.directory / 'journal.jsonl'
    lines = path2.read_bytes().split(b'\n')
    lines[1] = b'{"seq": 2, "kind": "workspace_snapshot", "payload": {"destination": "forged"}, "entry_sha256": "deadbeef"}'
    path2.write_bytes(b'\n'.join(lines))
    inspection2 = inspect_journal_dir(journal.directory)
    assert inspection2.verdict == 'corrupt'


def test_compaction_bounds_the_journal(tmp_path: Path) -> None:
    journal = _journal(tmp_path, 'sess-1')
    for i in range(MAX_JOURNAL_ENTRIES + 5):
        journal.append('heartbeat', {'i': i})
    inspection = inspect_journal_dir(journal.directory)
    assert inspection.verdict == 'intact'
    kinds = {e.kind for e in inspection.entries}
    assert 'snapshot_compaction' in kinds
    assert len(inspection.entries) <= MAX_JOURNAL_ENTRIES + 2
    state = sr._fold_entries(
        inspection.entries, inspection.resolved_payloads
    )
    assert state.heartbeat_count >= MAX_JOURNAL_ENTRIES


def test_unreadable_and_incompatible_rejected(tmp_path: Path) -> None:
    # unreadable envelope
    bad = session_journal_root(tmp_path) / 'sess-bad'
    bad.mkdir(parents=True)
    (bad / 'envelope.json').write_text('{not json', encoding='utf-8')
    (bad / 'journal.jsonl').write_bytes(b'')
    inspection = _inspect(tmp_path)
    assert not inspection.reports
    assert inspection.rejected[0].integrity == 'unreadable'
    # journal schema the build cannot read
    journal = _journal(tmp_path, 'sess-old')
    envelope = journal.envelope.model_copy(update={'schema_version': 0})
    (journal.directory / 'envelope.json').write_text(
        envelope.model_dump_json(), encoding='utf-8'
    )
    inspection2 = _inspect(tmp_path)
    assert any(
        item.integrity in {'incompatible', 'unreadable'}
        for item in inspection2.rejected
    )


# ---------------------------------------------------------------------------
# ending classification
# ---------------------------------------------------------------------------


def test_clean_close_is_not_recoverable(tmp_path: Path) -> None:
    journal = _journal(tmp_path, 'sess-1')
    journal.close_clean()
    inspection = _inspect(tmp_path)
    assert not inspection.reports


def test_forced_termination_classification(tmp_path: Path) -> None:
    journal = _journal(tmp_path, 'sess-1')
    journal.record_workspace_state(destination='scene')
    inspection = _inspect(tmp_path)
    assert inspection.reports[0].ending == 'forced_termination'


def test_application_crash_via_failure_noted(tmp_path: Path) -> None:
    journal = _journal(tmp_path, 'sess-1')
    journal.note_failure('renderer_initialization')
    inspection = _inspect(tmp_path)
    report = inspection.reports[0]
    assert report.ending == 'application_crash'


def test_os_restart_classification(tmp_path: Path) -> None:
    _journal(tmp_path, 'sess-1', boot='boot-1000')
    inspection = _inspect(tmp_path, current_boot_token='boot-2000')
    assert inspection.reports[0].ending == 'os_restart_or_power_loss'


def test_still_running_is_not_recoverable(tmp_path: Path) -> None:
    _journal(tmp_path, 'sess-1')
    inspection = _inspect(tmp_path, pid_alive=lambda _pid: True)
    assert not inspection.reports
    assert not inspection.rejected


def test_newer_native_schema_is_incompatible(tmp_path: Path) -> None:
    _journal(tmp_path, 'sess-1', native_schema_version=101)
    inspection = _inspect(tmp_path, current_native_schema_version=100)
    assert not inspection.reports
    assert inspection.rejected[0].integrity == 'incompatible'


def test_boot_token_helper() -> None:
    token = boot_token()
    assert token is None or token.startswith('boot-')


# ---------------------------------------------------------------------------
# recoverable items + reconciliation
# ---------------------------------------------------------------------------


def _draft_document(tmp_path: Path) -> tuple[SceneRepository, str, str]:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None).revision
    working = WorkingDocument(
        first.document,
        source_revision_id=first.revision_id,
        saved_content_hash=first.content_hash,
    )
    working.move_entity(
        'speaker-fl', Position3(x_m=1.9, y_m=0.75, z_m=1.05)
    )
    recovery = repository.save_recovery(
        working.committed_document,
        source_revision_id=working.source_revision_id,
    )
    return repository, first.document_id, recovery.content_hash


def test_scene_draft_verified_then_blocked(tmp_path: Path) -> None:
    repository, document_id, content_hash = _draft_document(tmp_path)
    journal = _journal(
        tmp_path, 'sess-1', project_document_id=document_id
    )
    # the domain hook already fired via save_recovery → record a matching
    # journaled draft explicitly (the hook only fires with an active journal)
    journal.record_scene_draft(
        document_id=document_id, content_hash=content_hash
    )
    inspection = _inspect(
        tmp_path, scene_repository=repository
    )
    items = {i.kind: i for i in inspection.reports[0].items}
    draft = items['scene_draft']
    assert draft.availability == 'available'
    # drift: persisted draft no longer matches the journaled hash
    working2 = WorkingDocument(
        repository.recovery(document_id).document,
        source_revision_id=None,
        saved_content_hash=content_hash,
    )
    repository.clear_recovery(document_id)
    inspection2 = _inspect(tmp_path, scene_repository=repository)
    items2 = {i.kind: i for i in inspection2.reports[0].items}
    assert items2['scene_draft'].availability == 'blocked'


def test_pending_import_and_workspace_items(tmp_path: Path) -> None:
    journal = _journal(tmp_path, 'sess-1')
    journal.record_workspace_state(
        destination='measurement',
        mounts=('scene', 'measurement'),
        dirty_documents=('doc-1',),
    )
    journal.record_pending_import(
        {
            'document_id': 'doc-1',
            'source_kind': 'rew_text',
            'source_label': 'front-left.txt',
            'frequency_hz': [20.0, 21.0],
            'level_db': [80.0, 80.5],
        }
    )
    journal.record_pending_annotation(
        annotation_id='ann-1',
        document_id='doc-1',
        summary='壁の注記',
        body={'text': 'x'},
    )
    inspection = _inspect(tmp_path)
    report = inspection.reports[0]
    kinds = {i.kind for i in report.items}
    assert {'workspace_state', 'pending_import', 'pending_annotation'} <= kinds
    pending = next(i for i in report.items if i.kind == 'pending_import')
    assert pending.detail['source_label'] == 'front-left.txt'
    binding = next(
        i for i in report.items if i.kind == 'project_binding'
    )
    assert '未保存' in binding.summary


class _StoreList:
    def __init__(self, items=()) -> None:
        self._items = list(items)

    def list(self, document_id=None):
        return list(self._items)


class _ApplyRepo:
    def __init__(self, plans=(), transactions=(), writes=(), verifications=()):
        self.plans = _StoreList(plans)
        self.transactions = _StoreList(transactions)
        self.writes = _StoreList(writes)
        self.verifications = _StoreList(verifications)


def _txn(
    transaction_id='txn-1',
    document_id='doc-1',
    state_verdict='partially_applied',
    closed_at_utc=None,
    plan_sha='plan-sha',
):
    from types import SimpleNamespace

    return SimpleNamespace(
        transaction_id=transaction_id,
        document_id=document_id,
        state_verdict=state_verdict,
        closed_at_utc=closed_at_utc,
        plan_ref=SimpleNamespace(ref_sha256=plan_sha),
    )


def test_declared_apply_op_reconciles(tmp_path: Path) -> None:
    journal = _journal(tmp_path, 'sess-1')
    journal.record_operation_started(
        'device_apply_transaction', 'txn-1', document_id='doc-1'
    )
    inspection = _inspect(
        tmp_path,
        apply_repository=_ApplyRepo(),
    )
    recons = inspection.reports[0].reconciliations
    assert any(
        item.state == 'DEVICE_STATE_UNKNOWN' and item.ref_id == 'txn-1'
        for item in recons
    )
    target = next(i for i in recons if i.ref_id == 'txn-1')
    assert target.detail['state'] == 'RECONCILIATION_REQUIRED'


def test_unclosed_sealed_transaction_reconciles(tmp_path: Path) -> None:
    _journal(tmp_path, 'sess-1')
    inspection = _inspect(
        tmp_path,
        apply_repository=_ApplyRepo(
            transactions=(_txn(state_verdict='partially_applied'),)
        ),
    )
    recons = inspection.reports[0].reconciliations
    assert any(
        item.ref_id == 'txn-1' and item.state == 'DEVICE_STATE_UNKNOWN'
        for item in recons
    )


def test_terminal_sealed_verdict_resolves(tmp_path: Path) -> None:
    journal = _journal(tmp_path, 'sess-1')
    journal.record_operation_started(
        'device_apply_transaction', 'txn-1', document_id='doc-1'
    )
    inspection = _inspect(
        tmp_path,
        apply_repository=_ApplyRepo(
            transactions=(
                _txn(
                    state_verdict='fully_applied',
                    closed_at_utc='2026-10-07T01:00:00Z',
                ),
            )
        ),
    )
    item = next(
        i for i in inspection.reports[0].reconciliations
        if i.ref_id == 'txn-1'
    )
    assert 'fully_applied' in (item.resolution or '')


def test_readback_resolves_uncertain_apply(tmp_path: Path) -> None:
    journal = _journal(tmp_path, 'sess-1')
    journal.record_operation_started(
        'device_apply_transaction', 'txn-1', document_id='doc-1'
    )
    inspection = _inspect(
        tmp_path,
        apply_repository=_ApplyRepo(),
        readback=lambda ref, ctx: 'confirmed',
    )
    item = next(
        i for i in inspection.reports[0].reconciliations
        if i.ref_id == 'txn-1'
    )
    assert item.resolution == 'machine read-back confirmed the device state'


class _SweepRepo:
    def __init__(self, run=None) -> None:
        self._run = run

    def get_run_by_run_id(self, run_id):
        return self._run


def test_acquisition_incomplete_until_terminal_sealed(tmp_path: Path) -> None:
    journal = _journal(tmp_path, 'sess-1')
    journal.record_acquisition_stage(
        run_id='run-1', stage='playing_recording'
    )
    inspection = _inspect(tmp_path, sweep_repository=_SweepRepo())
    item = inspection.reports[0].reconciliations[0]
    assert item.state == 'ACQUISITION_INCOMPLETE'
    assert 'never counts as a completed' in item.detail['state']
    # a terminally-sealed run clears the reconciliation
    from types import SimpleNamespace

    sealed = SimpleNamespace(
        stage='completed', outcome='measured', document_id='doc-1'
    )
    inspection2 = _inspect(
        tmp_path, sweep_repository=_SweepRepo(run=sealed)
    )
    assert not [
        r for r in inspection2.reports[0].reconciliations
        if r.kind == 'sweep_acquisition'
    ]


def test_write_completion_unknown_until_hash_matches(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path, 'sess-1')
    target = tmp_path / 'out.txt'
    target.write_text('actual content', encoding='utf-8')
    journal.record_write_intent(
        intent_id='w-1',
        path=str(target),
        expected_sha256='0' * 64,
    )
    inspection = _inspect(tmp_path)
    item = inspection.reports[0].reconciliations[0]
    assert item.state == 'WRITE_COMPLETION_UNKNOWN'
    assert item.resolution is None
    # matching hash resolves it
    import hashlib

    sha = hashlib.sha256(b'actual content').hexdigest()
    journal2_dir = tmp_path / 'j2'
    journal2 = SessionJournal.open(
        journal2_dir,
        session_id='sess-2',
        pid=999_999_999,
        boot='boot-1',
        started_at_utc='2026-10-07T01:00:00Z',
    )
    journal2.record_write_intent(
        intent_id='w-2', path=str(target), expected_sha256=sha
    )
    inspection2 = _inspect(journal2_dir)
    resolved = inspection2.reports[0].reconciliations[0]
    assert resolved.resolution == 'file hash matches the recorded intent'


# ---------------------------------------------------------------------------
# sealed decisions + retention
# ---------------------------------------------------------------------------


def test_decisions_are_sealed_and_terminal(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    srr = SessionRecoveryRepository(repository)
    journal = _journal(tmp_path, 'sess-1')
    journal.record_workspace_state(destination='scene')
    inspection = _inspect(tmp_path, session_recovery_repository=srr)
    report = inspection.reports[0]
    # detection was sealed once
    record = srr.journal_record_for_session('sess-1')
    assert record is not None
    assert record.ending == 'forced_termination'
    assert not srr.session_is_resolved('sess-1')
    decisions = apply_session_restore(
        srr, report, restoring_session_id='sess-2'
    )
    assert decisions[-1].action == 'restore_completed'
    assert decisions[-1].restoring_session_id == 'sess-2'
    assert srr.session_is_resolved('sess-1')
    # the same session restores only once in the report — still resolved
    assert srr.get_decision(decisions[-1].decision_id) is not None


def test_discard_seals_and_removes(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    srr = SessionRecoveryRepository(repository)
    journal = _journal(tmp_path, 'sess-1')
    inspection = _inspect(tmp_path, session_recovery_repository=srr)
    report = inspection.reports[0]
    discard_session(srr, report)
    assert srr.session_is_resolved('sess-1')
    assert not journal.directory.exists()


def test_append_only_conflict_fails(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    srr = SessionRecoveryRepository(repository)
    decision = decide_session(
        srr, 'sess-1', 'deferred',
        document_id='doc-1', actor='a',
    )
    # re-saving the identical sealed record is idempotent
    srr.save_decision(decision)
    # same id, different sealed content — the append-only authority
    # refuses the rewrite
    from htdt.canonical_json import canonical_sha256

    payload = decision.model_dump(mode='python')
    payload['actor'] = 'mallory'
    payload['decision_sha256'] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key not in ('decision_id', 'decision_sha256')
        }
    )
    forged = type(decision)(**payload)
    with pytest.raises(SessionRecoveryConflictError):
        srr.save_decision(forged)
    # an unsealed payload is rejected before the store even opens
    unsealed = decision.model_copy(update={'actor': 'eve'})
    with pytest.raises(Exception):
        srr.save_decision(unsealed)
    assert srr.journal_record_for_session('none') is None


def test_reconciliation_records_sealed(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    srr = SessionRecoveryRepository(repository)
    record = record_reconciliation(
        srr,
        session_id='sess-1',
        operation_kind='device_apply_transaction',
        operation_ref_id='txn-1',
        verdict='resolved_confirmed',
        document_id='doc-1',
    )
    assert srr.get_reconciliation(record.reconciliation_id) is not None
    listed = srr.reconciliations_for_session('sess-1')
    assert len(listed) == 1
    assert listed[0].verdict == 'resolved_confirmed'


def test_retention_bounds_resolved_and_unresolved(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    srr = SessionRecoveryRepository(repository)
    root = session_journal_root(tmp_path)
    for i in range(6):
        journal = _journal(tmp_path, f'clean-{i}')
        journal.close_clean()
    unresolved = _journal(tmp_path, 'sess-live')
    unresolved.record_workspace_state(destination='x')
    enforce_retention(
        tmp_path,
        session_recovery_repository=srr,
        max_sessions=3,
        max_unresolved=4,
    )
    remaining = [p.name for p in root.iterdir() if p.is_dir()]
    assert len(remaining) == 4
    assert 'sess-live' in remaining
    # unresolved beyond the bound gets a sealed retention_discard
    for i in range(6):
        _journal(tmp_path, f'unresolved-{i}')
    removed = enforce_retention(
        tmp_path,
        session_recovery_repository=srr,
        max_sessions=3,
        max_unresolved=4,
    )
    assert removed
    assert any(
        d.action == 'retention_discard' for d in srr.list_decisions()
    )


# ---------------------------------------------------------------------------
# registry + domain hooks
# ---------------------------------------------------------------------------


def test_declare_helpers_noop_without_journal(tmp_path: Path) -> None:
    deactivate_journal()
    declare_operation_started('device_apply_transaction', 'txn-1')
    declare_acquisition_stage('run-1', 'armed')
    declare_pending_import({'x': 1})
    declare_scene_draft(document_id='doc-1', content_hash='h')
    declare_write_intent(intent_id='w-1', path='x')
    # nothing to inspect — root never created
    assert not session_journal_root(tmp_path).exists()


def test_declare_helpers_land_when_active(tmp_path: Path) -> None:
    journal = _journal(tmp_path, 'sess-1')
    activate_journal(journal)
    try:
        declare_operation_started('sweep_acquisition', 'run-9')
        declare_acquisition_stage('run-9', 'armed')
        declare_pending_import({'source_label': 'm.txt'})
        declare_scene_draft(document_id='doc-1', content_hash='h1')
        declare_write_intent(
            intent_id='w-1', path=str(tmp_path / 'f'), expected_sha256=None
        )
        declare_write_completed('w-1')
        declare_operation_finished('run-9')
    finally:
        deactivate_journal(journal)
    inspection = inspect_journal_dir(journal.directory)
    kinds = [e.kind for e in inspection.entries]
    assert 'operation_started' in kinds
    assert 'acquisition_stage' in kinds
    assert 'pending_import' in kinds
    assert 'scene_draft' in kinds
    assert 'write_intent' in kinds and 'write_completed' in kinds
    state = sr._fold_entries(
        inspection.entries, inspection.resolved_payloads
    )
    assert 'run-9' not in state.open_operations  # finished
    assert not state.open_write_intents
    assert state.acquisition_stages['run-9'] == 'armed'


def test_save_recovery_mirrors_into_active_journal(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None).revision
    journal = _journal(tmp_path, 'sess-1')
    activate_journal(journal)
    try:
        working = WorkingDocument(
            first.document,
            source_revision_id=first.revision_id,
            saved_content_hash=first.content_hash,
        )
        working.move_entity(
            'speaker-fl', Position3(x_m=1.9, y_m=0.75, z_m=1.05)
        )
        repository.save_recovery(
            working.committed_document,
            source_revision_id=working.source_revision_id,
        )
    finally:
        deactivate_journal(journal)
    inspection = inspect_journal_dir(journal.directory)
    kinds = [e.kind for e in inspection.entries]
    assert 'scene_draft' in kinds
    draft = next(e for e in inspection.entries if e.kind == 'scene_draft')
    assert draft.payload['content_hash']


# ---------------------------------------------------------------------------
# real crash injection (owned Windows box: true process kill)
# ---------------------------------------------------------------------------


_CHILD_SCRIPT = textwrap.dedent(
    '''
    import sys, time
    sys.path.insert(0, r'{src}')
    from htdt.session_recovery import SessionJournal
    journal = SessionJournal.open(
        r'{data}',
        session_id='killed-1',
        launch_id='L-kill',
        app_version='0.2.0.dev0',
        build_id='t',
        native_schema_version=100,
        boot={boot!r},
        started_at_utc='2026-10-07T00:00:00Z',
        project_document_id='doc-killed',
    )
    journal.record_workspace_state(destination='scene')
    journal.record_pending_import({{'source_label': 'killed.txt'}})
    print('JOURNAL_READY', flush=True)
    time.sleep(60)
    '''
)


@pytest.mark.skipif(os.name != 'nt', reason='owned-Windows kill path')
def test_real_process_kill_leaves_recoverable_journal(
    tmp_path: Path,
) -> None:
    """taskkill a child mid-session — the next launch classifies the
    ending as forced termination and surfaces the journaled state."""
    src = Path(__file__).resolve().parents[1] / 'src'
    script = _CHILD_SCRIPT.format(
        src=str(src), data=str(tmp_path), boot=boot_token()
    )
    proc = subprocess.Popen(
        [sys.executable, '-c', script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding='utf-8',
        errors='replace',
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        assert 'JOURNAL_READY' in line
        proc.kill()
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    inspection = _inspect(tmp_path, current_boot_token=boot_token())
    assert len(inspection.reports) == 1
    report = inspection.reports[0]
    assert report.ending in (
        'forced_termination',
        'application_crash',
    )
    kinds = {i.kind for i in report.items}
    assert 'pending_import' in kinds
    assert 'workspace_state' in kinds
    # the report names the project + session the crash belonged to
    assert report.project_document_id == 'doc-killed'
    assert report.session_id == 'killed-1'


@pytest.mark.skipif(os.name != 'nt', reason='owned-Windows exit path')
def test_real_process_dirty_exit_is_recoverable(tmp_path: Path) -> None:
    """os._exit mid-session (no atexit, no finally) — same recovery lane."""
    src = Path(__file__).resolve().parents[1] / 'src'
    crash = _CHILD_SCRIPT.format(
        src=str(src), data=str(tmp_path), boot=boot_token()
    ).replace("time.sleep(60)", "import os; os._exit(3)")
    proc = subprocess.run(
        [sys.executable, '-c', crash],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 3
    inspection = _inspect(tmp_path, current_boot_token=boot_token())
    assert len(inspection.reports) == 1
    assert inspection.reports[0].ending in (
        'forced_termination',
        'application_crash',
    )


# ---------------------------------------------------------------------------
# rejected evidence lifecycle
# ---------------------------------------------------------------------------


def test_rejected_evidence_sealed_and_deleted(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    srr = SessionRecoveryRepository(repository)
    bad = session_journal_root(tmp_path) / 'sess-corrupt'
    bad.mkdir(parents=True)
    (bad / 'envelope.json').write_text('{bad', encoding='utf-8')
    (bad / 'journal.jsonl').write_bytes(b'\x00\x01')
    inspection = _inspect(tmp_path)
    rejected = inspection.rejected[0]
    reject_session_evidence(
        srr, rejected, actor='operator', delete_journal=True
    )
    assert not bad.exists()
    decisions = srr.decisions_for_session('sess-corrupt')
    assert decisions[0].action == 'evidence_rejected'
