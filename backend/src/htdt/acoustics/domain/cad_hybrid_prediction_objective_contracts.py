"""R170B hybrid-prediction objective contracts (domain layer).

Record surfaces and pure typed reads the objective-authority replay binds
against: the provider reference record, the immutable O30 input authority,
the band-validated frequency-response read, and the structural
``HybridObjectiveProvider`` port. The concrete
``HybridPredictionProvider`` model and its repository live at services rank
in ``acoustics.services.cad_hybrid_prediction_provider``; domain code binds
against the port instead of importing them, and the services modules
re-export these contracts so every pre-split import path keeps resolving to
the same objects.
"""

from __future__ import annotations

from collections.abc import Sequence
from math import isfinite
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...canonical_json import canonical_sha256 as _digest
from ...comparison import FrequencyResponse


class HybridProviderPressureSample(Protocol):
    """Surface of one absolute-pressure sample the typed read consumes."""

    frequency_hz: float
    magnitude_db_spl: float


class HybridProviderFrequencyDomain(Protocol):
    """Surface of the provider's exact output domain (``FrequencyDomain``).

    Declared structurally so this module stays free of flat imports that sit
    inside the import mega-SCC — the domain layer records contracts, not
    dependencies on tangled modules."""

    minimum_hz: float
    maximum_hz: float


class HybridObjectiveProvider(Protocol):
    """Domain port for the hybrid prediction provider these contracts read.

    The concrete ``HybridPredictionProvider`` lives at services rank; domain
    code binds against this structural contract instead of importing it.
    """

    source_entity_id: str
    receiver_id: str
    valid_frequency_domain: HybridProviderFrequencyDomain
    absolute_pressure_samples: Sequence[HybridProviderPressureSample]

    def require_observable(self, observable: str) -> None: ...

    def ref(self) -> 'HybridPredictionProviderRef': ...

    def model_dump(self, *, mode: str, **kwargs: Any) -> dict[str, Any]: ...


class HybridPredictionProviderRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    provider_id: str = Field(pattern=r'^r170b-hybrid-provider:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


def hybrid_provider_frequency_response(
    provider: HybridObjectiveProvider,
    *,
    source_entity_id: str,
    receiver_id: str,
    low_hz: float,
    high_hz: float,
) -> FrequencyResponse:
    """N70 product-facing typed read. R160 raw JSON never crosses this boundary."""

    # Re-validate through the concrete model class: the port guarantees a
    # pydantic model dump, and this keeps the defensive normalization the
    # services-tier signature applied.
    provider = type(provider).model_validate(provider.model_dump(mode='python'))
    if source_entity_id != provider.source_entity_id:
        raise ValueError('hybrid provider source identity mismatch')
    if receiver_id != provider.receiver_id:
        raise ValueError('hybrid provider receiver identity mismatch')
    provider.require_observable('frequency_response_magnitude')

    low = float(low_hz)
    high = float(high_hz)
    if not isfinite(low) or not isfinite(high) or high <= low:
        raise ValueError('hybrid provider requested frequency band is invalid')
    domain = provider.valid_frequency_domain
    if low < float(domain.minimum_hz) or high > float(domain.maximum_hz):
        raise ValueError('hybrid provider requested band exceeds exact output domain')

    selected = tuple(
        item
        for item in provider.absolute_pressure_samples
        if low <= float(item.frequency_hz) <= high
    )
    if len(selected) < 2:
        raise ValueError(
            'hybrid provider exact output grid has fewer than two points in requested band'
        )
    return FrequencyResponse(
        frequency_hz=tuple(float(item.frequency_hz) for item in selected),
        level_db=tuple(float(item.magnitude_db_spl) for item in selected),
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


def build_hybrid_provider_objective_input(
    provider: HybridObjectiveProvider,
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
