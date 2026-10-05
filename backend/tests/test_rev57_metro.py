"""REV57-METRO regression tests — #609 measurement timebase / clock
authority, #610 reproducible evidence bundle / integrity manifest,
#611 measurement-instrument calibration lifecycle."""

from __future__ import annotations

import sqlite3

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene

from htdt.cad_timebase_authority import (
    CadChannelSyncSpec,
    CadClockDriftEstimate,
    CadDriftCompensation,
    CadTimingReferenceEvidence,
    build_clock_domain,
    build_measurement_timebase,
    evaluate_timebase_capability,
    timebase_binding,
)
from htdt.cad_timebase_authority_repository import (
    CadTimebaseAuthorityRepository,
    TimebaseAuthorityIntegrityError,
)
from htdt.cad_evidence_bundle import (
    build_artifact_entry,
    build_attestation,
    build_derivation_edge,
    build_evidence_bundle,
    bundle_binding,
    compute_manifest_root,
    finalize_evidence_bundle,
    validate_evidence_bundle,
)
from htdt.cad_evidence_bundle_repository import (
    CadEvidenceBundleRepository,
    EvidenceBundleIntegrityError,
)
from htdt.cad_calibration_lifecycle import (
    CadCorrectionFileBinding,
    build_calibration_event,
    build_instrument_instance,
    build_interval_policy,
    build_service_event,
    build_verification_check,
    evaluate_instrument_fitness,
    instrument_binding,
    review_out_of_tolerance,
)
from htdt.cad_calibration_lifecycle_repository import (
    CadCalibrationLifecycleRepository,
    CalibrationLifecycleIntegrityError,
)


DOC = 'doc-rev57-metro'
T0 = '2026-10-05T00:00:00+00:00'
T1 = '2026-10-05T01:00:00+00:00'
T2 = '2026-10-05T02:00:00+00:00'
SHA_A = 'a' * 64
SHA_B = 'b' * 64
SHA_C = 'c' * 64
SHA_D = 'd' * 64


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene(doc_id), parent_revision_id=None)
    return scene_repository


def _synced_timebase(**overrides):
    """A well-evidenced synchronous capture: one hardware clock domain
    shared by playback and capture, wired loopback t=0 reference."""
    playback = overrides.pop(
        'playback_domain',
        build_clock_domain(
            document_id=DOC,
            domain_kind='playback_clock',
            device_identity='iface-1:out',
            nominal_sample_rate_hz=48000.0,
            common_clock_group='iface-1',
            declared_at_utc=T0,
        ),
    )
    capture = overrides.pop(
        'capture_domain',
        build_clock_domain(
            document_id=DOC,
            domain_kind='capture_clock',
            device_identity='iface-1:in',
            nominal_sample_rate_hz=48000.0,
            common_clock_group='iface-1',
            declared_at_utc=T0,
        ),
    )
    reference = overrides.pop(
        'timing_reference',
        CadTimingReferenceEvidence(
            kind='wired_loopback_reference',
            reference_channel='ref-loopback',
            result='valid',
        ),
    )
    kwargs = dict(
        document_id=DOC,
        topology='common_hardware_clock',
        topology_evidence='device_specification',
        playback_domain=playback,
        capture_domain=capture,
        timing_reference=reference,
        capture_duration_s=1.0,
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_measurement_timebase(**kwargs), capture


def _instrument(**overrides):
    kwargs = dict(
        document_id=DOC,
        category='measurement_microphone',
        serial_or_instance_id='UMIK-1 SN7012345',
        manufacturer='miniDSP',
        model='UMIK-1',
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_instrument_instance(**kwargs)


def _accredited_calibration(instrument, **overrides):
    kwargs = dict(
        document_id=DOC,
        instrument_ref=instrument,
        event_kind='laboratory_calibration',
        performed_at_utc=T0,
        provider_or_lab='Accredited Lab KK',
        certificate_id='CAL-2026-001',
        traceability_class='accredited_traceable',
        valid_until_utc='2027-10-05T00:00:00+00:00',
        recorded_at_utc=T1,
    )
    kwargs.update(overrides)
    return build_calibration_event(**kwargs)


# ===========================================================================
# #609 — timebase / clock authority (fixtures CLK10–CLK80)
# ===========================================================================

def test_clk10_common_clock_wired_loopback_carries_phase_and_delay() -> None:
    timebase, capture = _synced_timebase()
    assessment = evaluate_timebase_capability(
        document_id=DOC,
        timebase=timebase,
        capture_domain=capture,
        evaluated_at_utc=T1,
    )
    assert assessment.capability_state('magnitude_valid') == 'valid'
    assert assessment.capability_state('absolute_phase_valid') == 'valid'
    assert assessment.capability_state('absolute_delay_valid') == 'valid'
    assert assessment.capability_state('relative_delay_valid') == 'valid'
    assert (
        assessment.capability_state('complex_transfer_eligible')
        == 'valid'
    )
    # Single-input synchronous capture keeps complex values.
    assert (
        assessment.capability_state('vector_averaging_eligible')
        == 'valid'
    )
    assert assessment.drift_material in (None, False)


def test_clk20_independent_async_keeps_magnitude_but_no_phase() -> None:
    playback = build_clock_domain(
        document_id=DOC,
        domain_kind='playback_clock',
        device_identity='usb-dac',
        nominal_sample_rate_hz=48000.0,
        declared_at_utc=T0,
    )
    capture = build_clock_domain(
        document_id=DOC,
        domain_kind='capture_clock',
        device_identity='usb-adc',
        nominal_sample_rate_hz=48000.0,
        declared_at_utc=T0,
    )
    timebase = build_measurement_timebase(
        document_id=DOC,
        topology='independent_asynchronous',
        topology_evidence='device_specification',
        playback_domain=playback,
        capture_domain=capture,
        timing_reference=CadTimingReferenceEvidence(
            kind='no_timing_reference',
        ),
        capture_duration_s=1.0,
        declared_at_utc=T0,
    )
    assessment = evaluate_timebase_capability(
        document_id=DOC,
        timebase=timebase,
        capture_domain=capture,
        evaluated_at_utc=T1,
    )
    # Asynchronous captures still carry magnitude/statistical value.
    assert assessment.capability_state('magnitude_valid') == 'valid'
    assert (
        assessment.capability_state('absolute_phase_valid') == 'invalid'
    )
    assert assessment.capability_state('absolute_delay_valid') == 'invalid'
    assert assessment.capability_state('relative_delay_valid') == 'invalid'
    assert (
        assessment.capability_state('complex_transfer_eligible')
        == 'invalid'
    )


def test_clk30_shared_interface_unconfirmed_is_unknown_not_valid() -> None:
    # Same USB interface label ≠ confirmed shared sample clock —
    # phase/delay claims stay unknown rather than assumed valid.
    capture = build_clock_domain(
        document_id=DOC,
        domain_kind='capture_clock',
        device_identity='iface-usb',
        nominal_sample_rate_hz=48000.0,
        declared_at_utc=T0,
    )
    timebase = build_measurement_timebase(
        document_id=DOC,
        topology='shared_interface_unconfirmed',
        topology_evidence='assumed',
        capture_domain=capture,
        capture_duration_s=1.0,
        declared_at_utc=T0,
    )
    assessment = evaluate_timebase_capability(
        document_id=DOC,
        timebase=timebase,
        capture_domain=capture,
        evaluated_at_utc=T1,
    )
    assert assessment.capability_state('magnitude_valid') == 'valid'
    assert (
        assessment.capability_state('absolute_phase_valid') == 'unknown'
    )
    assert assessment.capability_state('absolute_delay_valid') == 'unknown'
    assert (
        assessment.capability_state('complex_transfer_eligible')
        == 'unknown'
    )


def test_clk35_strong_topology_rejects_weak_basis() -> None:
    with pytest.raises(ValueError, match='evidence basis'):
        build_measurement_timebase(
            document_id=DOC,
            topology='common_hardware_clock',
            topology_evidence='assumed',
            capture_duration_s=1.0,
            declared_at_utc=T0,
        )


def test_clk35_matching_nominal_rates_are_not_sync_evidence() -> None:
    # Two devices at the same nominal 48 kHz are still asynchronous —
    # nominal rate equality never upgrades a topology.
    playback = build_clock_domain(
        document_id=DOC,
        domain_kind='playback_clock',
        nominal_sample_rate_hz=48000.0,
        declared_at_utc=T0,
    )
    capture = build_clock_domain(
        document_id=DOC,
        domain_kind='capture_clock',
        nominal_sample_rate_hz=48000.0,
        declared_at_utc=T0,
    )
    timebase = build_measurement_timebase(
        document_id=DOC,
        topology='independent_asynchronous',
        topology_evidence='assumed',
        playback_domain=playback,
        capture_domain=capture,
        declared_at_utc=T0,
    )
    assessment = evaluate_timebase_capability(
        document_id=DOC,
        timebase=timebase,
        capture_domain=capture,
        evaluated_at_utc=T1,
    )
    assert (
        assessment.capability_state('absolute_phase_valid') == 'invalid'
    )


def test_clk40_material_drift_downgrades_timing_claims() -> None:
    # 200 ppm over a 30 s sweep accumulates ~6 ms — material against a
    # 1 ms requested resolution, so absolute/phase claims are limited.
    timebase, capture = _synced_timebase(
        capture_duration_s=30.0,
        requested_timing_resolution_s=0.001,
        drift_estimate=CadClockDriftEstimate(
            method='sweep_trajectory_estimation',
            algorithm_identity='trajectory-fit/1.0',
            drift_ppm=200.0,
        ),
    )
    assessment = evaluate_timebase_capability(
        document_id=DOC,
        timebase=timebase,
        capture_domain=capture,
        evaluated_at_utc=T1,
    )
    assert assessment.drift_material is True
    assert (
        assessment.capability_state('absolute_phase_valid') == 'limited'
    )
    assert assessment.capability_state('absolute_delay_valid') == 'limited'
    assert (
        assessment.capability_state('complex_transfer_eligible')
        == 'limited'
    )
    assert assessment.timing_uncertainty_s == pytest.approx(0.006)


def test_clk50_compensated_drift_keeps_residual_only_uncertainty() -> None:
    compensation = CadDriftCompensation(
        raw_capture_ref=AuthorityRef(
            kind='measurement_dataset',
            ref_id='ds-1',
            ref_sha256=SHA_A,
        ),
        estimated_clock_ratio=1.0002,
        resampler_identity='resampler/1.2',
        output_artifact_sha256=SHA_B,
    )
    timebase, capture = _synced_timebase(
        capture_duration_s=30.0,
        requested_timing_resolution_s=0.001,
        drift_estimate=CadClockDriftEstimate(
            method='sweep_trajectory_estimation',
            algorithm_identity='trajectory-fit/1.0',
            drift_ppm=200.0,
            residual_timing_error_s=0.0001,
        ),
        drift_compensation=compensation,
    )
    assessment = evaluate_timebase_capability(
        document_id=DOC,
        timebase=timebase,
        capture_domain=capture,
        evaluated_at_utc=T1,
    )
    # Only the declared residual counts once compensation is pinned.
    assert assessment.timing_uncertainty_s == pytest.approx(0.0001)
    assert assessment.drift_material is False
    assert assessment.capability_state('absolute_delay_valid') == 'valid'


def test_clk55_compensation_needs_estimate_or_transform_pin() -> None:
    compensation = CadDriftCompensation(
        raw_capture_ref=AuthorityRef(
            kind='measurement_dataset',
            ref_id='ds-1',
            ref_sha256=SHA_A,
        ),
        estimated_clock_ratio=1.0002,
        resampler_identity='resampler/1.2',
        output_artifact_sha256=SHA_B,
    )
    with pytest.raises(ValueError, match='invisible fix'):
        _synced_timebase(drift_compensation=compensation)


def test_clk60_sequential_magnitude_only_kills_phase_claims() -> None:
    timebase, capture = _synced_timebase(
        sequential_anchor='magnitude_only',
    )
    assessment = evaluate_timebase_capability(
        document_id=DOC,
        timebase=timebase,
        capture_domain=capture,
        evaluated_at_utc=T1,
    )
    assert assessment.capability_state('magnitude_valid') == 'valid'
    assert (
        assessment.capability_state('absolute_phase_valid') == 'invalid'
    )
    assert assessment.capability_state('absolute_delay_valid') == 'invalid'
    assert (
        assessment.capability_state('vector_averaging_eligible')
        == 'invalid'
    )


def test_clk70_file_playback_requires_stimulus_pin() -> None:
    capture = build_clock_domain(
        document_id=DOC,
        domain_kind='capture_clock',
        device_identity='iface-usb',
        declared_at_utc=T0,
    )
    with pytest.raises(ValueError, match='stimulus'):
        build_measurement_timebase(
            document_id=DOC,
            topology='file_playback_external_device',
            capture_domain=capture,
            declared_at_utc=T0,
        )


def test_clk75_independent_adc_channels_cannot_claim_phase() -> None:
    channels = tuple(
        build_clock_domain(
            document_id=DOC,
            domain_kind='input_channel_clock_domain',
            device_identity=f'mic-{index}',
            declared_at_utc=T0,
        )
        for index in range(2)
    )
    timebase = build_measurement_timebase(
        document_id=DOC,
        topology='common_hardware_clock',
        topology_evidence='measured',
        channel_domains=channels,
        channel_sync=CadChannelSyncSpec(
            channel_count=2,
            same_adc_clock='no',
        ),
        capture_duration_s=1.0,
        declared_at_utc=T0,
    )
    assessment = evaluate_timebase_capability(
        document_id=DOC,
        timebase=timebase,
        evaluated_at_utc=T1,
    )
    assert (
        assessment.capability_state('inter_channel_phase_valid')
        == 'invalid'
    )
    assert (
        assessment.capability_state('vector_averaging_eligible')
        == 'invalid'
    )


def test_clk80_no_clock_evidence_is_honestly_unknown() -> None:
    timebase = build_measurement_timebase(
        document_id=DOC,
        topology='unknown',
        topology_evidence='unknown',
        declared_at_utc=T0,
    )
    assessment = evaluate_timebase_capability(
        document_id=DOC,
        timebase=timebase,
        evaluated_at_utc=T1,
    )
    assert assessment.capability_state('magnitude_valid') == 'unknown'
    assert (
        assessment.capability_state('absolute_phase_valid') == 'unknown'
    )
    assert assessment.reasons


def test_timebase_domain_refs_must_pin_sha() -> None:
    loose = AuthorityRef(
        kind='clock_domain', ref_id='clkdom-x', ref_sha256=None,
    )
    with pytest.raises(ValueError, match='sha256'):
        build_measurement_timebase(
            document_id=DOC,
            topology='independent_asynchronous',
            topology_evidence='assumed',
            capture_domain=loose,
            declared_at_utc=T0,
        )


def test_timebase_repository_round_trip_and_append_only(tmp_path) -> None:
    repo = CadTimebaseAuthorityRepository(_scene_repo(tmp_path))
    domain = build_clock_domain(
        document_id=DOC,
        domain_kind='capture_clock',
        device_identity='iface-1:in',
        nominal_sample_rate_hz=48000.0,
        declared_at_utc=T0,
    )
    timebase, capture = _synced_timebase()
    assessment = evaluate_timebase_capability(
        document_id=DOC,
        timebase=timebase,
        capture_domain=capture,
        evaluated_at_utc=T1,
    )
    repo.save_clock_domain(domain)
    repo.save_timebase(timebase)
    repo.save_assessment(assessment)
    # Idempotent re-save.
    repo.save_clock_domain(domain)
    repo.save_timebase(timebase)
    repo.save_assessment(assessment)

    assert repo.get_clock_domain(domain.clock_domain_id) == domain
    assert repo.get_timebase(timebase.timebase_id) == timebase
    assert repo.get_assessment(assessment.assessment_id) == assessment
    assert len(repo.list_clock_domains(DOC)) == 1
    assert len(repo.list_timebases(DOC)) == 1
    assert len(repo.list_assessments(DOC)) == 1


def test_timebase_repository_integrity_fails_closed(tmp_path) -> None:
    repo = CadTimebaseAuthorityRepository(_scene_repo(tmp_path))
    domain = build_clock_domain(
        document_id=DOC,
        domain_kind='capture_clock',
        declared_at_utc=T0,
    )
    repo.save_clock_domain(domain)
    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            'UPDATE cad_timebase_clock_domains '
            'SET nominal_sample_rate_hz=44100.0 WHERE clock_domain_id=?',
            (domain.clock_domain_id,),
        )
    with pytest.raises(TimebaseAuthorityIntegrityError):
        repo.get_clock_domain(domain.clock_domain_id)


# ===========================================================================
# #610 — evidence bundle / integrity manifest (fixtures EVB10–EVB80)
# ===========================================================================

def _entry(
    bundle_id: str,
    role: str,
    *,
    inclusion: str = 'embedded',
    artifact_class: str = 'raw_acquired',
    digest: str | None = SHA_A,
    package_path: str | None = 'raw/measurement.mdat',
    required: bool = True,
    **overrides,
):
    kwargs = dict(
        document_id=DOC,
        bundle_id=bundle_id,
        logical_role=role,
        artifact_class=artifact_class,
        inclusion=inclusion,
        package_path=package_path,
        digest_algorithm='sha256' if digest else None,
        digest=digest,
        required=required,
        rights_sensitivity='open',
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_artifact_entry(**kwargs)


def _commissioning_entries(bundle_id: str) -> tuple:
    return (
        _entry(
            bundle_id, 'measurement_raw',
            package_path='raw/measurement.mdat',
            digest=SHA_A,
        ),
        _entry(
            bundle_id, 'measurement_derived',
            artifact_class='derived_numeric',
            package_path='derived/ir.wav',
            digest=SHA_B,
        ),
        _entry(
            bundle_id, 'device_configuration',
            artifact_class='source_external',
            package_path='config/device.json',
            digest=SHA_C,
        ),
        _entry(
            bundle_id, 'standards_profile',
            artifact_class='decision_verdict',
            package_path='standards/profile.json',
            digest=SHA_D,
        ),
        _entry(
            bundle_id, 'report',
            artifact_class='presentation_report',
            package_path='report/report.pdf',
            digest='e' * 64,
        ),
    )


def _draft(**overrides):
    kwargs = dict(
        document_id=DOC,
        purpose='commissioning',
        producer_software='htdt',
        producer_version='0.9.0',
        status='draft',
        completeness_profile='commissioning_minimum',
        created_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_evidence_bundle(**kwargs)


def _sealed_package(
    *,
    with_report: bool = True,
    extra_entries=(),
    edges=(),
    draft_overrides=None,
):
    """Draft → draft-bound members → sealed finalized bundle.

    Entries/edges bind to the draft id; the finalized record supersedes
    the draft so validation resolves membership without a circular
    hash dependency.
    """
    draft = _draft(**(draft_overrides or {}))
    entries = _commissioning_entries(draft.bundle_id)
    if not with_report:
        entries = tuple(
            entry for entry in entries if entry.logical_role != 'report'
        )
    extras = (
        extra_entries(draft.bundle_id)
        if callable(extra_entries)
        else extra_entries
    )
    entries = tuple(entries) + tuple(extras)
    bundle = finalize_evidence_bundle(
        document_id=DOC,
        draft=draft,
        entries=entries,
        edges=edges,
        finalized_at_utc=T1,
    )
    return bundle, entries


def test_evb10_complete_bundle_validates() -> None:
    bundle, entries = _sealed_package()
    verdict = validate_evidence_bundle(
        document_id=DOC,
        bundle=bundle,
        entries=entries,
        digest_resolver=lambda entry: entry.digest,
        validated_at_utc=T2,
    )
    assert verdict.state == 'complete_valid'
    assert dict(verdict.checks)['digest_match'] == 'verified'


def test_evb20_tampered_payload_is_integrity_failure() -> None:
    bundle, entries = _sealed_package()
    resolver = lambda entry: (
        'f' * 64 if entry.logical_role == 'measurement_raw'
        else entry.digest
    )
    verdict = validate_evidence_bundle(
        document_id=DOC,
        bundle=bundle,
        entries=entries,
        digest_resolver=resolver,
        validated_at_utc=T2,
    )
    assert verdict.state == 'integrity_failure'
    assert dict(verdict.checks)['digest_match'] == 'failed'
    assert verdict.failed_digests


def test_evb30_missing_required_role_is_incomplete() -> None:
    bundle, entries = _sealed_package(with_report=False)
    verdict = validate_evidence_bundle(
        document_id=DOC,
        bundle=bundle,
        entries=entries,
        digest_resolver=lambda entry: entry.digest,
        validated_at_utc=T2,
    )
    assert verdict.state == 'incomplete'
    assert 'report' in verdict.missing_roles


def test_evb40_external_mutable_url_bounds_reproducibility() -> None:
    bundle, entries = _sealed_package(
        extra_entries=lambda bundle_id: (
            _entry(
                bundle_id, 'external_raw_archive',
                inclusion='external_mutable_url',
                artifact_class='raw_acquired',
                digest=None,
                package_path=None,
                record_uri='https://files.example.org/raw.tgz',
                captured_at_utc=T0,
                required=False,
            ),
        )
    )
    verdict = validate_evidence_bundle(
        document_id=DOC,
        bundle=bundle,
        entries=entries,
        digest_resolver=lambda entry: entry.digest,
        validated_at_utc=T2,
    )
    assert verdict.state == 'complete_but_external_dependencies'
    assert dict(verdict.checks)['external_dependencies'] == 'limited'


def test_evb40b_mutable_url_without_capture_time_rejected() -> None:
    with pytest.raises(ValueError, match='captured'):
        _entry(
            'bundle-x', 'external_raw_archive',
            inclusion='external_mutable_url',
            digest=None,
            package_path=None,
            record_uri='https://files.example.org/raw.tgz',
        )


def test_evb50_unverifiable_payload_is_unresolved_reference() -> None:
    bundle, entries = _sealed_package()
    verdict = validate_evidence_bundle(
        document_id=DOC,
        bundle=bundle,
        entries=entries,
        digest_resolver=None,
        validated_at_utc=T2,
    )
    assert verdict.state == 'unresolved_reference'
    assert dict(verdict.checks)['payload_presence'] == 'failed'


def test_evb60_derivation_chain_is_checked_against_manifest() -> None:
    draft = _draft()
    entries = _commissioning_entries(draft.bundle_id)
    edge = build_derivation_edge(
        document_id=DOC,
        bundle_id=draft.bundle_id,
        operation='deconvolution',
        software_identity='htdt-sweep/1.0',
        input_artifact_ids=(entries[0].artifact_id,),
        output_artifact_id=entries[1].artifact_id,
        declared_at_utc=T1,
    )
    bundle = finalize_evidence_bundle(
        document_id=DOC,
        draft=draft,
        entries=entries,
        edges=(edge,),
        finalized_at_utc=T1,
    )
    verdict = validate_evidence_bundle(
        document_id=DOC,
        bundle=bundle,
        entries=entries,
        edges=(edge,),
        digest_resolver=lambda entry: entry.digest,
        validated_at_utc=T2,
    )
    assert verdict.state == 'complete_valid'
    assert dict(verdict.checks)['derivation_integrity'] == 'verified'

    broken = build_derivation_edge(
        document_id=DOC,
        bundle_id=draft.bundle_id,
        operation='deconvolution',
        software_identity='htdt-sweep/1.0',
        input_artifact_ids=('evart-does-not-exist',),
        output_artifact_id=entries[1].artifact_id,
        declared_at_utc=T1,
    )
    bundle2 = finalize_evidence_bundle(
        document_id=DOC,
        draft=draft,
        entries=entries,
        edges=(broken,),
        finalized_at_utc=T1,
    )
    verdict2 = validate_evidence_bundle(
        document_id=DOC,
        bundle=bundle2,
        entries=entries,
        edges=(broken,),
        digest_resolver=lambda entry: entry.digest,
        validated_at_utc=T2,
    )
    assert dict(verdict2.checks)['derivation_integrity'] == 'failed'
    assert verdict2.state == 'integrity_failure'


def test_evb70_profile_mismatch_is_named_explicitly() -> None:
    # The bundle only carries commissioning roles but is validated
    # against the stricter simulation profile.
    bundle, entries = _sealed_package()
    verdict = validate_evidence_bundle(
        document_id=DOC,
        bundle=bundle,
        entries=entries,
        digest_resolver=lambda entry: entry.digest,
        profile='simulation_validation',
        validated_at_utc=T2,
    )
    assert verdict.state == 'profile_mismatch'


def test_evb80_draft_bundle_is_not_sealed_evidence() -> None:
    bundle = build_evidence_bundle(
        document_id=DOC,
        purpose='commissioning',
        producer_software='htdt',
        producer_version='0.9.0',
        status='draft',
        created_at_utc=T0,
    )
    verdict = validate_evidence_bundle(
        document_id=DOC,
        bundle=bundle,
        entries=(),
        validated_at_utc=T2,
    )
    assert verdict.state == 'integrity_failure'
    assert dict(verdict.checks)['status_finalized'] == 'failed'


def test_evidence_embedded_requires_digest_and_path() -> None:
    with pytest.raises(ValueError, match='digest'):
        _entry('b', 'measurement_raw', digest=None)
    with pytest.raises(ValueError, match='package_path'):
        _entry('b', 'measurement_raw', package_path=None)


def test_evidence_package_path_must_be_relative_posix() -> None:
    for bad in ('/abs/path.wav', 'C:\\x.wav', 'a\\b.wav', 'a/../b.wav'):
        with pytest.raises(ValueError, match='POSIX'):
            _entry('b', 'measurement_raw', package_path=bad)


def test_evidence_corrected_package_supersedes_append_only() -> None:
    # Tamper semantics: a corrected package is a NEW bundle that
    # supersedes the old — the original is never rewritten.
    first, _ = _sealed_package()
    corrected_draft = _draft()
    corrected_entries = _commissioning_entries(corrected_draft.bundle_id)
    corrected = build_evidence_bundle(
        document_id=DOC,
        purpose='commissioning',
        producer_software='htdt',
        producer_version='0.9.0',
        status='finalized',
        completeness_profile='commissioning_minimum',
        manifest_root_sha256=compute_manifest_root(
            [e.artifact_sha256 for e in corrected_entries], [],
        ),
        supersedes_bundle_ref=bundle_binding(first),
        created_at_utc=T0,
        finalized_at_utc=T2,
    )
    assert corrected.supersedes_bundle_ref is not None
    assert (
        corrected.supersedes_bundle_ref.ref_sha256
        == first.bundle_sha256
    )


def test_evidence_attestation_pins_the_bundle_sha() -> None:
    bundle, _ = _sealed_package()
    attestation = build_attestation(
        document_id=DOC,
        bundle_ref=bundle,
        signer_identity='reviewer-1',
        signer_key_ref='key-fp-1',
        signature_algorithm='ed25519',
        signature_value='deadbeef',
        role='qa_reviewer',
        signed_at_utc=T2,
    )
    assert attestation.bundle_ref.ref_sha256 == bundle.bundle_sha256


def test_evidence_repository_round_trip_and_integrity(tmp_path) -> None:
    repo = CadEvidenceBundleRepository(_scene_repo(tmp_path))
    draft = _draft()
    entries = _commissioning_entries(draft.bundle_id)
    edge = build_derivation_edge(
        document_id=DOC,
        bundle_id=draft.bundle_id,
        operation='deconvolution',
        software_identity='htdt-sweep/1.0',
        input_artifact_ids=(entries[0].artifact_id,),
        output_artifact_id=entries[1].artifact_id,
        declared_at_utc=T1,
    )
    bundle = finalize_evidence_bundle(
        document_id=DOC,
        draft=draft,
        entries=entries,
        edges=(edge,),
        finalized_at_utc=T1,
    )
    verdict = validate_evidence_bundle(
        document_id=DOC,
        bundle=bundle,
        entries=entries,
        edges=(edge,),
        digest_resolver=lambda entry: entry.digest,
        validated_at_utc=T2,
    )
    attestation = build_attestation(
        document_id=DOC,
        bundle_ref=bundle,
        signer_identity='reviewer-1',
        signature_algorithm='ed25519',
        signature_value='deadbeef',
        role='qa_reviewer',
        signed_at_utc=T2,
    )
    repo.save_bundle(bundle)
    for entry in entries:
        repo.save_artifact(entry)
    repo.save_edge(edge)
    repo.save_attestation(attestation)
    repo.save_verdict(verdict)
    # Idempotent re-saves.
    repo.save_bundle(bundle)
    repo.save_verdict(verdict)

    assert repo.get_bundle(bundle.bundle_id) == bundle
    assert repo.get_artifact(entries[0].artifact_id) == entries[0]
    assert repo.get_edge(edge.edge_id) == edge
    assert (
        repo.get_attestation(attestation.attestation_id) == attestation
    )
    assert repo.get_verdict(verdict.verdict_id) == verdict
    assert len(repo.list_bundles(DOC)) == 1
    # Members remain bound to the draft record the finalized bundle
    # supersedes.
    assert len(repo.list_artifacts(bundle_id=draft.bundle_id)) == 5

    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            'UPDATE cad_evidence_bundles SET status=? WHERE bundle_id=?',
            ('draft', bundle.bundle_id),
        )
    with pytest.raises(EvidenceBundleIntegrityError):
        repo.get_bundle(bundle.bundle_id)


# ===========================================================================
# #611 — instrument calibration lifecycle (fixtures CAL10–CAL80)
# ===========================================================================

def test_cal10_in_date_accredited_calibration_is_fit() -> None:
    instrument = _instrument()
    calibration = _accredited_calibration(instrument)
    assessment = evaluate_instrument_fitness(
        document_id=DOC,
        instrument=instrument,
        calibrations=(calibration,),
        at_utc=T1,
        evaluated_at_utc=T2,
    )
    assert assessment.state == 'fit_for_purpose'
    assert assessment.basis_calibration_ref is not None
    assert (
        assessment.basis_calibration_ref.ref_sha256
        == calibration.calibration_sha256
    )


def test_cal20_policy_overdue_is_policy_evidence_not_failure() -> None:
    instrument = _instrument()
    calibration = _accredited_calibration(
        instrument, valid_until_utc=None,
    )
    policy = build_interval_policy(
        document_id=DOC,
        instrument_ref=instrument,
        basis='manufacturer_recommended',
        nominal_interval_days=1,
        declared_at_utc=T0,
    )
    assessment = evaluate_instrument_fitness(
        document_id=DOC,
        instrument=instrument,
        calibrations=(calibration,),
        policies=(policy,),
        at_utc='2026-10-10T00:00:00+00:00',
        evaluated_at_utc='2026-10-10T01:00:00+00:00',
    )
    assert assessment.state == 'calibration_overdue_by_policy'


def test_cal20b_certificate_valid_until_overdue() -> None:
    instrument = _instrument()
    calibration = _accredited_calibration(
        instrument,
        valid_until_utc='2026-10-05T00:30:00+00:00',
    )
    assessment = evaluate_instrument_fitness(
        document_id=DOC,
        instrument=instrument,
        calibrations=(calibration,),
        at_utc=T1,
        evaluated_at_utc=T2,
    )
    assert assessment.state == 'calibration_overdue_by_policy'


def test_cal30_field_reference_is_not_lab_calibration() -> None:
    instrument = _instrument()
    field_reference = build_calibration_event(
        document_id=DOC,
        instrument_ref=instrument,
        event_kind='field_reference',
        performed_at_utc=T0,
        provider_or_lab='field-tech',
        traceability_class='field_referenced',
        recorded_at_utc=T0,
    )
    assessment = evaluate_instrument_fitness(
        document_id=DOC,
        instrument=instrument,
        calibrations=(field_reference,),
        at_utc=T1,
        evaluated_at_utc=T2,
    )
    assert assessment.state == 'fit_with_limitations'
    assert assessment.limitations


def test_cal40_unqualified_calibrator_cannot_upgrade() -> None:
    instrument = _instrument()
    calibrator = _instrument(
        category='sound_calibrator',
        serial_or_instance_id='CAL-4231',
        model='4231',
        manufacturer='B&K',
    )
    calibration = _accredited_calibration(instrument)
    check = build_verification_check(
        document_id=DOC,
        instrument_ref=instrument,
        kind='pre_use_field_check',
        outcome='within_tolerance',
        performed_at_utc=T1,
        calibrator_ref=calibrator,
        recorded_at_utc=T1,
    )
    assessment = evaluate_instrument_fitness(
        document_id=DOC,
        instrument=instrument,
        calibrations=(calibration,),
        checks=(check,),
        at_utc=T1,
        calibrator_fitness=lambda _id, _at: 'unknown',
        evaluated_at_utc=T2,
    )
    assert assessment.state == 'fit_with_limitations'


def test_cal50_service_event_forces_review_required() -> None:
    instrument = _instrument()
    calibration = _accredited_calibration(instrument)
    drop = build_service_event(
        document_id=DOC,
        instrument_ref=instrument,
        kind='dropped_or_impact',
        occurred_at_utc=T1,
        recorded_at_utc=T1,
    )
    assessment = evaluate_instrument_fitness(
        document_id=DOC,
        instrument=instrument,
        calibrations=(calibration,),
        service_events=(drop,),
        at_utc=T2,
        evaluated_at_utc=T2,
    )
    assert assessment.state == 'calibration_review_required'


def test_cal55_out_of_tolerance_check_marks_not_erases() -> None:
    instrument = _instrument()
    calibration = _accredited_calibration(instrument)
    oot = build_verification_check(
        document_id=DOC,
        instrument_ref=instrument,
        kind='post_use_field_check',
        outcome='out_of_tolerance',
        performed_at_utc=T1,
        observed_deviation_db=1.5,
        recorded_at_utc=T1,
    )
    assessment = evaluate_instrument_fitness(
        document_id=DOC,
        instrument=instrument,
        calibrations=(calibration,),
        checks=(oot,),
        at_utc=T2,
        evaluated_at_utc=T2,
    )
    assert assessment.state == 'out_of_tolerance'
    assert assessment.basis_check_ref is not None


def test_cal60_oot_review_marks_affected_measurements() -> None:
    instrument = _instrument()
    oot = build_verification_check(
        document_id=DOC,
        instrument_ref=instrument,
        kind='post_use_field_check',
        outcome='out_of_tolerance',
        performed_at_utc=T1,
        recorded_at_utc=T1,
    )
    review = review_out_of_tolerance(
        document_id=DOC,
        instrument=instrument,
        triggering_ref=oot,
        last_known_valid_at_utc=T0,
        affected_measurement_refs=('measurement-1', 'measurement-2'),
        disposition='review_required',
        recorded_at_utc=T2,
    )
    assert len(review.dispositions) == 2
    assert all(
        disposition == 'review_required'
        for _, disposition in review.dispositions
    )


def test_cal70_under_development_standard_is_research_only() -> None:
    instrument = _instrument()
    with pytest.raises(ValueError, match='research-only'):
        _accredited_calibration(
            instrument,
            standard_refs=('IEC 60942 Ed.5',),
            standard_edition_state='under_development',
        )


def test_cal75_accredited_class_requires_certificate() -> None:
    instrument = _instrument()
    with pytest.raises(ValueError, match='certificate'):
        build_calibration_event(
            document_id=DOC,
            instrument_ref=instrument,
            event_kind='laboratory_calibration',
            performed_at_utc=T0,
            traceability_class='accredited_traceable',
            recorded_at_utc=T0,
        )


def test_cal80_no_evidence_is_unknown_checks_only_is_check_required() -> (
    None
):
    instrument = _instrument()
    assessment = evaluate_instrument_fitness(
        document_id=DOC,
        instrument=instrument,
        at_utc=T1,
        evaluated_at_utc=T2,
    )
    assert assessment.state == 'unknown'

    check = build_verification_check(
        document_id=DOC,
        instrument_ref=instrument,
        kind='pre_use_field_check',
        outcome='within_tolerance',
        performed_at_utc=T0,
        recorded_at_utc=T0,
    )
    assessment2 = evaluate_instrument_fitness(
        document_id=DOC,
        instrument=instrument,
        checks=(check,),
        at_utc=T1,
        evaluated_at_utc=T2,
    )
    assert assessment2.state == 'check_required'


def test_calibration_correction_binding_blocks_silent_extrapolation() -> (
    None
):
    binding = CadCorrectionFileBinding(
        file_sha256=SHA_A,
        provider='miniDSP',
        orientation_deg=0.0,
        applies_to_serial='UMIK-1 SN7012345',
        allows_extrapolation=False,
    )
    assert binding.orientation_deg == 0.0
    assert binding.allows_extrapolation is False


def test_calibration_interval_policy_targets_instance_or_category() -> None:
    instrument = _instrument()
    by_instance = build_interval_policy(
        document_id=DOC,
        instrument_ref=instrument,
        basis='lab_quality_policy',
        nominal_interval_days=365,
        declared_at_utc=T0,
    )
    by_category = build_interval_policy(
        document_id=DOC,
        instrument_category='sound_calibrator',
        basis='ilac_g24_derived',
        nominal_interval_days=730,
        declared_at_utc=T0,
    )
    assert by_instance.instrument_ref is not None
    assert by_category.instrument_ref is None


def test_calibration_repository_round_trip_and_integrity(tmp_path) -> None:
    repo = CadCalibrationLifecycleRepository(_scene_repo(tmp_path))
    instrument = _instrument()
    calibration = _accredited_calibration(instrument)
    policy = build_interval_policy(
        document_id=DOC,
        instrument_ref=instrument,
        basis='manufacturer_recommended',
        nominal_interval_days=365,
        declared_at_utc=T0,
    )
    check = build_verification_check(
        document_id=DOC,
        instrument_ref=instrument,
        kind='pre_use_field_check',
        outcome='within_tolerance',
        performed_at_utc=T0,
        recorded_at_utc=T0,
    )
    service = build_service_event(
        document_id=DOC,
        instrument_ref=instrument,
        kind='sensor_cleaning_or_filter_change',
        occurred_at_utc=T0,
        recorded_at_utc=T0,
    )
    assessment = evaluate_instrument_fitness(
        document_id=DOC,
        instrument=instrument,
        calibrations=(calibration,),
        at_utc=T1,
        evaluated_at_utc=T2,
    )
    review = review_out_of_tolerance(
        document_id=DOC,
        instrument=instrument,
        triggering_ref=calibration,
        last_known_valid_at_utc=None,
        affected_measurement_refs=('measurement-1',),
        recorded_at_utc=T2,
    )
    repo.save_instrument(instrument)
    repo.save_calibration(calibration)
    repo.save_policy(policy)
    repo.save_check(check)
    repo.save_service_event(service)
    repo.save_assessment(assessment)
    repo.save_review(review)
    # Idempotent re-saves.
    repo.save_instrument(instrument)
    repo.save_assessment(assessment)

    assert repo.get_instrument(instrument.instrument_id) == instrument
    assert (
        repo.get_calibration(calibration.calibration_id) == calibration
    )
    assert repo.get_policy(policy.policy_id) == policy
    assert repo.get_check(check.check_id) == check
    assert repo.get_service_event(service.event_id) == service
    assert repo.get_assessment(assessment.assessment_id) == assessment
    assert repo.get_review(review.review_id) == review
    assert len(
        repo.list_calibrations(DOC, instrument_id=instrument.instrument_id)
    ) == 1
    assert len(
        repo.list_assessments(DOC, instrument_id=instrument.instrument_id)
    ) == 1

    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            'UPDATE cad_instrument_instances SET service_state=? '
            'WHERE instrument_id=?',
            ('damaged', instrument.instrument_id),
        )
    with pytest.raises(CalibrationLifecycleIntegrityError):
        repo.get_instrument(instrument.instrument_id)


def test_instrument_binding_pins_sha() -> None:
    instrument = _instrument()
    binding = instrument_binding(instrument)
    assert binding.ref_sha256 == instrument.instrument_sha256
