from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
import sqlite3
from math import cos, radians, sin
from types import SimpleNamespace

import pytest

from htdt.cad_constraint_models import CadConstraintSet, CadWallClearanceConstraint
from htdt.cad_document import WorkingDocument
from htdt.cad_extended_search import (
    CadExtendedSearchAxis,
    aim_horizontal_yaw_deg,
    aim_pitch_deg,
    body_horizontal_yaw_deg,
    direction_with_aim_pitch,
    direction_with_horizontal_yaw,
    apply_extended_candidate,
    build_extended_model_capability,
    build_extended_parameter_evidence,
    build_extended_search_spec,
    extended_candidate_preview_document,
    generate_extended_candidates,
)
from htdt.cad_extended_search_repository import CadExtendedSearchRepository
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
from htdt.cad_search import build_cad_search_spec, generate_cad_candidates
from htdt.cad_search_models import CadSearchAxis
from htdt.cad_search_repository import CadSearchRepository
from htdt.cad_walls import make_wall_topology
from htdt.canonical_json import canonical_sha256


DOCUMENT_ID = 'o80-extended-fixture'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scene() -> SceneDocument:
    return SceneDocument(
        document_id=DOCUMENT_ID,
        schema_version=2,
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.2, y_m=0.2, z_m=0.4),
                acoustic_reference_offset_m=Offset3(),
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
            ),
            SceneEntity(
                entity_id='mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=2.5, y_m=3.0, z_m=1.1),
            ),
        ),
    )


def _synthetic_evidence(
    parameter,
    *,
    model_id='synthetic-directional-fixture',
    model_version='1',
    tested_min_deg=-180.0,
    tested_max_deg=180.0,
):
    return build_extended_parameter_evidence(
        parameter=parameter,
        model_id=model_id,
        model_version=model_version,
        evidence_scope='synthetic_fixture',
        tested_min_deg=tested_min_deg,
        tested_max_deg=tested_max_deg,
        source_kind='synthetic_fixture',
        source_id=f'synthetic-evidence:{model_id}:{parameter}',
        source_sha256=sha256(
            f'synthetic-evidence:{model_id}:{parameter}'.encode('utf-8')
        ).hexdigest(),
        detail='declared synthetic fixture evidence',
        created_at_utc=_now(),
    )


def _synthetic_capability(**overrides):
    parameters = overrides.pop(
        'supported_parameters', ('aim_yaw_deg', 'body_yaw_deg')
    )
    model_id = overrides.pop('model_id', 'synthetic-directional-fixture')
    model_version = overrides.pop('model_version', '1')
    evidence = overrides.pop('parameter_evidence', None) or tuple(
        _synthetic_evidence(
            parameter,
            model_id=model_id,
            model_version=model_version,
        )
        for parameter in parameters
    )
    fields = dict(
        model_id=model_id,
        model_version=model_version,
        evidence_scope='synthetic_fixture',
        supported_parameters=parameters,
        parameter_evidence=evidence,
        detail='synthetic directional model for software acceptance only',
        created_at_utc=_now(),
    )
    fields.update(overrides)
    return build_extended_model_capability(**fields)


def _fixture(tmp_path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(_scene(), parent_revision_id=None).revision
    constraints = CadConstraintSet(document_id=DOCUMENT_ID, constraints=())
    base_spec, _estimate = build_cad_search_spec(
        revision,
        constraints,
        (
            CadSearchAxis(
                entity_id='fl',
                axis='x',
                min_m=1.0,
                max_m=1.2,
                step_m=0.2,
            ),
        ),
        candidate_limit=20,
        name='base position sweep',
    )
    search_repository = CadSearchRepository(scene_repository)
    search_repository.save(base_spec)
    base_page = generate_cad_candidates(scene_repository, base_spec, limit=20)
    capability = _synthetic_capability()
    extended_repository = CadExtendedSearchRepository(search_repository)
    for item in (
        _synthetic_evidence('aim_yaw_deg'),
        _synthetic_evidence('body_yaw_deg'),
    ):
        extended_repository.save_parameter_evidence(item)
    extended_repository.save_capability(capability)
    spec = build_extended_search_spec(
        source_revision=revision,
        base_spec=base_spec,
        base_candidate_set_sha256=base_page.candidate_set_sha256,
        base_candidate_count=base_page.feasible_candidate_count,
        capability=capability,
        axes=(
            CadExtendedSearchAxis(
                entity_id='fl',
                min_value=-15.0,
                max_value=15.0,
                step=15.0,
            ),
        ),
        candidate_limit=20,
        created_at_utc=_now(),
    )
    extended_repository.save_spec(spec)
    return (
        scene_repository,
        revision,
        constraints,
        base_spec,
        base_page,
        capability,
        extended_repository,
        spec,
    )


def test_extended_search_is_deterministic_and_layers_on_base_candidate_set(tmp_path):
    (
        scene_repository,
        _revision,
        _constraints,
        base_spec,
        base_page,
        _capability,
        repository,
        spec,
    ) = _fixture(tmp_path)

    first = generate_extended_candidates(
        scene_repository,
        base_spec,
        spec,
        limit=20,
    )
    second = generate_extended_candidates(
        scene_repository,
        base_spec,
        spec,
        limit=20,
    )

    assert base_page.feasible_candidate_count == 2
    assert first.feasible_candidate_count == 6
    assert first.raw_candidate_count == 6
    assert first.candidate_set_sha256 == second.candidate_set_sha256
    assert [item.candidate_id for item in first.candidates] == [
        item.candidate_id for item in second.candidates
    ]
    assert [item.aim_yaw_deg['fl'] for item in first.candidates] == [
        -15.0, 0.0, 15.0, -15.0, 0.0, 15.0
    ]
    assert repository.get_spec(spec.extended_search_id) == spec


def test_extended_preview_and_apply_change_position_and_aim_in_one_undo(tmp_path):
    (
        scene_repository,
        revision,
        constraints,
        base_spec,
        _base_page,
        _capability,
        _repository,
        spec,
    ) = _fixture(tmp_path)
    page = generate_extended_candidates(scene_repository, base_spec, spec, limit=20)
    candidate = page.candidates[-1]
    working = WorkingDocument(
        revision.document,
        source_revision_id=revision.revision_id,
        saved_content_hash=revision.content_hash,
    )

    preview = extended_candidate_preview_document(
        working.committed_document,
        candidate,
    )
    assert preview.entity('fl').position.x_m == pytest.approx(1.2)
    assert aim_horizontal_yaw_deg(preview.entity('fl').aim_xyz) == pytest.approx(15.0)
    assert working.committed_document.entity('fl').position.x_m == pytest.approx(1.0)
    assert aim_horizontal_yaw_deg(
        working.committed_document.entity('fl').aim_xyz
    ) == pytest.approx(0.0)

    assert apply_extended_candidate(
        working,
        candidate,
        extended_spec=spec,
        base_spec=base_spec,
        current_constraint_set=constraints,
        current_document_id=DOCUMENT_ID,
    )
    assert working.history_length == 1
    assert working.committed_document.entity('fl').position.x_m == pytest.approx(1.2)
    assert aim_horizontal_yaw_deg(
        working.committed_document.entity('fl').aim_xyz
    ) == pytest.approx(15.0)

    assert working.undo()
    assert not working.is_dirty
    assert working.committed_document.entity('fl').position.x_m == pytest.approx(1.0)
    assert aim_horizontal_yaw_deg(
        working.committed_document.entity('fl').aim_xyz
    ) == pytest.approx(0.0)


def test_extended_apply_rejects_position_tampering_even_with_base_candidate_id(tmp_path):
    (
        scene_repository,
        revision,
        constraints,
        base_spec,
        _base_page,
        _capability,
        _repository,
        spec,
    ) = _fixture(tmp_path)
    page = generate_extended_candidates(scene_repository, base_spec, spec, limit=20)
    candidate = page.candidates[-1]
    tampered_positions = {
        entity_id: dict(position)
        for entity_id, position in candidate.positions.items()
    }
    tampered_positions['fl']['x_m'] = 1.1
    tampered = candidate.model_copy(update={'positions': tampered_positions})
    working = WorkingDocument(
        revision.document,
        source_revision_id=revision.revision_id,
        saved_content_hash=revision.content_hash,
    )

    with pytest.raises(ValueError, match='identity mismatch'):
        apply_extended_candidate(
            working,
            tampered,
            extended_spec=spec,
            base_spec=base_spec,
            current_constraint_set=constraints,
            current_document_id=DOCUMENT_ID,
        )


def test_extended_apply_rejects_positions_violating_placement_constraints(tmp_path):
    (
        scene_repository,
        revision,
        constraints,
        base_spec,
        _base_page,
        _capability,
        _repository,
        spec,
    ) = _fixture(tmp_path)
    page = generate_extended_candidates(scene_repository, base_spec, spec, limit=20)
    candidate = page.candidates[-1]

    # x=6.0 puts the speaker envelope outside the 5 m room boundary; a
    # fabricated ec- id that stays self-consistent must still fail apply.
    tampered_positions = {
        entity_id: dict(position)
        for entity_id, position in candidate.positions.items()
    }
    tampered_positions['fl']['x_m'] = 6.0
    payload = {
        'extended_search_sha256': spec.extended_search_sha256,
        'base_candidate_id': candidate.base_candidate_id,
        'positions': tampered_positions,
        'aim_yaw_deg': dict(candidate.aim_yaw_deg),
    }
    if candidate.body_yaw_deg:
        payload['body_yaw_deg'] = dict(candidate.body_yaw_deg)
    if candidate.aim_pitch_deg:
        payload['aim_pitch_deg'] = dict(candidate.aim_pitch_deg)
    forged = candidate.model_copy(update={
        'positions': tampered_positions,
        'candidate_id': 'ec-' + canonical_sha256(payload)[:20],
    })
    working = WorkingDocument(
        revision.document,
        source_revision_id=revision.revision_id,
        saved_content_hash=revision.content_hash,
    )
    with pytest.raises(ValueError, match='placement constraints'):
        apply_extended_candidate(
            working,
            forged,
            extended_spec=spec,
            base_spec=base_spec,
            current_constraint_set=constraints,
            current_document_id=DOCUMENT_ID,
        )
    assert working.history_length == 0
    assert working.committed_document.entity('fl').position.x_m == pytest.approx(1.0)


def test_rew_roomsim_cannot_claim_toe_in_capability():
    with pytest.raises(ValueError, match='does not model speaker acoustic direction'):
        _synthetic_capability(
            model_id='rew-room-simulator',
            model_version='5.40',
            detail='invalid',
        )


def test_extended_search_requires_explicit_source_aim(tmp_path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = _scene()
    speaker = document.entity('fl').model_copy(update={'aim_xyz': None})
    no_aim = document.model_copy(update={
        'entities': tuple(
            speaker if entity.entity_id == 'fl' else entity
            for entity in document.entities
        )
    })
    revision = scene_repository.save(no_aim, parent_revision_id=None).revision
    constraints = CadConstraintSet(document_id=DOCUMENT_ID, constraints=())
    base_spec, _ = build_cad_search_spec(
        revision,
        constraints,
        (
            CadSearchAxis(
                entity_id='fl',
                axis='x',
                min_m=1.0,
                max_m=1.0,
                step_m=0.1,
            ),
        ),
        candidate_limit=10,
    )
    base_page = generate_cad_candidates(scene_repository, base_spec)
    capability = _synthetic_capability(detail='fixture')

    with pytest.raises(ValueError, match='requires explicit speaker aim'):
        build_extended_search_spec(
            source_revision=revision,
            base_spec=base_spec,
            base_candidate_set_sha256=base_page.candidate_set_sha256,
            base_candidate_count=base_page.feasible_candidate_count,
            capability=capability,
            axes=(CadExtendedSearchAxis(
                entity_id='fl',
                min_value=-10.0,
                max_value=10.0,
                step=10.0,
            ),),
            candidate_limit=10,
            created_at_utc=_now(),
        )

class _ValidationRepository:
    def __init__(self, path, record):
        self.path = path
        self.record = record

    def get(self, validation_id):
        return (
            self.record
            if validation_id == self.record.validation_id
            else None
        )


def test_owned_room_extended_spec_requires_exact_o60_search_authority(tmp_path):
    (
        scene_repository,
        revision,
        _constraints,
        base_spec,
        base_page,
        _capability,
        _repository,
        _spec,
    ) = _fixture(tmp_path)
    validation = SimpleNamespace(
        validation_id='owned-directional-validation',
        evidence_scope='owned_room',
        recommendation_gate='eligible',
        gate_reasons=(),
        campaign_id='owned-directional-campaign',
        campaign_sha256='a' * 64,
        model_id='owned-directional-model',
        model_version='1',
        document_id=DOCUMENT_ID,
        search_spec_id='different-search-spec',
        search_spec_sha256='b' * 64,
        candidate_set_sha256='c' * 64,
    )
    validation_repository = _ValidationRepository(
        scene_repository.path,
        validation,
    )
    repository = CadExtendedSearchRepository(
        CadSearchRepository(scene_repository),
        validation_repository,
    )
    capability = build_extended_model_capability(
        model_id=validation.model_id,
        model_version=validation.model_version,
        evidence_scope='owned_room',
        supported_parameters=('aim_yaw_deg', 'body_yaw_deg'),
        parameter_evidence=(
            build_extended_parameter_evidence(
                parameter='aim_yaw_deg',
                model_id=validation.model_id,
                model_version=validation.model_version,
                evidence_scope='owned_room',
                tested_min_deg=-45.0,
                tested_max_deg=45.0,
                source_kind='o90e_decision',
                source_id='o90e-decision:' + 'a' * 64,
                source_sha256='a' * 64,
                detail='forged owned-room directional evidence',
                created_at_utc=_now(),
            ),
            build_extended_parameter_evidence(
                parameter='body_yaw_deg',
                model_id=validation.model_id,
                model_version=validation.model_version,
                evidence_scope='owned_room',
                tested_min_deg=-45.0,
                tested_max_deg=45.0,
                source_kind='o90e_decision',
                source_id='o90e-decision:' + 'b' * 64,
                source_sha256='b' * 64,
                detail='forged owned-room directional evidence',
                created_at_utc=_now(),
            ),
        ),
        detail='owned-room directional fixture',
        validation=validation,
        created_at_utc=_now(),
    )
    # #384: generic O60 eligibility cannot authorize a directional
    # parameter — the forged o90e evidence does not resolve, so the
    # capability itself is rejected before any spec-level check.
    with pytest.raises(
        ValueError,
        match='parameter evidence does not resolve',
    ):
        repository.save_capability(capability)



def test_physical_toe_in_rotates_body_and_coupled_aim_in_one_undo(tmp_path):
    (
        scene_repository,
        revision,
        constraints,
        base_spec,
        base_page,
        capability,
        _repository,
        _aim_spec,
    ) = _fixture(tmp_path)
    body_spec = build_extended_search_spec(
        source_revision=revision,
        base_spec=base_spec,
        base_candidate_set_sha256=base_page.candidate_set_sha256,
        base_candidate_count=base_page.feasible_candidate_count,
        capability=capability,
        axes=(
            CadExtendedSearchAxis(
                entity_id='fl',
                parameter='body_yaw_deg',
                min_value=-15.0,
                max_value=15.0,
                step=15.0,
            ),
        ),
        candidate_limit=20,
        created_at_utc=_now(),
    )
    page = generate_extended_candidates(
        scene_repository,
        base_spec,
        body_spec,
        limit=20,
    )
    candidate = page.candidates[-1]
    assert candidate.body_yaw_deg == {'fl': 15.0}
    assert candidate.aim_yaw_deg == {}

    preview = extended_candidate_preview_document(revision.document, candidate)
    preview_speaker = preview.entity('fl')
    assert body_horizontal_yaw_deg(preview_speaker) == pytest.approx(15.0)
    assert aim_horizontal_yaw_deg(preview_speaker.aim_xyz) == pytest.approx(15.0)

    working = WorkingDocument(
        revision.document,
        source_revision_id=revision.revision_id,
        saved_content_hash=revision.content_hash,
    )
    assert apply_extended_candidate(
        working,
        candidate,
        extended_spec=body_spec,
        base_spec=base_spec,
        current_constraint_set=constraints,
        current_document_id=DOCUMENT_ID,
    )
    assert working.history_length == 1
    applied = working.committed_document.entity('fl')
    assert body_horizontal_yaw_deg(applied) == pytest.approx(15.0)
    assert aim_horizontal_yaw_deg(applied.aim_xyz) == pytest.approx(15.0)

    assert working.undo()
    restored = working.committed_document.entity('fl')
    assert body_horizontal_yaw_deg(restored) == pytest.approx(0.0)
    assert aim_horizontal_yaw_deg(restored.aim_xyz) == pytest.approx(0.0)


def test_physical_toe_in_rechecks_exact_oriented_wall_clearance(tmp_path):
    room = RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4)
    topology = make_wall_topology(room)
    document = SceneDocument(
        document_id='o80p-wall-fixture',
        schema_version=3,
        room=room,
        wall_topology=topology,
        entities=(
            SceneEntity(
                entity_id='fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=0.6, y_m=1.5, z_m=1.0),
                size_m=Size3(x_m=0.2, y_m=1.0, z_m=0.4),
                acoustic_reference_offset_m=Offset3(),
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
            ),
            SceneEntity(
                entity_id='mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=2.5, y_m=3.0, z_m=1.1),
            ),
        ),
    )
    scene_repository = SceneRepository(tmp_path / 'physical.sqlite3')
    revision = scene_repository.save(document, parent_revision_id=None).revision
    left_wall_id = 'wall:rear-left->front-left'
    constraints = CadConstraintSet(
        document_id=document.document_id,
        constraints=(
            CadWallClearanceConstraint(
                constraint_id='left-clearance',
                name='left clearance',
                entity_ids=('fl',),
                wall_id=left_wall_id,
                min_m=0.3,
            ),
        ),
    )
    base_spec, _ = build_cad_search_spec(
        revision,
        constraints,
        (
            CadSearchAxis(
                entity_id='fl',
                axis='x',
                min_m=0.6,
                max_m=0.6,
                step_m=0.1,
            ),
        ),
        candidate_limit=10,
        name='source orientation position',
    )
    base_page = generate_cad_candidates(scene_repository, base_spec, limit=10)

    # Exact source body yaw=0 footprint has x half-extent 0.1 m, so the
    # cabinet clearance is 0.5 m and this XYZ is feasible.
    assert base_page.feasible_candidate_count == 1

    capability = _synthetic_capability(
        supported_parameters=('body_yaw_deg',),
        detail='physical toe-in geometry acceptance',
    )
    spec = build_extended_search_spec(
        source_revision=revision,
        base_spec=base_spec,
        base_candidate_set_sha256=base_page.candidate_set_sha256,
        base_candidate_count=base_page.feasible_candidate_count,
        capability=capability,
        axes=(
            CadExtendedSearchAxis(
                entity_id='fl',
                parameter='body_yaw_deg',
                min_value=0.0,
                max_value=90.0,
                step=90.0,
            ),
        ),
        candidate_limit=10,
        created_at_utc=_now(),
    )
    page = generate_extended_candidates(
        scene_repository,
        base_spec,
        spec,
        limit=10,
    )

    # At 90 degrees the 1.0 m cabinet depth becomes the x extent, reducing
    # exact wall clearance to 0.1 m. The rotated body must be rejected.
    assert page.raw_candidate_count == 2
    assert page.feasible_candidate_count == 1
    assert page.rejected_candidate_count == 1
    assert page.rejection_counts == {'left-clearance': 1}
    assert [item.body_yaw_deg['fl'] for item in page.candidates] == [0.0]


def test_capability_requires_evidence_for_every_parameter(tmp_path):
    """#384: a supported parameter without exact evidence cannot persist."""
    (
        _scene_repository,
        _revision,
        _constraints,
        _base_spec,
        _base_page,
        _capability,
        extended_repository,
        _spec,
    ) = _fixture(tmp_path)

    # Builder-level: missing evidence for a declared parameter.
    with pytest.raises(
        ValueError, match='requires exact evidence for every supported'
    ):
        _synthetic_capability(
            parameter_evidence=(_synthetic_evidence('aim_yaw_deg'),)
        )

    # Persisted-evidence-level: a capability referencing an evidence id
    # that was never saved fails closed.
    ghost = _synthetic_capability(
        supported_parameters=('aim_yaw_deg',),
        parameter_evidence=(
            _synthetic_evidence(
                'aim_yaw_deg',
                model_id='ghost-model',
            ),
        ),
        model_id='ghost-model',
    )
    with pytest.raises(ValueError, match='parameter evidence does not resolve'):
        extended_repository.save_capability(ghost)


def test_capability_rejects_foreign_or_tampered_evidence(tmp_path):
    """#384: evidence for another model/scope cannot back a capability."""
    (
        _scene_repository,
        _revision,
        _constraints,
        _base_spec,
        _base_page,
        _capability,
        extended_repository,
        _spec,
    ) = _fixture(tmp_path)

    # Scope mismatch is structural and rejected by the builder.
    foreign_scope = build_extended_parameter_evidence(
        parameter='aim_yaw_deg',
        model_id='synthetic-directional-fixture',
        model_version='1',
        evidence_scope='owned_room',
        tested_min_deg=-45.0,
        tested_max_deg=45.0,
        source_kind='o90e_decision',
        source_id='o90e-decision:' + 'a' * 64,
        source_sha256='a' * 64,
        detail='owned-room evidence attached to a synthetic capability',
        created_at_utc=_now(),
    )
    with pytest.raises(ValueError, match='scope does not match capability'):
        _synthetic_capability(
            supported_parameters=('aim_yaw_deg',),
            parameter_evidence=(foreign_scope,),
        )

    # Model mismatch is likewise structural.
    other_model = _synthetic_evidence(
        'aim_yaw_deg',
        model_id='other-model',
    )
    with pytest.raises(ValueError, match='model does not match capability'):
        _synthetic_capability(
            supported_parameters=('aim_yaw_deg',),
            parameter_evidence=(other_model,),
        )


def test_owned_room_evidence_requires_o90e_decision_authority(tmp_path):
    """#384: owned-room evidence cannot claim an unresolvable source."""
    (
        _scene_repository,
        _revision,
        _constraints,
        _base_spec,
        _base_page,
        _capability,
        extended_repository,
        _spec,
    ) = _fixture(tmp_path)

    forged = build_extended_parameter_evidence(
        parameter='aim_yaw_deg',
        model_id='any-model',
        model_version='1',
        evidence_scope='owned_room',
        tested_min_deg=-45.0,
        tested_max_deg=45.0,
        source_kind='o90e_decision',
        source_id='o90e-decision:' + 'f' * 64,
        source_sha256='f' * 64,
        detail='forged owned-room evidence',
        created_at_utc=_now(),
    )
    with pytest.raises(
        ValueError, match='requires a robustness validation repository'
    ):
        extended_repository.save_parameter_evidence(forged)

    # A synthetic source can never claim owned-room scope.
    with pytest.raises(
        ValueError, match='cannot use a synthetic source'
    ):
        build_extended_parameter_evidence(
            parameter='aim_yaw_deg',
            model_id='any-model',
            model_version='1',
            evidence_scope='owned_room',
            tested_min_deg=-45.0,
            tested_max_deg=45.0,
            source_kind='synthetic_fixture',
            source_id='declared',
            source_sha256='0' * 64,
            detail='invalid',
            created_at_utc=_now(),
        )


def test_axis_range_must_stay_within_tested_evidence(tmp_path):
    """#384: a capability cannot extend a spec beyond validated range."""
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(_scene(), parent_revision_id=None).revision
    constraints = CadConstraintSet(document_id=DOCUMENT_ID, constraints=())
    base_spec, _ = build_cad_search_spec(
        revision,
        constraints,
        (
            CadSearchAxis(
                entity_id='fl',
                axis='x',
                min_m=1.0,
                max_m=1.0,
                step_m=0.1,
            ),
        ),
        candidate_limit=10,
    )
    search_repository = CadSearchRepository(scene_repository)
    search_repository.save(base_spec)
    base_page = generate_cad_candidates(scene_repository, base_spec)
    extended_repository = CadExtendedSearchRepository(search_repository)

    evidence = _synthetic_evidence(
        'aim_yaw_deg',
        tested_min_deg=-15.0,
        tested_max_deg=15.0,
    )
    extended_repository.save_parameter_evidence(evidence)
    capability = _synthetic_capability(
        supported_parameters=('aim_yaw_deg',),
        parameter_evidence=(evidence,),
    )
    extended_repository.save_capability(capability)

    spec = build_extended_search_spec(
        source_revision=revision,
        base_spec=base_spec,
        base_candidate_set_sha256=base_page.candidate_set_sha256,
        base_candidate_count=base_page.feasible_candidate_count,
        capability=capability,
        axes=(
            CadExtendedSearchAxis(
                entity_id='fl',
                min_value=-30.0,
                max_value=30.0,
                step=15.0,
            ),
        ),
        candidate_limit=20,
        created_at_utc=_now(),
    )
    with pytest.raises(ValueError, match='exceeds the tested applicability'):
        extended_repository.save_spec(spec)


def test_capability_and_spec_reads_replay_authority(tmp_path):
    """#384: read paths fail closed when persisted authority is tampered."""
    (
        scene_repository,
        _revision,
        _constraints,
        _base_spec,
        _base_page,
        capability,
        extended_repository,
        spec,
    ) = _fixture(tmp_path)

    # A forged capability row fails closed on read.
    tampered = capability.model_dump(mode='json')
    tampered['detail'] = 'forged detail'
    tampered['capability_id'] = 'forged-capability'
    tampered['capability_sha256'] = '0' * 64
    with closing(
        sqlite3.connect(scene_repository.path)
    ) as connection, connection:
        connection.execute(
            'INSERT INTO cad_extended_model_capabilities('
            'capability_id, model_id, model_version, evidence_scope, '
            'capability_sha256, payload_json, created_at_utc'
            ') VALUES (?, ?, ?, ?, ?, ?, ?)',
            (
                tampered['capability_id'],
                tampered['model_id'],
                tampered['model_version'],
                tampered['evidence_scope'],
                tampered['capability_sha256'],
                json.dumps(tampered),
                tampered['created_at_utc'],
            ),
        )
    with pytest.raises(ValueError):
        extended_repository.get_capability('forged-capability')

    # Deleting the evidence behind the real capability invalidates reads.
    with closing(
        sqlite3.connect(scene_repository.path)
    ) as connection, connection:
        connection.execute(
            'DELETE FROM cad_extended_parameter_evidence'
        )
    with pytest.raises(ValueError, match='does not resolve'):
        extended_repository.get_capability(capability.capability_id)
    with pytest.raises(ValueError):
        extended_repository.get_spec(spec.extended_search_id)


def test_aim_pitch_is_a_first_class_extended_parameter(tmp_path):
    (
        scene_repository,
        revision,
        _constraints,
        base_spec,
        base_page,
        _capability,
        extended_repository,
        _spec,
    ) = _fixture(tmp_path)

    evidence = _synthetic_evidence(
        'aim_pitch_deg',
        tested_min_deg=-89.0,
        tested_max_deg=89.0,
    )
    extended_repository.save_parameter_evidence(evidence)
    capability = _synthetic_capability(
        supported_parameters=('aim_pitch_deg',),
        parameter_evidence=(evidence,),
    )
    extended_repository.save_capability(capability)
    spec = build_extended_search_spec(
        source_revision=revision,
        base_spec=base_spec,
        base_candidate_set_sha256=base_page.candidate_set_sha256,
        base_candidate_count=base_page.feasible_candidate_count,
        capability=capability,
        axes=(
            CadExtendedSearchAxis(
                entity_id='fl',
                parameter='aim_pitch_deg',
                min_value=-10.0,
                max_value=10.0,
                step=10.0,
            ),
        ),
        candidate_limit=50,
        created_at_utc=_now(),
    )
    page = generate_extended_candidates(
        scene_repository, base_spec, spec, limit=50
    )
    # 2 base positions x 3 pitch values
    assert page.feasible_candidate_count == 6
    ids = {candidate.candidate_id for candidate in page.candidates}
    assert len(ids) == 6
    pitches = sorted(
        candidate.aim_pitch_deg['fl'] for candidate in page.candidates
    )
    assert pitches == [-10.0, -10.0, 0.0, 0.0, 10.0, 10.0]

    down = next(
        c for c in page.candidates if c.aim_pitch_deg.get('fl') == -10.0
    )
    up = next(
        c for c in page.candidates if c.aim_pitch_deg.get('fl') == 10.0
    )
    assert down.candidate_id != up.candidate_id
    assert down.aim_yaw_deg == up.aim_yaw_deg == {}

    preview = extended_candidate_preview_document(revision.document, up)
    aim = preview.entity('fl').aim_xyz
    assert aim.z == pytest.approx(sin(radians(10.0)))
    assert aim.y == pytest.approx(cos(radians(10.0)))
    assert aim_pitch_deg(aim) == pytest.approx(10.0)
    assert aim_horizontal_yaw_deg(aim) == pytest.approx(0.0)


def test_aim_pitch_repitches_a_vertical_only_source_with_canonical_yaw():
    vertical = Direction3(x=0.0, y=0.0, z=-1.0)
    repitched = direction_with_aim_pitch(vertical, -30.0)
    assert repitched.x == pytest.approx(0.0)
    assert repitched.y == pytest.approx(cos(radians(-30.0)))
    assert repitched.z == pytest.approx(sin(radians(-30.0)))
    assert aim_pitch_deg(repitched) == pytest.approx(-30.0)
    # once repitched off the pole, a searched horizontal yaw applies normally
    yawed = direction_with_horizontal_yaw(repitched, 90.0)
    assert yawed.x == pytest.approx(cos(radians(-30.0)))
    assert yawed.y == pytest.approx(0.0, abs=1e-12)
    assert aim_pitch_deg(vertical) == pytest.approx(-90.0)


def test_aim_pitch_axis_and_candidate_bounds():
    with pytest.raises(ValueError, match='aim pitch'):
        CadExtendedSearchAxis(
            entity_id='fl',
            parameter='aim_pitch_deg',
            min_value=-90.0,
            max_value=0.0,
            step=1.0,
        )
    with pytest.raises(ValueError, match='aim pitch'):
        CadExtendedSearchAxis(
            entity_id='fl',
            parameter='aim_pitch_deg',
            min_value=0.0,
            max_value=90.0,
            step=1.0,
        )
