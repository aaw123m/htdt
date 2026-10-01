"""Issue #983: proposed SystemVariant as an exact prediction target.

The Room prediction workflow can select a persisted proposed SystemVariant
without applying it: the materialized proposal supplies the source/receiver
set, and the exact variant id/hash joins the request identity so current
and proposed predictions never share a cache entry merely because room
geometry is identical.
"""

from __future__ import annotations

from pathlib import Path
from threading import Event

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    F1_DOCUMENT_ID,
    Direction3,
    Offset3,
    Position3,
    SceneEntity,
    Size3,
    acoustic_reference_position,
    make_f1_scene,
)
from htdt.cad_prediction_request import rectangular_geometry_request_identity
from htdt.cad_predictions import analyze_native_rectangular_geometry
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    ProposedEntitySpec,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.room_prediction import RoomPredictionController
from htdt.room_prediction_target import resolve_room_prediction_target
from htdt.room_workspace import RoomWorkspaceController


NOW = '2026-09-19T00:00:00+00:00'
RECEIVER = 'point-mlp'


def _speaker(entity_id: str, role: str, x_m: float) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name=role,
        speaker_role=role,
        position=Position3(x_m=x_m, y_m=3.0, z_m=1.3),
        size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
        aim_xyz=Direction3(x=0.0, y=-1.0, z=0.0),
        acoustic_reference_offset_m=Offset3(),
    )


def _roles(*role_ids: str) -> tuple[ChannelRoleBinding, ...]:
    return tuple(
        ChannelRoleBinding(role_id=role, display_name=role)
        for role in role_ids
    )


def _fixture(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    baseline = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    variant_repository = CadSystemVariantRepository(scene_repository)
    variant = build_system_variant(
        baseline=baseline,
        name='Proposed 5.0.2',
        role_bindings=_roles('FL', 'C', 'FR', 'SL', 'SR'),
        proposed_entities=(
            ProposedEntitySpec(
                spec_id='proposal-sl',
                entity=_speaker('speaker-sl', 'SL', 0.6),
                role_binding_id='SL',
            ),
            ProposedEntitySpec(
                spec_id='proposal-sr',
                entity=_speaker('speaker-sr', 'SR', 5.4),
                role_binding_id='SR',
            ),
        ),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant)
    return scene_repository, baseline, variant_repository, variant


def _controller(scene_repository: SceneRepository):
    room = RoomWorkspaceController(scene_repository, F1_DOCUMENT_ID)
    return RoomPredictionController(scene_repository, room)


def test_persisted_variant_resolves_without_applying(tmp_path: Path) -> None:
    scene_repository, baseline, variant_repository, variant = _fixture(
        tmp_path
    )

    target = resolve_room_prediction_target(
        scene_repository=scene_repository,
        revision_id=baseline.revision_id,
        system_variant_id=variant.variant_id,
        variant_repository=variant_repository,
    )

    assert target.variant_bound
    assert target.system_variant_id == variant.variant_id
    assert target.system_variant_sha256 == variant.variant_sha256
    materialized_speakers = {
        entity.entity_id
        for entity in target.document.entities
        if entity.kind == 'speaker'
    }
    assert materialized_speakers == {
        'speaker-fl',
        'speaker-c',
        'speaker-fr',
        'speaker-sl',
        'speaker-sr',
    }
    # The Scene head is untouched: the proposal was only resolved.
    assert scene_repository.latest(F1_DOCUMENT_ID) == baseline
    # The effective input view is keyed by the exact variant hash so
    # provider staleness checks stay explicit.
    assert target.input_revision.revision_id == (
        f'proposal:{variant.variant_sha256[:24]}'
    )
    assert target.input_revision.content_hash != baseline.content_hash


def test_current_and_proposed_requests_have_distinct_exact_identity(
    tmp_path: Path,
) -> None:
    scene_repository, baseline, variant_repository, variant = _fixture(
        tmp_path
    )
    prediction = _controller(scene_repository)

    current_spec = prediction.prepare_run(RECEIVER)
    proposed_spec = prediction.prepare_run(
        RECEIVER, system_variant_id=variant.variant_id
    )

    assert proposed_spec.system_variant == variant
    assert proposed_spec.identity.input_hash != current_spec.identity.input_hash
    assert proposed_spec.identity.input_snapshot_json != (
        current_spec.identity.input_snapshot_json
    )
    import json

    snapshot = json.loads(proposed_spec.identity.input_snapshot_json)
    assert snapshot['system_variant'] == {
        'variant_id': variant.variant_id,
        'variant_sha256': variant.variant_sha256,
        'baseline_revision_id': baseline.revision_id,
        'baseline_content_hash': baseline.content_hash,
    }
    assert [item['entity_id'] for item in snapshot['speakers']][-2:] == [
        'speaker-sl',
        'speaker-sr',
    ]
    # Same baseline anchor, different exact identity: the two requests
    # coexist and stale independently.
    assert proposed_spec.revision == current_spec.revision == baseline
    assert scene_repository.latest(F1_DOCUMENT_ID) == baseline

    prediction.dispose()


def test_variant_prediction_uses_materialized_source_set(
    tmp_path: Path,
) -> None:
    scene_repository, baseline, _variant_repository, variant = _fixture(
        tmp_path
    )

    current = analyze_native_rectangular_geometry(baseline, RECEIVER)
    proposed = analyze_native_rectangular_geometry(
        baseline, RECEIVER, system_variant=variant
    )

    proposed_speakers = {
        item.speaker_entity_id
        for item in proposed[1].reflections
    }
    assert {'speaker-sl', 'speaker-sr'} <= proposed_speakers
    current_speakers = {
        item.speaker_entity_id for item in current[1].reflections
    }
    assert 'speaker-sl' not in current_speakers

    # Current and proposed outputs are distinct canonical results anchored
    # on the same baseline revision — never a shared cache entry.
    assert proposed[1].input_hash != current[1].input_hash
    assert proposed[1].result_sha256 != current[1].result_sha256
    assert all(
        item.scene_revision_id == baseline.revision_id for item in proposed
    )
    assert scene_repository.latest(F1_DOCUMENT_ID) == baseline


def test_variant_request_identity_differs_even_with_identical_geometry(
    tmp_path: Path,
) -> None:
    scene_repository, baseline, _variant_repository, _variant = _fixture(
        tmp_path
    )
    # A variant that changes nothing physically still gets a distinct exact
    # request identity — "same room" is not "same prediction".
    no_op = build_system_variant(
        baseline=baseline,
        name='Proposal naming pass',
        role_bindings=_roles('FL', 'C', 'FR'),
        proposed_entities=(),
        created_at_utc=NOW,
    )
    identity = rectangular_geometry_request_identity(
        baseline, RECEIVER, system_variant=no_op
    )
    current = rectangular_geometry_request_identity(baseline, RECEIVER)
    assert identity.input_hash != current.input_hash
    import json

    snapshot = json.loads(identity.input_snapshot_json)
    assert snapshot['system_variant']['variant_sha256'] == no_op.variant_sha256


def test_receiver_added_by_variant_is_resolvable(tmp_path: Path) -> None:
    scene_repository, baseline, variant_repository, _variant = _fixture(
        tmp_path
    )
    variant = build_system_variant(
        baseline=baseline,
        name='Proposal with rear seat receiver',
        role_bindings=_roles('FL', 'C', 'FR'),
        proposed_entities=(
            ProposedEntitySpec(
                spec_id='proposal-rear-seat',
                entity=SceneEntity(
                    entity_id='seat-rear',
                    kind='seat',
                    name='Rear seat',
                    position=Position3(x_m=3.0, y_m=1.0, z_m=1.1),
                    size_m=Size3(x_m=0.6, y_m=0.6, z_m=1.0),
                    acoustic_reference_offset_m=Offset3(),
                ),
            ),
        ),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant)
    prediction = _controller(scene_repository)

    # The receiver only exists in the proposed scene — baseline prediction
    # fails closed instead of silently substituting it.
    with pytest.raises(KeyError):
        prediction.prepare_run('seat-rear')

    spec = prediction.prepare_run(
        'seat-rear', system_variant_id=variant.variant_id
    )
    assert spec.system_variant == variant
    import json

    snapshot = json.loads(spec.identity.input_snapshot_json)
    assert snapshot['receiver_entity_id'] == 'seat-rear'
    prediction.dispose()


@pytest.fixture(autouse=True)
def _offscreen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')


def test_variant_baseline_mismatch_and_unpersisted_fail_closed(
    tmp_path: Path,
) -> None:
    scene_repository, baseline, variant_repository, variant = _fixture(
        tmp_path
    )
    prediction = _controller(scene_repository)

    with pytest.raises(ValueError, match='存在しません'):
        prediction.prepare_run(RECEIVER, system_variant_id='missing-variant')

    # A variant built on a different baseline cannot target this revision.
    # A variant bound to a different baseline cannot target the saved
    # revision — the proposal's exact baseline binding is enforced.
    moved = scene_repository.save(
        baseline.document.model_copy(
            update={
                'entities': tuple(
                    entity.model_copy(
                        update={
                            'position': entity.position.model_copy(
                                update={'x_m': 1.3}
                            )
                        }
                    )
                    if entity.entity_id == 'speaker-fl'
                    else entity
                    for entity in baseline.document.entities
                )
            }
        ),
        parent_revision_id=baseline.revision_id,
    ).revision
    other = build_system_variant(
        baseline=moved,
        name='Proposal on newer revision',
        role_bindings=_roles('FL', 'C', 'FR'),
        proposed_entities=(),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(other)
    with pytest.raises(ValueError, match='現在のシーンリビジョンを基にしていません'):
        prediction.prepare_run(
            RECEIVER, system_variant_id=other.variant_id
        )
    assert scene_repository.latest(F1_DOCUMENT_ID) == moved
    prediction.dispose()


def test_variant_bound_options_keep_provider_staleness_explicit(
    tmp_path: Path,
) -> None:
    scene_repository, _baseline, _variant_repository, variant = _fixture(
        tmp_path
    )
    prediction = _controller(scene_repository)

    options = prediction.prediction_options(
        RECEIVER, system_variant_id=variant.variant_id
    )
    rectangular = next(
        item for item in options if item.model_key == 'rectangular'
    )
    # The proposed target is runnable under the rectangular lane against
    # the materialized scene — never a silent fallback to baseline input.
    assert rectangular.state == 'READY'
    prediction.dispose()
