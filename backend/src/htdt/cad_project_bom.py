"""Project BOM / procurement lifecycle authority (#639).

Once a design alternative is selected, the workflow jumped from "design
selected" to external spreadsheets and installation without a first-class
record of *what exact items the design requires, which are already owned,
which still need acquisition, and what was actually received*.

:class:`ProjectBOM` is that record: a reproducible, content-addressed
snapshot of typed requirement line items bound to the exact design
authority that created them (SceneRevision / SystemVariant / cable plan /
treatment plan / rack plan refs). Contract properties:

- line items are typed by category with explicit quantity *and unit*
  semantics — a 64 m cable run is never "64 pieces";
- model-level requirements and exact SKU resolution stay separate
  (progressive resolution): a BOM can require "2 × surround speaker meeting
  definition X" before a retail SKU exists, and commercial identity never
  becomes engineering identity;
- owned InstalledEquipmentInstances (#569) satisfy requirements explicitly
  through :func:`reconcile_bom` — an owned item is never re-purchased just
  because it appears in the BOM;
- procurement state (required → planned → ordered → partially_received →
  received, plus cancelled / substituted / not_needed_after_design_change)
  is tracked independently of physical installed truth — a received box is
  not proof of installation (#520 owns that);
- :func:`diff_boms` produces a line-level diff between snapshots: added,
  removed, quantity-changed, spec-changed, and ordered-but-no-longer-
  required lines stay visible — history is never deleted. Diff identity is
  the *requirement identity* (#894), never the snapshot-local row id:
  regenerating a BOM from reordered design inputs leaves requirement ids
  untouched, so a cosmetic reorder diffs clean while a real quantity or
  spec change still lands on the same logical requirement;
- :class:`SubstitutionRecord` keeps the original requirement when a
  purchase substitutes a different product, with an explicit revalidation
  state rather than silently mutating the design;
- procurement records (:class:`PurchaseRecord`,
  :class:`SubstitutionRecord`, :class:`OwnedAllocation`) are qualified by
  the exact document/BOM snapshot (``document_id`` + ``bom_id`` +
  ``bom_semantic_hash``) and requirement id they were made against — a
  later BOM snapshot can reconcile them forward explicitly, but line-id
  reuse can never silently rebind them to a different requirement (#894);
- cost facts (#514) may decorate a line but never become part of its
  identity, and different currencies are never silently summed;
- :func:`build_readiness_summary` reports workflow status counts — not a
  project-quality score.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest


BOM_SCHEMA_VERSION = 1
BOM_AUTHORITY_VERSION = 'project-bom-1'


BOMLineCategory = Literal[
    'loudspeaker',
    'subwoofer',
    'display',
    'projector',
    'projection_screen',
    'avr_processor',
    'amplifier',
    'tactile_actuator',
    'acoustic_treatment',
    'rack_infrastructure',
    'mount_accessory',
    'cable',
    'connector',
    'conduit',
    'installation_material',
    'misc',
]

#: Typed units — cable length is meters, not pieces.
BOMUnit = Literal['each', 'meter', 'square_meter', 'set']

#: Procurement workflow state, separate from installed/as-built truth.
ProcurementState = Literal[
    'required',
    'planned',
    'ordered',
    'partially_received',
    'received',
    'cancelled',
    'substituted',
    'not_needed_after_design_change',
]

#: Reconciliation class of a required line against owned inventory.
OwnershipClass = Literal[
    'owned_reusable',
    'newly_required',
    'optional',
    'replacement',
    'spare',
    'unresolved',
]

SubstitutionRevalidation = Literal['pending', 'validated', 'failed']






def _positive(value: float, *, field_name: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not isfinite(float(value))
        or float(value) <= 0.0
    ):
        raise ValueError(f'{field_name} must be a positive finite number')
    return float(value)


class DesignAuthorityRef(BaseModel):
    """Exact reference to the design authority that created a requirement."""

    model_config = ConfigDict(frozen=True)

    authority_kind: str = Field(min_length=1)
    ref_id: str = Field(min_length=1)
    ref_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )


class BOMLineItem(BaseModel):
    """One typed requirement with explicit quantity/unit semantics."""

    model_config = ConfigDict(frozen=True)

    line_id: str = Field(min_length=1)
    #: Stable cross-snapshot identity of the logical requirement this line
    #: satisfies (#894): a deterministic origin key built from the design
    #: authority + source item/entity/run ids + requirement role — never a
    #: content hash (quantity/spec changes must stay the same requirement)
    #: and never an enumeration index (input order is not identity).
    #: Generated lines always carry one; manual lines may omit it, in which
    #: case ``line_id`` remains the only identity the snapshot can offer.
    requirement_id: str | None = Field(default=None, min_length=1)
    category: BOMLineCategory
    name: str = Field(min_length=1)
    quantity: float
    unit: BOMUnit
    #: Originating design authorities (scene entity, variant binding, cable
    #: plan, treatment placement, rack plan, manual requirement, ...).
    design_refs: tuple[DesignAuthorityRef, ...] = ()
    #: Model-level requirement — an EquipmentDefinition id, a capability
    #: constraint description, or a free requirement string.
    requirement: str = Field(min_length=1)
    #: Progressive resolution: exact manufacturer/model or user-selected
    #: SKU/merchant reference once procurement resolves it. Never required.
    resolved_sku: str | None = Field(default=None, min_length=1)
    procurement_state: ProcurementState = 'required'
    #: Optional unit-price fact (#514 style): currency-pinned and never part
    #: of identity — different currencies are never silently summed.
    unit_price: float | None = Field(default=None, ge=0.0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    note: str | None = Field(default=None, min_length=1)

    @field_validator('quantity')
    @classmethod
    def finite_quantity(cls, value: float) -> float:
        return _positive(value, field_name='line quantity')

    @model_validator(mode='after')
    def valid_line(self) -> 'BOMLineItem':
        if (self.unit_price is None) != (self.currency is None):
            raise ValueError('unit price and currency must be supplied together')
        return self

    def requirement_signature(self) -> dict[str, Any]:
        """The fields a spec-change diff compares (never price/state)."""
        return {
            'category': self.category,
            'name': self.name,
            'unit': self.unit,
            'requirement': self.requirement,
            'resolved_sku': self.resolved_sku,
        }


def _requirement_key(*parts: str) -> str:
    """Deterministic requirement identity from design lineage (#894).

    Composite of the source authority, source item/run ids and the
    requirement role — stable across snapshot regeneration regardless of
    input ordering, and unrelated to the mutable quantity/spec content.
    """
    return ':'.join(parts)


def _stable_line_id(prefix: str, requirement_id: str) -> str:
    """Deterministic snapshot row id derived from requirement identity."""
    return f'{prefix}-{_digest(requirement_id)[:12]}'


class CableTakeoffRequirement(BaseModel):
    """Typed cable-run material input for BOM derivation (#538 feed)."""

    model_config = ConfigDict(frozen=True)

    cable_type: str = Field(min_length=1)
    total_length_m: float
    pre_terminated_runs: int = Field(default=0, ge=0)
    run_refs: tuple[str, ...] = ()
    #: Only set when the design/user policy explicitly declares it — there
    #: is no hidden waste factor.
    waste_allowance_m: float | None = Field(default=None, ge=0.0)

    @field_validator('total_length_m')
    @classmethod
    def finite_length(cls, value: float) -> float:
        return _positive(value, field_name='total cable length')


class TreatmentTakeoffRequirement(BaseModel):
    """One exact selected treatment placement for BOM derivation."""

    model_config = ConfigDict(frozen=True)

    treatment_definition_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    count: int = Field(gt=0)
    #: Exact resulting panel dimensions where the design resized them —
    #: different thickness/air-gap configurations never collapse into one
    #: generic line.
    panel_dimensions_m: tuple[float, float, float] | None = None
    design_ref: DesignAuthorityRef | None = None


class ProjectBOM(BaseModel):
    """Reproducible content-addressed requirement snapshot for one design."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = BOM_SCHEMA_VERSION
    authority_version: Literal['project-bom-1'] = BOM_AUTHORITY_VERSION
    bom_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    #: Exact design authorities this snapshot was generated from; changing
    #: the design stales the BOM — the old snapshot is never rewritten.
    design_refs: tuple[DesignAuthorityRef, ...] = Field(min_length=1)
    line_items: tuple[BOMLineItem, ...] = ()
    generation_policy: str = Field(min_length=1)
    generated_at_utc: str = Field(min_length=1)
    supersedes_bom_id: str | None = Field(default=None, min_length=1)
    bom_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_bom(self) -> 'ProjectBOM':
        line_ids = [item.line_id for item in self.line_items]
        if len(line_ids) != len(set(line_ids)):
            raise ValueError('BOM line ids must be unique')
        requirement_ids = [
            item.requirement_id
            for item in self.line_items
            if item.requirement_id is not None
        ]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError('BOM requirement ids must be unique')
        if self.bom_semantic_hash != _digest(self.semantic_payload()):
            raise ValueError('ProjectBOM semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'bom_id': self.bom_id,
            'document_id': self.document_id,
            'version': self.version,
            'design_refs': [item.model_dump(mode='json') for item in self.design_refs],
            'line_items': [item.model_dump(mode='json') for item in self.line_items],
            'generation_policy': self.generation_policy,
            'generated_at_utc': self.generated_at_utc,
            'supersedes_bom_id': self.supersedes_bom_id,
        }

    def line(self, line_id: str) -> BOMLineItem | None:
        return next(
            (item for item in self.line_items if item.line_id == line_id),
            None,
        )


def build_project_bom(
    *,
    bom_id: str,
    document_id: str,
    version: str,
    design_refs: Sequence[DesignAuthorityRef],
    line_items: Sequence[BOMLineItem],
    generation_policy: str,
    generated_at_utc: str,
    supersedes_bom_id: str | None = None,
) -> ProjectBOM:
    payload: dict[str, Any] = {
        'schema_version': BOM_SCHEMA_VERSION,
        'authority_version': BOM_AUTHORITY_VERSION,
        'bom_id': bom_id,
        'document_id': document_id,
        'version': version,
        'design_refs': [item.model_dump(mode='json') for item in design_refs],
        'line_items': [item.model_dump(mode='json') for item in line_items],
        'generation_policy': generation_policy,
        'generated_at_utc': generated_at_utc,
        'supersedes_bom_id': supersedes_bom_id,
    }
    return ProjectBOM(
        bom_id=bom_id,
        document_id=document_id,
        version=version,
        design_refs=tuple(design_refs),
        line_items=tuple(line_items),
        generation_policy=generation_policy,
        generated_at_utc=generated_at_utc,
        supersedes_bom_id=supersedes_bom_id,
        bom_semantic_hash=_digest(payload),
    )


def cable_takeoff_lines(
    requirements: Sequence[CableTakeoffRequirement],
    *,
    design_ref: DesignAuthorityRef,
) -> tuple[BOMLineItem, ...]:
    """Derive cable material lines: type × total length, in meters.

    Line and requirement ids are derived from the cable-plan authority plus
    the exact source run refs (#894): reordering or inserting requirements
    can never reassign an existing line identity to a different run set.
    """
    lines: list[BOMLineItem] = []
    for req in requirements:
        length = req.total_length_m + (req.waste_allowance_m or 0.0)
        requirement_id = _requirement_key(
            'cable',
            design_ref.authority_kind,
            design_ref.ref_id,
            req.cable_type,
            ','.join(sorted(req.run_refs)),
        )
        lines.append(BOMLineItem(
            line_id=_stable_line_id('cable', requirement_id),
            requirement_id=requirement_id,
            category='cable',
            name=req.cable_type,
            quantity=length,
            unit='meter',
            design_refs=(design_ref,),
            requirement=req.cable_type,
            note=(
                f'includes declared waste allowance {req.waste_allowance_m} m'
                if req.waste_allowance_m
                else None
            ),
        ))
        if req.pre_terminated_runs:
            preterm_id = f'{requirement_id}:pre-terminated'
            lines.append(BOMLineItem(
                line_id=_stable_line_id('cable', preterm_id),
                requirement_id=preterm_id,
                category='cable',
                name=f'{req.cable_type} pre-terminated runs',
                quantity=req.pre_terminated_runs,
                unit='each',
                design_refs=(design_ref,),
                requirement=req.cable_type,
            ))
    return tuple(lines)


def treatment_takeoff_lines(
    requirements: Sequence[TreatmentTakeoffRequirement],
) -> tuple[BOMLineItem, ...]:
    """Derive treatment lines preserving exact resulting dimensions.

    Requirement identity comes from the placement's design authority +
    treatment definition (#894); placements without a design ref fall back
    to the definition + label pair, and genuine duplicates fail closed at
    BOM validation instead of silently colliding.
    """
    lines: list[BOMLineItem] = []
    for req in requirements:
        refs = (req.design_ref,) if req.design_ref is not None else ()
        if req.design_ref is not None:
            requirement_id = _requirement_key(
                'treatment',
                req.design_ref.authority_kind,
                req.design_ref.ref_id,
                req.treatment_definition_id,
            )
        else:
            requirement_id = _requirement_key(
                'treatment', 'manual', req.treatment_definition_id, req.label
            )
        note = (
            'custom dimensions: '
            + ' × '.join(f'{d:g}' for d in req.panel_dimensions_m)
            + ' m'
            if req.panel_dimensions_m is not None
            else None
        )
        lines.append(BOMLineItem(
            line_id=_stable_line_id('treatment', requirement_id),
            requirement_id=requirement_id,
            category='acoustic_treatment',
            name=req.label,
            quantity=req.count,
            unit='each',
            design_refs=refs,
            requirement=req.treatment_definition_id,
            note=note,
        ))
    return tuple(lines)


class OwnedAllocation(BaseModel):
    """An explicit "already owned" satisfaction of a BOM line (#569).

    Carries the target's requirement identity in addition to the
    snapshot-local ``line_id`` so the allocation is checked against the
    logical requirement it was declared for (#894), not just whichever line
    happened to reuse the row id.
    """

    model_config = ConfigDict(frozen=True)

    line_id: str = Field(min_length=1)
    #: Logical requirement the allocation was declared against; when set it
    #: must match the resolved line's ``requirement_id`` exactly.
    requirement_id: str | None = Field(default=None, min_length=1)
    instance_id: str = Field(min_length=1)
    quantity: float = Field(gt=0.0)


#: An InstalledEquipmentInstance is a countable owned item — it can never
#: satisfy metered/surfaced material requirements or stock/consumable
#: categories (#894-E); those need inventory/material records instead.
_INSTANCE_ALLOCATABLE_UNITS = frozenset({'each', 'set'})
_INSTANCE_INELIGIBLE_CATEGORIES = frozenset({
    'cable', 'connector', 'conduit', 'installation_material',
})


def _resolve_allocation_line(
    bom: ProjectBOM, allocation: OwnedAllocation
) -> BOMLineItem:
    """Fail-closed resolution of one allocation against a BOM (#894)."""
    line = bom.line(allocation.line_id)
    if line is None:
        raise ValueError(
            f'allocation line_id {allocation.line_id!r} does not exist in '
            f'BOM {bom.bom_id}'
        )
    if (
        allocation.requirement_id is not None
        and allocation.requirement_id != line.requirement_id
    ):
        raise ValueError(
            f'allocation requirement {allocation.requirement_id!r} does not '
            f'match line {line.line_id!r} requirement '
            f'{line.requirement_id!r}'
        )
    if line.unit not in _INSTANCE_ALLOCATABLE_UNITS:
        raise ValueError(
            f'an installed-equipment instance cannot satisfy '
            f'{line.unit!r} requirement {line.line_id!r}'
        )
    if line.category in _INSTANCE_INELIGIBLE_CATEGORIES:
        raise ValueError(
            f'an installed-equipment instance cannot satisfy '
            f'{line.category!r} requirement {line.line_id!r}'
        )
    return line


class LineReconciliation(BaseModel):
    model_config = ConfigDict(frozen=True)

    line_id: str = Field(min_length=1)
    classification: OwnershipClass
    satisfied_quantity: float = Field(ge=0.0)
    remaining_quantity: float = Field(ge=0.0)


def reconcile_bom(
    bom: ProjectBOM,
    allocations: Sequence[OwnedAllocation],
    *,
    optional_line_ids: Sequence[str] = (),
    spare_line_ids: Sequence[str] = (),
) -> tuple[LineReconciliation, ...]:
    """Classify each line against declared owned-instance allocations.

    ``allocations`` are authored facts (which InstalledEquipmentInstance
    covers which line) — reconciliation never guesses from model names.
    Every allocation must resolve to a real line of *this* snapshot and to
    the requirement it was declared for; an equipment instance can never
    satisfy a metered/material requirement (#894).
    """
    by_line: dict[str, float] = {}
    for alloc in allocations:
        _resolve_allocation_line(bom, alloc)
        by_line[alloc.line_id] = by_line.get(alloc.line_id, 0.0) + alloc.quantity
    optional = set(optional_line_ids)
    spare = set(spare_line_ids)
    results: list[LineReconciliation] = []
    for line in bom.line_items:
        satisfied = min(by_line.get(line.line_id, 0.0), line.quantity)
        remaining = line.quantity - satisfied
        if line.line_id in spare:
            classification: OwnershipClass = 'spare'
        elif line.line_id in optional:
            classification = 'optional'
        elif satisfied >= line.quantity:
            classification = 'owned_reusable'
        elif satisfied > 0.0:
            classification = 'replacement'
        elif line.resolved_sku is None and not line.design_refs:
            classification = 'unresolved'
        else:
            classification = 'newly_required'
        results.append(LineReconciliation(
            line_id=line.line_id,
            classification=classification,
            satisfied_quantity=satisfied,
            remaining_quantity=remaining,
        ))
    return tuple(results)


class PurchaseRecord(BaseModel):
    """Lightweight local-first procurement record — HTDT is not an ERP.

    Bound to the exact design snapshot the order was placed against
    (``document_id`` + ``bom_id`` + ``bom_semantic_hash``): a later BOM may
    reconcile the purchase forward explicitly, but accidental line-id reuse
    can never silently rebind it (#894).
    """

    model_config = ConfigDict(frozen=True)

    record_id: str = Field(min_length=1)
    #: Exact snapshot the purchase was made against.
    document_id: str = Field(min_length=1)
    bom_id: str = Field(min_length=1)
    bom_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    vendor: str = Field(min_length=1)
    order_reference: str | None = Field(default=None, min_length=1)
    ordered_at_utc: str = Field(min_length=1)
    expected_at_utc: str | None = Field(default=None, min_length=1)
    received_at_utc: str | None = Field(default=None, min_length=1)
    line_allocations: tuple[OwnedAllocation, ...] = ()
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    total: float | None = Field(default=None, ge=0.0)
    note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_record(self) -> 'PurchaseRecord':
        if (self.total is None) != (self.currency is None):
            raise ValueError('purchase total and currency must be supplied together')
        return self


class SubstitutionRecord(BaseModel):
    """Explicit A→B substitution — never a silent design mutation."""

    model_config = ConfigDict(frozen=True)

    substitution_id: str = Field(min_length=1)
    #: Exact snapshot the substitution was decided against (#894).
    document_id: str = Field(min_length=1)
    bom_id: str = Field(min_length=1)
    bom_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    line_id: str = Field(min_length=1)
    #: Logical requirement the substitution applies to; when set it must
    #: match the resolved line's ``requirement_id`` exactly.
    requirement_id: str | None = Field(default=None, min_length=1)
    original_requirement: str = Field(min_length=1)
    substitute: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    revalidation: SubstitutionRevalidation = 'pending'
    created_at_utc: str = Field(min_length=1)


def validate_purchase_record(
    record: PurchaseRecord, bom: ProjectBOM
) -> None:
    """Fail-closed check that a purchase binds this exact BOM snapshot.

    Every allocation must resolve to an existing line, match its declared
    requirement identity, and satisfy the instance-allocation unit/category
    rules (#894) — a purchase can never satisfy a different requirement
    through accidental line-id reuse.
    """
    _validate_snapshot_binding(
        bom,
        document_id=record.document_id,
        bom_id=record.bom_id,
        bom_semantic_hash=record.bom_semantic_hash,
    )
    for allocation in record.line_allocations:
        _resolve_allocation_line(bom, allocation)


def validate_substitution_record(
    record: SubstitutionRecord, bom: ProjectBOM
) -> None:
    """Fail-closed check that a substitution binds this exact BOM line."""
    _validate_snapshot_binding(
        bom,
        document_id=record.document_id,
        bom_id=record.bom_id,
        bom_semantic_hash=record.bom_semantic_hash,
    )
    line = bom.line(record.line_id)
    if line is None:
        raise ValueError(
            f'substitution line_id {record.line_id!r} does not exist in '
            f'BOM {bom.bom_id}'
        )
    if (
        record.requirement_id is not None
        and record.requirement_id != line.requirement_id
    ):
        raise ValueError(
            f'substitution requirement {record.requirement_id!r} does not '
            f'match line {line.line_id!r} requirement '
            f'{line.requirement_id!r}'
        )
    if line.requirement != record.original_requirement:
        raise ValueError(
            f'substitution original_requirement {record.original_requirement!r} '
            f'does not match line {line.line_id!r} requirement '
            f'{line.requirement!r}'
        )


def _validate_snapshot_binding(
    bom: ProjectBOM,
    *,
    document_id: str,
    bom_id: str,
    bom_semantic_hash: str,
) -> None:
    if document_id != bom.document_id:
        raise ValueError(
            f'procurement document {document_id!r} does not match BOM '
            f'document {bom.document_id!r}'
        )
    if bom_id != bom.bom_id or bom_semantic_hash != bom.bom_semantic_hash:
        raise ValueError(
            f'procurement record binds BOM {bom_id} ({bom_semantic_hash}) '
            f'but validation ran against {bom.bom_id} '
            f'({bom.bom_semantic_hash})'
        )


class BOMDiff(BaseModel):
    """Line-level design-change reconciliation between two snapshots."""

    model_config = ConfigDict(frozen=True)

    from_bom_id: str = Field(min_length=1)
    to_bom_id: str = Field(min_length=1)
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    quantity_changed: tuple[str, ...] = ()
    spec_changed: tuple[str, ...] = ()
    #: Ordered/received lines dropped by the new design — a procurement/
    #: design mismatch that stays visible instead of silently disappearing.
    ordered_no_longer_required: tuple[str, ...] = ()


def _line_identity_key(item: BOMLineItem) -> str:
    """Cross-snapshot diff identity (#894): the stable requirement id when
    the line carries one, else its snapshot-local row id for legacy lines
    that were authored without requirement lineage."""
    return item.requirement_id or item.line_id


def diff_boms(previous: ProjectBOM, current: ProjectBOM) -> BOMDiff:
    """Line-level diff keyed on requirement identity, not row order.

    Regenerating a BOM from reordered design inputs produces identical
    requirement ids, so cosmetic reorder/insertion diffs clean while real
    quantity/spec changes land on the same logical requirement (#894).
    """
    old = {_line_identity_key(item): item for item in previous.line_items}
    new = {_line_identity_key(item): item for item in current.line_items}
    added = tuple(sorted(set(new) - set(old)))
    removed_ids = set(old) - set(new)
    removed = tuple(sorted(removed_ids))
    qty: list[str] = []
    spec: list[str] = []
    for key in sorted(set(old) & set(new)):
        if old[key].quantity != new[key].quantity:
            qty.append(key)
        elif old[key].requirement_signature() != new[key].requirement_signature():
            spec.append(key)
    ordered_obsolete = tuple(sorted(
        key
        for key in removed_ids
        if old[key].procurement_state
        in {'ordered', 'partially_received', 'received'}
    ))
    return BOMDiff(
        from_bom_id=previous.bom_id,
        to_bom_id=current.bom_id,
        added=added,
        removed=removed,
        quantity_changed=tuple(qty),
        spec_changed=tuple(spec),
        ordered_no_longer_required=ordered_obsolete,
    )


class BuildReadinessSummary(BaseModel):
    """Workflow-status counts for the Overview — never a quality score."""

    model_config = ConfigDict(frozen=True)

    bom_id: str = Field(min_length=1)
    required_lines: int = Field(ge=0)
    owned_lines: int = Field(ge=0)
    ordered_lines: int = Field(ge=0)
    received_lines: int = Field(ge=0)
    unresolved_lines: int = Field(ge=0)
    obsolete_ordered_lines: int = Field(ge=0)


def build_readiness_summary(
    bom: ProjectBOM,
    reconciliation: Sequence[LineReconciliation],
    diff: BOMDiff | None = None,
) -> BuildReadinessSummary:
    states = {item.line_id: item.classification for item in reconciliation}
    ordered = sum(
        1
        for item in bom.line_items
        if item.procurement_state in {'ordered', 'partially_received'}
    )
    return BuildReadinessSummary(
        bom_id=bom.bom_id,
        required_lines=len(bom.line_items),
        owned_lines=sum(
            1 for item in reconciliation if item.classification == 'owned_reusable'
        ),
        ordered_lines=ordered,
        received_lines=sum(
            1 for item in bom.line_items if item.procurement_state == 'received'
        ),
        unresolved_lines=sum(
            1
            for item in bom.line_items
            if states.get(item.line_id) in {'unresolved', 'replacement'}
            or (
                states.get(item.line_id) == 'newly_required'
                and item.procurement_state == 'required'
            )
        ),
        obsolete_ordered_lines=(
            0 if diff is None else len(diff.ordered_no_longer_required)
        ),
    )


__all__ = [
    'BOM_AUTHORITY_VERSION',
    'BOM_SCHEMA_VERSION',
    'BOMDiff',
    'BOMLineCategory',
    'BOMLineItem',
    'BOMUnit',
    'BuildReadinessSummary',
    'CableTakeoffRequirement',
    'DesignAuthorityRef',
    'LineReconciliation',
    'OwnedAllocation',
    'OwnershipClass',
    'ProcurementState',
    'ProjectBOM',
    'PurchaseRecord',
    'SubstitutionRecord',
    'SubstitutionRevalidation',
    'TreatmentTakeoffRequirement',
    'build_project_bom',
    'build_readiness_summary',
    'cable_takeoff_lines',
    'diff_boms',
    'reconcile_bom',
    'treatment_takeoff_lines',
    'validate_purchase_record',
    'validate_substitution_record',
]
