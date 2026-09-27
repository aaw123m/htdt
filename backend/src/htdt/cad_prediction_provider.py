from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from math import atan2, degrees, hypot, isfinite, log10
from pathlib import Path
import sqlite3
from typing import Any, Literal, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_acoustic_snapshot import (
    AcousticPredictionRequest,
    AcousticReceiverBinding,
    AcousticSceneSnapshot,
    AcousticSceneSourceBinding,
    SnapshotEnvironmentAuthorityRef,
)
from .cad_acoustic_solver_result import AcousticSolverResultEnvelope
from .cad_equipment import FrequencyDomain
from .cad_repository import SceneRepository, SceneRevision
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
    connect_sqlite,
)
from .comparison import FrequencyResponse
from .r120_geometry_compiler import ExactExternalAuthorityRef
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _semantic_hash


PREDICTION_PROVIDER_SCHEMA_VERSION = 1
PREDICTION_PROVIDER_AUTHORITY_VERSION = 'r170a-low-band-provider-1'
PREDICTION_PROVIDER_ADAPTER_ID = 'htdt.r170a.r130_complex_pressure'
PREDICTION_PROVIDER_ADAPTER_VERSION = '1'
PREDICTION_PROVIDER_BINDING_AUTHORITY_VERSION = 'r170a-provider-binding-1'

ProviderEvidenceState = Literal['candidate', 'validated', 'production']
ProviderEvidenceScope = Literal['unvalidated', 'synthetic_fixture', 'owned_room']
ProviderCapabilityState = Literal['READY', 'UNSUPPORTED']
ProviderStaleState = Literal['CURRENT', 'STALE']
ProviderConsumerKind = Literal[
    'N70_PRODUCT_PREDICTION',
    'O30_OBJECTIVE',
    'O40_PARETO',
    'O50_MEASUREMENT_PLAN',
    'O60_VALIDATION',
    'O70_ADAPTIVE',
]

R170A_OBSERVABLES = (
    'frequency_response_magnitude',
    'frequency_response_phase',
    'impulse_response',
    'rt60',
    'edt',
    'c50',
    'c80',
    'arrival_timing',
    'spatial_pressure_field',
    'broadband_hybrid',
)


ExternalPayloadResolver = Callable[[ExactExternalAuthorityRef], object]


class SnapshotRequestResolver(Protocol):
    path: Path

    def get_snapshot(self, snapshot_id: str) -> AcousticSceneSnapshot | None:
        ...

    def get_prediction_request(
        self,
        request_id: str,
    ) -> AcousticPredictionRequest | None:
        ...


class SolverResultResolver(Protocol):
    path: Path

    def get(self, result_id: str) -> AcousticSolverResultEnvelope | None:
        ...






def _model_hash(model: BaseModel) -> str:
    return _semantic_hash(model.model_dump(mode='json'))


def _domain_equal(left: FrequencyDomain, right: FrequencyDomain) -> bool:
    return (
        float(left.minimum_hz) == float(right.minimum_hz)
        and float(left.maximum_hz) == float(right.maximum_hz)
    )


def _domain_contains(domain: FrequencyDomain, low_hz: float, high_hz: float) -> bool:
    return (
        float(low_hz) >= float(domain.minimum_hz)
        and float(high_hz) <= float(domain.maximum_hz)
    )


class PredictionProviderCapability(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    observable: str = Field(min_length=1)
    state: ProviderCapabilityState
    reason: str | None = None

    @model_validator(mode='after')
    def validate_state(self) -> 'PredictionProviderCapability':
        if self.state == 'READY' and self.reason is not None:
            raise ValueError('READY provider capability cannot carry an unsupported reason')
        if self.state == 'UNSUPPORTED' and not self.reason:
            raise ValueError('UNSUPPORTED provider capability requires an explicit reason')
        return self


class PredictionProviderSourceIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    source_binding: AcousticSceneSourceBinding
    source_binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    wave_excitation_binding_id: str | None = None
    wave_excitation_binding_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )

    @model_validator(mode='after')
    def validate_identity(self) -> 'PredictionProviderSourceIdentity':
        if self.source_binding_sha256 != _model_hash(self.source_binding):
            raise ValueError('provider source binding hash mismatch')
        if (self.wave_excitation_binding_id is None) != (
            self.wave_excitation_binding_sha256 is None
        ):
            raise ValueError('wave excitation binding id/hash must be supplied together')
        return self


class PredictionProviderReceiverIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    receiver_binding: AcousticReceiverBinding
    receiver_binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def validate_identity(self) -> 'PredictionProviderReceiverIdentity':
        if self.receiver_binding_sha256 != _model_hash(self.receiver_binding):
            raise ValueError('provider receiver binding hash mismatch')
        return self


class PredictionProviderEnvironmentIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    environment: SnapshotEnvironmentAuthorityRef
    environment_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def validate_identity(self) -> 'PredictionProviderEnvironmentIdentity':
        if self.environment_sha256 != _model_hash(self.environment):
            raise ValueError('provider environment hash mismatch')
        return self


class LowBandReceiverResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    receiver_id: str = Field(min_length=1)
    receiver_entity_id: str = Field(min_length=1)
    frequency_hz: tuple[float, ...] = Field(min_length=2)
    magnitude_pa: tuple[float, ...] = Field(min_length=2)
    magnitude_db_spl: tuple[float, ...] = Field(min_length=2)
    phase_deg: tuple[float, ...] | None = None
    pressure_reference_pa: float = Field(default=20.0e-6, gt=0.0)
    phase_convention: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_arrays(self) -> 'LowBandReceiverResponse':
        count = len(self.frequency_hz)
        if len(self.magnitude_pa) != count or len(self.magnitude_db_spl) != count:
            raise ValueError('provider magnitude arrays must match frequency axis')
        if self.phase_deg is not None and len(self.phase_deg) != count:
            raise ValueError('provider phase array must match frequency axis')
        if tuple(self.frequency_hz) != tuple(sorted(set(self.frequency_hz))):
            raise ValueError('provider frequency axis must be sorted and unique')
        if any(
            not isfinite(float(value)) or float(value) <= 0.0
            for value in self.frequency_hz
        ):
            raise ValueError('provider frequencies must be finite and positive')
        if any(
            not isfinite(float(value)) or float(value) <= 0.0
            for value in self.magnitude_pa
        ):
            raise ValueError(
                'provider logarithmic magnitude capability requires positive finite pressure'
            )
        if any(not isfinite(float(value)) for value in self.magnitude_db_spl):
            raise ValueError('provider dB magnitudes must be finite')
        if self.phase_deg is not None and any(
            not isfinite(float(value)) for value in self.phase_deg
        ):
            raise ValueError('provider phase values must be finite')
        return self


class ProviderCurrentAuthority(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    acoustic_scene_snapshot_id: str = Field(min_length=1)
    acoustic_scene_snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    prediction_request_id: str = Field(min_length=1)
    prediction_request_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    prediction_deterministic_input_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    solver_result_id: str = Field(min_length=1)
    solver_result_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    deterministic_solver_input_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    solver_implementation_ref: ExactExternalAuthorityRef
    source_entity_id: str = Field(min_length=1)
    receiver_ids: tuple[str, ...] = Field(min_length=1)
    valid_frequency_domain: FrequencyDomain

    @model_validator(mode='after')
    def validate_receivers(self) -> 'ProviderCurrentAuthority':
        if len(self.receiver_ids) != len(set(self.receiver_ids)):
            raise ValueError('provider current authority receiver ids must be unique')
        return self


class PredictionProviderRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    provider_id: str = Field(pattern=r'^r170a-provider:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class LowBandPredictionProvider(BaseModel):
    """Solver-neutral product authority projected from one exact R130 result.

    The raw solver artifact is parsed only by the R170A adapter. Product layers
    consume this typed contract and never need to understand the underlying
    solver payload or RoomSim attempt representation.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = PREDICTION_PROVIDER_SCHEMA_VERSION
    authority_version: Literal[
        'r170a-low-band-provider-1'
    ] = PREDICTION_PROVIDER_AUTHORITY_VERSION
    adapter_id: Literal[
        'htdt.r170a.r130_complex_pressure'
    ] = PREDICTION_PROVIDER_ADAPTER_ID
    adapter_version: Literal['1'] = PREDICTION_PROVIDER_ADAPTER_VERSION

    provider_id: str = Field(pattern=r'^r170a-provider:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    evidence_state: ProviderEvidenceState
    evidence_scope: ProviderEvidenceScope
    validation_authority_ref: ExactExternalAuthorityRef | None = None
    production_adoption_authority_ref: ExactExternalAuthorityRef | None = None
    stale_state: Literal['CURRENT'] = 'CURRENT'

    current_authority: ProviderCurrentAuthority
    source_identity: PredictionProviderSourceIdentity
    receiver_identities: tuple[PredictionProviderReceiverIdentity, ...] = Field(
        min_length=1
    )
    environment_identity: PredictionProviderEnvironmentIdentity

    observable_capabilities: tuple[PredictionProviderCapability, ...] = Field(
        min_length=1
    )
    magnitude_capability: ProviderCapabilityState
    phase_capability: ProviderCapabilityState
    timing_capability: ProviderCapabilityState
    spatial_field_capability: ProviderCapabilityState

    # #942: coherent-composition semantics. Phase availability alone does
    # not prove transfers can be coherently summed — the matrix machine-
    # checks these declared identities instead of inferring them.
    source_normalization_id: str | None = Field(default=None, min_length=1)
    timing_authority: Literal[
        'absolute_propagation_time', 'relative_delay', 'unavailable'
    ] | None = None
    phasor_convention: str | None = Field(default=None, min_length=1)

    response_unit: Literal['Pa'] = 'Pa'
    magnitude_level_reference: Literal['20_uPa'] = '20_uPa'
    valid_frequency_domain: FrequencyDomain
    receiver_responses: tuple[LowBandReceiverResponse, ...] = Field(min_length=1)

    result_envelope_id: str = Field(min_length=1)
    result_envelope_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    result_artifact_ref: ExactExternalAuthorityRef
    result_artifact_schema_ref: ExactExternalAuthorityRef
    execution_provenance_ref: ExactExternalAuthorityRef

    @model_validator(mode='after')
    def validate_identity(self) -> 'LowBandPredictionProvider':
        receiver_ids = tuple(
            item.receiver_binding.receiver_id for item in self.receiver_identities
        )
        response_ids = tuple(item.receiver_id for item in self.receiver_responses)
        if receiver_ids != response_ids:
            raise ValueError('provider response receiver order mismatch')
        if receiver_ids != self.current_authority.receiver_ids:
            raise ValueError('provider current authority receiver set mismatch')
        capabilities = [item.observable for item in self.observable_capabilities]
        if len(capabilities) != len(set(capabilities)):
            raise ValueError('provider observable capabilities must be unique')
        expected_states = {
            item.observable: item.state for item in self.observable_capabilities
        }
        if expected_states.get('frequency_response_magnitude') != self.magnitude_capability:
            raise ValueError('provider magnitude capability summary mismatch')
        if expected_states.get('frequency_response_phase') != self.phase_capability:
            raise ValueError('provider phase capability summary mismatch')
        if expected_states.get('arrival_timing') != self.timing_capability:
            raise ValueError('provider timing capability summary mismatch')
        if expected_states.get('spatial_pressure_field') != self.spatial_field_capability:
            raise ValueError('provider spatial-field capability summary mismatch')
        if self.current_authority.valid_frequency_domain != self.valid_frequency_domain:
            raise ValueError('provider valid band/current authority mismatch')
        if self.current_authority.solver_result_id != self.result_envelope_id:
            raise ValueError('provider current authority result id mismatch')
        if self.current_authority.solver_result_sha256 != self.result_envelope_sha256:
            raise ValueError('provider current authority result hash mismatch')
        if self.evidence_state == 'candidate':
            if self.evidence_scope != 'unvalidated':
                raise ValueError('candidate provider must remain unvalidated')
            if self.validation_authority_ref is not None:
                raise ValueError('candidate provider must not claim validation authority')
            if self.production_adoption_authority_ref is not None:
                raise ValueError('candidate provider must not claim production adoption')
        elif self.validation_authority_ref is None:
            raise ValueError('validated/production provider requires validation authority')
        elif self.evidence_scope == 'unvalidated':
            raise ValueError('validated/production provider requires explicit evidence scope')
        if self.evidence_state == 'production' and self.evidence_scope != 'owned_room':
            raise ValueError('production provider requires owned-room evidence scope')
        if (
            self.evidence_state == 'production'
            and self.production_adoption_authority_ref is None
        ):
            raise ValueError('production provider requires production adoption authority')
        if (
            self.evidence_state != 'production'
            and self.production_adoption_authority_ref is not None
        ):
            raise ValueError('non-production provider cannot carry adoption authority')
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('R170A provider semantic hash mismatch')
        if self.provider_id != f'r170a-provider:{expected}':
            raise ValueError('R170A provider id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            mode='json',
            exclude={'provider_id', 'semantic_sha256'},
        )
        # #942: fields absent on pre-composition providers must digest as if
        # they did not exist, so persisted identities stay byte-exact.
        for key in (
            'source_normalization_id',
            'timing_authority',
            'phasor_convention',
        ):
            if getattr(self, key) is None:
                payload.pop(key, None)
        return payload

    def ref(self) -> PredictionProviderRef:
        return PredictionProviderRef(
            provider_id=self.provider_id,
            semantic_sha256=self.semantic_sha256,
        )

    def capability(self, observable: str) -> PredictionProviderCapability:
        for capability in self.observable_capabilities:
            if capability.observable == observable:
                return capability
        return PredictionProviderCapability(
            observable=observable,
            state='UNSUPPORTED',
            reason='observable is outside the bounded R170A provider contract',
        )

    def require_observable(self, observable: str) -> None:
        capability = self.capability(observable)
        if capability.state != 'READY':
            raise ValueError(
                f'prediction provider observable is unsupported: '
                f'{observable}: {capability.reason}'
            )

    def response(self, receiver_id: str) -> LowBandReceiverResponse:
        for response in self.receiver_responses:
            if response.receiver_id == receiver_id:
                return response
        raise ValueError(f'prediction provider receiver mismatch: {receiver_id}')

    def frequency_response(self, receiver_id: str) -> FrequencyResponse:
        self.require_observable('frequency_response_magnitude')
        response = self.response(receiver_id)
        return FrequencyResponse(
            frequency_hz=response.frequency_hz,
            level_db=response.magnitude_db_spl,
        )


class PredictionProviderResolution(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    provider_ref: PredictionProviderRef
    stale_state: ProviderStaleState
    reasons: tuple[str, ...]

    @model_validator(mode='after')
    def validate_state(self) -> 'PredictionProviderResolution':
        if self.stale_state == 'CURRENT' and self.reasons:
            raise ValueError('CURRENT provider resolution cannot carry stale reasons')
        if self.stale_state == 'STALE' and not self.reasons:
            raise ValueError('STALE provider resolution requires explicit reasons')
        return self


class PredictionProviderBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r170a-provider-binding-1'
    ] = PREDICTION_PROVIDER_BINDING_AUTHORITY_VERSION
    binding_id: str = Field(pattern=r'^r170a-provider-binding:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    provider_ref: PredictionProviderRef
    consumer_kind: ProviderConsumerKind
    consumer_id: str = Field(min_length=1)
    consumer_semantic_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    required_observables: tuple[str, ...] = Field(min_length=1)
    expected_authority: ProviderCurrentAuthority
    stale_state: Literal['CURRENT'] = 'CURRENT'

    @model_validator(mode='after')
    def validate_identity(self) -> 'PredictionProviderBinding':
        if len(self.required_observables) != len(set(self.required_observables)):
            raise ValueError('provider binding observables must be unique')
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('provider binding semantic hash mismatch')
        if self.binding_id != f'r170a-provider-binding:{expected}':
            raise ValueError('provider binding id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'binding_id', 'semantic_sha256'},
        )


def _external_payload(
    resolver: ExternalPayloadResolver,
    ref: ExactExternalAuthorityRef,
    *,
    label: str,
) -> object:
    try:
        payload = resolver(ref)
    except Exception as exc:
        raise ValueError(f'{label} exact external authority is unavailable') from exc
    if _semantic_hash(payload) != ref.semantic_hash_sha256:
        raise ValueError(f'{label} exact external authority hash mismatch')
    return payload


def provider_current_authority(
    *,
    revision: SceneRevision,
    snapshot: AcousticSceneSnapshot,
    request: AcousticPredictionRequest,
    result: AcousticSolverResultEnvelope,
    source_entity_id: str,
    receiver_ids: Sequence[str],
    valid_frequency_domain: FrequencyDomain,
) -> ProviderCurrentAuthority:
    return ProviderCurrentAuthority(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        acoustic_scene_snapshot_id=snapshot.snapshot_id,
        acoustic_scene_snapshot_sha256=snapshot.semantic_sha256,
        prediction_request_id=request.request_id,
        prediction_request_sha256=request.request_semantic_sha256,
        prediction_deterministic_input_hash=request.deterministic_input_hash,
        solver_result_id=result.result_id,
        solver_result_sha256=result.semantic_sha256,
        deterministic_solver_input_hash=result.deterministic_solver_input_hash,
        solver_implementation_ref=result.solver_implementation_ref,
        source_entity_id=source_entity_id,
        receiver_ids=tuple(receiver_ids),
        valid_frequency_domain=valid_frequency_domain,
    )


def assess_provider_current(
    provider: LowBandPredictionProvider,
    current: ProviderCurrentAuthority,
) -> PredictionProviderResolution:
    expected = provider.current_authority
    reasons: list[str] = []
    checks = (
        ('SceneRevision', expected.scene_revision_id, current.scene_revision_id),
        ('scene content', expected.scene_content_hash, current.scene_content_hash),
        (
            'AcousticSceneSnapshot',
            expected.acoustic_scene_snapshot_id,
            current.acoustic_scene_snapshot_id,
        ),
        (
            'AcousticSceneSnapshot hash',
            expected.acoustic_scene_snapshot_sha256,
            current.acoustic_scene_snapshot_sha256,
        ),
        ('prediction request', expected.prediction_request_id, current.prediction_request_id),
        (
            'prediction request hash',
            expected.prediction_request_sha256,
            current.prediction_request_sha256,
        ),
        (
            'prediction deterministic input',
            expected.prediction_deterministic_input_hash,
            current.prediction_deterministic_input_hash,
        ),
        ('solver result', expected.solver_result_id, current.solver_result_id),
        (
            'solver result hash',
            expected.solver_result_sha256,
            current.solver_result_sha256,
        ),
        (
            'solver input',
            expected.deterministic_solver_input_hash,
            current.deterministic_solver_input_hash,
        ),
        ('source', expected.source_entity_id, current.source_entity_id),
        ('receivers', expected.receiver_ids, current.receiver_ids),
        (
            'valid band',
            expected.valid_frequency_domain.model_dump(mode='json'),
            current.valid_frequency_domain.model_dump(mode='json'),
        ),
        (
            'solver implementation',
            expected.solver_implementation_ref.model_dump(mode='json'),
            current.solver_implementation_ref.model_dump(mode='json'),
        ),
    )
    for label, left, right in checks:
        if left != right:
            reasons.append(f'{label} changed')
    return PredictionProviderResolution(
        provider_ref=provider.ref(),
        stale_state='STALE' if reasons else 'CURRENT',
        reasons=tuple(reasons),
    )


def require_provider_current(
    provider: LowBandPredictionProvider,
    current: ProviderCurrentAuthority,
) -> None:
    resolution = assess_provider_current(provider, current)
    if resolution.stale_state != 'CURRENT':
        raise ValueError(
            'prediction provider is stale: ' + ', '.join(resolution.reasons)
        )


def build_r130_low_band_prediction_provider(
    *,
    revision: SceneRevision,
    snapshot: AcousticSceneSnapshot,
    request: AcousticPredictionRequest,
    result: AcousticSolverResultEnvelope,
    external_payload_resolver: ExternalPayloadResolver,
    evidence_state: ProviderEvidenceState = 'candidate',
    evidence_scope: ProviderEvidenceScope = 'unvalidated',
    validation_authority_ref: ExactExternalAuthorityRef | None = None,
    production_adoption_authority_ref: ExactExternalAuthorityRef | None = None,
) -> LowBandPredictionProvider:
    if (
        snapshot.document_id != revision.document_id
        or snapshot.scene_revision_id != revision.revision_id
        or snapshot.scene_content_hash != revision.content_hash
    ):
        raise ValueError('R170A snapshot/SceneRevision identity mismatch')
    if len(snapshot.sources) != 1:
        raise ValueError('R170A bounded lane requires exactly one source')
    if snapshot.environment is None:
        raise ValueError('R170A requires exact environment identity')
    if not snapshot.receivers:
        raise ValueError('R170A requires an explicit receiver set')
    if (
        request.acoustic_scene_snapshot_id != snapshot.snapshot_id
        or request.acoustic_scene_snapshot_sha256 != snapshot.semantic_sha256
    ):
        raise ValueError('R170A request/snapshot identity mismatch')
    if request.requested_observables != ('complex_pressure',):
        raise ValueError(
            'R170A bounded R130 adapter requires exactly complex_pressure'
        )
    if (
        result.prediction_request_id != request.request_id
        or result.prediction_request_semantic_sha256
        != request.request_semantic_sha256
        or result.prediction_deterministic_input_hash
        != request.deterministic_input_hash
        or result.acoustic_scene_snapshot_id != snapshot.snapshot_id
        or result.acoustic_scene_snapshot_sha256 != snapshot.semantic_sha256
    ):
        raise ValueError('R170A result/request/snapshot identity mismatch')

    artifacts = tuple(
        item for item in result.artifacts if item.observable == 'complex_pressure'
    )
    if len(artifacts) != 1 or len(result.artifacts) != 1:
        raise ValueError('R170A bounded lane requires one complex-pressure artifact')
    artifact = artifacts[0]
    if not _domain_equal(
        artifact.valid_frequency_domain,
        request.requested_frequency_domain,
    ):
        raise ValueError('R170A solver result valid-band mismatch')

    artifact_payload = _external_payload(
        external_payload_resolver,
        artifact.artifact_authority,
        label='complex-pressure artifact',
    )
    schema_payload = _external_payload(
        external_payload_resolver,
        artifact.encoding_schema_ref,
        label='complex-pressure artifact schema',
    )
    provenance_payload = _external_payload(
        external_payload_resolver,
        result.execution_provenance_ref,
        label='solver execution provenance',
    )
    if not isinstance(artifact_payload, dict):
        raise ValueError('complex-pressure artifact payload must be an object')
    if not isinstance(schema_payload, dict):
        raise ValueError('complex-pressure schema payload must be an object')
    if not isinstance(provenance_payload, dict):
        raise ValueError('solver execution provenance payload must be an object')
    if (
        artifact_payload.get('quantity_type') != 'complex_pressure'
        or schema_payload.get('quantity_type') != 'complex_pressure'
    ):
        raise ValueError('R170A requires explicit complex-pressure quantity authority')

    payload_domain = artifact_payload.get('valid_domain')
    if payload_domain != artifact.valid_frequency_domain.model_dump(mode='json'):
        raise ValueError('R170A artifact payload valid-band mismatch')
    if provenance_payload.get('solver_implementation_ref') != (
        result.solver_implementation_ref.model_dump(mode='json')
    ):
        raise ValueError('R170A solver implementation/provenance mismatch')
    if provenance_payload.get('solver_configuration_ref') != (
        result.solver_configuration_ref.model_dump(mode='json')
    ):
        raise ValueError('R170A solver configuration/provenance mismatch')
    artifact_input_hash = artifact_payload.get('candidate_execution_input_sha256')
    provenance_input_hash = provenance_payload.get('execution_input_sha256')
    if (
        artifact_input_hash is not None
        and provenance_input_hash is not None
        and artifact_input_hash != provenance_input_hash
    ):
        raise ValueError('R170A solver-input provenance mismatch')

    source = snapshot.sources[0]
    artifact_source = artifact_payload.get('source_authority')
    if not isinstance(artifact_source, dict):
        raise ValueError('R170A artifact is missing exact source authority')
    if artifact_source.get('r110_compiled_source_sha256') != (
        source.r110_compiled_source_sha256
    ):
        raise ValueError('R170A source mismatch')

    wave_bindings = tuple(
        item
        for item in snapshot.wave_source_excitation_bindings
        if item.source_entity_id == source.source_entity_id
    )
    if len(wave_bindings) > 1:
        raise ValueError('R170A source has ambiguous wave-excitation bindings')
    wave_binding = wave_bindings[0] if wave_bindings else None
    if wave_binding is not None and artifact_source.get(
        'wave_excitation_binding_sha256'
    ) != wave_binding.semantic_sha256:
        raise ValueError('R170A wave-excitation source mismatch')

    receiver_payload = artifact_payload.get('receiver_identity_order')
    expected_receivers = [
        {
            'receiver_id': item.receiver_id,
            'entity_id': item.entity_id,
            'position_m': [
                float(item.world_position.x_m),
                float(item.world_position.y_m),
                float(item.world_position.z_m),
            ],
        }
        for item in snapshot.receivers
    ]
    if receiver_payload != expected_receivers:
        raise ValueError('R170A receiver identity/set mismatch')

    frequencies_raw = artifact_payload.get('frequency_axis_hz')
    real_raw = artifact_payload.get('pressure_real_pa')
    imag_raw = artifact_payload.get('pressure_imag_pa')
    if not isinstance(frequencies_raw, list) or len(frequencies_raw) < 2:
        raise ValueError('R170A complex-pressure artifact frequency axis is invalid')
    frequencies = tuple(float(value) for value in frequencies_raw)
    if (
        tuple(frequencies) != tuple(sorted(set(frequencies)))
        or any(not isfinite(value) or value <= 0.0 for value in frequencies)
    ):
        raise ValueError('R170A complex-pressure frequencies must be finite/sorted/unique')
    if not _domain_contains(
        artifact.valid_frequency_domain,
        frequencies[0],
        frequencies[-1],
    ):
        raise ValueError('R170A complex-pressure samples exceed valid band')
    if (
        not isinstance(real_raw, list)
        or not isinstance(imag_raw, list)
        or len(real_raw) != len(snapshot.receivers)
        or len(imag_raw) != len(snapshot.receivers)
    ):
        raise ValueError('R170A complex-pressure receiver dimensions mismatch')

    complex_meta = artifact_payload.get('complex_representation')
    if not isinstance(complex_meta, dict):
        raise ValueError('R170A complex-pressure representation metadata is missing')
    phase_convention = complex_meta.get('phasor_convention')
    if not isinstance(phase_convention, str) or not phase_convention:
        raise ValueError('R170A phase convention is missing')

    responses: list[LowBandReceiverResponse] = []
    for index, receiver in enumerate(snapshot.receivers):
        real_row = real_raw[index]
        imag_row = imag_raw[index]
        if (
            not isinstance(real_row, list)
            or not isinstance(imag_row, list)
            or len(real_row) != len(frequencies)
            or len(imag_row) != len(frequencies)
        ):
            raise ValueError('R170A complex-pressure frequency dimensions mismatch')
        magnitudes: list[float] = []
        levels: list[float] = []
        phases: list[float] = []
        for real_value, imag_value in zip(real_row, imag_row, strict=True):
            real = float(real_value)
            imag = float(imag_value)
            if not isfinite(real) or not isfinite(imag):
                raise ValueError('R170A complex pressure must be finite')
            magnitude = hypot(real, imag)
            if magnitude <= 0.0:
                raise ValueError(
                    'R170A logarithmic magnitude/phase capability is undefined at zero pressure'
                )
            magnitudes.append(magnitude)
            levels.append(20.0 * log10(magnitude / 20.0e-6))
            phases.append(degrees(atan2(imag, real)))
        responses.append(
            LowBandReceiverResponse(
                receiver_id=receiver.receiver_id,
                receiver_entity_id=receiver.entity_id,
                frequency_hz=frequencies,
                magnitude_pa=tuple(magnitudes),
                magnitude_db_spl=tuple(levels),
                phase_deg=tuple(phases),
                phase_convention=phase_convention,
            )
        )

    candidate_only = provenance_payload.get('candidate_only')
    production_selected = provenance_payload.get('production_solver_selected')
    owned_room_evidence = provenance_payload.get('owned_room_evidence', False)
    if evidence_state == 'candidate':
        if evidence_scope != 'unvalidated':
            raise ValueError('candidate R170A provider must remain unvalidated')
        if validation_authority_ref is not None or production_adoption_authority_ref is not None:
            raise ValueError('candidate R170A provider cannot claim validation/production refs')
    elif evidence_state == 'validated':
        if evidence_scope == 'unvalidated':
            raise ValueError('validated R170A provider requires explicit evidence scope')
        if validation_authority_ref is None:
            raise ValueError('validated R170A provider requires exact validation authority')
        _external_payload(
            external_payload_resolver,
            validation_authority_ref,
            label='provider validation',
        )
        if production_adoption_authority_ref is not None:
            raise ValueError('validated non-production provider cannot claim adoption')
    else:
        if evidence_scope != 'owned_room':
            raise ValueError('production R170A provider requires owned-room evidence scope')
        if validation_authority_ref is None or production_adoption_authority_ref is None:
            raise ValueError(
                'production R170A provider requires validation and adoption authorities'
            )
        _external_payload(
            external_payload_resolver,
            validation_authority_ref,
            label='provider validation',
        )
        _external_payload(
            external_payload_resolver,
            production_adoption_authority_ref,
            label='production adoption',
        )
        if (
            candidate_only is not False
            or production_selected is not True
            or owned_room_evidence is not True
        ):
            raise ValueError(
                'candidate/non-owned-room solver result cannot be promoted to production'
            )

    source_identity = PredictionProviderSourceIdentity(
        source_binding=source,
        source_binding_sha256=_model_hash(source),
        wave_excitation_binding_id=(
            None if wave_binding is None else wave_binding.binding_id
        ),
        wave_excitation_binding_sha256=(
            None if wave_binding is None else wave_binding.semantic_sha256
        ),
    )
    receiver_identities = tuple(
        PredictionProviderReceiverIdentity(
            receiver_binding=item,
            receiver_binding_sha256=_model_hash(item),
        )
        for item in snapshot.receivers
    )
    environment_identity = PredictionProviderEnvironmentIdentity(
        environment=snapshot.environment,
        environment_sha256=_model_hash(snapshot.environment),
    )
    capabilities = (
        PredictionProviderCapability(
            observable='frequency_response_magnitude',
            state='READY',
        ),
        PredictionProviderCapability(
            observable='frequency_response_phase',
            state='READY',
        ),
        PredictionProviderCapability(
            observable='impulse_response',
            state='UNSUPPORTED',
            reason='R170A does not synthesize an impulse response from sparse complex pressure',
        ),
        PredictionProviderCapability(
            observable='rt60',
            state='UNSUPPORTED',
            reason='R170A has no decay authority for RT60',
        ),
        PredictionProviderCapability(
            observable='edt',
            state='UNSUPPORTED',
            reason='R170A has no decay authority for EDT',
        ),
        PredictionProviderCapability(
            observable='c50',
            state='UNSUPPORTED',
            reason='R170A has no impulse-response clarity authority',
        ),
        PredictionProviderCapability(
            observable='c80',
            state='UNSUPPORTED',
            reason='R170A has no impulse-response clarity authority',
        ),
        PredictionProviderCapability(
            observable='arrival_timing',
            state='UNSUPPORTED',
            reason='R170A sparse frequency samples do not establish arrival timing',
        ),
        PredictionProviderCapability(
            observable='spatial_pressure_field',
            state='UNSUPPORTED',
            reason='R170A bounded lane contains only the explicit receiver set',
        ),
        PredictionProviderCapability(
            observable='broadband_hybrid',
            state='UNSUPPORTED',
            reason='R170A does not perform R160 numeric stitching',
        ),
    )
    current = provider_current_authority(
        revision=revision,
        snapshot=snapshot,
        request=request,
        result=result,
        source_entity_id=source.source_entity_id,
        receiver_ids=tuple(item.receiver_id for item in snapshot.receivers),
        valid_frequency_domain=artifact.valid_frequency_domain,
    )
    core = {
        'schema_version': PREDICTION_PROVIDER_SCHEMA_VERSION,
        'authority_version': PREDICTION_PROVIDER_AUTHORITY_VERSION,
        'adapter_id': PREDICTION_PROVIDER_ADAPTER_ID,
        'adapter_version': PREDICTION_PROVIDER_ADAPTER_VERSION,
        'evidence_state': evidence_state,
        'evidence_scope': evidence_scope,
        'validation_authority_ref': (
            None
            if validation_authority_ref is None
            else validation_authority_ref.model_dump(mode='json')
        ),
        'production_adoption_authority_ref': (
            None
            if production_adoption_authority_ref is None
            else production_adoption_authority_ref.model_dump(mode='json')
        ),
        'stale_state': 'CURRENT',
        'current_authority': current.model_dump(mode='json'),
        'source_identity': source_identity.model_dump(mode='json'),
        'receiver_identities': [
            item.model_dump(mode='json') for item in receiver_identities
        ],
        'environment_identity': environment_identity.model_dump(mode='json'),
        'observable_capabilities': [
            item.model_dump(mode='json') for item in capabilities
        ],
        'magnitude_capability': 'READY',
        'phase_capability': 'READY',
        'timing_capability': 'UNSUPPORTED',
        'spatial_field_capability': 'UNSUPPORTED',
        'response_unit': 'Pa',
        'magnitude_level_reference': '20_uPa',
        'valid_frequency_domain': artifact.valid_frequency_domain.model_dump(mode='json'),
        'receiver_responses': [item.model_dump(mode='json') for item in responses],
        # #942: the artifact's declared phasor convention is real authority;
        # normalization and timing stay undeclared for the sparse-sample lane.
        'phasor_convention': phase_convention,
        'result_envelope_id': result.result_id,
        'result_envelope_sha256': result.semantic_sha256,
        'result_artifact_ref': artifact.artifact_authority.model_dump(mode='json'),
        'result_artifact_schema_ref': artifact.encoding_schema_ref.model_dump(mode='json'),
        'execution_provenance_ref': result.execution_provenance_ref.model_dump(mode='json'),
    }
    digest = _semantic_hash(core)
    return LowBandPredictionProvider(
        provider_id=f'r170a-provider:{digest}',
        semantic_sha256=digest,
        evidence_state=evidence_state,
        evidence_scope=evidence_scope,
        validation_authority_ref=validation_authority_ref,
        production_adoption_authority_ref=production_adoption_authority_ref,
        current_authority=current,
        source_identity=source_identity,
        receiver_identities=receiver_identities,
        environment_identity=environment_identity,
        observable_capabilities=capabilities,
        magnitude_capability='READY',
        phase_capability='READY',
        timing_capability='UNSUPPORTED',
        spatial_field_capability='UNSUPPORTED',
        phasor_convention=phase_convention,
        valid_frequency_domain=artifact.valid_frequency_domain,
        receiver_responses=tuple(responses),
        result_envelope_id=result.result_id,
        result_envelope_sha256=result.semantic_sha256,
        result_artifact_ref=artifact.artifact_authority,
        result_artifact_schema_ref=artifact.encoding_schema_ref,
        execution_provenance_ref=result.execution_provenance_ref,
    )


def build_prediction_provider_binding(
    provider: LowBandPredictionProvider,
    *,
    consumer_kind: ProviderConsumerKind,
    consumer_id: str,
    required_observables: Sequence[str],
    consumer_semantic_sha256: str | None = None,
) -> PredictionProviderBinding:
    required = tuple(sorted(set(required_observables)))
    if not required:
        raise ValueError('prediction provider binding requires observable capabilities')
    for observable in required:
        provider.require_observable(observable)
    core = {
        'authority_version': PREDICTION_PROVIDER_BINDING_AUTHORITY_VERSION,
        'provider_ref': provider.ref().model_dump(mode='json'),
        'consumer_kind': consumer_kind,
        'consumer_id': consumer_id,
        'consumer_semantic_sha256': consumer_semantic_sha256,
        'required_observables': list(required),
        'expected_authority': provider.current_authority.model_dump(mode='json'),
        'stale_state': 'CURRENT',
    }
    digest = _semantic_hash(core)
    return PredictionProviderBinding(
        binding_id=f'r170a-provider-binding:{digest}',
        semantic_sha256=digest,
        provider_ref=provider.ref(),
        consumer_kind=consumer_kind,
        consumer_id=consumer_id,
        consumer_semantic_sha256=consumer_semantic_sha256,
        required_observables=required,
        expected_authority=provider.current_authority,
    )


class CadPredictionProviderRepository:
    """Append-only R170A provider/binding persistence with exact reopen checks."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        snapshot_request_resolver: SnapshotRequestResolver,
        solver_result_resolver: SolverResultResolver,
        external_payload_resolver: ExternalPayloadResolver,
    ) -> None:
        self.scene_repository = scene_repository
        self.snapshot_request_resolver = snapshot_request_resolver
        self.solver_result_resolver = solver_result_resolver
        self.external_payload_resolver = external_payload_resolver
        self.path = Path(scene_repository.path)
        for label, resolver in (
            ('snapshot/request', snapshot_request_resolver),
            ('solver result', solver_result_resolver),
        ):
            if Path(resolver.path) != self.path:
                raise ValueError(
                    f'prediction provider and {label} repositories must share one native CAD database'
                )
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_prediction_providers', 'cad_prediction_provider_bindings')

    def _validate_provider(
        self,
        provider: LowBandPredictionProvider,
    ) -> LowBandPredictionProvider:
        provider = LowBandPredictionProvider.model_validate(
            provider.model_dump(mode='python')
        )
        revision = self.scene_repository.get(
            provider.current_authority.scene_revision_id
        )
        if revision is None:
            raise ValueError('prediction provider underlying SceneRevision is missing')
        snapshot = self.snapshot_request_resolver.get_snapshot(
            provider.current_authority.acoustic_scene_snapshot_id
        )
        if snapshot is None:
            raise ValueError('prediction provider underlying snapshot is missing')
        request = self.snapshot_request_resolver.get_prediction_request(
            provider.current_authority.prediction_request_id
        )
        if request is None:
            raise ValueError('prediction provider underlying request is missing')
        result = self.solver_result_resolver.get(provider.result_envelope_id)
        if result is None:
            raise ValueError('prediction provider underlying solver result is missing')
        regenerated = build_r130_low_band_prediction_provider(
            revision=revision,
            snapshot=snapshot,
            request=request,
            result=result,
            external_payload_resolver=self.external_payload_resolver,
            evidence_state=provider.evidence_state,
            evidence_scope=provider.evidence_scope,
            validation_authority_ref=provider.validation_authority_ref,
            production_adoption_authority_ref=provider.production_adoption_authority_ref,
        )
        if regenerated != provider:
            raise ValueError(
                'prediction provider does not reproduce from exact underlying authorities'
            )
        return provider

    def save_provider(
        self,
        provider: LowBandPredictionProvider,
    ) -> LowBandPredictionProvider:
        provider = self._validate_provider(provider)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT payload_json FROM cad_prediction_providers WHERE provider_id=?',
                (provider.provider_id,),
            ).fetchone()
            if existing is not None:
                persisted = LowBandPredictionProvider.model_validate_json(
                    existing['payload_json']
                )
                if persisted != provider:
                    raise ValueError('prediction provider id exists with different semantics')
                return self._validate_provider(persisted)
            connection.execute(
                """
                INSERT INTO cad_prediction_providers(
                    provider_id,
                    semantic_sha256,
                    document_id,
                    scene_revision_id,
                    result_envelope_id,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    provider.provider_id,
                    provider.semantic_sha256,
                    provider.current_authority.document_id,
                    provider.current_authority.scene_revision_id,
                    provider.result_envelope_id,
                    provider.model_dump_json(),
                ),
            )
        return provider

    def get_provider(self, provider_id: str) -> LowBandPredictionProvider | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_prediction_providers WHERE provider_id=?',
                (provider_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate_provider(
            LowBandPredictionProvider.model_validate_json(row['payload_json'])
        )

    def list_providers(
        self,
        document_id: str,
    ) -> tuple[LowBandPredictionProvider, ...]:
        """All persisted providers for one document, reopen-validated (#457)."""

        if not document_id:
            raise ValueError('document_id must not be empty')
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_prediction_providers '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            self._validate_provider(
                LowBandPredictionProvider.model_validate_json(row['payload_json'])
            )
            for row in rows
        )

    def save_binding(
        self,
        binding: PredictionProviderBinding,
    ) -> PredictionProviderBinding:
        binding = PredictionProviderBinding.model_validate(
            binding.model_dump(mode='python')
        )
        provider = self.get_provider(binding.provider_ref.provider_id)
        if provider is None:
            raise ValueError('prediction provider binding references missing provider')
        if binding.provider_ref != provider.ref():
            raise ValueError('prediction provider binding provider hash mismatch')
        if binding.expected_authority != provider.current_authority:
            raise ValueError('prediction provider binding authority mismatch')
        for observable in binding.required_observables:
            provider.require_observable(observable)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT payload_json FROM cad_prediction_provider_bindings WHERE binding_id=?',
                (binding.binding_id,),
            ).fetchone()
            if existing is not None:
                persisted = PredictionProviderBinding.model_validate_json(
                    existing['payload_json']
                )
                if persisted != binding:
                    raise ValueError('provider binding id exists with different semantics')
                return persisted
            connection.execute(
                """
                INSERT INTO cad_prediction_provider_bindings(
                    binding_id,
                    semantic_sha256,
                    provider_id,
                    consumer_kind,
                    consumer_id,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    binding.binding_id,
                    binding.semantic_sha256,
                    binding.provider_ref.provider_id,
                    binding.consumer_kind,
                    binding.consumer_id,
                    binding.model_dump_json(),
                ),
            )
        return binding

    def get_binding(self, binding_id: str) -> PredictionProviderBinding | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_prediction_provider_bindings WHERE binding_id=?',
                (binding_id,),
            ).fetchone()
        if row is None:
            return None
        binding = PredictionProviderBinding.model_validate_json(row['payload_json'])
        provider = self.get_provider(binding.provider_ref.provider_id)
        if provider is None or provider.ref() != binding.provider_ref:
            raise ValueError('prediction provider binding exact provider is unavailable')
        if binding.expected_authority != provider.current_authority:
            raise ValueError('prediction provider binding exact authority mismatch')
        for observable in binding.required_observables:
            provider.require_observable(observable)
        return binding

    def list_bindings(
        self,
        provider_id: str | None = None,
    ) -> tuple[PredictionProviderBinding, ...]:
        """Persisted bindings without the strict reopen checks.

        Unlike ``get_binding`` (which raises on a stale provider or an
        observable that is no longer READY), this listing is for read
        models (#727) that must surface degraded coverage instead of
        failing closed.
        """

        with closing(self._connect()) as connection, connection:
            if provider_id is None:
                rows = connection.execute(
                    'SELECT payload_json FROM cad_prediction_provider_bindings '
                    'ORDER BY seq ASC'
                ).fetchall()
            else:
                rows = connection.execute(
                    'SELECT payload_json FROM cad_prediction_provider_bindings '
                    'WHERE provider_id=? ORDER BY seq ASC',
                    (provider_id,),
                ).fetchall()
        return tuple(
            PredictionProviderBinding.model_validate_json(row['payload_json'])
            for row in rows
        )
