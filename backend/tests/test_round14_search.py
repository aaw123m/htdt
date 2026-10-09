"""Round 14 — list / search / filter / sort truth verification.

Each check populates a real model (candidate pages, explorer sessions,
palette providers), activates the UI control, and asserts on what the
VIEW actually shows — not on internal state.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

from htdt.cad_extended_search import (
    CadExtendedCandidate,
    CadExtendedCandidateSetPage,
)
from htdt.cad_predictions import analyze_native_rectangular_geometry
from htdt.cad_prediction_repository import CadPredictionRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_field_explorer import explorer_plane_coordinates
from htdt.cad_scene import (
    F1_DOCUMENT_ID,
    Offset3,
    Position3,
    RoomVertex,
    SceneDocument,
    SceneEntity,
    Size3,
    make_f1_scene,
    make_polygon_room,
)
from htdt.cad_search_models import CadCandidate, CadCandidateSetPage
from htdt.tree_item_role import ROLE
from htdt.optimization_workflow_workspace import (
    OptimizationWorkflowWorkspace,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _FakePlotter:
    def add_mesh(self, *_args, **_kwargs):
        return object()

    def remove_actor(self, *_args, **_kwargs) -> None:
        pass

    def add_text(self, *_args, **_kwargs) -> None:
        pass

    def render(self) -> None:
        pass


class _FakeViewport(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.plotter = _FakePlotter()

    def render_document(self, *_args, **_kwargs) -> None:
        pass


def _workspace(tmp_path) -> OptimizationWorkflowWorkspace:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    return OptimizationWorkflowWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: _FakeViewport(parent),
    )


def _candidate_page(count: int, *, offset: int = 0) -> CadCandidateSetPage:
    candidates = tuple(
        CadCandidate(
            candidate_id=f"cand-{offset + index}",
            raw_index=offset + index,
            feasible_index=offset + index,
            positions={
                "speaker-1": {
                    "x_m": 1.0 + index,
                    "y_m": 2.0,
                    "z_m": 0.5,
                }
            },
        )
        for index in range(count)
    )
    return CadCandidateSetPage(
        search_spec_id="spec-1",
        search_spec_sha256="0" * 64,
        candidate_set_sha256="1" * 64,
        raw_candidate_count=offset + count,
        feasible_candidate_count=offset + count,
        rejected_candidate_count=0,
        duplicate_candidate_count=0,
        rejection_counts={},
        offset=offset,
        limit=250,
        candidates=candidates,
    )


def _extended_page(count: int, *, offset: int = 0) -> CadExtendedCandidateSetPage:
    candidates = tuple(
        CadExtendedCandidate(
            candidate_id=f"ext-{offset + index:08x}",
            base_candidate_id=f"cand-{offset + index}",
            raw_index=offset + index,
            feasible_index=offset + index,
            positions={
                "speaker-1": {
                    "x_m": 1.0 + index,
                    "y_m": 2.0,
                    "z_m": 0.5,
                }
            },
            aim_yaw_deg={"speaker-1": float(index)},
        )
        for index in range(count)
    )
    return CadExtendedCandidateSetPage(
        extended_search_id="ext-1",
        extended_search_sha256="2" * 64,
        candidate_set_sha256="3" * 64,
        raw_candidate_count=offset + count,
        feasible_candidate_count=offset + count,
        rejected_candidate_count=0,
        offset=offset,
        limit=250,
        candidates=candidates,
    )


def _visible_items(tree):
    return [
        tree.topLevelItem(index)
        for index in range(tree.topLevelItemCount())
        if not tree.topLevelItem(index).isHidden()
    ]


# ---------------------------------------------------------------------------
# 位置候補 tree — sort truth


def test_candidate_label_column_sorts_numerically(tmp_path) -> None:
    """候補 N is a number-bearing label; sorting it must not go lexical."""
    app = _app()
    workspace = _workspace(tmp_path)
    controller = workspace.controller
    controller.search_candidate_page = _candidate_page(15)
    controller._refresh_search_candidate_tree()
    tree = controller.search_candidate_tree

    tree.sortItems(0, Qt.SortOrder.AscendingOrder)
    app.processEvents()
    labels = [tree.topLevelItem(i).text(0) for i in range(15)]
    assert labels == [f"候補 {number}" for number in range(1, 16)]

    tree.sortItems(0, Qt.SortOrder.DescendingOrder)
    app.processEvents()
    labels = [tree.topLevelItem(i).text(0) for i in range(15)]
    assert labels == [f"候補 {number}" for number in range(15, 0, -1)]

    workspace.close()
    workspace.deleteLater()


def test_candidate_sort_round_trip_between_columns(tmp_path) -> None:
    """Sort A then B then A: every activation orders honestly."""
    app = _app()
    workspace = _workspace(tmp_path)
    controller = workspace.controller
    controller.search_candidate_page = _candidate_page(12)
    controller._refresh_search_candidate_tree()
    tree = controller.search_candidate_tree

    tree.sortItems(0, Qt.SortOrder.DescendingOrder)
    app.processEvents()
    assert tree.topLevelItem(0).text(0) == "候補 12"
    tree.sortItems(1, Qt.SortOrder.AscendingOrder)
    app.processEvents()
    assert tree.topLevelItem(0).text(1) == "1"
    assert tree.topLevelItem(11).text(1) == "12"

    workspace.close()
    workspace.deleteLater()


# ---------------------------------------------------------------------------
# 位置候補 filter — page-scope honesty


def test_candidate_filter_discloses_page_scope(tmp_path) -> None:
    """The filter only sees the loaded page — the view must say so."""
    app = _app()
    workspace = _workspace(tmp_path)
    controller = workspace.controller
    # Three pages worth of feasible candidates; only the first is loaded.
    controller.search_candidate_page = _candidate_page(250)
    page = controller.search_candidate_page
    assert page.feasible_candidate_count == 250
    page = page.model_copy(update={"feasible_candidate_count": 600})
    controller.search_candidate_page = page
    controller._refresh_search_candidate_tree()
    tree = controller.search_candidate_tree

    note = controller.search_candidate_filter_note

    controller.search_candidate_filter_field.setText("候補 2")
    app.processEvents()
    visible = _visible_items(tree)
    assert visible
    assert all("候補 2" in item.text(0) for item in visible)
    assert not note.isHidden()
    assert "このページ" in note.text()

    # Zero matches on this page must not read as "no such candidate".
    controller.search_candidate_filter_field.setText("一致しない候補名")
    app.processEvents()
    assert not _visible_items(tree)
    assert not note.isHidden()
    assert "一致" in note.text()
    assert "ページ" in note.text()

    controller.search_candidate_filter_field.clear()
    app.processEvents()
    assert note.isHidden()
    assert len(_visible_items(tree)) == 250

    workspace.close()
    workspace.deleteLater()


def test_candidate_filter_single_page_states_count(tmp_path) -> None:
    """On a single page the note shows the count without page caveats."""
    app = _app()
    workspace = _workspace(tmp_path)
    controller = workspace.controller
    controller.search_candidate_page = _candidate_page(15)
    controller._refresh_search_candidate_tree()
    tree = controller.search_candidate_tree
    note = controller.search_candidate_filter_note

    controller.search_candidate_filter_field.setText("候補 3")
    app.processEvents()
    visible = _visible_items(tree)
    assert len(visible) == 1 and visible[0].text(0) == "候補 3"
    assert not note.isHidden()
    assert "1" in note.text()

    controller.search_candidate_filter_field.setText("該当なし")
    app.processEvents()
    assert not note.isHidden()
    assert "一致" in note.text()

    workspace.close()
    workspace.deleteLater()


def test_extended_candidate_filter_and_sort(tmp_path) -> None:
    """Extended tree: same filter contract; text columns sort lexically."""
    app = _app()
    workspace = _workspace(tmp_path)
    controller = workspace.controller
    controller.extended_candidate_page = _extended_page(20)
    controller._refresh_extended_candidate_tree()
    tree = controller.extended_candidate_tree
    assert tree.topLevelItemCount() == 20
    note = controller.extended_candidate_filter_note

    controller.extended_candidate_filter_field.setText("ext-0000000a")
    app.processEvents()
    visible = _visible_items(tree)
    assert len(visible) == 1
    assert visible[0].data(0, ROLE) == "ext-0000000a"
    assert not note.isHidden()

    controller.extended_candidate_filter_field.setText("候補なし")
    app.processEvents()
    assert not _visible_items(tree)
    assert not note.isHidden()
    assert "一致" in note.text()

    controller.search_candidate_filter_field.clear()
    controller.extended_candidate_filter_field.clear()
    app.processEvents()
    assert note.isHidden()
    assert len(_visible_items(tree)) == 20

    # Sorting the 音響 yaw column orders by its text deterministically.
    tree.sortItems(3, Qt.SortOrder.AscendingOrder)
    app.processEvents()
    texts = [tree.topLevelItem(i).text(3) for i in range(20)]
    assert texts == sorted(texts)

    workspace.close()
    workspace.deleteLater()


# ---------------------------------------------------------------------------
# 音場エクスプローラ — 断面/断面位置 combos must stay in sync


def _explorer_scene(document_id: str) -> SceneDocument:
    room = make_polygon_room(
        (
            RoomVertex(vertex_id="a", x_m=1.0, y_m=2.0),
            RoomVertex(vertex_id="b", x_m=5.0, y_m=2.0),
            RoomVertex(vertex_id="c", x_m=5.0, y_m=5.0),
            RoomVertex(vertex_id="d", x_m=1.0, y_m=5.0),
        ),
        height_m=2.5,
    )
    return SceneDocument(
        document_id=document_id,
        schema_version=2,
        room=room,
        entities=(
            SceneEntity(
                entity_id="speaker-fl",
                kind="speaker",
                name="FL",
                position=Position3(x_m=1.4, y_m=2.6, z_m=1.05),
                size_m=Size3(x_m=0.2, y_m=0.25, z_m=0.4),
                acoustic_reference_offset_m=Offset3(),
                speaker_role="FL",
            ),
            SceneEntity(
                entity_id="point-mlp",
                kind="measurement_point",
                name="MLP",
                position=Position3(x_m=3.0, y_m=4.0, z_m=1.1),
            ),
        ),
    )


def test_field_explorer_plane_switch_repopulates_coordinates(
    tmp_path: Path,
) -> None:
    """断面 (plane) is plane-axis bound: switching it must offer the new
    plane's fixed-axis coordinates, not the previous plane's."""
    from htdt.field_explorer_panel import FieldExplorerPanel

    app = _app()
    repository = SceneRepository(tmp_path / "cad.sqlite3")
    revision = repository.save(
        _explorer_scene("doc-explorer"), parent_revision_id=None
    ).revision
    modes, reflections = analyze_native_rectangular_geometry(
        revision, "point-mlp", max_mode_hz=150.0
    )
    prediction_repository = CadPredictionRepository(repository)
    prediction_repository.save_run((modes, reflections))

    panel = FieldExplorerPanel(
        repository, prediction_repository, revision.document_id
    )
    assert panel.open_for_run(modes.run_id) is True
    panel.mode_combo.setCurrentIndex(0)
    panel._build_session()
    # The build runs on the worker pool (#995) — pump events until the
    # session lands before asserting on its derived state.
    import time
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        app.processEvents()
        if (
            not panel._build_busy
            and len(panel._pool) == 0
            and panel._session is not None
        ):
            break
        time.sleep(0.005)
    session = panel._session
    assert session is not None

    xy_coords = [
        panel.coordinate_combo.itemData(i)
        for i in range(panel.coordinate_combo.count())
    ]
    assert tuple(xy_coords) == explorer_plane_coordinates(session, "xy")
    assert "断面を表示できません" not in panel.field_status_label.text()

    # Switch to 断面 XZ — the dropdown must now list the y-axis coordinates.
    panel.plane_combo.setCurrentIndex(1)
    app.processEvents()
    xz_coords = [
        panel.coordinate_combo.itemData(i)
        for i in range(panel.coordinate_combo.count())
    ]
    assert tuple(xz_coords) == explorer_plane_coordinates(session, "xz")
    assert xz_coords != xy_coords
    assert "断面を表示できません" not in panel.field_status_label.text()

    panel.plane_combo.setCurrentIndex(2)
    app.processEvents()
    yz_coords = [
        panel.coordinate_combo.itemData(i)
        for i in range(panel.coordinate_combo.count())
    ]
    assert tuple(yz_coords) == explorer_plane_coordinates(session, "yz")
    assert "断面を表示できません" not in panel.field_status_label.text()

    panel.close()
    panel.deleteLater()


# ---------------------------------------------------------------------------
# コマンドパレット — normalization + honest empty state


def test_palette_search_is_case_and_width_insensitive() -> None:
    from htdt.command_registry import (
        CommandContext,
        CommandDefinition,
        CommandRegistry,
    )
    from htdt.palette_search import (
        CommandPaletteProvider,
        PaletteSearchService,
        SceneEntityPaletteProvider,
    )
    from htdt.workflow_navigation import WorkspaceDeepLink, WorkspaceId

    registry = CommandRegistry()
    registry.set_deep_link_handler(lambda link: True)
    registry.register(
        CommandDefinition(
            command_id="workspace.navigate.room",
            display_name="部屋を開く",
            contexts=(CommandContext.GLOBAL,),
            keywords=("room", "画面"),
            deep_link=WorkspaceDeepLink(WorkspaceId.ROOM),
        ),
    )
    entities = (
        SimpleNamespace(
            entity_id="sp-fl",
            kind="speaker",
            name="Front Left",
            speaker_role="FL",
        ),
    )
    service = PaletteSearchService(
        (
            CommandPaletteProvider(registry),
            SceneEntityPaletteProvider(
                lambda: entities,
                lambda entity: WorkspaceDeepLink(WorkspaceId.ROOM),
            ),
        ),
    )

    for query in ("FL", "fl", "ＦＬ"):
        results = service.search(query)
        assert any(r.result_id == "entity:sp-fl" for r in results), query

    # JP display names match JP queries.
    assert any(
        r.command_id == "workspace.navigate.room" for r in service.search("部屋")
    )
    # Unrelated text must not surface the entity.
    assert not any(
        r.result_id == "entity:sp-fl" for r in service.search("シート")
    )


def test_command_palette_empty_result_state_is_honest() -> None:
    from htdt.command_palette import CommandPalette
    from htdt.command_registry import CommandRegistry

    _app()
    palette = CommandPalette(CommandRegistry())
    palette.search_field.setText("存在しない機能xyz")
    assert palette.results_list.count() == 1
    item = palette.results_list.item(0)
    assert "該当する項目がありません" in item.text()
    assert not item.flags() & Qt.ItemFlag.ItemIsSelectable
    palette.close()
    palette.deleteLater()
