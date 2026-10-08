from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import (
    TYPE_CHECKING,
    Any,
    Iterable,
    Literal,
    Protocol,
    cast,
    get_args,
)
from uuid import uuid4

from .cad_listener_pose import CadListenerPoseRepository

if TYPE_CHECKING:
    from collections.abc import Callable
    from threading import Event

    from .cad_listener_pose import ListenerPoseAuthority
    from .cad_measurement_quality import CadMeasurementQualityReport
    from .cad_measurement_target_pattern import (
        CadTargetPatternRepository,
        MeasurementTargetPattern,
    )
    from .cad_scene import Position3
    from .cad_system_variant_measurement_campaign import (
        CadSystemVariantMeasurementCampaignRepository,
        SystemVariantMeasurementPlan,
    )
    from .r120_geometry_compiler import ExactExternalAuthorityRef

from .cad_measurement_disposition import (
    MEASUREMENT_ELIGIBLE_DISPOSITIONS,
    CadMeasurementCorrection,
    CadMeasurementDisposition,
    CorrectionKind,
    MeasurementDispositionState,
    build_measurement_correction,
    build_measurement_disposition,
)
from .cad_comparison_semantics import (
    ComparisonSemantics,
    derive_comparison_semantics,
    resolve_comparison_side,
)
from .cad_measurement_models import (
    CadFrequencyResponseDataset,
    CadMeasurementAttachment,
    CadMeasurementComparison,
    CadMeasurementRecord,
    MeasurementEvidenceType,
    MeasurementPhaseStatus,
    RadiationScope,
    RoutingEvidence,
)
from .cad_measurement_ir import (
    CadImpulseResponseDataset,
    IrAmplitudeReference,
    IrCalibrationState,
    IrSemantics,
    IrT0Semantics,
    normalize_rew_ir_text,
)
from .cad_measurement_quality import (
    MEASUREMENT_QUALITY_CHECKS,
    AcquisitionContextSourceKind,
    CadAcquisitionContext,
    CadMeasurementCapability,
    CadMeasurementLineageRecord,
    CadMicrophoneCapture,
    CadPlaybackCapture,
    MeasurementCapabilityClaim,
    MeasurementRetakeGuidance,
    QualityDecision,
    RetakeRecommendation,
    build_acquisition_context,
    build_measurement_lineage,
    dataset_sha256,
    gate_measurement_claim,
    measurement_retake_guidance,
    phase_response_capability,
    unestablished_capability_claims,
    unestablished_common_timing_capability,
)
from .cad_measurement_authorities import routing_profile_binding
from .cad_measurement_quality_producer import CadMeasurementQualityProducer
from .cad_measurement_quality_repository import CadMeasurementQualityRepository
from .cad_measurement_repository import CadMeasurementRepository
from .cad_measurement_runner import (
    GuidedStep,
    MeasurementRunnerPlan,
    MeasurementRunnerRun,
    RunnerCellSpec,
    RunnerCellState,
    RunnerPurpose,
    build_runner_plan,
    build_runner_plan_from_cells,
    guided_step,
    runner_progress,
)
from .cad_measurement_runner_repository import (
    CadMeasurementRunnerRepository,
    RunnerError,
)
from .cad_measurements import normalize_rew_api_snapshot, normalize_rew_text
from .cad_repository import SceneRepository, SceneRevision
from .cad_scene import (
    Direction3,
    acoustic_reference_position,
    is_measurement_target_eligible,
    is_unassigned_speaker_role,
)
from .comparison import FrequencyResponse, compare_frequency_responses
from .rew_api import RewFrequencyResponseSnapshot
from .rew_auto import propose_assignment_target
from .rew_parser import parse_rew_frequency_response
from .user_facing_error import operation_error_message

_LOGGER = logging.getLogger('htdt.measurement_workflow')


class MeasurementWorkflowError(ValueError):
    """User-correctable UX130 workflow error without introducing domain authority."""


class RewReadSource(Protocol):
    def list_measurements(self) -> list[dict[str, Any]]: ...

    def get_frequency_response_snapshot(
        self,
        measurement_uuid: str,
        *,
        ppo: int | None = None,
        unit: str = "SPL",
        smoothing: str | None = None,
    ) -> RewFrequencyResponseSnapshot: ...


@dataclass(frozen=True, slots=True)
class PendingMeasurementImport:
    """Transient import preview. It is intentionally not measurement evidence.

    ``scene_revision_id``/``scene_content_hash`` pin the SceneRevision the
    measurement is bound to. ``scene_revision_explicit`` records whether the
    user explicitly chose that revision (the acquisition-time binding the
    issue #659 requires) or merely accepted the current-head proposal — the
    proposal is a default, never proof the data was acquired against it.
    """

    source_kind: str
    source_label: str
    scene_revision_id: str
    scene_content_hash: str
    frequency_hz: tuple[float, ...]
    level_db: tuple[float, ...]
    has_phase_samples: bool
    scene_revision_explicit: bool = False
    raw_text: bytes | None = None
    raw_filename: str | None = None
    rew_snapshot: RewFrequencyResponseSnapshot | None = None
    # Same-content / same-acquisition classification against persisted
    # evidence — the single-file lane's equivalent of the batch lane's
    # per-item duplicate row. 'new' means no persisted match; the flag is
    # advisory and never blocks the explicit commit.
    duplicate_kind: BatchDuplicateKind = 'new'
    duplicate_of_measurement_id: str | None = None

    @property
    def sample_count(self) -> int:
        return len(self.frequency_hz)

    @property
    def frequency_band_hz(self) -> tuple[float, float]:
        return (self.frequency_hz[0], self.frequency_hz[-1])


def _pending_import_journal_state(
    pending: PendingMeasurementImport,
) -> dict[str, Any]:
    """JSON-safe journal projection of a staged import (#883).

    Carries enough to re-stage honestly after a crash: samples, binding
    and the file payload (base64 — the journal is JSON). A live
    ``rew_snapshot`` cannot ride along; ``rew_snapshot_resident`` marks
    that the API snapshot must be re-fetched to restore fully.
    """

    state: dict[str, Any] = {
        'source_kind': pending.source_kind,
        'source_label': pending.source_label,
        'scene_revision_id': pending.scene_revision_id,
        'scene_content_hash': pending.scene_content_hash,
        'frequency_hz': list(pending.frequency_hz),
        'level_db': list(pending.level_db),
        'has_phase_samples': pending.has_phase_samples,
        'scene_revision_explicit': pending.scene_revision_explicit,
        'raw_filename': pending.raw_filename,
        'duplicate_kind': pending.duplicate_kind,
        'duplicate_of_measurement_id': pending.duplicate_of_measurement_id,
        'rew_snapshot_resident': pending.rew_snapshot is not None,
    }
    if pending.raw_text is not None:
        import base64

        state['raw_text_b64'] = base64.b64encode(
            pending.raw_text
        ).decode('ascii')
    return state


@dataclass(frozen=True, slots=True)
class AcquisitionCapture:
    """Acquisition-condition spec to persist as a CadAcquisitionContext (#471).

    Reuse/presets: an existing persisted context's capture fields can seed a
    new context for a new measurement via :meth:`from_context` — the new
    context is a new sealed authority with its own subjects, never a
    mutation of the old one.
    """

    source_kind: AcquisitionContextSourceKind = 'manual'
    microphone: CadMicrophoneCapture | dict[str, Any] | None = None
    playback: CadPlaybackCapture | dict[str, Any] | None = None
    timing_reference_valid: bool | None = None
    timing_reference_id: str | None = None
    timing_reference_sha256: str | None = None
    clock_source: str | None = None
    sample_rate_hz: int | None = None
    delay_correction_s: float | None = None
    # #479: exact AcousticEnvironmentProfile the capture runs under; the
    # persisted context seals it so predicted-vs-measured environment
    # comparison resolves from authority, not assumptions.
    environment_ref: 'ExactExternalAuthorityRef | None' = None
    # #849/#850: explicit scope identity the persisted context proves —
    # the immutable acquisition session the capture belongs to, the full
    # device/path/clock configuration fingerprint (persistent timing
    # scope) and the input-chain fingerprint (instrument calibration
    # scope).
    acquisition_session_id: str | None = None
    signal_path_identity: str | None = None
    input_path_identity: str | None = None
    notes: tuple[str, ...] = ()

    @classmethod
    def from_context(cls, context: CadAcquisitionContext) -> 'AcquisitionCapture':
        return cls(
            source_kind=context.source_kind,
            microphone=context.microphone,
            playback=context.playback,
            timing_reference_valid=context.timing_reference_valid,
            timing_reference_id=context.timing_reference_id,
            timing_reference_sha256=context.timing_reference_sha256,
            clock_source=context.clock_source,
            sample_rate_hz=context.sample_rate_hz,
            delay_correction_s=context.delay_correction_s,
            environment_ref=context.environment_ref,
            acquisition_session_id=context.acquisition_session_id,
            signal_path_identity=context.signal_path_identity,
            input_path_identity=context.input_path_identity,
            notes=context.notes,
        )


@dataclass(frozen=True, slots=True)
class MeasurementAssignment:
    measurement_entity_id: str
    evidence_type: MeasurementEvidenceType = "measured"
    channel_role: str = "unknown"
    source_speaker_ids: tuple[str, ...] = ()
    radiation_scope: RadiationScope = "unknown"
    routing_evidence: RoutingEvidence = "unknown"
    measurement_direction: Direction3 | None = None
    routing_profile_id: str | None = None
    acquisition: AcquisitionCapture | None = None


@dataclass(frozen=True, slots=True)
class AssignmentTarget:
    entity_id: str
    name: str
    kind: str


@dataclass(frozen=True, slots=True)
class SpeakerTarget:
    entity_id: str
    name: str
    role: str


@dataclass(frozen=True, slots=True)
class MeasurementCheckView:
    """One independent quality check surfaced for UX display (#468)."""

    check: str
    status: QualityDecision
    reason: str


@dataclass(frozen=True, slots=True)
class MeasurementView:
    measurement_id: str
    dataset_id: str | None
    evidence_type: MeasurementEvidenceType
    channel_role: str
    target_entity_id: str
    target_name: str
    source_kind: str
    source_speaker_ids: tuple[str, ...]
    routing_evidence: RoutingEvidence
    radiation_scope: RadiationScope
    quality_status: str
    quality_reasons: tuple[str, ...]
    quality_source: str
    phase_status: MeasurementPhaseStatus | None
    # Canonical capability claims (#466). phase_response and common_timing are
    # distinct authority claims: valid phase samples never imply a shared
    # timing reference. When a replay-validated CadMeasurementQualityReport
    # exists for the exact bound dataset these mirror its capability matrix;
    # otherwise phase_response falls back to the canonical dataset-only rule
    # and common_timing fails closed at UNKNOWN.
    phase_response_capability: CadMeasurementCapability | None
    common_timing_capability: CadMeasurementCapability | None
    # Full canonical claim matrix (#468) in canonical claim order. From the
    # replay-validated report bound to the exact dataset when one exists;
    # otherwise the fail-closed matrix in which only dataset-local claims keep
    # their verdicts and every acquisition-evidence claim stays UNKNOWN.
    capabilities: tuple[CadMeasurementCapability, ...]
    # Independent quality checks (clipping, SNR, usable band, timing, polarity,
    # IR window, calibration, repeatability) with PASS/FAIL/UNKNOWN/
    # NOT_EVALUATED status and the authority reason. Empty without a current
    # replay-validated report — missing evidence is never shown as PASS.
    quality_checks: tuple[MeasurementCheckView, ...]
    # Report summary state: 'current' binds the exact dataset, 'stale' means a
    # report exists but is pinned to a different dataset, 'missing' means no
    # report exists for this measurement, 'error' means a report exists but
    # its replay re-verification failed (detail in ``report_error``).
    quality_report_state: Literal['current', 'stale', 'missing', 'error']
    quality_profile_version: str | None
    quality_report_created_at: str | None
    # Retake authority: recommendation/reasons plus structured guidance on
    # which acquisition context/evidence is missing and what to re-measure.
    # None/empty without a current replay-validated report.
    retake_recommendation: RetakeRecommendation | None
    retake_reasons: tuple[str, ...]
    retake_guidance: MeasurementRetakeGuidance | None
    # Append-only retake lineage: the measurement currently selected in this
    # retake chain, and the chain neighbours of this measurement if any.
    selected_measurement_id: str
    supersedes_measurement_id: str | None
    superseded_by_measurement_id: str | None
    sample_count: int
    frequency_band_hz: tuple[float, float] | None
    captured_at: str | None
    imported_at: str
    scene_revision_id: str
    scene_matches_current: bool

    # Lifecycle disposition (#509): the latest append-only disposition event,
    # or None when the measurement has never been dispositioned (the normal
    # active state). ``is_normally_eligible`` gates every downstream selector.
    disposition: str | None
    disposition_reason: str | None
    is_normally_eligible: bool
    # Effective binding (#509): the record's assignment overlaid by the latest
    # append-only correction. Without a correction these equal the persisted
    # record fields.
    is_corrected: bool
    effective_target_entity_id: str
    effective_target_name: str
    effective_channel_role: str
    effective_source_speaker_ids: tuple[str, ...]
    effective_radiation_scope: RadiationScope
    effective_routing_evidence: RoutingEvidence
    # Import-time processing declared on the bound dataset (#503).
    smoothing: str | None
    attachment_count: int
    # Physical-position truth (#863): the immutable import position never
    # changes on a correction; ``observed_actual_position`` is the resolved
    # world position of the pinned pose observation (None unless pose
    # evidence is bound), and ``assignment_position_compatibility``
    # classifies how the effective target relates to the import position —
    # 'original' when uncorrected or non-spatial, 'exact' when the relabeled
    # target's reference equals the import position, 'pose_observed' when it
    # differs and an exact pose authority backs the reassignment.
    original_import_position: Position3
    observed_actual_position: Position3 | None
    assignment_position_compatibility: Literal[
        'original', 'exact', 'pose_observed'
    ]
    # Authoritative-read failure for the bound dataset (seal/hash/raw-asset
    # verification). The row stays listed with ``dataset_id=None`` so the
    # measurement is visible and unusable rather than crashing the whole
    # listing; None means the bound dataset read cleanly or never existed.
    dataset_error: str | None = None
    # Authoritative-read failure for the bound quality report — isolated
    # per row like ``dataset_error`` so one corrupt report cannot take the
    # listing down.
    report_error: str | None = None

    @property
    def is_selected(self) -> bool:
        """Whether this measurement is the selected evidence of its retake chain."""
        return self.selected_measurement_id == self.measurement_id

    @property
    def has_lineage(self) -> bool:
        """Whether this measurement participates in a retake chain."""
        return (
            self.supersedes_measurement_id is not None
            or self.superseded_by_measurement_id is not None
        )


BatchDuplicateKind = Literal['exact_duplicate', 'same_acquisition', 'new']
BatchResolution = Literal['reuse_existing', 'import_as_new']
BatchItemStatus = Literal['staged', 'committed', 'reused', 'failed']
BatchCommitResult = Literal[
    'committed',
    'reused',
    'already_committed',
    'skipped',
    'failed',
]


@dataclass(frozen=True, slots=True)
class BatchImportItem:
    """Public view of one staged batch-import entry (#446)."""

    item_id: str
    filename: str
    source_kind: str
    status: BatchItemStatus
    error: str | None
    duplicate_kind: BatchDuplicateKind
    duplicate_of_measurement_id: str | None
    duplicate_of_name: str | None
    # Set when the item duplicates another *uncommitted* batch entry rather
    # than persisted evidence — the reuse target resolves at commit time.
    duplicate_of_item_id: str | None
    resolution: BatchResolution
    assignment: MeasurementAssignment | None
    committed_measurement_id: str | None
    attachment_count: int
    sample_count: int
    frequency_band_hz: tuple[float, float] | None
    has_phase_samples: bool | None


@dataclass(frozen=True, slots=True)
class BatchCommitOutcome:
    """Per-item result of an explicit batch save (#446)."""

    item: BatchImportItem
    outcome: BatchCommitResult
    measurement_id: str | None
    error: str | None


@dataclass(slots=True)
class _BatchEntry:
    """Mutable internal state behind each :class:`BatchImportItem`."""

    item_id: str
    source_kind: str
    source_label: str
    raw_bytes: bytes | None
    rew_snapshot: RewFrequencyResponseSnapshot | None
    pending: PendingMeasurementImport | None
    error: str | None
    assignment: MeasurementAssignment | None
    duplicate_kind: BatchDuplicateKind
    duplicate_of_measurement_id: str | None
    resolution: BatchResolution
    committed_measurement_id: str | None
    committed: bool
    # (filename, kind, raw bytes, note) staged until the item is committed.
    attachments: list[tuple[str, str, bytes, str | None]]
    # Commit resume state (#801): normalization and the acquisition context
    # are built once per entry so a retry after a mid-commit failure resumes
    # the same logical item instead of registering duplicates.
    commit_payload: (
        tuple[CadMeasurementRecord, CadFrequencyResponseDataset, str, bytes] | None
    ) = None
    commit_context: CadAcquisitionContext | None = None
    # In-queue duplicate edge: this entry carries the same source identity
    # as an earlier *uncommitted* batch item (persisted lookups cannot see
    # it yet), so reuse must resolve the sibling's committed identity.
    duplicate_of_item_id: str | None = None


@dataclass(frozen=True, slots=True)
class AssignmentCorrection:
    """Corrected evidence-binding fields (#509). None leaves the field unchanged.

    ``correction_kind``/``pose_evidence_ref`` (#863): relabeling a measurement
    onto a differently positioned target requires pinning an exact pose-
    observation authority; the repository rejects the correction otherwise.
    """

    measurement_entity_id: str | None = None
    channel_role: str | None = None
    source_speaker_ids: tuple[str, ...] | None = None
    radiation_scope: RadiationScope | None = None
    routing_evidence: RoutingEvidence | None = None
    correction_kind: CorrectionKind | None = None
    pose_evidence_ref: ExactExternalAuthorityRef | None = None


@dataclass(frozen=True, slots=True)
class SpatialEntityChange:
    """One entity-level difference between bound and current scene (#487)."""

    kind: Literal['moved', 'removed', 'added', 'changed']
    entity_id: str
    name: str
    distance_m: float | None


@dataclass(frozen=True, slots=True)
class MeasurementSpatialContext:
    """Spatial context of a saved measurement: bound vs current scene (#487)."""

    record: CadMeasurementRecord
    bound_revision: SceneRevision | None
    current_revision: SceneRevision | None
    bound_is_current: bool
    measurement_position: Any
    measurement_direction: Any
    effective_entity_id: str
    effective_source_speaker_ids: tuple[str, ...]
    changes: tuple[SpatialEntityChange, ...]
    room_changed: bool


@dataclass(frozen=True, slots=True)
class RunnerSourceOption:
    """One selectable campaign source: a speaker or a routed speaker group.

    ``channel_role`` is the logical role the runner cell records;
    ``speaker_entity_ids`` is the exact radiator set — a grouped option
    keeps several physical speakers under one logical source so a single
    subwoofer is never silently equated with the LFE channel (#925).
    """

    key: str
    channel_role: str
    speaker_entity_ids: tuple[str, ...]
    grouped: bool


@dataclass(frozen=True, slots=True)
class RunnerPlanPreview:
    """Pre-persistence plan shape — what ``create_runner_plan`` would make."""

    scene_revision_id: str
    source_count: int
    target_count: int
    repeat_count: int
    purposes: tuple[RunnerPurpose, ...]
    cell_count: int


_RUNNER_ROLE_TOKENS = {
    'FL': 'front_left',
    'FR': 'front_right',
    'C': 'center',
    'LFE': 'subwoofer',
    'SUB': 'subwoofer',
}
_RUNNER_PURPOSE_LABELS = {
    'measurement': '測定',
    'calibration': '校正',
    'holdout': 'ホールドアウト',
    'diagnostic': '診断',
}


def _runner_purpose(value: str | None) -> RunnerPurpose:
    if value in get_args(RunnerPurpose):
        return cast(RunnerPurpose, value)
    return 'measurement'


class MeasurementWorkflowController:
    """Thin UX130 orchestration over existing measurement/import/comparison authorities.

    The controller owns only transient workflow state. Persisted measurement meaning
    stays in CadMeasurementRecord/CadFrequencyResponseDataset and
    CadMeasurementRepository.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        document_id: str,
        *,
        measurement_repository: CadMeasurementRepository | None = None,
        quality_repository: CadMeasurementQualityRepository | None = None,
        rew_client: RewReadSource | None = None,
        listener_pose_repository: CadListenerPoseRepository | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.document_id = document_id
        self._batch: dict[str, _BatchEntry] = {}
        self._calibration_repository: Any = None
        self.measurement_repository = (
            measurement_repository
            if measurement_repository is not None
            else CadMeasurementRepository(scene_repository)
        )
        self.listener_pose_repository = (
            listener_pose_repository
            if listener_pose_repository is not None
            else CadListenerPoseRepository(
                scene_repository.path, scene_repository
            )
        )
        self.quality_repository = (
            quality_repository
            if quality_repository is not None
            else CadMeasurementQualityRepository(
                self.measurement_repository,
                listener_pose_repository=self.listener_pose_repository,
            )
        )
        if rew_client is None:
            from .rew_api import RewApiClient

            rew_client = RewApiClient()
        self.rew_client = rew_client
        self._pending: PendingMeasurementImport | None = None
        self.runner_repository = CadMeasurementRunnerRepository(
            scene_repository,
            self.measurement_repository,
        )
        # Lazily built: the producer derives quality reports from provable
        # evidence at every commit/promote path (#REV42-QUALITYPROD).
        self._producer: CadMeasurementQualityProducer | None = None
        self._producer_resolved = False
        self._target_pattern_repository: CadTargetPatternRepository | None = None
        self._variant_campaign_repository: (
            CadSystemVariantMeasurementCampaignRepository | None
        ) = None
        self._campaign_execution_repository: Any = None

    @property
    def pending_import(self) -> PendingMeasurementImport | None:
        return self._pending

    def latest_revision(self) -> SceneRevision:
        revision = self.scene_repository.current_head(self.document_id)
        if revision is None:
            raise MeasurementWorkflowError(
                "測定を読み込む前に、部屋を一度保存してください"
            )
        return revision

    def _stage_revision(
        self, scene_revision_id: str | None
    ) -> tuple[SceneRevision, bool]:
        """Resolve the acquisition-time SceneRevision for a staged import.

        ``None`` proposes the current head (the default — the measurement
        claims no acquisition revision until the user confirms it); an
        explicit id must resolve to a persisted revision of this document so
        an import can be committed to the layout it was actually acquired
        against (#659).
        """
        if scene_revision_id is None:
            return self.latest_revision(), False
        revision = self.scene_repository.get(scene_revision_id)
        if revision is None or revision.document_id != self.document_id:
            raise MeasurementWorkflowError(
                "取得時のシーンリビジョンを確認できません"
            )
        return revision, True

    def stage_rew_text(
        self,
        raw: bytes,
        filename: str,
        *,
        scene_revision_id: str | None = None,
    ) -> PendingMeasurementImport:
        revision, explicit = self._stage_revision(scene_revision_id)
        parsed = parse_rew_frequency_response(raw)
        pending = PendingMeasurementImport(
            source_kind="rew_text",
            source_label=filename,
            scene_revision_id=revision.revision_id,
            scene_content_hash=revision.content_hash,
            frequency_hz=parsed.frequency_hz,
            level_db=parsed.level_db,
            has_phase_samples=parsed.phase_deg is not None,
            scene_revision_explicit=explicit,
            raw_text=raw,
            raw_filename=filename,
        )
        duplicate_kind, duplicate_of = self._classify_duplicate(pending, raw)
        pending = replace(
            pending,
            duplicate_kind=duplicate_kind,
            duplicate_of_measurement_id=duplicate_of,
        )
        self._pending = pending
        self._journal_pending()
        return pending

    def stage_rew_snapshot(
        self,
        snapshot: RewFrequencyResponseSnapshot,
        *,
        scene_revision_id: str | None = None,
    ) -> PendingMeasurementImport:
        revision, explicit = self._stage_revision(scene_revision_id)
        decoded = snapshot.decoded
        title = snapshot.measurement_summary.get("title")
        source_label = (
            str(title).strip()
            if isinstance(title, str) and title.strip()
            else f"REW {decoded.measurement_id}"
        )
        pending = PendingMeasurementImport(
            source_kind="rew_api",
            source_label=source_label,
            scene_revision_id=revision.revision_id,
            scene_content_hash=revision.content_hash,
            frequency_hz=decoded.frequency_hz,
            level_db=decoded.magnitude,
            has_phase_samples=decoded.phase_deg is not None,
            scene_revision_explicit=explicit,
            rew_snapshot=snapshot,
        )
        duplicate_kind, duplicate_of = self._classify_duplicate(pending, None)
        pending = replace(
            pending,
            duplicate_kind=duplicate_kind,
            duplicate_of_measurement_id=duplicate_of,
        )
        self._pending = pending
        self._journal_pending()
        return pending

    def revision_options(self) -> tuple[SceneRevision, ...]:
        """Acquisition-time revision choices for the picker: head first.

        The current head is the proposal; earlier revisions are listed so a
        measurement taken against a previous layout can be bound to it
        explicitly instead of silently joining the newest configuration.
        """
        return tuple(
            reversed(self.scene_repository.list_revisions(self.document_id))
        )

    def select_pending_revision(
        self, scene_revision_id: str
    ) -> PendingMeasurementImport:
        """Re-bind the staged import to an explicitly chosen revision."""
        pending = self._pending
        if pending is None:
            raise MeasurementWorkflowError("先にREWデータを読み込んでください")
        revision = self.scene_repository.get(scene_revision_id)
        if revision is None or revision.document_id != self.document_id:
            raise MeasurementWorkflowError(
                "取得時のシーンリビジョンを確認できません"
            )
        pending = replace(
            pending,
            scene_revision_id=revision.revision_id,
            scene_content_hash=revision.content_hash,
            scene_revision_explicit=True,
        )
        self._pending = pending
        self._journal_pending()
        return pending

    def pending_divergence(self) -> bool:
        """Whether the staged import's bound revision diverges from the head."""
        pending = self._pending
        if pending is None:
            return False
        current = self.scene_repository.current_head(self.document_id)
        return (
            current is None
            or current.revision_id != pending.scene_revision_id
            or current.content_hash != pending.scene_content_hash
        )

    def clear_pending(self) -> None:
        self._pending = None
        self._journal_pending()

    def _journal_pending(self) -> None:
        """#883: mirror the staged import into the session journal so a
        crash mid-staging leaves honest recoverable evidence. The journal
        is volatile trace, never canonical — commit/clear stay the
        authority; journaling failures never gate the workflow.
        """

        try:
            from .session_recovery import declare_pending_import

            declare_pending_import(
                None
                if self._pending is None
                else {
                    'document_id': self.document_id,
                    **_pending_import_journal_state(self._pending),
                }
            )
        except Exception:
            pass

    def list_rew_measurements(
        self, *, cancel_event: Event | None = None
    ) -> list[dict[str, Any]]:
        return self.rew_client.list_measurements(
            is_cancelled=(
                None if cancel_event is None else cancel_event.is_set
            )
        )

    def fetch_rew_snapshot(
        self, measurement_uuid: str, *, cancel_event: Event | None = None
    ) -> RewFrequencyResponseSnapshot:
        return self.rew_client.get_frequency_response_snapshot(
            measurement_uuid,
            unit="SPL",
            ppo=None,
            smoothing=None,
            is_cancelled=(
                None if cancel_event is None else cancel_event.is_set
            ),
        )

    def _assignment_revision(self) -> SceneRevision:
        pending = self._pending
        if pending is None:
            return self.latest_revision()
        revision = self.scene_repository.get(pending.scene_revision_id)
        if revision is None or revision.content_hash != pending.scene_content_hash:
            raise MeasurementWorkflowError(
                "読み込み時の部屋データを確認できません。REWを読み込み直してください"
            )
        return revision

    def assignment_targets(self) -> tuple[AssignmentTarget, ...]:
        """Eligible measurement targets: listener positions, never speakers.

        Source/measurement-receiver semantics are separate (#475): a
        speaker's acoustic reference serves directivity/excitation — it is
        not a seat target. Seat-to-point binding stays on the #472 lineage
        path (``derive_measurement_point_from_seat``).
        """
        revision = self._assignment_revision()
        return tuple(
            AssignmentTarget(entity.entity_id, entity.name, entity.kind)
            for entity in revision.document.entities
            if is_measurement_target_eligible(entity)
        )

    def source_speakers(self) -> tuple[SpeakerTarget, ...]:
        revision = self._assignment_revision()
        return tuple(
            SpeakerTarget(
                entity_id=entity.entity_id,
                name=entity.name,
                role=(
                    "未設定"
                    if is_unassigned_speaker_role(entity.speaker_role)
                    else entity.speaker_role or "unknown"
                ),
            )
            for entity in revision.document.entities
            if entity.kind == "speaker"
        )

    def commit_pending(
        self,
        assignment: MeasurementAssignment,
        *,
        on_divergence: Literal['reject', 'historical', 'accept_current'] = 'reject',
    ) -> CadMeasurementRecord:
        """Commit the staged import against its bound SceneRevision.

        ``on_divergence`` decides what happens when the document head moved
        after staging (#659): 'reject' keeps the historical behaviour (fail
        with the stale-layout error — the user can hold the import pending
        and re-choose); 'historical' commits to the explicitly bound past
        revision without moving the head; 'accept_current' re-binds the
        staged data to the current head before committing.
        """
        pending = self._pending
        if pending is None:
            raise MeasurementWorkflowError("先にREWデータを読み込んでください")

        current = self.latest_revision()
        diverged = (
            current.revision_id != pending.scene_revision_id
            or current.content_hash != pending.scene_content_hash
        )
        if diverged and on_divergence == 'reject':
            raise MeasurementWorkflowError(
                "読み込み後に部屋の保存状態が変更されています。REWを読み込み直して割り当てを確認してください"
            )
        if diverged and on_divergence == 'accept_current':
            # Re-choose: the user confirms the measurement is interpreted
            # against the current head (equivalent to staging against it).
            self.select_pending_revision(current.revision_id)
            pending = self._pending
            assert pending is not None
        # 'historical' keeps the pending's bound revision — the measurement
        # commits to the layout it was acquired against without moving the
        # document head (#659).
        revision = self._assignment_revision()
        record, dataset, raw_filename, raw_bytes = self._normalize_for_commit(
            pending, assignment, revision
        )
        self.measurement_repository.save(
            record,
            dataset,
            raw_filename=raw_filename,
            raw_bytes=raw_bytes,
        )
        self._save_acquisition_context(assignment, record)
        self._produce_quality_report(record.measurement_id)
        self._pending = None
        self._journal_pending()
        return record

    def _engine_session_payload(self) -> dict[str, Any] | None:
        """The acquisition engine session for provenance merge (#599).

        Resolved lazily at commit so imports from a file (rew_text) with no
        live REW keep working; a client without ``engine_session`` (test
        doubles, older adapters) yields no session provenance.
        """
        opener = getattr(self.rew_client, 'engine_session', None)
        if opener is None:
            return None
        try:
            session = opener()
        except Exception:
            return None
        payload = session.payload() if hasattr(session, 'payload') else None
        return payload

    def _routing_provenance(
        self, assignment: MeasurementAssignment
    ) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
        """Resolve a persisted routing profile for provenance + derivation (#473).

        Returns the provenance merge payload and the observed speaker ids the
        profile's entry for the assignment role carries — used only when the
        assignment left ``source_speaker_ids`` empty, so an explicit manual
        selection always wins over a derived one.

        ``routing_evidence='verified'`` is the strong claim: it requires a
        bound profile scoped to this exact document whose entry for the
        role was itself verified — a bare evidence label can never stand
        in for a resolvable routing authority (#858).
        """
        if assignment.routing_profile_id is None:
            if assignment.routing_evidence == 'verified':
                raise MeasurementWorkflowError(
                    "検証済みルーティングを主張するには保存済みルーティングプロファイルが必要です"
                )
            return None, ()
        profile = self.quality_repository.get_routing_profile(
            assignment.routing_profile_id
        )
        if profile is None:
            raise MeasurementWorkflowError(
                f"保存済みルーティングプロファイルを確認できません: "
                f"{assignment.routing_profile_id}"
            )
        if (
            profile.document_id is not None
            and profile.document_id != self.document_id
        ):
            raise MeasurementWorkflowError(
                "ルーティングプロファイルはこのプロジェクトのものではありません"
            )
        entry = profile.entry_for_role(assignment.channel_role)
        if assignment.routing_evidence == 'verified' and (
            profile.document_id is None
            or entry is None
            or entry.verification != 'verified'
        ):
            raise MeasurementWorkflowError(
                "検証済みルーティングの主張には、このプロジェクトにスコープされた"
                "プロファイル内の検証済みチャネルマップエントリが必要です"
            )
        if (
            entry is not None
            and entry.observed_speaker_ids
            and assignment.source_speaker_ids
            and frozenset(assignment.source_speaker_ids)
            != frozenset(entry.observed_speaker_ids)
        ):
            # The summary cannot silently contradict the bound verified
            # authority — diverging speakers require a different profile.
            raise MeasurementWorkflowError(
                "割り当てのソーススピーカーがルーティングプロファイルの"
                "検証済みエントリと一致しません"
            )
        provenance: dict[str, Any] = {
            'routing_profile_id': profile.routing_profile_id,
            'routing_profile_sha256': profile.routing_profile_sha256,
        }
        if entry is not None:
            provenance['entry'] = {
                'logical_role': entry.logical_role,
                'output_device_label': entry.output_device_label,
                'rew_channel_label': entry.rew_channel_label,
                'hardware_channel_index': entry.hardware_channel_index,
                'verification': entry.verification,
            }
        observed: tuple[str, ...] = ()
        if entry is not None:
            observed = tuple(entry.observed_speaker_ids)
        return provenance, observed

    def acquisition_contexts(
        self,
    ) -> tuple[CadAcquisitionContext, ...]:
        """Persisted acquisition contexts usable as reusable presets (#471)."""
        return tuple(self.quality_repository.list_acquisition_contexts())

    def import_ir_for_measurement(
        self,
        measurement_id: str,
        raw: bytes,
        *,
        filename: str,
        sample_rate_hz: float | None = None,
        t0_semantics: IrT0Semantics = 'export_t0',
        amplitude_reference: IrAmplitudeReference = 'normalized',
        normalized: bool = True,
        window_kind: str | None = None,
        ir_semantics: IrSemantics = 'deconvolved',
        calibration_state: IrCalibrationState = 'uncalibrated',
    ) -> CadImpulseResponseDataset:
        """Import one measured impulse response onto an existing measurement (#474).

        The IR binds to the same measurement record — and therefore the same
        SceneRevision and acquisition context — as the frequency response.
        Interpretation fields that the text export cannot carry are explicit
        importer declarations sealed into the dataset; a normalized IR never
        becomes absolute SPL.
        """
        record = self.measurement_repository.get_measurement(measurement_id)
        if record is None:
            raise MeasurementWorkflowError(
                f"測定を確認できません: {measurement_id}"
            )
        dataset, raw_filename, raw_bytes = normalize_rew_ir_text(
            measurement_id,
            raw,
            filename=filename,
            sample_rate_hz=sample_rate_hz,
            t0_semantics=t0_semantics,
            amplitude_reference=amplitude_reference,
            normalized=normalized,
            window_kind=window_kind,
            ir_semantics=ir_semantics,
            calibration_state=calibration_state,
        )
        saved = self.measurement_repository.save_ir_dataset(
            dataset,
            raw_filename=raw_filename,
            raw_bytes=raw_bytes,
        )
        # IR evidence is a new report epoch: re-derive so
        # has_impulse_response/ir_window claims bind the persisted IR.
        self._produce_quality_report(measurement_id)
        return saved

    def ir_datasets_for_measurement(
        self, measurement_id: str
    ) -> tuple[CadImpulseResponseDataset, ...]:
        return self.measurement_repository.ir_datasets_for_measurement(
            measurement_id
        )

    def measurement_environment_ref(
        self,
        measurement_id: str,
    ) -> 'ExactExternalAuthorityRef | None':
        """The exact AcousticEnvironmentProfile a measurement was captured
        under (#479) — resolved from its persisted acquisition context."""
        for context in self.quality_repository.list_acquisition_contexts():
            if measurement_id in context.subject_measurement_ids:
                return context.environment_ref
        return None

    def environment_compatibility_for(
        self,
        measurement_id: str,
        prediction_environment: 'ExactExternalAuthorityRef | None',
    ) -> str:
        """Predicted-vs-measured environment claim (#479):
        'same' | 'different' | 'unknown' — never a stronger claim than the
        sealed authorities support."""
        from .cad_acoustic_environment import environment_compatibility

        return environment_compatibility(
            prediction_environment,
            self.measurement_environment_ref(measurement_id),
        )

    def derive_measurement_point_from_seat(
        self,
        source_seat_id: str,
        *,
        measurement_point_id: str,
        name: str | None = None,
        listener_pose: 'ListenerPoseAuthority | None' = None,
    ) -> CadMeasurementTargetLineage:
        """Create a measurement point at a seat's listener reference (#472).

        The new point copies the seat's acoustic reference position — the
        coordinate is never retyped — and is committed as a new head
        revision alongside a sealed lineage row recording the source seat,
        the creation revision and the initial position. Later seat moves
        leave the point's stored position untouched and surface as drift.
        """
        from .cad_measurement_targets import (
            MeasurementTargetError,
            build_measurement_target_lineage,
            derive_measurement_point_document,
        )

        revision = self.latest_revision()
        # Draft guard: this path writes a new head outside the owning
        # workspace's working document. A persisted draft (keep_draft or an
        # unresolved crash-recovery snapshot) would be orphaned — its later
        # save could never land on the moved head.
        if self.scene_repository.recovery(self.document_id) is not None:
            raise MeasurementWorkflowError(
                "未保存の部屋の下書きを保存または破棄してから測定点を追加してください"
            )
        if listener_pose is None:
            # A seat with a selected pose derives its measurement point from
            # the pose authority (#632); without one the seat's own
            # acoustic reference offset applies as before.
            listener_pose = self.listener_pose_repository.selected_pose(
                self.document_id, source_seat_id
            )
        try:
            new_document = derive_measurement_point_document(
                revision.document,
                source_seat_id=source_seat_id,
                measurement_point_id=measurement_point_id,
                name=name,
                listener_pose=listener_pose,
            )
        except MeasurementTargetError as exc:
            raise MeasurementWorkflowError(str(exc)) from exc
        try:
            point = new_document.entity(measurement_point_id)
        except KeyError as exc:
            raise MeasurementWorkflowError(str(exc)) from exc
        position = acoustic_reference_position(point)
        if position is None:
            raise MeasurementWorkflowError(
                'measurement point has no acoustic reference position'
            )
        result = self.scene_repository.save(
            new_document,
            parent_revision_id=revision.revision_id,
        )
        lineage = build_measurement_target_lineage(
            document_id=self.document_id,
            measurement_point_id=measurement_point_id,
            source_seat_id=source_seat_id,
            creation_revision_id=result.revision.revision_id,
            initial_position=position,
            source_pose_ref=(
                None
                if listener_pose is None
                else listener_pose.authority_ref()
            ),
            creation_scene_content_hash=result.revision.content_hash,
            source_scene_revision_id=revision.revision_id,
            source_scene_content_hash=revision.content_hash,
        )
        self.quality_repository.save_target_lineage(lineage)
        return lineage

    def measurement_views(self) -> tuple[MeasurementView, ...]:
        latest = self.scene_repository.current_head(self.document_id)
        # Retake lineage is append-only validated evidence; resolving the chain
        # once keeps the per-row topology identical to
        # CadMeasurementQualityRepository.selected_measurement_for_lineage.
        lineage = self.quality_repository.list_lineage(self.document_id)
        lineage_children = {
            event.supersedes_measurement_id: event for event in lineage
        }
        lineage_parents = {event.measurement_id: event for event in lineage}
        records = self.measurement_repository.list_measurements(self.document_id)
        measurements_by_id = {
            record.measurement_id: record for record in records
        }
        # Per-operation verification memos: each dataset still gets the full
        # authoritative read (seals + managed asset + importer replay), but
        # only ONCE per listing — correction/disposition/report validators
        # share the proven rows instead of re-verifying every measurement 3-5
        # times per page view. ``revisions`` memoizes the immutable
        # SceneRevision materialization the same way.
        datasets: dict[str, CadFrequencyResponseDataset] = {}
        revisions: dict[str, SceneRevision | None] = {}

        datasets_for_document = getattr(
            self.measurement_repository, 'datasets_for_document', None
        )
        dataset_by_measurement: dict[str, CadFrequencyResponseDataset] = {}
        dataset_errors: dict[str, BaseException] = {}
        batch_listing_failed = datasets_for_document is None
        if datasets_for_document is not None:
            try:
                dataset_by_measurement, dataset_errors = datasets_for_document(
                    self.document_id, datasets=datasets
                )
            except Exception:
                # A batch-level failure (connection, schema gate) must not
                # kill the listing — fall back to the per-row path so each
                # measurement's error stays isolated on its own view.
                _LOGGER.exception(
                    'document dataset listing failed; falling back to '
                    'per-measurement reads'
                )
                batch_listing_failed = True
        if batch_listing_failed:
            # Stand-in repositories (tests, minimal stores) may implement only
            # the per-measurement read — keep the loop shape for them.
            for record in records:
                try:
                    dataset = self.measurement_repository.dataset_for_measurement(
                        record.measurement_id, datasets=datasets
                    )
                except Exception as exc:
                    dataset_errors[record.measurement_id] = exc
                else:
                    if dataset is not None:
                        dataset_by_measurement[record.measurement_id] = dataset

        # Batched document-scoped overlays replace the per-row
        # latest_correction/latest_disposition/latest_report scans (each of
        # which used to open its own connection AND revalidate a full
        # dataset). Stand-ins without the batched methods fall back to the
        # per-measurement lookups unchanged.
        latest_corrections_fn = getattr(
            self.quality_repository, 'latest_corrections', None
        )
        corrections: dict[str, CadMeasurementCorrection] | None = None
        if latest_corrections_fn is not None:
            corrections = latest_corrections_fn(
                self.document_id,
                datasets=datasets,
                measurements=measurements_by_id,
                revisions=revisions,
                bound=dataset_by_measurement,
            )
        latest_dispositions_fn = getattr(
            self.quality_repository, 'latest_dispositions', None
        )
        dispositions: dict[str, CadMeasurementDisposition] | None = None
        if latest_dispositions_fn is not None:
            dispositions = latest_dispositions_fn(
                self.document_id,
                datasets=datasets,
                measurements=measurements_by_id,
                bound=dataset_by_measurement,
            )
        latest_reports_fn = getattr(
            self.quality_repository, 'latest_reports', None
        )
        reports: dict[str, CadMeasurementQualityReport] | None = None
        report_errors: dict[str, BaseException] = {}
        if latest_reports_fn is not None:
            reports, report_errors = latest_reports_fn(
                self.document_id,
                datasets=datasets,
                measurements=measurements_by_id,
                bound=dataset_by_measurement,
            )
        # First-quality-read backfill (#REV42-QUALITYPROD): a committed
        # measurement whose report epoch was never produced (pre-producer
        # imports, interrupted commits) derives now from stored evidence —
        # the report lands in the same ``reports`` overlay the cells and
        # gated consumers below already read.
        producer = self._quality_producer()
        if producer is not None and reports is not None:
            for record in records:
                if record.measurement_id in reports:
                    continue
                produced = self._produce_quality_report(record.measurement_id)
                if produced is not None:
                    reports[record.measurement_id] = produced
        attachments_for_document = getattr(
            self.measurement_repository, 'attachments_for_document', None
        )
        attachment_counts: dict[str, int] | None = None
        if attachments_for_document is not None:
            attachment_counts = {}
            for attachment in attachments_for_document(self.document_id):
                attachment_counts[attachment.measurement_id] = (
                    attachment_counts.get(attachment.measurement_id, 0) + 1
                )

        latest_correction_row = getattr(
            self.quality_repository, 'latest_correction', None
        )
        latest_disposition_row = getattr(
            self.quality_repository, 'latest_disposition', None
        )
        rows: list[MeasurementView] = []
        for record in records:
            dataset_error: str | None = None
            dataset = dataset_by_measurement.get(record.measurement_id)
            dataset_failure = dataset_errors.get(record.measurement_id)
            if dataset_failure is not None:
                # One row that fails its authoritative re-verification must
                # not take the whole listing down with it: the measurement
                # stays visible with dataset_id=None (so nothing downstream
                # can consume it) and the failure is surfaced on the view.
                _LOGGER.warning(
                    'dataset re-verification failed for %s: %r',
                    record.measurement_id,
                    dataset_failure,
                )
                dataset = None
                dataset_error = operation_error_message(dataset_failure)
            if record.scene_revision_id in revisions:
                source_revision = revisions[record.scene_revision_id]
            else:
                source_revision = self.scene_repository.get(
                    record.scene_revision_id
                )
                revisions[record.scene_revision_id] = source_revision
            # Effective binding = persisted record overlaid by the latest
            # append-only correction (#509). The immutable record fields stay
            # on the view unchanged for audit display. Stand-in quality
            # repositories (tests, minimal stores) may not implement the
            # correction/disposition tables — treat those as "no overlay".
            correction = (
                corrections.get(record.measurement_id)
                if corrections is not None
                else (
                    latest_correction_row(record.measurement_id)
                    if latest_correction_row is not None
                    else None
                )
            )
            disposition_event = (
                dispositions.get(record.measurement_id)
                if dispositions is not None
                else (
                    latest_disposition_row(record.measurement_id)
                    if latest_disposition_row is not None
                    else None
                )
            )
            effective_entity_id = (
                record.measurement_entity_id
                if correction is None or correction.measurement_entity_id is None
                else correction.measurement_entity_id
            )
            effective_channel_role = (
                record.channel_role
                if correction is None or correction.channel_role is None
                else correction.channel_role
            )
            effective_speakers = (
                record.source_speaker_ids
                if correction is None or correction.source_speaker_ids is None
                else correction.source_speaker_ids
            )
            effective_scope: RadiationScope = (
                record.radiation_scope
                if correction is None or correction.radiation_scope is None
                else correction.radiation_scope  # type: ignore[assignment]
            )
            effective_routing: RoutingEvidence = (
                record.routing_evidence
                if correction is None or correction.routing_evidence is None
                else correction.routing_evidence  # type: ignore[assignment]
            )
            target_name = record.measurement_entity_id
            effective_target_name = effective_entity_id
            if source_revision is not None:
                try:
                    target_name = source_revision.document.entity(
                        record.measurement_entity_id
                    ).name
                except KeyError:
                    pass
                try:
                    effective_target_name = source_revision.document.entity(
                        effective_entity_id
                    ).name
                except KeyError:
                    pass
            # Physical-position truth (#863): surface the observed pose
            # position when the correction pins resolvable pose evidence.
            observed_actual_position = None
            if (
                correction is not None
                and correction.pose_evidence_ref is not None
            ):
                resolver = getattr(
                    self.quality_repository, 'pose_evidence_resolver', None
                )
                if resolver is not None:
                    observed_actual_position = resolver(
                        correction.pose_evidence_ref, record.document_id
                    )
            assignment_compatibility = 'original'
            if correction is not None and correction.measurement_entity_id is not None:
                assignment_compatibility = (
                    'pose_observed'
                    if correction.pose_evidence_ref is not None
                    else 'exact'
                )
            disposition_state = (
                None if disposition_event is None else disposition_event.disposition
            )
            band = None
            sample_count = 0
            phase_status: MeasurementPhaseStatus | None = None
            phase_capability: CadMeasurementCapability | None = None
            timing_capability: CadMeasurementCapability | None = None
            capabilities: tuple[CadMeasurementCapability, ...] = ()
            checks: tuple[MeasurementCheckView, ...] = ()
            report_state: Literal[
                'current', 'stale', 'missing', 'error'
            ] = 'missing'
            report_error: str | None = None
            profile_version: str | None = None
            report_created_at: str | None = None
            retake_recommendation: RetakeRecommendation | None = None
            retake_reasons: tuple[str, ...] = ()
            retake_guidance: MeasurementRetakeGuidance | None = None
            dataset_id = None
            if dataset is not None:
                dataset_id = dataset.dataset_id
                sample_count = len(dataset.frequency_hz)
                band = (dataset.frequency_hz[0], dataset.frequency_hz[-1])
                phase_status = dataset.phase_status
                # Capability claims come from the replay-validated quality
                # authority for the exact bound dataset. Valid phase samples
                # alone authorize only phase-response inspection; they never
                # imply a common timing reference, so without a report common
                # timing fails closed at UNKNOWN.
                try:
                    report = self._latest_report_for(
                        record.measurement_id, reports, report_errors
                    )
                except Exception as exc:
                    # A corrupt report must isolate like a corrupt dataset:
                    # flag the row instead of crashing the whole listing.
                    _LOGGER.warning(
                        'quality report re-verification failed for %s: %r',
                        record.measurement_id,
                        exc,
                    )
                    report = None
                    report_state = 'error'
                    report_error = operation_error_message(exc)
                if report is not None and report.dataset_id == dataset.dataset_id:
                    phase_capability = gate_measurement_claim(report, "phase_response")
                    timing_capability = gate_measurement_claim(report, "common_timing")
                    # Full claim matrix (#468): every canonical claim is gated
                    # through the report so historical reports missing a claim
                    # still fail closed at UNKNOWN.
                    capabilities = tuple(
                        gate_measurement_claim(report, claim)
                        for claim in get_args(MeasurementCapabilityClaim)
                    )
                    checks = tuple(
                        MeasurementCheckView(
                            check=name,
                            status=getattr(report, name).status,
                            reason=getattr(report, name).reason,
                        )
                        for name in MEASUREMENT_QUALITY_CHECKS
                    )
                    report_state = 'current'
                    profile_version = report.profile.profile_version
                    report_created_at = report.created_at_utc
                    retake_recommendation = report.retake_recommendation
                    retake_reasons = report.retake_reasons
                    retake_guidance = measurement_retake_guidance(report)
                else:
                    # A report pinned to a different (superseded) dataset is
                    # stale evidence: it must not drive claims for the dataset
                    # actually bound to this measurement.
                    phase_capability = phase_response_capability(dataset)
                    timing_capability = unestablished_common_timing_capability()
                    capabilities = unestablished_capability_claims(dataset)
                    if report is not None:
                        report_state = 'stale'
            elif dataset_error is not None:
                # A persisted report cannot bind the dataset while the
                # dataset itself fails verification — report it as stale
                # rather than silently 'missing'. A report that cannot be
                # re-verified at all is 'error', likewise not 'missing'.
                try:
                    unverifiable_report = self._latest_report_for(
                        record.measurement_id, reports, report_errors
                    )
                except Exception as exc:
                    _LOGGER.warning(
                        'quality report re-verification failed for %s: %r',
                        record.measurement_id,
                        exc,
                    )
                    report_state = 'error'
                    report_error = operation_error_message(exc)
                else:
                    if unverifiable_report is not None:
                        report_state = 'stale'

            superseded_by = lineage_children.get(record.measurement_id)
            supersedes = lineage_parents.get(record.measurement_id)
            head = record.measurement_id
            while head in lineage_children:
                head = lineage_children[head].measurement_id
            head_edge = lineage_parents.get(head)
            selected_id = head if head_edge is None else head_edge.selected_measurement_id

            rows.append(
                MeasurementView(
                    measurement_id=record.measurement_id,
                    dataset_id=dataset_id,
                    evidence_type=record.evidence_type,
                    channel_role=record.channel_role,
                    target_entity_id=record.measurement_entity_id,
                    target_name=target_name,
                    source_kind=record.source_kind,
                    source_speaker_ids=record.source_speaker_ids,
                    routing_evidence=record.routing_evidence,
                    radiation_scope=record.radiation_scope,
                    quality_status=record.quality_status,
                    quality_reasons=record.quality_reasons,
                    quality_source=record.quality_source,
                    phase_status=phase_status,
                    phase_response_capability=phase_capability,
                    common_timing_capability=timing_capability,
                    capabilities=capabilities,
                    quality_checks=checks,
                    quality_report_state=report_state,
                    report_error=report_error,
                    quality_profile_version=profile_version,
                    quality_report_created_at=report_created_at,
                    retake_recommendation=retake_recommendation,
                    retake_reasons=retake_reasons,
                    retake_guidance=retake_guidance,
                    selected_measurement_id=selected_id,
                    supersedes_measurement_id=(
                        None if supersedes is None else supersedes.supersedes_measurement_id
                    ),
                    superseded_by_measurement_id=(
                        None if superseded_by is None else superseded_by.measurement_id
                    ),
                    sample_count=sample_count,
                    frequency_band_hz=band,
                    captured_at=record.captured_at,
                    imported_at=record.imported_at,
                    scene_revision_id=record.scene_revision_id,
                    scene_matches_current=bool(
                        latest is not None
                        and latest.content_hash == record.scene_content_hash
                    ),
                    disposition=disposition_state,
                    disposition_reason=(
                        None if disposition_event is None else disposition_event.reason
                    ),
                    is_normally_eligible=(
                        disposition_state is None
                        or disposition_state in MEASUREMENT_ELIGIBLE_DISPOSITIONS
                    ),
                    is_corrected=correction is not None,
                    effective_target_entity_id=effective_entity_id,
                    effective_target_name=effective_target_name,
                    effective_channel_role=effective_channel_role,
                    effective_source_speaker_ids=effective_speakers,
                    effective_radiation_scope=effective_scope,
                    effective_routing_evidence=effective_routing,
                    smoothing=None if dataset is None else dataset.smoothing,
                    attachment_count=(
                        attachment_counts.get(record.measurement_id, 0)
                        if attachment_counts is not None
                        else (
                            len(
                                self.measurement_repository.list_attachments(
                                    record.measurement_id
                                )
                            )
                            if hasattr(
                                self.measurement_repository, 'list_attachments'
                            )
                            else 0
                        )
                    ),
                    original_import_position=record.measurement_position,
                    observed_actual_position=observed_actual_position,
                    assignment_position_compatibility=assignment_compatibility,
                    dataset_error=dataset_error,
                )
            )
        return tuple(rows)

    def _quality_producer(self) -> CadMeasurementQualityProducer | None:
        """Lazily build the report producer on the real quality authority.

        Stand-in repositories (tests, minimal stores) may not implement the
        observation/report writers — no producer then, and callers skip
        derivation exactly like they skip the batched reads.
        """
        if self._producer_resolved:
            return self._producer
        self._producer_resolved = True
        required = (
            'save_observation',
            'list_observations',
            'save_report',
            'latest_report',
            'list_acquisition_contexts',
            'get_dataset_level_reference',
            'get_level_calibration',
            'validate_calibration_file',
        )
        if all(hasattr(self.quality_repository, name) for name in required):
            self._producer = CadMeasurementQualityProducer(
                self.quality_repository
            )
        return self._producer

    def _produce_quality_report(
        self, measurement_id: str
    ) -> CadMeasurementQualityReport | None:
        """Best-effort derivation; never blocks a completed commit.

        A derivation failure leaves the measurement honestly report-less
        (quality_pending) and is retried by the next quality read — it must
        not retroactively fail a measurement save that already succeeded.
        """
        producer = self._quality_producer()
        if producer is None:
            return None
        try:
            result = producer.produce_report(measurement_id)
        except Exception:
            _LOGGER.warning(
                'quality report production failed for %s',
                measurement_id,
                exc_info=True,
            )
            return None
        if result.status == 'unresolved':
            _LOGGER.warning(
                'quality report unresolved for %s: %s',
                measurement_id,
                result.detail,
            )
            return None
        return result.report

    def reproduce_quality_report(
        self, measurement_id: str
    ) -> CadMeasurementQualityReport | None:
        """Re-derive the quality report after an authority landed (REV44).

        Pinning a dataset level reference (or any new authority the
        producer resolves) changes the report epoch; callers use this
        after persisting the authority so the produced report seals the
        new pin instead of the previous epoch's unresolvable state.
        """
        return self._produce_quality_report(measurement_id)

    def _latest_report_for(
        self,
        measurement_id: str,
        reports: dict[str, CadMeasurementQualityReport] | None,
        report_errors: dict[str, BaseException],
    ) -> CadMeasurementQualityReport | None:
        """Report for one measurement from the batched map (or per-row fallback).

        Batched validation failures surface exactly where the per-row
        ``latest_report`` call would have raised — reached rows re-raise, so
        a corrupt report on an unreachable row stays dormant.
        """
        if reports is None:
            return self.quality_repository.latest_report(measurement_id)
        error = report_errors.get(measurement_id)
        if error is not None:
            raise error
        return reports.get(measurement_id)

    def record_retake(
        self,
        *,
        measurement_id: str,
        supersedes_measurement_id: str,
        selected_measurement_id: str | None = None,
        reason: str,
    ) -> CadMeasurementLineageRecord:
        """Append a retake/selection lineage record between two persisted measurements.

        The repository enforces the append-only single-head contract and the
        identical-binding rule (same SceneRevision, measurement point,
        position, channel role, source speakers, radiation scope); this method
        only requires an explicit reason and defaults the selection to the
        retake measurement. The previous measurement and its quality reports
        are never rewritten or moved — calibration/holdout assignments stay
        bound to the evidence they selected.
        """
        if measurement_id == supersedes_measurement_id:
            raise MeasurementWorkflowError("再測定には元の測定とは別の測定を記録してください")
        if not reason.strip():
            raise MeasurementWorkflowError("再測定の理由を記録してください")
        lineage = build_measurement_lineage(
            document_id=self.document_id,
            measurement_id=measurement_id,
            supersedes_measurement_id=supersedes_measurement_id,
            selected_measurement_id=selected_measurement_id or measurement_id,
            reason=reason.strip(),
        )
        self.quality_repository.save_lineage(lineage)
        return lineage

    def dataset(self, dataset_id: str) -> CadFrequencyResponseDataset:
        dataset = self.measurement_repository.get_dataset(dataset_id)
        if dataset is None:
            raise KeyError(dataset_id)
        return dataset

    def comparison_candidates(
        self,
        evidence_type: MeasurementEvidenceType | None = None,
        *,
        views: tuple[MeasurementView, ...] | None = None,
    ) -> tuple[MeasurementView, ...]:
        """Dataset-bearing measurements eligible for comparison selection (#483).

        ``None`` returns every eligible dataset so the comparison page can
        drive measured/predicted, before/after, retake and seat-to-seat
        pairs from one selector (#509: excluded/misassigned/test/duplicate
        evidence never appears as normally eligible).

        Callers that already hold a fresh listing pass ``views`` — each
        ``measurement_views()`` call is a full evidence re-verification pass,
        so a page refresh must not multiply it per candidate filter.
        """
        return tuple(
            row
            for row in (
                self.measurement_views() if views is None else views
            )
            if row.dataset_id is not None
            and row.is_normally_eligible
            and (evidence_type is None or row.evidence_type == evidence_type)
        )

    def _view_by_dataset(
        self,
        views: tuple[MeasurementView, ...] | None = None,
    ) -> dict[str, MeasurementView]:
        return {
            row.dataset_id: row
            for row in (
                self.measurement_views() if views is None else views
            )
            if row.dataset_id is not None
        }

    def _resolve_comparison_sides(
        self,
        dataset_a_id: str,
        dataset_b_id: str,
        *,
        views: tuple[MeasurementView, ...] | None = None,
    ) -> tuple[MeasurementView, MeasurementView, Any, Any] | None:
        """Resolve both picks to views + semantic side contexts (#852).

        Returns None when either dataset is not a normally-eligible pick;
        side resolution goes through the effective-measurement resolver so
        corrections/dispositions are always reflected.
        """
        views = self._view_by_dataset(views)
        a = views.get(dataset_a_id)
        b = views.get(dataset_b_id)
        if a is None or b is None:
            return None
        side_a = resolve_comparison_side(
            measurement_repository=self.measurement_repository,
            quality_repository=self.quality_repository,
            measurement_id=a.measurement_id,
            target_name=a.effective_target_name,
        )[1]
        side_b = resolve_comparison_side(
            measurement_repository=self.measurement_repository,
            quality_repository=self.quality_repository,
            measurement_id=b.measurement_id,
            target_name=b.effective_target_name,
        )[1]
        return a, b, side_a, side_b

    def comparison_semantics(
        self,
        dataset_a_id: str,
        dataset_b_id: str,
        *,
        reference_band_hz: tuple[float, float] | None = None,
        views: tuple[MeasurementView, ...] | None = None,
    ) -> ComparisonSemantics | None:
        """Typed compatibility + advisory context for a pick pair (#852).

        None when either pick is unavailable. The result snapshots each
        side's effective binding, acquisition context, routing, level
        reference and quality state, then decides absolute-level,
        normalized-shape and common-time eligibility.
        """
        resolved = self._resolve_comparison_sides(
            dataset_a_id, dataset_b_id, views=views
        )
        if resolved is None:
            return None
        _, _, side_a, side_b = resolved
        return derive_comparison_semantics(
            side_a=side_a, side_b=side_b, reference_band_hz=reference_band_hz
        )

    def comparison_mismatches(
        self,
        dataset_a_id: str,
        dataset_b_id: str,
        *,
        views: tuple[MeasurementView, ...] | None = None,
    ) -> tuple[str, ...]:
        """Semantic mismatch codes between two comparison picks (#483/#852).

        Advisory warnings, never blocks: 'evidence_type', 'channel_role',
        'target', 'source_speakers', 'scene_revision', 'smoothing',
        'acquisition_context', 'routing_profile', 'level_reference',
        'timing_reference', 'radiation_scope'.
        """
        semantics = self.comparison_semantics(
            dataset_a_id, dataset_b_id, views=views
        )
        if semantics is None:
            return ()
        return semantics.mismatches

    def compare_datasets(
        self,
        dataset_a_id: str,
        dataset_b_id: str,
        *,
        low_hz: float,
        high_hz: float,
        reference_band_hz: tuple[float, float] | None = None,
        excluded_bands: tuple[tuple[float, float], ...] = (),
        views: tuple[MeasurementView, ...] | None = None,
    ) -> CadMeasurementComparison:
        allowed_dataset_ids = {
            row.dataset_id
            for row in (
                self.measurement_views() if views is None else views
            )
            if row.dataset_id is not None and row.is_normally_eligible
        }
        if dataset_a_id not in allowed_dataset_ids or dataset_b_id not in allowed_dataset_ids:
            raise MeasurementWorkflowError(
                "比較対象は現在のプロジェクトに保存された有効な測定から選択してください"
            )
        dataset_a = self.dataset(dataset_a_id)
        dataset_b = self.dataset(dataset_b_id)
        result = compare_frequency_responses(
            FrequencyResponse(dataset_a.frequency_hz, dataset_a.level_db),
            FrequencyResponse(dataset_b.frequency_hz, dataset_b.level_db),
            low_hz,
            high_hz,
            reference_band_hz=reference_band_hz,
            excluded_bands=excluded_bands,
        )
        semantics = self.comparison_semantics(
            dataset_a_id,
            dataset_b_id,
            reference_band_hz=reference_band_hz,
            views=views,
        )
        return self.measurement_repository.save_comparison(
            dataset_a_id,
            dataset_b_id,
            result,
            semantics_json=(
                None
                if semantics is None
                else semantics.model_dump_json()
            ),
            label_a=None if semantics is None else semantics.label_a,
            label_b=None if semantics is None else semantics.label_b,
            level_compatibility=(
                None if semantics is None else semantics.level_compatibility
            ),
        )

    def saved_comparisons(self) -> tuple[CadMeasurementComparison, ...]:
        return self.measurement_repository.list_comparisons(self.document_id)

    # ------------------------------------------------------------------
    # Campaign runner (#529): guided source × target × repeat execution over
    # the existing measurement/import authorities. REW stays the acquisition
    # engine; the runner only tracks exact per-cell planned evidence.

    def runner_plans(self) -> tuple[MeasurementRunnerPlan, ...]:
        return self.runner_repository.list_plans(self.document_id)

    def runner_plan_created_at_utc(self) -> dict[str, str]:
        return self.runner_repository.list_plan_created_at_utc(
            self.document_id
        )

    def runner_source_options(self) -> tuple[RunnerSourceOption, ...]:
        """Selectable campaign sources: each speaker plus role-grouped sources.

        Speakers sharing one channel-role token are physical radiators of
        the same logical source; the grouped option keeps them in a single
        runner cell so one physical subwoofer is never silently equated
        with the logical LFE channel (#925).
        """
        speakers = self.source_speakers()
        options: list[RunnerSourceOption] = []
        groups: dict[str, list[str]] = {}
        for speaker in speakers:
            token = _RUNNER_ROLE_TOKENS.get(speaker.role, speaker.role)
            options.append(
                RunnerSourceOption(
                    key=f'speaker:{speaker.entity_id}',
                    channel_role=token,
                    speaker_entity_ids=(speaker.entity_id,),
                    grouped=False,
                )
            )
            group = groups.setdefault(token, [])
            if speaker.entity_id not in group:
                group.append(speaker.entity_id)
        for token, entity_ids in groups.items():
            if len(entity_ids) > 1:
                options.append(
                    RunnerSourceOption(
                        key=f'group:{token}',
                        channel_role=token,
                        speaker_entity_ids=tuple(entity_ids),
                        grouped=True,
                    )
                )
        return tuple(options)

    def _resolve_runner_plan_inputs(
        self,
        sources: tuple[tuple[str, tuple[str, ...]], ...] | None,
        target_entity_ids: tuple[str, ...] | None,
    ) -> tuple[
        tuple[tuple[str, tuple[str, ...]], ...],
        tuple[str, ...],
        SceneRevision,
    ]:
        revision = self.latest_revision()
        entities = {
            entity.entity_id: entity for entity in revision.document.entities
        }
        if sources is None:
            resolved_sources = tuple(
                (_RUNNER_ROLE_TOKENS.get(s.role, s.role), (s.entity_id,))
                for s in self.source_speakers()
            )
        else:
            resolved_sources = tuple(
                (role, tuple(entity_ids)) for role, entity_ids in sources
            )
            for _, speaker_ids in resolved_sources:
                for speaker_id in speaker_ids:
                    entity = entities.get(speaker_id)
                    if entity is None or entity.kind != 'speaker':
                        raise MeasurementWorkflowError(
                            "選択された音源が現在の部屋に存在しません"
                        )
        if target_entity_ids is None:
            resolved_targets = tuple(
                target.entity_id for target in self.assignment_targets()
            )
        else:
            resolved_targets = tuple(target_entity_ids)
            for target_id in resolved_targets:
                entity = entities.get(target_id)
                if entity is None or not is_measurement_target_eligible(entity):
                    raise MeasurementWorkflowError(
                        "選択された測定位置が現在の部屋に存在しません"
                    )
        if not resolved_sources or not resolved_targets:
            raise MeasurementWorkflowError(
                "キャンペーンには少なくとも1つの測定位置と1つの音源が必要です"
            )
        return resolved_sources, resolved_targets, revision

    def preview_runner_plan(
        self,
        *,
        sources: tuple[tuple[str, tuple[str, ...]], ...] | None = None,
        target_entity_ids: tuple[str, ...] | None = None,
        repeat_count: int = 1,
        purposes: tuple[RunnerPurpose, ...] = ('measurement',),
    ) -> RunnerPlanPreview:
        """Cell count of a would-be plan — preview before persistence."""
        resolved_sources, resolved_targets, revision = (
            self._resolve_runner_plan_inputs(sources, target_entity_ids)
        )
        return RunnerPlanPreview(
            scene_revision_id=revision.revision_id,
            source_count=len(resolved_sources),
            target_count=len(resolved_targets),
            repeat_count=repeat_count,
            purposes=purposes,
            cell_count=(
                len(purposes)
                * len(resolved_sources)
                * len(resolved_targets)
                * repeat_count
            ),
        )

    def create_runner_plan(
        self,
        *,
        sources: tuple[tuple[str, tuple[str, ...]], ...] | None = None,
        target_entity_ids: tuple[str, ...] | None = None,
        repeat_count: int = 1,
        purposes: tuple[RunnerPurpose, ...] = ('measurement',),
        allow_skip: bool = False,
    ) -> MeasurementRunnerPlan:
        """Register a new plan from the current scene's speakers and targets.

        With no explicit selection this stays the simple all-speakers x
        all-targets preset; ``sources``/``target_entity_ids`` author an
        exact subset that keeps its source/target/routing identity (#925).
        """
        resolved_sources, resolved_targets, revision = (
            self._resolve_runner_plan_inputs(sources, target_entity_ids)
        )
        plan = build_runner_plan(
            document_id=self.document_id,
            scene_revision_id=revision.revision_id,
            scene_content_hash=revision.content_hash,
            sources=resolved_sources,
            target_entity_ids=resolved_targets,
            repeat_count=repeat_count,
            purposes=purposes,
            allow_skip=allow_skip,
        )
        self.runner_repository.save_plan(plan)
        return plan

    def runner_plan_summary(self, plan: MeasurementRunnerPlan) -> str:
        """Human plan identity for selectors — never the raw plan id.

        Names resolve against the plan's own bound SceneRevision so the
        label cannot drift after later Scene edits (#925).
        """
        revision = self.scene_repository.get(plan.scene_revision_id)
        names = {}
        if revision is not None:
            names = {
                entity.entity_id: (entity.name or entity.entity_id)
                for entity in revision.document.entities
            }
        source_labels: list[str] = []
        seen_sources: set[tuple[str, tuple[str, ...]]] = set()
        target_labels: list[str] = []
        seen_targets: set[str] = set()
        purposes: list[RunnerPurpose] = []
        repeats = 1
        for cell in plan.cells:
            source_key = (cell.channel_role, cell.source_speaker_ids)
            if source_key not in seen_sources:
                seen_sources.add(source_key)
                speaker_names = [
                    names.get(entity_id, entity_id)
                    for entity_id in cell.source_speaker_ids
                ]
                if len(speaker_names) == 1:
                    source_labels.append(speaker_names[0])
                else:
                    source_labels.append(
                        f"{cell.channel_role}（{'+'.join(speaker_names)}）"
                    )
            if cell.target_entity_id not in seen_targets:
                seen_targets.add(cell.target_entity_id)
                target_labels.append(
                    names.get(cell.target_entity_id, cell.target_entity_id)
                )
            if cell.purpose not in purposes:
                purposes.append(cell.purpose)
            repeats = max(repeats, cell.repeat_index + 1)
        target_label = '・'.join(target_labels[:3])
        if len(target_labels) > 3:
            target_label += f" 他{len(target_labels) - 3}"
        source_label = '・'.join(source_labels[:4])
        if len(source_labels) > 4:
            source_label += f" 他{len(source_labels) - 4}"
        purpose_label = '・'.join(
            _RUNNER_PURPOSE_LABELS[purpose] for purpose in purposes
        )
        return (
            f"{target_label} / {source_label} / {purpose_label} / "
            f"{repeats}回 · {len(plan.cells)}セル"
        )

    def target_patterns(self) -> tuple[MeasurementTargetPattern, ...]:
        """Persisted #543 target patterns of this project."""
        return self._target_pattern_repo().list_patterns(self.document_id)

    def target_pattern_entity_ids(self, pattern_id: str) -> tuple[str, ...]:
        """Materialized target entity ids of a #543 pattern, restricted to
        entities that are still eligible targets on the document head."""
        points = self._target_pattern_repo().list_pattern_points(pattern_id)
        eligible = {t.entity_id for t in self.assignment_targets()}
        return tuple(
            point.measurement_point_entity_id
            for point in points
            if point.measurement_point_entity_id in eligible
        )

    def _target_pattern_repo(self) -> CadTargetPatternRepository:
        if self._target_pattern_repository is None:
            from .cad_measurement_target_pattern import (
                CadTargetPatternRepository,
            )

            self._target_pattern_repository = CadTargetPatternRepository(
                self.scene_repository
            )
        return self._target_pattern_repository

    def variant_measurement_plans(
        self,
    ) -> tuple[SystemVariantMeasurementPlan, ...]:
        """Existing exact SystemVariant/validation plans of this project."""
        return self._variant_campaign_repo().list_plans(self.document_id)

    def create_runner_plan_from_variant_plan(
        self,
        plan_id: str,
    ) -> MeasurementRunnerPlan:
        """Project an exact SystemVariant measurement plan into runner cells.

        The runner plan binds the variant plan's exact as-built revision
        and keeps each target's channel role, exact source entity set,
        expected repeat count and purpose — lineage is preserved and no
        extra all×all cells are invented. Projecting the same variant plan
        twice returns the already-persisted runner plan instead of a
        duplicate.
        """
        variant_plan = self._variant_campaign_repo().get_plan(plan_id)
        if variant_plan is None:
            raise MeasurementWorkflowError("対象の測定計画が見つかりません")
        revision = self.scene_repository.get(variant_plan.as_built_revision_id)
        if (
            revision is None
            or revision.document_id != self.document_id
            or revision.content_hash != variant_plan.as_built_content_hash
        ):
            raise MeasurementWorkflowError(
                "測定計画が参照する設置済みリビジョンを確認できません"
            )
        cells: list[RunnerCellSpec] = []
        for target in variant_plan.targets:
            purpose = _runner_purpose(
                target.validation_purpose or variant_plan.purpose
            )
            for repeat_index in range(target.expected_measurement_count):
                cells.append(
                    RunnerCellSpec(
                        cell_index=len(cells),
                        channel_role=target.channel_role,
                        source_speaker_ids=target.source_entity_ids,
                        target_entity_id=target.measurement_point_entity_id,
                        repeat_index=repeat_index,
                        purpose=purpose,
                    )
                )
        for existing in self.runner_plans():
            if (
                existing.scene_revision_id == revision.revision_id
                and existing.scene_content_hash == revision.content_hash
                and list(existing.cells) == cells
            ):
                return existing
        plan = build_runner_plan_from_cells(
            document_id=self.document_id,
            scene_revision_id=revision.revision_id,
            scene_content_hash=revision.content_hash,
            cells=cells,
        )
        self.runner_repository.save_plan(plan)
        return plan

    def _variant_campaign_repo(
        self,
    ) -> CadSystemVariantMeasurementCampaignRepository:
        if self._variant_campaign_repository is None:
            from .cad_system_variant_lifecycle import (
                CadSystemVariantLifecycleRepository,
            )
            from .cad_system_variant_measured_lifecycle import (
                CadSystemVariantMeasuredLifecycleRepository,
            )
            from .cad_system_variant_measurement_campaign import (
                CadSystemVariantMeasurementCampaignRepository,
            )
            from .cad_system_variant_repository import (
                CadSystemVariantRepository,
            )

            variant_repository = CadSystemVariantRepository(
                self.scene_repository
            )
            lifecycle_repository = CadSystemVariantLifecycleRepository(
                scene_repository=self.scene_repository,
                variant_repository=variant_repository,
            )
            measured_lifecycle_repository = (
                CadSystemVariantMeasuredLifecycleRepository(
                    scene_repository=self.scene_repository,
                    lifecycle_repository=lifecycle_repository,
                    measurement_repository=self.measurement_repository,
                    quality_repository=self.quality_repository,
                )
            )
            self._variant_campaign_repository = (
                CadSystemVariantMeasurementCampaignRepository(
                    scene_repository=self.scene_repository,
                    variant_repository=variant_repository,
                    lifecycle_repository=lifecycle_repository,
                    measurement_repository=self.measurement_repository,
                    quality_repository=self.quality_repository,
                    measured_lifecycle_repository=measured_lifecycle_repository,
                )
            )
        return self._variant_campaign_repository

    def open_runner(self, plan_id: str) -> MeasurementRunnerRun:
        """Resume the latest run for the plan, or start a new one."""
        runs = self.runner_repository.list_runs(plan_id)
        if runs:
            return runs[-1]
        return self.runner_repository.start_run(plan_id)

    def runner_cell_states(self, run_id: str) -> dict[int, RunnerCellState]:
        return self.runner_repository.cell_states(run_id)

    def runner_next_incomplete(self, run_id: str) -> int | None:
        return self.runner_repository.next_incomplete(run_id)

    def runner_guided_step(
        self, run_id: str, cell_index: int | None = None
    ) -> GuidedStep | None:
        run = self.runner_repository.get_run(run_id)
        if run is None:
            raise MeasurementWorkflowError("キャンペーンの実行が見つかりません")
        plan = self.runner_repository.get_plan(run.plan_id)
        if cell_index is None:
            cell_index = self.runner_repository.next_incomplete(run_id)
        if cell_index is None:
            return None
        return guided_step(plan, cell_index)

    def runner_progress(self, run_id: str):
        run = self.runner_repository.get_run(run_id)
        if run is None:
            raise MeasurementWorkflowError("キャンペーンの実行が見つかりません")
        try:
            plan, states = self.runner_repository.progress_for_run(run_id)
        except RunnerError as exc:
            raise MeasurementWorkflowError(str(exc)) from exc
        return runner_progress(plan, states)

    def runner_plan_for_run(self, run_id: str) -> MeasurementRunnerPlan:
        run = self.runner_repository.get_run(run_id)
        if run is None:
            raise MeasurementWorkflowError("キャンペーンの実行が見つかりません")
        return self.runner_repository.get_plan(run.plan_id)

    def runner_commit_cell(
        self,
        run_id: str,
        cell_index: int,
        measurement_id: str,
    ) -> None:
        """Commit one saved measurement to the exact planned cell.

        The runner repository canonically derives the cell outcome itself
        (#853): effective binding + eligibility, dataset hash, and the
        latest replay-validated quality report — RETAKE marks the cell
        retake_required, a clean report completes it, otherwise it stays
        quality_pending. The caller never asserts a verdict.
        """
        record = self.measurement_repository.get_measurement(measurement_id)
        if record is None:
            raise MeasurementWorkflowError("登録する測定が保存されていません")
        dataset = self.measurement_repository.dataset_for_measurement(measurement_id)
        if dataset is None:
            raise MeasurementWorkflowError("測定に周波数応答データがありません")
        # Quality read: derive the report now if no commit path produced it.
        self._produce_quality_report(measurement_id)
        self.runner_repository.commit_cell(
            run_id,
            cell_index,
            measurement_id=measurement_id,
            dataset_id=dataset.dataset_id,
            dataset_sha256=dataset.dataset_sha256,
        )

    def runner_skip_cell(self, run_id: str, cell_index: int) -> None:
        self.runner_repository.skip_cell(run_id, cell_index)

    # ------------------------------------------------------------------
    # Native automated campaign execution (#956)
    # ------------------------------------------------------------------

    def _campaign_execution_repo(self):
        if self._campaign_execution_repository is None:
            from .cad_campaign_execution_repository import (
                CadCampaignExecutionRepository,
            )
            from .cad_sweep_acquisition_repository import (
                CadSweepAcquisitionRepository,
            )

            self._campaign_execution_repository = (
                CadCampaignExecutionRepository(
                    self.scene_repository,
                    sweep_repository=CadSweepAcquisitionRepository(
                        self.scene_repository,
                    ),
                )
            )
        return self._campaign_execution_repository

    def materialize_native_campaign(
        self,
        plan_id: str,
        *,
        routings,
        stimulus_template,
        policy=None,
    ):
        """Convert + persist the canonical #875 plan for a runner plan.

        The sealed plan hash is a pure function of its inputs — the same
        runner plan + routing + stimulus always yields the same
        ``mcplan-``, so re-saving is a no-op and any existing journal
        stays attached (that IS resume). A non-canonical runner matrix
        fails closed with a conversion error instead of inventing runs.
        """
        from .cad_campaign_native import (
            NativePlanConversionError,
            materialize_native_campaign_plan,
        )

        runner_plan = self.runner_repository.get_plan(plan_id)
        if runner_plan is None:
            raise MeasurementWorkflowError("対象の測定計画が見つかりません")
        revision = self.scene_repository.get(runner_plan.scene_revision_id)
        if revision is None or revision.document_id != self.document_id:
            raise MeasurementWorkflowError(
                "計画が参照するシーンリビジョンを確認できません"
            )
        created = self.runner_plan_created_at_utc().get(plan_id)
        try:
            materialization = materialize_native_campaign_plan(
                runner_plan=runner_plan,
                revision=revision,
                routings=routings,
                stimulus_template=stimulus_template,
                policy=policy,
                # Deterministic per runner plan — never the wall clock,
                # or every materialization would mint a new plan id.
                generated_at_utc=created or '1970-01-01T00:00:00+00:00',
            )
        except NativePlanConversionError as exc:
            raise MeasurementWorkflowError(str(exc)) from exc
        self._campaign_execution_repo().save_plan(materialization.plan)
        return materialization

    def native_campaign_for_runner_plan(self, plan_id: str):
        """Latest sealed campaign plan materialized from a runner plan."""
        plans = self._campaign_execution_repo().list_plans(self.document_id)
        for plan in reversed(plans):
            if plan.campaign_ref.ref_id == plan_id:
                return plan
        return None

    def native_preflight(self, plan, *, backend, calibration_state='unknown'):
        """Read-only pre-arm report — devices, routing, level, PRECHECK."""
        from .cad_campaign_native import build_native_preflight

        names: dict[str, str] = {}
        if plan.scene_ref is not None:
            revision = self.scene_repository.get(plan.scene_ref.ref_id)
            if revision is not None:
                names = {
                    entity.entity_id: (entity.name or entity.entity_id)
                    for entity in revision.document.entities
                }
        return build_native_preflight(
            plan=plan,
            backend=backend,
            calibration_state=calibration_state,
            position_names=names,
        )

    def native_campaign_drive(
        self,
        plan,
        *,
        engine_factory,
        arm_confirmation_provider,
        calibration_state_provider=None,
    ):
        """Open (or resume, via the sealed journal) a campaign runner."""
        from .cad_campaign_native import NativeCampaignDrive

        return NativeCampaignDrive(
            campaign_repository=self._campaign_execution_repo(),
            plan=plan,
            engine_factory=engine_factory,
            arm_confirmation_provider=arm_confirmation_provider,
            calibration_state_provider=calibration_state_provider,
        )

    def native_campaign_status(self, plan, runner_plan):
        """Derived state + exact cell binding + run records for the UI."""
        from .cad_campaign_execution import derive_campaign_state
        from .cad_campaign_native import resolve_cell_assignments

        repo = self._campaign_execution_repo()
        events = repo.list_events(plan.plan_id)
        records = repo.list_run_records(plan.plan_id)
        state = derive_campaign_state(plan, events, records)
        assignments = resolve_cell_assignments(runner_plan, plan)
        return state, assignments, records

    # ------------------------------------------------------------------
    # Batch campaign import (#446)
    # ------------------------------------------------------------------

    def _duplicate_source_maps(
        self,
    ) -> tuple[dict[str, str], dict[str, str]]:
        """(source_sha256 -> measurement_id, external_source_id -> measurement_id)

        Built once per batch operation so classification is O(1) per staged
        file instead of an O(document) indexed scan per file. Stand-in
        repositories without the bulk methods leave empty maps — per-file
        lookups then fall back to the single-item queries unchanged.
        """
        by_sha_fn = getattr(
            self.measurement_repository,
            'measurement_ids_by_source_sha256',
            None,
        )
        by_ext_fn = getattr(
            self.measurement_repository,
            'measurement_ids_by_external_source',
            None,
        )
        return (
            by_sha_fn(self.document_id) if by_sha_fn is not None else {},
            by_ext_fn(self.document_id) if by_ext_fn is not None else {},
        )

    def _staged_duplicate_sources(
        self,
    ) -> tuple[dict[str, '_BatchEntry'], dict[str, '_BatchEntry']]:
        """(source_sha256 -> entry, external_source_id -> entry) over the
        uncommitted batch queue — the in-flight sibling half of duplicate
        detection.

        ``_classify_duplicate`` only sees persisted evidence, so two
        identical sources staged before either commits would both classify
        'new' and register byte-identical measurements. ``setdefault``
        keeps the earliest sibling as the canonical target.
        """
        by_sha: dict[str, '_BatchEntry'] = {}
        by_ext: dict[str, '_BatchEntry'] = {}
        for entry in self._batch.values():
            if entry.committed or entry.pending is None:
                continue
            if entry.raw_bytes is not None:
                by_sha.setdefault(sha256(entry.raw_bytes).hexdigest(), entry)
            if entry.rew_snapshot is not None:
                by_ext.setdefault(
                    entry.rew_snapshot.decoded.measurement_id, entry
                )
        return by_sha, by_ext

    def _classify_duplicate(
        self,
        pending: PendingMeasurementImport | None,
        raw_bytes: bytes | None,
        source_maps: tuple[dict[str, str], dict[str, str]] | None = None,
    ) -> tuple[BatchDuplicateKind, str | None]:
        """Classify one staged item against persisted evidence.

        Byte-identical raw sources resolve to the measurement whose dataset
        pinned the same ``source_sha256``; REW API snapshots carrying an
        already-imported ``external_source_id`` are the same acquisition
        re-exported. Neither auto-registers anything — classification only
        drives the explicit resolution shown to the user.

        ``source_maps`` is the per-batch-operations lookup built by
        :meth:`_duplicate_source_maps`; absent it falls back to the
        per-item repository queries (correct, just O(document) per file).
        """
        if pending is None:
            return ('new', None)
        if pending.source_kind == 'rew_text' and raw_bytes is not None:
            digest = sha256(raw_bytes).hexdigest()
            measurement_id = (
                source_maps[0].get(digest)
                if source_maps is not None
                else self.measurement_repository.measurement_id_by_source_sha256(
                    self.document_id,
                    digest,
                )
            )
            if measurement_id is not None:
                return ('exact_duplicate', measurement_id)
        elif pending.source_kind == 'rew_api' and pending.rew_snapshot is not None:
            external_id = pending.rew_snapshot.decoded.measurement_id
            measurement_id = (
                source_maps[1].get(external_id)
                if source_maps is not None
                else self.measurement_repository.measurement_id_by_external_source(
                    self.document_id,
                    external_id,
                )
            )
            if measurement_id is not None:
                return ('same_acquisition', measurement_id)
        return ('new', None)

    def _duplicate_names(self) -> dict[str, str]:
        """measurement_id -> effective target name for duplicate display.

        Resolves names from the measurement records + memoized source
        revisions + the batched corrections overlay — the batch table only
        needs the *label*, not the per-row evidence verification
        ``measurement_views()`` performs. Calling that full listing per batch
        item made staging/listing/commit O(batch × measurements × verify).
        """
        records = self.measurement_repository.list_measurements(
            self.document_id
        )
        latest_corrections_fn = getattr(
            self.quality_repository, 'latest_corrections', None
        )
        corrections = (
            latest_corrections_fn(self.document_id)
            if latest_corrections_fn is not None
            else {}
        )
        revisions: dict[str, SceneRevision | None] = {}
        names: dict[str, str] = {}
        for record in records:
            revision_id = record.scene_revision_id
            if revision_id not in revisions:
                revisions[revision_id] = self.scene_repository.get(revision_id)
            revision = revisions[revision_id]
            effective_entity_id = record.measurement_entity_id
            correction = corrections.get(record.measurement_id)
            if (
                correction is not None
                and correction.measurement_entity_id is not None
            ):
                effective_entity_id = correction.measurement_entity_id
            name = effective_entity_id
            if revision is not None:
                try:
                    name = revision.document.entity(effective_entity_id).name
                except KeyError:
                    pass
            names[record.measurement_id] = name
        return names

    def _duplicate_names_for(
        self,
        entries: Iterable[_BatchEntry],
    ) -> dict[str, str]:
        """Build the duplicate-name map once — only when any entry needs it."""
        if not any(
            entry.duplicate_of_measurement_id for entry in entries
        ):
            return {}
        return self._duplicate_names()

    def _batch_item_view(
        self,
        entry: _BatchEntry,
        duplicate_names: dict[str, str] | None = None,
    ) -> BatchImportItem:
        if entry.committed:
            status: BatchItemStatus = (
                'reused'
                if entry.resolution == 'reuse_existing'
                else 'committed'
            )
        elif entry.error is not None:
            status = 'failed'
        else:
            status = 'staged'
        pending = entry.pending
        duplicate_of_name: str | None = None
        if entry.duplicate_of_measurement_id is not None:
            duplicate_of_name = (duplicate_names or {}).get(
                entry.duplicate_of_measurement_id,
                entry.duplicate_of_measurement_id,
            )
        elif entry.duplicate_of_item_id is not None:
            sibling = self._batch.get(entry.duplicate_of_item_id)
            duplicate_of_name = (
                sibling.source_label
                if sibling is not None
                else entry.duplicate_of_item_id
            )
        return BatchImportItem(
            item_id=entry.item_id,
            filename=entry.source_label,
            source_kind=entry.source_kind,
            status=status,
            error=entry.error,
            duplicate_kind=entry.duplicate_kind,
            duplicate_of_measurement_id=entry.duplicate_of_measurement_id,
            duplicate_of_name=duplicate_of_name,
            duplicate_of_item_id=entry.duplicate_of_item_id,
            resolution=entry.resolution,
            assignment=entry.assignment,
            committed_measurement_id=entry.committed_measurement_id,
            attachment_count=len(entry.attachments),
            sample_count=0 if pending is None else pending.sample_count,
            frequency_band_hz=(
                None if pending is None else pending.frequency_band_hz
            ),
            has_phase_samples=None if pending is None else pending.has_phase_samples,
        )

    def batch_items(self) -> tuple[BatchImportItem, ...]:
        entries = list(self._batch.values())
        duplicate_names = self._duplicate_names_for(entries)
        return tuple(
            self._batch_item_view(entry, duplicate_names)
            for entry in entries
        )

    def stage_rew_text_files(
        self,
        files: Iterable[tuple[bytes, str]],
    ) -> tuple[BatchImportItem, ...]:
        """Stage one or many REW text exports for the current scene head (#446).

        Every file is parsed immediately so the pre-save list shows per-item
        status; nothing is persisted until :meth:`commit_batch`.
        """
        revision = self.latest_revision()
        # One document-scoped lookup for the whole batch — classifying each
        # file against persisted evidence stays O(1) per file.
        source_maps = self._duplicate_source_maps()
        # In-queue siblings share the persisted-evidence classification: two
        # identical files staged before either commits must not both pass
        # as 'new' and register two byte-identical measurements.
        staged_sha, _staged_ext = self._staged_duplicate_sources()
        staged_entries: list[_BatchEntry] = []
        try:
            for raw, filename in files:
                pending: PendingMeasurementImport | None = None
                error: str | None = None
                try:
                    parsed = parse_rew_frequency_response(raw)
                except Exception as exc:
                    error = operation_error_message(exc)
                else:
                    pending = PendingMeasurementImport(
                        source_kind='rew_text',
                        source_label=filename,
                        scene_revision_id=revision.revision_id,
                        scene_content_hash=revision.content_hash,
                        frequency_hz=parsed.frequency_hz,
                        level_db=parsed.level_db,
                        has_phase_samples=parsed.phase_deg is not None,
                        raw_text=raw,
                        raw_filename=filename,
                    )
                kind, duplicate_of = self._classify_duplicate(
                    pending, raw, source_maps
                )
                duplicate_of_item_id: str | None = None
                if kind == 'new' and pending is not None:
                    sibling = staged_sha.get(sha256(raw).hexdigest())
                    if sibling is not None:
                        kind = 'exact_duplicate'
                        duplicate_of_item_id = sibling.item_id
                entry = _BatchEntry(
                    item_id=uuid4().hex,
                    source_kind='rew_text',
                    source_label=filename,
                    raw_bytes=raw,
                    rew_snapshot=None,
                    pending=pending,
                    error=error,
                    assignment=None,
                    duplicate_kind='new' if pending is None else kind,
                    duplicate_of_measurement_id=duplicate_of,
                    resolution=(
                        'reuse_existing' if kind == 'exact_duplicate' else 'import_as_new'
                    ),
                    committed_measurement_id=None,
                    committed=False,
                    attachments=[],
                    duplicate_of_item_id=duplicate_of_item_id,
                )
                self._batch[entry.item_id] = entry
                staged_entries.append(entry)
                if pending is not None:
                    staged_sha.setdefault(sha256(raw).hexdigest(), entry)
            # The duplicate-name label map is built once for all staged
            # items — never inside the per-item view path.
            names = self._duplicate_names_for(staged_entries)
            return tuple(
                self._batch_item_view(entry, names) for entry in staged_entries
            )
        except Exception:
            # A mid-stage failure (e.g. the duplicate-name repository read)
            # must not leak the entries appended so far — a retry would
            # stage the same files again and double the queue rows.
            self._unstage_batch_entries(staged_entries)
            raise

    def stage_rew_snapshots(
        self,
        snapshots: Iterable[RewFrequencyResponseSnapshot],
    ) -> tuple[BatchImportItem, ...]:
        """Stage already-fetched REW API measurements into the batch queue."""
        revision = self.latest_revision()
        source_maps = self._duplicate_source_maps()
        _staged_sha, staged_ext = self._staged_duplicate_sources()
        staged_entries: list[_BatchEntry] = []
        try:
            for snapshot in snapshots:
                pending: PendingMeasurementImport | None = None
                error: str | None = None
                try:
                    decoded = snapshot.decoded
                    title = snapshot.measurement_summary.get('title')
                    source_label = (
                        str(title).strip()
                        if isinstance(title, str) and title.strip()
                        else f"REW {decoded.measurement_id}"
                    )
                    pending = PendingMeasurementImport(
                        source_kind='rew_api',
                        source_label=source_label,
                        scene_revision_id=revision.revision_id,
                        scene_content_hash=revision.content_hash,
                        frequency_hz=decoded.frequency_hz,
                        level_db=decoded.magnitude,
                        has_phase_samples=decoded.phase_deg is not None,
                        rew_snapshot=snapshot,
                    )
                except Exception as exc:
                    error = operation_error_message(exc)
                kind, duplicate_of = self._classify_duplicate(
                    pending, None, source_maps
                )
                duplicate_of_item_id = None
                if kind == 'new' and pending is not None:
                    sibling = staged_ext.get(pending.rew_snapshot.decoded.measurement_id)
                    if sibling is not None:
                        kind = 'same_acquisition'
                        duplicate_of_item_id = sibling.item_id
                entry = _BatchEntry(
                    item_id=uuid4().hex,
                    source_kind='rew_api',
                    source_label=(
                        pending.source_label if pending is not None else 'REW API'
                    ),
                    raw_bytes=None,
                    rew_snapshot=snapshot,
                    pending=pending,
                    error=error,
                    assignment=None,
                    duplicate_kind='new' if pending is None else kind,
                    duplicate_of_measurement_id=duplicate_of,
                    resolution=(
                        'reuse_existing' if kind == 'exact_duplicate' else 'import_as_new'
                    ),
                    committed_measurement_id=None,
                    committed=False,
                    attachments=[],
                    duplicate_of_item_id=duplicate_of_item_id,
                )
                self._batch[entry.item_id] = entry
                staged_entries.append(entry)
                if pending is not None:
                    staged_ext.setdefault(
                        pending.rew_snapshot.decoded.measurement_id, entry
                    )
            names = self._duplicate_names_for(staged_entries)
            return tuple(
                self._batch_item_view(entry, names) for entry in staged_entries
            )
        except Exception:
            # Same atomicity rule as stage_rew_text_files: a mid-stage
            # failure must not leak appended entries for a retry to
            # duplicate.
            self._unstage_batch_entries(staged_entries)
            raise

    def set_batch_resolution(self, item_id: str, resolution: BatchResolution) -> None:
        entry = self._batch.get(item_id)
        if entry is None:
            raise MeasurementWorkflowError("バッチ項目が見つかりません")
        if entry.committed:
            raise MeasurementWorkflowError("保存済みの項目は変更できません")
        entry.resolution = resolution

    def apply_batch_assignment(
        self,
        assignment: MeasurementAssignment,
        *,
        item_ids: Iterable[str] | None = None,
    ) -> int:
        """Bulk-apply one assignment to staged batch items (#446).

        ``item_ids=None`` applies to every uncommitted parseable item;
        a restricted list is the per-item override path.
        """
        selected = (
            list(self._batch.values())
            if item_ids is None
            else [self._batch[i] for i in item_ids if i in self._batch]
        )
        applied = 0
        for entry in selected:
            if entry.committed or entry.pending is None:
                continue
            entry.assignment = assignment
            applied += 1
        return applied

    def set_batch_item_assignment(
        self,
        item_id: str,
        assignment: MeasurementAssignment,
    ) -> None:
        entry = self._batch.get(item_id)
        if entry is None:
            raise MeasurementWorkflowError("バッチ項目が見つかりません")
        if entry.committed:
            raise MeasurementWorkflowError("保存済みの項目は変更できません")
        if entry.pending is None:
            raise MeasurementWorkflowError("解析に失敗した項目には割り当てできません")
        entry.assignment = assignment

    def auto_assign_batch_items(
        self,
        *,
        item_ids: Iterable[str] | None = None,
    ) -> tuple[int, int]:
        """Name-match unassigned batch items to measurement targets (REV40).

        For every uncommitted, parseable, still-unassigned entry, the
        single eligible target whose normalized name appears in the
        entry's source label is applied. Entries matching zero or several
        targets keep ``assignment=None`` — the user resolves them on the
        assignment path exactly as before. Items the user already assigned
        are never overwritten.

        Returns ``(applied, unresolved)``: how many entries received an
        assignment and how many eligible entries could not be matched.
        """
        targets = self.assignment_targets()
        if not targets:
            return 0, 0
        selected = (
            list(self._batch.values())
            if item_ids is None
            else [self._batch[i] for i in item_ids if i in self._batch]
        )
        applied = 0
        unresolved = 0
        for entry in selected:
            if (
                entry.committed
                or entry.pending is None
                or entry.assignment is not None
            ):
                continue
            chosen, _candidates = propose_assignment_target(
                entry.source_label, targets
            )
            if chosen is None:
                unresolved += 1
                continue
            entry.assignment = MeasurementAssignment(
                measurement_entity_id=chosen.entity_id
            )
            applied += 1
        return applied, unresolved

    def attach_to_batch_item(
        self,
        item_id: str,
        *,
        filename: str,
        raw_bytes: bytes,
        kind: str,
        note: str | None = None,
    ) -> None:
        """Stage a raw source attachment for one batch item (#446).

        Bytes stay in memory until the item commits, then land in the
        managed-asset store and the backup-included manifest.
        """
        entry = self._batch.get(item_id)
        if entry is None:
            raise MeasurementWorkflowError("バッチ項目が見つかりません")
        if entry.committed:
            raise MeasurementWorkflowError(
                "保存済みの項目への添付は測定に直接追加してください"
            )
        entry.attachments.append((filename, kind, bytes(raw_bytes), note))

    def _install_staged_attachments(self, entry: _BatchEntry) -> None:
        """Persist staged attachments, skipping ones already installed (#801).

        Identical (filename, kind, sha256) attachments are deduplicated so a
        retry after a partial install does not register them twice.
        """
        if entry.committed_measurement_id is None:
            return
        installed = {
            (attachment.filename, attachment.kind, attachment.sha256)
            for attachment in self.measurement_repository.list_attachments(
                entry.committed_measurement_id
            )
        }
        for filename, kind, raw_bytes, note in entry.attachments:
            if (filename, kind, sha256(raw_bytes).hexdigest()) in installed:
                continue
            self.measurement_repository.save_attachment(
                measurement_id=entry.committed_measurement_id,
                kind=kind,
                filename=filename,
                raw_bytes=raw_bytes,
                note=note,
            )
        entry.attachments.clear()

    def commit_batch(
        self,
        item_ids: Iterable[str] | None = None,
        *,
        cancel_event: Event | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> tuple[BatchCommitOutcome, ...]:
        """Explicitly persist every committable staged item (#446).

        Only items that parse, carry an assignment (or resolve to an existing
        measurement) and pass validation are saved — already-committed items
        are never re-registered on retry, so rerunning after a partial
        failure is idempotent.

        ``cancel_event`` is a cooperative stop checked before each item — a
        cancelled run keeps its already-persisted commits (each is a complete
        evidence record) and returns the outcomes produced so far.
        ``progress`` is invoked after each processed item with
        ``(done, total)`` for UI progress display.
        """
        current = self.latest_revision()
        outcomes: list[BatchCommitOutcome] = []
        entries = (
            list(self._batch.values())
            if item_ids is None
            else [self._batch[i] for i in item_ids if i in self._batch]
        )
        # One duplicate-name map + revision memo for the whole commit —
        # outcome views must not re-walk the full measurement listing.
        duplicate_names = self._duplicate_names_for(entries)
        revisions: dict[str, SceneRevision | None] = {}
        for item_index, entry in enumerate(entries):
            if cancel_event is not None and cancel_event.is_set():
                break
            if progress is not None:
                progress(item_index, len(entries))
            if entry.committed:
                outcomes.append(
                    BatchCommitOutcome(
                        item=self._batch_item_view(entry, duplicate_names),
                        outcome='already_committed',
                        measurement_id=entry.committed_measurement_id,
                        error=None,
                    )
                )
                continue
            pending = entry.pending
            if pending is None:
                # A staged parse failure stays visible as a failure rather
                # than a silent skip — the item never committed anything.
                outcomes.append(
                    BatchCommitOutcome(
                        item=self._batch_item_view(entry, duplicate_names),
                        outcome='failed',
                        measurement_id=None,
                        error=entry.error or '解析に失敗しています',
                    )
                )
                continue
            # A commit-phase error is transient state on a retryable item —
            # clearing it lets the resumable save path re-attempt (#801).
            entry.error = None
            if (
                current.revision_id != pending.scene_revision_id
                or current.content_hash != pending.scene_content_hash
            ):
                entry.error = (
                    '読み込み後に部屋の保存状態が変更されています。再読み込みしてください'
                )
                outcomes.append(
                    BatchCommitOutcome(
                        item=self._batch_item_view(entry, duplicate_names),
                        outcome='failed',
                        measurement_id=None,
                        error=entry.error,
                    )
                )
                continue
            if (
                entry.duplicate_kind in ('exact_duplicate', 'same_acquisition')
                and entry.resolution == 'reuse_existing'
            ):
                reuse_target = entry.duplicate_of_measurement_id
                if reuse_target is None and entry.duplicate_of_item_id is not None:
                    sibling = self._batch.get(entry.duplicate_of_item_id)
                    if sibling is not None and sibling.committed:
                        reuse_target = sibling.committed_measurement_id
                if reuse_target is None:
                    # The staged sibling may have committed in this run (or
                    # since staging) — persisted evidence is authoritative.
                    _kind, persisted_target = self._classify_duplicate(
                        pending, entry.raw_bytes
                    )
                    if persisted_target is not None:
                        reuse_target = persisted_target
                if (
                    reuse_target is None
                    or self.measurement_repository.get_measurement(reuse_target)
                    is None
                ):
                    # 'reuse_existing' can never silently become an insert:
                    # an unsatisfied reuse target is reported honestly and
                    # the item stays committable on retry.
                    entry.error = '再利用先の測定がまだ保存されていません'
                    outcomes.append(
                        BatchCommitOutcome(
                            item=self._batch_item_view(entry, duplicate_names),
                            outcome='skipped',
                            measurement_id=None,
                            error=entry.error,
                        )
                    )
                    continue
                entry.committed_measurement_id = reuse_target
                try:
                    self._install_staged_attachments(entry)
                except Exception as exc:
                    _LOGGER.warning('attachment install failed: %r', exc)
                    entry.error = operation_error_message(exc)
                    outcomes.append(
                        BatchCommitOutcome(
                            item=self._batch_item_view(entry, duplicate_names),
                            outcome='failed',
                            measurement_id=None,
                            error=entry.error,
                        )
                    )
                    continue
                entry.committed = True
                outcomes.append(
                    BatchCommitOutcome(
                        item=self._batch_item_view(entry, duplicate_names),
                        outcome='reused',
                        measurement_id=entry.committed_measurement_id,
                        error=None,
                    )
                )
                continue
            if entry.assignment is None:
                outcomes.append(
                    BatchCommitOutcome(
                        item=self._batch_item_view(entry, duplicate_names),
                        outcome='skipped',
                        measurement_id=None,
                        error='割り当てが未設定です',
                    )
                )
                continue
            try:
                revision = revisions.get(pending.scene_revision_id)
                if pending.scene_revision_id not in revisions:
                    revision = self.scene_repository.get(
                        pending.scene_revision_id
                    )
                    revisions[pending.scene_revision_id] = revision
                if revision is None or revision.content_hash != pending.scene_content_hash:
                    raise MeasurementWorkflowError(
                        "読み込み時の部屋データを確認できません。再読み込みしてください"
                    )
                if entry.commit_payload is None:
                    entry.commit_payload = self._normalize_for_commit(
                        pending, entry.assignment, revision
                    )
                record, dataset, raw_filename, raw_bytes = entry.commit_payload
                entry.committed_measurement_id = record.measurement_id
                self._save_measurement_for_commit(
                    record, dataset, raw_filename, raw_bytes
                )
                self._save_acquisition_context_for_commit(entry)
                self._install_staged_attachments(entry)
                self._produce_quality_report(record.measurement_id)
            except Exception as exc:
                _LOGGER.warning('batch commit failed for staged item: %r', exc)
                entry.error = operation_error_message(exc)
                outcomes.append(
                    BatchCommitOutcome(
                        item=self._batch_item_view(entry, duplicate_names),
                        outcome='failed',
                        measurement_id=None,
                        error=entry.error,
                    )
                )
                continue
            entry.committed = True
            outcomes.append(
                BatchCommitOutcome(
                    item=self._batch_item_view(entry, duplicate_names),
                    outcome='committed',
                    measurement_id=record.measurement_id,
                    error=None,
                )
            )
            if progress is not None:
                progress(item_index + 1, len(entries))
        return tuple(outcomes)

    def _save_measurement_for_commit(
        self,
        record: CadMeasurementRecord,
        dataset: CadFrequencyResponseDataset,
        raw_filename: str,
        raw_bytes: bytes,
    ) -> None:
        """Persist, or accept the identical already-persisted row on resume (#801).

        The batch entry pins its normalized payload across attempts, so an
        'already exists' hit for the same identity is the second half of an
        interrupted commit — resume, never a duplicate. A mismatch under the
        same id is a genuine conflict and fails closed.
        """
        try:
            self.measurement_repository.save(
                record,
                dataset,
                raw_filename=raw_filename,
                raw_bytes=raw_bytes,
            )
            return
        except ValueError as exc:
            if 'already exists' not in str(exc):
                raise
        existing = self.measurement_repository.get_measurement(record.measurement_id)
        existing_dataset = self.measurement_repository.get_dataset(dataset.dataset_id)
        if existing != record or existing_dataset != dataset:
            raise MeasurementWorkflowError(
                '同一IDの保存済み測定が内容と一致しません: '
                f'{record.measurement_id}'
            )

    def _save_acquisition_context_for_commit(self, entry: _BatchEntry) -> None:
        """Persist the entry's cached acquisition context, resuming on retry (#801)."""
        assignment = entry.assignment
        if assignment is None or assignment.acquisition is None:
            return
        if entry.commit_context is None:
            if entry.commit_payload is None:
                raise MeasurementWorkflowError('コミット対象の測定が確定していません')
            entry.commit_context = self._build_acquisition_context(
                assignment, entry.commit_payload[0]
            )
        context = entry.commit_context
        if context is None:
            return
        try:
            self.quality_repository.save_acquisition_context(context)
        except ValueError as exc:
            if 'already exists' not in str(exc):
                raise
            existing = self.quality_repository.get_acquisition_context(
                context.acquisition_context_id
            )
            if existing != context:
                raise MeasurementWorkflowError(
                    '同一IDの取得コンテキストが内容と一致しません: '
                    f'{context.acquisition_context_id}'
                ) from exc

    def _normalize_for_commit(
        self,
        pending: PendingMeasurementImport,
        assignment: MeasurementAssignment,
        revision: SceneRevision,
    ) -> tuple[CadMeasurementRecord, CadFrequencyResponseDataset, str, bytes]:
        """Normalize a staged pending import through its declared importer.

        Shared by the single pending commit and the batch commit so both
        record the same direction/routing-profile/acquisition/engine
        provenance from the assignment form.
        """
        engine_session_payload = self._engine_session_payload()
        routing_profile_provenance, derived_speakers = (
            self._routing_provenance(assignment)
        )
        acquisition_context_provenance = (
            {'source_kind': assignment.acquisition.source_kind}
            if assignment.acquisition is not None
            else None
        )
        source_speaker_ids = assignment.source_speaker_ids or derived_speakers
        if pending.source_kind == 'rew_text':
            if pending.raw_text is None or pending.raw_filename is None:
                raise MeasurementWorkflowError("REWテキストの一時データがありません")
            return normalize_rew_text(
                revision,
                assignment.measurement_entity_id,
                pending.raw_text,
                filename=pending.raw_filename,
                measurement_direction=assignment.measurement_direction,
                evidence_type=assignment.evidence_type,
                channel_role=assignment.channel_role,
                source_speaker_ids=source_speaker_ids,
                radiation_scope=assignment.radiation_scope,
                routing_evidence=assignment.routing_evidence,
                routing_profile=routing_profile_provenance,
                acquisition_context=acquisition_context_provenance,
                engine_session=engine_session_payload,
            )
        if pending.source_kind == 'rew_api':
            if pending.rew_snapshot is None:
                raise MeasurementWorkflowError("REW APIの一時データがありません")
            return normalize_rew_api_snapshot(
                revision,
                assignment.measurement_entity_id,
                pending.rew_snapshot,
                measurement_direction=assignment.measurement_direction,
                evidence_type=assignment.evidence_type,
                channel_role=assignment.channel_role,
                source_speaker_ids=source_speaker_ids,
                radiation_scope=assignment.radiation_scope,
                routing_evidence=assignment.routing_evidence,
                routing_profile=routing_profile_provenance,
                acquisition_context=acquisition_context_provenance,
                engine_session=engine_session_payload,
            )
        raise MeasurementWorkflowError(f"未対応の測定ソースです: {pending.source_kind}")

    def _save_acquisition_context(
        self,
        assignment: MeasurementAssignment,
        record: CadMeasurementRecord,
    ) -> None:
        """Persist the declared acquisition context bound to the saved measurement."""
        context = self._build_acquisition_context(assignment, record)
        if context is not None:
            self.quality_repository.save_acquisition_context(context)

    def _build_acquisition_context(
        self,
        assignment: MeasurementAssignment,
        record: CadMeasurementRecord,
    ) -> CadAcquisitionContext | None:
        if assignment.acquisition is None:
            return None
        # The persisted acquisition context is the authority; its subject
        # list names the measurement just saved (#471).
        return build_acquisition_context(
            source_kind=assignment.acquisition.source_kind,
            subject_measurement_ids=(record.measurement_id,),
            timing_reference_valid=assignment.acquisition.timing_reference_valid,
            timing_reference_id=assignment.acquisition.timing_reference_id,
            clock_source=assignment.acquisition.clock_source,
            sample_rate_hz=assignment.acquisition.sample_rate_hz,
            delay_correction_s=assignment.acquisition.delay_correction_s,
            microphone=assignment.acquisition.microphone,
            playback=assignment.acquisition.playback,
            measurement_direction=assignment.measurement_direction,
            timing_reference_sha256=assignment.acquisition.timing_reference_sha256,
            environment_ref=assignment.acquisition.environment_ref,
            acquisition_session_id=assignment.acquisition.acquisition_session_id,
            signal_path_identity=assignment.acquisition.signal_path_identity,
            input_path_identity=assignment.acquisition.input_path_identity,
            routing_profile=self._routing_context_binding(assignment),
            notes=assignment.acquisition.notes,
        )

    def _routing_context_binding(
        self, assignment: MeasurementAssignment
    ):
        """Exact routing-profile binding persisted on the context (#858)."""
        if assignment.routing_profile_id is None:
            return None
        profile = self.quality_repository.get_routing_profile(
            assignment.routing_profile_id
        )
        if profile is None:
            raise MeasurementWorkflowError(
                f"保存済みルーティングプロファイルを確認できません: "
                f"{assignment.routing_profile_id}"
            )
        return routing_profile_binding(profile)

    def discard_batch_committed(self) -> None:
        """Drop committed entries from the queue (keeps unfinished items)."""
        for item_id in [
            item_id
            for item_id, entry in self._batch.items()
            if entry.committed
        ]:
            del self._batch[item_id]

    def _unstage_batch_entries(self, entries: Iterable[_BatchEntry]) -> None:
        """Roll back entries a failed stage call already appended.

        Only the entries still owned by this queue are removed — the
        identity check never lets a rollback touch a replaced entry, and
        a committed entry is kept since it now names persisted evidence.
        """
        for entry in entries:
            if not entry.committed and self._batch.get(entry.item_id) is entry:
                del self._batch[entry.item_id]

    def uncommitted_batch_item_ids(self) -> frozenset[str]:
        """Item ids of staged entries that have not persisted anything.

        The batch queue is in-memory: an uncommitted row is unsaved work
        the same way ``pending_import`` is, so the workspace's dirty-state
        contract tracks them for deactivation/discard decisions.
        """
        return frozenset(
            item_id
            for item_id, entry in self._batch.items()
            if not entry.committed
        )

    def discard_batch_items(self, item_ids: Iterable[str]) -> int:
        """Drop uncommitted batch entries the operator chose to discard.

        Committed entries are never touched — they name persisted
        evidence and leave via ``discard_batch_committed`` only.
        Returns the number of entries removed.
        """
        removed = 0
        for item_id in tuple(item_ids):
            entry = self._batch.get(item_id)
            if entry is not None and not entry.committed:
                del self._batch[item_id]
                removed += 1
        return removed

    # ------------------------------------------------------------------
    # Source attachments (#446)
    # ------------------------------------------------------------------

    def save_source_attachment(
        self,
        measurement_id: str,
        *,
        filename: str,
        raw_bytes: bytes,
        kind: str,
        note: str | None = None,
    ) -> CadMeasurementAttachment:
        """Persist a raw source artifact (.mdat/calibration/notes) under the
        same content-addressed authority that project backups archive."""
        return self.measurement_repository.save_attachment(
            measurement_id=measurement_id,
            kind=kind,
            filename=filename,
            raw_bytes=raw_bytes,
            note=note,
        )

    def list_source_attachments(
        self,
        measurement_id: str,
    ) -> tuple[CadMeasurementAttachment, ...]:
        return self.measurement_repository.list_attachments(measurement_id)

    def read_source_attachment(self, attachment: CadMeasurementAttachment) -> bytes:
        return self.measurement_repository.read_attachment(attachment)

    # ------------------------------------------------------------------
    # Disposition and assignment correction (#509)
    # ------------------------------------------------------------------

    def dispositions_for(
        self,
        measurement_id: str,
    ) -> tuple[CadMeasurementDisposition, ...]:
        return self.quality_repository.list_dispositions(measurement_id)

    def set_disposition(
        self,
        measurement_id: str,
        disposition: MeasurementDispositionState,
        reason: str,
    ) -> CadMeasurementDisposition:
        """Append one lifecycle disposition; the evidence itself is untouched."""
        if not reason.strip():
            raise MeasurementWorkflowError("状態変更の理由を記録してください")
        event = build_measurement_disposition(
            document_id=self.document_id,
            measurement_id=measurement_id,
            disposition=disposition,
            reason=reason.strip(),
        )
        self.quality_repository.save_disposition(event)
        return event

    def corrections_for(
        self,
        measurement_id: str,
    ) -> tuple[CadMeasurementCorrection, ...]:
        return self.quality_repository.list_corrections(measurement_id)

    def correct_assignment(
        self,
        measurement_id: str,
        corrected: AssignmentCorrection,
        reason: str,
    ) -> CadMeasurementCorrection:
        """Append a corrected evidence binding and mark the record corrected.

        The original record and raw dataset are never rewritten and no new
        physical acquisition is claimed — the correction pins the exact bound
        dataset and the 'corrected' disposition keeps it auditable.
        """
        if not reason.strip():
            raise MeasurementWorkflowError("訂正の理由を記録してください")
        record = self.measurement_repository.get_measurement(measurement_id)
        if record is None:
            raise MeasurementWorkflowError("測定が見つかりません")
        dataset = self.measurement_repository.dataset_for_measurement(measurement_id)
        if dataset is None:
            raise MeasurementWorkflowError("訂正対象の測定にデータセットがありません")
        if corrected.measurement_entity_id is not None:
            revision = self.scene_repository.get(record.scene_revision_id)
            if revision is None:
                raise MeasurementWorkflowError(
                    "測定時の部屋データを確認できません"
                )
            try:
                entity = revision.document.entity(corrected.measurement_entity_id)
            except KeyError as exc:
                raise MeasurementWorkflowError(
                    "訂正先のポイントが測定時の部屋に存在しません"
                ) from exc
            if acoustic_reference_position(entity) is None:
                raise MeasurementWorkflowError(
                    "訂正先のポイントに音響基準位置がありません"
                )
        correction = build_measurement_correction(
            document_id=self.document_id,
            measurement_id=measurement_id,
            dataset_id=dataset.dataset_id,
            dataset_sha256=dataset_sha256(dataset),
            reason=reason.strip(),
            measurement_entity_id=corrected.measurement_entity_id,
            channel_role=corrected.channel_role,
            source_speaker_ids=corrected.source_speaker_ids,
            radiation_scope=corrected.radiation_scope,
            routing_evidence=corrected.routing_evidence,
            correction_kind=corrected.correction_kind,
            pose_evidence_ref=corrected.pose_evidence_ref,
        )
        self.quality_repository.save_correction(correction)
        event = build_measurement_disposition(
            document_id=self.document_id,
            measurement_id=measurement_id,
            disposition='corrected',
            reason=reason.strip(),
            correction_id=correction.correction_id,
        )
        self.quality_repository.save_disposition(event)
        return correction

    # ------------------------------------------------------------------
    # Spatial context (#487)
    # ------------------------------------------------------------------

    def spatial_context(self, measurement_id: str) -> MeasurementSpatialContext:
        """Bound-SceneRevision spatial context plus a current-scene diff.

        The bound revision is resolved by exact id — never by document head —
        so the measurement's own room state renders even after the workspace
        moved on. The diff never mutates either authority.
        """
        record = self.measurement_repository.get_measurement(measurement_id)
        if record is None:
            raise MeasurementWorkflowError("測定が見つかりません")
        bound = self.scene_repository.get(record.scene_revision_id)
        current = self.scene_repository.current_head(record.document_id)
        correction = self.quality_repository.latest_correction(measurement_id)
        effective_entity = (
            record.measurement_entity_id
            if correction is None or correction.measurement_entity_id is None
            else correction.measurement_entity_id
        )
        effective_speakers = (
            record.source_speaker_ids
            if correction is None or correction.source_speaker_ids is None
            else correction.source_speaker_ids
        )
        changes: tuple[SpatialEntityChange, ...] = ()
        room_changed = False
        if bound is not None and current is not None:
            room_changed = bound.content_hash != current.content_hash
            if bound.revision_id != current.revision_id:
                changes = self._scene_entity_changes(
                    bound.document.entities,
                    current.document.entities,
                )
        return MeasurementSpatialContext(
            record=record,
            bound_revision=bound,
            current_revision=current,
            bound_is_current=(
                bound is not None
                and current is not None
                and bound.revision_id == current.revision_id
            ),
            measurement_position=record.measurement_position,
            measurement_direction=record.measurement_direction,
            effective_entity_id=effective_entity,
            effective_source_speaker_ids=effective_speakers,
            changes=changes,
            room_changed=room_changed,
        )

    @staticmethod
    def _scene_entity_changes(bound_entities, current_entities) -> tuple[SpatialEntityChange, ...]:
        current_by_id = {entity.entity_id: entity for entity in current_entities}
        bound_by_id = {entity.entity_id: entity for entity in bound_entities}
        changes: list[SpatialEntityChange] = []
        for entity in bound_entities:
            current = current_by_id.get(entity.entity_id)
            if current is None:
                changes.append(
                    SpatialEntityChange(
                        kind='removed',
                        entity_id=entity.entity_id,
                        name=entity.name,
                        distance_m=None,
                    )
                )
                continue
            position = entity.position
            moved = (
                position.x_m != current.position.x_m
                or position.y_m != current.position.y_m
                or position.z_m != current.position.z_m
            )
            same_semantics = (
                entity.name == current.name
                and entity.kind == current.kind
                and entity.size_m == current.size_m
                and entity.speaker_role == current.speaker_role
                and entity.acoustic_reference_offset_m
                == current.acoustic_reference_offset_m
            )
            if moved:
                dx = position.x_m - current.position.x_m
                dy = position.y_m - current.position.y_m
                dz = position.z_m - current.position.z_m
                changes.append(
                    SpatialEntityChange(
                        kind='moved',
                        entity_id=entity.entity_id,
                        name=entity.name,
                        distance_m=(dx * dx + dy * dy + dz * dz) ** 0.5,
                    )
                )
            elif not same_semantics:
                changes.append(
                    SpatialEntityChange(
                        kind='changed',
                        entity_id=entity.entity_id,
                        name=entity.name,
                        distance_m=None,
                    )
                )
        for entity in current_entities:
            if entity.entity_id not in bound_by_id:
                changes.append(
                    SpatialEntityChange(
                        kind='added',
                        entity_id=entity.entity_id,
                        name=entity.name,
                        distance_m=None,
                    )
                )
        return tuple(changes)

    # ------------------------------------------------------------------
    # Target curve overlays (#503)
    # ------------------------------------------------------------------

    def target_curves(self) -> tuple[Any, ...]:
        """CadTargetCurve objects declared by calibration plans of this document."""
        if self._calibration_repository is None:
            from .cad_calibration_repository import CadCalibrationRepository
            from .cad_system_variant_repository import CadSystemVariantRepository

            self._calibration_repository = CadCalibrationRepository(
                scene_repository=self.scene_repository,
                system_variant_repository=CadSystemVariantRepository(
                    self.scene_repository
                ),
                measurement_repository=self.measurement_repository,
                quality_repository=self.quality_repository,
            )
        plans = self._calibration_repository.list_plans(self.document_id)
        return tuple(
            (plan.plan_id, plan.target_curve)
            for plan in plans
            if plan.target_curve is not None
        )


__all__ = [
    "AssignmentCorrection",
    "AssignmentTarget",
    "BatchCommitOutcome",
    "BatchDuplicateKind",
    "BatchImportItem",
    "BatchItemStatus",
    "BatchResolution",
    "MeasurementAssignment",
    "MeasurementCheckView",
    "MeasurementSpatialContext",
    "MeasurementView",
    "MeasurementWorkflowController",
    "MeasurementWorkflowError",
    "PendingMeasurementImport",
    "RewReadSource",
    "SpeakerTarget",
    "SpatialEntityChange",
]
