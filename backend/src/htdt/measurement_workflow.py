from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .cad_measurement_models import (
    CadFrequencyResponseDataset,
    CadMeasurementComparison,
    CadMeasurementRecord,
    MeasurementEvidenceType,
    MeasurementPhaseStatus,
    RadiationScope,
    RoutingEvidence,
)
from .cad_measurement_repository import CadMeasurementRepository
from .cad_measurements import normalize_rew_api_snapshot, normalize_rew_text
from .cad_repository import SceneRepository, SceneRevision
from .cad_scene import acoustic_reference_position, is_unassigned_speaker_role
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
    """Transient import preview. It is intentionally not measurement evidence."""

    source_kind: str
    source_label: str
    scene_revision_id: str
    scene_content_hash: str
    frequency_hz: tuple[float, ...]
    level_db: tuple[float, ...]
    has_phase_samples: bool
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
class MeasurementAssignment:
    measurement_entity_id: str
    evidence_type: MeasurementEvidenceType = "measured"
    channel_role: str = "unknown"
    source_speaker_ids: tuple[str, ...] = ()
    radiation_scope: RadiationScope = "unknown"
    routing_evidence: RoutingEvidence = "unknown"


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
    phase_timing_available: bool
    sample_count: int
    frequency_band_hz: tuple[float, float] | None
    captured_at: str | None
    imported_at: str
    scene_revision_id: str
    scene_matches_current: bool


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
        rew_client: RewReadSource | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.document_id = document_id
        self.measurement_repository = (
            measurement_repository
            if measurement_repository is not None
            else CadMeasurementRepository(scene_repository)
        )
        if rew_client is None:
            from .rew_api import RewApiClient

            rew_client = RewApiClient()
        self.rew_client = rew_client
        self._pending: PendingMeasurementImport | None = None

    @property
    def pending_import(self) -> PendingMeasurementImport | None:
        return self._pending

    def latest_revision(self) -> SceneRevision:
        revision = self.scene_repository.latest(self.document_id)
        if revision is None:
            raise MeasurementWorkflowError(
                "測定を読み込む前に、部屋を一度保存してください"
            )
        return revision

    def stage_rew_text(self, raw: bytes, filename: str) -> PendingMeasurementImport:
        revision = self.latest_revision()
        parsed = parse_rew_frequency_response(raw)
        pending = PendingMeasurementImport(
            source_kind="rew_text",
            source_label=filename,
            scene_revision_id=revision.revision_id,
            scene_content_hash=revision.content_hash,
            frequency_hz=parsed.frequency_hz,
            level_db=parsed.level_db,
            has_phase_samples=parsed.phase_deg is not None,
            raw_text=raw,
            raw_filename=filename,
        )
        self._pending = pending
        return pending

    def stage_rew_snapshot(
        self,
        snapshot: RewFrequencyResponseSnapshot,
    ) -> PendingMeasurementImport:
        revision = self.latest_revision()
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
            rew_snapshot=snapshot,
        )
        self._pending = pending
        return pending

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

    def commit_pending(self, assignment: MeasurementAssignment) -> CadMeasurementRecord:
        pending = self._pending
        if pending is None:
            raise MeasurementWorkflowError("先にREWデータを読み込んでください")

        current = self.latest_revision()
        if (
            current.revision_id != pending.scene_revision_id
            or current.content_hash != pending.scene_content_hash
        ):
            raise MeasurementWorkflowError(
                "読み込み後に部屋の保存状態が変更されています。REWを読み込み直して割り当てを確認してください"
            )
        revision = self._assignment_revision()

        if pending.source_kind == "rew_text":
            if pending.raw_text is None or pending.raw_filename is None:
                raise MeasurementWorkflowError("REWテキストの一時データがありません")
            record, dataset, raw_filename, raw_bytes = normalize_rew_text(
                revision,
                assignment.measurement_entity_id,
                pending.raw_text,
                filename=pending.raw_filename,
                evidence_type=assignment.evidence_type,
                channel_role=assignment.channel_role,
                source_speaker_ids=assignment.source_speaker_ids,
                radiation_scope=assignment.radiation_scope,
                routing_evidence=assignment.routing_evidence,
            )
        elif pending.source_kind == "rew_api":
            if pending.rew_snapshot is None:
                raise MeasurementWorkflowError("REW APIの一時データがありません")
            record, dataset, raw_filename, raw_bytes = normalize_rew_api_snapshot(
                revision,
                assignment.measurement_entity_id,
                pending.rew_snapshot,
                evidence_type=assignment.evidence_type,
                channel_role=assignment.channel_role,
                source_speaker_ids=assignment.source_speaker_ids,
                radiation_scope=assignment.radiation_scope,
                routing_evidence=assignment.routing_evidence,
            )
        else:
            raise MeasurementWorkflowError(f"未対応の測定ソースです: {pending.source_kind}")

        self.measurement_repository.save(
            record,
            dataset,
            raw_filename=raw_filename,
            raw_bytes=raw_bytes,
        )
        self._pending = None
        return record

    def measurement_views(self) -> tuple[MeasurementView, ...]:
        latest = self.scene_repository.latest(self.document_id)
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
            phase_timing_available = False
            dataset_id = None
            if dataset is not None:
                dataset_id = dataset.dataset_id
                sample_count = len(dataset.frequency_hz)
                band = (dataset.frequency_hz[0], dataset.frequency_hz[-1])
                phase_status = dataset.phase_status
                # Existing Overview/capability contract: only explicit "valid"
                # phase evidence enables timing/phase workflows.
                phase_timing_available = dataset.phase_status == "valid"

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
                    phase_timing_available=phase_timing_available,
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


__all__ = [
    "AssignmentTarget",
    "MeasurementAssignment",
    "MeasurementView",
    "MeasurementWorkflowController",
    "MeasurementWorkflowError",
    "PendingMeasurementImport",
    "RewReadSource",
    "SpeakerTarget",
]
