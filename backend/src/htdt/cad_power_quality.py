"""AC power-quality measurement authority (issue #738).

Sufficient branch-circuit capacity, UPS rating and thermal headroom do
NOT prove that the actual AC supply delivered to AV equipment is
electrically stable enough for repeatable operation. In-situ PQ
evidence (RMS voltage/frequency, dips/swells, interruptions, rapid
voltage changes, THD) must be pinned before qualifying a circuit.

Basis: issue #738 scope; IEC 61000-4-30 measurand classes;
EN 50160 supply-voltage characteristics; #587 rack/power qualification.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{sha_field: digest, id_field: _semantic_id(prefix, digest)},
    )


def _require_refs(*refs: AuthorityRef) -> None:
    for ref in refs:
        if ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')


PqEventKind = Literal[
    'dip', 'swell', 'short_interruption', 'long_interruption',
    'rapid_voltage_change', 'transient_overvoltage',
]

PQ_LABELS: dict[str, str] = {
    'supply_qualified': '供給品質は適格',
    'supply_degraded': '供給品質は劣化',
    'unmeasured_supply': '供給品質は未測定',
    'insufficient_evidence': '証拠不足',
}


class PowerQualityMeasurement(BaseModel):
    """In-situ PQ observation at a circuit/receptacle (pqm- prefix)."""

    model_config = ConfigDict(frozen=True)

    measurement_id: str
    measurement_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    circuit_id: str
    rms_voltage_v: float | None = None
    frequency_hz: float | None = None
    thd_percent: float | None = None
    events: tuple[dict[str, Any], ...] = ()
    instrument_ref: AuthorityRef | None = None
    measurement_window: str | None = None
    result_data_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'PowerQualityMeasurement':
        if self.rms_voltage_v is None and self.frequency_hz is None \
                and self.result_data_ref is None:
            raise ValueError(
                'PQ measurement needs a measured figure or pinned data')
        for kind in (e.get('kind') for e in self.events):
            if kind not in (
                'dip', 'swell', 'short_interruption', 'long_interruption',
                'rapid_voltage_change', 'transient_overvoltage',
            ):
                raise ValueError(f'undeclared PQ event kind {kind!r}')
        for ref in (self.instrument_ref, self.result_data_ref):
            if ref is not None:
                _require_refs(ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'measurement_id', 'measurement_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'PowerQualityMeasurement':
        return _seal(
            cls, payload, 'measurement_id', 'measurement_sha256', 'pqm')


class PowerQualityQualification(BaseModel):
    """Supply-stability verdict bound to pinned PQ evidence
    (pqq- prefix)."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    circuit_id: str
    measurement_ref: AuthorityRef
    verdict: str
    limits_profile: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'PowerQualityQualification':
        _require_refs(self.measurement_ref)
        if self.verdict not in PQ_LABELS:
            raise ValueError(f'unknown PQ verdict {self.verdict!r}')
        if self.verdict == 'supply_qualified' \
                and self.limits_profile is None:
            raise ValueError(
                'qualified supply must declare a limits profile')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'qualification_id', 'qualification_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'PowerQualityQualification':
        return _seal(
            cls, payload, 'qualification_id',
            'qualification_sha256', 'pqq')


def evaluate_supply_claim(
    measurement: PowerQualityMeasurement | None,
) -> tuple[str, str]:
    """Circuit capacity never implies a stable supply."""
    if measurement is None:
        return ('unmeasured_supply', 'capacity_is_not_stability')
    severe = {
        'long_interruption', 'short_interruption',
        'transient_overvoltage',
    }
    if any(e.get('kind') in severe for e in measurement.events):
        return ('supply_degraded', 'interruption_or_transient_observed')
    return ('insufficient_evidence', 'limits_profile_required')
