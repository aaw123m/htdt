from __future__ import annotations

from PySide6.QtCore import QSize, QTimer, Qt
from PySide6.QtWidgets import QAbstractScrollArea, QDockWidget, QScrollArea, QSizePolicy, QWidget

from .cad_repository import SceneRepository
from .cad_scene import F1_DOCUMENT_ID
from .measurement_editor import MeasurementEditorWindow


class _MeasurementScrollArea(QScrollArea):
    """Scrollable form whose large content extent never becomes a dock minimum."""

    def __init__(self, content: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._content = content
        self.setWidgetResizable(False)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(0, 0)

        content.setMinimumSize(0, 0)
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setWidget(content)
        QTimer.singleShot(0, self._sync_content_extent)

    def sizeHint(self) -> QSize:
        # The content is deliberately taller than the viewport. Returning its size
        # hint here would make QMainWindow allocate the whole form instead of letting
        # this scroll area do its job.
        return QSize(420, 560)

    def minimumSizeHint(self) -> QSize:
        return QSize(240, 160)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._sync_content_extent()

    def _sync_content_extent(self) -> None:
        content = getattr(self, '_content', None)
        if content is None:
            return
        natural_height = max(
            content.sizeHint().height(),
            content.minimumSizeHint().height(),
            self.viewport().height(),
        )
        content.resize(max(1, self.viewport().width()), max(1, natural_height))


class MeasurementWorkspaceWindow(MeasurementEditorWindow):
    """N60 product composition with a DPI-safe scrollable measurement dock."""

    def __init__(self, repository: SceneRepository, document_id: str = F1_DOCUMENT_ID) -> None:
        self.measurement_scroll: QScrollArea | None = None
        super().__init__(repository, document_id)
        self._fit_initial_size_to_screen()
        QTimer.singleShot(0, self._fit_initial_size_to_screen)

    def _fit_initial_size_to_screen(self) -> None:
        """Keep the first product window inside the usable logical screen area."""
        screen = self.screen()
        if screen is None:
            return
        available = screen.availableGeometry()
        margin = 40
        target_width = min(self.width(), max(1, available.width() - margin))
        target_height = min(self.height(), max(1, available.height() - margin))
        self.resize(target_width, target_height)

    def _create_measurement_dock(self) -> None:
        super()._create_measurement_dock()
        dock = next(
            (candidate for candidate in self.findChildren(QDockWidget) if candidate.windowTitle() == '実測'),
            None,
        )
        if dock is None:
            return
        panel = dock.widget()
        if panel is None or isinstance(panel, QScrollArea):
            self.measurement_scroll = panel if isinstance(panel, QScrollArea) else None
            self._unify_right_context_docks(dock)
            return

        # Keep the natural full-form height as the scrollable child extent without
        # publishing that height as the QScrollArea/QDockWidget minimum.
        panel.setParent(None)
        scroll = _MeasurementScrollArea(panel, dock)
        dock.setMinimumSize(0, 0)
        dock.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        dock.setWidget(scroll)
        self.measurement_scroll = scroll
        self._unify_right_context_docks(dock)

    def _unify_right_context_docks(self, active_dock: QDockWidget) -> None:
        """Rebuild every right-side context surface into one CAD-style tab stack."""
        preferred_titles = (
            'Inspector',
            'Room',
            '壁・開口',
            'オブジェクト詳細',
            '制約',
            '実測',
            '予測',
            '最適化',
        )
        right_docks = [
            candidate
            for candidate in self.findChildren(QDockWidget)
            if self.dockWidgetArea(candidate) == Qt.DockWidgetArea.RightDockWidgetArea
        ]
        if active_dock not in right_docks:
            right_docks.append(active_dock)
        by_title = {candidate.windowTitle(): candidate for candidate in right_docks}
        ordered = [by_title[title] for title in preferred_titles if title in by_title]
        ordered.extend(candidate for candidate in right_docks if candidate not in ordered)
        if not ordered:
            return

        # The inherited editors create context surfaces at different construction
        # stages. Leaving any right-area dock outside the final tab group can preserve
        # a vertical split row and push a lower panel below a 200% DPI desktop.
        for candidate in ordered:
            candidate.setMinimumSize(0, 0)
            self.removeDockWidget(candidate)
        for candidate in ordered:
            candidate.setFloating(False)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, candidate)
            candidate.show()

        anchor = by_title.get('Inspector', ordered[0])
        for candidate in ordered:
            if candidate is not anchor:
                self.tabifyDockWidget(anchor, candidate)
        active_dock.raise_()
