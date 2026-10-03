from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_geometric_acoustics_adapter import (
    CadDeterministicPathArtifactRepository,
)
from htdt.cad_stochastic_ray_receiver import (
    HTDT_STOCHASTIC_RAY_RECEIVER_IMPLEMENTATION_REF,
    STOCHASTIC_RECEIVER_ESTIMATE_OBSERVABLE,
    STOCHASTIC_RECEIVER_ESTIMATE_SCHEMA_REF,
    STOCHASTIC_RAY_ENGINE_ID,
    STOCHASTIC_RAY_ENGINE_VERSION,
    CadStochasticReceiverEstimateRepository,
    StochasticRayUnsupportedError,
    build_stochastic_ray_estimation_policy,
    build_stochastic_receiver_estimate_result_envelope,
    execute_stochastic_ray_receiver_estimate,
    stochastic_receiver_estimate_observable_manifest,
)
from htdt.clock import utc_now_iso
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef

from test_cad_geometric_acoustics_adapter import (  # noqa: E402  (shared fixtures)
    GENERAL_POLICY,
    _execute,
    _fixture,
    _material,
    _portal_fixture,
)


def _policy(**kwargs):
    kwargs.setdefault('sampling_seed', 42)
    kwargs.setdefault('ray_budgets', (64, 256))
    kwargs.setdefault('maximum_bounces', 3)
    kwargs.setdefault('receiver_capture_radius_m', 0.4)
    kwargs.setdefault('declared_convergence_bound', 0.5)
    return build_stochastic_ray_estimation_policy(**kwargs)


def _execute_stochastic(fx, policy=None):
    return execute_stochastic_ray_receiver_estimate(
        execution_input=fx['execution_input'],
        stochastic_policy=policy if policy is not None else _policy(),
        compiled_geometry=fx['compiled'],
        region_authority=fx['region'],
        portal_authority=fx['portals'],
        boundary_termination_authority=fx['terminations'],
        directivity_datasets=(fx['dataset'],),
        material_resolver=fx['material_resolver'],
    )


def test_policy_is_content_addressed_and_bounded() -> None:
    policy = _policy()
    assert policy.policy_id.startswith('r150-stochastic-ray-policy:')
    assert len(policy.semantic_sha256) == 64
    assert build_stochastic_ray_estimation_policy(
        sampling_seed=42,
        ray_budgets=(64, 256),
        maximum_bounces=3,
        receiver_capture_radius_m=0.4,
        declared_convergence_bound=0.5,
    ).policy_id == policy.policy_id
    assert build_stochastic_ray_estimation_policy(
        sampling_seed=43,
        ray_budgets=(64, 256),
        maximum_bounces=3,
        receiver_capture_radius_m=0.4,
        declared_convergence_bound=0.5,
    ).policy_id != policy.policy_id

    with pytest.raises(ValueError):
        _policy(ray_budgets=(256, 256))
    with pytest.raises(ValueError):
        _policy(ray_budgets=(256, 64))
    with pytest.raises(ValueError):
        _policy(ray_budgets=(16, 64))
    with pytest.raises(ValueError):
        _policy(maximum_bounces=9)
    with pytest.raises(ValueError):
        _policy(declared_convergence_bound=0.0)
    with pytest.raises(ValueError):
        _policy(declared_convergence_bound=1.5)


def test_seeded_estimate_reproduces_bit_exactly(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, room_policy=GENERAL_POLICY)
    policy = _policy()

    first = _execute_stochastic(fx, policy)
    second = _execute_stochastic(fx, policy)

    assert first.model_dump_json() == second.model_dump_json()
    assert first.artifact_id == second.artifact_id
    assert first.execution_id == second.execution_id

    other_seed = _execute_stochastic(fx, _policy(sampling_seed=43))
    assert other_seed.model_dump_json() != first.model_dump_json()


def test_convergence_evidence_shape_is_declared(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, room_policy=GENERAL_POLICY)
    policy = _policy(ray_budgets=(64, 256, 1024))
    artifact = _execute_stochastic(fx, policy)

    assert artifact.convergence_status == 'WITHIN_DECLARED_BOUND'
    assert artifact.estimates
    budgets = policy.ray_budgets
    for estimate in artifact.estimates:
        assert [
            level.ray_budget for level in estimate.levels
        ] == list(budgets)
        counts = [level.captured_ray_count for level in estimate.levels]
        assert all(
            count >= 0 and count <= budget
            for count, budget in zip(counts, budgets)
        )
        assert counts == sorted(counts)
        assert (
            estimate.estimate_per_m2
            == estimate.levels[-1].estimate_per_m2
        )
        previous = estimate.levels[-2].estimate_per_m2
        expected_deviation = abs(
            estimate.estimate_per_m2 - previous
        ) / max(previous, policy.convergence_floor_per_m2)
        assert estimate.relative_deviation_from_previous_level == pytest.approx(
            expected_deviation, rel=1e-9
        )
        expected_verdict = (
            'WITHIN_DECLARED_BOUND'
            if expected_deviation <= policy.declared_convergence_bound
            else 'OUTSIDE_DECLARED_BOUND'
        )
        assert estimate.convergence_verdict == expected_verdict
        assert estimate.standard_error_per_m2 >= 0.0


def test_declared_bound_tightening_flips_verdict(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, room_policy=GENERAL_POLICY)
    loose = _execute_stochastic(fx, _policy(declared_convergence_bound=1.0))
    assert loose.convergence_status == 'WITHIN_DECLARED_BOUND'
    assert all(
        item.convergence_verdict == 'WITHIN_DECLARED_BOUND'
        for item in loose.estimates
    )

    tight = _execute_stochastic(fx, _policy(declared_convergence_bound=1.0e-9))
    assert tight.convergence_status == 'OUTSIDE_DECLARED_BOUND'
    assert all(
        item.convergence_verdict == 'OUTSIDE_DECLARED_BOUND'
        for item in tight.estimates
    )


def test_legacy_shoebox_lane_fails_closed(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)  # exact_axis_aligned_closed_shoebox_v1
    with pytest.raises(StochasticRayUnsupportedError) as failure:
        _execute_stochastic(fx)
    assert failure.value.reason_code == 'UNSUPPORTED_GEOMETRY'


def test_capture_radius_bounds_fail_closed(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, room_policy=GENERAL_POLICY)
    with pytest.raises(StochasticRayUnsupportedError) as too_large:
        _execute_stochastic(fx, _policy(receiver_capture_radius_m=0.51))
    assert too_large.value.reason_code == 'CAPTURE_RADIUS_OUT_OF_BOUNDS'
    with pytest.raises(StochasticRayUnsupportedError) as too_small:
        _execute_stochastic(fx, _policy(receiver_capture_radius_m=1.0e-12))
    assert too_small.value.reason_code == 'CAPTURE_RADIUS_OUT_OF_BOUNDS'


def test_engine_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, room_policy=GENERAL_POLICY)

    class ForeignEngine:
        engine_id = 'htdt.r150.foreign'
        engine_version = '1'
        candidate_source_commit = None
        solver_implementation_ref = ExactExternalAuthorityRef(
            authority_id='adapter-kernel:foreign',
            authority_version='1',
            semantic_hash_sha256='0' * 64,
        )

    with pytest.raises(ValueError):
        execute_stochastic_ray_receiver_estimate(
            execution_input=fx['execution_input'],
            stochastic_policy=_policy(),
            compiled_geometry=fx['compiled'],
            region_authority=fx['region'],
            portal_authority=fx['portals'],
            boundary_termination_authority=fx['terminations'],
            directivity_datasets=(fx['dataset'],),
            material_resolver=fx['material_resolver'],
            engine=ForeignEngine(),
        )


def test_unsupported_material_rejects_bands(tmp_path: Path) -> None:
    fx = _fixture(
        tmp_path, room_policy=GENERAL_POLICY, supported_material=False
    )
    artifact = _execute_stochastic(fx)
    assert artifact.convergence_status == 'NO_EVALUABLE_ESTIMATE'
    assert not artifact.estimates
    assert artifact.rejected_candidates
    assert all(
        item.decision == 'UNSUPPORTED_BOUNDARY_QUANTITY'
        for item in artifact.rejected_candidates
    )


def test_narrow_directivity_rejects_bands(tmp_path: Path) -> None:
    fx = _fixture(
        tmp_path, room_policy=GENERAL_POLICY, narrow_directivity=True
    )
    artifact = _execute_stochastic(fx)
    assert artifact.convergence_status == 'NO_EVALUABLE_ESTIMATE'
    assert not artifact.estimates
    assert artifact.rejected_candidates
    assert all(
        item.decision == 'UNSUPPORTED_DIRECTIVITY'
        for item in artifact.rejected_candidates
    )


def test_portal_lane_transmits_through_aperture(tmp_path: Path) -> None:
    fx = _portal_fixture(tmp_path)
    artifact = _execute_stochastic(fx)
    assert artifact.estimates
    for estimate in artifact.estimates:
        assert estimate.receiver_id == 'receiver-mlp'
        assert estimate.estimate_per_m2 >= 0.0
        assert [level.ray_budget for level in estimate.levels] == [
            64,
            256,
        ]
    assert artifact.convergence_status in (
        'WITHIN_DECLARED_BOUND',
        'OUTSIDE_DECLARED_BOUND',
    )


def test_tampered_region_binding_fails_closed(tmp_path: Path) -> None:
    # The source physically sits in region-a; an execution input whose
    # binding claims region-b is not a valid authority — execution must fail
    # closed rather than fabricate an emission (identity revalidation or the
    # region-membership gate, whichever fires first).
    fx = _portal_fixture(tmp_path)
    tampered = fx['execution_input'].model_copy(
        update={
            'sources': (
                fx['execution_input'].sources[0].model_copy(
                    update={'acoustic_region_id': 'region-b'}
                ),
            ),
        }
    )
    with pytest.raises(ValueError):
        execute_stochastic_ray_receiver_estimate(
            execution_input=tampered,
            stochastic_policy=_policy(),
            compiled_geometry=fx['compiled'],
            region_authority=fx['region'],
            portal_authority=fx['portals'],
            boundary_termination_authority=fx['terminations'],
            directivity_datasets=(fx['dataset'],),
            material_resolver=fx['material_resolver'],
        )


def test_observable_manifest_and_envelope_fail_closed(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path, room_policy=GENERAL_POLICY)
    artifact = _execute_stochastic(fx)

    manifest = stochastic_receiver_estimate_observable_manifest(artifact)
    assert manifest.observable == STOCHASTIC_RECEIVER_ESTIMATE_OBSERVABLE
    assert manifest.encoding_schema_ref == STOCHASTIC_RECEIVER_ESTIMATE_SCHEMA_REF

    # The fixture request declares only deterministic_paths — the envelope
    # authority must reject the observable-set mismatch rather than emit a
    # fabricated solver result.
    with pytest.raises(ValueError):
        build_stochastic_receiver_estimate_result_envelope(
            dispatch=fx['dispatch'],
            request=fx['request'],
            artifact=artifact,
            completed_at_utc=utc_now_iso(),
        )


def test_deterministic_lane_bit_stable_alongside_stochastic_lane(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path, room_policy=GENERAL_POLICY)

    before = _execute(fx).model_dump_json()
    _execute_stochastic(fx)
    after = _execute(fx).model_dump_json()

    assert before == after


def test_persisted_artifact_revalidates_all_authorities(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path, room_policy=GENERAL_POLICY)
    policy = _policy()
    artifact = _execute_stochastic(fx, policy)

    deterministic_repository = CadDeterministicPathArtifactRepository(
        fx['scene_repository'],
        snapshot_repository=fx['snapshot_repository'],
        dispatch_repository=fx['dispatch_repository'],
        configuration_resolver=fx['configuration_resolver'],
        material_resolver=fx['material_resolver'],
        geometry_authority_resolver=fx['geometry_resolver'],
    )
    deterministic_repository.save_execution_input(fx['execution_input'])

    repository = CadStochasticReceiverEstimateRepository(
        fx['scene_repository'],
        snapshot_repository=fx['snapshot_repository'],
        dispatch_repository=fx['dispatch_repository'],
        configuration_resolver=fx['configuration_resolver'],
        stochastic_policy_resolver=lambda ref: (
            policy if policy.as_external_ref() == ref else None
        ),
        geometry_authority_resolver=fx['geometry_resolver'],
        material_resolver=fx['material_resolver'],
    )
    repository.save(artifact)
    assert repository.get(artifact.artifact_id) == artifact
    assert repository.save(artifact) == artifact

    ref = artifact.as_external_ref()
    assert repository.resolve_external_authority(ref) == ref
    provenance = artifact.execution_provenance_ref()
    assert repository.resolve_external_authority(provenance) == provenance
    manifest = repository.resolve_artifact_manifest(ref)
    assert manifest is not None
    assert manifest.observable == STOCHASTIC_RECEIVER_ESTIMATE_OBSERVABLE
    assert manifest.encoding_schema_ref == STOCHASTIC_RECEIVER_ESTIMATE_SCHEMA_REF
    assert manifest.solver_lineage['execution_input_id'] == (
        artifact.execution_input_id
    )
    assert manifest.solver_lineage['stochastic_policy_id'] == (
        policy.policy_id
    )

    # Stale authority: the recorded material authority no longer reproduces
    # the artifact — reopening fails closed.
    fx['material_box']['value'] = _material(supported=False)
    with pytest.raises(ValueError):
        repository.get(artifact.artifact_id)


def test_engine_marker_declares_kernel_identity() -> None:
    from htdt.cad_stochastic_ray_receiver import (
        HtdtStochasticRayReceiverEngine,
    )

    engine = HtdtStochasticRayReceiverEngine()
    assert engine.engine_id == STOCHASTIC_RAY_ENGINE_ID
    assert engine.engine_version == STOCHASTIC_RAY_ENGINE_VERSION
    assert (
        engine.solver_implementation_ref
        == HTDT_STOCHASTIC_RAY_RECEIVER_IMPLEMENTATION_REF
    )
    assert engine.coherent_phase == 'not_applicable_energy_domain'
