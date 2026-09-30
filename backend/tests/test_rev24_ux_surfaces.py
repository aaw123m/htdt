"""REV24-UX surface-localization regression tests.

User-facing surfaces must render Japanese display labels — never raw
internal identifiers (preference keys, enum values, navigation kinds,
diagnostic codes, authority vocabulary, entity kinds). Each test also pins
the label maps' coverage so a new enum member or key that ships unmapped
is caught here instead of leaking an id into a Japanese sentence.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import get_args

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QGroupBox,
    QLabel,
    QWidget,
)

from htdt.application_preferences import (
    PREFERENCE_DEFINITIONS,
    ApplicationPreferenceStore,
    PreferenceLoadState,
)
from htdt.authority_graph import (
    AuthorityDomain,
    AuthorityLifecycle,
    AuthorityNode,
    StaticAuthoritySource,
    build_authority_graph,
)
from htdt.authority_inspector_ui import AuthorityInspectorDialog
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import EntityKind
from htdt.capture_retention_ui import _DEPENDENT_KIND_LABELS
from htdt.geometry_import_dialog import (
    GeometryImportDialog,
    _ACOUSTIC_VOLUME_LABELS,
    _ISSUE_ACTION_LABELS,
    _ISSUE_CODE_LABELS,
    _REPAIR_CHECKBOXES,
    _REPAIR_OPERATION_LABELS,
    _REPAIR_RISK_LABELS,
    _unresolved_finding_label,
)
from htdt.navigation_target import (
    NavigationResolver,
    NavigationTarget,
    NavigationTargetKind,
    _NAVIGATION_KIND_LABELS,
)
from htdt.raw_mesh_health import (
    _ISSUE_TEXT,
    _REPAIR_RISK,
    classify_repair_operation,
)
from htdt.room_workspace import SelectionInspector
from htdt.workflow_application import WorkflowApplicationComposition
from htdt.workflow_navigation import (
    ApplicationDestinationId,
    CANONICAL_WORKSPACE_CONTEXTS,
    WorkspaceId,
    destination_label,
    workspace_context_label,
)
from htdt.workflow_settings import (
    PreferencesWidget,
    _LOAD_STATE_LABELS,
    _PREFERENCE_DESCRIPTIONS,
    _PREFERENCE_LABELS,
)
from htdt.workflow_shell import (
    WorkspaceMount,
    WorkspaceRegistration,
    WorkspaceRouter,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


# --- Preferences tab ---------------------------------------------------------


def test_preference_labels_cover_every_registered_key() -> None:
    """A new preference key must ship its Japanese name + description."""
    keys = set(PREFERENCE_DEFINITIONS)
    assert keys <= set(_PREFERENCE_LABELS)
    assert keys <= set(_PREFERENCE_DESCRIPTIONS)


def test_preferences_render_japanese_names_and_enum_values(
    tmp_path: Path,
) -> None:
    app = _app()
    store = ApplicationPreferenceStore(tmp_path / "preferences.json")
    widget = PreferencesWidget(store)

    texts = [label.text() for label in widget.findChildren(QLabel)]
    for key in _PREFERENCE_LABELS:
        assert key not in texts, f"raw key leaked: {key}"
    for key in PREFERENCE_DEFINITIONS:
        expected = _PREFERENCE_LABELS[key]
        assert any(
            text.startswith(expected) for text in texts
        ), f"missing JA label for {key}"

    widget.close()
    widget.deleteLater()
    app.processEvents()


def test_preferences_enum_combo_shows_japanese_values(tmp_path: Path) -> None:
    _app()
    store = ApplicationPreferenceStore(tmp_path / "preferences.json")
    widget = PreferencesWidget(store)

    combo = widget._editors["compute.preferred_backend"]
    assert isinstance(combo, QComboBox)
    rendered = [combo.itemText(i) for i in range(combo.count())]
    assert rendered == ["自動", "CPU", "GPU（検証済みのみ）"]
    # The committed value stays the canonical enum id.
    assert [combo.itemData(i) for i in range(combo.count())] == [
        "auto",
        "cpu",
        "gpu_when_validated",
    ]

    language = widget._editors["general.language"]
    assert language.itemText(0) == "システムに従う"
    assert language.itemText(1) == "日本語"
    assert language.itemText(2) == "English"

    widget.close()
    widget.deleteLater()


def test_preferences_refused_banner_localizes_load_state(
    tmp_path: Path,
) -> None:
    _app()
    path = tmp_path / "preferences.json"
    path.write_text("{ not json", encoding="utf-8")
    store = ApplicationPreferenceStore(path)
    assert store.load_state == PreferenceLoadState.CORRUPT
    assert not store.write_allowed

    widget = PreferencesWidget(store)
    assert "破損" in widget.status.text()
    assert "corrupt" not in widget.status.text()
    widget.close()
    widget.deleteLater()


def test_load_state_labels_cover_write_refused_states() -> None:
    assert PreferenceLoadState.CORRUPT in _LOAD_STATE_LABELS
    assert PreferenceLoadState.INCOMPATIBLE_NEWER_SCHEMA in _LOAD_STATE_LABELS
    assert PreferenceLoadState.PARTIAL_INVALID_VALUE in _LOAD_STATE_LABELS


# --- Navigation messages ------------------------------------------------------


def test_navigation_kind_labels_cover_every_kind() -> None:
    assert set(NavigationTargetKind) == set(_NAVIGATION_KIND_LABELS)


def test_unsupported_resolution_message_uses_japanese_kind() -> None:
    resolution = NavigationResolver().resolve(
        NavigationTarget(kind=NavigationTargetKind.OPERATING_PRESET),
        registered=set(),
        capabilities={},
    )
    assert resolution.status == "unsupported"
    assert "運用プリセット" in (resolution.message or "")
    assert "operating_preset" not in (resolution.message or "")


def test_focus_message_uses_japanese_kind() -> None:
    app = _app()
    registrations = [
        WorkspaceRegistration(
            workspace_id=workspace_id,
            label=str(workspace_id.value),
            factory=lambda: WorkspaceMount.from_widget(QWidget()),
        )
        for workspace_id in WorkspaceId
    ]
    router = WorkspaceRouter(registrations)
    result = router.focus_target(
        WorkspaceId.ROOM,
        NavigationTarget(
            kind=NavigationTargetKind.OPERATING_PRESET,
            object_ids=("preset-1",),
        ),
    )
    assert result.focused is False
    assert (
        result.message
        == "運用プリセットの個別フォーカスはこの画面では未対応です"
    )
    router.close()
    router.deleteLater()
    app.processEvents()


def test_destination_and_context_labels_are_japanese() -> None:
    assert destination_label(WorkspaceId.ROOM) == "部屋"
    assert destination_label(ApplicationDestinationId.LIBRARY) == "ライブラリ"
    # Canonical ids map; a foreign-workspace section still gets its JA
    # name; an unknown id falls back to itself rather than guessing.
    assert (
        workspace_context_label(WorkspaceId.ROOM, "placement")
        == "スピーカー・座席"
    )
    assert (
        workspace_context_label(WorkspaceId.OPTIMIZATION, "placement")
        == "スピーカー・座席"
    )
    assert workspace_context_label(WorkspaceId.ROOM, "nope") == "nope"
    # Every canonical context id is covered by construction.
    for contexts in CANONICAL_WORKSPACE_CONTEXTS.values():
        for context in contexts:
            assert (
                workspace_context_label("overview", context.context_id)
                == context.label
            )


# --- Authority inspector ------------------------------------------------------


def _inspector_graph():
    return build_authority_graph(
        [
            StaticAuthoritySource(
                [
                    AuthorityNode(
                        node_id="room:document:doc-1",
                        domain=AuthorityDomain.ROOM,
                        node_type="document",
                        label="Document doc-1",
                        lifecycle=AuthorityLifecycle.CURRENT,
                    ),
                    AuthorityNode(
                        node_id="room:scene_revision:rev-1",
                        domain=AuthorityDomain.ROOM,
                        node_type="scene_revision",
                        label="SceneRevision rev-1",
                        lifecycle=AuthorityLifecycle.HISTORICAL,
                    ),
                    AuthorityNode(
                        node_id="measurement:measurement:m-1",
                        domain=AuthorityDomain.MEASUREMENT,
                        node_type="measurement",
                        label="測点 m-1 (measured)",
                        lifecycle=AuthorityLifecycle.MEASURED,
                    ),
                ]
            )
        ]
    )


def test_authority_inspector_renders_japanese_summary() -> None:
    app = _app()
    dialog = AuthorityInspectorDialog(
        _inspector_graph(), initial_node_id="room:scene_revision:rev-1"
    )

    combo_texts = [
        dialog.node_combo.itemText(i) for i in range(dialog.node_combo.count())
    ]
    assert "[部屋] ドキュメント doc-1" in combo_texts
    assert "[部屋] シーンリビジョン rev-1" in combo_texts
    assert "[測定] 測点 m-1（実測）" in combo_texts
    assert not any("Document" in t or "SceneRevision" in t for t in combo_texts)

    assert dialog.authority_class_label.text() == "部屋・シーンリビジョン"
    assert dialog.lifecycle_label.text() == "履歴"
    assert dialog.lifecycle_label.text() != AuthorityLifecycle.HISTORICAL.value

    dialog.node_combo.setCurrentIndex(
        dialog._node_ids.index("measurement:measurement:m-1")
    )
    assert dialog.authority_class_label.text() == "測定・測定"
    assert dialog.lifecycle_label.text() == "実測"

    dialog.close()
    dialog.deleteLater()
    app.processEvents()


# --- Geometry import dialog ---------------------------------------------------


def test_geometry_import_label_maps_cover_model_vocabulary() -> None:
    """Every diagnostic code, repair kind, and risk class maps to JA."""
    assert set(_ISSUE_TEXT) == set(_ISSUE_CODE_LABELS)
    assert set(_ISSUE_TEXT) == set(_ISSUE_ACTION_LABELS)
    assert set(_REPAIR_RISK) <= set(_REPAIR_OPERATION_LABELS)
    assert {kind for kind, _risk, _label in _REPAIR_CHECKBOXES} <= set(
        _REPAIR_OPERATION_LABELS
    )
    risk_classes = {classify_repair_operation(k) for k in _REPAIR_RISK}
    assert risk_classes <= set(_REPAIR_RISK_LABELS)
    assert set(_ACOUSTIC_VOLUME_LABELS) == {
        "not_ready",
        "geometry_checks_pass_but_semantic_conversion_required",
    }


def test_geometry_import_dialog_renders_japanese(tmp_path: Path) -> None:
    _app()
    obj = tmp_path / "room.obj"
    obj.write_bytes(
        b"v 0.0 0.0 0.0\nv 0.4 0.0 0.0\nv 0.0 0.3 0.0\nv 0.0 0.0 0.2\n"
        b"f 1 2 3\nf 1 2 4\nf 1 3 4\nf 2 3 4\n"
    )
    dialog = GeometryImportDialog(obj)

    group_titles = [g.title() for g in dialog.findChildren(QGroupBox)]
    assert "ソースの単位・座標系（オペレーターによる宣言）" in group_titles
    assert not any("演算子" in title for title in group_titles)
    assert "format_specification" not in dialog.unit_combo.toolTip()

    # QA table renders localized code + action, never the raw finding ids.
    for row in range(dialog.issues_table.rowCount()):
        for column in (2, 4):
            text = dialog.issues_table.item(row, column).text()
            assert text not in _ISSUE_TEXT, f"raw code leaked: {text}"
            assert "_" not in text, f"snake_case leaked: {text}"

    for check in dialog.repair_checks.values():
        assert "操作:" not in check.toolTip()
        assert "リスククラス:" in check.toolTip()

    dialog.close()
    dialog.deleteLater()


def test_unresolved_finding_labels_are_japanese() -> None:
    assert (
        _unresolved_finding_label("unsupported_operation:fill_hole")
        == "穴埋めは対象外のため未解決"
    )
    assert (
        _unresolved_finding_label(
            "blocked_operation:remove_degenerate_faces:would_remove_all_faces"
        )
        == "退化面の削除がブロックされました（全面が削除されるため）"
    )
    assert (
        _unresolved_finding_label(
            "blocked_operation:correct_consistent_winding:non_manifold_components=3"
        )
        == "面の巻き方向の統一がブロックされました（非多様体成分 3 件）"
    )
    # Unknown detail text passes through rather than fabricating.
    assert _unresolved_finding_label("opaque") == "opaque"


# --- Room + retention + menu terminology --------------------------------------


def test_selection_inspector_kind_labels_cover_entity_kinds() -> None:
    """isolate_kind's status must always have a JA name to render."""
    assert set(get_args(EntityKind)) <= set(SelectionInspector.KIND_LABELS)


def test_retention_dependent_kinds_are_japanese() -> None:
    assert _DEPENDENT_KIND_LABELS == {
        "semantic_promotion": "意味昇格",
        "mesh_composition": "メッシュ合成",
    }


def test_project_menu_archive_terminology_is_consistent(tmp_path: Path) -> None:
    """Archive/unarchive reads one term across menu, dialog, and library."""
    app = _app()
    repository = SceneRepository(tmp_path / "data" / "cad-scenes.sqlite3")
    composition = WorkflowApplicationComposition(repository, "document-1")

    texts = [
        action.text()
        for menu in (
            action.menu()
            for action in composition.shell.menuBar().actions()
        )
        if menu is not None
        for action in menu.actions()
    ]
    assert "アーカイブ解除…(&U)" in texts
    assert not any("アーカイブから復元" in text for text in texts)

    composition.shell.close()
    composition.shell.deleteLater()
    app.processEvents()
