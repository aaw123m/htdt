"""Material/boundary evidence compatibility gate (#570).

A material row existing in a database is never automatically solver-ready:
this module decides whether a *specific evidence artifact* may legitimately
feed a *specific solver/boundary model*, and fails closed when it cannot.

The gate composes with — never replaces — the domain authorities:

- ``cad_material_library`` (#771): per-material ``MaterialAcousticEvidence``
  datasets (quantity + incidence + method + provenance);
- ``cad_surface_scattering`` (#1032): ISO 17497-1/2 scattering vs diffusion
  semantics and ``ga_scatter_fraction`` (ISO 17497-2 diffusion already
  returns ``None`` — this module turns that into an explicit INCOMPATIBLE
  verdict);
- ``cad_installed_surface`` (#1033): in-situ measurement method taxonomy.

Standards basis (recorded verbatim for reviewers):

- ISO 10534-2:2023 — "Normal incidence absorption coefficients coming from
  impedance tube measurements are not comparable with random incidence
  absorption coefficients measured in reverberation rooms according to
  ISO 354." Its Annex E offers a *locally-reacting* diffuse-field estimate
  derived from tube data — a conversion path that must be an explicit
  artifact, never a silent cast.
- ISO 354:2003 — reverberation-room (random/diffuse-incidence) absorption;
  the standard excludes weakly damped resonators from its intended scope.
- ISO 17497-2:2012 — "The diffusion coefficient is not suitable for direct
  use as an input to current diffusion algorithms in geometric room
  acoustic models."

Literature context (issue §research): boundary-condition representation is
a major model-error source (Thydal 2021; Li 2022; Fratoni & D'Orazio 2025;
Brinkmann 2019 round robin), so conversion is an evidence-bearing model
operation — an immutable ``BoundaryConversionArtifact`` — not a data-format
cast.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Mapping, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_material_library import MaterialAcousticEvidence
from .cad_surface_scattering import (
    SurfaceScatteringEvidence,
    ga_scatter_fraction,
)
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


_SHA256 = r'^[0-9a-f]{64}$'

EVIDENCE_AUTHORITY_VERSION = 'material-boundary-evidence-1'
CONVERSION_AUTHORITY_VERSION = 'boundary-conversion-artifact-1'
COMPATIBILITY_EVALUATOR_VERSION = 'material-evidence-compatibility-1'
COMPATIBILITY_MATRIX_VERSION = 1


# ---------------------------------------------------------------------------
# Taxonomies (issue §1, §2, §4, §5, §10)
# ---------------------------------------------------------------------------

EvidenceMethodClass = Literal[
    'iso_354_reverberation_room',
    'iso_10534_2_impedance_tube',
    'iso_17497_1_scattering',
    'iso_17497_2_directional_diffusion',
    'in_situ_measurement',
    'full_complex_reflection_measurement',
    'manufacturer_declared',
    'database_reference',
    'derived_conversion',
    'inverse_estimated',
    'unknown_method',
]

BoundaryPhysicalQuantity = Literal[
    'absorption_coefficient_diffuse_field',
    'absorption_coefficient_normal_incidence',
    'complex_reflection_coefficient',
    'surface_impedance',
    'surface_admittance',
    'scattering_coefficient_random_incidence',
    'directional_diffusion_coefficient',
    'equivalent_absorption_area',
    'single_number_rating',
    'porous_model_parameters',
    'unknown_quantity',
]

EvidenceIncidence = Literal[
    'normal',
    'specific_angle',
    'angle_dependent_dataset',
    'diffuse_reverberant',
    'unknown',
]

EvidencePhase = Literal[
    'magnitude_energy_only',
    'complex_reflection',
    'complex_impedance',
    'derived_complex_model',
    'unknown_phase',
]

BoundaryConsumerKind = Literal[
    'wave_complex_boundary',
    'wave_local_reaction_boundary',
    'geometric_arbitrary_incidence',
    'geometric_scatter_fraction',
    'statistical_energy_model',
    'hybrid_lf_wave_boundary',
]

EvidenceEligibility = Literal[
    'DIRECTLY_COMPATIBLE',
    'COMPATIBLE_WITH_DECLARED_ASSUMPTIONS',
    'DERIVED_WITH_UNCERTAINTY',
    'INCOMPATIBLE',
    'INSUFFICIENT_EVIDENCE',
]

MaterialCompatibilityReason = Literal[
    'METHOD_UNKNOWN',
    'QUANTITY_UNKNOWN',
    'INCIDENCE_UNKNOWN',
    'PHASE_UNKNOWN',
    'STANDARD_REVISION_UNKNOWN',
    'NORMAL_INCIDENCE_DATA_USED_FOR_ARBITRARY_ANGLE_BOUNDARY',
    'DIFFUSE_DATA_USED_FOR_POINT_INCIDENCE_BOUNDARY',
    'ENERGY_COEFFICIENT_HAS_NO_PHASE_AUTHORITY',
    'DIFFUSION_COEFFICIENT_NOT_SCATTERING_PARAMETER',
    'SCATTERING_REQUIRED_DIFFUSION_PRESENT',
    'SINGLE_NUMBER_RATING_NOT_FREQUENCY_DEPENDENT',
    'BUILD_UP_MISMATCH',
    'RESONANT_MATERIAL_OUTSIDE_METHOD_SCOPE',
    'RESONANT_MATERIAL_CAUTION',
    'METHOD_QUANTITY_MISMATCH',
    'QUANTITY_NOT_USABLE_FOR_CONSUMER',
    'CONVERSION_ARTIFACT_MISSING',
    'CONVERSION_ARTIFACT_SOURCE_MISMATCH',
    'CONVERSION_ARTIFACT_FREQUENCY_MISMATCH',
    'DERIVED_DATA_CANNOT_BE_MEASURED_AUTHORITY',
    'DECLARED_METHOD_NOT_MEASURED',
    'FREQUENCY_RANGE_UNDECLARED',
    'COMPATIBLE_AS_DECLARED',
    'CONVERTED_VIA_ARTIFACT',
]

UncertaintyKind = Literal[
    'measurement_uncertainty',
    'inter_lab_reproducibility',
    'conversion_model_uncertainty',
    'specimen_installation_variation',
    'inverse_estimation_uncertainty',
]


#: Product-facing Japanese labels for the eligibility states (issue §11).
ELIGIBILITY_LABELS: dict[str, str] = {
    'DIRECTLY_COMPATIBLE': '直接使用可能',
    'COMPATIBLE_WITH_DECLARED_ASSUMPTIONS': '宣言仮定つきで使用可能',
    'DERIVED_WITH_UNCERTAINTY': '変換済み（不確かさあり）',
    'INCOMPATIBLE': '非互換',
    'INSUFFICIENT_EVIDENCE': '証拠不足',
}

REASON_LABELS: dict[str, str] = {
    'METHOD_UNKNOWN': '測定手法が不明',
    'QUANTITY_UNKNOWN': '物理量が不明',
    'INCIDENCE_UNKNOWN': '入射条件が不明',
    'PHASE_UNKNOWN': '位相情報が不明',
    'STANDARD_REVISION_UNKNOWN': '規格 revision が未記録',
    'NORMAL_INCIDENCE_DATA_USED_FOR_ARBITRARY_ANGLE_BOUNDARY':
        '垂直入射データを任意入射境界に流用不可（ISO 10534-2 は ISO 354 と互換なし）',
    'DIFFUSE_DATA_USED_FOR_POINT_INCIDENCE_BOUNDARY':
        '拡散場データを特定入射角境界に流用不可',
    'ENERGY_COEFFICIENT_HAS_NO_PHASE_AUTHORITY':
        'エネルギー係数には位相権威がない（波動境界には複素数が必要）',
    'DIFFUSION_COEFFICIENT_NOT_SCATTERING_PARAMETER':
        'ISO 17497-2 拡散係数は幾何散乱パラメータとして直接使用不可（規格明示）',
    'SCATTERING_REQUIRED_DIFFUSION_PRESENT':
        '散乱係数が必要な箇所に拡散係数が供給された',
    'SINGLE_NUMBER_RATING_NOT_FREQUENCY_DEPENDENT':
        '単一数値格付（αw/NRC/SAA）は周波数依存入力の代替にならない',
    'BUILD_UP_MISMATCH': '材料の構築状態（厚さ/空隙/背裏など）が一致しない',
    'RESONANT_MATERIAL_OUTSIDE_METHOD_SCOPE':
        '共振型材料は ISO 354 の適用範囲外（弱減衰共振器の除外条項）',
    'RESONANT_MATERIAL_CAUTION': '共振型材料 — 手法適用範囲に注意',
    'METHOD_QUANTITY_MISMATCH': '測定手法がその物理量を産出しない',
    'QUANTITY_NOT_USABLE_FOR_CONSUMER': 'この物理量はその境界モデルに使えない',
    'CONVERSION_ARTIFACT_MISSING': '変換アーティファクトが必要',
    'CONVERSION_ARTIFACT_SOURCE_MISMATCH': '変換元証拠が不一致',
    'CONVERSION_ARTIFACT_FREQUENCY_MISMATCH': '変換の周波数適用範囲が不一致',
    'DERIVED_DATA_CANNOT_BE_MEASURED_AUTHORITY': '導出データを実測権威として扱えない',
    'DECLARED_METHOD_NOT_MEASURED': '宣言手法は実測ではない',
    'FREQUENCY_RANGE_UNDECLARED': '周波数適用範囲が未宣言',
    'COMPATIBLE_AS_DECLARED': '宣言どおり使用可能',
    'CONVERTED_VIA_ARTIFACT': '変換アーティファクト経由で使用可能',
}


# ---------------------------------------------------------------------------
# Build-up identity (issue §9) — evidence applies to the exact tested state.
# ---------------------------------------------------------------------------


class MaterialBuildUp(BaseModel):
    """Exact tested construction state — two variants are distinct evidence."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    product: str | None = Field(default=None, min_length=1)
    thickness_mm: float | None = Field(default=None, gt=0.0)
    density_kg_m3: float | None = Field(default=None, gt=0.0)
    flow_resistivity_pa_s_m2: float | None = Field(default=None, gt=0.0)
    mounting: str | None = Field(default=None, min_length=1)
    air_gap_mm: float | None = Field(default=None, ge=0.0)
    backing: str | None = Field(default=None, min_length=1)
    perforation_open_area_percent: float | None = Field(
        default=None, ge=0.0, le=100.0
    )
    specimen_dimensions: str | None = Field(default=None, min_length=1)
    orientation: str | None = Field(default=None, min_length=1)
    substrate: str | None = Field(default=None, min_length=1)
    environmental_state: str | None = Field(default=None, min_length=1)
    # ISO 354 explicitly excludes weakly damped resonators (Helmholtz /
    # membrane / panel) from its absorption-characterization scope.
    is_resonant: bool = False

    @model_validator(mode='after')
    def finite(self) -> 'MaterialBuildUp':
        for value in (
            self.thickness_mm,
            self.density_kg_m3,
            self.flow_resistivity_pa_s_m2,
            self.air_gap_mm,
            self.perforation_open_area_percent,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('build-up numeric fields must be finite')
        return self


# Fields that must agree for "same tested construction state" comparisons.
_BUILD_UP_COMPARE_FIELDS: tuple[str, ...] = (
    'product',
    'thickness_mm',
    'density_kg_m3',
    'flow_resistivity_pa_s_m2',
    'mounting',
    'air_gap_mm',
    'backing',
    'perforation_open_area_percent',
    'specimen_dimensions',
    'orientation',
    'substrate',
)


def build_up_matches(a: MaterialBuildUp, b: MaterialBuildUp) -> bool:
    """Exact-identity comparison — unknown fields never silently match.

    A field absent on *either* side is a mismatch (the evidence cannot attest
    the consumer's required state).
    """

    for field in _BUILD_UP_COMPARE_FIELDS:
        va, vb = getattr(a, field), getattr(b, field)
        if va is None and vb is None:
            continue
        if va is None or vb is None:
            return False
        if isinstance(va, float) or isinstance(vb, float):
            if not isfinite(float(va)) or not isfinite(float(vb)):
                return False
            if abs(float(va) - float(vb)) > 1e-9:
                return False
        elif va != vb:
            return False
    return True


# ---------------------------------------------------------------------------
# Material boundary evidence record (issue §1–§5, §9, §10)
# ---------------------------------------------------------------------------


class EvidenceUncertaintyContribution(BaseModel):
    """One declared uncertainty contribution (issue §10).

    ``value`` may be absent — where no uncertainty information exists the
    record stays explicit instead of fabricating an interval.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: UncertaintyKind
    value: float | None = Field(default=None, ge=0.0)
    unit: str | None = Field(default=None, min_length=1)
    note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def honest(self) -> 'EvidenceUncertaintyContribution':
        if self.value is not None and not isfinite(float(self.value)):
            raise ValueError('uncertainty values must be finite')
        if self.value is None and self.unit is not None:
            raise ValueError('a unit without a value is meaningless')
        if self.value is None and not self.note:
            raise ValueError(
                'an uncertainty contribution needs a value or an explicit note'
            )
        return self


#: Which quantity each measurement method can honestly produce
#: (issue §1/§2: the method decides the quantity, not the marketing name).
METHOD_PRODUCIBLE_QUANTITIES: dict[str, frozenset[str]] = {
    'iso_354_reverberation_room': frozenset(
        {'absorption_coefficient_diffuse_field', 'equivalent_absorption_area'}
    ),
    'iso_10534_2_impedance_tube': frozenset(
        {
            'absorption_coefficient_normal_incidence',
            'surface_impedance',
            'surface_admittance',
            'porous_model_parameters',
        }
    ),
    'iso_17497_1_scattering': frozenset(
        {'scattering_coefficient_random_incidence'}
    ),
    'iso_17497_2_directional_diffusion': frozenset(
        {'directional_diffusion_coefficient'}
    ),
    'in_situ_measurement': frozenset(
        {
            'absorption_coefficient_normal_incidence',
            'surface_impedance',
            'surface_admittance',
            'complex_reflection_coefficient',
        }
    ),
    'full_complex_reflection_measurement': frozenset(
        {'complex_reflection_coefficient'}
    ),
    # Declared/derived/inverse sources may carry any quantity — their claim
    # is bounded by eligibility rules, not method honesty.
    'manufacturer_declared': frozenset(
        {
            'absorption_coefficient_diffuse_field',
            'absorption_coefficient_normal_incidence',
            'scattering_coefficient_random_incidence',
            'directional_diffusion_coefficient',
            'equivalent_absorption_area',
            'single_number_rating',
        }
    ),
    'database_reference': frozenset(
        {
            'absorption_coefficient_diffuse_field',
            'absorption_coefficient_normal_incidence',
            'scattering_coefficient_random_incidence',
            'equivalent_absorption_area',
            'single_number_rating',
            'porous_model_parameters',
        }
    ),
    'derived_conversion': frozenset(
        {
            'absorption_coefficient_diffuse_field',
            'surface_impedance',
            'surface_admittance',
            'complex_reflection_coefficient',
        }
    ),
    'inverse_estimated': frozenset(
        {
            'absorption_coefficient_diffuse_field',
            'absorption_coefficient_normal_incidence',
            'surface_impedance',
            'surface_admittance',
            'complex_reflection_coefficient',
        }
    ),
    'unknown_method': frozenset(),
}


class MaterialBoundaryEvidence(BaseModel):
    """Sealed evidence record the compatibility gate evaluates (issue §1).

    Pins *what was actually measured/derived*: method class + standard
    revision, physical quantity, incidence and phase semantics, frequency
    grid, exact build-up identity, and declared uncertainty contributions.
    ``source_refs`` bind back to the upstream authorities
    (``MaterialAcousticEvidence``, ``SurfaceScatteringEvidence``,
    ``InstalledSurfaceAcousticMeasurement``…) that carry the actual values —
    this record never replaces them.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['material-boundary-evidence-1'] = (
        EVIDENCE_AUTHORITY_VERSION
    )
    evidence_id: str = Field(min_length=1)
    method_class: EvidenceMethodClass
    # Exact standard revision where a standard method is claimed
    # (e.g. 'ISO 10534-2:2023'); ``None`` with a standard method yields
    # STANDARD_REVISION_UNKNOWN downstream.
    standard_revision: str | None = Field(default=None, min_length=1)
    laboratory: str | None = Field(default=None, min_length=1)
    source_report: str | None = Field(default=None, min_length=1)
    quantity: BoundaryPhysicalQuantity
    incidence: EvidenceIncidence = 'unknown'
    incidence_angle_deg: float | None = Field(default=None, ge=0.0, le=90.0)
    phase: EvidencePhase = 'unknown_phase'
    band_center_hz: tuple[float, ...] = ()
    frequency_validity_hz: tuple[float, float] | None = None
    build_up: MaterialBuildUp | None = None
    uncertainty: tuple[EvidenceUncertaintyContribution, ...] = ()
    source_refs: tuple[str, ...] = ()
    provenance_note: str | None = Field(default=None, min_length=1)
    evidence_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_evidence(self) -> 'MaterialBoundaryEvidence':
        if self.incidence == 'specific_angle':
            if self.incidence_angle_deg is None:
                raise ValueError('specific_angle evidence requires incidence_angle_deg')
        elif self.incidence_angle_deg is not None:
            raise ValueError(
                'incidence_angle_deg is only meaningful for specific_angle evidence'
            )
        if self.incidence_angle_deg is not None and not isfinite(
            float(self.incidence_angle_deg)
        ):
            raise ValueError('incidence angle must be finite')
        if self.band_center_hz:
            if any(not isfinite(f) or f <= 0.0 for f in self.band_center_hz):
                raise ValueError('band centers must be positive and finite')
            if list(self.band_center_hz) != sorted(self.band_center_hz):
                raise ValueError('band centers must be sorted')
        if self.frequency_validity_hz is not None:
            lo, hi = self.frequency_validity_hz
            if not (isfinite(lo) and isfinite(hi)) or not (0.0 < lo <= hi):
                raise ValueError('frequency validity range must be positive/ordered')
        producible = METHOD_PRODUCIBLE_QUANTITIES.get(self.method_class, frozenset())
        if (
            self.quantity != 'unknown_quantity'
            and producible
            and self.quantity not in producible
        ):
            # Method/quantity honesty: ISO 354 cannot produce a normal-
            # incidence coefficient; ISO 17497-2 cannot produce scattering.
            raise ValueError(
                f'{self.method_class} cannot produce {self.quantity}'
            )
        if self.method_class in (
            'iso_354_reverberation_room',
            'iso_17497_1_scattering',
            'iso_17497_2_directional_diffusion',
        ):
            if self.phase not in ('magnitude_energy_only', 'unknown_phase'):
                # These standards output scalar energy coefficients.
                raise ValueError(
                    f'{self.method_class} evidence cannot claim complex phase'
                )
        if self.method_class == 'iso_354_reverberation_room' and self.incidence not in (
            'diffuse_reverberant',
            'unknown',
        ):
            raise ValueError(
                'ISO 354 evidence is diffuse/reverberant by construction'
            )
        if self.method_class == 'iso_10534_2_impedance_tube' and self.incidence not in (
            'normal',
            'unknown',
        ):
            raise ValueError('ISO 10534-2 evidence is normal-incidence')
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError('source refs must be unique')
        uncertainty_kinds = [item.kind for item in self.uncertainty]
        if len(uncertainty_kinds) != len(set(uncertainty_kinds)):
            raise ValueError('uncertainty kinds must be unique')
        if self.evidence_sha256 != _hash(self.identity_payload()):
            raise ValueError('material boundary evidence hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'evidence_id': self.evidence_id,
            'method_class': self.method_class,
            'standard_revision': self.standard_revision,
            'laboratory': self.laboratory,
            'source_report': self.source_report,
            'quantity': self.quantity,
            'incidence': self.incidence,
            'incidence_angle_deg': self.incidence_angle_deg,
            'phase': self.phase,
            'band_center_hz': list(self.band_center_hz),
            'frequency_validity_hz': (
                None if self.frequency_validity_hz is None
                else list(self.frequency_validity_hz)
            ),
            'build_up': (
                None if self.build_up is None
                else self.build_up.model_dump(mode='json')
            ),
            'uncertainty': [
                item.model_dump(mode='json') for item in self.uncertainty
            ],
            'source_refs': list(self.source_refs),
            'provenance_note': self.provenance_note,
        }


def build_boundary_evidence(
    *,
    evidence_id: str,
    method_class: EvidenceMethodClass,
    quantity: BoundaryPhysicalQuantity,
    standard_revision: str | None = None,
    laboratory: str | None = None,
    source_report: str | None = None,
    incidence: EvidenceIncidence = 'unknown',
    incidence_angle_deg: float | None = None,
    phase: EvidencePhase = 'unknown_phase',
    band_center_hz: Sequence[float] = (),
    frequency_validity_hz: tuple[float, float] | None = None,
    build_up: MaterialBuildUp | None = None,
    uncertainty: Sequence[EvidenceUncertaintyContribution | Mapping[str, Any]] = (),
    source_refs: Sequence[str] = (),
    provenance_note: str | None = None,
) -> MaterialBoundaryEvidence:
    contributions = tuple(
        item if isinstance(item, EvidenceUncertaintyContribution)
        else EvidenceUncertaintyContribution.model_validate(item)
        for item in uncertainty
    )
    payload: dict[str, Any] = {
        'evidence_id': evidence_id,
        'method_class': method_class,
        'standard_revision': standard_revision,
        'laboratory': laboratory,
        'source_report': source_report,
        'quantity': quantity,
        'incidence': incidence,
        'incidence_angle_deg': incidence_angle_deg,
        'phase': phase,
        'band_center_hz': tuple(band_center_hz),
        'frequency_validity_hz': frequency_validity_hz,
        'build_up': build_up,
        'uncertainty': contributions,
        'source_refs': tuple(source_refs),
        'provenance_note': provenance_note,
    }
    provisional = MaterialBoundaryEvidence.model_construct(
        **canonicalize_payload(
            MaterialBoundaryEvidence, dict(**payload, evidence_sha256='0' * 64)
        )
    )
    return MaterialBoundaryEvidence(
        **payload, evidence_sha256=_hash(provisional.identity_payload())
    )


# ---------------------------------------------------------------------------
# Conversion artifact (issue §6) — immutable derived boundary evidence.
# ---------------------------------------------------------------------------


class BoundaryConversionArtifact(BaseModel):
    """One immutable evidence-type conversion (issue §6).

    Converting e.g. ISO 354 diffuse absorption into a complex impedance
    boundary model is an evidence-bearing model operation: the artifact pins
    the source evidence by id+hash, the conversion model/algorithm/version,
    fitting parameters, priors, assumed reaction/incidence behavior,
    frequency validity, residual fit quality and uncertainty — and never
    overwrites the source measurement.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['boundary-conversion-artifact-1'] = (
        CONVERSION_AUTHORITY_VERSION
    )
    artifact_id: str = Field(min_length=1)
    source_evidence_id: str = Field(min_length=1)
    source_evidence_sha256: str = Field(pattern=_SHA256)
    source_quantity: BoundaryPhysicalQuantity
    source_method: EvidenceMethodClass
    target_consumer: BoundaryConsumerKind
    target_representation: str = Field(min_length=1)
    conversion_model: str = Field(min_length=1)
    conversion_version: str = Field(min_length=1)
    fitting_parameters: tuple[str, ...] = ()
    priors_constraints: tuple[str, ...] = ()
    assumed_reaction: Literal['local', 'non_local', 'unspecified'] = 'unspecified'
    assumed_incidence: EvidenceIncidence = 'unknown'
    assumed_incidence_angle_deg: float | None = Field(
        default=None, ge=0.0, le=90.0
    )
    frequency_validity_hz: tuple[float, float] | None = None
    residual_fit_quality: str | None = Field(default=None, min_length=1)
    uncertainty_contributions: tuple[EvidenceUncertaintyContribution, ...] = ()
    literature_reference: str | None = Field(default=None, min_length=1)
    limitations: tuple[str, ...] = ()
    created_at_utc: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_artifact(self) -> 'BoundaryConversionArtifact':
        if self.assumed_incidence == 'specific_angle':
            if self.assumed_incidence_angle_deg is None:
                raise ValueError('specific_angle assumption requires an angle')
        elif self.assumed_incidence_angle_deg is not None:
            raise ValueError('assumed incidence angle requires specific_angle')
        if self.frequency_validity_hz is not None:
            lo, hi = self.frequency_validity_hz
            if not (isfinite(lo) and isfinite(hi)) or not (0.0 < lo <= hi):
                raise ValueError('conversion frequency validity must be ordered')
        if self.artifact_sha256 != _hash(self.identity_payload()):
            raise ValueError('boundary conversion artifact hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'artifact_id': self.artifact_id,
            'source_evidence_id': self.source_evidence_id,
            'source_evidence_sha256': self.source_evidence_sha256,
            'source_quantity': self.source_quantity,
            'source_method': self.source_method,
            'target_consumer': self.target_consumer,
            'target_representation': self.target_representation,
            'conversion_model': self.conversion_model,
            'conversion_version': self.conversion_version,
            'fitting_parameters': list(self.fitting_parameters),
            'priors_constraints': list(self.priors_constraints),
            'assumed_reaction': self.assumed_reaction,
            'assumed_incidence': self.assumed_incidence,
            'assumed_incidence_angle_deg': self.assumed_incidence_angle_deg,
            'frequency_validity_hz': (
                None if self.frequency_validity_hz is None
                else list(self.frequency_validity_hz)
            ),
            'residual_fit_quality': self.residual_fit_quality,
            'uncertainty_contributions': [
                item.model_dump(mode='json')
                for item in self.uncertainty_contributions
            ],
            'literature_reference': self.literature_reference,
            'limitations': list(self.limitations),
            'created_at_utc': self.created_at_utc,
        }


def build_conversion_artifact(
    *,
    source_evidence_id: str,
    source_evidence_sha256: str,
    source_quantity: BoundaryPhysicalQuantity,
    source_method: EvidenceMethodClass,
    target_consumer: BoundaryConsumerKind,
    target_representation: str,
    conversion_model: str,
    conversion_version: str,
    created_at_utc: str,
    fitting_parameters: Sequence[str] = (),
    priors_constraints: Sequence[str] = (),
    assumed_reaction: Literal['local', 'non_local', 'unspecified'] = 'unspecified',
    assumed_incidence: EvidenceIncidence = 'unknown',
    assumed_incidence_angle_deg: float | None = None,
    frequency_validity_hz: tuple[float, float] | None = None,
    residual_fit_quality: str | None = None,
    uncertainty_contributions: Sequence[
        EvidenceUncertaintyContribution | Mapping[str, Any]
    ] = (),
    literature_reference: str | None = None,
    limitations: Sequence[str] = (),
    artifact_id: str | None = None,
) -> BoundaryConversionArtifact:
    contributions = tuple(
        item if isinstance(item, EvidenceUncertaintyContribution)
        else EvidenceUncertaintyContribution.model_validate(item)
        for item in uncertainty_contributions
    )
    payload: dict[str, Any] = {
        'artifact_id': artifact_id or str(uuid4()),
        'source_evidence_id': source_evidence_id,
        'source_evidence_sha256': source_evidence_sha256,
        'source_quantity': source_quantity,
        'source_method': source_method,
        'target_consumer': target_consumer,
        'target_representation': target_representation,
        'conversion_model': conversion_model,
        'conversion_version': conversion_version,
        'fitting_parameters': tuple(fitting_parameters),
        'priors_constraints': tuple(priors_constraints),
        'assumed_reaction': assumed_reaction,
        'assumed_incidence': assumed_incidence,
        'assumed_incidence_angle_deg': assumed_incidence_angle_deg,
        'frequency_validity_hz': frequency_validity_hz,
        'residual_fit_quality': residual_fit_quality,
        'uncertainty_contributions': contributions,
        'literature_reference': literature_reference,
        'limitations': tuple(limitations),
        'created_at_utc': created_at_utc,
    }
    provisional = BoundaryConversionArtifact.model_construct(
        **canonicalize_payload(
            BoundaryConversionArtifact, dict(**payload, artifact_sha256='0' * 64)
        )
    )
    return BoundaryConversionArtifact(
        **payload, artifact_sha256=_hash(provisional.identity_payload())
    )


# ---------------------------------------------------------------------------
# Direct-use compatibility matrix (issue §3) — inspectable, versioned.
#
# Each rule declares which quantities a consumer may use *directly*, the
# incidence/phase semantics that use requires, and the baseline eligibility.
# Cross-cutting guards (single-number ratings, ISO 17497-2 misuse, resonant
# build-ups, method/quantity honesty) apply on top of these rules inside
# ``evaluate_material_evidence_compatibility``.
# ---------------------------------------------------------------------------


class CompatibilityRule(BaseModel):
    """One row of the versioned source-evidence -> consumer matrix."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    rule_id: str = Field(min_length=1)
    consumer: BoundaryConsumerKind
    quantities: tuple[BoundaryPhysicalQuantity, ...] = Field(min_length=1)
    required_incidence: tuple[EvidenceIncidence, ...] | None = None
    required_phase: tuple[EvidencePhase, ...] | None = None
    base_eligibility: EvidenceEligibility
    rationale: str = Field(min_length=8)


#: The versioned matrix. Bump COMPATIBILITY_MATRIX_VERSION on any change.
COMPATIBILITY_RULES_V1: tuple[CompatibilityRule, ...] = (
    CompatibilityRule(
        rule_id='wave-complex:impedance',
        consumer='wave_complex_boundary',
        quantities=('surface_impedance', 'surface_admittance'),
        required_phase=('complex_impedance',),
        base_eligibility='DIRECTLY_COMPATIBLE',
        rationale='A measured complex impedance/admittance is exactly the '
        'boundary condition a wave solver consumes (e.g. ISO 10534-2 '
        'surface impedance within its normal-incidence scope).',
    ),
    CompatibilityRule(
        rule_id='wave-complex:reflection',
        consumer='wave_complex_boundary',
        quantities=('complex_reflection_coefficient',),
        required_phase=('complex_reflection', 'complex_impedance'),
        base_eligibility='DIRECTLY_COMPATIBLE',
        rationale='A full complex reflection measurement carries phase '
        'authority and converts to an impedance boundary algebraically.',
    ),
    CompatibilityRule(
        rule_id='wave-complex:derived',
        consumer='wave_complex_boundary',
        quantities=(
            'surface_impedance',
            'surface_admittance',
            'complex_reflection_coefficient',
            'absorption_coefficient_diffuse_field',
            'absorption_coefficient_normal_incidence',
        ),
        required_phase=('derived_complex_model',),
        base_eligibility='DERIVED_WITH_UNCERTAINTY',
        rationale='Phase data derived by a declared conversion model is '
        'usable only as derived evidence — never re-labeled as measured '
        '(issue #570 §5/§6).',
    ),
    CompatibilityRule(
        rule_id='wave-local:absorption',
        consumer='wave_local_reaction_boundary',
        quantities=(
            'absorption_coefficient_diffuse_field',
            'absorption_coefficient_normal_incidence',
            'surface_impedance',
        ),
        base_eligibility='COMPATIBLE_WITH_DECLARED_ASSUMPTIONS',
        rationale='A local-reaction assumption may reduce magnitude data to '
        'an absorptive boundary, but the assumption is part of prediction '
        'identity and must be declared.',
    ),
    CompatibilityRule(
        rule_id='ga-arbitrary:diffuse-absorption',
        consumer='geometric_arbitrary_incidence',
        quantities=('absorption_coefficient_diffuse_field',),
        required_incidence=('diffuse_reverberant', 'angle_dependent_dataset'),
        base_eligibility='COMPATIBLE_WITH_DECLARED_ASSUMPTIONS',
        rationale='ISO 354 random-incidence absorption is the conventional '
        'GA absorption input; the diffuse-field model assumption is '
        'declared, not implied.',
    ),
    CompatibilityRule(
        rule_id='ga-arbitrary:angle-dataset',
        consumer='geometric_arbitrary_incidence',
        quantities=(
            'absorption_coefficient_normal_incidence',
            'complex_reflection_coefficient',
            'surface_impedance',
        ),
        required_incidence=('angle_dependent_dataset',),
        base_eligibility='COMPATIBLE_WITH_DECLARED_ASSUMPTIONS',
        rationale='Only angle-resolved data may feed an arbitrary-incidence '
        'boundary directly; per-angle use stays inside the measured angles.',
    ),
    CompatibilityRule(
        rule_id='ga-scatter:random-incidence',
        consumer='geometric_scatter_fraction',
        quantities=('scattering_coefficient_random_incidence',),
        required_incidence=('diffuse_reverberant', 'unknown'),
        base_eligibility='DIRECTLY_COMPATIBLE',
        rationale='ISO 17497-1 random-incidence scattering is the only '
        'scalar GA scatter fraction (#1032).',
    ),
    CompatibilityRule(
        rule_id='statistical:diffuse-absorption',
        consumer='statistical_energy_model',
        quantities=(
            'absorption_coefficient_diffuse_field',
            'equivalent_absorption_area',
        ),
        required_incidence=('diffuse_reverberant',),
        base_eligibility='COMPATIBLE_WITH_DECLARED_ASSUMPTIONS',
        rationale='Sabine-family energy models consume diffuse-field '
        'absorption (ISO 354) or equivalent absorption area directly under '
        'the diffuse-field assumption.',
    ),
    CompatibilityRule(
        rule_id='hybrid-lf:impedance',
        consumer='hybrid_lf_wave_boundary',
        quantities=('surface_impedance', 'surface_admittance'),
        required_phase=('complex_impedance', 'derived_complex_model'),
        base_eligibility='COMPATIBLE_WITH_DECLARED_ASSUMPTIONS',
        rationale='Hybrid LF wave boundaries need complex boundary data; '
        'measured complex impedance is directly usable, derived data only '
        'as DERIVED_WITH_UNCERTAINTY.',
    ),
)


def list_compatibility_matrix() -> dict[str, Any]:
    """Inspectable, versioned dump of the direct-use matrix (issue §3)."""

    return {
        'matrix_version': COMPATIBILITY_MATRIX_VERSION,
        'rules': [rule.model_dump(mode='json') for rule in COMPATIBILITY_RULES_V1],
    }


# ---------------------------------------------------------------------------
# The gate (issue §11)
# ---------------------------------------------------------------------------


class MaterialEvidenceCompatibility(BaseModel):
    """One solver-facing eligibility decision with reasons (issue §11)."""

    model_config = ConfigDict(frozen=True)

    evaluator_version: Literal['material-evidence-compatibility-1'] = (
        COMPATIBILITY_EVALUATOR_VERSION
    )
    matrix_version: int = COMPATIBILITY_MATRIX_VERSION
    evidence_id: str = Field(min_length=1)
    consumer: BoundaryConsumerKind
    eligibility: EvidenceEligibility
    reasons: tuple[MaterialCompatibilityReason, ...] = ()
    rule_id: str | None = None
    conversion_artifact_id: str | None = None
    note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_decision(self) -> 'MaterialEvidenceCompatibility':
        if len(self.reasons) != len(set(self.reasons)):
            raise ValueError('reason codes must be unique')
        if self.eligibility == 'DIRECTLY_COMPATIBLE' and self.reasons and (
            set(self.reasons) != {'COMPATIBLE_AS_DECLARED'}
        ):
            raise ValueError('directly compatible decisions carry no failure reasons')
        if self.eligibility == 'DERIVED_WITH_UNCERTAINTY' and (
            self.conversion_artifact_id is None
        ):
            raise ValueError(
                'DERIVED_WITH_UNCERTAINTY requires a bound conversion artifact'
            )
        return self

    def render_text(self) -> str:
        reasons = '、'.join(REASON_LABELS[r] for r in self.reasons) or '—'
        return (
            f'{ELIGIBILITY_LABELS[self.eligibility]} '
            f'({self.consumer}): {reasons}'
        )


_STANDARD_METHODS: frozenset[str] = frozenset(
    {
        'iso_354_reverberation_room',
        'iso_10534_2_impedance_tube',
        'iso_17497_1_scattering',
        'iso_17497_2_directional_diffusion',
    }
)

_PHASE_CONSUMERS: frozenset[str] = frozenset(
    {'wave_complex_boundary', 'hybrid_lf_wave_boundary'}
)


def evaluate_material_evidence_compatibility(
    evidence: MaterialBoundaryEvidence,
    consumer: BoundaryConsumerKind,
    *,
    required_build_up: MaterialBuildUp | None = None,
    conversion: BoundaryConversionArtifact | None = None,
    required_incidence_angle_deg: float | None = None,
    required_frequency_hz: float | None = None,
) -> MaterialEvidenceCompatibility:
    """Decide whether ``evidence`` may feed ``consumer`` — fail closed.

    Returns a ``MaterialEvidenceCompatibility`` decision with explicit reason
    codes; never raises for an incompatible combination and never silently
    substitutes a generic wall preset for an incompatible requested
    material.
    """

    if conversion is not None:
        if (
            conversion.source_evidence_id != evidence.evidence_id
            or conversion.source_evidence_sha256 != evidence.evidence_sha256
        ):
            return _decision(
                evidence, consumer, 'INCOMPATIBLE',
                ('CONVERSION_ARTIFACT_SOURCE_MISMATCH',),
            )
        if conversion.target_consumer != consumer:
            return _decision(
                evidence, consumer, 'INCOMPATIBLE',
                ('CONVERSION_ARTIFACT_SOURCE_MISMATCH',),
                note='conversion artifact targets a different consumer',
            )
        if (
            required_frequency_hz is not None
            and conversion.frequency_validity_hz is not None
            and not (
                conversion.frequency_validity_hz[0]
                <= required_frequency_hz
                <= conversion.frequency_validity_hz[1]
            )
        ):
            return _decision(
                evidence, consumer, 'INCOMPATIBLE',
                ('CONVERSION_ARTIFACT_FREQUENCY_MISMATCH',),
            )

    reasons: list[MaterialCompatibilityReason] = []

    # --- Fail-closed unknowns ------------------------------------------------
    if evidence.method_class == 'unknown_method':
        reasons.append('METHOD_UNKNOWN')
    if evidence.quantity == 'unknown_quantity':
        reasons.append('QUANTITY_UNKNOWN')
    if evidence.incidence == 'unknown':
        reasons.append('INCIDENCE_UNKNOWN')
    if evidence.phase == 'unknown_phase':
        reasons.append('PHASE_UNKNOWN')
    if (
        evidence.method_class in _STANDARD_METHODS
        and evidence.standard_revision is None
    ):
        reasons.append('STANDARD_REVISION_UNKNOWN')
    if not evidence.band_center_hz and evidence.frequency_validity_hz is None:
        reasons.append('FREQUENCY_RANGE_UNDECLARED')

    # --- Absolute guards ------------------------------------------------------
    if evidence.quantity == 'single_number_rating':
        reasons.append('SINGLE_NUMBER_RATING_NOT_FREQUENCY_DEPENDENT')
        return _decision(evidence, consumer, 'INCOMPATIBLE', reasons)

    if evidence.quantity == 'directional_diffusion_coefficient':
        if consumer == 'geometric_scatter_fraction':
            # ISO 17497-2 verbatim guardrail — compose with #1032:
            # ga_scatter_fraction() already refuses this quantity.
            reasons.append('DIFFUSION_COEFFICIENT_NOT_SCATTERING_PARAMETER')
            return _decision(evidence, consumer, 'INCOMPATIBLE', reasons)

    if (
        consumer == 'geometric_scatter_fraction'
        and evidence.quantity != 'scattering_coefficient_random_incidence'
        and evidence.quantity != 'directional_diffusion_coefficient'
    ):
        reasons.append('SCATTERING_REQUIRED_DIFFUSION_PRESENT')
        return _decision(evidence, consumer, 'INCOMPATIBLE', reasons)

    # Resonant build-up + ISO 354 scope exclusion (issue §7).
    if evidence.build_up is not None and evidence.build_up.is_resonant:
        if evidence.method_class == 'iso_354_reverberation_room':
            if consumer in (
                'wave_complex_boundary',
                'wave_local_reaction_boundary',
                'geometric_arbitrary_incidence',
                'hybrid_lf_wave_boundary',
            ):
                reasons.append('RESONANT_MATERIAL_OUTSIDE_METHOD_SCOPE')
                return _decision(evidence, consumer, 'INCOMPATIBLE', reasons)
            reasons.append('RESONANT_MATERIAL_CAUTION')

    # Build-up identity: an exact required construction state must match.
    if required_build_up is not None:
        if evidence.build_up is None or not build_up_matches(
            evidence.build_up, required_build_up
        ):
            reasons.append('BUILD_UP_MISMATCH')
            return _decision(evidence, consumer, 'INCOMPATIBLE', reasons)

    # --- Phase authority (issue §5) -------------------------------------------
    if consumer in _PHASE_CONSUMERS:
        if evidence.phase == 'unknown_phase':
            reasons.append('PHASE_UNKNOWN')
        elif evidence.phase == 'magnitude_energy_only':
            if conversion is not None and conversion.target_consumer == consumer:
                pass  # derived path handled below
            else:
                reasons.append('ENERGY_COEFFICIENT_HAS_NO_PHASE_AUTHORITY')
                return _decision(evidence, consumer, 'INCOMPATIBLE', reasons)

    # --- Incidence semantics (issue §4) ---------------------------------------
    incidence_fail = False
    if consumer == 'geometric_arbitrary_incidence':
        if evidence.incidence == 'normal':
            # ISO 10534-2 αn is not comparable to ISO 354 αs (standard text).
            if conversion is None:
                reasons.append(
                    'NORMAL_INCIDENCE_DATA_USED_FOR_ARBITRARY_ANGLE_BOUNDARY'
                )
                incidence_fail = True
        elif evidence.incidence == 'specific_angle':
            if (
                required_incidence_angle_deg is None
                or evidence.incidence_angle_deg is None
                or abs(
                    float(evidence.incidence_angle_deg)
                    - float(required_incidence_angle_deg)
                ) > 1e-9
            ):
                reasons.append(
                    'NORMAL_INCIDENCE_DATA_USED_FOR_ARBITRARY_ANGLE_BOUNDARY'
                )
                incidence_fail = True
        elif evidence.incidence == 'unknown':
            pass  # already counted above
    elif consumer in ('wave_complex_boundary', 'wave_local_reaction_boundary',
                      'hybrid_lf_wave_boundary'):
        # Wave boundaries are evaluated at a specific incidence condition;
        # diffuse-only data is not a point-incidence boundary truth.
        if evidence.incidence == 'diffuse_reverberant' and (
            conversion is None
            and evidence.phase != 'derived_complex_model'
        ):
            reasons.append('DIFFUSE_DATA_USED_FOR_POINT_INCIDENCE_BOUNDARY')
            incidence_fail = True

    if incidence_fail:
        return _decision(evidence, consumer, 'INCOMPATIBLE', reasons)

    # --- Matrix lookup ---------------------------------------------------------
    rule = _select_rule(consumer, evidence.quantity)
    if rule is None:
        reasons.append('QUANTITY_NOT_USABLE_FOR_CONSUMER')
        return _decision(evidence, consumer, 'INCOMPATIBLE', reasons)

    if rule.required_incidence is not None and (
        evidence.incidence not in rule.required_incidence
    ):
        if evidence.incidence == 'unknown':
            reasons.append('INCIDENCE_UNKNOWN')
            return _decision(evidence, consumer, 'INSUFFICIENT_EVIDENCE', reasons, rule)
        reasons.append('NORMAL_INCIDENCE_DATA_USED_FOR_ARBITRARY_ANGLE_BOUNDARY'
                       if evidence.incidence == 'normal'
                       else 'QUANTITY_NOT_USABLE_FOR_CONSUMER')
        return _decision(evidence, consumer, 'INCOMPATIBLE', reasons, rule)

    if rule.required_phase is not None and (
        evidence.phase not in rule.required_phase
    ):
        if evidence.phase == 'derived_complex_model':
            if conversion is None:
                reasons.append('CONVERSION_ARTIFACT_MISSING')
                return _decision(evidence, consumer, 'INSUFFICIENT_EVIDENCE', reasons, rule)
        elif evidence.phase == 'unknown_phase':
            return _decision(evidence, consumer, 'INSUFFICIENT_EVIDENCE', reasons, rule)
        else:
            reasons.append('ENERGY_COEFFICIENT_HAS_NO_PHASE_AUTHORITY')
            return _decision(evidence, consumer, 'INCOMPATIBLE', reasons, rule)

    # --- Provenance honesty ----------------------------------------------------
    eligibility = rule.base_eligibility
    if evidence.method_class in ('manufacturer_declared', 'database_reference'):
        reasons.append('DECLARED_METHOD_NOT_MEASURED')
        if eligibility == 'DIRECTLY_COMPATIBLE':
            eligibility = 'COMPATIBLE_WITH_DECLARED_ASSUMPTIONS'
    if evidence.method_class == 'inverse_estimated':
        reasons.append('DERIVED_DATA_CANNOT_BE_MEASURED_AUTHORITY')
        if eligibility == 'DIRECTLY_COMPATIBLE':
            eligibility = 'COMPATIBLE_WITH_DECLARED_ASSUMPTIONS'
    if evidence.method_class == 'derived_conversion':
        reasons.append('DERIVED_DATA_CANNOT_BE_MEASURED_AUTHORITY')
        if conversion is None:
            reasons.append('CONVERSION_ARTIFACT_MISSING')
            return _decision(evidence, consumer, 'INSUFFICIENT_EVIDENCE', reasons, rule)

    if conversion is not None:
        eligibility = 'DERIVED_WITH_UNCERTAINTY'
        reasons.append('CONVERTED_VIA_ARTIFACT')

    if 'STANDARD_REVISION_UNKNOWN' in reasons and eligibility == 'DIRECTLY_COMPATIBLE':
        eligibility = 'COMPATIBLE_WITH_DECLARED_ASSUMPTIONS'

    if reasons and all(
        reason in (
            'INCIDENCE_UNKNOWN',
            'PHASE_UNKNOWN',
            'METHOD_UNKNOWN',
            'QUANTITY_UNKNOWN',
            'FREQUENCY_RANGE_UNDECLARED',
            'STANDARD_REVISION_UNKNOWN',
        )
        for reason in reasons
        if reason not in (
            'DECLARED_METHOD_NOT_MEASURED',
            'CONVERTED_VIA_ARTIFACT',
        )
    ) and any(
        reason in (
            'METHOD_UNKNOWN',
            'QUANTITY_UNKNOWN',
            'INCIDENCE_UNKNOWN',
            'PHASE_UNKNOWN',
            'FREQUENCY_RANGE_UNDECLARED',
        )
        for reason in reasons
    ):
        return _decision(evidence, consumer, 'INSUFFICIENT_EVIDENCE', reasons, rule)

    if not reasons:
        reasons.append('COMPATIBLE_AS_DECLARED')
    return _decision(
        evidence, consumer, eligibility, reasons, rule,
        conversion_artifact_id=(
            None if conversion is None else conversion.artifact_id
        ),
    )


def _select_rule(
    consumer: BoundaryConsumerKind,
    quantity: BoundaryPhysicalQuantity,
) -> CompatibilityRule | None:
    """The most specific rule for (consumer, quantity), or None."""

    candidates = [
        rule for rule in COMPATIBILITY_RULES_V1
        if rule.consumer == consumer and quantity in rule.quantities
    ]
    if not candidates:
        return None
    # Prefer the narrowest (least permissive) matching rule — a phase-bearing
    # 'derived' variant outranks a generic direct rule for the same pair.
    def specificity(rule: CompatibilityRule) -> int:
        score = 0
        if rule.required_phase is not None:
            score += 2
        if rule.required_incidence is not None:
            score += 1
        return -score

    return sorted(candidates, key=specificity)[0]


def _decision(
    evidence: MaterialBoundaryEvidence,
    consumer: BoundaryConsumerKind,
    eligibility: EvidenceEligibility,
    reasons: Sequence[MaterialCompatibilityReason],
    rule: CompatibilityRule | None = None,
    note: str | None = None,
    conversion_artifact_id: str | None = None,
) -> MaterialEvidenceCompatibility:
    unique_reasons = tuple(dict.fromkeys(reasons))
    return MaterialEvidenceCompatibility(
        evidence_id=evidence.evidence_id,
        consumer=consumer,
        eligibility=eligibility,
        reasons=unique_reasons,
        rule_id=None if rule is None else rule.rule_id,
        conversion_artifact_id=conversion_artifact_id,
        note=note,
    )


# ---------------------------------------------------------------------------
# Composition with existing authorities (#771 / #1032)
# ---------------------------------------------------------------------------


_LIBRARY_QUANTITY_MAP: dict[str, BoundaryPhysicalQuantity] = {
    'random_incidence_absorption_coefficient': 'absorption_coefficient_diffuse_field',
    'normal_incidence_absorption_coefficient': 'absorption_coefficient_normal_incidence',
    'complex_reflection_coefficient': 'complex_reflection_coefficient',
    'surface_impedance': 'surface_impedance',
    'surface_admittance': 'surface_admittance',
    'scattering_coefficient': 'scattering_coefficient_random_incidence',
    'transmission_loss': 'unknown_quantity',
    'porous_model_parameters': 'porous_model_parameters',
}

_LIBRARY_INCIDENCE_MAP: dict[str, EvidenceIncidence] = {
    'random': 'diffuse_reverberant',
    'normal': 'normal',
    'oblique': 'specific_angle',
    'unknown': 'unknown',
}

_LIBRARY_PHASE_MAP: dict[str, EvidencePhase] = {
    'random_incidence_absorption_coefficient': 'magnitude_energy_only',
    'normal_incidence_absorption_coefficient': 'magnitude_energy_only',
    'scattering_coefficient': 'magnitude_energy_only',
    'transmission_loss': 'magnitude_energy_only',
    'porous_model_parameters': 'magnitude_energy_only',
    'complex_reflection_coefficient': 'complex_reflection',
    'surface_impedance': 'complex_impedance',
    'surface_admittance': 'complex_impedance',
}


def boundary_evidence_from_material_acoustic(
    evidence: MaterialAcousticEvidence,
    *,
    method_class: EvidenceMethodClass,
    standard_revision: str | None = None,
    laboratory: str | None = None,
    build_up: MaterialBuildUp | None = None,
    provenance_note: str | None = None,
) -> MaterialBoundaryEvidence:
    """Map a #771 ``MaterialAcousticEvidence`` onto the gate's record.

    The library row declares quantity/incidence; the *method class* and
    *standard revision* are caller authority because the library's
    ``method`` field is free text — callers must state what was actually
    done, and a standard method without a revision is flagged downstream.
    """

    quantity = _LIBRARY_QUANTITY_MAP.get(evidence.quantity, 'unknown_quantity')
    phase = _LIBRARY_PHASE_MAP.get(evidence.quantity, 'unknown_phase')
    if evidence.phase_deg is None and phase in (
        'complex_reflection',
        'complex_impedance',
    ):
        # A complex-capable quantity without phase data is magnitude-only.
        phase = 'magnitude_energy_only'
    incidence_angle = evidence.incidence_angle_deg
    incidence = _LIBRARY_INCIDENCE_MAP.get(evidence.incidence, 'unknown')
    return build_boundary_evidence(
        evidence_id=evidence.evidence_id,
        method_class=method_class,
        standard_revision=standard_revision,
        laboratory=laboratory,
        source_report=evidence.source_label,
        quantity=quantity,
        incidence=incidence,
        incidence_angle_deg=incidence_angle,
        phase=phase,
        band_center_hz=evidence.frequency_hz,
        build_up=build_up,
        source_refs=(f'material-acoustic:{evidence.evidence_sha256}',),
        provenance_note=provenance_note or evidence.uncertainty_note,
    )


_SCATTERING_QUANTITY_MAP: dict[str, BoundaryPhysicalQuantity] = {
    'random_incidence_scattering_coefficient': 'scattering_coefficient_random_incidence',
    'directional_diffusion_coefficient': 'directional_diffusion_coefficient',
    'random_incidence_diffusion_coefficient': 'directional_diffusion_coefficient',
    'directional_scattering_distribution': 'unknown_quantity',
    'model_derived_scattering': 'scattering_coefficient_random_incidence',
    'explicit_geometry_only': 'unknown_quantity',
    'unknown': 'unknown_quantity',
}

_SCATTERING_METHOD_MAP: dict[str, EvidenceMethodClass] = {
    'iso_17497_1': 'iso_17497_1_scattering',
    'iso_17497_2': 'iso_17497_2_directional_diffusion',
    'iso_354': 'iso_354_reverberation_room',
    'declared_no_standard': 'manufacturer_declared',
    'unknown': 'unknown_method',
}

_SCATTERING_REVISION_MAP: dict[str, str] = {
    'iso_17497_1': 'ISO 17497-1:2004',
    'iso_17497_2': 'ISO 17497-2:2012',
    'iso_354': 'ISO 354:2003',
}

_SCATTERING_INCIDENCE_MAP: dict[str, EvidenceIncidence] = {
    'random_or_diffuse_incidence': 'diffuse_reverberant',
    'angle_specific': 'specific_angle',
    'normal_incidence': 'normal',
    'unknown_incidence': 'unknown',
}


def boundary_evidence_from_surface_scattering(
    evidence: SurfaceScatteringEvidence,
) -> MaterialBoundaryEvidence:
    """Map a #1032 ``SurfaceScatteringEvidence`` onto the gate's record."""

    method_class = _SCATTERING_METHOD_MAP[evidence.standard]
    build_up = (
        None if evidence.mounting is None
        else MaterialBuildUp(mounting=evidence.mounting)
    )
    return build_boundary_evidence(
        evidence_id=evidence.evidence_id,
        method_class=method_class,
        standard_revision=_SCATTERING_REVISION_MAP.get(evidence.standard),
        laboratory=evidence.source,
        quantity=_SCATTERING_QUANTITY_MAP[evidence.quantity_kind],
        incidence=_SCATTERING_INCIDENCE_MAP[evidence.incidence_semantics],
        incidence_angle_deg=evidence.incidence_angle_deg,
        phase='magnitude_energy_only',
        band_center_hz=evidence.band_center_hz,
        frequency_validity_hz=evidence.valid_frequency_range_hz,
        build_up=build_up,
        uncertainty=(
            ()
            if evidence.uncertainty is None
            else (
                EvidenceUncertaintyContribution(
                    kind='measurement_uncertainty',
                    value=evidence.uncertainty,
                    unit='coefficient (0-1)',
                ),
            )
        ),
        source_refs=(f'surface-scattering:{evidence.evidence_sha256}',),
        provenance_note=evidence.provenance,
    )


def evaluate_surface_scattering_for_ga(
    evidence: SurfaceScatteringEvidence,
) -> MaterialEvidenceCompatibility:
    """GA scatter-fraction eligibility for #1032 scattering evidence.

    Composes with ``ga_scatter_fraction``: when the typed guard returns
    ``None`` (ISO 17497-2 diffusion, geometry-only, unknown), the verdict is
    INCOMPATIBLE/INSUFFICIENT_EVIDENCE rather than a fabricated coefficient.
    """

    boundary = boundary_evidence_from_surface_scattering(evidence)
    if ga_scatter_fraction(evidence) is None:
        if evidence.quantity_kind in (
            'directional_diffusion_coefficient',
            'random_incidence_diffusion_coefficient',
        ):
            return _decision(
                boundary,
                'geometric_scatter_fraction',
                'INCOMPATIBLE',
                ('DIFFUSION_COEFFICIENT_NOT_SCATTERING_PARAMETER',),
            )
        return _decision(
            boundary,
            'geometric_scatter_fraction',
            'INSUFFICIENT_EVIDENCE',
            ('QUANTITY_UNKNOWN',),
        )
    return evaluate_material_evidence_compatibility(
        boundary, 'geometric_scatter_fraction'
    )


__all__ = [
    'BoundaryConversionArtifact',
    'BoundaryConsumerKind',
    'BoundaryPhysicalQuantity',
    'COMPATIBILITY_EVALUATOR_VERSION',
    'COMPATIBILITY_MATRIX_VERSION',
    'COMPATIBILITY_RULES_V1',
    'CompatibilityRule',
    'CONVERSION_AUTHORITY_VERSION',
    'ELIGIBILITY_LABELS',
    'EVIDENCE_AUTHORITY_VERSION',
    'EvidenceIncidence',
    'EvidenceMethodClass',
    'EvidencePhase',
    'EvidenceEligibility',
    'EvidenceUncertaintyContribution',
    'MaterialBoundaryEvidence',
    'MaterialBuildUp',
    'MaterialCompatibilityReason',
    'MaterialEvidenceCompatibility',
    'METHOD_PRODUCIBLE_QUANTITIES',
    'REASON_LABELS',
    'UncertaintyKind',
    'boundary_evidence_from_material_acoustic',
    'boundary_evidence_from_surface_scattering',
    'build_boundary_evidence',
    'build_conversion_artifact',
    'build_up_matches',
    'evaluate_material_evidence_compatibility',
    'evaluate_surface_scattering_for_ga',
    'list_compatibility_matrix',
]
