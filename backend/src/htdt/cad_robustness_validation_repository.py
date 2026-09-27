from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Sequence

from .cad_measurement_loop import CadMeasurementPlan
from .cad_measurement_quality import (
    CadMeasurementQualityReport,
    dataset_sha256,
    gate_measurement_claim,
    measurement_sha256,
)
from .cad_measurement_quality_repository import CadMeasurementQualityRepository
from .cad_measurement_repository import CadMeasurementRepository
from .cad_model_validation import CadModelValidationRecord
from .cad_model_validation_repository import CadModelValidationRepository
from .cad_robustness_repository import CadRobustnessRepository
from .cad_roomsim_repository import CadRoomSimRepository
from .cad_schema import (
    check_native_schema_compatibility,
    require_native_tables,
    connect_sqlite,

)
from .cad_system_variant_repository import CadSystemVariantRepository
from .cad_validation_campaign_repository import CadValidationCampaignRepository
from .optimization_robustness import RobustnessSpec
from .optimization_robustness_validation import (
    O90EAxisCoverage,
    O90ECaseAssessment,
    O90EMeasurementEvidenceRef,
    O90EPredictionAuthorityRef,
    O90EReason,
    O90ESupportState,
    O90ESystemVariantAuthorityRef,
    O90EValidationCase,
    O90EValidationDecision,
    build_o90e_decision,
    build_o90e_validation_case,
    matching_o60_sensitivity,
    sensitivity_evidence_sha256,
)


def _utc_now() -> str:
    """Repository commit clock; the only source of durable registration time."""
    return datetime.now(timezone.utc).isoformat()


class CadRobustnessValidationRepository:
    """Append-only O90E authority over existing O90/O60/N60 evidence.

    This repository deliberately does not own a second model-validation flag.
    Every production decision re-resolves the exact existing authorities and,
    on save and on every authoritative read, must reproduce the canonical
    ``evaluate_decision(...)`` output over those authorities exactly; anything
    less fails closed.
    """

    def __init__(
        self,
        *,
        robustness_repository: CadRobustnessRepository,
        model_validation_repository: CadModelValidationRepository,
        campaign_repository: CadValidationCampaignRepository,
        measurement_repository: CadMeasurementRepository,
        quality_repository: CadMeasurementQualityRepository,
        roomsim_repository: CadRoomSimRepository,
    ) -> None:
        self.robustness_repository = robustness_repository
        self.model_validation_repository = model_validation_repository
        self.campaign_repository = campaign_repository
        self.measurement_repository = measurement_repository
        self.quality_repository = quality_repository
        self.roomsim_repository = roomsim_repository
        self.system_variant_repository = CadSystemVariantRepository(
            measurement_repository.scene_repository
        )

        self.path = Path(measurement_repository.path)
        paths = (
            Path(robustness_repository.db_path),
            Path(model_validation_repository.path),
            Path(campaign_repository.path),
            Path(quality_repository.path),
            Path(roomsim_repository.path),
        )
        if any(path != self.path for path in paths):
            raise ValueError('O90E repositories must share one native CAD database')
        check_native_schema_compatibility(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_robustness_validation_cases', 'cad_robustness_validation_decisions')

    @staticmethod
    def _aware_timestamp(value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except (AttributeError, ValueError):
            return None
        return parsed if parsed.tzinfo is not None else None

    def _spec(self, robustness_spec_id: str) -> RobustnessSpec:
        try:
            return self.robustness_repository.get_spec(robustness_spec_id)
        except KeyError as exc:
            raise ValueError('O90E RobustnessSpec does not exist') from exc

    def _plan_history(
        self,
        *,
        search_spec_id: str,
        plan_id: str,
    ) -> tuple[CadMeasurementPlan, ...]:
        return tuple(
            plan
            for plan in self.measurement_repository.list_measurement_plans(
                search_spec_id
            )
            if plan.plan_id == plan_id
        )

    def _plan_by_hash(
        self,
        *,
        search_spec_id: str,
        plan_id: str,
        plan_sha256: str,
    ) -> CadMeasurementPlan:
        matches = tuple(
            plan
            for plan in self._plan_history(
                search_spec_id=search_spec_id,
                plan_id=plan_id,
            )
            if plan.plan_sha256 == plan_sha256
        )
        if len(matches) != 1:
            raise ValueError('O90E preregistered MeasurementPlan binding is unavailable')
        return matches[0]

    def _latest_plan(
        self,
        *,
        search_spec_id: str,
        plan_id: str,
    ) -> CadMeasurementPlan | None:
        history = self._plan_history(
            search_spec_id=search_spec_id,
            plan_id=plan_id,
        )
        return history[-1] if history else None

    def _raw_asset_valid(self, digest: str) -> bool:
        # The O90E evidence gate deliberately reuses the N60 managed
        # raw-asset contract instead of implementing its own file checks:
        # the asset row must resolve under the managed assets directory to a
        # regular file of the stored size whose SHA-256 equals the digest.
        try:
            self.measurement_repository.validate_raw_asset(digest)
        except (ValueError, OSError):
            return False
        return True

    def _system_variant_ref(
        self,
        revision_id: str,
    ) -> O90ESystemVariantAuthorityRef | None:
        lineage = self.system_variant_repository.proposal_lineage_for_revision(
            revision_id
        )
        if lineage is None:
            return None
        application, variant = lineage
        if (
            application.variant_id != variant.variant_id
            or application.variant_sha256 != variant.variant_sha256
        ):
            raise ValueError('SystemVariant application/variant authority mismatch')
        return O90ESystemVariantAuthorityRef(
            application_id=application.application_id,
            application_sha256=application.application_sha256,
            variant_id=variant.variant_id,
            variant_sha256=variant.variant_sha256,
        )

    def preregister_case(
        self,
        *,
        robustness_spec_id: str,
        axis_id: str,
        direction: str,
        nominal_measurement_plan_id: str,
        perturbation_measurement_plan_id: str,
        o60_campaign_id: str,
        observable_id: str,
        receiver_entity_id: str,
        required_capability: str,
        channel_role: str,
        source_speaker_ids: Sequence[str],
        radiation_scope: str,
    ) -> O90EValidationCase:
        """Resolve and durably register one O90E validation case.

        The durable ``preregistered_at_utc`` is repository-generated inside
        the same ``BEGIN IMMEDIATE`` write transaction that re-verifies the
        preregistered plan heads and inserts the case row, so a prospective
        case can never commit after a racing measurement completion and its
        timestamp can never claim a durable registration that has not
        committed yet.
        """
        spec = self._spec(robustness_spec_id)
        campaign = self.campaign_repository.get(o60_campaign_id)
        if campaign is None:
            raise ValueError('O90E preregistration requires an existing O60 campaign')
        registration = self.campaign_repository.get_registration(o60_campaign_id)
        if registration is None:
            raise ValueError(
                'O90E preregistration requires a durable O60 campaign registration'
            )

        nominal_plan = self._latest_plan(
            search_spec_id=spec.search_spec_id,
            plan_id=nominal_measurement_plan_id,
        )
        perturbation_plan = self._latest_plan(
            search_spec_id=spec.search_spec_id,
            plan_id=perturbation_measurement_plan_id,
        )
        if nominal_plan is None or perturbation_plan is None:
            raise ValueError('O90E preregistration MeasurementPlan is missing')

        nominal_revision = self.measurement_repository.scene_repository.get(
            nominal_plan.applied_scene_revision_id
        )
        perturbation_revision = self.measurement_repository.scene_repository.get(
            perturbation_plan.applied_scene_revision_id
        )
        if nominal_revision is None or perturbation_revision is None:
            raise ValueError('O90E MeasurementPlan SceneRevision is missing')

        nominal_variant = self._system_variant_ref(
            nominal_plan.applied_scene_revision_id
        )
        perturbation_variant = self._system_variant_ref(
            perturbation_plan.applied_scene_revision_id
        )
        if nominal_variant != perturbation_variant:
            raise ValueError(
                'O90E nominal/perturbation SystemVariant lineage mismatch'
            )

        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            case = build_o90e_validation_case(
                spec=spec,
                axis_id=axis_id,
                direction=direction,
                nominal_plan=nominal_plan,
                perturbation_plan=perturbation_plan,
                nominal_revision=nominal_revision,
                perturbation_revision=perturbation_revision,
                campaign=campaign,
                campaign_registration=registration,
                observable_id=observable_id,
                receiver_entity_id=receiver_entity_id,
                required_capability=required_capability,
                channel_role=channel_role,
                source_speaker_ids=source_speaker_ids,
                radiation_scope=radiation_scope,
                preregistered_at_utc=_utc_now(),
                system_variant=nominal_variant,
            )
            # Re-validate the case against the frozen committed state inside
            # the write transaction; the pre-resolved spec is passed through
            # because the robustness repository's schema gate takes its own
            # write lock on connect.
            self._validate_case_bindings(case, spec=spec)
            if case.preregistration_status == 'prospective':
                self._require_prospective_plan_state(
                    connection,
                    search_spec_id=spec.search_spec_id,
                    case=case,
                )
            self._insert_case(connection, case)
            connection.commit()
        return case

    def _validate_case_bindings(
        self,
        case: O90EValidationCase,
        *,
        spec: RobustnessSpec | None = None,
    ) -> None:
        if spec is None:
            spec = self._spec(case.robustness_spec_id)
        if spec.robustness_spec_sha256 != case.robustness_spec_sha256:
            raise ValueError('O90E case RobustnessSpec hash mismatch')
        if (
            spec.document_id != case.document_id
            or spec.scene_revision_id != case.scene_revision_id
            or spec.scene_content_hash != case.scene_content_hash
        ):
            raise ValueError('O90E case SceneRevision binding mismatch')
        source_revision = self.measurement_repository.scene_repository.get(
            spec.scene_revision_id
        )
        if (
            source_revision is None
            or source_revision.document_id != spec.document_id
            or source_revision.content_hash != spec.scene_content_hash
        ):
            raise ValueError('O90E source SceneRevision authority is unavailable or stale')
        if (
            spec.candidate_id != case.candidate_id
            or spec.candidate_sha256 != case.candidate_sha256
        ):
            raise ValueError('O90E case candidate binding mismatch')

        campaign = self.campaign_repository.get(case.o60_campaign_id)
        if campaign is None or campaign.campaign_sha256 != case.o60_campaign_sha256:
            raise ValueError('O90E case O60 campaign binding mismatch')
        registration = self.campaign_repository.get_registration(case.o60_campaign_id)
        if registration is None:
            raise ValueError('O90E case O60 campaign registration is missing')

        nominal_plan = self._plan_by_hash(
            search_spec_id=spec.search_spec_id,
            plan_id=case.nominal_measurement_plan_id,
            plan_sha256=case.nominal_preregistered_plan_sha256,
        )
        perturbation_plan = self._plan_by_hash(
            search_spec_id=spec.search_spec_id,
            plan_id=case.perturbation_measurement_plan_id,
            plan_sha256=case.perturbation_preregistered_plan_sha256,
        )
        nominal_revision = self.measurement_repository.scene_repository.get(
            nominal_plan.applied_scene_revision_id
        )
        perturbation_revision = self.measurement_repository.scene_repository.get(
            perturbation_plan.applied_scene_revision_id
        )
        if nominal_revision is None or perturbation_revision is None:
            raise ValueError('O90E case MeasurementPlan SceneRevision is missing')

        nominal_variant = self._system_variant_ref(
            nominal_plan.applied_scene_revision_id
        )
        perturbation_variant = self._system_variant_ref(
            perturbation_plan.applied_scene_revision_id
        )
        if nominal_variant != perturbation_variant:
            raise ValueError('O90E case SystemVariant lineage is no longer exact')
        if nominal_variant != case.system_variant:
            raise ValueError('O90E case SystemVariant binding mismatch')

        rebuilt = build_o90e_validation_case(
            spec=spec,
            axis_id=case.axis_id,
            direction=case.direction,
            nominal_plan=nominal_plan,
            perturbation_plan=perturbation_plan,
            nominal_revision=nominal_revision,
            perturbation_revision=perturbation_revision,
            campaign=campaign,
            campaign_registration=registration,
            observable_id=case.observable_id,
            receiver_entity_id=case.receiver_entity_id,
            required_capability=case.required_capability,
            channel_role=case.channel_role,
            source_speaker_ids=case.source_speaker_ids,
            radiation_scope=case.radiation_scope,
            preregistered_at_utc=case.preregistered_at_utc,
            system_variant=case.system_variant,
        )
        if rebuilt != case:
            raise ValueError('O90E case no longer matches exact preregistered authority')

    def _plan_head(
        self,
        connection: sqlite3.Connection,
        *,
        search_spec_id: str,
        plan_id: str,
    ) -> CadMeasurementPlan | None:
        """Latest persisted version of one plan, read on the write connection."""
        row = connection.execute(
            'SELECT status, plan_sha256, payload_json FROM cad_measurement_plans '
            'WHERE plan_id=? AND search_spec_id=? ORDER BY seq DESC LIMIT 1',
            (plan_id, search_spec_id),
        ).fetchone()
        if row is None:
            return None
        plan = CadMeasurementPlan.model_validate_json(row['payload_json'])
        if (
            plan.plan_id != plan_id
            or plan.search_spec_id != search_spec_id
            or plan.status != row['status']
            or plan.plan_sha256 != row['plan_sha256']
        ):
            raise ValueError(
                'persisted measurement plan head disagrees with its payload'
            )
        return plan

    def _require_prospective_plan_state(
        self,
        connection: sqlite3.Connection,
        *,
        search_spec_id: str,
        case: O90EValidationCase,
    ) -> None:
        """Re-resolve the exact plan heads under the write lock.

        A prospective case may only commit while both preregistered plan
        snapshots are still the exact planned heads of their plan_id
        lifecycles and no measured evidence already exists under their
        applied revisions. Running inside the same ``BEGIN IMMEDIATE``
        transaction as the case insert makes the gate and the durable
        registration atomic: a measurement completion that commits first is
        seen here, and one that commits after could not have influenced the
        check.
        """
        for plan_id, plan_sha256 in (
            (
                case.nominal_measurement_plan_id,
                case.nominal_preregistered_plan_sha256,
            ),
            (
                case.perturbation_measurement_plan_id,
                case.perturbation_preregistered_plan_sha256,
            ),
        ):
            head = self._plan_head(
                connection,
                search_spec_id=search_spec_id,
                plan_id=plan_id,
            )
            if head is None or head.status != 'planned':
                raise ValueError(
                    'prospective O90E case must be persisted before '
                    'measurement completion'
                )
            if head.plan_sha256 != plan_sha256:
                raise ValueError(
                    'O90E preregistered MeasurementPlan is no longer the '
                    'current planned head'
                )
            blocker = connection.execute(
                'SELECT measurement_id FROM cad_measurements '
                "WHERE document_id=? AND evidence_type='measured' "
                'AND scene_revision_id=? LIMIT 1',
                (head.document_id, head.applied_scene_revision_id),
            ).fetchone()
            if blocker is not None:
                raise ValueError(
                    'prospective O90E case cannot register over existing '
                    'qualifying measurement evidence'
                )

    def _insert_case(
        self,
        connection: sqlite3.Connection,
        case: O90EValidationCase,
    ) -> None:
        if connection.execute(
            'SELECT 1 FROM cad_robustness_validation_cases WHERE case_id=?',
            (case.case_id,),
        ).fetchone() is not None:
            raise ValueError(f'O90E validation case already exists: {case.case_id}')
        connection.execute(
            '''
            INSERT INTO cad_robustness_validation_cases(
                case_id, robustness_spec_id, candidate_id, axis_id, direction,
                case_sha256, preregistration_status, preregistered_at_utc,
                payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                case.case_id,
                case.robustness_spec_id,
                case.candidate_id,
                case.axis_id,
                case.direction,
                case.case_sha256,
                case.preregistration_status,
                case.preregistered_at_utc,
                case.model_dump_json(),
            ),
        )

    def save_case(self, case: O90EValidationCase) -> None:
        """Persist one fully-bound O90E validation case.

        For a prospective case the planned-head gate and the insert share
        one ``BEGIN IMMEDIATE`` transaction, so a measurement completion
        cannot commit between the check and the durable registration.
        """
        case = O90EValidationCase.model_validate(case.model_dump(mode='python'))
        self._validate_case_bindings(case)
        search_spec_id = self._spec(case.robustness_spec_id).search_spec_id
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if case.preregistration_status == 'prospective':
                self._require_prospective_plan_state(
                    connection,
                    search_spec_id=search_spec_id,
                    case=case,
                )
            self._insert_case(connection, case)
            connection.commit()

    def get_case(self, case_id: str) -> O90EValidationCase | None:
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_robustness_validation_cases WHERE case_id=?',
                (case_id,),
            ).fetchone()
        if row is None:
            return None
        case = O90EValidationCase.model_validate_json(row['payload_json'])
        self._validate_case_bindings(case)
        return case

    def list_cases(self, robustness_spec_id: str) -> tuple[O90EValidationCase, ...]:
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                '''
                SELECT payload_json
                FROM cad_robustness_validation_cases
                WHERE robustness_spec_id=?
                ORDER BY seq ASC
                ''',
                (robustness_spec_id,),
            ).fetchall()
        cases = tuple(
            O90EValidationCase.model_validate_json(row['payload_json'])
            for row in rows
        )
        for case in cases:
            self._validate_case_bindings(case)
        return cases

    @staticmethod
    def _pair_for_candidate(
        record: CadModelValidationRecord,
        candidate_id: str,
    ):
        matches = tuple(
            pair for pair in record.pairs if pair.candidate_id == candidate_id
        )
        return matches[0] if len(matches) == 1 else None

    def _prediction_ref(
        self,
        *,
        spec: RobustnessSpec,
        record: CadModelValidationRecord,
        candidate_id: str,
        role: str,
    ) -> tuple[O90EPredictionAuthorityRef | None, list[O90EReason]]:
        reasons: list[O90EReason] = []
        pair = self._pair_for_candidate(record, candidate_id)
        if pair is None:
            return None, ['stale_model_or_result']

        if role == 'nominal' and pair.prediction_source_id != spec.nominal_prediction_result_ref:
            reasons.append('stale_model_or_result')

        attempt = self.roomsim_repository.get_attempt(pair.prediction_source_id)
        if (
            attempt is None
            or attempt.status != 'completed'
            or attempt.result is None
            or attempt.candidate_id != candidate_id
            or attempt.result.model_version != spec.model_version
        ):
            return None, list(dict.fromkeys(reasons + ['stale_model_or_result']))
        batch = self.roomsim_repository.get_batch_spec(attempt.batch_run_id)
        if batch is None:
            return None, list(dict.fromkeys(reasons + ['stale_model_or_result']))
        if (
            batch.document_id != spec.document_id
            or batch.scene_revision_id != spec.scene_revision_id
            or batch.scene_content_hash != spec.scene_content_hash
        ):
            reasons.append('wrong_scene_revision')
        if (
            batch.search_spec_id != spec.search_spec_id
            or batch.search_spec_sha256 != spec.search_spec_sha256
            or batch.candidate_set_sha256 != spec.candidate_set_sha256
            or batch.model_id != spec.model_id
        ):
            reasons.append('stale_model_or_result')
        if spec.prediction_provider_id != batch.adapter_version:
            reasons.append('stale_model_or_result')

        ref = O90EPredictionAuthorityRef(
            role=role,
            candidate_id=candidate_id,
            prediction_attempt_id=attempt.attempt_id,
            prediction_attempt_sha256=attempt.attempt_sha256,
            batch_run_id=batch.batch_run_id,
            batch_spec_sha256=batch.batch_spec_sha256,
            binding_sha256=batch.binding_sha256,
            model_id=batch.model_id,
            model_version=attempt.result.model_version,
            adapter_version=batch.adapter_version,
        )
        return ref, list(dict.fromkeys(reasons))

    def _quality_failed(self, report: CadMeasurementQualityReport) -> bool:
        return any(
            check.status == 'FAIL'
            for check in (
                report.clipping,
                report.noise_snr,
                report.usable_frequency_band,
                report.timing_reference,
                report.polarity,
                report.ir_window,
                report.calibration,
                report.repeatability,
            )
        )

    def _measurement_ref(
        self,
        *,
        case: O90EValidationCase,
        record: CadModelValidationRecord,
        role: str,
        candidate_id: str,
        plan_id: str,
    ) -> tuple[O90EMeasurementEvidenceRef | None, list[O90EReason]]:
        reasons: list[O90EReason] = []
        pair = self._pair_for_candidate(record, candidate_id)
        if pair is None:
            return None, ['wrong_candidate', 'missing_measurement_evidence']

        plan = self._latest_plan(
            search_spec_id=record.search_spec_id,
            plan_id=plan_id,
        )
        if plan is None or plan.status != 'measured':
            return None, ['missing_measurement_evidence']
        if plan.candidate_id != candidate_id:
            return None, ['wrong_candidate']
        if pair.measurement_id not in plan.measurement_ids:
            return None, ['missing_measurement_evidence']

        measurement = self.measurement_repository.get_measurement(pair.measurement_id)
        dataset = self.measurement_repository.dataset_for_measurement(pair.measurement_id)
        if measurement is None or dataset is None:
            return None, ['missing_measurement_evidence']
        if not self._raw_asset_valid(dataset.source_sha256):
            return None, ['missing_measurement_evidence']
        if measurement.evidence_type != 'measured':
            reasons.append('synthetic_evidence')
        if (
            measurement.scene_revision_id != plan.applied_scene_revision_id
            or measurement.scene_content_hash != plan.applied_scene_content_hash
        ):
            reasons.append('wrong_scene_revision')
        measurement_variant = self._system_variant_ref(
            measurement.scene_revision_id
        )
        if measurement_variant != case.system_variant:
            reasons.append('wrong_system_variant')
        if (
            measurement.measurement_entity_id != case.receiver_entity_id
            or measurement.measurement_position != (
                case.nominal_receiver_position
                if role == 'nominal'
                else case.target_receiver_position
            )
            or measurement.channel_role != case.channel_role
            or tuple(sorted(measurement.source_speaker_ids))
            != case.source_speaker_ids
            or measurement.radiation_scope != case.radiation_scope
        ):
            reasons.append('perturbation_domain_outside_validated_applicability')

        try:
            provenance = json.loads(measurement.provenance_json)
        except json.JSONDecodeError:
            provenance = {}
        if (
            provenance.get('validation_scope') != 'owned_room'
            or provenance.get('validation_campaign_id') != case.o60_campaign_id
        ):
            reasons.append('synthetic_evidence')

        capture_time = (
            None
            if measurement.captured_at is None
            else self._aware_timestamp(measurement.captured_at)
        )
        prereg_time = self._aware_timestamp(case.preregistered_at_utc)
        if (
            case.preregistration_status != 'prospective'
            or capture_time is None
            or prereg_time is None
            or capture_time < prereg_time
        ):
            reasons.append('retrospective_evidence')

        report = self.quality_repository.latest_report(measurement.measurement_id)
        if report is None:
            return None, list(
                dict.fromkeys(reasons + ['insufficient_measurement_capability'])
            )

        capability = gate_measurement_claim(
            report,
            case.required_capability,
            required_band_hz=case.requested_band_hz,
        )
        if report.acquisition_context is None:
            reasons.append('insufficient_measurement_capability')
        if capability.decision != 'ALLOWED':
            reasons.append('insufficient_measurement_capability')
        if self._quality_failed(report):
            reasons.append('quality_failure')

        acquisition = report.acquisition_context
        ref = O90EMeasurementEvidenceRef(
            role=role,
            plan_id=plan.plan_id,
            completed_plan_sha256=plan.plan_sha256,
            candidate_id=candidate_id,
            measurement_id=measurement.measurement_id,
            measurement_sha256=measurement_sha256(measurement),
            dataset_id=dataset.dataset_id,
            dataset_sha256=dataset_sha256(dataset),
            raw_asset_sha256=dataset.source_sha256,
            quality_report_id=report.report_id,
            quality_report_sha256=report.report_sha256,
            acquisition_context_id=(
                None if acquisition is None else acquisition.acquisition_context_id
            ),
            acquisition_context_sha256=(
                None
                if acquisition is None
                else acquisition.acquisition_context_sha256
            ),
            capability_claim=case.required_capability,
            capability_decision=capability.decision,
            scene_revision_id=measurement.scene_revision_id,
            scene_content_hash=measurement.scene_content_hash,
            measurement_entity_id=measurement.measurement_entity_id,
            captured_at=measurement.captured_at,
            system_variant=measurement_variant,
        )
        return ref, list(dict.fromkeys(reasons))

    def _assessment(
        self,
        *,
        spec: RobustnessSpec,
        case: O90EValidationCase,
        record: CadModelValidationRecord | None,
    ) -> O90ECaseAssessment:
        reasons: list[O90EReason] = []
        sensitivity_sha = None
        nominal_measurement = None
        perturbation_measurement = None
        nominal_prediction = None
        perturbation_prediction = None

        if case.preregistration_status != 'prospective':
            reasons.append('retrospective_evidence')

        if record is None:
            reasons.append('missing_underlying_model_validation')
        else:
            record_candidate_ids = {
                pair.candidate_id for pair in record.pairs
            }
            if (
                case.candidate_id not in record_candidate_ids
                or case.perturbation_candidate_id not in record_candidate_ids
            ):
                reasons.append('wrong_candidate')
            if (
                record.document_id != spec.document_id
                or record.search_spec_id != spec.search_spec_id
                or record.search_spec_sha256 != spec.search_spec_sha256
                or record.candidate_set_sha256 != spec.candidate_set_sha256
            ):
                reasons.append('stale_model_or_result')
            if (
                record.model_id != spec.model_id
                or record.model_version != spec.model_version
            ):
                reasons.append('stale_model_or_result')
            if record.evidence_scope != 'owned_room':
                reasons.append('synthetic_evidence')
            if record.recommendation_gate != 'eligible':
                reasons.append('missing_underlying_model_validation')
            if (
                record.campaign_id != case.o60_campaign_id
                or record.campaign_sha256 != case.o60_campaign_sha256
            ):
                reasons.append('missing_underlying_model_validation')
            if record.requested_band_hz != case.requested_band_hz:
                reasons.append('wrong_observable_or_band')

            sensitivity = matching_o60_sensitivity(record, case)
            if sensitivity is None:
                reasons.append(
                    'perturbation_domain_outside_validated_applicability'
                )
            else:
                sensitivity_sha = sensitivity_evidence_sha256(sensitivity)
                if sensitivity.gate != 'pass':
                    reasons.append(
                        'perturbation_domain_outside_validated_applicability'
                    )
                if (
                    sensitivity.max_observed_sensitivity_per_m
                    != case.comparison_rule.max_observed_sensitivity_per_m
                    or sensitivity.max_model_error_per_m
                    != case.comparison_rule.max_model_error_per_m
                ):
                    reasons.append(
                        'perturbation_domain_outside_validated_applicability'
                    )

            nominal_prediction, prediction_reasons = self._prediction_ref(
                spec=spec,
                record=record,
                candidate_id=case.candidate_id,
                role='nominal',
            )
            reasons.extend(prediction_reasons)
            perturbation_prediction, prediction_reasons = self._prediction_ref(
                spec=spec,
                record=record,
                candidate_id=case.perturbation_candidate_id,
                role='perturbation',
            )
            reasons.extend(prediction_reasons)

            nominal_measurement, measurement_reasons = self._measurement_ref(
                case=case,
                record=record,
                role='nominal',
                candidate_id=case.candidate_id,
                plan_id=case.nominal_measurement_plan_id,
            )
            reasons.extend(measurement_reasons)
            perturbation_measurement, measurement_reasons = self._measurement_ref(
                case=case,
                record=record,
                role='perturbation',
                candidate_id=case.perturbation_candidate_id,
                plan_id=case.perturbation_measurement_plan_id,
            )
            reasons.extend(measurement_reasons)

        reasons = list(dict.fromkeys(reasons))
        status = (
            'supported'
            if not reasons
            else 'missing'
            if any(
                reason
                in {
                    'missing_underlying_model_validation',
                    'missing_measurement_evidence',
                    'missing_preregistration',
                }
                for reason in reasons
            )
            else 'unsupported'
        )
        return O90ECaseAssessment(
            case_id=case.case_id,
            case_sha256=case.case_sha256,
            axis_id=case.axis_id,
            direction=case.direction,
            target_delta=case.target_delta,
            status=status,
            reasons=tuple(reasons),
            sensitivity_evidence_sha256=sensitivity_sha,
            nominal_measurement=nominal_measurement,
            perturbation_measurement=perturbation_measurement,
            nominal_prediction=nominal_prediction,
            perturbation_prediction=perturbation_prediction,
        )

    def evaluate_decision(
        self,
        *,
        robustness_spec_id: str,
        o60_validation_id: str,
        case_ids: Sequence[str],
        decided_at_utc: str,
    ) -> O90EValidationDecision:
        spec = self._spec(robustness_spec_id)
        record = self.model_validation_repository.get(o60_validation_id)
        reasons: list[O90EReason] = []

        if record is None:
            reasons.append('missing_underlying_model_validation')
            validation_sha = None
            campaign_id = None
            campaign_sha = None
        else:
            validation_sha = record.validation_sha256
            campaign_id = record.campaign_id
            campaign_sha = record.campaign_sha256
            if (
                record.document_id != spec.document_id
                or record.search_spec_id != spec.search_spec_id
                or record.search_spec_sha256 != spec.search_spec_sha256
                or record.candidate_set_sha256 != spec.candidate_set_sha256
            ):
                reasons.append('stale_model_or_result')
            if record.model_id != spec.model_id or record.model_version != spec.model_version:
                reasons.append('stale_model_or_result')
            if record.evidence_scope != 'owned_room':
                reasons.append('synthetic_evidence')
            if record.recommendation_gate != 'eligible':
                reasons.append('missing_underlying_model_validation')

        cases: list[O90EValidationCase] = []
        for case_id in case_ids:
            case = self.get_case(case_id)
            if case is None:
                reasons.append('missing_preregistration')
                continue
            if case.robustness_spec_id != spec.robustness_spec_id:
                raise ValueError('O90E decision case belongs to another RobustnessSpec')
            cases.append(case)
        if len(case_ids) != len(set(case_ids)):
            raise ValueError('O90E decision case ids must be unique')

        by_key: dict[tuple[str, str], O90EValidationCase] = {}
        for case in cases:
            key = (case.axis_id, case.direction)
            if key in by_key:
                raise ValueError(
                    'O90E decision cannot cherry-pick duplicate axis/direction cases'
                )
            by_key[key] = case

        assessments = tuple(
            self._assessment(spec=spec, case=case, record=record)
            for case in cases
        )
        assessment_by_key = {
            (item.axis_id, item.direction): item for item in assessments
        }

        coverage: list[O90EAxisCoverage] = []
        for axis in spec.axes:
            minus_case = by_key.get((axis.axis_id, 'minus'))
            plus_case = by_key.get((axis.axis_id, 'plus'))
            minus_assessment = assessment_by_key.get((axis.axis_id, 'minus'))
            plus_assessment = assessment_by_key.get((axis.axis_id, 'plus'))

            minus_ok = (
                minus_case is not None
                and minus_assessment is not None
                and minus_assessment.status == 'supported'
                and abs(abs(minus_case.target_delta) - axis.minus_delta) <= 1e-9
            )
            plus_ok = (
                plus_case is not None
                and plus_assessment is not None
                and plus_assessment.status == 'supported'
                and abs(abs(plus_case.target_delta) - axis.plus_delta) <= 1e-9
            )
            state = 'full' if minus_ok and plus_ok else 'partial' if minus_ok or plus_ok else 'none'
            coverage.append(
                O90EAxisCoverage(
                    axis_id=axis.axis_id,
                    required_minus_delta=axis.minus_delta,
                    required_plus_delta=axis.plus_delta,
                    tested_minus_delta=(
                        None if minus_case is None else abs(minus_case.target_delta)
                    ),
                    tested_plus_delta=(
                        None if plus_case is None else abs(plus_case.target_delta)
                    ),
                    state=state,
                )
            )
            if minus_case is None or plus_case is None:
                reasons.append('missing_preregistration')
            if state != 'full':
                reasons.append('incomplete_required_perturbations')
            if not axis.parameter.endswith('_m'):
                reasons.append(
                    'perturbation_domain_outside_validated_applicability'
                )

        for assessment in assessments:
            reasons.extend(assessment.reasons)

        if record is not None:
            case_observables = {case.observable_id for case in cases}
            if case_observables and not case_observables.issubset(
                {
                    sample.objective_id
                    for sample in record.objective_samples
                }
            ):
                reasons.append('wrong_observable_or_band')

        reasons = list(dict.fromkeys(reasons))
        if not reasons:
            support_state: O90ESupportState = 'full'
        elif (
            record is None
            or record.evidence_scope != 'owned_room'
            or 'synthetic_evidence' in reasons
        ):
            support_state = 'unsupported'
        elif any(item.status == 'supported' for item in assessments):
            support_state = 'partially_supported'
        else:
            support_state = 'model_conditioned_only'

        return build_o90e_decision(
            spec=spec,
            validation_id=o60_validation_id,
            validation_sha256=validation_sha,
            campaign_id=campaign_id,
            campaign_sha256=campaign_sha,
            assessments=assessments,
            axis_coverage=coverage,
            support_state=support_state,
            reasons=reasons,
            decided_at_utc=decided_at_utc,
        )

    def evaluate_and_save_decision(
        self,
        *,
        robustness_spec_id: str,
        o60_validation_id: str,
        case_ids: Sequence[str],
        decided_at_utc: str,
    ) -> O90EValidationDecision:
        decision = self.evaluate_decision(
            robustness_spec_id=robustness_spec_id,
            o60_validation_id=o60_validation_id,
            case_ids=case_ids,
            decided_at_utc=decided_at_utc,
        )
        self.save_decision(decision)
        return decision

    def _validate_prediction_ref(
        self,
        ref: O90EPredictionAuthorityRef,
    ) -> None:
        attempt = self.roomsim_repository.get_attempt(ref.prediction_attempt_id)
        if (
            attempt is None
            or attempt.attempt_sha256 != ref.prediction_attempt_sha256
            or attempt.batch_run_id != ref.batch_run_id
            or attempt.candidate_id != ref.candidate_id
            or attempt.result is None
            or attempt.result.model_version != ref.model_version
        ):
            raise ValueError('O90E decision prediction binding is stale or tampered')
        batch = self.roomsim_repository.get_batch_spec(ref.batch_run_id)
        if (
            batch is None
            or batch.batch_spec_sha256 != ref.batch_spec_sha256
            or batch.binding_sha256 != ref.binding_sha256
            or batch.model_id != ref.model_id
            or batch.adapter_version != ref.adapter_version
        ):
            raise ValueError('O90E decision prediction config binding is stale or tampered')

    def _validate_decision_bindings(
        self,
        decision: O90EValidationDecision,
    ) -> None:
        spec = self._spec(decision.robustness_spec_id)
        if (
            spec.robustness_spec_sha256 != decision.robustness_spec_sha256
            or spec.candidate_id != decision.candidate_id
            or spec.candidate_sha256 != decision.candidate_sha256
        ):
            raise ValueError('O90E decision RobustnessSpec/candidate binding mismatch')
        if (
            spec.scene_revision_id != decision.scene_revision_id
            or spec.scene_content_hash != decision.scene_content_hash
        ):
            raise ValueError('O90E decision SceneRevision binding mismatch')
        source_revision = self.measurement_repository.scene_repository.get(
            spec.scene_revision_id
        )
        if (
            source_revision is None
            or source_revision.document_id != spec.document_id
            or source_revision.content_hash != spec.scene_content_hash
        ):
            raise ValueError('O90E decision source SceneRevision is unavailable or stale')
        if (
            spec.model_id != decision.model_id
            or spec.model_version != decision.model_version
            or spec.prediction_provider_id != decision.prediction_provider_id
            or spec.fidelity != decision.fidelity
            or spec.objective_evaluation_spec_sha256
            != decision.objective_evaluation_spec_sha256
        ):
            raise ValueError('O90E decision model/config binding mismatch')

        if decision.production_gate == 'eligible':
            required_axes = {axis.axis_id for axis in spec.axes}
            covered_axes = {
                item.axis_id
                for item in decision.axis_coverage
                if item.state == 'full'
            }
            assessment_keys = {
                (item.axis_id, item.direction)
                for item in decision.assessments
                if item.status == 'supported'
            }
            required_assessments = {
                (axis.axis_id, direction)
                for axis in spec.axes
                for direction in ('minus', 'plus')
            }
            if covered_axes != required_axes:
                raise ValueError(
                    'O90E eligible decision does not cover every RobustnessSpec axis'
                )
            if assessment_keys != required_assessments:
                raise ValueError(
                    'O90E eligible decision does not contain every signed perturbation'
                )

        if decision.o60_validation_sha256 is not None:
            record = self.model_validation_repository.get(decision.o60_validation_id)
            if (
                record is None
                or record.validation_sha256 != decision.o60_validation_sha256
            ):
                raise ValueError('O90E decision O60 validation binding mismatch')
            if (
                record.campaign_id != decision.o60_campaign_id
                or record.campaign_sha256 != decision.o60_campaign_sha256
            ):
                raise ValueError('O90E decision O60 campaign binding mismatch')
            campaign = self.campaign_repository.get(record.campaign_id)
            if (
                campaign is None
                or campaign.campaign_sha256 != record.campaign_sha256
            ):
                raise ValueError('O90E decision O60 campaign is unavailable or tampered')

        for assessment in decision.assessments:
            case = self.get_case(assessment.case_id)
            if case is None or case.case_sha256 != assessment.case_sha256:
                raise ValueError('O90E decision case binding mismatch')
            for prediction in (
                assessment.nominal_prediction,
                assessment.perturbation_prediction,
            ):
                if prediction is not None:
                    self._validate_prediction_ref(prediction)

            for measurement in (
                assessment.nominal_measurement,
                assessment.perturbation_measurement,
            ):
                if measurement is not None:
                    plan_matches = tuple(
                        plan
                        for plan in self.measurement_repository.list_measurement_plans(
                            spec.search_spec_id
                        )
                        if (
                            plan.plan_id == measurement.plan_id
                            and plan.plan_sha256
                            == measurement.completed_plan_sha256
                        )
                    )
                    if len(plan_matches) != 1:
                        raise ValueError(
                            'O90E decision MeasurementPlan binding is stale or tampered'
                        )
                    current = self.measurement_repository.get_measurement(
                        measurement.measurement_id
                    )
                    dataset = self.measurement_repository.get_dataset(
                        measurement.dataset_id
                    )
                    report = self.quality_repository.get_report(
                        measurement.quality_report_id
                    )
                    if (
                        current is None
                        or dataset is None
                        or report is None
                        or measurement_sha256(current)
                        != measurement.measurement_sha256
                        or dataset_sha256(dataset) != measurement.dataset_sha256
                        or dataset.source_sha256 != measurement.raw_asset_sha256
                        or report.report_sha256
                        != measurement.quality_report_sha256
                    ):
                        raise ValueError(
                            'O90E decision measurement/quality binding is stale or tampered'
                        )
                    if not self._raw_asset_valid(measurement.raw_asset_sha256):
                        raise ValueError(
                            'O90E decision raw measurement asset is missing or tampered'
                        )
                    current_variant = self._system_variant_ref(
                        current.scene_revision_id
                    )
                    if current_variant != measurement.system_variant:
                        raise ValueError(
                            'O90E decision SystemVariant binding is stale or tampered'
                        )
                    acquisition = report.acquisition_context
                    if measurement.acquisition_context_id is None:
                        if acquisition is not None:
                            raise ValueError(
                                'O90E historical acquisition binding changed'
                            )
                    elif (
                        acquisition is None
                        or acquisition.acquisition_context_id
                        != measurement.acquisition_context_id
                        or acquisition.acquisition_context_sha256
                        != measurement.acquisition_context_sha256
                    ):
                        raise ValueError(
                            'O90E decision acquisition binding is stale or tampered'
                        )

    def _require_canonical_decision(
        self,
        decision: O90EValidationDecision,
    ) -> None:
        """Re-run the canonical evaluator and require exact reproduction.

        ``_validate_decision_bindings`` proves the referenced authorities
        exist; it does not prove the stored payload is their canonical
        interpretation. The trusted ``evaluate_decision(...)`` algorithm is
        replayed here over the persisted RobustnessSpec, the exact persisted
        case set, the exact O60 validation authority, and the stored
        ``decided_at_utc``. Every caller-supplied field — assessment
        axis/direction/delta, status and reasons, sensitivity evidence hash,
        measurement refs with their embedded capability decision, prediction
        refs, axis coverage, support state, and the production gate itself —
        is recomputed by the canonical evaluator and must reproduce the
        stored decision exactly. ``schema_version``/``authority_version``
        are Literal-typed, so unknown evaluator versions already fail closed
        at model validation; anything short of exact reproduction fails
        closed here.
        """
        regenerated = self.evaluate_decision(
            robustness_spec_id=decision.robustness_spec_id,
            o60_validation_id=decision.o60_validation_id,
            case_ids=tuple(item.case_id for item in decision.assessments),
            decided_at_utc=decision.decided_at_utc,
        )
        if regenerated != decision:
            raise ValueError(
                'O90E decision does not match the canonical evaluation of '
                'its resolved evidence'
            )

    def save_decision(self, decision: O90EValidationDecision) -> None:
        """Persist one canonical O90E production-gate decision.

        The stored payload must be the exact output of the canonical
        ``evaluate_decision(...)`` algorithm over the resolved authorities;
        caller-supplied assessment/coverage/gate fields are never trusted.
        """
        decision = O90EValidationDecision.model_validate(
            decision.model_dump(mode='python')
        )
        self._validate_decision_bindings(decision)
        self._require_canonical_decision(decision)
        with closing(self._connect()) as connection, connection:
            if connection.execute(
                'SELECT 1 FROM cad_robustness_validation_decisions WHERE decision_id=?',
                (decision.decision_id,),
            ).fetchone() is not None:
                raise ValueError(
                    f'O90E validation decision already exists: {decision.decision_id}'
                )
            connection.execute(
                '''
                INSERT INTO cad_robustness_validation_decisions(
                    decision_id, robustness_spec_id, candidate_id,
                    production_gate, support_state, decision_sha256,
                    decided_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    decision.decision_id,
                    decision.robustness_spec_id,
                    decision.candidate_id,
                    decision.production_gate,
                    decision.support_state,
                    decision.decision_sha256,
                    decision.decided_at_utc,
                    decision.model_dump_json(),
                ),
            )

    def get_decision(self, decision_id: str) -> O90EValidationDecision | None:
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_robustness_validation_decisions WHERE decision_id=?',
                (decision_id,),
            ).fetchone()
        if row is None:
            return None
        decision = O90EValidationDecision.model_validate_json(row['payload_json'])
        self._validate_decision_bindings(decision)
        self._require_canonical_decision(decision)
        return decision

    def list_decisions(
        self,
        robustness_spec_id: str,
    ) -> tuple[O90EValidationDecision, ...]:
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                '''
                SELECT payload_json
                FROM cad_robustness_validation_decisions
                WHERE robustness_spec_id=?
                ORDER BY seq ASC
                ''',
                (robustness_spec_id,),
            ).fetchall()
        decisions = tuple(
            O90EValidationDecision.model_validate_json(row['payload_json'])
            for row in rows
        )
        for decision in decisions:
            self._validate_decision_bindings(decision)
            self._require_canonical_decision(decision)
        return decisions
