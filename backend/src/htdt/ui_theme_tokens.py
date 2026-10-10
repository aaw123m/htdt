"""Theme tokens - Qt-free presentation data (#807 boundary refactor).

Token dataclasses, semantic enums and the ``DARK_THEME`` token tree are
presentation *data*, not Qt code: domain modules that only need severity
states or shared colors take them from here, while the Qt-side
palette/stylesheet/widget-role application stays in ``ui_theme`` (which
re-exports every public name - existing ``from .ui_theme import X`` paths
keep working).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

@dataclass(frozen=True, slots=True)
class ColorToken:
    hex: str

    def rgb01(self) -> tuple[float, float, float]:
        value = int(self.hex.lstrip('#'), 16)
        return (
            ((value >> 16) & 0xFF) / 255,
            ((value >> 8) & 0xFF) / 255,
            (value & 0xFF) / 255,
        )


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
    target: ColorToken
    scale: tuple[ColorToken, ...]
    channels: tuple[ColorToken, ...]


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
    categories: 'ViewportCategoryTokens'


@dataclass(frozen=True, slots=True)
class ViewportCategoryTokens:
    """Muted per-category fills for the semantic entity palette (#572).

    Values stay low-saturation and keep clear distance from the scientific
    trace palette (measured/predicted/cursor) so traces and status overlays
    never visually collide with entity categories.
    """

    architecture: ColorToken
    source: ColorToken
    listener: ColorToken
    display: ColorToken
    treatment: ColorToken
    infrastructure: ColorToken
    reference: ColorToken


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
    muted=ColorToken('#93A0AD'),
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
    error=ColorToken('#E58383'),
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
    target=ColorToken('#C9AE63'),
    scale=(
        ColorToken('#440154'),
        ColorToken('#31688E'),
        ColorToken('#35B779'),
        ColorToken('#FDE725'),
    ),
    # Bounded muted channel palette for multi-series legends (#579). Distinct
    # from the evidence semantics (measured/predicted) so channel identity and
    # evidence state never share a color.
    channels=(
        ColorToken('#7ED6FF'),
        ColorToken('#F2A6C0'),
        ColorToken('#9FD18B'),
        ColorToken('#E4C06B'),
        ColorToken('#8FB8C9'),
        ColorToken('#C9A8E8'),
        ColorToken('#D9A87E'),
        ColorToken('#8FC9B4'),
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
    categories=ViewportCategoryTokens(
        architecture=ColorToken('#7E8791'),
        source=ColorToken('#A8845C'),
        listener=ColorToken('#7D9B86'),
        display=ColorToken('#8494A8'),
        treatment=ColorToken('#9B88A0'),
        infrastructure=ColorToken('#8F857B'),
        reference=ColorToken('#A9975F'),
    ),
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

