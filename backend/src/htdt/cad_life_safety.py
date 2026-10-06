"""Seating circulation / egress / accessibility evidence authority (#746).

A room that fits geometrically in CAD — seats placed, aisles drawn, risers
modeled — is not thereby egress- or accessibility-compliant. Whether a
layout satisfies building-code / life-safety requirements depends on the
jurisdiction, the occupancy/use classification, the exact adopted code
editions, and professional interpretation — none of which CAD geometry can
manufacture. This module keeps the layers separate: applicability
authority → circulation route graph → requirement profile → evidence
class → professional approval. Safety/legal claims fail closed.

Basis: issue #746 scope; 2024 IBC Chapter 10 (means of egress, assembly
aisles §1030); ISO 21542:2021 (access/circulation/egress); 2010 ADA
Standards §221/§802 (wheelchair/companion seating, dispersion, equivalent
sightlines). HTDT never asserts code compliance or ICC/ISO/ADA
certification; jurisdiction/edition lifecycle composes with #599,
structural support stays with #620, general viewing with #259,
substitution staling with #596/#729.
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


def _require_refs(*refs: AuthorityRef) -> None:
    for ref in refs:
        if ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')


ProjectKind = Literal[
    'private_residential', 'shared_space', 'public_assembly',
    'commercial', 'other', 'undeclared',
]

ApplicabilityDecision = Literal[
    'applies', 'does_not_apply', 'project_goal_only', 'undecided',
]

ApplicabilityBasis = Literal[
    'declared_by_project_authority', 'code_professional_determination',
    'ahj_determination', 'project_goal',
    # Present so stored legacy rows replay honestly; rejected for new
    # records — a 'home theater' label never determines applicability.
    'inferred_from_room_label',
]

RouteSegmentKind = Literal[
    'seat', 'wheelchair_space', 'aisle_accessway', 'aisle', 'step',
    'ramp', 'riser_transition', 'door_opening', 'corridor', 'exit',
    'obstruction',
]

RouteDestinationKind = Literal[
    'door', 'exit', 'exit_access', 'area_of_refuge', 'other',
]

FurnitureState = Literal[
    'upright', 'reclined', 'footrest_extended', 'service_position',
    'temporary_seat', 'stored',
]

AccessibilityItem = Literal[
    'wheelchair_space', 'companion_seat', 'accessible_route',
    'transfer_seat', 'integration', 'dispersion',
    'equivalent_sightline',
]

EgressEvidenceClass = Literal[
    'geometry_only', 'external_profile_selected',
    'professional_review_required', 'design_reviewed',
    'field_verified', 'approved_with_limitations',
    'not_applicable', 'insufficient_evidence', 'stale_after_change',
]

EGRESS_LABELS: dict[str, str] = {
    'geometry_only_not_compliance': '幾何適合は規範適合ではない',
    'not_applicable_declared': '適用外（宣言済み）',
    'insufficient_evidence': '証拠不足',
    'professional_review_required': '専門家レビューが必要',
    'stale_after_change': '変更後に陳腐化',
    'professional_rejected': '専門家が不承認',
    'approved_with_limitations': '限定条件付き承認',
    'compliance_professionally_approved': '専門家承認済み適合',
}


class ProjectLifeSafetyProfile(BaseModel):
    """Jurisdiction / occupancy / adopted-code applicability authority
    for one project document (lsp- prefix).

    Applicability is a declared, project-specific determination — never
    inferred from a room label like 'home theater'.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    jurisdiction: str
    project_kind: ProjectKind
    occupancy_classification: str | None = None
    # Exact ``standard_id@edition`` pins composing with #599, e.g.
    # ``IBC@2024`` or ``ISO 21542@2021`` — never bare standard numbers.
    adopted_references: tuple[str, ...] = ()
    local_amendments: str | None = None
    applicability_decision: ApplicabilityDecision = 'undecided'
    applicability_basis: ApplicabilityBasis
    ahj_ref: AuthorityRef | None = None
    design_professional_ref: AuthorityRef | None = None
    source_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'ProjectLifeSafetyProfile':
        if self.applicability_basis == 'inferred_from_room_label':
            raise ValueError(
                'applicability cannot be inferred from a room label')
        for pinned in self.adopted_references:
            if '@' not in pinned:
                raise ValueError(
                    'adopted_references must pin an exact edition '
                    '(standard_id@edition)')
        if self.applicability_decision == 'applies':
            if not self.jurisdiction.strip():
                raise ValueError(
                    'an applies decision requires a jurisdiction')
            if not self.adopted_references:
                raise ValueError(
                    'an applies decision requires adopted_references')
        for ref in (self.ahj_ref, self.design_professional_ref,
                    self.source_ref):
            if ref is not None:
                _require_refs(ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'ProjectLifeSafetyProfile':
        return _seal(cls, payload, 'profile_id', 'profile_sha256', 'lsp')


class CirculationRoute(BaseModel):
    """One seat/zone → exit circulation path evaluated under a specific
    furniture state (rte- prefix). Raw quantities are preserved — the
    external profile, not this record, supplies acceptance limits.
    """

    model_config = ConfigDict(frozen=True)

    route_id: str
    route_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    origin_kind: Literal['seat', 'wheelchair_space', 'zone']
    origin_ref: AuthorityRef
    destination_kind: RouteDestinationKind
    destination_ref: AuthorityRef
    segments: tuple[RouteSegmentKind, ...]
    furniture_state: FurnitureState = 'upright'
    min_clear_width_m: float | None = None
    pinch_points: tuple[str, ...] = ()
    door_clear_opening_m: float | None = None
    travel_length_m: float | None = None
    occupant_catchment: int | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'CirculationRoute':
        _require_refs(
            self.profile_ref, self.origin_ref, self.destination_ref)
        if not self.segments:
            raise ValueError('segments must not be empty')
        if self.segments[-1] in ('seat', 'wheelchair_space',
                                 'obstruction'):
            raise ValueError(
                'a route must terminate at an egress element')
        if self.min_clear_width_m is not None \
                and self.min_clear_width_m <= 0.0:
            raise ValueError('min_clear_width_m must be > 0')
        if self.door_clear_opening_m is not None \
                and self.door_clear_opening_m <= 0.0:
            raise ValueError('door_clear_opening_m must be > 0')
        if self.travel_length_m is not None \
                and self.travel_length_m <= 0.0:
            raise ValueError('travel_length_m must be > 0')
        if self.occupant_catchment is not None \
                and self.occupant_catchment < 1:
            raise ValueError('occupant_catchment must be >= 1')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'route_id', 'route_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'CirculationRoute':
        return _seal(cls, payload, 'route_id', 'route_sha256', 'rte')


class SeatingAccessibilityRequirement(BaseModel):
    """Accessibility features the *selected project profile* requires
    (acr- prefix). Inert data unless the life-safety profile declares
    accessibility applicable — a private residence does not silently
    acquire ADA obligations."""

    model_config = ConfigDict(frozen=True)

    requirement_id: str
    requirement_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    required_items: tuple[AccessibilityItem, ...]
    sightline_equivalence_required: bool = False
    requirement_source: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'SeatingAccessibilityRequirement':
        _require_refs(self.profile_ref)
        if not self.required_items:
            raise ValueError('required_items must not be empty')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'requirement_id', 'requirement_sha256'})

    @classmethod
    def create(
        cls, **payload: Any
    ) -> 'SeatingAccessibilityRequirement':
        return _seal(
            cls, payload, 'requirement_id',
            'requirement_sha256', 'acr')


class EgressEvidence(BaseModel):
    """Evidence class pinned to evaluated routes (egx- prefix). The
    class records *what kind* of support exists — CAD derivation, field
    observation, professional review — and stales after layout changes
    rather than silently staying current."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    route_refs: tuple[AuthorityRef, ...]
    evidence_class: EgressEvidenceClass
    observation_basis: Literal[
        'cad_derived', 'field_observed', 'professionally_reviewed',
        'as_built', 'unknown',
    ]
    staling_ref: AuthorityRef | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'EgressEvidence':
        if self.evidence_class != 'not_applicable' \
                and not self.route_refs:
            raise ValueError(
                'route_refs must not be empty outside not_applicable')
        for ref in self.route_refs:
            _require_refs(ref)
        if self.evidence_class == 'stale_after_change' \
                and self.staling_ref is None:
            raise ValueError(
                'stale_after_change requires the staling change ref')
        if self.staling_ref is not None:
            _require_refs(self.staling_ref)
        if self.observation_basis == 'unknown' \
                and self.evidence_class not in (
                    'geometry_only', 'insufficient_evidence',
                    'not_applicable'):
            raise ValueError(
                'unknown observation basis supports only '
                'geometry_only / insufficient_evidence / not_applicable')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'evidence_id', 'evidence_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'EgressEvidence':
        return _seal(
            cls, payload, 'evidence_id', 'evidence_sha256', 'egx')


class ProfessionalApprovalReference(BaseModel):
    """Reference to a real professional/jurisdictional determination
    (appr- prefix). HTDT stores the reference — it never fabricates an
    approval; a record without a pinned supporting document is a
    pending reference, not an approval."""

    model_config = ConfigDict(frozen=True)

    approval_id: str
    approval_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    approver_role: Literal[
        'licensed_architect', 'code_consultant', 'fire_marshal',
        'ahj', 'accessibility_specialist', 'other',
    ]
    approver_name: str
    approval_scope: str
    verdict: Literal[
        'approved', 'approved_with_limitations', 'rejected', 'pending',
    ]
    support_ref: AuthorityRef | None = None
    issued_utc: str | None = None
    deviations: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _validate(self) -> 'ProfessionalApprovalReference':
        _require_refs(self.profile_ref)
        if self.verdict != 'pending' and self.support_ref is None:
            raise ValueError(
                'a decided approval must pin its supporting document')
        if self.support_ref is not None:
            _require_refs(self.support_ref)
        if not self.approver_name.strip():
            raise ValueError('approver_name must not be empty')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'approval_id', 'approval_sha256'})

    @classmethod
    def create(
        cls, **payload: Any
    ) -> 'ProfessionalApprovalReference':
        return _seal(
            cls, payload, 'approval_id',
            'approval_sha256', 'appr')


def evaluate_egress_claim(
    profile: ProjectLifeSafetyProfile | None,
    routes: tuple[CirculationRoute, ...],
    evidence: EgressEvidence | None,
    approval: ProfessionalApprovalReference | None,
    cad_fit_ok: bool = False,
) -> tuple[str, str]:
    """Fail-closed egress/accessibility claim gate (#746 §12).

    CAD fit alone is always ``geometry_only_not_compliance``. A decided
    compliance claim requires: declared applicable profile + route graph
    + non-stale evidence at field/review level + a decided professional
    approval. HTDT itself never issues the 'compliant' verdict.
    """
    if profile is None:
        if cad_fit_ok:
            return ('geometry_only_not_compliance',
                    'cad_fit_without_life_safety_profile')
        return ('insufficient_evidence', 'no_life_safety_profile')
    if profile.applicability_decision in (
            'does_not_apply', 'project_goal_only'):
        return ('not_applicable_declared',
                'profile_decision:' + profile.applicability_decision)
    if profile.applicability_decision == 'undecided':
        return ('insufficient_evidence',
                'applicability_undecided')
    if not routes:
        return ('insufficient_evidence', 'no_circulation_routes')
    if evidence is None:
        return ('insufficient_evidence', 'no_egress_evidence')
    if evidence.evidence_class == 'stale_after_change':
        return ('stale_after_change',
                'evidence_staled_by_layout_change')
    if evidence.evidence_class in (
            'geometry_only', 'insufficient_evidence'):
        return ('insufficient_evidence',
                'evidence_class:' + evidence.evidence_class)
    if evidence.evidence_class == 'not_applicable':
        return ('not_applicable_declared',
                'evidence_class:not_applicable')
    if evidence.evidence_class in (
            'external_profile_selected', 'professional_review_required'):
        return ('professional_review_required',
                'evidence_class:' + evidence.evidence_class)
    if approval is None or approval.verdict == 'pending':
        return ('professional_review_required',
                'no_decided_professional_approval')
    if approval.verdict == 'rejected':
        return ('professional_rejected',
                'professional_rejected:' + approval.approval_scope)
    if approval.verdict == 'approved_with_limitations':
        reason = (
            'limitations:' + ','.join(approval.deviations)
            if approval.deviations else 'limitations_declared')
        return ('approved_with_limitations', reason)
    return ('compliance_professionally_approved',
            'professional_approval:' + approval.approval_id)
