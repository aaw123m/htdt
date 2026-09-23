from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, get_args

from .cad_measurement_models import (
    CadFrequencyResponseDataset,
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
    gate_measurement_claim,
    measurement_retake_guidance,
    phase_response_capability,
    unestablished_capability_claims,
    unestablished_common_timing_capability,
)
from .cad_measurement_quality_repository import CadMeasurementQualityRepository
from .cad_measurement_repository import CadMeasurementRepository
from .cad_measurement_runner import (
    GuidedStep,
    MeasurementRunnerPlan,
    MeasurementRunnerRun,
    RunnerCellState,
    RunnerPurpose,
    build_runner_plan,
    guided_step,
    runner_progress,
)
from .cad_measurement_runner_repository import CadMeasurementRunnerRepository
from .cad_measurements import normalize_rew_api_snapshot, normalize_rew_text
from .cad_repository import SceneRepository, SceneRevision
from .cad_scene import (
    Direction3,
    acoustic_reference_position,
    is_unassigned_speaker_role,
)
from .comparison import FrequencyResponse, compare_frequency_responses
from .rew_api import RewFrequencyResponseSnapshot
from .rew_parser import parse_rew_frequency_response


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

    @property
    def sample_count(self) -> int:
        return len(self.frequency_hz)

    @property
    def frequency_band_hz(self) -> tuple[float, float]:
        return (self.frequency_hz[0], self.frequency_hz[-1])


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
    # report exists for this measurement.
    quality_report_state: Literal['current', 'stale', 'missing']
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
    ) -> None:
        self.scene_repository = scene_repository
        self.document_id = document_id
        self.measurement_repository = (
            measurement_repository
            if measurement_repository is not None
            else CadMeasurementRepository(scene_repository)
        )
        self.quality_repository = (
            quality_repository
            if quality_repository is not None
            else CadMeasurementQualityRepository(self.measurement_repository)
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
        self._pending = pending
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
        self._pending = pending
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
        pending = PendingMeasurementImport(
            source_kind=pending.source_kind,
            source_label=pending.source_label,
            scene_revision_id=revision.revision_id,
            scene_content_hash=revision.content_hash,
            frequency_hz=pending.frequency_hz,
            level_db=pending.level_db,
            has_phase_samples=pending.has_phase_samples,
            scene_revision_explicit=True,
            raw_text=pending.raw_text,
            raw_filename=pending.raw_filename,
            rew_snapshot=pending.rew_snapshot,
        )
        self._pending = pending
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

    def list_rew_measurements(self) -> list[dict[str, Any]]:
        return self.rew_client.list_measurements()

    def fetch_rew_snapshot(self, measurement_uuid: str) -> RewFrequencyResponseSnapshot:
        return self.rew_client.get_frequency_response_snapshot(
            measurement_uuid,
            unit="SPL",
            ppo=None,
            smoothing=None,
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
        revision = self._assignment_revision()
        return tuple(
            AssignmentTarget(entity.entity_id, entity.name, entity.kind)
            for entity in revision.document.entities
            if acoustic_reference_position(entity) is not None
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

        if pending.source_kind == "rew_text":
            if pending.raw_text is None or pending.raw_filename is None:
                raise MeasurementWorkflowError("REWテキストの一時データがありません")
            record, dataset, raw_filename, raw_bytes = normalize_rew_text(
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
        elif pending.source_kind == "rew_api":
            if pending.rew_snapshot is None:
                raise MeasurementWorkflowError("REW APIの一時データがありません")
            record, dataset, raw_filename, raw_bytes = normalize_rew_api_snapshot(
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
        else:
            raise MeasurementWorkflowError(f"未対応の測定ソースです: {pending.source_kind}")

        self.measurement_repository.save(
            record,
            dataset,
            raw_filename=raw_filename,
            raw_bytes=raw_bytes,
        )
        if assignment.acquisition is not None:
            # The persisted acquisition context is the authority; its subject
            # list names the measurement just saved (#471).
            context = build_acquisition_context(
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
                notes=assignment.acquisition.notes,
            )
            self.quality_repository.save_acquisition_context(context)
        self._pending = None
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
        """
        if assignment.routing_profile_id is None:
            return None, ()
        profile = self.quality_repository.get_routing_profile(
            assignment.routing_profile_id
        )
        if profile is None:
            raise MeasurementWorkflowError(
                f"保存済みルーティングプロファイルを確認できません: "
                f"{assignment.routing_profile_id}"
            )
        entry = profile.entry_for_role(assignment.channel_role)
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
        self.measurement_repository.save_ir_dataset(
            dataset,
            raw_filename=raw_filename,
            raw_bytes=raw_bytes,
        )
        return dataset

    def ir_datasets_for_measurement(
        self, measurement_id: str
    ) -> tuple[CadImpulseResponseDataset, ...]:
        return self.measurement_repository.ir_datasets_for_measurement(
            measurement_id
        )

    def derive_measurement_point_from_seat(
        self,
        source_seat_id: str,
        *,
        measurement_point_id: str,
        name: str | None = None,
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
        try:
            new_document = derive_measurement_point_document(
                revision.document,
                source_seat_id=source_seat_id,
                measurement_point_id=measurement_point_id,
                name=name,
            )
        except MeasurementTargetError as exc:
            raise MeasurementWorkflowError(str(exc)) from exc
        try:
            point = new_document.entity(measurement_point_id)
        except KeyError as exc:
            raise MeasurementWorkflowError(str(exc)) from exc
        position = acoustic_reference_position(point)
        assert position is not None
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
        rows: list[MeasurementView] = []
        for record in self.measurement_repository.list_measurements(self.document_id):
            dataset = self.measurement_repository.dataset_for_measurement(record.measurement_id)
            source_revision = self.scene_repository.get(record.scene_revision_id)
            target_name = record.measurement_entity_id
            if source_revision is not None:
                try:
                    target_name = source_revision.document.entity(
                        record.measurement_entity_id
                    ).name
                except KeyError:
                    pass
            band = None
            sample_count = 0
            phase_status: MeasurementPhaseStatus | None = None
            phase_capability: CadMeasurementCapability | None = None
            timing_capability: CadMeasurementCapability | None = None
            capabilities: tuple[CadMeasurementCapability, ...] = ()
            checks: tuple[MeasurementCheckView, ...] = ()
            report_state: Literal['current', 'stale', 'missing'] = 'missing'
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
                report = self.quality_repository.latest_report(record.measurement_id)
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
                )
            )
        return tuple(rows)

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
        evidence_type: MeasurementEvidenceType,
    ) -> tuple[MeasurementView, ...]:
        return tuple(
            row
            for row in self.measurement_views()
            if row.evidence_type == evidence_type and row.dataset_id is not None
        )

    def compare_datasets(
        self,
        dataset_a_id: str,
        dataset_b_id: str,
        *,
        low_hz: float,
        high_hz: float,
    ) -> CadMeasurementComparison:
        allowed_dataset_ids = {
            row.dataset_id
            for row in self.measurement_views()
            if row.dataset_id is not None
        }
        if dataset_a_id not in allowed_dataset_ids or dataset_b_id not in allowed_dataset_ids:
            raise MeasurementWorkflowError(
                "比較対象は現在のプロジェクトに保存された測定から選択してください"
            )
        dataset_a = self.dataset(dataset_a_id)
        dataset_b = self.dataset(dataset_b_id)
        result = compare_frequency_responses(
            FrequencyResponse(dataset_a.frequency_hz, dataset_a.level_db),
            FrequencyResponse(dataset_b.frequency_hz, dataset_b.level_db),
            low_hz,
            high_hz,
        )
        return self.measurement_repository.save_comparison(
            dataset_a_id,
            dataset_b_id,
            result,
        )

    def saved_comparisons(self) -> tuple[CadMeasurementComparison, ...]:
        return self.measurement_repository.list_comparisons(self.document_id)

    # ------------------------------------------------------------------
    # Campaign runner (#529): guided source × target × repeat execution over
    # the existing measurement/import authorities. REW stays the acquisition
    # engine; the runner only tracks exact per-cell planned evidence.

    def runner_plans(self) -> tuple[MeasurementRunnerPlan, ...]:
        return self.runner_repository.list_plans()

    def create_runner_plan(
        self,
        *,
        repeat_count: int = 1,
        purposes: tuple[RunnerPurpose, ...] = ('measurement',),
    ) -> MeasurementRunnerPlan:
        """Register a new plan from the current scene's speakers and targets."""
        revision = self.latest_revision()
        targets = self.assignment_targets()
        speakers = self.source_speakers()
        if not targets or not speakers:
            raise MeasurementWorkflowError(
                "キャンペーンには少なくとも1つの測定位置と1つの音源が必要です"
            )
        # Speaker display roles ('FL', 'C', ...) map onto the measurement
        # channel-role tokens used everywhere else in this workflow.
        role_tokens = {
            'FL': 'front_left',
            'FR': 'front_right',
            'C': 'center',
            'LFE': 'subwoofer',
            'SUB': 'subwoofer',
        }
        plan = build_runner_plan(
            scene_revision_id=revision.revision_id,
            scene_content_hash=revision.content_hash,
            sources=tuple(
                (role_tokens.get(speaker.role, speaker.role), (speaker.entity_id,))
                for speaker in speakers
            ),
            target_entity_ids=tuple(target.entity_id for target in targets),
            repeat_count=repeat_count,
            purposes=purposes,
        )
        self.runner_repository.save_plan(plan)
        return plan

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
        plan = self.runner_repository.get_plan(run.plan_id)
        return runner_progress(plan, self.runner_repository.cell_states(run_id))

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

        The quality decision is read from the measurement's replay-validated
        report state — RETAKE keeps the cell retake_required, a current
        not-needed report completes it, everything else stays quality_pending.
        """
        record = self.measurement_repository.get_measurement(measurement_id)
        if record is None:
            raise MeasurementWorkflowError("登録する測定が保存されていません")
        dataset = self.measurement_repository.dataset_for_measurement(measurement_id)
        if dataset is None:
            raise MeasurementWorkflowError("測定に周波数応答データがありません")
        view = next(
            (
                row
                for row in self.measurement_views()
                if row.measurement_id == measurement_id
            ),
            None,
        )
        decision: Literal['passed', 'blocked', 'pending'] = 'pending'
        if view is not None and view.quality_report_state == 'current':
            if view.retake_recommendation == 'RETAKE':
                decision = 'blocked'
            elif view.retake_recommendation == 'NOT_NEEDED':
                decision = 'passed'
        self.runner_repository.commit_cell(
            run_id,
            cell_index,
            measurement_id=measurement_id,
            dataset_id=dataset.dataset_id,
            dataset_sha256=dataset.dataset_sha256,
            quality_decision=decision,
        )

    def runner_skip_cell(self, run_id: str, cell_index: int) -> None:
        self.runner_repository.skip_cell(run_id, cell_index)


__all__ = [
    "AssignmentTarget",
    "MeasurementAssignment",
    "MeasurementCheckView",
    "MeasurementView",
    "MeasurementWorkflowController",
    "MeasurementWorkflowError",
    "PendingMeasurementImport",
    "RewReadSource",
    "SpeakerTarget",
]
