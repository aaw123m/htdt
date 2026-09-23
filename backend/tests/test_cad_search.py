from __future__ import annotations

import json
import sqlite3

import pytest

from htdt.cad_constraint_models import (
    CadConstraintPoint2D,
    CadConstraintSet,
    CadExclusionRegionConstraint,
)
from htdt.cad_document import WorkingDocument
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, RoomPrism, SceneDocument, SceneEntity, Size3
from htdt.cad_search import (
    apply_candidate_positions,
    build_cad_search_spec,
    candidate_preview_document,
    generate_cad_candidates,
    search_spec_current,
    search_spec_current_working,
)
from htdt.cad_search_models import (
    CadSearchAxis,
    CadSearchSpec,
    canonical_search_json,
    canonical_search_sha256,
)
from htdt.cad_search_repository import CadSearchRepository


DOCUMENT_ID = 'n80-search-fixture'


def _scene(*, speaker_x: float = 1.0) -> SceneDocument:
    return SceneDocument(
        document_id=DOCUMENT_ID,
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                position=Position3(x_m=speaker_x, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.22, y_m=0.28, z_m=0.42),
                speaker_role='FL',
            ),
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )


def _constraints(*, center_x: float = 2.0) -> CadConstraintSet:
    half = 0.2
    return CadConstraintSet(
        document_id=DOCUMENT_ID,
        constraints=(
            CadExclusionRegionConstraint(
                constraint_id='rack-zone',
                name='Rack exclusion',
                entity_ids=('speaker-fl',),
                vertices=(
                    CadConstraintPoint2D(x_m=center_x - half, y_m=0.8),
                    CadConstraintPoint2D(x_m=center_x + half, y_m=0.8),
                    CadConstraintPoint2D(x_m=center_x + half, y_m=1.2),
                    CadConstraintPoint2D(x_m=center_x - half, y_m=1.2),
                ),
            ),
        ),
    )


def _build(repository: SceneRepository):
    revision = repository.save(_scene(), parent_revision_id=None).revision
    spec, estimate = build_cad_search_spec(
        revision,
        _constraints(),
        (CadSearchAxis(entity_id='speaker-fl', axis='x', min_m=1.0, max_m=3.0, step_m=1.0),),
        candidate_limit=10,
        name='FL X sweep',
    )
    return revision, spec, estimate


def _rebound(spec: CadSearchSpec, **updates) -> CadSearchSpec:
    """Rehash a spec so tampered payloads keep self-consistent hashes."""

    tampered = spec.model_copy(update=updates)
    return tampered.model_copy(update={
        'search_spec_sha256': canonical_search_sha256(tampered.identity_payload()),
    })


def test_native_search_spec_round_trip_and_deterministic_generation(tmp_path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision, spec, estimate = _build(scene_repository)
    search_repository = CadSearchRepository(scene_repository)

    assert estimate['raw_candidate_count'] == 3
    search_repository.save(spec)
    loaded = search_repository.get(spec.search_spec_id)
    assert loaded == spec
    assert search_repository.list_specs(DOCUMENT_ID) == (spec,)

    first = generate_cad_candidates(scene_repository, loaded)
    second = generate_cad_candidates(scene_repository, loaded)

    assert first.raw_candidate_count == 3
    assert first.feasible_candidate_count == 2
    assert first.rejected_candidate_count == 1
    assert first.duplicate_candidate_count == 0
    assert first.candidate_set_sha256 == second.candidate_set_sha256
    assert [item.candidate_id for item in first.candidates] == [item.candidate_id for item in second.candidates]
    assert [item.positions['speaker-fl']['x_m'] for item in first.candidates] == [1.0, 3.0]
    assert search_spec_current(spec, revision, _constraints())


def test_search_spec_keeps_constraint_snapshot_but_current_apply_gate_detects_stale(tmp_path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision, spec, _ = _build(scene_repository)
    original = generate_cad_candidates(scene_repository, spec)

    changed_constraints = _constraints(center_x=4.0)
    assert not search_spec_current(spec, revision, changed_constraints)
    regenerated = generate_cad_candidates(scene_repository, spec)
    assert regenerated.candidate_set_sha256 == original.candidate_set_sha256

    changed_scene = _scene(speaker_x=1.1)
    newer_revision = scene_repository.save(changed_scene, parent_revision_id=revision.revision_id).revision
    assert not search_spec_current(spec, newer_revision, _constraints())


def test_candidate_preview_is_non_authoritative_and_apply_is_one_undo(tmp_path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision, spec, _ = _build(scene_repository)
    page = generate_cad_candidates(scene_repository, spec)
    candidate = page.candidates[-1]
    constraints = _constraints()
    working = WorkingDocument(
        revision.document,
        source_revision_id=revision.revision_id,
        saved_content_hash=revision.content_hash,
    )

    preview = candidate_preview_document(working.committed_document, candidate)
    assert preview.entity('speaker-fl').position.x_m == 3.0
    assert working.committed_document.entity('speaker-fl').position.x_m == 1.0
    assert working.history_length == 0
    assert not working.is_dirty
    assert search_spec_current_working(spec, working, constraints)

    assert apply_candidate_positions(working, candidate, spec=spec, current_constraint_set=constraints)
    assert working.committed_document.entity('speaker-fl').position.x_m == 3.0
    assert working.history_length == 1
    assert working.is_dirty

    assert working.undo()
    assert working.committed_document.entity('speaker-fl').position.x_m == 1.0
    assert not working.is_dirty


def test_candidate_apply_rejects_dirty_or_constraint_stale_working_state(tmp_path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision, spec, _ = _build(scene_repository)
    candidate = generate_cad_candidates(scene_repository, spec).candidates[-1]
    working = WorkingDocument(
        revision.document,
        source_revision_id=revision.revision_id,
        saved_content_hash=revision.content_hash,
    )

    working.move_entity('speaker-fl', Position3(x_m=1.2, y_m=1.0, z_m=1.0))
    with pytest.raises(ValueError, match='stale SearchSpec'):
        apply_candidate_positions(working, candidate, spec=spec, current_constraint_set=_constraints())

    working.undo()
    with pytest.raises(ValueError, match='stale SearchSpec'):
        apply_candidate_positions(
            working,
            candidate,
            spec=spec,
            current_constraint_set=_constraints(center_x=4.0),
        )


def test_room_boundary_only_search_allows_empty_explicit_constraint_workspace(tmp_path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(_scene(), parent_revision_id=None).revision
    spec, estimate = build_cad_search_spec(
        revision,
        CadConstraintSet(document_id=DOCUMENT_ID, constraints=()),
        (CadSearchAxis(entity_id='speaker-fl', axis='x', min_m=0.0, max_m=1.0, step_m=1.0),),
        candidate_limit=10,
        name='Room-boundary-only sweep',
    )

    assert estimate['raw_candidate_count'] == 2
    page = generate_cad_candidates(scene_repository, spec)
    assert page.raw_candidate_count == 2
    assert page.feasible_candidate_count == 1
    assert page.rejected_candidate_count == 1
    assert page.candidates[0].positions['speaker-fl']['x_m'] == 1.0
    assert any(key.startswith('__room_boundary__:') for key in page.rejection_counts)


def test_candidate_generation_honors_cancellation_hook(tmp_path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    _revision, spec, _estimate = _build(scene_repository)

    with pytest.raises(RuntimeError, match='search generation cancelled'):
        generate_cad_candidates(scene_repository, spec, cancelled=lambda: True)


def test_working_search_spec_rejects_current_document_switch(tmp_path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision, spec, _ = _build(scene_repository)
    working = WorkingDocument(
        revision.document,
        source_revision_id=revision.revision_id,
        saved_content_hash=revision.content_hash,
    )
    candidate = generate_cad_candidates(scene_repository, spec).candidates[-1]

    assert search_spec_current_working(
        spec,
        working,
        _constraints(),
        current_document_id=DOCUMENT_ID,
    )
    assert not search_spec_current_working(
        spec,
        working,
        _constraints(),
        current_document_id='other-document',
    )
    with pytest.raises(ValueError, match='stale SearchSpec'):
        apply_candidate_positions(
            working,
            candidate,
            spec=spec,
            current_constraint_set=_constraints(),
            current_document_id='other-document',
        )


def test_search_spec_identity_binds_executable_o10_and_engine_payloads(tmp_path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    _revision, spec, _estimate = _build(scene_repository)

    # Altering o10_spec_json alone breaks the outer identity hash.
    o10_spec = json.loads(spec.o10_spec_json)
    o10_spec['candidate_limit'] = 99
    with pytest.raises(ValueError, match='identity hash mismatch'):
        CadSearchSpec.model_validate({
            **spec.model_dump(mode='json'),
            'o10_spec_json': canonical_search_json(o10_spec),
        })

    # Altering the engine spec and recomputing only its local SHA still
    # breaks the outer identity hash.
    engine_spec = json.loads(spec.constraint_engine_spec_json)
    engine_spec['constraints'] = []
    with pytest.raises(ValueError, match='identity hash mismatch'):
        CadSearchSpec.model_validate({
            **spec.model_dump(mode='json'),
            'constraint_engine_spec_json': canonical_search_json(engine_spec),
            'constraint_engine_spec_sha256': canonical_search_sha256(engine_spec),
        })

    # The executable payloads must be canonical JSON, not just parseable.
    o10_padded = json.dumps(json.loads(spec.o10_spec_json), indent=2)
    with pytest.raises(ValueError, match='canonical JSON'):
        CadSearchSpec.model_validate({
            **spec.model_dump(mode='json'),
            'o10_spec_json': o10_padded,
        })


def test_search_repository_rejects_noncanonical_payloads_on_save_and_read(tmp_path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    _revision, spec, _estimate = _build(scene_repository)
    search_repository = CadSearchRepository(scene_repository)
    search_repository.save(spec)

    # Canonical spec round-trips unchanged.
    assert search_repository.get(spec.search_spec_id) == spec
    assert search_repository.list_specs(DOCUMENT_ID) == (spec,)

    # A coherently rehashed o10 payload is rejected on save.
    o10_spec = json.loads(spec.o10_spec_json)
    o10_spec['candidate_limit'] = 99
    tampered_o10 = _rebound(spec, o10_spec_json=canonical_search_json(o10_spec))
    with pytest.raises(ValueError, match='canonical compilation'):
        search_repository.save(tampered_o10)

    # A coherently rehashed engine payload is rejected on save.
    engine_spec = json.loads(spec.constraint_engine_spec_json)
    engine_spec['constraints'] = []
    tampered_engine = _rebound(
        spec,
        constraint_engine_spec_json=canonical_search_json(engine_spec),
        constraint_engine_spec_sha256=canonical_search_sha256(engine_spec),
    )
    with pytest.raises(ValueError, match='canonical compilation'):
        search_repository.save(tampered_engine)

    # Fully self-consistent tampered rows (payload + hash column rewritten
    # together) still fail closed on every authoritative read.
    for tampered in (tampered_o10, tampered_engine):
        with sqlite3.connect(search_repository.path) as connection:
            connection.execute(
                'UPDATE cad_search_specs SET payload_json=?, search_spec_sha256=? '
                'WHERE search_spec_id=?',
                (
                    tampered.model_dump_json(),
                    tampered.search_spec_sha256,
                    spec.search_spec_id,
                ),
            )
        with pytest.raises(ValueError, match='canonical compilation'):
            search_repository.get(spec.search_spec_id)
        with pytest.raises(ValueError, match='canonical compilation'):
            search_repository.list_specs(DOCUMENT_ID)
        with sqlite3.connect(search_repository.path) as connection:
            connection.execute(
                'UPDATE cad_search_specs SET payload_json=?, search_spec_sha256=? '
                'WHERE search_spec_id=?',
                (spec.model_dump_json(), spec.search_spec_sha256, spec.search_spec_id),
            )
    assert search_repository.get(spec.search_spec_id) == spec


def test_search_repository_rejects_row_payload_disagreement(tmp_path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    _revision, spec, _estimate = _build(scene_repository)
    search_repository = CadSearchRepository(scene_repository)
    search_repository.save(spec)

    with sqlite3.connect(search_repository.path) as connection:
        connection.execute(
            'UPDATE cad_search_specs SET search_spec_sha256=? WHERE search_spec_id=?',
            ('0' * 64, spec.search_spec_id),
        )
    with pytest.raises(ValueError, match='disagrees with its payload'):
        search_repository.get(spec.search_spec_id)


def test_prior_schema_version_spec_fails_closed_on_read(tmp_path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    _revision, spec, _estimate = _build(scene_repository)
    search_repository = CadSearchRepository(scene_repository)
    search_repository.save(spec)

    # A historical v1 payload hashed under the old identity layout is a prior
    # schema version: it fails closed rather than being silently reinterpreted.
    v1_payload = spec.model_dump(mode='json')
    v1_payload['schema_version'] = 1
    v1_payload['search_spec_sha256'] = canonical_search_sha256({
        'schema_version': 1,
        'document_id': spec.document_id,
        'scene_revision_id': spec.scene_revision_id,
        'scene_content_hash': spec.scene_content_hash,
        'constraint_workspace_hash': spec.constraint_workspace_hash,
        'algorithm': 'deterministic_grid',
        'algorithm_version': 'search-space-grid-1',
        'axes': [item.model_dump(mode='json') for item in spec.axes],
        'candidate_limit': spec.candidate_limit,
    })
    with sqlite3.connect(search_repository.path) as connection:
        connection.execute(
            'UPDATE cad_search_specs SET payload_json=?, search_spec_sha256=? '
            'WHERE search_spec_id=?',
            (json.dumps(v1_payload), v1_payload['search_spec_sha256'], spec.search_spec_id),
        )
    with pytest.raises(ValueError, match='prior schema_version'):
        search_repository.get(spec.search_spec_id)
    with pytest.raises(ValueError, match='prior schema_version'):
        search_repository.list_specs(DOCUMENT_ID)


def test_candidate_generation_executes_only_canonical_executable_payload(tmp_path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    _revision, spec, _estimate = _build(scene_repository)
    canonical = generate_cad_candidates(scene_repository, spec)

    # A coherently rehashed o10 payload is rejected before generation.
    o10_spec = json.loads(spec.o10_spec_json)
    o10_spec['candidate_limit'] = 99
    tampered_o10 = _rebound(spec, o10_spec_json=canonical_search_json(o10_spec))
    with pytest.raises(ValueError, match='canonical compilation'):
        generate_cad_candidates(scene_repository, tampered_o10)

    # A coherently rehashed engine payload is rejected before generation.
    engine_spec = json.loads(spec.constraint_engine_spec_json)
    engine_spec['constraints'] = []
    tampered_engine = _rebound(
        spec,
        constraint_engine_spec_json=canonical_search_json(engine_spec),
        constraint_engine_spec_sha256=canonical_search_sha256(engine_spec),
    )
    with pytest.raises(ValueError, match='canonical compilation'):
        generate_cad_candidates(scene_repository, tampered_engine)

    # The canonical spec still generates deterministically.
    assert generate_cad_candidates(scene_repository, spec) == canonical
