from __future__ import annotations

from dataclasses import dataclass
import json
from math import asin, atan2, cos, degrees, radians, sin

from .cad_extended_search import (
    CadExtendedCandidate,
    extended_candidate_preview_document,
)
from .cad_repository import SceneRevision
from .cad_scene import SceneDocument
from .cad_search import candidate_preview_document
from .cad_search_models import CadCandidate
from .optimization_robustness import PerturbationSample, RobustnessSpec


@dataclass(frozen=True, slots=True)
class PositionToleranceOverlay:
    axis_id: str
    entity_id: str
    parameter: str
    nominal: tuple[float, float, float]
    minimum: tuple[float, float, float]
    maximum: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class AngularToleranceOverlay:
    axis_id: str
    entity_id: str
    parameter: str
    origin: tuple[float, float, float]
    minus_endpoint: tuple[float, float, float]
    nominal_endpoint: tuple[float, float, float]
    plus_endpoint: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class InfeasibleToleranceMarker:
    sample_id: str
    entity_id: str
    point: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class RobustnessOverlayModel:
    document: SceneDocument
    position_tolerances: tuple[PositionToleranceOverlay, ...]
    angular_tolerances: tuple[AngularToleranceOverlay, ...]
    infeasible_markers: tuple[InfeasibleToleranceMarker, ...]


def _candidate_document(
    source_revision: SceneRevision,
    spec: RobustnessSpec,
) -> SceneDocument:
    payload = json.loads(spec.candidate_payload_json)
    if spec.candidate_kind == 'extended_candidate':
        candidate = CadExtendedCandidate.model_validate(payload)
        return extended_candidate_preview_document(source_revision.document, candidate)
    candidate = CadCandidate.model_validate(payload)
    return candidate_preview_document(source_revision.document, candidate)


def _direction_endpoint(
    origin: tuple[float, float, float],
    *,
    yaw_deg: float,
    pitch_deg: float,
    length_m: float,
) -> tuple[float, float, float]:
    yaw = radians(float(yaw_deg))
    pitch = radians(float(pitch_deg))
    horizontal = cos(pitch)
    return (
        origin[0] + length_m * horizontal * cos(yaw),
        origin[1] + length_m * horizontal * sin(yaw),
        origin[2] + length_m * sin(pitch),
    )


def build_robustness_overlay_model(
    *,
    source_revision: SceneRevision,
    spec: RobustnessSpec,
    samples: tuple[PerturbationSample, ...] = (),
    ray_length_m: float = 0.9,
) -> RobustnessOverlayModel:
    """Build read-only 3D tolerance/aim primitives from exact O90 authority."""

    if ray_length_m <= 0.0:
        raise ValueError('robustness overlay ray length must be positive')
    if (
        source_revision.document_id != spec.document_id
        or source_revision.revision_id != spec.scene_revision_id
        or source_revision.content_hash != spec.scene_content_hash
    ):
        raise ValueError('robustness overlay SceneRevision authority mismatch')

    document = _candidate_document(source_revision, spec)
    positions: list[PositionToleranceOverlay] = []
    angular: list[AngularToleranceOverlay] = []
    infeasible_markers: list[InfeasibleToleranceMarker] = []
    axis_by_id = {axis.axis_id: axis for axis in spec.axes}

    for axis in spec.axes:
        entity = document.entity(axis.entity_id)
        origin = (
            float(entity.position.x_m),
            float(entity.position.y_m),
            float(entity.position.z_m),
        )
        if axis.parameter.endswith('_m'):
            coordinate = (
                axis.parameter.removeprefix('speaker_')
                .removeprefix('listener_')
                .removesuffix('_m')
            )
            index = {'x': 0, 'y': 1, 'z': 2}[coordinate]
            nominal = list(origin)
            minimum = list(origin)
            maximum = list(origin)
            nominal[index] = float(axis.nominal_value)
            minimum[index] = float(axis.nominal_value - axis.minus_delta)
            maximum[index] = float(axis.nominal_value + axis.plus_delta)
            positions.append(
                PositionToleranceOverlay(
                    axis_id=axis.axis_id,
                    entity_id=axis.entity_id,
                    parameter=axis.parameter,
                    nominal=tuple(nominal),
                    minimum=tuple(minimum),
                    maximum=tuple(maximum),
                )
            )
            continue

        if axis.parameter in {'aim_yaw_deg', 'aim_pitch_deg'}:
            if entity.aim_xyz is None:
                raise ValueError(
                    f'robustness overlay axis requires acoustic aim: {axis.axis_id}'
                )
            direction = entity.aim_xyz
            actual_yaw = degrees(atan2(float(direction.y), float(direction.x)))
            actual_pitch = degrees(
                asin(max(-1.0, min(1.0, float(direction.z))))
            )
            if axis.parameter == 'aim_yaw_deg':
                nominal_yaw = float(axis.nominal_value)
                nominal_pitch = actual_pitch
                minus_yaw = nominal_yaw - float(axis.minus_delta)
                plus_yaw = nominal_yaw + float(axis.plus_delta)
                minus_pitch = plus_pitch = nominal_pitch
            else:
                nominal_yaw = actual_yaw
                nominal_pitch = float(axis.nominal_value)
                minus_yaw = plus_yaw = nominal_yaw
                minus_pitch = nominal_pitch - float(axis.minus_delta)
                plus_pitch = nominal_pitch + float(axis.plus_delta)
        elif axis.parameter == 'body_yaw_deg':
            nominal_yaw = float(axis.nominal_value)
            nominal_pitch = 0.0
            minus_yaw = nominal_yaw - float(axis.minus_delta)
            plus_yaw = nominal_yaw + float(axis.plus_delta)
            minus_pitch = plus_pitch = 0.0
        else:
            raise ValueError(f'unsupported robustness overlay axis: {axis.parameter}')

        angular.append(
            AngularToleranceOverlay(
                axis_id=axis.axis_id,
                entity_id=axis.entity_id,
                parameter=axis.parameter,
                origin=origin,
                minus_endpoint=_direction_endpoint(
                    origin,
                    yaw_deg=minus_yaw,
                    pitch_deg=minus_pitch,
                    length_m=ray_length_m,
                ),
                nominal_endpoint=_direction_endpoint(
                    origin,
                    yaw_deg=nominal_yaw,
                    pitch_deg=nominal_pitch,
                    length_m=ray_length_m,
                ),
                plus_endpoint=_direction_endpoint(
                    origin,
                    yaw_deg=plus_yaw,
                    pitch_deg=plus_pitch,
                    length_m=ray_length_m,
                ),
            )
        )

    for sample in samples:
        if sample.feasible:
            continue
        changed: dict[str, list[float]] = {}
        for axis_id, delta in sample.parameter_deltas.items():
            axis = axis_by_id.get(axis_id)
            if axis is None:
                continue
            entity = document.entity(axis.entity_id)
            point = changed.setdefault(
                axis.entity_id,
                [
                    float(entity.position.x_m),
                    float(entity.position.y_m),
                    float(entity.position.z_m),
                ],
            )
            if axis.parameter.endswith('_m'):
                coordinate = (
                    axis.parameter.removeprefix('speaker_')
                    .removeprefix('listener_')
                    .removesuffix('_m')
                )
                index = {'x': 0, 'y': 1, 'z': 2}[coordinate]
                point[index] = float(axis.nominal_value + float(delta))
        for entity_id, point in sorted(changed.items()):
            infeasible_markers.append(
                InfeasibleToleranceMarker(
                    sample_id=sample.sample_id,
                    entity_id=entity_id,
                    point=tuple(point),
                )
            )

    return RobustnessOverlayModel(
        document=document,
        position_tolerances=tuple(positions),
        angular_tolerances=tuple(angular),
        infeasible_markers=tuple(infeasible_markers),
    )
