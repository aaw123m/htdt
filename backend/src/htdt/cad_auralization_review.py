"""Capability-bound auralization review packages (#538).

#515 owns the deterministic mono render (:mod:`htdt.cad_auralization`),
#784 the comparison player state (:mod:`htdt.cad_auralization_player`),
#518 blinded listening sessions (:mod:`htdt.cad_listening_session`). This
module owns the *evidence and shareability* layer the issue asks for:

- :class:`AuralizationRoutingDeclaration` — the typed multi-source/stem
  routing authority: every stem names its dry source, impulse authority,
  output channel, gain, delay and filter identity explicitly. There are
  no silent defaults: a field the caller did not declare is absent, never
  assumed.
- :class:`AuralizationCapability` — the sealed capability/confidence
  record preserved on every auralization result and package. Its
  confidence state is bounded by the evidence of the underlying
  solver/measurement path: renderer polish can never upgrade it.
- :class:`MeasuredPredictedListeningValidation` — the per-band
  measured-vs-predicted agreement record for one binding; honestly
  ``no_measured_reference`` when the measured leg does not exist.
- :class:`AuralizationReviewPackage` +
  :func:`build_review_package` / :func:`verify_review_package` — the
  deterministic, provenance-safe shareable package. The package fails
  closed: a missing IR/asset reference or an unverified member produces
  no package at all, never a partial-truth one.
"""

from __future__ import annotations

import html
import io
import json
from hashlib import sha256
from math import isfinite, log10, sqrt
import zipfile
from typing import Any, Literal

import numpy as np
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .cad_auralization import (
    AuralizationArtifact,
    AuralizationRenderSpec,
    DryProgramAssetRef,
    ImpulseAuthorityRef,
    LevelSemantics,
    RenderedPcm,
)
from .cad_ir_analysis import _fft_bandpass
from .canonical_json import (
    canonical_json as _canonical,
    canonical_sha256 as _digest,
    canonicalize_payload,
)


AURALIZATION_REVIEW_SCHEMA_VERSION = 1
CAPABILITY_AUTHORITY_VERSION = 'auralization-capability-1'
ROUTING_AUTHORITY_VERSION = 'auralization-routing-1'
LISTENING_VALIDATION_AUTHORITY_VERSION = 'auralization-listening-validation-1'
REVIEW_PACKAGE_AUTHORITY_VERSION = 'auralization-review-package-1'
BAND_AGREEMENT_ANALYSIS_METHOD = 'htdt.aur538_band_agreement_v1'
_PACKAGE_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


IrOrigin = Literal['measured', 'predicted', 'hybrid']
AuralizationConfidenceState = Literal[
    'measured_reference',
    'predicted_validated_for_declared_domain',
    'predicted_with_limitations',
    'demonstration_only',
    'unvalidated',
]
"""Bounded user-facing confidence states (#538 section 2). A polished
render never upgrades the state: only the declared evidence bound to the
capability can. ``unvalidated`` is the honest floor — it is a recorded
claim that no validation evidence exists, not silence."""

AcousticAuthorityState = Literal['measured', 'declared', 'none', 'unknown']
"""Directivity/material/scattering/diffraction authority of the chain
behind one render. ``unknown`` is the default honesty state — it is a
recorded absence of evidence, never an implicit claim."""

HrtfProcessing = Literal['none', 'generic', 'individualized', 'unknown']

BandFraction = Literal['octave', 'third_octave']

ComparisonState = Literal['computed', 'partial', 'no_measured_reference']


def _finite(value: float, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


def _sha256_bytes(data: bytes) -> str:
    return sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Multi-source routing declaration
# ---------------------------------------------------------------------------


class AuralizationStemRoute(BaseModel):
    """One source/stem → output-channel route. Every field is explicit:
    gain, delay and filter identity are declared per stem, never left to a
    hidden mixer default."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    stem_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    dry_source: DryProgramAssetRef
    impulse_authority: ImpulseAuthorityRef
    output_channel: int = Field(ge=0)
    gain_db: float
    delay_ms: float = Field(ge=0.0)
    filter_identity: str = Field(min_length=1)
    """Exact filter/processing identity on the stem path, or ``'none'``
    when the path is unfiltered."""

    @field_validator('gain_db', 'delay_ms')
    @classmethod
    def finite_numbers(cls, value: float) -> float:
        return _finite(value, field_name='stem gain/delay')


class AuralizationRoutingDeclaration(BaseModel):
    """Sealed routing authority for a multi-source auralization session.

    The matrix is the whole truth of the mix: each stem's source, dry
    program, impulse authority, channel, gain, delay and filter identity
    is enumerated; anything not listed here does not exist in the mix.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = AURALIZATION_REVIEW_SCHEMA_VERSION
    authority_version: Literal[
        'auralization-routing-1'
    ] = ROUTING_AUTHORITY_VERSION
    routing_id: str = Field(pattern=r'^auralization-routing:[0-9a-f]{64}$')
    routing_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    document_id: str = Field(min_length=1)
    output_sample_rate_hz: int = Field(gt=0)
    output_layout: Literal['mono_mix', 'stereo', 'multichannel']
    stems: tuple[AuralizationStemRoute, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def validate_routing(self) -> 'AuralizationRoutingDeclaration':
        stem_ids = [stem.stem_id for stem in self.stems]
        if len(set(stem_ids)) != len(stem_ids):
            raise ValueError('routing stems must have unique stem ids')
        source_ids = [stem.source_id for stem in self.stems]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError(
                'one source id may only feed one stem — a shared physical '
                'source still needs distinct stems per output path'
            )
        if self.output_layout == 'mono_mix':
            if any(stem.output_channel != 0 for stem in self.stems):
                raise ValueError('mono_mix routing has a single output channel')
        elif self.output_layout == 'stereo':
            if any(stem.output_channel > 1 for stem in self.stems):
                raise ValueError('stereo routing has output channels 0/1 only')
        expected = _digest(self.semantic_payload())
        if self.routing_sha256 != expected:
            raise ValueError('auralization routing semantic hash mismatch')
        if self.routing_id != f'auralization-routing:{expected}':
            raise ValueError('auralization routing id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'routing_id', 'routing_sha256'},
        )


def build_auralization_routing(**kwargs: Any) -> AuralizationRoutingDeclaration:
    probe = AuralizationRoutingDeclaration.model_construct(
        **canonicalize_payload(AuralizationRoutingDeclaration, dict(
            routing_id='auralization-routing:' + '0' * 64,
            routing_sha256='0' * 64,
            **kwargs,
        ))
    )
    digest = _digest(probe.semantic_payload())
    return AuralizationRoutingDeclaration(
        routing_id=f'auralization-routing:{digest}',
        routing_sha256=digest,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# AuralizationCapability authority
# ---------------------------------------------------------------------------


class ValidatedBand(BaseModel):
    """One validated frequency region backed by an external evidence
    reference (validation record, measurement comparison, benchmark run).
    A band without an evidence id is not a validated band."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    band_center_hz: float = Field(gt=0.0)
    band_fraction: BandFraction
    evidence_id: str = Field(min_length=1)

    @field_validator('band_center_hz')
    @classmethod
    def finite_band(cls, value: float) -> float:
        return _finite(value, field_name='band center')


class AuralizationCapability(BaseModel):
    """Sealed capability record preserved on every auralization
    result/package (#538 section 1).

    The record derives its render-chain facts from the exact render spec
    and artifact it is bound to — IR hash, scene revision/variant,
    receiver, sample rate, level semantics — and carries the declared
    evidence claims (solver identity, validated bands, phenomenon
    capability, HRTF/headphone path, limitations) as explicit fields.
    Unknown remains UNKNOWN: absent authority states are ``'unknown'``,
    absent HRTF is ``'none'``/``'unknown'``, never silently generic.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = AURALIZATION_REVIEW_SCHEMA_VERSION
    authority_version: Literal[
        'auralization-capability-1'
    ] = CAPABILITY_AUTHORITY_VERSION
    capability_id: str = Field(
        pattern=r'^auralization-capability:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    system_variant_id: str | None = Field(default=None, min_length=1)
    system_variant_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    spec_id: str = Field(pattern=r'^auralization-render-spec:[0-9a-f]{64}$')
    spec_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    artifact_id: str = Field(pattern=r'^auralization-artifact:[0-9a-f]{64}$')
    artifact_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    ir_origin: IrOrigin
    impulse_authority: ImpulseAuthorityRef
    prediction_measurement_identity: str = Field(min_length=1)
    """Exact measurement or prediction-run identity producing the IR."""
    ir_producer_id: str = Field(min_length=1)
    """Producer chain, e.g. ``htdt.rew_ir_import`` or a solver id."""
    ir_producer_version: str = Field(min_length=1)
    renderer_id: str = Field(min_length=1)
    renderer_version: str = Field(min_length=1)

    validated_bands: tuple[ValidatedBand, ...] = ()
    phenomenon_capabilities: tuple[str, ...] = ()
    source_directivity_authority: AcousticAuthorityState
    material_authority: AcousticAuthorityState
    scattering_authority: AcousticAuthorityState
    diffraction_authority: AcousticAuthorityState
    listener_pose_id: str | None = Field(default=None, min_length=1)
    """Optional listener-pose authority binding head orientation."""

    hrtf_processing: HrtfProcessing
    hrtf_dataset_id: str | None = Field(default=None, min_length=1)
    hrtf_dataset_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    hrtf_subject_id: str | None = Field(default=None, min_length=1)
    hrtf_license_id: str | None = Field(default=None, min_length=1)
    headphone_compensation_id: str | None = Field(
        default=None, min_length=1
    )

    output_sample_rate_hz: int = Field(gt=0)
    resample_method: str | None = Field(default=None, min_length=1)
    level_semantics: LevelSemantics
    applied_gain_db: float
    routing_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    known_limitations: tuple[str, ...] = ()
    confidence_state: AuralizationConfidenceState

    @model_validator(mode='after')
    def validate_capability(self) -> 'AuralizationCapability':
        _finite(self.applied_gain_db, field_name='applied gain')
        if (self.system_variant_id is None) != (self.system_variant_sha256 is None):
            raise ValueError('system variant id/hash must be supplied together')
        # IR origin honesty: 'measured' can only sit on a measured impulse
        # authority, 'predicted' on a predicted one; 'hybrid' is an explicit
        # composition claim that must state its known limitations.
        if self.ir_origin == 'measured':
            if self.impulse_authority.kind != 'measured':
                raise ValueError(
                    'a measured capability claim requires a measured impulse '
                    'authority'
                )
        elif self.ir_origin == 'predicted':
            if self.impulse_authority.kind != 'predicted':
                raise ValueError(
                    'a predicted capability claim requires a predicted impulse '
                    'authority'
                )
        elif not self.known_limitations:
            raise ValueError(
                'a hybrid IR claim must declare the known limitations of its '
                'composition'
            )
        # HRTF group: declared processing requires full provenance; none/
        # unknown carries no HRTF fields at all.
        if self.hrtf_processing in ('generic', 'individualized'):
            if self.hrtf_dataset_id is None or self.hrtf_dataset_sha256 is None:
                raise ValueError(
                    'a declared HRTF path requires dataset id and hash'
                )
            if self.hrtf_license_id is None:
                raise ValueError(
                    'a declared HRTF path requires license provenance'
                )
            if (
                self.hrtf_processing == 'individualized'
                and self.hrtf_subject_id is None
            ):
                raise ValueError(
                    'individualized HRTF requires a subject identity — a '
                    'generic dataset can never claim personal localization'
                )
        else:
            if any(
                value is not None
                for value in (
                    self.hrtf_dataset_id,
                    self.hrtf_dataset_sha256,
                    self.hrtf_subject_id,
                    self.hrtf_license_id,
                )
            ):
                raise ValueError(
                    'HRTF fields are only valid with a declared HRTF path'
                )
        # Confidence state is bounded by evidence:
        # - measured_reference only on a fully measured IR chain;
        # - validated_for_declared_domain requires declared validated bands;
        # - predicted_with_limitations requires declared limitations;
        # - demonstration_only / unvalidated may not claim validated bands.
        if self.confidence_state == 'measured_reference':
            if self.ir_origin != 'measured':
                raise ValueError(
                    'measured_reference requires a measured IR origin'
                )
        elif self.confidence_state == (
            'predicted_validated_for_declared_domain'
        ):
            if self.ir_origin not in ('predicted', 'hybrid'):
                raise ValueError(
                    'validated-domain prediction requires a predicted or '
                    'hybrid IR origin'
                )
            if not self.validated_bands:
                raise ValueError(
                    'predicted_validated_for_declared_domain requires at '
                    'least one evidence-backed validated band'
                )
        elif self.confidence_state == 'predicted_with_limitations':
            if self.ir_origin not in ('predicted', 'hybrid'):
                raise ValueError(
                    'predicted_with_limitations requires a predicted or '
                    'hybrid IR origin'
                )
            if not self.known_limitations:
                raise ValueError(
                    'predicted_with_limitations must state its limitations'
                )
        else:
            if self.validated_bands:
                raise ValueError(
                    'demonstration_only/unvalidated cannot claim validated '
                    'bands'
                )
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('auralization capability semantic hash mismatch')
        if self.capability_id != f'auralization-capability:{expected}':
            raise ValueError('auralization capability id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'capability_id', 'semantic_sha256'},
        )


def build_auralization_capability(
    *,
    spec: AuralizationRenderSpec,
    artifact: AuralizationArtifact,
    routing: AuralizationRoutingDeclaration,
    ir_origin: IrOrigin,
    prediction_measurement_identity: str,
    ir_producer_id: str,
    ir_producer_version: str,
    validated_bands: tuple[ValidatedBand, ...] = (),
    phenomenon_capabilities: tuple[str, ...] = (),
    source_directivity_authority: AcousticAuthorityState = 'unknown',
    material_authority: AcousticAuthorityState = 'unknown',
    scattering_authority: AcousticAuthorityState = 'unknown',
    diffraction_authority: AcousticAuthorityState = 'unknown',
    listener_pose_id: str | None = None,
    hrtf_processing: HrtfProcessing,
    hrtf_dataset_id: str | None = None,
    hrtf_dataset_sha256: str | None = None,
    hrtf_subject_id: str | None = None,
    hrtf_license_id: str | None = None,
    headphone_compensation_id: str | None = None,
    known_limitations: tuple[str, ...] = (),
    confidence_state: AuralizationConfidenceState,
) -> AuralizationCapability:
    """Seal a capability record onto one exact render spec + artifact.

    The artifact must belong to the spec and pin the same impulse
    authority — anything else fails closed rather than recording a
    capability against unrelated evidence.
    """
    if artifact.spec_id != spec.spec_id or (
        artifact.spec_semantic_sha256 != spec.semantic_sha256
    ):
        raise ValueError('capability artifact does not belong to the spec')
    if artifact.impulse_artifact_sha256 != (
        spec.impulse_authority.artifact_sha256
    ):
        raise ValueError(
            'capability artifact pins a different impulse authority'
        )
    kwargs: dict[str, Any] = dict(
        document_id=spec.document_id,
        scene_revision_id=spec.scene_revision_id,
        scene_content_hash=spec.scene_content_hash,
        system_variant_id=spec.system_variant_id,
        system_variant_sha256=spec.system_variant_sha256,
        spec_id=spec.spec_id,
        spec_semantic_sha256=spec.semantic_sha256,
        artifact_id=artifact.artifact_id,
        artifact_semantic_sha256=artifact.semantic_sha256,
        ir_origin=ir_origin,
        impulse_authority=spec.impulse_authority,
        prediction_measurement_identity=prediction_measurement_identity,
        ir_producer_id=ir_producer_id,
        ir_producer_version=ir_producer_version,
        renderer_id=artifact.renderer_id,
        renderer_version=artifact.renderer_version,
        validated_bands=tuple(validated_bands),
        phenomenon_capabilities=tuple(phenomenon_capabilities),
        source_directivity_authority=source_directivity_authority,
        material_authority=material_authority,
        scattering_authority=scattering_authority,
        diffraction_authority=diffraction_authority,
        listener_pose_id=listener_pose_id,
        hrtf_processing=hrtf_processing,
        hrtf_dataset_id=hrtf_dataset_id,
        hrtf_dataset_sha256=hrtf_dataset_sha256,
        hrtf_subject_id=hrtf_subject_id,
        hrtf_license_id=hrtf_license_id,
        headphone_compensation_id=headphone_compensation_id,
        output_sample_rate_hz=artifact.sample_rate_hz,
        resample_method=artifact.resample_method,
        level_semantics=artifact.level_semantics,
        applied_gain_db=artifact.applied_gain_db,
        routing_sha256=routing.routing_sha256,
        known_limitations=tuple(known_limitations),
        confidence_state=confidence_state,
    )
    probe = AuralizationCapability.model_construct(
        **canonicalize_payload(AuralizationCapability, dict(
            capability_id='auralization-capability:' + '0' * 64,
            semantic_sha256='0' * 64,
            **kwargs,
        ))
    )
    digest = _digest(probe.semantic_payload())
    return AuralizationCapability(
        capability_id=f'auralization-capability:{digest}',
        semantic_sha256=digest,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Measured-vs-predicted listening validation
# ---------------------------------------------------------------------------


class BandAgreementMetric(BaseModel):
    """Per-band agreement between a measured reference render and a
    predicted render on the same binding. ``insufficient_signal`` is an
    honest state: a band whose band-limited energy is below the declared
    floor on either side reports no metric, never a fabricated 0 dB."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    band_center_hz: float = Field(gt=0.0)
    band_fraction: BandFraction
    state: Literal['computed', 'insufficient_signal']
    measured_level_dbfs: float | None = None
    predicted_level_dbfs: float | None = None
    level_difference_db: float | None = None
    waveform_correlation: float | None = None

    @model_validator(mode='after')
    def validate_metric(self) -> 'BandAgreementMetric':
        metrics = (
            self.measured_level_dbfs,
            self.predicted_level_dbfs,
            self.level_difference_db,
            self.waveform_correlation,
        )
        if self.state == 'computed':
            if any(value is None for value in metrics):
                raise ValueError('a computed band requires all metrics')
            for value in metrics:
                _finite(float(value), field_name='band metric')
            if not (-1.0 <= float(self.waveform_correlation) <= 1.0):
                raise ValueError('waveform correlation must be in [-1, 1]')
        elif any(value is not None for value in metrics):
            raise ValueError(
                'an insufficient-signal band reports no metrics'
            )
        return self


class MeasuredPredictedListeningValidation(BaseModel):
    """Immutable measured-vs-predicted listening comparison for one
    binding (#538 section 7).

    The record reports per-band agreement metrics between the measured
    reference render and the predicted render — never an authenticity
    verdict. ``no_measured_reference`` is the explicit honest state when
    the binding has no measured leg.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = AURALIZATION_REVIEW_SCHEMA_VERSION
    authority_version: Literal[
        'auralization-listening-validation-1'
    ] = LISTENING_VALIDATION_AUTHORITY_VERSION
    validation_id: str = Field(
        pattern=r'^auralization-listening-validation:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    document_id: str = Field(min_length=1)
    source_scenario_id: str = Field(min_length=1)
    receiver_id: str = Field(min_length=1)
    measured_artifact_id: str | None = Field(
        default=None, pattern=r'^auralization-artifact:[0-9a-f]{64}$'
    )
    measured_artifact_semantic_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    predicted_artifact_id: str = Field(
        pattern=r'^auralization-artifact:[0-9a-f]{64}$'
    )
    predicted_artifact_semantic_sha256: str = Field(
        pattern=r'^[0-9a-f]{64}$'
    )
    sample_rate_hz: int = Field(gt=0)
    alignment_semantics: str = Field(min_length=1)
    """Declared IR/render alignment method (e.g. 'direct_arrival_aligned',
    'time_zero_aligned', 'unaligned') — recorded, not computed."""
    level_matching_method: str = Field(min_length=1)
    """Declared level-matching method identity applied before comparison."""
    comparison_state: ComparisonState
    bands: tuple[BandAgreementMetric, ...] = ()
    audible_limitations: tuple[str, ...] = ()
    analysis_method: Literal[
        'htdt.aur538_band_agreement_v1'
    ] = BAND_AGREEMENT_ANALYSIS_METHOD

    @model_validator(mode='after')
    def validate_record(self) -> 'MeasuredPredictedListeningValidation':
        if (self.measured_artifact_id is None) != (
            self.measured_artifact_semantic_sha256 is None
        ):
            raise ValueError('measured artifact id/hash supplied together')
        if self.comparison_state == 'no_measured_reference':
            if self.measured_artifact_id is not None:
                raise ValueError(
                    'no_measured_reference cannot bind a measured artifact'
                )
            if self.bands:
                raise ValueError(
                    'no_measured_reference cannot report band metrics'
                )
        else:
            if self.measured_artifact_id is None:
                raise ValueError(
                    'a computed/partial comparison requires the measured '
                    'reference artifact'
                )
            if not self.bands:
                raise ValueError(
                    'a computed/partial comparison reports band metrics'
                )
            band_states = {band.state for band in self.bands}
            if self.comparison_state == 'computed' and (
                band_states != {'computed'}
            ):
                raise ValueError(
                    'computed comparison requires every band computed'
                )
            if self.comparison_state == 'partial' and (
                'insufficient_signal' not in band_states
            ):
                raise ValueError(
                    'partial comparison requires at least one '
                    'insufficient-signal band'
                )
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError(
                'listening validation semantic hash mismatch'
            )
        if self.validation_id != (
            f'auralization-listening-validation:{expected}'
        ):
            raise ValueError('listening validation id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'validation_id', 'semantic_sha256'},
        )


def _band_level_dbfs(samples: np.ndarray) -> float | None:
    """Band RMS in dBFS; None below the declared signal floor."""
    if samples.size == 0:
        return None
    mean_square = float(np.mean(samples ** 2))
    if mean_square < 1e-12:
        return None
    return 10.0 * log10(mean_square)


def _band_correlation(a: np.ndarray, b: np.ndarray) -> float | None:
    """Normalized zero-lag band-waveform correlation in [-1, 1]."""
    n = min(a.size, b.size)
    if n == 0:
        return None
    a = a[:n]
    b = b[:n]
    norm = sqrt(float(np.sum(a * a)) * float(np.sum(b * b)))
    if norm < 1e-12:
        return None
    return float(np.sum(a * b)) / norm


def build_listening_validation(
    *,
    document_id: str,
    source_scenario_id: str,
    receiver_id: str,
    predicted_artifact: AuralizationArtifact,
    predicted_pcm: RenderedPcm,
    measured_artifact: AuralizationArtifact | None = None,
    measured_pcm: RenderedPcm | None = None,
    band_centers_hz: tuple[float, ...] = (),
    band_fraction: BandFraction = 'octave',
    alignment_semantics: str,
    level_matching_method: str,
    audible_limitations: tuple[str, ...] = (),
) -> MeasuredPredictedListeningValidation:
    """Compute the measured-vs-predicted agreement record for one binding.

    Both renders must come from artifacts bound to *this* binding: the
    measured artifact's ``ir_kind`` must be ``'measured'`` and the
    predicted's ``'predicted'`` — a comparison pretending otherwise fails
    closed. When no measured reference exists the record is still built,
    with ``comparison_state='no_measured_reference'``.
    """
    if predicted_artifact.ir_kind != 'predicted':
        raise ValueError(
            'the predicted leg must bind a predicted-IR artifact'
        )
    if predicted_pcm.pcm_semantic_sha256 != (
        predicted_artifact.pcm_semantic_sha256
    ):
        raise ValueError(
            'predicted PCM does not match the bound artifact'
        )
    if measured_artifact is None or measured_pcm is None:
        if (measured_artifact is None) != (measured_pcm is None):
            raise ValueError(
                'measured artifact and PCM are supplied together'
            )
        comparison_state: ComparisonState = 'no_measured_reference'
        bands: tuple[BandAgreementMetric, ...] = ()
        sample_rate = predicted_pcm.sample_rate_hz
    else:
        if measured_artifact.ir_kind != 'measured':
            raise ValueError(
                'the measured leg must bind a measured-IR artifact'
            )
        if measured_pcm.pcm_semantic_sha256 != (
            measured_artifact.pcm_semantic_sha256
        ):
            raise ValueError(
                'measured PCM does not match the bound artifact'
            )
        if measured_pcm.sample_rate_hz != predicted_pcm.sample_rate_hz:
            raise ValueError(
                'measured/predicted sample rates must match — resampling '
                'the reference is a declared decision, not an implicit one'
            )
        if not band_centers_hz:
            raise ValueError(
                'a computed comparison requires declared band centers'
            )
        sample_rate = predicted_pcm.sample_rate_hz
        reference = np.asarray(measured_pcm.samples, dtype=np.float64)
        candidate = np.asarray(predicted_pcm.samples, dtype=np.float64)
        n = min(reference.size, candidate.size)
        reference = reference[:n]
        candidate = candidate[:n]
        band_rows: list[BandAgreementMetric] = []
        for center in band_centers_hz:
            center_value = _finite(center, field_name='band center')
            if center_value <= 0.0:
                raise ValueError('band centers must be positive')
            ref_band = _fft_bandpass(
                reference, float(sample_rate), center_value, band_fraction
            )
            pred_band = _fft_bandpass(
                candidate, float(sample_rate), center_value, band_fraction
            )
            ref_level = _band_level_dbfs(ref_band)
            pred_level = _band_level_dbfs(pred_band)
            correlation = _band_correlation(ref_band, pred_band)
            if ref_level is None or pred_level is None or correlation is None:
                band_rows.append(
                    BandAgreementMetric(
                        band_center_hz=center_value,
                        band_fraction=band_fraction,
                        state='insufficient_signal',
                    )
                )
            else:
                band_rows.append(
                    BandAgreementMetric(
                        band_center_hz=center_value,
                        band_fraction=band_fraction,
                        state='computed',
                        measured_level_dbfs=ref_level,
                        predicted_level_dbfs=pred_level,
                        level_difference_db=pred_level - ref_level,
                        waveform_correlation=correlation,
                    )
                )
        bands = tuple(band_rows)
        comparison_state = (
            'computed'
            if all(band.state == 'computed' for band in bands)
            else 'partial'
        )
    kwargs: dict[str, Any] = dict(
        document_id=document_id,
        source_scenario_id=source_scenario_id,
        receiver_id=receiver_id,
        measured_artifact_id=(
            measured_artifact.artifact_id if measured_artifact else None
        ),
        measured_artifact_semantic_sha256=(
            measured_artifact.semantic_sha256 if measured_artifact else None
        ),
        predicted_artifact_id=predicted_artifact.artifact_id,
        predicted_artifact_semantic_sha256=predicted_artifact.semantic_sha256,
        sample_rate_hz=sample_rate,
        alignment_semantics=alignment_semantics,
        level_matching_method=level_matching_method,
        comparison_state=comparison_state,
        bands=bands,
        audible_limitations=tuple(audible_limitations),
    )
    probe = MeasuredPredictedListeningValidation.model_construct(
        **canonicalize_payload(
            MeasuredPredictedListeningValidation, dict(
                validation_id='auralization-listening-validation:' + '0' * 64,
                semantic_sha256='0' * 64,
                **kwargs,
            )
        )
    )
    digest = _digest(probe.semantic_payload())
    return MeasuredPredictedListeningValidation(
        validation_id=f'auralization-listening-validation:{digest}',
        semantic_sha256=digest,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Shareable review package
# ---------------------------------------------------------------------------


class PackageComparisonEntry(BaseModel):
    """One exact listening comparison sealed into the package manifest."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    label: str = Field(min_length=1)
    evidence_kind: Literal[
        'predicted_auralization', 'measured_auralization'
    ]
    artifact_id: str = Field(pattern=r'^auralization-artifact:[0-9a-f]{64}$')
    artifact_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    spec_id: str = Field(pattern=r'^auralization-render-spec:[0-9a-f]{64}$')
    spec_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    capability_id: str = Field(
        pattern=r'^auralization-capability:[0-9a-f]{64}$'
    )
    capability_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    routing_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    member_name: str = Field(min_length=1)
    output_asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class PackageAssetEntry(BaseModel):
    """Security manifest row: every packaged binary member with its
    content hash, role and declared redistribution state."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    member_name: str = Field(min_length=1)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    size_bytes: int = Field(ge=0)
    kind: Literal['rendered_audio', 'viewer']
    rights: str = Field(min_length=1)


class AuralizationReviewPackage(BaseModel):
    """Immutable shareable review package (#538 sections 5-6).

    The semantic hash covers the whole manifest: document/scene authority,
    every comparison's exact spec/artifact/capability/routing pins, the
    per-member security manifest and the security assertions. The
    package's own zip bytes are hashed separately into
    ``package_asset_sha256`` — the manifest cannot hash itself.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = AURALIZATION_REVIEW_SCHEMA_VERSION
    authority_version: Literal[
        'auralization-review-package-1'
    ] = REVIEW_PACKAGE_AUTHORITY_VERSION
    package_id: str = Field(
        pattern=r'^auralization-review-package:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    system_variant_id: str | None = Field(default=None, min_length=1)
    system_variant_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    comparisons: tuple[PackageComparisonEntry, ...] = Field(min_length=1)
    capability_payloads: tuple[dict[str, Any], ...] = Field(min_length=1)
    routing_payloads: tuple[dict[str, Any], ...] = Field(min_length=1)
    assets: tuple[PackageAssetEntry, ...] = Field(min_length=1)
    security_assertions: tuple[str, ...] = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    package_asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def validate_package(self) -> 'AuralizationReviewPackage':
        if (self.system_variant_id is None) != (self.system_variant_sha256 is None):
            raise ValueError('system variant id/hash must be supplied together')
        labels = [entry.label for entry in self.comparisons]
        if len(set(labels)) != len(labels):
            raise ValueError('comparison labels must be unique')
        members = [entry.member_name for entry in self.comparisons]
        if len(set(members)) != len(members):
            raise ValueError('comparison member names must be unique')
        asset_names = {asset.member_name for asset in self.assets}
        comparison_names = set(members)
        if not comparison_names <= asset_names:
            raise ValueError(
                'every comparison member must appear in the asset manifest'
            )
        audio_names = {
            asset.member_name for asset in self.assets
            if asset.kind == 'rendered_audio'
        }
        if audio_names != comparison_names:
            raise ValueError(
                'the rendered_audio asset manifest must exactly cover the '
                'comparison members'
            )
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('review package semantic hash mismatch')
        if self.package_id != f'auralization-review-package:{expected}':
            raise ValueError('review package id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={
                'package_id',
                'semantic_sha256',
                'package_asset_sha256',
            },
        )


def _member_name_for_artifact(artifact_id: str) -> str:
    return 'audio/' + artifact_id.replace(':', '_') + '.wav'


_CONFIDENCE_LABEL_JA = {
    'measured_reference': '実測リファレンス',
    'predicted_validated_for_declared_domain': '宣言領域で検証済みの予測',
    'predicted_with_limitations': '制限付き予測',
    'demonstration_only': 'デモンストレーション専用',
    'unvalidated': '未検証',
}

_EVIDENCE_LABEL_JA = {
    'measured_auralization': '実測',
    'predicted_auralization': '予測',
}


def _review_index_html(
    *,
    document_id: str,
    scene_revision_id: str,
    comparisons: tuple[PackageComparisonEntry, ...],
    capabilities: dict[str, AuralizationCapability],
) -> bytes:
    """Deterministic static JA viewer for the packaged comparisons."""
    rows: list[str] = []
    for entry in comparisons:
        capability = capabilities[entry.capability_id]
        badge = _EVIDENCE_LABEL_JA[entry.evidence_kind]
        confidence = _CONFIDENCE_LABEL_JA[capability.confidence_state]
        limitations = ''.join(
            f'<li>{html.escape(item)}</li>'
            for item in capability.known_limitations
        ) or '<li>なし</li>'
        rows.append(
            '<section class="comparison">'
            f'<h2>{html.escape(entry.label)} '
            f'<span class="badge">{badge}</span></h2>'
            f'<p class="confidence">信頼度: {html.escape(confidence)}</p>'
            f'<audio controls preload="none" '
            f'src="{html.escape(entry.member_name)}"></audio>'
            '<details><summary>検証済み帯域・既知の制限</summary>'
            '<p>検証済み帯域: '
            + (
                ', '.join(
                    f'{band.band_center_hz:g} Hz'
                    for band in capability.validated_bands
                )
                or 'なし'
            )
            + f'</p><ul>{limitations}</ul></details>'
            f'<p class="hash">artifact {html.escape(entry.artifact_id)}</p>'
            '</section>'
        )
    document = (
        '<!doctype html>\n'
        '<html lang="ja"><head><meta charset="utf-8">'
        '<title>試聴レビューパッケージ</title>'
        '<style>body{font-family:sans-serif;margin:2rem;max-width:52rem}'
        '.badge{border:1px solid #666;border-radius:4px;padding:0 .4em;'
        'font-size:.8em}.confidence{font-weight:bold}'
        '.hash{font-family:monospace;font-size:.75em;color:#666;'
        'word-break:break-all}audio{width:100%}</style>'
        '</head><body>'
        '<h1>試聴レビューパッケージ</h1>'
        '<p>このパッケージは完全にオフラインで再生できます。'
        '表示される信頼度はソルバ/測定のエビデンスに拘束されており、'
        '再生品質によって昇格しません。</p>'
        f'<p>ドキュメント: <code>{html.escape(document_id)}</code><br>'
        f'シーン改訂: <code>{html.escape(scene_revision_id)}</code></p>'
        + ''.join(rows)
        + '</body></html>\n'
    )
    return document.encode('utf-8')


def _write_deterministic_zip(members: list[tuple[str, bytes]]) -> bytes:
    """Fixed-metadata zip: member order, timestamps, compression and
    attributes are pinned so identical inputs produce identical bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9) as archive:
        for name, data in members:
            info = zipfile.ZipInfo(name, date_time=_PACKAGE_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def _safe_member_name(name: str) -> None:
    if (
        not name
        or name.startswith(('/', '\\'))
        or ':' in name
        or '..' in name.split('/')
        or '\\' in name
    ):
        raise ValueError(f'unsafe package member name: {name!r}')


def build_review_package(
    *,
    document_id: str,
    scene_revision_id: str,
    scene_content_hash: str,
    comparisons: tuple[dict[str, Any], ...],
    created_at_utc: str,
    system_variant_id: str | None = None,
    system_variant_sha256: str | None = None,
) -> tuple[AuralizationReviewPackage, bytes]:
    """Build the sealed review package + deterministic zip bytes.

    ``comparisons`` rows supply ``label``, ``artifact``
    (:class:`AuralizationArtifact`), ``spec``
    (:class:`AuralizationRenderSpec`), ``wav_bytes`` and ``capability``
    (:class:`AuralizationCapability`). Every referenced authority must be
    present and consistent: a missing spec/capability or a WAV member that
    does not match its sealed digest fails closed — no partial package.
    """
    if not comparisons:
        raise ValueError('a review package requires at least one comparison')
    entries: list[PackageComparisonEntry] = []
    capability_rows: dict[str, AuralizationCapability] = {}
    routing_rows: dict[str, dict[str, Any]] = {}
    audio_members: list[tuple[str, bytes]] = []
    assets: list[PackageAssetEntry] = []
    for row in comparisons:
        label = row.get('label')
        artifact = row.get('artifact')
        spec = row.get('spec')
        wav = row.get('wav_bytes')
        capability = row.get('capability')
        if not isinstance(label, str) or not label:
            raise ValueError('each comparison requires a label')
        if not isinstance(artifact, AuralizationArtifact):
            raise ValueError('each comparison requires an artifact')
        if not isinstance(spec, AuralizationRenderSpec):
            raise ValueError('each comparison requires its render spec')
        if not isinstance(capability, AuralizationCapability):
            raise ValueError('each comparison requires its capability record')
        if not isinstance(wav, (bytes, bytearray)):
            raise ValueError('each comparison requires its rendered WAV bytes')
        if artifact.spec_id != spec.spec_id or (
            artifact.spec_semantic_sha256 != spec.semantic_sha256
        ):
            raise ValueError('comparison artifact does not match its spec')
        if capability.spec_id != spec.spec_id or (
            capability.spec_semantic_sha256 != spec.semantic_sha256
        ):
            raise ValueError(
                'comparison capability does not match its spec'
            )
        if capability.artifact_id != artifact.artifact_id or (
            capability.artifact_semantic_sha256 != artifact.semantic_sha256
        ):
            raise ValueError(
                'comparison capability does not match its artifact'
            )
        if artifact.clipped_sample_count > 0:
            raise ValueError(
                'a clipped render is never packaged — fix headroom first'
            )
        wav = bytes(wav)
        if _sha256_bytes(wav) != artifact.output_asset_sha256:
            raise ValueError(
                'comparison WAV bytes do not match the artifact digest'
            )
        member_name = _member_name_for_artifact(artifact.artifact_id)
        evidence_kind = (
            'measured_auralization'
            if artifact.ir_kind == 'measured'
            else 'predicted_auralization'
        )
        entries.append(
            PackageComparisonEntry(
                label=label,
                evidence_kind=evidence_kind,
                artifact_id=artifact.artifact_id,
                artifact_semantic_sha256=artifact.semantic_sha256,
                spec_id=spec.spec_id,
                spec_semantic_sha256=spec.semantic_sha256,
                capability_id=capability.capability_id,
                capability_semantic_sha256=capability.semantic_sha256,
                routing_sha256=capability.routing_sha256,
                member_name=member_name,
                output_asset_sha256=artifact.output_asset_sha256,
            )
        )
        capability_rows[capability.capability_id] = capability
        audio_members.append((member_name, wav))
        assets.append(
            PackageAssetEntry(
                member_name=member_name,
                sha256=_sha256_bytes(wav),
                size_bytes=len(wav),
                kind='rendered_audio',
                rights='derived_render_internal_review',
            )
        )
    entries.sort(key=lambda entry: entry.label)
    audio_members.sort(key=lambda member: member[0])

    for row in comparisons:
        capability = row['capability']
        routing = row.get('routing')
        if routing is None:
            raise ValueError(
                'each comparison requires its routing declaration'
            )
        if not isinstance(routing, AuralizationRoutingDeclaration):
            raise ValueError('comparison routing has the wrong type')
        if capability.routing_sha256 != routing.routing_sha256:
            raise ValueError(
                'capability pins a different routing declaration'
            )
        routing_rows[routing.routing_sha256] = routing
    routing_payloads = tuple(
        routing.model_dump(mode='json')
        for _sha, routing in sorted(routing_rows.items())
    )

    ordered_capabilities = tuple(
        capability_rows[entry.capability_id] for entry in entries
    )
    index_html = _review_index_html(
        document_id=document_id,
        scene_revision_id=scene_revision_id,
        comparisons=tuple(entries),
        capabilities=capability_rows,
    )
    assets.append(
        PackageAssetEntry(
            member_name='index.html',
            sha256=_sha256_bytes(index_html),
            size_bytes=len(index_html),
            kind='viewer',
            rights='generated_no_restrictions',
        )
    )
    kwargs: dict[str, Any] = dict(
        document_id=document_id,
        scene_revision_id=scene_revision_id,
        scene_content_hash=scene_content_hash,
        system_variant_id=system_variant_id,
        system_variant_sha256=system_variant_sha256,
        comparisons=tuple(entries),
        capability_payloads=tuple(
            capability.model_dump(mode='json')
            for capability in ordered_capabilities
        ),
        routing_payloads=routing_payloads,
        assets=tuple(assets),
        security_assertions=(
            'no_local_paths',
            'no_credentials_or_device_identifiers',
            'no_unselected_assets',
            'rendered_audio_only_no_source_program_assets',
        ),
        created_at_utc=created_at_utc,
    )
    probe = AuralizationReviewPackage.model_construct(
        **canonicalize_payload(AuralizationReviewPackage, dict(
            package_id='auralization-review-package:' + '0' * 64,
            semantic_sha256='0' * 64,
            **kwargs,
        ))
    )
    manifest_payload = probe.semantic_payload()
    manifest_bytes = _canonical(manifest_payload).encode('utf-8')
    members: list[tuple[str, bytes]] = [
        ('manifest.json', manifest_bytes),
        ('index.html', index_html),
        *audio_members,
    ]
    zip_bytes = _write_deterministic_zip(members)
    asset_sha = _sha256_bytes(zip_bytes)
    semantic = _digest(manifest_payload)
    package = AuralizationReviewPackage(
        package_id=f'auralization-review-package:{semantic}',
        semantic_sha256=semantic,
        package_asset_sha256=asset_sha,
        **kwargs,
    )
    return package, zip_bytes


def verify_review_package(data: bytes) -> AuralizationReviewPackage:
    """Re-derive and verify a packaged review bundle fail-closed.

    Member names must be safe and exactly the declared set; every declared
    asset's bytes must hash to its manifest digest; the manifest digest
    must re-derive the package identity; and every embedded capability/
    routing payload must re-validate as its declared model.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data), 'r')
    except zipfile.BadZipFile as exc:
        raise ValueError('not a review package zip') from exc
    with archive:
        names = archive.namelist()
        if len(set(names)) != len(names):
            raise ValueError('duplicate package members')
        for name in names:
            _safe_member_name(name)
        if 'manifest.json' not in names:
            raise ValueError('package has no manifest.json')
        manifest_bytes = archive.read('manifest.json')
        try:
            payload = json.loads(manifest_bytes.decode('utf-8'))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError('manifest.json is not valid UTF-8 JSON') from exc
        if not isinstance(payload, dict):
            raise ValueError('manifest.json is not an object')
        expected_semantic = _digest(payload)
        package_id = f'auralization-review-package:{expected_semantic}'
        record = AuralizationReviewPackage(
            package_id=package_id,
            semantic_sha256=expected_semantic,
            package_asset_sha256=_sha256_bytes(data),
            **payload,
        )
        declared = {asset.member_name: asset for asset in record.assets}
        member_set = set(names) - {'manifest.json'}
        if member_set != set(declared):
            raise ValueError(
                'package members do not match the declared asset manifest'
            )
        for asset in record.assets:
            raw = archive.read(asset.member_name)
            if len(raw) != asset.size_bytes:
                raise ValueError(
                    f'member {asset.member_name} size mismatch'
                )
            if _sha256_bytes(raw) != asset.sha256:
                raise ValueError(
                    f'member {asset.member_name} hash mismatch'
                )
        for entry in record.comparisons:
            raw = archive.read(entry.member_name)
            if _sha256_bytes(raw) != entry.output_asset_sha256:
                raise ValueError(
                    f'comparison member {entry.member_name} does not match '
                    'its sealed output digest'
                )
        capabilities = {
            capability.capability_id: capability
            for capability in (
                AuralizationCapability.model_validate(capability_payload)
                for capability_payload in record.capability_payloads
            )
        }
        if len(capabilities) != len(record.capability_payloads):
            raise ValueError('duplicate capability_id in manifest payloads')
        routings = {
            routing.routing_sha256: routing
            for routing in (
                AuralizationRoutingDeclaration.model_validate(routing_payload)
                for routing_payload in record.routing_payloads
            )
        }
        # Entry pins must resolve to the embedded payloads — a manifest
        # that validates shape but links comparisons to capabilities/
        # routings it does not carry is incoherent.
        for entry in record.comparisons:
            capability = capabilities.get(entry.capability_id)
            if capability is None:
                raise ValueError(
                    f'comparison {entry.label!r} pins a capability that '
                    'is not embedded in the package'
                )
            if (
                capability.semantic_sha256
                != entry.capability_semantic_sha256
            ):
                raise ValueError(
                    f'comparison {entry.label!r} capability digest mismatch'
                )
            if (
                capability.artifact_id != entry.artifact_id
                or capability.artifact_semantic_sha256
                != entry.artifact_semantic_sha256
            ):
                raise ValueError(
                    f'comparison {entry.label!r} artifact pins mismatch'
                )
            if (
                capability.spec_id != entry.spec_id
                or capability.spec_semantic_sha256
                != entry.spec_semantic_sha256
            ):
                raise ValueError(
                    f'comparison {entry.label!r} spec pins mismatch'
                )
            if capability.routing_sha256 != entry.routing_sha256:
                raise ValueError(
                    f'comparison {entry.label!r} routing pin mismatch'
                )
            if entry.routing_sha256 not in routings:
                raise ValueError(
                    f'comparison {entry.label!r} pins a routing declaration '
                    'that is not embedded in the package'
                )
    return record


__all__ = [
    'AcousticAuthorityState',
    'AuralizationCapability',
    'AuralizationConfidenceState',
    'AuralizationReviewPackage',
    'AuralizationRoutingDeclaration',
    'AuralizationStemRoute',
    'BandAgreementMetric',
    'BandFraction',
    'ComparisonState',
    'HrtfProcessing',
    'IrOrigin',
    'MeasuredPredictedListeningValidation',
    'PackageAssetEntry',
    'PackageComparisonEntry',
    'ValidatedBand',
    'BAND_AGREEMENT_ANALYSIS_METHOD',
    'build_auralization_capability',
    'build_auralization_routing',
    'build_listening_validation',
    'build_review_package',
    'verify_review_package',
]
