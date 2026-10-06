"""Acoustic / interior material fire-safety evidence authority (#782).

An acoustic treatment that fits, absorbs and looks right is not thereby
safe to install. Reaction-to-fire is a separate authority: the exact
installed build-up (base material, facing, backing, adhesive, substrate,
coating, thickness, density, orientation, air gap, mounting, joints)
must be matched — not merely approximated — by a tested specimen under
a pinned standard edition, at a stated evidence maturity. Marketing
words like 'flame retardant' or 'class A' are declarations, not
classifications. This module keeps the layers separate: jurisdictional
profile → per-material test evidence → installed-material requirement
state → professional/AHJ approval. Safety claims fail closed.

Basis: issue #782 scope; ISO 11925-2:2026 (ignitability; replaces the
2020 edition), ISO 5660-1:2015+Amd.1:2019 (cone calorimeter heat
release), NFPA 701:2023 (textiles/films), NFPA 286:2023 (room-corner
contribution), ASTM E84-26a (surface burning; compose — do not
duplicate — with cad_fire_evidence #648). General product lifecycle
substitution staling composes with #596/#729.
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


FireEvidenceKind = Literal[
    'ignitability_test', 'flame_propagation_test', 'heat_release_test',
    'smoke_production_test', 'room_corner_test', 'textile_film_test',
    'local_classification', 'manufacturer_declaration',
    'engineer_ahj_approval', 'unknown',
]

SpecimenApplicability = Literal[
    'directly_applicable', 'applicable_with_limitations',
    'assembly_differs', 'test_method_incompatible',
    'insufficient_evidence',
]

EvidenceMaturity = Literal[
    'accredited_lab_report', 'manufacturer_test_report',
    'third_party_certificate', 'product_datasheet_only',
    'declaration_only', 'project_ahj_approval', 'unknown',
]

MaterialSafetyState = Literal[
    'required_evidence_present',
    'required_evidence_present_with_limitations',
    'project_approval_required', 'ahj_review_required',
    'test_assembly_mismatch', 'unsupported_classification',
    'fire_safety_evidence_required', 'stale_after_substitution',
]

FIRE_MATERIAL_LABELS: dict[str, str] = {
    'deployable': '設置可能（防火証拠充足）',
    'deployable_with_limitations': '設置可能（限定条件付き）',
    'fire_safety_evidence_required': '防火証拠が必要',
    'test_assembly_mismatch': '試験体と実装構成が不一致',
    'stale_after_substitution': '材料代替により陳腐化',
    'approval_pending': '承認待ち',
    'rejected': '承認拒否',
    'insufficient_evidence': '証拠不足',
}


class MaterialBuildUp(BaseModel):
    """The exact physical identity of a material installation or test
    specimen — base material plus facing, backing, adhesive, substrate,
    coating, thickness, density, orientation, air gap, mounting and
    joint/edge detail. Specimen-vs-installed mismatch is the classic
    way a 'tested' panel silently becomes an untested assembly."""

    model_config = ConfigDict(frozen=True)

    base_material: str
    facing: str | None = None
    backing: str | None = None
    adhesive_or_fastener: str | None = None
    substrate: str | None = None
    coating_or_laminate: str | None = None
    thickness_mm: float | None = None
    density_kg_m3: float | None = None
    orientation: str | None = None
    air_gap_mm: float | None = None
    mounting_method: str | None = None
    joint_edge_detail: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'MaterialBuildUp':
        if not self.base_material.strip():
            raise ValueError('base_material must not be empty')
        if self.thickness_mm is not None \
                and self.thickness_mm <= 0.0:
            raise ValueError('thickness_mm must be > 0')
        if self.density_kg_m3 is not None \
                and self.density_kg_m3 <= 0.0:
            raise ValueError('density_kg_m3 must be > 0')
        if self.air_gap_mm is not None and self.air_gap_mm < 0.0:
            raise ValueError('air_gap_mm must be >= 0')
        return self


class FireSafetyEvidenceProfile(BaseModel):
    """The jurisdictional frame for material fire-safety review (fsep-
    prefix): occupancy/project class, pinned adopted code editions, and
    the state of the authority-facing approval process. A private
    residence never silently acquires public-assembly obligations, and
    'not_required' must be a decision, not an omission."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    jurisdiction: str
    occupancy_class: str | None = None
    project_class: Literal[
        'private_residential', 'shared_space', 'public_assembly',
        'commercial', 'other', 'undeclared',
    ] = 'undeclared'
    applicable_codes: tuple[str, ...] = ()
    requirement_source: str | None = None
    approval_status: Literal[
        'pending', 'approved', 'approved_with_limitations',
        'rejected', 'not_required', 'undecided',
    ] = 'undecided'
    reviewer_ref: AuthorityRef | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'FireSafetyEvidenceProfile':
        for pinned in self.applicable_codes:
            if '@' not in pinned:
                raise ValueError(
                    'applicable_codes must pin exact editions '
                    '(standard_id@edition)')
        if self.approval_status in (
                'approved', 'approved_with_limitations',
                'rejected', 'not_required') \
                and self.reviewer_ref is None:
            raise ValueError(
                'a decided approval status requires a reviewer_ref')
        if self.reviewer_ref is not None:
            _require_refs(self.reviewer_ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'FireSafetyEvidenceProfile':
        return _seal(
            cls, payload, 'profile_id', 'profile_sha256', 'fsep')


class MaterialReactionToFireEvidence(BaseModel):
    """One reaction-to-fire evidence item for a product/build-up
    (mrfe- prefix). Binds the tested specimen identity and the installed
    identity, the pinned standard edition and its lifecycle status, the
    evidence maturity, the report reference, and the explicit
    applicability judgment. A datasheet or declaration can never be
    'directly_applicable', and a draft standard can never satisfy a
    requirement."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    material_label: str
    installed_buildup: MaterialBuildUp
    specimen_buildup: MaterialBuildUp | None = None
    evidence_kind: FireEvidenceKind
    standard_reference: str | None = None
    standard_status: Literal[
        'current', 'withdrawn_historical', 'amendment',
        'draft_research_only', 'unknown',
    ] = 'unknown'
    evidence_maturity: EvidenceMaturity = 'unknown'
    report_ref: AuthorityRef | None = None
    specimen_applicability: SpecimenApplicability \
        = 'insufficient_evidence'
    declared_classification: str | None = None
    limitations: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _validate(self) -> 'MaterialReactionToFireEvidence':
        if not self.material_label.strip():
            raise ValueError('material_label must not be empty')
        if self.standard_reference is not None \
                and '@' not in self.standard_reference:
            raise ValueError(
                'standard_reference must pin an exact edition '
                '(standard_id@edition)')
        if self.report_ref is not None:
            _require_refs(self.report_ref)
        if self.evidence_kind in ('manufacturer_declaration',
                                  'unknown') \
                or self.evidence_maturity in (
                    'product_datasheet_only', 'declaration_only',
                    'unknown'):
            if self.specimen_applicability in (
                    'directly_applicable',
                    'applicable_with_limitations'):
                raise ValueError(
                    'declarations/datasheets cannot be directly '
                    'applicable — marketing words are not a '
                    'classification')
        if self.standard_status == 'draft_research_only' \
                and self.specimen_applicability in (
                    'directly_applicable',
                    'applicable_with_limitations'):
            raise ValueError(
                'a draft standard cannot yield applicable evidence')
        if self.evidence_kind.endswith('_test') \
                or self.evidence_kind == 'local_classification':
            if self.report_ref is None:
                raise ValueError(
                    'a test/classification evidence kind requires a '
                    'pinned report_ref')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evidence_id', 'evidence_sha256'})

    @classmethod
    def create(
        cls, **payload: Any
    ) -> 'MaterialReactionToFireEvidence':
        return _seal(
            cls, payload, 'evidence_id', 'evidence_sha256', 'mrfe')


class InstalledMaterialSafetyRequirement(BaseModel):
    """The safety gate for one installed material item (imsr- prefix):
    which evidence kinds the selected profile requires, which evidence
    records currently satisfy them, and the lifecycle state — including
    'stale_after_substitution' when the installed material was swapped
    after the evidence was bound."""

    model_config = ConfigDict(frozen=True)

    requirement_id: str
    requirement_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    material_ref: AuthorityRef | None = None
    material_label: str
    required_evidence_kinds: tuple[FireEvidenceKind, ...]
    state: MaterialSafetyState = 'fire_safety_evidence_required'
    satisfied_by_refs: tuple[AuthorityRef, ...] = ()
    substituted_from_ref: AuthorityRef | None = None
    staling_ref: AuthorityRef | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'InstalledMaterialSafetyRequirement':
        _require_refs(self.profile_ref)
        if not self.material_label.strip():
            raise ValueError('material_label must not be empty')
        if not self.required_evidence_kinds:
            raise ValueError(
                'required_evidence_kinds must not be empty')
        for ref in self.satisfied_by_refs:
            _require_refs(ref)
        for ref in (self.material_ref, self.substituted_from_ref,
                    self.staling_ref):
            if ref is not None:
                _require_refs(ref)
        if self.state in (
                'required_evidence_present',
                'required_evidence_present_with_limitations') \
                and not self.satisfied_by_refs:
            raise ValueError(
                'an evidence-present state requires satisfied_by_refs')
        if self.state == 'stale_after_substitution' \
                and self.staling_ref is None \
                and self.substituted_from_ref is None:
            raise ValueError(
                'stale_after_substitution requires the staling or '
                'substitution ref')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'requirement_id', 'requirement_sha256'})

    @classmethod
    def create(
        cls, **payload: Any
    ) -> 'InstalledMaterialSafetyRequirement':
        return _seal(
            cls, payload, 'requirement_id', 'requirement_sha256',
            'imsr')


class FireSafetyApprovalReference(BaseModel):
    """A real professional / AHJ determination on one requirement
    (fsar- prefix). HTDT stores the reference — it never fabricates an
    approval; a record without a pinned supporting document is pending,
    not decided."""

    model_config = ConfigDict(frozen=True)

    approval_id: str
    approval_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    requirement_ref: AuthorityRef
    approver_role: Literal[
        'ahj', 'fire_engineer', 'code_consultant', 'manufacturer',
        'other',
    ]
    approver_name: str
    approval_scope: str
    verdict: Literal[
        'approved', 'approved_with_limitations', 'rejected',
        'pending',
    ]
    support_ref: AuthorityRef | None = None
    issued_utc: str | None = None
    deviations: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _validate(self) -> 'FireSafetyApprovalReference':
        _require_refs(self.requirement_ref)
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
    ) -> 'FireSafetyApprovalReference':
        return _seal(
            cls, payload, 'approval_id', 'approval_sha256', 'fsar')


def evaluate_material_deployability(
    requirement: InstalledMaterialSafetyRequirement | None,
    evidence_items: tuple[MaterialReactionToFireEvidence, ...],
    approval: FireSafetyApprovalReference | None,
) -> tuple[str, str]:
    """Fail-closed installation gate for one material (#782).

    'deployable' requires a requirement whose evidence kinds are
    satisfied by bound, applicable, non-draft evidence and — when the
    state demands approval — a decided non-rejected approval. Anything
    weaker fails closed to the matching blocking state.
    """
    if requirement is None:
        return ('insufficient_evidence', 'no_safety_requirement')
    if requirement.state == 'stale_after_substitution':
        return ('stale_after_substitution',
                'material_substituted_after_evidence')
    if requirement.state in (
            'fire_safety_evidence_required',
            'unsupported_classification'):
        return ('fire_safety_evidence_required',
                'requirement_state:' + requirement.state)
    if requirement.state == 'test_assembly_mismatch':
        return ('test_assembly_mismatch',
                'requirement_state:test_assembly_mismatch')
    for item in evidence_items:
        if item.specimen_applicability in (
                'assembly_differs', 'test_method_incompatible'):
            return ('test_assembly_mismatch',
                    'evidence:' + item.evidence_id)
        if item.standard_status == 'draft_research_only':
            return ('fire_safety_evidence_required',
                    'draft_standard_evidence:' + item.evidence_id)
        if item.specimen_applicability == 'insufficient_evidence':
            return ('fire_safety_evidence_required',
                    'insufficient_evidence:' + item.evidence_id)
    if requirement.state in (
            'project_approval_required', 'ahj_review_required'):
        if approval is None or approval.verdict == 'pending':
            return ('approval_pending',
                    'requirement_state:' + requirement.state)
    if approval is not None and approval.verdict == 'rejected':
        return ('rejected', 'approval_rejected:' + approval.approval_id)
    if requirement.state \
            == 'required_evidence_present_with_limitations' \
            or (approval is not None
                and approval.verdict == 'approved_with_limitations'):
        return ('deployable_with_limitations',
                'limitations_recorded')
    return ('deployable', 'requirement:' + requirement.requirement_id)
