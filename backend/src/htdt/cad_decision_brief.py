"""Decision brief authority (#937).

Optimization already produces Pareto candidates, intervention studies,
sealed solver/channel/deployment/campaign verdicts and a production
readiness gate — but the operator still has to turn "non-dominated
candidate" into "what do I do next" by hand. :class:`CadDecisionBrief` is
the sealed record that does the conversion: given the baseline and the
alternative candidates of one exact scene revision plus their *declared*
evidence pins, it ranks the candidate actions and states, per action, an
honest next step.

Contract properties:

- fail-closed evidence chain: an action is only ``ready`` when every
  named gate (solver gate, channel verify, deployment, campaign and the
  production gate) is pinned to a sealed verdict observed as ``verified``
  and ``current``; missing, failed or stale evidence lists the action as
  ``not_ready`` with the precise gap — it is never silently promoted;
- no single-score collapse: per-dimension objective deltas keep the
  established evidence-class vocabulary (measured / predicted / derived /
  unknown), trade-offs stay explicit, and ``ranking_explanation`` is one
  deterministic sentence grounded in the pinned evidence;
- the production gate is structural: without a ``production_ready``
  readiness pin an action stays ``requires_verification`` — the brief
  can never express a product recommendation the gate did not earn;
- deterministic: the brief is a pure function of its inputs — the same
  candidates, evidence pins and timestamp produce a byte-identical
  record (``brief_id``/``brief_sha256`` are content-addressed);
- nothing is inferred: gate pins and cost provenance are declared by the
  caller; a cost without complete provenance (amount, currency, source,
  date, quantity, assumptions) is recorded as ``unknown``, never
  fabricated.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from math import isfinite
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash
from .cad_delegated_provider import _seal


DECISION_BRIEF_SCHEMA_VERSION = 1
DECISION_BRIEF_AUTHORITY_VERSION = 'decision-brief-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'

#: The named evidence gates a ready action must close, in declared order.
#: ``solver_gate`` — the solver path's sealed verdict for the candidate;
#: ``channel_verify`` — channel/routing verification on the applied
#: variant; ``deployment`` — deployment pipeline verdict; ``campaign`` —
#: measurement-campaign verdict; ``production_gate`` — the #801
#: production-readiness adoption decision.
DecisionGateKind = Literal[
    'solver_gate',
    'channel_verify',
    'deployment',
    'campaign',
    'production_gate',
]
DECISION_GATE_ORDER: tuple[DecisionGateKind, ...] = (
    'solver_gate',
    'channel_verify',
    'deployment',
    'campaign',
    'production_gate',
)

#: Outcome class the caller observed on the pinned verdict record:
#: ``verified`` — the sealed verdict passed; ``conditional`` — the sealed
#: verdict is a bounded/partial pass (limited adoption, assisted-only
#: deploy, partial campaign); ``failed`` — the sealed verdict failed.
GateVerdict = Literal['verified', 'conditional', 'failed']

#: Whether the pinned evidence still describes the current inputs.
PinFreshness = Literal['current', 'stale', 'unknown']

GateState = Literal['satisfied', 'open', 'blocked']

#: Why a gate (or the action itself) cannot recommend.
GapCode = Literal[
    'evidence_missing',
    'evidence_stale',
    'verdict_failed',
    'not_comparable',
]

DecisionTier = Literal['ready', 'conditional', 'not_ready']

#: ``recommended`` is structurally bound to ``ready`` — only a complete
#: evidence chain may read as a recommendation; everything else is
#: ``requires_verification`` (要検証の候補), never a product pitch.
DecisionDisposition = Literal['recommended', 'requires_verification']

#: Brief-level headline tier; ``none`` when the brief has no actions.
BriefTopTier = Literal['ready', 'conditional', 'not_ready', 'none']

DeltaDirection = Literal['improved', 'regressed', 'unchanged', 'unknown']

#: Established O30 evidence-class vocabulary — a delta may also be
#: honestly ``unknown`` when no evidence addresses the dimension.
DeltaBasis = Literal['measured', 'predicted', 'derived', 'unknown']

DecisionChangeDomain = Literal[
    'placement', 'equipment', 'treatment', 'calibration', 'measurement',
    'other',
]

RecommendationKind = Literal[
    'apply_candidate',
    'remeasure',
    'verify_channel',
    'deploy',
    'collect_evidence',
]
#: Recommendation kinds that close evidence gaps instead of applying
#: the candidate — the only kinds a non-``ready`` action may carry.
_VERIFICATION_KINDS: frozenset[str] = frozenset(
    {'remeasure', 'verify_channel', 'deploy', 'collect_evidence'}
)

RecommendationActor = Literal['operator', 'calibrator', 'system']

ValidationState = Literal[
    'physically_validated', 'holdout_validated', 'unvalidated', 'unknown',
]

BriefFreshness = Literal['current', 'stale']


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _validate_instant(value: str) -> None:
    """Enforce strict, explicitly-UTC ISO 8601 persisted timestamps (#870)."""

    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('created_at_utc is not a valid ISO 8601 instant') from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError('created_at_utc must carry an explicit UTC offset')


class DecisionEvidencePin(BaseModel):
    """Exact pin of one sealed verdict record backing a gate.

    The pin is caller-declared evidence: it names the sealed authority row
    (kind/id/sha256) that was observed, its evidence class and whether it
    still describes the current inputs. The brief seals the declaration —
    persistence re-resolves what the deployment can resolve; an absent pin
    is the ``evidence_missing`` gap, never an implied pass.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: str = Field(min_length=1)
    ref_id: str = Field(min_length=1)
    ref_sha256: str = Field(pattern=_SHA256_PATTERN)
    evidence_class: Literal['measured', 'predicted', 'derived']
    freshness: PinFreshness

    def as_authority_ref(self) -> AuthorityRef:
        return AuthorityRef(
            kind=self.kind,
            ref_id=self.ref_id,
            ref_sha256=self.ref_sha256,
        )


class DecisionGate(BaseModel):
    """One named evidence gate on the action's chain.

    ``pin`` names the sealed verdict record; ``verdict`` is the outcome
    class observed on that record. A gate with no pin is the precise
    ``evidence_missing`` gap — the chain is incomplete and says so.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    gate: DecisionGateKind
    pin: DecisionEvidencePin | None = None
    verdict: GateVerdict | None = None
    note: str | None = None

    @model_validator(mode='after')
    def valid_gate(self) -> 'DecisionGate':
        if self.pin is None and self.verdict is not None:
            raise ValueError(
                'a gate verdict requires the pinned record it was read from'
            )
        if self.pin is not None and self.verdict is None:
            raise ValueError(
                'a pinned gate must declare the observed verdict class'
            )
        return self

    def gate_state(self) -> GateState:
        """The honest gate state derived from pin + observed verdict."""

        if self.pin is None:
            return 'blocked'
        if self.pin.freshness == 'stale':
            return 'blocked'
        if self.verdict == 'failed':
            return 'blocked'
        if self.verdict == 'verified' and self.pin.freshness == 'current':
            return 'satisfied'
        # pinned evidence whose freshness is unconfirmed, or whose verdict
        # is bounded/partial — the gate is named and open, never absent.
        return 'open'

    def gap(self) -> 'DecisionGap | None':
        """The precise gap when the gate blocks the action."""

        if self.pin is None:
            return DecisionGap(
                gate=self.gate,
                code='evidence_missing',
                detail=self.note
                or 'このゲートにピンされた検証レコードがありません',
            )
        if self.pin.freshness == 'stale':
            return DecisionGap(
                gate=self.gate,
                code='evidence_stale',
                detail=self.note
                or 'ピンされた検証レコードが現在の入力と一致しません',
            )
        if self.verdict == 'failed':
            return DecisionGap(
                gate=self.gate,
                code='verdict_failed',
                detail=self.note
                or 'ピンされた検証レコードが不合格です',
            )
        return None


class DecisionGap(BaseModel):
    """A named, precise reason an action cannot be recommended."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    gate: str = Field(min_length=1)
    code: GapCode
    detail: str = Field(min_length=1)


class DecisionDelta(BaseModel):
    """One objective dimension's per-dimension contribution.

    Directions and bases are declared per dimension; nothing is summed,
    weighted or collapsed into a universal score.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    objective_id: str = Field(min_length=1)
    direction: DeltaDirection
    basis: DeltaBasis
    detail: str | None = None


class DecisionChange(BaseModel):
    """What the candidate changes, in user-facing words."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    domain: DecisionChangeDomain
    detail: str = Field(min_length=1)


class DecisionCost(BaseModel):
    """Cost/construction burden — only with full provenance.

    A ``known`` cost must carry amount, currency, source, quote date,
    quantity and assumptions; anything less is ``unknown``. The model
    rejects partial provenance so a fabricated or unverifiable figure can
    never read as priced.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    state: Literal['known', 'unknown'] = 'unknown'
    amount: float | None = None
    currency: str | None = Field(default=None, min_length=1)
    source: str | None = Field(default=None, min_length=1)
    quoted_on: str | None = Field(default=None, min_length=1)
    quantity: str | None = Field(default=None, min_length=1)
    assumptions: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_cost(self) -> 'DecisionCost':
        provenance = (
            self.currency,
            self.source,
            self.quoted_on,
            self.quantity,
            self.assumptions,
        )
        if self.state == 'unknown':
            if self.amount is not None or any(
                item is not None for item in provenance
            ):
                raise ValueError(
                    'an unknown cost carries no amount or provenance'
                )
            return self
        if self.amount is None or not isfinite(self.amount):
            raise ValueError('a known cost needs a finite amount')
        missing = [
            name
            for name, value in zip(
                ('currency', 'source', 'quoted_on', 'quantity', 'assumptions'),
                provenance,
            )
            if value is None
        ]
        if missing:
            raise ValueError(
                'a known cost requires complete provenance: '
                + ', '.join(missing)
            )
        return self


class DecisionEvidenceProfile(BaseModel):
    """User-facing provenance summary for the action's evidence.

    ``solver_note`` names the solver path/input constraints in operator
    words; ``validation_state`` records holdout/physical validation
    honestly; ``applicability_note`` states the confidence/applicability
    reason — the fields stay verbatim so nothing is paraphrased into
    false precision.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    solver_note: str | None = None
    validation_state: ValidationState = 'unknown'
    applicability_note: str | None = None


class DecisionRecommendation(BaseModel):
    """The next action: why / who / what / how to verify.

    ``kind`` is constrained per tier — a ``ready`` action recommends
    applying the candidate; every other tier may only name a
    verification step, so a blocked candidate can never read as a
    product recommendation.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: RecommendationKind
    why: str = Field(min_length=1)
    actor: RecommendationActor
    verify_by: str = Field(min_length=1)


def _derive_gaps(
    gates: tuple[DecisionGate, ...],
    comparability: str,
) -> tuple[DecisionGap, ...]:
    gaps: list[DecisionGap] = []
    for gate in gates:
        gap = gate.gap()
        if gap is not None:
            gaps.append(gap)
    if comparability != 'comparable':
        gaps.append(
            DecisionGap(
                gate='comparison',
                code='not_comparable',
                detail=(
                    '同一基準で比較できない候補です'
                    if comparability == 'incompatible_fidelity'
                    else '候補の比較可能性が確立していません'
                ),
            )
        )
    return tuple(gaps)


def _derive_tier(
    gates: tuple[DecisionGate, ...],
    comparability: str,
) -> DecisionTier:
    """The honest tier: complete verified chain → ``ready``.

    Any blocked gate or a comparability gap is ``not_ready`` — the action
    is listed with its gap list, never promoted. Named-but-open gates
    (bounded verdicts, unconfirmed freshness) make it ``conditional``.
    """

    states = tuple(gate.gate_state() for gate in gates)
    if 'blocked' in states or comparability != 'comparable':
        return 'not_ready'
    if 'open' in states:
        return 'conditional'
    return 'ready'


def _disposition_for(tier: DecisionTier) -> DecisionDisposition:
    return 'recommended' if tier == 'ready' else 'requires_verification'


class DecisionAction(BaseModel):
    """One candidate converted into a next-action row.

    ``tier``, ``gaps``, ``rank`` and ``disposition`` are stored so the
    sealed record states its own verdicts — and re-derived on validate,
    so a forged tier cannot survive construction.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    action_id: str = Field(pattern=r'^dact-[0-9a-f]{24}$')
    candidate_ref: AuthorityRef
    label: str = Field(min_length=1)
    changes: tuple[DecisionChange, ...] = ()
    deltas: tuple[DecisionDelta, ...] = ()
    gates: tuple[DecisionGate, ...]
    comparability: Literal['comparable', 'incompatible_fidelity', 'unknown']
    evidence_profile: DecisionEvidenceProfile = Field(
        default_factory=DecisionEvidenceProfile
    )
    cost: DecisionCost = Field(default_factory=DecisionCost)
    recommendation: DecisionRecommendation
    tier: DecisionTier
    disposition: DecisionDisposition
    gaps: tuple[DecisionGap, ...]
    rank: int = Field(ge=1)

    @model_validator(mode='after')
    def valid_action(self) -> 'DecisionAction':
        if self.candidate_ref.ref_sha256 is None:
            raise ValueError(
                'candidate_ref must pin the candidate semantic hash'
            )
        gate_kinds = tuple(gate.gate for gate in self.gates)
        if gate_kinds != DECISION_GATE_ORDER:
            raise ValueError(
                'action gates must enumerate DECISION_GATE_ORDER once each, '
                'in order'
            )
        delta_ids = [delta.objective_id for delta in self.deltas]
        if len(delta_ids) != len(set(delta_ids)):
            raise ValueError('action objective deltas must be unique')
        expected_tier = _derive_tier(self.gates, self.comparability)
        if self.tier != expected_tier:
            raise ValueError(
                f'action tier {self.tier} does not match derived '
                f'{expected_tier}'
            )
        if self.disposition != _disposition_for(self.tier):
            raise ValueError('action disposition does not match its tier')
        if self.gaps != _derive_gaps(self.gates, self.comparability):
            raise ValueError('action gaps do not match derived gap list')
        if self.tier == 'ready':
            if self.recommendation.kind != 'apply_candidate':
                raise ValueError(
                    'a ready action recommends applying the candidate'
                )
        elif self.recommendation.kind not in _VERIFICATION_KINDS:
            raise ValueError(
                'a non-ready action may only recommend a verification step'
            )
        return self

    @property
    def satisfied_gate_count(self) -> int:
        return sum(
            1 for gate in self.gates if gate.gate_state() == 'satisfied'
        )

    @property
    def improved_delta_count(self) -> int:
        return sum(
            1 for delta in self.deltas if delta.direction == 'improved'
        )


def _action_id(candidate_ref: AuthorityRef, label: str) -> str:
    """Deterministic action identity — a pure function of the candidate."""

    return _semantic_id(
        'dact',
        _hash(
            {
                'candidate_ref': candidate_ref.model_dump(mode='json'),
                'label': label,
            }
        ),
    )


class CadDecisionBrief(BaseModel):
    """Sealed decision brief for one baseline + its candidate actions."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = DECISION_BRIEF_SCHEMA_VERSION
    authority_version: Literal['decision-brief-1'] = (
        DECISION_BRIEF_AUTHORITY_VERSION
    )
    brief_id: str = Field(pattern=r'^dbrief-[0-9a-f]{24}$')
    document_id: str = Field(min_length=1)
    #: The exact scene revision every candidate shares — the brief is
    #: pinned to this baseline and reports stale rather than re-show its
    #: conclusions once the scene head moves on.
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=_SHA256_PATTERN)
    baseline_ref: AuthorityRef
    baseline_label: str = Field(min_length=1)
    #: The A/B surface the brief was composed from, when one exists.
    comparison_ref: AuthorityRef | None = None
    actions: tuple[DecisionAction, ...] = ()
    top_tier: BriefTopTier
    action_count: int = Field(ge=0)
    ready_count: int = Field(ge=0)
    ranking_explanation: str = Field(min_length=1)
    provenance: tuple[str, ...] = ()
    created_at_utc: str = Field(min_length=1)
    brief_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_brief(self) -> 'CadDecisionBrief':
        _validate_instant(self.created_at_utc)
        if self.baseline_ref.ref_sha256 is None:
            raise ValueError(
                'baseline_ref must pin the baseline semantic hash'
            )
        action_ids = [action.action_id for action in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError('brief action ids must be unique')
        candidate_keys = [
            (action.candidate_ref.kind, action.candidate_ref.ref_id)
            for action in self.actions
        ]
        if len(candidate_keys) != len(set(candidate_keys)):
            raise ValueError('brief candidate refs must be unique')
        baseline_key = (self.baseline_ref.kind, self.baseline_ref.ref_id)
        if baseline_key in candidate_keys:
            raise ValueError('the baseline cannot be its own candidate')
        if tuple(action.rank for action in self.actions) != tuple(
            range(1, len(self.actions) + 1)
        ):
            raise ValueError('actions must be stored in rank order 1..N')
        if self.action_count != len(self.actions):
            raise ValueError('action_count does not match the action list')
        if self.ready_count != sum(
            1 for action in self.actions if action.tier == 'ready'
        ):
            raise ValueError('ready_count does not match the action list')
        expected_top: BriefTopTier = (
            self.actions[0].tier if self.actions else 'none'
        )
        if self.top_tier != expected_top:
            raise ValueError('top_tier does not match the first action')
        if self.brief_sha256 != _hash(self.identity_payload()):
            raise ValueError('CadDecisionBrief hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('brief_id', None)
        payload.pop('brief_sha256', None)
        return payload


def _rank_key(action: DecisionAction) -> tuple[Any, ...]:
    tier_order = {'ready': 0, 'conditional': 1, 'not_ready': 2}
    return (
        tier_order[action.tier],
        -action.satisfied_gate_count,
        -action.improved_delta_count,
        action.action_id,
    )


def _tier_word(tier: DecisionTier) -> str:
    return {
        'ready': '推奨可能',
        'conditional': '条件付き',
        'not_ready': '要検証',
    }[tier]


def _explain_ranking(actions: tuple[DecisionAction, ...]) -> str:
    """One deterministic sentence for why rank 1 outranks rank 2.

    The sentence is generated from the computed tier, gate states and
    per-dimension delta counts only — it can only say what the pinned
    evidence supports.
    """

    top = actions[0]
    if len(actions) == 1:
        return (
            f'{top.label} が唯一の評価済みアクションです'
            f'（{_tier_word(top.tier)}、証拠ゲート '
            f'{top.satisfied_gate_count}/{len(top.gates)} 検証済み）'
        )
    nxt = actions[1]
    if top.tier != nxt.tier:
        detail = ''
        if nxt.gaps:
            gap = nxt.gaps[0]
            detail = f'（最初の欠落: {gap.gate} {gap.code}）'
        return (
            f'{top.label} が {nxt.label} を上回ります: {top.label} は'
            f'{_tier_word(top.tier)}、{nxt.label} は'
            f'{_tier_word(nxt.tier)}です{detail}'
        )
    if top.satisfied_gate_count != nxt.satisfied_gate_count:
        return (
            f'{top.label} が {nxt.label} を上回ります: 検証済みの証拠'
            f'ゲートが多いためです（{top.satisfied_gate_count}/'
            f'{len(top.gates)} 対 {nxt.satisfied_gate_count}/'
            f'{len(nxt.gates)}）'
        )
    if top.improved_delta_count != nxt.improved_delta_count:
        return (
            f'{top.label} が {nxt.label} を上回ります: 同一の証拠階層で'
            f'改善した追跡指標が多いためです（{top.improved_delta_count}'
            f' 対 {nxt.improved_delta_count}）'
        )
    return (
        f'{top.label} が {nxt.label} を上回ります: 証拠の位置が同一の'
        'ため、アクションID順の確定的な順位です'
    )


def build_decision_action(
    *,
    candidate_ref: AuthorityRef,
    label: str,
    gates: tuple[DecisionGate, ...],
    comparability: Literal['comparable', 'incompatible_fidelity', 'unknown'],
    recommendation: DecisionRecommendation,
    changes: tuple[DecisionChange, ...] = (),
    deltas: tuple[DecisionDelta, ...] = (),
    evidence_profile: DecisionEvidenceProfile | None = None,
    cost: DecisionCost | None = None,
    rank: int = 1,
) -> DecisionAction:
    """Compose one action — tier/gaps/disposition are derived, not taken."""

    declared = {gate.gate: gate for gate in gates}
    unknown = sorted(set(declared) - set(DECISION_GATE_ORDER))
    if unknown:
        raise ValueError(
            f'unknown decision gate kinds: {", ".join(unknown)}'
        )
    gate_tuple = tuple(
        DecisionGate(
            gate=gate,
            pin=None,
            verdict=None,
            note=None,
        )
        for gate in DECISION_GATE_ORDER
    )
    gates_in_order = tuple(
        declared.get(kind, fallback)
        for kind, fallback in zip(DECISION_GATE_ORDER, gate_tuple)
    )
    tier = _derive_tier(gates_in_order, comparability)
    payload: dict[str, Any] = {
        'action_id': _action_id(candidate_ref, label),
        'candidate_ref': candidate_ref,
        'label': label,
        'changes': tuple(changes),
        'deltas': tuple(deltas),
        'gates': gates_in_order,
        'comparability': comparability,
        'evidence_profile': evidence_profile or DecisionEvidenceProfile(),
        'cost': cost or DecisionCost(),
        'recommendation': recommendation,
        'tier': tier,
        'disposition': _disposition_for(tier),
        'gaps': _derive_gaps(gates_in_order, comparability),
        'rank': rank,
    }
    return DecisionAction(**payload)


def build_decision_brief(
    *,
    document_id: str,
    scene_revision_id: str,
    scene_content_hash: str,
    baseline_ref: AuthorityRef,
    baseline_label: str,
    actions: tuple[DecisionAction, ...] = (),
    comparison_ref: AuthorityRef | None = None,
    provenance: tuple[str, ...] = (),
    created_at_utc: str,
) -> CadDecisionBrief:
    """Seal a decision brief — a pure function of its inputs.

    Actions are re-ranked deterministically (tier, verified-gate count,
    improved-dimension count, action id) so the same candidate set and
    evidence always produce a byte-identical record.
    """

    ranked = tuple(
        DecisionAction(
            **action.model_dump(mode='python', exclude={'rank'}),
            rank=index,
        )
        for index, action in enumerate(
            sorted(actions, key=_rank_key), start=1
        )
    )
    payload: dict[str, Any] = {
        'document_id': document_id,
        'scene_revision_id': scene_revision_id,
        'scene_content_hash': scene_content_hash,
        'baseline_ref': baseline_ref,
        'baseline_label': baseline_label,
        'comparison_ref': comparison_ref,
        'actions': ranked,
        'top_tier': ranked[0].tier if ranked else 'none',
        'action_count': len(ranked),
        'ready_count': sum(
            1 for action in ranked if action.tier == 'ready'
        ),
        'ranking_explanation': (
            _explain_ranking(ranked)
            if ranked
            else '評価された候補アクションがありません'
        ),
        'provenance': tuple(provenance),
        'created_at_utc': created_at_utc,
    }
    return _seal(
        CadDecisionBrief, payload, 'brief_id', 'brief_sha256', 'dbrief'
    )


def decision_brief_ref(brief: CadDecisionBrief) -> AuthorityRef:
    """The typed ref downstream authorities use to pin this brief."""

    return AuthorityRef(
        kind='decision_brief',
        ref_id=brief.brief_id,
        ref_sha256=brief.brief_sha256,
    )


def brief_freshness(
    brief: CadDecisionBrief,
    *,
    current_scene_revision_id: str,
    current_scene_content_hash: str | None = None,
) -> tuple[BriefFreshness, str]:
    """Whether the brief still describes the scene head.

    A brief pinned to a revision that is no longer current reports
    ``stale`` — callers must not re-present its conclusions as live.
    """

    if brief.scene_revision_id != current_scene_revision_id:
        return (
            'stale',
            '決定ブリーフが現在でないシーンリビジョンにピンされています',
        )
    if (
        current_scene_content_hash is not None
        and brief.scene_content_hash != current_scene_content_hash
    ):
        return (
            'stale',
            '決定ブリーフのシーン内容ハッシュが変更されています',
        )
    return 'current', '決定ブリーフは現在のシーンリビジョンと一致しています'


def collect_gate_pins(
    brief: CadDecisionBrief,
) -> tuple[AuthorityRef, ...]:
    """Every authority ref the brief binds — identity refs plus pins."""

    refs: list[AuthorityRef] = [brief.baseline_ref]
    if brief.comparison_ref is not None:
        refs.append(brief.comparison_ref)
    for action in brief.actions:
        refs.append(action.candidate_ref)
        for gate in action.gates:
            if gate.pin is not None:
                refs.append(gate.pin.as_authority_ref())
    return tuple(refs)


__all__ = [
    'BriefFreshness',
    'BriefTopTier',
    'DECISION_BRIEF_AUTHORITY_VERSION',
    'DECISION_BRIEF_SCHEMA_VERSION',
    'DECISION_GATE_ORDER',
    'CadDecisionBrief',
    'DecisionAction',
    'DecisionChange',
    'DecisionChangeDomain',
    'DecisionCost',
    'DecisionDisposition',
    'DecisionEvidencePin',
    'DecisionEvidenceProfile',
    'DecisionGap',
    'DecisionGate',
    'DecisionGateKind',
    'DecisionRecommendation',
    'DecisionTier',
    'DeltaBasis',
    'DeltaDirection',
    'GapCode',
    'GateState',
    'GateVerdict',
    'PinFreshness',
    'RecommendationActor',
    'RecommendationKind',
    'ValidationState',
    'brief_freshness',
    'build_decision_action',
    'build_decision_brief',
    'collect_gate_pins',
    'decision_brief_ref',
]
