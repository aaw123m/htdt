from __future__ import annotations

from base64 import b64encode
from hashlib import sha256
import json
import uuid

import pytest

from htdt.cad_equipment_catalog import EquipmentCatalogSnapshot
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.capture_mission import (
    CaptureMissionError,
    HTDTCaptureTaskPlan,
    MissionPackageDependency,
    MissionTaskDependencyRef,
    build_bounded_equipment_snapshot,
    build_capture_task_plan,
    build_mission,
    build_mission_package,
    build_repair_mission,
    decode_mission_package,
    entity_pose_fingerprint,
    mission_equipment_definition_ids,
    mission_package_descriptor,
    mission_package_wire_payload,
)
from htdt.project_identity import new_project_reference


def _document(
    *,
    room: RoomPrism | None = None,
    entities: tuple[SceneEntity, ...] = (),
) -> SceneDocument:
    return SceneDocument(
        document_id='mission-doc-1',
        room=room,
        entities=entities,
    )


def _speaker(entity_id: str, role: str, x: float) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name=entity_id.upper(),
        speaker_role=role,
        position=Position3(x_m=x, y_m=1.0, z_m=1.0),
        size_m=Size3(x_m=0.2, y_m=0.3, z_m=0.4),
    )


def _measurement_point(entity_id: str, x: float) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='measurement_point',
        name=entity_id.upper(),
        position=Position3(x_m=x, y_m=2.0, z_m=1.2),
    )


def test_no_room_document_produces_required_room_scan_task() -> None:
    plan = build_capture_task_plan(
        _document(),
        project=new_project_reference(document_id='mission-doc-1'),
        purpose='initial_capture',
    )
    assert plan.schema == 'htdt.capture.task-plan'
    kinds = [task.kind for task in plan.tasks]
    assert kinds == ['acquire_room_scan']
    task = plan.tasks[0]
    assert task.requirement == 'required'
    assert task.title
    assert uuid.UUID(task.task_id).version == 4
    assert task.dependencies[0].kind == 'room_geometry'


def test_room_document_produces_verify_and_measure_tasks() -> None:
    document = _document(
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
        entities=(
            _speaker('spk-fl', 'FL', 1.0),
            _speaker('spk-fr', 'FR', 3.0),
            _measurement_point('mp-1', 2.0),
            SceneEntity(
                entity_id='av-1',
                kind='av_equipment',
                name='AVR',
                position=Position3(x_m=0.5, y_m=0.5, z_m=0.5),
                size_m=Size3(x_m=0.4, y_m=0.2, z_m=0.35),
            ),
        ),
    )
    plan = build_capture_task_plan(
        document,
        project=new_project_reference(document_id='mission-doc-1'),
        purpose='design_verification',
    )
    kinds = sorted(task.kind for task in plan.tasks)
    assert 'measure_dimension' in kinds
    assert kinds.count('verify_placement') == 2
    assert 'measurement' in kinds
    assert 'capture_equipment_identity' in kinds

    speaker_task = next(
        task for task in plan.tasks if task.target_entity_id == 'spk-fl'
    )
    assert speaker_task.expected_speaker_role == 'FL'
    dep_kinds = {dep.kind for dep in speaker_task.dependencies}
    assert 'entity_pose' in dep_kinds
    assert 'entity_identity' in dep_kinds

    measure_task = next(
        task for task in plan.tasks if task.kind == 'measure_dimension'
    )
    assert measure_task.measurement is not None
    assert measure_task.measurement.acquisition == 'spatial_required'


def test_plan_is_deterministic_for_identical_inputs() -> None:
    document = _document(
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
        entities=(_speaker('spk-fl', 'FL', 1.0),),
    )
    project = new_project_reference(document_id='mission-doc-1')
    first = build_capture_task_plan(
        document, project=project, purpose='initial_capture'
    )
    second = build_capture_task_plan(
        document, project=project, purpose='initial_capture'
    )
    assert first == second
    assert first.plan_sha256 == second.plan_sha256
    assert first.plan_id == second.plan_id


def test_plan_identity_changes_when_document_changes() -> None:
    project = new_project_reference(document_id='mission-doc-1')
    first = build_capture_task_plan(
        _document(
            room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
            entities=(_speaker('spk-fl', 'FL', 1.0),),
        ),
        project=project,
        purpose='initial_capture',
    )
    second = build_capture_task_plan(
        _document(
            room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
            entities=(_speaker('spk-fl', 'FL', 2.0),),
        ),
        project=project,
        purpose='initial_capture',
    )
    assert first.plan_sha256 != second.plan_sha256
    assert first.plan_id != second.plan_id


def test_mission_baseline_pins_issuing_document_state() -> None:
    document = _document(
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
        entities=(_speaker('spk-fl', 'FL', 1.0),),
    )
    project = new_project_reference(document_id='mission-doc-1')
    mission = build_mission(
        document,
        project=project,
        purpose='design_verification',
        scene_revision_id='rev-42',
        design_checkpoint='cp-9',
    )
    assert mission.mission_sha256
    assert mission.baseline.document_id == 'mission-doc-1'
    assert mission.baseline.scene_revision_id == 'rev-42'
    assert mission.baseline.design_checkpoint == 'cp-9'
    assert len(mission.baseline.scene_content_sha256) == 64
    assert mission.issued_from == 'current_scene'
    assert mission.supersedes_mission_id is None
    assert uuid.UUID(mission.mission_id).version == 4


def test_repair_mission_reuses_parent_tasks_and_links_lineage() -> None:
    document = _document(
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
        entities=(
            _speaker('spk-fl', 'FL', 1.0),
            _speaker('spk-fr', 'FR', 3.0),
        ),
    )
    project = new_project_reference(document_id='mission-doc-1')
    parent = build_mission(document, project=project, purpose='initial_capture')
    task_ids = [task.task_id for task in parent.plan.tasks]
    repair = build_repair_mission(
        parent,
        [task_ids[0]],
        document=document,
        project=project,
    )
    assert repair.issued_from == 'repair_request'
    assert repair.plan.purpose == 'recapture'
    assert repair.supersedes_mission_id == parent.mission_id
    assert len(repair.plan.tasks) == 1
    assert repair.plan.tasks[0].task_id == task_ids[0]

    with pytest.raises(CaptureMissionError):
        build_repair_mission(
            parent, [], document=document, project=project
        )
    with pytest.raises(CaptureMissionError):
        build_repair_mission(
            parent, ['not-a-task'], document=document, project=project
        )


def test_mission_package_embeds_hash_pinned_dependencies() -> None:
    document = _document(
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
        entities=(_speaker('spk-fl', 'FL', 1.0),),
    )
    project = new_project_reference(document_id='mission-doc-1')
    mission = build_mission(document, project=project, purpose='initial_capture')
    package = build_mission_package(
        mission,
        extra_dependencies=(
            (
                'field_datum',
                'htdt.capture.field-datum',
                1,
                b'{"datum":true}',
                'optional',
            ),
        ),
    )
    assert package.mission == mission
    kinds = {dep.kind for dep in package.dependencies}
    assert kinds == {'task_plan', 'field_datum'}
    task_plan_dep = next(
        dep for dep in package.dependencies if dep.kind == 'task_plan'
    )
    assert task_plan_dep.role == 'required'
    payload = package.dependency_payload(task_plan_dep.dependency_id)
    decoded = HTDTCaptureTaskPlan.model_validate(json.loads(payload))
    assert decoded == mission.plan

    # Re-decode round-trips with identity + payload-hash revalidation.
    reopened = decode_mission_package(package.model_dump_json())
    assert reopened.package_sha256 == package.package_sha256
    assert reopened == package

    tampered = package.model_copy(
        update={'package_sha256': '0' * 64}
    )
    with pytest.raises(CaptureMissionError):
        decode_mission_package(tampered.model_dump_json())


def _mission_wire(**tweaks) -> str:
    document = _document(
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
        entities=(_speaker('spk-fl', 'FL', 1.0),),
    )
    project = new_project_reference(document_id='mission-doc-1')
    mission = build_mission(
        document, project=project, purpose='initial_capture'
    )
    package = build_mission_package(mission)
    wire = json.loads(package.model_dump_json())
    for dotted, value in tweaks.items():
        keys = dotted.split('__')
        node = wire
        for key in keys[:-1]:
            node = node[int(key)] if key.isdigit() else node[key]
        node[keys[-1]] = value
    return json.dumps(wire)


def test_decode_revalidates_plan_and_mission_hashes() -> None:
    for payload in (
        # Plan content changed while plan_sha256 stayed recorded.
        _mission_wire(mission__plan__room_name='forged room'),
        _mission_wire(mission__plan__purpose='recapture'),
        # Plan hash link broken.
        _mission_wire(mission__plan__plan_sha256='0' * 64),
        # Baseline / lineage content changed under a stale mission_sha256.
        _mission_wire(mission__baseline__document_id='mission-doc-2'),
        _mission_wire(mission__baseline__scene_revision_id='rev-99'),
        _mission_wire(mission__issued_from='repair_request'),
        _mission_wire(
            mission__supersedes_mission_id=(
                '00000000-0000-4000-8000-000000000000'
            )
        ),
    ):
        with pytest.raises(CaptureMissionError):
            decode_mission_package(payload)


def test_decode_revalidates_derived_identities() -> None:
    for payload in (
        _mission_wire(
            mission__plan__plan_id=(
                '00000000-0000-4000-8000-000000000000'
            )
        ),
        _mission_wire(
            mission__mission_id='00000000-0000-4000-8000-000000000000'
        ),
        _mission_wire(
            package_id='00000000-0000-4000-8000-000000000000'
        ),
        _mission_wire(
            dependencies__0__dependency_id=(
                '00000000-0000-4000-8000-000000000000'
            )
        ),
    ):
        with pytest.raises(CaptureMissionError):
            decode_mission_package(payload)


def test_bounded_equipment_snapshot_requires_known_definitions() -> None:
    from htdt.cad_equipment_catalog import EquipmentCatalogSnapshot

    snapshot = EquipmentCatalogSnapshot(definitions=())
    with pytest.raises(CaptureMissionError):
        build_bounded_equipment_snapshot(snapshot, ['missing-def'])


def test_entity_pose_fingerprint_changes_with_position() -> None:
    before = _speaker('spk-fl', 'FL', 1.0)
    after = _speaker('spk-fl', 'FL', 1.5)
    assert entity_pose_fingerprint(before) != entity_pose_fingerprint(after)


def test_mission_equipment_definition_ids_collects_slot_refs() -> None:
    document = _document(
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
        entities=(),
    )
    project = new_project_reference(document_id='mission-doc-1')
    mission = build_mission(document, project=project, purpose='initial_capture')
    # No equipment_slot deps derived from bare document state.
    assert mission_equipment_definition_ids(mission) == ()


def _wire_package(**mission_kwargs):
    document = _document(
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
        entities=(
            _speaker('spk-fl', 'FL', 1.0),
            _measurement_point('mp-1', 2.0),
            SceneEntity(
                entity_id='av-1',
                kind='av_equipment',
                name='AVR',
                position=Position3(x_m=0.5, y_m=0.5, z_m=0.5),
                size_m=Size3(x_m=0.4, y_m=0.2, z_m=0.35),
            ),
        ),
    )
    project = new_project_reference(document_id='mission-doc-1')
    mission = build_mission(
        document,
        project=project,
        purpose='initial_capture',
        room_name='Theater A',
        **mission_kwargs,
    )
    return build_mission_package(mission)


def test_wire_payload_emits_app_envelope_and_plan() -> None:
    package = _wire_package()
    payload = json.loads(mission_package_wire_payload(package))
    assert payload['schema'] == 'htdt.capture-mission'
    assert payload['schema_version'] == '1.0.0'
    assert payload['mission_id'] == package.mission.mission_id
    assert payload['mission_kind'] == 'initial_survey'
    assert payload['purpose'] == 'initial_capture'
    assert 'supersedes_mission_id' not in payload

    plan = payload['plan']
    assert plan['schema'] == 'htdt.capture-task-plan'
    assert plan['schema_version'] == '2.0.0'
    assert plan['plan_id'] == package.mission.plan.plan_id
    assert plan['plan_version'] == '1.0.0'
    assert plan['project_ref'] == package.mission.plan.project.project_id
    assert plan['room_name'] == 'Theater A'

    semantic_kinds = {
        item['semantic_kind'] for item in plan['semantic_tasks']
    }
    assert 'speaker_installation' in semantic_kinds
    assert 'inventory_item' in semantic_kinds
    # Task ids stay the app-side item ids; target refs land on
    # planned_ref, never target_ref (the app-side annotation identity
    # does not exist until the capture is made).
    placement = next(
        item for item in plan['semantic_tasks']
        if item['semantic_kind'] == 'speaker_installation'
    )
    assert placement['planned_ref'] == 'spk-fl'
    assert 'target_ref' not in placement
    # 'FL' is not an app-standard channel role — preserved losslessly
    # as a custom token rather than rewritten to a lookalike.
    assert plan['expected_channel_roles'] == ['X_FL']

    measurement = next(
        item for item in plan['measurement_requests']
        if item['quantity_type'] == 'spatial_location'
    )
    assert measurement['acquisition_requirement'] == (
        'spatial_point_required'
    )
    assert plan['entity_checklist'] == []
    assert plan['evidence_tasks'] == []

    requirement = payload['receiver_requirement']
    # Semantic tasks produce 'annotations'-family records (entities +
    # authorities payloads); measurement tasks produce 'measurements'.
    assert requirement['required_authority_families'] == [
        'annotations',
        'measurements',
    ]
    assert set(requirement['required_payload_schemas']) == {
        'htdt.capture.entities',
        'htdt.capture.authorities',
        'htdt.capture.measurements',
    }
    assert requirement['require_mission_receipts'] is True
    assert requirement['destination_project_ref'] == plan['project_ref']

    # The listing descriptor mirrors the same projection.
    descriptor = mission_package_descriptor(package)
    assert descriptor['mission_id'] == package.mission.mission_id
    assert descriptor['project_ref'] == plan['project_ref']
    assert descriptor['room_label'] == 'Theater A'
    assert descriptor['required_schema_version'] == '1.0.0'
    assert descriptor['receiver_requirement'] == requirement


def test_wire_payload_repair_mission_links_supersession() -> None:
    package = _wire_package()
    repair = build_repair_mission(
        package.mission,
        [package.mission.plan.tasks[0].task_id],
        document=_document(
            room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
            entities=(_speaker('spk-fl', 'FL', 1.0),),
        ),
        project=package.mission.plan.project,
    )
    repair_package = build_mission_package(repair)
    payload = json.loads(mission_package_wire_payload(repair_package))
    assert payload['mission_kind'] == 'repair'
    assert payload['supersedes_mission_id'] == package.mission.mission_id
    descriptor = mission_package_descriptor(repair_package)
    assert descriptor['supersedes_package_id'] == (
        package.mission.mission_id
    )


def test_wire_payload_fails_closed_on_unexpressible_content() -> None:
    # 'custom' tasks have no faithful app-plan expression.
    package = _wire_package()
    from htdt.capture_mission import MissionTask

    custom = MissionTask(
        task_id=str(uuid.uuid4()),
        kind='custom',
        title='Do the unusual thing',
    )
    tweaked = package.mission.model_copy(
        update={
            'plan': package.mission.plan.model_copy(
                update={
                    'tasks': package.mission.plan.tasks + (custom,)
                }
            )
        }
    )
    tampered = package.model_copy(update={'mission': tweaked})
    with pytest.raises(CaptureMissionError, match='custom'):
        mission_package_wire_payload(tampered)

    # Payload-only dependencies (underlay/datum/repair-request) cannot
    # be smuggled into the app envelope.
    with_datum = _wire_package()
    from htdt.capture_mission import MissionPackageDependency

    dep = MissionPackageDependency(
        dependency_id=str(uuid.uuid4()),
        kind='underlay',
        role='optional',
        schema='htdt.capture.underlay',
        schema_version=1,
        payload_sha256='0' * 64,
        payload_base64='e30=',
    )
    tampered2 = with_datum.model_copy(
        update={'dependencies': with_datum.dependencies + (dep,)}
    )
    with pytest.raises(CaptureMissionError, match='underlay'):
        mission_package_wire_payload(tampered2)


def test_wire_payload_embeds_equipment_catalog() -> None:
    document = _document(
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
        entities=(_speaker('spk-fl', 'FL', 1.0),),
    )
    project = new_project_reference(document_id='mission-doc-1')
    mission = build_mission(
        document,
        project=project,
        purpose='initial_capture',
        room_name='Theater A',
    )
    snapshot = EquipmentCatalogSnapshot(definitions=())
    package = build_mission_package(
        mission, equipment_snapshot=snapshot
    )
    payload = json.loads(mission_package_wire_payload(package))
    # The app's snapshot decoder reads camelCase header keys over
    # snake_case entries — verify the emitted shape decodes.
    catalog = payload['plan']['equipment_catalog']
    assert catalog == {
        'schema': 'htdt.equipment.catalog-snapshot',
        'schemaVersion': 1,
        'authorityVersion': 'o100c-equipment-definition-1',
        'definitions': [],
    }

    # An undecodable catalog payload fails closed, not with a raw
    # pydantic/codec exception.
    dep = MissionPackageDependency(
        dependency_id='d06c693e-7d70-4d6c-b641-34d2e27e6c29',
        kind='equipment_catalog_snapshot',
        role='optional',
        schema='htdt.equipment.catalog-snapshot',
        schema_version=1,
        payload_sha256=sha256(b'{"schema":').hexdigest(),
        payload_base64=b64encode(b'{"schema":').decode('ascii'),
    )
    tampered = package.model_copy(
        update={'dependencies': package.dependencies + (dep,)}
    )
    with pytest.raises(CaptureMissionError, match='catalog'):
        mission_package_wire_payload(tampered)


def test_wire_payload_rejects_duplicate_task_identity() -> None:
    package = _wire_package()
    tasks = package.mission.plan.tasks
    assert len(tasks) > 1
    tweaked = package.mission.model_copy(
        update={
            'plan': package.mission.plan.model_copy(
                update={'tasks': (tasks[0], tasks[0], *tasks[2:])}
            )
        }
    )
    tampered = package.model_copy(update={'mission': tweaked})
    with pytest.raises(CaptureMissionError, match='duplicate task'):
        mission_package_wire_payload(tampered)


def test_wire_payload_requires_a_room_name() -> None:
    document = _document(
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
    )
    project = new_project_reference(document_id='mission-doc-1')
    mission = build_mission(
        document, project=project, purpose='initial_capture'
    )
    package = build_mission_package(mission)
    with pytest.raises(CaptureMissionError, match='room'):
        mission_package_wire_payload(package)


def test_wire_payload_rejects_unknown_measurement_unit() -> None:
    package = _wire_package()
    tasks = []
    for task in package.mission.plan.tasks:
        if task.measurement is not None:
            task = task.model_copy(
                update={
                    'measurement': task.measurement.model_copy(
                        update={'expected_unit': 'fathoms'}
                    )
                }
            )
        tasks.append(task)
    tweaked = package.mission.model_copy(
        update={
            'plan': package.mission.plan.model_copy(
                update={'tasks': tuple(tasks)}
            )
        }
    )
    tampered = package.model_copy(update={'mission': tweaked})
    with pytest.raises(CaptureMissionError, match='fathoms'):
        mission_package_wire_payload(tampered)
