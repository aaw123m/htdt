"""Video colorimetry commissioning authority (#647).

Persists white-point, grayscale/EOTF and gamut measurements as durable
authorities — deliberately stopping short of LUT generation or display
characterisation curves.

- :class:`VideoColorTargetProfile` — the target a display is calibrated to:
  EOTF reference (gamma 2.4 / PQ / HLG / named vendor curve), white point,
  RGB primaries, the stimulus list, and per-criterion-group tolerances.
  Provenance says where the target came from (e.g. "Rec.709", "calibrator's
  custom target").
- :class:`StimulusDefinition` — how each test patch was generated:
  signal encoding, range, bit depth, patch size / APL, pattern generator
  identity and the signal path it traversed. Without this a measurement
  cannot be replayed.
- :class:`ColorimeterCorrectionProfile` — a correction file/matrix applied
  on top of a base meter (e.g. a probe's per-display-technology correction).
  Kept separate so a measurement set reveals both the raw meter and the
  correction used.
- :class:`VideoColorMeasurementSet` — canonical CIE XYZ tristimulus samples
  per stimulus id plus meter identity, timestamp and quality flags
  (including near-black / meter-floor warnings). Samples are stored as XYZ,
  never as pre-cooked deltas, so any metric family can be computed later.
- :class:`VideoColorEvaluation` — measurement set × target profile × a
  named metric family at an explicit version. ``metric_family`` selects a
  real algorithm (#1018): ``dE2000`` is a verified CIEDE2000 in CIELAB,
  ``dEuv1976`` is CIE 1976 u′v′ distance, ``other``/``xy_euclidean_scaled_v1``
  keeps the legacy xy screening metric under an honest name, and unsupported
  families (``dEICtCp``) fail closed instead of being relabelled. Criterion
  groups (white point, grayscale chromaticity, EOTF tracking, peak
  luminance, gamut) each report PASS / FAIL / UNKNOWN / NOT_APPLICABLE —
  EOTF and peak luminance are real criteria, not descriptive metadata
  (#1019); there is no hidden overall score.
- Applied-calibration state lives in a separate
  :class:`AppliedCalibrationState` record so "the device was set to X" is
  never conflated with "measured state met the target".

All authorities are frozen, self-hashed and append-only like the other CAD
authorities. Imported measurement sets keep the importer app/version, the
source asset hash and the parser id so the provenance chain survives.
"""

from __future__ import annotations

from math import atan2, cos, degrees, exp, radians, sin
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_video_geometry import EvaluationStatus, _combine_status
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload




EOTFReference = Literal[
    'gamma_2_2',
    'gamma_2_4',
    'bt1886',
    'pq_st2084',
    'hlg',
    'linear',
    'other',
    'unknown',
]
"""The EOTF a target references. ``other`` is for named custom curves
recorded in ``eotf_label``; ``unknown`` means unrecorded."""

ColorEncoding = Literal[
    'rgb_full', 'rgb_limited', 'ycbcr_limited', 'ycbcr_full', 'unknown'
]

ColorMetricFamily = Literal['dE2000', 'dEICtCp', 'dEuv1976', 'other']
"""Supported metric families. The family+version pair on an evaluation is
what makes its numbers replayable — the same samples scored under a
different metric or metric version produce different criteria results."""


class ChromaticityPoint(BaseModel):
    """CIE 1931 xy chromaticity."""

    model_config = ConfigDict(frozen=True)

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


class ColorTolerances(BaseModel):
    """Per-criterion-group tolerances; ``None`` means the group is not
    constrained by this target (reported NOT_APPLICABLE).

    ``metric_family``/``metric_version`` optionally bind the ΔE tolerances
    (white point, grayscale, gamut) to the exact metric they were authored
    for (#1018): a threshold entered under one metric cannot be silently
    reused under another — the groups report UNKNOWN on mismatch. An
    unbound tolerance applies to whatever metric the evaluation selects
    (legacy semantics).
    """

    model_config = ConfigDict(frozen=True)

    white_point_delta_e: float | None = Field(default=None, ge=0.0)
    grayscale_delta_e: float | None = Field(default=None, ge=0.0)
    gamut_delta_e: float | None = Field(default=None, ge=0.0)
    eotf_deviation_fraction: float | None = Field(
        default=None, ge=0.0
    )
    peak_luminance_tolerance_fraction: float | None = Field(
        default=None, ge=0.0
    )
    metric_family: str | None = None
    metric_version: str | None = None


class VideoColorTargetProfile(BaseModel):
    """The calibration target: EOTF + white point + primaries + tolerances."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['video-color-target-1'] = (
        'video-color-target-1'
    )
    target_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    label: str | None = None
    eotf: EOTFReference
    eotf_label: str | None = None
    peak_luminance_cd_m2: float | None = Field(default=None, gt=0.0)
    white_point: ChromaticityPoint | None = None
    primary_red: ChromaticityPoint | None = None
    primary_green: ChromaticityPoint | None = None
    primary_blue: ChromaticityPoint | None = None
    stimulus_levels: tuple[float, ...] = ()
    tolerances: ColorTolerances = Field(default_factory=ColorTolerances)
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    target_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'target_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'VideoColorTargetProfile':
        if self.eotf == 'other' and not self.eotf_label:
            raise ValueError("eotf 'other' requires eotf_label naming it")
        if self.eotf != 'other' and self.eotf_label is not None:
            raise ValueError(
                'eotf_label is only valid with eotf=other — the EOTF field '
                'itself names standard curves'
            )
        if self.target_sha256 != _hash(self.semantic_payload()):
            raise ValueError('video color target semantic hash mismatch')
        return self


def build_video_color_target_profile(
    *,
    target_id: str,
    version: str,
    eotf: EOTFReference,
    eotf_label: str | None = None,
    label: str | None = None,
    peak_luminance_cd_m2: float | None = None,
    white_point: ChromaticityPoint | None = None,
    primary_red: ChromaticityPoint | None = None,
    primary_green: ChromaticityPoint | None = None,
    primary_blue: ChromaticityPoint | None = None,
    stimulus_levels: tuple[float, ...] = (),
    tolerances: ColorTolerances | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> VideoColorTargetProfile:
    probe = VideoColorTargetProfile.model_construct(**canonicalize_payload(VideoColorTargetProfile, dict(
        target_id=target_id,
        version=version,
        label=label,
        eotf=eotf,
        eotf_label=eotf_label,
        peak_luminance_cd_m2=peak_luminance_cd_m2,
        white_point=white_point,
        primary_red=primary_red,
        primary_green=primary_green,
        primary_blue=primary_blue,
        stimulus_levels=tuple(stimulus_levels),
        tolerances=tolerances if tolerances is not None else ColorTolerances(),
        provenance=tuple(provenance),
        target_sha256='',
    )))
    return VideoColorTargetProfile(
        **probe.model_dump(mode='python', exclude={'target_sha256'}),
        target_sha256=_hash(probe.semantic_payload()),
    )


class StimulusDefinition(BaseModel):
    """How a stimulus patch was generated — required for replayability."""

    model_config = ConfigDict(frozen=True)

    encoding: ColorEncoding = 'unknown'
    bit_depth: int | None = Field(default=None, ge=8)
    patch_size_percent: float | None = Field(default=None, gt=0.0, le=100.0)
    apl_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    pattern_generator: str | None = None
    signal_path: str | None = None


class ColorimeterCorrectionProfile(BaseModel):
    """A probe correction layered over a base meter — e.g. a spectroradiometer-
    derived matrix for an OLED panel. Never merged into the meter identity."""

    model_config = ConfigDict(frozen=True)

    correction_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    base_meter: str = Field(min_length=1)
    correction_kind: str = Field(min_length=1)
    applies_to_display_class: str | None = None
    source_reference: str | None = None
    source_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    provenance: tuple[EquipmentDataProvenance, ...] = ()


class TristimulusSample(BaseModel):
    """Canonical XYZ tristimulus reading for one stimulus level/point.

    ``stimulus_level`` is the explicit normalized signal level (0–1) the
    patch was driven at (#1019) — the EOTF evaluator consumes this field,
    never a level guessed from the free-text ``stimulus_id``.
    """

    model_config = ConfigDict(frozen=True)

    stimulus_id: str = Field(min_length=1)
    stimulus_level: float | None = Field(default=None, ge=0.0, le=1.0)
    x: float = Field(ge=0.0)
    y_luminance: float = Field(ge=0.0)
    z: float = Field(ge=0.0)
    at_meter_floor: bool = False
    note: str | None = None


class VideoColorMeasurementSet(BaseModel):
    """One captured set of color samples — immutable, canonical, replayable."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['video-color-measurement-1'] = (
        'video-color-measurement-1'
    )
    measurement_set_id: str = Field(min_length=1)
    measured_at_utc: str = Field(min_length=1)
    surface_entity_id: str = Field(min_length=1)
    meter: str = Field(min_length=1)
    meter_correction: ColorimeterCorrectionProfile | None = None
    stimulus: StimulusDefinition = Field(
        default_factory=StimulusDefinition
    )
    samples: tuple[TristimulusSample, ...]
    import_source: str | None = None
    import_app_version: str | None = None
    import_asset_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    import_parser_id: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    measurement_set_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'measurement_set_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'VideoColorMeasurementSet':
        ids = [s.stimulus_id for s in self.samples]
        if len(set(ids)) != len(ids):
            raise ValueError('stimulus ids must be unique within a set')
        import_fields = (
            self.import_source is not None,
            self.import_asset_sha256 is not None,
            self.import_parser_id is not None,
        )
        if any(import_fields) and not all(import_fields):
            raise ValueError(
                'import source, asset hash and parser id must be supplied '
                'together'
            )
        if self.measurement_set_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'video color measurement set semantic hash mismatch'
            )
        return self


def build_video_color_measurement_set(
    *,
    measurement_set_id: str,
    measured_at_utc: str,
    surface_entity_id: str,
    meter: str,
    samples: tuple[TristimulusSample, ...],
    meter_correction: ColorimeterCorrectionProfile | None = None,
    stimulus: StimulusDefinition | None = None,
    import_source: str | None = None,
    import_app_version: str | None = None,
    import_asset_sha256: str | None = None,
    import_parser_id: str | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> VideoColorMeasurementSet:
    probe = VideoColorMeasurementSet.model_construct(**canonicalize_payload(VideoColorMeasurementSet, dict(
        measurement_set_id=measurement_set_id,
        measured_at_utc=measured_at_utc,
        surface_entity_id=surface_entity_id,
        meter=meter,
        meter_correction=meter_correction,
        stimulus=stimulus if stimulus is not None else StimulusDefinition(),
        samples=tuple(samples),
        import_source=import_source,
        import_app_version=import_app_version,
        import_asset_sha256=import_asset_sha256,
        import_parser_id=import_parser_id,
        provenance=tuple(provenance),
        measurement_set_sha256='',
    )))
    return VideoColorMeasurementSet(
        **probe.model_dump(mode='python', exclude={'measurement_set_sha256'}),
        measurement_set_sha256=_hash(probe.semantic_payload()),
    )


class AppliedCalibrationState(BaseModel):
    """"What the device was set to" — kept separate from measured truth."""

    model_config = ConfigDict(frozen=True)

    state_id: str = Field(min_length=1)
    surface_entity_id: str = Field(min_length=1)
    applied_at_utc: str = Field(min_length=1)
    settings: dict[str, str] = Field(default_factory=dict)
    applied_by: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()


class ColorCriterionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    group: str = Field(min_length=1)
    status: EvaluationStatus
    worst_delta_e: float | None = None
    measured_points: int = 0
    note: str | None = None


class VideoColorEvaluation(BaseModel):
    """measurement × target × metric family+version → per-group results."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    evaluation_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    target_version: str = Field(min_length=1)
    target_sha256: str = Field(min_length=16)
    measurement_set_id: str = Field(min_length=1)
    measurement_set_sha256: str = Field(min_length=16)
    metric_family: ColorMetricFamily
    metric_version: str = Field(min_length=1)
    groups: tuple[ColorCriterionResult, ...]
    evaluation_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'evaluation_sha256', 'evaluation_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'VideoColorEvaluation':
        digest = _hash(self.semantic_payload())
        if self.evaluation_sha256 != digest:
            raise ValueError('video color evaluation hash mismatch')
        if self.evaluation_id != 'vce-' + digest[:24]:
            raise ValueError('video color evaluation id mismatch')
        return self


_GRAYSCALE_PREFIXES = ('gray_', 'grey_', 'eotf_', 'w')
_GAMUT_PREFIXES = ('r', 'g', 'b', 'c', 'm', 'y', 'red', 'green', 'blue')

# The single implemented algorithm version per metric family (#1018). A
# family+version pair only exists when a verified implementation exists —
# unlisted combinations reject evaluation instead of relabelling results.
_SUPPORTED_METRIC_VERSIONS: dict[str, str] = {
    'dE2000': 'ciede2000-1',
    'dEuv1976': 'cie1976-uv-1',
    'other': 'xy_euclidean_scaled_v1',
}

_D65 = (0.3127, 0.3290)
_EOTF_LEVEL_MATCH = 0.005
_EOTF_DEVIATION_VERSION = 'eotf-relative-deviation-1'
_HLG_SYSTEM_GAMMA = 1.2


def _resolve_metric_version(
    metric_family: ColorMetricFamily,
    metric_version: str | None,
) -> str:
    """Bind a family to its implemented algorithm version — fail closed.

    ``dEICtCp`` has no implemented algorithm in this version and unlisted
    versions are rejected outright: a result is never labelled with a metric
    that was not actually computed (#1018).
    """
    implemented = _SUPPORTED_METRIC_VERSIONS.get(metric_family)
    if implemented is None:
        raise ValueError(
            f'metric family {metric_family!r} has no implemented algorithm '
            'in this version — evaluation rejected rather than mislabelled'
        )
    if metric_version is None:
        return implemented
    if metric_version != implemented:
        raise ValueError(
            f'metric family {metric_family!r} version {metric_version!r} is '
            f'not implemented (implemented: {implemented!r})'
        )
    return metric_version



def _xy_to_xyz(xy: tuple[float, float], luminance: float) -> tuple[float, float, float]:
    x, y = xy
    if y <= 0.0:
        return 0.0, luminance, 0.0
    scale = luminance / y
    return x * scale, luminance, (1.0 - x - y) * scale


def _xyz_to_lab(
    x: float, y_lum: float, z: float, white_xyz: tuple[float, float, float]
) -> tuple[float, float, float]:
    xr = x / white_xyz[0]
    yr = y_lum / white_xyz[1]
    zr = z / white_xyz[2]
    eps = 216.0 / 24389.0
    kap = 24389.0 / 27.0

    def f(t: float) -> float:
        return t ** (1.0 / 3.0) if t > eps else (kap * t + 16.0) / 116.0

    fx, fy, fz = f(xr), f(yr), f(zr)
    return 116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz)


def _delta_e_2000_lab(
    lab1: tuple[float, float, float],
    lab2: tuple[float, float, float],
) -> float:
    """CIEDE2000 color difference (Sharma, Wu & Dalal 2005), kL=kC=kH=1."""

    l1, a1, b1 = lab1
    l2, a2, b2 = lab2
    c1 = (a1 * a1 + b1 * b1) ** 0.5
    c2 = (a2 * a2 + b2 * b2) ** 0.5
    c_bar = (c1 + c2) / 2.0
    c7 = c_bar ** 7
    g = 0.5 * (1.0 - (c7 / (c7 + 25.0 ** 7)) ** 0.5)
    a1p = (1.0 + g) * a1
    a2p = (1.0 + g) * a2
    c1p = (a1p * a1p + b1 * b1) ** 0.5
    c2p = (a2p * a2p + b2 * b2) ** 0.5

    def _hp(a: float, b: float) -> float:
        if a == 0.0 and b == 0.0:
            return 0.0
        h = degrees(atan2(b, a))
        return h if h >= 0.0 else h + 360.0

    h1p = _hp(a1p, b1)
    h2p = _hp(a2p, b2)
    dLp = l2 - l1
    dCp = c2p - c1p
    if c1p * c2p == 0.0:
        dhp = 0.0
    else:
        dh = h2p - h1p
        if dh > 180.0:
            dh -= 360.0
        elif dh < -180.0:
            dh += 360.0
        dhp = 2.0 * (c1p * c2p) ** 0.5 * sin(radians(dh / 2.0))
    l_bar = (l1 + l2) / 2.0
    c_bar_p = (c1p + c2p) / 2.0
    if c1p * c2p == 0.0:
        h_bar = h1p + h2p
    else:
        dh = abs(h1p - h2p)
        if dh <= 180.0:
            h_bar = (h1p + h2p) / 2.0
        elif h1p + h2p < 360.0:
            h_bar = (h1p + h2p + 360.0) / 2.0
        else:
            h_bar = (h1p + h2p - 360.0) / 2.0
    t = (
        1.0
        - 0.17 * cos(radians(h_bar - 30.0))
        + 0.24 * cos(radians(2.0 * h_bar))
        + 0.32 * cos(radians(3.0 * h_bar + 6.0))
        - 0.20 * cos(radians(4.0 * h_bar - 63.0))
    )
    theta = 30.0 * exp(-(((h_bar - 275.0) / 25.0) ** 2))
    c7p = c_bar_p ** 7
    rc = 2.0 * (c7p / (c7p + 25.0 ** 7)) ** 0.5
    sl = 1.0 + (0.015 * (l_bar - 50.0) ** 2) / (
        (20.0 + (l_bar - 50.0) ** 2) ** 0.5
    )
    sc = 1.0 + 0.045 * c_bar_p
    sh = 1.0 + 0.015 * c_bar_p * t
    rt = -sin(radians(2.0 * theta)) * rc
    return (
        (dLp / sl) ** 2
        + (dCp / sc) ** 2
        + (dhp / sh) ** 2
        + rt * (dCp / sc) * (dhp / sh)
    ) ** 0.5


def _uv_prime_1976(
    x: float, y_lum: float, z: float
) -> tuple[float, float] | None:
    denom = x + 15.0 * y_lum + 3.0 * z
    if denom <= 0.0:
        return None
    return 4.0 * x / denom, 9.0 * y_lum / denom


def _delta_e_xy_scaled(
    measured_xyz: tuple[float, float, float],
    target_xy: tuple[float, float],
    white_xyz: tuple[float, float, float] | None,
) -> float | None:
    """Legacy screening metric ``xy_euclidean_scaled_v1``: Euclidean distance
    in CIE 1931 xy × 100. Not CIEDE2000 — retained under family ``other``
    for historical evaluations only (#1018)."""

    total = measured_xyz[0] + measured_xyz[1] + measured_xyz[2]
    if total <= 0.0:
        return None
    dx = measured_xyz[0] / total - target_xy[0]
    dy = measured_xyz[1] / total - target_xy[1]
    return (dx * dx + dy * dy) ** 0.5 * 100.0


def _delta_e_uv_1976(
    measured_xyz: tuple[float, float, float],
    target_xy: tuple[float, float],
    white_xyz: tuple[float, float, float] | None,
) -> float | None:
    """CIE 1976 u′v′ chromaticity distance (unscaled Euclidean)."""

    measured = _uv_prime_1976(*measured_xyz)
    if measured is None:
        return None
    target = _uv_prime_1976(*_xy_to_xyz(target_xy, measured_xyz[1]))
    if target is None:
        return None
    return (
        (measured[0] - target[0]) ** 2 + (measured[1] - target[1]) ** 2
    ) ** 0.5


def _delta_e_ciede2000(
    measured_xyz: tuple[float, float, float],
    target_xy: tuple[float, float],
    white_xyz: tuple[float, float, float] | None,
) -> float | None:
    """CIEDE2000 between the measured sample and the target chromaticity.

    The target point is converted to XYZ at the measured sample's own
    luminance — ΔL* is therefore 0 and the result is a pure chromaticity
    difference, which is what white-point/grayscale/gamut tolerances mean.
    A reference white is required for the XYZ→Lab conversion.
    """

    if white_xyz is None:
        return None
    target_xyz = _xy_to_xyz(target_xy, measured_xyz[1])
    lab1 = _xyz_to_lab(*measured_xyz, white_xyz)
    lab2 = _xyz_to_lab(*target_xyz, white_xyz)
    return _delta_e_2000_lab(lab1, lab2)


_METRIC_DISPATCH = {
    'ciede2000-1': _delta_e_ciede2000,
    'cie1976-uv-1': _delta_e_uv_1976,
    'xy_euclidean_scaled_v1': _delta_e_xy_scaled,
}


def _reference_white_xyz(
    target: VideoColorTargetProfile,
    samples: tuple[TristimulusSample, ...],
) -> tuple[float, float, float] | None:
    """Reference white for Lab conversion: the measured white sample when
    present, else the target white point at Yn=100, else D65 at Yn=100."""
    whites = [
        s for s in samples
        if _sample_group(s.stimulus_id) == 'white_point'
        and not s.at_meter_floor
    ]
    if whites:
        brightest = max(whites, key=lambda s: s.y_luminance)
        return brightest.x, brightest.y_luminance, brightest.z
    if target.white_point is not None:
        return _xy_to_xyz(
            (target.white_point.x, target.white_point.y), 100.0
        )
    return _xy_to_xyz(_D65, 100.0)


def _sample_group(stimulus_id: str) -> str:
    sid = stimulus_id.lower()
    if sid in ('white', 'w100', 'white_100') or sid.startswith('white_'):
        return 'white_point'
    if any(sid.startswith(p) for p in _GRAYSCALE_PREFIXES):
        return 'grayscale'
    if any(sid.startswith(p) for p in _GAMUT_PREFIXES):
        return 'gamut'
    return 'other'


def _black_or_zero(black_cd_m2: float | None) -> float:
    return black_cd_m2 if black_cd_m2 is not None else 0.0


def _eotf_expected_luminance(
    eotf: EOTFReference,
    level: float,
    white_cd_m2: float,
    black_cd_m2: float | None,
) -> float | None:
    """Expected luminance for a normalized stimulus level under the named
    EOTF — versioned transfer semantics, ``None`` when the curve cannot be
    evaluated with the evidence at hand (#1019)."""

    if eotf == 'gamma_2_2':
        return _black_or_zero(black_cd_m2) + (
            white_cd_m2 - _black_or_zero(black_cd_m2)
        ) * level ** 2.2
    if eotf == 'gamma_2_4':
        return _black_or_zero(black_cd_m2) + (
            white_cd_m2 - _black_or_zero(black_cd_m2)
        ) * level ** 2.4
    if eotf == 'linear':
        return _black_or_zero(black_cd_m2) + (
            white_cd_m2 - _black_or_zero(black_cd_m2)
        ) * level
    if eotf == 'bt1886':
        if black_cd_m2 is None:
            return None  # BT.1886 needs the measured black level
        lw_pow = white_cd_m2 ** (1.0 / 2.4)
        lb_pow = black_cd_m2 ** (1.0 / 2.4)
        a = (lw_pow - lb_pow) ** 2.4
        b = lb_pow / (lw_pow - lb_pow) if lw_pow != lb_pow else 0.0
        return a * (level + b) ** 2.4
    if eotf == 'pq_st2084':
        # ST 2084 absolute EOTF — luminance is absolute, not normalized.
        m1 = 0.1593017578125
        m2 = 78.84375
        c1 = 0.8359375
        c2 = 18.8515625
        c3 = 18.6875
        v = level ** (1.0 / m2)
        num = max(v - c1, 0.0)
        den = c2 - c3 * v
        if den <= 0.0:
            return None
        return 10000.0 * (num / den) ** (1.0 / m1)
    if eotf == 'hlg':
        # HLG inverse-OETF to scene linear, then system-gamma OOTF
        # (gamma = 1.2, the nominal 1000 cd/m² reference system).
        a, b, c = 0.17883277, 0.28466892, 0.55991073
        if level <= 0.5:
            scene = level * level / 3.0
        else:
            scene = (exp((level - a) / b) + c) / 12.0
        return white_cd_m2 * scene ** _HLG_SYSTEM_GAMMA
    return None



def _tolerance_metric_compatible(
    tolerances: ColorTolerances,
    metric_family: str,
    metric_version: str,
) -> bool:
    """Whether the declared ΔE tolerances may be consumed under the
    selected metric (#1018). Unbound tolerances apply to any metric."""
    if (
        tolerances.metric_family is not None
        and tolerances.metric_family != metric_family
    ):
        return False
    if (
        tolerances.metric_version is not None
        and tolerances.metric_version != metric_version
    ):
        return False
    return True


def _eotf_group(
    target: VideoColorTargetProfile,
    samples: tuple[TristimulusSample, ...],
    tol: ColorTolerances,
) -> ColorCriterionResult:
    """EOTF / luminance-tracking criterion group (#1019).

    Consumes only samples with an explicit ``stimulus_level`` — stimulus
    semantics are never inferred from ids. Required levels come from
    ``target.stimulus_levels``; missing coverage yields UNKNOWN. Deviation
    metric: ``eotf-relative-deviation-1`` (worst relative |measured −
    expected| / expected across covered levels).
    """
    group = 'eotf_tracking'
    if tol.eotf_deviation_fraction is None:
        return ColorCriterionResult(
            group=group,
            status='NOT_APPLICABLE',
            note='target does not constrain EOTF tracking',
        )
    if target.eotf in ('other', 'unknown'):
        return ColorCriterionResult(
            group=group,
            status='UNKNOWN',
            note='target EOTF is not a named standard curve',
        )
    leveled = sorted(
        (s for s in samples
         if s.stimulus_level is not None and not s.at_meter_floor),
        key=lambda s: s.stimulus_level or 0.0,
    )
    required = target.stimulus_levels
    missing = tuple(
        level for level in required
        if not any(
            s.stimulus_level is not None
            and abs(s.stimulus_level - level) <= _EOTF_LEVEL_MATCH
            for s in leveled
        )
    )
    if not leveled:
        return ColorCriterionResult(
            group=group,
            status='UNKNOWN',
            measured_points=0,
            note='no samples carry explicit stimulus_level semantics',
        )
    if missing:
        return ColorCriterionResult(
            group=group,
            status='UNKNOWN',
            measured_points=len(leveled),
            note=(
                f'missing required stimulus levels {missing} — '
                f'{len(leveled)} covered samples are insufficient'
            ),
        )
    white_l = target.peak_luminance_cd_m2
    if white_l is None:
        peak = max((s.y_luminance for s in leveled), default=None)
        white_l = peak
    if white_l is None or white_l <= 0.0:
        return ColorCriterionResult(
            group=group,
            status='UNKNOWN',
            measured_points=len(leveled),
            note='no white-reference luminance for EOTF normalization',
        )
    black_l = None
    for s in leveled:
        if s.stimulus_level is not None and s.stimulus_level <= _EOTF_LEVEL_MATCH:
            black_l = s.y_luminance
            break
    evaluated = [
        s for s in leveled
        if not required
        or any(
            abs(s.stimulus_level - level) <= _EOTF_LEVEL_MATCH
            for level in required
        )
    ]
    worst = 0.0
    usable = 0
    for sample in evaluated:
        expected = _eotf_expected_luminance(
            target.eotf,
            sample.stimulus_level or 0.0,
            white_l,
            black_l,
        )
        if expected is None or expected <= 0.0:
            continue
        usable += 1
        deviation = abs(sample.y_luminance - expected) / expected
        worst = max(worst, deviation)
    if usable == 0:
        return ColorCriterionResult(
            group=group,
            status='UNKNOWN',
            measured_points=len(leveled),
            note='EOTF curve could not be evaluated with this evidence',
        )
    return ColorCriterionResult(
        group=group,
        status=(
            'PASS' if worst <= tol.eotf_deviation_fraction else 'FAIL'
        ),
        worst_delta_e=worst,
        measured_points=usable,
        note=(
            f'{_EOTF_DEVIATION_VERSION}; coverage '
            f'{len(evaluated)}/{len(required) if required else len(evaluated)}'
        ),
    )


def _peak_luminance_group(
    target: VideoColorTargetProfile,
    samples: tuple[TristimulusSample, ...],
    tol: ColorTolerances,
) -> ColorCriterionResult:
    """Peak-luminance criterion group (#1019).

    A declared target peak is a real criterion, never descriptive metadata:
    with a configured tolerance it is PASS/FAIL, without one the gap is
    UNKNOWN rather than silently unconstrained.
    """
    group = 'peak_luminance'
    if target.peak_luminance_cd_m2 is None:
        return ColorCriterionResult(
            group=group,
            status='NOT_APPLICABLE',
            note='target declares no peak luminance',
        )
    peaks = [
        s for s in samples
        if not s.at_meter_floor
        and (
            _sample_group(s.stimulus_id) == 'white_point'
            or (
                s.stimulus_level is not None
                and s.stimulus_level >= 1.0 - _EOTF_LEVEL_MATCH
            )
        )
    ]
    if tol.peak_luminance_tolerance_fraction is None:
        return ColorCriterionResult(
            group=group,
            status='UNKNOWN',
            measured_points=len(peaks),
            note='target peak declared without a tolerance policy',
        )
    if not peaks:
        return ColorCriterionResult(
            group=group,
            status='UNKNOWN',
            measured_points=0,
            note='no white/peak samples to compare against target peak',
        )
    measured = max(s.y_luminance for s in peaks)
    deviation = abs(measured - target.peak_luminance_cd_m2) / (
        target.peak_luminance_cd_m2
    )
    return ColorCriterionResult(
        group=group,
        status=(
            'PASS'
            if deviation <= tol.peak_luminance_tolerance_fraction
            else 'FAIL'
        ),
        worst_delta_e=deviation,
        measured_points=len(peaks),
    )


def evaluate_video_color(
    *,
    target: VideoColorTargetProfile,
    measurement_set: VideoColorMeasurementSet,
    metric_family: ColorMetricFamily = 'dE2000',
    metric_version: str | None = None,
) -> VideoColorEvaluation:
    """Evaluate a measurement set against a target under one metric family.

    ``metric_family``/``metric_version`` select one exact implemented
    algorithm (#1018); unsupported combinations raise. Groups:
    ``white_point``, ``grayscale``, ``eotf_tracking``, ``peak_luminance``,
    ``gamut``. A group is UNKNOWN when the target constrains it but the set
    has no usable samples, NOT_APPLICABLE when the target sets no tolerance
    for it, and FAIL when any usable sample exceeds the group tolerance.
    Meter-floor samples are counted but excluded from worst-case deltas.
    EOTF tracking consumes explicit ``stimulus_level`` semantics and the
    target's declared ``stimulus_levels`` coverage — a configured criterion
    that was never measured cannot fold into PASS (#1019).
    """

    resolved_version = _resolve_metric_version(metric_family, metric_version)
    differ = _METRIC_DISPATCH[resolved_version]
    tol = target.tolerances
    metric_ok = _tolerance_metric_compatible(
        tol, metric_family, resolved_version
    )
    white_xyz = _reference_white_xyz(target, measurement_set.samples)
    groups: list[ColorCriterionResult] = []

    def _group_result(
        name: str,
        tolerance: float | None,
        target_xy: ChromaticityPoint | None,
        samples: tuple[TristimulusSample, ...],
    ) -> ColorCriterionResult:
        if tolerance is None or target_xy is None:
            return ColorCriterionResult(
                group=name,
                status='NOT_APPLICABLE',
                measured_points=len(samples),
                note='target does not constrain this group',
            )
        if not metric_ok:
            return ColorCriterionResult(
                group=name,
                status='UNKNOWN',
                measured_points=len(samples),
                note=(
                    f'tolerance bound to metric '
                    f'{tol.metric_family}/{tol.metric_version}, evaluation '
                    f'uses {metric_family}/{resolved_version}'
                ),
            )
        if not samples:
            return ColorCriterionResult(
                group=name,
                status='UNKNOWN',
                measured_points=0,
                note='no samples in this group',
            )
        usable = [s for s in samples if not s.at_meter_floor]
        if not usable:
            return ColorCriterionResult(
                group=name,
                status='UNKNOWN',
                measured_points=len(samples),
                note='all samples are at the meter floor',
            )
        deltas = []
        for sample in usable:
            delta = differ(
                (sample.x, sample.y_luminance, sample.z),
                (target_xy.x, target_xy.y),
                white_xyz,
            )
            if delta is not None:
                deltas.append(delta)
        if not deltas:
            return ColorCriterionResult(
                group=name,
                status='UNKNOWN',
                measured_points=len(samples),
                note='no usable XYZ samples for this metric',
            )
        worst = max(deltas)
        return ColorCriterionResult(
            group=name,
            status='PASS' if worst <= tolerance else 'FAIL',
            worst_delta_e=worst,
            measured_points=len(usable),
        )

    by_group: dict[str, list[TristimulusSample]] = {}
    for sample in measurement_set.samples:
        by_group.setdefault(_sample_group(sample.stimulus_id), []).append(
            sample
        )

    groups.append(
        _group_result(
            'white_point',
            tol.white_point_delta_e,
            target.white_point,
            tuple(by_group.get('white_point', ())),
        )
    )
    groups.append(
        _group_result(
            'grayscale',
            tol.grayscale_delta_e,
            target.white_point,
            tuple(by_group.get('grayscale', ())),
        )
    )
    # gamut compares each primary against its own target point
    gamut_tolerance = tol.gamut_delta_e
    gamut_samples = tuple(by_group.get('gamut', ()))
    if gamut_tolerance is None or not any(
        (target.primary_red, target.primary_green, target.primary_blue)
    ):
        groups.append(
            ColorCriterionResult(
                group='gamut',
                status='NOT_APPLICABLE',
                measured_points=len(gamut_samples),
                note='target does not constrain gamut primaries',
            )
        )
    elif not metric_ok:
        groups.append(
            ColorCriterionResult(
                group='gamut',
                status='UNKNOWN',
                measured_points=len(gamut_samples),
                note=(
                    f'tolerance bound to metric '
                    f'{tol.metric_family}/{tol.metric_version}, evaluation '
                    f'uses {metric_family}/{resolved_version}'
                ),
            )
        )
    elif not gamut_samples:
        groups.append(
            ColorCriterionResult(
                group='gamut', status='UNKNOWN', measured_points=0
            )
        )
    else:
        target_map = {
            'r': target.primary_red,
            'g': target.primary_green,
            'b': target.primary_blue,
        }
        deltas: list[float] = []
        counted = 0
        for sample in gamut_samples:
            key = sample.stimulus_id.lower()[:1]
            point = target_map.get(key)
            if point is None or sample.at_meter_floor:
                continue
            delta = differ(
                (sample.x, sample.y_luminance, sample.z),
                (point.x, point.y),
                white_xyz,
            )
            if delta is None:
                continue
            counted += 1
            deltas.append(delta)
        if not deltas:
            groups.append(
                ColorCriterionResult(
                    group='gamut',
                    status='UNKNOWN',
                    measured_points=len(gamut_samples),
                    note='no gamut samples matching target primaries',
                )
            )
        else:
            worst = max(deltas)
            groups.append(
                ColorCriterionResult(
                    group='gamut',
                    status='PASS' if worst <= gamut_tolerance else 'FAIL',
                    worst_delta_e=worst,
                    measured_points=counted,
                )
            )

    groups.append(
        _eotf_group(target, measurement_set.samples, tol)
    )
    groups.append(
        _peak_luminance_group(target, measurement_set.samples, tol)
    )

    probe = VideoColorEvaluation.model_construct(**canonicalize_payload(VideoColorEvaluation, dict(
        evaluation_id='',
        target_id=target.target_id,
        target_version=target.version,
        target_sha256=target.target_sha256,
        measurement_set_id=measurement_set.measurement_set_id,
        measurement_set_sha256=measurement_set.measurement_set_sha256,
        metric_family=metric_family,
        metric_version=resolved_version,
        groups=tuple(groups),
        evaluation_sha256='',
    )))
    digest = _hash(probe.semantic_payload())
    return VideoColorEvaluation(
        **probe.model_dump(
            mode='python',
            exclude={'evaluation_sha256', 'evaluation_id'},
        ),
        evaluation_id='vce-' + digest[:24],
        evaluation_sha256=digest,
    )


def color_evaluation_status(
    evaluation: VideoColorEvaluation,
) -> EvaluationStatus:
    """Fold group statuses for callers that need one value — the groups
    themselves remain the authoritative record."""
    return _combine_status(tuple(group.status for group in evaluation.groups))
