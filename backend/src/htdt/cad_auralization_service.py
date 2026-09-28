"""Measured-leg auralization primitives — resolver, provider and composer (#1202).

The auralization contract (:mod:`htdt.cad_auralization`) requires three
authorities before :func:`render_auralization` can run:

* an :class:`ImpulseAuthorityRef` pinning the impulse-response dataset that
  convolves the dry programme,
* a :class:`DryProgramAssetRef` pinning the dry programme asset, and
* an :class:`AuralizationRenderSpec` pinning the scene/system/source/receiver
  lane plus deterministic render policies.

This module composes them from the *measured* leg:

* ``resolve_impulse_authority`` seals a persisted
  :class:`CadImpulseResponseDataset` as the impulse authority — measured IRs
  are already shipped with channel_count=1 and are the only IR producer that
  exists today. Predicted (simulated) IRs have no producer in the codebase,
  so the predicted leg stays unimplemented rather than faked.
* ``decode_dry_program_wav`` + ``register_dry_program_asset`` turn a mono
  integer-PCM WAV (as produced by :func:`encode_wav_pcm_s16le` or the user's
  own dry material) into a managed-asset ``DryProgramAssetRef``.
* ``build_measured_render_spec`` pins the spec from the measurement record —
  document/revision/content-hash authority, the measured channel as the
  source scenario, and the measured receiver position — with
  ``system_variant`` left unpinned because measurements bind the exact
  measured revision, not a variant authority.
"""

from __future__ import annotations

from hashlib import sha256
import io
import wave

import numpy as np

from .cad_auralization import (
    AuralizationArtifact,
    AuralizationReceiverRef,
    AuralizationRenderSpec,
    DryProgramAssetRef,
    GainPolicy,
    HeadroomPolicy,
    ImpulseAuthorityRef,
    ResamplePolicy,
    build_auralization_artifact,
    build_auralization_render_spec,
    render_auralization,
)
from .cad_auralization_repository import CadAuralizationRepository
from .cad_measurement_ir import CadImpulseResponseDataset
from .cad_measurement_repository import CadMeasurementRepository
from .cad_measurement_quality import measurement_sha256
from .cad_measurements import CadMeasurementRecord
from .managed_assets import ManagedAssetStore


def resolve_impulse_authority(
    dataset: CadImpulseResponseDataset,
) -> ImpulseAuthorityRef:
    """Seal a measured IR dataset as the render's impulse authority.

    Measured datasets ship as mono PCM; the per-artifact ``decoded_pcm_sha256``
    is the byte digest of the float64 sample vector the renderer consumes, so
    :func:`render_auralization` can re-verify the persisted amplitudes
    bit-for-bit. ``absolute_amplitude_authority`` is granted only when the
    dataset declares a full-scale reference with declared calibration and no
    normalization — anything weaker renders *relative* level.
    """

    rate = float(dataset.sample_rate_hz)
    rounded = int(round(rate))
    # The REW IR importer derives the rate as 1/dt over decimal timestamps,
    # so a nominal integer rate seals as a slightly-off float (e.g.
    # 48000.0768). Accepting only within parse-noise tolerance keeps a
    # genuinely fractional declared rate (44100.5) failing closed while
    # naming the nominal integer lane the samples were captured on.
    if rounded < 1 or abs(rate - rounded) > 1e-5 * rate:
        raise ValueError(
            'auralization requires a nominally integral sample rate; the '
            f'measured IR is pinned at {rate!r} Hz'
        )
    pcm = np.asarray(dataset.amplitudes, dtype='<f8')
    if not np.all(np.isfinite(pcm)):
        raise ValueError('measured IR amplitudes must all be finite')
    absolute = (
        dataset.amplitude_reference == 'full_scale'
        and dataset.calibration_state == 'calibrated'
        and not dataset.normalized
    )
    return ImpulseAuthorityRef(
        kind='measured',
        artifact_id=dataset.dataset_id,
        artifact_sha256=dataset.dataset_sha256,
        decoded_pcm_sha256=sha256(pcm.tobytes()).hexdigest(),
        sample_rate_hz=rounded,
        sample_count=int(pcm.size),
        channel_count=1,
        absolute_amplitude_authority=absolute,
    )


_PCM_DIVISORS = {
    # Integer full-scale divisor per integer-PCM sample width.
    1: 128.0,
    2: 32768.0,
    3: 8388608.0,
    4: 2147483648.0,
}


def decode_dry_program_wav(raw: bytes) -> tuple[tuple[float, ...], int]:
    """Decode a mono integer-PCM WAV into float64 samples on ±1.0.

    Complement of :func:`encode_wav_pcm_s16le` — the reverse-adaptation
    boundary for the dry-programme leg. Only uncompressed integer PCM is
    accepted (µ-law/ALaw/float formats have no import authority here), and
    only mono: downmixing multichannel dry material is a lossy product
    decision, not an honest fixed-point adaptation.
    """

    try:
        with wave.open(io.BytesIO(raw), 'rb') as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            frames = wav.getnframes()
            comptype = wav.getcomptype()
            if comptype != 'NONE':
                raise ValueError(
                    f'dry program WAV must be uncompressed PCM; got {comptype}'
                )
            if channels != 1:
                raise ValueError(
                    'dry program WAV must be mono; got '
                    f'{channels} channels — downmixing is a product decision'
                )
            divisor = _PCM_DIVISORS.get(width)
            if divisor is None:
                raise ValueError(
                    'dry program WAV must be 8/16/24/32-bit integer PCM; '
                    f'got {width}-byte samples'
                )
            payload = wav.readframes(frames)
    except wave.Error as exc:
        raise ValueError(f'invalid dry program WAV bytes: {exc}') from exc

    if width == 3:
        raw_samples = np.frombuffer(payload, dtype=np.uint8)
        if raw_samples.size % 3:
            raise ValueError('24-bit WAV payload is not sample-aligned')
        grouped = raw_samples.reshape(-1, 3).astype(np.int32)
        ints = grouped[:, 0] | (grouped[:, 1] << 8) | (grouped[:, 2] << 16)
        ints = np.where(ints >= 1 << 23, ints - (1 << 24), ints)
    else:
        dtype = {1: np.uint8, 2: '<i2', 4: '<i4'}[width]
        ints = np.frombuffer(payload, dtype=dtype).astype(np.int64)
        if width == 1:
            ints = ints - 128
    samples = ints.astype(np.float64) / divisor
    return tuple(float(sample) for sample in samples.tolist()), int(rate)


def register_dry_program_asset(
    assets: ManagedAssetStore,
    raw: bytes,
) -> tuple[DryProgramAssetRef, tuple[float, ...], int]:
    """Decode + install dry programme bytes; return ref, samples, rate.

    ``ManagedAssetStore.ensure_installed`` persists the canonical bytes
    content-addressed; the returned :class:`DryProgramAssetRef` pins the
    byte digest the artifact seals. ``program_level_authority`` stays
    ``'unknown'`` — imported WAV bytes carry no level-calibration claim.
    """

    samples, rate = decode_dry_program_wav(raw)
    if not samples:
        raise ValueError('dry program WAV contains no samples')
    digest = sha256(raw).hexdigest()
    assets.ensure_installed(digest, raw)
    ref = DryProgramAssetRef(
        asset_sha256=digest,
        decoded_pcm_sha256=sha256(
            np.asarray(samples, dtype='<f8').tobytes()
        ).hexdigest(),
        sample_rate_hz=rate,
        sample_count=len(samples),
        channel_count=1,
        program_level_authority='unknown',
    )
    return ref, samples, rate


def build_measured_render_spec(
    measurement_repository: CadMeasurementRepository,
    *,
    ir_dataset: CadImpulseResponseDataset,
    dry_ref: DryProgramAssetRef,
    output_sample_rate_hz: int,
    gain_policy: GainPolicy,
    rms_target_dbfs: float | None = None,
    resample_policy: ResamplePolicy = 'exact_rate_match_required',
    headroom_policy: HeadroomPolicy = 'report_only',
) -> tuple[AuralizationRenderSpec, CadMeasurementRecord, ImpulseAuthorityRef]:
    """Pin an auralization spec from the measurement authority chain.

    The measured IR belongs to a persisted :class:`CadMeasurementRecord`:
    ``get_measurement`` re-derives the record's revision authority
    (document_id/scene_revision_id/scene_content_hash) and verifies the
    measured receiver position still resolves against it, so the returned
    spec is pinned to the exact measured revision. The measurement record is
    the source-scenario authority — it seals source_speaker_ids /
    channel_role / routing_evidence — and the measured entity is the
    receiver (``receiver_id = entity_id`` matches the scene binding
    convention).
    """

    measurement = measurement_repository.get_measurement(ir_dataset.measurement_id)
    if measurement is None:
        raise ValueError(
            f'IR dataset measurement is not persisted: {ir_dataset.measurement_id}'
        )
    impulse_authority = resolve_impulse_authority(ir_dataset)
    receiver = AuralizationReceiverRef(
        receiver_id=measurement.measurement_entity_id,
        receiver_entity_id=measurement.measurement_entity_id,
    )
    spec = build_auralization_render_spec(
        document_id=measurement.document_id,
        scene_revision_id=measurement.scene_revision_id,
        scene_content_hash=measurement.scene_content_hash,
        system_variant_id=None,
        system_variant_sha256=None,
        source_scenario_id=measurement.measurement_id,
        source_scenario_sha256=measurement_sha256(measurement),
        receiver=receiver,
        impulse_authority=impulse_authority,
        dry_source=dry_ref,
        output_sample_rate_hz=output_sample_rate_hz,
        gain_policy=gain_policy,
        rms_target_dbfs=rms_target_dbfs,
        resample_policy=resample_policy,
        headroom_policy=headroom_policy,
    )
    return spec, measurement, impulse_authority


def materialize_measured_auralization(
    auralization_repository: CadAuralizationRepository,
    spec: AuralizationRenderSpec,
    *,
    dry_samples: tuple[float, ...],
    ir_dataset: CadImpulseResponseDataset,
) -> tuple[AuralizationArtifact, bytes]:
    """Render one spec against the measured IR and persist the artifact.

    The renderer re-verifies the spec-pinned asset/artifact digests against
    the exact samples supplied, so the dry samples must be the decode of the
    ``dry_source`` bytes and ``ir_dataset`` must be the artifact
    ``impulse_authority`` resolved — anything else fails closed. The spec is
    persisted on first use; artifacts are append-only, so re-materializing
    the same spec against changed inputs yields a new sealed artifact.
    """

    pcm = render_auralization(
        spec,
        dry_samples=dry_samples,
        impulse_samples=ir_dataset.amplitudes,
        dry_asset_sha256=spec.dry_source.asset_sha256,
        impulse_artifact_sha256=spec.impulse_authority.artifact_sha256,
    )
    artifact, wav = build_auralization_artifact(spec, pcm)
    if auralization_repository.get_render_spec(spec.spec_id) is None:
        auralization_repository.save_render_spec(spec)
    auralization_repository.save_artifact(artifact, wav)
    return artifact, wav


__all__ = [
    'resolve_impulse_authority',
    'decode_dry_program_wav',
    'register_dry_program_asset',
    'build_measured_render_spec',
    'materialize_measured_auralization',
]
