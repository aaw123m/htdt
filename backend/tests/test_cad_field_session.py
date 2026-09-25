"""#728: bounded field session + scoped immutable field evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_field_session import (
    FieldSessionRepository,
    evaluate_field_session_freshness,
    issue_field_session,
    record_field_evidence,
    transition_field_session,
)


def _session(**kwargs):
    return issue_field_session(
        document_id='doc-1',
        build_identity='htdt 1.0.0',
        revision_id='rev-1',
        system_variant_id='var-1',
        commissioning_plan_id='plan-1',
        task_kind='commissioning_checklist',
        scope_refs=('check-1', 'check-2', 'cable-a'),
        issued_at_utc='2026-09-24T00:00:00+00:00',
        **kwargs,
    )


def test_session_binds_exact_authority() -> None:
    session = _session()
    assert session.document_id == 'doc-1'
    assert session.revision_id == 'rev-1'
    assert session.state == 'active'
    ref = session.authority_ref()
    assert ref.semantic_hash_sha256 == session.semantic_sha256


def test_freshness_stale_when_design_moved() -> None:
    session = _session()
    assert (
        evaluate_field_session_freshness(
            session, current_revision_id='rev-2'
        )
        == 'stale'
    )
    assert (
        evaluate_field_session_freshness(
            session, current_revision_id='rev-1'
        )
        == 'current'
    )


def test_freshness_expired_and_terminal_states() -> None:
    session = _session(expires_at_utc='2026-09-25T00:00:00+00:00')
    assert (
        evaluate_field_session_freshness(
            session,
            current_revision_id='rev-1',
            now_utc='2026-09-26T00:00:00+00:00',
        )
        == 'expired'
    )
    returned = transition_field_session(
        session, 'returned', at_utc='2026-09-24T12:00:00+00:00'
    )
    assert (
        evaluate_field_session_freshness(
            returned, current_revision_id='rev-2'
        )
        == 'returned'
    )
    revoked = transition_field_session(session, 'revoked')
    assert (
        evaluate_field_session_freshness(
            revoked, current_revision_id='rev-1'
        )
        == 'revoked'
    )


def test_evidence_scoped_and_pending_review() -> None:
    session = _session()
    record = record_field_evidence(
        session,
        kind='checklist_response',
        subject_ref='check-1',
        value_text='yes',
        captured_at_utc='2026-09-24T01:00:00+00:00',
    )
    assert record.review_state == 'pending_review'
    assert record.session_id == session.session_id


def test_evidence_outside_scope_refused() -> None:
    session = _session()
    with pytest.raises(ValueError, match='outside field session scope'):
        record_field_evidence(
            session,
            kind='checklist_response',
            subject_ref='check-99',
            value_text='yes',
        )


def test_evidence_kind_contract() -> None:
    session = _session()
    with pytest.raises(ValueError, match='value_numeric'):
        record_field_evidence(session, kind='numeric_measurement')
    with pytest.raises(ValueError, match='source_sha256'):
        record_field_evidence(session, kind='photo')
    with pytest.raises(ValueError, match='value_text'):
        record_field_evidence(session, kind='serial_capture')


def test_repository_round_trip(tmp_path: Path) -> None:
    repo = FieldSessionRepository(tmp_path / 'cad.sqlite3')
    session = _session()
    repo.save_session(session)
    assert repo.get_session(session.session_id) == session

    returned = transition_field_session(
        session, 'returned', at_utc='2026-09-24T12:00:00+00:00'
    )
    repo.save_session(returned)
    assert repo.get_session(session.session_id).state == 'returned'

    record = record_field_evidence(
        session,
        kind='note',
        subject_ref='check-2',
        value_text='cabinet seam tight',
        captured_at_utc='2026-09-24T02:00:00+00:00',
    )
    repo.save_evidence(record)
    repo.save_evidence(record)
    assert repo.get_evidence(record.record_id) == record
    assert [
        r.record_id
        for r in repo.list_evidence_for_session(session.session_id)
    ] == [record.record_id]
    assert [
        s.session_id for s in repo.list_sessions_for_document('doc-1')
    ] == [session.session_id]
