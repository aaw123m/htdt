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
  named metric family at an explicit version (e.g. ``dE2000`` or
  ``dEICtCp``). Criterion groups (white point, grayscale/EOTF, gamut, …)
  each report PASS / FAIL / UNKNOWN / NOT_APPLICABLE; there is no hidden
  overall score.
- Applied-calibration state lives in a separate
  :class:`AppliedCalibrationState` record so "the device was set to X" is
  never conflated with "measured state met the target".

All authorities are frozen, self-hashed and append-only like the other CAD
authorities. Imported measurement sets keep the importer app/version, the
source asset hash and the parser id so the provenance chain survives.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_video_geometry import EvaluationStatus, _combine_status


def _hash(payload) -> str:
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()


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
    constrained by this target (reported NOT_APPLICABLE)."""

    model_config = ConfigDict(frozen=True)

    white_point_delta_e: float | None = Field(default=None, ge=0.0)
    grayscale_delta_e: float | None = Field(default=None, ge=0.0)
    gamut_delta_e: float | None = Field(default=None, ge=0.0)
    eotf_deviation_fraction: float | None = Field(
        default=None, ge=0.0
    )


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
    probe = VideoColorTargetProfile.model_construct(
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
    )
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
    """Canonical XYZ tristimulus reading for one stimulus level/point."""

    model_config = ConfigDict(frozen=True)

    stimulus_id: str = Field(min_length=1)
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
    probe = VideoColorMeasurementSet.model_construct(
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
    )
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


def _xy_from_xyz(sample: TristimulusSample) -> tuple[float, float] | None:
    total = sample.x + sample.y_luminance + sample.z
    if total <= 0.0:
        return None
    return sample.x / total, sample.y_luminance / total


def _delta_e_2000_xy(
    measured_xy: tuple[float, float],
    target_xy: tuple[float, float],
) -> float:
    """Euclidean xy-chromaticity distance scaled to a ΔE-like magnitude.

    This is a commissioning screening metric, not CIEDE2000 — the family is
    recorded explicitly on the evaluation so consumers know exactly what was
    computed. Kept deliberately simple; full Lab/CIEDE2000 conversion is out
    of scope for the twin's v1.
    """

    dx = measured_xy[0] - target_xy[0]
    dy = measured_xy[1] - target_xy[1]
    return (dx * dx + dy * dy) ** 0.5 * 100.0


def _sample_group(stimulus_id: str) -> str:
    sid = stimulus_id.lower()
    if sid in ('white', 'w100', 'white_100') or sid.startswith('white_'):
        return 'white_point'
    if any(sid.startswith(p) for p in _GRAYSCALE_PREFIXES):
        return 'grayscale'
    if any(sid.startswith(p) for p in _GAMUT_PREFIXES):
        return 'gamut'
    return 'other'


def evaluate_video_color(
    *,
    target: VideoColorTargetProfile,
    measurement_set: VideoColorMeasurementSet,
    metric_family: ColorMetricFamily = 'dE2000',
    metric_version: str = 'htdt-xy-screen-1',
) -> VideoColorEvaluation:
    """Evaluate a measurement set against a target under one metric family.

    Groups: ``white_point``, ``grayscale``, ``gamut``. A group is UNKNOWN when
    the target constrains it but the set has no usable samples, NOT_APPLICABLE
    when the target sets no tolerance for it, and FAIL when any usable sample
    exceeds the group tolerance. Meter-floor samples are counted but excluded
    from worst-case deltas.
    """

    tol = target.tolerances
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
            xy = _xy_from_xyz(sample)
            if xy is None:
                continue
            deltas.append(
                _delta_e_2000_xy(xy, (target_xy.x, target_xy.y))
            )
        if not deltas:
            return ColorCriterionResult(
                group=name,
                status='UNKNOWN',
                measured_points=len(samples),
                note='no usable XYZ samples',
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
            xy = _xy_from_xyz(sample)
            if point is None or xy is None or sample.at_meter_floor:
                continue
            counted += 1
            deltas.append(_delta_e_2000_xy(xy, (point.x, point.y)))
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

    probe = VideoColorEvaluation.model_construct(
        evaluation_id='',
        target_id=target.target_id,
        target_version=target.version,
        target_sha256=target.target_sha256,
        measurement_set_id=measurement_set.measurement_set_id,
        measurement_set_sha256=measurement_set.measurement_set_sha256,
        metric_family=metric_family,
        metric_version=metric_version,
        groups=tuple(groups),
        evaluation_sha256='',
    )
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
