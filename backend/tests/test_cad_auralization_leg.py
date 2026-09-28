"""Measured-leg auralization: resolver + provider + repository (#1202).

The writer chain in ``cad_auralization`` needs three authorities before it
can render — an ``ImpulseAuthorityRef``, a ``DryProgramAssetRef`` and a
pinned ``AuralizationRenderSpec``. These tests cover the measured-leg
composition: WAV decode/registration, IR authority resolution, spec
pinning from the measurement record, and the append-only artifact
repository that stores spec + sealed WAV bytes.
"""
from __future__ import annotations

import io
from hashlib import sha256
from pathlib import Path
import wave

import numpy as np
import pytest

from htdt.cad_auralization import encode_wav_pcm_s16le
from htdt.cad_auralization_repository import CadAuralizationRepository
from htdt.cad_auralization_service import (
    build_measured_render_spec,
    decode_dry_program_wav,
    materialize_measured_auralization,
    register_dry_program_asset,
    resolve_impulse_authority,
)
from htdt.cad_measurement_ir import normalize_rew_ir_text
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.managed_assets import ManagedAssetStore


def _ir_text() -> bytes:
    return (
        b'Impulse response export\n'
        b'Sample rate: 48000\n'
        b'0.0000000000 0.0001\n'
        b'0.0000208333 0.0500\n'
        b'0.0000416667 0.2000\n'
        b'0.0000625000 -0.0500\n'
        b'0.0000833333 -0.0100\n'
    )


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    measurement_repository = CadMeasurementRepository(scene_repository)
    auralization_repository = CadAuralizationRepository(scene_repository)
    return (
        scene_repository,
        revision,
        measurement_repository,
        auralization_repository,
    )


def _save_measurement(repository: CadMeasurementRepository, revision, mid: str):
    raw = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0), level_db=(70.0, 71.0, 69.0)
    )
    record = measurement_record_for_revision(
        revision,
        'point-mlp',
        measurement_id=mid,
        evidence_type='measured',
        channel_role='front_left',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
        routing_evidence='verified',
        source_kind='unknown',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f'dataset-{mid}',
        measurement_id=mid,
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
        phase_status='absent',
        level_reference='unknown',
        processing_json='{}',
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    repository.save(
        record, dataset, raw_filename=f'{mid}.json', raw_bytes=raw
    )
    return record


def _save_ir_dataset(
    repository: CadMeasurementRepository,
    measurement_id: str,
    **ir_kwargs,
):
    dataset, filename, raw = normalize_rew_ir_text(
        measurement_id, _ir_text(), filename='ir.txt', **ir_kwargs
    )
    repository.save_ir_dataset(dataset, raw_filename=filename, raw_bytes=raw)
    return dataset


def _dry_wav(samples: tuple[float, ...] = (0.1, -0.2, 0.3, -0.1)) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        pcm = np.clip(np.asarray(samples, dtype=np.float64), -1.0, 1.0)
        wav.writeframes(
            np.round(pcm * 32767).astype('<i2').tobytes()
        )
    return buffer.getvalue()


# ----------------------------------------------------------------------
# WAV decode + registration
# ----------------------------------------------------------------------


def test_decode_dry_program_wav_roundtrips_encoder(tmp_path: Path) -> None:
    class _Pcm:
        sample_rate_hz = 48000
        samples = (0.25, -0.5, 0.125)

    raw = encode_wav_pcm_s16le(_Pcm())
    samples, rate = decode_dry_program_wav(raw)
    assert rate == 48000
    assert len(samples) == 3
    np.testing.assert_allclose(
        np.asarray(samples), np.asarray(_Pcm.samples), atol=1e-4
    )


def test_decode_dry_program_wav_rejects_stereo() -> None:
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(b'\x00\x00' * 8)
    with pytest.raises(ValueError, match='mono'):
        decode_dry_program_wav(buffer.getvalue())


def test_decode_dry_program_wav_rejects_non_wav() -> None:
    with pytest.raises(ValueError, match='WAV'):
        decode_dry_program_wav(b'not a wav file')


def test_register_dry_program_asset_installs_managed_bytes(tmp_path: Path) -> None:
    assets = ManagedAssetStore(tmp_path / 'assets')
    raw = _dry_wav()
    ref, samples, rate = register_dry_program_asset(assets, raw)
    assert ref.asset_sha256 == sha256(raw).hexdigest()
    assert ref.decoded_pcm_sha256 == sha256(
        np.asarray(samples, dtype='<f8').tobytes()
    ).hexdigest()
    assert ref.sample_rate_hz == rate == 48000
    assert ref.sample_count == len(samples)
    assert ref.program_level_authority == 'unknown'
    assert assets.read_verified(ref.asset_sha256) == raw


# ----------------------------------------------------------------------
# Impulse authority resolution
# ----------------------------------------------------------------------


def test_resolve_impulse_authority_grants_absolute_only_when_calibrated(
    tmp_path: Path,
) -> None:
    _, revision, measurement_repository, _ = _repositories(tmp_path)
    record = _save_measurement(measurement_repository, revision, 'm-1')
    uncalibrated = _save_ir_dataset(measurement_repository, record.measurement_id)
    ref = resolve_impulse_authority(uncalibrated)
    assert ref.kind == 'measured'
    assert ref.artifact_id == uncalibrated.dataset_id
    assert ref.artifact_sha256 == uncalibrated.dataset_sha256
    assert ref.decoded_pcm_sha256 == sha256(
        np.asarray(uncalibrated.amplitudes, dtype='<f8').tobytes()
    ).hexdigest()
    assert ref.sample_rate_hz == 48000
    assert ref.sample_count == len(uncalibrated.amplitudes)
    assert ref.absolute_amplitude_authority is False

    calibrated = _save_ir_dataset(
        measurement_repository,
        record.measurement_id,
        amplitude_reference='full_scale',
        normalized=False,
        calibration_state='calibrated',
    )
    assert (
        resolve_impulse_authority(calibrated).absolute_amplitude_authority
        is True
    )


def test_resolve_impulse_authority_rejects_non_integral_rate(
    tmp_path: Path,
) -> None:
    _, revision, measurement_repository, _ = _repositories(tmp_path)
    record = _save_measurement(measurement_repository, revision, 'm-1')
    dataset, filename, raw = normalize_rew_ir_text(
        record.measurement_id,
        b'0.0\n0.5\n1.0\n',
        filename='ir.txt',
        sample_rate_hz=44100.5,
    )
    measurement_repository.save_ir_dataset(
        dataset, raw_filename=filename, raw_bytes=raw
    )
    with pytest.raises(ValueError, match='integral sample rate'):
        resolve_impulse_authority(dataset)


# ----------------------------------------------------------------------
# Spec composition + repository materialization
# ----------------------------------------------------------------------


def test_measured_leg_materializes_persisted_artifact(tmp_path: Path) -> None:
    (
        scene_repository,
        revision,
        measurement_repository,
        auralization_repository,
    ) = _repositories(tmp_path)
    record = _save_measurement(measurement_repository, revision, 'm-1')
    ir_dataset = _save_ir_dataset(measurement_repository, record.measurement_id)

    assets = ManagedAssetStore(tmp_path / 'dry-assets')
    dry_ref, dry_samples, _ = register_dry_program_asset(assets, _dry_wav())

    spec, measurement, impulse_authority = build_measured_render_spec(
        measurement_repository,
        ir_dataset=ir_dataset,
        dry_ref=dry_ref,
        output_sample_rate_hz=48000,
        gain_policy='unity',
    )
    assert spec.document_id == revision.document_id
    assert spec.scene_revision_id == revision.revision_id
    assert spec.scene_content_hash == revision.content_hash
    assert spec.system_variant_id is None
    assert spec.source_scenario_id == measurement.measurement_id
    assert spec.receiver.receiver_entity_id == measurement.measurement_entity_id
    assert spec.impulse_authority == impulse_authority
    assert spec.dry_source == dry_ref

    artifact, wav = materialize_measured_auralization(
        auralization_repository,
        spec,
        dry_samples=dry_samples,
        ir_dataset=ir_dataset,
    )
    assert artifact.spec_id == spec.spec_id
    assert artifact.ir_kind == 'measured'
    assert sha256(wav).hexdigest() == artifact.output_asset_sha256
    assert wav[:4] == b'RIFF'

    persisted = auralization_repository.get_artifact(artifact.artifact_id)
    assert persisted == artifact
    assert auralization_repository.get_render_spec(spec.spec_id) == spec
    assert (
        auralization_repository.find_artifact_by_sha(artifact.semantic_sha256)
        == artifact
    )
    assert auralization_repository.artifacts_for_spec(spec.spec_id) == (artifact,)
    assert auralization_repository.read_artifact_wav(artifact) == wav


def test_artifact_save_requires_spec_and_exact_bytes(tmp_path: Path) -> None:
    (
        _scene_repository,
        revision,
        measurement_repository,
        auralization_repository,
    ) = _repositories(tmp_path)
    record = _save_measurement(measurement_repository, revision, 'm-1')
    ir_dataset = _save_ir_dataset(measurement_repository, record.measurement_id)
    assets = ManagedAssetStore(tmp_path / 'dry-assets')
    dry_ref, dry_samples, _ = register_dry_program_asset(assets, _dry_wav())
    spec, _, _ = build_measured_render_spec(
        measurement_repository,
        ir_dataset=ir_dataset,
        dry_ref=dry_ref,
        output_sample_rate_hz=48000,
        gain_policy='unity',
    )

    artifact, wav = materialize_measured_auralization(
        auralization_repository,
        spec,
        dry_samples=dry_samples,
        ir_dataset=ir_dataset,
    )
    with pytest.raises(ValueError, match='append-only'):
        auralization_repository.save_artifact(artifact, wav)
    with pytest.raises(ValueError, match='output_asset_sha256'):
        auralization_repository.save_artifact(
            artifact.model_copy(
                update={
                    'output_asset_sha256': 'f' * 64,
                    'artifact_id': 'auralization-artifact:' + 'e' * 64,
                    'semantic_sha256': 'e' * 64,
                }
            ),
            wav,
        )
