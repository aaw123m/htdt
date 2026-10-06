"""Occupied-room indoor environment authority (issues #740, #750).

An HVAC design meeting its airflow target does not prove the
occupied theater maintains acceptable IAQ/thermal conditions during
a real session (#740 — measured occupied outcome vs design
requirement). And acoustic/fire-documented material does not prove
low chemical emissions, while a product certificate does not prove
the completed room's IAQ (#750 — emission-rate vs model-room
concentration semantics, distinct layers).

Basis: ISO 16000 chamber methods / product emission programs for
#750; occupied-room IAQ/thermal-comfort measurement practice for
#740.
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


class IndoorAirObservation(BaseModel):
    """Measured occupied-room IAQ/thermal evidence (#740) — CO2,
    temperature/RH, air speed, per-seat differences over session
    time. 'unknown' sensor class fails closed."""

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    sensor_class: Literal[
        'ndir_co2', 'electrochemical', 'thermal_comfort_station',
        'hvac_telemetry', 'other', 'unknown',
    ]
    co2_ppm_peak: float | None = None
    temperature_c: float | None = None
    relative_humidity_pct: float | None = None
    session_duration_min: float | None = None
    seat_count_observed: int | None = None
    capture_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('sensor_class') not in (
                'ndir_co2', 'electrochemical',
                'thermal_comfort_station', 'hvac_telemetry',
                'other', 'unknown',
            ):
                raise ValueError('unknown sensor class')
            if data.get('sensor_class') == 'unknown':
                raise ValueError(
                    'an IAQ observation requires a declared sensor '
                    'class'
                )
            if data.get('capture_ref') is None:
                raise ValueError(
                    'an occupied-room observation requires pinned '
                    'capture evidence'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'observation_id', 'observation_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'IndoorAirObservation':
        return _seal(
            cls, payload, 'observation_id', 'observation_sha256', 'iao'
        )


class MaterialEmissionEvidence(BaseModel):
    """Product chemical-emission evidence (#750) — chamber-test
    result bound to exact tested product/build-up, kept separate
    from the occupied-room outcome."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    emission_class: Literal[
        'voc_chamber', 'formaldehyde', 'svoc', 'vvoc',
        'manufacturer_program', 'other', 'unknown',
    ]
    tested_product_descriptor: str
    chamber_standard_ref: AuthorityRef | None = None
    report_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('emission_class') not in (
                'voc_chamber', 'formaldehyde', 'svoc', 'vvoc',
                'manufacturer_program', 'other', 'unknown',
            ):
                raise ValueError('unknown emission class')
            if data.get('emission_class') == 'unknown':
                raise ValueError(
                    'emission evidence must declare its test class'
                )
            if not data.get('tested_product_descriptor'):
                raise ValueError(
                    'emission evidence requires the exact tested '
                    'product/build-up descriptor'
                )
            if data.get('emission_class') != 'manufacturer_program' and (
                data.get('report_ref') is None
            ):
                raise ValueError(
                    'a chamber-test claim requires the report ref'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evidence_id', 'evidence_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'MaterialEmissionEvidence':
        return _seal(
            cls, payload, 'evidence_id', 'evidence_sha256', 'mem'
        )


IndoorVerdict = Literal[
    'qualified_indoor',
    'design_is_not_occupied_outcome',
    'certificate_is_not_room_iaq',
    'emission_unbounded',
    'observation_unqualified',
]


def evaluate_indoor_claim(
    observation: IndoorAirObservation | None,
    emissions: tuple[MaterialEmissionEvidence, ...] | None,
    *,
    ventilation_design_qualified: bool = False,
) -> tuple[IndoorVerdict, str]:
    """Judge an occupied-room environmental claim (#740/#750)."""
    if observation is None:
        if ventilation_design_qualified:
            return (
                'design_is_not_occupied_outcome',
                'a ventilating design does not prove the occupied '
                'room outcome — no measured session evidence',
            )
        return (
            'observation_unqualified',
            'no occupied-room observation pinned',
        )
    if emissions:
        for e in emissions:
            if e.emission_class == 'manufacturer_program' and (
                e.chamber_standard_ref is None
            ):
                return (
                    'certificate_is_not_room_iaq',
                    'a manufacturer program claim does not bound '
                    'chamber-tested emissions or room IAQ',
                )
    elif observation.co2_ppm_peak is not None:
        return (
            'emission_unbounded',
            'occupied observation exists but no material-emission '
            'evidence bound — room chemistry source unknown',
        )
    return (
        'qualified_indoor',
        'occupied-room observation plus material-emission evidence',
    )


INDOOR_LABELS: dict[str, str] = {
    'qualified_indoor': '室内環境適格',
    'design_is_not_occupied_outcome': '設計は占有結果ではない',
    'certificate_is_not_room_iaq': '認証は室内IAQではない',
    'emission_unbounded': '放出上限なし',
    'observation_unqualified': '観測未適格',
}
