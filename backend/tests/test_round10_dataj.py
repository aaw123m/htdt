"""Round-10 convergence: compare eligibility, dataset read-time
verification, bilingual report renderer, and the project lifecycle UI
wiring (data-safety #8)."""

from __future__ import annotations

import base64
import os
import sqlite3
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from fastapi.testclient import TestClient

from htdt.main import create_app


def _context() -> dict:
    return {
        'room': {'width_m': 4.0, 'depth_m': 5.0, 'height_m': 2.4},
        'speakers': [{'speaker_id': 'FL', 'role': 'front_left', 'position': {'x_m': 1.0, 'y_m': 1.0, 'z_m': 1.0}}],
        'measurement_point': {'point_id': 'MLP', 'label': 'MLP', 'position': {'x_m': 2.0, 'y_m': 3.0, 'z_m': 1.0}},
        'avr': {'manufacturer': 'Yamaha', 'model': 'RX-A4A'},
    }


def _raw(offset: float = 0.0) -> str:
    data = '\n'.join(
        f'{f} {level + offset}'
        for f, level in ((20, 70), (40, 71), (80, 72), (160, 73), (320, 74))
    ) + '\n'
    return base64.b64encode(data.encode()).decode()


def _import(
    client: TestClient,
    project_id: str,
    context_id: str,
    *,
    filename: str,
    offset: float = 0.0,
    evidence_type: str = 'measured',
    quality_status: str = 'usable',
) -> dict:
    response = client.post(
        f"/api/projects/{project_id}/measurements",
        json={
            'filename': filename,
            'raw_base64': _raw(offset),
            'context_id': context_id,
            'channel_role': 'front_left',
            'evidence_type': evidence_type,
            'source_speaker_ids': ['FL'],
            'radiation_scope': 'single',
            'quality_status': quality_status,
            'quality_reasons': ['fixture'] if quality_status != 'usable' else [],
            'quality_source': 'manual',
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _project(client: TestClient) -> tuple[dict, dict]:
    project = client.post('/api/projects', json={'name': 'Room'}).json()
    context = client.post(
        f"/api/projects/{project['id']}/contexts", json=_context()
    ).json()
    return project, context


def _compare(client: TestClient, project_id: str, a: dict, b: dict, **extra):
    return client.post(
        f"/api/projects/{project_id}/comparisons",
        json={
            'dataset_a_id': a['dataset_id'],
            'dataset_b_id': b['dataset_id'],
            'low_hz': 30,
            'high_hz': 200,
            'reference_low_hz': 40,
            'reference_high_hz': 160,
            **extra,
        },
    )


# -- parity #8: compare eligibility --------------------------------------

def test_comparison_verdict_fields_and_eligible_defaults(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project, context = _project(client)
    a = _import(client, project['id'], context['id'], filename='a.txt')
    b = _import(client, project['id'], context['id'], filename='b.txt', offset=-2)

    response = _compare(client, project['id'], a, b)
    assert response.status_code == 201, response.text
    result = response.json()['result']
    assert result['level_compatibility'] == 'normalized_shape_comparable'
    assert result['forced'] is False
    assert result['eligibility'] == {'a': 'eligible', 'b': 'eligible'}
    assert result['label_a'].startswith('front_left')
    assert result['label_b'].startswith('front_left')

    # Verdict fields persist on the stored comparison row.
    listing = client.get(f"/api/projects/{project['id']}/comparisons").json()
    stored = next(
        item for item in listing if item['id'] == response.json()['id']
    )
    assert stored['result']['level_compatibility'] == 'normalized_shape_comparable'
    assert stored['result']['eligibility'] == {'a': 'eligible', 'b': 'eligible'}


def test_comparison_refuses_ineligible_without_force(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project, context = _project(client)
    a = _import(client, project['id'], context['id'], filename='a.txt')
    bad = _import(
        client, project['id'], context['id'], filename='bad.txt',
        offset=4.0, quality_status='invalid',
    )
    response = _compare(client, project['id'], a, bad)
    assert response.status_code == 422
    assert 'force=true' in response.json()['detail']

    forced = _compare(client, project['id'], a, bad, force=True)
    assert forced.status_code == 201, forced.text
    result = forced.json()['result']
    assert result['forced'] is True
    assert result['level_compatibility'] == 'diagnostic_only'
    assert result['eligibility']['b'].startswith('quality_status is invalid')


def test_comparison_predicted_evidence_needs_force(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project, context = _project(client)
    a = _import(client, project['id'], context['id'], filename='a.txt')
    predicted = _import(
        client, project['id'], context['id'], filename='pred.txt',
        evidence_type='predicted',
    )
    assert _compare(client, project['id'], a, predicted).status_code == 422
    forced = _compare(client, project['id'], a, predicted, force=True)
    assert forced.status_code == 201, forced.text
    assert forced.json()['result']['level_compatibility'] == 'diagnostic_only'
    assert 'predicted' in forced.json()['result']['eligibility']['b']


# -- parity #9: dataset read-time verification ----------------------------

def _tamper_dataset(tmp_path: Path, dataset_id: str) -> None:
    store = tmp_path / 'htdt.sqlite3'
    with sqlite3.connect(store) as connection:
        connection.execute(
            "UPDATE datasets SET level_blob = ? WHERE id = ?",
            (b'tampered', dataset_id),
        )


def test_dataset_integrity_verified_on_read(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project, context = _project(client)
    imported = _import(client, project['id'], context['id'], filename='a.txt')

    assert client.get('/api/integrity').json() == {'status': 'ok', 'problems': []}

    _tamper_dataset(tmp_path, imported['dataset_id'])

    # Feature candidates (descriptor read) and comparison reads both 409.
    candidates = client.get(
        f"/api/projects/{project['id']}/datasets/{imported['dataset_id']}/feature-candidates"
    )
    assert candidates.status_code == 409
    b = _import(client, project['id'], context['id'], filename='b.txt')
    response = _compare(client, project['id'], imported, b)
    assert response.status_code == 409

    problems = client.get('/api/integrity').json()
    assert problems['status'] == 'error'
    assert any(
        p == f"dataset_hash_mismatch:{imported['dataset_id']}"
        for p in problems['problems']
    )


def test_dataset_descriptor_reports_integrity_fields(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project, context = _project(client)
    imported = _import(client, project['id'], context['id'], filename='a.txt')
    candidates = client.get(
        f"/api/projects/{project['id']}/datasets/{imported['dataset_id']}/feature-candidates"
    )
    assert candidates.status_code == 200, candidates.text
    payload = candidates.json()
    assert payload['dataset_id'] == imported['dataset_id']


# -- parity #10: bilingual report renderer ---------------------------------

def test_report_lang_field_and_renderer(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project, context = _project(client)
    a = _import(client, project['id'], context['id'], filename='a.txt')
    b = _import(client, project['id'], context['id'], filename='b.txt', offset=-2)
    comparison = _compare(client, project['id'], a, b).json()

    base = f"/api/projects/{project['id']}/comparisons/{comparison['id']}/report"
    en_json = client.get(f'{base}.json').json()
    assert en_json['lang'] == 'en'
    ja_json = client.get(f'{base}.json?lang=ja').json()
    assert ja_json['lang'] == 'ja'
    assert ja_json['interpretation_notice'] != en_json['interpretation_notice']
    assert client.get(f'{base}.json?lang=xx').status_code == 422

    en_html = client.get(f'{base}.html').text
    assert 'Comparison Report' in en_html
    ja_html = client.get(f'{base}.html?lang=ja').text
    assert ja_html != en_html
    assert '比較レポート' in ja_html
    # Verdict section renders persisted verdict fields.
    assert 'diagnostic_only' in ja_html or '正規化形状比較可' in ja_html or 'normalized_shape_comparable' in ja_html


# -- data-safety #8: lifecycle UI ------------------------------------------

def _qt_app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_project_library_lifecycle_wiring(tmp_path, monkeypatch) -> None:
    """Archive/restore/delete reach the lifecycle authority from the page."""
    app = _qt_app()
    from PySide6.QtWidgets import QMessageBox

    from htdt.application_pages import (
        ProjectLibraryPage,
        ProjectLibraryService,
    )
    from htdt.cad_repository import SceneRepository
    from htdt.cad_scene import make_empty_scene, make_f1_scene

    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_empty_scene('doc-alpha'), parent_revision_id=None)
    repository.save(make_f1_scene(), parent_revision_id=None)

    service = ProjectLibraryService(repository)
    current_document_id = 'fixture-f1'
    page = ProjectLibraryPage(
        service, current_document_id=lambda: current_document_id
    )
    try:
        entries = service.list_projects()
        assert {e.document_id for e in entries} == {
            'doc-alpha', 'fixture-f1'
        }
        assert all(not e.archived for e in entries)

        # Select the non-current row: archive+delete arm, restore gated.
        row = next(
            r for r in range(page.table.rowCount())
            if page.table.item(r, 0).text() == 'doc-alpha'
        )
        page.table.selectRow(row)
        app.processEvents()
        assert page.archive_button.isEnabled()
        assert page.delete_button.isEnabled()
        assert not page.restore_button.isEnabled()
        assert page.open_button.isEnabled()

        page.archive_button.click()
        app.processEvents()
        entry = next(
            e for e in service.list_projects()
            if e.document_id == 'doc-alpha'
        )
        assert entry.archived
        assert page.table.item(row, 4).text() == 'アーカイブ済み'
        # Refresh clears selection; re-select the now-archived row.
        page.table.selectRow(row)
        app.processEvents()
        # Archived rows stay listed but refuse open; restore arms.
        assert not page.open_button.isEnabled()
        assert not page.archive_button.isEnabled()
        assert page.restore_button.isEnabled()
        assert page.delete_button.isEnabled()

        page.restore_button.click()
        app.processEvents()
        page.table.selectRow(row)
        app.processEvents()
        entry = next(
            e for e in service.list_projects()
            if e.document_id == 'doc-alpha'
        )
        assert not entry.archived

        # Delete: confirm dialog approved -> plan -> archive -> delete.
        monkeypatch.setattr(
            QMessageBox,
            'exec',
            lambda self: QMessageBox.StandardButton.Yes,
        )
        monkeypatch.setattr(
            QMessageBox, 'information', staticmethod(lambda *a, **k: None)
        )
        monkeypatch.setattr(
            QMessageBox, 'warning', staticmethod(lambda *a, **k: None)
        )
        page.delete_button.click()
        app.processEvents()
        remaining = {e.document_id for e in service.list_projects()}
        assert remaining == {'fixture-f1'}

        # The current document is never a lifecycle target.
        page.table.selectRow(0)
        app.processEvents()
        assert not page.archive_button.isEnabled()
        assert not page.delete_button.isEnabled()
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_project_library_delete_refuses_on_blockers(tmp_path, monkeypatch) -> None:
    """A blocked plan never reaches the confirm dialog."""
    app = _qt_app()
    from PySide6.QtWidgets import QMessageBox

    from htdt.application_pages import (
        ProjectLibraryPage,
        ProjectLibraryService,
    )
    from htdt.cad_repository import SceneRepository
    from htdt.cad_scene import make_empty_scene, make_f1_scene

    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_empty_scene('doc-alpha'), parent_revision_id=None)
    repository.save(make_f1_scene(), parent_revision_id=None)
    service = ProjectLibraryService(repository)

    class _BlockedPlan:
        display_name = 'doc-alpha'
        executable = False

        class _Blocker:
            kind = 'test_blocker'
            detail = 'テスト用ブロッカー'

        hard_blockers = (_Blocker(),)
        total_rows = 0
        estimated_bytes = 0
        authorities = ()

        class _Assets:
            shared_asset_count = 0
            shared_asset_bytes = 0
            local_asset_count = 0
            local_asset_bytes = 0

        assets = _Assets()
        pending_mission_count = 0
        pending_inbox_item_count = 0

    monkeypatch.setattr(
        ProjectLibraryService,
        'plan_project_deletion',
        lambda self, project_id: _BlockedPlan(),
    )
    deleted = {'called': False}
    monkeypatch.setattr(
        ProjectLibraryService,
        'delete_project',
        lambda *a, **k: deleted.__setitem__('called', True),
    )
    monkeypatch.setattr(
        QMessageBox, 'exec', lambda self: QMessageBox.StandardButton.Ok
    )
    monkeypatch.setattr(
        QMessageBox, 'information', staticmethod(lambda *a, **k: None)
    )
    monkeypatch.setattr(
        QMessageBox, 'warning', staticmethod(lambda *a, **k: None)
    )

    page = ProjectLibraryPage(service, current_document_id=lambda: 'fixture-f1')
    try:
        row = next(
            r for r in range(page.table.rowCount())
            if page.table.item(r, 0).text() == 'doc-alpha'
        )
        page.table.selectRow(row)
        app.processEvents()
        page.delete_button.click()
        app.processEvents()
        assert not deleted['called']
        assert any(
            e.document_id == 'doc-alpha' for e in service.list_projects()
        )
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()
