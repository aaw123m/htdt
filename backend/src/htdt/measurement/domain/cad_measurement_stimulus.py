"""Measurement excitation/stimulus authorities (#874).

Two sealed records model *what signal was played* when a measurement or
verification ran:

- :class:`CadMeasurementExcitationAsset` — a managed content-addressed raw
  excitation/test-signal file admitted through the same #834 managed-asset
  store as measurement raw assets. The record seals byte length, format
  metadata and declared provenance; the bytes are the identity.
- :class:`CadMeasurementStimulusProfile` — the typed stimulus reference a
  later measurement or replay pins: stimulus kind, optional excitation-asset
  and/or source-measurement bindings, and honest metadata (levels, band,
  duration, sample rate).

Honesty rules baked into the models:

- Digital signal level (``level_dbfs``/``peak_dbfs``) and acoustic level
  (``level_db_spl``) are separate fields — dBFS never implies SPL and vice
  versa. Kinds whose reference is a digital file (sweeps, noise, impulses)
  carry no SPL claim; acoustic-capable kinds may carry either axis but the
  fields stay independent.
- ``unknown_external`` and ``external`` carry only what the caller declared;
  they never resolve to a shared canonical authority, so they can never
  compare equal to each other or to anything else.
- Comparisons never treat same-kind as same-reference:
  ``stimulus_comparison`` returns ``MATCH`` only when the two profiles pin
  the same resolved reference (identical excitation content hash or the same
  source measurement dataset) and every metadata field populated on both
  sides is equal; a populated-vs-missing field degrades to ``UNKNOWN`` and a
  differing populated field to ``CONDITION_MISMATCH``.
- No standards-conformance fields exist: a ``channel_ident_sequence`` or
  ``av_sync_test_media`` profile records provenance, never an EBU/other
  conformance claim HTDT cannot verify.
"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from math import isfinite
from typing import Any, Literal, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ...managed_assets import MANAGED_ASSETS_DIRNAME
from ...canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash, canonicalize_payload
from ...clock import utc_now_iso as _utc_now


_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


# ---------------------------------------------------------------------------
# Stimulus kind vocabulary (#874)


MeasurementStimulusKind = Literal[
    'log_sweep',
    'fixed_sine',
    'pink_noise',
    'white_noise',
    'impulse',
    'mls',
    'speech',
    'program_material',
    'tone_burst',
    'channel_ident_sequence',
    'av_sync_test_media',
    'external',
    'unknown_external',
]

#: Kinds whose reference is a digital excitation file: digital level fields
#: may be populated, acoustic SPL never is — an SPL claim belongs to the
#: acoustic calibration authority, not to the file reference.
DIGITAL_STIMULUS_KINDS: frozenset[MeasurementStimulusKind] = frozenset(
    {
        'log_sweep',
        'fixed_sine',
        'pink_noise',
        'white_noise',
        'impulse',
        'mls',
        'tone_burst',
        'channel_ident_sequence',
        'av_sync_test_media',
    }
)

#: Kinds that can never prove a shared reference identity: two ``external``
#: or ``unknown_external`` profiles can describe the same real signal, but
#: nothing canonical distinguishes "same reference" from "same description".
UNBOUNDED_STIMULUS_KINDS: frozenset[MeasurementStimulusKind] = frozenset(
    {'external', 'unknown_external'}
)

MeasurementStimulusIntent = Literal[
    'measurement',
    'verification',
    'calibration',
    'diagnostic',
    'unknown',
]

StimulusComparisonVerdict = Literal['MATCH', 'CONDITION_MISMATCH', 'UNKNOWN']


class CadMeasurementExcitationAsset(BaseModel):
    """A managed raw excitation/test-signal file bound to one document.

    ``sha256``/``byte_length`` describe the admitted bytes; ``relative_path``
    is the content-addressed managed-asset path the bytes live at. The
    repository verifies them against the managed asset store at save and
    re-verifies on every read, so a record can never outlive or out-run its
    bytes.
    """

    model_config = ConfigDict(frozen=True)

    excitation_asset_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    byte_length: int = Field(ge=1)
    format: str | None = None
    duration_s: float | None = None
    sample_rate_hz: float | None = None
    channel_count: int | None = Field(default=None, ge=1)
    created_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    excitation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_asset(self) -> 'CadMeasurementExcitationAsset':
        _require_iso8601(self.created_at_utc, 'excitation asset created_at_utc')
        if self.duration_s is not None and (
            not isfinite(self.duration_s) or self.duration_s <= 0
        ):
            raise ValueError('excitation asset duration_s must be positive')
        if self.sample_rate_hz is not None and (
            not isfinite(self.sample_rate_hz) or self.sample_rate_hz <= 0
        ):
            raise ValueError('excitation asset sample_rate_hz must be positive')
        if self.excitation_sha256 != _hash(self.identity_payload()):
            raise ValueError('excitation asset hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'excitation_asset_id': self.excitation_asset_id,
            'document_id': self.document_id,
            'relative_path': self.relative_path,
            'filename': self.filename,
            'sha256': self.sha256,
            'byte_length': self.byte_length,
            'format': self.format,
            'duration_s': self.duration_s,
            'sample_rate_hz': self.sample_rate_hz,
            'channel_count': self.channel_count,
            'created_at_utc': self.created_at_utc,
            'provenance_json': self.provenance_json,
        }


class CadExcitationAssetBinding(BaseModel):
    """Exact id/seal/content binding for a persisted excitation asset.

    All three pins travel together: the id names the record, the seal hash
    proves the record's metadata, and the content hash names the admitted
    bytes. A profile binds all three so reference equality is provable.
    """

    model_config = ConfigDict(frozen=True)

    excitation_asset_id: str = Field(min_length=1)
    excitation_sha256: str = Field(pattern=_SHA256_PATTERN)
    sha256: str = Field(pattern=_SHA256_PATTERN)


def excitation_asset_binding(
    asset: CadMeasurementExcitationAsset,
) -> CadExcitationAssetBinding:
    return CadExcitationAssetBinding(
        excitation_asset_id=asset.excitation_asset_id,
        excitation_sha256=asset.excitation_sha256,
        sha256=asset.sha256,
    )


class CadStimulusProfileBinding(BaseModel):
    """Exact id/seal reference to a persisted stimulus profile."""

    model_config = ConfigDict(frozen=True)

    stimulus_profile_id: str = Field(min_length=1)
    stimulus_profile_sha256: str = Field(pattern=_SHA256_PATTERN)


def stimulus_profile_binding(
    profile: 'CadMeasurementStimulusProfile',
) -> CadStimulusProfileBinding:
    return CadStimulusProfileBinding(
        stimulus_profile_id=profile.stimulus_profile_id,
        stimulus_profile_sha256=profile.stimulus_profile_sha256,
    )


class CadMeasurementStimulusProfile(BaseModel):
    """Typed reference to the exact stimulus a measurement/verification ran.

    ``stimulus_kind`` names the signal class; provenance is bound exactly via
    ``excitation_asset`` (a managed raw file) and/or
    ``measurement_dataset_sha256`` (the measurement the stimulus derives
    from, e.g. the sweep a response was captured with). Neither binding is
    required to persist — an unbound profile stays honest but can never
    prove a shared reference, so comparisons against it stay ``UNKNOWN``.
    """

    model_config = ConfigDict(frozen=True)

    stimulus_profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    stimulus_kind: MeasurementStimulusKind
    excitation_asset: CadExcitationAssetBinding | None = None
    measurement_dataset_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    intent: MeasurementStimulusIntent = 'unknown'
    level_dbfs: float | None = None
    peak_dbfs: float | None = None
    level_db_spl: float | None = None
    duration_s: float | None = None
    sample_rate_hz: float | None = None
    channel_count: int | None = Field(default=None, ge=1)
    band_low_hz: float | None = None
    band_high_hz: float | None = None
    created_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    stimulus_profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_profile(self) -> 'CadMeasurementStimulusProfile':
        _require_iso8601(self.created_at_utc, 'stimulus profile created_at_utc')
        for label, value in (
            ('level_dbfs', self.level_dbfs),
            ('peak_dbfs', self.peak_dbfs),
            ('duration_s', self.duration_s),
            ('sample_rate_hz', self.sample_rate_hz),
            ('band_low_hz', self.band_low_hz),
            ('band_high_hz', self.band_high_hz),
        ):
            if value is not None and not isfinite(value):
                raise ValueError(f'stimulus profile {label} must be finite')
        if self.duration_s is not None and self.duration_s <= 0:
            raise ValueError('stimulus profile duration_s must be positive')
        if self.sample_rate_hz is not None and self.sample_rate_hz <= 0:
            raise ValueError('stimulus profile sample_rate_hz must be positive')
        if (
            self.band_low_hz is not None
            and self.band_high_hz is not None
            and not (0 < self.band_low_hz < self.band_high_hz)
        ):
            raise ValueError('stimulus profile band must satisfy low < high')
        if self.stimulus_kind in DIGITAL_STIMULUS_KINDS:
            if self.level_db_spl is not None:
                raise ValueError(
                    'digital stimulus references cannot claim an acoustic '
                    'SPL level — the SPL axis belongs to the acoustic '
                    'calibration authority'
                )
        if self.stimulus_profile_sha256 != _hash(self.identity_payload()):
            raise ValueError('stimulus profile hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'stimulus_profile_id': self.stimulus_profile_id,
            'document_id': self.document_id,
            'stimulus_kind': self.stimulus_kind,
            'excitation_asset': (
                self.excitation_asset.model_dump(mode='json')
                if self.excitation_asset is not None
                else None
            ),
            'measurement_dataset_sha256': self.measurement_dataset_sha256,
            'intent': self.intent,
            'level_dbfs': self.level_dbfs,
            'peak_dbfs': self.peak_dbfs,
            'level_db_spl': self.level_db_spl,
            'duration_s': self.duration_s,
            'sample_rate_hz': self.sample_rate_hz,
            'channel_count': self.channel_count,
            'band_low_hz': self.band_low_hz,
            'band_high_hz': self.band_high_hz,
            'created_at_utc': self.created_at_utc,
            'provenance_json': self.provenance_json,
        }


def build_excitation_asset(
    *,
    document_id: str,
    filename: str,
    sha256: str,
    byte_length: int,
    excitation_asset_id: str | None = None,
    relative_path: str | None = None,
    format: str | None = None,
    duration_s: float | None = None,
    sample_rate_hz: float | None = None,
    channel_count: int | None = None,
    created_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadMeasurementExcitationAsset:
    """Assemble a sealed excitation-asset record (bytes are admitted
    separately by the repository, which verifies them before persisting)."""
    payload: dict[str, Any] = {
        'excitation_asset_id': excitation_asset_id or str(uuid4()),
        'document_id': document_id,
        # Managed assets are content-addressed: the record names the exact
        # digest path the #834 store installs bytes at.
        'relative_path': relative_path or f'{MANAGED_ASSETS_DIRNAME}/{sha256}',
        'filename': filename,
        'sha256': sha256,
        'byte_length': byte_length,
        'format': format,
        'duration_s': duration_s,
        'sample_rate_hz': sample_rate_hz,
        'channel_count': channel_count,
        'created_at_utc': created_at_utc or _utc_now(),
        'provenance_json': provenance_json,
    }
    provisional = CadMeasurementExcitationAsset.model_construct(**canonicalize_payload(CadMeasurementExcitationAsset, dict(
        **payload,
        excitation_sha256='0' * 64,
    )))
    return CadMeasurementExcitationAsset(
        **payload,
        excitation_sha256=_hash(provisional.identity_payload()),
    )


def build_stimulus_profile(
    *,
    document_id: str,
    stimulus_kind: MeasurementStimulusKind,
    stimulus_profile_id: str | None = None,
    excitation_asset: CadExcitationAssetBinding | CadMeasurementExcitationAsset | None = None,
    measurement_dataset_sha256: str | None = None,
    intent: MeasurementStimulusIntent = 'unknown',
    level_dbfs: float | None = None,
    peak_dbfs: float | None = None,
    level_db_spl: float | None = None,
    duration_s: float | None = None,
    sample_rate_hz: float | None = None,
    channel_count: int | None = None,
    band_low_hz: float | None = None,
    band_high_hz: float | None = None,
    created_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadMeasurementStimulusProfile:
    """Assemble a sealed stimulus-profile authority record."""
    if isinstance(excitation_asset, CadMeasurementExcitationAsset):
        binding: CadExcitationAssetBinding | None = excitation_asset_binding(
            excitation_asset
        )
    else:
        binding = excitation_asset
    payload: dict[str, Any] = {
        'stimulus_profile_id': stimulus_profile_id or str(uuid4()),
        'document_id': document_id,
        'stimulus_kind': stimulus_kind,
        'excitation_asset': binding,
        'measurement_dataset_sha256': measurement_dataset_sha256,
        'intent': intent,
        'level_dbfs': level_dbfs,
        'peak_dbfs': peak_dbfs,
        'level_db_spl': level_db_spl,
        'duration_s': duration_s,
        'sample_rate_hz': sample_rate_hz,
        'channel_count': channel_count,
        'band_low_hz': band_low_hz,
        'band_high_hz': band_high_hz,
        'created_at_utc': created_at_utc or _utc_now(),
        'provenance_json': provenance_json,
    }
    provisional = CadMeasurementStimulusProfile.model_construct(**canonicalize_payload(CadMeasurementStimulusProfile, dict(
        **payload,
        stimulus_profile_sha256='0' * 64,
    )))
    return CadMeasurementStimulusProfile(
        **payload,
        stimulus_profile_sha256=_hash(provisional.identity_payload()),
    )


# ---------------------------------------------------------------------------
# Reference comparison


def stimulus_profiles_comparable(
    left: CadMeasurementStimulusProfile,
    right: CadMeasurementStimulusProfile,
) -> bool:
    """Whether two profiles can provably name the same reference.

    Same kind is never enough: the pair must agree on a canonical reference —
    identical bound excitation content (same asset record *and* byte hash) or
    the same source measurement dataset. ``external``/``unknown_external``
    kinds carry no canonical reference, so they never compare.
    """
    if left.stimulus_kind != right.stimulus_kind:
        return False
    if left.stimulus_kind in UNBOUNDED_STIMULUS_KINDS:
        return False
    left_asset = left.excitation_asset
    right_asset = right.excitation_asset
    if left_asset is not None and right_asset is not None:
        if (
            left_asset.excitation_asset_id == right_asset.excitation_asset_id
            and left_asset.excitation_sha256 == right_asset.excitation_sha256
            and left_asset.sha256 == right_asset.sha256
        ):
            return True
    if (
        left.measurement_dataset_sha256 is not None
        and left.measurement_dataset_sha256 == right.measurement_dataset_sha256
    ):
        return True
    return False


#: Metadata fields a comparison evaluates; each axis compares only against
#: itself (dBFS vs dBFS, SPL vs SPL — never across).
_STIMULUS_COMPARISON_FIELDS: tuple[str, ...] = (
    'intent',
    'level_dbfs',
    'peak_dbfs',
    'level_db_spl',
    'duration_s',
    'sample_rate_hz',
    'channel_count',
    'band_low_hz',
    'band_high_hz',
)


def stimulus_comparison(
    left: CadMeasurementStimulusProfile,
    right: CadMeasurementStimulusProfile,
) -> StimulusComparisonVerdict:
    """Verdict for whether two profiles describe the same stimulus condition.

    ``MATCH`` requires a provably shared reference plus equal populated
    metadata. A field populated on exactly one side means the condition
    cannot be established — ``UNKNOWN``. Two populated-but-different fields
    are a real condition mismatch. Profiles with no shared canonical
    reference stay ``UNKNOWN``; they are never a mismatch either, since no
    honest verdict exists.
    """
    if not stimulus_profiles_comparable(left, right):
        return 'UNKNOWN'
    verdict: StimulusComparisonVerdict = 'MATCH'
    for field in _STIMULUS_COMPARISON_FIELDS:
        left_value = getattr(left, field)
        right_value = getattr(right, field)
        if left_value == right_value:
            continue
        if left_value is None or right_value is None:
            verdict = 'UNKNOWN'
        else:
            return 'CONDITION_MISMATCH'
    return verdict


__all__ = [
    'CadExcitationAssetBinding',
    'CadMeasurementExcitationAsset',
    'CadMeasurementStimulusProfile',
    'CadStimulusProfileBinding',
    'DIGITAL_STIMULUS_KINDS',
    'MeasurementStimulusIntent',
    'MeasurementStimulusKind',
    'StimulusComparisonVerdict',
    'UNBOUNDED_STIMULUS_KINDS',
    'build_excitation_asset',
    'build_stimulus_profile',
    'excitation_asset_binding',
    'stimulus_comparison',
    'stimulus_profile_binding',
    'stimulus_profiles_comparable',
]
