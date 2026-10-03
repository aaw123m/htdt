"""REV32-TERMS: field explanations, glossary surface, error comprehension
and dialog help buttons on the measurement workflow.

The registry (``measurement_explanations``) is the single source of truth;
these tests lock coverage of the surveyed field/column/status vocabulary
and the real wiring paths — tooltip/WhatsThis on widgets and headers,
status-cell tooltips, the glossary dialog, and error→topic resolution.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QLabel,
    QLineEdit,
    QMessageBox,
)

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.help_registry import build_help_registry
from htdt.localization import PresentationLocale
from htdt.measurement_explanations import (
    FIELD_EXPLANATIONS,
    METRIC_EXPLANATIONS,
    STATUS_EXPLANATIONS,
    apply_explanation,
    explain_combo_items,
    explain_table_columns,
    metric_explanation,
    status_explanation,
)
from htdt.measurement_page_workspace import (
    _CELL_STATUS_LABELS,
    _VARIANT_PURPOSE_LABELS,
    MeasurementPageWorkspace,
)
from htdt.measurement_workflow import MeasurementWorkflowController
from htdt.palette_search import help_destinations
from htdt.user_facing_error import warn_user
from htdt.workflow_help import GlossaryDialog, ReasonTextDialog


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _workspace(tmp_path: Path, **kwargs):
    scene_repository = SceneRepository(tmp_path / "cad.sqlite3")
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    controller = MeasurementWorkflowController(
        scene_repository, revision.document_id
    )
    return controller, MeasurementPageWorkspace(controller, **kwargs)


# ---------------------------------------------------------------------------
# Registry content
# ---------------------------------------------------------------------------


def test_every_explanation_has_plain_ja_meaning() -> None:
    for key, entry in FIELD_EXPLANATIONS.items():
        assert entry.meaning.strip(), key
        text = entry.text()
        assert entry.meaning.split('。')[0][:10] in text or text.startswith(
            entry.meaning[:10]
        ), key
        if entry.unit:
            assert entry.unit in text, key
        if entry.valid:
            assert entry.valid.split(':')[0] in text, key


def test_field_registry_covers_the_surveyed_surface() -> None:
    """Every field/column/metric wired in _wire_explanations has an entry."""
    expected = {
        'import.rew_measurement',
        'import.attachment_kind',
        'assignment.scope',
        'assignment.target',
        'assignment.evidence_type',
        'assignment.channel_role',
        'assignment.radiation_scope',
        'assignment.routing_evidence',
        'assignment.acquisition_revision',
        'assignment.routing_profile',
        'assignment.source_speakers',
        'acquisition.preset',
        'acquisition.mic_orientation',
        'acquisition.mic_manufacturer',
        'acquisition.mic_model',
        'acquisition.mic_serial',
        'acquisition.sample_rate',
        'acquisition.calibration_file',
        'acquisition.calibration_sha',
        'acquisition.output_device',
        'acquisition.avr_model',
        'acquisition.avr_volume',
        'acquisition.avr_processing',
        'acquisition.avr_peq',
        'campaign.plan',
        'campaign.sources',
        'campaign.targets',
        'campaign.purpose',
        'campaign.repeat',
        'campaign.pattern',
        'campaign.variant_plan',
        'campaign.measurement',
        'campaign.table.channel_role',
        'campaign.table.target',
        'campaign.table.repeat',
        'campaign.table.status',
        'campaign.table.measurement',
        'quality.table.channel',
        'quality.table.evidence',
        'quality.table.target',
        'quality.table.quality',
        'quality.table.phase',
        'quality.table.timing',
        'quality.table.placement',
        'quality.table.band',
        'quality.table.disposition',
        'quality.table.retake',
        'quality.smoothing',
        'quality.target',
        'quality.phase_unwrap',
        'quality.spatial_mode',
        'quality.disposition',
        'quality.attach_kind',
        'comparison.preset',
        'comparison.dataset_a',
        'comparison.dataset_b',
        'comparison.band_low',
        'comparison.band_high',
        'comparison.reference_band',
        'comparison.excluded_band',
        'comparison.smoothing',
        'comparison.metrics.name',
        'comparison.metrics.value',
        'comparison.history.created',
        'comparison.history.band',
        'comparison.history.reference',
        'comparison.history.excluded',
        'comparison.history.rms',
        'comparison.history.offset',
        'comparison.history.shape',
        'calibration.table.step',
        'calibration.table.status',
        'calibration.table.check',
        'import.batch_table.file',
        'import.batch_table.status',
        'import.batch_table.band',
        'import.batch_table.phase',
        'import.batch_table.duplicate',
        'import.batch_table.resolution',
        'import.batch_table.saved',
        'editor.pattern_anchor',
        'editor.pattern_spacing',
        'editor.channel_role',
    }
    missing = expected - FIELD_EXPLANATIONS.keys()
    assert not missing, f"unexplained fields: {sorted(missing)}"


def test_every_status_code_on_the_surface_has_an_explanation() -> None:
    """Status cells get "意味 · 次にやること" tooltips — the emitted
    vocabulary must stay covered. Codes shared across vocabularies are
    resolved per domain ('unknown' alone is emitted by evidence, quality
    AND phase columns)."""
    emitted = {
        # batch import
        'staged', 'committed', 'reused', 'failed',
        'exact_duplicate', 'same_acquisition', 'new',
        'reuse_existing', 'import_as_new',
        # campaign cells (from _CELL_STATUS_LABELS)
        *_CELL_STATUS_LABELS.keys(),
        # placement + report state
        'current', 'stale', 'missing',
        # lifecycle disposition
        'active', 'corrected', 'misassigned',
        'excluded_from_normal_use', 'test_only', 'duplicate_import',
        # retake recommendation
        'RETAKE', 'NOT_NEEDED',
        # onboarding + checks
        'ready', 'action', 'manual', 'synthetic_fixture',
        'verified', 'not_applicable',
        # level compatibility + spatial diff
        'absolute_level_comparable', 'normalized_shape_comparable',
        'diagnostic_only',
        'moved', 'removed', 'added', 'changed',
        # check names
        'clipping', 'noise_snr', 'usable_frequency_band',
        'timing_reference', 'polarity', 'ir_window', 'repeatability',
    }
    domain_emitted = {
        'evidence': {'measured', 'derived', 'predicted', 'unverified', 'unknown'},
        'quality': {'usable', 'warning', 'invalid', 'unknown', 'synthetic_fixture'},
        'phase': {'valid', 'absent', 'unknown'},
        'capability': {'ALLOWED', 'BLOCKED', 'UNKNOWN'},
        'quality_decision': {'PASS', 'FAIL', 'UNKNOWN', 'NOT_EVALUATED'},
        'attach': {'mdat', 'calibration', 'notes', 'other'},
        'purpose': set(_VARIANT_PURPOSE_LABELS.keys()),
    }
    missing = {
        code for code in emitted if status_explanation(code) is None
    }
    assert not missing, f"status codes without explanation: {sorted(missing)}"
    for domain, codes in domain_emitted.items():
        missing = {
            code
            for code in codes
            if status_explanation(code, domain=domain) is None
        }
        assert not missing, f"{domain} codes without explanation: {sorted(missing)}"


def test_colliding_status_codes_resolve_per_domain() -> None:
    """Regression: the same raw code emitted by different columns must
    explain THAT column's meaning, not a neighbouring vocabulary's.
    Observed on the live UI: a 品質 cell emitting 'unknown' showed the
    phase text, and a 共通タイミング cell emitting 'UNKNOWN' showed the
    quality-decision text."""
    assert status_explanation('unknown', domain='quality') != status_explanation(
        'unknown', domain='phase'
    )
    assert '品質' in status_explanation('unknown', domain='quality')
    assert '位相' in status_explanation('unknown', domain='phase')
    assert '証拠' in status_explanation('unknown', domain='evidence')
    capability = status_explanation('UNKNOWN', domain='capability')
    assert capability and '品質レポート' not in capability
    assert status_explanation('calibration', domain='attach') != status_explanation(
        'calibration', domain='purpose'
    )


def test_metric_names_have_explanations() -> None:
    for name in ('RMS差', '形状RMS', '平均差', '有効点', '実帯域'):
        assert metric_explanation(name), name
    assert metric_explanation('存在しない指標') is None


# ---------------------------------------------------------------------------
# Widget wiring (real widgets, offscreen)
# ---------------------------------------------------------------------------


def test_workspace_fields_carry_tooltips(tmp_path: Path) -> None:
    app = _app()
    _, workspace = _workspace(tmp_path)
    try:
        for widget in (
            workspace.evidence_combo,
            workspace.target_combo,
            workspace.mic_orientation_combo,
            workspace.avr_volume_edit,
            workspace.campaign_repeat,
            workspace.disposition_combo,
            workspace.compare_low,
            workspace.ref_band_check,
        ):
            assert widget.toolTip(), widget
            assert widget.whatsThis() == widget.toolTip(), widget
        assert 'Hz' in workspace.compare_low.toolTip()
        assert '単位' in workspace.avr_volume_edit.toolTip()
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_workspace_table_headers_carry_tooltips(tmp_path: Path) -> None:
    app = _app()
    _, workspace = _workspace(tmp_path)
    try:
        for table, columns in (
            (workspace.batch_table, range(7)),
            (workspace.campaign_table, range(5)),
            (workspace.quality_table, range(10)),
            (workspace.comparison_metrics, range(2)),
            (workspace.comparison_history, range(7)),
            (workspace.excluded_table, range(2)),
            (workspace.onboarding_table, range(3)),
        ):
            for column in columns:
                item = table.horizontalHeaderItem(column)
                assert item is not None and item.toolTip(), (
                    table.objectName(),
                    column,
                )
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_explain_combo_items_uses_status_registry() -> None:
    app = _app()
    combo = QComboBox()
    combo.addItem('既存の測定を利用', 'reuse_existing')
    combo.addItem('別の測定として保存', 'import_as_new')
    try:
        assert explain_combo_items(combo) == 2
        from PySide6.QtCore import Qt

        assert combo.itemData(0, Qt.ItemDataRole.ToolTipRole)
    finally:
        combo.deleteLater()
        app.processEvents()


def test_apply_explanation_missing_key_is_noop() -> None:
    app = _app()
    widget = QLineEdit()
    try:
        assert apply_explanation(widget, 'no.such.key') is False
        assert not widget.toolTip()
    finally:
        widget.deleteLater()
        app.processEvents()


def test_explain_table_columns_missing_key_is_noop() -> None:
    app = _app()
    from PySide6.QtWidgets import QTableWidget

    table = QTableWidget(0, 2)
    table.setHorizontalHeaderLabels(['A', 'B'])
    try:
        applied = explain_table_columns(
            table, {0: 'no.such.key', 1: 'import.batch_table.file'}
        )
        assert applied == 1
        assert table.horizontalHeaderItem(1).toolTip()
        assert not table.horizontalHeaderItem(0).toolTip()
    finally:
        table.deleteLater()
        app.processEvents()


# ---------------------------------------------------------------------------
# Glossary surface
# ---------------------------------------------------------------------------


def test_glossary_dialog_lists_registered_terms() -> None:
    app = _app()
    registry = build_help_registry()
    dialog = GlossaryDialog(
        registry, locale=PresentationLocale.JAPANESE
    )
    try:
        assert len(dialog._entry_widgets) == len(registry.glossary())
        # Registry-driven: every term renders its preferred JA label.
        names = [
            child.text()
            for child in dialog.findChildren(QLabel)
            if child.text() and len(child.text()) < 30
        ]
        assert any('実測' in name for name in names)
        # Filter narrows to matching cards.
        dialog._filter.setText('実測')
        visible = [
            card for _haystack, card in dialog._entry_widgets
            if not card.isHidden()
        ]
        assert 0 < len(visible) < len(dialog._entry_widgets)
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_palette_offers_glossary_destination() -> None:
    ids = {d.destination_id for d in help_destinations()}
    assert 'help.glossary' in ids


# ---------------------------------------------------------------------------
# Error comprehension + help buttons
# ---------------------------------------------------------------------------


def test_operation_error_topic_binds_error_codes() -> None:
    registry = build_help_registry()
    topic = registry.get('trouble.operation_error')
    assert topic is not None
    assert PresentationLocale.JAPANESE in topic.content
    assert PresentationLocale.ENGLISH in topic.content
    for code in (
        'storage.locked',
        'rew.unavailable',
        'io.no_space',
        'data.validation',
        'operation.rejected',
        'authority.conflict',
    ):
        bound = registry.topic_for_reason(code)
        assert bound is not None and bound.topic_id == (
            'trouble.operation_error'
        ), code


def test_help_registry_still_validates() -> None:
    registry = build_help_registry()
    report = registry.validate()
    assert not report.errors, report.errors


class _FakeBoxButton:
    def __init__(self, role) -> None:
        self.role = role


class _FakeMessageBox:
    """QMessageBox stand-in: scripted clicks, no Qt event machinery.

    warn_user imports QMessageBox lazily inside the function, so patching
    PySide6.QtWidgets.QMessageBox with this class intercepts construction.
    ``script`` holds the ButtonRole "clicked" by each successive exec().
    """

    Icon = QMessageBox.Icon
    StandardButton = QMessageBox.StandardButton
    ButtonRole = QMessageBox.ButtonRole
    script: list = []

    def __init__(self, parent=None) -> None:
        self._buttons: list[_FakeBoxButton] = []
        self._clicked = None

    def setIcon(self, icon) -> None:  # noqa: ARG002
        pass

    def setWindowTitle(self, title) -> None:
        self.window_title = title

    def setText(self, text) -> None:
        self.body_text = text

    def setDetailedText(self, detail) -> None:
        self.detail = detail

    def addButton(self, *args):
        if len(args) == 1:  # StandardButton form
            role = (
                QMessageBox.ButtonRole.AcceptRole
                if args[0] == QMessageBox.StandardButton.Ok
                else QMessageBox.ButtonRole.RejectRole
            )
        else:
            role = args[1]
        button = _FakeBoxButton(role)
        self._buttons.append(button)
        return button

    def setDefaultButton(self, standard) -> None:  # noqa: ARG002
        pass

    def buttons(self):
        return self._buttons

    def exec(self) -> int:
        role = _FakeMessageBox.script.pop(0)
        self._clicked = next(
            (b for b in self._buttons if b.role == role), None
        )
        return 0

    def clickedButton(self):
        return self._clicked


def test_warn_user_help_button_opens_bound_topic(monkeypatch) -> None:
    _app()
    opened: list[str] = []
    _FakeMessageBox.script = [
        QMessageBox.ButtonRole.HelpRole,   # operator reads the topic…
        QMessageBox.ButtonRole.AcceptRole,  # …then dismisses with OK
    ]
    monkeypatch.setattr(
        'PySide6.QtWidgets.QMessageBox', _FakeMessageBox
    )
    error = warn_user(
        None,
        '保存できませんでした',
        OSError('io failure'),
        on_help=opened.append,
    )
    assert error.code == 'io.error'
    assert opened == ['io.error']
    assert not _FakeMessageBox.script  # help re-showed the dialog once


def test_error_notice_carries_help_action(tmp_path: Path) -> None:
    app = _app()
    registry = build_help_registry()
    _, workspace = _workspace(tmp_path, help_registry=registry)
    try:
        workspace._operation_error_notice(
            '保存できませんでした', OSError('io failure')
        )
        # setVisible(True) was applied; the hidden workspace parent keeps
        # isVisible() false, so assert the explicit hidden flag instead.
        assert not workspace.notice_action.isHidden()
        assert workspace.notice_action.text() == '意味と対処'
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_retryable_job_error_shows_warn_user_with_help(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: to_user_facing_error requires title= — the retryable
    branch used to TypeError before warn_user (and its ヘルプ button)
    ever ran, so no path reached the error-help button."""
    app = _app()
    registry = build_help_registry()
    _, workspace = _workspace(tmp_path, help_registry=registry)
    try:
        import htdt.measurement_page_workspace as mpw

        calls: list[dict] = []
        monkeypatch.setattr(
            mpw, 'warn_user', lambda *args, **kwargs: calls.append(kwargs)
        )
        retried: list[str] = []
        workspace._job_handlers['job-1'] = (
            lambda result: None,
            '一覧の更新に失敗しました',
            lambda: retried.append('retry'),
        )
        # io.error is retryable; before the fix this raised TypeError
        # inside the branch condition and warn_user never ran.
        workspace._job_completed('job-1', None, OSError('disk error'))
        assert len(calls) == 1
        assert calls[0].get('on_help') is not None
        assert '一覧の更新に失敗しました' in workspace.notice.text()
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_reason_dialog_offers_help_button() -> None:
    app = _app()
    hits: list[str] = []
    dialog = ReasonTextDialog(
        None,
        '割り当ての訂正',
        '訂正の理由を記録してください:',
        on_help=lambda: hits.append('help'),
    )
    try:
        from PySide6.QtWidgets import QPushButton

        buttons = dialog.findChildren(QPushButton)
        labels = {button.text() for button in buttons}
        assert 'ヘルプ' in labels
        help_button = next(b for b in buttons if b.text() == 'ヘルプ')
        help_button.click()
        assert hits == ['help']
        assert not dialog.result()  # help click keeps the dialog open
        dialog.line_edit.setText('理由')
        assert dialog.text() == '理由'
    finally:
        dialog.deleteLater()
        app.processEvents()
