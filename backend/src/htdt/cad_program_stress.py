"""Program-material stress authority (#1003).

Playback feasibility decisions (amplifier headroom, driver thermal load,
tactile demand) all start from *what the program material actually is* —
its spectral content, crest factor, temporal windows, channel activity and
duration. Until now the model had no first-class record for that: a
program profile was implicit in ad-hoc headroom checks, so scenarios could
not be reproduced and missing evidence could not be told apart from a
benign signal.

A :class:`ProgramStressProfile` is a named, versioned, hashed record of the
stress a program places on the playback chain:

- the **kind** of profile is explicit — standardized test signal (e.g.
  AES75 Music-Noise), user-authored engineering envelope, measured content
  statistics, imported vendor design profile, locally analyzed media, or
  ``unknown``;
- spectral stress is **band-resolved** — per-band RMS/peak envelopes with
  explicit crest factor, never a single broadband number that hides where
  the energy lives;
- temporal structure is explicit — instantaneous / short-term / long-term
  windows stay distinct fields, and duty/activity is recorded, not
  assumed;
- channel activity is explicit per channel — which channels carry energy
  and whether channel content is correlated is declared, not defaulted;
- loudness, true-peak and band-RMS stay **distinct quantities** — one is
  never silently substituted for another;
- the profile records its normalization basis and measurement uncertainty
  so a consumer can see what the numbers are relative to;
- there is deliberately **no overall stress score** — consumers combine
  profile fields with their own limits; an undetermined quantity is
  ``None``/``'unknown'``, never zero.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_video_geometry import EvaluationStatus
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash


PROGRAM_STRESS_AUTHORITY_VERSION = 'program-stress-1'

#: Where a program stress profile's contents came from.
ProgramStressKind = Literal[
    'standardized_test_signal',
    'user_authored_engineering_envelope',
    'measured_content_statistics',
    'imported_vendor_design_profile',
    'locally_analyzed_media',
    'unknown',
]

#: Temporal window a level figure describes. These are different physical
#: quantities — e.g. an instantaneous true-peak is never a long-term RMS.
StressTemporalWindow = Literal[
    'instantaneous',
    'short_term',
    'long_term',
    'program',
    'unknown',
]

#: What the dB values in a profile are measured against.
StressLevelReference = Literal[
    'dbfs',
    'db_spl',
    'dbu',
    'relative',
    'unknown',
]

#: Whether the channels carry the same energy at the same time. A
#: correlated multi-channel signal loads shared resources (power supply,
#: room gain) very differently from independent content.
ChannelCorrelation = Literal[
    'correlated',
    'independent',
    'partial',
    'unknown',
]






def _finite(value: object, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


class ProgramBandStress(BaseModel):
    """Stress envelope for one frequency band within one temporal window.

    ``crest_factor_db`` may be recorded explicitly (measured/declared) or
    left ``None`` — it is only *derived* from peak minus RMS when both are
    present, and even then the recorded value wins. An absent value means
    no crest evidence, never crest = 0.
    """

    model_config = ConfigDict(frozen=True)

    band_id: str = Field(min_length=1)
    low_hz: float | None = Field(default=None, gt=0.0)
    high_hz: float | None = Field(default=None, gt=0.0)
    center_hz: float | None = Field(default=None, gt=0.0)
    rms_level_db: float | None = None
    peak_level_db: float | None = None
    crest_factor_db: float | None = Field(default=None, ge=0.0)
    window: StressTemporalWindow = 'unknown'
    #: Fraction of the window the band carries program content (0..1).
    duty_cycle: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode='after')
    def valid_band(self) -> 'ProgramBandStress':
        if self.low_hz is None and self.high_hz is None and (
            self.center_hz is None
        ):
            raise ValueError(
                'program band requires frequency bounds or a center frequency'
            )
        if (self.low_hz is None) != (self.high_hz is None):
            raise ValueError('band low/high frequencies come in pairs')
        if self.low_hz is not None and self.high_hz is not None and (
            self.low_hz >= self.high_hz
        ):
            raise ValueError('band low_hz must be below high_hz')
        for name in (
            'rms_level_db',
            'peak_level_db',
            'crest_factor_db',
            'duty_cycle',
        ):
            value = getattr(self, name)
            if value is not None:
                _finite(value, field_name=name)
        if (
            self.crest_factor_db is None
            and self.rms_level_db is not None
            and self.peak_level_db is not None
        ):
            object.__setattr__(
                self,
                'crest_factor_db',
                self.peak_level_db - self.rms_level_db,
            )
        return self


class ProgramChannelActivity(BaseModel):
    """Recorded activity for one channel of the program.

    ``active_fraction`` is the share of program duration the channel is
    driven — silence on a surround channel must not read as stress.
    """

    model_config = ConfigDict(frozen=True)

    channel_id: str = Field(min_length=1)
    active: bool = True
    active_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    rms_level_db: float | None = None
    peak_level_db: float | None = None
    window: StressTemporalWindow = 'long_term'

    @model_validator(mode='after')
    def valid_activity(self) -> 'ProgramChannelActivity':
        for name in ('active_fraction', 'rms_level_db', 'peak_level_db'):
            value = getattr(self, name)
            if value is not None:
                _finite(value, field_name=name)
        if not self.active and (
            self.rms_level_db is not None or self.peak_level_db is not None
        ):
            raise ValueError(
                'an inactive channel cannot carry level figures'
            )
        return self


class ProgramTemporalLevels(BaseModel):
    """Whole-program levels for one temporal window.

    Keeps loudness-like (average) and true-peak quantities as separate
    fields — an integrated loudness is never a peak, and neither is a band
    RMS figure.
    """

    model_config = ConfigDict(frozen=True)

    window: StressTemporalWindow
    rms_level_db: float | None = None
    peak_level_db: float | None = None
    true_peak_level_db: float | None = None

    @model_validator(mode='after')
    def finite_levels(self) -> 'ProgramTemporalLevels':
        for name in ('rms_level_db', 'peak_level_db', 'true_peak_level_db'):
            value = getattr(self, name)
            if value is not None:
                _finite(value, field_name=name)
        return self


class ProgramStressProfile(BaseModel):
    """Versioned record of the stress a program places on the playback chain.

    Identity: ``profile_id`` + ``version`` + ``semantic_sha256`` over the
    canonical payload — the same program content recorded twice produces
    the same hash, so scenario evaluations are reproducible.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal[
        'program-stress-1'
    ] = PROGRAM_STRESS_AUTHORITY_VERSION
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    kind: ProgramStressKind = 'unknown'
    #: For standardized profiles — the exact standard + signal name
    #: (e.g. ``AES75`` / ``Music-Noise``), recorded verbatim.
    standardized_name: str | None = Field(default=None, min_length=1)
    standardized_version: str | None = Field(default=None, min_length=1)
    level_reference: StressLevelReference = 'unknown'
    bands: tuple[ProgramBandStress, ...] = ()
    windows: tuple[ProgramTemporalLevels, ...] = ()
    channel_activity: tuple[ProgramChannelActivity, ...] = ()
    channel_correlation: ChannelCorrelation = 'unknown'
    duration_s: float | None = Field(default=None, gt=0.0)
    #: What the profile is normalized to — e.g. 'as_recorded',
    #: 'referenced_to_reference_level', 'unity_gain'. Never inferred.
    normalization_basis: str | None = Field(default=None, min_length=1)
    #: Declared measurement/analysis uncertainty in dB, when known.
    uncertainty_db: float | None = Field(default=None, ge=0.0)
    #: Binding to the thermal authority whose evidence this profile is
    #: allowed to claim; unbound = no thermal evidence.
    thermal_evidence_ref: str | None = Field(default=None, min_length=1)
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_profile(self) -> 'ProgramStressProfile':
        if self.kind == 'standardized_test_signal' and not (
            self.standardized_name and self.standardized_version
        ):
            raise ValueError(
                'standardized profiles require standardized_name and '
                'standardized_version'
            )
        if self.duration_s is not None:
            _finite(self.duration_s, field_name='duration_s')
        if self.uncertainty_db is not None:
            _finite(self.uncertainty_db, field_name='uncertainty_db')
        band_ids = [band.band_id for band in self.bands]
        if len(set(band_ids)) != len(band_ids):
            raise ValueError('duplicate program band ids')
        channel_ids = [c.channel_id for c in self.channel_activity]
        if len(set(channel_ids)) != len(channel_ids):
            raise ValueError('duplicate channel activity entries')
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('ProgramStressProfile semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'profile_id': self.profile_id,
            'version': self.version,
            'kind': self.kind,
            'standardized_name': self.standardized_name,
            'standardized_version': self.standardized_version,
            'level_reference': self.level_reference,
            'bands': [band.model_dump(mode='json') for band in self.bands],
            'windows': [
                window.model_dump(mode='json') for window in self.windows
            ],
            'channel_activity': [
                channel.model_dump(mode='json')
                for channel in self.channel_activity
            ],
            'channel_correlation': self.channel_correlation,
            'duration_s': self.duration_s,
            'normalization_basis': self.normalization_basis,
            'uncertainty_db': self.uncertainty_db,
            'thermal_evidence_ref': self.thermal_evidence_ref,
            'provenance': [
                item.model_dump(mode='json') for item in self.provenance
            ],
        }

    def window_levels(
        self, window: StressTemporalWindow
    ) -> ProgramTemporalLevels | None:
        for entry in self.windows:
            if entry.window == window:
                return entry
        return None


def build_program_stress_profile(
    *,
    profile_id: str | None = None,
    version: str = '1',
    kind: ProgramStressKind = 'unknown',
    standardized_name: str | None = None,
    standardized_version: str | None = None,
    level_reference: StressLevelReference = 'unknown',
    bands: tuple[ProgramBandStress, ...] = (),
    windows: tuple[ProgramTemporalLevels, ...] = (),
    channel_activity: tuple[ProgramChannelActivity, ...] = (),
    channel_correlation: ChannelCorrelation = 'unknown',
    duration_s: float | None = None,
    normalization_basis: str | None = None,
    uncertainty_db: float | None = None,
    thermal_evidence_ref: str | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> ProgramStressProfile:
    payload: dict[str, Any] = {
        'authority_version': PROGRAM_STRESS_AUTHORITY_VERSION,
        'profile_id': profile_id or str(uuid4()),
        'version': version,
        'kind': kind,
        'standardized_name': standardized_name,
        'standardized_version': standardized_version,
        'level_reference': level_reference,
        'bands': bands,
        'windows': windows,
        'channel_activity': channel_activity,
        'channel_correlation': channel_correlation,
        'duration_s': duration_s,
        'normalization_basis': normalization_basis,
        'uncertainty_db': uncertainty_db,
        'thermal_evidence_ref': thermal_evidence_ref,
        'provenance': provenance,
    }
    provisional = ProgramStressProfile.model_construct(
        **payload,
        semantic_sha256='0' * 64,
    )
    return ProgramStressProfile(
        **payload,
        semantic_sha256=_hash(provisional.semantic_payload()),
    )


def build_standardized_program_stress_profile(
    *,
    standardized_name: str,
    standardized_version: str,
    bands: tuple[ProgramBandStress, ...],
    windows: tuple[ProgramTemporalLevels, ...] = (),
    channel_activity: tuple[ProgramChannelActivity, ...] = (),
    channel_correlation: ChannelCorrelation = 'unknown',
    duration_s: float | None = None,
    normalization_basis: str | None = None,
    level_reference: StressLevelReference = 'dbfs',
    profile_id: str | None = None,
    version: str = '1',
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> ProgramStressProfile:
    """Record an exact standardized profile (e.g. AES75 Music-Noise).

    The standard's name/version and its published band/window figures are
    recorded verbatim with provenance — a standardized profile is only
    exact when its figures come from the standard document itself.
    """
    return build_program_stress_profile(
        profile_id=profile_id,
        version=version,
        kind='standardized_test_signal',
        standardized_name=standardized_name,
        standardized_version=standardized_version,
        level_reference=level_reference,
        bands=bands,
        windows=windows,
        channel_activity=channel_activity,
        channel_correlation=channel_correlation,
        duration_s=duration_s,
        normalization_basis=normalization_basis,
        provenance=provenance,
    )


class ProgramStressCheck(BaseModel):
    """One readiness check on a program stress profile."""

    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    status: EvaluationStatus
    reason: str


class ProgramStressEvaluation(BaseModel):
    """Readiness report: what the profile can support, not a verdict score."""

    model_config = ConfigDict(frozen=True)

    evaluation_id: str = Field(min_length=1)
    profile_id: str
    profile_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    checks: tuple[ProgramStressCheck, ...]
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_evaluation(self) -> 'ProgramStressEvaluation':
        if self.evaluation_sha256 != _hash(self.identity_payload()):
            raise ValueError('program stress evaluation hash mismatch')
        expected = 'pse-' + self.evaluation_sha256[:24]
        if self.evaluation_id != expected:
            raise ValueError('program stress evaluation id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'authority_version': PROGRAM_STRESS_AUTHORITY_VERSION,
            'profile_id': self.profile_id,
            'profile_sha256': self.profile_sha256,
            'checks': [
                check.model_dump(mode='json') for check in self.checks
            ],
        }


def evaluate_program_stress(
    *,
    profile: ProgramStressProfile,
    known_thermal_authority_ids: tuple[str, ...] = (),
) -> ProgramStressEvaluation:
    """Report which evidence a program stress profile carries.

    - ``spectral_content_recorded``: band-resolved envelope present.
    - ``crest_factor_evidence``: at least one band carries a crest factor
      (recorded or derived) — headroom cannot be reasoned about without it.
    - ``temporal_windows_distinct``: window levels recorded without
      conflating instantaneous/short-term/long-term.
    - ``channel_activity_recorded``: per-channel activity declared.
    - ``duration_recorded``: program duration is part of stress.
    - ``level_reference_recorded``: dB values have a known basis.
    - ``thermal_evidence_bound``: a thermal authority must be bound and
      resolvable before thermal feasibility can be reasoned about — a
      missing or unresolvable ref keeps it UNKNOWN/FAIL.
    """

    checks: list[ProgramStressCheck] = []

    checks.append(
        ProgramStressCheck(
            check='spectral_content_recorded',
            status='PASS' if profile.bands else 'UNKNOWN',
            reason=(
                f'{len(profile.bands)} band(s) recorded'
                if profile.bands
                else 'no band-resolved stress envelope recorded'
            ),
        )
    )

    has_crest = any(band.crest_factor_db is not None for band in profile.bands)
    checks.append(
        ProgramStressCheck(
            check='crest_factor_evidence',
            status='PASS' if has_crest else 'UNKNOWN',
            reason=(
                'crest factor recorded on at least one band'
                if has_crest
                else 'no band carries crest factor evidence'
            ),
        )
    )

    declared_windows = {entry.window for entry in profile.windows}
    declared_windows.discard('unknown')
    checks.append(
        ProgramStressCheck(
            check='temporal_windows_distinct',
            status='PASS' if declared_windows else 'UNKNOWN',
            reason=(
                'temporal windows recorded: ' + ', '.join(sorted(declared_windows))
                if declared_windows
                else 'no temporal window levels recorded'
            ),
        )
    )

    checks.append(
        ProgramStressCheck(
            check='channel_activity_recorded',
            status='PASS' if profile.channel_activity else 'UNKNOWN',
            reason=(
                f'{len(profile.channel_activity)} channel(s) recorded'
                if profile.channel_activity
                else 'no per-channel activity recorded'
            ),
        )
    )

    checks.append(
        ProgramStressCheck(
            check='duration_recorded',
            status='PASS' if profile.duration_s is not None else 'UNKNOWN',
            reason=(
                f'duration {profile.duration_s}s'
                if profile.duration_s is not None
                else 'program duration not recorded'
            ),
        )
    )

    checks.append(
        ProgramStressCheck(
            check='level_reference_recorded',
            status=(
                'PASS' if profile.level_reference != 'unknown' else 'UNKNOWN'
            ),
            reason=(
                f'level reference: {profile.level_reference}'
                if profile.level_reference != 'unknown'
                else 'level reference not recorded — dB basis unknowable'
            ),
        )
    )

    if profile.thermal_evidence_ref is None:
        checks.append(
            ProgramStressCheck(
                check='thermal_evidence_bound',
                status='UNKNOWN',
                reason='no thermal evidence bound — thermal feasibility '
                'undetermined',
            )
        )
    else:
        resolved = profile.thermal_evidence_ref in set(
            known_thermal_authority_ids
        )
        checks.append(
            ProgramStressCheck(
                check='thermal_evidence_bound',
                status='PASS' if resolved else 'FAIL',
                reason=(
                    f"thermal evidence '{profile.thermal_evidence_ref}' resolves"
                    if resolved
                    else f"thermal evidence ref "
                    f"'{profile.thermal_evidence_ref}' cannot be resolved"
                ),
            )
        )

    probe = ProgramStressEvaluation.model_construct(
        evaluation_id='',
        profile_id=profile.profile_id,
        profile_sha256=profile.semantic_sha256,
        checks=tuple(checks),
        evaluation_sha256='',
    )
    digest = _hash(probe.identity_payload())
    return ProgramStressEvaluation(
        **probe.model_dump(
            mode='python',
            exclude={'evaluation_sha256', 'evaluation_id'},
        ),
        evaluation_id='pse-' + digest[:24],
        evaluation_sha256=digest,
    )


__all__ = [
    'ChannelCorrelation',
    'PROGRAM_STRESS_AUTHORITY_VERSION',
    'ProgramBandStress',
    'ProgramChannelActivity',
    'ProgramStressCheck',
    'ProgramStressEvaluation',
    'ProgramStressKind',
    'ProgramStressProfile',
    'ProgramTemporalLevels',
    'StressLevelReference',
    'StressTemporalWindow',
    'build_program_stress_profile',
    'build_standardized_program_stress_profile',
    'evaluate_program_stress',
]
