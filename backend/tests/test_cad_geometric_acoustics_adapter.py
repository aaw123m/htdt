from __future__ import annotations

from hashlib import sha256
import json
from math import isclose, sqrt
from pathlib import Path

import pytest

from htdt.acoustic_benchmark import AcousticMaterial, GeometricAcousticBand
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
from htdt.cad_acoustic_solver_dispatch_repository import (
    CadAcousticSolverDispatchRepository,
)
from htdt.cad_acoustic_solver_result import CadAcousticSolverResultRepository
from htdt.cad_directivity import (
    DirectivityCoordinateConvention,
    DirectivityNormalization,
    DirectivitySample,
    build_directivity_dataset,
)
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
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_geometric_acoustics_adapter import (
    DETERMINISTIC_GA_ADAPTER_ID,
    DETERMINISTIC_GA_ADAPTER_VERSION,
    CadDeterministicPathArtifactRepository,
    DeterministicGaConfiguration,
    GeometricMaterialAuthority,
    NativeImageSource,
    PYROOMACOUSTICS_SOLVER_IMPLEMENTATION_REF,
    PyroomacousticsImageSourceEngine,
    build_deterministic_ga_configuration,
    build_deterministic_ga_result_envelope,
    compile_deterministic_ga_execution_input,
    execute_deterministic_ga,
)
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
from htdt.r120_geometry_compiler import (
    AcousticRegionAuthority,
    AcousticRegionDeclaration,
    BoundaryTerminationAuthority,
    ExactExternalAuthorityRef,
    PortalAuthority,
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


NOW = '2026-09-20T00:30:00+00:00'

SHOEBOX_OBJ = b'''\
v 0 0 0
v 4 0 0
v 4 3 0
v 0 3 0
v 0 0 2
v 4 0 2
v 4 3 2
v 0 3 2
f 1 3 2
f 1 4 3
f 5 6 7
f 5 7 8
f 1 2 6
f 1 6 5
f 4 8 7
f 4 7 3
f 1 5 8
f 1 8 4
f 2 3 7
f 2 7 6
'''

SHOEBOX_WITH_TETRA_OCCLUDER_OBJ = SHOEBOX_OBJ + b'''\
v 1.8 1.3 0.5
v 2.2 1.3 0.5
v 2.0 1.7 0.5
v 2.0 1.5 1.5
f 9 11 10
f 9 10 12
f 9 12 11
f 10 11 12
'''


def _digest(payload: object) -> str:
    return sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()


def _ref(name: str, seed: str) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=name,
        authority_version='fixture-v1',
        semantic_hash_sha256=sha256(seed.encode('utf-8')).hexdigest(),
    )


def _material(*, supported: bool = True) -> GeometricMaterialAuthority:
    material = AcousticMaterial(
        material_id='fixture-wall',
        provenance='fixture exact GA material',
        version='1',
        wave_model='unsupported',
        geometric_model='banded' if supported else 'unsupported',
        geometric_bands=(
            (
                GeometricAcousticBand(
                    center_hz=500.0,
                    absorption=0.2,
                    scattering=0.1,
                ),
                GeometricAcousticBand(
                    center_hz=1000.0,
                    absorption=0.3,
                    scattering=0.2,
                ),
            )
            if supported
            else ()
        ),
    )
    payload = material.model_dump(mode='json')
    ref = ExactExternalAuthorityRef(
        authority_id='fixture-material:wall',
        authority_version=material.version,
        semantic_hash_sha256=_digest(payload),
    )
    return GeometricMaterialAuthority(authority_ref=ref, material=material)


def _semantic_geometry(*, occluder: bool):
    mesh = import_raw_visual_mesh(
        SHOEBOX_WITH_TETRA_OCCLUDER_OBJ if occluder else SHOEBOX_OBJ,
        source_name='r150-ga-fixture.obj',
    )
    ids = raw_triangle_ids(mesh)
    assignments = [
        SurfaceSemanticAssignment(
            surface_key='floor-z-min',
            triangle_ids=ids[0:2],
            semantic_class='room_boundary',
        ),
        SurfaceSemanticAssignment(
            surface_key='ceiling-z-max',
            triangle_ids=ids[2:4],
            semantic_class='room_boundary',
        ),
        SurfaceSemanticAssignment(
            surface_key='front-y-min',
            triangle_ids=ids[4:6],
            semantic_class='room_boundary',
        ),
        SurfaceSemanticAssignment(
            surface_key='rear-y-max',
            triangle_ids=ids[6:8],
            semantic_class='room_boundary',
        ),
        SurfaceSemanticAssignment(
            surface_key='left-x-min',
            triangle_ids=ids[8:10],
            semantic_class='room_boundary',
        ),
        SurfaceSemanticAssignment(
            surface_key='right-x-max',
            triangle_ids=ids[10:12],
            semantic_class='room_boundary',
        ),
    ]
    if occluder:
        assignments.append(
            SurfaceSemanticAssignment(
                surface_key='internal-occluder',
                triangle_ids=ids[12:16],
                semantic_class='object_surface',
            )
        )
    request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=None,
        source_to_scene_transform=explicit_identity_source_to_scene_transform(
            reason='fixture coordinates are explicit HTDT metres',
        ),
        surface_assignments=tuple(assignments),
    )
    return convert_raw_visual_mesh_to_semantic_geometry(mesh, request)


def _directivity_definition(*, narrow: bool):
    source_hash = 'd' * 64
    provenance = EquipmentDataProvenance(
        evidence_kind='measured',
        source_name='r150-ga-fixture',
        source_version='1',
        source_reference='unit-fixture',
        source_sha256=source_hash,
    )
    horizontal = (-30.0, 30.0) if narrow else (-180.0, 180.0)
    domain = DirectivityDomain(
        frequency=FrequencyDomain(minimum_hz=500.0, maximum_hz=1000.0),
        horizontal=AngleDomain(
            minimum_deg=horizontal[0],
            maximum_deg=horizontal[-1],
        ),
        vertical=AngleDomain(minimum_deg=-90.0, maximum_deg=90.0),
    )
    interpolation = InterpolationProvenance(
        method='linear',
        implementation='r150-ga-fixture-linear',
        implementation_version='1',
        provenance=provenance,
    )
    definition = build_equipment_definition(
        definition_id='r150-ga-speaker',
        version='1',
        identity_kind='user_defined',
        user_label='R150 GA fixture source',
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.2, y_m=0.2, z_m=0.3),
        acoustic_reference_point_m=Offset3(x_m=0.0, y_m=0.0, z_m=0.0),
        directivity=DirectivityCapability(
            tier='magnitude_only',
            data_format='custom',
            provenance=provenance,
            data_asset_sha256=source_hash,
            valid_domain=domain,
            interpolation=interpolation,
            coherent_phase=False,
        ),
    )
    horizontal_grid = (-30.0, 0.0, 30.0) if narrow else (-180.0, 0.0, 180.0)
    samples = tuple(
        DirectivitySample(
            frequency_hz=frequency,
            horizontal_angle_deg=horizontal_angle,
            vertical_angle_deg=vertical_angle,
            magnitude_db=0.0,
        )
        for frequency in (500.0, 1000.0)
        for horizontal_angle in horizontal_grid
        for vertical_angle in (-90.0, 0.0, 90.0)
    )
    dataset = build_directivity_dataset(
        dataset_id='r150-ga-speaker-dataset',
        version='1',
        definition=definition,
        source_asset_sha256=source_hash,
        source_format='custom',
        parser_id='fixture',
        parser_version='1',
        adapter_id='fixture',
        adapter_version='1',
        evidence_kind='measured',
        source_provenance=provenance,
        kind='magnitude_only',
        coordinate_convention=DirectivityCoordinateConvention(
            angle_semantics='horizontal_vertical',
            horizontal_wrap='none',
        ),
        normalization=DirectivityNormalization(
            source_magnitude_unit='db',
            reference='on_axis_per_frequency',
        ),
        frequencies_hz=(500.0, 1000.0),
        horizontal_angles_deg=horizontal_grid,
        vertical_angles_deg=(-90.0, 0.0, 90.0),
        samples=samples,
        interpolation=interpolation,
    )
    return definition, dataset


class FixtureImageEngine:
    engine_id = 'fixture.image-source'
    engine_version = '1'
    candidate_source_commit = None
    solver_implementation_ref = _ref(
        'fixture:deterministic-image-source',
        'fixture-image-source-implementation',
    )

    def execute_shoebox(
        self,
        *,
        dimensions_m: tuple[float, float, float],
        source_local_m: tuple[float, float, float],
        receiver_local_m: tuple[float, float, float],
    ) -> tuple[NativeImageSource, ...]:
        del receiver_local_m
        x, y, z = source_local_m
        width, depth, height = dimensions_m
        positions = (
            (x, y, z),
            (-x, y, z),
            (2.0 * width - x, y, z),
            (x, -y, z),
            (x, 2.0 * depth - y, z),
            (x, y, -z),
            (x, y, 2.0 * height - z),
        )
        return tuple(NativeImageSource(position_local_m=item) for item in positions)


def _fixture(
    tmp_path: Path,
    *,
    occluder: bool = False,
    narrow_directivity: bool = False,
    supported_material: bool = True,
    use_pyroomacoustics: bool = False,
    receiver_position: Position3 | None = None,
):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id='r150-ga-fixture',
        schema_version=4,
        room=None,
        r120_semantic_geometry=_semantic_geometry(occluder=occluder),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.2, y_m=0.2, z_m=0.3),
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
            ),
            SceneEntity(
                entity_id='receiver-mlp',
                kind='measurement_point',
                name='MLP',
                position=(
                    Position3(x_m=3.0, y_m=2.0, z_m=1.0)
                    if receiver_position is None
                    else receiver_position
                ),
            ),
        ),
    )
    revision = scene_repository.save(document, parent_revision_id=None).revision

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

    definition, dataset = _directivity_definition(narrow=narrow_directivity)
    equipment_repository.save_definition(definition)
    directivity_repository.save_dataset(dataset)
    variant = build_system_variant(
        baseline=revision,
        name='R150 GA fixture current',
        role_bindings=(ChannelRoleBinding(role_id='FL', display_name='Front left'),),
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
    source = compile_r110_source_model(
        scene_revision=revision,
        system_variant=variant,
        source_entity_id='speaker-fl',
        equipment_definition=definition,
        directivity_dataset=dataset,
    )
    r110_repository.save_model(source)

    geometry = revision.document.r120_semantic_geometry
    assert geometry is not None
    surface_by_key = {item.surface_key: item.surface_id for item in geometry.surfaces}
    room_surface_ids = tuple(
        surface_by_key[key]
        for key in (
            'floor-z-min',
            'ceiling-z-max',
            'front-y-min',
            'rear-y-max',
            'left-x-min',
            'right-x-max',
        )
    )
    region = make_acoustic_region_authority(
        (
            AcousticRegionDeclaration(
                region_id='room-air',
                boundary_surface_ids=room_surface_ids,
            ),
        )
    )
    portals = make_portal_authority(declaration_mode='explicit_none')
    terminations = make_boundary_termination_authority(
        declaration_mode='explicit_none'
    )
    material = _material(supported=supported_material)
    boundary_ref = _ref('fixture-boundary-physics', 'boundary')
    bindings = tuple(
        SurfaceBoundaryAuthorityBinding(
            source_surface_id=item.surface_id,
            material_authority=material.authority_ref,
            boundary_physics_authority=boundary_ref,
        )
        for item in geometry.surfaces
    )
    compiled = compile_r120_geometry(
        revision,
        make_r120_geometry_compilation_request(
            revision,
            geometric_tolerance_m=1.0e-9,
        ),
        surface_boundary_bindings=bindings,
        region_authority=region,
        portal_authority=portals,
        boundary_termination_authority=terminations,
    )
    r120_repository.save_compiled_geometry(compiled)

    receiver = receiver_binding_from_scene(
        scene_revision=revision,
        system_variant=variant,
        entity_id='receiver-mlp',
        requested_output_capabilities=('deterministic_paths',),
    )
    environment = SnapshotEnvironmentAuthorityRef(
        authority=_ref('fixture-environment', 'environment'),
        sound_speed_m_s=343.0,
        sound_speed_source_authority=_ref(
            'fixture-sound-speed',
            'sound-speed',
        ),
    )
    domain = FrequencyDomain(minimum_hz=500.0, maximum_hz=1000.0)
    snapshot = build_acoustic_scene_snapshot(
        scene_revision=revision,
        system_variant=variant,
        compiled_geometry=compiled,
        source_models=(source,),
        receivers=(receiver,),
        requested_frequency_domain=domain,
        requested_observables=('deterministic_paths',),
        environment=environment,
        valid_frequency_domain=domain,
        valid_frequency_domain_authority_ref=_ref(
            'fixture-valid-domain',
            'valid-domain',
        ),
    )
    request = build_acoustic_prediction_request(
        snapshot=snapshot,
        model_solver_role_id='r150-deterministic-ga',
        requested_frequency_domain=domain,
        requested_observables=('deterministic_paths',),
        numerical_fidelity_policy_ref=_ref(
            'fixture-fidelity-policy',
            'fidelity',
        ),
    )
    configuration = build_deterministic_ga_configuration(
        frequency_centers_hz=(500.0, 1000.0),
        geometric_tolerance_m=1.0e-9,
        engine_image_match_tolerance_m=1.0e-8,
    )
    implementation_ref = (
        PYROOMACOUSTICS_SOLVER_IMPLEMENTATION_REF
        if use_pyroomacoustics
        else FixtureImageEngine.solver_implementation_ref
    )
    configuration_schema_ref = _ref(
        'htdt.r150.deterministic-ga-configuration.schema',
        'config-schema',
    )
    descriptor = build_acoustic_solver_adapter_descriptor(
        adapter_id=DETERMINISTIC_GA_ADAPTER_ID,
        adapter_version=DETERMINISTIC_GA_ADAPTER_VERSION,
        model_solver_role_id='r150-deterministic-ga',
        acoustic_domain='geometric',
        solver_implementation_ref=implementation_ref,
        solver_configuration_schema_ref=configuration_schema_ref,
        supported_snapshot_schema_versions=(1, 2, 3),
        supported_observables=('deterministic_paths',),
        valid_frequency_domain=domain,
    )
    dispatch = bind_prediction_request_to_solver_adapter(
        snapshot=snapshot,
        request=request,
        adapter=descriptor,
        solver_configuration_ref=configuration.as_external_ref(),
    )
    assert dispatch.state == 'READY'

    snapshot_repository = CadAcousticSnapshotRepository(
        scene_repository,
        variant_repository=variant_repository,
        r110_repository=r110_repository,
        r120_repository=r120_repository,
    )
    snapshot_repository.save_snapshot(snapshot)
    snapshot_repository.save_prediction_request(request)

    static_external = {
        tuple(
            (
                ref.authority_id,
                ref.authority_version,
                ref.semantic_hash_sha256,
            )
        ): ref
        for ref in (
            implementation_ref,
            configuration_schema_ref,
            configuration.as_external_ref(),
        )
    }

    def external_resolver(ref: ExactExternalAuthorityRef):
        return static_external.get(
            (ref.authority_id, ref.authority_version, ref.semantic_hash_sha256)
        )

    dispatch_repository = CadAcousticSolverDispatchRepository(
        scene_repository,
        snapshot_repository=snapshot_repository,
        external_authority_resolver=external_resolver,
    )
    dispatch_repository.save_descriptor(descriptor)
    dispatch_repository.save_dispatch(dispatch)

    geometry_authorities = {
        item.authority_id: item
        for item in (region, portals, terminations)
    }
    material_box = {'value': material}

    def geometry_resolver(ref: ExactExternalAuthorityRef):
        item = geometry_authorities.get(ref.authority_id)
        if item is None:
            return None
        actual = ExactExternalAuthorityRef(
            authority_id=item.authority_id,
            authority_version=item.authority_version,
            semantic_hash_sha256=item.semantic_hash_sha256,
        )
        return item if actual == ref else None

    def material_resolver(ref: ExactExternalAuthorityRef):
        item = material_box['value']
        return (
            item
            if item is not None and item.authority_ref == ref
            else None
        )

    def configuration_resolver(ref: ExactExternalAuthorityRef):
        return configuration if configuration.as_external_ref() == ref else None

    execution_input = compile_deterministic_ga_execution_input(
        snapshot=snapshot,
        request=request,
        dispatch=dispatch,
        descriptor=descriptor,
        compiled_geometry=compiled,
        region_authority=region,
        portal_authority=portals,
        boundary_termination_authority=terminations,
        directivity_datasets=(dataset,),
        configuration=configuration,
    )

    return {
        'scene_repository': scene_repository,
        'snapshot_repository': snapshot_repository,
        'dispatch_repository': dispatch_repository,
        'snapshot': snapshot,
        'request': request,
        'descriptor': descriptor,
        'dispatch': dispatch,
        'compiled': compiled,
        'region': region,
        'portals': portals,
        'terminations': terminations,
        'dataset': dataset,
        'material': material,
        'material_box': material_box,
        'material_resolver': material_resolver,
        'configuration': configuration,
        'configuration_resolver': configuration_resolver,
        'geometry_resolver': geometry_resolver,
        'external_resolver': external_resolver,
        'execution_input': execution_input,
        'surface_by_key': surface_by_key,
    }


def _execute(fx, engine=None):
    return execute_deterministic_ga(
        execution_input=fx['execution_input'],
        compiled_geometry=fx['compiled'],
        directivity_datasets=(fx['dataset'],),
        material_resolver=fx['material_resolver'],
        engine=FixtureImageEngine() if engine is None else engine,
    )


def test_direct_and_first_reflection_match_analytic_geometry(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    artifact = _execute(fx)

    direct = next(item for item in artifact.paths if item.path_type == 'direct')
    assert isclose(direct.geometric_path_length_m, sqrt(5.0), abs_tol=1.0e-9)
    assert isclose(
        direct.propagation_delay_s,
        sqrt(5.0) / 343.0,
        abs_tol=1.0e-12,
    )

    front_id = fx['surface_by_key']['front-y-min']
    reflected = next(
        item
        for item in artifact.paths
        if item.path_type == 'specular_reflection'
        and item.ordered_interaction_surface_ids == (front_id,)
    )
    assert isclose(reflected.geometric_path_length_m, sqrt(13.0), abs_tol=1.0e-9)
    point = reflected.ordered_interaction_points[0]
    assert isclose(point.x_m, 5.0 / 3.0, abs_tol=1.0e-9)
    assert isclose(point.y_m, 0.0, abs_tol=1.0e-9)
    assert isclose(point.z_m, 1.0, abs_tol=1.0e-9)
    assert reflected.bands[0].boundary_material is not None
    assert reflected.bands[0].coherent_phase == 'UNAVAILABLE_NOT_SYNTHESIZED'
    assert artifact.coherent_phase_authority == 'UNAVAILABLE_NOT_SYNTHESIZED'


def test_blocked_direct_path_is_not_retained_and_object_reflection_is_not_invented(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path, occluder=True)
    artifact = _execute(fx)

    assert not any(item.path_type == 'direct' for item in artifact.paths)
    assert any(
        item.path_type == 'direct' and item.decision == 'BLOCKED_VISIBILITY'
        for item in artifact.rejected_candidates
    )
    object_id = fx['surface_by_key']['internal-occluder']
    assert any(
        item.interaction_surface_ids == (object_id,)
        and item.decision == 'UNSUPPORTED_GEOMETRY'
        for item in artifact.rejected_candidates
    )


def test_receiver_outside_explicit_region_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(
        ValueError,
        match='receiver receiver-mlp position is not strictly inside',
    ):
        _fixture(
            tmp_path,
            receiver_position=Position3(x_m=4.5, y_m=2.0, z_m=1.0),
        )


def test_same_exact_input_has_same_path_identity_and_order(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    first = _execute(fx)
    second = _execute(fx)

    assert second == first
    assert second.artifact_id == first.artifact_id
    assert [item.path_id for item in second.paths] == [
        item.path_id for item in first.paths
    ]


def test_engine_must_match_ready_dispatch_solver_implementation(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)

    class WrongImplementationEngine(FixtureImageEngine):
        solver_implementation_ref = _ref(
            'fixture:wrong-image-source',
            'wrong-image-source-implementation',
        )

    with pytest.raises(ValueError, match='solver implementation authority'):
        _execute(fx, engine=WrongImplementationEngine())


def test_unsupported_directivity_angle_is_not_filled_as_omnidirectional(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path, narrow_directivity=True)
    artifact = _execute(fx)

    assert not any(item.path_type == 'direct' for item in artifact.paths)
    assert any(
        item.path_type == 'direct'
        and item.decision == 'UNSUPPORTED_DIRECTIVITY'
        for item in artifact.rejected_candidates
    )


def test_missing_geometric_boundary_quantity_does_not_fabricate_reflection(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path, supported_material=False)
    artifact = _execute(fx)

    assert any(item.path_type == 'direct' for item in artifact.paths)
    assert not any(
        item.path_type == 'specular_reflection' for item in artifact.paths
    )
    assert any(
        item.decision == 'UNSUPPORTED_BOUNDARY_QUANTITY'
        for item in artifact.rejected_candidates
    )


def test_execution_input_artifact_and_result_save_reopen_fail_closed(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    artifact = _execute(fx)

    path_repository = CadDeterministicPathArtifactRepository(
        fx['scene_repository'],
        snapshot_repository=fx['snapshot_repository'],
        dispatch_repository=fx['dispatch_repository'],
        configuration_resolver=fx['configuration_resolver'],
        material_resolver=fx['material_resolver'],
        geometry_authority_resolver=fx['geometry_resolver'],
    )
    path_repository.save_execution_input(fx['execution_input'])
    path_repository.save(artifact)

    reopened = CadDeterministicPathArtifactRepository(
        SceneRepository(fx['scene_repository'].path),
        snapshot_repository=CadAcousticSnapshotRepository(
            SceneRepository(fx['scene_repository'].path)
        ),
        dispatch_repository=CadAcousticSolverDispatchRepository(
            SceneRepository(fx['scene_repository'].path),
            external_authority_resolver=fx['external_resolver'],
        ),
        configuration_resolver=fx['configuration_resolver'],
        material_resolver=fx['material_resolver'],
        geometry_authority_resolver=fx['geometry_resolver'],
    )
    assert reopened.get_execution_input(fx['execution_input'].execution_input_id) == (
        fx['execution_input']
    )
    assert reopened.get(artifact.artifact_id) == artifact

    result = build_deterministic_ga_result_envelope(
        artifact=artifact,
        dispatch=fx['dispatch'],
        request=fx['request'],
        completed_at_utc='2026-09-20T01:00:00+00:00',
    )

    def result_external_resolver(ref: ExactExternalAuthorityRef):
        from_path = reopened.resolve_external_authority(ref)
        if from_path is not None:
            return from_path
        return fx['external_resolver'](ref)

    result_repository = CadAcousticSolverResultRepository(
        SceneRepository(fx['scene_repository'].path),
        dispatch_resolver=reopened.dispatch_repository,
        request_resolver=reopened.snapshot_repository,
        external_authority_resolver=result_external_resolver,
    )
    result_repository.save(result)
    reopened_result_repository = CadAcousticSolverResultRepository(
        SceneRepository(fx['scene_repository'].path),
        dispatch_resolver=reopened.dispatch_repository,
        request_resolver=reopened.snapshot_repository,
        external_authority_resolver=result_external_resolver,
    )
    assert reopened_result_repository.get(result.result_id) == result

    fx['material_box']['value'] = None
    with pytest.raises(ValueError, match='boundary material exact authority'):
        reopened.get(artifact.artifact_id)


def test_actual_pyroomacoustics_candidate_executes_same_direct_first_reflection_fixture(
    tmp_path: Path,
) -> None:
    pytest.importorskip('pyroomacoustics')
    fx = _fixture(tmp_path, use_pyroomacoustics=True)
    artifact = _execute(fx, engine=PyroomacousticsImageSourceEngine())

    assert any(item.path_type == 'direct' for item in artifact.paths)
    front_id = fx['surface_by_key']['front-y-min']
    reflected = next(
        item
        for item in artifact.paths
        if item.path_type == 'specular_reflection'
        and item.ordered_interaction_surface_ids == (front_id,)
    )
    assert isclose(reflected.geometric_path_length_m, sqrt(13.0), abs_tol=1.0e-8)
