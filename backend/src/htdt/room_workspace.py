from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from hashlib import sha256
import json
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4

from PySide6.QtCore import QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from . import file_dialog_memory
from .cad_document import (
    CommandHistoryEntry,
    CommandPresentation,
    EditStateError,
    EditorViewState,
)
from .cad_constraint_authoring import (
    add_constraint,
    constraint_entity_ids,
    make_allowed_region_constraint,
    make_pair_distance_constraint,
    make_walkway_constraint,
    make_wall_clearance_constraint,
    remove_constraint,
)
from .cad_constraints import CadConstraintAdapterError, evaluate_cad_constraints
from .cad_constraint_policy import blocking_candidate_violations
from .cad_constraint_repository import CadConstraintRepository
from .cad_display_labels import revision_display_label
from .cad_display_units import (
    DEFAULT_DISPLAY_DECIMALS,
    DISPLAY_LENGTH_UNITS,
    display_to_si,
    si_to_display,
)
from .cad_repository import SceneRevision
from .cad_scene_history import diff_scene_documents, diff_summary_lines
from .cad_measure import format_measure_result
from .field_tooltips import apply_field_tooltip
from .cad_snap import AxisName
from .cad_system_variant_repository import CadSystemVariantRepository
from .cad_video_geometry import (
    AngleRange,
    LensShiftRange,
    ProjectorSpecificationProvenance,
    SeatGeometryBinding,
    ScreenGeometryBinding,
    VideoGeometryPolicy,
    build_projector_spec_field_assertions,
    build_projector_spec_manual_evidence,
    build_projector_specification,
    evaluate_video_geometry,
    projector_spec_optical_values,
    PROJECTOR_SPEC_EVIDENCED_FIELDS,
)
from .cad_direct_view import (
    DisplayGeometryBinding,
    build_direct_view_display_specification,
    evaluate_direct_view_geometry,
)
from .cad_direct_view_repository import CadDirectViewRepository
from .cad_lighting_repository import CadLightingRepository
from .cad_video_geometry_repository import CadVideoGeometryRepository
from .cad_video_workspace import (
    CadVideoWorkspaceRepository,
    DEFAULT_POLICY,
    VideoGeometryWorkspace,
    build_direct_view_request_from_workspace,
    build_request_from_workspace,
    default_screen_binding,
    display_image_center_world,
    screen_image_center_world,
    seat_eye_world,
    video_workspace_missing_inputs,
)
from .cad_objects import (
    aim_target_entities,
    aim_yaw_pitch_deg,
    direction_from_yaw_pitch_deg,
    orientation_aligning_forward,
    speaker_aim_replacements,
)
from .cad_orientation_constraints import entity_collision_geometry_authority
from .cad_orientation_display import (
    body_view_angles,
    forward_aim_delta_deg,
    orientation_from_view_angles,
)
from .cad_listener_pose import (
    CadListenerPoseRepository,
    listener_pose_for_seat,
    seat_binding_from_pose,
)
from .cad_repository import (
    RecoverySnapshot,
    SceneRepository,
    SceneRevisionConflictError,
)
from .cad_screen_transfer import (
    CadScreenTransferRepository,
    TransferSample,
    build_screen_transfer,
    transfer_capability_label,
)
from .cad_equipment import EquipmentDataProvenance, FrequencyDomain
from .cad_acoustic_environment import (
    AcousticEnvironmentProfile,
    CadAcousticEnvironmentRepository,
)
from .cad_acoustic_material import CadAcousticMaterialRepository
from .cad_acoustic_treatment_repository import CadAcousticTreatmentRepository
from .cad_acoustic_treatment_comparison import (
    CadAcousticTreatmentComparisonRepository,
)
from .r120_geometry_compiler import ExactExternalAuthorityRef
from .cad_geometric_constraints import (
    AuthoringConstraint,
    AuthoringConstraintSet,
    CONSTRAINT_KIND_LABELS,
    constraint_guide_items,
    make_centerline_constraint,
    make_equal_spacing_constraint,
    make_fixed_distance_constraint,
    make_symmetric_pair_constraint,
    solve_constraint,
)
from .cad_layout_tools import (
    LayoutClipboard,
    LayoutError,
    align_entities,
    copy_selection,
    distribute_entities,
    duplicate_entities,
    mirror_entities_x,
    mirror_entities_y,
    mirror_speaker_pair,
    paste_clipboard,
    propose_pair_role,
)
from .cad_seating import (
    AisleSpec,
    SeatRowSpec,
    SeatingLayoutError,
    SeatingLayoutSpec,
    apply_seating_layout,
    new_seating_spec_id,
    plan_regeneration,
)
from .cad_view_state import (
    NamedViewSpec,
    PersistedViewState,
    SectionPlaneState,
    STANDARD_VIEW_LABELS,
    StandardView,
)
from .cad_scene import (
    PHYSICAL_ENTITY_KINDS,
    BodyGeometryKind,
    BodyMeshAsset,
    BodyMeshTriangle,
    BodyMeshVertex,
    EntityBodyGeometry,
    FootprintVertex,
    Offset3,
    Position3,
    Quaternion4,
    SceneDocument,
    SceneEntity,
    Size3,
    Direction3,
    domain_to_render,
    duplicated_speaker_roles,
    quaternion_to_matrix3,
    is_unassigned_speaker_role,
    make_empty_scene,
    next_unassigned_speaker_role,
    room_vertices,
    scene_content_hash,
    quaternion_from_euler_deg,
    quaternion_to_euler_deg,
)
from .cad_room_authoring import (
    RoomAuthoringError,
    validate_room_authoring_model,
)
from .mesh_import_authority import (
    MESH_IMPORT_AUTHORITY_VERSION,
    MeshImportCancelledError,
    MeshImportOversizeDecision,
    apply_mesh_import_decision,
    format_declared_source_unit,
    import_entity_mesh_asset,
    legacy_mesh_import_authority,
    make_mesh_import_authority,
    mesh_import_scene_transform,
)
from .ingress import IngressTooLargeError, read_file_bounded
from .limits import MAX_ATTACHMENT_BYTES
from .raw_mesh import RawMeshImportError, import_raw_visual_mesh
from .raw_mesh_repair import RepairedRawMesh, RepairedRawMeshDiagnosticResult
from .semantic_geometry import (
    SemanticAcousticGeometry,
    SemanticCoordinateTransform,
    SurfaceSemanticAssignment,
    convert_raw_visual_mesh_to_semantic_geometry,
    make_semantic_geometry_conversion_request,
)
from .geometry_import_dialog import GeometryImportDialog, GeometryImportRequest
from .cad_geometry_intake import (
    geometry_intake_label,
    intake_locate_anchors,
    proposal_actions_for_parts,
)
from .command_palette import flush_focused_text_editor, focused_text_editor
from .prediction_interpretation import PredictionSpatialLink
from .room_underlay import (
    IMAGE_SUFFIXES,
    DXF_SUFFIXES,
    PDF_SUFFIXES,
    MAX_SNAP_HINTS,
    MAX_SOURCE_BYTES,
    FloorPlanUnderlay,
    UnderlayCalibrationMethod,
    UnderlayImportError,
    UnderlaySourceFormat,
    calibrate_two_point,
    decode_image_bytes,
    domain_to_source,
    is_calibrated,
    new_underlay_id,
    parse_dxf,
    render_pdf_page,
    underlay_quad_domain,
    underlay_segments_domain,
    underlay_snap_points,
    utc_now_iso,
)
from .reflection_guidance_presentation import (
    ReflectionGuidanceOverlayMarker,
    ReflectionGuidanceView,
    guidance_overlay_markers,
)
from .room_viewport import (
    GuideRenderItem,
    RoomOverlayState,
    RoomViewport3D,
)
from .room_field_overlay import FieldOverlay3DRequest, RoomFieldOverlayController
from .room_viewport import (
    UnderlayRenderItem,
)
from .theater_document import TheaterWorkingDocument
from .ui_theme import (
    ControlSize,
    DARK_THEME,
    SemanticState,
    SurfaceRole,
    TypographyRole,
    set_control_size,
    set_primary_action,
    set_semantic_state,
    set_surface_role,
    set_typography_role,
)
from .error_boundary import (
    EXPECTED_OPERATION_ERRORS,
    is_authority_failure,
    report_boundary_failure,
)
from .user_facing_error import (
    log_operation_error,
    operation_error_message,
    to_user_facing_error,
    warn_user,
)
from .workflow_shell import WorkspaceMount
from .workflow_navigation import WorkspaceDeepLink, WorkspaceId
from .workspace_dirty_state import DirtyResolutionAction, WorkspaceDirtyState
from .system_expansion_workflow import SystemExpansionWorkflowService
from .system_expansion_widgets import SystemExpansionRoomPanel
from .standards_workspace import StandardsCriterionPanel
from .installation_panel import InstallationPanel
from .rack_workspace import RackWorkspacePanel
from .length_spinbox import MetricSpinBox, PendingTextSpinBox
from .room_lighting_panel import RoomLightingPreviewPanel
from .room_lighting_preview import build_lighting_scene_preview
from .room_operational_clearance import build_operational_clearance_preview
from .room_operational_clearance_panel import RoomOperationalClearancePanel
from .room_objects_panel import RoomObjectsPanel
from .room_constraints_panel import RoomConstraintsPanel
from .room_measure_input import RoomMeasureController, RoomMeasurePanel
from .room_history_panel import RoomHistoryPanel
from .seat_priority_panel import SeatPriorityPanel
from .room_video_panel import (
    DisplaySpecDialog,
    ProjectorSpecDialog,
    RoomVideoPanel,
    ScreenTransferDialog,
)
from .cad_prediction_repository import CadPredictionRepository
from .room_journey import (
    RoomJourneyStep,
    current_journey_step,
    evaluate_room_journey,
)


ROOM_CONTEXT_IDS = (
    "geometry",
    "objects",
    "placement",
    "acoustics",
    "history",
)

SPEAKER_ROLE_UNASSIGNED_LABEL = "未設定"

# Sentinel for "caller did not pass body_geometry" — distinct from an explicit
# None, which downgrades the entity back to the plain box envelope.
_UNSET: object = object()

BODY_SHAPE_ITEMS: tuple[tuple[BodyGeometryKind, str], ...] = (
    ("box", "直方体（包絡）"),
    ("cylinder", "円柱"),
    ("extruded_polygon", "多角形フットプリント"),
    ("mesh_asset", "メッシュアセット"),
)


def parse_footprint_vertices(text: str) -> tuple[FootprintVertex, ...]:
    """Parse ``x,y; x,y; …`` entity-local footprint text into vertices."""

    vertices: list[FootprintVertex] = []
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.replace(",", " ").split()
        if len(parts) != 2:
            raise ValueError("フットプリントは「x,y; x,y; …」（物体ローカル m）で入力してください")
        try:
            x_m, y_m = float(parts[0]), float(parts[1])
        except ValueError as exc:
            raise ValueError("フットプリント頂点は数値で入力してください") from exc
        vertices.append(FootprintVertex(x_m=x_m, y_m=y_m))
    if len(vertices) < 3:
        raise ValueError("フットプリントには3頂点以上が必要です")
    return tuple(vertices)


def format_footprint_vertices(vertices: tuple[FootprintVertex, ...]) -> str:
    return "; ".join(f"{vertex.x_m:g},{vertex.y_m:g}" for vertex in vertices)
# Inspector suggestions only — not a persisted enum. Custom roles remain
# free-form text and are stored verbatim.
SPEAKER_ROLE_SUGGESTIONS = (
    "FL",
    "C",
    "FR",
    "SL",
    "SR",
    "SBL",
    "SBR",
    "SUB",
    "TFL",
    "TFR",
    "TML",
    "TMR",
    "TRL",
    "TRR",
)


class RoomViewportPort(Protocol):
    entitySelected: object

    def render_document(
        self,
        document: SceneDocument,
        *,
        selected_id: str | None,
        overlays: RoomOverlayState,
        reset_camera: bool = False,
    ) -> None: ...

    def fit_scene(self) -> None: ...

    def focus_entity(self, entity_id: str) -> None: ...

    def render_proposed_entities(
        self,
        entities: tuple[SceneEntity, ...],
        *,
        selected_id: str | None = None,
    ) -> None: ...

    def close(self) -> bool: ...


ViewportFactory = Callable[[QWidget | None], QWidget]


@dataclass
class _RoomSidecarSnapshot:
    """Persisted state of every transactional Room design sidecar (#915).

    Participating sidecars — surface material assignments, the environment
    profile selection, per-seat listener pose selections, per-screen
    transfer selections, the video geometry workspace, and proposed
    treatment placements — all carry design intent and roll with the Room
    design transaction. Library records (new materials, profiles, poses,
    transfers, treatment definitions) are immediate library facts and
    installed placements are physical lifecycle facts: neither is captured
    here.
    """

    materials: dict = field(default_factory=dict)
    environment: AcousticEnvironmentProfile | None = None
    poses: dict = field(default_factory=dict)
    transfers: dict = field(default_factory=dict)
    video_workspace: VideoGeometryWorkspace | None = None
    proposed_placements: frozenset = frozenset()


class RoomWorkspaceController:
    """UX120 application boundary over existing Scene/WorkingDocument authorities."""

    def __init__(self, repository: SceneRepository, document_id: str) -> None:
        self.repository = repository
        self.document_id = document_id
        self.working: TheaterWorkingDocument
        self.view_state = EditorViewState()
        self.recovery_candidate: RecoverySnapshot | None = None
        self.constraint_repository = CadConstraintRepository(repository.path)
        self.constraint_set = None
        self.video_workspace_repository = CadVideoWorkspaceRepository(repository.path)
        self.video_workspace: VideoGeometryWorkspace | None = None
        self.video_geometry_repository = CadVideoGeometryRepository(repository)
        self.screen_transfer_repository = CadScreenTransferRepository(
            repository.path, repository
        )
        self.listener_pose_repository = CadListenerPoseRepository(
            repository.path, repository
        )
        self.variant_repository = CadSystemVariantRepository(repository)
        self.direct_view_repository = CadDirectViewRepository(
            repository, self.variant_repository
        )
        self.material_repository = CadAcousticMaterialRepository(repository.path)
        self.environment_repository = CadAcousticEnvironmentRepository(
            repository.path
        )
        self.treatment_repository = CadAcousticTreatmentRepository(
            repository, self.variant_repository
        )
        self.treatment_comparison_repository = (
            CadAcousticTreatmentComparisonRepository(
                repository, variant_repository=self.variant_repository
            )
        )
        self._underlay_calibration: dict | None = None
        # Decoded underlay rasters memoized by content blob digest so
        # refreshes don't re-hit the store and re-decode the same bytes.
        self._underlay_image_cache: dict = {}
        self._constraint_state = None  # AuthoringConstraintSet, lazy
        self._last_constraint_notes: tuple[str, ...] = ()
        # Labels of constraints broken inside a delete command — reported once
        # by mark_broken_constraints on the next refresh (#REVIEW14).
        self._pending_broken_labels: tuple[str, ...] = ()
        # (source_revision_id, committed content hash, sidecar digest)
        # acknowledged via the keep_draft resolution; edits invalidate it so
        # the prompt reappears (#915 — sidecars joined the token).
        self._draft_release: tuple[str | None, str, str] | None = None
        self._load_latest_or_seed()

    @property
    def document(self) -> SceneDocument:
        """Current presentation snapshot, including an active transform preview."""
        return self.working.document

    @property
    def committed_document(self) -> SceneDocument:
        return self.working.committed_document

    @property
    def selected_id(self) -> str | None:
        return self.view_state.selected_id

    @property
    def is_dirty(self) -> bool:
        """Scene or participating sidecar edits pending (#915).

        Scene edits live in the working document's undo stack; sidecar
        edits persist to their stores immediately but join the same design
        transaction through the baseline captured at bind time — Discard
        restores them, Keep Draft acknowledges them, Save commits them.
        """
        return self.working.is_dirty or self._sidecars_dirty()

    # --- Design-transaction sidecars (#915) ---------------------------------

    def _capture_sidecars(self) -> '_RoomSidecarSnapshot':
        """Current persisted state of every transactional sidecar store."""
        document_id = self.document_id
        return _RoomSidecarSnapshot(
            materials=self.material_repository.assignments_for_document(
                document_id
            ),
            environment=self.environment_repository.selected_profile(
                document_id
            ),
            poses=self.listener_pose_repository.selections_for_document(
                document_id
            ),
            transfers=self.screen_transfer_repository.selections_for_document(
                document_id
            ),
            video_workspace=self.video_workspace_repository.load(document_id),
            proposed_placements=frozenset(
                self.treatment_repository.proposed_placement_ids(document_id)
            ),
        )

    @staticmethod
    def _sidecar_digest(snapshot: '_RoomSidecarSnapshot') -> str:
        payload = {
            'materials': sorted(
                (sid, material.semantic_sha256)
                for sid, material in snapshot.materials.items()
            ),
            'environment': (
                None
                if snapshot.environment is None
                else snapshot.environment.semantic_hash_sha256
            ),
            'poses': sorted(
                (seat_id, pose.semantic_sha256)
                for seat_id, pose in snapshot.poses.items()
            ),
            'transfers': sorted(
                (screen_id, transfer.semantic_sha256)
                for screen_id, transfer in snapshot.transfers.items()
            ),
            'video_workspace': snapshot.video_workspace.model_dump(
                mode='json'
            ),
            'proposed_placements': sorted(snapshot.proposed_placements),
        }
        return sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
                allow_nan=False,
            ).encode('utf-8')
        ).hexdigest()

    def _sidecars_dirty(self) -> bool:
        baseline = getattr(self, '_sidecar_baseline', None)
        if baseline is None:
            return False
        return self._sidecar_digest(
            self._capture_sidecars()
        ) != self._sidecar_digest(baseline)

    def _restore_sidecars(self, snapshot: '_RoomSidecarSnapshot') -> None:
        """Roll every transactional sidecar store back to the baseline.

        Library records created during the session stay — creating an
        authority is an immediate library fact, never a design edit. Dead
        selections (seat/screen removed by the restored scene) are dropped
        rather than resurrecting entities the discard removed.
        """
        document_id = self.document_id
        current_materials = self.material_repository.assignments_for_document(
            document_id
        )
        for surface_id in set(current_materials) | set(snapshot.materials):
            target = snapshot.materials.get(surface_id)
            if target is None:
                self.material_repository.clear_assignment(
                    document_id, surface_id
                )
            elif current_materials.get(surface_id) != target:
                self.material_repository.assign_material(
                    document_id, surface_id, target
                )

        if snapshot.environment is None:
            self.environment_repository.clear_selection(document_id)
        else:
            self.environment_repository.select_profile(
                document_id, snapshot.environment
            )

        current_poses = self.listener_pose_repository.selections_for_document(
            document_id
        )
        for seat_id in set(current_poses) | set(snapshot.poses):
            target = snapshot.poses.get(seat_id)
            if target is None:
                self.listener_pose_repository.clear_selection(
                    document_id, seat_id
                )
            elif current_poses.get(seat_id) != target:
                self.listener_pose_repository.select_pose(document_id, target)

        current_transfers = (
            self.screen_transfer_repository.selections_for_document(
                document_id
            )
        )
        for screen_id in set(current_transfers) | set(snapshot.transfers):
            target = snapshot.transfers.get(screen_id)
            if target is None:
                self.screen_transfer_repository.clear_selection(
                    document_id, screen_id
                )
            elif current_transfers.get(screen_id) != target:
                self.screen_transfer_repository.select_transfer(
                    document_id, target
                )

        self.video_workspace_repository.save(snapshot.video_workspace)
        self.video_workspace = snapshot.video_workspace

        # Proposed placements authored inside the discarded session are
        # design intent, not lifecycle facts — roll them out. Installed
        # facts always survive a discard.
        for instance_id in (
            frozenset(self.treatment_repository.proposed_placement_ids(
                document_id))
            - snapshot.proposed_placements
        ):
            self.treatment_repository.delete_placement(instance_id)

        # A selection may reference an entity the restored head does not
        # have; drops are inherent — verified readers already skip stale
        # rows, so no explicit reconciliation is needed here.

    @property
    def can_edit(self) -> bool:
        return (
            self.recovery_candidate is None
            and self.document.room is not None
            and not self.working.has_preview
        )

    def _load_latest_or_seed(self) -> None:
        revision = self.repository.current_head(self.document_id)
        if revision is None:
            # A previously unseen document identity starts empty. No document
            # id — including the legacy F1 default — implicitly selects the
            # synthetic fixture; demo content only exists through the explicit
            # development seed path (#627).
            revision = self.repository.save(
                make_empty_scene(self.document_id),
                parent_revision_id=None,
            ).revision
        self.working = TheaterWorkingDocument(
            revision.document,
            source_revision_id=revision.revision_id,
            saved_content_hash=revision.content_hash,
        )
        self._draft_release = None
        record = self.repository.view_state(self.document_id)
        if record is not None:
            self.view_state = EditorViewState(
                selected_id=record.selected_id,
                selected_ids=list(record.selected_ids),
                hidden_ids=set(record.hidden_ids),
                locked_ids=set(record.locked_ids),
                object_snap_enabled=record.object_snap_enabled,
                grid_snap_enabled=record.grid_snap_enabled,
                grid_step_m=record.grid_step_m,
                angle_snap_enabled=record.angle_snap_enabled,
                angle_step_deg=record.angle_step_deg,
            )
            self.view_state.sanitize(revision.document)
        self.constraint_set = self.constraint_repository.load(self.document_id)
        self.video_workspace = self.video_workspace_repository.load(self.document_id)
        self.recovery_candidate = self.repository.recovery(self.document_id)
        # #915: the design-transaction baseline — every transactional
        # sidecar persisted for this document, captured whenever the working
        # document re-binds to a persisted head. Sidecar edits made after
        # this point count as design edits until Save or Discard resolves
        # them; library-level records (new materials, profiles, poses,
        # transfers, treatment definitions) and physical lifecycle facts
        # (installed placements) are never part of this baseline.
        self._sidecar_baseline = self._capture_sidecars()

    def reload_if_clean(self) -> bool:
        if self.is_dirty or self.working.has_preview or self.recovery_candidate is not None:
            return False
        revision = self.repository.current_head(self.document_id)
        if revision is None or revision.revision_id == self.working.source_revision_id:
            return False
        self.working = TheaterWorkingDocument(
            revision.document,
            source_revision_id=revision.revision_id,
            saved_content_hash=revision.content_hash,
        )
        self.view_state.sanitize(revision.document)
        self.recovery_candidate = self.repository.recovery(self.document_id)
        return True

    def before_deactivate(self) -> tuple[bool, str | None]:
        if self.working.has_preview:
            return False, "操作中のプレビューを確定またはキャンセルしてから画面を切り替えてください"
        if self.is_dirty and not self._draft_release_current():
            return False, "未保存の変更を保存または元に戻してから画面を切り替えてください"
        if self.recovery_candidate is not None:
            return False, "復旧データを復元または破棄してから画面を切り替えてください"
        return True, None

    # --- Dirty-state resolution port (#610/#678) ------------------------------

    def dirty_state(self) -> WorkspaceDirtyState:
        """Classification the shell turns into an explicit operator choice."""
        if self.working.has_preview:
            return "preview_active"
        if self.is_dirty:
            if self._draft_release_current():
                return "clean"
            return "dirty_recoverable"
        if self.recovery_candidate is not None:
            return "recovery_candidate_pending"
        return "clean"

    def _draft_release_current(self) -> bool:
        """Whether keep_draft already acknowledged the exact dirty state."""
        return self._draft_release == (
            self.working.source_revision_id,
            scene_content_hash(self.working.committed_document),
            self._sidecar_digest(self._capture_sidecars()),
        )

    def discard_unsaved_changes(self) -> None:
        """Reset the working document to the saved head and drop the draft.

        Scene edits restore through the working document; every
        transactional sidecar (#915) restores through its own store —
        material assignments, environment/listener-pose/screen-transfer
        selections, the video workspace, and proposed placements authored
        inside the dirty session all return to the baseline captured when
        the document last bound to its persisted head.
        """
        # Restore BEFORE re-binding: _load_latest_or_seed re-captures the
        # baseline, so the bind-time baseline must be consumed first.
        self._restore_sidecars(self._sidecar_baseline)
        self.repository.clear_recovery(self.document_id)
        self._load_latest_or_seed()

    def keep_draft(self) -> None:
        """Persist the edits as a recovery draft and release deactivation.

        The stored snapshot is what a later Recover-Draft decision restores;
        the release token keeps this deactivation from re-prompting while the
        acknowledged content — scene AND sidecars — is unchanged: any new
        edit re-blocks.
        """
        self._sync_recovery()
        self._draft_release = (
            self.working.source_revision_id,
            scene_content_hash(self.working.committed_document),
            self._sidecar_digest(self._capture_sidecars()),
        )

    def resolve_dirty_state(
        self, action: DirtyResolutionAction
    ) -> tuple[bool, str | None]:
        """Perform one explicit dirty-state resolution.

        Returns (cleared, message). A failure keeps the current context —
        e.g. a save failure leaves the dirty document untouched.
        """
        try:
            if action == "save":
                self.save()
                return True, "変更を保存しました"
            if action == "discard":
                self.discard_unsaved_changes()
                return True, "変更を破棄しました"
            if action == "keep_draft":
                self.keep_draft()
                return True, "未保存の変更を下書きとして残しました"
            if action == "commit_preview":
                if not self.working.has_preview:
                    return False, "確定できるプレビューがありません"
                self.working.commit_preview()
                self._sync_recovery()
                return True, "プレビューを確定しました"
            if action == "cancel_preview":
                if not self.working.has_preview:
                    return False, "キャンセルできるプレビューがありません"
                self.working.cancel_preview()
                self._sync_recovery()
                return True, "プレビューを破棄しました"
            if action == "recover_draft":
                if self.recovery_candidate is None:
                    return False, "復旧できる下書きがありません"
                self.recover_draft()
                return True, "下書きを復旧しました"
            if action == "discard_recovery":
                if self.recovery_candidate is None:
                    return False, "破棄できる復旧データがありません"
                self.discard_recovery()
                return True, "復旧データを破棄しました"
        except (EditStateError, ValueError) as error:
            return False, operation_error_message(error)
        return False, "この状態では実行できない操作です"

    def set_selection(self, entity_id: str | None, *, additive: bool = False) -> None:
        """N20b ordered selection: additive clicks extend, keep order, first = primary."""
        if entity_id is None:
            self.view_state.set_selection(())
        else:
            self.document.entity(entity_id)
            if additive and entity_id in self.view_state.selection:
                # Re-clicking a member makes it the primary (anchor) entity.
                selection = list(self.view_state.selection)
                selection.remove(entity_id)
                selection.insert(0, entity_id)
                self.view_state.set_selection(selection, primary_id=entity_id)
            elif additive and self.view_state.selection:
                self.view_state.set_selection(
                    list(self.view_state.selection) + [entity_id],
                    primary_id=self.view_state.selected_id,
                )
            else:
                self.view_state.set_selection((entity_id,), primary_id=entity_id)
        self._persist_view_state()

    def toggle_selection(self, entity_id: str) -> None:
        """Ctrl+click semantics: toggle membership without disturbing order."""
        self.document.entity(entity_id)
        if entity_id in self.view_state.selection:
            selection = [eid for eid in self.view_state.selection if eid != entity_id]
            primary = self.view_state.selected_id
            if primary == entity_id:
                primary = selection[0] if selection else None
            self.view_state.set_selection(selection, primary_id=primary)
        else:
            self.set_selection(entity_id, additive=True)
            return
        self._persist_view_state()

    def set_selection_many(
        self,
        entity_ids: list[str] | tuple[str, ...],
        *,
        additive: bool = False,
    ) -> None:
        """Marquee/area selection: ordered ids, first = primary; additive unions.

        Stale ids are dropped rather than raising; an empty non-additive set
        clears the selection (marquee over empty space).
        """
        selection = []
        for entity_id in entity_ids:
            try:
                self.document.entity(entity_id)
            except KeyError:
                continue
            if entity_id not in selection:
                selection.append(entity_id)
        if additive:
            merged = list(self.view_state.selection)
            merged.extend(eid for eid in selection if eid not in merged)
            self.view_state.set_selection(
                merged, primary_id=self.view_state.selected_id
            )
        else:
            self.view_state.set_selection(
                selection, primary_id=selection[0] if selection else None
            )
        self._persist_view_state()

    def set_entities_hidden(self, entity_ids: tuple[str, ...], hidden: bool) -> int:
        changed = 0
        for entity_id in entity_ids:
            # Note: `x in s != flag` is a chained comparison, not a flag test —
            # compare the membership result explicitly.
            if (entity_id in self.view_state.hidden_ids) != hidden:
                self.view_state.set_hidden(entity_id, hidden)
                changed += 1
        if changed:
            self._persist_view_state()
        return changed

    def set_entities_locked(self, entity_ids: tuple[str, ...], locked: bool) -> int:
        changed = 0
        for entity_id in entity_ids:
            if (entity_id in self.view_state.locked_ids) != locked:
                self.view_state.set_locked(entity_id, locked)
                changed += 1
        if changed:
            self._persist_view_state()
        return changed

    def delete_entities(self, entity_ids: tuple[str, ...]) -> int:
        """Undo-safe batch delete: one command, exact restore, selection cleaned.

        Constraints losing a member are marked broken inside the same Undo
        step (#REVIEW14): undoing the delete restores the entities and the
        unbroken constraint set atomically, and the refresh-time marker never
        pushes a phantom command that would livelock Undo.
        """
        if not entity_ids or not self.can_edit:
            return 0
        removed_ids = set(entity_ids)
        state = self.authoring_constraints
        after_state = None
        labels: list[str] = []
        if state.constraints:
            marked = []
            for constraint in state.constraints:
                if not constraint.broken and set(constraint.entity_ids) & removed_ids:
                    marked.append(
                        constraint.model_copy(
                            update={
                                'broken': True,
                                'broken_reason': '拘束の対象オブジェクトが削除されました',
                            }
                        )
                    )
                    labels.append(constraint.label or constraint.constraint_id)
                else:
                    marked.append(constraint)
            if labels:
                after_state = state.model_copy(
                    update={
                        'constraints': tuple(marked),
                        'solve_version': state.solve_version + 1,
                    }
                )
        if after_state is None:
            changed = self.working.delete_entities(entity_ids)
        else:
            changed = self.working.delete_entities(
                entity_ids,
                apply_side=lambda: self._apply_constraint_state(after_state),
                revert_side=lambda: self._apply_constraint_state(state),
            )
            if changed:
                self._pending_broken_labels += tuple(labels)
        self.view_state.sanitize(self.document)
        self._sync_recovery()
        self._persist_view_state()
        return changed

    def update_entities(self, updates: dict[str, dict]) -> int:
        """Atomic batch patch — group edits roll back together (one Undo)."""
        if not updates or not self.can_edit:
            return 0
        changed = self.working.update_entities(updates)
        self._sync_recovery()
        return changed

    def duplicate_selected(self) -> int:
        """Duplicate the selection (single or group) as one Undo step."""
        if not self.can_edit:
            return 0
        ids = tuple(self.view_state.selection)
        if not ids:
            return 0
        if any(self.view_state.is_locked(eid) for eid in ids):
            return 0
        # Fuse the per-entity duplicates into the single Undo step this
        # method promises (#REVIEW14) — before the fix a group duplicate
        # pushed N commands and Undo restored them one at a time.
        # Epoch marker (not history_index): index moves backwards when the
        # bounded history evicts, which would silently leave the batch
        # unfused near the cap (#REV21).
        marker = self.working.history_epoch
        new_ids: list[str] = []
        for entity_id in ids:
            source = self.document.entity(entity_id)
            new_id = f"{source.kind}-{uuid4().hex[:10]}"
            position = Position3(
                x_m=source.position.x_m + 0.10,
                y_m=source.position.y_m + 0.10,
                z_m=source.position.z_m,
            )
            if not self.working.duplicate_entity(
                entity_id,
                new_entity_id=new_id,
                name=f"{source.name} コピー",
                position=position,
            ):
                continue
            new_ids.append(new_id)
        if not new_ids:
            return 0
        self.working.merge_history_since(marker)
        self.view_state.set_selection(new_ids, primary_id=new_ids[0])
        self._sync_recovery()
        self._persist_view_state()
        return len(new_ids)

    # -- snap preferences (#481) ---------------------------------------------------

    def set_snap_preferences(
        self,
        *,
        object_snap: bool | None = None,
        grid_snap: bool | None = None,
        grid_step_m: float | None = None,
        angle_snap: bool | None = None,
        angle_step_deg: float | None = None,
    ) -> None:
        if object_snap is not None:
            self.view_state.object_snap_enabled = object_snap
        if grid_snap is not None:
            self.view_state.grid_snap_enabled = grid_snap
        if grid_step_m is not None:
            self.view_state.grid_step_m = grid_step_m
        if angle_snap is not None:
            self.view_state.angle_snap_enabled = angle_snap
        if angle_step_deg is not None:
            self.view_state.angle_step_deg = angle_step_deg
        self._persist_view_state()

    # -- constraints (#486) ----------------------------------------------------------

    def save_constraints(self) -> None:
        if self.constraint_set is not None:
            self.constraint_repository.save(self.constraint_set)

    def _apply_placement_constraints(self, constraint_set) -> None:
        """Persist the hard-constraint set, then swap it in-memory.

        Persist-first ordering: a failed save raises before any in-memory
        state changes, so an apply/revert failure cannot leave the
        controller diverged from disk.
        """

        self.constraint_repository.save(constraint_set)
        self.constraint_set = constraint_set
        self._sync_recovery()

    def update_placement_constraints(
        self,
        new_set,
        *,
        presentation: CommandPresentation | None = None,
    ) -> bool:
        """Commit a hard-constraint (#486) change as its own Undo step.

        Placement constraints are stored beside the scene (not inside the
        document), so the command carries no entity edit — Undo restores
        the previous persisted set and nothing else. Mirrors
        ``update_authoring_constraints`` (#843).
        """

        before = self.constraint_set
        if before is None or new_set == before:
            return False
        return self.working.apply_entity_set_edit(
            apply_side=lambda: self._apply_placement_constraints(new_set),
            revert_side=lambda: self._apply_placement_constraints(before),
            presentation=presentation
            or CommandPresentation(action='edit', detail='配置制約'),
        )

    def evaluate_constraints(self) -> object:
        if self.constraint_set is None:
            return None
        try:
            return evaluate_cad_constraints(self.document, self.constraint_set)
        except CadConstraintAdapterError:
            # Dangling wall/entity references must surface in the panel
            # (evaluate_error path), not be swallowed into "no results".
            raise
        except ValueError:
            return None

    def move_commit_gate(self, changed_ids: tuple[str, ...]) -> str | None:
        """Hard-constraint gate: reject a move commit that introduces violations."""
        if self.constraint_set is None or not self.constraint_set.constraints:
            return None
        try:
            before = evaluate_cad_constraints(self.committed_document, self.constraint_set)
            candidate = evaluate_cad_constraints(self.document, self.constraint_set)
        except CadConstraintAdapterError as exc:
            # A constraint whose wall/entity reference dangles cannot be
            # evaluated — block the commit honestly instead of crashing or
            # silently skipping the gate.
            return f"配置制約が参照先を失っています · {operation_error_message(exc)}"
        blocking = blocking_candidate_violations(before, candidate, changed_ids)
        if blocking:
            reasons = "、".join(item.name or item.reason_ja for item in blocking[:2])
            return f"制約違反のため確定できません: {reasons}"
        return None

    # -- video geometry (#455) -------------------------------------------------------

    def save_video_workspace(self, workspace: VideoGeometryWorkspace) -> None:
        self.video_workspace = workspace
        self.video_workspace_repository.save(workspace)

    def evaluate_video(self, variant_id: str | None = None):
        """Evaluate video geometry on the current baseline or a SystemVariant."""
        workspace = self.video_workspace
        if workspace is None:
            raise EditStateError("映像バインドを設定してください")
        missing = video_workspace_missing_inputs(self.committed_document, workspace)
        if missing:
            raise EditStateError("未設定: " + "、".join(missing))
        head = self.repository.current_head(self.document_id)
        if head is None:
            raise EditStateError("シーンを保存してから評価してください")
        variant = None
        if variant_id is not None:
            variant = next(
                (
                    item
                    for item in self.variant_repository.list_variants(self.document_id)
                    if item.variant_id == variant_id
                ),
                None,
            )
            if variant is None:
                raise EditStateError("選択したバリアントが見つかりません")
        if workspace.target_type == 'direct_view':
            # #1054: a direct-view display target is never routed through the
            # projector+screen path — no projector spec is required.
            display_specification = None
            if workspace.display_specification_sha256 is not None:
                display_specification = (
                    self.direct_view_repository.get_specification_by_hash(
                        workspace.display_specification_sha256
                    )
                )
                if display_specification is None:
                    raise EditStateError("ディスプレイ仕様が未保存です")
            request = build_direct_view_request_from_workspace(
                self.committed_document, workspace, display_specification
            )
            return evaluate_direct_view_geometry(
                baseline=head,
                variant=variant,
                display_specification=display_specification,
                request=request,
            )
        specification = self.video_geometry_repository.get_projector_specification_by_hash(
            workspace.projector_specification_sha256
        )
        if specification is None:
            raise EditStateError("プロジェクター仕様が未保存です")
        request = build_request_from_workspace(
            self.committed_document, workspace, specification
        )
        screen_transfers = None
        if request.screen.screen_transfer_ref is not None:
            screen_transfers = {}
            transfer = self.screen_transfer_repository.get_transfer(
                request.screen.screen_transfer_ref.authority_id
            )
            if transfer is not None:
                screen_transfers[transfer.transfer_id] = transfer
        return evaluate_video_geometry(
            baseline=head,
            variant=variant,
            projector_specification=specification,
            request=request,
            screen_transfers=screen_transfers,
        )

    # -- revision history (#485) -------------------------------------------------------

    def list_revisions(self) -> tuple[SceneRevision, ...]:
        return self.repository.list_revisions(self.document_id)

    def revision_labels(self) -> dict:
        return self.repository.revision_labels(self.document_id)

    def set_revision_label(self, revision_id: str, label: str, note: str) -> None:
        if label.strip() or note.strip():
            self.repository.set_revision_label(
                revision_id, label=label.strip(), note=note.strip()
            )
        else:
            self.repository.clear_revision_label(revision_id)

    def restore_revision(self, revision_id: str) -> SceneRevision:
        """Restore a historical revision as a NEW head (history stays append-only)."""
        revision = self.repository.get(revision_id)
        if revision is None:
            raise EditStateError("対象のリビジョンが見つかりません")
        if revision.document_id != self.document_id:
            raise EditStateError("対象のリビジョンは別のドキュメントに属しています")
        if self.recovery_candidate is not None:
            raise EditStateError("復旧可能な下書きを処理してから履歴を復元してください")
        if self.working.has_preview:
            raise EditStateError("プレビュー中は復元できません")
        if self.is_dirty:
            raise EditStateError("未保存の変更を保存または元に戻してから履歴を復元してください")
        head = self.repository.current_head(self.document_id)
        if head is not None and revision.revision_id == head.revision_id:
            raise EditStateError("現在の先頭版と同じ内容です")
        try:
            result = self.repository.save(
                revision.document,
                parent_revision_id=head.revision_id if head is not None else None,
            )
        except SceneRevisionConflictError as exc:
            raise EditStateError(
                "別の変更で先頭版が更新されました。最新の状態を読み込んでから復元してください"
            ) from exc
        self.working = TheaterWorkingDocument(
            result.revision.document,
            source_revision_id=result.revision.revision_id,
            saved_content_hash=result.revision.content_hash,
        )
        self.view_state.sanitize(result.revision.document)
        self.repository.clear_recovery(self.document_id)
        self._persist_view_state()
        return result.revision

    def save(self) -> bool:
        if self.recovery_candidate is not None:
            raise EditStateError("復旧可能な下書きを処理してから保存してください")
        if self.working.has_preview:
            raise EditStateError("操作中のプレビューを確定またはキャンセルしてから保存してください")
        # Sidecar edits persist to their stores at edit time but remain
        # part of this design transaction until Save commits them (#915):
        # a scene-identical save still reports a change when the sidecars
        # moved, and the commit point advances the baseline either way.
        sidecars_were_dirty = self._sidecars_dirty()
        try:
            result = self.repository.save(
                self.committed_document,
                parent_revision_id=self.working.source_revision_id,
            )
        except SceneRevisionConflictError as exc:
            raise EditStateError(
                "別の変更で先頭版が更新されました。最新の状態を読み込んでから保存してください"
            ) from exc
        self.working.mark_saved(
            result.revision.revision_id,
            result.revision.content_hash,
        )
        self.repository.clear_recovery(self.document_id)
        self._sidecar_baseline = self._capture_sidecars()
        return bool(result.created) or sidecars_were_dirty

    def undo(self) -> bool:
        if self.recovery_candidate is not None:
            return False
        changed = self.working.undo()
        if changed:
            self.view_state.sanitize(self.document)
            self._sync_recovery()
            self._persist_view_state()
        return changed

    def redo(self) -> bool:
        if self.recovery_candidate is not None:
            return False
        changed = self.working.redo()
        if changed:
            self.view_state.sanitize(self.document)
            self._sync_recovery()
            self._persist_view_state()
        return changed

    @property
    def undo_label(self) -> str | None:
        """Descriptive label for what the next undo will change (#662)."""

        return self.working.undo_label

    @property
    def redo_label(self) -> str | None:
        return self.working.redo_label

    def history_entries(self, *, limit: int | None = 20) -> tuple[CommandHistoryEntry, ...]:
        return self.working.history_entries(limit=limit)

    # --- Saved views / viewport workspace state (#545, #629) -------------------

    def persisted_view_state(self) -> PersistedViewState | None:
        """Last working camera + section for this document (fail-soft read).

        The record is non-authoritative: a corrupt payload returns ``None``
        and callers fall back to the default fit-all camera.
        """

        record = self.repository.camera_state(self.document_id)
        if record is None:
            return None
        try:
            return PersistedViewState.model_validate(record.payload)
        except (TypeError, ValueError):
            return None

    def persist_view_extras(self, state: PersistedViewState) -> None:
        self.repository.save_camera_state(
            self.document_id,
            state.model_dump(mode='json'),
        )

    def named_views(self) -> tuple[tuple[str, NamedViewSpec], ...]:
        """Validated ``(view_id, spec)`` pairs; corrupt rows fail soft."""

        views: list[tuple[str, NamedViewSpec]] = []
        for record in self.repository.named_views(self.document_id):
            try:
                spec = NamedViewSpec.model_validate(record.payload)
            except (TypeError, ValueError):
                continue
            views.append((record.record_id, spec))
        return tuple(views)

    def save_named_view(self, spec: NamedViewSpec) -> str:
        view_id = f"view-{uuid4().hex[:10]}"
        self.repository.save_named_view(
            self.document_id,
            view_id,
            spec.model_dump(mode='json'),
        )
        return view_id

    def delete_named_view(self, view_id: str) -> None:
        self.repository.delete_named_view(self.document_id, view_id)

    def isolate_entities(self, keep_ids: set[str]) -> None:
        """Hide every entity outside ``keep_ids`` (display-only isolation)."""

        self.view_state.hidden_ids = {
            entity.entity_id
            for entity in self.document.entities
            if entity.entity_id not in keep_ids
        }
        self.view_state.sanitize(self.document)
        self._persist_view_state()

    def set_hidden_ids(self, hidden_ids: set[str]) -> None:
        """Replace the visibility set (named-view recall / isolation restore)."""

        self.view_state.hidden_ids = set(hidden_ids)
        self.view_state.sanitize(self.document)
        self._persist_view_state()

    # --- Floor-plan underlays (#534) -------------------------------------------

    def underlays(self) -> tuple[FloorPlanUnderlay, ...]:
        underlays: list[FloorPlanUnderlay] = []
        for record in self.repository.underlays(self.document_id):
            try:
                underlays.append(FloorPlanUnderlay.model_validate(record.payload))
            except (TypeError, ValueError):
                continue
        return tuple(underlays)

    def _underlay(self, underlay_id: str) -> FloorPlanUnderlay:
        for underlay in self.underlays():
            if underlay.underlay_id == underlay_id:
                return underlay
        raise KeyError(underlay_id)

    def _save_underlay_record(self, underlay: FloorPlanUnderlay) -> None:
        self.repository.save_underlay(
            self.document_id,
            underlay.underlay_id,
            underlay.model_dump(mode='json'),
        )

    def import_underlay(
        self,
        file_path: str | Path,
        *,
        name: str | None = None,
        page: int = 0,
    ) -> FloorPlanUnderlay:
        """Import a PNG/JPEG, PDF page, or simple DXF as a tracing underlay.

        The source bytes are stored in the content-addressed blob store
        (provenance preserved), the parsed record lands in
        ``floor_plan_underlays``, and nothing here touches SceneRevisions.
        """

        path = Path(file_path)
        try:
            data = read_file_bounded(path, MAX_SOURCE_BYTES)
        except IngressTooLargeError as exc:
            raise UnderlayImportError(
                'ファイルが大きすぎます (64 MB まで)'
            ) from exc
        suffix = path.suffix.lower()
        source_sha = ''
        render_sha: str | None = None
        source_format: UnderlaySourceFormat
        width: float | None = None
        height: float | None = None
        source_page = 0
        segments: tuple = ()
        hints: tuple = ()

        if suffix in IMAGE_SUFFIXES:
            source_format = UnderlaySourceFormat.IMAGE
            array = decode_image_bytes(data)
            height, width = float(array.shape[0]), float(array.shape[1])
            source_sha = self.repository.store_blob(data)
            render_sha = source_sha
        elif suffix in DXF_SUFFIXES:
            source_format = UnderlaySourceFormat.DXF
            parsed = parse_dxf(data)
            if not parsed.segments and not parsed.points:
                raise UnderlayImportError(
                    'DXF から図形を読み取れませんでした (LINE/LWPOLYLINE/POINT のみ対応)'
                )
            segments = parsed.segments
            hints = parsed.points
            xs = [point[0] for segment in segments for point in segment] + [
                point[0] for point in hints
            ]
            ys = [point[1] for segment in segments for point in segment] + [
                point[1] for point in hints
            ]
            if xs and ys:
                width = max(xs) - min(xs)
                height = max(ys) - min(ys)
            source_sha = self.repository.store_blob(data)
        elif suffix in PDF_SUFFIXES:
            source_format = UnderlaySourceFormat.PDF_PAGE
            png = render_pdf_page(data, page=int(page))
            array = decode_image_bytes(png)
            height, width = float(array.shape[0]), float(array.shape[1])
            source_sha = self.repository.store_blob(data)
            render_sha = self.repository.store_blob(png)
            source_page = int(page)
        else:
            raise UnderlayImportError(
                f'未対応の形式です: {suffix or path.name} (PNG/JPEG/PDF/DXF のみ対応)'
            )

        underlay = FloorPlanUnderlay(
            underlay_id=new_underlay_id(),
            name=(name or path.stem or '下図')[:64],
            source_format=source_format,
            source_file_name=path.name,
            imported_at_utc=utc_now_iso(),
            source_blob_sha256=source_sha,
            render_blob_sha256=render_sha,
            source_page=source_page,
            source_width=width,
            source_height=height,
            segments=segments,
            snap_hints=hints,
        )
        self._save_underlay_record(underlay)
        return underlay

    def update_underlay(self, underlay_id: str, **fields) -> FloorPlanUnderlay:
        """Editor-authority underlay updates (placement, visibility, lock)."""

        underlay = self._underlay(underlay_id)
        allowed = {
            'name',
            'origin_x_m',
            'origin_y_m',
            'rotation_deg',
            'visible',
            'locked',
            'opacity',
            'elevation_m',
            'units_per_meter',
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if updates.get('units_per_meter') is not None:
            updates['calibration'] = UnderlayCalibrationMethod.MANUAL
            updates['calibration_point_a'] = None
            updates['calibration_point_b'] = None
            updates['calibration_distance_m'] = None
        updated = underlay.model_copy(update=updates)
        self._save_underlay_record(updated)
        return updated

    def delete_underlay_record(self, underlay_id: str) -> None:
        self.repository.delete_underlay(self.document_id, underlay_id)
        state = self._underlay_calibration
        if state is not None and state['underlay_id'] == underlay_id:
            self._underlay_calibration = None

    # Two-point calibration flow -------------------------------------------------

    def begin_underlay_calibration(self, underlay_id: str) -> None:
        self._underlay(underlay_id)
        self._underlay_calibration = {'underlay_id': underlay_id, 'points': []}

    def cancel_underlay_calibration(self) -> None:
        self._underlay_calibration = None

    @property
    def underlay_calibration_underlay_id(self) -> str | None:
        state = self._underlay_calibration
        return state['underlay_id'] if state is not None else None

    def handle_underlay_click(self, underlay_id: str, x_m: float, y_m: float) -> str | None:
        """Feed a picked domain point into the armed two-point calibration.

        Returns 'first' when the first point was captured, 'ready' once two
        points exist (the caller then asks for the true distance), else None.
        """

        state = self._underlay_calibration
        if state is None or state['underlay_id'] != underlay_id:
            return None
        underlay = self._underlay(underlay_id)
        u, v = domain_to_source(underlay, x_m, y_m)
        if len(state['points']) >= 2:
            state['points'] = [state['points'][0], (u, v)]
        else:
            state['points'].append((u, v))
        if len(state['points']) < 2:
            return 'first'
        return 'ready'

    def finish_underlay_calibration(self, distance_m: float) -> FloorPlanUnderlay:
        state = self._underlay_calibration
        if state is None or len(state['points']) < 2:
            raise EditStateError('先に下図上の2点をクリックしてください')
        underlay = self._underlay(state['underlay_id'])
        updated = calibrate_two_point(
            underlay,
            state['points'][0],
            state['points'][1],
            float(distance_m),
        )
        self._save_underlay_record(updated)
        self._underlay_calibration = None
        return updated

    def underlay_snap_points(self) -> tuple[tuple[float, float], ...]:
        """Domain-space snap hints from visible, calibrated, unlocked underlays."""

        points: list[tuple[float, float]] = []
        for underlay in self.underlays():
            if underlay.locked or not underlay.visible:
                continue
            points.extend(underlay_snap_points(underlay))
            if len(points) >= MAX_SNAP_HINTS:
                break
        return tuple(points)

    # --- Authoring constraints (#618) -------------------------------------------

    @property
    def authoring_constraints(self):
        """The persisted authoring-constraint set (lazy read, fail-closed).

        Corrupt authority raises — it is a retained integrity problem, never
        reinterpreted as an empty set that a later edit would overwrite
        (#843).
        """

        if self._constraint_state is None:
            record = self.repository.authoring_constraints(self.document_id)
            if record is None:
                self._constraint_state = AuthoringConstraintSet()
            else:
                self._constraint_state = AuthoringConstraintSet.model_validate(
                    record.payload
                )
        return self._constraint_state

    def _save_authoring_constraints(self, state=None) -> None:
        state = self._constraint_state if state is None else state
        if state is None:
            return
        self.repository.save_authoring_constraints(
            self.document_id,
            state.model_dump(mode='json'),
            scene_revision_id=self.working.source_revision_id,
        )

    def _apply_constraint_state(self, state) -> None:
        """Persist one versioned revision, then swap the in-memory set.

        Persist-first ordering: a failed save raises before any in-memory
        state changes, so an apply/revert failure cannot leave the
        controller diverged from disk.
        """

        self._save_authoring_constraints(state)
        self._constraint_state = state
        self._sync_recovery()

    def update_authoring_constraints(
        self,
        new_state,
        *,
        presentation: CommandPresentation | None = None,
    ) -> bool:
        """Commit a constraint-set change as its own Undo step (#843).

        The command carries no entity edit: Undo restores the previous
        constraint set and nothing else.
        """

        before = self.authoring_constraints
        if new_state == before:
            return False
        return self.working.apply_entity_set_edit(
            apply_side=lambda: self._apply_constraint_state(new_state),
            revert_side=lambda: self._apply_constraint_state(before),
            presentation=presentation
            or CommandPresentation(action='edit', detail='拘束'),
        )

    def add_authoring_constraint(self, constraint: AuthoringConstraint) -> None:
        constraints = list(self.authoring_constraints.constraints)
        constraints.append(constraint)
        self.update_authoring_constraints(
            self.authoring_constraints.model_copy(
                update={
                    'constraints': tuple(constraints),
                    'solve_version': self.authoring_constraints.solve_version + 1,
                }
            )
        )

    def remove_authoring_constraints_for(self, entity_ids: set[str]) -> int:
        """Remove constraints involving the given entities. Returns count."""

        keep = tuple(
            constraint
            for constraint in self.authoring_constraints.constraints
            if not (set(constraint.entity_ids) & entity_ids)
        )
        removed = len(self.authoring_constraints.constraints) - len(keep)
        if removed <= 0:
            return 0
        self.update_authoring_constraints(
            self.authoring_constraints.model_copy(
                update={
                    'constraints': keep,
                    'solve_version': self.authoring_constraints.solve_version + 1,
                }
            )
        )
        return removed

    def pop_constraint_notes(self) -> tuple[str, ...]:
        """Solve notes from the last propagated edit (consumed once)."""

        notes, self._last_constraint_notes = self._last_constraint_notes, ()
        return notes

    def pop_pending_broken_labels(self) -> tuple[str, ...]:
        """Labels of constraints broken inside a delete command (consumed once).

        The delete step marks constraints atomically, but the refresh still
        reports the breakage through mark_broken_constraints.
        """

        labels, self._pending_broken_labels = self._pending_broken_labels, ()
        return labels

    def propagate_constraints(
        self,
        changed_ids: set[str],
        *,
        merge_with_previous: bool = False,
    ) -> tuple[str, ...]:
        """Re-solve constraints touched by an edit; returns warning notes.

        Deterministic local solve: only the declared driver propagates to its
        subjects. A subject-side edit that would violate the constraint marks
        it broken — it is surfaced, never silently dropped or jittered.

        ``merge_with_previous`` fuses the propagation step into the driver
        edit that caused it, so one Undo restores both (#REVIEW14); callers
        only set it immediately after committing the driver edit.
        """

        state = self.authoring_constraints
        if not state.constraints or not changed_ids:
            return ()
        notes: list[str] = []
        updates: dict[str, dict[str, object]] = {}
        constraints = list(state.constraints)
        changed_constraints = False
        for index, constraint in enumerate(constraints):
            if constraint.broken:
                continue
            members = [eid for eid in constraint.entity_ids if eid in changed_ids]
            if not members:
                continue
            resolution = solve_constraint(
                constraint,
                self.working.committed_document,
                driver_moved=members,
            )
            if resolution.broken_reason is not None:
                constraints[index] = constraint.model_copy(
                    update={
                        'broken': True,
                        'broken_reason': resolution.broken_reason,
                    }
                )
                changed_constraints = True
                notes.append(resolution.broken_reason)
                continue
            for entity_id, position in resolution.moved.items():
                entry = updates.setdefault(entity_id, {})
                entry['position'] = position
            for entity_id, orientation in resolution.rotated.items():
                entry = updates.setdefault(entity_id, {})
                entry['orientation'] = orientation
        replaced_before: list[SceneEntity] = []
        replaced_after: list[SceneEntity] = []
        for entity_id, fields in updates.items():
            try:
                current = self.document.entity(entity_id)
            except KeyError:
                continue
            after = current.model_copy(update=fields)
            if after == current:
                continue
            replaced_before.append(current)
            replaced_after.append(after)
            notes.append(f'拘束により「{current.name}」を調整しました')
        if updates or changed_constraints:
            new_state = state.model_copy(
                update={
                    'constraints': tuple(constraints),
                    'solve_version': state.solve_version + 1,
                }
            )
            # One Undo unit: entity propagation and the constraint-state
            # mutation commit together (#843).
            epoch_before = self.working.history_epoch
            self.working.apply_entity_set_edit(
                replaced_before=tuple(replaced_before),
                replaced_after=tuple(replaced_after),
                presentation=CommandPresentation(
                    action='transform', detail='拘束による追従'
                ),
                apply_side=lambda: self._apply_constraint_state(new_state),
                revert_side=lambda: self._apply_constraint_state(state),
            )
            # Epoch (not index) counts pushes even when cap eviction moved
            # the cursor backwards mid-op (#REV21).
            if (
                merge_with_previous
                and self.working.history_epoch == epoch_before + 1
            ):
                self.working.merge_last(2)
        return tuple(notes)

    def mark_broken_constraints(self) -> tuple[str, ...]:
        """Flag constraints whose members vanished (delete/replace). Returns
        labels of newly broken constraints — surfaced, never rebound by name."""

        state = self.authoring_constraints
        if not state.constraints:
            return self.pop_pending_broken_labels()
        existing = {entity.entity_id for entity in self.document.entities}
        broken_labels: list[str] = []
        constraints = list(state.constraints)
        changed = False
        for index, constraint in enumerate(constraints):
            if constraint.broken:
                continue
            missing = [eid for eid in constraint.entity_ids if eid not in existing]
            if missing:
                constraints[index] = constraint.model_copy(
                    update={
                        'broken': True,
                        'broken_reason': '拘束の対象オブジェクトが削除されました',
                    }
                )
                broken_labels.append(constraint.label or constraint.constraint_id)
                changed = True
        if changed:
            # Refresh-derived marking is state maintenance, not a user edit:
            # apply the constraint set directly so Undo is never wedged behind
            # a phantom step it must immediately recreate (#REVIEW14).
            self._apply_constraint_state(
                state.model_copy(
                    update={
                        'constraints': tuple(constraints),
                        'solve_version': state.solve_version + 1,
                    }
                )
            )
        return self.pop_pending_broken_labels() + tuple(broken_labels)

    def guide_render_items(self) -> tuple[GuideRenderItem, ...]:
        """Construction-guide lines derived from constraints (display-only)."""

        return constraint_guide_items(self.authoring_constraints, self.document)

    def underlay_render_items(self) -> tuple[UnderlayRenderItem, ...]:
        """Resolve persisted underlays + blob bytes into render items."""

        items: list[UnderlayRenderItem] = []
        image_cache = getattr(self, "_underlay_image_cache", None)
        if image_cache is None:
            image_cache = self._underlay_image_cache = {}
        seen_digests: set[str] = set()
        for underlay in self.underlays():
            if not underlay.visible:
                continue
            image = None
            if underlay.render_blob_sha256:
                digest = underlay.render_blob_sha256
                seen_digests.add(digest)
                if digest in image_cache:
                    # A cached raster still must not outlive its blob: a
                    # deleted render blob renders empty (missing_source
                    # flags it), never the last-good pixels.
                    if self.repository.has_blob(digest):
                        image = image_cache[digest]
                    else:
                        del image_cache[digest]
                else:
                    data = self.repository.read_blob(digest)
                    if data:
                        try:
                            image = decode_image_bytes(data)
                        except UnderlayImportError:
                            image = None
                    if image is not None:
                        image_cache[digest] = image
            items.append(
                UnderlayRenderItem(
                    underlay_id=underlay.underlay_id,
                    name=underlay.name,
                    quad_domain=underlay_quad_domain(underlay),
                    image=image,
                    segments_domain=underlay_segments_domain(underlay),
                    opacity=underlay.opacity,
                    elevation_m=underlay.elevation_m,
                    missing_source=self.underlay_missing_source(underlay),
                )
            )
        for digest in tuple(image_cache):
            if digest not in seen_digests:
                del image_cache[digest]
        return tuple(items)

    def underlay_missing_source(self, underlay: FloorPlanUnderlay) -> bool:
        """True when the record references blob bytes the store lost.

        A missing source or render blob means the underlay renders empty or
        its provenance is gone — surfaces must mark it, never present a
        normal-looking blank underlay (integrity failure, not empty data).
        """

        for digest in (underlay.render_blob_sha256, underlay.source_blob_sha256):
            if digest and not self.repository.has_blob(digest):
                return True
        return False

    def add_object(self, kind: str) -> SceneEntity:
        if not self.can_edit:
            raise EditStateError("オブジェクト追加には完成した部屋と編集可能な下書きが必要です")
        entity = self._make_object(kind)
        if not self.working.add_entity(entity):
            raise EditStateError("オブジェクトを追加できませんでした")
        self.view_state.set_selection((entity.entity_id,), primary_id=entity.entity_id)
        self._sync_recovery()
        self._persist_view_state()
        return entity

    def replace_room(self, room) -> bool:
        if self.recovery_candidate is not None:
            raise EditStateError("復旧データを処理してから部屋形状を編集してください")
        changed = self.working.replace_room(room)
        if changed:
            self._sync_recovery()
        return changed

    def replace_room_topology(self, room, topology) -> bool:
        if self.recovery_candidate is not None:
            raise EditStateError("復旧データを処理してから壁・開口を編集してください")
        changed = self.working.replace_room_topology(room, topology)
        if changed:
            self._sync_recovery()
        return changed

    def replace_room_authoring(self, authoring) -> bool:
        """Commit #976 semantic primitives (None clears them) as one undoable edit.

        The candidate is fully validated here — RoomAuthoringError carries the
        typed fail-closed findings the panel renders next to the offending row.
        """
        if self.recovery_candidate is not None:
            raise EditStateError("復旧データを処理してから高度な形状を編集してください")
        if authoring is not None:
            issues = validate_room_authoring_model(
                authoring,
                wall_topology=self.committed_document.wall_topology,
            )
            errors = tuple(issue for issue in issues if issue.severity == 'error')
            if errors:
                raise RoomAuthoringError(errors)
        changed = self.working.replace_room_authoring(authoring)
        if changed:
            self._sync_recovery()
        return changed

    def update_selected(
        self,
        *,
        name: str,
        position: Position3,
        size_m: Size3 | None,
        speaker_role: str | None,
        orientation: Quaternion4 | None = None,
        aim_yaw_pitch_deg: tuple[float, float] | None = None,
        body_geometry: EntityBodyGeometry | None | object = _UNSET,
    ) -> bool:
        entity_id = self.selected_id
        if entity_id is None:
            return False
        if not self.can_edit:
            raise EditStateError("現在の状態では選択項目を編集できません")
        if self.view_state.is_locked(entity_id):
            raise EditStateError("ロック中のオブジェクトは編集できません")
        entity = self.document.entity(entity_id)
        updates: dict[str, object] = {
            "name": name.strip() or entity.name,
            "position": position,
        }
        if entity.size_m is not None:
            if size_m is None:
                raise EditStateError("物理オブジェクトには寸法が必要です")
            updates["size_m"] = size_m
        if body_geometry is not _UNSET:
            # None here is an explicit downgrade back to the box envelope.
            updates["body_geometry"] = body_geometry
        if entity.kind == "speaker":
            role = (speaker_role or "").strip()
            if is_unassigned_speaker_role(role):
                # Clearing the role (or picking 未設定) keeps a unique reserved
                # placeholder instead of a plausible-looking fake channel role.
                role = next_unassigned_speaker_role(
                    item.speaker_role
                    for item in self.document.entities
                    if item.kind == "speaker" and item.entity_id != entity_id
                )
            updates["speaker_role"] = role
        if orientation is not None:
            if entity.kind not in PHYSICAL_ENTITY_KINDS:
                raise EditStateError("この種類のオブジェクトには姿勢フィールドがありません")
            # Numeric orientation is an exact pose edit through the same
            # quaternion authority as gizmo rotation; it never touches aim_xyz.
            updates["orientation"] = orientation
        if aim_yaw_pitch_deg is not None:
            if entity.kind != "speaker":
                raise EditStateError("音響方向はスピーカー専用です")
            # Acoustic aim edits never rotate the cabinet; aligning the body is
            # a separate explicit action (align_selected_cabinet_to_aim).
            updates["aim_xyz"] = direction_from_yaw_pitch_deg(
                yaw_deg=aim_yaw_pitch_deg[0],
                pitch_deg=aim_yaw_pitch_deg[1],
            )
        changed = self.working.update_entity(entity_id, **updates)
        if changed:
            notes = self.propagate_constraints(
                {entity_id}, merge_with_previous=True
            )
            self._sync_recovery()
            if notes:
                self._last_constraint_notes = notes
        return changed

    def aim_targets(self) -> tuple[SceneEntity, ...]:
        """Seats and measurement points the selected speaker may be aimed at."""

        return aim_target_entities(self.document)

    def _selected_speaker_for_edit(self) -> SceneEntity:
        entity_id = self.selected_id
        if entity_id is None:
            raise EditStateError("スピーカーを選択してください")
        if not self.can_edit:
            raise EditStateError("現在の状態では選択項目を編集できません")
        if self.view_state.is_locked(entity_id):
            raise EditStateError("ロック中のオブジェクトは編集できません")
        entity = self.document.entity(entity_id)
        if entity.kind != "speaker":
            raise EditStateError("音響方向はスピーカー専用です")
        return entity

    def aim_selected_speaker_at(self, target_id: str) -> bool:
        """Set the selected speaker's acoustic aim toward a seat/measurement point.

        Body orientation is intentionally untouched; one Undo transaction.
        """

        entity = self._selected_speaker_for_edit()
        replacements = speaker_aim_replacements(
            self.document,
            (entity.entity_id,),
            target_id,
        )
        changed = self.working.update_entity(
            entity.entity_id,
            aim_xyz=replacements[0].aim_xyz,
        )
        if changed:
            self._sync_recovery()
        return changed

    def clear_selected_speaker_aim(self) -> bool:
        """Reset the selected speaker's acoustic aim to explicit unknown (None)."""

        entity = self._selected_speaker_for_edit()
        if entity.aim_xyz is None:
            return False
        changed = self.working.update_entity(entity.entity_id, aim_xyz=None)
        if changed:
            self._sync_recovery()
        return changed

    def align_selected_cabinet_to_aim(self) -> bool:
        """Explicit command: rotate the cabinet so its front (+Y) follows the aim.

        The acoustic aim itself is never modified by this action.
        """

        entity = self._selected_speaker_for_edit()
        if entity.aim_xyz is None:
            raise EditStateError("音響方向が未設定のため本体を合わせられません")
        changed = self.working.update_entity(
            entity.entity_id,
            orientation=orientation_aligning_forward(entity.aim_xyz),
        )
        if changed:
            self.propagate_constraints(
                {entity.entity_id}, merge_with_previous=True
            )
            self._sync_recovery()
        return changed

    def attach_mesh_asset(self, entity_id: str, file_path: str | Path) -> SceneEntity:
        """Import a mesh file as the entity's asset-backed body geometry.

        The original bytes land in the project content-addressed blob store;
        the entity persists the parsed local-coordinate mesh plus exact asset
        hash/provenance, so the body is reproducible and auditable.
        """

        if not self.can_edit:
            raise EditStateError("現在の状態ではメッシュを設定できません")
        if self.view_state.is_locked(entity_id):
            raise EditStateError("ロック中のオブジェクトは編集できません")
        entity = self.document.entity(entity_id)
        if entity.size_m is None:
            raise EditStateError("メッシュボディは物理オブジェクトのみに設定できます")
        path = Path(file_path)
        data = read_file_bounded(path, MAX_ATTACHMENT_BYTES)
        imported = import_raw_visual_mesh(data, source_name=path.name)
        self.repository.store_blob(data)
        # This legacy path parses source coordinates verbatim and cannot ask
        # the operator for units/axes — it records an explicit
        # legacy_assumed_meter authority instead of silently assuming. Use
        # mesh_import_authority.import_entity_mesh_asset for the declared
        # unit/axis/anchor path (#669).
        import_authority = legacy_mesh_import_authority()
        geometry = EntityBodyGeometry(
            kind="mesh_asset",
            mesh=BodyMeshAsset(
                asset_sha256=imported.provenance.original_asset_sha256,
                source_name=imported.provenance.source_name,
                asset_format=imported.provenance.asset_format,
                original_size_bytes=imported.provenance.original_size_bytes,
                vertices=tuple(
                    BodyMeshVertex(x_m=vertex.x, y_m=vertex.y, z_m=vertex.z)
                    for vertex in imported.vertices
                ),
                triangles=tuple(
                    BodyMeshTriangle(a=triangle.a, b=triangle.b, c=triangle.c)
                    for triangle in imported.triangles
                ),
                import_authority=import_authority,
            ),
        )
        if not self.working.update_entity(entity_id, body_geometry=geometry):
            raise EditStateError("メッシュボディを設定できませんでした")
        self._sync_recovery()
        return self.document.entity(entity_id)

    def attach_mesh_asset_declared(
        self,
        entity_id: str,
        file_path: str | Path,
        *,
        source_unit: str | None = None,
        custom_scale_to_meters: float | None = None,
        up_axis: str = 'unknown',
        forward_axis: str = 'unknown',
        handedness: str = 'unknown',
        local_anchor: str = 'source_origin',
        repaired_mesh: RepairedRawMesh | None = None,
        oversize_decision: MeshImportOversizeDecision = 'cancel',
    ) -> SceneEntity:
        """Attach a mesh through the declared-authority path (#669/#762).

        Unlike :meth:`attach_mesh_asset` (which records a legacy
        assumed-meter authority), every unit/axis/anchor term is an explicit
        operator declaration. An optional bounded-repair preview replaces the
        parsed geometry; an oversized result is resolved by the operator's
        ``oversize_decision`` rather than silently adopted.
        """

        if not self.can_edit:
            raise EditStateError("現在の状態ではメッシュを設定できません")
        if self.view_state.is_locked(entity_id):
            raise EditStateError("ロック中のオブジェクトは編集できません")
        entity = self.document.entity(entity_id)
        if entity.size_m is None:
            raise EditStateError("メッシュボディは物理オブジェクトのみに設定できます")
        path = Path(file_path)
        data = read_file_bounded(path, MAX_ATTACHMENT_BYTES)
        body_mesh, _authority = import_entity_mesh_asset(
            data,
            source_name=path.name,
            source_unit=source_unit,
            custom_scale_to_meters=custom_scale_to_meters,
            up_axis=up_axis,
            forward_axis=forward_axis,
            handedness=handedness,
            local_anchor=local_anchor,
            repaired_mesh=repaired_mesh,
        )
        self.repository.store_blob(data)
        geometry = EntityBodyGeometry(kind="mesh_asset", mesh=body_mesh)
        size_m, geometry = apply_mesh_import_decision(
            geometry, entity.size_m, decision=oversize_decision
        )
        updates: dict = {"body_geometry": geometry}
        if size_m != entity.size_m:
            updates["size_m"] = size_m
        if not self.working.update_entity(entity_id, **updates):
            raise EditStateError("メッシュボディを設定できませんでした")
        self._sync_recovery()
        return self.document.entity(entity_id)

    def import_room_mesh_geometry(
        self,
        file_path: str | Path,
        *,
        source_unit: str | None = None,
        custom_scale_to_meters: float | None = None,
        up_axis: str = 'unknown',
        forward_axis: str = 'unknown',
        handedness: str = 'unknown',
        local_anchor: str = 'source_origin',
        surface_assignments: tuple[SurfaceSemanticAssignment, ...] = (),
        repaired_mesh: RepairedRawMesh | None = None,
        repaired_diagnostic: RepairedRawMeshDiagnosticResult | None = None,
    ) -> SemanticAcousticGeometry:
        """Import an external mesh as the scene's ``r120_semantic_geometry``.

        The guided dialog's declarations become a ``MeshImportAuthority`` and
        the identical source→scene transform the entity path applies
        (``mesh_import_scene_transform``), recorded with
        ``explicit_import_metadata`` provenance. The conversion runs through
        the canonical semantic-geometry contract and the result is committed
        as one undoable ``replace_document`` step.
        """

        if not self.can_edit:
            raise EditStateError("現在の状態ではジオメトリをインポートできません")
        path = Path(file_path)
        data = read_file_bounded(path, MAX_ATTACHMENT_BYTES)
        raw_mesh = import_raw_visual_mesh(data, source_name=path.name)
        spec_unit = format_declared_source_unit(raw_mesh.provenance.asset_format)
        resolved_unit = source_unit if source_unit is not None else spec_unit
        declared_by = (
            'format_specification'
            if spec_unit != 'unknown'
            else 'operator_confirmed'
        )
        authority = make_mesh_import_authority(
            source_unit=resolved_unit,
            unit_declared_by=declared_by,
            custom_scale_to_meters=custom_scale_to_meters,
            up_axis=up_axis,
            forward_axis=forward_axis,
            handedness=handedness,
            local_anchor=local_anchor,
            importer_version=MESH_IMPORT_AUTHORITY_VERSION,
        )
        transform = SemanticCoordinateTransform(
            matrix_source_to_scene_m=mesh_import_scene_transform(
                authority,
                [(v.x, v.y, v.z) for v in raw_mesh.vertices],
            ),
            provenance='explicit_import_metadata',
            reason=f'ガイド付き幾何インポート元 {path.name}',
        )
        request = make_semantic_geometry_conversion_request(
            raw_mesh,
            source_scene_revision_id=self.working.source_revision_id,
            source_to_scene_transform=transform,
            surface_assignments=surface_assignments,
            repaired_mesh=repaired_mesh,
            repaired_diagnostic=repaired_diagnostic,
        )
        geometry = convert_raw_visual_mesh_to_semantic_geometry(
            raw_mesh,
            request,
            repaired_mesh=repaired_mesh,
            repaired_diagnostic=repaired_diagnostic,
        )
        updated = self.committed_document.model_copy(
            update={
                'schema_version': max(4, self.committed_document.schema_version),
                'r120_semantic_geometry': geometry,
            }
        )
        self.working.replace_document(
            updated,
            presentation=CommandPresentation(
                action='import_room_geometry',
                subject_names=(path.name,),
                label='部屋のジオメトリをインポート',
            ),
        )
        self._sync_recovery()
        return geometry

    def recover_draft(self) -> bool:
        recovery = self.recovery_candidate
        if recovery is None:
            return False
        source_id = recovery.source_revision_id
        source = self.repository.get(source_id) if source_id is not None else None
        if source is None:
            raise EditStateError("復旧元のリビジョンが見つかりません")
        self.working = TheaterWorkingDocument(
            recovery.document,
            source_revision_id=source.revision_id,
            saved_content_hash=source.content_hash,
        )
        self.view_state.sanitize(self.working.committed_document)
        self.recovery_candidate = None
        self._persist_view_state()
        return True

    def discard_recovery(self) -> bool:
        if self.recovery_candidate is None:
            return False
        self.repository.clear_recovery(self.document_id)
        self.recovery_candidate = None
        self._load_latest_or_seed()
        return True

    def close(self) -> None:
        if self.working.has_preview:
            self.working.cancel_preview()
        self._sync_recovery()
        self._persist_view_state()

    def _sync_recovery(self) -> None:
        if self.working.has_preview or self.recovery_candidate is not None:
            return
        if self.working.is_dirty:
            self.repository.save_recovery(
                self.document,
                source_revision_id=self.working.source_revision_id,
            )
        else:
            self.repository.clear_recovery(self.document_id)

    def _persist_view_state(self) -> None:
        self.repository.save_view_state(
            self.document_id,
            selected_id=self.view_state.selected_id,
            selected_ids=self.view_state.selection,
            hidden_ids=self.view_state.hidden_ids,
            locked_ids=self.view_state.locked_ids,
            object_snap_enabled=self.view_state.object_snap_enabled,
            grid_snap_enabled=self.view_state.grid_snap_enabled,
            grid_step_m=self.view_state.grid_step_m,
            angle_snap_enabled=self.view_state.angle_snap_enabled,
            angle_step_deg=self.view_state.angle_step_deg,
        )

    def _default_position(self, kind: str, size: Size3 | None = None) -> Position3:
        room = self.document.room
        if room is None:
            raise EditStateError("部屋を作成してからオブジェクトを追加してください")
        min_x, min_y, max_x, max_y = room.bounds_m
        width = max_x - min_x
        depth = max_y - min_y
        center_x = min_x + width * 0.5
        center_y = min_y + depth * 0.55
        if kind in {"screen", "display"}:
            return Position3(
                x_m=center_x,
                y_m=min_y + min(depth * 0.04, 0.15),
                z_m=min(max(room.height_m * 0.55, 0.5), max(room.height_m - 0.1, 0.1)),
            )
        half_height = size.z_m * 0.5 if size is not None else 0.08
        z_m = max(half_height, min(1.0, room.height_m - half_height - 0.05))
        return Position3(x_m=center_x, y_m=center_y, z_m=z_m)

    def _make_object(self, kind: str) -> SceneEntity:
        token = uuid4().hex[:10]
        if kind == "speaker":
            size = Size3(x_m=0.24, y_m=0.28, z_m=0.42)
            return SceneEntity(
                entity_id=f"speaker-{token}",
                kind="speaker",
                name="スピーカー",
                speaker_role=next_unassigned_speaker_role(
                    entity.speaker_role
                    for entity in self.document.entities
                    if entity.kind == "speaker"
                ),
                position=self._default_position(kind, size),
                size_m=size,
                acoustic_reference_offset_m=Offset3(y_m=size.y_m * 0.5),
            )
        if kind == "seat":
            size = Size3(x_m=0.70, y_m=0.80, z_m=0.90)
            return SceneEntity(
                entity_id=f"seat-{token}",
                kind="seat",
                name="座席",
                position=self._default_position(kind, size),
                orientation=quaternion_from_euler_deg(
                    yaw_deg=180.0,
                    pitch_deg=0.0,
                    roll_deg=0.0,
                ),
                size_m=size,
                acoustic_reference_offset_m=Offset3(z_m=0.65),
            )
        if kind == "screen":
            size = Size3(x_m=2.60, y_m=0.04, z_m=1.46)
            return SceneEntity(
                entity_id=f"screen-{token}",
                kind="screen",
                name="スクリーン",
                position=self._default_position(kind, size),
                size_m=size,
            )
        if kind == "display":
            size = Size3(x_m=1.50, y_m=0.06, z_m=0.85)
            return SceneEntity(
                entity_id=f"display-{token}",
                kind="display",
                name="ディスプレイ",
                position=self._default_position(kind, size),
                size_m=size,
            )
        if kind == "projector":
            size = Size3(x_m=0.40, y_m=0.32, z_m=0.16)
            return SceneEntity(
                entity_id=f"projector-{token}",
                kind="projector",
                name="プロジェクター",
                position=self._default_position(kind, size),
                size_m=size,
            )
        if kind == "riser":
            size = Size3(x_m=1.60, y_m=1.20, z_m=0.20)
            return SceneEntity(
                entity_id=f"riser-{token}",
                kind="riser",
                name="ライザー",
                position=self._default_position(kind, size),
                size_m=size,
            )
        if kind == "furniture":
            size = Size3(x_m=1.20, y_m=0.55, z_m=0.90)
            return SceneEntity(
                entity_id=f"furniture-{token}",
                kind="furniture",
                name="家具",
                position=self._default_position(kind, size),
                size_m=size,
            )
        if kind == "av_equipment":
            size = Size3(x_m=0.60, y_m=0.55, z_m=1.30)
            return SceneEntity(
                entity_id=f"av-{token}",
                kind="av_equipment",
                name="AV機器",
                position=self._default_position(kind, size),
                size_m=size,
            )
        if kind == "measurement_point":
            return SceneEntity(
                entity_id=f"point-{token}",
                kind="measurement_point",
                name="測定点",
                position=self._default_position(kind),
            )
        raise ValueError(f"unsupported object kind: {kind}")


class ObjectPalette(QFrame):
    addRequested = Signal(str)

    ITEMS = (
        ("speaker", "スピーカー"),
        ("seat", "座席"),
        ("screen", "スクリーン"),
        ("display", "ディスプレイ"),
        ("projector", "プロジェクター"),
        ("riser", "ライザー"),
        ("furniture", "家具"),
        ("av_equipment", "AV機器"),
        ("measurement_point", "測定点"),
    )

    KIND_HINTS = {
        "speaker": "音を出す機器 · 追加後、右パネルの「役割」（FL/C/SUBなど）を設定します",
        "seat": "視聴位置 · 音響予測や配置評価の受音点になります",
        "screen": "プロジェクターの映写面（スクリーン）",
        "display": "テレビなどの表示機器",
        "projector": "映写機 · スロー比やレンズシフトの仕様を設定できます",
        "riser": "後段座席用の段差（ひな段）",
        "furniture": "ソファ・棚などの家具",
        "av_equipment": "アンプ・プレーヤーなどのAV機器",
        "measurement_point": "音響測定を行う位置のマーカー",
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("roomObjectPalette")
        set_surface_role(self, SurfaceRole.RAISED)
        self.setMinimumWidth(140)
        self.setMaximumWidth(176)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)
        title = QLabel("追加")
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)
        subtitle = QLabel("部屋へ配置する項目（クリックで3D上に追加）")
        subtitle.setWordWrap(True)
        set_typography_role(subtitle, TypographyRole.SECONDARY)
        layout.addWidget(subtitle)
        for kind, label in self.ITEMS:
            button = QPushButton(label)
            button.setProperty("objectKind", kind)
            button.setToolTip(self.KIND_HINTS.get(kind, ""))
            set_control_size(button, ControlSize.STANDARD)
            button.clicked.connect(
                lambda checked=False, object_kind=kind: self.addRequested.emit(object_kind)
            )
            layout.addWidget(button)
        layout.addStretch(1)


class InspectorValidationError(ValueError):
    """Field-level validation failure carrying the owning inspector section.

    The workspace maps ``section`` to the section's inline error label so a
    rejected commit surfaces next to the field that caused it (#583).
    """

    def __init__(self, section: str, message: str) -> None:
        super().__init__(message)
        self.section = section


class Vector3Editor(QFrame):
    """Compact X/Y/Z editor triplet with per-axis mixed/dirty badges.

    Axis groups reflow between one horizontal row and a vertical stack at a
    width breakpoint, so the control stays usable at the inspector's narrow
    column width (#583). ``None`` values render an explicit "未定義" state —
    an unknown size is never presented as an editable 0.000 (#583).
    """

    AXES: tuple[str, ...] = ('X', 'Y', 'Z')

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        minimum_m: float = -1000.0,
    ) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(6)
        self._grid.setVerticalSpacing(4)
        outer.addWidget(self._grid_host)
        self.fields: dict[str, MetricSpinBox] = {}
        self._badges: dict[str, QLabel] = {}
        self._groups: list[QWidget] = []
        for axis in self.AXES:
            group = QWidget()
            row = QHBoxLayout(group)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            label = QLabel(axis)
            label.setFixedWidth(12)
            field = MetricSpinBox(minimum_m=minimum_m)
            badge = QLabel('混在')
            badge.setVisible(False)
            set_typography_role(badge, TypographyRole.SECONDARY)
            row.addWidget(label)
            row.addWidget(field, 1)
            row.addWidget(badge)
            self._groups.append(group)
            self.fields[axis] = field
            self._badges[axis] = badge
        self._relayout()
        self._unknown_label = QLabel('未定義')
        self._unknown_label.setVisible(False)
        set_typography_role(self._unknown_label, TypographyRole.SECONDARY)
        outer.addWidget(self._unknown_label)
        self._baseline_display: tuple[float, float, float] | None = None

    def minimumSizeHint(self) -> QSize:
        # The stacked arrangement is the floor — the wide row reflows away
        # under the inspector's dock width, so it must not inflate the hint.
        w = max(group.minimumSizeHint().width() for group in self._groups)
        h = sum(
            group.minimumSizeHint().height() for group in self._groups
        ) + self._grid.verticalSpacing() * (len(self._groups) - 1)
        return QSize(w, h + self.layout().spacing())

    def _wide_minimum(self) -> int:
        """Width at which the 3-in-a-row arrangement actually fits."""
        return sum(
            group.minimumSizeHint().width() for group in self._groups
        ) + self._grid.horizontalSpacing() * (len(self._groups) - 1)

    def _relayout(self) -> None:
        wide = self.width() >= self._wide_minimum()
        for index, group in enumerate(self._groups):
            if wide:
                self._grid.addWidget(group, 0, index)
            else:
                self._grid.addWidget(group, index, 0)

    def resizeEvent(self, event: object) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)  # type: ignore[arg-type]
        self._relayout()

    def set_known(self, known: bool, *, unknown_text: str = '未定義') -> None:
        """Toggle between editable axes and the explicit "unknown" state."""

        self._grid_host.setVisible(known)
        self._unknown_label.setText(unknown_text)
        self._unknown_label.setVisible(not known)
        if not known:
            self._baseline_display = None
            for badge in self._badges.values():
                badge.setVisible(False)

    def set_values_m(
        self,
        values: tuple[float, float, float],
        *,
        preserve: set[QWidget] | None = None,
    ) -> None:
        baseline = list(
            self._baseline_display
            if self._baseline_display is not None
            else (0.0, 0.0, 0.0)
        )
        for index, (axis, value) in enumerate(zip(self.AXES, values, strict=True)):
            field = self.fields[axis]
            if preserve and field in preserve:
                continue
            blocker = QSignalBlocker(field)
            try:
                field.set_value_m(value)
            finally:
                del blocker
            baseline[index] = field.value()
        self._baseline_display = tuple(baseline)

    def values_m(self) -> tuple[float, float, float]:
        return tuple(field.value_m() for field in self.fields.values())

    def edited_axes_m(self) -> dict[int, float]:
        """Axes the user changed since load, keyed 0/1/2 → SI metres."""

        if self._baseline_display is None:
            return {}
        edited: dict[int, float] = {}
        for index, field in enumerate(self.fields.values()):
            if abs(field.value() - self._baseline_display[index]) > 1e-9:
                edited[index] = field.value_m()
        return edited

    def merged_values_m(
        self,
        exact: tuple[float, float, float],
    ) -> tuple[float, float, float]:
        """Exact authority values where unedited, field values where edited.

        Same contract as ``SelectionInspector._edited_angles``: untouched axes
        contribute the exact stored value so committing one axis never snaps
        the others to the display rounding.
        """

        edited = self.edited_axes_m()
        return tuple(edited.get(index, exact[index]) for index in range(3))

    def set_mixed_axes(self, axes: set[int]) -> None:
        for index, axis in enumerate(self.AXES):
            self._badges[axis].setVisible(index in axes)

    def set_read_only(self, read_only: bool) -> None:
        for field in self.fields.values():
            field.setReadOnly(read_only)

    def set_display_unit(self, unit: str, *, decimals: int | None = None) -> None:
        for field in self.fields.values():
            field.set_display_unit(unit, decimals=decimals)
        if self._baseline_display is not None:
            self._baseline_display = tuple(
                field.value() for field in self.fields.values()
            )


class InspectorSection(QFrame):
    """One titled inspector group with an inline error line (#583).

    ``collapsible`` sections (Advanced) keep a toggle header; the error label
    stays hidden until ``show_error`` paints it, and clears on the next
    ``set_entity`` refresh.
    """

    def __init__(
        self,
        title: str,
        parent: QWidget | None = None,
        *,
        collapsible: bool = False,
        expanded: bool = True,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        if collapsible:
            self.header = QToolButton()
            self.header.setText(title)
            self.header.setCheckable(True)
            self.header.setChecked(expanded)
            self.header.setArrowType(
                Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
            )
            self.header.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            )
            self.header.toggled.connect(self._toggle_body)
        else:
            self.header = QLabel(title)
        set_typography_role(self.header, TypographyRole.SECTION_TITLE)
        layout.addWidget(self.header)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(6)
        layout.addWidget(self.body)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        error_palette = self.error_label.palette()
        error_palette.setColor(
            error_palette.ColorRole.WindowText,
            QColor(DARK_THEME.semantic.error.hex),
        )
        self.error_label.setPalette(error_palette)
        layout.addWidget(self.error_label)
        if collapsible:
            self.body.setVisible(expanded)

    def _toggle_body(self, checked: bool) -> None:
        self.body.setVisible(checked)
        if isinstance(self.header, QToolButton):
            self.header.setArrowType(
                Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
            )

    def show_error(self, message: str | None) -> None:
        self.error_label.setText(message or '')
        self.error_label.setVisible(bool(message))
        # Surface the error even inside a collapsed section.
        if message and isinstance(self.header, QToolButton):
            if not self.header.isChecked():
                self.header.setChecked(True)

    def clear_error(self) -> None:
        self.show_error(None)

    def resizeEvent(self, event: object) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._relayout_forms()

    def _relayout_forms(self) -> None:
        """Narrow sections wrap form labels above their fields (#583)."""

        wrap = (
            QFormLayout.RowWrapPolicy.WrapAllRows
            if self.width() < 300
            else QFormLayout.RowWrapPolicy.DontWrapRows
        )
        for index in range(self.body_layout.count()):
            layout = self.body_layout.itemAt(index).layout()
            if isinstance(layout, QFormLayout):
                layout.setRowWrapPolicy(wrap)


class SelectionInspector(QFrame):
    editCommitted = Signal()
    aimTargetRequested = Signal(str)
    aimClearRequested = Signal()
    alignCabinetRequested = Signal()
    meshImportRequested = Signal()

    KIND_LABELS = {
        "speaker": "スピーカー",
        "seat": "座席",
        "screen": "スクリーン",
        "display": "ディスプレイ",
        "projector": "プロジェクター",
        "riser": "ライザー",
        "furniture": "家具",
        "av_equipment": "AV機器",
        "measurement_point": "測定点",
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("roomSelectionInspector")
        self.setMinimumWidth(248)
        self.setMaximumWidth(320)
        set_surface_role(self, SurfaceRole.RAISED)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        self.title_label = QLabel("選択項目")
        set_typography_role(self.title_label, TypographyRole.SECTION_TITLE)
        layout.addWidget(self.title_label)
        self.empty_label = QLabel("3Dビューで項目を選択してください")
        self.empty_label.setWordWrap(True)
        set_typography_role(self.empty_label, TypographyRole.SECONDARY)
        layout.addWidget(self.empty_label)
        self.state_label = QLabel("")
        self.state_label.setWordWrap(True)
        set_typography_role(self.state_label, TypographyRole.SECONDARY)
        layout.addWidget(self.state_label)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        error_palette = self.error_label.palette()
        error_palette.setColor(
            error_palette.ColorRole.WindowText,
            QColor(DARK_THEME.semantic.error.hex),
        )
        self.error_label.setPalette(error_palette)
        layout.addWidget(self.error_label)

        self._entity: SceneEntity | None = None
        self._entity_id: str | None = None
        self._selection: tuple[SceneEntity, ...] = ()
        self._editable = False
        self._aim_targets: tuple[SceneEntity, ...] = ()
        self._baseline: dict[str, object] = {}
        self._dirty_widgets: set[QWidget] = set()

        form_host = QWidget()
        form_layout = QVBoxLayout(form_host)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(14)

        # -- 識別 (identity) ---------------------------------------------------
        self.identity_section = InspectorSection("識別")
        identity_form = QFormLayout()
        identity_form.setContentsMargins(0, 0, 0, 0)
        self.kind_label = QLabel("—")
        self.kind_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.name_field = QLineEdit()
        identity_form.addRow("種類", self.kind_label)
        identity_form.addRow("名前", self.name_field)
        self._hint(
            identity_form, self.kind_label,
            "物体の種類 · 追加したときに決まり、後から変更はできません",
        )
        self._hint(
            identity_form, self.name_field,
            "一覧・ラベル・差分表示に使う表示名 · 自由に変更できます",
        )
        self.identity_section.body_layout.addLayout(identity_form)
        form_layout.addWidget(self.identity_section)

        # -- 変換 (transform): position vector + physical orientation ----------
        self.transform_section = InspectorSection("変換")
        transform_form = QFormLayout()
        transform_form.setContentsMargins(0, 0, 0, 0)
        self.position_editor = Vector3Editor()
        self.position_fields = self.position_editor.fields
        transform_form.addRow("位置", self.position_editor)
        self._hint(
            transform_form, self.position_editor,
            "物体中心の部屋座標 · +X=部屋右、+Y=部屋奥（後方）、+Z=床から上 · "
            "表示単位は設定で変更できます",
        )
        # Numeric physical orientation (#470), backed by the exact persisted
        # quaternion; one field commit is one Undo transaction. #660: the
        # displayed angles use the same installation-facing vocabulary as the
        # acoustic-aim block so the same direction reads as the same numbers.
        self.orientation_header = QLabel("姿勢")
        set_typography_role(self.orientation_header, TypographyRole.SECTION_TITLE)
        self.orientation_header.setToolTip(
            "本体の正面（+Y）の向きを設置作業向けの角度（°）で正確に編集します。"
            "基準姿勢（全て 0°）では本体の正面は +Y（部屋後方）を向きます"
        )
        transform_form.addRow(self.orientation_header)
        self.orientation_labels: dict[str, QLabel] = {}
        self.orientation_fields: dict[str, QDoubleSpinBox] = {}
        orientation_tooltips = {
            "heading": (
                "本体正面の水平向き（°）· 0° = +Y（部屋後方）· 正値 = +X（部屋右）方向へ旋回· "
                "音響方向と同じ角度表現です"
            ),
            "elevation": "本体正面の仰角（°）· 正値 = 正面を+Z（上）へ傾けます",
            "twist": "正面軸まわりのねじれ（°）· 正面の向きは変わりません",
        }
        for axis in ("heading", "elevation", "twist"):
            label = QLabel({"heading": "水平向き", "elevation": "仰角", "twist": "ねじれ"}[axis])
            field = self._angle_field()
            if axis == "elevation":
                field.setRange(-90.0, 90.0)
            if axis == "heading":
                # Sentinel below the editable range shows "垂直（不定）" instead
                # of a fake 0° when the front axis is (near-)vertical.
                field.setMinimum(-999.0)
                field.setSpecialValueText("垂直（不定）")
            label.setToolTip(orientation_tooltips[axis])
            field.setToolTip(orientation_tooltips[axis])
            self.orientation_labels[axis] = label
            self.orientation_fields[axis] = field
            transform_form.addRow(label, field)
        self._orientation_widgets: tuple[QWidget, ...] = (
            self.orientation_header,
            *self.orientation_labels.values(),
            *self.orientation_fields.values(),
        )
        self.transform_section.body_layout.addLayout(transform_form)
        form_layout.addWidget(self.transform_section)

        # -- 形状 (geometry): size vector + body-shape parameters (#464) --------
        self.geometry_section = InspectorSection("形状")
        geometry_form = QFormLayout()
        geometry_form.setContentsMargins(0, 0, 0, 0)
        self.size_editor = Vector3Editor(minimum_m=0.001)
        self.size_fields = self.size_editor.fields
        geometry_form.addRow("寸法", self.size_editor)
        self._hint(
            geometry_form, self.size_editor,
            "物体の外形サイズ（X=幅・Y=奥行・Z=高さ、物体ローカル座標）",
        )
        self._shape_label = QLabel("形状")
        self.shape_field = QComboBox()
        for shape_kind, shape_label in BODY_SHAPE_ITEMS:
            self.shape_field.addItem(shape_label, userData=shape_kind)
        shape_tooltip = (
            "外形の表現方法 · 直方体（包絡）は寸法そのまま · 円柱は半径 · "
            "多角形はフットプリント · メッシュは外部3Dモデル"
        )
        self._shape_label.setToolTip(shape_tooltip)
        self.shape_field.setToolTip(shape_tooltip)
        geometry_form.addRow(self._shape_label, self.shape_field)
        self._radius_label = QLabel("半径")
        self._radius_label.setToolTip("円柱の底面半径（m）")
        self.radius_field = self._metric_field(minimum=0.001)
        self.radius_field.setToolTip("円柱の底面半径（m）")
        geometry_form.addRow(self._radius_label, self.radius_field)
        self._footprint_label = QLabel("フットプリント")
        self.footprint_field = QLineEdit()
        self.footprint_field.setPlaceholderText("x,y; x,y; …（物体ローカル m）")
        footprint_tooltip = (
            "物体ローカルXYの多角形頂点を「x,y; x,y; …」（m）で入力 · "
            "3頂点以上 · 入力した形が高さZ全体に押し出されます"
        )
        self._footprint_label.setToolTip(footprint_tooltip)
        self.footprint_field.setToolTip(footprint_tooltip)
        geometry_form.addRow(self._footprint_label, self.footprint_field)
        self._mesh_label = QLabel("メッシュ")
        self.mesh_summary = QLabel("未設定")
        self.mesh_summary.setWordWrap(True)
        set_typography_role(self.mesh_summary, TypographyRole.SECONDARY)
        self.mesh_button = QPushButton("メッシュを選択…")
        set_control_size(self.mesh_button, ControlSize.COMPACT)
        mesh_row = QWidget()
        mesh_row_layout = QHBoxLayout(mesh_row)
        mesh_row_layout.setContentsMargins(0, 0, 0, 0)
        mesh_row_layout.setSpacing(6)
        mesh_row_layout.addWidget(self.mesh_summary, 1)
        mesh_row_layout.addWidget(self.mesh_button)
        self.mesh_row = mesh_row
        mesh_tooltip = "外部3Dモデルを外形として割り当てます · 寸法値で拡大縮小されます"
        self._mesh_label.setToolTip(mesh_tooltip)
        self.mesh_button.setToolTip(mesh_tooltip)
        geometry_form.addRow(self._mesh_label, mesh_row)
        self._basis_label = QLabel("衝突・クリアランス")
        self.basis_value = QLabel("—")
        self.basis_value.setWordWrap(True)
        basis_tooltip = (
            "衝突判定・離隔制約の計算に使う外形 · 「実形状（厳密）」は定義した"
            "形状のまま、「包絡近似」は直方体として評価します"
        )
        self._basis_label.setToolTip(basis_tooltip)
        self.basis_value.setToolTip(basis_tooltip)
        set_typography_role(self.basis_value, TypographyRole.SECONDARY)
        geometry_form.addRow(self._basis_label, self.basis_value)
        self.geometry_section.body_layout.addLayout(geometry_form)
        form_layout.addWidget(self.geometry_section)

        # -- スピーカー ----------------------------------------------------------
        self.speaker_section = InspectorSection("スピーカー")
        speaker_form = QFormLayout()
        speaker_form.setContentsMargins(0, 0, 0, 0)
        self.role_field = QComboBox()
        self.role_field.setEditable(True)
        self.role_field.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.role_field.addItem(SPEAKER_ROLE_UNASSIGNED_LABEL)
        self.role_field.addItems(SPEAKER_ROLE_SUGGESTIONS)
        role_edit = self.role_field.lineEdit()
        if role_edit is not None:
            role_edit.setPlaceholderText("役割を選択または入力（例: FL / C / TFL）")
        speaker_form.addRow("役割", self.role_field)
        self._hint(
            speaker_form, self.role_field,
            "スピーカーのチャンネル役割（FL=前方左、C=センター、SL/SR=左右サラウンド、"
            "SUB=サブウーファー、T**=天井など）· 自由入力も可 · 配置評価・提案に使われます",
        )
        self.speaker_section.body_layout.addLayout(speaker_form)

        # Speaker-only acoustic aim block (#470): independent authority from the
        # cabinet pose. Unknown aim is never shown as a zero vector.
        self.aim_section = QWidget()
        aim_layout = QVBoxLayout(self.aim_section)
        aim_layout.setContentsMargins(0, 0, 0, 0)
        aim_layout.setSpacing(6)
        aim_header = QLabel("音響方向")
        set_typography_role(aim_header, TypographyRole.SECTION_TITLE)
        aim_header.setToolTip(
            "スピーカーの音響照準は本体の姿勢とは独立した権威です。"
            "本体を回転させても音響方向は変わりません"
        )
        aim_layout.addWidget(aim_header)
        self.aim_state_label = QLabel("—")
        self.aim_state_label.setWordWrap(True)
        set_typography_role(self.aim_state_label, TypographyRole.SECONDARY)
        aim_layout.addWidget(self.aim_state_label)

        target_row = QHBoxLayout()
        target_label = QLabel("対象")
        target_label.setToolTip("音響照準を向ける座席または測定点の音響基準点")
        target_row.addWidget(target_label)
        self.aim_target_combo = QComboBox()
        target_row.addWidget(self.aim_target_combo, 1)
        self.aim_apply_button = QPushButton("対象へ向ける")
        self.aim_apply_button.setToolTip(
            "選択した座席・測定点の音響基準点へ照準を設定します（本体は回転しません）"
        )
        self.aim_apply_button.clicked.connect(self._emit_aim_target)
        target_row.addWidget(self.aim_apply_button)
        aim_layout.addLayout(target_row)

        self.aim_known_host = QWidget()
        known_layout = QVBoxLayout(self.aim_known_host)
        known_layout.setContentsMargins(0, 0, 0, 0)
        known_layout.setSpacing(6)
        aim_form = QFormLayout()
        aim_form.setContentsMargins(0, 0, 0, 0)
        self.aim_yaw_field = self._angle_field()
        self.aim_yaw_field.setToolTip(
            "音響照準の水平角（°）· 0° = +Y（部屋後方）· 正値 = +X（部屋右）方向"
        )
        self.aim_pitch_field = self._angle_field(minimum=-90.0, maximum=90.0)
        self.aim_pitch_field.setToolTip("音響照準の仰角（°）· 正値 = +Z（上）方向")
        aim_form.addRow("水平向き", self.aim_yaw_field)
        aim_form.addRow("仰角", self.aim_pitch_field)
        known_layout.addLayout(aim_form)
        aim_actions = QHBoxLayout()
        self.aim_clear_button = QPushButton("未設定に戻す")
        self.aim_clear_button.setToolTip(
            "音響方向を消去して明示的な「未設定（不明）」に戻します"
        )
        self.aim_clear_button.clicked.connect(
            lambda checked=False: self.aimClearRequested.emit()
        )
        self.aim_align_button = QPushButton("本体を方向に合わせる")
        self.aim_align_button.setToolTip(
            "明示操作: キャビネット正面（+Y）を音響方向へ向けます。音響方向自体は変わりません"
        )
        self.aim_align_button.clicked.connect(
            lambda checked=False: self.alignCabinetRequested.emit()
        )
        aim_actions.addWidget(self.aim_clear_button)
        aim_actions.addWidget(self.aim_align_button)
        known_layout.addLayout(aim_actions)
        aim_layout.addWidget(self.aim_known_host)
        self.speaker_section.body_layout.addWidget(self.aim_section)
        form_layout.addWidget(self.speaker_section)

        # -- 詳細 (advanced): identifiers + internal orientation (collapsed) ----
        self.advanced_section = InspectorSection(
            "詳細", collapsible=True, expanded=False
        )
        advanced_form = QFormLayout()
        advanced_form.setContentsMargins(0, 0, 0, 0)
        self.id_label = QLabel("—")
        self.id_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.id_label.setToolTip("シーン内部で物体を一意に識別するID（読み取り専用）")
        set_typography_role(self.id_label, TypographyRole.SECONDARY)
        advanced_form.addRow("ID", self.id_label)
        self.orientation_detail = QLabel("—")
        self.orientation_detail.setWordWrap(True)
        set_typography_role(self.orientation_detail, TypographyRole.SECONDARY)
        self.orientation_detail.setToolTip(
            "内部表現（厳密な Z-Y-X Euler: Yaw/Pitch/Roll）· 読み取り専用"
        )
        advanced_form.addRow("内部姿勢", self.orientation_detail)
        self.advanced_section.body_layout.addLayout(advanced_form)
        form_layout.addWidget(self.advanced_section)

        self._sections: dict[str, InspectorSection] = {
            'identity': self.identity_section,
            'transform': self.transform_section,
            'geometry': self.geometry_section,
            'speaker': self.speaker_section,
            'advanced': self.advanced_section,
        }

        self.shape_field.activated.connect(lambda _index=-1: self._shape_activated())
        self.radius_field.editingFinished.connect(self.editCommitted.emit)
        self.footprint_field.editingFinished.connect(self.editCommitted.emit)
        self.mesh_button.clicked.connect(
            lambda checked=False: self.meshImportRequested.emit()
        )
        self.name_field.editingFinished.connect(self.editCommitted.emit)
        self.role_field.activated.connect(
            lambda _index=-1: self.editCommitted.emit()
        )
        if self.role_field.lineEdit() is not None:
            self.role_field.lineEdit().editingFinished.connect(self.editCommitted.emit)
        for field in (
            *self.position_fields.values(),
            *self.size_fields.values(),
            *self.orientation_fields.values(),
            self.aim_yaw_field,
            self.aim_pitch_field,
        ):
            field.editingFinished.connect(self.editCommitted.emit)

        # In-progress edits mark their field so uncommitted input is visible
        # and preserved across same-entity refreshes (#583).
        self.name_field.textChanged.connect(
            lambda _text: self._mark_dirty(self.name_field)
        )
        self.footprint_field.textChanged.connect(
            lambda _text: self._mark_dirty(self.footprint_field)
        )
        for field in (
            *self.position_fields.values(),
            *self.size_fields.values(),
            self.radius_field,
            *self.orientation_fields.values(),
            self.aim_yaw_field,
            self.aim_pitch_field,
        ):
            field.valueChanged.connect(
                lambda _value, widget=field: self._mark_dirty(widget)
            )
            # In-flight text (typed but not yet interpreted) is also an
            # in-progress edit — mark it so same-entity refreshes never
            # reset the field's visible input (#583, Ctrl+S flush order).
            line_edit = field.lineEdit()
            if line_edit is not None:
                line_edit.textChanged.connect(
                    lambda _text, widget=field: self._mark_dirty(
                        widget, restyle=False
                    )
                )

        layout.addWidget(form_host)
        layout.addStretch(1)
        self.form_host = form_host
        self.set_entity(None, editable=False)

    @staticmethod
    def _hint(form: QFormLayout, field: QWidget, text: str) -> None:
        """Attach an explanation to a field and its auto-created row label."""
        field.setToolTip(text)
        label = form.labelForField(field)
        if label is not None:
            label.setToolTip(text)

    @staticmethod
    def _metric_field(*, minimum: float = -1000.0) -> MetricSpinBox:
        return MetricSpinBox(minimum_m=minimum)

    @staticmethod
    def _angle_field(
        *,
        minimum: float = -180.0,
        maximum: float = 180.0,
    ) -> QDoubleSpinBox:
        field = PendingTextSpinBox()
        field.setRange(minimum, maximum)
        field.setDecimals(3)
        field.setSingleStep(1.0)
        field.setSuffix("°")
        field.setKeyboardTracking(False)
        return field

    def _sync_shape_visibility(self, shape_kind: str | None) -> None:
        physical = self._entity is not None and self._entity.size_m is not None
        show = physical and shape_kind is not None
        self._shape_label.setVisible(physical)
        self.shape_field.setVisible(physical)
        self._radius_label.setVisible(bool(show and shape_kind == "cylinder"))
        self.radius_field.setVisible(bool(show and shape_kind == "cylinder"))
        self._footprint_label.setVisible(bool(show and shape_kind == "extruded_polygon"))
        self.footprint_field.setVisible(bool(show and shape_kind == "extruded_polygon"))
        self._mesh_label.setVisible(bool(show and shape_kind == "mesh_asset"))
        self.mesh_row.setVisible(bool(show and shape_kind == "mesh_asset"))
        self._basis_label.setVisible(physical)
        self.basis_value.setVisible(physical)

    def _refresh_basis_label(self) -> None:
        """Show whether clearance/collision consumes the exact authored body.

        The label follows the currently selected shape so users see the basis
        before committing; invalid parameter text degrades to "—".
        """

        entity = self._entity
        if entity is None or entity.size_m is None:
            self.basis_value.setText("—")
            return
        try:
            _name, _pos, _size, _role, _orientation, _aim, body = self.values(entity)
        except ValueError:
            self.basis_value.setText("—")
            return
        candidate = entity.model_copy(update={"body_geometry": body})
        authority = entity_collision_geometry_authority(candidate)
        self.basis_value.setText(
            "実形状（厳密）" if authority == "exact_body_geometry" else "包絡近似"
        )

    def _shape_activated(self) -> None:
        self._sync_shape_visibility(self.shape_field.currentData())
        self._refresh_basis_label()
        self.editCommitted.emit()

    def set_entity(
        self,
        entity: SceneEntity | None,
        *,
        editable: bool,
        aim_targets: tuple[SceneEntity, ...] = (),
        selection: tuple[SceneEntity, ...] = (),
        preserve_dirty: bool | None = None,
    ) -> None:
        previous_id = self._entity_id
        self._entity = entity
        self._selection = selection
        self._editable = editable
        self._aim_targets = aim_targets
        self._entity_id = entity.entity_id if entity is not None else None
        self.clear_errors()
        self.empty_label.setVisible(entity is None)
        self.form_host.setVisible(entity is not None)
        if entity is None:
            self.title_label.setText("選択項目")
            self.state_label.setText("")
            self.state_label.setVisible(False)
            self._baseline = {}
            self._dirty_widgets.clear()
            return
        if preserve_dirty is None:
            # A refresh of the same entity keeps fields the user is still
            # editing; switching entities always reloads (#583).
            preserve_dirty = self._entity_id == previous_id
        if not preserve_dirty:
            self._clear_all_dirty()

        kind_text = self.KIND_LABELS.get(entity.kind, entity.kind)
        self.title_label.setText(entity.name or kind_text)
        state_bits = [kind_text]
        if len(selection) > 1:
            state_bits.append(f"{len(selection)}項目選択中 · 編集は全項目へ適用")
        if not editable:
            state_bits.append("読み取り専用")
        self.state_label.setText(" · ".join(state_bits))
        self.state_label.setVisible(True)

        role_edit = self.role_field.lineEdit()
        blockers = [
            QSignalBlocker(self.name_field),
            QSignalBlocker(self.role_field),
            QSignalBlocker(self.shape_field),
            QSignalBlocker(self.radius_field),
            QSignalBlocker(self.footprint_field),
            *(QSignalBlocker(field) for field in self.position_fields.values()),
            *(QSignalBlocker(field) for field in self.size_fields.values()),
            *(QSignalBlocker(field) for field in self.orientation_fields.values()),
            QSignalBlocker(self.aim_yaw_field),
            QSignalBlocker(self.aim_pitch_field),
            QSignalBlocker(self.aim_target_combo),
            *([QSignalBlocker(role_edit)] if role_edit is not None else []),
        ]
        try:
            self.kind_label.setText(kind_text)
            if self.name_field not in self._dirty_widgets:
                self.name_field.setText(entity.name)
            role = entity.speaker_role or ""
            if is_unassigned_speaker_role(role):
                self.role_field.setCurrentIndex(0)
            else:
                index = self.role_field.findText(role)
                if index >= 0:
                    self.role_field.setCurrentIndex(index)
                else:
                    self.role_field.setCurrentIndex(-1)
                    self.role_field.setEditText(role)
            self.name_field.setReadOnly(not editable)
            self.role_field.setEnabled(editable)

            # Transforms — grouped X/Y/Z vector editors. Per-axis mixed badges
            # mark values that differ across a multi-selection.
            self.position_editor.set_values_m(
                (entity.position.x_m, entity.position.y_m, entity.position.z_m),
                preserve=self._dirty_widgets,
            )
            self.position_editor.set_mixed_axes(
                self._mixed_axes(
                    selection,
                    lambda item: (
                        item.position.x_m, item.position.y_m, item.position.z_m
                    ),
                )
            )
            self.position_editor.set_read_only(not editable)
            if entity.size_m is None:
                self.size_editor.set_known(False)
            else:
                self.size_editor.set_known(True)
                self.size_editor.set_values_m(
                    (entity.size_m.x_m, entity.size_m.y_m, entity.size_m.z_m),
                    preserve=self._dirty_widgets,
                )
                self.size_editor.set_mixed_axes(
                    self._mixed_axes(
                        selection,
                        lambda item: (
                            None
                            if item.size_m is None
                            else (
                                item.size_m.x_m,
                                item.size_m.y_m,
                                item.size_m.z_m,
                            )
                        ),
                    )
                )
            self.size_editor.set_read_only(not editable)

            # Heading/elevation/twist rows exist only for physical bodies; a
            # measurement point is a reference position without a pose to author.
            physical = entity.kind in PHYSICAL_ENTITY_KINDS
            for widget in self._orientation_widgets:
                widget.setVisible(physical)
            self.orientation_detail.setVisible(physical)
            if physical:
                angles = body_view_angles(entity.orientation)
                heading_field = self.orientation_fields["heading"]
                heading_display = (
                    angles.heading_deg
                    if angles.heading_deg is not None
                    else heading_field.minimum()
                )
                for axis, value in (
                    ("heading", heading_display),
                    ("elevation", angles.elevation_deg),
                    ("twist", angles.twist_deg),
                ):
                    field = self.orientation_fields[axis]
                    if field not in self._dirty_widgets:
                        field.setValue(value)
                for field in self.orientation_fields.values():
                    field.setReadOnly(not editable)
                self._baseline["orientation"] = (
                    heading_display,
                    angles.elevation_deg,
                    angles.twist_deg,
                )
                yaw, pitch, roll = quaternion_to_euler_deg(entity.orientation)
                self.orientation_detail.setText(
                    f"内部 Euler · ヨー {yaw:.4f}° · ピッチ {pitch:.4f}° · ロール {roll:.4f}°"
                )
            self.speaker_section.setVisible(entity.kind == "speaker")
            if entity.kind == "speaker":
                self._set_aim_state(entity, editable=editable, aim_targets=aim_targets)
                self._baseline["aim"] = (
                    self.aim_yaw_field.value(),
                    self.aim_pitch_field.value(),
                )

            # Body geometry authoring (Issue #464): ``size_m`` stays the
            # bounding envelope; shape fields edit the refined body.
            self.geometry_section.setVisible(entity.size_m is not None)
            body = entity.body_geometry
            body_kind = body.kind if body is not None else "box"
            index = self.shape_field.findData(body_kind)
            self.shape_field.setCurrentIndex(index if index >= 0 else 0)
            self.shape_field.setEnabled(editable)
            radius_default = (
                min(entity.size_m.x_m, entity.size_m.y_m) * 0.5
                if entity.size_m is not None
                else 0.5
            )
            if body is not None and body.radius_m is not None:
                radius_default = float(body.radius_m)
            if self.radius_field not in self._dirty_widgets:
                self.radius_field.set_value_m(radius_default)
            if self.footprint_field not in self._dirty_widgets:
                if body is not None and body.footprint_vertices:
                    self.footprint_field.setText(
                        format_footprint_vertices(body.footprint_vertices)
                    )
                elif entity.size_m is not None:
                    half_x = entity.size_m.x_m * 0.5
                    half_y = entity.size_m.y_m * 0.5
                    self.footprint_field.setText(format_footprint_vertices((
                        FootprintVertex(x_m=-half_x, y_m=-half_y),
                        FootprintVertex(x_m=half_x, y_m=-half_y),
                        FootprintVertex(x_m=half_x, y_m=half_y),
                        FootprintVertex(x_m=-half_x, y_m=half_y),
                    )))
                else:
                    self.footprint_field.clear()
            self.radius_field.setReadOnly(not editable)
            self.footprint_field.setReadOnly(not editable)
            if body is not None and body.mesh is not None:
                self.mesh_summary.setText(
                    f"{body.mesh.source_name} · {body.mesh.asset_format} · "
                    f"{len(body.mesh.vertices)}頂点/{len(body.mesh.triangles)}面 · "
                    f"sha256:{body.mesh.asset_sha256[:12]}…"
                )
            else:
                self.mesh_summary.setText("未設定")
            self.mesh_button.setEnabled(editable and entity.size_m is not None)
            self._sync_shape_visibility(body_kind)
            self._refresh_basis_label()

            self.id_label.setText(entity.entity_id)

            self._baseline.update(
                {
                    "name": entity.name,
                    "role": self.role_field.currentText(),
                    "shape": body_kind,
                    "radius": self.radius_field.value_m(),
                    "footprint": self.footprint_field.text(),
                }
            )
            # Fields whose value again matches the freshly loaded baseline are
            # no longer dirty (e.g. an edit that just committed successfully).
            for widget in tuple(self._dirty_widgets):
                if self._widget_matches_baseline(widget):
                    self._clear_widget_dirty(widget)
        finally:
            del blockers

    # -- presentation state -----------------------------------------------------

    def _mark_dirty(self, widget: QWidget, *, restyle: bool = True) -> None:
        self._dirty_widgets.add(widget)
        widget.setProperty("inspectorDirty", True)
        # polish() on a QAbstractSpinBox resets the line text to
        # textFromValue — never restyle while it holds un-interpreted input.
        if restyle:
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def _clear_widget_dirty(self, widget: QWidget) -> None:
        self._dirty_widgets.discard(widget)
        widget.setProperty("inspectorDirty", False)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _clear_all_dirty(self) -> None:
        for widget in tuple(self._dirty_widgets):
            self._clear_widget_dirty(widget)

    def _widget_matches_baseline(self, widget: QWidget) -> bool:
        if widget is self.name_field:
            return self.name_field.text() == self._baseline.get("name", "")
        if widget is self.footprint_field:
            return self.footprint_field.text() == self._baseline.get(
                "footprint", ""
            )
        if widget is self.radius_field:
            return abs(
                self.radius_field.value_m() - float(self._baseline.get("radius", 0.0))
            ) <= 1e-9
        for editor, key in (
            (self.position_editor, "position"),
            (self.size_editor, "size"),
        ):
            for axis, field in editor.fields.items():
                if widget is field:
                    baseline = editor._baseline_display
                    if baseline is None:
                        return True
                    index = editor.AXES.index(axis)
                    return abs(field.value() - baseline[index]) <= 1e-9
        orientation_baseline = self._baseline.get("orientation")
        for index, field in enumerate(self.orientation_fields.values()):
            if widget is field:
                if orientation_baseline is None:
                    return True
                return abs(field.value() - orientation_baseline[index]) <= 1e-9
        aim_baseline = self._baseline.get("aim")
        for index, field in enumerate(
            (self.aim_yaw_field, self.aim_pitch_field)
        ):
            if widget is field:
                if aim_baseline is None:
                    return True
                return abs(field.value() - aim_baseline[index]) <= 1e-9
        return False

    @staticmethod
    def _mixed_axes(
        entities: tuple[SceneEntity, ...],
        values_of: Callable[[SceneEntity], tuple[float, ...] | None],
    ) -> set[int]:
        """Axis indices whose values differ across the selection (#583)."""

        rows = [values_of(entity) for entity in entities]
        if len(rows) < 2:
            return set()
        mixed: set[int] = set()
        for index in range(3):
            values = [None if row is None else row[index] for row in rows]
            first = values[0]
            if any(
                (value is None) != (first is None)
                or (
                    value is not None
                    and first is not None
                    and abs(value - first) > 1e-6
                )
                for value in values[1:]
            ):
                mixed.add(index)
        return mixed

    def show_error(self, section: str | None, message: str) -> None:
        """Surface a rejected commit near the field that caused it (#583)."""

        if section and section in self._sections:
            self._sections[section].show_error(message)
        else:
            self.error_label.setText(message)
            self.error_label.setVisible(True)

    def clear_errors(self) -> None:
        self.error_label.setText("")
        self.error_label.setVisible(False)
        for section in self._sections.values():
            section.clear_error()

    def reset_current_entity(self) -> None:
        """Restore exact authority values after a rejected commit (#583)."""

        self._clear_all_dirty()
        self.set_entity(
            self._entity,
            editable=self._editable,
            aim_targets=self._aim_targets,
            selection=self._selection,
            preserve_dirty=False,
        )

    def set_display_units(self, *, length_unit: str, precision: int = 3) -> None:
        """Apply #496 display units to every length field (storage stays SI)."""

        for editor in (self.position_editor, self.size_editor):
            editor.set_display_unit(length_unit, decimals=precision)
        self.radius_field.set_display_unit(length_unit, decimals=precision)

    def _set_aim_state(
        self,
        entity: SceneEntity,
        *,
        editable: bool,
        aim_targets: tuple[SceneEntity, ...],
    ) -> None:
        previous_target = self.aim_target_combo.currentData()
        self.aim_target_combo.clear()
        for target in aim_targets:
            label = f"{target.name} · {self.KIND_LABELS.get(target.kind, target.kind)}"
            self.aim_target_combo.addItem(label, target.entity_id)
        if previous_target is not None:
            index = self.aim_target_combo.findData(previous_target)
            if index >= 0:
                self.aim_target_combo.setCurrentIndex(index)
        has_target = self.aim_target_combo.count() > 0
        self.aim_target_combo.setEnabled(editable and has_target)
        self.aim_apply_button.setEnabled(editable and has_target)

        aim = entity.aim_xyz
        self.aim_known_host.setVisible(aim is not None)
        if aim is None:
            self.aim_state_label.setText(
                "未設定（不明）· 座席・測定点へ向けると確定します"
            )
            return
        yaw_deg, pitch_deg = aim_yaw_pitch_deg(aim)
        delta = forward_aim_delta_deg(entity)
        alignment = (
            "正面との一致"
            if delta is not None and delta < 0.5
            else f"正面との差 {delta:.3f}°"
            if delta is not None
            else ""
        )
        detail = f"{alignment} · " if alignment else ""
        self.aim_state_label.setText(
            f"既知 · {detail}水平向き {yaw_deg:.3f}° · 仰角 {pitch_deg:.3f}°\n"
            f"方向 ({aim.x:.4f}, {aim.y:.4f}, {aim.z:.4f})"
        )
        self.aim_yaw_field.setValue(yaw_deg)
        self.aim_pitch_field.setValue(pitch_deg)
        self.aim_yaw_field.setEnabled(editable)
        self.aim_pitch_field.setEnabled(editable)
        self.aim_clear_button.setEnabled(editable)
        self.aim_align_button.setEnabled(editable)

    def _emit_aim_target(self, checked: bool = False) -> None:
        del checked
        target = self.aim_target_combo.currentData()
        if target is not None:
            self.aimTargetRequested.emit(str(target))

    @staticmethod
    def _edited_angles(
        fields: tuple[QDoubleSpinBox, ...],
        exact: tuple[float, ...],
    ) -> tuple[float, ...] | None:
        """Return edited angle values, or None when the user changed nothing.

        Untouched fields contribute their exact authority values instead of the
        display-rounded spin value, so editing one axis never snaps the others.
        """

        edited: list[float] = []
        changed = False
        for field, component in zip(fields, exact, strict=True):
            value = field.value()
            if abs(value - round(component, field.decimals())) > 1e-9:
                changed = True
                edited.append(value)
            else:
                edited.append(component)
        return tuple(edited) if changed else None

    def _edited_orientation(self, entity: SceneEntity) -> Quaternion4 | None:
        if entity.kind not in PHYSICAL_ENTITY_KINDS:
            return None
        exact = body_view_angles(entity.orientation)
        heading_field = self.orientation_fields["heading"]
        heading_sentinel = heading_field.minimum()
        heading_text = heading_field.value()
        if heading_text <= heading_sentinel + 1e-9:
            edited_heading: float | None = None
            heading_changed = exact.heading_deg is not None
        elif exact.heading_deg is None or abs(
            heading_text - round(exact.heading_deg, heading_field.decimals())
        ) > 1e-9:
            edited_heading = heading_text
            heading_changed = True
        else:
            edited_heading = exact.heading_deg
            heading_changed = False
        edited_elevation = self._edited_angles(
            (self.orientation_fields["elevation"],), (exact.elevation_deg,)
        )
        edited_twist = self._edited_angles(
            (self.orientation_fields["twist"],), (exact.twist_deg,)
        )
        elevation_changed = edited_elevation is not None
        twist_changed = edited_twist is not None
        if not (heading_changed or elevation_changed or twist_changed):
            return None
        heading = edited_heading
        elevation = (
            edited_elevation[0] if edited_elevation is not None else exact.elevation_deg
        )
        twist = edited_twist[0] if edited_twist is not None else exact.twist_deg
        if heading is None and abs(elevation) < 90.0 - 1e-6:
            # The user cleared a horizontal heading on a non-vertical pose.
            raise InspectorValidationError(
                'transform',
                "正面が水平方向を向く姿勢では水平向きを指定してください（垂直時のみ不定可）",
            )
        return orientation_from_view_angles(
            heading_deg=heading,
            elevation_deg=elevation,
            twist_deg=twist,
            fallback_heading_deg=exact.heading_deg,
        )

    def _edited_aim_angles(self, entity: SceneEntity) -> tuple[float, float] | None:
        if entity.kind != "speaker" or entity.aim_xyz is None:
            return None
        exact = aim_yaw_pitch_deg(entity.aim_xyz)
        return self._edited_angles(
            (self.aim_yaw_field, self.aim_pitch_field),
            exact,
        )

    def _body_geometry_from_fields(
        self,
        entity: SceneEntity,
    ) -> EntityBodyGeometry | None:
        """Current shape fields as body geometry (``box`` → ``None``).

        Raises ``InspectorValidationError`` tagged to the geometry section on
        malformed input so the workspace can paint the error next to the
        offending field (#583).
        """

        shape = str(self.shape_field.currentData() or "box")
        if shape == "cylinder":
            return EntityBodyGeometry(
                kind="cylinder",
                radius_m=self.radius_field.value_m(),
            )
        if shape == "extruded_polygon":
            try:
                vertices = parse_footprint_vertices(self.footprint_field.text())
            except ValueError as exc:
                raise InspectorValidationError('geometry', str(exc)) from exc
            return EntityBodyGeometry(
                kind="extruded_polygon",
                footprint_vertices=vertices,
            )
        if shape == "mesh_asset":
            existing = entity.body_geometry
            if existing is not None and existing.kind == "mesh_asset":
                return existing
            raise InspectorValidationError(
                'geometry',
                "メッシュボディは「メッシュを選択…」からインポートしてください",
            )
        # "box" downgrades to no explicit body geometry (legacy envelope).
        return None

    def _body_geometry_edited(self, entity: SceneEntity) -> object:
        """``_UNSET`` when the shape fields still match the loaded baseline."""

        if entity.size_m is None:
            return _UNSET
        shape = str(self.shape_field.currentData() or "box")
        if shape != self._baseline.get("shape"):
            return True
        if shape == "cylinder" and abs(
            self.radius_field.value_m() - float(self._baseline.get("radius", 0.0))
        ) > 1e-9:
            return True
        if shape == "extruded_polygon" and self.footprint_field.text() != self._baseline.get(
            "footprint", ""
        ):
            return True
        if shape == "mesh_asset" and entity.body_geometry is None:
            return True
        return _UNSET

    def edited_values(self, entity: SceneEntity) -> dict[str, object]:
        """Only the properties the user changed since load (#583 batch commit).

        Vector axes come back per-axis (``position_axes``/``size_axes``) so a
        multi-entity commit edits one axis without flattening the others;
        orientation and aim stay whole-property since they share a pose unit.
        """

        edited: dict[str, object] = {}
        if self.name_field.text() != self._baseline.get("name", entity.name):
            edited["name"] = self.name_field.text()
        position_axes = self.position_editor.edited_axes_m()
        if position_axes:
            edited["position_axes"] = position_axes
        if entity.size_m is not None:
            size_axes = self.size_editor.edited_axes_m()
            if size_axes:
                edited["size_axes"] = size_axes
        if entity.kind == "speaker":
            if self.role_field.currentText() != self._baseline.get("role"):
                text = self.role_field.currentText().strip()
                edited["role"] = (
                    "" if not text or text == SPEAKER_ROLE_UNASSIGNED_LABEL else text
                )
            aim = self._edited_aim_angles(entity)
            if aim is not None:
                edited["aim_yaw_pitch_deg"] = aim
        if entity.kind in PHYSICAL_ENTITY_KINDS:
            orientation = self._edited_orientation(entity)
            if orientation is not None:
                edited["orientation"] = orientation
        if self._body_geometry_edited(entity) is not _UNSET:
            edited["body_geometry"] = self._body_geometry_from_fields(entity)
        return edited

    def values(
        self,
        entity: SceneEntity,
    ) -> tuple[
        str,
        Position3,
        Size3 | None,
        str | None,
        Quaternion4 | None,
        tuple[float, float] | None,
        EntityBodyGeometry | None,
    ]:
        # Unedited axes contribute their exact authority values — committing
        # one axis never snaps the others to the display rounding (#583).
        px, py, pz = self.position_editor.merged_values_m(
            (entity.position.x_m, entity.position.y_m, entity.position.z_m)
        )
        position = Position3(x_m=px, y_m=py, z_m=pz)
        size = None
        if entity.size_m is not None:
            sx, sy, sz = self.size_editor.merged_values_m(
                (entity.size_m.x_m, entity.size_m.y_m, entity.size_m.z_m)
            )
            size = Size3(x_m=sx, y_m=sy, z_m=sz)
        role = None
        if entity.kind == "speaker":
            text = self.role_field.currentText().strip()
            role = "" if not text or text == SPEAKER_ROLE_UNASSIGNED_LABEL else text
        body_geometry: EntityBodyGeometry | None = entity.body_geometry
        if entity.size_m is not None and self._body_geometry_edited(entity) is not _UNSET:
            body_geometry = self._body_geometry_from_fields(entity)
        return (
            self.name_field.text(),
            position,
            size,
            role,
            self._edited_orientation(entity),
            self._edited_aim_angles(entity),
            body_geometry,
        )


class ContextToolStrip(QFrame):
    toolRequested = Signal(str)

    DEFINITIONS = {
        "geometry": (("draw-room", "部屋を描く"), ("edit-room", "形状を編集")),
        "objects": (
            ("show-palette", "オブジェクト追加"),
            ("measure", "計測"),
            ("delete-selection", "選択を削除"),
            ("view-menu", "ビュー"),
        ),
        "placement": (
            ("focus-selection", "選択へ移動"),
            ("measure", "計測"),
            ("fit-scene", "全体表示"),
            ("view-menu", "ビュー"),
        ),
        "acoustics": (("toggle-acoustics", "音響表示"), ("fit-scene", "全体表示")),
        "history": (("fit-scene", "全体表示"),),
    }

    ACTION_HINTS = {
        "draw-room": "部屋の外形を3D上でクリックして描き始めます（Escで中止）",
        "edit-room": "既存の部屋の頂点・辺・壁・開口を編集モードで調整します",
        "show-palette": "物体追加パレットを開きます · 種類を選ぶと3D上でクリック配置できます",
        "measure": "3D上で2点間の距離などを計ります（結果は保存されません）",
        "delete-selection": "選択中の物体を削除します（Ctrl+Zで元に戻せます）",
        "view-menu": "表示設定（グリッド・吸着・カメラ操作）を開きます",
        "focus-selection": "選択した物体にカメラを合わせます",
        "fit-scene": "部屋全体が見えるようカメラを調整します",
        "toggle-acoustics": "音響予測結果の3Dオーバーレイ表示を切り替えます",
    }

    GUIDANCE = {
        "geometry": (
            "このタブで部屋の形を作ります · 「部屋を描く」で外形をクリック入力し、"
            "「形状を編集」で頂点・壁・開口を調整 → つぎは「物体」タブで機器を配置"
        ),
        "objects": (
            "物体を置くタブです · 左の「追加」から種類を選んで3D上でクリック配置。"
            "選ぶと右パネルで位置・寸法・向きを編集 → つぎは「スピーカー・座席」タブへ"
        ),
        "placement": (
            "スピーカーと座席の位置・向きを整えるタブです · 3D上でドラッグ、"
            "または右パネルの値を直接編集。配置制約と自動提案もここで扱います → つぎは「音響」タブへ"
        ),
        "acoustics": (
            "壁材・吸音処理と音響予測のタブです · 右パネルで材質と予測条件を設定して実行。"
            "保存した版の確認は「履歴」タブで"
        ),
        "history": (
            "保存した版の履歴です · リビジョンを選ぶと差分・3Dプレビュー・復元ができます"
        ),
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        set_surface_role(self, SurfaceRole.RAISED)
        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {}
        for context_id in ROOM_CONTEXT_IDS:
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(0, 0, 0, 0)
            page_layout.setSpacing(2)
            row = QHBoxLayout()
            row.setSpacing(6)
            for tool_id, label in self.DEFINITIONS[context_id]:
                button = QPushButton(label)
                button.setProperty("actionId", tool_id)
                button.setToolTip(self.ACTION_HINTS.get(tool_id, ""))
                set_control_size(button, ControlSize.COMPACT)
                button.clicked.connect(
                    lambda checked=False, target=tool_id: self.toolRequested.emit(target)
                )
                row.addWidget(button)
            row.addStretch(1)
            page_layout.addLayout(row)
            guidance = QLabel(self.GUIDANCE[context_id])
            guidance.setWordWrap(True)
            set_typography_role(guidance, TypographyRole.SECONDARY)
            page_layout.addWidget(guidance)
            self.pages[context_id] = page
            self.stack.addWidget(page)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.addWidget(self.stack)
        self.set_context("geometry")

    def set_context(self, context_id: str) -> None:
        if context_id not in self.pages:
            raise ValueError(f"unknown Room context: {context_id}")
        self.stack.setCurrentWidget(self.pages[context_id])


class OverlayControls(QFrame):
    changed = Signal()
    snapChanged = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        set_surface_role(self, SurfaceRole.OVERLAY)
        self._compact = False
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(10, 6, 10, 6)
        self._layout.setSpacing(10)
        self.grid = QCheckBox("グリッド")
        self.grid.setToolTip("床面に寸法目盛のグリッドを表示します")
        self.labels = QCheckBox("ラベル")
        self.labels.setToolTip("物体名・寸法線などのラベルを3D上に表示します")
        self.acoustics = QCheckBox("音響")
        self.acoustics.setToolTip("音響予測結果（音圧分布など）を3D上に重ねて表示します")
        self.focus = QCheckBox("選択に集中")
        self.focus.setToolTip("選択中の物体以外を薄く表示し、編集対象に集中しやすくします")
        self.grid.setChecked(True)
        for toggle in (self.grid, self.labels, self.acoustics, self.focus):
            toggle.toggled.connect(lambda checked=False: self.changed.emit())
            self._layout.addWidget(toggle)

        # Precision snapping (#481): object snap is on by default; grid and
        # angle snap are opt-in, with editable step sizes under the 表示… menu.
        self.object_snap = QCheckBox("吸着")
        self.object_snap.setChecked(True)
        self.object_snap.setToolTip(
            "ドラッグ中に頂点・辺・中点・整列へ吸着します（Shiftで一時解除）"
        )
        self.object_snap.toggled.connect(lambda checked=False: self.snapChanged.emit())
        self._layout.addWidget(self.object_snap)

        self.navigation_hint = QLabel(
            "操作: 中ボタン=画面移動 / Shift+中ボタン=回転 / "
            "ホイール=拡大縮小 / 右クリック=メニュー"
        )
        self.navigation_hint.setObjectName("cadNavigationHint")
        self.navigation_hint.setWordWrap(True)
        self.navigation_hint.setToolTip(
            "中ボタン: 画面移動\n"
            "Shift + 中ボタン: 視点回転\n"
            "ホイール: 拡大縮小\n"
            "右クリック: 操作メニュー"
        )
        self.navigation_hint.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        set_typography_role(self.navigation_hint, TypographyRole.SECONDARY)
        self._layout.addWidget(self.navigation_hint, 1)

        self.more_button = QPushButton("表示…")
        self.more_button.setToolTip(
            "表示オーバーレイと吸着（スナップ）の詳細設定を開きます"
        )
        set_control_size(self.more_button, ControlSize.COMPACT)
        self.more_menu = QMenu(self.more_button)
        self.labels_action = self.more_menu.addAction("ラベル")
        self.labels_action.setCheckable(True)
        self.labels_action.setToolTip("物体名・寸法線などのラベルを3D上に表示します")
        self.focus_action = self.more_menu.addAction("選択に集中")
        self.focus_action.setCheckable(True)
        self.focus_action.setToolTip("選択中の物体以外を薄く表示します")
        self.labels_action.toggled.connect(self.labels.setChecked)
        self.focus_action.toggled.connect(self.focus.setChecked)
        self.labels.toggled.connect(self.labels_action.setChecked)
        self.focus.toggled.connect(self.focus_action.setChecked)
        self.more_menu.addSeparator()
        self.grid_snap_action = self.more_menu.addAction("グリッド吸着")
        self.grid_snap_action.setCheckable(True)
        self.grid_snap_action.setToolTip(
            "ドラッグ中に位置を下の「グリッド刻み」単位に丸めます"
        )
        self.angle_snap_action = self.more_menu.addAction("角度吸着")
        self.angle_snap_action.setCheckable(True)
        self.angle_snap_action.setToolTip(
            "回転中に向きを下の「角度刻み」単位に丸めます"
        )
        self.grid_snap_action.toggled.connect(
            lambda checked=False: self.snapChanged.emit()
        )
        self.angle_snap_action.toggled.connect(
            lambda checked=False: self.snapChanged.emit()
        )
        self.more_menu.addSeparator()
        grid_step_host = QWidget()
        grid_step_row = QHBoxLayout(grid_step_host)
        grid_step_row.setContentsMargins(8, 2, 8, 2)
        grid_step_row.addWidget(QLabel("グリッド刻み"))
        self.grid_step_spin = QDoubleSpinBox()
        self.grid_step_spin.setRange(0.01, 1.0)
        self.grid_step_spin.setSingleStep(0.01)
        self.grid_step_spin.setDecimals(3)
        self.grid_step_spin.setSuffix(" m")
        self.grid_step_spin.setToolTip("グリッド吸着の刻み幅（m）")
        self.grid_step_spin.setValue(0.05)
        self.grid_step_spin.valueChanged.connect(
            lambda _v: self.snapChanged.emit()
        )
        grid_step_row.addWidget(self.grid_step_spin)
        grid_step_action = QWidgetAction(self.more_menu)
        grid_step_action.setDefaultWidget(grid_step_host)
        self.more_menu.addAction(grid_step_action)

        angle_step_host = QWidget()
        angle_step_row = QHBoxLayout(angle_step_host)
        angle_step_row.setContentsMargins(8, 2, 8, 2)
        angle_step_row.addWidget(QLabel("角度刻み"))
        self.angle_step_spin = QDoubleSpinBox()
        self.angle_step_spin.setRange(1.0, 90.0)
        self.angle_step_spin.setSingleStep(5.0)
        self.angle_step_spin.setSuffix("°")
        self.angle_step_spin.setToolTip("角度吸着の刻み幅（°）")
        self.angle_step_spin.setValue(15.0)
        self.angle_step_spin.valueChanged.connect(
            lambda _v: self.snapChanged.emit()
        )
        angle_step_row.addWidget(self.angle_step_spin)
        angle_step_action = QWidgetAction(self.more_menu)
        angle_step_action.setDefaultWidget(angle_step_host)
        self.more_menu.addAction(angle_step_action)

        self.more_button.setMenu(self.more_menu)
        self.more_button.hide()
        self._layout.addWidget(self.more_button)
        self._layout.addStretch(1)

    @property
    def is_compact(self) -> bool:
        return self._compact

    def set_compact(self, compact: bool) -> None:
        compact = bool(compact)
        if self._compact == compact:
            return
        self._compact = compact
        self.labels.setVisible(not compact)
        self.focus.setVisible(not compact)
        self.more_button.setVisible(compact)
        self._layout.setContentsMargins(
            6 if compact else 10,
            4 if compact else 6,
            6 if compact else 10,
            4 if compact else 6,
        )
        self._layout.setSpacing(4 if compact else 10)

    def state(self) -> RoomOverlayState:
        return RoomOverlayState(
            grid=self.grid.isChecked(),
            labels=self.labels.isChecked(),
            acoustics=self.acoustics.isChecked(),
            focus_selection=self.focus.isChecked(),
        )


class RecoveryBanner(QFrame):
    recoverRequested = Signal()
    discardRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        set_surface_role(self, SurfaceRole.OVERLAY)
        set_semantic_state(self, SemanticState.WARNING)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        message = QLabel("保存前の下書きがあります。復旧するか破棄してから編集してください。")
        message.setWordWrap(True)
        layout.addWidget(message, 1)
        recover = QPushButton("復旧")
        recover.setToolTip("保存前の下書きを読み込んで編集を再開します")
        discard = QPushButton("破棄")
        discard.setToolTip("下書きを削除して現在の保存版に戻します")
        set_primary_action(recover)
        recover.clicked.connect(lambda checked=False: self.recoverRequested.emit())
        discard.clicked.connect(lambda checked=False: self.discardRequested.emit())
        layout.addWidget(recover)
        layout.addWidget(discard)


class RoomWorkspace(QWidget):
    """Viewport-centric UX120 Room workspace with no legacy dock composition."""

    toolRequested = Signal(str)
    optimizeRequested = Signal()
    #: Emitted when Esc disarms the 3D field probe — the field-explorer
    #: panel listens so its checkbox follows the viewport state (#999).
    field3DProbeDisarmed = Signal()

    def __init__(
        self,
        repository: SceneRepository,
        document_id: str,
        parent: QWidget | None = None,
        *,
        viewport_factory: ViewportFactory | None = None,
        on_navigate: Callable[[WorkspaceDeepLink], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("roomWorkspace")
        set_surface_role(self, SurfaceRole.BASE)
        self.controller = RoomWorkspaceController(repository, document_id)
        self.listener_pose_repository = CadListenerPoseRepository(
            repository.path, repository
        )
        self.screen_transfer_repository = CadScreenTransferRepository(
            repository.path, repository
        )
        self.prediction_repository = CadPredictionRepository(repository)
        self.lighting_repository = CadLightingRepository(repository)
        # #1013 seam: fixture/zone/commissioning inventory is not yet
        # persisted — a provider callable returns (fixtures, zones,
        # records) for the current document. None = empty inventory; the
        # preview then honestly reports every scene ref as unresolved
        # instead of inventing placements.
        self.lighting_inventory_provider = None
        self._lighting_scene_cache: tuple[str, object] | None = None
        self._on_navigate = on_navigate
        self.current_context = "geometry"
        self.active_axis_constraint: str | None = None
        self.geometry_input = None
        self.transform_input = None
        self.geometry_panel: QWidget | None = None
        self.acoustics_panel: QWidget | None = None
        self._geometry_page: QScrollArea | None = None
        self._acoustics_page: QScrollArea | None = None
        self.prediction_results: tuple = ()
        # Spatial link of the selected interpretation finding (Issue #469).
        self.prediction_focus: PredictionSpatialLink | None = None
        self._responsive_compact = False
        self._palette_user_open = False
        self._viewport_factory = viewport_factory or (lambda owner: RoomViewport3D(owner))
        self.system_expansion = SystemExpansionWorkflowService(repository, document_id)
        self.system_expansion.apply_guard = self._system_expansion_apply_block_reason
        self._proposed_variant_id: str | None = None
        self._proposed_selected_id: str | None = None
        self._pending_editor_rejected = False
        self._section: SectionPlaneState | None = None
        self._pre_isolation_hidden: set[str] | None = None
        self._clipboard = None  # LayoutClipboard, set by layout tools (#613)
        self._guides_visible = True
        # REV40: persisted display_input.reflection_guidance_overlay policy.
        # 'off' until bind_reflection_guidance resolves the stored value —
        # an unbound workspace hides markers rather than guessing a policy.
        self._guidance_view: ReflectionGuidanceView | None = None
        self._guidance_overlay_mode: str = 'off'
        # #999 3D field overlay: armed request + current-head staleness live
        # in the controller; resolve() runs inside _render's deferred block.
        self.field_overlay = RoomFieldOverlayController(repository)
        # Esc exits probe mode only — armed while 3D probing so normal Esc
        # behaviour elsewhere is untouched.
        self._field_probe_esc = QShortcut(
            QKeySequence(Qt.Key.Key_Escape), self
        )
        self._field_probe_esc.setEnabled(False)
        self._field_probe_esc.activated.connect(self._disarm_field_probe)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.recovery_banner = RecoveryBanner()
        self.recovery_banner.recoverRequested.connect(self._recover)
        self.recovery_banner.discardRequested.connect(self._discard_recovery)
        root.addWidget(self.recovery_banner)

        self.tools = ContextToolStrip()
        self.tools.toolRequested.connect(self._tool_requested)
        root.addWidget(self.tools)

        # Numbered journey strip (REV34): the five context tabs are flat
        # siblings that never express the canonical first-build order, so a
        # state-driven step list stays pinned under the tab bar — each
        # number opens the context that owns the step.
        self.journey_card = QFrame(self)
        self.journey_card.setObjectName("roomJourneyCard")
        set_surface_role(self.journey_card, SurfaceRole.RAISED)
        journey_layout = QVBoxLayout(self.journey_card)
        journey_layout.setContentsMargins(16, 10, 16, 10)
        journey_layout.setSpacing(6)
        journey_head = QHBoxLayout()
        journey_head.setSpacing(8)
        journey_title = QLabel("部屋づくりの手順", self.journey_card)
        set_typography_role(journey_title, TypographyRole.SECTION_TITLE)
        journey_head.addWidget(journey_title)
        journey_head.addStretch(1)
        self.journey_progress = QLabel(self.journey_card)
        self.journey_progress.setObjectName("roomJourneyProgress")
        set_typography_role(self.journey_progress, TypographyRole.SECONDARY)
        journey_head.addWidget(self.journey_progress)
        journey_layout.addLayout(journey_head)
        # Steps live in a grid so the strip can reflow to 3-per-row under
        # the responsive-compact breakpoint instead of pinning a wide
        # minimum width on the whole workspace.
        self.journey_steps_grid = QGridLayout()
        self.journey_steps_grid.setSpacing(6)
        journey_layout.addLayout(self.journey_steps_grid)
        journey_hint_row = QHBoxLayout()
        journey_hint_row.setSpacing(8)
        self.journey_hint = QLabel(self.journey_card)
        self.journey_hint.setObjectName("roomJourneyHint")
        self.journey_hint.setWordWrap(True)
        set_typography_role(self.journey_hint, TypographyRole.SECONDARY)
        journey_hint_row.addWidget(self.journey_hint, 1)
        self.journey_open = QPushButton("現在の手順を開く", self.journey_card)
        self.journey_open.setObjectName("roomJourneyOpen")
        self.journey_open.clicked.connect(self._open_current_journey_step)
        journey_hint_row.addWidget(self.journey_open)
        journey_layout.addLayout(journey_hint_row)
        root.addWidget(self.journey_card)
        self._journey_steps: tuple[RoomJourneyStep, ...] = ()
        self._journey_buttons: dict[str, QPushButton] = {}

        content = QHBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)

        self.object_palette = ObjectPalette()
        self.object_palette.addRequested.connect(self._add_object)
        content.addWidget(self.object_palette)

        viewport_column = QWidget()
        viewport_column.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        viewport_layout = QVBoxLayout(viewport_column)
        viewport_layout.setContentsMargins(0, 0, 0, 0)
        viewport_layout.setSpacing(0)
        self.overlay_controls = OverlayControls()
        self.overlay_controls.changed.connect(self._render)
        self.overlay_controls.snapChanged.connect(self._snap_controls_changed)
        viewport_layout.addWidget(self.overlay_controls)

        viewport_widget = self._viewport_factory(viewport_column)
        self.viewport = cast(RoomViewportPort, viewport_widget)
        # entityPicked carries the display position AND fires before
        # entitySelected; the compat path dedupes the follow-up signal.
        picked_signal = getattr(viewport_widget, "entityPicked", None)
        if picked_signal is not None and hasattr(picked_signal, "connect"):
            picked_signal.connect(self._entity_picked)
        selected_signal = getattr(viewport_widget, "entitySelected", None)
        if selected_signal is not None and hasattr(selected_signal, "connect"):
            selected_signal.connect(self._entity_selected_compat)
        empty_signal = getattr(viewport_widget, "emptyClicked", None)
        if empty_signal is not None and hasattr(empty_signal, "connect"):
            empty_signal.connect(self._empty_clicked)
        marquee_signal = getattr(viewport_widget, "entitiesMarqueeSelected", None)
        if marquee_signal is not None and hasattr(marquee_signal, "connect"):
            marquee_signal.connect(self._entities_marquee_selected)
        self._suppress_next_select = False
        self._saved_camera_view: tuple | None = None
        self._video_evaluation = None
        self._history_preview_revision_id: str | None = None
        proposed_signal = getattr(viewport_widget, "proposedEntitySelected", None)
        if proposed_signal is not None and hasattr(proposed_signal, "connect"):
            proposed_signal.connect(self._proposal_entity_selected)
        viewport_layout.addWidget(viewport_widget, 1)

        # The viewport and the right-hand panel stack share a splitter so the
        # panel width is user-resizable instead of a fixed column.
        self._content_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._content_splitter.setChildrenCollapsible(False)
        self._content_splitter.addWidget(viewport_column)
        self._right_panel_user_sized = False
        self._content_splitter.splitterMoved.connect(
            lambda *_args: setattr(self, '_right_panel_user_sized', True)
        )
        content.addWidget(self._content_splitter, 1)

        self.inspector = SelectionInspector()
        # The right panel width is user-resizable via the content splitter;
        # inner panels loosen their own caps so they follow the stack.
        self.inspector.setMaximumWidth(560)
        self.inspector.editCommitted.connect(self._commit_inspector)
        self.inspector.aimTargetRequested.connect(self._aim_target_committed)
        self.inspector.aimClearRequested.connect(self._aim_clear_committed)
        self.inspector.alignCabinetRequested.connect(self._cabinet_align_committed)
        self.inspector.meshImportRequested.connect(self._import_mesh_for_selected)
        self.right_stack = QStackedWidget()
        self.right_stack.setMinimumWidth(248)
        self.right_stack.setMaximumWidth(560)

        # Objects context (#480/#482/#491): object list + inspector + measure
        # tool live together so list, viewport and form stay in sync.
        self.objects_panel = RoomObjectsPanel()
        self.objects_panel.selectionRequested.connect(self._objects_selection)
        self.objects_panel.hideRequested.connect(self._objects_hidden)
        self.objects_panel.lockRequested.connect(self._objects_locked)
        self.objects_panel.deleteRequested.connect(self._objects_delete)
        self.measure_controller = RoomMeasureController(self, self.viewport)
        self.measure_panel = RoomMeasurePanel(self.measure_controller)
        self.measure_controller.stateChanged.connect(self._measure_state_changed)
        objects_body = QWidget()
        objects_layout = QVBoxLayout(objects_body)
        objects_layout.setContentsMargins(0, 0, 0, 0)
        objects_layout.setSpacing(10)
        objects_layout.addWidget(self.objects_panel)
        objects_layout.addWidget(self.inspector)
        objects_layout.addWidget(self.measure_panel)
        objects_layout.addStretch(1)
        self.objects_page = QScrollArea()
        self.objects_page.setWidgetResizable(True)
        self.objects_page.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.objects_page.setFrameShape(QFrame.Shape.NoFrame)
        self.objects_page.setWidget(objects_body)
        self.right_stack.addWidget(self.objects_page)

        self.system_expansion_panel = SystemExpansionRoomPanel(self.system_expansion)
        self.system_expansion_panel.variantChanged.connect(self._proposal_variant_changed)
        self.system_expansion_panel.ghostEntityRequested.connect(
            self._proposal_entity_selected
        )
        self.standards_panel = StandardsCriterionPanel(repository, document_id)
        self.constraints_panel = RoomConstraintsPanel()
        self.constraints_panel.constraintActionRequested.connect(
            self._constraint_action
        )
        self.constraints_panel.resultSelected.connect(self._constraint_highlight)
        self.constraints_panel.optimizeRequested.connect(
            lambda: self.optimizeRequested.emit()
        )
        self.video_panel = RoomVideoPanel()
        self.video_panel.bindingsChanged.connect(self._video_bindings_changed)
        self.video_panel.poseChanged.connect(self._seat_pose_changed)
        self.video_panel.poseSaveRequested.connect(self._seat_pose_saved)
        self.video_panel.transferChanged.connect(self._screen_transfer_changed)
        self.video_panel.transferSaveRequested.connect(self._screen_transfer_saved)
        self.video_panel.evaluateRequested.connect(self._video_evaluate)
        self.video_panel.viewFromSeatRequested.connect(self._view_from_seat)
        self.video_panel.createSpecRequested.connect(self._video_create_spec)
        self.video_panel.createDisplaySpecRequested.connect(
            self._video_create_display_spec
        )
        # #1013: read-only 「照明シーン」 preview on the Room/Video surface.
        # The toggle only repaints explanation glyphs — it never sends to
        # a device; apply/read-back stay on the approved action path.
        self.lighting_panel = RoomLightingPreviewPanel()
        self.lighting_panel.previewToggled.connect(
            lambda _checked=False: self._render()
        )
        # #1010: read-only 「運用クリアランス」 layer — declared zone XY
        # footprints + conflict highlights; never authors geometry.
        self.clearance_panel = RoomOperationalClearancePanel()
        self.clearance_panel.changed.connect(lambda: self._render())
        # UX140B: リスニング集団 (seat-priority profile authoring) — the
        # legacy TheaterEditorWindow dock's workflow mount; it edits the
        # same seats the placement context owns.
        self.seat_priority_panel = SeatPriorityPanel(repository, document_id)
        self.seat_priority_panel.refresh(self.controller.committed_document)
        # REV44-INSTALL: per-speaker installation context + scene datum
        # registration — equipment assignment already lives here, so the
        # authority writers mount on the same placement page.
        self.installation_panel = InstallationPanel(
            repository,
            self.system_expansion.equipment_repository,
            document_id,
        )
        # #1012: ラック配置ワークスペース — the #562 rack authority's 2D RU
        # elevation + fit surface; lives with the equipment-assignment
        # context like the installation writers above it.
        self.rack_workspace_panel = RackWorkspacePanel(
            repository, document_id
        )
        placement_body = QWidget()
        placement_layout = QVBoxLayout(placement_body)
        placement_layout.setContentsMargins(0, 0, 0, 0)
        placement_layout.setSpacing(10)
        placement_layout.addWidget(self.system_expansion_panel)
        placement_layout.addWidget(self.constraints_panel)
        placement_layout.addWidget(self.video_panel)
        placement_layout.addWidget(self.lighting_panel)
        placement_layout.addWidget(self.clearance_panel)
        placement_layout.addWidget(self.seat_priority_panel)
        placement_layout.addWidget(self.standards_panel)
        placement_layout.addWidget(self.installation_panel)
        placement_layout.addWidget(self.rack_workspace_panel)
        placement_layout.addStretch(1)
        # Narrow-column safety: every combo in this column shrinks to a short
        # minimum, every line edit and spin box may squeeze below its size
        # hint (a hard floor keeps it usable), and every form wraps its label
        # above the field — otherwise long spec names and mm-unit values
        # force the scroll area's horizontal scrollbar. Widgets built later
        # at refresh time repeat the same treatment at their creation sites.
        for _combo in placement_body.findChildren(QComboBox):
            _combo.setMinimumContentsLength(6)
            _combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            _combo.setMinimumWidth(72)
            _combo.setSizePolicy(
                QSizePolicy.Policy.Ignored, _combo.sizePolicy().verticalPolicy()
            )
        for _field in (
            *placement_body.findChildren(QAbstractSpinBox),
            *placement_body.findChildren(QLineEdit),
        ):
            _field.setMinimumWidth(72)
            _field.setSizePolicy(
                QSizePolicy.Policy.Ignored, _field.sizePolicy().verticalPolicy()
            )
        for _form in placement_body.findChildren(QFormLayout):
            _form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.placement_panel = QScrollArea()
        self.placement_panel.setWidgetResizable(True)
        self.placement_panel.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.placement_panel.setFrameShape(QFrame.Shape.NoFrame)
        self.placement_panel.setWidget(placement_body)
        self.right_stack.addWidget(self.placement_panel)

        # History context (#485): revision browser/labels/ghost preview/restore.
        self.history_panel = RoomHistoryPanel()
        self.history_panel.previewRequested.connect(self._history_preview)
        self.history_panel.restoreRequested.connect(self._history_restore)
        self.history_panel.labelRequested.connect(self._history_label)
        self.history_panel.diffRequested.connect(self._history_diff)
        self.history_panel.set_label_fields("", "")
        self.history_body = QWidget()
        history_layout = QVBoxLayout(self.history_body)
        history_layout.setContentsMargins(0, 0, 0, 0)
        history_layout.addWidget(self.history_panel)
        history_layout.addStretch(1)
        self.history_page = QScrollArea()
        self.history_page.setWidgetResizable(True)
        self.history_page.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.history_page.setFrameShape(QFrame.Shape.NoFrame)
        self.history_page.setWidget(self.history_body)
        self.right_stack.addWidget(self.history_page)

        self.right_stack.setCurrentWidget(self.objects_page)
        self._content_splitter.addWidget(self.right_stack)
        self._content_splitter.setStretchFactor(0, 1)
        self._content_splitter.setStretchFactor(1, 0)
        root.addLayout(content, 1)

        self._last_operation_error_detail: str | None = None
        self.status_strip = QFrame()
        set_surface_role(self.status_strip, SurfaceRole.RAISED)
        strip_layout = QHBoxLayout(self.status_strip)
        strip_layout.setContentsMargins(12, 6, 12, 6)
        strip_layout.setSpacing(12)
        self.status = QLabel()
        set_typography_role(self.status, TypographyRole.SECONDARY)
        strip_layout.addWidget(self.status, 1)
        # Always-visible dirty badge on the same strip: the notice label is
        # reused for last-action text and must never hide whether the
        # document actually has unsaved changes.
        self.dirty_status_label = QLabel()
        set_typography_role(self.dirty_status_label, TypographyRole.SECONDARY)
        strip_layout.addWidget(self.dirty_status_label)
        root.addWidget(self.status_strip)

        self.set_context("geometry")
        self._refresh(reset_camera=True)
        self._restore_view_extras()
        self.view_menu = QMenu(self)
        self.view_menu.aboutToShow.connect(self._rebuild_view_menu)
        underlay_signal = getattr(self.viewport, "underlayClicked", None)
        if underlay_signal is not None and hasattr(underlay_signal, "connect"):
            underlay_signal.connect(self._underlay_clicked)

    @property
    def is_dirty(self) -> bool:
        return self.controller.is_dirty

    def activate(self) -> None:
        changed = self.controller.reload_if_clean()
        self._sync_snap_controls()
        self._sync_objects_panel()
        if self.current_context == "placement":
            self._sync_constraints_panel()
            self._sync_video_panel()
            self._sync_seat_priority_panel()
        if self.current_context == "history":
            self._sync_history_panel()
        self._refresh(reset_camera=changed)

    def before_deactivate(self) -> tuple[bool, str | None]:
        if self.measure_controller.is_active:
            self.measure_controller.cancel()
        if self.geometry_input is not None and self.geometry_input.is_active:
            return False, "部屋形状の編集中です。確定またはキャンセルしてから画面を切り替えてください"
        if self.transform_input is not None and self.transform_input.is_active:
            return False, "項目の移動または回転を確定・キャンセルしてから画面を切り替えてください"
        if getattr(self, 'system_expansion_panel', None) is not None and (
            self.system_expansion_panel.is_running()
        ):
            return False, (
                "提案の作成が完了またはキャンセルされるまで"
                "画面を切り替えられません"
            )
        allowed, reason = self.controller.before_deactivate()
        if allowed:
            self._persist_view_extras()
        return allowed, reason

    def attach_geometry_input(self, controller) -> None:
        self.geometry_input = controller

    def attach_transform_input(self, controller) -> None:
        self.transform_input = controller

    def _dock_scroll_page(self, panel: QWidget) -> QScrollArea:
        """Stack page chrome shared by the dock pages: resizable, frameless,
        and honestly scrollable when the content outgrows the dock width."""

        page = QScrollArea()
        page.setWidgetResizable(True)
        page.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        page.setFrameShape(QFrame.Shape.NoFrame)
        page.setWidget(panel)
        return page

    def attach_geometry_panel(self, panel: QWidget) -> None:
        if self._geometry_page is not None:
            self.right_stack.removeWidget(self._geometry_page)
            self._geometry_page.setParent(None)
        self.geometry_panel = panel
        self._geometry_page = self._dock_scroll_page(panel)
        self.right_stack.addWidget(self._geometry_page)
        if self.current_context == "geometry":
            self.right_stack.setCurrentWidget(self._geometry_page)

    def attach_acoustics_panel(self, panel: QWidget) -> None:
        if self._acoustics_page is not None:
            self.right_stack.removeWidget(self._acoustics_page)
            self._acoustics_page.setParent(None)
        self.acoustics_panel = panel
        self._acoustics_page = self._dock_scroll_page(panel)
        self.right_stack.addWidget(self._acoustics_page)
        if self.current_context == "acoustics":
            self.right_stack.setCurrentWidget(self._acoustics_page)

    # --- geometry intake (#866) ----------------------------------------------

    def bind_geometry_intake(self, controller, panel) -> None:
        """Wire a GeometryIntakeController + GeometryIntakePanel into the
        geometry dock page. The controller owns the repository and the
        sealed decision/derivation chain; the panel only renders and
        forwards operator intent."""
        self.geometry_intake_controller = controller
        self.geometry_intake_panel = panel
        panel.locateRequested.connect(self._geometry_intake_locate)
        panel.ifcImportRequested.connect(self._geometry_intake_import_ifc)
        panel.sceneSubjectRequested.connect(
            self._geometry_intake_adopt_scene
        )
        panel.diagnoseRequested.connect(self._geometry_intake_diagnose)
        panel.deriveRequested.connect(self._geometry_intake_derive)
        panel.decisionRequested.connect(self._geometry_intake_decision)
        panel.solverSelectionChanged.connect(
            self._geometry_intake_solver_changed
        )
        self._sync_geometry_intake_panel()

    def _geometry_intake_solver_options(self):
        controller = self.geometry_intake_controller
        options = []
        for descriptor, manifest in controller.solver_options():
            options.append((
                f'{descriptor.adapter_id} '
                f'v{descriptor.adapter_version} '
                f'({descriptor.acoustic_domain})',
                (descriptor, manifest),
            ))
        return options

    def _sync_geometry_intake_panel(self) -> None:
        if getattr(self, '_geometry_intake_syncing', False):
            return
        controller = getattr(self, 'geometry_intake_controller', None)
        panel = getattr(self, 'geometry_intake_panel', None)
        if controller is None or panel is None:
            return
        self._geometry_intake_syncing = True
        try:
            panel.set_solver_options(
                self._geometry_intake_solver_options()
            )
            panel.set_report(controller.report)
            panel.set_proposal(controller.proposal)
            panel.set_stage(
                has_subject=controller.subject is not None,
                derive_enabled=(
                    controller.acceptance is not None
                    and controller.revision is None
                ),
            )
            if controller.verdict is not None:
                evidence = controller.verdict_evidence_state()
                panel.set_verdict(
                    controller.verdict, evidence_state=evidence
                )
            else:
                panel.set_verdict(None)
            total = (
                len(controller.proposal.actions)
                if controller.proposal is not None
                else 0
            )
            decided = len(controller.pending_decisions)
            if total:
                if controller.acceptance is not None:
                    progress = (
                        f'決定 {decided}/{total} — '
                        '受理レコードを生成済み'
                    )
                else:
                    progress = f'決定 {decided}/{total}'
                panel.set_decision_progress(progress)
            elif controller.proposal is not None:
                panel.set_decision_progress('修復提案はありません')
            else:
                panel.set_decision_progress('')
        finally:
            self._geometry_intake_syncing = False

    def _geometry_intake_locate(self, targets: object) -> None:
        ids = [str(target) for target in targets]
        self._intake_locate_markers = ()
        known = {
            entity.entity_id for entity in self.controller.document.entities
        }
        scene_ids = [entity_id for entity_id in ids if entity_id in known]
        # Non-scene ids are subject-side part refs (e.g. 'ifc:<step_id>').
        part_ids = [
            entity_id
            for entity_id in ids
            if entity_id not in known and not entity_id.startswith('entity:')
        ]
        controller = self.geometry_intake_controller
        anchors = (
            intake_locate_anchors(controller.subject, part_ids)
            if part_ids and controller.subject is not None
            else ()
        )
        located = [a for a in anchors if a.state == 'located']
        if located:
            markers = []
            for index, anchor in enumerate(located):
                markers.append(
                    ReflectionGuidanceOverlayMarker(
                        surface_id=anchor.part_id,
                        label=f'{anchor.part_id} · {anchor.detail}',
                        confidence='authority_backed',
                        zone_anchor=anchor.center,
                        source_anchor=None,
                        source_label=None,
                        path_points=(),
                    )
                )
            self._intake_locate_markers = tuple(markers)
        if scene_ids:
            self.controller.set_selection_many(scene_ids, additive=False)
            self._after_selection_changed()
        if part_ids and controller.subject is not None:
            # Route the operator to the repair targets of the located
            # parts — the direct defect -> fix-target guidance (#977).
            panel = getattr(self, 'geometry_intake_panel', None)
            if controller.proposal is not None and panel is not None:
                panel.focus_proposal_actions(
                    proposal_actions_for_parts(
                        controller.proposal, part_ids
                    )
                )
            unresolved = [a for a in anchors if a.state != 'located']
            if located:
                where = ', '.join(
                    f'{a.part_id} ({a.center.x_m:.2f}, {a.center.y_m:.2f}, '
                    f'{a.center.z_m:.2f})'
                    for a in located
                )
                tail = (
                    f' / 未解決 {len(unresolved)} 件' if unresolved else ''
                )
                self._set_status(
                    f'{len(scene_ids)} 件を選択、IFC 部位の位置をマーカー表示: '
                    f'{where}{tail}'
                )
            elif unresolved:
                self._set_status(
                    'IFC 部位の位置を特定できません: '
                    + '; '.join(a.detail for a in unresolved),
                    error=True,
                )
            else:
                self._set_status(
                    f'{len(scene_ids)} 件の対象を選択しました'
                )
        elif part_ids:
            # No subject loaded — the honest answer names that, not a
            # silent no-op.
            self._set_status(
                '対象はシーン内に存在しません (IFC ソース由来の部位です '
                '— intake subject が未ロードです)',
                error=True,
            )
        elif not scene_ids:
            self._set_status(
                '対象はシーン内に存在しません (IFC ソース由来の部位です)',
                error=True,
            )
        else:
            self._set_status(f'{len(scene_ids)} 件の対象を選択しました')
        self._render()

    def _geometry_intake_import_ifc(self) -> None:
        controller = self.geometry_intake_controller
        path_text, _ = file_dialog_memory.get_open_file_name(
            self,
            'IFC ファイルをインポート',
            'room.import_ifc',
            'IFC ファイル (*.ifc);;すべてのファイル (*)',
        )
        if not path_text:
            return
        try:
            source = Path(path_text).read_bytes()
            artifact, _subject = controller.import_ifc_source(
                source, file_name=Path(path_text).name
            )
            report, proposal = controller.run_health_check()
        except (OSError, ValueError) as exc:
            self._set_operation_error(
                'IFC の取り込みに失敗しました', exc
            )
            return
        self._sync_geometry_intake_panel()
        self._set_status(
            f'IFC「{artifact.file_name}」を取り込みました: '
            f'欠陥 {len(report.defects)} 件 / '
            f'修復提案 {len(proposal.actions)} 件'
        )

    def _geometry_intake_adopt_scene(self) -> None:
        controller = self.geometry_intake_controller
        try:
            subject = controller.adopt_current_subject(
                read_blob=self.controller.repository.read_blob,
            )
            report, proposal = controller.run_health_check()
        except (ValueError, KeyError) as exc:
            self._set_operation_error(
                'シーンの取り込みに失敗しました', exc
            )
            return
        self._sync_geometry_intake_panel()
        self._set_status(
            f'シーンから {len(subject.parts)} 部位を採用しました: '
            f'欠陥 {len(report.defects)} 件 / '
            f'修復提案 {len(proposal.actions)} 件'
        )

    def _geometry_intake_diagnose(self) -> None:
        controller = self.geometry_intake_controller
        try:
            report, proposal = controller.run_health_check()
        except (ValueError, KeyError) as exc:
            self._set_operation_error('診断を実行できませんでした', exc)
            return
        self._sync_geometry_intake_panel()
        self._set_status(
            f'診断を実行しました: 欠陥 {len(report.defects)} 件 / '
            f'修復提案 {len(proposal.actions)} 件'
        )

    def _geometry_intake_decision(
        self, action_id, decision, params
    ) -> None:
        controller = self.geometry_intake_controller
        try:
            acceptance = controller.submit_decision(
                str(action_id),
                str(decision),
                accepted_parameters=dict(params or {}),
            )
        except (ValueError, KeyError) as exc:
            self._set_operation_error(
                '決定を記録できませんでした', exc
            )
            return
        self.geometry_intake_panel.mark_decided(str(action_id))
        self._sync_geometry_intake_panel()
        if acceptance is not None:
            self._set_status(
                'すべての修復提案への決定を記録しました。'
                '派生リビジョンを生成できます'
            )

    def _geometry_intake_derive(self) -> None:
        controller = self.geometry_intake_controller
        try:
            revision = controller.derive_revision()
            verdict = (
                controller.evaluate_readiness()
                if controller.solver_descriptor is not None
                else None
            )
        except (ValueError, KeyError) as exc:
            self._set_operation_error(
                '派生リビジョンを生成できませんでした', exc
            )
            return
        self._sync_geometry_intake_panel()
        verdict_note = (
            f' — 判定: {geometry_intake_label(f"verdict.{verdict.verdict}")}'
            if verdict is not None
            else ''
        )
        self._set_status(
            f'派生リビジョン {revision.derived_revision_id} '
            f'を生成しました{verdict_note}'
        )

    def _geometry_intake_solver_changed(self, payload: object) -> None:
        if getattr(self, '_geometry_intake_syncing', False):
            return
        controller = self.geometry_intake_controller
        if payload is None:
            controller.solver_descriptor = None
            controller.solver_manifest = None
            self._sync_geometry_intake_panel()
            return
        descriptor, manifest = payload
        try:
            controller.select_solver(descriptor, manifest)
        except ValueError as exc:
            self._set_operation_error(
                'ソルバー適合性を評価できませんでした', exc
            )
            return
        self._sync_geometry_intake_panel()

    def set_prediction_results(self, results: object) -> None:
        self.prediction_results = results if isinstance(results, tuple) else ()
        self.prediction_focus = None
        self._render()

    def set_prediction_focus(self, link: PredictionSpatialLink | None) -> None:
        """Highlight the spatial evidence behind the selected finding."""
        self.prediction_focus = link
        self._render()

    def refresh(self, *, reset_camera: bool = False) -> None:
        self._refresh(reset_camera=reset_camera)

    def add_object(self, kind: str) -> bool:
        before = self.controller.document
        self._add_object(kind)
        return self.controller.document != before

    def duplicate_selected(self) -> bool:
        # Route through the layout duplicate so multi-selection duplicates stay
        # a single atomic Undo step (#613, #480).
        return self.layout_duplicate()

    def delete_selection(self) -> bool:
        """Undo-safe batch delete from the viewport/context menu (#482).

        While the geometry editor is active the same Delete verb targets the
        selected vertex/wall — both are undoable through the working document
        and both reject locked/structurally-invalid states.
        """
        geometry = self.geometry_input
        if geometry is not None and geometry.is_active:
            try:
                if geometry.selected_vertex_id is not None:
                    return geometry.delete_selected_vertex()
                if geometry.selected_edge_index is not None:
                    return geometry.delete_selected_wall()
            except (EditStateError, ValueError) as exc:
                self._set_operation_error("形状の選択項目を削除できませんでした", exc)
                return False
            return False
        ids = tuple(self.controller.view_state.selection)
        if not ids:
            return False
        locked = [
            self.controller.document.entity(eid).name
            for eid in ids
            if self.controller.view_state.is_locked(eid)
        ]
        if locked:
            self._set_status(
                f"ロック中の項目は削除できません: {'、'.join(locked)}",
                error=True,
            )
            return False
        try:
            changed = self.controller.delete_entities(ids)
        except EditStateError as exc:
            self._set_operation_error("項目を削除できませんでした", exc, effect='変更は保存されていません')
            return False
        if changed:
            self._refresh()
            self._set_status(f"{changed} 項目を削除しました（元に戻せます）")
        return changed

    def set_selected_hidden(self, hidden: bool) -> int:
        ids = tuple(self.controller.view_state.selection)
        changed = self.controller.set_entities_hidden(ids, hidden)
        if changed:
            self._refresh()
            self._set_status(
                f"{changed} 項目を非表示にしました" if hidden else f"{changed} 項目を表示しました"
            )
        return changed

    def set_selected_locked(self, locked: bool) -> int:
        ids = tuple(self.controller.view_state.selection)
        changed = self.controller.set_entities_locked(ids, locked)
        if changed:
            self._refresh()
            self._set_status(
                f"{changed} 項目をロックしました" if locked else f"{changed} 項目のロックを解除しました"
            )
        return changed

    def toggle_measure(self) -> bool:
        if self.measure_controller.is_active:
            self.measure_controller.cancel()
        else:
            self.measure_controller.begin()
        self._render()
        return True

    def set_transform_mode(self, mode: str) -> None:
        if mode not in {"move", "rotate"}:
            raise ValueError(mode)
        self.controller.view_state.transform_mode = mode
        self._set_status("移動モード" if mode == "move" else "回転モード")

    def select_all(self) -> None:
        """Select every visible entity (hidden items can't be picked, so they
        stay unselected for delete/drag safety)."""
        ids = [
            entity.entity_id
            for entity in self.controller.document.entities
            if not self.controller.view_state.is_hidden(entity.entity_id)
        ]
        self.controller.view_state.set_selection(ids)
        self.controller._persist_view_state()
        self._after_selection_changed()

    def select_invert(self) -> None:
        """Invert the selection across visible entities."""
        view_state = self.controller.view_state
        inverted = [
            entity.entity_id
            for entity in self.controller.document.entities
            if not view_state.is_selected(entity.entity_id)
            and not view_state.is_hidden(entity.entity_id)
        ]
        view_state.set_selection(inverted)
        self.controller._persist_view_state()
        self._after_selection_changed()

    def clear_selection(self) -> None:
        if not self.controller.view_state.selection:
            return
        self.controller.view_state.set_selection(())
        self.controller._persist_view_state()
        self._after_selection_changed()

    def fit_selection(self) -> None:
        selection = self.controller.view_state.selection
        if not selection and self.controller.selected_id is not None:
            selection = (self.controller.selected_id,)
        focus = getattr(self.viewport, "focus_entities", None)
        if callable(focus):
            focus(selection)
            return
        if selection:
            self.viewport.focus_entity(selection[-1])

    def fit_all(self) -> None:
        self.viewport.fit_scene()

    # --- Standard views, saved views, isolation and section (#545, #629) ------

    def apply_standard_view(self, view: StandardView | str) -> None:
        apply = getattr(self.viewport, "apply_standard_view", None)
        if not callable(apply):
            return
        apply(view)
        self._persist_view_extras()
        label = STANDARD_VIEW_LABELS.get(StandardView(view), str(view))
        self._set_status(f"{label}ビューに切り替えました")

    def _persist_view_extras(self) -> None:
        """Persist the current camera + section as this document's view state.

        Never writes SceneRevisions — this only updates the editor-side
        ``editor_camera_states`` row.
        """

        capture = getattr(self.viewport, "capture_camera_state", None)
        if not callable(capture):
            return
        try:
            camera = capture()
        except (RuntimeError, TypeError, ValueError):
            return
        try:
            self.controller.persist_view_extras(
                PersistedViewState(camera=camera, section=self._section)
            )
        except (RuntimeError, TypeError, ValueError):
            return

    def _restore_view_extras(self) -> None:
        """Restore the last working camera/section, or fall back to fit-all."""

        stored = self.controller.persisted_view_state()
        if stored is not None:
            apply_state = getattr(self.viewport, "apply_camera_state", None)
            if callable(apply_state):
                try:
                    apply_state(stored.camera)
                except (RuntimeError, TypeError, ValueError):
                    self.viewport.fit_scene()
            self._section = stored.section
        self._sync_aux_render_state()

    def _sync_aux_render_state(self) -> None:
        """Push non-authoritative extras (underlays, guides, section) into the
        viewport; the next ``render_document`` draws them."""

        set_aux = getattr(self.viewport, "set_aux_render_state", None)
        if not callable(set_aux):
            return
        set_aux(
            underlays=self.controller.underlay_render_items(),
            guides=self.controller.guide_render_items(),
            section=self._section,
        )

    def isolate_selection(self) -> bool:
        selection = tuple(self.controller.view_state.selection)
        if not selection and self.controller.selected_id is not None:
            selection = (self.controller.selected_id,)
        if not selection:
            return False
        if self._pre_isolation_hidden is None:
            self._pre_isolation_hidden = set(self.controller.view_state.hidden_ids)
        self.controller.isolate_entities(set(selection))
        self._render()
        self._set_status("選択項目のみ表示しています")
        return True

    def isolate_kind(self) -> bool:
        selected = self.controller.selected_id
        if selected is None:
            return False
        try:
            kind = self.controller.document.entity(selected).kind
        except KeyError:
            return False
        keep = {
            entity.entity_id
            for entity in self.controller.document.entities
            if entity.kind == kind
        }
        if self._pre_isolation_hidden is None:
            self._pre_isolation_hidden = set(self.controller.view_state.hidden_ids)
        self.controller.isolate_entities(keep)
        self._render()
        self._set_status(
            f"「{SelectionInspector.KIND_LABELS.get(kind, kind)}」のみ表示しています"
        )
        return True

    def clear_isolation(self) -> bool:
        if self._pre_isolation_hidden is None:
            return False
        self.controller.set_hidden_ids(self._pre_isolation_hidden)
        self._pre_isolation_hidden = None
        self._render()
        self._set_status("分離を解除しました")
        return True

    def toggle_section(self) -> bool:
        if self._section is not None and self._section.enabled:
            self._section = None
            self._set_status("断面を解除しました")
        else:
            room = self.controller.document.room
            bounds = room.bounds_m if room is not None else (0.0, 0.0, 1.0, 1.0)
            height = room.height_m if room is not None else 2.4
            z_level = min(1.2, max(0.5, height * 0.5))
            self._section = SectionPlaneState(
                enabled=True,
                origin=(
                    (bounds[0] + bounds[2]) * 0.5,
                    (bounds[1] + bounds[3]) * 0.5,
                    z_level,
                ),
                normal=(0.0, 0.0, 1.0),
                label="水平断面",
            )
            self._set_status(f"水平断面を表示しました (z={z_level:.2f} m)")
        self._sync_aux_render_state()
        self._render()
        self._persist_view_extras()
        return True

    def save_named_view(self) -> bool:
        capture = getattr(self.viewport, "capture_camera_state", None)
        if not callable(capture):
            self._set_status("このビューポートではビューを保存できません", error=True)
            return False
        name, ok = QInputDialog.getText(self, "ビューを保存", "ビュー名:")
        name = (name or "").strip()
        if not ok:
            return False
        if not name:
            self._set_status("ビュー名を入力してください", error=True)
            return False
        hidden = sorted(self.controller.view_state.hidden_ids)
        spec = NamedViewSpec(
            name=name,
            camera=capture(),
            hidden_ids=tuple(hidden) if hidden else None,
            focus_entity_id=self.controller.selected_id,
            section=self._section,
        )
        try:
            self.controller.save_named_view(spec)
        except (TypeError, ValueError) as exc:
            self._set_operation_error("ビューを保存できませんでした", exc, effect='変更は保存されていません')
            return False
        self._set_status(f"ビュー「{name}」を保存しました")
        return True

    def apply_named_view(self, view_id: str) -> bool:
        spec = None
        for candidate_id, candidate in self.controller.named_views():
            if candidate_id == view_id:
                spec = candidate
                break
        if spec is None:
            self._set_status("指定されたビューが見つかりません", error=True)
            return False
        # Missing entities referenced by the view fail soft — never rebind
        # by name, never abort the recall.
        if spec.hidden_ids is not None:
            self.controller.set_hidden_ids(set(spec.hidden_ids))
            self._pre_isolation_hidden = None
        self._section = spec.section
        if spec.focus_entity_id is not None:
            try:
                self.controller.set_selection(spec.focus_entity_id)
            except KeyError:
                pass
        apply_state = getattr(self.viewport, "apply_camera_state", None)
        if callable(apply_state):
            apply_state(spec.camera)
        self._sync_aux_render_state()
        self._persist_view_extras()
        self._refresh()
        self._set_status(f"ビュー「{spec.name}」を適用しました")
        return True

    def delete_named_view(self, view_id: str) -> None:
        self.controller.delete_named_view(view_id)
        self._set_status("ビューを削除しました")

    def _rebuild_view_menu(self) -> None:
        menu = self.view_menu
        menu.clear()
        current_view = getattr(self.viewport, "standard_view", "")
        for view in (
            StandardView.PERSPECTIVE,
            StandardView.TOP,
            StandardView.FRONT,
            StandardView.REAR,
            StandardView.LEFT,
            StandardView.RIGHT,
        ):
            action = menu.addAction(STANDARD_VIEW_LABELS[view])
            action.setCheckable(True)
            action.setChecked(current_view == view.value)
            action.triggered.connect(
                lambda checked=False, target=view: self.apply_standard_view(target)
            )
        menu.addSeparator()
        has_selection = bool(self.controller.view_state.selection) or (
            self.controller.selected_id is not None
        )
        action = menu.addAction("選択のみ表示")
        action.setEnabled(has_selection)
        action.triggered.connect(lambda checked=False: self.isolate_selection())
        action = menu.addAction("同じ種類のみ表示")
        action.setEnabled(has_selection)
        action.triggered.connect(lambda checked=False: self.isolate_kind())
        action = menu.addAction("分離解除")
        action.setEnabled(self._pre_isolation_hidden is not None)
        action.triggered.connect(lambda checked=False: self.clear_isolation())
        menu.addSeparator()
        action = menu.addAction("水平断面")
        action.setCheckable(True)
        action.setChecked(self._section is not None and self._section.enabled)
        action.triggered.connect(lambda checked=False: self.toggle_section())
        action = menu.addAction("ビューを保存…")
        action.triggered.connect(lambda checked=False: self.save_named_view())
        named = self.controller.named_views()
        if named:
            named_menu = menu.addMenu("名前付きビュー")
            for view_id, spec in named:
                sub = named_menu.addMenu(spec.name)
                open_action = sub.addAction("開く")
                open_action.triggered.connect(
                    lambda checked=False, target=view_id: self.apply_named_view(target)
                )
                delete_action = sub.addAction("削除")
                delete_action.triggered.connect(
                    lambda checked=False, target=view_id: self.delete_named_view(target)
                )
        menu.addSeparator()
        action = menu.addAction("ジオメトリをインポート…")
        action.triggered.connect(
            lambda checked=False: self.import_geometry_dialog()
        )
        self._rebuild_underlay_menu(menu)
        self._rebuild_constraint_menu(menu)

    def _underlay_clicked(self, underlay_id: object, x_m: float, y_m: float) -> None:
        result = self.controller.handle_underlay_click(str(underlay_id), x_m, y_m)
        self._refresh_underlay_ui()
        if result == 'first':
            self._set_status('1点目を記録しました。2点目をクリックしてください')
        elif result == 'ready':
            self._finish_underlay_calibration()
        else:
            # Not calibrating: the click must not be swallowed — behave like an
            # empty-space click so it still deselects or feeds the measure tool.
            last = getattr(self.viewport, "_last_display_position", None)
            position = last() if callable(last) else None
            if position is not None:
                self._empty_clicked(position)

    def _refresh_underlay_ui(self) -> None:
        self._sync_aux_render_state()
        self._render()
        armed = self.controller.underlay_calibration_underlay_id
        if armed is not None:
            self._set_status(
                '下図校正中: 図面上の既知の2点をクリックしてください'
                ' (Escで中止)'
            )

    # --- Floor-plan underlay UI (#534) -----------------------------------------

    def import_underlay_dialog(self) -> bool:
        """Pick an image/PDF/DXF file and import it as a tracing underlay."""

        if self.controller.document.room is None:
            self._set_status("先に部屋を作成してください", error=True)
            return False
        path_text, _ = file_dialog_memory.get_open_file_name(
            self,
            "下図をインポート",
            'room.import_underlay',
            "下図ファイル (*.png *.jpg *.jpeg *.pdf *.dxf)",
        )
        if not path_text:
            return False
        try:
            underlay = self.controller.import_underlay(path_text)
        except (OSError, UnderlayImportError, ValueError) as exc:
            self._set_operation_error("下図の読み込みに失敗しました", exc)
            return False
        self._refresh_underlay_ui()
        self._set_status(
            f"下図「{underlay.name}」を読み込みました。"
            "校正するにはビューメニューから「2点で校正」を選んでください"
        )
        return True

    def arm_underlay_calibration(self, underlay_id: str) -> bool:
        try:
            self.controller.begin_underlay_calibration(underlay_id)
        except KeyError:
            return False
        self._refresh_underlay_ui()
        return True

    def arm_first_underlay_calibration(self) -> bool:
        """Command-palette entry: calibrate the first uncalibrated underlay,
        else the first underlay. Returns False when none exist."""

        underlays = self.controller.underlays()
        if not underlays:
            self._set_status("先に下図をインポートしてください", error=True)
            return False
        target = next(
            (u for u in underlays if not is_calibrated(u)),
            underlays[0],
        )
        return self.arm_underlay_calibration(target.underlay_id)

    def update_underlay_field(self, underlay_id: str, **fields) -> bool:
        try:
            self.controller.update_underlay(underlay_id, **fields)
        except (KeyError, ValueError) as exc:
            self._set_operation_error("下図を更新できませんでした", exc)
            return False
        self._refresh_underlay_ui()
        return True

    def delete_underlay(self, underlay_id: str) -> None:
        self.controller.delete_underlay_record(underlay_id)
        self._refresh_underlay_ui()
        self._set_status("下図を削除しました")

    def _finish_underlay_calibration(self) -> None:
        distance, ok = QInputDialog.getDouble(
            self,
            "2点校正",
            "選んだ2点間の実寸距離 (m):",
            2.0,
            0.01,
            100.0,
            2,
        )
        if not ok:
            self.controller.cancel_underlay_calibration()
            self._set_status("校正を中止しました")
            return
        try:
            underlay = self.controller.finish_underlay_calibration(distance)
        except (EditStateError, KeyError, ValueError) as exc:
            self._set_operation_error("校正を完了できませんでした", exc)
            self._refresh_underlay_ui()
            return
        self._refresh_underlay_ui()
        self._set_status(
            f"下図「{underlay.name}」を校正しました"
            f" ({underlay.units_per_meter:.3f} 単位/m)。"
            "トレースのスナップ候補が有効になりました"
        )

    def _rebuild_underlay_menu(self, menu: QMenu) -> None:
        menu.addSeparator()
        header = menu.addAction("下図（トレース用 — 設計データではありません）")
        header.setEnabled(False)
        action = menu.addAction("下図をインポート…")
        action.triggered.connect(
            lambda checked=False: self.import_underlay_dialog()
        )
        for underlay in self.controller.underlays():
            calibrated = is_calibrated(underlay)
            missing = self.controller.underlay_missing_source(underlay)
            sub = menu.addMenu(
                f"{underlay.name}"
                + ("" if calibrated else "（未校正）")
                + ("（データ欠落）" if missing else "")
            )
            shown = sub.addAction("表示")
            shown.setCheckable(True)
            shown.setChecked(underlay.visible)
            shown.triggered.connect(
                lambda checked=False, uid=underlay.underlay_id, value=not underlay.visible: (
                    self.update_underlay_field(uid, visible=value)
                )
            )
            locked = sub.addAction("ロック（スナップ対象から外す）")
            locked.setCheckable(True)
            locked.setChecked(underlay.locked)
            locked.triggered.connect(
                lambda checked=False, uid=underlay.underlay_id, value=not underlay.locked: (
                    self.update_underlay_field(uid, locked=value)
                )
            )
            calibrate = sub.addAction("2点で校正…")
            calibrate.triggered.connect(
                lambda checked=False, uid=underlay.underlay_id: (
                    self.arm_underlay_calibration(uid)
                )
            )
            manual = sub.addAction("縮尺を直接入力…")
            manual.triggered.connect(
                lambda checked=False, uid=underlay.underlay_id, u=underlay: (
                    self._set_underlay_scale(uid, u)
                )
            )
            delete = sub.addAction("削除")
            delete.triggered.connect(
                lambda checked=False, uid=underlay.underlay_id: (
                    self.delete_underlay(uid)
                )
            )

    def _set_underlay_scale(self, underlay_id: str, underlay: FloorPlanUnderlay) -> None:
        current = underlay.units_per_meter or 100.0
        value, ok = QInputDialog.getDouble(
            self,
            "縮尺の入力",
            "図面の 1 単位あたりのメートルではなく、1 m あたりの図面単位数を入力\n"
            "(例: 1px=1cm → 100、1mm単位 → 1000):",
            float(current),
            0.001,
            1_000_000.0,
            3,
        )
        if ok:
            self.update_underlay_field(underlay_id, units_per_meter=value)

    # --- Layout tools (#613) ----------------------------------------------------

    def _layout_selection(self, *, minimum: int) -> tuple[str, ...]:
        if not self.controller.can_edit:
            raise LayoutError("現在の状態では編集できません")
        selection = tuple(
            entity_id
            for entity_id in self.controller.view_state.selection
            if not self.controller.view_state.is_locked(entity_id)
        )
        if not selection and self.controller.selected_id is not None:
            candidate = self.controller.selected_id
            if not self.controller.view_state.is_locked(candidate):
                selection = (candidate,)
        if len(selection) < minimum:
            raise LayoutError(f"この操作には{minimum}つ以上の選択が必要です")
        return selection

    def layout_copy(self) -> bool:
        try:
            selection = self._layout_selection(minimum=1)
            self._clipboard = copy_selection(
                self.controller.document, selection
            )
        except (LayoutError, KeyError) as exc:
            self._set_operation_error("コピーできませんでした", exc)
            return False
        self._set_status(f"{len(self._clipboard.entities)}件をコピーしました")
        return True

    def layout_paste(self) -> bool:
        if self._clipboard is None or not self._clipboard.entities:
            self._set_status("先にコピーしてください", error=True)
            return False
        if not self.controller.can_edit:
            self._set_status("現在の状態では編集できません", error=True)
            return False
        try:
            new_ids = paste_clipboard(self.controller.working, self._clipboard)
        except (LayoutError, EditStateError) as exc:
            self._set_operation_error("貼り付けに失敗しました", exc)
            return False
        self.controller.view_state.set_selection(new_ids, primary_id=new_ids[0])
        self.controller._sync_recovery()
        self._refresh()
        self._set_status(f"{len(new_ids)}件を貼り付けました")
        return True

    def layout_duplicate(self) -> bool:
        try:
            selection = self._layout_selection(minimum=1)
            new_ids = duplicate_entities(
                self.controller.working,
                self.controller.document,
                selection,
                offset=(0.10, 0.10, 0.0),
            )
        except (LayoutError, EditStateError) as exc:
            self._set_operation_error("複製に失敗しました", exc)
            return False
        self.controller.view_state.set_selection(new_ids, primary_id=new_ids[0])
        self.controller._sync_recovery()
        self._refresh()
        self._set_status(f"{len(new_ids)}件を複製しました")
        return True

    def layout_mirror(self, axis: str) -> bool:
        try:
            selection = self._layout_selection(minimum=1)
            if axis == "x":
                ids = mirror_entities_x(
                    self.controller.working, self.controller.document, selection
                )
            else:
                ids = mirror_entities_y(
                    self.controller.working, self.controller.document, selection
                )
        except (LayoutError, EditStateError) as exc:
            self._set_operation_error("ミラーに失敗しました", exc)
            return False
        self.controller._sync_recovery()
        self._refresh()
        self._set_status(f"{len(ids)}件をミラーしました")
        return True

    def layout_pair_speaker(self) -> bool:
        selected = self.controller.selected_id
        if selected is None:
            self._set_status("スピーカーを選択してください", error=True)
            return False
        try:
            entity = self.controller.document.entity(selected)
        except KeyError:
            return False
        proposed = propose_pair_role(entity.speaker_role)
        apply_role = False
        if proposed is not None:
            answer = QMessageBox.question(
                self,
                "ペア複製",
                f"ミラーしたスピーカーの役割を「{proposed}」にしますか？\n"
                "(いいえを選ぶと元の役割のまま複製します)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            apply_role = answer == QMessageBox.StandardButton.Yes
        try:
            new_id = mirror_speaker_pair(
                self.controller.working,
                self.controller.document,
                selected,
                apply_role_proposal=apply_role,
            )
        except (LayoutError, EditStateError) as exc:
            self._set_operation_error("ミラーペアを作成できませんでした", exc)
            return False
        self.controller.view_state.set_selection((new_id,), primary_id=new_id)
        self.controller._sync_recovery()
        self._refresh()
        self._set_status("ペアスピーカーを作成しました")
        return True

    def layout_align(self, axis: str, mode: str) -> bool:
        try:
            selection = self._layout_selection(minimum=2)
            ids = align_entities(
                self.controller.working,
                self.controller.document,
                selection,
                axis=axis,  # type: ignore[arg-type]
                mode=mode,  # type: ignore[arg-type]
            )
        except (LayoutError, EditStateError) as exc:
            self._set_operation_error("整列に失敗しました", exc)
            return False
        self._propagate_and_report(set(ids))
        self._set_status(f"{len(ids)}件を揃えました")
        return True

    def layout_distribute(self, axis: str) -> bool:
        try:
            selection = self._layout_selection(minimum=3)
            ids = distribute_entities(
                self.controller.working,
                self.controller.document,
                selection,
                axis=axis,  # type: ignore[arg-type]
            )
        except (LayoutError, EditStateError) as exc:
            self._set_operation_error("分布に失敗しました", exc)
            return False
        self._propagate_and_report(set(ids))
        self._set_status(f"{len(ids)}件を等間隔に配置しました")
        return True

    def _propagate_and_report(self, changed_ids: set[str]) -> None:
        notes = self.controller.propagate_constraints(
            changed_ids, merge_with_previous=True
        )
        self.controller._sync_recovery()
        self._refresh()
        if notes:
            self._set_status(" / ".join(notes))

    # --- Authoring constraints (#618) --------------------------------------------

    def add_centerline_constraint(self, axis: str) -> bool:
        selection = tuple(self.controller.view_state.selection) or (
            (self.controller.selected_id,) if self.controller.selected_id else ()
        )
        if not selection:
            self._set_status("中心線に拘束する項目を選択してください", error=True)
            return False
        constraint = make_centerline_constraint(axis, selection)
        self.controller.add_authoring_constraint(constraint)
        self._propagate_and_report(set(selection))
        self._set_status("中心線拘束を追加しました")
        return True

    def add_symmetric_pair_constraint(self) -> bool:
        selection = tuple(self.controller.view_state.selection)
        if len(selection) != 2:
            self._set_status("対称にする2つの項目を選択してください", error=True)
            return False
        driver, subject = selection[0], selection[1]
        constraint = make_symmetric_pair_constraint(driver, subject)
        self.controller.add_authoring_constraint(constraint)
        self._propagate_and_report({driver})
        self._set_status("対称ペア拘束を追加しました")
        return True

    def add_fixed_distance_constraint(self, axis: str) -> bool:
        selection = tuple(self.controller.view_state.selection)
        if len(selection) != 2:
            self._set_status("距離を固定する2つの項目を選択してください", error=True)
            return False
        document = self.controller.document
        try:
            driver = document.entity(selection[0])
            subject = document.entity(selection[1])
        except KeyError:
            return False
        constraint = make_fixed_distance_constraint(driver, subject, axis=axis)
        self.controller.add_authoring_constraint(constraint)
        self._set_status(
            f"距離固定拘束を追加しました ({axis.upper()}方向 {abs(constraint.value or 0):.2f} m)"
        )
        return True

    def add_equal_spacing_constraint(self, axis: str | None = None) -> bool:
        selection = tuple(self.controller.view_state.selection)
        if len(selection) < 3:
            self._set_status("等間隔にする3つ以上の項目を選択してください", error=True)
            return False
        document = self.controller.document
        try:
            entities = [document.entity(entity_id) for entity_id in selection]
        except KeyError:
            return False
        if axis is None:
            # Palette-friendly form: pick the axis with the wider selection
            # spread — deterministic from geometry, never selection order.
            span_x = max(e.position.x_m for e in entities) - min(
                e.position.x_m for e in entities
            )
            span_y = max(e.position.y_m for e in entities) - min(
                e.position.y_m for e in entities
            )
            axis = "x" if span_x >= span_y else "y"
        ordered = sorted(
            entities,
            key=lambda e: e.position.x_m if axis == "x" else e.position.y_m,
        )
        constraint = make_equal_spacing_constraint(
            tuple(entity.entity_id for entity in ordered), axis=axis
        )
        self.controller.add_authoring_constraint(constraint)
        self._propagate_and_report({ordered[0].entity_id, ordered[-1].entity_id})
        self._set_status(f"{axis.upper()}方向の等間隔拘束を追加しました")
        return True

    def add_fixed_distance_auto(self) -> bool:
        """Palette-friendly fixed-distance: dominant axis of the pair."""

        selection = tuple(self.controller.view_state.selection)
        if len(selection) != 2:
            self._set_status("距離を固定する2つの項目を選択してください", error=True)
            return False
        document = self.controller.document
        try:
            a = document.entity(selection[0])
            b = document.entity(selection[1])
        except KeyError:
            return False
        dx = abs(a.position.x_m - b.position.x_m)
        dy = abs(a.position.y_m - b.position.y_m)
        return self.add_fixed_distance_constraint("x" if dx >= dy else "y")

    def remove_constraints_touching_selection(self) -> bool:
        """Remove constraints whose members include the current selection."""

        selection = set(self.controller.view_state.selection)
        if not selection and self.controller.selected_id is not None:
            selection = {self.controller.selected_id}
        if not selection:
            self._set_status("拘束を解除する項目を選択してください", error=True)
            return False
        removed = self.controller.remove_authoring_constraints_for(selection)
        self._refresh_underlay_ui()
        self._set_status(
            f"選択項目の拘束を{removed}件解除しました"
            if removed
            else "選択項目に関連する拘束はありません"
        )
        return removed > 0

    def remove_constraint(self, constraint_id: str) -> None:
        state = self.controller.authoring_constraints
        keep = tuple(
            constraint
            for constraint in state.constraints
            if constraint.constraint_id != constraint_id
        )
        self.controller.update_authoring_constraints(
            state.model_copy(
                update={"constraints": keep, "solve_version": state.solve_version + 1}
            )
        )
        self._refresh_underlay_ui()
        self._set_status("拘束を解除しました")

    def clear_broken_constraints(self) -> None:
        state = self.controller.authoring_constraints
        keep = tuple(c for c in state.constraints if not c.broken)
        removed = len(state.constraints) - len(keep)
        if removed:
            self.controller.update_authoring_constraints(
                state.model_copy(
                    update={"constraints": keep, "solve_version": state.solve_version + 1}
                )
            )
        self._refresh_underlay_ui()
        self._set_status(f"破損した拘束を{removed}件削除しました")

    def bind_reflection_guidance(self, panel, preferences) -> None:
        """Bind the reflection-guidance overlay to its panel + preference.

        ``panel`` is the ``ReflectionGuidancePanel`` mounted in the
        acoustics dock: its ``guidanceViewChanged`` signal re-projects the
        persisted path artifacts into viewport markers. ``preferences``
        supplies ``display_input.reflection_guidance_overlay``
        ('off' | 'auto' | 'on'); 'auto' draws markers only while the
        acoustics context is active — the context that owns the guidance
        panel — matching the app's convention that entering acoustics
        auto-enables its overlay. The store listener live-applies changes
        without a restart; it is released on destruction (the store
        outlives the composition).
        """

        key = 'display_input.reflection_guidance_overlay'
        self._guidance_overlay_mode = str(preferences.get(key))

        def _on_pref(change) -> None:
            if change.key == key:
                self._guidance_overlay_mode = str(change.new)
                self._render()

        preferences.subscribe(_on_pref)
        # Bound-method listeners can't receive ``destroyed`` (same PySide6
        # caveat as PreferencesWidget), so release goes through a lambda.
        self.destroyed.connect(
            lambda: preferences.unsubscribe(_on_pref)
        )
        panel.guidanceViewChanged.connect(
            lambda: self._on_guidance_view_changed(panel)
        )
        # The panel refreshed before this binding existed — pull its
        # already-loaded view so markers are honest from the first render.
        self._on_guidance_view_changed(panel)

    def _on_guidance_view_changed(self, panel) -> None:
        view = getattr(panel, 'guidance_view', None)
        self._guidance_view = (
            view if isinstance(view, ReflectionGuidanceView) else None
        )
        self._render()

    def _visible_guidance_markers(self, overlays: RoomOverlayState) -> tuple:
        """Markers the current overlay policy makes visible (REV40).

        The ガイド toggle (``guides_visible``) suppresses guidance markers
        alongside construction guides — a guides-hidden viewport stays
        clean even when the policy is 'on'.
        """

        if not overlays.guides_visible:
            return ()
        # Operator-invoked intake locate markers (#977) ride the same
        # overlay independent of the reflection-guidance mode/context
        # gates — they are explicit intent, not ambient guidance.
        locate = tuple(getattr(self, '_intake_locate_markers', ()))
        if self._guidance_view is None:
            return locate
        mode = self._guidance_overlay_mode
        if mode == 'off':
            return locate
        if mode == 'auto' and self.current_context != 'acoustics':
            return locate
        return guidance_overlay_markers(self._guidance_view) + locate

    def toggle_guides(self) -> bool:
        self._guides_visible = not self._guides_visible
        self._refresh_underlay_ui()
        self._set_status(
            "ガイドを表示しました" if self._guides_visible else "ガイドを隠しました"
        )
        return True

    def _rebuild_constraint_menu(self, menu: QMenu) -> None:
        menu.addSeparator()
        header = menu.addAction("拘束 / ガイド")
        header.setEnabled(False)
        guides = menu.addAction("ガイドを表示")
        guides.setCheckable(True)
        guides.setChecked(self._guides_visible)
        guides.triggered.connect(lambda checked=False: self.toggle_guides())
        has_selection = bool(self.controller.view_state.selection) or (
            self.controller.selected_id is not None
        )
        for label, callback in (
            ("左右中心線(X)に拘束", lambda: self.add_centerline_constraint("x")),
            ("前後中心線(Y)に拘束", lambda: self.add_centerline_constraint("y")),
            ("対称ペアにする (左右)", lambda: self.add_symmetric_pair_constraint()),
            ("X距離を固定", lambda: self.add_fixed_distance_constraint("x")),
            ("Y距離を固定", lambda: self.add_fixed_distance_constraint("y")),
            ("X方向を等間隔に", lambda: self.add_equal_spacing_constraint("x")),
            ("Y方向を等間隔に", lambda: self.add_equal_spacing_constraint("y")),
        ):
            action = menu.addAction(label)
            action.setEnabled(has_selection)
            action.triggered.connect(lambda checked=False, cb=callback: cb())
        constraints = self.controller.authoring_constraints.constraints
        if constraints:
            submenu = menu.addMenu("登録済みの拘束")
            for constraint in constraints:
                label = constraint.label or CONSTRAINT_KIND_LABELS.get(
                    constraint.kind, constraint.kind
                )
                if constraint.broken:
                    label = f"{label}（破損: {constraint.broken_reason or ''}）"
                sub = submenu.addMenu(label)
                remove = sub.addAction("この拘束を解除")
                remove.triggered.connect(
                    lambda checked=False, cid=constraint.constraint_id: (
                        self.remove_constraint(cid)
                    )
                )
            if any(c.broken for c in constraints):
                clear = menu.addAction("破損した拘束を削除")
                clear.triggered.connect(
                    lambda checked=False: self.clear_broken_constraints()
                )

    # --- Seating layout (#546) ---------------------------------------------------

    def seating_specs(self) -> tuple[tuple[str, SeatingLayoutSpec], ...]:
        specs: list[tuple[str, SeatingLayoutSpec]] = []
        for record in self.controller.repository.seating_specs(
            self.controller.document_id
        ):
            try:
                specs.append(
                    (record.record_id, SeatingLayoutSpec.model_validate(record.payload))
                )
            except (TypeError, ValueError):
                continue
        return tuple(specs)

    def open_seating_layout(self) -> bool:
        if self.controller.document.room is None:
            self._set_status("先に部屋を作成してください", error=True)
            return False
        existing = self.seating_specs()
        dialog = SeatingLayoutDialog(
            self,
            document=self.controller.document,
            existing=existing[0][1] if existing else None,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        spec = dialog.spec()
        if spec is None:
            return False
        diff = plan_regeneration(spec, self.controller.document)
        summary = (
            f"追加 {len(diff.added)} / 移動 {len(diff.moved_after)} / "
            f"未使用 {len(diff.removed)}"
        )
        remove_orphaned = False
        if diff.removed:
            answer = QMessageBox.question(
                self,
                "座席レイアウト",
                f"{summary}\n\nレイアウト外となった座席が{len(diff.removed)}件あります。"
                "削除しますか？（いいえを選ぶと座席は残ります — 証拠を持つ座席は削除されません）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            remove_orphaned = answer == QMessageBox.StandardButton.Yes
        try:
            apply_seating_layout(
                self.controller.working,
                self.controller.document,
                spec,
                remove_orphaned=remove_orphaned,
            )
        except (SeatingLayoutError, EditStateError) as exc:
            self._set_operation_error("座席レイアウトを適用できませんでした", exc)
            return False
        self.controller.repository.save_seating_spec(
            self.controller.document_id,
            spec.spec_id,
            spec.model_dump(mode="json"),
        )
        self.controller._sync_recovery()
        self._refresh()
        self._set_status(f"座席レイアウト「{spec.name}」を適用しました（{summary}）")
        return True


    def cancel_active_operation(self) -> bool:
        if self.controller.underlay_calibration_underlay_id is not None:
            self.controller.cancel_underlay_calibration()
            self._set_status("校正を中止しました")
            return True
        if self.measure_controller.is_active:
            self.measure_controller.cancel()
            self._render()
            return True
        if self.transform_input is not None and self.transform_input.is_active:
            return bool(self.transform_input.cancel())
        if self.geometry_input is not None and self.geometry_input.is_active:
            return bool(self.geometry_input.cancel())
        if self.controller.working.has_preview:
            changed = self.controller.working.cancel_preview()
            self._refresh()
            return changed
        if self.controller.view_state.selection:
            # CAD Esc chain: cancel the active gesture first, then fall back
            # to clearing the selection once nothing is in flight.
            self.clear_selection()
            return True
        return False

    def commit_active_operation(self) -> bool:
        if self.transform_input is not None and self.transform_input.is_active:
            return bool(self.transform_input.commit())
        if self.geometry_input is not None and self.geometry_input.is_active:
            return bool(self.geometry_input.commit())
        if self.controller.working.has_preview:
            changed = self.controller.working.commit_preview()
            if changed:
                self.controller._sync_recovery()
            self._refresh()
            return changed
        return False

    def constrain_axis(self, axis) -> None:
        if self.transform_input is not None and self.transform_input.is_active:
            self.transform_input.set_axis(axis)
            return
        value = getattr(axis, "value", str(axis))
        self.active_axis_constraint = value
        self._set_status(f"{str(value).upper()}軸に拘束")

    def set_context(self, context_id: str) -> None:
        if context_id not in ROOM_CONTEXT_IDS:
            raise ValueError(f"unknown Room context: {context_id}")
        self.current_context = context_id
        self.tools.set_context(context_id)
        self._palette_user_open = False
        if context_id != 'acoustics':
            # #999: field actors never leak into non-acoustics contexts;
            # the armed request stays so returning re-shows it.
            clear_field = getattr(self.viewport, 'clear_field_overlay', None)
            if callable(clear_field):
                clear_field()
        self._update_responsive_layout()
        if context_id == "geometry" and self._geometry_page is not None:
            self.right_stack.setCurrentWidget(self._geometry_page)
            refresh = getattr(self.geometry_panel, "refresh", None)
            if callable(refresh):
                refresh()
        elif context_id == "objects":
            self._sync_objects_panel()
            self.right_stack.setCurrentWidget(self.objects_page)
        elif context_id == "placement":
            self.system_expansion_panel.refresh()
            self.standards_panel.refresh_targets()
            self.standards_panel.refresh()
            self.installation_panel.refresh()
            self.rack_workspace_panel.refresh()
            self._sync_constraints_panel()
            self._sync_video_panel()
            self._sync_seat_priority_panel()
            # Lighting-scene identity line must be current on entry, not
            # only after the first _refresh (#1013).
            self._current_lighting_scene()
            self.right_stack.setCurrentWidget(self.placement_panel)
        elif context_id == "acoustics":
            self.overlay_controls.acoustics.setChecked(True)
            if self._acoustics_page is not None:
                self.right_stack.setCurrentWidget(self._acoustics_page)
                refresh = getattr(self.acoustics_panel, "refresh", None)
                if callable(refresh):
                    refresh()
            else:
                self._sync_objects_panel()
                self.right_stack.setCurrentWidget(self.objects_page)
        elif context_id == "history":
            self._sync_history_panel()
            self.right_stack.setCurrentWidget(self.history_page)
        else:
            self.right_stack.setCurrentWidget(self.objects_page)
        self._refresh_journey()
        self._render()

    def _proposal_variant_changed(self, variant_id: str) -> None:
        self._proposed_variant_id = variant_id
        self._proposed_selected_id = None
        self._render()

    def _system_expansion_apply_block_reason(self) -> str | None:
        """Veto for variant apply: a new head must never orphan a draft."""

        if self.controller.recovery_candidate is not None:
            return "復旧可能な下書きを処理してから提案を適用してください"
        if self.controller.working.has_preview:
            return "プレビュー中は提案を適用できません"
        if self.controller.is_dirty:
            return "未保存の変更を保存または破棄してから提案を適用してください"
        return None

    def _proposal_entity_selected(self, entity_id: object) -> None:
        self._proposed_selected_id = str(entity_id)
        self._render()

    def select_entity(self, entity_id: object) -> None:
        target = str(entity_id) if entity_id is not None else None
        try:
            self.controller.set_selection(target)
        except KeyError:
            return
        self._after_selection_changed()

    def _entity_picked(self, entity_id: object, display_position: object = None) -> None:
        """Viewport pick: measure capture, Ctrl+click additive select, plain click."""
        target = str(entity_id)
        if self.field_overlay.probe_armed:
            self._field_probe_at(display_position)
            return
        if self.measure_controller.is_active:
            self.measure_controller.pick_entity(target, display_position)
            self._suppress_next_select = True
            return
        modifiers = QGuiApplication.keyboardModifiers()
        additive = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        self._suppress_next_select = additive
        try:
            if additive:
                self.controller.toggle_selection(target)
            else:
                self.controller.set_selection(target)
        except KeyError:
            return
        self._after_selection_changed()

    def _entity_selected_compat(self, entity_id: object) -> None:
        """Fake/legacy viewports emit only entitySelected — plain-click path."""
        if self._suppress_next_select:
            self._suppress_next_select = False
            return
        self._entity_picked(entity_id, None)

    def _entities_marquee_selected(self, entity_ids: object, additive: object = False) -> None:
        """Left-drag marquee: replace selection, or Shift-extend it.

        Measure mode swallows the gesture — a region of space is not an
        endpoint, so the marquee is inert there rather than misfiring.
        """
        if self.measure_controller.is_active:
            return
        ids = [str(item) for item in entity_ids]
        self.controller.set_selection_many(ids, additive=bool(additive))
        self._after_selection_changed()

    def _empty_clicked(self, display_position: object) -> None:
        """Click on empty space: measure free-point or clear selection."""
        if self.field_overlay.probe_armed:
            self._field_probe_at(display_position)
            return
        if self.measure_controller.is_active:
            self.measure_controller.pick_free_point(display_position)
            return
        self.select_entity(None)

    # -- 3D field overlay (#999) ---------------------------------------------

    def show_field_overlay_3d(self, request: FieldOverlay3DRequest) -> None:
        """Arm the field overlay from the explorer panel ('音場を3D表示')."""

        session = self.field_overlay.field_repository.get(request.session_id)
        if session is None or session.document_id != self.controller.document.document_id:
            self._set_status('このプロジェクトの音場セッションではありません')
            return
        self.field_overlay.set_request(request)
        self._field_probe_esc.setEnabled(request.probe_enabled)
        if self.current_context != 'acoustics':
            self.set_context('acoustics')
        elif not self.overlay_controls.acoustics.isChecked():
            self.overlay_controls.acoustics.setChecked(True)
        self._render()

    def clear_field_overlay_3d(self) -> None:
        self.field_overlay.clear()
        self._field_probe_esc.setEnabled(False)
        clear_field = getattr(self.viewport, 'clear_field_overlay', None)
        if callable(clear_field):
            clear_field()
        self._render()

    def _disarm_field_probe(self) -> None:
        if not self.field_overlay.probe_armed:
            return
        self.field_overlay.disarm_probe()
        self._field_probe_esc.setEnabled(False)
        self.field3DProbeDisarmed.emit()
        self._render()

    def _field_probe_at(self, display_position) -> None:
        if display_position is None:
            return
        probe_at = getattr(self.viewport, 'field_probe_world', None)
        world = (
            probe_at(display_position)
            if callable(probe_at)
            else self.viewport.pick_world_position(display_position)
        )
        if world is None:
            return
        self._set_status(f'音場プローブ: {self.field_overlay.probe_world(world)}')
        self._render()

    def _after_selection_changed(self) -> None:
        self._refresh_inspector()
        self._sync_objects_panel()
        installation_panel = getattr(self, 'installation_panel', None)
        if installation_panel is not None:
            installation_panel.set_selected_entity(
                self.controller.view_state.selected_id
            )
        if self.acoustics_panel is not None and self.current_context == "acoustics":
            refresh = getattr(self.acoustics_panel, "refresh", None)
            if callable(refresh):
                refresh()
        self._render()

    # -- snap preferences (#481) --------------------------------------------------

    def _snap_controls_changed(self) -> None:
        self.controller.set_snap_preferences(
            object_snap=self.overlay_controls.object_snap.isChecked(),
            grid_snap=self.overlay_controls.grid_snap_action.isChecked(),
            grid_step_m=float(self.overlay_controls.grid_step_spin.value()),
            angle_snap=self.overlay_controls.angle_snap_action.isChecked(),
            angle_step_deg=float(self.overlay_controls.angle_step_spin.value()),
        )
        self._set_status("スナップ設定を保存しました")

    def _sync_snap_controls(self) -> None:
        vs = self.controller.view_state
        controls = self.overlay_controls
        with QSignalBlocker(controls.object_snap):
            controls.object_snap.setChecked(vs.object_snap_enabled)
        with QSignalBlocker(controls.grid_snap_action):
            controls.grid_snap_action.setChecked(vs.grid_snap_enabled)
        with QSignalBlocker(controls.angle_snap_action):
            controls.angle_snap_action.setChecked(vs.angle_snap_enabled)
        with QSignalBlocker(controls.grid_step_spin):
            controls.grid_step_spin.setValue(vs.grid_step_m)
        with QSignalBlocker(controls.angle_step_spin):
            controls.angle_step_spin.setValue(vs.angle_step_deg)

    def set_snap_feedback(self, label: str | None) -> None:
        render = getattr(self.viewport, "render_snap_feedback", None)
        if callable(render):
            render(label)
        if label:
            self._set_status(f"吸着: {label}")

    # -- objects panel (#480/#482) -----------------------------------------------------

    def _sync_objects_panel(self) -> None:
        if self.controller.document is None:
            return
        self.objects_panel.sync_document(
            self.controller.document,
            selected_ids=tuple(self.controller.view_state.selection),
            primary_id=self.controller.view_state.selected_id,
            hidden_ids=frozenset(self.controller.view_state.hidden_ids),
            locked_ids=frozenset(self.controller.view_state.locked_ids),
            kind_labels=SelectionInspector.KIND_LABELS,
        )

    def _objects_selection(self, entity_ids: object, primary_id: object) -> None:
        ids = tuple(str(eid) for eid in entity_ids)
        try:
            self.controller.view_state.set_selection(
                ids, primary_id=str(primary_id) if primary_id is not None else None
            )
            self.controller._persist_view_state()
        except (KeyError, ValueError):
            return
        self._after_selection_changed()

    def _objects_hidden(self, entity_ids: object, hidden: object) -> None:
        changed = self.controller.set_entities_hidden(
            tuple(str(eid) for eid in entity_ids), bool(hidden)
        )
        if changed:
            self._refresh()
            self._set_status(
                f"{changed} 項目を非表示にしました" if hidden else f"{changed} 項目を表示しました"
            )

    def _objects_locked(self, entity_ids: object, locked: object) -> None:
        changed = self.controller.set_entities_locked(
            tuple(str(eid) for eid in entity_ids), bool(locked)
        )
        if changed:
            self._refresh()
            self._set_status(
                f"{changed} 項目をロックしました" if locked else f"{changed} 項目のロックを解除しました"
            )

    def _objects_delete(self, entity_ids: object) -> None:
        ids = tuple(str(eid) for eid in entity_ids)
        locked = [
            self.controller.document.entity(eid).name
            for eid in ids
            if eid in {entity.entity_id for entity in self.controller.document.entities}
            and self.controller.view_state.is_locked(eid)
        ]
        if locked:
            self._set_status(
                f"ロック中の項目は削除できません: {'、'.join(locked)}",
                error=True,
            )
            return
        try:
            changed = self.controller.delete_entities(ids)
        except EditStateError as exc:
            self._set_operation_error("項目を削除できませんでした", exc, effect='変更は保存されていません')
            return
        if changed:
            self._refresh()
            self._set_status(f"{changed} 項目を削除しました（元に戻せます）")

    # -- constraints (#486) ------------------------------------------------------------

    def _sync_constraints_panel(self) -> None:
        if self.controller.document is None:
            return
        self.constraints_panel.set_walls(self.controller.document)
        try:
            evaluation = self.controller.evaluate_constraints()
        except EXPECTED_OPERATION_ERRORS as exc:  # evaluator raises on malformed constraints
            if is_authority_failure(exc):
                raise
            evaluation = None
            self.constraints_panel.sync_state(
                self.controller.constraint_set,
                None,
                self.controller.document,
                evaluate_error=operation_error_message(exc),
            )
            return
        self.constraints_panel.sync_state(
            self.controller.constraint_set,
            evaluation,
            self.controller.document,
        )

    def _constraint_action(self, action_id: object) -> None:
        kind = str(action_id)
        selected = tuple(self.controller.view_state.selection)
        constraint_set = self.controller.constraint_set
        if constraint_set is None:
            self._set_status("制約セットを読み込めません", error=True)
            return
        document = self.controller.document
        try:
            if kind == "add_walkway":
                if not selected:
                    self._set_status("通路の対象を選択してください", error=True)
                    return
                new_set = add_constraint(
                    constraint_set,
                    make_walkway_constraint(document, selected[0]),
                )
            elif kind == "add_allowed":
                if not selected:
                    self._set_status("許可領域の対象を選択してください", error=True)
                    return
                new_set = add_constraint(
                    constraint_set,
                    make_allowed_region_constraint(document, selected[0]),
                )
            elif kind == "add_wall":
                if not selected:
                    self._set_status("壁離隔の対象を選択してください", error=True)
                    return
                new_set = add_constraint(
                    constraint_set,
                    make_wall_clearance_constraint(
                        document,
                        selected[0],
                        wall_id=self.constraints_panel.selected_wall_id(),
                        min_m=self.constraints_panel.distance_m(),
                    ),
                )
            elif kind == "add_pair":
                if len(selected) < 2:
                    self._set_status("物体間離隔は2項目を選択してください", error=True)
                    return
                new_set = add_constraint(
                    constraint_set,
                    make_pair_distance_constraint(
                        document,
                        selected[0],
                        selected[1],
                        min_m=self.constraints_panel.distance_m(),
                    ),
                )
            elif kind == "delete":
                constraint_id = self.constraints_panel.selected_constraint_id()
                if constraint_id is None:
                    self._set_status("削除する制約を選択してください", error=True)
                    return
                new_set = remove_constraint(constraint_set, constraint_id)
            else:
                return
            self.controller.update_placement_constraints(new_set)
        except (EditStateError, ValueError) as exc:
            self._set_operation_error("拘束を保存できませんでした", exc, effect='変更は保存されていません')
            return
        self._sync_constraints_panel()
        self._render()
        self._set_status("制約を更新しました")

    def _constraint_highlight(self, result: object) -> None:
        if result is None:
            self._render()
            return
        entity_ids = list(getattr(result, "entity_ids", ()) or ())
        name = getattr(result, "name", "") or getattr(result, "reason_ja", "")
        self._set_status(f"制約: {name}")
        if entity_ids:
            self.select_entity(entity_ids[0])

    # -- video geometry (#455) ---------------------------------------------------------

    def _video_workspace_from_panel(self) -> VideoGeometryWorkspace:
        workspace = self.controller.video_workspace or VideoGeometryWorkspace(
            document_id=self.controller.document_id
        )
        target_type = self.video_panel.current_target_type()
        screens = [
            entity
            for entity in self.controller.document.entities
            if entity.kind == "screen"
        ]
        screen_bindings = dict(workspace.screen_bindings)
        if screens and target_type == 'projection':
            values = self.video_panel.current_screen_values()
            screen_transfer = self.screen_transfer_repository.selected_transfer(
                self.controller.document_id, screens[0].entity_id
            )
            screen_bindings[screens[0].entity_id] = ScreenGeometryBinding(
                entity_id=screens[0].entity_id,
                visible_width_m=float(values["visible_width_m"]),
                visible_height_m=float(values["visible_height_m"]),
                image_center_offset_local_m=Offset3(
                    x_m=float(values["image_center_offset_x_m"]),
                    y_m=0.0,
                    z_m=float(values["image_center_offset_z_m"]),
                ),
                frame_clearance_m=float(values["frame_clearance_m"]),
                screen_transfer_ref=(
                    None
                    if screen_transfer is None
                    else screen_transfer.authority_ref()
                ),
            )
        seat_bindings = dict(workspace.seat_bindings)
        seat_pose_refs: dict[str, ExactExternalAuthorityRef] = {}
        seat_values = self.video_panel.current_seat_bindings()
        for entity in self.controller.document.entities:
            if entity.kind != "seat":
                continue
            values = seat_values.get(entity.entity_id)
            pose = self.listener_pose_repository.selected_pose(
                self.controller.document_id, entity.entity_id
            )
            if pose is not None:
                # A bound pose is the eye/head authority (#632): the binding
                # derives from the pose's offsets, not the manual spins.
                seat_pose_refs[entity.entity_id] = pose.authority_ref()
                seat_bindings[entity.entity_id] = seat_binding_from_pose(
                    pose,
                    row_id=str(values["row_id"]) if values is not None else 'row-1',
                    riser_entity_id=(
                        values["riser_entity_id"] if values is not None else None
                    ),
                )
                continue
            if values is None:
                # #1056: never invent occupant geometry — a seat with no
                # pose and no explicit manual values simply isn't bound;
                # video_workspace_missing_inputs reports the gap.
                continue
            seat_bindings[entity.entity_id] = SeatGeometryBinding(
                entity_id=entity.entity_id,
                row_id=str(values["row_id"]),
                eye_reference_offset_local_m=Offset3(
                    x_m=0.0, y_m=0.0, z_m=float(values["eye_z_m"])
                ),
                head_center_offset_local_m=Offset3(
                    x_m=0.0, y_m=0.0, z_m=float(values["head_z_m"])
                ),
                head_radius_m=float(values["head_radius_m"]),
                riser_entity_id=values["riser_entity_id"],
                geometry_source='manual',
            )
        for stale_id in list(seat_bindings):
            if stale_id not in seat_values and all(
                e.entity_id != stale_id
                for e in self.controller.document.entities
                if e.kind == "seat"
            ):
                del seat_bindings[stale_id]
        for stale_id in list(screen_bindings):
            if all(
                e.entity_id != stale_id
                for e in self.controller.document.entities
                if e.kind == "screen"
            ):
                del screen_bindings[stale_id]
        policy_values = self.video_panel.current_policy_values()
        policy = workspace.policy.model_copy(
            update={
                "sightline_clearance_m": policy_values["sightline_clearance_m"],
                "max_optical_axis_deviation_deg": policy_values[
                    "max_optical_axis_deviation_deg"
                ],
            }
        )
        # #1054: a direct-view target binds a display entity + its active
        # aperture — projector/screen fields stay untouched for switching back.
        display_binding = workspace.display_binding
        display_entity_id = self.video_panel.current_display_entity_id()
        if target_type == 'direct_view' and display_entity_id is not None:
            display_values = self.video_panel.current_display_values()
            display_binding = DisplayGeometryBinding(
                entity_id=display_entity_id,
                visible_width_m=float(display_values["visible_width_m"]),
                visible_height_m=float(display_values["visible_height_m"]),
                image_center_offset_local_m=Offset3(
                    x_m=float(display_values["image_center_offset_x_m"]),
                    y_m=0.0,
                    z_m=float(display_values["image_center_offset_z_m"]),
                ),
                frame_clearance_m=float(display_values["frame_clearance_m"]),
                mounting=display_values["mounting"],
            )
        return workspace.model_copy(
            update={
                "target_type": target_type,
                "projector_entity_id": self.video_panel.current_projector_entity_id(),
                "projector_specification_sha256": self.video_panel.current_specification_sha256(),
                "screen_bindings": screen_bindings,
                "display_entity_id": display_entity_id,
                "display_specification_sha256": (
                    self.video_panel.current_display_specification_sha256()
                ),
                "display_binding": display_binding,
                "seat_bindings": seat_bindings,
                "seat_pose_refs": seat_pose_refs,
                "policy": policy,
            }
        )

    def _seat_pose_changed(self, seat_id: str, pose_id: object) -> None:
        """Persist/clear the selected listener pose for a seat (#632) and
        materialize its offsets into the seat card spins so what the user
        sees is exactly what the pose authority says."""
        seat = next(
            (
                entity
                for entity in self.controller.document.entities
                if entity.entity_id == seat_id and entity.kind == "seat"
            ),
            None,
        )
        if seat is None:
            return
        if pose_id is None:
            self.listener_pose_repository.clear_selection(
                self.controller.document_id, seat_id
            )
        else:
            pose = self.listener_pose_repository.get_pose(str(pose_id))
            if pose is None:
                return
            self.listener_pose_repository.select_pose(
                self.controller.document_id, pose
            )
            widgets = self.video_panel._seat_widgets.get(seat_id)
            if widgets is not None:
                # Guard: spin writes must not read as a manual customization
                # that would clear the pose selection just stored.
                self.video_panel._syncing = True
                try:
                    widgets['eye_z'].spin.set_value_m(
                        pose.eye_reference_offset_local_m.z_m
                    )
                    widgets['head_z'].spin.set_value_m(
                        pose.head_center_offset_local_m.z_m
                    )
                    widgets['head_r'].spin.set_value_m(pose.head_radius_m)
                finally:
                    self.video_panel._syncing = False
        self._video_bindings_changed()

    def _screen_transfer_changed(
        self, screen_id: str, transfer_id: object
    ) -> None:
        """Persist/clear the selected screen-transfer authority (#541)."""
        screen = next(
            (
                entity
                for entity in self.controller.document.entities
                if entity.entity_id == screen_id and entity.kind == "screen"
            ),
            None,
        )
        if screen is None:
            return
        if transfer_id is None:
            self.screen_transfer_repository.clear_selection(
                self.controller.document_id, screen_id
            )
        else:
            transfer = self.screen_transfer_repository.get_transfer(
                str(transfer_id)
            )
            if transfer is None:
                return
            self.screen_transfer_repository.select_transfer(
                self.controller.document_id, transfer
            )
        self._video_bindings_changed()

    def _screen_transfer_saved(self, screen_id: str) -> None:
        """Register a new screen-transfer authority bound to the screen
        (#541) from the typed dialog values — sample rows are parsed
        strictly; malformed input is rejected, not guessed."""
        screen = next(
            (
                entity
                for entity in self.controller.document.entities
                if entity.entity_id == screen_id and entity.kind == "screen"
            ),
            None,
        )
        if screen is None:
            return
        dialog = ScreenTransferDialog(self)
        if dialog.exec() != ScreenTransferDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        samples: list[TransferSample] = []
        for line_number, line in enumerate(
            str(values['samples_text']).splitlines(), start=1
        ):
            line = line.strip()
            if not line:
                continue
            parts = [part.strip() for part in line.split(',')]
            try:
                fields = [float(part) for part in parts]
            except ValueError:
                self._set_status(
                    f"サンプル{line_number}行目が数値ではありません", error=True
                )
                return
            try:
                samples.append(
                    TransferSample(
                        frequency_hz=fields[0],
                        incidence_angle_deg=(
                            fields[1] if len(fields) > 1 else None
                        ),
                        magnitude=fields[2] if len(fields) > 2 else None,
                        phase_deg=fields[3] if len(fields) > 3 else None,
                        reflection_magnitude=(
                            fields[4] if len(fields) > 4 else None
                        ),
                    )
                )
            except ValueError as exc:
                self._set_status(
                    f"サンプル{line_number}行目が不正です: {operation_error_message(exc)}",
                    error=True,
                )
                return
        try:
            transfer = build_screen_transfer(
                screen_entity_id=screen_id,
                document_id=self.controller.document_id,
                label=str(values['label']),
                capability_tier=values['capability_tier'],
                provenance=str(values['provenance']),
                transfer_samples=tuple(samples),
                valid_frequency_domain=(
                    FrequencyDomain(
                        minimum_hz=float(values['frequency_minimum_hz']),
                        maximum_hz=float(values['frequency_maximum_hz']),
                    )
                    if samples
                    else None
                ),
                measurement_condition=str(values['measurement_condition']),
                notes=str(values['notes']),
            )
        except ValueError as exc:
            self._set_operation_error("転送仕様を登録できませんでした", exc, effect='変更は保存されていません')
            return
        self.screen_transfer_repository.save_transfer(transfer)
        self.screen_transfer_repository.select_transfer(
            self.controller.document_id, transfer
        )
        self._sync_video_panel()
        self._video_bindings_changed()
        self._set_status(
            f"伝達権威 '{transfer.label}' を登録しました "
            f"({transfer_capability_label(transfer.capability_tier)})"
        )

    def _seat_pose_saved(self, seat_id: str) -> None:
        """Materialize the seat card's current offsets as a sealed
        ListenerPoseAuthority bound to the seat (#632), then select it."""
        seat = next(
            (
                entity
                for entity in self.controller.document.entities
                if entity.entity_id == seat_id and entity.kind == "seat"
            ),
            None,
        )
        if seat is None:
            return
        if seat.acoustic_reference_offset_m is None:
            self._set_status(
                "この座席には音響基準点がありません — 先にリスニング位置を定義してください",
                error=True,
            )
            return
        label, ok = QInputDialog.getText(
            self, "ポーズ保存", "ポーズ名:", text=f"{seat.name} ポーズ"
        )
        if not ok:
            return
        if not label.strip():
            self._set_status("ポーズ名を入力してください", error=True)
            return
        widgets = self.video_panel._seat_widgets.get(seat_id)
        if widgets is None:
            return
        pose = listener_pose_for_seat(
            seat,
            document_id=self.controller.document_id,
            label=label.strip(),
            eye_reference_offset_local_m=Offset3(
                x_m=0.0, y_m=0.0, z_m=float(widgets['eye_z'].spin.value_m())
            ),
            head_center_offset_local_m=Offset3(
                x_m=0.0, y_m=0.0, z_m=float(widgets['head_z'].spin.value_m())
            ),
            head_radius_m=float(widgets['head_r'].spin.value_m()),
            provenance='部屋映像パネルで作成 (UX120)',
        )
        self.listener_pose_repository.save_pose(pose)
        self.listener_pose_repository.select_pose(
            self.controller.document_id, pose
        )
        self._sync_video_panel()
        self._video_bindings_changed()
        self._set_status(f"ポーズ '{pose.label}' を保存しました")

    def _video_bindings_changed(self) -> None:
        if self.video_panel._syncing:
            return
        workspace = self._video_workspace_from_panel()
        self.controller.save_video_workspace(workspace)
        missing = video_workspace_missing_inputs(
            self.controller.committed_document, workspace
        )
        self.video_panel.show_message(
            "未設定: " + "、".join(missing) if missing else "評価できます"
        )

    def _sync_video_panel(self) -> None:
        workspace = self.controller.video_workspace or VideoGeometryWorkspace(
            document_id=self.controller.document_id
        )
        specifications = (
            self.controller.video_geometry_repository.list_projector_specifications()
        )
        variants = self.controller.variant_repository.list_variants(
            self.controller.document_id
        )
        if self.controller.document is None:
            return
        seat_names = {
            entity.entity_id: entity.name
            for entity in self.controller.document.entities
            if entity.kind == "seat"
        }
        poses_by_seat = self.listener_pose_repository.list_poses_for_seats(
            tuple(seat_names)
        )
        selected_by_seat = (
            self.listener_pose_repository.selected_poses_for_document(
                self.controller.document_id
            )
        )
        seat_poses = {}
        for seat_id in seat_names:
            poses = poses_by_seat.get(seat_id, ())
            selected = selected_by_seat.get(seat_id)
            seat_poses[seat_id] = (
                tuple((pose.label, pose.pose_id) for pose in poses),
                None if selected is None else selected.pose_id,
            )
        screen_transfers = None
        screens = [
            entity
            for entity in self.controller.document.entities
            if entity.kind == "screen"
        ]
        if screens:
            transfers = self.screen_transfer_repository.list_transfers_for_screen(
                screens[0].entity_id
            )
            selected_transfer = (
                self.screen_transfer_repository.selected_transfer(
                    self.controller.document_id, screens[0].entity_id
                )
            )
            screen_transfers = (
                tuple(
                    (
                        f"{item.label} · {transfer_capability_label(item.capability_tier)}",
                        item.transfer_id,
                    )
                    for item in transfers
                ),
                (
                    None
                    if selected_transfer is None
                    else selected_transfer.transfer_id
                ),
            )
        self.video_panel.sync_document(
            self.controller.document,
            workspace,
            specifications,
            variants,
            seat_names,
            seat_poses,
            screen_transfers,
            self.controller.direct_view_repository.list_specifications(),
        )
        missing = video_workspace_missing_inputs(
            self.controller.committed_document, workspace
        )
        self.video_panel.show_message(
            "未設定: " + "、".join(missing) if missing else "評価できます"
        )

    def _sync_seat_priority_panel(self) -> None:
        # The panel rebuilds its member rows from the committed head —
        # same contract the legacy TheaterEditorWindow dock followed.
        self.seat_priority_panel.refresh(self.controller.committed_document)

    def _video_evaluate(self, variant_id: object) -> None:
        self._video_bindings_changed()
        try:
            evaluation = self.controller.evaluate_video(
                None if variant_id is None else str(variant_id)
            )
        except (EditStateError, ValueError) as exc:
            self._set_operation_error("映像を評価できませんでした", exc)
            self.video_panel.show_message(operation_error_message(exc))
            return
        self._video_evaluation = evaluation
        self.video_panel.show_evaluation(evaluation)
        self.video_panel.show_message("評価しました（映像面/視線/衝突）")
        self._render()

    def _video_create_spec(self) -> None:
        dialog = ProjectorSpecDialog(self)
        if dialog.exec() != ProjectorSpecDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        if not values["specification_id"]:
            self._set_status("仕様IDを入力してください", error=True)
            return
        try:
            h_shift = values["horizontal_lens_shift"]
            v_shift = values["vertical_lens_shift"]
            optical = projector_spec_optical_values(
                lens_reference_offset_m=Offset3(),
                optical_axis_local=Direction3(x=0.0, y=-1.0, z=0.0),
                throw_ratio_min=float(values["throw_ratio_min"]),
                throw_ratio_max=float(values["throw_ratio_max"]),
                horizontal_lens_shift=(
                    None
                    if h_shift is None
                    else LensShiftRange(min=float(h_shift[0]), max=float(h_shift[1]))
                ),
                vertical_lens_shift=(
                    None
                    if v_shift is None
                    else LensShiftRange(min=float(v_shift[0]), max=float(v_shift[1]))
                ),
            )
            evidence = build_projector_spec_manual_evidence(
                publisher=str(values["publisher"]),
                document_title=str(values["document_title"]),
                document_version=str(values["version"]),
                reference=str(values["reference"]),
                field_assertions=build_projector_spec_field_assertions(
                    optical_values=optical,
                    field_locators={
                        field: str(values["source_citation"])
                        for field in PROJECTOR_SPEC_EVIDENCED_FIELDS
                    },
                ),
                source_citation=str(values["source_citation"]),
                actor=str(values["actor"]),
                evidence_basis="user_measurement",
            )
            specification = build_projector_specification(
                specification_id=str(values["specification_id"]),
                version=str(values["version"]),
                provenance=ProjectorSpecificationProvenance(
                    source_kind="user_defined",
                    publisher=str(values["publisher"]),
                    document_title=str(values["document_title"]),
                    document_version=str(values["version"]),
                    reference=str(values["reference"]),
                    evidence=evidence.ref(),
                ),
                lens_reference_offset_m=Offset3(),
                optical_axis_local=Direction3(x=0.0, y=-1.0, z=0.0),
                throw_ratio_min=float(values["throw_ratio_min"]),
                throw_ratio_max=float(values["throw_ratio_max"]),
                horizontal_lens_shift=(
                    None
                    if h_shift is None
                    else LensShiftRange(min=float(h_shift[0]), max=float(h_shift[1]))
                ),
                vertical_lens_shift=(
                    None
                    if v_shift is None
                    else LensShiftRange(min=float(v_shift[0]), max=float(v_shift[1]))
                ),
            )
            self.controller.video_geometry_repository.save_projector_specification(
                specification, evidence=evidence
            )
        except (ValueError, KeyError) as exc:
            self._set_operation_error('仕様を登録できませんでした', exc, effect='変更は保存されていません')
            return
        self._sync_video_panel()
        index = self.video_panel.spec_combo.findData(
            specification.specification_sha256
        )
        if index >= 0:
            self.video_panel.spec_combo.setCurrentIndex(index)
        self._set_status(f"仕様を登録しました: {specification.specification_id}")

    def _video_create_display_spec(self) -> None:
        """Register a user-defined direct-view display specification (#1054)."""
        dialog = DisplaySpecDialog(
            self, length_policy=self.video_panel.length_policy()
        )
        if dialog.exec() != DisplaySpecDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        if not values["specification_id"]:
            self._set_status("仕様IDを入力してください", error=True)
            return
        source_name = str(values["source_name"])
        source_version = str(values["version"])
        source_reference = str(values["source_reference"])
        provenance = (
            EquipmentDataProvenance(
                evidence_kind='user_defined',
                source_name=source_name,
                source_version=source_version,
                source_reference=source_reference,
                source_sha256=sha256(
                    f"{source_name}|{source_version}|{source_reference}".encode(
                        'utf-8'
                    )
                ).hexdigest(),
            ),
        )
        try:
            specification = build_direct_view_display_specification(
                specification_id=str(values["specification_id"]),
                version=str(values["version"]),
                manufacturer=None,
                model=None,
                user_label=str(values["user_label"]),
                display_class=str(values["display_class"]),
                chassis_size_m=Size3(
                    x_m=float(values["chassis_width_m"]),
                    y_m=float(values["chassis_depth_m"]),
                    z_m=float(values["chassis_height_m"]),
                ),
                active_image_width_m=float(values["active_image_width_m"]),
                active_image_height_m=float(values["active_image_height_m"]),
                provenance=provenance,
            )
            self.controller.direct_view_repository.save_specification(
                specification
            )
        except (ValueError, KeyError) as exc:
            self._set_operation_error(
                '仕様を登録できませんでした', exc, effect='変更は保存されていません'
            )
            return
        self._sync_video_panel()
        index = self.video_panel.display_spec_combo.findData(
            specification.specification_sha256
        )
        if index >= 0:
            self.video_panel.display_spec_combo.setCurrentIndex(index)
        self._set_status(f"ディスプレイ仕様を登録しました: {specification.specification_id}")

    def _view_from_seat(self, seat_id: object) -> None:
        """View-from-seat camera bound to the seat's eye authority (#455)."""
        view_from = getattr(self.viewport, "view_from", None)
        capture = getattr(self.viewport, "capture_camera_view", None)
        apply_view = getattr(self.viewport, "apply_camera_view", None)
        if seat_id is None:
            if self._saved_camera_view is not None and callable(apply_view):
                apply_view(self._saved_camera_view)
                self._saved_camera_view = None
                self._set_status("カメラを戻しました")
            return
        if not callable(view_from) or not callable(capture):
            self._set_status("このビューポートは座席視点に対応していません", error=True)
            return
        workspace = self.controller.video_workspace
        binding = None if workspace is None else workspace.seat_bindings.get(str(seat_id))
        if binding is None:
            self._set_status("座席に眼/頭バインドを設定してください", error=True)
            return
        try:
            seat = self.controller.document.entity(str(seat_id))
            eye = seat_eye_world(seat, binding)
        except (KeyError, ValueError) as exc:
            self._set_operation_error("座席の視点を取得できませんでした", exc)
            return
        target = None
        if workspace is not None and workspace.target_type == 'direct_view':
            # #1054: a direct-view target aims at the display image centre.
            display_entity = None
            if workspace.display_entity_id is not None:
                try:
                    display_entity = self.controller.document.entity(
                        workspace.display_entity_id
                    )
                except KeyError:
                    display_entity = None
            if display_entity is not None and workspace.display_binding is not None:
                try:
                    target = display_image_center_world(
                        display_entity, workspace.display_binding
                    )
                except ValueError:
                    target = None
        screens = [
            entity
            for entity in self.controller.document.entities
            if entity.kind == "screen"
        ]
        if target is None and workspace is not None and screens:
            screen_binding = workspace.screen_bindings.get(screens[0].entity_id)
            if screen_binding is not None:
                target = screen_image_center_world(screens[0], screen_binding)
        if target is None:
            # Fall back to the seat's local forward (-Y) direction 3 m ahead.
            matrix = quaternion_to_matrix3(seat.orientation)
            forward = (
                matrix[0][1] * -1.0,
                matrix[1][1] * -1.0,
                matrix[2][1] * -1.0,
            )
            target = (eye[0] + 3.0 * forward[0], eye[1] + 3.0 * forward[1], eye[2] + 3.0 * forward[2])
        self._saved_camera_view = capture()
        view_from(
            domain_to_render(Position3(x_m=eye[0], y_m=eye[1], z_m=eye[2])),
            domain_to_render(Position3(x_m=target[0], y_m=target[1], z_m=target[2])),
            view_angle_deg=45.0,
        )
        self._set_status(f"座席視点: {seat.name}（カメラを戻すで復帰）")

    # -- history (#485) ----------------------------------------------------------------

    def _sync_history_panel(self) -> None:
        revisions = self.controller.list_revisions()
        head = self.controller.repository.current_head(self.controller.document_id)
        labels = self.controller.revision_labels()
        self.history_panel.sync_revisions(
            revisions,
            head_revision_id=(
                head.revision_id if head is not None else None
            ),
            labels=labels,
        )
        selected_id = self.history_panel.selected_revision_id()
        if selected_id is not None:
            # Keep the open diff live against the (possibly new) HEAD instead
            # of resetting the detail pane to the summary line.
            self._history_diff(selected_id)
            return
        self.history_panel.show_detail(
            f"リビジョン数: {len(revisions)} · HEAD: "
            + (
                revision_display_label(head, labels)
                if head is not None
                else '—'
            )
        )

    def _history_preview(self, revision_id: object) -> None:
        render = getattr(self.viewport, "render_history_ghost", None)
        if revision_id is None:
            if callable(render):
                render(None)
            self._render()
            return
        revision = self.controller.repository.get(str(revision_id))
        if revision is None or not callable(render):
            return
        label_map = self.controller.revision_labels()
        render(
            revision.document,
            label=revision_display_label(revision, label_map),
        )

    def _history_label(self, revision_id: object, label: object, note: object) -> None:
        try:
            self.controller.set_revision_label(
                str(revision_id), str(label), str(note)
            )
        except (EditStateError, ValueError) as exc:
            self._set_operation_error("ラベルを保存できませんでした", exc, effect='変更は保存されていません')
            return
        self._sync_history_panel()
        self._set_status("ラベルを保存しました（履歴は不変です）")

    def _history_diff(self, revision_id: object) -> None:
        if revision_id is None:
            return
        revision = self.controller.repository.get(str(revision_id))
        head = self.controller.repository.current_head(self.controller.document_id)
        if revision is None or head is None:
            return
        diff = diff_scene_documents(revision.document, head.document)
        # Removed entities only exist on the older side — resolve their names
        # from it instead of showing raw ids.
        lines = diff_summary_lines(
            diff, head.document, fallback_document=revision.document
        )
        labels = self.controller.revision_labels()
        prefix = (
            "過去版 → 現在の差分 ("
            f"{revision_display_label(revision, labels)} → HEAD):\n"
        )
        self.history_panel.show_detail(prefix + "\n".join(lines))

    def _history_restore(self, revision_id: object) -> None:
        if self.controller.working.has_preview:
            self._set_status("プレビュー中は復元できません", error=True)
            return
        head_before = self.controller.repository.current_head(
            self.controller.document_id
        )
        try:
            revision = self.controller.restore_revision(str(revision_id))
        except EditStateError as exc:
            self._set_operation_error("履歴を復元できませんでした", exc)
            return
        self._history_preview(None)
        self._refresh(reset_camera=True)
        self._sync_history_panel()
        labels = self.controller.revision_labels()
        if head_before is not None and revision.revision_id == head_before.revision_id:
            # Content-identical restore dedupes to the existing head — no new
            # revision was created, so don't claim one was.
            self._set_status("その履歴版は現在の先頭版と同一内容です")
            return
        self._set_status(
            "履歴版を新しい先頭版として復元しました: "
            f"{revision_display_label(revision, labels)}"
        )

    def _measure_state_changed(self) -> None:
        self._render()
        if self.measure_controller.is_active:
            self._set_status(
                "計測中: オブジェクトまたは空の位置をクリック（Escで中止）"
            )

    # -- refresh -------------------------------------------------------------------------

    def has_focused_text_editor(self) -> bool:
        """True while a text/numeric field inside this workspace owns focus."""

        return focused_text_editor(self) is not None

    def commit_pending_editor(self) -> bool:
        """Flush a pending inspector/geometry edit into the WorkingDocument.

        project.save is a GLOBAL shortcut: Ctrl+S arrives while a field still
        owns focus, before ``editingFinished`` commits the visible value. This
        boundary forces that commit (reusing the field's own commit handler) so
        the saved revision reflects what the user sees. Returns False when the
        pending value was rejected, so Save can refuse instead of persisting a
        revision that silently omits the visible edit.
        """

        self._pending_editor_rejected = False
        flush_focused_text_editor(self)
        rejected = self._pending_editor_rejected
        self._pending_editor_rejected = False
        return not rejected

    def mark_pending_editor_rejected(self) -> None:
        """Record that a pending-editor commit was refused during a flush."""

        self._pending_editor_rejected = True

    def save(self) -> bool:
        if not self.commit_pending_editor():
            self._set_status("入力中の値を確定できないため保存できません", error=True)
            return False
        try:
            created = self.controller.save()
        except EditStateError as exc:
            self._set_operation_error("保存できませんでした", exc)
            return False
        self._refresh()
        self._set_status("保存しました" if created else "変更はありません")
        return created

    def resolve_dirty_state(
        self, action: DirtyResolutionAction
    ) -> tuple[bool, str | None]:
        """Resolve dirty state through the controller, then re-render.

        The controller mutates the working document without the workspace:
        discard/recover rebind it and every action can flip ``is_dirty``, so
        the viewport and the dirty badge only stay truthful after a refresh.
        """
        resolved, message = self.controller.resolve_dirty_state(action)
        if resolved:
            self._refresh()
        return resolved, message

    def undo(self) -> bool:
        label = self.controller.undo_label
        changed = self.controller.undo()
        if changed:
            self._refresh()
            self._set_status(f"元に戻しました: {label}" if label else "元に戻しました")
        return changed

    def redo(self) -> bool:
        label = self.controller.redo_label
        changed = self.controller.redo()
        if changed:
            self._refresh()
            self._set_status(f"やり直しました: {label}" if label else "やり直しました")
        return changed

    def _update_responsive_layout(self) -> None:
        width = self.width()
        compact = width < 900
        ultra_compact = width < 720
        self._responsive_compact = compact
        if ultra_compact:
            self._palette_user_open = False
        right_width = 260 if ultra_compact else (280 if compact else 300)
        if not self._right_panel_user_sized:
            self._content_splitter.setSizes(
                [max(1, width - right_width), right_width]
            )
        self.overlay_controls.set_compact(compact)
        show_palette = (
            self.current_context in {"objects", "placement"}
            and (not compact or self._palette_user_open)
        )
        self.object_palette.setVisible(show_palette)
        self._layout_journey_steps()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_responsive_layout()

    def _tool_requested(self, tool_id: str) -> None:
        if tool_id == "show-palette":
            if self._responsive_compact:
                self._palette_user_open = not self.object_palette.isVisible()
                self._update_responsive_layout()
            else:
                self.object_palette.setVisible(True)
            return
        if tool_id == "focus-selection":
            self.fit_selection()
            return
        if tool_id == "fit-scene":
            self.viewport.fit_scene()
            return
        if tool_id == "toggle-acoustics":
            self.overlay_controls.acoustics.toggle()
            return
        if tool_id == "measure":
            self.toggle_measure()
            return
        if tool_id == "delete-selection":
            self.delete_selection()
            return
        if tool_id == "view-menu":
            focus = QApplication.focusWidget()
            if focus is not None and self.tools.isAncestorOf(focus):
                # Keyboard activation: anchor the menu to the focused tool
                # button — QCursor.pos() opens it wherever the pointer sits.
                self.view_menu.popup(
                    focus.mapToGlobal(focus.rect().bottomLeft())
                )
            else:
                self.view_menu.popup(QCursor.pos())
            return
        if tool_id == "draw-room" and self.geometry_input is not None:
            self.geometry_input.start_sketch()
            return
        if tool_id == "edit-room" and self.geometry_input is not None:
            self.geometry_input.start_edit()
            return
        self.toolRequested.emit(tool_id)

    def _add_object(self, kind: str) -> None:
        try:
            entity = self.controller.add_object(kind)
        except (EditStateError, ValueError) as exc:
            self._set_operation_error("オブジェクトを追加できませんでした", exc)
            return
        self._refresh()
        hint = " · 役割を選択してください" if entity.kind == "speaker" else ""
        self._set_status(f"{entity.name}を追加しました{hint}")

    def _commit_inspector(self) -> None:
        entity_id = self.controller.selected_id
        if entity_id is None:
            return
        entity = self.controller.document.entity(entity_id)
        selection_ids = tuple(self.controller.view_state.selection) or (entity_id,)
        try:
            if len(selection_ids) > 1:
                changed = self._commit_inspector_batch(entity, selection_ids)
            else:
                (
                    name,
                    position,
                    size,
                    role,
                    orientation,
                    aim_angles,
                    body_geometry,
                ) = self.inspector.values(entity)
                changed = self.controller.update_selected(
                    name=name,
                    position=position,
                    size_m=size,
                    speaker_role=role,
                    orientation=orientation,
                    aim_yaw_pitch_deg=aim_angles,
                    body_geometry=body_geometry,
                )
        except (EditStateError, ValueError) as exc:
            # Rejected commits keep the user's in-progress edits (never a
            # silent drop) and pin the error beside the field that caused it
            # (#583). update_entities is atomic, so authority is unchanged.
            self._pending_editor_rejected = True
            section = exc.section if isinstance(exc, InspectorValidationError) else None
            self.inspector.show_error(section, operation_error_message(exc))
            self._set_operation_error("フィールドを保存できませんでした", exc, effect='変更は保存されていません')
            return
        if changed:
            notes = self.controller.pop_constraint_notes()
            self._refresh()
            suffix = " / " + " / ".join(notes) if notes else ""
            self._set_status(f"選択項目を更新しました{suffix}")

    def _commit_inspector_batch(
        self,
        primary: SceneEntity,
        selection_ids: tuple[str, ...],
    ) -> bool:
        """Apply the inspector's edited fields to every editable selection member.

        Per-axis position/size edits merge into each entity's own values —
        multi-edit never flattens unedited axes (#583). Name and speaker role
        stay primary-only; transforms, orientation, aim and body geometry fan
        out under the entity-kind guards the single edit path uses.
        """

        if not self.controller.can_edit:
            raise EditStateError("現在の状態では選択項目を編集できません")
        edited = self.inspector.edited_values(primary)
        if not edited:
            return False
        name = cast("str | None", edited.get("name"))
        position_axes = cast("dict[int, float] | None", edited.get("position_axes"))
        size_axes = cast("dict[int, float] | None", edited.get("size_axes"))
        orientation = cast("Quaternion4 | None", edited.get("orientation"))
        aim_angles = cast(
            "tuple[float, float] | None", edited.get("aim_yaw_pitch_deg")
        )
        role = cast("str | None", edited.get("role"))
        body_geometry_present = "body_geometry" in edited
        body_geometry = edited.get("body_geometry")
        updates: dict[str, dict[str, object]] = {}
        for entity_id in selection_ids:
            if self.controller.view_state.is_locked(entity_id):
                continue
            entity = self.controller.document.entity(entity_id)
            patch: dict[str, object] = {}
            if entity_id == primary.entity_id:
                if name is not None:
                    patch["name"] = name.strip() or entity.name
                if role is not None and entity.kind == "speaker":
                    patch["speaker_role"] = role
            if position_axes:
                patch["position"] = Position3(
                    x_m=position_axes.get(0, entity.position.x_m),
                    y_m=position_axes.get(1, entity.position.y_m),
                    z_m=position_axes.get(2, entity.position.z_m),
                )
            if size_axes and entity.size_m is not None:
                patch["size_m"] = Size3(
                    x_m=size_axes.get(0, entity.size_m.x_m),
                    y_m=size_axes.get(1, entity.size_m.y_m),
                    z_m=size_axes.get(2, entity.size_m.z_m),
                )
            if orientation is not None and entity.kind in PHYSICAL_ENTITY_KINDS:
                patch["orientation"] = orientation
            if aim_angles is not None and entity.kind == "speaker":
                patch["aim_xyz"] = direction_from_yaw_pitch_deg(
                    yaw_deg=aim_angles[0],
                    pitch_deg=aim_angles[1],
                )
            if body_geometry_present and entity.size_m is not None:
                patch["body_geometry"] = body_geometry
            if patch:
                updates[entity_id] = patch
        if not updates:
            return False
        changed = self.controller.update_entities(updates)
        if changed:
            notes = self.controller.propagate_constraints(
                set(updates), merge_with_previous=True
            )
            if notes:
                self.controller._last_constraint_notes = notes
        return changed

    def _aim_target_committed(self, target_id: object) -> None:
        try:
            changed = self.controller.aim_selected_speaker_at(str(target_id))
        except (EditStateError, ValueError) as exc:
            self._refresh_inspector()
            self._set_operation_error("音響方向を設定できませんでした", exc)
            return
        if changed:
            self._refresh()
            self._set_status("選択した対象へ音響方向を設定しました")
        else:
            self._set_status("音響方向は変更されませんでした")

    def _aim_clear_committed(self) -> None:
        try:
            changed = self.controller.clear_selected_speaker_aim()
        except (EditStateError, ValueError) as exc:
            self._refresh_inspector()
            self._set_operation_error("音響方向をクリアできませんでした", exc)
            return
        if changed:
            self._refresh()
            self._set_status("音響方向を未設定に戻しました")
        else:
            self._set_status("音響方向はすでに未設定です")

    def _cabinet_align_committed(self) -> None:
        try:
            changed = self.controller.align_selected_cabinet_to_aim()
        except (EditStateError, ValueError) as exc:
            self._refresh_inspector()
            self._set_operation_error("キャビネットを音響方向に合わせられませんでした", exc)
            return
        if changed:
            self._refresh()
            self._set_status("本体を音響方向に合わせました")
        else:
            self._set_status("本体の向きは変更されませんでした")

    def import_mesh_for_selected(self, file_path: str | Path) -> bool:
        """Attach an imported mesh as the selected entity's body geometry."""

        entity_id = self.controller.selected_id
        if entity_id is None:
            self._set_status("メッシュを設定するオブジェクトを選択してください", error=True)
            return False
        try:
            entity = self.controller.attach_mesh_asset(entity_id, file_path)
        except (EditStateError, RawMeshImportError, ValueError, OSError) as exc:
            self._pending_editor_rejected = True
            self._set_operation_error("メッシュを設定できませんでした", exc)
            return False
        self._refresh()
        self._set_status(f"{entity.name}にメッシュボディを設定しました")
        return True

    def _import_mesh_for_selected(self) -> None:
        # Inspector mesh button → guided import with the selection as the
        # entity-body destination (#762).
        self.import_geometry_dialog()

    def import_geometry_dialog(self, file_path: str | Path | None = None) -> bool:
        """Guided geometry import: declare units/axes, QA, repair preview (#762).

        One dialog covers both destinations — the selected entity's body
        (declared-authority attach) and the room's ``r120_semantic_geometry``
        (solver-readiness commit). Replaces the blind legacy attach as the
        only UI-reachable import path.
        """

        if file_path is None:
            file_path, _filter = file_dialog_memory.get_open_file_name(
                self,
                "ジオメトリをインポート",
                'room.import_geometry',
                "メッシュ (*.obj *.glb *.meshbin *.ply *.stl);;すべてのファイル (*)",
            )
            if not file_path:
                return False
        entity_id = self.controller.selected_id
        entity_name = None
        if entity_id is not None:
            try:
                entity = self.controller.document.entity(entity_id)
            except (EditStateError, KeyError):
                entity = None
            if entity is not None and entity.size_m is not None:
                entity_name = entity.name
            else:
                entity_id = None
        try:
            dialog = GeometryImportDialog(
                file_path, entity_target=entity_name, parent=self
            )
        except (EditStateError, RawMeshImportError, ValueError, OSError) as exc:
            self._set_operation_error("ジオメトリをインポートできませんでした", exc)
            return False
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        request = dialog.import_request()
        if request.destination == 'entity_body' and entity_id is not None:
            return self._commit_entity_mesh_import(entity_id, file_path, request)
        return self._commit_room_geometry_import(file_path, request)

    def _commit_entity_mesh_import(
        self,
        entity_id: str,
        file_path: str | Path,
        request: GeometryImportRequest,
    ) -> bool:
        def attach(decision: MeshImportOversizeDecision):
            return self.controller.attach_mesh_asset_declared(
                entity_id,
                file_path,
                source_unit=request.source_unit,
                custom_scale_to_meters=request.custom_scale_to_meters,
                up_axis=request.up_axis,
                forward_axis=request.forward_axis,
                handedness=request.handedness,
                local_anchor=request.local_anchor,
                repaired_mesh=request.repaired_mesh,
                oversize_decision=decision,
            )

        try:
            try:
                entity = attach('cancel')
            except MeshImportCancelledError:
                box = QMessageBox(self)
                box.setWindowTitle("メッシュが包絡サイズを超えています")
                box.setText(
                    "インポートしたメッシュが宣言済みのオブジェクトサイズ"
                    "（size_m）を超えています。"
                )
                adopt = box.addButton(
                    "包絡をメッシュに合わせる", QMessageBox.ButtonRole.AcceptRole
                )
                rescale = box.addButton(
                    "メッシュを縮小して適合", QMessageBox.ButtonRole.DestructiveRole
                )
                box.addButton(QMessageBox.StandardButton.Cancel)
                box.exec()
                clicked = box.clickedButton()
                if clicked is adopt:
                    entity = attach('adopt')
                elif clicked is rescale:
                    entity = attach('rescale')
                else:
                    return False
        except (EditStateError, RawMeshImportError, ValueError, OSError) as exc:
            self._pending_editor_rejected = True
            self._set_operation_error("メッシュを設定できませんでした", exc)
            return False
        self._refresh()
        self._set_status(f"{entity.name}にメッシュボディを設定しました")
        return True

    def _commit_room_geometry_import(
        self,
        file_path: str | Path,
        request: GeometryImportRequest,
    ) -> bool:
        try:
            geometry = self.controller.import_room_mesh_geometry(
                file_path,
                source_unit=request.source_unit,
                custom_scale_to_meters=request.custom_scale_to_meters,
                up_axis=request.up_axis,
                forward_axis=request.forward_axis,
                handedness=request.handedness,
                local_anchor=request.local_anchor,
                surface_assignments=request.surface_assignments,
                repaired_mesh=request.repaired_mesh,
                repaired_diagnostic=request.repaired_diagnostic,
            )
        except (EditStateError, RawMeshImportError, ValueError, OSError) as exc:
            self._pending_editor_rejected = True
            self._set_operation_error(
                "部屋のジオメトリをインポートできませんでした", exc
            )
            return False
        self._refresh(reset_camera=True)
        if (
            geometry.geometry_compiler_readiness
            == 'ready_for_r120_geometry_compiler_contract'
        ):
            self._set_status(
                "部屋の音響ジオメトリをインポートしました（R120契約対応可）"
            )
        else:
            detail = "、".join(geometry.unresolved_conditions) or "未解決項目"
            self._set_status(
                f"部屋の音響ジオメトリをインポートしました（{detail}）"
            )
        return True

    def _recover(self) -> None:
        try:
            changed = self.controller.recover_draft()
        except EditStateError as exc:
            self._set_operation_error("ドラフトを復元できませんでした", exc)
            return
        if changed:
            self._refresh(reset_camera=True)
            self._set_status("下書きを復旧しました")

    def _discard_recovery(self) -> None:
        if self.controller.discard_recovery():
            self._refresh(reset_camera=True)
            self._set_status("保存前の下書きを破棄しました")

    def _refresh(self, *, reset_camera: bool = False) -> None:
        # Broken-by-delete detection is cheap (early exit without constraints)
        # and keeps stale constraints surfaced instead of silently reusing them.
        broken_labels = self.controller.mark_broken_constraints()
        if broken_labels:
            self._set_status(
                "拘束が破損しました: " + "、".join(broken_labels), error=True
            )
        self.recovery_banner.setVisible(self.controller.recovery_candidate is not None)
        self._refresh_journey()
        self._refresh_inspector()
        self._sync_objects_panel()
        if self.current_context == "placement":
            self._sync_constraints_panel()
            self._sync_video_panel()
            self._sync_seat_priority_panel()
            # Keep the lighting-scene summary honest while browsing the
            # placement/video page even when the preview toggle is off.
            self._current_lighting_scene()
            installation_panel = getattr(self, "installation_panel", None)
            if installation_panel is not None:
                installation_panel.refresh()
        if self.current_context == "history":
            self._sync_history_panel()
        if self.geometry_panel is not None:
            refresh_geometry = getattr(self.geometry_panel, "refresh", None)
            if callable(refresh_geometry):
                refresh_geometry()
        if self.acoustics_panel is not None and self.current_context == "acoustics":
            refresh_acoustics = getattr(self.acoustics_panel, "refresh", None)
            if callable(refresh_acoustics):
                refresh_acoustics()
        self._render(reset_camera=reset_camera)
        # Dirty state is refreshed on its own badge every render — routing it
        # through the notice strip meant the first notice suppressed it
        # forever and "保存しました" stayed up while edits were pending.
        self.dirty_status_label.setText(
            "未保存の変更があります" if self.controller.is_dirty else "保存済み"
        )
        set_semantic_state(
            self.dirty_status_label,
            SemanticState.WARNING if self.controller.is_dirty else None,
        )

    def _refresh_journey(self) -> None:
        """Re-evaluate the numbered journey strip from persisted state.

        Every signal comes from the saved head revision plus document
        sidecars — never the working draft — so a step only completes when
        the saved room proves it. Runs from _refresh (the universal
        mutation funnel) and from set_context, since acoustics-panel
        sidecar writes bypass _refresh. Individual reads degrade to zero
        rather than take down the whole workspace refresh.
        """
        if not hasattr(self, "journey_steps_grid"):
            # _refresh/set_context can run while __init__ is still building —
            # before the strip exists there is nothing to refresh.
            return
        try:
            head = self.controller.repository.latest(
                self.controller.document_id
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise  # store failures never masquerade as 'nothing saved'
            report_boundary_failure(exc, operation='保存済みシーンの確認')
            head = None
        document = head.document if head is not None else None
        room = document.room if document is not None else None
        entities = document.entities if document is not None else ()
        topology = document.wall_topology if document is not None else None
        document_id = self.controller.document_id
        speakers = tuple(
            entity for entity in entities if entity.kind == "speaker"
        )
        try:
            pose_count = len(
                self.listener_pose_repository.selections_for_document(
                    document_id
                )
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='測定姿勢の確認')
            pose_count = 0
        try:
            material_count = len(
                self.controller.material_repository.assignments_for_document(
                    document_id
                )
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='素材割り当ての確認')
            material_count = 0
        try:
            treatment_count = len(
                self.controller.treatment_repository.proposed_placement_ids(
                    document_id
                )
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='吸音処理の確認')
            treatment_count = 0
        try:
            prediction_count = len(
                self.prediction_repository.list_results(document_id)
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='予測結果の確認')
            prediction_count = 0
        steps = evaluate_room_journey(
            room_saved=room is not None,
            vertex_count=(
                len(room_vertices(room)) if room is not None else 0
            ),
            wall_count=len(topology.walls) if topology is not None else 0,
            opening_count=(
                len(topology.openings) if topology is not None else 0
            ),
            speaker_count=len(speakers),
            unassigned_speaker_count=sum(
                1
                for entity in speakers
                if is_unassigned_speaker_role(entity.speaker_role)
            ),
            duplicate_role_count=len(duplicated_speaker_roles(entities)),
            equipment_count=sum(
                1
                for entity in entities
                if entity.kind
                in ("screen", "display", "projector", "av_equipment")
            ),
            seat_count=sum(
                1 for entity in entities if entity.kind == "seat"
            ),
            pose_count=pose_count,
            material_count=material_count,
            treatment_count=treatment_count,
            prediction_count=prediction_count,
        )
        self._journey_steps = steps

        for button in self._journey_buttons.values():
            button.deleteLater()
        self._journey_buttons.clear()
        done_count = sum(1 for step in steps if step.status == "done")
        self.journey_progress.setText(f"{done_count}/{len(steps)}")
        for step in steps:
            label = f"{step.number} {step.title}"
            if step.status == "done":
                label += " ✓"
            button = QPushButton(label, self.journey_card)
            button.setObjectName(f"roomJourneyStep_{step.key}")
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
            self._journey_buttons[step.key] = button
        self._layout_journey_steps()

        current = current_journey_step(steps)
        if current is None:
            self.journey_hint.setText(
                "すべての手順が完了しています。「履歴」で版の確認・復元が"
                "できます。測定や最適化へ進む準備ができています。"
            )
            self.journey_open.setVisible(False)
        else:
            self.journey_hint.setText(
                f"次にやること — {current.number} {current.title}: {current.detail}"
            )
            self.journey_open.setVisible(True)

    def _layout_journey_steps(self) -> None:
        """Reflow journey buttons: one row normally, 3-per-row when compact."""
        while self.journey_steps_grid.count():
            self.journey_steps_grid.takeAt(0)
        columns = 3 if self._responsive_compact else max(1, len(self._journey_steps))
        for index, step in enumerate(self._journey_steps):
            button = self._journey_buttons.get(step.key)
            if button is None:
                continue
            row, column = divmod(index, columns)
            self.journey_steps_grid.addWidget(button, row, column)
        self.journey_steps_grid.setColumnStretch(columns, 1)

    def _open_journey_step(self, key: str) -> None:
        step = next(
            (step for step in self._journey_steps if step.key == key), None
        )
        if step is None or step.context_id is None:
            return
        # Deep links keep the shell's context bar and router in sync with
        # the context switch; bare set_context is the standalone fallback.
        if self._on_navigate is not None:
            self._on_navigate(
                WorkspaceDeepLink(WorkspaceId.ROOM, step.context_id)
            )
        else:
            self.set_context(step.context_id)

    def _open_current_journey_step(self) -> None:
        step = current_journey_step(self._journey_steps)
        if step is not None:
            self._open_journey_step(step.key)

    def _refresh_inspector(self) -> None:
        entity = None
        selected_id = self.controller.selected_id
        if selected_id is not None:
            try:
                entity = self.controller.document.entity(selected_id)
            except KeyError:
                self.controller.set_selection(None)
        editable = bool(
            entity is not None
            and self.controller.can_edit
            and not self.controller.view_state.is_locked(entity.entity_id)
        )
        selection: list[SceneEntity] = []
        for member_id in self.controller.view_state.selection:
            try:
                selection.append(self.controller.document.entity(member_id))
            except KeyError:
                continue
        aim_targets = (
            self.controller.aim_targets()
            if entity is not None and entity.kind == "speaker"
            else ()
        )
        self.inspector.set_entity(
            entity,
            editable=editable,
            aim_targets=aim_targets,
            selection=tuple(selection),
        )

    def _render(self, *, reset_camera: bool = False) -> None:
        overlays = replace(
            self.overlay_controls.state(),
            hidden_ids=frozenset(self.controller.view_state.hidden_ids),
            guides_visible=self._guides_visible,
            lighting_scene=(
                getattr(self, 'lighting_panel', None) is not None
                and self.lighting_panel.preview_enabled
            ),
            operational_clearance=(
                getattr(self, 'clearance_panel', None) is not None
                and self.clearance_panel.preview_enabled
            ),
            operational_zone_kinds=(
                self.clearance_panel.enabled_kinds
                if getattr(self, 'clearance_panel', None) is not None
                else frozenset()
            ),
        )
        self._sync_aux_render_state()
        # The document rebuild plus each overlay renderer used to trigger a
        # full plotter.render() apiece; deferred_render() coalesces them into
        # a single draw of the final state (duck-typed viewports keep working
        # via nullcontext).
        deferred = getattr(self.viewport, "deferred_render", None)
        with (deferred() if callable(deferred) else nullcontext()):
            self.viewport.render_document(
                self.controller.document,
                selected_id=self.controller.selected_id,
                selected_ids=self.controller.view_state.selection,
                hidden_ids=frozenset(self.controller.view_state.hidden_ids),
                locked_ids=frozenset(self.controller.view_state.locked_ids),
                overlays=overlays,
                reset_camera=reset_camera,
            )
            # Constraints + measure + video overlays ride the same viewport render.
            render_constraints = getattr(self.viewport, "render_constraint_overlay", None)
            if callable(render_constraints) and self.controller.constraint_set is not None:
                render_constraints(
                    self.controller.constraint_set,
                    self.controller.evaluate_constraints(),
                )
            render_measure = getattr(self.viewport, "render_measure_overlay", None)
            if callable(render_measure):
                render_measure(
                    self.measure_controller.result,
                    draft_endpoints=tuple(
                        endpoint.position
                        for endpoint in self.measure_controller.endpoints
                    ),
                )
            render_video = getattr(self.viewport, "render_video_overlay", None)
            if callable(render_video):
                render_video(self._video_evaluation)
            render_lighting = getattr(
                self.viewport, 'render_lighting_scene_preview', None
            )
            lighting_preview = (
                self._lighting_scene_preview()
                if overlays.lighting_scene
                else None
            )
            if callable(render_lighting):
                render_lighting(lighting_preview)
            lighting_panel = getattr(self, 'lighting_panel', None)
            if lighting_panel is not None:
                lighting_panel.show_preview(
                    lighting_preview if overlays.lighting_scene else None
                )
            render_opclear = getattr(
                self.viewport, 'render_operational_clearance_overlay', None
            )
            # #1010: conflicts are recomputed on every render from
            # ``controller.document`` — which returns the live preview
            # document during drags — so stale-revision results can never
            # paint on screen.
            opclear_preview = (
                build_operational_clearance_preview(
                    document=self.controller.document,
                    enabled_kinds=overlays.operational_zone_kinds,
                )
                if overlays.operational_clearance
                else None
            )
            if callable(render_opclear):
                render_opclear(opclear_preview)
            clearance_panel = getattr(self, 'clearance_panel', None)
            if clearance_panel is not None:
                clearance_panel.show_preview(
                    opclear_preview
                    if overlays.operational_clearance
                    else None
                )
            if self.current_context == "placement" and self._proposed_variant_id is not None:
                try:
                    proposal_entities = self.system_expansion.ghost_preview(
                        self._proposed_variant_id
                    )
                except (KeyError, ValueError):
                    proposal_entities = ()
                render_proposals = getattr(self.viewport, "render_proposed_entities", None)
                if callable(render_proposals):
                    render_proposals(
                        proposal_entities,
                        selected_id=self._proposed_selected_id,
                    )
            if overlays.acoustics and self.prediction_results:
                render_prediction = getattr(self.viewport, "render_prediction_results", None)
                if callable(render_prediction):
                    render_prediction(
                        self.prediction_results,
                        highlight=self.prediction_focus,
                    )
            # REV40: persisted-artifact reflection-guidance markers ride
            # the same render; the setting resolves to () when hidden.
            render_guidance = getattr(
                self.viewport, "render_reflection_guidance_overlay", None
            )
            if callable(render_guidance):
                render_guidance(self._visible_guidance_markers(overlays))
            # #999: 3D field overlay — acoustics context + overlay ON +
            # armed request + CURRENT head; anything else draws nothing.
            render_field = getattr(self.viewport, 'render_field_overlay', None)
            if callable(render_field):
                if (
                    self.current_context == 'acoustics'
                    and overlays.acoustics
                    and self.field_overlay.armed
                ):
                    resolution = self.field_overlay.resolve()
                    if resolution.scene is not None:
                        render_field(resolution.scene)
                    else:
                        self.viewport.clear_field_overlay()
                        if resolution.blocked_reason:
                            self._set_status(resolution.blocked_reason)
                            self.field_overlay.clear()
                            self.field3DProbeDisarmed.emit()
                else:
                    self.viewport.clear_field_overlay()

    def _current_lighting_scene(self):
        """Current persisted LightingScene for this document, or None.

        Cached per document id — the indexed read would otherwise run on
        every viewport refresh. A selection changed mid-session after the
        first read is NOT observed; anything that re-selects the scene
        must reset ``self._lighting_scene_cache``.
        """
        document_id = self.controller.document_id
        if (
            self._lighting_scene_cache is not None
            and self._lighting_scene_cache[0] == document_id
        ):
            return self._lighting_scene_cache[1]
        scene = self.lighting_repository.current_scene(document_id)
        self._lighting_scene_cache = (document_id, scene)
        self.lighting_panel.show_scene(scene)
        return scene

    def _lighting_inventory(self) -> tuple:
        """(fixtures, zones, commissioning_records) for the preview.

        ``lighting_inventory_provider`` is the seam a future lighting
        inventory store plugs into; with none installed the inventory is
        empty and every scene ref surfaces as unresolved.
        """
        provider = self.lighting_inventory_provider
        if provider is None:
            return (), (), ()
        return provider()

    def _lighting_scene_preview(self):
        scene = self._current_lighting_scene()
        if scene is None:
            return None
        fixtures, zones, records = self._lighting_inventory()
        return build_lighting_scene_preview(
            document=self.controller.document,
            scene=scene,
            fixtures=fixtures,
            zones=zones,
            commissioning_records=records,
        )

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status.setText(text)
        set_semantic_state(self.status, SemanticState.ERROR if error else None)

    def _set_operation_error(
        self,
        title: str,
        exc: BaseException,
        *,
        effect: str | None = None,
    ) -> None:
        """#903: mapped actionable message; raw detail stays in diagnostics."""
        error = to_user_facing_error(exc, title=title, effect=effect)
        self._last_operation_error_detail = error.technical_detail
        log_operation_error(error, exc)
        self._set_status(error.notice_text(), error=True)

    @property
    def last_operation_error_detail(self) -> str | None:
        """Technical detail of the last operation failure (diagnostics path)."""
        return self._last_operation_error_detail

    def closeEvent(self, event) -> None:  # noqa: N802
        self._persist_view_extras()
        if getattr(self, 'system_expansion_panel', None) is not None:
            self.system_expansion_panel.dispose()
        if self.transform_input is not None:
            self.transform_input.dispose()
        if self.geometry_input is not None:
            self.geometry_input.dispose()
        self.controller.close()
        self.viewport.close()
        event.accept()


class SeatingLayoutDialog(QDialog):
    """Bounded authoring dialog for one SeatingLayoutSpec (#546).

    Rows are uniform through this dialog (count/spacing shared); the spec
    model itself keeps per-row independence for callers that need it.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        document: SceneDocument,
        existing: SeatingLayoutSpec | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("座席レイアウト")
        self.setMinimumWidth(360)
        self._existing = existing
        self._riser_ids: list[str | None] = [None]
        form = QFormLayout(self)

        self.name_field = QLineEdit(existing.name if existing else "座席ブロック")
        form.addRow("名前", self.name_field)

        self.rows_field = QSpinBox()
        self.rows_field.setRange(1, 20)
        self.rows_field.setValue(len(existing.rows) if existing else 2)
        form.addRow("列数 (前→後)", self.rows_field)

        first_row = existing.rows[0] if existing else None
        self.count_field = QSpinBox()
        self.count_field.setRange(1, 40)
        self.count_field.setValue(first_row.count if first_row else 4)
        form.addRow("1列あたりの座席数", self.count_field)

        self.spacing_field = QDoubleSpinBox()
        self.spacing_field.setRange(0.3, 3.0)
        self.spacing_field.setSingleStep(0.05)
        self.spacing_field.setSuffix(" m")
        self.spacing_field.setValue(first_row.spacing_m if first_row else 0.75)
        form.addRow("座席間隔", self.spacing_field)

        self.row_spacing_field = QDoubleSpinBox()
        self.row_spacing_field.setRange(0.5, 5.0)
        self.row_spacing_field.setSingleStep(0.05)
        self.row_spacing_field.setSuffix(" m")
        self.row_spacing_field.setValue(first_row.row_spacing_m if first_row else 1.0)
        form.addRow("列間隔", self.row_spacing_field)

        self.stagger_field = QCheckBox("偶数列を半ピッチずらす")
        self.stagger_field.setChecked(bool(first_row and first_row.stagger))
        form.addRow("千鳥配置", self.stagger_field)

        self.aisle_field = QLineEdit()
        self.aisle_field.setPlaceholderText("例: 2:0.9; 5:0.9 (座席番号の後に幅m)")
        if existing and existing.aisles:
            self.aisle_field.setText(
                "; ".join(
                    f"{aisle.after_index}:{aisle.width_m:g}"
                    for aisle in existing.aisles
                )
            )
        form.addRow("通路 (任意)", self.aisle_field)

        self.facing_field = QComboBox()
        self.facing_field.addItem("前面 (-Y, スクリーン向き)", "front")
        self.facing_field.addItem("背面 (+Y)", "rear")
        if existing and existing.facing == "rear":
            self.facing_field.setCurrentIndex(1)
        form.addRow("向き", self.facing_field)

        self.anchor_x_field = QDoubleSpinBox()
        self.anchor_y_field = QDoubleSpinBox()
        room = document.room
        for field in (self.anchor_x_field, self.anchor_y_field):
            field.setRange(-1000.0, 1000.0)
            field.setSingleStep(0.05)
            field.setSuffix(" m")
        if room is not None:
            min_x, min_y, max_x, _ = room.bounds_m
            self.anchor_x_field.setValue(
                existing.anchor_x_m if existing else (min_x + max_x) * 0.5
            )
            self.anchor_y_field.setValue(
                existing.anchor_y_m if existing else min_y + 1.5
            )
        form.addRow("起点 X", self.anchor_x_field)
        form.addRow("起点 Y", self.anchor_y_field)

        self.riser_field = QComboBox()
        self.riser_field.addItem("なし（床置き）", None)
        for entity in document.entities:
            if entity.kind == "riser":
                self.riser_ids.append(entity.entity_id)
                self.riser_field.addItem(entity.name, entity.entity_id)
        if existing and existing.rows and existing.rows[0].riser_entity_id:
            idx = self.riser_field.findData(existing.rows[0].riser_entity_id)
            if idx >= 0:
                self.riser_field.setCurrentIndex(idx)
        form.addRow("ライザー参照 (全列)", self.riser_field)

        for field, tip in (
            (self.name_field, "この座席ブロックの表示名"),
            (self.rows_field, "前から後ろへの列数（1–20）"),
            (self.count_field, "1列に並ぶ座席の数（1–40）"),
            (self.spacing_field, "同じ列内の座席間隔（0.3–3 m）"),
            (self.row_spacing_field, "列と列の間隔（0.5–5 m）"),
            (self.stagger_field, "偶数列を半ピッチずらして前後の視線を確保します"),
            (self.aisle_field, "通路の位置と幅（任意）— 「座席番号:幅m」を ; 区切りで（例: 2:0.9; 5:0.9）"),
            (self.facing_field, "座席が向く方向（前面=スクリーン向き）"),
            (self.anchor_x_field, "座席ブロックの起点X座標（m）"),
            (self.anchor_y_field, "座席ブロックの起点Y座標（m）"),
            (self.riser_field, "全列を載せるライザー（段床）エンティティ · なし=床置き"),
        ):
            apply_field_tooltip(field, tip, form)

        if existing is not None:
            uniform = {
                (
                    row.count,
                    row.spacing_m,
                    row.row_spacing_m,
                    row.stagger,
                    row.riser_entity_id,
                )
                for row in existing.rows
            }
            if len(uniform) > 1:
                note = QLabel(
                    "注意: このレイアウトは列ごとの設定が異なります。"
                    "ここで保存すると全列が最初の列の設定に均一化されます。"
                )
                note.setWordWrap(True)
                form.addRow(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def accept(self) -> None:
        # Validate the aisle syntax while the dialog is still open — a
        # post-close rejection would lose the whole form to a typo.
        try:
            self._parse_aisles()
        except ValueError as exc:
            warn_user(self, "座席レイアウトを適用できませんでした", exc)
            return
        super().accept()

    def spec(self) -> SeatingLayoutSpec | None:
        name = self.name_field.text().strip() or "座席ブロック"
        rows = self.rows_field.value()
        count = self.count_field.value()
        spacing = self.spacing_field.value()
        row_spacing = self.row_spacing_field.value()
        stagger = self.stagger_field.isChecked()
        riser_id = self.riser_field.currentData()
        try:
            aisles = self._parse_aisles()
        except ValueError as exc:
            warn_user(self, "座席レイアウトを適用できませんでした", exc)
            return None
        row_specs = tuple(
            SeatRowSpec(
                row_id=f"r{index + 1}",
                name=chr(ord("A") + index),
                count=count,
                spacing_m=spacing,
                row_spacing_m=row_spacing,
                stagger=stagger,
                riser_entity_id=riser_id,
            )
            for index in range(rows)
        )
        try:
            return SeatingLayoutSpec(
                spec_id=self._existing.spec_id
                if self._existing is not None
                else new_seating_spec_id(),
                name=name[:64],
                anchor_x_m=self.anchor_x_field.value(),
                anchor_y_m=self.anchor_y_field.value(),
                rows=row_specs,
                aisles=aisles,
                facing=self.facing_field.currentData(),
            )
        except (TypeError, ValueError) as exc:
            warn_user(self, "座席レイアウトを適用できませんでした", exc)
            return None

    def _parse_aisles(self) -> tuple[AisleSpec, ...]:
        text = self.aisle_field.text().strip()
        if not text:
            return ()
        aisles: list[AisleSpec] = []
        for chunk in text.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = chunk.split(":")
            if len(parts) != 2:
                raise ValueError("通路は「座席番号:幅m」の形式で入力してください")
            try:
                index = int(parts[0])
                width = float(parts[1])
            except ValueError as exc:
                raise ValueError("通路の番号と幅は数値で入力してください") from exc
            aisles.append(AisleSpec(after_index=index, width_m=width))
        return tuple(aisles)


def build_room_workspace_mount(
    repository: SceneRepository,
    document_id: str,
    *,
    viewport_factory: ViewportFactory | None = None,
    on_navigate: Callable[[WorkspaceDeepLink], bool] | None = None,
) -> WorkspaceMount:
    """Return the UX110 shell mount contract without modifying workflow_shell.py."""

    workspace = RoomWorkspace(
        repository,
        document_id,
        viewport_factory=viewport_factory,
        on_navigate=on_navigate,
    )
    return WorkspaceMount.from_widget(
        workspace,
        on_activate=workspace.activate,
        before_deactivate=workspace.before_deactivate,
        on_context_changed=workspace.set_context,
        on_entity_requested=workspace.select_entity,
    )


__all__ = [
    "ROOM_CONTEXT_IDS",
    "ContextToolStrip",
    "ObjectPalette",
    "RoomWorkspace",
    "RoomWorkspaceController",
    "SelectionInspector",
    "build_room_workspace_mount",
]
