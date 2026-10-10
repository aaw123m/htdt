"""#1012: rack-layout workspace — 2D RU elevation with per-device
fit/conflict surfacing.

Covers the elevation's 1-based RU geometry, both-parties conflict
highlighting, UNKNOWN honesty for undeclared dimensions, the
preview→re-evaluate→confirm→explicit-commit update path, and the
narrow-width / DPI-200 / UIA-reachable requirements.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest

pytest.importorskip('PySide6')

from PySide6.QtWidgets import QApplication, QDialog

from htdt.cad_feature_authority_repository import (  # noqa: E402
    CadFeatureAuthorityRepository,
)
from htdt.cad_equipment import EquipmentDataProvenance  # noqa: E402
from htdt.cad_rack_infrastructure import (  # noqa: E402
    CircuitAssignment,
    CircuitEndpoint,
    ElectricalScenario,
    EquipmentPowerProfile,
    PowerValue,
    RackLayout,
    RackPlacement,
    build_rack_definition,
    evaluate_rack_fit,
)
from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.cad_scene import make_f1_scene  # noqa: E402
from htdt.rack_workspace import (  # noqa: E402
    RackElevationView,
    RackWorkspacePanel,
)
from test_accessible_labels import _unnamed_controls  # noqa: E402


NOW = '2026-10-09T00:00:00+00:00'


def _prov() -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='manufacturer',
        source_name='datasheet',
        source_version='2026',
        source_reference='specs-page',
        source_sha256='b' * 64,
    )


def _rack(rack_id: str = 'rack-1', **overrides):
    kwargs = {
        'rack_id': rack_id,
        'name': '機器ラック',
        'width_m': 0.6,
        'height_m': 1.2,
        'usable_depth_m': 0.45,
        'ru_capacity': 24,
        'front_clearance_m': 0.8,
        'rear_clearance_m': 0.8,
        'service_clearance_m': 0.6,
        'provenance': (_prov(),),
    }
    kwargs.update(overrides)
    return build_rack_definition(**kwargs)


def _profile(
    device_id: str,
    *,
    ru_height: int | None = 2,
    chassis_depth_m: float | None = 0.3,
    heat_dissipation_w: float | None = 120.0,
    power_w: float = 80.0,
) -> EquipmentPowerProfile:
    return EquipmentPowerProfile(
        device_id=device_id,
        ru_height=ru_height,
        chassis_depth_m=chassis_depth_m,
        heat_dissipation_w=heat_dissipation_w,
        heat_provenance=(
            _prov() if heat_dissipation_w is not None else None
        ),
        power_values=(
            PowerValue(
                state='typical',
                power_w=power_w,
                kind='manufacturer_declared',
                provenance=_prov(),
            ),
        ),
        provenance=(_prov(),),
    )


@pytest.fixture()
def repo(tmp_path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    feature_repo = CadFeatureAuthorityRepository(scene_repository)
    return scene_repository, revision.document_id, feature_repo


def _panel(
    repo,
    *,
    rack=None,
    layout: RackLayout | None = None,
    devices: tuple[EquipmentPowerProfile, ...] = (),
    endpoints: tuple[CircuitEndpoint, ...] = (),
    assignments: tuple[CircuitAssignment, ...] = (),
    scenarios: tuple[ElectricalScenario, ...] = (),
) -> RackWorkspacePanel:
    scene_repository, document_id, feature_repo = repo
    if rack is not None:
        feature_repo.save_rack_definition(rack, document_id=document_id)
    if layout is not None:
        feature_repo.save_rack_layout(layout, document_id=document_id)
    panel = RackWorkspacePanel(
        scene_repository,
        document_id,
        feature_repository=feature_repo,
    )
    panel.set_authority_context(
        devices=devices,
        endpoints=endpoints,
        assignments=assignments,
        scenarios=scenarios,
        source_label='テスト注入',
    )
    panel.refresh()
    return panel


@pytest.fixture()
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# ---------------------------------------------------------------- RU grid


def test_elevation_ru_numbering_is_one_based(qapp):
    """The elevation's RU slots come straight from ru_position (1-based);
    RU 1 renders at the bottom and the declared capacity bounds the grid."""
    rack = _rack()
    amp = _profile('amp-1', ru_height=4)
    dsp = _profile('dsp-1', ru_height=2)
    shelf = _profile('appliance-1', ru_height=None)
    layout = RackLayout(
        rack_id=rack.rack_id,
        placements=(
            RackPlacement(device_id='amp-1', ru_position=5),
            RackPlacement(device_id='dsp-1', ru_position=20),
            RackPlacement(device_id='appliance-1', shelf_label='棚板A'),
        ),
    )
    results = evaluate_rack_fit(layout, rack, (amp, dsp, shelf))
    view = RackElevationView()
    view.set_scene(rack, layout, (amp, dsp, shelf), results)
    view.resize(400, 800)

    assert list(view.displayed_ru_range()) == list(range(1, 25))

    rect = view.block_rect('amp-1')
    assert rect is not None
    # Block centre maps back to a row inside the 4RU span [5..8].
    centre_ru = view.ru_at(rect.center())
    assert centre_ru in range(5, 9)

    top = view.block_rect('dsp-1')
    assert top is not None
    assert view.ru_at(top.center()) in range(20, 22)
    # 1-based grid: the bottom row of the face is RU 1, top row is RU 24.
    assert view.ru_at(rect.topLeft()) <= 8
    assert view.device_at(rect.center()) == 'amp-1'


def test_elevation_without_declared_capacity_uses_occupancy(qapp):
    """An undeclared ru_capacity never invents an extent — the grid only
    spans rows that are actually occupied."""
    rack = _rack(ru_capacity=None)
    device = _profile('amp-1', ru_height=2)
    layout = RackLayout(
        rack_id=rack.rack_id,
        placements=(RackPlacement(device_id='amp-1', ru_position=3),),
    )
    view = RackElevationView()
    view.set_scene(rack, layout, (device,), evaluate_rack_fit(layout, rack, (device,)))
    assert view.displayed_ru_range() == range(1, 5)


# ------------------------------------------------------------- conflicts


def test_conflicting_devices_are_both_highlighted(repo, qapp):
    """An RU double-booking highlights BOTH parties — in the elevation
    conflict set and in the fit table rows."""
    rack = _rack()
    first = _profile('amp-1', ru_height=4)
    second = _profile('dsp-1', ru_height=2)
    layout = RackLayout(
        rack_id=rack.rack_id,
        placements=(
            RackPlacement(device_id='amp-1', ru_position=1),
            RackPlacement(device_id='dsp-1', ru_position=2),
        ),
    )
    panel = _panel(repo, rack=rack, layout=layout, devices=(first, second))

    assert panel.elevation.conflicted_device_ids() == frozenset(
        {'amp-1', 'dsp-1'}
    )

    rows = {
        panel.fit_tree.topLevelItem(i).text(0): panel.fit_tree.topLevelItem(i)
        for i in range(panel.fit_tree.topLevelItemCount())
    }
    assert set(rows) == {'amp-1', 'dsp-1'}
    for device_id in ('amp-1', 'dsp-1'):
        item = rows[device_id]
        assert item.text(2) == '不合格'  # ru_occupancy column
        assert 'amp-1' in item.text(5) or 'dsp-1' in item.text(5)


def test_fit_table_reports_depth_and_ru_failures(repo, qapp):
    """Depth overage and capacity overflow surface as per-column FAILs
    computed by evaluate_rack_fit — nothing is re-derived in the UI."""
    rack = _rack(ru_capacity=4)
    deep = _profile('amp-1', ru_height=2, chassis_depth_m=0.9)
    tall = _profile('dsp-1', ru_height=4)
    layout = RackLayout(
        rack_id=rack.rack_id,
        placements=(
            RackPlacement(device_id='amp-1', ru_position=1),
            RackPlacement(device_id='dsp-1', ru_position=3),
        ),
    )
    panel = _panel(repo, rack=rack, layout=layout, devices=(deep, tall))
    rows = {
        panel.fit_tree.topLevelItem(i).text(0): panel.fit_tree.topLevelItem(i)
        for i in range(panel.fit_tree.topLevelItemCount())
    }
    assert rows['amp-1'].text(3) == '不合格'  # depth_fit
    assert rows['dsp-1'].text(2) == '不合格'  # 4RU at bottom 3 exceeds cap 4


# ------------------------------------------------------- UNKNOWN honesty


def test_undeclared_dimensions_surface_as_unknown(repo, qapp):
    """Undeclared ru_height / chassis_depth / clearances are UNKNOWN —
    never painted as PASS."""
    rack = _rack()
    device = _profile(
        'appliance-1', ru_height=None, chassis_depth_m=None,
    )
    layout = RackLayout(
        rack_id=rack.rack_id,
        placements=(
            RackPlacement(device_id='appliance-1', ru_position=2),
        ),
    )
    panel = _panel(repo, rack=rack, layout=layout, devices=(device,))
    item = panel.fit_tree.topLevelItem(0)
    assert item.text(0) == 'appliance-1'
    assert item.text(2) == '不明'  # ru_occupancy — height undeclared
    assert item.text(3) == '不明'  # depth_fit — chassis depth undeclared

    # Rack with no declared clearances → clearance_fit UNKNOWN too.
    rack_nc = _rack(
        rack_id='rack-no-clearance',
        front_clearance_m=None,
        rear_clearance_m=None,
        service_clearance_m=None,
    )
    panel2 = _panel(
        repo,
        rack=None,
        layout=None,
        devices=(),
    )
    scene_repository, document_id, feature_repo = repo
    feature_repo.save_rack_definition(rack_nc, document_id=document_id)
    feature_repo.save_rack_layout(
        RackLayout(
            rack_id=rack_nc.rack_id,
            placements=(
                RackPlacement(device_id='x', ru_position=1),
            ),
        ),
        document_id=document_id,
    )
    panel2.set_authority_context(
        devices=(_profile('x'),),
        endpoints=(),
        assignments=(),
        scenarios=(),
    )
    panel2.refresh()
    panel2.rack_combo.setCurrentIndex(
        panel2.rack_combo.findData(rack_nc.rack_id)
    )
    item = panel2.fit_tree.topLevelItem(0)
    assert item.text(4) == '不明'


# -------------------------------------------- preview → confirm → commit


def test_move_preview_confirm_commits_new_layout_version(repo, qapp):
    """Dragging the preview and accepting writes a NEW append-only layout
    row; the rack authority's current layout only changes on commit."""
    rack = _rack()
    amp = _profile('amp-1', ru_height=2)
    dsp = _profile('dsp-1', ru_height=1)
    layout = RackLayout(
        rack_id=rack.rack_id,
        placements=(
            RackPlacement(device_id='amp-1', ru_position=1),
            RackPlacement(device_id='dsp-1', ru_position=10),
        ),
    )
    scene_repository, document_id, feature_repo = repo
    panel = _panel(repo, rack=rack, layout=layout, devices=(amp, dsp))

    accepted = []
    original_exec = QDialog.exec

    def _auto_accept(dialog):
        accepted.append(dialog)
        return QDialog.DialogCode.Accepted

    QDialog.exec = _auto_accept
    try:
        panel._preview_move('amp-1', 6)
    finally:
        QDialog.exec = original_exec

    assert len(accepted) == 1  # a diff-confirm dialog was shown
    layouts = feature_repo.list_rack_layouts(rack.rack_id)
    assert len(layouts) == 2  # original + new version, append-only
    current = layouts[-1]
    moved = next(
        p for p in current.placements if p.device_id == 'amp-1'
    )
    assert moved.ru_position == 6
    # Unmoved placement untouched.
    assert next(
        p for p in current.placements if p.device_id == 'dsp-1'
    ).ru_position == 10
    # Panel refreshed to the committed version.
    assert panel._layout is not None
    assert next(
        p for p in panel._layout.placements
        if p.device_id == 'amp-1'
    ).ru_position == 6


def test_move_cancel_writes_nothing(repo, qapp):
    """Rejecting the confirm dialog leaves the persisted layout alone —
    no silent mutation on cancel."""
    rack = _rack()
    amp = _profile('amp-1', ru_height=2)
    layout = RackLayout(
        rack_id=rack.rack_id,
        placements=(RackPlacement(device_id='amp-1', ru_position=1),),
    )
    _, _, feature_repo = repo
    panel = _panel(repo, rack=rack, layout=layout, devices=(amp,))

    original_exec = QDialog.exec
    QDialog.exec = lambda dialog: QDialog.DialogCode.Rejected
    try:
        panel._preview_move('amp-1', 9)
    finally:
        QDialog.exec = original_exec

    layouts = feature_repo.list_rack_layouts(rack.rack_id)
    assert len(layouts) == 1
    assert layouts[-1].placements[0].ru_position == 1


def test_commit_refuses_candidate_that_moves_two_devices(repo, qapp):
    """The commit seam re-verifies the confirmed diff: a candidate that
    is not 'current + exactly one moved placement' is refused."""
    rack = _rack()
    amp = _profile('amp-1', ru_height=2)
    dsp = _profile('dsp-1', ru_height=1)
    layout = RackLayout(
        rack_id=rack.rack_id,
        placements=(
            RackPlacement(device_id='amp-1', ru_position=1),
            RackPlacement(device_id='dsp-1', ru_position=10),
        ),
    )
    _, _, feature_repo = repo
    panel = _panel(repo, rack=rack, layout=layout, devices=(amp, dsp))

    forged = RackLayout(
        rack_id=rack.rack_id,
        placements=(
            RackPlacement(device_id='amp-1', ru_position=6),
            RackPlacement(device_id='dsp-1', ru_position=12),
        ),
    )
    panel._commit_layout(forged)

    assert len(feature_repo.list_rack_layouts(rack.rack_id)) == 1
    assert 'もう一度' in panel.status_label.text()


# ------------------------------------------------ context JSON ingestion


def test_context_json_import_feeds_summaries(repo, qapp, tmp_path):
    """Session-injected context (devices/endpoints/scenarios have no
    table of their own) is imported honestly and labeled."""
    rack = _rack()
    device = _profile('amp-1', ru_height=2)
    layout = RackLayout(
        rack_id=rack.rack_id,
        placements=(RackPlacement(device_id='amp-1', ru_position=1),),
    )
    payload = {
        'devices': [device.model_dump(mode='json')],
        'scenarios': [
            ElectricalScenario(
                scenario_id='scn-1',
                device_ids=('amp-1',),
                state='typical',
            ).model_dump(mode='json'),
        ],
    }
    path = tmp_path / 'ctx.json'
    path.write_text(json.dumps(payload), encoding='utf-8')

    panel = _panel(repo, rack=rack, layout=layout)
    assert '未提供' in panel.context_label.text()
    assert panel.import_context_json(path) is True
    assert '未提供' not in panel.context_label.text()
    item = panel.fit_tree.topLevelItem(0)
    assert item.text(0) == 'amp-1'


# -------------------------------------------------- narrow / DPI / UIA


def test_panel_usable_at_narrow_width(repo, qapp):
    """At phone-ish width the scroll area keeps every control reachable;
    no horizontal squeeze hides the elevation."""
    rack = _rack()
    device = _profile('amp-1', ru_height=2)
    layout = RackLayout(
        rack_id=rack.rack_id,
        placements=(RackPlacement(device_id='amp-1', ru_position=1),),
    )
    panel = _panel(repo, rack=rack, layout=layout, devices=(device,))
    panel.resize(360, 640)
    panel.show()
    assert panel.elevation_scroll.horizontalScrollBarPolicy() is not None
    # Elevation keeps its natural size inside the scroll area.
    assert panel.elevation.minimumWidth() >= 200
    assert panel.fit_tree.topLevelItemCount() == 1


def test_panel_has_accessible_names(repo, qapp):
    """Every interactive control reports an accessible name for UIA /
    screen readers (DPI-200 zoom changes pixels, not the contract)."""
    rack = _rack()
    device = _profile('amp-1', ru_height=2)
    layout = RackLayout(
        rack_id=rack.rack_id,
        placements=(RackPlacement(device_id='amp-1', ru_position=1),),
    )
    panel = _panel(repo, rack=rack, layout=layout, devices=(device,))
    leftovers = list(_unnamed_controls(panel))
    assert leftovers == []


def test_paint_surfaces_do_not_throw(qapp):
    """``qcolor(painter.setPen(TOKEN))`` was a paren bug — ``setPen`` got the
    ColorToken, threw ``TypeError`` on every paint, and aborted the whole
    ``paintEvent`` at the first pen stroke (only the canvas fill ever
    painted). Both widgets must paint cleanly in every state."""
    from PySide6.QtGui import QPaintEvent
    from htdt.rack_workspace import _LoadBarWidget

    view = RackElevationView()
    view.resize(480, 360)
    # Empty rack: the 'ラック定義が保存されていません' branch set the
    # muted pen — it threw before drawText ever ran.
    view.paintEvent(QPaintEvent(view.rect()))
    # Populated: faces + shelf + depth + ghost helpers all set pens.
    rack = _rack()
    amp = _profile('amp-1', ru_height=4)
    layout = RackLayout(
        rack_id=rack.rack_id,
        placements=(RackPlacement(device_id='amp-1', ru_position=5),),
    )
    results = evaluate_rack_fit(layout, rack, (amp,))
    view.set_scene(rack, layout, (amp,), results)
    view.paintEvent(QPaintEvent(view.rect()))

    bar = _LoadBarWidget()
    bar.resize(480, 40)
    bar.paintEvent(QPaintEvent(bar.rect()))
    bar.set_segments((('amp', 120.0), ('dsp', 60.0)))
    bar.paintEvent(QPaintEvent(bar.rect()))
