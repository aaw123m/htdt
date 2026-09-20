from __future__ import annotations

import argparse
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import traceback

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
from htdt.cad_candidate_wave_execution import (
    COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
    PFFDTD_CANDIDATE_ADAPTER_ID,
    PFFDTD_CANDIDATE_ADAPTER_VERSION,
    CandidateResourceConfiguration,
    CandidateWaveExecutionCancelled,
    CandidateWaveExecutionError,
    ExactJsonAuthorityStore,
    PffdtdCandidateWaveExecutor,
    build_pffdtd_candidate_configuration,
    register_authority_model,
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


PFFDTD_SHA = 'aa319f6c86517cb95aabfae8656277da62c3ead5'
NOW = '2026-09-20T00:00:00+00:00'
FIXTURE_ID = 'r130a-candidate-wave-cube-v1'

CUBE_OBJ = b'''\
v 0 0 0
v 4 0 0
v 4 4 0
v 0 4 0
v 0 0 4
v 4 0 4
v 4 4 4
v 0 4 4
f 1 4 3
f 1 3 2
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


def _hash_json(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()


def _exact_ref(model) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=model.authority_id,
        authority_version=model.authority_version,
        semantic_hash_sha256=model.semantic_hash_sha256,
    )


def _semantic_geometry():
    mesh = import_raw_visual_mesh(
        CUBE_OBJ,
        source_name=f'{FIXTURE_ID}.obj',
    )
    request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=None,
        source_to_scene_transform=explicit_identity_source_to_scene_transform(
            reason='candidate fixture OBJ is authored directly in exact HTDT metres',
        ),
        surface_assignments=(
            SurfaceSemanticAssignment(
                surface_key='closed-room-shell',
                triangle_ids=raw_triangle_ids(mesh),
                semantic_class='room_boundary',
            ),
        ),
    )
    return convert_raw_visual_mesh_to_semantic_geometry(mesh, request)


def _scene() -> SceneDocument:
    return SceneDocument(
        document_id=FIXTURE_ID,
        schema_version=4,
        room=None,
        r120_semantic_geometry=_semantic_geometry(),
        entities=(
            SceneEntity(
                entity_id='speaker-source',
                kind='speaker',
                name='Explicit wave source',
                speaker_role='C',
                position=Position3(x_m=1.5, y_m=2.0, z_m=2.0),
                size_m=Size3(x_m=0.2, y_m=0.2, z_m=0.2),
                acoustic_reference_offset_m=Offset3(),
                aim_xyz=Direction3(x=1.0, y=0.0, z=0.0),
            ),
            SceneEntity(
                entity_id='receiver-1',
                kind='measurement_point',
                name='Candidate receiver',
                position=Position3(x_m=2.5, y_m=2.0, z_m=2.0),
            ),
        ),
    )


def _register_r120_authority(store, model) -> ExactExternalAuthorityRef:
    ref = _exact_ref(model)
    return register_authority_model(
        store,
        ref=ref,
        model=model,
        exclude={'authority_id', 'semantic_hash_sha256'},
    )


def _fixture(
    root: Path,
    upstream_root: Path,
):
    db_path = root / 'candidate.sqlite3'
    authority_root = root / 'authorities'
    work_root = root / 'work'
    store = ExactJsonAuthorityStore(authority_root)

    scene_repository = SceneRepository(db_path)
    revision = scene_repository.save(
        _scene(),
        parent_revision_id=None,
    ).revision
    variant_repository = CadSystemVariantRepository(scene_repository)
    equipment_repository = CadEquipmentRepository(
        scene_repository,
        variant_repository,
    )
    r110_repository = CadR110SourceRepository(
        scene_repository,
        variant_repository=variant_repository,
        equipment_repository=equipment_repository,
    )
    wave_repository = CadWaveExcitationRepository(
        scene_repository,
        equipment_repository=equipment_repository,
        r110_repository=r110_repository,
    )
    r120_repository = R120GeometryCompilerRepository(scene_repository)

    equipment_provenance = EquipmentDataProvenance(
        evidence_kind='user_defined',
        source_name='HTDT R130A candidate fixture equipment identity',
        source_version='1',
        source_reference='synthetic candidate fixture; not measured evidence',
        source_sha256=_hash_json(
            {'fixture_id': FIXTURE_ID, 'kind': 'equipment-identity'}
        ),
    )
    equipment = build_equipment_definition(
        definition_id='r130a-candidate-source',
        version='1',
        identity_kind='user_defined',
        user_label='R130A explicit wave fixture source',
        provenance=(equipment_provenance,),
        cabinet_envelope_m=Size3(x_m=0.2, y_m=0.2, z_m=0.2),
        acoustic_reference_point_m=Offset3(),
        directivity=DirectivityCapability(
            tier='unknown',
            data_format='unknown',
            provenance=equipment_provenance,
        ),
    )
    equipment_repository.save_definition(equipment)

    variant = build_system_variant(
        baseline=revision,
        name='R130A candidate execution fixture',
        role_bindings=(
            ChannelRoleBinding(role_id='C', display_name='Candidate source'),
        ),
        proposed_entities=(),
        equipment_bindings=(
            EquipmentBindingRef(
                entity_id='speaker-source',
                equipment_definition_id=equipment.definition_id,
                equipment_definition_version=equipment.version,
                equipment_definition_sha256=equipment.semantic_sha256,
            ),
        ),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant)

    source = compile_r110_source_model(
        scene_revision=revision,
        system_variant=variant,
        source_entity_id='speaker-source',
        equipment_definition=equipment,
    )
    r110_repository.save_model(source)

    excitation_source_sha = _hash_json(
        {
            'fixture_id': FIXTURE_ID,
            'quantity': 'complex_volume_velocity_m3_s',
            'samples': [
                [40.0, 1.0e-4, 0.0],
                [80.0, 0.0, 1.0e-4],
            ],
            'statement': (
                'Explicit synthetic acoustic excitation for candidate execution '
                'only; it is not derived from speaker sensitivity.'
            ),
        }
    )
    excitation_provenance = EquipmentDataProvenance(
        evidence_kind='user_defined',
        source_name='HTDT R130A explicit acoustic wave excitation',
        source_version='1',
        source_reference='synthetic candidate fixture; not owned-room evidence',
        source_sha256=excitation_source_sha,
    )
    excitation = build_acoustic_wave_excitation_authority(
        definition_id=equipment.definition_id,
        definition_version=equipment.version,
        definition_sha256=equipment.semantic_sha256,
        samples=(
            ComplexVolumeVelocitySample(
                frequency_hz=40.0,
                real_m3_s=1.0e-4,
                imag_m3_s=0.0,
            ),
            ComplexVolumeVelocitySample(
                frequency_hz=80.0,
                real_m3_s=0.0,
                imag_m3_s=1.0e-4,
            ),
        ),
        interpolation=InterpolationProvenance(
            method='linear',
            implementation='fixture-exact-sample-linear',
            implementation_version='1',
            provenance=excitation_provenance,
        ),
        provenance=(excitation_provenance,),
        approximation_note=(
            'No sensitivity conversion is used. Candidate execution consumes '
            'the two explicit complex volume-velocity samples directly.'
        ),
    )
    wave_repository.save_excitation(excitation)
    wave_binding = bind_wave_excitation_to_r110_source(
        source=source,
        excitation=excitation,
    )
    wave_repository.save_binding(wave_binding)

    geometry = revision.document.r120_semantic_geometry
    assert geometry is not None
    assert len(geometry.surfaces) == 1
    surface_id = geometry.surfaces[0].surface_id

    material_ref = store.put_json(
        'r130a-fixture-material',
        '1',
        {
            'authority_kind': 'acoustic_material',
            'fixture_id': FIXTURE_ID,
            'name': 'candidate rigid shell host material',
            'candidate_only': True,
        },
    )
    boundary_ref = store.put_json(
        'r130a-fixture-wave-boundary',
        '1',
        {
            'authority_kind': 'wave_boundary_physics',
            'model': 'rigid_zero_normal_velocity',
            'normal_velocity_m_s': 0.0,
            'fixture_id': FIXTURE_ID,
            'candidate_only': True,
        },
    )
    region = make_acoustic_region_authority(
        (
            AcousticRegionDeclaration(
                region_id='room-air',
                boundary_surface_ids=(surface_id,),
            ),
        )
    )
    portals = make_portal_authority(declaration_mode='explicit_none')
    terminations = make_boundary_termination_authority(
        declaration_mode='explicit_none'
    )
    region_ref = _register_r120_authority(store, region)
    portal_ref = _register_r120_authority(store, portals)
    termination_ref = _register_r120_authority(store, terminations)

    compiled = compile_r120_geometry(
        revision,
        make_r120_geometry_compilation_request(
            revision,
            geometric_tolerance_m=1.0e-6,
        ),
        surface_boundary_bindings=(
            SurfaceBoundaryAuthorityBinding(
                source_surface_id=surface_id,
                material_authority=material_ref,
                boundary_physics_authority=boundary_ref,
            ),
        ),
        region_authority=region,
        portal_authority=portals,
        boundary_termination_authority=terminations,
    )
    r120_repository.save_compiled_geometry(compiled)
    assert compiled.readiness.wave_geometry_ready

    sound_speed_ref = store.put_json(
        'r130a-fixture-sound-speed',
        '1',
        {
            'quantity': 'sound_speed_m_s',
            'value': 343.2,
            'fixture_id': FIXTURE_ID,
            'candidate_only': True,
        },
    )
    temperature_ref = store.put_json(
        'r130a-fixture-temperature',
        '1',
        {
            'quantity': 'temperature_c',
            'value': 20.0,
            'fixture_id': FIXTURE_ID,
            'candidate_only': True,
        },
    )
    environment_ref = store.put_json(
        'r130a-fixture-environment',
        '1',
        {
            'fixture_id': FIXTURE_ID,
            'sound_speed_m_s': 343.2,
            'temperature_c': 20.0,
            'candidate_only': True,
        },
    )
    environment = SnapshotEnvironmentAuthorityRef(
        authority=environment_ref,
        sound_speed_m_s=343.2,
        sound_speed_source_authority=sound_speed_ref,
        temperature_c=20.0,
        temperature_source_authority=temperature_ref,
    )
    frequency_ref = store.put_json(
        'r130a-fixture-valid-frequency-domain',
        '1',
        {
            'minimum_hz': 40.0,
            'maximum_hz': 80.0,
            'fixture_id': FIXTURE_ID,
            'candidate_only': True,
        },
    )
    receiver = receiver_binding_from_scene(
        scene_revision=revision,
        system_variant=variant,
        entity_id='receiver-1',
        requested_output_capabilities=('complex_pressure',),
    )
    band = FrequencyDomain(minimum_hz=40.0, maximum_hz=80.0)

    snapshot = build_acoustic_scene_snapshot(
        scene_revision=revision,
        compiled_geometry=compiled,
        source_models=(source,),
        receivers=(receiver,),
        requested_frequency_domain=band,
        requested_observables=('complex_pressure',),
        system_variant=variant,
        environment=environment,
        valid_frequency_domain=band,
        valid_frequency_domain_authority_ref=frequency_ref,
        wave_source_excitation_bindings=(wave_binding,),
    )
    assert snapshot.schema_version == 3
    assert snapshot.readiness.wave_source_ready
    assert snapshot.readiness.wave_boundary_ready
    assert snapshot.readiness.requested_observable_ready

    snapshot_repository = CadAcousticSnapshotRepository(
        scene_repository,
        variant_repository=variant_repository,
        r110_repository=r110_repository,
        r120_repository=r120_repository,
        wave_excitation_repository=wave_repository,
    )
    snapshot_repository.save_snapshot(snapshot)

    fidelity_ref = store.put_json(
        'r130a-candidate-fidelity-policy',
        '1',
        {
            'fixture_id': FIXTURE_ID,
            'purpose': 'bounded candidate execution only',
            'numerical_acceptance_claim': False,
            'production_adoption_claim': False,
        },
    )
    request = build_acoustic_prediction_request(
        snapshot=snapshot,
        model_solver_role_id='r130a-candidate-wave',
        requested_frequency_domain=band,
        requested_observables=('complex_pressure',),
        numerical_fidelity_policy_ref=fidelity_ref,
    )
    snapshot_repository.save_prediction_request(request)

    implementation_ref = store.put_json(
        'pffdtd-solver-implementation',
        PFFDTD_SHA,
        {
            'repository': 'bsxfun/pffdtd',
            'git_commit_sha': PFFDTD_SHA,
            'runtime': 'python/numba CPU',
            'candidate_only': True,
            'production_adoption_claim': False,
        },
    )
    configuration_schema_ref = store.put_json(
        'pffdtd-candidate-configuration-schema',
        '1',
        {
            'schema_version': 'htdt.r130a.pffdtd-candidate-config-schema-1',
            'required_semantics': [
                'explicit_fmax_hz',
                'explicit_points_per_wavelength',
                'explicit_finite_record_duration_s',
                'exact_frequency_samples_hz',
                'explicit_density_authority',
                'explicit_humidity_authority',
                'bounded_cpu_resources',
            ],
            'candidate_only': True,
        },
    )
    density_ref = store.put_json(
        'r130a-fixture-density',
        '1',
        {
            'quantity': 'air_density_kg_m3',
            'value': 1.2,
            'fixture_id': FIXTURE_ID,
            'candidate_only': True,
        },
    )
    humidity_ref = store.put_json(
        'r130a-fixture-relative-humidity',
        '1',
        {
            'quantity': 'relative_humidity_percent',
            'value': 50.0,
            'fixture_id': FIXTURE_ID,
            'candidate_only': True,
        },
    )
    configuration = build_pffdtd_candidate_configuration(
        expected_pffdtd_commit_sha=PFFDTD_SHA,
        fmax_hz=100.0,
        points_per_wavelength=8.0,
        duration_s=0.03,
        frequency_samples_hz=(40.0, 80.0),
        density_kg_m3=1.2,
        density_authority_ref=density_ref,
        relative_humidity_percent=50.0,
        humidity_authority_ref=humidity_ref,
        resource=CandidateResourceConfiguration(
            solver_threads=4,
            setup_processes=1,
            max_grid_cells=200_000,
            max_time_steps=512,
            max_output_bytes=32 * 1024 * 1024,
            max_solver_wall_seconds=60.0,
        ),
    )
    store.put_exact_json(
        configuration.as_external_ref(),
        configuration.semantic_payload(),
    )
    descriptor = build_acoustic_solver_adapter_descriptor(
        adapter_id=PFFDTD_CANDIDATE_ADAPTER_ID,
        adapter_version=PFFDTD_CANDIDATE_ADAPTER_VERSION,
        model_solver_role_id='r130a-candidate-wave',
        acoustic_domain='wave',
        solver_implementation_ref=implementation_ref,
        solver_configuration_schema_ref=configuration_schema_ref,
        supported_snapshot_schema_versions=(1, 3),
        supported_observables=('complex_pressure',),
        valid_frequency_domain=band,
    )
    dispatch_repository = CadAcousticSolverDispatchRepository(
        scene_repository,
        snapshot_repository=snapshot_repository,
        external_authority_resolver=store.resolve,
    )
    dispatch_repository.save_descriptor(descriptor)
    dispatch = bind_prediction_request_to_solver_adapter(
        snapshot=snapshot,
        request=request,
        adapter=descriptor,
        solver_configuration_ref=configuration.as_external_ref(),
    )
    assert dispatch.state == 'READY'
    dispatch_repository.save_dispatch(dispatch)

    output_schema_ref = store.put_json(
        'r130a-complex-pressure-artifact-schema',
        COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
        {
            'schema_version': COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
            'quantity_type': 'complex_pressure',
            'complex_representation': 'cartesian_real_imag',
            'phasor_convention': 'exp(-i*omega*t)',
            'receiver_axis': 'ordered exact receiver ids',
            'frequency_axis_unit': 'Hz',
            'quantity_unit': 'Pa',
            'raw_asset_hash_required': True,
            'candidate_only': True,
        },
    )
    result_repository = CadAcousticSolverResultRepository(
        scene_repository,
        dispatch_resolver=dispatch_repository,
        request_resolver=snapshot_repository,
        external_authority_resolver=store.resolve,
    )
    executor = PffdtdCandidateWaveExecutor(
        snapshot_repository=snapshot_repository,
        dispatch_repository=dispatch_repository,
        r120_repository=r120_repository,
        r110_repository=r110_repository,
        wave_excitation_repository=wave_repository,
        result_repository=result_repository,
        authority_store=store,
        output_schema_ref=output_schema_ref,
        upstream_root=upstream_root,
        work_root=work_root,
    )

    return {
        'db_path': db_path,
        'authority_root': authority_root,
        'work_root': work_root,
        'store': store,
        'scene_repository': scene_repository,
        'variant_repository': variant_repository,
        'equipment_repository': equipment_repository,
        'r110_repository': r110_repository,
        'wave_repository': wave_repository,
        'r120_repository': r120_repository,
        'snapshot_repository': snapshot_repository,
        'dispatch_repository': dispatch_repository,
        'result_repository': result_repository,
        'executor': executor,
        'configuration': configuration,
        'descriptor': descriptor,
        'dispatch': dispatch,
        'request': request,
        'snapshot': snapshot,
        'revision': revision,
        'variant': variant,
        'source': source,
        'wave_binding': wave_binding,
        'excitation': excitation,
        'compiled': compiled,
        'output_schema_ref': output_schema_ref,
        'environment': environment,
        'fidelity_ref': fidelity_ref,
    }


def _result_count(db_path: Path) -> int:
    with closing(sqlite3.connect(db_path)) as connection:
        row = connection.execute(
            'SELECT COUNT(*) FROM cad_acoustic_solver_results'
        ).fetchone()
    assert row is not None
    return int(row[0])


def _assert_preflight_gates(fixture: dict[str, object]) -> dict[str, bool]:
    executor = fixture['executor']
    configuration = fixture['configuration']
    dispatch = fixture['dispatch']
    snapshot_repository = fixture['snapshot_repository']
    scene_repository = fixture['scene_repository']
    snapshot = fixture['snapshot']
    request = fixture['request']
    descriptor = fixture['descriptor']
    store = fixture['store']
    source = fixture['source']
    revision = fixture['revision']
    variant = fixture['variant']
    compiled = fixture['compiled']
    environment = fixture['environment']
    fidelity_ref = fixture['fidelity_ref']

    first, model_a = executor.compile_input(
        dispatch_binding_id=dispatch.binding_id,
        configuration=configuration,
    )
    second, model_b = executor.compile_input(
        dispatch_binding_id=dispatch.binding_id,
        configuration=configuration,
    )
    assert first == second
    assert model_a == model_b

    # Explicitly remove wave-excitation composition at the snapshot layer.
    blocked_snapshot = build_acoustic_scene_snapshot(
        scene_revision=revision,
        compiled_geometry=compiled,
        source_models=(source,),
        receivers=snapshot.receivers,
        requested_frequency_domain=snapshot.requested_frequency_domain,
        requested_observables=('complex_pressure',),
        system_variant=variant,
        environment=environment,
    )
    snapshot_repository.save_snapshot(blocked_snapshot)
    blocked_request = build_acoustic_prediction_request(
        snapshot=blocked_snapshot,
        model_solver_role_id=request.model_solver_role_id,
        requested_frequency_domain=request.requested_frequency_domain,
        requested_observables=('complex_pressure',),
        numerical_fidelity_policy_ref=fidelity_ref,
    )
    snapshot_repository.save_prediction_request(blocked_request)
    blocked_dispatch = bind_prediction_request_to_solver_adapter(
        snapshot=blocked_snapshot,
        request=blocked_request,
        adapter=descriptor,
        solver_configuration_ref=configuration.as_external_ref(),
    )
    assert blocked_dispatch.state == 'BLOCKED'
    fixture['dispatch_repository'].save_dispatch(blocked_dispatch)
    try:
        executor.compile_input(
            dispatch_binding_id=blocked_dispatch.binding_id,
            configuration=configuration,
        )
    except CandidateWaveExecutionError as exc:
        assert 'requires READY' in str(exc)
    else:
        raise AssertionError('BLOCKED dispatch was executable')

    # An observable outside the exact snapshot contract is rejected before dispatch.
    unsupported_observable = False
    try:
        build_acoustic_prediction_request(
            snapshot=snapshot,
            model_solver_role_id=request.model_solver_role_id,
            requested_frequency_domain=request.requested_frequency_domain,
            requested_observables=('phase_response',),
            numerical_fidelity_policy_ref=fidelity_ref,
        )
    except ValueError:
        unsupported_observable = True
    assert unsupported_observable

    # A request band outside snapshot/adapter authority becomes UNSUPPORTED.
    wide_request = build_acoustic_prediction_request(
        snapshot=snapshot,
        model_solver_role_id=request.model_solver_role_id,
        requested_frequency_domain=FrequencyDomain(
            minimum_hz=40.0,
            maximum_hz=100.0,
        ),
        requested_observables=('complex_pressure',),
        numerical_fidelity_policy_ref=fidelity_ref,
    )
    snapshot_repository.save_prediction_request(wide_request)
    wide_dispatch = bind_prediction_request_to_solver_adapter(
        snapshot=snapshot,
        request=wide_request,
        adapter=descriptor,
        solver_configuration_ref=configuration.as_external_ref(),
    )
    assert wide_dispatch.state == 'UNSUPPORTED'
    fixture['dispatch_repository'].save_dispatch(wide_dispatch)
    try:
        executor.compile_input(
            dispatch_binding_id=wide_dispatch.binding_id,
            configuration=configuration,
        )
    except CandidateWaveExecutionError as exc:
        assert 'requires READY' in str(exc)
    else:
        raise AssertionError('UNSUPPORTED dispatch was executable')

    file_count = len(tuple(fixture['authority_root'].glob('*.json')))
    try:
        executor.execute(
            dispatch_binding_id=dispatch.binding_id,
            configuration=configuration,
            cancel_check=lambda: True,
        )
    except CandidateWaveExecutionCancelled:
        pass
    else:
        raise AssertionError('cancelled execution produced a result')
    assert len(tuple(fixture['authority_root'].glob('*.json'))) == file_count

    artifacts_before_failure = set(fixture['authority_root'].glob('*.json'))
    results_before_failure = _result_count(fixture['db_path'])
    failed_executor = PffdtdCandidateWaveExecutor(
        snapshot_repository=fixture['snapshot_repository'],
        dispatch_repository=fixture['dispatch_repository'],
        r120_repository=fixture['r120_repository'],
        r110_repository=fixture['r110_repository'],
        wave_excitation_repository=fixture['wave_repository'],
        result_repository=fixture['result_repository'],
        authority_store=fixture['store'],
        output_schema_ref=fixture['output_schema_ref'],
        upstream_root=fixture['work_root'] / 'missing-pffdtd-checkout',
        work_root=fixture['work_root'] / 'expected-backend-failure',
    )
    try:
        failed_executor.execute(
            dispatch_binding_id=dispatch.binding_id,
            configuration=configuration,
        )
    except CandidateWaveExecutionError as exc:
        assert 'implementation identity resolution failed' in str(exc)
    else:
        raise AssertionError('missing PFFDTD backend unexpectedly produced a result')
    assert set(fixture['authority_root'].glob('*.json')) == artifacts_before_failure
    assert _result_count(fixture['db_path']) == results_before_failure

    return {
        'ready_dispatch_only': True,
        'blocked_dispatch_rejected': True,
        'missing_wave_excitation_rejected': True,
        'deterministic_input_identity': True,
        'unsupported_observable_rejected': True,
        'unsupported_band_rejected': True,
        'pre_backend_cancellation_no_artifact': True,
        'backend_failure_no_fake_artifact_or_result': True,
    }


def _verify_reopen_and_tamper(
    fixture: dict[str, object],
    result,
) -> dict[str, bool]:
    db_path = fixture['db_path']
    store = fixture['store']

    reopened_scene = SceneRepository(db_path)
    reopened_variant = CadSystemVariantRepository(reopened_scene)
    reopened_equipment = CadEquipmentRepository(
        reopened_scene,
        reopened_variant,
    )
    reopened_r110 = CadR110SourceRepository(
        reopened_scene,
        variant_repository=reopened_variant,
        equipment_repository=reopened_equipment,
    )
    reopened_wave = CadWaveExcitationRepository(
        reopened_scene,
        equipment_repository=reopened_equipment,
        r110_repository=reopened_r110,
    )
    reopened_r120 = R120GeometryCompilerRepository(reopened_scene)
    reopened_snapshot = CadAcousticSnapshotRepository(
        reopened_scene,
        variant_repository=reopened_variant,
        r110_repository=reopened_r110,
        r120_repository=reopened_r120,
        wave_excitation_repository=reopened_wave,
    )
    reopened_dispatch = CadAcousticSolverDispatchRepository(
        reopened_scene,
        snapshot_repository=reopened_snapshot,
        external_authority_resolver=store.resolve,
    )
    reopened_result = CadAcousticSolverResultRepository(
        reopened_scene,
        dispatch_resolver=reopened_dispatch,
        request_resolver=reopened_snapshot,
        external_authority_resolver=store.resolve,
    )
    assert reopened_result.get(result.result_id) == result

    artifact_ref = result.artifacts[0].artifact_authority
    artifact_path = store.path_for(artifact_ref)
    original = artifact_path.read_bytes()

    artifact_path.unlink()
    try:
        reopened_result.get(result.result_id)
    except ValueError:
        pass
    else:
        raise AssertionError('missing result artifact did not fail closed')
    artifact_path.write_bytes(original)
    assert reopened_result.get(result.result_id) == result

    artifact_path.write_text('{}\n', encoding='utf-8')
    try:
        reopened_result.get(result.result_id)
    except ValueError:
        pass
    else:
        raise AssertionError('modified result artifact did not fail closed')
    artifact_path.write_bytes(original)
    assert reopened_result.get(result.result_id) == result

    return {
        'save_reopen_exact_reresolution': True,
        'missing_artifact_fail_closed': True,
        'modified_artifact_fail_closed': True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Run the bounded R130A PFFDTD candidate wave vertical slice'
    )
    parser.add_argument('--upstream-root', required=True, type=Path)
    parser.add_argument('--work-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.work_root.exists():
        shutil.rmtree(args.work_root)
    args.work_root.mkdir(parents=True)
    status = 'PASS'
    payload: dict[str, object] = {
        'schema_version': 'htdt.r130a.candidate-wave-execution-evidence-1',
        'fixture_id': FIXTURE_ID,
        'candidate_backend': 'PFFDTD Python/Numba CPU',
        'candidate_source_commit_sha': PFFDTD_SHA,
        'production_solver_selected': False,
        'r100b_completed': False,
        'r130a_numerical_acceptance_completed': False,
        'owned_room_evidence': False,
        'rdc_calls': 0,
    }
    try:
        fixture = _fixture(args.work_root, args.upstream_root)
        preflight = _assert_preflight_gates(fixture)
        authority, _ = fixture['executor'].compile_input(
            dispatch_binding_id=fixture['dispatch'].binding_id,
            configuration=fixture['configuration'],
        )
        result = fixture['executor'].execute(
            dispatch_binding_id=fixture['dispatch'].binding_id,
            configuration=fixture['configuration'],
        )
        artifact_payload = fixture['store'].read_payload(
            result.artifacts[0].artifact_authority
        )
        provenance_payload = fixture['store'].read_payload(
            result.execution_provenance_ref
        )
        reopen = _verify_reopen_and_tamper(fixture, result)
        payload.update(
            {
                'status': status,
                'preflight': preflight,
                'reopen': reopen,
                'snapshot_id': fixture['snapshot'].snapshot_id,
                'snapshot_sha256': fixture['snapshot'].semantic_sha256,
                'prediction_request_id': fixture['request'].request_id,
                'dispatch_binding_id': fixture['dispatch'].binding_id,
                'dispatch_state': fixture['dispatch'].state,
                'candidate_execution_input_id': authority.execution_input_id,
                'candidate_execution_input_sha256': authority.semantic_sha256,
                'compiled_geometry_sha256': authority.compiled_geometry_sha256,
                'r110_compiled_source_sha256': (
                    authority.r110_compiled_source_sha256
                ),
                'wave_excitation_binding_sha256': (
                    authority.wave_excitation_binding_sha256
                ),
                'wave_excitation_sha256': authority.wave_excitation_sha256,
                'solver_configuration_sha256': (
                    fixture['configuration'].semantic_sha256
                ),
                'result_id': result.result_id,
                'result_sha256': result.semantic_sha256,
                'result_artifact_ref': (
                    result.artifacts[0].artifact_authority.model_dump(
                        mode='json'
                    )
                ),
                'execution_provenance_ref': (
                    result.execution_provenance_ref.model_dump(mode='json')
                ),
                'raw_solver_asset_sha256': (
                    artifact_payload['solver_raw_asset']['sha256']
                ),
                'receiver_identity_order': (
                    artifact_payload['receiver_identity_order']
                ),
                'frequency_axis_hz': artifact_payload['frequency_axis_hz'],
                'pressure_real_pa': artifact_payload['pressure_real_pa'],
                'pressure_imag_pa': artifact_payload['pressure_imag_pa'],
                'runtime_identity': provenance_payload['runtime_identity'],
                'resource_configuration': (
                    provenance_payload['resource_configuration']
                ),
                'timings_s': provenance_payload['timings_s'],
            }
        )
    except Exception as exc:
        status = 'FAIL'
        payload.update(
            {
                'status': status,
                'error': f'{type(exc).__name__}: {exc}',
                'traceback': traceback.format_exc(),
            }
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if status == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
