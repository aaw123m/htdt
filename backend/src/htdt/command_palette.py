from __future__ import annotations

from collections.abc import Callable, Iterable

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QKeySequence, QPalette, QShortcut
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
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .command_registry import (
    CommandContext,
    CommandRegistry,
    CommandSearchResult,
    command_shortcut_allowed,
)


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


class CommandPalette(QDialog):
    def __init__(
        self,
        registry: CommandRegistry,
        *,
        context_provider: Callable[[], CommandContext | None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.registry = registry
        self.context_provider = context_provider or (lambda: None)
        self.setWindowTitle('コマンド検索')
        self.setModal(False)
        self.resize(620, 430)

        layout = QVBoxLayout(self)
        self.search_field = QLineEdit(self)
        self.search_field.setPlaceholderText('機能や操作を検索…')
        self.search_field.setClearButtonEnabled(True)
        self.search_field.textChanged.connect(self.refresh_results)
        self.search_field.returnPressed.connect(self.activate_current)
        layout.addWidget(self.search_field)

        self.results_list = QListWidget(self)
        self.results_list.setUniformItemSizes(False)
        self.results_list.currentItemChanged.connect(self._selection_changed)
        self.results_list.itemActivated.connect(self._activate_item)
        layout.addWidget(self.results_list, 1)

        self.detail_label = QLabel(self)
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)

        self.refresh_results('')

    def prepare_to_show(self) -> None:
        self.search_field.clear()
        self.refresh_results('')
        self.search_field.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search_field.selectAll()

    def refresh_results(self, query: str) -> None:
        context = self.context_provider()
        results = self.registry.search(query, context=context)
        self.results_list.clear()
        for result in results:
            self.results_list.addItem(self._item_for_result(result))

        if self.results_list.count() == 0:
            item = QListWidgetItem('該当するコマンドがありません')
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            item.setForeground(
                self.palette().color(QPalette.ColorRole.PlaceholderText)
            )
            self.results_list.addItem(item)
            self.detail_label.setText('')
            return

        self.results_list.setCurrentRow(0)
        self._selection_changed(self.results_list.currentItem(), None)

    def _item_for_result(self, result: CommandSearchResult) -> QListWidgetItem:
        definition = result.definition
        suffix = '' if definition.shortcut is None else f'    {definition.shortcut}'
        text = f'{definition.display_name}{suffix}'
        if not result.availability.enabled:
            text += f'\n利用不可 · {result.availability.disabled_reason}'
        item = QListWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, definition.command_id)
        if not result.availability.enabled:
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
        command_id = current.data(Qt.ItemDataRole.UserRole)
        if not isinstance(command_id, str):
            self.detail_label.setText('')
            return
        definition = self.registry.definition(command_id)
        availability = self.registry.availability(command_id)
        context_labels = {
            CommandContext.GLOBAL: '共通',
            CommandContext.OVERVIEW: '概要',
            CommandContext.ROOM: '部屋',
            CommandContext.MEASUREMENT: '測定',
            CommandContext.OPTIMIZATION: '最適化',
        }
        contexts = ' / '.join(context_labels[context] for context in definition.contexts)
        if availability.enabled:
            self.detail_label.setText(f'利用場所 · {contexts}')
        else:
            self.detail_label.setText(
                f'{availability.disabled_reason} · 利用場所 · {contexts}'
            )

    def activate_current(self) -> None:
        self._activate_item(self.results_list.currentItem())

    def _activate_item(self, item: QListWidgetItem | None) -> None:
        if item is None:
            return
        command_id = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(command_id, str):
            return
        if self.registry.execute(command_id):
            self.hide()
            return
        availability = self.registry.availability(command_id)
        self.detail_label.setText(availability.disabled_reason or '現在は実行できません')


class CommandPaletteController(QObject):
    """Own the Ctrl+K entry point; shell/workspace code only supplies registry context."""

    def __init__(
        self,
        window: QWidget,
        registry: CommandRegistry,
        *,
        context_provider: Callable[[], CommandContext | None] | None = None,
    ) -> None:
        super().__init__(window)
        self.registry = registry
        self.palette = CommandPalette(
            registry,
            context_provider=context_provider,
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
