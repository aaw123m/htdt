"""Immersive-audio render-path qualification (issue #603).

A room can contain the intended number/positions of speakers while the
source/processor renders a given program into a materially different
channel/object output topology. This module versions the render-path
layer: content metadata, decoder/renderer capability, configured vs
installed speaker layout, transport format, and the actual rendered
output activity — each a distinct authority, never collapsed into an
'Atmos ready' flag.

Standards basis (see docs/reviews/rev56-infra.md): ITU-R BS.2051-3
(advanced sound systems; channel-, object- and scene-based
representations), ITU-R BS.2076-3 (2025, in force) Audio Definition
Model metadata authority, Dolby x.y.z home-layout guidance (the middle
figure is the LFE *channel* count, not the number of physical
subwoofers) and AURO-3D published home layouts. Proprietary renderers
are treated as black boxes — observed outputs are recorded, internals
are never claimed.

Contract properties:

- the four layouts stay distinct (:data:`LayoutKind`):
  ``physically_installed`` / ``processor_configured`` /
  ``content_native`` / ``rendered_output`` — no automatic equality
  between them;
- render provenance is explicit (:data:`RenderState`): native, upmixed,
  downmixed, virtualized, matrix-derived — the same speaker activity can
  have different provenance, and unobserved provenance stays UNKNOWN;
- the transport chain stays staged
  (source content → transported format → decoded/render state →
  physical output activity) — a source silently falling back to
  PCM/core is a ``fallback_transport`` verdict, not a native render;
- object/scene counts only exist when parsed from metadata — never
  guessed from listening (:class:`ImmersiveContentProfile` validators);
- the x.y.z middle figure is the LFE *channel*; physical subwoofer
  count and bass-managed redirected bass are separate fields — a
  7.1.4 label never encodes how many subs the room has (#603 §7);
- output activity proves use of an output, not the proprietary internal
  panning/render algorithm (#603 §5);
- verdict axes stay separate (:data:`RenderAxis`): format eligibility,
  layout geometry eligibility, output activity and acoustic performance
  never collapse into a generic 'compliant' boolean;
- fail-closed: missing capability evidence reads ``unknown``, never
  'probably native'.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_equipment import EquipmentDataProvenance
from .canonical_json import (
    canonical_sha256 as _digest,
    canonicalize_payload as _canon,
)
from .clock import utc_now_iso as _utc_now


_SHA256 = r'^[0-9a-f]{64}$'

RENDER_PATH_SCHEMA_VERSION = 1

CONTENT_AUTHORITY_VERSION = 'rev56-render-content-1'
CAPABILITY_AUTHORITY_VERSION = 'rev56-render-capability-1'
LAYOUT_AUTHORITY_VERSION = 'rev56-render-layout-1'
SESSION_AUTHORITY_VERSION = 'rev56-render-session-1'
OUTPUT_OBSERVATION_AUTHORITY_VERSION = 'rev56-render-observation-1'
RENDER_QUALIFICATION_AUTHORITY_VERSION = 'rev56-render-qualification-1'
RENDER_EVALUATION_VERSION = 'rev56-render-eval-1'


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _require_iso8601(value: str, label: str) -> None:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _finite(value: float, label: str) -> float:
    if not isfinite(value):
        raise ValueError(f'{label} must be finite')
    return value


# ---------------------------------------------------------------------------
# Taxonomies
# ---------------------------------------------------------------------------

ContentMetadataClass = Literal[
    'channel_bed',
    'object',
    'scene_hoa',
    'matrix_encoded',
    'mixed',
    'unparseable',
    'unknown',
]
"""Content representation class (#603 §1) per ITU-R BS.2051-3:
channel-, object- and scene-based are distinct — 'immersive logo' is
not a metadata class."""

ContainerTransport = Literal[
    'bitstream_hdmi',
    'pcm_multichannel',
    'compressed_bitstream',
    'pcm_file',
    'bw64_adm_file',
    'network_stream',
    'other',
    'unknown',
]

MetadataSource = Literal[
    'container_parser',
    'adm_parser',
    'sidecar_manifest',
    'device_readback',
    'test_fixture_spec',
    'user_declared',
    'listening_observation',
    'unknown',
]
"""Where content metadata came from (#603 §1/§18). ``listening_observation``
may describe what was heard but can never carry structural counts —
object/scene figures are parser/readback/fixture evidence only."""

_PARSER_SOURCES = frozenset({
    'container_parser', 'adm_parser', 'sidecar_manifest',
    'device_readback', 'test_fixture_spec',
})
"""Sources allowed to carry structural counts (objects, HOA order,
declared layouts)."""

LayoutKind = Literal[
    'physically_installed',
    'processor_configured',
    'content_native',
    'rendered_output',
]
"""The four layout authorities (#603 §3) — never equal by implication."""

LayoutEvidence = Literal[
    'physical_inventory',
    'processor_readback',
    'content_metadata',
    'observed_outputs',
    'user_declared',
    'unknown',
]
"""What made the layout claim. A configured layout can only claim what
was read back or declared, a content-native layout only what metadata
declares, and a rendered-output layout only what was observed."""

RenderState = Literal[
    'native_render',
    'upmixed',
    'downmixed',
    'virtualized',
    'matrix_derived',
    'fallback_transport',
    'unrendered',
    'unknown',
]
"""Render provenance (#603 §4) — the same active outputs can carry any
of these; the distinction requires evidence."""

DecoderMode = Literal[
    'auto',
    'direct',
    'native_decode',
    'stereo',
    'multichannel',
    'surround_upmixer',
    'auro',
    'virtualizer',
    'unknown',
]
"""Decoder/listening mode selection (#603 §12) — part of the device
state that makes two sessions over the same content distinct."""

UpmixerState = Literal['off', 'on', 'unknown']

CaptureMethod = Literal[
    'device_readback',
    'test_signal_asset',
    'electrical_per_output',
    'acoustic_per_output',
    'external_analyzer',
    'user_report',
    'unknown',
]
"""How output activity was observed (#603 §5). Activity is proof of
use, never proof of the internal algorithm."""

OutputActivity = Literal['active', 'inactive', 'unobserved']

LicenseState = Literal[
    'licensed', 'feature_limited', 'expired', 'unlicensed', 'unknown'
]

CapabilitySource = Literal[
    'manufacturer_document',
    'device_readback',
    'observed',
    'user_declared',
    'unknown',
]
"""Provenance of capability claims (#603 §2) — a family name is not
capability; firmware/license can change it."""

RenderAxis = Literal[
    'format_render_eligible',
    'layout_geometry_eligible',
    'output_channel_active',
    'acoustic_performance_deferred',
]
"""Independent verdict axes (#603 §14)."""

RenderAxisResult = Literal[
    'pass', 'fail', 'limited', 'unknown', 'not_applicable'
]

LayoutMatchState = Literal[
    'matches',
    'partial_unused_outputs',
    'configured_differs_from_installed',
    'content_mismatch',
    'unknown',
]

ProvenanceState = Literal[
    'native_confirmed',
    'upmixer_confirmed',
    'virtualized_confirmed',
    'downmix_confirmed',
    'ambiguous',
    'unknown',
]

RenderOverallState = Literal[
    'qualified',
    'qualified_with_limitations',
    'fallback_only',
    'mismatch',
    'insufficient_evidence',
    'not_applicable',
]
"""Session verdict (#603 §14) — a transport fallback or a configured/
installed mismatch is stated, never smoothed over."""


# ---------------------------------------------------------------------------
# JA display labels
# ---------------------------------------------------------------------------

METADATA_CLASS_LABELS = {
    'channel_bed': 'チャンネルベッド',
    'object': 'オブジェクト',
    'scene_hoa': 'シーン（HOA）',
    'matrix_encoded': 'マトリクス符号化',
    'mixed': '混合',
    'unparseable': '解析不可',
    'unknown': '不明',
}

LAYOUT_KIND_LABELS = {
    'physically_installed': '物理設置レイアウト',
    'processor_configured': 'プロセッサ設定レイアウト',
    'content_native': 'コンテンツネイティブレイアウト',
    'rendered_output': '実レンダリング出力レイアウト',
}

RENDER_STATE_LABELS = {
    'native_render': 'ネイティブレンダリング',
    'upmixed': 'アップミックス',
    'downmixed': 'ダウンミックス',
    'virtualized': '仮想化',
    'matrix_derived': 'マトリクス派生',
    'fallback_transport': '伝送フォールバック',
    'unrendered': '未レンダリング',
    'unknown': '不明',
}

RENDER_AXIS_LABELS = {
    'format_render_eligible': 'フォーマットレンダリング適格',
    'layout_geometry_eligible': 'レイアウト幾何適格',
    'output_channel_active': '出力チャンネル動作',
    'acoustic_performance_deferred': '音響性能（別権威）',
}

RENDER_OVERALL_LABELS = {
    'qualified': '適格',
    'qualified_with_limitations': '制限付き適格',
    'fallback_only': 'フォールバックのみ',
    'mismatch': '不一致',
    'insufficient_evidence': '証拠不足',
    'not_applicable': '対象外',
}


# ---------------------------------------------------------------------------
# Content identity (#603 §1)
# ---------------------------------------------------------------------------


class ImmersiveContentProfile(BaseModel):
    """What is actually being played (#603 §1).

    Structural claims (object count, scene order, declared layout) are
    allowed only from metadata-parsing sources — listening impressions
    can never carry them.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = RENDER_PATH_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-render-content-1'
    ] = CONTENT_AUTHORITY_VERSION
    content_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    asset_ref: str | None = None
    """Rights-safe test-asset identity (#603 §6) — source/licence/hash
    preserved, proprietary demo material never redistributed."""
    asset_sha256: str | None = Field(default=None, pattern=_SHA256)
    container: ContainerTransport = 'unknown'
    format_label: str | None = None
    """Free-text format (e.g. 'Dolby Atmos', 'DTS:X', 'Auro-3D',
    '5.1 PCM') — vendor names evolve; the record keeps the exact label."""
    metadata_class: ContentMetadataClass = 'unknown'
    declared_layout_label: str | None = None
    channel_count_declared: int | None = Field(default=None, ge=1)
    object_count_declared: int | None = Field(default=None, ge=1)
    scene_order: int | None = Field(default=None, ge=1)
    lfe_channel_count: int | None = Field(default=None, ge=0)
    """Content LFE channels — the x.y.z middle figure; never the
    physical subwoofer count (#603 §7)."""
    metadata_source: MetadataSource = 'unknown'
    metadata_parser_version: str | None = None
    adm_profile_ref: str | None = None
    """BS.2076 revision pin when ADM metadata is parsed (e.g.
    'itu-bs.2076-3@2025' via #599) — proprietary formats are never
    force-converted into ADM (#603 §10)."""
    sample_rate_hz: int | None = Field(default=None, gt=0)
    bit_depth: int | None = Field(default=None, gt=0)
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    declared_at_utc: str = Field(min_length=1)
    content_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'content_id', 'content_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'ImmersiveContentProfile':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        structural = (
            self.object_count_declared is not None
            or self.scene_order is not None
            or self.channel_count_declared is not None
            or self.declared_layout_label is not None
        )
        if structural and self.metadata_source in (
            'listening_observation', 'user_declared', 'unknown'
        ):
            raise ValueError(
                'structural content figures (object/scene counts, '
                'declared layout) require parser/readback/fixture '
                'evidence — never listening impressions or unverified '
                'declaration'
            )
        if self.metadata_class == 'scene_hoa' and (
            self.scene_order is None
        ):
            raise ValueError(
                "scene_hoa content must carry its declared HOA order"
            )
        digest = _digest(self.identity_payload())
        if self.content_sha256 != digest:
            raise ValueError('content profile hash mismatch')
        if self.content_id != _semantic_id('cont', digest):
            raise ValueError('content profile id mismatch')
        return self


# ---------------------------------------------------------------------------
# Renderer/decoder capability (#603 §2/§9/§15)
# ---------------------------------------------------------------------------


class RendererCapabilityProfile(BaseModel):
    """Exact device+firmware+license render capability (#603 §2).

    A product family name is not capability — supported formats,
    layouts and feature state are recorded per exact bound revision.
    Proprietary internals stay black-box: this record holds observable
    capability, never algorithm equivalence.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = RENDER_PATH_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-render-capability-1'
    ] = CAPABILITY_AUTHORITY_VERSION
    capability_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    device_ref: AuthorityRef
    model_label: str = Field(min_length=1)
    firmware_label: str | None = None
    license_state: LicenseState = 'unknown'
    supported_input_formats: tuple[str, ...] = ()
    supported_layouts: tuple[str, ...] = ()
    max_output_channels: int | None = Field(default=None, ge=1)
    height_support: Literal['top', 'height', 'none', 'unknown'] = 'unknown'
    wide_support: bool | None = None
    native_renderer_formats: tuple[str, ...] = ()
    """Format labels the device natively renders (e.g. 'atmos',
    'dts_x', 'auro3d') — distinct from upmixer capability."""
    upmixer_capabilities: tuple[str, ...] = ()
    """Upmixer/virtualizer identifiers (e.g. 'dolby_surround',
    'dts_neural_x', 'auromatic') — presence is capability, not proof
    of engagement."""
    virtualization_capability: Literal[
        'headphone', 'speaker_virtual', 'none', 'unknown'
    ] = 'unknown'
    bass_management_interaction: Literal[
        'declared', 'none', 'unknown'
    ] = 'unknown'
    capability_source: CapabilitySource = 'unknown'
    source_detail: str | None = None
    declared_at_utc: str = Field(min_length=1)
    capability_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'capability_id', 'capability_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'RendererCapabilityProfile':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        for items, label in (
            (self.supported_input_formats, 'supported_input_formats'),
            (self.supported_layouts, 'supported_layouts'),
            (self.native_renderer_formats, 'native_renderer_formats'),
            (self.upmixer_capabilities, 'upmixer_capabilities'),
        ):
            for item in items:
                if not item or not item.strip():
                    raise ValueError(f'{label} entries must be non-empty')
        if (
            self.native_renderer_formats
            and self.capability_source in ('user_declared', 'unknown')
        ):
            raise ValueError(
                'native renderer format claims require documented, '
                'read-back or observed evidence — a user declaration '
                'is not capability evidence'
            )
        digest = _digest(self.identity_payload())
        if self.capability_sha256 != digest:
            raise ValueError('renderer capability hash mismatch')
        if self.capability_id != _semantic_id('rcap', digest):
            raise ValueError('renderer capability id mismatch')
        return self


# ---------------------------------------------------------------------------
# Layout declarations (#603 §3/§7)
# ---------------------------------------------------------------------------


class SpeakerLayoutDeclaration(BaseModel):
    """One layout claim of exactly one kind (#603 §3).

    The four kinds carry different evidence bases; the model refuses a
    rendered-output claim that was never observed and a content-native
    claim that did not come from metadata.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = RENDER_PATH_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-render-layout-1'
    ] = LAYOUT_AUTHORITY_VERSION
    layout_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    kind: LayoutKind
    label: str | None = None
    """e.g. '7.1.4', '9.1.6', 'Auro 13.1' — the label is a name, not
    evidence; the role/count fields carry the structure."""
    ear_level_count: int | None = Field(default=None, ge=0)
    height_count: int | None = Field(default=None, ge=0)
    top_count: int | None = Field(default=None, ge=0)
    wide_count: int | None = Field(default=None, ge=0)
    lfe_channel_count: int | None = Field(default=None, ge=0)
    physical_subwoofer_count: int | None = Field(default=None, ge=0)
    """Physical bass-management subwoofers — distinct from
    ``lfe_channel_count`` (#603 §7; Dolby's x.y.z encodes the channel,
    not the hardware)."""
    speaker_roles: tuple[str, ...] = ()
    device_ref: AuthorityRef | None = None
    """Processor/AVR whose configuration this is, for
    ``processor_configured`` records."""
    evidence: LayoutEvidence = 'unknown'
    declared_at_utc: str = Field(min_length=1)
    layout_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'layout_id', 'layout_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'SpeakerLayoutDeclaration':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        for item in self.speaker_roles:
            if not item or not item.strip():
                raise ValueError('speaker_roles entries must be non-empty')
        if self.kind == 'rendered_output' and self.evidence != (
            'observed_outputs'
        ):
            raise ValueError(
                "a 'rendered_output' layout can only be declared from "
                'observed output evidence'
            )
        if self.kind == 'content_native' and self.evidence not in (
            'content_metadata', 'user_declared'
        ):
            raise ValueError(
                "a 'content_native' layout must come from content "
                'metadata (or stay an explicit user declaration)'
            )
        digest = _digest(self.identity_payload())
        if self.layout_sha256 != digest:
            raise ValueError('layout declaration hash mismatch')
        if self.layout_id != _semantic_id('lay', digest):
            raise ValueError('layout declaration id mismatch')
        return self


# ---------------------------------------------------------------------------
# Render session + output observation (#603 §5/§12/§13)
# ---------------------------------------------------------------------------


class RenderSession(BaseModel):
    """One bound playback state (#603 §12/§13).

    Changing decoder mode, upmixer state, firmware or source makes a
    distinct session — render results are state-bound, never generic.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = RENDER_PATH_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-render-session-1'
    ] = SESSION_AUTHORITY_VERSION
    session_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    content_ref: AuthorityRef
    renderer_ref: AuthorityRef | None = None
    """Pin to the RendererCapabilityProfile in force; ``None`` = the
    renderer capability is unknown and the verdict must stay so."""
    device_state_ref: AuthorityRef | None = None
    """Pin to the #592 device-configuration snapshot — render state is
    bound to the exact device state."""
    configured_layout_ref: AuthorityRef | None = None
    installed_layout_ref: AuthorityRef | None = None
    decoder_mode: DecoderMode = 'unknown'
    listening_mode_label: str | None = None
    upmixer_state: UpmixerState = 'unknown'
    source_format_label: str | None = None
    transported_format_label: str | None = None
    """The audio format actually observed at the processor input after
    transport (#603 §13) — e.g. 'bitstream_atmos' vs 'lpcm_5_1' vs
    'core_5_1'; a mismatch against the source is a fallback."""
    dsp_preset_label: str | None = None
    started_at_utc: str = Field(min_length=1)
    session_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'session_id', 'session_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'RenderSession':
        _require_iso8601(self.started_at_utc, 'started_at_utc')
        if self.content_ref.kind != 'immersive_content_profile':
            raise ValueError(
                "content_ref must pin an 'immersive_content_profile'"
            )
        if self.renderer_ref is not None and (
            self.renderer_ref.kind != 'renderer_capability_profile'
        ):
            raise ValueError(
                "renderer_ref must pin a 'renderer_capability_profile'"
            )
        for ref, label in (
            (self.configured_layout_ref, 'configured_layout_ref'),
            (self.installed_layout_ref, 'installed_layout_ref'),
        ):
            if ref is not None and ref.kind != 'speaker_layout':
                raise ValueError(
                    f"{label} must pin a 'speaker_layout' authority"
                )
        digest = _digest(self.identity_payload())
        if self.session_sha256 != digest:
            raise ValueError('render session hash mismatch')
        if self.session_id != _semantic_id('rsess', digest):
            raise ValueError('render session id mismatch')
        return self


class ObservedOutput(BaseModel):
    """One rendered-output observation row (#603 §5)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    output_label: str = Field(min_length=1)
    """Processor output/channel id or physical speaker entity."""
    speaker_ref: str | None = None
    signal_present: bool | None = None
    level_db: float | None = None
    timing_ms: float | None = None
    activity: OutputActivity = 'unobserved'

    @model_validator(mode='after')
    def _check(self) -> 'ObservedOutput':
        for value in (self.level_db, self.timing_ms):
            if value is not None:
                _finite(value, 'output measurement')
        if self.signal_present is True and self.activity == 'inactive':
            raise ValueError(
                'signal_present contradicts an inactive activity claim'
            )
        if self.signal_present is False and self.activity == 'active':
            raise ValueError(
                'an active claim requires observed signal'
            )
        return self


class RenderedOutputObservation(BaseModel):
    """External observation of which physical outputs carried signal.

    Records what was measured — never the proprietary renderer's
    internal panning algorithm (#603 §5/§15).
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = RENDER_PATH_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-render-observation-1'
    ] = OUTPUT_OBSERVATION_AUTHORITY_VERSION
    observation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    session_ref: AuthorityRef
    capture_method: CaptureMethod = 'unknown'
    outputs: tuple[ObservedOutput, ...]
    lfe_activity: OutputActivity = 'unobserved'
    redirected_bass_observed: bool | None = None
    """Bass arriving at subs through bass management rather than the
    LFE channel (#603 §8) — kept distinct from ``lfe_activity``."""
    observation_note: str | None = None
    observed_at_utc: str = Field(min_length=1)
    observation_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'observation_id', 'observation_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'RenderedOutputObservation':
        _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        if self.session_ref.kind != 'render_session':
            raise ValueError(
                "session_ref must pin a 'render_session' authority"
            )
        if not self.outputs:
            raise ValueError(
                'an output observation requires at least one output row'
            )
        digest = _digest(self.identity_payload())
        if self.observation_sha256 != digest:
            raise ValueError('output observation hash mismatch')
        if self.observation_id != _semantic_id('robs', digest):
            raise ValueError('output observation id mismatch')
        return self


# ---------------------------------------------------------------------------
# Qualification verdict (#603 §14)
# ---------------------------------------------------------------------------


class RenderPathQualification(BaseModel):
    """Sealed render-path verdict for one session (#603 §14).

    Produced only by :func:`evaluate_render_path`. The verdict keeps
    the independent axes separate; there is no composite 'immersive
    compliant' boolean.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = RENDER_PATH_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-render-qualification-1'
    ] = RENDER_QUALIFICATION_AUTHORITY_VERSION
    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    session_ref: AuthorityRef
    evaluation_version: str = Field(min_length=1)
    render_state: RenderState
    provenance_state: ProvenanceState
    layout_match_state: LayoutMatchState
    axes: tuple[tuple[RenderAxis, RenderAxisResult], ...]
    transport_fallback: bool
    fallback_detail: str | None = None
    active_outputs: tuple[str, ...] = ()
    unused_outputs: tuple[str, ...] = ()
    bass_note: str | None = None
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    overall_state: RenderOverallState
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'qualification_id', 'qualification_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'RenderPathQualification':
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        if self.session_ref.kind != 'render_session':
            raise ValueError(
                "session_ref must pin a 'render_session' authority"
            )
        if not self.axes:
            raise ValueError('a qualification must carry its axis set')
        seen: set[str] = set()
        for axis, _ in self.axes:
            if axis in seen:
                raise ValueError(f'duplicate axis {axis}')
            seen.add(axis)
        digest = _digest(self.identity_payload())
        if self.qualification_sha256 != digest:
            raise ValueError('render qualification hash mismatch')
        if self.qualification_id != _semantic_id('rpath', digest):
            raise ValueError('render qualification id mismatch')
        return self

    def axis_result(self, axis: RenderAxis) -> RenderAxisResult:
        for name, result in self.axes:
            if name == axis:
                return result
        return 'unknown'


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _build_sealed(model: type[BaseModel], sha_field: str,
                  id_field: str, prefix: str,
                  authority_version: str,
                  **kwargs: Any) -> Any:
    probe = model.model_construct(
        **_canon(
            model,
            dict(
                schema_version=RENDER_PATH_SCHEMA_VERSION,
                authority_version=authority_version,
                **{sha_field: '0' * 64, id_field: ''},
                **kwargs,
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={sha_field, id_field}),
        **{sha_field: sha, id_field: _semantic_id(prefix, sha)},
    )


def build_content_profile(**kwargs: Any) -> ImmersiveContentProfile:
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _build_sealed(
        ImmersiveContentProfile, 'content_sha256', 'content_id',
        'cont', CONTENT_AUTHORITY_VERSION, **kwargs,
    )


def build_renderer_capability(**kwargs: Any) -> RendererCapabilityProfile:
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _build_sealed(
        RendererCapabilityProfile, 'capability_sha256', 'capability_id',
        'rcap', CAPABILITY_AUTHORITY_VERSION, **kwargs,
    )


def build_layout(**kwargs: Any) -> SpeakerLayoutDeclaration:
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _build_sealed(
        SpeakerLayoutDeclaration, 'layout_sha256', 'layout_id',
        'lay', LAYOUT_AUTHORITY_VERSION, **kwargs,
    )


def build_render_session(**kwargs: Any) -> RenderSession:
    return _build_sealed(
        RenderSession, 'session_sha256', 'session_id', 'rsess',
        SESSION_AUTHORITY_VERSION, **kwargs,
    )


def build_output_observation(**kwargs: Any) -> RenderedOutputObservation:
    return _build_sealed(
        RenderedOutputObservation, 'observation_sha256',
        'observation_id', 'robs', OUTPUT_OBSERVATION_AUTHORITY_VERSION,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Evaluation (#603 §13/§14)
# ---------------------------------------------------------------------------

_DOWNMIX_DECODER_MODES = frozenset({'stereo', 'multichannel'})
"""Decoder modes that can never be a native immersive render."""


def _norm_format(value: str | None) -> str:
    return (value or '').strip().lower()


def evaluate_render_path(
    *,
    session: RenderSession,
    content: ImmersiveContentProfile | None = None,
    capability: RendererCapabilityProfile | None = None,
    configured_layout: SpeakerLayoutDeclaration | None = None,
    installed_layout: SpeakerLayoutDeclaration | None = None,
    observations: Sequence[RenderedOutputObservation] = (),
    expected_outputs: Sequence[str] = (),
    evaluated_at_utc: str,
) -> RenderPathQualification:
    """Fail-closed render-path verdict for one bound session.

    ``expected_outputs`` is the channel/role set a rights-safe test
    asset or the configured layout claims should be driven — supplied
    by the caller, never inferred from a layout label.
    """
    reasons: list[str] = []
    limitations: list[str] = []
    axes: dict[RenderAxis, RenderAxisResult] = {
        'format_render_eligible': 'unknown',
        'layout_geometry_eligible': 'unknown',
        'output_channel_active': 'unknown',
        'acoustic_performance_deferred': 'not_applicable',
    }

    # --- transport stage (#603 §13) --------------------------------------
    transport_fallback = False
    fallback_detail: str | None = None
    if (
        session.transported_format_label
        and session.source_format_label
        and _norm_format(session.transported_format_label)
        != _norm_format(session.source_format_label)
    ):
        transport_fallback = True
        fallback_detail = (
            f'source declared {_norm_format(session.source_format_label)!r} '
            f'but {_norm_format(session.transported_format_label)!r} '
            'arrived at the processor — richer render never happened '
            'downstream of the transport stage'
        )
        reasons.append(fallback_detail)

    # --- format eligibility axis ----------------------------------------
    render_state: RenderState = 'unknown'
    provenance: ProvenanceState = 'unknown'
    if capability is None:
        limitations.append(
            'renderer capability is not bound — renderability cannot '
            'be claimed'
        )
        axes['format_render_eligible'] = 'unknown'
    else:
        fmt = _norm_format(
            content.format_label if content else None
        ) or _norm_format(session.source_format_label)
        supported = {_norm_format(f) for f in capability.supported_input_formats}
        native = {_norm_format(f) for f in capability.native_renderer_formats}
        if fmt and fmt not in supported and supported:
            axes['format_render_eligible'] = 'fail'
            reasons.append(
                f'format {fmt!r} not in the renderer\'s declared '
                'supported input formats'
            )
        elif fmt and fmt in native:
            axes['format_render_eligible'] = 'pass'
        elif fmt and supported and fmt in supported:
            axes['format_render_eligible'] = 'limited'
            limitations.append(
                f'format {fmt!r} is accepted but has no native-'
                'renderer declaration — decode may be downmix/upmix'
            )
        elif not fmt:
            limitations.append('content format not identified')
            axes['format_render_eligible'] = 'unknown'
        else:
            limitations.append(
                'renderer declares no supported-format list'
            )
            axes['format_render_eligible'] = 'unknown'

    # --- render provenance (#603 §4) --------------------------------------
    if transport_fallback:
        render_state = 'fallback_transport'
        provenance = 'ambiguous'
    elif session.decoder_mode in _DOWNMIX_DECODER_MODES:
        render_state = 'downmixed'
        provenance = 'downmix_confirmed'
    elif capability is not None:
        fmt = _norm_format(
            content.format_label if content else None
        ) or _norm_format(session.source_format_label)
        native = {_norm_format(f) for f in capability.native_renderer_formats}
        if session.upmixer_state == 'on' and fmt not in native:
            render_state = 'upmixed'
            provenance = 'upmixer_confirmed'
        elif fmt and fmt in native:
            if capability.virtualization_capability != 'none' and (
                session.decoder_mode == 'virtualizer'
            ):
                render_state = 'virtualized'
                provenance = 'virtualized_confirmed'
            else:
                render_state = 'native_render'
                provenance = 'native_confirmed'
        elif content is not None and content.metadata_class in (
            'object', 'scene_hoa', 'mixed'
        ) and session.upmixer_state == 'unknown':
            limitations.append(
                'immersive content but upmixer state unobserved — '
                'provenance ambiguous'
            )
            provenance = 'ambiguous'
        elif not fmt:
            provenance = 'unknown'
        elif session.upmixer_state == 'on':
            render_state = 'upmixed'
            provenance = 'upmixer_confirmed'
        else:
            render_state = 'unknown'
            provenance = 'unknown'
    if (
        render_state == 'native_render'
        and content is not None
        and content.metadata_class == 'channel_bed'
        and session.upmixer_state == 'on'
    ):
        # A bed-only source with an engaged upmixer is never 'native'
        # for the driven height/top outputs (#603 §16).
        render_state = 'upmixed'
        provenance = 'upmixer_confirmed'
        reasons.append(
            'channel-bed source with upmixer engaged — driven height/'
            'top outputs carry upmixed provenance, not native'
        )

    # --- layout geometry axis (#603 §3) ------------------------------------
    layout_match: LayoutMatchState = 'unknown'
    unused: list[str] = []
    if configured_layout is not None and installed_layout is not None:
        configured_roles = set(configured_layout.speaker_roles)
        installed_roles = set(installed_layout.speaker_roles)
        if configured_roles and installed_roles:
            if configured_roles == installed_roles:
                layout_match = 'matches'
                axes['layout_geometry_eligible'] = 'pass'
            elif configured_roles < installed_roles:
                unused = sorted(installed_roles - configured_roles)
                layout_match = 'partial_unused_outputs'
                axes['layout_geometry_eligible'] = 'limited'
                reasons.append(
                    'installed speakers unaddressed by the configured '
                    'layout: ' + ', '.join(unused)
                )
            elif configured_roles > installed_roles:
                layout_match = 'configured_differs_from_installed'
                axes['layout_geometry_eligible'] = 'fail'
                missing = sorted(configured_roles - installed_roles)
                reasons.append(
                    'configured roles with no installed speaker: '
                    + ', '.join(missing)
                )
            else:
                layout_match = 'configured_differs_from_installed'
                axes['layout_geometry_eligible'] = 'fail'
                reasons.append(
                    'configured and installed layouts differ'
                )
        else:
            limitations.append(
                'layout role sets undeclared — cannot compare '
                'configured vs installed'
            )
            axes['layout_geometry_eligible'] = 'unknown'
            layout_match = 'unknown'
    elif configured_layout is None or installed_layout is None:
        limitations.append(
            'configured and installed layouts are not both bound — '
            'geometry eligibility unproven'
        )
    if capability is not None and configured_layout is not None and (
        configured_layout.label
        and capability.supported_layouts
        and configured_layout.label not in capability.supported_layouts
    ):
        axes['layout_geometry_eligible'] = 'fail'
        reasons.append(
            f'configured layout {configured_layout.label!r} is not in '
            'the renderer\'s declared supported layouts'
        )
        if layout_match != 'configured_differs_from_installed':
            layout_match = 'content_mismatch'

    # --- output activity axis (#603 §5) -----------------------------------
    session_observations = [
        o for o in observations
        if o.session_ref.ref_id == session.session_id
    ]
    active: list[str] = []
    if session_observations:
        latest = session_observations[-1]
        active = sorted(
            o.output_label for o in latest.outputs if o.activity == 'active'
        )
        inactive = sorted(
            o.output_label
            for o in latest.outputs
            if o.activity == 'inactive'
        )
        if expected_outputs:
            expected = set(expected_outputs)
            missing = expected - set(active)
            if not missing:
                axes['output_channel_active'] = 'pass'
            elif active:
                axes['output_channel_active'] = 'limited'
                reasons.append(
                    'expected outputs without observed activity: '
                    + ', '.join(sorted(missing))
                )
            else:
                axes['output_channel_active'] = 'fail'
                reasons.append(
                    'no expected output showed activity'
                )
        elif inactive:
            axes['output_channel_active'] = 'limited'
            reasons.append(
                'inactive outputs observed: ' + ', '.join(inactive)
            )
        else:
            axes['output_channel_active'] = 'pass'
    else:
        limitations.append('no output observation bound to the session')

    # --- bass provenance note (#603 §7/§8) --------------------------------
    bass_note: str | None = None
    if session_observations:
        latest = session_observations[-1]
        if latest.redirected_bass_observed and latest.lfe_activity != 'active':
            bass_note = (
                'subwoofer output carries bass-managed/redirected bass '
                '— not the content LFE channel; x.y.z never encodes '
                'physical sub count'
            )

    # --- overall (#603 §14) ------------------------------------------------
    if transport_fallback:
        overall: RenderOverallState = 'fallback_only'
    elif axes['format_render_eligible'] == 'fail' or (
        axes['layout_geometry_eligible'] == 'fail'
    ) or axes['output_channel_active'] == 'fail':
        overall = 'mismatch'
    elif (
        axes['format_render_eligible'] == 'unknown'
        and axes['output_channel_active'] == 'unknown'
    ):
        overall = 'insufficient_evidence'
    elif (
        'limited' in axes.values() or 'unknown' in axes.values()
    ):
        overall = 'qualified_with_limitations'
    else:
        overall = 'qualified'

    axes_tuple: tuple[tuple[RenderAxis, RenderAxisResult], ...] = tuple(
        (axis, result) for axis, result in axes.items()
    )
    probe = RenderPathQualification.model_construct(
        **_canon(
            RenderPathQualification,
            dict(
                schema_version=RENDER_PATH_SCHEMA_VERSION,
                authority_version=RENDER_QUALIFICATION_AUTHORITY_VERSION,
                qualification_id='',
                document_id=session.document_id,
                session_ref=AuthorityRef(
                    kind='render_session',
                    ref_id=session.session_id,
                    ref_sha256=session.session_sha256,
                ),
                evaluation_version=RENDER_EVALUATION_VERSION,
                render_state=render_state,
                provenance_state=provenance,
                layout_match_state=layout_match,
                axes=axes_tuple,
                transport_fallback=transport_fallback,
                fallback_detail=fallback_detail,
                active_outputs=tuple(active),
                unused_outputs=tuple(unused),
                bass_note=bass_note,
                reasons=tuple(reasons),
                limitations=tuple(limitations),
                overall_state=overall,
                evaluated_at_utc=evaluated_at_utc,
                qualification_sha256='0' * 64,
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return RenderPathQualification(
        **probe.model_dump(
            mode='python',
            exclude={'qualification_id', 'qualification_sha256'},
        ),
        qualification_id=_semantic_id('rpath', sha),
        qualification_sha256=sha,
    )
