from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite, log10, sqrt
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_equipment import EquipmentDefinition, FrequencyDomain
from .cad_repository import SceneRevision
from .cad_scene import (
    Position3,
    SceneEntity,
    acoustic_reference_position,
    quaternion_to_matrix3,
)
from .cad_system_variant import SystemVariant, materialize_system_variant
from .optimization_objectives import (
    ObjectiveDefinition,
    ObjectiveMetric,
    ObjectiveState,
    ObjectiveValidDomain,
    ObjectiveVector,
)


DIRECT_LEVEL_SCHEMA_VERSION = 1
DIRECT_LEVEL_AUTHORITY_VERSION = 'o100d-direct-equipment-derived-1'
DISTANCE_LEVEL_MODEL_ID = 'free-field-spherical-pressure-decay'
DISTANCE_LEVEL_MODEL_VERSION = '20log10-distance-ratio-1'
INPUT_NORMALIZATION_MODEL_ID = 'matched-reference-input-scaling'
INPUT_NORMALIZATION_MODEL_VERSION = 'voltage20-power10-log-ratio-1'
OBJECTIVE_COMPARISON_MODEL_ID = 'o100d-direct-equipment-derived-objective'

InputQuantity = Literal['voltage_v_rms', 'power_w']


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


def _finite(value: float, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


class DirectLevelFrequencyBand(BaseModel):
    model_config = ConfigDict(frozen=True)

    low_hz: float = Field(gt=0.0)
    high_hz: float = Field(gt=0.0)

    @field_validator('low_hz', 'high_hz')
    @classmethod
    def finite_frequency(cls, value: float) -> float:
        return _finite(value, field_name='frequency')

    @model_validator(mode='after')
    def valid_band(self) -> 'DirectLevelFrequencyBand':
        if self.high_hz <= self.low_hz:
            raise ValueError('direct-level frequency band high_hz must exceed low_hz')
        return self


class ReferenceInputCondition(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_quantity: InputQuantity
    input_value: float = Field(gt=0.0)

    @field_validator('input_value')
    @classmethod
    def finite_input(cls, value: float) -> float:
        return _finite(value, field_name='reference input')


class SeatPopulation(BaseModel):
    """Exact receiver population; no implicit percentile or missing-seat policy."""

    model_config = ConfigDict(frozen=True)

    population_id: str = Field(min_length=1)
    seat_entity_ids: tuple[str, ...] = Field(min_length=1)
    receiver_reference_semantics: Literal[
        'scene_acoustic_reference_required'
    ] = 'scene_acoustic_reference_required'
    population_weighting: Literal[
        'equal_unweighted'
    ] = 'equal_unweighted'

    @model_validator(mode='after')
    def unique_seats(self) -> 'SeatPopulation':
        if len(self.seat_entity_ids) != len(set(self.seat_entity_ids)):
            raise ValueError('seat population entity IDs must be unique')
        return self


class DistanceLevelAuthority(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_id: Literal[
        'free-field-spherical-pressure-decay'
    ] = DISTANCE_LEVEL_MODEL_ID
    model_version: Literal[
        '20log10-distance-ratio-1'
    ] = DISTANCE_LEVEL_MODEL_VERSION
    equation: Literal[
        'level_at_r=level_at_ref+20*log10(ref_distance_m/r_m)'
    ] = 'level_at_r=level_at_ref+20*log10(ref_distance_m/r_m)'


class InputNormalizationAuthority(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_id: Literal[
        'matched-reference-input-scaling'
    ] = INPUT_NORMALIZATION_MODEL_ID
    model_version: Literal[
        'voltage20-power10-log-ratio-1'
    ] = INPUT_NORMALIZATION_MODEL_VERSION
    voltage_equation: Literal[
        'delta_db=20*log10(voltage_v_rms/reference_voltage_v_rms)'
    ] = 'delta_db=20*log10(voltage_v_rms/reference_voltage_v_rms)'
    power_equation: Literal[
        'delta_db=10*log10(power_w/reference_power_w)'
    ] = 'delta_db=10*log10(power_w/reference_power_w)'


class PlaybackExcitationScenario(BaseModel):
    """Versioned single-channel O100D playback/excitation authority."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = DIRECT_LEVEL_SCHEMA_VERSION
    authority_version: Literal[
        'o100d-direct-equipment-derived-1'
    ] = DIRECT_LEVEL_AUTHORITY_VERSION
    source_entity_id: str = Field(min_length=1)
    channel_role_id: str = Field(min_length=1)
    reference_input: ReferenceInputCondition
    target_spl_db_spl: float
    target_reference_condition: str = Field(min_length=1)
    continuous_reference_duration_s: float = Field(gt=0.0)
    peak_reference_duration_s: float = Field(gt=0.0)
    frequency_band: DirectLevelFrequencyBand
    weighting: str = Field(min_length=1)
    receiver_population: SeatPopulation
    aggregation_semantics: Literal[
        'single_channel_no_coherent_sum'
    ] = 'single_channel_no_coherent_sum'
    level_semantics: Literal[
        'direct_equipment_derived_no_room_gain_no_reflections'
    ] = 'direct_equipment_derived_no_room_gain_no_reflections'
    distance_authority: DistanceLevelAuthority = DistanceLevelAuthority()
    input_normalization_authority: InputNormalizationAuthority = (
        InputNormalizationAuthority()
    )
    scenario_id: str = Field(min_length=1)
    scenario_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @field_validator(
        'target_spl_db_spl',
        'continuous_reference_duration_s',
        'peak_reference_duration_s',
    )
    @classmethod
    def finite_value(cls, value: float) -> float:
        return _finite(value, field_name='scenario value')

    @model_validator(mode='after')
    def valid_identity(self) -> 'PlaybackExcitationScenario':
        digest = _digest(self.semantic_payload())
        if self.scenario_sha256 != digest:
            raise ValueError('playback/excitation scenario semantic hash mismatch')
        if self.scenario_id != _semantic_id('playback', digest):
            raise ValueError('playback/excitation scenario ID does not match semantic hash')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'source_entity_id': self.source_entity_id,
            'channel_role_id': self.channel_role_id,
            'reference_input': self.reference_input.model_dump(mode='json'),
            'target_spl_db_spl': self.target_spl_db_spl,
            'target_reference_condition': self.target_reference_condition,
            'continuous_reference_duration_s': self.continuous_reference_duration_s,
            'peak_reference_duration_s': self.peak_reference_duration_s,
            'frequency_band': self.frequency_band.model_dump(mode='json'),
            'weighting': self.weighting,
            'receiver_population': self.receiver_population.model_dump(mode='json'),
            'aggregation_semantics': self.aggregation_semantics,
            'level_semantics': self.level_semantics,
            'distance_authority': self.distance_authority.model_dump(mode='json'),
            'input_normalization_authority': (
                self.input_normalization_authority.model_dump(mode='json')
            ),
        }


def build_playback_excitation_scenario(
    *,
    source_entity_id: str,
    channel_role_id: str,
    reference_input: ReferenceInputCondition,
    target_spl_db_spl: float,
    target_reference_condition: str,
    continuous_reference_duration_s: float,
    peak_reference_duration_s: float,
    frequency_band: DirectLevelFrequencyBand,
    weighting: str,
    receiver_population: SeatPopulation,
) -> PlaybackExcitationScenario:
    identity = {
        'schema_version': DIRECT_LEVEL_SCHEMA_VERSION,
        'authority_version': DIRECT_LEVEL_AUTHORITY_VERSION,
        'source_entity_id': source_entity_id,
        'channel_role_id': channel_role_id,
        'reference_input': reference_input.model_dump(mode='json'),
        'target_spl_db_spl': float(target_spl_db_spl),
        'target_reference_condition': target_reference_condition,
        'continuous_reference_duration_s': float(continuous_reference_duration_s),
        'peak_reference_duration_s': float(peak_reference_duration_s),
        'frequency_band': frequency_band.model_dump(mode='json'),
        'weighting': weighting,
        'receiver_population': receiver_population.model_dump(mode='json'),
        'aggregation_semantics': 'single_channel_no_coherent_sum',
        'level_semantics': 'direct_equipment_derived_no_room_gain_no_reflections',
        'distance_authority': DistanceLevelAuthority().model_dump(mode='json'),
        'input_normalization_authority': (
            InputNormalizationAuthority().model_dump(mode='json')
        ),
    }
    digest = _digest(identity)
    return PlaybackExcitationScenario(
        source_entity_id=source_entity_id,
        channel_role_id=channel_role_id,
        reference_input=reference_input,
        target_spl_db_spl=target_spl_db_spl,
        target_reference_condition=target_reference_condition,
        continuous_reference_duration_s=continuous_reference_duration_s,
        peak_reference_duration_s=peak_reference_duration_s,
        frequency_band=frequency_band,
        weighting=weighting,
        receiver_population=receiver_population,
        scenario_id=_semantic_id('playback', digest),
        scenario_sha256=digest,
    )


class DirectLevelScalarResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: ObjectiveState
    value: float | None
    unit: Literal['dB', 'dB SPL']
    reason: str | None = None

    @model_validator(mode='after')
    def valid_state(self) -> 'DirectLevelScalarResult':
        if self.state == 'available':
            if self.value is None or not isfinite(float(self.value)):
                raise ValueError('available direct-level result requires finite value')
            if self.reason is not None:
                raise ValueError('available direct-level result must not carry failure reason')
        else:
            if self.value is not None:
                raise ValueError('missing/unsupported direct-level result must not carry value')
            if self.reason is None:
                raise ValueError('missing/unsupported direct-level result requires reason')
        return self


class SeatDirectLevelResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    seat_entity_id: str = Field(min_length=1)
    receiver_position_m: Position3 | None = None
    distance_m: float | None = Field(default=None, gt=0.0)
    direct_level: DirectLevelScalarResult
    target_margin: DirectLevelScalarResult
    continuous_headroom: DirectLevelScalarResult
    peak_headroom: DirectLevelScalarResult

    @field_validator('distance_m')
    @classmethod
    def finite_distance(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='seat distance')


class DirectLevelAggregates(BaseModel):
    model_config = ConfigDict(frozen=True)

    worst_seat_direct_level: DirectLevelScalarResult
    seat_to_seat_direct_level_spread: DirectLevelScalarResult
    worst_seat_target_margin: DirectLevelScalarResult
    worst_seat_continuous_headroom: DirectLevelScalarResult
    worst_seat_peak_headroom: DirectLevelScalarResult


class DirectLevelEvaluation(BaseModel):
    """Self-contained immutable direct/equipment-derived O100D evaluation evidence."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = DIRECT_LEVEL_SCHEMA_VERSION
    authority_version: Literal[
        'o100d-direct-equipment-derived-1'
    ] = DIRECT_LEVEL_AUTHORITY_VERSION
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    variant_id: str = Field(min_length=1)
    variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    equipment_definition_id: str = Field(min_length=1)
    equipment_definition_version: str = Field(min_length=1)
    equipment_definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    scenario: PlaybackExcitationScenario
    source_reference_position_m: Position3
    seat_results: tuple[SeatDirectLevelResult, ...] = Field(min_length=1)
    aggregates: DirectLevelAggregates
    evaluation_id: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_identity(self) -> 'DirectLevelEvaluation':
        expected_seats = self.scenario.receiver_population.seat_entity_ids
        actual_seats = tuple(item.seat_entity_id for item in self.seat_results)
        if actual_seats != expected_seats:
            raise ValueError('direct-level seat results do not match exact receiver population')
        digest = _digest(self.identity_payload())
        if self.evaluation_sha256 != digest:
            raise ValueError('direct-level evaluation semantic hash mismatch')
        if self.evaluation_id != _semantic_id('o100d', digest):
            raise ValueError('direct-level evaluation ID does not match semantic hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'variant_id': self.variant_id,
            'variant_sha256': self.variant_sha256,
            'equipment_definition_id': self.equipment_definition_id,
            'equipment_definition_version': self.equipment_definition_version,
            'equipment_definition_sha256': self.equipment_definition_sha256,
            'scenario': self.scenario.model_dump(mode='json'),
            'source_reference_position_m': self.source_reference_position_m.model_dump(
                mode='json'
            ),
            'seat_results': [
                item.model_dump(mode='json') for item in self.seat_results
            ],
            'aggregates': self.aggregates.model_dump(mode='json'),
        }


def _available(value: float, unit: Literal['dB', 'dB SPL']) -> DirectLevelScalarResult:
    return DirectLevelScalarResult(state='available', value=float(value), unit=unit)


def _missing(reason: str, unit: Literal['dB', 'dB SPL']) -> DirectLevelScalarResult:
    return DirectLevelScalarResult(state='missing', value=None, unit=unit, reason=reason)


def _unsupported(reason: str, unit: Literal['dB', 'dB SPL']) -> DirectLevelScalarResult:
    return DirectLevelScalarResult(state='unsupported', value=None, unit=unit, reason=reason)


def _band_state(
    domain: FrequencyDomain | None,
    requested: DirectLevelFrequencyBand,
    *,
    source_name: str,
) -> DirectLevelScalarResult | None:
    if domain is None:
        return _missing(
            f'{source_name} has no explicit valid frequency domain',
            'dB',
        )
    if (
        requested.low_hz < domain.minimum_hz
        or requested.high_hz > domain.maximum_hz
    ):
        return _unsupported(
            f'requested frequency band is outside {source_name} valid domain',
            'dB',
        )
    return None


def _source_reference_position(
    entity: SceneEntity,
    definition: EquipmentDefinition,
) -> Position3:
    matrix = quaternion_to_matrix3(entity.orientation)
    offset = definition.acoustic_reference_point_m
    local = (offset.x_m, offset.y_m, offset.z_m)
    rotated = tuple(
        sum(matrix[row][column] * local[column] for column in range(3))
        for row in range(3)
    )
    return Position3(
        x_m=entity.position.x_m + rotated[0],
        y_m=entity.position.y_m + rotated[1],
        z_m=entity.position.z_m + rotated[2],
    )


def _distance(a: Position3, b: Position3) -> float:
    return sqrt(
        (a.x_m - b.x_m) ** 2
        + (a.y_m - b.y_m) ** 2
        + (a.z_m - b.z_m) ** 2
    )


def _distance_adjustment_db(reference_distance_m: float, distance_m: float) -> float:
    return 20.0 * log10(float(reference_distance_m) / float(distance_m))


def _input_adjustment_db(
    requested: ReferenceInputCondition,
    *,
    reference_quantity: InputQuantity,
    reference_value: float,
) -> float | None:
    if requested.input_quantity != reference_quantity:
        return None
    ratio = float(requested.input_value) / float(reference_value)
    if requested.input_quantity == 'voltage_v_rms':
        return 20.0 * log10(ratio)
    return 10.0 * log10(ratio)


def _sensitivity_direct_level(
    definition: EquipmentDefinition,
    scenario: PlaybackExcitationScenario,
    distance_m: float,
) -> DirectLevelScalarResult:
    sensitivity = definition.sensitivity
    if sensitivity is None:
        return _missing('equipment sensitivity/reference level is not evidenced', 'dB SPL')

    band_issue = _band_state(
        sensitivity.valid_frequency_domain,
        scenario.frequency_band,
        source_name='sensitivity',
    )
    if band_issue is not None:
        return band_issue.model_copy(update={'unit': 'dB SPL'})

    declared_weighting = sensitivity.weighting
    if declared_weighting is None:
        if scenario.weighting != 'unweighted':
            return _unsupported(
                'sensitivity has no weighting authority for requested weighting',
                'dB SPL',
            )
    elif declared_weighting != scenario.weighting:
        return _unsupported(
            'sensitivity weighting does not match playback scenario',
            'dB SPL',
        )

    input_adjustment = _input_adjustment_db(
        scenario.reference_input,
        reference_quantity=sensitivity.input_quantity,
        reference_value=sensitivity.input_value,
    )
    if input_adjustment is None:
        return _unsupported(
            'playback input quantity differs from sensitivity reference and no '
            'voltage/power conversion authority is available',
            'dB SPL',
        )

    value = (
        float(sensitivity.level_db_spl)
        + input_adjustment
        + _distance_adjustment_db(sensitivity.distance_m, distance_m)
    )
    return _available(value, 'dB SPL')


def _capability_headroom(
    definition: EquipmentDefinition,
    scenario: PlaybackExcitationScenario,
    distance_m: float,
    *,
    kind: Literal['continuous', 'peak'],
) -> DirectLevelScalarResult:
    capability = definition.spl_capability
    if capability is None:
        return _missing('equipment SPL capability is not evidenced', 'dB')

    if kind == 'continuous':
        level = capability.continuous_db_spl
        declared_duration = capability.continuous_duration_s
        requested_duration = scenario.continuous_reference_duration_s
    else:
        level = capability.peak_db_spl
        declared_duration = capability.peak_duration_s
        requested_duration = scenario.peak_reference_duration_s

    if level is None:
        return _missing(f'{kind} SPL capability is not evidenced', 'dB')

    band_issue = _band_state(
        capability.valid_frequency_domain,
        scenario.frequency_band,
        source_name=f'{kind} SPL capability',
    )
    if band_issue is not None:
        return band_issue

    if scenario.weighting != 'unweighted':
        return _unsupported(
            'SPL capability has no weighting provenance for requested weighting',
            'dB',
        )
    if declared_duration is None:
        return _missing(f'{kind} SPL capability has no duration authority', 'dB')
    if abs(float(declared_duration) - float(requested_duration)) > 1e-9:
        return _unsupported(
            f'{kind} duration differs from evidenced equipment capability duration',
            'dB',
        )

    seat_capability = float(level) + _distance_adjustment_db(
        capability.reference_distance_m,
        distance_m,
    )
    return _available(seat_capability - scenario.target_spl_db_spl, 'dB')


def _derived_margin(
    direct_level: DirectLevelScalarResult,
    target_spl_db_spl: float,
) -> DirectLevelScalarResult:
    if direct_level.state == 'available':
        assert direct_level.value is not None
        return _available(float(direct_level.value) - float(target_spl_db_spl), 'dB')
    if direct_level.state == 'missing':
        return _missing(
            direct_level.reason or 'direct level is missing',
            'dB',
        )
    return _unsupported(
        direct_level.reason or 'direct level is unsupported',
        'dB',
    )


def _aggregate_min(
    values: Sequence[DirectLevelScalarResult],
    *,
    unit: Literal['dB', 'dB SPL'],
    label: str,
) -> DirectLevelScalarResult:
    if any(item.state == 'unsupported' for item in values):
        return _unsupported(
            f'{label} unavailable because at least one seat is unsupported',
            unit,
        )
    if any(item.state == 'missing' for item in values):
        return _missing(
            f'{label} unavailable because at least one seat is missing evidence',
            unit,
        )
    available = [float(item.value) for item in values if item.value is not None]
    return _available(min(available), unit)


def _aggregate_spread(
    values: Sequence[DirectLevelScalarResult],
) -> DirectLevelScalarResult:
    if len(values) < 2:
        return _unsupported(
            'seat-to-seat direct-level spread requires at least two seats',
            'dB',
        )
    if any(item.state == 'unsupported' for item in values):
        return _unsupported(
            'seat-to-seat direct-level spread unavailable because at least one '
            'seat is unsupported',
            'dB',
        )
    if any(item.state == 'missing' for item in values):
        return _missing(
            'seat-to-seat direct-level spread unavailable because at least one '
            'seat is missing evidence',
            'dB',
        )
    available = [float(item.value) for item in values if item.value is not None]
    return _available(max(available) - min(available), 'dB')


def evaluate_direct_level(
    *,
    revision: SceneRevision,
    variant: SystemVariant,
    equipment_definition: EquipmentDefinition,
    scenario: PlaybackExcitationScenario,
) -> DirectLevelEvaluation:
    """Evaluate one channel without room gain, reflections, directivity loss, or channel summation."""

    if (
        variant.document_id != revision.document_id
        or variant.baseline_revision_id != revision.revision_id
        or variant.baseline_content_hash != revision.content_hash
    ):
        raise ValueError('direct-level SystemVariant/SceneRevision authority mismatch')

    bindings = [
        item
        for item in variant.equipment_bindings
        if item.entity_id == scenario.source_entity_id
    ]
    if len(bindings) != 1:
        raise ValueError(
            'direct-level scenario requires exactly one equipment binding for source entity'
        )
    binding = bindings[0]
    if (
        binding.equipment_definition_id != equipment_definition.definition_id
        or binding.equipment_definition_version != equipment_definition.version
        or binding.equipment_definition_sha256 != equipment_definition.semantic_sha256
    ):
        raise ValueError('direct-level EquipmentDefinition binding mismatch')

    scene = materialize_system_variant(revision, variant)
    try:
        source = scene.entity(scenario.source_entity_id)
    except KeyError as exc:
        raise ValueError('direct-level source entity is missing from SystemVariant') from exc
    if source.kind != 'speaker':
        raise ValueError('direct-level source entity must be a speaker')
    if source.speaker_role != scenario.channel_role_id:
        raise ValueError('direct-level channel role does not match source speaker role')

    source_reference = _source_reference_position(source, equipment_definition)
    seat_results: list[SeatDirectLevelResult] = []

    for seat_id in scenario.receiver_population.seat_entity_ids:
        try:
            seat = scene.entity(seat_id)
        except KeyError:
            unsupported_level = _unsupported(
                'receiver seat entity is missing from SystemVariant scene',
                'dB SPL',
            )
            seat_results.append(
                SeatDirectLevelResult(
                    seat_entity_id=seat_id,
                    direct_level=unsupported_level,
                    target_margin=_unsupported(
                        unsupported_level.reason or 'missing receiver',
                        'dB',
                    ),
                    continuous_headroom=_unsupported(
                        unsupported_level.reason or 'missing receiver',
                        'dB',
                    ),
                    peak_headroom=_unsupported(
                        unsupported_level.reason or 'missing receiver',
                        'dB',
                    ),
                )
            )
            continue

        if seat.kind != 'seat':
            reason = 'receiver population member is not a seat entity'
            seat_results.append(
                SeatDirectLevelResult(
                    seat_entity_id=seat_id,
                    direct_level=_unsupported(reason, 'dB SPL'),
                    target_margin=_unsupported(reason, 'dB'),
                    continuous_headroom=_unsupported(reason, 'dB'),
                    peak_headroom=_unsupported(reason, 'dB'),
                )
            )
            continue

        receiver = acoustic_reference_position(seat)
        if receiver is None:
            reason = 'seat has no explicit scene acoustic reference position'
            seat_results.append(
                SeatDirectLevelResult(
                    seat_entity_id=seat_id,
                    direct_level=_unsupported(reason, 'dB SPL'),
                    target_margin=_unsupported(reason, 'dB'),
                    continuous_headroom=_unsupported(reason, 'dB'),
                    peak_headroom=_unsupported(reason, 'dB'),
                )
            )
            continue

        distance_m = _distance(source_reference, receiver)
        if distance_m <= 1e-12:
            reason = 'source and receiver acoustic reference positions coincide'
            seat_results.append(
                SeatDirectLevelResult(
                    seat_entity_id=seat_id,
                    receiver_position_m=receiver,
                    direct_level=_unsupported(reason, 'dB SPL'),
                    target_margin=_unsupported(reason, 'dB'),
                    continuous_headroom=_unsupported(reason, 'dB'),
                    peak_headroom=_unsupported(reason, 'dB'),
                )
            )
            continue

        direct_level = _sensitivity_direct_level(
            equipment_definition,
            scenario,
            distance_m,
        )
        seat_results.append(
            SeatDirectLevelResult(
                seat_entity_id=seat_id,
                receiver_position_m=receiver,
                distance_m=distance_m,
                direct_level=direct_level,
                target_margin=_derived_margin(
                    direct_level,
                    scenario.target_spl_db_spl,
                ),
                continuous_headroom=_capability_headroom(
                    equipment_definition,
                    scenario,
                    distance_m,
                    kind='continuous',
                ),
                peak_headroom=_capability_headroom(
                    equipment_definition,
                    scenario,
                    distance_m,
                    kind='peak',
                ),
            )
        )

    direct_values = [item.direct_level for item in seat_results]
    target_values = [item.target_margin for item in seat_results]
    continuous_values = [item.continuous_headroom for item in seat_results]
    peak_values = [item.peak_headroom for item in seat_results]
    aggregates = DirectLevelAggregates(
        worst_seat_direct_level=_aggregate_min(
            direct_values,
            unit='dB SPL',
            label='worst-seat direct level',
        ),
        seat_to_seat_direct_level_spread=_aggregate_spread(direct_values),
        worst_seat_target_margin=_aggregate_min(
            target_values,
            unit='dB',
            label='worst-seat target margin',
        ),
        worst_seat_continuous_headroom=_aggregate_min(
            continuous_values,
            unit='dB',
            label='worst-seat continuous headroom',
        ),
        worst_seat_peak_headroom=_aggregate_min(
            peak_values,
            unit='dB',
            label='worst-seat peak headroom',
        ),
    )
    identity = {
        'schema_version': DIRECT_LEVEL_SCHEMA_VERSION,
        'authority_version': DIRECT_LEVEL_AUTHORITY_VERSION,
        'document_id': revision.document_id,
        'scene_revision_id': revision.revision_id,
        'scene_content_hash': revision.content_hash,
        'variant_id': variant.variant_id,
        'variant_sha256': variant.variant_sha256,
        'equipment_definition_id': equipment_definition.definition_id,
        'equipment_definition_version': equipment_definition.version,
        'equipment_definition_sha256': equipment_definition.semantic_sha256,
        'scenario': scenario.model_dump(mode='json'),
        'source_reference_position_m': source_reference.model_dump(mode='json'),
        'seat_results': [item.model_dump(mode='json') for item in seat_results],
        'aggregates': aggregates.model_dump(mode='json'),
    }
    digest = _digest(identity)
    return DirectLevelEvaluation(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        variant_id=variant.variant_id,
        variant_sha256=variant.variant_sha256,
        equipment_definition_id=equipment_definition.definition_id,
        equipment_definition_version=equipment_definition.version,
        equipment_definition_sha256=equipment_definition.semantic_sha256,
        scenario=scenario,
        source_reference_position_m=source_reference,
        seat_results=tuple(seat_results),
        aggregates=aggregates,
        evaluation_id=_semantic_id('o100d', digest),
        evaluation_sha256=digest,
    )


def _objective_definition(
    scenario: PlaybackExcitationScenario,
    *,
    objective_id: str,
    quantity: str,
    unit: str,
    direction: Literal['minimize', 'maximize'],
    nonnegative: bool = False,
) -> ObjectiveDefinition:
    return ObjectiveDefinition(
        objective_id=objective_id,
        quantity=quantity,
        unit=unit,
        direction=direction,
        valid_domain=(
            ObjectiveValidDomain(
                kind='bounded_real',
                minimum=0.0,
            )
            if nonnegative
            else ObjectiveValidDomain(kind='finite_real')
        ),
        comparison_model_id=OBJECTIVE_COMPARISON_MODEL_ID,
        comparison_model_version=scenario.scenario_sha256,
    )


def direct_level_objective_definitions(
    scenario: PlaybackExcitationScenario,
) -> tuple[ObjectiveDefinition, ...]:
    return (
        _objective_definition(
            scenario,
            objective_id='o100d.direct_level.worst_seat_db_spl',
            quantity='direct_equipment_derived_worst_seat_spl',
            unit='dB SPL',
            direction='maximize',
        ),
        _objective_definition(
            scenario,
            objective_id='o100d.direct_level.seat_to_seat_spread_db',
            quantity='direct_equipment_derived_seat_to_seat_spread',
            unit='dB',
            direction='minimize',
            nonnegative=True,
        ),
        _objective_definition(
            scenario,
            objective_id='o100d.target_margin.worst_seat_db',
            quantity='direct_equipment_derived_target_spl_margin',
            unit='dB',
            direction='maximize',
        ),
        _objective_definition(
            scenario,
            objective_id='o100d.continuous_headroom.worst_seat_db',
            quantity='direct_equipment_derived_continuous_headroom_margin',
            unit='dB',
            direction='maximize',
        ),
        _objective_definition(
            scenario,
            objective_id='o100d.peak_headroom.worst_seat_db',
            quantity='direct_equipment_derived_peak_headroom_margin',
            unit='dB',
            direction='maximize',
        ),
    )


def direct_level_objective_vector(
    evaluation: DirectLevelEvaluation,
) -> ObjectiveVector:
    definitions = direct_level_objective_definitions(evaluation.scenario)
    results = (
        evaluation.aggregates.worst_seat_direct_level,
        evaluation.aggregates.seat_to_seat_direct_level_spread,
        evaluation.aggregates.worst_seat_target_margin,
        evaluation.aggregates.worst_seat_continuous_headroom,
        evaluation.aggregates.worst_seat_peak_headroom,
    )
    metrics = tuple(
        ObjectiveMetric(
            objective_id=definition.objective_id,
            value=result.value,
            unit=definition.unit,
            direction=definition.direction,
            state=result.state,
            definition=definition,
        )
        for definition, result in zip(definitions, results, strict=True)
    )
    return ObjectiveVector(
        candidate_id=evaluation.variant_id,
        metrics=metrics,
    )
