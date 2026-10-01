from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from htdt.cad_equipment import (
    DirectivityCapability,
    EquipmentDataProvenance,
    build_equipment_definition,
)
from htdt.cad_equipment_evidence import build_equipment_manual_evidence
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
    ProposedEntitySpec,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.optimization_objectives import (
    ObjectiveDefinition,
    ObjectiveMetric,
    ObjectiveValidDomain,
)
from htdt.system_expansion_workflow import (
    ComparisonPresentation,
    ComparisonVariantPresentation,
    ProposalEquipmentChange,
    ProposalLinkInput,
    ProposalSpeakerInput,
    SystemExpansionWorkflowService,
    lifecycle_presentation,
    proposed_ghosts,
)
from htdt.workflow_navigation import (
    WorkspaceId,
    normalize_workspace_context,
)


DOCUMENT_ID = "o100g-workflow-ux"
NOW = "2026-09-20T02:00:00+00:00"


def _save_equipment(repository, definition) -> None:
    """Persist explicit manual evidence for every cited provenance, then save."""
    for evidence in build_equipment_manual_evidence(
        definition,
        actor='equipment-test-fixture',
        recorded_at_utc=NOW,
    ):
        repository.save_evidence(evidence)
    repository.save_definition(definition)


def _speaker(entity_id: str, role: str, x_m: float) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind="speaker",
        name=role,
        speaker_role=role,
        position=Position3(x_m=x_m, y_m=1.0, z_m=1.0),
        size_m=Size3(x_m=0.2, y_m=0.25, z_m=0.4),
    )


def _fixture(tmp_path: Path):
    scene = SceneRepository(tmp_path / "cad.sqlite3")
    baseline = scene.save(
        SceneDocument(
            document_id=DOCUMENT_ID,
            room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
            entities=(
                _speaker("fl", "FL", 1.0),
                _speaker("fr", "FR", 5.0),
            ),
        ),
        parent_revision_id=None,
    ).revision
    repository = CadSystemVariantRepository(scene)
    variant = build_system_variant(
        baseline=baseline,
        name="proposed 5.0.2 A",
        role_bindings=(
            ChannelRoleBinding(role_id="FL", display_name="Front Left"),
            ChannelRoleBinding(role_id="FR", display_name="Front Right"),
            ChannelRoleBinding(role_id="SL", display_name="Surround Left"),
        ),
        proposed_entities=(
            ProposedEntitySpec(
                spec_id="proposal-sl",
                entity=_speaker("sl", "SL", 0.7),
                role_binding_id="SL",
            ),
        ),
        created_at_utc=NOW,
    )
    repository.save_variant(variant)
    service = SystemExpansionWorkflowService(scene, DOCUMENT_ID)
    return scene, baseline, repository, variant, service


def _save_fixture_equipment(service: SystemExpansionWorkflowService):
    provenance = EquipmentDataProvenance(
        evidence_kind="user_defined",
        source_name="workflow UX fixture",
        source_version="1",
        source_reference="fixture",
        source_sha256=sha256(b"workflow-ux-equipment").hexdigest(),
    )
    definition = build_equipment_definition(
        definition_id="fixture-surround",
        version="1",
        identity_kind="user_defined",
        user_label="Fixture Surround",
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.2, y_m=0.25, z_m=0.4),
        acoustic_reference_point_m=Offset3(x_m=0.0, y_m=0.0, z_m=0.0),
        directivity=DirectivityCapability(
            tier="unknown",
            data_format="unknown",
            provenance=provenance,
        ),
    )
    _save_equipment(service.equipment_repository, definition)
    return definition


def test_lifecycle_japanese_labels_and_measured_is_not_validation() -> None:
    assert lifecycle_presentation("current").label == "現在"
    assert lifecycle_presentation("proposed").label == "提案"
    assert lifecycle_presentation("as_built").label == "設置済み"

    measured = lifecycle_presentation("measured")
    assert measured.label == "実測済み"
    assert measured.validated is False
    assert measured.validation_label == "未検証"

    validated = lifecycle_presentation("measured", validated=True)
    assert validated.label == "実測済み"
    assert validated.validated is True
    assert validated.validation_label == "検証済み"


def test_proposed_ghost_is_selectable_but_never_current_truth(tmp_path: Path) -> None:
    _scene, _baseline, _repository, variant, service = _fixture(tmp_path)

    ghosts = proposed_ghosts(variant)
    assert len(ghosts) == 1
    assert ghosts[0].lifecycle == "proposed"
    assert ghosts[0].is_current_scene_truth is False
    assert ghosts[0].participates_in_measurement is False
    assert ghosts[0].selectable is True
    assert service.ghost_preview(variant.variant_id)[0].entity_id == "sl"


def test_variant_selection_uses_human_name_and_keeps_provenance_advanced(
    tmp_path: Path,
) -> None:
    _scene, _baseline, _repository, variant, service = _fixture(tmp_path)

    view = service.variant_presentation(variant.variant_id)
    assert view.name == "proposed 5.0.2 A"
    assert view.lifecycle.label == "提案"
    assert view.entities[0].name == "SL"
    assert not hasattr(view.entities[0], "entity_id")
    assert view.advanced.variant_id == variant.variant_id
    assert view.advanced.variant_sha256 == variant.variant_sha256


def test_comparison_presents_blocked_reason_direction_and_no_overall_score(
    tmp_path: Path,
    monkeypatch,
) -> None:
    scene, baseline, _repository, variant, service = _fixture(tmp_path)
    definition = ObjectiveDefinition(
        objective_id="coverage",
        quantity="coverage",
        unit="ratio",
        direction="maximize",
        valid_domain=ObjectiveValidDomain(
            kind="bounded_real",
            minimum=0.0,
            maximum=1.0,
        ),
        comparison_model_id="fixture",
        comparison_model_version="1",
    )
    metric = ObjectiveMetric(
        objective_id="coverage",
        value=0.82,
        unit="ratio",
        direction="maximize",
        definition=definition,
    )
    bundle = SimpleNamespace(
        coverage_evaluation=None,
        direct_level_evaluation=None,
        amplifier_headroom_evaluation=None,
        standards_evaluation=None,
        objective_vector=SimpleNamespace(metrics=(metric,)),
    )
    candidate = SimpleNamespace(
        variant_id=variant.variant_id,
        comparison_label="proposed 5.0.2 A",
        role="proposed",
    )
    spec = SimpleNamespace(
        name="3.0.2 vs 5.0.2",
        baseline_scene_revision_id=baseline.revision_id,
        baseline_scene_content_hash=baseline.content_hash,
        candidate_variants=(candidate,),
    )
    eligibility = SimpleNamespace(
        variant_id=variant.variant_id,
        state="BLOCKED",
        issues=(SimpleNamespace(detail="指向性データなし"),),
    )
    evaluation = SimpleNamespace(
        eligibility=(eligibility,),
        pareto_result=None,
    )
    monkeypatch.setattr(
        service,
        "_latest_comparison_rows",
        lambda: (spec, evaluation, {variant.variant_id: bundle}),
    )

    view = service.comparison()
    assert view is not None
    item = view.variants[0]
    assert item.name == "proposed 5.0.2 A"
    assert item.eligibility_label == "比較不可"
    assert item.blocked_reason == "指向性データなし"
    assert item.coverage == "データなし"
    assert item.objectives[0].direction_label == "大きいほど優先"
    assert not hasattr(item, "overall_score")
    assert "overall_score" not in ComparisonVariantPresentation.__dataclass_fields__
    assert "overall_score" not in ComparisonPresentation.__dataclass_fields__


def test_comparison_authority_summaries_keep_real_values_and_unavailable_reasons(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _scene, _baseline, _repository, _variant, service = _fixture(tmp_path)
    ref = SimpleNamespace(authority_id="authority-1", semantic_sha256="0" * 64)
    scalar = lambda value, unit: SimpleNamespace(
        state="available",
        value=value,
        unit=unit,
        reason=None,
    )
    coverage = SimpleNamespace(
        aggregates=SimpleNamespace(
            useful_coverage_fraction=scalar(0.75, "ratio")
        )
    )
    direct = SimpleNamespace(
        aggregates=SimpleNamespace(
            worst_seat_direct_level=scalar(82.0, "dB SPL"),
            worst_seat_continuous_headroom=scalar(3.2, "dB"),
            worst_seat_peak_headroom=scalar(6.1, "dB"),
        )
    )
    amp = SimpleNamespace(
        amplifier_constrained_target_margin=scalar(2.5, "dB")
    )
    standards = SimpleNamespace(
        results=(
            SimpleNamespace(status="PASS"),
            SimpleNamespace(status="PASS"),
            SimpleNamespace(status="UNKNOWN"),
        )
    )

    def exact_authority(**kwargs):
        name = kwargs["model_type"].__name__
        return {
            "CoverageEvaluation": coverage,
            "DirectLevelEvaluation": direct,
            "PlaybackChainEvaluation": amp,
            "StandardsEvaluation": standards,
        }[name]

    monkeypatch.setattr(service, "_exact_authority", exact_authority)
    bundle = SimpleNamespace(
        direct_level_evaluation=ref,
        amplifier_headroom_evaluation=ref,
    )
    assert service._coverage_summary(ref) == "有効カバレッジ 0.750"
    assert "SPL 82 dB SPL" in service._spl_headroom_summary(bundle)
    assert "連続ヘッドルーム 3.2 dB" in service._spl_headroom_summary(bundle)
    assert "アンプ余量 2.5 dB" in service._spl_headroom_summary(bundle)
    assert service._standards_summary(ref) == "合格 2 / 不明 1"

    unsupported = SimpleNamespace(
        aggregates=SimpleNamespace(
            useful_coverage_fraction=SimpleNamespace(
                state="unsupported",
                value=None,
                unit="ratio",
                reason="指向性データなし",
            )
        )
    )
    monkeypatch.setattr(
        service,
        "_exact_authority",
        lambda **_kwargs: unsupported,
    )
    assert service._coverage_summary(ref) == "指向性データなし"


def test_apply_uses_existing_application_authority_and_creates_revision(
    tmp_path: Path,
) -> None:
    scene, baseline, repository, variant, service = _fixture(tmp_path)

    preview = service.apply_preview(variant.variant_id)
    assert preview.stale is False
    application = service.apply(variant.variant_id)

    latest = scene.latest(DOCUMENT_ID)
    assert latest is not None
    assert latest.revision_id == application.applied_revision_id
    assert latest.parent_revision_id == baseline.revision_id
    assert repository.application_for_variant(variant.variant_id) == application
    assert service.lifecycle(variant.variant_id).state == "proposed"
    # Apply never forges explicit physical completion.
    assert service._as_built(variant.variant_id) is None


def test_generic_measurement_does_not_imply_variant_measured(tmp_path: Path) -> None:
    _scene, _baseline, _repository, variant, service = _fixture(tmp_path)

    # No SystemVariant-specific AsBuilt/plan/campaign authority exists. Generic
    # measurement tables are deliberately irrelevant to this state machine.
    view = service.measurement(variant.variant_id)
    assert view.state == "unplanned"
    assert view.measured is False
    assert service.lifecycle(variant.variant_id).state == "proposed"


def test_preregistered_campaign_is_distinct_from_incomplete_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _scene, _baseline, _repository, variant, service = _fixture(tmp_path)
    campaign = SimpleNamespace(campaign_id="campaign-1")
    monkeypatch.setattr(service, "_as_built", lambda _variant_id: object())
    monkeypatch.setattr(service, "_plans", lambda _variant_id: (object(),))
    monkeypatch.setattr(service, "_campaigns", lambda _variant_id: (campaign,))
    monkeypatch.setattr(service, "_plan_completions", lambda _campaign_id: ())
    monkeypatch.setattr(service, "_campaign_completion", lambda _campaign_id: None)

    view = service.measurement(variant.variant_id)
    assert view.state == "campaign_preregistered"
    assert view.state_label == "キャンペーン事前登録済み"
    assert view.measured is False


def test_measured_campaign_presentation_remains_validation_pending(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _scene, _baseline, _repository, variant, service = _fixture(tmp_path)
    campaign = SimpleNamespace(campaign_id="campaign-1")
    completion = SimpleNamespace(measured_record_id="measured-1")
    measured = SimpleNamespace(record_id="measured-1")

    monkeypatch.setattr(service, "_as_built", lambda _variant_id: object())
    monkeypatch.setattr(service, "_plans", lambda _variant_id: (object(),))
    monkeypatch.setattr(service, "_campaigns", lambda _variant_id: (campaign,))
    monkeypatch.setattr(
        service,
        "_plan_completions",
        lambda _campaign_id: (object(),),
    )
    monkeypatch.setattr(
        service,
        "_campaign_completion",
        lambda campaign_id: completion if campaign_id == "campaign-1" else None,
    )
    monkeypatch.setattr(
        service,
        "_measured_records",
        lambda _variant_id: (measured,),
    )

    view = service.measurement(variant.variant_id)
    assert view.state == "validation_pending"
    assert view.state_label == "実測済み・未検証"
    assert view.measured is True
    assert view.validated is False
    assert view.validation_label == "検証保留"


def test_stale_proposal_is_presented_as_reason_not_numeric_zero(tmp_path: Path) -> None:
    scene, baseline, _repository, variant, service = _fixture(tmp_path)
    changed = baseline.document.model_copy(
        update={
            "entities": baseline.document.entities
            + (
                SceneEntity(
                    entity_id="seat",
                    kind="seat",
                    name="Seat",
                    position=Position3(x_m=3.0, y_m=3.0, z_m=0.5),
                    size_m=Size3(x_m=0.5, y_m=0.5, z_m=1.0),
                ),
            )
        }
    )
    scene.save(changed, parent_revision_id=baseline.revision_id)

    view = service.variant_presentation(variant.variant_id)
    assert view.stale is True
    assert view.stale_reason is not None
    assert "再評価" in view.stale_reason


def test_proposed_variant_navigates_to_existing_robustness_area_by_semantic_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _scene, _baseline, _repository, variant, service = _fixture(tmp_path)
    payload = (
        '{"robustness_spec_id":"proposal-robustness:fixture",'
        f'"candidate_variant_sha256":"{variant.variant_sha256}"'
        "}"
    )
    original = service._payload_rows

    def rows(table, **kwargs):
        if table == "cad_proposal_robustness_specs":
            return (payload,)
        return original(table, **kwargs)

    monkeypatch.setattr(service, "_payload_rows", rows)

    target = service.robustness_target(variant.variant_id)
    assert target.available is True
    assert target.variant_id == variant.variant_id
    assert target.robustness_spec_id == "proposal-robustness:fixture"


def test_room_authoring_uses_o100_authorities_without_internal_id_entry(
    tmp_path: Path,
) -> None:
    scene, baseline, repository, _existing, service = _fixture(tmp_path)
    equipment = _save_fixture_equipment(service)

    result = service.create_single_speaker_proposal(
        proposal_name="proposed 3.0.0 + SL",
        role_id="SL",
        equipment_sha256=equipment.semantic_sha256,
        zone_name="left surround wall",
        min_x_m=0.5,
        max_x_m=1.5,
        min_y_m=2.0,
        max_y_m=3.0,
        z_m=1.2,
        step_m=0.5,
        max_returned_candidates=24,
    )

    template = repository.get_variant(result.template_variant_id)
    assert template is not None
    assert template.name == "proposed 3.0.0 + SL"
    assert template.proposed_entities[0].entity.speaker_role == "SL"
    assert template.equipment_bindings[0].equipment_definition_sha256 == equipment.semantic_sha256
    assert scene.latest(DOCUMENT_ID).revision_id == baseline.revision_id
    assert result.feasible_candidate_count > 0
    assert result.candidate_variant_ids

    candidate = repository.get_variant(result.candidate_variant_ids[0])
    assert candidate is not None
    assert candidate.parent_variant_id == template.variant_id
    assert candidate.equipment_bindings == template.equipment_bindings
    view = service.variant_presentation(template.variant_id)
    assert view.entities[0].install_zone == "left surround wall"


def test_workflow_navigation_uses_semantic_sections_without_internal_ids() -> None:
    assert (
        normalize_workspace_context(WorkspaceId.ROOM, "system-proposal")
        == "placement"
    )
    assert (
        normalize_workspace_context(
            WorkspaceId.OPTIMIZATION,
            "topology-comparison",
        )
        == "comparison"
    )
    assert (
        normalize_workspace_context(
            WorkspaceId.OPTIMIZATION,
            "variant-robustness",
        )
        == "robustness"
    )
    assert (
        normalize_workspace_context(
            WorkspaceId.OPTIMIZATION,
            "variant-measurement",
        )
        == "validation"
    )


def _surround_inputs(equipment_sha256: str) -> tuple[ProposalSpeakerInput, ...]:
    return (
        ProposalSpeakerInput(
            role_id="SL",
            equipment_sha256=equipment_sha256,
            zone_name="left surround wall",
            min_x_m=0.5,
            max_x_m=1.5,
            min_y_m=2.0,
            max_y_m=3.0,
            min_z_m=1.0,
            max_z_m=1.2,
            step_m=0.5,
        ),
        ProposalSpeakerInput(
            role_id="SR",
            equipment_sha256=equipment_sha256,
            zone_name="right surround wall",
            min_x_m=4.5,
            max_x_m=5.5,
            min_y_m=2.0,
            max_y_m=3.0,
            min_z_m=1.0,
            max_z_m=1.2,
            step_m=0.5,
        ),
    )


def _mirror_pair_rules() -> tuple[ProposalLinkInput, ...]:
    return (
        ProposalLinkInput(
            master_role_id="SL",
            slave_role_id="SR",
            relation="mirror_x",
        ),
        ProposalLinkInput(
            master_role_id="SL",
            slave_role_id="SR",
            relation="equal_y",
        ),
        ProposalLinkInput(
            master_role_id="SL",
            slave_role_id="SR",
            relation="equal_z",
        ),
    )


def test_multi_speaker_linked_proposal_builds_one_variant_lineage(
    tmp_path: Path,
) -> None:
    scene, baseline, repository, _existing, service = _fixture(tmp_path)
    equipment = _save_fixture_equipment(service)

    result = service.create_topology_proposal(
        proposal_name="proposed 5.0.2 B",
        speakers=_surround_inputs(equipment.semantic_sha256),
        linked_rules=_mirror_pair_rules(),
        max_returned_candidates=24,
    )

    template = repository.get_variant(result.template_variant_id)
    assert template is not None
    assert template.name == "proposed 5.0.2 B"
    assert template.parent_variant_id is None
    assert [
        item.entity.speaker_role for item in template.proposed_entities
    ] == ["SL", "SR"]
    # Every proposed entity carries an exact persisted equipment binding.
    bound = {
        item.entity_id: item.equipment_definition_sha256
        for item in template.equipment_bindings
    }
    for proposal in template.proposed_entities:
        assert bound[proposal.entity.entity_id] == equipment.semantic_sha256
    assert {
        item.role_id for item in template.role_bindings
    } == {"FL", "FR", "SL", "SR"}
    # Authoring never mutates the current topology.
    assert scene.latest(DOCUMENT_ID).revision_id == baseline.revision_id

    spec = service.topology_repository.get_spec(result.placement_search_id)
    assert spec is not None
    assert spec.template_variant_id == template.variant_id
    placements = {item.role_id: item for item in spec.placement_specs}
    assert set(placements) == {"SL", "SR"}
    # The master keeps independent grid axes; every slave coordinate is derived
    # by the existing linked authority, so it has no searched axis.
    assert sorted(axis.axis for axis in placements["SL"].xyz_axes) == [
        "x",
        "y",
        "z",
    ]
    assert placements["SR"].xyz_axes == ()
    sl_entity = placements["SL"].entity_id
    sr_entity = placements["SR"].entity_id
    assert {
        (rule.master_entity_id, rule.relation, rule.slave_entity_id)
        for rule in spec.linked_rules
    } == {
        (sl_entity, "mirror_x", sr_entity),
        (sl_entity, "equal_y", sr_entity),
        (sl_entity, "equal_z", sr_entity),
    }

    # Deterministic candidates remain children of the same template lineage.
    assert result.candidate_variant_ids
    assert result.feasible_candidate_count > 0
    for variant_id in result.candidate_variant_ids:
        candidate = repository.get_variant(variant_id)
        assert candidate is not None
        assert candidate.parent_variant_id == template.variant_id
        assert [
            item.entity.speaker_role for item in candidate.proposed_entities
        ] == ["SL", "SR"]
        assert candidate.equipment_bindings == template.equipment_bindings

    view = service.variant_presentation(template.variant_id)
    zones = {item.name: item.install_zone for item in view.entities}
    assert zones == {
        "SL": "left surround wall",
        "SR": "right surround wall",
    }


def test_applying_linked_pair_candidate_creates_one_complete_revision(
    tmp_path: Path,
) -> None:
    scene, baseline, repository, _existing, service = _fixture(tmp_path)
    equipment = _save_fixture_equipment(service)
    result = service.create_topology_proposal(
        proposal_name="proposed 5.0.2 C",
        speakers=_surround_inputs(equipment.semantic_sha256),
        linked_rules=_mirror_pair_rules(),
        max_returned_candidates=8,
    )
    assert result.candidate_variant_ids

    application = service.apply(result.candidate_variant_ids[0])
    applied = scene.get(application.applied_revision_id)
    assert applied is not None
    assert applied.parent_revision_id == baseline.revision_id
    # One SceneRevision carries the complete topology change.
    assert [entity.speaker_role for entity in applied.document.entities] == [
        "FL",
        "FR",
        "SL",
        "SR",
    ]
    sl = next(
        entity
        for entity in applied.document.entities
        if entity.speaker_role == "SL"
    )
    sr = next(
        entity
        for entity in applied.document.entities
        if entity.speaker_role == "SR"
    )
    # Existing O100B linked authority derived SR around the room-center axis.
    assert sr.position.x_m == pytest.approx(6.0 - sl.position.x_m)
    assert sr.position.y_m == pytest.approx(sl.position.y_m)
    assert sr.position.z_m == pytest.approx(sl.position.z_m)


def test_independent_multi_speaker_proposal_keeps_full_search_axes(
    tmp_path: Path,
) -> None:
    _scene, _baseline, repository, _existing, service = _fixture(tmp_path)
    equipment = _save_fixture_equipment(service)

    result = service.create_topology_proposal(
        proposal_name="independent surrounds",
        speakers=_surround_inputs(equipment.semantic_sha256),
        max_returned_candidates=8,
    )

    spec = service.topology_repository.get_spec(result.placement_search_id)
    assert spec is not None
    assert spec.linked_rules == ()
    for placement in spec.placement_specs:
        assert sorted(axis.axis for axis in placement.xyz_axes) == [
            "x",
            "y",
            "z",
        ]
    assert result.candidate_variant_ids


def test_topology_proposal_removes_and_rebinds_existing_speakers(
    tmp_path: Path,
) -> None:
    scene, baseline, repository, _existing, service = _fixture(tmp_path)
    equipment = _save_fixture_equipment(service)

    result = service.create_topology_proposal(
        proposal_name="swap FR for SL",
        speakers=(
            ProposalSpeakerInput(
                role_id="SL",
                equipment_sha256=equipment.semantic_sha256,
                zone_name="left surround wall",
                min_x_m=0.5,
                max_x_m=1.5,
                min_y_m=2.0,
                max_y_m=3.0,
                min_z_m=1.0,
                max_z_m=1.2,
                step_m=0.5,
            ),
        ),
        remove_role_ids=("FR",),
        equipment_overrides=(
            ProposalEquipmentChange(
                role_id="FL",
                equipment_sha256=equipment.semantic_sha256,
            ),
        ),
        max_returned_candidates=8,
    )

    template = repository.get_variant(result.template_variant_id)
    assert template is not None
    removed = {
        item.entity_id for item in template.diff if item.kind == "remove"
    }
    assert removed == {"fr"}
    assert {
        item.role_id for item in template.role_bindings
    } == {"FL", "SL"}
    sl_entity = template.proposed_entities[0].entity.entity_id
    bound = {
        item.entity_id: item.equipment_definition_sha256
        for item in template.equipment_bindings
    }
    assert bound == {
        sl_entity: equipment.semantic_sha256,
        "fl": equipment.semantic_sha256,
    }

    application = service.apply(result.candidate_variant_ids[0])
    applied = scene.get(application.applied_revision_id)
    assert [entity.speaker_role for entity in applied.document.entities] == [
        "FL",
        "SL",
    ]


class _CapturedTemplate(Exception):
    def __init__(self, variant) -> None:
        super().__init__()
        self.variant = variant


def test_single_speaker_wrapper_matches_one_row_topology_proposal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # The second equivalent call would collide on the UNIQUE variant_sha256
    # constraint, so the multi-entity template is captured before persistence.
    _scene, _baseline, repository, _existing, service = _fixture(tmp_path)
    equipment = _save_fixture_equipment(service)

    single = service.create_single_speaker_proposal(
        proposal_name="proposed 3.0.0 + SL",
        role_id="SL",
        equipment_sha256=equipment.semantic_sha256,
        zone_name="left surround wall",
        min_x_m=0.5,
        max_x_m=1.5,
        min_y_m=2.0,
        max_y_m=3.0,
        z_m=1.2,
        step_m=0.5,
        max_returned_candidates=24,
    )
    single_template = repository.get_variant(single.template_variant_id)
    assert single_template is not None

    def capture(variant) -> None:
        raise _CapturedTemplate(variant)

    monkeypatch.setattr(service.variant_repository, "save_variant", capture)
    with pytest.raises(_CapturedTemplate) as captured:
        service.create_topology_proposal(
            proposal_name="proposed 3.0.0 + SL",
            speakers=(
                ProposalSpeakerInput(
                    role_id="SL",
                    equipment_sha256=equipment.semantic_sha256,
                    zone_name="left surround wall",
                    min_x_m=0.5,
                    max_x_m=1.5,
                    min_y_m=2.0,
                    max_y_m=3.0,
                    min_z_m=1.2,
                    max_z_m=1.2,
                    step_m=0.5,
                ),
            ),
            max_returned_candidates=24,
        )

    multi_template = captured.value.variant
    # The compatibility wrapper composes the same builder and authority set,
    # so both calls produce the identical canonical proposal identity.
    assert multi_template.variant_sha256 == single_template.variant_sha256
    assert multi_template.proposed_entities == single_template.proposed_entities
    assert multi_template.equipment_bindings == single_template.equipment_bindings
    assert multi_template.role_bindings == single_template.role_bindings


def test_topology_proposal_input_validation_fails_closed(tmp_path: Path) -> None:
    _scene, _baseline, _repository, _existing, service = _fixture(tmp_path)
    equipment = _save_fixture_equipment(service)
    speakers = _surround_inputs(equipment.semantic_sha256)

    def create(**overrides):
        kwargs = {
            "proposal_name": "invalid proposal",
            "speakers": speakers,
        }
        kwargs.update(overrides)
        return service.create_topology_proposal(**kwargs)

    with pytest.raises(ValueError, match="提案名"):
        create(proposal_name="  ")
    with pytest.raises(ValueError, match="1つ以上"):
        create(speakers=())
    with pytest.raises(ValueError, match="役割を入力"):
        create(speakers=(replace(speakers[0], role_id=" "),))
    with pytest.raises(ValueError, match="プレースホルダー"):
        create(speakers=(replace(speakers[0], role_id="UNASSIGNED-1"),))
    with pytest.raises(ValueError, match="重複"):
        create(speakers=(speakers[0], speakers[0]))
    with pytest.raises(ValueError, match="最小値/最大値"):
        create(speakers=(replace(speakers[0], max_x_m=0.0),))
    with pytest.raises(ValueError, match="探索刻み"):
        create(speakers=(replace(speakers[0], step_m=0.0),))
    with pytest.raises(ValueError, match="機器定義"):
        create(speakers=(replace(speakers[0], equipment_sha256="0" * 64),))
    with pytest.raises(ValueError, match="現在の構成に存在します"):
        create(speakers=(replace(speakers[0], role_id="FL"),))
    with pytest.raises(ValueError, match="基準役割"):
        create(
            linked_rules=(
                ProposalLinkInput(
                    master_role_id="SBL",
                    slave_role_id="SR",
                    relation="mirror_x",
                ),
            )
        )
    with pytest.raises(ValueError, match="対象役割"):
        create(
            linked_rules=(
                ProposalLinkInput(
                    master_role_id="SL",
                    slave_role_id="SBR",
                    relation="equal_y",
                ),
            )
        )
    with pytest.raises(ValueError, match="異なる2つ"):
        create(
            linked_rules=(
                ProposalLinkInput(
                    master_role_id="SL",
                    slave_role_id="SL",
                    relation="equal_y",
                ),
            )
        )
    with pytest.raises(ValueError, match="ミラー軸"):
        create(
            linked_rules=(
                ProposalLinkInput(
                    master_role_id="SL",
                    slave_role_id="SR",
                    relation="equal_y",
                    mirror_axis_x_m=3.0,
                ),
            )
        )
    with pytest.raises(ValueError, match="重複"):
        create(
            linked_rules=(
                ProposalLinkInput(
                    master_role_id="SL",
                    slave_role_id="SR",
                    relation="equal_y",
                ),
                ProposalLinkInput(
                    master_role_id="SL",
                    slave_role_id="SR",
                    relation="equal_y",
                ),
            )
        )
    with pytest.raises(ValueError, match="削除対象"):
        create(remove_role_ids=("TFL",))
    with pytest.raises(ValueError, match="機器変更対象"):
        create(
            equipment_overrides=(
                ProposalEquipmentChange(
                    role_id="TFL",
                    equipment_sha256=equipment.semantic_sha256,
                ),
            )
        )
    with pytest.raises(ValueError, match="追加スピーカー"):
        create(
            equipment_overrides=(
                ProposalEquipmentChange(
                    role_id="SL",
                    equipment_sha256=equipment.semantic_sha256,
                ),
            )
        )
    with pytest.raises(ValueError, match="削除対象のため機器"):
        create(
            remove_role_ids=("FL",),
            equipment_overrides=(
                ProposalEquipmentChange(
                    role_id="FL",
                    equipment_sha256=equipment.semantic_sha256,
                ),
            ),
        )


def _point_fixture(tmp_path: Path):
    """Fixture with a measurement point so lifecycle write paths are exercisable."""
    scene = SceneRepository(tmp_path / "cad.sqlite3")
    baseline = scene.save(
        SceneDocument(
            document_id=DOCUMENT_ID,
            room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
            entities=(
                _speaker("fl", "FL", 1.0),
                _speaker("fr", "FR", 5.0),
                SceneEntity(
                    entity_id="mlp",
                    kind="measurement_point",
                    name="MLP",
                    position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
                ),
            ),
        ),
        parent_revision_id=None,
    ).revision
    repository = CadSystemVariantRepository(scene)
    variant = build_system_variant(
        baseline=baseline,
        name="proposed 5.0.2 A",
        role_bindings=(
            ChannelRoleBinding(role_id="FL", display_name="Front Left"),
            ChannelRoleBinding(role_id="FR", display_name="Front Right"),
            ChannelRoleBinding(role_id="SL", display_name="Surround Left"),
        ),
        proposed_entities=(
            ProposedEntitySpec(
                spec_id="proposal-sl",
                entity=_speaker("sl", "SL", 0.7),
                role_binding_id="SL",
            ),
        ),
        created_at_utc=NOW,
    )
    repository.save_variant(variant)
    service = SystemExpansionWorkflowService(scene, DOCUMENT_ID)
    return scene, baseline, repository, variant, service


def test_apply_then_explicit_as_built_record_promotes_lifecycle(
    tmp_path: Path,
) -> None:
    scene, _baseline, _repository, variant, service = _point_fixture(tmp_path)

    # Apply alone must not promote the physical lifecycle.
    service.apply(variant.variant_id)
    assert service.lifecycle(variant.variant_id).state == "proposed"

    preview = service.as_built_preview(variant.variant_id)
    assert preview.ready is True
    assert len(preview.diffs) == 1
    assert preview.diffs[0].entity_id == "sl"
    assert preview.diffs[0].moved is False
    assert preview.lineage_revision_count == 1

    record = service.record_as_built(variant.variant_id, confirmed_by="installer")
    assert record.application_id is not None
    assert service._as_built(variant.variant_id) == record
    assert service.lifecycle(variant.variant_id).state == "as_built"
    # A second record for the same application is refused before any write.
    assert service.as_built_preview(variant.variant_id).ready is False


def test_as_built_preview_flags_position_drift_and_unrelated_head(
    tmp_path: Path,
) -> None:
    scene, _baseline, _repository, variant, service = _point_fixture(tmp_path)
    application = service.apply(variant.variant_id)
    applied = scene.get(application.applied_revision_id)

    moved = applied.document.model_copy(
        update={
            "entities": tuple(
                entity.model_copy(
                    update={
                        "position": entity.position.model_copy(
                            update={"x_m": 0.75}
                        )
                    }
                )
                if entity.entity_id == "sl"
                else entity
                for entity in applied.document.entities
            )
        }
    )
    moved_revision = scene.save(moved, parent_revision_id=applied.revision_id)

    preview = service.as_built_preview(variant.variant_id)
    assert preview.ready is True
    assert preview.diffs[0].moved is True
    assert preview.lineage_revision_count == 2

    # A descendant edit that drops the proposed entity cannot be recorded
    # as this variant's installed state.
    removed = moved_revision.revision.document.model_copy(
        update={
            "entities": tuple(
                entity
                for entity in moved_revision.revision.document.entities
                if entity.entity_id != "sl"
            )
        }
    )
    scene.save(removed, parent_revision_id=moved_revision.revision.revision_id)
    preview = service.as_built_preview(variant.variant_id)
    assert preview.ready is False
    assert any("sl" in item or "SL" in item for item in preview.blocking)
    with pytest.raises(ValueError):
        service.record_as_built(variant.variant_id, confirmed_by="installer")


def test_variant_measurement_plan_and_campaign_progression(
    tmp_path: Path,
) -> None:
    _scene, _baseline, _repository, variant, service = _point_fixture(tmp_path)
    service.apply(variant.variant_id)
    service.record_as_built(variant.variant_id, confirmed_by="installer")

    options = service.measurement_plan_options(variant.variant_id)
    assert options.ready is True
    assert [item.entity_id for item in options.measurement_points] == ["mlp"]
    source_ids = {item.entity_id for item in options.sources}
    assert {"fl", "fr", "sl"} <= source_ids

    plan = service.create_measurement_plan(
        variant.variant_id,
        measurement_point_entity_id="mlp",
        source_entity_ids=("sl",),
        channel_role="SL",
        purpose="measure installed proposal",
    )
    assert plan.as_built_record_id == service._as_built(variant.variant_id).record_id
    view = service.measurement(variant.variant_id)
    assert view.state == "planned"

    pending = service.pending_measurement_targets(variant.variant_id)
    assert len(pending) == 1
    assert pending[0].channel_role == "SL"
    assert pending[0].expected_measurement_count == 1
    assert pending[0].recorded_evidence_count == 0
    assert pending[0].pending is True

    campaign, registration = service.preregister_measurement_campaign(
        variant.variant_id,
        purpose="SystemVariant measurement campaign",
    )
    assert campaign.campaign_id == registration.campaign_id
    view = service.measurement(variant.variant_id)
    assert view.state == "campaign_preregistered"
    assert view.measured is False


def test_measurement_plan_requires_variant_specific_targets(
    tmp_path: Path,
) -> None:
    _scene, _baseline, _repository, variant, service = _point_fixture(tmp_path)
    service.apply(variant.variant_id)
    service.record_as_built(variant.variant_id, confirmed_by="installer")

    with pytest.raises(ValueError, match="測定点"):
        service.create_measurement_plan(
            variant.variant_id,
            measurement_point_entity_id="missing-point",
            source_entity_ids=("sl",),
            channel_role="SL",
        )
    with pytest.raises(ValueError, match="スピーカー"):
        service.create_measurement_plan(
            variant.variant_id,
            measurement_point_entity_id="mlp",
            source_entity_ids=("mlp",),
            channel_role="SL",
        )
    with pytest.raises(ValueError, match="2回以上"):
        service.create_measurement_plan(
            variant.variant_id,
            measurement_point_entity_id="mlp",
            source_entity_ids=("sl",),
            channel_role="SL",
            repeatability_required=True,
        )


def test_measurement_plan_and_campaign_require_prior_lifecycle(
    tmp_path: Path,
) -> None:
    _scene, _baseline, _repository, variant, service = _point_fixture(tmp_path)

    options = service.measurement_plan_options(variant.variant_id)
    assert options.ready is False
    with pytest.raises(ValueError, match="設置済み"):
        service.create_measurement_plan(
            variant.variant_id,
            measurement_point_entity_id="mlp",
            source_entity_ids=("sl",),
            channel_role="SL",
        )
    with pytest.raises(ValueError, match="測定計画"):
        service.preregister_measurement_campaign(
            variant.variant_id,
            purpose="campaign without plans",
        )


def _seed_o60_validation_authority(
    scene: SceneRepository,
    *,
    document_id: str,
    scene_revision_id: str,
    scene_content_hash: str,
    search_spec_sha256: str = "aa" * 32,
) -> None:
    """Persist the canonical O60 authority pair the workflow gate replays."""
    connection = sqlite3.connect(scene.path)
    try:
        connection.execute(
            "INSERT INTO cad_search_specs ("
            "search_spec_id, document_id, scene_revision_id, scene_content_hash,"
            " constraint_workspace_hash, payload_json, search_spec_sha256,"
            " created_at_utc) VALUES (?,?,?,?,?,?,?,?)",
            (
                "spec-1",
                document_id,
                scene_revision_id,
                scene_content_hash,
                "dd" * 32,
                json.dumps(
                    {
                        "search_spec_id": "spec-1",
                        "search_spec_sha256": search_spec_sha256,
                        "document_id": document_id,
                        "scene_revision_id": scene_revision_id,
                        "scene_content_hash": scene_content_hash,
                    }
                ),
                search_spec_sha256,
                "2030-01-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO cad_model_validations ("
            "validation_id, document_id, search_spec_id, model_id,"
            " model_version, recommendation_gate, validation_sha256,"
            " payload_json, created_at_utc) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "validation-1",
                document_id,
                "spec-1",
                "rew-roomsim",
                "fixture-1",
                "eligible",
                "ee" * 32,
                json.dumps(
                    {
                        "validation_id": "validation-1",
                        "document_id": document_id,
                        "search_spec_id": "spec-1",
                        "search_spec_sha256": search_spec_sha256,
                        "evidence_scope": "owned_room",
                        "recommendation_gate": "eligible",
                    }
                ),
                "2030-01-01T00:00:10+00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _seed_r180_validation_authority(
    scene: SceneRepository,
    *,
    document_id: str,
    baseline_revision_id: str,
    variant: "SystemVariant",
    plan_semantic_sha256: str = "bb" * 32,
) -> None:
    """Persist a terminal R180 'validated' lifecycle event bound to variant."""
    connection = sqlite3.connect(scene.path)
    try:
        connection.execute(
            "INSERT INTO cad_calibration_plans ("
            "plan_id, document_id, scene_revision_id, system_variant_id,"
            " source_measurement_id, source_dataset_id, quality_report_id,"
            " plan_semantic_sha256, support_state, created_at_utc,"
            " payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "calibration-plan-1",
                document_id,
                baseline_revision_id,
                variant.variant_id,
                "m-fixture",
                "dataset-fixture",
                "report-fixture",
                plan_semantic_sha256,
                "supported",
                "2030-01-01T00:00:00+00:00",
                json.dumps(
                    {
                        "plan_id": "calibration-plan-1",
                        "plan_semantic_sha256": plan_semantic_sha256,
                        "document_id": document_id,
                        "system_variant_id": variant.variant_id,
                        "system_variant_sha256": variant.variant_sha256,
                    }
                ),
            ),
        )
        connection.execute(
            "INSERT INTO cad_calibration_lifecycle_events ("
            "event_id, plan_id, state, event_semantic_sha256, created_at_utc,"
            " payload_json) VALUES (?,?,?,?,?,?)",
            (
                "calibration-event-1",
                "calibration-plan-1",
                "validated",
                "cc" * 32,
                "2030-01-01T00:01:00+00:00",
                json.dumps(
                    {
                        "event_id": "calibration-event-1",
                        "state": "validated",
                        "calibration_plan_id": "calibration-plan-1",
                        "calibration_plan_semantic_sha256": plan_semantic_sha256,
                    }
                ),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _measured_campaign_state(service, variant, baseline):
    """Monkeypatch the campaign authorities to a completed measured state."""
    campaign = SimpleNamespace(campaign_id="campaign-1")
    completion = SimpleNamespace(measured_record_id="measured-1")
    measured = SimpleNamespace(
        record_id="measured-1",
        as_built_revision_id=baseline.revision_id,
        as_built_content_hash=baseline.content_hash,
    )
    return (
        ("_application", lambda _vid: SimpleNamespace(applied_revision_id=baseline.revision_id)),
        ("_as_built", lambda _vid: object()),
        ("_plans", lambda _vid: (object(),)),
        ("_campaigns", lambda _vid: (campaign,)),
        ("_plan_completions", lambda _cid: (object(),)),
        ("_campaign_completion", lambda cid: completion if cid == "campaign-1" else None),
        ("_measured_records", lambda _vid: (measured,)),
    )


def test_o60_validation_authority_resolves_measured_variant_as_validated(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """#812: canonical owned-room O60 evidence resolves the measured variant."""
    scene, baseline, _repository, variant, service = _fixture(tmp_path)
    _seed_o60_validation_authority(
        scene,
        document_id=DOCUMENT_ID,
        scene_revision_id=baseline.revision_id,
        scene_content_hash=baseline.content_hash,
    )
    for name, stub in _measured_campaign_state(service, variant, baseline):
        monkeypatch.setattr(service, name, stub)

    view = service.measurement(variant.variant_id)
    assert view.state == "validated"
    assert view.state_label == "実測済み・検証済み"
    assert view.measured is True
    assert view.validated is True
    assert view.validation_label == "検証済み"

    lifecycle = service.lifecycle(variant.variant_id)
    assert lifecycle.state == "measured"
    assert lifecycle.validated is True
    assert lifecycle.validation_label == "検証済み"


def test_r180_validation_authority_resolves_measured_variant_as_validated(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """#812: a terminal R180 'validated' event bound to the variant resolves."""
    scene, baseline, _repository, variant, service = _fixture(tmp_path)
    _seed_r180_validation_authority(
        scene,
        document_id=DOCUMENT_ID,
        baseline_revision_id=baseline.revision_id,
        variant=variant,
    )
    for name, stub in _measured_campaign_state(service, variant, baseline):
        monkeypatch.setattr(service, name, stub)

    view = service.measurement(variant.variant_id)
    assert view.state == "validated"
    assert view.validated is True
    assert service.lifecycle(variant.variant_id).validated is True


def test_measured_variant_without_canonical_validation_stays_pending(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """#812: an O60 record bound to a different scene revision never validates."""
    scene, baseline, _repository, variant, service = _fixture(tmp_path)
    _seed_o60_validation_authority(
        scene,
        document_id=DOCUMENT_ID,
        scene_revision_id=baseline.revision_id,
        scene_content_hash="ff" * 32,  # stale: does not match the as-built hash
    )
    for name, stub in _measured_campaign_state(service, variant, baseline):
        monkeypatch.setattr(service, name, stub)

    view = service.measurement(variant.variant_id)
    assert view.state == "validation_pending"
    assert view.validated is False
    assert view.validation_label == "検証保留"
    assert service.lifecycle(variant.variant_id).validated is False
