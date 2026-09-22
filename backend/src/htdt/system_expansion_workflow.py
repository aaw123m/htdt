from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import hypot
from pathlib import Path
import sqlite3
from typing import Literal

from .cad_amplifier_headroom import PlaybackChainEvaluation
from .cad_constraint_models import CadConstraintPoint2D, CadConstraintSet
from .cad_coverage import CoverageEvaluation
from .cad_direct_level import DirectLevelEvaluation
from .cad_equipment_repository import CadEquipmentRepository
from .cad_proposal_robustness import (
    ProposalMultidimensionalRobustnessSpec,
    ProposalPerturbationSample,
    ProposalRobustnessSpec,
)
from .cad_repository import SceneRepository
from .cad_scene import (
    Direction3,
    Position3,
    SceneEntity,
    is_unassigned_speaker_role,
)
from .cad_search_models import CadSearchAxis
from .cad_schema import ensure_native_schema
from .cad_system_variant import (
    ChannelRoleBinding,
    EquipmentBindingRef,
    LifecycleState,
    ProposedEntitySpec,
    SystemVariant,
    VariantProvenanceItem,
    build_system_variant,
    materialize_system_variant,
)
from .cad_system_variant_lifecycle import SystemVariantAsBuiltRecord
from .cad_system_variant_measured_lifecycle import SystemVariantMeasuredRecord
from .cad_system_variant_measurement_campaign import (
    SystemVariantMeasurementCampaign,
    SystemVariantMeasurementCampaignCompletion,
    SystemVariantMeasurementPlan,
    SystemVariantMeasurementPlanCompletion,
)
from .cad_standards import StandardsEvaluation
from .cad_system_variant_repository import (
    CadSystemVariantRepository,
    SystemVariantApplication,
)
from .cad_topology_comparison import (
    SystemTopologyComparisonSpec,
    TopologyComparisonEvaluation,
    VariantEvaluationBundle,
)
from .cad_topology_search import (
    ProposedPlacementSpec,
    build_topology_placement_search_spec,
    generate_topology_placement_candidates,
    topology_candidate_to_system_variant,
)
from .cad_topology_search_repository import CadTopologySearchRepository
from .cad_topology_space import build_topology_search_spec
from .optimization_robustness import RobustnessEvaluation


LIFECYCLE_LABELS: dict[LifecycleState, str] = {
    "current": "現在",
    "proposed": "提案",
    "as_built": "設置済み",
    "measured": "実測済み",
}

MeasurementWorkflowState = Literal[
    "unplanned",
    "planned",
    "campaign_preregistered",
    "evidence_incomplete",
    "measured",
    "validation_pending",
]


@dataclass(frozen=True, slots=True)
class LifecyclePresentation:
    state: LifecycleState
    label: str
    validated: bool
    validation_label: str


@dataclass(frozen=True, slots=True)
class AdvancedProvenance:
    variant_id: str
    variant_sha256: str
    baseline_revision_id: str
    baseline_content_hash: str
    schema_version: str
    authority_version: str
    repository_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProposedGhost:
    entity_id: str
    name: str
    role: str | None
    lifecycle: Literal["proposed"] = "proposed"
    is_current_scene_truth: bool = False
    participates_in_measurement: bool = False
    selectable: bool = True


@dataclass(frozen=True, slots=True)
class ProposedEntityPresentation:
    name: str
    role: str | None
    lifecycle_label: str
    equipment: str
    install_zone: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class VariantPresentation:
    variant_id: str
    name: str
    lifecycle: LifecyclePresentation
    entities: tuple[ProposedEntityPresentation, ...]
    advanced: AdvancedProvenance
    stale: bool
    stale_reason: str | None


@dataclass(frozen=True, slots=True)
class ObjectivePresentation:
    objective_id: str
    value_text: str
    direction_label: str
    eligible: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class ComparisonVariantPresentation:
    variant_id: str
    name: str
    lifecycle_label: str
    eligibility_label: str
    blocked_reason: str | None
    coverage: str
    spl_headroom: str
    standards: str
    objectives: tuple[ObjectivePresentation, ...]
    pareto_state: str


@dataclass(frozen=True, slots=True)
class ComparisonPresentation:
    name: str
    variants: tuple[ComparisonVariantPresentation, ...]
    authority_stale: bool
    stale_reason: str | None


@dataclass(frozen=True, slots=True)
class MeasurementPresentation:
    variant_id: str
    state: MeasurementWorkflowState
    state_label: str
    detail: str
    measured: bool
    validated: bool
    validation_label: str


@dataclass(frozen=True, slots=True)
class RobustnessNavigationTarget:
    variant_id: str
    available: bool
    robustness_spec_id: str | None
    topology_candidate_id: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class ProposalRobustnessObjectivePresentation:
    objective_id: str
    direction_label: str
    nominal_text: str
    sampled_adverse_text: str
    sensitivity_text: str
    probability_text: str


@dataclass(frozen=True, slots=True)
class ProposalRobustnessPresentation:
    variant_id: str
    variant_name: str
    current: bool
    stale_reason: str | None
    sampling_label: str
    sample_count: int
    feasible_count: int
    infeasible_count: int
    failed_count: int
    unscored_count: int
    objectives: tuple[ProposalRobustnessObjectivePresentation, ...]
    robustness_spec_id: str
    robustness_spec_sha256: str
    candidate_variant_sha256: str


@dataclass(frozen=True, slots=True)
class ApplyPreview:
    variant_id: str
    name: str
    change_lines: tuple[str, ...]
    stale: bool
    stale_reason: str | None


@dataclass(frozen=True, slots=True)
class ProposalAuthoringResult:
    template_variant_id: str
    topology_search_id: str
    placement_search_id: str
    candidate_variant_ids: tuple[str, ...]
    feasible_candidate_count: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_semantic_id(prefix: str, payload: object) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{prefix}-{sha256(raw).hexdigest()[:20]}"


def lifecycle_presentation(
    state: LifecycleState,
    *,
    validated: bool = False,
) -> LifecyclePresentation:
    # "measured" is physical evidence state. Validation is a separate authority.
    validation_label = "検証済み" if validated else "未検証"
    return LifecyclePresentation(
        state=state,
        label=LIFECYCLE_LABELS[state],
        validated=validated,
        validation_label=validation_label,
    )


def proposed_ghosts(variant: SystemVariant) -> tuple[ProposedGhost, ...]:
    proposed_ids = {
        item.entity_id
        for item in variant.entity_lifecycle
        if item.state == "proposed"
    }
    return tuple(
        ProposedGhost(
            entity_id=item.entity.entity_id,
            name=item.entity.name,
            role=item.entity.speaker_role,
        )
        for item in variant.proposed_entities
        if item.entity.entity_id in proposed_ids
    )


class SystemExpansionWorkflowService:
    """Read/write workflow adapter over existing O100 authorities.

    The adapter owns presentation and navigation only. Domain truth remains in
    SystemVariant, topology comparison, O90/O100F robustness, explicit AsBuilt,
    and SystemVariant measurement campaign authorities.
    """

    def __init__(self, scene_repository: SceneRepository, document_id: str) -> None:
        self.scene_repository = scene_repository
        self.document_id = document_id
        self.path = Path(scene_repository.path)
        self.variant_repository = CadSystemVariantRepository(scene_repository)
        self.equipment_repository = CadEquipmentRepository(
            scene_repository,
            self.variant_repository,
        )
        self.topology_repository = CadTopologySearchRepository(
            self.variant_repository
        )

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _table_exists(self, connection: sqlite3.Connection, table: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone() is not None

    def _payload_rows(
        self,
        table: str,
        *,
        where: str = "",
        args: tuple[object, ...] = (),
        order: str = "seq ASC",
    ) -> tuple[str, ...]:
        with closing(self._connect()) as connection:
            if not self._table_exists(connection, table):
                return ()
            clause = f" WHERE {where}" if where else ""
            rows = connection.execute(
                f"SELECT payload_json FROM {table}{clause} ORDER BY {order}",
                args,
            ).fetchall()
        return tuple(str(row["payload_json"]) for row in rows)

    def variants(self) -> tuple[SystemVariant, ...]:
        return self.variant_repository.list_variants(self.document_id)

    def variant(self, variant_id: str) -> SystemVariant:
        variant = self.variant_repository.get_variant(variant_id)
        if variant is None or variant.document_id != self.document_id:
            raise KeyError(variant_id)
        return variant

    def _application(self, variant_id: str) -> SystemVariantApplication | None:
        return self.variant_repository.application_for_variant(variant_id)

    def _as_built(self, variant_id: str) -> SystemVariantAsBuiltRecord | None:
        rows = self._payload_rows(
            "cad_system_variant_as_built",
            where="variant_id=?",
            args=(variant_id,),
            order="seq DESC",
        )
        if not rows:
            return None
        return SystemVariantAsBuiltRecord.model_validate_json(rows[0])

    def _measured_records(
        self,
        variant_id: str,
    ) -> tuple[SystemVariantMeasuredRecord, ...]:
        return tuple(
            SystemVariantMeasuredRecord.model_validate_json(item)
            for item in self._payload_rows(
                "cad_system_variant_measured",
                where="variant_id=?",
                args=(variant_id,),
            )
        )

    def _plans(self, variant_id: str) -> tuple[SystemVariantMeasurementPlan, ...]:
        return tuple(
            SystemVariantMeasurementPlan.model_validate_json(item)
            for item in self._payload_rows(
                "cad_system_variant_measurement_plans",
                where="variant_id=?",
                args=(variant_id,),
            )
        )

    def _campaigns(
        self,
        variant_id: str,
    ) -> tuple[SystemVariantMeasurementCampaign, ...]:
        return tuple(
            SystemVariantMeasurementCampaign.model_validate_json(item)
            for item in self._payload_rows(
                "cad_system_variant_measurement_campaigns",
                where="variant_id=?",
                args=(variant_id,),
            )
        )

    def _plan_completions(
        self,
        campaign_id: str,
    ) -> tuple[SystemVariantMeasurementPlanCompletion, ...]:
        return tuple(
            SystemVariantMeasurementPlanCompletion.model_validate_json(item)
            for item in self._payload_rows(
                "cad_system_variant_measurement_plan_completions",
                where="campaign_id=?",
                args=(campaign_id,),
            )
        )

    def _campaign_completion(
        self,
        campaign_id: str,
    ) -> SystemVariantMeasurementCampaignCompletion | None:
        rows = self._payload_rows(
            "cad_system_variant_measurement_campaign_completions",
            where="campaign_id=?",
            args=(campaign_id,),
            order="seq DESC",
        )
        if not rows:
            return None
        return SystemVariantMeasurementCampaignCompletion.model_validate_json(rows[0])

    def lifecycle(self, variant_id: str) -> LifecyclePresentation:
        variant = self.variant(variant_id)
        application = self._application(variant_id)
        if application is None:
            if not variant.diff and all(
                item.state == "current" for item in variant.entity_lifecycle
            ):
                return lifecycle_presentation("current")
            return lifecycle_presentation("proposed")
        as_built = self._as_built(variant_id)
        if as_built is None:
            # Application changes modeled revision lineage only. It does not prove
            # physical installation, so the physical lifecycle remains proposed.
            return lifecycle_presentation("proposed")
        campaigns = self._campaigns(variant_id)
        measured_ids = {item.record_id for item in self._measured_records(variant_id)}
        completed = any(
            (completion := self._campaign_completion(campaign.campaign_id)) is not None
            and completion.measured_record_id in measured_ids
            for campaign in campaigns
        )
        if completed:
            return lifecycle_presentation("measured", validated=False)
        return lifecycle_presentation("as_built")

    def measurement(self, variant_id: str) -> MeasurementPresentation:
        self.variant(variant_id)
        as_built = self._as_built(variant_id)
        plans = self._plans(variant_id)
        campaigns = self._campaigns(variant_id)
        if as_built is None:
            return MeasurementPresentation(
                variant_id,
                "unplanned",
                "未計画",
                "設置済み記録がないためSystemVariant測定計画は作成できません。",
                False,
                False,
                "検証保留",
            )
        if not plans:
            return MeasurementPresentation(
                variant_id,
                "unplanned",
                "未計画",
                "SystemVariant固有の測定計画はまだありません。",
                False,
                False,
                "検証保留",
            )
        if not campaigns:
            return MeasurementPresentation(
                variant_id,
                "planned",
                "測定計画あり",
                "測定計画はありますがcampaignは事前登録されていません。",
                False,
                False,
                "検証保留",
            )
        plan_completions = tuple(
            completion
            for campaign in campaigns
            for completion in self._plan_completions(campaign.campaign_id)
        )
        completions = tuple(
            self._campaign_completion(item.campaign_id)
            for item in campaigns
        )
        completion = next((item for item in completions if item is not None), None)
        if completion is None and not plan_completions:
            return MeasurementPresentation(
                variant_id,
                "campaign_preregistered",
                "キャンペーン事前登録済み",
                "測定キャンペーンは事前登録済みです。実測根拠の収集はまだ開始されていません。",
                False,
                False,
                "検証保留",
            )
        if completion is None:
            return MeasurementPresentation(
                variant_id,
                "evidence_incomplete",
                "実測根拠不足",
                "一部の実測根拠はありますが、キャンペーン完了条件を満たしていません。",
                False,
                False,
                "検証保留",
            )
        measured = {
            item.record_id: item for item in self._measured_records(variant_id)
        }
        if completion.measured_record_id not in measured:
            return MeasurementPresentation(
                variant_id,
                "evidence_incomplete",
                "実測根拠不足",
                "キャンペーン完了が参照する実測authorityを再解決できません。",
                False,
                False,
                "検証保留",
            )
        return MeasurementPresentation(
            variant_id,
            "validation_pending",
            "実測済み・未検証",
            "SystemVariant campaign実測は完了しています。O60/R180 validationは別gateです。",
            True,
            False,
            "検証保留",
        )

    def equipment_choices(self) -> tuple[tuple[str, str], ...]:
        result: list[tuple[str, str]] = []
        for item in self.equipment_repository.list_definitions():
            label = (
                f"{item.manufacturer} {item.model}"
                if item.manufacturer and item.model
                else item.user_label or item.definition_id
            )
            result.append((item.semantic_sha256, label))
        return tuple(result)

    def create_single_speaker_proposal(
        self,
        *,
        proposal_name: str,
        role_id: str,
        equipment_sha256: str,
        zone_name: str,
        min_x_m: float,
        max_x_m: float,
        min_y_m: float,
        max_y_m: float,
        z_m: float,
        step_m: float,
        max_returned_candidates: int = 200,
    ) -> ProposalAuthoringResult:
        """Create O100A/B/C authorities from human-facing Room inputs.

        This is deliberately bounded to one added speaker per authoring action.
        It composes existing domain builders; it does not create an alternate
        topology/search/equipment authority in the UI layer.
        """
        name = proposal_name.strip()
        role = role_id.strip()
        zone = zone_name.strip()
        if not name:
            raise ValueError("提案名を入力してください。")
        if not role:
            raise ValueError("スピーカーの役割を入力してください。")
        if is_unassigned_speaker_role(role):
            raise ValueError(
                f"役割 {role} は未設定のプレースホルダーです。実際のチャンネル役割を指定してください。"
            )
        if not zone:
            raise ValueError("設置可能領域の名前を入力してください。")
        if max_x_m < min_x_m or max_y_m < min_y_m:
            raise ValueError("設置可能領域の最小値/最大値を確認してください。")
        if step_m <= 0.0:
            raise ValueError("探索刻みは0より大きくしてください。")

        baseline = self.scene_repository.latest(self.document_id)
        if baseline is None:
            raise ValueError("現在の部屋状態がありません。")
        definition = self.equipment_repository.get_definition_by_hash(
            equipment_sha256
        )
        if definition is None:
            raise ValueError("選択した機器定義を読み込めません。")

        # Placeholder tokens are not real channel identities: exclude them so a
        # SystemVariant never binds an unassigned speaker as a ChannelRoleBinding.
        existing_roles = tuple(
            entity.speaker_role
            for entity in baseline.document.entities
            if entity.kind == "speaker"
            and entity.speaker_role
            and not is_unassigned_speaker_role(entity.speaker_role)
        )
        if role in existing_roles:
            raise ValueError(
                f"役割 {role} は現在の構成に存在します。追加スピーカーには未使用の役割を指定してください。"
            )

        entity_id = _short_semantic_id(
            "proposal-speaker",
            {
                "baseline": baseline.content_hash,
                "name": name,
                "role": role,
                "equipment": equipment_sha256,
                "zone": zone,
                "bounds": [min_x_m, max_x_m, min_y_m, max_y_m, z_m],
            },
        )
        x_m = (min_x_m + max_x_m) * 0.5
        y_m = (min_y_m + max_y_m) * 0.5
        room = baseline.document.room
        if room is None:
            aim = Direction3(x=0.0, y=1.0, z=0.0)
        else:
            room_min_x, room_min_y, room_max_x, room_max_y = room.bounds_m
            dx = ((room_min_x + room_max_x) * 0.5) - x_m
            dy = ((room_min_y + room_max_y) * 0.5) - y_m
            magnitude = hypot(dx, dy)
            aim = (
                Direction3(x=0.0, y=1.0, z=0.0)
                if magnitude <= 1e-9
                else Direction3(x=dx / magnitude, y=dy / magnitude, z=0.0)
            )

        proposed_entity = SceneEntity(
            entity_id=entity_id,
            kind="speaker",
            name=role,
            speaker_role=role,
            position=Position3(x_m=x_m, y_m=y_m, z_m=z_m),
            size_m=definition.cabinet_envelope_m,
            aim_xyz=aim,
        )
        role_bindings = tuple(
            ChannelRoleBinding(role_id=item, display_name=item)
            for item in sorted({*existing_roles, role})
        )
        proposal = ProposedEntitySpec(
            spec_id=_short_semantic_id(
                "proposal-spec",
                {"entity": entity_id, "baseline": baseline.content_hash},
            ),
            entity=proposed_entity,
            role_binding_id=role,
            provenance=(
                VariantProvenanceItem(
                    key="o100g.authoring_surface",
                    value="room-placement",
                ),
            ),
        )
        equipment = EquipmentBindingRef(
            entity_id=entity_id,
            equipment_definition_id=definition.definition_id,
            equipment_definition_version=definition.version,
            equipment_definition_sha256=definition.semantic_sha256,
        )
        template = build_system_variant(
            baseline=baseline,
            name=name,
            role_bindings=role_bindings,
            proposed_entities=(proposal,),
            equipment_bindings=(equipment,),
            provenance=(
                VariantProvenanceItem(
                    key="o100g.install_zone",
                    value=zone,
                ),
            ),
            created_at_utc=_utc_now(),
        )
        self.variant_repository.save_variant(template)

        topology = build_topology_search_spec(
            baseline=baseline,
            template_variants=(template,),
            created_at_utc=_utc_now(),
        )
        self.topology_repository.save_topology_spec(topology)
        option = topology.options[0]

        region = (
            CadConstraintPoint2D(x_m=min_x_m, y_m=min_y_m),
            CadConstraintPoint2D(x_m=max_x_m, y_m=min_y_m),
            CadConstraintPoint2D(x_m=max_x_m, y_m=max_y_m),
            CadConstraintPoint2D(x_m=min_x_m, y_m=max_y_m),
        )
        placement = ProposedPlacementSpec(
            entity_id=entity_id,
            role_id=role,
            zone_id=zone,
            allowed_region=region,
            min_z_m=z_m,
            max_z_m=z_m,
            xyz_axes=(
                CadSearchAxis(
                    entity_id=entity_id,
                    axis="x",
                    min_m=min_x_m,
                    max_m=max_x_m,
                    step_m=step_m,
                ),
                CadSearchAxis(
                    entity_id=entity_id,
                    axis="y",
                    min_m=min_y_m,
                    max_m=max_y_m,
                    step_m=step_m,
                ),
                CadSearchAxis(
                    entity_id=entity_id,
                    axis="z",
                    min_m=z_m,
                    max_m=z_m,
                    step_m=step_m,
                ),
            ),
        )
        search = build_topology_placement_search_spec(
            baseline=baseline,
            template_variant=template,
            topology_spec=topology,
            topology_option_id=option.option_id,
            placement_specs=(placement,),
            constraint_set=CadConstraintSet(
                document_id=self.document_id,
                constraints=(),
            ),
            candidate_limit=10_000,
            created_at_utc=_utc_now(),
        )
        self.topology_repository.save_spec(search)
        page = generate_topology_placement_candidates(
            baseline=baseline,
            template_variant=template,
            spec=search,
            offset=0,
            limit=min(max(max_returned_candidates, 1), 500),
        )
        self.topology_repository.save_candidate_page(page)

        candidate_variant_ids: list[str] = []
        for index, candidate in enumerate(page.candidates):
            child = topology_candidate_to_system_variant(
                baseline=baseline,
                template_variant=template,
                spec=search,
                candidate=candidate,
                created_at_utc=_utc_now(),
                name=f"{name} / 候補 {index + 1}",
            )
            self.topology_repository.save_candidate_variant(
                candidate.candidate_id,
                child,
            )
            candidate_variant_ids.append(child.variant_id)
        return ProposalAuthoringResult(
            template_variant_id=template.variant_id,
            topology_search_id=topology.topology_search_id,
            placement_search_id=search.search_id,
            candidate_variant_ids=tuple(candidate_variant_ids),
            feasible_candidate_count=page.feasible_candidate_count,
        )

    def _equipment_label(
        self,
        variant: SystemVariant,
        entity_id: str,
    ) -> tuple[str, str | None]:
        binding = next(
            (item for item in variant.equipment_bindings if item.entity_id == entity_id),
            None,
        )
        if binding is None:
            return "未設定", "機器 / 音源モデルが設定されていません。"
        definition = self.equipment_repository.get_definition_by_hash(
            binding.equipment_definition_sha256
        )
        if definition is None:
            return "参照不可", "機器定義を読み込めません。"
        if definition.manufacturer and definition.model:
            return f"{definition.manufacturer} {definition.model}", None
        return definition.user_label or definition.definition_id, None

    def _install_zone(self, variant: SystemVariant, entity_id: str) -> str:
        provenance = {item.key: item.value for item in variant.provenance}
        authored_zone = provenance.get("o100g.install_zone")
        candidate_id = provenance.get("o100b.candidate_id")
        if candidate_id is None and authored_zone:
            return authored_zone
        if candidate_id is None and variant.parent_variant_id:
            parent = self.variant_repository.get_variant(variant.parent_variant_id)
            if parent is not None:
                return self._install_zone(parent, entity_id)
        if candidate_id is None:
            return "未設定"
        candidate = self.topology_repository.get_candidate(candidate_id)
        if candidate is None:
            return "参照不可"
        spec = self.topology_repository.get_spec(candidate.search_id)
        if spec is None:
            return "参照不可"
        placement = next(
            (item for item in spec.placement_specs if item.entity_id == entity_id),
            None,
        )
        return "未設定" if placement is None else placement.zone_id

    def variant_presentation(self, variant_id: str) -> VariantPresentation:
        variant = self.variant(variant_id)
        latest = self.scene_repository.latest(self.document_id)
        stale = bool(
            latest is None
            or (
                self._application(variant_id) is None
                and latest.revision_id != variant.baseline_revision_id
            )
        )
        entities = []
        for item in variant.proposed_entities:
            equipment, reason = self._equipment_label(
                variant,
                item.entity.entity_id,
            )
            entities.append(
                ProposedEntityPresentation(
                    name=item.entity.name,
                    role=item.entity.speaker_role,
                    lifecycle_label="提案",
                    equipment=equipment,
                    install_zone=self._install_zone(
                        variant,
                        item.entity.entity_id,
                    ),
                    reason=reason,
                )
            )
        advanced = AdvancedProvenance(
            variant_id=variant.variant_id,
            variant_sha256=variant.variant_sha256,
            baseline_revision_id=variant.baseline_revision_id,
            baseline_content_hash=variant.baseline_content_hash,
            schema_version=str(variant.schema_version),
            authority_version=variant.authority_version,
            repository_keys=tuple(item.key for item in variant.provenance),
        )
        return VariantPresentation(
            variant_id=variant.variant_id,
            name=variant.name,
            lifecycle=self.lifecycle(variant_id),
            entities=tuple(entities),
            advanced=advanced,
            stale=stale,
            stale_reason=(
                "baseline SceneRevisionが現在のrevisionではありません。再評価してください。"
                if stale
                else None
            ),
        )

    def ghost_preview(self, variant_id: str):
        variant = self.variant(variant_id)
        baseline = self.scene_repository.get(variant.baseline_revision_id)
        if baseline is None:
            raise ValueError("proposal baseline SceneRevisionがありません")
        scene = materialize_system_variant(baseline, variant)
        proposed_ids = {item.entity_id for item in proposed_ghosts(variant)}
        return tuple(
            entity for entity in scene.entities if entity.entity_id in proposed_ids
        )

    def apply_preview(self, variant_id: str) -> ApplyPreview:
        variant = self.variant(variant_id)
        latest = self.scene_repository.latest(self.document_id)
        stale = bool(
            latest is None
            or latest.revision_id != variant.baseline_revision_id
            or latest.content_hash != variant.baseline_content_hash
        )
        lines: list[str] = []
        for item in variant.diff:
            label = {"add": "追加", "remove": "削除", "replace": "置換"}[item.kind]
            entity = item.after_entity or item.before_entity
            name = item.entity_id if entity is None else entity.name
            role = ""
            if entity is not None and entity.speaker_role:
                role = (
                    " / 未設定"
                    if is_unassigned_speaker_role(entity.speaker_role)
                    else f" / {entity.speaker_role}"
                )
            lines.append(f"{label}: {name}{role}")
        return ApplyPreview(
            variant_id=variant.variant_id,
            name=variant.name,
            change_lines=tuple(lines),
            stale=stale,
            stale_reason=(
                "提案のbaselineが最新SceneRevisionと一致しません。"
                if stale
                else None
            ),
        )

    def apply(self, variant_id: str) -> SystemVariantApplication:
        # Exact existing application authority is the only write path.
        return self.variant_repository.apply_variant(
            variant_id,
            selected_by="native-o100-workflow",
        )

    def _exact_authority(
        self,
        *,
        table: str,
        id_column: str,
        authority_id: str,
        semantic_sha256: str,
        model_type,
        sha_attr: str,
    ):
        with closing(self._connect()) as connection:
            if not self._table_exists(connection, table):
                return None
            row = connection.execute(
                f"SELECT payload_json FROM {table} WHERE {id_column}=?",
                (authority_id,),
            ).fetchone()
        if row is None:
            return None
        value = model_type.model_validate_json(str(row["payload_json"]))
        if getattr(value, sha_attr) != semantic_sha256:
            return None
        return value

    @staticmethod
    def _scalar_text(value) -> str:
        if value.state == "available" and value.value is not None:
            return f"{value.value:.4g} {value.unit}"
        return value.reason or "利用不可"

    def _coverage_summary(self, ref) -> str:
        if ref is None:
            return "データなし"
        value = self._exact_authority(
            table="cad_coverage_evaluations",
            id_column="evaluation_id",
            authority_id=ref.authority_id,
            semantic_sha256=ref.semantic_sha256,
            model_type=CoverageEvaluation,
            sha_attr="evaluation_sha256",
        )
        if value is None:
            return "参照不可・再評価が必要"
        aggregate = value.aggregates.useful_coverage_fraction
        if aggregate.state != "available":
            return aggregate.reason or "利用不可"
        return f"有効coverage {aggregate.value:.3f}"

    def _spl_headroom_summary(self, bundle) -> str:
        parts: list[str] = []
        ref = bundle.direct_level_evaluation
        if ref is not None:
            direct = self._exact_authority(
                table="cad_direct_level_evaluations",
                id_column="evaluation_id",
                authority_id=ref.authority_id,
                semantic_sha256=ref.semantic_sha256,
                model_type=DirectLevelEvaluation,
                sha_attr="evaluation_sha256",
            )
            if direct is None:
                parts.append("direct SPL参照不可")
            else:
                aggregates = direct.aggregates
                parts.append(
                    "SPL " + self._scalar_text(aggregates.worst_seat_direct_level)
                )
                parts.append(
                    "連続headroom "
                    + self._scalar_text(aggregates.worst_seat_continuous_headroom)
                )
                parts.append(
                    "peak headroom "
                    + self._scalar_text(aggregates.worst_seat_peak_headroom)
                )
        amp_ref = bundle.amplifier_headroom_evaluation
        if amp_ref is not None:
            amp = self._exact_authority(
                table="cad_playback_chain_evaluations",
                id_column="evaluation_id",
                authority_id=amp_ref.authority_id,
                semantic_sha256=amp_ref.semantic_sha256,
                model_type=PlaybackChainEvaluation,
                sha_attr="evaluation_sha256",
            )
            if amp is None:
                parts.append("amp headroom参照不可")
            else:
                parts.append(
                    "amp margin "
                    + self._scalar_text(amp.amplifier_constrained_target_margin)
                )
        return " / ".join(parts) if parts else "データなし"

    def _standards_summary(self, ref) -> str:
        if ref is None:
            return "データなし"
        value = self._exact_authority(
            table="cad_standards_evaluations",
            id_column="evaluation_id",
            authority_id=ref.authority_id,
            semantic_sha256=ref.semantic_sha256,
            model_type=StandardsEvaluation,
            sha_attr="evaluation_sha256",
        )
        if value is None:
            return "参照不可・再評価が必要"
        counts: dict[str, int] = {}
        for result in value.results:
            counts[result.status] = counts.get(result.status, 0) + 1
        ordered = (
            ("PASS", "PASS"),
            ("FAIL", "FAIL"),
            ("UNKNOWN", "UNKNOWN"),
            ("NOT_APPLICABLE", "対象外"),
        )
        summary = [
            f"{label} {counts[state]}"
            for state, label in ordered
            if counts.get(state, 0)
        ]
        return " / ".join(summary) if summary else "結果なし"

    def _latest_comparison_rows(self):
        specs = self._payload_rows(
            "cad_topology_comparison_specs",
            where="document_id=?",
            args=(self.document_id,),
            order="seq DESC",
        )
        if not specs:
            return None
        spec = SystemTopologyComparisonSpec.model_validate_json(specs[0])
        evaluations = self._payload_rows(
            "cad_topology_comparison_evaluations",
            where="comparison_id=?",
            args=(spec.comparison_id,),
            order="seq DESC",
        )
        evaluation = (
            None
            if not evaluations
            else TopologyComparisonEvaluation.model_validate_json(evaluations[0])
        )
        bundles = tuple(
            VariantEvaluationBundle.model_validate_json(item)
            for item in self._payload_rows(
                "cad_topology_comparison_bundles",
                where="comparison_id=?",
                args=(spec.comparison_id,),
            )
        )
        return spec, evaluation, {item.variant_id: item for item in bundles}

    def comparison(self) -> ComparisonPresentation | None:
        rows = self._latest_comparison_rows()
        if rows is None:
            return None
        spec, evaluation, bundle_by_variant = rows
        latest = self.scene_repository.latest(self.document_id)
        stale = bool(
            latest is None
            or spec.baseline_scene_revision_id != latest.revision_id
            or spec.baseline_scene_content_hash != latest.content_hash
        )
        eligibility_by_variant = (
            {}
            if evaluation is None
            else {item.variant_id: item for item in evaluation.eligibility}
        )
        result = None if evaluation is None else evaluation.pareto_result
        views: list[ComparisonVariantPresentation] = []
        for candidate in spec.candidate_variants:
            variant = self.variant_repository.get_variant(candidate.variant_id)
            name = candidate.comparison_label or (
                variant.name if variant is not None else "参照不可"
            )
            bundle = bundle_by_variant.get(candidate.variant_id)
            eligibility = eligibility_by_variant.get(candidate.variant_id)
            issues = () if eligibility is None else eligibility.issues
            blocked = None
            if evaluation is None:
                blocked = "比較評価がまだありません。"
            elif eligibility is None:
                blocked = "比較eligibilityを再解決できません。"
            elif issues:
                blocked = " / ".join(item.detail for item in issues)
            objectives: list[ObjectivePresentation] = []
            if bundle is not None:
                for metric in bundle.objective_vector.metrics:
                    definition = metric.effective_definition()
                    direction = "小さいほど優先" if definition.direction == "minimize" else "大きいほど優先"
                    if metric.state == "available" and metric.value is not None:
                        value_text = f"{metric.value:.4g} {metric.unit}"
                        eligible_metric = True
                        reason = None
                    else:
                        value_text = "利用不可"
                        eligible_metric = False
                        reason = f"{metric.objective_id}: {metric.state}"
                    objectives.append(
                        ObjectivePresentation(
                            metric.objective_id,
                            value_text,
                            direction,
                            eligible_metric,
                            reason,
                        )
                    )
            coverage = (
                "データなし"
                if bundle is None
                else self._coverage_summary(bundle.coverage_evaluation)
            )
            spl_headroom = (
                "データなし"
                if bundle is None
                else self._spl_headroom_summary(bundle)
            )
            standards = (
                "データなし"
                if bundle is None
                else self._standards_summary(bundle.standards_evaluation)
            )
            if result is None:
                pareto = "比較対象外" if blocked else "未評価"
            elif candidate.variant_id in result.non_dominated_candidate_ids:
                pareto = "非劣"
            else:
                dominators = result.dominated_by.get(candidate.variant_id, ())
                pareto = (
                    "支配あり"
                    if dominators
                    else "比較対象外"
                )
            lifecycle = (
                self.lifecycle(candidate.variant_id).label
                if variant is not None
                else ("現在" if candidate.role == "current" else "提案")
            )
            views.append(
                ComparisonVariantPresentation(
                    variant_id=candidate.variant_id,
                    name=name,
                    lifecycle_label=lifecycle,
                    eligibility_label=(
                        "比較可能"
                        if eligibility is not None and eligibility.state == "ELIGIBLE"
                        else "比較不可"
                    ),
                    blocked_reason=blocked,
                    coverage=coverage,
                    spl_headroom=spl_headroom,
                    standards=standards,
                    objectives=tuple(objectives),
                    pareto_state=pareto,
                )
            )
        return ComparisonPresentation(
            name=spec.name,
            variants=tuple(views),
            authority_stale=stale,
            stale_reason=(
                "比較authorityは現在のSceneRevisionに対してstaleです。再評価してください。"
                if stale
                else None
            ),
        )

    def robustness_target(self, variant_id: str) -> RobustnessNavigationTarget:
        variant = self.variant(variant_id)
        provenance = {item.key: item.value for item in variant.provenance}
        topology_candidate_id = provenance.get("o100b.candidate_id")
        rows = self._payload_rows(
            "cad_proposal_robustness_specs",
            where="candidate_variant_id=?",
            args=(variant_id,),
            order="seq DESC",
        )
        if not rows:
            return RobustnessNavigationTarget(
                variant_id,
                False,
                None,
                topology_candidate_id,
                "この提案には保存済みO90/O100Fばらつき評価がありません。",
            )
        payload = json.loads(rows[0])
        if payload.get("candidate_variant_sha256") != variant.variant_sha256:
            return RobustnessNavigationTarget(
                variant_id,
                False,
                payload.get("robustness_spec_id"),
                topology_candidate_id,
                "ばらつきauthorityのSystemVariant hashがstaleです。",
            )
        return RobustnessNavigationTarget(
            variant_id,
            True,
            payload.get("robustness_spec_id"),
            topology_candidate_id,
            None,
        )

    @staticmethod
    def _proposal_robustness_spec(payload_json: str):
        payload = json.loads(payload_json)
        if payload.get("sampling_strategy") == "deterministic_multidimensional_bounded":
            return ProposalMultidimensionalRobustnessSpec.model_validate(payload)
        return ProposalRobustnessSpec.model_validate(payload)

    def proposal_robustness_presentation(
        self,
        variant_id: str,
    ) -> ProposalRobustnessPresentation | None:
        """Read exact O100F robustness evidence for the selected proposal.

        This is presentation-only. It does not reinterpret proposal evidence as
        an O10 CadCandidate or write into the canonical O90 repository.
        """
        variant = self.variant(variant_id)
        rows = self._payload_rows(
            "cad_proposal_robustness_specs",
            where="candidate_variant_id=?",
            args=(variant_id,),
            order="seq DESC",
        )
        if not rows:
            return None
        spec = self._proposal_robustness_spec(rows[0])
        if (
            spec.document_id != self.document_id
            or spec.candidate_variant_id != variant.variant_id
            or spec.candidate_variant_sha256 != variant.variant_sha256
        ):
            raise ValueError("提案のばらつきauthorityが選択SystemVariantと一致しません。")
        baseline = self.scene_repository.get(spec.scene_revision_id)
        if (
            baseline is None
            or baseline.document_id != self.document_id
            or baseline.content_hash != spec.scene_content_hash
        ):
            raise ValueError("提案のばらつきauthorityのbaselineを再解決できません。")

        samples = tuple(
            ProposalPerturbationSample.model_validate_json(item)
            for item in self._payload_rows(
                "cad_proposal_perturbation_samples",
                where="robustness_spec_id=?",
                args=(spec.robustness_spec_id,),
                order="sample_index ASC",
            )
        )
        evaluations = tuple(
            RobustnessEvaluation.model_validate_json(item)
            for item in self._payload_rows(
                "cad_proposal_robustness_evaluations",
                where="robustness_spec_id=?",
                args=(spec.robustness_spec_id,),
                order="objective_id ASC",
            )
        )
        for sample in samples:
            if (
                sample.robustness_spec_id != spec.robustness_spec_id
                or sample.robustness_spec_sha256 != spec.robustness_spec_sha256
                or sample.candidate_id != variant.variant_id
            ):
                raise ValueError("提案のばらつきsample authorityが一致しません。")
        sample_ids = {item.sample_id for item in samples}
        for evaluation in evaluations:
            if (
                evaluation.robustness_spec_id != spec.robustness_spec_id
                or evaluation.robustness_spec_sha256 != spec.robustness_spec_sha256
                or evaluation.candidate_id != variant.variant_id
                or not set(evaluation.sample_ids).issubset(sample_ids)
            ):
                raise ValueError("提案のばらつきevaluation authorityが一致しません。")

        latest = self.scene_repository.latest(self.document_id)
        current = bool(
            latest is not None
            and latest.revision_id == spec.scene_revision_id
            and latest.content_hash == spec.scene_content_hash
        )
        feasible_count = sum(1 for item in samples if item.feasible)
        infeasible_count = sum(1 for item in samples if not item.feasible)
        failed_count = sum(1 for item in samples if item.failure_reason)
        unscored_count = sum(
            1 for item in samples
            if item.feasible and item.objective_vector is None
        )

        objective_views: list[ProposalRobustnessObjectivePresentation] = []
        for evaluation in evaluations:
            direction_label = (
                "小さいほど優先"
                if evaluation.direction == "minimize"
                else "大きいほど優先"
            )
            sensitivity_values = [
                item.central_slope_per_unit
                for item in evaluation.local_sensitivities
                if item.central_slope_per_unit is not None
            ]
            sensitivity_text = (
                "—"
                if not sensitivity_values
                else " / ".join(f"{value:.4g}" for value in sensitivity_values)
            )
            probability_text = "確率モデルなし"
            if evaluation.percentile_semantics == "explicit_probability_model":
                p95 = None
                if (
                    evaluation.sampled_envelope is not None
                    and evaluation.sampled_envelope.percentile_values is not None
                ):
                    p95 = evaluation.sampled_envelope.percentile_values.get("p95")
                pieces = []
                if p95 is not None:
                    pieces.append(f"p95 {p95:.4g} {evaluation.objective_unit}")
                if evaluation.constraint_violation_probability is not None:
                    pieces.append(
                        "制約違反確率 "
                        f"{evaluation.constraint_violation_probability:.3g}"
                    )
                probability_text = " / ".join(pieces) or "明示確率モデル"
            objective_views.append(
                ProposalRobustnessObjectivePresentation(
                    objective_id=evaluation.objective_id,
                    direction_label=direction_label,
                    nominal_text=(
                        f"{evaluation.nominal_value:.4g} "
                        f"{evaluation.objective_unit}"
                    ),
                    sampled_adverse_text=(
                        f"{evaluation.sampled_worst_value:.4g} "
                        f"{evaluation.objective_unit}"
                    ),
                    sensitivity_text=sensitivity_text,
                    probability_text=probability_text,
                )
            )

        sampling_label = (
            "多次元 bounded sampling"
            if isinstance(spec, ProposalMultidimensionalRobustnessSpec)
            else "局所 ± sensitivity"
        )
        return ProposalRobustnessPresentation(
            variant_id=variant.variant_id,
            variant_name=variant.name,
            current=current,
            stale_reason=(
                None
                if current
                else "baseline SceneRevisionが現在状態と一致しないため再評価が必要です。"
            ),
            sampling_label=sampling_label,
            sample_count=len(samples),
            feasible_count=feasible_count,
            infeasible_count=infeasible_count,
            failed_count=failed_count,
            unscored_count=unscored_count,
            objectives=tuple(objective_views),
            robustness_spec_id=spec.robustness_spec_id,
            robustness_spec_sha256=spec.robustness_spec_sha256,
            candidate_variant_sha256=spec.candidate_variant_sha256,
        )
