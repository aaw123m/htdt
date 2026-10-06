"""Multi-source correlation / coherence authority (#690).

Coherent loudspeakers must not be summed like independent noise sources.
This authority seals a group's per-member delivered-signal pins (routing,
gain, delay, polarity, filters, stimulus), the declared correlation
relation between members (and its band + evidence basis), and the
combination mode a prediction is allowed to use. An ``incoherent``
energy-sum claim over deterministic identical signals is
``incompatible_combination``; an undeclared correlation can only produce
bounded scenarios or ``insufficient_evidence``.

Scope discipline:

* The authority declares *the correlation model and its evidence*; it
  does not estimate coherence. ``CsdEvidence`` records the provenance of
  a cross-spectral estimate (grid, Hermitian flag, PSD references) —
  partial coherence without it is rejected.
* Members pin their excitation/source profile refs; the same stimulus
  routed to N members is ``deterministic_identical`` and must be summed
  coherently, never energy-summed.
* Time-varying / programme-dependent correlation needs a statistical or
  bounded-scenario mode; a single definitive mode is refused.

Literature basis: coherent vs power summation of correlated sources
(standard room-acoustics result — correlated sources can produce +6 dB
where incoherent produce +3 dB); partial coherence handled through
cross-spectral density matrices (Bendat & Piersol); subwoofer group /
LCR phase-alignment practice where delay and polarity determine whether
the sum is constructive.
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


SOURCE_COHERENCE_SCHEMA_VERSION = 'source-coherence-1'
SOURCE_COHERENCE_EVALUATION_VERSION = 'source-coherence-eval-1'

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

CorrelationKind = Literal[
    'deterministic_identical',
    'deterministic_gain_delay_phase_related',
    'deterministic_filtered_related',
    'fully_coherent_measured',
    'partially_coherent',
    'uncorrelated_independent',
    'content_dependent',
    'time_varying',
    'unknown',
]
"""Correlation relation between group members. ``deterministic_*`` needs
per-member signal pins; ``partially_coherent`` needs CSD evidence;
``uncorrelated_independent`` needs an explicit basis."""

CorrelationBasis = Literal[
    'measured_cross_spectrum',
    'measured_coherence_function',
    'analytic_signal_path',
    'manufacturer_declared',
    'assumed_from_routing',
    'bounded_scenarios_declared',
    'unknown',
]

CombinationMode = Literal[
    'complex_deterministic',
    'time_domain_deterministic',
    'incoherent_expected_energy',
    'partial_coherence_csd',
    'bounded_scenarios',
    'monte_carlo_statistical',
]
"""The combination law a prediction uses. Energy sums are legal only
with evidence of incoherence or a declared scenario bound."""

CombinationVerdict = Literal[
    'combination_qualified',
    'qualified_with_limitations',
    'qualified_with_scenario_bounds',
    'incompatible_combination',
    'insufficient_evidence',
]

BassManagementRole = Literal[
    'native_lfe',
    'redirected_from_left',
    'redirected_from_right',
    'redirected_from_center',
    'redirected_from_surround',
    'redirected_from_other',
    'sub_output_sum',
    'none',
    'unknown',
]


# ---------------------------------------------------------------------------
# Sub-specs
# ---------------------------------------------------------------------------


class SourceSignalPin(BaseModel):
    """The delivered signal a group member carries.

    Pins what is actually driven into the member: routing, gain, delay,
    polarity, filter refs and the stimulus ref — the identity of the
    excitation, not just the speaker.
    """

    model_config = ConfigDict(frozen=True)

    member_id: str = Field(min_length=1)
    source_ref: AuthorityRef
    channel_label: str = ''
    stimulus_ref: AuthorityRef | None = None
    gain_db: float = 0.0
    delay_ms: float = Field(default=0.0, ge=0.0)
    polarity: Literal['normal', 'inverted'] = 'normal'
    filter_refs: tuple[AuthorityRef, ...] = ()
    bass_management_role: BassManagementRole = 'none'

    @model_validator(mode='after')
    def _check(self) -> 'SourceSignalPin':
        if self.source_ref.ref_sha256 is None:
            raise ValueError('member source_ref must pin its sha256')
        if self.stimulus_ref is not None and (
            self.stimulus_ref.ref_sha256 is None
        ):
            raise ValueError('stimulus_ref must pin its sha256')
        for ref in self.filter_refs:
            if ref.ref_sha256 is None:
                raise ValueError('filter_refs must pin their sha256')
        if not isfinite(float(self.gain_db)):
            raise ValueError('gain_db must be finite')
        if not isfinite(float(self.delay_ms)):
            raise ValueError('delay_ms must be finite')
        return self


class CsdEvidence(BaseModel):
    """Cross-spectral-density evidence for partial coherence."""

    model_config = ConfigDict(frozen=True)

    frequency_grid_hz: tuple[float, ...] = Field(min_length=1)
    hermitian_declared: bool = False
    member_psd_refs: tuple[AuthorityRef, ...] = ()
    estimation_method: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'CsdEvidence':
        for f in self.frequency_grid_hz:
            if not isfinite(float(f)) or float(f) <= 0.0:
                raise ValueError(
                    'frequency_grid_hz entries must be finite and > 0'
                )
        if sorted(self.frequency_grid_hz) != list(
            self.frequency_grid_hz
        ):
            raise ValueError('frequency_grid_hz must be sorted')
        if not self.hermitian_declared:
            raise ValueError(
                'partial-coherence CSD evidence must declare the '
                'Hermitian property'
            )
        for ref in self.member_psd_refs:
            if ref.ref_sha256 is None:
                raise ValueError(
                    'member PSD refs must pin their sha256'
                )
        return self


class CorrelationRelation(BaseModel):
    """Declared correlation relation between two members (or the
    group-level default when ``member_b_id`` is ``'*'``)."""

    model_config = ConfigDict(frozen=True)

    member_a_id: str = Field(min_length=1)
    member_b_id: str = Field(min_length=1)
    relation: CorrelationKind
    valid_band_hz: FrequencyBand | None = None
    basis: CorrelationBasis = 'unknown'
    csd: CsdEvidence | None = None
    frequency_varying: bool = False
    time_varying: bool = False
    programme_dependent: bool = False
    notes: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'CorrelationRelation':
        if (
            self.relation == 'partially_coherent'
            and self.csd is None
            and self.basis
            not in {'measured_coherence_function', 'unknown'}
        ):
            raise ValueError(
                'partially_coherent requires CSD evidence or a '
                'measured coherence basis'
            )
        if self.relation == 'unknown' and self.csd is not None:
            raise ValueError('unknown relation cannot assert a CSD')
        if self.basis == 'unknown' and self.relation != 'unknown':
            raise ValueError(
                'a declared correlation relation requires an explicit '
                'basis'
            )
        return self


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


class SourceCoherenceProfile(BaseModel):
    """Sealed declaration of a group's signal pins and correlation."""

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    group_label: str = ''
    members: tuple[SourceSignalPin, ...] = Field(min_length=2)
    relations: tuple[CorrelationRelation, ...] = ()
    default_relation: CorrelationKind = 'unknown'
    declared_combination_mode: CombinationMode | None = None
    authority_version: str = Field(
        default=SOURCE_COHERENCE_SCHEMA_VERSION, min_length=1
    )
    declared_at_utc: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'group_label': self.group_label,
            'members': [m.model_dump(mode='json') for m in self.members],
            'relations': [
                r.model_dump(mode='json') for r in self.relations
            ],
            'default_relation': self.default_relation,
            'declared_combination_mode': self.declared_combination_mode,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'SourceCoherenceProfile':
        _require_iso8601(self.declared_at_utc, 'profile declared_at_utc')
        member_ids = [m.member_id for m in self.members]
        if len(set(member_ids)) != len(member_ids):
            raise ValueError('duplicate member ids')
        member_set = set(member_ids)
        for relation in self.relations:
            if relation.member_a_id not in member_set:
                raise ValueError(
                    f'relation references unknown member '
                    f'{relation.member_a_id}'
                )
            if (
                relation.member_b_id != '*'
                and relation.member_b_id not in member_set
            ):
                raise ValueError(
                    f'relation references unknown member '
                    f'{relation.member_b_id}'
                )
        if self.default_relation != 'unknown' and not self.relations:
            raise ValueError(
                'a non-unknown default_relation requires at least one '
                'declared CorrelationRelation'
            )
        deterministic = {
            'deterministic_identical',
            'deterministic_gain_delay_phase_related',
            'deterministic_filtered_related',
        }
        if self.default_relation in deterministic:
            for member in self.members:
                if member.stimulus_ref is None:
                    raise ValueError(
                        'deterministic relations require every member '
                        'to pin its stimulus_ref'
                    )
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('source coherence profile hash mismatch')
        if self.profile_id != _semantic_id('mscprof', expected):
            raise ValueError(
                'source coherence profile id does not match its hash'
            )
        return self


class SourceCombinationQualification(BaseModel):
    """Sealed verdict of :func:`evaluate_source_combination`."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    verdict: CombinationVerdict
    requested_mode: CombinationMode
    effective_mode: CombinationMode | None = None
    governing_relations: tuple[CorrelationKind, ...] = ()
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_version: str = Field(
        default=SOURCE_COHERENCE_EVALUATION_VERSION, min_length=1
    )
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'verdict': self.verdict,
            'requested_mode': self.requested_mode,
            'effective_mode': self.effective_mode,
            'governing_relations': list(self.governing_relations),
            'reasons': list(self.reasons),
            'limitations': list(self.limitations),
            'evaluated_at_utc': self.evaluated_at_utc,
            'evaluation_version': self.evaluation_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'SourceCombinationQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        if self.profile_ref.ref_sha256 is None:
            raise ValueError('profile_ref must pin its sha256')
        if self.profile_ref.kind != 'source_coherence_profile':
            raise ValueError(
                "profile_ref must pin a 'source_coherence_profile'"
            )
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError(
                'source combination qualification hash mismatch'
            )
        if self.qualification_id != _semantic_id('mscqual', expected):
            raise ValueError(
                'source combination qualification id does not match '
                'its hash'
            )
        return self


def source_coherence_binding(
    profile: SourceCoherenceProfile,
) -> AuthorityRef:
    return AuthorityRef(
        kind='source_coherence_profile',
        ref_id=profile.profile_id,
        ref_sha256=profile.profile_sha256,
    )


def source_combination_qualification_binding(
    qualification: SourceCombinationQualification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='source_combination_qualification',
        ref_id=qualification.qualification_id,
        ref_sha256=qualification.qualification_sha256,
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

_DETERMINISTIC = {
    'deterministic_identical',
    'deterministic_gain_delay_phase_related',
    'deterministic_filtered_related',
    'fully_coherent_measured',
}
_COMPLEX_MODES = {'complex_deterministic', 'time_domain_deterministic'}
_STATISTICAL_MODES = {'bounded_scenarios', 'monte_carlo_statistical'}


def _pairwise_relations(
    profile: SourceCoherenceProfile,
) -> set[CorrelationKind]:
    return {r.relation for r in profile.relations}


def evaluate_source_combination(
    document_id: str,
    profile: SourceCoherenceProfile,
    *,
    requested_mode: CombinationMode,
    evaluated_at_utc: str | None = None,
) -> SourceCombinationQualification:
    """Evaluate whether the requested combination mode is lawful.

    Fail-closed rules:

    * deterministic/fully-coherent members + energy sum →
      ``incompatible_combination``
    * ``uncorrelated_independent`` + ``incoherent_expected_energy`` →
      ``combination_qualified``
    * ``partially_coherent`` requires ``partial_coherence_csd`` (with
      declared CSD) or a statistical mode; collapsing it to either
      limit is ``incompatible_combination``
    * ``content_dependent``/``time_varying`` requires statistical modes
    * ``unknown`` admits only bounded scenarios or reports
      ``insufficient_evidence``
    """

    evaluated_at_utc = evaluated_at_utc or _utc_now()
    reasons: list[str] = []
    limitations: list[str] = []
    kinds = _pairwise_relations(profile)
    if not kinds:
        kinds = {profile.default_relation}
    governing = sorted(kinds - {'unknown'}) or ['unknown']

    verdict: CombinationVerdict
    effective_mode: CombinationMode | None = requested_mode

    if kinds & {
        'deterministic_identical',
        'deterministic_gain_delay_phase_related',
        'deterministic_filtered_related',
        'fully_coherent_measured',
    }:
        if requested_mode == 'incoherent_expected_energy':
            verdict = 'incompatible_combination'
            reasons.append(
                'energy-sum over deterministic/coherent members is '
                'physically wrong (they can sum to +6 dB, not +3 dB)'
            )
            effective_mode = None
        elif requested_mode in _COMPLEX_MODES:
            verdict = 'combination_qualified'
        elif requested_mode in _STATISTICAL_MODES:
            verdict = 'qualified_with_limitations'
            limitations.append(
                'coherent members evaluated under a statistical mode; '
                'the deterministic sum is stronger evidence'
            )
        else:  # partial_coherence_csd
            verdict = 'qualified_with_limitations'
            limitations.append(
                'coherent members treated under a partial-coherence '
                'mode; CSD must absorb the deterministic part'
            )
    elif 'partially_coherent' in kinds:
        if requested_mode == 'partial_coherence_csd':
            if any(
                r.relation == 'partially_coherent' and r.csd is not None
                for r in profile.relations
            ):
                verdict = 'combination_qualified'
            else:
                verdict = 'insufficient_evidence'
                reasons.append(
                    'partial coherence declared without CSD evidence'
                )
                effective_mode = None
        elif requested_mode in _STATISTICAL_MODES:
            verdict = 'qualified_with_scenario_bounds'
        else:
            verdict = 'incompatible_combination'
            reasons.append(
                'partial coherence cannot be collapsed to coherent or '
                'incoherent limits'
            )
            effective_mode = None
    elif 'uncorrelated_independent' in kinds:
        if requested_mode == 'incoherent_expected_energy':
            basis = {
                r.basis for r in profile.relations
                if r.relation == 'uncorrelated_independent'
            }
            if basis - {'assumed_from_routing', 'unknown'}:
                verdict = 'combination_qualified'
            else:
                verdict = 'qualified_with_limitations'
                limitations.append(
                    'independence is assumed from routing, not '
                    'measured'
                )
        elif requested_mode in _STATISTICAL_MODES:
            verdict = 'qualified_with_scenario_bounds'
        elif requested_mode in _COMPLEX_MODES:
            verdict = 'incompatible_combination'
            reasons.append(
                'complex deterministic sum needs phase-related '
                'evidence the profile does not declare'
            )
            effective_mode = None
        else:
            verdict = 'qualified_with_limitations'
            limitations.append(
                'partial-coherence mode over independent members is '
                'permitted but unused'
            )
    elif kinds & {'content_dependent', 'time_varying'}:
        if requested_mode in _STATISTICAL_MODES:
            verdict = 'qualified_with_scenario_bounds'
        else:
            verdict = 'incompatible_combination'
            reasons.append(
                'content/time-varying correlation requires a '
                'statistical or bounded-scenario mode'
            )
            effective_mode = None
    else:  # unknown / empty
        if requested_mode in _STATISTICAL_MODES:
            if any(
                r.basis == 'bounded_scenarios_declared'
                for r in profile.relations
            ):
                verdict = 'qualified_with_scenario_bounds'
            else:
                verdict = 'insufficient_evidence'
                reasons.append(
                    'correlation undeclared and no bounded scenarios '
                    'declared'
                )
                effective_mode = None
        else:
            verdict = 'insufficient_evidence'
            reasons.append(
                'correlation between members was never declared; '
                'definitive combination modes are not permitted'
            )
            effective_mode = None

    return _seal(
        SourceCombinationQualification,
        {
            'document_id': document_id,
            'profile_ref': source_coherence_binding(profile),
            'verdict': verdict,
            'requested_mode': requested_mode,
            'effective_mode': effective_mode,
            'governing_relations': governing,
            'reasons': sorted(set(reasons)),
            'limitations': sorted(set(limitations)),
            'evaluated_at_utc': evaluated_at_utc,
            'evaluation_version': SOURCE_COHERENCE_EVALUATION_VERSION,
        },
        'qualification_id',
        'qualification_sha256',
        'mscqual',
    )


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_source_coherence_profile(
    document_id: str,
    members: Sequence[SourceSignalPin],
    *,
    group_label: str = '',
    relations: Sequence[CorrelationRelation] = (),
    default_relation: CorrelationKind = 'unknown',
    declared_combination_mode: CombinationMode | None = None,
    declared_at_utc: str | None = None,
) -> SourceCoherenceProfile:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        SourceCoherenceProfile,
        {
            'document_id': document_id,
            'group_label': group_label,
            'members': [
                m.model_dump(mode='json') for m in members
            ],
            'relations': [
                r.model_dump(mode='json') for r in relations
            ],
            'default_relation': default_relation,
            'declared_combination_mode': declared_combination_mode,
            'authority_version': SOURCE_COHERENCE_SCHEMA_VERSION,
            'declared_at_utc': declared_at_utc,
        },
        'profile_id',
        'profile_sha256',
        'mscprof',
    )
