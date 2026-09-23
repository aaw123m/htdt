from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication, QWidget


@dataclass(frozen=True, slots=True)
class ColorToken:
    hex: str

    def qcolor(self) -> QColor:
        return QColor(self.hex)

    def rgb01(self) -> tuple[float, float, float]:
        color = self.qcolor()
        return (color.redF(), color.greenF(), color.blueF())


@dataclass(frozen=True, slots=True)
class SurfaceTokens:
    canvas: ColorToken
    base: ColorToken
    raised: ColorToken
    overlay: ColorToken
    modal: ColorToken
    separator: ColorToken
    border_strong: ColorToken


@dataclass(frozen=True, slots=True)
class TextTokens:
    primary: ColorToken
    secondary: ColorToken
    muted: ColorToken
    disabled: ColorToken


@dataclass(frozen=True, slots=True)
class AccentTokens:
    primary: ColorToken
    hover: ColorToken
    pressed: ColorToken
    selection_fill: ColorToken
    focus_ring: ColorToken


@dataclass(frozen=True, slots=True)
class SemanticTokens:
    success: ColorToken
    warning: ColorToken
    error: ColorToken
    stale: ColorToken
    unsupported: ColorToken


@dataclass(frozen=True, slots=True)
class InteractionTokens:
    hover_surface: ColorToken
    pressed_surface: ColorToken
    disabled_surface: ColorToken


@dataclass(frozen=True, slots=True)
class ScientificTokens:
    measured: ColorToken
    predicted: ColorToken
    primary_trace: ColorToken
    secondary_trace: ColorToken
    grid: ColorToken
    cursor: ColorToken
    scale: tuple[ColorToken, ...]


@dataclass(frozen=True, slots=True)
class ViewportTokens:
    background: ColorToken
    floor: ColorToken
    grid_minor: ColorToken
    grid_major: ColorToken
    geometry: ColorToken
    geometry_edge: ColorToken
    selection_outline: ColorToken
    handle: ColorToken
    gizmo_x: ColorToken
    gizmo_y: ColorToken
    gizmo_z: ColorToken


@dataclass(frozen=True, slots=True)
class SpacingTokens:
    xxs: int = 4
    xs: int = 8
    sm: int = 12
    md: int = 16
    lg: int = 24
    xl: int = 32


@dataclass(frozen=True, slots=True)
class RadiusTokens:
    small: int = 6
    standard: int = 10
    large: int = 14


@dataclass(frozen=True, slots=True)
class ControlHeightTokens:
    compact: int = 30
    standard: int = 36
    prominent: int = 42


@dataclass(frozen=True, slots=True)
class TypographyTokens:
    workspace_title_px: int = 18
    section_title_px: int = 14
    body_px: int = 13
    secondary_px: int = 12
    numeric_px: int = 13
    regular_weight: int = 400
    semibold_weight: int = 600


@dataclass(frozen=True, slots=True)
class DarkThemeTokens:
    surfaces: SurfaceTokens
    text: TextTokens
    accent: AccentTokens
    semantic: SemanticTokens
    interaction: InteractionTokens
    scientific: ScientificTokens
    viewport: ViewportTokens
    spacing: SpacingTokens
    radius: RadiusTokens
    controls: ControlHeightTokens
    typography: TypographyTokens


class SurfaceRole(StrEnum):
    CANVAS = 'canvas'
    BASE = 'base'
    RAISED = 'raised'
    OVERLAY = 'overlay'
    MODAL = 'modal'


class SemanticState(StrEnum):
    SUCCESS = 'success'
    WARNING = 'warning'
    ERROR = 'error'
    STALE = 'stale'
    UNSUPPORTED = 'unsupported'
    SELECTED = 'selected'


class TypographyRole(StrEnum):
    WORKSPACE_TITLE = 'workspaceTitle'
    SECTION_TITLE = 'sectionTitle'
    BODY = 'body'
    SECONDARY = 'secondary'
    NUMERIC = 'numeric'


class ControlSize(StrEnum):
    COMPACT = 'compact'
    STANDARD = 'standard'
    PROMINENT = 'prominent'


SURFACES = SurfaceTokens(
    canvas=ColorToken('#0F141A'),
    base=ColorToken('#141A21'),
    raised=ColorToken('#1B232D'),
    overlay=ColorToken('#222C38'),
    modal=ColorToken('#293542'),
    separator=ColorToken('#2A3542'),
    border_strong=ColorToken('#3A4858'),
)

TEXT = TextTokens(
    primary=ColorToken('#F2F5F8'),
    secondary=ColorToken('#B5C0CC'),
    muted=ColorToken('#8793A0'),
    disabled=ColorToken('#5D6874'),
)

ACCENT = AccentTokens(
    primary=ColorToken('#6BA6FF'),
    hover=ColorToken('#83B5FF'),
    pressed=ColorToken('#4E8EEA'),
    selection_fill=ColorToken('#263D5C'),
    focus_ring=ColorToken('#6BA6FF'),
)

SEMANTIC = SemanticTokens(
    success=ColorToken('#68B98A'),
    warning=ColorToken('#E1B15A'),
    error=ColorToken('#E47B7B'),
    stale=ColorToken('#B09BC6'),
    unsupported=ColorToken('#98A2AD'),
)

INTERACTION = InteractionTokens(
    hover_surface=ColorToken('#25303C'),
    pressed_surface=ColorToken('#2B3744'),
    disabled_surface=ColorToken('#181E25'),
)

SCIENTIFIC = ScientificTokens(
    measured=ColorToken('#4CC5B1'),
    predicted=ColorToken('#BD9CF4'),
    primary_trace=ColorToken('#7ED6FF'),
    secondary_trace=ColorToken('#94A0AC'),
    grid=ColorToken('#2E3945'),
    cursor=ColorToken('#E9CE7A'),
    scale=(
        ColorToken('#440154'),
        ColorToken('#31688E'),
        ColorToken('#35B779'),
        ColorToken('#FDE725'),
    ),
)

VIEWPORT = ViewportTokens(
    background=SURFACES.canvas,
    floor=ColorToken('#171F27'),
    grid_minor=ColorToken('#202A34'),
    grid_major=ColorToken('#2D3945'),
    geometry=ColorToken('#7A8794'),
    geometry_edge=ColorToken('#A3ADB7'),
    selection_outline=ACCENT.primary,
    handle=ColorToken('#DDE8F7'),
    gizmo_x=ColorToken('#D66A6A'),
    gizmo_y=ColorToken('#6EBD78'),
    gizmo_z=ColorToken('#668FE0'),
)

DARK_THEME = DarkThemeTokens(
    surfaces=SURFACES,
    text=TEXT,
    accent=ACCENT,
    semantic=SEMANTIC,
    interaction=INTERACTION,
    scientific=SCIENTIFIC,
    viewport=VIEWPORT,
    spacing=SpacingTokens(),
    radius=RadiusTokens(),
    controls=ControlHeightTokens(),
    typography=TypographyTokens(),
)


def build_dark_palette(tokens: DarkThemeTokens = DARK_THEME) -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, tokens.surfaces.base.qcolor())
    palette.setColor(QPalette.ColorRole.WindowText, tokens.text.primary.qcolor())
    palette.setColor(QPalette.ColorRole.Base, tokens.surfaces.canvas.qcolor())
    palette.setColor(QPalette.ColorRole.AlternateBase, tokens.surfaces.raised.qcolor())
    palette.setColor(QPalette.ColorRole.ToolTipBase, tokens.surfaces.overlay.qcolor())
    palette.setColor(QPalette.ColorRole.ToolTipText, tokens.text.primary.qcolor())
    palette.setColor(QPalette.ColorRole.Text, tokens.text.primary.qcolor())
    palette.setColor(QPalette.ColorRole.Button, tokens.surfaces.raised.qcolor())
    palette.setColor(QPalette.ColorRole.ButtonText, tokens.text.primary.qcolor())
    palette.setColor(QPalette.ColorRole.BrightText, tokens.semantic.error.qcolor())
    palette.setColor(QPalette.ColorRole.Link, tokens.accent.primary.qcolor())
    palette.setColor(QPalette.ColorRole.Highlight, tokens.accent.selection_fill.qcolor())
    palette.setColor(QPalette.ColorRole.HighlightedText, tokens.text.primary.qcolor())
    palette.setColor(QPalette.ColorRole.PlaceholderText, tokens.text.muted.qcolor())

    disabled = QPalette.ColorGroup.Disabled
    palette.setColor(disabled, QPalette.ColorRole.WindowText, tokens.text.disabled.qcolor())
    palette.setColor(disabled, QPalette.ColorRole.Text, tokens.text.disabled.qcolor())
    palette.setColor(disabled, QPalette.ColorRole.ButtonText, tokens.text.disabled.qcolor())
    palette.setColor(disabled, QPalette.ColorRole.Button, tokens.interaction.disabled_surface.qcolor())
    palette.setColor(disabled, QPalette.ColorRole.Highlight, tokens.interaction.disabled_surface.qcolor())
    palette.setColor(disabled, QPalette.ColorRole.HighlightedText, tokens.text.disabled.qcolor())
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
    border-color: transparent;
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


def apply_dark_theme(app: QApplication, tokens: DarkThemeTokens = DARK_THEME) -> None:
    """Install the authoritative dark Qt palette/QSS at the application boundary."""
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
