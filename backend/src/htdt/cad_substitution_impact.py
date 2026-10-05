"""Equipment substitution / change-impact authority (#596).

Procurement reality replaces designed equipment: availability, budget,
revision changes and site conditions all force substitutions after
design. ``Same category`` or ``similar spec sheet`` never proves
equivalent system performance — one product swap can alter geometry,
directivity, load compatibility, DSP resources, HDMI features, thermal
state and every commissioning result that assumed the old product.

This module owns **change-impact/equivalence semantics**. It composes
with — never replaces — the equipment catalog (#586), installed instances
(#569), the #894 BOM ``SubstitutionRecord``, device state (#592) and
commissioning (#585).

Contract points honoured:

- a proposal pins the **exact** original and proposed identities —
  ``definition_id`` + ``version`` + semantic hash (or an honest
  uncatalogued identity); changing the proposed model creates a new
  assessment, never an edit;
- equivalence is a **per-dimension evidence matrix**, not one opaque
  score: physical / electroacoustic / signal-DSP / video / interop /
  infrastructure / lifecycle dimensions are assessed independently and
  unknown dimensions stay ``insufficient_evidence``;
- ``PRODUCT_DIFFERENCE`` and ``PROJECT_REQUIREMENT_VERDICT`` are separate
  fields — a lower-capability product can still meet the project
  requirement, and an identical-spec product can still be installation
  incompatible;
- positive verdicts (``equivalent_by_same_exact_evidence`` /
  ``meets_or_exceeds_requirement`` / ``different_but_acceptable``)
  require evidence bound to the **proposed** product or the project
  requirement — the original product's evidence is context only and can
  never satisfy a passing claim (fail-closed);
- ``equivalent_by_same_exact_evidence`` additionally requires the
  proposed identity to be literally identical to the original (same sha)
  — a label change is not evidence;
- affected authorities are enumerated per dependency role with a
  disposition (``stale`` / ``recompute`` / ``remeasure`` /
  ``review_required`` / ``unaffected``) — nothing unrelated is
  invalidated, and unmapped dependents become ``review_required``;
- ``technical_verdict`` (acceptable / limited / incompatible /
  indeterminate) is derived from the matrix and is independent from
  ``commercial_state`` — a client-approved downgrade stays a technical
  downgrade;
- approval is a state machine, not a checkbox: engineering approval
  requires an evidence-complete matrix; ``commercial_override_accepted``
  requires commercial authorization plus a technical limitation;
  ``as_built_verified`` requires a reconciliation proving the installed
  item is the approved one;
- as-built reconciliation compares the **installed** identity against
  the **approved** proposal — approval of a proposal is not proof the
  same item was installed;
- design → approved → procured → installed → service equipment schedule
  history is append-only: the design product is never overwritten by the
  final product.

Literature basis (see docs/reviews/rev56-lifecycle.md):

- AVIXA commissioning field practice: substitutions and late hardware
  changes are real commissioning risks; closeout requires as-built
  documentation of the final makes/models actually installed;
- AVIXA SOW/change-control guidance: equipment substitutions must flow
  through a documented change-control framework;
- Construction "or equal" substitution procedure (e.g. CSI Section
  01 25 13 practice): a substitution request requires a detailed
  comparison of significant qualities, explicit statement of
  installation changes, and stays unapproved until reviewed — ordering
  before approval is at the contractor's risk.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import (
    canonical_sha256 as _hash,
    canonicalize_payload,
)
from .clock import utc_now_iso as _utc_now


SUBSTITUTION_SCHEMA_VERSION = 1

SUBSTITUTION_PROPOSAL_AUTHORITY_VERSION = 'rev56-substitution-proposal-1'
CHANGE_IMPACT_AUTHORITY_VERSION = 'rev56-change-impact-assessment-1'
SUBSTITUTION_DECISION_AUTHORITY_VERSION = 'rev56-substitution-decision-1'
ASBUILT_RECONCILIATION_AUTHORITY_VERSION = 'rev56-asbuilt-reconciliation-1'
EQUIPMENT_SCHEDULE_AUTHORITY_VERSION = 'rev56-equipment-schedule-1'

_SHA256 = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


# ---------------------------------------------------------------------------
# Taxonomy (#596 §2, §3, §5, §10, §11, §14)
# ---------------------------------------------------------------------------

#: The independent dimensions a substitution is judged across (#596 §2).
#: None may be skipped silently — an unassessed dimension is
#: ``insufficient_evidence``.
SubstitutionDimension = Literal[
    'physical_installation',
    'electroacoustic',
    'signal_dsp',
    'video_projection',
    'interoperability',
    'infrastructure',
    'lifecycle_support',
]
SUBSTITUTION_DIMENSIONS: tuple[SubstitutionDimension, ...] = (
    'physical_installation',
    'electroacoustic',
    'signal_dsp',
    'video_projection',
    'interoperability',
    'infrastructure',
    'lifecycle_support',
)

#: Per-dimension evidence-state taxonomy (#596 §3).
DimensionVerdict = Literal[
    'equivalent_by_same_exact_evidence',
    'meets_or_exceeds_requirement',
    'different_but_acceptable',
    'inferior',
    'incompatible',
    'insufficient_evidence',
    'not_applicable',
]
_PASSING_VERDICTS: frozenset[str] = frozenset(
    {
        'equivalent_by_same_exact_evidence',
        'meets_or_exceeds_requirement',
        'different_but_acceptable',
    }
)
_FAILING_VERDICTS: frozenset[str] = frozenset({'inferior', 'incompatible'})

#: PRODUCT_DIFFERENCE — how the proposed product objectively differs
#: from the original product, independent of project needs (#596 §4).
ProductDifference = Literal['same', 'different', 'better', 'worse', 'unknown']

#: PROJECT_REQUIREMENT_VERDICT — whether the project requirement (the
#: pinned requirement ref, when one exists) is still met.
RequirementVerdict = Literal['meets', 'fails', 'indeterminate', 'not_applicable']

#: What the proposal's grounding evidence is worth — degradable by
#: evidence class (#596 goal + prompt: 同一型番 / スペック同等宣言 /
#: 検証済み同等).
EquivalenceEvidenceClass = Literal[
    'same_exact_evidence',
    'verified_equivalent',
    'declared_equivalent',
    'insufficient',
]

#: Why the substitution is proposed (metadata, never a technical verdict).
SubstitutionReason = Literal[
    'availability',
    'budget',
    'revision_change',
    'site_condition',
    'design_improvement',
    'service_replacement',
    'other',
]

#: Role an affected authority plays relative to the substituted product —
#: decides the disposition a change forces on it (#596 §5).
DependencyRole = Literal[
    'geometry_clearance',
    'mounting_fit',
    'sightline',
    'directivity_prediction',
    'output_headroom',
    'electrical_load_compatibility',
    'crossover_management',
    'room_correction',
    'dsp_routing',
    'video_throw_lens',
    'video_commissioning',
    'av_latency',
    'transport_feature',
    'network_qualification',
    'control_integration',
    'power_thermal',
    'standards_profile',
    'commissioning_result',
    'measurement_baseline',
    'other',
]

#: What a substitution does to a dependent authority (#596 §5).
AffectedDisposition = Literal[
    'stale',
    'recompute',
    'remeasure',
    'review_required',
    'unaffected',
]

#: Technical verdict — strictly engineering evidence (#596 §10).
TechnicalVerdict = Literal[
    'technically_acceptable',
    'technically_limited',
    'technically_incompatible',
    'indeterminate',
]

#: Commercial/procurement state — approval authority, never evidence.
CommercialState = Literal[
    'none',
    'client_approved',
    'designer_approved',
    'change_order_approved',
    'procured',
    'installed',
]

#: Approval lifecycle (#596 §11). Production installation may proceed
#: under an override; the limitation survives in downstream reports.
ApprovalState = Literal[
    'proposed',
    'evidence_review',
    'engineering_approved',
    'engineering_approved_with_limitations',
    'rejected',
    'commercial_override_accepted',
    'installed_unverified',
    'as_built_verified',
]

AsBuiltVerdict = Literal[
    'matches_approved',
    'differs_from_approved',
    'identity_unverified',
    'no_approved_baseline',
]

#: Equipment-schedule phases (#596 §14) — history is append-only.
SchedulePhase = Literal[
    'design',
    'approved_substitution',
    'procured',
    'installed_as_built',
    'service_replacement',
]

ReverificationTaskKind = Literal[
    'geometry_recheck',
    'prediction_recompute',
    'standards_profile_reeval',
    'measurement_targeted',
    'control_integration_check',
    'electrical_compat_check',
    'commissioning_reverify',
    'evidence_refresh',
]


# ---------------------------------------------------------------------------
# Exact identity (#596 §1)
# ---------------------------------------------------------------------------


class SubstitutionEquipmentRef(BaseModel):
    """Exact pin of one product identity in a substitution.

    ``definition_sha256=None`` marks an uncatalogued / unresolved product
    — honest UNKNOWN identity, never an implied match. It can never
    ground ``equivalent_by_same_exact_evidence``.
    """

    model_config = ConfigDict(frozen=True)

    definition_id: str = Field(min_length=1)
    definition_version: str = Field(min_length=1)
    definition_sha256: str | None = Field(
        default=None, pattern=_SHA256
    )
    instance_ref: AuthorityRef | None = None
    observed_identity_repr: str | None = None

    def identical_to(self, other: 'SubstitutionEquipmentRef') -> bool:
        return (
            self.definition_id == other.definition_id
            and self.definition_version == other.definition_version
            and self.definition_sha256 is not None
            and self.definition_sha256 == other.definition_sha256
        )


class SupplierMetadata(BaseModel):
    """Availability / commercial context — metadata only.

    A cheaper or in-stock option is not technically preferred; none of
    these fields participates in any verdict (#596 §15).
    """

    model_config = ConfigDict(frozen=True)

    availability_status: str | None = None
    lead_time_note: str | None = None
    quote_reference: str | None = None
    cost_delta: float | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)

    @model_validator(mode='after')
    def _check(self) -> 'SupplierMetadata':
        if (self.cost_delta is None) != (self.currency is None):
            raise ValueError('cost delta and currency must pair')
        return self


class EquipmentSubstitutionProposal(BaseModel):
    """Sealed "replace exact A with exact B" declaration (#596 §1).

    ``assessment_ids`` is the audit trail of impact assessments run
    against this proposal; proposing a different product/version produces
    a new sealed proposal rather than an edit.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = SUBSTITUTION_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-substitution-proposal-1'
    ] = SUBSTITUTION_PROPOSAL_AUTHORITY_VERSION
    proposal_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    proposal_revision: int = Field(ge=1)
    original: SubstitutionEquipmentRef
    proposed: SubstitutionEquipmentRef
    system_variant_ref: AuthorityRef | None = None
    bom_ref: AuthorityRef | None = None
    bom_line_id: str | None = None
    reason_kind: SubstitutionReason
    reason_detail: str = Field(min_length=1)
    requester: str | None = None
    requested_at_utc: str = Field(min_length=1)
    evidence_class: EquivalenceEvidenceClass
    supplier: SupplierMetadata | None = None
    assessment_ids: tuple[str, ...] = ()
    proposal_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'proposal_sha256', 'proposal_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'EquipmentSubstitutionProposal':
        _require_iso8601(self.requested_at_utc, 'requested_at_utc')
        if self.original.identical_to(self.proposed):
            # A same-identity substitution is a no-op claim — allowed only
            # when explicitly same-revision service replacement.
            if self.reason_kind != 'service_replacement':
                raise ValueError(
                    'identical product identities are only meaningful for '
                    'a service_replacement reason'
                )
        if self.evidence_class == 'same_exact_evidence' and not (
            self.original.identical_to(self.proposed)
        ):
            raise ValueError(
                "'same_exact_evidence' requires identical original/proposed "
                'product identities'
            )
        if len(set(self.assessment_ids)) != len(self.assessment_ids):
            raise ValueError('assessment ids must be unique')
        digest = _hash(self.semantic_payload())
        if self.proposal_sha256 != digest:
            raise ValueError('substitution proposal hash mismatch')
        if self.proposal_id != _semantic_id('subprop', digest):
            raise ValueError('substitution proposal id mismatch')
        return self


def build_substitution_proposal(
    *,
    document_id: str,
    original: SubstitutionEquipmentRef,
    proposed: SubstitutionEquipmentRef,
    reason_kind: SubstitutionReason,
    reason_detail: str,
    evidence_class: EquivalenceEvidenceClass,
    proposal_revision: int = 1,
    system_variant_ref: AuthorityRef | None = None,
    bom_ref: AuthorityRef | None = None,
    bom_line_id: str | None = None,
    requester: str | None = None,
    supplier: SupplierMetadata | None = None,
    assessment_ids: Sequence[str] = (),
    requested_at_utc: str | None = None,
) -> EquipmentSubstitutionProposal:
    payload = dict(
        schema_version=SUBSTITUTION_SCHEMA_VERSION,
        authority_version=SUBSTITUTION_PROPOSAL_AUTHORITY_VERSION,
        proposal_id='',
        document_id=document_id,
        proposal_revision=proposal_revision,
        original=original,
        proposed=proposed,
        system_variant_ref=system_variant_ref,
        bom_ref=bom_ref,
        bom_line_id=bom_line_id,
        reason_kind=reason_kind,
        reason_detail=reason_detail,
        requester=requester,
        requested_at_utc=requested_at_utc or _utc_now(),
        evidence_class=evidence_class,
        supplier=supplier,
        assessment_ids=tuple(assessment_ids),
        proposal_sha256='0' * 64,
    )
    probe = EquipmentSubstitutionProposal.model_construct(
        **canonicalize_payload(EquipmentSubstitutionProposal, payload)
    )
    digest = _hash(probe.semantic_payload())
    return EquipmentSubstitutionProposal(
        **probe.model_dump(
            mode='python', exclude={'proposal_sha256', 'proposal_id'}
        ),
        proposal_id=_semantic_id('subprop', digest),
        proposal_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Per-dimension evidence matrix (#596 §2, §3, §4)
# ---------------------------------------------------------------------------


class SubstitutionEvidenceRef(BaseModel):
    """One evidence pin inside a dimensional assessment.

    ``bound_to`` states whose evidence this is: evidence bound to the
    **original** product is context only — it can never satisfy a passing
    verdict for the proposed product (#596: "missing product data never
    inherits the original product's capabilities").
    """

    model_config = ConfigDict(frozen=True)

    ref: AuthorityRef
    bound_to: Literal[
        'original',
        'proposed',
        'requirement',
        'shared_exact',
        'field',
    ]
    note: str | None = None


class DimensionalAssessment(BaseModel):
    """One dimension of the equivalence matrix.

    ``product_difference`` and ``requirement_verdict`` are deliberately
    separate: "worse than the original but meets the requirement" and
    "identical specs but installation-incompatible" are both real
    outcomes (#596 §4).
    """

    model_config = ConfigDict(frozen=True)

    dimension: SubstitutionDimension
    product_difference: ProductDifference
    verdict: DimensionVerdict
    requirement_ref: AuthorityRef | None = None
    requirement_verdict: RequirementVerdict = 'not_applicable'
    evidence_refs: tuple[SubstitutionEvidenceRef, ...] = ()
    note: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'DimensionalAssessment':
        bound = {item.bound_to for item in self.evidence_refs}
        if self.verdict == 'equivalent_by_same_exact_evidence':
            if self.product_difference != 'same':
                raise ValueError(
                    'same-exact-evidence equivalence requires '
                    "product_difference='same'"
                )
            if 'shared_exact' not in bound:
                raise ValueError(
                    'same-exact-evidence equivalence requires evidence '
                    "bound_to='shared_exact'"
                )
        if self.verdict in {
            'meets_or_exceeds_requirement',
            'different_but_acceptable',
        }:
            # Fail-closed: original-product evidence alone can never
            # carry a passing verdict for the proposed product.
            if not (bound & {'proposed', 'requirement', 'shared_exact', 'field'}):
                raise ValueError(
                    f"{self.verdict} requires evidence bound to the "
                    'proposed product, the requirement, or field evidence — '
                    'original-product evidence is context only'
                )
        if self.verdict in _FAILING_VERDICTS and not self.evidence_refs:
            raise ValueError(
                'a negative verdict must name the evidence that shows it'
            )
        if self.verdict == 'not_applicable' and not self.note:
            raise ValueError('not_applicable requires an explanatory note')
        if self.requirement_verdict in {'meets', 'fails'} and (
            self.requirement_ref is None
        ):
            raise ValueError(
                'a requirement verdict requires a pinned requirement ref'
            )
        if (
            self.requirement_verdict == 'not_applicable'
            and self.requirement_ref is not None
        ):
            raise ValueError(
                'a pinned requirement must receive a verdict other than '
                'not_applicable'
            )
        if (
            self.requirement_verdict == 'fails'
            and self.verdict in _PASSING_VERDICTS - {'equivalent_by_same_exact_evidence'}
        ):
            raise ValueError(
                'a dimension failing its project requirement cannot carry '
                'a passing verdict'
            )
        return self


# ---------------------------------------------------------------------------
# Dependency graph (#596 §5)
# ---------------------------------------------------------------------------


class AffectedAuthorityEntry(BaseModel):
    """One authority whose evidence depends on the substituted product."""

    model_config = ConfigDict(frozen=True)

    affected_ref: AuthorityRef
    role: DependencyRole
    disposition: AffectedDisposition
    reason: str = Field(min_length=1)
    #: ``evidence_refs`` name the existing evidence rows the substitution
    #: invalidates or must re-run — old-product evidence never silently
    #: carries over (#596 §7).
    evidence_refs: tuple[AuthorityRef, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'AffectedAuthorityEntry':
        if self.disposition == 'unaffected' and not self.reason:
            raise ValueError('unaffected dispositions require a reason')
        return self


#: Which dimensions a dependency role is sensitive to — the wiring of the
#: issue's dependency examples (#596 §5). A role lists the dimensions that
#: must ALL be non-degraded for the dependent evidence to stay valid.
_ROLE_DIMENSIONS: dict[DependencyRole, tuple[SubstitutionDimension, ...]] = {
    'geometry_clearance': ('physical_installation',),
    'mounting_fit': ('physical_installation',),
    'sightline': ('physical_installation', 'video_projection'),
    'directivity_prediction': ('electroacoustic',),
    'output_headroom': ('electroacoustic', 'signal_dsp'),
    'electrical_load_compatibility': ('electroacoustic', 'infrastructure'),
    'crossover_management': ('signal_dsp', 'electroacoustic'),
    'room_correction': ('electroacoustic', 'signal_dsp'),
    'dsp_routing': ('signal_dsp', 'interoperability'),
    'video_throw_lens': ('video_projection', 'physical_installation'),
    'video_commissioning': ('video_projection',),
    'av_latency': ('video_projection', 'signal_dsp', 'interoperability'),
    'transport_feature': ('interoperability', 'signal_dsp'),
    'network_qualification': ('interoperability', 'infrastructure'),
    'control_integration': ('interoperability', 'lifecycle_support'),
    'power_thermal': ('infrastructure',),
    'standards_profile': (
        'electroacoustic',
        'video_projection',
        'signal_dsp',
        'physical_installation',
    ),
    'commissioning_result': (
        'electroacoustic',
        'video_projection',
        'signal_dsp',
        'physical_installation',
        'infrastructure',
        'interoperability',
    ),
    'measurement_baseline': (
        'electroacoustic',
        'video_projection',
        'signal_dsp',
    ),
    'other': (),
}


def disposition_for_role(
    role: DependencyRole,
    dimension_verdicts: dict[str, DimensionVerdict],
) -> AffectedDisposition:
    """Derive the disposition a dependent authority takes.

    - any sensitive dimension ``incompatible`` → ``remeasure`` (field
      state must be re-established, not recomputed in the model);
    - ``inferior`` / ``different_but_acceptable`` → ``recompute``
      (predictions/constraints re-derive from new product evidence);
    - ``insufficient_evidence`` on any sensitive dimension →
      ``review_required``;
    - all sensitive dimensions clean → ``unaffected`` is claimable only
      when the mapping says so; a role with no dimension mapping is
      always ``review_required`` (unknown dependency ≠ validity).
    """

    dims = _ROLE_DIMENSIONS[role]
    if not dims:
        return 'review_required'
    verdicts = [dimension_verdicts.get(d, 'insufficient_evidence') for d in dims]
    if 'incompatible' in verdicts:
        return 'remeasure'
    if any(v == 'insufficient_evidence' for v in verdicts):
        return 'review_required'
    if any(v in {'inferior', 'different_but_acceptable'} for v in verdicts):
        return 'recompute'
    if all(v in _PASSING_VERDICTS | {'not_applicable'} for v in verdicts):
        return 'unaffected'
    return 'review_required'


# ---------------------------------------------------------------------------
# Change-impact assessment (#596 §5-§10)
# ---------------------------------------------------------------------------


class ChangeImpactAssessment(BaseModel):
    """Sealed dimensional matrix + dependency enumeration for one proposal
    revision.

    ``technical_verdict`` is derived by :func:`evaluate_change_impact`
    and re-verified by the model — a stored verdict that disagrees with
    its own matrix is a hash/verdict integrity failure, not a valid
    record.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = SUBSTITUTION_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-change-impact-assessment-1'
    ] = CHANGE_IMPACT_AUTHORITY_VERSION
    assessment_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    proposal_sha256: str = Field(pattern=_SHA256)
    original: SubstitutionEquipmentRef
    proposed: SubstitutionEquipmentRef
    dimensions: tuple[DimensionalAssessment, ...] = Field(min_length=1)
    affected_entries: tuple[AffectedAuthorityEntry, ...] = ()
    technical_verdict: TechnicalVerdict
    limitation_notes: tuple[str, ...] = ()
    assessed_at_utc: str = Field(min_length=1)
    assessment_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'assessment_sha256', 'assessment_id'}
        )

    def dimension_map(self) -> dict[str, DimensionVerdict]:
        return {item.dimension: item.verdict for item in self.dimensions}

    @model_validator(mode='after')
    def _check(self) -> 'ChangeImpactAssessment':
        _require_iso8601(self.assessed_at_utc, 'assessed_at_utc')
        dims = [item.dimension for item in self.dimensions]
        if len(dims) != len(set(dims)):
            raise ValueError('each substitution dimension assessed at most once')
        # Fail-closed: same-exact-evidence claims are impossible when the
        # product identities differ (#596 §1/§3).
        if not self.original.identical_to(self.proposed):
            for item in self.dimensions:
                if item.verdict == 'equivalent_by_same_exact_evidence':
                    raise ValueError(
                        f"'equivalent_by_same_exact_evidence' on "
                        f'{item.dimension} is impossible: proposed product '
                        'identity differs from the original'
                    )
        entry_keys = {
            (item.affected_ref.kind, item.affected_ref.ref_id)
            for item in self.affected_entries
        }
        if len(entry_keys) != len(self.affected_entries):
            raise ValueError('affected entries must be unique per ref')
        derived = _derive_technical_verdict(self)
        if self.technical_verdict != derived:
            raise ValueError(
                f'technical verdict {self.technical_verdict!r} disagrees '
                f'with the matrix (derived {derived!r})'
            )
        if (
            self.technical_verdict in {'technically_limited'}
            and not self.limitation_notes
        ):
            raise ValueError(
                'a limited technical verdict must carry limitation notes'
            )
        digest = _hash(self.semantic_payload())
        if self.assessment_sha256 != digest:
            raise ValueError('change impact assessment hash mismatch')
        if self.assessment_id != _semantic_id('subimp', digest):
            raise ValueError('change impact assessment id mismatch')
        return self


def _derive_technical_verdict(
    assessment: 'ChangeImpactAssessment',
) -> TechnicalVerdict:
    """Technical verdict from the matrix alone — commercial state is
    never an input (#596 §10)."""

    verdicts = [item.verdict for item in assessment.dimensions]
    req_verdicts = [item.requirement_verdict for item in assessment.dimensions]
    if 'incompatible' in verdicts or 'fails' in req_verdicts:
        return 'technically_incompatible'
    if 'inferior' in verdicts:
        return 'technically_limited'
    if 'insufficient_evidence' in verdicts or 'indeterminate' in req_verdicts:
        return 'indeterminate'
    if any(
        entry.disposition == 'review_required'
        for entry in assessment.affected_entries
    ):
        return 'indeterminate'
    if any(v == 'different_but_acceptable' for v in verdicts):
        return 'technically_limited'
    return 'technically_acceptable'


def evaluate_change_impact(
    *,
    proposal: EquipmentSubstitutionProposal,
    dimensions: Sequence[DimensionalAssessment],
    dependent_refs: Sequence[tuple[AuthorityRef, DependencyRole]] = (),
    dependent_evidence: dict[tuple[str, str], tuple[AuthorityRef, ...]]
    | None = None,
    extra_dispositions: Sequence[AffectedAuthorityEntry] = (),
    limitation_notes: Sequence[str] = (),
    assessed_at_utc: str | None = None,
) -> ChangeImpactAssessment:
    """Build the sealed assessment; dispositions for dependent refs are
    derived from the role/dimension mapping unless the caller supplies a
    reasoned entry.

    ``dependent_evidence`` maps ``(kind, ref_id)`` → the evidence rows
    produced under the old product — they are recorded on the entry so
    nothing old "passes" for the new product by inheritance.
    """

    dimension_map = {item.dimension: item.verdict for item in dimensions}
    entries: list[AffectedAuthorityEntry] = []
    for ref, role in dependent_refs:
        disposition = disposition_for_role(role, dimension_map)
        ev = (dependent_evidence or {}).get((ref.kind, ref.ref_id), ())
        entries.append(
            AffectedAuthorityEntry(
                affected_ref=ref,
                role=role,
                disposition=disposition,
                reason=(
                    f'role {role} sensitivity to dimensions '
                    f'{_ROLE_DIMENSIONS[role] or "unmapped"}'
                ),
                evidence_refs=tuple(ev),
            )
        )
    for entry in extra_dispositions:
        if any(
            (e.affected_ref.kind, e.affected_ref.ref_id)
            == (entry.affected_ref.kind, entry.affected_ref.ref_id)
            for e in entries
        ):
            raise ValueError(
                f'duplicate disposition for {entry.affected_ref.ref_id}'
            )
        entries.append(entry)
    probe_dimensions = tuple(dimensions)
    notes = tuple(limitation_notes)
    if not notes:
        notes = tuple(
            f'{item.dimension}: {item.verdict}'
            for item in probe_dimensions
            if item.verdict in _FAILING_VERDICTS | {'different_but_acceptable'}
        )
    payload = dict(
        schema_version=SUBSTITUTION_SCHEMA_VERSION,
        authority_version=CHANGE_IMPACT_AUTHORITY_VERSION,
        assessment_id='',
        document_id=proposal.document_id,
        proposal_id=proposal.proposal_id,
        proposal_sha256=proposal.proposal_sha256,
        original=proposal.original,
        proposed=proposal.proposed,
        dimensions=probe_dimensions,
        affected_entries=tuple(entries),
        technical_verdict='indeterminate',
        limitation_notes=notes,
        assessed_at_utc=assessed_at_utc or _utc_now(),
        assessment_sha256='0' * 64,
    )
    probe = ChangeImpactAssessment.model_construct(
        **canonicalize_payload(ChangeImpactAssessment, payload)
    )
    derived = _derive_technical_verdict(probe)
    payload['technical_verdict'] = derived
    probe = ChangeImpactAssessment.model_construct(
        **canonicalize_payload(ChangeImpactAssessment, payload)
    )
    digest = _hash(probe.semantic_payload())
    return ChangeImpactAssessment(
        **probe.model_dump(
            mode='python', exclude={'assessment_sha256', 'assessment_id'}
        ),
        assessment_id=_semantic_id('subimp', digest),
        assessment_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Targeted re-verification (#596 §13) — smallest defensible set.
# ---------------------------------------------------------------------------


class ReverificationTask(BaseModel):
    """One post-substitution re-verification action."""

    model_config = ConfigDict(frozen=True)

    task_kind: ReverificationTaskKind
    target_refs: tuple[AuthorityRef, ...] = ()
    reason: str = Field(min_length=1)


#: Role → task kind mapping for the smallest defensible campaign
#: (#596 §13). ``remeasure`` dispositions map to field tasks;
#: ``recompute`` to model/prediction tasks.
_ROLE_TASK_KIND: dict[DependencyRole, ReverificationTaskKind] = {
    'geometry_clearance': 'geometry_recheck',
    'mounting_fit': 'geometry_recheck',
    'sightline': 'geometry_recheck',
    'directivity_prediction': 'prediction_recompute',
    'output_headroom': 'prediction_recompute',
    'electrical_load_compatibility': 'electrical_compat_check',
    'crossover_management': 'prediction_recompute',
    'room_correction': 'measurement_targeted',
    'dsp_routing': 'prediction_recompute',
    'video_throw_lens': 'geometry_recheck',
    'video_commissioning': 'measurement_targeted',
    'av_latency': 'measurement_targeted',
    'transport_feature': 'commissioning_reverify',
    'network_qualification': 'commissioning_reverify',
    'control_integration': 'control_integration_check',
    'power_thermal': 'commissioning_reverify',
    'standards_profile': 'standards_profile_reeval',
    'commissioning_result': 'commissioning_reverify',
    'measurement_baseline': 'measurement_targeted',
    'other': 'evidence_refresh',
}


def compose_reverification_scope(
    assessment: ChangeImpactAssessment,
) -> tuple[ReverificationTask, ...]:
    """Smallest defensible re-verification set: one task per
    (kind, reason-class), never a full-project recommission by default."""

    tasks: dict[tuple[str, int], ReverificationTask] = {}
    order = {'remeasure': 0, 'recompute': 1, 'stale': 2, 'review_required': 3}
    for entry in sorted(
        assessment.affected_entries,
        key=lambda e: order.get(e.disposition, 4),
    ):
        if entry.disposition == 'unaffected':
            continue
        kind = _ROLE_TASK_KIND[entry.role]
        key = (kind, order.get(entry.disposition, 4))
        if key not in tasks:
            tasks[key] = ReverificationTask(
                task_kind=kind,
                target_refs=(entry.affected_ref,),
                reason=f'{entry.role}: {entry.disposition} — {entry.reason}',
            )
        else:
            task = tasks[key]
            merged = tuple(
                dict.fromkeys(task.target_refs + (entry.affected_ref,))
            )
            tasks[key] = ReverificationTask(
                task_kind=task.task_kind,
                target_refs=merged,
                reason=task.reason,
            )
    # A dimension carrying insufficient evidence with no dependent entry
    # still needs evidence before approval — surface it.
    for dim in assessment.dimensions:
        if dim.verdict == 'insufficient_evidence' and not any(
            entry.disposition in {'remeasure', 'review_required'}
            for entry in assessment.affected_entries
        ):
            key = ('evidence_refresh', 4)
            if key not in tasks:
                tasks[key] = ReverificationTask(
                    task_kind='evidence_refresh',
                    target_refs=(),
                    reason=(
                        f'{dim.dimension} has insufficient evidence — '
                        'obtain proposed-product data before approval'
                    ),
                )
    return tuple(tasks.values())


# ---------------------------------------------------------------------------
# Approval state machine (#596 §10/§11)
# ---------------------------------------------------------------------------


class SubstitutionApprovalDecision(BaseModel):
    """Sealed approval/override record — technical verdict and commercial
    authorization stay on separate fields and can never rewrite each
    other (#596 §10)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = SUBSTITUTION_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-substitution-decision-1'
    ] = SUBSTITUTION_DECISION_AUTHORITY_VERSION
    decision_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    proposal_sha256: str = Field(pattern=_SHA256)
    assessment_id: str | None = None
    assessment_sha256: str | None = None
    state: ApprovalState
    technical_verdict: TechnicalVerdict | None = None
    commercial_state: CommercialState = 'none'
    limitations_carried: tuple[str, ...] = ()
    override_rationale: str | None = None
    approver: str | None = None
    reconciliation_ref: AuthorityRef | None = None
    decided_at_utc: str = Field(min_length=1)
    decision_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'decision_sha256', 'decision_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'SubstitutionApprovalDecision':
        _require_iso8601(self.decided_at_utc, 'decided_at_utc')
        if (self.assessment_id is None) != (self.assessment_sha256 is None):
            raise ValueError('assessment id/hash must be supplied together')
        if self.state == 'engineering_approved':
            if self.technical_verdict != 'technically_acceptable':
                raise ValueError(
                    'engineering approval requires a technically '
                    'acceptable verdict'
                )
        if self.state == 'engineering_approved_with_limitations':
            if self.technical_verdict != 'technically_limited':
                raise ValueError(
                    'approval-with-limitations requires a limited verdict'
                )
            if not self.limitations_carried:
                raise ValueError('limitations must be carried on the record')
        if self.state == 'commercial_override_accepted':
            if self.commercial_state == 'none':
                raise ValueError(
                    'a commercial override requires commercial authorization'
                )
            if not self.override_rationale:
                raise ValueError('a commercial override requires a rationale')
            if self.technical_verdict == 'technically_acceptable':
                raise ValueError(
                    'an override is meaningless for an acceptable verdict — '
                    'use engineering_approved'
                )
        if self.state == 'as_built_verified' and self.reconciliation_ref is None:
            raise ValueError(
                'as_built_verified requires an as-built reconciliation ref'
            )
        digest = _hash(self.semantic_payload())
        if self.decision_sha256 != digest:
            raise ValueError('substitution decision hash mismatch')
        if self.decision_id != _semantic_id('subapr', digest):
            raise ValueError('substitution decision id mismatch')
        return self


def evaluate_approval_decision(
    *,
    proposal: EquipmentSubstitutionProposal,
    state: ApprovalState,
    assessment: ChangeImpactAssessment | None = None,
    commercial_state: CommercialState = 'none',
    limitations_carried: Sequence[str] = (),
    override_rationale: str | None = None,
    approver: str | None = None,
    reconciliation: 'AsBuiltReconciliation | None' = None,
    decided_at_utc: str | None = None,
) -> SubstitutionApprovalDecision:
    """Issue a sealed decision; the model enforces the approval rules.

    - ``engineering_approved`` additionally requires that no dimension is
      ``insufficient_evidence`` and no dependent entry is
      ``review_required`` (already implied by the acceptable verdict);
    - ``as_built_verified`` requires a reconciliation whose verdict is
      ``matches_approved`` — approving a proposal never proves the
      installed item is the approved one (#596 §12).
    """

    if state in {
        'engineering_approved',
        'engineering_approved_with_limitations',
        'rejected',
        'commercial_override_accepted',
        'installed_unverified',
    } and assessment is None:
        raise ValueError(f'{state} requires an impact assessment')
    if assessment is not None and (
        assessment.proposal_id != proposal.proposal_id
        or assessment.proposal_sha256 != proposal.proposal_sha256
    ):
        raise ValueError('assessment does not bind this proposal revision')
    if state == 'as_built_verified':
        if reconciliation is None:
            raise ValueError('as_built_verified requires a reconciliation')
        if reconciliation.verdict != 'matches_approved':
            raise ValueError(
                'as_built_verified requires a matching installed identity'
            )
        if reconciliation.proposal_id != proposal.proposal_id:
            raise ValueError('reconciliation binds a different proposal')
    limitations = tuple(limitations_carried)
    if (
        not limitations
        and assessment is not None
        and assessment.limitation_notes
    ):
        limitations = assessment.limitation_notes
    payload = dict(
        schema_version=SUBSTITUTION_SCHEMA_VERSION,
        authority_version=SUBSTITUTION_DECISION_AUTHORITY_VERSION,
        decision_id='',
        document_id=proposal.document_id,
        proposal_id=proposal.proposal_id,
        proposal_sha256=proposal.proposal_sha256,
        assessment_id=None if assessment is None else assessment.assessment_id,
        assessment_sha256=(
            None if assessment is None else assessment.assessment_sha256
        ),
        state=state,
        technical_verdict=(
            None if assessment is None else assessment.technical_verdict
        ),
        commercial_state=commercial_state,
        limitations_carried=limitations,
        override_rationale=override_rationale,
        approver=approver,
        reconciliation_ref=None
        if reconciliation is None
        else AuthorityRef(
            kind='substitution_asbuilt_reconciliation',
            ref_id=reconciliation.reconciliation_id,
            ref_sha256=reconciliation.reconciliation_sha256,
        ),
        decided_at_utc=decided_at_utc or _utc_now(),
        decision_sha256='0' * 64,
    )
    probe = SubstitutionApprovalDecision.model_construct(
        **canonicalize_payload(SubstitutionApprovalDecision, payload)
    )
    digest = _hash(probe.semantic_payload())
    return SubstitutionApprovalDecision(
        **probe.model_dump(
            mode='python', exclude={'decision_sha256', 'decision_id'}
        ),
        decision_id=_semantic_id('subapr', digest),
        decision_sha256=digest,
    )


# ---------------------------------------------------------------------------
# As-built reconciliation (#596 §12)
# ---------------------------------------------------------------------------


class AsBuiltReconciliation(BaseModel):
    """Installed-item check against the approved substitution.

    ``verdict`` is derived from identity comparison, never asserted: a
    technically approved proposal is not proof the same exact item was
    installed.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = SUBSTITUTION_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-asbuilt-reconciliation-1'
    ] = ASBUILT_RECONCILIATION_AUTHORITY_VERSION
    reconciliation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    proposal_sha256: str = Field(pattern=_SHA256)
    approved: SubstitutionEquipmentRef
    installed: SubstitutionEquipmentRef
    installed_instance_ref: AuthorityRef | None = None
    serial_or_asset_tag: str | None = None
    verdict: AsBuiltVerdict
    detail: str | None = None
    reconciled_at_utc: str = Field(min_length=1)
    reconciliation_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'reconciliation_sha256', 'reconciliation_id'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'AsBuiltReconciliation':
        _require_iso8601(self.reconciled_at_utc, 'reconciled_at_utc')
        derived = _derive_asbuilt_verdict(self.approved, self.installed)
        if self.verdict != derived:
            raise ValueError(
                f'as-built verdict {self.verdict!r} disagrees with the '
                f'identity comparison (derived {derived!r})'
            )
        digest = _hash(self.semantic_payload())
        if self.reconciliation_sha256 != digest:
            raise ValueError('as-built reconciliation hash mismatch')
        if self.reconciliation_id != _semantic_id('subab', digest):
            raise ValueError('as-built reconciliation id mismatch')
        return self


def _derive_asbuilt_verdict(
    approved: SubstitutionEquipmentRef,
    installed: SubstitutionEquipmentRef,
) -> AsBuiltVerdict:
    if installed.definition_sha256 is None:
        return 'identity_unverified'
    if approved.identical_to(installed):
        return 'matches_approved'
    return 'differs_from_approved'


def reconcile_as_built(
    *,
    proposal: EquipmentSubstitutionProposal,
    installed: SubstitutionEquipmentRef,
    installed_instance_ref: AuthorityRef | None = None,
    serial_or_asset_tag: str | None = None,
    detail: str | None = None,
    reconciled_at_utc: str | None = None,
) -> AsBuiltReconciliation:
    payload = dict(
        schema_version=SUBSTITUTION_SCHEMA_VERSION,
        authority_version=ASBUILT_RECONCILIATION_AUTHORITY_VERSION,
        reconciliation_id='',
        document_id=proposal.document_id,
        proposal_id=proposal.proposal_id,
        proposal_sha256=proposal.proposal_sha256,
        approved=proposal.proposed,
        installed=installed,
        installed_instance_ref=installed_instance_ref,
        serial_or_asset_tag=serial_or_asset_tag,
        verdict=_derive_asbuilt_verdict(proposal.proposed, installed),
        detail=detail,
        reconciled_at_utc=reconciled_at_utc or _utc_now(),
        reconciliation_sha256='0' * 64,
    )
    probe = AsBuiltReconciliation.model_construct(
        **canonicalize_payload(AsBuiltReconciliation, payload)
    )
    digest = _hash(probe.semantic_payload())
    return AsBuiltReconciliation(
        **probe.model_dump(
            mode='python',
            exclude={'reconciliation_sha256', 'reconciliation_id'},
        ),
        reconciliation_id=_semantic_id('subab', digest),
        reconciliation_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Equipment schedule history (#596 §14)
# ---------------------------------------------------------------------------


class ScheduleLineItem(BaseModel):
    """One equipment-schedule row — the product bound at this phase."""

    model_config = ConfigDict(frozen=True)

    line_id: str = Field(min_length=1)
    requirement_id: str | None = None
    equipment: SubstitutionEquipmentRef
    source_version: str | None = None
    note: str | None = None


class EquipmentScheduleRecord(BaseModel):
    """One immutable phase of the project equipment schedule.

    ``supersedes`` chains the history design → approved → procured →
    installed → service; a later phase never edits a previous row —
    the design product survives the final product in the audit trail.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = SUBSTITUTION_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-equipment-schedule-1'
    ] = EQUIPMENT_SCHEDULE_AUTHORITY_VERSION
    schedule_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    phase: SchedulePhase
    lines: tuple[ScheduleLineItem, ...] = ()
    supersedes_schedule_id: str | None = None
    bom_ref: AuthorityRef | None = None
    recorded_at_utc: str = Field(min_length=1)
    schedule_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'schedule_sha256', 'schedule_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'EquipmentScheduleRecord':
        _require_iso8601(self.recorded_at_utc, 'recorded_at_utc')
        line_ids = [line.line_id for line in self.lines]
        if len(line_ids) != len(set(line_ids)):
            raise ValueError('schedule line ids must be unique')
        digest = _hash(self.semantic_payload())
        if self.schedule_sha256 != digest:
            raise ValueError('equipment schedule hash mismatch')
        if self.schedule_id != _semantic_id('subsch', digest):
            raise ValueError('equipment schedule id mismatch')
        return self


def build_equipment_schedule(
    *,
    document_id: str,
    phase: SchedulePhase,
    lines: Sequence[ScheduleLineItem] = (),
    supersedes_schedule_id: str | None = None,
    bom_ref: AuthorityRef | None = None,
    recorded_at_utc: str | None = None,
) -> EquipmentScheduleRecord:
    payload = dict(
        schema_version=SUBSTITUTION_SCHEMA_VERSION,
        authority_version=EQUIPMENT_SCHEDULE_AUTHORITY_VERSION,
        schedule_id='',
        document_id=document_id,
        phase=phase,
        lines=tuple(lines),
        supersedes_schedule_id=supersedes_schedule_id,
        bom_ref=bom_ref,
        recorded_at_utc=recorded_at_utc or _utc_now(),
        schedule_sha256='0' * 64,
    )
    probe = EquipmentScheduleRecord.model_construct(
        **canonicalize_payload(EquipmentScheduleRecord, payload)
    )
    digest = _hash(probe.semantic_payload())
    return EquipmentScheduleRecord(
        **probe.model_dump(
            mode='python', exclude={'schedule_sha256', 'schedule_id'}
        ),
        schedule_id=_semantic_id('subsch', digest),
        schedule_sha256=digest,
    )


__all__ = [
    'ASBUILT_RECONCILIATION_AUTHORITY_VERSION',
    'AffectedAuthorityEntry',
    'AffectedDisposition',
    'ApprovalState',
    'AsBuiltReconciliation',
    'AsBuiltVerdict',
    'CHANGE_IMPACT_AUTHORITY_VERSION',
    'ChangeImpactAssessment',
    'CommercialState',
    'DependencyRole',
    'DimensionalAssessment',
    'DimensionVerdict',
    'EQUIPMENT_SCHEDULE_AUTHORITY_VERSION',
    'EquipmentScheduleRecord',
    'EquipmentSubstitutionProposal',
    'EquivalenceEvidenceClass',
    'ProductDifference',
    'RequirementVerdict',
    'ReverificationTask',
    'ReverificationTaskKind',
    'SUBSTITUTION_DECISION_AUTHORITY_VERSION',
    'SUBSTITUTION_PROPOSAL_AUTHORITY_VERSION',
    'SUBSTITUTION_SCHEMA_VERSION',
    'ScheduleLineItem',
    'SchedulePhase',
    'SubstitutionApprovalDecision',
    'SubstitutionDimension',
    'SubstitutionEvidenceRef',
    'SubstitutionEquipmentRef',
    'SubstitutionReason',
    'TechnicalVerdict',
    'build_equipment_schedule',
    'build_substitution_proposal',
    'compose_reverification_scope',
    'disposition_for_role',
    'evaluate_approval_decision',
    'evaluate_change_impact',
    'reconcile_as_built',
]
