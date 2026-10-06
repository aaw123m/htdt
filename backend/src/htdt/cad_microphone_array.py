"""Microphone-array spatial-sampling / beamforming authority (#658).

Synchronized microphone channels with poses and calibration (#266)
do not prove the array geometry can reconstruct/localize the
requested field: spatial aliasing, aperture-limited resolution,
grating lobes and calibration/position errors are geometry- and
frequency-dependent. These records pin the array geometry, the
per-band sampling capability and the beamforming/reconstruction
transform so a smooth heatmap is never mistaken for spatial truth.

Basis: IEEE spatial-aliasing analysis (inter-element spacing vs
wavelength); Cigada et al., JASA 124(6) 2008 (moving arrays,
low-frequency resolution vs high-frequency aliasing tradeoff);
Chiariotti et al. 2019 beamforming review (calibration/uncertainty
as part of result identity); Rafaely 2023 spherical-array spatial
sampling review; IEC 60268-4:2018 scope limit (sound-system
microphones — NOT measurement microphones).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{sha_field: digest, id_field: _semantic_id(prefix, digest)},
    )


ArrayTopology = Literal[
    'linear',
    'planar',
    'spherical',
    'irregular',
    'distributed',
    'unknown',
]

ArraySyncCapability = Literal[
    'magnitude_only_array',
    'relative_phase_array',
    'coherent_beamforming_eligible',
    'spatial_reconstruction_eligible',
    'unknown',
]

BandSamplingState = Literal[
    'low_frequency_resolution_limited',
    'well_sampled',
    'spatial_aliasing_risk',
    'grating_lobe_ambiguity',
    'outside_sensor_band',
    'unknown',
]

PropagationModel = Literal[
    'far_field_plane_wave',
    'near_field_point_source',
    'near_field_focused_surface',
    'room_reflection_path',
    'spherical_harmonic_field',
    'custom',
    'unknown',
]

BeamformingAlgorithm = Literal[
    'delay_and_sum',
    'frequency_domain_beamforming',
    'mvdr_adaptive',
    'damas_deconvolution',
    'clean_like',
    'spherical_harmonic_beamforming',
    'sound_field_reconstruction',
    'time_domain_localization',
    'custom_validated_method',
    'external_imported',
    'unknown',
]

SpatialOutputState = Literal[
    'direction_supported',
    'multiple_paths_unresolved',
    'spatial_alias_ambiguous',
    'temporal_overlap_limited',
    'insufficient_array_aperture',
    'unknown',
]


class ArrayBandCapability(BaseModel):
    """Per-frequency-band spatial sampling state (#658 §4-§5).

    Aliasing and resolution are separate limitations: an alias-free
    array can still be too small to resolve two directions.
    """

    model_config = ConfigDict(frozen=True)

    band_hz: tuple[float, float]
    state: BandSamplingState
    angular_resolution_deg: float | None = None


class MicrophoneArrayGeometry(BaseModel):
    """First-class array geometry identity (#658 §2).

    Member channels, surveyed positions/orientations, uncertainties,
    aperture and topology. A moved element produces a new geometry
    identity (the seal hashes the members). #266 stays canonical for
    the raw channels; this record consumes them.
    """

    model_config = ConfigDict(frozen=True)

    geometry_id: str
    geometry_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    topology: ArrayTopology
    member_refs: tuple[AuthorityRef, ...]
    member_positions_m: tuple[tuple[float, float, float], ...]
    member_orientations: tuple[str, ...] | None = None
    position_uncertainty_m: float | None = None
    aperture_m: float | None = None
    min_spacing_m: float | None = None
    max_spacing_m: float | None = None
    origin_ref: AuthorityRef | None = None
    mount_frame: str | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('topology') in (None, 'unknown'):
                raise ValueError(
                    'array topology must be declared — linear, planar, '
                    'spherical, irregular or distributed'
                )
            members = data.get('member_refs') or ()
            positions = data.get('member_positions_m') or ()
            if len(members) < 2:
                raise ValueError(
                    'a microphone array requires at least two member '
                    'channels — N synchronized microphones alone are '
                    'not spatial resolution'
                )
            if len(positions) != len(members):
                raise ValueError(
                    'every member channel requires its surveyed '
                    'position — nominal jig coordinates are not '
                    'as-built truth'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'geometry_id', 'geometry_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'MicrophoneArrayGeometry':
        return _seal(
            cls, payload, 'geometry_id', 'geometry_sha256', 'mag'
        )


class SpatialSamplingCapability(BaseModel):
    """What the array can spatially support per band (#658 §3-§6).

    Binds #609-grade timing (magnitude-only vs coherent), the
    per-band sampling states and the propagation model the steering
    assumes. Far-field steering is not applied silently to a
    near-field focus.
    """

    model_config = ConfigDict(frozen=True)

    capability_id: str
    capability_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    array_ref: AuthorityRef
    sync_capability: ArraySyncCapability
    propagation_model: PropagationModel
    band_capabilities: tuple[ArrayBandCapability, ...]
    channel_latency_skew_s: float | None = None
    phase_calibrated: bool | None = None
    position_uncertainty_m: float | None = None
    timing_evidence_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('array_ref') is None:
                raise ValueError(
                    'a sampling capability requires its array '
                    'geometry record'
                )
            if data.get('sync_capability') in (None, 'unknown'):
                raise ValueError(
                    'channel synchronization/phase capability must be '
                    'declared — sequential or asynchronous samples '
                    'are not coherent spatial samples'
                )
            if data.get('propagation_model') in (None, 'unknown'):
                raise ValueError(
                    'the propagation model must be declared — '
                    'far-field vs near-field changes the steering '
                    'vectors'
                )
            if not data.get('band_capabilities'):
                raise ValueError(
                    'spatial capability is frequency-dependent — no '
                    'global array-valid flag'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'capability_id', 'capability_sha256'},
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'SpatialSamplingCapability':
        return _seal(
            cls, payload, 'capability_id', 'capability_sha256', 'ssc'
        )


class BeamformingTransform(BaseModel):
    """One beamforming/reconstruction transform (#658 §7-§8, §11-§12).

    Algorithm, steering model, bands, regularization, spatial grid
    and the input channel set are part of the result identity; the
    output state carries the unresolved/alias/aperture limitation
    instead of a falsely precise direction.
    """

    model_config = ConfigDict(frozen=True)

    transform_id: str
    transform_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    array_ref: AuthorityRef
    capability_ref: AuthorityRef
    algorithm: BeamformingAlgorithm
    steering_model: PropagationModel
    output_state: SpatialOutputState
    frequency_bands_hz: tuple[tuple[float, float], ...] | None = None
    regularization: str | None = None
    spatial_grid_ref: AuthorityRef | None = None
    normalization: str | None = None
    input_channel_refs: tuple[AuthorityRef, ...] | None = None
    psf_evidence_ref: AuthorityRef | None = None
    result_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('array_ref') is None:
                raise ValueError(
                    'a beamforming transform requires its array '
                    'geometry'
                )
            if data.get('capability_ref') is None:
                raise ValueError(
                    'a beamforming transform requires the spatial '
                    'sampling capability it ran under'
                )
            if data.get('algorithm') in (None, 'unknown'):
                raise ValueError(
                    'the beamforming/reconstruction algorithm must be '
                    'declared — beamformed = true is not a method'
                )
            if data.get('steering_model') in (None, 'unknown'):
                raise ValueError(
                    'the steering/focusing propagation model must be '
                    'declared'
                )
            if data.get('output_state') in (None, 'unknown'):
                raise ValueError(
                    'the output qualification state must be declared'
                )
            if (
                data.get('output_state') == 'direction_supported'
                and data.get('psf_evidence_ref') is None
            ):
                raise ValueError(
                    'a supported direction claim requires point-spread/'
                    'sidelobe evidence — a sharp heatmap pixel is not '
                    'exact localization'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'transform_id', 'transform_sha256'},
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'BeamformingTransform':
        return _seal(
            cls, payload, 'transform_id', 'transform_sha256', 'bft'
        )


SpatialClaimVerdict = Literal[
    'direction_supported',
    'sync_insufficient',
    'spatial_alias_ambiguous',
    'aperture_limited',
    'model_mismatch',
    'multiple_paths_unresolved',
    'temporal_overlap_limited',
    'insufficient_array_aperture',
    'unsupported_output',
    'no_transform',
]


def evaluate_spatial_claim(
    geometry: MicrophoneArrayGeometry | None,
    capability: SpatialSamplingCapability | None,
    transform: BeamformingTransform | None,
) -> tuple[SpatialClaimVerdict, str]:
    """Judge whether a beamforming result supports a direction/
    localization/reconstruction claim (#658)."""
    if transform is None:
        return (
            'no_transform',
            'no beamforming/reconstruction record — synchronized '
            'channels alone are not spatial evidence',
        )
    if geometry is None or capability is None:
        return (
            'unsupported_output',
            'array geometry or sampling capability unpinned — the '
            'result identity is incomplete',
        )
    if capability.sync_capability in (
        'magnitude_only_array', 'relative_phase_array',
    ):
        return (
            'sync_insufficient',
            'channels lack coherent timing/phase evidence — '
            'beamforming requires coherent sampling capability',
        )
    if transform.steering_model != capability.propagation_model:
        return (
            'model_mismatch',
            'the steering model disagrees with the declared '
            'propagation model — far-field steering on a near-field '
            'source biases direction and range',
        )
    states = {
        band.state for band in capability.band_capabilities
    }
    if 'spatial_aliasing_risk' in states or (
        'grating_lobe_ambiguity' in states
    ):
        return (
            'spatial_alias_ambiguous',
            'a band carries spatial-aliasing/grating-lobe ambiguity — '
            'no false exact direction',
        )
    if 'low_frequency_resolution_limited' in states:
        return (
            'aperture_limited',
            'the aperture is resolution-limited in a requested band — '
            'alias-free does not mean resolved',
        )
    if transform.output_state != 'direction_supported':
        mapping: dict[str, SpatialClaimVerdict] = {
            'multiple_paths_unresolved': 'multiple_paths_unresolved',
            'spatial_alias_ambiguous': 'spatial_alias_ambiguous',
            'temporal_overlap_limited': 'temporal_overlap_limited',
            'insufficient_array_aperture': 'insufficient_array_aperture',
        }
        return (
            mapping.get(transform.output_state, 'unsupported_output'),
            f'transform output state: {transform.output_state}',
        )
    return (
        'direction_supported',
        'array geometry, coherent timing and point-spread evidence '
        'bound — direction claim qualified',
    )


SPATIAL_CLAIM_LABELS: dict[str, str] = {
    'direction_supported': '方向推定適格',
    'sync_insufficient': '同期不足',
    'spatial_alias_ambiguous': '空間エイリアス曖昧',
    'aperture_limited': '開口限定',
    'model_mismatch': 'モデル不一致',
    'multiple_paths_unresolved': '複数経路未分離',
    'temporal_overlap_limited': '時間重複限定',
    'insufficient_array_aperture': '開口不足',
    'unsupported_output': '非対応出力',
    'no_transform': '変換なし',
}
