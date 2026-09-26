"""Backend tests for the native-UI authoring batch:

- #444 equipment/source library service + dialog (versioned definitions,
  provenance-preserving directivity import)
- #454 standards profile library/editor service + dialog (create, clone,
  immutable versioning, exact JSON import/export)
- #477 playback-chain service + dialog (amplifier capability, speaker load,
  routing, simultaneous-channel condition, O100D evaluation)
"""

from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from htdt.cad_equipment import Size3
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import RoomPrism, SceneDocument, SceneEntity
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    EquipmentBindingRef,
    build_system_variant,
)
from htdt.equipment_library import (
    EquipmentLibraryDialog,
    EquipmentLibraryService,
    capability_preview,
)
from htdt.playback_chain_widgets import (
    PlaybackChainDialog,
    PlaybackChainService,
    evaluation_summary,
)
from htdt.standards_profile_editor import (
    StandardsProfileEditorDialog,
    StandardsProfileLibraryService,
)
from htdt.standards_workspace import StandardsWorkspaceModel

NOW = "2026-09-20T00:00:00+00:00"
DOCUMENT_ID = "ui-workflow-fixture"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _speaker(entity_id="speaker-fl", role="FL"):
    return SceneEntity(
        entity_id=entity_id,
        kind="speaker",
        name="Front Left",
        speaker_role=role,
        position={"x_m": 1.0, "y_m": 1.0, "z_m": 1.0},
        size_m=Size3(x_m=0.20, y_m=0.25, z_m=0.35),
    )


@pytest.fixture()
def repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / "cad.sqlite3")
    revision = scene_repository.save(
        SceneDocument(
            document_id=DOCUMENT_ID,
            schema_version=2,
            room=RoomPrism(width_m=6.0, depth_m=6.0, height_m=2.5),
            entities=(_speaker(),),
        ),
        parent_revision_id=None,
    ).revision
    variant_repository = CadSystemVariantRepository(scene_repository)
    return scene_repository, revision, variant_repository


def _definition(service: EquipmentLibraryService):
    return service.create_user_definition(
        user_label="テストスピーカー",
        manufacturer=None,
        model=None,
        width_m=0.25,
        height_m=0.4,
        depth_m=0.3,
        evidence_kind="user_defined",
        source_name="fixture",
        source_version="1",
        source_reference="ui-test",
        actor="ui-test",
    )


def _persist_variant(variant_repository, revision, definition):
    variant = build_system_variant(
        baseline=revision,
        name="v1",
        role_bindings=(
            ChannelRoleBinding(role_id="FL", display_name="Front Left"),
        ),
        proposed_entities=(),
        equipment_bindings=(
            EquipmentBindingRef(
                entity_id="speaker-fl",
                equipment_definition_id=definition.definition_id,
                equipment_definition_version=definition.version,
                equipment_definition_sha256=definition.semantic_sha256,
            ),
        ),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant)
    return variant


def _polar_table_bytes() -> bytes:
    metadata = [
        "# schema=htdt.polar-table.v1",
        "# delimiter=csv",
        "# dataset_id=polar-fixture",
        "# dataset_version=1",
        "# capability=magnitude_only",
        "# angle_semantics=horizontal_vertical",
        "# horizontal_wrap=none",
        "# reference_axis=equipment_acoustic_reference_axis",
        "# azimuth_positive=left",
        "# elevation_positive=up",
        "# frequency_unit=Hz",
        "# angle_unit=degree",
        "# magnitude_unit=db",
        "# normalization_reference=on_axis_per_frequency",
        "# interpolation_method=linear",
        "# interpolation_implementation=htdt-grid-linear",
        "# interpolation_version=1",
        "# evidence_kind=user_defined",
        "# source_name=fixture polar table",
        "# source_version=1",
        "# source_reference=ui-test",
    ]
    header = (
        "frequency_hz,horizontal_angle_deg,vertical_angle_deg,"
        "magnitude,magnitude_unit"
    )
    rows = []
    for frequency in (500.0, 1000.0):
        for horizontal in (-30.0, 0.0, 30.0):
            magnitude = 0.0 if horizontal == 0.0 else -3.0
            rows.append(
                f"{frequency},{horizontal},0.0,{magnitude},db"
            )
    return ("\n".join([*metadata, header, *rows]) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# #444 — equipment/source library
# ---------------------------------------------------------------------------


def test_equipment_library_create_and_version(app, repositories):
    scene_repository, _, variant_repository = repositories
    service = EquipmentLibraryService(scene_repository, variant_repository)

    definition = _definition(service)
    assert definition.directivity.tier == "unknown"
    assert definition in service.definitions()
    assert "UNKNOWN" in "\n".join(capability_preview(definition))

    v2 = service.create_next_version(
        definition,
        version="2",
        user_label="テストスピーカー改",
        manufacturer="Acme",
        model="S1",
        width_m=0.25,
        height_m=0.4,
        depth_m=0.3,
        evidence_kind="user_defined",
        source_name="fixture",
        source_version="2",
        source_reference="ui-test",
        actor="ui-test",
    )
    assert v2.definition_id == definition.definition_id
    assert v2.version == "2"
    assert v2.directivity.tier == "unknown"
    assert v2.sensitivity == definition.sensitivity
    assert service.definition_versions(definition.definition_id) == ("1", "2")


def test_equipment_library_directivity_import(
    app, repositories, tmp_path: Path
):
    scene_repository, _, variant_repository = repositories
    service = EquipmentLibraryService(scene_repository, variant_repository)
    definition = _definition(service)
    asset = tmp_path / "polar.csv"
    raw = _polar_table_bytes()
    asset.write_bytes(raw)

    result_line = service.import_directivity(
        definition_sha256=definition.semantic_sha256,
        file_path=asset,
        adapter_id="htdt.polar-table-adapter",
    )
    assert result_line.startswith("IMPORTED")
    versions = service.definition_versions(definition.definition_id)
    assert len(versions) == 2
    updated = service.definitions()[-1]
    assert updated.directivity.tier == "magnitude_only"
    assert updated.directivity.data_asset_sha256 == sha256(raw).hexdigest()


def test_equipment_library_dialog_smoke(app, repositories):
    scene_repository, _, variant_repository = repositories
    service = EquipmentLibraryService(scene_repository, variant_repository)
    dialog = EquipmentLibraryDialog(service)
    assert dialog.definition_list.count() == 0
    _definition(service)
    dialog.refresh_definitions()
    assert dialog.definition_list.count() == 1
    dialog.definition_list.setCurrentRow(0)
    assert dialog.preview_label.text()


# ---------------------------------------------------------------------------
# #454 — standards profile library/editor
# ---------------------------------------------------------------------------


def _criterion(criterion_id="viewing-angle"):
    from htdt.cad_standards import (
        CriterionDefinition,
        CriterionRule,
        CriterionSource,
    )

    return CriterionDefinition(
        criterion_id=criterion_id,
        name="視聴角度",
        source=CriterionSource(
            publisher="fixture",
            document_title="fixture doc",
            document_version="1",
            reference="ui-test",
        ),
        quantity="viewing_angle",
        unit="deg",
        applicable_domains=("seat",),
        rule=CriterionRule(operator="max", maximum=45.0),
    )


def test_standards_profile_library_service(app, repositories):
    scene_repository, _, _ = repositories
    model = StandardsWorkspaceModel(scene_repository, DOCUMENT_ID)
    service = StandardsProfileLibraryService(model.repository)

    created = service.create_user_profile(
        name="マイ基準", criteria=(_criterion(),)
    )
    assert created.profile_kind == "user_defined"
    assert created.profile_id in {
        profile.profile_id for profile in service.profiles()
    }

    builtin = next(
        profile
        for profile in service.profiles()
        if profile.profile_kind == "published"
    )
    cloned = service.clone_profile(
        builtin, name="クローン", criteria=builtin.criteria
    )
    assert cloned.profile_kind == "user_defined"
    assert cloned.profile_id != builtin.profile_id
    assert cloned.criteria == builtin.criteria

    v2 = service.save_new_version(
        created, name="マイ基準v2", criteria=created.criteria
    )
    assert v2.profile_id == created.profile_id
    assert v2.version != created.version
    # semantic_payload covers version+name+criteria, so a new version is a
    # new semantic identity (also required by the UNIQUE column).
    assert v2.profile_semantic_hash != created.profile_semantic_hash
    # Both immutable versions remain retrievable.
    versions = service.profile_versions(created.profile_id)
    assert len(versions) == 2

    exported = service.export_profile_json(created)
    reimported = service.import_profile_json(exported)
    assert (
        reimported.profile_semantic_hash == created.profile_semantic_hash
    )
    with pytest.raises(Exception):
        service.import_profile_json('{"bogus": true}')

    with pytest.raises(ValueError):
        service.save_new_version(
            builtin, name="x", criteria=builtin.criteria
        )


def test_standards_profile_editor_dialog_smoke(app, repositories):
    scene_repository, _, _ = repositories
    model = StandardsWorkspaceModel(scene_repository, DOCUMENT_ID)
    service = StandardsProfileLibraryService(model.repository)
    dialog = StandardsProfileEditorDialog(service)
    assert dialog.profile_combo.count() >= 2


# ---------------------------------------------------------------------------
# #477 — playback chain authoring + O100D evaluation
# ---------------------------------------------------------------------------


def test_playback_chain_service_end_to_end(app, repositories):
    scene_repository, revision, variant_repository = repositories
    equipment_service = EquipmentLibraryService(
        scene_repository, variant_repository
    )
    definition = _definition(equipment_service)
    variant = _persist_variant(variant_repository, revision, definition)

    service = PlaybackChainService(scene_repository, DOCUMENT_ID)
    capability = service.create_amplifier_capability(
        user_label="テストアンプ",
        output_id="amp-fl",
        evidence_kind="user_defined",
        source_name="fixture",
        source_version="1",
        source_reference="ui-test",
        supported_min_load_ohm=4.0,
        supported_max_load_ohm=16.0,
        simultaneous_channel_count=1,
        shared_supply_evidence=False,
        continuous_voltage_v_rms=12.0,
        continuous_duration_s=60.0,
        peak_voltage_v_rms=20.0,
        peak_duration_s=0.05,
    )
    assert capability in service.amplifier_capabilities()

    load = service.create_speaker_load(
        equipment_sha256=definition.semantic_sha256,
        semantics="nominal_impedance_only",
        resistance_ohm=8.0,
        evidence_kind="user_defined",
        source_name="fixture",
        source_version="1",
        source_reference="ui-test",
    )
    assert load in service.speaker_loads()

    scenario = service.create_scenario(
        variant_id=variant.variant_id,
        source_entity_id="speaker-fl",
        channel_role_id="FL",
        amplifier_sha256=capability.semantic_sha256,
        speaker_load_sha256=load.semantic_sha256,
        source_equipment_sha256=definition.semantic_sha256,
        simultaneous_output_ids=("amp-fl",),
        requested_input_v_rms=1.0,
        requested_continuous_v_rms=10.0,
        requested_peak_v_rms=18.0,
        continuous_duration_s=60.0,
        peak_duration_s=0.05,
        target_spl_db_spl=85.0,
        acoustic_target_distance_m=2.0,
        target_mode="continuous",
    )
    assert scenario.simultaneous_channel_condition.output_ids == ("amp-fl",)

    evaluation = service.evaluate(scenario)
    assert evaluation.evaluation_sha256
    summary = evaluation_summary(evaluation)
    assert "リミッター" in summary
    # Speaker-limited vs amplifier-limited distinction is surfaced.
    assert "アンプ" in summary and "スピーカー" in summary

    # Missing-evidence fields stay honest instead of fabricating watts.
    incomplete = service.create_amplifier_capability(
        user_label="能力不明アンプ",
        output_id="amp-unknown",
        evidence_kind="user_defined",
        source_name="fixture",
        source_version="1",
        source_reference="ui-test",
        supported_min_load_ohm=4.0,
        supported_max_load_ohm=16.0,
        simultaneous_channel_count=1,
        shared_supply_evidence=False,
        continuous_voltage_v_rms=10.0,
        continuous_duration_s=60.0,
    )
    assert "peak_capability" in incomplete.missing_unsupported_fields
    assert "gain_db" in incomplete.missing_unsupported_fields


def test_playback_chain_dialog_smoke(app, repositories):
    scene_repository, _, variant_repository = repositories
    service = PlaybackChainService(scene_repository, DOCUMENT_ID)
    dialog = PlaybackChainDialog(service)
    assert dialog.tabs.count() == 3
