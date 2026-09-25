"""Deliverables Center catalog derivation (#900)."""

from __future__ import annotations

from types import SimpleNamespace

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene, make_f1_scene
from htdt.deliverables_catalog import DeliverablesCatalogService


def _overview(blockers=(), warnings=()):
    return SimpleNamespace(
        read=lambda document_id: SimpleNamespace(
            blockers=blockers, warnings=warnings
        )
    )


def _catalog(repository, document_id, *, blockers=(), warnings=()):
    service = DeliverablesCatalogService(
        repository,
        document_id,
        overview_service=_overview(blockers=blockers, warnings=warnings),
    )
    return {entry.deliverable_id: entry for entry in service.catalog()}


def test_empty_project_blocks_project_deliverables(tmp_path) -> None:
    """No SceneRevision → project-scoped outputs blocked with the exact
    missing input; the app-global interop export stays available."""

    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    entries = _catalog(repository, 'doc-a')

    assert entries['installation.handoff'].availability == 'blocked'
    assert 'シーンリビジョン' in (
        entries['installation.handoff'].reason or ''
    )
    assert entries['analysis.bundle'].availability == 'blocked'
    assert '測定' in (entries['analysis.bundle'].reason or '')
    assert entries['field.labels'].availability == 'blocked'
    assert (
        entries['commissioning.report'].availability == 'not_applicable'
    )
    # Interoperability export is application-global, not document-bound.
    capture = entries['equipment.capture_catalog']
    assert capture.availability == 'available'
    assert capture.command_id == 'equipment.export_capture_catalog'


def test_f1_project_offers_installation_deliverables(tmp_path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    from htdt.cad_scene import F1_DOCUMENT_ID

    entries = _catalog(repository, F1_DOCUMENT_ID)
    handoff = entries['installation.handoff']
    assert handoff.availability == 'available'
    assert handoff.command_id == 'installation.export_handoff'
    assert any(
        pin.startswith('scene_revision:')
        for pin in handoff.source_authorities
    )
    # F1 has speakers and a measurement point — BOM and labels available.
    assert entries['installation.bom'].availability == 'available'
    assert entries['field.labels'].availability == 'available'


def test_warnings_degrade_but_do_not_block(tmp_path) -> None:
    """Unresolved overview warnings produce 'available with incomplete
    sections' — honest degradation, never silent 'available'."""

    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    from htdt.cad_scene import F1_DOCUMENT_ID

    warning = SimpleNamespace(
        code='measurement.missing', message='測定がありません', action=None
    )
    entries = _catalog(repository, F1_DOCUMENT_ID, warnings=(warning,))
    handoff = entries['installation.handoff']
    assert handoff.availability == 'available_degraded'
    assert handoff.reason is not None


def test_room_blocker_marks_installation_blocked_with_deeplink(
    tmp_path,
) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_empty_scene('doc-a'), parent_revision_id=None)
    blocker = SimpleNamespace(
        code='room.missing', message='部屋がありません', action=None
    )
    entries = _catalog(repository, 'doc-a', blockers=(blocker,))
    handoff = entries['installation.handoff']
    assert handoff.availability == 'blocked'
    assert handoff.action is not None


def test_non_deliverables_are_not_catalogued(tmp_path) -> None:
    """Backup/transfer/diagnostics never appear beside export actions."""

    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    entries = _catalog(repository, 'doc-a')
    assert not any(
        'backup' in entry.deliverable_id
        or 'transfer' in entry.deliverable_id
        or 'project.export' in entry.deliverable_id
        for entry in entries.values()
    )
