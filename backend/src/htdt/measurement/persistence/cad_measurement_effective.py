"""Canonical effective-measurement resolver (#509 downstream enforcement, #839/#844).

Every normal-use consumer must decide eligibility from the *effective*
evidence, never from the bare immutable ``CadMeasurementRecord``:

    immutable acquisition record
    + current disposition (latest append-only event)
    + exact correction when the disposition is ``corrected``
    + bound dataset
    + retake lineage selection
    = effective measurement evidence

``resolve`` returns that bundle; ``require_normal_use`` fails closed for
``misassigned`` / ``excluded_from_normal_use`` / ``test_only`` /
``duplicate_import`` evidence. The original record stays immutable and
inspectable — eligibility is a derived, current-time property, so a
measurement excluded *after* it supported a completion makes derived
trust stale without rewriting history.
"""

from __future__ import annotations

from ..domain.cad_measurement_disposition import (
    EffectiveMeasurementEvidence,
    MEASUREMENT_ELIGIBLE_DISPOSITIONS,
)
from .cad_measurement_repository import CadMeasurementRepository
from .cad_measurement_quality_repository import CadMeasurementQualityRepository


class CadEffectiveMeasurementResolver:
    """Shared read boundary over measurement + quality repositories."""

    def __init__(
        self,
        measurement_repository: CadMeasurementRepository,
        quality_repository: CadMeasurementQualityRepository | None = None,
    ) -> None:
        self.measurement_repository = measurement_repository
        # Consumers that only hold the measurement repository get the
        # disposition/correction authority over the same native database —
        # the quality repository is a facade on the shared path. When the
        # supplied repository is a narrow stand-in (tests) rather than the
        # real authority, there are simply no lifecycle records to apply.
        if quality_repository is not None:
            self.quality_repository: CadMeasurementQualityRepository | None = (
                quality_repository
            )
        else:
            try:
                self.quality_repository = CadMeasurementQualityRepository(
                    measurement_repository
                )
            except (AttributeError, TypeError):
                self.quality_repository = None

    def resolve(self, measurement_id: str) -> EffectiveMeasurementEvidence:
        """Resolve the current effective evidence for one measurement id.

        ``measurement_id`` names the immutable original record. Raises
        ``ValueError`` when the id is unknown — an id that resolves to
        nothing cannot carry evidence at all.
        """
        measurement = self.measurement_repository.get_measurement(measurement_id)
        if measurement is None:
            raise ValueError(f'measurement evidence does not exist: {measurement_id}')
        dataset = self.measurement_repository.dataset_for_measurement(measurement_id)
        latest_disposition = getattr(self.quality_repository, 'latest_disposition', None)
        latest_correction = getattr(self.quality_repository, 'latest_correction', None)
        latest_report = getattr(self.quality_repository, 'latest_report', None)
        selected_for_lineage = getattr(
            self.quality_repository, 'selected_measurement_for_lineage', None
        )
        disposition = (
            latest_disposition(measurement_id)
            if latest_disposition is not None
            else None
        )
        correction = (
            latest_correction(measurement_id)
            if latest_correction is not None
            else None
        )
        report = (
            latest_report(measurement_id) if latest_report is not None else None
        )
        selected_id = (
            selected_for_lineage(measurement_id)
            if selected_for_lineage is not None
            else measurement_id
        )
        state = None if disposition is None else disposition.disposition
        eligible = state is None or state in MEASUREMENT_ELIGIBLE_DISPOSITIONS
        reasons = () if eligible else (state,)
        return EffectiveMeasurementEvidence(
            measurement=measurement,
            dataset=dataset,
            disposition=disposition,
            correction=correction,
            latest_quality_report=report,
            selected_measurement_id=selected_id,
            is_selected_head=(selected_id == measurement_id),
            measurement_entity_id=(
                getattr(measurement, 'measurement_entity_id', '')
                if correction is None or correction.measurement_entity_id is None
                else correction.measurement_entity_id
            ),
            channel_role=(
                getattr(measurement, 'channel_role', '')
                if correction is None or correction.channel_role is None
                else correction.channel_role
            ),
            source_speaker_ids=(
                tuple(getattr(measurement, 'source_speaker_ids', ()))
                if correction is None or correction.source_speaker_ids is None
                else correction.source_speaker_ids
            ),
            radiation_scope=(
                getattr(measurement, 'radiation_scope', '')
                if correction is None or correction.radiation_scope is None
                else correction.radiation_scope
            ),
            routing_evidence=(
                getattr(measurement, 'routing_evidence', '')
                if correction is None or correction.routing_evidence is None
                else correction.routing_evidence
            ),
            is_normally_eligible=eligible,
            ineligibility_reasons=reasons,
        )

    def dataset_for_measurement(self, measurement_id: str):
        """Pass-through for evidence-adjacent dataset reads (e.g. the
        verification contract's before-measurement lineage check in
        ``calibration.domain``)."""
        return self.measurement_repository.dataset_for_measurement(measurement_id)

    def require_normal_use(
        self,
        measurement_id: str,
        *,
        purpose: str = 'normal use',
    ) -> EffectiveMeasurementEvidence:
        """Fail closed: return the effective evidence or raise ``ValueError``.

        Dispositioned evidence (misassigned / excluded / test-only /
        duplicate import) and unknown ids are rejected for every normal-use
        purpose; the pinned correction is applied to the effective binding
        so a ``corrected`` measurement is judged by its corrected
        assignment, not by the original wrong one.
        """
        evidence = self.resolve(measurement_id)
        if not evidence.is_normally_eligible:
            raise ValueError(
                f'measurement {measurement_id} is not eligible for {purpose}: '
                + ', '.join(evidence.ineligibility_reasons)
            )
        return evidence


__all__ = [
    'CadEffectiveMeasurementResolver',
    'EffectiveMeasurementEvidence',
]
