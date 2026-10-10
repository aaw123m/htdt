"""Project assumption and evidence-gap register (#620).

Workflows across HTDT already fail closed on missing or unverified
evidence — overview notices, measurement-quality capability gates,
prediction staleness. What was missing is a single *enumerable* register
of "what is not known / assumed / inferred / unverified in this project"
that commissioning, reporting and support surfaces can read consistently.

:class:`ProjectEvidenceGapRegister` is a **derived read model**, never an
authority store: ``gaps(document_id)`` rebuilds the full register from
canonical persisted records on every call (same sources → same gaps),
classifying each entry with an explicit vocabulary:

* ``unknown`` — the value is absent and nothing was declared or inferred;
* ``missing_evidence`` — a required evidence step was never performed;
* ``assumed`` — a value is in use that was assumed, not evidenced;
* ``inferred`` — a value was derived from other evidence, not measured;
* ``unverified`` — evidence exists but has not been replay-validated;
* ``unsupported`` — the project's shape cannot be handled at all;
* ``unresolved_dependency`` — a referenced authority is absent or pending;
* ``stale`` — the evidence is real but pinned to an older authority state;
* ``user_attested`` — an explicit ``AssumptionDecision`` (#620) covers the
  gap; the record is listed, the limitation stays attached.

The register carries no confidence score and no global percentage: each
gap names its affected capabilities and the exact next action. Gaps are
context, not blockers — they never prevent unrelated workflows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_assumption_decision import (
    AssumptionDecision,
    active_assumption_decisions,
)
from .cad_construction_assembly import element_evidence
from .cad_measurement_disposition import MEASUREMENT_ELIGIBLE_DISPOSITIONS
from .cad_measurement_quality import gate_measurement_claim
from .cad_scene import is_unassigned_speaker_role
from .capture_inbox import capture_inbox_item_project_id
from .workflow_navigation import (
    ApplicationDestinationId,
    DestinationId,
    WorkspaceDeepLink,
    WorkspaceId,
)
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash


GAP_SCHEMA_VERSION = 1

EvidenceGapClassification = Literal[
    'unknown',
    'missing_evidence',
    'assumed',
    'inferred',
    'user_attested',
    'unverified',
    'unsupported',
    'unresolved_dependency',
    'stale',
]
EVIDENCE_GAP_CLASSIFICATIONS: frozenset[str] = frozenset(
    {
        'unknown',
        'missing_evidence',
        'assumed',
        'inferred',
        'user_attested',
        'unverified',
        'unsupported',
        'unresolved_dependency',
        'stale',
    }
)

#: Stable domain grouping — mirrors lifecycle surfaces, not a score axis.
EvidenceGapDomain = Literal[
    'room',
    'equipment',
    'measurement',
    'prediction',
    'calibration',
    'installation',
    'commissioning',
]
EVIDENCE_GAP_DOMAINS: frozenset[str] = frozenset(
    {
        'room',
        'equipment',
        'measurement',
        'prediction',
        'calibration',
        'installation',
        'commissioning',
    }
)






class EvidenceGapSubjectRef(BaseModel):
    """Exact typed pointer to the object a gap is about."""

    model_config = ConfigDict(frozen=True)

    kind: str = Field(min_length=1)
    ref_id: str = Field(min_length=1)
    ref_sha256: str | None = Field(default=None, min_length=8)
    label: str | None = Field(default=None, min_length=1)


class EvidenceGapResolution(BaseModel):
    """The concrete next action that would resolve or cover a gap."""

    model_config = ConfigDict(frozen=True)

    resolution_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    deep_link: str | None = Field(default=None, min_length=1)


class ProjectEvidenceGap(BaseModel):
    """One derived register entry — a named, classified evidence limitation.

    ``gap_id`` is deterministic: derived from the semantic payload, not
    persisted, so a rebuild on identical authority yields identical ids.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = GAP_SCHEMA_VERSION
    gap_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    domain: EvidenceGapDomain
    classification: EvidenceGapClassification
    subject: EvidenceGapSubjectRef | None = None
    summary: str = Field(min_length=1)
    #: What cannot be claimed while this gap stands — human-readable.
    limitation: str | None = None
    #: Capability ids this gap degrades (e.g. 'prediction',
    #: 'calibration.apply', 'measurement.timing_compare').
    affected_capabilities: tuple[str, ...] = ()
    resolutions: tuple[EvidenceGapResolution, ...] = ()
    source_refs: tuple[EvidenceGapSubjectRef, ...] = ()
    #: The AssumptionDecision ids covering this gap, when attested.
    assumption_decision_ids: tuple[str, ...] = ()
    #: The gap's own classification before attestation — attestation
    #: annotates, it never resolves the underlying limitation (#798).
    underlying_classification: str | None = None
    #: Human-readable attestation annotation, e.g.
    #: "analysis_study:S1 · until 2026-10-01".
    attestation_detail: str | None = None
    gap_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_gap(self) -> 'ProjectEvidenceGap':
        if len(set(self.affected_capabilities)) != len(self.affected_capabilities):
            raise ValueError('affected_capabilities must be unique')
        if len(set(self.assumption_decision_ids)) != len(self.assumption_decision_ids):
            raise ValueError('assumption_decision_ids must be unique')
        if self.classification == 'user_attested':
            if not self.assumption_decision_ids:
                raise ValueError('user_attested gaps must carry decision ids')
            if self.underlying_classification is None:
                raise ValueError(
                    'user_attested gaps must keep the underlying classification'
                )
        expected = 'gap:' + _hash(
            {
                'document_id': self.document_id,
                'domain': self.domain,
                'classification_basis': (
                    'user_attested' if self.assumption_decision_ids else self.classification
                ),
                'subject': (
                    None
                    if self.subject is None
                    else [self.subject.kind, self.subject.ref_id]
                ),
                'summary': self.summary,
            }
        )[:32]
        if self.gap_id != expected:
            raise ValueError('ProjectEvidenceGap id mismatch')
        if self.gap_sha256 != _hash(self.semantic_payload()):
            raise ValueError('ProjectEvidenceGap hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'gap_id': self.gap_id,
            'document_id': self.document_id,
            'domain': self.domain,
            'classification': self.classification,
            'subject': (
                None
                if self.subject is None
                else self.subject.model_dump(mode='json')
            ),
            'summary': self.summary,
            'limitation': self.limitation,
            'affected_capabilities': list(self.affected_capabilities),
            'resolutions': [
                item.model_dump(mode='json') for item in self.resolutions
            ],
            'source_refs': [
                item.model_dump(mode='json') for item in self.source_refs
            ],
            'assumption_decision_ids': list(self.assumption_decision_ids),
            'underlying_classification': self.underlying_classification,
            'attestation_detail': self.attestation_detail,
        }


def _link(workspace: DestinationId, context: str) -> str:
    return WorkspaceDeepLink(workspace=workspace, section=context).as_uri()


def _resolution(
    resolution_id: str,
    label: str,
    workspace: DestinationId,
    context: str,
) -> EvidenceGapResolution:
    return EvidenceGapResolution(
        resolution_id=resolution_id,
        label=label,
        deep_link=_link(workspace, context),
    )


class SceneReadSource(Protocol):
    def current_head(self, document_id: str): ...


class MeasurementReadSource(Protocol):
    def list_measurements(self, document_id: str) -> tuple: ...

    def dataset_for_measurement(self, measurement_id: str): ...


class MeasurementQualityReadSource(Protocol):
    def latest_report(self, measurement_id: str): ...


class PredictionReadSource(Protocol):
    def list_results(self, document_id: str) -> tuple: ...


class EquipmentBindingReadSource(Protocol):
    def get_binding_for_entity(self, document_id: str, entity_id: str): ...


class CaptureInboxReadSource(Protocol):
    def list_items(self) -> tuple: ...


class AssumptionDecisionReadSource(Protocol):
    def list_decisions(self, document_id: str) -> tuple[AssumptionDecision, ...]: ...


@dataclass(frozen=True, slots=True)
class EvidenceGapContext:
    """The evaluation context a register read is performed under (#798).

    An ``AssumptionDecision`` only covers a gap when its declared scope
    matches the context being read: project-scoped assumptions apply
    project-wide; study/checkpoint/commissioning-run/custom assumptions
    apply only while evaluating that exact bounded context.

    ``as_of_utc`` is the evaluation instant — current UI passes the current
    time so expired assumptions stop applying; historical evaluations pass
    the original decision/report time to reproduce what was applicable.
    """

    as_of_utc: str | None = None
    design_checkpoint_id: str | None = None
    analysis_study_id: str | None = None
    commissioning_run_id: str | None = None
    custom_scope_ref_id: str | None = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _attestation_annotation(decision: AssumptionDecision) -> str:
    """Presentation annotation: scope + expiry stay visible (#798)."""

    scope_label = decision.decision_scope
    scope_ref_id = (
        None if decision.scope_ref is None else decision.scope_ref.ref_id
    )
    if scope_ref_id is not None:
        scope_label = f'{scope_label}:{scope_ref_id}'
    elif decision.decision_scope == 'custom' and decision.custom_scope_label:
        scope_label = f'{scope_label}:{decision.custom_scope_label}'
    if decision.expires_at_utc is not None:
        return f'{scope_label} · until {decision.expires_at_utc}'
    return scope_label


def _scope_applies(
    decision: AssumptionDecision,
    context: EvidenceGapContext,
) -> bool:
    """Whether a scoped assumption covers the requested context (#798)."""

    if decision.decision_scope == 'project':
        return True
    scope_ref_id = (
        None
        if decision.scope_ref is None
        else decision.scope_ref.ref_id
    )
    if decision.decision_scope == 'design_checkpoint':
        return (
            context.design_checkpoint_id is not None
            and context.design_checkpoint_id == scope_ref_id
        )
    if decision.decision_scope == 'analysis_study':
        return (
            context.analysis_study_id is not None
            and context.analysis_study_id == scope_ref_id
        )
    if decision.decision_scope == 'commissioning_run':
        return (
            context.commissioning_run_id is not None
            and context.commissioning_run_id == scope_ref_id
        )
    # custom scope: bounded to an explicit scope ref or label.
    if context.custom_scope_ref_id is None:
        return False
    if scope_ref_id is not None:
        return context.custom_scope_ref_id == scope_ref_id
    return context.custom_scope_ref_id == decision.custom_scope_label


_ROOM_ELEMENTS: tuple[tuple[str, str], ...] = (
    ('floor', 'floor'),
    ('ceiling', 'ceiling'),
)


def _gap(
    *,
    document_id: str,
    domain: str,
    classification: str,
    summary: str,
    limitation: str | None = None,
    subject: EvidenceGapSubjectRef | None = None,
    affected_capabilities: tuple[str, ...] = (),
    resolutions: tuple[EvidenceGapResolution, ...] = (),
    source_refs: tuple[EvidenceGapSubjectRef, ...] = (),
    attestations: tuple[tuple[str, str], ...] = (),
) -> ProjectEvidenceGap:
    assumption_decision_ids = tuple(
        decision_id for decision_id, _detail in attestations
    )
    underlying: str | None = None
    attestation_detail: str | None = None
    if assumption_decision_ids:
        # Attestation annotates: the underlying classification and the
        # scoped decisions stay visible instead of a stronger-looking
        # generic state (#798).
        underlying = classification
        classification = 'user_attested'
        attestation_detail = '; '.join(
            detail for _decision_id, detail in attestations
        )
    gap_id = 'gap:' + _hash(
        {
            'document_id': document_id,
            'domain': domain,
            'classification_basis': (
                'user_attested' if assumption_decision_ids else classification
            ),
            'subject': (
                None if subject is None else [subject.kind, subject.ref_id]
            ),
            'summary': summary,
        }
    )[:32]
    payload: dict[str, Any] = {
        'gap_id': gap_id,
        'document_id': document_id,
        'domain': domain,
        'classification': classification,
        'subject': subject,
        'summary': summary,
        'limitation': limitation,
        'affected_capabilities': tuple(affected_capabilities),
        'resolutions': tuple(resolutions),
        'source_refs': tuple(source_refs),
        'assumption_decision_ids': tuple(assumption_decision_ids),
        'underlying_classification': underlying,
        'attestation_detail': attestation_detail,
    }
    provisional = ProjectEvidenceGap.model_construct(
        **payload, gap_sha256='0' * 64
    )
    return ProjectEvidenceGap(
        **payload,
        gap_sha256=_hash(provisional.semantic_payload()),
    )


class ProjectEvidenceGapRegister:
    """Rebuildable assumption/evidence-gap register for one project.

    Read-only: every call derives from the injected canonical sources plus
    active AssumptionDecisions. Never blocks a workflow, never mutates
    authority, never emits a global score.
    """

    def __init__(
        self,
        scene_source: SceneReadSource,
        measurement_source: MeasurementReadSource | None = None,
        prediction_source: PredictionReadSource | None = None,
        quality_source: MeasurementQualityReadSource | None = None,
        equipment_source: EquipmentBindingReadSource | None = None,
        inbox_source: CaptureInboxReadSource | None = None,
        assumption_source: AssumptionDecisionReadSource | None = None,
    ) -> None:
        self._scene_source = scene_source
        self._measurement_source = measurement_source
        self._prediction_source = prediction_source
        self._quality_source = quality_source
        self._equipment_source = equipment_source
        self._inbox_source = inbox_source
        self._assumption_source = assumption_source

    # -- public read ------------------------------------------------------

    def gaps(
        self,
        document_id: str,
        *,
        context: EvidenceGapContext | None = None,
        domain: str | None = None,
        classification: str | None = None,
        subject_ref_id: str | None = None,
    ) -> tuple[ProjectEvidenceGap, ...]:
        """Rebuild the register for ``document_id`` with optional filters.

        ``context`` bounds which AssumptionDecisions may cover a gap (#798):
        project-wide reads pass no bounded scope ids and evaluate expiry at
        the current instant; bounded study/checkpoint/run evaluations pass
        their scope id plus an explicit ``as_of_utc``.
        """

        context = context or EvidenceGapContext()
        decisions, assumption_issue = self._active_decisions(
            document_id, context
        )
        gaps: list[ProjectEvidenceGap] = []
        if assumption_issue is not None:
            # #869 G: a corrupt assumption record must surface as an
            # explicit integrity gap, not silently attest or take down the
            # register. The corrupt attestation is withheld; every other
            # domain still derives normally.
            gaps.append(
                _gap(
                    document_id=document_id,
                    domain='commissioning',
                    classification='unverified',
                    subject=EvidenceGapSubjectRef(
                        kind='document', ref_id=document_id
                    ),
                    summary='仮定判定の記録が整合しません。',
                    limitation=(
                        '破損した仮定レコードは証明として適用されません。'
                        '記録を点検してください。'
                    ),
                    affected_capabilities=('prediction', 'calibration'),
                    resolutions=(
                        _resolution(
                            'assumptions.review',
                            '仮定判定を確認',
                            WorkspaceId.MEASUREMENT,
                            'import',
                        ),
                    ),
                    source_refs=(
                        EvidenceGapSubjectRef(
                            kind='document', ref_id=document_id
                        ),
                    ),
                )
            )
        gaps.extend(self._room_gaps(document_id, decisions))
        gaps.extend(self._equipment_gaps(document_id, decisions))
        gaps.extend(self._measurement_gaps(document_id, decisions))
        gaps.extend(self._prediction_gaps(document_id, decisions))
        gaps.extend(self._inbox_gaps(document_id, decisions))
        out = tuple(gaps)
        if domain is not None:
            out = tuple(item for item in out if item.domain == domain)
        if classification is not None:
            out = tuple(
                item for item in out if item.classification == classification
            )
        if subject_ref_id is not None:
            out = tuple(
                item
                for item in out
                if item.subject is not None
                and item.subject.ref_id == subject_ref_id
            )
        return out

    def gap_for_subject(
        self,
        document_id: str,
        kind: str,
        ref_id: str,
        *,
        context: EvidenceGapContext | None = None,
    ) -> ProjectEvidenceGap | None:
        """First gap on one exact subject (kind + ref_id), if any."""

        for gap in self.gaps(document_id, context=context):
            if (
                gap.subject is not None
                and gap.subject.kind == kind
                and gap.subject.ref_id == ref_id
            ):
                return gap
        return None

    # -- derivation -------------------------------------------------------

    def _active_decisions(
        self,
        document_id: str,
        context: EvidenceGapContext,
    ) -> tuple[
        tuple[tuple[AssumptionDecision, EvidenceGapContext], ...],
        str | None,
    ]:
        """(applicable scoped decisions, integrity issue or None).

        #869 G: a malformed/corrupt assumption store must not make the
        whole register unavailable — the issue text is returned so the
        caller can surface it as an explicit gap while derivation
        continues without the corrupt attestation.
        """

        if self._assumption_source is None:
            return (), None
        as_of_utc = context.as_of_utc or _utc_now_iso()
        try:
            stored = tuple(self._assumption_source.list_decisions(document_id))
            active = active_assumption_decisions(stored, as_of_utc=as_of_utc)
        except ValueError as exc:
            return (), str(exc)
        return tuple(
            (decision, context)
            for decision in active
            if _scope_applies(decision, context)
        ), None

    def _attestation(
        self,
        decisions: tuple[tuple[AssumptionDecision, EvidenceGapContext], ...],
        *,
        subject_kind: str,
        subject_ref_id: str,
        classification: str,
    ) -> tuple[tuple[str, str], ...]:
        """(decision_id, annotation) covering this (subject, classification)."""

        return tuple(
            (decision.decision_id, _attestation_annotation(decision))
            for decision, _context in decisions
            if decision.subject.kind == subject_kind
            and decision.subject.ref_id == subject_ref_id
            and decision.attested_classification == classification
        )

    def _room_gaps(
        self,
        document_id: str,
        decisions: tuple[AssumptionDecision, ...],
    ) -> list[ProjectEvidenceGap]:
        gaps: list[ProjectEvidenceGap] = []
        revision = self._scene_source.current_head(document_id)
        if revision is None:
            gaps.append(
                _gap(
                    document_id=document_id,
                    domain='room',
                    classification='missing_evidence',
                    summary='部屋データがありません。',
                    limitation='部屋がない間、予測・測定・校正は成立しません。',
                    affected_capabilities=('prediction', 'measurement', 'calibration'),
                    resolutions=(
                        _resolution(
                            'room.create', '部屋を作成',
                            WorkspaceId.ROOM, 'geometry',
                        ),
                    ),
                    source_refs=(
                        EvidenceGapSubjectRef(kind='document', ref_id=document_id),
                    ),
                )
            )
            return gaps
        document = revision.document
        if document.room is None:
            gaps.append(
                _gap(
                    document_id=document_id,
                    domain='room',
                    classification='missing_evidence',
                    summary='部屋形状がまだありません。',
                    limitation='形状がない間、音響予測は成立しません。',
                    affected_capabilities=('prediction',),
                    resolutions=(
                        _resolution(
                            'room.complete_geometry', '部屋を完成させる',
                            WorkspaceId.ROOM, 'geometry',
                        ),
                    ),
                    source_refs=(
                        EvidenceGapSubjectRef(kind='document', ref_id=document_id),
                    ),
                )
            )
        elements: list[tuple[str, str]] = list(_ROOM_ELEMENTS)
        if document.wall_topology is not None:
            elements.extend(
                ('wall', wall.wall_id)
                for wall in document.wall_topology.walls
            )
        for element, element_ref in elements:
            evidence = element_evidence(document, element, element_ref)
            subject = EvidenceGapSubjectRef(
                kind='room_surface',
                ref_id=f'{element}:{element_ref}',
                label=f'{element}:{element_ref}',
            )
            attest_assumed = self._attestation(
                decisions,
                subject_kind='room_surface',
                subject_ref_id=subject.ref_id,
                classification='assumed',
            )
            attest_unknown = self._attestation(
                decisions,
                subject_kind='room_surface',
                subject_ref_id=subject.ref_id,
                classification='unknown',
            )
            if evidence.evidence_source == 'assumed':
                gaps.append(
                    _gap(
                        document_id=document_id,
                        domain='room',
                        classification='assumed',
                        subject=subject,
                        summary=f'{element}:{element_ref} の構造は未登録です。',
                        limitation='構造が不明なため、この表面の音響特性は仮定値です。',
                        affected_capabilities=('prediction',),
                        resolutions=(
                            _resolution(
                                'room.declare_assembly', '構造を登録',
                                WorkspaceId.ROOM, 'acoustics',
                            ),
                        ),
                        attestations=attest_assumed,
                    )
                )
            elif evidence.substrate_material == 'unknown':
                gaps.append(
                    _gap(
                        document_id=document_id,
                        domain='room',
                        classification='unknown',
                        subject=subject,
                        summary=f'{element}:{element_ref} の下地材が不明です。',
                        limitation='下地が不明なため、低音域の吸音は評価できません。',
                        affected_capabilities=('prediction',),
                        resolutions=(
                            _resolution(
                                'room.review_assembly', '構造を確認',
                                WorkspaceId.ROOM, 'acoustics',
                            ),
                        ),
                        attestations=attest_unknown,
                    )
                )
        return gaps

    def _equipment_gaps(
        self,
        document_id: str,
        decisions: tuple[AssumptionDecision, ...],
    ) -> list[ProjectEvidenceGap]:
        gaps: list[ProjectEvidenceGap] = []
        revision = self._scene_source.current_head(document_id)
        if revision is None:
            return gaps
        speakers = tuple(
            entity
            for entity in revision.document.entities
            if entity.kind == 'speaker'
        )
        for speaker in speakers:
            subject = EvidenceGapSubjectRef(
                kind='scene_entity',
                ref_id=speaker.entity_id,
                label=speaker.name or speaker.entity_id,
            )
            if is_unassigned_speaker_role(speaker.speaker_role):
                attest = self._attestation(
                    decisions,
                    subject_kind='scene_entity',
                    subject_ref_id=speaker.entity_id,
                    classification='unknown',
                )
                gaps.append(
                    _gap(
                        document_id=document_id,
                        domain='equipment',
                        classification='unknown',
                        subject=subject,
                        summary='スピーカーの役割が未設定です。',
                        limitation='チャンネル識別がないため、測定と校正に紐付きません。',
                        affected_capabilities=('measurement', 'calibration'),
                        resolutions=(
                            _resolution(
                                'room.assign_speaker_role', '役割を設定',
                                WorkspaceId.ROOM, 'placement',
                            ),
                        ),
                        attestations=attest,
                    )
                )
            if self._equipment_source is None:
                continue
            binding = self._equipment_source.get_binding_for_entity(
                document_id, speaker.entity_id
            )
            if binding is None:
                attest = self._attestation(
                    decisions,
                    subject_kind='scene_entity',
                    subject_ref_id=speaker.entity_id,
                    classification='missing_evidence',
                )
                gaps.append(
                    _gap(
                        document_id=document_id,
                        domain='equipment',
                        classification='missing_evidence',
                        subject=subject,
                        summary='機材・ソースモデルが未設定です。',
                        limitation='指向性・音圧の予測精度が制限されます。',
                        affected_capabilities=('prediction', 'calibration'),
                        resolutions=(
                            _resolution(
                                'equipment.bind', '機材を設定',
                                WorkspaceId.ROOM, 'objects',
                            ),
                        ),
                        attestations=attest,
                    )
                )
                continue
            evidence_kinds = {
                getattr(item, 'evidence_kind', 'unknown')
                for item in getattr(binding, 'provenance', ())
            }
            if 'inferred' in evidence_kinds:
                attest = self._attestation(
                    decisions,
                    subject_kind='scene_entity',
                    subject_ref_id=speaker.entity_id,
                    classification='inferred',
                )
                gaps.append(
                    _gap(
                        document_id=document_id,
                        domain='equipment',
                        classification='inferred',
                        subject=subject,
                        summary='機材データに推定値が含まれます。',
                        limitation='指向性・音圧の一部は実測ではなく推定です。',
                        affected_capabilities=('prediction',),
                        resolutions=(
                            _resolution(
                                'equipment.review', '機材データを確認',
                                WorkspaceId.ROOM, 'objects',
                            ),
                        ),
                        attestations=attest,
                    )
                )
        return gaps

    def _measurement_gaps(
        self,
        document_id: str,
        decisions: tuple[AssumptionDecision, ...],
    ) -> list[ProjectEvidenceGap]:
        if self._measurement_source is None:
            return []
        gaps: list[ProjectEvidenceGap] = []
        measurements = tuple(self._measurement_source.list_measurements(document_id))
        # #867: lifecycle eligibility, not row presence — measurements
        # dispositioned misassigned/excluded/test_only/duplicate_import
        # stay inspectable history but are not current evidence; they can
        # neither produce repair-the-evidence gaps nor satisfy
        # "measurement evidence exists".
        eligible: list[tuple[Any, Any]] = []
        for measurement in measurements:
            lifecycle = self._measurement_lifecycle(measurement.measurement_id)
            if (
                lifecycle is not None
                and lifecycle[0] not in MEASUREMENT_ELIGIBLE_DISPOSITIONS
            ):
                continue
            eligible.append((measurement, lifecycle))
        if not eligible:
            attest = self._attestation(
                decisions,
                subject_kind='document',
                subject_ref_id=document_id,
                classification='missing_evidence',
            )
            gaps.append(
                _gap(
                    document_id=document_id,
                    domain='measurement',
                    classification='missing_evidence',
                    summary='測定データがありません。',
                    limitation='予測検証・校正の実測根拠がありません。',
                    affected_capabilities=('prediction', 'calibration'),
                    resolutions=(
                        _resolution(
                            'measurement.import', '測定を読み込む',
                            WorkspaceId.MEASUREMENT, 'import',
                        ),
                    ),
                    source_refs=(
                        EvidenceGapSubjectRef(kind='document', ref_id=document_id),
                    ),
                    attestations=attest,
                )
            )
            return gaps
        for measurement, lifecycle in eligible:
            corrected = (
                lifecycle is not None and lifecycle[0] == 'corrected'
            )
            correction_id = lifecycle[1] if lifecycle is not None else None
            correction_refs: tuple[EvidenceGapSubjectRef, ...] = ()
            if corrected and correction_id is not None:
                # #867 C: a corrected measurement keeps its exact
                # acquisition/dataset evidence; the correction record is
                # named alongside so assignment/pose caveats stay attached
                # to the gap instead of being silently rewritten.
                correction_refs = (
                    EvidenceGapSubjectRef(
                        kind='measurement_correction',
                        ref_id=correction_id,
                        label='補正記録',
                    ),
                )
            subject = EvidenceGapSubjectRef(
                kind='measurement',
                ref_id=measurement.measurement_id,
                label=getattr(measurement, 'label', measurement.measurement_id),
            )
            dataset = self._measurement_source.dataset_for_measurement(
                measurement.measurement_id
            )
            if dataset is None:
                attest = self._attestation(
                    decisions,
                    subject_kind='measurement',
                    subject_ref_id=measurement.measurement_id,
                    classification='unresolved_dependency',
                )
                gaps.append(
                    _gap(
                        document_id=document_id,
                        domain='measurement',
                        classification='unresolved_dependency',
                        subject=subject,
                        summary='測定データセットが見つかりません。',
                        limitation='測定内容を評価できません。',
                        affected_capabilities=('measurement',),
                        resolutions=(
                            _resolution(
                                'measurement.reingest', 'データを再取り込み',
                                WorkspaceId.MEASUREMENT, 'import',
                            ),
                        ),
                        source_refs=correction_refs,
                        attestations=attest,
                    )
                )
                continue
            report = (
                self._quality_source.latest_report(measurement.measurement_id)
                if self._quality_source is not None
                else None
            )
            if report is None or report.dataset_id != dataset.dataset_id:
                attest = self._attestation(
                    decisions,
                    subject_kind='measurement',
                    subject_ref_id=measurement.measurement_id,
                    classification='unverified',
                )
                gaps.append(
                    _gap(
                        document_id=document_id,
                        domain='measurement',
                        classification='unverified',
                        subject=subject,
                        summary='測定品質が未検証です。',
                        limitation='品質レポートがないため、タイミング・位相の能力は不明です。',
                        affected_capabilities=('measurement', 'calibration'),
                        resolutions=(
                            _resolution(
                                'measurement.review_quality', '品質を確認',
                                WorkspaceId.MEASUREMENT, 'quality',
                            ),
                        ),
                        source_refs=correction_refs,
                        attestations=attest,
                    )
                )
                continue
            timing = gate_measurement_claim(report, 'common_timing').decision
            if timing != 'ALLOWED':
                attest = self._attestation(
                    decisions,
                    subject_kind='measurement',
                    subject_ref_id=measurement.measurement_id,
                    classification='unknown',
                )
                gaps.append(
                    _gap(
                        document_id=document_id,
                        domain='measurement',
                        classification='unknown',
                        subject=subject,
                        summary='共通タイミング基準が確認できません。',
                        limitation='測定間のタイミング/位相比較には品質確認が必要です。',
                        affected_capabilities=('measurement', 'calibration'),
                        resolutions=(
                            _resolution(
                                'measurement.review_quality', '品質を確認',
                                WorkspaceId.MEASUREMENT, 'quality',
                            ),
                        ),
                        source_refs=correction_refs,
                        attestations=attest,
                    )
                )
        return gaps

    def _measurement_lifecycle(
        self, measurement_id: str
    ) -> tuple[str, str | None] | None:
        """(disposition, correction_id) or None when no source knows.

        Uses the canonical #509 ``latest_disposition`` contract wherever a
        source exposes it; a measurement with no recorded disposition is
        normally eligible (``active``). Sources without the lookup keep
        legacy behavior — storage presence only.
        """

        for source in (self._quality_source, self._measurement_source):
            lookup = getattr(source, 'latest_disposition', None)
            if lookup is None:
                continue
            record = lookup(measurement_id)
            if record is None:
                return 'active', None
            return (
                getattr(record, 'disposition', 'active'),
                getattr(record, 'correction_id', None),
            )
        return None

    def _prediction_gaps(
        self,
        document_id: str,
        decisions: tuple[AssumptionDecision, ...],
    ) -> list[ProjectEvidenceGap]:
        if self._prediction_source is None:
            return []
        gaps: list[ProjectEvidenceGap] = []
        revision = self._scene_source.current_head(document_id)
        if revision is None:
            return gaps
        results = tuple(
            result
            for result in self._prediction_source.list_results(document_id)
            if getattr(result, 'status', None) == 'completed'
        )
        current = tuple(
            result
            for result in results
            if result.scene_revision_id == revision.revision_id
            and result.scene_content_hash == revision.content_hash
            and getattr(result, 'geometry_compatibility', None) != 'unsupported'
        )
        unsupported = tuple(
            result
            for result in results
            if result.scene_revision_id == revision.revision_id
            and result.scene_content_hash == revision.content_hash
            and getattr(result, 'geometry_compatibility', None) == 'unsupported'
        )
        subject = EvidenceGapSubjectRef(
            kind='document', ref_id=document_id, label='予測'
        )
        if unsupported and not current:
            attest = self._attestation(
                decisions,
                subject_kind='document',
                subject_ref_id=document_id,
                classification='unsupported',
            )
            gaps.append(
                _gap(
                    document_id=document_id,
                    domain='prediction',
                    classification='unsupported',
                    subject=subject,
                    summary='現在の部屋形状は予測モデルに対応していません。',
                    limitation='この形状では音響予測を実行できません。',
                    affected_capabilities=('prediction',),
                    resolutions=(
                        _resolution(
                            'prediction.review_geometry', '部屋形状を確認',
                            WorkspaceId.ROOM, 'geometry',
                        ),
                    ),
                    attestations=attest,
                )
            )
        elif not current:
            if results:
                attest = self._attestation(
                    decisions,
                    subject_kind='document',
                    subject_ref_id=document_id,
                    classification='stale',
                )
                gaps.append(
                    _gap(
                        document_id=document_id,
                        domain='prediction',
                        classification='stale',
                        subject=subject,
                        summary='予測が最新の部屋に追従していません。',
                        limitation='表示中の予測は古い部屋形状に基づきます。',
                        affected_capabilities=('prediction', 'optimization'),
                        resolutions=(
                            _resolution(
                                'prediction.run', '予測を再計算',
                                WorkspaceId.ROOM, 'acoustics',
                            ),
                        ),
                        attestations=attest,
                    )
                )
            else:
                attest = self._attestation(
                    decisions,
                    subject_kind='document',
                    subject_ref_id=document_id,
                    classification='unknown',
                )
                gaps.append(
                    _gap(
                        document_id=document_id,
                        domain='prediction',
                        classification='unknown',
                        subject=subject,
                        summary='予測をまだ実行していません。',
                        limitation='部屋の音響挙動は未評価です。',
                        affected_capabilities=('prediction', 'optimization'),
                        resolutions=(
                            _resolution(
                                'prediction.run', '予測を実行',
                                WorkspaceId.ROOM, 'acoustics',
                            ),
                        ),
                        attestations=attest,
                    )
                )
        return gaps

    def _inbox_gaps(
        self,
        document_id: str,
        decisions: tuple[AssumptionDecision, ...],
    ) -> list[ProjectEvidenceGap]:
        if self._inbox_source is None:
            return []
        gaps: list[ProjectEvidenceGap] = []
        for item in self._inbox_source.list_items():
            # Canonical Capture Inbox scoping (#798/#737): an item belongs
            # to a project only through its assigned ``scope`` (a document
            # id). ``capture-inbox-unassigned`` and foreign scopes never
            # project into this project's register.
            if capture_inbox_item_project_id(item) != document_id:
                continue
            item_id = getattr(item, 'inbox_item_id', None) or getattr(item, 'id', '')
            if not item_id:
                continue
            # #867: the canonical #589 disposition lifecycle decides whether
            # the item is a *current* unresolved gap — row presence is not
            # evidence of pending work.
            disposition = getattr(item, 'disposition', 'pending') or 'pending'
            if disposition in ('promoted', 'rejected', 'superseded'):
                continue
            subject = EvidenceGapSubjectRef(
                kind='capture_inbox_item', ref_id=str(item_id),
                label=getattr(item, 'title', str(item_id)),
            )
            attest = self._attestation(
                decisions,
                subject_kind='capture_inbox_item',
                subject_ref_id=str(item_id),
                classification='unresolved_dependency',
            )
            if disposition == 'partially_promoted':
                remaining = self._inbox_remaining_kinds(item)
                summary = '一部のキャプチャ権威がまだ昇格していません。'
                limitation = (
                    '未昇格の権威があります'
                    + (f'（{", ".join(remaining)}）' if remaining else '')
                    + '。昇格済みの部分のみ証拠として使えます。'
                )
            elif disposition == 'deferred':
                # Deferred is a documented park, not a fresh blocker —
                # lower prominence until work resumes.
                summary = '取り込みが保留されています。'
                limitation = (
                    '保留中の取り込みは証拠として使えません。'
                    '必要になった時点で処理を再開してください。'
                )
            else:
                summary = '取り込み待ちのキャプチャがあります。'
                limitation = '未処理の取り込みは証拠として使えません。'
            gaps.append(
                _gap(
                    document_id=document_id,
                    domain='measurement',
                    classification='unresolved_dependency',
                    subject=subject,
                    summary=summary,
                    limitation=limitation,
                    affected_capabilities=('measurement',),
                    resolutions=(
                        _resolution(
                            'inbox.triage', '取り込みを処理',
                            ApplicationDestinationId.INBOX, 'inbox',
                        ),
                    ),
                    attestations=attest,
                )
            )
        return gaps

    def _inbox_remaining_kinds(self, item) -> tuple[str, ...]:
        """Authority kinds still un-promoted for a partial item (#867).

        Derived from the source's inspection when it exposes one; an
        uninspectable source returns ``()`` and the gap stays generic —
        the register never fabricates kind names.
        """

        inspect = getattr(self._inbox_source, 'inspect', None)
        lineage = getattr(item, 'lineage_digest', None)
        if inspect is None or lineage is None:
            return ()
        try:
            inspection = inspect(lineage)
        except Exception:  # error-boundary: inspection probe — a failed inspect yields no remaining kinds honestly, never fabricated ones (noqa: BLE001)
            return ()
        if inspection is None:
            return ()
        available = set(getattr(inspection, 'available_authority_kinds', ()))
        promoted = set(getattr(inspection, 'promoted_authority_kinds', ()))
        blocked = set(getattr(inspection, 'blocked_authority_kinds', ()))
        return tuple(sorted((available - promoted) | blocked))


__all__ = [
    'EVIDENCE_GAP_CLASSIFICATIONS',
    'EVIDENCE_GAP_DOMAINS',
    'GAP_SCHEMA_VERSION',
    'EvidenceGapClassification',
    'EvidenceGapContext',
    'EvidenceGapDomain',
    'EvidenceGapResolution',
    'EvidenceGapSubjectRef',
    'ProjectEvidenceGap',
    'ProjectEvidenceGapRegister',
]
