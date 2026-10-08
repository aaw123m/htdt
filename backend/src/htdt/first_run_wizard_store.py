"""File-backed resume state for the first-run wizard (#886).

This is NOT sealed authority — it only records that a wizard session was
started/dismissed and which stage was last shown. Stage progress is
always re-derived from canonical sealed stores, so a tampered or lost
state file can only hide the wizard UI, never fabricate evidence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

WIZARD_STATE_DIRNAME = 'first-run-wizard'
WIZARD_STATE_FILENAME = 'state.json'
WIZARD_STATE_SCHEMA_VERSION = 1


class FirstRunWizardRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    schema_version: int = WIZARD_STATE_SCHEMA_VERSION
    started_utc: str
    last_shown_stage: str | None = None
    status: Literal['active', 'dismissed', 'completed'] = 'active'
    updated_utc: str


def wizard_state_path(data_dir: Path) -> Path:
    return Path(data_dir) / WIZARD_STATE_DIRNAME / WIZARD_STATE_FILENAME


def load_wizard_state(data_dir: Path) -> FirstRunWizardRecord | None:
    """Load the resume record; a corrupt/unreadable file is treated as
    "no record" — never blocks startup, never silently replaced."""
    path = wizard_state_path(data_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        return FirstRunWizardRecord.model_validate(payload)
    except Exception:  # error-boundary: corrupt state → absent record
        return None


def save_wizard_state(data_dir: Path, record: FirstRunWizardRecord) -> Path:
    path = wizard_state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(
        record.model_dump_json(indent=2), encoding='utf-8'
    )
    tmp.replace(path)
    return path


__all__ = [
    'FirstRunWizardRecord',
    'WIZARD_STATE_SCHEMA_VERSION',
    'load_wizard_state',
    'save_wizard_state',
    'wizard_state_path',
]
