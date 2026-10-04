"""REV44-INSTALL: production registration surfaces for the two dead
authority writers — speaker installation context + installation datum.

Each test drives the real registration affordance on the placement-page
panel (``InstallationPanel._save_context`` / ``_save_datum``), then proves
the downstream consumer reaches the new state: the R110 source compiler
binds the persisted context into the compiled model, the installation
handoff's 設置基準 section resolves AVAILABLE, and the overview
installation domain counts the speaker. Cancel/invalid input leaves the
repositories empty (fail-closed); overlapping override axes, a self-host
pick, an unpersisted equipment pin, and duplicate datum direction walls
all surface typed errors.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest

from htdt.cad_equipment import (
    ClearanceMetadata,
    DirectivityCapability,
    EquipmentDataProvenance,
    MountingMetadata,
    PortMetadata,
    build_equipment_definition,
)
from htdt.cad_equipment_evidence import build_equipment_manual_evidence
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_installation_context import build_installation_context
from htdt.cad_installation_context_repository import (
    CadInstallationContextRepository,
)
from htdt.cad_installation_datum_repository import (
    CadInstallationDatumRepository,
)
from htdt.cad_r110_source_repository import CadR110SourceRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    EquipmentBindingRef,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.cad_walls import make_wall_topology
from htdt.installation_output_authority import (
    InstallationAuthorityError,
    InstallationReportService,
)
from htdt.overview_readiness import OverviewReadinessService


NOW = '2026-10-04T00:00:00+00:00'
DOCUMENT_ID = 'rev44-install-fixture'


def _app():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _provenance(name: str, digit: str) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='user_defined',
        source_name=name,
        source_version='2026-10-04',
        source_reference='rev44-install-fixture',
        source_sha256=digit * 64,
    )


def _equipment(
    definition_id: str = 'speaker-a',
    digit: str = '1',
    mounting_modes=('free_standing', 'wall'),
):
    provenance = _provenance(definition_id, digit)
    return build_equipment_definition(
        definition_id=definition_id,
        version='1',
        identity_kind='user_defined',
        user_label=definition_id,
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.20, y_m=0.25, z_m=0.35),
        acoustic_reference_point_m=Offset3(),
        directivity=DirectivityCapability(
            tier='unknown',
            data_format='unknown',
            provenance=provenance,
        ),
        mounting=MountingMetadata(mounting_modes=mounting_modes),
        port=PortMetadata(port_type='rear', minimum_clearance_m=0.4),
        clearance=ClearanceMetadata(rear_m=0.4),
    )


def _speaker(entity_id: str = 'speaker-fl') -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name='Front Left',
        speaker_role='FL',
        position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
        size_m=Size3(x_m=0.20, y_m=0.25, z_m=0.35),
    )


def _repositories(
    tmp_path: Path,
    *,
    entities=(),
    wall_topology: bool = False,
):
    room = RoomPrism(width_m=6.0, depth_m=6.0, height_m=2.5)
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        SceneDocument(
            document_id=DOCUMENT_ID,
            # schema 3+ is required before a document may carry wall topology.
            schema_version=3 if wall_topology else 2,
            room=room,
            entities=entities or (_speaker(),),
            wall_topology=(
                make_wall_topology(room) if wall_topology else None
            ),
        ),
        parent_revision_id=None,
    ).revision
    variant_repository = CadSystemVariantRepository(scene_repository)
    equipment_repository = CadEquipmentRepository(
        scene_repository,
        variant_repository,
    )
    return (
        scene_repository,
        revision,
        variant_repository,
        equipment_repository,
    )


def _save_equipment(repository, definition) -> None:
    for evidence in build_equipment_manual_evidence(
        definition,
        actor='rev44-install-fixture',
        recorded_at_utc=NOW,
    ):
        repository.save_evidence(evidence)
    repository.save_definition(definition)


def _panel(scene_repository, equipment_repository, document_id):
    from htdt.installation_panel import InstallationPanel

    panel = InstallationPanel(
        scene_repository, equipment_repository, document_id
    )
    panel.refresh()
    return panel


def _set_combo(combo, data) -> None:
    index = combo.findData(data)
    assert index >= 0, f'combo is missing item data {data!r}'
    combo.setCurrentIndex(index)


def _persist_variant(variant_repository, revision, definition, name='v1'):
    variant = build_system_variant(
        baseline=revision,
        name=name,
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front Left'),
        ),
        proposed_entities=(),
        equipment_bindings=(
            EquipmentBindingRef(
                entity_id='speaker-fl',
                equipment_definition_id=definition.definition_id,
                equipment_definition_version=definition.version,
                equipment_definition_sha256=definition.semantic_sha256,
            ),
        ),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant)
    return variant


class _EmptyMeasurements:
    def list_measurements(self, document_id):
        return ()


class _EmptyPredictions:
    def list_results(self, document_id):
        return ()


class _EmptySearch:
    def list_specs(self, document_id):
        return ()


class _EmptyValidation:
    def inspect_for_search_spec(self, *args, **kwargs):
        return ()


def _overview_service(scene_repository, installation_source):
    return OverviewReadinessService(
        scene_repository,
        _EmptyMeasurements(),
        _EmptyPredictions(),
        _EmptySearch(),
        _EmptyValidation(),
        installation_source=installation_source,
    )


def _installation_domain(service, document_id):
    model = service.read(document_id)
    return next(
        (
            domain
            for domain in model.secondary_domains
            if domain.domain_id == 'installation'
        ),
        None,
    )


def test_context_registration_reaches_consumers(tmp_path: Path) -> None:
    _app()
    scene_repository, revision, variant_repository, equipment_repository = (
        _repositories(tmp_path)
    )
    definition = _equipment()
    _save_equipment(equipment_repository, definition)
    context_repository = CadInstallationContextRepository(
        scene_repository, equipment_repository
    )
    panel = _panel(scene_repository, equipment_repository, DOCUMENT_ID)

    _set_combo(panel.speaker_combo, 'speaker-fl')
    _set_combo(panel.equipment_combo, definition.semantic_sha256)
    # Mounting combo is restricted to the definition's declared modes.
    declared = {
        panel.mounting_combo.itemData(i)
        for i in range(panel.mounting_combo.count())
    }
    assert declared == {'free_standing', 'wall'}
    _set_combo(panel.mounting_combo, 'free_standing')
    panel.baffle_combo.setCurrentIndex(
        panel.baffle_combo.findData('free_space')
    )
    panel.actor_edit.setText('installer-a')

    panel._save_context()
    assert panel.context_error_label.text() == ''

    persisted = context_repository.get_context_for_entity(
        DOCUMENT_ID, 'speaker-fl'
    )
    assert persisted is not None
    assert persisted.entity_id == 'speaker-fl'
    assert persisted.equipment.equipment_definition_sha256 == (
        definition.semantic_sha256
    )
    assert (
        persisted.selected_mounting_mode == 'free_standing'
    )
    assert persisted.baffle_state == 'free_space'

    # The append-only chain lists every persisted record for the entity.
    chain = context_repository.list_contexts_for_entity(
        DOCUMENT_ID, 'speaker-fl'
    )
    assert len(chain) == 1
    panel.actor_edit.setText('installer-b')
    panel._save_context()
    chain = context_repository.list_contexts_for_entity(
        DOCUMENT_ID, 'speaker-fl'
    )
    assert len(chain) == 2
    assert chain[0].context_id != chain[1].context_id
    assert (
        context_repository.get_context_for_entity(
            DOCUMENT_ID, 'speaker-fl'
        ).context_id
        == chain[-1].context_id
    )

    # Reloaded context re-evaluates against the current head.
    evaluation = context_repository.evaluate_for_entity(
        DOCUMENT_ID, 'speaker-fl'
    )
    assert evaluation is not None
    states = {check.check: check.state for check in evaluation.checks}
    assert states['mounting_mode'] == 'PASS'
    assert evaluation.mounting_compatible is True

    # Consumer 1: the R110 source compiler binds the persisted context.
    variant = _persist_variant(variant_repository, revision, definition)
    r110_repository = CadR110SourceRepository(
        scene_repository,
        variant_repository,
        equipment_repository,
    )
    model = r110_repository.compile_for_variant_source(
        system_variant_id=variant.variant_id,
        source_entity_id='speaker-fl',
    )
    assert model.installation_context_sha256 == chain[-1].semantic_sha256

    # Consumer 2: the overview installation domain counts the speaker.
    service = _overview_service(scene_repository, context_repository)
    domain = _installation_domain(service, DOCUMENT_ID)
    assert domain is not None
    assert domain.state_label == '1/1 スピーカー'


def test_context_surface_fail_closed(tmp_path: Path) -> None:
    _app()
    scene_repository, _revision, _variant_repository, equipment_repository = (
        _repositories(tmp_path)
    )
    definition = _equipment()
    _save_equipment(equipment_repository, definition)
    context_repository = CadInstallationContextRepository(
        scene_repository, equipment_repository
    )
    panel = _panel(scene_repository, equipment_repository, DOCUMENT_ID)
    _set_combo(panel.speaker_combo, 'speaker-fl')
    _set_combo(panel.equipment_combo, definition.semantic_sha256)
    _set_combo(panel.mounting_combo, 'free_standing')

    # Cancel path: no actor name → typed error, nothing persisted.
    panel._save_context()
    assert panel.context_error_label.text() != ''
    assert (
        context_repository.list_contexts_for_entity(
            DOCUMENT_ID, 'speaker-fl'
        )
        == ()
    )

    # Non-default directivity without a recorded rationale fails closed.
    panel.actor_edit.setText('installer-a')
    panel.directivity_combo.setCurrentIndex(
        panel.directivity_combo.findData('iec_baffle')
    )
    panel._save_context()
    assert '根拠' in panel.context_error_label.text()
    assert (
        context_repository.list_contexts_for_entity(
            DOCUMENT_ID, 'speaker-fl'
        )
        == ()
    )

    # Overlapping override axes fail closed inside the form.
    panel.directivity_combo.setCurrentIndex(0)
    panel._add_override_row()
    panel._add_override_row()
    rows = [
        panel.override_rows.itemAt(i).widget()
        for i in range(panel.override_rows.count())
    ]
    for row in rows:
        row.axis_combo.setCurrentIndex(row.axis_combo.findData('rear'))
        row.rationale_edit.setText('設置制約による縮小')
    panel._save_context()
    assert '一意' in panel.context_error_label.text()
    assert (
        context_repository.list_contexts_for_entity(
            DOCUMENT_ID, 'speaker-fl'
        )
        == ()
    )

    # A host pick equal to the entity itself is a typed model error.
    with pytest.raises(
        ValueError, match='installation host cannot be the entity itself'
    ):
        build_installation_context(
            context_id='ctx-self-host',
            document_id=DOCUMENT_ID,
            entity_id='speaker-fl',
            equipment_definition=definition,
            selected_mounting_mode='free_standing',
            provenance=(_provenance('install', '6'),),
            created_at_utc=NOW,
            host_entity_id='speaker-fl',
        )

    # An equipment pin that was never persisted fails the repository check.
    ghost = _equipment(definition_id='ghost-speaker', digit='9')
    ghost_context = build_installation_context(
        context_id='ctx-ghost',
        document_id=DOCUMENT_ID,
        entity_id='speaker-fl',
        equipment_definition=ghost,
        selected_mounting_mode='free_standing',
        provenance=(_provenance('install', '7'),),
        created_at_utc=NOW,
    )
    with pytest.raises(ValueError, match='unpersisted EquipmentDefinition'):
        context_repository.save_context(ghost_context)
    assert (
        context_repository.list_contexts_for_entity(
            DOCUMENT_ID, 'speaker-fl'
        )
        == ()
    )


def test_datum_registration_reaches_handoff(tmp_path: Path) -> None:
    _app()
    scene_repository, revision, _variant_repository, equipment_repository = (
        _repositories(tmp_path, wall_topology=True)
    )
    datum_repository = CadInstallationDatumRepository(scene_repository)
    panel = _panel(scene_repository, equipment_repository, DOCUMENT_ID)

    assert panel.save_datum_button.isEnabled()
    _set_combo(panel.revision_combo, revision.revision_id)
    walls = revision.document.wall_topology.walls
    x_wall, y_wall = walls[0].wall_id, walls[1].wall_id
    _set_combo(panel.x_wall_combo, x_wall)
    _set_combo(panel.y_wall_combo, y_wall)
    _set_combo(panel.anchor_vertex_combo, 'front-left')
    panel._save_datum()
    assert panel.datum_error_label.text() == ''

    versions = datum_repository.list_datum_versions(f'datum-{DOCUMENT_ID}')
    assert len(versions) == 1
    datum = versions[0]
    assert datum.version == '1'
    assert datum.scene_revision_id == revision.revision_id
    assert datum.scene_content_hash == revision.content_hash
    assert datum.x_direction_wall_id == x_wall
    assert datum.y_direction_wall_id == y_wall

    # Append-only: a second save is a new version on the same datum id.
    panel._save_datum()
    versions = datum_repository.list_datum_versions(f'datum-{DOCUMENT_ID}')
    assert [item.version for item in versions] == ['1', '2']

    # Consumer: the installation handoff's 設置基準 section resolves.
    service = InstallationReportService.for_scene_repository(scene_repository)
    output = service.build_installation_output_from_authorities(
        scene_revision_id=revision.revision_id,
    )
    assert output.datum is not None
    assert output.datum.status == 'AVAILABLE'
    section = next(
        item for item in output.sections if item.section == 'installation_datum'
    )
    assert section.status == 'AVAILABLE'

    # The pin is exact: a different revision must fail closed.
    other_revision = scene_repository.save(
        SceneDocument(
            document_id=DOCUMENT_ID,
            schema_version=3,
            room=RoomPrism(width_m=7.0, depth_m=6.0, height_m=2.5),
            entities=(_speaker(),),
            wall_topology=make_wall_topology(
                RoomPrism(width_m=7.0, depth_m=6.0, height_m=2.5)
            ),
        ),
        parent_revision_id=revision.revision_id,
    ).revision
    with pytest.raises(InstallationAuthorityError):
        service.build_installation_output_from_authorities(
            scene_revision_id=other_revision.revision_id,
        )


def test_datum_surface_fail_closed(tmp_path: Path) -> None:
    _app()
    scene_repository, revision, _variant_repository, equipment_repository = (
        _repositories(tmp_path, wall_topology=True)
    )
    datum_repository = CadInstallationDatumRepository(scene_repository)
    panel = _panel(scene_repository, equipment_repository, DOCUMENT_ID)

    walls = revision.document.wall_topology.walls
    _set_combo(panel.x_wall_combo, walls[0].wall_id)
    _set_combo(panel.y_wall_combo, walls[0].wall_id)
    panel._save_datum()
    assert '別の壁' in panel.datum_error_label.text()
    assert datum_repository.list_datums(DOCUMENT_ID) == ()

    # A scene without wall topology keeps the surface fail-closed.
    scene_repository2, _revision2, _vr2, equipment_repository2 = (
        _repositories(tmp_path / 'nowalls')
    )
    panel2 = _panel(
        scene_repository2, equipment_repository2, DOCUMENT_ID
    )
    assert not panel2.save_datum_button.isEnabled()
    assert panel2.datum_hint_label.text() != ''
    panel2._save_datum()
    assert (
        CadInstallationDatumRepository(scene_repository2).list_datums(
            DOCUMENT_ID
        )
        == ()
    )


def test_workspace_mounts_installation_panel(tmp_path: Path) -> None:
    """The placement page hosts the panel and selection drives it."""
    _app()
    from PySide6.QtCore import QPointF
    from PySide6.QtWidgets import QFrame

    from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
    from htdt.room_workspace import RoomWorkspace

    class _FakePlotter:
        def remove_actor(self, *_args, **_kwargs) -> None:
            pass

        def render(self) -> None:
            pass

        def add_mesh(self, *_args, **_kwargs):
            return None

    class _FakeViewport(QFrame):
        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self.interactor = self
            self.plotter = _FakePlotter()

        def render_document(self, document, **kwargs) -> None:
            pass

        def fit_scene(self) -> None:
            pass

        def focus_entity(self, entity_id) -> None:
            pass

        def focus_entities(self, entity_ids) -> None:
            pass

        def _last_display_position(self) -> QPointF:
            return QPointF(10.0, 10.0)

        def pick_world_position(self, _position):
            return (1.0, -2.0, 0.0)

    repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: _FakeViewport(parent),
    )
    try:
        assert workspace.installation_panel is not None
        workspace.set_context('placement')
        # The f1 scene carries three speakers; all appear in the pick.
        assert workspace.installation_panel.speaker_combo.count() == 3
        workspace.installation_panel.set_selected_entity('speaker-c')
        assert (
            workspace.installation_panel.speaker_combo.currentData()
            == 'speaker-c'
        )
    finally:
        workspace.close()
        workspace.deleteLater()
        _app().processEvents()


def test_context_list_shows_authority_chain(tmp_path: Path) -> None:
    """The per-entity list surfaces the append-only chain honestly."""
    _app()
    scene_repository, _revision, _variant_repository, equipment_repository = (
        _repositories(tmp_path)
    )
    definition = _equipment()
    _save_equipment(equipment_repository, definition)
    panel = _panel(scene_repository, equipment_repository, DOCUMENT_ID)
    _set_combo(panel.speaker_combo, 'speaker-fl')
    _set_combo(panel.equipment_combo, definition.semantic_sha256)
    _set_combo(panel.mounting_combo, 'free_standing')
    panel.actor_edit.setText('installer-a')
    panel._save_context()
    panel.actor_edit.setText('installer-b')
    panel._save_context()

    assert panel.context_list.count() == 2
    texts = [
        panel.context_list.item(i).text()
        for i in range(panel.context_list.count())
    ]
    assert any('installer-a' in text for text in texts)
    assert any('installer-b' in text for text in texts)
    # Selecting the latest record shows its evaluation.
    panel.context_list.setCurrentRow(1)
    assert '合格' in panel.evaluation_label.text()
