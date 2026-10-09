"""#1006: spatial-campaign markers overlaid on RoomViewport3D (REV73).

Honesty contract under test: markers sit at the exact declared XYZ of
each CampaignPoint on the CURRENT head; marker shape carries the primary
role (a spatial holdout can never visually merge with an optimization
point); progress is fed ONLY by persisted runner-cell states + point
bindings — a point with neither reads as unbound/unknown, never guessed;
a design pinned to a superseded SceneRevision lapses (the #999/#1009
staleness lesson) with all claims withheld.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

pytest.importorskip('PySide6')

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_spatial_campaign import (
    CampaignPoint,
    ListeningAreaSpec,
    ListeningZone,
    build_point_binding,
    build_spatial_campaign_design,
)
from htdt.cad_spatial_campaign_repository import CadSpatialCampaignRepository
from htdt.measurement.domain.cad_measurement_runner import (
    RunnerCellSpec,
    build_runner_plan_from_cells,
)
from htdt.measurement.persistence.cad_measurement_runner_repository import (
    CadMeasurementRunnerRepository,
)
from htdt.room_campaign_overlay import (
    CAMPAIGN_PROGRESS_LABELS,
    CAMPAIGN_ROLE_COLORS,
    CAMPAIGN_ROLE_GLYPH,
    CampaignOverlayScene,
    RoomCampaignOverlayController,
    campaign_primary_role,
    resolve_campaign_overlay,
)

DOC_ID = 'campaign-1006-fixture'


def _fixture(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id=DOC_ID,
        room=RoomPrism(width_m=8.0, depth_m=6.0, height_m=3.0),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=1.0, y_m=0.5, z_m=1.4),
                size_m=Size3(x_m=0.3, y_m=0.3, z_m=0.4),
            ),
            SceneEntity(
                entity_id='mp-opt',
                kind='measurement_point',
                name='Opt',
                position=Position3(x_m=4.0, y_m=3.0, z_m=1.2),
            ),
            SceneEntity(
                entity_id='mp-hold',
                kind='measurement_point',
                name='Hold',
                position=Position3(x_m=6.0, y_m=3.0, z_m=1.2),
            ),
            SceneEntity(
                entity_id='mp-ref',
                kind='measurement_point',
                name='Ref',
                position=Position3(x_m=4.0, y_m=1.5, z_m=1.2),
            ),
        ),
    )
    revision = scene_repository.save(
        document, parent_revision_id=None
    ).revision
    return {
        'scene_repository': scene_repository,
        'spatial_repository': CadSpatialCampaignRepository(scene_repository),
        'runner_repository': CadMeasurementRunnerRepository(scene_repository),
        'revision': revision,
    }


def _design(fixture, *, revision=None, points=None) -> 'object':
    revision = fixture['revision'] if revision is None else revision
    if points is None:
        points = (
            CampaignPoint(
                point_id='p-ref',
                position=Position3(x_m=4.0, y_m=1.5, z_m=1.2),
                roles=('reference_alignment',),
            ),
            CampaignPoint(
                point_id='p-opt',
                position=Position3(x_m=4.0, y_m=3.0, z_m=1.2),
                roles=('optimization',),
            ),
            CampaignPoint(
                point_id='p-hold',
                position=Position3(x_m=6.0, y_m=3.0, z_m=1.2),
                # holdout + boundary on one point: holdout MUST own the
                # glyph (it can never merge with optimization).
                roles=('spatial_holdout', 'boundary_stress'),
            ),
            CampaignPoint(
                point_id='p-rep',
                position=Position3(x_m=5.0, y_m=3.0, z_m=1.2),
                roles=('repeatability',),
            ),
            CampaignPoint(
                point_id='p-diag',
                position=Position3(x_m=7.0, y_m=2.0, z_m=1.2),
                roles=('diagnostic',),
            ),
            CampaignPoint(
                point_id='p-bnd',
                position=Position3(x_m=0.5, y_m=0.5, z_m=1.2),
                roles=('boundary_stress',),
            ),
            CampaignPoint(
                point_id='p-std',
                position=Position3(x_m=4.0, y_m=4.5, z_m=1.2),
                roles=('standards_required',),
            ),
        )
    return build_spatial_campaign_design(
        document_id=DOC_ID,
        listening_area=ListeningAreaSpec(
            area_label='エリア',
            zones=(
                ListeningZone(
                    zone_id='seat',
                    kind='seat',
                    bounds_min=Position3(x_m=3.0, y_m=1.0, z_m=0.0),
                    bounds_max=Position3(x_m=7.0, y_m=5.0, z_m=2.5),
                ),
            ),
            head_height_range_m=(0.8, 1.6),
        ),
        points=points,
        declared_at_utc='2026-10-09T00:00:00+00:00',
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
    )


def _overlay(fixture) -> CampaignOverlayScene | None:
    return resolve_campaign_overlay(
        fixture['scene_repository'],
        fixture['spatial_repository'],
        fixture['runner_repository'],
        DOC_ID,
    )


def _runner_plan(fixture, targets=('mp-opt', 'mp-hold')):
    revision = fixture['revision']
    cells = [
        RunnerCellSpec(
            cell_index=index,
            channel_role='FL',
            source_speaker_ids=('speaker-fl',),
            target_entity_id=target,
            repeat_index=0,
            purpose='measurement',
        )
        for index, target in enumerate(targets)
    ]
    return build_runner_plan_from_cells(
        document_id=DOC_ID,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        cells=cells,
    )


# -- resolution ------------------------------------------------------------


def test_points_render_at_exact_declared_xyz(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    design = _design(fixture)
    fixture['spatial_repository'].save_design(design)
    scene = _overlay(fixture)
    assert scene is not None and not scene.lapsed
    assert scene.shown_count == 7 and scene.total_count == 7
    by_id = {marker.point_id: marker for marker in scene.markers}
    for point in design.points:
        marker = by_id[point.point_id]
        assert marker.position == point.position


def test_role_glyph_and_legend_vocabulary(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture['spatial_repository'].save_design(_design(fixture))
    scene = _overlay(fixture)
    assert scene is not None
    by_id = {marker.point_id: marker for marker in scene.markers}
    assert by_id['p-ref'].glyph == 'cone'
    assert by_id['p-opt'].glyph == 'sphere'
    # Holdout owns the glyph even when boundary_stress shares the point —
    # it can never visually merge with the optimization sphere.
    assert by_id['p-hold'].glyph == 'cube'
    assert by_id['p-rep'].glyph == 'disc'
    assert by_id['p-diag'].glyph == 'wireframe_sphere'
    assert by_id['p-bnd'].glyph == 'cylinder'
    assert by_id['p-std'].glyph == 'diamond'
    # Legend covers only the used roles; labels are ASCII for VTK.
    legend_labels = {label for label, _color in scene.legend}
    assert legend_labels == {'REF', 'OPT', 'HOLDOUT', 'REP', 'DIAG', 'BND', 'STD'}
    for label, _color in scene.legend:
        assert label.isascii()
    # Every declared role has a distinct shape AND a distinct colour.
    assert len(set(CAMPAIGN_ROLE_GLYPH.values())) == len(CAMPAIGN_ROLE_GLYPH)
    assert len(set(CAMPAIGN_ROLE_COLORS.values())) == len(CAMPAIGN_ROLE_COLORS)


def test_holdout_never_merges_with_optimization() -> None:
    assert campaign_primary_role(('spatial_holdout', 'boundary_stress')) == 'spatial_holdout'
    assert campaign_primary_role(('boundary_stress', 'optimization')) == 'boundary_stress'
    assert campaign_primary_role(('optimization', 'repeatability')) == 'optimization'
    assert CAMPAIGN_ROLE_GLYPH['spatial_holdout'] != CAMPAIGN_ROLE_GLYPH['optimization']
    assert CAMPAIGN_ROLE_COLORS['spatial_holdout'] != CAMPAIGN_ROLE_COLORS['optimization']


def test_progress_honesty_from_executor_cells(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    design = _design(fixture)
    fixture['spatial_repository'].save_design(design)
    plan = _runner_plan(fixture)
    fixture['runner_repository'].save_plan(plan)
    run = fixture['runner_repository'].start_run(plan.plan_id)
    # cell 0 (mp-opt) → staged; cell 1 (mp-hold) → retake_required via mark.
    fixture['runner_repository'].mark_cell(
        run.run_id, 0, 'staged', reason='captured'
    )
    fixture['runner_repository'].mark_cell(
        run.run_id, 1, 'assignment_incomplete', reason='missing routing'
    )
    # p-std measured via a persisted point binding.
    fixture['spatial_repository'].save_binding(
        build_point_binding(
            design=design,
            point_id='p-std',
            measurement_id='meas-1',
            measurement_sha256='a' * 64,
            captured_at_utc='2026-10-09T00:30:00+00:00',
        )
    )
    scene = _overlay(fixture)
    assert scene is not None
    assert scene.progress_source is not None
    by_id = {marker.point_id: marker for marker in scene.markers}
    assert by_id['p-opt'].progress == 'staged'
    assert by_id['p-hold'].progress == 'blocked'
    assert by_id['p-std'].progress == 'measured'
    # Points with no cell join and no binding are honest — never claimed.
    assert by_id['p-ref'].progress == 'unbound'
    assert by_id['p-rep'].progress == 'unbound'
    assert CAMPAIGN_PROGRESS_LABELS['unbound'] == 'セル未結合'
    counts = scene.progress_counts
    assert counts['unbound'] == 4
    # The executor's next incomplete cell drives bounded marker focus —
    # cell 0 is staged (still open) so p-opt is the focused marker.
    assert by_id['p-opt'].focused
    assert not by_id['p-hold'].focused


def test_no_executor_run_is_honest_unknown(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture['spatial_repository'].save_design(_design(fixture))
    scene = _overlay(fixture)
    assert scene is not None
    assert scene.progress_source is None
    assert all(m.progress == 'unbound' for m in scene.markers)
    assert any('no executor run' in line for line in scene.viewport_lines)


def test_stale_revision_lapses_with_claims_withheld(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture['spatial_repository'].save_design(_design(fixture))
    plan = _runner_plan(fixture)
    fixture['runner_repository'].save_plan(plan)
    fixture['runner_repository'].start_run(plan.plan_id)

    first = _overlay(fixture)
    assert first is not None and not first.lapsed

    # A scene edit mints a new head — the design stays bound to the old
    # revision and must surface as lapsed, with progress/validity withheld.
    revision = fixture['revision']
    fixture['scene_repository'].save(
        SceneDocument(
            document_id=DOC_ID,
            room=revision.document.room,
            entities=revision.document.entities,
        ).model_copy(update={'entities': revision.document.entities + (
            SceneEntity(
                entity_id='mp-new',
                kind='measurement_point',
                name='New',
                position=Position3(x_m=2.0, y_m=2.0, z_m=1.2),
            ),
        )}),
        parent_revision_id=revision.revision_id,
    )
    scene = _overlay(fixture)
    assert scene is not None and scene.lapsed
    assert scene.revision_id != first.revision_id
    assert all(m.progress == 'unknown' for m in scene.markers)
    assert all(m.validity == 'unknown' for m in scene.markers)
    assert any('LAPSED' in line for line in scene.viewport_lines)
    assert any('失効' in scene.summary_ja for _ in (0,))
    for line in scene.viewport_lines:
        assert line.isascii()


def test_controller_cache_invalidates_on_new_head(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture['spatial_repository'].save_design(_design(fixture))
    controller = RoomCampaignOverlayController(
        fixture['scene_repository'],
        DOC_ID,
        spatial_repository=fixture['spatial_repository'],
        runner_repository=fixture['runner_repository'],
    )
    controller.arm()
    first = controller.resolve()
    assert first is not None
    assert controller.resolve() is first
    revision = fixture['revision']
    fixture['scene_repository'].save(
        SceneDocument(
            document_id=DOC_ID,
            room=revision.document.room,
            entities=revision.document.entities + (
                SceneEntity(
                    entity_id='mp-new',
                    kind='measurement_point',
                    name='New',
                    position=Position3(x_m=2.0, y_m=2.0, z_m=1.2),
                ),
            ),
        ),
        parent_revision_id=revision.revision_id,
    )
    after = controller.resolve()
    assert after is not None and after is not first
    assert after.lapsed


def test_focus_entity_id_bounds_marker_sync(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture['spatial_repository'].save_design(_design(fixture))
    # The deep link carrying the holdout cell's target entity emphasizes
    # exactly the marker at that entity's acoustic reference position.
    scene = resolve_campaign_overlay(
        fixture['scene_repository'],
        fixture['spatial_repository'],
        fixture['runner_repository'],
        DOC_ID,
        focus_entity_id='mp-hold',
    )
    assert scene is not None
    by_id = {m.point_id: m for m in scene.markers}
    assert by_id['p-hold'].focused
    assert sum(1 for m in scene.markers if m.focused) == 1
    # An entity that joins no design point reports honestly, never guesses.
    scene2 = resolve_campaign_overlay(
        fixture['scene_repository'],
        fixture['spatial_repository'],
        fixture['runner_repository'],
        DOC_ID,
        focus_entity_id='no-such-entity',
    )
    assert scene2 is not None
    assert not any(m.focused for m in scene2.markers)
    assert any('結合しません' in n for n in scene2.notices)


def test_no_design_is_an_honest_empty_surface(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    scene = _overlay(fixture)
    assert scene is not None
    assert scene.design_id is None
    assert scene.markers == ()
    assert scene.shown_count == 0 and scene.total_count == 0
    assert any('no spatial design' in line for line in scene.viewport_lines)


def test_no_head_returns_none(tmp_path: Path) -> None:
    scene_repository = SceneRepository(tmp_path / 'empty.sqlite3')
    assert resolve_campaign_overlay(
        scene_repository,
        CadSpatialCampaignRepository(scene_repository),
        CadMeasurementRunnerRepository(scene_repository),
        'doc',
    ) is None


def test_explicit_design_id_selects_design(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    first = _design(fixture)
    second = build_spatial_campaign_design(
        document_id=DOC_ID,
        listening_area=first.listening_area,
        points=first.points,
        declared_at_utc='2026-10-09T01:00:00+00:00',
        scene_revision_id=fixture['revision'].revision_id,
        scene_content_hash=fixture['revision'].content_hash,
    )
    fixture['spatial_repository'].save_design(first)
    fixture['spatial_repository'].save_design(second)
    scene = resolve_campaign_overlay(
        fixture['scene_repository'],
        fixture['spatial_repository'],
        fixture['runner_repository'],
        DOC_ID,
        design_id=first.design_id,
    )
    assert scene is not None
    assert scene.design_id == first.design_id
    # Newest declared wins by default.
    default = _overlay(fixture)
    assert default is not None and default.design_id == second.design_id


# -- viewport + UI (offscreen) ---------------------------------------------


def _armed_scene(tmp_path: Path):
    fixture = _fixture(tmp_path)
    design = _design(fixture)
    fixture['spatial_repository'].save_design(design)
    plan = _runner_plan(fixture)
    fixture['runner_repository'].save_plan(plan)
    run = fixture['runner_repository'].start_run(plan.plan_id)
    fixture['runner_repository'].mark_cell(run.run_id, 0, 'staged')
    return fixture, _overlay(fixture)


def test_viewport_marker_actors_exact_xyz_and_non_pickable(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from htdt.room_viewport import RoomViewport3D, domain_to_render

    QApplication.instance() or QApplication(['htdt-test'])
    _fixture_obj, scene = _armed_scene(tmp_path)
    assert scene is not None
    viewport = RoomViewport3D()
    with viewport.deferred_render():
        viewport.render_campaign_overlay(scene)
    names = [
        name
        for name in viewport.plotter.renderer.actors
        if isinstance(name, str) and name.startswith('campaign-overlay-')
    ]
    assert any('campaign-overlay-marker-' in n for n in names)
    assert any('campaign-overlay-progress-' in n for n in names)
    assert 'campaign-overlay-status' in names
    assert 'campaign-overlay-legend' in names
    for name in names:
        actor = viewport.plotter.renderer.actors[name]
        if name in ('campaign-overlay-status', 'campaign-overlay-legend'):
            continue
        assert not actor.GetPickable(), name

    # Exact XYZ: actor placement equals domain_to_render(point).
    for index, marker in enumerate(scene.markers):
        actor = viewport.plotter.renderer.actors[
            f'campaign-overlay-marker-{index}'
        ]
        mesh = actor.GetMapper().GetInput()
        expected = domain_to_render(marker.position)
        assert tuple(mesh.GetCenter()) == pytest.approx(expected, abs=1e-6)

    viewport.clear_campaign_overlay()
    assert not [
        name
        for name in viewport.plotter.renderer.actors
        if isinstance(name, str) and name.startswith('campaign-overlay-')
    ]
    assert 'campaign-overlay-' in RoomViewport3D._OVERLAY_ACTOR_PREFIXES


def test_workspace_focus_arms_overlay(tmp_path: Path) -> None:
    """The deep link lands on the mount's focus port and arms the overlay."""
    from PySide6.QtWidgets import QApplication

    from htdt.navigation_target import NavigationTarget, NavigationTargetKind
    from htdt.room_workspace import build_room_workspace_mount

    QApplication.instance() or QApplication(['htdt-test'])
    fixture = _fixture(tmp_path)
    design = _design(fixture)
    fixture['spatial_repository'].save_design(design)

    from test_room_workspace import FakeRoomViewport

    mount = build_room_workspace_mount(
        fixture['scene_repository'],
        DOC_ID,
        viewport_factory=lambda owner: FakeRoomViewport(owner),
    )
    workspace = mount.widget
    assert NavigationTargetKind.MEASUREMENT_CAMPAIGN in mount.focus_kinds
    result = mount.focus_target(
        NavigationTarget(
            kind=NavigationTargetKind.MEASUREMENT_CAMPAIGN,
            object_ids=(design.design_id,),
        )
    )
    assert result.focused
    assert workspace.campaign_overlay.armed
    assert workspace.current_context == 'acoustics'
    resolved = workspace.campaign_overlay.resolve()
    assert resolved is not None and resolved.design_id == design.design_id


def test_measurement_button_deep_links_with_selected_cell(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from htdt.measurement_page_workspace import MeasurementPageWorkspace
    from htdt.measurement_workflow import MeasurementWorkflowController
    from htdt.cad_sweep_acquisition import (
        FakeAudioBackend,
        default_fake_scenario,
    )
    from htdt.navigation_target import NavigationTargetKind
    from htdt.workflow_navigation import WorkspaceId

    QApplication.instance() or QApplication(['htdt-test'])
    fixture = _fixture(tmp_path)
    design = _design(fixture)
    fixture['spatial_repository'].save_design(design)
    controller = MeasurementWorkflowController(
        fixture['scene_repository'], DOC_ID
    )
    plan = _runner_plan(fixture)
    fixture['runner_repository'].save_plan(plan)
    fixture['runner_repository'].start_run(plan.plan_id)

    links = []

    def capture(link) -> bool:
        links.append(link)
        return True

    workspace = MeasurementPageWorkspace(
        controller,
        acquisition_backend=FakeAudioBackend(default_fake_scenario()),
        on_navigate=capture,
    )
    assert workspace.campaign_3d_button.accessibleName() == '測定位置を3Dで確認'

    run = fixture['runner_repository'].list_runs(plan.plan_id)[-1]
    workspace._campaign_run_id = run.run_id
    workspace._refresh_campaign()
    # Selecting the holdout cell row syncs its target entity to the marker.
    holdout_row = next(
        index
        for index, cell in enumerate(plan.cells)
        if cell.target_entity_id == 'mp-hold'
    )
    workspace.campaign_table.selectRow(holdout_row)
    workspace.campaign_3d_button.click()
    assert len(links) == 1
    link = links[0]
    assert link.workspace == WorkspaceId.ROOM
    assert link.section == 'acoustics'
    assert link.kind == NavigationTargetKind.MEASUREMENT_CAMPAIGN
    assert link.entity_id == 'mp-hold'

    # No row selected → the newest spatial design id is carried instead.
    workspace.campaign_table.clearSelection()
    workspace.campaign_table.setCurrentCell(-1, -1)
    workspace.campaign_3d_button.click()
    assert len(links) == 2
    assert links[1].entity_id == design.design_id  # noqa: F821


def test_unmounted_room_registration_declares_focusable_campaign() -> None:
    """First-click deep link must resolve focusable BEFORE the room mounts.

    Regression: on a fresh session the router's capabilities came from the
    registration (empty) not the mount, so the first 「3Dで測定位置を確認」
    click degraded to request_entity and the overlay never armed.
    """
    from htdt.navigation_target import NavigationTargetKind
    from htdt.workflow_navigation import WorkspaceId
    from htdt.workflow_shell import build_canonical_workspace_registrations

    factories = {workspace_id: (lambda *a, **k: None) for workspace_id in WorkspaceId}
    registrations = build_canonical_workspace_registrations(
        factories,
        focus_kinds={
            WorkspaceId.ROOM: frozenset({
                NavigationTargetKind.SCENE_ENTITY,
                NavigationTargetKind.SCENE_REVISION,
                NavigationTargetKind.INSTALLED_EQUIPMENT_INSTANCE,
                NavigationTargetKind.MEASUREMENT_CAMPAIGN,
            }),
        },
    )
    room = next(r for r in registrations if r.workspace_id == WorkspaceId.ROOM)
    assert NavigationTargetKind.MEASUREMENT_CAMPAIGN in room.focus_kinds
    assert NavigationTargetKind.SCENE_ENTITY in room.focus_kinds


def test_measurement_button_narrow_and_dpi200(tmp_path: Path) -> None:
    """The campaign page button stays reachable at 260px and 200% text."""
    from PySide6.QtWidgets import QApplication

    from htdt.measurement_page_workspace import MeasurementPageWorkspace
    from htdt.measurement_workflow import MeasurementWorkflowController
    from htdt.cad_sweep_acquisition import (
        FakeAudioBackend,
        default_fake_scenario,
    )

    app = QApplication.instance() or QApplication(['htdt-test'])
    fixture = _fixture(tmp_path)
    fixture['spatial_repository'].save_design(_design(fixture))
    controller = MeasurementWorkflowController(
        fixture['scene_repository'], DOC_ID
    )
    workspace = MeasurementPageWorkspace(
        controller,
        acquisition_backend=FakeAudioBackend(default_fake_scenario()),
        on_navigate=lambda link: True,
    )
    workspace.campaign_3d_button.click()  # navigates; no exception
    workspace.resize(260, 700)
    workspace.show()
    app.processEvents()
    assert workspace.campaign_3d_button.isEnabled()
    assert workspace.campaign_3d_button.accessibleName()
    font = app.font()
    font.setPointSizeF(font.pointSizeF() * 2.0)
    app.setFont(font)
    workspace.resize(320, 900)
    app.processEvents()
    assert workspace.campaign_3d_button.accessibleName() == '測定位置を3Dで確認'
    workspace.close()
    workspace.deleteLater()
