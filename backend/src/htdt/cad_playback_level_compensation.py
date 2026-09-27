"""LC10/LC20: playback-level-dependent loudness compensation (#980).

A ``PlaybackLevelCompensationProfile`` is an immutable authority describing
how tonal (and separately spatial) compensation varies with playback level.
It is pinned to an exact ``ReferencePlaybackProfile`` — the level authority —
and an exact ``CadTargetCurveProfile`` it compensates. Deriving the active
target produces a new ``LevelCompensatedTarget`` record; the static target
profile is never mutated, and an opaque vendor model is preserved as
evidence, never evaluated into invented gains.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_calibration import CadTargetCurve
from .cad_playback_level import ReferenceProfileRef
from .r120_geometry_compiler import ExactExternalAuthorityRef
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest


LC_SCHEMA_VERSION = 1
LC_PROFILE_AUTHORITY_VERSION = 'lc10-level-compensation-1'
LC_TARGET_AUTHORITY_VERSION = 'lc20-level-compensated-target-1'
LC_INTERPOLATION_VERSION = 'level-pwl-db-v1'






LevelCompensationModelKind = Literal[
    'imported_vendor_profile',
    'user_authored_level_table',
    'explicit_parametric_model',
    'psychoacoustic_model_derived',
    'unknown_opaque',
]

#: The level input the profile compensates against. ``acoustic_level_db_spl``
#: is acoustic level at the seat; ``device_volume_indicator`` is an AVR
#: volume number — which only equals acoustic level when a level-calibration
#: authority (#733) exists. The kind is recorded, never silently upgraded.
LevelInputKind = Literal[
    'acoustic_level_db_spl',
    'device_volume_indicator',
]

LevelClampPolicy = Literal['clamp_to_domain', 'reject_outside_domain']

PsychoacousticScope = Literal['pure_tone_loudness', 'complex_sound_loudness']


class LevelCompensationCurvePoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    frequency_hz: float = Field(gt=0.0)
    gain_db: float

    @field_validator('gain_db')
    @classmethod
    def finite_gain(cls, value: float) -> float:
        number = float(value)
        if not isfinite(number):
            raise ValueError('compensation gain must be finite')
        return number


class LevelCompensationEntry(BaseModel):
    """One level row: tonal compensation curve at that playback level."""

    model_config = ConfigDict(frozen=True)

    level_db: float
    tonal_curve: tuple[LevelCompensationCurvePoint, ...] = Field(min_length=1)
    #: Optional separate spatial/channel-group compensation — kept distinct
    #: from tonal compensation, never merged into it.
    spatial_curve: tuple[LevelCompensationCurvePoint, ...] | None = None

    @field_validator('level_db')
    @classmethod
    def finite_level(cls, value: float) -> float:
        number = float(value)
        if not isfinite(number):
            raise ValueError('compensation level must be finite')
        return number

    @model_validator(mode='after')
    def valid_entry(self) -> 'LevelCompensationEntry':
        for name, curve in (
            ('tonal_curve', self.tonal_curve),
            ('spatial_curve', self.spatial_curve),
        ):
            if curve is None:
                continue
            frequencies = tuple(point.frequency_hz for point in curve)
            if any(b <= a for a, b in zip(frequencies, frequencies[1:])):
                raise ValueError(f'{name} frequencies must be increasing')
        return self


class LevelCompensationProvenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_kind: str = Field(min_length=1)
    source_name: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    source_reference: str | None = Field(default=None, min_length=1)
    source_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )


class PlaybackLevelCompensationProfile(BaseModel):
    """Immutable level-dependent compensation authority."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = LC_SCHEMA_VERSION
    authority_version: Literal[
        'lc10-level-compensation-1'
    ] = LC_PROFILE_AUTHORITY_VERSION

    profile_id: str = Field(pattern=r'^level-compensation:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    model_kind: LevelCompensationModelKind
    #: Exact pin of the playback-level authority this compensation anchors.
    reference_profile_ref: ReferenceProfileRef
    #: Exact pin of the static target curve authority being compensated.
    target_profile_id: str = Field(min_length=1)
    target_profile_version: str = Field(min_length=1)
    target_profile_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    level_input: LevelInputKind
    level_domain_db: tuple[float, float]
    clamp_policy: LevelClampPolicy
    interpolation: Literal['piecewise_linear_db'] = 'piecewise_linear_db'

    entries: tuple[LevelCompensationEntry, ...] = Field(min_length=1)

    #: Psychoacoustic derivation requires a pinned external authority plus a
    #: declared scope (e.g. ISO 226 only covers pure-tone equal loudness).
    psychoacoustic_authority: ExactExternalAuthorityRef | None = None
    psychoacoustic_scope: PsychoacousticScope | None = None

    provenance: tuple[LevelCompensationProvenance, ...] = ()
    algorithm_id: str = Field(min_length=1)
    algorithm_version: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_profile(self) -> 'PlaybackLevelCompensationProfile':
        low, high = self.level_domain_db
        if not (isfinite(low) and isfinite(high)) or high <= low:
            raise ValueError('level domain must be finite and increasing')
        levels = tuple(entry.level_db for entry in self.entries)
        if any(b <= a for a, b in zip(levels, levels[1:])):
            raise ValueError('compensation entry levels must be increasing')
        if min(levels) < low or max(levels) > high:
            raise ValueError('entry levels outside the declared domain')
        grid = self.entries[0].tonal_curve
        frequencies = tuple(point.frequency_hz for point in grid)
        for entry in self.entries:
            if tuple(
                point.frequency_hz for point in entry.tonal_curve
            ) != frequencies:
                raise ValueError(
                    'all entries must share one declared frequency grid'
                )
        if self.model_kind == 'psychoacoustic_model_derived':
            if (
                self.psychoacoustic_authority is None
                or self.psychoacoustic_scope is None
            ):
                raise ValueError(
                    'psychoacoustic_model_derived requires a pinned authority '
                    'and declared scope'
                )
        elif (
            self.psychoacoustic_authority is not None
            or self.psychoacoustic_scope is not None
        ):
            raise ValueError(
                'psychoacoustic authority pins belong to '
                'psychoacoustic_model_derived profiles only'
            )
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('level compensation semantic hash mismatch')
        if self.profile_id != f'level-compensation:{expected}':
            raise ValueError('level compensation id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'profile_id', 'semantic_sha256'},
        )


def build_level_compensation_profile(
    **kwargs: Any,
) -> PlaybackLevelCompensationProfile:
    candidate = PlaybackLevelCompensationProfile.model_construct(
        **kwargs,
        profile_id='level-compensation:' + '0' * 64,
        semantic_sha256='0' * 64,
    )
    digest = _digest(candidate.semantic_payload())
    return PlaybackLevelCompensationProfile(
        **kwargs,
        profile_id=f'level-compensation:{digest}',
        semantic_sha256=digest,
    )


LevelCompensatedState = Literal[
    'derived',
    'clamped_to_domain',
    'opaque_not_evaluable',
]


class LevelCompensatedTarget(BaseModel):
    """Derived active target at one level — a new record, not a mutation."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = LC_SCHEMA_VERSION
    authority_version: Literal[
        'lc20-level-compensated-target-1'
    ] = LC_TARGET_AUTHORITY_VERSION

    target_id: str = Field(pattern=r'^level-compensated-target:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    profile_id: str = Field(pattern=r'^level-compensation:[0-9a-f]{64}$')
    profile_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    target_profile_id: str = Field(min_length=1)
    target_profile_version: str = Field(min_length=1)
    target_profile_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    level_db_requested: float
    level_db_applied: float
    state: LevelCompensatedState
    #: (frequency_hz, compensation_db) actually applied at the level.
    compensation_points: tuple[tuple[float, float], ...]
    #: Active curve: static target level plus compensation, per frequency.
    active_target_points: tuple[tuple[float, float], ...]
    interpolation_version: str = LC_INTERPOLATION_VERSION

    @model_validator(mode='after')
    def valid_target(self) -> 'LevelCompensatedTarget':
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('level compensated target hash mismatch')
        if self.target_id != f'level-compensated-target:{expected}':
            raise ValueError('level compensated target id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'target_id', 'semantic_sha256'},
        )


def _curve_level_db(
    points: Sequence[tuple[float, float]],
    frequency_hz: float,
) -> float:
    """Piecewise-linear interpolation of ordered (frequency_hz, dB) pairs."""
    ordered = tuple(
        sorted(
            ((float(f), float(v)) for f, v in points),
            key=lambda pair: pair[0],
        )
    )
    if frequency_hz <= ordered[0][0]:
        return ordered[0][1]
    if frequency_hz >= ordered[-1][0]:
        return ordered[-1][1]
    for (f_a, v_a), (f_b, v_b) in zip(ordered, ordered[1:]):
        if f_a <= frequency_hz <= f_b:
            fraction = (frequency_hz - f_a) / (f_b - f_a)
            return v_a + fraction * (v_b - v_a)
    raise AssertionError('interpolation unreachable')


def _compensation_curve_at_level(
    profile: PlaybackLevelCompensationProfile,
    level_db: float,
) -> tuple[tuple[float, float], ...]:
    """Piecewise-linear compensation gain per declared frequency."""
    entries = profile.entries
    if len(entries) == 1 or level_db <= entries[0].level_db:
        selected = entries[0]
        curve = selected.tonal_curve
    elif level_db >= entries[-1].level_db:
        selected = entries[-1]
        curve = selected.tonal_curve
    else:
        lower = entries[0]
        upper = entries[-1]
        for left, right in zip(entries, entries[1:]):
            if left.level_db <= level_db <= right.level_db:
                lower, upper = left, right
                break
        fraction = (level_db - lower.level_db) / (
            upper.level_db - lower.level_db
        )
        return tuple(
            (
                point.frequency_hz,
                point.gain_db
                + fraction * (upper_point.gain_db - point.gain_db),
            )
            for point, upper_point in zip(
                lower.tonal_curve, upper.tonal_curve
            )
        )
    return tuple(
        (point.frequency_hz, point.gain_db) for point in curve
    )


def derive_level_compensated_target(
    profile: PlaybackLevelCompensationProfile,
    level_db: float,
    target_curve: CadTargetCurve,
) -> LevelCompensatedTarget:
    """Derive the exact active target at one level (#980 LC20).

    Returns a new immutable record; the static ``CadTargetCurveProfile`` is
    never mutated. An ``unknown_opaque`` profile preserves its declared
    evidence and reports 'opaque_not_evaluable' rather than fabricating a
    compensation curve.
    """
    level_db = _require_finite_level(level_db)
    low, high = profile.level_domain_db
    clamped = False
    applied = level_db
    if level_db < low or level_db > high:
        if profile.clamp_policy == 'reject_outside_domain':
            raise ValueError('level outside declared compensation domain')
        applied = min(max(level_db, low), high)
        clamped = True

    if profile.model_kind == 'unknown_opaque':
        payload = {
            'profile_id': profile.profile_id,
            'profile_sha256': profile.semantic_sha256,
            'target_profile_id': profile.target_profile_id,
            'target_profile_version': profile.target_profile_version,
            'target_profile_sha256': profile.target_profile_sha256,
            'level_db_requested': level_db,
            'level_db_applied': applied,
            'state': 'opaque_not_evaluable',
            'compensation_points': [],
            'active_target_points': [],
        }
        digest = _digest(
            {'schema_version': LC_SCHEMA_VERSION,
             'authority_version': LC_TARGET_AUTHORITY_VERSION,
             'interpolation_version': LC_INTERPOLATION_VERSION,
             **payload}
        )
        return LevelCompensatedTarget(
            **payload,
            target_id=f'level-compensated-target:{digest}',
            semantic_sha256=digest,
        )

    compensation = _compensation_curve_at_level(profile, applied)
    target_points = tuple(
        (point.frequency_hz, point.level_db) for point in target_curve.points
    )
    active = tuple(
        (
            frequency,
            _curve_level_db(target_points, frequency) + gain_db,
        )
        for frequency, gain_db in compensation
    )
    payload = {
        'profile_id': profile.profile_id,
        'profile_sha256': profile.semantic_sha256,
        'target_profile_id': profile.target_profile_id,
        'target_profile_version': profile.target_profile_version,
        'target_profile_sha256': profile.target_profile_sha256,
        'level_db_requested': level_db,
        'level_db_applied': applied,
        'state': 'clamped_to_domain' if clamped else 'derived',
        'compensation_points': [list(p) for p in compensation],
        'active_target_points': [list(p) for p in active],
    }
    digest = _digest(
        {'schema_version': LC_SCHEMA_VERSION,
         'authority_version': LC_TARGET_AUTHORITY_VERSION,
         'interpolation_version': LC_INTERPOLATION_VERSION,
         **payload}
    )
    return LevelCompensatedTarget(
        **payload,
        target_id=f'level-compensated-target:{digest}',
        semantic_sha256=digest,
    )


def _require_finite_level(level_db: float) -> float:
    number = float(level_db)
    if not isfinite(number):
        raise ValueError('level_db must be finite')
    return number


class LevelHeadroomEvaluation(BaseModel):
    """Worst-case compensation headroom against a device gain ceiling."""

    model_config = ConfigDict(frozen=True)

    target_id: str = Field(pattern=r'^level-compensated-target:[0-9a-f]{64}$')
    state: LevelCompensatedState
    max_compensation_boost_db: float
    device_max_boost_db: float | None
    headroom_margin_db: float | None
    feasible: bool
    reason: str


def evaluate_level_headroom(
    target: LevelCompensatedTarget,
    *,
    device_max_boost_db: float | None,
) -> LevelHeadroomEvaluation:
    """Check the *applied* compensation curve against the device ceiling.

    ``device_max_boost_db=None`` means the ceiling is unknown — not
    unlimited — so feasibility stays False ('cannot prove headroom').
    """
    boost = max(
        (float(gain) for _, gain in target.compensation_points),
        default=0.0,
    )
    if target.state == 'opaque_not_evaluable':
        return LevelHeadroomEvaluation(
            target_id=target.target_id,
            state=target.state,
            max_compensation_boost_db=0.0,
            device_max_boost_db=device_max_boost_db,
            headroom_margin_db=None,
            feasible=False,
            reason='opaque compensation cannot prove headroom',
        )
    if device_max_boost_db is None:
        return LevelHeadroomEvaluation(
            target_id=target.target_id,
            state=target.state,
            max_compensation_boost_db=boost,
            device_max_boost_db=None,
            headroom_margin_db=None,
            feasible=False,
            reason='device boost ceiling unknown',
        )
    margin = float(device_max_boost_db) - boost
    return LevelHeadroomEvaluation(
        target_id=target.target_id,
        state=target.state,
        max_compensation_boost_db=boost,
        device_max_boost_db=float(device_max_boost_db),
        headroom_margin_db=margin,
        feasible=margin >= 0.0,
        reason=(
            'compensation within device ceiling'
            if margin >= 0.0
            else 'compensation boost exceeds device ceiling'
        ),
    )
