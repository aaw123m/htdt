"""Media-playback capability qualification authority (#632, REV57-AUD).

An HDMI path can be fully capable while the source device / application /
player cannot decode, select, continuously render, or correctly fall back
from the intended media profile. Media playback (can the source/app/device
consume the content?) is a distinct layer from HDMI transport (#583 — can
the output signal traverse the link?) and from renderer semantics (#603 —
what did the processor do with it?). This module owns:

- :class:`CadPlaybackStackIdentity` — the exact implementation identity:
  device/hardware revision, OS/platform version, app/player name+version,
  playback engine, firmware, license state, source mode and config. A
  device model alone is never a playback implementation identity; an app
  update can change capability without any HDMI change (#632 §1/§17).
- :class:`CadMediaProfileRequirement` — the exact content/media profile:
  container, delivery class, video codec/profile/level, resolution/frame
  rate, bit depth/chroma, colorimetry/transfer/HDR profile, audio
  codec/profile/layout, subtitle track, encryption requirement and
  representation. ``'4K HDR Atmos'`` as a bare label is never enough
  (#632 §2).
- :class:`CadWaveProfileBinding` — an optional, versioned CTA WAVE
  profile binding (CTA-5003-C device playback spec, CTA-5001-F content
  spec, test-suite revision/test-content identity). WAVE applies only to
  segmented/streaming contexts — it is never a universal home-theater
  playback standard (#632 §4).
- :class:`CadPlaybackCapabilityRecord` — one (stack × media profile)
  capability observation: API-reported and application-declared states
  stay separate from empirically tested results and from the observed
  output profile. Fallback/downmix/SDR states are explicit verdicts, not
  hidden under "playback succeeded" (#632 §3/§7/§11).
- :class:`CadPlaybackOperationRun` — per-operation evidence (initial
  start / continuous / seek / pause-resume / representation switch /
  period transition / track change / language change / subtitle change /
  background-resume / standby-wake) with startup-time, stall, drop and
  error observations bound to scenario + measurement source (#632 §5/§6).
- :class:`CadPlaybackQualification` +
  :func:`evaluate_playback_capability` — the fail-closed verdict ladder:
  qualified exact profile / qualified with fallback / qualified with
  limitations / player unsupported / output profile mismatch / transport
  dependency failed / intermittent / insufficient evidence (#632 §19).

Honesty rules baked in:

- DRM/encryption is *observation-only*: this module records protection
  requirements, license state and interop results — it can never bypass
  DRM, extract or decrypt protected content, circumvent HDCP or defeat
  application authentication (#632 §13).
- API-reported capability is planning evidence only — a positive codec
  query never substitutes for empirical playback (#632 §11).
- A player that decodes HDR but outputs SDR, or selects stereo when the
  immersive track exists, is reported as a fallback — never as the
  requested profile rendered (#632 §7/§9/§10).
- Network confounds are separated: a demonstrably broken #591 transport
  makes the verdict ``transport_dependency_failed`` rather than a false
  ``player_unsupported`` (#632 §12).
- App/OS/firmware/provider changes stale prior qualifications — the
  qualification is bound to the exact stack fingerprint, never to a
  device model (#632 §17).
- There is no global ``supports 4K/HDR/Atmos`` badge (#632 §19).

Literature basis
----------------
- CTA-5003-C (Device Playback Capabilities Specification, June 2026) —
  capability detection, buffering/playback behavior, encryption
  interoperability, random access — WAVE/segmented-streaming scope only.
- CTA-5001-F (WAVE Content Specification, May 2025) — content profile
  identity.
- CTA WAVE Streaming Media Device Test Suite — reproducible known-content
  test pattern; beta/less-validated test status is preserved, never
  promoted to universal conformance authority.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


MEDIA_PLAYBACK_SCHEMA_VERSION = 'aud-media-playback-1'
PLAYBACK_EVALUATION_VERSION = 'aud-playback-eval-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _require_finite(value: float, label: str) -> None:
    if not isfinite(float(value)):
        raise ValueError(f'{label} must be finite')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


# ---------------------------------------------------------------------------
# Taxonomies (#632)
# ---------------------------------------------------------------------------

PlaybackSourceClass = Literal[
    'streaming_app',
    'local_file_player',
    'disc_player',
    'game_console',
    'set_top_box',
    'other',
]
"""#632 §15 — source classes. CTA WAVE profiles apply only to
``streaming_app``; a disc/game/local source is representable but is never
judged by WAVE rules."""

MediaDeliveryClass = Literal[
    'segmented_adaptive',
    'local_file',
    'disc',
    'broadcast',
    'network_stream',
    'other',
    'unknown',
]

PlaybackCapabilityClass = Literal[
    'declared_by_device_api',
    'declared_by_application',
    'tested_playable',
    'tested_with_limitations',
    'tested_fallback',
    'unsupported',
    'unknown',
]
"""#632 §3 — capability classes. Declared/API states are planning
evidence; ``tested_*`` states are empirical; neither silently upgrades."""

PlaybackEvidenceClass = Literal[
    'api_reported',
    'app_declared',
    'empirically_tested',
    'observed_output',
]
"""#632 §11 — evidence classes stay separate: an API report is never an
empirical result, and an observed output profile is its own class."""

PlaybackOperation = Literal[
    'initial_start',
    'continuous_playback',
    'random_access_seek',
    'pause_resume',
    'representation_switch',
    'period_transition',
    'track_change',
    'audio_language_change',
    'subtitle_change',
    'app_background_resume',
    'standby_wake',
]
"""#632 §5 — a player may start content but fail seek/switch/resume;
qualification never reduces to 'first frame rendered'."""

OperationResult = Literal['passed', 'failed', 'untested']

ObservedOutputState = Literal[
    'requested_profile_rendered',
    'lower_video_representation',
    'sdr_fallback',
    'lower_bit_depth_chroma',
    'audio_codec_fallback',
    'multichannel_to_stereo_downmix',
    'object_to_bed_fallback',
    'transcoded',
    'unexpected_output',
    'unknown_output',
]
"""#632 §7 — what actually reached the output. Fallback is not failure,
but it is always visible."""

FailureAttribution = Literal[
    'decoder_player_failure',
    'network_delivery_failure',
    'buffering_due_to_transport',
    'not_applicable',
    'unknown',
]
"""#632 §12 — playback-vs-transport attribution: a broken network never
falsely marks a player unsupported."""

PlaybackVerdict = Literal[
    'qualified_exact_profile',
    'qualified_with_fallback',
    'qualified_with_limitations',
    'player_unsupported',
    'output_profile_mismatch',
    'transport_dependency_failed',
    'intermittent',
    'insufficient_evidence',
]
"""#632 §19 — verdict ladder. No global capability flag exists."""

EncryptionRequirement = Literal['required', 'none', 'unknown']
"""#632 §13 — content-protection requirement is *recorded*; HTDT never
bypasses, decrypts or circumvents it."""


# ---------------------------------------------------------------------------
# Embedded models
# ---------------------------------------------------------------------------


class CadWaveProfileBinding(BaseModel):
    """Optional CTA WAVE external profile pin (#632 §4/§14).

    WAVE is a segmented-streaming profile family — never a universal
    home-theater playback rule. Test maturity stays recorded (the public
    suite includes beta/less-validated audio-codec tests that are not
    universal conformance authority).
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    device_spec_revision: str | None = Field(default=None, min_length=1)
    """CTA-5003 revision label, e.g. 'CTA-5003-C' — exact, never 'latest'."""
    content_spec_revision: str | None = Field(default=None, min_length=1)
    """CTA-5001 revision label, e.g. 'CTA-5001-F'."""
    wave_profile: str | None = Field(default=None, min_length=1)
    """WAVE device/core/extension/media profile identity."""
    test_suite_revision: str | None = Field(default=None, min_length=1)
    test_content_identity: str | None = Field(default=None, min_length=1)
    test_maturity: Literal[
        'validated', 'beta', 'less_validated', 'unknown'
    ] = 'unknown'
    """#632 §14 — beta tests never claim universal conformance."""

    @model_validator(mode='after')
    def _check(self) -> 'CadWaveProfileBinding':
        return self


class CadOperationResultRow(BaseModel):
    """One tested playback operation and its result (#632 §5)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    operation: PlaybackOperation
    result: OperationResult
    detail: str | None = Field(default=None, min_length=1)


class CadQualityObservation(BaseModel):
    """One playback-quality observation (#632 §6).

    Bound to an exact scenario + interval + measurement source — a
    stall/drop/error count is evidence only with its observation window,
    and an unmeasured quantity stays ``None`` (never fabricated as zero).
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    startup_time_s: float | None = Field(default=None, ge=0.0)
    stall_count: int | None = Field(default=None, ge=0)
    dropped_frame_count: int | None = Field(default=None, ge=0)
    repeated_frame_count: int | None = Field(default=None, ge=0)
    decode_error_count: int | None = Field(default=None, ge=0)
    representation_changes: int | None = Field(default=None, ge=0)
    av_discontinuity_count: int | None = Field(default=None, ge=0)
    error_code: str | None = Field(default=None, min_length=1)
    scenario: str = Field(min_length=1)
    interval_s: float | None = Field(default=None, gt=0.0)
    measurement_source: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'CadQualityObservation':
        for value, label in (
            (self.startup_time_s, 'startup_time_s'),
            (self.interval_s, 'interval_s'),
        ):
            if value is not None:
                _require_finite(value, label)
        return self


class CadPlaybackObservation(BaseModel):
    """One observed output/fallback fact (#632 §7/§9/§10)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    output_state: ObservedOutputState
    requested_profile_label: str | None = Field(default=None, min_length=1)
    observed_profile_label: str | None = Field(default=None, min_length=1)
    selected_audio_track: str | None = Field(default=None, min_length=1)
    bitstream_mode: Literal[
        'bitstream', 'decoded_pcm', 'provider_managed', 'unknown'
    ] = 'unknown'
    hdr_output_state: Literal[
        'hdr_rendered', 'sdr_output', 'unknown'
    ] = 'unknown'
    note: str | None = Field(default=None, min_length=1)


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{
            sha_field: digest,
            id_field: _semantic_id(prefix, digest),
        },
    )


class CadPlaybackStackIdentity(BaseModel):
    """The exact playback-implementation identity (#632 §1).

    Every versioned component is a separate field — an app or firmware
    update changes the fingerprint and stales prior qualifications even
    when the HDMI hardware is unchanged.
    """

    model_config = ConfigDict(frozen=True)

    stack_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    source_class: PlaybackSourceClass
    device_identity: str = Field(min_length=1)
    hardware_revision: str | None = Field(default=None, min_length=1)
    os_version: str | None = Field(default=None, min_length=1)
    app_name: str | None = Field(default=None, min_length=1)
    app_version: str | None = Field(default=None, min_length=1)
    playback_engine: str | None = Field(default=None, min_length=1)
    firmware: str | None = Field(default=None, min_length=1)
    license_state: str | None = Field(default=None, min_length=1)
    """e.g. 'signed_in_premium', 'free_tier' — account/license where
    relevant, never a credential."""
    source_mode: Literal['network', 'offline', 'unknown'] = 'unknown'
    player_config: str | None = Field(default=None, min_length=1)
    content_identity: str | None = Field(default=None, min_length=1)
    authority_version: str = Field(
        default=MEDIA_PLAYBACK_SCHEMA_VERSION, min_length=1
    )
    observed_at_utc: str = Field(min_length=1)
    stack_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'source_class': self.source_class,
            'device_identity': self.device_identity,
            'hardware_revision': self.hardware_revision,
            'os_version': self.os_version,
            'app_name': self.app_name,
            'app_version': self.app_version,
            'playback_engine': self.playback_engine,
            'firmware': self.firmware,
            'license_state': self.license_state,
            'source_mode': self.source_mode,
            'player_config': self.player_config,
            'content_identity': self.content_identity,
            'authority_version': self.authority_version,
            'observed_at_utc': self.observed_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'CadPlaybackStackIdentity':
        _require_iso8601(self.observed_at_utc, 'stack observed_at_utc')
        expected = _hash(self.identity_payload())
        if self.stack_sha256 != expected:
            raise ValueError('playback stack hash mismatch')
        if self.stack_id != _semantic_id('pbstack', expected):
            raise ValueError('playback stack id does not match its hash')
        return self


def playback_stack_binding(
    stack: CadPlaybackStackIdentity,
) -> AuthorityRef:
    return AuthorityRef(
        kind='playback_stack_identity',
        ref_id=stack.stack_id,
        ref_sha256=stack.stack_sha256,
    )


class CadMediaProfileRequirement(BaseModel):
    """The exact media profile a playback test targets (#632 §2).

    Codec/profile/level, resolution/frame rate, bit depth/chroma,
    colorimetry/HDR profile, audio codec/layout and the encryption
    requirement are all separate fields — a label like '4K HDR Atmos'
    is never a substitute.
    """

    model_config = ConfigDict(frozen=True)

    requirement_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    container: str | None = Field(default=None, min_length=1)
    delivery_class: MediaDeliveryClass = 'unknown'
    video_codec: str | None = Field(default=None, min_length=1)
    video_profile: str | None = Field(default=None, min_length=1)
    video_level: str | None = Field(default=None, min_length=1)
    resolution: str | None = Field(default=None, min_length=1)
    frame_rate_hz: float | None = Field(default=None, gt=0.0)
    bit_depth: int | None = Field(default=None, ge=8)
    chroma: str | None = Field(default=None, min_length=1)
    colorimetry: str | None = Field(default=None, min_length=1)
    hdr_metadata_profile: str | None = Field(default=None, min_length=1)
    audio_codec: str | None = Field(default=None, min_length=1)
    audio_profile: str | None = Field(default=None, min_length=1)
    audio_layout: str | None = Field(default=None, min_length=1)
    """e.g. '5.1', '7.1.4', 'object_channels' — the layout identity, not
    a verdict."""
    audio_sample_rate_hz: int | None = Field(default=None, gt=0)
    audio_bit_depth: int | None = Field(default=None, ge=8)
    subtitle_track: str | None = Field(default=None, min_length=1)
    encryption_requirement: EncryptionRequirement = 'unknown'
    """Whether the content requires protected playback — recorded, never
    bypassed/decrypted/circumvented (#632 §13)."""
    representation_id: str | None = Field(default=None, min_length=1)
    asset_ref: AuthorityRef | None = None
    """#608 test-asset pin for lawful/known test content."""
    wave_profile: CadWaveProfileBinding | None = None
    authority_version: str = Field(
        default=MEDIA_PLAYBACK_SCHEMA_VERSION, min_length=1
    )
    declared_at_utc: str = Field(min_length=1)
    requirement_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'label': self.label,
            'container': self.container,
            'delivery_class': self.delivery_class,
            'video_codec': self.video_codec,
            'video_profile': self.video_profile,
            'video_level': self.video_level,
            'resolution': self.resolution,
            'frame_rate_hz': self.frame_rate_hz,
            'bit_depth': self.bit_depth,
            'chroma': self.chroma,
            'colorimetry': self.colorimetry,
            'hdr_metadata_profile': self.hdr_metadata_profile,
            'audio_codec': self.audio_codec,
            'audio_profile': self.audio_profile,
            'audio_layout': self.audio_layout,
            'audio_sample_rate_hz': self.audio_sample_rate_hz,
            'audio_bit_depth': self.audio_bit_depth,
            'subtitle_track': self.subtitle_track,
            'encryption_requirement': self.encryption_requirement,
            'representation_id': self.representation_id,
            'asset_ref': (
                self.asset_ref.model_dump(mode='json')
                if self.asset_ref is not None
                else None
            ),
            'wave_profile': (
                self.wave_profile.model_dump(mode='json')
                if self.wave_profile is not None
                else None
            ),
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'CadMediaProfileRequirement':
        _require_iso8601(self.declared_at_utc, 'requirement declared_at_utc')
        if self.frame_rate_hz is not None:
            _require_finite(self.frame_rate_hz, 'frame_rate_hz')
        if self.wave_profile is not None and (
            self.delivery_class != 'segmented_adaptive'
        ):
            raise ValueError(
                'a WAVE profile binding applies only to segmented '
                'streaming delivery — it is not a universal playback '
                'rule for discs/files/game sources (#632 §4)'
            )
        if self.asset_ref is not None and (
            self.asset_ref.ref_sha256 is None
        ):
            raise ValueError('asset ref must pin its sha256')
        expected = _hash(self.identity_payload())
        if self.requirement_sha256 != expected:
            raise ValueError('media requirement hash mismatch')
        if self.requirement_id != _semantic_id('pbmedia', expected):
            raise ValueError('media requirement id does not match its hash')
        return self


def media_requirement_binding(
    requirement: CadMediaProfileRequirement,
) -> AuthorityRef:
    return AuthorityRef(
        kind='media_profile_requirement',
        ref_id=requirement.requirement_id,
        ref_sha256=requirement.requirement_sha256,
    )


class CadPlaybackCapabilityRecord(BaseModel):
    """One (stack × media-profile) capability observation (#632 §3/§7).

    The capability class, evidence class and observed output are separate
    fields: an API-reported codec capability is not an empirical result,
    and a successful decode is not the requested profile rendered.
    """

    model_config = ConfigDict(frozen=True)

    record_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    stack_ref: AuthorityRef
    media_ref: AuthorityRef
    capability_class: PlaybackCapabilityClass
    evidence_class: PlaybackEvidenceClass
    observation: CadPlaybackObservation | None = None
    operations: tuple[CadOperationResultRow, ...] = ()
    quality: CadQualityObservation | None = None
    failure_attribution: FailureAttribution = 'not_applicable'
    render_session_ref: AuthorityRef | None = None
    """#603 pin — the renderer verdict for this playback (downmix/fallback
    handled there stays separate)."""
    network_path_ref: AuthorityRef | None = None
    """#591 pin — the transport condition for streaming tests."""
    hdmi_path_ref: AuthorityRef | None = None
    """#583 pin — the HDMI transport condition."""
    note: str | None = Field(default=None, min_length=1)
    authority_version: str = Field(
        default=MEDIA_PLAYBACK_SCHEMA_VERSION, min_length=1
    )
    observed_at_utc: str = Field(min_length=1)
    record_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'stack_ref': self.stack_ref.model_dump(mode='json'),
            'media_ref': self.media_ref.model_dump(mode='json'),
            'capability_class': self.capability_class,
            'evidence_class': self.evidence_class,
            'observation': (
                self.observation.model_dump(mode='json')
                if self.observation is not None
                else None
            ),
            'operations': [
                row.model_dump(mode='json') for row in self.operations
            ],
            'quality': (
                self.quality.model_dump(mode='json')
                if self.quality is not None
                else None
            ),
            'failure_attribution': self.failure_attribution,
            'render_session_ref': (
                self.render_session_ref.model_dump(mode='json')
                if self.render_session_ref is not None
                else None
            ),
            'network_path_ref': (
                self.network_path_ref.model_dump(mode='json')
                if self.network_path_ref is not None
                else None
            ),
            'hdmi_path_ref': (
                self.hdmi_path_ref.model_dump(mode='json')
                if self.hdmi_path_ref is not None
                else None
            ),
            'note': self.note,
            'authority_version': self.authority_version,
            'observed_at_utc': self.observed_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'CadPlaybackCapabilityRecord':
        _require_iso8601(self.observed_at_utc, 'record observed_at_utc')
        if self.stack_ref.kind != 'playback_stack_identity':
            raise ValueError(
                "stack_ref must pin a 'playback_stack_identity' authority"
            )
        if self.media_ref.kind != 'media_profile_requirement':
            raise ValueError(
                "media_ref must pin a 'media_profile_requirement' authority"
            )
        for ref, label in (
            (self.stack_ref, 'stack_ref'),
            (self.media_ref, 'media_ref'),
            (self.render_session_ref, 'render_session_ref'),
            (self.network_path_ref, 'network_path_ref'),
            (self.hdmi_path_ref, 'hdmi_path_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        if self.render_session_ref is not None and (
            self.render_session_ref.kind != 'render_session'
        ):
            raise ValueError(
                "render_session_ref must pin a 'render_session' authority"
            )
        operation_names = [row.operation for row in self.operations]
        if len(operation_names) != len(set(operation_names)):
            raise ValueError('each operation may appear only once')
        # A 'tested_*' capability claim requires empirical evidence —
        # an API declaration can never wear a tested class (#632 §11).
        if self.capability_class.startswith('tested_') and (
            self.evidence_class in {'api_reported', 'app_declared'}
        ):
            raise ValueError(
                'a tested capability requires empirically_tested or '
                'observed_output evidence — API/declaration evidence '
                'cannot carry it'
            )
        if self.capability_class == 'unsupported' and (
            self.failure_attribution == 'unknown'
        ):
            raise ValueError(
                'an unsupported verdict must name a failure attribution — '
                'an unattributed failure reads as decoder fault and hides '
                'a possible network confound'
            )
        if (
            self.observation is not None
            and self.observation.output_state != 'requested_profile_rendered'
            and self.capability_class == 'tested_playable'
        ):
            raise ValueError(
                "'tested_playable' requires observed output matching the "
                'requested profile — fallbacks use tested_fallback/'
                'tested_with_limitations'
            )
        expected = _hash(self.identity_payload())
        if self.record_sha256 != expected:
            raise ValueError('capability record hash mismatch')
        if self.record_id != _semantic_id('pbcap', expected):
            raise ValueError('capability record id does not match its hash')
        return self


def capability_record_binding(
    record: CadPlaybackCapabilityRecord,
) -> AuthorityRef:
    return AuthorityRef(
        kind='playback_capability_record',
        ref_id=record.record_id,
        ref_sha256=record.record_sha256,
    )


class CadPlaybackOperationRun(BaseModel):
    """One scenario-bounded operation test (#632 §5/§6/§16).

    A run binds the tested operation set, its results and its quality
    observations to the exact stack + media profile and to the scenario
    window it ran under (e.g. a long-duration soak, a seek sweep, a
    standby-wake sequence).
    """

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    stack_ref: AuthorityRef
    media_ref: AuthorityRef
    scenario: str = Field(min_length=1)
    """Scenario identity — e.g. 'initial_start', 'long_duration_soak',
    'seek_sweep', 'standby_wake_cycle'."""
    duration_s: float | None = Field(default=None, gt=0.0)
    operations: tuple[CadOperationResultRow, ...] = Field(min_length=1)
    quality: CadQualityObservation | None = None
    network_path_ref: AuthorityRef | None = None
    authority_version: str = Field(
        default=MEDIA_PLAYBACK_SCHEMA_VERSION, min_length=1
    )
    started_at_utc: str = Field(min_length=1)
    run_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'stack_ref': self.stack_ref.model_dump(mode='json'),
            'media_ref': self.media_ref.model_dump(mode='json'),
            'scenario': self.scenario,
            'duration_s': self.duration_s,
            'operations': [
                row.model_dump(mode='json') for row in self.operations
            ],
            'quality': (
                self.quality.model_dump(mode='json')
                if self.quality is not None
                else None
            ),
            'network_path_ref': (
                self.network_path_ref.model_dump(mode='json')
                if self.network_path_ref is not None
                else None
            ),
            'authority_version': self.authority_version,
            'started_at_utc': self.started_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'CadPlaybackOperationRun':
        _require_iso8601(self.started_at_utc, 'run started_at_utc')
        if self.duration_s is not None:
            _require_finite(self.duration_s, 'duration_s')
        if self.stack_ref.kind != 'playback_stack_identity':
            raise ValueError(
                "stack_ref must pin a 'playback_stack_identity' authority"
            )
        if self.media_ref.kind != 'media_profile_requirement':
            raise ValueError(
                "media_ref must pin a 'media_profile_requirement' authority"
            )
        for ref, label in (
            (self.stack_ref, 'stack_ref'),
            (self.media_ref, 'media_ref'),
            (self.network_path_ref, 'network_path_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        operation_names = [row.operation for row in self.operations]
        if len(operation_names) != len(set(operation_names)):
            raise ValueError('each operation may appear only once per run')
        expected = _hash(self.identity_payload())
        if self.run_sha256 != expected:
            raise ValueError('operation run hash mismatch')
        if self.run_id != _semantic_id('pbrun', expected):
            raise ValueError('operation run id does not match its hash')
        return self


def operation_run_binding(
    run: CadPlaybackOperationRun,
) -> AuthorityRef:
    return AuthorityRef(
        kind='playback_operation_run',
        ref_id=run.run_id,
        ref_sha256=run.run_sha256,
    )


# ---------------------------------------------------------------------------
# Qualification (#632 §19)
# ---------------------------------------------------------------------------


class CadPlaybackQualification(BaseModel):
    """Sealed playback verdict for one (stack × media) pair (#632 §19).

    Reports the exact requested and observed profiles, the fallback
    state, the failure attribution and staleness — never a global
    capability badge.
    """

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    stack_ref: AuthorityRef
    media_ref: AuthorityRef
    record_refs: tuple[AuthorityRef, ...] = ()
    run_refs: tuple[AuthorityRef, ...] = ()
    verdict: PlaybackVerdict
    requested_profile_summary: str | None = Field(
        default=None, min_length=1
    )
    observed_profile_summary: str | None = Field(
        default=None, min_length=1
    )
    fallback_state: ObservedOutputState | None = None
    failure_attribution: FailureAttribution = 'not_applicable'
    failed_operations: tuple[PlaybackOperation, ...] = ()
    stale: bool = False
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'stack_ref': self.stack_ref.model_dump(mode='json'),
            'media_ref': self.media_ref.model_dump(mode='json'),
            'record_refs': [
                ref.model_dump(mode='json') for ref in self.record_refs
            ],
            'run_refs': [
                ref.model_dump(mode='json') for ref in self.run_refs
            ],
            'verdict': self.verdict,
            'requested_profile_summary': self.requested_profile_summary,
            'observed_profile_summary': self.observed_profile_summary,
            'fallback_state': self.fallback_state,
            'failure_attribution': self.failure_attribution,
            'failed_operations': list(self.failed_operations),
            'stale': self.stale,
            'reasons': list(self.reasons),
            'limitations': list(self.limitations),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'CadPlaybackQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        for ref, label, kind in (
            (self.stack_ref, 'stack_ref', 'playback_stack_identity'),
            (self.media_ref, 'media_ref', 'media_profile_requirement'),
        ):
            if ref.kind != kind:
                raise ValueError(f'{label} must pin a {kind!r} authority')
            if ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        for ref in self.record_refs:
            if ref.kind != 'playback_capability_record':
                raise ValueError(
                    'record_refs must pin playback capability records'
                )
            if ref.ref_sha256 is None:
                raise ValueError('record refs must pin their sha256')
        for ref in self.run_refs:
            if ref.kind != 'playback_operation_run':
                raise ValueError(
                    'run_refs must pin playback operation runs'
                )
            if ref.ref_sha256 is None:
                raise ValueError('run refs must pin their sha256')
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('playback qualification hash mismatch')
        if self.qualification_id != _semantic_id('pbqual', expected):
            raise ValueError('qualification id does not match its hash')
        return self


def playback_qualification_binding(
    qualification: CadPlaybackQualification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='playback_qualification',
        ref_id=qualification.qualification_id,
        ref_sha256=qualification.qualification_sha256,
    )


def evaluate_playback_capability(
    *,
    document_id: str,
    stack: CadPlaybackStackIdentity,
    media: CadMediaProfileRequirement,
    records: Sequence[CadPlaybackCapabilityRecord] = (),
    runs: Sequence[CadPlaybackOperationRun] = (),
    stack_updated_since: bool = False,
    evaluated_at_utc: str | None = None,
) -> CadPlaybackQualification:
    """Fail-closed playback verdict (#632 §19).

    Combines every bound capability record and operation run for the
    exact stack + media pair. API declarations are kept below empirical
    evidence; a broken transport is reported as a transport dependency,
    never a false unsupported verdict; a stack update stales every prior
    qualification.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    reasons: list[str] = []
    limitations: list[str] = []

    bound_records = [
        record
        for record in records
        if record.stack_ref.ref_id == stack.stack_id
        and record.stack_ref.ref_sha256 == stack.stack_sha256
        and record.media_ref.ref_id == media.requirement_id
        and record.media_ref.ref_sha256 == media.requirement_sha256
    ]
    bound_runs = [
        run
        for run in runs
        if run.stack_ref.ref_id == stack.stack_id
        and run.stack_ref.ref_sha256 == stack.stack_sha256
        and run.media_ref.ref_id == media.requirement_id
        and run.media_ref.ref_sha256 == media.requirement_sha256
    ]
    if len(bound_records) != len(tuple(records)) or len(bound_runs) != len(
        tuple(runs)
    ):
        reasons.append(
            'records/runs bound to a different stack or media revision '
            'were ignored — a stack or profile change stales evidence'
        )

    stale = bool(stack_updated_since)
    if stale:
        reasons.append(
            'stack fingerprint changed (app/OS/firmware/provider) since '
            'the evidence was captured — prior qualification is stale'
        )

    failed_operations: list[PlaybackOperation] = []
    for run in bound_runs:
        for row in run.operations:
            if row.result == 'failed':
                failed_operations.append(row.operation)
        quality = run.quality
        if quality is not None and (
            quality.decode_error_count or quality.stall_count
        ):
            limitations.append(
                f'run {run.scenario}: stalls/errors observed'
            )

    declared_only = bound_records and all(
        record.evidence_class in {'api_reported', 'app_declared'}
        for record in bound_records
    )
    unsupported = [
        record for record in bound_records
        if record.capability_class == 'unsupported'
    ]
    transport_failures = [
        record for record in bound_records
        if record.failure_attribution in {
            'network_delivery_failure',
            'buffering_due_to_transport',
        }
    ]
    observed_outputs = [
        record.observation.output_state
        for record in bound_records
        if record.observation is not None
    ]
    intermittent = len({
        record.capability_class for record in bound_records
        if record.capability_class.startswith('tested_')
    }) > 1 and any(
        record.capability_class == 'tested_playable'
        for record in bound_records
    ) and any(
        record.capability_class != 'tested_playable'
        for record in bound_records
    )

    if stale:
        verdict: PlaybackVerdict = 'insufficient_evidence'
    elif unsupported and transport_failures:
        # A broken transport never falsifies the player as unsupported.
        verdict = 'transport_dependency_failed'
    elif unsupported:
        verdict = 'player_unsupported'
    elif transport_failures and not bound_runs and not bound_records:
        verdict = 'transport_dependency_failed'
    elif transport_failures:
        if all(
            record.capability_class == 'unsupported'
            for record in bound_records
        ):
            verdict = 'transport_dependency_failed'
        else:
            verdict = 'qualified_with_limitations'
            limitations.append(
                'network delivery failures were recorded during testing'
            )
    elif not bound_records and not bound_runs:
        verdict = 'insufficient_evidence'
        reasons.append('no bound capability evidence')
    elif declared_only:
        verdict = 'insufficient_evidence'
        reasons.append(
            'only API/app-declared capability exists — empirical '
            'playback evidence is required for a qualified verdict'
        )
    elif 'unexpected_output' in observed_outputs or (
        'unknown_output' in observed_outputs
    ):
        verdict = 'output_profile_mismatch'
        reasons.append('observed output was unexpected/unknown')
    elif any(
        state != 'requested_profile_rendered' for state in observed_outputs
    ):
        fallback_states = {
            'lower_video_representation',
            'sdr_fallback',
            'lower_bit_depth_chroma',
            'audio_codec_fallback',
            'multichannel_to_stereo_downmix',
            'object_to_bed_fallback',
            'transcoded',
        }
        verdict = 'qualified_with_fallback'
        reasons.append(
            'fallback output states: '
            + ' / '.join(
                sorted({s for s in observed_outputs if s in fallback_states})
            )
        )
    elif failed_operations:
        verdict = 'qualified_with_limitations'
        reasons.append(
            'failed operations: ' + ' / '.join(sorted(set(failed_operations)))
        )
    elif intermittent:
        verdict = 'intermittent'
    elif bound_records or bound_runs:
        verdict = 'qualified_exact_profile'
    else:
        verdict = 'insufficient_evidence'

    requested_summary = (
        f'{media.video_codec or "?"}/{media.resolution or "?"}'
        f'@{media.frame_rate_hz or "?"} '
        f'{media.hdr_metadata_profile or "sdr"} | '
        f'{media.audio_codec or "?"}/{media.audio_layout or "?"}'
    )
    observed_summary = None
    if observed_outputs:
        observed_summary = ' / '.join(sorted(set(observed_outputs)))
    fallback = next(
        (
            state for state in observed_outputs
            if state != 'requested_profile_rendered'
        ),
        None,
    )

    return _seal(
        CadPlaybackQualification,
        {
            'document_id': document_id,
            'stack_ref': playback_stack_binding(stack).model_dump(
                mode='json'
            ),
            'media_ref': media_requirement_binding(media).model_dump(
                mode='json'
            ),
            'record_refs': [
                capability_record_binding(record).model_dump(mode='json')
                for record in bound_records
            ],
            'run_refs': [
                operation_run_binding(run).model_dump(mode='json')
                for run in bound_runs
            ],
            'verdict': verdict,
            'requested_profile_summary': requested_summary,
            'observed_profile_summary': observed_summary,
            'fallback_state': fallback,
            'failure_attribution': (
                'network_delivery_failure'
                if transport_failures
                else (
                    bound_records[0].failure_attribution
                    if bound_records
                    else 'not_applicable'
                )
            ),
            'failed_operations': sorted(set(failed_operations)),
            'stale': stale,
            'reasons': reasons,
            'limitations': limitations,
            'evaluation_version': PLAYBACK_EVALUATION_VERSION,
            'evaluated_at_utc': evaluated_at_utc,
        },
        'qualification_id',
        'qualification_sha256',
        'pbqual',
    )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_playback_stack(
    *,
    document_id: str,
    source_class: PlaybackSourceClass,
    device_identity: str,
    hardware_revision: str | None = None,
    os_version: str | None = None,
    app_name: str | None = None,
    app_version: str | None = None,
    playback_engine: str | None = None,
    firmware: str | None = None,
    license_state: str | None = None,
    source_mode: Literal['network', 'offline', 'unknown'] = 'unknown',
    player_config: str | None = None,
    content_identity: str | None = None,
    observed_at_utc: str | None = None,
) -> CadPlaybackStackIdentity:
    observed_at_utc = observed_at_utc or _utc_now()
    return _seal(
        CadPlaybackStackIdentity,
        {
            'document_id': document_id,
            'source_class': source_class,
            'device_identity': device_identity,
            'hardware_revision': hardware_revision,
            'os_version': os_version,
            'app_name': app_name,
            'app_version': app_version,
            'playback_engine': playback_engine,
            'firmware': firmware,
            'license_state': license_state,
            'source_mode': source_mode,
            'player_config': player_config,
            'content_identity': content_identity,
            'authority_version': MEDIA_PLAYBACK_SCHEMA_VERSION,
            'observed_at_utc': observed_at_utc,
        },
        'stack_id',
        'stack_sha256',
        'pbstack',
    )


def build_media_requirement(
    *,
    document_id: str,
    label: str,
    container: str | None = None,
    delivery_class: MediaDeliveryClass = 'unknown',
    video_codec: str | None = None,
    video_profile: str | None = None,
    video_level: str | None = None,
    resolution: str | None = None,
    frame_rate_hz: float | None = None,
    bit_depth: int | None = None,
    chroma: str | None = None,
    colorimetry: str | None = None,
    hdr_metadata_profile: str | None = None,
    audio_codec: str | None = None,
    audio_profile: str | None = None,
    audio_layout: str | None = None,
    audio_sample_rate_hz: int | None = None,
    audio_bit_depth: int | None = None,
    subtitle_track: str | None = None,
    encryption_requirement: EncryptionRequirement = 'unknown',
    representation_id: str | None = None,
    asset_ref: AuthorityRef | None = None,
    wave_profile: CadWaveProfileBinding | None = None,
    declared_at_utc: str | None = None,
) -> CadMediaProfileRequirement:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        CadMediaProfileRequirement,
        {
            'document_id': document_id,
            'label': label,
            'container': container,
            'delivery_class': delivery_class,
            'video_codec': video_codec,
            'video_profile': video_profile,
            'video_level': video_level,
            'resolution': resolution,
            'frame_rate_hz': frame_rate_hz,
            'bit_depth': bit_depth,
            'chroma': chroma,
            'colorimetry': colorimetry,
            'hdr_metadata_profile': hdr_metadata_profile,
            'audio_codec': audio_codec,
            'audio_profile': audio_profile,
            'audio_layout': audio_layout,
            'audio_sample_rate_hz': audio_sample_rate_hz,
            'audio_bit_depth': audio_bit_depth,
            'subtitle_track': subtitle_track,
            'encryption_requirement': encryption_requirement,
            'representation_id': representation_id,
            'asset_ref': (
                asset_ref.model_dump(mode='json')
                if asset_ref is not None
                else None
            ),
            'wave_profile': (
                wave_profile.model_dump(mode='json')
                if wave_profile is not None
                else None
            ),
            'authority_version': MEDIA_PLAYBACK_SCHEMA_VERSION,
            'declared_at_utc': declared_at_utc,
        },
        'requirement_id',
        'requirement_sha256',
        'pbmedia',
    )


def build_capability_record(
    *,
    document_id: str,
    stack: CadPlaybackStackIdentity,
    media: CadMediaProfileRequirement,
    capability_class: PlaybackCapabilityClass,
    evidence_class: PlaybackEvidenceClass,
    observation: CadPlaybackObservation | None = None,
    operations: Sequence[CadOperationResultRow] = (),
    quality: CadQualityObservation | None = None,
    failure_attribution: FailureAttribution = 'not_applicable',
    render_session_ref: AuthorityRef | None = None,
    network_path_ref: AuthorityRef | None = None,
    hdmi_path_ref: AuthorityRef | None = None,
    note: str | None = None,
    observed_at_utc: str | None = None,
) -> CadPlaybackCapabilityRecord:
    observed_at_utc = observed_at_utc or _utc_now()
    return _seal(
        CadPlaybackCapabilityRecord,
        {
            'document_id': document_id,
            'stack_ref': playback_stack_binding(stack).model_dump(
                mode='json'
            ),
            'media_ref': media_requirement_binding(media).model_dump(
                mode='json'
            ),
            'capability_class': capability_class,
            'evidence_class': evidence_class,
            'observation': (
                observation.model_dump(mode='json')
                if observation is not None
                else None
            ),
            'operations': [
                row.model_dump(mode='json')
                if isinstance(row, CadOperationResultRow)
                else row
                for row in operations
            ],
            'quality': (
                quality.model_dump(mode='json')
                if quality is not None
                else None
            ),
            'failure_attribution': failure_attribution,
            'render_session_ref': (
                render_session_ref.model_dump(mode='json')
                if render_session_ref is not None
                else None
            ),
            'network_path_ref': (
                network_path_ref.model_dump(mode='json')
                if network_path_ref is not None
                else None
            ),
            'hdmi_path_ref': (
                hdmi_path_ref.model_dump(mode='json')
                if hdmi_path_ref is not None
                else None
            ),
            'note': note,
            'authority_version': MEDIA_PLAYBACK_SCHEMA_VERSION,
            'observed_at_utc': observed_at_utc,
        },
        'record_id',
        'record_sha256',
        'pbcap',
    )


def build_operation_run(
    *,
    document_id: str,
    stack: CadPlaybackStackIdentity,
    media: CadMediaProfileRequirement,
    scenario: str,
    operations: Sequence[CadOperationResultRow],
    duration_s: float | None = None,
    quality: CadQualityObservation | None = None,
    network_path_ref: AuthorityRef | None = None,
    started_at_utc: str | None = None,
) -> CadPlaybackOperationRun:
    started_at_utc = started_at_utc or _utc_now()
    return _seal(
        CadPlaybackOperationRun,
        {
            'document_id': document_id,
            'stack_ref': playback_stack_binding(stack).model_dump(
                mode='json'
            ),
            'media_ref': media_requirement_binding(media).model_dump(
                mode='json'
            ),
            'scenario': scenario,
            'duration_s': duration_s,
            'operations': [
                row.model_dump(mode='json')
                if isinstance(row, CadOperationResultRow)
                else row
                for row in operations
            ],
            'quality': (
                quality.model_dump(mode='json')
                if quality is not None
                else None
            ),
            'network_path_ref': (
                network_path_ref.model_dump(mode='json')
                if network_path_ref is not None
                else None
            ),
            'authority_version': MEDIA_PLAYBACK_SCHEMA_VERSION,
            'started_at_utc': started_at_utc,
        },
        'run_id',
        'run_sha256',
        'pbrun',
    )


__all__ = [
    'CadMediaProfileRequirement',
    'CadOperationResultRow',
    'CadPlaybackCapabilityRecord',
    'CadPlaybackObservation',
    'CadPlaybackOperationRun',
    'CadPlaybackQualification',
    'CadPlaybackStackIdentity',
    'CadQualityObservation',
    'CadWaveProfileBinding',
    'EncryptionRequirement',
    'FailureAttribution',
    'MEDIA_PLAYBACK_SCHEMA_VERSION',
    'MediaDeliveryClass',
    'ObservedOutputState',
    'OperationResult',
    'PLAYBACK_EVALUATION_VERSION',
    'PlaybackCapabilityClass',
    'PlaybackEvidenceClass',
    'PlaybackOperation',
    'PlaybackSourceClass',
    'PlaybackVerdict',
    'build_capability_record',
    'build_media_requirement',
    'build_operation_run',
    'build_playback_stack',
    'capability_record_binding',
    'evaluate_playback_capability',
    'media_requirement_binding',
    'operation_run_binding',
    'playback_qualification_binding',
    'playback_stack_binding',
]
