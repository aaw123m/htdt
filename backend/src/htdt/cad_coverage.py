from __future__ import annotations

from hashlib import sha256
import json
from math import atan2, degrees, hypot, isfinite, sqrt
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_direct_level import SeatPopulation
from .cad_directivity import (
    DirectivityDataset,
    evaluate_directivity,
    validate_directivity_dataset_binding,
)
from .cad_equipment import EquipmentDefinition
from .cad_repository import SceneRevision
from .cad_scene import (
    Direction3,
    Position3,
    Quaternion4,
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


COVERAGE_SCHEMA_VERSION = 1
COVERAGE_AUTHORITY_VERSION = 'o100d-coverage-directivity-1'
COVERAGE_ALGORITHM_ID = 'seat-source-relative-directivity'
COVERAGE_ALGORITHM_VERSION = '1'
SOURCE_ANGLE_CONVENTION_VERSION = 'explicit-aim-body-up-source-frame-1'
OFF_AXIS_LOSS_AUTHORITY_VERSION = (
    'reference-level-db-minus-evaluated-relative-level-db-1'
)
OBJECTIVE_COMPARISON_MODEL_ID = 'o100d-coverage-directivity-objective'

FrequencyAggregationSemantics = Literal[
    'worst_over_requested_frequencies',
    'mean_over_requested_frequencies',
]
CoverageCriterion = Literal[
    'aggregated_relative_directivity_level_gte_threshold_db'
]
MissingSeatPolicy = Literal['fail_closed_required_population']
CoverageSupportState = Literal['SUPPORTED', 'UNSUPPORTED']
DatasetAngleSemantics = Literal[
    'horizontal_vertical',
    'spherical_azimuth_elevation',
]


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


def _normalize(
    vector: tuple[float, float, float],
    *,
    field_name: str,
) -> tuple[float, float, float]:
    length = sqrt(sum(value * value for value in vector))
    if length <= 1e-12:
        raise ValueError(f'{field_name} is degenerate')
    normalized = tuple(value / length for value in vector)
    return (normalized[0], normalized[1], normalized[2])


def _dot(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _cross(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


class SourceAngleConvention(BaseModel):
    """Exact world-to-source angle convention.

    aim_xyz is the acoustic reference axis. Body orientation is not allowed
    to replace an unknown aim; it only supplies the local +Z roll/up reference.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    convention_version: Literal[
        'explicit-aim-body-up-source-frame-1'
    ] = SOURCE_ANGLE_CONVENTION_VERSION
    acoustic_axis_semantics: Literal[
        'scene_speaker_aim_xyz_world_direction_required'
    ] = 'scene_speaker_aim_xyz_world_direction_required'
    roll_reference_semantics: Literal[
        'body_local_plus_z_projected_orthogonal_to_aim'
    ] = 'body_local_plus_z_projected_orthogonal_to_aim'
    horizontal_positive: Literal['left'] = 'left'
    vertical_positive: Literal['up'] = 'up'
    dataset_angle_semantics: DatasetAngleSemantics
    dataset_reference_axis: Literal[
        'equipment_acoustic_reference_axis'
    ] = 'equipment_acoustic_reference_axis'


class OffAxisLossAuthority(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'reference-level-db-minus-evaluated-relative-level-db-1'
    ] = OFF_AXIS_LOSS_AUTHORITY_VERSION
    equation: Literal[
        'loss_db=reference_level_db-evaluated_relative_level_db'
    ] = 'loss_db=reference_level_db-evaluated_relative_level_db'
    reference_direction: Literal[
        'dataset_equipment_acoustic_reference_axis_at_same_frequency'
    ] = 'dataset_equipment_acoustic_reference_axis_at_same_frequency'
    clamp_negative_loss: Literal[False] = False


class CoverageEvaluationScenario(BaseModel):
    """Immutable single-source coverage/directivity evaluation contract."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = COVERAGE_SCHEMA_VERSION
    authority_version: Literal[
        'o100d-coverage-directivity-1'
    ] = COVERAGE_AUTHORITY_VERSION

    source_entity_id: str = Field(min_length=1)
    channel_role_id: str = Field(min_length=1)
    receiver_population: SeatPopulation

    directivity_dataset_id: str = Field(min_length=1)
    directivity_dataset_version: str = Field(min_length=1)
    directivity_dataset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    equipment_definition_id: str = Field(min_length=1)
    equipment_definition_version: str = Field(min_length=1)
    equipment_definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    evaluation_frequencies_hz: tuple[float, ...] = Field(min_length=1)
    frequency_aggregation_semantics: FrequencyAggregationSemantics
    coverage_criterion: CoverageCriterion = (
        'aggregated_relative_directivity_level_gte_threshold_db'
    )
    coverage_threshold_db: float
    seat_weighting_semantics: Literal[
        'equal_unweighted'
    ] = 'equal_unweighted'
    missing_unsupported_seat_policy: MissingSeatPolicy = (
        'fail_closed_required_population'
    )

    source_angle_convention: SourceAngleConvention
    off_axis_loss_authority: OffAxisLossAuthority = OffAxisLossAuthority()
    directivity_request: Literal['magnitude'] = 'magnitude'
    algorithm_id: Literal[
        'seat-source-relative-directivity'
    ] = COVERAGE_ALGORITHM_ID
    algorithm_version: Literal['1'] = COVERAGE_ALGORITHM_VERSION

    scenario_id: str = Field(min_length=1)
    scenario_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @field_validator('evaluation_frequencies_hz')
    @classmethod
    def valid_frequencies(
        cls,
        values: tuple[float, ...],
    ) -> tuple[float, ...]:
        result = tuple(
            _finite(value, field_name='evaluation frequency')
            for value in values
        )
        if any(value <= 0.0 for value in result):
            raise ValueError('evaluation frequencies must be positive')
        if tuple(sorted(result)) != result or len(set(result)) != len(result):
            raise ValueError(
                'evaluation frequencies must be strictly increasing and unique'
            )
        return result

    @field_validator('coverage_threshold_db')
    @classmethod
    def finite_threshold(cls, value: float) -> float:
        return _finite(value, field_name='coverage threshold')

    @model_validator(mode='after')
    def valid_identity(self) -> 'CoverageEvaluationScenario':
        if (
            self.receiver_population.population_weighting
            != self.seat_weighting_semantics
        ):
            raise ValueError(
                'coverage seat weighting must match exact receiver population'
            )
        digest = _digest(self.semantic_payload())
        if self.scenario_sha256 != digest:
            raise ValueError('coverage scenario semantic hash mismatch')
        if self.scenario_id != _semantic_id('coverage-scenario', digest):
            raise ValueError('coverage scenario ID does not match semantic hash')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'source_entity_id': self.source_entity_id,
            'channel_role_id': self.channel_role_id,
            'receiver_population': self.receiver_population.model_dump(mode='json'),
            'directivity_dataset_id': self.directivity_dataset_id,
            'directivity_dataset_version': self.directivity_dataset_version,
            'directivity_dataset_sha256': self.directivity_dataset_sha256,
            'equipment_definition_id': self.equipment_definition_id,
            'equipment_definition_version': self.equipment_definition_version,
            'equipment_definition_sha256': self.equipment_definition_sha256,
            'evaluation_frequencies_hz': list(self.evaluation_frequencies_hz),
            'frequency_aggregation_semantics': (
                self.frequency_aggregation_semantics
            ),
            'coverage_criterion': self.coverage_criterion,
            'coverage_threshold_db': self.coverage_threshold_db,
            'seat_weighting_semantics': self.seat_weighting_semantics,
            'missing_unsupported_seat_policy': (
                self.missing_unsupported_seat_policy
            ),
            'source_angle_convention': self.source_angle_convention.model_dump(
                mode='json'
            ),
            'off_axis_loss_authority': self.off_axis_loss_authority.model_dump(
                mode='json'
            ),
            'directivity_request': self.directivity_request,
            'algorithm_id': self.algorithm_id,
            'algorithm_version': self.algorithm_version,
        }


def build_coverage_evaluation_scenario(
    *,
    source_entity_id: str,
    channel_role_id: str,
    receiver_population: SeatPopulation,
    directivity_dataset: DirectivityDataset,
    equipment_definition: EquipmentDefinition,
    evaluation_frequencies_hz: Sequence[float],
    frequency_aggregation_semantics: FrequencyAggregationSemantics,
    coverage_threshold_db: float,
) -> CoverageEvaluationScenario:
    validate_directivity_dataset_binding(
        directivity_dataset,
        equipment_definition,
    )
    frequencies = tuple(
        _finite(value, field_name='evaluation frequency')
        for value in evaluation_frequencies_hz
    )
    convention = SourceAngleConvention(
        dataset_angle_semantics=(
            directivity_dataset.coordinate_convention.angle_semantics
        ),
    )
    payload: dict[str, Any] = {
        'schema_version': COVERAGE_SCHEMA_VERSION,
        'authority_version': COVERAGE_AUTHORITY_VERSION,
        'source_entity_id': source_entity_id,
        'channel_role_id': channel_role_id,
        'receiver_population': receiver_population.model_dump(mode='json'),
        'directivity_dataset_id': directivity_dataset.dataset_id,
        'directivity_dataset_version': directivity_dataset.version,
        'directivity_dataset_sha256': directivity_dataset.semantic_sha256,
        'equipment_definition_id': equipment_definition.definition_id,
        'equipment_definition_version': equipment_definition.version,
        'equipment_definition_sha256': equipment_definition.semantic_sha256,
        'evaluation_frequencies_hz': list(frequencies),
        'frequency_aggregation_semantics': frequency_aggregation_semantics,
        'coverage_criterion': (
            'aggregated_relative_directivity_level_gte_threshold_db'
        ),
        'coverage_threshold_db': float(coverage_threshold_db),
        'seat_weighting_semantics': 'equal_unweighted',
        'missing_unsupported_seat_policy': 'fail_closed_required_population',
        'source_angle_convention': convention.model_dump(mode='json'),
        'off_axis_loss_authority': OffAxisLossAuthority().model_dump(
            mode='json'
        ),
        'directivity_request': 'magnitude',
        'algorithm_id': COVERAGE_ALGORITHM_ID,
        'algorithm_version': COVERAGE_ALGORITHM_VERSION,
    }
    digest = _digest(payload)
    return CoverageEvaluationScenario(
        **payload,
        scenario_id=_semantic_id('coverage-scenario', digest),
        scenario_sha256=digest,
    )


class SourceRelativeAngles(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    horizontal_angle_deg: float
    vertical_angle_deg: float
    angle_semantics: DatasetAngleSemantics
    convention_version: Literal[
        'explicit-aim-body-up-source-frame-1'
    ] = SOURCE_ANGLE_CONVENTION_VERSION

    @field_validator('horizontal_angle_deg', 'vertical_angle_deg')
    @classmethod
    def finite_angles(cls, value: float) -> float:
        return _finite(value, field_name='source-relative angle')


class SeatFrequencyDirectivityResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    requested_frequency_hz: float = Field(gt=0.0)
    source_relative_angles: SourceRelativeAngles | None = None
    directivity_evaluation_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    reference_evaluation_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    relative_level_db: float | None = None
    reference_level_db: float | None = None
    off_axis_loss_db: float | None = None
    support_state: CoverageSupportState
    reason: str | None = None

    @field_validator(
        'requested_frequency_hz',
        'relative_level_db',
        'reference_level_db',
        'off_axis_loss_db',
    )
    @classmethod
    def finite_values(
        cls,
        value: float | None,
    ) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='per-frequency coverage value')

    @model_validator(mode='after')
    def valid_support_state(self) -> 'SeatFrequencyDirectivityResult':
        if self.support_state == 'SUPPORTED':
            if self.source_relative_angles is None:
                raise ValueError('supported frequency result requires source angles')
            if (
                self.directivity_evaluation_sha256 is None
                or self.reference_evaluation_sha256 is None
            ):
                raise ValueError(
                    'supported frequency result requires exact directivity evaluation hashes'
                )
            if any(
                value is None
                for value in (
                    self.relative_level_db,
                    self.reference_level_db,
                    self.off_axis_loss_db,
                )
            ):
                raise ValueError(
                    'supported frequency result requires level and off-axis loss'
                )
            if self.reason is not None:
                raise ValueError(
                    'supported frequency result must not carry a failure reason'
                )
        else:
            if any(
                value is not None
                for value in (
                    self.relative_level_db,
                    self.reference_level_db,
                    self.off_axis_loss_db,
                )
            ):
                raise ValueError(
                    'unsupported frequency result must not carry fabricated values'
                )
            if self.reason is None:
                raise ValueError(
                    'unsupported frequency result requires explicit reason'
                )
        return self


class CoverageScalarResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    state: ObjectiveState
    value: float | None
    unit: Literal['dB', 'ratio']
    reason: str | None = None

    @model_validator(mode='after')
    def valid_state(self) -> 'CoverageScalarResult':
        if self.state == 'available':
            if self.value is None or not isfinite(float(self.value)):
                raise ValueError('available coverage scalar requires finite value')
            if self.reason is not None:
                raise ValueError(
                    'available coverage scalar must not carry failure reason'
                )
        else:
            if self.value is not None:
                raise ValueError(
                    'unavailable coverage scalar must not carry numeric value'
                )
            if self.reason is None:
                raise ValueError(
                    'unavailable coverage scalar requires explicit reason'
                )
        return self


class SeatCoverageResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    seat_entity_id: str = Field(min_length=1)
    receiver_reference_position_m: Position3 | None = None
    frequency_results: tuple[SeatFrequencyDirectivityResult, ...] = Field(
        min_length=1
    )
    aggregated_relative_directivity_level: CoverageScalarResult
    aggregated_off_axis_loss: CoverageScalarResult
    coverage_pass: bool | None = None
    state: ObjectiveState
    reason: str | None = None

    @model_validator(mode='after')
    def valid_state(self) -> 'SeatCoverageResult':
        if self.state == 'available':
            if self.coverage_pass is None:
                raise ValueError('available seat coverage requires pass/fail')
            if (
                self.aggregated_relative_directivity_level.state != 'available'
                or self.aggregated_off_axis_loss.state != 'available'
            ):
                raise ValueError(
                    'available seat coverage requires available aggregate values'
                )
            if any(
                item.support_state != 'SUPPORTED'
                for item in self.frequency_results
            ):
                raise ValueError(
                    'available seat coverage requires all requested frequencies'
                )
            if self.reason is not None:
                raise ValueError(
                    'available seat coverage must not carry failure reason'
                )
        else:
            if self.coverage_pass is not None:
                raise ValueError(
                    'unavailable seat coverage must not carry pass/fail'
                )
            if self.reason is None:
                raise ValueError(
                    'unavailable seat coverage requires explicit reason'
                )
        return self


class CoverageAggregates(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    useful_coverage_fraction: CoverageScalarResult
    worst_seat_relative_directivity_level: CoverageScalarResult
    worst_seat_off_axis_loss: CoverageScalarResult
    seat_to_seat_directivity_spread: CoverageScalarResult


class CoverageEvaluation(BaseModel):
    """Self-contained immutable O100D coverage/directivity evidence."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = COVERAGE_SCHEMA_VERSION
    authority_version: Literal[
        'o100d-coverage-directivity-1'
    ] = COVERAGE_AUTHORITY_VERSION

    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    variant_id: str = Field(min_length=1)
    variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    equipment_definition_id: str = Field(min_length=1)
    equipment_definition_version: str = Field(min_length=1)
    equipment_definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    directivity_dataset_id: str = Field(min_length=1)
    directivity_dataset_version: str = Field(min_length=1)
    directivity_dataset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    scenario: CoverageEvaluationScenario
    source_reference_position_m: Position3
    source_body_orientation: Quaternion4
    source_acoustic_axis: Direction3 | None = None
    seat_results: tuple[SeatCoverageResult, ...] = Field(min_length=1)
    aggregates: CoverageAggregates

    evaluation_id: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_identity(self) -> 'CoverageEvaluation':
        expected_seats = self.scenario.receiver_population.seat_entity_ids
        actual_seats = tuple(item.seat_entity_id for item in self.seat_results)
        if actual_seats != expected_seats:
            raise ValueError(
                'coverage seat results do not match exact receiver population'
            )
        if (
            self.scenario.equipment_definition_id
            != self.equipment_definition_id
            or self.scenario.equipment_definition_version
            != self.equipment_definition_version
            or self.scenario.equipment_definition_sha256
            != self.equipment_definition_sha256
        ):
            raise ValueError(
                'coverage evaluation EquipmentDefinition does not match scenario'
            )
        if (
            self.scenario.directivity_dataset_id
            != self.directivity_dataset_id
            or self.scenario.directivity_dataset_version
            != self.directivity_dataset_version
            or self.scenario.directivity_dataset_sha256
            != self.directivity_dataset_sha256
        ):
            raise ValueError(
                'coverage evaluation DirectivityDataset does not match scenario'
            )
        digest = _digest(self.identity_payload())
        if self.evaluation_sha256 != digest:
            raise ValueError('coverage evaluation semantic hash mismatch')
        if self.evaluation_id != _semantic_id('coverage', digest):
            raise ValueError('coverage evaluation ID does not match semantic hash')
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
            'directivity_dataset_id': self.directivity_dataset_id,
            'directivity_dataset_version': self.directivity_dataset_version,
            'directivity_dataset_sha256': self.directivity_dataset_sha256,
            'scenario': self.scenario.model_dump(mode='json'),
            'source_reference_position_m': self.source_reference_position_m.model_dump(
                mode='json'
            ),
            'source_body_orientation': self.source_body_orientation.model_dump(
                mode='json'
            ),
            'source_acoustic_axis': (
                None
                if self.source_acoustic_axis is None
                else self.source_acoustic_axis.model_dump(mode='json')
            ),
            'seat_results': [
                item.model_dump(mode='json') for item in self.seat_results
            ],
            'aggregates': self.aggregates.model_dump(mode='json'),
        }


def _available(value: float, unit: Literal['dB', 'ratio']) -> CoverageScalarResult:
    return CoverageScalarResult(
        state='available',
        value=float(value),
        unit=unit,
    )


def _unsupported(
    reason: str,
    unit: Literal['dB', 'ratio'],
) -> CoverageScalarResult:
    return CoverageScalarResult(
        state='unsupported',
        value=None,
        unit=unit,
        reason=reason,
    )


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


def _source_frame(
    source: SceneEntity,
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]:
    if source.aim_xyz is None:
        raise ValueError(
            'source speaker has no explicit acoustic aim_xyz; body orientation '
            'must not be guessed as acoustic reference axis'
        )
    forward = (
        float(source.aim_xyz.x),
        float(source.aim_xyz.y),
        float(source.aim_xyz.z),
    )
    forward = _normalize(forward, field_name='source acoustic aim')

    matrix = quaternion_to_matrix3(source.orientation)
    body_up = (matrix[0][2], matrix[1][2], matrix[2][2])
    projection = _dot(body_up, forward)
    up = (
        body_up[0] - projection * forward[0],
        body_up[1] - projection * forward[1],
        body_up[2] - projection * forward[2],
    )
    up = _normalize(
        up,
        field_name='body +Z roll reference projected orthogonal to source aim',
    )
    right = _normalize(
        _cross(forward, up),
        field_name='source right axis',
    )
    left = (-right[0], -right[1], -right[2])
    return forward, left, up


def _source_relative_angles(
    *,
    source_reference: Position3,
    receiver_reference: Position3,
    source_frame: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ],
    semantics: DatasetAngleSemantics,
) -> SourceRelativeAngles:
    forward, left, up = source_frame
    direction = _normalize(
        (
            receiver_reference.x_m - source_reference.x_m,
            receiver_reference.y_m - source_reference.y_m,
            receiver_reference.z_m - source_reference.z_m,
        ),
        field_name='source-to-receiver direction',
    )
    forward_component = _dot(direction, forward)
    left_component = _dot(direction, left)
    up_component = _dot(direction, up)

    horizontal = degrees(atan2(left_component, forward_component))
    if semantics == 'horizontal_vertical':
        vertical = degrees(atan2(up_component, forward_component))
    else:
        vertical = degrees(
            atan2(
                up_component,
                hypot(forward_component, left_component),
            )
        )
    return SourceRelativeAngles(
        horizontal_angle_deg=horizontal,
        vertical_angle_deg=vertical,
        angle_semantics=semantics,
    )


def _unsupported_frequency(
    frequency_hz: float,
    reason: str,
    *,
    angles: SourceRelativeAngles | None = None,
    directivity_evaluation_sha256: str | None = None,
    reference_evaluation_sha256: str | None = None,
) -> SeatFrequencyDirectivityResult:
    return SeatFrequencyDirectivityResult(
        requested_frequency_hz=frequency_hz,
        source_relative_angles=angles,
        directivity_evaluation_sha256=directivity_evaluation_sha256,
        reference_evaluation_sha256=reference_evaluation_sha256,
        relative_level_db=None,
        reference_level_db=None,
        off_axis_loss_db=None,
        support_state='UNSUPPORTED',
        reason=reason,
    )


def _evaluate_frequency(
    *,
    dataset: DirectivityDataset,
    frequency_hz: float,
    angles: SourceRelativeAngles,
) -> SeatFrequencyDirectivityResult:
    evaluated = evaluate_directivity(
        dataset,
        frequency_hz=frequency_hz,
        horizontal_angle_deg=angles.horizontal_angle_deg,
        vertical_angle_deg=angles.vertical_angle_deg,
        request='magnitude',
    )
    if evaluated.decision != 'SUPPORTED':
        return _unsupported_frequency(
            frequency_hz,
            '; '.join(evaluated.reasons),
            angles=angles,
            directivity_evaluation_sha256=evaluated.semantic_sha256,
        )

    reference = evaluate_directivity(
        dataset,
        frequency_hz=frequency_hz,
        horizontal_angle_deg=0.0,
        vertical_angle_deg=0.0,
        request='magnitude',
    )
    if reference.decision != 'SUPPORTED':
        return _unsupported_frequency(
            frequency_hz,
            'on-axis reference is unsupported: ' + '; '.join(reference.reasons),
            angles=angles,
            directivity_evaluation_sha256=evaluated.semantic_sha256,
            reference_evaluation_sha256=reference.semantic_sha256,
        )

    assert evaluated.magnitude_db is not None
    assert reference.magnitude_db is not None
    relative_level_db = float(evaluated.magnitude_db)
    reference_level_db = float(reference.magnitude_db)
    return SeatFrequencyDirectivityResult(
        requested_frequency_hz=frequency_hz,
        source_relative_angles=angles,
        directivity_evaluation_sha256=evaluated.semantic_sha256,
        reference_evaluation_sha256=reference.semantic_sha256,
        relative_level_db=relative_level_db,
        reference_level_db=reference_level_db,
        off_axis_loss_db=reference_level_db - relative_level_db,
        support_state='SUPPORTED',
    )


def _aggregate_frequency_values(
    values: Sequence[SeatFrequencyDirectivityResult],
    *,
    semantics: FrequencyAggregationSemantics,
) -> tuple[float, float]:
    levels = [
        float(item.relative_level_db)
        for item in values
        if item.relative_level_db is not None
    ]
    losses = [
        float(item.off_axis_loss_db)
        for item in values
        if item.off_axis_loss_db is not None
    ]
    if len(levels) != len(values) or len(losses) != len(values):
        raise ValueError('frequency aggregation requires fully supported values')
    if semantics == 'worst_over_requested_frequencies':
        return min(levels), max(losses)
    count = float(len(values))
    return sum(levels) / count, sum(losses) / count


def _unsupported_seat(
    *,
    seat_id: str,
    scenario: CoverageEvaluationScenario,
    reason: str,
    receiver_reference: Position3 | None = None,
) -> SeatCoverageResult:
    frequency_results = tuple(
        _unsupported_frequency(
            frequency,
            reason,
        )
        for frequency in scenario.evaluation_frequencies_hz
    )
    return SeatCoverageResult(
        seat_entity_id=seat_id,
        receiver_reference_position_m=receiver_reference,
        frequency_results=frequency_results,
        aggregated_relative_directivity_level=_unsupported(reason, 'dB'),
        aggregated_off_axis_loss=_unsupported(reason, 'dB'),
        coverage_pass=None,
        state='unsupported',
        reason=reason,
    )


def _population_aggregates(
    seat_results: Sequence[SeatCoverageResult],
) -> CoverageAggregates:
    unavailable = [
        item
        for item in seat_results
        if item.state != 'available'
    ]
    if unavailable:
        reason = (
            'coverage aggregate unavailable because at least one required seat '
            'is unsupported; partial population evaluation is forbidden'
        )
        return CoverageAggregates(
            useful_coverage_fraction=_unsupported(reason, 'ratio'),
            worst_seat_relative_directivity_level=_unsupported(reason, 'dB'),
            worst_seat_off_axis_loss=_unsupported(reason, 'dB'),
            seat_to_seat_directivity_spread=_unsupported(reason, 'dB'),
        )

    levels = [
        float(item.aggregated_relative_directivity_level.value)
        for item in seat_results
        if item.aggregated_relative_directivity_level.value is not None
    ]
    losses = [
        float(item.aggregated_off_axis_loss.value)
        for item in seat_results
        if item.aggregated_off_axis_loss.value is not None
    ]
    passes = sum(1 for item in seat_results if item.coverage_pass)
    fraction = passes / len(seat_results)
    spread = (
        _available(max(levels) - min(levels), 'dB')
        if len(levels) >= 2
        else _unsupported(
            'seat-to-seat directivity spread requires at least two seats',
            'dB',
        )
    )
    return CoverageAggregates(
        useful_coverage_fraction=_available(fraction, 'ratio'),
        worst_seat_relative_directivity_level=_available(
            min(levels),
            'dB',
        ),
        worst_seat_off_axis_loss=_available(max(losses), 'dB'),
        seat_to_seat_directivity_spread=spread,
    )


def evaluate_coverage(
    *,
    revision: SceneRevision,
    variant: SystemVariant,
    equipment_definition: EquipmentDefinition,
    directivity_dataset: DirectivityDataset,
    scenario: CoverageEvaluationScenario,
) -> CoverageEvaluation:
    """Evaluate exact single-source directivity coverage with no SPL/room coupling."""

    if (
        variant.document_id != revision.document_id
        or variant.baseline_revision_id != revision.revision_id
        or variant.baseline_content_hash != revision.content_hash
    ):
        raise ValueError('coverage SystemVariant/SceneRevision authority mismatch')

    if (
        scenario.equipment_definition_id != equipment_definition.definition_id
        or scenario.equipment_definition_version != equipment_definition.version
        or scenario.equipment_definition_sha256
        != equipment_definition.semantic_sha256
    ):
        raise ValueError('coverage EquipmentDefinition scenario mismatch')
    if (
        scenario.directivity_dataset_id != directivity_dataset.dataset_id
        or scenario.directivity_dataset_version != directivity_dataset.version
        or scenario.directivity_dataset_sha256
        != directivity_dataset.semantic_sha256
    ):
        raise ValueError('coverage DirectivityDataset scenario mismatch')
    if (
        scenario.source_angle_convention.dataset_angle_semantics
        != directivity_dataset.coordinate_convention.angle_semantics
        or scenario.source_angle_convention.dataset_reference_axis
        != directivity_dataset.coordinate_convention.reference_axis
    ):
        raise ValueError('coverage source-angle/dataset coordinate authority mismatch')

    validate_directivity_dataset_binding(
        directivity_dataset,
        equipment_definition,
    )

    bindings = [
        item
        for item in variant.equipment_bindings
        if item.entity_id == scenario.source_entity_id
    ]
    if len(bindings) != 1:
        raise ValueError(
            'coverage scenario requires exactly one equipment binding for source entity'
        )
    binding = bindings[0]
    if (
        binding.equipment_definition_id != equipment_definition.definition_id
        or binding.equipment_definition_version != equipment_definition.version
        or binding.equipment_definition_sha256
        != equipment_definition.semantic_sha256
    ):
        raise ValueError('coverage EquipmentDefinition binding mismatch')

    scene = materialize_system_variant(revision, variant)
    try:
        source = scene.entity(scenario.source_entity_id)
    except KeyError as exc:
        raise ValueError(
            'coverage source entity is missing from SystemVariant'
        ) from exc
    if source.kind != 'speaker':
        raise ValueError('coverage source entity must be a speaker')
    if source.speaker_role != scenario.channel_role_id:
        raise ValueError(
            'coverage channel role does not match source speaker role'
        )

    source_reference = _source_reference_position(
        source,
        equipment_definition,
    )
    frame = None
    frame_reason: str | None = None
    try:
        frame = _source_frame(source)
    except ValueError as exc:
        frame_reason = str(exc)

    seat_results: list[SeatCoverageResult] = []
    for seat_id in scenario.receiver_population.seat_entity_ids:
        try:
            seat = scene.entity(seat_id)
        except KeyError:
            seat_results.append(
                _unsupported_seat(
                    seat_id=seat_id,
                    scenario=scenario,
                    reason='required receiver seat entity is missing from SystemVariant scene',
                )
            )
            continue

        if seat.kind != 'seat':
            seat_results.append(
                _unsupported_seat(
                    seat_id=seat_id,
                    scenario=scenario,
                    reason='receiver population member is not a seat entity',
                )
            )
            continue

        receiver = acoustic_reference_position(seat)
        if receiver is None:
            seat_results.append(
                _unsupported_seat(
                    seat_id=seat_id,
                    scenario=scenario,
                    reason='seat has no explicit scene acoustic reference position',
                )
            )
            continue

        if frame is None:
            seat_results.append(
                _unsupported_seat(
                    seat_id=seat_id,
                    scenario=scenario,
                    reason=frame_reason or 'source angle semantics are unsupported',
                    receiver_reference=receiver,
                )
            )
            continue

        try:
            angles = _source_relative_angles(
                source_reference=source_reference,
                receiver_reference=receiver,
                source_frame=frame,
                semantics=scenario.source_angle_convention.dataset_angle_semantics,
            )
        except ValueError as exc:
            seat_results.append(
                _unsupported_seat(
                    seat_id=seat_id,
                    scenario=scenario,
                    reason=str(exc),
                    receiver_reference=receiver,
                )
            )
            continue

        frequency_results = tuple(
            _evaluate_frequency(
                dataset=directivity_dataset,
                frequency_hz=frequency,
                angles=angles,
            )
            for frequency in scenario.evaluation_frequencies_hz
        )
        unsupported = [
            item for item in frequency_results
            if item.support_state != 'SUPPORTED'
        ]
        if unsupported:
            reason = (
                'seat coverage unavailable because at least one requested '
                'frequency is unsupported: '
                + ' | '.join(
                    item.reason or 'unsupported directivity evaluation'
                    for item in unsupported
                )
            )
            seat_results.append(
                SeatCoverageResult(
                    seat_entity_id=seat_id,
                    receiver_reference_position_m=receiver,
                    frequency_results=frequency_results,
                    aggregated_relative_directivity_level=_unsupported(
                        reason,
                        'dB',
                    ),
                    aggregated_off_axis_loss=_unsupported(reason, 'dB'),
                    coverage_pass=None,
                    state='unsupported',
                    reason=reason,
                )
            )
            continue

        aggregate_level, aggregate_loss = _aggregate_frequency_values(
            frequency_results,
            semantics=scenario.frequency_aggregation_semantics,
        )
        seat_results.append(
            SeatCoverageResult(
                seat_entity_id=seat_id,
                receiver_reference_position_m=receiver,
                frequency_results=frequency_results,
                aggregated_relative_directivity_level=_available(
                    aggregate_level,
                    'dB',
                ),
                aggregated_off_axis_loss=_available(
                    aggregate_loss,
                    'dB',
                ),
                coverage_pass=(
                    aggregate_level >= scenario.coverage_threshold_db
                ),
                state='available',
            )
        )

    aggregates = _population_aggregates(seat_results)
    identity: dict[str, Any] = {
        'schema_version': COVERAGE_SCHEMA_VERSION,
        'authority_version': COVERAGE_AUTHORITY_VERSION,
        'document_id': revision.document_id,
        'scene_revision_id': revision.revision_id,
        'scene_content_hash': revision.content_hash,
        'variant_id': variant.variant_id,
        'variant_sha256': variant.variant_sha256,
        'equipment_definition_id': equipment_definition.definition_id,
        'equipment_definition_version': equipment_definition.version,
        'equipment_definition_sha256': equipment_definition.semantic_sha256,
        'directivity_dataset_id': directivity_dataset.dataset_id,
        'directivity_dataset_version': directivity_dataset.version,
        'directivity_dataset_sha256': directivity_dataset.semantic_sha256,
        'scenario': scenario.model_dump(mode='json'),
        'source_reference_position_m': source_reference.model_dump(mode='json'),
        'source_body_orientation': source.orientation.model_dump(mode='json'),
        'source_acoustic_axis': (
            None
            if source.aim_xyz is None
            else source.aim_xyz.model_dump(mode='json')
        ),
        'seat_results': [
            item.model_dump(mode='json') for item in seat_results
        ],
        'aggregates': aggregates.model_dump(mode='json'),
    }
    digest = _digest(identity)
    return CoverageEvaluation(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        variant_id=variant.variant_id,
        variant_sha256=variant.variant_sha256,
        equipment_definition_id=equipment_definition.definition_id,
        equipment_definition_version=equipment_definition.version,
        equipment_definition_sha256=equipment_definition.semantic_sha256,
        directivity_dataset_id=directivity_dataset.dataset_id,
        directivity_dataset_version=directivity_dataset.version,
        directivity_dataset_sha256=directivity_dataset.semantic_sha256,
        scenario=scenario,
        source_reference_position_m=source_reference,
        source_body_orientation=source.orientation,
        source_acoustic_axis=source.aim_xyz,
        seat_results=tuple(seat_results),
        aggregates=aggregates,
        evaluation_id=_semantic_id('coverage', digest),
        evaluation_sha256=digest,
    )


def _objective_definition(
    scenario: CoverageEvaluationScenario,
    *,
    objective_id: str,
    quantity: str,
    unit: Literal['dB', 'ratio'],
    direction: Literal['minimize', 'maximize'],
    valid_domain: ObjectiveValidDomain,
) -> ObjectiveDefinition:
    return ObjectiveDefinition(
        objective_id=objective_id,
        quantity=quantity,
        unit=unit,
        direction=direction,
        valid_domain=valid_domain,
        comparison_model_id=OBJECTIVE_COMPARISON_MODEL_ID,
        comparison_model_version=scenario.scenario_sha256,
    )


def coverage_objective_definitions(
    scenario: CoverageEvaluationScenario,
) -> tuple[ObjectiveDefinition, ...]:
    return (
        _objective_definition(
            scenario,
            objective_id='o100d.coverage.useful_fraction',
            quantity='useful_coverage_fraction',
            unit='ratio',
            direction='maximize',
            valid_domain=ObjectiveValidDomain(
                kind='bounded_real',
                minimum=0.0,
                maximum=1.0,
            ),
        ),
        _objective_definition(
            scenario,
            objective_id='o100d.directivity.worst_seat_relative_level_db',
            quantity='worst_seat_relative_directivity_level',
            unit='dB',
            direction='maximize',
            valid_domain=ObjectiveValidDomain(kind='finite_real'),
        ),
        _objective_definition(
            scenario,
            objective_id='o100d.directivity.worst_seat_off_axis_loss_db',
            quantity='worst_seat_off_axis_loss',
            unit='dB',
            direction='minimize',
            valid_domain=ObjectiveValidDomain(kind='finite_real'),
        ),
        _objective_definition(
            scenario,
            objective_id='o100d.directivity.seat_to_seat_spread_db',
            quantity='seat_to_seat_directivity_spread',
            unit='dB',
            direction='minimize',
            valid_domain=ObjectiveValidDomain(
                kind='bounded_real',
                minimum=0.0,
            ),
        ),
    )


def coverage_objective_vector(
    evaluation: CoverageEvaluation,
) -> ObjectiveVector:
    definitions = coverage_objective_definitions(evaluation.scenario)
    results = (
        evaluation.aggregates.useful_coverage_fraction,
        evaluation.aggregates.worst_seat_relative_directivity_level,
        evaluation.aggregates.worst_seat_off_axis_loss,
        evaluation.aggregates.seat_to_seat_directivity_spread,
    )
    return ObjectiveVector(
        candidate_id=evaluation.variant_id,
        metrics=tuple(
            ObjectiveMetric(
                objective_id=definition.objective_id,
                value=result.value,
                unit=definition.unit,
                direction=definition.direction,
                state=result.state,
                definition=definition,
            )
            for definition, result in zip(
                definitions,
                results,
                strict=True,
            )
        ),
    )
