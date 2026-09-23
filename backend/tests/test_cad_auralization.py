from __future__ import annotations

from hashlib import sha256
import io
import wave

import numpy as np
import pytest

from htdt.cad_auralization import (
    AuralizationReceiverRef,
    DryProgramAssetRef,
    ImpulseAuthorityRef,
    assess_auralization_currency,
    build_auralization_artifact,
    build_auralization_render_spec,
    encode_wav_pcm_s16le,
    render_auralization,
)


def _hash(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode('utf-8')
    return sha256(data).hexdigest()


def _samples_hash(samples) -> str:
    return sha256(np.asarray(samples, dtype='<f8').tobytes()).hexdigest()


def _spec(**overrides):
    dry = (0.1, -0.2, 0.3, -0.1)
    impulse = (1.0, 0.5, 0.25)
    kwargs = dict(
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash=_hash('scene'),
        source_scenario_id='scenario-1',
        source_scenario_sha256=_hash('scenario'),
        receiver=AuralizationReceiverRef(
            receiver_id='seat-1', receiver_entity_id='entity-1'
        ),
        impulse_authority=ImpulseAuthorityRef(
            kind='predicted',
            artifact_id='ir:1',
            artifact_sha256=_samples_hash(impulse),
            sample_rate_hz=48000,
            sample_count=len(impulse),
            absolute_amplitude_authority=True,
        ),
        dry_source=DryProgramAssetRef(
            asset_sha256=_samples_hash(dry),
            sample_rate_hz=48000,
            sample_count=len(dry),
            program_level_authority='absolute_calibrated',
        ),
        output_sample_rate_hz=48000,
        gain_policy='preserve_physical_level',
    )
    kwargs.update(overrides)
    return build_auralization_render_spec(**kwargs), dry, impulse


def test_render_spec_hash_identity() -> None:
    spec, _, _ = _spec()
    assert spec.spec_id.startswith('auralization-render-spec:')


def test_preserve_physical_level_requires_absolute_authorities() -> None:
    with pytest.raises(ValueError, match='absolute amplitude authority'):
        _spec(
            impulse_authority=ImpulseAuthorityRef(
                kind='predicted',
                artifact_id='ir:1',
                artifact_sha256='a' * 64,
                sample_rate_hz=48000,
                sample_count=3,
                absolute_amplitude_authority=False,
            )
        )
    with pytest.raises(ValueError, match='calibrated program level'):
        _spec(
            dry_source=DryProgramAssetRef(
                asset_sha256='a' * 64,
                sample_rate_hz=48000,
                sample_count=4,
                program_level_authority='unknown',
            )
        )


def test_render_convolves_and_pins_inputs() -> None:
    spec, dry, impulse = _spec()
    pcm = render_auralization(
        spec,
        dry_samples=dry,
        impulse_samples=impulse,
        dry_asset_sha256=_samples_hash(dry),
        impulse_artifact_sha256=_samples_hash(impulse),
    )
    expected = np.convolve(np.asarray(dry), np.asarray(impulse))
    assert len(pcm.samples) == len(expected)
    np.testing.assert_allclose(
        np.asarray(pcm.samples), expected, rtol=1e-7, atol=1e-12
    )
    assert pcm.peak_linear == pytest.approx(
        float(np.max(np.abs(expected))), abs=1e-12
    )
    assert pcm.applied_gain_db == 0.0
    with pytest.raises(ValueError, match='pinned asset hash'):
        render_auralization(
            spec,
            dry_samples=dry,
            impulse_samples=impulse,
            dry_asset_sha256='0' * 64,
            impulse_artifact_sha256=_samples_hash(impulse),
        )


def test_exact_rate_match_required_rejects_mismatch() -> None:
    spec, dry, impulse = _spec(
        output_sample_rate_hz=44100,
        resample_policy='exact_rate_match_required',
    )
    with pytest.raises(ValueError, match='exact_rate_match_required'):
        render_auralization(
            spec,
            dry_samples=dry,
            impulse_samples=impulse,
            dry_asset_sha256=_samples_hash(dry),
            impulse_artifact_sha256=_samples_hash(impulse),
        )


def test_level_matched_rms_applies_gain_and_reports() -> None:
    spec, dry, impulse = _spec(
        gain_policy='level_matched_rms', rms_target_dbfs=-20.0
    )
    pcm = render_auralization(
        spec,
        dry_samples=dry,
        impulse_samples=impulse,
        dry_asset_sha256=_samples_hash(dry),
        impulse_artifact_sha256=_samples_hash(impulse),
    )
    rms = float(np.sqrt(np.mean(np.asarray(pcm.samples) ** 2)))
    target = 10.0 ** (-20.0 / 20.0)
    assert rms == pytest.approx(target, rel=1e-9)
    assert pcm.applied_gain_db != 0.0


def test_hard_limit_headroom_prevents_clipping() -> None:
    spec, dry, impulse = _spec(
        gain_policy='unity', headroom_policy='hard_limit'
    )
    loud = (10.0, -10.0)
    impulse = (1.0,)
    pcm = render_auralization(
        spec,
        dry_samples=loud,
        impulse_samples=impulse,
        dry_asset_sha256=_samples_hash(loud),
        impulse_artifact_sha256=_samples_hash(impulse),
    )
    assert pcm.peak_linear <= 0.9991
    assert pcm.clipped_sample_count == 0


def test_artifact_wav_and_currency() -> None:
    spec, dry, impulse = _spec()
    pcm = render_auralization(
        spec,
        dry_samples=dry,
        impulse_samples=impulse,
        dry_asset_sha256=_samples_hash(dry),
        impulse_artifact_sha256=_samples_hash(impulse),
    )
    artifact, wav_bytes = build_auralization_artifact(spec, pcm)
    assert artifact.artifact_id.startswith('auralization-artifact:')
    assert artifact.output_asset_sha256 == _hash(wav_bytes)
    with wave.open(io.BytesIO(wav_bytes), 'rb') as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 48000

    currency = assess_auralization_currency(
        artifact,
        spec,
        current_scene_content_hash=_hash('scene'),
        current_system_variant_sha256=None,
        current_ir_artifact_sha256=spec.impulse_authority.artifact_sha256,
        current_dry_asset_sha256=spec.dry_source.asset_sha256,
        current_source_scenario_sha256=spec.source_scenario_sha256,
    )
    assert currency.state == 'CURRENT'

    stale = assess_auralization_currency(
        artifact,
        spec,
        current_scene_content_hash=_hash('different-scene'),
        current_system_variant_sha256=None,
        current_ir_artifact_sha256=spec.impulse_authority.artifact_sha256,
        current_dry_asset_sha256=spec.dry_source.asset_sha256,
        current_source_scenario_sha256=spec.source_scenario_sha256,
    )
    assert stale.state == 'STALE'
    assert stale.stale_reasons
