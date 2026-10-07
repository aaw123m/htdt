"""Applicability/confidence envelope composition for UI and reports (#814).

The upstream authorities are now sealed stores: solver capability
manifests (#580 family), accuracy envelopes (``acoustic_validation_envelope``),
uncertainty-aware validation verdicts/evaluations/protocols (#810),
input-authority bounds (#811), and external benchmark qualification (#809).
What was missing — and what this module adds — is the *composition*: a
read-only layer that pulls those sealed records together for one
solver path/document and renders an applicability envelope, dimension by
dimension.

The vocabulary of the issue is preserved deliberately:

* an applicability envelope is seven *separate* dimensions —
  representational capability, verification, external validation, input
  qualification, owned-room evidence, uncertainty/limitations, and
  context of use — each with its own state, band, evidence refs and
  caveats. They are never collapsed into one number, one color, or one
  universal confidence score.

* absent evidence renders as ``absent`` (未取得 / UNKNOWN), never as a
  green or passed state. ``unsupported``, ``failed``,
  ``outside_applicability`` and ``insufficient_evidence`` stay distinct —
  the UI must not collapse them into one icon.

* capability is declaration, never validation: a manifest ``SUPPORTED``
  row attains at most the ``supported`` evidence class here, and the
  label makes explicit that a declared capability is not a validated one.

* ranking is a separate axis from absolute accuracy: a ranking verdict
  only gates the compare-candidates decision; it never upgrades an
  absolute-validation dimension.

* staleness is declared, not inferred: when a sealed record binds a
  provider/model/scene identity that does not match the current context,
  the dimension reports ``stale`` instead of silently reusing evidence.

Composition is fail-closed end to end. Every state is derived strictly
from stored sealed records whose shas are re-validated on load; nothing
is inferred from sibling evidence, and nothing is minted on read.

Usage::

    bundle = load_envelope_evidence(scene_repository, document_id=doc_id)
    envelope = compose_applicability_envelope(
        bundle,
        context=EnvelopeContext(...),
        solver_envelopes=accuracy_envelopes,
        evaluated_at_utc='...',
    )
    report = build_envelope_report(envelope, generated_at_utc='...')
"""

from __future__ import annotations

from contextlib import closing
from typing import Any, Iterable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .acoustic_validation_envelope import AccuracyEnvelopeRecord
from .cad_authority_resolver import AuthorityRef
from .cad_benchmark_qualification import (
    BenchmarkPreregistration,
    BenchmarkQualification,
    BenchmarkSceneMapping,
)
from .cad_benchmark_qualification_repository import (
    CadBenchmarkQualificationRepository,
)
from .cad_equipment import FrequencyDomain
from .cad_solver_capability_manifest import (
    SOLVER_PATH_PHENOMENA,
    SolverCapabilityManifest,
    SolverPathPhenomenon,
)
from .cad_solver_confidence_bound import (
    _CLAIM_REQUIREMENTS,
    CONFIDENCE_BOUND_LABELS,
    BoundVerdict,
    ClaimBoundRecord,
    ClaimClass,
    SolverInputEnvelope,
)
from .cad_solver_confidence_bound_repository import (
    CadSolverConfidenceBoundRepository,
)
from .cad_validation_uncertainty import (
    VUQ_LABELS,
    ObservableUncertaintyEvaluation,
    UncertaintyValidationVerdict,
    ValidationUncertaintyProtocol,
)
from .cad_validation_uncertainty_repository import (
    CadValidationUncertaintyRepository,
)
from .cad_schema import (
    connect_sqlite,
    ensure_native_schema,
    require_native_tables,
)
from .cad_repository import SceneRepository
from .canonical_json import canonical_sha256 as _hash


_SHA256_PATTERN = r'^[0-9a-f]{64}$'

APPLICABILITY_ENVELOPE_VERSION = 'applicability-envelope-1'
APPLICABILITY_ENVELOPE_COMPOSER_VERSION = 'applicability-envelope-composer-1'
APPLICABILITY_ENVELOPE_REPORT_VERSION = 'applicability-envelope-report-1'


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

EnvelopeDimensionId = Literal[
    'representational_capability',
    'verification',
    'external_validation',
    'input_qualification',
    'owned_room_evidence',
    'uncertainty_limitations',
    'context_of_use',
]

#: The seven issue-required dimensions, in fixed render order.
ENVELOPE_DIMENSIONS: tuple[EnvelopeDimensionId, ...] = (
    'representational_capability',
    'verification',
    'external_validation',
    'input_qualification',
    'owned_room_evidence',
    'uncertainty_limitations',
    'context_of_use',
)

EnvelopeEvidenceClass = Literal[
    'externally_validated',
    'numerically_verified',
    'holdout_validated',
    'qualified',
    'bounded',
    'supported',
    'insufficient_evidence',
    'stale',
    'failed',
    'outside_applicability',
    'unsupported',
    'absent',
]

#: Positive attainments, strongest first. ``supported`` is a declaration,
#: not a validation — it stays below every verified/validated class.
_POSITIVE_PRECEDENCE: tuple[EnvelopeEvidenceClass, ...] = (
    'externally_validated',
    'numerically_verified',
    'holdout_validated',
    'qualified',
    'bounded',
    'supported',
)

#: Absence/limitation classes, most informative first. The ordering keeps
#: FAIL truth ahead of softer absences so a mixed evidence base never
#: hides a failure behind an absence.
_NEGATIVE_PRECEDENCE: tuple[EnvelopeEvidenceClass, ...] = (
    'failed',
    'outside_applicability',
    'unsupported',
    'insufficient_evidence',
    'stale',
    'absent',
)

EnvelopeDecision = Literal[
    'inspect_prediction',
    'compare_candidates',
    'automatic_recommendation',
]

ENVELOPE_DECISIONS: tuple[EnvelopeDecision, ...] = (
    'inspect_prediction',
    'compare_candidates',
    'automatic_recommendation',
)

EnvelopeDecisionVerdict = Literal[
    'allowed',
    'trend_only',
    'blocked',
    'not_supported',
]


# ---------------------------------------------------------------------------
# Japanese labels (product vocabulary for UI + report export)
# ---------------------------------------------------------------------------

ENVELOPE_DIMENSION_LABELS: dict[str, str] = {
    'representational_capability': '表現能力（ソルバーが現象を表せるか）',
    'verification': '検証（実装・数値解の検証）',
    'external_validation': '外部検証（独立ベンチマーク）',
    'input_qualification': '入力適格性',
    'owned_room_evidence': '実室証拠（ホールドアウト）',
    'uncertainty_limitations': '不確かさ・制約',
    'context_of_use': '用途コンテキスト（許可される判断）',
}

ENVELOPE_CLASS_LABELS: dict[str, str] = {
    'externally_validated': '外部実測検証済み',
    'numerically_verified': '数値検証済み',
    'holdout_validated': '実室ホールドアウト検証済み',
    'qualified': '適格（宣言された上限内）',
    'supported': 'サポート宣言あり（検証ではない）',
    'bounded': '制約付き',
    'insufficient_evidence': '証拠不足',
    'stale': '陳腐化',
    'failed': '失敗（基準未達）',
    'outside_applicability': '適用域外',
    'unsupported': '未サポート',
    'absent': '未取得',
}

QUALIFICATION_VERDICT_LABELS: dict[str, str] = {
    'PASS_WITHIN_DOMAIN': '宣言領域内パス',
    'FAIL': '失敗',
    'INSUFFICIENT_REFERENCE_QUALITY': '参照品質不足',
    'INSUFFICIENT_INPUT_AUTHORITY': '入力権限不足',
    'NUMERICAL_NONCONVERGENCE': '数値非収束',
    'OUTSIDE_APPLICABILITY': '適用域外',
    'UNSUPPORTED_OBSERVABLE': '未対応観測量',
}

QUALIFICATION_LEVEL_LABELS: dict[str, str] = {
    'CAN_REPRESENT': '表現可能（検証ではない）',
    'NUMERICALLY_VERIFIED': '数値検証済み',
    'EXTERNAL_BENCHMARK_VALIDATED': '外部ベンチマーク検証済み',
    'OWNED_ROOM_VALIDATED': '実室検証済み',
    'PRODUCTION_RECOMMENDATION_ELIGIBLE': '製品推奨適格',
}

CAPABILITY_STATE_LABELS: dict[str, str] = {
    'SUPPORTED': 'サポート宣言あり',
    'BOUNDED': '制約付きサポート',
    'UNSUPPORTED': '未サポート',
}

PHENOMENON_LABELS: dict[str, str] = {
    'direct_sound': '直接音',
    'specular_reflection': '鏡面反射',
    'edge_diffraction': 'エッジ回折',
    'scattering': '散乱',
    'low_frequency_modal_response': '低周波モード応答',
    'late_energy_decay': '後期エネルギー減衰',
    'coherent_phase': 'コヒーレント位相',
    'spatial_pressure_field': '空間音圧場',
    'portal_region_coupling': 'ポータル領域結合',
}

DECISION_LABELS: dict[str, str] = {
    'inspect_prediction': '予測の閲覧',
    'compare_candidates': '候補比較',
    'automatic_recommendation': '自動推奨',
}

DECISION_VERDICT_LABELS: dict[str, str] = {
    'allowed': '許可',
    'trend_only': '傾向のみ',
    'blocked': 'ブロック',
    'not_supported': '不可（証拠なし）',
}

CLAIM_LABELS: dict[str, str] = {
    'absolute_level_at_position': '位置における絶対レベル',
    'local_frequency_response': '局所周波数応答',
    'spatial_coverage': '空間カバレッジ',
    'early_reflection_structure': '初期反射構造',
    'modal_response': 'モード応答',
    'decay_time': '残響時間',
    'late_diffuse_field': '後期拡散場',
    'phase_coherent_field': '位相コヒーレント場',
    'speech_intelligibility': '音声明瞭度',
    'hybrid_overlap_consistency': 'ハイブリッド重複整合性',
}


def envelope_dimension_label(dimension: EnvelopeDimensionId | str) -> str:
    return ENVELOPE_DIMENSION_LABELS.get(dimension, dimension)


def envelope_class_label(evidence_class: EnvelopeEvidenceClass | str) -> str:
    return ENVELOPE_CLASS_LABELS.get(evidence_class, evidence_class)


def qualification_verdict_label(verdict: str) -> str:
    return QUALIFICATION_VERDICT_LABELS.get(verdict, verdict)


def qualification_level_label(level: str) -> str:
    return QUALIFICATION_LEVEL_LABELS.get(level, level)


def capability_state_label(state: str) -> str:
    return CAPABILITY_STATE_LABELS.get(state, state)


def phenomenon_label(phenomenon: str) -> str:
    return PHENOMENON_LABELS.get(phenomenon, phenomenon)


#: Labels for the meta-verdict tokens the composer itself emits.
META_VERDICT_LABELS: dict[str, str] = {
    'STALE': '陳腐化（証拠は過去のもの）',
    'DECLARED': '宣言済み',
}


def envelope_verdict_label(verdict: str | None) -> str:
    """JA label for any verdict token the envelope can carry —
    qualification verdicts, capability declarations, bound verdicts,
    VUQ verdicts and composer meta tokens; raw token otherwise."""
    if not verdict:
        return '—'
    for table in (
        QUALIFICATION_VERDICT_LABELS,
        CAPABILITY_STATE_LABELS,
        CONFIDENCE_BOUND_LABELS,
        VUQ_LABELS,
        META_VERDICT_LABELS,
    ):
        label = table.get(verdict)  # type: ignore[call-overload]
        if label is not None:
            return label
    return verdict


def envelope_decision_label(decision: EnvelopeDecision | str) -> str:
    return DECISION_LABELS.get(decision, decision)


def envelope_decision_verdict_label(verdict: str) -> str:
    return DECISION_VERDICT_LABELS.get(verdict, verdict)


def envelope_claim_label(claim: ClaimClass | str) -> str:
    return CLAIM_LABELS.get(claim, claim)


def _band_label(band: FrequencyDomain) -> str:
    return f'{band.minimum_hz:g}–{band.maximum_hz:g} Hz'


def _band_from_hz(
    band_hz: tuple[float, float] | None,
) -> FrequencyDomain | None:
    if band_hz is None:
        return None
    low, high = band_hz
    if not (0.0 < low < high):
        return None
    return FrequencyDomain(minimum_hz=low, maximum_hz=high)


# ---------------------------------------------------------------------------
# Context — the caller-declared solver-path identity
# ---------------------------------------------------------------------------


class EnvelopeContext(BaseModel):
    """The current solver-path identity the envelope is rendered for.

    The context is declared by the caller (the solver path selected in the
    workspace); it is what staleness is checked against. Evidence bound to
    an older identity is reported as ``stale``, never silently reused.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    document_id: str = Field(min_length=1)
    adapter_id: str | None = None
    adapter_version: str | None = None
    adapter_descriptor_id: str | None = None
    solver_path: str | None = None
    scene_revision_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )

    @model_validator(mode='after')
    def _validate(self) -> 'EnvelopeContext':
        if (self.adapter_id is None) != (self.adapter_version is None):
            raise ValueError(
                'adapter id and version must be supplied together'
            )
        return self


def context_for_document(
    bundle: EnvelopeEvidenceBundle,
    *,
    document_id: str,
) -> EnvelopeContext:
    """Derive the solver-path context from the bundle's sealed refs.

    This is a read of declared identity, never inference: the adapter
    identity comes from the capability manifest an input envelope pins,
    and the solver path from the scene mappings' solver_path column —
    only when the sealed evidence agrees on exactly one value. Ambiguous
    or absent bindings leave the field unset (fail-closed: no claimed
    identity means no staleness judgement is possible).
    """

    adapter_id = adapter_version = adapter_descriptor_id = None
    manifests = _bound_manifests(
        bundle, EnvelopeContext(document_id=document_id)
    )
    manifest_ids = {m.adapter_id for m in manifests}
    manifest_descriptors = {m.adapter_descriptor_id for m in manifests}
    manifest_versions = {m.adapter_version for m in manifests}
    if (
        len(manifest_ids) == 1
        and len(manifest_descriptors) == 1
        and len(manifest_versions) == 1
    ):
        adapter_id = next(iter(manifest_ids))
        adapter_version = next(iter(manifest_versions))
        adapter_descriptor_id = next(iter(manifest_descriptors))

    solver_paths = {m.solver_path for m in bundle.scene_mappings}
    solver_path = (
        next(iter(solver_paths)) if len(solver_paths) == 1 else None
    )

    scene_shas = {
        envelope.scene_revision_ref.ref_sha256
        for envelope in bundle.input_envelopes
        if envelope.scene_revision_ref is not None
        and envelope.scene_revision_ref.ref_sha256 is not None
    }
    scene_revision_sha256 = (
        next(iter(scene_shas)) if len(scene_shas) == 1 else None
    )

    return EnvelopeContext(
        document_id=document_id,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        adapter_descriptor_id=adapter_descriptor_id,
        solver_path=solver_path,
        scene_revision_sha256=scene_revision_sha256,
    )


# ---------------------------------------------------------------------------
# Evidence bundle — sealed records loaded for one document
# ---------------------------------------------------------------------------


class EnvelopeEvidenceBundle(BaseModel):
    """Every sealed record the composition may read, all document-scoped.

    ``capability_manifests`` are adapter-scoped by design; they are bound
    into the envelope only through a document's ``SolverInputEnvelope``
    ``capability_manifest_ref`` or the context's ``adapter_descriptor_id``.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    document_id: str = Field(min_length=1)
    qualifications: tuple[BenchmarkQualification, ...] = ()
    scene_mappings: tuple[BenchmarkSceneMapping, ...] = ()
    preregistrations: tuple[BenchmarkPreregistration, ...] = ()
    capability_manifests: tuple[SolverCapabilityManifest, ...] = ()
    input_envelopes: tuple[SolverInputEnvelope, ...] = ()
    claim_bound_records: tuple[ClaimBoundRecord, ...] = ()
    uncertainty_protocols: tuple[ValidationUncertaintyProtocol, ...] = ()
    uncertainty_evaluations: tuple[ObservableUncertaintyEvaluation, ...] = ()
    uncertainty_verdicts: tuple[UncertaintyValidationVerdict, ...] = ()


def _list_capability_manifests(
    path: Any,
) -> tuple[SolverCapabilityManifest, ...]:
    """Read every sealed capability manifest from the dispatch store.

    Manifests are adapter-scoped, so the loader lists the table and lets
    the compositor bind by ref/descriptor. Each payload re-validates the
    sealed model (id + semantic hash), so a tampered row fails closed.
    """

    ensure_native_schema(path)
    with closing(connect_sqlite(path)) as connection:
        require_native_tables(connection, 'cad_solver_capability_manifests')
        rows = connection.execute(
            'SELECT payload_json FROM cad_solver_capability_manifests '
            'ORDER BY seq ASC'
        ).fetchall()
    manifests: list[SolverCapabilityManifest] = []
    for row in rows:
        manifest = SolverCapabilityManifest.model_validate_json(
            row['payload_json']
        )
        payload = manifest.model_dump(mode='json')
        payload.pop('manifest_id', None)
        payload.pop('semantic_sha256', None)
        if manifest.semantic_sha256 != _hash(payload):
            raise ValueError(
                f'capability manifest {manifest.manifest_id} hash mismatch'
            )
        if manifest.manifest_id != (
            f'solver-capability-manifest:{manifest.semantic_sha256}'
        ):
            raise ValueError(
                f'capability manifest {manifest.manifest_id} id mismatch'
            )
        manifests.append(manifest)
    return tuple(manifests)


def load_envelope_evidence(
    scene_repository: SceneRepository,
    *,
    document_id: str,
) -> EnvelopeEvidenceBundle:
    """Load every sealed authority record bound to ``document_id``.

    The load is all-or-nothing: any tampered payload fails validation and
    raises, so a composed envelope never carries silently corrupted rows.
    """

    bound_repo = CadSolverConfidenceBoundRepository(scene_repository)
    uncertainty_repo = CadValidationUncertaintyRepository(scene_repository)
    benchmark_repo = CadBenchmarkQualificationRepository(scene_repository)
    return EnvelopeEvidenceBundle(
        document_id=document_id,
        qualifications=benchmark_repo.qualifications.list(document_id),
        scene_mappings=benchmark_repo.scene_mappings.list(document_id),
        preregistrations=benchmark_repo.preregistrations.list(document_id),
        capability_manifests=_list_capability_manifests(scene_repository.path),
        input_envelopes=bound_repo.list_input_envelopes(document_id),
        claim_bound_records=bound_repo.list_claim_bounds(document_id),
        uncertainty_protocols=uncertainty_repo.list_protocols(document_id),
        uncertainty_evaluations=uncertainty_repo.list_evaluations(document_id),
        uncertainty_verdicts=uncertainty_repo.list_verdicts(document_id),
    )


# ---------------------------------------------------------------------------
# Envelope model rows
# ---------------------------------------------------------------------------


class EnvelopeDimensionState(BaseModel):
    """One envelope dimension: a verbatim verdict token plus an evidence
    class, its declared band, the evidence refs consumed, caveats, and any
    sibling verdicts kept distinct rather than collapsed."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    dimension: EnvelopeDimensionId
    verdict: str | None = None
    evidence_class: EnvelopeEvidenceClass = 'absent'
    band: FrequencyDomain | None = None
    evidence_refs: tuple[AuthorityRef, ...] = ()
    caveats: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    stale_refs: tuple[AuthorityRef, ...] = ()
    staleness_notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _validate(self) -> 'EnvelopeDimensionState':
        if self.evidence_class == 'absent':
            if self.verdict is not None or self.evidence_refs:
                raise ValueError(
                    'absent evidence cannot carry a verdict or refs'
                )
        else:
            if self.verdict is None:
                raise ValueError(
                    'a non-absent dimension requires a verdict token'
                )
            if not self.evidence_refs:
                raise ValueError(
                    'a non-absent dimension requires evidence refs'
                )
        return self


class EnvelopePhenomenonCell(BaseModel):
    """One solver-path phenomenon: the declared capability state and band
    plus the external-validation verdicts and input-bound claim verdicts
    observed for it. Every column traces to sealed evidence."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    phenomenon: SolverPathPhenomenon
    capability_state: Literal['SUPPORTED', 'BOUNDED', 'UNSUPPORTED'] | None = (
        None
    )
    band: FrequencyDomain | None = None
    bound_description: str | None = None
    external_verdicts: tuple[str, ...] = ()
    external_evidence_class: EnvelopeEvidenceClass | None = None
    external_band: FrequencyDomain | None = None
    claim_verdicts: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _validate(self) -> 'EnvelopePhenomenonCell':
        if self.capability_state is None and self.band is not None:
            raise ValueError('a capability band requires a capability state')
        if self.external_evidence_class is None and self.external_verdicts:
            raise ValueError(
                'external verdicts require an external evidence class'
            )
        return self


class EnvelopeClaimRow(BaseModel):
    """One claim-class bound row from a landed #811 record."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    claim: ClaimClass
    verdict: BoundVerdict
    ceiling: str
    weakest_dimensions: tuple[str, ...] = ()
    record_id: str = Field(min_length=1)
    supporting_refs: tuple[AuthorityRef, ...] = ()


class EnvelopeDecisionRow(BaseModel):
    """One context-of-use decision with its basis in the other dimensions."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    decision: EnvelopeDecision
    verdict: EnvelopeDecisionVerdict
    basis_refs: tuple[AuthorityRef, ...] = ()
    basis_states: tuple[str, ...] = ()


class ApplicabilityEnvelope(BaseModel):
    """The composed, seven-dimension applicability envelope.

    The envelope is a view — never persisted, never a score. Every field
    traces to sealed records through ``evidence_refs``; ``envelope_hash``
    pins the full emitted structure so a report bound to it detects drift.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal['applicability-envelope-1'] = (
        APPLICABILITY_ENVELOPE_VERSION
    )
    composer_version: Literal['applicability-envelope-composer-1'] = (
        APPLICABILITY_ENVELOPE_COMPOSER_VERSION
    )
    evaluated_at_utc: str = Field(min_length=1)
    context: EnvelopeContext
    dimensions: tuple[EnvelopeDimensionState, ...]
    phenomenon_cells: tuple[EnvelopePhenomenonCell, ...] = ()
    claim_rows: tuple[EnvelopeClaimRow, ...] = ()
    decisions: tuple[EnvelopeDecisionRow, ...] = ()
    ranking_verdict: str | None = None
    model_form_discrepancy: bool = False
    envelope_hash: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _validate(self) -> 'ApplicabilityEnvelope':
        dims = tuple(d.dimension for d in self.dimensions)
        if dims != ENVELOPE_DIMENSIONS:
            raise ValueError(
                'dimensions must enumerate ENVELOPE_DIMENSIONS in order'
            )
        by_decision = {row.decision for row in self.decisions}
        if by_decision != set(ENVELOPE_DECISIONS):
            raise ValueError(
                'decisions must enumerate ENVELOPE_DECISIONS exactly'
            )
        if self.envelope_hash != _hash(self.identity_payload()):
            raise ValueError('applicability envelope hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'envelope_hash'})

    def dimension(
        self, dimension: EnvelopeDimensionId
    ) -> EnvelopeDimensionState:
        for state in self.dimensions:
            if state.dimension == dimension:
                return state
        raise KeyError(dimension)


# ---------------------------------------------------------------------------
# Composition internals
# ---------------------------------------------------------------------------


def _absent(dimension: EnvelopeDimensionId) -> EnvelopeDimensionState:
    return EnvelopeDimensionState(dimension=dimension)


def _qualification_class(
    qualification: BenchmarkQualification,
) -> EnvelopeEvidenceClass:
    """Map a #809 qualification (verdict + level) to an evidence class."""

    verdict = qualification.verdict
    if verdict == 'PASS_WITHIN_DOMAIN':
        if qualification.level_attained == 'EXTERNAL_BENCHMARK_VALIDATED':
            return 'externally_validated'
        if qualification.level_attained == 'NUMERICALLY_VERIFIED':
            return 'numerically_verified'
        return 'qualified'
    if verdict in ('FAIL', 'NUMERICAL_NONCONVERGENCE'):
        return 'failed'
    if verdict == 'OUTSIDE_APPLICABILITY':
        return 'outside_applicability'
    if verdict == 'UNSUPPORTED_OBSERVABLE':
        return 'unsupported'
    return 'insufficient_evidence'


def _bound_verdict_class(verdict: BoundVerdict) -> EnvelopeEvidenceClass:
    if verdict == 'envelope_inherited':
        return 'qualified'
    if verdict in ('solver_bounded', 'bounded_by_input'):
        return 'bounded'
    if verdict == 'claim_denied':
        return 'failed'
    if verdict == 'solver_unqualified':
        return 'unsupported'
    return 'insufficient_evidence'


def _absolute_verdict_class(
    verdict: str | None,
) -> EnvelopeEvidenceClass | None:
    if verdict is None:
        return None
    if verdict == 'consistent_with_reference_within_uncertainty':
        return 'holdout_validated'
    if verdict in ('discrepancy_significant', 'model_form_discrepancy_required'):
        return 'failed'
    return 'insufficient_evidence'


def _evaluation_class(
    evaluation: ObservableUncertaintyEvaluation,
) -> EnvelopeEvidenceClass:
    if (
        evaluation.summary_verdict
        == 'consistent_with_reference_within_uncertainty'
    ):
        return 'supported'
    if evaluation.summary_verdict in (
        'discrepancy_significant',
        'model_form_discrepancy_required',
    ):
        return 'failed'
    return 'insufficient_evidence'


def _pick_headline(
    entries: Sequence[tuple[str, EnvelopeEvidenceClass, str]],
) -> tuple[str | None, EnvelopeEvidenceClass]:
    """Pick the dimension verdict token without collapsing anything.

    ``entries`` are (verbatim verdict token, class, record key). If any
    positive class is present the verdict is the token of the strongest
    positive; otherwise it is the token of the most informative negative.
    """

    if not entries:
        return None, 'absent'
    positives = [e for e in entries if e[1] in _POSITIVE_PRECEDENCE]
    if positives:
        best = min(
            positives, key=lambda e: _POSITIVE_PRECEDENCE.index(e[1])
        )
        return best[0], best[1]
    negatives = [e for e in entries if e[1] in _NEGATIVE_PRECEDENCE]
    if negatives:
        best = min(
            negatives, key=lambda e: _NEGATIVE_PRECEDENCE.index(e[1])
        )
        return best[0], best[1]
    return None, 'absent'


def _conflicts(
    entries: Sequence[tuple[str, EnvelopeEvidenceClass, str]],
    headline: str | None,
) -> tuple[str, ...]:
    seen: list[str] = []
    for token, _cls, _key in entries:
        if token != headline and token not in seen:
            seen.append(token)
    return tuple(seen)


def _refs_union(*groups: Iterable[AuthorityRef]) -> tuple[AuthorityRef, ...]:
    seen: dict[str, AuthorityRef] = {}
    for group in groups:
        for ref in group:
            seen.setdefault(f'{ref.kind}:{ref.ref_id}', ref)
    return tuple(seen.values())


def _manifest_ref(manifest: SolverCapabilityManifest) -> AuthorityRef:
    return AuthorityRef(
        kind='solver_capability_manifest',
        ref_id=manifest.manifest_id,
        ref_sha256=manifest.semantic_sha256,
    )


def _manifest_stale(
    manifest: SolverCapabilityManifest,
    context: EnvelopeContext,
) -> str | None:
    """A manifest is stale when its pinned adapter identity no longer
    matches the context it is rendered for."""

    if (
        context.adapter_descriptor_id is not None
        and manifest.adapter_descriptor_id != context.adapter_descriptor_id
    ):
        return 'capability manifest adapter descriptor drifted'
    if (
        context.adapter_id is not None
        and manifest.adapter_id != context.adapter_id
    ):
        return 'capability manifest adapter id drifted'
    if (
        context.adapter_version is not None
        and manifest.adapter_version != context.adapter_version
    ):
        return 'capability manifest adapter version drifted'
    return None


def _bound_manifests(
    bundle: EnvelopeEvidenceBundle,
    context: EnvelopeContext,
) -> tuple[SolverCapabilityManifest, ...]:
    """Manifests bound to this document/context — via an input envelope's
    sealed ``capability_manifest_ref`` or the context descriptor id."""

    by_id = {m.manifest_id: m for m in bundle.capability_manifests}
    bound: list[SolverCapabilityManifest] = []
    for envelope in bundle.input_envelopes:
        ref = envelope.capability_manifest_ref
        if ref is None:
            continue
        manifest = by_id.get(ref.ref_id)
        if manifest is None:
            continue
        # the sealed envelope pins the manifest sha — a mismatch means the
        # store was tampered with, so fail closed rather than re-resolve
        if manifest.semantic_sha256 != ref.ref_sha256:
            raise ValueError(
                f'input envelope {envelope.envelope_id} pins capability '
                f'manifest {ref.ref_id} at {ref.ref_sha256[:12]}… but the '
                f'stored payload hashes {manifest.semantic_sha256[:12]}…'
            )
        if all(m.manifest_id != manifest.manifest_id for m in bound):
            bound.append(manifest)
    if context.adapter_descriptor_id is not None:
        for manifest in bundle.capability_manifests:
            if (
                manifest.adapter_descriptor_id
                == context.adapter_descriptor_id
                and all(
                    m.manifest_id != manifest.manifest_id for m in bound
                )
            ):
                bound.append(manifest)
    return tuple(bound)


def _compose_capability(
    bundle: EnvelopeEvidenceBundle,
    context: EnvelopeContext,
) -> tuple[EnvelopeDimensionState, dict[str, EnvelopePhenomenonCell]]:
    pairs = _bound_manifests(bundle, context)
    cells: dict[str, EnvelopePhenomenonCell] = {
        p: EnvelopePhenomenonCell(phenomenon=p) for p in SOLVER_PATH_PHENOMENA
    }
    if not pairs:
        return _absent('representational_capability'), cells

    current: list[SolverCapabilityManifest] = []
    stale: list[SolverCapabilityManifest] = []
    stale_notes: list[str] = []
    for manifest in pairs:
        note = _manifest_stale(manifest, context)
        if note is None:
            current.append(manifest)
        else:
            stale.append(manifest)
            stale_notes.append(f'{manifest.manifest_id}: {note}')

    entries: list[tuple[str, EnvelopeEvidenceClass, str]] = []
    refs: list[AuthorityRef] = []
    caveats: list[str] = []
    band: FrequencyDomain | None = None

    state_rank = {'SUPPORTED': 0, 'BOUNDED': 1, 'UNSUPPORTED': 2}
    for manifest in current:
        refs.append(_manifest_ref(manifest))
        band = band or manifest.solver_valid_frequency_domain
        states = {row.state for row in manifest.rows}
        if 'UNSUPPORTED' in states or 'BOUNDED' in states:
            caveats.append(
                'capability declared with bounds/gaps — see '
                'per-phenomenon rows'
            )
        if 'SUPPORTED' in states:
            entries.append(('SUPPORTED', 'supported', manifest.manifest_id))
        elif 'BOUNDED' in states:
            entries.append(('BOUNDED', 'bounded', manifest.manifest_id))
        else:
            entries.append(
                ('UNSUPPORTED', 'unsupported', manifest.manifest_id)
            )
        for row in manifest.rows:
            cell = cells[row.phenomenon]
            if cell.capability_state is not None and (
                state_rank[row.state] >= state_rank[cell.capability_state]
            ):
                continue  # keep the strongest declared state per cell
            cells[row.phenomenon] = EnvelopePhenomenonCell(
                phenomenon=row.phenomenon,
                capability_state=row.state,
                band=row.valid_frequency_domain,
                bound_description=row.bound_description,
                external_verdicts=cell.external_verdicts,
                external_evidence_class=cell.external_evidence_class,
                external_band=cell.external_band,
                claim_verdicts=cell.claim_verdicts,
            )

    stale_ref_tuple = tuple(_manifest_ref(m) for m in stale)
    if not current:
        return (
            EnvelopeDimensionState(
                dimension='representational_capability',
                verdict='STALE',
                evidence_class='stale',
                evidence_refs=stale_ref_tuple,
                stale_refs=stale_ref_tuple,
                staleness_notes=tuple(stale_notes),
            ),
            cells,
        )

    headline, headline_class = _pick_headline(entries)
    return (
        EnvelopeDimensionState(
            dimension='representational_capability',
            verdict=headline,
            evidence_class=headline_class,
            band=band,
            evidence_refs=tuple(refs),
            caveats=tuple(caveats),
            conflicts=_conflicts(entries, headline),
            stale_refs=stale_ref_tuple,
            staleness_notes=tuple(stale_notes),
        ),
        cells,
    )


def _qualification_ref(qualification: BenchmarkQualification) -> AuthorityRef:
    return AuthorityRef(
        kind='benchmark_qualification',
        ref_id=qualification.qualification_id,
        ref_sha256=qualification.qualification_sha256,
    )


def _qualification_stale(
    qualification: BenchmarkQualification,
    preregistrations: dict[str, BenchmarkPreregistration],
    context: EnvelopeContext,
) -> str | None:
    prereg = preregistrations.get(qualification.preregistration_ref.ref_id)
    if prereg is None:
        return None
    if (
        context.adapter_id is not None
        and prereg.provider_id != context.adapter_id
    ):
        return 'benchmark qualification solver provider drifted'
    if (
        context.adapter_version is not None
        and prereg.provider_version != context.adapter_version
    ):
        return 'benchmark qualification solver version drifted'
    return None


def _compose_external_validation(
    bundle: EnvelopeEvidenceBundle,
    context: EnvelopeContext,
    cells: dict[str, EnvelopePhenomenonCell],
) -> EnvelopeDimensionState:
    preregistrations = {
        p.preregistration_id: p for p in bundle.preregistrations
    }
    mappings = {m.mapping_id: m for m in bundle.scene_mappings}

    entries: list[tuple[str, EnvelopeEvidenceClass, str]] = []
    refs: list[AuthorityRef] = []
    stale_refs: list[AuthorityRef] = []
    stale_notes: list[str] = []
    band: FrequencyDomain | None = None
    cell_entries: dict[str, list[tuple[str, EnvelopeEvidenceClass, str]]] = {}
    cell_bands: dict[str, FrequencyDomain] = {}

    for qualification in bundle.qualifications:
        mapping = mappings.get(qualification.mapping_ref.ref_id)
        if mapping is None:
            continue  # unmapped qualification cannot attribute a solver path
        if (
            context.solver_path is not None
            and mapping.solver_path != context.solver_path
        ):
            continue  # evidence for another solver path never covers this one
        ref = _qualification_ref(qualification)
        note = _qualification_stale(qualification, preregistrations, context)
        if note is not None:
            stale_refs.append(ref)
            stale_notes.append(
                f'{qualification.qualification_id}: {note}'
            )
            continue
        cls = _qualification_class(qualification)
        entries.append(
            (qualification.verdict, cls, qualification.qualification_id)
        )
        refs.append(ref)
        band = band or _band_from_hz(qualification.verdict_band_hz)
        cell = cell_entries.setdefault(mapping.phenomenon_id, [])
        cell.append(
            (qualification.verdict, cls, qualification.qualification_id)
        )
        cell_band = _band_from_hz(qualification.verdict_band_hz)
        if cell_band is not None and mapping.phenomenon_id not in cell_bands:
            cell_bands[mapping.phenomenon_id] = cell_band

    for phenomenon_id, entries_for_cell in cell_entries.items():
        cell = cells.get(phenomenon_id)
        if cell is None:
            continue
        _headline, cell_class = _pick_headline(entries_for_cell)
        cells[phenomenon_id] = EnvelopePhenomenonCell(
            phenomenon=cell.phenomenon,
            capability_state=cell.capability_state,
            band=cell.band,
            bound_description=cell.bound_description,
            external_verdicts=tuple(
                dict.fromkeys(t for t, _c, _k in entries_for_cell)
            ),
            external_evidence_class=cell_class,
            external_band=cell_bands.get(phenomenon_id),
            claim_verdicts=cell.claim_verdicts,
        )

    if not entries:
        if stale_refs:
            return EnvelopeDimensionState(
                dimension='external_validation',
                verdict='STALE',
                evidence_class='stale',
                evidence_refs=tuple(stale_refs),
                stale_refs=tuple(stale_refs),
                staleness_notes=tuple(stale_notes),
            )
        return _absent('external_validation')

    headline, headline_class = _pick_headline(entries)
    return EnvelopeDimensionState(
        dimension='external_validation',
        verdict=headline,
        evidence_class=headline_class,
        band=band,
        evidence_refs=tuple(refs),
        conflicts=_conflicts(entries, headline),
        stale_refs=tuple(stale_refs),
        staleness_notes=tuple(stale_notes),
    )


def _compose_verification(
    bundle: EnvelopeEvidenceBundle,
    context: EnvelopeContext,
    solver_envelopes: Sequence[AccuracyEnvelopeRecord],
) -> EnvelopeDimensionState:
    entries: list[tuple[str, EnvelopeEvidenceClass, str]] = []
    refs: list[AuthorityRef] = []
    stale_refs: list[AuthorityRef] = []
    stale_notes: list[str] = []
    band: FrequencyDomain | None = None

    for record in solver_envelopes:
        if (
            context.adapter_id is not None
            and record.solver_id != context.adapter_id
        ):
            continue  # evidence for another solver never covers this one
        ref = AuthorityRef(
            kind='accuracy_envelope',
            ref_id=record.envelope_id,
            ref_sha256=record.envelope_sha256,
        )
        if (
            context.adapter_version is not None
            and record.solver_version != context.adapter_version
        ):
            stale_refs.append(ref)
            stale_notes.append(
                f'{record.envelope_id}: solver version drifted '
                f'({record.solver_version} != {context.adapter_version})'
            )
            continue
        refs.append(ref)
        if record.convergence == 'NOT_CONVERGED':
            entries.append(
                ('NOT_CONVERGED', 'failed', record.envelope_id)
            )
        elif record.convergence == 'CONVERGED_WITHIN_TESTED_RANGE':
            state = record.validation_state
            if state == 'VALIDATED_FOR_DECLARED_DOMAIN':
                entries.append(
                    (state, 'numerically_verified', record.envelope_id)
                )
            elif state == 'VALIDATED_WITH_LIMITATIONS':
                entries.append((state, 'bounded', record.envelope_id))
            elif state == 'NOT_APPLICABLE':
                entries.append((state, 'unsupported', record.envelope_id))
            elif state == 'EXPERIMENTAL':
                entries.append(
                    (state, 'insufficient_evidence', record.envelope_id)
                )
            else:
                entries.append((state, 'bounded', record.envelope_id))
        else:
            entries.append(
                (
                    record.convergence,
                    'insufficient_evidence',
                    record.envelope_id,
                )
            )
        if band is None:
            band = _band_from_hz(record.frequency_range_hz)

    preregistrations = {
        p.preregistration_id: p for p in bundle.preregistrations
    }
    mappings = {m.mapping_id: m for m in bundle.scene_mappings}
    for qualification in bundle.qualifications:
        mapping = mappings.get(qualification.mapping_ref.ref_id)
        if mapping is None:
            continue
        if (
            context.solver_path is not None
            and mapping.solver_path != context.solver_path
        ):
            continue
        if _qualification_stale(qualification, preregistrations, context):
            continue  # staleness is reported on the external dimension
        refs.append(_qualification_ref(qualification))
        if qualification.convergence == 'NOT_CONVERGED':
            entries.append(
                (
                    'NOT_CONVERGED',
                    'failed',
                    qualification.qualification_id,
                )
            )
        elif qualification.convergence == 'CONVERGED_WITHIN_TESTED_RANGE':
            entries.append(
                (
                    'CONVERGED_WITHIN_TESTED_RANGE',
                    'numerically_verified',
                    qualification.qualification_id,
                )
            )
        else:
            entries.append(
                (
                    qualification.convergence,
                    'insufficient_evidence',
                    qualification.qualification_id,
                )
            )

    if not entries:
        if stale_refs:
            return EnvelopeDimensionState(
                dimension='verification',
                verdict='STALE',
                evidence_class='stale',
                evidence_refs=tuple(stale_refs),
                stale_refs=tuple(stale_refs),
                staleness_notes=tuple(stale_notes),
            )
        return _absent('verification')

    headline, headline_class = _pick_headline(entries)
    return EnvelopeDimensionState(
        dimension='verification',
        verdict=headline,
        evidence_class=headline_class,
        band=band,
        evidence_refs=tuple(refs),
        conflicts=_conflicts(entries, headline),
        stale_refs=tuple(stale_refs),
        staleness_notes=tuple(stale_notes),
    )


def _compose_input_qualification(
    bundle: EnvelopeEvidenceBundle,
    context: EnvelopeContext,
) -> tuple[EnvelopeDimensionState, tuple[EnvelopeClaimRow, ...]]:
    claim_rows: list[EnvelopeClaimRow] = []
    entries: list[tuple[str, EnvelopeEvidenceClass, str]] = []
    refs: list[AuthorityRef] = []
    stale_refs: list[AuthorityRef] = []
    stale_notes: list[str] = []

    envelopes = {e.envelope_id: e for e in bundle.input_envelopes}
    for record in bundle.claim_bound_records:
        record_ref = AuthorityRef(
            kind='claim_bound_record',
            ref_id=record.record_id,
            ref_sha256=record.record_sha256,
        )
        envelope = envelopes.get(record.input_envelope_ref.ref_id)
        stale = (
            envelope is not None
            and context.scene_revision_sha256 is not None
            and envelope.scene_revision_ref is not None
            and envelope.scene_revision_ref.ref_sha256
            != context.scene_revision_sha256
        )
        if stale:
            stale_refs.append(record_ref)
            stale_notes.append(
                f'{record.record_id}: input envelope scene revision drifted'
            )
            continue
        refs.append(record_ref)
        for row in record.rows:
            claim_rows.append(
                EnvelopeClaimRow(
                    claim=row.claim,
                    verdict=row.verdict,
                    ceiling=row.ceiling,
                    weakest_dimensions=row.weakest_dimensions,
                    record_id=record.record_id,
                    supporting_refs=row.supporting_refs,
                )
            )
            entries.append(
                (row.verdict, _bound_verdict_class(row.verdict), row.claim)
            )

    if not entries:
        if stale_refs:
            return (
                EnvelopeDimensionState(
                    dimension='input_qualification',
                    verdict='STALE',
                    evidence_class='stale',
                    evidence_refs=tuple(stale_refs),
                    stale_refs=tuple(stale_refs),
                    staleness_notes=tuple(stale_notes),
                ),
                tuple(),
            )
        return _absent('input_qualification'), tuple()

    headline, headline_class = _pick_headline(entries)
    return (
        EnvelopeDimensionState(
            dimension='input_qualification',
            verdict=headline,
            evidence_class=headline_class,
            evidence_refs=tuple(refs),
            conflicts=_conflicts(entries, headline),
            stale_refs=tuple(stale_refs),
            staleness_notes=tuple(stale_notes),
        ),
        tuple(claim_rows),
    )


def _compose_owned_room(
    bundle: EnvelopeEvidenceBundle,
    context: EnvelopeContext,
) -> tuple[EnvelopeDimensionState, str | None]:
    protocols = {p.protocol_id: p for p in bundle.uncertainty_protocols}
    entries: list[tuple[str, EnvelopeEvidenceClass, str]] = []
    refs: list[AuthorityRef] = []
    stale_refs: list[AuthorityRef] = []
    stale_notes: list[str] = []
    caveats: list[str] = []
    ranking_verdict: str | None = None

    for verdict in bundle.uncertainty_verdicts:
        if verdict.evidence_scope != 'owned_room':
            continue  # synthetic-fixture evidence never counts as owned-room
        ref = AuthorityRef(
            kind='uncertainty_validation_verdict',
            ref_id=verdict.verdict_id,
            ref_sha256=verdict.verdict_sha256,
        )
        protocol = protocols.get(verdict.protocol_ref.ref_id)
        stale = (
            protocol is not None
            and context.adapter_version is not None
            and protocol.model_version != context.adapter_version
        )
        if stale:
            stale_refs.append(ref)
            stale_notes.append(
                f'{verdict.verdict_id}: validation protocol model '
                f'version drifted'
            )
            continue
        refs.append(ref)
        if verdict.ranking_verdict != 'ranking_not_evaluated':
            ranking_verdict = verdict.ranking_verdict
        caveats.extend(verdict.gate_reasons)
        cls = _absolute_verdict_class(verdict.absolute_verdict)
        if cls is not None:
            entries.append(
                (verdict.absolute_verdict, cls, verdict.verdict_id)
            )
        elif verdict.ranking_verdict != 'ranking_not_evaluated':
            # ranking-only verdicts never upgrade the absolute axis
            entries.append(
                (
                    verdict.ranking_verdict,
                    'insufficient_evidence',
                    verdict.verdict_id,
                )
            )

    if not entries:
        if stale_refs:
            return (
                EnvelopeDimensionState(
                    dimension='owned_room_evidence',
                    verdict='STALE',
                    evidence_class='stale',
                    evidence_refs=tuple(stale_refs),
                    stale_refs=tuple(stale_refs),
                    staleness_notes=tuple(stale_notes),
                ),
                ranking_verdict,
            )
        return _absent('owned_room_evidence'), ranking_verdict

    headline, headline_class = _pick_headline(entries)
    return (
        EnvelopeDimensionState(
            dimension='owned_room_evidence',
            verdict=headline,
            evidence_class=headline_class,
            evidence_refs=tuple(refs),
            caveats=tuple(dict.fromkeys(caveats)),
            conflicts=_conflicts(entries, headline),
            stale_refs=tuple(stale_refs),
            staleness_notes=tuple(stale_notes),
        ),
        ranking_verdict,
    )


def _compose_uncertainty_limitations(
    bundle: EnvelopeEvidenceBundle,
    context: EnvelopeContext,
) -> tuple[EnvelopeDimensionState, bool]:
    entries: list[tuple[str, EnvelopeEvidenceClass, str]] = []
    refs: list[AuthorityRef] = []
    caveats: list[str] = []
    model_form_discrepancy = False

    for verdict in bundle.uncertainty_verdicts:
        refs.append(
            AuthorityRef(
                kind='uncertainty_validation_verdict',
                ref_id=verdict.verdict_id,
                ref_sha256=verdict.verdict_sha256,
            )
        )
        cls = _absolute_verdict_class(verdict.absolute_verdict)
        if cls is not None:
            entries.append(
                (verdict.absolute_verdict, cls, verdict.verdict_id)
            )
        if verdict.model_form_discrepancy_suspected:
            model_form_discrepancy = True
            caveats.append(
                f'{verdict.verdict_id}: model-form discrepancy suspected'
            )
        if verdict.dominant_uncertainty_category != 'unknown':
            caveats.append(
                f'{verdict.verdict_id}: dominant uncertainty '
                f'{verdict.dominant_uncertainty_category}'
            )

    for evaluation in bundle.uncertainty_evaluations:
        entries.append(
            (
                evaluation.summary_verdict,
                _evaluation_class(evaluation),
                evaluation.evaluation_id,
            )
        )
        refs.append(
            AuthorityRef(
                kind='observable_uncertainty_evaluation',
                ref_id=evaluation.evaluation_id,
                ref_sha256=evaluation.evaluation_sha256,
            )
        )
        if evaluation.dominant_uncertainty_category != 'unknown':
            caveats.append(
                f'{evaluation.observable.observable_id}: dominant '
                f'uncertainty {evaluation.dominant_uncertainty_category}'
            )
        caveats.extend(evaluation.limitations)

    if not entries:
        return _absent('uncertainty_limitations'), model_form_discrepancy

    # For limitations the honest headline is the *worst* unresolved state,
    # not the best — this dimension reports what is not yet resolved.
    negatives = [e for e in entries if e[1] in _NEGATIVE_PRECEDENCE]
    if negatives:
        best = min(
            negatives, key=lambda e: _NEGATIVE_PRECEDENCE.index(e[1])
        )
        headline, headline_class = best[0], best[1]
    else:
        headline, headline_class = _pick_headline(entries)

    return (
        EnvelopeDimensionState(
            dimension='uncertainty_limitations',
            verdict=headline,
            evidence_class=headline_class,
            evidence_refs=tuple(refs),
            caveats=tuple(dict.fromkeys(caveats)),
            conflicts=_conflicts(entries, headline),
        ),
        model_form_discrepancy,
    )


def _compose_decisions(
    capability: EnvelopeDimensionState,
    owned_room: EnvelopeDimensionState,
    external: EnvelopeDimensionState,
    ranking_verdict: str | None,
) -> tuple[EnvelopeDecisionRow, ...]:
    # inspect_prediction: allowed whenever the solver path has any declared
    # capability; inspection is never labeled as validation.
    if capability.evidence_class in ('supported', 'bounded'):
        inspect = EnvelopeDecisionRow(
            decision='inspect_prediction',
            verdict='allowed',
            basis_refs=capability.evidence_refs,
            basis_states=(f'capability={capability.evidence_class}',),
        )
    else:
        inspect = EnvelopeDecisionRow(
            decision='inspect_prediction',
            verdict='not_supported',
            basis_states=(f'capability={capability.evidence_class}',),
        )

    # compare_candidates: ordering claims are gated by the ranking axis;
    # absolute validation upgrades it only when owned-room consistency and
    # external validation both hold for this solver path.
    absolute_ok = (
        owned_room.evidence_class == 'holdout_validated'
        and external.evidence_class == 'externally_validated'
    )
    if absolute_ok:
        compare_verdict: EnvelopeDecisionVerdict = 'allowed'
    elif ranking_verdict == 'ranking_supported':
        compare_verdict = 'trend_only'
    else:
        compare_verdict = 'not_supported'
    compare = EnvelopeDecisionRow(
        decision='compare_candidates',
        verdict=compare_verdict,
        basis_refs=_refs_union(
            owned_room.evidence_refs, external.evidence_refs
        ),
        basis_states=(
            f'owned_room={owned_room.evidence_class}',
            f'external={external.evidence_class}',
            f'ranking={ranking_verdict or "absent"}',
        ),
    )

    # automatic_recommendation: requires OWNED_ROOM_VALIDATED plus a
    # PRODUCTION_RECOMMENDATION_ELIGIBLE qualification level — no landed
    # authority can mint those yet, so this stays fail-closed blocked.
    recommend = EnvelopeDecisionRow(
        decision='automatic_recommendation',
        verdict='blocked',
        basis_states=(
            f'owned_room={owned_room.evidence_class}',
            f'external={external.evidence_class}',
            'production_recommendation_eligible=absent',
        ),
    )
    return (inspect, compare, recommend)


def _attach_claim_verdicts(
    cells: dict[str, EnvelopePhenomenonCell],
    claim_rows: tuple[EnvelopeClaimRow, ...],
) -> dict[str, EnvelopePhenomenonCell]:
    by_phenomenon: dict[str, list[str]] = {}
    for row in claim_rows:
        rule = _CLAIM_REQUIREMENTS.get(row.claim)
        phenomenon = rule.phenomenon if rule is not None else None
        if phenomenon is None:
            continue
        by_phenomenon.setdefault(phenomenon, []).append(
            f'{row.claim}:{row.verdict}'
        )
    for phenomenon, tokens in by_phenomenon.items():
        cell = cells.get(phenomenon)
        if cell is None:
            continue
        cells[phenomenon] = EnvelopePhenomenonCell(
            phenomenon=cell.phenomenon,
            capability_state=cell.capability_state,
            band=cell.band,
            bound_description=cell.bound_description,
            external_verdicts=cell.external_verdicts,
            external_evidence_class=cell.external_evidence_class,
            external_band=cell.external_band,
            claim_verdicts=tuple(tokens),
        )
    return cells


def compose_applicability_envelope(
    bundle: EnvelopeEvidenceBundle,
    *,
    context: EnvelopeContext,
    solver_envelopes: Sequence[AccuracyEnvelopeRecord] = (),
    evaluated_at_utc: str,
) -> ApplicabilityEnvelope:
    """Compose the seven-dimension envelope for one solver path/document.

    Fail-closed throughout: only sealed records in ``bundle`` count as
    evidence; nothing is inferred, and no aggregate score is computed.
    ``solver_envelopes`` carries the in-memory accuracy envelopes the
    caller resolved for this solver path (they are not persisted).
    """

    if bundle.document_id != context.document_id:
        raise ValueError('evidence bundle document does not match context')

    capability_dim, cells = _compose_capability(bundle, context)
    external_dim = _compose_external_validation(bundle, context, cells)
    verification_dim = _compose_verification(bundle, context, solver_envelopes)
    input_dim, claim_rows = _compose_input_qualification(bundle, context)
    owned_room_dim, ranking_verdict = _compose_owned_room(bundle, context)
    limitations_dim, model_form_discrepancy = _compose_uncertainty_limitations(
        bundle, context
    )

    # claim verdicts ride on the phenomenon cells via the declared
    # claim→phenomenon matrix (#811 _CLAIM_REQUIREMENTS).
    cells = _attach_claim_verdicts(cells, claim_rows)

    decisions = _compose_decisions(
        capability_dim, owned_room_dim, external_dim, ranking_verdict
    )

    # context_of_use dimension summarizes the decision rows. Its refs are
    # the real evidence consumed by the gated decisions plus a ref pinning
    # the declared context the decisions were computed against.
    decision_tokens = tuple(f'{d.decision}:{d.verdict}' for d in decisions)
    context_ref = AuthorityRef(
        kind='envelope_context',
        ref_id=context.document_id,
        ref_sha256=_hash(context.model_dump(mode='json')),
    )
    real_refs = _refs_union(
        capability_dim.evidence_refs,
        owned_room_dim.evidence_refs,
        external_dim.evidence_refs,
    )
    if not real_refs:
        context_class: EnvelopeEvidenceClass = 'insufficient_evidence'
    elif any(
        d.verdict in ('trend_only', 'not_supported', 'blocked')
        for d in decisions
    ):
        context_class = 'bounded'
    else:
        context_class = 'supported'
    context_dim = EnvelopeDimensionState(
        dimension='context_of_use',
        verdict='DECLARED',
        evidence_class=context_class,
        evidence_refs=real_refs + (context_ref,),
        caveats=decision_tokens,
    )

    fields: dict[str, Any] = dict(
        evaluated_at_utc=evaluated_at_utc,
        context=context,
        dimensions=(
            capability_dim,
            verification_dim,
            external_dim,
            input_dim,
            owned_room_dim,
            limitations_dim,
            context_dim,
        ),
        phenomenon_cells=tuple(cells[p] for p in SOLVER_PATH_PHENOMENA),
        claim_rows=claim_rows,
        decisions=decisions,
        ranking_verdict=ranking_verdict,
        model_form_discrepancy=model_form_discrepancy,
    )
    probe = ApplicabilityEnvelope.model_construct(
        **fields, envelope_hash='0' * 64
    )
    digest = _hash(probe.identity_payload())
    return ApplicabilityEnvelope(**fields, envelope_hash=digest)


# ---------------------------------------------------------------------------
# Report / export (#7: applicability envelope exports retain identity)
# ---------------------------------------------------------------------------


class EnvelopeBenchmarkIdentity(BaseModel):
    """Exact validation-evidence identity pinned into a report (#7)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    benchmark_id: str = Field(min_length=1)
    benchmark_version: str = Field(min_length=1)
    benchmark_sha256: str = Field(pattern=_SHA256_PATTERN)
    provider_id: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    qualification_id: str = Field(min_length=1)


class ApplicabilityEnvelopeReport(BaseModel):
    """A sealed export snapshot of one composed envelope (``aer-``).

    The report binds the exact solver/adapter identity, every evidence
    ref consumed, each bound benchmark's id/version/hash, and the full
    envelope payload — with the same prohibition: no aggregate score.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal['applicability-envelope-report-1'] = (
        APPLICABILITY_ENVELOPE_REPORT_VERSION
    )
    report_id: str = Field(pattern=r'^aer-[0-9a-f]{24}$')
    report_sha256: str = Field(pattern=_SHA256_PATTERN)
    generated_at_utc: str = Field(min_length=1)
    code_version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    adapter_id: str | None = None
    adapter_version: str | None = None
    solver_path: str | None = None
    envelope_hash: str = Field(pattern=_SHA256_PATTERN)
    envelope_payload: dict[str, Any]
    benchmark_identities: tuple[EnvelopeBenchmarkIdentity, ...] = ()
    evidence_refs: tuple[AuthorityRef, ...] = ()

    @model_validator(mode='after')
    def _validate(self) -> 'ApplicabilityEnvelopeReport':
        if self.report_sha256 != _hash(self.identity_payload()):
            raise ValueError('applicability envelope report hash mismatch')
        expected_id = f'aer-{self.report_sha256[:24]}'
        if self.report_id != expected_id:
            raise ValueError('applicability envelope report id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'report_id', 'report_sha256'}
        )

    def export_payload(self) -> dict[str, Any]:
        """The machine-readable export structure (JSON-serializable)."""
        return self.model_dump(mode='json')

    def render_text(self) -> str:
        """The JA human-readable report body."""
        return render_envelope_report_text(self)


def envelope_evidence_refs(
    envelope: ApplicabilityEnvelope,
) -> tuple[AuthorityRef, ...]:
    """Flatten every evidence ref consumed across the envelope."""
    groups = [d.evidence_refs for d in envelope.dimensions]
    groups.extend(d.basis_refs for d in envelope.decisions)
    groups.extend(row.supporting_refs for row in envelope.claim_rows)
    return _refs_union(*groups)


def build_envelope_report(
    envelope: ApplicabilityEnvelope,
    *,
    generated_at_utc: str,
    code_version: str = APPLICABILITY_ENVELOPE_COMPOSER_VERSION,
    preregistrations: Sequence[BenchmarkPreregistration] = (),
    qualifications: Sequence[BenchmarkQualification] = (),
) -> ApplicabilityEnvelopeReport:
    """Seal the composed envelope into an export report.

    ``preregistrations``/``qualifications`` re-attach the exact benchmark
    identities (id/version/hash + solver provider id/version) that #7 of
    the issue requires on every report surface.
    """

    preregs = {p.preregistration_id: p for p in preregistrations}
    identities: list[EnvelopeBenchmarkIdentity] = []
    for qualification in qualifications:
        prereg = preregs.get(qualification.preregistration_ref.ref_id)
        if prereg is None:
            continue
        identities.append(
            EnvelopeBenchmarkIdentity(
                benchmark_id=prereg.benchmark_id,
                benchmark_version=prereg.benchmark_version,
                benchmark_sha256=prereg.benchmark_sha256,
                provider_id=prereg.provider_id,
                provider_version=prereg.provider_version,
                qualification_id=qualification.qualification_id,
            )
        )
    fields: dict[str, Any] = dict(
        generated_at_utc=generated_at_utc,
        code_version=code_version,
        document_id=envelope.context.document_id,
        adapter_id=envelope.context.adapter_id,
        adapter_version=envelope.context.adapter_version,
        solver_path=envelope.context.solver_path,
        envelope_hash=envelope.envelope_hash,
        envelope_payload=envelope.model_dump(mode='json'),
        benchmark_identities=tuple(identities),
        evidence_refs=envelope_evidence_refs(envelope),
    )
    probe = ApplicabilityEnvelopeReport.model_construct(
        **fields, report_id='aer-' + '0' * 24, report_sha256='0' * 64
    )
    digest = _hash(probe.identity_payload())
    return ApplicabilityEnvelopeReport(
        **fields,
        report_id=f'aer-{digest[:24]}',
        report_sha256=digest,
    )


# ---------------------------------------------------------------------------
# JA render lines (shared by the UI surface and the report export)
# ---------------------------------------------------------------------------


def envelope_dimension_line(state: EnvelopeDimensionState) -> str:
    """One JA line per dimension, e.g.
    ``外部検証（独立ベンチマーク）: 宣言領域内パス（外部実測検証済み）``."""

    label = envelope_dimension_label(state.dimension)
    if state.evidence_class == 'absent':
        return f'{label}: {ENVELOPE_CLASS_LABELS["absent"]}'
    verdict = (
        envelope_verdict_label(state.verdict)
        if state.verdict is not None
        else ENVELOPE_CLASS_LABELS[state.evidence_class]
    )
    line = (
        f'{label}: {verdict}'
        f'（{envelope_class_label(state.evidence_class)}）'
    )
    extras: list[str] = []
    if state.band is not None:
        extras.append(_band_label(state.band))
    if state.conflicts:
        extras.append(
            '他判定: '
            + '、'.join(
                envelope_verdict_label(v) for v in state.conflicts
            )
        )
    if state.staleness_notes:
        extras.append('陳腐化: ' + '；'.join(state.staleness_notes))
    if extras:
        line += ' — ' + '；'.join(extras)
    return line


def envelope_decision_line(row: EnvelopeDecisionRow) -> str:
    label = envelope_decision_label(row.decision)
    verdict = envelope_decision_verdict_label(row.verdict)
    basis = '、'.join(row.basis_states)
    return f'{label}: {verdict}（{basis}）' if basis else f'{label}: {verdict}'


def envelope_lines(envelope: ApplicabilityEnvelope) -> tuple[str, ...]:
    """Every JA line for the envelope — dimensions, decisions, staleness."""
    lines = [envelope_dimension_line(d) for d in envelope.dimensions]
    lines.extend(envelope_decision_line(d) for d in envelope.decisions)
    if envelope.model_form_discrepancy:
        lines.append(
            'モデル形式乖離が疑われます（model-form discrepancy suspected）'
        )
    return tuple(lines)


def render_envelope_report_text(report: ApplicabilityEnvelopeReport) -> str:
    """The full JA text body of a sealed report."""
    lines = [
        f'適用範囲エンベロープレポート: {report.document_id}',
        f'生成: {report.generated_at_utc} / {report.code_version}',
    ]
    if report.adapter_id is not None:
        lines.append(
            f'ソルバー: {report.adapter_id} {report.adapter_version}'
            + (
                f'（パス: {report.solver_path}）'
                if report.solver_path
                else ''
            )
        )
    if report.benchmark_identities:
        lines.append('ベンチマーク証拠:')
        for identity in report.benchmark_identities:
            lines.append(
                f'  - {identity.benchmark_id} v{identity.benchmark_version} '
                f'({identity.benchmark_sha256[:12]}…) '
                f'提供者 {identity.provider_id} v{identity.provider_version}'
            )
    for dimension in report.envelope_payload.get('dimensions', ()):
        state = EnvelopeDimensionState.model_validate(dimension)
        lines.append(envelope_dimension_line(state))
    for decision in report.envelope_payload.get('decisions', ()):
        row = EnvelopeDecisionRow.model_validate(decision)
        lines.append(envelope_decision_line(row))
    return '\n'.join(lines)
