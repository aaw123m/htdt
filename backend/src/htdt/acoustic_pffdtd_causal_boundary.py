from __future__ import annotations

from hashlib import sha256
import json
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import FrequencyDomain
from .r120_geometry_compiler import ExactExternalAuthorityRef


PFFDTD_CAUSAL_BOUNDARY_MAPPING_ID = (
    'htdt.pffdtd.causal_frequency_dependent_normalized_admittance_def'
)
PFFDTD_CAUSAL_BOUNDARY_MAPPING_VERSION = '1'
CAUSAL_BOUNDARY_AUTHORITY_VERSION = 'r130c-causal-boundary-1'
CAUSAL_BOUNDARY_COMPILATION_VERSION = 'r130c-pffdtd-boundary-compilation-1'


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return sha256(_canonical_json(value).encode('utf-8')).hexdigest()


class CausalAdmittanceBranch(BaseModel):
    """One positive-real normalized series RLC branch used by PFFDTD DEF.

    PFFDTD evaluates branch normalized impedance as
    Zn_branch(s) = D*s + E + F/s and sums branch admittances in parallel.
    With D>=0, E>0 and F>=0 each branch is causal, passive and stable.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    d_seconds: float = Field(ge=0.0)
    e_dimensionless: float = Field(gt=0.0)
    f_per_second: float = Field(ge=0.0)

    @model_validator(mode='after')
    def finite_coefficients(self) -> 'CausalAdmittanceBranch':
        values = (self.d_seconds, self.e_dimensionless, self.f_per_second)
        if any(not math.isfinite(float(value)) for value in values):
            raise ValueError('causal admittance DEF coefficients must be finite')
        return self

    @property
    def frequency_dependent(self) -> bool:
        return self.d_seconds > 0.0 or self.f_per_second > 0.0

    def as_def(self) -> tuple[float, float, float]:
        return (
            float(self.d_seconds),
            float(self.e_dimensionless),
            float(self.f_per_second),
        )


class CausalBoundaryFitProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    source_data_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    fitting_algorithm: str = Field(min_length=1)
    fitting_algorithm_version: str = Field(min_length=1)
    order: int = Field(ge=1)
    error_metric: str = Field(min_length=1)
    valid_frequency_domain: FrequencyDomain
    passivity_correction_applied: bool
    residual: float = Field(ge=0.0)
    residual_unit: str = Field(min_length=1)


class CausalFrequencyDependentBoundaryAuthority(BaseModel):
    """Exact analytic R130C boundary authority with an explicit applicability band."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r130c-causal-boundary-1'
    ] = CAUSAL_BOUNDARY_AUTHORITY_VERSION
    authority_id: str = Field(pattern=r'^r130c-causal-boundary:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    source_scene_revision_id: str = Field(min_length=1)
    source_scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_surface_id: str = Field(pattern=r'^semantic-surface:[0-9a-f]{64}$')
    material_id: str = Field(min_length=1)
    material_version: str = Field(min_length=1)

    physical_quantity_type: Literal[
        'specific_acoustic_admittance'
    ] = 'specific_acoustic_admittance'
    physical_unit: Literal['m/(Pa*s)'] = 'm/(Pa*s)'
    normalization: Literal[
        'Yn=rho*c*Y_specific'
    ] = 'Yn=rho*c*Y_specific'
    representation: Literal[
        'parallel_series_RLC_normalized_DEF'
    ] = 'parallel_series_RLC_normalized_DEF'
    valid_frequency_domain: FrequencyDomain
    interpolation_semantics: Literal[
        'analytic_rational_evaluation_no_interpolation'
    ] = 'analytic_rational_evaluation_no_interpolation'
    extrapolation_rule: Literal['forbidden'] = 'forbidden'

    branches: tuple[CausalAdmittanceBranch, ...] = Field(min_length=1)
    evidence_state: Literal['measured', 'manufacturer', 'fitted', 'analytic']
    provenance: dict[str, Any]
    uncertainty: dict[str, Any] | None = None
    fit_provenance: CausalBoundaryFitProvenance | None = None

    causality_status: Literal['CAUSAL'] = 'CAUSAL'
    passivity_status: Literal['PASSIVE'] = 'PASSIVE'
    stability_status: Literal['STABLE'] = 'STABLE'
    structural_validation_rule: Literal[
        'nonnegative_D_F_positive_E_positive_real_series_RLC'
    ] = 'nonnegative_D_F_positive_E_positive_real_series_RLC'

    @model_validator(mode='after')
    def validate_contract(self) -> 'CausalFrequencyDependentBoundaryAuthority':
        if not self.provenance:
            raise ValueError('causal boundary authority requires provenance')
        if not any(branch.frequency_dependent for branch in self.branches):
            raise ValueError(
                'R130C authority must be frequency-dependent; use R130B for '
                'frequency-independent impedance'
            )
        if self.evidence_state == 'fitted':
            if self.fit_provenance is None:
                raise ValueError('fitted causal boundary requires fit provenance')
            if self.fit_provenance.valid_frequency_domain != self.valid_frequency_domain:
                raise ValueError('fit valid band must exactly match authority valid band')
        elif self.fit_provenance is not None:
            raise ValueError('fit provenance is only valid for fitted evidence')

        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('causal boundary semantic hash mismatch')
        if self.authority_id != f'r130c-causal-boundary:{expected}':
            raise ValueError('causal boundary authority id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'authority_id', 'semantic_sha256'},
        )

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.authority_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


class ComplexBoundarySample(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    frequency_hz: float = Field(gt=0.0)
    real: float
    imag: float

    @model_validator(mode='after')
    def finite_sample(self) -> 'ComplexBoundarySample':
        if not (math.isfinite(float(self.real)) and math.isfinite(float(self.imag))):
            raise ValueError('complex boundary samples must be finite')
        return self


class PffdtdCausalBoundaryCompilation(BaseModel):
    """Deterministic compiled representation distinct from source authority."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    compilation_version: Literal[
        'r130c-pffdtd-boundary-compilation-1'
    ] = CAUSAL_BOUNDARY_COMPILATION_VERSION
    compilation_id: str = Field(pattern=r'^r130c-pffdtd-boundary:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    source_boundary_authority_ref: ExactExternalAuthorityRef
    mapping_authority_ref: ExactExternalAuthorityRef
    source_scene_revision_id: str = Field(min_length=1)
    source_scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_surface_id: str = Field(pattern=r'^semantic-surface:[0-9a-f]{64}$')
    material_id: str = Field(min_length=1)
    material_version: str = Field(min_length=1)
    evidence_state: Literal['measured', 'manufacturer', 'fitted', 'analytic']
    provenance: dict[str, Any]
    uncertainty: dict[str, Any] | None = None

    physical_quantity_type: Literal[
        'specific_acoustic_admittance'
    ] = 'specific_acoustic_admittance'
    physical_unit: Literal['m/(Pa*s)'] = 'm/(Pa*s)'
    normalization: Literal[
        'Yn=rho*c*Y_specific'
    ] = 'Yn=rho*c*Y_specific'
    representation: Literal[
        'parallel_series_RLC_normalized_DEF'
    ] = 'parallel_series_RLC_normalized_DEF'
    interpolation_semantics: Literal[
        'analytic_rational_evaluation_no_interpolation'
    ] = 'analytic_rational_evaluation_no_interpolation'
    extrapolation_rule: Literal['forbidden'] = 'forbidden'
    valid_frequency_domain: FrequencyDomain

    requested_frequency_hz: tuple[float, ...] = Field(min_length=1)
    density_kg_m3: float = Field(gt=0.0)
    density_authority_ref: ExactExternalAuthorityRef
    sound_speed_m_s: float = Field(gt=0.0)
    sound_speed_authority_ref: ExactExternalAuthorityRef
    characteristic_impedance_pa_s_m: float = Field(gt=0.0)
    def_coefficients: tuple[tuple[float, float, float], ...] = Field(min_length=1)
    normalized_admittance_samples: tuple[ComplexBoundarySample, ...]
    physical_admittance_samples_m_per_pa_s: tuple[ComplexBoundarySample, ...]
    physical_impedance_samples_pa_s_m: tuple[ComplexBoundarySample, ...]

    causality_status: Literal['CAUSAL'] = 'CAUSAL'
    passivity_status: Literal['PASSIVE'] = 'PASSIVE'
    stability_status: Literal['STABLE'] = 'STABLE'
    passivity_validation: Literal[
        'positive_real_structural_plus_requested_band_reflection_check'
    ] = 'positive_real_structural_plus_requested_band_reflection_check'

    mapping_id: Literal[
        'htdt.pffdtd.causal_frequency_dependent_normalized_admittance_def'
    ] = PFFDTD_CAUSAL_BOUNDARY_MAPPING_ID
    mapping_version: Literal['1'] = PFFDTD_CAUSAL_BOUNDARY_MAPPING_VERSION

    @model_validator(mode='after')
    def validate_compilation(self) -> 'PffdtdCausalBoundaryCompilation':
        frequencies = tuple(float(value) for value in self.requested_frequency_hz)
        if frequencies != tuple(sorted(set(frequencies))):
            raise ValueError('compiled boundary frequencies must be unique/sorted')
        if (
            not self.valid_frequency_domain.contains(frequencies[0])
            or not self.valid_frequency_domain.contains(frequencies[-1])
        ):
            raise ValueError('compiled boundary frequency is outside valid band')
        if self.mapping_authority_ref.authority_version != self.mapping_version:
            raise ValueError('causal PFFDTD mapping authority version mismatch')
        if len(self.normalized_admittance_samples) != len(frequencies):
            raise ValueError('normalized admittance sample count mismatch')
        if len(self.physical_admittance_samples_m_per_pa_s) != len(frequencies):
            raise ValueError('physical admittance sample count mismatch')
        if len(self.physical_impedance_samples_pa_s_m) != len(frequencies):
            raise ValueError('physical impedance sample count mismatch')
        if any(
            tuple(sample.frequency_hz for sample in samples) != frequencies
            for samples in (
                self.normalized_admittance_samples,
                self.physical_admittance_samples_m_per_pa_s,
                self.physical_impedance_samples_pa_s_m,
            )
        ):
            raise ValueError('compiled boundary sample frequency mismatch')
        expected_rho_c = float(self.density_kg_m3) * float(self.sound_speed_m_s)
        if not math.isclose(
            self.characteristic_impedance_pa_s_m,
            expected_rho_c,
            rel_tol=1.0e-12,
            abs_tol=1.0e-12,
        ):
            raise ValueError('compiled boundary characteristic impedance != rho*c')
        if any(
            len(row) != 3
            or not all(math.isfinite(float(value)) for value in row)
            or row[0] < 0.0
            or row[1] <= 0.0
            or row[2] < 0.0
            for row in self.def_coefficients
        ):
            raise ValueError('compiled causal DEF violates passive/stable contract')
        if not any(row[0] > 0.0 or row[2] > 0.0 for row in self.def_coefficients):
            raise ValueError('compiled R130C DEF is not frequency-dependent')

        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('compiled causal boundary semantic hash mismatch')
        if self.compilation_id != f'r130c-pffdtd-boundary:{expected}':
            raise ValueError('compiled causal boundary id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'compilation_id', 'semantic_sha256'},
        )


def pffdtd_causal_boundary_mapping_authority_payload() -> dict[str, object]:
    return {
        'authority_kind': 'pffdtd_causal_boundary_mapping',
        'mapping_id': PFFDTD_CAUSAL_BOUNDARY_MAPPING_ID,
        'mapping_version': PFFDTD_CAUSAL_BOUNDARY_MAPPING_VERSION,
        'source_quantity': 'specific_acoustic_admittance',
        'source_unit': 'm/(Pa*s)',
        'normalization': 'Yn=rho*c*Y_specific',
        'source_representation': 'parallel_series_RLC_normalized_DEF',
        'pffdtd_representation': 'DEF',
        'pffdtd_frequency_domain_equation': 'Yn(jw)=sum(1/(jw*D+E+F/(jw)))',
        'pffdtd_time_domain_realization': 'native_recursive_boundary_state',
        'causality_contract': 'structural_positive_real_series_RLC',
        'passivity_contract': 'D>=0,E>0,F>=0_each_branch',
        'stability_contract': 'continuous_time_branch_poles_left_half_plane',
        'scalar_absorption_conversion': False,
        'frequency_sample_ifft': False,
        'fitting_performed_by_mapping': False,
    }


def build_causal_frequency_dependent_boundary_authority(
    *,
    source_scene_revision_id: str,
    source_scene_content_hash: str,
    source_surface_id: str,
    material_id: str,
    material_version: str,
    valid_frequency_domain: FrequencyDomain,
    branches: tuple[CausalAdmittanceBranch, ...],
    evidence_state: Literal['measured', 'manufacturer', 'fitted', 'analytic'],
    provenance: dict[str, Any],
    uncertainty: dict[str, Any] | None = None,
    fit_provenance: CausalBoundaryFitProvenance | None = None,
) -> CausalFrequencyDependentBoundaryAuthority:
    core = {
        'authority_version': CAUSAL_BOUNDARY_AUTHORITY_VERSION,
        'source_scene_revision_id': source_scene_revision_id,
        'source_scene_content_hash': source_scene_content_hash,
        'source_surface_id': source_surface_id,
        'material_id': material_id,
        'material_version': material_version,
        'physical_quantity_type': 'specific_acoustic_admittance',
        'physical_unit': 'm/(Pa*s)',
        'normalization': 'Yn=rho*c*Y_specific',
        'representation': 'parallel_series_RLC_normalized_DEF',
        'valid_frequency_domain': valid_frequency_domain.model_dump(mode='json'),
        'interpolation_semantics': 'analytic_rational_evaluation_no_interpolation',
        'extrapolation_rule': 'forbidden',
        'branches': [branch.model_dump(mode='json') for branch in branches],
        'evidence_state': evidence_state,
        'provenance': provenance,
        'uncertainty': uncertainty,
        'fit_provenance': None if fit_provenance is None else fit_provenance.model_dump(mode='json'),
        'causality_status': 'CAUSAL',
        'passivity_status': 'PASSIVE',
        'stability_status': 'STABLE',
        'structural_validation_rule': 'nonnegative_D_F_positive_E_positive_real_series_RLC',
    }
    digest = _digest(core)
    return CausalFrequencyDependentBoundaryAuthority(
        authority_id=f'r130c-causal-boundary:{digest}',
        semantic_sha256=digest,
        **core,
    )


def _require_frequency_domain(
    authority: CausalFrequencyDependentBoundaryAuthority,
    frequency_hz: tuple[float, ...],
) -> tuple[float, ...]:
    frequencies = tuple(float(value) for value in frequency_hz)
    if (
        not frequencies
        or frequencies != tuple(sorted(set(frequencies)))
        or any(not math.isfinite(value) or value <= 0.0 for value in frequencies)
    ):
        raise ValueError('frequency list must be finite, positive, unique and sorted')
    if (
        not authority.valid_frequency_domain.contains(frequencies[0])
        or not authority.valid_frequency_domain.contains(frequencies[-1])
    ):
        raise ValueError(
            'requested frequency is outside causal boundary valid band; '
            'extrapolation is forbidden'
        )
    return frequencies


def evaluate_normalized_admittance(
    authority: CausalFrequencyDependentBoundaryAuthority,
    frequency_hz: tuple[float, ...],
) -> tuple[complex, ...]:
    authority = CausalFrequencyDependentBoundaryAuthority.model_validate(
        authority.model_dump(mode='python')
    )
    frequencies = _require_frequency_domain(authority, frequency_hz)
    values: list[complex] = []
    for frequency in frequencies:
        jw = 1j * 2.0 * math.pi * frequency
        admittance = 0j
        for branch in authority.branches:
            branch_impedance = (
                jw * float(branch.d_seconds)
                + float(branch.e_dimensionless)
                + float(branch.f_per_second) / jw
            )
            admittance += 1.0 / branch_impedance
        if (
            not math.isfinite(admittance.real)
            or not math.isfinite(admittance.imag)
            or admittance.real <= 0.0
        ):
            raise ValueError(
                'causal boundary evaluation violated positive-real passivity contract'
            )
        values.append(admittance)
    return tuple(values)


def compile_causal_boundary_to_pffdtd(
    *,
    authority: CausalFrequencyDependentBoundaryAuthority,
    source_boundary_authority_ref: ExactExternalAuthorityRef,
    mapping_authority_ref: ExactExternalAuthorityRef,
    requested_frequency_hz: tuple[float, ...],
    density_kg_m3: float,
    density_authority_ref: ExactExternalAuthorityRef,
    sound_speed_m_s: float,
    sound_speed_authority_ref: ExactExternalAuthorityRef,
    expected_scene_revision_id: str,
    expected_scene_content_hash: str,
    expected_surface_id: str,
) -> PffdtdCausalBoundaryCompilation:
    authority = CausalFrequencyDependentBoundaryAuthority.model_validate(
        authority.model_dump(mode='python')
    )
    if source_boundary_authority_ref != authority.as_external_ref():
        raise ValueError('causal boundary external authority identity mismatch')
    if mapping_authority_ref.authority_version != PFFDTD_CAUSAL_BOUNDARY_MAPPING_VERSION:
        raise ValueError('causal boundary PFFDTD mapping version mismatch')
    if (
        authority.source_scene_revision_id != expected_scene_revision_id
        or authority.source_scene_content_hash != expected_scene_content_hash
        or authority.source_surface_id != expected_surface_id
    ):
        raise ValueError('causal boundary exact SceneRevision/surface binding mismatch')

    density = float(density_kg_m3)
    sound_speed = float(sound_speed_m_s)
    if (
        not math.isfinite(density)
        or density <= 0.0
        or not math.isfinite(sound_speed)
        or sound_speed <= 0.0
    ):
        raise ValueError('causal boundary requires finite positive rho and c')
    rho_c = density * sound_speed
    frequencies = _require_frequency_domain(authority, requested_frequency_hz)
    normalized = evaluate_normalized_admittance(authority, frequencies)

    reflection = tuple((1.0 - value) / (1.0 + value) for value in normalized)
    if any(abs(value) > 1.0 + 1.0e-12 for value in reflection):
        raise ValueError('causal boundary passivity check found |R| > 1')

    def samples(values: tuple[complex, ...]) -> list[dict[str, float]]:
        return [
            {'frequency_hz': frequency, 'real': float(value.real), 'imag': float(value.imag)}
            for frequency, value in zip(frequencies, values, strict=True)
        ]

    physical_admittance = tuple(value / rho_c for value in normalized)
    physical_impedance = tuple(rho_c / value for value in normalized)
    core = {
        'compilation_version': CAUSAL_BOUNDARY_COMPILATION_VERSION,
        'source_boundary_authority_ref': source_boundary_authority_ref.model_dump(mode='json'),
        'mapping_authority_ref': mapping_authority_ref.model_dump(mode='json'),
        'source_scene_revision_id': authority.source_scene_revision_id,
        'source_scene_content_hash': authority.source_scene_content_hash,
        'source_surface_id': authority.source_surface_id,
        'material_id': authority.material_id,
        'material_version': authority.material_version,
        'evidence_state': authority.evidence_state,
        'provenance': authority.provenance,
        'uncertainty': authority.uncertainty,
        'physical_quantity_type': authority.physical_quantity_type,
        'physical_unit': authority.physical_unit,
        'normalization': authority.normalization,
        'representation': authority.representation,
        'interpolation_semantics': authority.interpolation_semantics,
        'extrapolation_rule': authority.extrapolation_rule,
        'valid_frequency_domain': authority.valid_frequency_domain.model_dump(mode='json'),
        'requested_frequency_hz': list(frequencies),
        'density_kg_m3': density,
        'density_authority_ref': density_authority_ref.model_dump(mode='json'),
        'sound_speed_m_s': sound_speed,
        'sound_speed_authority_ref': sound_speed_authority_ref.model_dump(mode='json'),
        'characteristic_impedance_pa_s_m': rho_c,
        'def_coefficients': [list(branch.as_def()) for branch in authority.branches],
        'normalized_admittance_samples': samples(normalized),
        'physical_admittance_samples_m_per_pa_s': samples(physical_admittance),
        'physical_impedance_samples_pa_s_m': samples(physical_impedance),
        'causality_status': authority.causality_status,
        'passivity_status': authority.passivity_status,
        'stability_status': authority.stability_status,
        'passivity_validation': 'positive_real_structural_plus_requested_band_reflection_check',
        'mapping_id': PFFDTD_CAUSAL_BOUNDARY_MAPPING_ID,
        'mapping_version': PFFDTD_CAUSAL_BOUNDARY_MAPPING_VERSION,
    }
    digest = _digest(core)
    return PffdtdCausalBoundaryCompilation(
        compilation_id=f'r130c-pffdtd-boundary:{digest}',
        semantic_sha256=digest,
        **core,
    )
