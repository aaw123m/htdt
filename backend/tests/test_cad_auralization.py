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
            artifact_sha256=_hash('ir-artifact-bytes'),
            decoded_pcm_sha256=_samples_hash(impulse),
            sample_rate_hz=48000,
            sample_count=len(impulse),
            absolute_amplitude_authority=True,
        ),
        dry_source=DryProgramAssetRef(
            asset_sha256=_hash('dry-asset-bytes'),
            decoded_pcm_sha256=_samples_hash(dry),
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
                decoded_pcm_sha256='b' * 64,
                sample_rate_hz=48000,
                sample_count=3,
                absolute_amplitude_authority=False,
            )
        )
    with pytest.raises(ValueError, match='calibrated program level'):
        _spec(
            dry_source=DryProgramAssetRef(
                asset_sha256='a' * 64,
                decoded_pcm_sha256='b' * 64,
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
        dry_asset_sha256=spec.dry_source.asset_sha256,
        impulse_artifact_sha256=spec.impulse_authority.artifact_sha256,
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
    assert pcm.level_semantics == 'physical_level_preserved'
    assert pcm.pre_headroom_clipped_sample_count == 0
    with pytest.raises(ValueError, match='pinned by the spec'):
        render_auralization(
            spec,
            dry_samples=dry,
            impulse_samples=impulse,
            dry_asset_sha256='0' * 64,
            impulse_artifact_sha256=spec.impulse_authority.artifact_sha256,
        )


def test_render_rejects_samples_unrelated_to_pinned_assets() -> None:
    """#1031: samples for a different asset fail closed even when the caller
    supplies that other asset's own hash."""
    spec, dry, impulse = _spec()
    other_dry = (0.9, 0.9, 0.9, 0.9)
    with pytest.raises(ValueError, match='decoded PCM hash'):
        render_auralization(
            spec,
            dry_samples=other_dry,
            impulse_samples=impulse,
            dry_asset_sha256=spec.dry_source.asset_sha256,
            impulse_artifact_sha256=spec.impulse_authority.artifact_sha256,
        )
    # Declaring the other asset's hash also fails — it is not the spec pin.
    with pytest.raises(ValueError, match='pinned by the spec'):
        render_auralization(
            spec,
            dry_samples=dry,
            impulse_samples=impulse,
            dry_asset_sha256=_samples_hash(other_dry),
            impulse_artifact_sha256=spec.impulse_authority.artifact_sha256,
        )
    # Sample-count pins are validated before processing.
    with pytest.raises(ValueError, match='sample count'):
        render_auralization(
            spec,
            dry_samples=dry + (0.0,),
            impulse_samples=impulse,
            dry_asset_sha256=spec.dry_source.asset_sha256,
            impulse_artifact_sha256=spec.impulse_authority.artifact_sha256,
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
            dry_asset_sha256=spec.dry_source.asset_sha256,
            impulse_artifact_sha256=spec.impulse_authority.artifact_sha256,
        )


def test_band_limited_resample_preserves_tone_and_rejects_alias() -> None:
    """#956: band-limited SRC keeps an in-band sine and drops an alias."""
    fs_in = 48000
    n = 4800
    t = np.arange(n) / fs_in
    dry_mixed = (
        0.5 * np.sin(2.0 * np.pi * 1000.0 * t)
        + 0.1 * np.sin(2.0 * np.pi * 23000.0 * t)
    )
    impulse = (1.0,)
    spec, _, _ = _spec(
        impulse_authority=ImpulseAuthorityRef(
            kind='predicted',
            artifact_id='ir:delta',
            artifact_sha256=_hash('ir-delta'),
            decoded_pcm_sha256=_samples_hash(impulse),
            sample_rate_hz=fs_in,
            sample_count=len(impulse),
            absolute_amplitude_authority=True,
        ),
        dry_source=DryProgramAssetRef(
            asset_sha256=_hash('dry-tone'),
            decoded_pcm_sha256=_samples_hash(dry_mixed),
            sample_rate_hz=fs_in,
            sample_count=n,
            program_level_authority='absolute_calibrated',
        ),
        output_sample_rate_hz=16000,
        resample_policy='band_limited_resample',
        gain_policy='preserve_physical_level',
    )
    pcm = render_auralization(
        spec,
        dry_samples=tuple(float(v) for v in dry_mixed),
        impulse_samples=impulse,
        dry_asset_sha256=spec.dry_source.asset_sha256,
        impulse_artifact_sha256=spec.impulse_authority.artifact_sha256,
    )
    assert pcm.resample_method == 'htdt.fft_bandlimited_resample_v1'
    out = np.asarray(pcm.samples)
    fs_out = 16000
    t_out = np.arange(len(out)) / fs_out
    expected = 0.5 * np.sin(2.0 * np.pi * 1000.0 * t_out)
    # In-band tone preserved; the 23 kHz component cannot alias into band.
    residual = out - expected[: len(out)]
    assert float(np.sqrt(np.mean(residual**2))) < 1e-4
    assert len(out) == int(round(n * fs_out / fs_in))


def test_level_matched_rms_applies_gain_and_reports() -> None:
    spec, dry, impulse = _spec(
        gain_policy='level_matched_rms', rms_target_dbfs=-20.0
    )
    pcm = render_auralization(
        spec,
        dry_samples=dry,
        impulse_samples=impulse,
        dry_asset_sha256=spec.dry_source.asset_sha256,
        impulse_artifact_sha256=spec.impulse_authority.artifact_sha256,
    )
    rms = float(np.sqrt(np.mean(np.asarray(pcm.samples) ** 2)))
    target = 10.0 ** (-20.0 / 20.0)
    assert rms == pytest.approx(target, rel=1e-9)
    assert pcm.applied_gain_db != 0.0
    assert pcm.level_semantics == 'level_matched'


def _loud_spec(**overrides):
    """Spec pinned to a 2-sample loud dry asset + unit impulse."""
    loud = (10.0, -10.0)
    one = (1.0,)
    kwargs = dict(
        impulse_authority=ImpulseAuthorityRef(
            kind='predicted',
            artifact_id='ir:unit',
            artifact_sha256=_hash('ir-unit'),
            decoded_pcm_sha256=_samples_hash(one),
            sample_rate_hz=48000,
            sample_count=1,
            absolute_amplitude_authority=True,
        ),
        dry_source=DryProgramAssetRef(
            asset_sha256=_hash('dry-loud'),
            decoded_pcm_sha256=_samples_hash(loud),
            sample_rate_hz=48000,
            sample_count=len(loud),
            program_level_authority='absolute_calibrated',
        ),
    )
    kwargs.update(overrides)
    spec, _, _ = _spec(**kwargs)
    return spec, loud, one


def test_attenuate_to_fit_headroom_prevents_clipping() -> None:
    spec, loud, one = _loud_spec(
        gain_policy='unity', headroom_policy='attenuate_to_fit'
    )
    pcm = render_auralization(
        spec,
        dry_samples=loud,
        impulse_samples=one,
        dry_asset_sha256=spec.dry_source.asset_sha256,
        impulse_artifact_sha256=spec.impulse_authority.artifact_sha256,
    )
    assert pcm.peak_linear <= 0.9991
    assert pcm.clipped_sample_count == 0
    # Pre-attenuation diagnostics are preserved, headroom gain is separate.
    assert pcm.pre_headroom_clipped_sample_count == len(loud)
    assert pcm.pre_headroom_peak_linear == pytest.approx(10.0)
    assert pcm.headroom_gain_db < 0.0
    assert pcm.level_semantics == 'unity_relative'


def test_preserve_physical_level_overrange_marks_attenuated() -> None:
    """#1028: a physical-level render that needed headroom attenuation is
    labelled attenuated, never silently 'preserved'."""
    spec, loud, one = _loud_spec(
        headroom_policy='attenuate_to_fit',
        gain_policy='preserve_physical_level',
    )
    pcm = render_auralization(
        spec,
        dry_samples=loud,
        impulse_samples=one,
        dry_asset_sha256=spec.dry_source.asset_sha256,
        impulse_artifact_sha256=spec.impulse_authority.artifact_sha256,
    )
    assert pcm.level_semantics == 'physical_level_attenuated_for_playback'
    assert pcm.pre_headroom_clipped_sample_count == 2
    assert pcm.clipped_sample_count == 0
    assert pcm.headroom_gain_db < 0.0


def test_artifact_wav_and_currency() -> None:
    spec, dry, impulse = _spec()
    pcm = render_auralization(
        spec,
        dry_samples=dry,
        impulse_samples=impulse,
        dry_asset_sha256=spec.dry_source.asset_sha256,
        impulse_artifact_sha256=spec.impulse_authority.artifact_sha256,
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
