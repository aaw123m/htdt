from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
)

from .cad_display_labels import revision_display_label
from .cad_repository import SceneRepository
from .cad_scene import F1_DOCUMENT_ID
from .cad_search import search_spec_current_working
from .cad_authority_dependency_repository import (
    CadAuthorityDependencyRepository,
)
from .cad_authority_resolver import AuthorityRef
from .cad_campaign_execution_repository import (
    CadCampaignExecutionRepository,
)
from .cad_commissioning_orchestrator_repository import (
    CadCommissioningOrchestratorRepository,
)
from .cad_dependency_impact import WatchedArtifact
from .cad_evidence_invalidation import (
    QueueItemUnavailableError,
    RevalidationQueueItem,
    compose_revalidation_queue,
)
from .cad_evidence_invalidation_repository import (
    CadEvidenceInvalidationRepository,
)
from .cad_prediction_repository import CadPredictionRepository
from .comparison_context_strip import ComparisonContextStrip
from .decision_brief_panel import DecisionBriefPanel
from .revalidation_queue_panel import RevalidationQueuePanel
from .developer_mode import developer_mode_enabled
from .error_boundary import (
    EXPECTED_OPERATION_ERRORS,
    is_authority_failure,
    report_boundary_failure,
)
from .intervention_planner import InterventionPlanner
from .intervention_planner_panel import InterventionPlannerPanel
from .joint_optimization_context import JointOptimizationContext
from .joint_optimization_panel import JointOptimizationPanel
from .optimization_journey import (
    OptimizationJourneyStep,
    current_journey_step,
    evaluate_optimization_journey,
)
from .optimization_search_domain import SearchDomainPreview
from .optimization_workflow_controller import OptimizationWorkflowController
from .robustness_authoring_context import RobustnessAuthoringContext
from .robustness_authoring_panel import RobustnessAuthoringPanel
from .room_viewport import RoomOverlayState, RoomViewport3D
from .ui_theme import (
    ControlSize,
    SemanticState,
    SurfaceRole,
    TypographyRole,
    set_control_size,
    set_primary_action,
    set_semantic_state,
    set_surface_role,
    set_typography_role,
)
from .workflow_navigation import WorkspaceDeepLink, WorkspaceId
from .workflow_shell import WorkspaceMount
from .workspace_dirty_state import DirtyResolutionAction, WorkspaceDirtyState
from .system_expansion_workflow import SystemExpansionWorkflowService

if TYPE_CHECKING:
    from .measurement_workflow import RewReadSource
from .multi_sub_optimization_panel import MultiSubBaselinePanel
from .standards_workspace import StandardsVariantComparisonPanel
from .system_expansion_widgets import (
    SystemExpansionMeasurementPanel,
    SystemExpansionOptimizePanel,
    SystemExpansionRobustnessPanel,
)


OPTIMIZATION_PAGE_IDS = (
    "setup",
    "candidates",
    "comparison",
    "interventions",
    "robustness",
    "validation",
)
_OPTIMIZATION_PAGE_ALIASES = {
    "objectives": "comparison",
    "measurement-plan": "validation",
    "topology-comparison": "comparison",
    "variant-robustness": "robustness",
    "variant-measurement": "validation",
    "intervention-planner": "interventions",
}


def normalize_optimization_page(value: str) -> str:
    normalized = _OPTIMIZATION_PAGE_ALIASES.get(value, value)
    if normalized not in OPTIMIZATION_PAGE_IDS:
        raise ValueError(f"unknown optimization page: {value!r}")
    return normalized


TWidget = TypeVar("TWidget", bound=QWidget)


def _required(widget: TWidget | None, name: str) -> TWidget:
    if widget is None:
        raise RuntimeError(f"optimization controller did not create {name}")
    return widget


def _heading(title: str, description: str | None = None) -> QWidget:
    frame = QFrame()
    set_surface_role(frame, SurfaceRole.BASE)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)

    label = QLabel(title)
    set_typography_role(label, TypographyRole.WORKSPACE_TITLE)
    layout.addWidget(label)
    if description:
        detail = QLabel(description)
        detail.setWordWrap(True)
        set_typography_role(detail, TypographyRole.SECONDARY)
        layout.addWidget(detail)
    return frame


def _card(title: str, description: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    set_surface_role(frame, SurfaceRole.RAISED)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 14, 16, 16)
    layout.setSpacing(10)

    label = QLabel(title)
    set_typography_role(label, TypographyRole.SECTION_TITLE)
    layout.addWidget(label)
    if description:
        detail = QLabel(description)
        detail.setWordWrap(True)
        set_typography_role(detail, TypographyRole.SECONDARY)
        layout.addWidget(detail)
    return frame, layout


def _scroll_page(body: QWidget) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidget(body)
    return scroll


def _button(label: str, callback, *, primary: bool = False, tip: str | None = None) -> QPushButton:
    button = QPushButton(label)
    set_control_size(button, ControlSize.STANDARD)
    if primary:
        set_primary_action(button)
    if tip is not None:
        button.setToolTip(tip)
        button.setWhatsThis(tip)
    button.clicked.connect(callback)
    return button


def _advanced_block(
    label: str,
    description: str,
    content: QWidget,
    *,
    expanded: bool = False,
) -> QWidget:
    frame = QFrame()
    set_surface_role(frame, SurfaceRole.RAISED)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 12, 16, 14)
    layout.setSpacing(8)

    toggle = QPushButton(label)
    toggle.setCheckable(True)
    toggle.setChecked(expanded)
    toggle.setToolTip(f"{description}（クリックで設定項目を展開・折りたたみ）")
    toggle.setWhatsThis(f"{description}（クリックで設定項目を展開・折りたたみ）")
    set_control_size(toggle, ControlSize.COMPACT)
    layout.addWidget(toggle)

    detail = QLabel(description)
    detail.setWordWrap(True)
    set_typography_role(detail, TypographyRole.SECONDARY)
    layout.addWidget(detail)

    content.setVisible(expanded)
    toggle.toggled.connect(content.setVisible)
    layout.addWidget(content)
    return frame


class _OptimizationViewportAdapter:
    """PyVista-like overlay port over the shared dark Room viewport."""

    def __init__(self, widget: RoomViewport3D) -> None:
        self.widget = widget

    def add_mesh(self, *args, **kwargs):
        return self.widget.plotter.add_mesh(*args, **kwargs)

    def remove_actor(self, *args, **kwargs):
        return self.widget.plotter.remove_actor(*args, **kwargs)

    def add_text(self, *args, **kwargs):
        return self.widget.plotter.add_text(*args, **kwargs)

    def render(self) -> None:
        self.widget.plotter.render()


class OptimizationWorkflowWorkspace(QWidget):
    """UX140 page-first QWidget over QMainWindow-free O10-O80 controller state."""

    def __init__(
        self,
        repository: SceneRepository,
        document_id: str = F1_DOCUMENT_ID,
        *,
        viewport_factory: Callable[[QWidget | None], QWidget] | None = None,
        on_navigate: Callable[[WorkspaceDeepLink], bool] | None = None,
        rew_client: RewReadSource | None = None,
    ) -> None:
        super().__init__()
        self.setObjectName("optimizationWorkflowWorkspace")
        set_surface_role(self, SurfaceRole.BASE)
        self.controller = OptimizationWorkflowController(
            repository, document_id, rew_client=rew_client
        )
        self.controller.statusChanged.connect(self._set_status)
        # Every persisted mutation path (spec save/re-author, candidate apply,
        # plan record, campaign/evaluation/validation writes) ends with a
        # controller statusBar message — statusChanged is the universal
        # post-mutation pulse, so the numbered strip re-evaluates on it. The
        # queries it runs are bounded and cheap.
        self.controller.statusChanged.connect(
            lambda _message: self._refresh_journey()
        )
        self.system_expansion = SystemExpansionWorkflowService(repository, document_id)
        self.system_expansion.apply_guard = self._system_expansion_apply_block_reason
        self._on_navigate = on_navigate
        self._joint_context = JointOptimizationContext(
            repository,
            document_id,
            objective_repository=self.controller.objective_repository,
        )
        self._system_variant_robustness_variant_id: str | None = None
        self._journey_steps: tuple[OptimizationJourneyStep, ...] = ()
        self._journey_buttons: dict[str, QPushButton] = {}

        self._optimization_stack = QStackedWidget()
        self._optimization_pages: dict[str, QWidget] = {}
        viewport_widget = (
            RoomViewport3D(self)
            if viewport_factory is None
            else viewport_factory(self)
        )
        self.viewport_widget = viewport_widget
        self.viewport_adapter = _OptimizationViewportAdapter(viewport_widget)
        robustness_viewport_widget = (
            RoomViewport3D(self)
            if viewport_factory is None
            else viewport_factory(self)
        )
        self.robustness_viewport_widget = robustness_viewport_widget
        self.robustness_viewport_adapter = _OptimizationViewportAdapter(
            robustness_viewport_widget
        )
        # #530: setup-page search-domain preview viewport (editor-state overlay
        # only — axes keep materializing through the same numeric fields).
        domain_viewport_widget = (
            RoomViewport3D(self)
            if viewport_factory is None
            else viewport_factory(self)
        )
        self.domain_viewport_widget = domain_viewport_widget
        self.domain_viewport_widget.setMinimumHeight(280)
        self.search_domain_preview = SearchDomainPreview(
            self.controller,
            domain_viewport_widget,
        )

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(20, 16, 20, 16)
        root_layout.setSpacing(12)
        # #584: persistent comparison context — baseline/selected/scene/status
        # stays visible across every Optimize subpage.
        self.comparison_context_strip = ComparisonContextStrip(self)
        root_layout.addWidget(self.comparison_context_strip)

        # Numbered journey strip (REV33): the six page tabs are flat siblings
        # that never express the canonical first-run order, so a state-driven
        # step list stays pinned above the page stack — each number opens the
        # page that owns the step.
        self.journey_card = QFrame(self)
        self.journey_card.setObjectName("optimizationJourneyCard")
        set_surface_role(self.journey_card, SurfaceRole.RAISED)
        journey_layout = QVBoxLayout(self.journey_card)
        journey_layout.setContentsMargins(16, 10, 16, 10)
        journey_layout.setSpacing(6)
        journey_head = QHBoxLayout()
        journey_head.setSpacing(8)
        journey_title = QLabel("最適化の手順", self.journey_card)
        set_typography_role(journey_title, TypographyRole.SECTION_TITLE)
        journey_head.addWidget(journey_title)
        journey_head.addStretch(1)
        self.journey_progress = QLabel(self.journey_card)
        self.journey_progress.setObjectName("optimizationJourneyProgress")
        set_typography_role(self.journey_progress, TypographyRole.SECONDARY)
        journey_head.addWidget(self.journey_progress)
        journey_layout.addLayout(journey_head)
        self.journey_steps_row = QHBoxLayout()
        self.journey_steps_row.setSpacing(6)
        journey_layout.addLayout(self.journey_steps_row)
        journey_hint_row = QHBoxLayout()
        journey_hint_row.setSpacing(8)
        self.journey_hint = QLabel(self.journey_card)
        self.journey_hint.setObjectName("optimizationJourneyHint")
        self.journey_hint.setWordWrap(True)
        set_typography_role(self.journey_hint, TypographyRole.SECONDARY)
        journey_hint_row.addWidget(self.journey_hint, 1)
        self.journey_open = QPushButton("現在の手順を開く", self.journey_card)
        self.journey_open.setObjectName("optimizationJourneyOpen")
        self.journey_open.clicked.connect(self._open_current_journey_step)
        journey_hint_row.addWidget(self.journey_open)
        journey_layout.addLayout(journey_hint_row)
        root_layout.addWidget(self.journey_card)

        root_layout.addWidget(self._optimization_stack, 1)

        pages = {
            "setup": self._build_setup_page(),
            "candidates": self._build_candidates_page(self.viewport_widget),
            "comparison": self._build_comparison_page(),
            "interventions": self._build_interventions_page(),
            "robustness": self._build_robustness_page(
                self.robustness_viewport_widget
            ),
            "validation": self._build_validation_page(),
        }
        for page_id in OPTIMIZATION_PAGE_IDS:
            page = pages[page_id]
            page.setObjectName(f"optimizationPage:{page_id}")
            self._optimization_pages[page_id] = page
            self._optimization_stack.addWidget(page)

        self.status = QLabel()
        self.status.setContentsMargins(12, 6, 12, 6)
        set_surface_role(self.status, SurfaceRole.RAISED)
        set_typography_role(self.status, TypographyRole.SECONDARY)
        root_layout.addWidget(self.status)

        self.controller.bind_viewport(self.viewport_adapter, self._render_scene)
        self.controller.bind_robustness_viewport(
            self.robustness_viewport_adapter,
            self._render_robustness_scene,
        )
        # #530: numeric form + tree edits update the spatial overlay exactly.
        preview = self.search_domain_preview
        self.search_entity_combo.currentIndexChanged.connect(
            lambda _index: preview.refresh()
        )
        self.search_axis_combo.currentIndexChanged.connect(
            lambda _index: preview.refresh()
        )
        self.search_min_field.valueChanged.connect(lambda _v: preview.refresh())
        self.search_max_field.valueChanged.connect(lambda _v: preview.refresh())
        self.search_step_field.valueChanged.connect(lambda _v: preview.refresh())
        self.search_axis_tree.itemSelectionChanged.connect(preview.refresh)
        axis_model = self.search_axis_tree.model()
        axis_model.rowsInserted.connect(lambda *_args: preview.refresh())
        axis_model.rowsRemoved.connect(lambda *_args: preview.refresh())
        # Presets update existing rows in place (#1090) — only itemChanged
        # fires there, not rowsInserted/rowsRemoved.
        axis_model.dataChanged.connect(lambda *_args: preview.refresh())

        for tree_name in (
            "search_candidate_tree",
            "extended_candidate_tree",
            "pareto_tree",
        ):
            tree = getattr(self.controller, tree_name, None)
            if tree is not None:
                tree.itemSelectionChanged.connect(
                    self._refresh_context_strip
                )
        self.controller.statusChanged.connect(
            lambda _text: self._refresh_context_strip()
        )

        self.select_section("setup")
        self._refresh_context_strip()
        self._set_status("保存済み")

    def __getattr__(self, name: str):
        controller = self.__dict__.get("controller")
        if controller is not None and hasattr(controller, name):
            return getattr(controller, name)
        raise AttributeError(name)

    @property
    def page_ids(self) -> tuple[str, ...]:
        return OPTIMIZATION_PAGE_IDS

    @property
    def current_page_id(self) -> str:
        current = self._optimization_stack.currentWidget()
        for page_id, page in self._optimization_pages.items():
            if page is current:
                return page_id
        raise RuntimeError("optimization page stack has no active page")

    def activate(self) -> None:
        self.controller.activate()
        self._refresh_journey()

    def before_deactivate(self) -> tuple[bool, str | None]:
        if getattr(self, 'joint_optimization_panel', None) is not None and (
            self.joint_optimization_panel.is_running()
        ):
            return False, (
                "ジョイント最適化が完了またはキャンセルされるまで"
                "画面を切り替えられません"
            )
        if getattr(self, 'robustness_authoring_panel', None) is not None and (
            self.robustness_authoring_panel.is_running()
        ):
            return False, (
                "ばらつき評価が完了またはキャンセルされるまで"
                "画面を切り替えられません"
            )
        if getattr(self, 'system_expansion_compare_panel', None) is not None and (
            self.system_expansion_compare_panel.is_running()
        ):
            return False, (
                "提案比較の評価が完了またはキャンセルされるまで"
                "画面を切り替えられません"
            )
        return self.controller.before_deactivate()

    def dirty_state(self) -> WorkspaceDirtyState:
        if getattr(self, 'joint_optimization_panel', None) is not None and (
            self.joint_optimization_panel.is_running()
        ):
            return 'busy'
        if getattr(self, 'robustness_authoring_panel', None) is not None and (
            self.robustness_authoring_panel.is_running()
        ):
            return 'busy'
        if getattr(self, 'system_expansion_compare_panel', None) is not None and (
            self.system_expansion_compare_panel.is_running()
        ):
            return 'busy'
        return self.controller.dirty_state()

    def resolve_dirty_state(
        self, action: DirtyResolutionAction
    ) -> tuple[bool, str | None]:
        if action == 'stop_busy':
            # D1/#REV19: the operator abandoned in-flight work — drain each
            # mounted panel's pool AND the controller's pools; detached
            # workers' completions are disconnected inside stop_all so
            # late results can never apply or be persisted.
            lingering = False
            for panel in (
                getattr(self, 'joint_optimization_panel', None),
                getattr(self, 'robustness_authoring_panel', None),
                getattr(self, 'system_expansion_compare_panel', None),
            ):
                if panel is not None and panel.is_running():
                    lingering = not panel.stop().all_stopped or lingering
            resolved, message = self.controller.resolve_dirty_state(action)
            if not resolved:
                return resolved, message
            if lingering:
                return (
                    True,
                    '実行中の処理を中止しました · 停止が遅延している処理の結果は適用されません',
                )
            return True, '実行中の処理を中止しました'
        return self.controller.resolve_dirty_state(action)

    def select_section(self, section_id: str) -> None:
        page_id = normalize_optimization_page(section_id)
        self._optimization_stack.setCurrentWidget(self._optimization_pages[page_id])
        if page_id == "setup":
            self.search_domain_preview.refresh()
            # The joint panel gates its create/run actions on the latest
            # robustness spec — refresh whenever the setup page shows so a
            # spec authored this session is picked up without an app restart.
            if getattr(self, 'joint_optimization_panel', None) is not None:
                self.joint_optimization_panel.refresh()
        if page_id == "robustness":
            self.controller.refresh_robustness_view()
            if getattr(self, 'robustness_authoring_panel', None) is not None:
                self.robustness_authoring_panel.refresh()
        if page_id == "comparison" and hasattr(self, "system_expansion_compare_panel"):
            self.system_expansion_compare_panel.refresh()
            self.standards_comparison_panel.refresh()
            if hasattr(self, "multi_sub_baseline_panel"):
                self.multi_sub_baseline_panel._load()
        if page_id == "interventions" and hasattr(self, "intervention_planner_panel"):
            self.intervention_planner_panel.refresh()
        if page_id == "validation":
            if hasattr(self, "system_expansion_measurement_panel"):
                self.system_expansion_measurement_panel.refresh()
            self.controller.refresh_measurement_plans()
            self.controller.refresh_validation_campaigns()
            self.controller.refresh_model_validations()
            self.controller.refresh_adaptive_plans()
            self.controller.refresh_adaptive_extended_plans()
            self.controller._refresh_campaign_measurement_points()
        # The page switch lands the user on a new page — re-evaluate so the
        # strip's progress and next-step hint always match what they see.
        self._refresh_journey()

    def _open_variant_measurements(self) -> None:
        if self._on_navigate is None or not self._on_navigate(
            WorkspaceDeepLink(
                workspace=WorkspaceId.MEASUREMENT,
                section="campaign",
            )
        ):
            self._set_status(
                "測定ワークスペースへ移動できませんでした。"
            )

    # REV72: #964 change-diff evidence invalidation wiring -------------

    def _revalidation_repository(self) -> CadEvidenceInvalidationRepository:
        repository = getattr(self, "_rev_repo", None)
        if repository is None:
            repository = CadEvidenceInvalidationRepository(
                self.controller.repository
            )
            self._rev_repo = repository
        return repository

    def _dependency_repository(self) -> CadAuthorityDependencyRepository:
        repository = getattr(self, "_dep_repo", None)
        if repository is None:
            repository = CadAuthorityDependencyRepository(
                self.controller.repository
            )
            self._dep_repo = repository
        return repository

    def _revalidation_watched_artifacts(
        self, document_id: str
    ) -> tuple[WatchedArtifact, ...]:
        """The same watched set the Overview impact notices report on."""
        artifacts: list[WatchedArtifact] = []
        for result in CadPredictionRepository(
            self.controller.repository
        ).list_results(document_id):
            if result.status != 'completed':
                continue
            artifacts.append(
                WatchedArtifact(
                    artifact_kind='prediction',
                    artifact_id=result.prediction_id,
                    bound_revision_id=result.scene_revision_id,
                    bound_content_hash=result.scene_content_hash,
                    watched_axes=frozenset(
                        {
                            'geometry',
                            'source_equipment',
                            'material_boundary',
                            'operating_state',
                            'solver_provider',
                        }
                    ),
                )
            )
        for spec in self.controller.search_repository.list_specs(
            document_id
        ):
            artifacts.append(
                WatchedArtifact(
                    artifact_kind='optimization_run',
                    artifact_id=spec.search_spec_id,
                    bound_revision_id=spec.scene_revision_id,
                    bound_content_hash=spec.scene_content_hash,
                    watched_axes=frozenset(
                        {
                            'geometry',
                            'source_equipment',
                            'material_boundary',
                            'target_design',
                        }
                    ),
                )
            )
        for measurement in (
            self.controller.measurement_repository.list_measurements(
                document_id
            )
        ):
            artifacts.append(
                WatchedArtifact(
                    artifact_kind='measured_dataset',
                    artifact_id=measurement.measurement_id,
                    bound_revision_id=measurement.scene_revision_id,
                    bound_content_hash=measurement.scene_content_hash,
                    watched_axes=frozenset({'measurement_context'}),
                )
            )
        return tuple(artifacts)

    def _revalidation_prepared_refs(
        self, document_id: str
    ) -> dict[tuple[str, str], tuple[AuthorityRef, ...]]:
        """Prepared route args: remeasure items pin the newest #956
        campaign position plan; re-commission items pin the newest #946
        orchestration run."""
        prepared: dict[tuple[str, str], tuple[AuthorityRef, ...]] = {}
        campaign_plans = CadCampaignExecutionRepository(
            self.controller.repository
        ).list_plans(document_id)
        if campaign_plans:
            plan = campaign_plans[-1]
            ref = AuthorityRef(
                kind='campaign_execution_plan',
                ref_id=plan.plan_id,
                ref_sha256=plan.plan_sha256,
            )
            for kind in ('measured_dataset', 'measurement_plan'):
                prepared[(kind, '*')] = (ref,)
        commissioning_runs = CadCommissioningOrchestratorRepository(
            self.controller.repository
        ).list_runs(document_id)
        if commissioning_runs:
            run = commissioning_runs[-1]
            ref = AuthorityRef(
                kind='commissioning_orchestration_run',
                ref_id=run.run_id,
                ref_sha256=run.run_sha256,
            )
            for kind in ('calibration_plan', 'commissioning_plan'):
                prepared[(kind, '*')] = (ref,)
        return prepared

    def _compose_revalidation_queue(self):
        """queue_supplier for the panel: compose + persist the queue for
        head-vs-parent. Returns the bundle or None when no parent
        revision exists."""
        repository = self.controller.repository
        document_id = self.system_expansion.document_id
        head = repository.current_head(document_id)
        if head is None or head.parent_revision_id is None:
            return None
        parent = repository.get(head.parent_revision_id)
        if parent is None:
            return None
        dependency_repository = self._dependency_repository()
        profiles = dependency_repository.list_rule_profiles(document_id)
        return compose_revalidation_queue(
            document_id=document_id,
            from_revision=parent,
            to_revision=head,
            watched_artifacts=self._revalidation_watched_artifacts(
                document_id
            ),
            edges=dependency_repository.list_edges(document_id),
            rule_profile=profiles[-1] if profiles else None,
            prepared_refs=self._revalidation_prepared_refs(document_id),
            dependency_repository=dependency_repository,
            revalidation_repository=self._revalidation_repository(),
        )

    def _revalidation_software_runner(
        self, item: RevalidationQueueItem
    ) -> AuthorityRef | None:
        """Honest software executor for the verify operation.

        The comparison page can genuinely re-derive the Decision Brief
        for comparison/optimization evidence; solver predictions and
        re-imports stay operator-run on their own surfaces (the item's
        route names where), so those honestly report unavailable.
        """
        if item.route == 'evidence_re_evaluate' and item.artifact_kind in (
            'design_comparison',
            'optimization_run',
            'decision_brief',
        ):
            brief = self.decision_brief_panel.recompute_brief()
            if brief is None:
                raise QueueItemUnavailableError(
                    '比較セットがないためブリーフを再計算できません'
                )
            return AuthorityRef(
                kind='decision_brief',
                ref_id=brief.brief_id,
                ref_sha256=brief.brief_sha256,
            )
        raise QueueItemUnavailableError(
            'この項目は各画面からの実行が必要です'
        )

    def _revalidation_navigate(self, route: str) -> None:
        destinations = {
            'prediction_recompute': WorkspaceDeepLink(
                WorkspaceId.ROOM, 'prediction'
            ),
            'source_reimport': WorkspaceDeepLink(
                WorkspaceId.ROOM, 'sources'
            ),
            'measurement_position_plan': WorkspaceDeepLink(
                WorkspaceId.MEASUREMENT, 'campaign'
            ),
            'commissioning_authorization': WorkspaceDeepLink(
                WorkspaceId.MEASUREMENT, 'commissioning'
            ),
            # re-evaluate/review work lives on this comparison page
            'evidence_re_evaluate': WorkspaceDeepLink(
                WorkspaceId.OPTIMIZATION, 'comparison'
            ),
            'evidence_review': WorkspaceDeepLink(
                WorkspaceId.OPTIMIZATION, 'comparison'
            ),
        }
        link = destinations.get(route)
        if link is not None and self._on_navigate is not None:
            if not self._on_navigate(link):
                self._set_status('その画面へ移動できませんでした。')
        else:
            self._set_status('この項目の作業先はまだ接続されていません。')

    def _show_system_variant_robustness(self, variant_id: str) -> None:
        self._system_variant_robustness_variant_id = variant_id
        self.select_section("robustness")
        if hasattr(self, "system_expansion_robustness_panel"):
            self.system_expansion_robustness_panel.select_variant(variant_id)

    def _system_expansion_apply_block_reason(self) -> str | None:
        """Veto for variant apply: a new head must never orphan a draft."""

        scene = self.controller.scene
        if scene.recovery_candidate is not None:
            return "復旧可能な下書きを処理してから提案を適用してください"
        if scene.working.has_preview:
            return "プレビュー中は提案を適用できません"
        if scene.is_dirty:
            return "未保存の変更を保存または破棄してから提案を適用してください"
        return None

    def _system_variant_applied(self, _revision_id: str) -> None:
        self.controller.activate()
        self.system_expansion_compare_panel.refresh()
        self.system_expansion_measurement_panel.refresh()
        self._set_status(
            "システムバリアントを新しいシーンリビジョンへ適用しました。設置済み/実測済みへの自動昇格は行いません。"
        )

    def refresh_from_authorities(self) -> None:
        self.controller.refresh_from_authorities()
        if getattr(self, 'joint_optimization_panel', None) is not None:
            self.joint_optimization_panel.refresh()
        if hasattr(self, "system_expansion_compare_panel"):
            self.system_expansion_compare_panel.refresh()
        if hasattr(self, "multi_sub_baseline_panel"):
            self.multi_sub_baseline_panel._load()
        if hasattr(self, "system_expansion_measurement_panel"):
            self.system_expansion_measurement_panel.refresh()
        if (
            hasattr(self, "system_expansion_robustness_panel")
            and self._system_variant_robustness_variant_id is not None
        ):
            self.system_expansion_robustness_panel.refresh()
        if hasattr(self, "intervention_planner_panel"):
            self.intervention_planner_panel.refresh()
        self._refresh_journey()

    # ------------------------------------------------------------------
    # REV33 — numbered journey guidance

    def _refresh_journey(self) -> None:
        """Re-evaluate the numbered journey strip from persisted state.

        Every repository call degrades to an empty signal: a guide that
        silently drops to step 1 is annoying, but a guide that kills the
        whole workspace refresh is a regression.
        """
        if not hasattr(self, "journey_steps_row"):
            # statusChanged can fire while __init__ is still building pages —
            # before the strip exists there is nothing to refresh.
            return
        controller = self.controller
        try:
            head = controller.repository.latest(controller.document_id)
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: degraded head read — expected failures report and degrade to step 1; sealed-store failures must not masquerade as 'no scene'
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='最適化の手順の部屋状態確認')
            head = None
        scene_saved = head is not None
        specs: tuple = ()
        if scene_saved:
            try:
                specs = controller.search_repository.list_specs(
                    controller.document_id
                )
            except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: degraded spec listing — expected failures report and degrade to no-spec; sealed-store failures propagate
                if is_authority_failure(exc):
                    raise
                report_boundary_failure(exc, operation='探索設定の読み込み')
                specs = ()
        # Per-spec queries are bounded — an extreme spec count must not turn
        # a guide refresh into a scan.
        inspected = specs[:16]
        spec_current_count = 0
        working = getattr(controller, 'working', None)
        constraint_set = getattr(controller, 'constraint_set', None)
        if working is not None and constraint_set is not None:
            for spec in inspected:
                try:
                    if search_spec_current_working(
                        spec,
                        working,
                        constraint_set,
                        current_document_id=controller.document_id,
                    ):
                        spec_current_count += 1
                except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: per-spec freshness probe — expected failures report and count as not-current; sealed-store failures propagate
                    if is_authority_failure(exc):
                        raise
                    report_boundary_failure(exc, operation='探索設定の最新性確認')

        # An applied candidate persists as either a measurement plan (a plan
        # can only be built over an applied revision) or a head revision that
        # descends from a spec's source revision.
        applied = False
        if head is not None and specs:
            source_ids = {
                spec.scene_revision_id for spec in inspected
            }
            seen: set[str] = set()
            cursor = head
            for _hop in range(32):
                parent_id = cursor.parent_revision_id
                if parent_id is None or parent_id in seen:
                    break
                seen.add(parent_id)
                if parent_id in source_ids:
                    applied = True
                    break
                try:
                    cursor = controller.repository.get(parent_id)
                except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: lineage walk — expected failures report and stop the walk; sealed-store failures propagate
                    if is_authority_failure(exc):
                        raise
                    report_boundary_failure(exc, operation='適用履歴の確認')
                    break
                if cursor is None:
                    break

        plan_count = 0
        plans_measured = 0
        campaign_count = 0
        evaluation_count = 0
        pareto_set_count = 0
        validation_count = 0
        for spec in inspected:
            spec_id = spec.search_spec_id
            try:
                plans = controller.measurement_repository.latest_measurement_plans(
                    spec_id
                )
                plan_count += len(plans)
                plans_measured += sum(
                    1 for plan in plans if plan.status == 'measured'
                )
            except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: per-spec plan count — expected failures report and count zero; sealed-store failures propagate
                if is_authority_failure(exc):
                    raise
                report_boundary_failure(exc, operation='実測計画の状態確認')
            try:
                campaign_count += (
                    controller.campaign_repository.count_for_search_spec(
                        spec_id
                    )
                )
            except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: per-spec campaign count — expected failures report and count zero; sealed-store failures propagate
                if is_authority_failure(exc):
                    raise
                report_boundary_failure(exc, operation='検証条件の状態確認')
            try:
                evaluation_count += (
                    controller.objective_repository.count_evaluations(spec_id)
                )
            except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: per-spec evaluation count — expected failures report and count zero; sealed-store failures propagate
                if is_authority_failure(exc):
                    raise
                report_boundary_failure(exc, operation='比較指標の状態確認')
            try:
                pareto_set_count += (
                    controller.objective_repository.count_pareto_sets(spec_id)
                )
            except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: per-spec pareto count — expected failures report and count zero; sealed-store failures propagate
                if is_authority_failure(exc):
                    raise
                report_boundary_failure(exc, operation='比較集合の状態確認')
            try:
                # The browse path skips per-record evidence re-attestation —
                # a stale record must not blank the whole guide.
                validation_count += (
                    controller.validation_repository.count_for_search_spec(
                        spec_id
                    )
                )
            except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: per-spec validation count — expected failures report and count zero; sealed-store failures propagate
                if is_authority_failure(exc):
                    raise
                report_boundary_failure(exc, operation='検証結果の状態確認')
        applied = applied or plan_count > 0

        steps = evaluate_optimization_journey(
            scene_saved=scene_saved,
            spec_count=len(specs),
            spec_current_count=spec_current_count,
            applied=applied,
            plan_count=plan_count,
            plans_measured=plans_measured,
            campaign_count=campaign_count,
            evaluation_count=evaluation_count,
            pareto_set_count=pareto_set_count,
            validation_count=validation_count,
        )
        self._journey_steps = steps

        while self.journey_steps_row.count():
            item = self.journey_steps_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._journey_buttons.clear()
        done_count = sum(1 for step in steps if step.status == "done")
        self.journey_progress.setText(f"{done_count}/6")
        for step in steps:
            label = f"{step.number} {step.title}"
            if step.status == "done":
                label += " ✓"
            button = QPushButton(label, self.journey_card)
            button.setObjectName(f"optimizationJourneyStep_{step.key}")
            button.setFlat(True)
            button.setToolTip(step.detail)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            if step.status == "current":
                set_primary_action(button)
            elif step.status == "done":
                set_semantic_state(button, SemanticState.SUCCESS)
            button.clicked.connect(
                lambda checked=False, key=step.key: self._open_journey_step(key)
            )
            self.journey_steps_row.addWidget(button)
            self._journey_buttons[step.key] = button
        self.journey_steps_row.addStretch(1)

        current = current_journey_step(steps)
        if current is None:
            self.journey_hint.setText(
                "すべての手順が完了しています。「比較」「ばらつき耐性」"
                "「介入プランナー」でさらに候補を確かめられます。"
            )
            self.journey_open.setVisible(False)
        else:
            self.journey_hint.setText(
                f"次にやること — {current.number} {current.title}: {current.detail}"
            )
            self.journey_open.setVisible(True)

    def _open_journey_step(self, key: str) -> None:
        step = next(
            (step for step in self._journey_steps if step.key == key), None
        )
        if step is None:
            return
        # Deep links keep the shell's context bar and router in sync with
        # the page switch; bare select_section is the standalone fallback.
        if step.context_id is not None:
            if self._on_navigate is not None:
                self._on_navigate(
                    WorkspaceDeepLink(
                        WorkspaceId.OPTIMIZATION, step.context_id
                    )
                )
            else:
                self.select_section(step.context_id)
            return
        if step.workspace == "room" and self._on_navigate is not None:
            self._on_navigate(
                WorkspaceDeepLink(WorkspaceId.ROOM, "geometry")
            )

    def _open_current_journey_step(self) -> None:
        step = current_journey_step(self._journey_steps)
        if step is not None:
            self._open_journey_step(step.key)

    def _render_scene(self, reset_camera: bool = False) -> None:
        self.viewport_widget.render_document(
            self.controller.working.document,
            selected_id=self.controller.selected_id,
            overlays=RoomOverlayState(grid=True, labels=False, acoustics=False),
            reset_camera=reset_camera,
        )
        self.search_domain_preview.refresh()

    def _render_robustness_scene(self, document) -> None:
        self.robustness_viewport_widget.render_document(
            document,
            selected_id=None,
            overlays=RoomOverlayState(
                grid=True,
                labels=False,
                acoustics=True,
            ),
            reset_camera=False,
        )

    def _set_status(self, text: str) -> None:
        self.status.setText(str(text))

    def _refresh_context_strip(self, *_args) -> None:
        working = self.controller.working
        baseline_label = (
            '現在の部屋'
            if working is not None and working.source_revision_id is not None
            else '保存済みの部屋状態がありません'
        )
        selected_label: str | None = None
        controller = self.controller
        page = getattr(controller, 'search_candidate_page', None)
        selected_id = controller.search_selected_candidate_id
        if page is not None and selected_id is not None:
            for index, candidate in enumerate(page.candidates, start=1):
                if candidate.candidate_id == selected_id:
                    selected_label = f'候補 {page.offset + index}'
                    break
        if selected_label is None:
            extended_page = getattr(
                controller, 'extended_candidate_page', None
            )
            extended_id = controller.extended_selected_candidate_id
            if extended_page is not None and extended_id is not None:
                for index, candidate in enumerate(
                    extended_page.candidates, start=1
                ):
                    if candidate.candidate_id == extended_id:
                        selected_label = (
                            f'拡張候補 {extended_page.offset + index}'
                        )
                        break
        scene_label = None
        if working is not None and working.source_revision_id is not None:
            revision = self.controller.repository.get(
                working.source_revision_id
            )
            if revision is not None:
                revision_labels = self.controller.repository.revision_labels(
                    self.controller.document_id
                )
                scene_label = (
                    'リビジョン '
                    + revision_display_label(revision, revision_labels)
                )
            else:
                scene_label = 'リビジョン（解除済み）'
        if working is not None and working.has_preview:
            status_label = 'プレビュー'
        elif selected_label is not None:
            status_label = '選択済み'
        else:
            status_label = '保存済み'
        self.comparison_context_strip.set_context(
            baseline_label=baseline_label,
            selected_label=selected_label,
            scene_label=scene_label,
            status_label=status_label,
        )

    def closeEvent(self, event) -> None:  # noqa: N802
        if getattr(self, 'joint_optimization_panel', None) is not None:
            self.joint_optimization_panel.dispose()
        if getattr(self, 'robustness_authoring_panel', None) is not None:
            self.robustness_authoring_panel.dispose()
        if getattr(self, 'system_expansion_compare_panel', None) is not None:
            self.system_expansion_compare_panel.dispose()
        self.controller.dispose()
        self.viewport_widget.close()
        self.robustness_viewport_widget.close()
        self.domain_viewport_widget.close()
        event.accept()

    def _build_setup_page(self) -> QWidget:
        body = QWidget()
        set_surface_role(body, SurfaceRole.BASE)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 8, 12)
        layout.setSpacing(12)
        layout.addWidget(
            _heading(
                "探索設定",
                "保存済みの部屋・配置と制約に探索範囲を結び付けます。候補生成の前提だけをここで定義します。",
            )
        )

        search_card, search = _card(
            "位置の探索範囲",
            "候補はハード制約を満たす幾何配置です。ここでは順位や推奨を決めません。",
        )
        # #1090: task-first authoring — presets + the visual preview are the
        # primary surface; the low-level numeric axis form and the linked
        # variable authoring stay collapsed behind 詳細 blocks.
        form = QFormLayout()
        form.addRow("名前", _required(self.search_name_field, "search_name_field"))
        form.addRow("可動物体", _required(self.search_entity_combo, "search_entity_combo"))
        preset_row = QWidget()
        preset_layout = QHBoxLayout(preset_row)
        preset_layout.setContentsMargins(0, 0, 0, 0)
        preset_layout.setSpacing(8)
        preset_layout.addWidget(
            _required(self.search_preset_combo, "search_preset_combo"), 1
        )
        preset_layout.addWidget(
            _required(self.search_preset_apply_button, "search_preset_apply_button")
        )
        form.addRow("プリセット", preset_row)
        form.addRow("候補上限", _required(self.search_limit_field, "search_limit_field"))
        search.addLayout(form)

        axis_actions = QHBoxLayout()
        axis_actions.addWidget(
            _button(
                "軸を追加 / 更新",
                self.add_search_axis,
                tip="上のフォーム（可動物体・プリセット・詳細指定）の内容を探索軸として登録・更新します。",
            )
        )
        axis_actions.addWidget(
            _button(
                "選択軸を削除",
                self.remove_selected_search_axis,
                tip="一覧で選択中の探索軸を削除します。候補生成には残った軸だけが使われます。",
            )
        )
        axis_actions.addStretch(1)
        search.addLayout(axis_actions)
        search.addWidget(_required(self.search_axis_tree, "search_axis_tree"))
        search.addWidget(self.search_domain_preview)

        axis_detail = QWidget()
        axis_detail_form = QFormLayout(axis_detail)
        axis_detail_form.setContentsMargins(0, 0, 0, 0)
        axis_detail_form.addRow("軸", _required(self.search_axis_combo, "search_axis_combo"))
        axis_detail_form.addRow("最小", _required(self.search_min_field, "search_min_field"))
        axis_detail_form.addRow("最大", _required(self.search_max_field, "search_max_field"))
        axis_detail_form.addRow("刻み", _required(self.search_step_field, "search_step_field"))
        search.addWidget(
            _advanced_block(
                "詳細: 軸を数値で指定",
                "プリセットや3Dプレビューのハンドル操作の結果を微調整します。",
                axis_detail,
            )
        )

        linked_content = QWidget()
        linked_layout = QVBoxLayout(linked_content)
        linked_layout.setContentsMargins(0, 0, 0, 0)
        linked_layout.setSpacing(8)
        linked_note = QLabel(
            "連動探索変数は1つの可動軸から従属物体を派生します。"
            "mirror_x は部屋中心線を仮定せず鏡面xを必須指定します。"
        )
        linked_note.setWordWrap(True)
        linked_layout.addWidget(linked_note)
        linked_form = QFormLayout()
        linked_form.addRow(
            "マスター", _required(self.linked_master_combo, "linked_master_combo")
        )
        linked_form.addRow(
            "スレーブ", _required(self.linked_slave_combo, "linked_slave_combo")
        )
        linked_form.addRow(
            "関係", _required(self.linked_relation_combo, "linked_relation_combo")
        )
        linked_form.addRow(
            "鏡面 x", _required(self.linked_mirror_field, "linked_mirror_field")
        )
        linked_layout.addLayout(linked_form)
        linked_actions = QHBoxLayout()
        linked_actions.addWidget(
            _required(self.linked_add_button, "linked_add_button")
        )
        linked_actions.addWidget(
            _required(self.linked_remove_button, "linked_remove_button")
        )
        linked_actions.addStretch(1)
        linked_layout.addLayout(linked_actions)
        linked_layout.addWidget(_required(self.search_linked_tree, "search_linked_tree"))
        search.addWidget(
            _advanced_block(
                "詳細: 連動探索変数",
                "従属物体を可動軸から派生する連動ルールを定義します。",
                linked_content,
            )
        )

        binding = _required(self.search_binding_label, "search_binding_label")
        binding.setWordWrap(True)
        search.addWidget(binding)
        save_button = _required(self.search_save_button, "search_save_button")
        set_primary_action(save_button)
        search.addWidget(save_button)

        specs_card, specs = _card(
            "保存済み探索設定",
            "部屋または制約が変更された探索設定は、再設定が必要な状態として候補生成には使われません。"
            " 「同じ条件で再探索」で現在の部屋へ再作成できます。",
        )
        specs.addWidget(_required(self.search_spec_tree, "search_spec_tree"))
        specs.addWidget(
            _required(self.search_reauthor_button, "search_reauthor_button")
        )
        layout.addWidget(search_card)
        layout.addWidget(specs_card)

        extended_content = QWidget()
        extended_layout = QVBoxLayout(extended_content)
        extended_layout.setContentsMargins(0, 4, 0, 0)
        extended_layout.setSpacing(10)

        capability_row = QHBoxLayout()
        capability_row.addWidget(_required(self.extended_capability_combo, "extended_capability_combo"), 1)
        # #901: synthetic-fixture capability creation is a developer-only
        # affordance — production sessions create capabilities exclusively
        # from owned-room validation results.
        if developer_mode_enabled():
            capability_row.addWidget(
                _button("開発用の向き探索を有効化", self.create_synthetic_extended_capability)
            )
        capability_row.addWidget(
            _button("選択した検証結果から本番向け能力を作成", self.create_owned_room_extended_capability)
        )
        extended_layout.addLayout(capability_row)

        extended_form = QFormLayout()
        extended_form.addRow(
            "パラメーター",
            _required(self.extended_parameter_combo, "extended_parameter_combo"),
        )
        extended_form.addRow(
            "スピーカー",
            _required(self.extended_entity_combo, "extended_entity_combo"),
        )
        extended_form.addRow("最小", _required(self.extended_min_field, "extended_min_field"))
        extended_form.addRow("最大", _required(self.extended_max_field, "extended_max_field"))
        extended_form.addRow("刻み", _required(self.extended_step_field, "extended_step_field"))
        extended_form.addRow(
            "候補上限",
            _required(self.extended_limit_field, "extended_limit_field"),
        )
        extended_layout.addLayout(extended_form)

        extended_axis_actions = QHBoxLayout()
        extended_axis_actions.addWidget(
            _button(
                "軸を追加 / 更新",
                self.add_or_update_extended_axis,
                tip="上のフォーム（パラメーター・スピーカー・範囲）の内容を拡張探索軸として登録・更新します。",
            )
        )
        extended_axis_actions.addWidget(
            _button(
                "選択軸を削除",
                self.remove_selected_extended_axis,
                tip="一覧で選択中の拡張探索軸を削除します。候補生成には残った軸だけが使われます。",
            )
        )
        extended_axis_actions.addStretch(1)
        extended_layout.addLayout(extended_axis_actions)
        extended_layout.addWidget(_required(self.extended_axis_tree, "extended_axis_tree"))
        extended_layout.addWidget(
            _button("拡張探索設定を保存", self.save_extended_search_spec, primary=True)
        )
        extended_layout.addWidget(_required(self.extended_spec_tree, "extended_spec_tree"))

        layout.addWidget(
            _advanced_block(
                "詳細: 向き・トーインを探索",
                "向き探索が利用可能な場合だけ、音響照準や筐体ヨーを追加探索します。"
                " 合成データと実室データの区分は既存の検証ルールを維持します。",
                extended_content,
            )
        )

        self.joint_optimization_panel = JointOptimizationPanel(
            self._joint_context,
            on_status=self._set_status,
        )
        layout.addWidget(
            _advanced_block(
                "詳細: 配置 + DSP のジョイント探索",
                "配置・DSPを別々に、あるいは同時に探索する正準のジョイント仕様を作成します。"
                " ディレイ/極性/PEQなどは測定能力とデバイス制約が許す場合だけ有効です。",
                self.joint_optimization_panel,
            )
        )
        layout.addStretch(1)
        return _scroll_page(body)

    def _build_interventions_page(self) -> QWidget:
        body = QWidget()
        set_surface_role(body, SurfaceRole.BASE)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 8, 12)
        layout.setSpacing(12)
        layout.addWidget(
            _heading(
                "介入プランナー",
                "課題領域から介入スタディを作成し、介入案を独立した"
                "観測量で比較します。適用は型付き提案権威経由のみ。"
                "確認済みの効果は測定ワークスペースの検証で確定します。",
            )
        )
        self.intervention_planner_panel = InterventionPlannerPanel(
            InterventionPlanner(self._joint_context),
            self.system_expansion,
            on_status=self._set_status,
            on_navigate=self._on_navigate,
        )
        self.intervention_planner_panel.applied.connect(
            self._system_variant_applied
        )
        layout.addWidget(self.intervention_planner_panel, 1)
        return _scroll_page(body)

    def _build_candidates_page(self, viewport_widget: QWidget) -> QWidget:
        page = QWidget()
        set_surface_role(page, SurfaceRole.BASE)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(
            _heading(
                "候補",
                "候補を生成して3Dで確認します。プレビューでは保存データを変更せず、明示的に適用した操作だけがアンドゥ履歴に入ります。",
            )
        )

        splitter = QSplitter(Qt.Orientation.Horizontal)
        viewport_frame = QFrame()
        set_surface_role(viewport_frame, SurfaceRole.CANVAS)
        viewport_layout = QVBoxLayout(viewport_frame)
        viewport_layout.setContentsMargins(0, 0, 0, 0)
        viewport_layout.addWidget(viewport_widget)
        splitter.addWidget(viewport_frame)

        side_body = QWidget()
        set_surface_role(side_body, SurfaceRole.BASE)
        side = QVBoxLayout(side_body)
        side.setContentsMargins(12, 4, 4, 8)
        side.setSpacing(10)

        base_card, base = _card(
            "位置候補",
            "現在選択中の探索設定から候補を生成します。",
        )
        generation = QHBoxLayout()
        generation.addWidget(_required(self.search_generate_button, "search_generate_button"))
        generation.addWidget(_required(self.search_cancel_button, "search_cancel_button"))
        base.addLayout(generation)
        summary = _required(self.search_summary_label, "search_summary_label")
        summary.setWordWrap(True)
        base.addWidget(summary)
        reason = _required(
            self.search_generate_reason_label,
            "search_generate_reason_label",
        )
        reason.setWordWrap(True)
        base.addWidget(reason)
        base.addWidget(
            _required(
                self.search_candidate_filter_field,
                "search_candidate_filter_field",
            )
        )
        base.addWidget(
            _required(
                self.search_candidate_filter_note,
                "search_candidate_filter_note",
            )
        )
        base.addWidget(_required(self.search_candidate_tree, "search_candidate_tree"), 1)

        paging = QHBoxLayout()
        paging.addWidget(_required(self.search_prev_button, "search_prev_button"))
        paging.addWidget(_required(self.search_next_button, "search_next_button"))
        base.addLayout(paging)

        actions = QHBoxLayout()
        actions.addWidget(_required(self.search_preview_button, "search_preview_button"))
        actions.addWidget(
            _required(self.search_clear_preview_button, "search_clear_preview_button")
        )
        apply_button = _required(self.search_apply_button, "search_apply_button")
        set_primary_action(apply_button)
        actions.addWidget(apply_button)
        base.addLayout(actions)
        side.addWidget(base_card)

        extended_content = QWidget()
        extended = QVBoxLayout(extended_content)
        extended.setContentsMargins(0, 4, 0, 0)
        extended.setSpacing(8)
        extended_generation = QHBoxLayout()
        extended_generation.addWidget(
            _required(self.extended_generate_button, "extended_generate_button")
        )
        extended_generation.addWidget(
            _required(self.extended_cancel_button, "extended_cancel_button")
        )
        extended.addLayout(extended_generation)
        extended_summary = _required(self.extended_summary_label, "extended_summary_label")
        extended_summary.setWordWrap(True)
        extended.addWidget(extended_summary)
        extended.addWidget(
            _required(
                self.extended_candidate_filter_field,
                "extended_candidate_filter_field",
            )
        )
        extended.addWidget(
            _required(
                self.extended_candidate_filter_note,
                "extended_candidate_filter_note",
            )
        )
        extended.addWidget(
            _required(self.extended_candidate_tree, "extended_candidate_tree")
        )
        extended_paging = QHBoxLayout()
        extended_paging.addWidget(_required(self.extended_prev_button, "extended_prev_button"))
        extended_paging.addWidget(_required(self.extended_next_button, "extended_next_button"))
        extended.addLayout(extended_paging)
        extended_actions = QHBoxLayout()
        extended_actions.addWidget(
            _required(self.extended_preview_button, "extended_preview_button")
        )
        extended_actions.addWidget(
            _required(self.extended_clear_preview_button, "extended_clear_preview_button")
        )
        extended_apply = _required(self.extended_apply_button, "extended_apply_button")
        set_primary_action(extended_apply)
        extended_actions.addWidget(extended_apply)
        extended.addLayout(extended_actions)

        side.addWidget(
            _advanced_block(
                "詳細: 拡張候補",
                "拡張探索設定がある場合に、位置と向きの候補を同じルールで確認します。",
                extended_content,
            )
        )
        side.addStretch(1)

        side_scroll = _scroll_page(side_body)
        side_scroll.setMinimumWidth(300)
        side_scroll.setMaximumWidth(640)
        side_scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        splitter.addWidget(side_scroll)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([760, 340])
        layout.addWidget(splitter, 1)
        return page

    def _build_comparison_page(self) -> QWidget:
        body = QWidget()
        set_surface_role(body, SurfaceRole.BASE)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 8, 12)
        layout.setSpacing(12)
        layout.addWidget(
            _heading(
                "比較",
                "候補ごとの指標を独立して比較します。総合点や新しい推奨順位は作りません。",
            )
        )

        self.system_expansion_compare_panel = SystemExpansionOptimizePanel(
            self.system_expansion
        )
        self.system_expansion_compare_panel.robustnessRequested.connect(
            self._show_system_variant_robustness
        )
        self.system_expansion_compare_panel.applied.connect(
            self._system_variant_applied
        )
        layout.addWidget(self.system_expansion_compare_panel)

        self.standards_comparison_panel = StandardsVariantComparisonPanel(
            self.system_expansion.scene_repository,
            self.system_expansion.document_id,
        )
        layout.addWidget(self.standards_comparison_panel)

        self.multi_sub_baseline_panel = MultiSubBaselinePanel(
            self.system_expansion.scene_repository,
            self.system_expansion.document_id,
        )
        layout.addWidget(self.multi_sub_baseline_panel)

        metrics_card, metrics = _card(
            "比較する指標",
            "同じ指標集合・単位で比較できる根拠データだけをPareto比較に使います。",
        )
        metrics.addWidget(_required(self.objective_list, "objective_list"))
        refresh = _required(self.pareto_refresh_button, "pareto_refresh_button")
        set_primary_action(refresh)
        metrics.addWidget(refresh)
        summary = _required(self.pareto_summary_label, "pareto_summary_label")
        summary.setWordWrap(True)
        metrics.addWidget(summary)

        pareto_card, pareto = _card(
            "Pareto比較",
            "「非劣」は複数指標で他候補に支配されないことを示し、音質の総合順位や自動推奨ではありません。",
        )
        pareto.addWidget(_required(self.pareto_tree, "pareto_tree"), 1)
        verdict_label = _required(
            self.decision_verdict_label, "decision_verdict_label"
        )
        verdict_label.setWordWrap(True)
        pareto.addWidget(verdict_label)

        layout.addWidget(metrics_card)
        layout.addWidget(pareto_card, 1)

        # REV70: #937 Decision Brief — sealed next-action surface the
        # operator lands on after a comparison run.
        self.decision_brief_panel = DecisionBriefPanel(
            self.system_expansion.scene_repository,
            self.system_expansion.document_id,
            on_status=self._set_status,
            on_navigate=self._on_navigate,
        )
        layout.addWidget(self.decision_brief_panel)

        # REV72: #964 change-diff evidence invalidation — sealed
        # revalidation queue + verify operation on the same page.
        self.revalidation_queue_panel = RevalidationQueuePanel(
            self.system_expansion.scene_repository,
            self.system_expansion.document_id,
            queue_supplier=self._compose_revalidation_queue,
            software_runner=self._revalidation_software_runner,
            on_navigate=self._revalidation_navigate,
            on_status=self._set_status,
        )
        layout.addWidget(self.revalidation_queue_panel)
        return _scroll_page(body)

    def _build_robustness_page(self, viewport_widget: QWidget) -> QWidget:
        body = QWidget()
        set_surface_role(body, SurfaceRole.BASE)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 8, 12)
        layout.setSpacing(12)
        layout.addWidget(
            _heading(
                "ばらつき耐性",
                "保存済みばらつき評価の基準値・感度・評価サンプル内の不利側・制約・評価完全性を並べて確認します。"
                " 総合点や自動推奨は作りません。",
            )
        )

        authoring_card, authoring = _card(
            "ばらつき評価の作成",
            "評価済み候補と揺らす軸を選んでO90仕様を作成・実行します。"
            " 作成した仕様はジョイント最適化（配置+DSP）でも利用できます。",
        )
        self.robustness_authoring_panel = RobustnessAuthoringPanel(
            RobustnessAuthoringContext(
                scene_repository=self.controller.repository,
                search_repository=self.controller.search_repository,
                extended_search_repository=self.controller.extended_repository,
                objective_repository=self.controller.objective_repository,
                robustness_repository=self.controller.robustness_repository,
                document_id=self.document_id,
            ),
            selected_spec_id=lambda: self.controller.search_selected_spec_id,
            on_status=self._set_status,
        )
        self.robustness_authoring_panel.evaluationCompleted.connect(
            lambda _spec: self.controller.refresh_robustness_view()
        )
        authoring.addWidget(self.robustness_authoring_panel)
        layout.addWidget(authoring_card)

        self.system_expansion_robustness_panel = SystemExpansionRobustnessPanel(
            self.system_expansion
        )
        layout.addWidget(self.system_expansion_robustness_panel)

        overlay_card, overlay = _card(
            "3D ばらつき範囲 / 照準",
            "選択候補の位置許容差と音響照準 / キャビネットヨーの宣言範囲を、"
            "保存済みばらつき条件から読み取り専用で表示します。部屋状態は変更しません。",
        )
        viewport_widget.setMinimumHeight(300)
        overlay.addWidget(viewport_widget, 1)
        layout.addWidget(overlay_card, 1)

        overview_card, overview = _card(
            "候補比較",
            "同じ計算モデル・計算精度・評価条件で比較できる候補だけを横断評価します。"
            " 比較できない場合は理由を表示します。",
        )
        summary = _required(
            self.robustness_summary_label,
            "robustness_summary_label",
        )
        summary.setWordWrap(True)
        overview.addWidget(summary)
        comparison_tree = _required(
            self.robustness_comparison_tree,
            "robustness_comparison_tree",
        )
        comparison_tree.setMinimumHeight(180)
        overview.addWidget(comparison_tree, 1)
        tree = _required(self.robustness_tree, "robustness_tree")
        tree.setMinimumHeight(200)
        overview.addWidget(tree, 1)
        layout.addWidget(overview_card, 1)

        charts_card, charts = _card(
            "感度 / 性能分布",
            "軸別の局所感度と、保存済み評価点の有限サンプル分布を表示します。"
            " 分布頻度を暗黙の確率へ変換しません。",
        )
        sensitivity_plot = _required(
            self.robustness_sensitivity_plot,
            "robustness_sensitivity_plot",
        )
        sensitivity_plot.setMinimumHeight(170)
        charts.addWidget(sensitivity_plot)
        distribution_plot = _required(
            self.robustness_distribution_plot,
            "robustness_distribution_plot",
        )
        distribution_plot.setMinimumHeight(170)
        charts.addWidget(distribution_plot)
        distribution_note = _required(
            self.robustness_distribution_note_label,
            "robustness_distribution_note_label",
        )
        distribution_note.setWordWrap(True)
        charts.addWidget(distribution_note)
        layout.addWidget(charts_card)

        evidence_card, evidence = _card(
            "確率・制約・評価完全性",
            "有界区間を確率分布として扱いません。p95や制約違反確率は、"
            "バックエンドに明示的な確率モデルがある場合だけ表示します。",
        )
        probability = _required(
            self.robustness_probability_label,
            "robustness_probability_label",
        )
        probability.setWordWrap(True)
        evidence.addWidget(probability)
        detail = _required(
            self.robustness_detail_label,
            "robustness_detail_label",
        )
        detail.setWordWrap(True)
        evidence.addWidget(detail)
        layout.addWidget(evidence_card)

        advanced_content = QWidget()
        advanced_layout = QVBoxLayout(advanced_content)
        advanced_layout.setContentsMargins(0, 4, 0, 0)
        advanced = _required(
            self.robustness_advanced_label,
            "robustness_advanced_label",
        )
        advanced.setWordWrap(True)
        advanced_layout.addWidget(advanced)
        layout.addWidget(
            _advanced_block(
                "詳細: 権威 / モデル / 忠実度",
                "UUID・SHA・ソルバー/プロバイダー等の内部情報は通常表示から分離します。",
                advanced_content,
            )
        )
        return _scroll_page(body)

    def _build_validation_page(self) -> QWidget:
        body = QWidget()
        set_surface_role(body, SurfaceRole.BASE)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 8, 12)
        layout.setSpacing(12)
        layout.addWidget(
            _heading(
                "測定・検証",
                "候補を実測へ結び付け、事前登録した検証条件で評価します。"
                " 本番データと合成データの根拠区分は既存の検証ルールに従います。",
            )
        )

        self.system_expansion_measurement_panel = SystemExpansionMeasurementPanel(
            self.system_expansion
        )
        self.system_expansion_measurement_panel.openMeasurementsRequested.connect(
            self._open_variant_measurements
        )
        layout.addWidget(self.system_expansion_measurement_panel)

        measure_card, measure = _card(
            "測定計画",
            "適用・保存した候補と、その保存時点に正確に一致する実測データだけを関連付けます。",
        )
        measure_button = _required(self.measurement_plan_button, "measurement_plan_button")
        measure.addWidget(measure_button)
        plan_label = _required(self.measurement_plan_label, "measurement_plan_label")
        plan_label.setWordWrap(True)
        measure.addWidget(plan_label)
        measure.addWidget(_required(self.measurement_plan_tree, "measurement_plan_tree"))
        measure.addWidget(_required(self.measurement_match_list, "measurement_match_list"))
        complete = _required(self.measurement_complete_button, "measurement_complete_button")
        set_primary_action(complete)
        measure.addWidget(complete)
        layout.addWidget(measure_card)

        campaign_card, campaign = _card(
            "検証条件",
            "測定結果を見る前に調整用 / 検証用の候補、帯域、閾値を固定します。",
        )
        assignment = QHBoxLayout()
        assignment.addWidget(
            _button(
                "選択候補 → 調整用",
                lambda: self.assign_selected_candidate_to_campaign("calibration"),
            )
        )
        assignment.addWidget(
            _button(
                "選択候補 → 検証用",
                lambda: self.assign_selected_candidate_to_campaign("holdout"),
            )
        )
        assignment.addWidget(
            _button("割り当て解除", self.remove_selected_campaign_assignment)
        )
        assignment.addStretch(1)
        campaign.addLayout(assignment)
        campaign.addWidget(_required(self.campaign_assignment_tree, "campaign_assignment_tree"))

        campaign_form = QFormLayout()
        campaign_form.addRow(
            "モデル版",
            _required(self.campaign_model_version_field, "campaign_model_version_field"),
        )
        campaign_form.addRow(
            "検証帯域下限",
            _required(self.campaign_low_field, "campaign_low_field"),
        )
        campaign_form.addRow(
            "検証帯域上限",
            _required(self.campaign_high_field, "campaign_high_field"),
        )
        campaign_form.addRow(
            "検証用 RMS上限 dB",
            _required(self.campaign_residual_field, "campaign_residual_field"),
        )
        campaign_form.addRow(
            "感度上限 dB/m",
            _required(self.campaign_sensitivity_field, "campaign_sensitivity_field"),
        )
        campaign_form.addRow(
            "感度誤差上限 dB/m",
            _required(
                self.campaign_sensitivity_error_field,
                "campaign_sensitivity_error_field",
            ),
        )
        campaign_form.addRow(
            "候補差 / 再現性",
            _required(self.campaign_separation_field, "campaign_separation_field"),
        )
        campaign.addLayout(campaign_form)
        campaign.addWidget(
            _button(
                "検証条件を測定前に保存",
                self.save_validation_campaign,
                primary=True,
            )
        )
        campaign.addWidget(_required(self.campaign_tree, "campaign_tree"))

        rew_form = QFormLayout()
        rew_row = QHBoxLayout()
        rew_row.addWidget(_required(self.rew_combo, "rew_combo"), 1)
        rew_row.addWidget(_required(self.rew_refresh_button, "rew_refresh_button"))
        rew_form.addRow("REW測定", rew_row)
        rew_form.addRow(
            "測定点",
            _required(
                self.campaign_measurement_point_combo,
                "campaign_measurement_point_combo",
            ),
        )
        rew_form.addRow(
            "入力役割",
            _required(self.rew_channel_role_field, "rew_channel_role_field"),
        )
        campaign.addLayout(rew_form)

        campaign_actions = QHBoxLayout()
        campaign_actions.addWidget(_button("準備状況を更新", self.refresh_validation_campaigns))
        campaign_actions.addWidget(
            _button("比較指標の根拠データを生成", self.materialize_selected_campaign_objectives)
        )
        campaign_actions.addWidget(
            _button("選択REWを検証実測へ登録", self.read_selected_rew_for_campaign_async)
        )
        campaign.addLayout(campaign_actions)
        detail = _required(self.campaign_detail_label, "campaign_detail_label")
        detail.setWordWrap(True)
        campaign.addWidget(detail)

        applicability = QFormLayout()
        for code, label in (("geometry", "形状"), ("band", "帯域"), ("routing", "経路")):
            state: QComboBox = self.campaign_applicability_state[code]
            evidence = self.campaign_applicability_detail[code]
            attest = self.campaign_applicability_attest[code]
            applicability.addRow(f"{label} 判定", state)
            evidence_row = QHBoxLayout()
            evidence_row.addWidget(evidence, 1)
            evidence_row.addWidget(attest)
            applicability.addRow(f"{label} 根拠", evidence_row)
        campaign.addLayout(applicability)
        campaign.addWidget(
            _button(
                "検証結果を構築・保存",
                self.build_and_save_selected_campaign_validation,
                primary=True,
            )
        )
        layout.addWidget(campaign_card)

        validation_card, validation = _card(
            "保存済み検証",
            "残差 / 傾向 / 感度 / 再現性 / 適用条件と既存の推奨可否をそのまま表示します。",
        )
        validation.addWidget(
            _required(self.validation_refresh_button, "validation_refresh_button")
        )
        validation.addWidget(_required(self.validation_tree, "validation_tree"))
        validation_detail = _required(self.validation_detail_label, "validation_detail_label")
        validation_detail.setWordWrap(True)
        validation.addWidget(validation_detail)
        layout.addWidget(validation_card)

        adaptive_content = QWidget()
        adaptive = QVBoxLayout(adaptive_content)
        adaptive.setContentsMargins(0, 4, 0, 0)
        adaptive.setSpacing(10)
        adaptive_form = QFormLayout()
        adaptive_form.addRow(
            "実行範囲",
            _required(self.adaptive_scope_combo, "adaptive_scope_combo"),
        )
        adaptive_form.addRow(
            "GP長さ尺度",
            _required(self.adaptive_length_scale_field, "adaptive_length_scale_field"),
        )
        adaptive_form.addRow(
            "提案数上限",
            _required(self.adaptive_proposal_limit_field, "adaptive_proposal_limit_field"),
        )
        adaptive.addLayout(adaptive_form)
        adaptive_build_row = QHBoxLayout()
        adaptive_build = _required(self.adaptive_build_button, "adaptive_build_button")
        set_primary_action(adaptive_build)
        adaptive_build_row.addWidget(adaptive_build)
        adaptive_build_row.addWidget(
            _required(self.adaptive_cancel_button, "adaptive_cancel_button")
        )
        adaptive_build_row.addStretch(1)
        adaptive.addLayout(adaptive_build_row)
        adaptive.addWidget(_required(self.adaptive_tree, "adaptive_tree"))
        adaptive_detail = _required(self.adaptive_detail_label, "adaptive_detail_label")
        adaptive_detail.setWordWrap(True)
        adaptive.addWidget(adaptive_detail)

        adaptive_extended_form = QFormLayout()
        adaptive_extended_form.addRow(
            "正規化GP長さ尺度",
            _required(
                self.adaptive_extended_length_scale_field,
                "adaptive_extended_length_scale_field",
            ),
        )
        adaptive_extended_form.addRow(
            "拡張提案数上限",
            _required(
                self.adaptive_extended_proposal_limit_field,
                "adaptive_extended_proposal_limit_field",
            ),
        )
        adaptive.addLayout(adaptive_extended_form)
        adaptive_extended_row = QHBoxLayout()
        adaptive_extended_build = _required(
            self.adaptive_extended_build_button,
            "adaptive_extended_build_button",
        )
        adaptive_extended_row.addWidget(adaptive_extended_build)
        adaptive_extended_row.addWidget(
            _required(
                self.adaptive_extended_cancel_button,
                "adaptive_extended_cancel_button",
            )
        )
        adaptive_extended_row.addStretch(1)
        adaptive.addLayout(adaptive_extended_row)
        adaptive.addWidget(
            _required(self.adaptive_extended_tree, "adaptive_extended_tree")
        )
        adaptive_extended_detail = _required(
            self.adaptive_extended_detail_label,
            "adaptive_extended_detail_label",
        )
        adaptive_extended_detail.setWordWrap(True)
        adaptive.addWidget(adaptive_extended_detail)

        layout.addWidget(
            _advanced_block(
                "詳細: 次の測定候補",
                "既存の次候補探索ロジックを使います。合成データでの開発検証は本番推奨を解放せず、"
                " 本番利用には現在の検証条件に基づく適格な実室データが必要です。",
                adaptive_content,
            )
        )
        layout.addStretch(1)
        return _scroll_page(body)


def build_optimization_workspace_mount(
    repository: SceneRepository,
    document_id: str = F1_DOCUMENT_ID,
    *,
    on_navigate: Callable[[WorkspaceDeepLink], bool] | None = None,
    rew_client: RewReadSource | None = None,
) -> WorkspaceMount:
    """Build the UX140 workspace through the shell's existing mount contract."""

    workspace = OptimizationWorkflowWorkspace(
        repository,
        document_id,
        on_navigate=on_navigate,
        rew_client=rew_client,
    )

    return WorkspaceMount.from_widget(
        workspace,
        on_activate=workspace.activate,
        before_deactivate=workspace.before_deactivate,
        dirty_state=getattr(workspace, "dirty_state", None),
        resolve_dirty_state=getattr(workspace, "resolve_dirty_state", None),
        on_context_changed=workspace.select_section,
    )


__all__ = [
    "OPTIMIZATION_PAGE_IDS",
    "OptimizationWorkflowWorkspace",
    "build_optimization_workspace_mount",
    "normalize_optimization_page",
]
