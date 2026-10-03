from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_acoustic_snapshot import build_acoustic_prediction_request
from htdt.cad_acoustic_solver_adapter import (
    AcousticNumericalFidelityPolicy,
    bind_prediction_request_to_solver_adapter,
    build_acoustic_solver_adapter_descriptor,
)
from htdt.cad_hybrid_acoustic_result import (
    build_hybrid_acoustic_result,
    build_hybrid_stitching_policy,
)
from htdt.cad_hybrid_late_energy import (
    LATE_ENERGY_DECAY_OBSERVABLE,
    R160_LATE_ENERGY_ARTIFACT_SCHEMA_REF,
    R160_LATE_ENERGY_DECAY_ADAPTER_ID,
    R160_LATE_ENERGY_DECAY_SOLVER_IMPLEMENTATION_REF,
    R160_LATE_ENERGY_DECAY_SOLVER_ROLE,
    LateFieldSurfaceCapability,
    build_late_energy_decay_result_envelope,
    build_late_field_input_authority,
    declared_late_decay_law,
    late_energy_decay_observable_manifest,
    measured_late_decay_law,
    solve_late_energy_decay,
    surface_scattering_evidence_ref,
)
from htdt.cad_late_decay_estimate import (
    execute_late_decay_estimate,
    late_decay_estimate_decay_law,
)
from htdt.cad_surface_scattering import build_surface_scattering_evidence
from htdt.clock import utc_now_iso

from test_cad_geometric_acoustics_adapter import (  # noqa: E402
    _execute,
    _fixture,
)
from test_cad_hybrid_numerical_composition import _ref  # noqa: E402


def _bands_hz(path_artifact):
    return tuple(
        sorted(
            {
                float(band.center_hz)
                for path in path_artifact.paths
                for band in path.bands
            }
        )
    )


def _evidence(surface_id, bands_hz):
    return build_surface_scattering_evidence(
        quantity_kind='random_incidence_scattering_coefficient',
        surface_id=surface_id,
        band_center_hz=bands_hz,
        values=tuple(0.2 + index * 0.05 for index in range(len(bands_hz))),
        source='synthetic test evidence',
        provenance='r160 late-energy decay consumption test',
    )


def _late_input(path_artifact, evidences):
    """Late-field input declaring scattering_modeled on every touched surface."""
    touched = sorted(
        {
            surface_id
            for path in path_artifact.paths
            for surface_id in path.ordered_interaction_surface_ids
        }
    )
    first = path_artifact.paths[0]
    return build_late_field_input_authority(
        path_artifact=path_artifact,
        source_entity_id=first.source_entity_id,
        receiver_id=first.receiver_id,
        surface_capabilities=tuple(
            LateFieldSurfaceCapability(
                surface_id=surface_id,
                capability='scattering_modeled',
                evidence_ref=surface_scattering_evidence_ref(
                    evidences[surface_id]
                ),
                detail='synthetic modeled scattering',
            )
            for surface_id in touched
        ),
    )


def _decay_fixture(tmp_path):
    """Real R150 fixture + execution + late-field declarations.

    Returns (fixture, path_artifact, bands_hz, evidences, late_input, grid).
    The decay grid starts at the last deterministic arrival so the bounded
    tail never fabricates the unseparated field.
    """
    fx = _fixture(
        tmp_path,
        extra_requested_observables=(LATE_ENERGY_DECAY_OBSERVABLE,),
    )
    path_artifact = _execute(fx)
    bands_hz = _bands_hz(path_artifact)
    touched = sorted(
        {
            surface_id
            for path in path_artifact.paths
            for surface_id in path.ordered_interaction_surface_ids
        }
    )
    evidences = {
        surface_id: _evidence(surface_id, bands_hz) for surface_id in touched
    }
    late_input = _late_input(path_artifact, evidences)
    late_start = max(
        float(path.propagation_delay_s) for path in path_artifact.paths
    )
    grid = (late_start, late_start + 0.05, late_start + 0.15)
    return fx, path_artifact, bands_hz, evidences, late_input, grid


def _solve(path_artifact, late_input, law, evidences):
    return solve_late_energy_decay(
        path_artifact=path_artifact,
        late_field_input=late_input,
        decay_law=law,
        scattering_evidence={
            item.evidence_id: item for item in evidences.values()
        },
    )


def _late_request_dispatch(fx):
    """A READY late-energy-decay dispatch over the fixture snapshot."""
    domain = fx['request'].requested_frequency_domain
    policy = AcousticNumericalFidelityPolicy(
        authority_ref=_ref('late-energy-fidelity-policy'),
        acoustic_domain='geometric',
        model_solver_role_ids=(R160_LATE_ENERGY_DECAY_SOLVER_ROLE,),
        supported_observables=(LATE_ENERGY_DECAY_OBSERVABLE,),
        valid_frequency_domain=domain,
        parameter_bounds={},
    )
    request = build_acoustic_prediction_request(
        snapshot=fx['snapshot'],
        model_solver_role_id=R160_LATE_ENERGY_DECAY_SOLVER_ROLE,
        requested_frequency_domain=domain,
        requested_observables=(LATE_ENERGY_DECAY_OBSERVABLE,),
        numerical_fidelity_policy_ref=policy.authority_ref,
    )
    adapter = build_acoustic_solver_adapter_descriptor(
        adapter_id=R160_LATE_ENERGY_DECAY_ADAPTER_ID,
        adapter_version='1',
        model_solver_role_id=R160_LATE_ENERGY_DECAY_SOLVER_ROLE,
        acoustic_domain='geometric',
        solver_implementation_ref=(
            R160_LATE_ENERGY_DECAY_SOLVER_IMPLEMENTATION_REF
        ),
        solver_configuration_schema_ref=_ref(
            'late-energy-configuration-schema'
        ),
        supported_snapshot_schema_versions=(1, 2, 3),
        supported_observables=(LATE_ENERGY_DECAY_OBSERVABLE,),
        valid_frequency_domain=domain,
    )
    dispatch = bind_prediction_request_to_solver_adapter(
        snapshot=fx['snapshot'],
        request=request,
        adapter=adapter,
        solver_configuration_ref=_ref('late-energy-configuration'),
        numerical_fidelity_policy=policy,
    )
    return request, adapter, dispatch


def test_snapshot_declares_late_energy_decay_ready(tmp_path: Path) -> None:
    fx, _, _, _, _, _ = _decay_fixture(tmp_path)
    states = {
        item.observable: item.state
        for item in fx['snapshot'].readiness.observable_readiness
    }
    assert states['deterministic_paths'] == 'READY'
    assert states[LATE_ENERGY_DECAY_OBSERVABLE] == 'READY'

    _, _, dispatch = _late_request_dispatch(fx)
    assert dispatch.state == 'READY'
    assert dispatch.reasons == ()


def test_declared_model_law_producer(tmp_path: Path) -> None:
    fx, path_artifact, bands_hz, evidences, late_input, grid = (
        _decay_fixture(tmp_path)
    )
    law = declared_late_decay_law(
        band_decay_times={center: 0.4 for center in bands_hz},
        decay_time_grid_s=grid,
        rationale='declared model test law',
    )
    assert law.evidence_ref is None
    assert all(
        band.provenance == 'declared_model' for band in law.bands
    )
    assert [band.center_hz for band in law.bands] == list(bands_hz)

    artifact = _solve(path_artifact, late_input, law, evidences)
    assert artifact.capability_state == 'SUPPORTED'
    assert all(
        band.decay_provenance == 'declared_model'
        for band in artifact.bands
    )

    # A declared model may still bind an external authority for its
    # constants when one exists.
    bound = declared_late_decay_law(
        band_decay_times={center: 0.4 for center in bands_hz},
        decay_time_grid_s=grid,
        rationale='declared model bound to a declared source',
        evidence_ref=_ref('declared-decay-authority'),
    )
    assert bound.evidence_ref == _ref('declared-decay-authority')


def test_measured_law_requires_exact_evidence() -> None:
    with pytest.raises(ValueError, match='measured'):
        measured_late_decay_law(
            band_decay_times={500.0: 0.4},
            decay_time_grid_s=(0.0, 0.1),
            evidence_ref=None,
            rationale='unevidenced measured claim must fail closed',
        )


def test_measured_law_binds_evidence_and_propagates(tmp_path: Path) -> None:
    fx, path_artifact, bands_hz, evidences, late_input, grid = (
        _decay_fixture(tmp_path)
    )
    measured_ref = _ref('measured-rt60-campaign')
    law = measured_late_decay_law(
        band_decay_times={center: 0.5 for center in bands_hz},
        decay_time_grid_s=grid,
        evidence_ref=measured_ref,
        rationale='decay constants from bound measured rt60 evidence',
    )
    assert law.evidence_ref == measured_ref
    assert all(band.provenance == 'measured' for band in law.bands)

    artifact = _solve(path_artifact, late_input, law, evidences)
    assert artifact.capability_state == 'SUPPORTED'
    assert all(
        band.decay_provenance == 'measured' for band in artifact.bands
    )


def test_envelope_binds_supported_artifact_to_ready_dispatch(
    tmp_path: Path,
) -> None:
    fx, path_artifact, bands_hz, evidences, late_input, grid = (
        _decay_fixture(tmp_path)
    )
    law = declared_late_decay_law(
        band_decay_times={center: 0.4 for center in bands_hz},
        decay_time_grid_s=grid,
        rationale='declared model test law',
    )
    artifact = _solve(path_artifact, late_input, law, evidences)
    request, _, dispatch = _late_request_dispatch(fx)
    assert dispatch.state == 'READY'

    envelope = build_late_energy_decay_result_envelope(
        dispatch=dispatch,
        request=request,
        artifact=artifact,
        completed_at_utc=utc_now_iso(),
    )
    assert envelope.result_state == 'COMPLETED'
    assert envelope.execution_id == artifact.execution_id
    assert envelope.dispatch_binding_id == dispatch.binding_id
    (entry,) = envelope.artifacts
    assert entry.observable == LATE_ENERGY_DECAY_OBSERVABLE
    assert entry.encoding_schema_ref == R160_LATE_ENERGY_ARTIFACT_SCHEMA_REF
    assert entry.artifact_authority == artifact.as_external_ref()
    assert entry.valid_frequency_domain == artifact.valid_frequency_domain


def test_envelope_execution_identity_is_deterministic(
    tmp_path: Path,
) -> None:
    fx, path_artifact, bands_hz, evidences, late_input, grid = (
        _decay_fixture(tmp_path)
    )
    law = declared_late_decay_law(
        band_decay_times={center: 0.4 for center in bands_hz},
        decay_time_grid_s=grid,
        rationale='declared model test law',
    )
    first = _solve(path_artifact, late_input, law, evidences)
    second = _solve(path_artifact, late_input, law, evidences)
    assert first.execution_id == second.execution_id
    assert first.execution_provenance_ref() == (
        second.execution_provenance_ref()
    )
    assert first.execution_id.startswith(
        'r160-late-energy-decay-execution:'
    )
    assert first.execution_provenance_ref().authority_id.startswith(
        'r160-late-energy-decay-execution-provenance:'
    )


def test_envelope_fails_closed_on_observable_mismatch(
    tmp_path: Path,
) -> None:
    fx, path_artifact, bands_hz, evidences, late_input, grid = (
        _decay_fixture(tmp_path)
    )
    law = declared_late_decay_law(
        band_decay_times={center: 0.4 for center in bands_hz},
        decay_time_grid_s=grid,
        rationale='declared model test law',
    )
    artifact = _solve(path_artifact, late_input, law, evidences)

    # The fixture's deterministic request/dispatch never declared
    # late_energy_decay — the envelope authority must reject the
    # observable-set mismatch rather than emit a fabricated result.
    with pytest.raises(ValueError):
        build_late_energy_decay_result_envelope(
            dispatch=fx['dispatch'],
            request=fx['request'],
            artifact=artifact,
            completed_at_utc=utc_now_iso(),
        )


def test_envelope_rejects_unsupported_artifact(tmp_path: Path) -> None:
    fx, path_artifact, bands_hz, evidences, late_input, grid = (
        _decay_fixture(tmp_path)
    )
    # Drop every capability declaration: the solve records the artifact as
    # UNSUPPORTED rather than fabricating decay samples.
    first = path_artifact.paths[0]
    incomplete_input = build_late_field_input_authority(
        path_artifact=path_artifact,
        source_entity_id=first.source_entity_id,
        receiver_id=first.receiver_id,
        surface_capabilities=(),
    )
    law = declared_late_decay_law(
        band_decay_times={center: 0.4 for center in bands_hz},
        decay_time_grid_s=grid,
        rationale='declared model test law',
    )
    artifact = _solve(path_artifact, incomplete_input, law, {})
    assert artifact.capability_state == 'UNSUPPORTED'
    assert artifact.unsupported_reasons
    assert not artifact.bands

    request, _, dispatch = _late_request_dispatch(fx)
    assert dispatch.state == 'READY'
    with pytest.raises(ValueError, match='unsupported'):
        build_late_energy_decay_result_envelope(
            dispatch=dispatch,
            request=request,
            artifact=artifact,
            completed_at_utc=utc_now_iso(),
        )


def _hybrid_for_artifact(fx, artifact):
    request, _, dispatch = _late_request_dispatch(fx)
    envelope = build_late_energy_decay_result_envelope(
        dispatch=dispatch,
        request=request,
        artifact=artifact,
        completed_at_utc=utc_now_iso(),
    )
    return build_hybrid_acoustic_result(
        snapshot=fx['snapshot'],
        prediction_requests=(request,),
        solver_results=(envelope,),
        stitching_policy=build_hybrid_stitching_policy(
            mode='disjoint_by_observable'
        ),
        external_payload_resolver=lambda ref: {},
    )


def test_hybrid_consumes_declared_law_end_to_end(tmp_path: Path) -> None:
    fx, path_artifact, bands_hz, evidences, late_input, grid = (
        _decay_fixture(tmp_path)
    )
    law = declared_late_decay_law(
        band_decay_times={center: 0.4 for center in bands_hz},
        decay_time_grid_s=grid,
        rationale='declared model test law',
    )
    artifact = _solve(path_artifact, late_input, law, evidences)

    hybrid = _hybrid_for_artifact(fx, artifact)
    assert hybrid.late_energy_decay.state == 'AVAILABLE'
    assert hybrid.late_energy_decay.artifact_authority == (
        artifact.as_external_ref()
    )
    assert hybrid.late_energy_decay.encoding_schema_ref == (
        R160_LATE_ENERGY_ARTIFACT_SCHEMA_REF
    )


def test_hybrid_consumes_measured_law_end_to_end(tmp_path: Path) -> None:
    fx, path_artifact, bands_hz, evidences, late_input, grid = (
        _decay_fixture(tmp_path)
    )
    law = measured_late_decay_law(
        band_decay_times={center: 0.5 for center in bands_hz},
        decay_time_grid_s=grid,
        evidence_ref=_ref('measured-rt60-campaign'),
        rationale='decay constants from bound measured rt60 evidence',
    )
    artifact = _solve(path_artifact, late_input, law, evidences)
    assert all(
        band.decay_provenance == 'measured' for band in artifact.bands
    )

    hybrid = _hybrid_for_artifact(fx, artifact)
    assert hybrid.late_energy_decay.state == 'AVAILABLE'


def test_hybrid_consumes_analytic_estimate_end_to_end(
    tmp_path: Path,
) -> None:
    fx, path_artifact, bands_hz, evidences, late_input, grid = (
        _decay_fixture(tmp_path)
    )
    estimate = execute_late_decay_estimate(
        execution_input=fx['execution_input'],
        compiled_geometry=fx['compiled'],
        region_authority=fx['region'],
        portal_authority=fx['portals'],
        boundary_termination_authority=fx['terminations'],
        material_resolver=fx['material_resolver'],
    )
    assert estimate.estimation_status == 'EVALUATED'
    law = late_decay_estimate_decay_law(
        estimate,
        decay_time_grid_s=grid,
    )
    artifact = _solve(path_artifact, late_input, law, evidences)
    assert all(
        band.decay_provenance == 'analytic_estimate'
        for band in artifact.bands
    )

    hybrid = _hybrid_for_artifact(fx, artifact)
    assert hybrid.late_energy_decay.state == 'AVAILABLE'


def test_observable_manifest_carries_canonical_encoding(
    tmp_path: Path,
) -> None:
    fx, path_artifact, bands_hz, evidences, late_input, grid = (
        _decay_fixture(tmp_path)
    )
    law = declared_late_decay_law(
        band_decay_times={center: 0.4 for center in bands_hz},
        decay_time_grid_s=grid,
        rationale='declared model test law',
    )
    artifact = _solve(path_artifact, late_input, law, evidences)
    manifest = late_energy_decay_observable_manifest(artifact)
    assert manifest.observable == LATE_ENERGY_DECAY_OBSERVABLE
    assert manifest.encoding_schema_ref == R160_LATE_ENERGY_ARTIFACT_SCHEMA_REF
    assert manifest.valid_frequency_domain == artifact.valid_frequency_domain
