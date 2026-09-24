from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
from math import isfinite

from .cad_prediction_models import CadPredictionResult
from .cad_predictions import (
    RECTANGULAR_GEOMETRY_MODEL_ID,
    RECTANGULAR_GEOMETRY_MODEL_VERSION,
    analyze_native_rectangular_geometry,
    rectangular_geometry_model_input,
)
from .cad_repository import SceneRevision
from .r120_geometry_compiler import ExactExternalAuthorityRef


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
    environment_profile: ExactExternalAuthorityRef | None = None,
) -> RectangularGeometryRequestIdentity:
    """Build the exact canonical model identity used by the rectangular adapter."""

    model_input = rectangular_geometry_model_input(
        revision,
        receiver_entity_id,
        max_mode_hz=max_mode_hz,
        sound_speed_m_s=sound_speed_m_s,
        environment_profile=environment_profile,
    )
    return RectangularGeometryRequestIdentity(
        model_id=RECTANGULAR_GEOMETRY_MODEL_ID,
        model_version=RECTANGULAR_GEOMETRY_MODEL_VERSION,
        parameters_json=model_input.parameters_json,
        input_snapshot_json=model_input.input_snapshot_json,
        input_hash=model_input.input_hash,
        geometry_compatibility=model_input.geometry_compatibility,
    )


def _environment_profile_ref(decoded: object) -> ExactExternalAuthorityRef | None:
    """Optional exact environment-profile ref inside parameters_json (#479).

    The key only exists on requests bound to an ``AcousticEnvironmentProfile``;
    requests persisted before the authority existed have no key and stay
    canonical.
    """

    if not isinstance(decoded, dict):
        raise ValueError('prediction parameters_json must match the rectangular model contract')
    raw = decoded.get('environment_profile')
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError('prediction environment_profile must be an exact authority ref')
    try:
        return ExactExternalAuthorityRef.model_validate(raw)
    except Exception as exc:
        raise ValueError(
            'prediction environment_profile must be an exact authority ref'
        ) from exc


def _rectangular_geometry_parameters(
    parameters_json: str,
) -> tuple[float, float, ExactExternalAuthorityRef | None]:
    """Parse persisted parameters through the pinned rectangular model contract."""

    try:
        decoded = json.loads(parameters_json)
    except json.JSONDecodeError as exc:
        raise ValueError('prediction parameters_json must contain JSON') from exc
    if not isinstance(decoded, dict) or not {'max_mode_hz', 'sound_speed_m_s'} <= set(decoded):
        raise ValueError('prediction parameters_json must match the rectangular model contract')
    unknown = set(decoded) - {'max_mode_hz', 'sound_speed_m_s', 'environment_profile'}
    if unknown:
        raise ValueError('prediction parameters_json must match the rectangular model contract')
    environment_profile = _environment_profile_ref(decoded)
    raw_values = (decoded['max_mode_hz'], decoded['sound_speed_m_s'])
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in raw_values):
        raise ValueError('prediction parameters_json must match the rectangular model contract')
    max_mode_hz, sound_speed_m_s = (float(value) for value in raw_values)
    if not isfinite(max_mode_hz) or not isfinite(sound_speed_m_s):
        raise ValueError('prediction parameters_json must match the rectangular model contract')
    if max_mode_hz <= 0.0 or sound_speed_m_s <= 0.0:
        raise ValueError('prediction parameters_json must match the rectangular model contract')
    return max_mode_hz, sound_speed_m_s, environment_profile


def rectangular_geometry_environment_profile_ref(
    parameters_json: str,
) -> ExactExternalAuthorityRef | None:
    """Exact ``AcousticEnvironmentProfile`` ref a persisted run was bound to."""

    _max_mode_hz, _sound_speed_m_s, environment_profile = (
        _rectangular_geometry_parameters(parameters_json)
    )
    return environment_profile


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

    max_mode_hz, sound_speed_m_s, environment_profile = (
        _rectangular_geometry_parameters(parameters_json)
    )
    receiver_entity_id = _request_receiver_entity_id(input_snapshot_json)
    try:
        return rectangular_geometry_request_identity(
            revision,
            receiver_entity_id,
            max_mode_hz=max_mode_hz,
            sound_speed_m_s=sound_speed_m_s,
            environment_profile=environment_profile,
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


def _replay_rectangular_geometry_run(
    revision: SceneRevision,
    parameters_json: str,
    input_snapshot_json: str,
    constraint_workspace_hash: str | None,
) -> tuple[CadPredictionResult, ...]:
    """Re-run the pinned rectangular model for one exact SceneRevision.

    Parameters and the receiver identity are parsed through the same pinned
    contract helpers the input replayer uses, then the analyzer re-derives
    the whole run — modes, reflections, assumptions and warnings — so the
    canonical output identity can be compared against a stored record.
    """

    max_mode_hz, sound_speed_m_s, environment_profile = (
        _rectangular_geometry_parameters(parameters_json)
    )
    receiver_entity_id = _request_receiver_entity_id(input_snapshot_json)
    try:
        return analyze_native_rectangular_geometry(
            revision,
            receiver_entity_id,
            max_mode_hz=max_mode_hz,
            sound_speed_m_s=sound_speed_m_s,
            constraint_workspace_hash=constraint_workspace_hash,
            environment_profile=environment_profile,
        )
    except KeyError as exc:
        raise ValueError('prediction input receiver is not part of the source revision') from exc


PredictionOutputReplay = Callable[
    [SceneRevision, str, str, str | None],
    tuple[CadPredictionResult, ...],
]

# Output side of the versioned prediction-model authority registry. Each
# persisted (model_id, model_version) pair needs an explicit replayer that
# re-runs the pinned model against the exact SceneRevision and returns the
# canonical run. Versions without a registered replayer — including future
# historical records — are non-authoritative: persistence fails closed
# instead of trusting the stored payload.
PREDICTION_OUTPUT_AUTHORITIES: dict[tuple[str, str], PredictionOutputReplay] = {
    (
        RECTANGULAR_GEOMETRY_MODEL_ID,
        RECTANGULAR_GEOMETRY_MODEL_VERSION,
    ): _replay_rectangular_geometry_run,
}


def verify_prediction_output(
    revision: SceneRevision,
    result: CadPredictionResult,
) -> None:
    """Require ``result`` to be the canonical output of the pinned model.

    Re-runs the registered versioned output authority against the exact
    source SceneRevision and the (already input-verified) canonical request,
    then demands the stored ``result_sha256`` equal the semantic output
    identity of the canonical result for the same ``result_kind``. A row
    whose output was rewritten coherently — payload columns and a
    self-consistent ``result_sha256`` together — still fails closed because
    the hash must equal the *recomputed* canonical output, not merely the
    stored columns.
    """

    replayer = PREDICTION_OUTPUT_AUTHORITIES.get((result.model_id, result.model_version))
    if replayer is None:
        raise ValueError(
            'prediction model has no registered output authority: '
            f'{result.model_id} {result.model_version}'
        )
    canonical_run = replayer(
        revision,
        result.parameters_json,
        result.input_snapshot_json,
        result.constraint_workspace_hash,
    )
    canonical = next(
        (item for item in canonical_run if item.result_kind == result.result_kind),
        None,
    )
    if canonical is None or canonical.result_sha256 != result.result_sha256:
        raise ValueError('prediction result does not match the canonical model output')
