"""Speech-intelligibility / STI diagnostic authority (#997, STI10 slice).

IEC 60268-16:2020 STI is imported from a qualified producer report (REW)
only — this module never reimplements STI internally, and an STI value is
never derived from a magnitude frequency response, C50 or RT60 alone.

The spec pins every authority the imported result depends on: the exact
impulse response, the noise authority, the absolute-level authority and the
producer identity/build. If the producer's calculation parameters are not
recoverable from the bound report, the result is marked producer-dependent
for replay. Per-seat raw values are preserved; no universal pass threshold
exists.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest


STI_SCHEMA_VERSION = 1
STI_SPEC_AUTHORITY_VERSION = 'sti-import-spec-1'
STI_RESULT_AUTHORITY_VERSION = 'sti-import-result-1'

StiProducer = Literal['rew']
StiMethod = Literal['iec_60268_16_2020']
StiSeatState = Literal['reported', 'blocked', 'unavailable']






class StiAuthorityRef(BaseModel):
    """Pinned reference to an authority the imported STI depends on."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_id: str = Field(min_length=1)
    authority_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class StiImpulseEvidenceRef(BaseModel):
    """Exact IR the producer measurement was taken/derived from."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: Literal['predicted', 'measured']
    artifact_id: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    sample_rate_hz: int = Field(gt=0)
    receiver_id: str = Field(min_length=1)
    receiver_entity_id: str = Field(min_length=1)
    mic_applicability_authority: StiAuthorityRef


class StiProducerRef(BaseModel):
    """Qualified producer of the imported STI report (STI10: REW only)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    producer: StiProducer
    producer_version: str = Field(min_length=1)
    producer_build: str | None = None
    method: StiMethod
    report_artifact_id: str = Field(min_length=1)
    report_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    # True when the producer's calculation parameters (windowing, masking,
    # level authority selection) are recoverable from the bound report.
    producer_parameters_recoverable: bool


class SpeechIntelligibilityAnalysisSpec(BaseModel):
    """Pinned STI import spec: authorities + producer identity (#997)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = STI_SCHEMA_VERSION
    authority_version: Literal[
        'sti-import-spec-1'
    ] = STI_SPEC_AUTHORITY_VERSION
    spec_id: str = Field(
        pattern=r'^speech-intelligibility-spec:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    acoustic_scene_snapshot_id: str = Field(min_length=1)
    acoustic_scene_snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_scenario_id: str = Field(min_length=1)
    source_scenario_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_routing_identity: StiAuthorityRef

    impulse_evidence: StiImpulseEvidenceRef
    noise_authority: StiAuthorityRef
    absolute_level_authority: StiAuthorityRef
    producer: StiProducerRef
    seat_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def validate_spec(self) -> 'SpeechIntelligibilityAnalysisSpec':
        if len(set(self.seat_ids)) != len(self.seat_ids):
            raise ValueError('seat ids must be unique')
        if self.impulse_evidence.kind not in ('predicted', 'measured'):
            raise ValueError('impulse evidence kind must be pinned')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('STI spec semantic hash mismatch')
        if self.spec_id != f'speech-intelligibility-spec:{expected}':
            raise ValueError('STI spec id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'spec_id', 'semantic_sha256'},
        )


def build_speech_intelligibility_spec(
    **kwargs: Any,
) -> SpeechIntelligibilityAnalysisSpec:
    probe = SpeechIntelligibilityAnalysisSpec.model_construct(
        spec_id='speech-intelligibility-spec:' + '0' * 64,
        semantic_sha256='0' * 64,
        **kwargs,
    )
    digest = _digest(probe.semantic_payload())
    return SpeechIntelligibilityAnalysisSpec(
        spec_id=f'speech-intelligibility-spec:{digest}',
        semantic_sha256=digest,
        **kwargs,
    )


class SeatStiValue(BaseModel):
    """One seat's raw imported STI value — never thresholded here."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    seat_id: str = Field(min_length=1)
    state: StiSeatState
    sti: float | None = Field(default=None, ge=0.0, le=1.0)
    detail: str | None = None

    @model_validator(mode='after')
    def validate_seat(self) -> 'SeatStiValue':
        if self.state == 'reported':
            if self.sti is None:
                raise ValueError('a reported seat requires its STI value')
            if not math.isfinite(float(self.sti)):
                raise ValueError('STI must be finite')
        elif self.sti is not None:
            raise ValueError(
                'blocked/unavailable seats never carry an STI value'
            )
        if self.state != 'reported' and not self.detail:
            raise ValueError('blocked/unavailable seats require detail')
        return self


class SpeechIntelligibilityResult(BaseModel):
    """Imported STI evidence — derived, producer-bound, not acoustic truth."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = STI_SCHEMA_VERSION
    authority_version: Literal[
        'sti-import-result-1'
    ] = STI_RESULT_AUTHORITY_VERSION
    result_id: str = Field(
        pattern=r'^speech-intelligibility-result:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    spec_id: str = Field(
        pattern=r'^speech-intelligibility-spec:[0-9a-f]{64}$'
    )
    spec_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    per_seat: tuple[SeatStiValue, ...] = Field(min_length=1)
    replay_semantics: Literal[
        'producer_parameters_pinned', 'producer_dependent'
    ]

    @model_validator(mode='after')
    def validate_result(self) -> 'SpeechIntelligibilityResult':
        seat_ids = [seat.seat_id for seat in self.per_seat]
        if len(set(seat_ids)) != len(seat_ids):
            raise ValueError('per-seat STI values must be unique per seat')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('STI result semantic hash mismatch')
        if self.result_id != f'speech-intelligibility-result:{expected}':
            raise ValueError('STI result id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'result_id', 'semantic_sha256'},
        )


def import_sti_producer_report(
    spec: SpeechIntelligibilityAnalysisSpec,
    *,
    per_seat_values: dict[str, float | None],
) -> SpeechIntelligibilityResult:
    """Import a producer-derived STI report bound to the spec's authorities.

    ``per_seat_values`` maps each pinned seat to its raw STI (0..1) or None
    when the producer could not report it; missing seats are recorded
    ``unavailable`` rather than silently dropped.
    """
    seats: list[SeatStiValue] = []
    for seat_id in spec.seat_ids:
        if seat_id not in per_seat_values:
            seats.append(
                SeatStiValue(
                    seat_id=seat_id,
                    state='unavailable',
                    detail='producer report contains no value for this seat',
                )
            )
            continue
        value = per_seat_values[seat_id]
        if value is None:
            seats.append(
                SeatStiValue(
                    seat_id=seat_id,
                    state='blocked',
                    detail='producer could not report STI for this seat',
                )
            )
            continue
        seats.append(
            SeatStiValue(seat_id=seat_id, state='reported', sti=float(value))
        )
    replay_semantics = (
        'producer_parameters_pinned'
        if spec.producer.producer_parameters_recoverable
        else 'producer_dependent'
    )
    payload = {
        'schema_version': STI_SCHEMA_VERSION,
        'authority_version': STI_RESULT_AUTHORITY_VERSION,
        'spec_id': spec.spec_id,
        'spec_semantic_sha256': spec.semantic_sha256,
        'per_seat': [seat.model_dump(mode='json') for seat in seats],
        'replay_semantics': replay_semantics,
    }
    digest = _digest(payload)
    return SpeechIntelligibilityResult(
        spec_id=spec.spec_id,
        spec_semantic_sha256=spec.semantic_sha256,
        per_seat=tuple(seats),
        replay_semantics=replay_semantics,
        result_id=f'speech-intelligibility-result:{digest}',
        semantic_sha256=digest,
    )
