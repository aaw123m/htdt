"""Prediction capability read model (#727).

A deterministic, derived view over the persisted prediction-provider
authorities: for one document, what prediction providers exist, which
observables each supports (and with what evidence/stale state), and how
well persisted consumer bindings are covered by their bound provider.

Read-model contract:

- derived, never stored — the report is computed from sealed authorities
  at read time and carries no authority hash of its own;
- degradation is surfaced, not raised: a binding whose provider no longer
  reports a required observable READY shows ``missing_observables`` — the
  strict ``get_binding`` reopen path intentionally stays separate;
- absence is honest: no providers means an empty report, never implied
  capability; every UNSUPPORTED observable keeps its recorded reason;
- stale/evidence state is reported verbatim — this model never decides an
  authority is usable; it reports what the authorities declare.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ObservableCapabilityView(BaseModel):
    """One observable's declared capability on one provider."""

    model_config = ConfigDict(frozen=True)

    observable: str = Field(min_length=1)
    state: Literal['READY', 'UNSUPPORTED']
    reason: str | None = None


class ProviderCapabilitySummary(BaseModel):
    """Derived view of one persisted prediction provider."""

    model_config = ConfigDict(frozen=True)

    provider_id: str = Field(min_length=1)
    provider_sha256: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    evidence_state: str
    evidence_scope: str
    stale_state: str
    observables: tuple[ObservableCapabilityView, ...]
    axis_capabilities: tuple[tuple[str, str], ...]
    receiver_ids: tuple[str, ...]
    frequency_domain_hz: tuple[float, float] | None = None


class BindingCoverageItem(BaseModel):
    """Coverage of one persisted binding against its bound provider."""

    model_config = ConfigDict(frozen=True)

    binding_id: str = Field(min_length=1)
    consumer_kind: str
    consumer_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    provider_sha256: str = Field(min_length=1)
    required_observables: tuple[str, ...]
    missing_observables: tuple[str, ...]
    state: Literal['covered', 'missing_capability', 'provider_unavailable']

    @model_validator(mode='after')
    def valid_coverage(self) -> 'BindingCoverageItem':
        if self.state == 'covered' and self.missing_observables:
            raise ValueError(
                'a covered binding cannot carry missing observables'
            )
        if self.state == 'missing_capability' and not self.missing_observables:
            raise ValueError(
                'missing_capability coverage requires missing observables'
            )
        return self


class ObservableCoverage(BaseModel):
    """Per-observable rollup across a document's providers."""

    model_config = ConfigDict(frozen=True)

    observable: str = Field(min_length=1)
    ready_providers: tuple[str, ...]
    unsupported_providers: tuple[str, ...]


class PredictionCapabilityReport(BaseModel):
    """Derived capability view for one document's prediction authorities."""

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(min_length=1)
    generated_at_utc: str = Field(min_length=1)
    providers: tuple[ProviderCapabilitySummary, ...]
    bindings: tuple[BindingCoverageItem, ...]
    observable_coverage: tuple[ObservableCoverage, ...]


@runtime_checkable
class PredictionProviderSource(Protocol):
    """Narrow surface the report reads from a provider authority."""

    provider_id: str
    semantic_sha256: str
    adapter_id: str
    adapter_version: str
    evidence_state: str
    evidence_scope: str
    stale_state: str
    observable_capabilities: Any
    magnitude_capability: str
    phase_capability: str
    timing_capability: str
    spatial_field_capability: str
    receiver_identities: Any
    valid_frequency_domain: Any

    def capability(self, observable: str) -> Any: ...


@runtime_checkable
class PredictionBindingSource(Protocol):
    """Narrow surface the report reads from a provider binding."""

    binding_id: str
    provider_ref: Any
    consumer_kind: str
    consumer_id: str
    required_observables: tuple[str, ...]


def _capability_view(item: Any) -> ObservableCapabilityView:
    return ObservableCapabilityView(
        observable=item.observable,
        state=item.state,
        reason=item.reason,
    )


def _provider_summary(
    provider: PredictionProviderSource,
) -> ProviderCapabilitySummary:
    receivers = tuple(
        getattr(item.receiver_binding, 'receiver_id', None)
        for item in provider.receiver_identities
    )
    domain = provider.valid_frequency_domain
    domain_pair = (
        (float(domain.minimum_hz), float(domain.maximum_hz))
        if domain is not None
        else None
    )
    return ProviderCapabilitySummary(
        provider_id=provider.provider_id,
        provider_sha256=provider.semantic_sha256,
        adapter_id=provider.adapter_id,
        adapter_version=provider.adapter_version,
        evidence_state=str(provider.evidence_state),
        evidence_scope=str(provider.evidence_scope),
        stale_state=str(provider.stale_state),
        observables=tuple(
            _capability_view(item)
            for item in provider.observable_capabilities
        ),
        axis_capabilities=(
            ('magnitude', str(provider.magnitude_capability)),
            ('phase', str(provider.phase_capability)),
            ('timing', str(provider.timing_capability)),
            ('spatial_field', str(provider.spatial_field_capability)),
        ),
        receiver_ids=tuple(
            receiver for receiver in receivers if receiver is not None
        ),
        frequency_domain_hz=domain_pair,
    )


def build_prediction_capability_report(
    *,
    document_id: str,
    providers: tuple[PredictionProviderSource, ...],
    bindings: tuple[PredictionBindingSource, ...],
    generated_at_utc: str,
) -> PredictionCapabilityReport:
    """Derive the capability report from sealed authorities verbatim."""
    by_id = {provider.provider_id: provider for provider in providers}
    summaries = tuple(_provider_summary(provider) for provider in providers)

    coverage: list[BindingCoverageItem] = []
    for binding in bindings:
        provider = by_id.get(binding.provider_ref.provider_id)
        if provider is None:
            coverage.append(
                BindingCoverageItem(
                    binding_id=binding.binding_id,
                    consumer_kind=str(binding.consumer_kind),
                    consumer_id=binding.consumer_id,
                    provider_id=binding.provider_ref.provider_id,
                    provider_sha256=binding.provider_ref.semantic_sha256,
                    required_observables=tuple(binding.required_observables),
                    missing_observables=(),
                    state='provider_unavailable',
                )
            )
            continue
        missing = tuple(
            observable
            for observable in binding.required_observables
            if provider.capability(observable).state != 'READY'
        )
        coverage.append(
            BindingCoverageItem(
                binding_id=binding.binding_id,
                consumer_kind=str(binding.consumer_kind),
                consumer_id=binding.consumer_id,
                provider_id=provider.provider_id,
                provider_sha256=provider.semantic_sha256,
                required_observables=tuple(binding.required_observables),
                missing_observables=missing,
                state='covered' if not missing else 'missing_capability',
            )
        )

    observables: dict[str, dict[str, list[str]]] = {}
    for provider in providers:
        for item in provider.observable_capabilities:
            bucket = observables.setdefault(
                item.observable, {'ready': [], 'unsupported': []}
            )
            key = 'ready' if item.state == 'READY' else 'unsupported'
            bucket[key].append(provider.provider_id)
    rollup = tuple(
        ObservableCoverage(
            observable=observable,
            ready_providers=tuple(sorted(buckets['ready'])),
            unsupported_providers=tuple(sorted(buckets['unsupported'])),
        )
        for observable, buckets in sorted(observables.items())
    )

    return PredictionCapabilityReport(
        document_id=document_id,
        generated_at_utc=generated_at_utc,
        providers=summaries,
        bindings=tuple(coverage),
        observable_coverage=rollup,
    )


def load_prediction_capability_report(
    provider_repository: Any,
    document_id: str,
    *,
    generated_at_utc: str,
) -> PredictionCapabilityReport:
    """Load the report from a ``CadPredictionProviderRepository``.

    Uses the non-strict ``list_bindings`` so degraded bindings surface as
    ``missing_capability``/``provider_unavailable`` instead of raising.
    """

    providers = provider_repository.list_providers(document_id)
    provider_ids = {provider.provider_id for provider in providers}
    bindings = tuple(
        binding
        for binding in provider_repository.list_bindings()
        if binding.provider_ref.provider_id in provider_ids
    )
    return build_prediction_capability_report(
        document_id=document_id,
        providers=providers,
        bindings=bindings,
        generated_at_utc=generated_at_utc,
    )


__all__ = [
    'BindingCoverageItem',
    'ObservableCapabilityView',
    'ObservableCoverage',
    'PredictionBindingSource',
    'PredictionCapabilityReport',
    'PredictionProviderSource',
    'ProviderCapabilitySummary',
    'build_prediction_capability_report',
    'load_prediction_capability_report',
]
