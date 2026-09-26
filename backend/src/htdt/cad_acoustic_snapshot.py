from __future__ import annotations

from hashlib import sha256
import json
from typing import TYPE_CHECKING, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_serializer,
    model_validator,
)

if TYPE_CHECKING:
    from .cad_listener_pose import ListenerPoseAuthority
    from .cad_room_operating_state import RoomOperatingState

from .cad_acoustic_treatment import semantic_surface_host_authority_sha256
from .cad_equipment import DirectivityCapabilityTier, FrequencyDomain
from .cad_listener_pose import resolve_listener_receiver
from .cad_prediction_models import canonical_prediction_json, prediction_input_hash
from .cad_r110_source import R110CompiledSourceModel
from .cad_repository import SceneRevision
from .cad_scene import (
    Direction3,
    Position3,
    Quaternion4,
    acoustic_reference_position,
)
from .cad_screen_transfer import AcousticScreenTransferAuthority
from .cad_system_variant import SystemVariant, materialize_system_variant
from .cad_wave_excitation import WaveSourceExcitationBinding
from .r120_geometry_compiler import (
    ExactExternalAuthorityRef,
    R120CompiledGeometry,
)
from .treatment_boundary_overlay import (
    TreatmentBoundaryCompilationResult,
    TreatmentBoundaryCompileStatus,
    TreatmentBoundaryOverlay,
    TreatmentBoundaryTarget,
    adapt_treatment_boundary_composition_to_r120,
)


ACOUSTIC_SCENE_SNAPSHOT_SCHEMA_VERSION = 3
ACOUSTIC_SCENE_SNAPSHOT_AUTHORITY_VERSION = '3'
ACOUSTIC_SCENE_SNAPSHOT_COMPILER_ID = 'htdt.acoustic_scene_snapshot'
ACOUSTIC_SCENE_SNAPSHOT_COMPILER_VERSION = '3'
ACOUSTIC_SCENE_SNAPSHOT_V1_SCHEMA_VERSION = 1
ACOUSTIC_SCENE_SNAPSHOT_V1_AUTHORITY_VERSION = '1'
ACOUSTIC_SCENE_SNAPSHOT_V1_COMPILER_VERSION = '1'
ACOUSTIC_SCENE_SNAPSHOT_V2_SCHEMA_VERSION = 2
ACOUSTIC_SCENE_SNAPSHOT_V2_AUTHORITY_VERSION = '2'
ACOUSTIC_SCENE_SNAPSHOT_V2_COMPILER_VERSION = '2'
ACOUSTIC_PREDICTION_REQUEST_SCHEMA_VERSION = 1

KnownObservable = Literal[
    'complex_pressure',
    'magnitude_response',
    'phase_response',
    'impulse_response',
    'spatial_pressure_field',
    'deterministic_paths',
]
ObservableState = Literal['READY', 'BLOCKED', 'UNSUPPORTED']
ReceiverReferenceSemantics = Literal[
    'scene_acoustic_reference_position',
    'explicit_measurement_authority',
    # #939: the receiver was resolved through the selected exact
    # ListenerPoseAuthority rather than the legacy seat offset.
    'selected_listener_pose',
]


def _canonical(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(payload: object) -> str:
    return sha256(_canonical(payload).encode('utf-8')).hexdigest()


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


class SnapshotEnvironmentAuthorityRef(BaseModel):
    """Exact external environment authority without synthesizing defaults.

    Air-state quantities beyond sound speed/temperature (density, pressure,
    relative humidity) bind to exact per-field source authorities inside the
    same environment authority: a consumer can never mix an unrelated
    authority's density with this environment's sound speed silently — each
    value+source pair must resolve inside ``authority``.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority: ExactExternalAuthorityRef
    sound_speed_m_s: float | None = Field(default=None, gt=0.0)
    sound_speed_source_authority: ExactExternalAuthorityRef | None = None
    temperature_c: float | None = None
    temperature_source_authority: ExactExternalAuthorityRef | None = None
    air_density_kg_m3: float | None = Field(default=None, gt=0.0)
    air_density_source_authority: ExactExternalAuthorityRef | None = None
    air_pressure_pa: float | None = Field(default=None, gt=0.0)
    air_pressure_source_authority: ExactExternalAuthorityRef | None = None
    relative_humidity_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    relative_humidity_source_authority: ExactExternalAuthorityRef | None = None

    @model_validator(mode='after')
    def exact_environment_sources(self) -> 'SnapshotEnvironmentAuthorityRef':
        for name, value, source in (
            (
                'sound speed',
                self.sound_speed_m_s,
                self.sound_speed_source_authority,
            ),
            (
                'temperature',
                self.temperature_c,
                self.temperature_source_authority,
            ),
            (
                'air density',
                self.air_density_kg_m3,
                self.air_density_source_authority,
            ),
            (
                'air pressure',
                self.air_pressure_pa,
                self.air_pressure_source_authority,
            ),
            (
                'relative humidity',
                self.relative_humidity_percent,
                self.relative_humidity_source_authority,
            ),
        ):
            if (value is None) != (source is None):
                raise ValueError(
                    f'{name} value and exact source authority must be '
                    'supplied together'
                )
        return self


class AcousticReceiverBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    receiver_id: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    world_position: Position3
    orientation: Quaternion4 | None = None
    acoustic_reference_semantics: ReceiverReferenceSemantics
    measurement_authority_ref: ExactExternalAuthorityRef | None = None
    # #939: exact selected listener pose the receiver was resolved through;
    # serialized only when set so pre-pose snapshots digest identically.
    listener_pose_ref: ExactExternalAuthorityRef | None = None
    requested_output_capabilities: tuple[str, ...]

    @model_serializer(mode='wrap')
    def _serialize(self, handler):
        data = handler(self)
        if self.listener_pose_ref is None:
            data.pop('listener_pose_ref', None)
        return data

    @model_validator(mode='after')
    def receiver_contract(self) -> 'AcousticReceiverBinding':
        if not self.requested_output_capabilities:
            raise ValueError('receiver must request at least one output capability')
        if len(self.requested_output_capabilities) != len(
            set(self.requested_output_capabilities)
        ):
            raise ValueError('receiver output capabilities must be unique')
        if (
            self.acoustic_reference_semantics == 'explicit_measurement_authority'
            and self.measurement_authority_ref is None
        ):
            raise ValueError(
                'explicit measurement receiver semantics require an exact authority ref'
            )
        if (self.listener_pose_ref is not None) != (
            self.acoustic_reference_semantics == 'selected_listener_pose'
        ):
            raise ValueError(
                'selected listener pose receivers require the exact pose '
                'authority ref and the matching reference semantics'
            )
        return self


class ReceiverMeasurementAuthority(BaseModel):
    """Canonical external measurement record bound to an exact receiver pose.

    Persisted/reopened snapshots resolve ``measurement_authority_ref`` to this
    record and require it to reproduce the exact receiver entity, world
    position and claimed orientation; an opaque ref never authorizes a
    receiver pose.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_ref: ExactExternalAuthorityRef
    entity_id: str = Field(min_length=1)
    world_position: Position3
    orientation: Quaternion4 | None = None


class AcousticSceneSourceBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

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

    r110_compiled_source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_reference_point: Position3
    source_reference_semantics: str = Field(min_length=1)
    source_axis: Direction3 | None = None
    source_axis_semantics: str = Field(min_length=1)
    directivity_capability: DirectivityCapabilityTier
    geometric_directivity_state: str = Field(min_length=1)
    complex_directivity_state: str = Field(min_length=1)
    wave_excitation_state: str = Field(min_length=1)
    valid_frequency_domain: FrequencyDomain | None = None

    @model_validator(mode='after')
    def exact_dataset_tuple(self) -> 'AcousticSceneSourceBinding':
        values = (
            self.directivity_dataset_id,
            self.directivity_dataset_version,
            self.directivity_dataset_sha256,
            self.directivity_source_asset_sha256,
        )
        if any(value is not None for value in values) and any(
            value is None for value in values
        ):
            raise ValueError(
                'DirectivityDataset exact identity fields must be supplied together'
            )
        return self


class SurfaceBoundaryConfiguration(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    source_surface_id: str = Field(pattern=r'^semantic-surface:[0-9a-f]{64}$')
    material_authority: ExactExternalAuthorityRef | None = None
    boundary_physics_authority: ExactExternalAuthorityRef | None = None


class TreatmentBoundaryOverlaySnapshotRef(BaseModel):
    """Exact attached-treatment lineage retained without flattening base construction."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    overlay_ref: ExactExternalAuthorityRef
    host_surface_authority_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    lifecycle: Literal['proposed', 'installed']
    treatment_definition_id: str = Field(min_length=1)
    treatment_definition_version: str = Field(min_length=1)
    treatment_definition_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    treatment_placement_instance_id: str = Field(min_length=1)
    treatment_placement_version: int = Field(ge=1)
    treatment_placement_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    surface_binding_evaluation_id: str = Field(
        pattern=r'^treatment-surface-binding:[0-9a-f]{64}$'
    )
    surface_binding_evaluation_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    wave_capability_state: Literal['AVAILABLE', 'UNKNOWN']
    geometric_capability_state: Literal['AVAILABLE', 'UNKNOWN']


class TreatmentBoundarySnapshotBinding(BaseModel):
    """Snapshot binding to composition authority; blocked results never masquerade as input."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    status: TreatmentBoundaryCompileStatus
    target_domain: TreatmentBoundaryTarget
    host_surface_id: str = Field(pattern=r'^semantic-surface:[0-9a-f]{64}$')
    composition_id: str | None = Field(
        default=None,
        pattern=r'^treatment-boundary-composition:[0-9a-f]{64}$',
    )
    composition_authority_version: str | None = Field(default=None, min_length=1)
    composition_hash_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    attached_treatment_overlays: tuple[TreatmentBoundaryOverlaySnapshotRef, ...] = ()
    selected_treatment_material_authorities: tuple[ExactExternalAuthorityRef, ...] = ()
    lifecycle: Literal['proposed', 'installed'] | None = None
    base_material_authority: ExactExternalAuthorityRef | None = None
    base_boundary_physics_authority: ExactExternalAuthorityRef | None = None
    reasons: tuple[str, ...] = ()

    @model_validator(mode='after')
    def exact_composition_contract(self) -> 'TreatmentBoundarySnapshotBinding':
        composition_fields = (
            self.composition_id,
            self.composition_authority_version,
            self.composition_hash_sha256,
        )
        available = self.status == 'AVAILABLE'
        if available and any(value is None for value in composition_fields):
            raise ValueError('AVAILABLE treatment binding requires exact composition identity')
        if not available and any(value is not None for value in composition_fields):
            raise ValueError('blocked treatment binding cannot expose composition identity')
        if available and not self.attached_treatment_overlays:
            raise ValueError('AVAILABLE treatment binding requires exact overlay lineage')
        if available and len(self.attached_treatment_overlays) != len(
            self.selected_treatment_material_authorities
        ):
            raise ValueError(
                'AVAILABLE treatment binding requires one selected material per overlay'
            )
        if not available and self.selected_treatment_material_authorities:
            raise ValueError('blocked treatment binding cannot select treatment materials')
        if available and self.lifecycle is None:
            raise ValueError('AVAILABLE treatment binding requires lifecycle')
        if self.lifecycle is not None and any(
            overlay.lifecycle != self.lifecycle
            for overlay in self.attached_treatment_overlays
        ):
            raise ValueError('treatment overlay lifecycle mismatch')
        overlay_ids = [
            item.overlay_ref.authority_id
            for item in self.attached_treatment_overlays
        ]
        if len(overlay_ids) != len(set(overlay_ids)):
            raise ValueError('treatment overlay refs must be unique')
        if len(self.reasons) != len(set(self.reasons)):
            raise ValueError('treatment binding reasons must be unique')
        if not available and not self.reasons:
            raise ValueError('blocked treatment binding requires a reason')
        return self


class ScreenTransferSnapshotBinding(BaseModel):
    """Exact screen-transfer authority bound into the snapshot (#940).

    No thin-interface solver model applies the transfer yet, so a screen's
    presence is a typed readiness gap rather than silent omission — the exact
    authority ref keeps the bound evidence inside the snapshot identity.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    screen_entity_id: str = Field(min_length=1)
    screen_transfer_ref: ExactExternalAuthorityRef


class ObservableReadiness(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    observable: str = Field(min_length=1)
    state: ObservableState
    reasons: tuple[str, ...]

    @model_validator(mode='after')
    def unique_reasons(self) -> 'ObservableReadiness':
        if len(self.reasons) != len(set(self.reasons)):
            raise ValueError('observable readiness reasons must be unique')
        if self.state == 'READY' and self.reasons:
            raise ValueError('READY observable must not carry blocking reasons')
        if self.state != 'READY' and not self.reasons:
            raise ValueError('blocked/unsupported observable requires a reason')
        return self


class AcousticSceneReadiness(BaseModel):
    """Purpose-specific snapshot readiness; intentionally no solver_ready flag."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    geometry_ready: bool
    geometric_directivity_ready: bool
    wave_source_ready: bool
    wave_boundary_ready: bool
    geometric_boundary_ready: bool | None = None
    environment_ready: bool
    receiver_ready: bool
    requested_observable_ready: bool
    observable_readiness: tuple[ObservableReadiness, ...]


class AcousticSceneSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1, 2, 3] = ACOUSTIC_SCENE_SNAPSHOT_SCHEMA_VERSION
    authority_version: Literal['1', '2', '3'] = ACOUSTIC_SCENE_SNAPSHOT_AUTHORITY_VERSION
    compiler_id: Literal[
        'htdt.acoustic_scene_snapshot'
    ] = ACOUSTIC_SCENE_SNAPSHOT_COMPILER_ID
    compiler_version: Literal['1', '2', '3'] = ACOUSTIC_SCENE_SNAPSHOT_COMPILER_VERSION

    snapshot_id: str = Field(pattern=r'^acoustic-scene-snapshot:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    system_variant_id: str | None = Field(default=None, min_length=1)
    system_variant_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )

    semantic_geometry_id: str = Field(
        pattern=r'^semantic-acoustic-geometry:[0-9a-f]{64}$'
    )
    semantic_geometry_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    r120_compiled_geometry_id: str = Field(
        pattern=r'^r120-compiled-geometry:[0-9a-f]{64}$'
    )
    r120_compiled_geometry_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    compiled_topology_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    geometric_tolerance_m: float = Field(gt=0.0)
    approximation_error_bound_m: float | None = Field(default=None, ge=0.0)
    approximation_error_status: str = Field(min_length=1)
    maximum_dropped_feature_extent_m: float = Field(ge=0.0)

    acoustic_region_authority_ref: ExactExternalAuthorityRef | None = None
    portal_authority_ref: ExactExternalAuthorityRef | None = None
    boundary_termination_authority_ref: ExactExternalAuthorityRef | None = None
    geometric_acoustics_topology_preflight_ref: ExactExternalAuthorityRef | None = None

    surface_boundary_configuration: tuple[SurfaceBoundaryConfiguration, ...]
    material_boundary_configuration_sha256: str = Field(
        pattern=r'^[0-9a-f]{64}$'
    )
    treatment_boundary_bindings: tuple[TreatmentBoundarySnapshotBinding, ...] = ()
    wave_source_excitation_bindings: tuple[WaveSourceExcitationBinding, ...] = ()
    # #940: exact screen-transfer authorities bound into the snapshot. No
    # thin-interface solver model consumes them yet; the bindings pin which
    # authority was consulted while the screen effect stays unmodelled.
    screen_transfer_bindings: tuple[ScreenTransferSnapshotBinding, ...] = ()

    sources: tuple[AcousticSceneSourceBinding, ...]
    receivers: tuple[AcousticReceiverBinding, ...]
    environment: SnapshotEnvironmentAuthorityRef | None = None
    # #941: exact RoomOperatingState bound at request time; digested only
    # when set so pre-state snapshots keep their canonical identity.
    operating_state_ref: ExactExternalAuthorityRef | None = None

    valid_frequency_domain: FrequencyDomain | None = None
    valid_frequency_domain_authority_ref: ExactExternalAuthorityRef | None = None
    requested_frequency_domain: FrequencyDomain
    requested_observables: tuple[str, ...]

    readiness: AcousticSceneReadiness
    unresolved_conditions: tuple[str, ...]

    @model_validator(mode='after')
    def exact_snapshot_identity(self) -> 'AcousticSceneSnapshot':
        if self.schema_version == 1:
            if (
                self.authority_version != ACOUSTIC_SCENE_SNAPSHOT_V1_AUTHORITY_VERSION
                or self.compiler_version != ACOUSTIC_SCENE_SNAPSHOT_V1_COMPILER_VERSION
            ):
                raise ValueError('AcousticSceneSnapshot v1 version tuple mismatch')
            if self.treatment_boundary_bindings:
                raise ValueError('AcousticSceneSnapshot v1 cannot contain treatment bindings')
            if self.wave_source_excitation_bindings:
                raise ValueError(
                    'AcousticSceneSnapshot v1 cannot contain wave excitation bindings'
                )
            if self.readiness.geometric_boundary_ready is not None:
                raise ValueError(
                    'AcousticSceneSnapshot v1 cannot carry v2 geometric boundary readiness'
                )
            if self.geometric_acoustics_topology_preflight_ref is not None:
                raise ValueError(
                    'AcousticSceneSnapshot v1 cannot carry geometric topology preflight authority'
                )
        elif self.schema_version == 2:
            if (
                self.authority_version != ACOUSTIC_SCENE_SNAPSHOT_V2_AUTHORITY_VERSION
                or self.compiler_version != ACOUSTIC_SCENE_SNAPSHOT_V2_COMPILER_VERSION
            ):
                raise ValueError('AcousticSceneSnapshot v2 version tuple mismatch')
            if self.wave_source_excitation_bindings:
                raise ValueError(
                    'AcousticSceneSnapshot v2 cannot contain wave excitation bindings'
                )
            if self.readiness.geometric_boundary_ready is None:
                raise ValueError(
                    'AcousticSceneSnapshot v2 requires geometric boundary readiness'
                )
        else:
            if (
                self.authority_version != ACOUSTIC_SCENE_SNAPSHOT_AUTHORITY_VERSION
                or self.compiler_version != ACOUSTIC_SCENE_SNAPSHOT_COMPILER_VERSION
            ):
                raise ValueError('AcousticSceneSnapshot v3 version tuple mismatch')
            if self.readiness.geometric_boundary_ready is None:
                raise ValueError(
                    'AcousticSceneSnapshot v3 requires geometric boundary readiness'
                )
        wave_binding_sources = [
            item.source_entity_id
            for item in self.wave_source_excitation_bindings
        ]
        if len(wave_binding_sources) != len(set(wave_binding_sources)):
            raise ValueError(
                'wave source excitation bindings must be unique per source entity'
            )
        source_hashes = {
            item.r110_compiled_source_sha256
            for item in self.sources
        }
        if any(
            item.r110_compiled_source_sha256 not in source_hashes
            for item in self.wave_source_excitation_bindings
        ):
            raise ValueError(
                'wave source excitation binding references unknown R110 source'
            )

        treatment_keys = [
            (item.host_surface_id, item.target_domain)
            for item in self.treatment_boundary_bindings
        ]
        if len(treatment_keys) != len(set(treatment_keys)):
            raise ValueError(
                'treatment boundary bindings must be unique per surface/domain'
            )
        if (self.system_variant_id is None) != (
            self.system_variant_sha256 is None
        ):
            raise ValueError('SystemVariant id/hash must be supplied together')
        if (self.valid_frequency_domain is None) != (
            self.valid_frequency_domain_authority_ref is None
        ):
            raise ValueError(
                'valid frequency domain and exact authority ref must be supplied together'
            )
        source_ids = [item.source_entity_id for item in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError('snapshot source entity ids must be unique')
        receiver_ids = [item.receiver_id for item in self.receivers]
        if len(receiver_ids) != len(set(receiver_ids)):
            raise ValueError('snapshot receiver ids must be unique')
        receiver_entities = [item.entity_id for item in self.receivers]
        if len(receiver_entities) != len(set(receiver_entities)):
            raise ValueError('snapshot receiver entity ids must be unique')
        if len(self.requested_observables) != len(set(self.requested_observables)):
            raise ValueError('requested observables must be unique')
        if len(self.unresolved_conditions) != len(set(self.unresolved_conditions)):
            raise ValueError('snapshot unresolved conditions must be unique')
        screen_binding_entities = [
            item.screen_entity_id for item in self.screen_transfer_bindings
        ]
        if len(screen_binding_entities) != len(set(screen_binding_entities)):
            raise ValueError(
                'screen transfer bindings must be unique per screen entity'
            )
        expected_boundary_hash = _digest(
            [
                item.model_dump(mode='json')
                for item in self.surface_boundary_configuration
            ]
        )
        if self.material_boundary_configuration_sha256 != expected_boundary_hash:
            raise ValueError('material/boundary configuration hash mismatch')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('AcousticSceneSnapshot semantic hash mismatch')
        if self.snapshot_id != f'acoustic-scene-snapshot:{expected}':
            raise ValueError('AcousticSceneSnapshot id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            mode='json',
            exclude={'snapshot_id', 'semantic_sha256'},
        )
        if self.schema_version < 3:
            payload.pop('wave_source_excitation_bindings', None)
        if self.geometric_acoustics_topology_preflight_ref is None:
            payload.pop('geometric_acoustics_topology_preflight_ref', None)
        if self.operating_state_ref is None:
            payload.pop('operating_state_ref', None)
        if not self.screen_transfer_bindings:
            payload.pop('screen_transfer_bindings', None)
        if self.schema_version == 1:
            payload.pop('treatment_boundary_bindings', None)
            readiness = payload.get('readiness')
            if isinstance(readiness, dict):
                readiness.pop('geometric_boundary_ready', None)
        return payload


class AcousticPredictionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = ACOUSTIC_PREDICTION_REQUEST_SCHEMA_VERSION
    request_id: str = Field(pattern=r'^acoustic-prediction-request:[0-9a-f]{64}$')
    request_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    acoustic_scene_snapshot_id: str = Field(
        pattern=r'^acoustic-scene-snapshot:[0-9a-f]{64}$'
    )
    acoustic_scene_snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    model_solver_role_id: str = Field(min_length=1)
    requested_frequency_domain: FrequencyDomain
    requested_observables: tuple[str, ...]
    numerical_fidelity_policy_ref: ExactExternalAuthorityRef
    deterministic_input_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_request_identity(self) -> 'AcousticPredictionRequest':
        if len(self.requested_observables) != len(set(self.requested_observables)):
            raise ValueError('prediction requested observables must be unique')
        input_payload = self.input_payload()
        expected_input = prediction_input_hash(
            canonical_prediction_json(input_payload)
        )
        if self.deterministic_input_hash != expected_input:
            raise ValueError('prediction deterministic input hash mismatch')
        expected_semantic = _digest(input_payload)
        if self.request_semantic_sha256 != expected_semantic:
            raise ValueError('AcousticPredictionRequest semantic hash mismatch')
        if self.request_id != f'acoustic-prediction-request:{expected_semantic}':
            raise ValueError('AcousticPredictionRequest id mismatch')
        return self

    def input_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'acoustic_scene_snapshot_id': self.acoustic_scene_snapshot_id,
            'acoustic_scene_snapshot_sha256': self.acoustic_scene_snapshot_sha256,
            'model_solver_role_id': self.model_solver_role_id,
            'requested_frequency_domain': self.requested_frequency_domain.model_dump(
                mode='json'
            ),
            'requested_observables': list(self.requested_observables),
            'numerical_fidelity_policy_ref': (
                self.numerical_fidelity_policy_ref.model_dump(mode='json')
            ),
        }


def source_binding_from_r110(
    source: R110CompiledSourceModel,
) -> AcousticSceneSourceBinding:
    source = R110CompiledSourceModel.model_validate(
        source.model_dump(mode='python')
    )
    return AcousticSceneSourceBinding(
        source_entity_id=source.source_entity_id,
        source_entity_sha256=source.source_entity_sha256,
        equipment_definition_id=source.equipment_definition_id,
        equipment_definition_version=source.equipment_definition_version,
        equipment_definition_sha256=source.equipment_definition_sha256,
        directivity_dataset_id=source.directivity_dataset_id,
        directivity_dataset_version=source.directivity_dataset_version,
        directivity_dataset_sha256=source.directivity_dataset_sha256,
        directivity_source_asset_sha256=source.directivity_source_asset_sha256,
        r110_compiled_source_sha256=source.semantic_sha256,
        source_reference_point=source.source_acoustic_reference_world_position,
        source_reference_semantics=source.source_acoustic_reference_authority,
        source_axis=source.source_reference_axis_world,
        source_axis_semantics=source.source_reference_axis_authority,
        directivity_capability=source.source_directivity_capability,
        geometric_directivity_state=source.use_case_states.geometric_directivity,
        complex_directivity_state=source.use_case_states.complex_directivity,
        wave_excitation_state=source.use_case_states.wave_excitation,
        valid_frequency_domain=source.valid_frequency_domain,
    )


def receiver_binding_from_scene(
    *,
    scene_revision: SceneRevision,
    entity_id: str,
    requested_output_capabilities: tuple[str, ...],
    receiver_id: str | None = None,
    system_variant: SystemVariant | None = None,
    orientation_authoritative: bool = False,
    measurement_authority_ref: ExactExternalAuthorityRef | None = None,
    listener_pose: 'ListenerPoseAuthority | None' = None,
) -> AcousticReceiverBinding:
    if system_variant is not None:
        if (
            system_variant.document_id != scene_revision.document_id
            or system_variant.baseline_revision_id != scene_revision.revision_id
            or system_variant.baseline_content_hash != scene_revision.content_hash
        ):
            raise ValueError(
                'receiver SystemVariant does not match exact SceneRevision'
            )
        scene = materialize_system_variant(scene_revision, system_variant)
    else:
        scene = scene_revision.document
    try:
        entity = scene.entity(entity_id)
    except KeyError as exc:
        raise ValueError('receiver entity does not exist in exact scene') from exc
    if listener_pose is not None:
        if listener_pose.document_id not in (None, scene.document_id):
            raise ValueError(
                'listener pose is scoped to a different document'
            )
        resolved = resolve_listener_receiver(entity, listener_pose)
        assert resolved is not None
        position = resolved.position
    else:
        position = acoustic_reference_position(entity)
    if position is None:
        raise ValueError('receiver entity has no acoustic reference position')
    semantics: ReceiverReferenceSemantics = (
        'selected_listener_pose'
        if listener_pose is not None
        else 'explicit_measurement_authority'
        if measurement_authority_ref is not None
        else 'scene_acoustic_reference_position'
    )
    return AcousticReceiverBinding(
        receiver_id=entity_id if receiver_id is None else receiver_id,
        entity_id=entity_id,
        world_position=position,
        orientation=entity.orientation if orientation_authoritative else None,
        acoustic_reference_semantics=semantics,
        measurement_authority_ref=measurement_authority_ref,
        listener_pose_ref=(
            None if listener_pose is None else listener_pose.authority_ref()
        ),
        requested_output_capabilities=_unique(requested_output_capabilities),
    )


def _surface_configuration(
    compiled: R120CompiledGeometry,
) -> tuple[SurfaceBoundaryConfiguration, ...]:
    return tuple(
        SurfaceBoundaryConfiguration(
            source_surface_id=item.source_surface_id,
            material_authority=item.material_authority,
            boundary_physics_authority=item.boundary_physics_authority,
        )
        for item in sorted(
            compiled.surface_mapping,
            key=lambda item: item.source_surface_id,
        )
    )


_FREQUENCY_DOMAIN_REASONS = {
    'requested_frequency_outside_snapshot_valid_domain',
    'source_directivity_domain_incomplete',
    'wave_excitation_domain_incomplete',
}


def _covers_requested(
    domain: FrequencyDomain | None,
    requested: FrequencyDomain,
) -> bool:
    """Requested band containment; an undeclared domain is not a known
    contradiction (it is flagged via ``valid_frequency_domain_unknown``
    unresolved conditions instead)."""
    return domain is None or (
        domain.contains(requested.minimum_hz)
        and domain.contains(requested.maximum_hz)
    )


def _observable_readiness(
    observable: str,
    *,
    geometry_ready: bool,
    geometric_geometry_ready: bool,
    geometric_directivity_ready: bool,
    wave_source_ready: bool,
    wave_boundary_ready: bool,
    geometric_boundary_ready: bool,
    environment_ready: bool,
    receiver_ready: bool,
    snapshot_frequency_domain_ready: bool = True,
    source_directivity_domain_ready: bool = True,
    wave_excitation_domain_ready: bool = True,
    screen_transfer_ready: bool = True,
) -> ObservableReadiness:
    wave_reasons: list[str] = []
    if not geometry_ready:
        wave_reasons.append('geometry_not_ready')
    if not wave_source_ready:
        wave_reasons.append('wave_source_not_ready')
    if not wave_excitation_domain_ready:
        wave_reasons.append('wave_excitation_domain_incomplete')
    if not wave_boundary_ready:
        wave_reasons.append('wave_boundary_not_ready')
    if not environment_ready:
        wave_reasons.append('environment_not_ready')
    if not receiver_ready:
        wave_reasons.append('receiver_not_ready')
    if not snapshot_frequency_domain_ready:
        wave_reasons.append(
            'requested_frequency_outside_snapshot_valid_domain'
        )
    if not screen_transfer_ready:
        wave_reasons.append('screen_transfer_not_integrated')

    ga_reasons: list[str] = []
    if not geometry_ready:
        ga_reasons.append('geometry_not_ready')
    if not geometric_geometry_ready:
        ga_reasons.append('geometric_acoustics_geometry_not_ready')
    if not geometric_directivity_ready:
        ga_reasons.append('geometric_directivity_not_ready')
    if not source_directivity_domain_ready:
        ga_reasons.append('source_directivity_domain_incomplete')
    if not geometric_boundary_ready:
        ga_reasons.append('geometric_boundary_not_ready')
    if not environment_ready:
        ga_reasons.append('environment_not_ready')
    if not receiver_ready:
        ga_reasons.append('receiver_not_ready')
    if not snapshot_frequency_domain_ready:
        ga_reasons.append(
            'requested_frequency_outside_snapshot_valid_domain'
        )
    if not screen_transfer_ready:
        ga_reasons.append('screen_transfer_not_integrated')

    if observable == 'deterministic_paths':
        path_reasons = [
            reason
            for reason in ga_reasons
            if reason != 'environment_not_ready'
            and reason != 'screen_transfer_not_integrated'
            and reason not in _FREQUENCY_DOMAIN_REASONS
        ]
        return ObservableReadiness(
            observable=observable,
            state='READY' if not path_reasons else 'BLOCKED',
            reasons=tuple(path_reasons),
        )

    if observable == 'magnitude_response':
        if not wave_reasons or not ga_reasons:
            return ObservableReadiness(
                observable=observable,
                state='READY',
                reasons=(),
            )
        return ObservableReadiness(
            observable=observable,
            state='BLOCKED',
            reasons=tuple(
                _unique(
                    tuple(
                        f'wave:{reason}' for reason in wave_reasons
                    )
                    + tuple(
                        f'geometric:{reason}' for reason in ga_reasons
                    )
                )
            ),
        )

    if observable in {
        'complex_pressure',
        'phase_response',
        'impulse_response',
        'spatial_pressure_field',
    }:
        return ObservableReadiness(
            observable=observable,
            state='READY' if not wave_reasons else 'BLOCKED',
            reasons=tuple(wave_reasons),
        )

    return ObservableReadiness(
        observable=observable,
        state='UNSUPPORTED',
        reasons=('observable_not_defined_by_snapshot_contract_v1',),
    )


def _snapshot_schema_version(
    *,
    compiled: R120CompiledGeometry,
    treatment_bindings: tuple[TreatmentBoundarySnapshotBinding, ...],
    wave_excitation_bindings: tuple[WaveSourceExcitationBinding, ...],
    geometric_acoustics_topology_preflight_ref: ExactExternalAuthorityRef | None,
) -> int:
    if wave_excitation_bindings:
        return ACOUSTIC_SCENE_SNAPSHOT_SCHEMA_VERSION
    if (
        treatment_bindings
        or geometric_acoustics_topology_preflight_ref is not None
        or compiled.readiness.geometric_acoustics_geometry_ready
        != compiled.readiness.wave_geometry_ready
    ):
        # V2 is the first snapshot schema that carries geometric boundary
        # readiness independently from wave boundary readiness.
        return ACOUSTIC_SCENE_SNAPSHOT_V2_SCHEMA_VERSION
    return ACOUSTIC_SCENE_SNAPSHOT_V1_SCHEMA_VERSION


def _derive_readiness(
    *,
    compiled: R120CompiledGeometry,
    sources: tuple[AcousticSceneSourceBinding, ...],
    receivers: tuple[AcousticReceiverBinding, ...],
    environment: SnapshotEnvironmentAuthorityRef | None,
    requested_observables: tuple[str, ...],
    treatment_bindings: tuple[TreatmentBoundarySnapshotBinding, ...],
    wave_excitation_bindings: tuple[WaveSourceExcitationBinding, ...],
    requested_frequency_domain: FrequencyDomain,
    valid_frequency_domain: FrequencyDomain | None,
    schema_version: int,
    geometric_acoustics_topology_preflight_ref: ExactExternalAuthorityRef | None = None,
    screen_transfer_ready: bool = True,
) -> AcousticSceneReadiness:
    geometry_ready = compiled.readiness.geometry_compiled
    geometric_geometry_ready = (
        compiled.readiness.geometric_acoustics_geometry_ready
        or geometric_acoustics_topology_preflight_ref is not None
    )
    # #977: requested-vs-valid frequency coverage is part of canonical
    # readiness, not only adapter-dispatch policy.
    snapshot_frequency_domain_ready = _covers_requested(
        valid_frequency_domain, requested_frequency_domain
    )
    source_directivity_domain_ready = all(
        _covers_requested(
            item.valid_frequency_domain, requested_frequency_domain
        )
        for item in sources
    )
    geometric_directivity_ready = bool(sources) and all(
        item.geometric_directivity_state
        == 'SUPPORTED_FOR_GEOMETRIC_DIRECTIVITY'
        and _covers_requested(
            item.valid_frequency_domain, requested_frequency_domain
        )
        for item in sources
    )
    excitation_by_source_hash = {
        item.r110_compiled_source_sha256: item
        for item in wave_excitation_bindings
    }
    wave_excitation_domain_ready = all(
        _covers_requested(
            item.valid_frequency_domain, requested_frequency_domain
        )
        and (
            item.r110_compiled_source_sha256 not in excitation_by_source_hash
            or _covers_requested(
                excitation_by_source_hash[
                    item.r110_compiled_source_sha256
                ].valid_frequency_domain,
                requested_frequency_domain,
            )
        )
        for item in sources
    )
    wave_source_ready = bool(sources) and all(
        _covers_requested(
            item.valid_frequency_domain, requested_frequency_domain
        )
        and (
            item.wave_excitation_state not in {
                'BLOCKED_FOR_WAVE_EXCITATION',
                'UNSUPPORTED',
            }
            or (
                item.r110_compiled_source_sha256 in excitation_by_source_hash
                and excitation_by_source_hash[
                    item.r110_compiled_source_sha256
                ].valid_frequency_domain.contains(
                    requested_frequency_domain.minimum_hz
                )
                and excitation_by_source_hash[
                    item.r110_compiled_source_sha256
                ].valid_frequency_domain.contains(
                    requested_frequency_domain.maximum_hz
                )
            )
        )
        for item in sources
    )
    treated_surfaces = {
        binding.host_surface_id
        for binding in treatment_bindings
    }
    wave_composed_surfaces = {
        binding.host_surface_id
        for binding in treatment_bindings
        if binding.target_domain == 'wave' and binding.status == 'AVAILABLE'
    }
    geometric_composed_surfaces = {
        binding.host_surface_id
        for binding in treatment_bindings
        if binding.target_domain == 'geometric' and binding.status == 'AVAILABLE'
    }
    treatment_wave_ready = (
        not treated_surfaces
        or treated_surfaces.issubset(wave_composed_surfaces)
    )
    treatment_geometric_ready = (
        not treated_surfaces
        or treated_surfaces.issubset(geometric_composed_surfaces)
    )
    structural_blocks = {
        'BLOCKED_NO_ACOUSTIC_MODEL',
        'BLOCKED_PARTIAL_COVERAGE',
        'BLOCKED_COVERAGE_MISMATCH',
        'BLOCKED_OVERLAP',
    }
    if any(
        binding.status in structural_blocks
        for binding in treatment_bindings
    ):
        treatment_wave_ready = False
        treatment_geometric_ready = False

    wave_boundary_ready = (
        compiled.readiness.wave_geometry_ready and treatment_wave_ready
    )
    geometric_boundary_ready = (
        geometric_geometry_ready
        and treatment_geometric_ready
    )
    environment_ready = (
        environment is not None
        and environment.sound_speed_m_s is not None
        and environment.sound_speed_source_authority is not None
    )
    receiver_ready = bool(receivers)

    statuses = tuple(
        _observable_readiness(
            observable,
            geometry_ready=geometry_ready,
            geometric_geometry_ready=geometric_geometry_ready,
            geometric_directivity_ready=geometric_directivity_ready,
            wave_source_ready=wave_source_ready,
            wave_boundary_ready=wave_boundary_ready,
            geometric_boundary_ready=(
                geometric_boundary_ready if schema_version >= 2 else True
            ),
            environment_ready=environment_ready,
            receiver_ready=receiver_ready,
            snapshot_frequency_domain_ready=(
                snapshot_frequency_domain_ready
            ),
            source_directivity_domain_ready=(
                source_directivity_domain_ready
            ),
            wave_excitation_domain_ready=wave_excitation_domain_ready,
            screen_transfer_ready=screen_transfer_ready,
        )
        for observable in requested_observables
    )
    return AcousticSceneReadiness(
        geometry_ready=geometry_ready,
        geometric_directivity_ready=geometric_directivity_ready,
        wave_source_ready=wave_source_ready,
        wave_boundary_ready=wave_boundary_ready,
        geometric_boundary_ready=(
            geometric_boundary_ready if schema_version >= 2 else None
        ),
        environment_ready=environment_ready,
        receiver_ready=receiver_ready,
        requested_observable_ready=bool(statuses)
        and all(item.state == 'READY' for item in statuses),
        observable_readiness=statuses,
    )


def _derive_unresolved_conditions(
    *,
    compiled: R120CompiledGeometry,
    sources: tuple[AcousticSceneSourceBinding, ...],
    receivers: tuple[AcousticReceiverBinding, ...],
    environment: SnapshotEnvironmentAuthorityRef | None,
    readiness: AcousticSceneReadiness,
    valid_frequency_domain: FrequencyDomain | None,
    requested_frequency_domain: FrequencyDomain,
    treatment_bindings: tuple[TreatmentBoundarySnapshotBinding, ...],
    wave_excitation_bindings: tuple[WaveSourceExcitationBinding, ...],
    screen_entity_ids: tuple[str, ...] = (),
    screen_transfer_bindings: tuple[ScreenTransferSnapshotBinding, ...] = (),
) -> tuple[str, ...]:
    unresolved = list(compiled.unresolved_conditions)
    bound_screens = {
        item.screen_entity_id for item in screen_transfer_bindings
    }
    for entity_id in screen_entity_ids:
        if entity_id in bound_screens:
            unresolved.append(f'screen_transfer_not_integrated:{entity_id}')
        else:
            unresolved.append(f'screen_transfer_unbound:{entity_id}')
    if not sources:
        unresolved.append('source_authority_missing')
    externally_resolved_wave_sources = {
        item.r110_compiled_source_sha256
        for item in wave_excitation_bindings
    }
    if any(
        item.wave_excitation_state == 'BLOCKED_FOR_WAVE_EXCITATION'
        and item.r110_compiled_source_sha256
        not in externally_resolved_wave_sources
        for item in sources
    ):
        unresolved.append('wave_source_excitation_blocked')
    if any(
        (
            not binding.valid_frequency_domain.contains(
                requested_frequency_domain.minimum_hz
            )
            or not binding.valid_frequency_domain.contains(
                requested_frequency_domain.maximum_hz
            )
        )
        for binding in wave_excitation_bindings
    ):
        unresolved.append(
            'wave_source_excitation_frequency_domain_unsupported'
        )
    if not receivers:
        unresolved.append('receiver_set_missing')
    if environment is None:
        unresolved.append('environment_unknown')
    elif not readiness.environment_ready:
        unresolved.append('environment_sound_speed_unknown')
    if valid_frequency_domain is None:
        unresolved.append('valid_frequency_domain_unknown')
    elif not _covers_requested(
        valid_frequency_domain, requested_frequency_domain
    ):
        unresolved.append(
            'requested_frequency_outside_snapshot_valid_domain'
        )
    if any(
        item.valid_frequency_domain is not None
        and not _covers_requested(
            item.valid_frequency_domain, requested_frequency_domain
        )
        for item in sources
    ):
        unresolved.append('source_directivity_domain_incomplete')
    if compiled.region_authority_ref is None:
        unresolved.append('acoustic_region_authority_missing')
    if compiled.portal_authority_ref is None:
        unresolved.append('portal_authority_missing')
    if compiled.boundary_termination_authority_ref is None:
        unresolved.append('boundary_termination_authority_missing')
    for binding in treatment_bindings:
        if binding.status != 'AVAILABLE':
            unresolved.append(
                f'treatment_{binding.target_domain}_boundary_'
                f'{binding.status.lower()}'
            )
        if any(
            item.wave_capability_state != 'AVAILABLE'
            for item in binding.attached_treatment_overlays
        ):
            unresolved.append('treatment_wave_boundary_capability_unknown')
        if any(
            item.geometric_capability_state != 'AVAILABLE'
            for item in binding.attached_treatment_overlays
        ):
            unresolved.append('treatment_geometric_boundary_capability_unknown')
    return tuple(dict.fromkeys(unresolved))


def _require_geometric_topology_preflight_authority(
    *,
    preflight_ref: ExactExternalAuthorityRef,
    compiled: R120CompiledGeometry,
    requested_observables: tuple[str, ...],
) -> None:
    """Canonical R150 topology preflight contract shared by build and replay."""
    if (
        preflight_ref.authority_version != '1'
        or preflight_ref.authority_id
        != f'r150-portal-graph:{preflight_ref.semantic_hash_sha256}'
    ):
        raise ValueError(
            'geometric acoustic topology preflight must be an exact R150 Portal graph authority'
        )
    if set(requested_observables) != {'deterministic_paths'}:
        raise ValueError(
            'R150 Portal graph topology preflight is bounded to deterministic_paths'
        )
    if (
        compiled.approximation_operations
        or compiled.dropped_features
        or compiled.approximation_error_status != 'exact_preservation'
    ):
        raise ValueError(
            'R150 Portal graph topology preflight requires exact preserved R120 geometry'
        )
    allowed_preflight_unresolved = {
        'compiled_non_manifold_edges',
        'input_semantic_geometry_not_compiler_contract_ready',
    }
    unsupported_preflight = (
        set(compiled.readiness.unresolved_conditions)
        - allowed_preflight_unresolved
    )
    if unsupported_preflight:
        raise ValueError(
            'R150 Portal graph topology preflight cannot override unrelated '
            f'R120 unresolved conditions: {sorted(unsupported_preflight)}'
        )
    if (
        compiled.region_authority_ref is None
        or compiled.portal_authority_ref is None
        or compiled.boundary_termination_authority_ref is None
    ):
        raise ValueError(
            'R150 Portal graph topology preflight requires exact R120 geometry authorities'
        )


def _treatment_overlay_snapshot_ref(
    overlay: TreatmentBoundaryOverlay,
) -> TreatmentBoundaryOverlaySnapshotRef:
    return TreatmentBoundaryOverlaySnapshotRef(
        overlay_ref=overlay.as_external_authority_ref(),
        host_surface_authority_sha256=overlay.host_surface_authority_sha256,
        lifecycle=overlay.lifecycle,
        treatment_definition_id=overlay.treatment_definition_id,
        treatment_definition_version=overlay.treatment_definition_version,
        treatment_definition_hash_sha256=overlay.treatment_definition_hash_sha256,
        treatment_placement_instance_id=overlay.treatment_placement_instance_id,
        treatment_placement_version=overlay.treatment_placement_version,
        treatment_placement_hash_sha256=overlay.treatment_placement_hash_sha256,
        surface_binding_evaluation_id=overlay.surface_binding_evaluation_id,
        surface_binding_evaluation_hash_sha256=(
            overlay.surface_binding_evaluation_hash_sha256
        ),
        wave_capability_state=overlay.wave_capability_state,
        geometric_capability_state=overlay.geometric_capability_state,
    )


def _treatment_binding_from_result(
    result: TreatmentBoundaryCompilationResult,
    *,
    scene_revision: SceneRevision,
    compiled: R120CompiledGeometry,
) -> TreatmentBoundarySnapshotBinding:
    result = TreatmentBoundaryCompilationResult.model_validate(
        result.model_dump(mode='python')
    )
    if result.status in {
        'BLOCKED_STALE_SURFACE',
        'BLOCKED_STALE_R120_COMPILED_GEOMETRY',
    }:
        raise ValueError('stale treatment compilation result cannot enter snapshot')
    if result.host_surface_id is None:
        raise ValueError('treatment compilation result requires exact host surface')

    geometry = scene_revision.document.r120_semantic_geometry
    if geometry is None:
        raise ValueError('SceneRevision has no SemanticAcousticGeometry')
    surface_matches = [
        surface
        for surface in geometry.surfaces
        if surface.surface_id == result.host_surface_id
    ]
    if len(surface_matches) != 1:
        raise ValueError('treatment host SemanticSurface is missing or ambiguous')
    mapping_matches = [
        item
        for item in compiled.surface_mapping
        if item.source_surface_id == result.host_surface_id
    ]
    if len(mapping_matches) != 1:
        raise ValueError('treatment host R120 surface binding is missing or ambiguous')
    base_mapping = mapping_matches[0]

    overlay_refs: tuple[TreatmentBoundaryOverlaySnapshotRef, ...] = ()
    lifecycle: Literal['proposed', 'installed'] | None = None
    if result.overlay is not None:
        overlay = TreatmentBoundaryOverlay.model_validate(
            result.overlay.model_dump(mode='python')
        )
        if (
            overlay.exact_scene_revision_id != scene_revision.revision_id
            or overlay.exact_scene_revision_content_hash
            != scene_revision.content_hash
            or overlay.exact_semantic_geometry_id != geometry.geometry_id
            or overlay.exact_semantic_geometry_hash_sha256
            != geometry.semantic_hash_sha256
            or overlay.exact_r120_compiled_geometry_id
            != compiled.compiled_geometry_id
            or overlay.exact_r120_compiled_geometry_hash_sha256
            != compiled.compiled_hash_sha256
            or overlay.host_surface_id != result.host_surface_id
        ):
            raise ValueError('treatment overlay exact authority lineage mismatch')
        actual_surface_hash = semantic_surface_host_authority_sha256(
            surface_matches[0]
        )
        if overlay.host_surface_authority_sha256 != actual_surface_hash:
            raise ValueError('treatment overlay host SemanticSurface authority is stale')
        overlay_refs = (_treatment_overlay_snapshot_ref(overlay),)
        lifecycle = overlay.lifecycle

    composition = result.composition_request
    if result.status == 'AVAILABLE':
        if composition is None or result.overlay is None:
            raise ValueError('AVAILABLE treatment result is incomplete')
        if (
            composition.target_domain != result.target_domain
            or composition.host_surface_id != result.host_surface_id
            or composition.exact_scene_revision_id != scene_revision.revision_id
            or composition.exact_scene_revision_content_hash
            != scene_revision.content_hash
            or composition.exact_semantic_geometry_id != geometry.geometry_id
            or composition.exact_semantic_geometry_hash_sha256
            != geometry.semantic_hash_sha256
            or composition.exact_r120_compiled_geometry_id
            != compiled.compiled_geometry_id
            or composition.exact_r120_compiled_geometry_hash_sha256
            != compiled.compiled_hash_sha256
        ):
            raise ValueError('treatment composition exact authority lineage mismatch')
        if (
            composition.base_material_authority != base_mapping.material_authority
            or composition.base_boundary_physics_authority
            != base_mapping.boundary_physics_authority
        ):
            raise ValueError(
                'treatment composition does not preserve exact R120 base boundary'
            )
        if composition.attached_treatment_overlays != (
            result.overlay.as_external_authority_ref(),
        ):
            raise ValueError('treatment composition overlay refs mismatch')
        expected_selected = (
            result.overlay.wave_material_candidate_ref
            if result.target_domain == 'wave'
            else result.overlay.geometric_material_candidate_ref
        )
        if expected_selected is None or composition.selected_treatment_material_authorities != (
            expected_selected,
        ):
            raise ValueError('treatment composition selected material authority mismatch')
        if composition.selected_treatment_lifecycle != result.overlay.lifecycle:
            raise ValueError('treatment composition lifecycle mismatch')
        expected_r120_binding = adapt_treatment_boundary_composition_to_r120(
            composition
        )
        if result.r120_surface_binding != expected_r120_binding:
            raise ValueError('treatment composition R120 surface binding mismatch')
        return TreatmentBoundarySnapshotBinding(
            status=result.status,
            target_domain=result.target_domain,
            host_surface_id=result.host_surface_id,
            composition_id=composition.composition_id,
            composition_authority_version=composition.authority_version,
            composition_hash_sha256=composition.composition_hash_sha256,
            attached_treatment_overlays=overlay_refs,
            selected_treatment_material_authorities=(
                composition.selected_treatment_material_authorities
            ),
            lifecycle=composition.selected_treatment_lifecycle,
            base_material_authority=composition.base_material_authority,
            base_boundary_physics_authority=(
                composition.base_boundary_physics_authority
            ),
            reasons=result.reasons,
        )

    return TreatmentBoundarySnapshotBinding(
        status=result.status,
        target_domain=result.target_domain,
        host_surface_id=result.host_surface_id,
        attached_treatment_overlays=overlay_refs,
        lifecycle=lifecycle,
        base_material_authority=base_mapping.material_authority,
        base_boundary_physics_authority=base_mapping.boundary_physics_authority,
        reasons=result.reasons,
    )


def build_acoustic_scene_snapshot(
    *,
    scene_revision: SceneRevision,
    compiled_geometry: R120CompiledGeometry,
    source_models: tuple[R110CompiledSourceModel, ...],
    receivers: tuple[AcousticReceiverBinding, ...],
    requested_frequency_domain: FrequencyDomain,
    requested_observables: tuple[str, ...],
    system_variant: SystemVariant | None = None,
    environment: SnapshotEnvironmentAuthorityRef | None = None,
    valid_frequency_domain: FrequencyDomain | None = None,
    valid_frequency_domain_authority_ref: ExactExternalAuthorityRef | None = None,
    treatment_boundary_results: tuple[TreatmentBoundaryCompilationResult, ...] = (),
    wave_source_excitation_bindings: tuple[WaveSourceExcitationBinding, ...] = (),
    geometric_acoustics_topology_preflight_ref: ExactExternalAuthorityRef | None = None,
    operating_state: 'RoomOperatingState | None' = None,
    screen_transfer_authorities: tuple[AcousticScreenTransferAuthority, ...] = (),
) -> AcousticSceneSnapshot:
    compiled_geometry = R120CompiledGeometry.model_validate(
        compiled_geometry.model_dump(mode='python')
    )
    if compiled_geometry.exact_scene_revision_id != scene_revision.revision_id:
        raise ValueError('R120 compiled geometry SceneRevision id mismatch')
    if (
        compiled_geometry.exact_scene_revision_content_hash
        != scene_revision.content_hash
    ):
        raise ValueError('R120 compiled geometry SceneRevision hash mismatch')
    geometry = scene_revision.document.r120_semantic_geometry
    if geometry is None:
        raise ValueError('SceneRevision has no SemanticAcousticGeometry')
    if (
        geometry.geometry_id != compiled_geometry.exact_semantic_geometry_id
        or geometry.semantic_hash_sha256
        != compiled_geometry.exact_semantic_geometry_hash_sha256
    ):
        raise ValueError('R120 compiled geometry SemanticAcousticGeometry mismatch')

    if (valid_frequency_domain is None) != (
        valid_frequency_domain_authority_ref is None
    ):
        raise ValueError(
            'valid frequency domain requires an exact authority ref'
        )

    if geometric_acoustics_topology_preflight_ref is not None:
        _require_geometric_topology_preflight_authority(
            preflight_ref=geometric_acoustics_topology_preflight_ref,
            compiled=compiled_geometry,
            requested_observables=requested_observables,
        )

    source_models = tuple(
        sorted(
            (
                R110CompiledSourceModel.model_validate(
                    item.model_dump(mode='python')
                )
                for item in source_models
            ),
            key=lambda item: (item.source_entity_id, item.semantic_sha256),
        )
    )
    source_bindings = tuple(source_binding_from_r110(item) for item in source_models)
    source_by_hash = {item.semantic_sha256: item for item in source_models}
    wave_excitation_bindings = tuple(
        sorted(
            (
                WaveSourceExcitationBinding.model_validate(
                    item.model_dump(mode='python')
                )
                for item in wave_source_excitation_bindings
            ),
            key=lambda item: (item.source_entity_id, item.semantic_sha256),
        )
    )
    wave_binding_source_ids = [
        item.source_entity_id for item in wave_excitation_bindings
    ]
    if len(wave_binding_source_ids) != len(set(wave_binding_source_ids)):
        raise ValueError(
            'wave source excitation bindings must be unique per source entity'
        )
    for binding in wave_excitation_bindings:
        source = source_by_hash.get(binding.r110_compiled_source_sha256)
        if source is None:
            raise ValueError(
                'wave source excitation binding references unknown exact R110 source'
            )
        if (
            binding.source_entity_id != source.source_entity_id
            or binding.equipment_definition_id != source.equipment_definition_id
            or binding.equipment_definition_version
            != source.equipment_definition_version
            or binding.equipment_definition_sha256
            != source.equipment_definition_sha256
        ):
            raise ValueError(
                'wave source excitation binding does not match exact R110 source'
            )

    if source_models and system_variant is None:
        raise ValueError(
            'snapshot with R110 sources requires the exact SystemVariant authority'
        )
    if system_variant is not None:
        if (
            system_variant.document_id != scene_revision.document_id
            or system_variant.baseline_revision_id != scene_revision.revision_id
            or system_variant.baseline_content_hash != scene_revision.content_hash
        ):
            raise ValueError('snapshot SystemVariant/SceneRevision mismatch')
        for source in source_models:
            if (
                source.scene_revision_id != scene_revision.revision_id
                or source.scene_content_hash != scene_revision.content_hash
                or source.system_variant_id != system_variant.variant_id
                or source.system_variant_sha256 != system_variant.variant_sha256
            ):
                raise ValueError(
                    'R110 source does not bind the exact snapshot SceneRevision/SystemVariant'
                )
    elif any(
        source.scene_revision_id != scene_revision.revision_id
        or source.scene_content_hash != scene_revision.content_hash
        for source in source_models
    ):
        raise ValueError('R110 source SceneRevision mismatch')

    screen_entity_ids = tuple(
        sorted(
            entity.entity_id
            for entity in scene_revision.document.entities
            if entity.kind == 'screen'
        )
    )
    screen_authorities = tuple(
        sorted(
            (
                AcousticScreenTransferAuthority.model_validate(
                    item.model_dump(mode='python')
                )
                for item in screen_transfer_authorities
            ),
            key=lambda item: item.screen_entity_id,
        )
    )
    authority_screen_ids = [
        item.screen_entity_id for item in screen_authorities
    ]
    if len(authority_screen_ids) != len(set(authority_screen_ids)):
        raise ValueError(
            'screen transfer authorities must be unique per screen entity'
        )
    screen_entity_id_set = set(screen_entity_ids)
    for authority in screen_authorities:
        if authority.screen_entity_id not in screen_entity_id_set:
            raise ValueError(
                'screen transfer authority references an entity that is not '
                f'a screen in this scene: {authority.screen_entity_id}'
            )
        if (
            authority.document_id is not None
            and authority.document_id != scene_revision.document_id
        ):
            raise ValueError(
                'screen transfer authority document binding mismatch'
            )
    authority_by_screen = {
        item.screen_entity_id: item for item in screen_authorities
    }
    screen_transfer_bindings = tuple(
        ScreenTransferSnapshotBinding(
            screen_entity_id=entity_id,
            screen_transfer_ref=authority_by_screen[entity_id].authority_ref(),
        )
        for entity_id in screen_entity_ids
        if entity_id in authority_by_screen
    )

    requested_observables = _unique(requested_observables)
    if not requested_observables:
        raise ValueError('snapshot must request at least one observable')

    receiver_tuple = tuple(sorted(receivers, key=lambda item: item.receiver_id))
    treatment_bindings = tuple(
        sorted(
            (
                _treatment_binding_from_result(
                    item,
                    scene_revision=scene_revision,
                    compiled=compiled_geometry,
                )
                for item in treatment_boundary_results
            ),
            key=lambda item: (
                item.host_surface_id,
                item.target_domain,
                item.status,
                item.composition_hash_sha256 or '',
            ),
        )
    )
    treatment_keys = [
        (item.host_surface_id, item.target_domain)
        for item in treatment_bindings
    ]
    if len(treatment_keys) != len(set(treatment_keys)):
        raise ValueError(
            'treatment boundary results must be unique per surface/domain'
        )
    snapshot_schema_version = _snapshot_schema_version(
        compiled=compiled_geometry,
        treatment_bindings=treatment_bindings,
        wave_excitation_bindings=wave_excitation_bindings,
        geometric_acoustics_topology_preflight_ref=(
            geometric_acoustics_topology_preflight_ref
        ),
    )
    surface_configuration = _surface_configuration(compiled_geometry)
    boundary_hash = _digest(
        [item.model_dump(mode='json') for item in surface_configuration]
    )
    readiness = _derive_readiness(
        compiled=compiled_geometry,
        sources=source_bindings,
        receivers=receiver_tuple,
        environment=environment,
        requested_observables=requested_observables,
        treatment_bindings=treatment_bindings,
        wave_excitation_bindings=wave_excitation_bindings,
        requested_frequency_domain=requested_frequency_domain,
        valid_frequency_domain=valid_frequency_domain,
        schema_version=snapshot_schema_version,
        geometric_acoustics_topology_preflight_ref=(
            geometric_acoustics_topology_preflight_ref
        ),
        screen_transfer_ready=not screen_entity_ids,
    )

    unresolved = _derive_unresolved_conditions(
        compiled=compiled_geometry,
        sources=source_bindings,
        receivers=receiver_tuple,
        environment=environment,
        readiness=readiness,
        valid_frequency_domain=valid_frequency_domain,
        requested_frequency_domain=requested_frequency_domain,
        treatment_bindings=treatment_bindings,
        wave_excitation_bindings=wave_excitation_bindings,
        screen_entity_ids=screen_entity_ids,
        screen_transfer_bindings=screen_transfer_bindings,
    )

    if snapshot_schema_version == 1:
        snapshot_authority_version = ACOUSTIC_SCENE_SNAPSHOT_V1_AUTHORITY_VERSION
        snapshot_compiler_version = ACOUSTIC_SCENE_SNAPSHOT_V1_COMPILER_VERSION
    elif snapshot_schema_version == 2:
        snapshot_authority_version = ACOUSTIC_SCENE_SNAPSHOT_V2_AUTHORITY_VERSION
        snapshot_compiler_version = ACOUSTIC_SCENE_SNAPSHOT_V2_COMPILER_VERSION
    else:
        snapshot_authority_version = ACOUSTIC_SCENE_SNAPSHOT_AUTHORITY_VERSION
        snapshot_compiler_version = ACOUSTIC_SCENE_SNAPSHOT_COMPILER_VERSION
    if operating_state is not None:
        if operating_state.scene_revision_id != scene_revision.revision_id:
            raise ValueError(
                'operating state pins a different SceneRevision'
            )
        if operating_state.document_id != scene_revision.document_id:
            raise ValueError(
                'operating state belongs to a different document'
            )

    core: dict[str, Any] = {
        'schema_version': snapshot_schema_version,
        'authority_version': snapshot_authority_version,
        'compiler_id': ACOUSTIC_SCENE_SNAPSHOT_COMPILER_ID,
        'compiler_version': snapshot_compiler_version,
        'document_id': scene_revision.document_id,
        'scene_revision_id': scene_revision.revision_id,
        'scene_content_hash': scene_revision.content_hash,
        'system_variant_id': (
            None if system_variant is None else system_variant.variant_id
        ),
        'system_variant_sha256': (
            None if system_variant is None else system_variant.variant_sha256
        ),
        'semantic_geometry_id': geometry.geometry_id,
        'semantic_geometry_sha256': geometry.semantic_hash_sha256,
        'r120_compiled_geometry_id': compiled_geometry.compiled_geometry_id,
        'r120_compiled_geometry_sha256': compiled_geometry.compiled_hash_sha256,
        'compiled_topology_sha256': compiled_geometry.topology_identity_sha256,
        'geometric_tolerance_m': compiled_geometry.geometric_tolerance_m,
        'approximation_error_bound_m': (
            compiled_geometry.approximation_error_bound_m
        ),
        'approximation_error_status': compiled_geometry.approximation_error_status,
        'maximum_dropped_feature_extent_m': (
            compiled_geometry.maximum_dropped_feature_extent_m
        ),
        'acoustic_region_authority_ref': compiled_geometry.region_authority_ref,
        'portal_authority_ref': compiled_geometry.portal_authority_ref,
        'boundary_termination_authority_ref': (
            compiled_geometry.boundary_termination_authority_ref
        ),
        'surface_boundary_configuration': surface_configuration,
        'material_boundary_configuration_sha256': boundary_hash,
        'sources': source_bindings,
        'receivers': receiver_tuple,
        'environment': environment,
        'valid_frequency_domain': valid_frequency_domain,
        'valid_frequency_domain_authority_ref': (
            valid_frequency_domain_authority_ref
        ),
        'requested_frequency_domain': requested_frequency_domain,
        'requested_observables': requested_observables,
        'readiness': readiness,
        'unresolved_conditions': tuple(unresolved),
    }
    if geometric_acoustics_topology_preflight_ref is not None:
        core['geometric_acoustics_topology_preflight_ref'] = (
            geometric_acoustics_topology_preflight_ref
        )
    if operating_state is not None:
        core['operating_state_ref'] = ExactExternalAuthorityRef(
            authority_id=(
                f'room-operating-state:{operating_state.state_id}'
            ),
            authority_version=operating_state.version,
            semantic_hash_sha256=operating_state.semantic_sha256,
        )
    if snapshot_schema_version >= 2:
        core['treatment_boundary_bindings'] = treatment_bindings
    if snapshot_schema_version >= 3:
        core['wave_source_excitation_bindings'] = wave_excitation_bindings
    if screen_transfer_bindings:
        core['screen_transfer_bindings'] = screen_transfer_bindings
    semantic_payload = {
        key: (
            value.model_dump(mode='json')
            if isinstance(value, BaseModel)
            else [
                item.model_dump(mode='json')
                if isinstance(item, BaseModel)
                else item
                for item in value
            ]
            if isinstance(value, tuple)
            else value
        )
        for key, value in core.items()
    }
    if snapshot_schema_version == 1:
        readiness_payload = semantic_payload.get('readiness')
        if isinstance(readiness_payload, dict):
            readiness_payload.pop('geometric_boundary_ready', None)
    digest = _digest(semantic_payload)
    return AcousticSceneSnapshot(
        snapshot_id=f'acoustic-scene-snapshot:{digest}',
        semantic_sha256=digest,
        **core,
    )


def build_acoustic_prediction_request(
    *,
    snapshot: AcousticSceneSnapshot,
    model_solver_role_id: str,
    requested_frequency_domain: FrequencyDomain,
    requested_observables: tuple[str, ...],
    numerical_fidelity_policy_ref: ExactExternalAuthorityRef,
) -> AcousticPredictionRequest:
    snapshot = AcousticSceneSnapshot.model_validate(
        snapshot.model_dump(mode='python')
    )
    requested_observables = _unique(requested_observables)
    if not requested_observables:
        raise ValueError('prediction request must contain at least one observable')
    if any(
        observable not in snapshot.requested_observables
        for observable in requested_observables
    ):
        raise ValueError(
            'prediction request observable was not declared by AcousticSceneSnapshot'
        )
    core = {
        'schema_version': ACOUSTIC_PREDICTION_REQUEST_SCHEMA_VERSION,
        'acoustic_scene_snapshot_id': snapshot.snapshot_id,
        'acoustic_scene_snapshot_sha256': snapshot.semantic_sha256,
        'model_solver_role_id': model_solver_role_id,
        'requested_frequency_domain': requested_frequency_domain.model_dump(
            mode='json'
        ),
        'requested_observables': list(requested_observables),
        'numerical_fidelity_policy_ref': (
            numerical_fidelity_policy_ref.model_dump(mode='json')
        ),
    }
    canonical = canonical_prediction_json(core)
    input_hash = prediction_input_hash(canonical)
    semantic_hash = _digest(core)
    return AcousticPredictionRequest(
        request_id=f'acoustic-prediction-request:{semantic_hash}',
        request_semantic_sha256=semantic_hash,
        acoustic_scene_snapshot_id=snapshot.snapshot_id,
        acoustic_scene_snapshot_sha256=snapshot.semantic_sha256,
        model_solver_role_id=model_solver_role_id,
        requested_frequency_domain=requested_frequency_domain,
        requested_observables=requested_observables,
        numerical_fidelity_policy_ref=numerical_fidelity_policy_ref,
        deterministic_input_hash=input_hash,
    )
