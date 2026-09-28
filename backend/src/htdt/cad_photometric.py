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
  than a guess. Native (dark-room) and ambient-adjusted *effective* values
  are separate fields (#1015); the light-output evidence class actually
  used is machine-readable (#1016).
- :class:`AmbientReflectanceProfile` — how a display/screen surface returns
  *external ambient* light toward the viewer: a different physical quantity
  from projector image gain, with its own evidence tiers (#1050). Never
  inferred from ``nominal_gain`` or a marketing label.
- :class:`LuminanceMeasurement` — a metered reading (white level, black
  floor, contrast) retaining instrument, method and position context plus
  the exact scene/profile/ambient bindings a comparison requires (#1014).
- :func:`evaluate_photometric_state` — derives typed compatibility axes
  (same surface, scene state, display profile, operating mode, aperture,
  screen optics, ambient state, method) before comparing per criterion;
  incompatible or unbound applicability yields ``UNKNOWN``, never a numeric
  PASS (#1014). There is no hidden overall score.

Works polymorphically for projected and direct-view surfaces: the estimate
machinery accepts either a lumen-based projection chain or a display whose
peak/full-field luminance comes from a ``DirectViewDisplaySpecification``
photometric capability.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_video_geometry import EvaluationStatus, _combine_status
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload




PhotometricEvidenceClass = Literal['rated', 'user_measured', 'measured']
"""Evidence class of a light-output figure.

- ``rated``: manufacturer-published number for a reference condition.
- ``user_measured``: owner/tinkerer meter reading (unverified instrument).
- ``measured``: commissioning-grade instrument reading.
"""

PhotometricEvidencePolicy = Literal[
    'require_measured',
    'allow_user_measured_or_better',
    'allow_rated_estimate',
    'best_available_diagnostic',
]
"""Light-output evidence selection policy (#1016).

- ``require_measured``: commissioning gate — only ``measured`` readings may
  drive the estimate; anything weaker yields UNKNOWN.
- ``allow_user_measured_or_better``: owner meter readings allowed, ``rated``
  is not.
- ``allow_rated_estimate``: planning/diagnostic estimate — any class may be
  used; the actual class is recorded on the result.
- ``best_available_diagnostic``: strongest available class wins and is
  recorded; identical selection to ``allow_rated_estimate`` but the honest
  default for exploratory tooling.
"""

AmbientObservationQuantity = Literal[
    'screen_plane_illuminance',
    'room_illuminance',
    'reflected_luminance',
    'unknown',
]
"""What an ambient-light reading physically measures (#1050).

- ``screen_plane_illuminance``: lux incident on the display/screen plane —
  the only illuminance quantity a screen reflection equation may consume.
- ``room_illuminance``: a generic room lux reading elsewhere; it cannot be
  fed into a screen equation as if it were screen-plane illuminance.
- ``reflected_luminance``: an already-reflected screen/panel luminance
  (cd/m²) — strongest evidence, no reflectance model required.
- ``unknown``: quantity semantics unrecorded; unusable numerically.
"""

# Ordered weakest→strongest for evidence-policy selection (#1016).
_EVIDENCE_RANK: dict[PhotometricEvidenceClass, int] = {
    'rated': 0,
    'user_measured': 1,
    'measured': 2,
}

_POLICY_MINIMUM: dict[PhotometricEvidencePolicy, PhotometricEvidenceClass] = {
    'require_measured': 'measured',
    'allow_user_measured_or_better': 'user_measured',
    'allow_rated_estimate': 'rated',
    'best_available_diagnostic': 'rated',
}

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
    probe = ProjectorImagePerformanceProfile.model_construct(**canonicalize_payload(ProjectorImagePerformanceProfile, dict(
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
    )))
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
    probe = ScreenOpticalProfile.model_construct(**canonicalize_payload(ScreenOpticalProfile, dict(
        profile_id=profile_id,
        version=version,
        screen_material=screen_material,
        nominal_gain=nominal_gain,
        angular_gain=tuple(angular_gain),
        acoustically_transparent=acoustically_transparent,
        provenance=tuple(provenance),
        profile_sha256='',
    )))
    return ScreenOpticalProfile(
        **probe.model_dump(mode='python', exclude={'profile_sha256'}),
        profile_sha256=_hash(probe.semantic_payload()),
    )


class AmbientLightObservation(BaseModel):
    """Ambient light evidence at a location.

    ``declared_lux`` is the design intent, ``measured_lux`` is a meter reading
    with instrument/position/timestamp context. Neither is ever derived from
    the other; an observation may carry one, both, or neither (UNKNOWN).

    ``quantity_kind`` records what the reading physically measures (#1050):
    only ``screen_plane_illuminance`` may feed a screen-reflection equation
    and only ``reflected_luminance`` may bypass a reflectance model entirely.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    observation_id: str = Field(min_length=1)
    location_label: str | None = None
    declared_lux: float | None = Field(default=None, ge=0.0)
    measured_lux: float | None = Field(default=None, ge=0.0)
    measured_luminance_cd_m2: float | None = Field(default=None, ge=0.0)
    quantity_kind: AmbientObservationQuantity = 'unknown'
    incidence_geometry: str | None = None
    measured_at_utc: str | None = None
    instrument: str | None = None
    lighting_scene_ref: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()


AmbientReflectanceEvidenceKind = Literal[
    'measured_lift',
    'measured_reflectance',
    'manufacturer_reflectance',
    'user_declared',
]
"""Evidence tier of an ambient-reflectance figure (#1050).

``visual_only`` is deliberately absent: a surface color or marketing label
never authorizes a numerical ambient model.
"""


class AmbientReflectanceProfile(BaseModel):
    """How one display/screen surface returns external ambient light toward
    the viewer (#1050).

    This is *not* projector image gain: ``nominal_gain`` on
    :class:`ScreenOpticalProfile` describes on-axis gain for projector light
    and must never be reused as an ambient-reflection coefficient. Two
    numeric paths exist, in strict strength order:

    - ``measured_black_lift_cd_m2_per_lux`` — a measured effective luminance
      lift per unit screen-plane illuminance (strongest);
    - ``diffuse_reflectance_fraction`` — a bounded Lambertian reflectance
      (0–1) evidence figure; lift = rho · E_screen / π.

    ``alr_directional_evidence`` records whether exact angular/rejection
    data exists. When it does not, ALR-adjusted performance stays UNKNOWN —
    directional behavior is never inferred from ``nominal_gain``.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['ambient-reflectance-1'] = (
        'ambient-reflectance-1'
    )
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    surface_kind: Literal['projection', 'direct_view']
    screen_optical_profile_id: str | None = None
    screen_optical_profile_version: str | None = None
    screen_optical_profile_sha256: str | None = None
    display_specification_sha256: str | None = None
    diffuse_reflectance_fraction: float | None = Field(
        default=None, ge=0.0, le=1.0
    )
    measured_black_lift_cd_m2_per_lux: float | None = Field(
        default=None, ge=0.0
    )
    alr_directional_evidence: bool | None = None
    evidence_kind: AmbientReflectanceEvidenceKind | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    profile_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'profile_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'AmbientReflectanceProfile':
        provided = (
            self.screen_optical_profile_id is not None,
            self.screen_optical_profile_version is not None,
            self.screen_optical_profile_sha256 is not None,
        )
        if any(provided) and not all(provided):
            raise ValueError(
                'screen optical profile id, version and sha256 must be '
                'supplied together'
            )
        if self.diffuse_reflectance_fraction is not None and (
            self.evidence_kind is None
        ):
            raise ValueError(
                'diffuse reflectance requires an evidence kind — it is '
                'never implicit'
            )
        if self.profile_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'ambient reflectance profile semantic hash mismatch'
            )
        return self


def build_ambient_reflectance_profile(
    *,
    profile_id: str,
    version: str,
    surface_kind: Literal['projection', 'direct_view'],
    screen_optical_profile_id: str | None = None,
    screen_optical_profile_version: str | None = None,
    screen_optical_profile_sha256: str | None = None,
    display_specification_sha256: str | None = None,
    diffuse_reflectance_fraction: float | None = None,
    measured_black_lift_cd_m2_per_lux: float | None = None,
    alr_directional_evidence: bool | None = None,
    evidence_kind: AmbientReflectanceEvidenceKind | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> AmbientReflectanceProfile:
    probe = AmbientReflectanceProfile.model_construct(**canonicalize_payload(AmbientReflectanceProfile, dict(
        profile_id=profile_id,
        version=version,
        surface_kind=surface_kind,
        screen_optical_profile_id=screen_optical_profile_id,
        screen_optical_profile_version=screen_optical_profile_version,
        screen_optical_profile_sha256=screen_optical_profile_sha256,
        display_specification_sha256=display_specification_sha256,
        diffuse_reflectance_fraction=diffuse_reflectance_fraction,
        measured_black_lift_cd_m2_per_lux=(
            measured_black_lift_cd_m2_per_lux
        ),
        alr_directional_evidence=alr_directional_evidence,
        evidence_kind=evidence_kind,
        provenance=tuple(provenance),
        profile_sha256='',
    )))
    return AmbientReflectanceProfile(
        **probe.model_dump(mode='python', exclude={'profile_sha256'}),
        profile_sha256=_hash(probe.semantic_payload()),
    )


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
    surface_entity_id: str | None = None
    scene_revision_id: str = Field(min_length=1)
    scene_content_sha256: str = Field(min_length=16)
    projector_image_profile_id: str | None = None
    projector_image_profile_version: str | None = None
    projector_image_profile_sha256: str | None = None
    screen_optical_profile_id: str | None = None
    screen_optical_profile_version: str | None = None
    screen_optical_profile_sha256: str | None = None
    display_specification_sha256: str | None = None
    operating_mode: str | None = None
    aperture_width_m: float | None = Field(default=None, gt=0.0)
    aperture_height_m: float | None = Field(default=None, gt=0.0)
    ambient_observation_id: str | None = None
    ambient_reflectance_profile_id: str | None = None
    ambient_reflectance_profile_version: str | None = None
    ambient_reflectance_profile_sha256: str | None = None
    light_output_evidence_class: PhotometricEvidenceClass | None = None
    light_output_evidence_policy: PhotometricEvidencePolicy | None = None
    status: EvaluationStatus
    status_reason: str
    predicted_peak_white_cd_m2: float | None = None
    predicted_black_floor_cd_m2: float | None = None
    predicted_on_off_contrast: float | None = None
    predicted_native_peak_white_cd_m2: float | None = None
    predicted_native_black_floor_cd_m2: float | None = None
    predicted_native_on_off_contrast: float | None = None
    ambient_black_lift_cd_m2: float | None = None
    ambient_model: str | None = None
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
    from every prediction. Method context is retained verbatim.

    The optional binding fields pin the exact applicable state the reading
    was taken under (#1014): scene revision, display/projector profile,
    screen optics, aperture and ambient observation. A measurement missing
    a binding is still valid historical evidence — it simply cannot
    validate a different current state (UNKNOWN applicability).
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    measurement_id: str = Field(min_length=1)
    measured_at_utc: str = Field(min_length=1)
    instrument: str | None = None
    method: str | None = None
    surface_kind: Literal['projection', 'direct_view']
    surface_entity_id: str = Field(min_length=1)
    scene_revision_id: str | None = None
    scene_content_sha256: str | None = None
    projector_image_profile_id: str | None = None
    projector_image_profile_version: str | None = None
    projector_image_profile_sha256: str | None = None
    screen_optical_profile_id: str | None = None
    screen_optical_profile_version: str | None = None
    screen_optical_profile_sha256: str | None = None
    display_specification_sha256: str | None = None
    operating_mode: str | None = None
    aperture_width_m: float | None = Field(default=None, gt=0.0)
    aperture_height_m: float | None = Field(default=None, gt=0.0)
    ambient_observation_id: str | None = None
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
    blocking_axes: tuple[str, ...] = ()
    note: str | None = None


CompatibilityAxis = Literal[
    'compatible', 'incompatible', 'unbound', 'not_applicable'
]
"""One applicability axis between estimate and measurement (#1014).

- ``compatible``: both sides bind the axis and agree (or agree to disagree
  is impossible).
- ``incompatible``: both sides bind the axis and disagree — the measurement
  describes a different configuration.
- ``unbound``: at least one side does not pin the axis — applicability
  cannot be proven.
- ``not_applicable``: the axis does not exist for this surface kind (e.g.
  screen optics on a direct-view display).
"""


class PhotometricCompatibilityAxes(BaseModel):
    """Typed applicability result derived before any numeric comparison
    (#1014). Only ``compatible``/``not_applicable`` axes may admit a
    numeric PASS/FAIL; anything else yields UNKNOWN per criterion.
    """

    model_config = ConfigDict(frozen=True)

    same_surface: CompatibilityAxis
    same_scene_state: CompatibilityAxis
    same_display_profile: CompatibilityAxis
    same_operating_mode: CompatibilityAxis
    same_aperture: CompatibilityAxis
    same_screen_optics: CompatibilityAxis
    same_ambient_state: CompatibilityAxis
    compatible_method: CompatibilityAxis

    def blockers(self, required: tuple[str, ...]) -> tuple[str, ...]:
        """Names of required axes that are not satisfied."""
        blocked = []
        for axis in required:
            state = getattr(self, axis)
            if state in ('incompatible', 'unbound'):
                blocked.append(f'{axis}:{state}')
        return tuple(blocked)


class PhotometricEvaluation(BaseModel):
    """Per-criterion comparison of one estimate vs one measurement set.

    There is deliberately no hidden overall score — callers judge the listed
    criteria individually. The compatibility axes are part of the result so
    a replayed evaluation can re-derive why each criterion was or was not
    eligible (#1014).
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    evaluation_id: str = Field(min_length=1)
    estimate: ExpectedLuminanceEstimate | None
    measurement: LuminanceMeasurement | None
    compatibility: PhotometricCompatibilityAxes | None
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


def _select_light_output(
    readings: tuple[LightOutputReading, ...],
    policy: PhotometricEvidencePolicy,
) -> tuple[LightOutputReading | None, PhotometricEvidenceClass | None]:
    """Deterministic evidence-tier selection (#1016).

    Among readings of the selected class, the most recent timestamped
    reading wins; ties and missing timestamps resolve to the conservative
    (lowest) lumen figure — the estimate under-predicts rather than
    over-predicts light output.
    """

    minimum = _POLICY_MINIMUM[policy]
    eligible = [
        r for r in readings
        if _EVIDENCE_RANK[r.evidence_class] >= _EVIDENCE_RANK[minimum]
    ]
    if not eligible:
        return None, None
    strongest_rank = max(_EVIDENCE_RANK[r.evidence_class] for r in eligible)
    strongest = [r for r in eligible
                 if _EVIDENCE_RANK[r.evidence_class] == strongest_rank]
    # Most recent timestamped reading wins; a missing timestamp counts as
    # oldest, and equal timestamps take the lower lumens (conservative).
    strongest.sort(key=lambda r: r.lumens)
    strongest.sort(
        key=lambda r: (r.measured_at_utc is not None, r.measured_at_utc),
        reverse=True,
    )
    chosen = strongest[0]
    return chosen, chosen.evidence_class


def _ambient_black_lift(
    ambient_observation: AmbientLightObservation | None,
    reflectance: AmbientReflectanceProfile | None,
    notes: list[str],
) -> tuple[float | None, str | None]:
    """Modeled ambient-reflected luminance at the surface (#1050).

    Returns ``(lift_cd_m2, model)``. ``(None, None)`` means ambient light
    could not be modeled — never silently substituted by screen image gain.
    """

    if ambient_observation is None:
        return None, None
    if ambient_observation.quantity_kind == 'reflected_luminance':
        if ambient_observation.measured_luminance_cd_m2 is not None:
            return (
                ambient_observation.measured_luminance_cd_m2,
                'reflected-luminance-v1',
            )
        notes.append(
            'ambient observation declared reflected luminance but carries '
            'no measured luminance — ambient contribution UNKNOWN'
        )
        return None, None
    if ambient_observation.quantity_kind == 'room_illuminance':
        notes.append(
            'ambient lux was measured away from the screen plane — it is '
            'not screen-plane illuminance, ambient contribution UNKNOWN'
        )
        return None, None
    if ambient_observation.quantity_kind not in (
        'screen_plane_illuminance', 'unknown'
    ):
        notes.append('ambient observation quantity unrecognized')
        return None, None
    if ambient_observation.measured_lux is None:
        notes.append(
            'ambient observation carries no measured lux'
        )
        return None, None
    if ambient_observation.quantity_kind == 'unknown':
        notes.append(
            'ambient lux quantity semantics unrecorded — cannot feed the '
            'screen-plane reflection equation'
        )
        return None, None
    illuminance = ambient_observation.measured_lux
    if reflectance is None:
        notes.append(
            'no ambient reflectance authority — ambient contribution '
            'UNKNOWN (screen image gain is not an ambient-reflection '
            'coefficient)'
        )
        return None, None
    if reflectance.measured_black_lift_cd_m2_per_lux is not None:
        return (
            reflectance.measured_black_lift_cd_m2_per_lux * illuminance,
            'measured-lift-v1',
        )
    if reflectance.diffuse_reflectance_fraction is not None:
        return (
            reflectance.diffuse_reflectance_fraction
            * illuminance
            / 3.141592653589793,
            'diffuse-reflectance-v1',
        )
    notes.append(
        'ambient reflectance profile carries no measured lift or '
        'reflectance figure — ambient contribution UNKNOWN'
    )
    return None, None


def estimate_projection_luminance(
    *,
    scene_revision_id: str,
    scene_content_sha256: str,
    aperture_width_m: float,
    aperture_height_m: float,
    projector_profile: ProjectorImagePerformanceProfile | None,
    screen_profile: ScreenOpticalProfile | None,
    ambient_observation: AmbientLightObservation | None = None,
    ambient_reflectance: AmbientReflectanceProfile | None = None,
    surface_entity_id: str | None = None,
    evidence_policy: PhotometricEvidencePolicy = 'best_available_diagnostic',
) -> ExpectedLuminanceEstimate:
    """Predict on-screen peak luminance for a projected image.

    Light output is selected under an explicit ``evidence_policy`` (#1016):
    the actual evidence class used is recorded on the result and the
    policy's minimum tier fails closed (UNKNOWN). Screen image gain applies
    only to projector light; ambient reflected luminance comes from an
    :class:`AmbientReflectanceProfile` or a ``reflected_luminance``
    observation — never from ``nominal_gain`` (#1050). Where ambient is
    modeled, white and black both gain the same additive luminance and
    effective contrast is recomputed (``effective-on-off-v1``, #1015).
    """

    notes: list[str] = []
    reading: LightOutputReading | None = None
    used_class: PhotometricEvidenceClass | None = None
    if projector_profile is None:
        notes.append('no projector image performance profile supplied')
    elif not projector_profile.light_output:
        notes.append('profile carries no light-output readings')
    else:
        reading, used_class = _select_light_output(
            projector_profile.light_output, evidence_policy
        )
        if reading is None:
            notes.append(
                f'no light-output reading satisfies evidence policy '
                f'{evidence_policy}'
            )
        else:
            notes.append(
                f'light output {reading.lumens} lm from {used_class} '
                f'evidence (policy {evidence_policy})'
            )

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
    if (
        reading is not None
        and gain is not None
        and transmission is not None
    ):
        # illuminance = lumens/area (lux on screen); luminance = E * gain / pi
        native_white = (
            reading.lumens * transmission / area * gain
            / 3.141592653589793
        )
        status: EvaluationStatus = 'PASS'
        reason = {
            'measured': 'predicted from measured evidence',
            'user_measured': 'predicted from user-measured evidence',
            'rated': 'predicted from rated specification',
        }[used_class]
    else:
        native_white = None
        status = 'UNKNOWN'
        reason = '; '.join(notes)

    native_black: float | None = None
    native_contrast: float | None = None
    if (
        native_white is not None
        and projector_profile is not None
        and projector_profile.on_off_contrast_ratio is not None
    ):
        native_contrast = projector_profile.on_off_contrast_ratio
        native_black = native_white / native_contrast

    ambient_lift, ambient_model = _ambient_black_lift(
        ambient_observation, ambient_reflectance, notes
    )

    # effective-on-off-v1: ambient reflected luminance is additive on both
    # white and black fields; effective contrast is recomputed from the
    # ambient-adjusted pair, never left equal to the native ratio (#1015).
    if ambient_lift is not None:
        effective_white = (
            native_white + ambient_lift if native_white is not None else None
        )
        effective_black = (
            native_black + ambient_lift if native_black is not None else None
        )
        notes.append(
            f'ambient model {ambient_model}: +{ambient_lift:.4g} cd/m² on '
            'white and black fields'
        )
    else:
        effective_white = native_white
        effective_black = native_black
    effective_contrast: float | None = None
    if (
        effective_white is not None
        and effective_black is not None
        and effective_black > 0.0
    ):
        effective_contrast = effective_white / effective_black

    probe = ExpectedLuminanceEstimate.model_construct(**canonicalize_payload(ExpectedLuminanceEstimate, dict(
        estimate_id='',
        surface_kind='projection',
        surface_entity_id=surface_entity_id,
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
        operating_mode=(
            projector_profile.operating_mode if projector_profile else None
        ),
        aperture_width_m=aperture_width_m,
        aperture_height_m=aperture_height_m,
        ambient_observation_id=(
            ambient_observation.observation_id if ambient_observation else None
        ),
        ambient_reflectance_profile_id=(
            ambient_reflectance.profile_id if ambient_reflectance else None
        ),
        ambient_reflectance_profile_version=(
            ambient_reflectance.version if ambient_reflectance else None
        ),
        ambient_reflectance_profile_sha256=(
            ambient_reflectance.profile_sha256 if ambient_reflectance else None
        ),
        light_output_evidence_class=used_class,
        light_output_evidence_policy=evidence_policy,
        status=status,
        status_reason=reason,
        predicted_peak_white_cd_m2=effective_white,
        predicted_black_floor_cd_m2=effective_black,
        predicted_on_off_contrast=effective_contrast,
        predicted_native_peak_white_cd_m2=native_white,
        predicted_native_black_floor_cd_m2=native_black,
        predicted_native_on_off_contrast=native_contrast,
        ambient_black_lift_cd_m2=ambient_lift,
        ambient_model=ambient_model,
        input_notes=tuple(notes),
        estimate_sha256='',
    )))
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
    ambient_reflectance: AmbientReflectanceProfile | None = None,
    surface_entity_id: str | None = None,
    operating_mode: str | None = None,
) -> ExpectedLuminanceEstimate:
    """Predict direct-view surface luminance from a display spec's exact
    photometric capability figures (no optical chain to model).

    Ambient light affects the result only through an explicit panel
    reflectance model or a ``reflected_luminance`` observation (#1050):
    the intrinsic spec figures stay on the ``predicted_native_*`` fields,
    and ambient-adjusted values are produced only when a valid ambient
    model exists — otherwise ``ambient_black_lift_cd_m2`` stays ``None``
    and the observation is visibly unmodeled, never silently zero.
    """

    notes: list[str] = []
    if peak_luminance_cd_m2 is None:
        status: EvaluationStatus = 'UNKNOWN'
        reason = 'display specification carries no peak luminance figure'
    else:
        status = 'PASS'
        reason = 'predicted from display photometric capability'
    native_contrast = None
    if (
        peak_luminance_cd_m2 is not None
        and black_level_cd_m2 is not None
        and black_level_cd_m2 > 0.0
    ):
        native_contrast = peak_luminance_cd_m2 / black_level_cd_m2

    ambient_lift, ambient_model = _ambient_black_lift(
        ambient_observation, ambient_reflectance, notes
    )
    if ambient_lift is not None:
        effective_white = (
            peak_luminance_cd_m2 + ambient_lift
            if peak_luminance_cd_m2 is not None
            else None
        )
        effective_black = (
            black_level_cd_m2 + ambient_lift
            if black_level_cd_m2 is not None
            else None
        )
        notes.append(
            f'ambient model {ambient_model}: +{ambient_lift:.4g} cd/m² on '
            'white and black fields'
        )
    else:
        effective_white = peak_luminance_cd_m2
        effective_black = black_level_cd_m2
    effective_contrast: float | None = None
    if (
        effective_white is not None
        and effective_black is not None
        and effective_black > 0.0
    ):
        effective_contrast = effective_white / effective_black

    probe = ExpectedLuminanceEstimate.model_construct(**canonicalize_payload(ExpectedLuminanceEstimate, dict(
        estimate_id='',
        surface_kind='direct_view',
        surface_entity_id=surface_entity_id,
        scene_revision_id=scene_revision_id,
        scene_content_sha256=scene_content_sha256,
        projector_image_profile_id=None,
        projector_image_profile_version=None,
        projector_image_profile_sha256=None,
        screen_optical_profile_id=None,
        screen_optical_profile_version=None,
        screen_optical_profile_sha256=None,
        display_specification_sha256=display_specification_sha256,
        operating_mode=operating_mode,
        aperture_width_m=None,
        aperture_height_m=None,
        ambient_observation_id=(
            ambient_observation.observation_id if ambient_observation else None
        ),
        ambient_reflectance_profile_id=(
            ambient_reflectance.profile_id if ambient_reflectance else None
        ),
        ambient_reflectance_profile_version=(
            ambient_reflectance.version if ambient_reflectance else None
        ),
        ambient_reflectance_profile_sha256=(
            ambient_reflectance.profile_sha256 if ambient_reflectance else None
        ),
        light_output_evidence_class=None,
        light_output_evidence_policy=None,
        status=status,
        status_reason=reason,
        predicted_peak_white_cd_m2=effective_white,
        predicted_black_floor_cd_m2=effective_black,
        predicted_on_off_contrast=effective_contrast,
        predicted_native_peak_white_cd_m2=peak_luminance_cd_m2,
        predicted_native_black_floor_cd_m2=black_level_cd_m2,
        predicted_native_on_off_contrast=native_contrast,
        ambient_black_lift_cd_m2=ambient_lift,
        ambient_model=ambient_model,
        input_notes=tuple(notes),
        estimate_sha256='',
    )))
    digest = _hash(probe.semantic_payload())
    return ExpectedLuminanceEstimate(
        **probe.model_dump(
            mode='python',
            exclude={'estimate_sha256', 'estimate_id'},
        ),
        estimate_id='ele-' + digest[:24],
        estimate_sha256=digest,
    )


def _axis_state(
    estimate_value,
    measurement_value,
    *,
    applicable: bool = True,
    none_means_absent: bool = False,
) -> CompatibilityAxis:
    if not applicable:
        return 'not_applicable'
    if estimate_value is None and measurement_value is None:
        # For axes where None means "this input does not exist" (e.g. no
        # ambient observation bound), both-None is an agreement, not a
        # missing binding.
        return 'compatible' if none_means_absent else 'unbound'
    if estimate_value is None or measurement_value is None:
        return 'unbound'
    return (
        'compatible' if estimate_value == measurement_value
        else 'incompatible'
    )


def _axis_triple(
    estimate_triple: tuple[str | None, str | None, str | None],
    measurement_triple: tuple[str | None, str | None, str | None],
    *,
    applicable: bool = True,
) -> CompatibilityAxis:
    """Compare (id, version, sha256) bindings — all-or-none profiles."""
    if not applicable:
        return 'not_applicable'
    if None in estimate_triple or None in measurement_triple:
        return 'unbound'
    return (
        'compatible' if estimate_triple == measurement_triple
        else 'incompatible'
    )


def _aperture_axis(
    estimate: ExpectedLuminanceEstimate,
    measurement: LuminanceMeasurement,
    *,
    applicable: bool,
) -> CompatibilityAxis:
    if not applicable:
        return 'not_applicable'
    pairs = (
        (estimate.aperture_width_m, measurement.aperture_width_m),
        (estimate.aperture_height_m, measurement.aperture_height_m),
    )
    if any(a is None or b is None for a, b in pairs):
        return 'unbound'
    for a, b in pairs:
        if abs(a - b) > 1e-9 * max(a, b):
            return 'incompatible'
    return 'compatible'


def _compatibility_axes(
    estimate: ExpectedLuminanceEstimate,
    measurement: LuminanceMeasurement,
) -> PhotometricCompatibilityAxes:
    projection = (
        estimate.surface_kind == 'projection'
        and measurement.surface_kind == 'projection'
    )
    direct_view = (
        estimate.surface_kind == 'direct_view'
        and measurement.surface_kind == 'direct_view'
    )
    if projection:
        same_display_profile = _axis_triple(
            (
                estimate.projector_image_profile_id,
                estimate.projector_image_profile_version,
                estimate.projector_image_profile_sha256,
            ),
            (
                measurement.projector_image_profile_id,
                measurement.projector_image_profile_version,
                measurement.projector_image_profile_sha256,
            ),
        )
    elif direct_view:
        same_display_profile = _axis_state(
            estimate.display_specification_sha256,
            measurement.display_specification_sha256,
        )
    else:
        same_display_profile = 'not_applicable'
    scene_state = _axis_state(
        estimate.scene_revision_id, measurement.scene_revision_id
    )
    if scene_state == 'compatible':
        scene_state = _axis_state(
            estimate.scene_content_sha256, measurement.scene_content_sha256
        )
    same_surface = _axis_state(
        estimate.surface_kind, measurement.surface_kind
    )
    if same_surface == 'compatible':
        same_surface = _axis_state(
            estimate.surface_entity_id, measurement.surface_entity_id
        )
    return PhotometricCompatibilityAxes(
        same_surface=same_surface,
        same_scene_state=scene_state,
        same_display_profile=same_display_profile,
        same_operating_mode=_axis_state(
            estimate.operating_mode, measurement.operating_mode
        ),
        same_aperture=_aperture_axis(
            estimate, measurement, applicable=projection
        ),
        same_screen_optics=_axis_triple(
            (
                estimate.screen_optical_profile_id,
                estimate.screen_optical_profile_version,
                estimate.screen_optical_profile_sha256,
            ),
            (
                measurement.screen_optical_profile_id,
                measurement.screen_optical_profile_version,
                measurement.screen_optical_profile_sha256,
            ),
            applicable=projection,
        ),
        same_ambient_state=_axis_state(
            estimate.ambient_observation_id,
            measurement.ambient_observation_id,
            none_means_absent=True,
        ),
        compatible_method=(
            'unbound' if measurement.method is None else 'compatible'
        ),
    )


# Applicability axes each criterion requires before a numeric PASS/FAIL.
_CRITERION_AXES: dict[str, tuple[str, ...]] = {
    'peak_white_luminance': (
        'same_surface',
        'same_scene_state',
        'same_display_profile',
        'same_operating_mode',
        'same_aperture',
        'same_screen_optics',
        'same_ambient_state',
        'compatible_method',
    ),
    'black_floor': (
        'same_surface',
        'same_scene_state',
        'same_display_profile',
        'same_operating_mode',
        'same_aperture',
        'same_screen_optics',
        'same_ambient_state',
        'compatible_method',
    ),
    'on_off_contrast': (
        'same_surface',
        'same_scene_state',
        'same_display_profile',
        'same_operating_mode',
        'same_aperture',
        'same_screen_optics',
        'same_ambient_state',
        'compatible_method',
    ),
}


def _criterion(
    name: str,
    predicted: float | None,
    measured: float | None,
    tolerance_fraction: float,
    compatibility: PhotometricCompatibilityAxes | None,
) -> PhotometricCriterionResult:
    blocking: tuple[str, ...] = ()
    note: str | None = None
    if compatibility is not None:
        blocking = compatibility.blockers(_CRITERION_AXES[name])
        if blocking:
            note = 'not applicable to the same configuration'
    status: EvaluationStatus
    if blocking:
        status = 'UNKNOWN'
    elif predicted is None or measured is None:
        status = 'UNKNOWN'
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
        blocking_axes=blocking,
        note=note,
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
    predicted side as missing without inventing values. When both sides are
    present, typed compatibility axes are derived first (#1014): a criterion
    can only produce PASS/FAIL when estimate and measurement provably
    describe the same surface, scene state, display profile, operating mode,
    aperture, screen optics, ambient state and method; otherwise it reports
    UNKNOWN with the blocking axes named.
    """

    compatibility = (
        _compatibility_axes(estimate, measurement)
        if estimate is not None and measurement is not None
        else None
    )

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
            compatibility,
        ),
        _criterion(
            'black_floor',
            predicted_black,
            measured_black,
            black_floor_tolerance_fraction,
            compatibility,
        ),
        _criterion(
            'on_off_contrast',
            predicted_contrast,
            measured_contrast,
            contrast_tolerance_fraction,
            compatibility,
        ),
    )
    probe = PhotometricEvaluation.model_construct(**canonicalize_payload(PhotometricEvaluation, dict(
        evaluation_id='',
        estimate=estimate,
        measurement=measurement,
        compatibility=compatibility,
        criteria=criteria,
        evaluation_sha256='',
    )))
    digest = _hash(probe.semantic_payload())
    return PhotometricEvaluation(
        **probe.model_dump(
            mode='python',
            exclude={'evaluation_sha256', 'evaluation_id'},
        ),
        evaluation_id='phe-' + digest[:24],
        evaluation_sha256=digest,
    )
