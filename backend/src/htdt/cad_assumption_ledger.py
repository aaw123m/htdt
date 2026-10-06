"""Engineering assumption / permissible-use ledger (#730, REV59-UNITS).

HTDT refuses to fabricate unknowns, but real projects still proceed on
explicit engineering assumptions: hidden wall construction, generic
material coefficients, undocumented device behavior, environmental
state, installation tolerance. When those live only inside solver code
or config records, nobody can answer *which conclusions depend on which
assumption* or *which assumption should be replaced by measurement
next*. This module is the ledger that makes each assumption a sealed,
queryable authority record.

- :class:`CadEngineeringAssumption` — the sealed assumption record:
  subject ref, value binding (a #728 typed-quantity pin where the
  assumed thing is dimensional, a declared label where it is discrete),
  evidence class, conservatism, rationale/source, declared permissible
  uses, expiry/review trigger and the evidence that would resolve it.
  A default never enters silently: it exists only as an explicit
  ``default_assumed`` record.
- :class:`CadAssumptionResolution` — the append-only lifecycle event:
  an assumption is never edited; evidence resolves, partially resolves,
  replaces or contradicts it, and the original stays as provenance.
- :func:`evaluate_permissible_use` + :class:`CadPermissibleUseAssessment`
  — the sealed gate answering whether the project's open assumptions
  permit an intended use (concept design … commissioning …
  safety-critical decision) with per-assumption limiting reasons.

Composition (bindings, never merges):

- #620 ``cad_assumption_decision`` — a scoped *acceptance decision*
  (user attests a register gap); this ledger is the broader lifecycle
  authority and may carry the decision as ``decision_ref`` evidence.
- #604 ``cad_uncertainty_propagation`` — ranges/distributions and
  decision sensitivity stay with propagation; the assumption records
  its range through quantity refs and declares ``sensitivity``.
- #689 ``cad_parameter_identifiability`` — a calibrated parameter
  remains ``calibrated_inverse_estimated`` evidence; a good fit never
  upgrades it to a measurement.
- #729 dependency composition — affected-result refs are pinned so a
  resolved/contradicted assumption can stale its dependents.

Literature basis
----------------
- NASA-STD-7009B (active, 2024-03-05) — credible M&S documents a
  statement of intended use, assumptions/abstractions and permissible
  uses / validated limits. HTDT adopts the *concepts* (input pedigree,
  intended use, assumption disclosure), not a compliance claim.
- Aleatory vs epistemic uncertainty distinction (#604): a declared
  range or scenario set is preserved rather than collapsed into a fake
  central value.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


ASSUMPTION_SCHEMA_VERSION = 'assumption-ledger-1'
ASSUMPTION_EVALUATION_VERSION = 'asm-eval-1'

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
# Taxonomies (#730)
# ---------------------------------------------------------------------------

EvidenceState = Literal[
    'measured_observed',
    'manufacturer_declared',
    'independent_reference',
    'derived',
    'calibrated_inverse_estimated',
    'project_assumed',
    'literature_assumed',
    'default_assumed',
    'exploratory_scenario',
    'unknown',
]
"""#730 §1 — how the assumed value/state is evidenced. The ladder is the
honesty boundary: ``calibrated_inverse_estimated`` stays distinct from
``measured_observed`` (#689), and ``unknown`` is a declared state, never
a silent nominal value."""

#: Evidence classes that constitute an *assumption* rather than an
#: observed fact. Permissible-use gating applies to these.
ASSUMPTION_EVIDENCE_STATES: frozenset[str] = frozenset({
    'calibrated_inverse_estimated',
    'project_assumed',
    'literature_assumed',
    'default_assumed',
    'exploratory_scenario',
    'unknown',
})

AssumptionKind = Literal[
    'physical_parameter',
    'geometry',
    'boundary_condition',
    'source_model',
    'equipment_capability',
    'device_behavior',
    'environment',
    'operating_state',
    'installation_tolerance',
    'user_preference_target',
    'external_standard_interpretation',
    'commercial_project_constraint',
    'other_declared',
]
"""#730 §4 — what the assumption is about. Preference and commercial
records never masquerade as physics: their evidence class is restricted
to assumption states."""

#: Kinds that are not physical claims — they cannot carry observed
#: evidence classes.
_NONPHYSICAL_KINDS: frozenset[str] = frozenset({
    'user_preference_target',
    'commercial_project_constraint',
})

ConservatismKind = Literal[
    'nominal',
    'conservative_low',
    'conservative_high',
    'worst_case_bound',
    'best_case_bound',
    'central_estimate',
    'scenario_only',
    'undeclared',
]
"""#730 §7 — a conservative bound is never called 'measured' or treated
as the statistically probable value; the bound direction is identity."""

IntendedUse = Literal[
    'concept_design',
    'rough_layout_screening',
    'optimization_shortlist',
    'procurement_decision',
    'construction_drawing',
    'quantitative_solver_validation',
    'rp22_design_evaluation',
    'commissioning_pass_fail',
    'safety_critical_decision',
]
"""#730 §5 — permissible use is per-use, never one global 'valid' flag.
Ordered weakest→strongest claim so the gate can reason about it."""

#: Strictness order — later uses demand stronger evidence.
_USE_STRICTNESS: dict[str, int] = {
    'concept_design': 0,
    'rough_layout_screening': 1,
    'optimization_shortlist': 2,
    'procurement_decision': 3,
    'construction_drawing': 4,
    'rp22_design_evaluation': 5,
    'quantitative_solver_validation': 6,
    'commissioning_pass_fail': 7,
    'safety_critical_decision': 8,
}

#: Uses at or above this strictness count as validation-grade claims —
#: weak evidence cannot gate into them.
_VALIDATION_GRADE_STRICTNESS = 5

SensitivityClass = Literal[
    'high',
    'medium',
    'low',
    'not_evaluated',
]
"""#730 §11 — decision sensitivity (compose #604): whether plausible
variation of the assumption can change the decision at hand.``high``
amplifies gating; ``not_evaluated`` is honest, never assumed low."""

ResolutionState = Literal[
    'open_assumption',
    'evidence_requested',
    'partially_resolved',
    'replaced_by_observation',
    'replaced_by_measurement',
    'replaced_by_approved_design_fact',
    'superseded',
    'rejected_contradicted',
]
"""#730 §12 — append-only lifecycle. The terminal replaced_* / rejected
states never erase the original record."""

PermissibleVerdict = Literal[
    'permissible',
    'permissible_with_limitations',
    'stronger_evidence_required',
    'incompatible',
    'unknown',
]
"""#730 §6 — the gate's answer, with reasons naming the limiting
assumptions."""

VERDICT_RANK: dict[str, int] = {
    'permissible': 0,
    'permissible_with_limitations': 1,
    'stronger_evidence_required': 2,
    'incompatible': 3,
    'unknown': 4,
}

EVIDENCE_STATE_LABELS: dict[str, str] = {
    'measured_observed': '測定・観測済み',
    'manufacturer_declared': 'メーカー公称値',
    'independent_reference': '独立文献値',
    'derived': '導出値',
    'calibrated_inverse_estimated': '校正逆推定値',
    'project_assumed': 'プロジェクト仮定',
    'literature_assumed': '文献仮定値',
    'default_assumed': '既定仮定値',
    'exploratory_scenario': '探索シナリオ',
    'unknown': '不明',
}

ASSUMPTION_KIND_LABELS: dict[str, str] = {
    'physical_parameter': '物理パラメータ',
    'geometry': '幾何形状',
    'boundary_condition': '境界条件',
    'source_model': '音源モデル',
    'equipment_capability': '機器能力',
    'device_behavior': '機器振る舞い',
    'environment': '環境状態',
    'operating_state': '動作状態',
    'installation_tolerance': '設置公差',
    'user_preference_target': 'ユーザー目標',
    'external_standard_interpretation': '外部規格解釈',
    'commercial_project_constraint': '商業・プロジェクト制約',
    'other_declared': 'その他(宣言)',
}

INTENDED_USE_LABELS: dict[str, str] = {
    'concept_design': '概念設計',
    'rough_layout_screening': '概略レイアウト検討',
    'optimization_shortlist': '最適化候補絞込み',
    'procurement_decision': '調達判断',
    'construction_drawing': '施工図',
    'quantitative_solver_validation': '定量ソルバー検証',
    'rp22_design_evaluation': 'RP22設計評価',
    'commissioning_pass_fail': 'コミッショニング合否',
    'safety_critical_decision': '安全判断',
}

PERMISSIBLE_VERDICT_LABELS: dict[str, str] = {
    'permissible': '許容',
    'permissible_with_limitations': '許容（制約付き）',
    'stronger_evidence_required': 'より強い証拠が必要',
    'incompatible': '不適合',
    'unknown': '不明',
}

RESOLUTION_STATE_LABELS: dict[str, str] = {
    'open_assumption': '未解決の仮定',
    'evidence_requested': '証拠要求中',
    'partially_resolved': '一部解決',
    'replaced_by_observation': '観測で置換',
    'replaced_by_measurement': '測定で置換',
    'replaced_by_approved_design_fact': '承認済み設計事実で置換',
    'superseded': '新仮定で置換',
    'rejected_contradicted': '反証済み',
}


# ---------------------------------------------------------------------------
# Embedded descriptors
# ---------------------------------------------------------------------------


class CadAssumptionValue(BaseModel):
    """What is being assumed (#730 §2/§8).

    A dimensional assumption binds a #728 typed quantity pin
    (``value_quantity_ref``); a weak-evidence assumption may carry a
    *range* (low/high quantity pins) instead of a point; a discrete
    physical hypothesis (``wall cavity = insulated``) carries
    ``declared_value_label``. An ``unknown`` assumption asserts none of
    them — it records *that* nothing is known.
    """

    model_config = ConfigDict(frozen=True)

    value_quantity_ref: AuthorityRef | None = None
    range_low_ref: AuthorityRef | None = None
    range_high_ref: AuthorityRef | None = None
    declared_value_label: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_value(self) -> 'CadAssumptionValue':
        for label, ref in (
            ('value_quantity_ref', self.value_quantity_ref),
            ('range_low_ref', self.range_low_ref),
            ('range_high_ref', self.range_high_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must carry its sha256 pin')
        if (self.range_low_ref is None) != (self.range_high_ref is None):
            raise ValueError(
                'a ranged assumption requires both range bounds'
            )
        if self.value_quantity_ref is not None and (
            self.range_low_ref is not None
        ):
            raise ValueError(
                'a point value and a range are alternatives — never both'
            )
        if self.declared_value_label is not None and (
            self.value_quantity_ref is not None
            or self.range_low_ref is not None
        ):
            raise ValueError(
                'declared_value_label is the discrete-state form — '
                'it never coexists with a numeric pin'
            )
        if (
            self.value_quantity_ref is None
            and self.range_low_ref is None
            and not self.declared_value_label
        ):
            raise ValueError(
                'an assumption value must be a pinned quantity, a range, '
                'or a declared label — a silent default is not a value'
            )
        return self


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


class CadEngineeringAssumption(BaseModel):
    """One sealed engineering assumption (#730 goal).

    The ledger atom: what is assumed (subject + value binding), why it is
    needed (rationale/source), how strong it is (evidence class +
    conservatism + uncertainty ref), where it may be used
    (``permissible_uses``), when it must be revisited (review trigger),
    and what evidence would resolve it. ``unknown`` evidence is legal —
    it is the honest record that the project chose to proceed with a
    declared gap, not an invisible nominal.
    """

    model_config = ConfigDict(frozen=True)

    assumption_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    subject_ref: AuthorityRef
    assumption_kind: AssumptionKind
    value: CadAssumptionValue | None = None
    evidence_state: EvidenceState
    conservatism: ConservatismKind = 'undeclared'
    rationale: str = Field(min_length=1)
    source_label: str = ''
    uncertainty_ref: AuthorityRef | None = None
    permissible_uses: tuple[IntendedUse, ...] = ()
    sensitivity: SensitivityClass = 'not_evaluated'
    affected_result_refs: tuple[AuthorityRef, ...] = ()
    #: Mutually exclusive hypotheses share a group id — predictions stay
    #: separate branches (#730 §9), never averaged into a fictional
    #: material.
    scenario_group: str | None = Field(default=None, min_length=1)
    scenario_label: str | None = Field(default=None, min_length=1)
    expiry_trigger: str = ''
    resolution_needed: str = ''
    decision_ref: AuthorityRef | None = None
    supersedes_ref: AuthorityRef | None = None
    author: str | None = None
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    assumption_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_assumption(self) -> 'CadEngineeringAssumption':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        if self.subject_ref.ref_sha256 is None:
            raise ValueError('subject_ref must pin its sha256')
        if self.assumption_kind in _NONPHYSICAL_KINDS and (
            self.evidence_state in (
                'measured_observed',
                'manufacturer_declared',
                'independent_reference',
                'derived',
            )
        ):
            raise ValueError(
                f'a {self.assumption_kind} record cannot claim '
                f'{self.evidence_state} evidence — preference/commercial '
                'assumptions never masquerade as physics (#730 §4)'
            )
        if self.evidence_state == 'unknown' and self.value is not None:
            raise ValueError(
                'an unknown assumption asserts no value — the record '
                'documents the gap, not a nominal stand-in'
            )
        if self.evidence_state != 'unknown' and self.value is None:
            raise ValueError(
                'an assumption requires its value binding — quantity '
                'pin, range, or declared label'
            )
        if self.assumption_kind == 'other_declared' and not (
            self.source_label
        ):
            raise ValueError(
                'other_declared requires source_label — the custom '
                'kind is named by its source'
            )
        if self.scenario_group is not None and not self.scenario_label:
            raise ValueError(
                'a competing-hypothesis member requires scenario_label'
            )
        if self.evidence_state in ASSUMPTION_EVIDENCE_STATES - {
            'unknown'
        } and not self.permissible_uses:
            raise ValueError(
                'an assumption declares its permissible uses — a '
                'default valid-anywhere flag is exactly what this '
                'ledger exists to prevent (#730 §5)'
            )
        for label, ref in (
            ('uncertainty_ref', self.uncertainty_ref),
            ('decision_ref', self.decision_ref),
            ('supersedes_ref', self.supersedes_ref),
            *[
                (f'affected_result_refs[{i}]', r)
                for i, r in enumerate(self.affected_result_refs)
            ],
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must carry its sha256 pin')
        if (
            self.conservatism in ('worst_case_bound', 'best_case_bound')
            and self.evidence_state in (
                'measured_observed', 'manufacturer_declared',
            )
        ):
            raise ValueError(
                'a bound label never rides on measured/declared '
                'evidence — the bound is a scenario declaration'
            )
        expected = _hash(self.identity_payload())
        if self.assumption_sha256 != expected:
            raise ValueError('engineering assumption hash mismatch')
        if self.assumption_id != _semantic_id('asm', expected):
            raise ValueError(
                'engineering assumption id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'subject_ref': self.subject_ref.model_dump(mode='json'),
            'assumption_kind': self.assumption_kind,
            'value': (
                self.value.model_dump(mode='json')
                if self.value is not None
                else None
            ),
            'evidence_state': self.evidence_state,
            'conservatism': self.conservatism,
            'rationale': self.rationale,
            'source_label': self.source_label,
            'uncertainty_ref': (
                self.uncertainty_ref.model_dump(mode='json')
                if self.uncertainty_ref is not None
                else None
            ),
            'permissible_uses': list(self.permissible_uses),
            'sensitivity': self.sensitivity,
            'affected_result_refs': [
                r.model_dump(mode='json')
                for r in self.affected_result_refs
            ],
            'scenario_group': self.scenario_group,
            'scenario_label': self.scenario_label,
            'expiry_trigger': self.expiry_trigger,
            'resolution_needed': self.resolution_needed,
            'decision_ref': (
                self.decision_ref.model_dump(mode='json')
                if self.decision_ref is not None
                else None
            ),
            'supersedes_ref': (
                self.supersedes_ref.model_dump(mode='json')
                if self.supersedes_ref is not None
                else None
            ),
            'author': self.author,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def assumption_binding(
    assumption: CadEngineeringAssumption,
) -> AuthorityRef:
    return AuthorityRef(
        kind='engineering_assumption',
        ref_id=assumption.assumption_id,
        ref_sha256=assumption.assumption_sha256,
    )


class CadAssumptionResolution(BaseModel):
    """An append-only lifecycle event for one assumption (#730 §12/§13).

    Resolutions never edit the assumption: new evidence *replaces* or
    *contradicts* it, both records stay, and dependents stale via the
    assumption's affected_result_refs (compose #729).
    """

    model_config = ConfigDict(frozen=True)

    resolution_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    assumption_ref: AuthorityRef
    resolution_state: ResolutionState
    replacement_ref: AuthorityRef | None = None
    rationale: str = Field(min_length=1)
    resolved_at_utc: str = Field(min_length=1)
    resolution_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_resolution(self) -> 'CadAssumptionResolution':
        _require_iso8601(self.resolved_at_utc, 'resolved_at_utc')
        if self.assumption_ref.ref_sha256 is None:
            raise ValueError('assumption_ref must pin its sha256')
        if self.assumption_ref.kind != 'engineering_assumption':
            raise ValueError(
                "assumption_ref must pin an 'engineering_assumption'"
            )
        if self.replacement_ref is not None and (
            self.replacement_ref.ref_sha256 is None
        ):
            raise ValueError('replacement_ref must pin its sha256')
        if self.resolution_state in (
            'replaced_by_observation',
            'replaced_by_measurement',
            'replaced_by_approved_design_fact',
            'superseded',
        ) and self.replacement_ref is None:
            raise ValueError(
                f'{self.resolution_state} requires the replacement '
                'evidence ref — a resolution names what it resolved to'
            )
        if self.resolution_state == 'rejected_contradicted' and (
            self.replacement_ref is None
        ):
            raise ValueError(
                'a contradiction names the contradicting evidence — '
                'the record shows what killed the assumption'
            )
        expected = _hash(self.identity_payload())
        if self.resolution_sha256 != expected:
            raise ValueError('assumption resolution hash mismatch')
        if self.resolution_id != _semantic_id('asmres', expected):
            raise ValueError(
                'assumption resolution id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'assumption_ref': self.assumption_ref.model_dump(mode='json'),
            'resolution_state': self.resolution_state,
            'replacement_ref': (
                self.replacement_ref.model_dump(mode='json')
                if self.replacement_ref is not None
                else None
            ),
            'rationale': self.rationale,
            'resolved_at_utc': self.resolved_at_utc,
        }


def assumption_resolution_binding(
    resolution: CadAssumptionResolution,
) -> AuthorityRef:
    return AuthorityRef(
        kind='assumption_resolution',
        ref_id=resolution.resolution_id,
        ref_sha256=resolution.resolution_sha256,
    )


class CadPermissibleUseAssessment(BaseModel):
    """Sealed verdict of :func:`evaluate_permissible_use` (#730 §6)."""

    model_config = ConfigDict(frozen=True)

    assessment_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    intended_use: IntendedUse
    assumption_refs: tuple[AuthorityRef, ...]
    verdict: PermissibleVerdict
    limiting_assumption_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_version: str = Field(min_length=1)
    assessment_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_assessment(self) -> 'CadPermissibleUseAssessment':
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        for i, ref in enumerate(self.assumption_refs):
            if ref.ref_sha256 is None:
                raise ValueError(
                    f'assumption_refs[{i}] must carry its sha256 pin'
                )
        if self.verdict in (
            'permissible_with_limitations',
            'stronger_evidence_required',
            'incompatible',
        ) and not self.limiting_assumption_ids:
            raise ValueError(
                'a non-permissible verdict names its limiting '
                'assumptions — a bare gate is not a reason (#730 §6)'
            )
        expected = _hash(self.identity_payload())
        if self.assessment_sha256 != expected:
            raise ValueError('permissible-use assessment hash mismatch')
        if self.assessment_id != _semantic_id('asmpu', expected):
            raise ValueError(
                'permissible-use assessment id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'intended_use': self.intended_use,
            'assumption_refs': [
                r.model_dump(mode='json')
                for r in self.assumption_refs
            ],
            'verdict': self.verdict,
            'limiting_assumption_ids': list(
                self.limiting_assumption_ids
            ),
            'reasons': list(self.reasons),
            'evaluated_at_utc': self.evaluated_at_utc,
            'evaluation_version': self.evaluation_version,
        }


def permissible_use_binding(
    assessment: CadPermissibleUseAssessment,
) -> AuthorityRef:
    return AuthorityRef(
        kind='permissible_use_assessment',
        ref_id=assessment.assessment_id,
        ref_sha256=assessment.assessment_sha256,
    )


# ---------------------------------------------------------------------------
# Ledger projections + evaluation
# ---------------------------------------------------------------------------


class AssumptionLedgerIntegrityError(ValueError):
    """Persisted assumption lineage is corrupt (fork, cycle, dangling)."""


def assumption_lineage_issues(
    assumptions: tuple[CadEngineeringAssumption, ...],
) -> tuple[str, ...]:
    """Topology check on supersession edges — same rule as the #620
    decision lineage: single-head per subject chain, no forks/cycles."""
    issues: list[str] = []
    by_id = {a.assumption_id: a for a in assumptions}
    successors: dict[str, list[str]] = {}
    for item in assumptions:
        pred = (
            item.supersedes_ref.ref_id if item.supersedes_ref else None
        )
        if pred is None:
            continue
        successors.setdefault(pred, []).append(item.assumption_id)
        target = by_id.get(pred)
        if pred == item.assumption_id:
            issues.append(
                f'assumption {item.assumption_id} supersedes itself'
            )
        elif target is None:
            issues.append(
                f'assumption {item.assumption_id} supersedes missing '
                f'predecessor {pred}'
            )
        elif target.subject_ref != item.subject_ref:
            issues.append(
                f'assumption {item.assumption_id} supersedes an '
                'assumption on a different subject'
            )
    for pred, children in successors.items():
        if len(children) > 1:
            issues.append(
                f'assumption {pred} has multiple successors '
                f'({len(children)}): forked lineage'
            )
    for item in assumptions:
        seen: set[str] = set()
        cursor = item
        while cursor.supersedes_ref is not None:
            pred_id = cursor.supersedes_ref.ref_id
            if pred_id in seen:
                issues.append(
                    f'assumption {item.assumption_id} reaches a '
                    'supersession cycle'
                )
                break
            seen.add(pred_id)
            cursor = by_id.get(pred_id)
            if cursor is None:
                break
    return tuple(issues)


def active_assumptions(
    assumptions: tuple[CadEngineeringAssumption, ...],
    resolutions: tuple[CadAssumptionResolution, ...] = (),
) -> tuple[tuple[CadEngineeringAssumption, ...], tuple[str, ...]]:
    """Project the open ledger: drop superseded and terminally resolved
    records, report contradicted-but-retained ones as warnings.

    Returns ``(open_assumptions, inactive_ids)`` where inactive_ids are
    assumptions closed by resolution — retained for provenance, excluded
    from permissible-use gating.
    """
    issues = assumption_lineage_issues(assumptions)
    if issues:
        raise AssumptionLedgerIntegrityError(
            'assumption lineage is corrupt: ' + '; '.join(issues)
        )
    superseded = {
        a.supersedes_ref.ref_id
        for a in assumptions
        if a.supersedes_ref is not None
    }
    closed_states = {
        'replaced_by_observation',
        'replaced_by_measurement',
        'replaced_by_approved_design_fact',
        'rejected_contradicted',
    }
    closed = {
        r.assumption_ref.ref_id
        for r in resolutions
        if r.resolution_state in closed_states
    }
    inactive = superseded | closed
    return (
        tuple(
            a for a in assumptions
            if a.assumption_id not in inactive
        ),
        tuple(sorted(inactive)),
    )


def _assumption_verdict(
    assumption: CadEngineeringAssumption,
    intended_use: IntendedUse,
) -> tuple[PermissibleVerdict, str | None]:
    """One assumption's contribution to the gate.

    ``permissible_uses`` on the record is the primary contract; evidence
    strength bounds how far an undeclared use can stretch. The gate is
    fail-closed: an assumption silent on the requested use is limited by
    evidence class, never implicitly allowed.
    """
    use_strict = _USE_STRICTNESS[intended_use]
    if intended_use in assumption.permissible_uses:
        if assumption.sensitivity == 'high' and (
            use_strict >= _VALIDATION_GRADE_STRICTNESS
        ):
            return 'permissible_with_limitations', (
                'declared permissible but high-sensitivity — the '
                'verdict must carry the dependence visibly'
            )
        return 'permissible', None

    state = assumption.evidence_state
    if state == 'unknown':
        return 'stronger_evidence_required', (
            'the input is a declared unknown — nothing to bound the '
            'claim against'
        )
    if state == 'exploratory_scenario':
        if use_strict >= _VALIDATION_GRADE_STRICTNESS:
            return 'incompatible', (
                'an exploratory scenario cannot gate a '
                'validation/commissioning-grade claim'
            )
        return 'stronger_evidence_required', (
            'scenario-only evidence outside its declared use — the '
            'record is exploration, not support'
        )
    if state == 'calibrated_inverse_estimated':
        if use_strict >= _VALIDATION_GRADE_STRICTNESS:
            return 'stronger_evidence_required', (
                'an inverse-estimated parameter is not a measurement '
                '(#689) — a good fit does not close the gap'
            )
        return 'permissible_with_limitations', (
            'calibrated estimate outside declared uses — identifiability '
            'limits ride along'
        )
    # project_assumed / literature_assumed / default_assumed
    if use_strict >= _VALIDATION_GRADE_STRICTNESS:
        return 'stronger_evidence_required', (
            f'{state} evidence is below what {intended_use} demands'
        )
    if assumption.sensitivity == 'high':
        return 'stronger_evidence_required', (
            f'{state} evidence with high decision sensitivity — '
            'resolving it may change the answer'
        )
    return 'permissible_with_limitations', (
        f'{state} evidence stretched to {intended_use} — the use is '
        'not declared on the assumption'
    )


def evaluate_permissible_use(
    *,
    document_id: str,
    intended_use: IntendedUse,
    assumptions: tuple[CadEngineeringAssumption, ...]
    | list[CadEngineeringAssumption],
    evaluated_at_utc: str | None = None,
) -> CadPermissibleUseAssessment:
    """Sealed permissible-use verdict over the open assumption set
    (#730 §6).

    Only assumption-class evidence is gated; measured/declared/derived
    records are observations, not assumptions, and pass through. The
    verdict is the worst contribution; reasons name each limiting
    assumption.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    assumptions = tuple(assumptions)
    verdict: PermissibleVerdict = 'permissible'
    limiting: list[str] = []
    reasons: list[str] = []

    for assumption in assumptions:
        if assumption.evidence_state not in ASSUMPTION_EVIDENCE_STATES:
            continue
        contrib, reason = _assumption_verdict(assumption, intended_use)
        if reason is not None:
            reasons.append(
                f'{assumption.assumption_id}: {reason}'
            )
        if VERDICT_RANK[contrib] > VERDICT_RANK[verdict]:
            verdict = contrib
        if contrib != 'permissible':
            limiting.append(assumption.assumption_id)

    if not assumptions:
        verdict = 'permissible'
        reasons.append(
            'no assumption records gate this use — an empty ledger is '
            'only as honest as the registration discipline feeding it'
        )

    payload = dict(
        document_id=document_id,
        intended_use=intended_use,
        assumption_refs=tuple(
            assumption_binding(a) for a in assumptions
        ),
        verdict=verdict,
        limiting_assumption_ids=tuple(limiting),
        reasons=tuple(reasons),
        evaluated_at_utc=evaluated_at_utc,
        evaluation_version=ASSUMPTION_EVALUATION_VERSION,
    )
    return _seal(
        CadPermissibleUseAssessment, payload,
        'assessment_id', 'assessment_sha256', 'asmpu',
    )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_engineering_assumption(
    *,
    document_id: str,
    subject_ref: AuthorityRef,
    assumption_kind: AssumptionKind,
    evidence_state: EvidenceState,
    rationale: str,
    value: CadAssumptionValue | None = None,
    conservatism: ConservatismKind = 'undeclared',
    source_label: str = '',
    uncertainty_ref: AuthorityRef | None = None,
    permissible_uses: tuple[IntendedUse, ...] | list[IntendedUse] = (),
    sensitivity: SensitivityClass = 'not_evaluated',
    affected_result_refs: tuple[AuthorityRef, ...] | list[
        AuthorityRef
    ] = (),
    scenario_group: str | None = None,
    scenario_label: str | None = None,
    expiry_trigger: str = '',
    resolution_needed: str = '',
    decision_ref: AuthorityRef | None = None,
    supersedes_ref: AuthorityRef | None = None,
    author: str | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadEngineeringAssumption:
    """Seal one engineering assumption into the ledger."""
    payload = dict(
        document_id=document_id,
        subject_ref=subject_ref,
        assumption_kind=assumption_kind,
        value=value,
        evidence_state=evidence_state,
        conservatism=conservatism,
        rationale=rationale,
        source_label=source_label,
        uncertainty_ref=uncertainty_ref,
        permissible_uses=tuple(permissible_uses),
        sensitivity=sensitivity,
        affected_result_refs=tuple(affected_result_refs),
        scenario_group=scenario_group,
        scenario_label=scenario_label,
        expiry_trigger=expiry_trigger,
        resolution_needed=resolution_needed,
        decision_ref=decision_ref,
        supersedes_ref=supersedes_ref,
        author=author,
        authority_version=ASSUMPTION_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal(
        CadEngineeringAssumption, payload,
        'assumption_id', 'assumption_sha256', 'asm',
    )


def build_assumption_resolution(
    *,
    document_id: str,
    assumption_ref: AuthorityRef,
    resolution_state: ResolutionState,
    rationale: str,
    replacement_ref: AuthorityRef | None = None,
    resolved_at_utc: str | None = None,
) -> CadAssumptionResolution:
    """Seal an append-only lifecycle event for an assumption."""
    payload = dict(
        document_id=document_id,
        assumption_ref=assumption_ref,
        resolution_state=resolution_state,
        replacement_ref=replacement_ref,
        rationale=rationale,
        resolved_at_utc=resolved_at_utc or _utc_now(),
    )
    return _seal(
        CadAssumptionResolution, payload,
        'resolution_id', 'resolution_sha256', 'asmres',
    )


__all__ = [
    'ASSUMPTION_EVIDENCE_STATES',
    'ASSUMPTION_EVALUATION_VERSION',
    'ASSUMPTION_KIND_LABELS',
    'ASSUMPTION_SCHEMA_VERSION',
    'AssumptionKind',
    'AssumptionLedgerIntegrityError',
    'CadAssumptionResolution',
    'CadAssumptionValue',
    'CadEngineeringAssumption',
    'CadPermissibleUseAssessment',
    'ConservatismKind',
    'EVIDENCE_STATE_LABELS',
    'EvidenceState',
    'IntendedUse',
    'INTENDED_USE_LABELS',
    'PermissibleVerdict',
    'PERMISSIBLE_VERDICT_LABELS',
    'ResolutionState',
    'RESOLUTION_STATE_LABELS',
    'SensitivityClass',
    'VERDICT_RANK',
    'active_assumptions',
    'assumption_binding',
    'assumption_lineage_issues',
    'assumption_resolution_binding',
    'build_assumption_resolution',
    'build_engineering_assumption',
    'evaluate_permissible_use',
    'permissible_use_binding',
]
