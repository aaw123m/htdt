from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .cad_calibration import (
    CadCalibrationExportSnapshot,
    CadCalibrationLifecycleEvent,
    CadCalibrationPlan,
    CadVerificationMeasurementCompletion,
    CadVerificationMeasurementPlan,
    CadVerificationMeasurementPlanRegistration,
    build_generic_biquad_export,
    build_verification_measurement_completion,
    build_verification_plan_registration,
    evaluate_calibration_support,
    exact_verification_plan_registration,
)
from .cad_measurement_effective import CadEffectiveMeasurementResolver
from .cad_measurement_quality import dataset_sha256, measurement_sha256
from .cad_measurement_quality_repository import CadMeasurementQualityRepository
from .cad_measurement_repository import CadMeasurementRepository
from .cad_repository import SceneRepository
from .cad_scene import Position3
from .cad_schema import (
    check_native_schema_compatibility,
    require_native_tables,
    connect_sqlite,

)
from .cad_system_variant import materialize_system_variant
from .cad_system_variant_repository import CadSystemVariantRepository


def _utc_now() -> str:
    """Repository commit clock; the only source of durable registration time."""
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')
    return parsed


_LIFECYCLE_ORDER = {
    'proposed': 0,
    'exported': 1,
    'user_applied': 2,
    'remeasured': 3,
    'validated': 4,
}

# Exact #173 lifecycle edges. `exported` is also a legal root state: a plan
# persisted and exported before any `proposed` lifecycle event still opens its
# chain with the export fact. Export alone never implies the user applied the
# settings, that re-measurement happened, or that the plan validated — each of
# those is its own persisted transition of the exact current head.
_LIFECYCLE_ROOT_STATES = frozenset({'proposed', 'exported'})
_LIFECYCLE_TRANSITIONS = {
    'proposed': frozenset({'exported'}),
    'exported': frozenset({'user_applied'}),
    'user_applied': frozenset({'remeasured'}),
    'remeasured': frozenset({'validated'}),
    'validated': frozenset(),
}


class CalibrationLifecycleConflictError(ValueError):
    """A lifecycle-event save violated the plan's single-head lifecycle chain contract."""


def _lifecycle_chain_violation(
    head: CadCalibrationLifecycleEvent,
    event: CadCalibrationLifecycleEvent,
) -> str | None:
    """Return why *event* cannot extend persisted *head*, or None when valid."""
    if (
        event.supersedes_event_sha256 is not None
        and event.supersedes_event_sha256 != head.event_semantic_sha256
    ):
        return (
            f'claims predecessor {event.supersedes_event_sha256} '
            f'but the persisted head is {head.event_semantic_sha256}'
        )
    if event.state not in _LIFECYCLE_TRANSITIONS[head.state]:
        return f'{head.state} -> {event.state} is not an allowed lifecycle transition'
    return None


class CadCalibrationRepository:
    """Append-only #173 authority layered on exact scene/system/measurement sources.

    ``save_verification_plan`` is the durable preregistration authority for the
    re-measure contract: it commits the contract row together with a
    repository-attested registration record under one ``BEGIN IMMEDIATE``
    transaction, after proving the contract claims no already-collected after
    evidence and no qualifying re-measurement exists. Downstream gates
    (completion saves, ``remeasured``/``validated`` lifecycle transitions) use
    the persisted registration and completion records — never caller-supplied
    timestamps or bare measurement ids. Contract rows written before this
    authority existed stay readable but carry no registration, so they read as
    legacy post-hoc records rather than preregistered contracts.
    """

    def __init__(
        self,
        *,
        scene_repository: SceneRepository,
        system_variant_repository: CadSystemVariantRepository,
        measurement_repository: CadMeasurementRepository,
        quality_repository: CadMeasurementQualityRepository,
    ) -> None:
        paths = {
            Path(scene_repository.path),
            Path(system_variant_repository.path),
            Path(measurement_repository.path),
            Path(quality_repository.path),
        }
        if len(paths) != 1:
            raise ValueError('CalibrationPlan authorities must share one native repository')
        self.scene_repository = scene_repository
        self.system_variant_repository = system_variant_repository
        self.measurement_repository = measurement_repository
        self.quality_repository = quality_repository
        self._effective = CadEffectiveMeasurementResolver(
            measurement_repository, quality_repository
        )
        self.path = paths.pop()
        check_native_schema_compatibility(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_calibration_plans', 'cad_calibration_exports', 'cad_calibration_verification_plans', 'cad_calibration_verification_registrations', 'cad_calibration_verification_completions', 'cad_calibration_lifecycle_events')

    def _source_authorities(self, plan: CadCalibrationPlan):
        revision = self.scene_repository.get(plan.scene_revision_id)
        if revision is None:
            raise ValueError('CalibrationPlan references unknown SceneRevision')
        if (
            revision.document_id != plan.document_id
            or revision.content_hash != plan.scene_content_hash
        ):
            raise ValueError('CalibrationPlan SceneRevision authority mismatch')

        variant = self.system_variant_repository.get_variant(plan.system_variant_id)
        if variant is None:
            raise ValueError('CalibrationPlan references unknown SystemVariant')
        if variant.variant_sha256 != plan.system_variant_sha256:
            raise ValueError('CalibrationPlan SystemVariant hash mismatch')
        if variant.document_id != plan.document_id:
            raise ValueError('CalibrationPlan SystemVariant belongs to another document')
        if (
            variant.baseline_revision_id != revision.revision_id
            or variant.baseline_content_hash != revision.content_hash
        ):
            raise ValueError(
                'calibration-plan-1 requires SystemVariant baseline to equal exact source SceneRevision'
            )

        measurement = self.measurement_repository.get_measurement(plan.source_measurement_id)
        if measurement is None:
            raise ValueError('CalibrationPlan references unknown source Measurement')
        # Lifecycle gate (#509/#844): the canonical source must currently be
        # eligible — excluded/misassigned/test/duplicate evidence cannot
        # silently build a CalibrationPlan, and a corrected measurement is
        # judged by its corrected binding.
        self._effective.require_normal_use(
            plan.source_measurement_id, purpose='calibration source'
        )
        if plan.source_measurement_sha256 != measurement_sha256(measurement):
            raise ValueError('CalibrationPlan source Measurement hash mismatch')
        if (
            measurement.document_id != plan.document_id
            or measurement.scene_revision_id != revision.revision_id
            or measurement.scene_content_hash != revision.content_hash
        ):
            raise ValueError('CalibrationPlan source Measurement/SceneRevision binding mismatch')

        dataset = self.measurement_repository.get_dataset(plan.source_dataset_id)
        if dataset is None:
            raise ValueError('CalibrationPlan references unknown source Dataset')
        if dataset.measurement_id != measurement.measurement_id:
            raise ValueError('CalibrationPlan source Dataset/Measurement binding mismatch')
        if plan.source_dataset_sha256 != dataset_sha256(dataset):
            raise ValueError('CalibrationPlan source Dataset hash mismatch')

        report = self.quality_repository.get_report(plan.measurement_quality_report_id)
        if report is None:
            raise ValueError('CalibrationPlan references unknown MeasurementQualityReport')
        if report.report_sha256 != plan.measurement_quality_report_sha256:
            raise ValueError('CalibrationPlan MeasurementQualityReport hash mismatch')
        if (
            report.measurement_id != measurement.measurement_id
            or report.measurement_sha256 != plan.source_measurement_sha256
            or report.dataset_id != dataset.dataset_id
            or report.dataset_sha256 != plan.source_dataset_sha256
            or report.scene_revision_id != revision.revision_id
            or report.scene_content_hash != revision.content_hash
        ):
            raise ValueError('CalibrationPlan MeasurementQualityReport source binding mismatch')

        proposed_scene = materialize_system_variant(revision, variant)
        entities = {entity.entity_id: entity for entity in proposed_scene.entities}
        for channel in plan.channels:
            entity = entities.get(channel.source_entity_id)
            if entity is None or entity.kind != 'speaker':
                raise ValueError(
                    f'CalibrationPlan channel references non-speaker or missing source entity: '
                    f'{channel.source_entity_id}'
                )
            if entity.speaker_role and entity.speaker_role != channel.role_id:
                raise ValueError(
                    f'CalibrationPlan channel role does not match source speaker role: '
                    f'{channel.channel_id}'
                )

        return revision, variant, measurement, dataset, report

    def _validate_plan(self, plan: CadCalibrationPlan) -> None:
        plan = CadCalibrationPlan.model_validate(plan.model_dump(mode='python'))
        _revision, _variant, _measurement, _dataset, report = self._source_authorities(plan)
        state, reasons = evaluate_calibration_support(
            quality_report=report,
            sample_rate_hz=plan.sample_rate_hz,
            channels=plan.channels,
            target_curve=plan.target_curve,
            max_boost_db=plan.max_boost_db,
            max_cut_db=plan.max_cut_db,
            device_constraints=plan.device_constraints,
        )
        if state != plan.support_state or reasons != plan.unsupported_reasons:
            raise ValueError('CalibrationPlan support state is not canonical')

    def save_plan(self, plan: CadCalibrationPlan) -> None:
        self._validate_plan(plan)
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            if connection.execute(
                'SELECT 1 FROM cad_calibration_plans WHERE plan_id=?',
                (plan.plan_id,),
            ).fetchone() is not None:
                raise ValueError(f'CalibrationPlan already exists: {plan.plan_id}')
            connection.execute(
                """
                INSERT INTO cad_calibration_plans(
                    plan_id, document_id, scene_revision_id, system_variant_id,
                    source_measurement_id, source_dataset_id, quality_report_id,
                    plan_semantic_sha256, support_state, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.document_id,
                    plan.scene_revision_id,
                    plan.system_variant_id,
                    plan.source_measurement_id,
                    plan.source_dataset_id,
                    plan.measurement_quality_report_id,
                    plan.plan_semantic_sha256,
                    plan.support_state,
                    plan.created_at_utc,
                    plan.model_dump_json(),
                ),
            )

    def get_plan(self, plan_id: str) -> CadCalibrationPlan | None:
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_calibration_plans WHERE plan_id=?',
                (plan_id,),
            ).fetchone()
        if row is None:
            return None
        plan = CadCalibrationPlan.model_validate_json(row['payload_json'])
        self._validate_plan(plan)
        return plan

    def list_plans(self, document_id: str) -> tuple[CadCalibrationPlan, ...]:
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_calibration_plans
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        plans = tuple(CadCalibrationPlan.model_validate_json(row['payload_json']) for row in rows)
        for plan in plans:
            self._validate_plan(plan)
        return plans

    def _validate_export(self, snapshot: CadCalibrationExportSnapshot) -> CadCalibrationPlan:
        snapshot = CadCalibrationExportSnapshot.model_validate(
            snapshot.model_dump(mode='python')
        )
        plan = self.get_plan(snapshot.calibration_plan_id)
        if plan is None:
            raise ValueError('calibration export references unknown CalibrationPlan')
        rebuilt = build_generic_biquad_export(
            plan,
            export_id=snapshot.export_id,
            created_at_utc=snapshot.created_at_utc,
        )
        if rebuilt != snapshot:
            raise ValueError('calibration export does not match canonical generic adapter output')
        return plan

    def save_export(self, snapshot: CadCalibrationExportSnapshot) -> None:
        self._validate_export(snapshot)
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            if connection.execute(
                'SELECT 1 FROM cad_calibration_exports WHERE export_id=?',
                (snapshot.export_id,),
            ).fetchone() is not None:
                raise ValueError(f'calibration export already exists: {snapshot.export_id}')
            connection.execute(
                """
                INSERT INTO cad_calibration_exports(
                    export_id, plan_id, exported_settings_semantic_sha256,
                    adapter_id, adapter_version, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.export_id,
                    snapshot.calibration_plan_id,
                    snapshot.exported_settings_semantic_sha256,
                    snapshot.adapter_id,
                    snapshot.adapter_version,
                    snapshot.created_at_utc,
                    snapshot.model_dump_json(),
                ),
            )

    def get_export(self, export_id: str) -> CadCalibrationExportSnapshot | None:
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_calibration_exports WHERE export_id=?',
                (export_id,),
            ).fetchone()
        if row is None:
            return None
        snapshot = CadCalibrationExportSnapshot.model_validate_json(row['payload_json'])
        self._validate_export(snapshot)
        return snapshot

    def list_exports(self, plan_id: str) -> tuple[CadCalibrationExportSnapshot, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_calibration_exports
                WHERE plan_id=?
                ORDER BY seq ASC
                """,
                (plan_id,),
            ).fetchall()
        snapshots = tuple(
            CadCalibrationExportSnapshot.model_validate_json(row['payload_json'])
            for row in rows
        )
        for snapshot in snapshots:
            self._validate_export(snapshot)
        return snapshots

    def list_verification_plans(
        self,
        plan_id: str,
    ) -> tuple[CadVerificationMeasurementPlan, ...]:
        """Return every persisted re-measure contract for a plan, revalidated.

        Each row is replayed through ``_validate_verification`` so a stored
        contract whose plan/export/scene authority moved fails closed on read,
        matching ``list_exports``/``list_verification_completions`` semantics.
        """
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_calibration_verification_plans
                WHERE plan_id=?
                ORDER BY seq ASC
                """,
                (plan_id,),
            ).fetchall()
        verifications = tuple(
            CadVerificationMeasurementPlan.model_validate_json(row['payload_json'])
            for row in rows
        )
        for verification in verifications:
            self._validate_verification(verification)
        return verifications

    def _validate_verification(
        self,
        verification: CadVerificationMeasurementPlan,
    ) -> None:
        verification = CadVerificationMeasurementPlan.model_validate(
            verification.model_dump(mode='python')
        )
        plan = self.get_plan(verification.calibration_plan_id)
        if plan is None:
            raise ValueError('verification plan references unknown CalibrationPlan')
        snapshot = self.get_export(verification.exported_settings_id)
        if snapshot is None:
            raise ValueError('verification plan references unknown exported settings')
        if (
            verification.calibration_plan_semantic_sha256 != plan.plan_semantic_sha256
            or verification.exported_settings_semantic_sha256
            != snapshot.exported_settings_semantic_sha256
        ):
            raise ValueError('verification plan exact plan/export hash mismatch')
        if (
            verification.document_id != plan.document_id
            or verification.scene_revision_id != plan.scene_revision_id
            or verification.scene_content_hash != plan.scene_content_hash
            or verification.system_variant_id != plan.system_variant_id
            or verification.system_variant_sha256 != plan.system_variant_sha256
        ):
            raise ValueError('verification plan exact scene/system binding mismatch')
        for measurement_id in (
            *verification.before_measurement_ids,
            *verification.after_measurement_ids,
        ):
            measurement = self.measurement_repository.get_measurement(measurement_id)
            if measurement is None:
                raise ValueError(
                    f'verification plan references unknown Measurement: {measurement_id}'
                )
            self._effective.require_normal_use(
                measurement_id, purpose='calibration verification'
            )
            if (
                measurement.document_id != plan.document_id
                or measurement.scene_revision_id != plan.scene_revision_id
                or measurement.scene_content_hash != plan.scene_content_hash
            ):
                raise ValueError('verification measurement exact SceneRevision binding mismatch')

    def _preexisting_after_evidence(
        self,
        connection: sqlite3.Connection,
        verification: CadVerificationMeasurementPlan,
    ) -> str | None:
        """Return a persisted measurement id that could serve as after evidence.

        Qualifying evidence is a ``measured`` capture bound to the contract's
        exact SceneRevision at a preregistered measurement point on
        preregistered routing that is not one of the declared before
        measurements. The check runs inside the same write transaction that
        commits the contract and its registration, so a concurrent
        measurement import and a registration serialize into one ordering:
        either the contract commits first, or the already-committed evidence
        makes the registration fail.
        """
        points = {
            (point.point_id, point.position)
            for point in verification.measurement_points
        }
        before_ids = set(verification.before_measurement_ids)
        routing = set(verification.routing)
        rows = connection.execute(
            """
            SELECT measurement_id, measurement_entity_id, measurement_position_json,
                   channel_role
            FROM cad_measurements
            WHERE document_id=? AND scene_revision_id=? AND scene_content_hash=?
              AND evidence_type='measured'
            """,
            (
                verification.document_id,
                verification.scene_revision_id,
                verification.scene_content_hash,
            ),
        ).fetchall()
        for row in rows:
            if row['measurement_id'] in before_ids:
                continue
            # Qualifying evidence must currently be eligible (#509/#844): an
            # excluded/test/duplicate measurement must not block registration
            # by posing as existing qualifying after-evidence, and a corrected
            # binding decides the entity/channel match.
            try:
                evidence = self._effective.require_normal_use(
                    str(row['measurement_id']),
                    purpose='calibration verification after evidence',
                )
            except ValueError:
                continue
            if evidence.channel_role not in routing:
                continue
            position = Position3.model_validate(
                json.loads(row['measurement_position_json'])
            )
            if (evidence.measurement_entity_id, position) in points:
                return str(row['measurement_id'])
        return None

    def _commit_verification_registration(
        self,
        connection: sqlite3.Connection,
        verification: CadVerificationMeasurementPlan,
    ) -> CadVerificationMeasurementPlanRegistration:
        """Attest durable preregistration inside the contract write transaction.

        ``registered_at_utc`` is generated here at commit and the
        empty-after / no-qualifying-evidence checks run under the same
        ``BEGIN IMMEDIATE`` boundary, so evidence import and contract
        registration have exactly one deterministic ordering.
        """
        if verification.after_measurement_ids:
            raise ValueError(
                'verification plan cannot register while claiming '
                'already-collected after measurement evidence'
            )
        claimed = _parse_timestamp(
            verification.created_at_utc,
            'verification plan created_at_utc',
        )
        registered_at_utc = _utc_now()
        registered = _parse_timestamp(
            registered_at_utc,
            'verification plan registration registered_at_utc',
        )
        if claimed > registered:
            raise ValueError(
                'verification plan created_at_utc cannot postdate durable '
                'registration'
            )
        blocker = self._preexisting_after_evidence(connection, verification)
        if blocker is not None:
            raise ValueError(
                'verification plan cannot register over existing qualifying '
                f're-measurement evidence: {blocker}'
            )
        registration = build_verification_plan_registration(
            verification=verification,
            registered_at_utc=registered_at_utc,
        )
        connection.execute(
            """
            INSERT INTO cad_calibration_verification_plans(
                verification_plan_id, plan_id, export_id,
                verification_semantic_sha256, created_at_utc, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                verification.verification_plan_id,
                verification.calibration_plan_id,
                verification.exported_settings_id,
                verification.verification_semantic_sha256,
                registration.registered_at_utc,
                verification.model_dump_json(),
            ),
        )
        connection.execute(
            """
            INSERT INTO cad_calibration_verification_registrations(
                registration_id, registration_sha256, verification_plan_id,
                verification_plan_semantic_sha256, registered_at_utc, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                registration.registration_id,
                registration.registration_sha256,
                registration.verification_plan_id,
                registration.verification_plan_semantic_sha256,
                registration.registered_at_utc,
                registration.model_dump_json(),
            ),
        )
        return registration

    def save_verification_plan(
        self,
        verification: CadVerificationMeasurementPlan,
    ) -> CadVerificationMeasurementPlanRegistration:
        """Persist the re-measure contract and its durable preregistration.

        The contract row and a repository-attested
        ``CadVerificationMeasurementPlanRegistration`` commit under one
        ``BEGIN IMMEDIATE`` transaction. Registration fails when the contract
        claims already-collected ``after_measurement_ids`` or when qualifying
        re-measurement evidence already exists, so a contract built after
        seeing after data can never pose as the original preregistration. A
        repeated save of the exact persisted contract is idempotent and
        returns the original registration — the durable time is never
        rewritten, and a persisted contract without a registration row fails
        closed instead of silently inferring one.
        """
        self._validate_verification(verification)
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute(
                'SELECT payload_json FROM cad_calibration_verification_plans '
                'WHERE verification_plan_id=?',
                (verification.verification_plan_id,),
            ).fetchone()
            if row is not None:
                persisted = CadVerificationMeasurementPlan.model_validate_json(
                    row['payload_json']
                )
                if persisted != verification:
                    raise ValueError(
                        f'verification measurement plan already exists: '
                        f'{verification.verification_plan_id}'
                    )
                registration = self._registration_for(
                    connection,
                    verification.verification_plan_id,
                )
                if registration is None:
                    # No historical registration time is silently inferred.
                    raise ValueError(
                        'verification plan registration authority missing/stale'
                    )
                exact_verification_plan_registration(persisted, registration)
            else:
                registration = self._commit_verification_registration(
                    connection,
                    verification,
                )
            connection.commit()
        return registration

    def get_verification_plan(
        self,
        verification_plan_id: str,
    ) -> CadVerificationMeasurementPlan | None:
        """Return the persisted re-measure contract, including legacy rows.

        Rows written before the registration authority existed — recognizable
        by a missing ``cad_calibration_verification_registrations`` row or a
        non-empty ``after_measurement_ids`` — are still readable for audit but
        are post-hoc evidence bundles, not preregistered contracts: every
        attestation path (``get_verification_plan_registration``,
        ``save_verification_completion``, re-measure lifecycle transitions)
        fails closed for them.
        """
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_calibration_verification_plans
                WHERE verification_plan_id=?
                """,
                (verification_plan_id,),
            ).fetchone()
        if row is None:
            return None
        verification = CadVerificationMeasurementPlan.model_validate_json(
            row['payload_json']
        )
        self._validate_verification(verification)
        return verification

    @staticmethod
    def _registration_for(
        connection: sqlite3.Connection,
        verification_plan_id: str,
    ) -> CadVerificationMeasurementPlanRegistration | None:
        row = connection.execute(
            'SELECT payload_json FROM cad_calibration_verification_registrations '
            'WHERE verification_plan_id=?',
            (verification_plan_id,),
        ).fetchone()
        if row is None:
            return None
        return CadVerificationMeasurementPlanRegistration.model_validate_json(
            row['payload_json']
        )

    def get_verification_plan_registration(
        self,
        verification_plan_id: str,
    ) -> CadVerificationMeasurementPlanRegistration | None:
        """Return the durable preregistration for one contract, if attested.

        ``None`` marks a missing registration — including every legacy
        post-hoc plan row — never an inferred one. A registration pointing at
        a missing or different contract fails closed.
        """
        with closing(self._connect()) as connection, connection:
            registration = self._registration_for(connection, verification_plan_id)
        if registration is None:
            return None
        verification = self.get_verification_plan(verification_plan_id)
        if verification is None:
            raise ValueError(
                'verification plan registration references missing contract'
            )
        return exact_verification_plan_registration(verification, registration)

    def save_verification_completion(
        self,
        completion: CadVerificationMeasurementCompletion,
    ) -> CadVerificationMeasurementCompletion:
        """Persist append-only re-measure evidence for a preregistered contract.

        The completion must reproduce exactly from the persisted contract,
        its durable registration and the bound measurement/dataset/quality
        authorities: the whole record — including the ``pass``/``fail``
        result — is rebuilt and compared, so no claimed outcome is trusted
        on payload alone. Contracts without a registration row (legacy
        post-hoc records) can never gain completion evidence.
        """
        completion = CadVerificationMeasurementCompletion.model_validate(
            completion.model_dump(mode='python')
        )
        verification = self.get_verification_plan(completion.verification_plan_id)
        if (
            verification is None
            or verification.verification_semantic_sha256
            != completion.verification_plan_semantic_sha256
        ):
            raise ValueError('verification completion plan authority missing/stale')
        registration = self.get_verification_plan_registration(
            verification.verification_plan_id
        )
        if registration is None:
            raise ValueError(
                'verification plan registration authority missing/stale'
            )
        if (
            completion.registration_id != registration.registration_id
            or completion.registration_sha256 != registration.registration_sha256
        ):
            raise ValueError('verification completion registration authority mismatch')
        comparison = None
        if completion.comparison_id is not None:
            comparison = self.measurement_repository.get_comparison(
                completion.comparison_id
            )
            if (
                comparison is None
                or comparison.comparison_sha256 != completion.comparison_sha256
            ):
                raise ValueError(
                    'verification completion comparison authority missing/stale'
                )
        rebuilt = build_verification_measurement_completion(
            verification=verification,
            registration=registration,
            after_measurement_ids=completion.after_measurement_ids,
            measurement_repository=self.measurement_repository,
            quality_repository=self.quality_repository,
            comparison=comparison,
            completed_at_utc=completion.completed_at_utc,
        )
        if rebuilt != completion:
            raise ValueError('verification completion does not reproduce exactly')
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_calibration_verification_completions '
                'WHERE completion_id=?',
                (completion.completion_id,),
            ).fetchone()
            if row is not None:
                persisted = CadVerificationMeasurementCompletion.model_validate_json(
                    row['payload_json']
                )
                if persisted != completion:
                    raise ValueError(
                        'verification completion id has different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_calibration_verification_completions(
                    completion_id, completion_sha256, verification_plan_id,
                    result, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    completion.completion_id,
                    completion.completion_sha256,
                    completion.verification_plan_id,
                    completion.result,
                    completion.completed_at_utc,
                    completion.model_dump_json(),
                ),
            )
        return completion

    def get_verification_completion(
        self,
        completion_id: str,
    ) -> CadVerificationMeasurementCompletion | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_calibration_verification_completions '
                'WHERE completion_id=?',
                (completion_id,),
            ).fetchone()
        if row is None:
            return None
        completion = CadVerificationMeasurementCompletion.model_validate_json(
            row['payload_json']
        )
        return self.save_verification_completion(completion)

    def list_verification_completions(
        self,
        verification_plan_id: str,
    ) -> tuple[CadVerificationMeasurementCompletion, ...]:
        """Return every persisted completion for a contract, fully revalidated."""
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_calibration_verification_completions
                WHERE verification_plan_id=?
                ORDER BY seq ASC
                """,
                (verification_plan_id,),
            ).fetchall()
        completions = tuple(
            CadVerificationMeasurementCompletion.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )
        return tuple(
            self.save_verification_completion(completion)
            for completion in completions
        )

    def _validate_lifecycle_event(
        self,
        event: CadCalibrationLifecycleEvent,
    ) -> None:
        event = CadCalibrationLifecycleEvent.model_validate(
            event.model_dump(mode='python')
        )
        plan = self.get_plan(event.calibration_plan_id)
        if plan is None:
            raise ValueError('calibration lifecycle references unknown CalibrationPlan')
        if event.calibration_plan_semantic_sha256 != plan.plan_semantic_sha256:
            raise ValueError('calibration lifecycle CalibrationPlan hash mismatch')

        if event.exported_settings_id is not None:
            snapshot = self.get_export(event.exported_settings_id)
            if snapshot is None:
                raise ValueError('calibration lifecycle references unknown export')
            if (
                snapshot.exported_settings_semantic_sha256
                != event.exported_settings_semantic_sha256
            ):
                raise ValueError('calibration lifecycle export hash mismatch')
            if snapshot.calibration_plan_id != plan.plan_id:
                raise ValueError('calibration lifecycle export belongs to another plan')

        if event.verification_plan_id is not None:
            verification = self.get_verification_plan(event.verification_plan_id)
            if verification is None:
                raise ValueError('calibration lifecycle references unknown verification plan')
            if (
                verification.verification_semantic_sha256
                != event.verification_plan_semantic_sha256
            ):
                raise ValueError('calibration lifecycle verification hash mismatch')
            if verification.calibration_plan_id != plan.plan_id:
                raise ValueError('calibration lifecycle verification belongs to another plan')
            registration = self.get_verification_plan_registration(
                verification.verification_plan_id
            )
            if registration is None:
                raise ValueError(
                    'calibration lifecycle verification plan registration '
                    'authority missing/stale'
                )
            if event.state in {'remeasured', 'validated'}:
                # Re-measurement facts must reproduce a persisted completion
                # bound to the preregistered contract; arbitrary same-scene
                # measurement ids are not evidence, and `validated`
                # additionally requires the completion's reproduced PASS.
                matching = tuple(
                    item
                    for item in self.list_verification_completions(
                        verification.verification_plan_id
                    )
                    if item.after_measurement_ids
                    == tuple(sorted(event.measurement_ids))
                )
                if not matching:
                    raise ValueError(
                        'calibration lifecycle measurement ids do not reproduce '
                        'a persisted verification completion'
                    )
                if event.state == 'validated' and not any(
                    item.result == 'pass' for item in matching
                ):
                    raise ValueError(
                        'calibration lifecycle validated requires a passing '
                        'persisted verification completion'
                    )

        for measurement_id in event.measurement_ids:
            measurement = self.measurement_repository.get_measurement(measurement_id)
            if measurement is None:
                raise ValueError(
                    f'calibration lifecycle references unknown Measurement: {measurement_id}'
                )
            self._effective.require_normal_use(
                measurement_id, purpose='calibration lifecycle'
            )
            if (
                measurement.document_id != plan.document_id
                or measurement.scene_revision_id != plan.scene_revision_id
                or measurement.scene_content_hash != plan.scene_content_hash
            ):
                raise ValueError('calibration lifecycle measurement SceneRevision mismatch')

    def save_lifecycle_event(self, event: CadCalibrationLifecycleEvent) -> None:
        """Append one lifecycle event as the single head of its plan's chain.

        The persisted history of one ``calibration_plan_id`` is an append-only
        state machine enforced under one ``BEGIN IMMEDIATE`` transaction:

        - the first event must be ``proposed`` or ``exported`` and claim no
          predecessor;
        - every later event must claim the exact current head via
          ``supersedes_event_sha256``;
        - only the explicit ``proposed -> exported -> user_applied ->
          remeasured -> validated`` edges are legal, so export alone can never
          advance a plan to applied/validated semantics;
        - ``validated`` is terminal.

        Two writers building on the same head cannot both advance it: the head
        check runs inside the write transaction, so the loser sees the moved
        head and fails with ``CalibrationLifecycleConflictError``. The event's
        upstream authorities (exact plan, export, verification plan and
        measurement bindings) are revalidated on every save before the lock is
        taken, so the builder remains a convenience and not the only integrity
        boundary.
        """
        self._validate_lifecycle_event(event)
        with closing(self._connect()) as connection, connection:
            # BEGIN IMMEDIATE holds the write lock across the duplicate recheck,
            # the head read and the insert: concurrent writers cannot both
            # observe the same head.
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute(
                'SELECT 1 FROM cad_calibration_lifecycle_events WHERE event_id=?',
                (event.event_id,),
            ).fetchone() is not None:
                raise ValueError(f'calibration lifecycle event already exists: {event.event_id}')
            head_row = connection.execute(
                """
                SELECT state, event_semantic_sha256, payload_json
                FROM cad_calibration_lifecycle_events
                WHERE plan_id=?
                ORDER BY seq DESC
                LIMIT 1
                """,
                (event.calibration_plan_id,),
            ).fetchone()
            if head_row is None:
                if event.state not in _LIFECYCLE_ROOT_STATES:
                    raise CalibrationLifecycleConflictError(
                        f'first calibration lifecycle state for plan '
                        f'{event.calibration_plan_id} must be proposed or exported'
                    )
                if event.supersedes_event_sha256 is not None:
                    raise CalibrationLifecycleConflictError(
                        f'first calibration lifecycle event for plan '
                        f'{event.calibration_plan_id} must not claim a predecessor '
                        'that was never persisted'
                    )
            else:
                head = CadCalibrationLifecycleEvent.model_validate_json(
                    head_row['payload_json']
                )
                if (
                    head_row['state'] != head.state
                    or head_row['event_semantic_sha256'] != head.event_semantic_sha256
                ):
                    raise ValueError(
                        'persisted calibration lifecycle head disagrees with its payload'
                    )
                if event.supersedes_event_sha256 is None:
                    raise CalibrationLifecycleConflictError(
                        f'calibration lifecycle for plan {event.calibration_plan_id} '
                        'already has a persisted head; a new event must claim it '
                        'via supersedes_event_sha256'
                    )
                violation = _lifecycle_chain_violation(head, event)
                if violation is not None:
                    raise CalibrationLifecycleConflictError(
                        f'calibration lifecycle event {event.event_id} rejected: '
                        f'{violation}'
                    )
            connection.execute(
                """
                INSERT INTO cad_calibration_lifecycle_events(
                    event_id, plan_id, state, event_semantic_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.calibration_plan_id,
                    event.state,
                    event.event_semantic_sha256,
                    event.created_at_utc,
                    event.model_dump_json(),
                ),
            )

    def list_lifecycle_events(
        self,
        plan_id: str,
    ) -> tuple[CadCalibrationLifecycleEvent, ...]:
        """Return the persisted lifecycle as a validated single-head chain.

        Rows are replayed in insertion order; the chain must start with an
        unclaimed ``proposed``/``exported`` root and every successor must be an
        allowed transition that extends its predecessor — explicitly via
        ``supersedes_event_sha256``, or implicitly for rows persisted before
        predecessor tracking existed. A historical fork, an unclaimed root or
        an invalid edge is surfaced as ``ValueError`` rather than silently
        relying on insertion order.
        """
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT event_id, plan_id, state, event_semantic_sha256, payload_json
                FROM cad_calibration_lifecycle_events
                WHERE plan_id=?
                ORDER BY seq ASC
                """,
                (plan_id,),
            ).fetchall()
        events = tuple(
            CadCalibrationLifecycleEvent.model_validate_json(row['payload_json'])
            for row in rows
        )
        head: CadCalibrationLifecycleEvent | None = None
        for row, event in zip(rows, events):
            if (
                row['event_id'] != event.event_id
                or row['plan_id'] != event.calibration_plan_id
                or row['state'] != event.state
                or row['event_semantic_sha256'] != event.event_semantic_sha256
            ):
                raise ValueError(
                    'persisted calibration lifecycle row disagrees with its payload'
                )
            self._validate_lifecycle_event(event)
            if head is None:
                if event.state not in _LIFECYCLE_ROOT_STATES:
                    raise ValueError(
                        f'calibration lifecycle history for {plan_id} '
                        'does not start with proposed or exported'
                    )
                if event.supersedes_event_sha256 is not None:
                    raise ValueError(
                        f'calibration lifecycle history for {plan_id} '
                        'starts with a predecessor claim that was never persisted'
                    )
            else:
                violation = _lifecycle_chain_violation(head, event)
                if violation is not None:
                    raise ValueError(
                        f'calibration lifecycle history for {plan_id} '
                        f'is not a single chain: {violation}'
                    )
            head = event
        return events
