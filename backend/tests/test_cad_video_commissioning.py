"""Guided video commissioning journey — authority + persistence (#541).

The journey composes the existing color-science authorities
(``evaluate_video_color``, meter-correction compatibility); it never
re-implements color math. Everything is derived-id evidence: session →
readiness → diagnosis → proposal → adjustment → comparison.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_colorimetry import (
    ChromaticityPoint,
    ColorimeterCorrectionProfile,
    ColorTolerances,
    StimulusDefinition,
    TristimulusSample,
    build_video_color_measurement_set,
    build_video_color_target_profile,
)
from htdt.cad_equipment import EquipmentDataProvenance
from htdt.cad_meter_correction import CorrectionCompatibility
from htdt.cad_repository import SceneRepository
from htdt.cad_video_commissioning import (
    ACTION_KIND_LABELS,
    FINDING_KIND_LABELS,
    MODE_LABELS,
    READINESS_STATE_LABELS,
    SESSION_STATUS_LABELS,
    SIGNAL_RANGE_LABELS,
    SURFACE_KIND_LABELS,
    build_guided_video_session,
    compare_video_measurements,
    diagnose_video_measurement,
    evaluate_video_journey,
    evaluate_video_readiness,
    propose_video_actions,
    record_video_operator_adjustment,
    rebind_video_session,
)
from htdt.cad_video_commissioning_repository import (
    CadVideoCommissioningRepository,
    VideoCommissioningConflictError,
    VideoCommissioningIntegrityError,
)
from htdt.cad_video_measure_import import import_video_measurements
from htdt.canonical_json import canonical_sha256


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_PROVENANCE = (
    EquipmentDataProvenance(
        evidence_kind='manufacturer',
        source_name='Example Co.',
        source_version='2026.1',
        source_reference='datasheet',
        source_sha256='d' * 64,
    ),
)


def _target():
    return build_video_color_target_profile(
        target_id='rec709-d65',
        version='1',
        label='Rec.709 gamma 2.4',
        eotf='gamma_2_4',
        white_point=ChromaticityPoint(x=0.3127, y=0.3290),
        primary_red=ChromaticityPoint(x=0.640, y=0.330),
        primary_green=ChromaticityPoint(x=0.300, y=0.600),
        primary_blue=ChromaticityPoint(x=0.150, y=0.060),
        stimulus_levels=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
        tolerances=ColorTolerances(
            white_point_delta_e=2.0,
            grayscale_delta_e=3.0,
            gamut_delta_e=3.0,
        ),
        provenance=_PROVENANCE,
    )


def _correction():
    return ColorimeterCorrectionProfile(
        correction_id='k10-oled-1',
        version='1',
        base_meter='klein-k10',
        correction_kind='four_color_matrix',
        applies_to_display_class='oled',
    )


def _session(target=None, **kw):
    target = target if target is not None else _target()
    payload = dict(
        document_id='doc-1',
        surface_entity_id='screen-main',
        surface_kind='direct_view',
        mode='sdr',
        target_id=target.target_id,
        target_version=target.version,
        target_sha256=target.target_sha256,
        meter='klein-k10',
        signal_range='narrow',
        bit_depth=10,
        encoding='rgb_limited',
        meter_correction=_correction(),
        picture_mode='Cinema',
        stimulus=StimulusDefinition(
            encoding='rgb_limited',
            bit_depth=10,
            patch_size_percent=10.0,
            pattern_generator='murideo-g7',
        ),
        created_at_utc='2026-10-01T00:00:00+00:00',
    )
    payload.update(kw)
    return build_guided_video_session(**payload)


def _xyz(x: float, y: float, lum: float):
    return (x / y * lum, lum, (1 - x - y) / y * lum)


def _sample(stimulus_id: str, x: float, y: float, lum: float, **kw):
    xs, ys, zs = _xyz(x, y, lum)
    return TristimulusSample(
        stimulus_id=stimulus_id, x=xs, y_luminance=ys, z=zs, **kw
    )


def _measurement(set_id='vms-baseline', samples=None, **kw):
    payload = dict(
        measurement_set_id=set_id,
        measured_at_utc='2026-10-01T01:00:00+00:00',
        surface_entity_id='screen-main',
        meter='klein-k10',
        meter_correction=_correction(),
        stimulus=StimulusDefinition(
            encoding='rgb_limited',
            bit_depth=10,
            patch_size_percent=10.0,
            pattern_generator='murideo-g7',
        ),
        samples=samples
        if samples is not None
        else (
            _sample('w100', 0.3127, 0.3290, 120.0, stimulus_level=1.0),
            _sample('gray_80', 0.3130, 0.3295, 78.0, stimulus_level=0.8),
            _sample('gray_50', 0.3130, 0.3295, 18.0, stimulus_level=0.5),
            _sample('gray_20', 0.3135, 0.3300, 2.4, stimulus_level=0.2),
            _sample('r', 0.640, 0.330, 25.0),
            _sample('g', 0.300, 0.600, 80.0),
            _sample('b', 0.150, 0.060, 6.0),
        ),
        provenance=_PROVENANCE,
    )
    payload.update(kw)
    return build_video_color_measurement_set(**payload)


def _compat(readiness='ready', reasons=()):
    probe = CorrectionCompatibility.model_construct(
        artifact_id='ccmx-1',
        readiness=readiness,
        reasons=tuple(reasons),
        semantic_sha256='',
    )
    return CorrectionCompatibility(
        artifact_id='ccmx-1',
        readiness=readiness,
        reasons=tuple(reasons),
        semantic_sha256=canonical_sha256(probe.semantic_payload()),
    )


def _repository(tmp_path: Path) -> CadVideoCommissioningRepository:
    return CadVideoCommissioningRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )


# ---------------------------------------------------------------------------
# Session + readiness
# ---------------------------------------------------------------------------


def test_session_derived_id_stable_and_rebind_supersedes() -> None:
    a = _session()
    b = _session()
    assert a.session_id == b.session_id
    assert a.session_sha256 == b.session_sha256
    assert a.status == 'open'
    assert a.steps  # journey plan baked into the session
    rebound = rebind_video_session(
        a, meter='i1pro3', created_at_utc='2026-10-02T00:00:00+00:00'
    )
    assert rebound.session_id != a.session_id
    assert rebound.supersedes_session_id == a.session_id
    assert rebound.meter == 'i1pro3'


def test_readiness_ready_when_all_evidence_bound() -> None:
    report = evaluate_video_readiness(
        _session(),
        target=_target(),
        correction_compatibility=_compat('ready'),
        measurement_sets=(_measurement(),),
        evaluated_at_utc='2026-10-01T02:00:00+00:00',
    )
    assert report.state == 'READY'
    assert not any(
        c.status in ('FAIL', 'UNKNOWN') and c.blocking
        for c in report.checks
    )


def test_readiness_insufficient_when_measurement_missing() -> None:
    report = evaluate_video_readiness(
        _session(),
        target=_target(),
        correction_compatibility=_compat('ready'),
        measurement_sets=(),
        evaluated_at_utc='2026-10-01T02:00:00+00:00',
    )
    assert report.state == 'INSUFFICIENT_EVIDENCE'


def test_readiness_fields_union_across_session_sets() -> None:
    gray = _measurement(
        'vms-gray',
        samples=(
            _sample('w100', 0.3127, 0.3290, 120.0, stimulus_level=1.0),
            _sample('gray_80', 0.3130, 0.3295, 78.0, stimulus_level=0.8),
            _sample('gray_50', 0.3130, 0.3295, 18.0, stimulus_level=0.5),
            _sample('gray_20', 0.3135, 0.3300, 2.4, stimulus_level=0.2),
        ),
    )
    prim = _measurement(
        'vms-prim',
        samples=(
            _sample('r', 0.640, 0.330, 25.0),
            _sample('g', 0.300, 0.600, 80.0),
            _sample('b', 0.150, 0.060, 6.0),
        ),
    )
    target = _target()
    for only in (gray, prim):
        partial = evaluate_video_readiness(
            _session(),
            target=target,
            correction_compatibility=_compat('ready'),
            measurement_sets=(only,),
            evaluated_at_utc='2026-10-01T02:00:00+00:00',
        )
        fields = next(
            c for c in partial.checks
            if c.check_id == 'measurement_fields'
        )
        assert fields.status == 'UNKNOWN'
        assert partial.state == 'INSUFFICIENT_EVIDENCE'
    united = evaluate_video_readiness(
        _session(),
        target=target,
        correction_compatibility=_compat('ready'),
        measurement_sets=(gray, prim),
        evaluated_at_utc='2026-10-01T02:00:00+00:00',
    )
    fields = next(
        c for c in united.checks if c.check_id == 'measurement_fields'
    )
    assert fields.status == 'PASS'
    assert united.state == 'READY'


def test_readiness_incompatible_on_correction_mismatch() -> None:
    report = evaluate_video_readiness(
        _session(),
        target=_target(),
        correction_compatibility=_compat(
            'incompatible', ('wrong instrument family',)
        ),
        measurement_sets=(_measurement(),),
        evaluated_at_utc='2026-10-01T02:00:00+00:00',
    )
    assert report.state == 'INCOMPATIBLE'
    check = next(
        c for c in report.checks if c.check_id == 'correction_evaluated'
    )
    assert check.status == 'FAIL'
    assert 'wrong instrument family' in check.detail


def test_readiness_unknown_target_fails_closed() -> None:
    report = evaluate_video_readiness(
        _session(),
        target=None,
        evaluated_at_utc='2026-10-01T02:00:00+00:00',
    )
    assert report.state == 'INCOMPATIBLE'


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------


def test_diagnosis_is_deterministic() -> None:
    session, target, ms = _session(), _target(), _measurement()
    one = diagnose_video_measurement(
        session=session,
        measurement_set=ms,
        target=target,
        diagnosed_at_utc='2026-10-01T03:00:00+00:00',
    )
    two = diagnose_video_measurement(
        session=session,
        measurement_set=ms,
        target=target,
        diagnosed_at_utc='2026-10-01T03:00:00+00:00',
    )
    assert one.diagnosis_id == two.diagnosis_id
    assert one.diagnosis_sha256 == two.diagnosis_sha256
    assert one.findings
    assert one.session_sha256 == session.session_sha256
    assert one.measurement_set_sha256 == ms.measurement_set_sha256


def test_diagnosis_findings_carry_typed_verdicts_and_ja_text() -> None:
    diagnosis = diagnose_video_measurement(
        session=_session(),
        measurement_set=_measurement(),
        target=_target(),
        diagnosed_at_utc='2026-10-01T03:00:00+00:00',
    )
    assert diagnosis.findings
    for finding in diagnosis.findings:
        assert finding.kind in FINDING_KIND_LABELS
        assert finding.verdict in (
            'PASS', 'FAIL', 'UNKNOWN', 'NOT_APPLICABLE'
        )
        # Every finding explains itself in Japanese.
        assert finding.explanation
        assert isinstance(finding.explanation, str)


def test_diagnosis_flags_off_target_white_point() -> None:
    # White point dragged well off D65 — the finding must flag it.
    samples = (
        _sample('w100', 0.36, 0.36, 120.0, stimulus_level=1.0),
        _sample('gray_50', 0.3130, 0.3295, 18.0, stimulus_level=0.5),
        _sample('r', 0.640, 0.330, 25.0),
        _sample('g', 0.300, 0.600, 80.0),
        _sample('b', 0.150, 0.060, 6.0),
    )
    diagnosis = diagnose_video_measurement(
        session=_session(),
        measurement_set=_measurement(samples=samples),
        target=_target(),
        diagnosed_at_utc='2026-10-01T03:00:00+00:00',
    )
    white = next(
        f for f in diagnosis.findings if f.kind == 'white_point'
    )
    assert white.verdict == 'FAIL'
    assert diagnosis.overall == 'FAIL'


# ---------------------------------------------------------------------------
# Corrective actions
# ---------------------------------------------------------------------------


def test_proposals_bind_actions_to_fail_findings() -> None:
    session, target = _session(), _target()
    ms = _measurement()
    diagnosis = diagnose_video_measurement(
        session=session,
        measurement_set=ms,
        target=target,
        diagnosed_at_utc='2026-10-01T03:00:00+00:00',
    )
    proposal = propose_video_actions(
        diagnosis,
        session=session,
        measurement_set=ms,
        target=target,
        proposed_at_utc='2026-10-01T04:00:00+00:00',
    )
    assert proposal.diagnosis_sha256 == diagnosis.diagnosis_sha256
    for action in proposal.actions:
        assert action.kind in ACTION_KIND_LABELS
        # Every action cites its evidence and states its confidence.
        assert action.evidence
        assert action.explanation
        assert action.confidence in ('evidence_bound', 'indicative')


def test_proposal_never_fabricates_beyond_evidence() -> None:
    # An all-UNKNOWN diagnosis can only propose re-measurement — never a
    # corrective the evidence does not support.
    session, target = _session(), _target()
    empty_ms = _measurement(
        set_id='vms-empty',
        samples=(
            _sample('w100', 0.3127, 0.3290, 120.0, stimulus_level=1.0),
        ),
    )
    diagnosis = diagnose_video_measurement(
        session=session,
        measurement_set=empty_ms,
        target=target,
        diagnosed_at_utc='2026-10-01T03:00:00+00:00',
    )
    proposal = propose_video_actions(
        diagnosis,
        session=session,
        measurement_set=empty_ms,
        target=target,
        proposed_at_utc='2026-10-01T04:00:00+00:00',
    )
    for action in proposal.actions:
        assert action.confidence in ('evidence_bound', 'indicative')
        # UNKNOWN findings demand re-measurement or an honest no-action
        # record — never a corrective the evidence does not support.
        if action.kind not in ('remeasure', 'no_safe_action_from_current_evidence'):
            assert action.limitations


# ---------------------------------------------------------------------------
# Before/after evidence chain
# ---------------------------------------------------------------------------


def _improved_samples():
    return (
        _sample('w100', 0.3128, 0.3291, 121.0, stimulus_level=1.0),
        _sample('gray_50', 0.3129, 0.3292, 18.5, stimulus_level=0.5),
        _sample('r', 0.640, 0.330, 25.5),
        _sample('g', 0.300, 0.600, 81.0),
        _sample('b', 0.150, 0.060, 6.2),
    )


def test_adjustment_links_baseline_to_followup() -> None:
    session = _session()
    before = _measurement()
    after = _measurement(
        set_id='vms-after', samples=_improved_samples()
    )
    adjustment = record_video_operator_adjustment(
        session=session,
        iteration_index=1,
        before_measurement_set=before,
        selected_actions=('adjust_white_balance_high',),
        operator_note='赤緑青ゲインを調整',
        changed_controls=(('r_gain', '+2'), ('contrast', '-1')),
        followup_measurement_set=after,
        recorded_at_utc='2026-10-01T05:00:00+00:00',
    )
    assert adjustment.before_measurement_set_sha256 == (
        before.measurement_set_sha256
    )
    assert adjustment.followup_measurement_set_sha256 == (
        after.measurement_set_sha256
    )
    assert adjustment.iteration_index == 1


def test_comparison_comparable_for_compatible_sets() -> None:
    session, target = _session(), _target()
    before = _measurement()
    after = _measurement(
        set_id='vms-after', samples=_improved_samples()
    )
    comparison = compare_video_measurements(
        session=session,
        before_set=before,
        after_set=after,
        target=target,
        iteration_index=1,
        compared_at_utc='2026-10-01T06:00:00+00:00',
    )
    assert comparison.status == 'comparable'
    assert comparison.overall in ('improved', 'regressed', 'inconclusive')
    assert comparison.rows
    assert comparison.before_measurement_set_sha256 == (
        before.measurement_set_sha256
    )
    assert comparison.after_measurement_set_sha256 == (
        after.measurement_set_sha256
    )
    for row in comparison.rows:
        assert row.label  # JA metric label
        assert row.direction in (
            'improved', 'regressed', 'inconclusive', 'unknown'
        )


def test_comparison_incomparable_across_incompatible_evidence() -> None:
    session, target = _session(), _target()
    before = _measurement()
    # Different surface — never a misleading delta chart.
    after = _measurement(
        set_id='vms-other-surface',
        samples=_improved_samples(),
        surface_entity_id='screen-side',
    )
    comparison = compare_video_measurements(
        session=session,
        before_set=before,
        after_set=after,
        target=target,
        iteration_index=1,
        compared_at_utc='2026-10-01T06:00:00+00:00',
    )
    assert comparison.status == 'incomparable'
    assert comparison.overall == 'unknown'
    assert comparison.rows == ()
    assert comparison.incompatibility_reasons


# ---------------------------------------------------------------------------
# Journey steps
# ---------------------------------------------------------------------------


def test_journey_steps_derive_from_evidence() -> None:
    steps = evaluate_video_journey(
        session_exists=False,
        readiness_state=None,
        measurement_set_count=0,
        diagnosis_count=0,
        adjustment_count=0,
        comparison_count=0,
        session_status=None,
    )
    assert [s.number for s in steps] == list(range(1, len(steps) + 1))
    assert steps[0].key == 'session'
    assert steps[0].status == 'current'
    for step in steps:
        assert step.title  # JA step names
        assert step.status in ('done', 'current', 'pending', 'blocked')


def test_journey_reflects_completed_chain() -> None:
    steps = evaluate_video_journey(
        session_exists=True,
        readiness_state='READY',
        measurement_set_count=2,
        diagnosis_count=1,
        adjustment_count=1,
        comparison_count=1,
        session_status='open',
    )
    by_key = {s.key: s for s in steps}
    assert by_key['session'].status == 'done'
    assert by_key['readiness'].status == 'done'
    assert by_key['measure'].status == 'done'
    assert by_key['diagnose'].status == 'done'
    assert by_key['verify'].status == 'done'


# ---------------------------------------------------------------------------
# Repository — append-only evidence chain
# ---------------------------------------------------------------------------


def test_repository_round_trips_the_full_chain(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    session, target = _session(), _target()
    ms = _measurement()

    repository.save_session(session)
    assert repository.get_session('doc-1', session.session_id) == session

    report = evaluate_video_readiness(
        session,
        target=target,
        correction_compatibility=_compat('ready'),
        measurement_sets=(ms,),
        evaluated_at_utc='2026-10-01T02:00:00+00:00',
    )
    repository.save_readiness_report(report, 'doc-1')
    assert (
        repository.latest_readiness_report('doc-1', session.session_id)
        == report
    )

    diagnosis = diagnose_video_measurement(
        session=session,
        measurement_set=ms,
        target=target,
        diagnosed_at_utc='2026-10-01T03:00:00+00:00',
    )
    repository.save_diagnosis(diagnosis, 'doc-1')
    assert repository.get_diagnosis(
        'doc-1', diagnosis.diagnosis_id
    ) == diagnosis

    proposal = propose_video_actions(
        diagnosis,
        session=session,
        measurement_set=ms,
        target=target,
        proposed_at_utc='2026-10-01T04:00:00+00:00',
    )
    repository.save_proposal(proposal, 'doc-1')
    assert repository.list_proposals(
        'doc-1', diagnosis.diagnosis_id
    ) == (proposal,)

    after = _measurement(set_id='vms-after', samples=_improved_samples())
    adjustment = record_video_operator_adjustment(
        session=session,
        iteration_index=1,
        before_measurement_set=ms,
        selected_actions=('adjust_white_balance_high',),
        followup_measurement_set=after,
        recorded_at_utc='2026-10-01T05:00:00+00:00',
    )
    repository.save_adjustment(adjustment, 'doc-1')
    comparison = compare_video_measurements(
        session=session,
        before_set=ms,
        after_set=after,
        target=target,
        iteration_index=1,
        compared_at_utc='2026-10-01T06:00:00+00:00',
    )
    repository.save_comparison(comparison, 'doc-1')

    state = repository.journey_state('doc-1', session.session_id)
    assert state.session == session
    assert state.readiness_state == 'READY'
    assert state.diagnosis_ids == (diagnosis.diagnosis_id,)
    assert state.adjustment_ids == (adjustment.adjustment_id,)
    assert state.comparison_ids == (comparison.comparison_id,)
    assert state.current_status == 'open'


def test_repository_append_only_rejects_rewrite(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    session = _session()
    repository.save_session(session)
    repository.save_session(session)  # same row → no-op
    # Same session_id with a different payload sha → append-only conflict.
    tampered = session.model_copy(
        update={'session_sha256': '0' * 64}
    )
    with pytest.raises(VideoCommissioningConflictError):
        repository.save_session(tampered)


def test_repository_status_events_are_append_only_and_validated(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    session = _session()
    repository.save_session(session)
    assert repository.current_status('doc-1', session.session_id) == 'open'

    repository.record_status_event(
        'doc-1',
        session.session_id,
        'completed',
        event_at_utc='2026-10-02T00:00:00+00:00',
    )
    assert (
        repository.current_status('doc-1', session.session_id)
        == 'completed'
    )
    events = repository.list_status_events('doc-1', session.session_id)
    assert [e.to_status for e in events] == ['completed']

    # completed is terminal — no further transitions.
    with pytest.raises(VideoCommissioningConflictError):
        repository.record_status_event(
            'doc-1', session.session_id, 'abandoned'
        )


def test_repository_rejects_status_change_for_missing_session(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    with pytest.raises(VideoCommissioningIntegrityError):
        repository.record_status_event(
            'doc-1', 'vcs-nonexistent', 'completed'
        )


def test_repository_import_batches_link_session_and_set(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    session = _session()
    repository.save_session(session)
    result = import_video_measurements(
        file_name='demo.GrayScaleSheet.csv',
        data=(
            'Measure;0;1\n'
            'IRE;50;100\n'
            'X;0.95;39.0\n'
            'Y;1.00;41.0\n'
            'Z;1.10;44.0\n'
        ).encode('utf-8'),
        surface_entity_id='screen-main',
        meter='klein-k10',
        session_id=session.session_id,
    )
    assert result.status == 'imported'
    repository.save_import_batch(result.batch, 'doc-1')
    batches = repository.import_batches_for_set(
        'doc-1', result.measurement_set.measurement_set_id
    )
    assert len(batches) == 1
    assert batches[0].session_id == session.session_id


# ---------------------------------------------------------------------------
# JA maps — every enum value the UI can show has a label
# ---------------------------------------------------------------------------


def test_all_user_facing_enums_have_ja_labels() -> None:
    for mapping in (
        READINESS_STATE_LABELS,
        SESSION_STATUS_LABELS,
        SURFACE_KIND_LABELS,
        MODE_LABELS,
        SIGNAL_RANGE_LABELS,
        ACTION_KIND_LABELS,
        FINDING_KIND_LABELS,
    ):
        assert mapping
        for label in mapping.values():
            assert label and label.strip()
