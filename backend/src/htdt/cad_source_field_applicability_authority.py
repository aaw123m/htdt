"""Source near-field / far-field applicability authority (#655).

Measured directivity and sound-pressure data are only valid over the
distance regime in which they were captured. This authority seals the
measurement geometry (mic distance, reference point, environment, gate),
the per-band field regime, and any near-to-far derivation provenance, then
evaluates queries at a requested distance and band. Reusing a far-field
balloon inside the near field is ``distance_too_close_for_selected_
far_field_model`` — never silently applied.

Scope discipline:

* The authority declares *where the data applies*; it does not compute a
  far-field transform. A ``NearFarDerivation`` records the provenance of
  an already-computed transform (e.g. NAH); without it, near-field data
  stays near-field data.
* ``origin_ref`` pins the #654 ``source_reference_origin_profile`` so a
  distance claim cannot drift from the declared propagation origin.
* Quasi-anechoic gating bounds the lowest resolvable frequency
  (~1/gate); bands below that bound are ``insufficient_evidence`` unless
  a derivation covers them.

Literature basis: the D^2/lambda transition-distance criterion (Fraunhofer
vs Fresnel regions); large-source / array near-field behaviour (the
1/sqrt-style power-sum breakdown of point-source far-field assumptions);
near-field acoustical holography (Williams & Maynard) as the declared
near-to-far derivation path.
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


SOURCE_FIELD_SCHEMA_VERSION = 'source-field-1'
SOURCE_FIELD_EVALUATION_VERSION = 'source-field-eval-1'

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

MeasurementFieldRegime = Literal[
    'anechoic_far_field',
    'quasi_anechoic_gated',
    'ground_plane',
    'half_space',
    'near_field_scan',
    'near_field_holography',
    'free_field_inferred',
    'installed_in_situ',
    'unknown',
]
"""Field regime under which the dataset was captured. Gated and inferred
regimes carry explicit limitations."""

SourceModelCapability = Literal[
    'single_effective_source',
    'multi_radiator_source',
    'line_planar_array_source',
    'frequency_dependent_effective_source',
    'far_field_polar_only',
]
"""Source model the dataset supports. ``far_field_polar_only`` cannot
service distance-dependent or multi-radiator queries."""

ApplicabilityVerdict = Literal[
    'directly_applicable',
    'applicable_with_approximation',
    'transition_field_limited',
    'distance_too_close_for_selected_far_field_model',
    'nearfield_only',
    'farfield_only',
    'requires_explicit_multi_radiator_model',
    'insufficient_evidence',
]

RequestedFieldSemantics = Literal[
    'magnitude_angular',
    'complex_far_field',
    'distance_dependent_complex',
    'multi_radiator_time_domain',
]


# ---------------------------------------------------------------------------
# Sub-specs
# ---------------------------------------------------------------------------


class DistanceRange(BaseModel):
    """A [min, max] distance interval in metres."""

    model_config = ConfigDict(frozen=True)

    min_m: float = Field(gt=0.0)
    max_m: float = Field(gt=0.0)

    @model_validator(mode='after')
    def _check(self) -> 'DistanceRange':
        if self.min_m >= self.max_m:
            raise ValueError('distance range min_m must be below max_m')
        for label, v in (('min_m', self.min_m), ('max_m', self.max_m)):
            if not isfinite(float(v)):
                raise ValueError(f'{label} must be finite')
        return self


class MeasurementGeometrySpec(BaseModel):
    """How and where the dataset was captured.

    ``distance_reference_kind`` names the #654 reference point the mic
    distance is measured from — it must not be left implicit.
    """

    model_config = ConfigDict(frozen=True)

    mic_distance_m: float = Field(gt=0.0)
    distance_reference_kind: str = 'unknown'
    environment: MeasurementFieldRegime = 'unknown'
    gate_window_s: float | None = Field(default=None, gt=0.0)
    source_aperture_m: float | None = Field(default=None, gt=0.0)
    notes: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'MeasurementGeometrySpec':
        if not isfinite(float(self.mic_distance_m)):
            raise ValueError('mic_distance_m must be finite')
        if self.environment == 'quasi_anechoic_gated' and (
            self.gate_window_s is None
        ):
            raise ValueError(
                'quasi_anechoic_gated requires a declared gate_window_s'
            )
        if (
            self.environment == 'unknown'
            and self.distance_reference_kind != 'unknown'
        ):
            raise ValueError(
                'an unknown environment cannot name a distance reference'
            )
        return self


class BandFieldRegime(BaseModel):
    """Field regime and usable distance range for one band."""

    model_config = ConfigDict(frozen=True)

    band_hz: FrequencyBand
    regime: MeasurementFieldRegime
    applicable_distance_m: DistanceRange | None = None
    source_model_capability: SourceModelCapability | None = None
    transition_distance_m: float | None = Field(default=None, gt=0.0)
    transition_distance_basis: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'BandFieldRegime':
        if (
            self.transition_distance_m is not None
            and not self.transition_distance_basis
        ):
            raise ValueError(
                'declared transition_distance_m requires a basis '
                '(e.g. D^2/lambda)'
            )
        return self


class NearFarDerivation(BaseModel):
    """Provenance of a near-to-far field transform.

    A derivation converts near-field captures into a *derived* far-field
    estimate — the result is explicitly marked ``derived_farfield_estimate``
    quality, never silently promoted to measured far field.
    """

    model_config = ConfigDict(frozen=True)

    source_measurement_refs: tuple[AuthorityRef, ...] = Field(
        min_length=1
    )
    algorithm: str = Field(min_length=1)
    assumptions: tuple[str, ...] = ()
    splice_frequency_hz: float | None = Field(default=None, gt=0.0)
    normalization: str = ''
    residual_db: float | None = Field(default=None, ge=0.0)

    @model_validator(mode='after')
    def _check(self) -> 'NearFarDerivation':
        for ref in self.source_measurement_refs:
            if ref.ref_sha256 is None:
                raise ValueError(
                    'derivation source refs must pin their sha256'
                )
        return self


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


class SourceFieldProfile(BaseModel):
    """Sealed per-source measurement-distance authority."""

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    source_ref: AuthorityRef
    origin_ref: AuthorityRef | None = None
    dataset_ref: AuthorityRef | None = None
    measurement_geometry: MeasurementGeometrySpec
    band_regimes: tuple[BandFieldRegime, ...] = Field(min_length=1)
    default_source_model: SourceModelCapability = 'far_field_polar_only'
    near_far_derivation: NearFarDerivation | None = None
    authority_version: str = Field(
        default=SOURCE_FIELD_SCHEMA_VERSION, min_length=1
    )
    declared_at_utc: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'source_ref': self.source_ref.model_dump(mode='json'),
            'origin_ref': (
                self.origin_ref.model_dump(mode='json')
                if self.origin_ref is not None
                else None
            ),
            'dataset_ref': (
                self.dataset_ref.model_dump(mode='json')
                if self.dataset_ref is not None
                else None
            ),
            'measurement_geometry': (
                self.measurement_geometry.model_dump(mode='json')
            ),
            'band_regimes': [
                b.model_dump(mode='json') for b in self.band_regimes
            ],
            'default_source_model': self.default_source_model,
            'near_far_derivation': (
                self.near_far_derivation.model_dump(mode='json')
                if self.near_far_derivation is not None
                else None
            ),
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'SourceFieldProfile':
        _require_iso8601(self.declared_at_utc, 'profile declared_at_utc')
        for ref, label in (
            (self.source_ref, 'source_ref'),
            (self.origin_ref, 'origin_ref'),
            (self.dataset_ref, 'dataset_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        if self.origin_ref is not None and (
            self.origin_ref.kind != 'source_reference_origin_profile'
        ):
            raise ValueError(
                "origin_ref must pin a 'source_reference_origin_profile'"
            )
        bands = [
            (b.band_hz.low_hz, b.band_hz.high_hz) for b in self.band_regimes
        ]
        if len(set(bands)) != len(bands):
            raise ValueError('duplicate band_regimes bands')
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('source field profile hash mismatch')
        if self.profile_id != _semantic_id('sfldprof', expected):
            raise ValueError(
                'source field profile id does not match its hash'
            )
        return self


class SourceFieldQualification(BaseModel):
    """Sealed verdict of :func:`evaluate_source_field_applicability`."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    verdict: ApplicabilityVerdict
    requested_distance_m: float | None = None
    requested_band_hz: FrequencyBand | None = None
    requested_semantics: RequestedFieldSemantics = 'magnitude_angular'
    effective_regime: MeasurementFieldRegime = 'unknown'
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_version: str = Field(
        default=SOURCE_FIELD_EVALUATION_VERSION, min_length=1
    )
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'verdict': self.verdict,
            'requested_distance_m': self.requested_distance_m,
            'requested_band_hz': (
                self.requested_band_hz.model_dump(mode='json')
                if self.requested_band_hz is not None
                else None
            ),
            'requested_semantics': self.requested_semantics,
            'effective_regime': self.effective_regime,
            'reasons': list(self.reasons),
            'limitations': list(self.limitations),
            'evaluated_at_utc': self.evaluated_at_utc,
            'evaluation_version': self.evaluation_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'SourceFieldQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        if self.profile_ref.ref_sha256 is None:
            raise ValueError('profile_ref must pin its sha256')
        if self.profile_ref.kind != 'source_field_profile':
            raise ValueError(
                "profile_ref must pin a 'source_field_profile'"
            )
        if self.requested_distance_m is not None and (
            not isfinite(float(self.requested_distance_m))
            or self.requested_distance_m <= 0.0
        ):
            raise ValueError('requested_distance_m must be finite and > 0')
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('source field qualification hash mismatch')
        if self.qualification_id != _semantic_id('sfldqual', expected):
            raise ValueError(
                'source field qualification id does not match its hash'
            )
        return self


def source_field_binding(
    profile: SourceFieldProfile,
) -> AuthorityRef:
    return AuthorityRef(
        kind='source_field_profile',
        ref_id=profile.profile_id,
        ref_sha256=profile.profile_sha256,
    )


def source_field_qualification_binding(
    qualification: SourceFieldQualification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='source_field_qualification',
        ref_id=qualification.qualification_id,
        ref_sha256=qualification.qualification_sha256,
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

_NEAR_FIELD_REGIMES = {'near_field_scan', 'near_field_holography'}
_FAR_FIELD_REGIMES = {'anechoic_far_field', 'free_field_inferred'}
_APPROXIMATE_REGIMES = {
    'quasi_anechoic_gated',
    'ground_plane',
    'half_space',
    'installed_in_situ',
}


def evaluate_source_field_applicability(
    document_id: str,
    profile: SourceFieldProfile,
    *,
    requested_distance_m: float | None = None,
    requested_band_hz: FrequencyBand | None = None,
    requested_semantics: RequestedFieldSemantics = 'magnitude_angular',
    evaluated_at_utc: str | None = None,
) -> SourceFieldQualification:
    """Evaluate whether the dataset can answer the requested query.

    Fail-closed on every axis: a band outside every declared regime, a
    distance below a far-field regime's transition distance, or a
    distance-dependent request against a ``far_field_polar_only`` model
    are all refused explicitly.
    """

    evaluated_at_utc = evaluated_at_utc or _utc_now()
    reasons: list[str] = []
    limitations: list[str] = []
    effective_regime: MeasurementFieldRegime = 'unknown'

    # --- source-model capability gate ---------------------------------
    model = profile.default_source_model
    if requested_semantics in {
        'distance_dependent_complex',
        'multi_radiator_time_domain',
    } and model in {'far_field_polar_only', 'single_effective_source'}:
        verdict: ApplicabilityVerdict = (
            'requires_explicit_multi_radiator_model'
        )
        reasons.append(
            f'requested semantics {requested_semantics} need a '
            f'multi-radiator source model; profile declares {model}'
        )
        return _seal(
            SourceFieldQualification,
            {
                'document_id': document_id,
                'profile_ref': source_field_binding(profile),
                'verdict': verdict,
                'requested_distance_m': requested_distance_m,
                'requested_band_hz': (
                    requested_band_hz.model_dump(mode='json')
                    if requested_band_hz is not None
                    else None
                ),
                'requested_semantics': requested_semantics,
                'effective_regime': effective_regime,
                'reasons': sorted(set(reasons)),
                'limitations': sorted(set(limitations)),
                'evaluated_at_utc': evaluated_at_utc,
                'evaluation_version': SOURCE_FIELD_EVALUATION_VERSION,
            },
            'qualification_id',
            'qualification_sha256',
            'sfldqual',
        )

    # --- band coverage ------------------------------------------------
    covering: list[BandFieldRegime] = []
    if requested_band_hz is not None:
        covering = [
            b for b in profile.band_regimes
            if (
                b.band_hz.low_hz <= requested_band_hz.low_hz
                and b.band_hz.high_hz >= requested_band_hz.high_hz
            )
        ]
        if not covering:
            return _seal(
                SourceFieldQualification,
                {
                    'document_id': document_id,
                    'profile_ref': source_field_binding(profile),
                    'verdict': 'insufficient_evidence',
                    'requested_distance_m': requested_distance_m,
                    'requested_band_hz': (
                        requested_band_hz.model_dump(mode='json')
                    ),
                    'requested_semantics': requested_semantics,
                    'effective_regime': effective_regime,
                    'reasons': [
                        'no field-regime declaration covers the '
                        'requested band'
                    ],
                    'limitations': [],
                    'evaluated_at_utc': evaluated_at_utc,
                    'evaluation_version': (
                        SOURCE_FIELD_EVALUATION_VERSION
                    ),
                },
                'qualification_id',
                'qualification_sha256',
                'sfldqual',
            )
    else:
        covering = list(profile.band_regimes)

    regime = covering[0].regime
    effective_regime = regime
    applicable_distance = covering[0].applicable_distance_m
    transition_distance = covering[0].transition_distance_m

    # --- quasi-anechoic gate bound ------------------------------------
    gate = profile.measurement_geometry.gate_window_s
    if (
        regime == 'quasi_anechoic_gated'
        and gate is not None
        and requested_band_hz is not None
        and requested_band_hz.low_hz < 1.0 / gate
    ):
        if profile.near_far_derivation is None:
            return _seal(
                SourceFieldQualification,
                {
                    'document_id': document_id,
                    'profile_ref': source_field_binding(profile),
                    'verdict': 'insufficient_evidence',
                    'requested_distance_m': requested_distance_m,
                    'requested_band_hz': (
                        requested_band_hz.model_dump(mode='json')
                    ),
                    'requested_semantics': requested_semantics,
                    'effective_regime': effective_regime,
                    'reasons': [
                        'gate window cannot resolve the requested low '
                        'band and no near-far derivation covers it'
                    ],
                    'limitations': [],
                    'evaluated_at_utc': evaluated_at_utc,
                    'evaluation_version': (
                        SOURCE_FIELD_EVALUATION_VERSION
                    ),
                },
                'qualification_id',
                'qualification_sha256',
                'sfldqual',
            )
        limitations.append(
            'low band is below the gate limit; covered only by a '
            'derived estimate'
        )

    # --- distance gate --------------------------------------------------
    if requested_distance_m is not None:
        if regime in _NEAR_FIELD_REGIMES:
            if (
                applicable_distance is not None
                and requested_distance_m > applicable_distance.max_m
            ):
                if profile.near_far_derivation is not None:
                    verdict = 'applicable_with_approximation'
                    limitations.append(
                        'distance exceeds the near-field capture range; '
                        'answer comes from a declared near-far '
                        'derivation'
                    )
                else:
                    verdict = 'nearfield_only'
                    reasons.append(
                        'data is near-field only and the request lies '
                        'beyond the declared capture range'
                    )
                    return _qualify(
                        document_id, profile, verdict,
                        requested_distance_m, requested_band_hz,
                        requested_semantics, effective_regime,
                        reasons, limitations, evaluated_at_utc,
                    )
            else:
                verdict = 'directly_applicable'
        elif regime in _FAR_FIELD_REGIMES:
            min_distance = (
                applicable_distance.min_m
                if applicable_distance is not None
                else transition_distance
            )
            if (
                min_distance is not None
                and requested_distance_m < min_distance
            ):
                verdict = 'distance_too_close_for_selected_far_field_model'
                reasons.append(
                    'requested distance is inside the near field of a '
                    'far-field dataset'
                )
            elif (
                applicable_distance is not None
                and requested_distance_m > applicable_distance.max_m
            ):
                verdict = 'applicable_with_approximation'
                limitations.append(
                    'distance beyond the declared far-field range'
                )
            else:
                verdict = 'directly_applicable'
        elif regime in _APPROXIMATE_REGIMES:
            verdict = 'applicable_with_approximation'
            limitations.append(
                f'{regime} data is an approximation of free-field '
                'response'
            )
            if (
                applicable_distance is not None
                and not (
                    applicable_distance.min_m
                    <= requested_distance_m
                    <= applicable_distance.max_m
                )
            ):
                verdict = 'transition_field_limited'
                limitations.append(
                    'requested distance is outside the declared '
                    'applicable range'
                )
        else:  # unknown
            verdict = 'insufficient_evidence'
            reasons.append('measurement field regime never declared')
    else:
        if regime in _NEAR_FIELD_REGIMES:
            verdict = 'directly_applicable'
        elif regime in _FAR_FIELD_REGIMES:
            verdict = 'directly_applicable'
        elif regime in _APPROXIMATE_REGIMES:
            verdict = 'applicable_with_approximation'
            limitations.append(
                f'{regime} data is an approximation of free-field '
                'response'
            )
        else:
            verdict = 'insufficient_evidence'
            reasons.append('measurement field regime never declared')

    return _qualify(
        document_id, profile, verdict,
        requested_distance_m, requested_band_hz,
        requested_semantics, effective_regime,
        reasons, limitations, evaluated_at_utc,
    )


def _qualify(
    document_id: str,
    profile: SourceFieldProfile,
    verdict: ApplicabilityVerdict,
    requested_distance_m: float | None,
    requested_band_hz: FrequencyBand | None,
    requested_semantics: RequestedFieldSemantics,
    effective_regime: MeasurementFieldRegime,
    reasons: list[str],
    limitations: list[str],
    evaluated_at_utc: str,
) -> SourceFieldQualification:
    return _seal(
        SourceFieldQualification,
        {
            'document_id': document_id,
            'profile_ref': source_field_binding(profile),
            'verdict': verdict,
            'requested_distance_m': requested_distance_m,
            'requested_band_hz': (
                requested_band_hz.model_dump(mode='json')
                if requested_band_hz is not None
                else None
            ),
            'requested_semantics': requested_semantics,
            'effective_regime': effective_regime,
            'reasons': sorted(set(reasons)),
            'limitations': sorted(set(limitations)),
            'evaluated_at_utc': evaluated_at_utc,
            'evaluation_version': SOURCE_FIELD_EVALUATION_VERSION,
        },
        'qualification_id',
        'qualification_sha256',
        'sfldqual',
    )


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_source_field_profile(
    document_id: str,
    source_ref: AuthorityRef,
    measurement_geometry: MeasurementGeometrySpec,
    band_regimes: Sequence[BandFieldRegime],
    *,
    origin_ref: AuthorityRef | None = None,
    dataset_ref: AuthorityRef | None = None,
    default_source_model: SourceModelCapability = 'far_field_polar_only',
    near_far_derivation: NearFarDerivation | None = None,
    declared_at_utc: str | None = None,
) -> SourceFieldProfile:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        SourceFieldProfile,
        {
            'document_id': document_id,
            'source_ref': source_ref.model_dump(mode='json'),
            'origin_ref': (
                origin_ref.model_dump(mode='json')
                if origin_ref is not None
                else None
            ),
            'dataset_ref': (
                dataset_ref.model_dump(mode='json')
                if dataset_ref is not None
                else None
            ),
            'measurement_geometry': (
                measurement_geometry.model_dump(mode='json')
            ),
            'band_regimes': [
                b.model_dump(mode='json') for b in band_regimes
            ],
            'default_source_model': default_source_model,
            'near_far_derivation': (
                near_far_derivation.model_dump(mode='json')
                if near_far_derivation is not None
                else None
            ),
            'authority_version': SOURCE_FIELD_SCHEMA_VERSION,
            'declared_at_utc': declared_at_utc,
        },
        'profile_id',
        'profile_sha256',
        'sfldprof',
    )
