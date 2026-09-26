from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import hypot
from pathlib import Path
import sqlite3
from typing import Literal, Sequence

from .cad_amplifier_headroom import PlaybackChainEvaluation
from .cad_amplifier_headroom_repository import CadAmplifierHeadroomRepository
from .cad_constraint_models import CadConstraintPoint2D, CadConstraintSet
from .cad_coverage import CoverageEvaluation
from .cad_coverage_repository import CadCoverageRepository
from .cad_direct_level import (
    DirectLevelEvaluation,
    DirectLevelFrequencyBand,
    ReferenceInputCondition,
)
from .cad_direct_level_repository import CadDirectLevelRepository
from .cad_directivity_repository import CadDirectivityRepository
from .cad_equipment_binding_repository import (
    CadEquipmentBindingRepository,
)
from .cad_equipment_repository import CadEquipmentRepository
from .cad_installation_cost import CostRecord, CostScenario
from .cad_installation_cost_repository import CadInstallationCostRepository
from .cad_layout_profile import LayoutProfile
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
    is_listener_receiver_eligible,
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
from .cad_measurement_quality import MeasurementCapabilityClaim
from .cad_measurement_quality_repository import CadMeasurementQualityRepository
from .cad_measurement_repository import CadMeasurementRepository
from .cad_system_variant_lifecycle import (
    CadSystemVariantLifecycleRepository,
    SystemVariantAsBuiltRecord,
    build_system_variant_as_built_record,
)
from .cad_system_variant_measured_lifecycle import (
    CadSystemVariantMeasuredLifecycleRepository,
    SystemVariantMeasuredRecord,
)
from .cad_system_variant_measurement_campaign import (
    CadSystemVariantMeasurementCampaignRepository,
    SystemVariantMeasurementCampaign,
    SystemVariantMeasurementCampaignCompletion,
    SystemVariantMeasurementCampaignRegistration,
    SystemVariantMeasurementPlan,
    SystemVariantMeasurementPlanCompletion,
    SystemVariantMeasurementTarget,
    VariantMeasurementAcquisitionRequirement,
    build_system_variant_measurement_campaign,
    build_system_variant_measurement_plan,
)
from .cad_standards import StandardsEvaluation, StandardsProfile
from .cad_standards import build_user_standards_profile
from .cad_standards_repository import CadStandardsRepository
from .cad_system_variant_repository import (
    CadSystemVariantRepository,
    SystemVariantApplication,
)
from .cad_topology_comparison import (
    SystemTopologyComparisonSpec,
    TopologyComparisonEvaluation,
    VariantEvaluationBundle,
)
from .cad_topology_comparison_execution import (
    ComparisonEvaluationPolicy,
    CoverageLanePolicy,
    DirectLevelLanePolicy,
    PlaybackChainLanePolicy,
    TopologyComparisonExecution,
    execute_topology_comparison,
    installation_cost_resolver,
)
from .cad_topology_comparison_repository import CadTopologyComparisonRepository
from .cad_topology_search import (
    LinkRelation,
    LinkedPlacementRule,
    PlacementAngleAxis,
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
    "validated",
]


@dataclass(frozen=True, slots=True)
class LifecyclePresentation:
    state: LifecycleState
    label: str
    validated: bool
    validation_label: str
    #: A SystemVariantApplication authority exists for this variant: the
    #: proposal was applied to the modeled lineage but no AsBuilt record
    #: proves physical installation yet (#914).
    application_exists: bool = False
    applied_revision_id: str | None = None


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
class AsBuiltEntityDiff:
    """User-facing proposed-vs-installed pose comparison for one entity."""

    entity_id: str
    name: str
    role: str | None
    equipment: str
    proposed_pose: str
    actual_pose: str
    moved: bool


@dataclass(frozen=True, slots=True)
class AsBuiltPreview:
    """What the As-built confirmation would record, before any write."""

    variant_id: str
    ready: bool
    reason: str | None
    applied_revision_id: str | None
    target_revision_id: str | None
    lineage_revision_count: int
    diffs: tuple[AsBuiltEntityDiff, ...]
    blocking: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MeasurementPointOption:
    entity_id: str
    name: str
    position_text: str


@dataclass(frozen=True, slots=True)
class MeasurementSourceOption:
    entity_id: str
    name: str
    role: str | None


@dataclass(frozen=True, slots=True)
class MeasurementPlanOptions:
    """As-built-resolved entities a SystemVariant plan may target."""

    ready: bool
    reason: str | None
    measurement_points: tuple[MeasurementPointOption, ...]
    sources: tuple[MeasurementSourceOption, ...]


@dataclass(frozen=True, slots=True)
class PendingMeasurementTarget:
    """One preregistered target and how much evidence it still needs."""

    target_id: str
    plan_id: str
    measurement_point_entity_id: str
    channel_role: str
    expected_measurement_count: int
    recorded_evidence_count: int

    @property
    def pending(self) -> bool:
        return self.recorded_evidence_count < self.expected_measurement_count


@dataclass(frozen=True, slots=True)
class ProposalAuthoringResult:
    template_variant_id: str
    topology_search_id: str
    placement_search_id: str
    candidate_variant_ids: tuple[str, ...]
    feasible_candidate_count: int


@dataclass(frozen=True, slots=True)
class ProposalSpeakerInput:
    """One add-speaker operation inside a multi-entity topology proposal.

    Fields are human-facing Room inputs only; the service maps them to the
    exact O100A/B/C authorities (ProposedEntitySpec, EquipmentBindingRef,
    ProposedPlacementSpec). ``angle_axes`` passes through the existing
    PlacementAngleAxis authority so aim/toe-in ranges stay canonical.
    """

    role_id: str
    equipment_sha256: str
    zone_name: str
    min_x_m: float
    max_x_m: float
    min_y_m: float
    max_y_m: float
    min_z_m: float
    max_z_m: float
    step_m: float
    optional_role: bool = False
    angle_axes: tuple[PlacementAngleAxis, ...] = ()


@dataclass(frozen=True, slots=True)
class ProposalLinkInput:
    """One linked placement relation between two proposed speaker roles.

    ``relation`` is the existing O100B LinkRelation authority. ``mirror_axis_x_m``
    is valid only for ``mirror_x``; ``None`` derives the room-center axis inside
    the existing search authority.
    """

    master_role_id: str
    slave_role_id: str
    relation: LinkRelation
    mirror_axis_x_m: float | None = None
    tolerance_m: float = 1e-6


@dataclass(frozen=True, slots=True)
class ProposalEquipmentChange:
    """Re-bind one existing kept speaker to a different EquipmentDefinition."""

    role_id: str
    equipment_sha256: str


# Mirrors the O10 linked-derivation axis for each LinkRelation so the adapter
# can omit grid axes for coordinates the linked authority derives. Position
# synthesis itself stays inside the O10/O100B authorities.
_LINKED_RELATION_AXIS: dict[str, str] = {
    "mirror_x": "x",
    "equal_x": "x",
    "equal_delta_x": "x",
    "equal_y": "y",
    "equal_delta_y": "y",
    "equal_z": "z",
    "equal_delta_z": "z",
}


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
    application_exists: bool = False,
    applied_revision_id: str | None = None,
) -> LifecyclePresentation:
    # "measured" is physical evidence state. Validation is a separate authority.
    validation_label = "検証済み" if validated else "未検証"
    return LifecyclePresentation(
        state=state,
        label=LIFECYCLE_LABELS[state],
        validated=validated,
        validation_label=validation_label,
        application_exists=application_exists,
        applied_revision_id=applied_revision_id,
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

    def _o60_validates(
        self,
        variant: SystemVariant,
        measured: SystemVariantMeasuredRecord,
    ) -> bool:
        """An eligible owned-room O60 record bound to the exact as-built scene."""
        for row in self._payload_rows(
            "cad_model_validations",
            where="document_id=?",
            args=(variant.document_id,),
        ):
            record = json.loads(row)
            if (
                record.get("evidence_scope") != "owned_room"
                or record.get("recommendation_gate") != "eligible"
            ):
                continue
            for spec_row in self._payload_rows(
                "cad_search_specs",
                where="search_spec_id=?",
                args=(str(record.get("search_spec_id") or ""),),
            ):
                spec = json.loads(spec_row)
                if (
                    spec.get("search_spec_sha256")
                    == record.get("search_spec_sha256")
                    and spec.get("scene_revision_id")
                    == measured.as_built_revision_id
                    and spec.get("scene_content_hash")
                    == measured.as_built_content_hash
                ):
                    return True
        return False

    def _r180_validates(self, variant: SystemVariant) -> bool:
        """A terminal R180 'validated' event pinned to this exact variant."""
        for row in self._payload_rows(
            "cad_calibration_lifecycle_events",
            where="state=?",
            args=("validated",),
        ):
            event = json.loads(row)
            plan_id = event.get("calibration_plan_id")
            plan_sha = event.get("calibration_plan_semantic_sha256")
            if not plan_id or not plan_sha:
                continue
            for plan_row in self._payload_rows(
                "cad_calibration_plans",
                where="plan_id=?",
                args=(str(plan_id),),
            ):
                plan = json.loads(plan_row)
                if (
                    plan.get("plan_semantic_sha256") == plan_sha
                    and plan.get("document_id") == variant.document_id
                    and plan.get("system_variant_id") == variant.variant_id
                    and plan.get("system_variant_sha256")
                    == variant.variant_sha256
                ):
                    return True
        return False

    def _measured_validated(
        self,
        variant: SystemVariant,
        measured: SystemVariantMeasuredRecord,
    ) -> bool:
        """Whether canonical O60 or R180 authority validates this measured
        variant (#812): a completed measurement campaign alone never does."""
        return self._o60_validates(variant, measured) or self._r180_validates(
            variant
        )

    def lifecycle(self, variant_id: str) -> LifecyclePresentation:
        variant = self.variant(variant_id)
        application = self._application(variant_id)
        applied_revision_id = (
            None if application is None else application.applied_revision_id
        )
        if application is None:
            if not variant.diff and all(
                item.state == "current" for item in variant.entity_lifecycle
            ):
                return lifecycle_presentation("current")
            return lifecycle_presentation("proposed")
        as_built = self._as_built(variant_id)
        if as_built is None:
            # Application changes modeled revision lineage only. It does not prove
            # physical installation, so the physical lifecycle remains proposed —
            # but the presentation must expose that an application exists (#914).
            return lifecycle_presentation(
                "proposed",
                application_exists=True,
                applied_revision_id=applied_revision_id,
            )
        campaigns = self._campaigns(variant_id)
        measured_records = self._measured_records(variant_id)
        measured_by_id = {item.record_id: item for item in measured_records}
        completed_record = next(
            (
                measured_by_id[completion.measured_record_id]
                for campaign in campaigns
                if (completion := self._campaign_completion(campaign.campaign_id))
                is not None
                and completion.measured_record_id in measured_by_id
            ),
            None,
        )
        if completed_record is not None:
            return lifecycle_presentation(
                "measured",
                validated=self._measured_validated(variant, completed_record),
                application_exists=True,
                applied_revision_id=applied_revision_id,
            )
        return lifecycle_presentation(
            "as_built",
            application_exists=True,
            applied_revision_id=applied_revision_id,
        )

    def measurement(self, variant_id: str) -> MeasurementPresentation:
        variant = self.variant(variant_id)
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
        if self._measured_validated(variant, measured[completion.measured_record_id]):
            return MeasurementPresentation(
                variant_id,
                "validated",
                "実測済み・検証済み",
                "O60/R180 validation authority resolves this measured SystemVariant as validated.",
                True,
                True,
                "検証済み",
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
        """Create a one-speaker topology proposal from human-facing Room inputs.

        This is the single-row form of create_topology_proposal(): the same
        builder represents one added speaker without extra complexity, and
        composes the same existing domain authorities.
        """
        return self.create_topology_proposal(
            proposal_name=proposal_name,
            speakers=(
                ProposalSpeakerInput(
                    role_id=role_id,
                    equipment_sha256=equipment_sha256,
                    zone_name=zone_name,
                    min_x_m=min_x_m,
                    max_x_m=max_x_m,
                    min_y_m=min_y_m,
                    max_y_m=max_y_m,
                    min_z_m=z_m,
                    max_z_m=z_m,
                    step_m=step_m,
                ),
            ),
            max_returned_candidates=max_returned_candidates,
        )

    def create_topology_proposal(
        self,
        *,
        proposal_name: str,
        speakers: Sequence[ProposalSpeakerInput],
        linked_rules: Sequence[ProposalLinkInput] = (),
        remove_role_ids: Sequence[str] = (),
        equipment_overrides: Sequence[ProposalEquipmentChange] = (),
        layout_profile: LayoutProfile | None = None,
        max_returned_candidates: int = 200,
    ) -> ProposalAuthoringResult:
        """Create O100A/B/C authorities for a multi-speaker topology proposal.

        One authoring action builds one named SystemVariant template holding
        every added speaker (with exact equipment bindings), every removal of
        an existing speaker, and every equipment re-binding on kept speakers;
        one TopologySearchSpec option; one TopologyPlacementSearchSpec carrying
        the declared linked-placement rules; and the deterministic O100B
        candidate page plus candidate SystemVariants in the same proposal
        lineage. The adapter composes existing domain builders only — it never
        synthesizes pair positions or creates a second constraint authority.
        """
        name = proposal_name.strip()
        if not name:
            raise ValueError("提案名を入力してください。")
        drafts = tuple(speakers)
        if not drafts:
            raise ValueError(
                "追加するスピーカー / チャンネルを1つ以上指定してください。"
            )
        cleaned: list[tuple[ProposalSpeakerInput, str, str]] = []
        for draft in drafts:
            role = draft.role_id.strip()
            zone = draft.zone_name.strip()
            if not role:
                raise ValueError("スピーカーの役割を入力してください。")
            if is_unassigned_speaker_role(role):
                raise ValueError(
                    f"役割 {role} は未設定のプレースホルダーです。"
                    "実際のチャンネル役割を指定してください。"
                )
            if not zone:
                raise ValueError("設置可能領域の名前を入力してください。")
            if draft.max_x_m < draft.min_x_m or draft.max_y_m < draft.min_y_m:
                raise ValueError("設置可能領域の最小値/最大値を確認してください。")
            if draft.max_z_m < draft.min_z_m:
                raise ValueError("高さ範囲の最小値/最大値を確認してください。")
            if draft.step_m <= 0.0:
                raise ValueError("探索刻みは0より大きくしてください。")
            cleaned.append((draft, role, zone))
        roles = [role for _draft, role, _zone in cleaned]
        duplicated = sorted(
            {role for role in roles if roles.count(role) > 1}
        )
        if duplicated:
            raise ValueError(
                f"役割 {' / '.join(duplicated)} が提案内で重複しています。"
            )
        proposed_roles = set(roles)

        baseline = self.scene_repository.current_head(self.document_id)
        if baseline is None:
            raise ValueError("現在の部屋状態がありません。")
        baseline_speakers = tuple(
            entity
            for entity in baseline.document.entities
            if entity.kind == "speaker"
        )
        # Placeholder tokens are not real channel identities: exclude them so a
        # SystemVariant never binds an unassigned speaker as a ChannelRoleBinding.
        existing_roles = tuple(
            entity.speaker_role
            for entity in baseline_speakers
            if entity.speaker_role
            and not is_unassigned_speaker_role(entity.speaker_role)
        )
        existing_role_set = set(existing_roles)

        # Removals resolve a declared role to exactly one baseline speaker;
        # missing or duplicated roles fail closed instead of guessing a target.
        remove_roles = tuple(
            dict.fromkeys(role.strip() for role in remove_role_ids if role.strip())
        )
        remove_entity_ids: list[str] = []
        removed_role_set: set[str] = set()
        for role in remove_roles:
            matches = [
                entity
                for entity in baseline_speakers
                if (entity.speaker_role or "").strip() == role
            ]
            if not matches:
                raise ValueError(
                    f"削除対象の役割 {role} は現在の構成に存在しません。"
                )
            if len(matches) > 1:
                raise ValueError(
                    f"役割 {role} は現在の構成で重複しているため、"
                    "削除対象を一意に特定できません。"
                )
            remove_entity_ids.append(matches[0].entity_id)
            removed_role_set.add(role)

        definitions: list = []
        for draft, role, _zone in cleaned:
            definition = self.equipment_repository.get_definition_by_hash(
                draft.equipment_sha256
            )
            if definition is None:
                raise ValueError("選択した機器定義を読み込めません。")
            if role in existing_role_set and role not in removed_role_set:
                raise ValueError(
                    f"役割 {role} は現在の構成に存在します。"
                    "追加スピーカーには未使用の役割を指定するか、"
                    "既存役割の削除と組み合わせてください。"
                )
            definitions.append(definition)

        # Equipment changes re-bind kept baseline speakers inside the same
        # variant; they never silently retarget added or removed speakers.
        override_bindings: list[EquipmentBindingRef] = []
        override_targets: set[str] = set()
        for change in equipment_overrides:
            role = change.role_id.strip()
            if not role:
                raise ValueError("機器変更対象の役割を入力してください。")
            if role in proposed_roles:
                raise ValueError(
                    f"役割 {role} は追加スピーカーです。"
                    "機器は追加行で直接選択してください。"
                )
            matches = [
                entity
                for entity in baseline_speakers
                if (entity.speaker_role or "").strip() == role
            ]
            if not matches:
                raise ValueError(
                    f"機器変更対象の役割 {role} は現在の構成に存在しません。"
                )
            if len(matches) > 1:
                raise ValueError(
                    f"役割 {role} は現在の構成で重複しているため、"
                    "機器変更対象を一意に特定できません。"
                )
            target = matches[0]
            if target.entity_id in remove_entity_ids:
                raise ValueError(
                    f"役割 {role} は削除対象のため機器を変更できません。"
                )
            if target.entity_id in override_targets:
                raise ValueError(
                    f"役割 {role} の機器変更が重複しています。"
                )
            definition = self.equipment_repository.get_definition_by_hash(
                change.equipment_sha256
            )
            if definition is None:
                raise ValueError("選択した機器定義を読み込めません。")
            override_targets.add(target.entity_id)
            override_bindings.append(
                EquipmentBindingRef(
                    entity_id=target.entity_id,
                    equipment_definition_id=definition.definition_id,
                    equipment_definition_version=definition.version,
                    equipment_definition_sha256=definition.semantic_sha256,
                )
            )

        entity_ids: dict[str, str] = {}
        for draft, role, zone in cleaned:
            entity_ids[role] = _short_semantic_id(
                "proposal-speaker",
                {
                    "baseline": baseline.content_hash,
                    "name": name,
                    "role": role,
                    "equipment": draft.equipment_sha256,
                    "zone": zone,
                    "bounds": [
                        draft.min_x_m,
                        draft.max_x_m,
                        draft.min_y_m,
                        draft.max_y_m,
                        draft.min_z_m,
                        draft.max_z_m,
                    ],
                },
            )

        # Linked rules bind two proposed roles to the existing O10/G10 linked
        # derivation authority. The slave's derived coordinate must not also be
        # a searched grid axis, so it is omitted when the axes are compiled.
        links: list[LinkedPlacementRule] = []
        link_keys: set[tuple[str, str, str]] = set()
        derived_axes: dict[str, set[str]] = {}
        for rule in linked_rules:
            master = rule.master_role_id.strip()
            slave = rule.slave_role_id.strip()
            if master not in proposed_roles:
                raise ValueError(
                    f"連動ルールの基準役割 {master} は追加スピーカーに存在しません。"
                )
            if slave not in proposed_roles:
                raise ValueError(
                    f"連動ルールの対象役割 {slave} は追加スピーカーに存在しません。"
                )
            if master == slave:
                raise ValueError(
                    "連動ルールは異なる2つの追加スピーカーが必要です。"
                )
            if rule.relation not in _LINKED_RELATION_AXIS:
                raise ValueError(f"未対応の連動関係です: {rule.relation}")
            if rule.relation != "mirror_x" and rule.mirror_axis_x_m is not None:
                raise ValueError(
                    "ミラー軸は mirror_x 関係でのみ指定できます。"
                )
            key = (master, slave, rule.relation)
            if key in link_keys:
                raise ValueError(
                    f"連動ルール {master} → {slave} ({rule.relation}) "
                    "が重複しています。"
                )
            link_keys.add(key)
            axis = _LINKED_RELATION_AXIS[rule.relation]
            if axis in derived_axes.get(slave, set()):
                raise ValueError(
                    f"役割 {slave} の{axis}軸には複数の連動ルールを指定できません。"
                )
            derived_axes.setdefault(slave, set()).add(axis)
            links.append(
                LinkedPlacementRule(
                    constraint_id=_short_semantic_id(
                        "proposal-link",
                        {
                            "master": master,
                            "slave": slave,
                            "relation": rule.relation,
                            "mirror_axis_x_m": rule.mirror_axis_x_m,
                            "tolerance_m": rule.tolerance_m,
                        },
                    ),
                    master_entity_id=entity_ids[master],
                    slave_entity_id=entity_ids[slave],
                    relation=rule.relation,
                    mirror_axis_x_m=rule.mirror_axis_x_m,
                    tolerance_m=rule.tolerance_m,
                )
            )

        room = baseline.document.room
        proposals: list[ProposedEntitySpec] = []
        equipment_bindings: list[EquipmentBindingRef] = []
        placements: list[ProposedPlacementSpec] = []
        for (draft, role, zone), definition in zip(cleaned, definitions):
            entity_id = entity_ids[role]
            x_m = (draft.min_x_m + draft.max_x_m) * 0.5
            y_m = (draft.min_y_m + draft.max_y_m) * 0.5
            z_m = (draft.min_z_m + draft.max_z_m) * 0.5
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
            proposals.append(
                ProposedEntitySpec(
                    spec_id=_short_semantic_id(
                        "proposal-spec",
                        {"entity": entity_id, "baseline": baseline.content_hash},
                    ),
                    entity=SceneEntity(
                        entity_id=entity_id,
                        kind="speaker",
                        name=role,
                        speaker_role=role,
                        position=Position3(x_m=x_m, y_m=y_m, z_m=z_m),
                        size_m=definition.cabinet_envelope_m,
                        aim_xyz=aim,
                    ),
                    role_binding_id=role,
                    provenance=(
                        VariantProvenanceItem(
                            key="o100g.authoring_surface",
                            value="room-placement",
                        ),
                        VariantProvenanceItem(
                            key="o100g.install_zone",
                            value=zone,
                        ),
                    ),
                )
            )
            equipment_bindings.append(
                EquipmentBindingRef(
                    entity_id=entity_id,
                    equipment_definition_id=definition.definition_id,
                    equipment_definition_version=definition.version,
                    equipment_definition_sha256=definition.semantic_sha256,
                )
            )
            region = (
                CadConstraintPoint2D(x_m=draft.min_x_m, y_m=draft.min_y_m),
                CadConstraintPoint2D(x_m=draft.max_x_m, y_m=draft.min_y_m),
                CadConstraintPoint2D(x_m=draft.max_x_m, y_m=draft.max_y_m),
                CadConstraintPoint2D(x_m=draft.min_x_m, y_m=draft.max_y_m),
            )
            derived = derived_axes.get(role, set())
            axes = tuple(
                CadSearchAxis(
                    entity_id=entity_id,
                    axis=axis,
                    min_m=low,
                    max_m=high,
                    step_m=draft.step_m,
                )
                for axis, low, high in (
                    ("x", draft.min_x_m, draft.max_x_m),
                    ("y", draft.min_y_m, draft.max_y_m),
                    ("z", draft.min_z_m, draft.max_z_m),
                )
                if axis not in derived
            )
            placements.append(
                ProposedPlacementSpec(
                    entity_id=entity_id,
                    role_id=role,
                    zone_id=zone,
                    allowed_region=region,
                    min_z_m=draft.min_z_m,
                    max_z_m=draft.max_z_m,
                    xyz_axes=axes,
                    angle_axes=draft.angle_axes,
                )
            )

        # A removed role that is not re-added drops out of the declared channel
        # vocabulary; a removed role that is re-added is an explicit replace.
        # #505: when a LayoutProfile is the declared channel vocabulary, role
        # bindings carry its exact id/version instead of free strings.
        if layout_profile is not None:
            profile_role_set = set(layout_profile.role_ids)
        else:
            profile_role_set = None
        final_roles = sorted(
            (existing_role_set - removed_role_set) | proposed_roles
        )
        if profile_role_set is not None:
            undeclared = sorted(set(final_roles) - profile_role_set)
            if undeclared:
                raise ValueError(
                    "役割 "
                    + " / ".join(undeclared)
                    + " は選択されたLayoutProfileで宣言されていません。"
                )
        role_bindings = tuple(
            ChannelRoleBinding(
                role_id=item,
                display_name=(
                    layout_profile.role(item).display_name
                    if layout_profile is not None
                    else item
                ),
                layout_profile_id=(
                    layout_profile.profile_id
                    if layout_profile is not None
                    else None
                ),
                layout_profile_version=(
                    layout_profile.version
                    if layout_profile is not None
                    else None
                ),
            )
            for item in final_roles
        )
        template = build_system_variant(
            baseline=baseline,
            name=name,
            role_bindings=role_bindings,
            proposed_entities=tuple(proposals),
            remove_entity_ids=tuple(remove_entity_ids),
            equipment_bindings=tuple(equipment_bindings)
            + tuple(override_bindings),
            provenance=(
                VariantProvenanceItem(
                    key="o100g.install_zone",
                    value=" / ".join(zone for _d, _r, zone in cleaned),
                ),
            ),
            created_at_utc=_utc_now(),
        )
        self.variant_repository.save_variant(template)

        topology = build_topology_search_spec(
            baseline=baseline,
            template_variants=(template,),
            optional_role_ids=tuple(
                role for draft, role, _zone in cleaned if draft.optional_role
            ),
            created_at_utc=_utc_now(),
        )
        self.topology_repository.save_topology_spec(topology)
        option = topology.options[0]

        search = build_topology_placement_search_spec(
            baseline=baseline,
            template_variant=template,
            topology_spec=topology,
            topology_option_id=option.option_id,
            placement_specs=tuple(placements),
            linked_rules=tuple(links),
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
        candidate_id = provenance.get("o100b.candidate_id")
        if candidate_id is None:
            # Multi-speaker proposals record the authored zone on each
            # ProposedEntitySpec so every entity keeps its own install region.
            proposed = next(
                (
                    item
                    for item in variant.proposed_entities
                    if item.entity.entity_id == entity_id
                ),
                None,
            )
            if proposed is not None:
                entity_zone = {
                    item.key: item.value for item in proposed.provenance
                }.get("o100g.install_zone")
                if entity_zone:
                    return entity_zone
        authored_zone = provenance.get("o100g.install_zone")
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
        latest = self.scene_repository.current_head(self.document_id)
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
        latest = self.scene_repository.current_head(self.document_id)
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

    def _as_built_revision_chain(
        self,
        application: SystemVariantApplication,
        target,
    ) -> tuple | None:
        """Return the applied..target revision chain, or None when unrelated."""

        chain: list = []
        seen: set[str] = set()
        current = target
        while True:
            if current.revision_id in seen:
                return None
            seen.add(current.revision_id)
            chain.append(current)
            if current.revision_id == application.applied_revision_id:
                if current.content_hash != application.applied_content_hash:
                    return None
                return tuple(reversed(chain))
            if current.parent_revision_id is None:
                return None
            parent = self.scene_repository.get(current.parent_revision_id)
            if parent is None or parent.document_id != self.document_id:
                return None
            current = parent

    @staticmethod
    def _pose_text(entity) -> str:
        position = entity.position
        text = (
            f"({position.x_m:.3f}, {position.y_m:.3f}, {position.z_m:.3f})"
        )
        aim = getattr(entity, 'aim_xyz', None)
        if aim is not None:
            text += f" / aim ({aim.x:.3f}, {aim.y:.3f}, {aim.z:.3f})"
        return text

    def as_built_preview(self, variant_id: str) -> AsBuiltPreview:
        """Compose the pre-save As-built confirmation for one applied variant."""

        variant = self.variant(variant_id)
        application = self._application(variant_id)
        if application is None:
            return AsBuiltPreview(
                variant_id,
                False,
                'この提案はまだSceneRevisionへ適用されていません。'
                '先に「この提案を適用」を実行してください。',
                None,
                None,
                0,
                (),
                (),
            )
        if self._as_built(variant_id) is not None:
            return AsBuiltPreview(
                variant_id,
                False,
                'このapplicationには既に設置済み記録があります。',
                application.applied_revision_id,
                None,
                0,
                (),
                (),
            )
        head = self.scene_repository.current_head(self.document_id)
        if head is None:
            return AsBuiltPreview(
                variant_id,
                False,
                '現在の部屋状態がありません。',
                application.applied_revision_id,
                None,
                0,
                (),
                (),
            )
        chain = self._as_built_revision_chain(application, head)
        if chain is None:
            return AsBuiltPreview(
                variant_id,
                False,
                '現在の保存状態はこのproposalのapplied SceneRevisionの'
                '子孫ではありません。適用した構成へ戻してから記録してください。',
                application.applied_revision_id,
                head.revision_id,
                0,
                (),
                (),
            )

        actual_by_id = {
            entity.entity_id: entity for entity in head.document.entities
        }
        diffs: list[AsBuiltEntityDiff] = []
        blocking: list[str] = []
        for item in variant.proposed_entities:
            proposed = item.entity
            actual = actual_by_id.get(proposed.entity_id)
            equipment, _equipment_reason = self._equipment_label(
                variant, proposed.entity_id
            )
            if actual is None:
                blocking.append(
                    f'設置済み候補に提案物体 {proposed.name} がありません。'
                )
                continue
            if actual.kind != proposed.kind:
                blocking.append(
                    f'{proposed.name}: 提案kind {proposed.kind} と実物体kind '
                    f'{actual.kind} が一致しません。'
                )
            if actual.kind == 'speaker' and actual.speaker_role != proposed.speaker_role:
                blocking.append(
                    f'{proposed.name}: 提案role {proposed.speaker_role} と実物体 '
                    f'{actual.speaker_role} が一致しません。'
                )
            moved = actual.position != proposed.position or (
                getattr(actual, 'aim_xyz', None) != getattr(proposed, 'aim_xyz', None)
            )
            diffs.append(
                AsBuiltEntityDiff(
                    entity_id=proposed.entity_id,
                    name=actual.name or proposed.name,
                    role=actual.speaker_role or proposed.speaker_role,
                    equipment=equipment,
                    proposed_pose=self._pose_text(proposed),
                    actual_pose=self._pose_text(actual),
                    moved=moved,
                )
            )
        return AsBuiltPreview(
            variant_id,
            not blocking,
            None,
            application.applied_revision_id,
            head.revision_id,
            len(chain),
            tuple(diffs),
            tuple(blocking),
        )

    def _lifecycle_repository(self) -> CadSystemVariantLifecycleRepository:
        repository = getattr(self, '_as_built_repository', None)
        if repository is None:
            repository = CadSystemVariantLifecycleRepository(
                scene_repository=self.scene_repository,
                variant_repository=self.variant_repository,
            )
            self._as_built_repository = repository
        return repository

    def _measurement_campaign_repository(
        self,
    ) -> CadSystemVariantMeasurementCampaignRepository:
        repository = getattr(self, '_variant_campaign_repository', None)
        if repository is None:
            lifecycle_repository = self._lifecycle_repository()
            measurement_repository = CadMeasurementRepository(
                self.scene_repository
            )
            quality_repository = CadMeasurementQualityRepository(
                measurement_repository
            )
            measured_repository = CadSystemVariantMeasuredLifecycleRepository(
                scene_repository=self.scene_repository,
                lifecycle_repository=lifecycle_repository,
                measurement_repository=measurement_repository,
                quality_repository=quality_repository,
            )
            repository = CadSystemVariantMeasurementCampaignRepository(
                scene_repository=self.scene_repository,
                variant_repository=self.variant_repository,
                lifecycle_repository=lifecycle_repository,
                measurement_repository=measurement_repository,
                quality_repository=quality_repository,
                measured_lifecycle_repository=measured_repository,
            )
            self._variant_campaign_repository = repository
        return repository

    def record_as_built(
        self,
        variant_id: str,
        *,
        confirmed_by: str,
        notes: Sequence[str] = (),
    ) -> SystemVariantAsBuiltRecord:
        """Persist the explicit As-built record for one applied proposal.

        ``confirmed_by`` is the human attester label (never a DB id). The
        pre-save diff is exposed by ``as_built_preview``; this method still
        rechecks every gate so apply alone never promotes a variant.
        """

        preview = self.as_built_preview(variant_id)
        if not preview.ready:
            raise ValueError(
                preview.reason
                or ' / '.join(preview.blocking)
                or '設置済み記録の前提条件を満たしていません。'
            )
        confirmed = confirmed_by.strip()
        if not confirmed:
            raise ValueError('記録者名を入力してください。')
        variant = self.variant(variant_id)
        application = self._application(variant_id)
        assert application is not None  # guarded by preview.ready
        head = self.scene_repository.current_head(self.document_id)
        assert head is not None
        record = build_system_variant_as_built_record(
            scene_repository=self.scene_repository,
            variant_repository=self.variant_repository,
            application=application,
            variant=variant,
            as_built_revision=head,
            confirmed_by=confirmed,
            confirmed_at_utc=_utc_now(),
            notes=notes,
        )
        return self._lifecycle_repository().save(record)

    def measurement_plan_options(self, variant_id: str) -> MeasurementPlanOptions:
        """As-built-resolved point/source entities for plan authoring."""

        self.variant(variant_id)
        as_built = self._as_built(variant_id)
        if as_built is None:
            return MeasurementPlanOptions(
                False,
                '設置済み記録がないためSystemVariant測定計画は作成できません。',
                (),
                (),
            )
        revision = self.scene_repository.get(as_built.as_built_revision_id)
        if revision is None:
            return MeasurementPlanOptions(
                False,
                '設置済みSceneRevisionを再解決できません。',
                (),
                (),
            )
        points: list[MeasurementPointOption] = []
        sources: list[MeasurementSourceOption] = []
        for entity in revision.document.entities:
            if entity.kind == 'measurement_point':
                points.append(
                    MeasurementPointOption(
                        entity_id=entity.entity_id,
                        name=entity.name,
                        position_text=self._pose_text(entity),
                    )
                )
            elif entity.kind == 'speaker':
                sources.append(
                    MeasurementSourceOption(
                        entity_id=entity.entity_id,
                        name=entity.name,
                        role=entity.speaker_role,
                    )
                )
        return MeasurementPlanOptions(
            True,
            None,
            tuple(points),
            tuple(sources),
        )

    def create_measurement_plan(
        self,
        variant_id: str,
        *,
        measurement_point_entity_id: str,
        source_entity_ids: Sequence[str],
        channel_role: str,
        observable: MeasurementCapabilityClaim = 'magnitude_response',
        expected_measurement_count: int = 1,
        require_acquisition_context: bool = True,
        repeatability_required: bool = False,
        purpose: str | None = None,
    ) -> SystemVariantMeasurementPlan:
        """Create one SystemVariant-specific measurement plan.

        The target binds the exact As-built revision's point position and
        speaker sources; a generic N60 plan is a different authority and is
        never reused as a substitute.
        """

        self.variant(variant_id)
        as_built = self._as_built(variant_id)
        if as_built is None:
            raise ValueError(
                '設置済み記録がないためSystemVariant測定計画は作成できません。'
            )
        revision = self.scene_repository.get(as_built.as_built_revision_id)
        if revision is None:
            raise ValueError('設置済みSceneRevisionを再解決できません。')
        try:
            point = revision.document.entity(measurement_point_entity_id)
        except KeyError as exc:
            raise ValueError('選択した測定点は設置済み状態に存在しません。') from exc
        role = channel_role.strip()
        if not role:
            raise ValueError('測定のチャンネル役割を入力してください。')
        sources = tuple(sorted(set(source_entity_ids)))
        if not sources:
            raise ValueError('測定対象の音源を1つ以上選択してください。')
        if repeatability_required and expected_measurement_count < 2:
            raise ValueError(
                'repeatabilityを要求するtargetは2回以上の測定が必要です。'
            )
        for entity_id in sources:
            try:
                source = revision.document.entity(entity_id)
            except KeyError as exc:
                raise ValueError('選択した音源は設置済み状態に存在しません。') from exc
            if source.kind != 'speaker':
                raise ValueError('測定対象の音源はspeakerである必要があります。')
        target = SystemVariantMeasurementTarget(
            target_id=_short_semantic_id(
                'variant-measurement-target',
                {
                    'as_built': as_built.record_id,
                    'point': measurement_point_entity_id,
                    'sources': list(sources),
                    'role': role,
                    'observable': observable,
                },
            ),
            measurement_point_entity_id=measurement_point_entity_id,
            measurement_position=point.position,
            source_entity_ids=sources,
            channel_role=role,
            observable=observable,
            acquisition=VariantMeasurementAcquisitionRequirement(
                require_context=require_acquisition_context,
            ),
            expected_measurement_count=expected_measurement_count,
            repeatability_required=repeatability_required,
            validation_purpose=purpose,
        )
        plan = build_system_variant_measurement_plan(
            scene_repository=self.scene_repository,
            variant_repository=self.variant_repository,
            lifecycle_repository=self._lifecycle_repository(),
            as_built_record=as_built,
            targets=(target,),
            created_at_utc=_utc_now(),
            purpose=purpose,
        )
        return self._measurement_campaign_repository().save_plan(plan)

    def preregister_measurement_campaign(
        self,
        variant_id: str,
        *,
        purpose: str,
        plan_ids: Sequence[str] | None = None,
    ) -> tuple[
        SystemVariantMeasurementCampaign,
        SystemVariantMeasurementCampaignRegistration,
    ]:
        """Preregister one campaign over this variant's exact saved plans.

        Fixes requirements before measured evidence is seen; the repository
        commits the durable registration inside the campaign transaction.
        """

        self.variant(variant_id)
        plans = self._plans(variant_id)
        if plan_ids is not None:
            wanted = set(plan_ids)
            plans = tuple(plan for plan in plans if plan.plan_id in wanted)
        if not plans:
            raise ValueError(
                'campaignを事前登録するSystemVariant測定計画がありません。'
            )
        text = purpose.strip()
        if not text:
            raise ValueError('campaignの目的を入力してください。')
        campaign = build_system_variant_measurement_campaign(
            plans=plans,
            purpose=text,
            preregistered_at_utc=_utc_now(),
        )
        registration = self._measurement_campaign_repository().save_campaign(
            campaign
        )
        return campaign, registration

    def pending_measurement_targets(
        self,
        variant_id: str,
    ) -> tuple[PendingMeasurementTarget, ...]:
        """Preregistered targets and how much accepted evidence each still needs."""

        self.variant(variant_id)
        completions = tuple(
            completion
            for campaign in self._campaigns(variant_id)
            for completion in self._plan_completions(campaign.campaign_id)
        )
        pending: list[PendingMeasurementTarget] = []
        for plan in self._plans(variant_id):
            for target in plan.targets:
                recorded = sum(
                    1
                    for completion in completions
                    if completion.plan_ref.plan_id == plan.plan_id
                    for evidence in completion.evidence
                    if evidence.target_id == target.target_id
                )
                pending.append(
                    PendingMeasurementTarget(
                        target_id=target.target_id,
                        plan_id=plan.plan_id,
                        measurement_point_entity_id=target.measurement_point_entity_id,
                        channel_role=target.channel_role,
                        expected_measurement_count=target.expected_measurement_count,
                        recorded_evidence_count=recorded,
                    )
                )
        return tuple(pending)

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
        latest = self.scene_repository.current_head(self.document_id)
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

    # ------------------------------------------------------------------
    # O100D comparison execution (#982) + O100C cost/budget (#988)
    # ------------------------------------------------------------------

    def _comparison_execution_dependencies(self):
        """Repositories for the canonical O100D execution path."""
        standards = CadStandardsRepository(
            self.scene_repository, self.variant_repository
        )
        directivity = CadDirectivityRepository(
            self.scene_repository, self.equipment_repository
        )
        coverage = CadCoverageRepository(
            self.scene_repository,
            self.variant_repository,
            self.equipment_repository,
            directivity,
        )
        direct_level = CadDirectLevelRepository(
            self.scene_repository,
            self.variant_repository,
            self.equipment_repository,
        )
        amplifier = CadAmplifierHeadroomRepository(
            self.scene_repository,
            self.variant_repository,
            self.equipment_repository,
        )
        cost = CadInstallationCostRepository(self.scene_repository)
        bindings = CadEquipmentBindingRepository(
            self.scene_repository, self.equipment_repository
        )
        comparison = CadTopologyComparisonRepository(
            scene_repository=self.scene_repository,
            system_variant_repository=self.variant_repository,
            standards_repository=standards,
            coverage_repository=coverage,
            direct_level_repository=direct_level,
            amplifier_headroom_repository=amplifier,
            external_resolvers={
                'installation_cost_evaluation': installation_cost_resolver(
                    cost
                ),
            },
        )
        return {
            'standards': standards,
            'directivity': directivity,
            'coverage': coverage,
            'direct_level': direct_level,
            'amplifier': amplifier,
            'cost': cost,
            'bindings': bindings,
            'comparison': comparison,
        }

    def save_cost_record(self, record: CostRecord) -> CostRecord:
        """Persist one exact project cost record (#988)."""
        return CadInstallationCostRepository(
            self.scene_repository
        ).save_record(record, document_id=self.document_id)

    def cost_records(self) -> tuple[CostRecord, ...]:
        return CadInstallationCostRepository(
            self.scene_repository
        ).list_records(self.document_id)

    def cost_scenarios(self) -> tuple[CostScenario, ...]:
        """Persisted cost scenarios are not a product surface; scenarios are
        caller-built in-memory authorities (``build_cost_scenario``)."""
        return ()

    def comparison_standards_profiles(self) -> tuple[StandardsProfile, ...]:
        """Persisted StandardsProfiles available to comparison runs."""
        return CadStandardsRepository(
            self.scene_repository, self.variant_repository
        ).list_profiles()

    def evaluate_proposals(
        self,
        variant_ids: Sequence[str],
        *,
        name: str | None = None,
        include_current: bool = True,
        source_entity_id: str | None = None,
        channel_role_id: str | None = None,
        seat_entity_ids: Sequence[str] | None = None,
        standards_profile_id: str | None = None,
        cost_scenario: CostScenario | None = None,
        playback_chain: PlaybackChainLanePolicy | None = None,
        required_objective_ids: frozenset[str] = frozenset(),
    ) -> TopologyComparisonExecution:
        """Evaluate selected proposed SystemVariants and persist a comparison.

        One normal O100D run: builds the canonical spec, runs every typed
        evaluator the candidate's equipment bindings/capability support,
        persists exact VariantEvaluationBundles and the reproducible
        TopologyComparisonEvaluation. Nothing is synthesized: unsupported
        lanes surface as explicit eligibility issues, not zeros.
        """
        baseline = self.scene_repository.current_head(self.document_id)
        if baseline is None:
            raise ValueError("比較対象となる現在のSceneRevisionがありません。")

        candidates: list[SystemVariant] = []
        for variant_id in variant_ids:
            variant = self.variant_repository.get_variant(variant_id)
            if variant is None:
                raise ValueError(
                    f"選択したSystemVariantが存在しません: {variant_id}"
                )
            candidates.append(variant)
        if not candidates and not include_current:
            raise ValueError("比較には少なくとも1つの候補が必要です。")

        document = baseline.document
        if source_entity_id is None or channel_role_id is None:
            speaker = next(
                (
                    entity
                    for entity in document.entities
                    if entity.kind == 'speaker' and entity.speaker_role
                ),
                None,
            )
            if speaker is not None:
                source_entity_id = (
                    source_entity_id or speaker.entity_id
                )
                channel_role_id = (
                    channel_role_id or speaker.speaker_role
                )

        if seat_entity_ids is None:
            seat_entity_ids = tuple(
                entity.entity_id
                for entity in document.entities
                if is_listener_receiver_eligible(entity)
            )
        seats = tuple(seat_entity_ids)

        dependencies = self._comparison_execution_dependencies()
        standards = dependencies['standards']
        if standards_profile_id is not None:
            profile = standards.get_profile(standards_profile_id)
            if profile is None:
                raise ValueError(
                    f"選択したStandardsProfileが存在しません: "
                    f"{standards_profile_id}"
                )
        else:
            profiles = standards.list_profiles()
            if profiles:
                profile = profiles[-1]
            else:
                profile = build_user_standards_profile(
                    profile_id='o100d-comparison-default',
                    version='1',
                    name='O100D comparison default',
                    criteria=(),
                )

        coverage_policy = None
        if source_entity_id is not None and channel_role_id and seats:
            coverage_policy = CoverageLanePolicy(
                source_entity_id=source_entity_id,
                channel_role_id=channel_role_id,
                seat_entity_ids=seats,
                evaluation_frequencies_hz=(
                    125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0
                ),
                coverage_threshold_db=-6.0,
            )
        direct_level_policy = None
        if source_entity_id is not None and channel_role_id and seats:
            direct_level_policy = DirectLevelLanePolicy(
                source_entity_id=source_entity_id,
                channel_role_id=channel_role_id,
                seat_entity_ids=seats,
                reference_input=ReferenceInputCondition(
                    input_quantity='voltage_v_rms',
                    input_value=2.83,
                ),
                target_spl_db_spl=75.0,
                frequency_band=DirectLevelFrequencyBand(
                    low_hz=100.0, high_hz=10000.0
                ),
                target_reference_condition=(
                    'single-channel direct target at each seat'
                ),
            )

        return execute_topology_comparison(
            scene_repository=self.scene_repository,
            variant_repository=self.variant_repository,
            comparison_repository=dependencies['comparison'],
            standards_repository=standards,
            equipment_repository=self.equipment_repository,
            baseline=baseline,
            name=name or 'SystemVariant 比較',
            standards_profile=profile,
            candidates=candidates,
            policy=ComparisonEvaluationPolicy(
                coverage=coverage_policy,
                direct_level=direct_level_policy,
                playback_chain=playback_chain,
                cost_scenario=cost_scenario,
                required_objective_ids=required_objective_ids,
            ),
            include_current=include_current,
            coverage_repository=dependencies['coverage'],
            direct_level_repository=dependencies['direct_level'],
            amplifier_headroom_repository=dependencies['amplifier'],
            directivity_repository=dependencies['directivity'],
            cost_repository=dependencies['cost'],
            equipment_binding_repository=dependencies['bindings'],
            created_at_utc=datetime.now(timezone.utc).isoformat(),
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

        latest = self.scene_repository.current_head(self.document_id)
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
