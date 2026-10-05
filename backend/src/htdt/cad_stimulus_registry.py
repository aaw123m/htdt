"""Test-stimulus / calibration-asset registry (issue #608).

A measurement is not reproducible unless the *exact* stimulus that drove it
is identifiable, versioned and bound to the resulting evidence. This module
owns the immutable stimulus/test-asset registry and the eligibility gate a
measurement procedure applies before any result may claim a standard or a
calibrated condition.

Composition with existing authorities:

- :class:`CadMeasurementStimulusProfile` (#874) is the per-measurement
  *pin* of what played; the registry is the document-level catalogue of
  *what exists*. A profile may bind a registry entry by id+hash through
  :class:`StimulusMeasurementPin`; an unbound profile can never claim a
  registered stimulus.
- :class:`StandardsSourceRecord` (#807) is provenance/licensing authority;
  :class:`StandardProfileRef` here only *names* the exact standard profile
  an asset claims to satisfy — it never imports standard content.

Honesty rules (mechanically enforced):

- A ``stimulus_kind``/subtype label is never sufficient identity. Fixed
  files require a content hash (or a publisher checksum marked
  ``hash_pending`` — which degrades every strict eligibility verdict to
  ``INSUFFICIENT_EVIDENCE``); deterministic generators require a complete
  :class:`StimulusGeneratorSpec`; stochastic signals require a
  :class:`StochasticRealization` that states whether bit-exact replay is
  even claimable.
- AES75 Music-Noise eligibility requires the official publisher checksum
  to match — an arbitrary music-shaped noise file is ``INCOMPATIBLE``
  for the strict profile, never "close enough".
- IEC 60268-16 revision is normative: a 2011-spectrum asset presented to
  a 2020-profile requirement is ``WRONG_REVISION``.
- Delivered ≠ source: :class:`PlaybackPathReport` records every declared
  transformation; a bit-exact requirement with any declared or unknown
  transformation is ``TRANSFORMED_NOT_BIT_EXACT`` (or
  ``INSUFFICIENT_EVIDENCE`` when the path is unrecorded).
- ``stimulus_claim_allowed`` is the fail-closed gate: no registered pin
  means no calibrated/standard-compliance claim — the verdict is
  ``claim_blocked_no_stimulus_pin``, never an implicit pass.

Literature/standards basis (issue #608 + investigation): AES75-2023
(Music-Noise official 48/96 kHz assets + checksums; M-Noise renamed
Music-Noise in 2023), IEC 60268-16:2020+COR1:2025 (male speech spectrum
changed vs 2011 — 125/250 Hz reduced), AES17-2020, IEC 60268-21:2018
(measurement is inseparable from exact input conditions), Farina ESS
literature (sweep duration/amplitude/fade are part of stimulus identity).
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _require_finite(value: float, label: str) -> float:
    if not isfinite(value):
        raise ValueError(f'{label} must be finite')
    return value


def _sha_or_none(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if len(value) != 64:
        raise ValueError(f'{label} must be 64 hex characters')
    int(value, 16)
    return value


# ---------------------------------------------------------------------------
# Taxonomy (#608 §1)


StimulusOriginClass = Literal[
    'fixed_audio_file',
    'deterministic_generated_audio',
    'stochastic_generated_audio',
    'video_pattern',
    'audio_video_sync_asset',
    'immersive_metadata_test_asset',
    'external_device_generated',
    'user_provided',
    'standard_profiled_asset',
    'other',
]
"""Where the stimulus comes from — drives which identity block is required."""


StimulusSignalSubtype = Literal[
    'ess_log_sweep',
    'linear_sweep',
    'stepped_sine',
    'single_tone',
    'multitone',
    'pink_noise',
    'white_noise',
    'band_limited_noise',
    'impulse',
    'mls_like',
    'music_noise',
    'sti_stipa',
    'channel_id',
    'lfe_only',
    'loudness_reference',
    'pluge_clipping',
    'grayscale',
    'color_patch',
    'hdr_eotf',
    'latency_flash_click',
    'speech',
    'program_material',
    'other',
    'unknown',
]
"""Signal class. A subtype alone is never an identity — two ``ess_log_sweep``
assets with different duration/amplitude/fade are distinct stimuli."""


StochasticRealizationClass = Literal[
    'fixed_realization',
    'seeded_generation',
    'unseeded_live',
    'statistical_profile_only',
]
"""Whether a noise-class stimulus can be replayed bit-exactly at all."""


StimulusRightsClass = Literal[
    'open_redistributable',
    'standard_body_downloadable',
    'licensed_local_use',
    'user_supplied',
    'reference_only',
    'generated_locally',
    'proprietary_demo_not_redistributable',
    'unknown_rights',
]


DeliveryVerification = Literal[
    'source_asset_known',
    'delivery_config_known',
    'delivered_signal_verified',
]
"""Stimulus-verification ladder (#608 §18): a file hash proves the *source*
only; the delivered signal needs its own evidence class."""


PlaybackTransformationKind = Literal[
    'player_resampling',
    'volume_normalization',
    'sample_rate_conversion',
    'codec_transcode',
    'os_mixer',
    'bass_management',
    'upmix_downmix',
    'video_scaling_range',
    'tone_mapping',
    'hdmi_fallback',
    'other',
]


StimulusEligibilityVerdict = Literal[
    'ELIGIBLE',
    'ELIGIBLE_WITH_LIMITATIONS',
    'WRONG_REVISION',
    'WRONG_SAMPLE_RATE',
    'WRONG_LEVEL_OR_CREST_FACTOR',
    'TRANSFORMED_NOT_BIT_EXACT',
    'INCOMPATIBLE',
    'INSUFFICIENT_EVIDENCE',
]
"""Procedure-facing eligibility outcomes (#608 §16) — no silent
"similar enough" fallback exists in the taxonomy."""


StimulusPinState = Literal[
    'pinned',
    'unpinned',
]
"""Whether a measurement/dataset references a registered stimulus."""


# ---------------------------------------------------------------------------
# Identity blocks


class StimulusMediaFormat(BaseModel):
    """Exact media identity — audio and/or video fields as applicable."""

    model_config = ConfigDict(frozen=True)

    container: str | None = None
    codec: str | None = None
    sample_rate_hz: float | None = None
    bit_depth: int | None = None
    channel_count: int | None = Field(default=None, ge=1)
    duration_s: float | None = None
    raster_width_px: int | None = Field(default=None, ge=1)
    raster_height_px: int | None = Field(default=None, ge=1)
    frame_rate_hz: float | None = None
    chroma_subsampling: str | None = None
    video_bit_depth: int | None = None
    video_range: Literal['full', 'limited', 'unknown'] | None = None
    colorimetry: str | None = None
    transfer_eotf: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'StimulusMediaFormat':
        for label, value in (
            ('sample_rate_hz', self.sample_rate_hz),
            ('duration_s', self.duration_s),
            ('frame_rate_hz', self.frame_rate_hz),
        ):
            if value is not None:
                _require_finite(value, f'media format {label}')
                if value <= 0:
                    raise ValueError(f'media format {label} must be positive')
        if self.bit_depth is not None and self.bit_depth <= 0:
            raise ValueError('media format bit_depth must be positive')
        if self.video_bit_depth is not None and self.video_bit_depth <= 0:
            raise ValueError('media format video_bit_depth must be positive')
        return self


class StimulusGeneratorSpec(BaseModel):
    """Deterministic-generation identity (#608 §3): enough to reproduce the
    signal exactly. Every parameter that changes the output bytes is part of
    the identity — two ESS sweeps differing only in fade are distinct."""

    model_config = ConfigDict(frozen=True)

    generator_id: str = Field(min_length=1)
    generator_version: str = Field(min_length=1)
    signal_family: str = Field(min_length=1)
    start_frequency_hz: float | None = None
    end_frequency_hz: float | None = None
    sweep_law: Literal['linear', 'log', 'stepped', 'other'] | None = None
    sample_rate_hz: float
    duration_s: float
    amplitude_dbfs: float | None = None
    phase_deg: float | None = None
    fade_in_s: float | None = None
    fade_out_s: float | None = None
    pre_silence_s: float | None = None
    post_silence_s: float | None = None
    normalization_rule: str | None = None
    channel_assignment: str | None = None
    random_seed: int | None = None
    generated_content_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )

    @model_validator(mode='after')
    def _check(self) -> 'StimulusGeneratorSpec':
        _require_finite(self.sample_rate_hz, 'generator sample_rate_hz')
        _require_finite(self.duration_s, 'generator duration_s')
        if self.sample_rate_hz <= 0 or self.duration_s <= 0:
            raise ValueError('generator sample rate and duration must be positive')
        for label, value in (
            ('start_frequency_hz', self.start_frequency_hz),
            ('end_frequency_hz', self.end_frequency_hz),
            ('amplitude_dbfs', self.amplitude_dbfs),
            ('phase_deg', self.phase_deg),
            ('fade_in_s', self.fade_in_s),
            ('fade_out_s', self.fade_out_s),
            ('pre_silence_s', self.pre_silence_s),
            ('post_silence_s', self.post_silence_s),
        ):
            if value is not None:
                _require_finite(value, f'generator {label}')
        if (
            self.start_frequency_hz is not None
            and self.end_frequency_hz is not None
            and not (0 < self.start_frequency_hz < self.end_frequency_hz)
        ):
            raise ValueError(
                'generator sweep must satisfy 0 < start < end frequency'
            )
        return self


class StochasticRealization(BaseModel):
    """Noise-class reproducibility (#608 §4). ``unseeded_live`` and
    ``statistical_profile_only`` can never support a bit-identical replay
    claim — eligibility consumers must read ``bit_exact_replayable``."""

    model_config = ConfigDict(frozen=True)

    realization_class: StochasticRealizationClass
    rng_algorithm: str | None = None
    seed: int | None = None
    spectral_shaping: str | None = None
    crest_factor_db: float | None = None
    realization_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )

    @model_validator(mode='after')
    def _check(self) -> 'StochasticRealization':
        if self.realization_class == 'seeded_generation':
            if self.seed is None:
                raise ValueError('seeded generation requires a seed')
            if self.rng_algorithm is None:
                raise ValueError('seeded generation requires an RNG algorithm')
        if self.realization_class == 'fixed_realization' and (
            self.realization_sha256 is None
        ):
            raise ValueError(
                'a fixed realization requires its content hash — without it '
                'the realization is not identifiable'
            )
        if self.crest_factor_db is not None:
            _require_finite(self.crest_factor_db, 'crest factor')
        return self

    @property
    def bit_exact_replayable(self) -> bool:
        return self.realization_class in (
            'fixed_realization',
            'seeded_generation',
        )


class StimulusLevelSemantics(BaseModel):
    """Level/reference semantics (#608 §5): digital, electrical and acoustic
    axes are independent — dBFS never implies SPL."""

    model_config = ConfigDict(frozen=True)

    digital_peak_dbfs: float | None = None
    rms_dbfs: float | None = None
    crest_factor_db: float | None = None
    full_scale_convention: str | None = None
    analog_level_dbv: float | None = None
    acoustic_level_db_spl: float | None = None
    calibration_reference_id: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'StimulusLevelSemantics':
        for label, value in (
            ('digital_peak_dbfs', self.digital_peak_dbfs),
            ('rms_dbfs', self.rms_dbfs),
            ('crest_factor_db', self.crest_factor_db),
            ('analog_level_dbv', self.analog_level_dbv),
            ('acoustic_level_db_spl', self.acoustic_level_db_spl),
        ):
            if value is not None:
                _require_finite(value, f'level semantics {label}')
        if self.crest_factor_db is not None and self.crest_factor_db < 0:
            raise ValueError('crest factor cannot be negative')
        return self


class StimulusChannelIdentity(BaseModel):
    """Multichannel/immersive identity (#608 §6): a 7.1.4 *asset* and a
    7.1.4 *physical layout* are separate identities."""

    model_config = ConfigDict(frozen=True)

    layout_label: str | None = None
    speaker_role_map: str | None = None
    lfe_present: bool | None = None
    object_metadata_format: str | None = None
    expected_active_outputs: int | None = Field(default=None, ge=1)


class StandardProfileRef(BaseModel):
    """One exact standard profile an asset claims (e.g. AES75-2023
    Music-Noise 48 kHz). Revision is part of the identity — IEC 60268-16
    2011 vs 2020 spectra are not interchangeable."""

    model_config = ConfigDict(frozen=True)

    standard_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    asset_role: str | None = None
    publisher_checksum: str | None = None
    checksum_algorithm: str | None = None
    source_url: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'StandardProfileRef':
        if self.publisher_checksum is not None and (
            self.checksum_algorithm is None
        ):
            raise ValueError(
                'a publisher checksum requires its algorithm — an unlabelled '
                'checksum cannot be verified'
            )
        return self


class PlaybackTransformation(BaseModel):
    """One declared playback-path transformation (#608 §14)."""

    model_config = ConfigDict(frozen=True)

    kind: PlaybackTransformationKind
    detail: str | None = None
    bit_transparent: bool | None = None
    """Whether this transform preserves the exact delivered signal;
    ``None`` (unknown) cannot pass a bit-exact requirement."""


class PlaybackPathReport(BaseModel):
    """What happened between the registered asset and the DUT input."""

    model_config = ConfigDict(frozen=True)

    transformations: tuple[PlaybackTransformation, ...] = ()
    delivery_verification: DeliveryVerification = 'source_asset_known'
    playback_chain_notes: str | None = None


# ---------------------------------------------------------------------------
# Registry entry


class StimulusAssetEntry(BaseModel):
    """One immutable registered stimulus/test asset.

    ``stimulus_sha256`` seals the entire record; the registry refuses a
    same-id entry with different content (immutability), and an identical
    re-registration is a no-op.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    stimulus_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    origin_class: StimulusOriginClass
    subtype: StimulusSignalSubtype = 'unknown'
    media_format: StimulusMediaFormat | None = None
    generator_spec: StimulusGeneratorSpec | None = None
    stochastic: StochasticRealization | None = None
    level_semantics: StimulusLevelSemantics | None = None
    channel_identity: StimulusChannelIdentity | None = None
    standard_profiles: tuple[StandardProfileRef, ...] = ()
    rights: StimulusRightsClass = 'unknown_rights'
    publisher: str | None = None
    source_url: str | None = None
    content_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    signal_band_low_hz: float | None = None
    signal_band_high_hz: float | None = None
    purposes: tuple[str, ...] = ()
    known_limitations: tuple[str, ...] = ()
    registered_at_utc: str = Field(min_length=1)
    stimulus_sha256: str = Field(pattern=_SHA256_PATTERN)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='python', exclude={'stimulus_sha256'})

    @property
    def has_content_identity(self) -> bool:
        """Whether the asset's bytes are cryptographically pinned — by file
        hash, a fixed realization hash, or a generated-content hash."""
        if self.content_sha256 is not None:
            return True
        if (
            self.stochastic is not None
            and self.stochastic.realization_sha256 is not None
        ):
            return True
        if (
            self.generator_spec is not None
            and self.generator_spec.generated_content_sha256 is not None
        ):
            return True
        return False

    @model_validator(mode='after')
    def _check(self) -> 'StimulusAssetEntry':
        _require_iso8601(self.registered_at_utc, 'stimulus registered_at_utc')
        if self.origin_class in (
            'fixed_audio_file',
            'video_pattern',
            'audio_video_sync_asset',
            'immersive_metadata_test_asset',
            'standard_profiled_asset',
        ) and self.content_sha256 is None:
            raise ValueError(
                f'{self.origin_class} requires content_sha256 — a fixed '
                'asset without a content hash is not identifiable'
            )
        if self.origin_class == 'deterministic_generated_audio' and (
            self.generator_spec is None
        ):
            raise ValueError(
                'deterministic_generated_audio requires a generator spec'
            )
        if self.origin_class == 'stochastic_generated_audio' and (
            self.stochastic is None
        ):
            raise ValueError(
                'stochastic_generated_audio requires a realization spec'
            )
        for label, value in (
            ('signal_band_low_hz', self.signal_band_low_hz),
            ('signal_band_high_hz', self.signal_band_high_hz),
        ):
            if value is not None:
                _require_finite(value, f'stimulus {label}')
        if (
            self.signal_band_low_hz is not None
            and self.signal_band_high_hz is not None
            and not (0 < self.signal_band_low_hz < self.signal_band_high_hz)
        ):
            raise ValueError('stimulus signal band must satisfy 0 < low < high')
        if self.stimulus_sha256 != _hash(self.semantic_payload()):
            raise ValueError('stimulus asset semantic hash mismatch')
        return self


def build_stimulus_asset(
    *,
    stimulus_id: str,
    document_id: str,
    label: str,
    origin_class: StimulusOriginClass,
    subtype: StimulusSignalSubtype = 'unknown',
    media_format: StimulusMediaFormat | None = None,
    generator_spec: StimulusGeneratorSpec | None = None,
    stochastic: StochasticRealization | None = None,
    level_semantics: StimulusLevelSemantics | None = None,
    channel_identity: StimulusChannelIdentity | None = None,
    standard_profiles: tuple[StandardProfileRef, ...] = (),
    rights: StimulusRightsClass = 'unknown_rights',
    publisher: str | None = None,
    source_url: str | None = None,
    content_sha256: str | None = None,
    signal_band_low_hz: float | None = None,
    signal_band_high_hz: float | None = None,
    purposes: tuple[str, ...] = (),
    known_limitations: tuple[str, ...] = (),
    registered_at_utc: str | None = None,
) -> StimulusAssetEntry:
    """Assemble a sealed registry entry (the registry persists it)."""
    provisional = StimulusAssetEntry.model_construct(**canonicalize_payload(StimulusAssetEntry, dict(
        stimulus_id=stimulus_id,
        document_id=document_id,
        label=label,
        origin_class=origin_class,
        subtype=subtype,
        media_format=media_format,
        generator_spec=generator_spec,
        stochastic=stochastic,
        level_semantics=level_semantics,
        channel_identity=channel_identity,
        standard_profiles=tuple(standard_profiles),
        rights=rights,
        publisher=publisher,
        source_url=source_url,
        content_sha256=content_sha256,
        signal_band_low_hz=signal_band_low_hz,
        signal_band_high_hz=signal_band_high_hz,
        purposes=tuple(purposes),
        known_limitations=tuple(known_limitations),
        registered_at_utc=registered_at_utc or _utc_now(),
        stimulus_sha256='0' * 64,
    )))
    return StimulusAssetEntry(
        **provisional.model_dump(mode='python', exclude={'stimulus_sha256'}),
        stimulus_sha256=_hash(provisional.semantic_payload()),
    )


# ---------------------------------------------------------------------------
# Procedure eligibility (#608 §16)


class ProcedureStimulusRequirement(BaseModel):
    """What stimulus class a measurement procedure accepts.

    Every populated field is a constraint; unpopulated fields constrain
    nothing. ``bit_exact_delivery_required`` demands a declared-clean
    playback path (every transformation ``bit_transparent=True`` and at
    least ``delivery_config_known`` verification).
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    procedure_id: str = Field(min_length=1)
    required_standard_profiles: tuple[tuple[str, str], ...] = ()
    """(standard_id, revision) pairs — e.g. ('AES75', '2023')."""
    allowed_subtypes: tuple[StimulusSignalSubtype, ...] = ()
    required_origin_classes: tuple[StimulusOriginClass, ...] = ()
    required_sample_rate_hz: float | None = None
    minimum_sample_rate_hz: float | None = None
    required_band_low_hz: float | None = None
    required_band_high_hz: float | None = None
    max_crest_factor_db: float | None = None
    min_crest_factor_db: float | None = None
    bit_exact_delivery_required: bool = False
    required_channel_layout: str | None = None
    requirement_sha256: str = Field(pattern=_SHA256_PATTERN)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='python', exclude={'requirement_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'ProcedureStimulusRequirement':
        for label, value in (
            ('required_sample_rate_hz', self.required_sample_rate_hz),
            ('minimum_sample_rate_hz', self.minimum_sample_rate_hz),
            ('required_band_low_hz', self.required_band_low_hz),
            ('required_band_high_hz', self.required_band_high_hz),
            ('max_crest_factor_db', self.max_crest_factor_db),
            ('min_crest_factor_db', self.min_crest_factor_db),
        ):
            if value is not None:
                _require_finite(value, f'requirement {label}')
        if (
            self.required_band_low_hz is not None
            and self.required_band_high_hz is not None
            and not (0 < self.required_band_low_hz < self.required_band_high_hz)
        ):
            raise ValueError('required band must satisfy 0 < low < high')
        if self.requirement_sha256 != _hash(self.semantic_payload()):
            raise ValueError('procedure requirement hash mismatch')
        return self


def build_procedure_requirement(
    *,
    procedure_id: str,
    required_standard_profiles: tuple[tuple[str, str], ...] = (),
    allowed_subtypes: tuple[StimulusSignalSubtype, ...] = (),
    required_origin_classes: tuple[StimulusOriginClass, ...] = (),
    required_sample_rate_hz: float | None = None,
    minimum_sample_rate_hz: float | None = None,
    required_band_low_hz: float | None = None,
    required_band_high_hz: float | None = None,
    max_crest_factor_db: float | None = None,
    min_crest_factor_db: float | None = None,
    bit_exact_delivery_required: bool = False,
    required_channel_layout: str | None = None,
) -> ProcedureStimulusRequirement:
    provisional = ProcedureStimulusRequirement.model_construct(**canonicalize_payload(ProcedureStimulusRequirement, dict(
        procedure_id=procedure_id,
        required_standard_profiles=tuple(tuple(p) for p in required_standard_profiles),
        allowed_subtypes=tuple(allowed_subtypes),
        required_origin_classes=tuple(required_origin_classes),
        required_sample_rate_hz=required_sample_rate_hz,
        minimum_sample_rate_hz=minimum_sample_rate_hz,
        required_band_low_hz=required_band_low_hz,
        required_band_high_hz=required_band_high_hz,
        max_crest_factor_db=max_crest_factor_db,
        min_crest_factor_db=min_crest_factor_db,
        bit_exact_delivery_required=bit_exact_delivery_required,
        required_channel_layout=required_channel_layout,
        requirement_sha256='0' * 64,
    )))
    return ProcedureStimulusRequirement(
        **provisional.model_dump(mode='python', exclude={'requirement_sha256'}),
        requirement_sha256=_hash(provisional.semantic_payload()),
    )


class StimulusEligibilityRecord(BaseModel):
    """Sealed verdict: may this asset serve this procedure (on this
    playback path)? Verdicts only ever report what the evidence supports."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    eligibility_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    procedure_id: str = Field(min_length=1)
    stimulus_id: str = Field(min_length=1)
    stimulus_sha256: str = Field(pattern=_SHA256_PATTERN)
    requirement_sha256: str = Field(pattern=_SHA256_PATTERN)
    verdict: StimulusEligibilityVerdict
    reasons: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    eligibility_sha256: str = Field(pattern=_SHA256_PATTERN)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'eligibility_sha256', 'eligibility_id'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'StimulusEligibilityRecord':
        _require_iso8601(self.evaluated_at_utc, 'eligibility evaluated_at_utc')
        digest = _hash(self.semantic_payload())
        if self.eligibility_sha256 != digest:
            raise ValueError('stimulus eligibility hash mismatch')
        if self.eligibility_id != 'stimelig-' + digest[:24]:
            raise ValueError('stimulus eligibility id mismatch')
        return self


def evaluate_stimulus_eligibility(
    *,
    entry: StimulusAssetEntry,
    requirement: ProcedureStimulusRequirement,
    playback_path: PlaybackPathReport | None = None,
    document_id: str | None = None,
    evaluated_at_utc: str | None = None,
) -> StimulusEligibilityRecord:
    """Fail-closed eligibility evaluation.

    Check order matters for honesty: identity sufficiency first (no
    evidence → INSUFFICIENT_EVIDENCE), then incompatibility (subtype /
    origin / standard profile), then measurable mismatches (sample rate,
    band, level/crest), then the playback path.
    """
    reasons: list[str] = []
    doc = document_id or entry.document_id

    # --- incompatibility -------------------------------------------------
    if requirement.allowed_subtypes and (
        entry.subtype not in requirement.allowed_subtypes
    ):
        reasons.append(
            f'subtype {entry.subtype!r} not in allowed set '
            f'{sorted(requirement.allowed_subtypes)!r}'
        )
    if requirement.required_origin_classes and (
        entry.origin_class not in requirement.required_origin_classes
    ):
        reasons.append(
            f'origin class {entry.origin_class!r} not in required set '
            f'{sorted(requirement.required_origin_classes)!r}'
        )
    for standard_id, revision in requirement.required_standard_profiles:
        match = [
            p for p in entry.standard_profiles
            if p.standard_id == standard_id
        ]
        if not match:
            reasons.append(
                f'no standard profile {standard_id!r} declared on asset'
            )
            continue
        if not any(p.revision == revision for p in match):
            return _eligibility(
                entry=entry,
                requirement=requirement,
                document_id=doc,
                verdict='WRONG_REVISION',
                reasons=(
                    f'{standard_id} revision {revision!r} required; asset '
                    f'carries {sorted(p.revision for p in match)!r}',
                    *reasons,
                ),
                evaluated_at_utc=evaluated_at_utc,
            )
        # Publisher checksum: a strict standard profile must carry the
        # official checksum AND match the asset's own content hash class.
        profile = next(p for p in match if p.revision == revision)
        if profile.publisher_checksum is None:
            reasons.append(
                f'{standard_id} profile carries no publisher checksum — '
                'official-asset identity is unverifiable'
            )
        elif profile.checksum_algorithm == 'sha256' and (
            entry.content_sha256 is not None
            and profile.publisher_checksum.lower() != entry.content_sha256
        ):
            return _eligibility(
                entry=entry,
                requirement=requirement,
                document_id=doc,
                verdict='INCOMPATIBLE',
                reasons=(
                    f'{standard_id} publisher checksum '
                    f'{profile.publisher_checksum[:16]}… does not match the '
                    'registered content hash — the substituted file is not '
                    'the official asset',
                ),
                evaluated_at_utc=evaluated_at_utc,
            )
    if reasons:
        return _eligibility(
            entry=entry,
            requirement=requirement,
            document_id=doc,
            verdict='INCOMPATIBLE',
            reasons=tuple(reasons),
            evaluated_at_utc=evaluated_at_utc,
        )

    # --- measurable mismatches -------------------------------------------
    fmt = entry.media_format
    if (
        requirement.required_sample_rate_hz is not None
        or requirement.minimum_sample_rate_hz is not None
    ):
        if fmt is None or fmt.sample_rate_hz is None:
            return _eligibility(
                entry=entry,
                requirement=requirement,
                document_id=doc,
                verdict='INSUFFICIENT_EVIDENCE',
                reasons=('asset sample rate is not recorded',),
                evaluated_at_utc=evaluated_at_utc,
            )
        if (
            requirement.required_sample_rate_hz is not None
            and fmt.sample_rate_hz != requirement.required_sample_rate_hz
        ):
            return _eligibility(
                entry=entry,
                requirement=requirement,
                document_id=doc,
                verdict='WRONG_SAMPLE_RATE',
                reasons=(
                    f'sample rate {fmt.sample_rate_hz} Hz != required '
                    f'{requirement.required_sample_rate_hz} Hz',
                ),
                evaluated_at_utc=evaluated_at_utc,
            )
        if (
            requirement.minimum_sample_rate_hz is not None
            and fmt.sample_rate_hz < requirement.minimum_sample_rate_hz
        ):
            return _eligibility(
                entry=entry,
                requirement=requirement,
                document_id=doc,
                verdict='WRONG_SAMPLE_RATE',
                reasons=(
                    f'sample rate {fmt.sample_rate_hz} Hz below minimum '
                    f'{requirement.minimum_sample_rate_hz} Hz',
                ),
                evaluated_at_utc=evaluated_at_utc,
            )
    if (
        requirement.required_band_low_hz is not None
        or requirement.required_band_high_hz is not None
    ):
        if (
            entry.signal_band_low_hz is None
            or entry.signal_band_high_hz is None
        ):
            return _eligibility(
                entry=entry,
                requirement=requirement,
                document_id=doc,
                verdict='INSUFFICIENT_EVIDENCE',
                reasons=('asset signal band limits are not recorded',),
                evaluated_at_utc=evaluated_at_utc,
            )
        if (
            requirement.required_band_low_hz is not None
            and entry.signal_band_low_hz > requirement.required_band_low_hz
        ) or (
            requirement.required_band_high_hz is not None
            and entry.signal_band_high_hz < requirement.required_band_high_hz
        ):
            return _eligibility(
                entry=entry,
                requirement=requirement,
                document_id=doc,
                verdict='INCOMPATIBLE',
                reasons=(
                    f'asset band [{entry.signal_band_low_hz}, '
                    f'{entry.signal_band_high_hz}] Hz does not cover '
                    f'required [{requirement.required_band_low_hz}, '
                    f'{requirement.required_band_high_hz}] Hz',
                ),
                evaluated_at_utc=evaluated_at_utc,
            )
    if (
        requirement.max_crest_factor_db is not None
        or requirement.min_crest_factor_db is not None
    ):
        crest = None
        if entry.level_semantics is not None:
            crest = entry.level_semantics.crest_factor_db
        if crest is None and entry.stochastic is not None:
            crest = entry.stochastic.crest_factor_db
        if crest is None:
            return _eligibility(
                entry=entry,
                requirement=requirement,
                document_id=doc,
                verdict='INSUFFICIENT_EVIDENCE',
                reasons=('asset crest factor is not recorded',),
                evaluated_at_utc=evaluated_at_utc,
            )
        if (
            requirement.max_crest_factor_db is not None
            and crest > requirement.max_crest_factor_db
        ) or (
            requirement.min_crest_factor_db is not None
            and crest < requirement.min_crest_factor_db
        ):
            return _eligibility(
                entry=entry,
                requirement=requirement,
                document_id=doc,
                verdict='WRONG_LEVEL_OR_CREST_FACTOR',
                reasons=(
                    f'crest factor {crest} dB outside required '
                    f'[{requirement.min_crest_factor_db}, '
                    f'{requirement.max_crest_factor_db}] dB',
                ),
                evaluated_at_utc=evaluated_at_utc,
            )

    # --- content identity -------------------------------------------------
    if not entry.has_content_identity:
        return _eligibility(
            entry=entry,
            requirement=requirement,
            document_id=doc,
            verdict='INSUFFICIENT_EVIDENCE',
            reasons=(
                'asset carries no content hash — the exact stimulus bytes '
                'cannot be identified',
            ),
            evaluated_at_utc=evaluated_at_utc,
        )

    # --- playback path ----------------------------------------------------
    limitations: list[str] = []
    if requirement.bit_exact_delivery_required:
        if entry.stochastic is not None and not (
            entry.stochastic.bit_exact_replayable
        ):
            return _eligibility(
                entry=entry,
                requirement=requirement,
                document_id=doc,
                verdict='INCOMPATIBLE',
                reasons=(
                    f'stochastic realization class '
                    f'{entry.stochastic.realization_class!r} cannot support a '
                    'bit-exact replay requirement',
                ),
                evaluated_at_utc=evaluated_at_utc,
            )
        if playback_path is None:
            return _eligibility(
                entry=entry,
                requirement=requirement,
                document_id=doc,
                verdict='INSUFFICIENT_EVIDENCE',
                reasons=(
                    'playback path not reported — delivered stimulus '
                    'integrity is unproven',
                ),
                evaluated_at_utc=evaluated_at_utc,
            )
        transformed = [
            t.kind for t in playback_path.transformations
            if t.bit_transparent is not True
        ]
        if transformed:
                return _eligibility(
                    entry=entry,
                    requirement=requirement,
                    document_id=doc,
                    verdict='TRANSFORMED_NOT_BIT_EXACT',
                    reasons=(
                        'declared transformations are not bit-transparent: '
                        + ', '.join(transformed),
                    ),
                    evaluated_at_utc=evaluated_at_utc,
                )
        if playback_path.delivery_verification == 'source_asset_known':
            limitations.append(
                'only the source asset is verified — delivered-signal '
                'integrity is not observed'
            )

    verdict: StimulusEligibilityVerdict = (
        'ELIGIBLE_WITH_LIMITATIONS' if limitations else 'ELIGIBLE'
    )
    return _eligibility(
        entry=entry,
        requirement=requirement,
        document_id=doc,
        verdict=verdict,
        reasons=tuple(limitations),
        evaluated_at_utc=evaluated_at_utc,
    )


def _eligibility(
    *,
    entry: StimulusAssetEntry,
    requirement: ProcedureStimulusRequirement,
    document_id: str,
    verdict: StimulusEligibilityVerdict,
    reasons: tuple[str, ...],
    evaluated_at_utc: str | None,
) -> StimulusEligibilityRecord:
    probe = StimulusEligibilityRecord.model_construct(**canonicalize_payload(StimulusEligibilityRecord, dict(
        eligibility_id='',
        document_id=document_id,
        procedure_id=requirement.procedure_id,
        stimulus_id=entry.stimulus_id,
        stimulus_sha256=entry.stimulus_sha256,
        requirement_sha256=requirement.requirement_sha256,
        verdict=verdict,
        reasons=tuple(reasons),
        evaluated_at_utc=evaluated_at_utc or _utc_now(),
        eligibility_sha256='0' * 64,
    )))
    digest = _hash(probe.semantic_payload())
    return StimulusEligibilityRecord(
        **probe.model_dump(
            mode='python',
            exclude={'eligibility_sha256', 'eligibility_id'},
        ),
        eligibility_id='stimelig-' + digest[:24],
        eligibility_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Measurement pinning (#608 §17 + fail-closed claim gate)


class StimulusMeasurementPin(BaseModel):
    """Binds one measurement/dataset to one registered stimulus entry and,
    where reported, the playback path that delivered it."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    pin_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    measurement_ref: str = Field(min_length=1)
    """The pinned measurement/dataset identity (measurement id or dataset
    content hash — same ref space REV56-MEASEV budgets use)."""
    stimulus_id: str = Field(min_length=1)
    stimulus_sha256: str = Field(pattern=_SHA256_PATTERN)
    playback_path: PlaybackPathReport | None = None
    pinned_at_utc: str = Field(min_length=1)
    pin_sha256: str = Field(pattern=_SHA256_PATTERN)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='python', exclude={'pin_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'StimulusMeasurementPin':
        _require_iso8601(self.pinned_at_utc, 'pin pinned_at_utc')
        if self.pin_sha256 != _hash(self.semantic_payload()):
            raise ValueError('stimulus pin hash mismatch')
        return self


def build_stimulus_pin(
    *,
    pin_id: str,
    document_id: str,
    measurement_ref: str,
    entry: StimulusAssetEntry,
    playback_path: PlaybackPathReport | None = None,
    pinned_at_utc: str | None = None,
) -> StimulusMeasurementPin:
    provisional = StimulusMeasurementPin.model_construct(**canonicalize_payload(StimulusMeasurementPin, dict(
        pin_id=pin_id,
        document_id=document_id,
        measurement_ref=measurement_ref,
        stimulus_id=entry.stimulus_id,
        stimulus_sha256=entry.stimulus_sha256,
        playback_path=playback_path,
        pinned_at_utc=pinned_at_utc or _utc_now(),
        pin_sha256='0' * 64,
    )))
    return StimulusMeasurementPin(
        **provisional.model_dump(mode='python', exclude={'pin_sha256'}),
        pin_sha256=_hash(provisional.semantic_payload()),
    )


def measurement_stimulus_pin_state(
    pins: tuple[StimulusMeasurementPin, ...],
    measurement_ref: str,
) -> StimulusPinState:
    """Whether a measurement has any registered-stimulus pin."""
    return (
        'pinned'
        if any(p.measurement_ref == measurement_ref for p in pins)
        else 'unpinned'
    )


def stimulus_claim_allowed(
    *,
    pins: tuple[StimulusMeasurementPin, ...],
    measurement_ref: str,
    claim: str,
) -> tuple[bool, str]:
    """Fail-closed claim gate (#608): a calibrated / standard-compliance /
    reproducibility claim needs a registered stimulus pin.

    Returns ``(allowed, reason)`` — never an implicit pass.
    """
    matching = [p for p in pins if p.measurement_ref == measurement_ref]
    if not matching:
        return (
            False,
            'claim_blocked_no_stimulus_pin: the measurement is not bound '
            'to any registered stimulus asset — no calibrated or '
            f'standard-compliance claim ({claim!r}) is permitted',
        )
    return (
        True,
        f'stimulus pinned ({len(matching)} pin(s)); claim {claim!r} may '
        'proceed through its own gates',
    )


__all__ = [
    'DeliveryVerification',
    'PlaybackPathReport',
    'PlaybackTransformation',
    'PlaybackTransformationKind',
    'ProcedureStimulusRequirement',
    'StandardProfileRef',
    'StimulusAssetEntry',
    'StimulusChannelIdentity',
    'StimulusEligibilityRecord',
    'StimulusEligibilityVerdict',
    'StimulusGeneratorSpec',
    'StimulusLevelSemantics',
    'StimulusMeasurementPin',
    'StimulusMediaFormat',
    'StimulusOriginClass',
    'StimulusPinState',
    'StimulusRightsClass',
    'StimulusSignalSubtype',
    'StochasticRealization',
    'StochasticRealizationClass',
    'build_procedure_requirement',
    'build_stimulus_asset',
    'build_stimulus_pin',
    'evaluate_stimulus_eligibility',
    'measurement_stimulus_pin_state',
    'stimulus_claim_allowed',
]
