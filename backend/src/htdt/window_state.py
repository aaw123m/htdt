"""Persisted shell window/navigation state, one JSON file per data root.

``window-state.json`` remembers the main-window geometry, the last active
workspace and per-workspace context selections so the next launch — or a
project switch that rebuilds the shell — restores the user's place instead
of resetting to the default Overview every time (#739 window_layout reset
scope now has a real artifact to clear).

The file is convenience state, never authority: a missing or corrupt file
silently degrades to defaults, and write failures are logged rather than
raised — a failed layout save must never block or crash a close.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .workflow_navigation import normalize_destination_id


_LOGGER = logging.getLogger(__name__)

WINDOW_STATE_NAME = 'window-state.json'
WINDOW_STATE_SCHEMA = 1


class PersistedWindowState(BaseModel):
    """Durable shell presentation state for one data root."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = WINDOW_STATE_SCHEMA
    #: QByteArray.saveGeometry() blob (geometry + maximized/fullscreen
    #: state), base64 so the file stays plain JSON.
    geometry_b64: str | None = None
    workspace: str | None = None
    contexts: dict[str, str] = Field(default_factory=dict)


def window_state_path(data_dir: Path) -> Path:
    return Path(data_dir) / WINDOW_STATE_NAME


def load_window_state(data_dir: Path) -> PersistedWindowState | None:
    """Read the persisted state; missing/corrupt/foreign → ``None``."""

    path = window_state_path(data_dir)
    try:
        state = PersistedWindowState.model_validate_json(
            path.read_text(encoding='utf-8')
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        _LOGGER.warning('window state unreadable, using defaults: %s', exc)
        return None
    # Drop destinations this build no longer registers — replaying them
    # would fail navigation noisily for no benefit.
    if state.workspace is not None:
        try:
            normalize_destination_id(state.workspace)
        except ValueError:
            state = state.model_copy(update={'workspace': None})
    contexts = {
        workspace: context
        for workspace, context in state.contexts.items()
        if _known_destination(workspace)
    }
    if contexts != state.contexts:
        state = state.model_copy(update={'contexts': contexts})
    return state


def _known_destination(workspace: str) -> bool:
    try:
        normalize_destination_id(workspace)
    except ValueError:
        return False
    return True


def save_window_state(data_dir: Path, state: PersistedWindowState) -> None:
    """Best-effort write; failures are logged, never raised."""

    path = window_state_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(state.model_dump_json(), encoding='utf-8')
    except OSError as exc:
        _LOGGER.warning('window state could not be saved: %s', exc)


__all__ = [
    'PersistedWindowState',
    'WINDOW_STATE_NAME',
    'load_window_state',
    'save_window_state',
    'window_state_path',
]
