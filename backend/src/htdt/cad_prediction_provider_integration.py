from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_measurement_repository import CadMeasurementRepository
from .cad_model_validation import (
    CadModelValidationRecord,
    EvidenceScope,
    build_model_validation,
)
from .cad_objective_models import CadObjectiveEvaluation, CadObjectiveInputRef
from .cad_objective_repository import CadObjectiveRepository
from .cad_objectives import build_objective_evaluation
from .cad_prediction_provider import (
    CadPredictionProviderRepository,
    LowBandPredictionProvider,
    PredictionProviderBinding,
    PredictionProviderRef,
    build_prediction_provider_binding,
)
from .cad_repository import SceneRevision
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
    connect_sqlite,

)
from .cad_search_models import CadSearchSpec
from .comparison import FrequencyResponse
from .optimization_objectives import (
    ObjectiveVector,
    ResponseObjectiveSpec,
    target_response_objectives,
)
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _digest


PROVIDER_OBJECTIVE_CONNECTION_AUTHORITY_VERSION = (
    'r170a-provider-objective-connection-1'
)






def _require_provider_band(
    provider: LowBandPredictionProvider,
    *,
    low_hz: float,
    high_hz: float,
) -> None:
    if high_hz <= low_hz:
        raise ValueError('provider consumer frequency band is invalid')
    domain = provider.valid_frequency_domain
    if (
        float(low_hz) < float(domain.minimum_hz)
        or float(high_hz) > float(domain.maximum_hz)
    ):
        raise ValueError('provider consumer frequency band exceeds exact valid band')


def provider_objective_input_ref(
    provider: LowBandPredictionProvider,
) -> CadObjectiveInputRef:
    provider.require_observable('frequency_response_magnitude')
    return CadObjectiveInputRef(
        evidence_class='predicted',
        source_kind='r170a_prediction_provider',
        source_id=provider.provider_id,
        source_sha256=provider.semantic_sha256,
    )


def provider_frequency_response(
    provider: LowBandPredictionProvider,
    *,
    receiver_id: str,
    low_hz: float,
    high_hz: float,
) -> FrequencyResponse:
    """N70/O-series typed read path; raw solver payload is not exposed."""

    provider.require_observable('frequency_response_magnitude')
    _require_provider_band(provider, low_hz=low_hz, high_hz=high_hz)
    response = provider.frequency_response(receiver_id)
    selected = [
        (frequency, level)
        for frequency, level in zip(
            response.frequency_hz,
            response.level_db,
            strict=True,
        )
        if low_hz <= frequency <= high_hz
    ]
    if len(selected) < 2:
        raise ValueError(
            'provider valid band has fewer than two response points for requested lane'
        )
    return FrequencyResponse(
        frequency_hz=tuple(item[0] for item in selected),
        level_db=tuple(item[1] for item in selected),
    )


def target_objective_evaluation_from_provider(
    *,
    revision: SceneRevision,
    search_spec: CadSearchSpec,
    candidate_id: str,
    provider: LowBandPredictionProvider,
    receiver_id: str,
    target: FrequencyResponse,
    objective_spec: ResponseObjectiveSpec,
    prefix: str = 'response',
) -> CadObjectiveEvaluation:
    """O30 typed objective adapter; unsupported capability never becomes a score."""

    authority = provider.current_authority
    if (
        authority.document_id != revision.document_id
        or authority.scene_revision_id != revision.revision_id
        or authority.scene_content_hash != revision.content_hash
    ):
        raise ValueError('objective provider belongs to another SceneRevision')
    _require_provider_band(
        provider,
        low_hz=objective_spec.low_hz,
        high_hz=objective_spec.high_hz,
    )
    response = provider_frequency_response(
        provider,
        receiver_id=receiver_id,
        low_hz=objective_spec.low_hz,
        high_hz=objective_spec.high_hz,
    )
    vector: ObjectiveVector = target_response_objectives(
        candidate_id,
        response,
        target,
        objective_spec,
        prefix=prefix,
    )
    evaluation_spec = {
        'authority': 'r170a-provider-objective-1',
        'provider_id': provider.provider_id,
        'provider_sha256': provider.semantic_sha256,
        'receiver_id': receiver_id,
        'observable': 'frequency_response_magnitude',
        'valid_band_hz': [
            float(provider.valid_frequency_domain.minimum_hz),
            float(provider.valid_frequency_domain.maximum_hz),
        ],
        'objective_spec': objective_spec.model_dump(mode='json'),
        'target': {
            'frequency_hz': list(target.frequency_hz),
            'level_db': list(target.level_db),
        },
    }
    return build_objective_evaluation(
        revision,
        search_spec,
        candidate_id,
        vector,
        evaluation_spec=evaluation_spec,
        input_refs=(provider_objective_input_ref(provider),),
    )


class PredictionProviderObjectiveConnection(BaseModel):
    """Exact persisted O30/O40 connection to one provider-backed evaluation."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r170a-provider-objective-connection-1'
    ] = PROVIDER_OBJECTIVE_CONNECTION_AUTHORITY_VERSION
    connection_id: str = Field(
        pattern=r'^r170a-provider-objective:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    provider_ref: PredictionProviderRef
    provider_binding_id: str = Field(
        pattern=r'^r170a-provider-binding:[0-9a-f]{64}$'
    )
    provider_binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    evaluation_id: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    receiver_id: str = Field(min_length=1)
    observable: Literal['frequency_response_magnitude'] = (
        'frequency_response_magnitude'
    )

    @model_validator(mode='after')
    def validate_identity(self) -> 'PredictionProviderObjectiveConnection':
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('provider objective connection hash mismatch')
        if self.connection_id != f'r170a-provider-objective:{expected}':
            raise ValueError('provider objective connection id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'connection_id', 'semantic_sha256'},
        )


def build_provider_objective_connection(
    provider: LowBandPredictionProvider,
    evaluation: CadObjectiveEvaluation,
    *,
    receiver_id: str,
) -> tuple[PredictionProviderBinding, PredictionProviderObjectiveConnection]:
    expected_ref = provider_objective_input_ref(provider)
    if expected_ref not in evaluation.input_refs:
        raise ValueError('objective evaluation is not bound to this provider')
    binding = build_prediction_provider_binding(
        provider,
        consumer_kind='O30_OBJECTIVE',
        consumer_id=evaluation.evaluation_id,
        consumer_semantic_sha256=evaluation.evaluation_sha256,
        required_observables=('frequency_response_magnitude',),
    )
    core = {
        'authority_version': PROVIDER_OBJECTIVE_CONNECTION_AUTHORITY_VERSION,
        'provider_ref': provider.ref().model_dump(mode='json'),
        'provider_binding_id': binding.binding_id,
        'provider_binding_sha256': binding.semantic_sha256,
        'evaluation_id': evaluation.evaluation_id,
        'evaluation_sha256': evaluation.evaluation_sha256,
        'receiver_id': receiver_id,
        'observable': 'frequency_response_magnitude',
    }
    digest = _digest(core)
    connection = PredictionProviderObjectiveConnection(
        connection_id=f'r170a-provider-objective:{digest}',
        semantic_sha256=digest,
        provider_ref=provider.ref(),
        provider_binding_id=binding.binding_id,
        provider_binding_sha256=binding.semantic_sha256,
        evaluation_id=evaluation.evaluation_id,
        evaluation_sha256=evaluation.evaluation_sha256,
        receiver_id=receiver_id,
    )
    return binding, connection


class CadPredictionProviderObjectiveRepository:
    def __init__(
        self,
        provider_repository: CadPredictionProviderRepository,
        objective_repository: CadObjectiveRepository,
    ) -> None:
        self.provider_repository = provider_repository
        self.objective_repository = objective_repository
        self.path = Path(provider_repository.path)
        if Path(objective_repository.path) != self.path:
            raise ValueError(
                'provider/objective repositories must share one native CAD database'
            )
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_prediction_provider_objectives')

    def _validate(
        self,
        connection: PredictionProviderObjectiveConnection,
    ) -> PredictionProviderObjectiveConnection:
        connection = PredictionProviderObjectiveConnection.model_validate(
            connection.model_dump(mode='python')
        )
        provider = self.provider_repository.get_provider(
            connection.provider_ref.provider_id
        )
        if provider is None or provider.ref() != connection.provider_ref:
            raise ValueError('objective connection provider is missing or changed')
        binding = self.provider_repository.get_binding(
            connection.provider_binding_id
        )
        if binding is None:
            raise ValueError('objective connection provider binding is missing')
        if (
            binding.semantic_sha256 != connection.provider_binding_sha256
            or binding.provider_ref != connection.provider_ref
            or binding.consumer_kind != 'O30_OBJECTIVE'
        ):
            raise ValueError('objective connection provider binding mismatch')
        evaluation = self.objective_repository.get_evaluation(
            connection.evaluation_id
        )
        if evaluation is None:
            raise ValueError('objective connection evaluation is missing')
        if evaluation.evaluation_sha256 != connection.evaluation_sha256:
            raise ValueError('objective connection evaluation hash mismatch')
        if (
            binding.consumer_id != evaluation.evaluation_id
            or binding.consumer_semantic_sha256 != evaluation.evaluation_sha256
        ):
            raise ValueError('objective connection consumer identity mismatch')
        if provider_objective_input_ref(provider) not in evaluation.input_refs:
            raise ValueError('objective evaluation lost provider evidence binding')
        provider.response(connection.receiver_id)
        return connection

    def save(
        self,
        connection: PredictionProviderObjectiveConnection,
    ) -> PredictionProviderObjectiveConnection:
        connection = self._validate(connection)
        with closing(self._connect()) as db, db:
            existing = db.execute(
                """
                SELECT payload_json
                FROM cad_prediction_provider_objectives
                WHERE connection_id=?
                """,
                (connection.connection_id,),
            ).fetchone()
            if existing is not None:
                persisted = PredictionProviderObjectiveConnection.model_validate_json(
                    existing['payload_json']
                )
                if persisted != connection:
                    raise ValueError(
                        'provider objective connection id exists with different semantics'
                    )
                return self._validate(persisted)
            db.execute(
                """
                INSERT INTO cad_prediction_provider_objectives(
                    connection_id,
                    semantic_sha256,
                    provider_id,
                    provider_binding_id,
                    evaluation_id,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    connection.connection_id,
                    connection.semantic_sha256,
                    connection.provider_ref.provider_id,
                    connection.provider_binding_id,
                    connection.evaluation_id,
                    connection.model_dump_json(),
                ),
            )
        return connection

    def get(
        self,
        connection_id: str,
    ) -> PredictionProviderObjectiveConnection | None:
        with closing(self._connect()) as db, db:
            row = db.execute(
                """
                SELECT payload_json
                FROM cad_prediction_provider_objectives
                WHERE connection_id=?
                """,
                (connection_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate(
            PredictionProviderObjectiveConnection.model_validate_json(
                row['payload_json']
            )
        )


def build_provider_measurement_validation(
    *,
    provider: LowBandPredictionProvider,
    receiver_id: str,
    measurement_repository: CadMeasurementRepository,
    measurement_id: str,
    document_id: str,
    search_spec_id: str,
    search_spec_sha256: str,
    candidate_set_sha256: str,
    candidate_id: str,
    split: Literal['calibration', 'holdout'],
    low_hz: float,
    high_hz: float,
    max_holdout_rms_db: float,
    evidence_scope: EvidenceScope = 'synthetic_fixture',
    campaign_id: str | None = None,
    campaign_sha256: str | None = None,
) -> CadModelValidationRecord:
    """O60 residual comparison using the typed provider, never a RoomSim disguise."""

    _require_provider_band(provider, low_hz=low_hz, high_hz=high_hz)
    authority = provider.current_authority
    if document_id != authority.document_id:
        raise ValueError('provider validation document identity mismatch')
    measurement = measurement_repository.get_measurement(measurement_id)
    if measurement is None:
        raise ValueError('provider validation measurement does not exist')
    if (
        measurement.document_id != authority.document_id
        or measurement.scene_revision_id != authority.scene_revision_id
        or measurement.scene_content_hash != authority.scene_content_hash
    ):
        raise ValueError(
            'provider validation measurement is not bound to the provider SceneRevision'
        )
    dataset = measurement_repository.dataset_for_measurement(measurement_id)
    if dataset is None:
        raise ValueError('provider validation measurement has no frequency response')
    predicted = provider_frequency_response(
        provider,
        receiver_id=receiver_id,
        low_hz=low_hz,
        high_hz=high_hz,
    )
    measured = FrequencyResponse(
        frequency_hz=dataset.frequency_hz,
        level_db=dataset.level_db,
    )
    return build_model_validation(
        document_id=document_id,
        search_spec_id=search_spec_id,
        search_spec_sha256=search_spec_sha256,
        candidate_set_sha256=candidate_set_sha256,
        campaign_id=campaign_id,
        campaign_sha256=campaign_sha256,
        model_id=provider.adapter_id,
        model_version=provider.adapter_version,
        samples=(
            (
                candidate_id,
                split,
                provider.provider_id,
                measurement_id,
                predicted,
                measured,
            ),
        ),
        low_hz=low_hz,
        high_hz=high_hz,
        max_holdout_rms_db=max_holdout_rms_db,
        evidence_scope=evidence_scope,
    )


def bind_provider_to_validation(
    provider: LowBandPredictionProvider,
    validation: CadModelValidationRecord,
) -> PredictionProviderBinding:
    if not any(
        pair.prediction_source_id == provider.provider_id
        for pair in validation.pairs
    ):
        raise ValueError('O60 validation does not reference this prediction provider')
    return build_prediction_provider_binding(
        provider,
        consumer_kind='O60_VALIDATION',
        consumer_id=validation.validation_id,
        consumer_semantic_sha256=validation.validation_sha256,
        required_observables=('frequency_response_magnitude',),
    )


def bind_provider_to_adaptive_validation(
    provider: LowBandPredictionProvider,
    validation: CadModelValidationRecord,
) -> PredictionProviderBinding:
    """O70 binds through the exact O60 residual authority, not solver payloads."""

    if not any(
        pair.prediction_source_id == provider.provider_id
        for pair in validation.pairs
    ):
        raise ValueError('O70 validation does not reference this prediction provider')
    return build_prediction_provider_binding(
        provider,
        consumer_kind='O70_ADAPTIVE',
        consumer_id=validation.validation_id,
        consumer_semantic_sha256=validation.validation_sha256,
        required_observables=('frequency_response_magnitude',),
    )
