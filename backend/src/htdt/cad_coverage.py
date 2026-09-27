from __future__ import annotations

from math import atan2, degrees, hypot, isfinite, sqrt
from typing import TYPE_CHECKING, Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest

if TYPE_CHECKING:
    from collections.abc import Callable

    from .cad_listener_pose import ListenerPoseAuthority

from .cad_direct_level import SeatPopulation
from .cad_listener_pose import resolve_listener_receiver
from .cad_seat_priority import SeatPriorityProfile
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
        'equal_unweighted',
        'seat_priority',
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
        # The scenario inherits the exact population weighting; the model
        # validator still fails closed when they diverge.
        'seat_weighting_semantics': receiver_population.population_weighting,
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


class SeatPriorityCoverageAggregates(BaseModel):
    """Priority-aware coverage aggregates over the exact profile (#513).

    ``weighted_*`` metrics cover the soft-objective population
    (non-diagnostic members, independent of ``required``);
    ``worst_required_*`` is the hard floor over explicitly required seats.
    Soft and hard membership are separate (#975); diagnostics never enter
    either aggregate.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    priority_profile_id: str = Field(min_length=1)
    priority_profile_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    weight_normalization: str = Field(min_length=1)
    normalization_version: str = Field(min_length=1)
    required_seat_entity_ids: tuple[str, ...] = Field(min_length=1)
    normalized_weights: dict[str, float] = Field(min_length=1)
    weighted_useful_coverage_fraction: CoverageScalarResult
    weighted_relative_directivity_level: CoverageScalarResult
    worst_required_seat_relative_directivity_level: CoverageScalarResult
    worst_required_seat_off_axis_loss: CoverageScalarResult


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
    # Optional priority extension (#513): identity-stable only when present,
    # so equal-unweighted evaluations keep byte-exact identity.
    priority_aggregates: SeatPriorityCoverageAggregates | None = None

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
        if self.priority_aggregates is not None:
            payload['priority_aggregates'] = self.priority_aggregates.model_dump(
                mode='json'
            )
        return payload


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
    priority_profile: SeatPriorityProfile | None = None,
    listener_pose_resolver: 'Callable[[str], ListenerPoseAuthority | None] | None' = (
        None
    ),
) -> CoverageEvaluation:
    """Evaluate exact single-source directivity coverage with no SPL/room coupling."""

    if (
        variant.document_id != revision.document_id
        or variant.baseline_revision_id != revision.revision_id
        or variant.baseline_content_hash != revision.content_hash
    ):
        raise ValueError('coverage SystemVariant/SceneRevision authority mismatch')

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
    priority_aggregates = _seat_priority_aggregates(
        seat_results,
        priority_profile,
    )
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
    if priority_aggregates is not None:
        identity['priority_aggregates'] = priority_aggregates.model_dump(
            mode='json'
        )
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
        priority_aggregates=priority_aggregates,
        evaluation_id=_semantic_id('coverage', digest),
        evaluation_sha256=digest,
    )


def _seat_priority_aggregates(
    seat_results: Sequence[SeatCoverageResult],
    priority_profile: SeatPriorityProfile | None,
) -> SeatPriorityCoverageAggregates | None:
    """Weighted soft + required-floor coverage over the exact profile (#975).

    Soft population = non-diagnostic members; hard floor = required members.
    Each aggregate evaluates its own population; a missing seat fails closed
    rather than silently dropping out.
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

    soft_unavailable = any(item.state != 'available' for _w, item in soft)
    if soft_unavailable:
        soft_reason = (
            'weighted priority coverage aggregate unavailable because at '
            'least one soft-objective seat is unsupported; partial '
            'population evaluation is forbidden'
        )
        weighted_fraction = _unsupported(soft_reason, 'ratio')
        weighted_level = _unsupported(soft_reason, 'dB')
    else:
        weighted_fraction = _available(
            sum(
                float(weight)
                for weight, item in soft
                if item.coverage_pass is True
            ),
            'ratio',
        )
        weighted_level = _available(
            sum(
                float(weight)
                * float(item.aggregated_relative_directivity_level.value)
                for weight, item in soft
            ),
            'dB',
        )

    hard_unavailable = any(item.state != 'available' for item in hard)
    if hard_unavailable:
        hard_reason = (
            'required-seat coverage floor unavailable because at least one '
            'required seat is unsupported; partial population evaluation is '
            'forbidden'
        )
        worst_level = _unsupported(hard_reason, 'dB')
        worst_loss = _unsupported(hard_reason, 'dB')
    else:
        levels = [
            float(item.aggregated_relative_directivity_level.value)
            for item in hard
            if item.aggregated_relative_directivity_level.value is not None
        ]
        losses = [
            float(item.aggregated_off_axis_loss.value)
            for item in hard
            if item.aggregated_off_axis_loss.value is not None
        ]
        worst_level = _available(min(levels), 'dB')
        worst_loss = _available(max(losses), 'dB')

    return SeatPriorityCoverageAggregates(
        priority_profile_id=priority_profile.profile_id,
        priority_profile_sha256=priority_profile.profile_sha256,
        weight_normalization=priority_profile.weight_normalization,
        normalization_version=priority_profile.normalization_version,
        required_seat_entity_ids=priority_profile.required_seat_entity_ids,
        normalized_weights=weights,
        weighted_useful_coverage_fraction=weighted_fraction,
        weighted_relative_directivity_level=weighted_level,
        worst_required_seat_relative_directivity_level=worst_level,
        worst_required_seat_off_axis_loss=worst_loss,
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
    pairs: list[tuple[ObjectiveDefinition, CoverageScalarResult]] = list(
        zip(definitions, results, strict=True)
    )
    if evaluation.priority_aggregates is not None:
        priority = evaluation.priority_aggregates
        # #975: the weighted soft aggregate and the independent
        # required-seat floor are optimization objectives too — selecting a
        # SeatPriorityProfile must change the vector optimization consumes,
        # not only the persisted evidence.
        bounded = ObjectiveValidDomain(
            kind='bounded_real', minimum=0.0, maximum=1.0
        )
        finite = ObjectiveValidDomain(kind='finite_real')
        for objective_id, quantity, unit, direction, domain, result in (
            (
                'o100d.priority.coverage.weighted_useful_fraction',
                'coverage_weighted_useful_fraction',
                'ratio',
                'maximize',
                bounded,
                priority.weighted_useful_coverage_fraction,
            ),
            (
                'o100d.priority.coverage.weighted_relative_level_db',
                'coverage_weighted_relative_directivity_level',
                'dB',
                'maximize',
                finite,
                priority.weighted_relative_directivity_level,
            ),
            (
                'o100d.priority.coverage.worst_required_relative_level_db',
                'coverage_worst_required_relative_directivity_level',
                'dB',
                'maximize',
                finite,
                priority.worst_required_seat_relative_directivity_level,
            ),
            (
                'o100d.priority.coverage.worst_required_off_axis_loss_db',
                'coverage_worst_required_off_axis_loss',
                'dB',
                'minimize',
                finite,
                priority.worst_required_seat_off_axis_loss,
            ),
        ):
            pairs.append(
                (
                    _objective_definition(
                        evaluation.scenario,
                        objective_id=objective_id,
                        quantity=quantity,
                        unit=unit,
                        direction=direction,
                        valid_domain=domain,
                    ),
                    result,
                )
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
            for definition, result in pairs
        ),
    )
