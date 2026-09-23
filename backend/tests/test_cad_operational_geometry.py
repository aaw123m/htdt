"""Issue #651 regression tests: operational geometry and swept clearance.

Doors swing, recliners extend, racks slide out — persisted per-entity
operational zones evaluated against room/entity footprints.
"""

from __future__ import annotations

import json
import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from pydantic import ValidationError

from htdt.cad_operational_geometry import (
    operational_clearance_conflicts,
    operational_swept_footprint,
    operational_zone_footprint,
)
from htdt.cad_scene import (
    OperationalZone,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)


def _entity(entity_id: str, *, x: float, y: float, zones=()) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='furniture',
        name=entity_id,
        position=Position3(x_m=x, y_m=y, z_m=0.5),
        size_m=Size3(x_m=0.6, y_m=0.6, z_m=1.0),
        operational_zones=zones or None,
    )


def _scene(*entities: SceneEntity) -> SceneDocument:
    return SceneDocument(
        document_id='ops-fixture',
        room=RoomPrism(width_m=10.0, depth_m=8.0, height_m=3.0),
        entities=entities,
    )


def test_zone_per_kind_validation() -> None:
    with pytest.raises(ValidationError, match='hinge_offset_m'):
        OperationalZone(zone_id='z', kind='door_swing')
    with pytest.raises(ValidationError, match='unit length'):
        OperationalZone(
            zone_id='z', kind='recline',
            direction=(2.0, 0.0), distance_m=0.4,
        )
    with pytest.raises(ValidationError, match='direction and distance_m'):
        OperationalZone(zone_id='z', kind='recline')
    with pytest.raises(ValidationError, match='radius_m'):
        OperationalZone(zone_id='z', kind='rotate', angle_deg=180.0)


def test_door_swing_footprint_sector() -> None:
    door = _entity(
        'door', x=1.0, y=1.0,
        zones=(OperationalZone(
            zone_id='swing', kind='door_swing',
            hinge_offset_m=(0.3, 0.0), angle_deg=90.0, radius_m=0.8,
        ),),
    )
    footprint = operational_zone_footprint(door, door.operational_zones[0])
    # Quarter circle r=0.8 → ~pi*0.64/4 = 0.5
    assert footprint.area == pytest.approx(math.pi * 0.64 / 4, rel=0.15)


def test_recline_corridor_footprint() -> None:
    seat = _entity(
        'recliner', x=4.0, y=4.0,
        zones=(OperationalZone(
            zone_id='recline', kind='recline',
            direction=(0.0, -1.0), distance_m=0.5,
        ),),
    )
    footprint = operational_zone_footprint(seat, seat.operational_zones[0])
    # 0.6-wide box swept 0.5 forward → corridor area ≥ 0.6*0.5 + body 0.36.
    assert footprint.area >= 0.6 * 0.5
    swept = operational_swept_footprint(seat)
    assert swept is not None and swept.equals(footprint)
    plain = _entity('plain', x=1.0, y=1.0)
    assert operational_swept_footprint(plain) is None


def test_clearance_conflicts_detect_intersections() -> None:
    # Zone sweeps forward (−Y) onto a table placed in its corridor.
    cabinet = _entity(
        'cabinet', x=2.0, y=5.0,
        zones=(OperationalZone(
            zone_id='slide', kind='slide_out',
            direction=(0.0, -1.0), distance_m=2.0,
        ),),
    )
    blocker = _entity('blocker', x=2.0, y=3.6)
    clear = _entity('clear', x=8.0, y=5.0)
    document = _scene(cabinet, blocker, clear)
    conflicts = operational_clearance_conflicts(document)
    assert any(
        c.conflict_kind == 'intersects_entity' and c.other_entity_id == 'blocker'
        for c in conflicts
    )
    assert not any(c.other_entity_id == 'clear' for c in conflicts)


def test_clearance_conflict_leaves_room() -> None:
    near_edge = _entity(
        'edge', x=0.4, y=0.4,
        zones=(OperationalZone(
            zone_id='swing', kind='door_swing',
            hinge_offset_m=(0.0, 0.0), angle_deg=180.0, radius_m=1.5,
        ),),
    )
    document = _scene(near_edge)
    conflicts = operational_clearance_conflicts(document)
    assert any(c.conflict_kind == 'leaves_room' for c in conflicts)


def test_clearance_conflicts_fail_closed_on_unknown() -> None:
    document = _scene(_entity('a', x=1.0, y=1.0))
    with pytest.raises(ValueError, match='unknown entities'):
        operational_clearance_conflicts(document, entity_ids={'ghost'})


def test_operational_zones_persist_and_omit() -> None:
    entity = _entity(
        'door', x=1.0, y=1.0,
        zones=(OperationalZone(
            zone_id='swing', kind='door_swing',
            hinge_offset_m=(0.3, 0.0), angle_deg=90.0, radius_m=0.8,
        ),),
    )
    document = _scene(entity)
    payload = json.loads(document.model_dump_json())
    assert payload['entities'][0]['operational_zones'][0]['kind'] == 'door_swing'
    reopened = SceneDocument.model_validate(payload)
    assert reopened.entity('door').operational_zones == entity.operational_zones
    with pytest.raises(ValidationError, match='duplicate operational zone'):
        _entity('dup', x=0, y=0, zones=(
            OperationalZone(zone_id='z', kind='recline',
                            direction=(1.0, 0.0), distance_m=0.3),
            OperationalZone(zone_id='z', kind='recline',
                            direction=(1.0, 0.0), distance_m=0.5),
        ))
    with pytest.raises(ValidationError, match='physical entity kinds'):
        SceneEntity(
            entity_id='mp', kind='measurement_point', name='MP',
            position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
            operational_zones=(OperationalZone(
                zone_id='z', kind='recline',
                direction=(1.0, 0.0), distance_m=0.3,
            ),),
        )
