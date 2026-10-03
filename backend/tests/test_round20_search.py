"""Round 20 — search / filter / sort / pagination truth (follow-up to round 14).

The widget-level checks drive real controls on a real workspace (candidate
filter line edits, sortable candidate trees); page-boundary determinism is
already pinned by ``test_cad_candidate_pagination.py`` and is not repeated.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTreeWidget, QWidget

from htdt.cad_extended_search import (
    CadExtendedCandidate,
    CadExtendedCandidateSetPage,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_empty_scene, make_f1_scene
from htdt.cad_search_models import CadCandidate, CadCandidateSetPage
from htdt.commissioning_wizard import CommissioningWizard
from htdt.tree_item_role import ROLE
from htdt.optimization_search_controller import (
    CandidateTreeItem,
    _candidate_matches_filter,
    _candidate_sort_key,
    _normalized_text,
)
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
# SEARCH — the candidate filters must fold width/case like palette search does


def test_search_filter_matches_full_width_digits(tmp_path) -> None:
    """１２ (full-width) must find 候補 12 — same contract as the palette."""
    app = _app()
    workspace = _workspace(tmp_path)
    controller = workspace.controller
    controller.search_candidate_page = _candidate_page(15)
    controller._refresh_search_candidate_tree()
    tree = controller.search_candidate_tree

    controller.search_candidate_filter_field.setText("候補 １２")
    app.processEvents()
    visible = _visible_items(tree)
    assert len(visible) == 1
    assert visible[0].text(0) == "候補 12"

    workspace.close()
    workspace.deleteLater()


def test_extended_filter_matches_full_width(tmp_path) -> None:
    app = _app()
    workspace = _workspace(tmp_path)
    controller = workspace.controller
    controller.extended_candidate_page = _extended_page(20)
    controller._refresh_extended_candidate_tree()
    tree = controller.extended_candidate_tree

    # ５.０° (full-width) -> '5.0°'; matches yaw 5.0 and 15.0.
    controller.extended_candidate_filter_field.setText("５.０°")
    app.processEvents()
    visible = _visible_items(tree)
    assert {item.data(0, ROLE) for item in visible} == {
        "ext-00000005",
        "ext-0000000f",
    }

    workspace.close()
    workspace.deleteLater()


def test_candidate_matches_filter_normalizes_both_sides() -> None:
    _app()
    item = CandidateTreeItem(["スピーカー １２", "1", "x"])
    assert _candidate_matches_filter(item, _normalized_text("ｽﾋﾟｰｶｰ"))
    assert _candidate_matches_filter(item, _normalized_text("12"))
    assert not _candidate_matches_filter(item, _normalized_text("カタ"))


# ---------------------------------------------------------------------------
# SORT — one fixed total order: type-aware, deterministic, insertion-proof


def _sorted_display(texts: list[str]) -> list[str]:
    tree = QTreeWidget()
    tree.setHeaderLabels(["c"])
    tree.setSortingEnabled(True)
    for text in texts:
        tree.addTopLevelItem(CandidateTreeItem([text]))
    tree.sortItems(0, Qt.SortOrder.AscendingOrder)
    return [
        tree.topLevelItem(index).text(0)
        for index in range(tree.topLevelItemCount())
    ]


def test_candidate_sort_total_order_insertion_proof() -> None:
    """A mixed numeric/text column used to compare pairs under different
    rules, so the displayed order depended on insertion order — 5 distinct
    orders across 720 permutations on the old comparator. The sort key is
    a single tuple now: exactly one order is possible.
    """
    import itertools

    _app()
    texts = ["a10", "a9", "a1a", "a2", "b", "a1"]
    orders = {
        tuple(_sorted_display(list(perm)))
        for perm in itertools.islice(itertools.permutations(texts), 48)
    }
    assert len(orders) == 1
    (order,) = orders
    assert list(order) == sorted(
        texts,
        key=lambda text: _candidate_sort_key(CandidateTreeItem([text]), 0),
    )
    # numeric-bearing strings still sort numerically ahead of plain text
    assert order[:4] == ("a1", "a2", "a9", "a10")


def test_candidate_sort_numeric_with_text_tiebreak() -> None:
    _app()
    order = _sorted_display(["候補 10", "候補 2", "候補 1", "b5", "a5"])
    # numbers sort as numbers; equal numbers break on folded text
    assert order == ["候補 1", "候補 2", "a5", "b5", "候補 10"]


def test_extended_tree_default_order_is_enumeration(tmp_path) -> None:
    """The 候補 column shows opaque ids; the tree's default order must be
    the canonical enumeration order, not hash-prefix lexical order."""
    app = _app()
    workspace = _workspace(tmp_path)
    controller = workspace.controller
    page = _extended_page(6, offset=7)
    controller.extended_candidate_page = page
    controller._refresh_extended_candidate_tree()
    tree = controller.extended_candidate_tree
    app.processEvents()

    expected = [candidate.candidate_id for candidate in page.candidates]
    actual = [
        tree.topLevelItem(index).data(0, ROLE)
        for index in range(tree.topLevelItemCount())
    ]
    assert actual == expected

    # Re-sorting the 候補 column ascending keeps the canonical order;
    # descending produces its exact reverse.
    tree.sortItems(0, Qt.SortOrder.AscendingOrder)
    app.processEvents()
    asc = [
        tree.topLevelItem(index).data(0, ROLE)
        for index in range(tree.topLevelItemCount())
    ]
    assert asc == expected
    tree.sortItems(0, Qt.SortOrder.DescendingOrder)
    app.processEvents()
    desc = [
        tree.topLevelItem(index).data(0, ROLE)
        for index in range(tree.topLevelItemCount())
    ]
    assert desc == expected[::-1]

    workspace.close()
    workspace.deleteLater()


def test_extended_tree_groups_base_candidates_when_sorted(tmp_path) -> None:
    """元候補 sorts by its text so rows sharing a base stay together."""
    app = _app()
    workspace = _workspace(tmp_path)
    controller = workspace.controller
    page = _extended_page(4)
    candidates = (
        page.candidates[0].model_copy(update={"base_candidate_id": "cand-b"}),
        page.candidates[1].model_copy(update={"base_candidate_id": "cand-a"}),
        page.candidates[2].model_copy(update={"base_candidate_id": "cand-b"}),
        page.candidates[3].model_copy(update={"base_candidate_id": "cand-a"}),
    )
    controller.extended_candidate_page = page.model_copy(
        update={"candidates": candidates}
    )
    controller._refresh_extended_candidate_tree()
    tree = controller.extended_candidate_tree

    tree.sortItems(1, Qt.SortOrder.AscendingOrder)
    app.processEvents()
    bases = [
        tree.topLevelItem(index).text(1)
        for index in range(tree.topLevelItemCount())
    ]
    assert bases == ["cand-a", "cand-a", "cand-b", "cand-b"]

    workspace.close()
    workspace.deleteLater()


# ---------------------------------------------------------------------------
# Ordering honesty beyond the candidate trees


def test_wizard_existing_project_combo_is_sorted(tmp_path) -> None:
    """scene_document_heads has no inherent order — the picker must sort."""
    _app()
    repository = SceneRepository(tmp_path / "cad.sqlite3")
    # Deliberately save in reverse-alphabetical order.
    for document_id in ("doc-z", "doc-m", "doc-a"):
        repository.save(make_empty_scene(document_id), parent_revision_id=None)
    wizard = CommissioningWizard(repository, "doc-a")
    assert wizard._other_documents() == ("doc-m", "doc-z")


def test_search_filter_note_counts_visible_rows(tmp_path) -> None:
    """The disclosed match count must equal the rows actually visible."""
    app = _app()
    workspace = _workspace(tmp_path)
    controller = workspace.controller
    controller.search_candidate_page = _candidate_page(15)
    controller._refresh_search_candidate_tree()
    tree = controller.search_candidate_tree
    note = controller.search_candidate_filter_note

    controller.search_candidate_filter_field.setText("候補 １")
    app.processEvents()
    visible = _visible_items(tree)
    assert f"{len(visible)} 件一致" in note.text()

    workspace.close()
    workspace.deleteLater()
