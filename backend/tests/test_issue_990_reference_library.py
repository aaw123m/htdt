"""Issue #990: Reference Library 横断検索・比較・出典確認.

Covers the cross-search/filter/detail/compare read model
(``reference_library_browser``), the ReferenceLibraryPage browser tab,
both deep-link directions, and deferred rendering. Identity safety is the
DoD: same-name entries with different identity/version/hash stay distinct
rows keyed by semantic_key — never merged.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from htdt.application_pages import ReferenceLibraryPage
from htdt.cad_equipment import (
    DirectivityCapability,
    EquipmentDataProvenance,
    build_equipment_definition,
)
from htdt.cad_equipment_evidence import build_equipment_manual_evidence
from htdt.cad_equipment_binding import build_equipment_binding_semantics
from htdt.cad_equipment_binding_repository import CadEquipmentBindingRepository
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_material_library import build_material_definition
from htdt.cad_material_library_repository import CadMaterialLibraryRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_speaker_library import build_speaker_definition
from htdt.cad_speaker_library_repository import CadSpeakerLibraryRepository
from htdt.reference_libraries import (
    LibraryEntry,
    LibraryFamily,
    LibraryMetaStore,
    LibraryScope,
    ReferenceLibraryIndex,
)
from htdt.reference_library_browser import (
    STATUS_ATTENTION,
    STATUS_LATEST,
    STATUS_UNQUALIFIED,
    LibraryRecordDetail,
    LibraryUsageSite,
    build_reference_library_detail_resolver,
    collect_library_rows,
    collect_usage_sites,
    compare_rows,
    filter_rows,
)
from htdt.reference_library_sources import build_reference_library_index

NOW = "2026-10-09T00:00:00+00:00"


@pytest.fixture()
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


class _StubProvider:
    """Minimal LibraryProvider for synthetic index fixtures."""

    def __init__(self, family: LibraryFamily, entries) -> None:
        self.family = family
        self._entries = tuple(entries)

    def list_entries(self):
        return self._entries

    def open_editor_hint(self, entry: LibraryEntry):
        return None


def _entry(
    identity: str,
    *,
    family=LibraryFamily.EQUIPMENT,
    scope=LibraryScope.USER_LIBRARY,
    name: str | None = None,
    version: str = "1",
    digest: str | None = None,
    is_latest: bool = True,
    category: str | None = None,
    source: str | None = None,
    missing: tuple[str, ...] = (),
) -> LibraryEntry:
    return LibraryEntry(
        identity=identity,
        family=family,
        scope=scope,
        display_name=name or identity,
        version=version,
        authority_hash=digest or (identity + version).encode().hex().ljust(64, "0"),
        is_latest=is_latest,
        capability_summary=category,
        source_summary=source,
        missing_dependencies=missing,
    )


def _index(tmp_path: Path, *providers) -> ReferenceLibraryIndex:
    index = ReferenceLibraryIndex(
        LibraryMetaStore.for_data_dir(tmp_path / "meta")
    )
    for provider in providers:
        index.register_provider(provider)
    return index


def _rows(index, **kwargs) -> tuple:
    return collect_library_rows(index, **kwargs)


# ---------------------------------------------------------------- model


def test_same_name_different_identity_and_version_never_merged(
    tmp_path: Path,
) -> None:
    """DoD: name collisions and old versions stay distinct rows keyed by
    semantic_key — search results never swap entity ids."""
    entries = (
        _entry("speaker-acme-1", name="Acme AX-1", version="1", digest="a" * 64),
        _entry(
            "speaker-acme-1",
            name="Acme AX-1",
            version="2",
            digest="b" * 64,
        ),
        _entry(
            "vendor-b-ax1", name="Acme AX-1", version="1", digest="c" * 64
        ),
    )
    index = _index(tmp_path, _StubProvider(LibraryFamily.EQUIPMENT, entries))
    rows = _rows(index)
    assert len(rows) == 3
    keys = {row.semantic_key for row in rows}
    assert keys == {entry.semantic_key for entry in entries}
    found = filter_rows(rows, query="Acme AX-1")
    assert len(found) == 3
    # Old version is honestly marked, the other-maker same-name entry is not.
    stale = [row for row in found if row.superseded]
    assert [row.semantic_key for row in stale] == [entries[0].semantic_key]


def test_search_covers_maker_model_id_version_source_and_role(
    tmp_path: Path,
) -> None:
    row_detail = LibraryRecordDetail(
        sources=("Acme datasheet v3",),
        evidence_labels=("製品データ",),
        fields=(("メーカー", "Acme"), ("モデル", "AX-1")),
    )
    site = LibraryUsageSite(
        kind="scene_entity",
        target_id="fl",
        label="スピーカー FL",
        resolution="explicit_binding",
        role="FL",
    )
    index = _index(
        tmp_path,
        _StubProvider(
            LibraryFamily.EQUIPMENT,
            (
                _entry(
                    "spk-1",
                    name="Acme AX-1",
                    version="9.1",
                    source="pack-A",
                ),
            ),
        ),
    )
    entry = next(iter(index.entries()))
    rows = _rows(
        index,
        detail_resolver=lambda _entry: row_detail,
        usage_sites={entry.semantic_key: (site,)},
    )
    for needle in (
        "Acme",  # display name + resolved メーカー field
        "spk-1",  # authority ID
        "9.1",  # version
        "pack-A",  # source summary
        "Acme datasheet",  # provenance source
        "FL",  # speaker role from the usage site
    ):
        hits = filter_rows(rows, query=needle)
        assert hits, f"query {needle!r} should hit"
        assert hits[0].semantic_key == entry.semantic_key


def test_filter_statuses_latest_attention_unqualified(tmp_path: Path) -> None:
    current = _entry("m-new", name="new", version="2", digest="d" * 64)
    stale = _entry(
        "m-new", name="new", version="1", digest="e" * 64, is_latest=False
    )
    broken = _entry(
        "m-broken", name="broken", version="1", digest="f" * 64,
        missing=("evidence-missing",),
    )
    archived = _entry(
        "m-arch", name="arch", version="1", digest="0" * 64
    )
    index = _index(
        tmp_path,
        _StubProvider(
            LibraryFamily.MATERIAL, (current, stale, broken, archived)
        ),
    )
    index.set_archived(archived, True)
    rows = _rows(index)
    assert {r.semantic_key for r in filter_rows(rows)} == {
        current.semantic_key,
        stale.semantic_key,
        broken.semantic_key,
    }
    assert {r.semantic_key for r in filter_rows(rows, status=STATUS_LATEST)} == {
        current.semantic_key
    }
    assert {
        r.semantic_key
        for r in filter_rows(rows, status=STATUS_ATTENTION)
    } == {stale.semantic_key, broken.semantic_key}
    assert {
        r.semantic_key
        for r in filter_rows(rows, status=STATUS_UNQUALIFIED)
    } == {archived.semantic_key}


def test_record_missing_marks_unqualified(tmp_path: Path) -> None:
    entry = _entry("ghost", name="ghost", digest="1" * 64)
    index = _index(
        tmp_path, _StubProvider(LibraryFamily.EQUIPMENT, (entry,))
    )
    rows = _rows(
        index,
        detail_resolver=lambda _e: LibraryRecordDetail(
            record_missing=True
        ),
    )
    assert rows[0].record_missing
    assert not rows[0].qualified
    # Default view still lists it — honestly badged 記録なし — but it is
    # excluded from 最新 only and grouped with the unqualified set.
    assert filter_rows(rows)[0].semantic_key == entry.semantic_key
    assert filter_rows(rows, status=STATUS_LATEST) == []
    assert [r.semantic_key for r in filter_rows(rows, status=STATUS_UNQUALIFIED)] == [
        entry.semantic_key
    ]
    status_text = dict(rows[0].detail_fields)["状態"]
    assert "記録なし" in status_text


def test_row_detail_carries_exact_authority_and_provenance(
    tmp_path: Path,
) -> None:
    entry = _entry(
        "spk-9",
        name="Acme AX-9",
        version="4",
        digest="9" * 64,
        scope=LibraryScope.BUILTIN,
        category="manufacturer",
        source="pack",
    )
    index = _index(
        tmp_path, _StubProvider(LibraryFamily.EQUIPMENT, (entry,))
    )
    detail = LibraryRecordDetail(
        sources=("Acme datasheet", "ISO 1234"),
        evidence_labels=("製品データ", "実測値"),
        rights_label="権利未確認",
        fields=(("メーカー", "Acme"),),
    )
    rows = _rows(index, detail_resolver=lambda _e: detail)
    fields = dict(rows[0].detail_fields)
    assert fields["ID"] == "spk-9"
    assert fields["バージョン"] == "4"
    assert fields["SHA-256"] == "9" * 64
    assert fields["参照キー"] == entry.semantic_key
    assert fields["スコープ"] == "同梱"
    assert fields["出典"] == "Acme datasheet、ISO 1234"
    assert fields["根拠区分"] == "製品データ、実測値"
    assert fields["権利"] == "権利未確認"


def test_compare_rows_versions_and_dependents(tmp_path: Path) -> None:
    site = LibraryUsageSite(
        kind="scene_entity",
        target_id="fl",
        label="スピーカー FL",
        resolution="explicit_binding",
    )
    old = _entry("spk", name="AX", version="1", digest="a" * 64,
                 is_latest=False)
    new = _entry("spk", name="AX", version="2", digest="b" * 64)
    index = _index(
        tmp_path, _StubProvider(LibraryFamily.EQUIPMENT, (old, new))
    )
    rows = _rows(
        index, usage_sites={old.semantic_key: (site,)}
    )
    by_key = {row.semantic_key: row for row in rows}
    comparison = compare_rows(by_key[old.semantic_key], by_key[new.semantic_key])
    assert comparison is not None
    fields = {label: (a, b) for label, a, b in comparison.fields}
    assert fields["バージョン"] == ("1", "2")
    assert fields["SHA-256"] == ("a" * 64, "b" * 64)
    assert fields["ID"] == ("spk", "spk")
    assert comparison.dependents_a == (site,)
    assert comparison.dependents_b == ()


def test_compare_rows_different_family_is_rejected(tmp_path: Path) -> None:
    eq = _entry("a", family=LibraryFamily.EQUIPMENT, digest="a" * 64)
    mat = _entry("b", family=LibraryFamily.MATERIAL, digest="b" * 64)
    index = _index(
        tmp_path,
        _StubProvider(LibraryFamily.EQUIPMENT, (eq,)),
        _StubProvider(LibraryFamily.MATERIAL, (mat,)),
    )
    rows = _rows(index)
    by_key = {row.semantic_key: row for row in rows}
    assert (
        compare_rows(by_key[eq.semantic_key], by_key[mat.semantic_key])
        is None
    )


# ------------------------------------------------------------------- page


def _seeded_repository(tmp_path: Path) -> SceneRepository:
    scenes = SceneRepository(tmp_path / "cad.sqlite3")
    speakers = CadSpeakerLibraryRepository(scenes)
    speakers.save_speaker(
        build_speaker_definition(
            model="Bookshelf",
            manufacturer="Acme",
            configuration="bookshelf",
            created_at_utc=NOW,
            document_id=None,
        )
    )
    materials = CadMaterialLibraryRepository(scenes)
    materials.save_material(
        build_material_definition(
            category="porous_absorber",
            name="Fixture absorber 990",
            mounting="rigid_backing",
            created_at_utc=NOW,
            document_id=None,
        )
    )
    return scenes


def _close(page) -> None:
    page.close()
    page.deleteLater()


def test_page_search_filter_and_detail(qapp, tmp_path: Path) -> None:
    scenes = _seeded_repository(tmp_path)
    index = build_reference_library_index(scenes, tmp_path / "data")
    page = ReferenceLibraryPage(
        lambda: (),
        library_index=index,
        detail_resolver=lambda: build_reference_library_detail_resolver(
            scenes, tmp_path / "data"
        ),
    )
    try:
        total = page.results_table.rowCount()
        assert total > 0
        page.search_edit.setText("Fixture absorber 990")
        assert page.results_table.rowCount() == 1
        item = page.results_table.item(0, 0)
        assert item.text() == "Fixture absorber 990"
        # Material category + scope + family filters narrow the same way.
        page.search_edit.clear()
        material_index = page.family_combo.findData("material")
        page.family_combo.setCurrentIndex(material_index)
        category_index = page.category_combo.findData("porous_absorber")
        assert category_index > 0
        page.category_combo.setCurrentIndex(category_index)
        names = {
            page.results_table.item(row, 0).text()
            for row in range(page.results_table.rowCount())
        }
        # Built-in porous absorbers share the category; ours must be there
        # and every shown row must carry exactly that category.
        assert "Fixture absorber 990" in names
        assert {
            page.results_table.item(row, 2).text()
            for row in range(page.results_table.rowCount())
        } == {"porous_absorber"}
        # Selecting the row shows exact identity/version/SHA in the detail.
        page.results_table.selectRow(0)
        assert not page._detail_scroll.isHidden()
        detail_texts = {
            page._detail_table.item(row, 0).text():
                page._detail_table.item(row, 1).text()
            for row in range(page._detail_table.rowCount())
        }
        assert detail_texts["区分"] == "porous_absorber"
        assert len(detail_texts["SHA-256"]) == 64
        assert detail_texts["権利"]
    finally:
        _close(page)


def test_page_usage_site_opens_navigation_target(
    qapp, tmp_path: Path
) -> None:
    entry = _entry("spk-x", name="X", digest="a" * 64)
    index = _index(
        tmp_path, _StubProvider(LibraryFamily.EQUIPMENT, (entry,))
    )
    site = LibraryUsageSite(
        kind="scene_entity",
        target_id="fl",
        label="スピーカー FL",
        resolution="explicit_binding",
    )
    opened: list = []
    page = ReferenceLibraryPage(
        lambda: (),
        library_index=index,
        usage_resolver=lambda: {entry.semantic_key: (site,)},
        open_target=opened.append,
    )
    try:
        page.results_table.selectRow(0)
        assert page._usage_table.rowCount() == 1
        assert page._usage_table.item(0, 1).text() == "explicit_binding"
        button = page._usage_table.cellWidget(0, 2)
        button.click()
        assert len(opened) == 1
        target = opened[0]
        assert str(target.kind) == "scene_entity"
        assert target.object_ids == ("fl",)
        assert target.referrer == "reference_library"
        assert str(target.intent) == "inspect"
    finally:
        _close(page)


def test_page_compare_two_rows(qapp, tmp_path: Path) -> None:
    old = _entry("spk", name="AX", version="1", digest="a" * 64,
                 is_latest=False)
    new = _entry("spk", name="AX", version="2", digest="b" * 64)
    index = _index(
        tmp_path, _StubProvider(LibraryFamily.EQUIPMENT, (old, new))
    )
    site = LibraryUsageSite(
        kind="scene_entity",
        target_id="fl",
        label="スピーカー FL",
        resolution="explicit_binding",
    )
    page = ReferenceLibraryPage(
        lambda: (),
        library_index=index,
        usage_resolver=lambda: {old.semantic_key: (site,)},
        open_target=lambda _t: None,
    )
    try:
        page.results_table.selectRow(0)
        selection = page.results_table.selectionModel()
        selection.select(
            page.results_table.model().index(1, 0),
            selection.SelectionFlag.Select
            | selection.SelectionFlag.Rows,
        )
        assert not page._compare_frame.isHidden()
        rows_text = {
            page._compare_table.item(r, 0).text(): (
                page._compare_table.item(r, 1).text(),
                page._compare_table.item(r, 2).text(),
            )
            for r in range(page._compare_table.rowCount())
        }
        assert rows_text["バージョン"] == ("1", "2")
        assert "1件" in page._dependents_label.text()
    finally:
        _close(page)


def test_page_deferred_rendering_counts(qapp, tmp_path: Path) -> None:
    entries = tuple(
        _entry(f"bulk-{i:04d}", name=f"Bulk {i:04d}", version="1",
               digest=f"{i:064x}"[:64])
        for i in range(120)
    )
    index = _index(
        tmp_path, _StubProvider(LibraryFamily.EQUIPMENT, entries)
    )
    page = ReferenceLibraryPage(lambda: (), library_index=index)
    try:
        assert page.results_table.rowCount() == 50
        assert page._more_button.isEnabled()
        page._more_button.click()
        assert page.results_table.rowCount() == 100
        page._more_button.click()
        assert page.results_table.rowCount() == 120
        assert not page._more_button.isEnabled()
        assert "全120件" in page._count_label.text()
    finally:
        _close(page)


def test_page_zero_and_thousand_rows(qapp, tmp_path: Path) -> None:
    empty_index = _index(
        tmp_path / "empty",
        _StubProvider(LibraryFamily.EQUIPMENT, ()),
    )
    page = ReferenceLibraryPage(lambda: (), library_index=empty_index)
    try:
        assert page.results_table.rowCount() == 0
        assert "全0件" in page._count_label.text()
    finally:
        _close(page)

    entries = tuple(
        _entry(f"many-{i:04d}", name=f"Many {i:04d}", version="1",
               digest=f"{i:064x}")
        for i in range(1000)
    )
    big_index = _index(
        tmp_path / "big",
        _StubProvider(LibraryFamily.EQUIPMENT, entries),
    )
    page = ReferenceLibraryPage(lambda: (), library_index=big_index)
    try:
        assert page.results_table.rowCount() == 50
        assert "全1000件" in page._count_label.text()
        page.search_edit.setText("Many 0999")
        assert page.results_table.rowCount() == 1
    finally:
        _close(page)


def test_focus_definition_reveals_filtered_row(
    qapp, tmp_path: Path
) -> None:
    entry = _entry("target-def", name="Target", digest="a" * 64)
    other = _entry("other-def", name="Other", digest="b" * 64)
    index = _index(
        tmp_path, _StubProvider(LibraryFamily.EQUIPMENT, (entry, other))
    )
    page = ReferenceLibraryPage(lambda: (), library_index=index)
    try:
        page.search_edit.setText("nothing matches")
        assert page.results_table.rowCount() == 0
        result = page.focus_definition("target-def")
        assert result.focused
        selected = {
            index.row()
            for index in page.results_table.selectionModel().selectedRows()
        }
        assert selected
        key = page.results_table.item(
            selected.pop(), 0
        ).data(Qt.ItemDataRole.UserRole)
        assert key == entry.semantic_key
    finally:
        _close(page)


def test_archived_row_reachable_via_unqualified_filter(
    qapp, tmp_path: Path
) -> None:
    entry = _entry("old-def", name="Old", digest="a" * 64)
    index = _index(
        tmp_path, _StubProvider(LibraryFamily.EQUIPMENT, (entry,))
    )
    index.set_archived(entry, True)
    page = ReferenceLibraryPage(lambda: (), library_index=index)
    try:
        assert page.results_table.rowCount() == 0
        idx = page.status_combo.findData(STATUS_UNQUALIFIED)
        page.status_combo.setCurrentIndex(idx)
        assert page.results_table.rowCount() == 1
        assert "アーカイブ" in page.results_table.item(0, 5).text()
    finally:
        _close(page)


def test_new_controls_have_accessible_names(qapp, tmp_path: Path) -> None:
    index = _index(
        tmp_path, _StubProvider(LibraryFamily.EQUIPMENT, ())
    )
    page = ReferenceLibraryPage(lambda: (), library_index=index)
    try:
        for widget in (
            page.search_edit,
            page.family_combo,
            page.category_combo,
            page.source_combo,
            page.status_combo,
            page.results_table,
            page._more_button,
            page._count_label,
            page._detail_table,
            page._usage_table,
            page._compare_table,
        ):
            assert widget.accessibleName(), widget
        assert page.search_edit.toolTip()
        assert page.results_table.toolTip()
    finally:
        _close(page)


# ------------------------------------------------------- usage collection


def _equipment(definition_id: str, version: str, digest_seed: str):
    provenance = EquipmentDataProvenance(
        evidence_kind="user_defined",
        source_name="fixture-operator",
        source_version="1",
        source_reference="fixture",
        source_sha256=digest_seed * 64,
    )
    return build_equipment_definition(
        definition_id=definition_id,
        version=version,
        identity_kind="user_defined",
        user_label=f"Fixture {definition_id}",
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
        acoustic_reference_point_m=Offset3(),
        directivity=DirectivityCapability(
            tier="unknown",
            data_format="unknown",
            provenance=provenance,
        ),
    )


def test_collect_usage_sites_reports_bound_speakers(tmp_path: Path) -> None:
    scenes = SceneRepository(tmp_path / "cad.sqlite3")
    document = SceneDocument(
        document_id="doc-990",
        room=RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id="fl",
                kind="speaker",
                name="Front Left",
                speaker_role="FL",
                position=Position3(x_m=1.2, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
            ),
            SceneEntity(
                entity_id="mlp",
                kind="measurement_point",
                name="MLP",
                position=Position3(x_m=3.0, y_m=3.2, z_m=1.1),
            ),
        ),
    )
    revision = scenes.save(document, parent_revision_id=None).revision
    assert revision is not None
    equipment = CadEquipmentRepository(scenes)
    definition = _equipment("fixture-def", "1", "a")
    for evidence in build_equipment_manual_evidence(
        definition,
        actor="issue-990-fixture",
        recorded_at_utc=NOW,
    ):
        equipment.save_evidence(evidence)
    equipment.save_definition(definition)
    bindings = CadEquipmentBindingRepository(scenes, equipment)
    bindings.save_binding(
        build_equipment_binding_semantics(
            binding_id="binding-990",
            document_id="doc-990",
            entity_id="fl",
            equipment_definition=definition,
            body_geometry_authority="equipment_nominal",
            acoustic_reference_authority="equipment_derived",
            provenance=(
                EquipmentDataProvenance(
                    evidence_kind="user_defined",
                    source_name="fixture-operator",
                    source_version="1",
                    source_reference="fixture",
                    source_sha256="b" * 64,
                ),
            ),
            created_at_utc="2026-10-09T01:00:00+00:00",
        )
    )
    sites = collect_usage_sites(scenes, "doc-990")
    expected_key = (
        f"{definition.definition_id}@{definition.version}"
        f"#{definition.semantic_sha256}"
    )
    assert expected_key in sites
    site = sites[expected_key][0]
    assert site.kind == "scene_entity"
    assert site.target_id == "fl"
    assert site.role == "FL"
    assert site.resolution == "explicit_binding"
    # The measurement point and unrelated documents produce no sites.
    assert collect_usage_sites(scenes, "doc-nonexistent") == {}
    assert collect_usage_sites(scenes, None) == {}
