"""#1010: 「運用クリアランス」 layer — operational-zone footprints + conflicts.

Covers the preview model (footprint mapping, conflict dual-highlight
geometry, UNKNOWN-vs-clear distinction, kind filtering with honest
counts, disclaimer wording), the viewport overlay lifecycle (named
non-pickable actors, legend/disclaimer texts, cleanup), the panel, and
the workspace wiring (toggle + live refresh on edit preview).
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from htdt.cad_operational_geometry import operational_clearance_conflicts
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    F1_DOCUMENT_ID,
    OperationalZone,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    make_f1_scene,
)
from htdt.room_operational_clearance import (
    OPERATIONAL_CLEARANCE_DISCLAIMER,
    OPERATIONAL_ZONE_KIND_VOCAB,
    OPERATIONAL_ZONE_KINDS,
    build_operational_clearance_preview,
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
        document_id='opclear-fixture',
        room=RoomPrism(width_m=10.0, depth_m=8.0, height_m=3.0),
        entities=entities,
    )


def _slide_zone(zone_id: str = 'zs', *, direction=(1.0, 0.0), distance=0.6):
    return OperationalZone(
        zone_id=zone_id, kind='slide_out',
        direction=direction, distance_m=distance,
    )


def _conflicting_scene() -> SceneDocument:
    """Entity A's slide-out zone reaches into entity B's footprint."""
    a = _entity('a', x=1.0, y=1.0, zones=(_slide_zone(distance=1.2),))
    b = _entity('b', x=1.9, y=1.0)
    return _scene(a, b)


# -- preview model --------------------------------------------------------------


def test_preview_maps_zone_footprints_and_kinds() -> None:
    doc = _scene(
        _entity('cab', x=2.0, y=2.0, zones=(
            _slide_zone('zs1'),
            OperationalZone(
                zone_id='sw', kind='door_swing',
                hinge_offset_m=(0.3, 0.0), angle_deg=90.0, radius_m=0.8,
            ),
        )),
        _entity('seat', x=5.0, y=5.0, zones=(
            OperationalZone(
                zone_id='rc', kind='recline',
                direction=(0.0, 1.0), distance_m=0.5,
            ),
        )),
    )
    preview = build_operational_clearance_preview(document=doc)
    assert len(preview.zones) == 3
    by_zone = {zone.zone_id: zone for zone in preview.zones}
    assert set(by_zone) == {'zs1', 'sw', 'rc'}
    # Footprints are real area polygons produced by the authority.
    for zone in preview.zones:
        assert zone.footprint is not None and zone.footprint.area > 0.01
    assert by_zone['zs1'].kind == 'slide_out'
    assert preview.total_zone_count == 3
    assert preview.filtered_zone_count == 0
    assert preview.undefined_zones == ()
    # Legend covers the used kinds only, in declaration order.
    legend_labels = [label for label, _color in preview.kind_legend]
    assert '開閉' in legend_labels and '引出し' in legend_labels
    assert 'リクライニング' in legend_labels
    assert '干渉あり' not in legend_labels  # no conflicts in this scene
    # Undeclared badge for the physical entity with no zones — there is
    # none here; both entities declare zones.
    assert preview.undeclared == ()


def test_preview_conflicts_dual_side_geometry() -> None:
    doc = _conflicting_scene()
    preview = build_operational_clearance_preview(document=doc)
    authority = operational_clearance_conflicts(doc)
    assert len(preview.conflicts) == len(authority) == 1
    conflict = preview.conflicts[0]
    assert conflict.conflict_kind == 'intersects_entity'
    assert conflict.entity_id == 'a' and conflict.zone_id == 'zs'
    assert conflict.other_entity_id == 'b'
    # BOTH sides carry display geometry: overlap region + the other
    # entity's own footprint outline.
    assert conflict.region is not None and conflict.region.area > 0
    assert conflict.other_footprint is not None and conflict.other_footprint.area > 0
    # The owning zone is flagged in conflict; the legend gains 干渉あり.
    zone = next(z for z in preview.zones if z.zone_id == 'zs')
    assert zone.in_conflict is True
    assert ('干渉あり', '#e05555') in preview.kind_legend


def test_preview_leaves_room_boundary_geometry() -> None:
    # Zone sweeps past the right wall of a 10-wide room.
    doc = _scene(_entity('edge', x=9.5, y=4.0, zones=(
        _slide_zone('zs', direction=(1.0, 0.0), distance=1.5),
    ),))
    preview = build_operational_clearance_preview(document=doc)
    conflict = next(c for c in preview.conflicts if c.conflict_kind == 'leaves_room')
    # The room-boundary side is highlighted too.
    assert conflict.region is not None
    assert conflict.boundary_geometry is not None
    assert not conflict.boundary_geometry.is_empty


def test_preview_zone_overlap_dual_zone_geometry() -> None:
    a = _entity('a', x=1.0, y=1.0, zones=(_slide_zone('za', distance=1.0),))
    b = _entity('b', x=2.0, y=1.0, zones=(
        OperationalZone(
            zone_id='zb', kind='service_access',
            direction=(-1.0, 0.0), distance_m=1.0,
        ),
    ))
    preview = build_operational_clearance_preview(document=_scene(a, b))
    kinds = {c.conflict_kind for c in preview.conflicts}
    if 'overlaps_zone' in kinds:
        conflict = next(
            c for c in preview.conflicts if c.conflict_kind == 'overlaps_zone'
        )
        assert conflict.other_zone_footprint is not None


def test_preview_unknown_zone_is_not_clear(monkeypatch) -> None:
    doc = _scene(_entity('cab', x=2.0, y=2.0, zones=(_slide_zone('zs'),)))
    import htdt.room_operational_clearance as roc

    def _broken(entity, zone):
        raise ValueError('cannot evaluate')

    monkeypatch.setattr(roc, 'operational_zone_footprint', _broken)
    preview = roc.build_operational_clearance_preview(document=doc)
    # UNKNOWN zone: not in the drawable set, not counted as clear.
    assert preview.zones == ()
    assert len(preview.undefined_zones) == 1
    assert preview.undefined_zones[0].zone_id == 'zs'
    assert preview.total_zone_count == 1
    legend_labels = [label for label, _c in preview.kind_legend]
    assert '未評価ゾーン（判定不能）' in legend_labels


def test_preview_undeclared_distinct_from_no_interference() -> None:
    doc = _scene(_entity('cab', x=2.0, y=2.0), _entity('zoned', x=6.0, y=6.0, zones=(
        OperationalZone(
            zone_id='rot', kind='rotate', radius_m=0.5, angle_deg=180.0,
        ),
    ),))
    preview = build_operational_clearance_preview(document=doc)
    assert [m.entity_id for m in preview.undeclared] == ['cab']
    assert '判定不能' in preview.summary
    legend_labels = [label for label, _c in preview.kind_legend]
    assert '未宣言（判定不能）' in legend_labels


def test_preview_kind_filter_honest_counts() -> None:
    doc = _conflicting_scene()
    preview = build_operational_clearance_preview(
        document=doc, enabled_kinds={'door_swing'}
    )
    # Slide-out zone filtered out; its conflict stays in the honest total.
    assert preview.zones == ()
    assert preview.filtered_zone_count == 1
    assert preview.total_zone_count == 1
    assert preview.conflicts == ()
    assert preview.hidden_conflict_count == 1
    assert preview.total_conflict_count == 1


def test_disclaimer_honesty_wording() -> None:
    text = OPERATIONAL_CLEARANCE_DISCLAIMER
    assert 'XY投影' in text
    assert '正確な衝突判定' in text
    assert '高さ' in text
    assert '判定不能' in text


def test_zone_kind_vocab_covers_five_kinds() -> None:
    assert tuple(OPERATIONAL_ZONE_KIND_VOCAB) == (
        'door_swing', 'recline', 'slide_out', 'service_access', 'rotate'
    )
    labels = [v[0] for v in OPERATIONAL_ZONE_KIND_VOCAB.values()]
    assert labels == ['開閉', 'リクライニング', '引出し', 'サービス', '回転']


# -- viewport overlay ------------------------------------------------------------


def _viewport():
    from PySide6.QtWidgets import QApplication

    from htdt.room_viewport import RoomViewport3D

    QApplication.instance() or QApplication(['htdt-test'])
    return RoomViewport3D()


def _opclear_actor_names(viewport) -> list[str]:
    return [
        name
        for name in viewport.plotter.renderer.actors
        if isinstance(name, str) and name.startswith('opclear-')
    ]


def test_viewport_footprint_actor_mapping_and_cleanup() -> None:
    viewport = _viewport()
    preview = build_operational_clearance_preview(document=_conflicting_scene())
    viewport.render_operational_clearance_overlay(preview)

    names = _opclear_actor_names(viewport)
    # Zone fill + outline per declared zone.
    assert 'opclear-zone-a-zs' in names
    assert 'opclear-zone-outline-a-zs' in names
    # Conflict: clash region + the OTHER side's footprint outline.
    assert 'opclear-clash-a-zs' in names
    assert 'opclear-conflict-entity-b' in names
    # Undeclared badge on entity b (physical, no zones).
    assert 'opclear-undeclared-b' in names
    # Summary / disclaimer / legend texts.
    assert 'opclear-summary' in names
    assert 'opclear-disclaimer' in names
    assert 'opclear-legend' in names
    for name in names:
        assert not viewport.plotter.renderer.actors[name].GetPickable()

    viewport.clear_operational_clearance_overlay()
    assert _opclear_actor_names(viewport) == []


def test_viewport_unknown_and_boundary_markers(monkeypatch) -> None:
    viewport = _viewport()
    import htdt.room_operational_clearance as roc

    doc = _scene(_entity('edge', x=9.5, y=4.0, zones=(
        _slide_zone('zs', direction=(1.0, 0.0), distance=1.5),
        OperationalZone(
            zone_id='bad', kind='rotate', radius_m=0.4, angle_deg=90.0,
        ),
    ),))

    original = roc.operational_zone_footprint

    def _sometimes_broken(entity, zone):
        if zone.zone_id == 'bad':
            raise ValueError('cannot evaluate')
        return original(entity, zone)

    monkeypatch.setattr(roc, 'operational_zone_footprint', _sometimes_broken)
    preview = roc.build_operational_clearance_preview(document=doc)
    viewport.render_operational_clearance_overlay(preview)
    names = _opclear_actor_names(viewport)
    # leaves_room boundary highlight + UNKNOWN bead; both non-pickable.
    assert 'opclear-boundary-edge-zs' in names
    assert 'opclear-unknown-edge-bad' in names


def test_viewport_none_clears() -> None:
    viewport = _viewport()
    viewport.render_operational_clearance_overlay(
        build_operational_clearance_preview(document=_conflicting_scene())
    )
    assert _opclear_actor_names(viewport)
    viewport.render_operational_clearance_overlay(None)
    assert _opclear_actor_names(viewport) == []


# -- panel + workspace wiring ----------------------------------------------------


def _workspace(tmp_path: Path):
    from PySide6.QtWidgets import QApplication

    from htdt.room_workspace import RoomWorkspace
    from test_room_cadux import FakeRoomViewport

    app = QApplication.instance() or QApplication(['htdt-test'])
    repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)

    class OpClearViewport(FakeRoomViewport):
        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self.opclear_calls: list = []

        def render_operational_clearance_overlay(self, preview) -> None:
            self.opclear_calls.append(preview)

        def clear_operational_clearance_overlay(self) -> None:
            self.opclear_calls.append('cleared')

    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: OpClearViewport(parent),
    )
    return app, workspace, repository


def test_workspace_toggle_renders_and_clears(tmp_path: Path) -> None:
    _app, workspace, _repository = _workspace(tmp_path)
    try:
        workspace.clearance_panel.preview_toggle.setChecked(True)
        preview = workspace.viewport.opclear_calls[-1]
        assert preview is not None
        # F1 has 4 physical entities (3 speakers + furniture), none with
        # declared zones → all surface as 未宣言, never "clear".
        assert len(preview.undeclared) == 4
        assert preview.zones == ()
        assert '未宣言4物体' in workspace.clearance_panel.counts_label.text()

        workspace.clearance_panel.preview_toggle.setChecked(False)
        assert workspace.viewport.opclear_calls[-1] is None
    finally:
        workspace.close()
        workspace.deleteLater()


def test_workspace_live_refresh_during_edit_preview(tmp_path: Path) -> None:
    _app, workspace, _repository = _workspace(tmp_path)
    try:
        workspace.clearance_panel.preview_toggle.setChecked(True)
        first = workspace.viewport.opclear_calls[-1]
        assert first is not None and first.zones == ()

        # Give furniture-left a slide-out zone via a committed edit, then
        # drag-preview a move: the overlay must reflect the PREVIEW
        # document, never the stale committed revision.
        working = workspace.controller.working
        before = working.committed_document.entity('furniture-left')
        after = before.model_copy(update={
            'operational_zones': (
                OperationalZone(
                    zone_id='zs', kind='slide_out',
                    direction=(1.0, 0.0), distance_m=0.6,
                ),
            ),
        })
        working.apply_entity_set_edit(
            replaced_before=(before,), replaced_after=(after,)
        )
        workspace.refresh()
        committed_preview = workspace.viewport.opclear_calls[-1]
        assert len(committed_preview.zones) == 1

        committed_x = committed_preview.zones[0].footprint.centroid.x
        working.begin_move('furniture-left')
        working.preview_move(Position3(x_m=1.2, y_m=2.2, z_m=0.45))
        workspace.refresh()
        live_preview = workspace.viewport.opclear_calls[-1]
        assert live_preview is not committed_preview
        moved_zone = live_preview.zones[0]
        # Footprint follows the preview position, not the committed one.
        assert moved_zone.footprint is not None
        assert moved_zone.footprint.centroid.x > committed_x + 0.3

        working.cancel_preview()
        workspace.refresh()
        reverted = workspace.viewport.opclear_calls[-1]
        assert reverted.zones[0].footprint.centroid.x == pytest.approx(
            committed_x, abs=1e-6
        )
    finally:
        workspace.close()
        workspace.deleteLater()


def test_workspace_kind_filter_reaches_preview(tmp_path: Path) -> None:
    _app, workspace, _repository = _workspace(tmp_path)
    try:
        panel = workspace.clearance_panel
        panel.preview_toggle.setChecked(True)
        # All five kind checkboxes exist with JA labels and tooltips.
        assert set(panel.kind_toggles) == set(OPERATIONAL_ZONE_KINDS)
        for kind, toggle in panel.kind_toggles.items():
            assert toggle.toolTip()
            assert toggle.focusPolicy().value != 0

        panel.kind_toggles['slide_out'].setChecked(False)
        # Untoggling re-renders: the latest call is a fresh preview object.
        assert workspace.viewport.opclear_calls[-1] is not None
        assert 'slide_out' not in panel.enabled_kinds
    finally:
        workspace.close()
        workspace.deleteLater()


# -- narrow UI / DPI / UIA --------------------------------------------------------


def test_panel_narrow_dpi200_uia() -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from htdt.room_operational_clearance_panel import (
        RoomOperationalClearancePanel,
    )

    QApplication.instance() or QApplication(['htdt-test'])
    panel = RoomOperationalClearancePanel()
    try:
        panel.resize(320, 640)
        panel.show()
        # UIA-reachable: accessible names/tooltips + keyboard focus.
        assert panel.preview_toggle.toolTip()
        assert panel.preview_toggle.focusPolicy() != Qt.NoFocus
        assert panel.disclaimer.text() == OPERATIONAL_CLEARANCE_DISCLAIMER
        assert panel.counts_label.toolTip()
        # Five kind toggles, all checkable.
        assert len(panel.kind_toggles) == 5
        panel.resize(320, 640)
        # DPI-200-style doubling: set a larger font and ensure no crash.
        font = panel.font()
        font.setPointSizeF(font.pointSizeF() * 2.0)
        panel.setFont(font)
        panel.adjustSize()
    finally:
        panel.close()
        panel.deleteLater()
