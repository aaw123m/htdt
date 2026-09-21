from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
from math import isfinite

from .cad_predictions import (
    RECTANGULAR_GEOMETRY_MODEL_ID,
    RECTANGULAR_GEOMETRY_MODEL_VERSION,
    rectangular_geometry_model_input,
)
from .cad_repository import SceneRevision


@dataclass(frozen=True)
class RectangularGeometryRequestIdentity:
    model_id: str
    model_version: str
    parameters_json: str
    input_snapshot_json: str
    input_hash: str
    geometry_compatibility: str


def rectangular_geometry_request_identity(
    revision: SceneRevision,
    receiver_entity_id: str,
    *,
    max_mode_hz: float = 300.0,
    sound_speed_m_s: float = 343.0,
) -> RectangularGeometryRequestIdentity:
    """Build the exact canonical model identity used by the rectangular adapter."""

    model_input = rectangular_geometry_model_input(
        revision,
        receiver_entity_id,
        max_mode_hz=max_mode_hz,
        sound_speed_m_s=sound_speed_m_s,
    )
    return RectangularGeometryRequestIdentity(
        model_id=RECTANGULAR_GEOMETRY_MODEL_ID,
        model_version=RECTANGULAR_GEOMETRY_MODEL_VERSION,
        parameters_json=model_input.parameters_json,
        input_snapshot_json=model_input.input_snapshot_json,
        input_hash=model_input.input_hash,
        geometry_compatibility=model_input.geometry_compatibility,
    )


def _rectangular_geometry_parameters(parameters_json: str) -> tuple[float, float]:
    """Parse persisted parameters through the pinned rectangular model contract."""

    try:
        decoded = json.loads(parameters_json)
    except json.JSONDecodeError as exc:
        raise ValueError('prediction parameters_json must contain JSON') from exc
    if not isinstance(decoded, dict) or set(decoded) != {'max_mode_hz', 'sound_speed_m_s'}:
        raise ValueError('prediction parameters_json must match the rectangular model contract')
    raw_values = (decoded['max_mode_hz'], decoded['sound_speed_m_s'])
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in raw_values):
        raise ValueError('prediction parameters_json must match the rectangular model contract')
    max_mode_hz, sound_speed_m_s = (float(value) for value in raw_values)
    if not isfinite(max_mode_hz) or not isfinite(sound_speed_m_s):
        raise ValueError('prediction parameters_json must match the rectangular model contract')
    if max_mode_hz <= 0.0 or sound_speed_m_s <= 0.0:
        raise ValueError('prediction parameters_json must match the rectangular model contract')
    return max_mode_hz, sound_speed_m_s


def _request_receiver_entity_id(input_snapshot_json: str) -> str:
    try:
        decoded = json.loads(input_snapshot_json)
    except json.JSONDecodeError as exc:
        raise ValueError('prediction input_snapshot_json must contain JSON') from exc
    if not isinstance(decoded, dict):
        raise ValueError('prediction input_snapshot_json must be a JSON object')
    receiver_entity_id = decoded.get('receiver_entity_id')
    if not isinstance(receiver_entity_id, str) or not receiver_entity_id:
        raise ValueError('prediction input snapshot must name a receiver entity')
    return receiver_entity_id


def _replay_rectangular_geometry_request(
    revision: SceneRevision,
    parameters_json: str,
    input_snapshot_json: str,
) -> RectangularGeometryRequestIdentity:
    """Re-derive the canonical rectangular request for one exact SceneRevision.

    The receiver identity is taken from the submitted snapshot; every other
    input element is recompiled from the persisted revision through the same
    request-compilation authority used by live prediction requests.
    """

    max_mode_hz, sound_speed_m_s = _rectangular_geometry_parameters(parameters_json)
    receiver_entity_id = _request_receiver_entity_id(input_snapshot_json)
    try:
        return rectangular_geometry_request_identity(
            revision,
            receiver_entity_id,
            max_mode_hz=max_mode_hz,
            sound_speed_m_s=sound_speed_m_s,
        )
    except KeyError as exc:
        raise ValueError('prediction input receiver is not part of the source revision') from exc


PredictionInputReplay = Callable[
    [SceneRevision, str, str],
    RectangularGeometryRequestIdentity,
]

# Versioned prediction-model authority registry. Each persisted
# (model_id, model_version) pair needs an explicit replayer that reruns the
# pinned canonical request compilation against the exact SceneRevision.
# Versions without a registered replayer — including future historical records
# — are non-authoritative: persistence fails closed instead of trusting the
# stored payload.
PREDICTION_INPUT_AUTHORITIES: dict[tuple[str, str], PredictionInputReplay] = {
    (
        RECTANGULAR_GEOMETRY_MODEL_ID,
        RECTANGULAR_GEOMETRY_MODEL_VERSION,
    ): _replay_rectangular_geometry_request,
}


def verify_prediction_input(
    revision: SceneRevision,
    *,
    model_id: str,
    model_version: str,
    parameters_json: str,
    input_snapshot_json: str,
    input_hash: str,
    geometry_compatibility: str,
) -> RectangularGeometryRequestIdentity:
    """Require the submitted request identity to be the canonical model input.

    Replays the registered versioned model authority against the exact source
    SceneRevision and demands byte-exact equality for model identity, canonical
    parameters JSON, canonical input snapshot JSON, input hash and the
    input-derived geometry compatibility classification.
    """

    replayer = PREDICTION_INPUT_AUTHORITIES.get((model_id, model_version))
    if replayer is None:
        raise ValueError(
            f'prediction model has no registered input authority: {model_id} {model_version}'
        )
    identity = replayer(revision, parameters_json, input_snapshot_json)
    if identity.model_id != model_id or identity.model_version != model_version:
        raise ValueError('prediction model identity does not match the canonical model request')
    if parameters_json != identity.parameters_json:
        raise ValueError('prediction parameters do not match the canonical model request')
    if input_snapshot_json != identity.input_snapshot_json:
        raise ValueError('prediction input snapshot is not the canonical model request')
    if input_hash != identity.input_hash:
        raise ValueError('prediction input hash does not match the canonical model request')
    if geometry_compatibility != identity.geometry_compatibility:
        raise ValueError('prediction geometry compatibility is not the canonical classification')
    return identity
