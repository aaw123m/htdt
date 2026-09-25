"""A/V playback-session reliability authority (#1053).

Static compatibility (#570/#820) answers "is this path structurally
compatible?"; this module answers "how reliably did the exact installed
path establish and retain playback across real sessions?"

- :class:`AVSessionEvent` — one event of a bounded vocabulary: power,
  wake/resume, input select, hot-plug, EDID observed/changed, negotiation
  started/completed, negotiated-mode change, HDCP authentication
  success/failure, audio/video lock/unlock, dropout, format change, CEC
  event, reboot, recovery. Vendor event names stay attached as raw
  labels; unobserved protocol transitions are never invented. A
  successful HDCP/handshake state never implies bypass capability.
- :class:`AVPlaybackSession` — one immutable session pinned to the exact
  path/condition + firmware + preset. A later failure never overwrites a
  recorded success.
- :func:`summarize_reliability` — a summary that always exposes sample
  count and the exact trigger/path/mode scope it covers.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance


def _hash(payload) -> str:
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()


SessionTrigger = Literal[
    'cold_start',
    'resume',
    'input_switch',
    'format_change',
    'hot_plug',
    'manual_test',
    'unknown',
]

AVSessionEventKind = Literal[
    'device_power_on',
    'device_power_off',
    'wake_resume',
    'input_select',
    'hot_plug_detected',
    'hot_plug_lost',
    'edid_observed',
    'edid_changed',
    'negotiation_started',
    'negotiation_completed',
    'negotiated_mode_changed',
    'hdcp_auth_success',
    'hdcp_auth_failure',
    'audio_lock',
    'audio_unlock',
    'video_lock',
    'video_unlock',
    'dropout',
    'format_change',
    'cec_event',
    'device_reboot',
    'automatic_recovery',
    'user_recovery_action',
]


class AVSessionEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: AVSessionEventKind
    occurred_at_s: float = Field(ge=0.0)
    vendor_event_name: str | None = None
    negotiated_mode_ref: str | None = None
    edid_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    detail: str | None = None


class AVPlaybackSession(BaseModel):
    """One immutable playback session on one exact path/condition."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['av-playback-session-1'] = (
        'av-playback-session-1'
    )
    session_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    signal_path_id: str = Field(min_length=1)
    trigger: SessionTrigger = 'unknown'
    source_condition_id: str | None = None
    display_equipment_id: str | None = None
    processor_equipment_id: str | None = None
    firmware_refs: tuple[str, ...] = ()
    operating_preset_id: str | None = None
    started_at_utc: str | None = None
    duration_s: float | None = Field(default=None, ge=0.0)
    initial_mode_ref: str | None = None
    final_mode_ref: str | None = None
    outcome: Literal['success', 'failure', 'recovered', 'unknown'] = 'unknown'
    events: tuple[AVSessionEvent, ...] = ()
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    session_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'session_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'AVPlaybackSession':
        times = [e.occurred_at_s for e in self.events]
        if times != sorted(times):
            raise ValueError('events must be recorded in time order')
        if self.session_sha256 != _hash(self.semantic_payload()):
            raise ValueError('av session semantic hash mismatch')
        return self


def build_av_playback_session(
    *,
    session_id: str,
    version: str,
    signal_path_id: str,
    trigger: SessionTrigger = 'unknown',
    source_condition_id: str | None = None,
    display_equipment_id: str | None = None,
    processor_equipment_id: str | None = None,
    firmware_refs: tuple[str, ...] = (),
    operating_preset_id: str | None = None,
    started_at_utc: str | None = None,
    duration_s: float | None = None,
    initial_mode_ref: str | None = None,
    final_mode_ref: str | None = None,
    outcome: Literal['success', 'failure', 'recovered', 'unknown'] = 'unknown',
    events: tuple[AVSessionEvent, ...] = (),
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> AVPlaybackSession:
    probe = AVPlaybackSession.model_construct(
        session_id=session_id,
        version=version,
        signal_path_id=signal_path_id,
        trigger=trigger,
        source_condition_id=source_condition_id,
        display_equipment_id=display_equipment_id,
        processor_equipment_id=processor_equipment_id,
        firmware_refs=tuple(firmware_refs),
        operating_preset_id=operating_preset_id,
        started_at_utc=started_at_utc,
        duration_s=duration_s,
        initial_mode_ref=initial_mode_ref,
        final_mode_ref=final_mode_ref,
        outcome=outcome,
        events=tuple(events),
        provenance=tuple(provenance),
        session_sha256='',
    )
    return AVPlaybackSession(
        **probe.model_dump(mode='python', exclude={'session_sha256'}),
        session_sha256=_hash(probe.semantic_payload()),
    )


class ReliabilitySummary(BaseModel):
    """Aggregate over sessions — always exposing the sample count and the
    exact scope (path + trigger + mode) the numbers cover."""

    model_config = ConfigDict(frozen=True)

    signal_path_id: str
    trigger: str | None
    mode_ref: str | None
    sample_count: int
    success_count: int
    failure_count: int
    recovered_count: int
    unknown_count: int
    dropout_event_count: int
    negotiation_failure_count: int
    success_ratio: float | None = None


def summarize_reliability(
    sessions: tuple[AVPlaybackSession, ...],
    *,
    signal_path_id: str,
    trigger: SessionTrigger | None = None,
    mode_ref: str | None = None,
) -> ReliabilitySummary:
    """Aggregate sessions for one exact scope. With zero matching
    sessions every ratio is None — never a fake 0% or 100%."""
    scoped = [
        s
        for s in sessions
        if s.signal_path_id == signal_path_id
        and (trigger is None or s.trigger == trigger)
        and (mode_ref is None or s.final_mode_ref == mode_ref)
    ]
    counts = {'success': 0, 'failure': 0, 'recovered': 0, 'unknown': 0}
    dropouts = 0
    negotiation_failures = 0
    for s in scoped:
        counts[s.outcome] += 1
        dropouts += sum(1 for e in s.events if e.kind == 'dropout')
        negotiation_failures += sum(
            1 for e in s.events if e.kind == 'hdcp_auth_failure'
        )
    total = len(scoped)
    return ReliabilitySummary(
        signal_path_id=signal_path_id,
        trigger=trigger,
        mode_ref=mode_ref,
        sample_count=total,
        success_count=counts['success'],
        failure_count=counts['failure'],
        recovered_count=counts['recovered'],
        unknown_count=counts['unknown'],
        dropout_event_count=dropouts,
        negotiation_failure_count=negotiation_failures,
        success_ratio=(
            counts['success'] / total if total else None
        ),
    )
