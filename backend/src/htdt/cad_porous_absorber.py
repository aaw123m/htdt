"""Porous-absorber physical-model authority (#615).

A material named "glass wool" is not physics: an absorption prediction is
only as honest as (a) the parameter evidence behind it, (b) the model's
declared validity domain, and (c) the exact tested build-up it describes.
This module is the fail-closed authority over all three:

- :class:`PorousParameterEvidence` — one parameter measurement or claim
  (airflow resistivity, porosity, tortuosity, viscous/thermal
  characteristic lengths, density, thickness, …) with its *method
  identity*: ISO 9053-1:2026 static, ISO 9053-2:2020 alternating flow,
  historical ISO 9053-1:2018, manufacturer-declared, independent lab,
  in-situ, inverse-estimated, user-assumed, unknown. The historical
  edition stays distinct — a pre-2026 value never gets relabeled.
- :class:`PorousMaterialModel` — a declared model identity: family
  (Delany–Bazley, Miki empirical, DB with LF correction,
  Johnson–Champoux–Allard family, other equivalent-fluid, poroelastic
  Biot, measured complex impedance, custom validated), its exact
  coefficient set, the parameters it requires, its declared validity
  domain (dimensionless or absolute), incidence capability and
  assumptions. Coefficients are model content — sealed, versioned,
  reproducible.
- :class:`PorousBuildUp` — the exact tested construction: ordered porous
  layers with thickness and per-layer air gap, backing kind, facing
  (which carries its own support state), orientation, frame, finite-area
  note. Air gap is an explicit layer, never an implicit fudge — a gap
  change is a different build-up.
- :class:`PorousBoundaryPrediction` — a sealed parametric prediction:
  model + build-up + parameter-evidence refs pinned by id+sha, the
  computed band table (complex surface impedance + normal-incidence
  absorption), and the excluded bands that fell outside the model's
  domain. Evidence class is always ``parametric_model_prediction`` —
  predictions never masquerade as measurements, and out-of-domain
  frequencies are never clipped into a plausible-looking curve.
- :class:`PorousFitComparison` — prediction-vs-measurement residual
  evidence with disjoint fit/holdout band sets; a fitted parameter set
  is never relabeled as measured.

Contract properties:

- the eligibility gate (:func:`evaluate_porous_model_eligibility`) is
  fail-closed: missing parameters → ``missing_parameters``; out-of-domain
  request → ``outside_validated_domain``; unknown anisotropy on an
  isotropic model → ``eligible_with_limitations`` at best; an uncomputed
  family → ``unsupported``;
- predictions only carry bands inside the model's declared validity
  domain — out-of-domain bands are listed in ``excluded_bands_hz``;
- measured curves and predicted curves are different records — a fit
  comparison links them but never merges them;
- :func:`prediction_as_boundary_evidence` exports the prediction into the
  #570 material-boundary compatibility gate as ``derived_conversion``
  evidence — solver compatibility stays that gate's decision, this
  module only declares what was actually computed.

Literature basis (issue §research): Delany & Bazley (1970) empirical
power laws with the published dimensionless domain; Miki (1990)
positive-real corrected coefficients (J. Acoust. Soc. Jpn. 11(1));
Johnson–Champoux–Allard equivalent-fluid model for rigid-frame porous
media (Allard & Atalla, *Propagation of Sound in Porous Media*, 2nd ed.);
ISO 9053-1:2026 (Edition 2 — cancels and replaces the 2018 edition,
which stays as ``historical_iso_9053_1_2018`` method), ISO 9053-2:2020
(confirmed 2025), ISO 10534-2:2023 for tube-measured surface impedance
and absorption (whose values are explicitly not comparable with ISO 354
random-incidence coefficients), and ISO 354 for the diffuse-field class.
"""

from __future__ import annotations

from cmath import pi as _cpi, sqrt as _csqrt, tan as _ctan
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_material_evidence_compatibility import (
    MaterialBoundaryEvidence,
    MaterialBuildUp,
    build_boundary_evidence,
)
from .canonical_json import canonical_sha256 as _digest, canonicalize_payload as _canon


_SHA256 = r'^[0-9a-f]{64}$'

POROUS_ABSORBER_AUTHORITY_VERSION = 'porous-absorber-authority-1'


# ---------------------------------------------------------------------------
# Taxonomies (issue §1–§5, §8, §10)
# ---------------------------------------------------------------------------

PorousParameterQuantity = Literal[
    'airflow_resistance',
    'airflow_resistivity',
    'porosity',
    'tortuosity',
    'viscous_characteristic_length',
    'thermal_characteristic_length',
    'static_viscous_permeability',
    'static_thermal_permeability',
    'material_density',
    'thickness',
]

PorousEvidenceClass = Literal[
    'measured',
    'manufacturer_declared',
    'literature_assumed',
    'inverse_estimated',
    'user_assumed',
    'unknown',
]

PorousParameterMethod = Literal[
    'iso_9053_1_2026_static',
    'iso_9053_2_2020_alternating',
    'historical_iso_9053_1_2018',
    'manufacturer_method',
    'independent_lab_other',
    'in_situ_estimate',
    'inverse_estimated',
    'user_assumed',
    'unknown',
]

PorousModelFamily = Literal[
    'delany_bazley',
    'miki_empirical',
    'delany_bazley_lf_corrected',
    'johnson_champoux_allard',
    'other_equivalent_fluid',
    'poroelastic_biot',
    'measured_complex_impedance',
    'custom_validated',
]

PorousMaterialClass = Literal[
    'fibrous_porous',
    'open_cell_foam',
    'granular_porous',
    'fabric_layer',
    'membrane_resonant',
    'perforated_resonator',
    'mixed_structure',
    'unknown',
]

AnisotropyState = Literal[
    'isotropic_assumed',
    'directional_available',
    'declared_anisotropic',
    'anisotropy_unknown',
]

PorousFacingKind = Literal[
    'none',
    'fabric_scrim',
    'perforated_panel',
    'membrane_resonant',
    'impermeable_skin',
    'unknown',
]

PorousFacingSupport = Literal[
    'unsupported',
    'negligible_declared',
    'resistive_modelled',
    'unknown',
]

PorousBacking = Literal[
    'rigid',
    'finite_absorbing',
    'free_air',
    'unknown',
]

IncidenceCapability = Literal[
    'normal_impedance_only',
    'oblique_supported',
    'locally_reacting_diffuse_estimate',
    'unknown',
]

PorousEligibility = Literal[
    'eligible',
    'eligible_with_limitations',
    'outside_validated_domain',
    'missing_parameters',
    'incompatible_material',
    'unsupported',
]

PorousEligibilityReason = Literal[
    'REQUIRED_PARAMETER_MISSING',
    'PARAMETER_WITHOUT_EVIDENCE',
    'INVERSE_ESTIMATED_PARAMETERS',
    'ASSUMED_PARAMETERS',
    'OUTSIDE_DIMENSIONLESS_DOMAIN',
    'OUTSIDE_FREQUENCY_DOMAIN',
    'MATERIAL_CLASS_INCOMPATIBLE',
    'ANISOTROPY_UNKNOWN_ASSUMED_ISOTROPIC',
    'ANISOTROPY_UNSUPPORTED',
    'FACING_UNSUPPORTED',
    'BACKING_UNSUPPORTED',
    'MODEL_NOT_IMPLEMENTED',
    'MULTI_LAYER_UNSUPPORTED',
    'ELIGIBLE_AS_DECLARED',
    'LIMITED_AS_DECLARED',
]

FitVerdict = Literal[
    'validated_on_holdout',
    'fit_only',
    'residual_exceeds_declared',
    'insufficient_coverage',
]

PorousParameterUnit = Literal[
    'Pa*s/m^2',
    'Pa*s',
    'dimensionless',
    'm',
    'mm',
    'm^2',
    'kg/m^3',
]


# ---------------------------------------------------------------------------
# Japanese product labels
# ---------------------------------------------------------------------------

PARAMETER_QUANTITY_LABELS: dict[str, str] = {
    'airflow_resistance': '気流抵抗',
    'airflow_resistivity': '気流抵抗率',
    'porosity': '空隙率',
    'tortuosity': '迷路率',
    'viscous_characteristic_length': '粘性特性長',
    'thermal_characteristic_length': '熱特性長',
    'static_viscous_permeability': '静的粘性透過率',
    'static_thermal_permeability': '静的熱透過率',
    'material_density': '材料密度',
    'thickness': '厚さ',
}

EVIDENCE_CLASS_LABELS: dict[str, str] = {
    'measured': '実測',
    'manufacturer_declared': 'メーカー宣言値',
    'literature_assumed': '文献想定値',
    'inverse_estimated': '逆推定値',
    'user_assumed': 'ユーザー想定値',
    'unknown': '不明',
}

METHOD_LABELS: dict[str, str] = {
    'iso_9053_1_2026_static': 'ISO 9053-1:2026 静的法',
    'iso_9053_2_2020_alternating': 'ISO 9053-2:2020 交流気流法',
    'historical_iso_9053_1_2018': 'ISO 9053-1:2018（旧版・歴史的）',
    'manufacturer_method': 'メーカー手法',
    'independent_lab_other': '独立ラボその他手法',
    'in_situ_estimate': '現場推定',
    'inverse_estimated': '逆推定',
    'user_assumed': 'ユーザー想定',
    'unknown': '不明',
}

MODEL_FAMILY_LABELS: dict[str, str] = {
    'delany_bazley': 'Delany-Bazley',
    'miki_empirical': 'Miki 実験式',
    'delany_bazley_lf_corrected': 'Delany-Bazley 低域補正',
    'johnson_champoux_allard': 'Johnson-Champoux-Allard',
    'other_equivalent_fluid': 'その他等価流体モデル',
    'poroelastic_biot': 'Biot 弾性多孔質',
    'measured_complex_impedance': '実測複素インピーダンス',
    'custom_validated': 'カスタム検証済み',
}

ELIGIBILITY_LABELS: dict[str, str] = {
    'eligible': '適格',
    'eligible_with_limitations': '制限付き適格',
    'outside_validated_domain': '検証適用域外',
    'missing_parameters': 'パラメータ不足',
    'incompatible_material': '材料クラス非互換',
    'unsupported': '未対応',
}

ELIGIBILITY_REASON_LABELS: dict[str, str] = {
    'REQUIRED_PARAMETER_MISSING': '必須パラメータ不足',
    'PARAMETER_WITHOUT_EVIDENCE': '証拠なしパラメータ',
    'INVERSE_ESTIMATED_PARAMETERS': '逆推定パラメータを含む',
    'ASSUMED_PARAMETERS': '想定値パラメータを含む',
    'OUTSIDE_DIMENSIONLESS_DOMAIN': '無次元適用域外',
    'OUTSIDE_FREQUENCY_DOMAIN': '周波数適用域外',
    'MATERIAL_CLASS_INCOMPATIBLE': '材料クラスがモデル非互換',
    'ANISOTROPY_UNKNOWN_ASSUMED_ISOTROPIC': '異方性不明・等方仮定',
    'ANISOTROPY_UNSUPPORTED': '異方性宣言・等方モデル非対応',
    'FACING_UNSUPPORTED': '表面材が未対応',
    'BACKING_UNSUPPORTED': '背後条件が未対応',
    'MODEL_NOT_IMPLEMENTED': 'モデル未実装（宣言のみ）',
    'MULTI_LAYER_UNSUPPORTED': '多層構成未対応',
    'ELIGIBLE_AS_DECLARED': '宣言どおり適格',
    'LIMITED_AS_DECLARED': '宣言どおり限定的',
}

FACING_LABELS: dict[str, str] = {
    'none': 'なし',
    'fabric_scrim': '不織布/スクリム',
    'perforated_panel': '穿孔板',
    'membrane_resonant': '膜共振面',
    'impermeable_skin': '不透過スキン',
    'unknown': '不明',
}

FACING_SUPPORT_LABELS: dict[str, str] = {
    'unsupported': '未対応',
    'negligible_declared': '無視可能（宣言）',
    'resistive_modelled': '抵抗性モデル済み',
    'unknown': '不明',
}

BACKING_LABELS: dict[str, str] = {
    'rigid': '剛壁',
    'finite_absorbing': '有限吸収',
    'free_air': '開放空気',
    'unknown': '不明',
}

MATERIAL_CLASS_LABELS: dict[str, str] = {
    'fibrous_porous': '繊維系多孔質',
    'open_cell_foam': '連続気泡フォーム',
    'granular_porous': '粒状多孔質',
    'fabric_layer': '布層',
    'membrane_resonant': '膜共振型',
    'perforated_resonator': '穿孔板共振型',
    'mixed_structure': '混合構造',
    'unknown': '不明',
}

FIT_VERDICT_LABELS: dict[str, str] = {
    'validated_on_holdout': 'ホールドアウト検証済み',
    'fit_only': 'フィットのみ（ホールドアウトなし）',
    'residual_exceeds_declared': '残差が宣言値超過',
    'insufficient_coverage': '帯域カバー不足',
}


# ---------------------------------------------------------------------------
# Air constants + built-in model registry
# ---------------------------------------------------------------------------

AIR_DENSITY_KG_M3 = 1.204
AIR_SOUND_SPEED_M_S = 343.0
AIR_VISCOSITY_PA_S = 1.85e-5
AIR_PRANDTL = 0.71
AIR_SPECIFIC_HEAT_RATIO = 1.4
AIR_PRESSURE_PA = 101325.0


class ModelCoefficient(BaseModel):
    """One named coefficient of a declared model — sealed model content."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    name: str = Field(min_length=1)
    value: float

    @model_validator(mode='after')
    def finite(self) -> 'ModelCoefficient':
        if not isfinite(float(self.value)):
            raise ValueError('coefficients must be finite')
        return self


class ModelValidityDomain(BaseModel):
    """Declared applicability domain of a porous model."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    dimensionless_min: float | None = Field(default=None, gt=0.0)
    dimensionless_max: float | None = Field(default=None, gt=0.0)
    frequency_min_hz: float | None = Field(default=None, gt=0.0)
    frequency_max_hz: float | None = Field(default=None, gt=0.0)
    resistivity_min_pa_s_m2: float | None = Field(default=None, gt=0.0)
    resistivity_max_pa_s_m2: float | None = Field(default=None, gt=0.0)

    @model_validator(mode='after')
    def consistent(self) -> 'ModelValidityDomain':
        for value in (
            self.dimensionless_min,
            self.dimensionless_max,
            self.frequency_min_hz,
            self.frequency_max_hz,
            self.resistivity_min_pa_s_m2,
            self.resistivity_max_pa_s_m2,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('validity bounds must be finite')
        pairs = (
            (self.dimensionless_min, self.dimensionless_max),
            (self.frequency_min_hz, self.frequency_max_hz),
            (self.resistivity_min_pa_s_m2, self.resistivity_max_pa_s_m2),
        )
        for lo, hi in pairs:
            if lo is not None and hi is not None and hi < lo:
                raise ValueError('validity bound order violated')
        return self


class PorousMaterialModel(BaseModel):
    """Declared porous-model identity (#615 §3).

    ``required_parameters`` names the quantities the model needs;
    ``coefficients`` is the sealed parameterization; ``compute_capable``
    distinguishes a *implemented* model family from a declared-but
    uncomputed one (a declared model can never fabricate predictions).
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['porous-absorber-authority-1'] = (
        POROUS_ABSORBER_AUTHORITY_VERSION
    )
    model_id: str = Field(min_length=1)
    family: PorousModelFamily
    label: str = Field(min_length=1)
    version: str = Field(min_length=1)
    reference: str | None = Field(default=None, min_length=1)
    required_parameters: tuple[PorousParameterQuantity, ...] = ()
    material_classes: tuple[PorousMaterialClass, ...] = ()
    coefficients: tuple[ModelCoefficient, ...] = ()
    validity: ModelValidityDomain | None = None
    incidence_capability: IncidenceCapability = 'unknown'
    requires_isotropy: bool = True
    compute_capable: bool = False
    assumptions: tuple[str, ...] = ()
    model_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_model(self) -> 'PorousMaterialModel':
        if len(set(self.required_parameters)) != len(
            self.required_parameters
        ):
            raise ValueError('required parameters must be unique')
        names = [c.name for c in self.coefficients]
        if len(names) != len(set(names)):
            raise ValueError('coefficient names must be unique')
        if len(set(self.material_classes)) != len(self.material_classes):
            raise ValueError('material classes must be unique')
        if len(set(self.assumptions)) != len(self.assumptions):
            raise ValueError('assumptions must be unique')
        if self.model_sha256 != _digest(self.identity_payload()):
            raise ValueError('porous material model hash mismatch')
        return self

    def coefficient(self, name: str) -> float:
        for c in self.coefficients:
            if c.name == name:
                return c.value
        raise KeyError(name)

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('model_id', None)
        payload.pop('model_sha256', None)
        return payload


# ---------------------------------------------------------------------------
# Built-in declared models — coefficients are sealed content.
# ---------------------------------------------------------------------------


def delany_bazley_model() -> PorousMaterialModel:
    """Delany & Bazley (1970), Appl. Acoust. 3:105 — normalized-X form.

    X = rho0 * f / sigma. Published regression domain
    0.01 <= X <= 1.0 for fibrous materials with
    sigma roughly 1e3..6e4 Pa*s/m^2.
    """
    return _seal(
        PorousMaterialModel,
        dict(
            family='delany_bazley',
            label='Delany-Bazley (1970)',
            version='DB1970-normalized',
            reference='Delany & Bazley, Appl. Acoust. 3:105-116 (1970)',
            required_parameters=('airflow_resistivity', 'thickness'),
            material_classes=('fibrous_porous', 'open_cell_foam'),
            coefficients=(
                ModelCoefficient(name='zc_real_coeff', value=0.0571),
                ModelCoefficient(name='zc_real_exp', value=-0.754),
                ModelCoefficient(name='zc_imag_coeff', value=0.0870),
                ModelCoefficient(name='zc_imag_exp', value=-0.732),
                ModelCoefficient(name='k_real_coeff', value=0.0978),
                ModelCoefficient(name='k_real_exp', value=-0.700),
                ModelCoefficient(name='k_imag_coeff', value=0.1890),
                ModelCoefficient(name='k_imag_exp', value=-0.595),
            ),
            validity=ModelValidityDomain(
                dimensionless_min=0.01,
                dimensionless_max=1.0,
                resistivity_min_pa_s_m2=1000.0,
                resistivity_max_pa_s_m2=60000.0,
            ),
            incidence_capability='normal_impedance_only',
            requires_isotropy=True,
            compute_capable=True,
            assumptions=(
                'rigid_frame_equivalent_fluid',
                'isotropic_material',
                'single_porous_layer',
            ),
        ),
        'model_id',
        'model_sha256',
        'pmm:',
    )  # type: ignore[return-value]


def miki_model() -> PorousMaterialModel:
    """Miki (1990) positive-real correction of Delany-Bazley.

    J. Acoust. Soc. Jpn. 11(1):19-24 — satisfies positive-real impedance
    and is regular in the right half-plane; safer at low frequency than
    the original DB fit, still empirical (same X domain declared).
    """
    return _seal(
        PorousMaterialModel,
        dict(
            family='miki_empirical',
            label='Miki (1990) DB-modified',
            version='MIKI1990-normalized',
            reference=(
                'Y. Miki, J. Acoust. Soc. Jpn. 11(1):19-24 (1990)'
            ),
            required_parameters=('airflow_resistivity', 'thickness'),
            material_classes=('fibrous_porous', 'open_cell_foam'),
            coefficients=(
                ModelCoefficient(name='zc_real_coeff', value=0.0699),
                ModelCoefficient(name='zc_real_exp', value=-0.632),
                ModelCoefficient(name='zc_imag_coeff', value=0.1070),
                ModelCoefficient(name='zc_imag_exp', value=-0.632),
                ModelCoefficient(name='k_real_coeff', value=0.1090),
                ModelCoefficient(name='k_real_exp', value=-0.618),
                ModelCoefficient(name='k_imag_coeff', value=0.1600),
                ModelCoefficient(name='k_imag_exp', value=-0.618),
            ),
            validity=ModelValidityDomain(
                dimensionless_min=0.006,
                dimensionless_max=1.5,
                resistivity_min_pa_s_m2=1000.0,
                resistivity_max_pa_s_m2=100000.0,
            ),
            incidence_capability='normal_impedance_only',
            requires_isotropy=True,
            compute_capable=True,
            assumptions=(
                'rigid_frame_equivalent_fluid',
                'isotropic_material',
                'single_porous_layer',
                'positive_real_corrected',
            ),
        ),
        'model_id',
        'model_sha256',
        'pmm:',
    )  # type: ignore[return-value]


def johnson_champoux_allard_model() -> PorousMaterialModel:
    """Johnson-Champoux-Allard rigid-frame equivalent-fluid model.

    Allard & Atalla, Propagation of Sound in Porous Media, 2nd ed. —
    needs the real parameter set (sigma, porosity, tortuosity, Lambda,
    Lambda'); missing values must gate, never be invented.
    """
    return _seal(
        PorousMaterialModel,
        dict(
            family='johnson_champoux_allard',
            label='Johnson-Champoux-Allard',
            version='JCA-rigid-frame',
            reference=(
                'Allard & Atalla, Propagation of Sound in Porous Media, '
                '2nd ed. (Wiley 2009), ch. 5'
            ),
            required_parameters=(
                'airflow_resistivity',
                'porosity',
                'tortuosity',
                'viscous_characteristic_length',
                'thermal_characteristic_length',
                'thickness',
            ),
            material_classes=('fibrous_porous', 'open_cell_foam', 'granular_porous'),
            coefficients=(),
            validity=ModelValidityDomain(
                frequency_min_hz=20.0,
                frequency_max_hz=20000.0,
            ),
            incidence_capability='normal_impedance_only',
            requires_isotropy=True,
            compute_capable=True,
            assumptions=(
                'rigid_frame_equivalent_fluid',
                'isotropic_material',
                'single_porous_layer',
                'jca_5_parameter_form',
            ),
        ),
        'model_id',
        'model_sha256',
        'pmm:',
    )  # type: ignore[return-value]


def measured_impedance_model(reference: str) -> PorousMaterialModel:
    """Declared passthrough for measured complex impedance datasets.

    The model is not a predictor — predictions built on it carry only the
    measured bands themselves, sourced from impedance-tube evidence.
    """
    return _seal(
        PorousMaterialModel,
        dict(
            family='measured_complex_impedance',
            label='Measured complex impedance (ISO 10534-2)',
            version='measured-1',
            reference=reference,
            required_parameters=(),
            material_classes=(),
            coefficients=(),
            validity=None,
            incidence_capability='normal_impedance_only',
            requires_isotropy=False,
            compute_capable=False,
            assumptions=('bands_come_from_measurement_only',),
        ),
        'model_id',
        'model_sha256',
        'pmm:',
    )  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Parameter evidence record
# ---------------------------------------------------------------------------

_MEASURED_METHODS = frozenset(
    {
        'iso_9053_1_2026_static',
        'iso_9053_2_2020_alternating',
        'historical_iso_9053_1_2018',
        'independent_lab_other',
        'in_situ_estimate',
    }
)


class PorousParameterEvidence(BaseModel):
    """One material parameter with method + provenance (#615 §1)."""

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['porous-absorber-authority-1'] = (
        POROUS_ABSORBER_AUTHORITY_VERSION
    )
    evidence_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    material_ref: str = Field(min_length=1)
    quantity: PorousParameterQuantity
    value: float = Field(gt=0.0)
    unit: PorousParameterUnit
    evidence_class: PorousEvidenceClass = 'unknown'
    method: PorousParameterMethod = 'unknown'
    laboratory: str | None = Field(default=None, min_length=1)
    specimen_ref: str | None = Field(default=None, min_length=1)
    uncertainty_note: str | None = Field(default=None, min_length=1)
    source_ref: str | None = Field(default=None, min_length=1)
    fit_record_ref: str | None = Field(default=None, min_length=1)
    evidence_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_evidence(self) -> 'PorousParameterEvidence':
        if not isfinite(float(self.value)):
            raise ValueError('parameter values must be finite')
        if self.evidence_class == 'measured':
            if self.method not in _MEASURED_METHODS:
                raise ValueError(
                    "evidence_class 'measured' requires a measured method"
                )
        elif self.method in _MEASURED_METHODS and self.method not in (
            'in_situ_estimate',
        ):
            # A standard method claimed but not 'measured' class is fine
            # only when the class honestly says derived/declared.
            pass
        if self.evidence_class == 'inverse_estimated':
            if self.method != 'inverse_estimated':
                raise ValueError(
                    'inverse-estimated evidence must declare its method'
                )
            if self.fit_record_ref is None:
                raise ValueError(
                    'inverse-estimated parameters require a fit record ref'
                )
        if self.method == 'inverse_estimated' and (
            self.evidence_class != 'inverse_estimated'
        ):
            raise ValueError(
                'inverse-estimated parameters are never relabeled measured'
            )
        if self.evidence_sha256 != _digest(self.identity_payload()):
            raise ValueError('parameter evidence hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('evidence_id', None)
        payload.pop('evidence_sha256', None)
        return payload


# ---------------------------------------------------------------------------
# Build-up record
# ---------------------------------------------------------------------------


class PorousFacing(BaseModel):
    """Facing declaration — unsupported facings limit predictions."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: PorousFacingKind = 'none'
    support: PorousFacingSupport = 'unknown'
    surface_resistivity_pa_s_m: float | None = Field(default=None, gt=0.0)

    @model_validator(mode='after')
    def consistent(self) -> 'PorousFacing':
        if self.surface_resistivity_pa_s_m is not None and not isfinite(
            float(self.surface_resistivity_pa_s_m)
        ):
            raise ValueError('surface resistivity must be finite')
        if self.support == 'resistive_modelled':
            if self.surface_resistivity_pa_s_m is None:
                raise ValueError(
                    'resistive-modelled facings need a surface resistivity'
                )
        if self.kind in ('none',) and self.support not in (
            'negligible_declared',
            'unknown',
        ):
            raise ValueError('a missing facing cannot claim support')
        if self.kind == 'unknown' and self.support not in (
            'unknown',
            'unsupported',
        ):
            raise ValueError('an unknown facing cannot claim support')
        return self


class PorousLayer(BaseModel):
    """One porous layer in the build-up — material + exact geometry."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    material_ref: str = Field(min_length=1)
    thickness_mm: float = Field(gt=0.0)
    air_gap_behind_mm: float | None = Field(default=None, ge=0.0)
    facing: PorousFacing | None = None

    @model_validator(mode='after')
    def finite(self) -> 'PorousLayer':
        for value in (self.thickness_mm, self.air_gap_behind_mm):
            if value is not None and not isfinite(float(value)):
                raise ValueError('layer dimensions must be finite')
        return self


class PorousBuildUp(BaseModel):
    """Exact tested construction (#615 §8) — sealed identity.

    Air gap is an explicit per-layer field: a gap change produces a
    different sealed build-up, so predictions never silently absorb it.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['porous-absorber-authority-1'] = (
        POROUS_ABSORBER_AUTHORITY_VERSION
    )
    buildup_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    label: str | None = Field(default=None, min_length=1)
    layers: tuple[PorousLayer, ...]
    backing: PorousBacking = 'unknown'
    orientation: str | None = Field(default=None, min_length=1)
    frame_kind: str | None = Field(default=None, min_length=1)
    finite_area_note: str | None = Field(default=None, min_length=1)
    anisotropy: AnisotropyState = 'anisotropy_unknown'
    evidence_refs: tuple[str, ...] = ()
    buildup_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_buildup(self) -> 'PorousBuildUp':
        if not self.layers:
            raise ValueError('a build-up requires at least one layer')
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError('evidence refs must be unique')
        if self.buildup_sha256 != _digest(self.identity_payload()):
            raise ValueError('porous build-up hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('buildup_id', None)
        payload.pop('buildup_sha256', None)
        return payload


# ---------------------------------------------------------------------------
# Prediction record
# ---------------------------------------------------------------------------


class PredictedBandPoint(BaseModel):
    """One computed band of a parametric prediction."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    frequency_hz: float = Field(gt=0.0)
    impedance_real_pa_s_m: float
    impedance_imag_pa_s_m: float
    absorption_alpha: float = Field(ge=0.0, le=1.0)

    @model_validator(mode='after')
    def finite(self) -> 'PredictedBandPoint':
        for value in (
            self.frequency_hz,
            self.impedance_real_pa_s_m,
            self.impedance_imag_pa_s_m,
            self.absorption_alpha,
        ):
            if not isfinite(float(value)):
                raise ValueError('band values must be finite')
        return self


class PorousBoundaryPrediction(BaseModel):
    """Sealed parametric prediction artifact (#615 §4).

    ``evidence_class`` is always ``parametric_model_prediction`` —
    downstream gates see exactly what this is. Bands outside the model's
    declared domain are never emitted; they live in ``excluded_bands_hz``.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['porous-absorber-authority-1'] = (
        POROUS_ABSORBER_AUTHORITY_VERSION
    )
    prediction_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    model_sha256: str = Field(pattern=_SHA256)
    buildup_id: str = Field(min_length=1)
    buildup_sha256: str = Field(pattern=_SHA256)
    parameter_evidence_ids: tuple[str, ...] = ()
    evidence_class: Literal['parametric_model_prediction'] = (
        'parametric_model_prediction'
    )
    incidence: Literal['normal'] = 'normal'
    bands: tuple[PredictedBandPoint, ...] = ()
    excluded_bands_hz: tuple[float, ...] = ()
    eligibility: PorousEligibility
    eligibility_reasons: tuple[PorousEligibilityReason, ...] = ()
    computed_at_utc: str = Field(min_length=1)
    prediction_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_prediction(self) -> 'PorousBoundaryPrediction':
        freqs = [b.frequency_hz for b in self.bands]
        if list(freqs) != sorted(freqs):
            raise ValueError('prediction bands must be frequency-sorted')
        if len(set(freqs)) != len(freqs):
            raise ValueError('prediction bands must be unique')
        for f in self.excluded_bands_hz:
            if not isfinite(float(f)) or f <= 0.0:
                raise ValueError('excluded bands must be positive finite')
        if set(freqs) & set(self.excluded_bands_hz):
            raise ValueError(
                'a band cannot be both predicted and excluded'
            )
        if len(set(self.parameter_evidence_ids)) != len(
            self.parameter_evidence_ids
        ):
            raise ValueError('parameter evidence ids must be unique')
        if self.prediction_sha256 != _digest(self.identity_payload()):
            raise ValueError('prediction hash mismatch')
        return self

    def band_at(self, frequency_hz: float) -> PredictedBandPoint | None:
        for band in self.bands:
            if band.frequency_hz == frequency_hz:
                return band
        return None

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('prediction_id', None)
        payload.pop('prediction_sha256', None)
        return payload


# ---------------------------------------------------------------------------
# Fit-comparison record
# ---------------------------------------------------------------------------


class FitBandResidual(BaseModel):
    """Predicted-vs-measured residual at one band."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    frequency_hz: float = Field(gt=0.0)
    predicted_alpha: float | None = Field(default=None, ge=0.0, le=1.0)
    measured_alpha: float = Field(ge=0.0, le=1.0)
    fit_set: bool = False

    @model_validator(mode='after')
    def finite(self) -> 'FitBandResidual':
        for value in (self.frequency_hz, self.measured_alpha):
            if not isfinite(float(value)):
                raise ValueError('residual band values must be finite')
        if self.predicted_alpha is not None and not isfinite(
            float(self.predicted_alpha)
        ):
            raise ValueError('predicted alpha must be finite')
        return self

    @property
    def residual(self) -> float | None:
        if self.predicted_alpha is None:
            return None
        return abs(self.measured_alpha - self.predicted_alpha)


class PorousFitComparison(BaseModel):
    """Prediction-vs-measurement record (#615 §6).

    ``fit_band_hz`` and ``holdout_band_hz`` are disjoint — bands used to
    fit parameters can never validate the model.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['porous-absorber-authority-1'] = (
        POROUS_ABSORBER_AUTHORITY_VERSION
    )
    comparison_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    prediction_id: str | None = Field(default=None, min_length=1)
    prediction_sha256: str | None = Field(default=None, pattern=_SHA256)
    measured_evidence_ref: str = Field(min_length=1)
    residuals: tuple[FitBandResidual, ...] = ()
    fit_band_hz: tuple[float, ...] = ()
    holdout_band_hz: tuple[float, ...] = ()
    declared_tolerance: float | None = Field(default=None, gt=0.0)
    verdict: FitVerdict
    compared_at_utc: str = Field(min_length=1)
    notes: str | None = Field(default=None, min_length=1)
    comparison_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_comparison(self) -> 'PorousFitComparison':
        if set(self.fit_band_hz) & set(self.holdout_band_hz):
            raise ValueError(
                'fit and holdout bands must be disjoint — a band used '
                'to fit parameters cannot validate the model'
            )
        freqs = {r.frequency_hz for r in self.residuals}
        if not (set(self.fit_band_hz) | set(self.holdout_band_hz)) <= freqs:
            raise ValueError(
                'fit/holdout bands must exist among residuals'
            )
        for f in self.fit_band_hz:
            if not isfinite(float(f)) or f <= 0.0:
                raise ValueError('band frequencies must be positive finite')
        for f in self.holdout_band_hz:
            if not isfinite(float(f)) or f <= 0.0:
                raise ValueError('band frequencies must be positive finite')
        if self.declared_tolerance is not None and not isfinite(
            float(self.declared_tolerance)
        ):
            raise ValueError('declared tolerance must be finite')
        if self.comparison_sha256 != _digest(self.identity_payload()):
            raise ValueError('fit comparison hash mismatch')
        return self

    def holdout_residuals(self) -> tuple[FitBandResidual, ...]:
        holdout = set(self.holdout_band_hz)
        return tuple(
            r for r in self.residuals if r.frequency_hz in holdout
        )

    def fit_residuals(self) -> tuple[FitBandResidual, ...]:
        fit = set(self.fit_band_hz)
        return tuple(r for r in self.residuals if r.frequency_hz in fit)

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('comparison_id', None)
        payload.pop('comparison_sha256', None)
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


def build_parameter_evidence(
    *,
    document_id: str,
    material_ref: str,
    quantity: PorousParameterQuantity,
    value: float,
    unit: PorousParameterUnit,
    **fields: Any,
) -> PorousParameterEvidence:
    return _seal(
        PorousParameterEvidence,
        dict(
            document_id=document_id,
            material_ref=material_ref,
            quantity=quantity,
            value=value,
            unit=unit,
            **fields,
        ),
        'evidence_id',
        'evidence_sha256',
        'ppe:',
    )  # type: ignore[return-value]


def build_porous_model(
    *,
    family: PorousModelFamily,
    label: str,
    version: str,
    required_parameters: Sequence[PorousParameterQuantity] = (),
    coefficients: Sequence[ModelCoefficient] = (),
    **fields: Any,
) -> PorousMaterialModel:
    return _seal(
        PorousMaterialModel,
        dict(
            family=family,
            label=label,
            version=version,
            required_parameters=tuple(required_parameters),
            coefficients=tuple(coefficients),
            **fields,
        ),
        'model_id',
        'model_sha256',
        'pmm:',
    )  # type: ignore[return-value]


def build_porous_buildup(
    *,
    document_id: str,
    layers: Sequence[PorousLayer],
    backing: PorousBacking = 'unknown',
    **fields: Any,
) -> PorousBuildUp:
    return _seal(
        PorousBuildUp,
        dict(
            document_id=document_id,
            layers=tuple(layers),
            backing=backing,
            **fields,
        ),
        'buildup_id',
        'buildup_sha256',
        'pbu:',
    )  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Eligibility evaluator — fail-closed, never clips
# ---------------------------------------------------------------------------


def evaluate_porous_model_eligibility(
    *,
    model: PorousMaterialModel,
    parameters: Sequence[PorousParameterEvidence],
    buildup: PorousBuildUp | None,
    frequencies_hz: Sequence[float],
    material_class: PorousMaterialClass = 'unknown',
) -> tuple[PorousEligibility, tuple[PorousEligibilityReason, ...]]:
    """Decide whether the model may legitimately predict this case.

    Returns the eligibility state plus reason codes. Never extrapolates:
    any frequency outside the declared domain keeps the verdict at
    ``outside_validated_domain`` (when nothing is computable) or
    ``eligible_with_limitations`` (when only part of the band fits).
    """
    reasons: list[PorousEligibilityReason] = []

    # --- material class check -------------------------------------------
    # A declared material class that conflicts with the model's supported
    # classes is a veto; an *undeclared* one is merely unverifiable.
    if model.material_classes and material_class != 'unknown':
        if material_class not in model.material_classes:
            return 'incompatible_material', ('MATERIAL_CLASS_INCOMPATIBLE',)

    # --- implemented-family gate ----------------------------------------
    if not model.compute_capable:
        return 'unsupported', ('MODEL_NOT_IMPLEMENTED',)

    # --- required-parameter gate ----------------------------------------
    by_quantity: dict[str, PorousParameterEvidence] = {}
    for param in parameters:
        by_quantity[param.quantity] = param
    missing = [q for q in model.required_parameters if q not in by_quantity]
    if missing:
        reasons.append('REQUIRED_PARAMETER_MISSING')
        return 'missing_parameters', tuple(reasons)

    if any(
        p.evidence_class == 'unknown' for p in parameters
    ):
        reasons.append('PARAMETER_WITHOUT_EVIDENCE')
    if any(
        p.evidence_class == 'inverse_estimated' for p in parameters
    ):
        reasons.append('INVERSE_ESTIMATED_PARAMETERS')
    if any(
        p.evidence_class in ('user_assumed', 'literature_assumed')
        for p in parameters
    ):
        reasons.append('ASSUMED_PARAMETERS')

    # --- isotropy gate ---------------------------------------------------
    if buildup is not None and model.requires_isotropy:
        if buildup.anisotropy in (
            'declared_anisotropic',
            'directional_available',
        ):
            # An isotropic model applied to material declared (or
            # characterized) anisotropic is a wrong-physics claim —
            # fail closed, never compute.
            reasons.append('ANISOTROPY_UNSUPPORTED')
            return 'unsupported', tuple(dict.fromkeys(reasons))
        if buildup.anisotropy == 'anisotropy_unknown':
            reasons.append('ANISOTROPY_UNKNOWN_ASSUMED_ISOTROPIC')

    # --- facing / backing gates -----------------------------------------
    if buildup is not None:
        for layer in buildup.layers:
            facing = layer.facing
            if facing is None or facing.kind == 'none':
                continue
            if facing.kind == 'unknown' or facing.support in (
                'unknown',
                'unsupported',
            ):
                reasons.append('FACING_UNSUPPORTED')
                break
        if buildup.backing == 'unknown':
            reasons.append('BACKING_UNSUPPORTED')
        elif buildup.backing == 'finite_absorbing':
            # A finite absorbing backing needs a declared load impedance —
            # without one it cannot be honestly modelled.
            reasons.append('BACKING_UNSUPPORTED')
        if len(buildup.layers) > 1:
            reasons.append('MULTI_LAYER_UNSUPPORTED')

    # --- domain gates -----------------------------------------------------
    validity = model.validity
    if validity is not None and frequencies_hz:
        sigma = by_quantity.get('airflow_resistivity')
        inside_any = False
        all_inside = True
        for freq in frequencies_hz:
            inside = True
            if validity.frequency_min_hz is not None:
                inside &= freq >= validity.frequency_min_hz
            if validity.frequency_max_hz is not None:
                inside &= freq <= validity.frequency_max_hz
            if (
                validity.dimensionless_min is not None
                or validity.dimensionless_max is not None
            ):
                if sigma is None:
                    inside = False
                else:
                    x = AIR_DENSITY_KG_M3 * freq / sigma.value
                    if validity.dimensionless_min is not None:
                        inside &= x >= validity.dimensionless_min
                    if validity.dimensionless_max is not None:
                        inside &= x <= validity.dimensionless_max
            inside_any = inside_any or inside
            all_inside = all_inside and inside
        if not inside_any:
            reasons.append('OUTSIDE_DIMENSIONLESS_DOMAIN')
            return 'outside_validated_domain', tuple(dict.fromkeys(reasons))
        if not all_inside:
            reasons.append('OUTSIDE_DIMENSIONLESS_DOMAIN')

    if not reasons:
        return 'eligible', ('ELIGIBLE_AS_DECLARED',)
    if 'OUTSIDE_DIMENSIONLESS_DOMAIN' in reasons:
        # part of the band is out-of-domain: the prediction keeps only
        # the in-domain bands and reports the limitation.
        return 'eligible_with_limitations', tuple(dict.fromkeys(reasons))
    return 'eligible_with_limitations', tuple(dict.fromkeys(reasons))


# ---------------------------------------------------------------------------
# Parametric computation (DB / Miki / JCA)
# ---------------------------------------------------------------------------


def _impedance_translation(
    z_c: complex, k: complex, thickness_m: float, z_load: complex
) -> complex:
    """Input impedance of a layer over a load (transmission-line form)."""
    tan_kd = _ctan(k * thickness_m)
    return z_c * (z_load + 1j * z_c * tan_kd) / (z_c + 1j * z_load * tan_kd)


def _delany_bazley_family_zk(
    model: PorousMaterialModel, sigma: float, frequency_hz: float
) -> tuple[complex, complex]:
    """Characteristic impedance + wavenumber for the DB-family power laws."""
    x = AIR_DENSITY_KG_M3 * frequency_hz / sigma
    zc = AIR_DENSITY_KG_M3 * AIR_SOUND_SPEED_M_S * (
        1.0
        + model.coefficient('zc_real_coeff') * x ** model.coefficient('zc_real_exp')
        - 1j * model.coefficient('zc_imag_coeff') * x ** model.coefficient('zc_imag_exp')
    )
    k = (2 * _cpi * frequency_hz / AIR_SOUND_SPEED_M_S) * (
        1.0
        + model.coefficient('k_real_coeff') * x ** model.coefficient('k_real_exp')
        - 1j * model.coefficient('k_imag_coeff') * x ** model.coefficient('k_imag_exp')
    )
    return complex(zc), complex(k)


def _jca_zk(parameters: dict[str, float], frequency_hz: float) -> tuple[complex, complex]:
    """Johnson-Champoux-Allard rigid-frame equivalent fluid."""
    omega = 2 * _cpi * frequency_hz
    rho0 = AIR_DENSITY_KG_M3
    eta = AIR_VISCOSITY_PA_S
    pr = AIR_PRANDTL
    gamma = AIR_SPECIFIC_HEAT_RATIO
    p0 = AIR_PRESSURE_PA

    sigma = parameters['airflow_resistivity']
    phi = parameters['porosity']
    alpha_inf = parameters['tortuosity']
    lam = parameters['viscous_characteristic_length']
    lam_t = parameters['thermal_characteristic_length']

    g_j = _csqrt(
        1
        + 1j * (4 * alpha_inf**2 * eta * rho0 * omega)
        / (sigma**2 * lam**2 * phi**2)
    )
    rho_tilde = (rho0 * alpha_inf / phi) * (
        1 + (sigma * phi / (1j * omega * rho0 * alpha_inf)) * g_j
    )
    inner = 1 + (8 * eta / (1j * omega * lam_t**2 * pr * rho0)) * _csqrt(
        1 + 1j * rho0 * omega * lam_t**2 * pr / (16 * eta)
    )
    k_tilde = gamma * p0 / (phi * (gamma - (gamma - 1) / inner))

    zc = _csqrt(rho_tilde * k_tilde)
    k = omega * _csqrt(rho_tilde / k_tilde)
    return complex(zc), complex(k)


def _layer_zk(
    model: PorousMaterialModel,
    parameters: dict[str, float],
    frequency_hz: float,
) -> tuple[complex, complex]:
    if model.family in (
        'delany_bazley',
        'miki_empirical',
        'delany_bazley_lf_corrected',
    ):
        return _delany_bazley_family_zk(
            model, parameters['airflow_resistivity'], frequency_hz
        )
    if model.family == 'johnson_champoux_allard':
        return _jca_zk(parameters, frequency_hz)
    raise ValueError(f'model family {model.family} is not computable')


def predict_porous_boundary(
    *,
    document_id: str,
    model: PorousMaterialModel,
    parameters: Sequence[PorousParameterEvidence],
    buildup: PorousBuildUp,
    frequencies_hz: Sequence[float],
    computed_at_utc: str,
    material_class: PorousMaterialClass = 'unknown',
) -> PorousBoundaryPrediction:
    """Compute a sealed parametric surface-impedance/absorption table.

    The gate runs first — non-eligible cases still produce a sealed
    record (with the eligibility state) but with no computed bands; bands
    outside the declared domain land in ``excluded_bands_hz`` and are
    never interpolated.
    """
    for freq in frequencies_hz:
        if not isfinite(float(freq)) or freq <= 0.0:
            raise ValueError('frequencies must be positive finite')
    frequencies = tuple(sorted(set(float(f) for f in frequencies_hz)))

    eligibility, reasons = evaluate_porous_model_eligibility(
        model=model,
        parameters=parameters,
        buildup=buildup,
        frequencies_hz=frequencies,
        material_class=material_class,
    )

    by_quantity = {p.quantity: p for p in parameters}
    param_evidence_ids = tuple(
        p.evidence_id for p in parameters if p.evidence_class != 'unknown'
    )

    computable = eligibility in ('eligible', 'eligible_with_limitations')
    bands: list[PredictedBandPoint] = []
    excluded: list[float] = []

    if computable:
        params = {
            'airflow_resistivity': by_quantity['airflow_resistivity'].value,
            'porosity': (
                by_quantity['porosity'].value if 'porosity' in by_quantity else None
            ),
            'tortuosity': (
                by_quantity['tortuosity'].value if 'tortuosity' in by_quantity else None
            ),
            'viscous_characteristic_length': (
                by_quantity['viscous_characteristic_length'].value
                if 'viscous_characteristic_length' in by_quantity
                else None
            ),
            'thermal_characteristic_length': (
                by_quantity['thermal_characteristic_length'].value
                if 'thermal_characteristic_length' in by_quantity
                else None
            ),
        }
        sigma = params['airflow_resistivity']
        validity = model.validity
        for freq in frequencies:
            in_domain = True
            if validity is not None:
                if validity.frequency_min_hz is not None:
                    in_domain &= freq >= validity.frequency_min_hz
                if validity.frequency_max_hz is not None:
                    in_domain &= freq <= validity.frequency_max_hz
                if (
                    validity.dimensionless_min is not None
                    or validity.dimensionless_max is not None
                ):
                    x = AIR_DENSITY_KG_M3 * freq / sigma
                    if validity.dimensionless_min is not None:
                        in_domain &= x >= validity.dimensionless_min
                    if validity.dimensionless_max is not None:
                        in_domain &= x <= validity.dimensionless_max
            if not in_domain:
                excluded.append(freq)
                continue

            # Walk the stack from backing toward the front face.
            backing = buildup.backing
            if backing == 'rigid':
                z_load: complex | None = None  # infinite (rigid wall)
            elif backing == 'free_air':
                z_load = AIR_DENSITY_KG_M3 * AIR_SOUND_SPEED_M_S
            else:
                # finite_absorbing/unknown backing cannot be honestly
                # modelled without a declared load impedance.
                excluded.append(freq)
                continue

            z_face = z_load
            for layer in reversed(buildup.layers):
                zc, k = _layer_zk(model, params, freq)
                layer_d = layer.thickness_mm / 1000.0
                if layer.air_gap_behind_mm:
                    gap_d = layer.air_gap_behind_mm / 1000.0
                    k0 = 2 * _cpi * freq / AIR_SOUND_SPEED_M_S
                    zc_air = AIR_DENSITY_KG_M3 * AIR_SOUND_SPEED_M_S
                    if z_face is None:
                        z_face = -1j * zc_air / _ctan(k0 * gap_d)
                    else:
                        z_face = _impedance_translation(
                            zc_air, k0, gap_d, z_face
                        )
                if z_face is None:
                    z_face = -1j * zc / _ctan(k * layer_d)
                else:
                    z_face = _impedance_translation(zc, k, layer_d, z_face)

                # facing adds series surface resistance when modelled
                facing = layer.facing
                if (
                    facing is not None
                    and facing.support == 'resistive_modelled'
                    and facing.surface_resistivity_pa_s_m is not None
                ):
                    z_face = z_face + facing.surface_resistivity_pa_s_m

            if z_face is None:
                excluded.append(freq)
                continue

            z0 = AIR_DENSITY_KG_M3 * AIR_SOUND_SPEED_M_S
            reflection = (z_face - z0) / (z_face + z0)
            alpha = 1.0 - abs(reflection) ** 2
            alpha = max(0.0, min(1.0, alpha))
            bands.append(
                PredictedBandPoint(
                    frequency_hz=freq,
                    impedance_real_pa_s_m=z_face.real,
                    impedance_imag_pa_s_m=z_face.imag,
                    absorption_alpha=alpha,
                )
            )
    else:
        excluded = list(frequencies)

    probe = PorousBoundaryPrediction.model_construct(
        **_canon(
            PorousBoundaryPrediction,
            dict(
                prediction_id='',
                document_id=document_id,
                model_id=model.model_id,
                model_sha256=model.model_sha256,
                buildup_id=buildup.buildup_id,
                buildup_sha256=buildup.buildup_sha256,
                parameter_evidence_ids=param_evidence_ids,
                evidence_class='parametric_model_prediction',
                incidence='normal',
                bands=tuple(bands),
                excluded_bands_hz=tuple(sorted(excluded)),
                eligibility=eligibility,
                eligibility_reasons=reasons,
                computed_at_utc=computed_at_utc,
                prediction_sha256='',
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return PorousBoundaryPrediction(
        **probe.model_dump(exclude={'prediction_id', 'prediction_sha256'}),
        prediction_id=f'pbp:{sha}',
        prediction_sha256=sha,
    )


def compare_prediction_to_measured(
    *,
    document_id: str,
    prediction: PorousBoundaryPrediction | None,
    measured_evidence_ref: str,
    measured_bands: Sequence[tuple[float, float]],
    fit_band_hz: Sequence[float] = (),
    holdout_band_hz: Sequence[float] = (),
    declared_tolerance: float | None = None,
    compared_at_utc: str,
    notes: str | None = None,
) -> PorousFitComparison:
    """Bind a prediction to measured evidence with honest residuals."""
    residuals: list[FitBandResidual] = []
    fit_set = set(fit_band_hz)
    for freq, measured_alpha in measured_bands:
        predicted = (
            prediction.band_at(freq) if prediction is not None else None
        )
        residuals.append(
            FitBandResidual(
                frequency_hz=freq,
                predicted_alpha=(
                    None if predicted is None else predicted.absorption_alpha
                ),
                measured_alpha=measured_alpha,
                fit_set=freq in fit_set,
            )
        )

    verdict: FitVerdict
    holdout_residuals = [
        r for r in residuals if r.frequency_hz in set(holdout_band_hz)
    ]
    if holdout_residuals and declared_tolerance is not None:
        verdict = (
            'validated_on_holdout'
            if all(
                (r.residual or 0.0) <= declared_tolerance
                for r in holdout_residuals
                if r.residual is not None
            )
            else 'residual_exceeds_declared'
        )
    elif declared_tolerance is not None:
        checked = [r for r in residuals if r.residual is not None]
        verdict = (
            'fit_only'
            if checked
            and all(r.residual <= declared_tolerance for r in checked)
            else 'residual_exceeds_declared'
        )
    else:
        verdict = 'insufficient_coverage'

    probe = PorousFitComparison.model_construct(
        **_canon(
            PorousFitComparison,
            dict(
                comparison_id='',
                document_id=document_id,
                prediction_id=(
                    None if prediction is None else prediction.prediction_id
                ),
                prediction_sha256=(
                    None
                    if prediction is None
                    else prediction.prediction_sha256
                ),
                measured_evidence_ref=measured_evidence_ref,
                residuals=tuple(residuals),
                fit_band_hz=tuple(fit_band_hz),
                holdout_band_hz=tuple(holdout_band_hz),
                declared_tolerance=declared_tolerance,
                verdict=verdict,
                compared_at_utc=compared_at_utc,
                notes=notes,
                comparison_sha256='',
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return PorousFitComparison(
        **probe.model_dump(exclude={'comparison_id', 'comparison_sha256'}),
        comparison_id=f'pfc:{sha}',
        comparison_sha256=sha,
    )


def prediction_as_boundary_evidence(
    prediction: PorousBoundaryPrediction,
    *,
    evidence_id: str,
) -> MaterialBoundaryEvidence:
    """Export a prediction into the #570 compatibility gate (#615 §9).

    The derived evidence is ``derived_conversion`` /
    ``derived_complex_model`` — the solver-compatibility decision stays
    with the material-evidence authority.
    """
    freqs = tuple(b.frequency_hz for b in prediction.bands)
    validity = (
        (min(freqs), max(freqs)) if freqs else None
    )
    return build_boundary_evidence(
        evidence_id=evidence_id,
        method_class='derived_conversion',
        quantity='surface_impedance',
        incidence='normal',
        phase='derived_complex_model',
        band_center_hz=freqs,
        frequency_validity_hz=validity,
        build_up=None,
        source_refs=(prediction.prediction_id,),
        provenance_note=(
            f'parametric prediction {prediction.model_id} '
            f'({prediction.eligibility})'
        ),
    )


def porous_buildup_as_material_buildup(
    buildup: PorousBuildUp,
) -> MaterialBuildUp:
    """Project the porous build-up into the #570 comparison shape."""
    first = buildup.layers[0]
    gap = first.air_gap_behind_mm
    return MaterialBuildUp(
        thickness_mm=first.thickness_mm,
        air_gap_mm=gap,
        backing=buildup.backing,
        orientation=buildup.orientation,
    )


__all__ = [
    'AIR_DENSITY_KG_M3',
    'AIR_PRANDTL',
    'AIR_PRESSURE_PA',
    'AIR_SOUND_SPEED_M_S',
    'AIR_SPECIFIC_HEAT_RATIO',
    'AIR_VISCOSITY_PA_S',
    'AnisotropyState',
    'BACKING_LABELS',
    'ELIGIBILITY_LABELS',
    'ELIGIBILITY_REASON_LABELS',
    'EVIDENCE_CLASS_LABELS',
    'FACING_LABELS',
    'FACING_SUPPORT_LABELS',
    'FIT_VERDICT_LABELS',
    'FitBandResidual',
    'FitVerdict',
    'IncidenceCapability',
    'MATERIAL_CLASS_LABELS',
    'METHOD_LABELS',
    'ModelCoefficient',
    'ModelValidityDomain',
    'PARAMETER_QUANTITY_LABELS',
    'POROUS_ABSORBER_AUTHORITY_VERSION',
    'PorousBacking',
    'PorousBoundaryPrediction',
    'PorousBuildUp',
    'PorousEligibility',
    'PorousEligibilityReason',
    'PorousEvidenceClass',
    'PorousFacing',
    'PorousFacingKind',
    'PorousFacingSupport',
    'PorousFitComparison',
    'PorousLayer',
    'PorousMaterialClass',
    'PorousMaterialModel',
    'PorousModelFamily',
    'PorousParameterEvidence',
    'PorousParameterMethod',
    'PorousParameterQuantity',
    'PorousParameterUnit',
    'PredictedBandPoint',
    'build_parameter_evidence',
    'build_porous_buildup',
    'build_porous_model',
    'compare_prediction_to_measured',
    'delany_bazley_model',
    'evaluate_porous_model_eligibility',
    'johnson_champoux_allard_model',
    'measured_impedance_model',
    'miki_model',
    'porous_buildup_as_material_buildup',
    'predict_porous_boundary',
    'prediction_as_boundary_evidence',
]
