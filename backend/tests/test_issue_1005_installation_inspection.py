"""#1005: 設置実現性検査 — mounting feasibility inspection layer.

Covers the read-only view model (context → bound InstallationRequest,
authority-verbatim checks, UNKNOWN-vs-fail vocabulary, evidence-gated
preview volumes, on-site confirmation guidance, head-keyed staleness),
the viewport overlay lifecycle (named non-pickable actors, disclaimer),
the panel (six check reasons + confirmation list), and the workspace
wiring (toggle + live refresh from persisted records).
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from htdt.cad_attachment_models import AssemblyLayer, ConstructionAssembly
from htdt.cad_equipment import (
    DirectivityCapability,
    EquipmentDataProvenance,
    build_equipment_definition,
)
from htdt.cad_equipment_evidence import build_equipment_manual_evidence
from htdt.cad_installation_context import (
    SpeakerInstallationContext,
    build_installation_context,
)
from htdt.cad_installation_feasibility import installation_feasibility
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    SemanticCapabilityBinding,
    Size3,
    scene_content_hash,
)
from htdt.cad_walls import make_wall_topology
from htdt.installation_feasibility_viewmodel import (
    FEASIBILITY_CHECK_LABELS,
    FEASIBILITY_ITEM_STATE_LABELS,
    FEASIBILITY_VERDICT_VOCAB,
    INSTALLATION_FEASIBILITY_DISCLAIMER,
    build_installation_feasibility_preview,
)


NOW = '2026-10-09T00:00:00+00:00'
_ROOM = RoomPrism(width_m=8.0, depth_m=6.0, height_m=3.0)
_TOPOLOGY = make_wall_topology(_ROOM)
_WALL_ID = _TOPOLOGY.walls[0].wall_id  # front wall: x 0→8 at y=0


def _entity(
    entity_id: str,
    *,
    x: float = 2.0,
    y: float = 0.8,
    z: float = 1.5,
    payload_kg: float | None = 10.0,
) -> SceneEntity:
    bindings = (
        (
            SemanticCapabilityBinding(
                capability='mountable',
                parameters={'max_payload_kg': payload_kg},
            ),
        )
        if payload_kg is not None
        else None
    )
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name=entity_id,
        speaker_role='FL',
        position=Position3(x_m=x, y_m=y, z_m=z),
        size_m=Size3(x_m=0.24, y_m=0.25, z_m=0.40),
        semantic_bindings=bindings,
    )


def _stud_wall() -> ConstructionAssembly:
    return ConstructionAssembly(
        assembly_id='asm-w1', element='wall', element_ref=_WALL_ID,
        layers=(
            AssemblyLayer(layer_id='fin', kind='finish', material='drywall',
                          thickness_m=0.0125, evidence_source='capture'),
            AssemblyLayer(layer_id='sub', kind='substrate', material='wood_stud',
                          thickness_m=0.0125, evidence_source='capture'),
            AssemblyLayer(layer_id='cav', kind='cavity', material='unknown',
                          thickness_m=0.10, evidence_source='operator'),
            AssemblyLayer(layer_id='frm', kind='framing', material='wood_stud',
                          thickness_m=0.089, evidence_source='operator'),
        ),
        evidence_source='operator',
    )


def _masonry_wall() -> ConstructionAssembly:
    return ConstructionAssembly(
        assembly_id='asm-w2', element='wall', element_ref=_WALL_ID,
        layers=(
            AssemblyLayer(layer_id='fin', kind='finish', material='drywall',
                          thickness_m=0.0125, evidence_source='capture'),
            AssemblyLayer(layer_id='sub', kind='substrate', material='masonry',
                          thickness_m=0.20, evidence_source='capture'),
        ),
        evidence_source='capture',
    )


def _document(
    *entities: SceneEntity,
    assemblies: tuple[ConstructionAssembly, ...] = (),
) -> SceneDocument:
    return SceneDocument(
        document_id='feas-1005-fixture',
        schema_version=5,
        room=_ROOM,
        wall_topology=_TOPOLOGY,
        entities=entities,
        construction_assemblies=assemblies or None,
    )


def _provenance(name: str) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='user_defined',
        source_name=name,
        source_version='2026-10-09',
        source_reference='issue-1005-fixture',
        source_sha256='ab' * 32,
    )


def _equipment(definition_id: str = 'speaker-a'):
    provenance = _provenance(definition_id)
    return build_equipment_definition(
        definition_id=definition_id,
        version='1',
        identity_kind='user_defined',
        user_label=definition_id,
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.24, y_m=0.25, z_m=0.40),
        acoustic_reference_point_m=Offset3(),
        directivity=DirectivityCapability(
            tier='unknown',
            data_format='unknown',
            provenance=provenance,
        ),
    )


def _context(
    entity_id: str,
    mode: str,
    *,
    host: str | None = None,
    context_id: str | None = None,
    document_id: str = 'feas-1005-fixture',
) -> SpeakerInstallationContext:
    return build_installation_context(
        context_id=context_id or f'ctx-{entity_id}-{mode}',
        document_id=document_id,
        entity_id=entity_id,
        equipment_definition=_equipment(),
        selected_mounting_mode=mode,
        host_entity_id=host,
        provenance=(_provenance('ctx'),),
        created_at_utc=NOW,
    )


def _preview(
    document: SceneDocument,
    *contexts: SpeakerInstallationContext,
    clearances: dict[str, float] | None = None,
):
    return build_installation_feasibility_preview(
        document=document,
        contexts=contexts,
        service_clearances=clearances,
    )


# -- viewmodel binding -----------------------------------------------------------


def test_viewmodel_binds_wall_context_to_exact_element() -> None:
    doc = _document(_entity('spk'), assemblies=(_stud_wall(),))
    preview = _preview(doc, _context('spk', 'wall', host=_WALL_ID))
    assert len(preview.items) == 1
    item = preview.items[0]
    assert item.entity_id == 'spk'
    assert item.element == 'wall'
    assert item.element_ref == _WALL_ID
    assert item.request is not None
    assert item.request.entity_id == 'spk'
    assert item.request.payload_kg == 10.0  # declared mountable rating
    assert item.report is not None
    assert item.overall == 'supported'
    # Five construction checks — substrate/payload/framing/cutout/service.
    assert {row.check for row in item.checks} == {
        'substrate', 'payload', 'framing', 'cutout', 'service_clearance'
    }
    # Checks are the authority output verbatim — same objects' fields.
    authority = installation_feasibility(doc, item.request)
    assert [(r.check, r.verdict, r.detail) for r in item.checks] == [
        (c.check, c.verdict, c.detail) for c in authority.checks
    ]
    assert item.substrate_anchor is not None
    assert item.wall_face is not None
    assert item.wall_face.wall_id == _WALL_ID
    # Face is pinned at the entity's real projection on the wall
    # (x=2.0 along an 8 m front wall at y=0) — not at a unit-step from
    # the wall's from-vertex. Regression: the face used to pin at ~1 m.
    assert item.wall_face.center[0] == pytest.approx(2.0, abs=1e-6)
    assert item.wall_face.center[1] == pytest.approx(0.0, abs=1e-6)
    assert item.substrate_anchor[0] == pytest.approx(2.0, abs=1e-6)


def test_viewmodel_unknown_substrate_is_never_fail() -> None:
    doc = _document(_entity('spk'))  # no assembly → evidence unknown
    preview = _preview(doc, _context('spk', 'wall', host=_WALL_ID))
    item = preview.items[0]
    assert item.report is not None
    assert item.report.overall == 'unknown'
    assert item.overall == 'unknown'
    # Vocabulary: 不明 gray — never the conflict red.
    label, color = FEASIBILITY_VERDICT_VOCAB[item.overall]
    assert label == '不明'
    assert color == '#8a93a3'
    assert color != FEASIBILITY_VERDICT_VOCAB['conflict'][1]
    # UNKNOWN items translate to on-site construction confirmations.
    assert any('基材' in c for c in item.confirmations)


def test_viewmodel_undeclared_payload_never_promotes_supported() -> None:
    # No mountable binding → the load is undeclared. The checks still run
    # verbatim but the headline stays 未評価 gray, never 証拠あり green.
    doc = _document(_entity('spk', payload_kg=None), assemblies=(_stud_wall(),))
    preview = _preview(doc, _context('spk', 'wall', host=_WALL_ID))
    item = preview.items[0]
    assert item.report is not None
    assert 'payload' in item.undeclared_inputs
    assert item.overall is None
    assert item.overall_label == '不明'
    assert any('荷重' in c for c in item.confirmations)
    # Check rows remain the authority's verbatim output.
    assert len(item.checks) == 5


def test_viewmodel_entity_host_is_not_applicable() -> None:
    host = _entity('stand-1', x=4.0, y=4.0, payload_kg=None)
    doc = _document(_entity('spk'), host)
    preview = _preview(doc, _context('spk', 'wall', host='stand-1'))
    item = preview.items[0]
    assert item.host_kind == 'entity'
    assert item.overall == 'not_applicable'
    assert item.report is not None
    assert {r.check for r in item.checks} == {'mount_surface'}
    # Non-construction surface: undeclared payload does not gate it.
    host_no_payload = _entity('spk2', payload_kg=None)
    doc2 = _document(host_no_payload, host)
    preview2 = _preview(doc2, _context('spk2', 'wall', host='stand-1'))
    assert preview2.items[0].overall == 'not_applicable'


def test_viewmodel_unbound_and_missing_and_undeclared_states() -> None:
    doc = _document(_entity('spk'))
    unbound = _preview(doc, _context('spk', 'wall', host=None)).items[0]
    assert unbound.state == 'unbound_host'
    assert unbound.state_label == FEASIBILITY_ITEM_STATE_LABELS['unbound_host']
    assert unbound.overall is None
    assert any('壁' in c for c in unbound.confirmations)

    dangling = _preview(
        doc, _context('spk', 'wall', host='wall:gone->gone')
    ).items[0]
    assert dangling.state == 'unbound_host'

    missing = _preview(
        doc, _context('ghost', 'wall', host=_WALL_ID)
    ).items[0]
    assert missing.state == 'missing_entity'
    assert any('存在しません' in c for c in missing.confirmations)

    undeclared = _preview(doc, _context('spk', 'unknown')).items[0]
    assert undeclared.state == 'undeclared'
    assert any('設置方式' in c for c in undeclared.confirmations)


def test_viewmodel_ceiling_and_floor_mounts() -> None:
    doc = _document(_entity('spk', z=2.4))
    ceiling = _preview(doc, _context('spk', 'ceiling')).items[0]
    assert ceiling.element == 'ceiling'
    assert ceiling.element_ref == 'ceiling'
    assert ceiling.slab_face is not None
    assert ceiling.slab_face.z_m == 3.0
    floor = _preview(doc, _context('spk', 'free_standing')).items[0]
    assert floor.element == 'floor'
    assert floor.slab_face is not None
    assert floor.slab_face.z_m == 0.0


def test_viewmodel_in_wall_cutout_and_cavity_gating() -> None:
    # Entity depth 0.25m vs stud-wall cavity 0.10m → authority conflict.
    doc = _document(_entity('spk'), assemblies=(_stud_wall(),))
    item = _preview(doc, _context('spk', 'in_wall', host=_WALL_ID)).items[0]
    assert item.request is not None and item.request.cutout_required
    assert item.request.cutout_depth_m == pytest.approx(0.25)
    cutout = next(r for r in item.checks if r.check == 'cutout')
    assert cutout.verdict == 'conflict'
    assert 'cavity' in cutout.detail
    # Evidence-backed volumes: required recess + declared cavity both
    # drawable; no invented depth anywhere.
    assert item.cutout_volume is not None
    assert item.cutout_volume.depth_m == pytest.approx(0.25)
    assert item.cavity_volume is not None
    assert item.cavity_volume.depth_m == pytest.approx(0.10)

    # Masonry wall declares no cavity → no cavity prism, UNKNOWN check,
    # and an explicit cavity-confirmation item.
    doc2 = _document(_entity('spk'), assemblies=(_masonry_wall(),))
    item2 = _preview(doc2, _context('spk', 'in_wall', host=_WALL_ID)).items[0]
    cutout2 = next(r for r in item2.checks if r.check == 'cutout')
    assert cutout2.verdict == 'unknown'
    assert item2.cavity_volume is None
    assert item2.cutout_volume is not None  # requirement is still drawable
    assert any('キャビティ' in c for c in item2.confirmations)

    # No assembly at all → cavity unknown → same honesty.
    doc3 = _document(_entity('spk'))
    item3 = _preview(doc3, _context('spk', 'in_wall', host=_WALL_ID)).items[0]
    assert item3.cavity_volume is None
    assert next(r for r in item3.checks if r.check == 'cutout').verdict == 'unknown'


def test_viewmodel_service_clearance_volume_gating() -> None:
    doc = _document(_entity('spk'), assemblies=(_stud_wall(),))
    with_clearance = _preview(
        doc, _context('spk', 'wall', host=_WALL_ID),
        clearances={'spk': 0.08},
    ).items[0]
    assert with_clearance.service_volume is not None
    assert with_clearance.service_volume.depth_m == pytest.approx(0.08)
    check = next(
        r for r in with_clearance.checks if r.check == 'service_clearance'
    )
    assert check.verdict == 'supported'
    # Without a service envelope → no volume, no requirement.
    without = _preview(doc, _context('spk', 'wall', host=_WALL_ID)).items[0]
    assert without.service_volume is None
    assert next(
        r for r in without.checks if r.check == 'service_clearance'
    ).verdict == 'not_applicable'
    # Clearance larger than the declared cavity → conflict, honestly.
    too_deep = _preview(
        doc, _context('spk', 'wall', host=_WALL_ID),
        clearances={'spk': 0.30},
    ).items[0]
    assert next(
        r for r in too_deep.checks if r.check == 'service_clearance'
    ).verdict == 'conflict'


def test_preview_head_keying_and_staleness() -> None:
    doc = _document(_entity('spk'), assemblies=(_stud_wall(),))
    ctx = _context('spk', 'wall', host=_WALL_ID)
    first = _preview(doc, ctx)
    assert first.head_sha256 == scene_content_hash(doc)
    # Scene edit → the rebuilt preview keys to the NEW head.
    moved = doc.model_copy(
        update={
            'entities': (
                _entity('spk', x=3.5).model_copy(
                    update={
                        'position': Position3(x_m=3.5, y_m=0.8, z_m=1.5)
                    }
                ),
            )
        }
    )
    second = _preview(moved, ctx)
    assert second.head_sha256 != first.head_sha256
    assert second.items[0].entity_anchor != first.items[0].entity_anchor


def test_preview_summary_counts_and_legend() -> None:
    doc = _document(
        _entity('ok', payload_kg=10.0),
        _entity('warn', x=3.0, payload_kg=None),
        assemblies=(_stud_wall(),),
    )
    preview = _preview(
        doc,
        _context('ok', 'wall', host=_WALL_ID),
        _context('warn', 'in_wall', host=_WALL_ID, context_id='ctx-warn'),
    )
    assert '記録2件' in preview.summary
    assert '証拠あり' in preview.summary
    labels = [label for label, _color in preview.verdict_legend]
    assert '証拠あり' in labels and '不適合' in labels
    assert '不明' in labels and '対象外' in labels


def test_disclaimer_and_check_vocab() -> None:
    assert '施工許可' in INSTALLATION_FEASIBILITY_DISCLAIMER
    assert '建築基準' in INSTALLATION_FEASIBILITY_DISCLAIMER
    assert '不明' in INSTALLATION_FEASIBILITY_DISCLAIMER
    # Six check ids all have JA labels.
    assert set(FEASIBILITY_CHECK_LABELS) == {
        'mount_surface', 'substrate', 'payload', 'framing',
        'cutout', 'service_clearance',
    }
    # Verdict vocabulary: unknown is gray, conflict is red.
    assert FEASIBILITY_VERDICT_VOCAB['unknown'] == ('不明', '#8a93a3')
    assert FEASIBILITY_VERDICT_VOCAB['conflict'][0] == '不適合'


# -- viewport overlay ------------------------------------------------------------


def _viewport():
    from PySide6.QtWidgets import QApplication

    from htdt.room_viewport import RoomViewport3D

    QApplication.instance() or QApplication(['htdt-test'])
    return RoomViewport3D()


def _feasibility_actor_names(viewport) -> list[str]:
    return [
        name
        for name in viewport.plotter.renderer.actors
        if isinstance(name, str) and name.startswith('installation-feasibility-')
    ]


def test_viewport_glyph_mapping_and_cleanup() -> None:
    viewport = _viewport()
    doc = _document(_entity('spk'), assemblies=(_stud_wall(),))
    preview = _preview(doc, _context('spk', 'in_wall', host=_WALL_ID))
    viewport.render_installation_feasibility_overlay(preview)

    names = _feasibility_actor_names(viewport)
    assert 'installation-feasibility-glyph-spk' in names
    assert 'installation-feasibility-substrate-spk' in names
    assert 'installation-feasibility-face-spk' in names
    assert 'installation-feasibility-face-outline-spk' in names
    # Evidence-backed volumes only.
    assert 'installation-feasibility-vol-cutout-spk' in names
    assert 'installation-feasibility-vol-cavity-spk' in names
    assert 'installation-feasibility-summary' in names
    assert 'installation-feasibility-disclaimer' in names
    assert 'installation-feasibility-legend' in names
    for name in names:
        actor = viewport.plotter.renderer.actors[name]
        assert not actor.GetPickable()

    viewport.clear_installation_feasibility_overlay()
    assert _feasibility_actor_names(viewport) == []


def test_viewport_unknown_glyph_is_gray_not_red() -> None:
    viewport = _viewport()
    doc = _document(_entity('spk'))  # no assembly → unknown
    preview = _preview(doc, _context('spk', 'wall', host=_WALL_ID))
    viewport.render_installation_feasibility_overlay(preview)
    actor = viewport.plotter.renderer.actors[
        'installation-feasibility-glyph-spk'
    ]
    rgb = actor.GetProperty().GetColor()
    expected = tuple(
        int('#8a93a3'[i : i + 2], 16) / 255.0 for i in (1, 3, 5)
    )
    assert rgb == pytest.approx(expected, abs=1e-3)


def test_viewport_no_cavity_volume_without_evidence() -> None:
    viewport = _viewport()
    doc = _document(_entity('spk'), assemblies=(_masonry_wall(),))
    preview = _preview(doc, _context('spk', 'in_wall', host=_WALL_ID))
    viewport.render_installation_feasibility_overlay(preview)
    names = _feasibility_actor_names(viewport)
    # Requirement is drawn (declared depth); the cavity prism is not
    # — the assembly declares none, so no invented depth appears.
    assert 'installation-feasibility-vol-cutout-spk' in names
    assert 'installation-feasibility-vol-cavity-spk' not in names


def test_viewport_none_clears() -> None:
    viewport = _viewport()
    doc = _document(_entity('spk'), assemblies=(_stud_wall(),))
    viewport.render_installation_feasibility_overlay(
        _preview(doc, _context('spk', 'wall', host=_WALL_ID))
    )
    assert _feasibility_actor_names(viewport)
    viewport.render_installation_feasibility_overlay(None)
    assert _feasibility_actor_names(viewport) == []


# -- panel -----------------------------------------------------------------------


def _panel():
    from PySide6.QtWidgets import QApplication

    from htdt.installation_feasibility_panel import (
        RoomInstallationFeasibilityPanel,
    )

    QApplication.instance() or QApplication(['htdt-test'])
    return RoomInstallationFeasibilityPanel()


def test_panel_shows_six_check_reasons_and_confirmations() -> None:
    panel = _panel()
    try:
        doc = _document(_entity('spk'), assemblies=(_stud_wall(),))
        preview = _preview(doc, _context('spk', 'in_wall', host=_WALL_ID))
        panel.show_preview(preview)
        assert panel.items_list.count() == 1
        panel.items_list.setCurrentRow(0)
        detail = panel.detail_label.text()
        # All five construction checks surface with JA labels + authority
        # detail verbatim.
        for label in ('基材', '荷重', '下地', '開口深さ', '保守空間'):
            assert label in detail
        assert 'cavity' in detail  # authority detail passes through
        assert '掘込深さ' in detail  # declared requirement line
        # All inputs declared + all evidence bound → no extra on-site items.
        assert panel.confirm_label.text() == '（追加の現場確認項目なし）'
        assert panel.counts_label.text() == preview.summary
        assert '施工許可' in panel.disclaimer.text()
    finally:
        panel.close()
        panel.deleteLater()


def test_panel_unknown_selection_shows_confirmations() -> None:
    panel = _panel()
    try:
        doc = _document(_entity('spk'))
        preview = _preview(doc, _context('spk', 'wall', host=_WALL_ID))
        panel.show_preview(preview)
        panel.items_list.setCurrentRow(0)
        # UNKNOWN → guidance, never a fail message.
        assert '不適合' not in panel.detail_label.text().split('\n')[1]
        assert '基材' in panel.confirm_label.text()
        assert 'アセンブリ' in panel.confirm_label.text()
    finally:
        panel.close()
        panel.deleteLater()


def test_panel_empty_and_narrow_dpi200_uia() -> None:
    from PySide6.QtCore import Qt

    panel = _panel()
    try:
        panel.resize(320, 640)
        panel.show()
        assert panel.preview_toggle.toolTip()
        assert panel.preview_toggle.focusPolicy() != Qt.NoFocus
        # Keyboard-navigable list + selectable detail/confirmation text.
        assert panel.items_list.focusPolicy() != Qt.NoFocus
        assert panel.detail_label.textInteractionFlags() & (
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        panel.show_preview(
            _preview(
                _document(_entity('spk')),
                _context('spk', 'wall', host=_WALL_ID),
            )
        )
        font = panel.font()
        font.setPointSizeF(font.pointSizeF() * 2.0)
        panel.setFont(font)
        panel.adjustSize()
        assert panel.counts_label.text()
    finally:
        panel.close()
        panel.deleteLater()


# -- workspace wiring ------------------------------------------------------------


def _workspace(tmp_path: Path):
    from PySide6.QtWidgets import QApplication

    from htdt.cad_repository import SceneRepository
    from htdt.room_workspace import RoomWorkspace
    from test_room_cadux import FakeRoomViewport

    app = QApplication.instance() or QApplication(['htdt-test'])
    repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    document = _document(
        _entity('spk', payload_kg=10.0),
        assemblies=(_stud_wall(),),
    )
    repository.save(document, parent_revision_id=None)

    class FeasibilityViewport(FakeRoomViewport):
        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self.feasibility_calls: list = []

        def render_installation_feasibility_overlay(self, preview) -> None:
            self.feasibility_calls.append(preview)

        def clear_installation_feasibility_overlay(self) -> None:
            self.feasibility_calls.append('cleared')

    workspace = RoomWorkspace(
        repository,
        document.document_id,
        viewport_factory=lambda parent: FeasibilityViewport(parent),
    )
    return app, workspace, repository


def _seed_context(workspace, entity_id='spk', mode='wall', host=_WALL_ID):
    definition = _equipment()
    repo = workspace.system_expansion.equipment_repository
    for evidence in build_equipment_manual_evidence(
        definition,
        actor='issue-1005-fixture',
        recorded_at_utc=NOW,
    ):
        repo.save_evidence(evidence)
    repo.save_definition(definition)
    context = _context(
        entity_id,
        mode,
        host=host,
        document_id=workspace.controller.document_id,
    )
    workspace.installation_panel.context_repository.save_context(context)
    return context


def test_workspace_toggle_draws_from_persisted_records(tmp_path: Path) -> None:
    _app, workspace, _repository = _workspace(tmp_path)
    try:
        # No contexts → the layer is inert and honest about it.
        workspace.feasibility_panel.preview_toggle.setChecked(True)
        assert workspace.viewport.feasibility_calls[-1] is None
        assert '取付記録がありません' in (
            workspace.feasibility_panel.counts_label.text()
        )

        _seed_context(workspace)
        workspace.refresh()
        preview = workspace.viewport.feasibility_calls[-1]
        assert preview is not None
        assert len(preview.items) == 1
        item = preview.items[0]
        assert item.entity_id == 'spk'
        assert item.element_ref == _WALL_ID
        assert item.overall == 'supported'
        assert _panel_has_item(workspace.feasibility_panel, 'spk')
        assert _panel_detail_has(workspace.feasibility_panel, '基材')

        workspace.feasibility_panel.preview_toggle.setChecked(False)
        workspace.refresh()
        assert workspace.viewport.feasibility_calls[-1] is None
    finally:
        workspace.close()
        workspace.deleteLater()


def _panel_has_item(panel, entity_id: str) -> bool:
    from PySide6.QtCore import Qt

    for row in range(panel.items_list.count()):
        if (
            panel.items_list.item(row).data(Qt.ItemDataRole.UserRole)
            == entity_id
        ):
            return True
    return False


def _panel_detail_has(panel, needle: str) -> bool:
    panel.items_list.setCurrentRow(0)
    return needle in panel.detail_label.text()


def test_workspace_stale_on_scene_edit(tmp_path: Path) -> None:
    _app, workspace, _repository = _workspace(tmp_path)
    try:
        _seed_context(workspace)
        workspace.feasibility_panel.preview_toggle.setChecked(True)
        workspace.refresh()
        first = workspace.viewport.feasibility_calls[-1]
        assert first is not None
        assert first.head_sha256 == scene_content_hash(
            workspace.controller.document
        )

        # Edit the scene: move the entity — the overlay re-keys to the
        # new head and the anchors follow the live position.
        working = workspace.controller.working
        before = working.committed_document.entity('spk')
        after = before.model_copy(
            update={'position': Position3(x_m=4.0, y_m=0.8, z_m=1.5)}
        )
        working.apply_entity_set_edit(
            replaced_before=(before,), replaced_after=(after,)
        )
        workspace.refresh()
        second = workspace.viewport.feasibility_calls[-1]
        assert second is not None
        assert second.head_sha256 != first.head_sha256
        assert second.items[0].entity_anchor[0] == pytest.approx(4.0)

        # Delete the entity — the context survives and honestly reports
        # the target is gone instead of painting a stale glyph.
        working.apply_entity_set_edit(removed=(after,))
        workspace.refresh()
        third = workspace.viewport.feasibility_calls[-1]
        assert third.items[0].state == 'missing_entity'
        assert third.items[0].entity_anchor is None
    finally:
        workspace.close()
        workspace.deleteLater()
