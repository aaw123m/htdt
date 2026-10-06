"""Power sequencing and AC power-quality authority (issues
#736, #738).

Circuit/UPS capacity and a working automation scene do not prove the
AV system enters/leaves operational state in a safe deterministic
order — amplifier last-on/first-off, inrush staging, EDID/HDMI
dependency ordering, brownout transitions and startup transient
evidence need an authority (AVIXA ELEC-104/105). And capacity does
not prove the delivered AC is stable — sags/swells/interruptions/
harmonics need in-situ evidence tied to observed symptoms
(reboots, clipping, relock, hum).

Basis: AVIXA Systems Performance Verification Guide ELEC-104/105;
Q-SYS amplifier sequencing guidance; IEC 61000-4-30 power-quality
measurement classes.
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


class PowerSequencePlan(BaseModel):
    """Declared power-up/power-down ordering (#736) — staged steps
    with dependencies; amplifiers must be last-on / first-off and the
    transient expectation is declared."""

    model_config = ConfigDict(frozen=True)

    plan_id: str
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    steps: tuple[str, ...]
    amplifier_step: str | None = None
    amplifier_last_on_first_off: bool | None = None
    inrush_staged: bool | None = None
    always_on_devices: tuple[str, ...] = ()
    edid_control_dependencies: tuple[str, ...] = ()

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            steps = data.get('steps')
            if not steps:
                raise ValueError(
                    'a power sequence requires declared steps'
                )
            if data.get('amplifier_step') is not None and (
                data.get('amplifier_last_on_first_off') is not True
            ):
                raise ValueError(
                    'an amplifier step must declare '
                    'last-on/first-off behavior'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'plan_id', 'plan_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'PowerSequencePlan':
        return _seal(cls, payload, 'plan_id', 'plan_sha256', 'pseq')


class PowerSequenceEvidence(BaseModel):
    """Executed-sequence evidence (#736) — the run outcome including
    partial/failed/timeout states and startup transient observation."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    plan_ref: AuthorityRef
    outcome: Literal[
        'clean_sequence', 'partial_state', 'timeout',
        'order_violation', 'transient_observed', 'power_loss_event',
        'orderly_ups_shutdown', 'recovery_after_outage',
    ]
    transient_dbfs: float | None = None
    observation_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('plan_ref') is None:
                raise ValueError(
                    'sequence evidence requires a pinned plan'
                )
            if data.get('outcome') == 'clean_sequence' and (
                data.get('observation_ref') is None
            ):
                raise ValueError(
                    'a clean-sequence claim requires observation '
                    'evidence (pop/click/transient absence)'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evidence_id', 'evidence_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'PowerSequenceEvidence':
        return _seal(
            cls, payload, 'evidence_id', 'evidence_sha256', 'psev'
        )


class PowerQualityObservation(BaseModel):
    """In-situ AC power-quality evidence (#738) — instrument-class-
    qualified observations of dips/swells/interruptions/harmonics,
    optionally correlated to observed system symptoms."""

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    instrument_class: Literal[
        'iec_61000_4_30_class_a', 'iec_61000_4_30_class_s',
        'unclassified_monitor', 'ups_telemetry', 'other', 'unknown',
    ]
    observed_events: tuple[str, ...]
    rms_voltage_v: float | None = None
    frequency_hz: float | None = None
    correlated_symptom: str | None = None
    capture_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('instrument_class') not in (
                'iec_61000_4_30_class_a', 'iec_61000_4_30_class_s',
                'unclassified_monitor', 'ups_telemetry', 'other',
                'unknown',
            ):
                raise ValueError('unknown instrument class')
            if data.get('instrument_class') == 'unknown':
                raise ValueError(
                    'a power-quality observation requires a declared '
                    'instrument class (IEC 61000-4-30 method class)'
                )
            if not data.get('observed_events'):
                raise ValueError(
                    'a power-quality observation requires at least '
                    'one observed event or quantity'
                )
            if data.get('correlated_symptom') and (
                data.get('capture_ref') is None
            ):
                raise ValueError(
                    'a symptom correlation requires capture evidence'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'observation_id', 'observation_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'PowerQualityObservation':
        return _seal(
            cls, payload, 'observation_id', 'observation_sha256', 'pqo'
        )


PowerVerdict = Literal[
    'qualified_sequence',
    'capacity_is_not_sequence',
    'order_unverified',
    'transient_unbounded',
    'quality_unobserved',
]


def evaluate_power_claim(
    plan: PowerSequencePlan | None,
    evidence: PowerSequenceEvidence | None,
    quality: PowerQualityObservation | None,
    *,
    capacity_qualified: bool = False,
) -> tuple[PowerVerdict, str]:
    """Judge a power-operability claim (#736/#738)."""
    if plan is None:
        if capacity_qualified:
            return (
                'capacity_is_not_sequence',
                'circuit/UPS capacity does not prove deterministic '
                'operational sequencing',
            )
        return ('order_unverified', 'no sequence plan declared')
    if evidence is None or evidence.outcome not in (
        'clean_sequence', 'orderly_ups_shutdown',
        'recovery_after_outage',
    ):
        return (
            'order_unverified',
            'no successful sequence evidence for the plan',
        )
    if quality is None:
        return (
            'quality_unobserved',
            'sequence verified but AC quality unobserved — stability '
            'of the supply is unknown',
        )
    return (
        'qualified_sequence',
        'sequencing verified with in-situ quality evidence',
    )


POWER_LABELS: dict[str, str] = {
    'qualified_sequence': '電源シーケンス適格',
    'capacity_is_not_sequence': '容量は順序証明ではない',
    'order_unverified': '順序未検証',
    'transient_unbounded': '過渡上限なし',
    'quality_unobserved': '電源品質未観測',
}
