from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import struct

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_fixture_support as support  # noqa: E402

from htdt.cad_repository import SceneRepository
from htdt.capture_ingestion_transaction import (
    CaptureIngestionPlan,
    CaptureIngestionRepository,
    CaptureIngestionTransactionError,
    PersistedIngestionIntegrityError,
)


BUNDLE_DIGEST = support.BUNDLE_DIGEST
SERIES_ID = support.SERIES_ID
REVISION_ID = support.REVISION_ID
SESSION_ID = support.SESSION_ID
SPACE_ID = support.SPACE_ID
ANCHOR_ID = support.ANCHOR_ID
ANNOTATION_ID = support.ANNOTATION_ID


def _hash_parts(prefix: str, *parts: str) -> str:
    digest = sha256(prefix.encode('utf-8'))
    for part in parts:
        digest.update(b'\x00')
        digest.update(part.encode('utf-8'))
    return digest.hexdigest()


def _plan_and_payloads(
    tmp_path: Path | None = None,
) -> tuple[dict, dict[str, bytes]]:
    import tempfile
    bundle_dir = (
        tmp_path if tmp_path is not None else Path(tempfile.mkdtemp())
    )
    plan, payloads, _manifest = support.plan_and_payloads(bundle_dir)
    return plan, payloads


def _run_id(repository, lineage_digest: str) -> str:
    """The single persisted run id for a lineage in these fixtures."""

    runs = repository.list_ingestion_runs(lineage_digest=lineage_digest)
    assert len(runs) == 1
    return runs[0].ingestion_run_id


def test_transaction_commits_all_source_authorities_and_reopens(
    tmp_path: Path,
) -> None:
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    repository = CaptureIngestionRepository(scene)
    plan, payloads = _plan_and_payloads()

    result = repository.ingest(plan, payloads)

    assert result.created
    assert result.source_evidence_count == 10
    assert result.roomplan_record_count == 2
    assert result.raw_mesh_binding_count == 1
    assert result.authority_record_count == 2

    typed = CaptureIngestionPlan.model_validate(plan)
    reopened = repository.get_ingestion_run_plan(result.ingestion_run_id)
    assert reopened == typed

    geometry_source = next(
        item for item in typed.source_evidence
        if item.path.endswith('.meshbin')
    )
    persisted = repository.get_source_evidence(
        geometry_source.source_evidence_id
    )
    assert persisted is not None
    assert persisted.record == geometry_source
    assert persisted.payload == payloads[geometry_source.path]

    handoff = typed.raw_visual_mesh_handoffs[0]
    binding_id = repository.get_ingestion_run_plan(
        result.ingestion_run_id
    ).raw_visual_mesh_handoffs[0].raw_visual_mesh_handoff_id
    # Binding ID is adapter-derived, so locate it through the persisted DB link.
    with sqlite3.connect(scene.path) as connection:
        row = connection.execute(
            '''
            SELECT binding_id
            FROM capture_ingestion_mesh_links
            WHERE ingestion_run_id=?
            ''',
            (result.ingestion_run_id,),
        ).fetchone()
    assert row is not None
    binding = repository.get_mesh_binding(row[0])
    assert binding is not None
    assert binding.handoff == handoff
    assert binding.raw_mesh.original_asset_bytes() == payloads[handoff.geometry_path]

    authority = repository.get_authority_record(
        typed.authority_records[0].authority_record_handoff_id
    )
    assert authority == typed.authority_records[0]


def test_reingestion_is_idempotent_for_same_plan_and_payloads(
    tmp_path: Path,
) -> None:
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads = _plan_and_payloads()

    first = repository.ingest(plan, payloads)
    second = repository.ingest(plan, payloads)

    assert first.created
    assert not second.created
    assert first.lineage_digest == second.lineage_digest
    assert repository.source_evidence_count() == 10


def test_payload_failure_leaves_no_partial_source_evidence(
    tmp_path: Path,
) -> None:
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads = _plan_and_payloads()
    typed = CaptureIngestionPlan.model_validate(plan)
    bad = dict(payloads)
    geometry_path = typed.raw_visual_mesh_handoffs[0].geometry_path
    bad[geometry_path] = bad[geometry_path] + b'tamper'

    with pytest.raises(
        CaptureIngestionTransactionError,
        match='byte-count mismatch',
    ):
        repository.ingest(typed, bad)

    assert not repository.list_ingestion_runs(
        lineage_digest=typed.lineage_digest
    )
    assert repository.source_evidence_count() == 0


def test_plan_deterministic_identity_is_revalidated_backend_side() -> None:
    plan, _ = _plan_and_payloads()
    plan['source_evidence'][0]['source_evidence_id'] = 'f' * 64

    with pytest.raises(ValueError, match='deterministic identity mismatch'):
        CaptureIngestionPlan.model_validate(plan)



def test_plan_rejects_unresolved_source_reference_backend_side() -> None:
    plan, _ = _plan_and_payloads()
    plan['source_evidence'][0]['source_refs'] = ['path:missing/payload.bin']

    with pytest.raises(ValueError, match='unresolved source evidence path reference'):
        CaptureIngestionPlan.model_validate(plan)


def test_plan_rejects_roomplan_kind_path_provenance_mismatch() -> None:
    plan, _ = _plan_and_payloads()
    plan['roomplan_records'][0]['kind'] = 'postprocessed_inference'

    with pytest.raises(ValueError, match='RoomPlan kind/path/provenance mismatch'):
        CaptureIngestionPlan.model_validate(plan)


def test_plan_rejects_wrong_mesh_anchor_index_authority() -> None:
    plan, _ = _plan_and_payloads()
    annotation_source = next(
        item for item in plan['source_evidence']
        if item['path'] == 'annotations/entities.json'
    )
    plan['raw_visual_mesh_handoffs'][0][
        'anchor_index_source_evidence_id'
    ] = annotation_source['source_evidence_id']

    with pytest.raises(ValueError, match='raw mesh anchor-index authority mismatch'):
        CaptureIngestionPlan.model_validate(plan)


def test_plan_rejects_unpinned_ingestor_configuration() -> None:
    plan, _ = _plan_and_payloads()
    plan['ingestor']['configuration_digest'] = 'f' * 64

    with pytest.raises(ValueError):
        CaptureIngestionPlan.model_validate(plan)


def _persisted_binding_row(
    path: Path,
    binding_id: str,
) -> sqlite3.Row:
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            '''
            SELECT *
            FROM capture_raw_visual_mesh_bindings
            WHERE binding_id=?
            ''',
            (binding_id,),
        ).fetchall()
    assert len(rows) == 1
    return rows[0]


def test_mesh_binding_persists_normalized_source_authority_edges(
    tmp_path: Path,
) -> None:
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads = _plan_and_payloads()
    typed = CaptureIngestionPlan.model_validate(plan)
    repository.ingest(plan, payloads)
    handoff = typed.raw_visual_mesh_handoffs[0]
    binding_id = repository.mesh_binding_ids_for_lineage(
        typed.lineage_digest
    )[0]

    row = _persisted_binding_row(repository.path, binding_id)
    assert (
        row['anchor_index_source_evidence_id']
        == handoff.anchor_index_source_evidence_id
    )
    assert (
        row['geometry_source_evidence_id']
        == handoff.geometry_source_evidence_id
    )

    with closing(sqlite3.connect(repository.path)) as connection:
        connection.execute('PRAGMA foreign_keys=ON')
        # The normalized edge is FK-bound: a dangling authority id is
        # rejected at the schema level.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                '''
                UPDATE capture_raw_visual_mesh_bindings
                SET geometry_source_evidence_id=?
                WHERE binding_id=?
                ''',
                ('f' * 64, binding_id),
            )
        connection.rollback()
        # Deleting a source authority that backs a binding is rejected.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                '''
                DELETE FROM capture_source_evidence
                WHERE source_evidence_id=?
                ''',
                (handoff.geometry_source_evidence_id,),
            )
        connection.rollback()


def test_mesh_binding_read_fails_closed_on_diverged_source_column(
    tmp_path: Path,
) -> None:
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads = _plan_and_payloads()
    typed = CaptureIngestionPlan.model_validate(plan)
    repository.ingest(plan, payloads)
    handoff = typed.raw_visual_mesh_handoffs[0]
    binding_id = repository.mesh_binding_ids_for_lineage(
        typed.lineage_digest
    )[0]
    other = next(
        item
        for item in typed.source_evidence
        if item.source_evidence_id != handoff.geometry_source_evidence_id
    )

    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute(
            '''
            UPDATE capture_raw_visual_mesh_bindings
            SET geometry_source_evidence_id=?
            WHERE binding_id=?
            ''',
            (other.source_evidence_id, binding_id),
        )

    with pytest.raises(
        CaptureIngestionTransactionError,
        match='normalized source authorities do not match',
    ):
        repository.get_mesh_binding(binding_id)

    # A missing normalized authority fails closed the same way; the row can
    # no longer attest its provenance.
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            '''
            UPDATE capture_raw_visual_mesh_bindings
            SET anchor_index_source_evidence_id=NULL
            WHERE binding_id=?
            ''',
            (binding_id,),
        )

    with pytest.raises(
        CaptureIngestionTransactionError,
        match='missing its normalized',
    ):
        repository.get_mesh_binding(binding_id)


def _tamper(path: Path, *statements: tuple[str, tuple]) -> None:
    """Write directly with foreign-key enforcement disabled.

    Mirrors the damage paths issue #377 covers — faulty migrations, restore
    bugs, or FK-disabled tooling can leave a surviving run row over a
    partial or corrupt materialization.
    """

    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute('PRAGMA foreign_keys=OFF')
        for sql, args in statements:
            connection.execute(sql, args)


def _rows(path: Path, sql: str, args: tuple = ()) -> list:
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(sql, args).fetchall()


def test_healthy_reimport_returns_verified_persisted_counts(
    tmp_path: Path,
) -> None:
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads = _plan_and_payloads()

    first = repository.ingest(plan, payloads)
    second = repository.ingest(plan, payloads)

    assert first.created
    assert not second.created
    # Counts come from the verified persisted materialization.
    assert second.source_evidence_count == 10
    assert second.roomplan_record_count == 2
    assert second.raw_mesh_binding_count == 1
    assert second.authority_record_count == 2

    # The same integrity routine is reusable standalone.
    assert repository.verify_persisted_ingestion(plan) == second


def test_verify_persisted_ingestion_rejects_unknown_or_foreign_plan(
    tmp_path: Path,
) -> None:
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads = _plan_and_payloads()
    typed = CaptureIngestionPlan.model_validate(plan)

    with pytest.raises(
        PersistedIngestionIntegrityError,
        match='persisted_ingestion_integrity_mismatch',
    ):
        repository.verify_persisted_ingestion(typed)

    repository.ingest(plan, payloads)

    # A valid plan that shares the lineage projection but carries different
    # metadata derives a different run identity — it was never persisted.
    foreign = json.loads(json.dumps(plan))
    foreign['source_evidence'][0]['producer'] = 'tampered_producer'
    foreign_typed = CaptureIngestionPlan.model_validate(foreign)
    assert foreign_typed.lineage_digest == typed.lineage_digest
    with pytest.raises(
        PersistedIngestionIntegrityError,
        match='ingestion run is not persisted',
    ):
        repository.verify_persisted_ingestion(foreign_typed)

    # A persisted plan_json that diverges from the run identity is a
    # persisted-identity mismatch, not a healthy ingestion.
    run_id = _run_id(repository, typed.lineage_digest)
    _tamper(
        repository.path,
        (
            'UPDATE capture_ingestion_runs SET plan_json=? '
            'WHERE ingestion_run_id=?',
            (
                CaptureIngestionPlan.model_validate(foreign).model_dump_json(),
                run_id,
            ),
        ),
    )
    with pytest.raises(
        PersistedIngestionIntegrityError,
        match='plan identity',
    ):
        repository.verify_persisted_ingestion(typed)


def test_reimport_after_source_link_delete_fails_closed(tmp_path: Path) -> None:
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads = _plan_and_payloads()
    typed = CaptureIngestionPlan.model_validate(plan)
    assert repository.ingest(plan, payloads).created

    run_id = _run_id(repository, typed.lineage_digest)
    victim = typed.source_evidence[0].source_evidence_id
    _tamper(
        repository.path,
        (
            'DELETE FROM capture_ingestion_source_links '
            'WHERE ingestion_run_id=? AND source_evidence_id=?',
            (run_id, victim),
        ),
    )

    with pytest.raises(
        PersistedIngestionIntegrityError,
        match='source evidence link set mismatch',
    ) as excinfo:
        repository.ingest(plan, payloads)
    assert (
        excinfo.value.diagnostic == 'persisted_ingestion_integrity_mismatch'
    )

    # Fail closed: the damaged state is left untouched for repair tooling —
    # no partial repair is committed on failure.
    links = _rows(
        repository.path,
        'SELECT source_evidence_id FROM capture_ingestion_source_links '
        'WHERE ingestion_run_id=?',
        (run_id,),
    )
    assert len(links) == len(typed.source_evidence) - 1
    with pytest.raises(PersistedIngestionIntegrityError):
        repository.verify_persisted_ingestion(typed)


def test_reimport_after_source_row_delete_fails_closed(tmp_path: Path) -> None:
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads = _plan_and_payloads()
    typed = CaptureIngestionPlan.model_validate(plan)
    repository.ingest(plan, payloads)

    victim = typed.source_evidence[0].source_evidence_id
    _tamper(
        repository.path,
        (
            'DELETE FROM capture_source_evidence WHERE source_evidence_id=?',
            (victim,),
        ),
    )

    with pytest.raises(
        PersistedIngestionIntegrityError,
        match='source evidence row is missing',
    ):
        repository.ingest(plan, payloads)


def test_reimport_after_source_payload_delete_fails_closed(
    tmp_path: Path,
) -> None:
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads = _plan_and_payloads()
    typed = CaptureIngestionPlan.model_validate(plan)
    repository.ingest(plan, payloads)

    victim = typed.source_evidence[0]
    _tamper(
        repository.path,
        (
            'DELETE FROM htdt_content_blobs WHERE payload_sha256=?',
            (victim.payload_sha256,),
        ),
    )

    with pytest.raises(
        PersistedIngestionIntegrityError,
        match='source evidence payload is missing or corrupt',
    ):
        repository.ingest(plan, payloads)


def test_reimport_after_source_metadata_corruption_fails_closed(
    tmp_path: Path,
) -> None:
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads = _plan_and_payloads()
    typed = CaptureIngestionPlan.model_validate(plan)
    repository.ingest(plan, payloads)

    victim = typed.source_evidence[0].source_evidence_id
    _tamper(
        repository.path,
        (
            "UPDATE capture_source_evidence SET producer='tampered' "
            'WHERE source_evidence_id=?',
            (victim,),
        ),
    )

    with pytest.raises(
        PersistedIngestionIntegrityError,
        match='source evidence metadata mismatch',
    ):
        repository.ingest(plan, payloads)


def test_reimport_after_roomplan_delete_fails_closed(tmp_path: Path) -> None:
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads = _plan_and_payloads()
    typed = CaptureIngestionPlan.model_validate(plan)
    repository.ingest(plan, payloads)

    run_id = _run_id(repository, typed.lineage_digest)
    _tamper(
        repository.path,
        (
            'DELETE FROM capture_roomplan_records WHERE ingestion_run_id=?',
            (run_id,),
        ),
    )

    with pytest.raises(
        PersistedIngestionIntegrityError,
        match='RoomPlan record set mismatch',
    ):
        repository.ingest(plan, payloads)


def test_reimport_after_mesh_link_delete_fails_closed(tmp_path: Path) -> None:
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads = _plan_and_payloads()
    typed = CaptureIngestionPlan.model_validate(plan)
    repository.ingest(plan, payloads)

    run_id = _run_id(repository, typed.lineage_digest)
    _tamper(
        repository.path,
        (
            'DELETE FROM capture_ingestion_mesh_links '
            'WHERE ingestion_run_id=?',
            (run_id,),
        ),
    )

    with pytest.raises(
        PersistedIngestionIntegrityError,
        match='raw mesh binding link set mismatch',
    ):
        repository.ingest(plan, payloads)


def test_reimport_after_mesh_binding_delete_fails_closed(tmp_path: Path) -> None:
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads = _plan_and_payloads()
    repository.ingest(plan, payloads)

    # The link row survives over the missing binding — a dangling reference.
    _tamper(
        repository.path,
        ('DELETE FROM capture_raw_visual_mesh_bindings', ()),
    )

    with pytest.raises(
        PersistedIngestionIntegrityError,
        match='raw mesh binding is missing',
    ):
        repository.ingest(plan, payloads)


def test_reimport_after_mesh_binding_corruption_fails_closed(
    tmp_path: Path,
) -> None:
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads = _plan_and_payloads()
    repository.ingest(plan, payloads)

    _tamper(
        repository.path,
        (
            "UPDATE capture_raw_visual_mesh_bindings SET payload_json='{}'",
            (),
        ),
    )

    with pytest.raises(
        PersistedIngestionIntegrityError,
        match='raw mesh binding cannot be verified',
    ):
        repository.ingest(plan, payloads)


def test_reimport_after_authority_link_delete_fails_closed(
    tmp_path: Path,
) -> None:
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads = _plan_and_payloads()
    typed = CaptureIngestionPlan.model_validate(plan)
    repository.ingest(plan, payloads)

    run_id = _run_id(repository, typed.lineage_digest)
    _tamper(
        repository.path,
        (
            'DELETE FROM capture_ingestion_authority_links '
            'WHERE ingestion_run_id=?',
            (run_id,),
        ),
    )

    with pytest.raises(
        PersistedIngestionIntegrityError,
        match='authority record link set mismatch',
    ):
        repository.ingest(plan, payloads)


def test_reimport_after_authority_record_delete_fails_closed(
    tmp_path: Path,
) -> None:
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads = _plan_and_payloads()
    repository.ingest(plan, payloads)

    _tamper(
        repository.path,
        ('DELETE FROM capture_authority_records', ()),
    )

    with pytest.raises(
        PersistedIngestionIntegrityError,
        match='authority record is missing',
    ):
        repository.ingest(plan, payloads)


def test_reimport_with_extra_linked_record_fails_closed(tmp_path: Path) -> None:
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads = _plan_and_payloads()
    typed = CaptureIngestionPlan.model_validate(plan)
    repository.ingest(plan, payloads)

    run_id = _run_id(repository, typed.lineage_digest)
    _tamper(
        repository.path,
        (
            'INSERT INTO capture_ingestion_source_links('
            'ingestion_run_id, source_evidence_id) VALUES (?, ?)',
            (run_id, 'e' * 64),
        ),
    )

    with pytest.raises(
        PersistedIngestionIntegrityError,
        match='unexpected',
    ):
        repository.ingest(plan, payloads)


def test_reimport_after_run_metadata_corruption_fails_closed(
    tmp_path: Path,
) -> None:
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads = _plan_and_payloads()
    typed = CaptureIngestionPlan.model_validate(plan)
    repository.ingest(plan, payloads)

    _tamper(
        repository.path,
        (
            "UPDATE capture_ingestion_runs SET ingestor_version='9.9.9' "
            'WHERE lineage_digest=?',
            (typed.lineage_digest,),
        ),
    )

    with pytest.raises(
        PersistedIngestionIntegrityError,
        match='run metadata mismatch',
    ):
        repository.ingest(plan, payloads)


def test_integrity_failure_is_distinguishable_from_invalid_incoming(
    tmp_path: Path,
) -> None:
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads = _plan_and_payloads()
    typed = CaptureIngestionPlan.model_validate(plan)
    repository.ingest(plan, payloads)

    # An invalid incoming archive still raises the untyped transaction error.
    bad = dict(payloads)
    geometry_path = typed.raw_visual_mesh_handoffs[0].geometry_path
    bad[geometry_path] = bad[geometry_path] + b'tamper'
    with pytest.raises(
        CaptureIngestionTransactionError,
        match='byte-count mismatch',
    ) as excinfo:
        repository.ingest(typed, bad)
    assert not isinstance(excinfo.value, PersistedIngestionIntegrityError)

    # Damaged persisted state raises the typed diagnostic instead — an
    # ingestion failure whose machine-readable code names the persisted
    # materialization, not the incoming archive.
    run_id = _run_id(repository, typed.lineage_digest)
    _tamper(
        repository.path,
        (
            'DELETE FROM capture_ingestion_mesh_links '
            'WHERE ingestion_run_id=?',
            (run_id,),
        ),
    )
    with pytest.raises(PersistedIngestionIntegrityError) as excinfo:
        repository.ingest(plan, payloads)
    assert isinstance(excinfo.value, CaptureIngestionTransactionError)
    assert excinfo.value.diagnostic == (
        'persisted_ingestion_integrity_mismatch'
    )


def _plan_with_extra(layer: str) -> dict:
    """A valid v1 plan with one unknown field injected at *layer*."""
    plan, _payloads = _plan_and_payloads()
    if layer == 'plan':
        plan['future_contract_extension'] = True
    elif layer == 'ingestor':
        plan['ingestor']['future_contract_extension'] = True
    elif layer == 'bundle':
        plan['bundle']['future_contract_extension'] = True
    elif layer == 'source_evidence':
        plan['source_evidence'][0]['future_contract_extension'] = True
    elif layer == 'roomplan_records':
        plan['roomplan_records'][0]['future_contract_extension'] = True
    elif layer == 'raw_visual_mesh_handoffs':
        plan['raw_visual_mesh_handoffs'][0][
            'future_contract_extension'
        ] = True
    elif layer == 'mesh_matrix':
        plan['raw_visual_mesh_handoffs'][0]['T_world_from_mesh_anchor'][
            'future_contract_extension'
        ] = True
    elif layer == 'authority_records':
        plan['authority_records'][0]['future_contract_extension'] = True
    else:
        raise AssertionError(layer)
    return plan


@pytest.mark.parametrize(
    'layer',
    (
        'plan',
        'ingestor',
        'bundle',
        'source_evidence',
        'roomplan_records',
        'raw_visual_mesh_handoffs',
        'mesh_matrix',
        'authority_records',
    ),
)
def test_ingestion_plan_rejects_unknown_fields_at_every_layer(
    layer: str,
) -> None:
    """Untrusted plan parsing is fail-closed on unknown contract fields."""

    plan = _plan_with_extra(layer)

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CaptureIngestionPlan.model_validate(plan)


def test_ingestion_plan_extra_field_never_reaches_persisted_plan(
    tmp_path: Path,
) -> None:
    """An unknown field cannot be silently normalized into plan_json."""
    from pydantic import ValidationError

    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    repository = CaptureIngestionRepository(scene)
    plan, payloads = _plan_and_payloads()
    plan['future_contract_extension'] = True

    with pytest.raises(ValidationError):
        repository.ingest(plan, payloads)

    # Nothing was persisted for the rejected plan.
    valid, _ = _plan_and_payloads()
    lineage = CaptureIngestionPlan.model_validate(valid).lineage_digest
    assert not repository.list_ingestion_runs(lineage_digest=lineage)


def test_ingestion_plan_valid_v1_still_accepted(tmp_path: Path) -> None:
    """Baseline: the current v1 contract and re-ingest are unchanged."""
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    repository = CaptureIngestionRepository(scene)
    plan, payloads = _plan_and_payloads()

    typed = CaptureIngestionPlan.model_validate(plan)
    result = repository.ingest(plan, payloads)
    assert result.created

    again = repository.ingest(plan, payloads)
    assert not again.created
    assert again.lineage_digest == typed.lineage_digest
