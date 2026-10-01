from __future__ import annotations

import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtGui import QFont, QPalette
from PySide6.QtWidgets import QApplication

from htdt.ui_theme import (
    DARK_THEME,
    apply_dark_theme,
    build_dark_stylesheet,
)


def _wcag_contrast_ratio(foreground: str, background: str) -> float:
    """WCAG 2.x relative-luminance contrast ratio for two sRGB hex colors."""

    def channel(value: float) -> float:
        return (
            value / 12.92
            if value <= 0.03928
            else ((value + 0.055) / 1.055) ** 2.4
        )

    def luminance(hex_color: str) -> float:
        value = hex_color.lstrip('#')
        red, green, blue = (
            int(value[index:index + 2], 16) / 255 for index in (0, 2, 4)
        )
        return (
            0.2126 * channel(red)
            + 0.7152 * channel(green)
            + 0.0722 * channel(blue)
        )

    lighter, darker = sorted(
        (luminance(foreground), luminance(background)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


_CONTENT_SURFACES = ('canvas', 'base', 'raised', 'overlay', 'modal')


def test_meaning_bearing_tokens_meet_wcag_aa_contrast() -> None:
    """Text/semantic tokens carry meaning: ≥4.5:1 on every content surface.

    ``text.disabled`` is intentionally exempt (WCAG AA does not apply to
    disabled controls); separator/border surfaces are not text backgrounds.
    """
    surfaces = DARK_THEME.surfaces
    content_surfaces = tuple(
        getattr(surfaces, name) for name in _CONTENT_SURFACES
    )
    text = DARK_THEME.text
    semantic = DARK_THEME.semantic
    meaning_bearing = (
        text.primary,
        text.secondary,
        text.muted,
        semantic.success,
        semantic.warning,
        semantic.error,
        semantic.stale,
        semantic.unsupported,
    )
    for foreground in meaning_bearing:
        for surface in content_surfaces:
            assert _wcag_contrast_ratio(foreground.hex, surface.hex) >= 4.5, (
                f'{foreground.hex} on {surface.hex} = '
                f'{_wcag_contrast_ratio(foreground.hex, surface.hex):.2f}'
            )

    # Primary action: canvas text on the accent fills.
    accent = DARK_THEME.accent
    for fill in (accent.primary, accent.hover, accent.pressed):
        assert _wcag_contrast_ratio(surfaces.canvas.hex, fill.hex) >= 4.5
    # Selected items: primary text on the selection fill.
    assert (
        _wcag_contrast_ratio(text.primary.hex, accent.selection_fill.hex)
        >= 4.5
    )


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _rgb_sum(hex_color: str) -> int:
    value = hex_color.lstrip('#')
    return sum(int(value[index:index + 2], 16) for index in (0, 2, 4))


def test_dark_surface_hierarchy_is_semantic_and_not_pure_black() -> None:
    surfaces = DARK_THEME.surfaces
    ordered = (
        surfaces.canvas.hex,
        surfaces.base.hex,
        surfaces.raised.hex,
        surfaces.overlay.hex,
        surfaces.modal.hex,
    )

    assert '#000000' not in ordered
    assert [_rgb_sum(color) for color in ordered] == sorted(_rgb_sum(color) for color in ordered)
    assert len(set(ordered)) == len(ordered)


def test_ui_semantics_are_distinct_from_scientific_visualization_colors() -> None:
    ui_meanings = {
        DARK_THEME.accent.primary.hex,
        DARK_THEME.semantic.warning.hex,
        DARK_THEME.semantic.error.hex,
        DARK_THEME.semantic.stale.hex,
        DARK_THEME.accent.selection_fill.hex,
    }
    scientific = {
        DARK_THEME.scientific.measured.hex,
        DARK_THEME.scientific.predicted.hex,
        DARK_THEME.scientific.primary_trace.hex,
        DARK_THEME.scientific.secondary_trace.hex,
        *(token.hex for token in DARK_THEME.scientific.scale),
    }

    assert ui_meanings.isdisjoint(scientific)
    assert DARK_THEME.scientific.measured.hex != DARK_THEME.scientific.predicted.hex


def test_common_stylesheet_covers_required_interaction_states() -> None:
    stylesheet = build_dark_stylesheet()

    for selector in (':hover', ':pressed', ':focus', ':disabled', ':checked', '::item:selected'):
        assert selector in stylesheet
    for selector in (
        'QFrame[surfaceRole="raised"]',
        'QFrame#workflowRail',
        'QFrame#workflowContextBar',
        'QSplitter::handle',
    ):
        assert selector in stylesheet
    for semantic_state in ('warning', 'error', 'stale', 'selected', 'unsupported'):
        assert f'semanticState="{semantic_state}"' in stylesheet

    assert DARK_THEME.controls.compact < DARK_THEME.controls.standard < DARK_THEME.controls.prominent
    assert DARK_THEME.radius.small < DARK_THEME.radius.standard < DARK_THEME.radius.large


def test_apply_dark_theme_installs_palette_and_application_marker() -> None:
    app = _app()
    original_palette = QPalette(app.palette())
    original_font = QFont(app.font())
    original_stylesheet = app.styleSheet()
    original_marker = app.property('htdtTheme')

    try:
        apply_dark_theme(app)
        assert app.property('htdtTheme') == 'dark'
        assert (
            app.palette().color(QPalette.ColorRole.Window).name().upper()
            == DARK_THEME.surfaces.base.hex
        )
        assert (
            app.palette().color(QPalette.ColorRole.Highlight).name().upper()
            == DARK_THEME.accent.selection_fill.hex
        )
        assert app.styleSheet() == build_dark_stylesheet()
    finally:
        app.setPalette(original_palette)
        app.setFont(original_font)
        app.setStyleSheet(original_stylesheet)
        app.setProperty('htdtTheme', original_marker)
