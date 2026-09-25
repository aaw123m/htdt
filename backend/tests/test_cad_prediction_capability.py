"""#727 prediction capability read model tests."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from htdt.cad_prediction_capability import (
    BindingCoverageItem,
    build_prediction_capability_report,
)


class _Cap:
    def __init__(self, observable, state, reason=None):
        self.observable = observable
        self.state = state
        self.reason = reason


class _StubProvider:
    """Minimal stand-in for LowBandPredictionProvider (read surface only)."""

    def __init__(self, provider_id, sha, capabilities, *, evidence='validated',
                 scope='owned_room', stale='CURRENT', receivers=('r-1',),
                 domain=(40.0, 80.0), axes=('READY', 'READY', 'UNSUPPORTED', 'UNSUPPORTED')):
        self.provider_id = provider_id
        self.semantic_sha256 = sha
        self.adapter_id = 'htdt.r170a.r130_complex_pressure'
        self.adapter_version = '1'
        self.evidence_state = evidence
        self.evidence_scope = scope
        self.stale_state = stale
        self.observable_capabilities = tuple(capabilities)
        (self.magnitude_capability, self.phase_capability,
         self.timing_capability, self.spatial_field_capability) = axes
        self.receiver_identities = tuple(
            SimpleNamespace(receiver_binding=SimpleNamespace(receiver_id=r))
            for r in receivers
        )
        self.valid_frequency_domain = (
            SimpleNamespace(minimum_hz=domain[0], maximum_hz=domain[1])
            if domain else None
        )

    def capability(self, observable):
        for item in self.observable_capabilities:
            if item.observable == observable:
                return item
        return _Cap(observable, 'UNSUPPORTED', reason='outside contract')


class _StubBinding:
    def __init__(self, binding_id, provider, consumer_kind, consumer_id,
                 required):
        self.binding_id = binding_id
        self.provider_ref = SimpleNamespace(
            provider_id=provider.provider_id,
            semantic_sha256=provider.semantic_sha256,
        )
        self.consumer_kind = consumer_kind
        self.consumer_id = consumer_id
        self.required_observables = tuple(required)


NOW = '2026-09-23T00:00:00+00:00'


def test_report_lists_providers_with_verbatim_state() -> None:
    provider = _StubProvider(
        'r170a-provider:' + 'a' * 64,
        'a' * 64,
        capabilities=[
            _Cap('frequency_response_magnitude', 'READY'),
            _Cap('impulse_response', 'UNSUPPORTED', reason='not computed'),
        ],
        evidence='candidate',
        scope='synthetic_fixture',
        stale='STALE',
    )
    report = build_prediction_capability_report(
        document_id='doc-1',
        providers=(provider,),
        bindings=(),
        generated_at_utc=NOW,
    )
    summary = report.providers[0]
    assert summary.provider_sha256 == 'a' * 64
    assert summary.evidence_state == 'candidate'
    assert summary.evidence_scope == 'synthetic_fixture'
    assert summary.stale_state == 'STALE'
    states = {item.observable: item for item in summary.observables}
    assert states['impulse_response'].state == 'UNSUPPORTED'
    assert states['impulse_response'].reason == 'not computed'
    assert summary.frequency_domain_hz == (40.0, 80.0)


def test_binding_coverage_reports_missing_not_raises() -> None:
    provider = _StubProvider(
        'r170a-provider:' + 'b' * 64,
        'b' * 64,
        capabilities=[_Cap('frequency_response_magnitude', 'READY')],
    )
    binding = _StubBinding(
        'bind-1', provider, 'calibration', 'plan-1',
        required=('frequency_response_magnitude', 'impulse_response'),
    )
    report = build_prediction_capability_report(
        document_id='doc-1',
        providers=(provider,),
        bindings=(binding,),
        generated_at_utc=NOW,
    )
    item = report.bindings[0]
    assert item.state == 'missing_capability'
    assert item.missing_observables == ('impulse_response',)
    assert item.required_observables == (
        'frequency_response_magnitude',
        'impulse_response',
    )


def test_binding_on_unavailable_provider_is_flagged() -> None:
    provider = _StubProvider('r170a-provider:' + 'c' * 64, 'c' * 64, [])
    binding = _StubBinding('bind-2', provider, 'study', 'study-1',
                           required=('frequency_response_magnitude',))
    report = build_prediction_capability_report(
        document_id='doc-1',
        providers=(),  # provider missing from this document
        bindings=(binding,),
        generated_at_utc=NOW,
    )
    assert report.bindings[0].state == 'provider_unavailable'


def test_covered_binding_reports_covered() -> None:
    provider = _StubProvider(
        'r170a-provider:' + 'd' * 64,
        'd' * 64,
        capabilities=[_Cap('frequency_response_magnitude', 'READY')],
    )
    binding = _StubBinding('bind-3', provider, 'calibration', 'plan-9',
                           required=('frequency_response_magnitude',))
    report = build_prediction_capability_report(
        document_id='doc-1',
        providers=(provider,),
        bindings=(binding,),
        generated_at_utc=NOW,
    )
    assert report.bindings[0].state == 'covered'


def test_no_providers_is_empty_never_implied() -> None:
    report = build_prediction_capability_report(
        document_id='doc-empty',
        providers=(),
        bindings=(),
        generated_at_utc=NOW,
    )
    assert report.providers == ()
    assert report.bindings == ()
    assert report.observable_coverage == ()


def test_observable_rollup_counts_ready_and_unsupported() -> None:
    ready = _StubProvider(
        'r170a-provider:' + 'e' * 64,
        'e' * 64,
        [_Cap('frequency_response_magnitude', 'READY')],
    )
    limited = _StubProvider(
        'r170a-provider:' + 'f' * 64,
        'f' * 64,
        [_Cap('frequency_response_magnitude', 'UNSUPPORTED', 'no phase')],
    )
    report = build_prediction_capability_report(
        document_id='doc-1',
        providers=(ready, limited),
        bindings=(),
        generated_at_utc=NOW,
    )
    item = report.observable_coverage[0]
    assert item.observable == 'frequency_response_magnitude'
    assert item.ready_providers == (ready.provider_id,)
    assert item.unsupported_providers == (limited.provider_id,)
