from __future__ import annotations

import pytest

from htdt.cad_fir_filter import (
    DeviceFIRCapabilityProfile,
    build_fir_filter_artifact,
    derive_fir_class,
    evaluate_device_fir_support,
    evaluate_fir_artifact,
    evaluate_fir_chain,
    import_fir_filter_artifact,
    materialize_fir_artifact,
)


def _linear_phase(**overrides):
    kwargs = dict(
        filter_class='linear_phase',
        sample_rate_hz=48000.0,
        taps=(0.1, 0.2, 0.5, 0.2, 0.1),
        tap_format='float64',
        channel_id='FL',
        time_reference_sample=2,
        latency_s=0.0005,
        source_producer='test',
        source_version='1',
    )
    kwargs.update(overrides)
    return build_fir_filter_artifact(**kwargs)


def test_artifact_is_content_addressed_and_class_derived() -> None:
    artifact = _linear_phase()
    assert artifact.artifact_id.startswith('fir-filter:')
    assert artifact.class_verified is True
    assert artifact.filter_class == 'linear_phase'
    assert artifact.duration_s == pytest.approx(5.0 / 48000.0)
    assert _linear_phase() == artifact

    # A claim the taps cannot satisfy fails closed.
    with pytest.raises(ValueError, match='disagrees with the derived'):
        _linear_phase(filter_class='minimum_phase')

    assert derive_fir_class((1.0, -0.5, 0.25)) == 'minimum_phase'
    assert derive_fir_class((0.5, 0.0, 1.0)) == 'mixed_phase'


def test_response_reports_magnitude_phase_and_group_delay() -> None:
    delay = build_fir_filter_artifact(
        filter_class='linear_phase',
        sample_rate_hz=48000.0,
        taps=(0.0, 1.0, 0.0),
        tap_format='float64',
        channel_id='FL',
        time_reference_sample=1,
        latency_s=0.001,
        source_producer='test',
        source_version='1',
    )
    diagnostics = evaluate_fir_artifact(
        delay, (1000.0, 4000.0, 8000.0)
    )
    for level in diagnostics.magnitude_db:
        assert level == pytest.approx(0.0, abs=1e-6)
    # Pure delay -> linear phase, constant group delay of 1 sample.
    for delay_s in diagnostics.group_delay_s:
        assert delay_s == pytest.approx(1.0 / 48000.0, rel=1e-3)
    assert diagnostics.pre_ringing_energy == pytest.approx(0.0)
    assert diagnostics.post_ringing_energy == pytest.approx(0.0)
    assert diagnostics.total_latency_s == pytest.approx(0.001)

    pre_event = _linear_phase(
        taps=(0.6, 0.0, 1.0, 0.0, 0.0), filter_class='arbitrary_fir'
    )
    diagnostics = evaluate_fir_artifact(pre_event, (1000.0,))
    assert diagnostics.pre_ringing_energy == pytest.approx(0.36)
    assert diagnostics.pre_event_peak_amplitude == pytest.approx(0.6)


def test_ordered_chain_composes_and_sums_latency() -> None:
    first = _linear_phase()
    second = _linear_phase(
        taps=(1.0, 0.0, 0.5, 0.0),
        filter_class='arbitrary_fir',
        time_reference_sample=0,
        latency_s=0.001,
    )
    chain = evaluate_fir_chain(
        (first, second), (1000.0, 2000.0, 4000.0)
    )
    assert chain.order_state == 'declared'
    assert chain.total_latency_s == pytest.approx(0.0015)
    assert chain.artifact_ids == (first.artifact_id, second.artifact_id)
    for level in chain.total_magnitude_db:
        assert abs(level) < 6.0

    with pytest.raises(ValueError, match='one common sample rate'):
        evaluate_fir_chain(
            (first, _linear_phase(sample_rate_hz=44100.0)),
            (1000.0,),
        )


def test_import_preserves_bytes_and_never_marks_applied() -> None:
    artifact, record = import_fir_filter_artifact(
        source_bytes=b'0.1 0.5 0.1\n',
        source_format='raw_float_taps',
        declared_sample_rate_hz=48000.0,
        channel_id='FR',
        declared_channel_id='FR',
    )
    assert artifact.filter_class == 'imported_opaque_fir'
    assert artifact.class_verified is False
    assert artifact.raw_source_sha256 == record.source_sha256
    assert record.import_id.startswith('fir-import:')
    assert record.artifact_sha256 == artifact.semantic_sha256

    with pytest.raises(ValueError, match='not implemented'):
        import_fir_filter_artifact(
            source_bytes=b'0 0 1',
            source_format='minidsp_text_export',
            declared_sample_rate_hz=48000.0,
            channel_id='FR',
        )


def test_device_support_fails_closed() -> None:
    artifact = _linear_phase()
    unknown = DeviceFIRCapabilityProfile(
        capability_id='av',
        capability_version='1',
        support_state='unknown',
    )
    verdict, reasons = evaluate_device_fir_support(artifact, unknown)
    assert verdict == 'unsupported'
    assert reasons

    tight = DeviceFIRCapabilityProfile(
        capability_id='av',
        capability_version='1',
        support_state='supported',
        supported_sample_rates_hz=(48000.0,),
        max_taps_per_channel=4,
    )
    verdict, reasons = evaluate_device_fir_support(artifact, tight)
    assert verdict == 'limited'
    assert any('tap count' in reason for reason in reasons)

    fits = DeviceFIRCapabilityProfile(
        capability_id='av',
        capability_version='1',
        support_state='supported',
        supported_sample_rates_hz=(48000.0, 44100.0),
        max_taps_per_channel=128,
    )
    assert evaluate_device_fir_support(artifact, fits)[0] == 'supported'


def test_materialization_is_explicit_and_reported() -> None:
    artifact = _linear_phase()
    materialized, report = materialize_fir_artifact(
        artifact, max_tap_count=4
    )
    assert materialized.artifact_id != artifact.artifact_id
    assert len(materialized.taps) == 4
    assert report.truncated_tap_count == 1
    assert report.materialized_artifact_sha256 == materialized.semantic_sha256
    assert report.max_response_error_db >= 0.0

    resampled, report = materialize_fir_artifact(
        artifact, target_sample_rate_hz=24000.0
    )
    assert resampled.sample_rate_hz == pytest.approx(24000.0)
    assert len(resampled.taps) == 2  # round(5 taps x 24k/48k)
    assert report.resampled is True
    assert report.max_response_error_db >= 0.0
