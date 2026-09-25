"""Issue #1007: measurement-chain dynamic range authority — per-stage
capabilities, capture headroom/clipping provenance, evidence gates that
fail closed."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_input_chain_capability import (
    CaptureDynamicRangeObservation,
    ChainStageCapability,
    MeasurementInputChainProfile,
    build_dynamic_range_observation,
    build_input_chain_profile,
    gate_distortion_evidence,
    gate_fr_ir_evidence,
)


def _profile(**overrides):
    kwargs = dict(
        profile_id='chain-1',
        schema_version='chain_v1',
        stages=(
            ChainStageCapability(
                stage='capsule',
                max_spl_db=133.0,
                max_spl_distortion_percent=3.0,
                sensitivity_mv_per_pa=10.0,
            ),
            ChainStageCapability(
                stage='adc',
                bit_depth=24,
                adc_snr_db=114.0,
                self_noise_dbfs=-110.0,
            ),
        ),
        gain_state_db=0.0,
    )
    kwargs.update(overrides)
    return build_input_chain_profile(**kwargs)


def _observation(**overrides):
    kwargs = dict(
        observation_id='obs-1',
        schema_version='obs_v1',
        document_id='doc-1',
        measurement_id='meas-1',
        chain_profile_id='chain-1',
        chain_profile_sha256=_profile().profile_sha256,
        observed_peak_dbfs=-8.0,
        observed_headroom_db=8.0,
        clipping_state='none',
        noise_floor_dbfs=-96.0,
        observed_at_utc='2026-09-25T00:01:00+00:00',
    )
    kwargs.update(overrides)
    return build_dynamic_range_observation(**kwargs)


def test_profile_sealed_and_stage_specific():
    profile = _profile()
    assert len(profile.profile_sha256) == 64
    MeasurementInputChainProfile.model_validate(profile.model_dump(mode='json'))


def test_max_spl_needs_distortion_criterion():
    with pytest.raises(ValidationError, match='distortion criterion'):
        ChainStageCapability(stage='capsule', max_spl_db=140.0)


def test_profile_requires_declared_stages():
    with pytest.raises(ValidationError, match='at least one stage'):
        _profile(stages=())


def test_unique_stages():
    with pytest.raises(ValidationError, match='duplicate'):
        _profile(
            stages=(
                ChainStageCapability(stage='adc', bit_depth=24),
                ChainStageCapability(stage='adc', bit_depth=16),
            )
        )


def test_headroom_consistency():
    obs = _observation()
    assert obs.observed_headroom_db == pytest.approx(8.0, abs=0.51)
    with pytest.raises(ValidationError, match='headroom'):
        CaptureDynamicRangeObservation(
            **{
                **obs.model_dump(mode='json'),
                'observed_headroom_db': 10.0,
            }
        )


def test_clean_digital_peak_never_proves_analog_linearity():
    with pytest.raises(ValidationError, match='upstream clipping states'):
        _observation(clipping_state='analog_suspected')
    # with evidence the suspect state is representable
    obs = _observation(
        clipping_state='analog_suspected',
        clipping_evidence_refs=('producer-warning-1',),
    )
    assert obs.clipping_state == 'analog_suspected'


def test_fr_ir_gate_states():
    profile = _profile()
    assert gate_fr_ir_evidence(profile, _observation()).state == 'supported'
    assert (
        gate_fr_ir_evidence(profile, _observation(clipping_state='unknown')).state
        == 'unknown'
    )
    assert (
        gate_fr_ir_evidence(
            profile,
            _observation(
                clipping_state='digital',
                clipping_evidence_refs=('overflow-flag',),
            ),
        ).state
        == 'unsupported'
    )
    assert (
        gate_fr_ir_evidence(
            profile,
            _observation(observed_peak_dbfs=-4.0, observed_headroom_db=4.0),
        ).state
        == 'limited'
    )
    assert gate_fr_ir_evidence(profile, None).state == 'unknown'


def test_distortion_gate_vs_measured_floor():
    profile = _profile()
    obs = _observation()
    # instrument floor -96, measured -80 → 16 dB separation → supported
    assert (
        gate_distortion_evidence(profile, obs, measured_noise_floor_db=-80.0).state
        == 'supported'
    )
    # 3 dB separation → limited
    assert (
        gate_distortion_evidence(profile, obs, measured_noise_floor_db=-93.0).state
        == 'limited'
    )
    # at/below floor → unsupported
    assert (
        gate_distortion_evidence(profile, obs, measured_noise_floor_db=-97.0).state
        == 'unsupported'
    )
    # no measured floor → limited (floor known)
    assert gate_distortion_evidence(profile, obs).state == 'limited'
    # clipped capture → unsupported regardless of floor
    clipped = _observation(
        clipping_state='upstream_suspected',
        clipping_evidence_refs=('ev-1',),
    )
    assert (
        gate_distortion_evidence(
            profile, clipped, measured_noise_floor_db=-80.0
        ).state
        == 'unsupported'
    )
