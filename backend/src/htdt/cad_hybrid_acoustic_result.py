from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_acoustic_snapshot import AcousticPredictionRequest, AcousticSceneSnapshot
from .cad_acoustic_solver_result import (
    AcousticSolverObservableArtifact,
    AcousticSolverResultEnvelope,
)
from .cad_equipment import FrequencyDomain
from .cad_geometric_acoustics_adapter import DeterministicPathArtifact
from .cad_hybrid_late_energy import R160_LATE_ENERGY_ARTIFACT_SCHEMA_REF
from .cad_repository import SceneRepository
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
    connect_sqlite,
)
from .r120_geometry_compiler import ExactExternalAuthorityRef
from .canonical_json import canonical_sha256 as _semantic_hash
from .clock import utc_now_iso as _utc_now


HYBRID_RESULT_SCHEMA_VERSION = 1
HYBRID_RESULT_AUTHORITY_VERSION = 'r160-hybrid-result-1'
HYBRID_STITCHING_POLICY_AUTHORITY_VERSION = 'r160-stitching-policy-1'
HYBRID_ALGORITHM_ID = 'htdt.r160.typed_hybrid_result'
HYBRID_ALGORITHM_VERSION = '1'

HybridObservableType = Literal[
    'coherent_transfer',
    'deterministic_paths',
    'late_energy_decay',
]
HybridPhaseCapability = Literal[
    'COMPLEX_EXPLICIT_REFERENCE',
    'UNAVAILABLE_NOT_SYNTHESIZED',
    'NOT_APPLICABLE',
]
HybridEvidenceState = Literal['EXECUTED_UNVALIDATED']
LateEnergyDecayState = Literal[
    'AVAILABLE',
    'UNAVAILABLE',
    'UNSUPPORTED',
    'NOT_PROVIDED',
]
StitchingMode = Literal[
    'disjoint_by_observable',
    'frequency_partition_no_blend',
    'overlap_preserve_components',
]
HybridCompositionObservable = Literal[
    'magnitude_energy',
    'coherent_phase',
    'arrival_timing',
    'deterministic_path_identity',
    'late_decay',
]
HybridPhysicalComponent = Literal[
    'direct',
    'deterministic_early_reflection',
    'diffuse_late_energy',
    'full_field',
]
HybridCompositionCapabilityRequirement = Literal[
    'compatible_quantity_reference',
    'double_count_resolved',
    'coherent_phase_authority_both',
    'source_phase_reference',
    'shared_time_origin',
    'compatible_fourier_sign_convention',
    'compatible_propagation_timing',
    'deterministic_path_identity',
    'late_energy_decay_authority',
]

HYBRID_COMPOSITION_SPEC_AUTHORITY_VERSION = 'r160-hybrid-composition-spec-1'

ExternalPayloadResolver = Callable[[ExactExternalAuthorityRef], Any]


class SnapshotRequestResolver(Protocol):
    path: Path

    def get_snapshot(self, snapshot_id: str) -> AcousticSceneSnapshot | None:
        ...

    def get_prediction_request(
        self,
        request_id: str,
    ) -> AcousticPredictionRequest | None:
        ...


class SolverResultResolver(Protocol):
    path: Path

    def get(self, result_id: str) -> AcousticSolverResultEnvelope | None:
        ...


class DeterministicPathResolver(Protocol):
    path: Path

    def get(self, artifact_id: str) -> DeterministicPathArtifact | None:
        ...


def _model_hash(model: BaseModel) -> str:
    return _semantic_hash(model.model_dump(mode='json'))


def _domain_contains(
    container: FrequencyDomain,
    requested: FrequencyDomain,
) -> bool:
    return (
        float(requested.minimum_hz) >= float(container.minimum_hz)
        and float(requested.maximum_hz) <= float(container.maximum_hz)
    )


def _domains_overlap(first: FrequencyDomain, second: FrequencyDomain) -> bool:
    return (
        max(float(first.minimum_hz), float(second.minimum_hz))
        <= min(float(first.maximum_hz), float(second.maximum_hz))
    )


def _domain_intersection(
    first: FrequencyDomain,
    second: FrequencyDomain,
) -> FrequencyDomain | None:
    minimum = max(float(first.minimum_hz), float(second.minimum_hz))
    maximum = min(float(first.maximum_hz), float(second.maximum_hz))
    if maximum <= minimum:
        return None
    return FrequencyDomain(minimum_hz=minimum, maximum_hz=maximum)


def _gap_within_requested_domain(
    first: FrequencyDomain,
    second: FrequencyDomain,
    requested: FrequencyDomain,
) -> tuple[FrequencyDomain, ...]:
    if _domain_intersection(first, second) is not None:
        return ()
    left, right = sorted(
        (first, second),
        key=lambda item: float(item.minimum_hz),
    )
    gap_minimum = max(float(requested.minimum_hz), float(left.maximum_hz))
    gap_maximum = min(float(requested.maximum_hz), float(right.minimum_hz))
    if gap_maximum <= gap_minimum:
        return ()
    return (
        FrequencyDomain(
            minimum_hz=gap_minimum,
            maximum_hz=gap_maximum,
        ),
    )


def _composition_requirements(
    observable: HybridCompositionObservable,
) -> tuple[HybridCompositionCapabilityRequirement, ...]:
    mapping: dict[
        HybridCompositionObservable,
        tuple[HybridCompositionCapabilityRequirement, ...],
    ] = {
        'magnitude_energy': (
            'compatible_quantity_reference',
            'double_count_resolved',
        ),
        'coherent_phase': (
            'coherent_phase_authority_both',
            'source_phase_reference',
            'shared_time_origin',
            'compatible_fourier_sign_convention',
            'double_count_resolved',
        ),
        'arrival_timing': (
            'shared_time_origin',
            'compatible_propagation_timing',
        ),
        'deterministic_path_identity': (
            'deterministic_path_identity',
        ),
        'late_decay': (
            'late_energy_decay_authority',
        ),
    }
    return mapping[observable]


class HybridSourceIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    source_entity_id: str = Field(min_length=1)
    source_binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_entity_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    equipment_definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    r110_compiled_source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    directivity_dataset_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )


class HybridReceiverIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    receiver_id: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    receiver_binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class HybridEnvironmentIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    environment_binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    environment_authority_ref: ExactExternalAuthorityRef
    sound_speed_m_s: float | None = Field(default=None, gt=0.0)
    sound_speed_source_authority: ExactExternalAuthorityRef | None = None

    @model_validator(mode='after')
    def exact_sound_speed_pair(self) -> 'HybridEnvironmentIdentity':
        if (self.sound_speed_m_s is None) != (
            self.sound_speed_source_authority is None
        ):
            raise ValueError(
                'hybrid sound speed value and source authority must be paired'
            )
        return self


class HybridPredictionRequestRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    request_id: str = Field(
        pattern=r'^acoustic-prediction-request:[0-9a-f]{64}$'
    )
    request_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    deterministic_input_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    model_solver_role_id: str = Field(min_length=1)
    requested_frequency_domain: FrequencyDomain
    requested_observables: tuple[str, ...]


class HybridSolverResultRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    result_id: str = Field(pattern=r'^acoustic-solver-result:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    dispatch_binding_id: str = Field(
        pattern=r'^acoustic-solver-dispatch:[0-9a-f]{64}$'
    )
    dispatch_binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    prediction_request_id: str = Field(
        pattern=r'^acoustic-prediction-request:[0-9a-f]{64}$'
    )
    prediction_request_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    prediction_deterministic_input_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    adapter_descriptor_id: str = Field(
        pattern=r'^acoustic-solver-adapter:[0-9a-f]{64}$'
    )
    adapter_descriptor_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    deterministic_solver_input_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    solver_implementation_ref: ExactExternalAuthorityRef
    solver_configuration_ref: ExactExternalAuthorityRef
    artifacts: tuple[AcousticSolverObservableArtifact, ...]


class HybridObservableValidity(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    frequency_domain: FrequencyDomain
    observable_type: HybridObservableType
    source_observable: str = Field(min_length=1)
    phase_capability: HybridPhaseCapability
    solver_result_id: str = Field(
        pattern=r'^acoustic-solver-result:[0-9a-f]{64}$'
    )
    solver_result_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    adapter_descriptor_id: str = Field(
        pattern=r'^acoustic-solver-adapter:[0-9a-f]{64}$'
    )
    adapter_descriptor_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    solver_implementation_ref: ExactExternalAuthorityRef
    solver_configuration_ref: ExactExternalAuthorityRef
    evidence_state: HybridEvidenceState

    @property
    def minimum_frequency_hz(self) -> float:
        return float(self.frequency_domain.minimum_hz)

    @property
    def maximum_frequency_hz(self) -> float:
        return float(self.frequency_domain.maximum_hz)


class CoherentTransfer(BaseModel):
    """Exact ref to phase-bearing complex transfer evidence; never copied data."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    coherent: Literal[True] = True
    quantity: Literal['complex_pressure'] = 'complex_pressure'
    artifact_authority: ExactExternalAuthorityRef
    encoding_schema_ref: ExactExternalAuthorityRef
    source_entity_ids: tuple[str, ...] = Field(min_length=1)
    receiver_identity_order: tuple[str, ...] = Field(min_length=1)
    units: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    complex_form: str = Field(min_length=1)
    phasor_convention: str = Field(min_length=1)
    analysis_fourier_kernel: str = Field(min_length=1)
    validity: HybridObservableValidity

    @model_validator(mode='after')
    def coherent_contract(self) -> 'CoherentTransfer':
        if self.validity.observable_type != 'coherent_transfer':
            raise ValueError('CoherentTransfer validity observable mismatch')
        if self.validity.source_observable != 'complex_pressure':
            raise ValueError('CoherentTransfer requires complex_pressure authority')
        if self.validity.phase_capability != 'COMPLEX_EXPLICIT_REFERENCE':
            raise ValueError('CoherentTransfer requires explicit complex phase')
        return self


class DeterministicPathSet(BaseModel):
    """Exact ref to R150 typed paths; path payload remains owned by R150."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    coherent: Literal[False] = False
    artifact_authority: ExactExternalAuthorityRef
    encoding_schema_ref: ExactExternalAuthorityRef
    path_scope: str = Field(min_length=1)
    contribution_quantity: Literal[
        'relative_energy_transport_per_m2'
    ] = 'relative_energy_transport_per_m2'
    direct_path_count: int = Field(ge=0)
    first_order_specular_path_count: int = Field(ge=0)
    path_types_present: tuple[Literal['direct', 'specular_reflection'], ...]
    validity: HybridObservableValidity

    @model_validator(mode='after')
    def path_contract(self) -> 'DeterministicPathSet':
        if self.validity.observable_type != 'deterministic_paths':
            raise ValueError('DeterministicPathSet validity observable mismatch')
        if self.validity.source_observable != 'deterministic_paths':
            raise ValueError(
                'DeterministicPathSet requires deterministic_paths authority'
            )
        if self.validity.phase_capability != 'UNAVAILABLE_NOT_SYNTHESIZED':
            raise ValueError('DeterministicPathSet cannot claim coherent phase')
        return self


class LateEnergyDecay(BaseModel):
    """R160 late-tail slot. No synthetic decay is generated by this authority."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    state: LateEnergyDecayState
    reason: str = Field(min_length=1)
    artifact_authority: ExactExternalAuthorityRef | None = None
    encoding_schema_ref: ExactExternalAuthorityRef | None = None
    validity: HybridObservableValidity | None = None

    @model_validator(mode='after')
    def late_contract(self) -> 'LateEnergyDecay':
        available = self.state == 'AVAILABLE'
        values = (
            self.artifact_authority,
            self.encoding_schema_ref,
            self.validity,
        )
        if available and any(value is None for value in values):
            raise ValueError(
                'AVAILABLE LateEnergyDecay requires exact artifact and validity'
            )
        if not available and any(value is not None for value in values):
            raise ValueError(
                'unavailable LateEnergyDecay cannot expose fabricated evidence'
            )
        if available:
            assert self.validity is not None
            if self.validity.observable_type != 'late_energy_decay':
                raise ValueError('LateEnergyDecay validity observable mismatch')
            if self.validity.phase_capability != 'NOT_APPLICABLE':
                raise ValueError(
                    'LateEnergyDecay phase capability must be NOT_APPLICABLE'
                )
            if (
                self.encoding_schema_ref
                != R160_LATE_ENERGY_ARTIFACT_SCHEMA_REF
            ):
                raise ValueError(
                    'AVAILABLE LateEnergyDecay requires the canonical '
                    'late-energy decay encoding schema'
                )
        return self


class HybridFrequencyPartition(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    component: HybridObservableType
    frequency_domain: FrequencyDomain


class HybridStitchingPolicy(BaseModel):
    """Versioned non-blending policy. All unsafe synthesis flags are fixed false."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r160-stitching-policy-1'
    ] = HYBRID_STITCHING_POLICY_AUTHORITY_VERSION
    policy_id: str = Field(pattern=r'^hybrid-stitching-policy:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    mode: StitchingMode
    frequency_partitions: tuple[HybridFrequencyPartition, ...] = ()
    numerical_blend_permitted: Literal[False] = False
    cross_observable_numeric_combination_permitted: Literal[False] = False
    frequency_extrapolation_permitted: Literal[False] = False
    unsupported_phase_generation_permitted: Literal[False] = False

    @model_validator(mode='after')
    def policy_contract(self) -> 'HybridStitchingPolicy':
        if self.mode == 'frequency_partition_no_blend':
            if not self.frequency_partitions:
                raise ValueError(
                    'frequency_partition_no_blend requires explicit partitions'
                )
            ordered = tuple(
                sorted(
                    self.frequency_partitions,
                    key=lambda item: (
                        float(item.frequency_domain.minimum_hz),
                        float(item.frequency_domain.maximum_hz),
                        item.component,
                    ),
                )
            )
            if ordered != self.frequency_partitions:
                raise ValueError(
                    'hybrid frequency partitions must use canonical ordering'
                )
            for first, second in zip(ordered, ordered[1:], strict=False):
                if _domains_overlap(
                    first.frequency_domain,
                    second.frequency_domain,
                ):
                    raise ValueError(
                        'frequency_partition_no_blend partitions must not overlap'
                    )
        elif self.frequency_partitions:
            raise ValueError(
                f'{self.mode} does not accept frequency partition boundaries'
            )
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('HybridStitchingPolicy semantic hash mismatch')
        if self.policy_id != f'hybrid-stitching-policy:{expected}':
            raise ValueError('HybridStitchingPolicy id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'policy_id', 'semantic_sha256'},
        )

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.policy_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


def build_hybrid_stitching_policy(
    *,
    mode: StitchingMode,
    frequency_partitions: Sequence[HybridFrequencyPartition] = (),
) -> HybridStitchingPolicy:
    partitions = tuple(
        sorted(
            (
                HybridFrequencyPartition.model_validate(
                    item.model_dump(mode='python')
                )
                for item in frequency_partitions
            ),
            key=lambda item: (
                float(item.frequency_domain.minimum_hz),
                float(item.frequency_domain.maximum_hz),
                item.component,
            ),
        )
    )
    core = {
        'authority_version': HYBRID_STITCHING_POLICY_AUTHORITY_VERSION,
        'mode': mode,
        'frequency_partitions': [
            item.model_dump(mode='json') for item in partitions
        ],
        'numerical_blend_permitted': False,
        'cross_observable_numeric_combination_permitted': False,
        'frequency_extrapolation_permitted': False,
        'unsupported_phase_generation_permitted': False,
    }
    digest = _semantic_hash(core)
    return HybridStitchingPolicy(
        policy_id=f'hybrid-stitching-policy:{digest}',
        semantic_sha256=digest,
        **core,
    )


class HybridCompositionArtifactIdentity(BaseModel):
    """Exact participating artifact identity and physical component semantics."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    backend: Literal['wave', 'deterministic_ga']
    solver_result_id: str = Field(pattern=r'^acoustic-solver-result:[0-9a-f]{64}$')
    solver_result_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    artifact_authority: ExactExternalAuthorityRef
    encoding_schema_ref: ExactExternalAuthorityRef
    valid_band: FrequencyDomain
    quantity: str = Field(min_length=1)
    units: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    component_semantics: tuple[HybridPhysicalComponent, ...] = Field(min_length=1)


class HybridCrossoverPolicy(BaseModel):
    """Explicit overlap/crossover behavior. There is intentionally no default."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    mode: Literal[
        'preserve_overlap_no_blend',
        'explicit_partition_no_blend',
        'preserve_gap_no_fill',
    ]
    crossover_hz: float | None = Field(default=None, gt=0.0)
    blend_width_hz: float = Field(default=0.0, ge=0.0, le=0.0)

    @model_validator(mode='after')
    def explicit_crossover_contract(self) -> 'HybridCrossoverPolicy':
        if self.mode == 'explicit_partition_no_blend':
            if self.crossover_hz is None:
                raise ValueError(
                    'explicit_partition_no_blend requires explicit crossover_hz'
                )
        elif self.crossover_hz is not None:
            raise ValueError(
                f'{self.mode} forbids an implicit/unused crossover frequency'
            )
        return self


class HybridNormalizationReferenceConvention(BaseModel):
    """Exact native quantity/reference convention; no hidden unit conversion."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    mode: Literal['native_exact_no_cross_quantity_conversion'] = (
        'native_exact_no_cross_quantity_conversion'
    )
    wave_quantity: Literal['complex_pressure'] = 'complex_pressure'
    wave_units: str = Field(min_length=1)
    wave_reference: str = Field(min_length=1)
    ga_quantity: Literal['relative_energy_transport_per_m2'] = (
        'relative_energy_transport_per_m2'
    )
    ga_units: Literal['relative_energy_per_m2'] = 'relative_energy_per_m2'
    ga_reference: Literal[
        'solver_native_relative_energy_transport'
    ] = 'solver_native_relative_energy_transport'


class HybridDoubleCountExclusionPolicy(BaseModel):
    """Explicit policy for overlapping physical components."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    mode: Literal[
        'preserve_components_no_numeric_sum',
        'require_disjoint_component_ownership',
    ]
    subtraction_authority_ref: None = None


class HybridCompositionSpec(BaseModel):
    """Immutable authority for one bounded wave/GA composition request."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r160-hybrid-composition-spec-1'
    ] = HYBRID_COMPOSITION_SPEC_AUTHORITY_VERSION
    composition_spec_id: str = Field(
        pattern=r'^hybrid-composition-spec:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    wave_input: HybridCompositionArtifactIdentity
    ga_input: HybridCompositionArtifactIdentity

    acoustic_scene_snapshot_id: str
    acoustic_scene_snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_identity_sha256: tuple[str, ...] = Field(min_length=1)
    receiver_identity_sha256: tuple[str, ...] = Field(min_length=1)
    environment_binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    observable: HybridCompositionObservable
    requested_frequency_domain: FrequencyDomain
    wave_valid_band: FrequencyDomain
    ga_valid_band: FrequencyDomain
    overlap_domain: FrequencyDomain | None
    crossover_policy: HybridCrossoverPolicy
    normalization_reference_convention: HybridNormalizationReferenceConvention
    double_count_exclusion_policy: HybridDoubleCountExclusionPolicy
    capability_requirements: tuple[
        HybridCompositionCapabilityRequirement, ...
    ] = Field(min_length=1)

    @model_validator(mode='after')
    def composition_spec_contract(self) -> 'HybridCompositionSpec':
        if self.wave_input.backend != 'wave':
            raise ValueError('hybrid composition wave input backend mismatch')
        if self.ga_input.backend != 'deterministic_ga':
            raise ValueError('hybrid composition GA input backend mismatch')
        if self.wave_input.valid_band != self.wave_valid_band:
            raise ValueError('hybrid composition wave valid-band mismatch')
        if self.ga_input.valid_band != self.ga_valid_band:
            raise ValueError('hybrid composition GA valid-band mismatch')
        normalization = self.normalization_reference_convention
        if (
            self.wave_input.quantity != normalization.wave_quantity
            or self.wave_input.units != normalization.wave_units
            or self.wave_input.reference != normalization.wave_reference
            or self.ga_input.quantity != normalization.ga_quantity
            or self.ga_input.units != normalization.ga_units
            or self.ga_input.reference != normalization.ga_reference
        ):
            raise ValueError(
                'hybrid composition normalization/reference convention '
                'does not match exact input quantities'
            )
        expected_overlap = _domain_intersection(
            self.wave_valid_band,
            self.ga_valid_band,
        )
        if self.overlap_domain != expected_overlap:
            raise ValueError('hybrid composition overlap-domain mismatch')
        if self.capability_requirements != _composition_requirements(
            self.observable
        ):
            raise ValueError(
                'hybrid composition capability requirements are incomplete'
            )
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('HybridCompositionSpec semantic hash mismatch')
        if self.composition_spec_id != f'hybrid-composition-spec:{expected}':
            raise ValueError('HybridCompositionSpec id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'composition_spec_id', 'semantic_sha256'},
        )

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.composition_spec_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


class HybridUnsupportedObservable(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    observable: HybridCompositionObservable
    reason_code: Literal[
        'INCOMPATIBLE_QUANTITY_REFERENCE',
        'DOUBLE_COUNT_AMBIGUITY',
        'MISSING_COHERENT_PHASE_AUTHORITY',
        'MISSING_SHARED_TIME_ORIGIN',
        'MISSING_LATE_DECAY_AUTHORITY',
        'NO_DETERMINISTIC_PATHS',
    ]
    detail: str = Field(min_length=1)


class HybridApproximationErrorMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    numerical_combination_performed: Literal[False] = False
    interpolation_performed: Literal[False] = False
    extrapolation_performed: Literal[False] = False
    estimated_error_bound: None = None
    error_bound_state: Literal[
        'UNAVAILABLE_NO_CROSS_BACKEND_NUMERIC_OPERATION'
    ] = 'UNAVAILABLE_NO_CROSS_BACKEND_NUMERIC_OPERATION'
    note: str = Field(min_length=1)


class HybridCompositionDecision(BaseModel):
    """Auditable result of capability, overlap, and double-count gates."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    status: Literal['ELIGIBLE_BOUNDED_NO_NUMERIC_BLEND']
    composition_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    requested_observable: HybridCompositionObservable
    supported_observables: tuple[HybridCompositionObservable, ...] = Field(
        min_length=1
    )
    unsupported_observables: tuple[HybridUnsupportedObservable, ...]
    source_valid_domains: tuple[FrequencyDomain, FrequencyDomain]
    valid_domains: tuple[FrequencyDomain, ...] = Field(min_length=1)
    overlap_domain: FrequencyDomain | None
    gap_domains: tuple[FrequencyDomain, ...]
    path_reflection_orders_present: tuple[int, ...]
    double_count_decision: Literal[
        'PRESERVED_COMPONENT_IDENTITIES_NO_NUMERIC_SUM',
        'NOT_APPLICABLE_NON_NUMERIC_OBSERVABLE',
    ]
    evidence: tuple[str, ...] = Field(min_length=1)
    approximation_error: HybridApproximationErrorMetadata

    @model_validator(mode='after')
    def composition_decision_contract(self) -> 'HybridCompositionDecision':
        if self.requested_observable not in self.supported_observables:
            raise ValueError(
                'composition decision cannot mark requested observable unsupported'
            )
        unsupported_names = {
            item.observable for item in self.unsupported_observables
        }
        if unsupported_names.intersection(self.supported_observables):
            raise ValueError(
                'composition observable cannot be both supported and unsupported'
            )
        return self


class HybridAcousticResult(BaseModel):
    """Solver-neutral R160 authority containing refs, not copied solver truth."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = HYBRID_RESULT_SCHEMA_VERSION
    authority_version: Literal[
        'r160-hybrid-result-1'
    ] = HYBRID_RESULT_AUTHORITY_VERSION
    hybrid_result_id: str = Field(pattern=r'^hybrid-acoustic-result:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    acoustic_scene_snapshot_id: str = Field(
        pattern=r'^acoustic-scene-snapshot:[0-9a-f]{64}$'
    )
    acoustic_scene_snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    semantic_geometry_id: str = Field(
        pattern=r'^semantic-acoustic-geometry:[0-9a-f]{64}$'
    )
    semantic_geometry_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    source_identities: tuple[HybridSourceIdentity, ...] = Field(min_length=1)
    receiver_identity_order: tuple[HybridReceiverIdentity, ...] = Field(
        min_length=1
    )
    environment_identity: HybridEnvironmentIdentity

    prediction_requests: tuple[HybridPredictionRequestRef, ...] = Field(
        min_length=1
    )
    participating_solver_results: tuple[HybridSolverResultRef, ...] = Field(
        min_length=1
    )

    coherent_transfer: CoherentTransfer | None = None
    deterministic_path_set: DeterministicPathSet | None = None
    late_energy_decay: LateEnergyDecay

    frequency_domain_relationship: Literal[
        'SINGLE_COMPONENT',
        'DISJOINT_COMPONENT_VALIDITY',
        'OVERLAPPING_COMPONENT_VALIDITY',
    ]
    stitching_policy_ref: ExactExternalAuthorityRef
    composition_spec: HybridCompositionSpec | None = None
    composition_decision: HybridCompositionDecision | None = None
    algorithm_id: Literal[
        'htdt.r160.typed_hybrid_result'
    ] = HYBRID_ALGORITHM_ID
    algorithm_version: Literal['1'] = HYBRID_ALGORITHM_VERSION

    @model_validator(mode='after')
    def hybrid_contract(self) -> 'HybridAcousticResult':
        request_ids = [item.request_id for item in self.prediction_requests]
        if len(request_ids) != len(set(request_ids)):
            raise ValueError('hybrid prediction request refs must be unique')
        result_ids = [
            item.result_id for item in self.participating_solver_results
        ]
        if len(result_ids) != len(set(result_ids)):
            raise ValueError('hybrid solver result refs must be unique')
        if self.coherent_transfer is None and self.deterministic_path_set is None:
            if self.late_energy_decay.state != 'AVAILABLE':
                raise ValueError(
                    'hybrid result requires at least one available typed component'
                )
        available_result_ids = set(result_ids)
        for validity in self._available_validities():
            if validity.solver_result_id not in available_result_ids:
                raise ValueError(
                    'hybrid component references a non-participating solver result'
                )
        if self.frequency_domain_relationship != self._derived_frequency_relationship():
            raise ValueError('hybrid frequency-domain relationship mismatch')
        if (self.composition_spec is None) != (self.composition_decision is None):
            raise ValueError(
                'hybrid composition spec and decision must be paired'
            )
        if self.composition_spec is not None:
            assert self.composition_decision is not None
            spec = self.composition_spec
            decision = self.composition_decision
            if (
                spec.acoustic_scene_snapshot_id
                != self.acoustic_scene_snapshot_id
                or spec.acoustic_scene_snapshot_sha256
                != self.acoustic_scene_snapshot_sha256
                or spec.scene_revision_id != self.scene_revision_id
                or spec.scene_content_hash != self.scene_content_hash
                or spec.source_identity_sha256
                != tuple(item.source_binding_sha256 for item in self.source_identities)
                or spec.receiver_identity_sha256
                != tuple(
                    item.receiver_binding_sha256
                    for item in self.receiver_identity_order
                )
                or spec.environment_binding_sha256
                != self.environment_identity.environment_binding_sha256
            ):
                raise ValueError(
                    'hybrid composition spec is incompatible with result authority'
                )
            if decision.composition_spec_sha256 != spec.semantic_sha256:
                raise ValueError(
                    'hybrid composition decision/spec identity mismatch'
                )
            if decision.overlap_domain != spec.overlap_domain:
                raise ValueError(
                    'hybrid composition decision/spec overlap mismatch'
                )
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('HybridAcousticResult semantic hash mismatch')
        if self.hybrid_result_id != f'hybrid-acoustic-result:{expected}':
            raise ValueError('HybridAcousticResult id mismatch')
        return self

    def _available_validities(self) -> tuple[HybridObservableValidity, ...]:
        values: list[HybridObservableValidity] = []
        if self.coherent_transfer is not None:
            values.append(self.coherent_transfer.validity)
        if self.deterministic_path_set is not None:
            values.append(self.deterministic_path_set.validity)
        if (
            self.late_energy_decay.state == 'AVAILABLE'
            and self.late_energy_decay.validity is not None
        ):
            values.append(self.late_energy_decay.validity)
        return tuple(values)

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            mode='json',
            exclude={'hybrid_result_id', 'semantic_sha256'},
        )
        # Preserve PR #254 v1 identities exactly when no bounded composition
        # authority is attached.
        if self.composition_spec is None:
            payload.pop('composition_spec', None)
            payload.pop('composition_decision', None)
        return payload

    def has_frequency_response(self) -> bool:
        return self.coherent_transfer is not None

    def has_phase(self) -> bool:
        return (
            self.coherent_transfer is not None
            and self.coherent_transfer.validity.phase_capability
            == 'COMPLEX_EXPLICIT_REFERENCE'
        )

    def has_direct_or_early_paths(self) -> bool:
        component = self.deterministic_path_set
        return (
            component is not None
            and (
                component.direct_path_count > 0
                or component.first_order_specular_path_count > 0
            )
        )

    def has_late_energy_decay(self) -> bool:
        return self.late_energy_decay.state == 'AVAILABLE'

    def late_energy_decay_state(self) -> LateEnergyDecayState:
        return self.late_energy_decay.state

    def valid_frequency_domain_for(
        self,
        observable_type: HybridObservableType,
    ) -> FrequencyDomain | None:
        if observable_type == 'coherent_transfer':
            return (
                None
                if self.coherent_transfer is None
                else self.coherent_transfer.validity.frequency_domain
            )
        if observable_type == 'deterministic_paths':
            return (
                None
                if self.deterministic_path_set is None
                else self.deterministic_path_set.validity.frequency_domain
            )
        if self.late_energy_decay.validity is None:
            return None
        return self.late_energy_decay.validity.frequency_domain

    def _derived_frequency_relationship(self) -> Literal[
        'SINGLE_COMPONENT',
        'DISJOINT_COMPONENT_VALIDITY',
        'OVERLAPPING_COMPONENT_VALIDITY',
    ]:
        domains = [
            item.frequency_domain for item in self._available_validities()
        ]
        if len(domains) < 2:
            return 'SINGLE_COMPONENT'
        for index, first in enumerate(domains):
            for second in domains[index + 1 :]:
                if _domains_overlap(first, second):
                    return 'OVERLAPPING_COMPONENT_VALIDITY'
        return 'DISJOINT_COMPONENT_VALIDITY'

    def frequency_relationship(self) -> Literal[
        'SINGLE_COMPONENT',
        'DISJOINT_COMPONENT_VALIDITY',
        'OVERLAPPING_COMPONENT_VALIDITY',
    ]:
        return self.frequency_domain_relationship

    def numerical_blend_permitted(self) -> bool:
        return False


def _source_identities(
    snapshot: AcousticSceneSnapshot,
) -> tuple[HybridSourceIdentity, ...]:
    return tuple(
        HybridSourceIdentity(
            source_entity_id=item.source_entity_id,
            source_binding_sha256=_model_hash(item),
            source_entity_sha256=item.source_entity_sha256,
            equipment_definition_sha256=item.equipment_definition_sha256,
            r110_compiled_source_sha256=item.r110_compiled_source_sha256,
            directivity_dataset_sha256=item.directivity_dataset_sha256,
        )
        for item in snapshot.sources
    )


def _receiver_identities(
    snapshot: AcousticSceneSnapshot,
) -> tuple[HybridReceiverIdentity, ...]:
    return tuple(
        HybridReceiverIdentity(
            receiver_id=item.receiver_id,
            entity_id=item.entity_id,
            receiver_binding_sha256=_model_hash(item),
        )
        for item in snapshot.receivers
    )


def _environment_identity(
    snapshot: AcousticSceneSnapshot,
) -> HybridEnvironmentIdentity:
    if snapshot.environment is None:
        raise ValueError(
            'R160 hybrid result requires exact environment authority'
        )
    return HybridEnvironmentIdentity(
        environment_binding_sha256=_model_hash(snapshot.environment),
        environment_authority_ref=snapshot.environment.authority,
        sound_speed_m_s=snapshot.environment.sound_speed_m_s,
        sound_speed_source_authority=(
            snapshot.environment.sound_speed_source_authority
        ),
    )


def _request_ref(
    request: AcousticPredictionRequest,
) -> HybridPredictionRequestRef:
    return HybridPredictionRequestRef(
        request_id=request.request_id,
        request_semantic_sha256=request.request_semantic_sha256,
        deterministic_input_hash=request.deterministic_input_hash,
        model_solver_role_id=request.model_solver_role_id,
        requested_frequency_domain=request.requested_frequency_domain,
        requested_observables=request.requested_observables,
    )


def _result_ref(
    result: AcousticSolverResultEnvelope,
) -> HybridSolverResultRef:
    return HybridSolverResultRef(
        result_id=result.result_id,
        semantic_sha256=result.semantic_sha256,
        dispatch_binding_id=result.dispatch_binding_id,
        dispatch_binding_sha256=result.dispatch_binding_sha256,
        prediction_request_id=result.prediction_request_id,
        prediction_request_semantic_sha256=(
            result.prediction_request_semantic_sha256
        ),
        prediction_deterministic_input_hash=(
            result.prediction_deterministic_input_hash
        ),
        adapter_descriptor_id=result.adapter_descriptor_id,
        adapter_descriptor_semantic_sha256=(
            result.adapter_descriptor_semantic_sha256
        ),
        deterministic_solver_input_hash=result.deterministic_solver_input_hash,
        solver_implementation_ref=result.solver_implementation_ref,
        solver_configuration_ref=result.solver_configuration_ref,
        artifacts=result.artifacts,
    )



def _validity(
    *,
    result: AcousticSolverResultEnvelope,
    artifact: AcousticSolverObservableArtifact,
    observable_type: HybridObservableType,
    phase_capability: HybridPhaseCapability,
    evidence_state: HybridEvidenceState,
) -> HybridObservableValidity:
    return HybridObservableValidity(
        frequency_domain=artifact.valid_frequency_domain,
        observable_type=observable_type,
        source_observable=artifact.observable,
        phase_capability=phase_capability,
        solver_result_id=result.result_id,
        solver_result_sha256=result.semantic_sha256,
        adapter_descriptor_id=result.adapter_descriptor_id,
        adapter_descriptor_sha256=(
            result.adapter_descriptor_semantic_sha256
        ),
        solver_implementation_ref=result.solver_implementation_ref,
        solver_configuration_ref=result.solver_configuration_ref,
        evidence_state=evidence_state,
    )


def _read_payload(
    resolver: ExternalPayloadResolver,
    ref: ExactExternalAuthorityRef,
    *,
    label: str,
) -> Any:
    try:
        payload = resolver(ref)
    except (OSError, ValueError, KeyError) as exc:
        raise ValueError(f'{label} exact external artifact is unavailable') from exc
    if payload is None:
        raise ValueError(f'{label} exact external artifact is unavailable')
    return payload


def _coherent_component(
    *,
    snapshot: AcousticSceneSnapshot,
    result: AcousticSolverResultEnvelope,
    artifact: AcousticSolverObservableArtifact,
    payload_resolver: ExternalPayloadResolver,
    evidence_state: HybridEvidenceState,
) -> CoherentTransfer:
    payload = _read_payload(
        payload_resolver,
        artifact.artifact_authority,
        label='coherent transfer',
    )
    if not isinstance(payload, dict):
        raise ValueError('coherent transfer artifact payload must be an object')
    if payload.get('quantity_type') != 'complex_pressure':
        raise ValueError(
            'magnitude-only or non-complex evidence cannot become CoherentTransfer'
        )
    representation = payload.get('complex_representation')
    if not isinstance(representation, dict):
        raise ValueError('CoherentTransfer requires complex representation metadata')
    complex_form = representation.get('form')
    phasor_convention = representation.get('phasor_convention')
    analysis_kernel = representation.get('analysis_fourier_kernel')
    if not all(
        isinstance(value, str) and value
        for value in (complex_form, phasor_convention, analysis_kernel)
    ):
        raise ValueError(
            'CoherentTransfer requires explicit complex/phase conventions'
        )
    if payload.get('units') != 'Pa':
        raise ValueError('complex pressure CoherentTransfer requires Pa units')
    reference = payload.get('reference')
    if not isinstance(reference, str) or not reference:
        raise ValueError('CoherentTransfer requires explicit pressure reference')

    try:
        payload_domain = FrequencyDomain.model_validate(payload.get('valid_domain'))
    except Exception as exc:
        raise ValueError(
            'CoherentTransfer artifact valid-domain metadata is invalid'
        ) from exc
    if payload_domain != artifact.valid_frequency_domain:
        raise ValueError(
            'CoherentTransfer artifact/result validity domains do not match'
        )

    expected_receiver_order = [
        {
            'receiver_id': item.receiver_id,
            'entity_id': item.entity_id,
            'position_m': [
                float(item.world_position.x_m),
                float(item.world_position.y_m),
                float(item.world_position.z_m),
            ],
        }
        for item in snapshot.receivers
    ]
    if payload.get('receiver_identity_order') != expected_receiver_order:
        raise ValueError(
            'CoherentTransfer receiver identity/order does not match snapshot'
        )

    source_authority = payload.get('source_authority')
    if not isinstance(source_authority, dict):
        raise ValueError('CoherentTransfer source authority is missing')
    source_hash = source_authority.get('r110_compiled_source_sha256')
    matched_sources = tuple(
        item.source_entity_id
        for item in snapshot.sources
        if item.r110_compiled_source_sha256 == source_hash
    )
    if len(matched_sources) != 1:
        raise ValueError(
            'CoherentTransfer source authority does not resolve uniquely in snapshot'
        )

    axis = payload.get('frequency_axis_hz')
    if (
        not isinstance(axis, list)
        or not axis
        or any(not isinstance(value, (int, float)) for value in axis)
    ):
        raise ValueError('CoherentTransfer requires explicit frequency axis')
    frequency_axis = tuple(float(value) for value in axis)
    if frequency_axis != tuple(sorted(set(frequency_axis))):
        raise ValueError(
            'CoherentTransfer frequency axis must be unique and sorted'
        )
    if any(
        not artifact.valid_frequency_domain.contains(value)
        for value in frequency_axis
    ):
        raise ValueError(
            'CoherentTransfer frequency sample lies outside declared validity'
        )

    real = payload.get('pressure_real_pa')
    imag = payload.get('pressure_imag_pa')
    if not isinstance(real, list) or not isinstance(imag, list) or len(real) != len(imag):
        raise ValueError('CoherentTransfer requires paired real/imag pressure arrays')
    if len(real) != len(snapshot.receivers):
        raise ValueError(
            'CoherentTransfer pressure rows do not match receiver identity/order'
        )
    for real_row, imag_row in zip(real, imag, strict=True):
        if (
            not isinstance(real_row, list)
            or not isinstance(imag_row, list)
            or len(real_row) != len(frequency_axis)
            or len(imag_row) != len(frequency_axis)
        ):
            raise ValueError(
                'CoherentTransfer pressure arrays do not match frequency axis'
            )

    return CoherentTransfer(
        artifact_authority=artifact.artifact_authority,
        encoding_schema_ref=artifact.encoding_schema_ref,
        source_entity_ids=matched_sources,
        receiver_identity_order=tuple(
            item.receiver_id for item in snapshot.receivers
        ),
        units='Pa',
        reference=reference,
        complex_form=complex_form,
        phasor_convention=phasor_convention,
        analysis_fourier_kernel=analysis_kernel,
        validity=_validity(
            result=result,
            artifact=artifact,
            observable_type='coherent_transfer',
            phase_capability='COMPLEX_EXPLICIT_REFERENCE',
            evidence_state=evidence_state,
        ),
    )


def _path_component(
    *,
    snapshot: AcousticSceneSnapshot,
    result: AcousticSolverResultEnvelope,
    artifact_manifest: AcousticSolverObservableArtifact,
    path_artifact: DeterministicPathArtifact,
    evidence_state: HybridEvidenceState,
) -> DeterministicPathSet:
    if path_artifact.as_external_ref() != artifact_manifest.artifact_authority:
        raise ValueError(
            'DeterministicPathSet exact R150 artifact ref does not match result'
        )
    if (
        path_artifact.snapshot_id != snapshot.snapshot_id
        or path_artifact.snapshot_sha256 != snapshot.semantic_sha256
        or path_artifact.prediction_request_id != result.prediction_request_id
        or path_artifact.prediction_request_sha256
        != result.prediction_request_semantic_sha256
        or path_artifact.dispatch_binding_id != result.dispatch_binding_id
        or path_artifact.dispatch_binding_sha256
        != result.dispatch_binding_sha256
        or path_artifact.execution_id != result.execution_id
        or path_artifact.solver_implementation_ref
        != result.solver_implementation_ref
        or path_artifact.solver_configuration_ref
        != result.solver_configuration_ref
    ):
        raise ValueError(
            'DeterministicPathSet R150 artifact/result compatibility mismatch'
        )
    if path_artifact.frequency_domain != artifact_manifest.valid_frequency_domain:
        raise ValueError(
            'DeterministicPathSet artifact/result validity domains do not match'
        )
    if path_artifact.coherent_phase_authority != 'UNAVAILABLE_NOT_SYNTHESIZED':
        raise ValueError(
            'phase-bearing DeterministicPathSet requires a different authority'
        )
    source_ids = {item.source_entity_id for item in snapshot.sources}
    receiver_ids = {item.receiver_id for item in snapshot.receivers}
    if any(item.source_entity_id not in source_ids for item in path_artifact.paths):
        raise ValueError(
            'DeterministicPathSet contains source outside exact snapshot'
        )
    if any(item.receiver_id not in receiver_ids for item in path_artifact.paths):
        raise ValueError(
            'DeterministicPathSet contains receiver outside exact snapshot'
        )
    direct_count = sum(
        item.path_type == 'direct' for item in path_artifact.paths
    )
    first_count = sum(
        item.path_type == 'specular_reflection'
        for item in path_artifact.paths
    )
    path_types = tuple(
        sorted(
            {item.path_type for item in path_artifact.paths},
            key=lambda value: 0 if value == 'direct' else 1,
        )
    )
    return DeterministicPathSet(
        artifact_authority=artifact_manifest.artifact_authority,
        encoding_schema_ref=artifact_manifest.encoding_schema_ref,
        path_scope=path_artifact.path_scope,
        direct_path_count=direct_count,
        first_order_specular_path_count=first_count,
        path_types_present=path_types,
        validity=_validity(
            result=result,
            artifact=artifact_manifest,
            observable_type='deterministic_paths',
            phase_capability='UNAVAILABLE_NOT_SYNTHESIZED',
            evidence_state=evidence_state,
        ),
    )


def _late_component(
    *,
    result: AcousticSolverResultEnvelope,
    artifact: AcousticSolverObservableArtifact,
    evidence_state: HybridEvidenceState,
) -> LateEnergyDecay:
    if artifact.encoding_schema_ref != R160_LATE_ENERGY_ARTIFACT_SCHEMA_REF:
        raise ValueError(
            'R160 LateEnergyDecay requires the canonical late-energy decay '
            'encoding schema; solver artifact declares '
            f'{artifact.encoding_schema_ref.authority_id}@'
            f'{artifact.encoding_schema_ref.authority_version}'
        )
    return LateEnergyDecay(
        state='AVAILABLE',
        reason='exact late-energy-decay solver artifact is referenced',
        artifact_authority=artifact.artifact_authority,
        encoding_schema_ref=artifact.encoding_schema_ref,
        validity=_validity(
            result=result,
            artifact=artifact,
            observable_type='late_energy_decay',
            phase_capability='NOT_APPLICABLE',
            evidence_state=evidence_state,
        ),
    )


def _validate_partition_policy(
    *,
    policy: HybridStitchingPolicy,
    coherent: CoherentTransfer | None,
    paths: DeterministicPathSet | None,
    late: LateEnergyDecay,
) -> None:
    if policy.mode != 'frequency_partition_no_blend':
        return
    domains: dict[HybridObservableType, FrequencyDomain] = {}
    if coherent is not None:
        domains['coherent_transfer'] = coherent.validity.frequency_domain
    if paths is not None:
        domains['deterministic_paths'] = paths.validity.frequency_domain
    if late.state == 'AVAILABLE' and late.validity is not None:
        domains['late_energy_decay'] = late.validity.frequency_domain

    covered = {item.component for item in policy.frequency_partitions}
    if covered != set(domains):
        raise ValueError(
            'frequency_partition_no_blend must explicitly partition every '
            'available hybrid component'
        )
    for partition in policy.frequency_partitions:
        domain = domains.get(partition.component)
        if domain is None or not _domain_contains(
            domain,
            partition.frequency_domain,
        ):
            raise ValueError(
                'hybrid frequency partition exceeds component validity domain'
            )


def build_hybrid_acoustic_result(
    *,
    snapshot: AcousticSceneSnapshot,
    prediction_requests: Sequence[AcousticPredictionRequest],
    solver_results: Sequence[AcousticSolverResultEnvelope],
    stitching_policy: HybridStitchingPolicy,
    deterministic_path_artifacts: Sequence[DeterministicPathArtifact] = (),
    external_payload_resolver: ExternalPayloadResolver,
    late_energy_decay_state: Literal[
        'UNAVAILABLE',
        'UNSUPPORTED',
        'NOT_PROVIDED',
    ] = 'NOT_PROVIDED',
    late_energy_decay_reason: str = (
        'no late-energy-decay solver artifact was provided'
    ),
) -> HybridAcousticResult:
    snapshot = AcousticSceneSnapshot.model_validate(
        snapshot.model_dump(mode='python')
    )
    policy = HybridStitchingPolicy.model_validate(
        stitching_policy.model_dump(mode='python')
    )
    requests = tuple(
        sorted(
            (
                AcousticPredictionRequest.model_validate(
                    item.model_dump(mode='python')
                )
                for item in prediction_requests
            ),
            key=lambda item: item.request_id,
        )
    )
    results = tuple(
        sorted(
            (
                AcousticSolverResultEnvelope.model_validate(
                    item.model_dump(mode='python')
                )
                for item in solver_results
            ),
            key=lambda item: item.result_id,
        )
    )
    if not requests:
        raise ValueError('R160 hybrid result requires prediction request refs')
    if not results:
        raise ValueError('R160 hybrid result requires solver result refs')
    if not snapshot.sources or not snapshot.receivers:
        raise ValueError(
            'R160 hybrid result requires exact source and receiver identities'
        )
    _environment_identity(snapshot)

    request_by_id = {item.request_id: item for item in requests}
    if len(request_by_id) != len(requests):
        raise ValueError('R160 prediction request set contains duplicates')
    for request in requests:
        if (
            request.acoustic_scene_snapshot_id != snapshot.snapshot_id
            or request.acoustic_scene_snapshot_sha256
            != snapshot.semantic_sha256
        ):
            raise ValueError(
                'R160 prediction requests must bind the same exact snapshot'
            )

    coherent_candidates: list[
        tuple[AcousticSolverResultEnvelope, AcousticSolverObservableArtifact]
    ] = []
    path_candidates: list[
        tuple[AcousticSolverResultEnvelope, AcousticSolverObservableArtifact]
    ] = []
    late_candidates: list[
        tuple[AcousticSolverResultEnvelope, AcousticSolverObservableArtifact]
    ] = []

    for result in results:
        request = request_by_id.get(result.prediction_request_id)
        if request is None:
            raise ValueError(
                'solver result prediction request is absent from hybrid request set'
            )
        if (
            result.prediction_request_semantic_sha256
            != request.request_semantic_sha256
            or result.prediction_deterministic_input_hash
            != request.deterministic_input_hash
            or result.acoustic_scene_snapshot_id != snapshot.snapshot_id
            or result.acoustic_scene_snapshot_sha256 != snapshot.semantic_sha256
        ):
            raise ValueError(
                'solver result is stale or incompatible with exact hybrid request/snapshot'
            )
        contributed = False
        for artifact in result.artifacts:
            if artifact.observable == 'complex_pressure':
                coherent_candidates.append((result, artifact))
                contributed = True
            elif artifact.observable == 'deterministic_paths':
                path_candidates.append((result, artifact))
                contributed = True
            elif artifact.observable == 'late_energy_decay':
                late_candidates.append((result, artifact))
                contributed = True
            elif artifact.observable in {'magnitude_response', 'phase_response'}:
                raise ValueError(
                    'magnitude-only/phase-only solver output cannot be promoted '
                    'to CoherentTransfer'
                )
            else:
                raise ValueError(
                    'solver result observable is unsupported by R160 foundation: '
                    f'{artifact.observable}'
                )
        if not contributed:
            raise ValueError(
                'participating solver result has no R160 typed observable'
            )

    if len(coherent_candidates) > 1:
        raise ValueError(
            'R160 foundation accepts at most one CoherentTransfer component'
        )
    if len(path_candidates) > 1:
        raise ValueError(
            'R160 foundation accepts at most one DeterministicPathSet component'
        )
    if len(late_candidates) > 1:
        raise ValueError(
            'R160 foundation accepts at most one LateEnergyDecay component'
        )

    coherent: CoherentTransfer | None = None
    if coherent_candidates:
        result, artifact = coherent_candidates[0]
        coherent = _coherent_component(
            snapshot=snapshot,
            result=result,
            artifact=artifact,
            payload_resolver=external_payload_resolver,
            evidence_state='EXECUTED_UNVALIDATED',
        )

    path_by_ref = {
        item.as_external_ref().authority_id: DeterministicPathArtifact.model_validate(
            item.model_dump(mode='python')
        )
        for item in deterministic_path_artifacts
    }
    paths: DeterministicPathSet | None = None
    if path_candidates:
        result, artifact_manifest = path_candidates[0]
        path_artifact = path_by_ref.get(
            artifact_manifest.artifact_authority.authority_id
        )
        if path_artifact is None:
            raise ValueError(
                'R160 DeterministicPathSet requires exact R150 artifact resolution'
            )
        paths = _path_component(
            snapshot=snapshot,
            result=result,
            artifact_manifest=artifact_manifest,
            path_artifact=path_artifact,
            evidence_state='EXECUTED_UNVALIDATED',
        )

    if late_candidates:
        result, artifact = late_candidates[0]
        late = _late_component(
            result=result,
            artifact=artifact,
            evidence_state='EXECUTED_UNVALIDATED',
        )
    else:
        late = LateEnergyDecay(
            state=late_energy_decay_state,
            reason=late_energy_decay_reason,
        )

    _validate_partition_policy(
        policy=policy,
        coherent=coherent,
        paths=paths,
        late=late,
    )

    available_domains = []
    if coherent is not None:
        available_domains.append(coherent.validity.frequency_domain)
    if paths is not None:
        available_domains.append(paths.validity.frequency_domain)
    if late.state == 'AVAILABLE' and late.validity is not None:
        available_domains.append(late.validity.frequency_domain)
    if len(available_domains) < 2:
        frequency_relationship = 'SINGLE_COMPONENT'
    elif any(
        _domains_overlap(first, second)
        for index, first in enumerate(available_domains)
        for second in available_domains[index + 1 :]
    ):
        frequency_relationship = 'OVERLAPPING_COMPONENT_VALIDITY'
    else:
        frequency_relationship = 'DISJOINT_COMPONENT_VALIDITY'

    core = {
        'schema_version': HYBRID_RESULT_SCHEMA_VERSION,
        'authority_version': HYBRID_RESULT_AUTHORITY_VERSION,
        'acoustic_scene_snapshot_id': snapshot.snapshot_id,
        'acoustic_scene_snapshot_sha256': snapshot.semantic_sha256,
        'scene_revision_id': snapshot.scene_revision_id,
        'scene_content_hash': snapshot.scene_content_hash,
        'semantic_geometry_id': snapshot.semantic_geometry_id,
        'semantic_geometry_sha256': snapshot.semantic_geometry_sha256,
        'source_identities': [
            item.model_dump(mode='json')
            for item in _source_identities(snapshot)
        ],
        'receiver_identity_order': [
            item.model_dump(mode='json')
            for item in _receiver_identities(snapshot)
        ],
        'environment_identity': _environment_identity(snapshot).model_dump(
            mode='json'
        ),
        'prediction_requests': [
            _request_ref(item).model_dump(mode='json') for item in requests
        ],
        'participating_solver_results': [
            _result_ref(item).model_dump(mode='json') for item in results
        ],
        'coherent_transfer': (
            None if coherent is None else coherent.model_dump(mode='json')
        ),
        'deterministic_path_set': (
            None if paths is None else paths.model_dump(mode='json')
        ),
        'late_energy_decay': late.model_dump(mode='json'),
        'frequency_domain_relationship': frequency_relationship,
        'stitching_policy_ref': policy.as_external_ref().model_dump(mode='json'),
        'algorithm_id': HYBRID_ALGORITHM_ID,
        'algorithm_version': HYBRID_ALGORITHM_VERSION,
    }
    digest = _semantic_hash(core)
    return HybridAcousticResult(
        hybrid_result_id=f'hybrid-acoustic-result:{digest}',
        semantic_sha256=digest,
        **core,
    )


def _composition_result_ref(
    hybrid: HybridAcousticResult,
    result_id: str,
) -> HybridSolverResultRef:
    for item in hybrid.participating_solver_results:
        if item.result_id == result_id:
            return item
    raise ValueError('hybrid composition references non-participating result')


def _composition_artifact_semantics(
    path_artifact: DeterministicPathArtifact,
) -> tuple[HybridPhysicalComponent, ...]:
    semantics: list[HybridPhysicalComponent] = []
    if any(item.path_type == 'direct' for item in path_artifact.paths):
        semantics.append('direct')
    if any(item.path_type == 'specular_reflection' for item in path_artifact.paths):
        semantics.append('deterministic_early_reflection')
    if not semantics:
        raise ValueError(
            'hybrid composition requires at least one deterministic GA path'
        )
    return tuple(semantics)


def build_hybrid_composition_spec(
    *,
    hybrid: HybridAcousticResult,
    deterministic_path_artifact: DeterministicPathArtifact,
    observable: HybridCompositionObservable,
    requested_frequency_domain: FrequencyDomain,
    crossover_policy: HybridCrossoverPolicy,
    double_count_exclusion_policy: HybridDoubleCountExclusionPolicy,
) -> HybridCompositionSpec:
    """Bind exact R130/R150 artifacts to one explicit composition request."""

    hybrid = HybridAcousticResult.model_validate(
        hybrid.model_dump(mode='python')
    )
    if hybrid.coherent_transfer is None or hybrid.deterministic_path_set is None:
        raise ValueError(
            'bounded hybrid composition requires exact wave and GA components'
        )
    coherent = hybrid.coherent_transfer
    paths = hybrid.deterministic_path_set
    path_artifact = DeterministicPathArtifact.model_validate(
        deterministic_path_artifact.model_dump(mode='python')
    )
    if path_artifact.as_external_ref() != paths.artifact_authority:
        raise ValueError(
            'hybrid composition GA artifact identity does not match R160 component'
        )
    if (
        path_artifact.snapshot_id != hybrid.acoustic_scene_snapshot_id
        or path_artifact.snapshot_sha256
        != hybrid.acoustic_scene_snapshot_sha256
    ):
        raise ValueError(
            'hybrid composition GA artifact binds an incompatible scene'
        )
    ga_source_ids = {
        item.source_entity_id for item in path_artifact.paths
    } | {
        item.source_entity_id for item in path_artifact.rejected_candidates
    }
    if ga_source_ids != set(coherent.source_entity_ids):
        raise ValueError(
            'hybrid composition wave/GA source identity mismatch'
        )
    ga_receiver_ids = {
        item.receiver_id for item in path_artifact.paths
    } | {
        item.receiver_id for item in path_artifact.rejected_candidates
    }
    if ga_receiver_ids != set(coherent.receiver_identity_order):
        raise ValueError(
            'hybrid composition wave/GA receiver identity mismatch'
        )

    wave_result = _composition_result_ref(
        hybrid,
        coherent.validity.solver_result_id,
    )
    ga_result = _composition_result_ref(
        hybrid,
        paths.validity.solver_result_id,
    )
    wave_band = coherent.validity.frequency_domain
    ga_band = paths.validity.frequency_domain
    overlap = _domain_intersection(wave_band, ga_band)

    policy = HybridCrossoverPolicy.model_validate(
        crossover_policy.model_dump(mode='python')
    )
    if policy.mode == 'preserve_overlap_no_blend' and overlap is None:
        raise ValueError(
            'preserve_overlap_no_blend requires a real wave/GA overlap domain'
        )
    if policy.mode == 'explicit_partition_no_blend':
        if overlap is None:
            raise ValueError(
                'explicit crossover requires a real wave/GA overlap domain'
            )
        assert policy.crossover_hz is not None
        if not overlap.contains(policy.crossover_hz):
            raise ValueError(
                'explicit crossover must lie inside the exact overlap domain'
            )
    if policy.mode == 'preserve_gap_no_fill' and overlap is not None:
        raise ValueError(
            'preserve_gap_no_fill is only valid when wave/GA bands are disjoint'
        )
    requested = FrequencyDomain.model_validate(
        requested_frequency_domain.model_dump(mode='python')
    )
    if (
        _domain_intersection(requested, wave_band) is None
        and _domain_intersection(requested, ga_band) is None
    ):
        raise ValueError(
            'requested composition domain does not intersect either source band'
        )

    normalization = HybridNormalizationReferenceConvention(
        wave_units=coherent.units,
        wave_reference=coherent.reference,
    )
    wave_input = HybridCompositionArtifactIdentity(
        backend='wave',
        solver_result_id=wave_result.result_id,
        solver_result_sha256=wave_result.semantic_sha256,
        artifact_authority=coherent.artifact_authority,
        encoding_schema_ref=coherent.encoding_schema_ref,
        valid_band=wave_band,
        quantity=coherent.quantity,
        units=coherent.units,
        reference=coherent.reference,
        component_semantics=('full_field',),
    )
    ga_input = HybridCompositionArtifactIdentity(
        backend='deterministic_ga',
        solver_result_id=ga_result.result_id,
        solver_result_sha256=ga_result.semantic_sha256,
        artifact_authority=paths.artifact_authority,
        encoding_schema_ref=paths.encoding_schema_ref,
        valid_band=ga_band,
        quantity=paths.contribution_quantity,
        units='relative_energy_per_m2',
        reference='solver_native_relative_energy_transport',
        component_semantics=_composition_artifact_semantics(path_artifact),
    )
    core = {
        'authority_version': HYBRID_COMPOSITION_SPEC_AUTHORITY_VERSION,
        'wave_input': wave_input.model_dump(mode='json'),
        'ga_input': ga_input.model_dump(mode='json'),
        'acoustic_scene_snapshot_id': hybrid.acoustic_scene_snapshot_id,
        'acoustic_scene_snapshot_sha256': hybrid.acoustic_scene_snapshot_sha256,
        'scene_revision_id': hybrid.scene_revision_id,
        'scene_content_hash': hybrid.scene_content_hash,
        'source_identity_sha256': [
            item.source_binding_sha256 for item in hybrid.source_identities
        ],
        'receiver_identity_sha256': [
            item.receiver_binding_sha256
            for item in hybrid.receiver_identity_order
        ],
        'environment_binding_sha256': (
            hybrid.environment_identity.environment_binding_sha256
        ),
        'observable': observable,
        'requested_frequency_domain': requested.model_dump(mode='json'),
        'wave_valid_band': wave_band.model_dump(mode='json'),
        'ga_valid_band': ga_band.model_dump(mode='json'),
        'overlap_domain': (
            None if overlap is None else overlap.model_dump(mode='json')
        ),
        'crossover_policy': policy.model_dump(mode='json'),
        'normalization_reference_convention': normalization.model_dump(
            mode='json'
        ),
        'double_count_exclusion_policy': (
            double_count_exclusion_policy.model_dump(mode='json')
        ),
        'capability_requirements': list(_composition_requirements(observable)),
    }
    digest = _semantic_hash(core)
    return HybridCompositionSpec(
        composition_spec_id=f'hybrid-composition-spec:{digest}',
        semantic_sha256=digest,
        **core,
    )


_COMPOSITION_OBSERVABLE_ORDER: tuple[HybridCompositionObservable, ...] = (
    'magnitude_energy',
    'coherent_phase',
    'arrival_timing',
    'deterministic_path_identity',
    'late_decay',
)


def _composition_capabilities(
    *,
    hybrid: HybridAcousticResult,
    spec: HybridCompositionSpec,
    path_artifact: DeterministicPathArtifact,
) -> tuple[
    tuple[HybridCompositionObservable, ...],
    tuple[HybridUnsupportedObservable, ...],
    tuple[int, ...],
]:
    supported: list[HybridCompositionObservable] = []
    unsupported: list[HybridUnsupportedObservable] = []

    if path_artifact.paths:
        supported.append('deterministic_path_identity')
    else:
        unsupported.append(
            HybridUnsupportedObservable(
                observable='deterministic_path_identity',
                reason_code='NO_DETERMINISTIC_PATHS',
                detail='R150 artifact contains no deterministic paths',
            )
        )

    if hybrid.late_energy_decay.state == 'AVAILABLE':
        supported.append('late_decay')
    else:
        unsupported.append(
            HybridUnsupportedObservable(
                observable='late_decay',
                reason_code='MISSING_LATE_DECAY_AUTHORITY',
                detail=(
                    'bounded early-specular R150 evidence is not a '
                    'LateEnergyDecay authority'
                ),
            )
        )

    if (
        spec.double_count_exclusion_policy.mode
        == 'require_disjoint_component_ownership'
        and 'full_field' in spec.wave_input.component_semantics
        and any(
            component in spec.ga_input.component_semantics
            for component in ('direct', 'deterministic_early_reflection')
        )
    ):
        magnitude_reason = HybridUnsupportedObservable(
            observable='magnitude_energy',
            reason_code='DOUBLE_COUNT_AMBIGUITY',
            detail=(
                'double-count ambiguity: wave full_field may already contain '
                'the direct/deterministic-early components represented by GA; '
                'no exact subtraction authority exists'
            ),
        )
    else:
        magnitude_reason = HybridUnsupportedObservable(
            observable='magnitude_energy',
            reason_code='INCOMPATIBLE_QUANTITY_REFERENCE',
            detail=(
                'wave complex pressure in Pa and GA relative energy transport '
                'do not share an exact numeric normalization/reference'
            ),
        )
    unsupported.append(magnitude_reason)
    unsupported.append(
        HybridUnsupportedObservable(
            observable='coherent_phase',
            reason_code='MISSING_COHERENT_PHASE_AUTHORITY',
            detail=(
                'missing coherent phase authority: R150 path length/material '
                'energy does not authorize coherent reflection phase'
            ),
        )
    )
    unsupported.append(
        HybridUnsupportedObservable(
            observable='arrival_timing',
            reason_code='MISSING_SHARED_TIME_ORIGIN',
            detail=(
                'wave and GA artifacts do not expose one shared explicit time '
                'origin/propagation timing convention'
            ),
        )
    )

    supported_set = set(supported)
    unsupported_by_name = {item.observable: item for item in unsupported}
    ordered_supported = tuple(
        item for item in _COMPOSITION_OBSERVABLE_ORDER if item in supported_set
    )
    ordered_unsupported = tuple(
        unsupported_by_name[item]
        for item in _COMPOSITION_OBSERVABLE_ORDER
        if item in unsupported_by_name
    )
    reflection_orders = tuple(
        sorted(
            {
                0
                if item.path_type == 'direct'
                else len(item.ordered_interaction_surface_ids)
                for item in path_artifact.paths
            }
        )
    )
    return ordered_supported, ordered_unsupported, reflection_orders


def compose_hybrid_acoustic_result(
    *,
    hybrid: HybridAcousticResult,
    composition_spec: HybridCompositionSpec,
    deterministic_path_artifact: DeterministicPathArtifact,
) -> HybridAcousticResult:
    """Attach a fail-closed bounded composition decision to R160 typed authority."""

    hybrid = HybridAcousticResult.model_validate(
        hybrid.model_dump(mode='python')
    )
    if hybrid.composition_spec is not None:
        raise ValueError(
            'bounded hybrid composition requires an uncomposed R160 foundation result'
        )

    path_artifact = DeterministicPathArtifact.model_validate(
        deterministic_path_artifact.model_dump(mode='python')
    )
    spec = HybridCompositionSpec.model_validate(
        composition_spec.model_dump(mode='python')
    )
    expected_spec = build_hybrid_composition_spec(
        hybrid=hybrid,
        deterministic_path_artifact=path_artifact,
        observable=spec.observable,
        requested_frequency_domain=spec.requested_frequency_domain,
        crossover_policy=spec.crossover_policy,
        double_count_exclusion_policy=spec.double_count_exclusion_policy,
    )
    if expected_spec != spec:
        raise ValueError(
            'hybrid composition spec is stale or incompatible with exact inputs'
        )

    supported, unsupported, path_orders = _composition_capabilities(
        hybrid=hybrid,
        spec=spec,
        path_artifact=path_artifact,
    )
    unsupported_by_name = {item.observable: item for item in unsupported}
    if spec.observable not in supported:
        reason = unsupported_by_name[spec.observable]
        raise ValueError(reason.detail)

    if spec.observable == 'deterministic_path_identity':
        valid_domain = _domain_intersection(
            spec.requested_frequency_domain,
            spec.ga_valid_band,
        )
    elif spec.observable == 'late_decay':
        late_validity = hybrid.late_energy_decay.validity
        valid_domain = (
            None
            if late_validity is None
            else _domain_intersection(
                spec.requested_frequency_domain,
                late_validity.frequency_domain,
            )
        )
    else:
        valid_domain = None
    if valid_domain is None:
        raise ValueError(
            'requested observable has no valid domain inside requested frequencies'
        )

    decision = HybridCompositionDecision(
        status='ELIGIBLE_BOUNDED_NO_NUMERIC_BLEND',
        composition_spec_sha256=spec.semantic_sha256,
        requested_observable=spec.observable,
        supported_observables=supported,
        unsupported_observables=unsupported,
        source_valid_domains=(spec.wave_valid_band, spec.ga_valid_band),
        valid_domains=(valid_domain,),
        overlap_domain=spec.overlap_domain,
        gap_domains=_gap_within_requested_domain(
            spec.wave_valid_band,
            spec.ga_valid_band,
            spec.requested_frequency_domain,
        ),
        path_reflection_orders_present=path_orders,
        double_count_decision='NOT_APPLICABLE_NON_NUMERIC_OBSERVABLE',
        evidence=(
            'exact R130 complex-pressure artifact identity resolved',
            'exact R150 deterministic-path artifact identity resolved',
            'scene/source/receiver/environment compatibility inherited and rechecked',
            'requested observable passed observable-specific capability gate',
            'no cross-backend numeric addition, interpolation, or extrapolation performed',
        ),
        approximation_error=HybridApproximationErrorMetadata(
            note=(
                'authority-level composition only; no numeric cross-backend '
                'operation exists from which to claim an error bound'
            )
        ),
    )

    core = hybrid.model_dump(
        mode='json',
        exclude={
            'hybrid_result_id',
            'semantic_sha256',
            'composition_spec',
            'composition_decision',
        },
    )
    core['composition_spec'] = spec.model_dump(mode='json')
    core['composition_decision'] = decision.model_dump(mode='json')
    digest = _semantic_hash(core)
    return HybridAcousticResult(
        hybrid_result_id=f'hybrid-acoustic-result:{digest}',
        semantic_sha256=digest,
        **core,
    )


class CadHybridAcousticResultRepository:
    """Append-only R160 persistence with exact source re-resolution."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        snapshot_request_resolver: SnapshotRequestResolver,
        solver_result_resolver: SolverResultResolver,
        deterministic_path_resolver: DeterministicPathResolver,
        external_payload_resolver: ExternalPayloadResolver,
    ) -> None:
        self.scene_repository = scene_repository
        self.snapshot_request_resolver = snapshot_request_resolver
        self.solver_result_resolver = solver_result_resolver
        self.deterministic_path_resolver = deterministic_path_resolver
        self.external_payload_resolver = external_payload_resolver
        self.path = Path(scene_repository.path)
        for label, resolver in (
            ('snapshot/request', snapshot_request_resolver),
            ('solver result', solver_result_resolver),
            ('deterministic path', deterministic_path_resolver),
        ):
            if Path(resolver.path) != self.path:
                raise ValueError(
                    f'R160 hybrid and {label} repositories must share one '
                    'native CAD database'
                )
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_hybrid_stitching_policies', 'cad_hybrid_acoustic_results')

    def get_policy(
        self,
        policy_id: str,
    ) -> HybridStitchingPolicy | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_hybrid_stitching_policies
                WHERE policy_id=?
                """,
                (policy_id,),
            ).fetchone()
        if row is None:
            return None
        return HybridStitchingPolicy.model_validate_json(row['payload_json'])

    def _resolve_result(
        self,
        ref: HybridSolverResultRef,
    ) -> AcousticSolverResultEnvelope:
        result = self.solver_result_resolver.get(ref.result_id)
        if result is None:
            raise ValueError(
                'R160 references missing/stale AcousticSolverResultEnvelope'
            )
        if _result_ref(result) != ref:
            raise ValueError(
                'R160 solver result exact identity/provenance mismatch'
            )
        return result

    def _validate(
        self,
        hybrid: HybridAcousticResult,
        *,
        policy: HybridStitchingPolicy | None = None,
    ) -> HybridAcousticResult:
        hybrid = HybridAcousticResult.model_validate(
            hybrid.model_dump(mode='python')
        )
        snapshot = self.snapshot_request_resolver.get_snapshot(
            hybrid.acoustic_scene_snapshot_id
        )
        if (
            snapshot is None
            or snapshot.semantic_sha256
            != hybrid.acoustic_scene_snapshot_sha256
            or snapshot.scene_revision_id != hybrid.scene_revision_id
            or snapshot.scene_content_hash != hybrid.scene_content_hash
            or snapshot.semantic_geometry_id != hybrid.semantic_geometry_id
            or snapshot.semantic_geometry_sha256
            != hybrid.semantic_geometry_sha256
            or _source_identities(snapshot) != hybrid.source_identities
            or _receiver_identities(snapshot) != hybrid.receiver_identity_order
            or _environment_identity(snapshot) != hybrid.environment_identity
        ):
            raise ValueError(
                'R160 exact snapshot/geometry/source/receiver/environment '
                'compatibility failed'
            )

        requests: list[AcousticPredictionRequest] = []
        for ref in hybrid.prediction_requests:
            request = self.snapshot_request_resolver.get_prediction_request(
                ref.request_id
            )
            if request is None or _request_ref(request) != ref:
                raise ValueError(
                    'R160 references missing/stale AcousticPredictionRequest'
                )
            if (
                request.acoustic_scene_snapshot_id != snapshot.snapshot_id
                or request.acoustic_scene_snapshot_sha256
                != snapshot.semantic_sha256
            ):
                raise ValueError(
                    'R160 request no longer resolves to exact snapshot'
                )
            requests.append(request)

        results = [
            self._resolve_result(ref)
            for ref in hybrid.participating_solver_results
        ]

        if policy is None:
            policy = self.get_policy(hybrid.stitching_policy_ref.authority_id)
        if policy is None or policy.as_external_ref() != hybrid.stitching_policy_ref:
            raise ValueError(
                'R160 references missing/mismatched stitching policy authority'
            )

        path_artifacts: list[DeterministicPathArtifact] = []
        if hybrid.deterministic_path_set is not None:
            path_ref = hybrid.deterministic_path_set.artifact_authority
            path = self.deterministic_path_resolver.get(path_ref.authority_id)
            if path is None or path.as_external_ref() != path_ref:
                raise ValueError(
                    'R160 references missing/stale R150 DeterministicPathArtifact'
                )
            path_artifacts.append(path)

        regenerated = build_hybrid_acoustic_result(
            snapshot=snapshot,
            prediction_requests=requests,
            solver_results=results,
            stitching_policy=policy,
            deterministic_path_artifacts=path_artifacts,
            external_payload_resolver=self.external_payload_resolver,
            late_energy_decay_state=(
                'NOT_PROVIDED'
                if hybrid.late_energy_decay.state == 'AVAILABLE'
                else hybrid.late_energy_decay.state
            ),
            late_energy_decay_reason=hybrid.late_energy_decay.reason,
        )
        if hybrid.composition_spec is not None:
            if len(path_artifacts) != 1:
                raise ValueError(
                    'R160 bounded composition requires exact persisted R150 artifact'
                )
            regenerated = compose_hybrid_acoustic_result(
                hybrid=regenerated,
                composition_spec=hybrid.composition_spec,
                deterministic_path_artifact=path_artifacts[0],
            )
        if regenerated != hybrid:
            raise ValueError(
                'R160 hybrid result does not reproduce from exact persisted '
                'authorities'
            )
        return hybrid

    def save(
        self,
        hybrid: HybridAcousticResult,
        *,
        policy: HybridStitchingPolicy,
    ) -> HybridAcousticResult:
        policy = HybridStitchingPolicy.model_validate(
            policy.model_dump(mode='python')
        )
        if policy.as_external_ref() != hybrid.stitching_policy_ref:
            raise ValueError(
                'R160 save policy does not match hybrid policy identity'
            )
        hybrid = self._validate(hybrid, policy=policy)

        with closing(self._connect()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                existing_policy = connection.execute(
                    """
                    SELECT payload_json
                    FROM cad_hybrid_stitching_policies
                    WHERE policy_id=?
                    """,
                    (policy.policy_id,),
                ).fetchone()
                if existing_policy is not None:
                    persisted_policy = HybridStitchingPolicy.model_validate_json(
                        existing_policy['payload_json']
                    )
                    if persisted_policy != policy:
                        raise ValueError(
                            'hybrid stitching policy id exists with '
                            'different semantics'
                        )
                else:
                    connection.execute(
                        """
                        INSERT INTO cad_hybrid_stitching_policies(
                            policy_id,
                            semantic_sha256,
                            mode,
                            payload_json,
                            recorded_at_utc
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            policy.policy_id,
                            policy.semantic_sha256,
                            policy.mode,
                            policy.model_dump_json(),
                            _utc_now(),
                        ),
                    )

                existing = connection.execute(
                    """
                    SELECT payload_json
                    FROM cad_hybrid_acoustic_results
                    WHERE hybrid_result_id=?
                    """,
                    (hybrid.hybrid_result_id,),
                ).fetchone()
                if existing is not None:
                    persisted = HybridAcousticResult.model_validate_json(
                        existing['payload_json']
                    )
                    if persisted != hybrid:
                        raise ValueError(
                            'HybridAcousticResult id exists with '
                            'different semantics'
                        )
                else:
                    connection.execute(
                        """
                        INSERT INTO cad_hybrid_acoustic_results(
                            hybrid_result_id,
                            semantic_sha256,
                            acoustic_scene_snapshot_id,
                            scene_revision_id,
                            stitching_policy_id,
                            payload_json,
                            recorded_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            hybrid.hybrid_result_id,
                            hybrid.semantic_sha256,
                            hybrid.acoustic_scene_snapshot_id,
                            hybrid.scene_revision_id,
                            policy.policy_id,
                            hybrid.model_dump_json(),
                            _utc_now(),
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return hybrid

    def get(
        self,
        hybrid_result_id: str,
        *,
        expected_composition_spec: HybridCompositionSpec | None = None,
    ) -> HybridAcousticResult | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_hybrid_acoustic_results
                WHERE hybrid_result_id=?
                """,
                (hybrid_result_id,),
            ).fetchone()
        if row is None:
            return None
        hybrid = self._validate(
            HybridAcousticResult.model_validate_json(row['payload_json'])
        )
        if expected_composition_spec is not None:
            if (
                hybrid.composition_spec is None
                or hybrid.composition_spec != expected_composition_spec
            ):
                raise ValueError(
                    'R160 persisted hybrid composition is stale for expected spec'
                )
        return hybrid
