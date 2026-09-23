from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_equipment import (
    DirectivityCapability,
    EquipmentDataProvenance,
    build_equipment_definition,
)
from htdt.cad_equipment_evidence import build_equipment_manual_evidence
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_installation_cost import (
    BudgetConstraint,
    CostRecord,
    FixedLineItem,
    build_cost_record,
    build_cost_scenario,
    evaluate_variant_installation_cost,
    installation_cost_objective_vector,
)
from htdt.cad_installation_cost_repository import (
    CadInstallationCostRepository,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    EquipmentBindingRef,
    ProposedEntitySpec,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository


NOW = '2026-09-20T14:00:00+00:00'
DOCUMENT_ID = 'cost-fixture'


def _provenance(name: str, source_hash: str) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='user_defined',
        source_name=name,
        source_version='2026-09-20',
        source_reference='issue-514-fixture',
        source_sha256=source_hash,
    )


def _equipment(definition_id: str, source_hash: str):
    provenance = _provenance(definition_id, source_hash)
    return build_equipment_definition(
        definition_id=definition_id,
        version='1',
        identity_kind='user_defined',
        user_label=definition_id,
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.20, y_m=0.25, z_m=0.35),
        acoustic_reference_point_m=Offset3(),
        directivity=DirectivityCapability(
            tier='unknown',
            data_format='unknown',
            provenance=provenance,
        ),
    )


def _seat() -> SceneEntity:
    return SceneEntity(
        entity_id='seat-a',
        kind='seat',
        name='seat-a',
        position=Position3(x_m=1.0, y_m=3.0, z_m=0.5),
        size_m=Size3(x_m=0.6, y_m=0.8, z_m=1.0),
        acoustic_reference_offset_m=Offset3(),
    )


def _speaker(entity_id: str, role: str) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name=entity_id,
        speaker_role=role,
        position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
        size_m=Size3(x_m=0.20, y_m=0.25, z_m=0.35),
    )


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id=DOCUMENT_ID,
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=8.0, height_m=2.5),
        entities=(_speaker('speaker-fl', 'FL'), _seat()),
    )
    revision = scene_repository.save(
        document,
        parent_revision_id=None,
    ).revision
    variant_repository = CadSystemVariantRepository(scene_repository)
    equipment_repository = CadEquipmentRepository(
        scene_repository,
        variant_repository,
    )
    return (
        scene_repository,
        revision,
        variant_repository,
        equipment_repository,
    )


def _binding(entity_id: str, definition) -> EquipmentBindingRef:
    return EquipmentBindingRef(
        entity_id=entity_id,
        equipment_definition_id=definition.definition_id,
        equipment_definition_version=definition.version,
        equipment_definition_sha256=definition.semantic_sha256,
    )


def _persist_pair(equipment_repository, *definitions) -> None:
    for definition in definitions:
        for evidence in build_equipment_manual_evidence(
            definition,
            actor='cost-fixture',
            recorded_at_utc=NOW,
        ):
            equipment_repository.save_evidence(evidence)
        equipment_repository.save_definition(definition)


def _variant(
    variant_repository,
    revision,
    existing_definition,
    added_definition,
):
    variant = build_system_variant(
        baseline=revision,
        name='add surround left',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front Left'),
            ChannelRoleBinding(role_id='SL', display_name='Surround Left'),
        ),
        proposed_entities=(
            ProposedEntitySpec(
                spec_id='sl-spec',
                entity=_speaker('speaker-sl', 'SL'),
                role_binding_id='SL',
            ),
        ),
        equipment_bindings=(
            _binding('speaker-fl', existing_definition),
            _binding('speaker-sl', added_definition),
        ),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant)
    return variant


def _record(definition, *, amount: float, currency: str = 'JPY', when=NOW):
    return build_cost_record(
        category='equipment_purchase',
        amount=amount,
        currency=currency,
        source_kind='user_entered',
        recorded_at_utc=when,
        equipment_definition_id=definition.definition_id,
        equipment_definition_version=definition.version,
        equipment_definition_sha256=definition.semantic_sha256,
    )


def _scenario(**kwargs):
    return build_cost_scenario(
        document_id=DOCUMENT_ID,
        name='incremental install',
        currency='JPY',
        **kwargs,
    )


def test_incremental_cost_distinguishes_new_from_owned(tmp_path: Path) -> None:
    (
        _sr,
        revision,
        variant_repository,
        equipment_repository,
    ) = _repositories(tmp_path)
    existing = _equipment('owned-speaker', 'b' * 64)
    added = _equipment('new-speaker', 'c' * 64)
    _persist_pair(equipment_repository, existing, added)
    variant = _variant(variant_repository, revision, existing, added)
    record = _record(added, amount=120000.0)
    evaluation = evaluate_variant_installation_cost(
        revision=revision,
        variant=variant,
        scenario=_scenario(),
        cost_records=(record,),
    )

    by_kind = {item.item_kind: item for item in evaluation.line_items}
    acquisition = by_kind['equipment_acquisition']
    assert acquisition.state == 'priced'
    assert acquisition.amount == 120000.0
    assert acquisition.entity_id == 'speaker-sl'
    existing_line = by_kind['equipment_existing']
    assert existing_line.entity_id == 'speaker-fl'
    assert existing_line.state == 'not_priced_by_scope'
    assert evaluation.totals_by_currency == {'JPY': 120000.0}
    assert evaluation.unknown_item_ids == ()
    # Identity is reproducible.
    repeated = evaluate_variant_installation_cost(
        revision=revision,
        variant=variant,
        scenario=_scenario(),
        cost_records=(record,),
    )
    assert repeated.evaluation_sha256 == evaluation.evaluation_sha256


def test_missing_price_is_unknown_not_zero_and_budget_is_unknown(
    tmp_path: Path,
) -> None:
    (
        _sr,
        revision,
        variant_repository,
        equipment_repository,
    ) = _repositories(tmp_path)
    existing = _equipment('owned-speaker', 'b' * 64)
    added = _equipment('new-speaker', 'c' * 64)
    _persist_pair(equipment_repository, existing, added)
    variant = _variant(variant_repository, revision, existing, added)
    scenario = _scenario(
        budget=BudgetConstraint(maximum_amount=1.0, currency='JPY')
    )
    evaluation = evaluate_variant_installation_cost(
        revision=revision,
        variant=variant,
        scenario=scenario,
        cost_records=(),
    )

    acquisition = next(
        item
        for item in evaluation.line_items
        if item.item_kind == 'equipment_acquisition'
    )
    assert acquisition.state == 'unknown_price'
    assert evaluation.totals_by_currency == {}
    # Nothing is priced: the budget state is UNKNOWN, never pass/over.
    assert evaluation.budget_state == 'unknown'

    generous = _scenario(
        budget=BudgetConstraint(maximum_amount=10_000_000.0, currency='JPY')
    )
    evaluation = evaluate_variant_installation_cost(
        revision=revision,
        variant=variant,
        scenario=generous,
        cost_records=(),
    )
    assert evaluation.budget_state == 'unknown'

    # A priced scope already over budget reports OVER even with unknowns.
    record = _record(added, amount=120000.0)
    tight = _scenario(
        budget=BudgetConstraint(maximum_amount=1.0, currency='JPY'),
        fixed_line_items=(
            FixedLineItem(
                item_id='unspecified-survey',
                category='fixed_project',
                description='site survey priced in USD, scenario JPY',
                amount=100.0,
                currency='USD',
            ),
        ),
    )
    evaluation = evaluate_variant_installation_cost(
        revision=revision,
        variant=variant,
        scenario=tight,
        cost_records=(record,),
    )
    assert evaluation.budget_state == 'over'
    vector = installation_cost_objective_vector(evaluation)
    cost_metric = vector.metric('o100c.incremental_acquisition_cost')
    assert cost_metric.state == 'missing'
    assert cost_metric.value is None
    unknown_metric = vector.metric('o100c.unknown_cost_item_count')
    assert unknown_metric.value == 1.0


def test_foreign_currency_is_never_silently_summed(tmp_path: Path) -> None:
    (
        _sr,
        revision,
        variant_repository,
        equipment_repository,
    ) = _repositories(tmp_path)
    existing = _equipment('owned-speaker', 'b' * 64)
    added = _equipment('new-speaker', 'c' * 64)
    _persist_pair(equipment_repository, existing, added)
    variant = _variant(variant_repository, revision, existing, added)
    usd_record = _record(added, amount=800.0, currency='USD')
    evaluation = evaluate_variant_installation_cost(
        revision=revision,
        variant=variant,
        scenario=_scenario(
            budget=BudgetConstraint(maximum_amount=1.0, currency='JPY')
        ),
        cost_records=(usd_record,),
    )
    acquisition = next(
        item
        for item in evaluation.line_items
        if item.item_kind == 'equipment_acquisition'
    )
    assert acquisition.state == 'foreign_currency'
    assert evaluation.totals_by_currency == {}


def test_effort_hours_exist_only_with_explicit_rates(tmp_path: Path) -> None:
    (
        _sr,
        revision,
        variant_repository,
        equipment_repository,
    ) = _repositories(tmp_path)
    existing = _equipment('owned-speaker', 'b' * 64)
    added = _equipment('new-speaker', 'c' * 64)
    _persist_pair(equipment_repository, existing, added)
    variant = _variant(variant_repository, revision, existing, added)
    record = _record(added, amount=120000.0)

    evaluation = evaluate_variant_installation_cost(
        revision=revision,
        variant=variant,
        scenario=_scenario(),
        cost_records=(record,),
    )
    assert evaluation.effort.added_speaker_count == 1
    assert evaluation.effort.estimated_person_hours is None
    effort_metric = installation_cost_objective_vector(evaluation).metric(
        'o100c.estimated_installation_effort_hours'
    )
    assert effort_metric.state == 'missing'

    with pytest.raises(ValueError, match='both hours_per_added_entity'):
        _scenario(labor_rate_per_hour=9000.0)

    labor = _scenario(
        hours_per_added_entity=1.5,
        labor_rate_per_hour=9000.0,
        fixed_line_items=(
            FixedLineItem(
                item_id='delivery',
                category='fixed_project',
                description='delivery fee',
                amount=5000.0,
                currency='JPY',
            ),
        ),
    )
    evaluation = evaluate_variant_installation_cost(
        revision=revision,
        variant=variant,
        scenario=labor,
        cost_records=(record,),
    )
    assert evaluation.effort.estimated_person_hours == pytest.approx(1.5)
    kinds = {item.item_kind for item in evaluation.line_items}
    assert 'labor' in kinds
    assert 'fixed_project' in kinds
    assert evaluation.totals_by_currency == {
        'JPY': pytest.approx(120000.0 + 13500.0 + 5000.0)
    }


def test_cost_repository_round_trip_and_tamper_rejection(tmp_path: Path) -> None:
    (
        scene_repository,
        revision,
        variant_repository,
        equipment_repository,
    ) = _repositories(tmp_path)
    repository = CadInstallationCostRepository(scene_repository)
    existing = _equipment('owned-speaker', 'b' * 64)
    added = _equipment('new-speaker', 'c' * 64)
    _persist_pair(equipment_repository, existing, added)
    variant = _variant(variant_repository, revision, existing, added)
    record = _record(added, amount=120000.0)

    persisted = repository.save_record(record, document_id=DOCUMENT_ID)
    assert repository.get_record(record.record_id) == persisted
    assert repository.list_records(DOCUMENT_ID) == (persisted,)

    evaluation = evaluate_variant_installation_cost(
        revision=revision,
        variant=variant,
        scenario=_scenario(),
        cost_records=(persisted,),
    )
    saved = repository.save_evaluation(evaluation)
    assert repository.get_evaluation(saved.evaluation_id) == saved
    assert repository.list_evaluations(DOCUMENT_ID) == (saved,)

    tampered = record.model_dump(mode='python')
    tampered['amount'] = float(tampered['amount']) + 1.0
    with pytest.raises(ValueError, match='semantic hash mismatch'):
        CostRecord.model_validate(tampered)
