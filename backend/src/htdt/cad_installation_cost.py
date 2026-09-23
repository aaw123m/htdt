"""O100C installation cost/effort authority (#514).

A versioned immutable cost authority keeps volatile commercial prices out of
acoustic definition identity: :class:`CostRecord` binds an amount to an exact
EquipmentDefinition (or stays free-form for fixed project items), a
:class:`CostScenario` declares currency/scope/budget/explicit effort rates,
and :func:`evaluate_variant_installation_cost` derives a reproducible
incremental line-item breakdown for one SystemVariant.

Money and effort stay separate axes; missing prices are UNKNOWN line items,
never zero; different currencies are never silently summed.
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_repository import SceneRevision
from .cad_system_variant import SystemVariant
from .optimization_objectives import (
    ObjectiveDefinition,
    ObjectiveMetric,
    ObjectiveValidDomain,
    ObjectiveVector,
)


COST_SCHEMA_VERSION = 1
COST_AUTHORITY_VERSION = 'o100c-installation-cost-1'
EFFORT_MODEL_VERSION = 'explicit-count-rates-v1'
OBJECTIVE_COMPARISON_MODEL_ID = 'o100c-installation-cost-objective'

CostCategory = Literal[
    'equipment_purchase',
    'treatment_unit',
    'mount_accessory',
    'cable_per_length',
    'labor',
    'fixed_project',
]
CostSourceKind = Literal['user_entered', 'imported', 'quoted']
CostLineItemKind = Literal[
    'equipment_acquisition',
    'equipment_existing',
    'equipment_removed',
    'labor',
    'fixed_project',
]
CostLineItemState = Literal[
    'priced',
    'unknown_price',
    'foreign_currency',
    'not_priced_by_scope',
]
BudgetState = Literal['within', 'over', 'unknown']


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


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _currency(value: str) -> str:
    code = str(value).strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValueError('currency must be an ISO 4217 three-letter code')
    return code


class CostRecord(BaseModel):
    """One immutable priced fact bound to an exact definition or free-standing."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = COST_SCHEMA_VERSION
    authority_version: Literal[
        'o100c-installation-cost-1'
    ] = COST_AUTHORITY_VERSION

    category: CostCategory
    amount: float = Field(ge=0.0)
    currency: str = Field(min_length=3, max_length=3)
    source_kind: CostSourceKind
    source_reference: str | None = Field(default=None, min_length=1)
    # Exact equipment-definition binding; absent only for definition-free
    # categories such as fixed_project or labor.
    equipment_definition_id: str | None = Field(default=None, min_length=1)
    equipment_definition_version: str | None = Field(default=None, min_length=1)
    equipment_definition_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    observed_at_utc: str | None = Field(default=None, min_length=1)
    recorded_at_utc: str = Field(min_length=1)
    note: str | None = Field(default=None, min_length=1)

    record_id: str = Field(min_length=1)
    record_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @field_validator('amount')
    @classmethod
    def finite_amount(cls, value: float) -> float:
        number = float(value)
        if not isfinite(number):
            raise ValueError('cost amount must be finite')
        return number

    @field_validator('currency')
    @classmethod
    def valid_currency(cls, value: str) -> str:
        return _currency(value)

    @model_validator(mode='after')
    def valid_record(self) -> 'CostRecord':
        binding = (
            self.equipment_definition_id,
            self.equipment_definition_version,
            self.equipment_definition_sha256,
        )
        if any(value is not None for value in binding) and not all(
            value is not None for value in binding
        ):
            raise ValueError(
                'cost record equipment binding must supply id/version/sha256 '
                'together'
            )
        if (
            self.category == 'equipment_purchase'
            and self.equipment_definition_id is None
        ):
            raise ValueError(
                'equipment_purchase cost requires an exact definition binding'
            )
        digest = _digest(self.identity_payload())
        if self.record_sha256 != digest:
            raise ValueError('cost record semantic hash mismatch')
        if self.record_id != _semantic_id('cost-record', digest):
            raise ValueError('cost record ID does not match semantic hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'category': self.category,
            'amount': self.amount,
            'currency': self.currency,
            'source_kind': self.source_kind,
            'source_reference': self.source_reference,
            'equipment_definition_id': self.equipment_definition_id,
            'equipment_definition_version': self.equipment_definition_version,
            'equipment_definition_sha256': self.equipment_definition_sha256,
            'observed_at_utc': self.observed_at_utc,
            'recorded_at_utc': self.recorded_at_utc,
            'note': self.note,
        }


def build_cost_record(
    *,
    category: CostCategory,
    amount: float,
    currency: str,
    source_kind: CostSourceKind,
    recorded_at_utc: str,
    equipment_definition_id: str | None = None,
    equipment_definition_version: str | None = None,
    equipment_definition_sha256: str | None = None,
    source_reference: str | None = None,
    observed_at_utc: str | None = None,
    note: str | None = None,
) -> CostRecord:
    payload = {
        'schema_version': COST_SCHEMA_VERSION,
        'authority_version': COST_AUTHORITY_VERSION,
        'category': category,
        'amount': float(amount),
        'currency': _currency(currency),
        'source_kind': source_kind,
        'source_reference': source_reference,
        'equipment_definition_id': equipment_definition_id,
        'equipment_definition_version': equipment_definition_version,
        'equipment_definition_sha256': equipment_definition_sha256,
        'observed_at_utc': observed_at_utc,
        'recorded_at_utc': recorded_at_utc,
        'note': note,
    }
    digest = _digest(payload)
    return CostRecord(
        **payload,
        record_id=_semantic_id('cost-record', digest),
        record_sha256=digest,
    )


class BudgetConstraint(BaseModel):
    """Explicit budget; missing prices must never read as 'under budget'."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    maximum_amount: float = Field(ge=0.0)
    currency: str = Field(min_length=3, max_length=3)
    included_categories: tuple[CostCategory, ...] = (
        'equipment_purchase',
        'treatment_unit',
        'mount_accessory',
        'cable_per_length',
        'labor',
        'fixed_project',
    )
    source_reference: str | None = Field(default=None, min_length=1)

    @field_validator('maximum_amount')
    @classmethod
    def finite_maximum(cls, value: float) -> float:
        number = float(value)
        if not isfinite(number):
            raise ValueError('budget maximum must be finite')
        return number

    @field_validator('currency')
    @classmethod
    def valid_currency(cls, value: str) -> str:
        return _currency(value)


class FixedLineItem(BaseModel):
    """A user-entered one-off project cost inside a scenario."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    item_id: str = Field(min_length=1)
    category: CostCategory
    description: str = Field(min_length=1)
    amount: float = Field(ge=0.0)
    currency: str = Field(min_length=3, max_length=3)
    source_kind: CostSourceKind = 'user_entered'

    @field_validator('amount')
    @classmethod
    def finite_amount(cls, value: float) -> float:
        number = float(value)
        if not isfinite(number):
            raise ValueError('fixed line item amount must be finite')
        return number

    @field_validator('currency')
    @classmethod
    def valid_currency(cls, value: str) -> str:
        return _currency(value)




class CostScenario(BaseModel):
    """Versioned incremental-cost evaluation authority for one document."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = COST_SCHEMA_VERSION
    authority_version: Literal[
        'o100c-installation-cost-1'
    ] = COST_AUTHORITY_VERSION

    document_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    currency: str = Field(min_length=3, max_length=3)
    scope: Literal['incremental_variant'] = 'incremental_variant'
    # When False, baseline-owned equipment is reported as existing, never as
    # a new purchase. A True value is an explicit replacement-cost request.
    charge_existing_equipment: bool = False
    budget: BudgetConstraint | None = None
    # Explicit user-entered effort rates — hours are only computed when the
    # user supplies them; no hidden labor heuristic is implied.
    hours_per_added_entity: float | None = Field(default=None, ge=0.0)
    labor_rate_per_hour: float | None = Field(default=None, ge=0.0)
    effort_model_version: Literal[
        'explicit-count-rates-v1'
    ] = EFFORT_MODEL_VERSION
    fixed_line_items: tuple['FixedLineItem', ...] = ()

    scenario_id: str = Field(min_length=1)
    scenario_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @field_validator('currency')
    @classmethod
    def valid_currency(cls, value: str) -> str:
        return _currency(value)

    @model_validator(mode='after')
    def valid_scenario(self) -> 'CostScenario':
        if (self.labor_rate_per_hour is None) != (
            self.hours_per_added_entity is None
        ):
            raise ValueError(
                'labor cost requires both hours_per_added_entity and '
                'labor_rate_per_hour'
            )
        if self.budget is not None and self.budget.currency != self.currency:
            raise ValueError('budget currency must match scenario currency')
        item_ids = [item.item_id for item in self.fixed_line_items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError('fixed line item ids must be unique')
        digest = _digest(self.identity_payload())
        if self.scenario_sha256 != digest:
            raise ValueError('cost scenario semantic hash mismatch')
        if self.scenario_id != _semantic_id('cost-scenario', digest):
            raise ValueError('cost scenario ID does not match semantic hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'name': self.name,
            'currency': self.currency,
            'scope': self.scope,
            'charge_existing_equipment': self.charge_existing_equipment,
            'budget': (
                None
                if self.budget is None
                else self.budget.model_dump(mode='json')
            ),
            'hours_per_added_entity': self.hours_per_added_entity,
            'labor_rate_per_hour': self.labor_rate_per_hour,
            'effort_model_version': self.effort_model_version,
            'fixed_line_items': [
                item.model_dump(mode='json') for item in self.fixed_line_items
            ],
        }


def build_cost_scenario(
    *,
    document_id: str,
    name: str,
    currency: str,
    charge_existing_equipment: bool = False,
    budget: BudgetConstraint | None = None,
    hours_per_added_entity: float | None = None,
    labor_rate_per_hour: float | None = None,
    fixed_line_items: Sequence[FixedLineItem] = (),
) -> CostScenario:
    payload: dict[str, Any] = {
        'schema_version': COST_SCHEMA_VERSION,
        'authority_version': COST_AUTHORITY_VERSION,
        'document_id': document_id,
        'name': name,
        'currency': _currency(currency),
        'scope': 'incremental_variant',
        'charge_existing_equipment': charge_existing_equipment,
        'budget': (
            None if budget is None else budget.model_dump(mode='json')
        ),
        'hours_per_added_entity': hours_per_added_entity,
        'labor_rate_per_hour': labor_rate_per_hour,
        'effort_model_version': EFFORT_MODEL_VERSION,
        'fixed_line_items': [
            item.model_dump(mode='json') for item in fixed_line_items
        ],
    }
    digest = _digest(payload)
    return CostScenario(
        document_id=document_id,
        name=name,
        currency=_currency(currency),
        charge_existing_equipment=charge_existing_equipment,
        budget=budget,
        hours_per_added_entity=hours_per_added_entity,
        labor_rate_per_hour=labor_rate_per_hour,
        fixed_line_items=tuple(fixed_line_items),
        scenario_id=_semantic_id('cost-scenario', digest),
        scenario_sha256=digest,
    )


class CostLineItem(BaseModel):
    """One derived cost line with provenance and explicit unknown state."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    item_id: str = Field(min_length=1)
    item_kind: CostLineItemKind
    category: CostCategory
    quantity: float = Field(default=1.0, gt=0.0)
    entity_id: str | None = Field(default=None, min_length=1)
    equipment_definition_id: str | None = Field(default=None, min_length=1)
    equipment_definition_version: str | None = Field(default=None, min_length=1)
    equipment_definition_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    cost_record_id: str | None = Field(default=None, min_length=1)
    amount: float | None = Field(default=None, ge=0.0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    state: CostLineItemState
    reason: str | None = None

    @model_validator(mode='after')
    def valid_item(self) -> 'CostLineItem':
        if self.state == 'priced':
            if self.amount is None or self.currency is None:
                raise ValueError('priced cost line requires amount and currency')
            if self.reason is not None:
                raise ValueError('priced cost line must not carry a reason')
        else:
            if self.reason is None:
                raise ValueError('unpriced cost line requires an explicit reason')
        return self


class InstallationEffortSummary(BaseModel):
    """Non-monetary effort metrics; hours exist only via explicit rates."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    added_physical_entity_count: int = Field(ge=0)
    added_speaker_count: int = Field(ge=0)
    estimated_person_hours: float | None = Field(default=None, ge=0.0)
    effort_model_version: Literal[
        'explicit-count-rates-v1'
    ] = EFFORT_MODEL_VERSION


class VariantCostEvaluation(BaseModel):
    """Reproducible incremental cost/effort evidence for one SystemVariant."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = COST_SCHEMA_VERSION
    authority_version: Literal[
        'o100c-installation-cost-1'
    ] = COST_AUTHORITY_VERSION

    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    variant_id: str = Field(min_length=1)
    variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    scenario: CostScenario
    # Exact cost-record digests consumed; replay-validated on read.
    cost_record_sha256s: tuple[str, ...] = ()

    line_items: tuple[CostLineItem, ...]
    totals_by_currency: dict[str, float]
    unknown_item_ids: tuple[str, ...] = ()
    budget_state: BudgetState | None = None
    effort: InstallationEffortSummary

    evaluation_id: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_evaluation(self) -> 'VariantCostEvaluation':
        item_ids = [item.item_id for item in self.line_items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError('cost evaluation line item ids must be unique')
        if self.budget_state is not None and self.scenario.budget is None:
            raise ValueError('budget state requires a scenario budget')
        for currency, total in self.totals_by_currency.items():
            if _currency(currency) != currency or not isfinite(float(total)):
                raise ValueError('cost totals require explicit finite currency')
        digest = _digest(self.identity_payload())
        if self.evaluation_sha256 != digest:
            raise ValueError('cost evaluation semantic hash mismatch')
        if self.evaluation_id != _semantic_id('cost-evaluation', digest):
            raise ValueError('cost evaluation ID does not match semantic hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'variant_id': self.variant_id,
            'variant_sha256': self.variant_sha256,
            'scenario': self.scenario.model_dump(mode='json'),
            'cost_record_sha256s': list(self.cost_record_sha256s),
            'line_items': [
                item.model_dump(mode='json') for item in self.line_items
            ],
            'totals_by_currency': dict(self.totals_by_currency),
            'unknown_item_ids': list(self.unknown_item_ids),
            'budget_state': self.budget_state,
            'effort': self.effort.model_dump(mode='json'),
        }


def _price_record(
    records: Sequence[CostRecord],
    *,
    definition_sha256: str,
) -> CostRecord | None:
    """Most recent explicit purchase record for one exact definition."""
    candidates = [
        record
        for record in records
        if record.category == 'equipment_purchase'
        and record.equipment_definition_sha256 == definition_sha256
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda record: (record.recorded_at_utc, record.record_id),
    )


def evaluate_variant_installation_cost(
    *,
    revision: SceneRevision,
    variant: SystemVariant,
    scenario: CostScenario,
    cost_records: Sequence[CostRecord],
) -> VariantCostEvaluation:
    """Derive an incremental line-item cost/effort breakdown.

    Only entities the variant *adds or replaces* become acquisition lines;
    baseline-owned equipment is reported as existing/removed for provenance
    and never charged unless the scenario explicitly requests it.
    """
    if (
        variant.document_id != revision.document_id
        or variant.baseline_revision_id != revision.revision_id
        or variant.baseline_content_hash != revision.content_hash
    ):
        raise ValueError('cost SystemVariant/SceneRevision authority mismatch')
    if scenario.document_id != revision.document_id:
        raise ValueError('cost scenario document mismatch')

    binding_by_entity = {
        binding.entity_id: binding for binding in variant.equipment_bindings
    }
    used_records: set[str] = set()
    line_items: list[CostLineItem] = []
    index = 0

    def next_id() -> str:
        nonlocal index
        index += 1
        return f'line-{index}'

    added_entity_ids = {
        diff.entity_id
        for diff in variant.diff
        if diff.kind in ('add', 'replace')
    }
    removed_entity_ids = {
        diff.entity_id for diff in variant.diff if diff.kind in ('remove', 'replace')
    }

    for entity_id in sorted(added_entity_ids):
        binding = binding_by_entity.get(entity_id)
        if binding is None:
            continue
        record = _price_record(
            cost_records,
            definition_sha256=binding.equipment_definition_sha256,
        )
        if record is None:
            line_items.append(
                CostLineItem(
                    item_id=next_id(),
                    item_kind='equipment_acquisition',
                    category='equipment_purchase',
                    entity_id=entity_id,
                    equipment_definition_id=binding.equipment_definition_id,
                    equipment_definition_version=(
                        binding.equipment_definition_version
                    ),
                    equipment_definition_sha256=(
                        binding.equipment_definition_sha256
                    ),
                    state='unknown_price',
                    reason='no purchase cost record for exact definition',
                )
            )
            continue
        used_records.add(record.record_sha256)
        if record.currency != scenario.currency:
            line_items.append(
                CostLineItem(
                    item_id=next_id(),
                    item_kind='equipment_acquisition',
                    category='equipment_purchase',
                    entity_id=entity_id,
                    equipment_definition_id=binding.equipment_definition_id,
                    equipment_definition_version=(
                        binding.equipment_definition_version
                    ),
                    equipment_definition_sha256=(
                        binding.equipment_definition_sha256
                    ),
                    cost_record_id=record.record_id,
                    state='foreign_currency',
                    reason=(
                        f'record currency {record.currency} does not match '
                        f'scenario currency {scenario.currency}'
                    ),
                )
            )
            continue
        line_items.append(
            CostLineItem(
                item_id=next_id(),
                item_kind='equipment_acquisition',
                category='equipment_purchase',
                entity_id=entity_id,
                equipment_definition_id=binding.equipment_definition_id,
                equipment_definition_version=binding.equipment_definition_version,
                equipment_definition_sha256=(
                    binding.equipment_definition_sha256
                ),
                cost_record_id=record.record_id,
                amount=float(record.amount),
                currency=record.currency,
                state='priced',
            )
        )

    for entity_id in sorted(
        set(binding_by_entity) - added_entity_ids - removed_entity_ids
    ):
        binding = binding_by_entity[entity_id]
        line_items.append(
            CostLineItem(
                item_id=next_id(),
                item_kind='equipment_existing',
                category='equipment_purchase',
                entity_id=entity_id,
                equipment_definition_id=binding.equipment_definition_id,
                equipment_definition_version=binding.equipment_definition_version,
                equipment_definition_sha256=(
                    binding.equipment_definition_sha256
                ),
                state='not_priced_by_scope',
                reason=(
                    'baseline-owned equipment is outside incremental scope'
                    if not scenario.charge_existing_equipment
                    else 'existing equipment priced only via explicit record'
                ),
            )
        )

    for entity_id in sorted(removed_entity_ids - added_entity_ids):
        binding = binding_by_entity.get(entity_id)
        if binding is None:
            continue
        line_items.append(
            CostLineItem(
                item_id=next_id(),
                item_kind='equipment_removed',
                category='equipment_purchase',
                entity_id=entity_id,
                equipment_definition_id=binding.equipment_definition_id,
                equipment_definition_version=binding.equipment_definition_version,
                equipment_definition_sha256=(
                    binding.equipment_definition_sha256
                ),
                state='not_priced_by_scope',
                reason='removed equipment is excluded from acquisition cost',
            )
        )

    # Existing equipment optionally charged when a record exists.
    if scenario.charge_existing_equipment:
        for item in list(line_items):
            if item.item_kind != 'equipment_existing':
                continue
            record = _price_record(
                cost_records,
                definition_sha256=item.equipment_definition_sha256 or '',
            )
            if record is None or record.currency != scenario.currency:
                continue
            used_records.add(record.record_sha256)
            line_items.append(
                CostLineItem(
                    item_id=next_id(),
                    item_kind='equipment_existing',
                    category='equipment_purchase',
                    entity_id=item.entity_id,
                    equipment_definition_id=item.equipment_definition_id,
                    equipment_definition_version=(
                        item.equipment_definition_version
                    ),
                    equipment_definition_sha256=(
                        item.equipment_definition_sha256
                    ),
                    cost_record_id=record.record_id,
                    amount=float(record.amount),
                    currency=record.currency,
                    state='priced',
                )
            )

    for fixed in scenario.fixed_line_items:
        if fixed.currency != scenario.currency:
            line_items.append(
                CostLineItem(
                    item_id=next_id(),
                    item_kind='fixed_project',
                    category=fixed.category,
                    state='foreign_currency',
                    reason=(
                        f'fixed item {fixed.item_id} currency '
                        f'{fixed.currency} does not match scenario currency'
                    ),
                )
            )
            continue
        line_items.append(
            CostLineItem(
                item_id=next_id(),
                item_kind='fixed_project',
                category=fixed.category,
                quantity=1.0,
                amount=float(fixed.amount),
                currency=fixed.currency,
                state='priced',
            )
        )

    added_entities = [
        entity
        for entity in variant.proposed_entities
        if entity.entity.entity_id in added_entity_ids
    ]
    added_speakers = sum(
        1 for item in added_entities if item.entity.kind == 'speaker'
    )
    estimated_hours: float | None = None
    if scenario.hours_per_added_entity is not None:
        estimated_hours = (
            float(scenario.hours_per_added_entity) * len(added_entities)
        )
        labor_amount = estimated_hours * float(
            scenario.labor_rate_per_hour or 0.0
        )
        line_items.append(
            CostLineItem(
                item_id=next_id(),
                item_kind='labor',
                category='labor',
                quantity=1.0,
                amount=labor_amount,
                currency=scenario.currency,
                state='priced',
            )
        )

    totals: dict[str, float] = {}
    for item in line_items:
        if item.state == 'priced' and item.amount is not None:
            currency = item.currency or scenario.currency
            totals[currency] = totals.get(currency, 0.0) + float(
                item.amount
            ) * float(item.quantity)
    unknown_item_ids = tuple(
        item.item_id
        for item in line_items
        if item.state in ('unknown_price', 'foreign_currency')
    )

    budget_state: BudgetState | None = None
    if scenario.budget is not None:
        subtotal = totals.get(scenario.budget.currency, 0.0)
        if subtotal > scenario.budget.maximum_amount:
            budget_state = 'over'
        elif unknown_item_ids:
            budget_state = 'unknown'
        else:
            budget_state = 'within'

    effort = InstallationEffortSummary(
        added_physical_entity_count=len(added_entities),
        added_speaker_count=added_speakers,
        estimated_person_hours=estimated_hours,
    )

    identity: dict[str, Any] = {
        'schema_version': COST_SCHEMA_VERSION,
        'authority_version': COST_AUTHORITY_VERSION,
        'document_id': revision.document_id,
        'scene_revision_id': revision.revision_id,
        'scene_content_hash': revision.content_hash,
        'variant_id': variant.variant_id,
        'variant_sha256': variant.variant_sha256,
        'scenario': scenario.model_dump(mode='json'),
        'cost_record_sha256s': sorted(used_records),
        'line_items': [item.model_dump(mode='json') for item in line_items],
        'totals_by_currency': totals,
        'unknown_item_ids': list(unknown_item_ids),
        'budget_state': budget_state,
        'effort': effort.model_dump(mode='json'),
    }
    digest = _digest(identity)
    return VariantCostEvaluation(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        variant_id=variant.variant_id,
        variant_sha256=variant.variant_sha256,
        scenario=scenario,
        cost_record_sha256s=tuple(sorted(used_records)),
        line_items=tuple(line_items),
        totals_by_currency=totals,
        unknown_item_ids=unknown_item_ids,
        budget_state=budget_state,
        effort=effort,
        evaluation_id=_semantic_id('cost-evaluation', digest),
        evaluation_sha256=digest,
    )


def _objective_definition(
    scenario: CostScenario,
    *,
    objective_id: str,
    quantity: str,
    unit: str,
    nonnegative: bool = True,
) -> ObjectiveDefinition:
    return ObjectiveDefinition(
        objective_id=objective_id,
        quantity=quantity,
        unit=unit,
        direction='minimize',
        valid_domain=(
            ObjectiveValidDomain(kind='bounded_real', minimum=0.0)
            if nonnegative
            else ObjectiveValidDomain(kind='finite_real')
        ),
        comparison_model_id=OBJECTIVE_COMPARISON_MODEL_ID,
        comparison_model_version=scenario.scenario_sha256,
    )


def installation_cost_objective_vector(
    evaluation: VariantCostEvaluation,
) -> ObjectiveVector:
    """Independent Pareto axes; never a hidden weighted value score."""
    scenario = evaluation.scenario
    specs = (
        (
            'o100c.incremental_acquisition_cost',
            'incremental_variant_acquisition_cost',
            scenario.currency,
            evaluation.totals_by_currency.get(scenario.currency),
            'costed scope carries unknown or foreign-currency line items'
            if evaluation.unknown_item_ids
            else None,
        ),
        (
            'o100c.unknown_cost_item_count',
            'unpriced_line_item_count',
            'count',
            float(len(evaluation.unknown_item_ids)),
            None,
        ),
        (
            'o100c.estimated_installation_effort_hours',
            'installation_effort_person_hours',
            'person-hour',
            evaluation.effort.estimated_person_hours,
            'effort hours require an explicit hours-per-entity rate',
        ),
        (
            'o100c.added_speaker_count',
            'added_speaker_count',
            'count',
            float(evaluation.effort.added_speaker_count),
            None,
        ),
    )
    metrics = []
    for objective_id, quantity, unit, value, missing_reason in specs:
        definition = _objective_definition(
            scenario,
            objective_id=objective_id,
            quantity=quantity,
            unit=unit,
        )
        # A partially-known total is never presented as a real value — the
        # known subtotal stays visible on line items, the objective stays
        # 'missing' so Pareto never rewards an understated cost.
        if value is None or missing_reason is not None:
            metrics.append(
                ObjectiveMetric(
                    objective_id=objective_id,
                    value=None,
                    unit=unit,
                    direction='minimize',
                    state='missing',
                    definition=definition,
                )
            )
            continue
        metrics.append(
            ObjectiveMetric(
                objective_id=objective_id,
                value=float(value),
                unit=unit,
                direction='minimize',
                state='available',
                definition=definition,
            )
        )
    return ObjectiveVector(
        candidate_id=evaluation.variant_id,
        metrics=tuple(metrics),
    )
