"""Round-8 regression: ReferenceLibraryPage renders the #630 hub families
(speakers/materials/standards profiles) via ReferenceLibraryIndex providers —
the round-6 deferred library wiring."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from htdt.application_pages import ReferenceLibraryPage
from htdt.cad_material_library import build_material_definition
from htdt.cad_material_library_repository import CadMaterialLibraryRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_speaker_library import build_speaker_definition
from htdt.cad_speaker_library_repository import CadSpeakerLibraryRepository
from htdt.cad_standards import (
    CriterionDefinition,
    CriterionRule,
    CriterionSource,
    build_user_standards_profile,
)
from htdt.cad_standards_repository import CadStandardsRepository
from htdt.reference_library_sources import build_reference_library_index

NOW = "2026-09-27T00:00:00+00:00"


@pytest.fixture()
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _seeded_repository(tmp_path: Path) -> SceneRepository:
    scenes = SceneRepository(tmp_path / "cad.sqlite3")
    speakers = CadSpeakerLibraryRepository(scenes)
    speakers.save_speaker(
        build_speaker_definition(
            model="Fixture Bookshelf",
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
            name="Fixture absorber",
            mounting="rigid_backing",
            created_at_utc=NOW,
            document_id=None,
        )
    )
    standards = CadStandardsRepository(scenes)
    standards.save_profile(
        build_user_standards_profile(
            profile_id="fixture-profile",
            version="1.0",
            name="Fixture profile",
            criteria=(
                CriterionDefinition(
                    criterion_id="fixture",
                    name="fixture",
                    source=CriterionSource(
                        publisher="Fixture publisher",
                        document_title="Fixture criteria",
                        document_version="1.0",
                        reference="Fixture §1",
                    ),
                    quantity="fixture_quantity",
                    unit="m",
                    applicable_domains=("room",),
                    required_inputs=("fixture_input",),
                    required_capabilities=("fixture-capability-v1",),
                    rule=CriterionRule(operator="max", maximum=1.0),
                ),
            ),
        )
    )
    return scenes


def test_index_lists_every_family(tmp_path: Path) -> None:
    scenes = _seeded_repository(tmp_path)
    index = build_reference_library_index(scenes, tmp_path / "data")
    assert set(index.families()) >= {
        "equipment",
        "material",
        "standard_profile",
    }
    names = {entry.display_name for entry in index.entries()}
    assert {"Acme Fixture Bookshelf", "Fixture absorber", "Fixture profile"} <= names


def test_page_renders_family_sections(qapp, tmp_path: Path) -> None:
    scenes = _seeded_repository(tmp_path)
    index = build_reference_library_index(scenes, tmp_path / "data")
    page = ReferenceLibraryPage(lambda: (), library_index=index)
    try:
        rendered = {}
        for family, (header, table) in page._family_frames.items():
            if table.rowCount():
                rendered[family] = [
                    table.item(row, 0).text()
                    for row in range(table.rowCount())
                ]
        assert "Acme Fixture Bookshelf" in rendered.get("equipment", [])
        assert "Fixture absorber" in rendered.get("material", [])
        assert "Fixture profile" in rendered.get("standard_profile", [])
        # Scope column shows the JP user-visible scope label.
        material_table = page._family_frames["material"][1]
        assert material_table.item(0, 2).text() == "ユーザーライブラリ"
    finally:
        page.close()
        page.deleteLater()


def test_page_without_index_behaves_as_before(qapp) -> None:
    class _Def:
        manufacturer = "Acme"
        model = "AVR-1"
        version = "1"
        definition_id = "def-1"
        user_label = None

    page = ReferenceLibraryPage(lambda: (_Def(),))
    try:
        assert page.table.rowCount() == 1
        assert page._family_frames == {}
    finally:
        page.close()
        page.deleteLater()


def test_archived_entries_are_hidden(qapp, tmp_path: Path) -> None:
    scenes = _seeded_repository(tmp_path)
    index = build_reference_library_index(scenes, tmp_path / "data")
    entry = next(
        e for e in index.entries() if e.display_name == "Fixture absorber"
    )
    index.set_archived(entry, True)
    page = ReferenceLibraryPage(lambda: (), library_index=index)
    try:
        material_table = page._family_frames["material"][1]
        assert material_table.rowCount() == 0
        assert not material_table.isVisibleTo(page)
    finally:
        page.close()
        page.deleteLater()
