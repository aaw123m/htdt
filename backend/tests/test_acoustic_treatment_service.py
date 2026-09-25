from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from htdt.acoustic_treatment_service import (
    TREATMENT_TYPES,
    AcousticTreatmentService,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


DOCUMENT_ID = 'o985-treatment-fixture'


def _fixture(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id=DOCUMENT_ID,
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=6.0, height_m=2.5),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='speaker-fl',
                speaker_role='FL',
                position=Position3(x_m=-1.0, y_m=0.0, z_m=1.0),
                size_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
            ),
        ),
    )
    scene_repository.save(document, parent_revision_id=None)
    return AcousticTreatmentService(scene_repository, DOCUMENT_ID)


def _result_ref(tag: str) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=f'prediction:{tag}',
        authority_version='1',
        semantic_hash_sha256=sha256(tag.encode()).hexdigest(),
    )


def test_author_place_compare_reopen(tmp_path):
    service = _fixture(tmp_path)
    definition = service.create_definition(
        name='60x120 absorber',
        treatment_type='porous_absorber',
        width_m=0.6,
        height_m=1.2,
        thickness_m=0.1,
    )
    assert definition in service.list_definitions()

    placement = service.place_treatment(
        definition=definition,
        position=Position3(x_m=2.0, y_m=0.0, z_m=1.5),
    )
    presentations = service.list_placements()
    assert [item.instance_id for item in presentations] == [
        placement.instance_id
    ]
    # Unsupported physics is shown as UNKNOWN, never fabricated (#985).
    assert presentations[0].wave_capability == 'UNKNOWN'

    comparison = service.create_comparison(
        name='baseline vs absorber A',
        candidate_designs=(('absorber A', (placement.instance_id,)),),
    )
    roles = {c.role for c in comparison.candidates}
    assert roles == {'no_treatment', 'treatment'}
    assert len(comparison.candidates) == 2

    # Reopen: definitions, placements and the named comparison replay.
    reopened = AcousticTreatmentService(
        service.scene_repository, DOCUMENT_ID
    )
    assert reopened.list_definitions()
    assert reopened.list_placements()
    listed = reopened.list_comparisons()
    assert [item.name for item in listed] == ['baseline vs absorber A']
    assert set(listed[0].candidates) == {'baseline', 'absorber A'}


def test_outcome_binds_evaluated_evidence(tmp_path):
    service = _fixture(tmp_path)
    definition = service.create_definition(
        name='panel', treatment_type='bass_trap',
        width_m=0.6, height_m=1.2, thickness_m=0.15,
    )
    a = service.place_treatment(
        definition=definition,
        position=Position3(x_m=1.0, y_m=0.0, z_m=1.0),
    )
    b = service.place_treatment(
        definition=definition,
        position=Position3(x_m=-1.0, y_m=0.0, z_m=1.0),
    )
    comparison = service.create_comparison(
        name='A vs B',
        candidate_designs=(
            ('A', (a.instance_id,)),
            ('B', (b.instance_id,)),
        ),
    )

    # All candidates evaluated → compatible.
    outcome = service.bind_comparison_outcome(
        comparison.comparison_id,
        candidate_results={
            'baseline': (_result_ref('baseline'),),
            'A': (_result_ref('a'),),
            'B': (_result_ref('b'),),
        },
    )
    assert outcome.compatibility == 'compatible'

    # Partial: one design could not be evaluated → explicit unavailable,
    # never a fabricated benefit.
    second = service.bind_comparison_outcome(
        comparison.comparison_id,
        candidate_results={
            'A': (_result_ref('a2'),),
            'B': ('unavailable', 'provider unsupported'),
        },
    )
    assert second.compatibility == 'partial'
    unavailable = next(
        item for item in second.outcomes if item.availability == 'unavailable'
    )
    assert unavailable.unavailable_reason == 'provider unsupported'

    latest = service.latest_outcome(comparison.comparison_id)
    assert latest is not None
    assert latest.outcome_id == second.outcome_id


def test_outcome_rejects_foreign_labels_and_empty_refs(tmp_path):
    service = _fixture(tmp_path)
    definition = service.create_definition(
        name='panel', treatment_type='porous_absorber',
        width_m=0.6, height_m=1.2, thickness_m=0.1,
    )
    placement = service.place_treatment(
        definition=definition,
        position=Position3(x_m=0.0, y_m=0.0, z_m=1.0),
    )
    comparison = service.create_comparison(
        name='compare',
        candidate_designs=(('A', (placement.instance_id,)),),
    )
    with pytest.raises(ValueError, match='candidate label'):
        service.bind_comparison_outcome(
            comparison.comparison_id,
            candidate_results={'ghost': (_result_ref('x'),)},
        )


def test_placement_requires_current_revision(tmp_path):
    service = _fixture(tmp_path)
    definition = service.create_definition(
        name='panel', treatment_type='porous_absorber',
        width_m=0.6, height_m=1.2, thickness_m=0.1,
    )
    placement = service.place_treatment(
        definition=definition,
        position=Position3(x_m=0.0, y_m=0.0, z_m=1.0),
    )
    service.remove_placement(placement.instance_id)
    assert service.list_placements() == ()
