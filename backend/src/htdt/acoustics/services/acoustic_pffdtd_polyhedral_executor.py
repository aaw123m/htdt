
import math
from ...canonical_json import canonical_sha256 as _digest
from ...r120_geometry_compiler import ExactExternalAuthorityRef
from ..domain.acoustic_pffdtd_polyhedral_geometry import (
    PffdtdPolyhedralGeometryRepresentation,
    _exact_ref,
    _read_executed_grid,
    compile_r120b_polyhedral_to_pffdtd,
    compiled_geometry_ref,
    load_r120b_polyhedral_authorities,
    r130d_snapshot_geometry_identity_prefix,
    semantic_geometry_ref,
)
from ..services.cad_candidate_wave_execution import (
    COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
    CandidateBoundaryBinding,
    CandidatePolyhedralGeometryBinding,
    CandidateWaveExecutionError,
    CandidateWaveExecutionInput,
    PFFDTD_CANDIDATE_POLYHEDRAL_INPUT_AUTHORITY_VERSION,
    PffdtdCandidateConfiguration,
    PffdtdCandidateWaveExecutor,
)
from ..domain.cad_acoustic_solver_result import (
    AcousticSolverObservableArtifact,
    AcousticSolverResultEnvelope,
    build_acoustic_solver_result_envelope,
)
from ..domain.cad_pffdtd_resource_estimator import PffdtdCandidateResourceEstimator
from datetime import (
    datetime,
    timezone,
)
from hashlib import sha256
from typing import Any
from uuid import uuid4

class PffdtdPolyhedralCandidateWaveExecutor:
    """R130D exact-polyhedron lane over the established R130 CPU candidate."""

    def __init__(
        self,
        *,
        base_executor: PffdtdCandidateWaveExecutor,
        containment_tolerance_m: float = 1.0e-9,
    ) -> None:
        if containment_tolerance_m <= 0.0:
            raise ValueError('R130D containment tolerance must be positive')
        self.base_executor = base_executor
        self.containment_tolerance_m = float(containment_tolerance_m)

    def compile_input(
        self,
        *,
        dispatch_binding_id: str,
        configuration: PffdtdCandidateConfiguration,
        semantic_geometry_ref: ExactExternalAuthorityRef,
        compiled_geometry_ref: ExactExternalAuthorityRef,
        rigid_boundary_physics_ref: ExactExternalAuthorityRef,
    ) -> tuple[
        CandidateWaveExecutionInput,
        dict[str, Any],
        PffdtdPolyhedralGeometryRepresentation,
    ]:
        base_authority, base_model = self.base_executor.compile_input(
            dispatch_binding_id=dispatch_binding_id,
            configuration=configuration,
        )
        if any(
            item.impedance_mapping is not None or item.causal_mapping is not None
            for item in base_authority.boundary_bindings
        ):
            raise CandidateWaveExecutionError(
                'R130D initial polyhedral slice supports rigid boundary only'
            )

        semantic, compiled = load_r120b_polyhedral_authorities(
            self.base_executor.authority_store,
            semantic_ref=semantic_geometry_ref,
            compiled_ref=compiled_geometry_ref,
        )
        expected_prefix = r130d_snapshot_geometry_identity_prefix(
            base_authority.snapshot_id,
            base_authority.snapshot_sha256,
        )
        if not semantic.source_geometry_identity.startswith(expected_prefix):
            raise CandidateWaveExecutionError(
                'R120B semantic polyhedron is stale/unbound for this exact '
                'AcousticSceneSnapshot'
            )

        boundary_payload = self.base_executor._require_external(
            rigid_boundary_physics_ref,
            label='R130D rigid boundary physics',
        )
        if (
            not isinstance(boundary_payload, dict)
            or boundary_payload.get('authority_kind') != 'wave_boundary_physics'
            or boundary_payload.get('model') != 'rigid_zero_normal_velocity'
            or float(boundary_payload.get('normal_velocity_m_s', math.nan)) != 0.0
        ):
            raise CandidateWaveExecutionError(
                'R130D polyhedral slice requires exact rigid_zero_normal_velocity '
                'boundary authority'
            )

        if (
            len(base_model.get('sources', ())) != 1
            or len(base_authority.receivers) < 1
        ):
            raise CandidateWaveExecutionError(
                'R130D polyhedral candidate requires exactly one source and '
                'at least one receiver'
            )
        source_position = tuple(
            float(item) for item in base_model['sources'][0]['xyz']
        )
        receiver_positions = tuple(
            (item.receiver_id, tuple(float(value) for value in item.position_m))
            for item in base_authority.receivers
        )
        snapshot = self.base_executor.snapshot_repository.get_snapshot(
            base_authority.snapshot_id
        )
        if (
            snapshot is None
            or snapshot.semantic_sha256 != base_authority.snapshot_sha256
            or snapshot.environment is None
            or snapshot.environment.sound_speed_m_s is None
        ):
            raise CandidateWaveExecutionError(
                'R130D exact snapshot/environment authority disappeared or changed'
            )

        representation, model = compile_r120b_polyhedral_to_pffdtd(
            semantic=semantic,
            compiled=compiled,
            source_position_m=source_position,
            receivers=receiver_positions,
            sound_speed_m_s=float(snapshot.environment.sound_speed_m_s),
            configuration=configuration,
            containment_tolerance_m=self.containment_tolerance_m,
            topology_tolerance_m=self.containment_tolerance_m,
            source_name=base_authority.source_entity_id,
        )
        self.base_executor.authority_store.put_exact_json(
            representation.as_external_ref(),
            representation.semantic_payload(),
        )

        boundary_bindings: list[CandidateBoundaryBinding] = []
        for mapping in sorted(
            compiled.surface_mapping,
            key=lambda value: (value.source_surface_key, value.source_surface_id),
        ):
            material_ref = _exact_ref(
                authority_id=mapping.material_authority.authority_id,
                authority_version=mapping.material_authority.authority_version,
                semantic_hash_sha256=mapping.material_authority.semantic_hash_sha256,
            )
            self.base_executor._require_external(
                material_ref,
                label=f'R130D material {mapping.source_surface_id}',
            )
            boundary_bindings.append(
                CandidateBoundaryBinding(
                    source_surface_id=mapping.source_surface_id,
                    material_authority=material_ref,
                    boundary_physics_authority=rigid_boundary_physics_ref,
                )
            )

        treatment_hash = _digest([])
        boundary_hash = _digest(
            [
                item.model_dump(mode='json', exclude_none=True)
                for item in boundary_bindings
            ]
        )
        binding = CandidatePolyhedralGeometryBinding(
            scene_revision_id=snapshot.scene_revision_id,
            scene_revision_content_hash=snapshot.scene_content_hash,
            snapshot_id=snapshot.snapshot_id,
            snapshot_sha256=snapshot.semantic_sha256,
            semantic_geometry_ref=semantic_geometry_ref,
            compiled_geometry_ref=compiled_geometry_ref,
            solver_geometry_ref=representation.as_external_ref(),
            exact_topology_report_id=compiled.exact_topology_report_id,
            exact_topology_report_hash_sha256=(
                compiled.exact_topology_report_hash_sha256
            ),
            topology_identity_sha256=compiled.topology_identity_sha256,
            topology_tolerance_m=representation.topology_tolerance_m,
            region_id=representation.region_id,
            containment_algorithm_id=representation.containment_algorithm_id,
            containment_algorithm_version=(
                representation.containment_algorithm_version
            ),
            containment_tolerance_m=representation.containment_tolerance_m,
            grid_algorithm_id=representation.grid_algorithm_id,
            grid_algorithm_version=representation.grid_algorithm_version,
            grid_origin_m=representation.grid_origin_m,
            grid_spacing_m=representation.grid_spacing_m,
            grid_dimensions=representation.grid_dimensions,
            grid_geometry_sha256=representation.generated_geometry_sha256,
        )

        core = base_authority.semantic_payload()
        core.update(
            {
                'authority_version': (
                    PFFDTD_CANDIDATE_POLYHEDRAL_INPUT_AUTHORITY_VERSION
                ),
                'compiled_geometry_id': compiled.compiled_geometry_id,
                'compiled_geometry_sha256': compiled.compiled_hash_sha256,
                'compiled_topology_sha256': compiled.topology_identity_sha256,
                'material_boundary_configuration_sha256': boundary_hash,
                'boundary_bindings': [
                    item.model_dump(mode='json', exclude_none=True)
                    for item in boundary_bindings
                ],
                'treatment_boundary_composition_sha256': treatment_hash,
                'adapter_compiler_id': (
                    'htdt.r130d.pffdtd_polyhedral_input_compiler'
                ),
                'adapter_compiler_version': '4',
                'solver_model_sha256': representation.solver_model_sha256,
                'polyhedral_geometry_binding': binding.model_dump(mode='json'),
            }
        )
        digest = _digest(core)
        authority = CandidateWaveExecutionInput(
            execution_input_id=f'candidate-wave-input:{digest}',
            semantic_sha256=digest,
            **core,
        )
        return authority, model, representation

    def execute(
        self,
        *,
        dispatch_binding_id: str,
        configuration: PffdtdCandidateConfiguration,
        semantic_geometry_ref: ExactExternalAuthorityRef,
        compiled_geometry_ref: ExactExternalAuthorityRef,
        rigid_boundary_physics_ref: ExactExternalAuthorityRef,
    ) -> AcousticSolverResultEnvelope:
        authority, model, representation = self.compile_input(
            dispatch_binding_id=dispatch_binding_id,
            configuration=configuration,
            semantic_geometry_ref=semantic_geometry_ref,
            compiled_geometry_ref=compiled_geometry_ref,
            rigid_boundary_physics_ref=rigid_boundary_physics_ref,
        )
        snapshot = self.base_executor.snapshot_repository.get_snapshot(
            authority.snapshot_id
        )
        request = self.base_executor.snapshot_repository.get_prediction_request(
            authority.prediction_request_id,
            _validated_snapshot=snapshot,
        )
        dispatch = self.base_executor.dispatch_repository.get_dispatch(
            authority.dispatch_binding_id,
            _validated_snapshot=snapshot,
            _validated_request=request,
        )
        if snapshot is None or request is None or dispatch is None:
            raise CandidateWaveExecutionError(
                'R130D exact snapshot/request/dispatch chain disappeared'
            )
        if (
            snapshot.semantic_sha256 != authority.snapshot_sha256
            or dispatch.semantic_sha256 != authority.dispatch_binding_sha256
            or request.request_semantic_sha256
            != authority.prediction_request_sha256
        ):
            raise CandidateWaveExecutionError(
                'R130D exact snapshot/request/dispatch chain became stale'
            )
        assert snapshot.environment is not None
        assert snapshot.environment.sound_speed_m_s is not None

        estimation = PffdtdCandidateResourceEstimator().estimate(
            authority=authority,
            model=model,
            configuration=configuration,
            sound_speed_m_s=float(snapshot.environment.sound_speed_m_s),
        )
        workload = estimation.workload
        workload_ref = _exact_ref(
            authority_id=workload.workload_estimate_id,
            authority_version=workload.authority_version,
            semantic_hash_sha256=workload.semantic_sha256,
        )
        self.base_executor.authority_store.put_exact_json(
            workload_ref,
            workload.semantic_payload(),
        )

        wave_binding = self.base_executor.wave_excitation_repository.get_binding(
            authority.wave_excitation_binding_id
        )
        if wave_binding is None:
            raise CandidateWaveExecutionError(
                'R130D exact wave excitation binding disappeared'
            )
        excitation = self.base_executor.wave_excitation_repository.get_excitation(
            wave_binding.excitation_id
        )
        if excitation is None:
            raise CandidateWaveExecutionError(
                'R130D exact wave excitation authority disappeared'
            )

        numerical = self.base_executor._run_pffdtd(
            authority=authority,
            model=model,
            configuration=configuration,
            excitation=excitation,
            sound_speed_m_s=float(snapshot.environment.sound_speed_m_s),
            cancel_check=lambda: False,
        )
        executed_grid = _read_executed_grid(
            run_dir=self.base_executor.work_root / authority.semantic_sha256,
            representation=representation,
        )
        if numerical.grid_shape != executed_grid.dimensions:
            raise CandidateWaveExecutionError(
                'R130D numerical grid shape differs from exact executed-grid authority'
            )
        self.base_executor.authority_store.put_exact_json(
            executed_grid.as_external_ref(),
            executed_grid.semantic_payload(),
        )

        schema_payload = self.base_executor._require_external(
            self.base_executor.output_schema_ref,
            label='complex pressure artifact schema',
        )
        if (
            not isinstance(schema_payload, dict)
            or schema_payload.get('schema_version')
            != COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION
            or schema_payload.get('quantity_type') != 'complex_pressure'
        ):
            raise CandidateWaveExecutionError(
                'R130D complex-pressure artifact schema authority is incompatible'
            )

        execution_id = (
            f'r130d-candidate-polyhedral:{authority.semantic_sha256[:20]}:'
            f'{uuid4().hex}'
        )
        artifact_payload = {
            'schema_version': COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
            'quantity_type': 'complex_pressure',
            'complex_representation': {
                'form': 'cartesian_real_imag',
                'phasor_convention': 'exp(-i*omega*t)',
                'analysis_fourier_kernel': 'exp(+i*omega*t)',
            },
            'receiver_identity_order': [
                {
                    'receiver_id': item.receiver_id,
                    'entity_id': item.entity_id,
                    'position_m': list(item.position_m),
                }
                for item in authority.receivers
            ],
            'frequency_axis_hz': list(numerical.frequency_hz),
            'time_sampling': {
                'time_step_s': numerical.time_step_s,
                'sample_count': numerical.time_step_count,
                'finite_record_interval': '[0,T)',
                'requested_duration_s': authority.observation_time_s,
            },
            'units': 'Pa',
            'reference': (
                'absolute complex acoustic pressure from actual pinned PFFDTD '
                'CPU solve over exact R120B polyhedral triangle geometry'
            ),
            'valid_domain': request.requested_frequency_domain.model_dump(
                mode='json'
            ),
            'solver_execution_id': execution_id,
            'candidate_execution_input_id': authority.execution_input_id,
            'candidate_execution_input_sha256': authority.semantic_sha256,
            'polyhedral_geometry_binding': (
                authority.polyhedral_geometry_binding.model_dump(mode='json')
                if authority.polyhedral_geometry_binding is not None
                else None
            ),
            'solver_geometry_ref': representation.as_external_ref().model_dump(
                mode='json'
            ),
            'executed_grid_ref': executed_grid.as_external_ref().model_dump(
                mode='json'
            ),
            'resource_estimate_ref': workload_ref.model_dump(mode='json'),
            'source_authority': {
                'r110_compiled_source_sha256': (
                    authority.r110_compiled_source_sha256
                ),
                'wave_excitation_binding_sha256': (
                    authority.wave_excitation_binding_sha256
                ),
                'wave_excitation_sha256': authority.wave_excitation_sha256,
            },
            'solver_raw_asset': {
                'name': numerical.raw_solver_asset_name,
                'sha256': numerical.raw_solver_asset_sha256,
            },
            'pressure_real_pa': [
                list(row) for row in numerical.pressure_real_pa
            ],
            'pressure_imag_pa': [
                list(row) for row in numerical.pressure_imag_pa
            ],
        }
        artifact_ref = self.base_executor.authority_store.put_json(
            'r130d-acoustic-solver-artifact',
            COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
            artifact_payload,
        )

        provenance_payload = {
            'schema_version': (
                'htdt.r130d.polyhedral-candidate-execution-provenance-1'
            ),
            'execution_id': execution_id,
            'candidate_only': True,
            'production_solver_selected': False,
            'r130d_software_execution_completed': True,
            'r130d_general_3d_physics_validated': False,
            'r130_numerical_acceptance_completed': False,
            'scene_revision_id': snapshot.scene_revision_id,
            'scene_revision_content_hash': snapshot.scene_content_hash,
            'snapshot_id': snapshot.snapshot_id,
            'snapshot_sha256': snapshot.semantic_sha256,
            'execution_input_id': authority.execution_input_id,
            'execution_input_sha256': authority.semantic_sha256,
            'semantic_geometry_ref': semantic_geometry_ref.model_dump(
                mode='json'
            ),
            'compiled_geometry_ref': compiled_geometry_ref.model_dump(
                mode='json'
            ),
            'solver_geometry_ref': representation.as_external_ref().model_dump(
                mode='json'
            ),
            'executed_grid_ref': executed_grid.as_external_ref().model_dump(
                mode='json'
            ),
            'resource_estimate_ref': workload_ref.model_dump(mode='json'),
            'resource_estimate': workload.model_dump(mode='json'),
            'solver_implementation_ref': (
                dispatch.solver_implementation_ref.model_dump(mode='json')
            ),
            'solver_configuration_ref': (
                dispatch.solver_configuration_ref.model_dump(mode='json')
            ),
            'runtime_identity': authority.runtime_identity.model_dump(
                mode='json'
            ),
            'compiled_solver_model_sha256': authority.solver_model_sha256,
            'raw_solver_asset_sha256': numerical.raw_solver_asset_sha256,
            'grid_shape': list(numerical.grid_shape),
            'time_step_s': numerical.time_step_s,
            'time_step_count': numerical.time_step_count,
            'sound_speed_m_s': numerical.sound_speed_m_s,
            'timings_s': {
                'compile': numerical.compile_seconds,
                'solve': numerical.solve_seconds,
                'postprocess': numerical.postprocess_seconds,
            },
            'compatibility_patch': numerical.compatibility_patch,
        }
        provenance_ref = self.base_executor.authority_store.put_json(
            'solver-execution-provenance',
            'htdt.r130d.polyhedral-candidate-execution-provenance-1',
            provenance_payload,
        )

        artifact = AcousticSolverObservableArtifact(
            observable='complex_pressure',
            artifact_authority=artifact_ref,
            encoding_schema_ref=self.base_executor.output_schema_ref,
            valid_frequency_domain=request.requested_frequency_domain,
        )
        result = build_acoustic_solver_result_envelope(
            dispatch=dispatch,
            request=request,
            execution_id=execution_id,
            execution_provenance_ref=provenance_ref,
            artifacts=(artifact,),
            completed_at_utc=datetime.now(timezone.utc).isoformat(),
            artifact_manifest_resolver=(
                self.base_executor.authority_store.solver_artifact_manifest_resolver(
                    encoding_schema_ref=self.base_executor.output_schema_ref,
                )
            ),
        )
        saved = self.base_executor.result_repository.save(result)
        self.base_executor._persist_capability_manifest(
            dispatch,
            produced_observables=(
                item.observable for item in saved.artifacts
            ),
        )
        return saved

__all__ = [
    'PffdtdPolyhedralCandidateWaveExecutor',
]
