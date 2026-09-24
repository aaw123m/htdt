"""Session-5 evidence-integrity coverage (#825 #826 #831 #832 #872 #874).

These tests pin the contracts this batch added: wiring checks bound to exact
scene/variant/routing authority, timing-reference semantics that flat fields
can never strengthen, field-evidence row/target-index cross-checks, design-
brief single-head lineage, commissioning-run input integrity, and typed
stimulus authorities.
"""

from hashlib import sha256
from pathlib import Path
import sqlite3

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_commissioning import (
    AcceptedDeviation,
    CheckSubject,
    CommissioningCheck,
    CommissioningObservation,
    ToleranceSpec,
    build_commissioning_plan,
    build_commissioning_run,
    build_tolerance_profile,
)
from htdt.cad_commissioning_repository import CadCommissioningRepository
from htdt.cad_design_brief import (
    BriefGoalRef,
    build_design_brief,
    revise_design_brief,
)
from htdt.cad_design_brief_repository import (
    CadDesignBriefRepository,
    DesignBriefConflictError,
)
from htdt.cad_design_decision import (
    DecisionAuthorityRef,
    build_decision_record,
)
from htdt.cad_design_decision_repository import CadDesignDecisionRepository
from htdt.cad_field_evidence import (
    EvidenceTarget,
    build_field_evidence,
)
from htdt.cad_field_evidence_repository import CadFieldEvidenceRepository
from htdt.cad_measurement_authorities import (
    build_routing_profile,
    build_timing_reference,
    build_wiring_check,
    timing_reference_supports_common_timing,
)
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality import (
    CadMeasurementQualityEvidence,
    acquisition_context_binding,
    build_acquisition_context,
    build_measurement_quality_profile,
    build_measurement_quality_report,
    measurement_sha256,
)
from htdt.cad_measurement_quality_repository import (
    CadMeasurementQualityRepository,
)
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurement_stimulus import (
    build_excitation_asset,
    build_stimulus_profile,
    stimulus_comparison,
    stimulus_profiles_comparable,
)
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene


DOC = F1_DOCUMENT_ID


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(measurement_repository)
    return revision, measurement_repository, quality_repository


def _save_measurement(repository, revision, measurement_id):
    raw = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
    )
    record = measurement_record_for_revision(
        revision,
        'point-mlp',
        measurement_id=measurement_id,
        evidence_type='measured',
        channel_role='front_left',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
        routing_evidence='verified',
        source_kind='unknown',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f'dataset-{measurement_id}',
        measurement_id=measurement_id,
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
        phase_status='absent',
        level_reference='unknown',
        processing_json='{}',
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    repository.save(
        record, dataset, raw_filename=f'{measurement_id}.json', raw_bytes=raw
    )
    return record, dataset


# ---------------------------------------------------------------------------
# #825 wiring checks pinned to exact scene/variant/evidence authority


def _wiring_check(revision, **kwargs):
    kwargs.setdefault('check_kind', 'continuity')
    kwargs.setdefault('method', 'DCR probe')
    kwargs.setdefault('result', 'PASS')
    kwargs.setdefault('measured_at_utc', '2026-09-20T00:00:00+00:00')
    kwargs.setdefault('scene_revision_id', revision.revision_id)
    kwargs.setdefault('scene_revision_sha256', revision.content_hash)
    return build_wiring_check(document_id=revision.document_id, **kwargs)


def test_wiring_check_requires_resolvable_scene_pin(tmp_path: Path) -> None:
    revision, _, quality_repository = _repositories(tmp_path)

    with pytest.raises(ValueError, match='unknown scene revision'):
        quality_repository.save_wiring_check(
            _wiring_check(
                revision, scene_revision_id='rev-nonexistent', operator='op'
            )
        )
    with pytest.raises(ValueError, match='hash mismatch'):
        quality_repository.save_wiring_check(
            _wiring_check(
                revision, scene_revision_sha256='f' * 64, operator='op'
            )
        )


def test_wiring_check_speaker_ids_resolve_as_speakers(tmp_path: Path) -> None:
    revision, _, quality_repository = _repositories(tmp_path)

    with pytest.raises(ValueError, match='non-speaker'):
        quality_repository.save_wiring_check(
            _wiring_check(
                revision,
                expected_speaker_ids=('point-mlp',),
                operator='op',
            )
        )
    with pytest.raises(ValueError, match='outside the pinned scene'):
        quality_repository.save_wiring_check(
            _wiring_check(
                revision,
                observed_speaker_ids=('speaker-nope',),
                operator='op',
            )
        )


def test_wiring_check_evidence_refs_are_typed_and_resolved(
    tmp_path: Path,
) -> None:
    revision, measurement_repository, quality_repository = _repositories(
        tmp_path
    )
    record, _ = _save_measurement(measurement_repository, revision, 'm-ev')

    # A typed ref to a persisted authority resolves.
    check = _wiring_check(
        revision,
        evidence_refs=(
            AuthorityRef(
                kind='measurement',
                ref_id=record.measurement_id,
                ref_sha256=measurement_sha256(record),
            ),
            AuthorityRef(
                kind='scene_revision',
                ref_id=revision.revision_id,
                ref_sha256=revision.content_hash,
            ),
        ),
    )
    quality_repository.save_wiring_check(check)
    assert quality_repository.get_wiring_check(check.check_id) == check

    # A forged semantic hash is rejected, even when the id is real.
    with pytest.raises(ValueError):
        quality_repository.save_wiring_check(
            _wiring_check(
                revision,
                evidence_refs=(
                    AuthorityRef(
                        kind='measurement',
                        ref_id=record.measurement_id,
                        ref_sha256='0' * 64,
                    ),
                ),
            )
        )
    # An unknown authority id is rejected.
    with pytest.raises(ValueError):
        quality_repository.save_wiring_check(
            _wiring_check(
                revision,
                evidence_refs=(
                    AuthorityRef(
                        kind='measurement',
                        ref_id='m-ghost',
                        ref_sha256='0' * 64,
                    ),
                ),
            )
        )


def test_wiring_check_pass_requires_attribution(tmp_path: Path) -> None:
    revision, _, _ = _repositories(tmp_path)
    # Model-level: PASS with neither evidence nor operator is a bare claim.
    with pytest.raises(ValueError, match='typed evidence or a named operator'):
        _wiring_check(revision)
    # FAIL stays attestation-free but still needs a reason.
    with pytest.raises(ValueError, match='reason'):
        _wiring_check(revision, result='FAIL')
    check = _wiring_check(
        revision, result='FAIL', reason='open circuit', operator=None
    )
    assert check.result == 'FAIL'


def test_wiring_check_acoustic_polarity_needs_typed_evidence(
    tmp_path: Path,
) -> None:
    revision, _, _ = _repositories(tmp_path)
    with pytest.raises(ValueError, match='acoustic polarity PASS'):
        _wiring_check(
            revision, check_kind='acoustic_polarity', operator='op'
        )
    check = _wiring_check(
        revision,
        check_kind='acoustic_polarity',
        evidence_refs=(
            AuthorityRef(
                kind='scene_revision',
                ref_id=revision.revision_id,
                ref_sha256=revision.content_hash,
            ),
        ),
    )
    assert check.result == 'PASS'


def test_wiring_check_routing_pass_requires_bound_profile(
    tmp_path: Path,
) -> None:
    revision, _, quality_repository = _repositories(tmp_path)
    # Model gate: routing PASS without a bound profile is impossible.
    with pytest.raises(ValueError, match='bound routing profile'):
        _wiring_check(revision, check_kind='routing', operator='op')

    profile = build_routing_profile(
        entries=(
            {
                'output_device_label': 'AVR',
                'rew_channel_label': 'C:FL',
                'hardware_channel_index': 0,
                'logical_role': 'front_left',
                'expected_speaker_ids': ('speaker-fl',),
                'observed_speaker_ids': ('speaker-fl',),
            },
        )
    )
    quality_repository.save_routing_profile(profile)
    check = _wiring_check(
        revision,
        check_kind='routing',
        routing_profile=profile,
        expected_output_reference='C:FL',
        expected_speaker_ids=('speaker-fl',),
        operator='op',
    )
    quality_repository.save_wiring_check(check)
    assert quality_repository.get_wiring_check(check.check_id) == check

    # An output reference the bound profile does not carry is rejected.
    with pytest.raises(ValueError, match='routing profile entry'):
        quality_repository.save_wiring_check(
            _wiring_check(
                revision,
                check_kind='routing',
                routing_profile=profile,
                expected_output_reference='C:SL',
                operator='op',
            )
        )
    # A different profile hash is rejected.
    other = build_routing_profile(
        entries=(
            {
                'output_device_label': 'AVR',
                'rew_channel_label': 'C:FR',
                'hardware_channel_index': 1,
            },
        )
    )
    quality_repository.save_routing_profile(other)
    forged = _wiring_check(
        revision,
        check_kind='routing',
        routing_profile=other,
        operator='op',
    )
    forged = forged.model_copy(
        update={
            'routing_profile': forged.routing_profile.model_copy(
                update={
                    'routing_profile_id': profile.routing_profile_id,
                }
            )
        }
    )
    with pytest.raises(ValueError, match='hash mismatch'):
        quality_repository.save_wiring_check(forged)


def test_wiring_check_unknown_system_variant_rejected(tmp_path: Path) -> None:
    revision, _, quality_repository = _repositories(tmp_path)
    with pytest.raises(ValueError):
        quality_repository.save_wiring_check(
            _wiring_check(
                revision,
                system_variant_id='variant-ghost',
                system_variant_sha256='1' * 64,
                operator='op',
            )
        )


# ---------------------------------------------------------------------------
# #826 timing-reference authority: flat fields cannot strengthen it


def test_manual_timing_reference_never_supports_common_timing() -> None:
    for method in ('manual', 'imported', 'unknown'):
        reference = build_timing_reference(
            method=method,
            reference_channel='ch-1',
            input_clock_identity='clk',
            output_clock_identity='clk',
            sample_rate_hz=48000,
            t0_convention='sweep_start',
            delay_corrections=(
                {'correction_kind': 'driver_latency', 'value_s': 0.002},
            ),
        )
        assert not timing_reference_supports_common_timing(reference)

    strong = build_timing_reference(
        method='acoustic_reference',
        reference_channel='ch-1',
        sample_rate_hz=48000,
        t0_convention='acoustic_reference_signal',
    )
    assert timing_reference_supports_common_timing(strong)
    # Method alone is not enough — the t0 convention/channel evidence is
    # what makes the reference authoritative.
    weak = build_timing_reference(
        method='acoustic_reference',
        sample_rate_hz=48000,
    )
    assert not timing_reference_supports_common_timing(weak)


def test_context_flat_fields_cannot_exceed_timing_authority(
    tmp_path: Path,
) -> None:
    revision, measurement_repository, quality_repository = _repositories(
        tmp_path
    )
    record, _ = _save_measurement(measurement_repository, revision, 'm-t')
    reference = build_timing_reference(
        method='external_sync',
        output_clock_identity='avr-wordclock',
        sample_rate_hz=48000,
        t0_convention='sweep_start',
        delay_corrections=(
            {'correction_kind': 'driver_latency', 'value_s': 0.002},
            {'correction_kind': 'acoustic_distance', 'value_s': 0.001},
        ),
    )
    quality_repository.save_timing_reference(reference)

    def context(**overrides):
        kwargs = dict(
            source_kind='native',
            subject_measurement_ids=(record.measurement_id,),
            timing_reference_id=reference.timing_reference_id,
            timing_reference_sha256=reference.timing_reference_sha256,
        )
        kwargs.update(overrides)
        return build_acquisition_context(**kwargs)

    # A clock identity the authority never declared is a contradiction.
    with pytest.raises(ValueError, match='clock_source'):
        quality_repository.save_acquisition_context(
            context(clock_source='some-other-clock')
        )
    # A divergent sample rate is a contradiction.
    with pytest.raises(ValueError, match='sample_rate'):
        quality_repository.save_acquisition_context(
            context(sample_rate_hz=96000)
        )
    # A correction the authority does not carry is a contradiction.
    with pytest.raises(ValueError, match='delay_correction_s'):
        quality_repository.save_acquisition_context(
            context(delay_correction_s=0.999)
        )
    # Attested values mirror verbatim.
    quality_repository.save_acquisition_context(
        context(
            clock_source='avr-wordclock',
            sample_rate_hz=48000,
            delay_correction_s=0.003,
        )
    )


def test_weak_timing_reference_cannot_authorize_pass_report(
    tmp_path: Path,
) -> None:
    revision, measurement_repository, quality_repository = _repositories(
        tmp_path
    )
    record, dataset = _save_measurement(
        measurement_repository, revision, 'm-weak-timing'
    )
    # A fully populated manual reference is still a manual record: it can
    # never witness common timing.
    reference = build_timing_reference(
        method='manual',
        input_clock_identity='operator-clock',
        output_clock_identity='operator-clock',
        sample_rate_hz=48000,
        delay_corrections=(
            {'correction_kind': 'driver_latency', 'value_s': 0.002},
        ),
    )
    quality_repository.save_timing_reference(reference)
    context = build_acquisition_context(
        source_kind='native',
        subject_measurement_ids=(record.measurement_id,),
        timing_reference_valid=True,
        timing_reference_id=reference.timing_reference_id,
        timing_reference_sha256=reference.timing_reference_sha256,
        clock_source='operator-clock',
        sample_rate_hz=48000,
        delay_correction_s=0.002,
    )
    quality_repository.save_acquisition_context(context)

    report = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(
            timing_reference_valid=True,
            timing_reference_id=reference.timing_reference_id,
            clock_source='operator-clock',
            sample_rate_hz=48000,
            delay_correction_s=0.002,
        ),
        profile=build_measurement_quality_profile(),
        acquisition_context=acquisition_context_binding(context),
    )
    # The evaluator derived PASS/ALLOWED from flat fields; the repository
    # must refuse to persist it against a weak authority.
    assert report.timing_reference.status == 'PASS'
    assert report.capability('common_timing').decision == 'ALLOWED'
    with pytest.raises(ValueError, match='common timing'):
        quality_repository.save_report(report)


def test_strong_timing_reference_authorizes_pass_report(
    tmp_path: Path,
) -> None:
    revision, measurement_repository, quality_repository = _repositories(
        tmp_path
    )
    record, dataset = _save_measurement(
        measurement_repository, revision, 'm-strong-timing'
    )
    reference = build_timing_reference(
        method='acoustic_reference',
        reference_channel='ch-1',
        input_clock_identity='capture-clock',
        sample_rate_hz=48000,
        t0_convention='acoustic_reference_signal',
        delay_corrections=(
            {'correction_kind': 'acoustic_distance', 'value_s': 0.001},
        ),
    )
    quality_repository.save_timing_reference(reference)
    context = build_acquisition_context(
        source_kind='native',
        subject_measurement_ids=(record.measurement_id,),
        timing_reference_valid=True,
        timing_reference_id=reference.timing_reference_id,
        timing_reference_sha256=reference.timing_reference_sha256,
        clock_source='ch-1',
        sample_rate_hz=48000,
        delay_correction_s=0.001,
    )
    quality_repository.save_acquisition_context(context)
    report = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(
            timing_reference_valid=True,
            timing_reference_id=reference.timing_reference_id,
            clock_source='ch-1',
            sample_rate_hz=48000,
            delay_correction_s=0.001,
        ),
        profile=build_measurement_quality_profile(),
        acquisition_context=acquisition_context_binding(context),
    )
    quality_repository.save_report(report)
    assert quality_repository.get_report(report.report_id) == report


# ---------------------------------------------------------------------------
# #831 field evidence: stored rows and target index must match the payload


def _evidence_repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'scene.sqlite3')
    saved = scene_repository.save(make_f1_scene(), parent_revision_id=None)
    return CadFieldEvidenceRepository(scene_repository), saved.revision


def _evidence(revision, **kwargs):
    return build_field_evidence(
        document_id=DOC,
        kind=kwargs.pop('kind', 'installation_photo'),
        targets=kwargs.pop(
            'targets',
            (
                EvidenceTarget(
                    kind='scene_entity',
                    revision_id=revision.revision_id,
                    entity_id='speaker-fl',
                    ref_sha256=revision.content_hash,
                ),
            ),
        ),
        provenance='installer phone upload',
        text=kwargs.pop('text', 'FL mounted'),
        captured_at_utc='2026-09-24T00:00:00+00:00',
        observer='installer-a',
        created_at_utc='2026-09-24T00:00:00+00:00',
        **kwargs,
    )


def _raw_update(path: Path, sql: str, params: tuple) -> None:
    from contextlib import closing

    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(sql, params)


def test_field_evidence_row_columns_checked_on_read(tmp_path: Path) -> None:
    repository, revision = _evidence_repositories(tmp_path)
    record = _evidence(revision)
    repository.save_evidence(record)
    assert repository.get_evidence(record.evidence_id) == record

    # A forged row column can never masquerade as the sealed payload.
    _raw_update(
        repository.path,
        'UPDATE cad_field_evidence SET document_id=? WHERE evidence_id=?',
        ('doc-other', record.evidence_id),
    )
    with pytest.raises(ValueError, match='row disagrees'):
        repository.get_evidence(record.evidence_id)


def test_field_evidence_target_index_must_match_payload(
    tmp_path: Path,
) -> None:
    repository, revision = _evidence_repositories(tmp_path)
    record = _evidence(revision)
    repository.save_evidence(record)

    # A forged index row cannot make the record claim a second target.
    _raw_update(
        repository.path,
        'INSERT INTO cad_field_evidence_targets '
        '(evidence_id, target_kind, revision_id, entity_id, ref_id) '
        'VALUES (?, ?, ?, ?, ?)',
        (
            record.evidence_id,
            'scene_entity',
            revision.revision_id,
            'speaker-fr',
            None,
        ),
    )
    with pytest.raises(ValueError, match='target index'):
        repository.get_evidence(record.evidence_id)
    with pytest.raises(ValueError, match='target index'):
        repository.evidence_for_entity(
            revision.revision_id, 'speaker-fl', document_id=DOC
        )


def test_field_evidence_missing_index_row_detected(tmp_path: Path) -> None:
    repository, revision = _evidence_repositories(tmp_path)
    record = _evidence(
        revision,
        targets=(
            EvidenceTarget(
                kind='scene_entity',
                revision_id=revision.revision_id,
                entity_id='speaker-fl',
                ref_sha256=revision.content_hash,
            ),
            EvidenceTarget(
                kind='scene_entity',
                revision_id=revision.revision_id,
                entity_id='speaker-c',
                ref_sha256=revision.content_hash,
            ),
        ),
    )
    repository.save_evidence(record)
    _raw_update(
        repository.path,
        'DELETE FROM cad_field_evidence_targets '
        'WHERE evidence_id=? AND entity_id=?',
        (record.evidence_id, 'speaker-c'),
    )
    with pytest.raises(ValueError, match='target index'):
        repository.get_evidence(record.evidence_id)


# ---------------------------------------------------------------------------
# #832 design briefs: single-head lineage, monotone timestamps, row checks


def _brief_repository(tmp_path: Path) -> CadDesignBriefRepository:
    return CadDesignBriefRepository(
        SceneRepository(tmp_path / 'scene.sqlite3')
    )


def _brief(document_id: str = DOC, **kwargs):
    kwargs.setdefault('created_at_utc', '2026-09-24T00:00:00+00:00')
    return build_design_brief(
        document_id=document_id,
        title='Theater goals',
        goal_refs=(
            BriefGoalRef(
                goal_id='goal-note',
                kind='free_text',
                requirement='informational',
                label='Quiet HVAC preferred',
            ),
        ),
        **kwargs,
    )


def test_brief_lineage_is_single_headed(tmp_path: Path) -> None:
    repository = _brief_repository(tmp_path)
    first = _brief()
    repository.save_brief(first)
    child = revise_design_brief(
        first, title='v2', created_at_utc='2026-09-24T01:00:00+00:00'
    )
    repository.save_brief(child)

    # A second child of the same parent is a fork — never persisted.
    fork = revise_design_brief(
        first, title='fork', created_at_utc='2026-09-24T02:00:00+00:00'
    )
    with pytest.raises(DesignBriefConflictError):
        repository.save_brief(fork)

    # latest_brief is the lineage head, not the newest timestamped row.
    assert repository.latest_brief(DOC) == child


def test_brief_revision_cannot_predate_parent(tmp_path: Path) -> None:
    repository = _brief_repository(tmp_path)
    first = _brief()
    repository.save_brief(first)
    backdated = revise_design_brief(
        first, created_at_utc='2026-09-23T00:00:00+00:00'
    )
    with pytest.raises(ValueError, match='cannot predate'):
        repository.save_brief(backdated)


def test_brief_created_at_must_be_tz_aware() -> None:
    with pytest.raises(ValueError, match='timezone-aware'):
        _brief(created_at_utc='2026-09-24T00:00:00')


def test_brief_row_columns_checked_on_read(tmp_path: Path) -> None:
    repository = _brief_repository(tmp_path)
    first = _brief()
    repository.save_brief(first)
    _raw_update(
        repository.path,
        'UPDATE cad_design_briefs SET document_id=? WHERE brief_id=?',
        ('doc-other', first.brief_id),
    )
    with pytest.raises(ValueError, match='row disagrees'):
        repository.get_brief(first.brief_id)


# ---------------------------------------------------------------------------
# #872 commissioning runs: one observation per check, honest deviations


def _commissioning(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'scene.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    decision_repository = CadDesignDecisionRepository(scene_repository)
    repository = CadCommissioningRepository(
        scene_repository,
        design_decision_repository=decision_repository,
    )
    profile = build_tolerance_profile(
        document_id=DOC,
        name='acceptance',
        version='v1',
        specs=(
            ToleranceSpec(
                key='speaker-position-m',
                operator='abs_error',
                target=3.0,
                limit=0.05,
                unit='m',
            ),
        ),
        created_at_utc='2026-09-24T00:00:00+00:00',
    )
    repository.save_tolerance_profile(profile)
    plan = build_commissioning_plan(
        document_id=DOC,
        scene_revision=revision,
        tolerance_profile=profile,
        created_at_utc='2026-09-24T00:05:00+00:00',
        checks=(
            CommissioningCheck(
                check_id='check-fl-x',
                subject_kind='position',
                subject=CheckSubject(
                    scene_entity_id='speaker-fl', property='position.x'
                ),
                tolerance_key='speaker-position-m',
            ),
            CommissioningCheck(
                check_id='check-fr-x',
                subject_kind='position',
                subject=CheckSubject(
                    scene_entity_id='speaker-fr', property='position.x'
                ),
                tolerance_key='speaker-position-m',
            ),
        ),
    )
    repository.save_plan(plan)
    return repository, decision_repository, plan, profile


def _observation(check_id: str, **kwargs) -> CommissioningObservation:
    return CommissioningObservation(
        observation_id=kwargs.pop('observation_id', f'obs-{check_id}'),
        check_id=check_id,
        value=kwargs.pop('value', 3.02),
        unit='m',
        uncertainty=0.01,
        **kwargs,
    )


def _deviation(check_id: str, **kwargs) -> AcceptedDeviation:
    kwargs.setdefault('decided_at_utc', '2026-09-24T00:30:00+00:00')
    return AcceptedDeviation(
        deviation_id=kwargs.pop('deviation_id', f'dev-{check_id}'),
        check_id=check_id,
        rationale='within installer tolerance in practice',
        approved_by='installer-a',
        **kwargs,
    )


def test_run_rejects_competing_observations(tmp_path: Path) -> None:
    _, _, plan, profile = _commissioning(tmp_path)
    with pytest.raises(ValueError, match='two observations'):
        build_commissioning_run(
            plan=plan,
            tolerance_profile=profile,
            observations=(
                _observation('check-fl-x', observation_id='obs-a'),
                _observation('check-fl-x', observation_id='obs-b'),
            ),
            created_at_utc='2026-09-24T00:10:00+00:00',
        )


def test_run_rejects_duplicate_ids_and_extra_deviations(
    tmp_path: Path,
) -> None:
    _, _, plan, profile = _commissioning(tmp_path)
    # The same observation id twice on different checks is also a forgery:
    # observations are immutable evidence items, never aliases.
    with pytest.raises(ValueError, match='unique ids'):
        build_commissioning_run(
            plan=plan,
            tolerance_profile=profile,
            observations=(
                _observation('check-fl-x', observation_id='obs-same'),
                CommissioningObservation(
                    observation_id='obs-same',
                    check_id='check-fr-x',
                    value=3.03,
                    unit='m',
                    uncertainty=0.01,
                ),
            ),
            created_at_utc='2026-09-24T00:10:00+00:00',
        )
    with pytest.raises(ValueError, match='one deviation per check'):
        build_commissioning_run(
            plan=plan,
            tolerance_profile=profile,
            observations=(
                _observation('check-fl-x', value=4.0),
            ),
            accepted_deviations=(
                _deviation('check-fl-x', deviation_id='d1'),
                _deviation('check-fl-x', deviation_id='d2'),
            ),
            created_at_utc='2026-09-24T00:10:00+00:00',
        )


def test_deviation_never_predates_its_evidence(tmp_path: Path) -> None:
    _, _, plan, profile = _commissioning(tmp_path)
    with pytest.raises(ValueError, match='before the observation'):
        build_commissioning_run(
            plan=plan,
            tolerance_profile=profile,
            observations=(
                _observation(
                    'check-fl-x',
                    value=4.0,
                    observed_at_utc='2026-09-24T00:45:00+00:00',
                ),
            ),
            accepted_deviations=(_deviation('check-fl-x'),),
            created_at_utc='2026-09-24T01:00:00+00:00',
        )
    with pytest.raises(ValueError, match='after the run was created'):
        build_commissioning_run(
            plan=plan,
            tolerance_profile=profile,
            observations=(
                _observation('check-fl-x', value=4.0),
            ),
            accepted_deviations=(
                _deviation(
                    'check-fl-x', decided_at_utc='2026-09-25T00:00:00+00:00'
                ),
            ),
            created_at_utc='2026-09-24T01:00:00+00:00',
        )


def test_deviation_decision_ref_must_be_design_decision() -> None:
    with pytest.raises(ValueError, match='design_decision'):
        _deviation(
            'check-fl-x',
            decision_ref=AuthorityRef(
                kind='scene_revision',
                ref_id='rev-1',
                ref_sha256='0' * 64,
            ),
        )


def test_run_save_resolves_deviation_decision_ref(tmp_path: Path) -> None:
    repository, decision_repository, plan, profile = _commissioning(tmp_path)
    decision = build_decision_record(
        document_id=DOC,
        title='Accept FL position deviation',
        decision_scope='commissioning_acceptance',
        lifecycle_intent='accept_commissioning_limitation',
        selected_ref=DecisionAuthorityRef(
            kind='other', ref_id='site-note-7', label='installer note'
        ),
        considered_refs=(
            DecisionAuthorityRef(kind='other', ref_id='site-note-7'),
        ),
        created_at_utc='2026-09-24T00:20:00+00:00',
    )
    decision_repository.save_decision(decision)

    run = build_commissioning_run(
        plan=plan,
        tolerance_profile=profile,
        observations=(_observation('check-fl-x', value=4.0),),
        accepted_deviations=(
            _deviation(
                'check-fl-x',
                decision_ref=AuthorityRef(
                    kind='design_decision',
                    ref_id=decision.decision_id,
                    ref_sha256=decision.decision_sha256,
                ),
            ),
        ),
        created_at_utc='2026-09-24T01:00:00+00:00',
    )
    repository.save_run(run)
    assert repository.get_run(run.run_id) == run

    # A forged semantic hash on a real decision id is rejected.
    forged = build_commissioning_run(
        plan=plan,
        tolerance_profile=profile,
        observations=(
            _observation('check-fl-x', value=4.0, observation_id='obs-2'),
        ),
        accepted_deviations=(
            _deviation(
                'check-fl-x',
                deviation_id='dev-2',
                decision_ref=AuthorityRef(
                    kind='design_decision',
                    ref_id=decision.decision_id,
                    ref_sha256='0' * 64,
                ),
            ),
        ),
        created_at_utc='2026-09-24T01:10:00+00:00',
    )
    with pytest.raises(ValueError):
        repository.save_run(forged)


# ---------------------------------------------------------------------------
# #874 measurement stimulus authorities


def test_excitation_asset_save_read_verified(tmp_path: Path) -> None:
    _, _, quality_repository = _repositories(tmp_path)
    content = b'ess-sweep-20-20k-48k.wav bytes'
    asset = build_excitation_asset(
        document_id=DOC,
        filename='ess-sweep.wav',
        sha256=sha256(content).hexdigest(),
        byte_length=len(content),
        format='wav',
        duration_s=15.0,
        sample_rate_hz=48000.0,
        channel_count=1,
    )
    quality_repository.save_excitation_asset(asset, content)
    assert (
        quality_repository.get_excitation_asset(asset.excitation_asset_id)
        == asset
    )

    # Declared metadata that disagrees with the bytes is refused.
    wrong = build_excitation_asset(
        document_id=DOC,
        filename='other.wav',
        sha256='0' * 64,
        byte_length=len(content),
    )
    with pytest.raises(ValueError, match='sha256'):
        quality_repository.save_excitation_asset(wrong, content)
    bad_length = build_excitation_asset(
        document_id=DOC,
        filename='len.wav',
        sha256=sha256(content).hexdigest(),
        byte_length=len(content) + 1,
    )
    with pytest.raises(ValueError, match='byte_length'):
        quality_repository.save_excitation_asset(bad_length, content)
    wrong_path = build_excitation_asset(
        document_id=DOC,
        filename='path.wav',
        sha256=sha256(content).hexdigest(),
        byte_length=len(content),
        relative_path='uploads/sweep.wav',
    )
    with pytest.raises(ValueError, match='relative_path'):
        quality_repository.save_excitation_asset(wrong_path, content)


def test_stimulus_profile_resolves_bound_authorities(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(
        tmp_path
    )
    record, dataset = _save_measurement(
        measurement_repository, revision, 'm-stim'
    )
    content = b'sweep-bytes'
    asset = build_excitation_asset(
        document_id=DOC,
        filename='sweep.wav',
        sha256=sha256(content).hexdigest(),
        byte_length=len(content),
        format='wav',
    )
    quality_repository.save_excitation_asset(asset, content)

    profile = build_stimulus_profile(
        document_id=DOC,
        stimulus_kind='log_sweep',
        excitation_asset=asset,
        measurement_dataset_sha256=dataset.dataset_sha256,
        intent='measurement',
        level_dbfs=-12.0,
        duration_s=15.0,
        sample_rate_hz=48000.0,
    )
    quality_repository.save_stimulus_profile(profile)
    assert (
        quality_repository.get_stimulus_profile(profile.stimulus_profile_id)
        == profile
    )
    assert profile in quality_repository.list_stimulus_profiles(DOC)

    # Unknown asset ids, foreign documents and mismatched seals fail closed.
    ghost = build_stimulus_profile(
        document_id=DOC,
        stimulus_kind='log_sweep',
        excitation_asset=asset.model_copy(
            update={'excitation_asset_id': 'asset-ghost'}
        ),
    )
    with pytest.raises(ValueError, match='unknown excitation asset'):
        quality_repository.save_stimulus_profile(ghost)

    other_sha = build_excitation_asset(
        document_id=DOC,
        filename='sweep2.wav',
        sha256=sha256(b'other-bytes').hexdigest(),
        byte_length=11,
    )
    forged = build_stimulus_profile(
        document_id=DOC,
        stimulus_kind='log_sweep',
        excitation_asset=other_sha,
    )
    # The binding claims asset's id but a different content hash.
    forged = forged.model_copy(
        update={
            'excitation_asset': forged.excitation_asset.model_copy(
                update={
                    'excitation_asset_id': asset.excitation_asset_id,
                }
            )
        }
    )
    with pytest.raises(ValueError, match='hash mismatch'):
        quality_repository.save_stimulus_profile(forged)

    with pytest.raises(ValueError, match='unknown measurement dataset'):
        quality_repository.save_stimulus_profile(
            build_stimulus_profile(
                document_id=DOC,
                stimulus_kind='log_sweep',
                measurement_dataset_sha256='0' * 64,
            )
        )


def test_digital_stimulus_never_claims_spl() -> None:
    with pytest.raises(ValueError, match='SPL'):
        build_stimulus_profile(
            document_id=DOC,
            stimulus_kind='log_sweep',
            level_db_spl=75.0,
        )
    # Acoustic-capable kinds may carry SPL honestly.
    profile = build_stimulus_profile(
        document_id=DOC,
        stimulus_kind='program_material',
        level_db_spl=75.0,
    )
    assert profile.level_db_spl == 75.0


def test_stimulus_comparison_semantics() -> None:
    base = build_stimulus_profile(
        document_id=DOC,
        stimulus_kind='pink_noise',
        measurement_dataset_sha256='a' * 64,
        level_dbfs=-20.0,
        duration_s=60.0,
    )
    same = build_stimulus_profile(
        document_id=DOC,
        stimulus_kind='pink_noise',
        measurement_dataset_sha256='a' * 64,
        level_dbfs=-20.0,
        duration_s=60.0,
    )
    assert stimulus_profiles_comparable(base, same)
    assert stimulus_comparison(base, same) == 'MATCH'

    different_level = build_stimulus_profile(
        document_id=DOC,
        stimulus_kind='pink_noise',
        measurement_dataset_sha256='a' * 64,
        level_dbfs=-12.0,
        duration_s=60.0,
    )
    assert stimulus_comparison(base, different_level) == 'CONDITION_MISMATCH'

    partial = build_stimulus_profile(
        document_id=DOC,
        stimulus_kind='pink_noise',
        measurement_dataset_sha256='a' * 64,
        level_dbfs=-20.0,
    )
    assert stimulus_comparison(base, partial) == 'UNKNOWN'

    # No shared canonical reference: never comparable, never a mismatch.
    unrelated = build_stimulus_profile(
        document_id=DOC,
        stimulus_kind='pink_noise',
        measurement_dataset_sha256='b' * 64,
    )
    assert stimulus_comparison(base, unrelated) == 'UNKNOWN'

    # External kinds can never prove a shared reference at all.
    ext_a = build_stimulus_profile(
        document_id=DOC, stimulus_kind='external', duration_s=30.0
    )
    ext_b = build_stimulus_profile(
        document_id=DOC, stimulus_kind='external', duration_s=30.0
    )
    assert not stimulus_profiles_comparable(ext_a, ext_b)
    assert stimulus_comparison(ext_a, ext_b) == 'UNKNOWN'
