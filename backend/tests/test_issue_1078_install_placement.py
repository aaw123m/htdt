"""Regression for #1078: 選択配置を設置済みにする crashed on a stray
``document_id`` argument to ``latest_placement`` (#716-era).

The panel must resolve the exact placement instance, revise it to
``installed`` (append-only lifecycle), and leave the repository authority
intact — not raise ``TypeError``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip('PySide6')

from PySide6.QtWidgets import QApplication

from htdt.cad_acoustic_treatment import (
    TreatmentCoverage,
    build_treatment_placement,
)
from htdt.cad_acoustic_treatment_repository import CadAcousticTreatmentRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, Position3, make_f1_scene
from htdt.room_acoustics_panel import RoomTreatmentPanel
from htdt.room_workspace import RoomWorkspaceController

from test_cad_acoustic_treatment import _porous_definition, _save_definition


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_install_selected_proposed_placement_flips_lifecycle(
    tmp_path: Path,
) -> None:
    _app()
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    head = repository.save(make_f1_scene(), parent_revision_id=None).revision
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    treatment_repo: CadAcousticTreatmentRepository = (
        controller.treatment_repository
    )

    definition, evidence = _porous_definition()
    saved = _save_definition(treatment_repo, definition, evidence)
    proposed = build_treatment_placement(
        definition=saved,
        revision=head,
        instance_id='panel-install-01',
        position=Position3(x_m=0.05, y_m=1.0, z_m=1.2),
        coverage=TreatmentCoverage(width_m=0.6, height_m=1.2),
    )
    treatment_repo.save_placement(proposed)

    panel = RoomTreatmentPanel(controller)
    try:
        panel.refresh()
        assert panel.placements.topLevelItemCount() == 1
        panel.placements.setCurrentItem(panel.placements.topLevelItem(0))

        # Pre-fix this raised TypeError (extra document_id positional arg).
        panel.install_button.click()

        latest = treatment_repo.latest_placement('panel-install-01')
        assert latest is not None
        assert latest.lifecycle == 'installed'
        assert latest.placement_version == 2
        assert latest.previous_placement_sha256 == proposed.placement_sha256
        # v1 history preserved (append-only).
        assert treatment_repo.get_placement('panel-install-01', 1) == proposed
        assert 'install' in panel.status.text() or '設置' in panel.status.text()
    finally:
        panel.deleteLater()


def test_install_selected_without_selection_is_noop(tmp_path: Path) -> None:
    _app()
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    panel = RoomTreatmentPanel(controller)
    try:
        panel.refresh()
        panel.install_button.click()  # no selection — must not raise
    finally:
        panel.deleteLater()
