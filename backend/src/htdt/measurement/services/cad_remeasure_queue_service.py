"""Re-measurement queue service (#968).

Orchestrates the sealed queue lifecycle on top of the measurement,
quality and runner authorities:

* ``generate`` — scan the document's sealed quality evaluations, classify
  every committed measurement (queued / soft re-evaluation / skipped /
  passed), seal and persist the deterministic queue.
* ``status`` — lapse detection: a queue is only current while the scene
  head and the (measurement → report) evaluation set it pinned are
  unchanged. Any drift blocks conversion until re-generation.
* ``dismiss_item`` — record an operator's reason as a sealed terminal
  event (per-item, never silent).
* ``convert_to_runner_plan`` — materialize pending items into an
  immutable ``MeasurementRunnerPlan`` (one cell per item, the item's
  exact binding) so the existing campaign runner executes the queue.
  Idempotent: the same items on the same scene reuse the same plan.
* ``soft_reevaluate`` — the safe soft re-evaluation path: re-derive the
  measurement's quality epoch from currently persisted authorities
  (file re-read / completed metadata), never a fabricated PASS.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Literal

_LOGGER = logging.getLogger(__name__)

from ...cad_repository import SceneRepository
from ...cad_scene import is_measurement_target_eligible
from ...canonical_json import canonical_sha256 as _hash
from ...clock import utc_now_iso as _utc_now
from ..domain.cad_measurement_runner import (
    MeasurementRunnerPlan,
    RunnerCellSpec,
    build_runner_plan_from_cells,
)
from ..domain.cad_remeasure_queue import (
    CAPTURE_REQUIRED_EVIDENCE,
    RemeasureEvaluation,
    RemeasureItemState,
    RemeasureQueue,
    RemeasureQueueEvent,
    RemeasureQueueStatus,
    RemeasureRequiredInputs,
    build_queue_event,
    build_remeasure_queue,
    evaluation_set_sha256,
)
from ..persistence.cad_measurement_quality_repository import (
    CadMeasurementQualityRepository,
)
from ..persistence.cad_measurement_repository import CadMeasurementRepository
from ..persistence.cad_measurement_runner_repository import (
    CadMeasurementRunnerRepository,
    RunnerError,
)
from ..persistence.cad_remeasure_queue_repository import (
    CadRemeasureQueueRepository,
    RemeasureQueueError,
)
from ..persistence.cad_measurement_effective import CadEffectiveMeasurementResolver
from .cad_measurement_quality_producer import (
    CadMeasurementQualityProducer,
    QualityProductionResult,
    _REPEAT_BINDING_FIELDS,
)


class RemeasureQueueServiceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RemeasureQueueSnapshot:
    """One read of the queue surface: queue, freshness, per-item states."""

    queue: RemeasureQueue
    status: RemeasureQueueStatus
    item_states: dict[str, RemeasureItemState]
    events: tuple[RemeasureQueueEvent, ...]
    created_at_utc: str


class CadRemeasureQueueService:
    """Generate, persist and execute sealed re-measurement queues."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        measurement_repository: CadMeasurementRepository,
        quality_repository: CadMeasurementQualityRepository,
        *,
        queue_repository: CadRemeasureQueueRepository | None = None,
        runner_repository: CadMeasurementRunnerRepository | None = None,
        producer: CadMeasurementQualityProducer | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.measurement_repository = measurement_repository
        self.quality_repository = quality_repository
        self.queue_repository = (
            queue_repository
            if queue_repository is not None
            else CadRemeasureQueueRepository(scene_repository)
        )
        self.runner_repository = (
            runner_repository
            if runner_repository is not None
            else CadMeasurementRunnerRepository(
                scene_repository,
                measurement_repository,
            )
        )
        self._producer = producer
        self._effective = CadEffectiveMeasurementResolver(
            measurement_repository, quality_repository
        )

    def _quality_producer(self) -> CadMeasurementQualityProducer | None:
        if self._producer is not None:
            return self._producer
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

    # ------------------------------------------------------------------
    # Evaluation set
    # ------------------------------------------------------------------

    def _latest_reports(
        self, document_id: str, records
    ):
        """Batched quality read with a per-measurement fallback.

        One poisoned payload fails the document-scoped pass entirely; the
        fallback keeps the healthy rows evaluable and records the broken
        one in the errors map, verbatim.
        """
        try:
            return self.quality_repository.latest_reports(document_id)
        except Exception as batch_error:
            _LOGGER.warning(
                'batched quality read failed for %s (%r); '
                're-reading per measurement',
                document_id,
                batch_error,
            )
            reports = {}
            report_errors = {}
            for record in records:
                try:
                    report = self.quality_repository.latest_report(
                        record.measurement_id
                    )
                except Exception as exc:
                    report_errors[record.measurement_id] = exc
                else:
                    if report is not None:
                        reports[record.measurement_id] = report
            return reports, report_errors

    def _evaluation_rows(
        self, document_id: str
    ) -> tuple[tuple[str, str, str, str], ...]:
        """(measurement_id, report_id, report_sha256, error) per row.

        The freshness pin covers every committed measurement — including
        report-less and report-unreadable ones — so any evaluation drift
        lapses the queue. Uses the same batched authoritative overlay the
        builder consumed so the pins compare byte-for-byte.
        """
        records = self.measurement_repository.list_measurements(document_id)
        reports, report_errors = self._latest_reports(document_id, records)
        rows: list[tuple[str, str, str, str]] = []
        for record in records:
            report = reports.get(record.measurement_id)
            error = report_errors.get(record.measurement_id)
            rows.append(
                (
                    record.measurement_id,
                    '' if report is None else report.report_id,
                    '' if report is None else report.report_sha256,
                    '' if error is None else str(error),
                )
            )
        return tuple(sorted(rows))

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    def _required_inputs(
        self,
        effective,
        report,
    ) -> RemeasureRequiredInputs | None:
        """Resolve the re-capture inputs from the sealed context.

        The binding is the *effective* one (correction applied) — the same
        identity a runner commit validates. Equipment mirrors the
        acquisition-context authority the report pinned; an absent context
        leaves the fields honestly empty.
        """
        if not effective.measurement_entity_id:
            return None
        context = None
        binding = report.acquisition_context
        if binding is not None:
            context = self.quality_repository.get_acquisition_context(
                binding.acquisition_context_id
            )
        return RemeasureRequiredInputs(
            measurement_entity_id=effective.measurement_entity_id,
            measurement_position=effective.measurement.measurement_position,
            measurement_direction=effective.measurement.measurement_direction,
            channel_role=effective.channel_role or 'unknown',
            source_speaker_ids=tuple(effective.source_speaker_ids),
            radiation_scope=effective.radiation_scope or 'unknown',
            routing_evidence=effective.routing_evidence or 'unknown',
            acquisition_context_id=(
                None if context is None else context.acquisition_context_id
            ),
            microphone=None if context is None else context.microphone,
            playback=None if context is None else context.playback,
            sample_rate_hz=(
                report.evidence.sample_rate_hz
                if report.evidence.sample_rate_hz is not None
                else (None if context is None else context.sample_rate_hz)
            ),
            calibration_file_sha256=report.evidence.calibration_file_sha256,
        )

    def _has_same_binding_sibling(self, record) -> bool:
        """Whether a committed same-binding repeat already exists."""
        for other in self.measurement_repository.list_measurements(
            record.document_id
        ):
            if other.measurement_id == record.measurement_id:
                continue
            if all(
                getattr(other, field) == getattr(record, field)
                for field in _REPEAT_BINDING_FIELDS
            ):
                return True
        return False

    def generate(self, document_id: str) -> RemeasureQueue:
        """Scan sealed quality evaluations and persist the sealed queue.

        Missing reports are first derived through the honest producer
        backfill — the queue never fabricates quality, so a measurement
        whose report cannot be produced lands in ``skipped`` with a
        stated reason.
        """
        head = self.scene_repository.current_head(document_id)
        if head is None:
            raise RemeasureQueueServiceError(
                'project has no current scene revision'
            )
        producer = self._quality_producer()
        if producer is not None:
            # A measurement whose persisted report cannot be replayed must
            # not poison the whole queue: backfill what is reachable and let
            # the evaluation rows below record the unreadable one verbatim.
            try:
                producer.ensure_reports(document_id)
            except Exception:
                _LOGGER.warning(
                    'quality report backfill partially failed for %s; '
                    'continuing with reachable evaluations',
                    document_id,
                )

        records = self.measurement_repository.list_measurements(document_id)
        reports, report_errors = self._latest_reports(document_id, records)
        eligible_entities = {
            entity.entity_id
            for entity in head.document.entities
            if is_measurement_target_eligible(entity)
        }

        evaluations: list[RemeasureEvaluation] = []
        for record in records:
            report = reports.get(record.measurement_id)
            error = report_errors.get(record.measurement_id)
            try:
                effective = self._effective.resolve(record.measurement_id)
            except Exception as exc:
                # The resolver re-reads the report authority; an unreadable
                # one cannot poison the queue — it lands skipped with the
                # error recorded verbatim.
                evaluations.append(
                    RemeasureEvaluation(
                        measurement_id=record.measurement_id,
                        eligible=False,
                        ineligibility_reasons=(),
                        is_selected_head=False,
                        target_in_current_scene=False,
                        inputs=None,
                        report=None,
                        report_error=str(
                            error if error is not None else exc
                        ),
                        has_same_binding_sibling=(
                            self._has_same_binding_sibling(record)
                        ),
                    )
                )
                continue
            evaluations.append(
                RemeasureEvaluation(
                    measurement_id=record.measurement_id,
                    eligible=effective.is_normally_eligible,
                    ineligibility_reasons=effective.ineligibility_reasons,
                    is_selected_head=effective.is_selected_head,
                    target_in_current_scene=(
                        effective.measurement_entity_id in eligible_entities
                    ),
                    inputs=(
                        None
                        if report is None
                        else self._required_inputs(effective, report)
                    ),
                    report=report,
                    report_error=None if error is None else str(error),
                    has_same_binding_sibling=self._has_same_binding_sibling(
                        record
                    ),
                )
            )

        queue = build_remeasure_queue(
            document_id=document_id,
            scene_revision_id=head.revision_id,
            scene_content_hash=head.content_hash,
            evaluations=tuple(evaluations),
        )
        self.queue_repository.save_queue(queue)
        return queue

    # ------------------------------------------------------------------
    # Freshness + reads
    # ------------------------------------------------------------------

    def status(self, queue: RemeasureQueue) -> RemeasureQueueStatus:
        """Lapse verdict — execution stays blocked until re-generation."""
        head = self.scene_repository.current_head(queue.document_id)
        if (
            head is None
            or head.revision_id != queue.scene_revision_id
            or head.content_hash != queue.scene_content_hash
        ):
            return 'lapsed_scene'
        current = evaluation_set_sha256(
            self._evaluation_rows(queue.document_id)
        )
        if current != queue.evaluation_set_sha256:
            return 'lapsed_evaluations'
        return 'current'

    def latest(self, document_id: str) -> RemeasureQueueSnapshot | None:
        queue = self.queue_repository.latest_queue(document_id)
        if queue is None:
            return None
        return self.snapshot(queue)

    def snapshot(self, queue: RemeasureQueue) -> RemeasureQueueSnapshot:
        return RemeasureQueueSnapshot(
            queue=queue,
            status=self.status(queue),
            item_states=self.queue_repository.item_states(queue),
            events=self.queue_repository.list_events(queue.queue_id),
            created_at_utc=(
                self.queue_repository.queue_created_at_utc(
                    queue.document_id
                ).get(queue.queue_id, '')
            ),
        )

    # ------------------------------------------------------------------
    # Item transitions
    # ------------------------------------------------------------------

    def _require_current_queue(self, queue_id: str) -> RemeasureQueue:
        queue = self.queue_repository.get_queue(queue_id)
        if queue is None:
            raise RemeasureQueueServiceError(
                're-measurement queue is not persisted'
            )
        status = self.status(queue)
        if status != 'current':
            raise RemeasureQueueServiceError(
                're-measurement queue is stale '
                f'({status}) — regenerate before acting on it'
            )
        return queue

    def dismiss_item(
        self,
        queue_id: str,
        measurement_id: str,
        *,
        reason: str,
        created_at_utc: str | None = None,
    ) -> RemeasureQueueEvent:
        """Record a per-item dismissal with the operator's reason."""
        queue = self._require_current_queue(queue_id)
        if queue.item(measurement_id) is None:
            raise RemeasureQueueServiceError(
                'measurement is not in this re-measurement queue'
            )
        if not reason.strip():
            raise RemeasureQueueServiceError(
                'a dismissal reason is required'
            )
        event = build_queue_event(
            queue=queue,
            measurement_id=measurement_id,
            kind='dismissed',
            reason=reason.strip(),
            created_at_utc=created_at_utc or _utc_now(),
        )
        self.queue_repository.append_event(event)
        return event

    # ------------------------------------------------------------------
    # Campaign-runner wiring
    # ------------------------------------------------------------------

    def convert_to_runner_plan(
        self,
        queue_id: str,
        *,
        measurement_ids: tuple[str, ...] | None = None,
        created_at_utc: str | None = None,
    ) -> MeasurementRunnerPlan:
        """Materialize pending queue items into a sealed runner plan.

        One cell per pending item pins the exact effective binding
        (target entity, channel role, source speakers); the cell notes
        keep the source measurement/report lineage. Converting the same
        pending items on the same scene reuses the already-persisted plan
        — a re-run never double-registers cells.
        """
        queue = self._require_current_queue(queue_id)
        states = self.queue_repository.item_states(queue)
        wanted = (
            None if measurement_ids is None else set(measurement_ids)
        )
        pending = tuple(
            item
            for item in queue.items
            if states.get(item.measurement_id) == 'pending'
            and (wanted is None or item.measurement_id in wanted)
        )
        if not pending:
            raise RemeasureQueueServiceError(
                're-measurement queue has no pending items to convert'
            )
        unknown = (wanted or set()) - {i.measurement_id for i in pending}
        if unknown:
            raise RemeasureQueueServiceError(
                'measurements are not pending queue items: '
                + ', '.join(sorted(unknown))
            )

        cells = [
            RunnerCellSpec(
                cell_index=index,
                channel_role=item.inputs.channel_role,
                source_speaker_ids=item.inputs.source_speaker_ids,
                target_entity_id=item.inputs.measurement_entity_id,
                repeat_index=0,
                purpose='measurement',
                notes=(
                    f'remeasure:{item.measurement_id} '
                    f'report:{item.report_id}'
                ),
            )
            for index, item in enumerate(pending)
        ]

        head = self.scene_repository.current_head(queue.document_id)
        plan: MeasurementRunnerPlan | None = None
        for existing in self.runner_repository.list_plans(queue.document_id):
            if (
                existing.scene_revision_id == queue.scene_revision_id
                and existing.scene_content_hash == queue.scene_content_hash
                and list(existing.cells) == cells
            ):
                plan = existing
                break
        if plan is None:
            assert head is not None  # _require_current_queue guarantees it
            plan_id = 'rqplan-' + _hash(
                {
                    'kind': 'remeasure-runner-plan',
                    'scene_revision_id': head.revision_id,
                    'scene_content_hash': head.content_hash,
                    'cells': [c.model_dump(mode='json') for c in cells],
                }
            )[:24]
            try:
                plan = build_runner_plan_from_cells(
                    document_id=queue.document_id,
                    scene_revision_id=head.revision_id,
                    scene_content_hash=head.content_hash,
                    cells=cells,
                    plan_id=plan_id,
                )
                self.runner_repository.save_plan(plan)
            except RunnerError:
                # A concurrent conversion persisted the identical plan.
                plan = self.runner_repository.get_plan(plan_id)
                if plan is None:
                    raise

        for item in pending:
            event = build_queue_event(
                queue=queue,
                measurement_id=item.measurement_id,
                kind='converted',
                runner_plan_id=plan.plan_id,
                created_at_utc=created_at_utc or _utc_now(),
            )
            self.queue_repository.append_event(event)
        return plan

    # ------------------------------------------------------------------
    # Soft re-evaluation
    # ------------------------------------------------------------------

    def soft_reevaluate(
        self, measurement_id: str
    ) -> QualityProductionResult:
        """Re-derive the quality epoch from currently stored authorities.

        This is the honest recovery path for soft candidates: completed
        metadata and re-read files produce a new sealed report epoch —
        missing evidence is never asserted, so an unresolved measurement
        keeps its honest state.
        """
        producer = self._quality_producer()
        if producer is None:
            raise RemeasureQueueServiceError(
                'quality report production is unavailable'
            )
        return producer.produce_report(measurement_id)


__all__ = [
    'CadRemeasureQueueService',
    'RemeasureQueueServiceError',
    'RemeasureQueueSnapshot',
]
