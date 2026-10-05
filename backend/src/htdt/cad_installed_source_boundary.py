"""Installed loudspeaker boundary-condition authority (#614).

Free-field loudspeaker data is *measurement-conditioned* evidence: the
boundary it was captured against (anechoic full-space, ground-plane
half-space, a standard IEC baffle, the product's intended in-wall mount)
is part of what the dataset can claim. Applying it unchanged to a
different installation — flush-mounted into a wall, against a rear
boundary, in a screen cavity — silently fabricates physics.

This module is the fail-closed authority over that claim:

- :class:`SourceMeasurementCondition` — the measurement-condition
  identity bound to a speaker dataset: acoustic environment class,
  standard/method profile (ANSI/CTA-2034-B, IEC 60268-21, IEC 60268-5,
  manufacturer, other versioned, unversioned, unknown), the baffle /
  mounting state during measurement, distance and reference-axis
  geometry, frequency validity, evidence class, and — critically —
  ``includes_installed_boundary``: a dataset measured *in* its installed
  boundary already carries that loading and must never receive a second
  boundary-gain correction.
- :class:`InstalledMountingCondition` — the declared actual installation:
  mounting kind (free-standing / stand / on-wall / near-wall / in-wall /
  flush / baffle wall / ceiling-recessed / soffit / corner
  multi-boundary / custom cavity / unknown), the mounted-geometry block
  (front-baffle plane, wall plane, recess depth, clearance gap, baffle
  extent, edge distances, rear cavity, screen distance, absorber
  packing), and bindings to as-built geometry evidence (#613 elements)
  plus any DSP boundary-compensation preset (#592 device state).
- :class:`SourceBoundaryCorrection` — a *declared* model correction:
  identity + version, the mounting kinds it covers, frequency validity,
  parameter bounds, and whether it models a source-boundary loading
  effect or a path effect. A "+6 dB half-space" factor is never an exact
  claim — it is an approximation with a declared validity interval
  (Allison, AES preprint 951 / JAES 22(5), 1974; Waterhouse boundary
  output formulas).
- :class:`InstalledSourceMeasurement` — installed-condition acoustic
  verification: per-position transfer observations with disjoint
  fit/holdout position sets (calibration-style honesty — a model tuned
  on a position is never validated by it).
- :class:`InstalledSourceQualification` — the sealed verdict: achieved
  capability level, directivity applicability, low-frequency boundary
  state, applied corrections, conflicts and limitations.

Contract properties:

- capability levels are a ladder, not interchangeable words: a
  ``reference_source_only`` claim can never satisfy a mounting that
  requires ``explicit_installed_source_model``;
- the source-boundary loading effect stays separate from SBIR path
  interference — SBIR paths are owned by the #129 acoustics modules,
  this authority never models a reflection as a source correction;
- finite baffles are never equivalent to an infinite half-space — a
  declared finite baffle edge caps half-space claims at "limited";
- double-application is structurally prevented: a dataset that already
  embeds its installed boundary plus a requested boundary correction is
  a conflict verdict, not a stacked gain;
- a DSP boundary-compensation preset plus a modelled boundary
  correction is a double-compensation conflict — both are recorded, the
  prediction never silently applies both;
- verification measurements without held-out positions can state
  ``fit_only`` — never ``validated``.

Literature basis (issue §research): IEC 60268-21:2018 (acoustical
output-based measurements, incl. boundary-condition awareness of the
target application), ANSI/CTA-2034-B (Jul 2024 spinorama profile),
IEC 60268-5 standard baffle / measuring-enclosure provisions, the AES
3571 large-baffle practice line (Salmensaari, Helsinki Univ. Tech. 1992
large-baffle flush-mounting thesis), Allison's boundary power
augmentation analysis and Waterhouse's boundary-output formulas
(approximately +6 dB only while the boundary is a small fraction of a
wavelength away), and manufacturer flush-mount guidance (e.g. Genelec)
as product-bound, versioned evidence — never generic claims.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import FrequencyDomain
from .canonical_json import canonical_sha256 as _digest, canonicalize_payload as _canon


_SHA256 = r'^[0-9a-f]{64}$'

INSTALLED_SOURCE_AUTHORITY_VERSION = 'installed-source-boundary-1'


# ---------------------------------------------------------------------------
# Taxonomies (issue §1–§4, §7, §10)
# ---------------------------------------------------------------------------

SourceMeasurementEnvironment = Literal[
    'free_field_anechoic',
    'quasi_anechoic',
    'half_space_ground_plane',
    'ground_plane',
    'in_wall_baffle',
    'custom_boundary',
    'reverberant',
    'in_situ',
    'unknown',
]

SourceMeasurementProfile = Literal[
    'cta_2034_b',
    'iec_60268_21',
    'iec_60268_5',
    'manufacturer_method',
    'other_versioned',
    'unversioned_method',
    'unknown',
]

SourceMeasurementBaffle = Literal[
    'unbaffled_free_air',
    'standard_baffle',
    'large_baffle',
    'in_room_boundary',
    'installed_as_sold',
    'unknown',
]

SourceConditionEvidenceClass = Literal[
    'manufacturer_declared',
    'third_party_lab',
    'independent_measurement',
    'htdt_field_measurement',
    'database_reference',
    'user_assumed',
    'unknown',
]

MountingKind = Literal[
    'free_standing',
    'stand_mounted',
    'on_wall',
    'near_wall',
    'in_wall',
    'flush_mounted',
    'baffle_wall',
    'ceiling_recessed',
    'soffit_mounted',
    'corner_multi_boundary',
    'custom_cavity',
    'unknown',
]

RearCavityState = Literal[
    'none_declared',
    'sealed_volume',
    'vented_to_room',
    'backbox_installed',
    'open_stud_bay',
    'unknown',
]

FacingAbsorberState = Literal[
    'none',
    'declared_absorber',
    'unknown',
]

BoundaryCorrectionKind = Literal[
    'half_space_boundary_gain',
    'quarter_space_boundary_gain',
    'eighth_space_boundary_gain',
    'finite_baffle_diffraction',
    'cavity_loading',
    'manufacturer_install_preset',
    'screen_transfer',
    'custom_declared',
]

CorrectionDomain = Literal[
    'low_frequency_only',
    'band_limited',
    'broadband_declared',
    'unknown',
]

InstalledSourceCapability = Literal[
    'reference_source_only',
    'reference_plus_geometric_sbir',
    'half_space_approximation',
    'boundary_corrected_empirical',
    'explicit_installed_source_model',
    'coupled_wave_model',
    'installed_measured_transfer',
    'unsupported',
]

DirectivityApplicability = Literal[
    'directly_applicable',
    'applicable_above_band',
    'applicable_below_band',
    'corrected_with_model',
    'installed_data_available',
    'limited',
    'incompatible',
    'unknown',
]

BoundaryVerificationState = Literal[
    'unverified',
    'fit_only',
    'validated_on_holdout',
    'holdout_disagreement',
    'not_applicable',
]

InstalledSourceQualificationState = Literal[
    'qualified',
    'qualified_with_limitations',
    'unqualified_mounting_effect',
    'conflicting_corrections',
    'insufficient_evidence',
    'unsupported_configuration',
]

InstalledSourceReason = Literal[
    'MEASUREMENT_CONDITION_UNKNOWN',
    'MEASUREMENT_INCOMPATIBLE_WITH_MOUNTING',
    'MOUNTING_UNCHARACTERIZED',
    'REAR_CAVITY_UNCHARACTERIZED',
    'FINITE_BAFFLE_NOT_HALF_SPACE',
    'BOUNDARY_GAIN_DOUBLE_APPLY',
    'DSP_DOUBLE_COMPENSATION_RISK',
    'CORRECTION_OUTSIDE_DECLARED_DOMAIN',
    'CORRECTION_PARAMETERS_MISSING',
    'SBIR_SEPARATION_REQUIRED',
    'SCREEN_TRANSFER_EXTERNAL_AUTHORITY',
    'AS_BUILT_MOUNTING_UNVERIFIED',
    'HOLDOUT_VALIDATED',
    'FIT_ONLY_NO_HOLDOUT',
    'HOLDOUT_DISAGREEMENT',
    'INSTALLED_MEASUREMENT_BOUND',
    'QUALIFIED_AS_DECLARED',
    'LIMITED_AS_DECLARED',
    'INSUFFICIENT_EVIDENCE',
]


# ---------------------------------------------------------------------------
# Japanese product labels
# ---------------------------------------------------------------------------

MEASUREMENT_ENVIRONMENT_LABELS: dict[str, str] = {
    'free_field_anechoic': '無響室自由音場',
    'quasi_anechoic': '準無響',
    'half_space_ground_plane': '半空間（接地）',
    'ground_plane': '接地平面',
    'in_wall_baffle': '壁埋め込みバッフル',
    'custom_boundary': 'カスタム境界',
    'reverberant': '残響場',
    'in_situ': '現場実測',
    'unknown': '不明',
}

MEASUREMENT_PROFILE_LABELS: dict[str, str] = {
    'cta_2034_b': 'ANSI/CTA-2034-B',
    'iec_60268_21': 'IEC 60268-21',
    'iec_60268_5': 'IEC 60268-5',
    'manufacturer_method': 'メーカー手法',
    'other_versioned': 'その他版管理手法',
    'unversioned_method': '版管理なし手法',
    'unknown': '不明',
}

MOUNTING_KIND_LABELS: dict[str, str] = {
    'free_standing': '自立設置',
    'stand_mounted': 'スタンド設置',
    'on_wall': '壁面設置（密着）',
    'near_wall': '壁際設置（離れ）',
    'in_wall': '壁埋め込み',
    'flush_mounted': 'フラッシュマウント',
    'baffle_wall': 'バッフル壁',
    'ceiling_recessed': '天井埋め込み',
    'soffit_mounted': 'ソフィットマウント',
    'corner_multi_boundary': 'コーナー/多境界',
    'custom_cavity': 'カスタムキャビティ',
    'unknown': '不明',
}

CAPABILITY_LABELS: dict[str, str] = {
    'reference_source_only': '基準ソースのみ',
    'reference_plus_geometric_sbir': '基準ソース＋幾何SBIR',
    'half_space_approximation': '半空間近似',
    'boundary_corrected_empirical': '境界補正（経験則）',
    'explicit_installed_source_model': '明示的設置ソースモデル',
    'coupled_wave_model': '結合波動モデル',
    'installed_measured_transfer': '設置実測伝達',
    'unsupported': '未対応',
}

DIRECTIVITY_APPLICABILITY_LABELS: dict[str, str] = {
    'directly_applicable': '直接適用可',
    'applicable_above_band': '帯域上側のみ適用可',
    'applicable_below_band': '帯域下側のみ適用可',
    'corrected_with_model': 'モデル補正適用',
    'installed_data_available': '設置状態データあり',
    'limited': '限定的',
    'incompatible': '非互換',
    'unknown': '不明',
}

VERIFICATION_STATE_LABELS: dict[str, str] = {
    'unverified': '未検証',
    'fit_only': 'フィットのみ（ホールドアウトなし）',
    'validated_on_holdout': 'ホールドアウト検証済み',
    'holdout_disagreement': 'ホールドアウト不一致',
    'not_applicable': '対象外',
}

QUALIFICATION_STATE_LABELS: dict[str, str] = {
    'qualified': '適格',
    'qualified_with_limitations': '制限付き適格',
    'unqualified_mounting_effect': '設置影響が未検証',
    'conflicting_corrections': '補正が競合',
    'insufficient_evidence': '証拠不足',
    'unsupported_configuration': '未対応構成',
}

REASON_LABELS: dict[str, str] = {
    'MEASUREMENT_CONDITION_UNKNOWN': '測定条件が不明',
    'MEASUREMENT_INCOMPATIBLE_WITH_MOUNTING': '測定条件と設置形態が不整合',
    'MOUNTING_UNCHARACTERIZED': '設置形態が未特定',
    'REAR_CAVITY_UNCHARACTERIZED': '背面キャビティが未特定',
    'FINITE_BAFFLE_NOT_HALF_SPACE': '有限バッフルを半空間とみなせない',
    'BOUNDARY_GAIN_DOUBLE_APPLY': '境界ゲインの二重適用',
    'DSP_DOUBLE_COMPENSATION_RISK': 'DSP境界補正との二重補正リスク',
    'CORRECTION_OUTSIDE_DECLARED_DOMAIN': '補正が宣言適用域外',
    'CORRECTION_PARAMETERS_MISSING': '補正パラメータ不足',
    'SBIR_SEPARATION_REQUIRED': 'SBIR経路とソース境界効果の分離が必要',
    'SCREEN_TRANSFER_EXTERNAL_AUTHORITY': 'スクリーン透過は外部権威の管轄',
    'AS_BUILT_MOUNTING_UNVERIFIED': '設置実態が未検証',
    'HOLDOUT_VALIDATED': 'ホールドアウトで検証済み',
    'FIT_ONLY_NO_HOLDOUT': 'ホールドアウトなしのフィットのみ',
    'HOLDOUT_DISAGREEMENT': 'ホールドアウト位置で不一致',
    'INSTALLED_MEASUREMENT_BOUND': '設置状態実測がバインド済み',
    'QUALIFIED_AS_DECLARED': '宣言どおり適格',
    'LIMITED_AS_DECLARED': '宣言どおり限定的',
    'INSUFFICIENT_EVIDENCE': '証拠不足',
}

CORRECTION_KIND_LABELS: dict[str, str] = {
    'half_space_boundary_gain': '半空間境界ゲイン',
    'quarter_space_boundary_gain': '1/4空間境界ゲイン',
    'eighth_space_boundary_gain': '1/8空間境界ゲイン',
    'finite_baffle_diffraction': '有限バッフル回折',
    'cavity_loading': 'キャビティ負荷',
    'manufacturer_install_preset': 'メーカー設置プリセット',
    'screen_transfer': 'スクリーン透過',
    'custom_declared': 'カスタム宣言補正',
}


# ---------------------------------------------------------------------------
# Capability requirements per mounting kind.
# ---------------------------------------------------------------------------

#: Minimum capability a mounting kind requires before its boundary effect
#: can be claimed. ``unknown`` mounting never qualifies.
_MOUNTING_REQUIRED_CAPABILITY: dict[str, str] = {
    'free_standing': 'reference_source_only',
    'stand_mounted': 'reference_source_only',
    'near_wall': 'reference_plus_geometric_sbir',
    'on_wall': 'half_space_approximation',
    'in_wall': 'explicit_installed_source_model',
    'flush_mounted': 'explicit_installed_source_model',
    'baffle_wall': 'explicit_installed_source_model',
    'ceiling_recessed': 'explicit_installed_source_model',
    'soffit_mounted': 'explicit_installed_source_model',
    'corner_multi_boundary': 'coupled_wave_model',
    'custom_cavity': 'explicit_installed_source_model',
    'unknown': 'unsupported',
}

_CAPABILITY_RANK: dict[str, int] = {
    'unsupported': 0,
    'reference_source_only': 1,
    'reference_plus_geometric_sbir': 2,
    'half_space_approximation': 3,
    'boundary_corrected_empirical': 4,
    'explicit_installed_source_model': 5,
    'coupled_wave_model': 6,
    'installed_measured_transfer': 7,
}

_CORRECTION_CAPABILITY: dict[str, str] = {
    'half_space_boundary_gain': 'half_space_approximation',
    'quarter_space_boundary_gain': 'half_space_approximation',
    'eighth_space_boundary_gain': 'half_space_approximation',
    'finite_baffle_diffraction': 'boundary_corrected_empirical',
    'cavity_loading': 'explicit_installed_source_model',
    'manufacturer_install_preset': 'boundary_corrected_empirical',
    'screen_transfer': 'boundary_corrected_empirical',
    'custom_declared': 'unsupported',
}


# ---------------------------------------------------------------------------
# Sub-records
# ---------------------------------------------------------------------------


class MountedGeometry(BaseModel):
    """Geometry of the installed mount — every field honest-optional."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    wall_plane_ref: str | None = Field(default=None, min_length=1)
    baffle_extent_mm: tuple[float, float] | None = None
    recess_depth_mm: float | None = Field(default=None, ge=0.0)
    clearance_gap_mm: float | None = Field(default=None, ge=0.0)
    boundary_distances_mm: tuple[float, ...] = ()
    cavity_dimensions_mm: tuple[float, float, float] | None = None
    screen_distance_mm: float | None = Field(default=None, ge=0.0)
    absorber_packing: FacingAbsorberState = 'unknown'
    as_built_element_refs: tuple[str, ...] = ()

    @model_validator(mode='after')
    def finite(self) -> 'MountedGeometry':
        values: list[float] = []
        if self.baffle_extent_mm is not None:
            values.extend(self.baffle_extent_mm)
        if self.recess_depth_mm is not None:
            values.append(self.recess_depth_mm)
        if self.clearance_gap_mm is not None:
            values.append(self.clearance_gap_mm)
        values.extend(self.boundary_distances_mm)
        if self.cavity_dimensions_mm is not None:
            values.extend(self.cavity_dimensions_mm)
        if self.screen_distance_mm is not None:
            values.append(self.screen_distance_mm)
        for value in values:
            if not isfinite(float(value)):
                raise ValueError('mounted geometry values must be finite')
        if self.baffle_extent_mm is not None and any(
            v <= 0.0 for v in self.baffle_extent_mm
        ):
            raise ValueError('baffle extent must be positive')
        if self.cavity_dimensions_mm is not None and any(
            v <= 0.0 for v in self.cavity_dimensions_mm
        ):
            raise ValueError('cavity dimensions must be positive')
        if len(set(self.as_built_element_refs)) != len(
            self.as_built_element_refs
        ):
            raise ValueError('as-built element refs must be unique')
        return self

    @property
    def finite_baffle_declared(self) -> bool:
        """An explicitly bounded baffle was declared — never infinite."""
        return self.baffle_extent_mm is not None


class MeasurementGeometry(BaseModel):
    """Distance/axis conditions the dataset was captured under."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    measurement_distance_m: float | None = Field(default=None, gt=0.0)
    reference_axis: str | None = Field(default=None, min_length=1)
    orientation: str | None = Field(default=None, min_length=1)
    floor_boundary: Literal['free_air', 'ground_plane', 'unknown'] = 'unknown'

    @model_validator(mode='after')
    def finite(self) -> 'MeasurementGeometry':
        if self.measurement_distance_m is not None and not isfinite(
            float(self.measurement_distance_m)
        ):
            raise ValueError('measurement distance must be finite')
        return self


class InstalledMeasurementObservation(BaseModel):
    """One position's installed-response observation or check."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    position_id: str = Field(min_length=1)
    observable: Literal[
        'seat_transfer',
        'off_axis_transfer',
        'sbir_notch',
        'early_reflection_pattern',
        'output_headroom',
        'band_levels',
    ]
    band_hz: float | None = Field(default=None, gt=0.0)
    measured_db: float | None = None
    predicted_db: float | None = None
    tolerance_db: float | None = Field(default=None, gt=0.0)
    notes: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def finite(self) -> 'InstalledMeasurementObservation':
        for value in (
            self.band_hz,
            self.measured_db,
            self.predicted_db,
            self.tolerance_db,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('observation values must be finite')
        return self

    @property
    def residual_db(self) -> float | None:
        if self.measured_db is None or self.predicted_db is None:
            return None
        return abs(self.measured_db - self.predicted_db)

    @property
    def passed(self) -> bool | None:
        residual = self.residual_db
        if residual is None or self.tolerance_db is None:
            return None
        return residual <= self.tolerance_db


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


class SourceMeasurementCondition(BaseModel):
    """Measurement-condition identity of a source dataset (#614 §1).

    Bound to the dataset it describes by id + content sha so a dataset
    update invalidates the condition claim. ``includes_installed_boundary``
    declares the dataset already embeds its installation boundary — such
    data can never receive a further boundary-gain correction.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['installed-source-boundary-1'] = (
        INSTALLED_SOURCE_AUTHORITY_VERSION
    )
    condition_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    source_dataset_id: str = Field(min_length=1)
    source_dataset_sha256: str | None = Field(
        default=None, pattern=_SHA256
    )
    environment: SourceMeasurementEnvironment = 'unknown'
    standard_profile: SourceMeasurementProfile = 'unknown'
    profile_revision: str | None = Field(default=None, min_length=1)
    baffle_condition: SourceMeasurementBaffle = 'unknown'
    geometry: MeasurementGeometry | None = None
    frequency_validity: FrequencyDomain | None = None
    evidence_class: SourceConditionEvidenceClass = 'unknown'
    includes_installed_boundary: bool = False
    mounting_condition_at_capture: MountingKind = 'unknown'
    source_refs: tuple[str, ...] = ()
    notes: str | None = Field(default=None, min_length=1)
    condition_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_condition(self) -> 'SourceMeasurementCondition':
        if self.standard_profile in ('unknown', 'unversioned_method'):
            if self.profile_revision is not None:
                raise ValueError(
                    'unversioned/unknown profiles cannot claim a revision'
                )
        elif self.profile_revision is None:
            raise ValueError(
                'a standard-profile claim requires its revision/edition'
            )
        if len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError('source refs must be unique')
        if self.condition_sha256 != _digest(self.identity_payload()):
            raise ValueError('measurement condition hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('condition_id', None)
        payload.pop('condition_sha256', None)
        return payload


class InstalledMountingCondition(BaseModel):
    """Declared actual installation state of one loudspeaker (#614 §2)."""

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['installed-source-boundary-1'] = (
        INSTALLED_SOURCE_AUTHORITY_VERSION
    )
    mounting_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    kind: MountingKind = 'unknown'
    geometry: MountedGeometry | None = None
    rear_cavity: RearCavityState = 'unknown'
    dsp_boundary_preset_ref: str | None = Field(default=None, min_length=1)
    declared_by: Literal[
        'design_intent', 'as_built_verified', 'user_declared', 'unknown'
    ] = 'unknown'
    notes: str | None = Field(default=None, min_length=1)
    mounting_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_mounting(self) -> 'InstalledMountingCondition':
        if self.mounting_sha256 != _digest(self.identity_payload()):
            raise ValueError('mounting condition hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('mounting_id', None)
        payload.pop('mounting_sha256', None)
        return payload


class SourceBoundaryCorrection(BaseModel):
    """A declared boundary correction model (#614 §4).

    The correction is identity + applicability, never "the physics": a
    half-space gain is an approximation with a declared validity band and
    mounting kinds, and ``corrects`` separates source-boundary loading
    from path effects (SBIR lives in the acoustic path authority).
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['installed-source-boundary-1'] = (
        INSTALLED_SOURCE_AUTHORITY_VERSION
    )
    correction_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    kind: BoundaryCorrectionKind
    model_identity: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    applicable_mountings: tuple[MountingKind, ...] = ()
    domain: CorrectionDomain = 'unknown'
    frequency_validity: FrequencyDomain | None = None
    parameters: tuple[tuple[str, float], ...] = ()
    corrects: Literal['source_boundary_effect', 'path_effect'] = (
        'source_boundary_effect'
    )
    assumptions: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    correction_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_correction(self) -> 'SourceBoundaryCorrection':
        if self.corrects == 'path_effect':
            # SBIR path interference is owned by the acoustics authority —
            # a boundary correction may not claim it.
            raise ValueError(
                'boundary corrections model source-boundary effects only; '
                'path effects belong to the SBIR/path authority'
            )
        if len(set(self.applicable_mountings)) != len(
            self.applicable_mountings
        ):
            raise ValueError('applicable mountings must be unique')
        param_names = [name for name, _ in self.parameters]
        if len(param_names) != len(set(param_names)):
            raise ValueError('correction parameter names must be unique')
        for _, value in self.parameters:
            if not isfinite(float(value)):
                raise ValueError('correction parameters must be finite')
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError('evidence refs must be unique')
        if self.kind == 'manufacturer_install_preset':
            # Product-bound guidance must name its product/version claim.
            if not any(
                name in param_names
                for name in ('product', 'preset_name')
            ) and not any(
                'product' in a or 'preset' in a for a in self.assumptions
            ):
                raise ValueError(
                    'manufacturer presets require a product/preset binding'
                )
        if self.correction_sha256 != _digest(self.identity_payload()):
            raise ValueError('boundary correction hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('correction_id', None)
        payload.pop('correction_sha256', None)
        return payload


class InstalledSourceMeasurement(BaseModel):
    """Installed-condition acoustic verification (#614 §11).

    ``fit_position_ids`` and ``holdout_position_ids`` are disjoint by
    construction — a position used to tune a correction is never reused
    to validate it.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['installed-source-boundary-1'] = (
        INSTALLED_SOURCE_AUTHORITY_VERSION
    )
    measurement_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    mounting_id: str = Field(min_length=1)
    mounting_sha256: str = Field(pattern=_SHA256)
    observations: tuple[InstalledMeasurementObservation, ...] = ()
    fit_position_ids: tuple[str, ...] = ()
    holdout_position_ids: tuple[str, ...] = ()
    measured_at_utc: str = Field(min_length=1)
    instrument_refs: tuple[str, ...] = ()
    notes: str | None = Field(default=None, min_length=1)
    measurement_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_measurement(self) -> 'InstalledSourceMeasurement':
        fit = set(self.fit_position_ids)
        holdout = set(self.holdout_position_ids)
        if fit & holdout:
            raise ValueError(
                'fit and holdout positions must be disjoint — a position '
                'used for tuning cannot validate the model'
            )
        observed = {o.position_id for o in self.observations}
        if not (fit | holdout) <= observed:
            raise ValueError(
                'fit/holdout positions must exist among observations'
            )
        if len(set(self.instrument_refs)) != len(self.instrument_refs):
            raise ValueError('instrument refs must be unique')
        if self.measurement_sha256 != _digest(self.identity_payload()):
            raise ValueError('installed measurement hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('measurement_id', None)
        payload.pop('measurement_sha256', None)
        return payload


class InstalledSourceQualification(BaseModel):
    """Sealed fail-closed verdict for one installed loudspeaker."""

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['installed-source-boundary-1'] = (
        INSTALLED_SOURCE_AUTHORITY_VERSION
    )
    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    source_dataset_id: str = Field(min_length=1)
    mounting_id: str = Field(min_length=1)
    mounting_sha256: str = Field(pattern=_SHA256)
    condition_id: str | None = Field(default=None, min_length=1)
    condition_sha256: str | None = Field(default=None, pattern=_SHA256)
    state: InstalledSourceQualificationState
    required_capability: InstalledSourceCapability
    achieved_capability: InstalledSourceCapability
    directivity_applicability: DirectivityApplicability = 'unknown'
    lf_boundary_state: Literal[
        'characterized', 'approximated', 'uncharacterized', 'not_applicable'
    ] = 'uncharacterized'
    correction_ids: tuple[str, ...] = ()
    verification: BoundaryVerificationState = 'unverified'
    installed_measurement_ids: tuple[str, ...] = ()
    reasons: tuple[InstalledSourceReason, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_qualification(self) -> 'InstalledSourceQualification':
        if len(set(self.correction_ids)) != len(self.correction_ids):
            raise ValueError('correction ids must be unique')
        if len(set(self.installed_measurement_ids)) != len(
            self.installed_measurement_ids
        ):
            raise ValueError('installed measurement ids must be unique')
        if not self.reasons:
            raise ValueError('a qualification requires at least one reason')
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError('reasons must be unique')
        if self.qualification_sha256 != _digest(self.identity_payload()):
            raise ValueError('installed source qualification hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('qualification_id', None)
        payload.pop('qualification_sha256', None)
        return payload


# ---------------------------------------------------------------------------
# Builders
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


def build_source_measurement_condition(
    *,
    document_id: str,
    source_dataset_id: str,
    **fields: Any,
) -> SourceMeasurementCondition:
    return _seal(
        SourceMeasurementCondition,
        dict(
            document_id=document_id,
            source_dataset_id=source_dataset_id,
            **fields,
        ),
        'condition_id',
        'condition_sha256',
        'smc:',
    )  # type: ignore[return-value]


def build_installed_mounting(
    *,
    document_id: str,
    source_ref: str,
    kind: MountingKind = 'unknown',
    **fields: Any,
) -> InstalledMountingCondition:
    return _seal(
        InstalledMountingCondition,
        dict(
            document_id=document_id,
            source_ref=source_ref,
            kind=kind,
            **fields,
        ),
        'mounting_id',
        'mounting_sha256',
        'imc:',
    )  # type: ignore[return-value]


def build_boundary_correction(
    *,
    document_id: str,
    label: str,
    kind: BoundaryCorrectionKind,
    model_identity: str,
    model_version: str,
    **fields: Any,
) -> SourceBoundaryCorrection:
    return _seal(
        SourceBoundaryCorrection,
        dict(
            document_id=document_id,
            label=label,
            kind=kind,
            model_identity=model_identity,
            model_version=model_version,
            **fields,
        ),
        'correction_id',
        'correction_sha256',
        'sbc:',
    )  # type: ignore[return-value]


def build_installed_measurement(
    *,
    document_id: str,
    mounting: InstalledMountingCondition,
    measured_at_utc: str,
    observations: Sequence[InstalledMeasurementObservation] = (),
    fit_position_ids: Sequence[str] = (),
    holdout_position_ids: Sequence[str] = (),
    **fields: Any,
) -> InstalledSourceMeasurement:
    return _seal(
        InstalledSourceMeasurement,
        dict(
            document_id=document_id,
            mounting_id=mounting.mounting_id,
            mounting_sha256=mounting.mounting_sha256,
            observations=tuple(observations),
            fit_position_ids=tuple(fit_position_ids),
            holdout_position_ids=tuple(holdout_position_ids),
            measured_at_utc=measured_at_utc,
            **fields,
        ),
        'measurement_id',
        'measurement_sha256',
        'ism:',
    )  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Fail-closed qualification evaluator
# ---------------------------------------------------------------------------


def _mounting_matches_condition(
    condition: SourceMeasurementCondition,
    mounting: InstalledMountingCondition,
) -> bool | None:
    """Whether the dataset's capture condition matches the installation.

    ``None`` = cannot decide (condition uncharacterized).
    """
    if (
        condition.mounting_condition_at_capture == 'unknown'
        or condition.environment == 'unknown'
    ):
        return None
    capture = condition.mounting_condition_at_capture
    if capture == mounting.kind:
        return True
    # free-field / anechoic capture corresponds to free-standing use.
    if (
        condition.environment in ('free_field_anechoic', 'quasi_anechoic')
        and mounting.kind in ('free_standing', 'stand_mounted')
        and capture in ('free_standing', 'stand_mounted')
    ):
        return True
    return False


def evaluate_installed_source(
    *,
    document_id: str,
    source_dataset_id: str,
    mounting: InstalledMountingCondition,
    condition: SourceMeasurementCondition | None = None,
    corrections: Sequence[SourceBoundaryCorrection] = (),
    measurements: Sequence[InstalledSourceMeasurement] = (),
    evaluated_at_utc: str,
) -> InstalledSourceQualification:
    """Fail-closed installed-source qualification (#614 §14).

    The verdict never lets declared capability exceed bound evidence:

    - ``unsupported_configuration`` — mounting kind unknown, or the
      required capability is above the ladder this authority supports;
    - ``conflicting_corrections`` — double-application detected (dataset
      already embeds the installed boundary while a boundary gain is
      requested, or a DSP boundary preset coexists with a modelled gain);
    - ``unqualified_mounting_effect`` — the mounting requires a boundary
      treatment but none is evidenced;
    - ``insufficient_evidence`` — measurement condition uncharacterized;
    - ``qualified`` / ``qualified_with_limitations`` — achieved
      capability meets the mounting's requirement, with every residual
      caveat recorded.
    """
    reasons: list[InstalledSourceReason] = []
    limitations: list[str] = []
    required = _MOUNTING_REQUIRED_CAPABILITY[mounting.kind]  # type: ignore[return-value] from Literal map

    if mounting.kind == 'unknown':
        return _finalize(
            document_id=document_id,
            source_dataset_id=source_dataset_id,
            mounting=mounting,
            condition=condition,
            state='unsupported_configuration',
            required_capability=required,  # type: ignore[arg-type]
            achieved_capability='unsupported',
            corrections=corrections,
            measurements=measurements,
            reasons=('MOUNTING_UNCHARACTERIZED',),
            limitations=(),
            evaluated_at_utc=evaluated_at_utc,
        )

    # ---- conflict checks (double application) --------------------------
    boundary_gain_kinds = {
        'half_space_boundary_gain',
        'quarter_space_boundary_gain',
        'eighth_space_boundary_gain',
        'finite_baffle_diffraction',
        'cavity_loading',
        'manufacturer_install_preset',
    }
    requested_gains = [c for c in corrections if c.kind in boundary_gain_kinds]
    condition_includes_boundary = (
        condition is not None and condition.includes_installed_boundary
    )
    if requested_gains and condition_includes_boundary:
        reasons.append('BOUNDARY_GAIN_DOUBLE_APPLY')
        return _finalize(
            document_id=document_id,
            source_dataset_id=source_dataset_id,
            mounting=mounting,
            condition=condition,
            state='conflicting_corrections',
            required_capability=required,  # type: ignore[arg-type]
            achieved_capability='unsupported',
            corrections=corrections,
            measurements=measurements,
            reasons=tuple(dict.fromkeys(reasons)),
            limitations=limitations,
            evaluated_at_utc=evaluated_at_utc,
            directivity='incompatible',
        )
    if requested_gains and mounting.dsp_boundary_preset_ref is not None:
        reasons.append('DSP_DOUBLE_COMPENSATION_RISK')
        limitations.append(
            'dsp_boundary_compensation_declared_alongside_model_correction'
        )

    # ---- measurement-condition honesty ---------------------------------
    condition_match = (
        _mounting_matches_condition(condition, mounting)
        if condition is not None
        else None
    )
    if condition is None or condition_match is None:
        reasons.append('MEASUREMENT_CONDITION_UNKNOWN')
    elif condition_match is False:
        reasons.append('MEASUREMENT_INCOMPATIBLE_WITH_MOUNTING')

    # ---- verification from installed measurements ----------------------
    verification: BoundaryVerificationState = 'unverified'
    if measurements:
        holdout_pass: list[bool] = []
        fit_pass: list[bool] = []
        for measurement in measurements:
            for obs in measurement.observations:
                if obs.passed is None:
                    continue
                if obs.position_id in measurement.holdout_position_ids:
                    holdout_pass.append(bool(obs.passed))
                else:
                    fit_pass.append(bool(obs.passed))
        if holdout_pass:
            verification = (
                'validated_on_holdout'
                if all(holdout_pass)
                else 'holdout_disagreement'
            )
            reasons.append(
                'HOLDOUT_VALIDATED'
                if verification == 'validated_on_holdout'
                else 'HOLDOUT_DISAGREEMENT'
            )
        else:
            verification = 'fit_only'
            reasons.append('FIT_ONLY_NO_HOLDOUT')
        if fit_pass and not all(fit_pass):
            limitations.append('fit_position_residuals_exceed_tolerance')
        if any(m.mounting_sha256 != mounting.mounting_sha256 for m in measurements):
            limitations.append(
                'installed_measurement_against_superseded_mounting'
            )

    # ---- achieved capability -------------------------------------------
    achieved: InstalledSourceCapability = 'unsupported'
    correction_ids: list[str] = []
    if verification == 'validated_on_holdout':
        achieved = 'installed_measured_transfer'
        reasons.append('INSTALLED_MEASUREMENT_BOUND')
    elif condition_includes_boundary and condition_match:
        achieved = 'explicit_installed_source_model'
        reasons.append('INSTALLED_MEASUREMENT_BOUND')
    else:
        best = 0
        for correction in corrections:
            if correction.kind not in boundary_gain_kinds:
                # e.g. screen_transfer stays under its own authority.
                if correction.kind == 'screen_transfer':
                    reasons.append('SCREEN_TRANSFER_EXTERNAL_AUTHORITY')
                continue
            correction_ids.append(correction.correction_id)
            capability = _CORRECTION_CAPABILITY[correction.kind]
            rank = _CAPABILITY_RANK[capability]
            applicable = (
                not correction.applicable_mountings
                or mounting.kind in correction.applicable_mountings
            )
            if not applicable:
                reasons.append('CORRECTION_OUTSIDE_DECLARED_DOMAIN')
                continue
            if rank > best:
                best = rank
        if best:
            achieved = next(
                name for name, r in _CAPABILITY_RANK.items() if r == best
            )  # type: ignore[assignment]
        elif condition_match and mounting.kind in (
            'free_standing',
            'stand_mounted',
        ):
            achieved = 'reference_source_only'
        elif condition_match and mounting.kind == 'near_wall':
            achieved = 'reference_plus_geometric_sbir'
            reasons.append('SBIR_SEPARATION_REQUIRED')
        elif requested_gains:
            # Corrections existed but none applied to this mounting.
            achieved = 'unsupported'

    # ---- caveats --------------------------------------------------------
    finite_baffle = bool(
        mounting.geometry and mounting.geometry.finite_baffle_declared
    )
    if finite_baffle and achieved in (
        'half_space_approximation',
        'explicit_installed_source_model',
    ):
        reasons.append('FINITE_BAFFLE_NOT_HALF_SPACE')
        limitations.append('declared_finite_baffle_blocks_infinite_claim')

    cavity_mounts = (
        'in_wall',
        'flush_mounted',
        'baffle_wall',
        'ceiling_recessed',
        'soffit_mounted',
        'custom_cavity',
    )
    if mounting.kind in cavity_mounts and mounting.rear_cavity in (
        'unknown',
        'open_stud_bay',
    ):
        reasons.append('REAR_CAVITY_UNCHARACTERIZED')
        limitations.append('rear_cavity_state_not_evidenced')

    if (
        mounting.declared_by in ('design_intent', 'user_declared', 'unknown')
        and mounting.kind not in ('free_standing', 'stand_mounted')
    ):
        reasons.append('AS_BUILT_MOUNTING_UNVERIFIED')
        limitations.append('mounting_not_verified_against_as_built_geometry')

    # ---- final verdict --------------------------------------------------
    achieved_rank = _CAPABILITY_RANK[achieved]
    required_rank = _CAPABILITY_RANK[required]  # type: ignore[index]
    if 'BOUNDARY_GAIN_DOUBLE_APPLY' in reasons:
        state = 'conflicting_corrections'
    elif achieved_rank >= required_rank:
        if limitations:
            state = 'qualified_with_limitations'
            reasons.append('LIMITED_AS_DECLARED')
        else:
            state = 'qualified'
            reasons.append('QUALIFIED_AS_DECLARED')
    elif achieved == 'unsupported' and not reasons:
        state = 'insufficient_evidence'
        reasons.append('INSUFFICIENT_EVIDENCE')
    elif achieved_rank >= _CAPABILITY_RANK['half_space_approximation']:
        # partial boundary treatment exists but below requirement.
        state = 'qualified_with_limitations'
        reasons.append('LIMITED_AS_DECLARED')
    elif 'MEASUREMENT_CONDITION_UNKNOWN' in reasons:
        state = 'insufficient_evidence'
    else:
        state = 'unqualified_mounting_effect'
        if 'INSUFFICIENT_EVIDENCE' not in reasons:
            reasons.append('INSUFFICIENT_EVIDENCE')

    # ---- directivity + LF boundary views -------------------------------
    if state == 'qualified' and condition_match:
        directivity: DirectivityApplicability = 'directly_applicable'
    elif condition_includes_boundary and condition_match:
        directivity = 'installed_data_available'
    elif state in ('qualified_with_limitations',) and corrections:
        directivity = 'corrected_with_model'
    elif achieved == 'reference_source_only' and condition_match:
        directivity = 'directly_applicable'
    elif state == 'unqualified_mounting_effect':
        directivity = 'limited' if condition_match else 'unknown'
    elif state == 'conflicting_corrections':
        directivity = 'incompatible'
    else:
        directivity = 'unknown'

    if mounting.kind in ('free_standing', 'stand_mounted'):
        lf_boundary = 'not_applicable'
    elif verification == 'validated_on_holdout':
        lf_boundary = 'characterized'
    elif achieved in (
        'half_space_approximation',
        'boundary_corrected_empirical',
        'explicit_installed_source_model',
        'coupled_wave_model',
    ):
        lf_boundary = 'approximated'
    else:
        lf_boundary = 'uncharacterized'

    return _finalize(
        document_id=document_id,
        source_dataset_id=source_dataset_id,
        mounting=mounting,
        condition=condition,
        state=state,
        required_capability=required,  # type: ignore[arg-type]
        achieved_capability=achieved,
        corrections=corrections,
        measurements=measurements,
        reasons=tuple(dict.fromkeys(reasons)),
        limitations=tuple(dict.fromkeys(limitations)),
        evaluated_at_utc=evaluated_at_utc,
        directivity=directivity,
        lf_boundary=lf_boundary,
        verification=verification,
        correction_ids=correction_ids,
    )


def _finalize(
    *,
    document_id: str,
    source_dataset_id: str,
    mounting: InstalledMountingCondition,
    condition: SourceMeasurementCondition | None,
    state: InstalledSourceQualificationState,
    required_capability: InstalledSourceCapability,
    achieved_capability: InstalledSourceCapability,
    corrections: Sequence[SourceBoundaryCorrection],
    measurements: Sequence[InstalledSourceMeasurement],
    reasons: Sequence[InstalledSourceReason],
    limitations: Sequence[str],
    evaluated_at_utc: str,
    directivity: DirectivityApplicability = 'unknown',
    lf_boundary: Literal[
        'characterized', 'approximated', 'uncharacterized', 'not_applicable'
    ] = 'uncharacterized',
    verification: BoundaryVerificationState = 'unverified',
    correction_ids: Sequence[str] | None = None,
) -> InstalledSourceQualification:
    probe = InstalledSourceQualification.model_construct(
        **_canon(
            InstalledSourceQualification,
            dict(
                qualification_id='',
                document_id=document_id,
                source_dataset_id=source_dataset_id,
                mounting_id=mounting.mounting_id,
                mounting_sha256=mounting.mounting_sha256,
                condition_id=(
                    None if condition is None else condition.condition_id
                ),
                condition_sha256=(
                    None if condition is None else condition.condition_sha256
                ),
                state=state,
                required_capability=required_capability,
                achieved_capability=achieved_capability,
                directivity_applicability=directivity,
                lf_boundary_state=lf_boundary,
                correction_ids=(
                    tuple(correction_ids)
                    if correction_ids is not None
                    else tuple(c.correction_id for c in corrections)
                ),
                verification=verification,
                installed_measurement_ids=tuple(
                    m.measurement_id for m in measurements
                ),
                reasons=tuple(reasons),
                limitations=tuple(limitations),
                evaluated_at_utc=evaluated_at_utc,
                qualification_sha256='',
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return InstalledSourceQualification(
        **probe.model_dump(
            exclude={'qualification_id', 'qualification_sha256'}
        ),
        qualification_id=f'isq:{sha}',
        qualification_sha256=sha,
    )


__all__ = [
    'BoundaryCorrectionKind',
    'BoundaryVerificationState',
    'CAPABILITY_LABELS',
    'CORRECTION_KIND_LABELS',
    'CorrectionDomain',
    'DIRECTIVITY_APPLICABILITY_LABELS',
    'DirectivityApplicability',
    'FacingAbsorberState',
    'INSTALLED_SOURCE_AUTHORITY_VERSION',
    'InstalledMeasurementObservation',
    'InstalledMountingCondition',
    'InstalledSourceCapability',
    'InstalledSourceMeasurement',
    'InstalledSourceQualification',
    'InstalledSourceQualificationState',
    'InstalledSourceReason',
    'MEASUREMENT_ENVIRONMENT_LABELS',
    'MEASUREMENT_PROFILE_LABELS',
    'MOUNTING_KIND_LABELS',
    'MeasurementGeometry',
    'MountedGeometry',
    'MountingKind',
    'QUALIFICATION_STATE_LABELS',
    'REASON_LABELS',
    'RearCavityState',
    'SourceBoundaryCorrection',
    'SourceConditionEvidenceClass',
    'SourceMeasurementBaffle',
    'SourceMeasurementCondition',
    'SourceMeasurementEnvironment',
    'SourceMeasurementProfile',
    'VERIFICATION_STATE_LABELS',
    'build_boundary_correction',
    'build_installed_measurement',
    'build_installed_mounting',
    'build_source_measurement_condition',
    'evaluate_installed_source',
]
