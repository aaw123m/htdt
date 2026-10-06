"""Sound-field diffuseness / statistical-model applicability authority
(#673, REV58-VALIDMETH).

Real rooms need not be diffuse just because an RT can be fitted, a
reverberant tail exists, or surfaces were modelled with scattering.
"Diffuse" is a sound-field property inside a *declared domain* —
frequency/time/spatial region/source-receiver conditions — and its
evidence can come from measurement, from a declared model-side
diffuseness indicator, or from an explicit theoretical assumption. Each
basis supports a different claim class; none is silently upgraded.

Scope discipline (#673):

- The diffuseness domain is identity — spatial extent, frequency band,
  time region, position class and source/receiver conditions; an
  estimator over a different domain says nothing outside it (#673 §3).
- A surface scattering coefficient does not imply field diffuseness
  (composes #252 — scattering is a surface-material property) and an
  RT tail does not prove diffuseness; the Schroeder-frequency / modal
  context of #571 gates interpretation, never proves the field is
  diffuse (#673 §2, §9).
- Estimator profiles are explicit: spatial level variance, spatial
  energy uniformity, directional energy isotropy, intensity-based
  diffuseness, spatial coherence, RIR source/receiver position
  sensitivity, multifractal RIR uniformity, declared model-internal
  diffuseness indicators, custom-validated methods (#673 §4).
- Eligibility is honest: non-diffuse, spatially nonuniform,
  directionally biased, transition region, insufficient evidence and
  estimator-incompatible are first-class states (#673 §6).
- Statistical diffuse-field behaviour is *claimed through* a
  SoundFieldDiffusenessAssessment — never inferred from geometry-only
  reasoning or scalar-RoomAcoustics alone (#673 §8).
- Diffuseness is not uniformity in energy decay time — non-diffuse
  fields with heavy early energy can still decay exponentially (#673
  §2; Hodgson 1996).

Records:

- :class:`DiffuseFieldDomainPin` — the declared domain identity.
- :class:`FieldUniformityProfile` — one declared estimator plus its
  evaluated metrics over a pinned domain.
- :class:`DiffusenessEvidenceRef` — a pinned evidence object carrying
  measurement vs model-derived provenance.
- :class:`SoundFieldDiffusenessAssessment` — the sealed eligibility
  assessment over a domain.
- :class:`StatisticalModelApplicabilityDeclaration` — the sealed
  downstream applicability decision from
  :func:`evaluate_diffuseness_applicability`.

Literature basis
----------------
- Hodgson (1996), "When is diffuse-field theory applicable?",
  Appl. Acoust. — DOI 10.1016/S0003-682X(96)00010-2. Diffuse-field
  theory is invalid in long/flat rooms with early strong reflections;
  applicability is a field property, not a property of RT alone.
- Prislan et al. (2014), JASA 135(6) — DOI 10.1121/1.4894797.
  Position-dependent sound field variance in rooms — spatial
  nonuniformity breaks statistical assumptions.
- Loutridis (2009), JASA 125(4) — DOI 10.1121/1.3075560. Structure of
  sound-field spatial variance / energy distribution.
- Del Galdo et al. (2012), JASA 131(6) — DOI 10.1121/1.3682064.
  Reverberation decay inside real enclosures as a spatially
  inhomogeneous stochastic process — decay consistency ≠ diffuseness.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


DIFFUSENESS_SCHEMA_VERSION = 'diffuseness-applicability-1'
DIFFUSENESS_EVALUATION_VERSION = 'diffuseness-applicability-eval-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _require_finite(value: float, label: str) -> None:
    if not isfinite(float(value)):
        raise ValueError(f'{label} must be finite')


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
        **{
            sha_field: digest,
            id_field: _semantic_id(prefix, digest),
        },
    )


def _require_ref_sha(ref: AuthorityRef | None, label: str) -> None:
    if ref is not None and ref.ref_sha256 is None:
        raise ValueError(f'{label} must pin its sha256')


# ----------------------------------------------------------------------
# Taxonomies

DiffusenessEstimatorKind = Literal[
    'spatial_level_variance',
    'spatial_energy_uniformity',
    'directional_energy_isotropy',
    'intensity_based_diffuseness',
    'spatial_coherence',
    'rir_source_position_sensitivity',
    'rir_receiver_position_sensitivity',
    'multifractal_rir_uniformity',
    'model_internal_diffuseness_indicator',
    'custom_validated_method',
]
"""#673 §4 — estimator profiles; each measures a different property and
none substitutes for the others."""

EvidenceProvenance = Literal[
    'measured',
    'model_derived_indicator',
    'theory_assumption',
]
"""Where the diffuseness evidence comes from; only `measured` can ever
carry a direct claim, `model_derived_indicator` is declared heuristic
evidence, `theory_assumption` is an assumption plus published limits —
never field evidence (#673 §10)."""

DiffuseFieldEligibilityState = Literal[
    'eligible_within_declared_domain',
    'eligible_with_limitations',
    'non_diffuse_field',
    'spatially_nonuniform',
    'directionally_biased',
    'transition_region',
    'insufficient_evidence',
    'estimator_incompatible',
]
"""#673 §6 — eligibility states; the honest failure modes are named."""

ApplicabilityBasis = Literal[
    'direct_measured_diffuseness_evaluation',
    'model_derived_diffuseness_estimate',
    'theory_assumption_and_limits',
    'partial_evidence_heuristic',
    'unsupported',
]
"""#673 §10 — the claim basis a downstream statistical method may
use."""

ApplicabilityState = Literal[
    'applicable_within_domain',
    'applicable_with_limitations',
    'not_applicable',
    'insufficient_evidence',
]
"""The fail-closed per-use verdict."""

StatisticalPropertyNeeded = Literal[
    'energy_uniformity',
    'directional_isotropy',
    'incoherent_tail',
    'ensemble_decay_statistics',
    'position_invariance',
]
"""What a statistical method actually needs; an energy-variance-only
estimator cannot satisfy a directional-isotropy requirement (#673
§11)."""

DiffusenessFixtureId = Literal[
    'DFF10', 'DFF20', 'DFF30', 'DFF40', 'DFF50', 'DFF60', 'DFF70',
    'DFF80',
]
"""#673 fixtures: DFF10 diffuse room via model indicator, DFF20
unevenly absorbing room, DFF30 coupling aperture transition, DFF40
live/end-long-room transition, DFF50 intensity evidence, DFF60
spatial-coherence evidence, DFF70 ensemble RIR uniformity, DFF80
material #570 intervention."""


# ----------------------------------------------------------------------
# Records

class DiffuseFieldDomainPin(BaseModel):
    """The declared domain of a diffuseness assessment (#673 §3).

    The domain is identity: it fixes spatial extent, frequency band,
    time region and the source/receiver conditions inside which the
    estimator claims anything at all.
    """

    model_config = ConfigDict(frozen=True)

    spatial_extent_ref: AuthorityRef | None = None
    """Pins a region/volume/grid artifact."""
    spatial_extent_label: str = Field(min_length=1)
    frequency_band_hz: tuple[float, float] | None = None
    time_region_s: tuple[float, float] | None = None
    position_class: str | None = None
    """e.g. source-independent distance class, audience plane, source
    zone — free-form but declared."""
    source_condition: str | None = None
    receiver_condition: str | None = None
    sample_position_refs: tuple[AuthorityRef, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'DiffuseFieldDomainPin':
        _require_ref_sha(self.spatial_extent_ref, 'spatial_extent_ref')
        if self.frequency_band_hz is not None:
            low, high = self.frequency_band_hz
            for bound, label in ((low, 'low'), (high, 'high')):
                _require_finite(bound, f'frequency band {label}')
            if low >= high:
                raise ValueError('frequency_band_hz must be low < high')
        if self.time_region_s is not None:
            low, high = self.time_region_s
            for bound, label in ((low, 'low'), (high, 'high')):
                _require_finite(bound, f'time region {label}')
            if low >= high:
                raise ValueError('time_region_s must be low < high')
        for ref in self.sample_position_refs:
            _require_ref_sha(ref, 'sample_position_refs')
        return self


class DiffusenessEvidenceRef(BaseModel):
    """A pinned evidence object with explicit provenance (#673 §10)."""

    model_config = ConfigDict(frozen=True)

    evidence_ref: AuthorityRef
    provenance: EvidenceProvenance
    label: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'DiffusenessEvidenceRef':
        _require_ref_sha(self.evidence_ref, 'evidence_ref')
        return self


class FieldUniformityProfile(BaseModel):
    """One declared estimator run over a pinned domain (#673 §4/§5).

    ``value``/`confidence_interval` are a *profile result* — no hidden
    thresholds. Whether the profile is compatible with a statistical
    claim is decided later by the assessment, which records which
    property the estimator actually covers.
    """

    model_config = ConfigDict(frozen=True)

    estimator: DiffusenessEstimatorKind
    method_parameters: tuple[str, ...] = ()
    """Canonical ``name=value`` strings."""
    covered_property: StatisticalPropertyNeeded
    domain: DiffuseFieldDomainPin
    evidence: tuple[DiffusenessEvidenceRef, ...] = ()
    value: float | None = None
    confidence_interval: tuple[float, float] | None = None
    """Declared interval — not a default ±x band."""
    estimator_version: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'FieldUniformityProfile':
        for ev in self.evidence:
            _require_ref_sha(ev.evidence_ref, 'uniformity evidence')
        if self.value is not None:
            _require_finite(self.value, 'uniformity value')
        if self.confidence_interval is not None:
            low, high = self.confidence_interval
            _require_finite(low, 'confidence interval low')
            _require_finite(high, 'confidence interval high')
            if low > high:
                raise ValueError(
                    'confidence_interval must be low <= high'
                )
        if self.value is None and not self.evidence:
            raise ValueError(
                'a uniformity profile needs an evaluated value or '
                'pinned evidence'
            )
        return self


class SoundFieldDiffusenessAssessment(BaseModel):
    """The sealed diffuseness eligibility assessment (#673 §5/§6).

    One sealed record per domain+evidence bundle; eligibility states
    stay honest — ``non_diffuse_field``, ``directionally_biased`` etc.
    are first-class outcomes, never silently converted into a diffuse
    claim.
    """

    model_config = ConfigDict(frozen=True)

    assessment_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    domain: DiffuseFieldDomainPin
    eligibility_state: DiffuseFieldEligibilityState
    estimator_profiles: tuple[FieldUniformityProfile, ...] = ()
    evidence_refs: tuple[DiffusenessEvidenceRef, ...] = ()
    covered_properties: tuple[StatisticalPropertyNeeded, ...] = ()
    known_conflicts: tuple[str, ...] = ()
    """Recorded conflicts between estimators (e.g. energy uniform but
    directionally biased) — never averaged away."""
    limitations: tuple[str, ...] = ()
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=DIFFUSENESS_SCHEMA_VERSION, min_length=1
    )
    assessment_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'domain': self.domain.model_dump(mode='json'),
            'eligibility_state': self.eligibility_state,
            'estimator_profiles': [
                p.model_dump(mode='json')
                for p in self.estimator_profiles
            ],
            'evidence_refs': [
                e.model_dump(mode='json') for e in self.evidence_refs
            ],
            'covered_properties': list(self.covered_properties),
            'known_conflicts': list(self.known_conflicts),
            'limitations': list(self.limitations),
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'SoundFieldDiffusenessAssessment':
        _require_iso8601(
            self.declared_at_utc, 'assessment declared_at_utc'
        )
        if not self.estimator_profiles and not self.evidence_refs:
            if self.eligibility_state not in (
                'insufficient_evidence',
                'estimator_incompatible',
            ):
                raise ValueError(
                    'an assessment without estimators or evidence can '
                    'only be insufficient_evidence / '
                    'estimator_incompatible'
                )
        if len(set(self.covered_properties)) != len(
            self.covered_properties
        ):
            raise ValueError('covered_properties must be unique')
        for ev in self.evidence_refs:
            _require_ref_sha(ev.evidence_ref, 'evidence_refs')
        expected = _hash(self.identity_payload())
        if self.assessment_sha256 != expected:
            raise ValueError('diffuseness assessment hash mismatch')
        if self.assessment_id != _semantic_id('diffassess', expected):
            raise ValueError(
                'diffuseness assessment id does not match its hash'
            )
        return self


def build_diffuseness_assessment(
    *,
    document_id: str,
    domain: DiffuseFieldDomainPin,
    eligibility_state: DiffuseFieldEligibilityState,
    estimator_profiles: Sequence[FieldUniformityProfile] = (),
    evidence_refs: Sequence[DiffusenessEvidenceRef] = (),
    covered_properties: Sequence[StatisticalPropertyNeeded] = (),
    known_conflicts: Sequence[str] = (),
    limitations: Sequence[str] = (),
    declared_at_utc: str | None = None,
) -> SoundFieldDiffusenessAssessment:
    """Seal one diffuseness assessment."""
    return _seal(
        SoundFieldDiffusenessAssessment,
        {
            'document_id': document_id,
            'domain': domain.model_dump(mode='json'),
            'eligibility_state': eligibility_state,
            'estimator_profiles': [
                p.model_dump(mode='json') for p in estimator_profiles
            ],
            'evidence_refs': [
                e.model_dump(mode='json') for e in evidence_refs
            ],
            'covered_properties': list(covered_properties),
            'known_conflicts': list(known_conflicts),
            'limitations': list(limitations),
            'declared_at_utc': declared_at_utc or _utc_now(),
        },
        'assessment_id',
        'assessment_sha256',
        'diffassess',
    )


def diffuseness_assessment_binding(
    assessment: SoundFieldDiffusenessAssessment,
) -> AuthorityRef:
    return AuthorityRef(
        kind='sound_field_diffuseness_assessment',
        ref_id=assessment.assessment_id,
        ref_sha256=assessment.assessment_sha256,
    )


# ----------------------------------------------------------------------
# Applicability declaration

class StatisticalModelApplicabilityDeclaration(BaseModel):
    """Sealed decision: may this statistical method claim diffuse-field
    behaviour inside this domain? (#673 §8/§10/§11)."""

    model_config = ConfigDict(frozen=True)

    declaration_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    assessment_ref: AuthorityRef | None = None
    use_domain: DiffuseFieldDomainPin
    needed_properties: tuple[StatisticalPropertyNeeded, ...] = ()
    state: ApplicabilityState
    basis: ApplicabilityBasis
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_version: str = Field(
        default=DIFFUSENESS_EVALUATION_VERSION, min_length=1
    )
    declaration_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'assessment_ref': (
                self.assessment_ref.model_dump(mode='json')
                if self.assessment_ref is not None
                else None
            ),
            'use_domain': self.use_domain.model_dump(mode='json'),
            'needed_properties': list(self.needed_properties),
            'state': self.state,
            'basis': self.basis,
            'reasons': list(self.reasons),
            'limitations': list(self.limitations),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'StatisticalModelApplicabilityDeclaration':
        _require_iso8601(
            self.evaluated_at_utc, 'declaration evaluated_at_utc'
        )
        _require_ref_sha(self.assessment_ref, 'assessment_ref')
        if (
            self.assessment_ref is not None
            and self.assessment_ref.kind
            != 'sound_field_diffuseness_assessment'
        ):
            raise ValueError(
                'assessment_ref must pin a '
                "'sound_field_diffuseness_assessment'"
            )
        if (
            self.state == 'applicable_within_domain'
            and self.basis == 'unsupported'
        ):
            raise ValueError(
                'an unsupported basis cannot reach '
                'applicable_within_domain'
            )
        if (
            self.basis == 'direct_measured_diffuseness_evaluation'
            and self.assessment_ref is None
        ):
            raise ValueError(
                'a direct-measured basis requires a pinned assessment'
            )
        if len(set(self.needed_properties)) != len(
            self.needed_properties
        ):
            raise ValueError('needed_properties must be unique')
        expected = _hash(self.identity_payload())
        if self.declaration_sha256 != expected:
            raise ValueError(
                'applicability declaration hash mismatch'
            )
        if self.declaration_id != _semantic_id('diffdec', expected):
            raise ValueError(
                'applicability declaration id does not match its hash'
            )
        return self


def diffuseness_declaration_binding(
    declaration: StatisticalModelApplicabilityDeclaration,
) -> AuthorityRef:
    return AuthorityRef(
        kind='statistical_model_applicability_declaration',
        ref_id=declaration.declaration_id,
        ref_sha256=declaration.declaration_sha256,
    )


def evaluate_diffuseness_applicability(
    document_id: str,
    assessment: SoundFieldDiffusenessAssessment | None,
    *,
    use_domain: DiffuseFieldDomainPin,
    needed_properties: Sequence[StatisticalPropertyNeeded] = (),
    evaluated_at_utc: str | None = None,
) -> StatisticalModelApplicabilityDeclaration:
    """Fail-closed statistical-model applicability decision (#673).

    Rules:

    - No assessment → ``insufficient_evidence``/``unsupported``.
    - ``non_diffuse_field``, ``directionally_biased`` (when directional
      properties are needed), ``estimator_incompatible`` or
      ``insufficient_evidence`` eligibility → ``not_applicable``.
    - Theoretical-only evidence can never exceed
      ``applicable_with_limitations`` on a
      ``theory_assumption_and_limits`` basis — never a direct claim.
    - Every needed property must be covered by the assessment; any gap
      → ``applicable_with_limitations`` naming the uncovered property.
    - Conflicts between estimators are kept, not averaged (#673 §11).
    """

    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')

    reasons: list[str] = []
    limitations: list[str] = []
    needed = tuple(needed_properties)

    if assessment is None:
        return _seal(
            StatisticalModelApplicabilityDeclaration,
            {
                'document_id': document_id,
                'assessment_ref': None,
                'use_domain': use_domain.model_dump(mode='json'),
                'needed_properties': list(needed),
                'state': 'insufficient_evidence',
                'basis': 'unsupported',
                'reasons': (
                    'no sound-field diffuseness assessment declared — '
                    'statistical diffuse-field behaviour cannot be '
                    'claimed',
                ),
                'limitations': (),
                'evaluated_at_utc': evaluated_at_utc,
            },
            'declaration_id',
            'declaration_sha256',
            'diffdec',
        )

    state: ApplicabilityState
    basis: ApplicabilityBasis

    if assessment.eligibility_state == 'insufficient_evidence':
        state = 'insufficient_evidence'
        basis = 'unsupported'
        reasons.append(
            'the assessment itself has insufficient evidence'
        )
    elif assessment.eligibility_state == 'estimator_incompatible':
        state = 'not_applicable'
        basis = 'unsupported'
        reasons.append(
            'no declared estimator is compatible with the required '
            'property'
        )
    elif assessment.eligibility_state == 'non_diffuse_field':
        state = 'not_applicable'
        basis = 'unsupported'
        reasons.append(
            'the field was assessed non-diffuse inside the declared '
            'domain'
        )
    elif (
        assessment.eligibility_state == 'directionally_biased'
        and 'directional_isotropy' in needed
    ):
        state = 'not_applicable'
        basis = 'unsupported'
        reasons.append(
            'directional bias contradicts the isotropy the method '
            'needs'
        )
    elif assessment.eligibility_state in (
        'spatially_nonuniform',
        'directionally_biased',
        'transition_region',
    ):
        state = 'applicable_with_limitations'
        basis = 'partial_evidence_heuristic'
        limitations.append(
            f'domain eligibility is '
            f'{assessment.eligibility_state} — statistical claims are '
            'limited to the unaffected sub-domain if any'
        )
    else:
        provenances = {
            ev.provenance for ev in assessment.evidence_refs
        } | {
            ev.provenance
            for p in assessment.estimator_profiles
            for ev in p.evidence
        }
        if 'measured' in provenances:
            basis = 'direct_measured_diffuseness_evaluation'
        elif 'model_derived_indicator' in provenances:
            basis = 'model_derived_diffuseness_estimate'
        elif 'theory_assumption' in provenances:
            basis = 'theory_assumption_and_limits'
        else:
            basis = 'partial_evidence_heuristic'

        missing = set(needed) - set(assessment.covered_properties)
        if missing:
            state = 'applicable_with_limitations'
            limitations.append(
                'needed properties not covered by any estimator: '
                + ', '.join(sorted(missing))
            )
        elif basis == 'theory_assumption_and_limits':
            state = 'applicable_with_limitations'
            limitations.append(
                'basis is a theoretical assumption plus published '
                'limits — not field evidence'
            )
        elif basis == 'model_derived_diffuseness_estimate':
            state = 'applicable_with_limitations'
            limitations.append(
                'basis is a declared model-internal diffuseness '
                'indicator — heuristic evidence, not measured '
                'diffuseness'
            )
        elif assessment.eligibility_state == (
            'eligible_with_limitations'
        ):
            state = 'applicable_with_limitations'
        else:
            state = 'applicable_within_domain'

    for conflict in assessment.known_conflicts:
        limitations.append(f'estimator conflict kept: {conflict}')
    for limitation in assessment.limitations:
        limitations.append(f'assessment limitation: {limitation}')
    if assessment.known_conflicts and state == 'applicable_within_domain':
        state = 'applicable_with_limitations'

    return _seal(
        StatisticalModelApplicabilityDeclaration,
        {
            'document_id': document_id,
            'assessment_ref': diffuseness_assessment_binding(
                assessment
            ).model_dump(mode='json'),
            'use_domain': use_domain.model_dump(mode='json'),
            'needed_properties': list(needed),
            'state': state,
            'basis': basis,
            'reasons': tuple(reasons),
            'limitations': tuple(dict.fromkeys(limitations)),
            'evaluated_at_utc': evaluated_at_utc,
        },
        'declaration_id',
        'declaration_sha256',
        'diffdec',
    )
