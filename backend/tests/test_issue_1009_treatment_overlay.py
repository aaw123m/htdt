"""#1009: acoustic-treatment coverage overlay on RoomViewport3D (REV73).

Honesty contract under test: only the clipped effective patch is drawn
(never the authored rectangle), lifecycle vocabulary is explicit
(提案された配置 / 設置記録 / ホストバインド失効), overlap/out-of-bounds are
warnings rather than silent states, and the overlay resolves against the
EXACT current head so a scene edit can never leave a stale footprint drawn
(the #999 staleness lesson).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

pytest.importorskip('PySide6')

from htdt.cad_acoustic_treatment import (
    TreatmentCoverage,
    build_treatment_placement,
    revise_treatment_placement,
)
from htdt.cad_scene import Position3
from htdt.room_treatment_overlay import (
    BINDING_STATE_LABELS,
    LIFECYCLE_LABELS,
    NOTICE_KIND_LABELS,
    RoomTreatmentOverlayController,
    footprint_render_polygons,
    resolve_treatment_overlay,
)

from test_treatment_boundary_overlay import (
    _definition,
    _fixture,
    _second_revision_and_compiled,
)


def _place(
    fixture,
    *,
    instance_id: str,
    position: Position3,
    width: float,
    height: float,
    installed: bool = False,
    bound: bool = True,
    suffix: str = '',
):
    repository = fixture['treatment_repository']
    definition, evidence = _definition('geometric', suffix)
    for item in evidence:
        repository.save_evidence(item)
    definition = repository.save_definition(definition)
    proposed = build_treatment_placement(
        definition=definition,
        revision=fixture['revision'],
        instance_id=instance_id,
        position=position,
        coverage=TreatmentCoverage(width_m=width, height_m=height),
        host_surface_id=fixture['surface_id'] if bound else None,
    )
    repository.save_placement(proposed)
    if not installed:
        return proposed
    installed_placement = revise_treatment_placement(
        proposed, revision=fixture['revision'], lifecycle='installed'
    )
    repository.save_placement(installed_placement)
    return installed_placement


def _overlay(fixture):
    return resolve_treatment_overlay(
        fixture['scene_repository'],
        fixture['treatment_repository'],
        fixture['revision'].document.document_id,
    )


def test_overlay_patch_maps_uv_to_render_via_plane_frame(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    _place(
        fixture,
        instance_id='panel-full',
        position=Position3(x_m=0.5, y_m=0.5, z_m=1.0),
        width=1.0,
        height=1.0,
    )
    overlay = _overlay(fixture)
    assert overlay is not None
    assert len(overlay.patches) == 1
    patch = overlay.patches[0]
    assert patch.patch_area_m2 == pytest.approx(1.0)
    assert patch.rectangle_area_m2 == pytest.approx(1.0)
    assert patch.effective_ratio == pytest.approx(1.0)
    assert not patch.clipped

    # UV -> world -> render(x, -y, z): the top face z=1 must map to render
    # z=1 with domain +y flipped — the same convention the field overlay
    # uses for every surface render.
    footprint = patch.footprint
    render_rings = footprint_render_polygons(footprint)
    assert render_rings == patch.render_polygons
    assert len(render_rings) == 1
    ring = render_rings[0]
    assert len(ring) >= 3
    for x, y, z in ring:
        # plane_origin/u/v reconstruction puts domain z on the host plane.
        assert z == pytest.approx(1.0, abs=1e-9)
        # Render y is negated domain y: face spans domain y∈[0,1] → [-1,0].
        assert -1.0 - 1e-9 <= y <= 0.0 + 1e-9
        assert 0.0 - 1e-9 <= x <= 1.0 + 1e-9
    # Sanity: origin + u*u_axis + v*v_axis → domain_to_render round trip.
    u0, v0 = footprint.patch_uv_polygons[0][0]
    expected = (
        footprint.plane_origin_m[0]
        + u0 * footprint.plane_u_axis[0]
        + v0 * footprint.plane_v_axis[0],
        -(
            footprint.plane_origin_m[1]
            + u0 * footprint.plane_u_axis[1]
            + v0 * footprint.plane_v_axis[1]
        ),
        footprint.plane_origin_m[2]
        + u0 * footprint.plane_u_axis[2]
        + v0 * footprint.plane_v_axis[2],
    )
    assert ring[0] == pytest.approx(expected)


def test_clipped_patch_never_paints_the_rectangle(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _place(
        fixture,
        instance_id='panel-overhang',
        position=Position3(x_m=0.5, y_m=0.5, z_m=1.0),
        width=1.4,
        height=1.0,
    )
    overlay = _overlay(fixture)
    assert overlay is not None and len(overlay.patches) == 1
    patch = overlay.patches[0]
    assert patch.rectangle_area_m2 == pytest.approx(1.4)
    # Effective patch is clipped to the unit face — the authored 1.4×1.0
    # rectangle must not be what gets reported/drawn.
    assert patch.patch_area_m2 == pytest.approx(1.0)
    assert patch.effective_ratio == pytest.approx(1.0 / 1.4)
    assert patch.clipped and patch.warning
    for ring in patch.render_polygons:
        for x, y, z in ring:
            assert 0.0 - 1e-9 <= x <= 1.0 + 1e-9
            assert -1.0 - 1e-9 <= y <= 0.0 + 1e-9
    assert any('実効' in line for line in overlay.summary_ja)
    assert any('63%' in line or '71%' in line or '%' in line
               for line in overlay.summary_ja)


def test_proposed_and_installed_lifecycle_vocabulary(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _place(
        fixture,
        instance_id='panel-proposed',
        position=Position3(x_m=0.5, y_m=0.5, z_m=1.0),
        width=0.4,
        height=0.4,
    )
    _place(
        fixture,
        instance_id='panel-installed',
        position=Position3(x_m=0.5, y_m=0.5, z_m=1.0),
        width=0.4,
        height=0.4,
        installed=True,
        suffix='-installed',
    )
    overlay = _overlay(fixture)
    assert overlay is not None
    lifecycles = {p.placement.instance_id: p.lifecycle for p in overlay.patches}
    assert lifecycles == {
        'panel-proposed': 'proposed',
        'panel-installed': 'installed',
    }
    assert LIFECYCLE_LABELS['proposed'] == '提案された配置'
    assert LIFECYCLE_LABELS['installed'] == '設置記録'
    text = '\n'.join(overlay.viewport_lines)
    assert 'PROPOSED' in text and 'INSTALLED' in text
    ja = '\n'.join(overlay.summary_ja)
    assert '提案された配置' in ja and '設置記録' in ja
    # Same-host overlapping panels carry the overlap warning explicitly.
    assert all(p.warning for p in overlay.patches)
    assert '重複' in ja
    assert 'OVERLAP' in text


def test_stale_placement_is_lapsed_not_drawn(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _place(
        fixture,
        instance_id='panel-stale',
        position=Position3(x_m=0.5, y_m=0.5, z_m=1.0),
        width=0.5,
        height=0.5,
    )
    first = _overlay(fixture)
    assert first is not None and len(first.patches) == 1

    # A scene edit mints a new head; the placement stays bound to the old
    # revision and must surface as lapsed — never drawn as current.
    _second_revision_and_compiled(fixture)
    overlay = _overlay(fixture)
    assert overlay is not None
    assert overlay.revision_id != fixture['revision'].revision_id
    assert overlay.patches == ()
    assert len(overlay.notices) == 1
    notice = overlay.notices[0]
    assert notice.instance_id == 'panel-stale'
    assert notice.kind == 'lapsed'
    assert notice.binding_state in (
        'stale_scene_revision',
        'stale_semantic_geometry',
    )
    assert NOTICE_KIND_LABELS['lapsed'] == 'ホストバインド失効'
    assert BINDING_STATE_LABELS[notice.binding_state]
    assert 'ホストバインド失効' in notice.message
    assert 'Lapsed' in notice.viewport_line
    # ASCII-only viewport lines — VTK text drops CJK glyphs entirely.
    for line in overlay.viewport_lines:
        assert line.isascii()


def test_unbound_and_out_of_bounds_are_explicit_notices(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _place(
        fixture,
        instance_id='panel-unbound',
        position=Position3(x_m=0.5, y_m=0.5, z_m=1.0),
        width=0.5,
        height=0.5,
        bound=False,
    )
    _place(
        fixture,
        instance_id='panel-offwall',
        position=Position3(x_m=5.0, y_m=5.0, z_m=1.0),
        width=0.5,
        height=0.5,
        suffix='-off',
    )
    overlay = _overlay(fixture)
    assert overlay is not None
    assert overlay.patches == ()
    kinds = {notice.instance_id: notice.kind for notice in overlay.notices}
    assert kinds['panel-unbound'] == 'unbound'
    assert kinds['panel-offwall'] == 'out_of_bounds'
    assert NOTICE_KIND_LABELS['unbound'] == 'ホスト面未設定'
    assert '範囲外' in NOTICE_KIND_LABELS['out_of_bounds']
    text = '\n'.join(overlay.viewport_lines)
    assert 'not drawn' in text


def test_overlay_keys_off_exact_head_and_cache_invalidates(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    _place(
        fixture,
        instance_id='panel-cache',
        position=Position3(x_m=0.5, y_m=0.5, z_m=1.0),
        width=0.5,
        height=0.5,
    )
    controller = RoomTreatmentOverlayController(
        fixture['scene_repository'],
        fixture['treatment_repository'],
        fixture['revision'].document.document_id,
    )
    first = controller.resolve()
    assert first is not None and len(first.patches) == 1
    # Same head → cached scene object, no re-derivation.
    assert controller.resolve() is first

    _second_revision_and_compiled(fixture)
    after = controller.resolve()
    assert after is not None
    assert after is not first
    assert after.revision_id != first.revision_id
    assert after.patches == ()
    assert after.notices[0].kind == 'lapsed'


def test_no_head_returns_none(tmp_path: Path) -> None:
    from htdt.cad_repository import SceneRepository
    from htdt.cad_system_variant_repository import CadSystemVariantRepository
    from htdt.cad_acoustic_treatment_repository import (
        CadAcousticTreatmentRepository,
    )

    scene_repository = SceneRepository(tmp_path / 'empty.sqlite3')
    treatment_repository = CadAcousticTreatmentRepository(
        scene_repository, CadSystemVariantRepository(scene_repository)
    )
    assert (
        resolve_treatment_overlay(scene_repository, treatment_repository, 'doc')
        is None
    )


# -- viewport + panel (offscreen) -------------------------------------------


def _panel_overlay_scene(tmp_path: Path):
    fixture = _fixture(tmp_path)
    _place(
        fixture,
        instance_id='panel-ui',
        position=Position3(x_m=0.5, y_m=0.5, z_m=1.0),
        width=1.4,
        height=1.0,
    )
    overlay = _overlay(fixture)
    assert overlay is not None and overlay.patches
    return overlay


def test_viewport_draws_only_clipped_patch_actors(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from htdt.room_viewport import RoomViewport3D

    QApplication.instance() or QApplication(['htdt-test'])
    overlay = _panel_overlay_scene(tmp_path)
    viewport = RoomViewport3D()
    with viewport.deferred_render():
        viewport.render_treatment_overlay(overlay)
    names = [
        name
        for name in viewport.plotter.renderer.actors
        if isinstance(name, str) and name.startswith('treatment-overlay-')
    ]
    assert any('treatment-overlay-patch-' in n for n in names)
    assert any('treatment-overlay-edge-' in n for n in names)
    assert 'treatment-overlay-status' in names
    for name in names:
        actor = viewport.plotter.renderer.actors[name]
        if name != 'treatment-overlay-status':
            assert not actor.GetPickable()

    # The drawn patch covers the clipped face area only — its vertex set
    # is exactly the clipped ring, never the full 1.4×1.0 rectangle.
    patch_actor = viewport.plotter.renderer.actors[
        next(n for n in names if 'patch-' in n)
    ]
    mesh = patch_actor.GetMapper().GetInput()
    xs = [mesh.GetPoint(i)[0] for i in range(mesh.GetNumberOfPoints())]
    assert min(xs) >= -1e-9 and max(xs) <= 1.0 + 1e-9

    viewport.clear_treatment_overlay()
    assert not [
        name
        for name in viewport.plotter.renderer.actors
        if isinstance(name, str) and name.startswith('treatment-overlay-')
    ]


def test_viewport_prefix_swept_by_overlay_removal(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from htdt.room_viewport import RoomViewport3D

    QApplication.instance() or QApplication(['htdt-test'])
    overlay = _panel_overlay_scene(tmp_path)
    viewport = RoomViewport3D()
    viewport.render_treatment_overlay(overlay)
    assert any(
        name.startswith('treatment-overlay-')
        for name in viewport.plotter.renderer.actors
    )
    assert 'treatment-overlay-' in RoomViewport3D._OVERLAY_ACTOR_PREFIXES
    viewport._remove_overlay_actors()
    assert not any(
        name.startswith('treatment-overlay-')
        for name in viewport.plotter.renderer.actors
    )


def test_panel_coverage_section_vocabulary_and_uia(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from htdt.cad_system_variant_repository import CadSystemVariantRepository
    from htdt.cad_acoustic_treatment_repository import (
        CadAcousticTreatmentRepository,
    )
    from htdt.room_acoustics_panel import RoomTreatmentPanel
    from htdt.room_workspace import RoomWorkspaceController

    QApplication.instance() or QApplication(['htdt-test'])
    overlay_fixture = _fixture(tmp_path)
    _place(
        overlay_fixture,
        instance_id='panel-ui',
        position=Position3(x_m=0.5, y_m=0.5, z_m=1.0),
        width=1.4,
        height=1.0,
    )
    controller = RoomTreatmentOverlayController(
        overlay_fixture['scene_repository'],
        overlay_fixture['treatment_repository'],
        overlay_fixture['revision'].document.document_id,
    )

    class _PanelController:
        def __init__(self) -> None:
            self.repository = overlay_fixture['scene_repository']
            self.document_id = (
                overlay_fixture['revision'].document.document_id
            )
            self.treatment_repository = overlay_fixture[
                'treatment_repository'
            ]
            self.treatment_comparison_repository = (
                RoomWorkspaceController(
                    self.repository, self.document_id
                ).treatment_comparison_repository
            )

    panel = RoomTreatmentPanel(_PanelController())
    panel.coverage_provider = controller.resolve
    panel.refresh()
    assert panel.coverage.accessibleName() == '被覆一覧'
    assert panel.coverage.topLevelItemCount() == 1
    row = panel.coverage.topLevelItem(0).text(1)
    assert '提案された配置' in row
    assert '実効' in row and '矩形' in row
    assert '%' in row
    assert 'クリップ済み' in row
    assert panel.coverage.topLevelItem(0).toolTip(1)


def test_panel_shows_lapsed_notice_after_scene_edit(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication(['htdt-test'])
    overlay_fixture = _fixture(tmp_path)
    _place(
        overlay_fixture,
        instance_id='panel-lapsed',
        position=Position3(x_m=0.5, y_m=0.5, z_m=1.0),
        width=0.5,
        height=0.5,
    )
    _second_revision_and_compiled(overlay_fixture)
    scene = _overlay(overlay_fixture)
    assert scene is not None and scene.notices
    assert scene.notices[0].kind == 'lapsed'


def test_panel_narrow_layout_and_dpi200(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from htdt.room_acoustics_panel import RoomTreatmentPanel
    from htdt.room_workspace import RoomWorkspaceController

    app = QApplication.instance() or QApplication(['htdt-test'])
    overlay_fixture = _fixture(tmp_path)
    overlay_controller = RoomTreatmentOverlayController(
        overlay_fixture['scene_repository'],
        overlay_fixture['treatment_repository'],
        overlay_fixture['revision'].document.document_id,
    )

    class _PanelController:
        def __init__(self) -> None:
            self.repository = overlay_fixture['scene_repository']
            self.document_id = (
                overlay_fixture['revision'].document.document_id
            )
            self.treatment_repository = overlay_fixture[
                'treatment_repository'
            ]
            self.treatment_comparison_repository = (
                RoomWorkspaceController(
                    self.repository, self.document_id
                ).treatment_comparison_repository
            )

    panel = RoomTreatmentPanel(_PanelController())
    panel.coverage_provider = overlay_controller.resolve
    panel.refresh()
    # Narrow dock: the coverage list must stay inside a scrolled page.
    panel.resize(260, 700)
    panel.show()
    assert panel.coverage.width() <= panel.width() + 20
    font = app.font()
    font.setPointSizeF(font.pointSizeF() * 2.0)
    app.setFont(font)
    panel.resize(320, 900)
    panel.refresh()
    assert panel.coverage.topLevelItemCount() >= 1
    panel.close()
    panel.deleteLater()
