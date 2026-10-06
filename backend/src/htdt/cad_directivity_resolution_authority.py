"""Directivity angular-resolution / interpolation authority (#656).

A dense rendered balloon is not evidence of fine spatial sampling. This
authority seals the measured angular grid (coverage class, direction count,
nominal step, missing regions), the declared interpolation method and its
effective spherical-harmonic order, and every derived interpolation
artifact as an immutable record. Direction queries then answer whether a
direction is measured, legitimately interpolated, order-limited,
extrapolated, or outside coverage — the claim ceiling stays at the
measured information density, not the render grid.

Scope discipline:

* The authority declares sampling and interpolation provenance; it does
  not perform the interpolation. ``DirectivityInterpolationRecord`` pins
  the produced artifact (method + params + source dataset sha).
* ``dataset_kind`` mirrors the source dataset's magnitude_only/complex
  split (``cad_directivity``) so complex-domain interpolation cannot be
  claimed over magnitude-only data.
* ``supported_sh_order`` bounds spherical-harmonic queries; a request
  above the declared order is ``sh_order_unsupported``, not silently
  truncated.

Literature basis: the spherical sampling theorem — a polar pattern
band-limited to harmonic order N needs ~(N+1)^2 samples and cannot
resolve finer detail (Rafaely, "Analysis and design of spherical
microphone arrays"; the SH sampling bound used by Ambisonics/HO-SIRS);
up-sampling by interpolation adds no information (Shannon); directional
aliasing when the measured step is coarser than lambda/2-scale lobes.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_bass_management import FrequencyBand
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


DIRECTIVITY_RESOLUTION_SCHEMA_VERSION = 'directivity-resolution-1'
DIRECTIVITY_RESOLUTION_EVALUATION_VERSION = (
    'directivity-resolution-eval-1'
)

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


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
        **{
            sha_field: digest,
            id_field: _semantic_id(prefix, digest),
        },
    )


# ---------------------------------------------------------------------------
# Taxonomies
# ---------------------------------------------------------------------------

AngularCoverageClass = Literal[
    'horizontal_cut_only',
    'vertical_cut_only',
    'hv_cuts',
    'full_sphere_sampled',
    'full_sphere_derived',
    'partial_sector',
    'unknown',
]
"""Measured angular coverage. ``full_sphere_derived`` is completed by
symmetry/model — not measured — and carries that provenance."""

AngularCapabilityState = Literal[
    'well_sampled',
    'interpolation_supported',
    'order_limited',
    'spatial_aliasing_risk',
    'under_sampled',
    'outside_measured_domain',
    'unknown',
]

InterpolationMethod = Literal[
    'nearest_neighbor',
    'linear_angular',
    'bilinear_triangulated_sphere',
    'log_magnitude_interpolation',
    'complex_tf_interpolation',
    'time_aligned_complex_interpolation',
    'spherical_harmonic_reconstruction',
    'model_based_regularized_upsampling',
    'custom_validated_method',
]

InterpolationDomain = Literal[
    'magnitude_db',
    'magnitude_linear',
    'complex_tf',
    'log_magnitude',
    'sh_coefficients',
    'other_declared',
]

CompletionKind = Literal[
    'interpolation_inside_measured_coverage',
    'extrapolation_outside_measured_coverage',
    'symmetry_assumed',
    'mirrored_derivation',
    'model_completion',
    'unknown_fill',
]

DirectionVerdict = Literal[
    'measured_direction',
    'interpolated_eligible',
    'interpolated_limited',
    'extrapolated',
    'outside_coverage',
    'sh_order_unsupported',
    'insufficient_evidence',
]

DatasetKind = Literal['magnitude_only', 'complex', 'unknown']

_INTERP_DOMAINS_BY_METHOD: dict[str, tuple[InterpolationDomain, ...]] = {
    'nearest_neighbor': ('magnitude_db', 'magnitude_linear'),
    'linear_angular': (
        'magnitude_db', 'magnitude_linear', 'log_magnitude',
    ),
    'bilinear_triangulated_sphere': (
        'magnitude_db', 'magnitude_linear', 'log_magnitude',
    ),
    'log_magnitude_interpolation': ('log_magnitude', 'magnitude_db'),
    'complex_tf_interpolation': ('complex_tf',),
    'time_aligned_complex_interpolation': ('complex_tf',),
    'spherical_harmonic_reconstruction': ('sh_coefficients',),
    'model_based_regularized_upsampling': ('other_declared',),
    'custom_validated_method': ('other_declared',),
}


# ---------------------------------------------------------------------------
# Sub-specs
# ---------------------------------------------------------------------------


class AngularSamplingSpec(BaseModel):
    """Declared measured angular sampling of the dataset."""

    model_config = ConfigDict(frozen=True)

    coverage_class: AngularCoverageClass
    measured_direction_count: int = Field(ge=0)
    nominal_step_deg: float | None = Field(default=None, gt=0.0)
    azimuth_step_deg: float | None = Field(default=None, gt=0.0)
    elevation_step_deg: float | None = Field(default=None, gt=0.0)
    sphere_coverage_fraction: float | None = Field(
        default=None, ge=0.0, le=1.0
    )
    missing_region_count: int = Field(default=0, ge=0)
    reference_axis: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'AngularSamplingSpec':
        if self.coverage_class == 'full_sphere_sampled' and (
            self.measured_direction_count < 12
        ):
            raise ValueError(
                'full_sphere_sampled requires at least 12 measured '
                'directions'
            )
        if self.coverage_class == 'unknown' and (
            self.measured_direction_count != 0
        ):
            raise ValueError(
                'unknown coverage cannot assert a direction count'
            )
        return self


class ShRepresentation(BaseModel):
    """Declared spherical-harmonic representation used by an
    interpolation or reconstruction."""

    model_config = ConfigDict(frozen=True)

    supported_order: int = Field(ge=0)
    convention: str = Field(min_length=1)
    band_limited: bool = True


class HeldoutAngleStudy(BaseModel):
    """Withheld-angle validation evidence for an interpolation method."""

    model_config = ConfigDict(frozen=True)

    withheld_count: int = Field(ge=1)
    metric: str = Field(min_length=1)
    mean_error_db: float | None = Field(default=None, ge=0.0)
    max_error_db: float | None = Field(default=None, ge=0.0)
    study_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def _check(self) -> 'HeldoutAngleStudy':
        if self.study_ref is not None and (
            self.study_ref.ref_sha256 is None
        ):
            raise ValueError('heldout study ref must pin its sha256')
        return self


class AngularInterpolationSpec(BaseModel):
    """Declared interpolation law between measured directions."""

    model_config = ConfigDict(frozen=True)

    method: InterpolationMethod
    domain: InterpolationDomain
    completion_kind: CompletionKind = (
        'interpolation_inside_measured_coverage'
    )
    sh: ShRepresentation | None = None
    validation: HeldoutAngleStudy | None = None
    method_version: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'AngularInterpolationSpec':
        allowed = _INTERP_DOMAINS_BY_METHOD.get(self.method, ())
        if allowed and self.domain not in allowed:
            raise ValueError(
                f'interpolation method {self.method} cannot declare '
                f'domain {self.domain}'
            )
        if self.method == 'spherical_harmonic_reconstruction' and (
            self.sh is None
        ):
            raise ValueError(
                'spherical_harmonic_reconstruction requires a declared '
                'ShRepresentation (order + convention)'
            )
        if self.method == 'custom_validated_method' and (
            self.validation is None
        ):
            raise ValueError(
                'custom_validated_method requires a heldout-angle '
                'validation study'
            )
        if self.domain == 'complex_tf' and self.method not in {
            'complex_tf_interpolation',
            'time_aligned_complex_interpolation',
        }:
            raise ValueError(
                'complex_tf domain requires a complex interpolation '
                'method'
            )
        return self


class BandAngularCapability(BaseModel):
    """Per-band angular capability ceiling."""

    model_config = ConfigDict(frozen=True)

    band_hz: FrequencyBand
    state: AngularCapabilityState
    max_reliable_sh_order: int | None = Field(default=None, ge=0)
    notes: str = ''


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


class DirectivitySamplingProfile(BaseModel):
    """Sealed declaration of measured angular sampling + interpolation.

    ``dataset_ref`` pins the directivity dataset by content hash so the
    sampling claim cannot drift from the measured grid.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    dataset_ref: AuthorityRef
    dataset_kind: DatasetKind = 'unknown'
    sampling: AngularSamplingSpec
    interpolation: AngularInterpolationSpec | None = None
    band_capabilities: tuple[BandAngularCapability, ...] = ()
    presentation_step_deg: float | None = Field(default=None, gt=0.0)
    authority_version: str = Field(
        default=DIRECTIVITY_RESOLUTION_SCHEMA_VERSION, min_length=1
    )
    declared_at_utc: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'dataset_ref': self.dataset_ref.model_dump(mode='json'),
            'dataset_kind': self.dataset_kind,
            'sampling': self.sampling.model_dump(mode='json'),
            'interpolation': (
                self.interpolation.model_dump(mode='json')
                if self.interpolation is not None
                else None
            ),
            'band_capabilities': [
                b.model_dump(mode='json')
                for b in self.band_capabilities
            ],
            'presentation_step_deg': self.presentation_step_deg,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'DirectivitySamplingProfile':
        _require_iso8601(self.declared_at_utc, 'profile declared_at_utc')
        if self.dataset_ref.ref_sha256 is None:
            raise ValueError('dataset_ref must pin its sha256')
        if self.interpolation is not None and (
            self.dataset_kind != 'complex'
        ) and self.interpolation.domain == 'complex_tf':
            raise ValueError(
                'complex_tf interpolation requires a complex dataset; '
                'the dataset_kind is not complex'
            )
        if (
            self.presentation_step_deg is not None
            and self.sampling.nominal_step_deg is not None
            and self.presentation_step_deg
            < self.sampling.nominal_step_deg
        ):
            # Render grid finer than the measured grid is legal — but the
            # capability ceiling stays at the measured step; the profile
            # records both so the qualification can state the bound.
            pass
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('directivity sampling profile hash mismatch')
        if self.profile_id != _semantic_id('drsprof', expected):
            raise ValueError(
                'directivity sampling profile id does not match its hash'
            )
        return self


class DirectivityInterpolationRecord(BaseModel):
    """Sealed record of a produced interpolation artifact.

    Every upsampled balloon is an immutable derivation: the record pins
    the source dataset sha, the method + params, and the output grid so
    the artifact can never masquerade as measured data.
    """

    model_config = ConfigDict(frozen=True)

    record_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    output_artifact_ref: AuthorityRef | None = None
    method: InterpolationMethod
    domain: InterpolationDomain
    output_grid: AngularSamplingSpec
    parameters: tuple[tuple[str, str], ...] = ()
    validation: HeldoutAngleStudy | None = None
    authority_version: str = Field(
        default=DIRECTIVITY_RESOLUTION_SCHEMA_VERSION, min_length=1
    )
    declared_at_utc: str = Field(min_length=1)
    record_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'output_artifact_ref': (
                self.output_artifact_ref.model_dump(mode='json')
                if self.output_artifact_ref is not None
                else None
            ),
            'method': self.method,
            'domain': self.domain,
            'output_grid': self.output_grid.model_dump(mode='json'),
            'parameters': [list(p) for p in self.parameters],
            'validation': (
                self.validation.model_dump(mode='json')
                if self.validation is not None
                else None
            ),
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'DirectivityInterpolationRecord':
        _require_iso8601(self.declared_at_utc, 'record declared_at_utc')
        if self.profile_ref.ref_sha256 is None:
            raise ValueError('profile_ref must pin its sha256')
        if self.profile_ref.kind != 'directivity_sampling_profile':
            raise ValueError(
                "profile_ref must pin a 'directivity_sampling_profile'"
            )
        if self.output_artifact_ref is not None and (
            self.output_artifact_ref.ref_sha256 is None
        ):
            raise ValueError(
                'output_artifact_ref must pin its sha256'
            )
        expected = _hash(self.identity_payload())
        if self.record_sha256 != expected:
            raise ValueError(
                'directivity interpolation record hash mismatch'
            )
        if self.record_id != _semantic_id('dinterp', expected):
            raise ValueError(
                'directivity interpolation record id does not match '
                'its hash'
            )
        return self


class DirectionQueryQualification(BaseModel):
    """Sealed verdict of :func:`evaluate_direction_query`."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    verdict: DirectionVerdict
    azimuth_deg: float
    elevation_deg: float
    frequency_hz: float | None = None
    requested_sh_order: int | None = None
    interpolation_record_ref: AuthorityRef | None = None
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_version: str = Field(
        default=DIRECTIVITY_RESOLUTION_EVALUATION_VERSION, min_length=1
    )
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'verdict': self.verdict,
            'azimuth_deg': self.azimuth_deg,
            'elevation_deg': self.elevation_deg,
            'frequency_hz': self.frequency_hz,
            'requested_sh_order': self.requested_sh_order,
            'interpolation_record_ref': (
                self.interpolation_record_ref.model_dump(mode='json')
                if self.interpolation_record_ref is not None
                else None
            ),
            'reasons': list(self.reasons),
            'limitations': list(self.limitations),
            'evaluated_at_utc': self.evaluated_at_utc,
            'evaluation_version': self.evaluation_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'DirectionQueryQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        if self.profile_ref.ref_sha256 is None:
            raise ValueError('profile_ref must pin its sha256')
        if self.profile_ref.kind != 'directivity_sampling_profile':
            raise ValueError(
                "profile_ref must pin a 'directivity_sampling_profile'"
            )
        if self.interpolation_record_ref is not None and (
            self.interpolation_record_ref.ref_sha256 is None
        ):
            raise ValueError(
                'interpolation_record_ref must pin its sha256'
            )
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError(
                'direction query qualification hash mismatch'
            )
        if self.qualification_id != _semantic_id('drqual', expected):
            raise ValueError(
                'direction query qualification id does not match '
                'its hash'
            )
        return self


def directivity_sampling_binding(
    profile: DirectivitySamplingProfile,
) -> AuthorityRef:
    return AuthorityRef(
        kind='directivity_sampling_profile',
        ref_id=profile.profile_id,
        ref_sha256=profile.profile_sha256,
    )


def directivity_interpolation_binding(
    record: DirectivityInterpolationRecord,
) -> AuthorityRef:
    return AuthorityRef(
        kind='directivity_interpolation_record',
        ref_id=record.record_id,
        ref_sha256=record.record_sha256,
    )


def direction_query_qualification_binding(
    qualification: DirectionQueryQualification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='direction_query_qualification',
        ref_id=qualification.qualification_id,
        ref_sha256=qualification.qualification_sha256,
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

_PLANE_COVERAGE = {
    'horizontal_cut_only', 'vertical_cut_only', 'hv_cuts',
}


def _angle_on_step(value_deg: float, step_deg: float | None) -> bool:
    if step_deg is None or step_deg <= 0.0:
        return False
    k = value_deg / step_deg
    return abs(k - round(k)) < 1e-6


def evaluate_direction_query(
    document_id: str,
    profile: DirectivitySamplingProfile,
    *,
    azimuth_deg: float,
    elevation_deg: float,
    frequency_hz: float | None = None,
    requested_sh_order: int | None = None,
    interpolation_record_ref: AuthorityRef | None = None,
    evaluated_at_utc: str | None = None,
) -> DirectionQueryQualification:
    """Classify one direction query against the declared sampling.

    Fail-closed rules:

    * no measured sampling declared → ``insufficient_evidence``
    * direction inside measured grid → ``measured_direction``
    * inside coverage with interpolation → ``interpolated_eligible``,
      downgraded to ``interpolated_limited`` when the band capability is
      ``order_limited``/``spatial_aliasing_risk``/``under_sampled``
    * planar coverage queried off its planes → ``outside_coverage``
    * direction outside coverage → ``extrapolated``
    * SH request above the declared order → ``sh_order_unsupported``
    """

    evaluated_at_utc = evaluated_at_utc or _utc_now()
    reasons: list[str] = []
    limitations: list[str] = []

    sampling = profile.sampling
    interpolation = profile.interpolation

    # --- SH order gate --------------------------------------------------
    supported_order = (
        interpolation.sh.supported_order
        if interpolation is not None and interpolation.sh is not None
        else None
    )
    if (
        requested_sh_order is not None
        and interpolation is not None
        and interpolation.method == 'spherical_harmonic_reconstruction'
        and supported_order is not None
        and requested_sh_order > supported_order
    ):
        reasons.append(
            f'requested SH order {requested_sh_order} exceeds the '
            f'declared order {supported_order}'
        )
        verdict: DirectionVerdict = 'sh_order_unsupported'
        return _finish(
            document_id, profile, verdict,
            azimuth_deg, elevation_deg, frequency_hz,
            requested_sh_order, interpolation_record_ref,
            reasons, limitations, evaluated_at_utc,
        )

    # --- coverage gate ----------------------------------------------------
    coverage = sampling.coverage_class
    if coverage == 'unknown' or sampling.measured_direction_count == 0:
        return _finish(
            document_id, profile, 'insufficient_evidence',
            azimuth_deg, elevation_deg, frequency_hz,
            requested_sh_order, interpolation_record_ref,
            ['measured angular sampling never declared'],
            limitations, evaluated_at_utc,
        )

    on_horizontal = abs(elevation_deg) < 1e-6
    on_vertical = (
        abs(azimuth_deg) < 1e-6 or abs(abs(azimuth_deg) - 180.0) < 1e-6
    )
    in_plane = (
        (coverage == 'horizontal_cut_only' and on_horizontal)
        or (coverage == 'vertical_cut_only' and on_vertical)
        or (
            coverage == 'hv_cuts' and (on_horizontal or on_vertical)
        )
    )
    if coverage in _PLANE_COVERAGE and not in_plane:
        return _finish(
            document_id, profile, 'outside_coverage',
            azimuth_deg, elevation_deg, frequency_hz,
            requested_sh_order, interpolation_record_ref,
            [
                f'coverage {coverage} does not measure the queried '
                'direction'
            ],
            limitations, evaluated_at_utc,
        )
    if coverage == 'partial_sector':
        return _finish(
            document_id, profile, 'outside_coverage',
            azimuth_deg, elevation_deg, frequency_hz,
            requested_sh_order, interpolation_record_ref,
            [
                'coverage is a declared partial sector; any direction '
                'requires explicit measured-membership evidence'
            ],
            limitations, evaluated_at_utc,
        )

    measured_hit = (
        _angle_on_step(azimuth_deg, sampling.azimuth_step_deg)
        and _angle_on_step(elevation_deg, sampling.elevation_step_deg)
    ) or (
        sampling.nominal_step_deg is not None
        and _angle_on_step(azimuth_deg, sampling.nominal_step_deg)
        and _angle_on_step(elevation_deg, sampling.nominal_step_deg)
    )
    if measured_hit:
        return _finish(
            document_id, profile, 'measured_direction',
            azimuth_deg, elevation_deg, frequency_hz,
            requested_sh_order, interpolation_record_ref,
            reasons, limitations, evaluated_at_utc,
        )

    # --- interpolation gate ------------------------------------------------
    if interpolation is None:
        return _finish(
            document_id, profile, 'outside_coverage',
            azimuth_deg, elevation_deg, frequency_hz,
            requested_sh_order, interpolation_record_ref,
            ['no interpolation declared between measured directions'],
            limitations, evaluated_at_utc,
        )

    if interpolation.completion_kind in {
        'extrapolation_outside_measured_coverage',
        'symmetry_assumed',
        'mirrored_derivation',
        'model_completion',
        'unknown_fill',
    }:
        limitations.append(
            f'interpolation uses {interpolation.completion_kind} '
            'completion'
        )
        verdict = 'extrapolated'
    else:
        verdict = 'interpolated_eligible'

    if frequency_hz is not None:
        for cap in profile.band_capabilities:
            if (
                cap.band_hz.low_hz <= frequency_hz
                <= cap.band_hz.high_hz
            ):
                if cap.state in {
                    'order_limited',
                    'spatial_aliasing_risk',
                    'under_sampled',
                }:
                    limitations.append(
                        f'band capability is {cap.state} at '
                        f'{frequency_hz} Hz'
                    )
                    if verdict == 'interpolated_eligible':
                        verdict = 'interpolated_limited'
                elif cap.state == 'outside_measured_domain':
                    return _finish(
                        document_id, profile, 'outside_coverage',
                        azimuth_deg, elevation_deg, frequency_hz,
                        requested_sh_order, interpolation_record_ref,
                        [
                            f'band capability outside measured domain '
                            f'at {frequency_hz} Hz'
                        ],
                        limitations, evaluated_at_utc,
                    )
                break

    if (
        interpolation.method == 'spherical_harmonic_reconstruction'
        and requested_sh_order is not None
        and supported_order is not None
        and requested_sh_order > supported_order
    ):
        return _finish(
            document_id, profile, 'sh_order_unsupported',
            azimuth_deg, elevation_deg, frequency_hz,
            requested_sh_order, interpolation_record_ref,
            [
                f'requested SH order {requested_sh_order} exceeds the '
                f'declared order {supported_order}'
            ],
            limitations, evaluated_at_utc,
        )

    if interpolation_record_ref is not None and (
        interpolation_record_ref.kind
        != 'directivity_interpolation_record'
    ):
        limitations.append(
            'interpolation_record_ref does not pin a '
            'directivity_interpolation_record'
        )

    return _finish(
        document_id, profile, verdict,
        azimuth_deg, elevation_deg, frequency_hz,
        requested_sh_order, interpolation_record_ref,
        reasons, limitations, evaluated_at_utc,
    )


def _finish(
    document_id: str,
    profile: DirectivitySamplingProfile,
    verdict: DirectionVerdict,
    azimuth_deg: float,
    elevation_deg: float,
    frequency_hz: float | None,
    requested_sh_order: int | None,
    interpolation_record_ref: AuthorityRef | None,
    reasons: list[str],
    limitations: list[str],
    evaluated_at_utc: str,
) -> DirectionQueryQualification:
    return _seal(
        DirectionQueryQualification,
        {
            'document_id': document_id,
            'profile_ref': directivity_sampling_binding(profile),
            'verdict': verdict,
            'azimuth_deg': float(azimuth_deg),
            'elevation_deg': float(elevation_deg),
            'frequency_hz': frequency_hz,
            'requested_sh_order': requested_sh_order,
            'interpolation_record_ref': (
                interpolation_record_ref.model_dump(mode='json')
                if interpolation_record_ref is not None
                else None
            ),
            'reasons': sorted(set(reasons)),
            'limitations': sorted(set(limitations)),
            'evaluated_at_utc': evaluated_at_utc,
            'evaluation_version': DIRECTIVITY_RESOLUTION_EVALUATION_VERSION,
        },
        'qualification_id',
        'qualification_sha256',
        'drqual',
    )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_directivity_sampling_profile(
    document_id: str,
    dataset_ref: AuthorityRef,
    sampling: AngularSamplingSpec,
    *,
    dataset_kind: DatasetKind = 'unknown',
    interpolation: AngularInterpolationSpec | None = None,
    band_capabilities: Sequence[BandAngularCapability] = (),
    presentation_step_deg: float | None = None,
    declared_at_utc: str | None = None,
) -> DirectivitySamplingProfile:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        DirectivitySamplingProfile,
        {
            'document_id': document_id,
            'dataset_ref': dataset_ref.model_dump(mode='json'),
            'dataset_kind': dataset_kind,
            'sampling': sampling.model_dump(mode='json'),
            'interpolation': (
                interpolation.model_dump(mode='json')
                if interpolation is not None
                else None
            ),
            'band_capabilities': [
                b.model_dump(mode='json') for b in band_capabilities
            ],
            'presentation_step_deg': presentation_step_deg,
            'authority_version': DIRECTIVITY_RESOLUTION_SCHEMA_VERSION,
            'declared_at_utc': declared_at_utc,
        },
        'profile_id',
        'profile_sha256',
        'drsprof',
    )


def build_directivity_interpolation_record(
    document_id: str,
    profile: DirectivitySamplingProfile,
    *,
    method: InterpolationMethod,
    domain: InterpolationDomain,
    output_grid: AngularSamplingSpec,
    output_artifact_ref: AuthorityRef | None = None,
    parameters: Sequence[tuple[str, str]] = (),
    validation: HeldoutAngleStudy | None = None,
    declared_at_utc: str | None = None,
) -> DirectivityInterpolationRecord:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        DirectivityInterpolationRecord,
        {
            'document_id': document_id,
            'profile_ref': directivity_sampling_binding(profile),
            'output_artifact_ref': (
                output_artifact_ref.model_dump(mode='json')
                if output_artifact_ref is not None
                else None
            ),
            'method': method,
            'domain': domain,
            'output_grid': output_grid.model_dump(mode='json'),
            'parameters': [list(p) for p in parameters],
            'validation': (
                validation.model_dump(mode='json')
                if validation is not None
                else None
            ),
            'authority_version': DIRECTIVITY_RESOLUTION_SCHEMA_VERSION,
            'declared_at_utc': declared_at_utc,
        },
        'record_id',
        'record_sha256',
        'dinterp',
    )
