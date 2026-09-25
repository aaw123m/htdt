"""Native Windows accessibility contract support (#731).

The acceptance fixture in docs/ACCESSIBILITY_BASELINE.md needs Qt-free
structures the UI layer can bind: a readable disabled-control reason, a
dynamic-state announcement model, and the deterministic focus-order
declaration. No custom screen-reader semantics here — the contract
delegates to native Win32/UIA announcements; this module keeps the
*content* honest and inspectable.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


AnnouncementEvent = Literal[
    'import_completed',
    'import_failed',
    'operation_queued',
    'operation_running',
    'operation_completed',
    'operation_failed',
    'retake_required',
    'result_stale',
    'save_state',
    'device_disconnected',
]

# Events that must reach the user through a non-visual channel: a status
# surface readable by screen readers, or a native accessibility
# announcement — high-frequency viewport motion is never announced.
REQUIRED_ANNOUNCEMENTS: frozenset[AnnouncementEvent] = frozenset(
    {
        'import_completed',
        'import_failed',
        'operation_queued',
        'operation_completed',
        'operation_failed',
        'retake_required',
        'result_stale',
        'save_state',
        'device_disconnected',
    }
)


class DisabledControlReason(BaseModel):
    """Every disabled primary control must expose a human-readable why."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    control_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_reason(self) -> 'DisabledControlReason':
        # Internal class names, UUIDs and schema tokens are not readable
        # reasons (#731: name/role/value contract).
        if len(self.reason) < 8 or ' ' not in self.reason:
            raise ValueError(
                'disabled reason must be a human-readable phrase'
            )
        return self


class StatusAnnouncement(BaseModel):
    """A dynamic-state transition surfaced for assistive technology."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    event: AnnouncementEvent
    text: str = Field(min_length=1)
    urgent: bool = False


def requires_announcement(event: AnnouncementEvent) -> bool:
    return event in REQUIRED_ANNOUNCEMENTS


class FocusSurface(BaseModel):
    """Deterministic Tab order for one surface (#731 focus model).

    ``order`` lists control ids in exact Tab sequence; Shift+Tab is the
    inverse. A routine refresh restores focus to the focused control if
    it still exists — never resetting to the top unless it disappeared.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    surface_id: str = Field(min_length=1)
    order: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_order(self) -> 'FocusSurface':
        if len(set(self.order)) != len(self.order) or any(
            not c for c in self.order
        ):
            raise ValueError(
                'focus order must contain unique non-empty controls'
            )
        return self

    def next_control(self, current: str) -> str | None:
        if current not in self.order:
            return self.order[0] if self.order else None
        index = self.order.index(current)
        return (
            self.order[index + 1] if index + 1 < len(self.order) else None
        )

    def previous_control(self, current: str) -> str | None:
        if current not in self.order:
            return self.order[0] if self.order else None
        index = self.order.index(current)
        return self.order[index - 1] if index > 0 else None

    def restore_focus(self, focused: str | None) -> str | None:
        """Routine-refresh rule: keep focus on the focused control when it
        still exists; only then fall back to the first control."""
        if focused is not None and focused in self.order:
            return focused
        return self.order[0] if self.order else None


__all__ = [
    'AnnouncementEvent',
    'DisabledControlReason',
    'FocusSurface',
    'REQUIRED_ANNOUNCEMENTS',
    'StatusAnnouncement',
    'requires_announcement',
]
