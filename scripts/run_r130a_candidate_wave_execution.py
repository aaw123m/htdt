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

from htdt.acoustic_benchmark import AcousticMaterial, SpecificImpedancePoint
from htdt.acoustic_pffdtd_causal_boundary import (
    PFFDTD_CAUSAL_BOUNDARY_MAPPING_VERSION,
    CausalAdmittanceBranch,
    build_causal_frequency_dependent_boundary_authority,
    pffdtd_causal_boundary_mapping_authority_payload,
)
from htdt.acoustic_pffdtd_impedance_adapter import (
    PFFDTD_IMPEDANCE_MAPPING_ID,
    PFFDTD_IMPEDANCE_MAPPING_VERSION,
    pffdtd_impedance_mapping_authority_payload,
)
from htdt.cad_acoustic_snapshot import (
    SnapshotEnvironmentAuthorityRef,
    build_acoustic_prediction_request,
    build_acoustic_scene_snapshot,
    receiver_binding_from_scene,
)
from htdt.cad_acoustic_snapshot_repository import (
    AcousticSnapshotAuthorityResolvers,
    CadAcousticSnapshotRepository,
)
from htdt.cad_acoustic_solver_adapter import (
    AcousticNumericalFidelityPolicy,
    bind_prediction_request_to_solver_adapter,
    build_acoustic_solver_adapter_descriptor,
)
from htdt.cad_acoustic_solver_dispatch_repository import (
    CadAcousticSolverDispatchRepository,
)
from htdt.cad_acoustic_solver_result import CadAcousticSolverResultRepository
from htdt.cad_solver_capability_manifest import (
    build_solver_capability_manifest,
    derive_solver_capability_rows,
)
from htdt.cad_candidate_wave_execution import (
    COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
    PFFDTD_CANDIDATE_ADAPTER_ID,
    PFFDTD_CANDIDATE_ADAPTER_VERSION,
    PFFDTD_CANDIDATE_IMPEDANCE_ADAPTER_VERSION,
    PFFDTD_CANDIDATE_CAUSAL_ADAPTER_VERSION,
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
from htdt.cad_equipment_evidence import build_equipment_manual_evidence
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_r110_source import compile_r110_source_model
from htdt.cad_r110_source_repository import CadR110SourceRepository
from htdt.cad_repository import SceneRepository
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
from htdt.cad_wave_excitation import (
    CadWaveExcitationRepository,
    ComplexVolumeVelocitySample,
    WaveExcitationEvidenceSubject,
    WaveExcitationManualDerivation,
    bind_wave_excitation_to_r110_source,
    build_acoustic_wave_excitation_authority,
    build_wave_excitation_evidence_authority,
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


def _semantic_geometry(
    boundary_mode: str = 'rigid',
    *,
    fixture_id: str = FIXTURE_ID,
):
    mesh = import_raw_visual_mesh(
        CUBE_OBJ,
        source_name=f'{fixture_id}.obj',
    )
    triangle_ids = raw_triangle_ids(mesh)
    if boundary_mode == 'rigid':
        assignments = (
            SurfaceSemanticAssignment(
                surface_key='closed-room-shell',
                triangle_ids=triangle_ids,
                semantic_class='room_boundary',
            ),
        )
    elif boundary_mode in {'impedance', 'causal'}:
        assignments = (
            SurfaceSemanticAssignment(
                surface_key='closed-room-shell-rigid',
                triangle_ids=triangle_ids[:-2],
                semantic_class='room_boundary',
            ),
            SurfaceSemanticAssignment(
                surface_key=(
                    'normal-incidence-impedance-wall'
                    if boundary_mode == 'impedance'
                    else 'normal-incidence-causal-wall'
                ),
                triangle_ids=triangle_ids[-2:],
                semantic_class='room_boundary',
            ),
        )
    else:
        raise ValueError(f'unsupported boundary mode: {boundary_mode}')

    request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=None,
        source_to_scene_transform=explicit_identity_source_to_scene_transform(
            reason='candidate fixture OBJ is authored directly in exact HTDT metres',
        ),
        surface_assignments=assignments,
    )
    return convert_raw_visual_mesh_to_semantic_geometry(mesh, request)


def _scene(
    boundary_mode: str = 'rigid',
    *,
    fixture_id: str = FIXTURE_ID,
    room: RoomPrism | None = None,
) -> SceneDocument:
    return SceneDocument(
        document_id=fixture_id,
        schema_version=4,
        room=room,
        r120_semantic_geometry=_semantic_geometry(
            boundary_mode,
            fixture_id=fixture_id,
        ),
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
    *,
    boundary_mode: str = 'rigid',
    fixture_id: str = FIXTURE_ID,
    room: RoomPrism | None = None,
):
    db_path = root / 'candidate.sqlite3'
    authority_root = root / 'authorities'
    work_root = root / 'work'
    store = ExactJsonAuthorityStore(authority_root)

    scene_repository = SceneRepository(db_path)
    if boundary_mode not in {'rigid', 'impedance', 'causal'}:
        raise ValueError(f'unsupported boundary mode: {boundary_mode}')
    revision = scene_repository.save(
        _scene(boundary_mode, fixture_id=fixture_id, room=room),
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
            {'fixture_id': fixture_id, 'kind': 'equipment-identity'}
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
    for _evidence in build_equipment_manual_evidence(
        equipment,
        actor='r130a-candidate-fixture',
        recorded_at_utc=NOW,
        citation=equipment_provenance.source_reference,
    ):
        equipment_repository.save_evidence(_evidence)
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

    excitation_samples = (
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
    )
    excitation_evidence = build_wave_excitation_evidence_authority(
        evidence_kind='user_defined',
        source_name='HTDT R130A explicit acoustic wave excitation',
        source_version='1',
        source_reference='synthetic candidate fixture; not owned-room evidence',
        derivation=WaveExcitationManualDerivation(
            author='HTDT R130A candidate fixture',
            authored_at_utc=NOW,
        ),
        subject=WaveExcitationEvidenceSubject(
            definition_id=equipment.definition_id,
            definition_version=equipment.version,
            definition_sha256=equipment.semantic_sha256,
            samples=excitation_samples,
        ),
    )
    excitation_provenance = excitation_evidence.provenance
    excitation = build_acoustic_wave_excitation_authority(
        definition_id=equipment.definition_id,
        definition_version=equipment.version,
        definition_sha256=equipment.semantic_sha256,
        samples=excitation_samples,
        interpolation=InterpolationProvenance(
            method='linear',
            implementation='fixture-exact-sample-linear',
            implementation_version='1',
            provenance=excitation_provenance,
        ),
        provenance=(excitation_provenance,),
        evidence=(excitation_evidence,),
        approximation_note=(
            'No sensitivity conversion is used. Candidate execution consumes '
            'the two explicit complex volume-velocity samples directly.'
        ),
    )
    wave_repository.save_evidence(excitation_evidence)
    wave_repository.save_excitation(excitation)
    wave_binding = bind_wave_excitation_to_r110_source(
        source=source,
        excitation=excitation,
    )
    wave_repository.save_binding(wave_binding)

    geometry = revision.document.r120_semantic_geometry
    assert geometry is not None

    sound_speed_m_s = 343.2 if boundary_mode == 'rigid' else 343.0
    if boundary_mode in {'impedance', 'causal'}:
        boundary_prefix = 'r130b' if boundary_mode == 'impedance' else 'r130c'
        sound_speed_ref = store.put_json(
            f'{boundary_prefix}-fixture-sound-speed',
            '1',
            {
                'quantity': 'sound_speed_m_s',
                'value': sound_speed_m_s,
                'fixture_id': fixture_id,
                'candidate_only': True,
            },
        )
        density_ref = store.put_json(
            f'{boundary_prefix}-fixture-density',
            '1',
            {
                'quantity': 'air_density_kg_m3',
                'value': 1.2,
                'fixture_id': fixture_id,
                'candidate_only': True,
            },
        )

    if boundary_mode == 'rigid':
        assert len(geometry.surfaces) == 1
        surface_id = geometry.surfaces[0].surface_id
        material_ref = store.put_json(
            'r130a-fixture-material',
            '1',
            {
                'authority_kind': 'acoustic_material',
                'fixture_id': fixture_id,
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
                'fixture_id': fixture_id,
                'candidate_only': True,
            },
        )
        surface_boundary_bindings = (
            SurfaceBoundaryAuthorityBinding(
                source_surface_id=surface_id,
                material_authority=material_ref,
                boundary_physics_authority=boundary_ref,
            ),
        )
        region_surface_ids = (surface_id,)
        impedance_material_ref = None
        impedance_boundary_ref = None
        impedance_mapping_ref = None
        causal_material_ref = None
        causal_boundary_ref = None
        causal_mapping_ref = None
        causal_authority = None
    elif boundary_mode == 'impedance':
        assert len(geometry.surfaces) == 2
        surface_by_key = {
            surface.surface_key: surface for surface in geometry.surfaces
        }
        rigid_surface = surface_by_key['closed-room-shell-rigid']
        impedance_surface = surface_by_key['normal-incidence-impedance-wall']

        rigid_material_ref = store.put_json(
            'r130b-fixture-rigid-material',
            '1',
            {
                'authority_kind': 'acoustic_material',
                'fixture_id': fixture_id,
                'name': 'candidate rigid companion material',
                'candidate_only': True,
            },
        )
        rigid_boundary_ref = store.put_json(
            'r130b-fixture-rigid-wave-boundary',
            '1',
            {
                'authority_kind': 'wave_boundary_physics',
                'model': 'rigid_zero_normal_velocity',
                'normal_velocity_m_s': 0.0,
                'fixture_id': fixture_id,
                'candidate_only': True,
            },
        )
        exact_impedance = 2.0 * 1.2 * sound_speed_m_s
        impedance_material = AcousticMaterial(
            material_id='z-2z0',
            provenance=(
                'R100B wave-normal-incidence-impedance-v1 explicit analytic '
                'specific-impedance authority; no scalar absorption conversion'
            ),
            version='1',
            wave_model='specific_impedance_table',
            specific_impedance=(
                SpecificImpedancePoint(
                    frequency_hz=40.0,
                    resistance_pa_s_m=exact_impedance,
                    reactance_pa_s_m=0.0,
                ),
                SpecificImpedancePoint(
                    frequency_hz=80.0,
                    resistance_pa_s_m=exact_impedance,
                    reactance_pa_s_m=0.0,
                ),
            ),
        )
        impedance_material_ref = store.put_json(
            'r130b-fixture-impedance-material',
            '1',
            impedance_material.model_dump(mode='json'),
        )
        mapping_payload = pffdtd_impedance_mapping_authority_payload()
        impedance_mapping_ref = store.put_json(
            'r130b-pffdtd-impedance-mapping',
            PFFDTD_IMPEDANCE_MAPPING_VERSION,
            mapping_payload,
        )
        impedance_boundary_ref = store.put_json(
            'r130b-fixture-wave-boundary',
            '1',
            {
                'authority_kind': 'wave_boundary_physics',
                'model': 'specific_impedance_table',
                'physical_quantity_type': 'specific_acoustic_impedance',
                'unit': 'Pa*s/m',
                'complex_capability': 'explicit_resistance_reactance',
                'valid_frequency_domain': {
                    'minimum_hz': 40.0,
                    'maximum_hz': 80.0,
                },
                'material_authority_ref': (
                    impedance_material_ref.model_dump(mode='json')
                ),
                'density_authority_ref': density_ref.model_dump(mode='json'),
                'sound_speed_authority_ref': (
                    sound_speed_ref.model_dump(mode='json')
                ),
                'pffdtd_mapping_authority_ref': (
                    impedance_mapping_ref.model_dump(mode='json')
                ),
                'provenance': {
                    'basis': 'analytic_model',
                    'source_fixture_id': 'wave-normal-incidence-impedance-v1',
                    'source_gate': 'R100B PR #151',
                },
            },
        )
        surface_boundary_bindings = (
            SurfaceBoundaryAuthorityBinding(
                source_surface_id=rigid_surface.surface_id,
                material_authority=rigid_material_ref,
                boundary_physics_authority=rigid_boundary_ref,
            ),
            SurfaceBoundaryAuthorityBinding(
                source_surface_id=impedance_surface.surface_id,
                material_authority=impedance_material_ref,
                boundary_physics_authority=impedance_boundary_ref,
            ),
        )
        region_surface_ids = (
            rigid_surface.surface_id,
            impedance_surface.surface_id,
        )
        causal_material_ref = None
        causal_boundary_ref = None
        causal_mapping_ref = None
        causal_authority = None
    else:
        assert boundary_mode == 'causal'
        assert len(geometry.surfaces) == 2
        surface_by_key = {
            surface.surface_key: surface for surface in geometry.surfaces
        }
        rigid_surface = surface_by_key['closed-room-shell-rigid']
        causal_surface = surface_by_key['normal-incidence-causal-wall']

        rigid_material_ref = store.put_json(
            'r130c-fixture-rigid-material',
            '1',
            {
                'authority_kind': 'acoustic_material',
                'fixture_id': fixture_id,
                'name': 'candidate rigid companion material',
                'candidate_only': True,
            },
        )
        rigid_boundary_ref = store.put_json(
            'r130c-fixture-rigid-wave-boundary',
            '1',
            {
                'authority_kind': 'wave_boundary_physics',
                'model': 'rigid_zero_normal_velocity',
                'normal_velocity_m_s': 0.0,
                'fixture_id': fixture_id,
                'candidate_only': True,
            },
        )
        causal_authority = build_causal_frequency_dependent_boundary_authority(
            source_scene_revision_id=revision.revision_id,
            source_scene_content_hash=revision.content_hash,
            source_surface_id=causal_surface.surface_id,
            material_id='r130c-analytic-series-rlc',
            material_version='1',
            valid_frequency_domain=FrequencyDomain(
                minimum_hz=40.0,
                maximum_hz=80.0,
            ),
            branches=(
                CausalAdmittanceBranch(
                    d_seconds=8.0e-4,
                    e_dimensionless=1.5,
                    f_per_second=120.0,
                ),
            ),
            evidence_state='analytic',
            provenance={
                'basis': 'closed_form_positive_real_series_RLC',
                'fixture_id': fixture_id,
                'candidate_only': True,
                'scalar_absorption_conversion': False,
            },
            uncertainty=None,
        )
        causal_material_ref = store.put_exact_json(
            causal_authority.as_external_ref(),
            causal_authority.semantic_payload(),
        )
        causal_mapping_ref = store.put_json(
            'r130c-pffdtd-causal-boundary-mapping',
            PFFDTD_CAUSAL_BOUNDARY_MAPPING_VERSION,
            pffdtd_causal_boundary_mapping_authority_payload(),
        )
        causal_boundary_ref = store.put_json(
            'r130c-fixture-wave-boundary',
            '1',
            {
                'authority_kind': 'wave_boundary_physics',
                'model': 'causal_specific_admittance_def',
                'physical_quantity_type': 'specific_acoustic_admittance',
                'unit': 'm/(Pa*s)',
                'normalization': 'Yn=rho*c*Y_specific',
                'representation': 'parallel_series_RLC_normalized_DEF',
                'interpolation_semantics': (
                    'analytic_rational_evaluation_no_interpolation'
                ),
                'extrapolation_rule': 'forbidden',
                'valid_frequency_domain': {
                    'minimum_hz': 40.0,
                    'maximum_hz': 80.0,
                },
                'causal_boundary_authority_ref': (
                    causal_material_ref.model_dump(mode='json')
                ),
                'density_authority_ref': density_ref.model_dump(mode='json'),
                'sound_speed_authority_ref': (
                    sound_speed_ref.model_dump(mode='json')
                ),
                'pffdtd_mapping_authority_ref': (
                    causal_mapping_ref.model_dump(mode='json')
                ),
                'provenance': {
                    'basis': 'analytic_positive_real_DEF',
                    'candidate_only': True,
                },
            },
        )
        surface_boundary_bindings = (
            SurfaceBoundaryAuthorityBinding(
                source_surface_id=rigid_surface.surface_id,
                material_authority=rigid_material_ref,
                boundary_physics_authority=rigid_boundary_ref,
            ),
            SurfaceBoundaryAuthorityBinding(
                source_surface_id=causal_surface.surface_id,
                material_authority=causal_material_ref,
                boundary_physics_authority=causal_boundary_ref,
            ),
        )
        region_surface_ids = (
            rigid_surface.surface_id,
            causal_surface.surface_id,
        )
        impedance_material_ref = None
        impedance_boundary_ref = None
        impedance_mapping_ref = None

    region = make_acoustic_region_authority(
        (
            AcousticRegionDeclaration(
                region_id='room-air',
                boundary_surface_ids=region_surface_ids,
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
        surface_boundary_bindings=surface_boundary_bindings,
        region_authority=region,
        portal_authority=portals,
        boundary_termination_authority=terminations,
    )
    r120_repository.save_compiled_geometry(
        compiled,
        surface_boundary_bindings=surface_boundary_bindings,
        region_authority=region,
        portal_authority=portals,
        boundary_termination_authority=terminations,
    )
    assert compiled.readiness.wave_geometry_ready

    if boundary_mode == 'rigid':
        sound_speed_ref = store.put_json(
            'r130a-fixture-sound-speed',
            '1',
            {
                'quantity': 'sound_speed_m_s',
                'value': sound_speed_m_s,
                'fixture_id': fixture_id,
                'candidate_only': True,
            },
        )

    temperature_ref = store.put_json(
        'r130a-fixture-temperature',
        '1',
        {
            'quantity': 'temperature_c',
            'value': 20.0,
            'fixture_id': fixture_id,
            'candidate_only': True,
        },
    )
    environment_ref = store.put_json(
        'r130a-fixture-environment',
        '1',
        {
            'fixture_id': fixture_id,
            'sound_speed_m_s': sound_speed_m_s,
            'temperature_c': 20.0,
            'candidate_only': True,
        },
    )
    environment = SnapshotEnvironmentAuthorityRef(
        authority=environment_ref,
        sound_speed_m_s=sound_speed_m_s,
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
            'fixture_id': fixture_id,
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

    role_id = {
        'rigid': 'r130a-candidate-wave',
        'impedance': 'r130b-candidate-impedance',
        'causal': 'r130c-candidate-causal-boundary',
    }[boundary_mode]
    fidelity_domain = FrequencyDomain(
        minimum_hz=min(band.minimum_hz, 40.0),
        maximum_hz=max(band.maximum_hz, 100.0),
    )
    fidelity_ref = store.put_json(
        {
            'rigid': 'r130a-candidate-fidelity-policy',
            'impedance': 'r130b-candidate-fidelity-policy',
            'causal': 'r130c-candidate-fidelity-policy',
        }[boundary_mode],
        '1',
        {
            # REV44: the payload carries the policy's own content so
            # authority lanes outside this process (e.g. the app's
            # PredictionAuthorityLane) can re-derive the typed model from
            # persisted bytes — claims-only payloads are unresolvable and
            # fail closed there. ``authority_ref`` itself stays out of the
            # payload: the content-addressed id already seals it.
            'acoustic_domain': 'wave',
            'model_solver_role_ids': [role_id],
            'supported_observables': ['complex_pressure'],
            'valid_frequency_domain': {
                'minimum_hz': fidelity_domain.minimum_hz,
                'maximum_hz': fidelity_domain.maximum_hz,
            },
            'parameter_bounds': {
                'max_grid_cells': 200000.0,
                'max_time_steps': 512.0,
            },
            'fixture_id': fixture_id,
            'purpose': 'bounded candidate execution only',
            'numerical_acceptance_claim': False,
            'production_adoption_claim': False,
        },
    )
    fidelity_policy = AcousticNumericalFidelityPolicy(
        authority_ref=fidelity_ref,
        acoustic_domain='wave',
        model_solver_role_ids=(role_id,),
        supported_observables=('complex_pressure',),
        valid_frequency_domain=fidelity_domain,
        parameter_bounds={
            'max_grid_cells': 200000.0,
            'max_time_steps': 512.0,
        },
    )

    def fidelity_policy_resolver(
        ref: ExactExternalAuthorityRef,
    ) -> AcousticNumericalFidelityPolicy | None:
        if ref != fidelity_policy.authority_ref:
            return None
        return (
            fidelity_policy
            if store.resolve(ref) is not None
            else None
        )

    def _resolved_payload(ref: ExactExternalAuthorityRef):
        if store.resolve(ref) is None:
            return None
        return store.read_payload(ref)

    def _resolve_scalar_authority(
        ref: ExactExternalAuthorityRef,
        *,
        quantity: str,
    ) -> float | None:
        payload = _resolved_payload(ref)
        if not isinstance(payload, dict) or payload.get('quantity') != quantity:
            return None
        return float(payload['value'])

    def _resolve_frequency_domain(
        ref: ExactExternalAuthorityRef,
    ) -> FrequencyDomain | None:
        payload = _resolved_payload(ref)
        if (
            not isinstance(payload, dict)
            or 'minimum_hz' not in payload
            or 'maximum_hz' not in payload
        ):
            return None
        return FrequencyDomain(
            minimum_hz=float(payload['minimum_hz']),
            maximum_hz=float(payload['maximum_hz']),
        )

    snapshot_authority_resolvers = AcousticSnapshotAuthorityResolvers(
        environment=lambda ref: (
            environment
            if ref == environment.authority
            and _resolved_payload(ref) is not None
            else None
        ),
        sound_speed_source=lambda ref: _resolve_scalar_authority(
            ref,
            quantity='sound_speed_m_s',
        ),
        temperature_source=lambda ref: _resolve_scalar_authority(
            ref,
            quantity='temperature_c',
        ),
        valid_frequency_domain=_resolve_frequency_domain,
        external_authority=store.resolve,
    )

    snapshot_repository = CadAcousticSnapshotRepository(
        scene_repository,
        variant_repository=variant_repository,
        r110_repository=r110_repository,
        r120_repository=r120_repository,
        wave_excitation_repository=wave_repository,
        fidelity_policy_resolver=fidelity_policy_resolver,
        authority_resolvers=snapshot_authority_resolvers,
    )
    snapshot_repository.save_snapshot(snapshot)

    request = build_acoustic_prediction_request(
        snapshot=snapshot,
        model_solver_role_id=role_id,
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
    if boundary_mode == 'rigid':
        density_ref = store.put_json(
            'r130a-fixture-density',
            '1',
            {
                'quantity': 'air_density_kg_m3',
                'value': 1.2,
                'fixture_id': fixture_id,
                'candidate_only': True,
            },
        )

    humidity_ref = store.put_json(
        'r130a-fixture-relative-humidity',
        '1',
        {
            'quantity': 'relative_humidity_percent',
            'value': 50.0,
            'fixture_id': fixture_id,
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
        adapter_version={
            'rigid': PFFDTD_CANDIDATE_ADAPTER_VERSION,
            'impedance': PFFDTD_CANDIDATE_IMPEDANCE_ADAPTER_VERSION,
            'causal': PFFDTD_CANDIDATE_CAUSAL_ADAPTER_VERSION,
        }[boundary_mode],
        model_solver_role_id=role_id,
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
        fidelity_policy_resolver=fidelity_policy_resolver,
    )
    dispatch_repository.save_descriptor(descriptor)
    # Runtime emit point (REV51): the descriptor's declared phenomenon
    # manifest is persisted alongside the descriptor authority itself.
    dispatch_repository.save_capability_manifest(
        build_solver_capability_manifest(
            descriptor=descriptor,
            rows=derive_solver_capability_rows(descriptor),
        )
    )
    dispatch = bind_prediction_request_to_solver_adapter(
        snapshot=snapshot,
        request=request,
        adapter=descriptor,
        solver_configuration_ref=configuration.as_external_ref(),
        numerical_fidelity_policy=fidelity_policy,
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
        artifact_manifest_resolver=store.solver_artifact_manifest_resolver(
            encoding_schema_ref=output_schema_ref,
        ),
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
        'fidelity_policy': fidelity_policy,
        'fidelity_policy_resolver': fidelity_policy_resolver,
        'snapshot_authority_resolvers': snapshot_authority_resolvers,
        'boundary_mode': boundary_mode,
        'impedance_material_ref': impedance_material_ref,
        'impedance_boundary_ref': impedance_boundary_ref,
        'impedance_mapping_ref': impedance_mapping_ref,
        'causal_material_ref': causal_material_ref,
        'causal_boundary_ref': causal_boundary_ref,
        'causal_mapping_ref': causal_mapping_ref,
        'causal_authority': causal_authority,
        'density_ref': density_ref,
        'sound_speed_ref': sound_speed_ref,
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
        numerical_fidelity_policy=fixture['fidelity_policy'],
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
        numerical_fidelity_policy=fixture['fidelity_policy'],
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
        fidelity_policy_resolver=fixture['fidelity_policy_resolver'],
        authority_resolvers=fixture['snapshot_authority_resolvers'],
    )
    reopened_dispatch = CadAcousticSolverDispatchRepository(
        reopened_scene,
        snapshot_repository=reopened_snapshot,
        external_authority_resolver=store.resolve,
        fidelity_policy_resolver=fixture['fidelity_policy_resolver'],
    )
    reopened_result = CadAcousticSolverResultRepository(
        reopened_scene,
        dispatch_resolver=reopened_dispatch,
        request_resolver=reopened_snapshot,
        external_authority_resolver=store.resolve,
        artifact_manifest_resolver=store.solver_artifact_manifest_resolver(
            encoding_schema_ref=fixture['output_schema_ref'],
        ),
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
