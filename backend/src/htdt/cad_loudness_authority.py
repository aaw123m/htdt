"""Content loudness / normalization authority (#607).

``volume setting``, ``programme loudness``, ``streaming normalization``,
``true peak``, ``decoder gain`` and ``in-room SPL`` are *different*
quantities. This module keeps them as separate sealed authorities and
never silently converts one into another.

Literature basis (research verified 2026-10-05):

- ITU-R BS.1770-5 (11/2023) — production-current recommendation:
  K-weighting (high-shelf + RLBP high-pass prefilters), channel-summed
  mean-square with channel weights, 400 ms blocks at 75% overlap,
  absolute gate -70 LUFS, relative gate -10 LU under the ungated mean,
  true-peak via oversampled metering (Annex 2), extended layouts
  (Annex 3) and object-based measurement (Annex 4).
- ITU-R 2026 draft-revision activity — registered as research-only via
  #599 until a final recommendation is published; historical
  measurements stay pinned to their exact algorithm revision.
- AES77-2023 — streaming/on-demand distribution loudness guidance; AES
  explicitly notes it does not define device playback targets.
- EBU R128 v5.0 (2023) — separate normalization/distribution profile.
- CEDIA 2026 reference-level clarification — calibration state and
  programme loudness are distinct quantities.

Authority boundary:

- content/asset identity is exact (title+track+edition+codec+hash);
- normalization state (service/metadata/provider) is distribution
  behavior, never a room-SPL target;
- programme LUFS never converts to system capability (#579/#593) or
  hearing dose (#602) without an explicit calibrated chain;
- immersive content requires an immersive-capable profile — a
  stereo-only meter is ineligible for object-based assets;
- matched-loudness A/B comparisons persist the matching method and
  residual — loudness is never an uncontrolled variable;
- this module does not own renderer state (#603), gain structure
  (#593), SPL capability (#579) or exposure safety (#602).
"""

from __future__ import annotations

from math import isfinite, log10
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical_json import (
    canonical_sha256 as _hash,
    canonicalize_payload,
)
from .clock import utc_now_iso as _utc_now


LOUDNESS_SCHEMA_VERSION = 1
LOUDNESS_AUTHORITY_VERSION = 'loudness-authority-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'
_PROFILE_ID_PATTERN = r'^ldnprof:[0-9a-f]{64}$'
_MEASUREMENT_ID_PATTERN = r'^plm:[0-9a-f]{64}$'
_OBSERVATION_ID_PATTERN = r'^norm:[0-9a-f]{64}$'
_GAIN_STATE_ID_PATTERN = r'^pgs:[0-9a-f]{64}$'
_MATCHING_ID_PATTERN = r'^lmr:[0-9a-f]{64}$'


LoudnessQuantity = Literal[
    'programme_integrated_loudness',
    'short_term_loudness',
    'momentary_loudness',
    'loudness_range',
    'true_peak',
    'content_metadata_gain',
    'service_normalization_gain',
    'player_gain',
    'processor_input_gain',
    'master_volume',
    'channel_trim',
    'measured_in_room_spl',
    'system_max_clean_capability',
]
"""Closed quantity taxonomy — no implicit conversion among these."""

LoudnessSourceClass = Literal[
    'disc_file',
    'streaming_video',
    'streaming_audio',
    'broadcast',
    'game',
    'live_external',
    'test_signal',
]

NormalizationMode = Literal[
    'off',
    'on_target_profile',
    'metadata_based',
    'replaygain_style',
    'provider_specific',
    'unknown',
]

LoudnessChannelConfig = Literal[
    'mono', 'stereo', 'multichannel_5_1', 'multichannel_7_1',
    'bs2051_extended', 'object_based', 'channel_and_object', 'custom',
]

LoudnessProfileEligibility = Literal[
    'production_current', 'research_only', 'historical',
]


QUANTITY_LABELS: dict[str, str] = {
    'programme_integrated_loudness': '番組統合ラウドネス (LUFS)',
    'short_term_loudness': '短期ラウドネス',
    'momentary_loudness': '瞬間ラウドネス',
    'loudness_range': 'ラウドネスレンジ (LU)',
    'true_peak': 'トゥルーピーク (dBTP)',
    'content_metadata_gain': 'コンテンツメタデータゲイン',
    'service_normalization_gain': 'サービス正規化ゲイン',
    'player_gain': 'プレイヤーゲイン',
    'processor_input_gain': 'プロセッサ入力トリム',
    'master_volume': 'マスターボリューム',
    'channel_trim': 'チャンネルトリム',
    'measured_in_room_spl': '室内実測SPL',
    'system_max_clean_capability': 'システム最大クリーン能力',
}

SOURCE_CLASS_LABELS: dict[str, str] = {
    'disc_file': 'ディスク/ファイル',
    'streaming_video': '映像ストリーミング',
    'streaming_audio': '音楽ストリーミング',
    'broadcast': '放送',
    'game': 'ゲーム',
    'live_external': 'ライブ/外部入力',
    'test_signal': 'テスト信号',
}

NORMALIZATION_LABELS: dict[str, str] = {
    'off': '正規化オフ',
    'on_target_profile': 'ターゲットプロファイル正規化',
    'metadata_based': 'メタデータベース正規化',
    'replaygain_style': 'ReplayGain系',
    'provider_specific': 'プロバイダ固有',
    'unknown': '不明',
}


# ---------------------------------------------------------------------------
# BS.1770 constants + math
# ---------------------------------------------------------------------------

#: ITU pre-filter (high-shelf) design parameters (BS.1770 Annex 1).
K_SHELF_F0_HZ = 1681.974450955533
K_SHELF_Q = 0.7071752369554196
K_SHELF_GAIN_DB = 3.999843853973347

#: ITU RLBP high-pass design parameters (BS.1770 Annex 1).
K_HP_F0_HZ = 38.13547087602444
K_HP_Q = 0.5003270373238773

#: 400 ms blocks, 75% overlap (gating block hop = 100 ms).
GATING_BLOCK_S = 0.400
GATING_ABSOLUTE_LUFS = -70.0
GATING_RELATIVE_LU = -10.0
LOUDNESS_OFFSET = -0.691


def loudness_channel_weights(
    channel_config: LoudnessChannelConfig,
    channel_count: int,
) -> tuple[float, ...]:
    """BS.1770 channel weights. Surrounds/top channels carry 1.41
    (+1.5 dB) per Annex 1/3; LFE is never weighted."""
    if channel_config == 'mono':
        return (1.0,)
    if channel_config == 'stereo':
        if channel_count != 2:
            raise ValueError('stereo profile requires exactly 2 channels')
        return (1.0, 1.0)
    if channel_config == 'multichannel_5_1':
        if channel_count != 5:
            raise ValueError('5.1 loudness excludes the LFE channel')
        return (1.0, 1.0, 1.0, 1.41, 1.41)
    if channel_config == 'multichannel_7_1':
        if channel_count != 7:
            raise ValueError('7.1 loudness excludes the LFE channel')
        return (1.0, 1.0, 1.0, 1.41, 1.41, 1.41, 1.41)
    raise ValueError(
        f'channel weights for {channel_config} are profile-declared, '
        'not assumed — provide them on the profile'
    )


def compute_integrated_loudness_lufs(
    block_powers: tuple[tuple[float, ...], ...],
    channel_weights: tuple[float, ...],
) -> tuple[float | None, int]:
    """BS.1770 integrated loudness with absolute+relative gating.

    ``block_powers[j][i]`` = K-weighted mean square of channel ``i`` in
    400 ms block ``j`` (75% overlap upstream). Returns
    ``(lufs, gated_block_count)``; ``None`` when nothing survives the
    gates (silence is honest, not 0)."""
    if not block_powers:
        return None, 0
    if any(len(row) != len(channel_weights) for row in block_powers):
        raise ValueError('block rows must align with channel weights')
    z_blocks = [
        sum(w * p for w, p in zip(channel_weights, row))
        for row in block_powers
    ]
    l_blocks = [
        LOUDNESS_OFFSET + 10.0 * log10(z) for z in z_blocks if z > 0.0
    ]
    gated = [
        (z, l) for z, l in zip(z_blocks, l_blocks)
        if z > 0.0 and l > GATING_ABSOLUTE_LUFS
    ]
    if not gated:
        return None, 0
    z_vals = [g[0] for g in gated]
    ungated_mean = sum(z_vals) / len(z_vals)
    if ungated_mean <= 0.0:
        return None, 0
    ungated_lufs = LOUDNESS_OFFSET + 10.0 * log10(ungated_mean)
    threshold = ungated_lufs + GATING_RELATIVE_LU
    final = [
        z for z, l in gated if l > threshold
    ]
    if not final:
        return None, 0
    integrated = (
        LOUDNESS_OFFSET + 10.0 * log10(sum(final) / len(final))
    )
    return integrated, len(final)


def compute_true_peak_dbtp(
    oversampled_channel_peaks: tuple[float, ...],
) -> float | None:
    """Annex-2-style true peak: maximum absolute level across channels of
    the >=4x-oversampled waveform. Caller supplies the oversampled peaks;
    this authority refuses to call a sample peak a true peak."""
    if not oversampled_channel_peaks:
        return None
    peak = max(abs(float(p)) for p in oversampled_channel_peaks)
    if peak <= 0.0:
        return None
    return 20.0 * log10(peak)


# ---------------------------------------------------------------------------
# Profiles and records
# ---------------------------------------------------------------------------


class ContentLoudnessProfile(BaseModel):
    """Sealed algorithm/profile identity — every loudness result binds
    exactly one of these (issue §2/§13)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = LOUDNESS_SCHEMA_VERSION
    authority_version: Literal[
        'loudness-authority-1'
    ] = LOUDNESS_AUTHORITY_VERSION
    profile_id: str = Field(pattern=_PROFILE_ID_PATTERN)
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    document_id: str = Field(min_length=1)
    standard_id: str = Field(min_length=1)
    standard_edition: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    profile_label: str = Field(min_length=1)
    eligibility: LoudnessProfileEligibility
    algorithm_version: str = Field(min_length=1)
    channel_config: LoudnessChannelConfig
    lfe_treatment: Literal['excluded', 'declared_other'] = 'excluded'
    gating_semantics: str = Field(min_length=1)
    true_peak_method: str | None = None
    distribution_target_lufs: float | None = None
    limitations: tuple[str, ...] = ()
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_profile(self) -> 'ContentLoudnessProfile':
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('content loudness profile hash mismatch')
        if self.profile_id != f'ldnprof:{expected}':
            raise ValueError('content loudness profile id mismatch')
        if self.distribution_target_lufs is not None and not isfinite(
            float(self.distribution_target_lufs)
        ):
            raise ValueError('distribution target must be finite')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'profile_id', 'profile_sha256'},
        )


class ContentIdentity(BaseModel):
    """Exact asset identity (issue §3): title alone never merges
    releases/tracks — different masters carry different loudness."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    title: str | None = None
    stream_or_file_ref: str | None = None
    edition: str | None = None
    track_id: str | None = None
    language: str | None = None
    codec: str | None = None
    container: str | None = None
    format_ref: str | None = None
    content_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    service_source_label: str | None = None


class ProgrammeLoudnessMeasurement(BaseModel):
    """Sealed per-asset loudness/true-peak evidence under one exact
    algorithm profile. Immutable across future standard revisions."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = LOUDNESS_SCHEMA_VERSION
    authority_version: Literal[
        'loudness-authority-1'
    ] = LOUDNESS_AUTHORITY_VERSION
    measurement_id: str = Field(pattern=_MEASUREMENT_ID_PATTERN)
    measurement_sha256: str = Field(pattern=_SHA256_PATTERN)

    document_id: str = Field(min_length=1)
    content: ContentIdentity
    source_class: LoudnessSourceClass
    profile_id: str = Field(pattern=_PROFILE_ID_PATTERN)
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    standard_id: str = Field(min_length=1)
    standard_edition: str = Field(min_length=1)
    algorithm_version: str = Field(min_length=1)
    channel_config: LoudnessChannelConfig
    profile_eligibility: LoudnessProfileEligibility

    integrated_loudness_lufs: float | None = None
    loudness_range_lu: float | None = None
    true_peak_dbtp: float | None = None
    momentary_max_lufs: float | None = None
    short_term_max_lufs: float | None = None
    segment_identity: str | None = None
    gated_block_count: int | None = None
    applicability: Literal['in_scope', 'limited', 'out_of_scope'] = (
        'in_scope'
    )
    applicability_reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    measured_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_measurement(self) -> 'ProgrammeLoudnessMeasurement':
        expected = _hash(self.identity_payload())
        if self.measurement_sha256 != expected:
            raise ValueError('programme loudness measurement hash')
        if self.measurement_id != f'plm:{expected}':
            raise ValueError('programme loudness measurement id')
        for field in (
            'integrated_loudness_lufs', 'loudness_range_lu',
            'true_peak_dbtp', 'momentary_max_lufs', 'short_term_max_lufs',
        ):
            value = getattr(self, field)
            if value is not None and not isfinite(float(value)):
                raise ValueError(f'{field} must be finite')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'measurement_id', 'measurement_sha256'},
        )


class NormalizationObservation(BaseModel):
    """Observed/configured normalization state — distribution behavior,
    never a room-SPL target (issue §4)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = LOUDNESS_SCHEMA_VERSION
    authority_version: Literal[
        'loudness-authority-1'
    ] = LOUDNESS_AUTHORITY_VERSION
    observation_id: str = Field(pattern=_OBSERVATION_ID_PATTERN)
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)

    document_id: str = Field(min_length=1)
    content: ContentIdentity | None = None
    source_class: LoudnessSourceClass
    mode: NormalizationMode
    target_profile_ref: str | None = None
    target_lufs: float | None = None
    applied_gain_db: float | None = None
    limiter_state: Literal[
        'inactive', 'engaged', 'unknown', 'not_applicable'
    ] = 'unknown'
    device_app_version: str | None = None
    user_setting: str | None = None
    observed_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_observation(self) -> 'NormalizationObservation':
        expected = _hash(self.identity_payload())
        if self.observation_sha256 != expected:
            raise ValueError('normalization observation hash mismatch')
        if self.observation_id != f'norm:{expected}':
            raise ValueError('normalization observation id mismatch')
        for field in ('target_lufs', 'applied_gain_db'):
            value = getattr(self, field)
            if value is not None and not isfinite(float(value)):
                raise ValueError(f'{field} must be finite')
        if self.mode == 'off' and self.applied_gain_db not in (None, 0.0):
            raise ValueError(
                'an off-mode observation cannot carry an applied gain'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'observation_id', 'observation_sha256'},
        )


class PlaybackGainState(BaseModel):
    """The full gain chain at playback time (issue §1/§8): metadata gain,
    normalization gain, player/decoder, processor trims, master volume,
    channel trims and the separately-pinned calibration reference state."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = LOUDNESS_SCHEMA_VERSION
    authority_version: Literal[
        'loudness-authority-1'
    ] = LOUDNESS_AUTHORITY_VERSION
    state_id: str = Field(pattern=_GAIN_STATE_ID_PATTERN)
    state_sha256: str = Field(pattern=_SHA256_PATTERN)

    document_id: str = Field(min_length=1)
    content: ContentIdentity | None = None
    normalization_observation_id: str | None = Field(
        default=None, pattern=_OBSERVATION_ID_PATTERN
    )
    content_metadata_gain_db: float | None = None
    service_normalization_gain_db: float | None = None
    player_gain_db: float | None = None
    processor_input_gain_db: float | None = None
    master_volume_db: float | None = None
    channel_trims_db: dict[str, float] = {}
    calibration_reference_state_ref: str | None = None
    measured_in_room_spl_db: float | None = None
    measured_position_label: str | None = None
    captured_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_state(self) -> 'PlaybackGainState':
        expected = _hash(self.identity_payload())
        if self.state_sha256 != expected:
            raise ValueError('playback gain state hash mismatch')
        if self.state_id != f'pgs:{expected}':
            raise ValueError('playback gain state id mismatch')
        for field in (
            'content_metadata_gain_db', 'service_normalization_gain_db',
            'player_gain_db', 'processor_input_gain_db',
            'master_volume_db', 'measured_in_room_spl_db',
        ):
            value = getattr(self, field)
            if value is not None and not isfinite(float(value)):
                raise ValueError(f'{field} must be finite')
        if not all(
            isfinite(float(v)) for v in self.channel_trims_db.values()
        ):
            raise ValueError('channel trims must be finite')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'state_id', 'state_sha256'},
        )

    @property
    def total_source_gain_db(self) -> float:
        """Source-side gain only (metadata + normalization + player) —
        processor/master-volume stages stay separate."""
        return sum(
            v for v in (
                self.content_metadata_gain_db,
                self.service_normalization_gain_db,
                self.player_gain_db,
            )
            if v is not None
        )


class LoudnessMatchingRecord(BaseModel):
    """Explicit level-matching evidence for an A/B comparison (issue §7):
    subjective preference at uncontrolled loudness is never attributed
    to EQ/renderer quality."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = LOUDNESS_SCHEMA_VERSION
    authority_version: Literal[
        'loudness-authority-1'
    ] = LOUDNESS_AUTHORITY_VERSION
    record_id: str = Field(pattern=_MATCHING_ID_PATTERN)
    record_sha256: str = Field(pattern=_SHA256_PATTERN)

    document_id: str = Field(min_length=1)
    comparison_label: str = Field(min_length=1)
    target_quantity: LoudnessQuantity
    measurement_window: str = Field(min_length=1)
    side_a_content: ContentIdentity
    side_b_content: ContentIdentity
    applied_gain_a_db: float = 0.0
    applied_gain_b_db: float = 0.0
    residual_mismatch_db: float
    method: str = Field(min_length=1)
    recorded_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_record(self) -> 'LoudnessMatchingRecord':
        expected = _hash(self.identity_payload())
        if self.record_sha256 != expected:
            raise ValueError('loudness matching record hash mismatch')
        if self.record_id != f'lmr:{expected}':
            raise ValueError('loudness matching record id mismatch')
        for field in (
            'applied_gain_a_db', 'applied_gain_b_db',
            'residual_mismatch_db',
        ):
            if not isfinite(float(getattr(self, field))):
                raise ValueError(f'{field} must be finite')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'record_id', 'record_sha256'},
        )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_loudness_profile(**kwargs: Any) -> ContentLoudnessProfile:
    probe = ContentLoudnessProfile.model_construct(
        **canonicalize_payload(
            ContentLoudnessProfile,
            dict(
                schema_version=LOUDNESS_SCHEMA_VERSION,
                authority_version=LOUDNESS_AUTHORITY_VERSION,
                profile_id='',
                profile_sha256='',
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return ContentLoudnessProfile(
        **probe.model_dump(exclude={'profile_id', 'profile_sha256'}),
        profile_id=f'ldnprof:{sha}',
        profile_sha256=sha,
    )


def build_loudness_measurement(**kwargs: Any) -> ProgrammeLoudnessMeasurement:
    probe = ProgrammeLoudnessMeasurement.model_construct(
        **canonicalize_payload(
            ProgrammeLoudnessMeasurement,
            dict(
                schema_version=LOUDNESS_SCHEMA_VERSION,
                authority_version=LOUDNESS_AUTHORITY_VERSION,
                measurement_id='',
                measurement_sha256='',
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return ProgrammeLoudnessMeasurement(
        **probe.model_dump(exclude={'measurement_id', 'measurement_sha256'}),
        measurement_id=f'plm:{sha}',
        measurement_sha256=sha,
    )


def build_normalization_observation(
    **kwargs: Any,
) -> NormalizationObservation:
    probe = NormalizationObservation.model_construct(
        **canonicalize_payload(
            NormalizationObservation,
            dict(
                schema_version=LOUDNESS_SCHEMA_VERSION,
                authority_version=LOUDNESS_AUTHORITY_VERSION,
                observation_id='',
                observation_sha256='',
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return NormalizationObservation(
        **probe.model_dump(exclude={'observation_id', 'observation_sha256'}),
        observation_id=f'norm:{sha}',
        observation_sha256=sha,
    )


def build_playback_gain_state(**kwargs: Any) -> PlaybackGainState:
    probe = PlaybackGainState.model_construct(
        **canonicalize_payload(
            PlaybackGainState,
            dict(
                schema_version=LOUDNESS_SCHEMA_VERSION,
                authority_version=LOUDNESS_AUTHORITY_VERSION,
                state_id='',
                state_sha256='',
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return PlaybackGainState(
        **probe.model_dump(exclude={'state_id', 'state_sha256'}),
        state_id=f'pgs:{sha}',
        state_sha256=sha,
    )


def build_loudness_matching_record(**kwargs: Any) -> LoudnessMatchingRecord:
    probe = LoudnessMatchingRecord.model_construct(
        **canonicalize_payload(
            LoudnessMatchingRecord,
            dict(
                schema_version=LOUDNESS_SCHEMA_VERSION,
                authority_version=LOUDNESS_AUTHORITY_VERSION,
                record_id='',
                record_sha256='',
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return LoudnessMatchingRecord(
        **probe.model_dump(exclude={'record_id', 'record_sha256'}),
        record_id=f'lmr:{sha}',
        record_sha256=sha,
    )


def seed_content_loudness_profiles(
    *, document_id: str, created_at_utc: str | None = None
) -> tuple[ContentLoudnessProfile, ...]:
    """Built-in profiles pinned to their standards registry entries
    (#599): BS.1770-5 production, 2026 draft research-only, AES77 and
    EBU R128 as independent external profiles."""
    created = created_at_utc or _utc_now()
    return (
        build_loudness_profile(
            document_id=document_id,
            standard_id='itu-r-bs1770',
            standard_edition='5',
            publisher='ITU-R',
            profile_label='ITU-R BS.1770-5 programme loudness + true peak',
            eligibility='production_current',
            algorithm_version='bs1770-5-k-gated-1',
            channel_config='multichannel_5_1',
            gating_semantics='absolute_-70_lufs_relative_-10_lu_400ms_75pct',
            true_peak_method='annex2_oversampled_min_4x',
            limitations=(
                'lfe_channel_excluded',
                'object_based_via_annex4_only_when_declared',
            ),
            created_at_utc=created,
        ),
        build_loudness_profile(
            document_id=document_id,
            standard_id='itu-r-bs1770',
            standard_edition='2026-draft',
            publisher='ITU-R',
            profile_label='ITU-R BS.1770 draft revision (2026 activity)',
            eligibility='research_only',
            algorithm_version='bs1770-draft-unreleased',
            channel_config='bs2051_extended',
            gating_semantics='draft_not_production',
            true_peak_method=None,
            limitations=(
                'draft_revision_not_published',
                'results_never_production_citations',
            ),
            created_at_utc=created,
        ),
        build_loudness_profile(
            document_id=document_id,
            standard_id='ebu-r128',
            standard_edition='v5.0',
            publisher='EBU',
            profile_label='EBU R128 v5.0 normalization profile',
            eligibility='production_current',
            algorithm_version='ebu-r128-v5-bs1770-based-1',
            channel_config='stereo',
            gating_semantics='bs1770_absolute_-70_relative_-10',
            true_peak_method='ebu_tech_3341_oversampled',
            distribution_target_lufs=-23.0,
            limitations=(
                'distribution_profile_not_room_target',
            ),
            created_at_utc=created,
        ),
        build_loudness_profile(
            document_id=document_id,
            standard_id='aes77',
            standard_edition='2023',
            publisher='AES',
            profile_label='AES77-2023 streaming/on-demand loudness',
            eligibility='production_current',
            algorithm_version='aes77-guidance-1',
            channel_config='stereo',
            gating_semantics='aes77_distribution_guidance',
            true_peak_method=None,
            limitations=(
                'no_device_playback_target_defined',
                'guidance_not_measurement_algorithm',
            ),
            created_at_utc=created,
        ),
    )


# ---------------------------------------------------------------------------
# Quantity-separation gates
# ---------------------------------------------------------------------------


def assert_quantity_separation(
    from_quantity: LoudnessQuantity,
    to_quantity: LoudnessQuantity,
    *,
    calibrated_chain_ref: str | None = None,
) -> None:
    """Fail closed on implicit conversions (issue §1/§9/§10): LUFS → SPL,
    programme loudness → capability, or anything → hearing dose requires
    an explicit persisted calibrated chain reference."""
    if from_quantity == to_quantity:
        return
    convertible_without_chain = {
        ('short_term_loudness', 'momentary_loudness'),
        ('momentary_loudness', 'short_term_loudness'),
    }
    if (from_quantity, to_quantity) in convertible_without_chain:
        return
    needs_chain = {
        'programme_integrated_loudness', 'short_term_loudness',
        'momentary_loudness', 'loudness_range', 'true_peak',
    }
    if from_quantity in needs_chain and calibrated_chain_ref is None:
        raise ValueError(
            f'cannot express {from_quantity} as {to_quantity} without an '
            'explicit calibrated playback-chain reference — content '
            'loudness is not room SPL, capability, or exposure'
        )


def evaluate_loudness_profile_eligibility(
    profile: ContentLoudnessProfile,
    *,
    content_config: LoudnessChannelConfig,
) -> tuple[LoudnessProfileEligibility, tuple[str, ...]]:
    """Eligibility gate: draft profiles are research-only; a stereo-only
    meter profile is ineligible for immersive assets (issue §6)."""
    if profile.eligibility != 'production_current':
        return profile.eligibility, ('profile_not_production_current',)
    immersive = content_config in (
        'bs2051_extended', 'object_based', 'channel_and_object'
    )
    if immersive and profile.channel_config in ('mono', 'stereo'):
        return 'research_only', (
            'immersive_content_requires_extended_or_object_profile',
        )
    return 'production_current', ()


def normalization_audit_diff(
    observation_on: NormalizationObservation,
    observation_off: NormalizationObservation,
    state_on: PlaybackGainState,
    state_off: PlaybackGainState,
) -> dict[str, float | None]:
    """Normalization audit (issue §11): source-side gain delta vs the
    measured in-room SPL delta — kept as reported evidence, never merged
    into a calibration verdict."""
    applied_delta = None
    if (
        observation_on.applied_gain_db is not None
        or observation_off.applied_gain_db is not None
    ):
        applied_delta = (observation_on.applied_gain_db or 0.0) - (
            observation_off.applied_gain_db or 0.0
        )
    spl_delta = None
    if (
        state_on.measured_in_room_spl_db is not None
        and state_off.measured_in_room_spl_db is not None
    ):
        spl_delta = (
            state_on.measured_in_room_spl_db
            - state_off.measured_in_room_spl_db
        )
    return {
        'applied_gain_delta_db': applied_delta,
        'measured_spl_delta_db': spl_delta,
        'master_volume_delta_db': (
            (state_on.master_volume_db or 0.0)
            - (state_off.master_volume_db or 0.0)
        ),
    }


__all__ = [
    'ContentIdentity',
    'ContentLoudnessProfile',
    'GATING_ABSOLUTE_LUFS',
    'GATING_BLOCK_S',
    'GATING_RELATIVE_LU',
    'K_HP_F0_HZ',
    'K_HP_Q',
    'K_SHELF_F0_HZ',
    'K_SHELF_GAIN_DB',
    'K_SHELF_Q',
    'LOUDNESS_AUTHORITY_VERSION',
    'LOUDNESS_OFFSET',
    'LOUDNESS_SCHEMA_VERSION',
    'LoudnessChannelConfig',
    'LoudnessMatchingRecord',
    'LoudnessProfileEligibility',
    'LoudnessQuantity',
    'LoudnessSourceClass',
    'NORMALIZATION_LABELS',
    'NormalizationMode',
    'NormalizationObservation',
    'PlaybackGainState',
    'ProgrammeLoudnessMeasurement',
    'QUANTITY_LABELS',
    'SOURCE_CLASS_LABELS',
    'assert_quantity_separation',
    'build_loudness_matching_record',
    'build_loudness_measurement',
    'build_loudness_profile',
    'build_normalization_observation',
    'build_playback_gain_state',
    'compute_integrated_loudness_lufs',
    'compute_true_peak_dbtp',
    'evaluate_loudness_profile_eligibility',
    'loudness_channel_weights',
    'normalization_audit_diff',
    'seed_content_loudness_profiles',
]
