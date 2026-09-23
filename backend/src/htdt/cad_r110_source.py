from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_directivity import (
    DirectivityDataset,
    DirectivityDatasetKind,
    DirectivityNormalization,
    validate_directivity_dataset_binding,
)
from .cad_equipment import (
    DirectivityCapabilityTier,
    FrequencyDomain,
    InterpolationMethod,
    EquipmentDefinition,
    SensitivityReference,
)
from .cad_repository import SceneRevision
from .cad_scene import (
    Direction3,
    Position3,
    Quaternion4,
    SceneEntity,
    quaternion_to_matrix3,
)
from .cad_system_variant import (
    EquipmentBindingRef,
    SystemVariant,
    materialize_system_variant,
)


R110_COMPILED_SOURCE_SCHEMA_VERSION = 1
R110_SOURCE_COMPILER_VERSION = 'r110-source-model-compiler-1'
R110_ACOUSTIC_REFERENCE_AUTHORITY = (
    'equipment-acoustic-reference-world-transform-v1'
)
R110_AIM_AXIS_AUTHORITY = 'scene-entity-aim-xyz-v1'

R110SourceCapabilityName = Literal[
    'geometry_reference_point',
    'magnitude_directivity',
    'complex_directivity',
    'coherent_phase',
    'electrical_sensitivity_reference',
    'acoustic_wave_excitation_normalization',
]
R110CapabilityDecision = Literal['SUPPORTED', 'UNSUPPORTED', 'BLOCKED']
R110FrequencyDomainAuthority = Literal[
    'directivity_dataset',
    'equipment_definition',
    'unavailable',
]
R110ReferenceAxisAuthority = Literal[
    'scene-entity-aim-xyz-v1',
    'unavailable',
]
R110ApproximationKind = Literal[
    'magnitude_only_no_phase_synthesis',
    'polar_summary_not_expanded',
    'analytic_without_numeric_evaluator',
    'missing_numeric_directivity_dataset',
    'source_reference_axis_unavailable',
]
R110GeometricDirectivityState = Literal[
    'SUPPORTED_FOR_GEOMETRIC_DIRECTIVITY',
    'UNSUPPORTED',
]
R110ComplexDirectivityState = Literal[
    'SUPPORTED_FOR_COMPLEX_DIRECTIVITY',
    'UNSUPPORTED',
]
R110WaveExcitationState = Literal[
    'BLOCKED_FOR_WAVE_EXCITATION',
    'UNSUPPORTED',
]


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


class R110CapabilityStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    capability: R110SourceCapabilityName
    decision: R110CapabilityDecision
    reason: str = Field(min_length=1)


class R110CompilerUseCaseStates(BaseModel):
    """Purpose-specific readiness; intentionally not a single solver_ready flag."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    geometric_directivity: R110GeometricDirectivityState
    complex_directivity: R110ComplexDirectivityState
    wave_excitation: R110WaveExcitationState


class R110InterpolationAuthorityRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    method: InterpolationMethod
    implementation: str = Field(min_length=1)
    implementation_version: str = Field(min_length=1)
    provenance_source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class R110ApproximationMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: R110ApproximationKind
    detail: str = Field(min_length=1)


class R110NormalizationSemantics(BaseModel):
    """Keep directional normalization separate from wave-excitation normalization."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    directivity_normalization: DirectivityNormalization | None = None
    wave_excitation_normalization: Literal['UNKNOWN'] = 'UNKNOWN'
    electrical_to_acoustic_transfer_authority: str | None = Field(
        default=None,
        min_length=1,
    )
    wave_excitation_reason: str = Field(min_length=1)

    @model_validator(mode='after')
    def no_unproven_transfer(self) -> 'R110NormalizationSemantics':
        if self.electrical_to_acoustic_transfer_authority is not None:
            raise ValueError(
                'R110 source compiler v1 does not accept an electrical-to-acoustic '
                'transfer authority'
            )
        return self


class R110CompiledSourceModel(BaseModel):
    """Immutable solver-neutral source authority compiled from exact O100C bindings."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = R110_COMPILED_SOURCE_SCHEMA_VERSION
    compiler_version: Literal[
        'r110-source-model-compiler-1'
    ] = R110_SOURCE_COMPILER_VERSION

    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    system_variant_id: str = Field(min_length=1)
    system_variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_entity_id: str = Field(min_length=1)
    source_entity_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    equipment_definition_id: str = Field(min_length=1)
    equipment_definition_version: str = Field(min_length=1)
    equipment_definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    directivity_dataset_id: str | None = Field(default=None, min_length=1)
    directivity_dataset_version: str | None = Field(default=None, min_length=1)
    directivity_dataset_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    directivity_source_asset_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )

    source_entity_position: Position3
    source_entity_orientation: Quaternion4
    source_acoustic_reference_world_position: Position3
    source_acoustic_reference_authority: Literal[
        'equipment-acoustic-reference-world-transform-v1'
    ] = R110_ACOUSTIC_REFERENCE_AUTHORITY
    source_reference_axis_world: Direction3 | None = None
    source_reference_axis_authority: R110ReferenceAxisAuthority

    source_directivity_capability: DirectivityCapabilityTier
    directivity_dataset_kind: DirectivityDatasetKind | None = None
    valid_frequency_domain: FrequencyDomain | None = None
    frequency_domain_authority: R110FrequencyDomainAuthority
    coherent_phase_available: bool
    phase_reference: str | None = Field(default=None, min_length=1)
    electrical_sensitivity_reference: SensitivityReference | None = None
    normalization: R110NormalizationSemantics
    interpolation_authority: R110InterpolationAuthorityRef | None = None

    capabilities: tuple[R110CapabilityStatus, ...]
    use_case_states: R110CompilerUseCaseStates
    approximation_metadata: tuple[R110ApproximationMetadata, ...] = ()
    unsupported_reasons: tuple[str, ...] = ()

    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_compiled_model(self) -> 'R110CompiledSourceModel':
        dataset_identity = (
            self.directivity_dataset_id,
            self.directivity_dataset_version,
            self.directivity_dataset_sha256,
            self.directivity_source_asset_sha256,
            self.directivity_dataset_kind,
        )
        if any(value is not None for value in dataset_identity):
            if any(value is None for value in dataset_identity):
                raise ValueError(
                    'DirectivityDataset identity fields must be supplied together'
                )
        if self.coherent_phase_available != (self.phase_reference is not None):
            raise ValueError(
                'coherent phase availability must match explicit phase reference'
            )
        electrical_status = next(
            (
                item
                for item in self.capabilities
                if item.capability == 'electrical_sensitivity_reference'
            ),
            None,
        )
        if electrical_status is not None:
            expected_electrical = (
                'SUPPORTED'
                if self.electrical_sensitivity_reference is not None
                else 'UNSUPPORTED'
            )
            if electrical_status.decision != expected_electrical:
                raise ValueError(
                    'electrical sensitivity capability does not match compiled reference'
                )
        capability_names = [item.capability for item in self.capabilities]
        if len(capability_names) != len(set(capability_names)):
            raise ValueError('R110 capability status entries must be unique')
        expected = {
            'geometry_reference_point',
            'magnitude_directivity',
            'complex_directivity',
            'coherent_phase',
            'electrical_sensitivity_reference',
            'acoustic_wave_excitation_normalization',
        }
        if set(capability_names) != expected:
            raise ValueError('R110 capability status set is incomplete')
        if len(self.unsupported_reasons) != len(set(self.unsupported_reasons)):
            raise ValueError('R110 unsupported reasons must be unique')
        if self.semantic_sha256 != _digest(self.semantic_payload()):
            raise ValueError('R110CompiledSourceModel semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('semantic_sha256', None)
        return payload

    def capability(self, name: R110SourceCapabilityName) -> R110CapabilityStatus:
        for item in self.capabilities:
            if item.capability == name:
                return item
        raise KeyError(name)


def _equipment_reference_world(
    entity: SceneEntity,
    definition: EquipmentDefinition,
) -> Position3:
    matrix = quaternion_to_matrix3(entity.orientation)
    offset = definition.acoustic_reference_point_m
    local = (offset.x_m, offset.y_m, offset.z_m)
    rotated = tuple(
        sum(matrix[row][column] * local[column] for column in range(3))
        for row in range(3)
    )
    return Position3(
        x_m=entity.position.x_m + rotated[0],
        y_m=entity.position.y_m + rotated[1],
        z_m=entity.position.z_m + rotated[2],
    )


def _exact_equipment_binding(
    variant: SystemVariant,
    source_entity_id: str,
    definition: EquipmentDefinition,
) -> EquipmentBindingRef:
    matches = [
        item
        for item in variant.equipment_bindings
        if item.entity_id == source_entity_id
    ]
    if len(matches) != 1:
        raise ValueError(
            'exactly one SystemVariant EquipmentDefinition binding is required '
            'for the source entity'
        )
    binding = matches[0]
    if (
        binding.equipment_definition_id != definition.definition_id
        or binding.equipment_definition_version != definition.version
        or binding.equipment_definition_sha256 != definition.semantic_sha256
    ):
        raise ValueError('EquipmentDefinition does not match exact SystemVariant binding')
    return binding


def _interpolation_ref(
    dataset: DirectivityDataset | None,
) -> R110InterpolationAuthorityRef | None:
    if dataset is None:
        return None
    interpolation = dataset.interpolation
    return R110InterpolationAuthorityRef(
        method=interpolation.method,
        implementation=interpolation.implementation,
        implementation_version=interpolation.implementation_version,
        provenance_source_sha256=interpolation.provenance.source_sha256,
    )


def compile_r110_source_model(
    *,
    scene_revision: SceneRevision,
    system_variant: SystemVariant,
    source_entity_id: str,
    equipment_definition: EquipmentDefinition,
    directivity_dataset: DirectivityDataset | None = None,
) -> R110CompiledSourceModel:
    """Compile one exact variant source without selecting or adapting a wave solver."""

    if scene_revision.revision_id != system_variant.baseline_revision_id:
        raise ValueError('R110 SceneRevision/SystemVariant revision mismatch')
    if scene_revision.document_id != system_variant.document_id:
        raise ValueError('R110 SceneRevision/SystemVariant document mismatch')
    if scene_revision.content_hash != system_variant.baseline_content_hash:
        raise ValueError('R110 SceneRevision/SystemVariant content hash mismatch')

    derived_scene = materialize_system_variant(scene_revision, system_variant)
    try:
        source_entity = derived_scene.entity(source_entity_id)
    except KeyError as exc:
        raise ValueError('R110 source entity does not exist in exact SystemVariant') from exc
    if source_entity.kind != 'speaker':
        raise ValueError('R110 source entity must be a speaker')

    _exact_equipment_binding(
        system_variant,
        source_entity_id,
        equipment_definition,
    )

    if (
        source_entity.acoustic_reference_offset_m is not None
        and source_entity.acoustic_reference_offset_m
        != equipment_definition.acoustic_reference_point_m
    ):
        raise ValueError(
            'Scene source acoustic reference conflicts with EquipmentDefinition '
            'acoustic reference authority'
        )

    if directivity_dataset is not None:
        validate_directivity_dataset_binding(
            directivity_dataset,
            equipment_definition,
        )

    axis = source_entity.aim_xyz
    axis_authority: R110ReferenceAxisAuthority = (
        R110_AIM_AXIS_AUTHORITY if axis is not None else 'unavailable'
    )

    dataset_kind = (
        None if directivity_dataset is None else directivity_dataset.kind
    )
    numerical_magnitude = (
        directivity_dataset is not None
        and dataset_kind in {'magnitude_only', 'complex'}
    )
    complex_data = (
        directivity_dataset is not None
        and dataset_kind == 'complex'
        and equipment_definition.directivity.coherent_phase
        and directivity_dataset.phase_reference is not None
        and equipment_definition.directivity.phase_reference
        == directivity_dataset.phase_reference
    )
    magnitude_ready = numerical_magnitude and axis is not None
    complex_ready = complex_data and axis is not None

    capability = equipment_definition.directivity
    if directivity_dataset is not None:
        frequency_domain = directivity_dataset.valid_domain.frequency
        frequency_domain_authority: R110FrequencyDomainAuthority = (
            'directivity_dataset'
        )
    elif capability.valid_domain is not None:
        frequency_domain = capability.valid_domain.frequency
        frequency_domain_authority = 'equipment_definition'
    else:
        frequency_domain = None
        frequency_domain_authority = 'unavailable'

    approximations: list[R110ApproximationMetadata] = []
    reasons: list[str] = []

    if axis is None:
        detail = (
            'source entity has no explicit aim_xyz; acoustic/directivity axis is '
            'not inferred from cabinet orientation'
        )
        approximations.append(
            R110ApproximationMetadata(
                kind='source_reference_axis_unavailable',
                detail=detail,
            )
        )
        reasons.append(detail)

    if capability.tier == 'magnitude_only':
        detail = (
            'magnitude-only directivity is never promoted to coherent phase or '
            'phase-bearing source synthesis'
        )
        approximations.append(
            R110ApproximationMetadata(
                kind='magnitude_only_no_phase_synthesis',
                detail=detail,
            )
        )
        reasons.append(detail)
    elif capability.tier == 'polar_summary':
        detail = 'polar_summary is not expanded into a full angular field'
        approximations.append(
            R110ApproximationMetadata(
                kind='polar_summary_not_expanded',
                detail=detail,
            )
        )
        reasons.append(detail)
    elif capability.tier == 'analytic':
        detail = (
            'analytic directivity name has no R110 numerical evaluator authority'
        )
        approximations.append(
            R110ApproximationMetadata(
                kind='analytic_without_numeric_evaluator',
                detail=detail,
            )
        )
        reasons.append(detail)
    elif capability.tier in {'complex', 'magnitude_only'} and directivity_dataset is None:
        detail = (
            'declared directivity capability has no exact numerical '
            'DirectivityDataset binding for this compiled source'
        )
        approximations.append(
            R110ApproximationMetadata(
                kind='missing_numeric_directivity_dataset',
                detail=detail,
            )
        )
        reasons.append(detail)
    elif capability.tier == 'unknown':
        reasons.append('EquipmentDefinition directivity capability is unknown')

    if directivity_dataset is not None and not magnitude_ready:
        reasons.append(
            'numerical directivity cannot be used without an explicit source aim axis'
        )
    if directivity_dataset is not None and dataset_kind == 'magnitude_only':
        reasons.append('DirectivityDataset has no coherent phase authority')

    wave_reason = (
        'no exact electrical-input to acoustic volume-velocity/complex-source-strength '
        'transfer authority exists; sensitivity/reference data is not wave excitation '
        'normalization'
    )
    reasons.append(wave_reason)

    capability_statuses = (
        R110CapabilityStatus(
            capability='geometry_reference_point',
            decision='SUPPORTED',
            reason='exact source pose and EquipmentDefinition acoustic reference resolve deterministically',
        ),
        R110CapabilityStatus(
            capability='magnitude_directivity',
            decision='SUPPORTED' if magnitude_ready else 'UNSUPPORTED',
            reason=(
                'exact numerical DirectivityDataset and source aim axis are available'
                if magnitude_ready
                else 'exact numerical magnitude directivity plus source aim axis are required'
            ),
        ),
        R110CapabilityStatus(
            capability='complex_directivity',
            decision='SUPPORTED' if complex_ready else 'UNSUPPORTED',
            reason=(
                'complex dataset, exact phase reference, exact binding, and source aim axis are available'
                if complex_ready
                else 'complex dataset with exact coherent phase binding and source aim axis is required'
            ),
        ),
        R110CapabilityStatus(
            capability='coherent_phase',
            decision='SUPPORTED' if complex_data else 'UNSUPPORTED',
            reason=(
                'complex dataset phase reference exactly matches EquipmentDefinition coherent phase authority'
                if complex_data
                else 'coherent phase is not established by the exact bound authorities'
            ),
        ),
        R110CapabilityStatus(
            capability='electrical_sensitivity_reference',
            decision=(
                'SUPPORTED'
                if equipment_definition.sensitivity is not None
                else 'UNSUPPORTED'
            ),
            reason=(
                'EquipmentDefinition contains exact electrical sensitivity/reference semantics'
                if equipment_definition.sensitivity is not None
                else 'EquipmentDefinition has no electrical sensitivity/reference authority'
            ),
        ),
        R110CapabilityStatus(
            capability='acoustic_wave_excitation_normalization',
            decision='BLOCKED',
            reason=wave_reason,
        ),
    )

    directivity_normalization = (
        None
        if directivity_dataset is None
        else directivity_dataset.normalization
    )
    phase_reference = (
        directivity_dataset.phase_reference
        if complex_data and directivity_dataset is not None
        else None
    )

    use_cases = R110CompilerUseCaseStates(
        geometric_directivity=(
            'SUPPORTED_FOR_GEOMETRIC_DIRECTIVITY'
            if magnitude_ready
            else 'UNSUPPORTED'
        ),
        complex_directivity=(
            'SUPPORTED_FOR_COMPLEX_DIRECTIVITY'
            if complex_ready
            else 'UNSUPPORTED'
        ),
        wave_excitation='BLOCKED_FOR_WAVE_EXCITATION',
    )

    payload: dict[str, Any] = {
        'schema_version': R110_COMPILED_SOURCE_SCHEMA_VERSION,
        'compiler_version': R110_SOURCE_COMPILER_VERSION,
        'scene_revision_id': scene_revision.revision_id,
        'scene_content_hash': scene_revision.content_hash,
        'system_variant_id': system_variant.variant_id,
        'system_variant_sha256': system_variant.variant_sha256,
        'source_entity_id': source_entity.entity_id,
        'source_entity_sha256': _digest(source_entity.model_dump(mode='json')),
        'equipment_definition_id': equipment_definition.definition_id,
        'equipment_definition_version': equipment_definition.version,
        'equipment_definition_sha256': equipment_definition.semantic_sha256,
        'directivity_dataset_id': (
            None if directivity_dataset is None else directivity_dataset.dataset_id
        ),
        'directivity_dataset_version': (
            None if directivity_dataset is None else directivity_dataset.version
        ),
        'directivity_dataset_sha256': (
            None if directivity_dataset is None else directivity_dataset.semantic_sha256
        ),
        'directivity_source_asset_sha256': (
            None if directivity_dataset is None else directivity_dataset.source_asset_sha256
        ),
        'source_entity_position': source_entity.position.model_dump(mode='json'),
        'source_entity_orientation': source_entity.orientation.model_dump(mode='json'),
        'source_acoustic_reference_world_position': _equipment_reference_world(
            source_entity,
            equipment_definition,
        ).model_dump(mode='json'),
        'source_acoustic_reference_authority': R110_ACOUSTIC_REFERENCE_AUTHORITY,
        'source_reference_axis_world': (
            None if axis is None else axis.model_dump(mode='json')
        ),
        'source_reference_axis_authority': axis_authority,
        'source_directivity_capability': capability.tier,
        'directivity_dataset_kind': dataset_kind,
        'valid_frequency_domain': (
            None if frequency_domain is None else frequency_domain.model_dump(mode='json')
        ),
        'frequency_domain_authority': frequency_domain_authority,
        'coherent_phase_available': complex_data,
        'phase_reference': phase_reference,
        'electrical_sensitivity_reference': (
            None
            if equipment_definition.sensitivity is None
            else equipment_definition.sensitivity.model_dump(mode='json')
        ),
        'normalization': R110NormalizationSemantics(
            directivity_normalization=directivity_normalization,
            wave_excitation_reason=wave_reason,
        ).model_dump(mode='json'),
        'interpolation_authority': (
            None
            if directivity_dataset is None
            else _interpolation_ref(directivity_dataset).model_dump(mode='json')
        ),
        'capabilities': [
            item.model_dump(mode='json')
            for item in capability_statuses
        ],
        'use_case_states': use_cases.model_dump(mode='json'),
        'approximation_metadata': [
            item.model_dump(mode='json')
            for item in approximations
        ],
        'unsupported_reasons': list(dict.fromkeys(reasons)),
    }
    return R110CompiledSourceModel(
        **payload,
        semantic_sha256=_digest(payload),
    )


def require_r110_source_capability(
    model: R110CompiledSourceModel,
    capability: R110SourceCapabilityName,
) -> None:
    status = model.capability(capability)
    if status.decision != 'SUPPORTED':
        raise ValueError(
            f'R110 source capability {capability} is {status.decision}: '
            f'{status.reason}'
        )
