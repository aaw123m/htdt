"""#1013: 3D Room lighting-scene preview — state honesty.

Covers the pure preview model (exact-bound pins, control-only listing,
4-stage distinction, unknown honesty, zone/bias/missing handling), the
viewport overlay lifecycle (named non-pickable actors, legend/disclaimer),
the Room/Video panel, and the workspace toggle wiring.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from htdt.cad_lighting import (
    ChannelState,
    LightingCommissioningRecord,
    LightingFixture,
    LightingScene,
    LightingZone,
    StageLevel,
    build_lighting_scene,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_lighting_repository import CadLightingRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
from htdt.room_lighting_preview import (
    LIGHTING_PREVIEW_DISCLAIMER,
    LIGHTING_STAGE_VOCAB,
    LIGHTING_UNKNOWN_STAGE_COLOR,
    build_lighting_scene_preview,
    stage_bead_label,
)

DOC = make_f1_scene()
# Real entity ids in the F1 document for exact bindings / bias targets.
SPEAKER = 'speaker-fl'
SCREEN_LIKE = 'furniture-left'


def _fixture(fixture_id: str, entity_id: str | None = None, **kw) -> LightingFixture:
    return LightingFixture(fixture_id=fixture_id, entity_id=entity_id, **kw)


def _scene(states=(), **kw) -> LightingScene:
    kwargs = dict(scene_id='sc-evening', version='1', label='鑑賞', states=tuple(states))
    kwargs.update(kw)
    return build_lighting_scene(**kwargs)


def _record(
    scene: LightingScene,
    ref_id: str,
    *,
    ref_kind: str = 'fixture',
    desired: float | None = None,
    commanded: float | None = None,
    read_back: float | None = None,
    measured: float | None = None,
    timestamps: bool = True,
) -> LightingCommissioningRecord:
    def slot(stage: str, level: float | None) -> StageLevel | None:
        if level is None:
            return None
        return StageLevel(
            stage=stage,
            level_percent=level,
            observed_at_utc='2026-10-08T00:00:00Z' if timestamps else None,
            source='test',
        )

    return LightingCommissioningRecord(
        record_id=f'rec-{ref_id}',
        scene_id=scene.scene_id,
        scene_version=scene.version,
        scene_sha256=scene.scene_sha256,
        ref_kind=ref_kind,
        ref_id=ref_id,
        desired=slot('desired', desired),
        commanded=slot('commanded', commanded),
        read_back=slot('read_back', read_back),
        measured=slot('measured', measured),
    )


# -- exact binding / placement -------------------------------------------------


def test_exact_bound_fixture_pins() -> None:
    scene = _scene([ChannelState(ref_kind='fixture', ref_id='fx-a', level_percent=30.0)])
    preview = build_lighting_scene_preview(
        document=DOC,
        scene=scene,
        fixtures=(_fixture('fx-a', entity_id=SPEAKER), _fixture('fx-unused')),
    )
    assert len(preview.pins) == 1
    pin = preview.pins[0]
    assert pin.fixture_id == 'fx-a'
    assert pin.entity_id == SPEAKER
    # Pin sits on the exact entity position — never a guessed point.
    assert pin.position == (1.35, 0.75, 1.05)
    assert pin.top_z_m == pytest.approx(1.05 + 0.42 / 2.0)
    # A fixture in the inventory but not referenced by the scene is not
    # part of the scene preview.
    assert 'fx-unused' not in {p.fixture_id for p in preview.pins}
    assert preview.unplaced == ()


def test_control_only_and_dangling_stay_unplaced() -> None:
    scene = _scene(
        [
            ChannelState(ref_kind='fixture', ref_id='fx-a', level_percent=30.0),
            ChannelState(ref_kind='fixture', ref_id='fx-b', level_percent=20.0),
            ChannelState(ref_kind='fixture', ref_id='fx-c', level_percent=10.0),
        ]
    )
    preview = build_lighting_scene_preview(
        document=DOC,
        scene=scene,
        fixtures=(
            _fixture('fx-a', entity_id=SPEAKER),
            _fixture('fx-b', entity_id=None),          # control-only
            _fixture('fx-c', entity_id='deleted-ent'),  # dangling binding
        ),
    )
    assert [p.fixture_id for p in preview.pins] == ['fx-a']
    reasons = {u.fixture_id: u.reason for u in preview.unplaced}
    assert reasons == {'fx-b': 'control_only', 'fx-c': 'entity_unresolved'}
    # The unplaced fixtures carry their full state vocabulary too.
    fx_b = next(u for u in preview.unplaced if u.fixture_id == 'fx-b')
    assert fx_b.states[0].level_percent == 20.0


# -- four stages stay distinct --------------------------------------------------


def test_four_stages_stay_distinct() -> None:
    """desired 30% + read_back 0% must never render as 'on'."""
    scene = _scene([ChannelState(ref_kind='fixture', ref_id='fx-a', level_percent=30.0)])
    record = _record(scene, 'fx-a', commanded=30.0, read_back=0.0)
    preview = build_lighting_scene_preview(
        document=DOC,
        scene=scene,
        fixtures=(_fixture('fx-a', entity_id=SPEAKER),),
        commissioning_records=(record,),
    )
    pin = preview.pins[0]
    by_stage = {s.stage: s for s in pin.states}
    assert tuple(by_stage) == ('desired', 'commanded', 'read_back', 'measured')
    assert by_stage['desired'].level_percent == 30.0
    assert by_stage['commanded'].level_percent == 30.0
    assert by_stage['read_back'].level_percent == 0.0
    assert by_stage['read_back'].known  # 0% is a real report, not absence
    assert not by_stage['measured'].known
    # No stage may be merged into another — the four stay separate slots.
    assert len({id(s) for s in pin.states}) == 4
    label = stage_bead_label(pin)
    assert '30%' in label and '0%' in label and '–' in label


def test_absent_stages_render_unknown_not_zero_or_desired() -> None:
    scene = _scene([ChannelState(ref_kind='fixture', ref_id='fx-a', level_percent=30.0)])
    preview = build_lighting_scene_preview(
        document=DOC,
        scene=scene,
        fixtures=(_fixture('fx-a', entity_id=SPEAKER),),
    )
    pin = preview.pins[0]
    by_stage = {s.stage: s for s in pin.states}
    assert by_stage['desired'].level_percent == 30.0
    for stage in ('commanded', 'read_back', 'measured'):
        state = by_stage[stage]
        assert state.level_percent is None
        assert not state.known
        # honesty: absent is NEVER 0.0 and never the desired value
        assert state.level_percent != 0.0
        assert state.level_percent != by_stage['desired'].level_percent


def test_record_desired_snapshot_wins_over_scene_assignment() -> None:
    scene = _scene([ChannelState(ref_kind='fixture', ref_id='fx-a', level_percent=30.0)])
    record = _record(scene, 'fx-a', desired=55.0, read_back=55.0)
    preview = build_lighting_scene_preview(
        document=DOC,
        scene=scene,
        fixtures=(_fixture('fx-a', entity_id=SPEAKER),),
        commissioning_records=(record,),
    )
    assert preview.pins[0].stage('desired').level_percent == 55.0


def test_foreign_scene_records_are_ignored() -> None:
    scene = _scene([ChannelState(ref_kind='fixture', ref_id='fx-a', level_percent=30.0)])
    foreign = _scene([ChannelState(ref_kind='fixture', ref_id='fx-a', level_percent=80.0)], scene_id='sc-other')
    record = _record(foreign, 'fx-a', commanded=80.0, read_back=80.0)
    preview = build_lighting_scene_preview(
        document=DOC,
        scene=scene,
        fixtures=(_fixture('fx-a', entity_id=SPEAKER),),
        commissioning_records=(record,),
    )
    pin = preview.pins[0]
    assert preview.ignored_records == 1
    for stage in ('commanded', 'read_back', 'measured'):
        assert not pin.stage(stage).known


def test_stale_readback_is_flagged() -> None:
    scene = _scene([ChannelState(ref_kind='fixture', ref_id='fx-a', level_percent=30.0)])
    record = _record(scene, 'fx-a', read_back=30.0, timestamps=False)
    preview = build_lighting_scene_preview(
        document=DOC,
        scene=scene,
        fixtures=(_fixture('fx-a', entity_id=SPEAKER),),
        commissioning_records=(record,),
    )
    assert preview.pins[0].stage('read_back').stale


# -- zones / missing refs -------------------------------------------------------


def test_zone_members_expand_and_multi_assign_flags() -> None:
    zone_bias = LightingZone(
        zone_id='z-bias', role='bias',
        member_fixture_ids=('fx-a', 'fx-b'),
        bias_target_entity_id=SCREEN_LIKE,
    )
    zone_path = LightingZone(
        zone_id='z-path', role='path',
        member_fixture_ids=('fx-a',),
    )
    scene = _scene(
        [
            ChannelState(ref_kind='zone', ref_id='z-bias', level_percent=40.0),
            ChannelState(ref_kind='zone', ref_id='z-path', level_percent=10.0),
        ]
    )
    preview = build_lighting_scene_preview(
        document=DOC,
        scene=scene,
        fixtures=(
            _fixture('fx-a', entity_id=SPEAKER),
            _fixture('fx-b', entity_id=None),
        ),
        zones=(zone_bias, zone_path),
    )
    pin_a = preview.pins[0]
    # Desired comes from the first scene-order assignment (bias 40%).
    assert pin_a.stage('desired').level_percent == 40.0
    assert pin_a.multi_assigned
    assert set(pin_a.assigning_refs) == {'zone:z-bias', 'zone:z-path'}
    assert set(pin_a.zone_roles) == {'bias', 'path'}
    # fx-b (control-only) still carries the zone desired state in the list.
    fx_b = preview.unplaced[0]
    assert fx_b.fixture_id == 'fx-b'
    assert fx_b.stage('desired').level_percent == 40.0
    assert fx_b.zone_roles == ('bias',)
    # Zone-level rows stay zone-level — never fanned out per device.
    assert {z.zone_id for z in preview.zone_rows} == {'z-bias', 'z-path'}


def test_bias_pin_links_to_screen_entity() -> None:
    zone = LightingZone(
        zone_id='z-bias', role='bias',
        member_fixture_ids=('fx-a',),
        bias_target_entity_id=SCREEN_LIKE,
    )
    scene = _scene([ChannelState(ref_kind='zone', ref_id='z-bias', level_percent=40.0)])
    preview = build_lighting_scene_preview(
        document=DOC,
        scene=scene,
        fixtures=(_fixture('fx-a', entity_id=SPEAKER),),
        zones=(zone,),
    )
    pin = preview.pins[0]
    assert pin.bias_target_entity_id == SCREEN_LIKE
    assert pin.bias_target_position == (0.55, 2.2, 0.45)


def test_missing_refs_surface_instead_of_silence() -> None:
    zone = LightingZone(zone_id='z-x', role='accent', member_fixture_ids=('fx-ghost',))
    empty_zone = LightingZone(zone_id='z-empty', role='general', member_fixture_ids=())
    scene = _scene(
        [
            ChannelState(ref_kind='fixture', ref_id='fx-missing', level_percent=50.0),
            ChannelState(ref_kind='zone', ref_id='z-missing', level_percent=50.0),
            ChannelState(ref_kind='zone', ref_id='z-x', level_percent=50.0),
            ChannelState(ref_kind='zone', ref_id='z-empty', level_percent=50.0),
        ]
    )
    preview = build_lighting_scene_preview(
        document=DOC, scene=scene, fixtures=(), zones=(zone, empty_zone),
    )
    assert 'fixture:fx-missing' in preview.missing_refs
    assert 'zone:z-missing' in preview.missing_refs
    assert 'zone:z-x/member:fx-ghost' in preview.missing_refs
    assert 'zone:z-empty(メンバーなし)' in preview.missing_refs
    assert preview.pins == ()


def test_zone_level_record_stays_at_zone_level() -> None:
    zone = LightingZone(zone_id='z-bias', role='bias', member_fixture_ids=('fx-a',), bias_target_entity_id=SCREEN_LIKE)
    scene = _scene([ChannelState(ref_kind='zone', ref_id='z-bias', level_percent=40.0)])
    record = _record(scene, 'z-bias', ref_kind='zone', read_back=35.0)
    preview = build_lighting_scene_preview(
        document=DOC, scene=scene,
        fixtures=(_fixture('fx-a', entity_id=SPEAKER),),
        zones=(zone,),
        commissioning_records=(record,),
    )
    # A zone-level read-back is zone evidence — the member pin's read_back
    # stays unknown rather than borrowing it.
    assert not preview.pins[0].stage('read_back').known
    zone_row = preview.zone_rows[0]
    assert zone_row.states[2].stage == 'read_back'
    assert zone_row.states[2].level_percent == 35.0
    assert not zone_row.states[3].known  # measured stays unknown


def test_capability_unsupported_marks() -> None:
    scene = _scene(
        [
            ChannelState(
                ref_kind='fixture', ref_id='fx-a',
                level_percent=50.0, cct_k=3000, color_rgb=(10, 20, 30),
            )
        ]
    )
    fixture = LightingFixture(
        fixture_id='fx-a', entity_id=SPEAKER,
        controllable_level=False, color_capable=False,
    )
    preview = build_lighting_scene_preview(
        document=DOC, scene=scene, fixtures=(fixture,),
    )
    unsupported = preview.pins[0].stage('desired').unsupported
    assert 'level' in unsupported and 'cct' in unsupported and 'color' in unsupported


# -- legend / honesty -----------------------------------------------------------


def test_legend_disclaimer_states_not_photometric() -> None:
    scene = _scene([ChannelState(ref_kind='fixture', ref_id='fx-a', level_percent=30.0)])
    preview = build_lighting_scene_preview(
        document=DOC, scene=scene, fixtures=(_fixture('fx-a', entity_id=SPEAKER),),
    )
    labels = [label for label, _c in preview.stage_legend]
    assert len(labels) == 5  # four stages + unknown
    assert '目標' in labels[0] and '送信' in labels[1]
    assert '応答' in labels[2] and '実測' in labels[3]
    assert '不明' in labels[4]
    assert '説明記号' in preview.disclaimer
    assert '照度' in preview.disclaimer
    assert '実測照度ではありません' in preview.disclaimer
    assert '送信しません' in preview.disclaimer
    # level_percent never becomes lux — the model carries no lux field.
    assert not hasattr(preview.pins[0].states[0], 'lux')
    assert 'lux' in LIGHTING_PREVIEW_DISCLAIMER


def test_zone_legend_only_lists_used_roles() -> None:
    zone = LightingZone(zone_id='z-bias', role='bias', member_fixture_ids=('fx-a',), bias_target_entity_id=SCREEN_LIKE)
    scene = _scene([ChannelState(ref_kind='zone', ref_id='z-bias', level_percent=40.0)])
    preview = build_lighting_scene_preview(
        document=DOC, scene=scene,
        fixtures=(_fixture('fx-a', entity_id=SPEAKER),), zones=(zone,),
    )
    assert [label for label, _c in preview.zone_legend] == ['バイアス照明']


# -- viewport overlay ------------------------------------------------------------


def _viewport():
    from PySide6.QtWidgets import QApplication

    from htdt.room_viewport import RoomViewport3D

    QApplication.instance() or QApplication(['htdt-test'])
    return RoomViewport3D()


def _viewport_preview() -> tuple:
    zone = LightingZone(
        zone_id='z-bias', role='bias',
        member_fixture_ids=('fx-a', 'fx-b'),
        bias_target_entity_id=SCREEN_LIKE,
    )
    zone_path = LightingZone(
        zone_id='z-path', role='path',
        member_fixture_ids=('fx-a',),
    )
    scene = _scene(
        [
            ChannelState(ref_kind='zone', ref_id='z-bias', level_percent=40.0),
            ChannelState(ref_kind='zone', ref_id='z-path', level_percent=10.0),
            ChannelState(ref_kind='fixture', ref_id='fx-c', level_percent=25.0),
        ]
    )
    record = _record(scene, 'fx-a', commanded=40.0, read_back=0.0)
    preview = build_lighting_scene_preview(
        document=DOC,
        scene=scene,
        fixtures=(
            _fixture('fx-a', entity_id=SPEAKER),
            _fixture('fx-b', entity_id=None),
            _fixture('fx-c', entity_id='speaker-c'),
        ),
        zones=(zone, zone_path),
        commissioning_records=(record,),
    )
    return preview


def _lighting_actor_names(viewport) -> list[str]:
    return [
        name
        for name in viewport.plotter.renderer.actors
        if isinstance(name, str) and name.startswith('lighting-')
    ]


def test_viewport_overlay_actors_and_cleanup() -> None:
    viewport = _viewport()
    preview = _viewport_preview()
    viewport.render_lighting_scene_preview(preview)

    names = _lighting_actor_names(viewport)
    assert 'lighting-anchor-fx-a' in names
    for stage in ('desired', 'commanded', 'read_back', 'measured'):
        assert f'lighting-bead-fx-a-{stage}' in names
    assert 'lighting-multi-fx-a' in names  # fx-a is also in the scene via zone
    # add_point_labels registers a point + a label actor under the name.
    assert 'lighting-label-fx-a-labels' in names
    assert 'lighting-bias-fx-a' in names   # bias link to the screen entity
    assert 'lighting-anchor-fx-c' in names
    assert 'lighting-scene-summary' in names
    assert 'lighting-scene-unplaced' in names
    assert 'lighting-scene-disclaimer' in names
    assert 'lighting-scene-legend' in names
    for name in names:
        actor = viewport.plotter.renderer.actors[name]
        assert not actor.GetPickable()

    viewport.clear_lighting_scene_preview()
    assert _lighting_actor_names(viewport) == []


def test_viewport_unknown_beads_render_dim() -> None:
    viewport = _viewport()
    preview = _viewport_preview()
    viewport.render_lighting_scene_preview(preview)
    actors = viewport.plotter.renderer.actors
    # fx-c has no commissioning record: 3 stages are unknown dim beads,
    # desired stays a real stage color.
    assert actors['lighting-bead-fx-c-desired'].GetProperty().GetOpacity() > 0.9
    for stage in ('commanded', 'read_back', 'measured'):
        actor = actors[f'lighting-bead-fx-c-{stage}']
        assert actor.GetProperty().GetOpacity() < 0.5


def test_viewport_overlay_prefix_swept_by_overlay_removal() -> None:
    viewport = _viewport()
    viewport.render_lighting_scene_preview(_viewport_preview())
    assert _lighting_actor_names(viewport)
    viewport._remove_overlay_actors()
    assert _lighting_actor_names(viewport) == []


def test_viewport_render_none_clears() -> None:
    viewport = _viewport()
    viewport.render_lighting_scene_preview(_viewport_preview())
    assert _lighting_actor_names(viewport)
    viewport.render_lighting_scene_preview(None)
    assert _lighting_actor_names(viewport) == []


# -- panel -----------------------------------------------------------------------


def _panel():
    from PySide6.QtWidgets import QApplication

    from htdt.room_lighting_panel import RoomLightingPreviewPanel

    QApplication.instance() or QApplication(['htdt-test'])
    return RoomLightingPreviewPanel()


def test_panel_toggle_signal_and_lists() -> None:
    panel = _panel()
    seen: list[bool] = []
    panel.previewToggled.connect(seen.append)
    panel.preview_toggle.setChecked(True)
    assert seen == [True]
    assert panel.preview_enabled

    scene = _scene([ChannelState(ref_kind='fixture', ref_id='fx-b', level_percent=20.0)])
    panel.show_scene(scene)
    assert 'sc-evening' in panel.scene_summary.text()
    preview = build_lighting_scene_preview(
        document=DOC, scene=scene, fixtures=(_fixture('fx-b'),),
    )
    panel.show_preview(preview)
    assert '3D未配置1件' in panel.counts_label.text()
    assert panel.unplaced_list.count() == 1
    assert '説明記号' in panel.disclaimer.text()


def test_panel_no_scene_is_honest() -> None:
    panel = _panel()
    panel.show_scene(None)
    assert '未選択' in panel.scene_summary.text()
    panel.show_preview(None)
    assert panel.unplaced_list.count() == 0


def test_panel_narrow_ui_and_uia_reachable() -> None:
    from PySide6.QtCore import Qt

    panel = _panel()
    try:
        panel.resize(320, 640)
        panel.show()
        for widget in (
            panel.preview_toggle,
            panel.unplaced_list,
        ):
            assert not widget.isHidden()
            assert widget.accessibleName() or widget.toolTip() or widget.text()
            assert widget.focusPolicy() != Qt.FocusPolicy.NoFocus
        # 200%-DPI proxy (doubled point size): controls keep their
        # accessible surface and the disclaimer wraps instead of clipping.
        font = panel.font()
        font.setPointSize(max(1, font.pointSize() * 2))
        panel.setFont(font)
        from PySide6.QtWidgets import QApplication

        QApplication.processEvents()
        assert panel.disclaimer.wordWrap()
    finally:
        panel.close()
        panel.deleteLater()


# -- workspace wiring -------------------------------------------------------------


def _workspace(tmp_path: Path):
    from PySide6.QtWidgets import QApplication

    from htdt.room_workspace import RoomWorkspace
    from test_room_cadux import FakeRoomViewport

    app = QApplication.instance() or QApplication(['htdt-test'])
    repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)

    class LightingViewport(FakeRoomViewport):
        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self.lighting_calls: list = []

        def render_lighting_scene_preview(self, preview) -> None:
            self.lighting_calls.append(preview)

        def clear_lighting_scene_preview(self) -> None:
            self.lighting_calls.append('cleared')

    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: LightingViewport(parent),
    )
    return app, workspace, repository


def test_workspace_toggle_renders_and_clears(tmp_path: Path) -> None:
    _app, workspace, repository = _workspace(tmp_path)
    try:
        # Persist + select the scene so the preview resolves an authority.
        scene = _scene(
            [ChannelState(ref_kind='fixture', ref_id='fx-a', level_percent=30.0)]
        )
        lighting_repo = CadLightingRepository(repository)
        lighting_repo.save_scene(scene, F1_DOCUMENT_ID)
        lighting_repo.select_scene(F1_DOCUMENT_ID, scene)
        workspace.lighting_inventory_provider = lambda: (
            (_fixture('fx-a', entity_id=SPEAKER),),
            (),
            (_record(scene, 'fx-a', read_back=0.0),),
        )
        workspace._lighting_scene_cache = None

        workspace.lighting_panel.preview_toggle.setChecked(True)
        preview = workspace.viewport.lighting_calls[-1]
        assert preview is not None
        assert [p.fixture_id for p in preview.pins] == ['fx-a']
        assert preview.pins[0].stage('read_back').level_percent == 0.0
        assert preview.pins[0].stage('measured').known is False
        assert '3D未配置0件' in workspace.lighting_panel.counts_label.text()

        workspace.lighting_panel.preview_toggle.setChecked(False)
        assert workspace.viewport.lighting_calls[-1] is None
    finally:
        workspace.close()
        workspace.deleteLater()


def test_workspace_no_scene_preview_is_none(tmp_path: Path) -> None:
    _app, workspace, _repository = _workspace(tmp_path)
    try:
        workspace.lighting_panel.preview_toggle.setChecked(True)
        assert workspace.viewport.lighting_calls[-1] is None
        assert '未選択' in workspace.lighting_panel.scene_summary.text()
    finally:
        workspace.close()
        workspace.deleteLater()
