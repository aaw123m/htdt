"""Contract-enforcement tests for the capture ingestion transaction.

Covers the gated contract surfaces added for #335 (immutable capture
revision identity/topology), #337 (persisted quality gate), #338
(canonical manifest retention + producer identity), #343 (handoff
rederivation from exact bytes), #345 (session/frame identity vs.
evidence) and #369 (bounded payload-schema layer before commit).

Two layers are exercised:

- The bundle/plan layer (``FrozenBundle`` → ``build_ingestion_plan``),
  which rejects contract-invalid bundles before a plan can exist;
- The ingest layer (``ingest()``), which re-enforces the same contract
  inside the commit transaction for any plan a producer claims.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

from pydantic import ValidationError
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_fixture_support as support  # noqa: E402

from htdt.cad_repository import SceneRepository
from htdt.capture_bundle import CaptureBundleError
from htdt.capture_reference import CaptureIngestionContractError
from htdt.capture_ingestion_transaction import (
    CaptureIngestionRepository,
    CaptureIngestionTransactionError,
    CapturePayloadContractError,
    CaptureQualityGateError,
    CaptureRevisionConflictError,
)


BUNDLE_DIGEST = support.BUNDLE_DIGEST
REVISION_ID = support.REVISION_ID
SERIES_ID = support.SERIES_ID
CORRECTED_REVISION_ID = '10000000-0000-4000-8000-00000000000a'
FOREIGN_SERIES_ID = '20000000-0000-4000-8000-000000000001'


def _repository(tmp_path: Path) -> CaptureIngestionRepository:
    return CaptureIngestionRepository(SceneRepository(tmp_path / 'cad.sqlite3'))


def _ingest_default(
    repository: CaptureIngestionRepository, tmp_path: Path
):
    plan, payloads, manifest = support.plan_and_payloads(tmp_path)
    return repository.ingest(plan, payloads, manifest=manifest)


def test_revision_identity_is_immutable_across_reuse(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    result = _ingest_default(repository, tmp_path)
    assert result.created is True

    # Identical re-ingest: no new revision, verified not duplicated.
    plan, payloads, manifest = support.plan_and_payloads(tmp_path)
    again = repository.ingest(plan, payloads, manifest=manifest)
    assert again.created is False
    assert len(repository.list_registered_revisions()) == 1

    # Same revision id under a different bundle digest: rejected and the
    # conflict is recorded, never guessed at.
    plan2, payloads2, _ = support.plan_and_payloads(
        tmp_path,
        manifest_overrides={
            'created_at': '2026-09-21T00:00:00Z',
            'finalized_at': '2026-09-21T00:00:01Z',
        },
    )
    with pytest.raises(CaptureRevisionConflictError, match='immutable'):
        repository.ingest(plan2, payloads2)
    conflicts = repository.list_revision_conflicts()
    assert any(
        conflict.capture_revision_id == REVISION_ID
        for conflict in conflicts
    )


def test_revision_self_parent_rejected(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    plan, payloads, _ = support.plan_and_payloads(tmp_path)
    poisoned = dict(plan)
    poisoned['bundle'] = dict(
        plan['bundle'],
        capture_revision_id=CORRECTED_REVISION_ID,
        parent_revision_id=CORRECTED_REVISION_ID,
    )
    with pytest.raises(ValidationError, match='parent'):
        repository.ingest(poisoned, payloads)


def test_revision_cross_series_parent_rejected(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _ingest_default(repository, tmp_path)

    plan, payloads, _ = support.plan_and_payloads(
        tmp_path,
        manifest_overrides={
            'capture_revision_id': CORRECTED_REVISION_ID,
            'capture_series_id': FOREIGN_SERIES_ID,
            'parent_revision_id': REVISION_ID,
            'created_at': '2026-09-21T00:00:00Z',
            'finalized_at': '2026-09-21T00:00:01Z',
        },
    )
    with pytest.raises(CaptureRevisionConflictError, match='series'):
        repository.ingest(plan, payloads)


def test_out_of_order_parent_is_pending_then_reconciled(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    # Child arrives before its parent: lands as pending_parent.
    child_plan, child_payloads, _ = support.plan_and_payloads(
        tmp_path,
        manifest_overrides={
            'capture_revision_id': CORRECTED_REVISION_ID,
            'parent_revision_id': REVISION_ID,
            'created_at': '2026-09-21T00:00:00Z',
            'finalized_at': '2026-09-21T00:00:01Z',
        },
    )
    child = repository.ingest(child_plan, child_payloads)
    assert child.created is True
    child_revision = repository.get_registered_revision(CORRECTED_REVISION_ID)
    assert child_revision.topology_state == 'pending_parent'

    # When the parent arrives the pending child reconciles to linked.
    _ingest_default(repository, tmp_path)
    parent_revision = repository.get_registered_revision(REVISION_ID)
    assert parent_revision.topology_state == 'root'
    reconciled = repository.get_registered_revision(CORRECTED_REVISION_ID)
    assert reconciled.topology_state == 'linked'


def test_quality_gate_rejects_unsupported_ruleset_and_not_ready(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    quality = json.loads(
        support.fixture_payloads()['quality/capture-quality.json']
    )

    # An unsupported ruleset is rejected as early as plan build.
    bad_ruleset = dict(quality, ruleset_version='9.9.9')
    with pytest.raises(
        CaptureIngestionContractError, match='ruleset'
    ):
        support.plan_and_payloads(
            tmp_path,
            json_overrides={'quality/capture-quality.json': bad_ruleset},
        )

    not_ready = dict(quality, ready_for_htdt_ingestion=False)
    with pytest.raises(CaptureIngestionContractError, match='ready'):
        support.plan_and_payloads(
            tmp_path,
            json_overrides={'quality/capture-quality.json': not_ready},
        )

    integrity_fail = dict(quality, integrity_status='fail')
    with pytest.raises(CaptureIngestionContractError, match='integrity'):
        support.plan_and_payloads(
            tmp_path,
            json_overrides={
                'quality/capture-quality.json': integrity_fail
            },
        )

    # The repository-level gate fails closed for a lineage that has no
    # validated quality state at all.
    with pytest.raises(CaptureQualityGateError):
        repository.require_quality_state('0' * 64)


def test_quality_gate_rejects_missing_foundation_member(
    tmp_path: Path,
) -> None:
    with pytest.raises(CaptureBundleError, match='foundation'):
        support.plan_and_payloads(
            tmp_path, drop=('quality/capture-quality.json',)
        )


def test_quality_state_is_persisted_and_queryable(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    result = _ingest_default(repository, tmp_path)
    state = repository.require_quality_state(result.lineage_digest)
    assert state.state == 'validated'
    assert state.ruleset_version == '1.0.0'
    quality = support.fixture_payloads()['quality/capture-quality.json']
    from hashlib import sha256
    assert state.payload_sha256 == sha256(quality).hexdigest()


def test_manifest_bytes_retained_verbatim(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    plan, payloads, manifest = support.plan_and_payloads(tmp_path)
    result = repository.ingest(plan, payloads, manifest=manifest)
    record = repository.get_capture_bundle(result.bundle_digest)
    assert record is not None
    assert record.manifest_bytes == manifest
    from hashlib import sha256
    assert sha256(record.manifest_bytes).hexdigest() == BUNDLE_DIGEST
    assert record.app_name == 'HTDT-Capture'
    assert record.app_version == '0.0.0-phase6'
    assert record.app_build == 'phase6-fixture'
    assert record.capture_revision_id == REVISION_ID

    # "manifest retained" vs "plan-only": ingest without manifest leaves
    # no bundle record.
    plan2, payloads2, _ = support.plan_and_payloads(
        tmp_path,
        manifest_overrides={
            'capture_revision_id': CORRECTED_REVISION_ID,
            'parent_revision_id': REVISION_ID,
            'created_at': '2026-09-21T00:00:00Z',
            'finalized_at': '2026-09-21T00:00:01Z',
        },
    )
    result2 = repository.ingest(plan2, payloads2)
    assert repository.get_capture_bundle(result2.bundle_digest) is None


def test_anchors_mesh_header_mismatch_rejected(tmp_path: Path) -> None:
    """anchors.json counts vs. HTDTMSH1 header disagree → no plan (#343)."""
    anchors = json.loads(
        support.fixture_payloads()['mesh/anchors.json']
    )
    anchors['anchors'][0]['vertex_count'] += 1
    with pytest.raises(CaptureBundleError, match='vertex_count'):
        support.plan_and_payloads(
            tmp_path,
            json_overrides={'mesh/anchors.json': anchors},
        )


def test_ingest_rederives_handoff_content_claims(tmp_path: Path) -> None:
    """A plan whose handoff claims disagree with rederivation fails (#343).

    The payloads are hash-consistent with the plan's evidence rows, so
    the byte-identity layer passes; the semantic rederivation layer is
    what rejects the mismatched claim.
    """
    repository = _repository(tmp_path)
    plan, payloads, _ = support.plan_and_payloads(tmp_path)
    tampered = dict(plan)
    handoff = dict(plan['raw_visual_mesh_handoffs'][0])
    handoff['vertex_count'] = handoff['vertex_count'] + 1
    tampered['raw_visual_mesh_handoffs'] = [handoff]
    with pytest.raises(CapturePayloadContractError):
        repository.ingest(tampered, payloads)


def test_fabricated_handoff_id_rejected(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    plan, payloads, _ = support.plan_and_payloads(tmp_path)
    fabricated = dict(plan)
    fabricated['raw_visual_mesh_handoffs'] = [
        dict(
            plan['raw_visual_mesh_handoffs'][0],
            raw_visual_mesh_handoff_id='0' * 64,
        )
    ]
    with pytest.raises(ValidationError, match='identity'):
        repository.ingest(fabricated, payloads)


def test_session_identity_mismatch_rejected(tmp_path: Path) -> None:
    """Session identity must agree with bundle identity (#345)."""
    session = json.loads(
        support.fixture_payloads()['session/capture-session.json']
    )
    session['capture_session_id'] = (
        '99999999-0000-4000-8000-000000000099'
    )
    with pytest.raises(CaptureBundleError, match='session'):
        support.plan_and_payloads(
            tmp_path,
            json_overrides={
                'session/capture-session.json': session
            },
        )


def test_frame_style_reference_mismatch_rejected(tmp_path: Path) -> None:
    """A session doc's config/timing refs must resolve (#345)."""
    session = json.loads(
        support.fixture_payloads()['session/capture-session.json']
    )
    session['configuration_ref'] = 'session/other.json'
    with pytest.raises(CaptureIngestionContractError, match='configuration_ref'):
        support.plan_and_payloads(
            tmp_path,
            json_overrides={
                'session/capture-session.json': session
            },
        )


def test_noncanonical_payload_json_rejected(tmp_path: Path) -> None:
    """Capture-owned JSON payloads must be canonical bytes (#369)."""
    files = support.default_file_specs()
    config = files['session/capture-configuration.json']
    files['session/capture-configuration.json'] = dict(
        config,
        bytes=config['bytes'] + b'\n',  # trailing newline: not canonical
    )
    with pytest.raises(CaptureBundleError, match='canonical'):
        support.plan_and_payloads(tmp_path, files=files)


def test_unowned_json_path_rejected(tmp_path: Path) -> None:
    """A JSON payload owned by no published schema is rejected (#369)."""
    files = support.default_file_specs()
    files['scratch/notes.json'] = {
        'bytes': b'{"a":1}',
        'media_type': 'application/json',
        'producer': 'unknown',
        'provenance_class': 'capture_app_derived',
        'role': 'derived',
    }
    with pytest.raises(CaptureBundleError):
        support.plan_and_payloads(tmp_path, files=files)


def test_plan_grammar_rejects_source_ref_cycles(tmp_path: Path) -> None:
    """Cyclic path references inside plan source_refs fail (#343)."""
    repository = _repository(tmp_path)
    plan, payloads, _ = support.plan_and_payloads(tmp_path)
    poisoned = dict(plan)
    evidence = [dict(item) for item in plan['source_evidence']]
    by_path = {item['path']: item for item in evidence}
    a_path = 'annotations/entities.json'
    b_path = 'annotations/measurements.json'
    by_path[a_path]['source_refs'] = (f'path:{b_path}',)
    by_path[b_path]['source_refs'] = (f'path:{a_path}',)
    poisoned['source_evidence'] = evidence
    with pytest.raises(ValidationError, match='cycle'):
        repository.ingest(poisoned, payloads)


def test_ingest_rejects_payload_bytes_mismatch(tmp_path: Path) -> None:
    """Bytes that differ from the plan's declared hash never commit."""
    repository = _repository(tmp_path)
    plan, payloads, _ = support.plan_and_payloads(tmp_path)
    tampered = dict(payloads)
    tampered['session/timing.json'] = payloads['session/timing.json'] + b' '
    with pytest.raises(CaptureIngestionTransactionError):
        repository.ingest(plan, tampered)
