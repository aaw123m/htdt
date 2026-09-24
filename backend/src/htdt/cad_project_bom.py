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
  required lines stay visible — history is never deleted;
- :class:`SubstitutionRecord` keeps the original requirement when a
  purchase substitutes a different product, with an explicit revalidation
  state rather than silently mutating the design;
- cost facts (#514) may decorate a line but never become part of its
  identity, and different currencies are never silently summed;
- :func:`build_readiness_summary` reports workflow status counts — not a
  project-quality score.
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


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
    """Derive cable material lines: type × total length, in meters."""
    lines: list[BOMLineItem] = []
    for index, req in enumerate(requirements, start=1):
        length = req.total_length_m + (req.waste_allowance_m or 0.0)
        lines.append(BOMLineItem(
            line_id=f'cable-{index}',
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
            lines.append(BOMLineItem(
                line_id=f'cable-{index}-preterm',
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
    """Derive treatment lines preserving exact resulting dimensions."""
    lines: list[BOMLineItem] = []
    for index, req in enumerate(requirements, start=1):
        refs = (req.design_ref,) if req.design_ref is not None else ()
        note = (
            'custom dimensions: '
            + ' × '.join(f'{d:g}' for d in req.panel_dimensions_m)
            + ' m'
            if req.panel_dimensions_m is not None
            else None
        )
        lines.append(BOMLineItem(
            line_id=f'treatment-{index}',
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
    """An explicit "already owned" satisfaction of a BOM line (#569)."""

    model_config = ConfigDict(frozen=True)

    line_id: str = Field(min_length=1)
    instance_id: str = Field(min_length=1)
    quantity: float = Field(gt=0.0)


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
    """
    by_line: dict[str, float] = {}
    for alloc in allocations:
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
    """Lightweight local-first procurement record — HTDT is not an ERP."""

    model_config = ConfigDict(frozen=True)

    record_id: str = Field(min_length=1)
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
    line_id: str = Field(min_length=1)
    original_requirement: str = Field(min_length=1)
    substitute: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    revalidation: SubstitutionRevalidation = 'pending'
    created_at_utc: str = Field(min_length=1)


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


def diff_boms(previous: ProjectBOM, current: ProjectBOM) -> BOMDiff:
    old = {item.line_id: item for item in previous.line_items}
    new = {item.line_id: item for item in current.line_items}
    added = tuple(sorted(set(new) - set(old)))
    removed_ids = set(old) - set(new)
    removed = tuple(sorted(removed_ids))
    qty: list[str] = []
    spec: list[str] = []
    for line_id in sorted(set(old) & set(new)):
        if old[line_id].quantity != new[line_id].quantity:
            qty.append(line_id)
        elif old[line_id].requirement_signature() != new[line_id].requirement_signature():
            spec.append(line_id)
    ordered_obsolete = tuple(sorted(
        line_id
        for line_id in removed_ids
        if old[line_id].procurement_state
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
]
