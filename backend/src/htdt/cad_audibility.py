"""Perceptual relevance / audibility authority (#720, REV59-UNITS).

Equal numeric residuals are not equally audible. A symmetric
squared-dB-error objective treats a +3 dB peak and a −3 dB dip as the
same cost, a deep spatial null as a fixable error, and a long modal
decay as negligible — the literature says otherwise under bounded
conditions. This module adds the explicit, source-and-version-pinned
perceptual-relevance layer that lets HTDT prioritize what to fix
*without* inventing a universal sound-quality score.

- :class:`CadPerceptualModelProfile` — the sealed model identity: exact
  literature reference + revision, declared thresholds per observable
  kind, applicable stimulus/field/listener/level conditions, and the
  scope class (``applicable_as_reference`` … ``research_only``). No
  model is called a bare "psychoacoustic weighting".
- :func:`evaluate_audibility` + :class:`CadAudibilityAssessment` — the
  sealed verdict on one measured/predicted difference: physically real
  but below the profile's declared threshold →
  ``not_distinguishable_under_profile``; above →
  ``potentially_audible``; wrong observable/domain →
  ``outside_model_scope`` or ``indeterminate``. The raw physical delta
  always rides on the verdict — a small perceptual score never erases
  a solver error.

Authority boundary:

- this layer *interprets* differences; it never ranks candidates on its
  own and never overrides physical/hard gates — a null that cannot be
  boosted stays a physical infeasibility regardless of audibility
  (#720 §6, compose #568/#569/#577);
- peak-vs-dip asymmetry applies only through a profile that declares
  it with an exact study reference (#720 §5);
- ISO 226 / ISO 532 are pinned by exact edition with their declared
  scope — ISO 226:2023 pure-tone/free-field/frontal/18–25
  normal-hearing scope is enforced as scope class, never stretched
  into a room-EQ target (#720 §3/§4);
- model predictions never replace listening tests (#538) — verdicts
  are model-derived evidence, not subjective truth (#720 §14).

Literature basis
----------------
- ISO 226:2023 — equal-loudness contours; pure continuous tones, free
  progressive plane wave, frontal source, binaural, otologically
  normal listeners 18–25, 20 Hz–12.5 kHz preferred frequencies.
- ISO 532-1:2017 (Zwicker) / ISO 532-2:2017 (Moore–Glasberg) —
  loudness estimation under specific conditions; not a timbre,
  localization or preference metric.
- Bücklein, JAES 29(3) 1981 — spectral peaks more audible than
  equivalent dips in the tested conditions; the asymmetry is a
  research profile, never a universal numeric weighting.
- Mäkivirta et al., JAES 51(5) 2003 — low-frequency modal decay is a
  distinct perceptual problem alongside magnitude error.
- Elliott, Holland & Newell, AES 57th Conf. 2015 — comb-filter ripple
  audibility is geometry/context specific; visually large FR ripple is
  not a sufficient audibility proxy.
- Bistafa & Bradley, JASA 108(4) 2000 — JND-style thresholds used in
  this repo's residual authorities for predicted-vs-measured
  tolerances (#564); reused here as declared thresholds only.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


AUDIBILITY_SCHEMA_VERSION = 'audibility-1'
AUDIBILITY_EVALUATION_VERSION = 'aud-eval-1'

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


def _require_finite(value: float, label: str) -> None:
    if not isfinite(float(value)):
        raise ValueError(f'{label} must be finite')


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
# Taxonomies (#720)
# ---------------------------------------------------------------------------

PerceptualModelKind = Literal[
    'jnd_threshold_profile',
    'iso226_reference',
    'iso532_loudness',
    'spectral_irregularity_research',
    'modal_decay_relevance',
    'comb_filter_research',
    'listening_test_derived',
    'project_practical_threshold',
    'custom_validated',
    'other_declared',
]
"""The declared model family (#720 §2). Every profile names an exact
literature/standard identity — there is no generic 'psychoacoustic
weighting' kind."""

ModelScopeClass = Literal[
    'applicable_as_reference',
    'applicable_with_limitations',
    'outside_scope',
    'research_only',
]
"""#720 §3 — how far the profile's applicability may be stretched for the
declared use. ``outside_scope``/``research_only`` profiles never produce
an audibility verdict."""

AudibilityObservable = Literal[
    'level_difference_db',
    'band_level_difference_db',
    'frequency_shift_relative',
    'time_shift_s',
    'spectral_irregularity_db',
    'modal_decay_time',
    'modal_frequency_hz',
    'group_delay_s',
    'early_reflection_delay_s',
    'early_reflection_level_db',
    'loudness_difference',
    'other_declared',
]
"""What the physical difference is. Each threshold binds an observable —
a level JND never judges a timing error."""

DeltaDirection = Literal[
    'peak',
    'dip',
    'symmetric',
    'increase',
    'decrease',
    'undeclared',
]
"""Sign/shape of the difference where it matters. The Bücklein-style
peak/dip asymmetry applies only when the profile declares a matching
directional threshold — a dip never borrows a peak's threshold."""

ListeningFieldClass = Literal[
    'free_field_frontal',
    'reverberant_room',
    'multichannel_reproduced',
    'headphone',
    'other_declared',
    'undeclared',
]

StimulusClass = Literal[
    'pure_tone',
    'narrowband',
    'broadband_program',
    'transient',
    'multichannel_cinema',
    'other_declared',
    'undeclared',
]

ListenerClass = Literal[
    'normal_hearing_18_25',
    'normal_hearing_general',
    'trained_listener',
    'untrained_listener',
    'other_declared',
    'undeclared',
]
"""Population assumption (#720 §11) — ISO 226's 18–25 normal-hearing
cohort is a *declared* population, never a universal user model."""

AudibilityVerdict = Literal[
    'not_distinguishable_under_profile',
    'potentially_audible',
    'indeterminate',
    'outside_model_scope',
    'insufficient_evidence',
]
"""#720 — the verdict names what the profile may say. ``not_distinguishable``
is scoped to the declared conditions; it is never a universal
'inaudible' claim and never licenses removing the physical evidence."""

AUDIBILITY_VERDICT_LABELS: dict[str, str] = {
    'not_distinguishable_under_profile': '宣言条件下で知覚的区別不可',
    'potentially_audible': '可聴の可能性あり',
    'indeterminate': '判定不能（閾値未整備）',
    'outside_model_scope': 'モデル適用範囲外',
    'insufficient_evidence': '証拠不足',
}

MODEL_KIND_LABELS: dict[str, str] = {
    'jnd_threshold_profile': 'JND閾値プロファイル',
    'iso226_reference': 'ISO 226 参照等 Loudness 曲線',
    'iso532_loudness': 'ISO 532 ラウドネスモデル',
    'spectral_irregularity_research': 'スペクトル不整研究プロファイル',
    'modal_decay_relevance': 'モーダル減衰知覚プロファイル',
    'comb_filter_research': 'コムフィルタ研究プロファイル',
    'listening_test_derived': '聴取試験由来プロファイル',
    'project_practical_threshold': 'プロジェクト実務閾値',
    'custom_validated': 'カスタム検証済みモデル',
    'other_declared': 'その他(宣言)',
}


# ---------------------------------------------------------------------------
# Embedded descriptors
# ---------------------------------------------------------------------------


class CadAudibilityThreshold(BaseModel):
    """One declared JND/relevance threshold inside a model profile.

    ``threshold_value`` in ``threshold_unit`` for ``observable`` in
    direction ``direction`` under ``condition_label`` — every dimension
    is pinned so a threshold never drifts into a different phenomenon
    (#720 §5: coefficients require explicit study source, stored on the
    profile's literature_ref).
    """

    model_config = ConfigDict(frozen=True)

    observable: AudibilityObservable
    direction: DeltaDirection = 'symmetric'
    threshold_value: float
    threshold_unit: str = Field(min_length=1)
    #: Free-text scope of the threshold — level, band, content class.
    condition_label: str = ''
    #: Literature anchor for this exact number (may refine the profile
    #: citation — e.g. which table/figure of the cited study).
    source_anchor: str = ''

    @model_validator(mode='after')
    def valid_threshold(self) -> 'CadAudibilityThreshold':
        _require_finite(self.threshold_value, 'threshold_value')
        if self.threshold_value < 0:
            raise ValueError('threshold_value must be non-negative')
        if self.direction in ('peak', 'dip') and (
            self.observable != 'spectral_irregularity_db'
        ):
            raise ValueError(
                'peak/dip directional thresholds apply to spectral '
                'irregularities only — other observables are symmetric '
                'or signed'
            )
        return self


class CadAudibilityDelta(BaseModel):
    """The physical difference under interpretation (#720 §1).

    Always carries the raw magnitude+unit so the perceptual verdict
    never replaces the physical record. ``direction`` distinguishes the
    peak-vs-dip asymmetry when the profile declares one.
    """

    model_config = ConfigDict(frozen=True)

    observable: AudibilityObservable
    magnitude: float
    magnitude_unit: str = Field(min_length=1)
    direction: DeltaDirection = 'undeclared'
    #: Signed value where meaningful (positive = level increase).
    signed_value: float | None = None
    context_label: str = ''

    @model_validator(mode='after')
    def valid_delta(self) -> 'CadAudibilityDelta':
        _require_finite(self.magnitude, 'delta magnitude')
        if self.magnitude < 0:
            raise ValueError('delta magnitude must be non-negative')
        if self.signed_value is not None:
            _require_finite(self.signed_value, 'delta signed_value')
        return self


class CadListeningConditions(BaseModel):
    """The presentation conditions the difference is interpreted under
    (#720 §9/§10/§11). Every field is a declared condition — undeclared
    is a state, never a default."""

    model_config = ConfigDict(frozen=True)

    stimulus: StimulusClass = 'undeclared'
    field: ListeningFieldClass = 'undeclared'
    listener: ListenerClass = 'undeclared'
    #: Declared listening level pin (e.g. ref to a #618 calibration);
    #: audibility is level-dependent — a verdict without its level
    #: context is weaker evidence.
    level_reference_ref: AuthorityRef | None = None
    content_label: str = ''
    content_asset_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def valid_conditions(self) -> 'CadListeningConditions':
        for label, ref in (
            ('level_reference_ref', self.level_reference_ref),
            ('content_asset_ref', self.content_asset_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must carry its sha256 pin')
        return self


class CadModelApplicability(BaseModel):
    """The declared applicability envelope of a model profile
    (#720 §2/§3). A use outside the envelope is
    ``outside_scope``/``indeterminate``, never silently evaluated."""

    model_config = ConfigDict(frozen=True)

    scope_class: ModelScopeClass
    stimulus: StimulusClass = 'undeclared'
    field: ListeningFieldClass = 'undeclared'
    listener: ListenerClass = 'undeclared'
    level_dependence: str = ''
    frequency_domain_label: str = ''
    limitations: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_applicability(self) -> 'CadModelApplicability':
        if self.scope_class == 'applicable_as_reference' and not (
            self.frequency_domain_label or self.stimulus != 'undeclared'
        ):
            raise ValueError(
                'applicable_as_reference requires the scope it '
                'references — name the stimulus or frequency domain '
                'it applies to'
            )
        return self


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


class CadPerceptualModelProfile(BaseModel):
    """A sealed psychoacoustic model / threshold profile (#720 §2).

    The profile pins its exact literature identity (citation + revision
    + where in the source each number comes from), its threshold table,
    and its applicability envelope. ``project_practical_threshold`` is a
    first-class kind — a project may declare its own relevance floor,
    but it is labeled as a project choice, not psychoacoustic truth.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    model_kind: PerceptualModelKind
    profile_label: str = Field(min_length=1)
    literature_ref: str = Field(min_length=1)
    literature_version: str = ''
    thresholds: tuple[CadAudibilityThreshold, ...] = ()
    applicability: CadModelApplicability
    #: Whether the model requires a level-calibrated presentation —
    #: ISO-532-class loudness needs exact level state (#720 §9).
    requires_level_calibration: bool = False
    output_semantics: str = ''
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_profile(self) -> 'CadPerceptualModelProfile':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        if not self.thresholds and self.model_kind not in (
            'other_declared',
        ):
            raise ValueError(
                'a perceptual profile declares its thresholds — a '
                'threshold-less model cannot bound relevance'
            )
        if self.model_kind == 'iso226_reference' and (
            self.applicability.scope_class == 'applicable_as_reference'
        ):
            scope = self.applicability
            if (
                scope.stimulus != 'pure_tone'
                or scope.field != 'free_field_frontal'
                or scope.listener != 'normal_hearing_18_25'
            ):
                raise ValueError(
                    'ISO 226:2023 scope is pure tone / frontal '
                    'free-field / binaural / normal-hearing 18–25 — '
                    'anything else is applicable_with_limitations at '
                    'best (#720 §3)'
                )
        if self.model_kind == 'iso532_loudness' and not (
            self.requires_level_calibration
        ):
            raise ValueError(
                'an ISO 532 loudness profile requires level calibration '
                '— loudness estimation without level state is not '
                'reproducible (#720 §4/§9)'
            )
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('perceptual profile hash mismatch')
        if self.profile_id != _semantic_id('pprof', expected):
            raise ValueError(
                'perceptual profile id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'model_kind': self.model_kind,
            'profile_label': self.profile_label,
            'literature_ref': self.literature_ref,
            'literature_version': self.literature_version,
            'thresholds': [
                t.model_dump(mode='json') for t in self.thresholds
            ],
            'applicability': self.applicability.model_dump(mode='json'),
            'requires_level_calibration': self.requires_level_calibration,
            'output_semantics': self.output_semantics,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def perceptual_profile_binding(
    profile: CadPerceptualModelProfile,
) -> AuthorityRef:
    return AuthorityRef(
        kind='perceptual_model_profile',
        ref_id=profile.profile_id,
        ref_sha256=profile.profile_sha256,
    )


class CadAudibilityAssessment(BaseModel):
    """Sealed verdict of :func:`evaluate_audibility` (#720).

    The verdict always re-states the physical delta and the conditions it
    was judged under — a ``not_distinguishable`` verdict is evidence for
    prioritization, never a deletion of the physical difference.
    """

    model_config = ConfigDict(frozen=True)

    assessment_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    difference_ref: AuthorityRef
    delta: CadAudibilityDelta
    conditions: CadListeningConditions
    verdict: AudibilityVerdict
    matched_threshold: CadAudibilityThreshold | None = None
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_version: str = Field(min_length=1)
    assessment_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_assessment(self) -> 'CadAudibilityAssessment':
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        if self.profile_ref.ref_sha256 is None:
            raise ValueError('profile_ref must pin its sha256')
        if self.profile_ref.kind != 'perceptual_model_profile':
            raise ValueError(
                "profile_ref must pin a 'perceptual_model_profile'"
            )
        if self.difference_ref.ref_sha256 is None:
            raise ValueError(
                'difference_ref must pin its sha256 — the physical '
                'record being interpreted stays pinned'
            )
        if self.verdict in (
            'not_distinguishable_under_profile', 'potentially_audible'
        ) and self.matched_threshold is None:
            raise ValueError(
                'a threshold verdict names the threshold it matched — '
                'otherwise the verdict has no declared basis'
            )
        expected = _hash(self.identity_payload())
        if self.assessment_sha256 != expected:
            raise ValueError('audibility assessment hash mismatch')
        if self.assessment_id != _semantic_id('aud', expected):
            raise ValueError(
                'audibility assessment id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'difference_ref': self.difference_ref.model_dump(mode='json'),
            'delta': self.delta.model_dump(mode='json'),
            'conditions': self.conditions.model_dump(mode='json'),
            'verdict': self.verdict,
            'matched_threshold': (
                self.matched_threshold.model_dump(mode='json')
                if self.matched_threshold is not None
                else None
            ),
            'reasons': list(self.reasons),
            'limitations': list(self.limitations),
            'evaluated_at_utc': self.evaluated_at_utc,
            'evaluation_version': self.evaluation_version,
        }


def audibility_assessment_binding(
    assessment: CadAudibilityAssessment,
) -> AuthorityRef:
    return AuthorityRef(
        kind='audibility_assessment',
        ref_id=assessment.assessment_id,
        ref_sha256=assessment.assessment_sha256,
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _direction_matches(
    threshold_dir: DeltaDirection, delta_dir: DeltaDirection,
) -> bool:
    """A directional threshold judges only its declared direction."""
    if threshold_dir in ('symmetric',):
        return True
    if delta_dir == 'undeclared':
        return threshold_dir == 'symmetric'
    if threshold_dir in ('increase', 'decrease'):
        return threshold_dir == delta_dir
    return threshold_dir == delta_dir


def _condition_mismatches(
    applicability: CadModelApplicability,
    conditions: CadListeningConditions,
) -> list[str]:
    mismatches: list[str] = []
    if applicability.stimulus != 'undeclared' and (
        conditions.stimulus != applicability.stimulus
    ):
        mismatches.append(
            f'stimulus {conditions.stimulus} is outside the declared '
            f'{applicability.stimulus} envelope'
        )
    if applicability.field != 'undeclared' and (
        conditions.field != applicability.field
    ):
        mismatches.append(
            f'field {conditions.field} is outside the declared '
            f'{applicability.field} envelope'
        )
    if applicability.listener != 'undeclared' and (
        conditions.listener != applicability.listener
    ):
        mismatches.append(
            f'listener {conditions.listener} is outside the declared '
            f'{applicability.listener} envelope'
        )
    return mismatches


def evaluate_audibility(
    *,
    document_id: str,
    profile: CadPerceptualModelProfile,
    delta: CadAudibilityDelta,
    difference_ref: AuthorityRef,
    conditions: CadListeningConditions | None = None,
    evaluated_at_utc: str | None = None,
) -> CadAudibilityAssessment:
    """Fail-closed audibility verdict on one physical difference (#720).

    - ``outside_scope``/``research_only`` profiles never verdict —
      ``outside_model_scope``.
    - A declared condition mismatch (stimulus/field/listener envelope)
      is ``outside_model_scope`` with named mismatches — ISO 226 on
      multichannel cinema is exactly this case.
    - A level-calibrated model without its level reference is
      ``insufficient_evidence``.
    - No threshold for the delta's observable/direction is
      ``indeterminate`` — the model says nothing rather than
      guessing a relevance.
    - ``magnitude ≤ threshold`` → ``not_distinguishable_under_profile``;
      strictly greater → ``potentially_audible``. Both carry the raw
      delta and conditions — never a universal score (#720 §12/§13).
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    conditions = conditions or CadListeningConditions()
    reasons: list[str] = []
    limitations: list[str] = []
    matched: CadAudibilityThreshold | None = None

    scope = profile.applicability.scope_class
    if scope in ('outside_scope', 'research_only'):
        verdict: AudibilityVerdict = 'outside_model_scope'
        reasons.append(
            f'profile {profile.profile_label} is declared '
            f'{scope} — it never produces an audibility verdict'
        )
    else:
        mismatches = _condition_mismatches(
            profile.applicability, conditions
        )
        if mismatches:
            verdict = 'outside_model_scope'
            reasons.extend(mismatches)
        elif profile.requires_level_calibration and (
            conditions.level_reference_ref is None
        ):
            verdict = 'insufficient_evidence'
            reasons.append(
                'the model requires a level-calibrated presentation '
                'but no level_reference_ref is pinned — audibility is '
                'level-dependent (#720 §9)'
            )
        else:
            candidates = [
                t for t in profile.thresholds
                if t.observable == delta.observable
                and _direction_matches(t.direction, delta.direction)
            ]
            if not candidates:
                verdict = 'indeterminate'
                reasons.append(
                    f'no declared threshold for {delta.observable} '
                    f'({delta.direction}) — the profile does not '
                    'pretend to judge this observable'
                )
            else:
                matched = min(
                    candidates, key=lambda t: t.threshold_value
                )
                if delta.magnitude <= matched.threshold_value:
                    verdict = 'not_distinguishable_under_profile'
                    reasons.append(
                        f'{delta.magnitude:g} {delta.magnitude_unit} is '
                        f'at/below the declared threshold '
                        f'{matched.threshold_value:g} '
                        f'{matched.threshold_unit} under the pinned '
                        'conditions — the difference stays recorded but '
                        'cannot rank candidates on audibility grounds'
                    )
                else:
                    verdict = 'potentially_audible'
                    reasons.append(
                        f'{delta.magnitude:g} {delta.magnitude_unit} '
                        f'exceeds the declared threshold '
                        f'{matched.threshold_value:g} '
                        f'{matched.threshold_unit} — a candidate for '
                        'perceptual prioritization, subject to physical '
                        'feasibility gates'
                    )
                if scope == 'applicable_with_limitations':
                    limitations.append(
                        'profile applicability is limited — the verdict '
                        'is bounded evidence, not a universal claim'
                    )
                if conditions.stimulus == 'undeclared':
                    limitations.append(
                        'stimulus undeclared — the verdict binds the '
                        'declared defaults only'
                    )
                if delta.direction == 'undeclared' and (
                    matched.direction == 'symmetric'
                ):
                    limitations.append(
                        'delta direction undeclared — evaluated against '
                        'the symmetric threshold'
                    )

    payload = dict(
        document_id=document_id,
        profile_ref=perceptual_profile_binding(profile),
        difference_ref=difference_ref,
        delta=delta,
        conditions=conditions,
        verdict=verdict,
        matched_threshold=matched,
        reasons=tuple(sorted(set(reasons))),
        limitations=tuple(sorted(set(limitations))),
        evaluated_at_utc=evaluated_at_utc,
        evaluation_version=AUDIBILITY_EVALUATION_VERSION,
    )
    return _seal(
        CadAudibilityAssessment, payload,
        'assessment_id', 'assessment_sha256', 'aud',
    )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_perceptual_model_profile(
    *,
    document_id: str,
    model_kind: PerceptualModelKind,
    profile_label: str,
    literature_ref: str,
    applicability: CadModelApplicability,
    literature_version: str = '',
    thresholds: tuple[CadAudibilityThreshold, ...]
    | list[CadAudibilityThreshold] = (),
    requires_level_calibration: bool = False,
    output_semantics: str = '',
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadPerceptualModelProfile:
    """Seal a perceptual model / threshold profile."""
    payload = dict(
        document_id=document_id,
        model_kind=model_kind,
        profile_label=profile_label,
        literature_ref=literature_ref,
        literature_version=literature_version,
        thresholds=tuple(thresholds),
        applicability=applicability,
        requires_level_calibration=requires_level_calibration,
        output_semantics=output_semantics,
        authority_version=AUDIBILITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal(
        CadPerceptualModelProfile, payload,
        'profile_id', 'profile_sha256', 'pprof',
    )


__all__ = [
    'AUDIBILITY_EVALUATION_VERSION',
    'AUDIBILITY_SCHEMA_VERSION',
    'AUDIBILITY_VERDICT_LABELS',
    'AudibilityObservable',
    'AudibilityVerdict',
    'CadAudibilityAssessment',
    'CadAudibilityDelta',
    'CadAudibilityThreshold',
    'CadListeningConditions',
    'CadModelApplicability',
    'CadPerceptualModelProfile',
    'DeltaDirection',
    'ListeningFieldClass',
    'ListenerClass',
    'MODEL_KIND_LABELS',
    'ModelScopeClass',
    'PerceptualModelKind',
    'StimulusClass',
    'audibility_assessment_binding',
    'build_perceptual_model_profile',
    'evaluate_audibility',
    'perceptual_profile_binding',
]
