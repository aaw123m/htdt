"""Multi-user collaboration / approval authority (issue #721).

Immutable revisions, evidence hashes and commissioning records are not
sufficient when several designers, installers, reviewers and service
technicians can edit or approve a project: authorship, review state,
conflict handling and permitted-use semantics must themselves be
versioned authority. This module adds that layer.

Basis: ISO 19650-1:2018 and ISO 19650-2:2018 (information management —
exchanging, recording, versioning and organizing asset information;
status codes controlling permitted use; explicit approval semantics)
are architecture inspiration only — HTDT does not claim BIM/ISO
compliance, and ISO/DIS 19650 Edition 2 drafts remain research-only
until published. The collaboration product boundary (#730,
docs/COLLABORATION_PRODUCT_BOUNDARY.md) still applies: bounded exchange
and review, no accounts/cloud sync/CRDT co-editing — these records are
the *authority* for what happened, who did it and what it permits, not
a live editing service.

Composition: ``cad_repository.SceneRevision`` supplies the revision DAG
(this module binds authors to it and never rewrites history);
``cad_review_note`` (#730) is the bounded review-comment primitive this
layer pins decisions to; ``cad_system_variant`` and detached revisions
carry branch/proposal lineage; ``cad_health_drift`` (#595) consumes
staleness; ``cad_material_*``/verification authorities remain the
technical truth a workflow approval never substitutes for.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


_SHA256_PATTERN = r'^[0-9a-f]{64}$'


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
        **{sha_field: digest, id_field: _semantic_id(prefix, digest)},
    )


def _require_refs(*refs: AuthorityRef | None) -> None:
    for ref in refs:
        if ref is not None and ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')


# ---------------------------------------------------------------------------
# Taxonomies (#721)


CollaborationRole = Literal[
    'designer',
    'acoustic_engineer',
    'video_designer',
    'integrator_installer',
    'commissioning_technician',
    'reviewer_approver',
    'client_stakeholder',
    'service_technician',
    'read_only_observer',
]

IdentityBasis = Literal[
    'declared',
    'externally_verified',
    'pseudonymous',
    'unknown',
]

InformationState = Literal[
    'work_in_progress',
    'draft',
    'shared_for_coordination',
    'review_required',
    'reviewed',
    'approved_for_declared_use',
    'rejected',
    'superseded',
    'as_installed_observed',
    'commissioned_verified',
    'archived',
]

PermittedUse = Literal[
    'design_coordination',
    'procurement',
    'construction_installation',
    'client_presentation',
    'commissioning_measurement',
    'final_handover',
    'service_reference',
]

ApprovalScopeKind = Literal[
    'whole_revision',
    'system_variant',
    'room',
    'equipment_substitution',
    'measurement_campaign',
    'drawing_report',
    'standards_profile_result',
    'exception_deviation',
]

ApprovalDecision = Literal[
    'approved',
    'approved_with_exceptions',
    'rejected',
    'withdrawn',
]

ApprovalCurrency = Literal[
    'current',
    'stale_dependency_changed',
    'superseded',
    'never_in_force',
    'subject_missing',
]

CollaborationAction = Literal[
    'view',
    'comment',
    'edit_design',
    'update_asbuilt',
    'attach_evidence',
    'promote_state',
    'approve',
    'client_accept',
    'resolve_conflict',
    'promote_proposal',
]

ChangeScope = Literal[
    'declared_refs',
    'whole_document',
    'imported_external',
]

MergeOutcome = Literal[
    'auto_merge_safe',
    'manual_review_required',
    'authority_conflict',
    'dependency_stale',
    'rebase_required',
]

DivergenceVerdict = Literal[
    'convergent',
    'divergent_mergeable',
    'divergent_conflicted',
    'divergent_dependency_stale',
    'unrelated_lineage',
]

ResolutionKind = Literal[
    'keep_left',
    'keep_right',
    'manual_composition',
    'defer',
    'abandon_branch',
]

ProposalKind = Literal[
    'alternative_design',
    'experimental_calibration',
    'client_requested_option',
    'service_repair_candidate',
]

ProposalStatus = Literal[
    'open',
    'promoted',
    'abandoned',
]

AcceptanceDimension = Literal[
    'aesthetic',
    'commercial',
    'presentation_choice',
    'schedule_cost',
]

DecisionKind = Literal[
    'comment_resolution',
    'exception_acceptance',
    'design_decision',
    'scope_clarification',
]

DecisionStatus = Literal[
    'open',
    'resolved_accepted',
    'resolved_declined',
    'superseded',
]

AuditEventKind = Literal[
    'create',
    'edit',
    'delete',
    'supersede',
    'import',
    'export',
    'review_request',
    'approve',
    'reject',
    'merge',
    'rebase',
    'conflict_resolution',
    'state_promotion',
    'exception_acceptance',
    'commissioning_signoff',
    'archive',
    'restore',
]


ROLE_LABELS: dict[str, str] = {
    'designer': '設計者',
    'acoustic_engineer': '音響エンジニア',
    'video_designer': '映像設計者',
    'integrator_installer': 'インテグレーター/施工者',
    'commissioning_technician': 'コミッショニング技術者',
    'reviewer_approver': 'レビューア/承認者',
    'client_stakeholder': 'クライアント関係者',
    'service_technician': 'サービス技術者',
    'read_only_observer': '閲覧のみ',
}
IDENTITY_BASIS_LABELS: dict[str, str] = {
    'declared': '自己申告',
    'externally_verified': '外部検証済み',
    'pseudonymous': '仮名',
    'unknown': '不明',
}
STATE_LABELS: dict[str, str] = {
    'work_in_progress': '作業中',
    'draft': 'ドラフト',
    'shared_for_coordination': '調整用に共有',
    'review_required': 'レビュー待ち',
    'reviewed': 'レビュー済み',
    'approved_for_declared_use': '宣言用途に承認済み',
    'rejected': '却下',
    'superseded': '後継ありで置換',
    'as_installed_observed': '施工実測済み',
    'commissioned_verified': 'コミッショニング検証済み',
    'archived': 'アーカイブ済み',
}
PERMITTED_USE_LABELS: dict[str, str] = {
    'design_coordination': '設計調整のみ',
    'procurement': '調達',
    'construction_installation': '施工/設置',
    'client_presentation': 'クライアント提示',
    'commissioning_measurement': 'コミッショニング測定',
    'final_handover': '最終引き渡し',
    'service_reference': '保守参照',
}
SCOPE_KIND_LABELS: dict[str, str] = {
    'whole_revision': 'リビジョン全体',
    'system_variant': 'システムバリアント',
    'room': '部屋',
    'equipment_substitution': '機器代替',
    'measurement_campaign': '測定キャンペーン',
    'drawing_report': '図面/レポート',
    'standards_profile_result': '規格プロファイル結果',
    'exception_deviation': '例外/逸脱',
}
DECISION_LABELS: dict[str, str] = {
    'approved': '承認',
    'approved_with_exceptions': '例外付き承認',
    'rejected': '却下',
    'withdrawn': '取り下げ',
}
CURRENCY_LABELS: dict[str, str] = {
    'current': '現在有効',
    'stale_dependency_changed': '依存変更で陳腐化',
    'superseded': '後続承認に置換',
    'never_in_force': '発効していない',
    'subject_missing': '対象が存在しない',
}
MERGE_OUTCOME_LABELS: dict[str, str] = {
    'auto_merge_safe': '自動マージ可能',
    'manual_review_required': '手動レビュー要',
    'authority_conflict': '権威競合',
    'dependency_stale': '依存陳腐化',
    'rebase_required': 'リベース要',
}
DIVERGENCE_LABELS: dict[str, str] = {
    'convergent': '収束済み（祖先関係あり）',
    'divergent_mergeable': '分岐・マージ可能（非重複）',
    'divergent_conflicted': '分岐・権威競合',
    'divergent_dependency_stale': '分岐・依存陳腐化',
    'unrelated_lineage': '無関係な系譜',
}
RESOLUTION_LABELS: dict[str, str] = {
    'keep_left': '左側を採用',
    'keep_right': '右側を採用',
    'manual_composition': '手動合成',
    'defer': '保留',
    'abandon_branch': 'ブランチ破棄',
}
PROPOSAL_KIND_LABELS: dict[str, str] = {
    'alternative_design': '代替設計案',
    'experimental_calibration': '実験的キャリブレーション',
    'client_requested_option': 'クライアント要求オプション',
    'service_repair_candidate': 'サービス修理候補',
}
PROPOSAL_STATUS_LABELS: dict[str, str] = {
    'open': '検討中',
    'promoted': '昇格済み',
    'abandoned': '破棄',
}
ACCEPTANCE_DIMENSION_LABELS: dict[str, str] = {
    'aesthetic': '美観',
    'commercial': '商業的条件',
    'presentation_choice': '提示上の選択',
    'schedule_cost': '工程/費用',
}
DECISION_KIND_LABELS: dict[str, str] = {
    'comment_resolution': 'コメント解決',
    'exception_acceptance': '例外承認',
    'design_decision': '設計決定',
    'scope_clarification': '範囲の明確化',
}
DECISION_STATUS_LABELS: dict[str, str] = {
    'open': '未解決',
    'resolved_accepted': '受理して解決',
    'resolved_declined': '否認して解決',
    'superseded': '後継ありで置換',
}
EVENT_KIND_LABELS: dict[str, str] = {
    'create': '作成',
    'edit': '編集',
    'delete': '削除',
    'supersede': '置換',
    'import': 'インポート',
    'export': 'エクスポート',
    'review_request': 'レビュー依頼',
    'approve': '承認',
    'reject': '却下',
    'merge': 'マージ',
    'rebase': 'リベース',
    'conflict_resolution': '競合解決',
    'state_promotion': '状態昇格',
    'exception_acceptance': '例外承認',
    'commissioning_signoff': 'コミッショニング署名',
    'archive': 'アーカイブ',
    'restore': '復元',
}


# ---------------------------------------------------------------------------
# Role capability (#721 §1, §17 — least privilege, role ≠ qualification)


_ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    'designer': frozenset({
        'view', 'comment', 'edit_design', 'promote_state',
        'resolve_conflict', 'promote_proposal',
    }),
    'acoustic_engineer': frozenset({
        'view', 'comment', 'edit_design', 'attach_evidence',
        'promote_state', 'resolve_conflict', 'promote_proposal',
    }),
    'video_designer': frozenset({
        'view', 'comment', 'edit_design', 'promote_state',
    }),
    'integrator_installer': frozenset({
        'view', 'comment', 'update_asbuilt', 'attach_evidence',
    }),
    'commissioning_technician': frozenset({
        'view', 'comment', 'update_asbuilt', 'attach_evidence',
        'promote_state',
    }),
    'reviewer_approver': frozenset({
        'view', 'comment', 'approve', 'resolve_conflict',
        'promote_state', 'promote_proposal',
    }),
    'client_stakeholder': frozenset({
        'view', 'comment', 'client_accept',
    }),
    'service_technician': frozenset({
        'view', 'comment', 'update_asbuilt', 'attach_evidence',
    }),
    'read_only_observer': frozenset({'view'}),
}


def check_capability(actor: 'CollaborationActor', action: CollaborationAction) -> bool:
    """Least-privilege capability check (#721 §17).

    Role grants workflow capability only — it never proves professional
    qualification, and unknown actions fail closed.
    """
    return any(
        action in _ROLE_CAPABILITIES.get(role, frozenset())
        for role in actor.roles
    )


def require_capability(actor: 'CollaborationActor', action: CollaborationAction) -> None:
    if not check_capability(actor, action):
        raise ValueError(
            f'actor roles {sorted(actor.roles)} cannot perform {action}'
        )


# ---------------------------------------------------------------------------
# Records


class CollaborationActor(BaseModel):
    """One project participant and their role-at-time (#721 §1).

    A participant is an authority record, not an authentication
    identity — ``identity_basis`` records how the label was established
    and role grants capability, never professional qualification.
    """

    model_config = ConfigDict(frozen=True)

    actor_id: str
    actor_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    display_label: str = Field(min_length=1)
    roles: tuple[CollaborationRole, ...] = Field(min_length=1)
    identity_basis: IdentityBasis = 'declared'
    qualification_refs: tuple[AuthorityRef, ...] = ()
    registered_by_ref: AuthorityRef | None = None
    registered_at_utc: str = Field(min_length=1)
    note: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'CollaborationActor':
        _require_refs(*self.qualification_refs, self.registered_by_ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'actor_id', 'actor_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'CollaborationActor':
        return _seal(cls, kwargs, 'actor_id', 'actor_sha256', 'cact')


class RevisionAuthorship(BaseModel):
    """Authorship of one state-changing revision (#721 §2).

    Binds author, timestamp, exact parent revision, the authority refs
    the edit touched, and the resulting revision identity. ``depends_refs``
    names authorities the change relies on but did not modify — the
    dependency edge that turns a geometric edit into a calibration's
    ``dependency_stale`` merge outcome. Historical authorship is never
    rewritten by later merges or approvals.
    """

    model_config = ConfigDict(frozen=True)

    authorship_id: str
    authorship_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    author_ref: AuthorityRef
    parent_revision_id: str | None
    result_revision_id: str = Field(min_length=1)
    result_content_hash: str | None = None
    changed_refs: tuple[AuthorityRef, ...] = ()
    depends_refs: tuple[AuthorityRef, ...] = ()
    change_scope: ChangeScope = 'declared_refs'
    tool_client: str = ''
    reason: str = ''
    recorded_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _validate(self) -> 'RevisionAuthorship':
        _require_refs(self.author_ref)
        if self.change_scope == 'declared_refs' and not self.changed_refs:
            raise ValueError(
                'declared_refs authorship must name changed_refs — '
                'use whole_document or imported_external when the '
                'touched set is not enumerable'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'authorship_id', 'authorship_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'RevisionAuthorship':
        return _seal(
            cls, kwargs, 'authorship_id', 'authorship_sha256', 'raut'
        )


class InformationStateRecord(BaseModel):
    """One information-state assignment bound to exact authority (#721 §3).

    State records workflow position only — a reviewed or approved object
    can still be technically wrong; approval records accountability and
    permitted use, never physical correctness. Project-defined profiles
    may rename the ladder but the canonical states stay enumerable.
    """

    model_config = ConfigDict(frozen=True)

    state_id: str
    state_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    subject_ref: AuthorityRef
    state: InformationState
    actor_ref: AuthorityRef
    declared_use_profile: str = ''
    supersedes_state_ref: AuthorityRef | None = None
    basis_note: str = ''
    recorded_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _validate(self) -> 'InformationStateRecord':
        _require_refs(
            self.subject_ref, self.actor_ref, self.supersedes_state_ref
        )
        if self.state == 'approved_for_declared_use' and (
            not self.declared_use_profile
        ):
            raise ValueError(
                'approved_for_declared_use must name the declared use '
                'profile it was approved under'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'state_id', 'state_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'InformationStateRecord':
        return _seal(cls, kwargs, 'state_id', 'state_sha256', 'ist')


class ApprovalRecord(BaseModel):
    """Scope-bound approval with permitted-use semantics (#721 §4, §10).

    An approval states what it permits — design coordination,
    procurement, installation, client presentation, commissioning,
    handover or service reference — and never silently means approved
    for every downstream use. ``scope_kind`` keeps partial approval
    partial: a screen option approval never blesses the audio layout.
    """

    model_config = ConfigDict(frozen=True)

    approval_id: str
    approval_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    subject_ref: AuthorityRef
    scope_kind: ApprovalScopeKind
    permitted_uses: tuple[PermittedUse, ...] = ()
    approver_ref: AuthorityRef
    decision: ApprovalDecision
    exception_refs: tuple[AuthorityRef, ...] = ()
    basis_refs: tuple[AuthorityRef, ...] = ()
    supersedes_ref: AuthorityRef | None = None
    approved_at_utc: str = Field(min_length=1)
    note: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'ApprovalRecord':
        _require_refs(
            self.subject_ref,
            self.approver_ref,
            *self.exception_refs,
            *self.basis_refs,
            self.supersedes_ref,
        )
        if self.decision in ('approved', 'approved_with_exceptions'):
            if not self.permitted_uses:
                raise ValueError(
                    'an approval must declare its permitted_uses'
                )
        elif self.permitted_uses:
            raise ValueError(
                'a rejected/withdrawn approval permits nothing'
            )
        if self.decision == 'approved_with_exceptions' and (
            not self.exception_refs
        ):
            raise ValueError(
                'approved_with_exceptions must pin the exception refs'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'approval_id', 'approval_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'ApprovalRecord':
        return _seal(cls, kwargs, 'approval_id', 'approval_sha256', 'appr')


class ReviewDecision(BaseModel):
    """A review decision bound to exact revision/object/evidence (#721 §9).

    Composes with ``cad_review_note`` (#730): notes carry the comment
    text, this record carries the decision outcome and its resolution —
    a resolved decision stays in the audit trail forever.
    """

    model_config = ConfigDict(frozen=True)

    decision_id: str
    decision_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    subject_ref: AuthorityRef
    author_ref: AuthorityRef
    kind: DecisionKind
    status: DecisionStatus = 'open'
    note_refs: tuple[AuthorityRef, ...] = ()
    proposed_change_ref: AuthorityRef | None = None
    resolution_note: str = ''
    recorded_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _validate(self) -> 'ReviewDecision':
        _require_refs(
            self.subject_ref,
            self.author_ref,
            *self.note_refs,
            self.proposed_change_ref,
        )
        if self.status in ('resolved_accepted', 'resolved_declined') and (
            not self.resolution_note
        ):
            raise ValueError(
                'a resolved decision must carry a resolution_note — '
                'the resolution itself is audit evidence'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'decision_id', 'decision_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'ReviewDecision':
        return _seal(cls, kwargs, 'decision_id', 'decision_sha256', 'rdec')


class SiblingDivergence(BaseModel):
    """Recorded outcome of comparing two same-parent edits (#721 §6, §7).

    Produced by :func:`evaluate_divergence`. The verdict distinguishes
    convergent ancestry, conflict-free mergeable divergence, semantic
    authority conflicts and dependency staleness — a textual merge that
    produces valid JSON can still be semantically invalid, so the
    outcome is a typed ladder, never ``latest file wins``.
    """

    model_config = ConfigDict(frozen=True)

    assessment_id: str
    assessment_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    base_revision_id: str | None
    left_authorship_ref: AuthorityRef
    right_authorship_ref: AuthorityRef
    overlap_refs: tuple[AuthorityRef, ...] = ()
    stale_dependency_refs: tuple[AuthorityRef, ...] = ()
    verdict: DivergenceVerdict
    outcome: MergeOutcome
    blocking_reason: str = ''
    recorded_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _validate(self) -> 'SiblingDivergence':
        _require_refs(self.left_authorship_ref, self.right_authorship_ref)
        if self.outcome == 'authority_conflict' and not self.overlap_refs:
            raise ValueError(
                'authority_conflict must name the overlapping refs'
            )
        if self.outcome == 'dependency_stale' and (
            not self.stale_dependency_refs
        ):
            raise ValueError(
                'dependency_stale must name the stale dependency refs'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'assessment_id', 'assessment_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'SiblingDivergence':
        return _seal(
            cls, kwargs, 'assessment_id', 'assessment_sha256', 'sdiv'
        )


class ConflictResolution(BaseModel):
    """How one recorded divergence was resolved (#721 §7, §16).

    ``result_revision_ref`` pins the descendant revision the resolution
    produced; defer/abandon outcomes carry no result revision — the
    resolution itself is still evidence.
    """

    model_config = ConfigDict(frozen=True)

    resolution_id: str
    resolution_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    assessment_ref: AuthorityRef
    resolution_kind: ResolutionKind
    resolver_ref: AuthorityRef
    result_revision_ref: AuthorityRef | None = None
    resolved_at_utc: str = Field(min_length=1)
    note: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'ConflictResolution':
        _require_refs(
            self.assessment_ref, self.resolver_ref, self.result_revision_ref
        )
        if self.resolution_kind in (
            'keep_left', 'keep_right', 'manual_composition'
        ) and self.result_revision_ref is None:
            raise ValueError(
                'a resolving outcome must pin the result revision it '
                'produced'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'resolution_id', 'resolution_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'ConflictResolution':
        return _seal(
            cls, kwargs, 'resolution_id', 'resolution_sha256', 'cres'
        )


class BranchProposal(BaseModel):
    """A deliberate alternative lineage (#721 §12).

    The proposal keeps its parent identity; its content lives in the
    (typically detached) proposal revision — promotion creates a new
    authoritative descendant and never mutates the parent.
    """

    model_config = ConfigDict(frozen=True)

    proposal_id: str
    proposal_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    proposal_kind: ProposalKind
    base_revision_ref: AuthorityRef
    proposal_revision_ref: AuthorityRef
    author_ref: AuthorityRef
    status: ProposalStatus = 'open'
    created_at_utc: str = Field(min_length=1)
    note: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'BranchProposal':
        _require_refs(
            self.base_revision_ref, self.proposal_revision_ref,
            self.author_ref,
        )
        if self.status == 'abandoned' and not self.note:
            raise ValueError(
                'an abandoned proposal must record why'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'proposal_id', 'proposal_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'BranchProposal':
        return _seal(cls, kwargs, 'proposal_id', 'proposal_sha256', 'bprp')


class ProposalPromotion(BaseModel):
    """Promotion of a proposal into authoritative lineage (#721 §12).

    The promoted revision is a NEW descendant — the parent revision is
    not mutated. ``approval_ref`` optionally pins the approval that
    authorized the promotion.
    """

    model_config = ConfigDict(frozen=True)

    promotion_id: str
    promotion_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    proposal_ref: AuthorityRef
    result_revision_ref: AuthorityRef
    promoted_by_ref: AuthorityRef
    approval_ref: AuthorityRef | None = None
    promoted_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _validate(self) -> 'ProposalPromotion':
        _require_refs(
            self.proposal_ref, self.result_revision_ref,
            self.promoted_by_ref, self.approval_ref,
        )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'promotion_id', 'promotion_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'ProposalPromotion':
        return _seal(
            cls, kwargs, 'promotion_id', 'promotion_sha256', 'pprm'
        )


class ClientAcceptance(BaseModel):
    """Client/commercial acceptance — never a technical verdict (#721 §13).

    ``accepted_dimensions`` is closed to non-technical scopes by
    construction: client acceptance may authorize aesthetic, commercial,
    presentation or schedule/cost choices, but cannot upgrade structural
    or electrical safety, solver validation, RP22/RP32 technical
    evidence, or device compatibility. ``declared_limitations`` keeps
    the acknowledged technical limits written into the record — a
    technically limited substitution stays technically limited.
    """

    model_config = ConfigDict(frozen=True)

    acceptance_id: str
    acceptance_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    subject_ref: AuthorityRef
    client_ref: AuthorityRef
    accepted_dimensions: tuple[AcceptanceDimension, ...] = Field(
        min_length=1
    )
    declared_limitations: tuple[str, ...] = ()
    disclaimer_acknowledged: Literal[True] = True
    accepted_at_utc: str = Field(min_length=1)
    note: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'ClientAcceptance':
        _require_refs(self.subject_ref, self.client_ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'acceptance_id', 'acceptance_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'ClientAcceptance':
        return _seal(
            cls, kwargs, 'acceptance_id', 'acceptance_sha256', 'cacc'
        )


class CollaborationEvent(BaseModel):
    """One append-only audit event (#721 §16).

    Sensitive detail may be access-controlled downstream, but event
    existence/history is never silently rewritten — the sealed ledger
    is the audit trail. Kinds requiring accountability
    (approve/reject/conflict/state promotion/signoff/exception) must
    name their actor; import/export/system events may omit it.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str
    event_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    kind: AuditEventKind
    actor_ref: AuthorityRef | None = None
    subject_refs: tuple[AuthorityRef, ...] = ()
    detail: str = ''
    causation_ref: AuthorityRef | None = None
    recorded_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _validate(self) -> 'CollaborationEvent':
        _require_refs(
            *self.subject_refs, self.actor_ref, self.causation_ref
        )
        if self.kind in (
            'approve',
            'reject',
            'conflict_resolution',
            'state_promotion',
            'commissioning_signoff',
            'exception_acceptance',
        ) and self.actor_ref is None:
            raise ValueError(
                f'{self.kind} events must name the accountable actor'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'event_id', 'event_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'CollaborationEvent':
        return _seal(cls, kwargs, 'event_id', 'event_sha256', 'cevt')


# ---------------------------------------------------------------------------
# Evaluation


# Canonical information-state ladder (#721 §3). State is workflow
# position, not technical truth — ``rejected`` may return to
# ``work_in_progress``, and ``superseded``/``archived`` are reachable
# from anywhere once the lifecycle is over.
_STATE_TRANSITIONS: dict[str, frozenset[str]] = {
    'work_in_progress': frozenset({
        'draft', 'shared_for_coordination', 'review_required', 'archived',
    }),
    'draft': frozenset({
        'shared_for_coordination', 'review_required', 'archived',
    }),
    'shared_for_coordination': frozenset({
        'draft', 'review_required', 'archived',
    }),
    'review_required': frozenset({'reviewed', 'rejected', 'archived'}),
    'reviewed': frozenset({
        'approved_for_declared_use', 'rejected', 'archived',
    }),
    'approved_for_declared_use': frozenset({
        'superseded', 'as_installed_observed', 'archived',
    }),
    'rejected': frozenset({'work_in_progress', 'archived'}),
    'superseded': frozenset({'archived'}),
    'as_installed_observed': frozenset({
        'commissioned_verified', 'superseded', 'archived',
    }),
    'commissioned_verified': frozenset({'superseded', 'archived'}),
    'archived': frozenset(),
}


def evaluate_state_transition(
    current: InformationState,
    nxt: InformationState,
) -> bool:
    """Whether a state promotion follows the canonical ladder (#721 §3)."""
    return nxt in _STATE_TRANSITIONS[current]


def evaluate_divergence(
    left: RevisionAuthorship,
    right: RevisionAuthorship,
    *,
    left_ancestor_of_right: bool | None = None,
    right_ancestor_of_left: bool | None = None,
    recorded_at_utc: str,
) -> SiblingDivergence:
    """Classify the relationship between two authored revisions (#721 §6, §7).

    * proven ancestry → ``convergent`` / ``auto_merge_safe``;
    * same parent, disjoint declared change sets → ``auto_merge_safe``;
    * one side changed an authority the other side's change depends on →
      ``dependency_stale``;
    * overlapping changed refs → ``authority_conflict``;
    * same parent but a side declared whole-document/imported scope →
      overlap cannot be proven disjoint → ``manual_review_required``;
    * unrelated lineage → ``rebase_required``.

    Never returns a silent winner — divergent outcomes always carry the
    refs that drove them.
    """
    if left.document_id != right.document_id:
        raise ValueError('divergence compares one document')

    left_ref = AuthorityRef(
        kind='revision_authorship',
        ref_id=left.authorship_id,
        ref_sha256=left.authorship_sha256,
    )
    right_ref = AuthorityRef(
        kind='revision_authorship',
        ref_id=right.authorship_id,
        ref_sha256=right.authorship_sha256,
    )

    def _record(
        verdict: DivergenceVerdict,
        outcome: MergeOutcome,
        overlap: tuple[AuthorityRef, ...] = (),
        stale: tuple[AuthorityRef, ...] = (),
        reason: str = '',
    ) -> SiblingDivergence:
        return SiblingDivergence.create(
            document_id=left.document_id,
            base_revision_id=left.parent_revision_id
            if left.parent_revision_id == right.parent_revision_id
            else None,
            left_authorship_ref=left_ref,
            right_authorship_ref=right_ref,
            overlap_refs=overlap,
            stale_dependency_refs=stale,
            verdict=verdict,
            outcome=outcome,
            blocking_reason=reason,
            recorded_at_utc=recorded_at_utc,
        )

    if right_ancestor_of_left or left_ancestor_of_right:
        return _record(
            'convergent', 'auto_merge_safe',
            reason='one side is already an ancestor of the other',
        )

    if left.result_revision_id == right.result_revision_id:
        return _record(
            'convergent', 'auto_merge_safe',
            reason='both sides produced the same revision identity',
        )

    if (
        left.parent_revision_id is None
        or right.parent_revision_id is None
        or left.parent_revision_id != right.parent_revision_id
    ):
        return _record(
            'unrelated_lineage', 'rebase_required',
            reason='different parents and no proven ancestry — one '
            'branch must rebase before merge can be evaluated',
        )

    changed_left = {r.ref_id for r in left.changed_refs}
    changed_right = {r.ref_id for r in right.changed_refs}
    overlap_ids = changed_left & changed_right
    stale_left = {
        r.ref_id for r in left.changed_refs
    } & {r.ref_id for r in right.depends_refs}
    stale_right = {
        r.ref_id for r in right.changed_refs
    } & {r.ref_id for r in left.depends_refs}
    stale_ids = stale_left | stale_right

    if overlap_ids:
        overlap = tuple(
            AuthorityRef(kind=r.kind, ref_id=r.ref_id, ref_sha256=r.ref_sha256)
            for r in left.changed_refs
            if r.ref_id in overlap_ids
        )
        return _record(
            'divergent_conflicted', 'authority_conflict', overlap=overlap,
            reason='both sides changed the same authorities',
        )

    if stale_ids:
        stale = tuple(
            AuthorityRef(kind=r.kind, ref_id=r.ref_id, ref_sha256=r.ref_sha256)
            for r in (*left.changed_refs, *right.changed_refs)
            if r.ref_id in stale_ids
        )
        return _record(
            'divergent_dependency_stale', 'dependency_stale', stale=stale,
            reason='one side changed an authority the other depends on',
        )

    if (
        left.change_scope != 'declared_refs'
        or right.change_scope != 'declared_refs'
    ):
        return _record(
            'divergent_mergeable', 'manual_review_required',
            reason='undeclared change scope cannot prove disjointness',
        )

    return _record(
        'divergent_mergeable', 'auto_merge_safe',
        reason='sibling edits touch disjoint declared authorities',
    )


def evaluate_approval_currency(
    approval: ApprovalRecord,
    *,
    subject_exists: bool = True,
    subject_current_sha256: str | None = None,
    superseded_by: AuthorityRef | None = None,
) -> ApprovalCurrency:
    """Whether an approval is still in force (#721 §11).

    A stale approval stays historically visible — this verdict never
    deletes or rewrites the record, it only reports currency.
    """
    if approval.decision in ('rejected', 'withdrawn'):
        return 'never_in_force'
    if superseded_by is not None:
        return 'superseded'
    if not subject_exists:
        return 'subject_missing'
    if (
        subject_current_sha256 is not None
        and subject_current_sha256 != approval.subject_ref.ref_sha256
    ):
        return 'stale_dependency_changed'
    return 'current'


def compose_acceptance_verdict(
    acceptance: ClientAcceptance,
) -> str:
    """Report what a client acceptance authorizes (#721 §13).

    Always returns ``'client_accepted_non_technical'`` — the model is
    closed to non-technical dimensions, so the verdict is a reminder
    that no technical scope moved. The function exists so callers get a
    verdict object rather than treating the record as a technical gate.
    """
    return 'client_accepted_non_technical'
