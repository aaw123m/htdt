from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest
from pydantic import ValidationError

from htdt.cad_document import WorkingDocument
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    EntityLifecycleBinding,
    ProposalEvidenceRef,
    ProposedEntitySpec,
    SystemVariant,
    VariantProvenanceItem,
    apply_system_variant_to_working_document,
    build_system_variant,
    materialize_system_variant,
)
from htdt.cad_system_variant_repository import (
    CadSystemVariantRepository,
    SystemVariantApplication,
)
from htdt.cad_system_variant_lifecycle import (
    CadSystemVariantLifecycleRepository,
    build_system_variant_as_built_record,
)


DOCUMENT_ID = 'o100a-fixture'
NOW = '2026-09-19T00:00:00+00:00'


def _speaker(entity_id: str, role: str, x_m: float, y_m: float, z_m: float) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name=role,
        speaker_role=role,
        position=Position3(x_m=x_m, y_m=y_m, z_m=z_m),
        size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
        aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
    )


def _scene_302() -> SceneDocument:
    return SceneDocument(
        document_id=DOCUMENT_ID,
        room=RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4),
        entities=(
            _speaker('fl', 'FL', 1.2, 0.8, 1.0),
            _speaker('c', 'C', 3.0, 0.6, 0.9),
            _speaker('fr', 'FR', 4.8, 0.8, 1.0),
            _speaker('tfl', 'TFL', 1.8, 2.0, 2.2),
            _speaker('tfr', 'TFR', 4.2, 2.0, 2.2),
            SceneEntity(
                entity_id='mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.2, z_m=1.1),
            ),
        ),
    )


def _roles(*role_ids: str) -> tuple[ChannelRoleBinding, ...]:
    return tuple(
        ChannelRoleBinding(role_id=role, display_name=role)
        for role in role_ids
    )


def _proposal(entity_id: str, role: str, x_m: float) -> ProposedEntitySpec:
    return ProposedEntitySpec(
        spec_id=f'proposal-{entity_id}',
        entity=_speaker(entity_id, role, x_m, 3.0, 1.3),
        role_binding_id=role,
        provenance=(
            VariantProvenanceItem(key='author', value='o100a-test'),
        ),
    )


def _baseline(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(_scene_302(), parent_revision_id=None).revision
    return scene_repository, revision


def test_302_to_proposed_502_is_exact_and_does_not_mutate_baseline(tmp_path: Path) -> None:
    scene_repository, baseline = _baseline(tmp_path)
    before = baseline.document
    sl = _proposal('sl', 'SL', 0.6)
    sr = _proposal('sr', 'SR', 5.4)

    variant = build_system_variant(
        baseline=baseline,
        name='Proposed 5.0.2',
        role_bindings=_roles('FL', 'C', 'FR', 'TFL', 'TFR', 'SL', 'SR'),
        proposed_entities=(sl, sr),
        proposal_evidence=(
            ProposalEvidenceRef(
                evidence_kind='user_decision',
                evidence_id='decision-add-surrounds',
            ),
        ),
        provenance=(VariantProvenanceItem(key='issue', value='#142'),),
        created_at_utc=NOW,
    )
    proposed = materialize_system_variant(baseline, variant)

    assert baseline.document == before
    assert scene_repository.get(baseline.revision_id).document == before
    assert [item.kind for item in variant.diff] == ['add', 'add']
    assert [item.entity_id for item in variant.diff] == ['sl', 'sr']
    assert [entity.speaker_role for entity in proposed.entities if entity.kind == 'speaker'] == [
        'FL', 'C', 'FR', 'TFL', 'TFR', 'SL', 'SR'
    ]
    lifecycle = {item.entity_id: item.state for item in variant.entity_lifecycle}
    assert lifecycle['fl'] == 'current'
    assert lifecycle['sl'] == 'proposed'
    assert lifecycle['sr'] == 'proposed'


def test_add_remove_replace_variant_is_one_working_document_undo_step(tmp_path: Path) -> None:
    _scene_repository, baseline = _baseline(tmp_path)
    replacement_fr = ProposedEntitySpec(
        spec_id='proposal-fr-replacement',
        entity=_speaker('fr', 'FR', 4.5, 1.0, 1.1),
        role_binding_id='FR',
    )
    sl = _proposal('sl', 'SL', 0.6)
    variant = build_system_variant(
        baseline=baseline,
        name='Replace FR, remove C, add SL',
        role_bindings=_roles('FL', 'FR', 'TFL', 'TFR', 'SL'),
        proposed_entities=(replacement_fr, sl),
        remove_entity_ids=('c',),
        created_at_utc=NOW,
    )

    assert [(item.kind, item.entity_id) for item in variant.diff] == [
        ('remove', 'c'),
        ('replace', 'fr'),
        ('add', 'sl'),
    ]

    working = WorkingDocument(
        baseline.document,
        source_revision_id=baseline.revision_id,
        saved_content_hash=baseline.content_hash,
    )
    assert apply_system_variant_to_working_document(
        working,
        baseline=baseline,
        variant=variant,
    )
    assert working.history_length == 1
    assert working.committed_document.entity('fr').position.x_m == pytest.approx(4.5)
    assert working.committed_document.entity('sl').speaker_role == 'SL'
    with pytest.raises(KeyError):
        working.committed_document.entity('c')

    assert working.undo()
    assert working.committed_document == baseline.document


def test_variant_persistence_explicit_apply_and_proposal_lineage(tmp_path: Path) -> None:
    scene_repository, baseline = _baseline(tmp_path)
    repository = CadSystemVariantRepository(scene_repository)
    variant = build_system_variant(
        baseline=baseline,
        name='Proposed 5.0.2',
        role_bindings=_roles('FL', 'C', 'FR', 'TFL', 'TFR', 'SL', 'SR'),
        proposed_entities=(
            _proposal('sl', 'SL', 0.6),
            _proposal('sr', 'SR', 5.4),
        ),
        proposal_evidence=(
            ProposalEvidenceRef(
                evidence_kind='objective',
                evidence_id='objective-evaluation-123',
                detail='reference only; O30 remains objective authority',
            ),
        ),
        created_at_utc=NOW,
    )
    repository.save_variant(variant)

    assert scene_repository.latest(DOCUMENT_ID).revision_id == baseline.revision_id
    comparison_before = repository.comparison_ref(variant.variant_id)
    assert comparison_before.applied_revision_id is None
    assert repository.get_variant(variant.variant_id) == variant

    application = repository.apply_variant(
        variant.variant_id,
        selected_by='explicit-test-selection',
        selected_at_utc='2026-09-19T00:01:00+00:00',
    )
    applied = scene_repository.get(application.applied_revision_id)
    assert applied is not None
    assert applied.parent_revision_id == baseline.revision_id
    assert applied.revision_id != baseline.revision_id
    assert scene_repository.get(baseline.revision_id).document == baseline.document
    assert [entity.speaker_role for entity in applied.document.entities if entity.kind == 'speaker'][-2:] == ['SL', 'SR']

    lineage = repository.proposal_lineage_for_revision(applied.revision_id)
    assert lineage == (application, variant)
    assert lineage[1].proposal_evidence[0].evidence_id == 'objective-evaluation-123'
    comparison_after = repository.comparison_ref(variant.variant_id)
    assert comparison_after.applied_revision_id == applied.revision_id
    assert comparison_after.applied_content_hash == applied.content_hash

    repeated = repository.apply_variant(
        variant.variant_id,
        selected_by='ignored-repeat',
    )
    assert repeated == application
    assert scene_repository.latest(DOCUMENT_ID).revision_id == applied.revision_id


def test_apply_variant_validates_application_before_scene_persistence(tmp_path: Path) -> None:
    scene_repository, baseline = _baseline(tmp_path)
    repository = CadSystemVariantRepository(scene_repository)
    variant = build_system_variant(
        baseline=baseline,
        name='Proposed 5.0.2',
        role_bindings=_roles('FL', 'C', 'FR', 'TFL', 'TFR', 'SL', 'SR'),
        proposed_entities=(
            _proposal('sl', 'SL', 0.6),
            _proposal('sr', 'SR', 5.4),
        ),
        created_at_utc=NOW,
    )
    repository.save_variant(variant)

    with pytest.raises(ValidationError, match='selected_by'):
        repository.apply_variant(
            variant.variant_id,
            selected_by='',
            selected_at_utc='2026-09-19T00:01:00+00:00',
        )

    assert scene_repository.latest(DOCUMENT_ID) == baseline
    assert scene_repository.get(baseline.revision_id) == baseline
    assert repository.application_for_variant(variant.variant_id) is None
    with sqlite3.connect(scene_repository.path) as connection:
        revision_count = connection.execute(
            'SELECT COUNT(*) FROM scene_revisions WHERE document_id=?',
            (DOCUMENT_ID,),
        ).fetchone()[0]
        application_count = connection.execute(
            'SELECT COUNT(*) FROM cad_system_variant_applications WHERE variant_id=?',
            (variant.variant_id,),
        ).fetchone()[0]
    assert revision_count == 1
    assert application_count == 0


def test_application_insert_failure_rolls_back_scene_and_allows_retry(tmp_path: Path) -> None:
    scene_repository, baseline = _baseline(tmp_path)
    repository = CadSystemVariantRepository(scene_repository)
    variant = build_system_variant(
        baseline=baseline,
        name='Proposed 5.0.2',
        role_bindings=_roles('FL', 'C', 'FR', 'TFL', 'TFR', 'SL', 'SR'),
        proposed_entities=(
            _proposal('sl', 'SL', 0.6),
            _proposal('sr', 'SR', 5.4),
        ),
        created_at_utc=NOW,
    )
    repository.save_variant(variant)

    recovery_document = WorkingDocument(
        baseline.document,
        source_revision_id=baseline.revision_id,
        saved_content_hash=baseline.content_hash,
    )
    assert recovery_document.move_entity(
        'fl',
        Position3(x_m=1.25, y_m=0.8, z_m=1.0),
    )
    recovery_before = scene_repository.save_recovery(
        recovery_document.committed_document,
        source_revision_id=baseline.revision_id,
    )
    assert recovery_before is not None

    with sqlite3.connect(scene_repository.path) as connection:
        connection.execute(
            '''
            CREATE TRIGGER fail_system_variant_application_insert
            BEFORE INSERT ON cad_system_variant_applications
            BEGIN
                SELECT RAISE(ABORT, 'injected application insert failure');
            END
            '''
        )

    with pytest.raises(sqlite3.DatabaseError, match='injected application insert failure'):
        repository.apply_variant(
            variant.variant_id,
            selected_by='failure-injection',
            selected_at_utc='2026-09-19T00:01:00+00:00',
        )

    assert scene_repository.latest(DOCUMENT_ID) == baseline
    assert scene_repository.get(baseline.revision_id) == baseline
    assert scene_repository.recovery(DOCUMENT_ID) == recovery_before
    assert repository.application_for_variant(variant.variant_id) is None
    assert repository.proposal_lineage_for_revision(baseline.revision_id) is None
    with sqlite3.connect(scene_repository.path) as connection:
        assert connection.execute(
            'SELECT COUNT(*) FROM scene_revisions WHERE document_id=?',
            (DOCUMENT_ID,),
        ).fetchone()[0] == 1
        assert connection.execute(
            'SELECT COUNT(*) FROM cad_system_variant_applications WHERE variant_id=?',
            (variant.variant_id,),
        ).fetchone()[0] == 0
        connection.execute('DROP TRIGGER fail_system_variant_application_insert')

    application = repository.apply_variant(
        variant.variant_id,
        selected_by='retry-after-failure',
        selected_at_utc='2026-09-19T00:02:00+00:00',
    )
    applied = scene_repository.get(application.applied_revision_id)
    assert applied is not None
    assert applied.parent_revision_id == baseline.revision_id
    assert scene_repository.get(baseline.revision_id) == baseline
    assert scene_repository.recovery(DOCUMENT_ID) is None
    assert repository.proposal_lineage_for_revision(application.applied_revision_id) == (
        application,
        variant,
    )
    with sqlite3.connect(scene_repository.path) as connection:
        assert connection.execute(
            'SELECT COUNT(*) FROM scene_revisions WHERE document_id=?',
            (DOCUMENT_ID,),
        ).fetchone()[0] == 2
        assert connection.execute(
            'SELECT COUNT(*) FROM cad_system_variant_applications WHERE variant_id=?',
            (variant.variant_id,),
        ).fetchone()[0] == 1


def test_proposed_lifecycle_cannot_claim_measurement_evidence() -> None:
    with pytest.raises(ValidationError, match='measurement evidence is valid only for measured'):
        EntityLifecycleBinding(
            entity_id='sl',
            state='proposed',
            measurement_ids=('fake-measurement',),
        )

    measured = EntityLifecycleBinding(
        entity_id='fl',
        state='measured',
        measurement_ids=('real-measurement-id',),
    )
    assert measured.state == 'measured'

def test_persisted_variant_rejects_apply_after_baseline_becomes_stale(tmp_path: Path) -> None:
    scene_repository, baseline = _baseline(tmp_path)
    repository = CadSystemVariantRepository(scene_repository)
    variant = build_system_variant(
        baseline=baseline,
        name='Proposed 5.0.2',
        role_bindings=_roles('FL', 'C', 'FR', 'TFL', 'TFR', 'SL', 'SR'),
        proposed_entities=(
            _proposal('sl', 'SL', 0.6),
            _proposal('sr', 'SR', 5.4),
        ),
        created_at_utc=NOW,
    )
    repository.save_variant(variant)

    working = WorkingDocument(
        baseline.document,
        source_revision_id=baseline.revision_id,
        saved_content_hash=baseline.content_hash,
    )
    assert working.move_entity('fl', Position3(x_m=1.3, y_m=0.8, z_m=1.0))
    newer = scene_repository.save(
        working.committed_document,
        parent_revision_id=baseline.revision_id,
    ).revision

    with pytest.raises(ValueError, match='stale baseline SceneRevision'):
        repository.apply_variant(
            variant.variant_id,
            selected_by='stale-test',
        )

    assert scene_repository.latest(DOCUMENT_ID).revision_id == newer.revision_id
    assert repository.application_for_variant(variant.variant_id) is None



def _applied_502_fixture(tmp_path: Path):
    scene_repository, baseline = _baseline(tmp_path)
    variant_repository = CadSystemVariantRepository(scene_repository)
    variant = build_system_variant(
        baseline=baseline,
        name='Proposed 5.0.2 lifecycle',
        role_bindings=_roles('FL', 'C', 'FR', 'TFL', 'TFR', 'SL', 'SR'),
        proposed_entities=(
            _proposal('sl', 'SL', 0.6),
            _proposal('sr', 'SR', 5.4),
        ),
        proposal_evidence=(
            ProposalEvidenceRef(
                evidence_kind='user_decision',
                evidence_id='selected-proposal-fixture',
            ),
        ),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant)
    application = variant_repository.apply_variant(
        variant.variant_id,
        selected_by='fixture-selection',
        selected_at_utc='2026-09-20T00:00:00+00:00',
    )
    applied = scene_repository.get(application.applied_revision_id)
    assert applied is not None
    return scene_repository, variant_repository, variant, application, applied


def test_proposal_lineage_survives_descendant_scene_edits(tmp_path: Path) -> None:
    (
        scene_repository,
        variant_repository,
        variant,
        application,
        applied,
    ) = _applied_502_fixture(tmp_path)

    edited_entities = tuple(
        entity.model_copy(
            update={
                'position': entity.position.model_copy(update={'x_m': 0.72})
            }
        )
        if entity.entity_id == 'sl'
        else entity
        for entity in applied.document.entities
    )
    descendant = scene_repository.save(
        applied.document.model_copy(update={'entities': edited_entities}),
        parent_revision_id=applied.revision_id,
    ).revision

    assert descendant.revision_id != applied.revision_id
    assert (
        variant_repository.proposal_lineage_for_revision(descendant.revision_id)
        == (application, variant)
    )
    assert variant.proposed_entities[0].entity.position.x_m == pytest.approx(0.6)
    assert descendant.document.entity('sl').position.x_m == pytest.approx(0.72)


def test_explicit_as_built_record_uses_descendant_without_rewriting_proposal(
    tmp_path: Path,
) -> None:
    (
        scene_repository,
        variant_repository,
        variant,
        application,
        applied,
    ) = _applied_502_fixture(tmp_path)

    edited_entities = tuple(
        entity.model_copy(
            update={
                'position': entity.position.model_copy(update={'x_m': 0.72})
            }
        )
        if entity.entity_id == 'sl'
        else entity
        for entity in applied.document.entities
    )
    as_built_revision = scene_repository.save(
        applied.document.model_copy(update={'entities': edited_entities}),
        parent_revision_id=applied.revision_id,
    ).revision

    record = build_system_variant_as_built_record(
        scene_repository=scene_repository,
        variant_repository=variant_repository,
        application=application,
        variant=variant,
        as_built_revision=as_built_revision,
        confirmed_by='installer-fixture',
        confirmed_at_utc='2026-09-20T00:10:00+00:00',
        notes=('SL moved 0.12 m during installation',),
    )

    assert [item.revision_id for item in record.revision_lineage] == [
        applied.revision_id,
        as_built_revision.revision_id,
    ]
    assert {item.entity_id for item in record.entity_lifecycle} == {'sl', 'sr'}
    assert all(item.state == 'as_built' for item in record.entity_lifecycle)
    assert variant.proposed_entities[0].entity.position.x_m == pytest.approx(0.6)
    assert as_built_revision.document.entity('sl').position.x_m == pytest.approx(
        0.72
    )

    lifecycle_repository = CadSystemVariantLifecycleRepository(
        scene_repository=scene_repository,
        variant_repository=variant_repository,
    )
    lifecycle_repository.save(record)

    reopened_scene = SceneRepository(scene_repository.path)
    reopened_variants = CadSystemVariantRepository(reopened_scene)
    reopened_lifecycle = CadSystemVariantLifecycleRepository(
        scene_repository=reopened_scene,
        variant_repository=reopened_variants,
    )
    assert reopened_lifecycle.get(record.record_id) == record
    assert (
        reopened_lifecycle.for_application(application.application_id)
        == record
    )


def test_as_built_promotion_rejects_missing_proposed_entity(tmp_path: Path) -> None:
    (
        scene_repository,
        variant_repository,
        variant,
        application,
        applied,
    ) = _applied_502_fixture(tmp_path)

    without_sl = applied.document.model_copy(
        update={
            'entities': tuple(
                item
                for item in applied.document.entities
                if item.entity_id != 'sl'
            )
        }
    )
    incomplete_revision = scene_repository.save(
        without_sl,
        parent_revision_id=applied.revision_id,
    ).revision

    with pytest.raises(
        ValueError,
        match='missing proposed entity: sl',
    ):
        build_system_variant_as_built_record(
            scene_repository=scene_repository,
            variant_repository=variant_repository,
            application=application,
            variant=variant,
            as_built_revision=incomplete_revision,
            confirmed_by='installer-fixture',
            confirmed_at_utc='2026-09-20T00:10:00+00:00',
        )


def _identity_digest(value: Any) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()


def _saved_502_variant(tmp_path: Path):
    scene_repository, baseline = _baseline(tmp_path)
    repository = CadSystemVariantRepository(scene_repository)
    variant = build_system_variant(
        baseline=baseline,
        name='Proposed 5.0.2',
        role_bindings=_roles('FL', 'C', 'FR', 'TFL', 'TFR', 'SL', 'SR'),
        proposed_entities=(
            _proposal('sl', 'SL', 0.6),
            _proposal('sr', 'SR', 5.4),
        ),
        created_at_utc=NOW,
    )
    repository.save_variant(variant)
    return scene_repository, repository, baseline, variant


def _rewrite_variant_row(
    scene_repository: SceneRepository,
    variant: SystemVariant,
    **updates: Any,
) -> SystemVariant:
    """Coherently rewrite one persisted variant row; its hash stays valid."""

    tampered = variant.model_copy(update=updates)
    tampered = tampered.model_copy(
        update={'variant_sha256': _identity_digest(tampered.identity_payload())}
    )
    tampered = SystemVariant.model_validate(tampered.model_dump(mode='python'))
    with sqlite3.connect(scene_repository.path) as connection:
        connection.execute(
            '''
            UPDATE cad_system_variants SET
                document_id=?, baseline_revision_id=?, baseline_content_hash=?,
                parent_variant_id=?, variant_sha256=?, payload_json=?,
                created_at_utc=?
            WHERE variant_id=?
            ''',
            (
                tampered.document_id,
                tampered.baseline_revision_id,
                tampered.baseline_content_hash,
                tampered.parent_variant_id,
                tampered.variant_sha256,
                tampered.model_dump_json(),
                tampered.created_at_utc,
                tampered.variant_id,
            ),
        )
    return tampered


def _rewrite_application_row(
    scene_repository: SceneRepository,
    application: SystemVariantApplication,
    **updates: Any,
) -> SystemVariantApplication:
    """Coherently rewrite one persisted application row; its hash stays valid."""

    tampered = application.model_copy(update=updates)
    tampered = tampered.model_copy(
        update={
            'application_sha256': _identity_digest(tampered.identity_payload())
        }
    )
    tampered = SystemVariantApplication.model_validate(
        tampered.model_dump(mode='python')
    )
    with sqlite3.connect(scene_repository.path) as connection:
        connection.execute(
            '''
            UPDATE cad_system_variant_applications SET
                variant_id=?, document_id=?, baseline_revision_id=?,
                applied_revision_id=?, application_sha256=?, payload_json=?,
                selected_at_utc=?
            WHERE application_id=?
            ''',
            (
                tampered.variant_id,
                tampered.document_id,
                tampered.baseline_revision_id,
                tampered.applied_revision_id,
                tampered.application_sha256,
                tampered.model_dump_json(),
                tampered.selected_at_utc,
                tampered.application_id,
            ),
        )
    return tampered


def test_variant_read_rejects_missing_baseline_revision(tmp_path: Path) -> None:
    scene_repository, repository, baseline, variant = _saved_502_variant(tmp_path)

    # Direct tamper simulates an authority that cannot be re-resolved after
    # reopen. SQLite foreign keys are intentionally not enabled on this raw
    # connection.
    with sqlite3.connect(scene_repository.path) as connection:
        connection.execute(
            'DELETE FROM scene_revisions WHERE revision_id=?',
            (baseline.revision_id,),
        )

    with pytest.raises(
        ValueError, match='baseline SceneRevision does not exist'
    ):
        repository.get_variant(variant.variant_id)
    with pytest.raises(
        ValueError, match='baseline SceneRevision does not exist'
    ):
        repository.list_variants(DOCUMENT_ID)


def test_variant_read_rejects_coherently_rewritten_baseline(tmp_path: Path) -> None:
    scene_repository, repository, _baseline_revision, variant = (
        _saved_502_variant(tmp_path)
    )
    _rewrite_variant_row(
        scene_repository,
        variant,
        baseline_content_hash='f' * 64,
    )

    with pytest.raises(ValueError, match='baseline authority mismatch'):
        repository.get_variant(variant.variant_id)
    with pytest.raises(ValueError, match='baseline authority mismatch'):
        repository.list_variants(DOCUMENT_ID)


def test_variant_read_rejects_row_payload_divergence(tmp_path: Path) -> None:
    scene_repository, repository, _baseline_revision, variant = (
        _saved_502_variant(tmp_path)
    )
    with sqlite3.connect(scene_repository.path) as connection:
        connection.execute(
            'UPDATE cad_system_variants SET baseline_content_hash=? '
            'WHERE variant_id=?',
            ('0' * 64, variant.variant_id),
        )

    with pytest.raises(ValueError, match='row disagrees with its payload'):
        repository.get_variant(variant.variant_id)
    with pytest.raises(ValueError, match='row disagrees with its payload'):
        repository.list_variants(DOCUMENT_ID)


def test_variant_read_rejects_payload_that_no_longer_materializes(
    tmp_path: Path,
) -> None:
    scene_repository, repository, _baseline_revision, variant = (
        _saved_502_variant(tmp_path)
    )
    moved = variant.proposed_entities[0].model_copy(
        update={'entity': _speaker('sl', 'SL', 0.9, 3.0, 1.3)}
    )
    _rewrite_variant_row(
        scene_repository,
        variant,
        proposed_entities=(moved,) + variant.proposed_entities[1:],
    )

    with pytest.raises(
        ValueError, match='does not match exact variant diff'
    ):
        repository.get_variant(variant.variant_id)


def test_variant_read_rejects_dangling_parent_variant(tmp_path: Path) -> None:
    scene_repository, repository, baseline, parent = _saved_502_variant(tmp_path)
    child = build_system_variant(
        baseline=baseline,
        name='Child proposal',
        role_bindings=_roles('FL', 'C', 'FR', 'TFL', 'TFR', 'SL', 'SR', 'SBL'),
        proposed_entities=(_proposal('sbl', 'SBL', 1.9),),
        parent_variant_id=parent.variant_id,
        created_at_utc=NOW,
    )
    repository.save_variant(child)
    assert repository.get_variant(child.variant_id) == child

    with sqlite3.connect(scene_repository.path) as connection:
        connection.execute(
            'DELETE FROM cad_system_variants WHERE variant_id=?',
            (parent.variant_id,),
        )

    assert repository.get_variant(parent.variant_id) is None
    with pytest.raises(ValueError, match='parent variant does not exist'):
        repository.get_variant(child.variant_id)
    with pytest.raises(ValueError, match='parent variant does not exist'):
        repository.list_variants(DOCUMENT_ID)


def test_variant_read_rejects_parent_from_another_document(tmp_path: Path) -> None:
    scene_repository, repository, _baseline_revision, variant = (
        _saved_502_variant(tmp_path)
    )
    other_baseline = scene_repository.save(
        _scene_302().model_copy(update={'document_id': 'o100a-other-document'}),
        parent_revision_id=None,
    ).revision
    other_variant = build_system_variant(
        baseline=other_baseline,
        name='Other document proposal',
        role_bindings=_roles('FL', 'C', 'FR', 'TFL', 'TFR', 'SL', 'SR'),
        proposed_entities=(
            _proposal('sl', 'SL', 0.6),
            _proposal('sr', 'SR', 5.4),
        ),
        created_at_utc=NOW,
    )
    repository.save_variant(other_variant)

    _rewrite_variant_row(
        scene_repository,
        variant,
        parent_variant_id=other_variant.variant_id,
    )
    with pytest.raises(ValueError, match='parent belongs to another document'):
        repository.get_variant(variant.variant_id)
    with pytest.raises(ValueError, match='parent belongs to another document'):
        repository.list_variants(DOCUMENT_ID)


def test_variant_read_rejects_cyclical_parent_lineage(tmp_path: Path) -> None:
    scene_repository, repository, baseline, parent = _saved_502_variant(tmp_path)
    child = build_system_variant(
        baseline=baseline,
        name='Child proposal',
        role_bindings=_roles('FL', 'C', 'FR', 'TFL', 'TFR', 'SL', 'SR', 'SBL'),
        proposed_entities=(_proposal('sbl', 'SBL', 1.9),),
        parent_variant_id=parent.variant_id,
        created_at_utc=NOW,
    )
    repository.save_variant(child)

    # Coherently rewrite the parent so its lineage points back at its child.
    _rewrite_variant_row(
        scene_repository,
        parent,
        parent_variant_id=child.variant_id,
    )
    with pytest.raises(
        ValueError, match='parent lineage contains a cycle'
    ):
        repository.get_variant(parent.variant_id)
    with pytest.raises(
        ValueError, match='parent lineage contains a cycle'
    ):
        repository.get_variant(child.variant_id)


def test_application_reads_replay_full_authority_unchanged(tmp_path: Path) -> None:
    (
        scene_repository,
        variant_repository,
        variant,
        application,
        applied,
    ) = _applied_502_fixture(tmp_path)

    assert (
        variant_repository.get_application(application.application_id)
        == application
    )
    assert (
        variant_repository.application_for_variant(variant.variant_id)
        == application
    )
    assert (
        variant_repository.application_for_revision(applied.revision_id)
        == application
    )
    assert variant_repository.get_variant(variant.variant_id) == variant
    assert variant_repository.list_variants(DOCUMENT_ID) == (variant,)
    assert (
        variant_repository.proposal_lineage_for_revision(applied.revision_id)
        == (application, variant)
    )
    assert (
        variant_repository.comparison_ref(variant.variant_id).applied_revision_id
        == applied.revision_id
    )

    reopened_scene = SceneRepository(scene_repository.path)
    reopened = CadSystemVariantRepository(reopened_scene)
    assert reopened.get_variant(variant.variant_id) == variant
    assert reopened.list_variants(DOCUMENT_ID) == (variant,)
    assert reopened.get_application(application.application_id) == application
    assert reopened.application_for_revision(applied.revision_id) == application


def test_application_read_rejects_dangling_variant(tmp_path: Path) -> None:
    (
        scene_repository,
        variant_repository,
        variant,
        application,
        applied,
    ) = _applied_502_fixture(tmp_path)

    with sqlite3.connect(scene_repository.path) as connection:
        connection.execute(
            'DELETE FROM cad_system_variants WHERE variant_id=?',
            (variant.variant_id,),
        )

    with pytest.raises(ValueError, match='references missing variant'):
        variant_repository.get_application(application.application_id)
    with pytest.raises(ValueError, match='references missing variant'):
        variant_repository.application_for_variant(variant.variant_id)
    with pytest.raises(ValueError, match='references missing variant'):
        variant_repository.proposal_lineage_for_revision(applied.revision_id)


def test_application_read_rejects_dangling_applied_revision(tmp_path: Path) -> None:
    (
        scene_repository,
        variant_repository,
        variant,
        application,
        applied,
    ) = _applied_502_fixture(tmp_path)

    with sqlite3.connect(scene_repository.path) as connection:
        connection.execute(
            'DELETE FROM scene_revisions WHERE revision_id=?',
            (applied.revision_id,),
        )

    with pytest.raises(
        ValueError, match='applied SceneRevision does not exist'
    ):
        variant_repository.get_application(application.application_id)
    with pytest.raises(
        ValueError, match='applied SceneRevision does not exist'
    ):
        variant_repository.application_for_variant(variant.variant_id)
    with pytest.raises(
        ValueError, match='applied SceneRevision does not exist'
    ):
        variant_repository.application_for_revision(applied.revision_id)


def test_application_read_rejects_dangling_baseline_revision(tmp_path: Path) -> None:
    (
        scene_repository,
        variant_repository,
        variant,
        application,
        _applied,
    ) = _applied_502_fixture(tmp_path)

    with sqlite3.connect(scene_repository.path) as connection:
        connection.execute(
            'DELETE FROM scene_revisions WHERE revision_id=?',
            (application.baseline_revision_id,),
        )

    with pytest.raises(
        ValueError, match='baseline SceneRevision does not exist'
    ):
        variant_repository.get_application(application.application_id)
    with pytest.raises(
        ValueError, match='baseline SceneRevision does not exist'
    ):
        variant_repository.application_for_variant(variant.variant_id)


def test_application_read_rejects_row_payload_divergence(tmp_path: Path) -> None:
    (
        scene_repository,
        variant_repository,
        _variant,
        application,
        _applied,
    ) = _applied_502_fixture(tmp_path)

    with sqlite3.connect(scene_repository.path) as connection:
        connection.execute(
            'UPDATE cad_system_variant_applications SET document_id=? '
            'WHERE application_id=?',
            ('other-document', application.application_id),
        )

    with pytest.raises(ValueError, match='row disagrees with its payload'):
        variant_repository.get_application(application.application_id)


def test_application_read_rejects_foreign_variant_hash(tmp_path: Path) -> None:
    (
        scene_repository,
        variant_repository,
        _variant,
        application,
        _applied,
    ) = _applied_502_fixture(tmp_path)

    _rewrite_application_row(
        scene_repository,
        application,
        variant_sha256='0' * 64,
    )
    with pytest.raises(ValueError, match='variant authority mismatch'):
        variant_repository.get_application(application.application_id)


def test_application_read_rejects_applied_revision_with_wrong_parent(
    tmp_path: Path,
) -> None:
    (
        scene_repository,
        variant_repository,
        _variant,
        application,
        applied,
    ) = _applied_502_fixture(tmp_path)

    descendant = scene_repository.save(
        applied.document.model_copy(
            update={
                'entities': tuple(
                    entity.model_copy(
                        update={
                            'position': entity.position.model_copy(
                                update={'x_m': 0.72}
                            )
                        }
                    )
                    if entity.entity_id == 'sl'
                    else entity
                    for entity in applied.document.entities
                )
            }
        ),
        parent_revision_id=applied.revision_id,
    ).revision

    _rewrite_application_row(
        scene_repository,
        application,
        applied_revision_id=descendant.revision_id,
        applied_content_hash=descendant.content_hash,
    )
    with pytest.raises(
        ValueError, match='applied SceneRevision mismatch'
    ):
        variant_repository.get_application(application.application_id)


def test_application_read_rejects_unreproduced_applied_revision(
    tmp_path: Path,
) -> None:
    (
        scene_repository,
        variant_repository,
        _variant,
        application,
        _applied,
    ) = _applied_502_fixture(tmp_path)
    baseline = scene_repository.get(application.baseline_revision_id)
    assert baseline is not None

    sibling = scene_repository.save(
        baseline.document.model_copy(
            update={
                'entities': tuple(
                    entity.model_copy(
                        update={
                            'position': entity.position.model_copy(
                                update={'x_m': 1.31}
                            )
                        }
                    )
                    if entity.entity_id == 'fl'
                    else entity
                    for entity in baseline.document.entities
                )
            }
        ),
        parent_revision_id=baseline.revision_id,
    ).revision

    _rewrite_application_row(
        scene_repository,
        application,
        applied_revision_id=sibling.revision_id,
        applied_content_hash=sibling.content_hash,
    )
    with pytest.raises(
        ValueError, match='does not reproduce the applied SceneRevision'
    ):
        variant_repository.get_application(application.application_id)
