from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_acoustic_snapshot import (
    SnapshotEnvironmentAuthorityRef,
    build_acoustic_prediction_request,
    build_acoustic_scene_snapshot,
    receiver_binding_from_scene,
)
from htdt.cad_acoustic_snapshot_repository import CadAcousticSnapshotRepository
from htdt.cad_acoustic_solver_adapter import (
    bind_prediction_request_to_solver_adapter,
    build_acoustic_solver_adapter_descriptor,
)
from htdt.cad_equipment import (
    DirectivityCapability,
    EquipmentDataProvenance,
    FrequencyDomain,
    InterpolationProvenance,
    build_equipment_definition,
)
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_r110_source import compile_r110_source_model
from htdt.cad_r110_source_repository import CadR110SourceRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Offset3,
    Position3,
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
from htdt.cad_wave_excitation import (
    CadWaveExcitationRepository,
    ComplexVolumeVelocitySample,
    bind_wave_excitation_to_r110_source,
    build_acoustic_wave_excitation_authority,
)
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

NOW = '2026-09-20T00:00:00+00:00'


def _ref(name: str, char: str) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=name,
        authority_version='fixture-v1',
        semantic_hash_sha256=char * 64,
    )


def _provenance(char: str = '1') -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='measured',
        source_name='explicit-volume-velocity-fixture',
        source_version='1',
        source_reference='fixture authority; not derived from sensitivity',
        source_sha256=char * 64,
    )


def _semantic_geometry():
    mesh = import_raw_visual_mesh(
        CLOSED_TETRA,
        source_name='wave-excitation-fixture.obj',
    )
    request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=None,
        source_to_scene_transform=explicit_identity_source_to_scene_transform(
            reason='fixture coordinates are explicit metres',
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


def _fixture(tmp_path: Path):
    db = tmp_path / 'cad.sqlite3'
    scene_repository = SceneRepository(db)
    document = SceneDocument(
        document_id='wave-excitation-fixture',
        schema_version=4,
        room=None,
        r120_semantic_geometry=_semantic_geometry(),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
            ),
            SceneEntity(
                entity_id='receiver-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=2.5, y_m=3.0, z_m=1.1),
            ),
        ),
    )
    revision = scene_repository.save(
        document,
        parent_revision_id=None,
    ).revision

    variant_repository = CadSystemVariantRepository(scene_repository)
    equipment_repository = CadEquipmentRepository(
        scene_repository,
        variant_repository,
    )
    provenance = _provenance()
    definition = build_equipment_definition(
        definition_id='explicit-wave-source-speaker',
        version='1',
        identity_kind='user_defined',
        user_label='Explicit wave source speaker',
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
        acoustic_reference_point_m=Offset3(x_m=0.0, y_m=0.1, z_m=0.0),
        directivity=DirectivityCapability(
            tier='unknown',
            data_format='unknown',
            provenance=provenance,
        ),
    )
    equipment_repository.save_definition(definition)

    variant = build_system_variant(
        baseline=revision,
        name='Wave excitation fixture',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front left'),
        ),
        proposed_entities=(),
        equipment_bindings=(
            EquipmentBindingRef(
                entity_id='speaker-fl',
                equipment_definition_id=definition.definition_id,
                equipment_definition_version=definition.version,
                equipment_definition_sha256=definition.semantic_sha256,
            ),
        ),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant)

    r110_repository = CadR110SourceRepository(
        scene_repository,
        variant_repository=variant_repository,
        equipment_repository=equipment_repository,
    )
    source = compile_r110_source_model(
        scene_revision=revision,
        system_variant=variant,
        source_entity_id='speaker-fl',
        equipment_definition=definition,
    )
    r110_repository.save_model(source)

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
    compiled = compile_r120_geometry(
        revision,
        make_r120_geometry_compilation_request(
            revision,
            geometric_tolerance_m=1.0e-6,
        ),
        surface_boundary_bindings=(
            SurfaceBoundaryAuthorityBinding(
                source_surface_id=surface_id,
                material_authority=_ref('fixture-material', 'a'),
                boundary_physics_authority=_ref(
                    'fixture-boundary-physics',
                    'b',
                ),
            ),
        ),
        region_authority=region,
        portal_authority=make_portal_authority(
            declaration_mode='explicit_none',
        ),
        boundary_termination_authority=make_boundary_termination_authority(
            declaration_mode='explicit_none',
        ),
    )
    r120_repository = R120GeometryCompilerRepository(scene_repository)
    r120_repository.save_compiled_geometry(compiled)

    excitation = build_acoustic_wave_excitation_authority(
        definition_id=definition.definition_id,
        definition_version=definition.version,
        definition_sha256=definition.semantic_sha256,
        samples=(
            ComplexVolumeVelocitySample(
                frequency_hz=100.0,
                real_m3_s=1.0e-4,
                imag_m3_s=0.0,
            ),
            ComplexVolumeVelocitySample(
                frequency_hz=200.0,
                real_m3_s=8.0e-5,
                imag_m3_s=-2.0e-5,
            ),
        ),
        interpolation=InterpolationProvenance(
            method='linear',
            implementation='wave-excitation-fixture-linear',
            implementation_version='1',
            provenance=provenance,
        ),
        provenance=(provenance,),
        approximation_note=(
            'Explicit measured equivalent-monopole volume velocity over the '
            'declared low-frequency band; no electrical sensitivity conversion.'
        ),
    )
    wave_repository = CadWaveExcitationRepository(
        scene_repository,
        equipment_repository=equipment_repository,
        r110_repository=r110_repository,
    )
    wave_repository.save_excitation(excitation)
    binding = bind_wave_excitation_to_r110_source(
        source=source,
        excitation=excitation,
    )
    wave_repository.save_binding(binding)

    receiver = receiver_binding_from_scene(
        scene_revision=revision,
        system_variant=variant,
        entity_id='receiver-mlp',
        requested_output_capabilities=('complex_pressure',),
    )
    environment = SnapshotEnvironmentAuthorityRef(
        authority=_ref('fixture-environment', 'c'),
        sound_speed_m_s=343.0,
        sound_speed_source_authority=_ref(
            'fixture-sound-speed',
            'd',
        ),
    )
    snapshot = build_acoustic_scene_snapshot(
        scene_revision=revision,
        system_variant=variant,
        compiled_geometry=compiled,
        source_models=(source,),
        receivers=(receiver,),
        requested_frequency_domain=FrequencyDomain(
            minimum_hz=100.0,
            maximum_hz=200.0,
        ),
        requested_observables=('complex_pressure',),
        environment=environment,
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=100.0,
            maximum_hz=200.0,
        ),
        valid_frequency_domain_authority_ref=_ref(
            'fixture-wave-valid-band',
            'e',
        ),
        wave_source_excitation_bindings=(binding,),
    )
    return {
        'scene_repository': scene_repository,
        'variant_repository': variant_repository,
        'equipment_repository': equipment_repository,
        'r110_repository': r110_repository,
        'r120_repository': r120_repository,
        'wave_repository': wave_repository,
        'revision': revision,
        'variant': variant,
        'definition': definition,
        'source': source,
        'compiled': compiled,
        'excitation': excitation,
        'binding': binding,
        'snapshot': snapshot,
    }


def test_explicit_wave_excitation_is_deterministic_and_not_inferred_from_r110(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)

    source = fx['source']
    excitation = fx['excitation']
    binding = fx['binding']

    assert (
        source.use_case_states.wave_excitation
        == 'BLOCKED_FOR_WAVE_EXCITATION'
    )
    assert excitation.quantity == 'complex_volume_velocity_m3_s'
    assert excitation.phasor_convention == 'exp(-i*omega*t)'
    assert excitation.reference_semantics == 'equipment_acoustic_reference_point'
    assert binding.r110_compiled_source_sha256 == source.semantic_sha256
    assert binding.excitation_semantic_sha256 == excitation.semantic_sha256


def test_wave_excitation_rejects_wrong_equipment_source(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    provenance = _provenance('2')
    other = build_acoustic_wave_excitation_authority(
        definition_id='other-equipment',
        definition_version='1',
        definition_sha256='f' * 64,
        samples=(
            ComplexVolumeVelocitySample(
                frequency_hz=100.0,
                real_m3_s=1.0e-4,
                imag_m3_s=0.0,
            ),
            ComplexVolumeVelocitySample(
                frequency_hz=200.0,
                real_m3_s=1.0e-4,
                imag_m3_s=0.0,
            ),
        ),
        interpolation=InterpolationProvenance(
            method='linear',
            implementation='fixture-linear',
            implementation_version='1',
            provenance=provenance,
        ),
        provenance=(provenance,),
        approximation_note='explicit unrelated source authority',
    )

    with pytest.raises(ValueError, match='does not match exact R110 source'):
        bind_wave_excitation_to_r110_source(
            source=fx['source'],
            excitation=other,
        )


def test_explicit_binding_promotes_snapshot_wave_readiness_and_dispatch(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    snapshot = fx['snapshot']

    assert snapshot.schema_version == 3
    assert snapshot.wave_source_excitation_bindings == (fx['binding'],)
    assert snapshot.readiness.wave_source_ready is True
    assert 'wave_source_excitation_blocked' not in snapshot.unresolved_conditions
    readiness = snapshot.readiness.observable_readiness[0]
    assert readiness.observable == 'complex_pressure'
    assert readiness.state == 'READY'

    request = build_acoustic_prediction_request(
        snapshot=snapshot,
        model_solver_role_id='r130-wave-fixture-role',
        requested_frequency_domain=snapshot.requested_frequency_domain,
        requested_observables=('complex_pressure',),
        numerical_fidelity_policy_ref=_ref(
            'fixture-numerical-policy',
            '6',
        ),
    )
    adapter = build_acoustic_solver_adapter_descriptor(
        adapter_id='r130-wave-fixture-adapter',
        adapter_version='1',
        model_solver_role_id='r130-wave-fixture-role',
        acoustic_domain='wave',
        solver_implementation_ref=_ref(
            'fixture-wave-solver-build',
            '7',
        ),
        solver_configuration_schema_ref=_ref(
            'fixture-wave-config-schema',
            '8',
        ),
        supported_snapshot_schema_versions=(3,),
        supported_observables=('complex_pressure',),
        valid_frequency_domain=snapshot.requested_frequency_domain,
    )
    dispatch = bind_prediction_request_to_solver_adapter(
        snapshot=snapshot,
        request=request,
        adapter=adapter,
        solver_configuration_ref=_ref(
            'fixture-wave-config',
            '9',
        ),
    )

    assert dispatch.state == 'READY'
    assert dispatch.reasons == ()


def test_wave_excitation_snapshot_save_reopen_reresolves_binding(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    repository = CadAcousticSnapshotRepository(
        fx['scene_repository'],
        variant_repository=fx['variant_repository'],
        r110_repository=fx['r110_repository'],
        r120_repository=fx['r120_repository'],
        wave_excitation_repository=fx['wave_repository'],
    )
    repository.save_snapshot(fx['snapshot'])

    reopened = CadAcousticSnapshotRepository(
        SceneRepository(fx['scene_repository'].path)
    ).get_snapshot(fx['snapshot'].snapshot_id)

    assert reopened == fx['snapshot']
    assert reopened is not None
    assert reopened.wave_source_excitation_bindings == (fx['binding'],)
