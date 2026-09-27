from __future__ import annotations

from contextlib import closing
from math import isfinite
from pathlib import Path
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_hybrid_prediction_provider import (
    CadHybridPredictionProviderRepository,
    HybridPredictionProvider,
    HybridPredictionProviderBinding,
    HybridPredictionProviderRef,
    build_hybrid_provider_binding,
    hybrid_provider_frequency_response,
)
from .cad_measurement_loop import (
    CadMeasurementPlan,
    bind_measurement_plan_prediction,
)
from .cad_measurement_repository import CadMeasurementRepository
from .cad_model_validation import (
    CadModelValidationRecord,
    EvidenceScope,
    build_model_validation,
)
from .cad_objective_models import CadObjectiveEvaluation, CadObjectiveInputRef
from .cad_objective_repository import CadObjectiveRepository
from .cad_objectives import build_objective_evaluation
from .cad_repository import SceneRevision
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
)
from .cad_search_models import CadSearchSpec
from .comparison import FrequencyResponse
from .optimization_objectives import (
    ObjectiveVector,
    ResponseObjectiveSpec,
    target_response_objectives,
)
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _digest


HYBRID_PROVIDER_OBJECTIVE_CONNECTION_AUTHORITY_VERSION = (
    'r170b-hybrid-provider-objective-connection-1'
)
HYBRID_PROVIDER_OBJECTIVE_INPUT_AUTHORITY_VERSION = (
    'r170b-hybrid-provider-objective-input-1'
)






def _validate_band(low_hz: float, high_hz: float) -> tuple[float, float]:
    low = float(low_hz)
    high = float(high_hz)
    if (
        not isfinite(low)
        or not isfinite(high)
        or low <= 0.0
        or high <= low
    ):
        raise ValueError('R170B objective requested band is invalid')
    return low, high


class HybridPredictionProviderObjectiveInput(BaseModel):
    """Immutable O30 input authority over one exact provider/source/receiver/band."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r170b-hybrid-provider-objective-input-1'
    ] = HYBRID_PROVIDER_OBJECTIVE_INPUT_AUTHORITY_VERSION
    input_id: str = Field(
        pattern=r'^r170b-hybrid-objective-input:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    provider_ref: HybridPredictionProviderRef
    source_entity_id: str = Field(min_length=1)
    receiver_id: str = Field(min_length=1)
    observable: Literal['frequency_response_magnitude'] = (
        'frequency_response_magnitude'
    )
    requested_low_hz: float = Field(gt=0.0)
    requested_high_hz: float = Field(gt=0.0)

    @model_validator(mode='after')
    def validate_identity(self) -> 'HybridPredictionProviderObjectiveInput':
        if self.requested_high_hz <= self.requested_low_hz:
            raise ValueError('R170B objective input band requires low < high')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('R170B objective input semantic hash mismatch')
        if self.input_id != f'r170b-hybrid-objective-input:{expected}':
            raise ValueError('R170B objective input id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'input_id', 'semantic_sha256'},
        )

    def as_objective_input_ref(self) -> CadObjectiveInputRef:
        return CadObjectiveInputRef(
            evidence_class='predicted',
            source_kind='r170b_hybrid_prediction_provider',
            source_id=self.input_id,
        )


def build_hybrid_provider_objective_input(
    provider: HybridPredictionProvider,
    *,
    source_entity_id: str,
    receiver_id: str,
    low_hz: float,
    high_hz: float,
) -> HybridPredictionProviderObjectiveInput:
    low, high = _validate_band(low_hz, high_hz)
    hybrid_provider_frequency_response(
        provider,
        source_entity_id=source_entity_id,
        receiver_id=receiver_id,
        low_hz=low,
        high_hz=high,
    )
    core = {
        'authority_version': HYBRID_PROVIDER_OBJECTIVE_INPUT_AUTHORITY_VERSION,
        'provider_ref': provider.ref().model_dump(mode='json'),
        'source_entity_id': source_entity_id,
        'receiver_id': receiver_id,
        'observable': 'frequency_response_magnitude',
        'requested_low_hz': low,
        'requested_high_hz': high,
    }
    digest = _digest(core)
    return HybridPredictionProviderObjectiveInput(
        input_id=f'r170b-hybrid-objective-input:{digest}',
        semantic_sha256=digest,
        **core,
    )


def target_objective_evaluation_from_hybrid_provider(
    *,
    revision: SceneRevision,
    search_spec: CadSearchSpec,
    candidate_id: str,
    provider: HybridPredictionProvider,
    source_entity_id: str,
    receiver_id: str,
    target: FrequencyResponse,
    objective_spec: ResponseObjectiveSpec,
    prefix: str = 'response',
) -> CadObjectiveEvaluation:
    """O30 adapter. Existing objective math consumes only typed magnitude response."""

    authority = provider.base_current_authority
    if (
        authority.document_id != revision.document_id
        or authority.scene_revision_id != revision.revision_id
        or authority.scene_content_hash != revision.content_hash
    ):
        raise ValueError('R170B objective provider belongs to another SceneRevision')

    objective_input = build_hybrid_provider_objective_input(
        provider,
        source_entity_id=source_entity_id,
        receiver_id=receiver_id,
        low_hz=objective_spec.low_hz,
        high_hz=objective_spec.high_hz,
    )
    response = hybrid_provider_frequency_response(
        provider,
        source_entity_id=source_entity_id,
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
        'authority': 'r170b-hybrid-provider-objective-1',
        'provider_id': provider.provider_id,
        'provider_sha256': provider.semantic_sha256,
        'source_entity_id': source_entity_id,
        'receiver_id': receiver_id,
        'observable': 'frequency_response_magnitude',
        'requested_band_hz': [
            float(objective_spec.low_hz),
            float(objective_spec.high_hz),
        ],
        'objective_input_id': objective_input.input_id,
        'objective_input_sha256': objective_input.semantic_sha256,
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
        input_refs=(objective_input.as_objective_input_ref(),),
    )


class HybridPredictionProviderObjectiveConnection(BaseModel):
    """Exact persisted O30/O40 connection without changing Pareto semantics."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r170b-hybrid-provider-objective-connection-1'
    ] = HYBRID_PROVIDER_OBJECTIVE_CONNECTION_AUTHORITY_VERSION
    connection_id: str = Field(
        pattern=r'^r170b-hybrid-provider-objective:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    provider_ref: HybridPredictionProviderRef
    objective_input_id: str = Field(
        pattern=r'^r170b-hybrid-objective-input:[0-9a-f]{64}$'
    )
    objective_input_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_entity_id: str = Field(min_length=1)
    receiver_id: str = Field(min_length=1)
    observable: Literal['frequency_response_magnitude'] = (
        'frequency_response_magnitude'
    )
    requested_low_hz: float = Field(gt=0.0)
    requested_high_hz: float = Field(gt=0.0)
    evaluation_id: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def validate_identity(self) -> 'HybridPredictionProviderObjectiveConnection':
        if self.requested_high_hz <= self.requested_low_hz:
            raise ValueError('R170B objective connection band requires low < high')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('R170B objective connection semantic hash mismatch')
        if (
            self.connection_id
            != f'r170b-hybrid-provider-objective:{expected}'
        ):
            raise ValueError('R170B objective connection id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'connection_id', 'semantic_sha256'},
        )


def build_hybrid_provider_objective_connection(
    provider: HybridPredictionProvider,
    evaluation: CadObjectiveEvaluation,
    *,
    source_entity_id: str,
    receiver_id: str,
    low_hz: float,
    high_hz: float,
) -> HybridPredictionProviderObjectiveConnection:
    objective_input = build_hybrid_provider_objective_input(
        provider,
        source_entity_id=source_entity_id,
        receiver_id=receiver_id,
        low_hz=low_hz,
        high_hz=high_hz,
    )
    expected_ref = objective_input.as_objective_input_ref()
    if expected_ref not in evaluation.input_refs:
        raise ValueError(
            'R170B objective evaluation is not bound to this exact provider input'
        )
    core = {
        'authority_version': HYBRID_PROVIDER_OBJECTIVE_CONNECTION_AUTHORITY_VERSION,
        'provider_ref': provider.ref().model_dump(mode='json'),
        'objective_input_id': objective_input.input_id,
        'objective_input_sha256': objective_input.semantic_sha256,
        'source_entity_id': source_entity_id,
        'receiver_id': receiver_id,
        'observable': 'frequency_response_magnitude',
        'requested_low_hz': float(low_hz),
        'requested_high_hz': float(high_hz),
        'evaluation_id': evaluation.evaluation_id,
        'evaluation_sha256': evaluation.evaluation_sha256,
    }
    digest = _digest(core)
    return HybridPredictionProviderObjectiveConnection(
        connection_id=f'r170b-hybrid-provider-objective:{digest}',
        semantic_sha256=digest,
        **core,
    )


class CadHybridPredictionProviderObjectiveRepository:
    def __init__(
        self,
        provider_repository: CadHybridPredictionProviderRepository,
        objective_repository: CadObjectiveRepository,
    ) -> None:
        self.provider_repository = provider_repository
        self.objective_repository = objective_repository
        self.path = Path(provider_repository.path)
        if Path(objective_repository.path) != self.path:
            raise ValueError(
                'R170B provider/objective repositories must share one native CAD database'
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
            require_native_tables(connection, 'cad_hybrid_prediction_provider_objectives')

    def _validate(
        self,
        connection: HybridPredictionProviderObjectiveConnection,
    ) -> HybridPredictionProviderObjectiveConnection:
        connection = HybridPredictionProviderObjectiveConnection.model_validate(
            connection.model_dump(mode='python')
        )
        provider = self.provider_repository.get(
            connection.provider_ref.provider_id
        )
        if provider is None or provider.ref() != connection.provider_ref:
            raise ValueError('R170B objective connection provider is missing/stale')

        expected_input = build_hybrid_provider_objective_input(
            provider,
            source_entity_id=connection.source_entity_id,
            receiver_id=connection.receiver_id,
            low_hz=connection.requested_low_hz,
            high_hz=connection.requested_high_hz,
        )
        if (
            expected_input.input_id != connection.objective_input_id
            or expected_input.semantic_sha256
            != connection.objective_input_sha256
        ):
            raise ValueError('R170B objective input authority mismatch')

        evaluation = self.objective_repository.get_evaluation(
            connection.evaluation_id
        )
        if evaluation is None:
            raise ValueError('R170B objective connection evaluation is missing')
        if evaluation.evaluation_sha256 != connection.evaluation_sha256:
            raise ValueError('R170B objective connection evaluation hash mismatch')
        if expected_input.as_objective_input_ref() not in evaluation.input_refs:
            raise ValueError('R170B objective evaluation lost exact provider input')
        return connection

    def save(
        self,
        connection: HybridPredictionProviderObjectiveConnection,
    ) -> HybridPredictionProviderObjectiveConnection:
        connection = self._validate(connection)
        with closing(self._connect()) as db, db:
            existing = db.execute(
                """
                SELECT payload_json
                FROM cad_hybrid_prediction_provider_objectives
                WHERE connection_id=?
                """,
                (connection.connection_id,),
            ).fetchone()
            if existing is not None:
                persisted = (
                    HybridPredictionProviderObjectiveConnection.model_validate_json(
                        existing['payload_json']
                    )
                )
                if persisted != connection:
                    raise ValueError(
                        'R170B objective connection id exists with different semantics'
                    )
                return self._validate(persisted)
            db.execute(
                """
                INSERT INTO cad_hybrid_prediction_provider_objectives(
                    connection_id,
                    semantic_sha256,
                    provider_id,
                    objective_input_id,
                    evaluation_id,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    connection.connection_id,
                    connection.semantic_sha256,
                    connection.provider_ref.provider_id,
                    connection.objective_input_id,
                    connection.evaluation_id,
                    connection.model_dump_json(),
                ),
            )
        return connection

    def get(
        self,
        connection_id: str,
    ) -> HybridPredictionProviderObjectiveConnection | None:
        with closing(self._connect()) as db:
            row = db.execute(
                """
                SELECT payload_json
                FROM cad_hybrid_prediction_provider_objectives
                WHERE connection_id=?
                """,
                (connection_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate(
            HybridPredictionProviderObjectiveConnection.model_validate_json(
                row['payload_json']
            )
        )


def bind_measurement_plan_hybrid_prediction(
    plan: CadMeasurementPlan,
    binding: HybridPredictionProviderBinding,
) -> CadMeasurementPlan:
    """Bind O50 planning to one exact R170B hybrid prediction authority."""

    if binding.consumer_kind != 'O50_MEASUREMENT_PLAN':
        raise ValueError('measurement plan requires an O50 hybrid-provider binding')
    return bind_measurement_plan_prediction(plan, binding)


def build_hybrid_provider_measurement_validation(
    *,
    provider: HybridPredictionProvider,
    source_entity_id: str,
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
    """O60 residual comparison using the typed hybrid provider."""

    _validate_band(low_hz, high_hz)
    authority = provider.base_current_authority
    if document_id != authority.document_id:
        raise ValueError('R170B validation document identity mismatch')
    measurement = measurement_repository.get_measurement(measurement_id)
    if measurement is None:
        raise ValueError('R170B validation measurement does not exist')
    if (
        measurement.document_id != authority.document_id
        or measurement.scene_revision_id != authority.scene_revision_id
        or measurement.scene_content_hash != authority.scene_content_hash
    ):
        raise ValueError(
            'R170B validation measurement is not bound to the provider SceneRevision'
        )
    dataset = measurement_repository.dataset_for_measurement(measurement_id)
    if dataset is None:
        raise ValueError('R170B validation measurement has no frequency response')
    predicted = hybrid_provider_frequency_response(
        provider,
        source_entity_id=source_entity_id,
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


def bind_hybrid_provider_to_validation(
    provider: HybridPredictionProvider,
    validation: CadModelValidationRecord,
) -> HybridPredictionProviderBinding:
    if not any(
        pair.prediction_source_id == provider.provider_id
        for pair in validation.pairs
    ):
        raise ValueError('O60 validation does not reference this hybrid provider')
    return build_hybrid_provider_binding(
        provider,
        consumer_kind='O60_VALIDATION',
        consumer_id=validation.validation_id,
        consumer_semantic_sha256=validation.validation_sha256,
        required_observables=('frequency_response_magnitude',),
    )


def bind_hybrid_provider_to_adaptive_validation(
    provider: HybridPredictionProvider,
    validation: CadModelValidationRecord,
) -> HybridPredictionProviderBinding:
    """O70 binds through the exact O60 residual authority, not solver payloads."""

    if not any(
        pair.prediction_source_id == provider.provider_id
        for pair in validation.pairs
    ):
        raise ValueError('O70 validation does not reference this hybrid provider')
    return build_hybrid_provider_binding(
        provider,
        consumer_kind='O70_ADAPTIVE',
        consumer_id=validation.validation_id,
        consumer_semantic_sha256=validation.validation_sha256,
        required_observables=('frequency_response_magnitude',),
    )
