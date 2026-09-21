from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Sequence

from pydantic import BaseModel, ConfigDict, Field

from .cad_measurement_quality import dataset_sha256
from .cad_measurement_repository import CadMeasurementRepository
from .cad_model_validation import CadModelValidationRecord
from .cad_model_validation_service import (
    CadModelValidationBuildSpec,
    CadModelValidationService,
    CadValidationCandidateBinding,
    CadValidationObjectiveBinding,
    CadValidationRepeatabilitySpec,
    CadValidationSensitivitySpec,
    CadValidationSeparationSpec,
)
from .cad_objective_models import (
    CadObjectiveInputRef,
    canonical_objective_sha256,
)
from .cad_objectives import build_objective_evaluation
from .cad_objective_repository import CadObjectiveRepository
from .cad_roomsim_repository import CadRoomSimRepository
from .cad_roomsim_results import roomsim_attempt_frequency_response
from .cad_validation_campaign import (
    CadValidationCampaign,
    CadValidationCampaignRegistration,
)
from .cad_validation_campaign_repository import CadValidationCampaignRepository
from .cad_validation_metrics import CadApplicabilityCheck
from .comparison import FrequencyResponse
from .optimization_objectives import (
    ObjectiveVector,
    ResponseObjectiveSpec,
    target_response_objectives,
)


class CadValidationCandidateReadiness(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: str = Field(min_length=1)
    split: str = Field(pattern=r'^(calibration|holdout)$')
    prediction_attempt_id: str | None = None
    measurement_plan_id: str | None = None
    measurement_ids: tuple[str, ...] = ()
    primary_measurement_id: str | None = None
    predicted_evaluation_id: str | None = None
    measured_evaluation_id: str | None = None
    missing_reasons: tuple[str, ...] = ()


class CadValidationCampaignReadiness(BaseModel):
    model_config = ConfigDict(frozen=True)

    campaign_id: str = Field(min_length=1)
    campaign_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    evidence_ready: bool
    candidates: tuple[CadValidationCandidateReadiness, ...]
    required_applicability_codes: tuple[str, ...]
    missing_reasons: tuple[str, ...] = ()


def _parse_aware_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


class CadValidationCampaignService:
    """Resolve preregistered O60 campaigns against immutable evidence."""

    def __init__(
        self,
        campaign_repository: CadValidationCampaignRepository,
        roomsim_repository: CadRoomSimRepository,
        measurement_repository: CadMeasurementRepository,
        objective_repository: CadObjectiveRepository,
        validation_service: CadModelValidationService,
    ) -> None:
        self.campaign_repository = campaign_repository
        self.roomsim_repository = roomsim_repository
        self.measurement_repository = measurement_repository
        self.objective_repository = objective_repository
        self.validation_service = validation_service
        paths = {
            str(campaign_repository.path),
            str(roomsim_repository.path),
            str(measurement_repository.path),
            str(objective_repository.path),
            str(validation_service.search_repository.path),
        }
        if len(paths) != 1:
            raise ValueError('campaign service repositories must share one native CAD database')
        self.path = Path(campaign_repository.path)

    @staticmethod
    def _has_metric_set(evaluation, objective_ids: tuple[str, ...]) -> bool:
        try:
            for objective_id in objective_ids:
                evaluation.vector.metric(objective_id)
        except KeyError:
            return False
        return True

    @staticmethod
    def _unique_semantic_evaluation(evaluations):
        if not evaluations:
            return None, 'missing'
        by_sha = {}
        for evaluation in evaluations:
            by_sha.setdefault(evaluation.evaluation_sha256, evaluation)
        if len(by_sha) != 1:
            return None, 'ambiguous'
        evaluation = next(iter(by_sha.values()))
        return evaluation, None

    def _matching_prediction_attempts(
        self,
        campaign: CadValidationCampaign,
        candidate_id: str,
    ):
        attempts = []
        for batch in self.roomsim_repository.list_batch_specs(campaign.search_spec_id):
            if (
                batch.document_id != campaign.document_id
                or batch.search_spec_sha256 != campaign.search_spec_sha256
                or batch.candidate_set_sha256 != campaign.candidate_set_sha256
                or batch.model_id != campaign.model_id
            ):
                continue
            for attempt in self.roomsim_repository.list_candidate_attempts(
                batch.batch_run_id,
                candidate_id,
            ):
                if (
                    attempt.status == 'completed'
                    and attempt.model_version == campaign.model_version
                ):
                    attempts.append(attempt)
        return tuple(attempts)

    def _candidate_measured_plans(
        self,
        campaign: CadValidationCampaign,
        candidate_id: str,
    ):
        return tuple(
            plan
            for plan in self.measurement_repository.latest_measurement_plans(
                campaign.search_spec_id
            )
            if (
                plan.status == 'measured'
                and plan.candidate_id == candidate_id
                and plan.candidate_set_sha256 == campaign.candidate_set_sha256
            )
        )

    def _validated_measurements(
        self,
        campaign: CadValidationCampaign,
        plan,
        registration: CadValidationCampaignRegistration,
    ):
        campaign_time = _parse_aware_timestamp(registration.registered_at_utc)
        if campaign_time is None:
            raise ValueError(
                'campaign registration registered_at_utc must be timezone-aware'
            )
        records = []
        reasons: list[str] = []
        for measurement_id in plan.measurement_ids:
            record = self.measurement_repository.get_measurement(measurement_id)
            if record is None:
                reasons.append(f'unknown measurement {measurement_id}')
                continue
            if record.evidence_type != 'measured':
                reasons.append(f'{measurement_id}: evidence_type is not measured')
                continue
            captured = _parse_aware_timestamp(record.captured_at)
            if captured is None:
                reasons.append(f'{measurement_id}: captured_at is missing or not timezone-aware')
                continue
            if captured < campaign_time:
                reasons.append(f'{measurement_id}: captured before campaign preregistration')
                continue
            try:
                provenance = json.loads(record.provenance_json)
            except json.JSONDecodeError:
                provenance = None
            if not isinstance(provenance, dict) or provenance.get('validation_scope') != 'owned_room':
                reasons.append(f'{measurement_id}: validation_scope is not owned_room')
                continue
            if provenance.get('validation_campaign_id') != campaign.campaign_id:
                reasons.append(f'{measurement_id}: validation campaign binding mismatch')
                continue
            records.append(record)
        records.sort(key=lambda record: (record.captured_at or '', record.measurement_id))
        return tuple(records), tuple(reasons)

    def _matching_objective_evaluations(
        self,
        campaign: CadValidationCampaign,
        candidate_id: str,
        *,
        evidence_class: str,
        source_kind: str,
        source_id: str,
    ):
        matching = []
        for evaluation in self.objective_repository.list_evaluations(
            campaign.search_spec_id
        ):
            if (
                evaluation.candidate_id != candidate_id
                or evaluation.search_spec_sha256 != campaign.search_spec_sha256
                or evaluation.evaluation_spec_sha256
                != campaign.objective_evaluation_spec_sha256
                or not self._has_metric_set(evaluation, campaign.objective_ids)
            ):
                continue
            if not any(
                ref.evidence_class == evidence_class
                and ref.source_kind == source_kind
                and ref.source_id == source_id
                for ref in evaluation.input_refs
            ):
                continue
            matching.append(evaluation)
        return tuple(matching)

    def _reuse_or_save_evaluation(self, evaluation):
        for existing in self.objective_repository.list_evaluations(
            evaluation.search_spec_id
        ):
            if existing.evaluation_sha256 == evaluation.evaluation_sha256:
                return existing
        self.objective_repository.save_evaluation(evaluation)
        return evaluation

    def materialize_objective_evidence(
        self,
        campaign_id: str,
    ) -> tuple[str, ...]:
        """Create O30 evidence from preregistered target semantics.

        This performs no physical action. It only derives objective vectors from
        already-persisted prediction attempts and owned-room measurements.
        """

        campaign = self.campaign_repository.get(campaign_id)
        if campaign is None:
            raise KeyError(campaign_id)
        registration = self.campaign_repository.get_registration(campaign_id)
        if registration is None:
            raise ValueError(
                'validation campaign registration authority missing/stale'
            )
        search_spec = self.validation_service.search_repository.get(
            campaign.search_spec_id
        )
        if search_spec is None:
            raise ValueError('campaign SearchSpec does not exist')
        revision = self.validation_service.search_repository.scene_repository.get(
            search_spec.scene_revision_id
        )
        if revision is None:
            raise ValueError('campaign source SceneRevision does not exist')

        objective_spec = ResponseObjectiveSpec(
            low_hz=campaign.requested_band_hz[0],
            high_hz=campaign.requested_band_hz[1],
            reference_band_hz=campaign.reference_band_hz,
            excluded_bands=campaign.excluded_bands,
        )
        target = FrequencyResponse(
            frequency_hz=campaign.target_response.frequency_hz,
            level_db=campaign.target_response.level_db,
        )
        evaluation_spec = json.loads(campaign.objective_evaluation_spec_json)
        saved_ids: list[str] = []

        for assignment in campaign.candidates:
            attempts = self._matching_prediction_attempts(
                campaign,
                assignment.candidate_id,
            )
            if len(attempts) != 1:
                raise ValueError(
                    f'{assignment.candidate_id}: objective materialization requires '
                    'exactly one completed prediction attempt'
                )
            plans = self._candidate_measured_plans(
                campaign,
                assignment.candidate_id,
            )
            if len(plans) != 1:
                raise ValueError(
                    f'{assignment.candidate_id}: objective materialization requires '
                    'exactly one completed Measurement Plan'
                )
            records, reasons = self._validated_measurements(
                campaign,
                plans[0],
                registration,
            )
            if reasons or not records:
                detail = '; '.join(reasons) if reasons else 'measured evidence is missing'
                raise ValueError(
                    f'{assignment.candidate_id}: objective materialization is not ready: {detail}'
                )
            measurement = records[0]
            dataset = self.measurement_repository.dataset_for_measurement(
                measurement.measurement_id
            )
            if dataset is None:
                raise ValueError(
                    f'{assignment.candidate_id}: primary measurement has no frequency response'
                )

            attempt_response_sha = getattr(
                attempts[0], 'response_sha256', None
            )
            if attempt_response_sha is None:
                # Match the authority resolver fallback for attempts that only
                # carry a canonical response payload.
                attempt_response_sha = canonical_objective_sha256(
                    json.loads(attempts[0].response_json)
                )
            evidence = (
                (
                    'predicted',
                    CadObjectiveInputRef(
                        evidence_class='predicted',
                        source_kind='cad_roomsim_attempt',
                        source_id=attempts[0].attempt_id,
                        source_sha256=attempt_response_sha,
                    ),
                    roomsim_attempt_frequency_response(attempts[0]),
                ),
                (
                    'measured',
                    CadObjectiveInputRef(
                        evidence_class='measured',
                        source_kind='cad_measurement',
                        source_id=measurement.measurement_id,
                        source_sha256=dataset_sha256(dataset),
                    ),
                    FrequencyResponse(
                        frequency_hz=dataset.frequency_hz,
                        level_db=dataset.level_db,
                    ),
                ),
            )
            for _evidence_class, input_ref, response in evidence:
                full_vector = target_response_objectives(
                    assignment.candidate_id,
                    response,
                    target,
                    objective_spec,
                )
                selected_metrics = tuple(
                    full_vector.metric(objective_id)
                    for objective_id in campaign.objective_ids
                )
                vector = ObjectiveVector(
                    candidate_id=assignment.candidate_id,
                    metrics=selected_metrics,
                )
                evaluation = build_objective_evaluation(
                    revision,
                    search_spec,
                    assignment.candidate_id,
                    vector,
                    evaluation_spec=evaluation_spec,
                    input_refs=(input_ref,),
                )
                stored = self._reuse_or_save_evaluation(evaluation)
                saved_ids.append(stored.evaluation_id)

        return tuple(saved_ids)

    def readiness(self, campaign_id: str) -> CadValidationCampaignReadiness:
        campaign = self.campaign_repository.get(campaign_id)
        if campaign is None:
            raise KeyError(campaign_id)
        registration = self.campaign_repository.get_registration(campaign_id)
        if registration is None:
            raise ValueError(
                'validation campaign registration authority missing/stale'
            )

        repeatability_required = {
            item.candidate_id: item
            for item in campaign.repeatability
        }
        candidates: list[CadValidationCandidateReadiness] = []
        global_reasons: list[str] = []

        for assignment in campaign.candidates:
            reasons: list[str] = []
            prediction_attempt_id: str | None = None
            measurement_plan_id: str | None = None
            measurement_ids: tuple[str, ...] = ()
            primary_measurement_id: str | None = None
            predicted_evaluation_id: str | None = None
            measured_evaluation_id: str | None = None

            attempts = self._matching_prediction_attempts(
                campaign,
                assignment.candidate_id,
            )
            if len(attempts) == 1:
                prediction_attempt_id = attempts[0].attempt_id
            elif not attempts:
                reasons.append('completed prediction attempt is missing')
            else:
                reasons.append('multiple completed prediction attempts are ambiguous')

            plans = self._candidate_measured_plans(
                campaign,
                assignment.candidate_id,
            )
            records = ()
            if len(plans) == 1:
                plan = plans[0]
                measurement_plan_id = plan.plan_id
                records, measurement_reasons = self._validated_measurements(
                    campaign,
                    plan,
                    registration,
                )
                reasons.extend(measurement_reasons)
                measurement_ids = tuple(record.measurement_id for record in records)
                if records:
                    primary_measurement_id = records[0].measurement_id
                else:
                    reasons.append('owned-room measured evidence is missing')
            elif not plans:
                reasons.append('completed Measurement Plan is missing')
            else:
                reasons.append('multiple completed Measurement Plans are ambiguous')

            repeatability = repeatability_required.get(assignment.candidate_id)
            if repeatability is not None and len(measurement_ids) < repeatability.min_measurements:
                reasons.append(
                    f'repeatability requires {repeatability.min_measurements} measurements'
                )

            if prediction_attempt_id is not None:
                predicted, error = self._unique_semantic_evaluation(
                    self._matching_objective_evaluations(
                        campaign,
                        assignment.candidate_id,
                        evidence_class='predicted',
                        source_kind='cad_roomsim_attempt',
                        source_id=prediction_attempt_id,
                    )
                )
                if error == 'missing':
                    reasons.append('predicted objective evaluation is missing')
                elif error == 'ambiguous':
                    reasons.append('predicted objective evaluations are ambiguous')
                elif predicted is not None:
                    predicted_evaluation_id = predicted.evaluation_id

            if primary_measurement_id is not None:
                measured, error = self._unique_semantic_evaluation(
                    self._matching_objective_evaluations(
                        campaign,
                        assignment.candidate_id,
                        evidence_class='measured',
                        source_kind='cad_measurement',
                        source_id=primary_measurement_id,
                    )
                )
                if error == 'missing':
                    reasons.append('measured objective evaluation is missing')
                elif error == 'ambiguous':
                    reasons.append('measured objective evaluations are ambiguous')
                elif measured is not None:
                    measured_evaluation_id = measured.evaluation_id

            candidates.append(CadValidationCandidateReadiness(
                candidate_id=assignment.candidate_id,
                split=assignment.split,
                prediction_attempt_id=prediction_attempt_id,
                measurement_plan_id=measurement_plan_id,
                measurement_ids=measurement_ids,
                primary_measurement_id=primary_measurement_id,
                predicted_evaluation_id=predicted_evaluation_id,
                measured_evaluation_id=measured_evaluation_id,
                missing_reasons=tuple(reasons),
            ))

        for separation in campaign.separation:
            candidate_map = {item.candidate_id: item for item in candidates}
            for candidate_id in (
                separation.candidate_a_id,
                separation.candidate_b_id,
                separation.repeatability_candidate_id,
            ):
                if candidate_id not in candidate_map:
                    global_reasons.append(
                        f'candidate separation references missing candidate {candidate_id}'
                    )

        evidence_ready = (
            not global_reasons
            and all(not item.missing_reasons for item in candidates)
        )
        return CadValidationCampaignReadiness(
            campaign_id=campaign.campaign_id,
            campaign_sha256=campaign.campaign_sha256,
            evidence_ready=evidence_ready,
            candidates=tuple(candidates),
            required_applicability_codes=campaign.required_applicability_codes,
            missing_reasons=tuple(global_reasons),
        )

    def build_validation_record(
        self,
        campaign_id: str,
        applicability_checks: Sequence[CadApplicabilityCheck],
    ) -> CadModelValidationRecord:
        campaign = self.campaign_repository.get(campaign_id)
        if campaign is None:
            raise KeyError(campaign_id)
        registration = self.campaign_repository.get_registration(campaign_id)
        if registration is None:
            raise ValueError(
                'validation campaign registration authority missing/stale'
            )
        readiness = self.readiness(campaign_id)
        if not readiness.evidence_ready:
            reasons = list(readiness.missing_reasons)
            for candidate in readiness.candidates:
                reasons.extend(
                    f'{candidate.candidate_id}: {reason}'
                    for reason in candidate.missing_reasons
                )
            raise ValueError(
                'validation campaign evidence is not ready: ' + '; '.join(reasons)
            )

        checks = tuple(applicability_checks)
        codes = tuple(check.code for check in checks)
        if len(codes) != len(set(codes)):
            raise ValueError('applicability check codes must be unique')
        if set(codes) != set(campaign.required_applicability_codes):
            raise ValueError(
                'applicability checks do not match preregistered campaign requirements'
            )

        candidate_map = {
            item.candidate_id: item
            for item in readiness.candidates
        }
        bindings = []
        for assignment in campaign.candidates:
            evidence = candidate_map[assignment.candidate_id]
            assert evidence.prediction_attempt_id is not None
            assert evidence.primary_measurement_id is not None
            assert evidence.predicted_evaluation_id is not None
            assert evidence.measured_evaluation_id is not None
            bindings.append(CadValidationCandidateBinding(
                candidate_id=assignment.candidate_id,
                split=assignment.split,
                prediction_attempt_id=evidence.prediction_attempt_id,
                measurement_id=evidence.primary_measurement_id,
                objectives=tuple(
                    CadValidationObjectiveBinding(
                        objective_id=objective_id,
                        predicted_evaluation_id=evidence.predicted_evaluation_id,
                        measured_evaluation_id=evidence.measured_evaluation_id,
                    )
                    for objective_id in campaign.objective_ids
                ),
            ))

        sensitivity = tuple(
            CadValidationSensitivitySpec(
                objective_id=requirement.objective_id,
                candidate_a_id=requirement.candidate_a_id,
                candidate_b_id=requirement.candidate_b_id,
                max_observed_sensitivity_per_m=requirement.max_observed_sensitivity_per_m,
                max_model_error_per_m=requirement.max_model_error_per_m,
            )
            for requirement in campaign.sensitivity
        )
        repeatability = tuple(
            CadValidationRepeatabilitySpec(
                measurement_ids=candidate_map[requirement.candidate_id].measurement_ids,
                reference_band_hz=requirement.reference_band_hz,
            )
            for requirement in campaign.repeatability
        )
        repeatability_ids_by_candidate = {
            requirement.candidate_id:
                candidate_map[requirement.candidate_id].measurement_ids
            for requirement in campaign.repeatability
        }
        separation = tuple(
            CadValidationSeparationSpec(
                candidate_a_id=requirement.candidate_a_id,
                candidate_b_id=requirement.candidate_b_id,
                measurement_a_id=(
                    candidate_map[requirement.candidate_a_id].primary_measurement_id
                    or ''
                ),
                measurement_b_id=(
                    candidate_map[requirement.candidate_b_id].primary_measurement_id
                    or ''
                ),
                repeatability_measurement_ids=repeatability_ids_by_candidate[
                    requirement.repeatability_candidate_id
                ],
                min_repeatability_multiple=requirement.min_repeatability_multiple,
            )
            for requirement in campaign.separation
        )

        build_spec = CadModelValidationBuildSpec(
            search_spec_id=campaign.search_spec_id,
            candidate_set_sha256=campaign.candidate_set_sha256,
            campaign_id=campaign.campaign_id,
            campaign_sha256=campaign.campaign_sha256,
            campaign_registration_id=registration.registration_id,
            campaign_registration_sha256=registration.registration_sha256,
            model_id=campaign.model_id,
            model_version=campaign.model_version,
            evidence_scope='owned_room',
            low_hz=campaign.requested_band_hz[0],
            high_hz=campaign.requested_band_hz[1],
            max_holdout_rms_db=campaign.max_holdout_rms_db,
            candidates=tuple(bindings),
            sensitivity=sensitivity,
            repeatability=repeatability,
            separation=separation,
            applicability=checks,
            trend_tolerance_by_objective=campaign.trend_tolerance_by_objective,
            trend_min_comparable_pairs=campaign.trend_min_comparable_pairs,
            trend_min_agreement_ratio=campaign.trend_min_agreement_ratio,
        )
        return self.validation_service.build(build_spec)
