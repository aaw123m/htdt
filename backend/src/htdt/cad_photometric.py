"""Video photometric/HDR commissioning authority (#557).

Extends the projection/display digital twin beyond geometry with light-output,
screen-gain, ambient-light and contrast evidence. The module deliberately keeps
predicted values and measured values in separate authority objects:

- :class:`ProjectorImagePerformanceProfile` — one operating mode of one
  projector (lamp mode, iris, filter), bound to an exact projector
  specification id/version/hash. Light output is declared per evidence class
  (``rated`` from a datasheet, ``user_measured`` from an owner meter,
  ``measured`` from a commissioning instrument) and never blended.
- :class:`ScreenOpticalProfile` — optical behaviour of the screen material,
  independent of the aperture geometry. Nominal on-axis gain may come from the
  manufacturer; angular gain is only representable when exact per-angle data
  exists — never synthesised from a gain curve guess.
- :class:`ToneMappingState` — the display/projector tone-mapping behaviour
  (mode, target peak, dynamic/static) kept separate from peak luminance so a
  bright projector does not imply good HDR handling.
- :class:`AmbientLightObservation` — ambient light at a location, with the
  declared (design target) value kept strictly separate from the measured
  lux reading; one is never derived from the other.
- :class:`ExpectedLuminanceEstimate` — a predicted on-screen luminance bound to
  the exact scene revision, projector spec/mode profile, screen optical
  profile and active aperture versions involved. Any missing input (e.g.
  lens/throw light loss without an exact profile) yields ``UNKNOWN`` rather
  than a guess.
- :class:`LuminanceMeasurement` — a metered reading (white level, black
  floor, contrast) retaining instrument, method and position context.
- :func:`evaluate_photometric_state` — compares one estimate with one
  measurement set per criterion (peak luminance, black floor, on/off
  contrast) with per-criterion statuses and no hidden overall score.

Works polymorphically for projected and direct-view surfaces: the estimate
machinery accepts either a lumen-based projection chain or a display whose
peak/full-field luminance comes from a ``DirectViewDisplaySpecification``
photometric capability.
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


PhotometricEvidenceClass = Literal['rated', 'user_measured', 'measured']
"""Evidence class of a light-output figure.

- ``rated``: manufacturer-published number for a reference condition.
- ``user_measured``: owner/tinkerer meter reading (unverified instrument).
- ``measured``: commissioning-grade instrument reading.
"""

HDRFormatFamily = Literal[
    'hdr10', 'hdr10_plus', 'dolby_vision', 'hlg', 'sdr', 'other', 'unknown'
]


class LightOutputReading(BaseModel):
    """One light-output figure with its evidence class kept explicit."""

    model_config = ConfigDict(frozen=True)

    evidence_class: PhotometricEvidenceClass
    lumens: float = Field(gt=0.0)
    measured_at_utc: str | None = None
    instrument: str | None = None


class ToneMappingState(BaseModel):
    """Tone-mapping behaviour, deliberately separate from peak luminance."""

    model_config = ConfigDict(frozen=True)

    mode: str = Field(min_length=1)
    dynamic: bool | None = None
    target_peak_cd_m2: float | None = Field(default=None, gt=0.0)
    note: str | None = None


class ProjectorImagePerformanceProfile(BaseModel):
    """One operating mode of one projector — e.g. "eco lamp, iris -5".

    Multiple profiles per projector are expected. The profile is bound to an
    exact projector specification (id + version + sha256 triple, all-or-none)
    so an estimate can never silently follow a spec change. Placement lives in
    the geometry authority — this profile covers light-path behaviour only.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['projector-image-performance-1'] = (
        'projector-image-performance-1'
    )
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    label: str | None = None
    operating_mode: str | None = None
    projector_specification_id: str | None = None
    projector_specification_version: str | None = None
    projector_specification_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    light_output: tuple[LightOutputReading, ...] = ()
    lens_transmission_fraction: float | None = Field(
        default=None, gt=0.0, le=1.0
    )
    lens_transmission_evidence_class: PhotometricEvidenceClass | None = None
    on_off_contrast_ratio: float | None = Field(default=None, gt=0.0)
    on_off_contrast_evidence_class: PhotometricEvidenceClass | None = None
    ansi_contrast_ratio: float | None = Field(default=None, gt=0.0)
    hdr_format_families: tuple[HDRFormatFamily, ...] = ()
    tone_mapping: ToneMappingState | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    profile_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'profile_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'ProjectorImagePerformanceProfile':
        provided = (
            self.projector_specification_id is not None,
            self.projector_specification_version is not None,
            self.projector_specification_sha256 is not None,
        )
        if any(provided) and not all(provided):
            raise ValueError(
                'projector specification id, version and sha256 must be '
                'supplied together'
            )
        if self.lens_transmission_fraction is not None and (
            self.lens_transmission_evidence_class is None
        ):
            raise ValueError(
                'lens transmission requires an evidence class — it is never '
                'implicitly rated'
            )
        if self.on_off_contrast_ratio is not None and (
            self.on_off_contrast_evidence_class is None
        ):
            raise ValueError(
                'on/off contrast requires an evidence class — it is never '
                'implicitly rated'
            )
        if self.profile_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'projector image performance profile semantic hash mismatch'
            )
        return self


def build_projector_image_performance_profile(
    *,
    profile_id: str,
    version: str,
    label: str | None = None,
    operating_mode: str | None = None,
    projector_specification_id: str | None = None,
    projector_specification_version: str | None = None,
    projector_specification_sha256: str | None = None,
    light_output: tuple[LightOutputReading, ...] = (),
    lens_transmission_fraction: float | None = None,
    lens_transmission_evidence_class: PhotometricEvidenceClass | None = None,
    on_off_contrast_ratio: float | None = None,
    on_off_contrast_evidence_class: PhotometricEvidenceClass | None = None,
    ansi_contrast_ratio: float | None = None,
    hdr_format_families: tuple[HDRFormatFamily, ...] = (),
    tone_mapping: ToneMappingState | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> ProjectorImagePerformanceProfile:
    probe = ProjectorImagePerformanceProfile.model_construct(
        profile_id=profile_id,
        version=version,
        label=label,
        operating_mode=operating_mode,
        projector_specification_id=projector_specification_id,
        projector_specification_version=projector_specification_version,
        projector_specification_sha256=projector_specification_sha256,
        light_output=tuple(light_output),
        lens_transmission_fraction=lens_transmission_fraction,
        lens_transmission_evidence_class=lens_transmission_evidence_class,
        on_off_contrast_ratio=on_off_contrast_ratio,
        on_off_contrast_evidence_class=on_off_contrast_evidence_class,
        ansi_contrast_ratio=ansi_contrast_ratio,
        hdr_format_families=tuple(hdr_format_families),
        tone_mapping=tone_mapping,
        provenance=tuple(provenance),
        profile_sha256='',
    )
    return ProjectorImagePerformanceProfile(
        **probe.model_dump(mode='python', exclude={'profile_sha256'}),
        profile_sha256=_hash(probe.semantic_payload()),
    )


class AngularGainSample(BaseModel):
    """One exact off-axis gain measurement of a screen material."""

    model_config = ConfigDict(frozen=True)

    angle_deg: float = Field(ge=0.0, le=90.0)
    gain: float = Field(gt=0.0)


class ScreenOpticalProfile(BaseModel):
    """Optical behaviour of one screen material, independent of aperture
    geometry.

    ``nominal_gain`` is the on-axis reference figure (manufacturer or
    measured). ``angular_gain`` is only populated when exact per-angle data
    exists — there is no curve synthesis.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['screen-optical-profile-1'] = (
        'screen-optical-profile-1'
    )
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    screen_material: str | None = None
    nominal_gain: float | None = Field(default=None, gt=0.0)
    angular_gain: tuple[AngularGainSample, ...] = ()
    acoustically_transparent: bool | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    profile_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'profile_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'ScreenOpticalProfile':
        angles = [s.angle_deg for s in self.angular_gain]
        if len(set(angles)) != len(angles):
            raise ValueError('angular gain samples must have unique angles')
        if self.profile_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'screen optical profile semantic hash mismatch'
            )
        return self


def build_screen_optical_profile(
    *,
    profile_id: str,
    version: str,
    screen_material: str | None = None,
    nominal_gain: float | None = None,
    angular_gain: tuple[AngularGainSample, ...] = (),
    acoustically_transparent: bool | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> ScreenOpticalProfile:
    probe = ScreenOpticalProfile.model_construct(
        profile_id=profile_id,
        version=version,
        screen_material=screen_material,
        nominal_gain=nominal_gain,
        angular_gain=tuple(angular_gain),
        acoustically_transparent=acoustically_transparent,
        provenance=tuple(provenance),
        profile_sha256='',
    )
    return ScreenOpticalProfile(
        **probe.model_dump(mode='python', exclude={'profile_sha256'}),
        profile_sha256=_hash(probe.semantic_payload()),
    )


class AmbientLightObservation(BaseModel):
    """Ambient light evidence at a location.

    ``declared_lux`` is the design intent, ``measured_lux`` is a meter reading
    with instrument/position/timestamp context. Neither is ever derived from
    the other; an observation may carry one, both, or neither (UNKNOWN).
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    observation_id: str = Field(min_length=1)
    location_label: str | None = None
    declared_lux: float | None = Field(default=None, ge=0.0)
    measured_lux: float | None = Field(default=None, ge=0.0)
    measured_at_utc: str | None = None
    instrument: str | None = None
    lighting_scene_ref: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()


class ExpectedLuminanceEstimate(BaseModel):
    """A predicted on-screen luminance for an exact configuration.

    Bound to the exact identities of every input — scene revision content
    hash, projector spec and mode profile, screen optical profile and the
    resolved aperture dimensions — so the prediction can never drift silently
    as upstream authorities change. ``status`` is UNKNOWN whenever an input
    required for an exact prediction is missing (e.g. lens/throw loss with no
    measured profile).
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    estimate_id: str = Field(min_length=1)
    surface_kind: Literal['projection', 'direct_view']
    scene_revision_id: str = Field(min_length=1)
    scene_content_sha256: str = Field(min_length=16)
    projector_image_profile_id: str | None = None
    projector_image_profile_version: str | None = None
    projector_image_profile_sha256: str | None = None
    screen_optical_profile_id: str | None = None
    screen_optical_profile_version: str | None = None
    screen_optical_profile_sha256: str | None = None
    display_specification_sha256: str | None = None
    aperture_width_m: float | None = Field(default=None, gt=0.0)
    aperture_height_m: float | None = Field(default=None, gt=0.0)
    ambient_observation_id: str | None = None
    status: EvaluationStatus
    status_reason: str
    predicted_peak_white_cd_m2: float | None = None
    predicted_black_floor_cd_m2: float | None = None
    predicted_on_off_contrast: float | None = None
    input_notes: tuple[str, ...] = ()
    estimate_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'estimate_sha256', 'estimate_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'ExpectedLuminanceEstimate':
        digest = _hash(self.semantic_payload())
        if self.estimate_sha256 != digest:
            raise ValueError('expected luminance estimate hash mismatch')
        if self.estimate_id != 'ele-' + digest[:24]:
            raise ValueError('expected luminance estimate id mismatch')
        return self


class LuminanceMeasurement(BaseModel):
    """Metered readings of the surface at a point in time — kept separate
    from every prediction. Method context is retained verbatim."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    measurement_id: str = Field(min_length=1)
    measured_at_utc: str = Field(min_length=1)
    instrument: str | None = None
    method: str | None = None
    surface_kind: Literal['projection', 'direct_view']
    surface_entity_id: str = Field(min_length=1)
    peak_white_cd_m2: float | None = Field(default=None, ge=0.0)
    black_floor_cd_m2: float | None = Field(default=None, ge=0.0)
    on_off_contrast_ratio: float | None = Field(default=None, ge=0.0)
    ambient_lux: float | None = Field(default=None, ge=0.0)
    hdr_format_family: HDRFormatFamily | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()


class PhotometricCriterionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    criterion: str = Field(min_length=1)
    status: EvaluationStatus
    predicted: float | None = None
    measured: float | None = None
    tolerance_fraction: float | None = None
    note: str | None = None


class PhotometricEvaluation(BaseModel):
    """Per-criterion comparison of one estimate vs one measurement set.

    There is deliberately no hidden overall score — callers judge the listed
    criteria individually.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    evaluation_id: str = Field(min_length=1)
    estimate: ExpectedLuminanceEstimate | None
    measurement: LuminanceMeasurement | None
    criteria: tuple[PhotometricCriterionResult, ...]
    evaluation_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'evaluation_sha256', 'evaluation_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'PhotometricEvaluation':
        digest = _hash(self.semantic_payload())
        if self.evaluation_sha256 != digest:
            raise ValueError('photometric evaluation hash mismatch')
        if self.evaluation_id != 'phe-' + digest[:24]:
            raise ValueError('photometric evaluation id mismatch')
        return self


def estimate_projection_luminance(
    *,
    scene_revision_id: str,
    scene_content_sha256: str,
    aperture_width_m: float,
    aperture_height_m: float,
    projector_profile: ProjectorImagePerformanceProfile | None,
    screen_profile: ScreenOpticalProfile | None,
    ambient_observation: AmbientLightObservation | None = None,
    reference_evidence_class: PhotometricEvidenceClass = 'measured',
) -> ExpectedLuminanceEstimate:
    """Predict on-screen peak luminance for a projected image.

    Uses the best available light-output reading (preferring the requested
    evidence class, else the strongest available) times screen gain over the
    exact active aperture area. Lens/throw loss is UNKNOWN unless the profile
    carries an exact ``lens_transmission_fraction``.
    """

    notes: list[str] = []
    lumens: float | None = None
    if projector_profile is None:
        notes.append('no projector image performance profile supplied')
    elif not projector_profile.light_output:
        notes.append('profile carries no light-output readings')
    else:
        by_class = {r.evidence_class: r.lumens
                    for r in projector_profile.light_output}
        lumens = by_class.get(reference_evidence_class)
        if lumens is None:
            # fall back to the best available evidence class, flagged
            for klass in ('measured', 'user_measured', 'rated'):
                if klass in by_class:
                    lumens = by_class[klass]
                    notes.append(
                        f'using {klass} light output (requested '
                        f'{reference_evidence_class} unavailable)'
                    )
                    break

    gain = screen_profile.nominal_gain if screen_profile is not None else None
    if gain is None:
        notes.append('no screen optical profile nominal gain')

    transmission = (
        projector_profile.lens_transmission_fraction
        if projector_profile is not None
        else None
    )
    if transmission is None:
        notes.append(
            'lens/throw light loss unknown — no exact transmission profile'
        )

    area = aperture_width_m * aperture_height_m
    if lumens is not None and gain is not None and transmission is not None:
        # illuminance = lumens/area (lux on screen); luminance = E * gain / pi
        predicted = lumens * transmission / area * gain / 3.141592653589793
        status: EvaluationStatus = 'PASS'
        reason = 'predicted from exact inputs'
    else:
        predicted = None
        status = 'UNKNOWN'
        reason = '; '.join(notes)

    black_floor: float | None = None
    contrast: float | None = None
    if (
        predicted is not None
        and projector_profile is not None
        and projector_profile.on_off_contrast_ratio is not None
    ):
        contrast = projector_profile.on_off_contrast_ratio
        black_floor = predicted / contrast
        if ambient_observation is not None and (
            ambient_observation.measured_lux is not None
        ):
            # ambient light adds a reflected black-floor component
            black_floor += (
                ambient_observation.measured_lux * (gain or 1.0) / 3.141592653589793
            )
            notes.append('black floor includes measured ambient lux')

    probe = ExpectedLuminanceEstimate.model_construct(
        estimate_id='',
        surface_kind='projection',
        scene_revision_id=scene_revision_id,
        scene_content_sha256=scene_content_sha256,
        projector_image_profile_id=(
            projector_profile.profile_id if projector_profile else None
        ),
        projector_image_profile_version=(
            projector_profile.version if projector_profile else None
        ),
        projector_image_profile_sha256=(
            projector_profile.profile_sha256 if projector_profile else None
        ),
        screen_optical_profile_id=(
            screen_profile.profile_id if screen_profile else None
        ),
        screen_optical_profile_version=(
            screen_profile.version if screen_profile else None
        ),
        screen_optical_profile_sha256=(
            screen_profile.profile_sha256 if screen_profile else None
        ),
        display_specification_sha256=None,
        aperture_width_m=aperture_width_m,
        aperture_height_m=aperture_height_m,
        ambient_observation_id=(
            ambient_observation.observation_id if ambient_observation else None
        ),
        status=status,
        status_reason=reason,
        predicted_peak_white_cd_m2=predicted,
        predicted_black_floor_cd_m2=black_floor,
        predicted_on_off_contrast=contrast,
        input_notes=tuple(notes),
        estimate_sha256='',
    )
    digest = _hash(probe.semantic_payload())
    return ExpectedLuminanceEstimate(
        **probe.model_dump(
            mode='python',
            exclude={'estimate_sha256', 'estimate_id'},
        ),
        estimate_id='ele-' + digest[:24],
        estimate_sha256=digest,
    )


def estimate_direct_view_luminance(
    *,
    scene_revision_id: str,
    scene_content_sha256: str,
    peak_luminance_cd_m2: float | None,
    black_level_cd_m2: float | None,
    display_specification_sha256: str | None,
    ambient_observation: AmbientLightObservation | None = None,
) -> ExpectedLuminanceEstimate:
    """Predict direct-view surface luminance from a display spec's exact
    photometric capability figures (no optical chain to model)."""

    notes: list[str] = []
    if peak_luminance_cd_m2 is None:
        status: EvaluationStatus = 'UNKNOWN'
        reason = 'display specification carries no peak luminance figure'
    else:
        status = 'PASS'
        reason = 'predicted from display photometric capability'
    contrast = None
    if (
        peak_luminance_cd_m2 is not None
        and black_level_cd_m2 is not None
        and black_level_cd_m2 > 0.0
    ):
        contrast = peak_luminance_cd_m2 / black_level_cd_m2

    probe = ExpectedLuminanceEstimate.model_construct(
        estimate_id='',
        surface_kind='direct_view',
        scene_revision_id=scene_revision_id,
        scene_content_sha256=scene_content_sha256,
        projector_image_profile_id=None,
        projector_image_profile_version=None,
        projector_image_profile_sha256=None,
        screen_optical_profile_id=None,
        screen_optical_profile_version=None,
        screen_optical_profile_sha256=None,
        display_specification_sha256=display_specification_sha256,
        aperture_width_m=None,
        aperture_height_m=None,
        ambient_observation_id=(
            ambient_observation.observation_id if ambient_observation else None
        ),
        status=status,
        status_reason=reason,
        predicted_peak_white_cd_m2=peak_luminance_cd_m2,
        predicted_black_floor_cd_m2=black_level_cd_m2,
        predicted_on_off_contrast=contrast,
        input_notes=tuple(notes),
        estimate_sha256='',
    )
    digest = _hash(probe.semantic_payload())
    return ExpectedLuminanceEstimate(
        **probe.model_dump(
            mode='python',
            exclude={'estimate_sha256', 'estimate_id'},
        ),
        estimate_id='ele-' + digest[:24],
        estimate_sha256=digest,
    )


def _criterion(
    name: str,
    predicted: float | None,
    measured: float | None,
    tolerance_fraction: float,
) -> PhotometricCriterionResult:
    if predicted is None or measured is None:
        status: EvaluationStatus = 'UNKNOWN'
    elif abs(measured - predicted) <= predicted * tolerance_fraction:
        status = 'PASS'
    else:
        status = 'FAIL'
    return PhotometricCriterionResult(
        criterion=name,
        status=status,
        predicted=predicted,
        measured=measured,
        tolerance_fraction=tolerance_fraction,
    )


def evaluate_photometric_state(
    *,
    estimate: ExpectedLuminanceEstimate | None,
    measurement: LuminanceMeasurement | None,
    peak_white_tolerance_fraction: float = 0.15,
    black_floor_tolerance_fraction: float = 0.30,
    contrast_tolerance_fraction: float = 0.30,
) -> PhotometricEvaluation:
    """Compare predicted vs measured photometric state per criterion.

    Both sides may be ``None`` — a measurement-only evaluation reports the
    predicted side as missing without inventing values.
    """

    predicted_white = (
        estimate.predicted_peak_white_cd_m2 if estimate is not None else None
    )
    predicted_black = (
        estimate.predicted_black_floor_cd_m2 if estimate is not None else None
    )
    predicted_contrast = (
        estimate.predicted_on_off_contrast if estimate is not None else None
    )
    measured_white = (
        measurement.peak_white_cd_m2 if measurement is not None else None
    )
    measured_black = (
        measurement.black_floor_cd_m2 if measurement is not None else None
    )
    measured_contrast = (
        measurement.on_off_contrast_ratio if measurement is not None else None
    )
    criteria = (
        _criterion(
            'peak_white_luminance',
            predicted_white,
            measured_white,
            peak_white_tolerance_fraction,
        ),
        _criterion(
            'black_floor',
            predicted_black,
            measured_black,
            black_floor_tolerance_fraction,
        ),
        _criterion(
            'on_off_contrast',
            predicted_contrast,
            measured_contrast,
            contrast_tolerance_fraction,
        ),
    )
    probe = PhotometricEvaluation.model_construct(
        evaluation_id='',
        estimate=estimate,
        measurement=measurement,
        criteria=criteria,
        evaluation_sha256='',
    )
    digest = _hash(probe.semantic_payload())
    return PhotometricEvaluation(
        **probe.model_dump(
            mode='python',
            exclude={'evaluation_sha256', 'evaluation_id'},
        ),
        evaluation_id='phe-' + digest[:24],
        evaluation_sha256=digest,
    )
