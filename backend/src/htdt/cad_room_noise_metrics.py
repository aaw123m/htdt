"""Versioned background-noise metric authority (#580).

The problem this module owns: ``NC``, ``NCB``, ``RNC``, ``RC Mark II`` and
``dBA`` are *different* versioned rating procedures over the same octave-band
evidence — not interchangeable labels for "the noise floor". CEDIA/CTA-RP22
v1.2 expresses its background-noise parameter in **NCB** (Balanced Noise
Criterion, Beranek 1989, ANSI S12.2-1995 lineage); ANSI/ASA S12.2-2019
covered A-weighted survey + expanded NC + RNC; ANSI/ASA S12.2-2026 revises
the standard to A-weighted level + NC curves. A newer edition must never
silently reinterpret an old RP22 project.

Literature basis (research verified 2026-10-05):

- Beranek, "Balanced noise-criterion (NCB) curves", JASA 86(2), 1989 —
  NCB rating = ANSI SIL (mean of 500/1000/2000/4000 Hz octave levels);
  spectral imbalance reported as rumble/hiss vs the SIL-rated contour;
  Table 1 curve values 16 Hz-8 kHz are reproduced below verbatim.
- ANSI/ASA S12.2-2019 clause 5.2 — NC designation via two-step procedure:
  the NC-(SIL) curve applies when no band exceeds it, else the tangency
  method (5.2.3) reports the highest touched curve + governing band;
  family NC-15..NC-70 tabulated over 16 Hz-8 kHz (Table 1 values as
  implemented by the published phonometry S12.2-2019 reference, which
  documents exact Table 1 fidelity).
- ANSI/ASA S12.2-2019 Annex D — RC Mark II: rating = round(LMF), LMF =
  mean(500/1000/2000 Hz); reference contour = -5 dB/octave keyed at
  1000 Hz, 31.5 Hz floored at 55 dB, 16 Hz equal to 31.5 Hz; spectral tags
  N/R/H from clause D.3 (rumble >5 dB at <=500 Hz, hiss >3 dB at
  >=1000 Hz).
- ANSI/ASA S12.2-2019 clause 5.3 — RNC for low-frequency fluctuating
  noise requires time-domain LF correction evidence; without it this
  authority rates the measurement as out-of-scope rather than guessing.
- ANSI/ASA S12.2-2026 — current edition; public scope lists A-weighted
  level + NC curves for representative steady-state HVAC noise.

Authority boundary:

- the raw banded measurement is canonical; every criterion rating is a
  *derived* record pinned to the exact profile id+hash+edition;
- profiles never substitute: an NCB-22 result is never quoted as NC-22;
- historical evaluations stay bound to their original profile — a new
  profile produces a parallel evaluation, never a rewrite;
- operating state (#573) is mandatory for qualified claims;
- threshold checks compose with the #577 guard-band semantics through
  the measurement/processing uncertainty carried on the record;
- this module does not own measurement-state stability (#573),
  uncertainty budgets (#572), decision rules (#577) or the RP22
  commissioning consumer (#579).
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical_json import (
    canonical_sha256 as _hash,
    canonicalize_payload,
)
from .clock import utc_now_iso as _utc_now


NOISE_METRIC_SCHEMA_VERSION = 1
NOISE_METRIC_AUTHORITY_VERSION = 'room-noise-metric-1'
NOISE_EVALUATION_AUTHORITY_VERSION = 'noise-metric-evaluation-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'
_MEASUREMENT_ID_PATTERN = r'^bnm:[0-9a-f]{64}$'
_PROFILE_ID_PATTERN = r'^rnprof:[0-9a-f]{64}$'
_EVALUATION_ID_PATTERN = r'^ncev:[0-9a-f]{64}$'


NoiseMetricFamily = Literal[
    'a_weighted_level',
    'nc',
    'ncb',
    'rnc',
    'rc_mark_ii',
    'custom_banded_limit',
    'other_versioned_profile',
]
"""Versioned criterion families. Never interchangeable: an ``nc`` rating
number is not an ``ncb`` rating number even when they coincide."""

NoiseTemporalClass = Literal[
    'steady_state',
    'low_frequency_fluctuating',
    'intermittent',
    'tonal',
    'impulsive',
    'mixed',
    'unknown',
]

NoiseSourcePresence = Literal[
    'off', 'low', 'normal', 'high', 'on', 'quiet', 'active',
    'unknown', 'not_applicable',
]

NoisePositionRole = Literal[
    'reference', 'listening_area', 'auxiliary', 'other',
]

NoiseLevelSemantics = Literal[
    'absolute_spl', 'relative', 'unknown',
]

NoiseBandSpec = Literal['octave', 'one_third_octave']

NoiseApplicability = Literal['in_scope', 'limited', 'out_of_scope']

NoiseProfileStatus = Literal[
    'profile_current',
    'profile_superseded_but_required_by_external_standard',
    'profile_historical',
    'profile_custom_project_requirement',
]

NoiseThresholdVerdict = Literal[
    'pass',
    'fail',
    'indeterminate_guard_band',
    'not_evaluated',
]


METRIC_FAMILY_LABELS: dict[str, str] = {
    'a_weighted_level': 'A特性レベル (dBA)',
    'nc': 'NC (Noise Criteria)',
    'ncb': 'NCB (Balanced Noise Criteria)',
    'rnc': 'RNC (Room Noise Criteria)',
    'rc_mark_ii': 'RC Mark II (Room Criteria)',
    'custom_banded_limit': 'カスタム帯域限界',
    'other_versioned_profile': 'その他版付きプロファイル',
}

TEMPORAL_CLASS_LABELS: dict[str, str] = {
    'steady_state': '定常',
    'low_frequency_fluctuating': '低周波変動',
    'intermittent': '間欠',
    'tonal': 'トーン性',
    'impulsive': '衝撃性',
    'mixed': '混合',
    'unknown': '不明',
}

PROFILE_STATUS_LABELS: dict[str, str] = {
    'profile_current': '現行プロファイル',
    'profile_superseded_but_required_by_external_standard': (
        '改訂済み（外部規格が要求）'
    ),
    'profile_historical': '歴史的プロファイル',
    'profile_custom_project_requirement': 'プロジェクト固有要件',
}

APPLICABILITY_LABELS: dict[str, str] = {
    'in_scope': '適用範囲内',
    'limited': '限定適用',
    'out_of_scope': '適用範囲外',
}

THRESHOLD_VERDICT_LABELS: dict[str, str] = {
    'pass': '適合',
    'fail': '不適合',
    'indeterminate_guard_band': '不確かさ内（判定保留）',
    'not_evaluated': '未評価',
}


# ---------------------------------------------------------------------------
# Published curve tables (verbatim, see module docstring for sources)
# ---------------------------------------------------------------------------

#: ANSI/ASA S12.2-2019 Table 1 octave bands, Hz.
S12_2_OCTAVE_BANDS_HZ: tuple[float, ...] = (
    16.0, 31.5, 63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0,
)

#: S12.2-2019 Table 1 NC curves, NC-15..NC-70 (designation = 1000 Hz value).
NC_2019_CURVES: tuple[tuple[str, tuple[float, ...]], ...] = (
    ('NC-15', (78.0, 61.0, 47.0, 36.0, 28.0, 22.0, 18.0, 14.0, 12.0, 11.0)),
    ('NC-20', (79.0, 63.0, 50.0, 40.0, 33.0, 26.0, 22.0, 20.0, 17.0, 16.0)),
    ('NC-25', (80.0, 65.0, 54.0, 44.0, 37.0, 31.0, 27.0, 24.0, 22.0, 22.0)),
    ('NC-30', (81.0, 68.0, 57.0, 48.0, 41.0, 35.0, 32.0, 29.0, 28.0, 27.0)),
    ('NC-35', (82.0, 71.0, 60.0, 52.0, 45.0, 40.0, 36.0, 34.0, 33.0, 32.0)),
    ('NC-40', (84.0, 74.0, 64.0, 56.0, 50.0, 44.0, 41.0, 39.0, 38.0, 37.0)),
    ('NC-45', (85.0, 76.0, 67.0, 60.0, 54.0, 49.0, 46.0, 44.0, 43.0, 42.0)),
    ('NC-50', (87.0, 79.0, 71.0, 64.0, 58.0, 54.0, 51.0, 49.0, 48.0, 47.0)),
    ('NC-55', (89.0, 82.0, 74.0, 67.0, 62.0, 58.0, 56.0, 54.0, 53.0, 52.0)),
    ('NC-60', (90.0, 85.0, 77.0, 71.0, 66.0, 63.0, 60.0, 59.0, 58.0, 57.0)),
    ('NC-65', (90.0, 88.0, 80.0, 75.0, 71.0, 68.0, 65.0, 64.0, 63.0, 62.0)),
    ('NC-70', (90.0, 90.0, 84.0, 79.0, 75.0, 72.0, 71.0, 70.0, 68.0, 68.0)),
)

#: Beranek 1989 Table 1 NCB curves (16 Hz-8 kHz); parenthesized Region-A
#: and asterisked Region-B vibration markers are carried as provenance.
NCB_1989_CURVES: tuple[tuple[str, tuple[float, ...]], ...] = (
    ('NCB-10', (78.0, 59.0, 43.0, 30.0, 21.0, 15.0, 12.0, 8.0, 5.0, 2.0)),
    ('NCB-15', (79.0, 61.0, 45.0, 34.0, 26.0, 20.0, 17.0, 13.0, 10.0, 7.0)),
    ('NCB-20', (80.0, 63.0, 49.0, 38.0, 30.0, 25.0, 22.0, 18.0, 15.0, 12.0)),
    ('NCB-25', (81.0, 66.0, 52.0, 42.0, 35.0, 30.0, 27.0, 23.0, 20.0, 17.0)),
    ('NCB-30', (82.0, 69.0, 55.0, 46.0, 40.0, 35.0, 32.0, 28.0, 25.0, 22.0)),
    ('NCB-35', (84.0, 71.0, 58.0, 50.0, 44.0, 40.0, 37.0, 33.0, 30.0, 27.0)),
    ('NCB-40', (85.0, 74.0, 62.0, 54.0, 49.0, 45.0, 42.0, 38.0, 35.0, 32.0)),
    ('NCB-45', (87.0, 76.0, 65.0, 58.0, 53.0, 50.0, 47.0, 43.0, 40.0, 37.0)),
    ('NCB-50', (89.0, 79.0, 69.0, 62.0, 58.0, 55.0, 52.0, 49.0, 46.0, 43.0)),
    ('NCB-55', (92.0, 82.0, 72.0, 67.0, 63.0, 60.0, 57.0, 54.0, 51.0, 48.0)),
    ('NCB-60', (94.0, 85.0, 76.0, 71.0, 67.0, 64.0, 62.0, 59.0, 56.0, 53.0)),
    ('NCB-65', (97.0, 88.0, 79.0, 75.0, 72.0, 69.0, 66.0, 64.0, 61.0, 58.0)),
)

#: NCB Region A/B low-frequency vibration markers (Beranek 1989 Table 1
#: annotations): 16 Hz levels are Region A on every curve; 31.5 Hz is
#: Region B for NCB-25..40 and Region A for NCB-45+; 63 Hz is Region B for
#: NCB-55+. Preserved as provenance, not silently turned into verdicts.
NCB_VIBRATION_MARKERS: tuple[tuple[str, str, float, str], ...] = tuple(
    (label, '16', levels[0], 'region_a')
    for label, levels in NCB_1989_CURVES
) + tuple(
    (label, '31.5', levels[1],
     'region_a' if int(label.split('-')[1]) >= 45 else 'region_b')
    for label, levels in NCB_1989_CURVES if int(label.split('-')[1]) >= 25
) + tuple(
    (label, '63', levels[2], 'region_b')
    for label, levels in NCB_1989_CURVES if int(label.split('-')[1]) >= 55
)

#: SIL bands per ANSI S3.5 / S12.2 clause 3.2.
SIL_BANDS_HZ: tuple[float, ...] = (500.0, 1000.0, 2000.0, 4000.0)
#: RC Mark II mid-frequency bands (S12.2-2019 Annex D clause D.4).
RC_MF_BANDS_HZ: tuple[float, ...] = (500.0, 1000.0, 2000.0)


def _finite(values: tuple[float, ...]) -> bool:
    return all(isfinite(float(v)) for v in values)


class NoiseOperatingState(BaseModel):
    """Operational room/device state that generated the noise (#573
    composition): a cold/silent commissioning room never substitutes for
    the normal-operating state a qualified noise claim needs."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    hvac: NoiseSourcePresence = 'unknown'
    projector: NoiseSourcePresence = 'unknown'
    rack_fans: NoiseSourcePresence = 'unknown'
    avr_equipment: NoiseSourcePresence = 'unknown'
    building_services: NoiseSourcePresence = 'unknown'
    doors_windows: Literal[
        'closed', 'open', 'mixed', 'unknown'
    ] = 'unknown'
    occupancy: Literal[
        'unoccupied', 'occupied', 'unknown'
    ] = 'unknown'
    lighting_dimmers: NoiseSourcePresence = 'unknown'
    external_sources: NoiseSourcePresence = 'unknown'
    detail: str | None = None
    #: Optional pin to a persisted #573 MeasurementStateSnapshot
    #: (``mss:<sha>``); either way the denormalized fields above travel
    #: with the measurement so a missing snapshot never erases state.
    state_snapshot_id: str | None = Field(default=None, min_length=1)

    @property
    def all_declared_off(self) -> bool:
        return all(
            getattr(self, field) in ('off', 'quiet', 'not_applicable')
            for field in (
                'hvac', 'projector', 'rack_fans', 'avr_equipment',
                'building_services', 'lighting_dimmers', 'external_sources',
            )
        )


class NoiseBandLevels(BaseModel):
    """One position's banded spectrum — the canonical evidence."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    band_center_hz: tuple[float, ...] = Field(min_length=1)
    band_level_db: tuple[float, ...] = Field(min_length=1)
    band_spec: NoiseBandSpec
    level_semantics: NoiseLevelSemantics = 'unknown'
    a_weighted_level_db: float | None = None
    lf_time_domain_correction_db: dict[str, float] | None = None
    """Optional S12.2 RNC time-domain LF corrections keyed by nominal band
    (``'16'``, ``'31.5'``, ``'63'``, ``'125'``) — surging/turbulence
    corrections from 100 ms sampled data per clause 5.3."""

    @model_validator(mode='after')
    def valid_bands(self) -> 'NoiseBandLevels':
        if len(self.band_center_hz) != len(self.band_level_db):
            raise ValueError('band centers and levels must be aligned')
        if not _finite(self.band_center_hz) or not _finite(self.band_level_db):
            raise ValueError('band values must be finite')
        if len(set(self.band_center_hz)) != len(self.band_center_hz):
            raise ValueError('duplicate band centers are not a spectrum')
        if self.a_weighted_level_db is not None and not isfinite(
            float(self.a_weighted_level_db)
        ):
            raise ValueError('a_weighted_level_db must be finite')
        return self


class NoisePositionSpectrum(BaseModel):
    """A single receiver position. Per-position evidence is preserved —
    aggregation never averages a localized source away."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    position_label: str = Field(min_length=1)
    role: NoisePositionRole = 'listening_area'
    spectrum: NoiseBandLevels
    note: str | None = None


class NoiseAcquisitionContext(BaseModel):
    """How the raw evidence was captured."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    instrument_id: str | None = None
    instrument_class: str | None = None
    calibration_ref: str | None = None
    method: Literal[
        'integrated_leq',
        'time_history',
        'max_hold',
        'statistical',
        'other',
        'unknown',
    ] = 'unknown'
    sampling_100ms: bool = False
    evidence_asset_refs: tuple[str, ...] = ()


class BackgroundNoiseMeasurement(BaseModel):
    """Sealed canonical banded measurement (issue §1).

    Content-derived id ``bnm:<sha256>`` over the semantic payload — the
    same evidence byte-for-byte never mints a second record."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = NOISE_METRIC_SCHEMA_VERSION
    authority_version: Literal[
        'room-noise-metric-1'
    ] = NOISE_METRIC_AUTHORITY_VERSION
    measurement_id: str = Field(pattern=_MEASUREMENT_ID_PATTERN)
    measurement_sha256: str = Field(pattern=_SHA256_PATTERN)

    document_id: str = Field(min_length=1)
    measurement_label: str | None = None
    acquisition: NoiseAcquisitionContext
    positions: tuple[NoisePositionSpectrum, ...] = Field(min_length=1)
    temporal_class: NoiseTemporalClass = 'unknown'
    tonal_indicator: bool | None = None
    operating_state: NoiseOperatingState
    uncertainty_db: float | None = None
    uncertainty_budget_ref: str | None = None
    captured_at_utc: str = Field(min_length=1)
    duration_s: float | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def valid_measurement(self) -> 'BackgroundNoiseMeasurement':
        expected = _hash(self.identity_payload())
        if self.measurement_sha256 != expected:
            raise ValueError('background noise measurement hash mismatch')
        if self.measurement_id != f'bnm:{expected}':
            raise ValueError('background noise measurement id mismatch')
        labels = [p.position_label for p in self.positions]
        if len(set(labels)) != len(labels):
            raise ValueError('position labels must be unique')
        if self.uncertainty_db is not None and not (
            isfinite(float(self.uncertainty_db)) and self.uncertainty_db >= 0
        ):
            raise ValueError('uncertainty_db must be a finite non-negative')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'measurement_id', 'measurement_sha256'},
        )


class NoiseImbalanceRule(BaseModel):
    """Spectral-shape qualifier rule owned by the profile — never an
    invented global threshold."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: Literal[
        'ncb_sil_balance', 'rc_mark_ii_tags', 'none'
    ] = 'none'
    lf_bound_hz: float | None = None
    hf_bound_hz: float | None = None
    lf_threshold_db: float | None = None
    hf_threshold_db: float | None = None


class NoiseCriterionCurve(BaseModel):
    """One labelled criterion curve (``NC-25``, ``NCB-25``...)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    label: str = Field(min_length=1)
    index: float
    levels_db: tuple[float, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def valid_curve(self) -> 'NoiseCriterionCurve':
        if not isfinite(float(self.index)):
            raise ValueError('curve index must be finite')
        if not _finite(self.levels_db):
            raise ValueError('curve levels must be finite')
        return self


class RoomNoiseMetricProfile(BaseModel):
    """A sealed, versioned criterion profile (issue §2/§12).

    Binding publisher + standard + edition + calculation version + curve
    table + temporal scope means an evaluation is reproducible forever,
    and a superseded family stays citable where an external standard
    (e.g. RP22 v1.2 -> NCB) requires it.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = NOISE_METRIC_SCHEMA_VERSION
    authority_version: Literal[
        'room-noise-metric-1'
    ] = NOISE_METRIC_AUTHORITY_VERSION
    profile_id: str = Field(pattern=_PROFILE_ID_PATTERN)
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    document_id: str = Field(min_length=1)
    metric_family: NoiseMetricFamily
    publisher: str = Field(min_length=1)
    standard_id: str = Field(min_length=1)
    standard_edition: str = Field(min_length=1)
    profile_label: str = Field(min_length=1)
    status: NoiseProfileStatus
    band_spec: NoiseBandSpec
    required_bands_hz: tuple[float, ...] = Field(min_length=1)
    curve_band_centers_hz: tuple[float, ...] = ()
    curves: tuple[NoiseCriterionCurve, ...] = ()
    temporal_scope: tuple[NoiseTemporalClass, ...] = Field(min_length=1)
    requires_absolute_levels: bool = True
    sil_bands_hz: tuple[float, ...] = ()
    mid_bands_hz: tuple[float, ...] = ()
    imbalance_rule: NoiseImbalanceRule = NoiseImbalanceRule()
    calculation_version: str = Field(min_length=1)
    curve_table_source: str | None = None
    external_standard_refs: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_profile(self) -> 'RoomNoiseMetricProfile':
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('room noise metric profile hash mismatch')
        if self.profile_id != f'rnprof:{expected}':
            raise ValueError('room noise metric profile id mismatch')
        if not _finite(self.required_bands_hz):
            raise ValueError('required bands must be finite')
        if self.curves:
            if len(self.curve_band_centers_hz) == 0:
                raise ValueError('curve band centers required with curves')
            for curve in self.curves:
                if len(curve.levels_db) != len(self.curve_band_centers_hz):
                    raise ValueError(
                        f'curve {curve.label} misaligned with band centers'
                    )
        if self.metric_family == 'a_weighted_level' and self.curves:
            raise ValueError('a-weighted profiles carry no criterion curves')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'profile_id', 'profile_sha256'},
        )

    def curve_for_index(self, index: float) -> tuple[float, ...] | None:
        """Band levels of the curve nearest ``index`` (linear interpolation
        between tabulated rows, matching the published methods)."""
        if not self.curves:
            return None
        rows = sorted(self.curves, key=lambda c: c.index)
        if index <= rows[0].index:
            return rows[0].levels_db
        if index >= rows[-1].index:
            return rows[-1].levels_db
        for lo, hi in zip(rows, rows[1:]):
            if lo.index <= index <= hi.index:
                if hi.index == lo.index:
                    return lo.levels_db
                t = (index - lo.index) / (hi.index - lo.index)
                return tuple(
                    a + (b - a) * t for a, b in zip(lo.levels_db, hi.levels_db)
                )
        return rows[-1].levels_db


class NoiseBandVerdict(BaseModel):
    """Per-band comparison against the applied curve — the limiting band
    stays visible instead of hiding behind a scalar."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    band_center_hz: float
    measured_level_db: float
    criterion_level_db: float | None = None
    exceedance_db: float | None = None
    governs_rating: bool = False
    band_outside_family: bool = False


class NoisePositionRating(BaseModel):
    """One position's derived rating under one profile."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    position_label: str = Field(min_length=1)
    role: NoisePositionRole = 'listening_area'
    method: str = Field(min_length=1)
    rating_label: str | None = None
    rating_value: float | None = None
    governing_band_hz: float | None = None
    sil_db: float | None = None
    qualifiers: tuple[str, ...] = ()
    band_verdicts: tuple[NoiseBandVerdict, ...] = ()
    applicability: NoiseApplicability = 'in_scope'
    applicability_reasons: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_rating(self) -> 'NoisePositionRating':
        if self.rating_value is not None and not isfinite(
            float(self.rating_value)
        ):
            raise ValueError('rating value must be finite')
        return self


class NoiseCriterionEvaluation(BaseModel):
    """Sealed derived rating (issue §3/§4): one measurement under one
    exact profile. Parallel evaluations are new records, never edits."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = NOISE_METRIC_SCHEMA_VERSION
    authority_version: Literal[
        'noise-metric-evaluation-1'
    ] = NOISE_EVALUATION_AUTHORITY_VERSION
    evaluation_id: str = Field(pattern=_EVALUATION_ID_PATTERN)
    evaluation_sha256: str = Field(pattern=_SHA256_PATTERN)

    document_id: str = Field(min_length=1)
    measurement_id: str = Field(pattern=_MEASUREMENT_ID_PATTERN)
    measurement_sha256: str = Field(pattern=_SHA256_PATTERN)
    profile_id: str = Field(pattern=_PROFILE_ID_PATTERN)
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    metric_family: NoiseMetricFamily
    standard_id: str = Field(min_length=1)
    standard_edition: str = Field(min_length=1)
    calculation_version: str = Field(min_length=1)
    temporal_class: NoiseTemporalClass

    applicability: NoiseApplicability
    applicability_reasons: tuple[str, ...] = ()
    position_results: tuple[NoisePositionRating, ...] = ()
    aggregate_basis: Literal[
        'worst_position', 'reference_position', 'none'
    ] = 'none'
    rating_label: str | None = None
    rating_value: float | None = None
    governing_band_hz: float | None = None
    qualifiers: tuple[str, ...] = ()

    target_label: str | None = None
    threshold_verdict: NoiseThresholdVerdict = 'not_evaluated'
    threshold_uncertainty_db: float | None = None

    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_evaluation(self) -> 'NoiseCriterionEvaluation':
        expected = _hash(self.identity_payload())
        if self.evaluation_sha256 != expected:
            raise ValueError('noise criterion evaluation hash mismatch')
        if self.evaluation_id != f'ncev:{expected}':
            raise ValueError('noise criterion evaluation id mismatch')
        if self.applicability == 'in_scope' and not self.position_results:
            raise ValueError('in-scope evaluations carry position results')
        if self.rating_value is not None and not isfinite(
            float(self.rating_value)
        ):
            raise ValueError('rating value must be finite')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'evaluation_id', 'evaluation_sha256'},
        )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def build_noise_measurement(**kwargs: Any) -> BackgroundNoiseMeasurement:
    probe = BackgroundNoiseMeasurement.model_construct(
        **canonicalize_payload(
            BackgroundNoiseMeasurement,
            dict(
                schema_version=NOISE_METRIC_SCHEMA_VERSION,
                authority_version=NOISE_METRIC_AUTHORITY_VERSION,
                measurement_id='',
                measurement_sha256='',
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return BackgroundNoiseMeasurement(
        **probe.model_dump(exclude={'measurement_id', 'measurement_sha256'}),
        measurement_id=f'bnm:{sha}',
        measurement_sha256=sha,
    )


def build_noise_metric_profile(**kwargs: Any) -> RoomNoiseMetricProfile:
    probe = RoomNoiseMetricProfile.model_construct(
        **canonicalize_payload(
            RoomNoiseMetricProfile,
            dict(
                schema_version=NOISE_METRIC_SCHEMA_VERSION,
                authority_version=NOISE_METRIC_AUTHORITY_VERSION,
                profile_id='',
                profile_sha256='',
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return RoomNoiseMetricProfile(
        **probe.model_dump(exclude={'profile_id', 'profile_sha256'}),
        profile_id=f'rnprof:{sha}',
        profile_sha256=sha,
    )


def _seal_evaluation(**kwargs: Any) -> NoiseCriterionEvaluation:
    probe = NoiseCriterionEvaluation.model_construct(
        **canonicalize_payload(
            NoiseCriterionEvaluation,
            dict(
                schema_version=NOISE_METRIC_SCHEMA_VERSION,
                authority_version=NOISE_EVALUATION_AUTHORITY_VERSION,
                evaluation_id='',
                evaluation_sha256='',
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return NoiseCriterionEvaluation(
        **probe.model_dump(exclude={'evaluation_id', 'evaluation_sha256'}),
        evaluation_id=f'ncev:{sha}',
        evaluation_sha256=sha,
    )


# ---------------------------------------------------------------------------
# Built-in profiles
# ---------------------------------------------------------------------------

_STEADY_SCOPE: tuple[NoiseTemporalClass, ...] = ('steady_state',)
_ANY_TEMPORAL: tuple[NoiseTemporalClass, ...] = (
    'steady_state',
    'low_frequency_fluctuating',
    'intermittent',
    'tonal',
    'impulsive',
    'mixed',
    'unknown',
)


def _curves(
    rows: tuple[tuple[str, tuple[float, ...]], ...]
) -> tuple[NoiseCriterionCurve, ...]:
    return tuple(
        NoiseCriterionCurve(
            label=label,
            index=float(label.split('-')[1]),
            levels_db=levels,
        )
        for label, levels in rows
    )


def seed_room_noise_metric_profiles(
    *, document_id: str, created_at_utc: str | None = None
) -> tuple[RoomNoiseMetricProfile, ...]:
    """Built-in profiles pinned to their exact standard editions.

    - ``ansi-s12-2-2026-nc`` — current production NC family (S12.2-2026;
      historic NC contour values, steady-state HVAC scope).
    - ``ansi-s12-2-2019-nc`` — the 2019 expanded NC method (historical).
    - ``ansi-s12-2-1995-ncb`` — Beranek NCB family; superseded in the S12.2
      lineage but still required by CEDIA/CTA-RP22 v1.2 — the exact
      profile #579 consumes.
    - ``ansi-s12-2-2019-rc-mark-ii`` — Annex D informative RC-II family.
    - ``ansi-s12-2-2019-rnc`` — RNC: needs LF time-domain corrections;
      curve table is intentionally unregistered so the evaluation fails
      closed instead of rating with fabricated contours.
    - ``ansi-s12-2-2026-a-weighted`` — A-weighted survey level.
    """
    created = created_at_utc or _utc_now()
    nc_2019 = _curves(NC_2019_CURVES)
    ncb_1989 = _curves(NCB_1989_CURVES)
    ncb_imbalance = NoiseImbalanceRule(
        kind='ncb_sil_balance',
        lf_bound_hz=1000.0,
        hf_bound_hz=1000.0,
        lf_threshold_db=3.0,
        hf_threshold_db=3.0,
    )
    rc_imbalance = NoiseImbalanceRule(
        kind='rc_mark_ii_tags',
        lf_bound_hz=500.0,
        hf_bound_hz=1000.0,
        lf_threshold_db=5.0,
        hf_threshold_db=3.0,
    )
    return (
        build_noise_metric_profile(
            document_id=document_id,
            metric_family='nc',
            publisher='ANSI/ASA',
            standard_id='ansi-asa-s12-2',
            standard_edition='2026',
            profile_label='ANSI/ASA S12.2-2026 NC curves',
            status='profile_current',
            band_spec='octave',
            required_bands_hz=S12_2_OCTAVE_BANDS_HZ,
            curve_band_centers_hz=S12_2_OCTAVE_BANDS_HZ,
            curves=nc_2019,
            temporal_scope=_STEADY_SCOPE,
            sil_bands_hz=SIL_BANDS_HZ,
            imbalance_rule=NoiseImbalanceRule(),
            calculation_version='s12-2-nc-two-step-1',
            curve_table_source=(
                'ANSI/ASA S12.2-2019 Table 1 octave-band values '
                '(16 Hz-8 kHz, NC-15..NC-70) — S12.2-2026 reverts NC to '
                'the historic contour family; the tabulated LF bands are '
                'carried from the published 2019 table pending licensed '
                '2026-text confirmation.'
            ),
            limitations=(
                'steady_state_hvac_assumption_only',
                'one_third_octave_nc_annexes_not_tabulated',
            ),
            created_at_utc=created,
        ),
        build_noise_metric_profile(
            document_id=document_id,
            metric_family='nc',
            publisher='ANSI/ASA',
            standard_id='ansi-asa-s12-2',
            standard_edition='2019',
            profile_label='ANSI/ASA S12.2-2019 expanded NC curves',
            status='profile_historical',
            band_spec='octave',
            required_bands_hz=S12_2_OCTAVE_BANDS_HZ,
            curve_band_centers_hz=S12_2_OCTAVE_BANDS_HZ,
            curves=nc_2019,
            temporal_scope=_STEADY_SCOPE,
            sil_bands_hz=SIL_BANDS_HZ,
            calculation_version='s12-2-nc-two-step-1',
            curve_table_source=(
                'ANSI/ASA S12.2-2019 Table 1 octave-band values '
                '(16 Hz-8 kHz, NC-15..NC-70).'
            ),
            limitations=('steady_state_hvac_assumption_only',),
            created_at_utc=created,
        ),
        build_noise_metric_profile(
            document_id=document_id,
            metric_family='ncb',
            publisher='ANSI/ASA',
            standard_id='ansi-asa-s12-2',
            standard_edition='1995',
            profile_label='NCB balanced noise criteria (Beranek 1989)',
            status='profile_superseded_but_required_by_external_standard',
            band_spec='octave',
            required_bands_hz=S12_2_OCTAVE_BANDS_HZ,
            curve_band_centers_hz=S12_2_OCTAVE_BANDS_HZ,
            curves=ncb_1989,
            temporal_scope=_STEADY_SCOPE,
            sil_bands_hz=SIL_BANDS_HZ,
            imbalance_rule=ncb_imbalance,
            calculation_version='ncb-sil-imbalance-1',
            curve_table_source=(
                'Beranek, JASA 86(2):650 Table 1 (1989) — NCB-10..NCB-65 '
                'curve values 16 Hz-8 kHz, Region A/B vibration markers '
                'preserved.'
            ),
            external_standard_refs=('cedia-cta-rp22@v1.2',),
            limitations=(
                'steady_state_assumption_only',
                'hiss_rule_simplified_to_threshold_exceedance',
            ),
            created_at_utc=created,
        ),
        build_noise_metric_profile(
            document_id=document_id,
            metric_family='rc_mark_ii',
            publisher='ANSI/ASA',
            standard_id='ansi-asa-s12-2',
            standard_edition='2019',
            profile_label='RC Mark II (S12.2-2019 Annex D)',
            status='profile_historical',
            band_spec='octave',
            required_bands_hz=S12_2_OCTAVE_BANDS_HZ[1:9],
            curve_band_centers_hz=S12_2_OCTAVE_BANDS_HZ,
            curves=(),
            temporal_scope=_STEADY_SCOPE,
            mid_bands_hz=RC_MF_BANDS_HZ,
            imbalance_rule=rc_imbalance,
            calculation_version='rc-mark-ii-closed-form-1',
            curve_table_source=(
                'Closed-form Annex D rule: -5 dB/octave keyed at 1000 Hz, '
                '31.5 Hz floored at 55 dB, 16 Hz equal to 31.5 Hz '
                '(reproduces Table D.1, RC-25..RC-50).'
            ),
            created_at_utc=created,
        ),
        build_noise_metric_profile(
            document_id=document_id,
            metric_family='rnc',
            publisher='ANSI/ASA',
            standard_id='ansi-asa-s12-2',
            standard_edition='2019',
            profile_label='RNC for LF fluctuating noise (S12.2-2019 5.3)',
            status='profile_historical',
            band_spec='octave',
            required_bands_hz=S12_2_OCTAVE_BANDS_HZ,
            curve_band_centers_hz=S12_2_OCTAVE_BANDS_HZ,
            curves=(),
            temporal_scope=('low_frequency_fluctuating', 'mixed'),
            requires_absolute_levels=True,
            calculation_version='rnc-lf-corrected-1',
            limitations=(
                'rnc_curve_table_not_registered',
                'requires_100ms_time_domain_lf_corrections',
            ),
            created_at_utc=created,
        ),
        build_noise_metric_profile(
            document_id=document_id,
            metric_family='a_weighted_level',
            publisher='ANSI/ASA',
            standard_id='ansi-asa-s12-2',
            standard_edition='2026',
            profile_label='A-weighted survey level (S12.2-2026)',
            status='profile_current',
            band_spec='octave',
            required_bands_hz=S12_2_OCTAVE_BANDS_HZ[:1],
            temporal_scope=_ANY_TEMPORAL,
            requires_absolute_levels=True,
            calculation_version='a-weighted-survey-1',
            limitations=('single_number_no_spectral_diagnostics',),
            created_at_utc=created,
        ),
    )


# ---------------------------------------------------------------------------
# Rating engine — per-family procedures, never cross-family substitution
# ---------------------------------------------------------------------------


def _aligned_levels(
    spectrum: NoiseBandLevels,
    required_bands_hz: tuple[float, ...],
    *,
    tolerance: float = 0.03,
) -> tuple[dict[float, float], list[float]]:
    """Map measured levels onto required band centers (nominal spellings
    within 3% accepted, matching published alignment tolerance)."""
    levels: dict[float, float] = {}
    missing: list[float] = []
    for band in required_bands_hz:
        best: tuple[float, float] | None = None
        for center, level in zip(
            spectrum.band_center_hz, spectrum.band_level_db
        ):
            rel = abs(center - band) / band
            if rel <= tolerance and (best is None or rel < best[0]):
                best = (rel, level)
        if best is None:
            missing.append(band)
        else:
            levels[band] = best[1]
    return levels, missing


def _sil(levels: dict[float, float], bands: tuple[float, ...]) -> float | None:
    if any(b not in levels for b in bands):
        return None
    return sum(levels[b] for b in bands) / len(bands)


def _nc_position_rating(
    position: NoisePositionSpectrum,
    profile: RoomNoiseMetricProfile,
) -> NoisePositionRating:
    """S12.2 NC two-step: NC-(SIL) designation when nothing exceeds the
    SIL-selected curve; else tangency rating + governing band."""
    levels, missing = _aligned_levels(
        position.spectrum, profile.required_bands_hz
    )
    reasons = [f'missing_band_{b:g}_hz' for b in missing]
    sil = _sil(levels, profile.sil_bands_hz or SIL_BANDS_HZ)
    if sil is None:
        return NoisePositionRating(
            position_label=position.position_label,
            role=position.role,
            method='nc_two_step',
            applicability='limited',
            applicability_reasons=tuple(
                ['sil_bands_missing'] + reasons
            ),
        )
    curves = sorted(profile.curves, key=lambda c: c.index)
    curve_bands = profile.curve_band_centers_hz
    # Fractional tangency index per band via piecewise interpolation.
    band_indices: list[tuple[float, float]] = []
    for i, band in enumerate(curve_bands):
        if band not in levels:
            continue
        level = levels[band]
        if level <= curves[0].levels_db[i]:
            band_indices.append((band, curves[0].index))
        elif level > curves[-1].levels_db[i]:
            band_indices.append(
                (band, curves[-1].index + (level - curves[-1].levels_db[i]))
            )
        else:
            for lo, hi in zip(curves, curves[1:]):
                if lo.levels_db[i] <= level <= hi.levels_db[i]:
                    span = hi.levels_db[i] - lo.levels_db[i]
                    frac = 0.0 if span == 0 else (level - lo.levels_db[i]) / span
                    band_indices.append(
                        (band, lo.index + frac * (hi.index - lo.index))
                    )
                    break
            else:
                band_indices.append((band, curves[-1].index))
    tangency = max((idx for _b_, idx in band_indices), default=None)
    governing = (
        max(band_indices, key=lambda kv: kv[1])[0] if band_indices else None
    )
    sil_curve = profile.curve_for_index(round(sil))
    exceeded = False
    if sil_curve is not None:
        for i, band in enumerate(curve_bands):
            if band in levels and levels[band] > sil_curve[i]:
                exceeded = True
                break
    verdicts = tuple(
        NoiseBandVerdict(
            band_center_hz=band,
            measured_level_db=levels[band],
            criterion_level_db=(
                sil_curve[i] if sil_curve is not None else None
            ),
            exceedance_db=(
                levels[band] - sil_curve[i]
                if sil_curve is not None else None
            ),
            governs_rating=(band == governing and exceeded),
            band_outside_family=False,
        )
        for i, band in enumerate(curve_bands)
        if band in levels
    )
    above_family = any(
        levels[band] > curves[-1].levels_db[i]
        for i, band in enumerate(curve_bands)
        if band in levels
    )
    below_family = all(
        levels[band] < curves[0].levels_db[i]
        for i, band in enumerate(curve_bands)
        if band in levels
    )
    if not exceeded:
        rating_value = round(sil)
        label = f'NC-{int(rating_value)}'
        method = 'sil_designation'
        governing = None
    elif tangency is None:
        return NoisePositionRating(
            position_label=position.position_label,
            role=position.role,
            method='nc_two_step',
            applicability='limited',
            applicability_reasons=tuple(reasons + ['no_rated_bands']),
            sil_db=sil,
        )
    elif above_family:
        rating_value = None
        label = f'>NC-{int(curves[-1].index)}'
        method = 'tangency'
        reasons.append('above_curve_family')
    elif below_family:
        rating_value = None
        label = f'<NC-{int(curves[0].index)}'
        method = 'sil_designation'
        reasons.append('below_curve_family')
    else:
        rating_value = tangency
        label = (
            f'NC-{int(round(tangency))}'
            + (f' ({governing:g} Hz)' if governing is not None else '')
        )
        method = 'tangency'
    return NoisePositionRating(
        position_label=position.position_label,
        role=position.role,
        method=method,
        rating_label=label,
        rating_value=(
            float(rating_value) if rating_value is not None else None
        ),
        governing_band_hz=governing,
        sil_db=sil,
        band_verdicts=verdicts,
        applicability='in_scope' if not missing else 'limited',
        applicability_reasons=tuple(reasons),
    )


def _ncb_position_rating(
    position: NoisePositionSpectrum,
    profile: RoomNoiseMetricProfile,
) -> NoisePositionRating:
    """Beranek NCB: rating = SIL; rumble/hiss qualifiers vs the
    SIL-rated contour per the profile's imbalance rule."""
    levels, missing = _aligned_levels(
        position.spectrum, profile.required_bands_hz
    )
    reasons = [f'missing_band_{b:g}_hz' for b in missing]
    sil = _sil(levels, profile.sil_bands_hz or SIL_BANDS_HZ)
    if sil is None:
        return NoisePositionRating(
            position_label=position.position_label,
            role=position.role,
            method='ncb_sil',
            applicability='limited',
            applicability_reasons=tuple(
                ['sil_bands_missing'] + reasons
            ),
        )
    rating = round(sil)
    curve = profile.curve_for_index(float(rating))
    qualifiers: list[str] = []
    lf_excess = hf_excess = 0.0
    rule = profile.imbalance_rule
    verdicts: list[NoiseBandVerdict] = []
    if curve is not None:
        for i, band in enumerate(profile.curve_band_centers_hz):
            if band not in levels:
                continue
            excess = levels[band] - curve[i]
            verdicts.append(
                NoiseBandVerdict(
                    band_center_hz=band,
                    measured_level_db=levels[band],
                    criterion_level_db=curve[i],
                    exceedance_db=excess,
                    governs_rating=False,
                )
            )
            if rule.lf_bound_hz is not None and band < rule.lf_bound_hz:
                lf_excess = max(lf_excess, excess)
            elif rule.hf_bound_hz is not None and band >= rule.hf_bound_hz:
                hf_excess = max(hf_excess, excess)
        if (
            rule.lf_threshold_db is not None
            and lf_excess > rule.lf_threshold_db
        ):
            qualifiers.append('rumble')
        if (
            rule.hf_threshold_db is not None
            and hf_excess > rule.hf_threshold_db
        ):
            qualifiers.append('hiss')
        if not qualifiers:
            qualifiers.append('balanced')
    return NoisePositionRating(
        position_label=position.position_label,
        role=position.role,
        method='ncb_sil',
        rating_label=f'NCB-{int(rating)}',
        rating_value=float(rating),
        sil_db=sil,
        qualifiers=tuple(qualifiers),
        band_verdicts=tuple(verdicts),
        applicability='in_scope' if not missing else 'limited',
        applicability_reasons=tuple(reasons),
    )


def _rc_curve_levels(
    index: float, bands: tuple[float, ...]
) -> tuple[float, ...]:
    """Closed-form Annex D reference curve: -5 dB/octave keyed at
    1000 Hz; 31.5 Hz floored at 55 dB; 16 Hz equal to 31.5 Hz."""
    from math import log2

    levels: list[float] = []
    value_31_5 = max(index + 5.0 * log2(1000.0 / 31.5), 55.0)
    for band in bands:
        if band == 16.0:
            levels.append(value_31_5)
        elif band == 31.5:
            levels.append(value_31_5)
        else:
            levels.append(index + 5.0 * log2(1000.0 / band))
    return tuple(levels)


def _rc_mark_ii_position_rating(
    position: NoisePositionSpectrum,
    profile: RoomNoiseMetricProfile,
) -> NoisePositionRating:
    levels, missing = _aligned_levels(
        position.spectrum, profile.required_bands_hz
    )
    reasons = [f'missing_band_{b:g}_hz' for b in missing]
    mf = profile.mid_bands_hz or RC_MF_BANDS_HZ
    if any(b not in levels for b in mf):
        return NoisePositionRating(
            position_label=position.position_label,
            role=position.role,
            method='rc_mark_ii',
            applicability='limited',
            applicability_reasons=tuple(
                ['mid_frequency_bands_missing'] + reasons
            ),
        )
    lmf = sum(levels[b] for b in mf) / len(mf)
    rating = round(lmf)
    reference = _rc_curve_levels(float(rating), profile.curve_band_centers_hz)
    rule = profile.imbalance_rule
    lf_excess = hf_excess = 0.0
    verdicts: list[NoiseBandVerdict] = []
    for i, band in enumerate(profile.curve_band_centers_hz):
        if band not in levels:
            continue
        excess = levels[band] - reference[i]
        verdicts.append(
            NoiseBandVerdict(
                band_center_hz=band,
                measured_level_db=levels[band],
                criterion_level_db=reference[i],
                exceedance_db=excess,
            )
        )
        if rule.lf_bound_hz is not None and band <= rule.lf_bound_hz:
            lf_excess = max(lf_excess, excess)
        elif rule.hf_bound_hz is not None and band >= rule.hf_bound_hz:
            hf_excess = max(hf_excess, excess)
    qualifiers: list[str] = []
    if rule.lf_threshold_db is not None and lf_excess > rule.lf_threshold_db:
        qualifiers.append('rumble')
    if rule.hf_threshold_db is not None and hf_excess > rule.hf_threshold_db:
        qualifiers.append('hiss')
    if not qualifiers:
        qualifiers.append('neutral')
    tag = (
        'N' if qualifiers == ['neutral']
        else ('R' if 'rumble' in qualifiers and 'hiss' not in qualifiers
              else 'H' if 'hiss' in qualifiers and 'rumble' not in qualifiers
              else 'RH')
    )
    return NoisePositionRating(
        position_label=position.position_label,
        role=position.role,
        method='rc_mark_ii',
        rating_label=f'RC-{int(rating)}({tag})',
        rating_value=float(rating),
        sil_db=lmf,
        qualifiers=tuple(qualifiers),
        band_verdicts=tuple(verdicts),
        applicability='in_scope' if not missing else 'limited',
        applicability_reasons=tuple(reasons),
    )


def _rnc_position_rating(
    position: NoisePositionSpectrum,
    profile: RoomNoiseMetricProfile,
) -> NoisePositionRating:
    """S12.2-2019 clause 5.3 RNC: requires the time-domain LF correction
    evidence and a registered curve family. Both absent → honest LIMITED,
    never a fabricated rating."""
    corrections = position.spectrum.lf_time_domain_correction_db
    if corrections is None:
        return NoisePositionRating(
            position_label=position.position_label,
            role=position.role,
            method='rnc_lf_corrected',
            applicability='out_of_scope',
            applicability_reasons=(
                'rnc_time_domain_correction_missing',
            ),
        )
    if not profile.curves:
        return NoisePositionRating(
            position_label=position.position_label,
            role=position.role,
            method='rnc_lf_corrected',
            applicability='limited',
            applicability_reasons=('rnc_curve_table_not_registered',),
        )
    return NoisePositionRating(
        position_label=position.position_label,
        role=position.role,
        method='rnc_lf_corrected',
        applicability='limited',
        applicability_reasons=('rnc_evaluation_not_implemented',),
    )


def _a_weighted_position_rating(
    position: NoisePositionSpectrum,
    profile: RoomNoiseMetricProfile,
) -> NoisePositionRating:
    value = position.spectrum.a_weighted_level_db
    if value is None:
        return NoisePositionRating(
            position_label=position.position_label,
            role=position.role,
            method='a_weighted_survey',
            applicability='limited',
            applicability_reasons=('a_weighted_level_not_reported',),
        )
    return NoisePositionRating(
        position_label=position.position_label,
        role=position.role,
        method='a_weighted_survey',
        rating_label=f'{value:.1f} dBA',
        rating_value=float(value),
        applicability='in_scope',
    )


def evaluate_noise_metric(
    measurement: BackgroundNoiseMeasurement,
    profile: RoomNoiseMetricProfile,
    *,
    target_label: str | None = None,
    target_rating: float | None = None,
    uncertainty_db: float | None = None,
    evaluated_at_utc: str | None = None,
) -> NoiseCriterionEvaluation:
    """One measurement under one exact profile → one sealed derived rating.

    - Temporal scope: an out-of-scope noise class never borrows the
      rating (``limited``/``out_of_scope`` with reasons).
    - Operating state is mandatory for qualified claims: all-``unknown``
      state marks the evaluation ``limited``.
    - Threshold check composes the #577 guard-band semantics: a rating
      within ``uncertainty_db`` of the target is
      ``indeterminate_guard_band``, never a rounded pass/fail.
    """
    reasons: list[str] = []
    if measurement.temporal_class not in profile.temporal_scope:
        applicability: NoiseApplicability = (
            'out_of_scope'
            if measurement.temporal_class in ('low_frequency_fluctuating',)
            and profile.metric_family in ('nc', 'ncb', 'rc_mark_ii')
            else 'limited'
        )
        reasons.append(
            f'temporal_class_{measurement.temporal_class}'
            '_outside_profile_scope'
        )
    else:
        applicability = 'in_scope'
    state = measurement.operating_state
    if all(
        getattr(state, f) == 'unknown'
        for f in (
            'hvac', 'projector', 'rack_fans', 'avr_equipment',
            'building_services', 'lighting_dimmers', 'external_sources',
        )
    ) and state.state_snapshot_id is None:
        applicability = 'limited'
        reasons.append('operating_state_undeclared')
    dispatch = {
        'nc': _nc_position_rating,
        'ncb': _ncb_position_rating,
        'rc_mark_ii': _rc_mark_ii_position_rating,
        'rnc': _rnc_position_rating,
        'a_weighted_level': _a_weighted_position_rating,
    }
    position_results: list[NoisePositionRating] = []
    limitations = list(profile.limitations)
    if applicability != 'out_of_scope':
        fn = dispatch.get(profile.metric_family)
        if fn is None:
            applicability = 'limited'
            reasons.append('profile_family_not_ratable')
        else:
            for position in measurement.positions:
                if (
                    profile.requires_absolute_levels
                    and position.spectrum.level_semantics != 'absolute_spl'
                ):
                    position_results.append(
                        NoisePositionRating(
                            position_label=position.position_label,
                            role=position.role,
                            method=profile.metric_family,
                            applicability='limited',
                            applicability_reasons=(
                                'level_semantics_not_absolute_spl',
                            ),
                        )
                    )
                    applicability = 'limited'
                    continue
                rated = fn(position, profile)
                if rated.applicability == 'limited':
                    applicability = 'limited'
                if rated.applicability == 'out_of_scope':
                    applicability = 'out_of_scope'
                position_results.append(rated)
    if measurement.tonal_indicator:
        limitations.append('tonal_component_declared')
    rated_positions = [
        r for r in position_results if r.rating_value is not None
    ]
    aggregate_basis: Literal[
        'worst_position', 'reference_position', 'none'
    ] = 'none'
    rating_value = None
    rating_label = None
    governing = None
    qualifiers: tuple[str, ...] = ()
    if rated_positions:
        # Worst position wins — never average a localized source away.
        worst = max(rated_positions, key=lambda r: float(r.rating_value))
        aggregate_basis = 'worst_position'
        rating_value = worst.rating_value
        rating_label = worst.rating_label
        governing = worst.governing_band_hz
        qualifiers = worst.qualifiers
        if len({r.position_label for r in rated_positions}) > 1:
            spread = (
                max(float(r.rating_value) for r in rated_positions)
                - min(float(r.rating_value) for r in rated_positions)
            )
            if spread > 0:
                limitations.append(
                    f'spatial_spread_{spread:.1f}_db_visible'
                )
    # Threshold verdict with #577-style guard band
    threshold: NoiseThresholdVerdict = 'not_evaluated'
    if target_rating is not None and rating_value is not None:
        u = float(
            uncertainty_db
            if uncertainty_db is not None
            else (measurement.uncertainty_db or 0.0)
        )
        if not isfinite(u) or u < 0:
            u = 0.0
        if rating_value > target_rating + u:
            threshold = 'fail'
        elif rating_value > target_rating - u:
            threshold = 'indeterminate_guard_band'
        else:
            threshold = 'pass'
    return _seal_evaluation(
        document_id=measurement.document_id,
        measurement_id=measurement.measurement_id,
        measurement_sha256=measurement.measurement_sha256,
        profile_id=profile.profile_id,
        profile_sha256=profile.profile_sha256,
        metric_family=profile.metric_family,
        standard_id=profile.standard_id,
        standard_edition=profile.standard_edition,
        calculation_version=profile.calculation_version,
        temporal_class=measurement.temporal_class,
        applicability=applicability,
        applicability_reasons=tuple(reasons),
        position_results=tuple(position_results),
        aggregate_basis=aggregate_basis,
        rating_label=rating_label,
        rating_value=rating_value,
        governing_band_hz=governing,
        qualifiers=qualifiers,
        target_label=target_label,
        threshold_verdict=threshold,
        threshold_uncertainty_db=(
            float(uncertainty_db) if uncertainty_db is not None
            else measurement.uncertainty_db
        ),
        limitations=tuple(dict.fromkeys(limitations)),
        evaluated_at_utc=evaluated_at_utc or _utc_now(),
    )


def assert_no_scalar_substitution(
    evaluation: NoiseCriterionEvaluation,
    *,
    claimed_family: NoiseMetricFamily | None = None,
) -> None:
    """Fail closed when a caller tries to quote a rating under a different
    family than the profile that produced it. ``NCB-22`` is never
    ``NC-22`` — the profile identity is part of the rating's meaning."""
    if claimed_family is not None and claimed_family != evaluation.metric_family:
        raise ValueError(
            f'noise metric substitution: evaluation is '
            f'{evaluation.metric_family} ({evaluation.standard_id}@'
            f'{evaluation.standard_edition}), not {claimed_family} — '
            'run a parallel evaluation instead of relabelling'
        )


def profile_for_rp22(
    profiles: tuple[RoomNoiseMetricProfile, ...] | list[RoomNoiseMetricProfile],
) -> RoomNoiseMetricProfile | None:
    """#579 consumer hook: the exact NCB-lineage profile RP22 v1.2 pins,
    identified by its external-standard ref — never whichever NC happens
    to be current."""
    for profile in profiles:
        if (
            'cedia-cta-rp22@v1.2' in profile.external_standard_refs
            and profile.metric_family == 'ncb'
        ):
            return profile
    return None


__all__ = [
    'APPLICABILITY_LABELS',
    'BackgroundNoiseMeasurement',
    'METRIC_FAMILY_LABELS',
    'NCB_1989_CURVES',
    'NCB_VIBRATION_MARKERS',
    'NC_2019_CURVES',
    'NOISE_EVALUATION_AUTHORITY_VERSION',
    'NOISE_METRIC_AUTHORITY_VERSION',
    'NOISE_METRIC_SCHEMA_VERSION',
    'NoiseAcquisitionContext',
    'NoiseApplicability',
    'NoiseBandLevels',
    'NoiseBandSpec',
    'NoiseBandVerdict',
    'NoiseCriterionCurve',
    'NoiseCriterionEvaluation',
    'NoiseImbalanceRule',
    'NoiseLevelSemantics',
    'NoiseMetricFamily',
    'NoiseOperatingState',
    'NoisePositionRating',
    'NoisePositionRole',
    'NoisePositionSpectrum',
    'NoiseProfileStatus',
    'NoiseSourcePresence',
    'NoiseTemporalClass',
    'NoiseThresholdVerdict',
    'PROFILE_STATUS_LABELS',
    'RC_MF_BANDS_HZ',
    'RoomNoiseMetricProfile',
    'S12_2_OCTAVE_BANDS_HZ',
    'SIL_BANDS_HZ',
    'TEMPORAL_CLASS_LABELS',
    'THRESHOLD_VERDICT_LABELS',
    'assert_no_scalar_substitution',
    'build_noise_measurement',
    'build_noise_metric_profile',
    'evaluate_noise_metric',
    'profile_for_rp22',
    'seed_room_noise_metric_profiles',
]
