"""#731: accessibility contract — announcements, focus, disabled reasons."""

from __future__ import annotations

import pytest

from htdt.native_accessibility import (
    DisabledControlReason,
    FocusSurface,
    REQUIRED_ANNOUNCEMENTS,
    StatusAnnouncement,
    requires_announcement,
)


def test_disabled_reason_is_human_readable() -> None:
    reason = DisabledControlReason(
        control_id='run_optimize',
        reason='No candidates are selected for optimization',
    )
    assert ' ' in reason.reason
    with pytest.raises(ValueError, match='human-readable'):
        DisabledControlReason(control_id='x', reason='NOCAND')
    with pytest.raises(ValueError, match='human-readable'):
        DisabledControlReason(control_id='x', reason='42')


def test_required_announcement_set() -> None:
    for event in (
        'import_completed',
        'operation_completed',
        'operation_failed',
        'retake_required',
        'result_stale',
        'save_state',
        'device_disconnected',
    ):
        assert requires_announcement(event)
    assert 'viewport_motion' not in REQUIRED_ANNOUNCEMENTS


def test_focus_surface_order() -> None:
    surface = FocusSurface(
        surface_id='measurements',
        order=('dataset_picker', 'quality_button', 'compare_button'),
    )
    assert surface.next_control('dataset_picker') == 'quality_button'
    assert surface.next_control('compare_button') is None
    assert surface.previous_control('compare_button') == 'quality_button'


def test_restore_focus_never_resets_to_top() -> None:
    surface = FocusSurface(
        surface_id='inspector',
        order=('name_field', 'x_field', 'y_field'),
    )
    assert surface.restore_focus('x_field') == 'x_field'
    # Only when the focused control disappeared does focus fall back.
    assert surface.restore_focus('deleted_field') == 'name_field'
    assert surface.restore_focus(None) == 'name_field'


def test_focus_order_must_be_unique() -> None:
    with pytest.raises(ValueError, match='unique'):
        FocusSurface(surface_id='s', order=('a', 'a'))


def test_announcement_shape() -> None:
    announcement = StatusAnnouncement(
        event='operation_completed', text='Optimize run finished'
    )
    assert announcement.event == 'operation_completed'
    assert not announcement.urgent
