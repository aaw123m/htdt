from __future__ import annotations

from math import sqrt
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_applicability import (
    APPLICABILITY_MANUAL_EVALUATOR_ID,
    AUTOMATED_EVALUATOR_BY_CODE,
    CadApplicabilityAttestationRepository,
    evaluate_applicability,
    resolve_applicability_context,
)
from .cad_measurement_repository import CadMeasurementRepository
from .cad_model_validation import CadModelValidationRecord, EvidenceScope, build_full_model_validation
from .cad_objective_repository import CadObjectiveRepository
from .cad_roomsim_repository import CadRoomSimRepository
from .cad_roomsim_results import roomsim_attempt_frequency_response
from .cad_search import generate_cad_candidates
from .cad_search_repository import CadSearchRepository
from .cad_validation_metrics import (
    CadApplicabilityCheck,
    CadObjectiveValidationSample,
    build_candidate_separation_check,
    build_repeatability_check,
    build_sensitivity_check,
)
from .comparison import FrequencyResponse


class CadValidationObjectiveBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    objective_id: str = Field(min_length=1)
    predicted_evaluation_id: str = Field(min_length=1)
    measured_evaluation_id: str = Field(min_length=1)


class CadValidationCandidateBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: str = Field(min_length=1)
    split: Literal['calibration', 'holdout']
    prediction_attempt_id: str = Field(min_length=1)
    measurement_id: str = Field(min_length=1)
    objectives: tuple[CadValidationObjectiveBinding, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def unique_objectives(self) -> 'CadValidationCandidateBinding':
        ids = [binding.objective_id for binding in self.objectives]
        if len(ids) != len(set(ids)):
            raise ValueError('validation candidate objective bindings must be unique')
        return self


class CadValidationSensitivitySpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    objective_id: str = Field(min_length=1)
    candidate_a_id: str = Field(min_length=1)
    candidate_b_id: str = Field(min_length=1)
    max_observed_sensitivity_per_m: float = Field(gt=0)
    max_model_error_per_m: float = Field(gt=0)


class CadValidationRepeatabilitySpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    measurement_ids: tuple[str, ...] = Field(min_length=2)
    reference_band_hz: tuple[float, float] | None = None

    @model_validator(mode='after')
    def unique_measurements(self) -> 'CadValidationRepeatabilitySpec':
        if len(self.measurement_ids) != len(set(self.measurement_ids)):
            raise ValueError('repeatability measurement ids must be unique')
        return self


class CadValidationSeparationSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_a_id: str = Field(min_length=1)
    candidate_b_id: str = Field(min_length=1)
    measurement_a_id: str = Field(min_length=1)
    measurement_b_id: str = Field(min_length=1)
    repeatability_measurement_ids: tuple[str, ...] = Field(min_length=2)
    min_repeatability_multiple: float = Field(gt=0)


class CadValidationApplicabilitySpec(BaseModel):
    """One preregistered applicability code to evaluate for a validation record.

    ``auto`` asks the evaluator registered for the code to derive the decision
    from resolved scene/spec/batch/plan authority. ``manual`` binds a persisted
    immutable :class:`CadApplicabilityAttestation` by id. Neither mode accepts a
    caller-supplied pass/fail — the decision is always derived from authority.
    """

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1)
    mode: Literal['auto', 'manual'] = 'auto'
    attestation_id: str | None = Field(default=None, min_length=1)
    detail: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_request(self) -> 'CadValidationApplicabilitySpec':
        if self.mode == 'manual' and not self.attestation_id:
            raise ValueError('manual applicability requires an attestation id')
        if self.mode == 'auto' and self.attestation_id is not None:
            raise ValueError('auto applicability must not claim an attestation')
        return self


class CadModelValidationBuildSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    search_spec_id: str = Field(min_length=1)
    candidate_set_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    campaign_id: str | None = Field(default=None, min_length=1)
    campaign_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    campaign_registration_id: str | None = Field(
        default=None,
        pattern=r'^o60-validation-campaign-registration:[0-9a-f]{64}$',
    )
    campaign_registration_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    model_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    evidence_scope: EvidenceScope
    low_hz: float = Field(gt=0)
    high_hz: float = Field(gt=0)
    max_holdout_rms_db: float = Field(gt=0)
    candidates: tuple[CadValidationCandidateBinding, ...] = Field(min_length=1)
    sensitivity: tuple[CadValidationSensitivitySpec, ...] = ()
    repeatability: tuple[CadValidationRepeatabilitySpec, ...] = ()
    separation: tuple[CadValidationSeparationSpec, ...] = ()
    applicability: tuple[CadValidationApplicabilitySpec, ...] = ()
    trend_tolerance_by_objective: dict[str, float] = Field(default_factory=dict)
    trend_min_comparable_pairs: int = Field(default=1, ge=1)
    trend_min_agreement_ratio: float = Field(default=0.75, ge=0, le=1)

    @model_validator(mode='after')
    def valid_build_spec(self) -> 'CadModelValidationBuildSpec':
        if self.high_hz <= self.low_hz:
            raise ValueError('validation band is invalid')
        registration_fields = (
            self.campaign_registration_id,
            self.campaign_registration_sha256,
        )
        if self.evidence_scope == 'owned_room':
            if self.campaign_id is None or self.campaign_sha256 is None:
                raise ValueError('owned-room build spec requires a validation campaign')
            if any(field is None for field in registration_fields):
                raise ValueError(
                    'owned-room build spec requires a durable campaign registration'
                )
        else:
            if self.campaign_id is not None or self.campaign_sha256 is not None:
                raise ValueError('synthetic build spec must not claim a validation campaign')
            if any(field is not None for field in registration_fields):
                raise ValueError(
                    'synthetic build spec must not claim a campaign registration'
                )
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError('validation candidate bindings must be unique')
        split_by_candidate = {candidate.candidate_id: candidate.split for candidate in self.candidates}
        if not any(split == 'calibration' for split in split_by_candidate.values()):
            raise ValueError('validation build spec requires calibration candidates')
        if not any(split == 'holdout' for split in split_by_candidate.values()):
            raise ValueError('validation build spec requires holdout candidates')
        if any(value < 0 for value in self.trend_tolerance_by_objective.values()):
            raise ValueError('trend tolerances must be non-negative')
        return self


class CadModelValidationService:
    """Build O60 records from immutable repositories without reclassifying evidence."""

    def __init__(
        self,
        search_repository: CadSearchRepository,
        roomsim_repository: CadRoomSimRepository,
        measurement_repository: CadMeasurementRepository,
        objective_repository: CadObjectiveRepository,
    ) -> None:
        self.search_repository = search_repository
        self.roomsim_repository = roomsim_repository
        self.measurement_repository = measurement_repository
        self.objective_repository = objective_repository
        paths = {
            str(search_repository.path),
            str(roomsim_repository.path),
            str(measurement_repository.path),
            str(objective_repository.path),
        }
        if len(paths) != 1:
            raise ValueError('O60 validation service repositories must share one native CAD database')
        self.applicability_attestations = CadApplicabilityAttestationRepository(
            Path(search_repository.path)
        )

    def _measurement_response(self, measurement_id: str) -> FrequencyResponse:
        dataset = self.measurement_repository.dataset_for_measurement(measurement_id)
        if dataset is None:
            raise ValueError(f'validation measurement has no frequency response: {measurement_id}')
        return FrequencyResponse(frequency_hz=dataset.frequency_hz, level_db=dataset.level_db)

    def _candidate_positions(self, spec, candidate_ids: set[str], candidate_set_sha256: str):
        remaining = set(candidate_ids)
        found = {}
        offset = 0
        page_limit = min(1000, spec.candidate_limit)
        while remaining:
            page = generate_cad_candidates(
                self.search_repository.scene_repository,
                spec,
                offset=offset,
                limit=page_limit,
            )
            if page.candidate_set_sha256 != candidate_set_sha256:
                raise ValueError('validation build candidate-set hash mismatch')
            for candidate in page.candidates:
                if candidate.candidate_id in remaining:
                    found[candidate.candidate_id] = candidate.positions
                    remaining.remove(candidate.candidate_id)
            offset += len(page.candidates)
            if not page.candidates or offset >= page.feasible_candidate_count:
                break
        if remaining:
            raise ValueError(f'validation build candidates not in SearchSpec: {sorted(remaining)}')
        return found

    @staticmethod
    def _placement_distance(left: dict, right: dict) -> float:
        if set(left) != set(right):
            raise ValueError('sensitivity candidates do not share moved entities')
        squared = 0.0
        for entity_id in sorted(left):
            for axis in ('x_m', 'y_m', 'z_m'):
                delta = float(right[entity_id][axis]) - float(left[entity_id][axis])
                squared += delta * delta
        distance = sqrt(squared)
        if distance <= 0:
            raise ValueError('sensitivity candidate placement delta must be positive')
        return distance

    @staticmethod
    def _scoped_measurement_ids(build_spec: CadModelValidationBuildSpec) -> tuple[str, ...]:
        ids = {binding.measurement_id for binding in build_spec.candidates}
        for check in build_spec.repeatability:
            ids.update(check.measurement_ids)
        for check in build_spec.separation:
            ids.update((check.measurement_a_id, check.measurement_b_id))
        return tuple(sorted(ids))

    def _derive_applicability_checks(
        self,
        spec,
        build_spec: CadModelValidationBuildSpec,
    ) -> tuple[CadApplicabilityCheck, ...]:
        """Derive every requested applicability check from exact authority.

        Automated codes are re-derived from the resolved SceneRevision /
        SearchSpec / prediction-batch / Measurement Plan evidence; manual codes
        resolve a persisted immutable attestation. A caller can never supply
        the pass/fail decision directly.
        """
        if not build_spec.applicability:
            return ()
        context = resolve_applicability_context(
            document_id=spec.document_id,
            search_spec_id=spec.search_spec_id,
            search_spec_sha256=spec.search_spec_sha256,
            candidate_set_sha256=build_spec.candidate_set_sha256,
            model_id=build_spec.model_id,
            model_version=build_spec.model_version,
            evidence_scope=build_spec.evidence_scope,
            campaign_id=build_spec.campaign_id,
            requested_band_hz=(float(build_spec.low_hz), float(build_spec.high_hz)),
            pair_attempt_ids=tuple(
                binding.prediction_attempt_id for binding in build_spec.candidates
            ),
            pair_measurement_ids=tuple(
                binding.measurement_id for binding in build_spec.candidates
            ),
            scoped_measurement_ids=self._scoped_measurement_ids(build_spec),
            search_repository=self.search_repository,
            roomsim_repository=self.roomsim_repository,
            measurement_repository=self.measurement_repository,
            attestation_repository=self.applicability_attestations,
        )
        checks: list[CadApplicabilityCheck] = []
        for request in build_spec.applicability:
            if request.mode == 'manual':
                evaluator_id = APPLICABILITY_MANUAL_EVALUATOR_ID
                attestation_id = request.attestation_id
            else:
                evaluator_id = AUTOMATED_EVALUATOR_BY_CODE.get(request.code)
                if evaluator_id is None:
                    raise ValueError(
                        'no automated applicability evaluator is registered '
                        f'for code: {request.code}'
                    )
                attestation_id = None
            checks.append(evaluate_applicability(
                context,
                code=request.code,
                evaluator_id=evaluator_id,
                detail=request.detail,
                attestation_id=attestation_id,
            ))
        return tuple(checks)

    def build(self, build_spec: CadModelValidationBuildSpec) -> CadModelValidationRecord:
        spec = self.search_repository.get(build_spec.search_spec_id)
        if spec is None:
            raise ValueError('validation SearchSpec does not exist')

        response_samples = []
        objective_samples: list[CadObjectiveValidationSample] = []
        candidate_bindings = {item.candidate_id: item for item in build_spec.candidates}

        for binding in build_spec.candidates:
            attempt = self.roomsim_repository.get_attempt(binding.prediction_attempt_id)
            if attempt is None or attempt.status != 'completed':
                raise ValueError('validation requires completed prediction attempts')
            if attempt.candidate_id != binding.candidate_id:
                raise ValueError('prediction attempt candidate mismatch')
            predicted_response = roomsim_attempt_frequency_response(attempt)
            measured_response = self._measurement_response(binding.measurement_id)
            response_samples.append((
                binding.candidate_id,
                binding.split,
                binding.prediction_attempt_id,
                binding.measurement_id,
                predicted_response,
                measured_response,
            ))

            for objective in binding.objectives:
                predicted = self.objective_repository.get_evaluation(
                    objective.predicted_evaluation_id
                )
                measured = self.objective_repository.get_evaluation(
                    objective.measured_evaluation_id
                )
                if predicted is None or measured is None:
                    raise ValueError('validation objective evaluation does not exist')
                predicted_metric = predicted.vector.metric(objective.objective_id)
                measured_metric = measured.vector.metric(objective.objective_id)
                if predicted_metric.unit != measured_metric.unit:
                    raise ValueError('validation objective evaluation units do not match')
                objective_samples.append(CadObjectiveValidationSample(
                    candidate_id=binding.candidate_id,
                    split=binding.split,
                    objective_id=objective.objective_id,
                    unit=predicted_metric.unit,
                    predicted_evaluation_id=predicted.evaluation_id,
                    measured_evaluation_id=measured.evaluation_id,
                    predicted_value=predicted_metric.value,
                    measured_value=measured_metric.value,
                ))

        objective_map = {
            (sample.candidate_id, sample.objective_id): sample
            for sample in objective_samples
        }
        sensitivity_candidate_ids = {
            candidate_id
            for check in build_spec.sensitivity
            for candidate_id in (check.candidate_a_id, check.candidate_b_id)
        }
        positions = (
            self._candidate_positions(
                spec,
                sensitivity_candidate_ids,
                build_spec.candidate_set_sha256,
            )
            if sensitivity_candidate_ids
            else {}
        )
        sensitivity_checks = []
        for check in build_spec.sensitivity:
            left = objective_map.get((check.candidate_a_id, check.objective_id))
            right = objective_map.get((check.candidate_b_id, check.objective_id))
            if left is None or right is None:
                raise ValueError('sensitivity objective sample is missing')
            if left.split != 'holdout' or right.split != 'holdout':
                raise ValueError('sensitivity checks require holdout candidates')
            if left.unit != right.unit:
                raise ValueError('sensitivity objective units do not match')
            sensitivity_checks.append(build_sensitivity_check(
                objective_id=check.objective_id,
                unit=left.unit,
                candidate_a_id=check.candidate_a_id,
                candidate_b_id=check.candidate_b_id,
                placement_delta_m=self._placement_distance(
                    positions[check.candidate_a_id],
                    positions[check.candidate_b_id],
                ),
                predicted_a=left.predicted_value,
                predicted_b=right.predicted_value,
                measured_a=left.measured_value,
                measured_b=right.measured_value,
                max_observed_sensitivity_per_m=check.max_observed_sensitivity_per_m,
                max_model_error_per_m=check.max_model_error_per_m,
            ))

        repeatability_checks = []
        for repeat_spec in build_spec.repeatability:
            records = [
                self.measurement_repository.get_measurement(measurement_id)
                for measurement_id in repeat_spec.measurement_ids
            ]
            if any(record is None for record in records):
                raise ValueError('repeatability measurement does not exist')
            revision_ids = {record.scene_revision_id for record in records if record is not None}
            if len(revision_ids) != 1:
                raise ValueError('repeatability measurements must share one SceneRevision')
            repeatability_checks.append(build_repeatability_check(
                scene_revision_id=next(iter(revision_ids)),
                measurements=tuple(
                    (
                        measurement_id,
                        self._measurement_response(measurement_id),
                    )
                    for measurement_id in repeat_spec.measurement_ids
                ),
                low_hz=build_spec.low_hz,
                high_hz=build_spec.high_hz,
                reference_band_hz=repeat_spec.reference_band_hz,
            ))

        repeatability_by_ids = {
            frozenset(check.measurement_ids): check
            for check in repeatability_checks
        }
        separation_checks = []
        for separation in build_spec.separation:
            repeatability = repeatability_by_ids.get(
                frozenset(separation.repeatability_measurement_ids)
            )
            if repeatability is None:
                raise ValueError('candidate separation references unknown repeatability group')
            if separation.candidate_a_id not in candidate_bindings or separation.candidate_b_id not in candidate_bindings:
                raise ValueError('candidate separation references candidate outside validation study')
            separation_checks.append(build_candidate_separation_check(
                candidate_a_id=separation.candidate_a_id,
                candidate_b_id=separation.candidate_b_id,
                measurement_a_id=separation.measurement_a_id,
                measurement_b_id=separation.measurement_b_id,
                response_a=self._measurement_response(separation.measurement_a_id),
                response_b=self._measurement_response(separation.measurement_b_id),
                low_hz=build_spec.low_hz,
                high_hz=build_spec.high_hz,
                repeatability_floor_db=repeatability.rms_floor_db,
                min_repeatability_multiple=separation.min_repeatability_multiple,
            ))

        applicability_checks = self._derive_applicability_checks(spec, build_spec)

        return build_full_model_validation(
            document_id=spec.document_id,
            search_spec_id=spec.search_spec_id,
            search_spec_sha256=spec.search_spec_sha256,
            candidate_set_sha256=build_spec.candidate_set_sha256,
            campaign_id=build_spec.campaign_id,
            campaign_sha256=build_spec.campaign_sha256,
            campaign_registration_id=build_spec.campaign_registration_id,
            campaign_registration_sha256=build_spec.campaign_registration_sha256,
            model_id=build_spec.model_id,
            model_version=build_spec.model_version,
            response_samples=tuple(response_samples),
            objective_samples=tuple(objective_samples),
            sensitivity_checks=tuple(sensitivity_checks),
            repeatability_checks=tuple(repeatability_checks),
            separation_checks=tuple(separation_checks),
            applicability_checks=applicability_checks,
            low_hz=build_spec.low_hz,
            high_hz=build_spec.high_hz,
            max_holdout_rms_db=build_spec.max_holdout_rms_db,
            evidence_scope=build_spec.evidence_scope,
            trend_tolerance_by_objective=build_spec.trend_tolerance_by_objective,
            trend_min_comparable_pairs=build_spec.trend_min_comparable_pairs,
            trend_min_agreement_ratio=build_spec.trend_min_agreement_ratio,
        )
