"""Round-8 regression: measurement + system-variant AuthoritySource
adapters and the read-only AuthorityInspectorDialog surface (#590 wiring,
deferred from round-6)."""

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from htdt.authority_graph import (
    AuthorityDomain,
    AuthorityEdgeKind,
    AuthorityLifecycle,
    AuthorityNode,
    StaticAuthoritySource,
    build_authority_graph,
    measurement_authority_source,
    system_variant_authority_source,
)
from htdt.authority_inspector_ui import AuthorityInspectorDialog
from htdt.workflow_navigation import WorkspaceDeepLink, WorkspaceId

DOC = "doc-1"
HEAD_HASH = "a" * 64
OLD_HASH = "b" * 64


def _measurement(
    measurement_id: str,
    *,
    scene_revision_id: str = "rev-1",
    scene_content_hash: str = HEAD_HASH,
    evidence_type: str = "measured",
):
    return SimpleNamespace(
        measurement_id=measurement_id,
        document_id=DOC,
        scene_revision_id=scene_revision_id,
        scene_content_hash=scene_content_hash,
        measurement_entity_id="mlp",
        evidence_type=evidence_type,
        captured_at=None,
        imported_at="2026-09-20T00:00:00+00:00",
    )


def _variant(
    variant_id: str,
    *,
    baseline_revision_id: str = "rev-1",
    baseline_content_hash: str = HEAD_HASH,
    parent_variant_id: str | None = None,
):
    return SimpleNamespace(
        variant_id=variant_id,
        name=f"バリアント {variant_id}",
        document_id=DOC,
        baseline_revision_id=baseline_revision_id,
        baseline_content_hash=baseline_content_hash,
        parent_variant_id=parent_variant_id,
        variant_sha256="c" * 64,
        created_at_utc="2026-09-20T00:00:00+00:00",
    )


def test_measurement_source_edges_and_staleness() -> None:
    source = measurement_authority_source(
        [_measurement("m1"), _measurement("m2", scene_content_hash=OLD_HASH)],
        head_content_hash_by_document={DOC: HEAD_HASH},
    )
    graph = build_authority_graph([source])
    fresh = graph.node("measurement:measurement:m1")
    stale = graph.node("measurement:measurement:m2")
    assert fresh.lifecycle == AuthorityLifecycle.MEASURED
    assert fresh.stale is False
    assert stale.stale is True
    assert stale.stale_reasons
    edges = {
        (e.source, e.kind, e.target) for e in graph.edges
    }
    assert (
        "measurement:measurement:m1",
        AuthorityEdgeKind.MEASURED_FOR,
        "room:scene_revision:rev-1",
    ) in edges
    # Uncontributed scene revision resolves to an explicit missing node.
    missing = graph.node("room:scene_revision:rev-1")
    assert missing.domain == AuthorityDomain.MISSING


def test_variant_source_links_baseline_and_parent() -> None:
    source = system_variant_authority_source(
        [
            _variant("v1"),
            _variant("v2", parent_variant_id="v1", baseline_content_hash=OLD_HASH),
        ],
        head_content_hash_by_document={DOC: HEAD_HASH},
    )
    graph = build_authority_graph([source])
    edges = {(e.source, e.kind, e.target) for e in graph.edges}
    assert (
        "optimization:system_variant:v1",
        AuthorityEdgeKind.DERIVED_FROM,
        "room:scene_revision:rev-1",
    ) in edges
    assert (
        "optimization:system_variant:v2",
        AuthorityEdgeKind.SUPERSEDES,
        "optimization:system_variant:v1",
    ) in edges
    assert graph.node("optimization:system_variant:v2").stale is True
    assert graph.node("optimization:system_variant:v1").stale is False


def test_combined_graph_reports_downstream_dependents() -> None:
    revisions = [
        SimpleNamespace(
            revision_id="rev-1",
            document_id=DOC,
            parent_revision_id=None,
            content_hash=HEAD_HASH,
            created_at_utc="2026-09-20T00:00:00+00:00",
            detached=False,
        )
    ]
    from htdt.authority_graph import scene_revision_authority_source

    graph = build_authority_graph(
        [
            scene_revision_authority_source(
                revisions, head_by_document={DOC: "rev-1"}
            ),
            measurement_authority_source([_measurement("m1")]),
            system_variant_authority_source([_variant("v1")]),
        ]
    )
    downstream = {n.node_id for n in graph.downstream("room:scene_revision:rev-1")}
    assert "measurement:measurement:m1" in downstream
    assert "optimization:system_variant:v1" in downstream


def test_inspector_dialog_renders_summary_and_deep_link() -> None:
    app = QApplication.instance() or QApplication([])
    landed: list[WorkspaceDeepLink] = []
    node = AuthorityNode(
        node_id="room:scene_revision:rev-1",
        domain=AuthorityDomain.ROOM,
        node_type="scene_revision",
        label="SceneRevision rev-1",
        lifecycle=AuthorityLifecycle.CURRENT,
        deep_link=WorkspaceDeepLink(
            WorkspaceId.ROOM, section="history", entity_id="rev-1"
        ),
    )
    graph = build_authority_graph([StaticAuthoritySource([node])])
    dialog = AuthorityInspectorDialog(
        graph, on_deep_link=lambda link: landed.append(link) or True
    )
    try:
        assert dialog.node_combo.count() == 1
        assert "現在" in dialog.freshness_label.text()
        assert dialog.open_button.isEnabled()
        dialog.open_button.click()
        assert landed and landed[0].entity_id == "rev-1"
    finally:
        dialog.close()
        dialog.deleteLater()
    _ = app
