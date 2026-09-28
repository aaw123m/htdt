"""Persisted shell window/navigation state, one JSON file per project.

``window-state/<project_ref>.json`` remembers the main-window geometry, the
last active workspace and per-workspace context selections *of that
project*, so the next launch — or a project switch that rebuilds the
shell — restores the user's place instead of resetting to the default
Overview every time (#739 window_layout reset scope now has a real
artifact to clear). Round-9 made the file project-scoped (round-8 deferred
item 12): the composition knows its ``project_entry`` before restore runs,
so no signature threading was needed. ``window-state.json`` stays as the
global legacy file — it is the fallback for projects that have no
per-project state yet, and remains the target for unbound windows.

The files are convenience state, never authority: a missing or corrupt
file silently degrades to defaults, and write failures are logged rather
than raised — a failed layout save must never block or crash a close.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .export_io import write_text_atomic
from .workflow_navigation import normalize_destination_id


_LOGGER = logging.getLogger(__name__)

WINDOW_STATE_NAME = 'window-state.json'
WINDOW_STATE_DIR = 'window-state'
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
    #: The project this record was saved under; absent on pre-round-9
    #: global records. Self-describing so recovery tooling can verify the
    #: file was replayed into the project it belongs to.
    project_ref: str | None = None


def window_state_path(data_dir: Path, project_ref: str | None = None) -> Path:
    """The state file for ``project_ref``; the global file when unset."""

    if project_ref is not None:
        ref = _safe_project_ref(project_ref)
        return Path(data_dir) / WINDOW_STATE_DIR / f'{ref}.json'
    return Path(data_dir) / WINDOW_STATE_NAME


def _safe_project_ref(project_ref: str) -> str:
    """Project ids are uuids today; defensively strip filename-hostile
    characters so a foreign/ref-shaped id cannot escape the state dir."""

    return ''.join(
        c if c.isalnum() or c in ('-', '_', '.') else '_' for c in project_ref
    )


def load_window_state(
    data_dir: Path, project_ref: str | None = None
) -> PersistedWindowState | None:
    """Read the persisted state; missing/corrupt/foreign → ``None``.

    Per-project state wins; when absent the legacy global file is the
    fallback (round-8 writes only ever went there), so upgraded installs
    keep their saved layout once per project instead of losing it.
    """

    state = None
    if project_ref is not None:
        state = _read_state(window_state_path(data_dir, project_ref))
    if state is None:
        state = _read_state(window_state_path(data_dir))
    if state is None:
        return None
    return _pruned_state(state)


def _read_state(path: Path) -> PersistedWindowState | None:
    try:
        state = PersistedWindowState.model_validate_json(
            path.read_text(encoding='utf-8')
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        _LOGGER.warning('window state unreadable, using defaults: %s', exc)
        return None
    return state


def _pruned_state(state: PersistedWindowState) -> PersistedWindowState:
    """Drop destinations this build no longer registers — replaying them
    would fail navigation noisily for no benefit."""

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


def save_window_state(
    data_dir: Path,
    state: PersistedWindowState,
    project_ref: str | None = None,
) -> None:
    """Best-effort write; failures are logged, never raised.

    With ``project_ref`` the record lands in ``window-state/`` keyed by the
    project — deliberately NOT mirrored into the global file, or the
    global fallback would keep leaking the last-touched project's layout
    into projects that have none of their own (the round-8 finding).
    """

    if project_ref is not None:
        state = state.model_copy(update={'project_ref': project_ref})
    path = window_state_path(data_dir, project_ref)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, state.model_dump_json())
    except OSError as exc:
        _LOGGER.warning('window state could not be saved: %s', exc)


__all__ = [
    'PersistedWindowState',
    'WINDOW_STATE_DIR',
    'WINDOW_STATE_NAME',
    'load_window_state',
    'save_window_state',
    'window_state_path',
]
