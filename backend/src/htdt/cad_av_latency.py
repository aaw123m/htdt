"""End-to-end A/V latency (lip-sync) authority (issue #582).

A system can be acoustically and visually correct and still feel wrong:
this module owns the *relative* timing between the reproduced audio
scene and the displayed image for one exact signal path, in one exact
device/profile mode, with one explicit sign convention.

Composition — this module deliberately does not re-model what other
authorities already own:

- :class:`~.cad_signal_path.AVSignalPath` (#570) owns the route
  topology (source -> AVR/switch -> display incl. eARC return routes).
  An ``AVLatencyPath`` pins one ``signal_path`` by id+version+sha256;
  any route change is a different latency path by construction.
- :mod:`cad_video_latency` (#1005) owns *display input-to-photon*
  latency; ``display_scanout_emission`` stages here reference, never
  re-derive, that authority's per-mode conditions.
- :mod:`cad_hdmi_transport` (#1041) owns per-port HDMI feature
  evidence and LIP readback. LIP values enter here only as
  ``protocol_reported`` evidence — never as a measured offset.
- :mod:`cad_correction_qualification` (#568) and the FIR/mixed-phase
  filter chain (#201) expose implementation delay; a
  ``filter_implementation_delay`` stage records it with the filter's
  sha so a changed filter stales the path.

Rules that are mechanically enforced:

- One sign convention: ``audio_late_positive`` — a positive offset
  means the acoustic event arrives *after* the photonic event (audio
  lags video). No record may carry a bare ``+80 ms``.
- ``composite_latency_seconds`` is fail-closed: any stage without a
  latency value makes the whole path UNKNOWN — a "zero delay" claim
  is impossible by construction.
- Per-speaker/channel acoustic alignment is a different problem
  (speaker-to-speaker timing relative to the listener). Every
  ``AVLatencyPath`` is permanently ``alignment_scope =
  'master_av_sync'``; compensation locations are restricted to
  master-sync insertion points so alignment math can never consume
  the master lip-sync delay (and vice versa).
- A measurement binds the exact path sha it measured. Evaluating a
  qualification against a different path revision resolves to
  ``stale`` — a filter length change, a picture-mode change or an
  eARC route change never silently reuses the old sync result.
- Protocol-reported offsets (LIP) and physically measured offsets are
  kept in separate fields; a ``lip_protocol_reported`` method record
  cannot mint a measured offset at all.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_video_latency import HdmilFeatureState
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


AV_LATENCY_PATH_AUTHORITY_VERSION = 'av-latency-path-1'
AV_LATENCY_MEASUREMENT_AUTHORITY_VERSION = 'av-latency-measurement-1'
AV_LATENCY_QUALIFICATION_AUTHORITY_VERSION = 'av-latency-qualification-1'
AV_LATENCY_PROFILE_AUTHORITY_VERSION = 'av-latency-profile-1'
AV_LATENCY_EVALUATION_VERSION = 'av-latency-eval-1'

_SHA256 = r'^[0-9a-f]{64}$'


LatencyComponentKind = Literal[
    'device_processing',
    'filter_implementation_delay',
    'buffering',
    'transport',
    'video_frame_processing',
    'display_scanout_emission',
    'audio_output_electrical',
    'acoustic_propagation',
]
"""Latency quantity taxonomy (#582 §2). ``filter_implementation_delay``
is where FIR/mixed-phase correction delay (#201/#568) enters the audio
path; ``acoustic_propagation`` is loudspeaker-to-listener air delay and
must never be double-counted against speaker-distance alignment."""

LatencyEvidenceClass = Literal[
    'protocol_reported',
    'manufacturer_declared',
    'derived_from_configuration',
    'electrically_measured',
    'acoustically_optically_measured',
    'unknown',
]
"""Evidence class of one latency figure (#582 §3). LIP/EDID readback is
``protocol_reported`` — useful device metadata that remains distinct
from a physical end-to-end measurement."""

_MEASURED_CLASSES = frozenset(
    {'electrically_measured', 'acoustically_optically_measured'}
)

AVLatencySignConvention = Literal['audio_late_positive']
"""The single sign convention (#582 §7): a positive relative offset
means the acoustic event arrives *after* the photonic event — audio
lags video. Negative means audio leads."""

AVLatencyMeasurementMethod = Literal[
    'lip_protocol_reported',
    'electrical_measurement',
    'acoustic_optical_measurement',
    'manual_estimate',
]
"""Offset acquisition method (#582 §6). ``manual_estimate`` (handheld
stopwatch / eyeball comparison) is a valid *low-confidence* class — it
is labeled so qualification can refuse it where the profile demands
physical evidence."""

CommonClockState = Literal['shared', 'independent', 'unknown']

AVLatencyProfileKind = Literal[
    'itu_r_bt_1359_1',
    'atsc_is_191',
    'gaming_strict',
    'project_custom',
    'other_evidence_profile',
]
"""Versioned perceptual/standards profiles (#582 §11). Broadcast
acceptability bounds are never silently transferred to interactive or
gaming use — the profile carries its own scope/context."""

CompensationLocation = Literal[
    'source_audio_delay',
    'source_video_delay',
    'avr_audio_delay',
    'dsp_audio_delay',
    'display_reported_latency_compensation',
    'hdmi_lip_automatic',
    'other',
]
"""Master A/V-sync insertion points (#582 §8). Per-speaker distance /
alignment trims are deliberately absent — they are channel-alignment
state, not master-sync compensation (issue §12)."""

AVLatencyVerdict = Literal[
    'qualified_within_profile',
    'exceeds_profile_bounds',
    'insufficient_evidence',
    'stale',
]
"""Closed-loop qualification verdicts (#582 §14). There is no "pass by
construction": without a bound physical measurement the verdict is
``insufficient_evidence``, and a path revision mismatch is ``stale``."""


def _require_iso8601(value: str, label: str) -> None:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


# ---------------------------------------------------------------------------
# Stages + compensation
# ---------------------------------------------------------------------------


class AVLatencyStage(BaseModel):
    """One processing stage of the audio or video path (#582 §1-§3).

    A stage may carry a *nominal/reported* latency, a *measured* latency,
    or neither (variable/unknown). ``latency_seconds`` resolves measured
    first, then nominal; a stage with neither is UNKNOWN and poisons any
    end-to-end sum — fail-closed by design.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    stage_id: str = Field(min_length=1)
    role: Literal['audio', 'video']
    component_kind: LatencyComponentKind
    device_ref: str | None = None
    hop_label: str | None = None
    """Edge/route label inside the bound signal path this stage sits on
    (e.g. ``avr-hdmi3``) — keeps stage-to-hop accountability."""
    operating_mode: str | None = None
    """Exact device/profile mode this stage figure binds (#582 §4) —
    e.g. ``movie``, ``game``, ``direct``."""
    nominal_latency_seconds: float | None = Field(default=None, ge=0.0)
    measured_latency_seconds: float | None = Field(default=None, ge=0.0)
    latency_min_seconds: float | None = Field(default=None, ge=0.0)
    latency_max_seconds: float | None = Field(default=None, ge=0.0)
    """Observed variability range for non-fixed paths (#582 §9 — VRR,
    adaptive processing, buffering)."""
    evidence_class: LatencyEvidenceClass = 'unknown'
    clock_domain: str | None = None
    """Clock/timebase identity where it matters (e.g. a PTP domain or a
    network media clock) — composes with the #591 timing authority."""
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    note: str | None = None

    @property
    def latency_seconds(self) -> float | None:
        """Best available figure: measured wins over nominal; None =
        UNKNOWN."""
        if self.measured_latency_seconds is not None:
            return self.measured_latency_seconds
        return self.nominal_latency_seconds

    @model_validator(mode='after')
    def _check(self) -> 'AVLatencyStage':
        if self.measured_latency_seconds is not None and (
            self.evidence_class not in _MEASURED_CLASSES
        ):
            raise ValueError(
                'a measured stage latency requires a measured evidence '
                'class — protocol/manufacturer figures are nominal'
            )
        if (
            self.latency_min_seconds is not None
            or self.latency_max_seconds is not None
        ):
            if (
                self.latency_min_seconds is None
                or self.latency_max_seconds is None
            ):
                raise ValueError(
                    'a variable latency range needs both min and max'
                )
            if self.latency_min_seconds > self.latency_max_seconds:
                raise ValueError('latency min exceeds max')
        if (
            self.latency_seconds is not None
            and self.evidence_class == 'unknown'
        ):
            raise ValueError(
                'a latency value requires a real evidence class — '
                'unknown evidence cannot assert a number'
            )
        return self


class AVLatencyCompensationEntry(BaseModel):
    """One applied/available sync-delay insertion (#582 §8).

    Only master-sync locations exist — speaker-distance trims can never
    appear here, so the master A/V delay cannot silently migrate into
    stored speaker geometry (and per-speaker alignment never consumes
    it).
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    location: CompensationLocation
    device_ref: str | None = None
    requested_seconds: float = Field(ge=0.0)
    applied_seconds: float | None = Field(default=None, ge=0.0)
    """Observed/applied value where the device reports it — the
    requested value alone is never the applied truth."""
    range_min_seconds: float | None = Field(default=None, ge=0.0)
    range_max_seconds: float | None = Field(default=None, ge=0.0)
    resolution_seconds: float | None = Field(default=None, gt=0.0)
    mode_dependent: bool = False
    """Whether the compensation changes with device mode (e.g. ALLM)."""
    note: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'AVLatencyCompensationEntry':
        if (
            self.range_min_seconds is not None
            and self.range_max_seconds is not None
            and self.range_min_seconds > self.range_max_seconds
        ):
            raise ValueError('compensation range min exceeds max')
        if self.applied_seconds is not None and (
            self.range_min_seconds is not None
            and self.range_max_seconds is not None
            and not (
                self.range_min_seconds - 1e-9
                <= self.applied_seconds
                <= self.range_max_seconds + 1e-9
            )
        ):
            raise ValueError(
                'applied compensation lies outside the device range'
            )
        return self


# ---------------------------------------------------------------------------
# Perceptual profile (BT.1359 / ATSC IS-191 / custom)
# ---------------------------------------------------------------------------


class AVLatencyProfile(BaseModel):
    """Versioned perceptual acceptability profile (#582 §11).

    Bounds are stored in the path's single convention
    (``audio_late_positive``): ``*_min_seconds`` is the largest tolerable
    audio-lead (negative), ``*_max_seconds`` the largest tolerable
    audio-lag (positive). ``context`` records who/what the bounds are
    for — broadcast acceptability is never silently applied to gaming.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    authority_version: Literal[
        'av-latency-profile-1'
    ] = AV_LATENCY_PROFILE_AUTHORITY_VERSION
    profile_id: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=_SHA256)
    document_id: str | None = None
    profile_kind: AVLatencyProfileKind
    label: str = Field(min_length=1)
    context: str = Field(min_length=1)
    """Scope statement (e.g. 'broadcast end-to-end', 'interactive
    gaming') — carried on every verdict."""
    standard_ref: str | None = None
    """External-document identity (e.g. ``itu-r-bt1359@1``) — pins the
    exact revision in the standards registry (#599)."""
    detectability_min_seconds: float
    detectability_max_seconds: float
    acceptability_min_seconds: float
    acceptability_max_seconds: float
    requires_physical_measurement: bool = True
    """Whether the profile demands a physical/electrical measurement;
    ``manual_estimate`` evidence cannot qualify a True profile."""
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    created_at_utc: str = Field(min_length=1)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'profile_id', 'profile_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'AVLatencyProfile':
        _require_iso8601(self.created_at_utc, 'created_at_utc')
        if self.detectability_min_seconds < self.acceptability_min_seconds:
            raise ValueError(
                'detectability lead bound must sit inside acceptability'
            )
        if self.detectability_max_seconds > self.acceptability_max_seconds:
            raise ValueError(
                'detectability lag bound must sit inside acceptability'
            )
        if self.detectability_min_seconds > 0.0 or (
            self.detectability_max_seconds < 0.0
        ):
            raise ValueError(
                'detectability bounds must straddle zero offset'
            )
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('av sync profile hash mismatch')
        if self.profile_id != f'avlatprof:{expected}':
            raise ValueError('av sync profile id mismatch')
        return self


def build_av_latency_profile(
    *, created_at_utc: str | None = None, **kwargs: Any
) -> AVLatencyProfile:
    probe = AVLatencyProfile.model_construct(
        **canonicalize_payload(
            AVLatencyProfile,
            dict(
                schema_version=1,
                authority_version=AV_LATENCY_PROFILE_AUTHORITY_VERSION,
                profile_id='',
                profile_sha256='0' * 64,
                created_at_utc=created_at_utc or _utc_now(),
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return AVLatencyProfile(
        **probe.model_dump(mode='python', exclude={'profile_id', 'profile_sha256'}),
        profile_id=f'avlatprof:{sha}',
        profile_sha256=sha,
    )


def build_itu_bt1359_profile(**kwargs: Any) -> AVLatencyProfile:
    """ITU-R BT.1359-1 bounds converted to ``audio_late_positive``:

    the recommendation (sound-advance-positive: detectability about
    +45/-125 ms, acceptability +90/-185 ms) becomes audio-lead 45 ms /
    audio-lag 125 ms detectable and 90/185 ms acceptable for broadcast
    end-to-end chains. Residential/gaming use keeps its own profile —
    these bounds are broadcast-chain evidence.
    """
    return build_av_latency_profile(
        profile_kind='itu_r_bt_1359_1',
        label='ITU-R BT.1359-1 relative timing of sound and vision',
        context='broadcast end-to-end chain (production -> emission)',
        standard_ref='itu-r-bt1359@1',
        detectability_min_seconds=-0.045,
        detectability_max_seconds=0.125,
        acceptability_min_seconds=-0.090,
        acceptability_max_seconds=0.185,
        requires_physical_measurement=True,
        **kwargs,
    )


def build_atsc_is191_profile(**kwargs: Any) -> AVLatencyProfile:
    """ATSC IS-191 operational bound: sound never leads video by more
    than 15 ms and never lags by more than 45 ms — a stricter
    broadcast-operations profile than BT.1359's perceptual bounds."""
    return build_av_latency_profile(
        profile_kind='atsc_is_191',
        label='ATSC IS-191 relative timing of sound and vision',
        context='broadcast operations encoder-input / emission bound',
        standard_ref='atsc-is@191',
        detectability_min_seconds=-0.015,
        detectability_max_seconds=0.045,
        acceptability_min_seconds=-0.015,
        acceptability_max_seconds=0.045,
        requires_physical_measurement=True,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Latency path
# ---------------------------------------------------------------------------


class AVLatencyPath(BaseModel):
    """One exact end-to-end A/V path state for sync evaluation (#582).

    Sealed: every latency-relevant mode/route/filter fact is inside the
    semantic hash, so any change (picture mode, ALLM, FIR length, eARC
    route) produces a different ``path_sha256`` and stales evidence
    bound to the old one.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    authority_version: Literal[
        'av-latency-path-1'
    ] = AV_LATENCY_PATH_AUTHORITY_VERSION
    path_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    path_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    label: str | None = None

    # Route binding — all-or-none triple into #570 AVSignalPath.
    signal_path_id: str | None = None
    signal_path_version: str | None = None
    signal_path_sha256: str | None = Field(default=None, pattern=_SHA256)

    # Mode/profile identity (#582 §4) — any change stales the path.
    display_picture_mode: str | None = None
    allm_state: HdmilFeatureState = 'unknown'
    vrr_state: HdmilFeatureState = 'unknown'
    source_frame_rate_hz: float | None = Field(default=None, gt=0.0)
    source_format_label: str | None = None
    scaling_mode: str | None = None
    motion_interpolation_mode: str | None = None
    tone_map_mode: str | None = None
    projector_frame_mode: str | None = None
    avr_dsp_profile: str | None = None
    room_correction_filter_ref: str | None = None
    room_correction_filter_sha256: str | None = Field(
        default=None, pattern=_SHA256
    )
    """Exact correction-filter identity (#201/#568) — a changed FIR
    length/truncation/modeling delay changes this sha and stales the
    path's sync evidence."""
    sample_rate_hz: float | None = Field(default=None, gt=0.0)
    bass_management_mode: str | None = None
    external_dsp_routing: str | None = None
    audio_route: str | None = None
    """e.g. ``source-avr-display`` vs ``source-display-earc-avr``
    (#582 §13) — each topology is a distinct latency path."""
    firmware_versions: tuple[str, ...] = ()

    audio_stages: tuple[AVLatencyStage, ...] = ()
    video_stages: tuple[AVLatencyStage, ...] = ()
    compensations: tuple[AVLatencyCompensationEntry, ...] = ()

    alignment_scope: Literal['master_av_sync'] = 'master_av_sync'
    """Permanent marker (#582 §12): this path answers
    ``MASTER_AUDIO_TO_VIDEO_SYNC`` only. Channel-to-channel acoustic
    alignment lives in the speaker/distance authorities."""

    provenance: tuple[EquipmentDataProvenance, ...] = ()
    created_at_utc: str = Field(min_length=1)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'path_id', 'version', 'path_sha256'}
        )

    def composite_latency_seconds(self, role: str) -> float | None:
        """Sum of stage latencies for ``role`` — fail-closed: any stage
        without a latency figure returns None (UNKNOWN total)."""
        stages = (
            self.audio_stages if role == 'audio' else self.video_stages
        )
        total = 0.0
        for stage in stages:
            value = stage.latency_seconds
            if value is None:
                return None
            total += value
        return total

    @property
    def relative_offset_seconds(self) -> float | None:
        """Audio-minus-video composite offset under
        ``audio_late_positive`` — None when either side is UNKNOWN."""
        audio = self.composite_latency_seconds('audio')
        video = self.composite_latency_seconds('video')
        if audio is None or video is None:
            return None
        return audio - video

    @property
    def applied_compensation_seconds(self) -> float:
        """Sum of *applied* compensations — requested-but-unobserved
        entries contribute 0 (a requested delay is not an applied one)."""
        return sum(
            c.applied_seconds
            for c in self.compensations
            if c.applied_seconds is not None
        )

    @model_validator(mode='after')
    def _check(self) -> 'AVLatencyPath':
        triple = (
            self.signal_path_id,
            self.signal_path_version,
            self.signal_path_sha256,
        )
        if (None in triple) and any(v is not None for v in triple):
            raise ValueError(
                'signal path id/version/sha256 must be supplied together '
                'or not at all'
            )
        for stage in self.audio_stages:
            if stage.role != 'audio':
                raise ValueError('non-audio stage in audio_stages')
        for stage in self.video_stages:
            if stage.role != 'video':
                raise ValueError('non-video stage in video_stages')
        _require_iso8601(self.created_at_utc, 'created_at_utc')
        expected = _hash(self.identity_payload())
        if self.path_sha256 != expected:
            raise ValueError('av latency path hash mismatch')
        return self


def build_av_latency_path(
    *,
    path_id: str,
    version: str,
    document_id: str,
    created_at_utc: str | None = None,
    **kwargs: Any,
) -> AVLatencyPath:
    probe = AVLatencyPath.model_construct(
        **canonicalize_payload(
            AVLatencyPath,
            dict(
                schema_version=1,
                authority_version=AV_LATENCY_PATH_AUTHORITY_VERSION,
                path_id=path_id,
                version=version,
                path_sha256='0' * 64,
                document_id=document_id,
                created_at_utc=created_at_utc or _utc_now(),
                **kwargs,
            ),
        )
    )
    return AVLatencyPath(
        **probe.model_dump(mode='python', exclude={'path_sha256'}),
        path_sha256=_hash(probe.identity_payload()),
    )


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


class AVLatencyObservation(BaseModel):
    """One repeated offset observation under the bound method."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    observed_at_utc: str = Field(min_length=1)
    offset_seconds: float
    """``audio_late_positive`` convention."""
    note: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'AVLatencyObservation':
        _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        return self


class AVLatencyPathMeasurement(BaseModel):
    """Physical/protocol A/V-sync evidence bound to one path revision
    (#582 §6).

    ``lip_reported_offset_seconds`` is protocol metadata (HDMI 2.2 LIP
    readback) — kept in a separate field so protocol and physical
    evidence are never conflated; a ``lip_protocol_reported`` method
    record cannot carry observations at all.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    authority_version: Literal[
        'av-latency-measurement-1'
    ] = AV_LATENCY_MEASUREMENT_AUTHORITY_VERSION
    measurement_id: str = Field(min_length=1)
    measurement_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    path_id: str = Field(min_length=1)
    path_version: str = Field(min_length=1)
    path_sha256: str = Field(pattern=_SHA256)

    method: AVLatencyMeasurementMethod
    method_version: str | None = None
    stimulus_asset_ref: str | None = None
    stimulus_sha256: str | None = Field(default=None, pattern=_SHA256)
    """Exact test-asset identity (e.g. an AV-sync flash/beep clip) —
    an unnamed clip cannot anchor a measurement."""
    source_player: str | None = None
    capture_device: str | None = None
    common_clock_state: CommonClockState = 'unknown'
    optical_sensor_delay_seconds: float | None = Field(
        default=None, ge=0.0
    )
    acoustic_sensor_delay_seconds: float | None = Field(
        default=None, ge=0.0
    )
    """Sensor delay/calibration of the optical + acoustic probes."""
    algorithm_version: str | None = None

    observations: tuple[AVLatencyObservation, ...] = ()
    mean_offset_seconds: float | None = None
    min_offset_seconds: float | None = None
    max_offset_seconds: float | None = None
    """Measured offset distribution — one scalar never qualifies a
    variable path (#582 §9)."""
    uncertainty_seconds: float | None = Field(default=None, ge=0.0)

    lip_reported_offset_seconds: float | None = None
    lip_evidence_ref: str | None = None
    """HDMI LIP protocol-reported offset, bound to its
    ``HDMILatencyIndicationEvidence`` record — reported metadata, kept
    beside (never inside) the measured distribution."""

    measured_at_utc: str = Field(min_length=1)
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'measurement_id', 'measurement_sha256'},
        )

    @property
    def protocol_physical_mismatch(self) -> bool | None:
        """Whether the LIP-reported and physically measured offsets
        disagree beyond the stated uncertainty — None when either side
        is missing (a mismatch is never fabricated)."""
        if (
            self.lip_reported_offset_seconds is None
            or self.mean_offset_seconds is None
        ):
            return None
        tolerance = self.uncertainty_seconds or 0.0
        return (
            abs(self.lip_reported_offset_seconds - self.mean_offset_seconds)
            > tolerance
        )

    @model_validator(mode='after')
    def _check(self) -> 'AVLatencyPathMeasurement':
        _require_iso8601(self.measured_at_utc, 'measured_at_utc')
        if self.method == 'lip_protocol_reported':
            if self.observations or self.mean_offset_seconds is not None:
                raise ValueError(
                    'a LIP protocol report cannot carry measured '
                    'observations — reported metadata is not a physical '
                    'measurement'
                )
            if self.lip_evidence_ref is None:
                raise ValueError(
                    'lip_protocol_reported requires the bound LIP '
                    'evidence record'
                )
        else:
            if self.lip_reported_offset_seconds is not None and (
                self.lip_evidence_ref is None
            ):
                raise ValueError(
                    'a LIP-reported offset requires its evidence ref'
                )
        if self.observations:
            offsets = [o.offset_seconds for o in self.observations]
            mean = sum(offsets) / len(offsets)
            for name, expected in (
                ('mean_offset_seconds', mean),
                ('min_offset_seconds', min(offsets)),
                ('max_offset_seconds', max(offsets)),
            ):
                actual = getattr(self, name)
                if actual is None or abs(actual - expected) > 1e-9:
                    raise ValueError(
                        f'{name} must equal the observation distribution'
                    )
        if self.method != 'manual_estimate' and not self.observations:
            raise ValueError(
                'a measurement method requires at least one observation '
                '— an empty record cannot anchor a sync claim'
            )
        expected = _hash(self.identity_payload())
        if self.measurement_sha256 != expected:
            raise ValueError('av sync measurement hash mismatch')
        if self.measurement_id != f'avlatm:{expected}':
            raise ValueError('av sync measurement id mismatch')
        return self


def build_av_latency_path_measurement(**kwargs: Any) -> AVLatencyPathMeasurement:
    probe = AVLatencyPathMeasurement.model_construct(
        **canonicalize_payload(
            AVLatencyPathMeasurement,
            dict(
                schema_version=1,
                authority_version=AV_LATENCY_MEASUREMENT_AUTHORITY_VERSION,
                measurement_id='',
                measurement_sha256='0' * 64,
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return AVLatencyPathMeasurement(
        **probe.model_dump(
            mode='python',
            exclude={'measurement_id', 'measurement_sha256'},
        ),
        measurement_id=f'avlatm:{sha}',
        measurement_sha256=sha,
    )


# ---------------------------------------------------------------------------
# Qualification (closed loop)
# ---------------------------------------------------------------------------


class AVLatencyQualification(BaseModel):
    """Sealed verdict of one closed-loop sync qualification (#582 §14).

    Produced only by :func:`evaluate_av_latency_path` — the verdict binds the
    exact path, measurement and profile hashes so a stale chain can
    never be re-presented as current.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    authority_version: Literal[
        'av-latency-qualification-1'
    ] = AV_LATENCY_QUALIFICATION_AUTHORITY_VERSION
    qualification_id: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    path_id: str = Field(min_length=1)
    path_version: str = Field(min_length=1)
    path_sha256: str = Field(pattern=_SHA256)
    measurement_id: str | None = None
    measurement_sha256: str | None = Field(default=None, pattern=_SHA256)
    profile_id: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=_SHA256)
    evaluation_version: str = Field(min_length=1)
    verdict: AVLatencyVerdict
    residual_offset_seconds: float | None = None
    """Measured offset net of applied compensation — the figure the
    profile bounds are applied to."""
    reasons: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'qualification_id', 'qualification_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'AVLatencyQualification':
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        if (
            self.measurement_id is None
        ) != (self.measurement_sha256 is None):
            raise ValueError(
                'measurement id/sha256 must be supplied together or not '
                'at all'
            )
        if self.verdict == 'qualified_within_profile' and (
            self.residual_offset_seconds is None
            or self.measurement_id is None
        ):
            raise ValueError(
                'a qualified verdict requires a bound measurement and a '
                'residual offset'
            )
        if self.verdict == 'stale' and self.measurement_id is None:
            raise ValueError(
                'a stale verdict requires the measurement it supersedes'
            )
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('av sync qualification hash mismatch')
        if self.qualification_id != f'avlatq:{expected}':
            raise ValueError('av sync qualification id mismatch')
        return self


def evaluate_av_latency_path(
    *,
    path: AVLatencyPath,
    profile: AVLatencyProfile,
    measurement: AVLatencyPathMeasurement | None,
    evaluated_at_utc: str | None = None,
) -> AVLatencyQualification:
    """Closed-loop verdict for one path/profile/measurement triple.

    Fail-closed ordering (#582 §14):

    1. no measurement bound to this path -> ``insufficient_evidence``;
    2. the measurement's pinned path sha differs -> ``stale`` (a
       route/mode/filter change never reuses the old result);
    3. the measured residual offset is missing (no physical evidence, or
       the profile demands physical measurement and the method was a
       manual estimate / LIP report) -> ``insufficient_evidence``;
    4. residual inside ``acceptability_*`` -> ``qualified_within_profile``,
       else ``exceeds_profile_bounds``.
    """
    reasons: list[str] = []
    verdict: AVLatencyVerdict
    residual: float | None = None

    if measurement is None:
        verdict = 'insufficient_evidence'
        reasons.append('no sync measurement is bound to this path')
    elif measurement.path_sha256 != path.path_sha256 or (
        measurement.path_id != path.path_id
        or measurement.path_version != path.version
    ):
        verdict = 'stale'
        reasons.append(
            'the measurement predates the current path revision — '
            'route/mode/filter changes stale prior sync evidence'
        )
        residual = measurement.mean_offset_seconds
    else:
        residual = measurement.mean_offset_seconds
        if residual is not None:
            # Applied compensation reduces the *audio-late* side: each
            # master audio delay entry moves the acoustic event later.
            residual = residual + path.applied_compensation_seconds
        measurement_evidence_ok = (
            measurement.method in ('electrical_measurement',
                                   'acoustic_optical_measurement')
            or not profile.requires_physical_measurement
        )
        if residual is None:
            verdict = 'insufficient_evidence'
            reasons.append(
                'no measured end-to-end offset — reported/nominal stage '
                'figures never mint a lip-sync verdict'
            )
        elif not measurement_evidence_ok:
            verdict = 'insufficient_evidence'
            reasons.append(
                f'profile {profile.label!r} requires physical '
                f'measurement; method {measurement.method!r} is '
                'insufficient evidence'
            )
        elif (
            profile.acceptability_min_seconds - 1e-9
            <= residual
            <= profile.acceptability_max_seconds + 1e-9
        ):
            verdict = 'qualified_within_profile'
            reasons.append(
                'residual offset inside profile acceptability bounds'
            )
        else:
            verdict = 'exceeds_profile_bounds'
            reasons.append(
                'residual offset outside profile acceptability bounds'
            )
        if measurement.protocol_physical_mismatch:
            reasons.append(
                'protocol-reported (LIP) and measured offsets disagree '
                'beyond stated uncertainty — keep both, diagnose the '
                'mismatch'
            )

    probe = AVLatencyQualification.model_construct(
        **canonicalize_payload(
            AVLatencyQualification,
            dict(
                schema_version=1,
                authority_version=AV_LATENCY_QUALIFICATION_AUTHORITY_VERSION,
                qualification_id='',
                qualification_sha256='0' * 64,
                document_id=path.document_id,
                path_id=path.path_id,
                path_version=path.version,
                path_sha256=path.path_sha256,
                measurement_id=(
                    measurement.measurement_id if measurement else None
                ),
                measurement_sha256=(
                    measurement.measurement_sha256 if measurement else None
                ),
                profile_id=profile.profile_id,
                profile_sha256=profile.profile_sha256,
                evaluation_version=AV_LATENCY_EVALUATION_VERSION,
                verdict=verdict,
                residual_offset_seconds=residual,
                reasons=tuple(reasons),
                evaluated_at_utc=evaluated_at_utc or _utc_now(),
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return AVLatencyQualification(
        **probe.model_dump(
            mode='python',
            exclude={'qualification_id', 'qualification_sha256'},
        ),
        qualification_id=f'avlatq:{sha}',
        qualification_sha256=sha,
    )


__all__ = [
    'AVLatencyPath',
    'AVLatencyStage',
    'AVLatencyCompensationEntry',
    'AVLatencyPathMeasurement',
    'AVLatencyMeasurementMethod',
    'AVLatencyObservation',
    'AVLatencyProfile',
    'AVLatencyProfileKind',
    'AVLatencyQualification',
    'AVLatencySignConvention',
    'AVLatencyVerdict',
    'CommonClockState',
    'CompensationLocation',
    'LatencyComponentKind',
    'LatencyEvidenceClass',
    'AV_LATENCY_PATH_AUTHORITY_VERSION',
    'AV_LATENCY_EVALUATION_VERSION',
    'AV_LATENCY_MEASUREMENT_AUTHORITY_VERSION',
    'AV_LATENCY_PROFILE_AUTHORITY_VERSION',
    'AV_LATENCY_QUALIFICATION_AUTHORITY_VERSION',
    'build_atsc_is191_profile',
    'build_av_latency_path',
    'build_av_latency_path_measurement',
    'build_av_latency_profile',
    'build_itu_bt1359_profile',
    'evaluate_av_latency_path',
]
