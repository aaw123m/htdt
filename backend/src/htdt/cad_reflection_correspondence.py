"""Predicted↔measured early-reflection correspondence authority (#677,
REV58-VALIDMETH).

A predicted reflection path (ordered surface/edge interactions) and an
observed reflection event (a gated ETC peak, a sparse-decomposition
arrival, a labelled early component) are different kinds of identity.
Associating them requires **compound evidence** — arrival time,
direction of arrival, geometric path consistency, reflection order,
level compatibility, spectral signature, cross-position consistency —
declared per match. "Nearest ETC peak" as the whole correspondence is
rejected for any calibration claim.

Scope discipline (#677):

- Predicted path identity is the ordered interaction sequence — two
  paths sharing an arrival time but different wall sequences are
  different paths (#677 §2).
- Observed event identity is a measured interval/extent with its
  extraction algorithm — a peak picked by one algorithm is not the same
  event as one picked by another (#677 §3).
- Direct-path registration is a prerequisite — unregistered
  correspondences are invalid for level/delay interpretation
  (composes #564 registration, #609 clock, #654 frame alignment)
  (#677 §4).
- Correspondence states are honest: one-to-one, many-to-one,
  one-to-many, unresolved cluster, unmatched on either side,
  ambiguous, outside observation capability (#677 §6).
- Specular / diffraction / scattered classes stay separate — a
  scattered event is not a failed specular path (#677 §7).
- Correspondence consumed to calibrate surfaces/materials can never
  double as independent validation of those same surfaces (#677 §11).

Records:

- :class:`PredictedReflectionPath` — solver-side ordered-interaction
  path pin.
- :class:`ObservedReflectionEvent` — measured/derived event pin with
  extraction-algorithm provenance.
- :class:`ReflectionCorrespondencePairing` — one declared association
  with its evidence dimensions and ambiguity.
- :class:`ReflectionCorrespondenceSet` — the sealed set-level record
  for one source↔receiver pair, requiring direct-path registration.
- :class:`ReflectionCorrespondenceVerdict` — the fail-closed verdict
  from :func:`evaluate_reflection_correspondence`.

Literature basis
----------------
- dEchorate corpus — Di Carlo et al., arXiv:2104.13168. Annotated
  early-reflection ground truth: peak↔wall correspondence needs
  geometry, not nearest-peak assignment.
- Lovedee-Turner & Murphy (2019), JASA — DOI 10.1121/1.5130569.
  Spatial decomposition of room impulse responses — early-reflection
  arrivals decomposed by direction, not just time.
- Dokmanić et al. (room-geometry / echo-sorting lineage). Geometric
  consistency of echoes across positions.
- Tsunokuni et al. (2021), Appl. Acoust. — DOI
  10.1016/j.apacoust.2021.108027. Estimating early reflections from
  measured RIRs; extraction-algorithm dependence.
- Defrance & Polack (referenced in
  ``cad_prediction_measurement_registration``) — time-windowed
  reflection estimation for registration.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


REFLECTION_CORRESPONDENCE_SCHEMA_VERSION = (
    'reflection-correspondence-1'
)
REFLECTION_CORRESPONDENCE_EVALUATION_VERSION = (
    'reflection-correspondence-eval-1'
)

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

ReflectionPathClass = Literal[
    'specular',
    'edge_diffraction',
    'surface_scattering',
    'mixed',
]
"""#677 §7 — path classes stay separate; a scattered event is not a
failed specular path."""

CorrespondenceEvidenceKind = Literal[
    'time_alignment',
    'direction_of_arrival',
    'geometric_path_consistency',
    'reflection_order',
    'level_compatibility',
    'spectral_signature',
    'cross_position_consistency',
]
"""#677 §5 — compound evidence dimensions; each stays inspectable."""

CorrespondenceState = Literal[
    'one_to_one',
    'many_to_one',
    'one_to_many',
    'unresolved_cluster',
    'unmatched_predicted',
    'unmatched_observed',
    'ambiguous',
    'outside_observation_capability',
]
"""#677 §6 — honest association states."""

MatchingAlgorithmKind = Literal[
    'manual_expert_label',
    'time_gate_peak_match',
    'time_doa_geometric_match',
    'probabilistic_assignment',
    'sparse_decomposition_match',
    'multi_position_joint_assignment',
    'custom_validated',
]
"""#677 §8 — provenance of the association algorithm."""

ExtractionAlgorithmKind = Literal[
    'etc_peak_gate',
    'sparse_decomposition',
    'spatial_decomposition',
    'beamforming_estimate',
    'manual_annotation',
    'custom_validated',
]
"""How the observed event was extracted from the measurement."""

CorrespondenceQualificationState = Literal[
    'qualified',
    'qualified_with_limitations',
    'insufficient_evidence',
    'registration_prerequisite_missing',
    'ambiguous_unresolved',
    'calibration_only_no_independent_validation',
]
"""Fail-closed verdict states; the last state is the calibration
guardrail — matched pairs consumed by calibration never validate the
calibrated object (#677 §11)."""

ReflectionCorrespondenceFixtureId = Literal[
    'RPA10', 'RPA20', 'RPA30', 'RPA40', 'RPA50', 'RPA60', 'RPA70',
    'RPA80', 'RPA90',
]
"""#677 fixtures: RPA10 shoebox analytic path↔peaks, RPA20 two walls
close in time (DOA needed), RPA30 ambiguous cluster, RPA40 predicted
without observation (occlusion), RPA50 observed without prediction
(unmodelled diffuser), RPA60 level mismatch, RPA70 diffraction path,
RPA80 measurement #609 timing shift, RPA90 multi-position
correspondence."""


# ----------------------------------------------------------------------
# Identity pins

class PredictedReflectionPath(BaseModel):
    """Solver-side reflection-path identity (#677 §2).

    ``interaction_refs`` is the *ordered* surface/edge interaction
    sequence — arrival order alone does not identify the path.
    """

    model_config = ConfigDict(frozen=True)

    solver_ref: AuthorityRef
    scene_revision_ref: AuthorityRef | None = None
    interaction_refs: tuple[AuthorityRef, ...] = Field(min_length=1)
    """Ordered surface/edge interaction refs."""
    path_class: ReflectionPathClass
    predicted_arrival_s: float = Field(ge=0.0)
    reflection_order: int = Field(ge=1)
    predicted_level_db: float | None = None
    arrival_direction: tuple[float, float, float] | None = None
    """Predicted DOA unit vector at the receiver, when available."""
    frequency_band_hz: tuple[float, float] | None = None

    @model_validator(mode='after')
    def _check(self) -> 'PredictedReflectionPath':
        _require_ref_sha(self.solver_ref, 'solver_ref')
        _require_ref_sha(
            self.scene_revision_ref, 'scene_revision_ref'
        )
        for ref in self.interaction_refs:
            _require_ref_sha(ref, 'interaction_refs')
        _require_finite(
            self.predicted_arrival_s, 'predicted_arrival_s'
        )
        if self.predicted_level_db is not None:
            _require_finite(
                self.predicted_level_db, 'predicted_level_db'
            )
        if self.arrival_direction is not None:
            for c in self.arrival_direction:
                _require_finite(c, 'arrival_direction component')
        if self.frequency_band_hz is not None:
            low, high = self.frequency_band_hz
            if low >= high:
                raise ValueError('frequency_band_hz must be low < high')
        return self


class ObservedReflectionEvent(BaseModel):
    """Measured/derived early-reflection event identity (#677 §3).

    The extraction algorithm and version are part of the identity —
    the same RIR yields different events under different extractors.
    """

    model_config = ConfigDict(frozen=True)

    measurement_ref: AuthorityRef
    extraction_algorithm: ExtractionAlgorithmKind
    extraction_version: str = Field(min_length=1)
    observed_time_s: float = Field(ge=0.0)
    observed_extent_s: tuple[float, float] | None = None
    """Optional observed interval (start, end)."""
    observed_doa: tuple[float, float, float] | None = None
    doa_uncertainty: float | None = None
    observed_level_db: float | None = None
    spectral_signature_ref: AuthorityRef | None = None
    extraction_parameters: tuple[str, ...] = ()
    """Canonical ``name=value`` parameter strings."""

    @model_validator(mode='after')
    def _check(self) -> 'ObservedReflectionEvent':
        _require_ref_sha(self.measurement_ref, 'measurement_ref')
        _require_ref_sha(
            self.spectral_signature_ref, 'spectral_signature_ref'
        )
        _require_finite(self.observed_time_s, 'observed_time_s')
        if self.observed_extent_s is not None:
            low, high = self.observed_extent_s
            for bound in (low, high):
                _require_finite(bound, 'observed_extent_s')
            if low > high:
                raise ValueError(
                    'observed_extent_s must be low <= high'
                )
        for label in ('observed_level_db', 'doa_uncertainty'):
            value = getattr(self, label)
            if value is not None:
                _require_finite(value, label)
        if self.observed_doa is not None:
            for c in self.observed_doa:
                _require_finite(c, 'observed_doa component')
        return self


class ReflectionCorrespondencePairing(BaseModel):
    """One declared predicted↔observed association (#677 §5/§6).

    Every pairing pins both identities, the algorithm that proposed it
    and the evidence dimensions actually evaluated — nearest-in-time
    alone can never silently calibrate a surface.
    """

    model_config = ConfigDict(frozen=True)

    pairing_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    predicted_path: PredictedReflectionPath | None = None
    observed_event: ObservedReflectionEvent | None = None
    correspondence_state: CorrespondenceState
    matching_algorithm: MatchingAlgorithmKind
    matching_algorithm_version: str = Field(min_length=1)
    evidence_dimensions: tuple[CorrespondenceEvidenceKind, ...] = ()
    cluster_member_ids: tuple[str, ...] = ()
    """Pairing ids of other members when this is a cluster/many-* state."""
    validation_role: Literal['calibration', 'holdout_validation'] = (
        'holdout_validation'
    )
    ambiguity_note: str | None = None
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=REFLECTION_CORRESPONDENCE_SCHEMA_VERSION,
        min_length=1,
    )
    pairing_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'predicted_path': (
                self.predicted_path.model_dump(mode='json')
                if self.predicted_path is not None
                else None
            ),
            'observed_event': (
                self.observed_event.model_dump(mode='json')
                if self.observed_event is not None
                else None
            ),
            'correspondence_state': self.correspondence_state,
            'matching_algorithm': self.matching_algorithm,
            'matching_algorithm_version': (
                self.matching_algorithm_version
            ),
            'evidence_dimensions': list(self.evidence_dimensions),
            'cluster_member_ids': list(self.cluster_member_ids),
            'validation_role': self.validation_role,
            'ambiguity_note': self.ambiguity_note,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'ReflectionCorrespondencePairing':
        _require_iso8601(
            self.declared_at_utc, 'pairing declared_at_utc'
        )
        matched_states = {
            'one_to_one',
            'many_to_one',
            'one_to_many',
            'unresolved_cluster',
            'ambiguous',
        }
        if self.correspondence_state in matched_states:
            if (
                self.predicted_path is None
                and self.observed_event is None
            ):
                raise ValueError(
                    'a matched/cluster state needs at least one '
                    'predicted path or observed event'
                )
            if self.correspondence_state == 'one_to_one' and (
                self.predicted_path is None
                or self.observed_event is None
            ):
                raise ValueError(
                    'one_to_one requires both a predicted path and an '
                    'observed event'
                )
            if not self.evidence_dimensions:
                raise ValueError(
                    'a matched state requires evidence dimensions'
                )
        if self.correspondence_state in (
            'one_to_many',
            'many_to_one',
            'unresolved_cluster',
        ) and not self.cluster_member_ids:
            raise ValueError(
                'cluster/many-* states must name cluster members'
            )
        if len(set(self.evidence_dimensions)) != len(
            self.evidence_dimensions
        ):
            raise ValueError('evidence_dimensions must be unique')
        expected = _hash(self.identity_payload())
        if self.pairing_sha256 != expected:
            raise ValueError('reflection pairing hash mismatch')
        if self.pairing_id != _semantic_id('rfxpair', expected):
            raise ValueError(
                'reflection pairing id does not match its hash'
            )
        return self


def build_reflection_pairing(
    *,
    document_id: str,
    correspondence_state: CorrespondenceState,
    matching_algorithm: MatchingAlgorithmKind,
    matching_algorithm_version: str,
    predicted_path: PredictedReflectionPath | None = None,
    observed_event: ObservedReflectionEvent | None = None,
    evidence_dimensions: Sequence[CorrespondenceEvidenceKind] = (),
    cluster_member_ids: Sequence[str] = (),
    validation_role: Literal[
        'calibration', 'holdout_validation'
    ] = 'holdout_validation',
    ambiguity_note: str | None = None,
    declared_at_utc: str | None = None,
) -> ReflectionCorrespondencePairing:
    """Seal one reflection-correspondence pairing."""
    return _seal(
        ReflectionCorrespondencePairing,
        {
            'document_id': document_id,
            'predicted_path': (
                predicted_path.model_dump(mode='json')
                if predicted_path is not None
                else None
            ),
            'observed_event': (
                observed_event.model_dump(mode='json')
                if observed_event is not None
                else None
            ),
            'correspondence_state': correspondence_state,
            'matching_algorithm': matching_algorithm,
            'matching_algorithm_version': matching_algorithm_version,
            'evidence_dimensions': list(evidence_dimensions),
            'cluster_member_ids': list(cluster_member_ids),
            'validation_role': validation_role,
            'ambiguity_note': ambiguity_note,
            'declared_at_utc': declared_at_utc or _utc_now(),
        },
        'pairing_id',
        'pairing_sha256',
        'rfxpair',
    )


def reflection_pairing_binding(
    pairing: ReflectionCorrespondencePairing,
) -> AuthorityRef:
    return AuthorityRef(
        kind='reflection_correspondence_pairing',
        ref_id=pairing.pairing_id,
        ref_sha256=pairing.pairing_sha256,
    )


# ----------------------------------------------------------------------
# Set-level record

class ReflectionCorrespondenceSet(BaseModel):
    """The sealed correspondence set for one source↔receiver pair
    (#677 §4).

    Direct-path registration is a hard prerequisite: without a pinned
    #564-class registration the set cannot support level/delay claims
    at all.
    """

    model_config = ConfigDict(frozen=True)

    set_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    registration_ref: AuthorityRef
    """Direct-path registration pin (#564 lineage) — required."""
    pairing_refs: tuple[AuthorityRef, ...] = ()
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=REFLECTION_CORRESPONDENCE_SCHEMA_VERSION,
        min_length=1,
    )
    set_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'registration_ref': self.registration_ref.model_dump(
                mode='json'
            ),
            'pairing_refs': [
                r.model_dump(mode='json') for r in self.pairing_refs
            ],
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'ReflectionCorrespondenceSet':
        _require_iso8601(self.declared_at_utc, 'set declared_at_utc')
        _require_ref_sha(self.registration_ref, 'registration_ref')
        for ref in self.pairing_refs:
            _require_ref_sha(ref, 'pairing_refs')
        expected = _hash(self.identity_payload())
        if self.set_sha256 != expected:
            raise ValueError('correspondence set hash mismatch')
        if self.set_id != _semantic_id('rfxset', expected):
            raise ValueError(
                'correspondence set id does not match its hash'
            )
        return self


def build_correspondence_set(
    *,
    document_id: str,
    registration_ref: AuthorityRef,
    pairings: Sequence[ReflectionCorrespondencePairing] = (),
    declared_at_utc: str | None = None,
) -> ReflectionCorrespondenceSet:
    """Seal one correspondence set."""
    return _seal(
        ReflectionCorrespondenceSet,
        {
            'document_id': document_id,
            'registration_ref': registration_ref.model_dump(mode='json'),
            'pairing_refs': [
                reflection_pairing_binding(p).model_dump(mode='json')
                for p in pairings
            ],
            'declared_at_utc': declared_at_utc or _utc_now(),
        },
        'set_id',
        'set_sha256',
        'rfxset',
    )


def correspondence_set_binding(
    record: ReflectionCorrespondenceSet,
) -> AuthorityRef:
    return AuthorityRef(
        kind='reflection_correspondence_set',
        ref_id=record.set_id,
        ref_sha256=record.set_sha256,
    )


# ----------------------------------------------------------------------
# Verdict

class ReflectionCorrespondenceVerdict(BaseModel):
    """Fail-closed verdict over one correspondence set (#677 §9/§11)."""

    model_config = ConfigDict(frozen=True)

    verdict_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    set_ref: AuthorityRef
    state: CorrespondenceQualificationState
    matched_pair_count: int = Field(ge=0)
    ambiguous_pair_count: int = Field(ge=0)
    unmatched_predicted_count: int = Field(ge=0)
    unmatched_observed_count: int = Field(ge=0)
    calibration_contaminated: bool
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_version: str = Field(
        default=REFLECTION_CORRESPONDENCE_EVALUATION_VERSION,
        min_length=1,
    )
    verdict_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'set_ref': self.set_ref.model_dump(mode='json'),
            'state': self.state,
            'matched_pair_count': self.matched_pair_count,
            'ambiguous_pair_count': self.ambiguous_pair_count,
            'unmatched_predicted_count': self.unmatched_predicted_count,
            'unmatched_observed_count': self.unmatched_observed_count,
            'calibration_contaminated': self.calibration_contaminated,
            'reasons': list(self.reasons),
            'limitations': list(self.limitations),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'ReflectionCorrespondenceVerdict':
        _require_iso8601(
            self.evaluated_at_utc, 'verdict evaluated_at_utc'
        )
        if self.set_ref.kind != 'reflection_correspondence_set':
            raise ValueError(
                "set_ref must pin a 'reflection_correspondence_set'"
            )
        _require_ref_sha(self.set_ref, 'set_ref')
        if self.state == 'qualified' and (
            self.calibration_contaminated
            or self.ambiguous_pair_count > 0
        ):
            raise ValueError(
                'qualified requires zero ambiguity and no '
                'calibration contamination'
            )
        expected = _hash(self.identity_payload())
        if self.verdict_sha256 != expected:
            raise ValueError('correspondence verdict hash mismatch')
        if self.verdict_id != _semantic_id('rfxverdict', expected):
            raise ValueError(
                'correspondence verdict id does not match its hash'
            )
        return self


def correspondence_verdict_binding(
    verdict: ReflectionCorrespondenceVerdict,
) -> AuthorityRef:
    return AuthorityRef(
        kind='reflection_correspondence_verdict',
        ref_id=verdict.verdict_id,
        ref_sha256=verdict.verdict_sha256,
    )


def evaluate_reflection_correspondence(
    document_id: str,
    correspondence_set: ReflectionCorrespondenceSet,
    pairings: Sequence[ReflectionCorrespondencePairing],
    *,
    registration_valid: bool,
    evaluated_at_utc: str | None = None,
) -> ReflectionCorrespondenceVerdict:
    """Fail-closed verdict over one correspondence set (#677 §9/§11).

    Rules:

    - ``registration_valid=False`` → ``registration_prerequisite_missing``
      — no level/delay correspondence is even defined.
    - Pairings whose only evidence is ``time_alignment`` (nearest-peak
      collapse) → counted ambiguous; the set can never be
      ``qualified``.
    - Any ``ambiguous``/``unresolved_cluster`` pairing →
      ``ambiguous_unresolved`` unless everything else is clean, in
      which case ``qualified_with_limitations``.
    - All pairings consumed by calibration →
      ``calibration_only_no_independent_validation`` (#677 §11).
    """

    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')

    reasons: list[str] = []
    limitations: list[str] = []

    if not registration_valid:
        return _seal(
            ReflectionCorrespondenceVerdict,
            {
                'document_id': document_id,
                'set_ref': correspondence_set_binding(
                    correspondence_set
                ).model_dump(mode='json'),
                'state': 'registration_prerequisite_missing',
                'matched_pair_count': 0,
                'ambiguous_pair_count': len(pairings),
                'unmatched_predicted_count': 0,
                'unmatched_observed_count': 0,
                'calibration_contaminated': False,
                'reasons': (
                    'direct-path registration is a prerequisite for '
                    'any reflection correspondence — it is missing or '
                    'invalid',
                ),
                'limitations': (),
                'evaluated_at_utc': evaluated_at_utc,
            },
            'verdict_id',
            'verdict_sha256',
            'rfxverdict',
        )

    matched = 0
    ambiguous = 0
    unmatched_pred = 0
    unmatched_obs = 0
    time_only = 0
    calib = 0

    for pairing in pairings:
        st = pairing.correspondence_state
        if st in ('one_to_one', 'many_to_one', 'one_to_many'):
            matched += 1
        elif st in ('ambiguous', 'unresolved_cluster'):
            ambiguous += 1
        elif st == 'unmatched_predicted':
            unmatched_pred += 1
        elif st == 'unmatched_observed':
            unmatched_obs += 1
        elif st == 'outside_observation_capability':
            unmatched_pred += 1
            limitations.append(
                f'{pairing.pairing_id}: predicted path is outside the '
                'declared observation capability — retained, not '
                'treated as an error'
            )
        if st in ('one_to_one', 'many_to_one', 'one_to_many',
                  'ambiguous', 'unresolved_cluster') and (
            pairing.evidence_dimensions == ('time_alignment',)
        ):
            time_only += 1
        if pairing.validation_role == 'calibration':
            calib += 1

    calibration_contaminated = calib > 0 and matched + ambiguous == calib

    if not pairings:
        state: CorrespondenceQualificationState = (
            'insufficient_evidence'
        )
        reasons.append('the set declares no pairings')
    elif calibration_contaminated:
        state = 'calibration_only_no_independent_validation'
        reasons.append(
            'every matched pair was consumed by calibration — the '
            'correspondence can never independently validate the '
            'calibrated surfaces'
        )
    elif ambiguous or time_only:
        state = 'ambiguous_unresolved'
        if time_only:
            reasons.append(
                f'{time_only} pairing(s) rest on time alignment alone '
                '— nearest-peak correspondence cannot calibrate '
                'surfaces'
            )
        if ambiguous:
            reasons.append(
                f'{ambiguous} pairing(s) remain ambiguous or clustered'
            )
    else:
        state = 'qualified'
        if unmatched_pred or unmatched_obs:
            limitations.append(
                f'{unmatched_pred} predicted and {unmatched_obs} '
                'observed items remain unmatched — recorded, never '
                'hidden'
            )
            state = 'qualified_with_limitations'

    return _seal(
        ReflectionCorrespondenceVerdict,
        {
            'document_id': document_id,
            'set_ref': correspondence_set_binding(
                correspondence_set
            ).model_dump(mode='json'),
            'state': state,
            'matched_pair_count': matched,
            'ambiguous_pair_count': ambiguous,
            'unmatched_predicted_count': unmatched_pred,
            'unmatched_observed_count': unmatched_obs,
            'calibration_contaminated': calibration_contaminated,
            'reasons': tuple(reasons),
            'limitations': tuple(dict.fromkeys(limitations)),
            'evaluated_at_utc': evaluated_at_utc,
        },
        'verdict_id',
        'verdict_sha256',
        'rfxverdict',
    )
