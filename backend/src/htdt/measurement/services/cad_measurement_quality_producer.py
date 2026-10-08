"""Production producer for measurement quality reports (#REV42-QUALITYPROD).

The audit found ``build_measurement_quality_report`` /
``CadMeasurementQualityRepository.save_report`` had no production caller:
every committed measurement stayed report-less, so quality-gated consumers
(calibration verification evidence, the runner's derived commit status,
workspace quality cells) could only ever resolve to ``missing`` /
``quality_pending``.

``CadMeasurementQualityProducer`` closes that gap: it derives a report for
one committed measurement *only* from evidence it can prove against the
persisted authorities, mirrors it through the canonical
``build_measurement_quality_report`` evaluator, and lets
``save_report``'s replay validation enforce consistency. Nothing the
producer cannot prove is claimed — non-derivable fields stay ``None`` and
their checks resolve to honest ``UNKNOWN`` / ``NOT_EVALUATED``, never a
fabricated PASS.

Per-field provenance of the produced evidence:

- ``usable_frequency_band_hz`` — the imported FR grid bounds
  ``(frequency_hz[0], frequency_hz[-1])`` of the subject's verified raw
  asset; attested by the bound machine observation.
- ``timing_*`` — copied verbatim from the persisted
  ``CadAcquisitionContext`` covering the measurement (the context is the
  sole authority); all ``None`` when no context exists.
- ``has_impulse_response`` / ``ir_window_*`` — the persisted IR datasets
  bound to the same measurement; the window is the time axis of the
  persisted IR dataset (deterministic ``dataset_id`` order; its identity
  is recorded in the observation provenance). ``ir_truncated`` stays
  ``None``: a text IR export cannot prove it was not truncated, so the
  check stays ``UNKNOWN``.
- ``calibration_*`` — only when the bound context's
  ``microphone.calibration_sha256`` resolves through
  ``validate_calibration_file`` to a retained calibration asset; the
  declared file is then both the expected and the applied authority.
  A sha that does not resolve claims nothing (fail closed).
- ``repeat_measurement_ids`` / ``repeatability_rms_db`` — committed
  measurements in the same document with the identical binding
  (revision, content hash, entity, position, channel role, speakers,
  radiation scope) whose datasets share the identical frequency grid and
  level reference; the RMS is the canonical
  ``measurement_repeatability_rms_db`` recomputation, never asserted.
- ``level_reference`` pin — the persisted ``CadDatasetLevelReference``
  for the exact dataset plus the ``CadAcousticLevelCalibration`` it
  binds, when those authorities exist.

Not derivable today (stay ``None`` → honest ``UNKNOWN``): clipping /
peak level, noise floor / signal level / SNR, polarity — REW text and
REW API exports carry no acquisition-time signal metadata — and IR
truncation, which no importer evidence can disprove. Calibration-file,
level-reference, timing-reference and ambient authorities likewise
require the corresponding persisted assets; producing them is a manual
operator step the producer only consumes, never invents.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Literal, Sequence

from ..domain.cad_measurement_ir import CadImpulseResponseDataset
from ..domain.cad_measurement_models import (
    CadFrequencyResponseDataset,
    CadMeasurementRecord,
)
from ..domain.cad_measurement_quality import (
    CadAcquisitionContext,
    CadMeasurementObservation,
    CadMeasurementQualityEvidence,
    CadMeasurementQualityProfile,
    CadMeasurementQualityReport,
    OBSERVATION_EVIDENCE_FIELDS,
    acquisition_context_binding,
    build_measurement_observation,
    build_measurement_quality_profile,
    build_measurement_quality_report,
    measurement_repeatability_rms_db,
    observation_binding,
)
from ..persistence.cad_measurement_quality_repository import (
    CadMeasurementQualityRepository,
)
from ..persistence.cad_measurement_repository import CadMeasurementRepository


_LOGGER = logging.getLogger(__name__)

# Producer identity stamped into each derived observation's provenance.
QUALITY_PRODUCER_VERSION = 'measurement-quality-producer-1'

QualityProductionStatus = Literal['produced', 'current', 'unresolved']

# Record fields that must match verbatim for another committed measurement
# to count as a repeat of the same acquisition binding (mirrors the
# repository's retake/repeat binding checks).
_REPEAT_BINDING_FIELDS: tuple[str, ...] = (
    'document_id',
    'scene_revision_id',
    'scene_content_hash',
    'measurement_entity_id',
    'measurement_position',
    'channel_role',
    'source_speaker_ids',
    'radiation_scope',
)


@dataclass(frozen=True, slots=True)
class QualityProductionResult:
    """Outcome of one ``produce_report`` derivation pass."""

    measurement_id: str
    status: QualityProductionStatus
    report: CadMeasurementQualityReport | None
    detail: str = ''


class CadMeasurementQualityProducer:
    """Derive and persist honest quality reports for committed measurements.

    One producer per quality repository; derivation re-reads the persisted
    authorities every call so a later context/IR/level-reference write is a
    new evidence epoch the next call observes.
    """

    def __init__(
        self,
        quality_repository: CadMeasurementQualityRepository,
        *,
        profile: CadMeasurementQualityProfile | None = None,
    ) -> None:
        self.quality_repository = quality_repository
        self.measurement_repository: CadMeasurementRepository = (
            quality_repository.measurement_repository
        )
        self.profile = (
            profile
            if profile is not None
            else build_measurement_quality_profile()
        )

    # ------------------------------------------------------------------
    # Derivation
    # ------------------------------------------------------------------

    def _covering_context(
        self, measurement_id: str
    ) -> CadAcquisitionContext | None:
        """Newest persisted acquisition context covering the measurement."""
        for context in self.quality_repository.list_acquisition_contexts():
            if measurement_id in context.subject_measurement_ids:
                return context
        return None

    def _repeat_datasets(
        self,
        record: CadMeasurementRecord,
        dataset: CadFrequencyResponseDataset,
    ) -> tuple[tuple[str, ...], tuple[CadFrequencyResponseDataset, ...]]:
        """Same-binding committed measurements on an identical grid/reference.

        Repeats must be re-verified the same way the repository resolves
        them: identical binding fields, a resolvable dataset, and — for the
        canonical RMS — the identical frequency grid and level reference.
        Ordered oldest-first so the evidence tuple is deterministic.
        """
        candidates: list[tuple[CadMeasurementRecord, CadFrequencyResponseDataset]] = []
        for other in self.measurement_repository.list_measurements(
            record.document_id
        ):
            if other.measurement_id == record.measurement_id:
                continue
            if any(
                getattr(other, field) != getattr(record, field)
                for field in _REPEAT_BINDING_FIELDS
            ):
                continue
            other_dataset = self.measurement_repository.dataset_for_measurement(
                other.measurement_id
            )
            if other_dataset is None:
                continue
            if (
                other_dataset.frequency_hz != dataset.frequency_hz
                or other_dataset.level_reference != dataset.level_reference
            ):
                continue
            candidates.append((other, other_dataset))
        candidates.sort(key=lambda pair: (pair[0].imported_at, pair[0].measurement_id))
        if not candidates:
            return (), ()
        ids = tuple(item[0].measurement_id for item in candidates) + (
            record.measurement_id,
        )
        datasets = tuple(item[1] for item in candidates) + (dataset,)
        return ids, datasets

    def _resolve_calibration(
        self, context: CadAcquisitionContext | None
    ) -> tuple[str | None, str | None]:
        """(filename, sha256) of the retained mic calibration, when provable.

        The context-declared calibration hash only becomes claimable when it
        resolves to a retained calibration-file asset — an unresolved digest
        is a pointer to nothing and claims nothing (fail closed).
        """
        if context is None or context.microphone is None:
            return None, None
        digest = context.microphone.calibration_sha256
        if digest is None:
            return None, None
        try:
            asset = self.quality_repository.validate_calibration_file(digest)
        except Exception:
            _LOGGER.warning(
                'context %s declares an unresolvable calibration file %s',
                context.acquisition_context_id,
                digest,
            )
            return None, None
        return asset.filename, digest

    def _observation(
        self,
        record: CadMeasurementRecord,
        dataset: CadFrequencyResponseDataset,
        ir_datasets: Sequence[CadImpulseResponseDataset],
        *,
        observed_at_utc: str | None,
    ) -> CadMeasurementObservation:
        """The observation authority attesting the derivable fields.

        Reuses an already-persisted observation whose attestation is
        identical — a repeated derivation is the same observation, never a
        new authority row.
        """
        # ``ir_datasets_for_measurement`` orders by dataset_id — a
        # deterministic choice even when several IRs are bound.
        ir_dataset = ir_datasets[-1] if ir_datasets else None
        has_ir = ir_dataset is not None
        source_kind = 'mixed' if has_ir else 'raw_asset'
        provenance: dict[str, object] = {
            'producer': 'cad_measurement_quality_producer',
            'producer_version': QUALITY_PRODUCER_VERSION,
            'field_sources': {
                'usable_frequency_band_hz': 'dataset.frequency_hz bounds',
            },
        }
        if ir_dataset is not None:
            # The observation pins the FR raw asset (report bindings require
            # it); the IR-derived values name their own artifact here.
            provenance['field_sources'].update(
                {
                    'has_impulse_response': 'persisted impulse-response dataset',
                    'ir_window_start_s': 'impulse-response dataset start_time_s',
                    'ir_window_end_s': 'impulse-response dataset end_time_s',
                }
            )
            provenance['ir_dataset_id'] = ir_dataset.dataset_id
            provenance['ir_source_sha256'] = ir_dataset.source_sha256
        candidate = build_measurement_observation(
            measurement_id=record.measurement_id,
            source_kind=source_kind,
            source_asset_sha256=dataset.source_sha256,
            observed_at_utc=observed_at_utc,
            usable_frequency_band_hz=(
                dataset.frequency_hz[0],
                dataset.frequency_hz[-1],
            ),
            has_impulse_response=has_ir,
            ir_window_start_s=(
                ir_dataset.start_time_s if ir_dataset is not None else None
            ),
            ir_window_end_s=(
                ir_dataset.end_time_s if ir_dataset is not None else None
            ),
            ir_truncated=None,
            provenance_json=json.dumps(
                provenance,
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
            ),
        )
        for existing in self.quality_repository.list_observations(
            record.measurement_id
        ):
            if (
                existing.source_kind == candidate.source_kind
                and existing.source_asset_sha256
                == candidate.source_asset_sha256
                and all(
                    getattr(existing, field) == getattr(candidate, field)
                    for field in OBSERVATION_EVIDENCE_FIELDS
                )
            ):
                return existing
        self.quality_repository.save_observation(candidate)
        return candidate

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def produce_report(
        self,
        measurement_id: str,
        *,
        observed_at_utc: str | None = None,
        created_at_utc: str | None = None,
    ) -> QualityProductionResult:
        """Derive, persist (when new) and return the measurement's report.

        Idempotent: when the derivable evidence still matches the latest
        persisted report nothing is written and the existing report is
        returned with status ``'current'``. ``'unresolved'`` means the
        measurement or its dataset could not be verified — the caller keeps
        the honest pending state.
        """
        record = self.measurement_repository.get_measurement(measurement_id)
        if record is None:
            return QualityProductionResult(
                measurement_id, 'unresolved', None, 'measurement not found'
            )
        dataset = self.measurement_repository.dataset_for_measurement(
            measurement_id
        )
        if dataset is None:
            return QualityProductionResult(
                measurement_id, 'unresolved', None, 'dataset not found'
            )

        context = self._covering_context(measurement_id)
        ir_datasets = self.measurement_repository.ir_datasets_for_measurement(
            measurement_id
        )
        level_reference = self.quality_repository.get_dataset_level_reference(
            dataset.dataset_id
        )
        level_calibration = (
            self.quality_repository.get_level_calibration(
                level_reference.calibration_id
            )
            if level_reference is not None
            and level_reference.calibration_id is not None
            else None
        )
        repeat_ids, repeat_datasets = self._repeat_datasets(record, dataset)
        repeatability_rms = (
            measurement_repeatability_rms_db(repeat_datasets)
            if len(repeat_datasets) >= 2
            else None
        )
        calibration_filename, calibration_sha = self._resolve_calibration(context)
        observation = self._observation(
            record, dataset, ir_datasets, observed_at_utc=observed_at_utc
        )

        evidence = CadMeasurementQualityEvidence(
            usable_frequency_band_hz=observation.usable_frequency_band_hz,
            timing_reference_valid=(
                None if context is None else context.timing_reference_valid
            ),
            timing_reference_id=(
                None if context is None else context.timing_reference_id
            ),
            clock_source=(None if context is None else context.clock_source),
            sample_rate_hz=(None if context is None else context.sample_rate_hz),
            delay_correction_s=(
                None if context is None else context.delay_correction_s
            ),
            has_impulse_response=observation.has_impulse_response,
            ir_window_start_s=observation.ir_window_start_s,
            ir_window_end_s=observation.ir_window_end_s,
            ir_truncated=observation.ir_truncated,
            calibration_filename=calibration_filename,
            calibration_file_sha256=calibration_sha,
            expected_calibration_file_sha256=calibration_sha,
            repeat_measurement_ids=repeat_ids,
            repeatability_rms_db=repeatability_rms,
            evidence_source=observation.source_kind,
        )
        report = build_measurement_quality_report(
            measurement=record,
            dataset=dataset,
            evidence=evidence,
            profile=self.profile,
            acquisition_context=(
                None if context is None else acquisition_context_binding(context)
            ),
            observation=observation_binding(observation),
            dataset_level_reference=level_reference,
            level_calibration=level_calibration,
            acquisition_context_record=context,
            observation_record=observation,
            created_at_utc=created_at_utc,
        )

        latest = self.quality_repository.latest_report(measurement_id)
        if latest is not None and _same_report_epoch(latest, report):
            return QualityProductionResult(
                measurement_id, 'current', latest
            )
        self.quality_repository.save_report(report)
        return QualityProductionResult(
            measurement_id, 'produced', report
        )

    def ensure_reports(
        self,
        document_id: str,
        *,
        observed_at_utc: str | None = None,
        created_at_utc: str | None = None,
    ) -> tuple[QualityProductionResult, ...]:
        """Backfill: produce a report for every committed measurement without one.

        Only measurements missing a report are derived — an existing report
        is a sealed historical epoch, not a row to rewrite. Measurements
        whose evidence cannot be verified stay unresolved and keep their
        honest pending state; the failure is recorded, never silently
        dropped.
        """
        results: list[QualityProductionResult] = []
        for record in self.measurement_repository.list_measurements(document_id):
            if (
                self.quality_repository.latest_report(record.measurement_id)
                is not None
            ):
                continue
            try:
                results.append(
                    self.produce_report(
                        record.measurement_id,
                        observed_at_utc=observed_at_utc,
                        created_at_utc=created_at_utc,
                    )
                )
            except Exception as exc:  # fail closed per measurement
                _LOGGER.warning(
                    'quality report production failed for %s: %r',
                    record.measurement_id,
                    exc,
                )
                results.append(
                    QualityProductionResult(
                        record.measurement_id,
                        'unresolved',
                        None,
                        str(exc),
                    )
                )
        return tuple(results)


def _same_report_epoch(
    persisted: CadMeasurementQualityReport,
    derived: CadMeasurementQualityReport,
) -> bool:
    """Two reports describe the same evidence epoch.

    ``report_id`` / ``created_at_utc`` / ``report_sha256`` name the row,
    not the evidence — everything else must be identical for an existing
    report to stand in for a fresh derivation.
    """
    excluded = {'report_id', 'created_at_utc', 'report_sha256'}
    persisted_payload = persisted.model_dump(mode='json')
    derived_payload = derived.model_dump(mode='json')
    return {
        key: value
        for key, value in persisted_payload.items()
        if key not in excluded
    } == {
        key: value
        for key, value in derived_payload.items()
        if key not in excluded
    }
