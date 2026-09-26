from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite, log10, sqrt
from typing import TYPE_CHECKING, Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from collections.abc import Callable

    from .cad_listener_pose import ListenerPoseAuthority

from .cad_equipment import EquipmentDefinition, FrequencyDomain, RadialDomain
from .cad_listener_pose import resolve_listener_receiver
from .cad_repository import SceneRevision
from .cad_seat_priority import SeatPriorityProfile
from .cad_scene import (
    Position3,
    SceneEntity,
    quaternion_to_matrix3,
)
from .cad_system_variant import SystemVariant, materialize_system_variant
from .cad_usable_output import (
    ListenerTransferAuthority,
    SourceUsableOutputProfile,
    evaluate_headroom,
)
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
        'equal_unweighted',
        'seat_priority',
    ] = 'equal_unweighted'
    # Exact SeatPriorityProfile binding (#513). Required iff weighting is
    # 'seat_priority'; the bound profile's exact member order must equal
    # ``seat_entity_ids`` (checked by the evaluator, which owns the scene).
    priority_profile_id: str | None = Field(default=None, min_length=1)
    priority_profile_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )

    @model_validator(mode='after')
    def unique_seats(self) -> 'SeatPopulation':
        if len(self.seat_entity_ids) != len(set(self.seat_entity_ids)):
            raise ValueError('seat population entity IDs must be unique')
        if (self.priority_profile_id is None) != (
            self.priority_profile_sha256 is None
        ):
            raise ValueError('priority profile id/hash must be supplied together')
        if self.population_weighting == 'seat_priority':
            if self.priority_profile_id is None:
                raise ValueError(
                    'seat_priority weighting requires an exact profile binding'
                )
        elif self.priority_profile_id is not None:
            raise ValueError(
                'priority profile binding requires seat_priority weighting'
            )
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
    # #964: declared radial/far-field validity domain of the point-source
    # decay model. ``None`` keeps the pre-existing unbounded claim for
    # authority compatibility — it is not evidence that every distance is
    # valid.
    radial_domain: RadialDomain | None = None


def _distance_authority_payload(authority: DistanceLevelAuthority) -> dict:
    """Digest-stable dump: ``radial_domain`` is emitted only when declared."""
    payload = authority.model_dump(mode='json')
    if authority.radial_domain is None:
        payload.pop('radial_domain', None)
    return payload


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
    # Optional nonlinear qualification policy consumed when a
    # SourceUsableOutputProfile is bound (#1047): the measured
    # compression/distortion ceiling qualifies headroom instead of the
    # declared SPL figure. ``None`` = no nonlinear policy declared.
    max_compression_db: float | None = Field(default=None, ge=0.0)
    max_distortion_percent: float | None = Field(default=None, ge=0.0)
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
        payload = {
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
            'distance_authority': _distance_authority_payload(
                self.distance_authority
            ),
            'input_normalization_authority': (
                self.input_normalization_authority.model_dump(mode='json')
            ),
        }
        # Excluded when absent so scenarios persisted before #1047 keep
        # their original identity digest.
        if self.max_compression_db is not None:
            payload['max_compression_db'] = self.max_compression_db
        if self.max_distortion_percent is not None:
            payload['max_distortion_percent'] = self.max_distortion_percent
        return payload


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
    max_compression_db: float | None = None,
    max_distortion_percent: float | None = None,
    radial_domain: RadialDomain | None = None,
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
        'distance_authority': _distance_authority_payload(
            DistanceLevelAuthority(radial_domain=radial_domain)
        ),
        'input_normalization_authority': (
            InputNormalizationAuthority().model_dump(mode='json')
        ),
    }
    # Excluded when absent so scenarios persisted before #1047 keep their
    # original identity digest.
    if max_compression_db is not None:
        identity['max_compression_db'] = float(max_compression_db)
    if max_distortion_percent is not None:
        identity['max_distortion_percent'] = float(max_distortion_percent)
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
        max_compression_db=max_compression_db,
        max_distortion_percent=max_distortion_percent,
        distance_authority=DistanceLevelAuthority(radial_domain=radial_domain),
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


class SeatHeadroomEvidence(BaseModel):
    """The evidence tier one seat headroom result rests on (#1047).

    Records which authority produced the margin — an exact-bound
    SourceUsableOutputProfile at a given capability tier/basis, or the
    declared scalar figure — so a hard headroom objective never hides
    which evidence drove it.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str | None = None
    profile_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    binding: Literal['exact', 'advisory', 'none'] = 'none'
    tier_used: Literal[
        'unknown',
        'scalar',
        'curve',
        'compression',
        'thd',
        'combined',
        'excursion_model',
        'declared',
    ] = 'declared'
    basis: Literal[
        'amplifier_margin',
        'scalar_declared',
        'distortion_qualified',
        'distortion_unqualified',
        'unknown',
        'declared',
    ] = 'declared'


class SeatDirectLevelResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    seat_entity_id: str = Field(min_length=1)
    receiver_position_m: Position3 | None = None
    distance_m: float | None = Field(default=None, gt=0.0)
    direct_level: DirectLevelScalarResult
    target_margin: DirectLevelScalarResult
    continuous_headroom: DirectLevelScalarResult
    peak_headroom: DirectLevelScalarResult
    # Evidence tier per headroom axis (#1047); absent on evaluations
    # produced before the usable-output authority was wired in.
    continuous_headroom_evidence: SeatHeadroomEvidence | None = None
    peak_headroom_evidence: SeatHeadroomEvidence | None = None

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


class SeatPriorityAggregates(BaseModel):
    """Priority-aware aggregates (#513).

    ``weighted_*`` is the sum_to_one weighted mean over the soft-objective
    population (non-diagnostic members, independent of ``required``); the
    ``worst_required_*`` hard floor covers explicitly required seats only.
    Soft and hard membership are structurally separate (#975): an optional
    seat can carry weight without joining the floor, and diagnostics never
    contribute either aggregate. Recorded normalization is the profile's
    exact ``weight_normalization``/``normalization_version``.
    """

    model_config = ConfigDict(frozen=True)

    priority_profile_id: str = Field(min_length=1)
    priority_profile_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    weight_normalization: str = Field(min_length=1)
    normalization_version: str = Field(min_length=1)
    required_seat_entity_ids: tuple[str, ...] = Field(min_length=1)
    normalized_weights: dict[str, float] = Field(min_length=1)
    weighted_direct_level: DirectLevelScalarResult
    weighted_target_margin: DirectLevelScalarResult
    weighted_continuous_headroom: DirectLevelScalarResult
    weighted_peak_headroom: DirectLevelScalarResult
    worst_required_seat_direct_level: DirectLevelScalarResult
    worst_required_seat_target_margin: DirectLevelScalarResult
    worst_required_seat_continuous_headroom: DirectLevelScalarResult
    worst_required_seat_peak_headroom: DirectLevelScalarResult


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
    # The exact-bound usable-output authority consumed for headroom
    # (#1047); embedded so repository replay reproduces the evaluation
    # bit-for-bit. ``None`` = the legacy declared-scalar path.
    usable_output_profile: SourceUsableOutputProfile | None = None
    source_reference_position_m: Position3
    seat_results: tuple[SeatDirectLevelResult, ...] = Field(min_length=1)
    aggregates: DirectLevelAggregates
    # Optional priority extension (#513): identity-stable only when present,
    # so equal-unweighted evaluations keep byte-exact identity.
    priority_aggregates: SeatPriorityAggregates | None = None
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
        payload = {
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
                _seat_result_payload(item) for item in self.seat_results
            ],
            'aggregates': self.aggregates.model_dump(mode='json'),
        }
        if self.usable_output_profile is not None:
            payload['usable_output_profile'] = (
                self.usable_output_profile.model_dump(mode='json')
            )
        if self.priority_aggregates is not None:
            payload['priority_aggregates'] = self.priority_aggregates.model_dump(
                mode='json'
            )
        return payload


def _available(value: float, unit: Literal['dB', 'dB SPL']) -> DirectLevelScalarResult:
    return DirectLevelScalarResult(state='available', value=float(value), unit=unit)


def _missing(reason: str, unit: Literal['dB', 'dB SPL']) -> DirectLevelScalarResult:
    return DirectLevelScalarResult(state='missing', value=None, unit=unit, reason=reason)


def _unsupported(reason: str, unit: Literal['dB', 'dB SPL']) -> DirectLevelScalarResult:
    return DirectLevelScalarResult(state='unsupported', value=None, unit=unit, reason=reason)


def _seat_result_payload(item: SeatDirectLevelResult) -> dict[str, Any]:
    """Serialized seat result; new additive fields are pruned when absent
    so pre-#1047 payloads keep byte-exact identity."""
    payload = item.model_dump(mode='json')
    for key in (
        'continuous_headroom_evidence',
        'peak_headroom_evidence',
    ):
        if payload.get(key) is None:
            payload.pop(key, None)
    return payload


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
    profile: SourceUsableOutputProfile | None = None,
) -> tuple[DirectLevelScalarResult, SeatHeadroomEvidence | None]:
    """Headroom for one seat under the requested duration class.

    With no bound usable-output profile this stays the legacy declared
    scalar path. With an exact-bound profile the evaluation is delegated
    to :func:`cad_usable_output.evaluate_headroom` (#1047): the band
    ceiling is the minimum qualifying in-band measured evidence, the
    declared figure never overrides it, and the listener-side comparison
    runs under the declared distance authority as an explicit
    propagation-model transfer.
    """
    capability = definition.spl_capability

    if profile is not None:
        return _profile_headroom(
            profile, definition, scenario, distance_m, kind=kind
        )
    evidence = SeatHeadroomEvidence(binding='none')

    if capability is None:
        return (
            _missing('equipment SPL capability is not evidenced', 'dB'),
            evidence,
        )

    if kind == 'continuous':
        level = capability.continuous_db_spl
        declared_duration = capability.continuous_duration_s
        requested_duration = scenario.continuous_reference_duration_s
    else:
        level = capability.peak_db_spl
        declared_duration = capability.peak_duration_s
        requested_duration = scenario.peak_reference_duration_s

    if level is None:
        return (
            _missing(f'{kind} SPL capability is not evidenced', 'dB'),
            evidence,
        )

    band_issue = _band_state(
        capability.valid_frequency_domain,
        scenario.frequency_band,
        source_name=f'{kind} SPL capability',
    )
    if band_issue is not None:
        return band_issue, evidence

    if scenario.weighting != 'unweighted':
        return (
            _unsupported(
                'SPL capability has no weighting provenance for requested '
                'weighting',
                'dB',
            ),
            evidence,
        )
    if declared_duration is None:
        return (
            _missing(
                f'{kind} SPL capability has no duration authority', 'dB'
            ),
            evidence,
        )
    if abs(float(declared_duration) - float(requested_duration)) > 1e-9:
        return (
            _unsupported(
                f'{kind} duration differs from evidenced equipment '
                'capability duration',
                'dB',
            ),
            evidence,
        )

    seat_capability = float(level) + _distance_adjustment_db(
        capability.reference_distance_m,
        distance_m,
    )
    return (
        _available(seat_capability - scenario.target_spl_db_spl, 'dB'),
        evidence,
    )


def _profile_headroom(
    profile: SourceUsableOutputProfile,
    definition: EquipmentDefinition,
    scenario: PlaybackExcitationScenario,
    distance_m: float,
    *,
    kind: Literal['continuous', 'peak'],
) -> tuple[DirectLevelScalarResult, SeatHeadroomEvidence]:
    """Usable-output-qualified headroom through the O100D evaluator.

    The profile's equipment binding is verified against the exact
    EquipmentDefinition triple the scenario resolved (#1026); an advisory
    (id-only) profile is never consulted for a hard headroom decision and
    a declared SPL figure never overrides a measured nonlinear ceiling
    (#1047).
    """
    evidence = SeatHeadroomEvidence(
        profile_id=profile.profile_id,
        profile_sha256=profile.profile_sha256,
        binding=profile.binding_class,
        tier_used='unknown',
        basis='unknown',
    )

    if (
        profile.equipment_definition_id != definition.definition_id
        or (
            profile.equipment_definition_version is not None
            and profile.equipment_definition_version != definition.version
        )
        or (
            profile.equipment_definition_sha256 is not None
            and profile.equipment_definition_sha256
            != definition.semantic_sha256
        )
    ):
        raise ValueError(
            'usable-output profile equipment binding does not match the '
            'resolved EquipmentDefinition'
        )

    if profile.binding_class != 'exact':
        return (
            _unsupported(
                'usable-output profile binding is advisory (id-only) and '
                'cannot drive O100D headroom (#1026)',
                'dB',
            ),
            evidence,
        )

    if profile.measurement_distance_m is None:
        return (
            _missing(
                'usable-output profile records no measurement distance — '
                'no listener transfer can be established',
                'dB',
            ),
            evidence,
        )

    duration_class: Literal['continuous', 'burst'] = (
        'continuous' if kind == 'continuous' else 'burst'
    )
    declared: float | None = None
    capability = definition.spl_capability
    if capability is not None:
        declared = (
            capability.continuous_db_spl
            if kind == 'continuous'
            else capability.peak_db_spl
        )

    transfer = ListenerTransferAuthority(
        kind='propagation_model',
        propagation_model=DISTANCE_LEVEL_MODEL_ID,
        model_version=DISTANCE_LEVEL_MODEL_VERSION,
        reference_distance_m=profile.measurement_distance_m,
        listener_distance_m=distance_m,
        transfer_db=_distance_adjustment_db(
            profile.measurement_distance_m, distance_m
        ),
    )
    evaluation = evaluate_headroom(
        profile=profile,
        target_level_db_spl=scenario.target_spl_db_spl,
        frequency_band_hz=(
            scenario.frequency_band.low_hz,
            scenario.frequency_band.high_hz,
        ),
        duration_class=duration_class,
        declared_spl_db=declared,
        max_compression_db=scenario.max_compression_db,
        max_distortion_percent=scenario.max_distortion_percent,
        reference_transfer=transfer,
        require_exact_binding=True,
    )
    evidence = SeatHeadroomEvidence(
        profile_id=profile.profile_id,
        profile_sha256=profile.profile_sha256,
        binding=profile.binding_class,
        tier_used=evaluation.tier_used,
        basis=evaluation.basis,
    )
    if evaluation.headroom_db is not None:
        return _available(evaluation.headroom_db, 'dB'), evidence
    if evaluation.basis == 'unknown':
        return _missing(evaluation.status_reason, 'dB'), evidence
    return _unsupported(evaluation.status_reason, 'dB'), evidence


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


def _aggregate_weighted_mean(
    values: Sequence[tuple[float, DirectLevelScalarResult]],
    *,
    unit: Literal['dB', 'dB SPL'],
    label: str,
) -> DirectLevelScalarResult:
    """sum_to_one-weighted mean over required seats; never a probability mix."""
    if any(item.state == 'unsupported' for _w, item in values):
        return _unsupported(
            f'{label} unavailable because at least one required seat is '
            'unsupported',
            unit,
        )
    if any(item.state == 'missing' for _w, item in values):
        return _missing(
            f'{label} unavailable because at least one required seat is '
            'missing evidence',
            unit,
        )
    total = sum(float(weight) for weight, _item in values)
    if total <= 0.0:
        return _unsupported(
            f'{label} unavailable because required weights do not normalize',
            unit,
        )
    value = sum(
        float(weight) * float(item.value)
        for weight, item in values
        if item.value is not None
    ) / total
    return _available(value, unit)


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
    priority_profile: SeatPriorityProfile | None = None,
    usable_output_profile: SourceUsableOutputProfile | None = None,
    listener_pose_resolver: 'Callable[[str], ListenerPoseAuthority | None] | None' = (
        None
    ),
) -> DirectLevelEvaluation:
    """Evaluate one channel without room gain, reflections, directivity loss, or channel summation.

    ``usable_output_profile`` (#1047): when supplied it must bind the
    evaluated EquipmentDefinition by the exact id+version+sha256 triple
    (#1026); advisory id-only profiles cannot drive hard headroom and
    render headroom unsupported rather than falling back to declared
    figures. When omitted the legacy declared-capability path applies.
    """

    if (
        variant.document_id != revision.document_id
        or variant.baseline_revision_id != revision.revision_id
        or variant.baseline_content_hash != revision.content_hash
    ):
        raise ValueError('direct-level SystemVariant/SceneRevision authority mismatch')

    population = scenario.receiver_population
    if population.population_weighting == 'seat_priority':
        if priority_profile is None:
            raise ValueError(
                'seat_priority weighting requires the bound SeatPriorityProfile'
            )
        if (
            priority_profile.profile_id != population.priority_profile_id
            or priority_profile.profile_sha256
            != population.priority_profile_sha256
        ):
            raise ValueError(
                'seat priority profile does not match the exact scenario '
                'binding'
            )
        if priority_profile.seat_entity_ids != population.seat_entity_ids:
            raise ValueError(
                'seat priority profile members do not match the receiver '
                'population'
            )
    elif priority_profile is not None:
        raise ValueError(
            'priority profile requires seat_priority population weighting'
        )

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

    if usable_output_profile is not None and (
        usable_output_profile.equipment_definition_id
        != equipment_definition.definition_id
        or (
            usable_output_profile.equipment_definition_version is not None
            and usable_output_profile.equipment_definition_version
            != equipment_definition.version
        )
        or (
            usable_output_profile.equipment_definition_sha256 is not None
            and usable_output_profile.equipment_definition_sha256
            != equipment_definition.semantic_sha256
        )
    ):
        raise ValueError(
            'usable-output profile does not bind the evaluated '
            'EquipmentDefinition'
        )

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

        # #939: seat receivers resolve through the canonical listener-pose
        # resolver — a selected pose repositions the receiver; no pose keeps
        # the legacy seat offset as the explicit fallback.
        pose = (
            None
            if listener_pose_resolver is None
            else listener_pose_resolver(seat.entity_id)
        )
        resolved = resolve_listener_receiver(seat, pose)
        receiver = None if resolved is None else resolved.position
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

        # #964: the spherical-decay transfer is only claimed inside the
        # declared radial/far-field domain — a seat outside it gets an
        # explicit unsupported verdict, never an extrapolated false-precision
        # level.
        radial_domain = scenario.distance_authority.radial_domain
        if radial_domain is not None and not radial_domain.contains(distance_m):
            reason = (
                'seat distance lies outside the declared radial validity '
                'domain of the distance authority'
            )
            seat_results.append(
                SeatDirectLevelResult(
                    seat_entity_id=seat_id,
                    receiver_position_m=receiver,
                    distance_m=distance_m,
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
        continuous_result, continuous_evidence = _capability_headroom(
            equipment_definition,
            scenario,
            distance_m,
            kind='continuous',
            profile=usable_output_profile,
        )
        peak_result, peak_evidence = _capability_headroom(
            equipment_definition,
            scenario,
            distance_m,
            kind='peak',
            profile=usable_output_profile,
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
                continuous_headroom=continuous_result,
                peak_headroom=peak_result,
                continuous_headroom_evidence=continuous_evidence,
                peak_headroom_evidence=peak_evidence,
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
    priority_aggregates = _seat_priority_aggregates(
        seat_results,
        priority_profile,
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
        'seat_results': [_seat_result_payload(item) for item in seat_results],
        'aggregates': aggregates.model_dump(mode='json'),
    }
    if usable_output_profile is not None:
        identity['usable_output_profile'] = (
            usable_output_profile.model_dump(mode='json')
        )
    if priority_aggregates is not None:
        identity['priority_aggregates'] = priority_aggregates.model_dump(
            mode='json'
        )
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
        usable_output_profile=usable_output_profile,
        source_reference_position_m=source_reference,
        seat_results=tuple(seat_results),
        aggregates=aggregates,
        priority_aggregates=priority_aggregates,
        evaluation_id=_semantic_id('o100d', digest),
        evaluation_sha256=digest,
    )


def _seat_priority_aggregates(
    seat_results: Sequence[SeatDirectLevelResult],
    priority_profile: SeatPriorityProfile | None,
) -> SeatPriorityAggregates | None:
    """Weighted soft + required-floor aggregates over the exact profile (#513, #975).

    Soft population = non-diagnostic members; hard floor = required members.
    Diagnostics are evidence rows, not objective members. A missing seat in
    either population fails closed rather than silently dropping out.
    """
    if priority_profile is None:
        return None
    weights = priority_profile.normalized_weights()
    by_id = {item.seat_entity_id: item for item in seat_results}
    for seat_id in (
        set(weights) | set(priority_profile.required_seat_entity_ids)
    ):
        if seat_id not in by_id:
            raise ValueError(
                f'seat priority member {seat_id} is missing from the '
                'evaluated seat population'
            )
    soft = tuple(
        (weights[seat_id], by_id[seat_id])
        for seat_id in priority_profile.soft_objective_seat_entity_ids
    )
    hard = tuple(
        by_id[seat_id]
        for seat_id in priority_profile.required_seat_entity_ids
    )
    direct_values = [item.direct_level for _w, item in soft]
    target_values = [item.target_margin for _w, item in soft]
    continuous_values = [item.continuous_headroom for _w, item in soft]
    peak_values = [item.peak_headroom for _w, item in soft]
    required_direct_values = [item.direct_level for item in hard]
    required_target_values = [item.target_margin for item in hard]
    required_continuous_values = [item.continuous_headroom for item in hard]
    required_peak_values = [item.peak_headroom for item in hard]
    return SeatPriorityAggregates(
        priority_profile_id=priority_profile.profile_id,
        priority_profile_sha256=priority_profile.profile_sha256,
        weight_normalization=priority_profile.weight_normalization,
        normalization_version=priority_profile.normalization_version,
        required_seat_entity_ids=priority_profile.required_seat_entity_ids,
        normalized_weights=weights,
        weighted_direct_level=_aggregate_weighted_mean(
            [(w, item.direct_level) for w, item in soft],
            unit='dB SPL',
            label='weighted direct level',
        ),
        weighted_target_margin=_aggregate_weighted_mean(
            [(w, item.target_margin) for w, item in soft],
            unit='dB',
            label='weighted target margin',
        ),
        weighted_continuous_headroom=_aggregate_weighted_mean(
            [(w, item.continuous_headroom) for w, item in soft],
            unit='dB',
            label='weighted continuous headroom',
        ),
        weighted_peak_headroom=_aggregate_weighted_mean(
            [(w, item.peak_headroom) for w, item in soft],
            unit='dB',
            label='weighted peak headroom',
        ),
        worst_required_seat_direct_level=_aggregate_min(
            required_direct_values,
            unit='dB SPL',
            label='worst required-seat direct level',
        ),
        worst_required_seat_target_margin=_aggregate_min(
            required_target_values,
            unit='dB',
            label='worst required-seat target margin',
        ),
        worst_required_seat_continuous_headroom=_aggregate_min(
            required_continuous_values,
            unit='dB',
            label='worst required-seat continuous headroom',
        ),
        worst_required_seat_peak_headroom=_aggregate_min(
            required_peak_values,
            unit='dB',
            label='worst required-seat peak headroom',
        ),
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
    pairs = list(zip(definitions, results, strict=True))
    if evaluation.priority_aggregates is not None:
        priority = evaluation.priority_aggregates
        # Weighted soft aggregate and the independent required-seat floor are
        # separate objectives — a low secondary weight never waives the floor.
        for objective_id, quantity, unit, result in (
            (
                'o100d.priority.weighted_direct_level_db_spl',
                'direct_equipment_derived_weighted_seat_spl',
                'dB SPL',
                priority.weighted_direct_level,
            ),
            (
                'o100d.priority.weighted_target_margin_db',
                'direct_equipment_derived_weighted_target_margin',
                'dB',
                priority.weighted_target_margin,
            ),
            (
                'o100d.priority.weighted_continuous_headroom_db',
                'direct_equipment_derived_weighted_continuous_headroom',
                'dB',
                priority.weighted_continuous_headroom,
            ),
            (
                'o100d.priority.weighted_peak_headroom_db',
                'direct_equipment_derived_weighted_peak_headroom',
                'dB',
                priority.weighted_peak_headroom,
            ),
            (
                'o100d.priority.worst_required_seat_direct_level_db_spl',
                'direct_equipment_derived_worst_required_seat_spl',
                'dB SPL',
                priority.worst_required_seat_direct_level,
            ),
            (
                'o100d.priority.worst_required_seat_target_margin_db',
                'direct_equipment_derived_worst_required_seat_margin',
                'dB',
                priority.worst_required_seat_target_margin,
            ),
            (
                'o100d.priority.worst_required_seat_continuous_headroom_db',
                'direct_equipment_derived_worst_required_seat_continuous',
                'dB',
                priority.worst_required_seat_continuous_headroom,
            ),
            (
                'o100d.priority.worst_required_seat_peak_headroom_db',
                'direct_equipment_derived_worst_required_seat_peak',
                'dB',
                priority.worst_required_seat_peak_headroom,
            ),
        ):
            pairs.append(
                (
                    _objective_definition(
                        evaluation.scenario,
                        objective_id=objective_id,
                        quantity=quantity,
                        unit=unit,
                        direction='maximize',
                    ),
                    result,
                )
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
        for definition, result in pairs
    )
    return ObjectiveVector(
        candidate_id=evaluation.variant_id,
        metrics=metrics,
    )
