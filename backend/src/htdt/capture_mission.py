from __future__ import annotations

from base64 import b64decode, b64encode
from hashlib import sha256
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_equipment_catalog import (
    EquipmentCatalogEntry,
    EquipmentCatalogSnapshot,
)
from .cad_scene import (
    SceneDocument,
    SceneEntity,
    is_unassigned_speaker_role,
    scene_content_hash,
)
from .project_identity import (
    HTDTLegacyProjectRef,
    HTDTProjectReference,
    InboundProjectRef,
)
from .canonical_json import canonical_json as _canonical_json, hash_parts as _hash_parts


class CaptureMissionError(ValueError):
    """A mission, plan, or package could not be produced or decoded."""




def _deterministic_uuid(domain: str, *parts: str) -> str:
    """Derive a lowercase UUIDv4-shaped identity from exact content.

    The same exact inputs always produce the same identifier, which keeps
    plan/task/mission generation deterministic and makes repeated production
    of an unchanged mission idempotent rather than a new identity each time.
    """

    hexed = _hash_parts(domain, *parts)[:32]
    chars = list(hexed)
    chars[12] = '4'
    chars[16] = '8'
    value = ''.join(chars)
    return (
        f'{value[0:8]}-{value[8:12]}-{value[12:16]}-'
        f'{value[16:20]}-{value[20:32]}'
    )


HEX64 = r'^[0-9a-f]{64}$'
UUID4_PATTERN = (
    r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}'
    r'-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
)


# --- baseline dependency fingerprints (#664) --------------------------------
#
# Every mission requirement declares which exact baseline authority it depends
# on and pins that authority's fingerprint at issue time. Reconciliation later
# recomputes the same projection against the current document and compares —
# one mission-wide stale flag would be wrong, and rebinding by display name is
# forbidden.

MissionDependencyKind = Literal[
    'entity_pose',
    'entity_identity',
    'room_geometry',
    'field_datum',
    'tolerance_policy',
    'measurement_plan',
    'equipment_slot',
]


def entity_pose_fingerprint(entity: SceneEntity) -> str:
    """Hash the pose authority a placement/observation task depends on."""

    return _hash_parts(
        'htdt.capture.mission-dep.entity-pose.v1',
        _canonical_json(
            {
                'entity_id': entity.entity_id,
                'kind': entity.kind,
                'position': entity.position.model_dump(mode='json'),
                'orientation': entity.orientation.model_dump(mode='json'),
                'size_m': (
                    entity.size_m.model_dump(mode='json')
                    if entity.size_m is not None
                    else None
                ),
                'aim_xyz': (
                    entity.aim_xyz.model_dump(mode='json')
                    if entity.aim_xyz is not None
                    else None
                ),
            }
        ),
    )


def entity_identity_fingerprint(entity: SceneEntity) -> str:
    """Hash the slot identity an equipment/identity task depends on.

    Independent of pose so an unrelated position edit does not invalidate
    serial/model acquisition work for the same physical slot.
    """

    return _hash_parts(
        'htdt.capture.mission-dep.entity-identity.v1',
        _canonical_json(
            {
                'entity_id': entity.entity_id,
                'kind': entity.kind,
                'name': entity.name,
                'speaker_role': entity.speaker_role,
            }
        ),
    )


def room_geometry_fingerprint(document: SceneDocument) -> str:
    """Hash the room geometry authority a spatial task depends on."""

    room = document.room.model_dump(mode='json') if document.room else None
    return _hash_parts(
        'htdt.capture.mission-dep.room-geometry.v1',
        _canonical_json({'room': room}),
    )


class MissionTaskDependencyRef(BaseModel):
    """One bounded baseline dependency a mission task pins at issue time."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: MissionDependencyKind
    target_id: str = Field(min_length=1)
    fingerprint: str = Field(pattern=HEX64)


class MissionMeasurementRequest(BaseModel):
    """Typed measurement acquisition requirement (Task Plan v1 refinement).

    ``acquisition`` replaces free-text guessing: ``spatial_required`` means a
    new Capture spatial observation is needed; ``supplied_endpoint`` means the
    mission supplies the exact endpoint/reference and the operator only
    acquires the quantity; ``non_spatial`` needs no spatial authority at all.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    quantity_type: str = Field(min_length=1)
    expected_unit: str | None = Field(default=None, min_length=1)
    endpoint_semantics: str | None = Field(default=None, min_length=1)
    acquisition: Literal[
        'spatial_required',
        'supplied_endpoint',
        'non_spatial',
    ]
    reference_dependencies: tuple[MissionTaskDependencyRef, ...] = ()


MissionTaskKind = Literal[
    'acquire_room_scan',
    'verify_placement',
    'capture_equipment_identity',
    'measure_dimension',
    'inspect_opening',
    'measurement',
    'custom',
]


class MissionTask(BaseModel):
    """One requested field task; never an observation of anything."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    task_id: str = Field(pattern=UUID4_PATTERN)
    kind: MissionTaskKind
    requirement: Literal['required', 'optional'] = 'required'
    title: str = Field(min_length=1)
    target_entity_id: str | None = Field(default=None, min_length=1)
    expected_speaker_role: str | None = Field(default=None, min_length=1)
    measurement: MissionMeasurementRequest | None = None
    dependencies: tuple[MissionTaskDependencyRef, ...] = ()


MissionPurpose = Literal[
    'initial_capture',
    'design_verification',
    'equipment_identity',
    'measurement_campaign',
    'recapture',
    'commissioning',
    'custom',
]


class HTDTCaptureTaskPlan(BaseModel):
    """Versioned Capture-side task plan produced by HTDT (#563)."""

    model_config = ConfigDict(frozen=True, extra='forbid', serialize_by_alias=True)

    schema_: Literal['htdt.capture.task-plan'] = Field(default='htdt.capture.task-plan', alias='schema')
    plan_version: Literal[1] = 1
    plan_id: str = Field(pattern=UUID4_PATTERN)
    plan_sha256: str = Field(pattern=HEX64)
    project: HTDTProjectReference | HTDTLegacyProjectRef
    room_name: str | None = Field(default=None, min_length=1)
    purpose: MissionPurpose
    tasks: tuple[MissionTask, ...]


class MissionBaseline(BaseModel):
    """Exact issuing design authority a mission is generated from (#664)."""

    model_config = ConfigDict(frozen=True, extra='forbid', serialize_by_alias=True)

    schema_: Literal['htdt.capture.mission-baseline'] = Field(default='htdt.capture.mission-baseline', alias='schema')
    baseline_version: Literal[1] = 1
    project: HTDTProjectReference
    document_id: str = Field(min_length=1)
    scene_revision_id: str | None = Field(default=None, min_length=1)
    scene_content_sha256: str = Field(pattern=HEX64)
    system_variant_id: str | None = Field(default=None, min_length=1)
    design_checkpoint: str | None = Field(default=None, min_length=1)


class CaptureMission(BaseModel):
    """One project-specific field mission: plan + issuing baseline."""

    model_config = ConfigDict(frozen=True, extra='forbid', serialize_by_alias=True)

    schema_: Literal['htdt.capture.mission'] = Field(default='htdt.capture.mission', alias='schema')
    mission_version: Literal[1] = 1
    mission_id: str = Field(pattern=UUID4_PATTERN)
    mission_sha256: str = Field(pattern=HEX64)
    plan: HTDTCaptureTaskPlan
    baseline: MissionBaseline
    issued_from: Literal[
        'current_scene',
        'system_variant',
        'design_checkpoint',
        'repair_request',
    ]
    supersedes_mission_id: str | None = Field(
        default=None,
        pattern=UUID4_PATTERN,
    )


class MissionPackageDependency(BaseModel):
    """Bounded dependency descriptor embedded in a mission package."""

    model_config = ConfigDict(frozen=True, extra='forbid', serialize_by_alias=True)

    dependency_id: str = Field(pattern=UUID4_PATTERN)
    kind: Literal[
        'equipment_catalog_snapshot',
        'task_plan',
        'planned_targets',
        'field_datum',
        'tolerance_profile',
        'underlay',
        'repair_request',
    ]
    role: Literal['required', 'optional']
    schema_: str = Field(min_length=1, alias='schema')
    schema_version: int = Field(ge=1)
    payload_sha256: str = Field(pattern=HEX64)
    payload_base64: str

    @field_validator('payload_base64')
    @classmethod
    def validate_payload(cls, value: str) -> str:
        try:
            b64decode(value.encode('ascii'), validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError('dependency payload must be valid Base64') from exc
        return value


class CaptureMissionPackage(BaseModel):
    """One-step versioned mission envelope with bounded dependencies."""

    model_config = ConfigDict(frozen=True, extra='forbid', serialize_by_alias=True)

    schema_: Literal['htdt.capture.mission-package'] = Field(default='htdt.capture.mission-package', alias='schema')
    package_version: Literal[1] = 1
    package_id: str = Field(pattern=UUID4_PATTERN)
    package_sha256: str = Field(pattern=HEX64)
    mission: CaptureMission
    dependencies: tuple[MissionPackageDependency, ...]

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode='json')).encode('utf-8')

    def dependency_payload(self, dependency_id: str) -> bytes:
        for dependency in self.dependencies:
            if dependency.dependency_id == dependency_id:
                payload = b64decode(dependency.payload_base64.encode('ascii'))
                if sha256(payload).hexdigest() != dependency.payload_sha256:
                    raise CaptureMissionError(
                        'mission dependency payload hash mismatch: '
                        f'{dependency_id}'
                    )
                return payload
        raise KeyError(dependency_id)


# --- producers ---------------------------------------------------------------


def _speaker_task(entity: SceneEntity, document_hash: str) -> MissionTask:
    dependencies = [
        MissionTaskDependencyRef(
            kind='entity_identity',
            target_id=entity.entity_id,
            fingerprint=entity_identity_fingerprint(entity),
        ),
        MissionTaskDependencyRef(
            kind='entity_pose',
            target_id=entity.entity_id,
            fingerprint=entity_pose_fingerprint(entity),
        ),
    ]
    role = (
        None
        if is_unassigned_speaker_role(entity.speaker_role)
        else entity.speaker_role
    )
    return MissionTask(
        task_id=_deterministic_uuid(
            'htdt.capture.mission-task.v1',
            document_hash,
            'verify_placement',
            entity.entity_id,
        ),
        kind='verify_placement',
        requirement='required',
        title=f'Verify placement of {entity.name}',
        target_entity_id=entity.entity_id,
        expected_speaker_role=role,
        dependencies=tuple(dependencies),
    )


def _identity_task(entity: SceneEntity, document_hash: str) -> MissionTask:
    return MissionTask(
        task_id=_deterministic_uuid(
            'htdt.capture.mission-task.v1',
            document_hash,
            'capture_equipment_identity',
            entity.entity_id,
        ),
        kind='capture_equipment_identity',
        requirement='required',
        title=f'Capture equipment identity for {entity.name}',
        target_entity_id=entity.entity_id,
        dependencies=(
            MissionTaskDependencyRef(
                kind='entity_identity',
                target_id=entity.entity_id,
                fingerprint=entity_identity_fingerprint(entity),
            ),
        ),
    )


def _measurement_point_task(
    entity: SceneEntity,
    document_hash: str,
) -> MissionTask:
    return MissionTask(
        task_id=_deterministic_uuid(
            'htdt.capture.mission-task.v1',
            document_hash,
            'measurement',
            entity.entity_id,
        ),
        kind='measurement',
        requirement='optional',
        title=f'Acquire measurement at {entity.name}',
        target_entity_id=entity.entity_id,
        measurement=MissionMeasurementRequest(
            quantity_type='spatial_location',
            expected_unit='m',
            acquisition='spatial_required',
        ),
        dependencies=(
            MissionTaskDependencyRef(
                kind='entity_identity',
                target_id=entity.entity_id,
                fingerprint=entity_identity_fingerprint(entity),
            ),
            MissionTaskDependencyRef(
                kind='entity_pose',
                target_id=entity.entity_id,
                fingerprint=entity_pose_fingerprint(entity),
            ),
        ),
    )


def build_capture_task_plan(
    document: SceneDocument,
    *,
    project: HTDTProjectReference | HTDTLegacyProjectRef,
    purpose: MissionPurpose,
    room_name: str | None = None,
    extra_tasks: Sequence[MissionTask] = (),
) -> HTDTCaptureTaskPlan:
    """Derive a deterministic Capture task plan from exact document state.

    Planned/reference values remain design intent: a task asks the field
    operator to verify/acquire, never records anything as observed.
    """

    document_hash = scene_content_hash(document)
    tasks: list[MissionTask] = []
    if document.room is None:
        tasks.append(
            MissionTask(
                task_id=_deterministic_uuid(
                    'htdt.capture.mission-task.v1',
                    document_hash,
                    'acquire_room_scan',
                ),
                kind='acquire_room_scan',
                requirement='required',
                title='Acquire a room scan for this project',
                dependencies=(
                    MissionTaskDependencyRef(
                        kind='room_geometry',
                        target_id=document.document_id,
                        fingerprint=room_geometry_fingerprint(document),
                    ),
                ),
            )
        )
    else:
        tasks.append(
            MissionTask(
                task_id=_deterministic_uuid(
                    'htdt.capture.mission-task.v1',
                    document_hash,
                    'measure_dimension',
                    document.document_id,
                ),
                kind='measure_dimension',
                requirement='optional',
                title='Verify key room dimensions',
                measurement=MissionMeasurementRequest(
                    quantity_type='room_height',
                    expected_unit='m',
                    endpoint_semantics='floor-to-ceiling',
                    acquisition='spatial_required',
                ),
                dependencies=(
                    MissionTaskDependencyRef(
                        kind='room_geometry',
                        target_id=document.document_id,
                        fingerprint=room_geometry_fingerprint(document),
                    ),
                ),
            )
        )

    for entity in sorted(document.entities, key=lambda item: item.entity_id):
        if entity.kind == 'speaker':
            tasks.append(_speaker_task(entity, document_hash))
        elif entity.kind == 'measurement_point':
            tasks.append(_measurement_point_task(entity, document_hash))
        elif entity.kind in {'projector', 'screen', 'av_equipment'}:
            tasks.append(_identity_task(entity, document_hash))
    tasks.extend(extra_tasks)

    task_ids = [task.task_id for task in tasks]
    if len(set(task_ids)) != len(task_ids):
        raise CaptureMissionError('duplicate derived task identity')

    projection = _canonical_json(
        {
            'project': project.model_dump(mode='json'),
            'room_name': room_name,
            'purpose': purpose,
            'tasks': [task.model_dump(mode='json') for task in tasks],
        }
    )
    plan_sha256 = sha256(projection.encode('utf-8')).hexdigest()
    return HTDTCaptureTaskPlan(
        plan_id=_deterministic_uuid(
            'htdt.capture.task-plan.v1',
            plan_sha256,
        ),
        plan_sha256=plan_sha256,
        project=project,
        room_name=room_name,
        purpose=purpose,
        tasks=tuple(tasks),
    )


def build_mission(
    document: SceneDocument,
    *,
    project: HTDTProjectReference,
    purpose: MissionPurpose,
    issued_from: Literal[
        'current_scene', 'system_variant', 'design_checkpoint', 'repair_request'
    ] = 'current_scene',
    room_name: str | None = None,
    scene_revision_id: str | None = None,
    system_variant_id: str | None = None,
    design_checkpoint: str | None = None,
    supersedes_mission_id: str | None = None,
    extra_tasks: Sequence[MissionTask] = (),
) -> CaptureMission:
    """Produce one versioned mission: task plan + issuing baseline."""

    plan = build_capture_task_plan(
        document,
        project=project,
        purpose=purpose,
        room_name=room_name,
        extra_tasks=extra_tasks,
    )
    baseline = MissionBaseline(
        project=project,
        document_id=document.document_id,
        scene_revision_id=scene_revision_id,
        scene_content_sha256=scene_content_hash(document),
        system_variant_id=system_variant_id,
        design_checkpoint=design_checkpoint,
    )
    mission_sha256 = sha256(
        _canonical_json(
            {
                'plan_sha256': plan.plan_sha256,
                'baseline': baseline.model_dump(mode='json'),
                'issued_from': issued_from,
                'supersedes_mission_id': supersedes_mission_id,
            }
        ).encode('utf-8')
    ).hexdigest()
    return CaptureMission(
        mission_id=_deterministic_uuid(
            'htdt.capture.mission.v1',
            mission_sha256,
        ),
        mission_sha256=mission_sha256,
        plan=plan,
        baseline=baseline,
        issued_from=issued_from,
        supersedes_mission_id=supersedes_mission_id,
    )


def build_repair_mission(
    parent: CaptureMission,
    unresolved_task_ids: Sequence[str],
    *,
    document: SceneDocument,
    project: HTDTProjectReference,
    scene_revision_id: str | None = None,
    design_checkpoint: str | None = None,
) -> CaptureMission:
    """Derived follow-up mission requesting only unresolved evidence (#321).

    The parent task objects are reused verbatim so task identities and their
    pinned baseline dependencies survive; the new mission supersedes nothing
    silently — it links the original through ``supersedes_mission_id`` lineage
    metadata rather than rewriting it.
    """

    unresolved = set(unresolved_task_ids)
    if not unresolved:
        raise CaptureMissionError('repair mission requires unresolved task ids')
    parent_ids = {task.task_id for task in parent.plan.tasks}
    missing = unresolved - parent_ids
    if missing:
        raise CaptureMissionError(
            f'repair mission references tasks not in parent: {sorted(missing)}'
        )
    # The repair plan contains ONLY the unresolved parent tasks verbatim —
    # re-deriving tasks from the document would widen a bounded repair back
    # into a full mission.
    tasks = tuple(
        task for task in parent.plan.tasks if task.task_id in unresolved
    )
    projection = _canonical_json(
        {
            'project': project.model_dump(mode='json'),
            'room_name': parent.plan.room_name,
            'purpose': 'recapture',
            'tasks': [task.model_dump(mode='json') for task in tasks],
        }
    )
    plan_sha256 = sha256(projection.encode('utf-8')).hexdigest()
    plan = HTDTCaptureTaskPlan(
        plan_id=_deterministic_uuid(
            'htdt.capture.task-plan.v1',
            plan_sha256,
        ),
        plan_sha256=plan_sha256,
        project=project,
        room_name=parent.plan.room_name,
        purpose='recapture',
        tasks=tasks,
    )
    baseline = MissionBaseline(
        project=project,
        document_id=document.document_id,
        scene_revision_id=scene_revision_id,
        scene_content_sha256=scene_content_hash(document),
        system_variant_id=parent.baseline.system_variant_id,
        design_checkpoint=design_checkpoint,
    )
    mission_sha256 = sha256(
        _canonical_json(
            {
                'plan_sha256': plan.plan_sha256,
                'baseline': baseline.model_dump(mode='json'),
                'issued_from': 'repair_request',
                'supersedes_mission_id': parent.mission_id,
            }
        ).encode('utf-8')
    ).hexdigest()
    return CaptureMission(
        mission_id=_deterministic_uuid(
            'htdt.capture.mission.v1',
            mission_sha256,
        ),
        mission_sha256=mission_sha256,
        plan=plan,
        baseline=baseline,
        issued_from='repair_request',
        supersedes_mission_id=parent.mission_id,
    )


def build_bounded_equipment_snapshot(
    snapshot: EquipmentCatalogSnapshot,
    definition_ids: Sequence[str],
) -> EquipmentCatalogSnapshot:
    """Closure-bounded picker: only the equipment a mission references."""

    wanted = set(definition_ids)
    entries = tuple(
        entry for entry in snapshot.definitions if entry.definition_id in wanted
    )
    missing = wanted - {entry.definition_id for entry in entries}
    if missing:
        raise CaptureMissionError(
            'bounded equipment snapshot references unknown definitions: '
            f'{sorted(missing)}'
        )
    return EquipmentCatalogSnapshot(definitions=entries)


def build_mission_package(
    mission: CaptureMission,
    *,
    equipment_snapshot: EquipmentCatalogSnapshot | None = None,
    extra_dependencies: Sequence[tuple[str, str, int, bytes, str]] = (),
) -> CaptureMissionPackage:
    """Assemble the one-step offline mission envelope.

    ``extra_dependencies`` carries (kind, schema, schema_version, payload,
    role) tuples for bounded references such as a field datum or underlay —
    every entry is hash-pinned and embedded so field use needs no network.
    """

    dependencies: list[MissionPackageDependency] = []

    def _append(
        kind: str,
        schema_name: str,
        schema_version: int,
        payload: bytes,
        role: str,
    ) -> None:
        dependencies.append(
            MissionPackageDependency(
                dependency_id=_deterministic_uuid(
                    'htdt.capture.mission-dependency.v1',
                    mission.mission_sha256,
                    kind,
                    sha256(payload).hexdigest(),
                ),
                kind=kind,  # type: ignore[arg-type]
                role=role,  # type: ignore[arg-type]
                schema=schema_name,
                schema_version=schema_version,
                payload_sha256=sha256(payload).hexdigest(),
                payload_base64=b64encode(payload).decode('ascii'),
            )
        )

    _append(
        'task_plan',
        'htdt.capture.task-plan',
        1,
        _canonical_json(mission.plan.model_dump(mode='json')).encode('utf-8'),
        'required',
    )
    if equipment_snapshot is not None:
        _append(
            'equipment_catalog_snapshot',
            'htdt.equipment.catalog-snapshot',
            1,
            equipment_snapshot.canonical_bytes(),
            'optional',
        )
    for kind, schema_name, schema_version, payload, role in extra_dependencies:
        _append(kind, schema_name, schema_version, payload, role)

    projection = _canonical_json(
        {
            'mission_sha256': mission.mission_sha256,
            'dependencies': [
                {
                    'kind': item.kind,
                    'role': item.role,
                    'schema': item.schema_,
                    'schema_version': item.schema_version,
                    'payload_sha256': item.payload_sha256,
                }
                for item in dependencies
            ],
        }
    )
    package_sha256 = sha256(projection.encode('utf-8')).hexdigest()
    return CaptureMissionPackage(
        package_id=_deterministic_uuid(
            'htdt.capture.mission-package.v1',
            package_sha256,
        ),
        package_sha256=package_sha256,
        mission=mission,
        dependencies=tuple(dependencies),
    )


def decode_mission_package(payload: bytes | str) -> CaptureMissionPackage:
    """Decode and revalidate a mission package; malformed input fails closed."""

    if isinstance(payload, bytes):
        try:
            text = payload.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise CaptureMissionError(
                'mission package payload is not UTF-8 JSON'
            ) from exc
    else:
        text = payload
    try:
        package = CaptureMissionPackage.model_validate_json(text)
    except ValueError as exc:
        raise CaptureMissionError(f'invalid mission package: {exc}') from exc
    recomputed = sha256(
        _canonical_json(
            {
                'mission_sha256': package.mission.mission_sha256,
                'dependencies': [
                    {
                        'kind': item.kind,
                        'role': item.role,
                        'schema': item.schema_,
                        'schema_version': item.schema_version,
                        'payload_sha256': item.payload_sha256,
                    }
                    for item in package.dependencies
                ],
            }
        ).encode('utf-8')
    ).hexdigest()
    if package.package_sha256 != recomputed:
        raise CaptureMissionError(
            'mission package identity does not reproduce its recorded hash'
        )
    for dependency in package.dependencies:
        package.dependency_payload(dependency.dependency_id)
    return package


def mission_equipment_definition_ids(
    mission: CaptureMission,
) -> tuple[str, ...]:
    """Definition ids a mission explicitly references (bounded closure aid)."""

    ids: list[str] = []
    for task in mission.plan.tasks:
        for dependency in task.dependencies:
            if dependency.kind == 'equipment_slot':
                ids.append(dependency.target_id)
    return tuple(sorted(set(ids)))
