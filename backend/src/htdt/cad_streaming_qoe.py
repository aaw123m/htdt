"""Streaming playback QoE authority (#1052).

Preserves startup, buffering, stall, representation/format-change and
source-output evidence as an immutable event timeline — not one opaque
quality score, and never a network optimizer.

- :class:`StreamingPlaybackEvent` — one timestamped event of a bounded
  vocabulary (playback_requested / first_frame / buffering / stall /
  representation_change / format_change / error / recovery / …).
- :class:`StreamingPlaybackSession` — the immutable session authority
  binding exact device + app + operating state, plus optional source
  output observations (#1045 consumes them; they are never conflated with
  the stream representation).
- :func:`derive_qoe_metrics` — derived metrics (startup delay, rebuffer
  duration/count/ratio, longest stall, error count, switch counts). Every
  metric is ``None`` when its events are missing — missing data produces
  UNKNOWN, never a zero.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload




StreamingEventKind = Literal[
    'playback_requested',
    'first_frame',
    'first_audio',
    'playing',
    'buffering_start',
    'buffering_end',
    'stall',
    'representation_change',
    'bitrate_change',
    'raster_change',
    'hdr_change',
    'audio_format_change',
    'seek',
    'error',
    'retry',
    'recovery',
    'playback_end',
]


class StreamingPlaybackEvent(BaseModel):
    """One QoE event. ``occurred_at_s`` is seconds since session start;
    detail fields are optional per kind — missing data stays absent."""

    model_config = ConfigDict(frozen=True)

    kind: StreamingEventKind
    occurred_at_s: float = Field(ge=0.0)
    representation_id: str | None = None
    bitrate_kbps: int | None = Field(default=None, gt=0)
    raster: str | None = None
    audio_format: str | None = None
    error_code: str | None = None
    detail: str | None = None


class SourceOutputObservation(BaseModel):
    """A physical output observation during the session — distinct from
    the stream's logical representation."""

    model_config = ConfigDict(frozen=True)

    observed_at_s: float = Field(ge=0.0)
    video_mode: str | None = None
    audio_format: str | None = None
    hdr_family: str | None = None
    evidence_ref: str | None = None


class StreamingPlaybackSession(BaseModel):
    """Immutable QoE session authority. Content title/name is never
    required — a synthetic or anonymized profile ref suffices."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['streaming-qoe-session-1'] = (
        'streaming-qoe-session-1'
    )
    session_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    device_equipment_id: str = Field(min_length=1)
    app_identity: str | None = None
    app_version: str | None = None
    content_profile_ref: str | None = None
    operating_state_id: str | None = None
    started_at_utc: str | None = None
    ended_at_utc: str | None = None
    duration_s: float | None = Field(default=None, ge=0.0)
    network_interface: str | None = None
    privacy_class: Literal['local_private', 'exportable'] = 'local_private'
    events: tuple[StreamingPlaybackEvent, ...] = ()
    source_output_observations: tuple[SourceOutputObservation, ...] = ()
    limitations: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    session_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'session_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'StreamingPlaybackSession':
        times = [e.occurred_at_s for e in self.events]
        if times != sorted(times):
            raise ValueError('events must be recorded in time order')
        if self.session_sha256 != _hash(self.semantic_payload()):
            raise ValueError('streaming session semantic hash mismatch')
        return self


def build_streaming_session(
    *,
    session_id: str,
    version: str,
    device_equipment_id: str,
    app_identity: str | None = None,
    app_version: str | None = None,
    content_profile_ref: str | None = None,
    operating_state_id: str | None = None,
    started_at_utc: str | None = None,
    ended_at_utc: str | None = None,
    duration_s: float | None = None,
    network_interface: str | None = None,
    privacy_class: Literal['local_private', 'exportable'] = 'local_private',
    events: tuple[StreamingPlaybackEvent, ...] = (),
    source_output_observations: tuple[SourceOutputObservation, ...] = (),
    limitations: str | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> StreamingPlaybackSession:
    probe = StreamingPlaybackSession.model_construct(**canonicalize_payload(StreamingPlaybackSession, dict(
        session_id=session_id,
        version=version,
        device_equipment_id=device_equipment_id,
        app_identity=app_identity,
        app_version=app_version,
        content_profile_ref=content_profile_ref,
        operating_state_id=operating_state_id,
        started_at_utc=started_at_utc,
        ended_at_utc=ended_at_utc,
        duration_s=duration_s,
        network_interface=network_interface,
        privacy_class=privacy_class,
        events=tuple(events),
        source_output_observations=tuple(source_output_observations),
        limitations=limitations,
        provenance=tuple(provenance),
        session_sha256='',
    )))
    return StreamingPlaybackSession(
        **probe.model_dump(mode='python', exclude={'session_sha256'}),
        session_sha256=_hash(probe.semantic_payload()),
    )


class StreamingQoEMetrics(BaseModel):
    """Derived session metrics. Every field is ``None`` when the events
    needed to compute it are absent — missing data is UNKNOWN, never 0."""

    model_config = ConfigDict(frozen=True)

    startup_delay_s: float | None = None
    rebuffer_count: int | None = None
    total_rebuffer_s: float | None = None
    rebuffer_ratio: float | None = None
    longest_stall_s: float | None = None
    playback_error_count: int | None = None
    representation_switch_count: int | None = None
    audio_format_change_count: int | None = None


def derive_qoe_metrics(session: StreamingPlaybackSession) -> StreamingQoEMetrics:
    events = list(session.events)
    by_kind = {}
    for e in events:
        by_kind.setdefault(e.kind, []).append(e)

    # Startup delay requires both request and first playback evidence.
    if 'playback_requested' in by_kind and (
        'first_frame' in by_kind or 'first_audio' in by_kind
    ):
        start = by_kind['playback_requested'][0].occurred_at_s
        first = min(
            e.occurred_at_s
            for kind in ('first_frame', 'first_audio')
            for e in by_kind.get(kind, [])
        )
        startup_delay = first - start
    else:
        startup_delay = None

    buffering_starts = by_kind.get('buffering_start', [])
    buffering_ends = by_kind.get('buffering_end', [])
    if buffering_starts:
        rebuffer_count = len(buffering_starts)
        total = 0.0
        longest = 0.0
        for i, start_event in enumerate(buffering_starts):
            end_event = (
                buffering_ends[i]
                if i < len(buffering_ends)
                else None
            )
            if end_event is not None:
                span = end_event.occurred_at_s - start_event.occurred_at_s
                total += span
                longest = max(longest, span)
        total_rebuffer = total
        longest_stall = longest if buffering_ends else None
    else:
        rebuffer_count = None
        total_rebuffer = None
        longest_stall = None

    if (
        rebuffer_count is not None
        and session.duration_s is not None
        and session.duration_s > 0.0
    ):
        rebuffer_ratio = total_rebuffer / session.duration_s
    else:
        rebuffer_ratio = None

    error_count = len(by_kind.get('error', [])) if 'error' in by_kind else None
    switch_count = (
        len(by_kind.get('representation_change', []))
        if 'representation_change' in by_kind
        else None
    )
    audio_changes = (
        len(by_kind.get('audio_format_change', []))
        if 'audio_format_change' in by_kind
        else None
    )

    return StreamingQoEMetrics(
        startup_delay_s=startup_delay,
        rebuffer_count=rebuffer_count,
        total_rebuffer_s=total_rebuffer,
        rebuffer_ratio=rebuffer_ratio,
        longest_stall_s=longest_stall,
        playback_error_count=error_count,
        representation_switch_count=switch_count,
        audio_format_change_count=audio_changes,
    )
