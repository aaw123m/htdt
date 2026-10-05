"""REV52 — remaining capability-emit paths.

A. GA descriptor persist + artifact-commit emit: persisting a solver
   adapter descriptor now emits the declared capability manifest through
   the same derivation table (fail closed — unevidenced phenomena stay
   UNSUPPORTED), and committing a deterministic-GA path artifact emits the
   produced-narrowed manifest, same shape as the R130A/polyhedral lanes.

B. Prediction/matrix verify-only persistence: a persisted matrix run can
   be re-verified against the persisted provider authorities and the
   verification record itself persisted to the authority store. That ref
   is honest validation evidence — a candidate provider verified with
   complete receiver coverage can be promoted to ``validated`` /
   ``synthetic_fixture`` only; nothing unmeasured is claimed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.run_r130a_candidate_wave_execution import (  # noqa: E402
    _fixture as r130_fixture,
)
from test_rev44_surfaces import (  # noqa: E402
    _persist_result,
    _registered_provider,
)
from test_cad_geometric_acoustics_adapter import (  # noqa: E402
    _execute as _ga_execute,
    _fixture as _ga_fixture,
)

from htdt.cad_geometric_acoustics_adapter import (  # noqa: E402
    CadDeterministicPathArtifactRepository,
)
from htdt.cad_prediction_matrix import (  # noqa: E402
    MATRIX_RUN_VERIFICATION_AUTHORITY_VERSION,
    MatrixObservableContract,
    MatrixReceiverRef,
    MatrixRunVerification,
    MatrixSourceRef,
    build_prediction_matrix_spec,
    execute_prediction_matrix,
)
from htdt.cad_prediction_registration import (  # noqa: E402
    PredictionAuthorityLane,
)
from htdt.cad_solver_capability_manifest import (  # noqa: E402
    build_solver_capability_manifest,
    derive_solver_capability_rows,
)
from htdt.clock import utc_now_iso as _utc_now  # noqa: E402
from htdt.prediction_matrix_service import (  # noqa: E402
    PredictionMatrixService,
)


# ---------------------------------------------------------------------------
# A. GA descriptor persist + artifact commit emit
# ---------------------------------------------------------------------------


def _ga_repository(fx) -> CadDeterministicPathArtifactRepository:
    return CadDeterministicPathArtifactRepository(
        fx['scene_repository'],
        snapshot_repository=fx['snapshot_repository'],
        dispatch_repository=fx['dispatch_repository'],
        configuration_resolver=fx['configuration_resolver'],
        material_resolver=fx['material_resolver'],
        geometry_authority_resolver=fx['geometry_resolver'],
    )


def test_ga_descriptor_persist_emits_declared_manifest(
    tmp_path: Path,
) -> None:
    """Persisting the GA descriptor auto-emits the declared manifest —
    the emit point the report said did not exist for the GA lane."""
    fx = _ga_fixture(tmp_path)
    descriptor = fx['descriptor']
    repository = fx['dispatch_repository']

    manifests = repository.list_capability_manifests(
        adapter_descriptor_id=descriptor.descriptor_id
    )
    expected = build_solver_capability_manifest(
        descriptor=descriptor,
        rows=derive_solver_capability_rows(descriptor),
    )
    assert manifests == (expected,)
    states = {row.phenomenon: row.state for row in expected.rows}
    assert states['direct_sound'] == 'SUPPORTED'
    # GA produces no coherent phase / modal field — unevidenced claims
    # stay UNSUPPORTED with reasons (fail closed).
    assert states['coherent_phase'] == 'UNSUPPORTED'
    assert states['low_frequency_modal_response'] == 'UNSUPPORTED'

    # Re-persisting the descriptor re-derives the identical manifest —
    # a deduped no-op, not a conflict.
    repository.save_descriptor(descriptor)
    assert (
        repository.list_capability_manifests(
            adapter_descriptor_id=descriptor.descriptor_id
        )
        == manifests
    )


def test_ga_artifact_commit_emits_produced_manifest_idempotently(
    tmp_path: Path,
) -> None:
    """Committing a deterministic-GA artifact emits the produced-narrowed
    manifest next to the declared one — same shape as R130A/polyhedral."""
    fx = _ga_fixture(tmp_path)
    descriptor = fx['descriptor']
    repository = _ga_repository(fx)
    artifact = _ga_execute(fx)

    repository.save_execution_input(fx['execution_input'])
    repository.save(artifact)

    manifests = fx['dispatch_repository'].list_capability_manifests(
        adapter_descriptor_id=descriptor.descriptor_id
    )
    declared = build_solver_capability_manifest(
        descriptor=descriptor,
        rows=derive_solver_capability_rows(descriptor),
    )
    produced = build_solver_capability_manifest(
        descriptor=descriptor,
        rows=derive_solver_capability_rows(
            descriptor,
            produced_observables=('deterministic_paths',),
        ),
    )
    assert manifests == (declared, produced)

    # Re-committing the identical artifact converges — no duplicate emit.
    repository.save(artifact)
    assert (
        fx['dispatch_repository'].list_capability_manifests(
            adapter_descriptor_id=descriptor.descriptor_id
        )
        == manifests
    )


# ---------------------------------------------------------------------------
# B. verify-only persisted verification of a matrix run
# ---------------------------------------------------------------------------


def _service_and_run(fixture, lane, provider):
    document_id = fixture['snapshot'].document_id
    service = PredictionMatrixService(
        fixture['scene_repository'], document_id
    )
    source_entity_id = (
        provider.source_identity.source_binding.source_entity_id
    )
    providers = {source_entity_id: provider}
    plan = service.creation_plan(providers)
    assert plan.problems == ()
    spec = service.create_matrix(
        source_entity_ids=plan.source_entity_ids,
        receiver_ids=plan.receiver_ids,
        providers=providers,
        frequency_axis_hz=plan.frequency_axis_hz,
        solver_implementation_ref=plan.solver_implementation_ref,
        valid_frequency_domain=plan.valid_frequency_domain,
    )
    run = service.run_matrix(spec.spec_id, providers)
    assert run.state == 'READY'
    return document_id, service, spec, run


def test_matrix_run_verification_persists_record_and_ref(
    tmp_path: Path,
) -> None:
    fixture = r130_fixture(tmp_path, tmp_path / 'unused-pffdtd-upstream')
    _envelope, lane, provider = _registered_provider(fixture)
    _document_id, _service, spec, run = _service_and_run(
        fixture, lane, provider
    )

    verification, ref = lane.persist_matrix_run_verification(run.run_id)

    assert verification.run_id == run.run_id
    assert verification.run_state == 'READY'
    assert verification.spec_id == spec.spec_id
    assert ref.authority_version == (
        MATRIX_RUN_VERIFICATION_AUTHORITY_VERSION
    )
    entry = verification.provider_entry(provider.provider_id)
    assert entry is not None
    assert entry.coverage_complete is True
    assert entry.unverified_cells == ()
    assert set(entry.verified_observables) == {
        'frequency_response_magnitude',
        'frequency_response_phase',
    }
    # The persisted bytes replay through the real validator — hash bound.
    payload = lane.authority_store.read_payload(ref)
    reloaded = MatrixRunVerification.model_validate(payload)
    assert reloaded == verification

    # Verify-only: re-persisting converges to the same record + ref.
    verification2, ref2 = lane.persist_matrix_run_verification(run.run_id)
    assert (verification2, ref2) == (verification, ref)


def test_matrix_verification_promotes_provider_to_validated(
    tmp_path: Path,
) -> None:
    """A fully-verified provider can promote candidate → validated /
    synthetic_fixture on the persisted verification ref — the capability
    emit of matrix execution results."""
    fixture = r130_fixture(tmp_path, tmp_path / 'unused-pffdtd-upstream')
    _envelope, lane, provider = _registered_provider(fixture)
    _document_id, _service, _spec, run = _service_and_run(
        fixture, lane, provider
    )
    _verification, ref = lane.persist_matrix_run_verification(run.run_id)

    promoted = lane.promote_provider_via_matrix_run(
        provider.provider_id, ref
    )
    assert promoted.evidence_state == 'validated'
    assert promoted.evidence_scope == 'synthetic_fixture'
    assert promoted.validation_authority_ref == ref
    assert promoted.provider_id != provider.provider_id
    # The promoted provider is itself persisted and replays on reopen.
    reopened = PredictionAuthorityLane(
        fixture['scene_repository'],
        authority_root=fixture['authority_root'],
    )
    loaded = reopened.provider_repository.get_provider(
        promoted.provider_id
    )
    assert loaded == promoted

    # Overclaim stays closed: a tampered verification record cannot
    # promote (hash-bound validation on read).
    doctored = _verification.model_dump(mode='json')
    doctored['providers'][0]['coverage_complete'] = False
    bad_ref = lane.authority_store.put_json(
        'prediction-matrix-run-verification',
        MATRIX_RUN_VERIFICATION_AUTHORITY_VERSION,
        doctored,
    )
    with pytest.raises(ValueError):
        lane.promote_provider_via_matrix_run(
            provider.provider_id, bad_ref
        )
    # A provider that is not persisted cannot promote at all.
    with pytest.raises(ValueError, match='not persisted'):
        lane.promote_provider_via_matrix_run(
            'r170a-provider:' + '0' * 64, ref
        )


def test_matrix_verification_records_unverified_cells_and_blocks_promotion(
    tmp_path: Path,
) -> None:
    """A run where a provider does not cover every receiver verifies only
    the produced cells; promotion fails closed on partial coverage."""
    fixture = r130_fixture(tmp_path, tmp_path / 'unused-pffdtd-upstream')
    _envelope, lane, provider = _registered_provider(fixture)
    revision = fixture['revision']
    snapshot = fixture['snapshot']
    source_entity_id = (
        provider.source_identity.source_binding.source_entity_id
    )
    receiver_id = provider.receiver_responses[0].receiver_id
    receiver_identity = provider.receiver_identities[0]

    spec = build_prediction_matrix_spec(
        document_id=snapshot.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        acoustic_scene_snapshot_id=snapshot.snapshot_id,
        acoustic_scene_snapshot_sha256=snapshot.semantic_sha256,
        solver_implementation_ref=(
            provider.current_authority.solver_implementation_ref
        ),
        valid_frequency_domain=provider.valid_frequency_domain,
        sources=(
            MatrixSourceRef(
                matrix_source_id=f'source:{source_entity_id}',
                source_entity_id=source_entity_id,
                source_binding_sha256=(
                    provider.source_identity.source_binding_sha256
                ),
            ),
        ),
        receivers=(
            MatrixReceiverRef(
                matrix_receiver_id=f'receiver:{receiver_id}',
                receiver_id=receiver_id,
                receiver_entity_id=(
                    receiver_identity.receiver_binding.entity_id
                ),
                receiver_binding_sha256=(
                    receiver_identity.receiver_binding_sha256
                ),
            ),
            # A receiver the provider does not cover — the cell must stay
            # honestly unverified rather than silently produced.
            MatrixReceiverRef(
                matrix_receiver_id='receiver:ghost',
                receiver_id='ghost',
                receiver_entity_id='ghost',
                receiver_binding_sha256='0' * 64,
            ),
        ),
        observable_contract=MatrixObservableContract(
            frequency_axis_hz=tuple(
                provider.receiver_responses[0].frequency_hz
            ),
        ),
    )
    run = execute_prediction_matrix(
        spec,
        {f'source:{source_entity_id}': provider},
        repository=lane.matrix_repository,
        started_at_utc=_utc_now(),
        finished_at_utc=_utc_now(),
    )
    assert run.state == 'READY'

    verification, ref = lane.persist_matrix_run_verification(run.run_id)
    entry = verification.provider_entry(provider.provider_id)
    assert entry is not None
    assert entry.coverage_complete is False
    assert len(entry.verified_cells) == 1
    assert len(entry.unverified_cells) == 1
    blocked = next(
        item for item in verification.cells if item.state == 'BLOCKED'
    )
    assert 'does not cover receiver' in blocked.blocked_reason

    with pytest.raises(ValueError, match='does not verify every receiver'):
        lane.promote_provider_via_matrix_run(provider.provider_id, ref)
