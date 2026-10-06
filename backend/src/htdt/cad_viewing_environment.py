"""Video viewing-environment authority (#633).

A display can produce the same measured pixels while the perceived
image changes because the observer is adapted to a different surround,
ambient-light state, wall colour/luminance or viewing geometry.  The
viewing environment is a first-class perceptual operating condition:

``DISPLAY_OUTPUT_STATE`` ≠ ``VIEWING_ENVIRONMENT_STATE``.

This module is the fail-closed authority over that separation:

- :class:`ViewingEnvironmentObservation` — the captured environment:
  ambient illuminance, *incident* illuminance on the image surface
  (never a substitute for general room lux), surround/periphery
  luminance, surround chromaticity/CCT with a spectral evidence class
  (CCT alone never claims an exact D65 spectral match), wall/surround
  material and reflectance, bias/reference-light state, stray-light
  sources, blind/curtain/door state, automation scene.
- :class:`ViewingGeometryObservation` — exact viewer/screen geometry:
  viewer position, screen plane, distance, h/v observation angle,
  screen angular size, eye height, seat identity.  Composes with #259
  and #625 — a reference calibration at the MLP never proves side-seat
  performance.
- :class:`LightingSceneRecord` — an automation scene (CALIBRATION /
  MOVIE_DARK / INTERMISSION / DAY_VIEWING / CLEANING/SERVICE / custom)
  binding exact lighting and blind states; many legitimate viewing
  conditions coexist — no single reference environment is required for
  all user scenarios.
- :func:`evaluate_viewing_environment` — seals a
  :class:`ViewingEnvironmentQualification` verdict against a declared
  external profile (BT.2035 reference HDTV, BT.500 lab/home subjective,
  BT.2166 critical HDR/SDR monitoring, project-defined, normal-use).
- :func:`compare_viewing_environments` — a non-sealed comparability
  result for before/after visual claims: if room lighting changed
  together with display calibration, both interventions are reported —
  never the display alone.

Contract properties:

- ``display calibrated`` never equals ``reference viewing condition
  established``;
- broadcast/production profiles are *method* profiles, not universal
  residential compliance rules — a home theater is never "defective"
  for not matching BT.2166 unless that profile was selected as a
  requirement;
- surround luminance is first-class adaptation evidence, never
  decorative lighting metadata;
- incident light on the image surface is distinct from general room
  illuminance;
- surround spectral quality keeps photometric / CCT / chromaticity /
  spectral-measured / manufacturer-declared evidence classes separate;
- subjective visual results bind the exact viewing environment —
  ``no visible difference`` never proves numerical equivalence;
- environmental changes (wall repaint, bias light, stray source)
  stale viewing evidence independently of the display configuration.

Literature basis (issue §research): ITU-R BT.2035 (07/2013 reference
viewing environment for HDTV evaluation, in force); ITU-R BT.500-15
(05/2023 subjective assessment viewing conditions, in force); ITU-R
BT.2166-0 (02/2025 critical HDR/SDR monitoring environment — neutral
D65 surround/periphery with explicit luminance control, in force);
ITU-R BT.2408-9 (03/2026 HDR operational guidance).  These are
broadcast/production/assessment method profiles — HTDT binds their
exact measurable semantics while residential/project targets stay
separate.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical_json import canonical_sha256 as _digest, canonicalize_payload as _canon


_SHA256 = r'^[0-9a-f]{64}$'

VIEWING_ENVIRONMENT_AUTHORITY_VERSION = 'viewing-environment-1'


# ---------------------------------------------------------------------------
# Taxonomies (issue §3, §5, §7, §13)
# ---------------------------------------------------------------------------

ViewingEnvironmentProfileKind = Literal[
    'itu_bt2035_reference_hdtv',
    'itu_bt500_subjective_lab',
    'itu_bt500_subjective_home',
    'itu_bt2166_hdr_sdr_critical_monitoring',
    'project_defined_dark_theater',
    'project_defined_day_viewing',
    'normal_use_scenario',
    'other_versioned',
    'unknown',
]

ProfileScope = Literal[
    'reference_evaluation',
    'subjective_test_method',
    'project_normal_use',
    'informational',
]

SurroundSpectralEvidence = Literal[
    'photometric_only',
    'cct_chromaticity_measured',
    'spectral_measured',
    'manufacturer_declared',
    'unknown',
]

LightingSceneKind = Literal[
    'calibration',
    'movie_dark',
    'intermission',
    'day_viewing',
    'cleaning_service',
    'custom',
    'unknown',
]

DeviceSwitch = Literal['off', 'on', 'dimmed', 'unknown']

EnvironmentRequirement = Literal[
    'surround_luminance_measured',
    'periphery_luminance_measured',
    'surround_neutral_chromaticity',
    'surround_spectral_evidence',
    'incident_illuminance_measured',
    'ambient_illuminance_bounded',
    'no_stray_light',
    'viewing_geometry_bound',
    'dark_room',
]

RequirementState = Literal['met', 'unmet', 'unknown', 'not_applicable']

EnvironmentQualificationState = Literal[
    'profile_met',
    'profile_met_with_limitations',
    'profile_not_met',
    'insufficient_evidence',
    'not_applicable',
]

ComparabilityVerdict = Literal[
    'comparable',
    'limited_comparability',
    'not_comparable',
]

EnvironmentChangeAxis = Literal[
    'ambient_illuminance',
    'incident_illuminance',
    'surround_luminance',
    'periphery_luminance',
    'surround_chromaticity',
    'wall_reflectance_or_colour',
    'bias_light_state',
    'stray_light_sources',
    'blind_curtain_state',
    'automation_scene',
]

EnvironmentReason = Literal[
    'PROFILE_MET',
    'PROFILE_MET_WITH_LIMITATIONS',
    'SURROUND_LUMINANCE_UNMEASURED',
    'PERIPHERY_LUMINANCE_UNMEASURED',
    'INCIDENT_LIGHT_UNMEASURED',
    'SURROUND_CHROMATICITY_MISMATCH',
    'SPECTRAL_EVIDENCE_INSUFFICIENT',
    'STRAY_LIGHT_PRESENT',
    'AMBIENT_ILLUMINANCE_UNMEASURED',
    'GEOMETRY_UNVERIFIED',
    'ENVIRONMENT_STALE',
    'PROFILE_SCOPE_NOTE',
    'NOT_REQUESTED_PROFILE',
    'INSUFFICIENT_EVIDENCE',
    'COMPARABILITY_PARTIAL',
]


#: Requirements a profile kind demands as *measured* before its name can
#: be claimed.  Project/normal-use profiles bind semantics only — they
#: never impose broadcast numeric targets on a residential room.
_PROFILE_REQUIREMENTS: dict[str, tuple[EnvironmentRequirement, ...]] = {
    'itu_bt2035_reference_hdtv': (
        'surround_luminance_measured',
        'surround_neutral_chromaticity',
        'ambient_illuminance_bounded',
        'no_stray_light',
        'viewing_geometry_bound',
    ),
    'itu_bt500_subjective_lab': (
        'surround_luminance_measured',
        'ambient_illuminance_bounded',
        'viewing_geometry_bound',
    ),
    'itu_bt500_subjective_home': (
        'viewing_geometry_bound',
    ),
    'itu_bt2166_hdr_sdr_critical_monitoring': (
        'surround_luminance_measured',
        'surround_neutral_chromaticity',
        'surround_spectral_evidence',
        'periphery_luminance_measured',
        'incident_illuminance_measured',
        'no_stray_light',
        'viewing_geometry_bound',
        'dark_room',
    ),
    'project_defined_dark_theater': (
        'surround_luminance_measured',
        'incident_illuminance_measured',
    ),
    'project_defined_day_viewing': (
        'incident_illuminance_measured',
    ),
    'normal_use_scenario': (),
    'other_versioned': (),
    'unknown': (),
}

#: Requirements that fail a reference profile when ``unmet`` rather than
#: merely limiting it.
_HARD_REQUIREMENTS: frozenset[EnvironmentRequirement] = frozenset({
    'surround_neutral_chromaticity',
    'no_stray_light',
    'dark_room',
})


# ---------------------------------------------------------------------------
# Japanese product labels
# ---------------------------------------------------------------------------

PROFILE_KIND_LABELS: dict[str, str] = {
    'itu_bt2035_reference_hdtv': 'ITU-R BT.2035 HDTV 参照視聴環境',
    'itu_bt500_subjective_lab': 'ITU-R BT.500 主観評価（ラボ）',
    'itu_bt500_subjective_home': 'ITU-R BT.500 主観評価（ホーム）',
    'itu_bt2166_hdr_sdr_critical_monitoring': (
        'ITU-R BT.2166 HDR/SDR クリティカルモニタリング'
    ),
    'project_defined_dark_theater': 'プロジェクト定義 ダークシアター',
    'project_defined_day_viewing': 'プロジェクト定義 デイ視聴',
    'normal_use_scenario': '通常使用シナリオ',
    'other_versioned': 'その他版管理プロファイル',
    'unknown': '不明',
}

PROFILE_SCOPE_LABELS: dict[str, str] = {
    'reference_evaluation': '参照評価',
    'subjective_test_method': '主観テスト手法',
    'project_normal_use': 'プロジェクト通常使用',
    'informational': '参考',
}

SPECTRAL_EVIDENCE_LABELS: dict[str, str] = {
    'photometric_only': '測光のみ',
    'cct_chromaticity_measured': 'CCT/色度実測',
    'spectral_measured': '分光実測',
    'manufacturer_declared': 'メーカー宣言',
    'unknown': '不明',
}

SCENE_KIND_LABELS: dict[str, str] = {
    'calibration': '校正',
    'movie_dark': '映像視聴（暗）',
    'intermission': 'インターミッション',
    'day_viewing': '日中視聴',
    'cleaning_service': '清掃/サービス',
    'custom': 'カスタム',
    'unknown': '不明',
}

REQUIREMENT_LABELS: dict[str, str] = {
    'surround_luminance_measured': '周辺輝度の測定',
    'periphery_luminance_measured': '周縁輝度の測定',
    'surround_neutral_chromaticity': '周辺の中性色度',
    'surround_spectral_evidence': '周辺の分光証拠',
    'incident_illuminance_measured': '画面入射照度の測定',
    'ambient_illuminance_bounded': '環境照度の上限',
    'no_stray_light': '迷光なし',
    'viewing_geometry_bound': '視聴幾何の束縛',
    'dark_room': '暗室',
}

REQUIREMENT_STATE_LABELS: dict[str, str] = {
    'met': '充足',
    'unmet': '未充足',
    'unknown': '不明',
    'not_applicable': '対象外',
}

QUALIFICATION_STATE_LABELS: dict[str, str] = {
    'profile_met': 'プロファイル充足',
    'profile_met_with_limitations': '制限付き充足',
    'profile_not_met': 'プロファイル未充足',
    'insufficient_evidence': '証拠不足',
    'not_applicable': '対象外',
}

COMPARABILITY_LABELS: dict[str, str] = {
    'comparable': '比較可能',
    'limited_comparability': '限定的比較可能',
    'not_comparable': '比較不可',
}

CHANGE_AXIS_LABELS: dict[str, str] = {
    'ambient_illuminance': '環境照度',
    'incident_illuminance': '画面入射照度',
    'surround_luminance': '周辺輝度',
    'periphery_luminance': '周縁輝度',
    'surround_chromaticity': '周辺色度',
    'wall_reflectance_or_colour': '壁面反射率/色',
    'bias_light_state': 'バイアス照明状態',
    'stray_light_sources': '迷光源',
    'blind_curtain_state': 'ブラインド/カーテン状態',
    'automation_scene': '自動化シーン',
}

REASON_LABELS: dict[str, str] = {
    'PROFILE_MET': 'プロファイル充足',
    'PROFILE_MET_WITH_LIMITATIONS': '制限付き充足',
    'SURROUND_LUMINANCE_UNMEASURED': '周辺輝度が未測定',
    'PERIPHERY_LUMINANCE_UNMEASURED': '周縁輝度が未測定',
    'INCIDENT_LIGHT_UNMEASURED': '画面入射照度が未測定',
    'SURROUND_CHROMATICITY_MISMATCH': '周辺色度がプロファイル不一致',
    'SPECTRAL_EVIDENCE_INSUFFICIENT': '分光証拠不足（CCT ≠ D65 分光）',
    'STRAY_LIGHT_PRESENT': '迷光あり',
    'AMBIENT_ILLUMINANCE_UNMEASURED': '環境照度が未測定',
    'GEOMETRY_UNVERIFIED': '視聴幾何が未検証',
    'ENVIRONMENT_STALE': '環境証跡が陳腐化',
    'PROFILE_SCOPE_NOTE': 'プロファイル適用範囲メモ',
    'NOT_REQUESTED_PROFILE': '要求されていないプロファイル',
    'INSUFFICIENT_EVIDENCE': '証拠不足',
    'COMPARABILITY_PARTIAL': '比較可能性が部分的',
}


# ---------------------------------------------------------------------------
# Sub-records
# ---------------------------------------------------------------------------


class RequirementVerdict(BaseModel):
    """One profile requirement's measured state."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    requirement: EnvironmentRequirement
    state: RequirementState
    reason: EnvironmentReason | None = None


class SurroundState(BaseModel):
    """The adapting surround as a first-class perceptual condition.

    ``spectral_evidence`` keeps the evidence class honest: a CCT number
    alone never claims an exact D65 spectral match.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    surround_luminance_cd_m2: float | None = Field(default=None, ge=0.0)
    periphery_luminance_cd_m2: float | None = Field(default=None, ge=0.0)
    surround_chromaticity_x: float | None = Field(
        default=None, ge=0.0, le=1.0
    )
    surround_chromaticity_y: float | None = Field(
        default=None, ge=0.0, le=1.0
    )
    surround_cct_k: float | None = Field(default=None, gt=0.0)
    spectral_evidence: SurroundSpectralEvidence = 'unknown'
    bias_light_state: DeviceSwitch = 'unknown'
    bias_light_device_ref: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def finite(self) -> 'SurroundState':
        for v in (
            self.surround_luminance_cd_m2,
            self.periphery_luminance_cd_m2,
            self.surround_chromaticity_x,
            self.surround_chromaticity_y,
            self.surround_cct_k,
        ):
            if v is not None and not isfinite(float(v)):
                raise ValueError('surround values must be finite')
        return self

    @property
    def neutral_chromaticity_declared(self) -> bool:
        """A D65-ish surround was *measured* chromatically (approximate
        D65 locus band), not just claimed."""
        if self.surround_chromaticity_x is None or (
            self.surround_chromaticity_y is None
        ):
            return False
        return (
            0.29 <= self.surround_chromaticity_x <= 0.34
            and 0.29 <= self.surround_chromaticity_y <= 0.36
        )


class IncidentLightState(BaseModel):
    """Light actually falling on the image surface — never a substitute
    for general room lux (issue §6)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    incident_illuminance_lx: float | None = Field(default=None, ge=0.0)
    measurement_direction: str | None = Field(default=None, min_length=1)
    source_scene_ref: str | None = Field(default=None, min_length=1)
    specular_reflection_observed: bool | None = None
    glare_observed: bool | None = None

    @model_validator(mode='after')
    def finite(self) -> 'IncidentLightState':
        if self.incident_illuminance_lx is not None and not isfinite(
            float(self.incident_illuminance_lx)
        ):
            raise ValueError('incident illuminance must be finite')
        return self


class EnvironmentComparability(BaseModel):
    """Non-sealed before/after comparability result (issue §14).

    If the environment changed alongside display calibration, both
    interventions are reported — the perceptual delta is never silently
    attributed to the display settings alone.
    """

    model_config = ConfigDict(frozen=True)

    verdict: ComparabilityVerdict
    changed_axes: tuple[EnvironmentChangeAxis, ...] = ()
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid(self) -> 'EnvironmentComparability':
        if len(set(self.changed_axes)) != len(self.changed_axes):
            raise ValueError('changed axes must be unique')
        if self.verdict == 'comparable' and self.changed_axes:
            raise ValueError(
                'a comparable verdict cannot list changed axes'
            )
        if self.verdict == 'not_comparable' and not self.changed_axes:
            raise ValueError(
                'a not-comparable verdict must name the changed axes'
            )
        return self


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


class ViewingEnvironmentObservation(BaseModel):
    """One captured viewing-environment state (#633 §2, §5–§7, §16)."""

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['viewing-environment-1'] = (
        VIEWING_ENVIRONMENT_AUTHORITY_VERSION
    )
    observation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    room_ref: str | None = Field(default=None, min_length=1)
    scene_revision_id: str | None = Field(default=None, min_length=1)
    label: str | None = Field(default=None, min_length=1)
    ambient_illuminance_lx: float | None = Field(default=None, ge=0.0)
    incident_light: IncidentLightState | None = None
    surround: SurroundState | None = None
    wall_material: str | None = Field(default=None, min_length=1)
    wall_reflectance: float | None = Field(
        default=None, ge=0.0, le=1.0
    )
    wall_colour: str | None = Field(default=None, min_length=1)
    ceiling_reflectance: float | None = Field(
        default=None, ge=0.0, le=1.0
    )
    floor_reflectance: float | None = Field(default=None, ge=0.0, le=1.0)
    stray_light_sources: tuple[str, ...] = ()
    blind_curtain_state: str | None = Field(default=None, min_length=1)
    door_state: str | None = Field(default=None, min_length=1)
    automation_scene_ref: str | None = Field(default=None, min_length=1)
    instrument_ref: str | None = Field(default=None, min_length=1)
    instrument_sha256: str | None = Field(default=None, pattern=_SHA256)
    captured_at_utc: str = Field(min_length=1)
    notes: str | None = Field(default=None, min_length=1)
    observation_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_observation(self) -> 'ViewingEnvironmentObservation':
        for v in (
            self.ambient_illuminance_lx,
            self.wall_reflectance,
            self.ceiling_reflectance,
            self.floor_reflectance,
        ):
            if v is not None and not isfinite(float(v)):
                raise ValueError('environment values must be finite')
        if len(set(self.stray_light_sources)) != len(
            self.stray_light_sources
        ):
            raise ValueError('stray light sources must be unique')
        if self.observation_sha256 != _digest(self.identity_payload()):
            raise ValueError('environment observation hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('observation_id', None)
        payload.pop('observation_sha256', None)
        return payload


class ViewingGeometryObservation(BaseModel):
    """Exact viewer/screen geometry (#633 §8)."""

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['viewing-environment-1'] = (
        VIEWING_ENVIRONMENT_AUTHORITY_VERSION
    )
    geometry_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    observation_id: str | None = Field(default=None, min_length=1)
    observation_sha256: str | None = Field(default=None, pattern=_SHA256)
    viewer_xyz_m: tuple[float, float, float] | None = None
    screen_plane_ref: str | None = Field(default=None, min_length=1)
    viewing_distance_m: float | None = Field(default=None, gt=0.0)
    horizontal_angle_deg: float | None = Field(
        default=None, ge=-90.0, le=90.0
    )
    vertical_angle_deg: float | None = Field(
        default=None, ge=-90.0, le=90.0
    )
    screen_angular_size_deg: float | None = Field(
        default=None, gt=0.0
    )
    eye_height_m: float | None = Field(default=None, gt=0.0)
    seat_ref: str | None = Field(default=None, min_length=1)
    measured_at_utc: str = Field(min_length=1)
    notes: str | None = Field(default=None, min_length=1)
    geometry_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_geometry(self) -> 'ViewingGeometryObservation':
        values: list[float] = []
        if self.viewer_xyz_m is not None:
            values.extend(self.viewer_xyz_m)
        for v in (
            self.viewing_distance_m,
            self.horizontal_angle_deg,
            self.vertical_angle_deg,
            self.screen_angular_size_deg,
            self.eye_height_m,
        ):
            if v is not None:
                values.append(v)
        for v in values:
            if not isfinite(float(v)):
                raise ValueError('geometry values must be finite')
        if self.geometry_sha256 != _digest(self.identity_payload()):
            raise ValueError('viewing geometry hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('geometry_id', None)
        payload.pop('geometry_sha256', None)
        return payload

    @property
    def bound(self) -> bool:
        """Enough geometry to qualify as bound viewing geometry."""
        return self.viewing_distance_m is not None or (
            self.seat_ref is not None
        )


class LightingSceneRecord(BaseModel):
    """One automation scene binding exact lighting/blind states
    (#633 §13).  Many legitimate viewing conditions coexist — no single
    reference environment is required for every user scenario."""

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['viewing-environment-1'] = (
        VIEWING_ENVIRONMENT_AUTHORITY_VERSION
    )
    scene_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: LightingSceneKind = 'unknown'
    lighting_states: tuple[str, ...] = ()
    blind_curtain_state: str | None = Field(default=None, min_length=1)
    bound_observation_id: str | None = Field(default=None, min_length=1)
    bound_observation_sha256: str | None = Field(
        default=None, pattern=_SHA256
    )
    declared_at_utc: str = Field(min_length=1)
    notes: str | None = Field(default=None, min_length=1)
    scene_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_scene(self) -> 'LightingSceneRecord':
        if len(set(self.lighting_states)) != len(self.lighting_states):
            raise ValueError('lighting states must be unique')
        if self.scene_sha256 != _digest(self.identity_payload()):
            raise ValueError('lighting scene hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('scene_id', None)
        payload.pop('scene_sha256', None)
        return payload


class ViewingEnvironmentQualification(BaseModel):
    """Sealed fail-closed verdict for one environment against one
    declared profile (#633 §10, §17)."""

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['viewing-environment-1'] = (
        VIEWING_ENVIRONMENT_AUTHORITY_VERSION
    )
    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    observation_sha256: str = Field(pattern=_SHA256)
    profile_kind: ViewingEnvironmentProfileKind
    profile_scope: ProfileScope
    profile_revision: str | None = Field(default=None, min_length=1)
    external_standard_ref: str | None = Field(default=None, min_length=1)
    selected_as_requirement: bool = False
    display_state_ref: str | None = Field(default=None, min_length=1)
    display_state_sha256: str | None = Field(default=None, pattern=_SHA256)
    geometry_ids: tuple[str, ...] = ()
    state: EnvironmentQualificationState
    requirement_verdicts: tuple[RequirementVerdict, ...] = ()
    reasons: tuple[EnvironmentReason, ...] = ()
    limitations: tuple[str, ...] = ()
    stale_after_change: tuple[EnvironmentChangeAxis, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_qualification(self) -> 'ViewingEnvironmentQualification':
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError('qualification reasons must be unique')
        if len(set(self.geometry_ids)) != len(self.geometry_ids):
            raise ValueError('geometry refs must be unique')
        if len(set(self.stale_after_change)) != len(
            self.stale_after_change
        ):
            raise ValueError('staleness axes must be unique')
        if self.state == 'profile_met' and any(
            v.state in ('unmet', 'unknown')
            for v in self.requirement_verdicts
        ):
            raise ValueError(
                'a met profile cannot carry unmet/unknown requirements'
            )
        if self.qualification_sha256 != _digest(
            self.identity_payload()
        ):
            raise ValueError('qualification hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('qualification_id', None)
        payload.pop('qualification_sha256', None)
        return payload


# ---------------------------------------------------------------------------
# Builders (content-sealed construction)
# ---------------------------------------------------------------------------


def _seal(model: type[BaseModel], payload: dict[str, Any], id_field: str, sha_field: str, prefix: str) -> BaseModel:
    probe = model.model_construct(
        **_canon(model, dict(**payload, **{id_field: '', sha_field: ''}))
    )
    sha = _digest(probe.identity_payload())  # type: ignore[attr-defined]
    return model(
        **probe.model_dump(exclude={id_field, sha_field}),
        **{id_field: f'{prefix}{sha}', sha_field: sha},
    )


def build_environment_observation(
    *,
    document_id: str,
    captured_at_utc: str,
    **fields: Any,
) -> ViewingEnvironmentObservation:
    return _seal(
        ViewingEnvironmentObservation,
        dict(
            document_id=document_id,
            captured_at_utc=captured_at_utc,
            **fields,
        ),
        'observation_id',
        'observation_sha256',
        'veo:',
    )  # type: ignore[return-value]


def build_viewing_geometry(
    *,
    document_id: str,
    measured_at_utc: str,
    observation: ViewingEnvironmentObservation | None = None,
    **fields: Any,
) -> ViewingGeometryObservation:
    return _seal(
        ViewingGeometryObservation,
        dict(
            document_id=document_id,
            observation_id=(
                None if observation is None else observation.observation_id
            ),
            observation_sha256=(
                None
                if observation is None
                else observation.observation_sha256
            ),
            measured_at_utc=measured_at_utc,
            **fields,
        ),
        'geometry_id',
        'geometry_sha256',
        'vgo:',
    )  # type: ignore[return-value]


def build_lighting_scene(
    *,
    document_id: str,
    name: str,
    declared_at_utc: str,
    kind: LightingSceneKind = 'unknown',
    bound_observation: ViewingEnvironmentObservation | None = None,
    **fields: Any,
) -> LightingSceneRecord:
    return _seal(
        LightingSceneRecord,
        dict(
            document_id=document_id,
            name=name,
            kind=kind,
            bound_observation_id=(
                None
                if bound_observation is None
                else bound_observation.observation_id
            ),
            bound_observation_sha256=(
                None
                if bound_observation is None
                else bound_observation.observation_sha256
            ),
            declared_at_utc=declared_at_utc,
            **fields,
        ),
        'scene_id',
        'scene_sha256',
        'lsr:',
    )  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _requirement_state(
    requirement: EnvironmentRequirement,
    observation: ViewingEnvironmentObservation,
    geometry: Sequence[ViewingGeometryObservation],
) -> tuple[RequirementState, EnvironmentReason | None]:
    surround = observation.surround
    incident = observation.incident_light
    if requirement == 'surround_luminance_measured':
        if surround is None or surround.surround_luminance_cd_m2 is None:
            return 'unknown', 'SURROUND_LUMINANCE_UNMEASURED'
        return 'met', None
    if requirement == 'periphery_luminance_measured':
        if surround is None or surround.periphery_luminance_cd_m2 is None:
            return 'unknown', 'PERIPHERY_LUMINANCE_UNMEASURED'
        return 'met', None
    if requirement == 'surround_neutral_chromaticity':
        if surround is None:
            return 'unknown', 'SURROUND_CHROMATICITY_MISMATCH'
        if surround.neutral_chromaticity_declared:
            return 'met', None
        return 'unmet', 'SURROUND_CHROMATICITY_MISMATCH'
    if requirement == 'surround_spectral_evidence':
        # CCT/chromaticity alone never proves a D65 spectral surround.
        if surround is None or surround.spectral_evidence in (
            'unknown', 'photometric_only', 'manufacturer_declared'
        ):
            return 'unknown', 'SPECTRAL_EVIDENCE_INSUFFICIENT'
        if surround.spectral_evidence == 'cct_chromaticity_measured':
            return 'unknown', 'SPECTRAL_EVIDENCE_INSUFFICIENT'
        return 'met', None
    if requirement == 'incident_illuminance_measured':
        if incident is None or incident.incident_illuminance_lx is None:
            return 'unknown', 'INCIDENT_LIGHT_UNMEASURED'
        return 'met', None
    if requirement == 'ambient_illuminance_bounded':
        if observation.ambient_illuminance_lx is None:
            return 'unknown', 'AMBIENT_ILLUMINANCE_UNMEASURED'
        return 'met', None
    if requirement == 'no_stray_light':
        if observation.stray_light_sources:
            return 'unmet', 'STRAY_LIGHT_PRESENT'
        return 'met', None
    if requirement == 'viewing_geometry_bound':
        if not geometry or not any(g.bound for g in geometry):
            return 'unknown', 'GEOMETRY_UNVERIFIED'
        return 'met', None
    if requirement == 'dark_room':
        # Dark-room profiles require measured near-dark ambient and no
        # incident light on the image surface.
        ambient = observation.ambient_illuminance_lx
        inc = (
            None if incident is None else incident.incident_illuminance_lx
        )
        if ambient is None or inc is None:
            return 'unknown', 'AMBIENT_ILLUMINANCE_UNMEASURED'
        if ambient > 5.0 or inc > 1.0:
            return 'unmet', 'STRAY_LIGHT_PRESENT'
        return 'met', None
    return 'unknown', 'INSUFFICIENT_EVIDENCE'


def evaluate_viewing_environment(
    *,
    document_id: str,
    observation: ViewingEnvironmentObservation,
    profile_kind: ViewingEnvironmentProfileKind,
    profile_scope: ProfileScope,
    selected_as_requirement: bool = False,
    profile_revision: str | None = None,
    external_standard_ref: str | None = None,
    geometry: Sequence[ViewingGeometryObservation] = (),
    display_state_ref: str | None = None,
    display_state_sha256: str | None = None,
    prior_change_axes: Sequence[EnvironmentChangeAxis] = (),
    evaluated_at_utc: str,
) -> ViewingEnvironmentQualification:
    """Fail-closed viewing-environment qualification (#633 §4, §10).

    - ``profile_met`` requires every profile requirement measured ``met``;
      ``unknown``/``unmet`` hard requirements fail or demote honestly;
    - production profiles are *method* profiles: when the profile was not
      selected as a project requirement, ``profile_not_met`` still reports
      PROFILE_SCOPE_NOTE — a residential room is never "defective" for
      missing a broadcast reference target;
    - ``prior_change_axes`` records environmental changes that stale
      previous evidence independently of the display configuration.
    """
    requirements = _PROFILE_REQUIREMENTS.get(profile_kind, ())
    verdicts: list[RequirementVerdict] = []
    reasons: list[EnvironmentReason] = []

    for req in requirements:
        req_state, req_reason = _requirement_state(
            req, observation, geometry
        )
        verdicts.append(
            RequirementVerdict(
                requirement=req, state=req_state, reason=req_reason
            )
        )
        if req_reason is not None:
            reasons.append(req_reason)

    if not selected_as_requirement:
        reasons.append('NOT_REQUESTED_PROFILE')
    if profile_scope in ('project_normal_use', 'informational'):
        reasons.append('PROFILE_SCOPE_NOTE')

    stale_axes = tuple(dict.fromkeys(prior_change_axes))
    if stale_axes:
        reasons.append('ENVIRONMENT_STALE')

    unmet_hard = [
        v for v in verdicts
        if v.requirement in _HARD_REQUIREMENTS and v.state == 'unmet'
    ]
    unmet_any = [v for v in verdicts if v.state == 'unmet']
    unknown_any = [v for v in verdicts if v.state == 'unknown']

    if profile_kind == 'unknown':
        state_out: EnvironmentQualificationState = 'insufficient_evidence'
        reasons.append('INSUFFICIENT_EVIDENCE')
    elif not requirements and profile_kind in (
        'normal_use_scenario', 'other_versioned'
    ):
        # Capture-only profiles: nothing to fail against.
        state_out = 'profile_met_with_limitations'
        reasons.append('PROFILE_MET_WITH_LIMITATIONS')
    elif unmet_hard:
        state_out = 'profile_not_met'
    elif unmet_any or unknown_any:
        if unknown_any and not unmet_any:
            state_out = 'insufficient_evidence'
            reasons.append('INSUFFICIENT_EVIDENCE')
        else:
            state_out = 'profile_not_met' if selected_as_requirement else (
                'profile_met_with_limitations'
            )
            if not selected_as_requirement:
                reasons.append('PROFILE_MET_WITH_LIMITATIONS')
    else:
        state_out = 'profile_met'
        reasons.append('PROFILE_MET')

    return _seal(
        ViewingEnvironmentQualification,
        dict(
            document_id=document_id,
            observation_id=observation.observation_id,
            observation_sha256=observation.observation_sha256,
            profile_kind=profile_kind,
            profile_scope=profile_scope,
            profile_revision=profile_revision,
            external_standard_ref=external_standard_ref,
            selected_as_requirement=selected_as_requirement,
            display_state_ref=display_state_ref,
            display_state_sha256=display_state_sha256,
            geometry_ids=tuple(g.geometry_id for g in geometry),
            state=state_out,
            requirement_verdicts=tuple(verdicts),
            reasons=tuple(dict.fromkeys(reasons)),
            stale_after_change=stale_axes,
            evaluated_at_utc=evaluated_at_utc,
        ),
        'qualification_id',
        'qualification_sha256',
        'veq:',
    )  # type: ignore[return-value]


def compare_viewing_environments(
    before: ViewingEnvironmentObservation,
    after: ViewingEnvironmentObservation,
) -> EnvironmentComparability:
    """Before/after comparability check (#633 §14).

    Any axis that differs between the two captures is reported.  Visual
    before/after claims remain valid only when display state, stimulus,
    surround/ambient and geometry all stay compatible.
    """
    changed: list[EnvironmentChangeAxis] = []

    if before.ambient_illuminance_lx != after.ambient_illuminance_lx:
        changed.append('ambient_illuminance')
    b_inc = None if before.incident_light is None else (
        before.incident_light.incident_illuminance_lx
    )
    a_inc = None if after.incident_light is None else (
        after.incident_light.incident_illuminance_lx
    )
    if b_inc != a_inc:
        changed.append('incident_illuminance')

    b_sur = before.surround
    a_sur = after.surround
    if (b_sur is None) != (a_sur is None):
        changed.extend(
            ('surround_luminance', 'periphery_luminance',
             'surround_chromaticity', 'bias_light_state')
        )
    elif b_sur is not None and a_sur is not None:
        if (
            b_sur.surround_luminance_cd_m2
            != a_sur.surround_luminance_cd_m2
        ):
            changed.append('surround_luminance')
        if (
            b_sur.periphery_luminance_cd_m2
            != a_sur.periphery_luminance_cd_m2
        ):
            changed.append('periphery_luminance')
        if (
            b_sur.surround_chromaticity_x != a_sur.surround_chromaticity_x
            or b_sur.surround_chromaticity_y
            != a_sur.surround_chromaticity_y
            or b_sur.surround_cct_k != a_sur.surround_cct_k
            or b_sur.spectral_evidence != a_sur.spectral_evidence
        ):
            changed.append('surround_chromaticity')
        if b_sur.bias_light_state != a_sur.bias_light_state:
            changed.append('bias_light_state')

    if (
        before.wall_material != after.wall_material
        or before.wall_reflectance != after.wall_reflectance
        or before.wall_colour != after.wall_colour
    ):
        changed.append('wall_reflectance_or_colour')
    if set(before.stray_light_sources) != set(after.stray_light_sources):
        changed.append('stray_light_sources')
    if before.blind_curtain_state != after.blind_curtain_state:
        changed.append('blind_curtain_state')
    if before.automation_scene_ref != after.automation_scene_ref:
        changed.append('automation_scene')

    if not changed:
        verdict: ComparabilityVerdict = 'comparable'
    elif any(
        axis in changed
        for axis in (
            'surround_luminance', 'surround_chromaticity',
            'stray_light_sources', 'wall_reflectance_or_colour',
        )
    ):
        verdict = 'not_comparable'
    else:
        verdict = 'limited_comparability'

    return EnvironmentComparability(
        verdict=verdict,
        changed_axes=tuple(dict.fromkeys(changed)),
        notes=(
            (
                'environment axes changed — attribute perceptual deltas '
                'to environment and display interventions together'
            ),
        ) if changed else (),
    )


__all__ = [
    'VIEWING_ENVIRONMENT_AUTHORITY_VERSION',
    'CHANGE_AXIS_LABELS',
    'COMPARABILITY_LABELS',
    'EnvironmentComparability',
    'IncidentLightState',
    'LightingSceneRecord',
    'PROFILE_KIND_LABELS',
    'PROFILE_SCOPE_LABELS',
    'REASON_LABELS',
    'REQUIREMENT_LABELS',
    'REQUIREMENT_STATE_LABELS',
    'QUALIFICATION_STATE_LABELS',
    'RequirementVerdict',
    'SCENE_KIND_LABELS',
    'SPECTRAL_EVIDENCE_LABELS',
    'SurroundState',
    'ViewingEnvironmentObservation',
    'ViewingEnvironmentQualification',
    'ViewingGeometryObservation',
    'build_environment_observation',
    'build_lighting_scene',
    'build_viewing_geometry',
    'compare_viewing_environments',
    'evaluate_viewing_environment',
]
