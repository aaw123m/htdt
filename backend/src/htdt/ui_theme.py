from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication, QWidget



from .ui_theme_tokens import (
    ACCENT,
    DARK_THEME,
    INTERACTION,
    SCIENTIFIC,
    SEMANTIC,
    SURFACES,
    TEXT,
    VIEWPORT,
    AccentTokens,
    ColorToken,
    ControlHeightTokens,
    ControlSize,
    DarkThemeTokens,
    InteractionTokens,
    RadiusTokens,
    ScientificTokens,
    SemanticState,
    SemanticTokens,
    SpacingTokens,
    SurfaceRole,
    SurfaceTokens,
    TextTokens,
    TypographyRole,
    TypographyTokens,
    ViewportCategoryTokens,
    ViewportTokens,
)


def qcolor(token: ColorToken) -> QColor:
    """QColor for one theme token (Qt conversion lives on the UI side)."""
    return QColor(token.hex)


def build_dark_palette(tokens: DarkThemeTokens = DARK_THEME) -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, qcolor(tokens.surfaces.base))
    palette.setColor(QPalette.ColorRole.WindowText, qcolor(tokens.text.primary))
    palette.setColor(QPalette.ColorRole.Base, qcolor(tokens.surfaces.canvas))
    palette.setColor(QPalette.ColorRole.AlternateBase, qcolor(tokens.surfaces.raised))
    palette.setColor(QPalette.ColorRole.ToolTipBase, qcolor(tokens.surfaces.overlay))
    palette.setColor(QPalette.ColorRole.ToolTipText, qcolor(tokens.text.primary))
    palette.setColor(QPalette.ColorRole.Text, qcolor(tokens.text.primary))
    palette.setColor(QPalette.ColorRole.Button, qcolor(tokens.surfaces.raised))
    palette.setColor(QPalette.ColorRole.ButtonText, qcolor(tokens.text.primary))
    palette.setColor(QPalette.ColorRole.BrightText, qcolor(tokens.semantic.error))
    palette.setColor(QPalette.ColorRole.Link, qcolor(tokens.accent.primary))
    palette.setColor(QPalette.ColorRole.Highlight, qcolor(tokens.accent.selection_fill))
    palette.setColor(QPalette.ColorRole.HighlightedText, qcolor(tokens.text.primary))
    palette.setColor(QPalette.ColorRole.PlaceholderText, qcolor(tokens.text.muted))

    disabled = QPalette.ColorGroup.Disabled
    palette.setColor(disabled, QPalette.ColorRole.WindowText, qcolor(tokens.text.disabled))
    palette.setColor(disabled, QPalette.ColorRole.Text, qcolor(tokens.text.disabled))
    palette.setColor(disabled, QPalette.ColorRole.ButtonText, qcolor(tokens.text.disabled))
    palette.setColor(disabled, QPalette.ColorRole.Button, qcolor(tokens.interaction.disabled_surface))
    palette.setColor(disabled, QPalette.ColorRole.Highlight, qcolor(tokens.interaction.disabled_surface))
    palette.setColor(disabled, QPalette.ColorRole.HighlightedText, qcolor(tokens.text.disabled))
    return palette


def build_dark_stylesheet(tokens: DarkThemeTokens = DARK_THEME) -> str:
    s = tokens.surfaces
    t = tokens.text
    a = tokens.accent
    sem = tokens.semantic
    interaction = tokens.interaction
    spacing = tokens.spacing
    radius = tokens.radius
    controls = tokens.controls
    typography = tokens.typography

    return f"""
QMainWindow, QDialog {{
    background-color: {s.base.hex};
    color: {t.primary.hex};
}}
QWidget {{
    color: {t.primary.hex};
}}
QToolBar, QDockWidget, QMenuBar, QStatusBar {{
    background-color: {s.raised.hex};
    border: 0;
}}
QStatusBar {{
    border-top: 1px solid {s.separator.hex};
}}
QToolBar {{
    spacing: {spacing.xs}px;
    padding: {spacing.xxs}px {spacing.xs}px;
}}
QDockWidget::title {{
    background-color: {s.raised.hex};
    color: {t.secondary.hex};
    padding: {spacing.xs}px {spacing.sm}px;
}}
QMenu {{
    background-color: {s.overlay.hex};
    color: {t.primary.hex};
    border: 1px solid {s.separator.hex};
    padding: {spacing.xxs}px;
}}
QMenu::item {{
    padding: {spacing.xs}px {spacing.md}px;
    border-radius: {radius.small}px;
}}
QMenu::item:selected {{
    background-color: {interaction.hover_surface.hex};
}}
QMenu::item:disabled {{
    color: {t.disabled.hex};
}}
QMenu::separator {{
    height: 1px;
    background-color: {s.separator.hex};
    margin: {spacing.xxs}px {spacing.xs}px;
}}
QToolTip {{
    background-color: {s.overlay.hex};
    color: {t.primary.hex};
    border: 1px solid {s.border_strong.hex};
    padding: {spacing.xxs}px {spacing.xs}px;
}}

QPushButton, QToolButton {{
    min-height: {controls.standard}px;
    padding: 0 {spacing.sm}px;
    background-color: {s.raised.hex};
    color: {t.primary.hex};
    border: 1px solid transparent;
    border-radius: {radius.small}px;
}}
QPushButton:hover, QToolButton:hover {{
    background-color: {interaction.hover_surface.hex};
}}
QPushButton:pressed, QToolButton:pressed {{
    background-color: {interaction.pressed_surface.hex};
}}
QPushButton:focus, QToolButton:focus {{
    border: 1px solid {a.focus_ring.hex};
}}
QPushButton:checked, QToolButton:checked {{
    background-color: {a.selection_fill.hex};
    border: 1px solid {a.primary.hex};
}}
QPushButton[role="primary"] {{
    background-color: {a.primary.hex};
    color: {s.canvas.hex};
    font-weight: {typography.semibold_weight};
}}
QPushButton[role="primary"]:hover {{
    background-color: {a.hover.hex};
}}
QPushButton[role="primary"]:pressed {{
    background-color: {a.pressed.hex};
}}
QPushButton:disabled, QToolButton:disabled {{
    background-color: {interaction.disabled_surface.hex};
    color: {t.disabled.hex};
    /* keep the control silhouette visible — a transparent border on the
       near-identical disabled surface makes the button unreadable */
    border-color: {s.separator.hex};
}}

QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    min-height: {controls.standard}px;
    background-color: {s.canvas.hex};
    color: {t.primary.hex};
    border: 1px solid {s.separator.hex};
    border-radius: {radius.small}px;
    selection-background-color: {a.selection_fill.hex};
    selection-color: {t.primary.hex};
}}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    padding: 0 {spacing.xs}px;
}}
QLineEdit:hover, QPlainTextEdit:hover, QTextEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{
    border-color: {s.border_strong.hex};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {a.focus_ring.hex};
}}
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    background-color: {interaction.disabled_surface.hex};
    color: {t.disabled.hex};
    border-color: {s.separator.hex};
}}
/* In-progress (uncommitted) inspector edits — left accent tick (#583). */
QLineEdit[inspectorDirty="true"], QDoubleSpinBox[inspectorDirty="true"], QComboBox[inspectorDirty="true"] {{
    border-left: 3px solid {a.primary.hex};
}}

QAbstractItemView {{
    background-color: {s.canvas.hex};
    alternate-background-color: {s.base.hex};
    color: {t.primary.hex};
    border: 0;
    outline: 0;
    selection-background-color: {a.selection_fill.hex};
    selection-color: {t.primary.hex};
}}
QTreeView::item, QListView::item {{
    min-height: {controls.compact}px;
    padding: 0 {spacing.xs}px;
    border-radius: {radius.small}px;
}}
QTreeView::item:hover, QListView::item:hover {{
    background-color: {interaction.hover_surface.hex};
}}
/* Keyboard focus on items is otherwise invisible (QAbstractItemView sets
   outline:0 above, which suppresses the platform focus rect). */
QTreeView::item:focus, QListView::item:focus, QTableView::item:focus {{
    border: 1px solid {a.focus_ring.hex};
}}
QTreeView::item:selected, QListView::item:selected {{
    background-color: {a.selection_fill.hex};
    color: {t.primary.hex};
}}
QHeaderView::section {{
    background-color: {s.raised.hex};
    color: {t.secondary.hex};
    border: 0;
    border-bottom: 1px solid {s.separator.hex};
    padding: {spacing.xs}px;
}}
QTabWidget::pane {{
    border: 0;
}}
QTabBar::tab {{
    min-height: {controls.compact}px;
    padding: 0 {spacing.sm}px;
    color: {t.secondary.hex};
    background: transparent;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:hover {{
    color: {t.primary.hex};
    background-color: {interaction.hover_surface.hex};
}}
QTabBar::tab:selected {{
    color: {t.primary.hex};
    border-bottom-color: {a.primary.hex};
}}
QTabBar::tab:focus {{
    color: {t.primary.hex};
    border-bottom-color: {a.focus_ring.hex};
}}

QScrollBar:vertical, QScrollBar:horizontal {{
    background: transparent;
    border: 0;
}}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: {s.border_strong.hex};
    border-radius: {radius.small}px;
    min-height: {spacing.lg}px;
    min-width: {spacing.lg}px;
}}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {{
    background: {t.muted.hex};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0;
    height: 0;
}}

QScrollArea {{
    border: 0;
    background: transparent;
}}
QSplitter::handle {{
    background-color: {s.separator.hex};
    margin: {spacing.xs}px 0;
    border-radius: 1px;
}}
QSplitter::handle:hover {{
    background-color: {s.border_strong.hex};
}}
QCheckBox {{
    spacing: {spacing.xs}px;
    color: {t.secondary.hex};
}}
QCheckBox:focus {{
    border: 1px solid {a.focus_ring.hex};
    border-radius: {radius.small}px;
}}
QComboBox::drop-down {{
    border: 0;
    width: {controls.compact}px;
}}

QWidget[surfaceRole="canvas"] {{ background-color: {s.canvas.hex}; }}
QWidget[surfaceRole="base"] {{ background-color: {s.base.hex}; }}
QWidget[surfaceRole="raised"] {{ background-color: {s.raised.hex}; }}
QWidget[surfaceRole="overlay"] {{ background-color: {s.overlay.hex}; }}
QWidget[surfaceRole="modal"] {{ background-color: {s.modal.hex}; }}

QFrame[surfaceRole="raised"] {{
    border: 1px solid {s.separator.hex};
    border-radius: {radius.standard}px;
}}
QFrame#workflowRail {{
    border: 0;
    border-right: 1px solid {s.separator.hex};
    border-radius: 0;
}}
QFrame#workflowContextBar {{
    border: 0;
    border-bottom: 1px solid {s.separator.hex};
    border-radius: 0;
}}
QPushButton[workspaceId] {{
    text-align: left;
    padding-left: {spacing.sm}px;
}}
QPushButton[workspaceId]:checked {{
    background-color: {a.selection_fill.hex};
    border-color: {a.primary.hex};
}}
QPushButton#workflowSettingsButton {{
    text-align: left;
    padding-left: {spacing.sm}px;
}}

QWidget[semanticState="success"] {{ color: {sem.success.hex}; }}
QWidget[semanticState="warning"] {{ color: {sem.warning.hex}; }}
QWidget[semanticState="error"] {{ color: {sem.error.hex}; }}
QWidget[semanticState="stale"] {{ color: {sem.stale.hex}; }}
QWidget[semanticState="unsupported"] {{ color: {sem.unsupported.hex}; }}
QWidget[semanticState="selected"] {{
    color: {t.primary.hex};
    background-color: {a.selection_fill.hex};
    border: 1px solid {a.primary.hex};
}}

QLabel[typographyRole="workspaceTitle"] {{
    font-size: {typography.workspace_title_px}px;
    font-weight: {typography.semibold_weight};
}}
QLabel[typographyRole="sectionTitle"] {{
    font-size: {typography.section_title_px}px;
    font-weight: {typography.semibold_weight};
}}
QLabel[typographyRole="body"] {{
    font-size: {typography.body_px}px;
    font-weight: {typography.regular_weight};
}}
QLabel[typographyRole="secondary"] {{
    font-size: {typography.secondary_px}px;
    color: {t.secondary.hex};
}}
QLabel[typographyRole="numeric"] {{
    font-size: {typography.numeric_px}px;
    font-weight: {typography.semibold_weight};
}}

QPushButton[controlSize="compact"], QToolButton[controlSize="compact"],
QLineEdit[controlSize="compact"], QComboBox[controlSize="compact"],
QSpinBox[controlSize="compact"], QDoubleSpinBox[controlSize="compact"] {{
    min-height: {controls.compact}px;
}}
QPushButton[controlSize="standard"], QToolButton[controlSize="standard"],
QLineEdit[controlSize="standard"], QComboBox[controlSize="standard"],
QSpinBox[controlSize="standard"], QDoubleSpinBox[controlSize="standard"] {{
    min-height: {controls.standard}px;
}}
QPushButton[controlSize="prominent"], QToolButton[controlSize="prominent"],
QLineEdit[controlSize="prominent"], QComboBox[controlSize="prominent"] {{
    min-height: {controls.prominent}px;
    padding-left: {spacing.md}px;
    padding-right: {spacing.md}px;
}}
"""


def _platform_ui_font(app: QApplication) -> QFont:
    families = set(QFontDatabase.families())
    current = QFont(app.font())
    for family in ('Segoe UI Variable', 'Segoe UI'):
        if family in families:
            current.setFamily(family)
            break
    current.setPointSizeF(10.0)
    return current


def install_japanese_translations(app: QApplication) -> None:
    """Localize Qt's own chrome (stock dialog buttons, native file dialogs).

    The product's authored strings are Japanese literals, but stock widgets
    (QMessageBox/QInputDialog OK・Cancel, file dialog buttons) come from Qt's
    own translation catalogs — without a translator they render English.
    """
    translations_dir = QLibraryInfo.path(
        QLibraryInfo.LibraryPath.TranslationsPath
    )
    for catalog in ('qtbase', 'qt'):
        translator = QTranslator(app)
        if translator.load(
            QLocale(QLocale.Language.Japanese), catalog, '_', translations_dir
        ):
            app.installTranslator(translator)


def apply_dark_theme(app: QApplication, tokens: DarkThemeTokens = DARK_THEME) -> None:
    """Install the authoritative dark Qt palette/QSS at the application boundary."""
    install_japanese_translations(app)
    app.setFont(_platform_ui_font(app))
    app.setPalette(build_dark_palette(tokens))
    app.setStyleSheet(build_dark_stylesheet(tokens))
    app.setProperty('htdtTheme', 'dark')


def _set_dynamic_property(widget: QWidget, name: str, value: str | None) -> None:
    widget.setProperty(name, value)
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()


def set_surface_role(widget: QWidget, role: SurfaceRole) -> None:
    _set_dynamic_property(widget, 'surfaceRole', role.value)


def set_semantic_state(widget: QWidget, state: SemanticState | None) -> None:
    _set_dynamic_property(widget, 'semanticState', None if state is None else state.value)


def set_typography_role(widget: QWidget, role: TypographyRole) -> None:
    _set_dynamic_property(widget, 'typographyRole', role.value)


def set_control_size(widget: QWidget, size: ControlSize) -> None:
    _set_dynamic_property(widget, 'controlSize', size.value)


def set_primary_action(widget: QWidget, enabled: bool = True) -> None:
    _set_dynamic_property(widget, 'role', 'primary' if enabled else None)


__all__ = [
    'ACCENT',
    'DARK_THEME',
    'SCIENTIFIC',
    'SEMANTIC',
    'SURFACES',
    'TEXT',
    'VIEWPORT',
    'ColorToken',
    'qcolor',
    'ControlSize',
    'DarkThemeTokens',
    'SemanticState',
    'SurfaceRole',
    'TypographyRole',
    'apply_dark_theme',
    'build_dark_palette',
    'build_dark_stylesheet',
    'set_control_size',
    'set_primary_action',
    'set_semantic_state',
    'set_surface_role',
    'set_typography_role',
]
