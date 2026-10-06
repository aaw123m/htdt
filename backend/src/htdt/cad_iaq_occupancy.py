"""Occupied-room IAQ / thermal-comfort authority (issue #740).

Meeting a declared HVAC airflow target does NOT prove that the
occupied theater maintains acceptable indoor-air and thermal
conditions during a real viewing session. Occupied-state evidence
(CO2, temperature, RH, ventilation effectiveness) measured during
occupancy must be pinned before claiming the room is comfortable.

Basis: issue #740 scope; ASHRAE 62.1 (ventilation for acceptable
IAQ); ASHRAE 55 (thermal comfort); ISO 7730 (PMV/PPD); #616 HVAC
acoustic/airflow co-design.
"""

from __future__ import annotations

from typing import Any

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


IAQ_LABELS: dict[str, str] = {
    'occupied_conditions_qualified': '占有時条件は適格',
    'occupied_conditions_degraded': '占有時条件は劣化',
    'airflow_is_not_occupied_iaq': '風量達成は占有IAQの証明ではない',
    'insufficient_evidence': '証拠不足',
}


class OccupiedIaqObservation(BaseModel):
    """Occupied-state IAQ/thermal observation (iaq- prefix)."""

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    room_id: str
    occupied: bool = True
    co2_ppm: float | None = None
    temperature_c: float | None = None
    relative_humidity_percent: float | None = None
    ventilation_effectiveness: float | None = None
    session_context: str | None = None
    result_data_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'OccupiedIaqObservation':
        measured = (
            self.co2_ppm is not None
            or self.temperature_c is not None
            or self.relative_humidity_percent is not None
            or self.ventilation_effectiveness is not None
        )
        if not measured and self.result_data_ref is None:
            raise ValueError(
                'IAQ observation needs a measured figure or pinned data')
        if self.result_data_ref is not None:
            _require_refs(self.result_data_ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'OccupiedIaqObservation':
        return _seal(
            cls, payload, 'observation_id',
            'observation_sha256', 'iaq')


class OccupiedIaqQualification(BaseModel):
    """Occupied-condition verdict bound to pinned observations
    (iaqq- prefix)."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    room_id: str
    observation_refs: tuple[AuthorityRef, ...]
    verdict: str
    limits_profile: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'OccupiedIaqQualification':
        if not self.observation_refs:
            raise ValueError('qualification needs observation refs')
        _require_refs(*self.observation_refs)
        if self.verdict not in IAQ_LABELS:
            raise ValueError(f'unknown IAQ verdict {self.verdict!r}')
        if self.verdict == 'occupied_conditions_qualified' \
                and self.limits_profile is None:
            raise ValueError(
                'qualified conditions must declare a limits profile')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'qualification_id', 'qualification_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'OccupiedIaqQualification':
        return _seal(
            cls, payload, 'qualification_id',
            'qualification_sha256', 'iaqq')


def evaluate_iaq_claim(
    observation: OccupiedIaqObservation | None,
    airflow_target_met: bool = False,
) -> tuple[str, str]:
    """A declared airflow target never implies occupied conditions."""
    if observation is None:
        if airflow_target_met:
            return ('airflow_is_not_occupied_iaq',
                    'design_airflow_without_occupied_evidence')
        return ('insufficient_evidence', 'no_occupied_observation')
    if not observation.occupied:
        return ('insufficient_evidence',
                'observation_not_occupied_state')
    return ('insufficient_evidence', 'limits_profile_required')
