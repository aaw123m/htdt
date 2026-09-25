"""Program-material stress profile tests (#1003)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_equipment import EquipmentDataProvenance
from htdt.cad_program_stress import (
    ProgramBandStress,
    ProgramChannelActivity,
    ProgramStressProfile,
    ProgramTemporalLevels,
    build_program_stress_profile,
    build_standardized_program_stress_profile,
    evaluate_program_stress,
)


def _provenance():
    return (
        EquipmentDataProvenance(
            evidence_kind='manufacturer',
            source_name='AES',
            source_version='AES75-2022',
            source_reference='Music-Noise test signal',
            source_sha256='7' * 64,
        ),
    )


def _band(**overrides):
    kwargs = dict(
        band_id='oct-63',
        low_hz=45.0,
        high_hz=90.0,
        rms_level_db=-23.0,
        peak_level_db=-5.0,
        window='long_term',
    )
    kwargs.update(overrides)
    return ProgramBandStress(**kwargs)


def _profile(**overrides):
    kwargs = dict(
        profile_id='stress-feature-film',
        version='1',
        kind='user_authored_engineering_envelope',
        level_reference='dbfs',
        bands=(
            _band(),
            _band(band_id='oct-125', low_hz=90.0, high_hz=180.0),
        ),
        windows=(
            ProgramTemporalLevels(
                window='long_term', rms_level_db=-20.0
            ),
            ProgramTemporalLevels(
                window='instantaneous', true_peak_level_db=-1.0
            ),
        ),
        channel_activity=(
            ProgramChannelActivity(channel_id='FL', active_fraction=1.0),
            ProgramChannelActivity(
                channel_id='SL', active=True, active_fraction=0.35
            ),
            ProgramChannelActivity(channel_id='SBR', active=False),
        ),
        channel_correlation='independent',
        duration_s=7200.0,
        normalization_basis='as_recorded',
        uncertainty_db=0.5,
        provenance=_provenance(),
    )
    kwargs.update(overrides)
    return build_program_stress_profile(**kwargs)


def test_profile_hash_and_kind_contract():
    profile = _profile()
    assert profile.semantic_sha256
    payload = profile.model_dump(mode='python')
    payload['kind'] = 'measured_content_statistics'
    with pytest.raises(ValidationError, match='semantic hash mismatch'):
        ProgramStressProfile(**payload)


def test_band_requires_frequency_basis():
    with pytest.raises(ValueError, match='frequency bounds'):
        ProgramBandStress(band_id='x', rms_level_db=-20.0)
    with pytest.raises(ValueError, match='pairs'):
        ProgramBandStress(band_id='x', low_hz=40.0)


def test_band_derives_crest_only_from_rms_peak():
    derived = _band(crest_factor_db=None, rms_level_db=-23.0, peak_level_db=-5.0)
    assert derived.crest_factor_db == pytest.approx(18.0)
    no_evidence = _band(
        crest_factor_db=None, rms_level_db=None, peak_level_db=None
    )
    assert no_evidence.crest_factor_db is None


def test_inactive_channel_carries_no_levels():
    with pytest.raises(ValueError, match='inactive channel'):
        ProgramChannelActivity(
            channel_id='SBR', active=False, rms_level_db=-30.0
        )


def test_standardized_profile_requires_name_and_version():
    with pytest.raises(ValidationError):
        build_program_stress_profile(kind='standardized_test_signal')


def test_aes75_style_standardized_profile():
    profile = build_standardized_program_stress_profile(
        standardized_name='AES75',
        standardized_version='Music-Noise 2022',
        bands=(
            _band(band_id='wide', low_hz=20.0, high_hz=20000.0,
                  rms_level_db=-20.0, peak_level_db=-2.0),
        ),
        channel_correlation='independent',
        provenance=_provenance(),
    )
    assert profile.kind == 'standardized_test_signal'
    assert profile.standardized_name == 'AES75'
    evaluation = evaluate_program_stress(profile=profile)
    checks = {c.check: c.status for c in evaluation.checks}
    assert checks['spectral_content_recorded'] == 'PASS'
    assert checks['crest_factor_evidence'] == 'PASS'


def test_evaluation_reports_missing_evidence_as_unknown():
    empty = build_program_stress_profile(profile_id='bare')
    evaluation = evaluate_program_stress(profile=empty)
    checks = {c.check: c.status for c in evaluation.checks}
    assert checks['spectral_content_recorded'] == 'UNKNOWN'
    assert checks['crest_factor_evidence'] == 'UNKNOWN'
    assert checks['temporal_windows_distinct'] == 'UNKNOWN'
    assert checks['channel_activity_recorded'] == 'UNKNOWN'
    assert checks['duration_recorded'] == 'UNKNOWN'
    assert checks['level_reference_recorded'] == 'UNKNOWN'
    assert checks['thermal_evidence_bound'] == 'UNKNOWN'


def test_thermal_evidence_resolution():
    profile = _profile(thermal_evidence_ref='thermal-avr-1')
    evaluation = evaluate_program_stress(
        profile=profile,
        known_thermal_authority_ids=('thermal-avr-1',),
    )
    checks = {c.check: c.status for c in evaluation.checks}
    assert checks['thermal_evidence_bound'] == 'PASS'
    evaluation = evaluate_program_stress(
        profile=profile,
        known_thermal_authority_ids=('other',),
    )
    checks = {c.check: c.status for c in evaluation.checks}
    assert checks['thermal_evidence_bound'] == 'FAIL'


def test_windows_stay_distinct_quantities():
    profile = _profile()
    long_term = profile.window_levels('long_term')
    instantaneous = profile.window_levels('instantaneous')
    assert long_term is not None and long_term.rms_level_db == -20.0
    assert instantaneous is not None
    assert instantaneous.true_peak_level_db == -1.0
    assert instantaneous.rms_level_db is None
    assert profile.window_levels('short_term') is None


def test_no_overall_score_field():
    # The profile deliberately carries no aggregate stress scalar.
    profile = _profile()
    assert not hasattr(profile, 'overall_stress')
    assert 'score' not in profile.semantic_payload()
