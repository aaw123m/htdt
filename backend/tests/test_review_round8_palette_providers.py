"""Round-8 regression: palette providers for authority records
(measurements / scene revisions / system variants / inbox items) — the
round-7 deferred palette gap. Results activate through the shared
``on_deep_link`` handoff; measurement activation selects the quality row.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from hashlib import sha256
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from htdt.cad_measurement_models import (
    CadFrequencyResponseDataset,
    CadMeasurementRecord,
)
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    declared_fr_raw,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    F1_DOCUMENT_ID,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    make_f1_scene,
)
from htdt.measurement_page_workspace import MeasurementPageWorkspace
from htdt.measurement_workflow import MeasurementWorkflowController
from htdt.navigation_target import NavigationTargetKind
from htdt.palette_search import (
    NavigationItemPaletteProvider,
    PaletteNavigationItem,
    PaletteResultKind,
    PaletteSearchService,
)
from htdt.workflow_navigation import ApplicationDestinationId, WorkspaceDeepLink, WorkspaceId


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _link(kind: str, workspace, entity_id: str) -> WorkspaceDeepLink:
    return WorkspaceDeepLink(
        workspace, "quality", entity_id=entity_id, kind=kind
    )


def _provider(items, name="records"):
    return NavigationItemPaletteProvider(
        name, PaletteResultKind.DATA, lambda: items
    )


# -- provider mechanics ------------------------------------------------------


def test_search_matches_title_keywords_and_id() -> None:
    provider = _provider(
        (
            PaletteNavigationItem(
                item_id="m-1",
                title="MLP 測定",
                subtitle="測定 · FL",
                keywords=("measurement", "rew"),
                deep_link=_link(
                    NavigationTargetKind.MEASUREMENT.value,
                    WorkspaceId.MEASUREMENT,
                    "m-1",
                ),
            ),
        )
    )
    by_title = provider.search("mlp")
    by_keyword = provider.search("rew")
    by_id = provider.search("m-1")
    assert by_title and by_keyword and by_id
    assert by_title[0].deep_link is not None
    assert by_title[0].kind == PaletteResultKind.DATA
    assert provider.search("zzzzz") == ()


def test_empty_query_yields_nothing() -> None:
    provider = _provider(
        (PaletteNavigationItem(item_id="a", title="anything", subtitle=""),)
    )
    assert provider.search("") == ()
    assert provider.suggested() == ()


def test_linkless_item_is_unavailable() -> None:
    provider = _provider(
        (PaletteNavigationItem(item_id="a", title="孤児", subtitle="", deep_link=None),)
    )
    (result,) = provider.search("孤児")
    assert result.available is False
    assert result.disabled_reason


def test_items_source_failure_is_nonfatal() -> None:
    def broken():
        raise RuntimeError("mid-restore")

    provider = NavigationItemPaletteProvider(
        "broken", PaletteResultKind.DATA, broken
    )
    assert provider.search("x") == ()


def test_activation_flows_through_on_deep_link() -> None:
    link = _link(
        NavigationTargetKind.MEASUREMENT.value, WorkspaceId.MEASUREMENT, "m-1"
    )
    provider = _provider(
        (
            PaletteNavigationItem(
                item_id="m-1", title="MLP", subtitle="", deep_link=link
            ),
        )
    )
    landed: list[WorkspaceDeepLink] = []

    def on_link(link_: WorkspaceDeepLink) -> bool:
        landed.append(link_)
        return True

    service = PaletteSearchService((provider,), on_deep_link=on_link)
    (result,) = provider.search("MLP")
    assert service.activate(result) is True
    assert landed == [link]
    # The deep link carries the typed kind so navigation focuses, not just opens.
    assert landed[0].kind == NavigationTargetKind.MEASUREMENT.value


def test_activation_rejects_unavailable_results() -> None:
    provider = _provider(
        (PaletteNavigationItem(item_id="a", title="孤児", subtitle=""),)
    )
    landed: list[WorkspaceDeepLink] = []
    service = PaletteSearchService((provider,), on_deep_link=landed.append)
    (result,) = provider.search("孤児")
    assert service.activate(result) is False
    assert landed == []


# -- measurement workspace focus port ----------------------------------------


def _save_fixture_measurement(tmp_path: Path):
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    document = SceneDocument(
        document_id="doc-palette",
        schema_version=2,
        room=RoomPrism(width_m=5, depth_m=4, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id="fl",
                kind="speaker",
                name="FL",
                position=Position3(x_m=1, y_m=1, z_m=1),
                size_m=Size3(x_m=0.2, y_m=0.2, z_m=0.4),
                speaker_role="FL",
            ),
            SceneEntity(
                entity_id="mlp",
                kind="measurement_point",
                name="MLP",
                position=Position3(x_m=2.5, y_m=3, z_m=1),
            ),
        ),
    )
    revision = repository.save(document, parent_revision_id=None).revision
    raw = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(80.0, 81.0, 79.0),
        phase_status="absent",
    )
    record = CadMeasurementRecord(
        measurement_id="measurement-palette",
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        measurement_entity_id="mlp",
        measurement_position=revision.document.entity("mlp").position,
        evidence_type="measured",
        channel_role="FL",
        source_speaker_ids=("fl",),
        radiation_scope="single",
        routing_evidence="manual",
        imported_at="2026-09-27T00:00:00+00:00",
        source_kind="unknown",
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id="dataset-palette",
        measurement_id=record.measurement_id,
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(80.0, 81.0, 79.0),
        phase_deg=None,
        phase_status="absent",
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    CadMeasurementRepository(repository).save(
        record, dataset, raw_filename="fixture.txt", raw_bytes=raw
    )
    return repository, revision


def test_select_measurement_id_focuses_quality_row(tmp_path: Path) -> None:
    _app()
    repository, revision = _save_fixture_measurement(tmp_path)
    controller = MeasurementWorkflowController(
        repository, revision.document_id
    )
    workspace = MeasurementPageWorkspace(controller)
    try:
        assert workspace.select_measurement_id("measurement-palette") is True
        assert workspace.current_context_id == "quality"
        selected = workspace.quality_table.selectedItems()
        assert selected
        assert (
            selected[0].data(Qt.ItemDataRole.UserRole)
            == "measurement-palette"
        )
        assert workspace.select_measurement_id("missing") is False
    finally:
        workspace.close()
        workspace.deleteLater()


def test_select_measurement_id_empty_repo_returns_false(tmp_path: Path) -> None:
    _app()
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    revision = repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    controller = MeasurementWorkflowController(repository, revision.document_id)
    workspace = MeasurementPageWorkspace(controller)
    try:
        assert workspace.select_measurement_id("measurement-palette") is False
    finally:
        workspace.close()
        workspace.deleteLater()


def test_palette_navigation_item_deep_link_round_trip() -> None:
    link = WorkspaceDeepLink(
        ApplicationDestinationId.INBOX,
        entity_id="capture-inbox-item:abc",
        kind=NavigationTargetKind.CAPTURE_INBOX_ITEM.value,
    )
    parsed = WorkspaceDeepLink.from_uri(link.as_uri())
    assert parsed.workspace == ApplicationDestinationId.INBOX
    assert parsed.entity_id == "capture-inbox-item:abc"
    assert parsed.kind == NavigationTargetKind.CAPTURE_INBOX_ITEM.value
