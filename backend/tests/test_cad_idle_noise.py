"""Playback-chain idle-noise commissioning tests (#1044)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_equipment import EquipmentDataProvenance
from htdt.cad_idle_noise import (
    IdleNoiseBand,
    IdleNoiseSpectrum,
    PlaybackIdleNoiseMeasurement,
    build_idle_noise_measurement,
    detect_mains_harmonic_candidate,
    evaluate_idle_noise_commissioning,
    suggest_idle_noise_actions,
)


def _provenance(digit: str = '2'):
    return (
        EquipmentDataProvenance(
            evidence_kind='measured',
            source_name='audio analyzer',
            source_version='1',
            source_reference='commissioning session 9',
            source_sha256=digit * 64,
        ),
    )


def _spectrum(**overrides):
    kwargs = dict(
        weighting='A',
        level_semantics='absolute_spl',
        bands=(
            IdleNoiseBand(band_center_hz=60.0, band_level_db=18.0),
            IdleNoiseBand(band_center_hz=120.0, band_level_db=15.0),
            IdleNoiseBand(band_center_hz=180.0, band_level_db=11.0),
        ),
    )
    kwargs.update(overrides)
    return IdleNoiseSpectrum(**kwargs)


def _measurement(**overrides):
    kwargs = dict(
        measurement_id='inm-1',
        session_id='sess-1',
        device_id='avr-1',
        channel='FL',
        device_state='unmuted_zero_input',
        mute_state='unmuted',
        volume_setting='-20 dB',
        spectrum=_spectrum(),
        acquisition_context_id='acq-1',
        calibration_authority_id='cal-1',
        provenance=_provenance(),
    )
    kwargs.update(overrides)
    return build_idle_noise_measurement(**kwargs)


def test_measurement_hash_and_calibration_contract():
    measurement = _measurement()
    payload = measurement.model_dump(mode='python')
    payload['channel'] = 'FR'
    with pytest.raises(ValidationError, match='semantic hash mismatch'):
        PlaybackIdleNoiseMeasurement(**payload)
    # absolute SPL without calibration is rejected at the model level
    with pytest.raises(ValidationError, match='calibration'):
        _measurement(calibration_authority_id=None)


def test_relative_semantics_no_calibration():
    measurement = _measurement(
        spectrum=_spectrum(level_semantics='relative_difference'),
        calibration_authority_id=None,
    )
    report = evaluate_idle_noise_commissioning(measurement=measurement)
    checks = {c.check: c.status for c in report.checks}
    assert checks['absolute_spl_calibrated'] == 'NOT_APPLICABLE'
    assert checks['spectrum_recorded'] == 'PASS'


def test_unknown_channel_and_state_stay_unknown():
    measurement = _measurement(
        channel='unknown',
        device_state='unknown',
        spectrum=None,
    )
    report = evaluate_idle_noise_commissioning(measurement=measurement)
    checks = {c.check: c.status for c in report.checks}
    assert checks['channel_bound'] == 'UNKNOWN'
    assert checks['device_state_recorded'] == 'UNKNOWN'
    assert checks['spectrum_recorded'] == 'UNKNOWN'


def test_mains_candidate_requires_supplied_mains_frequency():
    measurement = _measurement()
    # no mains frequency supplied → NOT_APPLICABLE, no candidate
    report = evaluate_idle_noise_commissioning(measurement=measurement)
    checks = {c.check: c.status for c in report.checks}
    assert checks['mains_candidate'] == 'NOT_APPLICABLE'
    assert report.candidates == ()
    # 60 Hz mains → candidate surfaces as a hypothesis
    assert detect_mains_harmonic_candidate(
        measurement=measurement, mains_frequency_hz=60.0
    )
    report = evaluate_idle_noise_commissioning(
        measurement=measurement, mains_frequency_hz=60.0
    )
    checks = {c.check: c.status for c in report.checks}
    assert checks['mains_candidate'] == 'UNKNOWN'
    assert 'mains_harmonic_pattern_candidate' in report.candidates
    # 50 Hz mains → bands at 60/120/180 do NOT match 50 Hz harmonics
    assert not detect_mains_harmonic_candidate(
        measurement=measurement, mains_frequency_hz=50.0
    )


def test_candidate_notes_never_defeat_earthing():
    notes = suggest_idle_noise_actions(
        candidates=('mains_harmonic_pattern_candidate',)
    )
    assert notes
    assert any('never defeat protective earthing' in n for n in notes)
    empty = suggest_idle_noise_actions(candidates=())
    assert empty == ()


def test_broadband_spectrum_no_mains_candidate():
    measurement = _measurement(
        spectrum=_spectrum(
            bands=(
                IdleNoiseBand(
                    band_center_hz=1000.0, band_level_db=12.0
                ),
                IdleNoiseBand(
                    band_center_hz=2000.0, band_level_db=12.0
                ),
            )
        )
    )
    assert not detect_mains_harmonic_candidate(
        measurement=measurement, mains_frequency_hz=60.0
    )
    report = evaluate_idle_noise_commissioning(
        measurement=measurement, mains_frequency_hz=60.0
    )
    checks = {c.check: c.status for c in report.checks}
    assert checks['mains_candidate'] == 'PASS'
