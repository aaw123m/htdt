"""Residual diagnostic-hypothesis authority (#719, REV59-UNITS).

A structured residual is evidence that the model/system state is
incomplete or wrong — it is *not* by itself a root cause. "Crossover
notch → polarity error" is an inference pattern that needs competing
hypotheses, discrimination tests and controlled intervention evidence
before it may be claimed. This module seals the generic evidence-
escalation layer between a residual and a diagnostic claim.

- :class:`CadDiagnosticCase` — the sealed symptom: which residual/
  observation triggered the diagnostic, pinned to its registration/
  comparability evidence (#564) so diagnosis never outruns the
  measurement gates.
- :class:`CadDiagnosticHypothesis` — the sealed candidate explanation:
  cause family (spanning measurement/registration/device/physical/
  model-form layers — not just acoustic parameters), predicted
  observable signature, required evidence and known confounders. The
  hypothesis record is *content*, not status: state is derived from
  evidence, never asserted.
- :class:`CadDiagnosticTest` — the sealed discrimination test /
  controlled intervention: predeclared prediction ("if H, observable X
  changes by ~Y") recorded *before* execution, the post-intervention
  outcome per hypothesis, and the verdict class.
- :func:`evaluate_diagnostic_verdict` — the case verdict:
  ``root_cause_confirmed_within_declared_scope`` only when controlled
  intervention or independent remeasurement confirmed a hypothesis;
  ``diagnostically_confounded`` when candidates remain observationally
  equivalent (#689 composition); never a fabricated confidence
  percent.

Authority boundary:

- a residual fingerprint is evidence for hypothesis *generation*; the
  fingerprint→cause mapping requires discrimination evidence (#719 §4);
- ``model_form_inadequacy`` is a first-class cause family — a residual
  that survives every parameter fit may mean the model family is
  wrong, not that a parameter needs pushing to its bound (#719 §8);
- calibration fit quality is *not* diagnostic evidence (#719 §18) —
  a better fit does not prove the physical story;
- diagnosis is not authorization — a confirmed cause does not license
  a remedy (#719 §19, compose #519);
- ML/correlation assistance stays at hypothesis *generation/ranking*;
  causal claims need the evidence ladder (#719 §21).

Literature basis
----------------
- NIST/SEMATECH e-Handbook of Statistical Methods §4.4 — non-random
  residual structure means the model is inadequate/misspecified;
  residual analysis does not by itself identify one physical cause.
- NIST model-error work — prediction−measurement differences mix model
  error and experimental uncertainty; #572/#564 gates run first.
- Beaton & Xiang, JASA 141(6) 2017 — model selection (which
  explanation) and parameter estimation (inside one explanation) are
  distinct inference problems.
- Yang, Dong & Wu, JCP 545 (2026) 114469 — model discrepancy biases
  calibration/experiment design when the assumed model is incomplete;
  ``model_form_inadequacy`` stays a live competing explanation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


DIAGNOSTIC_SCHEMA_VERSION = 'diagnostic-hypothesis-1'
DIAGNOSTIC_EVALUATION_VERSION = 'diag-eval-1'

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
# Taxonomies (#719)
# ---------------------------------------------------------------------------

CauseFamily = Literal[
    'measurement_chain',
    'registration_coordinate',
    'timebase_clock',
    'device_configuration',
    'routing_polarity',
    'source_position_aim',
    'receiver_position',
    'geometry_asbuilt',
    'material_boundary',
    'source_model_directivity',
    'nonlinear_level_state',
    'environment_occupancy',
    'numerical_solver',
    'model_form_inadequacy',
    'multiple_faults',
    'unknown',
]
"""#719 §3 — cause families span every layer; acoustic-model parameters
are never the default first explanation. ``model_form_inadequacy`` is a
first-class member: a residual the physics cannot express is not a
parameter value."""

EvidenceLevel = Literal[
    'observed_symptom',
    'correlated_pattern',
    'model_supported_hypothesis',
    'discrimination_test_support',
    'controlled_intervention_support',
    'independent_remeasurement_confirmation',
    'root_cause_confirmed_within_declared_scope',
    'unresolved',
]
"""#719 §1 — the escalation ladder. A hypothesis can only climb through
recorded evidence; no score jumps symptom→confirmed."""

TestKind = Literal[
    'discrimination_measurement',
    'loopback_check',
    'isolation_measurement',
    'controlled_intervention',
    'independent_remeasurement',
    'other_declared',
]
"""#719 §11/§12 — discrimination tests gather evidence; a controlled
intervention changes exactly one cause-relevant factor under a declared
held-constant scope. ``other_declared`` carries its method label."""

HypothesisOutcome = Literal[
    'supported',
    'contradicted',
    'not_tested',
    'indistinguishable',
    'insufficient_evidence',
    'out_of_domain',
]
"""#719 §5 — per-hypothesis outcome of a test. ``indistinguishable`` is
first-class: when two causes produce equivalent observables the record
says so instead of picking the better optimizer score."""

InterventionVerdict = Literal[
    'confirmed_by_intervention_and_remeasurement',
    'supported_but_confounded',
    'partially_explains_residual',
    'contradicted',
    'no_material_effect',
    'incomparable_retest',
    'pending',
]
"""#719 §14 — the post-intervention verdict. ``incomparable_retest``
covers an after-measurement that does not match the before profile —
the evidence is preserved but not usable."""

DiagnosticVerdict = Literal[
    'root_cause_confirmed_within_declared_scope',
    'supported_but_confounded',
    'partially_explained',
    'unresolved',
    'diagnostically_confounded',
    'insufficient_evidence',
]
"""The case-level conclusion. ``root_cause_confirmed_within_declared_scope``
requires intervention/remeasurement evidence on at least one hypothesis —
a correlated pattern is never enough (#719 §1)."""

HypothesisDerivedState = Literal[
    'candidate',
    'model_supported',
    'test_supported',
    'confirmed',
    'contradicted',
    'confounded',
    'not_tested',
]
"""Per-hypothesis state derived in the verdict — computed from evidence,
never declared on the hypothesis record."""

CAUSE_FAMILY_LABELS: dict[str, str] = {
    'measurement_chain': '測定チェーン',
    'registration_coordinate': '位置合わせ・座標系',
    'timebase_clock': 'タイムベース・クロック',
    'device_configuration': '機器設定',
    'routing_polarity': '配線・極性',
    'source_position_aim': '音源位置・指向',
    'receiver_position': '受音位置',
    'geometry_asbuilt': '竣工幾何',
    'material_boundary': '材料・境界',
    'source_model_directivity': '音源モデル・指向性',
    'nonlinear_level_state': '非線形・レベル状態',
    'environment_occupancy': '環境・占有状態',
    'numerical_solver': '数値ソルバー',
    'model_form_inadequacy': 'モデル形式の不備',
    'multiple_faults': '複合故障',
    'unknown': '不明',
}

EVIDENCE_LEVEL_LABELS: dict[str, str] = {
    'observed_symptom': '観測された症状',
    'correlated_pattern': '相関パターン',
    'model_supported_hypothesis': 'モデル裏付け仮説',
    'discrimination_test_support': '識別試験の裏付け',
    'controlled_intervention_support': '管理介入の裏付け',
    'independent_remeasurement_confirmation': '独立再測定による確認',
    'root_cause_confirmed_within_declared_scope': '宣言範囲内で原因確認',
    'unresolved': '未解決',
}

DIAGNOSTIC_VERDICT_LABELS: dict[str, str] = {
    'root_cause_confirmed_within_declared_scope': '宣言範囲内で根本原因確認',
    'supported_but_confounded': '裏付けあり・交絡残存',
    'partially_explained': '一部説明',
    'unresolved': '未解決',
    'diagnostically_confounded': '診断上交絡',
    'insufficient_evidence': '証拠不足',
}

INTERVENTION_VERDICT_LABELS: dict[str, str] = {
    'confirmed_by_intervention_and_remeasurement': '介入+再測定で確認',
    'supported_but_confounded': '裏付けあり・交絡残存',
    'partially_explains_residual': '残差を一部説明',
    'contradicted': '反証済み',
    'no_material_effect': '有意な効果なし',
    'incomparable_retest': '再測定比較不能',
    'pending': '未実行',
}


# ---------------------------------------------------------------------------
# Embedded descriptors
# ---------------------------------------------------------------------------


class CadResidualSignature(BaseModel):
    """The residual/symptom fingerprint the hypothesis predicts (#719 §4).

    A signature is *evidence*, not a cause: it names what observable
    pattern the hypothesis would produce (e.g. 'crossover-region notch',
    'one-channel polarity-like cancellation'). The extraction algorithm
    identity is pinned so a fingerprint computed differently cannot be
    silently compared.
    """

    model_config = ConfigDict(frozen=True)

    signature_label: str = Field(min_length=1)
    observable_label: str = Field(min_length=1)
    extraction_method: str = Field(min_length=1)
    extraction_version: str = ''


class CadHypothesisOutcome(BaseModel):
    """One hypothesis's outcome inside a test record."""

    model_config = ConfigDict(frozen=True)

    hypothesis_ref: AuthorityRef
    outcome: HypothesisOutcome
    note: str = ''

    @model_validator(mode='after')
    def valid_outcome(self) -> 'CadHypothesisOutcome':
        if self.hypothesis_ref.ref_sha256 is None:
            raise ValueError(
                'hypothesis_ref must carry its sha256 pin'
            )
        if self.hypothesis_ref.kind != 'diagnostic_hypothesis':
            raise ValueError(
                "outcome must pin a 'diagnostic_hypothesis'"
            )
        return self


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


class CadDiagnosticCase(BaseModel):
    """A sealed diagnostic case bound to its triggering symptom (#719).

    The symptom pin points at the residual report / observation that
    triggered diagnosis — the measurement/registration/comparability
    gates (#572/#564/#573) run there first; this layer never diagnoses
    an unregistered residual.
    """

    model_config = ConfigDict(frozen=True)

    case_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    symptom_ref: AuthorityRef
    symptom_summary: str = Field(min_length=1)
    status: Literal[
        'open', 'resolved', 'unresolved', 'confounded'
    ] = 'open'
    opened_at_utc: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    provenance_json: str = '{}'
    case_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_case(self) -> 'CadDiagnosticCase':
        _require_iso8601(self.opened_at_utc, 'opened_at_utc')
        if self.symptom_ref.ref_sha256 is None:
            raise ValueError('symptom_ref must pin its sha256')
        expected = _hash(self.identity_payload())
        if self.case_sha256 != expected:
            raise ValueError('diagnostic case hash mismatch')
        if self.case_id != _semantic_id('diagcase', expected):
            raise ValueError(
                'diagnostic case id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'symptom_ref': self.symptom_ref.model_dump(mode='json'),
            'symptom_summary': self.symptom_summary,
            'status': self.status,
            'opened_at_utc': self.opened_at_utc,
            'authority_version': self.authority_version,
            'provenance_json': self.provenance_json,
        }


def diagnostic_case_binding(case: CadDiagnosticCase) -> AuthorityRef:
    return AuthorityRef(
        kind='diagnostic_case',
        ref_id=case.case_id,
        ref_sha256=case.case_sha256,
    )


class CadDiagnosticHypothesis(BaseModel):
    """A sealed candidate explanation (#719 §2).

    Declares the cause family, the expected observables/residual
    signature, the evidence the hypothesis requires and its known
    confounders. The record carries NO status — a hypothesis's support
    level is derived from tests and verdicts, and the content hash pins
    exactly what was hypothesized.
    """

    model_config = ConfigDict(frozen=True)

    hypothesis_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    case_ref: AuthorityRef
    cause_family: CauseFamily
    hypothesis_label: str = Field(min_length=1)
    affected_entity_refs: tuple[AuthorityRef, ...] = ()
    expected_signatures: tuple[CadResidualSignature, ...]
    required_evidence: str = Field(min_length=1)
    known_confounders: tuple[str, ...] = ()
    counterfactual_ref: AuthorityRef | None = None
    domain_scope: str = ''
    source_label: str = ''
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    provenance_json: str = '{}'
    hypothesis_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_hypothesis(self) -> 'CadDiagnosticHypothesis':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        if self.case_ref.ref_sha256 is None:
            raise ValueError('case_ref must pin its sha256')
        if self.case_ref.kind != 'diagnostic_case':
            raise ValueError(
                "case_ref must pin a 'diagnostic_case'"
            )
        if not self.expected_signatures:
            raise ValueError(
                'a hypothesis declares its predicted observables — a '
                'signature-less hypothesis cannot be discriminated '
                '(#719 §2)'
            )
        for i, ref in enumerate(self.affected_entity_refs):
            if ref.ref_sha256 is None:
                raise ValueError(
                    f'affected_entity_refs[{i}] must carry its sha256 '
                    'pin'
                )
        if self.counterfactual_ref is not None and (
            self.counterfactual_ref.ref_sha256 is None
        ):
            raise ValueError(
                'counterfactual_ref must carry its sha256 pin'
            )
        expected = _hash(self.identity_payload())
        if self.hypothesis_sha256 != expected:
            raise ValueError('diagnostic hypothesis hash mismatch')
        if self.hypothesis_id != _semantic_id('diahyp', expected):
            raise ValueError(
                'diagnostic hypothesis id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'case_ref': self.case_ref.model_dump(mode='json'),
            'cause_family': self.cause_family,
            'hypothesis_label': self.hypothesis_label,
            'affected_entity_refs': [
                r.model_dump(mode='json')
                for r in self.affected_entity_refs
            ],
            'expected_signatures': [
                s.model_dump(mode='json')
                for s in self.expected_signatures
            ],
            'required_evidence': self.required_evidence,
            'known_confounders': list(self.known_confounders),
            'counterfactual_ref': (
                self.counterfactual_ref.model_dump(mode='json')
                if self.counterfactual_ref is not None
                else None
            ),
            'domain_scope': self.domain_scope,
            'source_label': self.source_label,
            'declared_at_utc': self.declared_at_utc,
            'authority_version': self.authority_version,
            'provenance_json': self.provenance_json,
        }


def diagnostic_hypothesis_binding(
    hypothesis: CadDiagnosticHypothesis,
) -> AuthorityRef:
    return AuthorityRef(
        kind='diagnostic_hypothesis',
        ref_id=hypothesis.hypothesis_id,
        ref_sha256=hypothesis.hypothesis_sha256,
    )


class CadDiagnosticTest(BaseModel):
    """A sealed discrimination test / controlled intervention
    (#719 §11–§14).

    - ``predeclared_prediction`` records, *before* execution, what the
      hypothesis predicts the observable will do — a post-fix
      improvement matching a predeclared prediction is stronger
      evidence than a story told after the fact.
    - ``held_constant_label`` names what the intervention kept fixed —
      a diagnostic state is a temporary controlled state, never a
      silent as-built mutation.
    - ``outcomes`` gives each discriminated hypothesis its own verdict;
      ``indistinguishable`` is a legal, honest result.
    """

    model_config = ConfigDict(frozen=True)

    test_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    case_ref: AuthorityRef
    test_kind: TestKind
    test_label: str = Field(min_length=1)
    predeclared_prediction: str = ''
    intervention_label: str = ''
    held_constant_label: str = ''
    result_evidence_ref: AuthorityRef | None = None
    outcomes: tuple[CadHypothesisOutcome, ...] = ()
    verdict: InterventionVerdict = 'pending'
    executed_at_utc: str | None = None
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    provenance_json: str = '{}'
    test_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_test(self) -> 'CadDiagnosticTest':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        if self.executed_at_utc is not None:
            _require_iso8601(self.executed_at_utc, 'executed_at_utc')
        if self.case_ref.ref_sha256 is None:
            raise ValueError('case_ref must pin its sha256')
        if self.case_ref.kind != 'diagnostic_case':
            raise ValueError("case_ref must pin a 'diagnostic_case'")
        if self.test_kind == 'controlled_intervention':
            if not self.intervention_label:
                raise ValueError(
                    'a controlled intervention records exactly what '
                    'changed — an unlabeled intervention is not '
                    'controlled (#719 §12)'
                )
            if not self.held_constant_label:
                raise ValueError(
                    'a controlled intervention declares what was held '
                    'constant — otherwise the result cannot isolate '
                    'the changed factor'
                )
        if self.verdict == 'confirmed_by_intervention_and_remeasurement':
            if not self.predeclared_prediction:
                raise ValueError(
                    'confirmation requires the predeclared prediction '
                    '— a match announced after seeing the result is '
                    'weaker evidence (#719 §13)'
                )
            if self.executed_at_utc is None:
                raise ValueError(
                    'a confirmed verdict requires executed_at_utc'
                )
            if self.result_evidence_ref is None:
                raise ValueError(
                    'a confirmed verdict pins the post-intervention '
                    'remeasurement evidence'
                )
        if self.verdict != 'pending' and self.executed_at_utc is None:
            raise ValueError(
                'a non-pending verdict requires executed_at_utc'
            )
        if self.result_evidence_ref is not None and (
            self.result_evidence_ref.ref_sha256 is None
        ):
            raise ValueError(
                'result_evidence_ref must carry its sha256 pin'
            )
        expected = _hash(self.identity_payload())
        if self.test_sha256 != expected:
            raise ValueError('diagnostic test hash mismatch')
        if self.test_id != _semantic_id('diatest', expected):
            raise ValueError(
                'diagnostic test id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'case_ref': self.case_ref.model_dump(mode='json'),
            'test_kind': self.test_kind,
            'test_label': self.test_label,
            'predeclared_prediction': self.predeclared_prediction,
            'intervention_label': self.intervention_label,
            'held_constant_label': self.held_constant_label,
            'result_evidence_ref': (
                self.result_evidence_ref.model_dump(mode='json')
                if self.result_evidence_ref is not None
                else None
            ),
            'outcomes': [
                o.model_dump(mode='json') for o in self.outcomes
            ],
            'verdict': self.verdict,
            'executed_at_utc': self.executed_at_utc,
            'declared_at_utc': self.declared_at_utc,
            'authority_version': self.authority_version,
            'provenance_json': self.provenance_json,
        }


def diagnostic_test_binding(test: CadDiagnosticTest) -> AuthorityRef:
    return AuthorityRef(
        kind='diagnostic_test',
        ref_id=test.test_id,
        ref_sha256=test.test_sha256,
    )


class CadHypothesisStateRecord(BaseModel):
    """The derived per-hypothesis state inside a case verdict."""

    model_config = ConfigDict(frozen=True)

    hypothesis_ref: AuthorityRef
    derived_state: HypothesisDerivedState
    evidence_level: EvidenceLevel
    reasons: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_state(self) -> 'CadHypothesisStateRecord':
        if self.hypothesis_ref.ref_sha256 is None:
            raise ValueError(
                'hypothesis_ref must carry its sha256 pin'
            )
        return self


class CadDiagnosticVerdict(BaseModel):
    """Sealed case verdict of :func:`evaluate_diagnostic_verdict` (#719).

    Names which hypotheses were confirmed (multi-fault legal), which
    remain confounded, and the reasons — evidence state plus explicit
    names, never an opaque percent (#719 §20).
    """

    model_config = ConfigDict(frozen=True)

    verdict_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    case_ref: AuthorityRef
    verdict: DiagnosticVerdict
    hypothesis_states: tuple[CadHypothesisStateRecord, ...]
    confirmed_hypothesis_ids: tuple[str, ...] = ()
    confounded_hypothesis_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_version: str = Field(min_length=1)
    verdict_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_verdict(self) -> 'CadDiagnosticVerdict':
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        if self.case_ref.ref_sha256 is None:
            raise ValueError('case_ref must pin its sha256')
        if self.case_ref.kind != 'diagnostic_case':
            raise ValueError("case_ref must pin a 'diagnostic_case'")
        if self.verdict == 'root_cause_confirmed_within_declared_scope' and (
            not self.confirmed_hypothesis_ids
        ):
            raise ValueError(
                'a confirmed verdict names the confirmed hypotheses'
            )
        if self.verdict == 'diagnostically_confounded' and (
            len(self.confounded_hypothesis_ids) < 2
        ):
            raise ValueError(
                'confounded requires at least two equivalent '
                'candidates — a single unresolvable hypothesis is '
                'unresolved, not confounded'
            )
        expected = _hash(self.identity_payload())
        if self.verdict_sha256 != expected:
            raise ValueError('diagnostic verdict hash mismatch')
        if self.verdict_id != _semantic_id('diaverdict', expected):
            raise ValueError(
                'diagnostic verdict id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'case_ref': self.case_ref.model_dump(mode='json'),
            'verdict': self.verdict,
            'hypothesis_states': [
                s.model_dump(mode='json')
                for s in self.hypothesis_states
            ],
            'confirmed_hypothesis_ids': list(
                self.confirmed_hypothesis_ids
            ),
            'confounded_hypothesis_ids': list(
                self.confounded_hypothesis_ids
            ),
            'reasons': list(self.reasons),
            'evaluated_at_utc': self.evaluated_at_utc,
            'evaluation_version': self.evaluation_version,
        }


def diagnostic_verdict_binding(
    verdict: CadDiagnosticVerdict,
) -> AuthorityRef:
    return AuthorityRef(
        kind='diagnostic_verdict',
        ref_id=verdict.verdict_id,
        ref_sha256=verdict.verdict_sha256,
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _derive_hypothesis_state(
    hypothesis: CadDiagnosticHypothesis,
    tests: tuple[CadDiagnosticTest, ...],
) -> CadHypothesisStateRecord:
    """Compute one hypothesis's evidence-derived state (#719 §1/§5).

    Ladder: contradicted > confirmed (intervention+remeasurement) >
    test_supported (discrimination/isolation support) >
    model_supported (declared counterfactual) > not_tested/candidate.
    """
    ref = diagnostic_hypothesis_binding(hypothesis)
    reasons: list[str] = []
    outcomes: list[CadHypothesisOutcome] = []
    confirming = False
    confounded = False
    for test in tests:
        for outcome in test.outcomes:
            if outcome.hypothesis_ref.ref_id != ref.ref_id:
                continue
            outcomes.append(outcome)
            if outcome.outcome == 'contradicted':
                reasons.append(
                    f'{test.test_label}: contradicted'
                    + (f' — {outcome.note}' if outcome.note else '')
                )
            elif outcome.outcome == 'indistinguishable':
                confounded = True
                reasons.append(
                    f'{test.test_label}: indistinguishable from '
                    'competing candidates under this campaign'
                )
            elif outcome.outcome == 'supported':
                if test.test_kind in (
                    'controlled_intervention',
                    'independent_remeasurement',
                    # Loopback/isolation are direct tests of chain or
                    # single-element faults — a confirmed verdict on
                    # one is confirmation-grade evidence for the
                    # chain/isolation hypothesis it supports.
                    'loopback_check',
                    'isolation_measurement',
                ) and (
                    test.verdict
                    == 'confirmed_by_intervention_and_remeasurement'
                ):
                    confirming = True
                    reasons.append(
                        f'{test.test_label}: predeclared prediction '
                        'matched by intervention + remeasurement'
                    )
                else:
                    reasons.append(
                        f'{test.test_label}: supported by '
                        f'{test.test_kind}'
                    )

    if any(o.outcome == 'contradicted' for o in outcomes):
        return CadHypothesisStateRecord(
            hypothesis_ref=ref,
            derived_state='contradicted',
            evidence_level='unresolved',
            reasons=tuple(reasons),
        )
    if confirming:
        return CadHypothesisStateRecord(
            hypothesis_ref=ref,
            derived_state='confirmed',
            evidence_level='controlled_intervention_support',
            reasons=tuple(reasons),
        )
    if confounded:
        return CadHypothesisStateRecord(
            hypothesis_ref=ref,
            derived_state='confounded',
            evidence_level='correlated_pattern',
            reasons=tuple(reasons),
        )
    if any(o.outcome == 'supported' for o in outcomes):
        return CadHypothesisStateRecord(
            hypothesis_ref=ref,
            derived_state='test_supported',
            evidence_level='discrimination_test_support',
            reasons=tuple(reasons),
        )
    if hypothesis.counterfactual_ref is not None:
        reasons.append(
            'declared counterfactual simulation supports the '
            'predicted signature — model support, not confirmation'
        )
        return CadHypothesisStateRecord(
            hypothesis_ref=ref,
            derived_state='model_supported',
            evidence_level='model_supported_hypothesis',
            reasons=tuple(reasons),
        )
    if outcomes:
        return CadHypothesisStateRecord(
            hypothesis_ref=ref,
            derived_state='not_tested',
            evidence_level='observed_symptom',
            reasons=tuple(
                reasons
                or ['test outcomes recorded no support']
            ),
        )
    return CadHypothesisStateRecord(
        hypothesis_ref=ref,
        derived_state='candidate',
        evidence_level='observed_symptom',
        reasons=('hypothesis declared but untested',),
    )


def evaluate_diagnostic_verdict(
    *,
    document_id: str,
    case: CadDiagnosticCase,
    hypotheses: tuple[CadDiagnosticHypothesis, ...]
    | list[CadDiagnosticHypothesis],
    tests: tuple[CadDiagnosticTest, ...]
    | list[CadDiagnosticTest] = (),
    evaluated_at_utc: str | None = None,
) -> CadDiagnosticVerdict:
    """Sealed case-level diagnostic verdict (#719 §1/§15/§20).

    - No hypotheses → ``insufficient_evidence``.
    - Every hypothesis contradicted → ``unresolved``.
    - ≥1 hypothesis confirmed by intervention+remeasurement →
      ``root_cause_confirmed_within_declared_scope`` (all confirmed
      hypotheses named — multi-fault is legal).
    - Supported-but-unconfirmed hypotheses plus at least one more
      hypothesis neither tested nor contradicted →
      ``diagnostically_confounded`` — the record refuses an arbitrary
      winner when the campaign cannot distinguish candidates (#719 §6).
    - Otherwise ``supported_but_confounded`` (some test support, no
      confirmation) or ``partially_explained``/``unresolved``.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    hypotheses = tuple(hypotheses)
    tests = tuple(tests)
    reasons: list[str] = []

    if not hypotheses:
        payload = dict(
            document_id=document_id,
            case_ref=diagnostic_case_binding(case),
            verdict='insufficient_evidence',
            hypothesis_states=(),
            confirmed_hypothesis_ids=(),
            confounded_hypothesis_ids=(),
            reasons=(
                'no hypotheses registered — a residual pattern alone '
                'is not a diagnosis (#719 §1)'
            ),
            evaluated_at_utc=evaluated_at_utc,
            evaluation_version=DIAGNOSTIC_EVALUATION_VERSION,
        )
        return _seal(
            CadDiagnosticVerdict, payload,
            'verdict_id', 'verdict_sha256', 'diaverdict',
        )

    states = tuple(
        _derive_hypothesis_state(h, tests) for h in hypotheses
    )
    confirmed = [
        s.hypothesis_ref.ref_id
        for s in states if s.derived_state == 'confirmed'
    ]
    contradicted = [
        s for s in states if s.derived_state == 'contradicted'
    ]
    supported = [
        s for s in states
        if s.derived_state in ('test_supported', 'model_supported')
    ]
    confounded_states = [
        s for s in states if s.derived_state == 'confounded'
    ]
    undecided = [
        s for s in states
        if s.derived_state in ('candidate', 'not_tested')
    ]

    if confirmed:
        verdict: DiagnosticVerdict = (
            'root_cause_confirmed_within_declared_scope'
        )
        reasons.append(
            f'{len(confirmed)} hypothesis(es) confirmed by controlled '
            'intervention + remeasurement or independent '
            'remeasurement — within the declared scope only'
        )
        if len(confirmed) > 1:
            reasons.append(
                'multi-fault explanation retained — the record does '
                'not force a single winner (#719 §15)'
            )
    elif len(states) == len(contradicted):
        verdict = 'unresolved'
        reasons.append(
            'every registered hypothesis was contradicted — the '
            'residual needs new candidates, not a best-of-bad '
            'selection'
        )
    elif (
        len(confounded_states) >= 2
        or (supported and (confounded_states or undecided))
    ):
        verdict = 'diagnostically_confounded'
        confounded_ids = [
            s.hypothesis_ref.ref_id
            for s in (*confounded_states, *supported, *undecided)
        ]
        reasons.append(
            'competing explanations remain observationally equivalent '
            'under the current campaign — compose #689; the next test '
            'must discriminate, not repeat (#719 §11)'
        )
        payload = dict(
            document_id=document_id,
            case_ref=diagnostic_case_binding(case),
            verdict=verdict,
            hypothesis_states=states,
            confirmed_hypothesis_ids=(),
            confounded_hypothesis_ids=tuple(confounded_ids),
            reasons=tuple(reasons),
            evaluated_at_utc=evaluated_at_utc,
            evaluation_version=DIAGNOSTIC_EVALUATION_VERSION,
        )
        return _seal(
            CadDiagnosticVerdict, payload,
            'verdict_id', 'verdict_sha256', 'diaverdict',
        )
    elif supported:
        verdict = 'supported_but_confounded'
        reasons.append(
            'some hypotheses carry model/test support but no '
            'controlled-intervention confirmation — support is not '
            'root cause'
        )
    elif contradicted:
        verdict = 'partially_explained'
        reasons.append(
            'some hypotheses were contradicted; the surviving set '
            'carries no discriminating evidence yet'
        )
    else:
        verdict = 'unresolved'
        reasons.append(
            'hypotheses exist but no test evidence discriminates them'
        )

    payload = dict(
        document_id=document_id,
        case_ref=diagnostic_case_binding(case),
        verdict=verdict,
        hypothesis_states=states,
        confirmed_hypothesis_ids=tuple(confirmed),
        confounded_hypothesis_ids=tuple(
            s.hypothesis_ref.ref_id for s in confounded_states
        ),
        reasons=tuple(reasons),
        evaluated_at_utc=evaluated_at_utc,
        evaluation_version=DIAGNOSTIC_EVALUATION_VERSION,
    )
    return _seal(
        CadDiagnosticVerdict, payload,
        'verdict_id', 'verdict_sha256', 'diaverdict',
    )


def diagnostic_claim_allowed(
    hypothesis: CadDiagnosticHypothesis,
    tests: tuple[CadDiagnosticTest, ...] | list[CadDiagnosticTest],
) -> tuple[bool, str]:
    """Claim gate (#719 §1): whether this hypothesis may be asserted as
    cause in an output claim.

    Only ``confirmed`` (controlled intervention + remeasurement, or
    independent remeasurement) hypotheses may be claimed as root cause;
    everything else returns the evidence level it honestly reached.
    """
    state = _derive_hypothesis_state(hypothesis, tuple(tests))
    if state.derived_state == 'confirmed':
        return True, state.evidence_level
    return False, state.evidence_level


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_diagnostic_case(
    *,
    document_id: str,
    symptom_ref: AuthorityRef,
    symptom_summary: str,
    opened_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadDiagnosticCase:
    payload = dict(
        document_id=document_id,
        symptom_ref=symptom_ref,
        symptom_summary=symptom_summary,
        status='open',
        opened_at_utc=opened_at_utc or _utc_now(),
        authority_version=DIAGNOSTIC_SCHEMA_VERSION,
        provenance_json=provenance_json,
    )
    return _seal(
        CadDiagnosticCase, payload,
        'case_id', 'case_sha256', 'diagcase',
    )


def build_diagnostic_hypothesis(
    *,
    document_id: str,
    case_ref: AuthorityRef,
    cause_family: CauseFamily,
    hypothesis_label: str,
    expected_signatures: tuple[CadResidualSignature, ...]
    | list[CadResidualSignature],
    required_evidence: str,
    affected_entity_refs: tuple[AuthorityRef, ...]
    | list[AuthorityRef] = (),
    known_confounders: tuple[str, ...] | list[str] = (),
    counterfactual_ref: AuthorityRef | None = None,
    domain_scope: str = '',
    source_label: str = '',
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadDiagnosticHypothesis:
    payload = dict(
        document_id=document_id,
        case_ref=case_ref,
        cause_family=cause_family,
        hypothesis_label=hypothesis_label,
        affected_entity_refs=tuple(affected_entity_refs),
        expected_signatures=tuple(expected_signatures),
        required_evidence=required_evidence,
        known_confounders=tuple(known_confounders),
        counterfactual_ref=counterfactual_ref,
        domain_scope=domain_scope,
        source_label=source_label,
        declared_at_utc=declared_at_utc or _utc_now(),
        authority_version=DIAGNOSTIC_SCHEMA_VERSION,
        provenance_json=provenance_json,
    )
    return _seal(
        CadDiagnosticHypothesis, payload,
        'hypothesis_id', 'hypothesis_sha256', 'diahyp',
    )


def build_diagnostic_test(
    *,
    document_id: str,
    case_ref: AuthorityRef,
    test_kind: TestKind,
    test_label: str,
    predeclared_prediction: str = '',
    intervention_label: str = '',
    held_constant_label: str = '',
    result_evidence_ref: AuthorityRef | None = None,
    outcomes: tuple[CadHypothesisOutcome, ...]
    | list[CadHypothesisOutcome] = (),
    verdict: InterventionVerdict = 'pending',
    executed_at_utc: str | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadDiagnosticTest:
    payload = dict(
        document_id=document_id,
        case_ref=case_ref,
        test_kind=test_kind,
        test_label=test_label,
        predeclared_prediction=predeclared_prediction,
        intervention_label=intervention_label,
        held_constant_label=held_constant_label,
        result_evidence_ref=result_evidence_ref,
        outcomes=tuple(outcomes),
        verdict=verdict,
        executed_at_utc=executed_at_utc,
        declared_at_utc=declared_at_utc or _utc_now(),
        authority_version=DIAGNOSTIC_SCHEMA_VERSION,
        provenance_json=provenance_json,
    )
    return _seal(
        CadDiagnosticTest, payload,
        'test_id', 'test_sha256', 'diatest',
    )


__all__ = [
    'CAUSE_FAMILY_LABELS',
    'CauseFamily',
    'CadDiagnosticCase',
    'CadDiagnosticHypothesis',
    'CadDiagnosticTest',
    'CadDiagnosticVerdict',
    'CadHypothesisOutcome',
    'CadHypothesisStateRecord',
    'CadResidualSignature',
    'DIAGNOSTIC_EVALUATION_VERSION',
    'DIAGNOSTIC_SCHEMA_VERSION',
    'DIAGNOSTIC_VERDICT_LABELS',
    'DiagnosticVerdict',
    'EVIDENCE_LEVEL_LABELS',
    'EvidenceLevel',
    'HypothesisDerivedState',
    'HypothesisOutcome',
    'INTERVENTION_VERDICT_LABELS',
    'InterventionVerdict',
    'TestKind',
    'build_diagnostic_case',
    'build_diagnostic_hypothesis',
    'build_diagnostic_test',
    'diagnostic_case_binding',
    'diagnostic_claim_allowed',
    'diagnostic_hypothesis_binding',
    'diagnostic_test_binding',
    'diagnostic_verdict_binding',
    'evaluate_diagnostic_verdict',
]
