from pathlib import Path

import pytest

from htdt.cad_authority_resolver import AuthorityRef, ResolvedAuthority
from htdt.cad_evidence_reconciliation import (
    AlignmentRef,
    build_evidence_subject,
    build_observation,
    reconcile_subject,
)
from htdt.cad_evidence_reconciliation_repository import (
    CadEvidenceReconciliationRepository,
    ReconciliationConflictError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene


DOC = F1_DOCUMENT_ID


def _scene_target(revision) -> AuthorityRef:
    return AuthorityRef(
        kind='scene_revision',
        ref_id=revision.revision_id,
        ref_sha256=revision.content_hash,
    )


def _subject(revision, document_id: str = DOC):
    return build_evidence_subject(
        document_id=document_id,
        subject_kind='dimension',
        target=_scene_target(revision),
        attribute='room_width_m',
        description='Room width at seat row',
    )


def _capture_frame(sha: str = 'f' * 64) -> AlignmentRef:
    return AlignmentRef(
        frame_kind='capture_frame',
        frame_id='capture-proj-A',
        frame_sha256=sha,
    )


def _observations(subject_id: str):
    frame = _capture_frame()
    return (
        build_observation(
            subject_id=subject_id,
            source='capture',
            source_ref='capture-proj-A',
            source_sha256='a' * 64,
            value=4.502,
            unit='m',
            uncertainty=0.01,
            alignment=frame,
            captured_at_utc='2026-09-20T00:00:00+00:00',
        ),
        build_observation(
            subject_id=subject_id,
            source='manual_dimension',
            source_ref='tape-measure-1',
            value=4.51,
            unit='m',
            uncertainty=0.02,
            alignment=frame,
            captured_at_utc='2026-09-21T00:00:00+00:00',
        ),
    )


def _seed(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'scene.sqlite3')
    saved = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    )

    def _scene_measured(ref_id: str):
        revision = scene_repository.get(ref_id)
        if revision is None:
            return None
        return ResolvedAuthority(
            kind='measurement',
            ref_id=ref_id,
            document_id=revision.document_id,
            semantic_sha256=revision.content_hash,
        )

    def _external(kind: str, sha: str):
        def resolve(ref_id: str):
            return ResolvedAuthority(
                kind=kind,
                ref_id=ref_id,
                document_id=DOC,
                semantic_sha256=sha,
            )

        return resolve

    repository = CadEvidenceReconciliationRepository(
        scene_repository,
        kind_resolvers={
            'capture': _external('capture', 'a' * 64),
            'floor_plan': _external('floor_plan', 'b' * 64),
            'imported_geometry': _external(
                'imported_geometry', 'c' * 64
            ),
            'measurement': _scene_measured,
            # Manual dimensions carry operator provenance — ID-bearing
            # only, never a hash.
            'manual_dimension': lambda ref_id: ResolvedAuthority(
                kind='manual_dimension',
                ref_id=ref_id,
                document_id=DOC,
                semantic_sha256=None,
            ),
        },
    )
    return repository, saved


def test_reconcile_consistent_sources(tmp_path: Path) -> None:
    _repository, saved = _seed(tmp_path)
    subject = _subject(saved.revision)
    observations = _observations(subject.subject_id)
    decision = reconcile_subject(
        subject,
        observations,
        tolerance=0.02,
        tolerance_unit='m',
        decided_at_utc='2026-09-24T00:00:00+00:00',
    )
    assert decision.outcome == 'consistent'
    assert len(decision.comparisons) == 1
    assert len(decision.decision_sha256) == 64
    assert sorted(decision.observation_ids) == list(
        decision.observation_ids
    )
    assert set(decision.observation_ids) == {
        item.observation_id for item in observations
    }


def test_reconcile_conflict_beyond_tolerance(tmp_path: Path) -> None:
    _repository, saved = _seed(tmp_path)
    subject = _subject(saved.revision)
    frame = _capture_frame()
    observations = (
        build_observation(
            subject_id=subject.subject_id,
            source='capture',
            source_ref='capture-proj-A',
            source_sha256='a' * 64,
            value=4.50,
            unit='m',
            uncertainty=0.01,
            alignment=frame,
        ),
        build_observation(
            subject_id=subject.subject_id,
            source='manual_dimension',
            value=4.80,
            unit='m',
            uncertainty=0.01,
            alignment=frame,
        ),
    )
    decision = reconcile_subject(
        subject,
        observations,
        tolerance=0.02,
        tolerance_unit='m',
        decided_at_utc='2026-09-24T00:00:00+00:00',
    )
    assert decision.outcome == 'conflict'


def test_reconcile_converts_units_through_canonical_policy(
    tmp_path: Path,
) -> None:
    _repository, saved = _seed(tmp_path)
    subject = _subject(saved.revision)
    frame = _capture_frame()
    observations = (
        build_observation(
            subject_id=subject.subject_id,
            source='capture',
            source_ref='capture-proj-A',
            source_sha256='a' * 64,
            value=4.50,
            unit='m',
            uncertainty=0.01,
            alignment=frame,
        ),
        build_observation(
            subject_id=subject.subject_id,
            source='manual_dimension',
            value=450.5,
            unit='cm',
            uncertainty=1.0,
            alignment=frame,
        ),
    )
    decision = reconcile_subject(
        subject,
        observations,
        tolerance=2.0,
        tolerance_unit='cm',
        decided_at_utc='2026-09-24T00:00:00+00:00',
    )
    assert decision.outcome == 'consistent'

    with pytest.raises(ValueError):
        reconcile_subject(
            subject,
            observations,
            tolerance=0.02,
            decided_at_utc='2026-09-24T00:00:00+00:00',
        )


def test_reconcile_unit_family_mismatch_not_comparable(
    tmp_path: Path,
) -> None:
    _repository, saved = _seed(tmp_path)
    subject = _subject(saved.revision)
    frame = _capture_frame()
    observations = (
        build_observation(
            subject_id=subject.subject_id,
            source='capture',
            source_ref='capture-proj-A',
            source_sha256='a' * 64,
            value=4.50,
            unit='m',
            uncertainty=0.01,
            alignment=frame,
        ),
        build_observation(
            subject_id=subject.subject_id,
            source='manual_dimension',
            value=45.0,
            unit='deg',
            uncertainty=0.1,
            alignment=frame,
        ),
    )
    decision = reconcile_subject(
        subject,
        observations,
        tolerance=0.02,
        tolerance_unit='m',
        decided_at_utc='2026-09-24T00:00:00+00:00',
    )
    assert decision.outcome == 'not_comparable'


def test_reconcile_not_comparable_when_alignment_unresolved(
    tmp_path: Path,
) -> None:
    _repository, saved = _seed(tmp_path)
    subject = _subject(saved.revision)
    observations = (
        build_observation(
            subject_id=subject.subject_id,
            source='floor_plan',
            source_ref='floor-plan.pdf',
            source_sha256='b' * 64,
            value=4.50,
            unit='m',
            uncertainty=0.01,
            alignment=None,
        ),
        build_observation(
            subject_id=subject.subject_id,
            source='manual_dimension',
            value=4.50,
            unit='m',
            uncertainty=0.01,
            alignment=_capture_frame(),
        ),
    )
    decision = reconcile_subject(
        subject,
        observations,
        tolerance=0.02,
        tolerance_unit='m',
        decided_at_utc='2026-09-24T00:00:00+00:00',
    )
    assert decision.outcome == 'not_comparable'

    different_frames = (
        build_observation(
            subject_id=subject.subject_id,
            source='capture',
            source_ref='capture-A',
            source_sha256='a' * 64,
            value=4.50,
            unit='m',
            uncertainty=0.01,
            alignment=AlignmentRef(
                frame_kind='capture_frame',
                frame_id='A',
                frame_sha256='a' * 64,
            ),
        ),
        build_observation(
            subject_id=subject.subject_id,
            source='capture',
            source_ref='capture-B',
            source_sha256='b' * 64,
            value=4.50,
            unit='m',
            uncertainty=0.01,
            alignment=AlignmentRef(
                frame_kind='capture_frame',
                frame_id='B',
                frame_sha256='b' * 64,
            ),
        ),
    )
    decision = reconcile_subject(
        subject,
        different_frames,
        tolerance=0.02,
        tolerance_unit='m',
        decided_at_utc='2026-09-24T00:00:00+00:00',
    )
    assert decision.outcome == 'not_comparable'


def test_reconcile_unknown_uncertainty_stays_unknown(tmp_path: Path) -> None:
    _repository, saved = _seed(tmp_path)
    subject = _subject(saved.revision)
    frame = _capture_frame()
    observations = (
        build_observation(
            subject_id=subject.subject_id,
            source='imported_geometry',
            source_ref='import-1',
            source_sha256='c' * 64,
            value=4.50,
            unit='m',
            uncertainty=None,
            alignment=frame,
        ),
        build_observation(
            subject_id=subject.subject_id,
            source='manual_dimension',
            value=4.60,
            unit='m',
            uncertainty=0.01,
            alignment=frame,
        ),
    )
    decision = reconcile_subject(
        subject,
        observations,
        tolerance=0.02,
        tolerance_unit='m',
        decided_at_utc='2026-09-24T00:00:00+00:00',
    )
    assert decision.outcome == 'unknown'
    assert decision.comparisons[0].outcome == 'unknown'


def test_reconcile_insufficient(tmp_path: Path) -> None:
    _repository, saved = _seed(tmp_path)
    subject = _subject(saved.revision)
    decision = reconcile_subject(
        subject,
        (),
        decided_at_utc='2026-09-24T00:00:00+00:00',
    )
    assert decision.outcome == 'insufficient'


def test_hash_bearing_sources_require_exact_provenance(tmp_path: Path) -> None:
    _repository, saved = _seed(tmp_path)
    subject = _subject(saved.revision)
    with pytest.raises(ValueError):
        build_observation(
            subject_id=subject.subject_id,
            source='capture',
            value=4.50,
            unit='m',
        )
    with pytest.raises(ValueError):
        build_observation(
            subject_id=subject.subject_id,
            source='imported_geometry',
            source_ref='import-1',
            value=4.50,
            unit='m',
        )
    with pytest.raises(ValueError):
        AlignmentRef(frame_kind='scene_datum', frame_id='datum-1')
    manual = build_observation(
        subject_id=subject.subject_id,
        source='manual_dimension',
        source_ref='tape-1',
        value=4.5,
        unit='m',
        alignment=AlignmentRef(
            frame_kind='manual_frame', frame_id='datum'
        ),
    )
    assert manual.source == 'manual_dimension'


def test_reconciliation_repository_append_only_and_replay(
    tmp_path: Path,
) -> None:
    repository, saved = _seed(tmp_path)
    subject = _subject(saved.revision)
    repository.save_subject(subject)
    assert repository.get_subject(subject.subject_id) == subject
    with pytest.raises(ReconciliationConflictError):
        repository.save_subject(subject)

    foreign = _subject(saved.revision, document_id='foreign-doc')
    with pytest.raises(ValueError):
        repository.save_subject(foreign)

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
        tolerance_unit='m',
        decided_at_utc='2026-09-24T00:00:00+00:00',
    )
    repository.save_decision(decision)
    assert repository.latest_decision(subject.subject_id) == decision
    with pytest.raises(ReconciliationConflictError):
        repository.save_decision(decision)

    with pytest.raises(ValueError):
        repository.save_observation(
            build_observation(
                subject_id='ghost',
                source='other',
                value=1.0,
                unit='m',
            )
        )


def test_decision_replay_rejects_forged_outcome_and_unknown_pin(
    tmp_path: Path,
) -> None:
    repository, saved = _seed(tmp_path)
    subject = _subject(saved.revision)
    repository.save_subject(subject)
    observations = _observations(subject.subject_id)
    for observation in observations:
        repository.save_observation(observation)

    decision = reconcile_subject(
        subject,
        observations,
        tolerance=0.02,
        tolerance_unit='m',
        decided_at_utc='2026-09-24T00:00:00+00:00',
    )
    assert decision.outcome == 'consistent'

    forged = decision.model_copy(update={'outcome': 'conflict'})
    with pytest.raises(ValueError):
        repository.save_decision(forged)

    unknown_pin = decision.model_copy(
        update={
            'observation_ids': (
                observations[0].observation_id,
                'observation-not-persisted',
            )
        }
    )
    with pytest.raises(ValueError):
        repository.save_decision(unknown_pin)


def test_observation_provenance_resolves_exact_authority(
    tmp_path: Path,
) -> None:
    repository, saved = _seed(tmp_path)
    subject = _subject(saved.revision)
    repository.save_subject(subject)

    pinned = build_observation(
        subject_id=subject.subject_id,
        source='measurement',
        source_ref=saved.revision.revision_id,
        source_sha256=saved.revision.content_hash,
        value=4.5,
        unit='m',
        alignment=_capture_frame(),
    )
    repository.save_observation(pinned)

    stale = build_observation(
        subject_id=subject.subject_id,
        source='measurement',
        source_ref=saved.revision.revision_id,
        source_sha256='b' * 64,
        value=4.5,
        unit='m',
        alignment=_capture_frame(),
    )
    with pytest.raises(ValueError):
        repository.save_observation(stale)
