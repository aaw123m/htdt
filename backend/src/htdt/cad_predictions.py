from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isclose
from typing import Any
from uuid import uuid4

from .acoustics import ACOUSTICS_ALGORITHM_VERSION, first_order_reflections, rectangular_room_modes
from .cad_prediction_models import (
    CadPredictionResult,
    CadPredictedReflection,
    CadPredictedRoomMode,
    PredictionGeometryCompatibility,
    canonical_prediction_json,
    prediction_input_hash,
    prediction_result_sha256,
)
from .cad_listener_pose import (
    ListenerPoseAuthority,
    resolve_listener_receiver,
)
from .cad_room_operating_state import (
    RoomOperatingState,
    compile_operating_state_consumption,
)
from .cad_repository import SceneRevision
from .cad_scene import (
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    acoustic_reference_position,
    room_vertices,
)
from .cad_system_variant import SystemVariant, materialize_system_variant
from .r120_geometry_compiler import ExactExternalAuthorityRef


RECTANGULAR_GEOMETRY_MODEL_ID = 'htdt.rectangular_geometry'
RECTANGULAR_GEOMETRY_MODEL_VERSION = ACOUSTICS_ALGORITHM_VERSION
RECTANGULAR_GEOMETRY_ASSUMPTIONS = (
    'rectangular_room',
    'point_source_geometry',
    'modal_frequency_only_amplitude_and_damping_not_modelled',
    'specular_first_order_reflection_geometry',
    'reflection_amplitude_and_phase_not_modelled',
    'speaker_directivity_not_modelled',
    'candidate_frequencies_are_not_measured_diagnoses',
)


@dataclass(frozen=True)
class RectangularRoomFrame:
    origin_x_m: float
    origin_y_m: float
    width_m: float
    depth_m: float
    height_m: float


def exact_rectangular_room_frame(room: RoomPrism, *, tolerance: float = 1e-9) -> RectangularRoomFrame | None:
    """Return an axis-aligned rectangular frame without silently approximating polygon rooms."""

    if room.footprint_vertices is None:
        return RectangularRoomFrame(0.0, 0.0, room.width_m, room.depth_m, room.height_m)

    vertices = room_vertices(room)
    if len(vertices) != 4:
        return None
    min_x, min_y, max_x, max_y = room.bounds_m
    expected = (
        (min_x, min_y),
        (max_x, min_y),
        (max_x, max_y),
        (min_x, max_y),
    )
    actual = tuple((vertex.x_m, vertex.y_m) for vertex in vertices)
    for corner in expected:
        if not any(
            isclose(corner[0], point[0], abs_tol=tolerance, rel_tol=0.0)
            and isclose(corner[1], point[1], abs_tol=tolerance, rel_tol=0.0)
            for point in actual
        ):
            return None
    return RectangularRoomFrame(min_x, min_y, max_x - min_x, max_y - min_y, room.height_m)


def _position_payload(position: Position3 | None) -> dict[str, float] | None:
    return None if position is None else position.model_dump(mode='json')


def _bind_listener_pose(
    snapshot: dict[str, object],
    listener_pose: ListenerPoseAuthority | None,
) -> None:
    """Pin the exact selected listener pose into the canonical request.

    The full sealed authority payload (id, version, semantic hash, offsets,
    facing) joins the snapshot, so input identity changes with the pose and
    replay can re-validate it. The orientation semantics marker states
    explicitly that this point-pressure receiver does not consume head
    facing; binaural/directional consumers bind ear geometry separately.
    """

    if listener_pose is None:
        return
    from .cad_listener_pose import LISTENER_RECEIVER_ORIENTATION_SEMANTICS

    snapshot['listener_pose'] = {
        'authority': listener_pose.model_dump(mode='json'),
        'receiver_resolution': 'listener_pose',
        'orientation_semantics': LISTENER_RECEIVER_ORIENTATION_SEMANTICS,
    }


def _bind_operating_state(
    snapshot: dict[str, object],
    operating_state: RoomOperatingState | None,
    consumption: Any | None,
) -> None:
    """Pin the exact selected room operating state into the request (#941).

    The full sealed authority joins the snapshot so replay revalidates the
    state record itself, and the typed consumption block states which state
    domains the solver actually consumed — doors/openings bind to portal
    topology where the scene supports it, curtains stay UNKNOWN without a
    material authority, HVAC never alters deterministic transfer.
    """

    if operating_state is None:
        return
    snapshot['room_operating_state'] = {
        'authority': operating_state.model_dump(mode='json'),
        'consumption': (
            None if consumption is None else consumption.model_dump(mode='json')
        ),
    }


def _local_position(position: Position3, frame: RectangularRoomFrame) -> tuple[float, float, float]:
    return (
        position.x_m - frame.origin_x_m,
        position.y_m - frame.origin_y_m,
        position.z_m,
    )


def _target_document(
    revision: SceneRevision,
    system_variant: SystemVariant | None,
) -> SceneDocument:
    """Effective prediction document (#983).

    A variant-bound request never applies the variant: the exact baseline
    SceneRevision is validated against the persisted proposal and the
    materialized proposal document is used for source/receiver resolution.
    """

    if system_variant is None:
        return revision.document
    if system_variant.baseline_revision_id != revision.revision_id:
        raise ValueError('SystemVariant baseline SceneRevision mismatch')
    if system_variant.document_id != revision.document_id:
        raise ValueError('SystemVariant baseline document mismatch')
    if system_variant.baseline_content_hash != revision.content_hash:
        raise ValueError('SystemVariant baseline SceneRevision content hash mismatch')
    return materialize_system_variant(revision, system_variant)


def _system_variant_ref(system_variant: SystemVariant) -> dict[str, str]:
    """Exact variant authority ref embedded in variant-bound request snapshots."""

    return {
        'variant_id': system_variant.variant_id,
        'variant_sha256': system_variant.variant_sha256,
        'baseline_revision_id': system_variant.baseline_revision_id,
        'baseline_content_hash': system_variant.baseline_content_hash,
    }


def _inside_frame(position: Position3, frame: RectangularRoomFrame, *, tolerance: float = 1e-9) -> bool:
    return (
        frame.origin_x_m - tolerance <= position.x_m <= frame.origin_x_m + frame.width_m + tolerance
        and frame.origin_y_m - tolerance <= position.y_m <= frame.origin_y_m + frame.depth_m + tolerance
        and -tolerance <= position.z_m <= frame.height_m + tolerance
    )


def _surface_identities(document: SceneDocument, frame: RectangularRoomFrame) -> dict[str, str]:
    room = document.room
    if room is None:
        return {}
    identities = {
        'left_x0': f'room:{room.room_id}:left',
        'right_xW': f'room:{room.room_id}:right',
        'front_y0': f'room:{room.room_id}:front',
        'rear_yD': f'room:{room.room_id}:rear',
        'floor_z0': f'room:{room.room_id}:floor',
        'ceiling_zH': f'room:{room.room_id}:ceiling',
    }
    topology = document.wall_topology
    if topology is None:
        return identities

    vertices = {vertex.vertex_id: vertex for vertex in room_vertices(room)}
    matches: dict[str, list[str]] = {key: [] for key in ('left_x0', 'right_xW', 'front_y0', 'rear_yD')}
    min_x = frame.origin_x_m
    max_x = frame.origin_x_m + frame.width_m
    min_y = frame.origin_y_m
    max_y = frame.origin_y_m + frame.depth_m
    for wall in topology.walls:
        start = vertices.get(wall.from_vertex_id)
        end = vertices.get(wall.to_vertex_id)
        if start is None or end is None:
            continue
        if isclose(start.x_m, min_x, abs_tol=1e-9) and isclose(end.x_m, min_x, abs_tol=1e-9):
            matches['left_x0'].append(wall.wall_id)
        elif isclose(start.x_m, max_x, abs_tol=1e-9) and isclose(end.x_m, max_x, abs_tol=1e-9):
            matches['right_xW'].append(wall.wall_id)
        elif isclose(start.y_m, min_y, abs_tol=1e-9) and isclose(end.y_m, min_y, abs_tol=1e-9):
            matches['front_y0'].append(wall.wall_id)
        elif isclose(start.y_m, max_y, abs_tol=1e-9) and isclose(end.y_m, max_y, abs_tol=1e-9):
            matches['rear_yD'].append(wall.wall_id)
    for key, wall_ids in matches.items():
        if wall_ids:
            identities[key] = 'wall:' + ','.join(sorted(wall_ids))
    return identities


@dataclass(frozen=True)
class RectangularGeometryModelInput:
    """Canonical rectangular-geometry request compiled from one exact SceneRevision."""

    parameters_json: str
    input_snapshot_json: str
    input_hash: str
    geometry_compatibility: PredictionGeometryCompatibility
    receiver_position: Position3
    frame: RectangularRoomFrame | None
    speaker_references: tuple[tuple[SceneEntity, Position3], ...]
    surface_identities: dict[str, str]


def rectangular_geometry_model_input(
    revision: SceneRevision,
    receiver_entity_id: str,
    *,
    max_mode_hz: float = 300.0,
    sound_speed_m_s: float = 343.0,
    environment_profile: ExactExternalAuthorityRef | None = None,
    listener_pose: ListenerPoseAuthority | None = None,
    operating_state: RoomOperatingState | None = None,
    system_variant: SystemVariant | None = None,
) -> RectangularGeometryModelInput:
    """Compile the canonical rectangular-geometry request for one exact SceneRevision.

    Single request-compilation authority shared by the prediction analyzer, the
    request-identity builder and persistence-boundary input replay.

    ``environment_profile`` is the exact ``AcousticEnvironmentProfile`` ref the
    ``sound_speed_m_s`` value was sourced from (#479); it is only written into
    the canonical request when bound, so requests persisted before environment
    authorities existed keep their canonical shape.

    ``listener_pose`` (#939) is the exact selected ``ListenerPoseAuthority``
    for a seat receiver: the receiver position resolves through the pose's
    acoustic reference and the full sealed pose joins the input snapshot so
    its id/version/hash pin the request identity. Unbound receivers keep the
    legacy seat offset and produce byte-identical snapshots.

    ``operating_state`` (#941) is the exact ``RoomOperatingState`` selected
    for the run; it must pin this exact SceneRevision, so two states on one
    unchanged revision produce distinct request identities. Its typed
    consumption (which domains the solver actually uses) joins the snapshot
    alongside the full sealed authority for replay revalidation. Unbound
    requests keep their canonical shape.

    ``system_variant`` is an exact proposed ``SystemVariant`` bound to
    ``revision`` (#983): the request evaluates the materialized proposal —
    added/removed/replaced speakers change the source set — and the snapshot
    carries the exact variant identity so current and proposed predictions
    never share a cache entry merely because room geometry is identical.
    """

    document = _target_document(revision, system_variant)
    variant_dump = (
        None
        if system_variant is None
        else _system_variant_ref(system_variant)
    )
    room = document.room
    if room is None:
        raise ValueError('prediction requires a room')
    receiver_entity = document.entity(receiver_entity_id)
    if listener_pose is not None:
        if listener_pose.document_id not in (None, revision.document_id):
            raise ValueError(
                'listener pose is scoped to a different document'
            )
        if receiver_entity.kind != 'seat':
            raise ValueError(
                'a listener pose can only bind a seat receiver'
            )
    resolved_receiver = resolve_listener_receiver(
        receiver_entity,
        listener_pose,
    )
    if resolved_receiver is None:
        raise ValueError('receiver entity has no acoustic reference position')
    receiver = resolved_receiver.position

    operating_consumption = None
    if operating_state is not None:
        if operating_state.scene_revision_id != revision.revision_id:
            raise ValueError(
                'operating state pins a different SceneRevision'
            )
        if operating_state.document_id != revision.document_id:
            raise ValueError(
                'operating state belongs to a different document'
            )
        operating_consumption = compile_operating_state_consumption(
            operating_state, revision.document
        )
    operating_ref_dump = (
        None
        if operating_state is None
        else {
            'state_id': operating_state.state_id,
            'version': operating_state.version,
            'semantic_sha256': operating_state.semantic_sha256,
        }
    )

    parameters: dict[str, object] = {
        'max_mode_hz': float(max_mode_hz),
        'sound_speed_m_s': float(sound_speed_m_s),
    }
    if operating_ref_dump is not None:
        parameters['operating_state'] = operating_ref_dump
    environment_dump = (
        None
        if environment_profile is None
        else environment_profile.model_dump(mode='json')
    )
    if environment_dump is not None:
        parameters['environment_profile'] = environment_dump
    parameters_json = canonical_prediction_json(parameters)
    speaker_inputs: list[dict[str, object]] = []
    speaker_references: list[tuple[SceneEntity, Position3]] = []
    for entity in document.entities:
        if entity.kind != 'speaker':
            continue
        reference = acoustic_reference_position(entity)
        speaker_inputs.append(
            {
                'entity_id': entity.entity_id,
                'speaker_role': entity.speaker_role,
                'acoustic_reference_position': _position_payload(reference),
            }
        )
        if reference is not None:
            speaker_references.append((entity, reference))

    frame = exact_rectangular_room_frame(room)
    if frame is None:
        unsupported_snapshot: dict[str, object] = {
            'room': room.model_dump(mode='json'),
            'receiver_entity_id': receiver_entity_id,
            'receiver_position': _position_payload(receiver),
            'speakers': speaker_inputs,
            'approximation_rule': None,
        }
        _bind_listener_pose(unsupported_snapshot, listener_pose)
        _bind_operating_state(
            unsupported_snapshot, operating_state, operating_consumption
        )
        if environment_dump is not None:
            unsupported_snapshot['environment_profile'] = environment_dump
        if variant_dump is not None:
            unsupported_snapshot['system_variant'] = variant_dump
        input_snapshot_json = canonical_prediction_json(unsupported_snapshot)
        return RectangularGeometryModelInput(
            parameters_json=parameters_json,
            input_snapshot_json=input_snapshot_json,
            input_hash=prediction_input_hash(input_snapshot_json),
            geometry_compatibility='unsupported',
            receiver_position=receiver,
            frame=None,
            speaker_references=tuple(speaker_references),
            surface_identities={},
        )

    if not _inside_frame(receiver, frame):
        raise ValueError('receiver acoustic reference is outside the rectangular room')
    surface_identities = _surface_identities(document, frame)
    snapshot: dict[str, object] = {
        'room_frame': {
            'origin_x_m': frame.origin_x_m,
            'origin_y_m': frame.origin_y_m,
            'width_m': frame.width_m,
            'depth_m': frame.depth_m,
            'height_m': frame.height_m,
        },
        'receiver_entity_id': receiver_entity_id,
        'receiver_position': _position_payload(receiver),
        'speakers': speaker_inputs,
        'surface_identities': surface_identities,
        'approximation_rule': None,
    }
    _bind_listener_pose(snapshot, listener_pose)
    _bind_operating_state(
        snapshot, operating_state, operating_consumption
    )
    if environment_dump is not None:
        snapshot['environment_profile'] = environment_dump
    if variant_dump is not None:
        snapshot['system_variant'] = variant_dump
    input_snapshot_json = canonical_prediction_json(snapshot)
    return RectangularGeometryModelInput(
        parameters_json=parameters_json,
        input_snapshot_json=input_snapshot_json,
        input_hash=prediction_input_hash(input_snapshot_json),
        geometry_compatibility='exact_for_model_geometry',
        receiver_position=receiver,
        frame=frame,
        speaker_references=tuple(speaker_references),
        surface_identities=surface_identities,
    )


def _make_result(
    *,
    run_id: str,
    revision: SceneRevision,
    result_kind: str,
    compatibility: str,
    parameters_json: str,
    input_snapshot_json: str,
    submitted_at_utc: str,
    completed_at_utc: str,
    constraint_workspace_hash: str | None,
    warnings: tuple[str, ...],
    modes: tuple[CadPredictedRoomMode, ...] = (),
    reflections: tuple[CadPredictedReflection, ...] = (),
) -> CadPredictionResult:
    fields = {
        'prediction_id': str(uuid4()),
        'run_id': run_id,
        'document_id': revision.document_id,
        'scene_revision_id': revision.revision_id,
        'scene_content_hash': revision.content_hash,
        'constraint_workspace_hash': constraint_workspace_hash,
        'model_id': RECTANGULAR_GEOMETRY_MODEL_ID,
        'model_version': RECTANGULAR_GEOMETRY_MODEL_VERSION,
        'result_kind': result_kind,
        'geometry_compatibility': compatibility,
        'parameters_json': parameters_json,
        'input_snapshot_json': input_snapshot_json,
        'input_hash': prediction_input_hash(input_snapshot_json),
        'submitted_at_utc': submitted_at_utc,
        'completed_at_utc': completed_at_utc,
        'assumptions': RECTANGULAR_GEOMETRY_ASSUMPTIONS,
        'warnings': warnings,
        'modes': modes,
        'reflections': reflections,
    }
    # Seal the result with its versioned output identity: ``result_sha256``
    # commits to every semantic field (and only those — storage ids and
    # timestamps are excluded), so two honest runs of the same model input
    # produce the same output identity.
    provisional = CadPredictionResult.model_construct(result_sha256='0' * 64, **fields)
    return CadPredictionResult(
        result_sha256=prediction_result_sha256(provisional.result_identity_payload()),
        **fields,
    )


def analyze_native_rectangular_geometry(
    revision: SceneRevision,
    receiver_entity_id: str,
    *,
    max_mode_hz: float = 300.0,
    sound_speed_m_s: float = 343.0,
    constraint_workspace_hash: str | None = None,
    environment_profile: ExactExternalAuthorityRef | None = None,
    listener_pose: ListenerPoseAuthority | None = None,
    operating_state: RoomOperatingState | None = None,
    system_variant: SystemVariant | None = None,
) -> tuple[CadPredictionResult, CadPredictionResult]:
    """Run the existing rectangular geometry model against one exact native revision."""

    model_input = rectangular_geometry_model_input(
        revision,
        receiver_entity_id,
        max_mode_hz=max_mode_hz,
        sound_speed_m_s=sound_speed_m_s,
        environment_profile=environment_profile,
        listener_pose=listener_pose,
        operating_state=operating_state,
        system_variant=system_variant,
    )
    document = _target_document(revision, system_variant)
    parameters_json = model_input.parameters_json
    input_snapshot_json = model_input.input_snapshot_json
    run_id = str(uuid4())
    submitted_at = datetime.now(timezone.utc).isoformat()

    frame = model_input.frame
    if frame is None:
        completed_at = datetime.now(timezone.utc).isoformat()
        warnings = ('rectangular_geometry_model_requires_axis_aligned_rectangular_room',)
        return (
            _make_result(
                run_id=run_id,
                revision=revision,
                result_kind='geometry_modes',
                compatibility='unsupported',
                parameters_json=parameters_json,
                input_snapshot_json=input_snapshot_json,
                submitted_at_utc=submitted_at,
                completed_at_utc=completed_at,
                constraint_workspace_hash=constraint_workspace_hash,
                warnings=warnings,
            ),
            _make_result(
                run_id=run_id,
                revision=revision,
                result_kind='geometry_reflections',
                compatibility='unsupported',
                parameters_json=parameters_json,
                input_snapshot_json=input_snapshot_json,
                submitted_at_utc=submitted_at,
                completed_at_utc=completed_at,
                constraint_workspace_hash=constraint_workspace_hash,
                warnings=warnings,
            ),
        )

    receiver = model_input.receiver_position
    surface_identities = model_input.surface_identities

    modes = tuple(
        CadPredictedRoomMode(
            n_x=mode.n_x,
            n_y=mode.n_y,
            n_z=mode.n_z,
            frequency_hz=mode.frequency_hz,
            mode_class=mode.mode_class,
        )
        for mode in rectangular_room_modes(
            frame.width_m,
            frame.depth_m,
            frame.height_m,
            max_hz=max_mode_hz,
            sound_speed_m_s=sound_speed_m_s,
        )
    )

    warnings: list[str] = []
    reflections: list[CadPredictedReflection] = []
    for entity, source in model_input.speaker_references:
        if not _inside_frame(source, frame):
            warnings.append(f'speaker_acoustic_reference_outside_room:{entity.entity_id}')
            continue
        local_source = _local_position(source, frame)
        local_receiver = _local_position(receiver, frame)
        for candidate in first_order_reflections(
            frame.width_m,
            frame.depth_m,
            frame.height_m,
            local_source,
            local_receiver,
            speaker_id=entity.entity_id,
            speaker_role=str(entity.speaker_role),
            sound_speed_m_s=sound_speed_m_s,
        ):
            point = Position3(
                x_m=candidate.reflection_point_m[0] + frame.origin_x_m,
                y_m=candidate.reflection_point_m[1] + frame.origin_y_m,
                z_m=candidate.reflection_point_m[2],
            )
            reflections.append(
                CadPredictedReflection(
                    speaker_entity_id=entity.entity_id,
                    speaker_role=str(entity.speaker_role),
                    surface_key=candidate.surface,
                    surface_identity=surface_identities[candidate.surface],
                    reflection_position=point,
                    source_position=source,
                    receiver_position=receiver,
                    direct_length_m=candidate.direct_length_m,
                    reflected_length_m=candidate.reflected_length_m,
                    excess_length_m=candidate.excess_length_m,
                    excess_delay_ms=candidate.excess_delay_ms,
                    first_destructive_hz=candidate.first_destructive_hz,
                )
            )
    referenced = {entity.entity_id for entity, _source in model_input.speaker_references}
    missing = [
        entity.entity_id
        for entity in document.entities
        if entity.kind == 'speaker' and entity.entity_id not in referenced
    ]
    warnings.extend(f'speaker_acoustic_reference_unknown:{entity_id}' for entity_id in missing)
    completed_at = datetime.now(timezone.utc).isoformat()
    warning_tuple = tuple(dict.fromkeys(warnings))
    return (
        _make_result(
            run_id=run_id,
            revision=revision,
            result_kind='geometry_modes',
            compatibility='exact_for_model_geometry',
            parameters_json=parameters_json,
            input_snapshot_json=input_snapshot_json,
            submitted_at_utc=submitted_at,
            completed_at_utc=completed_at,
            constraint_workspace_hash=constraint_workspace_hash,
            warnings=warning_tuple,
            modes=modes,
        ),
        _make_result(
            run_id=run_id,
            revision=revision,
            result_kind='geometry_reflections',
            compatibility='exact_for_model_geometry',
            parameters_json=parameters_json,
            input_snapshot_json=input_snapshot_json,
            submitted_at_utc=submitted_at,
            completed_at_utc=completed_at,
            constraint_workspace_hash=constraint_workspace_hash,
            warnings=warning_tuple,
            reflections=tuple(reflections),
        ),
    )
