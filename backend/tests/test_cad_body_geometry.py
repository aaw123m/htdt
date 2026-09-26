"""Issue #464 regression tests: rich SceneEntity body geometry.

Covers the four authored body kinds (box/cylinder/extruded_polygon/mesh_asset)
across model validation, canonical-hash compatibility, exact-vs-envelope
footprints, collision authority, snapping, repository/blob persistence,
viewport mesh construction, inspector editing, and installation reporting.
"""

from __future__ import annotations

import json
import math
import os
from hashlib import sha256
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pydantic import ValidationError

from htdt.cad_orientation_constraints import (
    entity_collision_geometry_authority,
    entity_exact_body_footprint,
    entity_horizontal_footprint,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    BodyMeshAsset,
    Direction3,
    BodyMeshTriangle,
    BodyMeshVertex,
    EntityBodyGeometry,
    F1_DOCUMENT_ID,
    FootprintVertex,
    Offset3,
    Position3,
    Quaternion4,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    canonical_scene_json,
    make_f1_scene,
    quaternion_from_euler_deg,
    scene_content_hash,
)
from htdt.cad_snap import generate_snap_candidates
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    ProposedEntitySpec,
    build_system_variant,
)
from htdt.cad_video_geometry import (
    AngleRange,
    AspectRatio,
    LensShiftRange,
    ProjectorSpecEvidenceRef,
    ProjectorSpecificationProvenance,
    ScreenGeometryBinding,
    SeatGeometryBinding,
    SightlineSample,
    VideoGeometryPolicy,
    build_projector_specification,
    build_video_geometry_request,
    evaluate_video_geometry,
)
from htdt.raw_mesh import import_raw_visual_mesh
from htdt.report import (
    _installation_entity,
    build_installation_output,
    render_installation_csv,
)


DOCUMENT_ID = 'body-geometry-fixture'
NOW = '2026-10-01T12:00:00+00:00'

_SIMPLE_OBJ = b"""# unit tetra body
v 0.0 0.0 0.0
v 0.4 0.0 0.0
v 0.0 0.3 0.0
v 0.0 0.0 0.2
f 1 2 3
f 1 2 4
f 1 3 4
f 2 3 4
"""

_L_FOOTPRINT = (
    FootprintVertex(x_m=-0.5, y_m=-0.5),
    FootprintVertex(x_m=0.5, y_m=-0.5),
    FootprintVertex(x_m=0.5, y_m=0.0),
    FootprintVertex(x_m=-0.1, y_m=0.0),
    FootprintVertex(x_m=-0.1, y_m=0.5),
    FootprintVertex(x_m=-0.5, y_m=0.5),
)


def _mesh_asset(**overrides) -> BodyMeshAsset:
    imported = import_raw_visual_mesh(_SIMPLE_OBJ, source_name='tetra.obj')
    payload = {
        'asset_sha256': imported.provenance.original_asset_sha256,
        'source_name': imported.provenance.source_name,
        'asset_format': imported.provenance.asset_format,
        'original_size_bytes': imported.provenance.original_size_bytes,
        'vertices': tuple(
            BodyMeshVertex(x_m=v.x, y_m=v.y, z_m=v.z) for v in imported.vertices
        ),
        'triangles': tuple(
            BodyMeshTriangle(a=t.a, b=t.b, c=t.c) for t in imported.triangles
        ),
    }
    payload.update(overrides)
    return BodyMeshAsset(**payload)


def _furniture(
    entity_id: str = 'table',
    *,
    x_m: float = 2.0,
    y_m: float = 2.0,
    size: Size3 | None = None,
    body: EntityBodyGeometry | None = None,
    orientation: Quaternion4 | None = None,
) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='furniture',
        name=entity_id,
        position=Position3(x_m=x_m, y_m=y_m, z_m=0.4),
        orientation=orientation or Quaternion4(),
        size_m=size or Size3(x_m=1.0, y_m=1.0, z_m=0.8),
        body_geometry=body,
    )


def _scene(*entities: SceneEntity) -> SceneDocument:
    return SceneDocument(
        document_id=DOCUMENT_ID,
        room=RoomPrism(width_m=8.0, depth_m=6.0, height_m=3.0),
        entities=entities,
    )


# --- Model validation --------------------------------------------------------


def test_legacy_scene_serializes_without_body_geometry_and_keeps_hash() -> None:
    document = make_f1_scene()
    canonical = canonical_scene_json(document)
    assert 'body_geometry' not in canonical
    round_tripped = SceneDocument.model_validate(json.loads(canonical))
    assert scene_content_hash(round_tripped) == scene_content_hash(document)


def test_body_geometry_round_trips_through_canonical_json() -> None:
    entity = _furniture(
        body=EntityBodyGeometry(kind='cylinder', radius_m=0.4),
    )
    document = _scene(entity)
    payload = json.loads(canonical_scene_json(document))
    body = payload['entities'][0]['body_geometry']
    assert body['kind'] == 'cylinder'
    assert body['radius_m'] == 0.4
    reopened = SceneDocument.model_validate(payload)
    assert reopened.entity('table').body_geometry == entity.body_geometry
    assert scene_content_hash(reopened) == scene_content_hash(document)


def test_cylinder_requires_radius_within_envelope() -> None:
    with pytest.raises(ValidationError, match='radius_m'):
        EntityBodyGeometry(kind='cylinder')
    with pytest.raises(ValidationError, match='bounding envelope'):
        _furniture(body=EntityBodyGeometry(kind='cylinder', radius_m=0.51))
    with pytest.raises(ValidationError, match='no additional parameters'):
        EntityBodyGeometry(kind='box', radius_m=0.2)


def test_polygon_footprint_validates_shape_and_envelope() -> None:
    with pytest.raises(ValidationError, match='footprint_vertices'):
        EntityBodyGeometry(kind='extruded_polygon')
    with pytest.raises(ValidationError, match='at least three'):
        EntityBodyGeometry(
            kind='extruded_polygon',
            footprint_vertices=_L_FOOTPRINT[:2],
        )
    # Bow-tie ordering is self-intersecting.
    with pytest.raises(ValidationError, match='Invalid entity footprint'):
        EntityBodyGeometry(
            kind='extruded_polygon',
            footprint_vertices=(
                FootprintVertex(x_m=-0.4, y_m=-0.4),
                FootprintVertex(x_m=0.4, y_m=0.4),
                FootprintVertex(x_m=-0.4, y_m=0.4),
                FootprintVertex(x_m=0.4, y_m=-0.4),
            ),
        )
    with pytest.raises(ValidationError, match='outside the size_m bounding envelope'):
        _furniture(
            body=EntityBodyGeometry(
                kind='extruded_polygon',
                footprint_vertices=(
                    FootprintVertex(x_m=-0.6, y_m=-0.5),
                    FootprintVertex(x_m=0.5, y_m=-0.5),
                    FootprintVertex(x_m=0.5, y_m=0.5),
                    FootprintVertex(x_m=-0.5, y_m=0.5),
                ),
            ),
        )


def test_mesh_asset_requires_mesh_and_valid_triangles() -> None:
    with pytest.raises(ValidationError, match='mesh'):
        EntityBodyGeometry(kind='mesh_asset')
    with pytest.raises(ValidationError, match='unknown vertex'):
        _mesh_asset(
            triangles=(BodyMeshTriangle(a=0, b=1, c=99),),
        )
    with pytest.raises(ValidationError, match='only accepts mesh'):
        EntityBodyGeometry(
            kind='mesh_asset',
            mesh=_mesh_asset(),
            radius_m=0.2,
        )


def test_body_geometry_rejected_for_measurement_point() -> None:
    with pytest.raises(ValidationError, match='physical entity kinds'):
        SceneEntity(
            entity_id='point-x',
            kind='measurement_point',
            name='X',
            position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
            body_geometry=EntityBodyGeometry(kind='box'),
        )


def test_mesh_asset_local_transform_and_provenance() -> None:
    mesh = _mesh_asset(uniform_scale=0.5, local_offset_m=Offset3(x_m=0.1))
    assert mesh.uniform_scale == 0.5
    assert mesh.local_offset_m.x_m == 0.1
    assert mesh.asset_sha256 == sha256(_SIMPLE_OBJ).hexdigest()
    assert mesh.original_size_bytes == len(_SIMPLE_OBJ)
    assert mesh.source_name == 'tetra.obj'


# --- Footprint / collision authority ----------------------------------------


def test_cylinder_and_polygon_have_exact_footprints() -> None:
    cylinder = _furniture(body=EntityBodyGeometry(kind='cylinder', radius_m=0.4))
    circle = entity_exact_body_footprint(cylinder)
    assert circle is not None
    assert circle.area == pytest.approx(math.pi * 0.16, rel=0.02)
    centroid = circle.centroid
    assert centroid.x == pytest.approx(2.0)
    assert centroid.y == pytest.approx(2.0)
    assert entity_collision_geometry_authority(cylinder) == 'exact_body_geometry'

    l_sofa = _furniture(body=EntityBodyGeometry(
        kind='extruded_polygon',
        footprint_vertices=_L_FOOTPRINT,
    ))
    polygon = entity_exact_body_footprint(l_sofa)
    assert polygon is not None
    # 1.0x1.0 envelope minus the missing 0.6x0.5 corner.
    assert polygon.area == pytest.approx(0.7)
    assert entity_collision_geometry_authority(l_sofa) == 'exact_body_geometry'


def test_tilted_body_and_mesh_fall_back_to_envelope() -> None:
    tilted = _furniture(
        body=EntityBodyGeometry(kind='cylinder', radius_m=0.4),
        orientation=quaternion_from_euler_deg(yaw_deg=0.0, pitch_deg=30.0, roll_deg=0.0),
    )
    assert entity_exact_body_footprint(tilted) is None
    assert entity_collision_geometry_authority(tilted) == 'bounding_envelope'
    # The envelope hull still covers the tilted projection.
    assert entity_horizontal_footprint(tilted).area > math.pi * 0.16

    meshed = _furniture(body=EntityBodyGeometry(kind='mesh_asset', mesh=_mesh_asset()))
    # Issue #656: resolved upright mesh bodies contribute their mesh-derived
    # concave footprint, not the size_m envelope rectangle.
    mesh_footprint = entity_exact_body_footprint(meshed)
    assert mesh_footprint is not None
    assert mesh_footprint.area == pytest.approx(0.5 * 0.4 * 0.3, rel=0.05)
    assert entity_collision_geometry_authority(meshed) == 'exact_body_geometry'


def test_mesh_beyond_envelope_reports_envelope_unverified() -> None:
    """Issue #656: a mesh body that is not provably inside size_m can no
    longer masquerade as 'bounding_envelope' clearance authority."""
    oversized = _furniture(
        body=EntityBodyGeometry(
            kind='mesh_asset', mesh=_mesh_asset(uniform_scale=4.0)
        ),
        orientation=quaternion_from_euler_deg(yaw_deg=0.0, pitch_deg=30.0, roll_deg=0.0),
    )
    assert entity_exact_body_footprint(oversized) is None
    assert entity_collision_geometry_authority(oversized) == 'envelope_unverified'
    upright_oversized = _furniture(
        body=EntityBodyGeometry(
            kind='mesh_asset', mesh=_mesh_asset(uniform_scale=4.0)
        ),
    )
    # The exact silhouette still wins when available — collision uses real
    # geometry, which is strictly better than the envelope proxy.
    assert entity_collision_geometry_authority(upright_oversized) == 'exact_body_geometry'

    yaw_only = _furniture(
        body=EntityBodyGeometry(kind='cylinder', radius_m=0.4),
        orientation=quaternion_from_euler_deg(yaw_deg=45.0, pitch_deg=0.0, roll_deg=0.0),
    )
    assert entity_collision_geometry_authority(yaw_only) == 'exact_body_geometry'


# --- Snapping ----------------------------------------------------------------


def _snap_vertex_count(entity: SceneEntity) -> int:
    candidates = generate_snap_candidates(
        _scene(entity),
        exclude_ids=set(),
        axis='x',
        probe=Position3(x_m=0.0, y_m=0.0, z_m=0.0),
        kinds={'vertex'},
    )
    return len(candidates)


def test_snap_vertices_follow_body_shape() -> None:
    assert _snap_vertex_count(_furniture()) == 8
    cylinder = _furniture(body=EntityBodyGeometry(kind='cylinder', radius_m=0.4))
    assert _snap_vertex_count(cylinder) == 16
    polygon = _furniture(body=EntityBodyGeometry(
        kind='extruded_polygon',
        footprint_vertices=_L_FOOTPRINT,
    ))
    assert _snap_vertex_count(polygon) == 2 * len(_L_FOOTPRINT)


def test_snap_edges_cover_extruded_polygon_rings() -> None:
    polygon = _furniture(body=EntityBodyGeometry(
        kind='extruded_polygon',
        footprint_vertices=_L_FOOTPRINT,
    ))
    edges = generate_snap_candidates(
        _scene(polygon),
        exclude_ids=set(),
        axis='z',
        probe=Position3(x_m=2.0, y_m=2.0, z_m=0.0),
        kinds={'edge'},
    )
    # bottom ring + top ring + verticals = 3n edges for an n-gon prism.
    assert len(edges) == 3 * len(_L_FOOTPRINT)


# --- Video-geometry collision evaluation -------------------------------------


def _projector_spec():
    provenance = ProjectorSpecificationProvenance(
        source_kind='manufacturer',
        publisher='Example Projection Co.',
        document_title='Model P Optical Installation Specification',
        document_version='2026.1',
        reference='Throw and lens-shift table',
        source_uri='https://example.invalid/projector-p/spec',
        source_sha256='a' * 64,
        evidence=ProjectorSpecEvidenceRef(
            evidence_kind='external_authority',
            evidence_sha256='b' * 64,
        ),
    )
    return build_projector_specification(
        specification_id='example-projector-p',
        version='2026.1',
        manufacturer='Example Projection Co.',
        model='P',
        provenance=provenance,
        lens_reference_offset_m=Offset3(x_m=0.0, y_m=-0.25, z_m=0.0),
        optical_axis_local=Direction3(x=0.0, y=-1.0, z=0.0),
        throw_ratio_min=1.2,
        throw_ratio_max=1.6,
        optical_zoom_ratio=1.33,
        horizontal_lens_shift=LensShiftRange(minimum_fraction=-0.25, maximum_fraction=0.25),
        vertical_lens_shift=LensShiftRange(minimum_fraction=-0.65, maximum_fraction=0.65),
        supported_aspect_ratios=(AspectRatio(width_units=16, height_units=9),),
    )


def _video_scene(speaker: SceneEntity) -> SceneDocument:
    return SceneDocument(
        document_id=DOCUMENT_ID,
        room=RoomPrism(width_m=8.0, depth_m=6.0, height_m=3.0),
        entities=(
            SceneEntity(
                entity_id='screen-main',
                kind='screen',
                name='Main Screen',
                position=Position3(x_m=4.0, y_m=0.2, z_m=1.5),
                size_m=Size3(x_m=3.2, y_m=0.1, z_m=1.8),
            ),
            SceneEntity(
                entity_id='projector-main',
                kind='projector',
                name='Projector',
                position=Position3(x_m=4.0, y_m=4.3, z_m=2.25),
                size_m=Size3(x_m=0.5, y_m=0.5, z_m=0.2),
            ),
            SceneEntity(
                entity_id='seat-front',
                kind='seat',
                name='Seat',
                position=Position3(x_m=4.0, y_m=2.2, z_m=0.5),
                size_m=Size3(x_m=0.8, y_m=0.8, z_m=1.0),
            ),
            speaker,
        ),
    )


def _video_request(specification):
    return build_video_geometry_request(
        projector_entity_id='projector-main',
        projector_specification=specification,
        screen=ScreenGeometryBinding(
            entity_id='screen-main',
            visible_width_m=8.0 / 3.0,
            visible_height_m=1.5,
            frame_clearance_m=0.05,
            acoustically_transparent=True,
        ),
        seats=(
            SeatGeometryBinding(
                geometry_source='manual',
                entity_id='seat-front',
                row_id='row-front',
                eye_reference_offset_local_m=Offset3(z_m=0.65),
                head_center_offset_local_m=Offset3(z_m=0.65),
                head_radius_m=0.16,
            ),
        ),
        policy=VideoGeometryPolicy(
            horizontal_viewing_angle_deg=AngleRange(minimum_deg=20.0, maximum_deg=80.0),
            vertical_viewing_angle_deg=AngleRange(minimum_deg=10.0, maximum_deg=50.0),
            center_elevation_angle_deg=AngleRange(minimum_deg=-15.0, maximum_deg=20.0),
            sightline_samples=(
                SightlineSample(
                    sample_id='bottom-center',
                    horizontal_fraction=0.5,
                    vertical_fraction=0.0,
                ),
            ),
            sightline_clearance_m=0.03,
            riser_support_tolerance_m=0.005,
            max_optical_axis_deviation_deg=0.1,
            collision_clearance_m=0.02,
        ),
        collision_entity_ids=('screen-main', 'projector-main', 'speaker-edge'),
    )


def _corner_speaker(body: EntityBodyGeometry | None) -> SceneEntity:
    """Speaker diagonally past the screen corner: envelope overlaps the
    clearance-inflated screen box, but a r=0.25 cylinder clears it."""
    return SceneEntity(
        entity_id='speaker-edge',
        kind='speaker',
        name='Edge speaker',
        speaker_role='FL',
        position=Position3(x_m=5.88, y_m=0.53, z_m=1.0),
        size_m=Size3(x_m=0.5, y_m=0.5, z_m=0.3),
        body_geometry=body,
    )


def _collision_for(tmp_path: Path, speaker: SceneEntity):
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(_video_scene(speaker), parent_revision_id=None).revision
    specification = _projector_spec()
    evaluation = evaluate_video_geometry(
        baseline=revision,
        variant=None,
        projector_specification=specification,
        request=_video_request(specification),
    )
    return next(
        item for item in evaluation.collisions
        if {item.entity_a, item.entity_b} == {'screen-main', 'speaker-edge'}
    ), evaluation


def test_collision_exact_cylinder_vs_envelope_divergence(tmp_path: Path) -> None:
    box_result, _ = _collision_for(tmp_path / 'box', _corner_speaker(None))
    assert box_result.status == 'FAIL'
    assert box_result.geometry_authority == 'bounding_envelope'

    cylinder = _corner_speaker(EntityBodyGeometry(kind='cylinder', radius_m=0.25))
    exact_result, _ = _collision_for(tmp_path / 'cyl', cylinder)
    assert exact_result.status == 'PASS'
    assert exact_result.geometry_authority == 'mixed_body_geometry'


def test_extruded_overlap_at_zero_clearance_still_intersects() -> None:
    """Touching/overlapping exact footprints collide even with zero clearance
    (mirrors the SAT 'no separating axis' convention of the OBB path)."""
    from htdt.cad_video_geometry import _extruded_intersects

    body = EntityBodyGeometry(kind='extruded_polygon', footprint_vertices=_L_FOOTPRINT)
    left = _furniture('left', body=body)
    overlapping = _furniture('right', x_m=2.4, body=body)
    assert _extruded_intersects(
        left, overlapping, left_extra_m=0.0, right_extra_m=0.0,
    ) is True
    apart = _furniture('far', x_m=5.0, body=body)
    assert _extruded_intersects(
        left, apart, left_extra_m=0.0, right_extra_m=0.0,
    ) is False


def test_collision_authority_omitted_keeps_legacy_evaluation_hash(tmp_path: Path) -> None:
    """Evaluation identity payload omits geometry_authority when None; with all
    box bodies the field is present but persisted payloads round-trip."""
    result, evaluation = _collision_for(tmp_path / 'hash', _corner_speaker(None))
    payload = evaluation.identity_payload()
    collision_payload = next(
        item for item in payload['collisions']
        if {item['entity_a'], item['entity_b']} == {'screen-main', 'speaker-edge'}
    )
    assert collision_payload['geometry_authority'] == 'bounding_envelope'
    # Rebuilding from the payload preserves the semantic hash.
    from htdt.cad_video_geometry import VideoGeometryEvaluation
    reopened = VideoGeometryEvaluation.model_validate(evaluation.model_dump(mode='json'))
    assert reopened.evaluation_sha256 == evaluation.evaluation_sha256


# --- Persistence -------------------------------------------------------------


def test_repository_round_trips_body_geometry_and_mesh_blob(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    asset = _mesh_asset()
    blob_sha = repository.store_blob(_SIMPLE_OBJ)
    assert blob_sha == asset.asset_sha256
    assert repository.read_blob(blob_sha) == _SIMPLE_OBJ
    assert repository.read_blob('0' * 64) is None

    document = _scene(
        _furniture('table-round', body=EntityBodyGeometry(kind='cylinder', radius_m=0.4)),
        _furniture('sofa-l', x_m=5.0, body=EntityBodyGeometry(
            kind='extruded_polygon', footprint_vertices=_L_FOOTPRINT,
        )),
        _furniture('rack', x_m=6.5, body=EntityBodyGeometry(kind='mesh_asset', mesh=asset)),
        _furniture('plain-box', x_m=7.0),
    )
    saved = repository.save(document, parent_revision_id=None)
    reopened = repository.latest(DOCUMENT_ID)
    assert reopened is not None
    # Issue #653: save upgrades inline meshes to the referenced form; the
    # persisted/loaded document carries BodyMeshReference + resolved cache.
    assert reopened.document == saved.revision.document
    assert reopened.content_hash == saved.revision.content_hash
    rack_body = reopened.document.entity('rack').body_geometry
    assert rack_body.mesh_reference is not None
    mesh = rack_body.mesh
    assert mesh.asset_sha256 == blob_sha
    assert mesh.source_name == 'tetra.obj'


# --- Viewport mesh -----------------------------------------------------------


def test_viewport_mesh_reflects_body_kind() -> None:
    from htdt.room_viewport import _entity_mesh

    box_mesh = _entity_mesh(_furniture())
    x0, x1, y0, y1, z0, z1 = box_mesh.bounds
    assert (x1 - x0) == pytest.approx(1.0)

    cylinder = _furniture(body=EntityBodyGeometry(kind='cylinder', radius_m=0.4))
    cyl_mesh = _entity_mesh(cylinder)
    x0, x1, y0, y1, z0, z1 = cyl_mesh.bounds
    assert (x1 - x0) == pytest.approx(0.8, abs=0.02)
    assert (z1 - z0) == pytest.approx(0.8)

    polygon = _furniture(body=EntityBodyGeometry(
        kind='extruded_polygon', footprint_vertices=_L_FOOTPRINT,
    ))
    poly_mesh = _entity_mesh(polygon)
    x0, x1, y0, y1, z0, z1 = poly_mesh.bounds
    assert (x1 - x0) == pytest.approx(1.0)
    assert (z1 - z0) == pytest.approx(0.8)

    mesh_entity = _furniture(body=EntityBodyGeometry(
        kind='mesh_asset',
        mesh=_mesh_asset(uniform_scale=2.0, local_offset_m=Offset3(x_m=-0.4)),
    ))
    asset_mesh = _entity_mesh(mesh_entity)
    x0, x1, _, _, z0, z1 = asset_mesh.bounds
    # local: x in [0*2-0.4, 0.4*2-0.4] + entity x 2.0 → [1.6, 2.4]
    assert x0 == pytest.approx(1.6)
    assert x1 == pytest.approx(2.4)
    assert (z1 - z0) == pytest.approx(0.4)


# --- Inspector / controller --------------------------------------------------


def test_inspector_edits_body_geometry_and_mesh(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from htdt.room_workspace import (
        RoomWorkspaceController,
        SelectionInspector,
        format_footprint_vertices,
        parse_footprint_vertices,
    )

    repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    repository.save(_scene(_furniture('table')), parent_revision_id=None)
    controller = RoomWorkspaceController(repository, DOCUMENT_ID)
    entity_id = 'table'
    controller.set_selection(entity_id)

    inspector = SelectionInspector()
    inspector.set_entity(controller.document.entity(entity_id), editable=True)
    assert inspector.shape_field.currentData() == 'box'

    index = inspector.shape_field.findData('cylinder')
    inspector.shape_field.setCurrentIndex(index)
    inspector.radius_field.setValue(0.35)
    name, _pos, _size, _role, _orientation, _aim, body = inspector.values(
        controller.document.entity(entity_id)
    )
    assert body == EntityBodyGeometry(kind='cylinder', radius_m=0.35)
    assert controller.update_selected(
        name=name, position=_pos, size_m=_size, speaker_role=_role, body_geometry=body,
    ) is True
    updated = controller.document.entity(entity_id)
    assert updated.body_geometry.kind == 'cylinder'
    assert entity_collision_geometry_authority(updated) == 'exact_body_geometry'

    # Undo restores the plain envelope body.
    controller.working.undo()
    assert controller.document.entity(entity_id).body_geometry is None

    # Mesh import binds exact asset provenance and persists the blob.
    obj_path = tmp_path / 'body.obj'
    obj_path.write_bytes(_SIMPLE_OBJ)
    meshed = controller.attach_mesh_asset(entity_id, obj_path)
    assert meshed.body_geometry.kind == 'mesh_asset'
    assert meshed.body_geometry.mesh.asset_sha256 == sha256(_SIMPLE_OBJ).hexdigest()
    assert repository.read_blob(sha256(_SIMPLE_OBJ).hexdigest()) == _SIMPLE_OBJ

    assert format_footprint_vertices(_L_FOOTPRINT) == '-0.5,-0.5; 0.5,-0.5; 0.5,0; -0.1,0; -0.1,0.5; -0.5,0.5'
    with pytest.raises(ValueError):
        parse_footprint_vertices('0,0; 1,0')


# --- Installation report -----------------------------------------------------


def test_installation_output_reports_geometry_basis(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id=DOCUMENT_ID,
        room=RoomPrism(width_m=8.0, depth_m=6.0, height_m=3.0),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=1.0, y_m=0.6, z_m=1.0),
                size_m=Size3(x_m=0.5, y_m=0.5, z_m=0.4),
                body_geometry=EntityBodyGeometry(kind='cylinder', radius_m=0.25),
            ),
            SceneEntity(
                entity_id='seat-main',
                kind='seat',
                name='Seat',
                position=Position3(x_m=4.0, y_m=3.0, z_m=0.5),
                size_m=Size3(x_m=0.8, y_m=0.8, z_m=1.0),
            ),
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=4.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )
    revision = repository.save(document, parent_revision_id=None).revision
    output = build_installation_output(revision)
    rows = {item.entity_id: item for item in output.entities}
    assert rows['speaker-fl'].body_geometry_kind == 'cylinder'
    assert rows['speaker-fl'].collision_geometry_authority == 'exact_body_geometry'
    assert rows['seat-main'].body_geometry_kind == 'box'
    assert rows['seat-main'].collision_geometry_authority == 'bounding_envelope'
    assert rows['point-mlp'].body_geometry_kind is None
    assert output.authority_version == 'installation-output-5'

    csv_text = render_installation_csv(output)
    assert 'body_geometry_kind' in csv_text
    assert 'exact_body_geometry' in csv_text


def test_installation_output_carries_view_outlines(tmp_path: Path) -> None:
    """Issue #893: the exact snapshot carries hash-bound top/front/side
    outlines so drawing sheets render the real body shape, never a
    fabricated marker."""
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id=DOCUMENT_ID,
        room=RoomPrism(width_m=8.0, depth_m=6.0, height_m=3.0),
        entities=(
            SceneEntity(
                entity_id='sub-cyl',
                kind='speaker',
                speaker_role='SW',
                name='Sub',
                position=Position3(x_m=1.0, y_m=1.0, z_m=0.4),
                size_m=Size3(x_m=0.9, y_m=0.9, z_m=0.8),
                body_geometry=EntityBodyGeometry(kind='cylinder', radius_m=0.4),
            ),
            SceneEntity(
                entity_id='seat-1',
                kind='seat',
                name='Seat',
                position=Position3(x_m=4.0, y_m=3.0, z_m=0.5),
                size_m=Size3(x_m=0.8, y_m=0.8, z_m=1.0),
            ),
        ),
    )
    revision = repository.save(document, parent_revision_id=None).revision
    output = build_installation_output(revision)
    rows = {item.entity_id: item for item in output.entities}

    cyl = {o.view: o for o in rows['sub-cyl'].outlines}
    assert set(cyl) == {'top', 'front', 'side'}
    assert all(o.basis == 'exact_body_geometry' for o in cyl.values())
    top_ring = cyl['top'].polygons_m[0]
    # cylinder r=0.4 centered at (1, 1) → silhouette reaches x=1.4
    assert max(x for x, _ in top_ring) == pytest.approx(1.4)
    # front view of an upright cylinder: the extrusion rectangle
    assert cyl['front'].polygons_m[0] == (
        (0.6, 0.0), (1.4, 0.0), (1.4, 0.8), (0.6, 0.8)
    )

    seat = {o.view: o for o in rows['seat-1'].outlines}
    assert set(seat) == {'top', 'front', 'side'}
    assert all(o.basis == 'bounding_envelope' for o in seat.values())
    top = seat['top'].polygons_m[0]
    xs = sorted({x for x, _ in top})
    ys = sorted({y for _, y in top})
    assert xs == [pytest.approx(3.6), pytest.approx(4.4)]
    assert ys == [pytest.approx(2.6), pytest.approx(3.4)]

    # envelope_unverified mesh bodies (legacy oversized inline meshes) no
    # longer crash the report Literal — the tilted oversized body cannot be
    # persisted under the post-#653 authority regime, so exercise the row
    # builder directly.
    oversized = _furniture(
        body=EntityBodyGeometry(
            kind='mesh_asset', mesh=_mesh_asset(uniform_scale=4.0)
        ),
        orientation=quaternion_from_euler_deg(
            yaw_deg=0.0, pitch_deg=30.0, roll_deg=0.0
        ),
    )
    mesh_row = _installation_entity(oversized)
    assert mesh_row.collision_geometry_authority == 'envelope_unverified'
    mesh_top = {o.view: o for o in mesh_row.outlines}['top']
    assert mesh_top.basis == 'bounding_envelope'
