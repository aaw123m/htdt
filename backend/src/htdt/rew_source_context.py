"""REW 5.40 beta 135 source-container provenance (#1042).

REW beta 135 MeasurementSummary adds producer-side container/calibration
metadata: ``containingFileName``/``containingFilePath``/
``containingFileNotes``, ``micCalFilePath`` and ``soundcardCalFilePath``.

Policy:

- The fields are recognized and preserved exactly — never discarded as
  unknown producer fields — but absolute local paths are privacy-sensitive
  local evidence, never canonical identity. ``RewMeasurementSourceContext``
  hashes only stable content: file *names*, notes, measurement UUID,
  producer version, and *admitted asset hashes* — never raw paths.
- Portable export (``portable_dict``) redacts every absolute path to its
  basename (or ``<redacted>``); the full paths only ever live in the
  private context itself.
- A calibration *path string* is a pointer, not proof. Calibration
  authority binds only through an admitted content-addressed asset
  (``*_cal_asset_sha256``); an unresolved path leaves that capability
  absent — it never authorizes calibration on its own.
- Import is read-only: nothing here ever writes paths back to REW.
"""

from __future__ import annotations

from pathlib import PurePath, PureWindowsPath
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .canonical_json import canonical_json, canonical_sha256 as _hash



REW_SOURCE_CONTEXT_ADAPTER_VERSION = 'rew-source-context-1'

_REDACTED = '<redacted>'


def _basename(path: str | None) -> str | None:
    """Best-effort basename for either local or Windows-style paths."""
    if not path:
        return None
    name = PureWindowsPath(path).name
    if name == path:
        name = PurePath(path).name
    return name or path


class CalibrationPathObservation(BaseModel):
    """A producer-declared calibration-file pointer (#1042 §4).

    ``declared_path`` is private/local evidence only. Capability is derived
    solely from ``asset_sha256`` — the content-addressed admitted calibration
    asset. A path without admitted bytes authorizes nothing.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    declared_path: str = Field(min_length=1)
    #: sha256 of the admitted calibration asset, when its bytes were
    #: imported and hashed. ``None`` = pointer observed, asset unavailable.
    asset_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    #: Sample-rate the calibration applies to when the source declares it
    #: (soundcard calibrations can be rate-specific).
    applicable_sample_rate_hz: int | None = Field(default=None, gt=0)

    @property
    def resolved(self) -> bool:
        return self.asset_sha256 is not None

    @property
    def basename(self) -> str | None:
        return _basename(self.declared_path)


class RewMeasurementSourceContext(BaseModel):
    """Typed producer-side container context for one REW measurement.

    ``context_sha256`` covers stable identity only — file names, notes,
    measurement UUID, producer version, admitted calibration asset hashes —
    never the raw absolute paths, so a path change on identical admitted
    bytes cannot fabricate a new measurement identity (#1042 §3).
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    context_id: str = Field(min_length=1)
    adapter_version: Literal[
        'rew-source-context-1'
    ] = REW_SOURCE_CONTEXT_ADAPTER_VERSION
    measurement_uuid: str = Field(min_length=1)
    rew_version: str | None = None
    # Containing-file metadata — names/notes are portable; the path is not.
    containing_file_name: str | None = None
    containing_file_notes: str | None = None
    containing_file_path: str | None = None
    # Calibration pointers — independent capabilities (mic response vs
    # soundcard response; neither is an absolute-SPL calibration, #643).
    mic_cal: CalibrationPathObservation | None = None
    soundcard_cal: CalibrationPathObservation | None = None
    observed_at: str = Field(min_length=1)
    context_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_context(self) -> 'RewMeasurementSourceContext':
        if self.context_sha256 != _hash(self.identity_payload()):
            raise ValueError('REW source context hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'adapter_version': self.adapter_version,
            'context_id': self.context_id,
            'measurement_uuid': self.measurement_uuid,
            'rew_version': self.rew_version,
            'containing_file_name': self.containing_file_name,
            'containing_file_notes': self.containing_file_notes,
            'containing_file_basename': _basename(self.containing_file_path),
            'mic_cal_asset_sha256': (
                None if self.mic_cal is None else self.mic_cal.asset_sha256
            ),
            'mic_cal_basename': (
                None if self.mic_cal is None else self.mic_cal.basename
            ),
            'soundcard_cal_asset_sha256': (
                None
                if self.soundcard_cal is None
                else self.soundcard_cal.asset_sha256
            ),
            'soundcard_cal_basename': (
                None
                if self.soundcard_cal is None
                else self.soundcard_cal.basename
            ),
            'observed_at': self.observed_at,
        }

    def portable_dict(self) -> dict[str, Any]:
        """Export/diagnostic view — every local path reduced to a basename.

        Absolute paths are never emitted; file notes are source text and
        stay intact.
        """

        def _cal(item: CalibrationPathObservation | None):
            if item is None:
                return None
            return {
                'file_name': item.basename,
                'declared_path': _REDACTED,
                'asset_sha256': item.asset_sha256,
                'applicable_sample_rate_hz': item.applicable_sample_rate_hz,
                'resolved': item.resolved,
            }

        return {
            'adapter_version': self.adapter_version,
            'context_id': self.context_id,
            'measurement_uuid': self.measurement_uuid,
            'rew_version': self.rew_version,
            'containing_file_name': self.containing_file_name,
            'containing_file_notes': self.containing_file_notes,
            'containing_file_path': _REDACTED
            if self.containing_file_path
            else None,
            'mic_cal': _cal(self.mic_cal),
            'soundcard_cal': _cal(self.soundcard_cal),
            'observed_at': self.observed_at,
            'context_sha256': self.context_sha256,
        }




def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def extract_rew_source_context(
    measurement_summary: dict[str, Any],
    *,
    measurement_uuid: str,
    observed_at: str,
    rew_version: str | None = None,
    mic_cal_asset_sha256: str | None = None,
    soundcard_cal_asset_sha256: str | None = None,
    mic_cal_sample_rate_hz: int | None = None,
    soundcard_cal_sample_rate_hz: int | None = None,
    context_id: str | None = None,
) -> RewMeasurementSourceContext:
    """Recognize the beta-135 summary fields explicitly.

    Older REW snapshots lack every one of these keys — they produce a
    context with all container/calibration fields ``None`` (explicitly
    unavailable), never fabricated paths.
    """

    mic_path = _text(measurement_summary.get('micCalFilePath'))
    card_path = _text(measurement_summary.get('soundcardCalFilePath'))
    mic_cal = (
        CalibrationPathObservation(
            declared_path=mic_path,
            asset_sha256=mic_cal_asset_sha256,
            applicable_sample_rate_hz=mic_cal_sample_rate_hz,
        )
        if mic_path is not None or mic_cal_asset_sha256 is not None
        else None
    )
    soundcard_cal = (
        CalibrationPathObservation(
            declared_path=card_path,
            asset_sha256=soundcard_cal_asset_sha256,
            applicable_sample_rate_hz=soundcard_cal_sample_rate_hz,
        )
        if card_path is not None or soundcard_cal_asset_sha256 is not None
        else None
    )
    version = rew_version or _text(measurement_summary.get('rewVersion'))
    payload: dict[str, Any] = {
        'context_id': context_id or str(uuid4()),
        'measurement_uuid': measurement_uuid,
        'rew_version': version,
        'containing_file_name': _text(
            measurement_summary.get('containingFileName')
        ),
        'containing_file_notes': _text(
            measurement_summary.get('containingFileNotes')
        ),
        'containing_file_path': _text(
            measurement_summary.get('containingFilePath')
        ),
        'mic_cal': mic_cal,
        'soundcard_cal': soundcard_cal,
        'observed_at': observed_at,
    }
    provisional = RewMeasurementSourceContext.model_construct(
        **payload, context_sha256='0' * 64
    )
    return RewMeasurementSourceContext(
        **payload, context_sha256=_hash(provisional.identity_payload())
    )
