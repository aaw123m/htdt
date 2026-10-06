"""Loudspeaker acoustic-reference origin / phase-center authority (#654).

A loudspeaker's physical propagation origin is not silently defined by the
CAD cabinet pose. This authority records which declared reference point a
model uses as its acoustic origin, the evidence class behind each
acoustic-center estimate, and the band-limited frequency range over which a
single effective origin is claimed. Models that cannot show an origin
declaration are evaluated ``origin_unverified`` / ``insufficient_evidence``
instead of defaulting to the cabinet geometry.

The module follows the sealed-authority pattern used by
``cad_hybrid_handoff_authority``: every record is a frozen pydantic model
whose identity is a canonical-sha256-pinned id, and every stored field is
part of ``identity_payload()``.

Scope discipline:

* The authority covers *declaration and evidence* of the origin — it does
  not compute an acoustic center. Measurement-side capture is #1006
  (``cad_multi_radiator_source.SourceReferencePoints``), which already
  separates the geometric reference from the dataset phase origin and the
  effective acoustic center; this profile is the document-level record
  those per-source fields pin to.
* A declared origin is per ``FrequencyBand`` when it is band-limited; a
  single point claim outside its declared band is an honesty violation.
* Boundary state changes (free-field reference vs installed boundary)
  invalidate an estimate made under a different boundary condition.

Literature basis: Keele, "The Effective Acoustic Center of Loudspeakers"
(JAES preprint lineage); Vanderkooy, "How Loudspeaker Driver
Nonlinearities Affect the Acoustic Centre"; the acoustic-centre
literature showing the phase centre migrates with frequency at low
frequency for multi-driver systems, and that inter-driver distance
relative to wavelength bounds the region where a single effective origin
is a physically meaningful model.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_bass_management import FrequencyBand
from .cad_scene import Offset3
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


SOURCE_ORIGIN_SCHEMA_VERSION = 'source-origin-1'
SOURCE_ORIGIN_EVALUATION_VERSION = 'source-origin-eval-1'

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

ReferencePointKind = Literal[
    'cad_object_origin',
    'cad_object_pivot',
    'enclosure_front_baffle_center',
    'enclosure_driver_axis_intersection',
    'dataset_measurement_origin',
    'dataset_phase_origin',
    'manufacturer_declared_reference',
    'low_frequency_acoustic_center',
    'band_limited_effective_acoustic_center',
    'frequency_dependent_effective_origin',
    'no_single_center_model',
    'solver_equivalent_source_point',
    'installed_geometry_anchor',
    'user_declared_point',
    'unknown',
]
"""Every named reference a model can use as its propagation origin. The
``unknown`` member exists so a model can honestly declare that no origin
was ever identified (ACO80-class)."""

OriginEvidenceClass = Literal[
    'directly_measured',
    'derived_from_time_delay',
    'derived_from_phase_reference',
    'derived_from_manufacturer_data',
    'inferred_from_geometry',
    'assumed_at_driver_aperture',
    'assumed_at_enclosure_center',
    'user_assumed',
    'unknown',
]
"""How the claimed origin was established. ``user_assumed``/``unknown``
cap the verdict at ``origin_limited``/``origin_unverified``."""

OriginModelKind = Literal[
    'low_frequency_center',
    'band_limited_center',
    'frequency_dependent_effective_origin',
    'no_single_center_model',
]
"""The single-origin model the estimate asserts. ``no_single_center_model``
is an explicit honesty declaration — it carries no position."""

OriginCapability = Literal[
    'magnitude_only_positioning',
    'relative_phase_geometry',
    'absolute_propagation_delay',
    'absolute_phase_reference',
]
"""Capability ceiling the declared origin supports, ordered from weakest
to strongest. ``absolute_phase_reference`` needs a declared phase-time
reference or delay-derived evidence; it cannot be asserted from geometry
alone."""

BoundaryBinding = Literal[
    'free_field_reference',
    'installed_boundary',
    'manufacturer_boundary_dsp',
    'screen_baffle_configuration',
]
"""Boundary state under which the estimate was made. A change in the
installed state invalidates estimates bound to a different one."""

OriginVerdict = Literal[
    'origin_qualified',
    'origin_limited',
    'origin_unverified',
    'insufficient_evidence',
]


_CAPABILITY_RANK = {
    'magnitude_only_positioning': 0,
    'relative_phase_geometry': 1,
    'absolute_propagation_delay': 2,
    'absolute_phase_reference': 3,
}

_MODEL_TO_POINT_KIND: dict[str, ReferencePointKind] = {
    'low_frequency_center': 'low_frequency_acoustic_center',
    'band_limited_center': 'band_limited_effective_acoustic_center',
    'frequency_dependent_effective_origin': (
        'frequency_dependent_effective_origin'
    ),
    'no_single_center_model': 'no_single_center_model',
}


# ---------------------------------------------------------------------------
# Sub-specs
# ---------------------------------------------------------------------------


class DeclaredReferencePoint(BaseModel):
    """One named point in the enclosure frame with its declared kind.

    ``kind='unknown'`` cannot carry a position: the declaration is then a
    marker that no origin was identified.
    """

    model_config = ConfigDict(frozen=True)

    kind: ReferencePointKind
    position_m: Offset3 | None = None
    ref: AuthorityRef | None = None
    note: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'DeclaredReferencePoint':
        if self.kind == 'unknown' and self.position_m is not None:
            raise ValueError(
                'unknown reference point kind cannot assert a position'
            )
        if self.ref is not None and self.ref.ref_sha256 is None:
            raise ValueError('reference point ref must pin its sha256')
        return self


class OriginUncertainty(BaseModel):
    """Per-axis positional uncertainty of an acoustic-center estimate."""

    model_config = ConfigDict(frozen=True)

    sigma_x_m: float = Field(ge=0.0)
    sigma_y_m: float = Field(ge=0.0)
    sigma_z_m: float = Field(ge=0.0)
    band_residual_db: float = Field(default=0.0, ge=0.0)

    @model_validator(mode='after')
    def _check(self) -> 'OriginUncertainty':
        for label, v in (
            ('sigma_x_m', self.sigma_x_m),
            ('sigma_y_m', self.sigma_y_m),
            ('sigma_z_m', self.sigma_z_m),
            ('band_residual_db', self.band_residual_db),
        ):
            if not isfinite(float(v)):
                raise ValueError(f'{label} must be finite')
        return self


class DatasetReferenceFrame(BaseModel):
    """Declared provenance of the dataset-side measurement frame.

    The frame can be declared without an identified origin
    (``origin_kind='unknown'``) — the absence is then honest rather than
    silently defaulted.
    """

    model_config = ConfigDict(frozen=True)

    axes_convention: str = 'rhs'
    reference_axis: str = 'z'
    origin_kind: ReferencePointKind = 'unknown'
    pivot_kind: ReferencePointKind = 'unknown'
    measurement_distance_definition: str = ''
    source_orientation: str = ''
    phase_time_reference: str = ''
    validity_band_hz: FrequencyBand | None = None
    standard_profile: str = ''
    provenance: str = ''


class AcousticCenterEstimate(BaseModel):
    """A sealed per-band estimate of the effective acoustic centre.

    * ``band_limited_center`` requires a declared ``valid_band_hz``.
    * ``no_single_center_model`` is an explicit declaration that no
      single origin is physically meaningful; it cannot assert a
      position.
    * ``directly_measured`` evidence requires a method declaration.
    """

    model_config = ConfigDict(frozen=True)

    model_kind: OriginModelKind
    evidence_class: OriginEvidenceClass
    position_m: Offset3 | None = None
    valid_band_hz: FrequencyBand | None = None
    uncertainty_m: OriginUncertainty | None = None
    boundary_binding: BoundaryBinding = 'free_field_reference'
    method: str = ''
    notes: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'AcousticCenterEstimate':
        if self.model_kind == 'no_single_center_model' and (
            self.position_m is not None
        ):
            raise ValueError(
                'no_single_center_model cannot assert an acoustic '
                'centre position'
            )
        if (
            self.model_kind == 'band_limited_center'
            and self.valid_band_hz is None
        ):
            raise ValueError(
                'band_limited_center requires a declared valid_band_hz'
            )
        if (
            self.evidence_class == 'directly_measured'
            and not self.method
        ):
            raise ValueError(
                'directly_measured acoustic centre requires a method '
                'declaration'
            )
        if (
            self.model_kind
            in {
                'low_frequency_center',
                'band_limited_center',
                'frequency_dependent_effective_origin',
            }
            and self.position_m is None
        ):
            raise ValueError(
                f'{self.model_kind} requires an asserted position_m'
            )
        return self


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


class SourceReferenceOriginProfile(BaseModel):
    """Sealed profile declaring which origin model a source uses.

    ``source_ref`` pins the excitation/source document by content hash so
    a source revision cannot silently change the origin claim.
    ``declared_points`` lists every named point the model has declared;
    ``acoustic_center_estimates`` carries the band-limited estimates with
    their evidence classes.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    source_ref: AuthorityRef
    dataset_frame: DatasetReferenceFrame | None = None
    declared_points: tuple[DeclaredReferencePoint, ...] = ()
    acoustic_center_estimates: tuple[AcousticCenterEstimate, ...] = ()
    boundary_state: BoundaryBinding = 'free_field_reference'
    capability: OriginCapability = 'magnitude_only_positioning'
    multi_radiator_ref: AuthorityRef | None = None
    authority_version: str = Field(
        default=SOURCE_ORIGIN_SCHEMA_VERSION, min_length=1
    )
    declared_at_utc: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'source_ref': self.source_ref.model_dump(mode='json'),
            'dataset_frame': (
                self.dataset_frame.model_dump(mode='json')
                if self.dataset_frame is not None
                else None
            ),
            'declared_points': [
                p.model_dump(mode='json') for p in self.declared_points
            ],
            'acoustic_center_estimates': [
                e.model_dump(mode='json')
                for e in self.acoustic_center_estimates
            ],
            'boundary_state': self.boundary_state,
            'capability': self.capability,
            'multi_radiator_ref': (
                self.multi_radiator_ref.model_dump(mode='json')
                if self.multi_radiator_ref is not None
                else None
            ),
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'SourceReferenceOriginProfile':
        _require_iso8601(self.declared_at_utc, 'profile declared_at_utc')
        if self.source_ref.ref_sha256 is None:
            raise ValueError('source_ref must pin its sha256')
        if self.multi_radiator_ref is not None and (
            self.multi_radiator_ref.ref_sha256 is None
        ):
            raise ValueError('multi_radiator_ref must pin its sha256')
        kinds = [p.kind for p in self.declared_points]
        if len(set(kinds)) != len(kinds):
            raise ValueError('duplicate declared reference point kinds')
        if self.capability in {
            'absolute_propagation_delay',
            'absolute_phase_reference',
        }:
            has_frame = (
                self.dataset_frame is not None
                and bool(self.dataset_frame.phase_time_reference)
            )
            has_delay_evidence = any(
                e.evidence_class
                in {'directly_measured', 'derived_from_time_delay'}
                for e in self.acoustic_center_estimates
            )
            if not (has_frame or has_delay_evidence):
                raise ValueError(
                    'absolute propagation/phase capability requires a '
                    'declared phase-time reference or delay-derived '
                    'acoustic centre'
                )
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('source origin profile hash mismatch')
        if self.profile_id != _semantic_id('sorprof', expected):
            raise ValueError(
                'source origin profile id does not match its hash'
            )
        return self


class SourceOriginQualification(BaseModel):
    """Sealed verdict of :func:`evaluate_source_origin`."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    verdict: OriginVerdict
    requested_capability: OriginCapability
    requested_band_hz: FrequencyBand | None = None
    effective_origin_kind: ReferencePointKind = 'unknown'
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_version: str = Field(
        default=SOURCE_ORIGIN_EVALUATION_VERSION, min_length=1
    )
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'verdict': self.verdict,
            'requested_capability': self.requested_capability,
            'requested_band_hz': (
                self.requested_band_hz.model_dump(mode='json')
                if self.requested_band_hz is not None
                else None
            ),
            'effective_origin_kind': self.effective_origin_kind,
            'reasons': list(self.reasons),
            'limitations': list(self.limitations),
            'evaluated_at_utc': self.evaluated_at_utc,
            'evaluation_version': self.evaluation_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'SourceOriginQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        if self.profile_ref.ref_sha256 is None:
            raise ValueError('profile_ref must pin its sha256')
        if self.profile_ref.kind != 'source_reference_origin_profile':
            raise ValueError(
                "profile_ref must pin a 'source_reference_origin_profile'"
            )
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('source origin qualification hash mismatch')
        if self.qualification_id != _semantic_id('sorqual', expected):
            raise ValueError(
                'source origin qualification id does not match its hash'
            )
        return self


def source_origin_binding(
    profile: SourceReferenceOriginProfile,
) -> AuthorityRef:
    return AuthorityRef(
        kind='source_reference_origin_profile',
        ref_id=profile.profile_id,
        ref_sha256=profile.profile_sha256,
    )


def source_origin_qualification_binding(
    qualification: SourceOriginQualification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='source_origin_qualification',
        ref_id=qualification.qualification_id,
        ref_sha256=qualification.qualification_sha256,
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _band_covers(
    outer: FrequencyBand | None, inner: FrequencyBand,
) -> bool:
    if outer is None:
        return False
    return (
        outer.low_hz <= inner.low_hz
        and outer.high_hz >= inner.high_hz
    )


def evaluate_source_origin(
    document_id: str,
    profile: SourceReferenceOriginProfile,
    *,
    requested_capability: OriginCapability = 'magnitude_only_positioning',
    requested_band_hz: FrequencyBand | None = None,
    evaluated_at_utc: str | None = None,
) -> SourceOriginQualification:
    """Evaluate whether the declared origin supports the requested use.

    Fail-closed: an undeclared origin is ``origin_unverified`` /
    ``insufficient_evidence``, never silently assumed from the cabinet
    pose.
    """

    evaluated_at_utc = evaluated_at_utc or _utc_now()
    reasons: list[str] = []
    limitations: list[str] = []
    effective_origin_kind: ReferencePointKind = 'unknown'

    estimates = list(profile.acoustic_center_estimates)
    frame = profile.dataset_frame

    def _covers_band(est: AcousticCenterEstimate) -> bool:
        if requested_band_hz is None:
            return True
        if est.model_kind in {
            'low_frequency_center',
            'frequency_dependent_effective_origin',
        }:
            return True
        return _band_covers(est.valid_band_hz, requested_band_hz)

    covering = [e for e in estimates if _covers_band(e)]

    if not estimates and (frame is None or frame.origin_kind == 'unknown'):
        verdict: OriginVerdict = 'insufficient_evidence'
        reasons.append(
            'no acoustic centre estimate and no dataset origin '
            'declaration'
        )
    elif not estimates:
        verdict = 'origin_unverified'
        effective_origin_kind = (
            frame.origin_kind if frame is not None else 'unknown'
        )
        reasons.append(
            'dataset frame declares an origin but no acoustic centre '
            'estimate supports it'
        )
    elif not covering:
        verdict = 'origin_limited'
        limitations.append(
            'no acoustic centre declared for the requested band'
        )
    else:
        est = covering[0]
        if est.model_kind == 'no_single_center_model':
            verdict = 'origin_limited'
            effective_origin_kind = 'no_single_center_model'
            limitations.append(
                'model declares that no single effective origin is '
                'physically meaningful'
            )
        else:
            effective_origin_kind = _MODEL_TO_POINT_KIND[est.model_kind]
            verdict = 'origin_qualified'
            if est.boundary_binding != profile.boundary_state:
                limitations.append(
                    'acoustic centre boundary binding differs from the '
                    'installed boundary state'
                )
                verdict = 'origin_limited'
            if (
                est.evidence_class
                in {
                    'user_assumed',
                    'inferred_from_geometry',
                    'assumed_at_driver_aperture',
                    'assumed_at_enclosure_center',
                }
            ):
                limitations.append(
                    f'acoustic centre is {est.evidence_class}, not '
                    'measured'
                )
                verdict = 'origin_limited'

    if verdict in {'origin_qualified', 'origin_limited'}:
        if (
            _CAPABILITY_RANK[profile.capability]
            < _CAPABILITY_RANK[requested_capability]
        ):
            limitations.append(
                f'requested capability {requested_capability} exceeds '
                f'declared {profile.capability}'
            )
            verdict = 'origin_limited'
        elif requested_capability in {
            'absolute_propagation_delay',
            'absolute_phase_reference',
        }:
            has_delay = any(
                e.evidence_class
                in {'directly_measured', 'derived_from_time_delay'}
                for e in estimates
            )
            has_frame_ref = (
                frame is not None and bool(frame.phase_time_reference)
            )
            if not (has_delay or has_frame_ref):
                limitations.append(
                    'absolute phase use has no declared phase-time '
                    'reference or delay-derived evidence'
                )
                verdict = 'origin_limited'

    return _seal(
        SourceOriginQualification,
        {
            'document_id': document_id,
            'profile_ref': source_origin_binding(profile),
            'verdict': verdict,
            'requested_capability': requested_capability,
            'requested_band_hz': (
                requested_band_hz.model_dump(mode='json')
                if requested_band_hz is not None
                else None
            ),
            'effective_origin_kind': effective_origin_kind,
            'reasons': sorted(set(reasons)),
            'limitations': sorted(set(limitations)),
            'evaluated_at_utc': evaluated_at_utc,
            'evaluation_version': SOURCE_ORIGIN_EVALUATION_VERSION,
        },
        'qualification_id',
        'qualification_sha256',
        'sorqual',
    )


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_source_reference_origin_profile(
    document_id: str,
    source_ref: AuthorityRef,
    *,
    dataset_frame: DatasetReferenceFrame | None = None,
    declared_points: Sequence[DeclaredReferencePoint] = (),
    acoustic_center_estimates: Sequence[AcousticCenterEstimate] = (),
    boundary_state: BoundaryBinding = 'free_field_reference',
    capability: OriginCapability = 'magnitude_only_positioning',
    multi_radiator_ref: AuthorityRef | None = None,
    declared_at_utc: str | None = None,
) -> SourceReferenceOriginProfile:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        SourceReferenceOriginProfile,
        {
            'document_id': document_id,
            'source_ref': source_ref.model_dump(mode='json'),
            'dataset_frame': (
                dataset_frame.model_dump(mode='json')
                if dataset_frame is not None
                else None
            ),
            'declared_points': [
                p.model_dump(mode='json') for p in declared_points
            ],
            'acoustic_center_estimates': [
                e.model_dump(mode='json')
                for e in acoustic_center_estimates
            ],
            'boundary_state': boundary_state,
            'capability': capability,
            'multi_radiator_ref': (
                multi_radiator_ref.model_dump(mode='json')
                if multi_radiator_ref is not None
                else None
            ),
            'authority_version': SOURCE_ORIGIN_SCHEMA_VERSION,
            'declared_at_utc': declared_at_utc,
        },
        'profile_id',
        'profile_sha256',
        'sorprof',
    )
