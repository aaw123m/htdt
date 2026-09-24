from pathlib import Path

import pytest

from htdt.cad_evidence_reconciliation import (
    build_evidence_subject,
    build_observation,
    reconcile_subject,
)
from htdt.cad_evidence_reconciliation_repository import (
    CadEvidenceReconciliationRepository,
    ReconciliationConflictError,
)
from htdt.cad_repository import SceneRepository


def _subject(document_id: str = 'doc-1'):
    return build_evidence_subject(
        document_id=document_id,
        subject_kind='dimension',
        target_ref='entity:room-shell',
        attribute='room_width_m',
        description='Room width at seat row',
    )


def _observations(subject_id: str):
    return (
        build_observation(
            subject_id=subject_id,
            source='capture',
            source_ref='capture-proj-A',
            source_sha256='a' * 64,
            value=4.502,
            uncertainty=0.01,
            alignment_key='rev-1:htdt-x-right-y-rear-z-up-m',
            captured_at_utc='2026-09-20T00:00:00+00:00',
        ),
        build_observation(
            subject_id=subject_id,
            source='manual_dimension',
            source_ref='tape-measure-1',
            value=4.51,
            uncertainty=0.02,
            alignment_key='rev-1:htdt-x-right-y-rear-z-up-m',
            captured_at_utc='2026-09-21T00:00:00+00:00',
        ),
    )


def test_reconcile_consistent_sources() -> None:
    subject = _subject()
    observations = _observations(subject.subject_id)
    decision = reconcile_subject(
        subject,
        observations,
        tolerance=0.02,
        decided_at_utc='2026-09-24T00:00:00+00:00',
    )
    assert decision.outcome == 'consistent'
    assert len(decision.comparisons) == 1
    assert len(decision.decision_sha256) == 64


def test_reconcile_conflict_beyond_tolerance() -> None:
    subject = _subject()
    observations = (
        build_observation(
            subject_id=subject.subject_id,
            source='capture',
            value=4.50,
            uncertainty=0.01,
            alignment_key='frame',
        ),
        build_observation(
            subject_id=subject.subject_id,
            source='manual_dimension',
            value=4.80,
            uncertainty=0.01,
            alignment_key='frame',
        ),
    )
    decision = reconcile_subject(
        subject,
        observations,
        tolerance=0.02,
        decided_at_utc='2026-09-24T00:00:00+00:00',
    )
    assert decision.outcome == 'conflict'


def test_reconcile_not_comparable_when_alignment_unresolved() -> None:
    subject = _subject()
    observations = (
        build_observation(
            subject_id=subject.subject_id,
            source='floor_plan',
            value=4.50,
            uncertainty=0.01,
            alignment_key=None,
        ),
        build_observation(
            subject_id=subject.subject_id,
            source='manual_dimension',
            value=4.50,
            uncertainty=0.01,
            alignment_key='frame',
        ),
    )
    decision = reconcile_subject(
        subject,
        observations,
        decided_at_utc='2026-09-24T00:00:00+00:00',
    )
    assert decision.outcome == 'not_comparable'

    different_frames = (
        build_observation(
            subject_id=subject.subject_id,
            source='capture',
            value=4.50,
            uncertainty=0.01,
            alignment_key='frame-a',
        ),
        build_observation(
            subject_id=subject.subject_id,
            source='manual_dimension',
            value=4.50,
            uncertainty=0.01,
            alignment_key='frame-b',
        ),
    )
    decision = reconcile_subject(
        subject,
        different_frames,
        decided_at_utc='2026-09-24T00:00:00+00:00',
    )
    assert decision.outcome == 'not_comparable'


def test_reconcile_unknown_uncertainty_stays_unknown() -> None:
    subject = _subject()
    observations = (
        build_observation(
            subject_id=subject.subject_id,
            source='imported_geometry',
            value=4.50,
            uncertainty=None,
            alignment_key='frame',
        ),
        build_observation(
            subject_id=subject.subject_id,
            source='manual_dimension',
            value=4.60,
            uncertainty=0.01,
            alignment_key='frame',
        ),
    )
    decision = reconcile_subject(
        subject,
        observations,
        tolerance=0.02,
        decided_at_utc='2026-09-24T00:00:00+00:00',
    )
    assert decision.outcome == 'unknown'
    assert decision.comparisons[0].outcome == 'unknown'


def test_reconcile_insufficient() -> None:
    subject = _subject()
    decision = reconcile_subject(
        subject,
        (),
        decided_at_utc='2026-09-24T00:00:00+00:00',
    )
    assert decision.outcome == 'insufficient'


def test_reconciliation_repository_append_only(tmp_path: Path) -> None:
    repository = CadEvidenceReconciliationRepository(
        SceneRepository(tmp_path / 'scene.sqlite3')
    )
    subject = _subject()
    repository.save_subject(subject)
    assert repository.get_subject(subject.subject_id) == subject
    with pytest.raises(ReconciliationConflictError):
        repository.save_subject(subject)

    observations = _observations(subject.subject_id)
    for observation in observations:
        repository.save_observation(observation)
    listed = repository.list_observations(subject.subject_id)
    assert sorted(
        item.observation_id for item in listed
    ) == sorted(item.observation_id for item in observations)

    decision = reconcile_subject(
        subject,
        observations,
        tolerance=0.02,
        decided_at_utc='2026-09-24T00:00:00+00:00',
    )
    repository.save_decision(decision)
    assert repository.latest_decision(subject.subject_id) == decision
    with pytest.raises(ReconciliationConflictError):
        repository.save_decision(decision)

    with pytest.raises(ValueError):
        repository.save_observation(
            build_observation(
                subject_id='ghost', source='other', value=1.0
            )
        )
