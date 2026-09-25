from __future__ import annotations

from contextlib import closing
from datetime import datetime
from math import sqrt
from pathlib import Path
import json
import sqlite3

from .cad_applicability import (
    CadApplicabilityAttestation,
    CadApplicabilityAttestationRepository,
    rederive_applicability_check,
    resolve_applicability_context,
)
from .cad_measurement_effective import CadEffectiveMeasurementResolver
from .cad_measurement_repository import CadMeasurementRepository
from .cad_model_validation import (
    CadModelValidationRecord,
    recompute_residual_payload,
)
from .cad_objective_repository import CadObjectiveRepository
from .cad_roomsim_repository import CadRoomSimRepository
from .cad_roomsim_results import roomsim_attempt_frequency_response
from .cad_search import iter_cad_candidate_pages
from .cad_search_repository import CadSearchRepository
from .cad_validation_campaign_repository import CadValidationCampaignRepository
from .cad_validation_metrics import (
    build_candidate_separation_check,
    build_repeatability_check,
    build_sensitivity_check,
)
from .comparison import FrequencyResponse
from .cad_schema import require_native_tables


class CadModelValidationIntegrityError(ValueError):
    """A persisted O60 record's exact evidence authority no longer replays.

    Raised on authoritative reads when a record that was valid at save time
    fails re-attestation because external or file-backed evidence has since
    disappeared or changed — a removed Room Simulator attempt/batch, a
    missing or corrupt raw measurement asset, a drifted O30 evaluation, or
    a broken repeatability/separation/campaign/applicability binding. The
    persisted row is never reclassified or deleted: it stays inspectable as
    history through ``inspect``/``inspect_for_search_spec`` and
    ``integrity_problems`` but can no longer authorize O70/O80 production
    work.
    """

    def __init__(self, validation_id: str, detail: str) -> None:
        self.validation_id = validation_id
        super().__init__(
            'persisted model validation '
            f'{validation_id} failed evidence re-attestation: {detail}'
        )


class CadModelValidationRepository:
    """Immutable O60 validation storage with cross-evidence authority checks."""

    def __init__(
        self,
        search_repository: CadSearchRepository,
        roomsim_repository: CadRoomSimRepository,
        measurement_repository: CadMeasurementRepository,
        objective_repository: CadObjectiveRepository | None = None,
    ) -> None:
        self.search_repository = search_repository
        self.roomsim_repository = roomsim_repository
        self.measurement_repository = measurement_repository
        self.objective_repository = objective_repository
        self._effective = CadEffectiveMeasurementResolver(measurement_repository)
        self.path = Path(search_repository.path)
        repositories = (roomsim_repository, measurement_repository)
        if any(Path(repository.path) != self.path for repository in repositories):
            raise ValueError('O60 repositories must share one native CAD database')
        if objective_repository is not None and Path(objective_repository.path) != self.path:
            raise ValueError('O60 objective repository must share one native CAD database')
        self.applicability_attestations = CadApplicabilityAttestationRepository(self.path)
        self._initialize()

    def save_attestation(
        self,
        attestation: CadApplicabilityAttestation,
    ) -> CadApplicabilityAttestation:
        """Persist one immutable manual applicability attestation."""
        return self.applicability_attestations.save(attestation)

    def get_attestation(
        self,
        attestation_id: str,
    ) -> CadApplicabilityAttestation | None:
        return self.applicability_attestations.get(attestation_id)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_model_validations')

    @staticmethod
    def _plan_linked(
        plans,
        *,
        candidate_id: str,
        measurement_id: str,
        candidate_set_sha256: str,
    ) -> bool:
        return any(
            plan.status == 'measured'
            and plan.candidate_id == candidate_id
            and plan.candidate_set_sha256 == candidate_set_sha256
            and measurement_id in plan.measurement_ids
            for plan in plans
        )

    @staticmethod
    def _measurement_plan_for_id(plans, measurement_id: str, candidate_set_sha256: str):
        return next(
            (
                plan
                for plan in plans
                if plan.status == 'measured'
                and plan.candidate_set_sha256 == candidate_set_sha256
                and measurement_id in plan.measurement_ids
            ),
            None,
        )

    def _measurement_response(self, measurement_id: str) -> FrequencyResponse:
        dataset = self.measurement_repository.dataset_for_measurement(measurement_id)
        if dataset is None:
            raise ValueError(f'validation measurement has no frequency response: {measurement_id}')
        return FrequencyResponse(
            frequency_hz=dataset.frequency_hz,
            level_db=dataset.level_db,
        )

    def _candidate_positions(self, spec, candidate_ids: set[str], candidate_set_sha256: str):
        remaining = set(candidate_ids)
        found = {}
        pages = iter_cad_candidate_pages(
            self.search_repository.scene_repository,
            spec,
        )
        while remaining:
            page = next(pages, None)
            if page is None:
                break
            if page.candidate_set_sha256 != candidate_set_sha256:
                raise ValueError('validation candidate-set hash does not match regenerated SearchSpec')
            for candidate in page.candidates:
                if candidate.candidate_id in remaining:
                    found[candidate.candidate_id] = candidate.positions
                    remaining.remove(candidate.candidate_id)
        if remaining:
            raise ValueError(f'validation references candidates outside SearchSpec: {sorted(remaining)}')
        return found

    @staticmethod
    def _placement_distance(left: dict, right: dict) -> float:
        if set(left) != set(right):
            raise ValueError('sensitivity candidates do not share the same moved entities')
        squared = 0.0
        for entity_id in sorted(left):
            if set(left[entity_id]) != {'x_m', 'y_m', 'z_m'} or set(right[entity_id]) != {
                'x_m', 'y_m', 'z_m'
            }:
                raise ValueError('candidate position payload is invalid')
            for axis in ('x_m', 'y_m', 'z_m'):
                delta = float(right[entity_id][axis]) - float(left[entity_id][axis])
                squared += delta * delta
        return sqrt(squared)

    def _validate_objective_samples(self, record: CadModelValidationRecord) -> None:
        if not record.objective_samples:
            return
        repository = self.objective_repository
        if repository is None:
            raise ValueError('full O60 validation requires CadObjectiveRepository')

        for sample in record.objective_samples:
            predicted = repository.get_evaluation(sample.predicted_evaluation_id)
            measured = repository.get_evaluation(sample.measured_evaluation_id)
            if predicted is None or measured is None:
                raise ValueError('objective validation references an unknown evaluation')
            for evaluation, expected_class, expected_value in (
                (predicted, 'predicted', sample.predicted_value),
                (measured, 'measured', sample.measured_value),
            ):
                if (
                    evaluation.document_id != record.document_id
                    or evaluation.search_spec_id != record.search_spec_id
                    or evaluation.search_spec_sha256 != record.search_spec_sha256
                    or evaluation.candidate_id != sample.candidate_id
                ):
                    raise ValueError('objective validation evaluation authority mismatch')
                evidence_classes = {ref.evidence_class for ref in evaluation.input_refs}
                if expected_class not in evidence_classes:
                    raise ValueError(
                        f'objective validation {expected_class} evaluation evidence mismatch'
                    )
                try:
                    metric = evaluation.vector.metric(sample.objective_id)
                except KeyError as exc:
                    raise ValueError('objective validation metric is missing from evaluation') from exc
                if metric.unit != sample.unit or abs(float(metric.value) - float(expected_value)) > 1e-12:
                    raise ValueError('objective validation metric snapshot mismatch')

    def _validate_sensitivity(
        self,
        record: CadModelValidationRecord,
        spec,
    ) -> None:
        if not record.sensitivity_checks:
            return
        sample_map = {
            (sample.candidate_id, sample.objective_id): sample
            for sample in record.objective_samples
        }
        candidate_ids = {
            candidate_id
            for check in record.sensitivity_checks
            for candidate_id in (check.candidate_a_id, check.candidate_b_id)
        }
        positions = self._candidate_positions(spec, candidate_ids, record.candidate_set_sha256)
        for check in record.sensitivity_checks:
            left = sample_map.get((check.candidate_a_id, check.objective_id))
            right = sample_map.get((check.candidate_b_id, check.objective_id))
            if left is None or right is None or left.split != 'holdout' or right.split != 'holdout':
                raise ValueError('sensitivity check requires holdout objective samples for both candidates')
            if left.unit != check.unit or right.unit != check.unit:
                raise ValueError('sensitivity objective unit mismatch')
            distance = self._placement_distance(
                positions[check.candidate_a_id],
                positions[check.candidate_b_id],
            )
            rebuilt = build_sensitivity_check(
                objective_id=check.objective_id,
                unit=check.unit,
                candidate_a_id=check.candidate_a_id,
                candidate_b_id=check.candidate_b_id,
                placement_delta_m=distance,
                predicted_a=left.predicted_value,
                predicted_b=right.predicted_value,
                measured_a=left.measured_value,
                measured_b=right.measured_value,
                max_observed_sensitivity_per_m=check.max_observed_sensitivity_per_m,
                max_model_error_per_m=check.max_model_error_per_m,
            )
            if rebuilt != check:
                raise ValueError('sensitivity check does not match candidate/objective evidence')

    def _validate_repeatability_and_separation(
        self,
        record: CadModelValidationRecord,
        plans,
    ) -> None:
        for check in record.repeatability_checks:
            measurements = []
            for measurement_id in check.measurement_ids:
                measurement = self.measurement_repository.get_measurement(measurement_id)
                if (
                    measurement is None
                    or measurement.evidence_type != 'measured'
                    or measurement.document_id != record.document_id
                    or measurement.scene_revision_id != check.scene_revision_id
                ):
                    raise ValueError('repeatability evidence binding mismatch')
                plan = self._measurement_plan_for_id(
                    plans,
                    measurement_id,
                    record.candidate_set_sha256,
                )
                if plan is None or plan.applied_scene_revision_id != check.scene_revision_id:
                    raise ValueError('repeatability measurement is not linked to an exact Measurement Plan')
                measurements.append((measurement_id, self._measurement_response(measurement_id)))
            rebuilt = build_repeatability_check(
                scene_revision_id=check.scene_revision_id,
                measurements=tuple(measurements),
                low_hz=check.requested_band_hz[0],
                high_hz=check.requested_band_hz[1],
                reference_band_hz=check.reference_band_hz,
            )
            if rebuilt != check:
                raise ValueError('repeatability check does not match immutable measurement evidence')

        for check in record.separation_checks:
            for candidate_id, measurement_id in (
                (check.candidate_a_id, check.measurement_a_id),
                (check.candidate_b_id, check.measurement_b_id),
            ):
                measurement = self.measurement_repository.get_measurement(measurement_id)
                if (
                    measurement is None
                    or measurement.evidence_type != 'measured'
                    or measurement.document_id != record.document_id
                ):
                    raise ValueError('candidate separation measurement binding mismatch')
                if not self._plan_linked(
                    plans,
                    candidate_id=candidate_id,
                    measurement_id=measurement_id,
                    candidate_set_sha256=record.candidate_set_sha256,
                ):
                    raise ValueError('candidate separation measurement is not linked to candidate plan')
            rebuilt = build_candidate_separation_check(
                candidate_a_id=check.candidate_a_id,
                candidate_b_id=check.candidate_b_id,
                measurement_a_id=check.measurement_a_id,
                measurement_b_id=check.measurement_b_id,
                response_a=self._measurement_response(check.measurement_a_id),
                response_b=self._measurement_response(check.measurement_b_id),
                low_hz=check.requested_band_hz[0],
                high_hz=check.requested_band_hz[1],
                repeatability_floor_db=check.repeatability_floor_db,
                min_repeatability_multiple=check.min_repeatability_multiple,
            )
            if rebuilt != check:
                raise ValueError('candidate separation check does not match measurement evidence')

    def _evidence_measurement_ids(
        self,
        record: CadModelValidationRecord,
    ) -> tuple[str, ...]:
        """Every measurement whose exact evidence the record depends on."""
        measurement_ids = {pair.measurement_id for pair in record.pairs}
        for check in record.repeatability_checks:
            measurement_ids.update(check.measurement_ids)
        for check in record.separation_checks:
            measurement_ids.update((check.measurement_a_id, check.measurement_b_id))
        if self.objective_repository is not None:
            for sample in record.objective_samples:
                evaluation = self.objective_repository.get_evaluation(sample.measured_evaluation_id)
                if evaluation is not None:
                    measurement_ids.update(
                        ref.source_id
                        for ref in evaluation.input_refs
                        if ref.evidence_class == 'measured' and ref.source_kind == 'cad_measurement'
                    )
        return tuple(sorted(measurement_ids))

    def _validate_measurement_lifecycle(
        self,
        record: CadModelValidationRecord,
    ) -> None:
        """Every bound measurement must currently be eligible (#509/#839).

        The check runs inside the shared save/read authority path: a record
        cannot be *created* over excluded/misassigned/test/duplicate
        evidence, and a record whose evidence was dispositioned *after* save
        fails re-attestation on authoritative reads — it stays inspectable
        via ``inspect``/``integrity_problems`` but can no longer authorize
        production work.
        """
        for measurement_id in self._evidence_measurement_ids(record):
            self._effective.require_normal_use(
                measurement_id, purpose='O60 model validation evidence'
            )

    def _validate_evidence_scope(self, record: CadModelValidationRecord) -> None:
        if record.evidence_scope != 'owned_room':
            return
        for measurement_id in self._evidence_measurement_ids(record):
            measurement = self.measurement_repository.get_measurement(measurement_id)
            if measurement is None:
                raise ValueError(f'owned-room validation references unknown measurement: {measurement_id}')
            try:
                provenance = json.loads(measurement.provenance_json)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError('owned-room measurement provenance is not valid JSON') from exc
            if not isinstance(provenance, dict) or provenance.get('validation_scope') != 'owned_room':
                raise ValueError(
                    'owned-room validation requires measurement provenance validation_scope=owned_room'
                )

    @staticmethod
    def _aware_timestamp(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else None

    def _validate_campaign_binding(self, record: CadModelValidationRecord, plans) -> None:
        if record.evidence_scope != 'owned_room':
            return
        if record.campaign_id is None or record.campaign_sha256 is None:
            raise ValueError('owned-room validation campaign binding is missing')

        campaign_repository = CadValidationCampaignRepository(
            self.search_repository,
            self.measurement_repository,
        )
        campaign = campaign_repository.get(record.campaign_id)
        if campaign is None:
            raise ValueError('owned-room validation campaign does not exist')
        if campaign.campaign_sha256 != record.campaign_sha256:
            raise ValueError('owned-room validation campaign hash mismatch')
        registration = campaign_repository.get_registration(record.campaign_id)
        if registration is None:
            raise ValueError('owned-room validation campaign registration is missing')
        if (
            record.campaign_registration_id != registration.registration_id
            or record.campaign_registration_sha256 != registration.registration_sha256
        ):
            raise ValueError('owned-room validation campaign registration mismatch')
        if (
            campaign.document_id != record.document_id
            or campaign.search_spec_id != record.search_spec_id
            or campaign.search_spec_sha256 != record.search_spec_sha256
            or campaign.candidate_set_sha256 != record.candidate_set_sha256
            or campaign.model_id != record.model_id
            or campaign.model_version != record.model_version
            or campaign.requested_band_hz != record.requested_band_hz
            or abs(campaign.max_holdout_rms_db - record.max_holdout_rms_db) > 1e-12
        ):
            raise ValueError('owned-room validation does not match preregistered campaign authority')

        expected_split = {
            item.candidate_id: item.split
            for item in campaign.candidates
        }
        pair_split = {pair.candidate_id: pair.split for pair in record.pairs}
        if pair_split != expected_split:
            raise ValueError('validation candidate split does not match preregistered campaign')

        objective_by_candidate: dict[str, set[str]] = {}
        for sample in record.objective_samples:
            objective_by_candidate.setdefault(sample.candidate_id, set()).add(sample.objective_id)
            predicted = (
                None
                if self.objective_repository is None
                else self.objective_repository.get_evaluation(sample.predicted_evaluation_id)
            )
            measured = (
                None
                if self.objective_repository is None
                else self.objective_repository.get_evaluation(sample.measured_evaluation_id)
            )
            if predicted is None or measured is None:
                raise ValueError('campaign objective evaluation does not exist')
            if (
                predicted.evaluation_spec_sha256
                != campaign.objective_evaluation_spec_sha256
                or measured.evaluation_spec_sha256
                != campaign.objective_evaluation_spec_sha256
            ):
                raise ValueError('objective evaluation spec does not match preregistered campaign')
        expected_objectives = set(campaign.objective_ids)
        if set(objective_by_candidate) != set(expected_split):
            raise ValueError('campaign objective evidence is missing a candidate')
        if any(values != expected_objectives for values in objective_by_candidate.values()):
            raise ValueError('campaign objective ids do not match preregistration')

        trend_by_objective = {check.objective_id: check for check in record.trend_checks}
        if set(trend_by_objective) != expected_objectives:
            raise ValueError('campaign trend checks do not match preregistered objectives')
        for objective_id, check in trend_by_objective.items():
            expected_tolerance = campaign.trend_tolerance_by_objective.get(objective_id, 0.0)
            if (
                abs(check.tie_tolerance - expected_tolerance) > 1e-12
                or check.min_comparable_pairs != campaign.trend_min_comparable_pairs
                or abs(check.min_agreement_ratio - campaign.trend_min_agreement_ratio) > 1e-12
            ):
                raise ValueError('campaign trend threshold mismatch')

        sensitivity_by_key = {
            (
                check.objective_id,
                tuple(sorted((check.candidate_a_id, check.candidate_b_id))),
            ): check
            for check in record.sensitivity_checks
        }
        expected_sensitivity = {
            (
                requirement.objective_id,
                tuple(sorted((requirement.candidate_a_id, requirement.candidate_b_id))),
            ): requirement
            for requirement in campaign.sensitivity
        }
        if set(sensitivity_by_key) != set(expected_sensitivity):
            raise ValueError('campaign sensitivity checks do not match preregistration')
        for key, requirement in expected_sensitivity.items():
            check = sensitivity_by_key[key]
            if (
                abs(
                    check.max_observed_sensitivity_per_m
                    - requirement.max_observed_sensitivity_per_m
                ) > 1e-12
                or abs(check.max_model_error_per_m - requirement.max_model_error_per_m)
                > 1e-12
            ):
                raise ValueError('campaign sensitivity threshold mismatch')

        measured_plans = [
            plan
            for plan in plans
            if plan.status == 'measured'
            and plan.candidate_set_sha256 == campaign.candidate_set_sha256
        ]
        plan_by_measurement = {
            measurement_id: plan
            for plan in measured_plans
            for measurement_id in plan.measurement_ids
        }
        repeatability_by_candidate = {}
        for check in record.repeatability_checks:
            candidate_ids = {
                plan_by_measurement[measurement_id].candidate_id
                for measurement_id in check.measurement_ids
                if measurement_id in plan_by_measurement
            }
            if len(candidate_ids) != 1:
                raise ValueError('campaign repeatability measurements do not map to one candidate')
            candidate_id = next(iter(candidate_ids))
            if candidate_id in repeatability_by_candidate:
                raise ValueError('campaign repeatability candidate has duplicate checks')
            repeatability_by_candidate[candidate_id] = check

        expected_repeatability = {
            requirement.candidate_id: requirement
            for requirement in campaign.repeatability
        }
        if set(repeatability_by_candidate) != set(expected_repeatability):
            raise ValueError('campaign repeatability checks do not match preregistration')
        for candidate_id, requirement in expected_repeatability.items():
            check = repeatability_by_candidate[candidate_id]
            if (
                len(check.measurement_ids) < requirement.min_measurements
                or check.reference_band_hz != requirement.reference_band_hz
                or check.requested_band_hz != campaign.requested_band_hz
            ):
                raise ValueError('campaign repeatability requirement mismatch')

        separation_by_key = {
            tuple(sorted((check.candidate_a_id, check.candidate_b_id))): check
            for check in record.separation_checks
        }
        expected_separation = {
            tuple(sorted((requirement.candidate_a_id, requirement.candidate_b_id))): requirement
            for requirement in campaign.separation
        }
        if set(separation_by_key) != set(expected_separation):
            raise ValueError('campaign candidate separation checks do not match preregistration')
        for key, requirement in expected_separation.items():
            check = separation_by_key[key]
            repeatability = repeatability_by_candidate[requirement.repeatability_candidate_id]
            if (
                abs(check.min_repeatability_multiple - requirement.min_repeatability_multiple)
                > 1e-12
                or abs(check.repeatability_floor_db - repeatability.rms_floor_db) > 1e-12
                or check.requested_band_hz != campaign.requested_band_hz
            ):
                raise ValueError('campaign candidate separation requirement mismatch')

        codes = {check.code for check in record.applicability_checks}
        if codes != set(campaign.required_applicability_codes):
            raise ValueError('campaign applicability checks do not match preregistration')

        campaign_time = self._aware_timestamp(registration.registered_at_utc)
        if campaign_time is None:
            raise ValueError('campaign registration timestamp must be timezone-aware')
        measurement_ids = {pair.measurement_id for pair in record.pairs}
        for check in record.repeatability_checks:
            measurement_ids.update(check.measurement_ids)
        for measurement_id in measurement_ids:
            measurement = self.measurement_repository.get_measurement(measurement_id)
            captured = None if measurement is None else self._aware_timestamp(measurement.captured_at)
            if captured is None or captured < campaign_time:
                raise ValueError(
                    'owned-room campaign measurement must be captured after preregistration'
                )
            try:
                provenance = json.loads(measurement.provenance_json)
            except (AttributeError, json.JSONDecodeError):
                provenance = None
            if (
                not isinstance(provenance, dict)
                or provenance.get('validation_scope') != 'owned_room'
                or provenance.get('validation_campaign_id') != campaign.campaign_id
            ):
                raise ValueError(
                    'owned-room measurement does not match preregistered campaign provenance'
                )

    def _validate_residual_authority(self, record: CadModelValidationRecord, plans) -> None:
        """Re-derive residual pairs and aggregate RMS from exact evidence.

        Each pair must reference a completed Room Simulator attempt and a
        measured MeasurementPlan dataset. The persisted ``rms_difference_db``,
        ``shape_rms_db``, ``calibration_rms_db``, ``holdout_rms_db`` and
        ``residual_gate`` values are caller-supplied claims, so they are
        recomputed here from the resolved prediction/measurement responses via
        the canonical residual builder and must match exactly.
        """
        samples = []
        for pair in record.pairs:
            attempt = self.roomsim_repository.get_attempt(pair.prediction_source_id)
            if attempt is None or attempt.status != 'completed':
                raise ValueError('validation prediction must reference a completed Room Simulator attempt')
            if attempt.candidate_id != pair.candidate_id:
                raise ValueError('validation prediction candidate mismatch')
            batch = self.roomsim_repository.get_batch_spec(attempt.batch_run_id)
            if batch is None:
                raise ValueError('validation prediction batch does not exist')
            if (
                batch.document_id != record.document_id
                or batch.search_spec_id != record.search_spec_id
                or batch.search_spec_sha256 != record.search_spec_sha256
                or batch.candidate_set_sha256 != record.candidate_set_sha256
                or batch.model_id != record.model_id
            ):
                raise ValueError('validation prediction batch authority mismatch')
            if attempt.result is None or attempt.result.model_version != record.model_version:
                raise ValueError('validation prediction model version mismatch')

            measurement = self.measurement_repository.get_measurement(pair.measurement_id)
            if measurement is None or measurement.evidence_type != 'measured':
                raise ValueError('validation measurement must reference measured evidence')
            if not self._plan_linked(
                plans,
                candidate_id=pair.candidate_id,
                measurement_id=pair.measurement_id,
                candidate_set_sha256=record.candidate_set_sha256,
            ):
                raise ValueError('validation measurement is not linked to the candidate Measurement Plan')
            samples.append((
                pair.candidate_id,
                pair.split,
                pair.prediction_source_id,
                pair.measurement_id,
                roomsim_attempt_frequency_response(attempt),
                self._measurement_response(pair.measurement_id),
            ))

        expected = recompute_residual_payload(record, tuple(samples))
        if (
            expected['pairs'] != record.pairs
            or expected['calibration_rms_db'] != record.calibration_rms_db
            or expected['holdout_rms_db'] != record.holdout_rms_db
            or expected['residual_gate'] != record.residual_gate
        ):
            raise ValueError(
                'model validation residuals do not match prediction/measurement evidence'
            )

    @staticmethod
    def _scoped_measurement_ids(record: CadModelValidationRecord) -> tuple[str, ...]:
        ids = {pair.measurement_id for pair in record.pairs}
        for check in record.repeatability_checks:
            ids.update(check.measurement_ids)
        for check in record.separation_checks:
            ids.update((check.measurement_a_id, check.measurement_b_id))
        return tuple(sorted(ids))

    def _validate_applicability_authority(
        self,
        record: CadModelValidationRecord,
        spec,
    ) -> None:
        """Re-derive every applicability decision from exact evidence authority.

        A persisted ``passed`` claim is never trusted: each check is rebuilt
        through its registered evaluator from the resolved SceneRevision /
        SearchSpec / prediction-batch / Measurement Plan / attestation
        authorities and must match exactly, so fabricated, foreign or tampered
        claims fail closed.
        """
        if not record.applicability_checks:
            return
        context = resolve_applicability_context(
            document_id=record.document_id,
            search_spec_id=record.search_spec_id,
            search_spec_sha256=record.search_spec_sha256,
            candidate_set_sha256=record.candidate_set_sha256,
            model_id=record.model_id,
            model_version=record.model_version,
            evidence_scope=record.evidence_scope,
            campaign_id=record.campaign_id,
            requested_band_hz=record.requested_band_hz,
            pair_attempt_ids=tuple(
                pair.prediction_source_id for pair in record.pairs
            ),
            pair_measurement_ids=tuple(
                pair.measurement_id for pair in record.pairs
            ),
            scoped_measurement_ids=self._scoped_measurement_ids(record),
            search_repository=self.search_repository,
            roomsim_repository=self.roomsim_repository,
            measurement_repository=self.measurement_repository,
            attestation_repository=self.applicability_attestations,
        )
        for check in record.applicability_checks:
            try:
                expected = rederive_applicability_check(context, check)
            except ValueError as exc:
                raise ValueError(
                    f'applicability {check.code} authority does not resolve: {exc}'
                ) from exc
            if expected != check:
                raise ValueError(
                    'applicability check does not match evidence authority: '
                    f'{check.code}'
                )

    def _validate_raw_asset_authority(self, record: CadModelValidationRecord) -> None:
        """Fail closed when file-backed raw measurement evidence is unavailable.

        Every measurement the record depends on — residual pairs,
        repeatability/separation checks and measured O30 inputs — must still
        resolve its bound frequency-response dataset. For the native
        ``CadMeasurementRepository`` the dataset read itself re-verifies the
        content-addressed raw asset hash and pinned importer replay, and
        ``verify_measurement_asset_authority`` additionally re-checks the
        raw-asset registry size; other evidence backends at minimum must
        still resolve the bound dataset.
        """
        verify = getattr(
            self.measurement_repository, 'verify_measurement_asset_authority', None
        )
        for measurement_id in self._evidence_measurement_ids(record):
            if verify is not None:
                verify(measurement_id)
            elif (
                self.measurement_repository.dataset_for_measurement(measurement_id)
                is None
            ):
                raise ValueError(
                    'validation measurement has no frequency response: '
                    f'{measurement_id}'
                )

    def _validate_record(
        self,
        record: CadModelValidationRecord,
        *,
        production_read: bool = False,
    ) -> CadModelValidationRecord:
        """Replay the complete save-time cross-evidence authority for *record*.

        One shared authority path for save and every authoritative read so
        the two cannot drift: SearchSpec authority, Room Simulator
        attempt/batch existence and exact binding, residual response
        reconstruction, O30 predicted/measured objective authority,
        sensitivity, repeatability and candidate-separation reconstruction,
        owned-room measurement provenance and campaign binding,
        applicability authority, and raw measurement asset presence, size
        and hash.

        With ``production_read=True`` — a persisted record being
        re-attested before it may authorize production work — any evidence
        failure is raised as :class:`CadModelValidationIntegrityError` so a
        formerly valid record whose evidence went stale is a typed
        integrity failure rather than an ambiguous input error; the row
        itself is never reclassified.
        """
        try:
            spec = self.search_repository.get(record.search_spec_id)
            if spec is None:
                raise ValueError('model validation SearchSpec does not exist')
            if (
                spec.document_id != record.document_id
                or spec.search_spec_sha256 != record.search_spec_sha256
            ):
                raise ValueError('model validation SearchSpec authority mismatch')
            plans = self.measurement_repository.list_measurement_plans(
                record.search_spec_id
            )
            self._validate_residual_authority(record, plans)
            self._validate_objective_samples(record)
            self._validate_sensitivity(record, spec)
            self._validate_repeatability_and_separation(record, plans)
            self._validate_measurement_lifecycle(record)
            self._validate_evidence_scope(record)
            self._validate_campaign_binding(record, plans)
            self._validate_applicability_authority(record, spec)
            self._validate_raw_asset_authority(record)
        except CadModelValidationIntegrityError:
            raise
        except ValueError as exc:
            if production_read:
                raise CadModelValidationIntegrityError(
                    record.validation_id,
                    str(exc),
                ) from exc
            raise
        return record

    @staticmethod
    def _persisted_record(row: sqlite3.Row) -> CadModelValidationRecord:
        """Deserialize a persisted payload, failing closed on a broken seal."""
        try:
            return CadModelValidationRecord.model_validate_json(row['payload_json'])
        except ValueError as exc:
            raise CadModelValidationIntegrityError(
                str(row['validation_id']),
                'persisted payload is not a sealed model validation record',
            ) from exc

    def save(self, record: CadModelValidationRecord) -> None:
        if not isinstance(record, CadModelValidationRecord):
            raise TypeError('record must be CadModelValidationRecord')
        record = CadModelValidationRecord.model_validate(record.model_dump(mode='python'))
        self._validate_record(record)

        with closing(self._connect()) as connection, connection:
            connection.execute(
                '''INSERT INTO cad_model_validations(
                    validation_id, document_id, search_spec_id, model_id, model_version,
                    recommendation_gate, validation_sha256, payload_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    record.validation_id,
                    record.document_id,
                    record.search_spec_id,
                    record.model_id,
                    record.model_version,
                    record.recommendation_gate,
                    record.validation_sha256,
                    record.model_dump_json(),
                    record.created_at_utc,
                ),
            )

    def get(self, validation_id: str) -> CadModelValidationRecord | None:
        """Authoritative read: full save-time evidence re-attestation.

        A persisted record whose exact source evidence disappeared or
        changed since save fails closed with
        :class:`CadModelValidationIntegrityError` instead of silently
        serving stale authority. Use :meth:`inspect` for a diagnostic-only
        view of stale or suspect history.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT validation_id, payload_json FROM cad_model_validations '
                'WHERE validation_id=?',
                (validation_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate_record(
            self._persisted_record(row),
            production_read=True,
        )

    def inspect(self, validation_id: str) -> CadModelValidationRecord | None:
        """Return the persisted payload without evidence re-attestation.

        Diagnostic/history view for stale or suspect records: the model is
        payload-consistent (self-hash verified) but its external evidence
        has NOT been re-attested, so the result must never authorize
        O70/O80 production work.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT validation_id, payload_json FROM cad_model_validations '
                'WHERE validation_id=?',
                (validation_id,),
            ).fetchone()
        return None if row is None else self._persisted_record(row)

    def latest_eligible_for_search_spec(
        self,
        search_spec_id: str,
    ) -> CadModelValidationRecord | None:
        """Authoritative O70-entry read: full evidence re-attestation.

        The newest eligible record is re-validated through the same
        ``_validate_record`` authority path used at save — including
        file-backed raw asset presence/size/hash — so a record whose
        evidence went stale after save fails closed with
        :class:`CadModelValidationIntegrityError` instead of being
        silently reclassified or skipped.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT validation_id, payload_json FROM cad_model_validations "
                "WHERE search_spec_id=? AND recommendation_gate='eligible' "
                "ORDER BY seq DESC LIMIT 1",
                (search_spec_id,),
            ).fetchone()
        if row is None:
            return None
        record = self._persisted_record(row)
        if record.recommendation_gate != 'eligible':
            raise CadModelValidationIntegrityError(
                record.validation_id,
                'eligible validation record payload is not eligible',
            )
        if record.evidence_scope != 'owned_room':
            raise CadModelValidationIntegrityError(
                record.validation_id,
                'eligible validation record is not owned-room evidence',
            )
        return self._validate_record(record, production_read=True)

    def list_for_search_spec(self, search_spec_id: str) -> tuple[CadModelValidationRecord, ...]:
        """Authoritative history: every record re-attested, fail closed.

        A stale or tampered record raises
        :class:`CadModelValidationIntegrityError`; use
        :meth:`inspect_for_search_spec` to browse history without
        re-attestation.
        """
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT validation_id, payload_json FROM cad_model_validations '
                'WHERE search_spec_id=? ORDER BY seq ASC',
                (search_spec_id,),
            ).fetchall()
        records = tuple(self._persisted_record(row) for row in rows)
        for record in records:
            self._validate_record(record, production_read=True)
        return records

    def inspect_for_search_spec(
        self,
        search_spec_id: str,
    ) -> tuple[CadModelValidationRecord, ...]:
        """History listing without evidence re-attestation (see :meth:`inspect`)."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT validation_id, payload_json FROM cad_model_validations '
                'WHERE search_spec_id=? ORDER BY seq ASC',
                (search_spec_id,),
            ).fetchall()
        return tuple(self._persisted_record(row) for row in rows)

    def integrity_problems(self, search_spec_id: str | None = None) -> list[str]:
        """Scan persisted O60 records and report evidence-integrity problems.

        Native integrity scan: every persisted payload is deserialized
        through the sealed model and re-attested through the same
        ``_validate_record`` authority path used by save and the production
        reads. Problems are reported as strings instead of raising so a
        stale or tampered record stays inspectable; a clean scan returns an
        empty list.
        """
        sql = (
            'SELECT validation_id, payload_json FROM cad_model_validations'
        )
        params: tuple[str, ...] = ()
        if search_spec_id is not None:
            sql += ' WHERE search_spec_id=?'
            params = (search_spec_id,)
        sql += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, params).fetchall()
        problems: list[str] = []
        for row in rows:
            validation_id = str(row['validation_id'])
            try:
                record = CadModelValidationRecord.model_validate_json(
                    row['payload_json']
                )
                self._validate_record(record)
            except ValueError as exc:
                problems.append(
                    f'model_validation_integrity:{validation_id}:{exc}'
                )
        return problems
