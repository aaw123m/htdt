from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest

from htdt.cad_acoustic_snapshot import (
    AcousticSceneSnapshot,
    ReceiverMeasurementAuthority,
    SnapshotEnvironmentAuthorityRef,
    _digest,
    build_acoustic_prediction_request,
    build_acoustic_scene_snapshot,
    receiver_binding_from_scene,
)
from htdt.cad_acoustic_snapshot_repository import (
    AcousticSnapshotAuthorityResolvers,
    CadAcousticSnapshotRepository,
)
from htdt.cad_directivity import NORMALIZED_JSON_DIRECTIVITY_ADAPTER
from htdt.cad_directivity_repository import CadDirectivityRepository
from htdt.cad_equipment import (
    AngleDomain,
    DirectivityCapability,
    DirectivityDomain,
    EquipmentDataProvenance,
    FrequencyDomain,
    InterpolationProvenance,
    build_equipment_definition,
)
from htdt.cad_equipment_evidence import build_equipment_manual_evidence
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_prediction_request import rectangular_geometry_request_identity
from htdt.cad_r110_source import compile_r110_source_model
from htdt.cad_r110_source_repository import CadR110SourceRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_screen_transfer import build_screen_transfer
from htdt.cad_scene import (
    Direction3,
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    EquipmentBindingRef,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.r120_geometry_compiler import (
    AcousticRegionDeclaration,
    ExactExternalAuthorityRef,
    SurfaceBoundaryAuthorityBinding,
    compile_r120_geometry,
    make_acoustic_region_authority,
    make_boundary_termination_authority,
    make_portal_authority,
    make_r120_geometry_compilation_request,
)
from htdt.r120_geometry_compiler_repository import R120GeometryCompilerRepository
from htdt.raw_mesh import import_raw_visual_mesh
from htdt.semantic_geometry import (
    SurfaceSemanticAssignment,
    convert_raw_visual_mesh_to_semantic_geometry,
    explicit_identity_source_to_scene_transform,
    make_semantic_geometry_conversion_request,
    raw_triangle_ids,
)


CLOSED_TETRA = b'''\
v 0 0 0
v 5 0 0
v 0 4 0
v 0 0 2.5
f 1 3 2
f 1 2 4
f 1 4 3
f 2 3 4
'''

NOW = '2026-09-19T13:30:00+00:00'


def _save_equipment(repository, definition) -> None:
    """Persist explicit manual evidence for every cited provenance, then save."""
    for evidence in build_equipment_manual_evidence(
        definition,
        actor='equipment-test-fixture',
        recorded_at_utc=NOW,
    ):
        repository.save_evidence(evidence)
    repository.save_definition(definition)


def _ref(name: str, char: str) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=name,
        authority_version='fixture-v1',
        semantic_hash_sha256=char * 64,
    )


def _ref_key(ref: ExactExternalAuthorityRef) -> tuple[str, str, str]:
    return (
        ref.authority_id,
        ref.authority_version,
        ref.semantic_hash_sha256,
    )


def _snapshot_authority_resolvers(
    snapshot,
    *,
    preflight_graph=None,
) -> AcousticSnapshotAuthorityResolvers:
    """Honest registry resolving every external authority a snapshot claims."""
    external: dict[tuple[str, str, str], ExactExternalAuthorityRef] = {}
    for ref in (
        snapshot.acoustic_region_authority_ref,
        snapshot.portal_authority_ref,
        snapshot.boundary_termination_authority_ref,
    ):
        if ref is not None:
            external[_ref_key(ref)] = ref
    for surface in snapshot.surface_boundary_configuration:
        for ref in (
            surface.material_authority,
            surface.boundary_physics_authority,
        ):
            if ref is not None:
                external[_ref_key(ref)] = ref
    for binding in snapshot.treatment_boundary_bindings:
        for ref in (
            binding.base_material_authority,
            binding.base_boundary_physics_authority,
            *binding.selected_treatment_material_authorities,
        ):
            if ref is not None:
                external[_ref_key(ref)] = ref

    environment = snapshot.environment
    domain = snapshot.valid_frequency_domain
    domain_ref = snapshot.valid_frequency_domain_authority_ref
    measurements = {
        _ref_key(receiver.measurement_authority_ref): receiver
        for receiver in snapshot.receivers
        if receiver.measurement_authority_ref is not None
    }
    preflight_ref = snapshot.geometric_acoustics_topology_preflight_ref

    def resolve_external(ref: ExactExternalAuthorityRef):
        return external.get(_ref_key(ref))

    def resolve_environment(ref: ExactExternalAuthorityRef):
        if environment is None or ref != environment.authority:
            return None
        return environment

    def resolve_sound_speed(ref: ExactExternalAuthorityRef):
        if (
            environment is None
            or ref != environment.sound_speed_source_authority
        ):
            return None
        return environment.sound_speed_m_s

    def resolve_temperature(ref: ExactExternalAuthorityRef):
        if (
            environment is None
            or ref != environment.temperature_source_authority
        ):
            return None
        return environment.temperature_c

    def resolve_measurement(ref: ExactExternalAuthorityRef):
        receiver = measurements.get(_ref_key(ref))
        if receiver is None:
            return None
        return ReceiverMeasurementAuthority(
            authority_ref=ref,
            entity_id=receiver.entity_id,
            world_position=receiver.world_position,
            orientation=receiver.orientation,
        )

    def resolve_domain(ref: ExactExternalAuthorityRef):
        if domain_ref is None or ref != domain_ref:
            return None
        return domain

    def resolve_preflight(ref: ExactExternalAuthorityRef):
        if (
            preflight_graph is None
            or preflight_ref is None
            or ref != preflight_ref
            or preflight_graph.as_external_ref() != ref
        ):
            return None
        return preflight_graph

    return AcousticSnapshotAuthorityResolvers(
        environment=resolve_environment,
        sound_speed_source=resolve_sound_speed,
        temperature_source=resolve_temperature,
        receiver_measurement=resolve_measurement,
        valid_frequency_domain=resolve_domain,
        geometric_topology_preflight=resolve_preflight,
        external_authority=resolve_external,
    )


def _domain() -> DirectivityDomain:
    return DirectivityDomain(
        frequency=FrequencyDomain(minimum_hz=500.0, maximum_hz=1000.0),
        horizontal=AngleDomain(minimum_deg=-30.0, maximum_deg=30.0),
        vertical=AngleDomain(minimum_deg=0.0, maximum_deg=0.0),
    )


def _provenance(source_hash: str, name: str) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='measured',
        source_name=name,
        source_version='1',
        source_reference=f'{name}-fixture',
        source_sha256=source_hash,
    )


def _persisted_directivity(definition_id: str, kind: str):
    """Build real imported source bytes, a bound definition and the parsed
    dataset so persistence verifies and replays the exact source."""
    source_payload = {
        'schema': 'htdt.normalized-directivity.v1',
        'dataset_id': f'{definition_id}-dataset',
        'version': '1',
        'source_format': 'custom',
        'evidence_kind': 'measured',
        'source_name': definition_id,
        'source_version': '1',
        'source_reference': f'{definition_id}-fixture',
        'kind': kind,
        'coordinate_convention': {
            'angle_semantics': 'horizontal_vertical',
            'horizontal_wrap': 'none',
            'reference_axis': 'equipment_acoustic_reference_axis',
            'azimuth_positive': 'left',
            'elevation_positive': 'up',
            'angle_unit': 'degree',
        },
        'normalization': {
            'source_magnitude_unit': 'db',
            'normalized_magnitude_unit': 'db',
            'reference': 'on_axis_per_frequency',
            'reference_level_db': None,
            'conversion_version': 'pressure-amplitude-db20-v1',
        },
        'phase_reference': (
            'acoustic_reference_point/source-t0'
            if kind == 'complex'
            else None
        ),
        'interpolation_method': 'linear',
        'interpolation_implementation': 'snapshot-fixture-linear',
        'interpolation_version': '1',
        'frequencies_hz': [500.0, 1000.0],
        'horizontal_angles_deg': [-30.0, 0.0, 30.0],
        'vertical_angles_deg': [0.0],
        'samples': [
            {
                'frequency_hz': frequency_hz,
                'horizontal_angle_deg': horizontal_angle_deg,
                'vertical_angle_deg': 0.0,
                'magnitude': (
                    0.0 if horizontal_angle_deg == 0.0 else -6.0
                ),
                'phase_deg': (
                    horizontal_angle_deg / 3.0
                    if kind == 'complex'
                    else None
                ),
            }
            for frequency_hz in (500.0, 1000.0)
            for horizontal_angle_deg in (-30.0, 0.0, 30.0)
        ],
    }
    source_bytes = json.dumps(
        source_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    source_hash = sha256(source_bytes).hexdigest()
    provenance = _provenance(source_hash, definition_id)
    definition = build_equipment_definition(
        definition_id=definition_id,
        version='1',
        identity_kind='user_defined',
        user_label=definition_id,
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
        acoustic_reference_point_m=Offset3(x_m=0.0, y_m=0.1, z_m=0.0),
        directivity=DirectivityCapability(
            tier=kind,
            data_format='custom',
            provenance=provenance,
            data_asset_sha256=source_hash,
            valid_domain=_domain(),
            interpolation=InterpolationProvenance(
                method='linear',
                implementation='snapshot-fixture-linear',
                implementation_version='1',
                provenance=provenance,
            ),
            coherent_phase=(kind == 'complex'),
            phase_reference=(
                'acoustic_reference_point/source-t0'
                if kind == 'complex'
                else None
            ),
        ),
    )
    dataset = NORMALIZED_JSON_DIRECTIVITY_ADAPTER.parse(
        source_bytes,
        definition,
    )
    return source_bytes, definition, dataset


def _semantic_geometry():
    mesh = import_raw_visual_mesh(
        CLOSED_TETRA,
        source_name='acoustic-snapshot-fixture.obj',
    )
    request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=None,
        source_to_scene_transform=explicit_identity_source_to_scene_transform(
            reason='fixture OBJ coordinates are explicit HTDT metres',
        ),
        surface_assignments=(
            SurfaceSemanticAssignment(
                surface_key='room-shell',
                triangle_ids=raw_triangle_ids(mesh),
                semantic_class='room_boundary',
            ),
        ),
    )
    return convert_raw_visual_mesh_to_semantic_geometry(mesh, request)


def _speaker(
    entity_id: str,
    role: str,
    *,
    x_m: float,
) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name=role,
        speaker_role=role,
        position=Position3(x_m=x_m, y_m=1.0, z_m=1.0),
        size_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
        aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
    )


def _scene_document(
    *,
    document_id: str = 'acoustic-snapshot-fixture',
    include_screen: bool = False,
):
    entities = [
        _speaker('speaker-fl', 'FL', x_m=1.0),
        _speaker('speaker-fr', 'FR', x_m=4.0),
        SceneEntity(
            entity_id='receiver-mlp',
            kind='measurement_point',
            name='MLP',
            position=Position3(x_m=2.5, y_m=3.0, z_m=1.1),
        ),
        SceneEntity(
            entity_id='receiver-rear',
            kind='measurement_point',
            name='Rear seat',
            position=Position3(x_m=2.5, y_m=3.5, z_m=1.1),
        ),
    ]
    if include_screen:
        entities.append(
            SceneEntity(
                entity_id='screen-projection',
                kind='screen',
                name='Projection screen',
                position=Position3(x_m=2.5, y_m=0.5, z_m=1.2),
                size_m=Size3(x_m=2.0, y_m=1.2, z_m=0.02),
            )
        )
    return SceneDocument(
        document_id=document_id,
        schema_version=4,
        room=None,
        r120_semantic_geometry=_semantic_geometry(),
        entities=tuple(entities),
    )


def _environment(hash_char: str = '9') -> SnapshotEnvironmentAuthorityRef:
    return SnapshotEnvironmentAuthorityRef(
        authority=_ref('fixture-environment', hash_char),
        sound_speed_m_s=342.7,
        sound_speed_source_authority=_ref(
            'fixture-sound-speed-source',
            '8',
        ),
    )


def _fixture(
    tmp_path: Path,
    *,
    include_material: bool = True,
    portal_mode: str = 'explicit_none',
    include_environment: bool = True,
    include_screen: bool = False,
    screen_transfer_authorities=(),
):
    db = tmp_path / 'cad.sqlite3'
    scene_repository = SceneRepository(db)
    revision = scene_repository.save(
        _scene_document(include_screen=include_screen),
        parent_revision_id=None,
    ).revision

    variant_repository = CadSystemVariantRepository(scene_repository)
    equipment_repository = CadEquipmentRepository(
        scene_repository,
        variant_repository,
    )
    directivity_repository = CadDirectivityRepository(
        scene_repository,
        equipment_repository,
    )
    r110_repository = CadR110SourceRepository(
        scene_repository,
        variant_repository=variant_repository,
        equipment_repository=equipment_repository,
        directivity_repository=directivity_repository,
    )
    r120_repository = R120GeometryCompilerRepository(scene_repository)

    (
        magnitude_bytes,
        magnitude_definition,
        magnitude_dataset,
    ) = _persisted_directivity('fixture-magnitude', 'magnitude_only')
    (
        complex_bytes,
        complex_definition,
        complex_dataset,
    ) = _persisted_directivity('fixture-complex', 'complex')
    _save_equipment(equipment_repository, magnitude_definition)
    _save_equipment(equipment_repository, complex_definition)
    directivity_repository.save_dataset(
        magnitude_dataset,
        source_bytes=magnitude_bytes,
        source_filename='fixture-magnitude.normalized.json',
        media_type='application/json',
        declared_schema='htdt.normalized-directivity.v1',
    )
    directivity_repository.save_dataset(
        complex_dataset,
        source_bytes=complex_bytes,
        source_filename='fixture-complex.normalized.json',
        media_type='application/json',
        declared_schema='htdt.normalized-directivity.v1',
    )

    variant = build_system_variant(
        baseline=revision,
        name='snapshot-fixture-variant',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front left'),
            ChannelRoleBinding(role_id='FR', display_name='Front right'),
        ),
        proposed_entities=(),
        equipment_bindings=(
            EquipmentBindingRef(
                entity_id='speaker-fl',
                equipment_definition_id=magnitude_definition.definition_id,
                equipment_definition_version=magnitude_definition.version,
                equipment_definition_sha256=magnitude_definition.semantic_sha256,
            ),
            EquipmentBindingRef(
                entity_id='speaker-fr',
                equipment_definition_id=complex_definition.definition_id,
                equipment_definition_version=complex_definition.version,
                equipment_definition_sha256=complex_definition.semantic_sha256,
            ),
        ),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant)

    magnitude_source = compile_r110_source_model(
        scene_revision=revision,
        system_variant=variant,
        source_entity_id='speaker-fl',
        equipment_definition=magnitude_definition,
        directivity_dataset=magnitude_dataset,
    )
    complex_source = compile_r110_source_model(
        scene_revision=revision,
        system_variant=variant,
        source_entity_id='speaker-fr',
        equipment_definition=complex_definition,
        directivity_dataset=complex_dataset,
    )
    r110_repository.save_model(magnitude_source)
    r110_repository.save_model(complex_source)

    geometry = revision.document.r120_semantic_geometry
    assert geometry is not None
    surface_id = geometry.surfaces[0].surface_id
    region = make_acoustic_region_authority(
        (
            AcousticRegionDeclaration(
                region_id='room-air',
                boundary_surface_ids=(surface_id,),
            ),
        )
    )
    portals = make_portal_authority(
        declaration_mode=portal_mode,
    )
    terminations = make_boundary_termination_authority(
        declaration_mode=(
            'unknown' if portal_mode == 'unknown' else 'explicit_none'
        ),
    )
    bindings = ()
    if include_material:
        bindings = (
            SurfaceBoundaryAuthorityBinding(
                source_surface_id=surface_id,
                material_authority=_ref('fixture-material', 'c'),
                boundary_physics_authority=_ref(
                    'fixture-boundary-physics',
                    'd',
                ),
            ),
        )
    compile_request = make_r120_geometry_compilation_request(
        revision,
        geometric_tolerance_m=1.0e-6,
    )
    compiled = compile_r120_geometry(
        revision,
        compile_request,
        surface_boundary_bindings=bindings,
        region_authority=region,
        portal_authority=portals,
        boundary_termination_authority=terminations,
    )
    r120_repository.save_compiled_geometry(
        compiled,
        surface_boundary_bindings=bindings,
        region_authority=region,
        portal_authority=portals,
        boundary_termination_authority=terminations,
    )

    observables = (
        'magnitude_response',
        'complex_pressure',
        'deterministic_paths',
    )
    receivers = (
        receiver_binding_from_scene(
            scene_revision=revision,
            system_variant=variant,
            entity_id='receiver-mlp',
            requested_output_capabilities=observables,
        ),
        receiver_binding_from_scene(
            scene_revision=revision,
            system_variant=variant,
            entity_id='receiver-rear',
            requested_output_capabilities=observables,
        ),
    )
    snapshot = build_acoustic_scene_snapshot(
        scene_revision=revision,
        system_variant=variant,
        compiled_geometry=compiled,
        source_models=(magnitude_source, complex_source),
        receivers=receivers,
        requested_frequency_domain=FrequencyDomain(
            minimum_hz=500.0,
            maximum_hz=1000.0,
        ),
        requested_observables=observables,
        environment=_environment() if include_environment else None,
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=500.0,
            maximum_hz=1000.0,
        ),
        valid_frequency_domain_authority_ref=_ref(
            'fixture-valid-frequency-domain',
            'e',
        ),
        screen_transfer_authorities=screen_transfer_authorities,
    )

    return {
        'scene_repository': scene_repository,
        'variant_repository': variant_repository,
        'equipment_repository': equipment_repository,
        'directivity_repository': directivity_repository,
        'r110_repository': r110_repository,
        'r120_repository': r120_repository,
        'revision': revision,
        'variant': variant,
        'magnitude_source': magnitude_source,
        'complex_source': complex_source,
        'compiled': compiled,
        'receivers': receivers,
        'snapshot': snapshot,
    }


def _policy() -> ExactExternalAuthorityRef:
    return _ref('fixture-numerical-fidelity-policy', 'f')


def _fidelity_policy(
    *,
    ref: ExactExternalAuthorityRef | None = None,
    domain: str = 'geometric',
    roles: tuple[str, ...] = ('future-r150-geometric-role',),
    observables: tuple[str, ...] = ('deterministic_paths',),
    minimum_hz: float = 500.0,
    maximum_hz: float = 1000.0,
) -> AcousticNumericalFidelityPolicy:
    return AcousticNumericalFidelityPolicy(
        authority_ref=_policy() if ref is None else ref,
        acoustic_domain=domain,
        model_solver_role_ids=roles,
        supported_observables=observables,
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=minimum_hz,
            maximum_hz=maximum_hz,
        ),
        parameter_bounds={
            'maximum_mesh_element_extent_m': 0.25,
            'convergence_tolerance': 1.0e-6,
        },
    )


def _fidelity_resolver(*policies: AcousticNumericalFidelityPolicy):
    registry = {policy.authority_ref: policy for policy in policies}

    def resolve(ref: ExactExternalAuthorityRef):
        return registry.get(ref)

    return registry, resolve


def test_closed_r120_snapshot_preserves_exact_geometry_and_topology(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    snapshot = fx['snapshot']
    compiled = fx['compiled']
    revision = fx['revision']
    geometry = revision.document.r120_semantic_geometry
    assert geometry is not None

    assert snapshot.document_id == revision.document_id
    assert snapshot.scene_revision_id == revision.revision_id
    assert snapshot.scene_content_hash == revision.content_hash
    assert snapshot.semantic_geometry_id == geometry.geometry_id
    assert snapshot.semantic_geometry_sha256 == geometry.semantic_hash_sha256
    assert snapshot.r120_compiled_geometry_id == compiled.compiled_geometry_id
    assert (
        snapshot.r120_compiled_geometry_sha256
        == compiled.compiled_hash_sha256
    )
    assert snapshot.compiled_topology_sha256 == compiled.topology_identity_sha256
    assert snapshot.readiness.geometry_ready is True


def test_magnitude_and_complex_r110_sources_keep_exact_capabilities(
    tmp_path: Path,
) -> None:
    snapshot = _fixture(tmp_path)['snapshot']
    by_entity = {item.source_entity_id: item for item in snapshot.sources}

    magnitude = by_entity['speaker-fl']
    assert magnitude.directivity_capability == 'magnitude_only'
    assert (
        magnitude.geometric_directivity_state
        == 'SUPPORTED_FOR_GEOMETRIC_DIRECTIVITY'
    )
    assert magnitude.complex_directivity_state == 'UNSUPPORTED'
    assert magnitude.directivity_dataset_sha256 is not None

    complex_source = by_entity['speaker-fr']
    assert complex_source.directivity_capability == 'complex'
    assert (
        complex_source.complex_directivity_state
        == 'SUPPORTED_FOR_COMPLEX_DIRECTIVITY'
    )
    assert complex_source.directivity_dataset_sha256 is not None


def test_wave_excitation_blocked_source_blocks_wave_prediction_readiness(
    tmp_path: Path,
) -> None:
    snapshot = _fixture(tmp_path)['snapshot']

    assert snapshot.readiness.wave_source_ready is False
    assert 'wave_source_excitation_blocked' in snapshot.unresolved_conditions
    pressure = next(
        item
        for item in snapshot.readiness.observable_readiness
        if item.observable == 'complex_pressure'
    )
    assert pressure.state == 'BLOCKED'
    assert 'wave_source_not_ready' in pressure.reasons


def test_multiple_sources_and_receiver_set_are_explicit(
    tmp_path: Path,
) -> None:
    snapshot = _fixture(tmp_path)['snapshot']

    assert [item.source_entity_id for item in snapshot.sources] == [
        'speaker-fl',
        'speaker-fr',
    ]
    assert [item.entity_id for item in snapshot.receivers] == [
        'receiver-mlp',
        'receiver-rear',
    ]
    assert snapshot.readiness.receiver_ready is True
    assert all(
        item.acoustic_reference_semantics
        == 'scene_acoustic_reference_position'
        for item in snapshot.receivers
    )


def test_missing_material_keeps_geometry_snapshot_but_blocks_boundary_readiness(
    tmp_path: Path,
) -> None:
    snapshot = _fixture(
        tmp_path,
        include_material=False,
    )['snapshot']

    assert snapshot.readiness.geometry_ready is True
    assert snapshot.readiness.wave_boundary_ready is False
    assert 'material_assignment_missing' in snapshot.unresolved_conditions
    assert snapshot.surface_boundary_configuration[0].material_authority is None
    assert (
        snapshot.surface_boundary_configuration[0].boundary_physics_authority
        is None
    )


def test_portal_and_termination_unknown_remain_unresolved(
    tmp_path: Path,
) -> None:
    snapshot = _fixture(
        tmp_path,
        portal_mode='unknown',
    )['snapshot']

    assert snapshot.readiness.wave_boundary_ready is False
    assert 'portal_definition_missing' in snapshot.unresolved_conditions
    assert (
        'boundary_termination_definition_missing'
        in snapshot.unresolved_conditions
        or snapshot.boundary_termination_authority_ref is not None
    )


def test_environment_unknown_does_not_add_implicit_343_m_s(
    tmp_path: Path,
) -> None:
    snapshot = _fixture(
        tmp_path,
        include_environment=False,
    )['snapshot']

    assert snapshot.environment is None
    assert snapshot.readiness.environment_ready is False
    assert 'environment_unknown' in snapshot.unresolved_conditions
    magnitude = next(
        item
        for item in snapshot.readiness.observable_readiness
        if item.observable == 'magnitude_response'
    )
    assert magnitude.state == 'BLOCKED'


def test_scene_revision_mismatch_is_rejected(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    other = fx['scene_repository'].save(
        _scene_document(document_id='other-document'),
        parent_revision_id=None,
    ).revision

    with pytest.raises(ValueError):
        build_acoustic_scene_snapshot(
            scene_revision=other,
            system_variant=fx['variant'],
            compiled_geometry=fx['compiled'],
            source_models=(
                fx['magnitude_source'],
                fx['complex_source'],
            ),
            receivers=fx['receivers'],
            requested_frequency_domain=FrequencyDomain(
                minimum_hz=500.0,
                maximum_hz=1000.0,
            ),
            requested_observables=('magnitude_response',),
        )


def test_compiled_geometry_hash_mismatch_is_rejected(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    bad = fx['compiled'].model_copy(
        update={'compiled_hash_sha256': '0' * 64}
    )

    with pytest.raises(ValueError):
        build_acoustic_scene_snapshot(
            scene_revision=fx['revision'],
            system_variant=fx['variant'],
            compiled_geometry=bad,
            source_models=(fx['magnitude_source'],),
            receivers=fx['receivers'],
            requested_frequency_domain=FrequencyDomain(
                minimum_hz=500.0,
                maximum_hz=1000.0,
            ),
            requested_observables=('magnitude_response',),
        )


def test_source_hash_mismatch_is_rejected(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    bad = fx['magnitude_source'].model_copy(
        update={'semantic_sha256': '0' * 64}
    )

    with pytest.raises(ValueError):
        build_acoustic_scene_snapshot(
            scene_revision=fx['revision'],
            system_variant=fx['variant'],
            compiled_geometry=fx['compiled'],
            source_models=(bad,),
            receivers=fx['receivers'],
            requested_frequency_domain=FrequencyDomain(
                minimum_hz=500.0,
                maximum_hz=1000.0,
            ),
            requested_observables=('magnitude_response',),
        )


def test_same_exact_input_has_same_snapshot_hash_even_if_input_order_differs(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    original = fx['snapshot']

    repeated = build_acoustic_scene_snapshot(
        scene_revision=fx['revision'],
        system_variant=fx['variant'],
        compiled_geometry=fx['compiled'],
        source_models=(
            fx['complex_source'],
            fx['magnitude_source'],
        ),
        receivers=tuple(reversed(fx['receivers'])),
        requested_frequency_domain=original.requested_frequency_domain,
        requested_observables=original.requested_observables,
        environment=original.environment,
        valid_frequency_domain=original.valid_frequency_domain,
        valid_frequency_domain_authority_ref=(
            original.valid_frequency_domain_authority_ref
        ),
    )

    assert repeated == original
    assert repeated.semantic_sha256 == original.semantic_sha256
    assert repeated.snapshot_id == original.snapshot_id


def test_save_reopen_reresolves_scene_variant_r120_and_r110_authorities(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    repository = CadAcousticSnapshotRepository(
        fx['scene_repository'],
        variant_repository=fx['variant_repository'],
        r110_repository=fx['r110_repository'],
        r120_repository=fx['r120_repository'],
        authority_resolvers=_snapshot_authority_resolvers(fx['snapshot']),
    )
    saved = repository.save_snapshot(fx['snapshot'])

    reopened = CadAcousticSnapshotRepository(
        SceneRepository(fx['scene_repository'].path),
        authority_resolvers=_snapshot_authority_resolvers(fx['snapshot']),
    ).get_snapshot(saved.snapshot_id)

    assert reopened == saved
    assert reopened is not None
    assert reopened.scene_content_hash == fx['revision'].content_hash
    assert (
        reopened.r120_compiled_geometry_sha256
        == fx['compiled'].compiled_hash_sha256
    )
    assert {
        item.r110_compiled_source_sha256 for item in reopened.sources
    } == {
        fx['magnitude_source'].semantic_sha256,
        fx['complex_source'].semantic_sha256,
    }


def test_environment_change_changes_snapshot_and_prediction_input_hash(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    first = fx['snapshot']
    second = build_acoustic_scene_snapshot(
        scene_revision=fx['revision'],
        system_variant=fx['variant'],
        compiled_geometry=fx['compiled'],
        source_models=(
            fx['magnitude_source'],
            fx['complex_source'],
        ),
        receivers=fx['receivers'],
        requested_frequency_domain=first.requested_frequency_domain,
        requested_observables=first.requested_observables,
        environment=_environment('7'),
        valid_frequency_domain=first.valid_frequency_domain,
        valid_frequency_domain_authority_ref=(
            first.valid_frequency_domain_authority_ref
        ),
    )

    request_a = build_acoustic_prediction_request(
        snapshot=first,
        model_solver_role_id='future-r130-wave-role',
        requested_frequency_domain=first.requested_frequency_domain,
        requested_observables=('complex_pressure',),
        numerical_fidelity_policy_ref=_policy(),
    )
    request_b = build_acoustic_prediction_request(
        snapshot=second,
        model_solver_role_id='future-r130-wave-role',
        requested_frequency_domain=second.requested_frequency_domain,
        requested_observables=('complex_pressure',),
        numerical_fidelity_policy_ref=_policy(),
    )

    assert second.snapshot_id != first.snapshot_id
    assert second.semantic_sha256 != first.semantic_sha256
    assert request_b.deterministic_input_hash != request_a.deterministic_input_hash


def test_acoustic_prediction_request_is_append_only_and_exact_snapshot_bound(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    _registry, fidelity_resolver = _fidelity_resolver(_fidelity_policy())
    repository = CadAcousticSnapshotRepository(
        fx['scene_repository'],
        variant_repository=fx['variant_repository'],
        r110_repository=fx['r110_repository'],
        r120_repository=fx['r120_repository'],
        fidelity_policy_resolver=fidelity_resolver,
        authority_resolvers=_snapshot_authority_resolvers(fx['snapshot']),
    )
    repository.save_snapshot(fx['snapshot'])
    request = build_acoustic_prediction_request(
        snapshot=fx['snapshot'],
        model_solver_role_id='future-r150-geometric-role',
        requested_frequency_domain=fx['snapshot'].requested_frequency_domain,
        requested_observables=('deterministic_paths',),
        numerical_fidelity_policy_ref=_policy(),
    )

    repository.save_prediction_request(request)
    reopened = CadAcousticSnapshotRepository(
        SceneRepository(fx['scene_repository'].path),
        fidelity_policy_resolver=fidelity_resolver,
        authority_resolvers=_snapshot_authority_resolvers(fx['snapshot']),
    ).get_prediction_request(request.request_id)

    assert reopened == request
    assert (
        reopened.acoustic_scene_snapshot_sha256
        == fx['snapshot'].semantic_sha256
    )


def test_unknown_observable_is_explicitly_unsupported(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    snapshot = build_acoustic_scene_snapshot(
        scene_revision=fx['revision'],
        system_variant=fx['variant'],
        compiled_geometry=fx['compiled'],
        source_models=(fx['magnitude_source'],),
        receivers=fx['receivers'],
        requested_frequency_domain=FrequencyDomain(
            minimum_hz=500.0,
            maximum_hz=1000.0,
        ),
        requested_observables=('future_unknown_observable',),
        environment=_environment(),
    )

    status = snapshot.readiness.observable_readiness[0]
    assert status.observable == 'future_unknown_observable'
    assert status.state == 'UNSUPPORTED'
    assert snapshot.readiness.requested_observable_ready is False


def test_requested_band_outside_snapshot_valid_domain_blocks_readiness(
    tmp_path: Path,
) -> None:
    """#977: a known narrower valid domain cannot coexist with READY."""
    fx = _fixture(tmp_path)
    original = fx['snapshot']
    snapshot = build_acoustic_scene_snapshot(
        scene_revision=fx['revision'],
        system_variant=fx['variant'],
        compiled_geometry=fx['compiled'],
        source_models=(fx['magnitude_source'], fx['complex_source']),
        receivers=fx['receivers'],
        requested_frequency_domain=FrequencyDomain(
            minimum_hz=20.0,
            maximum_hz=20000.0,
        ),
        requested_observables=original.requested_observables,
        environment=original.environment,
        valid_frequency_domain=original.valid_frequency_domain,
        valid_frequency_domain_authority_ref=(
            original.valid_frequency_domain_authority_ref
        ),
    )

    assert snapshot.valid_frequency_domain is not None
    assert not all(
        snapshot.valid_frequency_domain.contains(bound)
        for bound in (20.0, 20000.0)
    )
    for status in snapshot.readiness.observable_readiness:
        if status.observable == 'deterministic_paths':
            # Geometry paths are frequency-agnostic; the acoustic-domain
            # mismatch is recorded but does not block them.
            continue
        assert status.state != 'READY'
        assert any(
            'requested_frequency_outside_snapshot_valid_domain'
            in reason
            for reason in status.reasons
        )
    assert snapshot.readiness.requested_observable_ready is False
    assert (
        'requested_frequency_outside_snapshot_valid_domain'
        in snapshot.unresolved_conditions
    )


def test_rectangular_legacy_prediction_request_identity_is_unchanged(
    tmp_path: Path,
) -> None:
    repository = SceneRepository(tmp_path / 'legacy.sqlite3')
    document = SceneDocument(
        document_id='legacy-rectangular',
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                position=Position3(x_m=1.2, y_m=0.8, z_m=1.0),
                size_m=Size3(x_m=0.2, y_m=0.3, z_m=0.4),
                acoustic_reference_offset_m=Offset3(),
                speaker_role='FL',
            ),
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )
    revision = repository.save(
        document,
        parent_revision_id=None,
    ).revision

    identity = rectangular_geometry_request_identity(
        revision,
        'point-mlp',
        max_mode_hz=140.0,
        sound_speed_m_s=342.5,
    )

    assert identity.model_id
    assert identity.model_version
    assert identity.geometry_compatibility == 'exact_for_model_geometry'
    assert len(identity.input_hash) == 64


from htdt.cad_acoustic_solver_adapter import (
    AcousticNumericalFidelityPolicy,
    bind_prediction_request_to_solver_adapter,
    build_acoustic_solver_adapter_descriptor,
)


def _adapter_descriptor(
    *,
    role: str,
    domain: str,
    observables: tuple[str, ...],
    minimum_hz: float = 500.0,
    maximum_hz: float = 1000.0,
    solver_hash_char: str = '1',
):
    return build_acoustic_solver_adapter_descriptor(
        adapter_id=f'fixture-{domain}-adapter',
        adapter_version='1',
        model_solver_role_id=role,
        acoustic_domain=domain,
        solver_implementation_ref=_ref(
            f'fixture-{domain}-solver-build',
            solver_hash_char,
        ),
        solver_configuration_schema_ref=_ref(
            f'fixture-{domain}-config-schema',
            '2',
        ),
        supported_snapshot_schema_versions=(1, 2),
        supported_observables=observables,
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=minimum_hz,
            maximum_hz=maximum_hz,
        ),
    )


def test_geometric_adapter_dispatch_is_ready_only_for_exact_supported_contract(
    tmp_path: Path,
) -> None:
    snapshot = _fixture(tmp_path)['snapshot']
    request = build_acoustic_prediction_request(
        snapshot=snapshot,
        model_solver_role_id='future-r150-geometric-role',
        requested_frequency_domain=snapshot.requested_frequency_domain,
        requested_observables=('deterministic_paths',),
        numerical_fidelity_policy_ref=_policy(),
    )
    adapter = _adapter_descriptor(
        role='future-r150-geometric-role',
        domain='geometric',
        observables=('deterministic_paths',),
    )

    binding = bind_prediction_request_to_solver_adapter(
        snapshot=snapshot,
        request=request,
        adapter=adapter,
        solver_configuration_ref=_ref(
            'fixture-geometric-config',
            '3',
        ),
        numerical_fidelity_policy=_fidelity_policy(),
    )

    assert binding.state == 'READY'
    assert binding.reasons == ()
    assert binding.acoustic_scene_snapshot_id == snapshot.snapshot_id
    assert binding.prediction_request_id == request.request_id
    assert binding.solver_implementation_ref == adapter.solver_implementation_ref
    assert (
        binding.numerical_fidelity_policy_ref
        == request.numerical_fidelity_policy_ref
    )
    assert len(binding.deterministic_solver_input_hash) == 64


def test_wave_adapter_dispatch_preserves_current_wave_excitation_block(
    tmp_path: Path,
) -> None:
    snapshot = _fixture(tmp_path)['snapshot']
    request = build_acoustic_prediction_request(
        snapshot=snapshot,
        model_solver_role_id='future-r130-wave-role',
        requested_frequency_domain=snapshot.requested_frequency_domain,
        requested_observables=('complex_pressure',),
        numerical_fidelity_policy_ref=_policy(),
    )
    adapter = _adapter_descriptor(
        role='future-r130-wave-role',
        domain='wave',
        observables=('complex_pressure',),
    )

    binding = bind_prediction_request_to_solver_adapter(
        snapshot=snapshot,
        request=request,
        adapter=adapter,
        solver_configuration_ref=_ref('fixture-wave-config', '4'),
        numerical_fidelity_policy=_fidelity_policy(
            domain='wave',
            roles=('future-r130-wave-role',),
            observables=('complex_pressure',),
        ),
    )

    assert binding.state == 'BLOCKED'
    assert 'snapshot_wave_source_not_ready' in binding.reasons
    assert 'snapshot_observable_blocked:complex_pressure' in binding.reasons


def test_adapter_role_observable_and_frequency_capabilities_fail_closed(
    tmp_path: Path,
) -> None:
    snapshot = _fixture(tmp_path)['snapshot']
    request = build_acoustic_prediction_request(
        snapshot=snapshot,
        model_solver_role_id='future-r150-geometric-role',
        requested_frequency_domain=snapshot.requested_frequency_domain,
        requested_observables=('deterministic_paths',),
        numerical_fidelity_policy_ref=_policy(),
    )
    adapter = _adapter_descriptor(
        role='other-role',
        domain='geometric',
        observables=('magnitude_response',),
        minimum_hz=600.0,
        maximum_hz=900.0,
    )

    binding = bind_prediction_request_to_solver_adapter(
        snapshot=snapshot,
        request=request,
        adapter=adapter,
        solver_configuration_ref=_ref(
            'fixture-incompatible-config',
            '5',
        ),
        numerical_fidelity_policy=_fidelity_policy(),
    )

    assert binding.state == 'UNSUPPORTED'
    assert 'model_solver_role_not_supported_by_adapter' in binding.reasons
    assert (
        'observable_not_supported_by_adapter:deterministic_paths'
        in binding.reasons
    )
    assert 'frequency_domain_not_supported_by_adapter' in binding.reasons


def test_solver_build_or_configuration_changes_dispatch_identity(
    tmp_path: Path,
) -> None:
    snapshot = _fixture(tmp_path)['snapshot']
    request = build_acoustic_prediction_request(
        snapshot=snapshot,
        model_solver_role_id='future-r150-geometric-role',
        requested_frequency_domain=snapshot.requested_frequency_domain,
        requested_observables=('deterministic_paths',),
        numerical_fidelity_policy_ref=_policy(),
    )
    adapter_a = _adapter_descriptor(
        role='future-r150-geometric-role',
        domain='geometric',
        observables=('deterministic_paths',),
        solver_hash_char='6',
    )
    adapter_b = _adapter_descriptor(
        role='future-r150-geometric-role',
        domain='geometric',
        observables=('deterministic_paths',),
        solver_hash_char='7',
    )

    first = bind_prediction_request_to_solver_adapter(
        snapshot=snapshot,
        request=request,
        adapter=adapter_a,
        solver_configuration_ref=_ref('fixture-geometric-config', '8'),
        numerical_fidelity_policy=_fidelity_policy(),
    )
    changed_solver = bind_prediction_request_to_solver_adapter(
        snapshot=snapshot,
        request=request,
        adapter=adapter_b,
        solver_configuration_ref=_ref('fixture-geometric-config', '8'),
        numerical_fidelity_policy=_fidelity_policy(),
    )
    changed_config = bind_prediction_request_to_solver_adapter(
        snapshot=snapshot,
        request=request,
        adapter=adapter_a,
        solver_configuration_ref=_ref('fixture-geometric-config', '9'),
        numerical_fidelity_policy=_fidelity_policy(),
    )

    assert first.state == 'READY'
    assert changed_solver.state == 'READY'
    assert changed_config.state == 'READY'
    assert (
        first.deterministic_solver_input_hash
        != changed_solver.deterministic_solver_input_hash
    )
    assert (
        first.deterministic_solver_input_hash
        != changed_config.deterministic_solver_input_hash
    )
    assert first.binding_id != changed_solver.binding_id
    assert first.binding_id != changed_config.binding_id


from htdt.cad_acoustic_solver_dispatch_repository import (
    CadAcousticSolverDispatchRepository,
)


def _exact_ref_registry(*refs: ExactExternalAuthorityRef):
    registry = {
        (
            ref.authority_id,
            ref.authority_version,
            ref.semantic_hash_sha256,
        ): ref
        for ref in refs
    }

    def resolve(ref: ExactExternalAuthorityRef):
        return registry.get(
            (
                ref.authority_id,
                ref.authority_version,
                ref.semantic_hash_sha256,
            )
        )

    return registry, resolve


def _persisted_geometric_dispatch(tmp_path: Path):
    fx = _fixture(tmp_path)
    policy = _fidelity_policy()
    fidelity_registry, fidelity_resolver = _fidelity_resolver(policy)
    snapshot_repository = CadAcousticSnapshotRepository(
        fx['scene_repository'],
        variant_repository=fx['variant_repository'],
        r110_repository=fx['r110_repository'],
        r120_repository=fx['r120_repository'],
        fidelity_policy_resolver=fidelity_resolver,
        authority_resolvers=_snapshot_authority_resolvers(fx['snapshot']),
    )
    snapshot_repository.save_snapshot(fx['snapshot'])
    request = build_acoustic_prediction_request(
        snapshot=fx['snapshot'],
        model_solver_role_id='future-r150-geometric-role',
        requested_frequency_domain=fx['snapshot'].requested_frequency_domain,
        requested_observables=('deterministic_paths',),
        numerical_fidelity_policy_ref=_policy(),
    )
    snapshot_repository.save_prediction_request(request)

    adapter = _adapter_descriptor(
        role='future-r150-geometric-role',
        domain='geometric',
        observables=('deterministic_paths',),
    )
    configuration = _ref('fixture-geometric-config', '3')
    registry, resolver = _exact_ref_registry(
        adapter.solver_implementation_ref,
        adapter.solver_configuration_schema_ref,
        configuration,
    )
    repository = CadAcousticSolverDispatchRepository(
        fx['scene_repository'],
        snapshot_repository=snapshot_repository,
        external_authority_resolver=resolver,
        fidelity_policy_resolver=fidelity_resolver,
    )
    repository.save_descriptor(adapter)
    binding = bind_prediction_request_to_solver_adapter(
        snapshot=fx['snapshot'],
        request=request,
        adapter=adapter,
        solver_configuration_ref=configuration,
        numerical_fidelity_policy=policy,
    )
    repository.save_dispatch(binding)
    return {
        'fixture': fx,
        'registry': registry,
        'resolver': resolver,
        'fidelity_registry': fidelity_registry,
        'fidelity_resolver': fidelity_resolver,
        'adapter': adapter,
        'configuration': configuration,
        'binding': binding,
        'request': request,
        'policy': policy,
    }


def test_solver_dispatch_repository_save_reopen_recomputes_exact_authorities(
    tmp_path: Path,
) -> None:
    persisted = _persisted_geometric_dispatch(tmp_path)
    fx = persisted['fixture']

    reopened_scene = SceneRepository(fx['scene_repository'].path)
    reopened = CadAcousticSolverDispatchRepository(
        reopened_scene,
        external_authority_resolver=persisted['resolver'],
        fidelity_policy_resolver=persisted['fidelity_resolver'],
        snapshot_authority_resolvers=_snapshot_authority_resolvers(
            fx['snapshot']
        ),
    )

    adapter = persisted['adapter']
    binding = persisted['binding']
    assert reopened.get_descriptor(adapter.descriptor_id) == adapter
    assert reopened.get_dispatch(binding.binding_id) == binding
    assert binding.state == 'READY'
    assert binding.solver_configuration_ref == persisted['configuration']
    assert (
        binding.numerical_fidelity_policy_ref
        == persisted['request'].numerical_fidelity_policy_ref
    )


def test_solver_adapter_descriptor_requires_resolvable_external_authorities(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    adapter = _adapter_descriptor(
        role='future-r150-geometric-role',
        domain='geometric',
        observables=('deterministic_paths',),
    )
    _registry, resolver = _exact_ref_registry(
        adapter.solver_configuration_schema_ref,
    )
    repository = CadAcousticSolverDispatchRepository(
        fx['scene_repository'],
        external_authority_resolver=resolver,
        fidelity_policy_resolver=_fidelity_resolver()[1],
    )

    with pytest.raises(
        ValueError,
        match='solver implementation exact external authority does not exist',
    ):
        repository.save_descriptor(adapter)


def test_solver_dispatch_reopen_fails_closed_when_configuration_authority_stales(
    tmp_path: Path,
) -> None:
    persisted = _persisted_geometric_dispatch(tmp_path)
    configuration = persisted['configuration']
    persisted['registry'].pop(
        (
            configuration.authority_id,
            configuration.authority_version,
            configuration.semantic_hash_sha256,
        )
    )

    reopened = CadAcousticSolverDispatchRepository(
        SceneRepository(persisted['fixture']['scene_repository'].path),
        external_authority_resolver=persisted['resolver'],
        fidelity_policy_resolver=persisted['fidelity_resolver'],
        snapshot_authority_resolvers=_snapshot_authority_resolvers(
            persisted['fixture']['snapshot']
        ),
    )
    with pytest.raises(
        ValueError,
        match='solver configuration exact external authority does not exist',
    ):
        reopened.get_dispatch(persisted['binding'].binding_id)


def test_prediction_request_persistence_requires_fidelity_policy_resolver(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    resolverless = CadAcousticSnapshotRepository(
        fx['scene_repository'],
        variant_repository=fx['variant_repository'],
        r110_repository=fx['r110_repository'],
        r120_repository=fx['r120_repository'],
        authority_resolvers=_snapshot_authority_resolvers(fx['snapshot']),
    )
    resolverless.save_snapshot(fx['snapshot'])
    request = build_acoustic_prediction_request(
        snapshot=fx['snapshot'],
        model_solver_role_id='future-r150-geometric-role',
        requested_frequency_domain=fx['snapshot'].requested_frequency_domain,
        requested_observables=('deterministic_paths',),
        numerical_fidelity_policy_ref=_policy(),
    )

    with pytest.raises(
        ValueError,
        match='requires a numerical fidelity policy resolver',
    ):
        resolverless.save_prediction_request(request)

    _registry, fidelity_resolver = _fidelity_resolver(_fidelity_policy())
    resolved = CadAcousticSnapshotRepository(
        fx['scene_repository'],
        variant_repository=fx['variant_repository'],
        r110_repository=fx['r110_repository'],
        r120_repository=fx['r120_repository'],
        fidelity_policy_resolver=fidelity_resolver,
        authority_resolvers=_snapshot_authority_resolvers(fx['snapshot']),
    )
    resolved.save_prediction_request(request)

    with pytest.raises(
        ValueError,
        match='requires a numerical fidelity policy resolver',
    ):
        resolverless.get_prediction_request(request.request_id)


def test_prediction_request_rejects_unresolvable_fidelity_policy(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    _registry, fidelity_resolver = _fidelity_resolver()
    repository = CadAcousticSnapshotRepository(
        fx['scene_repository'],
        variant_repository=fx['variant_repository'],
        r110_repository=fx['r110_repository'],
        r120_repository=fx['r120_repository'],
        fidelity_policy_resolver=fidelity_resolver,
        authority_resolvers=_snapshot_authority_resolvers(fx['snapshot']),
    )
    repository.save_snapshot(fx['snapshot'])
    request = build_acoustic_prediction_request(
        snapshot=fx['snapshot'],
        model_solver_role_id='future-r150-geometric-role',
        requested_frequency_domain=fx['snapshot'].requested_frequency_domain,
        requested_observables=('deterministic_paths',),
        numerical_fidelity_policy_ref=_policy(),
    )

    with pytest.raises(
        ValueError,
        match='numerical fidelity policy exact external authority '
        'does not exist',
    ):
        repository.save_prediction_request(request)


def test_prediction_request_rejects_mismatched_fidelity_authority(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    mismatched = _fidelity_policy(
        ref=_ref('fixture-numerical-fidelity-policy', 'e')
    )
    repository = CadAcousticSnapshotRepository(
        fx['scene_repository'],
        variant_repository=fx['variant_repository'],
        r110_repository=fx['r110_repository'],
        r120_repository=fx['r120_repository'],
        fidelity_policy_resolver=lambda ref: mismatched,
        authority_resolvers=_snapshot_authority_resolvers(fx['snapshot']),
    )
    repository.save_snapshot(fx['snapshot'])
    request = build_acoustic_prediction_request(
        snapshot=fx['snapshot'],
        model_solver_role_id='future-r150-geometric-role',
        requested_frequency_domain=fx['snapshot'].requested_frequency_domain,
        requested_observables=('deterministic_paths',),
        numerical_fidelity_policy_ref=_policy(),
    )

    with pytest.raises(
        ValueError,
        match='numerical fidelity policy exact external authority mismatch',
    ):
        repository.save_prediction_request(request)


def test_prediction_request_rejects_inapplicable_fidelity_policy(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    repository = CadAcousticSnapshotRepository(
        fx['scene_repository'],
        variant_repository=fx['variant_repository'],
        r110_repository=fx['r110_repository'],
        r120_repository=fx['r120_repository'],
        fidelity_policy_resolver=_fidelity_resolver(
            _fidelity_policy(
                roles=('other-role',),
                observables=('magnitude_response',),
                minimum_hz=600.0,
                maximum_hz=900.0,
            )
        )[1],
        authority_resolvers=_snapshot_authority_resolvers(fx['snapshot']),
    )
    repository.save_snapshot(fx['snapshot'])
    request = build_acoustic_prediction_request(
        snapshot=fx['snapshot'],
        model_solver_role_id='future-r150-geometric-role',
        requested_frequency_domain=fx['snapshot'].requested_frequency_domain,
        requested_observables=('deterministic_paths',),
        numerical_fidelity_policy_ref=_policy(),
    )

    with pytest.raises(
        ValueError,
        match='numerical fidelity policy is not applicable',
    ) as error:
        repository.save_prediction_request(request)
    message = str(error.value)
    assert 'numerical_fidelity_policy_role_not_applicable' in message
    assert (
        'numerical_fidelity_policy_observable_not_supported:'
        'deterministic_paths'
    ) in message
    assert (
        'numerical_fidelity_policy_frequency_domain_not_supported' in message
    )


def test_prediction_request_reopen_detects_disappeared_fidelity_authority(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    fidelity_registry, fidelity_resolver = _fidelity_resolver(
        _fidelity_policy()
    )
    repository = CadAcousticSnapshotRepository(
        fx['scene_repository'],
        variant_repository=fx['variant_repository'],
        r110_repository=fx['r110_repository'],
        r120_repository=fx['r120_repository'],
        fidelity_policy_resolver=fidelity_resolver,
        authority_resolvers=_snapshot_authority_resolvers(fx['snapshot']),
    )
    repository.save_snapshot(fx['snapshot'])
    request = build_acoustic_prediction_request(
        snapshot=fx['snapshot'],
        model_solver_role_id='future-r150-geometric-role',
        requested_frequency_domain=fx['snapshot'].requested_frequency_domain,
        requested_observables=('deterministic_paths',),
        numerical_fidelity_policy_ref=_policy(),
    )
    repository.save_prediction_request(request)

    fidelity_registry.pop(request.numerical_fidelity_policy_ref)
    reopened = CadAcousticSnapshotRepository(
        SceneRepository(fx['scene_repository'].path),
        fidelity_policy_resolver=fidelity_resolver,
        authority_resolvers=_snapshot_authority_resolvers(fx['snapshot']),
    )
    with pytest.raises(
        ValueError,
        match='numerical fidelity policy exact external authority '
        'does not exist',
    ):
        reopened.get_prediction_request(request.request_id)


def test_solver_dispatch_rejects_unresolvable_fidelity_policy(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    policy = _fidelity_policy()
    _registry, snapshot_fidelity_resolver = _fidelity_resolver(policy)
    snapshot_repository = CadAcousticSnapshotRepository(
        fx['scene_repository'],
        variant_repository=fx['variant_repository'],
        r110_repository=fx['r110_repository'],
        r120_repository=fx['r120_repository'],
        fidelity_policy_resolver=snapshot_fidelity_resolver,
        authority_resolvers=_snapshot_authority_resolvers(fx['snapshot']),
    )
    snapshot_repository.save_snapshot(fx['snapshot'])
    request = build_acoustic_prediction_request(
        snapshot=fx['snapshot'],
        model_solver_role_id='future-r150-geometric-role',
        requested_frequency_domain=fx['snapshot'].requested_frequency_domain,
        requested_observables=('deterministic_paths',),
        numerical_fidelity_policy_ref=_policy(),
    )
    snapshot_repository.save_prediction_request(request)

    adapter = _adapter_descriptor(
        role='future-r150-geometric-role',
        domain='geometric',
        observables=('deterministic_paths',),
    )
    configuration = _ref('fixture-geometric-config', '3')
    _registry, resolver = _exact_ref_registry(
        adapter.solver_implementation_ref,
        adapter.solver_configuration_schema_ref,
        configuration,
    )
    dispatch_repository = CadAcousticSolverDispatchRepository(
        fx['scene_repository'],
        snapshot_repository=snapshot_repository,
        external_authority_resolver=resolver,
        fidelity_policy_resolver=_fidelity_resolver()[1],
    )
    dispatch_repository.save_descriptor(adapter)
    binding = bind_prediction_request_to_solver_adapter(
        snapshot=fx['snapshot'],
        request=request,
        adapter=adapter,
        solver_configuration_ref=configuration,
        numerical_fidelity_policy=policy,
    )

    with pytest.raises(
        ValueError,
        match='numerical fidelity policy exact external authority '
        'does not exist',
    ):
        dispatch_repository.save_dispatch(binding)


def test_solver_dispatch_reopen_detects_disappeared_fidelity_authority(
    tmp_path: Path,
) -> None:
    persisted = _persisted_geometric_dispatch(tmp_path)
    persisted['fidelity_registry'].pop(
        persisted['request'].numerical_fidelity_policy_ref
    )

    reopened = CadAcousticSolverDispatchRepository(
        SceneRepository(persisted['fixture']['scene_repository'].path),
        external_authority_resolver=persisted['resolver'],
        fidelity_policy_resolver=persisted['fidelity_resolver'],
        snapshot_authority_resolvers=_snapshot_authority_resolvers(
            persisted['fixture']['snapshot']
        ),
    )
    with pytest.raises(
        ValueError,
        match='numerical fidelity policy exact external authority '
        'does not exist',
    ):
        reopened.get_dispatch(persisted['binding'].binding_id)


def test_solver_dispatch_requires_snapshot_repository_fidelity_resolution(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    adapter = _adapter_descriptor(
        role='future-r150-geometric-role',
        domain='geometric',
        observables=('deterministic_paths',),
    )
    _registry, resolver = _exact_ref_registry(
        adapter.solver_implementation_ref,
        adapter.solver_configuration_schema_ref,
    )

    with pytest.raises(
        ValueError,
        match='requires a snapshot repository with numerical fidelity '
        'policy resolution',
    ):
        CadAcousticSolverDispatchRepository(
            fx['scene_repository'],
            snapshot_repository=CadAcousticSnapshotRepository(
                fx['scene_repository']
            ),
            external_authority_resolver=resolver,
            fidelity_policy_resolver=_fidelity_resolver()[1],
        )


def test_bind_rejects_fidelity_policy_authority_mismatch(
    tmp_path: Path,
) -> None:
    snapshot = _fixture(tmp_path)['snapshot']
    request = build_acoustic_prediction_request(
        snapshot=snapshot,
        model_solver_role_id='future-r150-geometric-role',
        requested_frequency_domain=snapshot.requested_frequency_domain,
        requested_observables=('deterministic_paths',),
        numerical_fidelity_policy_ref=_policy(),
    )
    adapter = _adapter_descriptor(
        role='future-r150-geometric-role',
        domain='geometric',
        observables=('deterministic_paths',),
    )
    mismatched = _fidelity_policy(
        ref=_ref('fixture-numerical-fidelity-policy', 'e')
    )

    with pytest.raises(
        ValueError,
        match='does not resolve the exact AcousticPredictionRequest authority',
    ):
        bind_prediction_request_to_solver_adapter(
            snapshot=snapshot,
            request=request,
            adapter=adapter,
            solver_configuration_ref=_ref('fixture-geometric-config', '3'),
            numerical_fidelity_policy=mismatched,
        )


def test_fidelity_policy_incompatibility_keeps_dispatch_unsupported(
    tmp_path: Path,
) -> None:
    snapshot = _fixture(tmp_path)['snapshot']
    request = build_acoustic_prediction_request(
        snapshot=snapshot,
        model_solver_role_id='future-r150-geometric-role',
        requested_frequency_domain=snapshot.requested_frequency_domain,
        requested_observables=('deterministic_paths',),
        numerical_fidelity_policy_ref=_policy(),
    )
    adapter = _adapter_descriptor(
        role='future-r150-geometric-role',
        domain='geometric',
        observables=('deterministic_paths',),
    )

    wrong_domain = bind_prediction_request_to_solver_adapter(
        snapshot=snapshot,
        request=request,
        adapter=adapter,
        solver_configuration_ref=_ref('fixture-geometric-config', '3'),
        numerical_fidelity_policy=_fidelity_policy(domain='wave'),
    )
    assert wrong_domain.state == 'UNSUPPORTED'
    assert (
        'numerical_fidelity_policy_domain_not_supported_by_adapter'
        in wrong_domain.reasons
    )

    wrong_role = bind_prediction_request_to_solver_adapter(
        snapshot=snapshot,
        request=request,
        adapter=adapter,
        solver_configuration_ref=_ref('fixture-geometric-config', '3'),
        numerical_fidelity_policy=_fidelity_policy(
            roles=('other-role',),
            observables=('magnitude_response',),
            minimum_hz=600.0,
            maximum_hz=900.0,
        ),
    )
    assert wrong_role.state == 'UNSUPPORTED'
    assert (
        'numerical_fidelity_policy_role_not_applicable' in wrong_role.reasons
    )
    assert (
        'numerical_fidelity_policy_observable_not_supported:'
        'deterministic_paths'
    ) in wrong_role.reasons
    assert (
        'numerical_fidelity_policy_frequency_domain_not_supported'
        in wrong_role.reasons
    )


def test_ready_dispatch_solver_input_proves_resolved_fidelity_authority(
    tmp_path: Path,
) -> None:
    snapshot = _fixture(tmp_path)['snapshot']
    request = build_acoustic_prediction_request(
        snapshot=snapshot,
        model_solver_role_id='future-r150-geometric-role',
        requested_frequency_domain=snapshot.requested_frequency_domain,
        requested_observables=('deterministic_paths',),
        numerical_fidelity_policy_ref=_policy(),
    )
    adapter = _adapter_descriptor(
        role='future-r150-geometric-role',
        domain='geometric',
        observables=('deterministic_paths',),
    )
    configuration = _ref('fixture-geometric-config', '3')

    binding = bind_prediction_request_to_solver_adapter(
        snapshot=snapshot,
        request=request,
        adapter=adapter,
        solver_configuration_ref=configuration,
        numerical_fidelity_policy=_fidelity_policy(),
    )

    assert binding.state == 'READY'
    solver_input = binding.solver_input_payload()
    assert solver_input['numerical_fidelity_policy_ref'] == (
        request.numerical_fidelity_policy_ref.model_dump(mode='json')
    )
    assert (
        binding.numerical_fidelity_policy_ref
        == request.numerical_fidelity_policy_ref
    )

    other_request = build_acoustic_prediction_request(
        snapshot=snapshot,
        model_solver_role_id='future-r150-geometric-role',
        requested_frequency_domain=snapshot.requested_frequency_domain,
        requested_observables=('deterministic_paths',),
        numerical_fidelity_policy_ref=_ref(
            'fixture-numerical-fidelity-policy',
            'e',
        ),
    )
    other_policy = _fidelity_policy(
        ref=other_request.numerical_fidelity_policy_ref
    )
    other_binding = bind_prediction_request_to_solver_adapter(
        snapshot=snapshot,
        request=other_request,
        adapter=adapter,
        solver_configuration_ref=configuration,
        numerical_fidelity_policy=other_policy,
    )
    assert (
        other_binding.deterministic_solver_input_hash
        != binding.deterministic_solver_input_hash
    )
    assert other_binding.binding_id != binding.binding_id



def _snapshot_repository(fx, resolvers) -> CadAcousticSnapshotRepository:
    return CadAcousticSnapshotRepository(
        fx['scene_repository'],
        variant_repository=fx['variant_repository'],
        r110_repository=fx['r110_repository'],
        r120_repository=fx['r120_repository'],
        authority_resolvers=resolvers,
    )


def test_snapshot_save_requires_typed_external_authority_resolvers(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    repository = CadAcousticSnapshotRepository(
        fx['scene_repository'],
        variant_repository=fx['variant_repository'],
        r110_repository=fx['r110_repository'],
        r120_repository=fx['r120_repository'],
    )
    with pytest.raises(
        ValueError,
        match='environment authority requires a typed environment resolver',
    ):
        repository.save_snapshot(fx['snapshot'])


def test_snapshot_read_rejects_dangling_and_stale_environment_authorities(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    snapshot = fx['snapshot']
    resolvers = _snapshot_authority_resolvers(snapshot)
    _snapshot_repository(fx, resolvers).save_snapshot(snapshot)

    environment = snapshot.environment
    assert environment is not None

    reopened_missing = CadAcousticSnapshotRepository(
        SceneRepository(fx['scene_repository'].path),
        authority_resolvers=resolvers._replace(
            environment=lambda ref: None
        ),
    )
    with pytest.raises(
        ValueError,
        match='environment exact external authority does not exist',
    ):
        reopened_missing.get_snapshot(snapshot.snapshot_id)

    stale_environment = SnapshotEnvironmentAuthorityRef(
        authority=environment.authority,
        sound_speed_m_s=999.0,
        sound_speed_source_authority=environment.sound_speed_source_authority,
    )
    reopened_stale = CadAcousticSnapshotRepository(
        SceneRepository(fx['scene_repository'].path),
        authority_resolvers=resolvers._replace(
            environment=lambda ref: stale_environment
        ),
    )
    with pytest.raises(
        ValueError,
        match='environment exact external authority mismatch',
    ):
        reopened_stale.get_snapshot(snapshot.snapshot_id)


def test_snapshot_read_rejects_missing_and_inconsistent_sound_speed_source(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    snapshot = fx['snapshot']
    resolvers = _snapshot_authority_resolvers(snapshot)
    _snapshot_repository(fx, resolvers).save_snapshot(snapshot)
    environment = snapshot.environment
    assert environment is not None

    missing = CadAcousticSnapshotRepository(
        SceneRepository(fx['scene_repository'].path),
        authority_resolvers=resolvers._replace(
            sound_speed_source=lambda ref: None
        ),
    )
    with pytest.raises(
        ValueError,
        match='sound speed source exact external authority does not exist',
    ):
        missing.get_snapshot(snapshot.snapshot_id)

    inconsistent = CadAcousticSnapshotRepository(
        SceneRepository(fx['scene_repository'].path),
        authority_resolvers=resolvers._replace(
            sound_speed_source=lambda ref: environment.sound_speed_m_s + 1.0
        ),
    )
    with pytest.raises(
        ValueError,
        match='sound speed value does not reproduce from exact source authority',
    ):
        inconsistent.get_snapshot(snapshot.snapshot_id)


def test_snapshot_read_reresolves_valid_frequency_domain_authority(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    snapshot = fx['snapshot']
    resolvers = _snapshot_authority_resolvers(snapshot)
    _snapshot_repository(fx, resolvers).save_snapshot(snapshot)

    missing = CadAcousticSnapshotRepository(
        SceneRepository(fx['scene_repository'].path),
        authority_resolvers=resolvers._replace(
            valid_frequency_domain=lambda ref: None
        ),
    )
    with pytest.raises(
        ValueError,
        match='valid frequency domain exact external authority does not exist',
    ):
        missing.get_snapshot(snapshot.snapshot_id)

    mismatched = CadAcousticSnapshotRepository(
        SceneRepository(fx['scene_repository'].path),
        authority_resolvers=resolvers._replace(
            valid_frequency_domain=lambda ref: FrequencyDomain(
                minimum_hz=600.0,
                maximum_hz=900.0,
            )
        ),
    )
    with pytest.raises(
        ValueError,
        match='valid frequency domain does not reproduce from exact authority',
    ):
        mismatched.get_snapshot(snapshot.snapshot_id)


def test_snapshot_read_rejects_dangling_and_mismatched_material_authority(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    snapshot = fx['snapshot']
    resolvers = _snapshot_authority_resolvers(snapshot)
    _snapshot_repository(fx, resolvers).save_snapshot(snapshot)

    material_ref = snapshot.surface_boundary_configuration[0].material_authority
    assert material_ref is not None
    external = resolvers.external_authority
    assert external is not None

    dangling = resolvers._replace(
        external_authority=lambda ref: (
            None if ref == material_ref else external(ref)
        )
    )
    with pytest.raises(
        ValueError,
        match='surface material exact external authority does not exist',
    ):
        CadAcousticSnapshotRepository(
            SceneRepository(fx['scene_repository'].path),
            authority_resolvers=dangling,
        ).get_snapshot(snapshot.snapshot_id)

    mismatched = resolvers._replace(
        external_authority=lambda ref: (
            _ref('attacker-material', '7')
            if ref == material_ref
            else external(ref)
        )
    )
    with pytest.raises(
        ValueError,
        match='surface material exact external authority mismatch',
    ):
        CadAcousticSnapshotRepository(
            SceneRepository(fx['scene_repository'].path),
            authority_resolvers=mismatched,
        ).get_snapshot(snapshot.snapshot_id)


def test_snapshot_read_rejects_dangling_boundary_physics_authority(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    snapshot = fx['snapshot']
    resolvers = _snapshot_authority_resolvers(snapshot)
    _snapshot_repository(fx, resolvers).save_snapshot(snapshot)

    boundary_ref = snapshot.surface_boundary_configuration[0].boundary_physics_authority
    assert boundary_ref is not None
    external = resolvers.external_authority
    assert external is not None

    dangling = resolvers._replace(
        external_authority=lambda ref: (
            None if ref == boundary_ref else external(ref)
        )
    )
    with pytest.raises(
        ValueError,
        match='surface boundary physics exact external authority does not exist',
    ):
        CadAcousticSnapshotRepository(
            SceneRepository(fx['scene_repository'].path),
            authority_resolvers=dangling,
        ).get_snapshot(snapshot.snapshot_id)


def _measured_receiver_snapshot(fx):
    measurement_ref = _ref('fixture-receiver-measurement', '7')
    receiver = receiver_binding_from_scene(
        scene_revision=fx['revision'],
        system_variant=fx['variant'],
        entity_id='receiver-mlp',
        requested_output_capabilities=('complex_pressure',),
        measurement_authority_ref=measurement_ref,
    )
    snapshot = build_acoustic_scene_snapshot(
        scene_revision=fx['revision'],
        system_variant=fx['variant'],
        compiled_geometry=fx['compiled'],
        source_models=(fx['magnitude_source'], fx['complex_source']),
        receivers=(receiver,),
        requested_frequency_domain=fx['snapshot'].requested_frequency_domain,
        requested_observables=('complex_pressure',),
        environment=fx['snapshot'].environment,
        valid_frequency_domain=fx['snapshot'].valid_frequency_domain,
        valid_frequency_domain_authority_ref=(
            fx['snapshot'].valid_frequency_domain_authority_ref
        ),
    )
    return snapshot, receiver, measurement_ref


def test_explicit_receiver_measurement_authority_binds_exact_pose(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    snapshot, receiver, _measurement_ref = _measured_receiver_snapshot(fx)
    assert (
        receiver.acoustic_reference_semantics
        == 'explicit_measurement_authority'
    )
    resolvers = _snapshot_authority_resolvers(snapshot)
    _snapshot_repository(fx, resolvers).save_snapshot(snapshot)

    reopened = CadAcousticSnapshotRepository(
        SceneRepository(fx['scene_repository'].path),
        authority_resolvers=resolvers,
    )
    assert reopened.get_snapshot(snapshot.snapshot_id) == snapshot

    dangling = resolvers._replace(
        receiver_measurement=lambda ref: None
    )
    with pytest.raises(
        ValueError,
        match='receiver measurement exact external authority does not exist',
    ):
        CadAcousticSnapshotRepository(
            SceneRepository(fx['scene_repository'].path),
            authority_resolvers=dangling,
        ).get_snapshot(snapshot.snapshot_id)

    wrong_pose = resolvers._replace(
        receiver_measurement=lambda ref: ReceiverMeasurementAuthority(
            authority_ref=ref,
            entity_id=receiver.entity_id,
            world_position=Position3(x_m=9.0, y_m=9.0, z_m=9.0),
        )
    )
    with pytest.raises(
        ValueError,
        match='receiver measurement authority does not bind the exact '
        'receiver position/context',
    ):
        CadAcousticSnapshotRepository(
            SceneRepository(fx['scene_repository'].path),
            authority_resolvers=wrong_pose,
        ).get_snapshot(snapshot.snapshot_id)


def test_persisted_snapshot_payload_identity_is_verified_on_read(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    snapshot = fx['snapshot']
    other = build_acoustic_scene_snapshot(
        scene_revision=fx['revision'],
        system_variant=fx['variant'],
        compiled_geometry=fx['compiled'],
        source_models=(fx['magnitude_source'], fx['complex_source']),
        receivers=fx['receivers'],
        requested_frequency_domain=snapshot.requested_frequency_domain,
        requested_observables=snapshot.requested_observables,
        environment=_environment('7'),
        valid_frequency_domain=snapshot.valid_frequency_domain,
        valid_frequency_domain_authority_ref=(
            snapshot.valid_frequency_domain_authority_ref
        ),
    )
    assert other.snapshot_id != snapshot.snapshot_id

    repository = _snapshot_repository(
        fx,
        _snapshot_authority_resolvers(snapshot),
    )
    repository.save_snapshot(snapshot)

    connection = sqlite3.connect(fx['scene_repository'].path)
    try:
        connection.execute(
            'UPDATE cad_acoustic_scene_snapshots SET payload_json=? '
            'WHERE snapshot_id=?',
            (other.model_dump_json(), snapshot.snapshot_id),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        ValueError,
        match='persisted AcousticSceneSnapshot payload identity mismatch',
    ):
        repository.get_snapshot(snapshot.snapshot_id)


def test_snapshot_read_repeats_save_side_authority_resolution(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    snapshot = fx['snapshot']
    resolvers = _snapshot_authority_resolvers(snapshot)
    repository = _snapshot_repository(fx, resolvers)
    repository.save_snapshot(snapshot)
    assert repository.get_snapshot(snapshot.snapshot_id) == snapshot

    reopened = CadAcousticSnapshotRepository(
        SceneRepository(fx['scene_repository'].path),
        authority_resolvers=AcousticSnapshotAuthorityResolvers(),
    )
    with pytest.raises(
        ValueError,
        match='requires a typed',
    ):
        reopened.get_snapshot(snapshot.snapshot_id)


def _forged_snapshot(snapshot, **updates) -> AcousticSceneSnapshot:
    """Self-consistent forgery: claimed semantics with a recomputed identity.

    The returned snapshot passes ``exact_snapshot_identity`` (semantic hash
    and id are recomputed over the tampered payload), so any rejection is
    attributable to canonical replay, not to a dangling hash.
    """
    forged = snapshot.model_copy(update=updates)
    digest = _digest(forged.semantic_payload())
    return AcousticSceneSnapshot.model_validate(
        {
            **forged.model_dump(mode='python'),
            'semantic_sha256': digest,
            'snapshot_id': f'acoustic-scene-snapshot:{digest}',
        }
    )


def _forged_ready_readiness(snapshot):
    return snapshot.readiness.model_copy(
        update={
            'wave_source_ready': True,
            'requested_observable_ready': True,
            'observable_readiness': tuple(
                item
                if item.state == 'READY'
                else item.model_copy(update={'state': 'READY', 'reasons': ()})
                for item in snapshot.readiness.observable_readiness
            ),
        }
    )


def test_canonical_builder_output_round_trips_unchanged(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    snapshot = fx['snapshot']
    resolvers = _snapshot_authority_resolvers(snapshot)
    repository = _snapshot_repository(fx, resolvers)

    assert repository.save_snapshot(snapshot) == snapshot

    reopened = CadAcousticSnapshotRepository(
        SceneRepository(fx['scene_repository'].path),
        authority_resolvers=resolvers,
    )
    assert reopened.get_snapshot(snapshot.snapshot_id) == snapshot
    assert reopened.get_snapshot_by_hash(snapshot.semantic_sha256) == snapshot


def test_forged_ready_claim_is_rejected_by_canonical_replay(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    snapshot = fx['snapshot']
    assert snapshot.readiness.wave_source_ready is False
    assert snapshot.readiness.requested_observable_ready is False
    repository = _snapshot_repository(
        fx,
        _snapshot_authority_resolvers(snapshot),
    )

    forged = _forged_snapshot(
        snapshot,
        readiness=_forged_ready_readiness(snapshot),
    )
    assert forged.snapshot_id != snapshot.snapshot_id
    assert forged.readiness.wave_source_ready is True
    assert all(
        item.state == 'READY'
        for item in forged.readiness.observable_readiness
    )

    with pytest.raises(
        ValueError,
        match='readiness does not reproduce from exact authorities',
    ):
        repository.save_snapshot(forged)


def test_forged_unsupported_observable_ready_claim_is_rejected(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    snapshot = build_acoustic_scene_snapshot(
        scene_revision=fx['revision'],
        system_variant=fx['variant'],
        compiled_geometry=fx['compiled'],
        source_models=(fx['magnitude_source'], fx['complex_source']),
        receivers=fx['receivers'],
        requested_frequency_domain=fx['snapshot'].requested_frequency_domain,
        requested_observables=('subjective_impression',),
        environment=fx['snapshot'].environment,
        valid_frequency_domain=fx['snapshot'].valid_frequency_domain,
        valid_frequency_domain_authority_ref=(
            fx['snapshot'].valid_frequency_domain_authority_ref
        ),
    )
    observable = snapshot.readiness.observable_readiness[0]
    assert observable.observable == 'subjective_impression'
    assert observable.state == 'UNSUPPORTED'

    forged = _forged_snapshot(
        snapshot,
        readiness=snapshot.readiness.model_copy(
            update={
                'requested_observable_ready': True,
                'observable_readiness': (
                    observable.model_copy(
                        update={'state': 'READY', 'reasons': ()}
                    ),
                ),
            }
        ),
    )
    repository = _snapshot_repository(
        fx,
        _snapshot_authority_resolvers(snapshot),
    )
    with pytest.raises(
        ValueError,
        match='readiness does not reproduce from exact authorities',
    ):
        repository.save_snapshot(forged)


def test_forged_unresolved_conditions_are_rejected_by_canonical_replay(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    snapshot = fx['snapshot']
    assert 'wave_source_excitation_blocked' in snapshot.unresolved_conditions

    forged = _forged_snapshot(snapshot, unresolved_conditions=())
    assert forged.unresolved_conditions == ()

    repository = _snapshot_repository(
        fx,
        _snapshot_authority_resolvers(snapshot),
    )
    with pytest.raises(
        ValueError,
        match='unresolved conditions do not reproduce from exact authorities',
    ):
        repository.save_snapshot(forged)


def test_forged_schema_version_claim_is_rejected_by_canonical_replay(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    snapshot = fx['snapshot']
    assert snapshot.schema_version == 1

    forged = _forged_snapshot(
        snapshot,
        schema_version=2,
        authority_version='2',
        compiler_version='2',
        readiness=snapshot.readiness.model_copy(
            update={'geometric_boundary_ready': True}
        ),
    )
    assert forged.schema_version == 2

    repository = _snapshot_repository(
        fx,
        _snapshot_authority_resolvers(snapshot),
    )
    with pytest.raises(
        ValueError,
        match='schema version does not reproduce from exact authorities',
    ):
        repository.save_snapshot(forged)


def _insert_persisted_snapshot_row(db_path, snapshot, *, like) -> None:
    """Insert a snapshot row directly, bypassing repository validation."""
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            INSERT INTO cad_acoustic_scene_snapshots(
                snapshot_id,
                semantic_sha256,
                document_id,
                scene_revision_id,
                scene_content_hash,
                system_variant_id,
                system_variant_sha256,
                r120_compiled_geometry_id,
                r120_compiled_geometry_sha256,
                material_boundary_configuration_sha256,
                environment_authority_sha256,
                payload_json,
                recorded_at_utc
            )
            SELECT ?, ?, document_id, scene_revision_id, scene_content_hash,
                system_variant_id, system_variant_sha256,
                r120_compiled_geometry_id, r120_compiled_geometry_sha256,
                material_boundary_configuration_sha256,
                environment_authority_sha256, ?, ?
            FROM cad_acoustic_scene_snapshots
            WHERE snapshot_id=?
            """,
            (
                snapshot.snapshot_id,
                snapshot.semantic_sha256,
                snapshot.model_dump_json(),
                NOW,
                like.snapshot_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_forged_ready_snapshot_fails_closed_on_read_and_dispatch(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    snapshot = fx['snapshot']
    resolvers = _snapshot_authority_resolvers(snapshot)
    repository = _snapshot_repository(fx, resolvers)
    repository.save_snapshot(snapshot)

    forged = _forged_snapshot(
        snapshot,
        readiness=_forged_ready_readiness(snapshot),
    )
    _insert_persisted_snapshot_row(
        fx['scene_repository'].path,
        forged,
        like=snapshot,
    )

    with pytest.raises(
        ValueError,
        match='readiness does not reproduce from exact authorities',
    ):
        repository.get_snapshot(forged.snapshot_id)
    with pytest.raises(
        ValueError,
        match='readiness does not reproduce from exact authorities',
    ):
        repository.get_snapshot_by_hash(forged.semantic_sha256)

    policy = _fidelity_policy(
        domain='wave',
        roles=('future-r130-wave-role',),
        observables=('complex_pressure',),
    )
    _fidelity_registry, fidelity_resolver = _fidelity_resolver(policy)
    snapshot_repository = CadAcousticSnapshotRepository(
        fx['scene_repository'],
        variant_repository=fx['variant_repository'],
        r110_repository=fx['r110_repository'],
        r120_repository=fx['r120_repository'],
        fidelity_policy_resolver=fidelity_resolver,
        authority_resolvers=resolvers,
    )
    request = build_acoustic_prediction_request(
        snapshot=forged,
        model_solver_role_id='future-r130-wave-role',
        requested_frequency_domain=forged.requested_frequency_domain,
        requested_observables=('complex_pressure',),
        numerical_fidelity_policy_ref=policy.authority_ref,
    )
    with pytest.raises(
        ValueError,
        match='readiness does not reproduce from exact authorities',
    ):
        snapshot_repository.save_prediction_request(request)

    adapter = _adapter_descriptor(
        role='future-r130-wave-role',
        domain='wave',
        observables=('complex_pressure',),
    )
    configuration = _ref('fixture-wave-config', '4')
    forged_binding = bind_prediction_request_to_solver_adapter(
        snapshot=forged,
        request=request,
        adapter=adapter,
        solver_configuration_ref=configuration,
        numerical_fidelity_policy=policy,
    )
    # The forgery itself claims full readiness; only canonical replay on the
    # read path stops it from reaching a READY solver dispatch.
    assert forged_binding.state == 'READY'

    _registry, external_resolver = _exact_ref_registry(
        adapter.solver_implementation_ref,
        adapter.solver_configuration_schema_ref,
        configuration,
    )
    dispatch_repository = CadAcousticSolverDispatchRepository(
        fx['scene_repository'],
        snapshot_repository=snapshot_repository,
        external_authority_resolver=external_resolver,
        fidelity_policy_resolver=fidelity_resolver,
    )
    dispatch_repository.save_descriptor(adapter)
    with pytest.raises(
        ValueError,
        match='readiness does not reproduce from exact authorities',
    ):
        dispatch_repository.save_dispatch(forged_binding)


def test_persisted_snapshot_index_columns_must_agree_with_payload(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    snapshot = fx['snapshot']
    repository = _snapshot_repository(
        fx,
        _snapshot_authority_resolvers(snapshot),
    )
    repository.save_snapshot(snapshot)

    connection = sqlite3.connect(fx['scene_repository'].path)
    try:
        connection.execute(
            'UPDATE cad_acoustic_scene_snapshots '
            'SET r120_compiled_geometry_sha256=? WHERE snapshot_id=?',
            ('0' * 64, snapshot.snapshot_id),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        ValueError,
        match='persisted AcousticSceneSnapshot payload identity mismatch',
    ):
        repository.get_snapshot(snapshot.snapshot_id)

    connection = sqlite3.connect(fx['scene_repository'].path)
    try:
        connection.execute(
            'UPDATE cad_acoustic_scene_snapshots '
            'SET r120_compiled_geometry_sha256=?, '
            'environment_authority_sha256=NULL WHERE snapshot_id=?',
            (
                snapshot.r120_compiled_geometry_sha256,
                snapshot.snapshot_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        ValueError,
        match='persisted AcousticSceneSnapshot payload identity mismatch',
    ):
        repository.get_snapshot_by_hash(snapshot.semantic_sha256)


def _screen_transfer(document_id: str = 'acoustic-snapshot-fixture'):
    return build_screen_transfer(
        screen_entity_id='screen-projection',
        label='Projection screen transfer',
        capability_tier='AT_CLAIM',
        provenance='fixture',
        document_id=document_id,
    )


def _observable_state(snapshot, observable: str):
    return next(
        item
        for item in snapshot.readiness.observable_readiness
        if item.observable == observable
    )


def test_screen_entity_blocks_field_observables_until_transfer_integrated(
    tmp_path: Path,
):
    fx = _fixture(tmp_path, include_screen=True)
    snapshot = fx['snapshot']

    magnitude = _observable_state(snapshot, 'magnitude_response')
    complex_pressure = _observable_state(snapshot, 'complex_pressure')
    paths = _observable_state(snapshot, 'deterministic_paths')
    assert magnitude.state == 'BLOCKED'
    assert complex_pressure.state == 'BLOCKED'
    assert 'wave:screen_transfer_not_integrated' in magnitude.reasons
    assert 'geometric:screen_transfer_not_integrated' in magnitude.reasons
    assert any(
        'screen_transfer_not_integrated' in reason
        for reason in complex_pressure.reasons
    )
    # The deterministic path set is pure R150 geometry: a screen does not
    # invalidate path existence, only the response through it (#940).
    assert paths.state == 'READY'
    assert 'screen_transfer_unbound:screen-projection' in (
        snapshot.unresolved_conditions
    )
    assert snapshot.screen_transfer_bindings == ()


def test_bound_screen_transfer_authority_is_recorded_but_stays_unmodelled(
    tmp_path: Path,
):
    authority = _screen_transfer()
    fx = _fixture(
        tmp_path,
        include_screen=True,
        screen_transfer_authorities=(authority,),
    )
    snapshot = fx['snapshot']

    assert len(snapshot.screen_transfer_bindings) == 1
    binding = snapshot.screen_transfer_bindings[0]
    assert binding.screen_entity_id == 'screen-projection'
    assert binding.screen_transfer_ref == authority.authority_ref()
    assert 'screen_transfer_not_integrated:screen-projection' in (
        snapshot.unresolved_conditions
    )
    magnitude = _observable_state(snapshot, 'magnitude_response')
    assert magnitude.state == 'BLOCKED'
    assert any(
        'screen_transfer_not_integrated' in reason
        for reason in magnitude.reasons
    )

    # The bound authority is part of snapshot identity: swapping it changes
    # the semantic hash rather than silently re-binding.
    other = build_screen_transfer(
        screen_entity_id='screen-projection',
        label='Different transfer evidence',
        capability_tier='AT_CLAIM',
        provenance='fixture',
        document_id='acoustic-snapshot-fixture',
    )
    other_dir = tmp_path / 'other'
    other_dir.mkdir()
    fx_other = _fixture(
        other_dir,
        include_screen=True,
        screen_transfer_authorities=(other,),
    )
    assert fx_other['snapshot'].semantic_sha256 != snapshot.semantic_sha256


def test_screen_transfer_authority_must_reference_a_screen_entity(
    tmp_path: Path,
):
    bad = build_screen_transfer(
        screen_entity_id='speaker-fl',
        label='Not a screen',
        capability_tier='AT_CLAIM',
        provenance='fixture',
        document_id='acoustic-snapshot-fixture',
    )
    with pytest.raises(
        ValueError,
        match='screen transfer authority references an entity',
    ):
        _fixture(
            tmp_path,
            include_screen=True,
            screen_transfer_authorities=(bad,),
        )


def test_screen_transfer_authority_document_binding_must_match(
    tmp_path: Path,
):
    wrong_document = _screen_transfer(document_id='other-document')
    with pytest.raises(
        ValueError,
        match='screen transfer authority document binding mismatch',
    ):
        _fixture(
            tmp_path,
            include_screen=True,
            screen_transfer_authorities=(wrong_document,),
        )
