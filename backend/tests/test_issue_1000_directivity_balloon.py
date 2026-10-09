"""Issue #1000 — speaker directivity-balloon overlay.

Contract under test: the overlay renders ONLY sealed measured directivity
(meshes over measured grid cells, or honest measured-direction point
clouds); a speaker without a bound dataset/aim renders UNKNOWN — never a
synthesized lobe. Orientation keys to the installed ``aim_xyz`` (the
coverage ``explicit-aim-body-up-source-frame-1`` convention), every
resolve re-reads ``current_head``, the frequency is exact-grid only, and
the balloon radius is an explicitly-relative-dB display mapping.
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import cos, radians, sin
from pathlib import Path

import pytest

from htdt.cad_coverage_aim_authority import CadAimAxis, build_aim_state
from htdt.cad_coverage_aim_repository import CadCoverageAimRepository
from htdt.cad_directivity import (
    NORMALIZED_JSON_DIRECTIVITY_ADAPTER,
)
from htdt.cad_directivity_repository import CadDirectivityRepository
from htdt.cad_equipment import (
    AngleDomain,
    DirectivityCapability,
    DirectivityDomain,
    EquipmentDataProvenance,
    FrequencyDomain,
    InterpolationProvenance,
    Offset3,
    Size3,
    build_equipment_definition,
)
from htdt.cad_equipment_evidence import build_equipment_manual_evidence
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_installation_context import build_installation_context
from htdt.cad_installation_context_repository import (
    CadInstallationContextRepository,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    quaternion_from_euler_deg,
)
from htdt.cad_schema import connect_sqlite
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.room_directivity_overlay import (
    DirectivityOverlayRequest,
    RoomDirectivityOverlayController,
    resolve_directivity_overlay,
)

NOW = '2026-10-09T00:00:00+00:00'
DOCUMENT_ID = 'issue1000-balloon-fixture'


def _provenance(source_hash: str) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='user_defined',
        source_name='issue-1000 deterministic fixture',
        source_version='2026-10-09',
        source_reference='issue-1000-balloon',
        source_sha256=source_hash,
    )


def _authority(
    *,
    definition_id: str = 'balloon-speaker',
    semantics: str = 'horizontal_vertical',
    horizontal_wrap: str = 'none',
    horizontal_angles: tuple[float, ...] = (-60.0, 0.0, 60.0),
    vertical_angles: tuple[float, ...] = (0.0,),
    frequencies: tuple[float, ...] = (500.0, 1000.0),
):
    samples = []
    for frequency_hz in frequencies:
        for v_deg in vertical_angles:
            for h_deg in horizontal_angles:
                magnitude = round(
                    -(abs(h_deg) * 0.05 + abs(v_deg) * 0.05), 4
                )
                samples.append(
                    {
                        'frequency_hz': frequency_hz,
                        'horizontal_angle_deg': h_deg,
                        'vertical_angle_deg': v_deg,
                        'magnitude': magnitude,
                        'phase_deg': None,
                    }
                )
    source_payload = {
        'schema': 'htdt.normalized-directivity.v1',
        'dataset_id': f'{definition_id}-directivity',
        'version': '1',
        'source_format': 'custom',
        'evidence_kind': 'user_defined',
        'source_name': 'issue-1000 deterministic fixture',
        'source_version': '2026-10-09',
        'source_reference': 'issue-1000-balloon',
        'kind': 'magnitude_only',
        'coordinate_convention': {
            'angle_semantics': semantics,
            'horizontal_wrap': horizontal_wrap,
            'reference_axis': 'equipment_acoustic_reference_axis',
            'azimuth_positive': 'left',
            'elevation_positive': 'up',
            'angle_unit': 'degree',
        },
        'normalization': {
            'source_magnitude_unit': 'db',
            'normalized_magnitude_unit': 'db',
            'reference': 'on_axis_per_frequency',
            'reference_level_db': None,
            'conversion_version': 'pressure-amplitude-db20-v1',
        },
        'phase_reference': None,
        'interpolation_method': 'linear',
        'interpolation_implementation': 'htdt-grid-linear',
        'interpolation_version': '1',
        'frequencies_hz': list(frequencies),
        'horizontal_angles_deg': list(horizontal_angles),
        'vertical_angles_deg': list(vertical_angles),
        'samples': samples,
    }
    source_bytes = json.dumps(
        source_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    source_hash = sha256(source_bytes).hexdigest()
    provenance = _provenance(source_hash)
    domain = DirectivityDomain(
        frequency=FrequencyDomain(
            minimum_hz=min(frequencies), maximum_hz=max(frequencies)
        ),
        horizontal=AngleDomain(
            minimum_deg=min(horizontal_angles),
            maximum_deg=max(horizontal_angles),
        ),
        vertical=AngleDomain(
            minimum_deg=min(vertical_angles),
            maximum_deg=max(vertical_angles),
        ),
    )
    interpolation = InterpolationProvenance(
        method='linear',
        implementation='htdt-grid-linear',
        implementation_version='1',
        provenance=provenance,
    )
    definition = build_equipment_definition(
        definition_id=definition_id,
        version='1',
        identity_kind='user_defined',
        user_label=definition_id,
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.20, y_m=0.25, z_m=0.35),
        acoustic_reference_point_m=Offset3(x_m=0.20),
        directivity=DirectivityCapability(
            tier='magnitude_only',
            data_format='custom',
            provenance=provenance,
            data_asset_sha256=source_hash,
            valid_domain=domain,
            interpolation=interpolation,
        ),
    )
    dataset = NORMALIZED_JSON_DIRECTIVITY_ADAPTER.parse(
        source_bytes, definition
    )
    return source_bytes, definition, dataset


def _speaker(
    entity_id: str = 'speaker-fl',
    *,
    aim: Direction3 | None = Direction3(x=0.0, y=1.0, z=0.0),
) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name='Front Left',
        speaker_role='FL',
        position=Position3(x_m=0.0, y_m=0.0, z_m=1.0),
        orientation=quaternion_from_euler_deg(
            yaw_deg=0.0, pitch_deg=0.0, roll_deg=0.0
        ),
        size_m=Size3(x_m=0.20, y_m=0.25, z_m=0.35),
        aim_xyz=aim,
    )


def _document(*entities: SceneEntity) -> SceneDocument:
    return SceneDocument(
        document_id=DOCUMENT_ID,
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=6.0, height_m=2.5),
        entities=tuple(entities),
    )


def _repositories(tmp_path: Path, *entities: SceneEntity):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        _document(*entities), parent_revision_id=None
    ).revision
    variant_repository = CadSystemVariantRepository(scene_repository)
    equipment_repository = CadEquipmentRepository(
        scene_repository, variant_repository
    )
    directivity_repository = CadDirectivityRepository(
        scene_repository, equipment_repository
    )
    installation_repository = CadInstallationContextRepository(
        scene_repository, equipment_repository
    )
    aim_repository = CadCoverageAimRepository(scene_repository)
    return (
        scene_repository,
        revision,
        equipment_repository,
        directivity_repository,
        installation_repository,
        aim_repository,
    )


def _persist_authority(equipment_repository, directivity_repository, authority):
    source_bytes, definition, dataset = authority
    for evidence in build_equipment_manual_evidence(
        definition, actor='issue1000-fixture', recorded_at_utc=NOW
    ):
        equipment_repository.save_evidence(evidence)
    equipment_repository.save_definition(definition)
    directivity_repository.save_dataset(
        dataset,
        source_bytes=source_bytes,
        source_filename=f'{definition.definition_id}.normalized.json',
        media_type='application/json',
        declared_schema='htdt.normalized-directivity.v1',
    )
    return definition


def _bind_context(installation_repository, entity_id: str, definition) -> None:
    context = build_installation_context(
        context_id=f'ctx-{entity_id}',
        document_id=DOCUMENT_ID,
        entity_id=entity_id,
        equipment_definition=definition,
        selected_mounting_mode='stand',
        provenance=(_provenance(definition.semantic_sha256),),
        created_at_utc=NOW,
    )
    installation_repository.save_context(context)


def _resolve(repositories, **request_kwargs):
    (
        scene_repository,
        _revision,
        equipment_repository,
        directivity_repository,
        installation_repository,
        aim_repository,
    ) = repositories
    request = DirectivityOverlayRequest(**request_kwargs)
    return resolve_directivity_overlay(
        scene_repository=scene_repository,
        equipment_repository=equipment_repository,
        directivity_repository=directivity_repository,
        installation_repository=installation_repository,
        aim_repository=aim_repository,
        document_id=DOCUMENT_ID,
        request=request,
    )


def _seeded(tmp_path: Path, *, aim=Direction3(x=0.0, y=1.0, z=0.0), authority=None):
    repositories = _repositories(tmp_path, _speaker(aim=aim))
    authority = authority or _authority()
    definition = _persist_authority(
        repositories[2], repositories[3], authority
    )
    _bind_context(repositories[4], 'speaker-fl', definition)
    return repositories


def test_no_installation_context_renders_unknown(tmp_path: Path):
    repositories = _repositories(tmp_path, _speaker())
    scene = _resolve(repositories)
    assert scene.state == 'rendered'
    assert len(scene.balloons) == 1
    balloon = scene.balloons[0]
    # honest UNKNOWN — a gray wireframe marker, never a synthesized lobe
    assert balloon.state == 'unknown'
    assert balloon.vertices == ()
    assert balloon.faces == ()
    assert balloon.dataset_id is None


def test_context_without_dataset_renders_unknown(tmp_path: Path):
    repositories = _repositories(tmp_path, _speaker())
    definition = _persist_authority(
        repositories[2], repositories[3], _authority()
    )
    # bind a DIFFERENT equipment definition that has no datasets
    other_source, other_definition, _other_dataset = _authority(
        definition_id='dataset-less-speaker'
    )
    for evidence in build_equipment_manual_evidence(
        other_definition, actor='issue1000-fixture', recorded_at_utc=NOW
    ):
        repositories[2].save_evidence(evidence)
    repositories[2].save_definition(other_definition)
    _bind_context(repositories[4], 'speaker-fl', other_definition)
    scene = _resolve(repositories)
    balloon = scene.balloons[0]
    assert balloon.state == 'unknown'
    assert balloon.vertices == ()
    assert any('データセット' in reason for reason in balloon.reasons)


def test_missing_aim_never_guesses_orientation(tmp_path: Path):
    # dataset bound, aim_xyz unset → UNKNOWN, not a balloon pointing at a
    # guessed direction (body orientation may not substitute for the aim)
    repositories = _seeded(tmp_path, aim=None)
    scene = _resolve(repositories)
    balloon = scene.balloons[0]
    assert balloon.state == 'unknown'
    assert balloon.vertices == ()
    assert any('aim_xyz' in reason for reason in balloon.reasons)


def test_measured_points_keyed_to_installed_aim(tmp_path: Path):
    repositories = _seeded(tmp_path)
    scene = _resolve(repositories)
    balloon = scene.balloons[0]
    # hv_cuts_suspect grid → honest point cloud, never an invented surface
    assert balloon.state == 'points'
    assert balloon.faces == ()
    assert balloon.dataset_id == 'balloon-speaker-directivity'
    assert balloon.dataset_semantic_sha256
    assert balloon.grid_classification == 'hv_cuts_suspect'
    assert balloon.display_scale == 'normalized_on_axis'
    assert balloon.frequency_hz == 500.0  # first exact grid frequency
    # every vertex sits at a DECLARED grid angle — no interpolation
    declared = {
        (h, v)
        for h in (-60.0, 0.0, 60.0)
        for v in (0.0,)
    }
    assert {
        (v.horizontal_angle_deg, v.vertical_angle_deg)
        for v in balloon.vertices
    } == declared
    # orientation: on-axis (h=0,v=0) vertex must lie along aim_xyz (+Y)
    on_axis = next(
        v
        for v in balloon.vertices
        if v.horizontal_angle_deg == 0.0 and v.vertical_angle_deg == 0.0
    )
    assert on_axis.direction_domain == pytest.approx((0.0, 1.0, 0.0), abs=1e-9)
    assert on_axis.level_t == pytest.approx(1.0)
    assert on_axis.magnitude_db == pytest.approx(0.0)
    # aim arrow drawn from the installed aim_xyz
    aim_arrow = next(a for a in balloon.arrows if a.kind == 'aim')
    assert aim_arrow.direction_domain == (0.0, 1.0, 0.0)


def test_aim_rotation_repositions_balloon(tmp_path: Path):
    # a rotated installed aim must rotate the balloon — sign-level check
    aim = Direction3(x=1.0, y=0.0, z=0.0)  # installed pointing +X
    repositories = _seeded(tmp_path, aim=aim)
    scene = _resolve(repositories)
    balloon = scene.balloons[0]
    on_axis = next(
        v for v in balloon.vertices if v.horizontal_angle_deg == 0.0
    )
    assert on_axis.direction_domain == pytest.approx((1.0, 0.0, 0.0), abs=1e-9)
    off_axis = next(
        v for v in balloon.vertices if v.horizontal_angle_deg == 60.0
    )
    # azimuth positive = local left → left of +X-forward is +Y in world
    assert off_axis.direction_domain[1] > 0.0


def test_off_grid_frequency_blocks_never_interpolates(tmp_path: Path):
    repositories = _seeded(tmp_path)
    scene = _resolve(repositories, frequency_hz=750.0)
    balloon = scene.balloons[0]
    assert balloon.state == 'blocked'
    assert balloon.vertices == ()
    assert any('補間' in reason for reason in balloon.reasons)


def test_full_sphere_grid_renders_mesh(tmp_path: Path):
    authority = _authority(
        semantics='spherical_azimuth_elevation',
        horizontal_wrap='signed_180',
        horizontal_angles=tuple(
            float(h) for h in range(-180, 180, 30)
        ),
        vertical_angles=(-45.0, 0.0, 15.0, 45.0),
    )
    repositories = _seeded(tmp_path, authority=authority)
    scene = _resolve(repositories)
    balloon = scene.balloons[0]
    assert balloon.grid_classification == 'full_sphere_grid'
    assert balloon.state == 'mesh'
    assert len(balloon.vertices) == 12 * 4
    assert balloon.faces  # closed quads only — triangulated
    # every face references existing vertices and is non-degenerate
    for tri in balloon.faces:
        assert len(set(tri)) == 3
        assert all(index < len(balloon.vertices) for index in tri)


def test_radius_is_display_scale_never_physical(tmp_path: Path):
    repositories = _seeded(tmp_path)
    scene = _resolve(repositories, floor_db=-20.0)
    balloon = scene.balloons[0]
    on_axis = next(
        v for v in balloon.vertices if v.horizontal_angle_deg == 0.0
    )
    off_axis = next(
        v for v in balloon.vertices if v.horizontal_angle_deg == -60.0
    )
    origin = balloon.origin
    assert origin is not None

    def radius(vertex):
        return (
            (vertex.position.x_m - origin.x_m) ** 2
            + (vertex.position.y_m - origin.y_m) ** 2
            + (vertex.position.z_m - origin.z_m) ** 2
        ) ** 0.5

    # r = r_min + (r_max - r_min) * clamp((gain - floor) / (0 - floor))
    r_min = balloon.radius_max_m * 0.06
    expected = r_min + (balloon.radius_max_m - r_min) * (
        (off_axis.magnitude_db - (-20.0)) / 20.0
    )
    assert radius(off_axis) == pytest.approx(expected, abs=1e-9)
    assert radius(on_axis) == pytest.approx(balloon.radius_max_m, abs=1e-9)


def test_missing_source_asset_fails_closed(tmp_path: Path):
    repositories = _seeded(tmp_path)
    scene = _resolve(repositories)
    asset_sha = scene.balloons[0].dataset_semantic_sha256
    assert asset_sha
    # delete the managed source asset row → listing marks it absent and
    # the resolver blocks instead of trusting an unverifiable payload
    with connect_sqlite(repositories[0].path) as connection:
        # FK normally keeps a dataset from pointing at a missing asset —
        # simulate store corruption directly.
        connection.execute('PRAGMA foreign_keys=OFF')
        connection.execute('DELETE FROM cad_measurement_assets')
        connection.commit()
    scene = _resolve(repositories)
    assert scene.balloons[0].state == 'blocked'


def test_aim_state_declared_axes_rendered_as_arrows(tmp_path: Path):
    repositories = _seeded(tmp_path)
    aim_repository = repositories[5]
    aim_repository.save_aim_state(
        build_aim_state(
            document_id=DOCUMENT_ID,
            speaker_entity_id='speaker-fl',
            aim_axes=(
                CadAimAxis(
                    kind='acoustic_reference_axis',
                    vector=(0.0, 1.0, 0.0),
                ),
                CadAimAxis(
                    kind='design_aim_target',
                    # unit vector, ~26° above +Y
                    vector=(0.0, 0.8987940463, 0.4383711468),
                ),
                CadAimAxis(
                    kind='as_built_observed_aim',
                    # unit vector, ~10° above +Y
                    vector=(0.0, 0.9848077530, 0.1736481777),
                ),
            ),
            cabinet_to_dataset_rotation=(1.0, 0.0, 0.0, 0.0),
            declared_at_utc=NOW,
        )
    )
    scene = _resolve(repositories)
    balloon = scene.balloons[0]
    kinds = {arrow.kind for arrow in balloon.arrows}
    assert {'aim', 'design', 'as-built'} <= kinds
    # design vs as-built divergence is surfaced, never merged silently
    assert any('乖離' in reason for reason in balloon.reasons)
    assert balloon.aim_sha256


def test_scene_edit_lapses_resolved_overlay(tmp_path: Path):
    repositories = _seeded(tmp_path)
    (
        scene_repository,
        _revision,
        equipment_repository,
        directivity_repository,
        installation_repository,
        aim_repository,
    ) = repositories
    controller = RoomDirectivityOverlayController(
        scene_repository,
        equipment_repository,
        directivity_repository,
        installation_repository,
        DOCUMENT_ID,
        aim_repository=aim_repository,
    )
    controller.arm(DirectivityOverlayRequest())
    first = controller.resolve()
    assert first.balloons[0].state == 'points'
    # cache hit: identical resolve key returns the same scene object
    assert controller.resolve() is first

    # scene edit: rotate the installed aim 90° (+Y → +X)
    head = scene_repository.current_head(DOCUMENT_ID)
    edited = _document(
        _speaker(aim=Direction3(x=1.0, y=0.0, z=0.0)),
    )
    scene_repository.save(edited, parent_revision_id=head.revision_id)

    second = controller.resolve()
    assert second is not first
    assert second.scene_revision_id != first.scene_revision_id
    on_axis = next(
        v
        for v in second.balloons[0].vertices
        if v.horizontal_angle_deg == 0.0
    )
    assert on_axis.direction_domain == pytest.approx((1.0, 0.0, 0.0), abs=1e-9)


def test_disarmed_controller_returns_none(tmp_path: Path):
    repositories = _seeded(tmp_path)
    (
        scene_repository,
        _revision,
        equipment_repository,
        directivity_repository,
        installation_repository,
        aim_repository,
    ) = repositories
    controller = RoomDirectivityOverlayController(
        scene_repository,
        equipment_repository,
        directivity_repository,
        installation_repository,
        DOCUMENT_ID,
        aim_repository=aim_repository,
    )
    assert controller.resolve() is None
    controller.arm(DirectivityOverlayRequest())
    assert controller.resolve() is not None
    controller.clear()
    assert controller.resolve() is None


def test_workspace_toggle_draws_and_clears(tmp_path: Path):
    pytest.importorskip('PySide6')
    from PySide6.QtWidgets import QApplication

    from htdt.room_workspace import RoomWorkspace
    from test_room_cadux import FakeRoomViewport

    app = QApplication.instance() or QApplication(['htdt-test'])
    repositories = _seeded(tmp_path)
    scene_repository = repositories[0]

    class DirectivityViewport(FakeRoomViewport):
        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self.directivity_calls: list = []

        def render_directivity_overlay(self, scene) -> None:
            self.directivity_calls.append(scene)

        def clear_directivity_overlay(self) -> None:
            self.directivity_calls.append('cleared')

    workspace = RoomWorkspace(
        scene_repository,
        DOCUMENT_ID,
        viewport_factory=lambda parent: DirectivityViewport(parent),
    )
    try:
        workspace.overlay_controls.acoustics.setChecked(True)
        workspace.directivity_panel.enable_check.setChecked(True)
        workspace.set_context('placement')
        calls = workspace.viewport.directivity_calls
        assert calls, 'armed overlay must render on placement context'
        scene = calls[-1]
        assert scene.balloons[0].state == 'points'
        assert (
            workspace.directivity_panel.status_label.text()
            == scene.summary_ja
        )
        workspace.directivity_panel.enable_check.setChecked(False)
        assert workspace.viewport.directivity_calls[-1] == 'cleared'
    finally:
        workspace.deleteLater()
