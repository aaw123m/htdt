from __future__ import annotations

from collections.abc import Callable, Iterable

from PySide6.QtCore import QObject, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetrics, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QStyle,
    QStyledItemDelegate,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .command_registry import (
    CommandContext,
    CommandRegistry,
    command_shortcut_allowed,
)
from .palette_search import (
    CommandPaletteProvider,
    PaletteResult,
    PaletteResultKind,
    PaletteSearchService,
)
from .workflow_navigation import WorkspaceDeepLink


def is_text_input_widget(widget: QWidget | None) -> bool:
    if widget is None:
        return False
    if isinstance(widget, (QLineEdit, QAbstractSpinBox, QTextEdit, QPlainTextEdit)):
        return True
    if isinstance(widget, QComboBox):
        return widget.isEditable()
    return False


def focused_text_editor(root: QWidget) -> QWidget | None:
    """Return the focused text/numeric editor contained by ``root``, if any."""

    focused = QApplication.focusWidget()
    if (
        focused is None
        or not root.isAncestorOf(focused)
        or not is_text_input_widget(focused)
    ):
        return None
    return focused


def flush_focused_text_editor(root: QWidget) -> QWidget | None:
    """Synchronously commit the focused editor under ``root`` via focus transfer.

    GLOBAL shortcuts such as Ctrl+S fire while a QLineEdit/QAbstractSpinBox still
    owns focus — before ``editingFinished`` can deliver the pending value to the
    document authority. Clearing focus runs the widget's validation and emits
    ``editingFinished`` synchronously, so the existing commit handler (and its
    revert behavior for rejected values) applies before the command executes.
    Focus is restored afterwards so the user can keep editing. Returns the
    flushed editor, or None when no text input owned by ``root`` was focused.
    """

    focused = focused_text_editor(root)
    if focused is None:
        return None
    focused.clearFocus()
    if focused.isEnabled():
        focused.setFocus(Qt.FocusReason.OtherFocusReason)
    return focused


class CommandShortcutBinder(QObject):
    """Bind registry shortcuts without stealing scene/document keys from text editors."""

    def __init__(
        self,
        window: QWidget,
        registry: CommandRegistry,
        *,
        command_ids: Iterable[str],
        shortcut_context: Qt.ShortcutContext = Qt.ShortcutContext.WindowShortcut,
    ) -> None:
        super().__init__(window)
        self._window = window
        self._registry = registry
        self._shortcuts: list[tuple[str, QShortcut]] = []
        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._focus_changed)

        for command_id in command_ids:
            definition = registry.definition(command_id)
            sequences = tuple(
                sequence
                for sequence in (definition.shortcut, *definition.shortcut_aliases)
                if sequence
            )
            for sequence in sequences:
                shortcut = QShortcut(QKeySequence(sequence), window)
                shortcut.setContext(shortcut_context)
                shortcut.activated.connect(
                    lambda command_id=command_id: self._registry.execute(command_id)
                )
                self._shortcuts.append((command_id, shortcut))
        self.refresh()

    def refresh(self) -> None:
        focus = QApplication.focusWidget()
        text_input_focused = is_text_input_widget(focus)
        for command_id, shortcut in self._shortcuts:
            definition = self._registry.definition(command_id)
            focus_allows = command_shortcut_allowed(
                definition,
                text_input_focused=text_input_focused,
            )
            # Availability providers can change after any scene edit. Keeping a
            # QShortcut disabled based on a stale snapshot would make Save/Undo/M/R
            # remain inaccessible until focus changes. Focus ownership is the only
            # property that must be enforced at the QShortcut level; execute() checks
            # live registry availability on every activation.
            shortcut.setEnabled(focus_allows)

    def _focus_changed(self, _old: QWidget | None, _new: QWidget | None) -> None:
        self.refresh()


_RESULT_ROLE = Qt.ItemDataRole.UserRole + 1

_KIND_GLYPHS = {
    PaletteResultKind.NAVIGATION: '→',
    PaletteResultKind.ACTION: '▶',
    PaletteResultKind.ENTITY: '◈',
    PaletteResultKind.DATA: '◆',
    PaletteResultKind.SETTINGS: '⚙',
    PaletteResultKind.HELP: '?',
}


class PaletteResultDelegate(QStyledItemDelegate):
    """Structured row: kind icon, title, subtitle and a right shortcut badge."""

    ROW_HEIGHT = 52

    def sizeHint(self, option, index):  # noqa: N802
        result: PaletteResult | None = index.data(_RESULT_ROLE)
        if result is None:
            return QSize(option.rect.width(), 22)
        return QSize(option.rect.width(), self.ROW_HEIGHT)

    def paint(self, painter, option, index) -> None:
        result: PaletteResult | None = index.data(_RESULT_ROLE)
        if result is None:
            super().paint(painter, option, index)
            return
        painter.save()
        palette = option.palette
        base = palette.color(QPalette.ColorRole.Base)
        text_color = palette.color(QPalette.ColorRole.Text)
        dim = palette.color(QPalette.ColorRole.PlaceholderText)
        if not result.available:
            text_color = dim
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, palette.color(QPalette.ColorRole.Highlight))
            text_color = palette.color(QPalette.ColorRole.HighlightedText)
            dim = text_color

        rect = option.rect
        icon_rect = QRect(rect.left() + 10, rect.top() + 13, 26, 26)
        painter.setPen(QColor(base).darker(140) if base.lightness() > 128 else dim)
        painter.drawText(
            icon_rect,
            Qt.AlignmentFlag.AlignCenter,
            _KIND_GLYPHS.get(result.kind, '•'),
        )

        metrics = QFontMetrics(option.font)
        right = rect.right() - 12
        if result.shortcut:
            badge_width = metrics.horizontalAdvance(result.shortcut) + 16
            badge = QRect(right - badge_width, rect.top() + 14, badge_width, 24)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(dim).lighter(160) if dim.lightness() < 160 else QColor(dim).darker(140))
            painter.setOpacity(0.25)
            painter.drawRoundedRect(badge, 4, 4)
            painter.setOpacity(1.0)
            painter.setPen(text_color)
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, result.shortcut)
            right = badge.left() - 8

        text_left = icon_rect.right() + 10
        painter.setPen(text_color)
        title_rect = QRect(text_left, rect.top() + 7, right - text_left, 20)
        painter.drawText(
            title_rect,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            metrics.elidedText(result.title, Qt.TextElideMode.ElideRight, title_rect.width()),
        )
        detail = result.disabled_reason or result.subtitle
        if detail:
            small_font = painter.font()
            small_font.setPointSizeF(max(small_font.pointSizeF() - 1.0, 7.0))
            painter.setFont(small_font)
            painter.setPen(dim)
            detail_rect = QRect(text_left, rect.top() + 27, right - text_left, 18)
            painter.drawText(
                detail_rect,
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                metrics.elidedText(detail, Qt.TextElideMode.ElideRight, detail_rect.width()),
            )
        painter.restore()


class CommandPalette(QDialog):
    """Ctrl+K palette over the composable PaletteSearchService."""

    def __init__(
        self,
        service: PaletteSearchService | CommandRegistry,
        *,
        context_provider: Callable[[], CommandContext | None] | None = None,
        on_deep_link: Callable[[WorkspaceDeepLink], bool] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if isinstance(service, PaletteSearchService):
            self.service = service
        else:
            self.service = PaletteSearchService(
                (CommandPaletteProvider(service),),
                on_deep_link=on_deep_link,
            )
        self.context_provider = context_provider or (lambda: None)
        self.setWindowTitle('コマンド検索')
        self.setModal(False)
        self.resize(640, 460)

        layout = QVBoxLayout(self)
        self._restore_focus_to: QWidget | None = None
        self.search_field = QLineEdit(self)
        self.search_field.setAccessibleName('コマンド検索')
        self.search_field.setPlaceholderText('機能・項目・設定・ヘルプを検索…')
        self.search_field.setClearButtonEnabled(True)
        self.search_field.textChanged.connect(self.refresh_results)
        self.search_field.returnPressed.connect(self.activate_current)
        layout.addWidget(self.search_field)

        self.results_list = QListWidget(self)
        self.results_list.setUniformItemSizes(False)
        self.results_list.setItemDelegate(PaletteResultDelegate(self.results_list))
        self.results_list.currentItemChanged.connect(self._selection_changed)
        self.results_list.itemActivated.connect(self._activate_item)
        layout.addWidget(self.results_list, 1)

        self.detail_label = QLabel(self)
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)

        # Focus-safe close: Esc clears the query first, then dismisses.
        self._escape = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._escape.activated.connect(self._escape_pressed)

        self.refresh_results('')

    def prepare_to_show(self) -> None:
        # Focus goes to the search field while the palette is up; remember
        # its owner so hideEvent can hand it back (#731 focus restore).
        self._restore_focus_to = QApplication.focusWidget()
        self.search_field.clear()
        self.refresh_results('')
        self.search_field.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search_field.selectAll()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        target = self._restore_focus_to
        self._restore_focus_to = None
        if target is None:
            return
        # Qt moves focus away from the hiding dialog after hideEvent, so a
        # synchronous restore is immediately overwritten. Deferring past the
        # event-loop turn lets Qt settle first, then hands focus back.
        def restore() -> None:
            try:
                # isVisible covers workspace switches: a pre-palette owner
                # in a now-hidden mount is skipped so navigation keeps
                # focus control.
                if target.isEnabled() and target.isVisible():
                    # The palette was a top-level window: when it hides, the
                    # platform may leave no active window, so setFocus alone
                    # cannot take effect. Reactivate the target's own window
                    # first (no-op when the shell already holds activation).
                    window = target.window()
                    if window is not None and not window.isActiveWindow():
                        window.activateWindow()
                    target.setFocus(Qt.FocusReason.OtherFocusReason)
            except RuntimeError:
                pass

        QTimer.singleShot(0, restore)

    def _escape_pressed(self) -> None:
        if self.search_field.text():
            self.search_field.clear()
        else:
            self.hide()

    def refresh_results(self, query: str) -> None:
        context = self.context_provider()
        if query.strip():
            results = self.service.search(query, context=context)
            grouped: list[PaletteResult] = list(results)
        else:
            grouped = list(self.service.suggested(context=context))

        self.results_list.clear()
        last_group: str | None = None
        for result in grouped:
            group = result.group
            if group is not None and group != last_group:
                header = QListWidgetItem(group)
                header.setFlags(Qt.ItemFlag.NoItemFlags)
                header.setForeground(
                    self.palette().color(QPalette.ColorRole.PlaceholderText)
                )
                self.results_list.addItem(header)
                last_group = group
            elif group is None and last_group is not None and query.strip():
                last_group = None
            self.results_list.addItem(self._item_for_result(result))

        if self.results_list.count() == 0:
            item = QListWidgetItem('該当する項目がありません')
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            item.setForeground(
                self.palette().color(QPalette.ColorRole.PlaceholderText)
            )
            self.results_list.addItem(item)
            self.detail_label.setText('')
            return

        for row in range(self.results_list.count()):
            if self.results_list.item(row).flags() & Qt.ItemFlag.ItemIsSelectable:
                self.results_list.setCurrentRow(row)
                break
        self._selection_changed(self.results_list.currentItem(), None)

    def _item_for_result(self, result: PaletteResult) -> QListWidgetItem:
        item = QListWidgetItem()
        item.setData(_RESULT_ROLE, result)
        item.setData(
            Qt.ItemDataRole.UserRole,
            result.command_id if result.command_id is not None else result.result_id,
        )
        # The delegate paints the structured row; text() still carries the
        # full content for tests, accessibility, and disabled-reason display.
        parts = [result.title]
        if result.subtitle:
            parts.append(result.subtitle)
        if not result.available and result.disabled_reason:
            parts.append(result.disabled_reason)
        item.setText(" — ".join(parts))
        if not result.available:
            item.setForeground(
                self.palette().color(QPalette.ColorRole.PlaceholderText)
            )
        return item

    def _selection_changed(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        if current is None:
            self.detail_label.setText('')
            return
        result: PaletteResult | None = current.data(_RESULT_ROLE)
        if result is None:
            self.detail_label.setText('')
            return
        detail_parts = [part for part in (result.subtitle,) if part]
        if not result.available and result.disabled_reason:
            detail_parts.insert(0, result.disabled_reason)
        self.detail_label.setText(' · '.join(detail_parts))

    def activate_current(self) -> None:
        self._activate_item(self.results_list.currentItem())

    def _activate_item(self, item: QListWidgetItem | None) -> None:
        if item is None:
            return
        result: PaletteResult | None = item.data(_RESULT_ROLE)
        if result is None:
            return
        if not result.available:
            self.detail_label.setText(
                result.disabled_reason or '現在は実行できません'
            )
            return
        if self.service.activate(result):
            self.hide()
            return
        self.detail_label.setText('現在は実行できません')


class CommandPaletteController(QObject):
    """Own the Ctrl+K entry point; shell/workspace code only supplies context."""

    def __init__(
        self,
        window: QWidget,
        service: PaletteSearchService | CommandRegistry,
        *,
        context_provider: Callable[[], CommandContext | None] | None = None,
        on_deep_link: Callable[[WorkspaceDeepLink], bool] | None = None,
    ) -> None:
        super().__init__(window)
        self.palette = CommandPalette(
            service,
            context_provider=context_provider,
            on_deep_link=on_deep_link,
            parent=window,
        )
        self.open_shortcut = QShortcut(QKeySequence('Ctrl+K'), window)
        self.open_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.open_shortcut.activated.connect(self.open)

    def open(self) -> None:
        self.palette.prepare_to_show()
        self.palette.show()
        self.palette.raise_()
        self.palette.activateWindow()
