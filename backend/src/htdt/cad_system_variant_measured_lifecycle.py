from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_measurement_models import (
    CadFrequencyResponseDataset,
    CadMeasurementRecord,
)
from .cad_measurement_quality import (
    CadMeasurementQualityReport,
    MeasurementCapabilityClaim,
    dataset_sha256,
    measurement_sha256,
)
from .cad_measurement_quality_repository import CadMeasurementQualityRepository
from .cad_measurement_repository import CadMeasurementRepository
from .cad_repository import SceneRepository
from .cad_schema import ensure_native_schema
from .cad_system_variant import EntityLifecycleBinding
from .cad_system_variant_lifecycle import (
    CadSystemVariantLifecycleRepository,
    SystemVariantAsBuiltRecord,
)


SYSTEM_VARIANT_MEASURED_SCHEMA_VERSION = 2
SYSTEM_VARIANT_MEASURED_AUTHORITY_VERSION = 'o100g-system-variant-measured-2'


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


def _authority_payload(
    connection: sqlite3.Connection,
    *,
    table: str,
    key_column: str,
    key: str,
) -> dict[str, Any] | None:
    """Read one persisted campaign-authority payload in the shared database.

    The O100G campaign repository owns the campaign/registration/completion
    tables over the same native CAD database. A missing table or row means
    the required authority is absent, never defaulted.
    """
    try:
        row = connection.execute(
            f'SELECT payload_json FROM {table} WHERE {key_column}=?',
            (key,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    payload = json.loads(row['payload_json'])
    return payload if isinstance(payload, dict) else None


class SystemVariantMeasurementEvidenceRef(BaseModel):
    """Exact measurement/quality evidence bound to one as-built revision."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    measurement_id: str = Field(min_length=1)
    measurement_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    dataset_id: str = Field(min_length=1)
    dataset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    raw_asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    quality_report_id: str = Field(min_length=1)
    quality_report_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    acquisition_context_id: str = Field(min_length=1)
    acquisition_context_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    allowed_capability_claims: tuple[MeasurementCapabilityClaim, ...] = Field(
        min_length=1
    )

    measurement_entity_id: str = Field(min_length=1)
    channel_role: str = Field(min_length=1)
    source_speaker_ids: tuple[str, ...] = ()
    radiation_scope: str = Field(min_length=1)

    @model_validator(mode='after')
    def unique_sources(self) -> 'SystemVariantMeasurementEvidenceRef':
        if self.source_speaker_ids != tuple(sorted(set(self.source_speaker_ids))):
            raise ValueError(
                'measured lifecycle source speaker ids must be unique and sorted'
            )
        if self.allowed_capability_claims != tuple(
            sorted(set(self.allowed_capability_claims))
        ):
            raise ValueError(
                'measured lifecycle allowed capability claims must be unique '
                'and sorted'
            )
        return self


class SystemVariantMeasuredRecord(BaseModel):
    """Explicit measured-evidence binding over one exact as-built authority.

    This does not mean every proposed entity was individually measured. Entity
    lifecycle is promoted to measured only for proposed speaker entities that
    appear explicitly in bound measurement source_speaker_ids.

    Measured promotion is campaign-bound: the record carries the exact
    preregistered campaign identity/hash and the durable campaign
    registration identity/hash that authorized it, hashed into
    `record_sha256`, so read-side validation can always prove why an entity
    entered the measured state.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[2] = SYSTEM_VARIANT_MEASURED_SCHEMA_VERSION
    authority_version: Literal[
        'o100g-system-variant-measured-2'
    ] = SYSTEM_VARIANT_MEASURED_AUTHORITY_VERSION

    record_id: str = Field(pattern=r'^system-variant-measured:[0-9a-f]{64}$')
    record_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    as_built_record_id: str = Field(
        pattern=r'^system-variant-as-built:[0-9a-f]{64}$'
    )
    as_built_record_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    campaign_id: str = Field(
        pattern=r'^system-variant-measurement-campaign:[0-9a-f]{64}$'
    )
    campaign_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    campaign_registration_id: str = Field(
        pattern=(
            r'^system-variant-measurement-campaign-registration:'
            r'[0-9a-f]{64}$'
        )
    )
    campaign_registration_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    application_id: str = Field(min_length=1)
    application_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    variant_id: str = Field(min_length=1)
    variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str = Field(min_length=1)
    as_built_revision_id: str = Field(min_length=1)
    as_built_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    measurements: tuple[SystemVariantMeasurementEvidenceRef, ...] = Field(
        min_length=1
    )
    entity_lifecycle: tuple[EntityLifecycleBinding, ...] = Field(min_length=1)

    bound_at_utc: str = Field(min_length=1)
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def validate_identity(self) -> 'SystemVariantMeasuredRecord':
        measurement_ids = [item.measurement_id for item in self.measurements]
        if measurement_ids != sorted(set(measurement_ids)):
            raise ValueError(
                'measured lifecycle measurements must be unique and sorted'
            )
        entity_ids = [item.entity_id for item in self.entity_lifecycle]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError('measured lifecycle entity ids must be unique')
        if any(
            item.state not in {'as_built', 'measured'}
            for item in self.entity_lifecycle
        ):
            raise ValueError(
                'measured lifecycle permits only as_built/measured entity states'
            )
        if len(self.notes) != len(set(self.notes)):
            raise ValueError('measured lifecycle notes must be unique')

        expected = _digest(self.semantic_payload())
        if self.record_sha256 != expected:
            raise ValueError('SystemVariantMeasuredRecord semantic hash mismatch')
        if self.record_id != f'system-variant-measured:{expected}':
            raise ValueError('SystemVariantMeasuredRecord id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'record_id', 'record_sha256'},
        )


def _measurement_evidence_ref(
    *,
    measurement: CadMeasurementRecord,
    dataset: CadFrequencyResponseDataset,
    report: CadMeasurementQualityReport,
) -> SystemVariantMeasurementEvidenceRef:
    if measurement.evidence_type != 'measured':
        raise ValueError(
            f'measured lifecycle requires measured evidence: '
            f'{measurement.measurement_id}'
        )
    if dataset.measurement_id != measurement.measurement_id:
        raise ValueError('measured lifecycle dataset/measurement mismatch')
    if report.measurement_id != measurement.measurement_id:
        raise ValueError('measured lifecycle quality report/measurement mismatch')
    if report.dataset_id != dataset.dataset_id:
        raise ValueError('measured lifecycle quality report/dataset mismatch')

    measurement_hash = measurement_sha256(measurement)
    data_hash = dataset_sha256(dataset)
    if report.measurement_sha256 != measurement_hash:
        raise ValueError('measured lifecycle quality report measurement hash mismatch')
    if report.dataset_sha256 != data_hash:
        raise ValueError('measured lifecycle quality report dataset hash mismatch')
    if report.raw_asset_sha256 != dataset.source_sha256:
        raise ValueError('measured lifecycle raw asset hash mismatch')
    if report.acquisition_context is None:
        raise ValueError(
            'measured lifecycle requires explicit acquisition context'
        )

    allowed_capabilities = tuple(
        sorted(
            item.claim
            for item in report.capabilities
            if item.decision == 'ALLOWED'
        )
    )
    if not allowed_capabilities:
        raise ValueError(
            'measured lifecycle requires at least one ALLOWED quality capability'
        )

    return SystemVariantMeasurementEvidenceRef(
        measurement_id=measurement.measurement_id,
        measurement_sha256=measurement_hash,
        dataset_id=dataset.dataset_id,
        dataset_sha256=data_hash,
        raw_asset_sha256=dataset.source_sha256,
        quality_report_id=report.report_id,
        quality_report_sha256=report.report_sha256,
        acquisition_context_id=report.acquisition_context.acquisition_context_id,
        acquisition_context_sha256=(
            report.acquisition_context.acquisition_context_sha256
        ),
        allowed_capability_claims=allowed_capabilities,
        measurement_entity_id=measurement.measurement_entity_id,
        channel_role=measurement.channel_role,
        source_speaker_ids=tuple(sorted(measurement.source_speaker_ids)),
        radiation_scope=measurement.radiation_scope,
    )


def build_system_variant_measured_record(
    *,
    scene_repository: SceneRepository,
    as_built_record: SystemVariantAsBuiltRecord,
    evidence: Sequence[
        tuple[
            CadMeasurementRecord,
            CadFrequencyResponseDataset,
            CadMeasurementQualityReport,
        ]
    ],
    campaign_id: str,
    campaign_sha256: str,
    campaign_registration_id: str,
    campaign_registration_sha256: str,
    bound_at_utc: str,
    notes: Sequence[str] = (),
) -> SystemVariantMeasuredRecord:
    """Compose one measured lifecycle record bound to a preregistered campaign.

    The campaign identity/hash and the durable campaign registration
    identity/hash are part of the record semantics, so the record always
    carries the exact campaign authority that authorized measured
    promotion. Persisting the record is a separate campaign-bound gate:
    `CadSystemVariantMeasuredLifecycleRepository` only commits it while the
    same transaction holds the campaign's persisted completion.
    """
    target = scene_repository.get(as_built_record.as_built_revision_id)
    if target is None:
        raise ValueError('measured lifecycle as-built SceneRevision disappeared')
    if (
        target.document_id != as_built_record.document_id
        or target.content_hash != as_built_record.as_built_content_hash
    ):
        raise ValueError('measured lifecycle as-built SceneRevision mismatch')

    refs: list[SystemVariantMeasurementEvidenceRef] = []
    measured_sources: dict[str, list[str]] = {}
    seen_measurements: set[str] = set()

    for measurement, dataset, report in evidence:
        if measurement.measurement_id in seen_measurements:
            raise ValueError(
                f'duplicate measured lifecycle measurement: '
                f'{measurement.measurement_id}'
            )
        seen_measurements.add(measurement.measurement_id)

        if (
            measurement.document_id != as_built_record.document_id
            or measurement.scene_revision_id != as_built_record.as_built_revision_id
            or measurement.scene_content_hash != as_built_record.as_built_content_hash
        ):
            raise ValueError(
                'measured lifecycle measurement must bind exact as-built revision'
            )
        if (
            report.document_id != measurement.document_id
            or report.scene_revision_id != measurement.scene_revision_id
            or report.scene_content_hash != measurement.scene_content_hash
        ):
            raise ValueError(
                'measured lifecycle quality report SceneRevision mismatch'
            )

        ref = _measurement_evidence_ref(
            measurement=measurement,
            dataset=dataset,
            report=report,
        )
        refs.append(ref)

        for source_id in ref.source_speaker_ids:
            try:
                source = target.document.entity(source_id)
            except KeyError as exc:
                raise ValueError(
                    f'measured lifecycle source speaker missing from as-built '
                    f'revision: {source_id}'
                ) from exc
            if source.kind != 'speaker':
                raise ValueError(
                    f'measured lifecycle source entity is not a speaker: {source_id}'
                )
            measured_sources.setdefault(source_id, []).append(
                measurement.measurement_id
            )

    if not refs:
        raise ValueError('measured lifecycle requires at least one measurement')
    refs.sort(key=lambda item: item.measurement_id)

    lifecycle: list[EntityLifecycleBinding] = []
    for original in as_built_record.entity_lifecycle:
        measurement_ids = tuple(
            sorted(set(measured_sources.get(original.entity_id, ())))
        )
        if measurement_ids:
            lifecycle.append(
                EntityLifecycleBinding(
                    entity_id=original.entity_id,
                    state='measured',
                    measurement_ids=measurement_ids,
                )
            )
        else:
            lifecycle.append(
                EntityLifecycleBinding(
                    entity_id=original.entity_id,
                    state='as_built',
                )
            )

    note_tuple = tuple(notes)
    core = {
        'schema_version': SYSTEM_VARIANT_MEASURED_SCHEMA_VERSION,
        'authority_version': SYSTEM_VARIANT_MEASURED_AUTHORITY_VERSION,
        'as_built_record_id': as_built_record.record_id,
        'as_built_record_sha256': as_built_record.record_sha256,
        'campaign_id': campaign_id,
        'campaign_sha256': campaign_sha256,
        'campaign_registration_id': campaign_registration_id,
        'campaign_registration_sha256': campaign_registration_sha256,
        'application_id': as_built_record.application_id,
        'application_sha256': as_built_record.application_sha256,
        'variant_id': as_built_record.variant_id,
        'variant_sha256': as_built_record.variant_sha256,
        'document_id': as_built_record.document_id,
        'as_built_revision_id': as_built_record.as_built_revision_id,
        'as_built_content_hash': as_built_record.as_built_content_hash,
        'measurements': [item.model_dump(mode='json') for item in refs],
        'entity_lifecycle': [
            item.model_dump(mode='json') for item in lifecycle
        ],
        'bound_at_utc': bound_at_utc,
        'notes': list(note_tuple),
    }
    digest = _digest(core)

    return SystemVariantMeasuredRecord(
        record_id=f'system-variant-measured:{digest}',
        record_sha256=digest,
        as_built_record_id=as_built_record.record_id,
        as_built_record_sha256=as_built_record.record_sha256,
        campaign_id=campaign_id,
        campaign_sha256=campaign_sha256,
        campaign_registration_id=campaign_registration_id,
        campaign_registration_sha256=campaign_registration_sha256,
        application_id=as_built_record.application_id,
        application_sha256=as_built_record.application_sha256,
        variant_id=as_built_record.variant_id,
        variant_sha256=as_built_record.variant_sha256,
        document_id=as_built_record.document_id,
        as_built_revision_id=as_built_record.as_built_revision_id,
        as_built_content_hash=as_built_record.as_built_content_hash,
        measurements=tuple(refs),
        entity_lifecycle=tuple(lifecycle),
        bound_at_utc=bound_at_utc,
        notes=note_tuple,
    )


class CadSystemVariantMeasuredLifecycleRepository:
    """Append-only measured lifecycle evidence bound to campaign authority.

    Every record binds one exact preregistered
    SystemVariantMeasurementCampaign and its durable registration
    (identity + SHA-256) into `record_sha256`. There is no standalone
    public save path: the only durable promotion is the O100G campaign
    repository's `complete_campaign`, which commits the exact campaign
    completion before this record inside one shared transaction. Reads
    re-resolve the record, its persisted campaign/registration authorities
    and the persisted campaign completion, so measured state can always
    prove which preregistered campaign authorized it.
    """

    def __init__(
        self,
        *,
        scene_repository: SceneRepository,
        lifecycle_repository: CadSystemVariantLifecycleRepository,
        measurement_repository: CadMeasurementRepository,
        quality_repository: CadMeasurementQualityRepository,
    ) -> None:
        self.scene_repository = scene_repository
        self.lifecycle_repository = lifecycle_repository
        self.measurement_repository = measurement_repository
        self.quality_repository = quality_repository
        self.path = Path(scene_repository.path)

        for label, repository in (
            ('as-built lifecycle', lifecycle_repository),
            ('measurement', measurement_repository),
            ('measurement quality', quality_repository),
        ):
            if Path(repository.path) != self.path:
                raise ValueError(
                    f'measured lifecycle and {label} repositories must share '
                    'one native CAD database'
                )

        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cad_system_variant_measured (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id TEXT NOT NULL UNIQUE,
                    record_sha256 TEXT NOT NULL UNIQUE,
                    as_built_record_id TEXT NOT NULL,
                    variant_id TEXT NOT NULL,
                    as_built_revision_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_system_variant_measured_as_built_seq
                    ON cad_system_variant_measured(
                        as_built_record_id,
                        seq ASC
                    );
                """
            )

    def _validate(
        self,
        record: SystemVariantMeasuredRecord,
    ) -> SystemVariantMeasuredRecord:
        record = SystemVariantMeasuredRecord.model_validate(
            record.model_dump(mode='python')
        )
        as_built = self.lifecycle_repository.get(record.as_built_record_id)
        if as_built is None:
            raise ValueError(
                'measured lifecycle references missing as-built record'
            )
        if (
            as_built.record_sha256 != record.as_built_record_sha256
            or as_built.application_id != record.application_id
            or as_built.application_sha256 != record.application_sha256
            or as_built.variant_id != record.variant_id
            or as_built.variant_sha256 != record.variant_sha256
            or as_built.as_built_revision_id != record.as_built_revision_id
            or as_built.as_built_content_hash != record.as_built_content_hash
        ):
            raise ValueError('measured lifecycle as-built authority mismatch')

        with closing(self._connect()) as connection:
            self._require_campaign_authority(connection, record)

        evidence = []
        for ref in record.measurements:
            measurement = self.measurement_repository.get_measurement(
                ref.measurement_id
            )
            if measurement is None:
                raise ValueError(
                    'measured lifecycle measurement evidence disappeared'
                )
            dataset = self.measurement_repository.get_dataset(ref.dataset_id)
            if dataset is None:
                raise ValueError(
                    'measured lifecycle dataset evidence disappeared'
                )
            report = self.quality_repository.get_report(
                ref.quality_report_id
            )
            if report is None:
                raise ValueError(
                    'measured lifecycle quality report disappeared'
                )

            exact_ref = _measurement_evidence_ref(
                measurement=measurement,
                dataset=dataset,
                report=report,
            )
            if exact_ref != ref:
                raise ValueError(
                    'measured lifecycle evidence exact identity mismatch'
                )
            evidence.append((measurement, dataset, report))

        rebuilt = build_system_variant_measured_record(
            scene_repository=self.scene_repository,
            as_built_record=as_built,
            evidence=evidence,
            campaign_id=record.campaign_id,
            campaign_sha256=record.campaign_sha256,
            campaign_registration_id=record.campaign_registration_id,
            campaign_registration_sha256=record.campaign_registration_sha256,
            bound_at_utc=record.bound_at_utc,
            notes=record.notes,
        )
        if rebuilt != record:
            raise ValueError(
                'measured lifecycle record does not reproduce from exact authorities'
            )
        return record

    def _require_campaign_authority(
        self,
        connection: sqlite3.Connection,
        record: SystemVariantMeasuredRecord,
    ) -> None:
        """Re-resolve the persisted campaign/registration this record binds."""
        campaign = _authority_payload(
            connection,
            table='cad_system_variant_measurement_campaigns',
            key_column='campaign_id',
            key=record.campaign_id,
        )
        if campaign is None:
            raise ValueError('measured lifecycle campaign authority missing')
        if campaign.get('campaign_sha256') != record.campaign_sha256:
            raise ValueError('measured lifecycle campaign authority mismatch')
        registration = _authority_payload(
            connection,
            table='cad_system_variant_measurement_campaign_registrations',
            key_column='campaign_id',
            key=record.campaign_id,
        )
        if registration is None:
            raise ValueError(
                'measured lifecycle campaign registration authority missing'
            )
        if (
            registration.get('registration_id')
            != record.campaign_registration_id
            or registration.get('registration_sha256')
            != record.campaign_registration_sha256
            or registration.get('campaign_id') != record.campaign_id
            or registration.get('campaign_sha256') != record.campaign_sha256
        ):
            raise ValueError(
                'measured lifecycle campaign registration authority mismatch'
            )

    def _require_campaign_completion(
        self,
        connection: sqlite3.Connection,
        record: SystemVariantMeasuredRecord,
    ) -> None:
        """Require the persisted campaign completion authorizing this record.

        Measured lifecycle promotion is durable only under the preregistered
        campaign's exact completion, which re-binds this record's identity,
        the campaign identity/hash and the durable registration. The campaign
        repository's `complete_campaign` commits that completion row before
        this record inside the shared transaction, so the check holds inside
        the publishing transaction as well as on every later read.
        """
        completion = _authority_payload(
            connection,
            table='cad_system_variant_measurement_campaign_completions',
            key_column='campaign_id',
            key=record.campaign_id,
        )
        if completion is None:
            raise ValueError(
                'measured lifecycle campaign completion authority missing'
            )
        if (
            completion.get('measured_record_id') != record.record_id
            or completion.get('measured_record_sha256') != record.record_sha256
            or completion.get('campaign_sha256') != record.campaign_sha256
            or completion.get('campaign_registration_id')
            != record.campaign_registration_id
            or completion.get('campaign_registration_sha256')
            != record.campaign_registration_sha256
        ):
            raise ValueError(
                'measured lifecycle campaign completion authority mismatch'
            )

    def _save_in_transaction(
        self,
        connection: sqlite3.Connection,
        record: SystemVariantMeasuredRecord,
    ) -> SystemVariantMeasuredRecord:
        """Persist one validated measured record inside the caller's transaction.

        Used only by the O100G measurement campaign repository's atomic
        campaign completion, which commits plan completions, the exact
        campaign completion and this measured lifecycle record over the same
        native database. The campaign completion row must already exist in
        the same transaction, so a measured record can never be persisted
        without its preregistered campaign completion authority. The caller
        owns BEGIN/COMMIT/ROLLBACK and must have validated the record first;
        persisted rows were validated on commit.
        """
        self._require_campaign_completion(connection, record)
        row = connection.execute(
            """
            SELECT payload_json
            FROM cad_system_variant_measured
            WHERE record_id=?
            """,
            (record.record_id,),
        ).fetchone()
        if row is not None:
            persisted = SystemVariantMeasuredRecord.model_validate_json(
                row['payload_json']
            )
            if persisted != record:
                raise ValueError(
                    'SystemVariantMeasuredRecord id exists with different semantics'
                )
            return persisted

        connection.execute(
            """
            INSERT INTO cad_system_variant_measured(
                record_id,
                record_sha256,
                as_built_record_id,
                variant_id,
                as_built_revision_id,
                payload_json,
                recorded_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.record_id,
                record.record_sha256,
                record.as_built_record_id,
                record.variant_id,
                record.as_built_revision_id,
                record.model_dump_json(),
                record.bound_at_utc,
            ),
        )
        return record

    def get(
        self,
        record_id: str,
    ) -> SystemVariantMeasuredRecord | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_system_variant_measured
                WHERE record_id=?
                """,
                (record_id,),
            ).fetchone()
            if row is None:
                return None
            return self._validated_record(connection, row['payload_json'])

    def list_for_as_built(
        self,
        as_built_record_id: str,
    ) -> tuple[SystemVariantMeasuredRecord, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_system_variant_measured
                WHERE as_built_record_id=?
                ORDER BY seq ASC
                """,
                (as_built_record_id,),
            ).fetchall()

            return tuple(
                self._validated_record(connection, row['payload_json'])
                for row in rows
            )

    def _validated_record(
        self,
        connection: sqlite3.Connection,
        payload_json: str,
    ) -> SystemVariantMeasuredRecord:
        record = self._validate(
            SystemVariantMeasuredRecord.model_validate_json(payload_json)
        )
        self._require_campaign_completion(connection, record)
        return record
