from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_geometric_acoustics_adapter import (
    CadDeterministicPathArtifactRepository,
    DeterministicGaUnsupportedError,
)
from htdt.cad_hybrid_late_energy import build_late_energy_decay_law
from htdt.cad_late_decay_estimate import (
    HTDT_LATE_DECAY_ESTIMATE_IMPLEMENTATION_REF,
    LATE_DECAY_ESTIMATE_DECAY_MODEL,
    LATE_DECAY_ESTIMATE_ENGINE_ID,
    LATE_DECAY_ESTIMATE_ENGINE_VERSION,
    LATE_DECAY_ESTIMATE_OBSERVABLE,
    LATE_DECAY_ESTIMATE_SCHEMA_REF,
    SABINE_DECAY_CONSTANT,
    CadLateDecayEstimateRepository,
    HtdtLateDecayEstimateEngine,
    build_late_decay_estimate_result_envelope,
    execute_late_decay_estimate,
    late_decay_estimate_decay_law,
    late_decay_estimate_observable_manifest,
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


def _execute_estimate(fx):
    return execute_late_decay_estimate(
        execution_input=fx['execution_input'],
        compiled_geometry=fx['compiled'],
        region_authority=fx['region'],
        portal_authority=fx['portals'],
        boundary_termination_authority=fx['terminations'],
        material_resolver=fx['material_resolver'],
    )


def test_sabine_estimate_reproduces_bit_exactly(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)  # 4x3x2 m axis-aligned shoebox
    first = _execute_estimate(fx)
    second = _execute_estimate(fx)

    assert first.model_dump_json() == second.model_dump_json()
    assert first.artifact_id == second.artifact_id
    assert first.execution_id == second.execution_id
    assert first.estimation_status == 'EVALUATED'
    assert first.decay_model == LATE_DECAY_ESTIMATE_DECAY_MODEL
    assert first.region_volume_m3 == pytest.approx(24.0, rel=1e-9)
    assert len(first.estimates) == 2

    by_band = {item.center_hz: item for item in first.estimates}
    for center, alpha in ((500.0, 0.2), (1000.0, 0.3)):
        estimate = by_band[center]
        # A = sum S_i * alpha_i over the six room-boundary surfaces
        # (2*12 + 2*8 + 2*6 = 52 m^2 total boundary area).
        expected_area = pytest.approx(52.0 * alpha, rel=1e-9)
        assert estimate.total_absorption_area_m2 == expected_area
        expected_t60 = (
            SABINE_DECAY_CONSTANT
            * first.region_volume_m3
            / (
                float(fx['execution_input'].sound_speed_m_s)
                * estimate.total_absorption_area_m2
            )
        )
        assert estimate.decay_time_s == pytest.approx(expected_t60, rel=1e-9)
        assert len(estimate.surface_contributions) == 6
        assert (
            sum(
                item.absorption_area_m2
                for item in estimate.surface_contributions
            )
            == expected_area
        )


def test_general_policy_uses_exact_shell_volume(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, room_policy=GENERAL_POLICY)
    artifact = _execute_estimate(fx)
    assert artifact.estimation_status == 'EVALUATED'
    assert artifact.region_volume_m3 > 0.0
    for estimate in artifact.estimates:
        assert estimate.decay_time_s == pytest.approx(
            SABINE_DECAY_CONSTANT
            * artifact.region_volume_m3
            / (
                float(fx['execution_input'].sound_speed_m_s)
                * estimate.total_absorption_area_m2
            ),
            rel=1e-9,
        )
        assert estimate.surface_contributions


def test_portal_policy_lane_fails_closed(tmp_path: Path) -> None:
    fx = _portal_fixture(tmp_path)
    with pytest.raises(DeterministicGaUnsupportedError) as failure:
        _execute_estimate(fx)
    assert failure.value.reason_code == 'UNSUPPORTED_GEOMETRY'


def test_interior_object_surface_fails_closed(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, occluder=True)
    with pytest.raises(DeterministicGaUnsupportedError) as failure:
        _execute_estimate(fx)
    assert failure.value.reason_code == 'UNSUPPORTED_GEOMETRY'


def test_unsupported_material_rejects_bands(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, supported_material=False)
    artifact = _execute_estimate(fx)
    assert artifact.estimation_status == 'NO_EVALUABLE_ESTIMATE'
    assert not artifact.estimates
    assert artifact.rejected_candidates
    assert all(
        item.decision == 'UNSUPPORTED_BOUNDARY_QUANTITY'
        for item in artifact.rejected_candidates
    )
    with pytest.raises(ValueError):
        late_decay_estimate_decay_law(
            artifact, decay_time_grid_s=(0.0, 0.1)
        )


def test_engine_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)

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
        execute_late_decay_estimate(
            execution_input=fx['execution_input'],
            compiled_geometry=fx['compiled'],
            region_authority=fx['region'],
            portal_authority=fx['portals'],
            boundary_termination_authority=fx['terminations'],
            material_resolver=fx['material_resolver'],
            engine=ForeignEngine(),
        )


def test_tampered_region_binding_fails_closed(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
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
        execute_late_decay_estimate(
            execution_input=tampered,
            compiled_geometry=fx['compiled'],
            region_authority=fx['region'],
            portal_authority=fx['portals'],
            boundary_termination_authority=fx['terminations'],
            material_resolver=fx['material_resolver'],
        )


def test_decay_law_derivation_binds_artifact_evidence(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    artifact = _execute_estimate(fx)

    law = late_decay_estimate_decay_law(
        artifact, decay_time_grid_s=(0.0, 0.1, 0.2)
    )
    assert law.evidence_ref == artifact.as_external_ref()
    assert [band.center_hz for band in law.bands] == [
        item.center_hz for item in artifact.estimates
    ]
    assert all(
        band.provenance == 'analytic_estimate' for band in law.bands
    )
    for band in law.bands:
        assert law.decay_time_for(band.center_hz) == band.decay_time_s

    foreign = ExactExternalAuthorityRef(
        authority_id='adapter-kernel:foreign',
        authority_version='1',
        semantic_hash_sha256='0' * 64,
    )
    explicit = late_decay_estimate_decay_law(
        artifact,
        decay_time_grid_s=(0.0, 0.1, 0.2),
        evidence_ref=foreign,
    )
    assert explicit.evidence_ref == foreign
    assert explicit.semantic_sha256 != law.semantic_sha256

    with pytest.raises(ValueError):
        # Band centers must be unique — a duplicated band is not a law.
        build_late_energy_decay_law(
            bands=law.bands + law.bands[:1],
            decay_time_grid_s=(0.0, 0.1, 0.2),
            evidence_ref=None,
            rationale='duplicated',
        )


def test_observable_manifest_and_envelope_fail_closed(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    artifact = _execute_estimate(fx)

    manifest = late_decay_estimate_observable_manifest(artifact)
    assert manifest.observable == LATE_DECAY_ESTIMATE_OBSERVABLE
    assert manifest.encoding_schema_ref == LATE_DECAY_ESTIMATE_SCHEMA_REF

    # The fixture request declares only deterministic_paths — the envelope
    # authority must reject the observable-set mismatch rather than emit a
    # fabricated solver result.
    with pytest.raises(ValueError):
        build_late_decay_estimate_result_envelope(
            dispatch=fx['dispatch'],
            request=fx['request'],
            artifact=artifact,
            completed_at_utc=utc_now_iso(),
        )


def test_deterministic_lane_bit_stable_alongside_estimate(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)

    before = _execute(fx).model_dump_json()
    _execute_estimate(fx)
    after = _execute(fx).model_dump_json()

    assert before == after


def test_persisted_artifact_revalidates_all_authorities(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    artifact = _execute_estimate(fx)

    deterministic_repository = CadDeterministicPathArtifactRepository(
        fx['scene_repository'],
        snapshot_repository=fx['snapshot_repository'],
        dispatch_repository=fx['dispatch_repository'],
        configuration_resolver=fx['configuration_resolver'],
        material_resolver=fx['material_resolver'],
        geometry_authority_resolver=fx['geometry_resolver'],
    )
    deterministic_repository.save_execution_input(fx['execution_input'])

    repository = CadLateDecayEstimateRepository(
        fx['scene_repository'],
        snapshot_repository=fx['snapshot_repository'],
        dispatch_repository=fx['dispatch_repository'],
        configuration_resolver=fx['configuration_resolver'],
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
    assert manifest.observable == LATE_DECAY_ESTIMATE_OBSERVABLE
    assert manifest.encoding_schema_ref == LATE_DECAY_ESTIMATE_SCHEMA_REF
    assert manifest.solver_lineage['execution_input_id'] == (
        artifact.execution_input_id
    )

    # Stale authority: the recorded material authority no longer reproduces
    # the artifact — reopening fails closed.
    fx['material_box']['value'] = _material(supported=False)
    with pytest.raises(ValueError):
        repository.get(artifact.artifact_id)


def test_engine_marker_declares_kernel_identity() -> None:
    engine = HtdtLateDecayEstimateEngine()
    assert engine.engine_id == LATE_DECAY_ESTIMATE_ENGINE_ID
    assert engine.engine_version == LATE_DECAY_ESTIMATE_ENGINE_VERSION
    assert (
        engine.solver_implementation_ref
        == HTDT_LATE_DECAY_ESTIMATE_IMPLEMENTATION_REF
    )
    assert engine.coherent_phase == 'not_applicable_energy_domain'
