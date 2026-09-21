from __future__ import annotations

import json
import sqlite3
import threading

from pathlib import Path

import pytest

import htdt.cad_topology_search as topology_search

from htdt.cad_constraint_models import (
    CadConstraintPoint2D,
    CadConstraintSet,
    CadPairDistanceConstraint,
    CadWallClearanceConstraint,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_search_models import CadSearchAxis
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    ProposedEntitySpec,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.cad_topology_search import (
    LinkedPlacementRule,
    PlacementAngleAxis,
    ProposedExclusionRegion,
    ProposedPlacementSpec,
    build_topology_placement_search_spec,
    generate_topology_placement_candidates,
    topology_candidate_document,
    topology_candidate_to_system_variant,
)
from htdt.cad_topology_search_repository import CadTopologySearchRepository
from htdt.cad_topology_space import build_topology_search_spec
from htdt.cad_walls import make_wall_topology


DOCUMENT_ID = 'o100b-virtual-placement-fixture'
NOW = '2026-09-19T00:00:00+00:00'


def _speaker(
    entity_id: str,
    role: str,
    x_m: float,
    y_m: float,
    z_m: float,
    *,
    size: Size3 | None = None,
) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name=role,
        speaker_role=role,
        position=Position3(x_m=x_m, y_m=y_m, z_m=z_m),
        size_m=size or Size3(x_m=0.24, y_m=0.28, z_m=0.42),
        aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
    )


def _scene_302(document_id: str = DOCUMENT_ID) -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
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


def _roles_with_surround_pair() -> tuple[ChannelRoleBinding, ...]:
    return (
        ChannelRoleBinding(role_id='FL', display_name='FL'),
        ChannelRoleBinding(role_id='C', display_name='C'),
        ChannelRoleBinding(role_id='FR', display_name='FR'),
        ChannelRoleBinding(role_id='TFL', display_name='TFL'),
        ChannelRoleBinding(role_id='TFR', display_name='TFR'),
        ChannelRoleBinding(
            role_id='SL',
            display_name='SL',
            paired_role_id='SR',
        ),
        ChannelRoleBinding(
            role_id='SR',
            display_name='SR',
            paired_role_id='SL',
        ),
    )


def _proposal(entity_id: str, role: str, x_m: float) -> ProposedEntitySpec:
    return ProposedEntitySpec(
        spec_id=f'proposal-{entity_id}',
        entity=_speaker(entity_id, role, x_m, 2.9, 1.3),
        role_binding_id=role,
    )


def _region(
    min_x: float,
    max_x: float,
    min_y: float,
    max_y: float,
) -> tuple[CadConstraintPoint2D, ...]:
    return (
        CadConstraintPoint2D(x_m=min_x, y_m=min_y),
        CadConstraintPoint2D(x_m=max_x, y_m=min_y),
        CadConstraintPoint2D(x_m=max_x, y_m=max_y),
        CadConstraintPoint2D(x_m=min_x, y_m=max_y),
    )


def _baseline(tmp_path: Path, document: SceneDocument | None = None):
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(
        document or _scene_302(),
        parent_revision_id=None,
    ).revision
    return repository, revision


def test_302_to_502_pair_search_is_reproducible_and_persistable(
    tmp_path: Path,
) -> None:
    scene_repository, baseline = _baseline(tmp_path)
    before = baseline.document
    template = build_system_variant(
        baseline=baseline,
        name='Proposed 5.0.2 placement template',
        role_bindings=_roles_with_surround_pair(),
        proposed_entities=(
            _proposal('sl', 'SL', 0.8),
            _proposal('sr', 'SR', 5.2),
        ),
        created_at_utc=NOW,
    )
    variant_repository = CadSystemVariantRepository(scene_repository)
    variant_repository.save_variant(template)
    topology = build_topology_search_spec(
        baseline=baseline,
        template_variants=(template,),
        optional_role_ids=('SL', 'SR'),
        include_baseline=True,
        created_at_utc=NOW,
    )
    rebuilt_topology = build_topology_search_spec(
        baseline=baseline,
        template_variants=(template,),
        optional_role_ids=('SR', 'SL'),
        include_baseline=True,
        created_at_utc='2026-09-19T00:00:30+00:00',
    )
    assert topology.topology_search_id == rebuilt_topology.topology_search_id
    assert topology.topology_search_sha256 == rebuilt_topology.topology_search_sha256
    assert topology.include_baseline
    assert [(item.kind, item.role_id, item.optional_role) for item in topology.options[0].operations] == [
        ('add', 'SL', True),
        ('add', 'SR', True),
    ]
    option_id = topology.options[0].option_id

    placements = (
        ProposedPlacementSpec(
            entity_id='sl',
            role_id='SL',
            zone_id='left-side-wall',
            allowed_region=_region(0.5, 1.4, 2.4, 3.4),
            exclusion_regions=(
                ProposedExclusionRegion(
                    region_id='blocked-mounting-strip',
                    vertices=_region(0.95, 1.2, 2.5, 3.3),
                ),
            ),
            min_z_m=1.2,
            max_z_m=1.4,
            xyz_axes=(
                CadSearchAxis(
                    entity_id='sl',
                    axis='x',
                    min_m=0.8,
                    max_m=1.0,
                    step_m=0.2,
                ),
                CadSearchAxis(
                    entity_id='sl',
                    axis='y',
                    min_m=2.8,
                    max_m=3.0,
                    step_m=0.2,
                ),
                CadSearchAxis(
                    entity_id='sl',
                    axis='z',
                    min_m=1.2,
                    max_m=1.4,
                    step_m=0.2,
                ),
            ),
            angle_axes=(
                PlacementAngleAxis(
                    parameter='aim_yaw_deg',
                    min_deg=-10.0,
                    max_deg=10.0,
                    step_deg=20.0,
                ),
                PlacementAngleAxis(
                    parameter='aim_pitch_deg',
                    min_deg=0.0,
                    max_deg=10.0,
                    step_deg=10.0,
                ),
            ),
        ),
        ProposedPlacementSpec(
            entity_id='sr',
            role_id='SR',
            zone_id='right-side-wall',
            allowed_region=_region(4.6, 5.5, 2.4, 3.4),
            min_z_m=1.2,
            max_z_m=1.4,
            angle_axes=(
                PlacementAngleAxis(
                    parameter='aim_yaw_deg',
                    min_deg=-10.0,
                    max_deg=10.0,
                    step_deg=20.0,
                ),
            ),
        ),
    )
    links = (
        LinkedPlacementRule(
            constraint_id='surround-mirror-x',
            master_entity_id='sl',
            slave_entity_id='sr',
            relation='mirror_x',
            mirror_axis_x_m=3.0,
        ),
        LinkedPlacementRule(
            constraint_id='surround-equal-y',
            master_entity_id='sl',
            slave_entity_id='sr',
            relation='equal_y',
        ),
        LinkedPlacementRule(
            constraint_id='surround-equal-z',
            master_entity_id='sl',
            slave_entity_id='sr',
            relation='equal_z',
        ),
    )
    constraints = CadConstraintSet(
        document_id=DOCUMENT_ID,
        constraints=(),
    )
    spec = build_topology_placement_search_spec(
        baseline=baseline,
        template_variant=template,
        topology_spec=topology,
        topology_option_id=option_id,
        placement_specs=placements,
        constraint_set=constraints,
        linked_rules=links,
        candidate_limit=200,
        created_at_utc=NOW,
    )
    rebuilt_spec = build_topology_placement_search_spec(
        baseline=baseline,
        template_variant=template,
        topology_spec=topology,
        topology_option_id=option_id,
        placement_specs=tuple(reversed(placements)),
        constraint_set=constraints,
        linked_rules=tuple(reversed(links)),
        candidate_limit=200,
        created_at_utc='2026-09-19T00:01:00+00:00',
    )
    assert rebuilt_spec.search_id == spec.search_id
    assert rebuilt_spec.search_sha256 == spec.search_sha256

    first = generate_topology_placement_candidates(
        baseline=baseline,
        template_variant=template,
        spec=spec,
        limit=100,
    )
    second = generate_topology_placement_candidates(
        baseline=baseline,
        template_variant=template,
        spec=spec,
        limit=100,
    )

    assert first.raw_candidate_count == 64
    assert first.feasible_candidate_count == 32
    assert first.rejected_candidate_count == 32
    assert first.duplicate_candidate_count == 0
    assert first.candidate_set_sha256 == second.candidate_set_sha256
    assert [item.candidate_id for item in first.candidates] == [
        item.candidate_id for item in second.candidates
    ]
    assert [item.candidate_sha256 for item in first.candidates] == [
        item.candidate_sha256 for item in second.candidates
    ]

    candidate = first.candidates[-1]
    assert candidate.positions['sr']['x_m'] == pytest.approx(
        6.0 - candidate.positions['sl']['x_m']
    )
    assert candidate.positions['sr']['y_m'] == pytest.approx(
        candidate.positions['sl']['y_m']
    )
    assert candidate.positions['sr']['z_m'] == pytest.approx(
        candidate.positions['sl']['z_m']
    )
    assert candidate.aim_yaw_deg == {'sl': 10.0, 'sr': 10.0}
    assert candidate.aim_pitch_deg == {'sl': 10.0}

    preview = topology_candidate_document(
        baseline=baseline,
        template_variant=template,
        spec=spec,
        candidate=candidate,
    )
    assert preview.entity('sl').speaker_role == 'SL'
    assert preview.entity('sr').speaker_role == 'SR'
    assert preview.entity('sl').position.x_m == pytest.approx(
        candidate.positions['sl']['x_m']
    )
    assert preview.entity('sr').position.x_m == pytest.approx(
        candidate.positions['sr']['x_m']
    )
    assert preview.entity('sl').aim_xyz is not None
    assert preview.entity('sl').aim_xyz.z > 0.0

    child = topology_candidate_to_system_variant(
        baseline=baseline,
        template_variant=template,
        spec=spec,
        candidate=candidate,
        created_at_utc='2026-09-19T00:02:00+00:00',
    )
    regenerated_child = topology_candidate_to_system_variant(
        baseline=baseline,
        template_variant=template,
        spec=spec,
        candidate=candidate,
        created_at_utc='2026-09-19T00:03:00+00:00',
    )
    assert child.variant_id == regenerated_child.variant_id
    assert child.variant_sha256 == regenerated_child.variant_sha256
    assert child.parent_variant_id == template.variant_id
    lifecycle = {item.entity_id: item for item in child.entity_lifecycle}
    assert lifecycle['sl'].state == 'proposed'
    assert lifecycle['sr'].state == 'proposed'
    assert lifecycle['sl'].measurement_ids == ()
    assert lifecycle['sr'].measurement_ids == ()

    topology_repository = CadTopologySearchRepository(variant_repository)
    topology_repository.save_topology_spec(topology)
    topology_repository.save_spec(spec)
    topology_repository.save_candidate_page(first)
    topology_repository.save_candidate_variant(candidate.candidate_id, child)
    assert topology_repository.get_topology_spec(topology.topology_search_id) == topology
    assert topology_repository.get_spec(spec.search_id) == spec
    assert topology_repository.get_candidate(candidate.candidate_id) == candidate
    assert topology_repository.variant_for_candidate(candidate.candidate_id) == child
    comparison = topology_repository.comparison_ref(candidate.candidate_id)
    assert comparison.variant_id == child.variant_id
    assert comparison.variant_sha256 == child.variant_sha256
    assert comparison.topology_search_id == topology.topology_search_id
    assert comparison.topology_option_id == option_id
    assert comparison.applied_revision_id is None

    # Search/persistence stays proposal-only: no temporary SceneRevision is created.
    assert baseline.document == before
    assert scene_repository.get(baseline.revision_id).document == before
    assert scene_repository.latest(DOCUMENT_ID).revision_id == baseline.revision_id

    tampered_positions = {
        entity_id: dict(position)
        for entity_id, position in candidate.positions.items()
    }
    tampered_positions['sl']['x_m'] += 0.01
    tampered = candidate.model_copy(update={'positions': tampered_positions})
    with pytest.raises(ValueError, match='identity mismatch'):
        topology_candidate_document(
            baseline=baseline,
            template_variant=template,
            spec=spec,
            candidate=tampered,
        )

    # A self-consistent hash is not enough: conversion must accept only an exact
    # member of the deterministic O10/O100B search grid.
    forged_aim = dict(candidate.aim_yaw_deg)
    forged_aim['sl'] = 5.0
    forged_payload = candidate.identity_payload()
    forged_payload['aim_yaw_deg'] = forged_aim
    forged_sha = topology_search._digest(forged_payload)
    forged = candidate.model_copy(update={
        'aim_yaw_deg': forged_aim,
        'candidate_sha256': forged_sha,
        'candidate_id': 'tpc-' + forged_sha[:20],
    })
    with pytest.raises(ValueError, match='exact deterministic search member'):
        topology_candidate_document(
            baseline=baseline,
            template_variant=template,
            spec=spec,
            candidate=forged,
        )


def test_body_yaw_reuses_o80_oriented_allowed_region_rejection(
    tmp_path: Path,
) -> None:
    document_id = 'o100b-body-yaw-fixture'
    baseline_document = SceneDocument(
        document_id=document_id,
        room=RoomPrism(width_m=4.0, depth_m=4.0, height_m=2.4),
        entities=(
            _speaker('fl', 'FL', 1.0, 0.8, 1.0),
            _speaker('c', 'C', 2.0, 0.6, 0.9),
            _speaker('fr', 'FR', 3.0, 0.8, 1.0),
            _speaker('tfl', 'TFL', 1.4, 2.0, 2.2),
            _speaker('tfr', 'TFR', 2.6, 2.0, 2.2),
            SceneEntity(
                entity_id='mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=2.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )
    scene_repository, baseline = _baseline(tmp_path, baseline_document)
    surround = ProposedEntitySpec(
        spec_id='proposal-sl',
        entity=_speaker(
            'sl',
            'SL',
            0.6,
            2.0,
            1.2,
            size=Size3(x_m=0.2, y_m=1.0, z_m=0.4),
        ),
        role_binding_id='SL',
    )
    template = build_system_variant(
        baseline=baseline,
        name='Proposed surround body-yaw template',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='FL'),
            ChannelRoleBinding(role_id='C', display_name='C'),
            ChannelRoleBinding(role_id='FR', display_name='FR'),
            ChannelRoleBinding(role_id='TFL', display_name='TFL'),
            ChannelRoleBinding(role_id='TFR', display_name='TFR'),
            ChannelRoleBinding(role_id='SL', display_name='SL'),
        ),
        proposed_entities=(surround,),
        created_at_utc=NOW,
    )
    topology = build_topology_search_spec(
        baseline=baseline,
        template_variants=(template,),
        created_at_utc=NOW,
    )
    spec = build_topology_placement_search_spec(
        baseline=baseline,
        template_variant=template,
        topology_spec=topology,
        topology_option_id=topology.options[0].option_id,
        placement_specs=(
            ProposedPlacementSpec(
                entity_id='sl',
                role_id='SL',
                zone_id='narrow-left-zone',
                allowed_region=_region(0.4, 0.8, 1.3, 2.7),
                xyz_axes=(
                    CadSearchAxis(
                        entity_id='sl',
                        axis='x',
                        min_m=0.6,
                        max_m=0.6,
                        step_m=0.1,
                    ),
                ),
                angle_axes=(
                    PlacementAngleAxis(
                        parameter='body_yaw_deg',
                        min_deg=0.0,
                        max_deg=90.0,
                        step_deg=90.0,
                    ),
                ),
            ),
        ),
        constraint_set=CadConstraintSet(
            document_id=document_id,
            constraints=(),
        ),
        candidate_limit=10,
        created_at_utc=NOW,
    )

    page = generate_topology_placement_candidates(
        baseline=baseline,
        template_variant=template,
        spec=spec,
        limit=10,
    )

    assert page.raw_candidate_count == 2
    assert page.feasible_candidate_count == 1
    assert page.rejected_candidate_count == 1
    assert sum(page.rejection_counts.values()) == 1
    assert next(iter(page.rejection_counts)).startswith('o100b-zone-')
    assert page.candidates[0].body_yaw_deg == {'sl': 0.0}
    assert scene_repository.latest(document_id).revision_id == baseline.revision_id


def test_body_yaw_can_make_template_infeasible_xyz_feasible_and_round_trip(
    tmp_path: Path,
) -> None:
    document_id = 'o100b-body-yaw-rotated-fit-fixture'
    baseline_document = SceneDocument(
        document_id=document_id,
        room=RoomPrism(width_m=4.0, depth_m=4.0, height_m=2.4),
        entities=(
            _speaker('fl', 'FL', 1.0, 0.8, 1.0),
            _speaker('c', 'C', 2.0, 0.6, 0.9),
            _speaker('fr', 'FR', 3.0, 0.8, 1.0),
            _speaker('tfl', 'TFL', 1.4, 2.0, 2.2),
            _speaker('tfr', 'TFR', 2.6, 2.0, 2.2),
            SceneEntity(
                entity_id='mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=2.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )
    scene_repository, baseline = _baseline(tmp_path, baseline_document)
    template = build_system_variant(
        baseline=baseline,
        name='Proposed rotated-fit surround',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='FL'),
            ChannelRoleBinding(role_id='C', display_name='C'),
            ChannelRoleBinding(role_id='FR', display_name='FR'),
            ChannelRoleBinding(role_id='TFL', display_name='TFL'),
            ChannelRoleBinding(role_id='TFR', display_name='TFR'),
            ChannelRoleBinding(role_id='SL', display_name='SL'),
        ),
        proposed_entities=(
            ProposedEntitySpec(
                spec_id='proposal-sl',
                entity=_speaker(
                    'sl',
                    'SL',
                    0.6,
                    2.0,
                    1.2,
                    size=Size3(x_m=1.0, y_m=0.2, z_m=0.4),
                ),
                role_binding_id='SL',
            ),
        ),
        created_at_utc=NOW,
    )
    variant_repository = CadSystemVariantRepository(scene_repository)
    variant_repository.save_variant(template)
    topology = build_topology_search_spec(
        baseline=baseline,
        template_variants=(template,),
        created_at_utc=NOW,
    )
    spec = build_topology_placement_search_spec(
        baseline=baseline,
        template_variant=template,
        topology_spec=topology,
        topology_option_id=topology.options[0].option_id,
        placement_specs=(
            ProposedPlacementSpec(
                entity_id='sl',
                role_id='SL',
                zone_id='narrow-left-zone',
                allowed_region=_region(0.4, 0.8, 1.3, 2.7),
                xyz_axes=(
                    CadSearchAxis(
                        entity_id='sl',
                        axis='x',
                        min_m=0.6,
                        max_m=0.6,
                        step_m=0.1,
                    ),
                ),
                angle_axes=(
                    PlacementAngleAxis(
                        parameter='body_yaw_deg',
                        min_deg=90.0,
                        max_deg=90.0,
                        step_deg=1.0,
                    ),
                ),
            ),
        ),
        constraint_set=CadConstraintSet(
            document_id=document_id,
            constraints=(),
        ),
        candidate_limit=10,
        created_at_utc=NOW,
    )

    assert spec.algorithm_version == 'o100b-o10-o80-grid-2'
    page = generate_topology_placement_candidates(
        baseline=baseline,
        template_variant=template,
        spec=spec,
        limit=10,
    )
    assert page.raw_candidate_count == 1
    assert page.feasible_candidate_count == 1
    assert page.rejected_candidate_count == 0
    assert page.duplicate_candidate_count == 0
    candidate = page.candidates[0]
    assert candidate.body_yaw_deg == {'sl': 90.0}

    preview = topology_candidate_document(
        baseline=baseline,
        template_variant=template,
        spec=spec,
        candidate=candidate,
    )
    assert preview.entity('sl').position.x_m == pytest.approx(0.6)

    child = topology_candidate_to_system_variant(
        baseline=baseline,
        template_variant=template,
        spec=spec,
        candidate=candidate,
        created_at_utc='2026-09-19T00:02:00+00:00',
    )
    provenance = {item.key: item.value for item in child.provenance}
    assert provenance['o100b.algorithm_version'] == 'o100b-o10-o80-grid-2'
    topology_repository = CadTopologySearchRepository(variant_repository)
    topology_repository.save_topology_spec(topology)
    topology_repository.save_spec(spec)
    topology_repository.save_candidate_page(page)
    topology_repository.save_candidate_variant(candidate.candidate_id, child)

    reopened_scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    reopened_variant_repository = CadSystemVariantRepository(
        reopened_scene_repository
    )
    reopened_topology_repository = CadTopologySearchRepository(
        reopened_variant_repository
    )
    assert reopened_topology_repository.get_spec(spec.search_id) == spec
    assert reopened_topology_repository.get_candidate(candidate.candidate_id) == candidate
    assert (
        reopened_topology_repository.variant_for_candidate(candidate.candidate_id)
        == child
    )
    assert reopened_scene_repository.latest(document_id).revision_id == baseline.revision_id


@pytest.mark.parametrize(
    ('case', 'expected_rejection'),
    (
        ('room', '__room_boundary__:sl'),
        ('exclusion', 'o100b-exclusion-'),
        ('wall', 'right-wall-clearance'),
        ('pair', 'speaker-pair-clearance'),
    ),
)
def test_body_yaw_final_pose_rejects_orientation_sensitive_constraints(
    tmp_path: Path,
    case: str,
    expected_rejection: str,
) -> None:
    document_id = f'o100b-body-yaw-{case}-rejection'
    room = RoomPrism(width_m=4.0, depth_m=4.0, height_m=2.4)
    sl_x = 0.25 if case == 'room' else 3.4 if case == 'wall' else 0.6
    fl_position = (1.3, 2.0) if case == 'pair' else (1.0, 0.8)
    baseline_document = SceneDocument(
        document_id=document_id,
        schema_version=3,
        room=room,
        wall_topology=make_wall_topology(room),
        entities=(
            _speaker('fl', 'FL', fl_position[0], fl_position[1], 1.0),
            _speaker('c', 'C', 2.0, 0.6, 0.9),
            _speaker('fr', 'FR', 3.0, 0.8, 1.0),
            _speaker('tfl', 'TFL', 1.4, 2.0, 2.2),
            _speaker('tfr', 'TFR', 2.6, 2.0, 2.2),
            SceneEntity(
                entity_id='mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=2.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )
    scene_repository, baseline = _baseline(tmp_path, baseline_document)
    template = build_system_variant(
        baseline=baseline,
        name=f'Proposed {case} rejection surround',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='FL'),
            ChannelRoleBinding(role_id='C', display_name='C'),
            ChannelRoleBinding(role_id='FR', display_name='FR'),
            ChannelRoleBinding(role_id='TFL', display_name='TFL'),
            ChannelRoleBinding(role_id='TFR', display_name='TFR'),
            ChannelRoleBinding(role_id='SL', display_name='SL'),
        ),
        proposed_entities=(
            ProposedEntitySpec(
                spec_id='proposal-sl',
                entity=_speaker(
                    'sl',
                    'SL',
                    sl_x,
                    2.0,
                    1.2,
                    size=Size3(x_m=0.2, y_m=1.0, z_m=0.4),
                ),
                role_binding_id='SL',
            ),
        ),
        created_at_utc=NOW,
    )
    topology = build_topology_search_spec(
        baseline=baseline,
        template_variants=(template,),
        created_at_utc=NOW,
    )

    exclusions = ()
    if case == 'exclusion':
        exclusions = (
            ProposedExclusionRegion(
                region_id='rotated-overlap',
                vertices=_region(0.95, 1.15, 1.8, 2.2),
            ),
        )

    constraints = ()
    if case == 'wall':
        constraints = (
            CadWallClearanceConstraint(
                constraint_id='right-wall-clearance',
                name='Right wall clearance',
                entity_ids=('sl',),
                wall_id='wall:front-right->rear-right',
                min_m=0.3,
            ),
        )
    elif case == 'pair':
        constraints = (
            CadPairDistanceConstraint(
                constraint_id='speaker-pair-clearance',
                name='Speaker pair clearance',
                entity_a='sl',
                entity_b='fl',
                min_m=0.3,
                distance_mode='horizontal_xy',
                distance_reference='envelope_clearance',
            ),
        )

    spec = build_topology_placement_search_spec(
        baseline=baseline,
        template_variant=template,
        topology_spec=topology,
        topology_option_id=topology.options[0].option_id,
        placement_specs=(
            ProposedPlacementSpec(
                entity_id='sl',
                role_id='SL',
                zone_id='rotation-sensitive-zone',
                allowed_region=_region(0.0, 4.0, 0.0, 4.0),
                exclusion_regions=exclusions,
                xyz_axes=(
                    CadSearchAxis(
                        entity_id='sl',
                        axis='x',
                        min_m=sl_x,
                        max_m=sl_x,
                        step_m=0.1,
                    ),
                ),
                angle_axes=(
                    PlacementAngleAxis(
                        parameter='body_yaw_deg',
                        min_deg=90.0,
                        max_deg=90.0,
                        step_deg=1.0,
                    ),
                ),
            ),
        ),
        constraint_set=CadConstraintSet(
            document_id=document_id,
            constraints=constraints,
        ),
        candidate_limit=10,
        created_at_utc=NOW,
    )
    page = generate_topology_placement_candidates(
        baseline=baseline,
        template_variant=template,
        spec=spec,
        limit=10,
    )

    assert page.raw_candidate_count == 1
    assert page.feasible_candidate_count == 0
    assert page.rejected_candidate_count == 1
    assert page.duplicate_candidate_count == 0
    assert page.candidates == ()
    if expected_rejection.endswith('-'):
        assert any(
            constraint_id.startswith(expected_rejection)
            for constraint_id in page.rejection_counts
        )
    else:
        assert expected_rejection in page.rejection_counts
    assert scene_repository.latest(document_id).revision_id == baseline.revision_id


def test_topology_search_spec_reuses_o100a_add_remove_replace_diff(
    tmp_path: Path,
) -> None:
    _scene_repository, baseline = _baseline(tmp_path)
    replacement_fr = ProposedEntitySpec(
        spec_id='proposal-fr-replacement',
        entity=_speaker('fr', 'FR', 4.5, 1.0, 1.1),
        role_binding_id='FR',
    )
    surround = _proposal('sl', 'SL', 0.8)
    variant = build_system_variant(
        baseline=baseline,
        name='Explicit topology operations',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='FL'),
            ChannelRoleBinding(role_id='FR', display_name='FR'),
            ChannelRoleBinding(role_id='TFL', display_name='TFL'),
            ChannelRoleBinding(role_id='TFR', display_name='TFR'),
            ChannelRoleBinding(role_id='SL', display_name='SL'),
        ),
        proposed_entities=(replacement_fr, surround),
        remove_entity_ids=('c',),
        created_at_utc=NOW,
    )

    topology = build_topology_search_spec(
        baseline=baseline,
        template_variants=(variant,),
        optional_role_ids=('SL',),
        include_baseline=True,
        created_at_utc=NOW,
    )

    assert [
        (
            operation.kind,
            operation.entity_id,
            operation.role_id,
            operation.optional_role,
        )
        for operation in topology.options[0].operations
    ] == [
        ('remove', 'c', 'C', False),
        ('replace', 'fr', 'FR', False),
        ('add', 'sl', 'SL', True),
    ]


def _persisted_placement_spec(tmp_path: Path):
    """Persist baseline, template, topology space, and an honest O100B spec."""

    scene_repository, baseline = _baseline(tmp_path)
    template = build_system_variant(
        baseline=baseline,
        name='Proposed surround pair template',
        role_bindings=_roles_with_surround_pair(),
        proposed_entities=(
            _proposal('sl', 'SL', 0.8),
            _proposal('sr', 'SR', 5.2),
        ),
        created_at_utc=NOW,
    )
    variant_repository = CadSystemVariantRepository(scene_repository)
    variant_repository.save_variant(template)
    topology = build_topology_search_spec(
        baseline=baseline,
        template_variants=(template,),
        optional_role_ids=('SL', 'SR'),
        created_at_utc=NOW,
    )
    placements = (
        ProposedPlacementSpec(
            entity_id='sl',
            role_id='SL',
            zone_id='left-side-wall',
            allowed_region=_region(0.5, 1.4, 2.4, 3.4),
            min_z_m=1.2,
            max_z_m=1.4,
            xyz_axes=(
                CadSearchAxis(
                    entity_id='sl',
                    axis='x',
                    min_m=0.8,
                    max_m=1.0,
                    step_m=0.2,
                ),
                CadSearchAxis(
                    entity_id='sl',
                    axis='y',
                    min_m=2.8,
                    max_m=3.0,
                    step_m=0.2,
                ),
            ),
        ),
        ProposedPlacementSpec(
            entity_id='sr',
            role_id='SR',
            zone_id='right-side-wall',
            allowed_region=_region(4.6, 5.5, 2.4, 3.4),
            min_z_m=1.2,
            max_z_m=1.4,
        ),
    )
    links = (
        LinkedPlacementRule(
            constraint_id='surround-mirror-x',
            master_entity_id='sl',
            slave_entity_id='sr',
            relation='mirror_x',
            mirror_axis_x_m=3.0,
        ),
        LinkedPlacementRule(
            constraint_id='surround-equal-y',
            master_entity_id='sl',
            slave_entity_id='sr',
            relation='equal_y',
        ),
    )
    spec = build_topology_placement_search_spec(
        baseline=baseline,
        template_variant=template,
        topology_spec=topology,
        topology_option_id=topology.options[0].option_id,
        placement_specs=placements,
        constraint_set=CadConstraintSet(
            document_id=DOCUMENT_ID,
            constraints=(),
        ),
        linked_rules=links,
        candidate_limit=200,
        created_at_utc=NOW,
    )
    repository = CadTopologySearchRepository(variant_repository)
    repository.save_topology_spec(topology)
    repository.save_spec(spec)
    return repository, spec


def _rebound_spec(spec, **updates):
    """Retimestamp a spec so tampered payloads keep self-consistent hashes."""

    tampered = spec.model_copy(update=updates)
    search_sha = topology_search._digest(tampered.identity_payload())
    return tampered.model_copy(update={
        'search_sha256': search_sha,
        'search_id': 'tps-' + search_sha[:20],
    })


def test_save_spec_rejects_noncanonical_execution_payloads(tmp_path: Path) -> None:
    repository, spec = _persisted_placement_spec(tmp_path)

    # The canonical compiler output saves idempotently and reopens unchanged.
    repository.save_spec(spec)
    assert repository.get_spec(spec.search_id) == spec
    assert repository.list_specs(spec.document_id) == (spec,)

    # A coherently rewritten G10 spec (recomputed hashes) is rejected.
    g10_spec = json.loads(spec.g10_constraint_spec_json)
    g10_spec['constraints'] = [
        item
        for item in g10_spec['constraints']
        if item.get('kind') != 'allowed_region'
    ]
    with pytest.raises(ValueError, match='canonical compilation'):
        repository.save_spec(_rebound_spec(
            spec,
            g10_constraint_spec_json=topology_search._canonical(g10_spec),
            g10_constraint_spec_sha256=topology_search._digest(g10_spec),
        ))

    # A coherently rewritten O10 axis payload is rejected.
    o10_spec = json.loads(spec.o10_search_spec_json)
    o10_spec['axes'][0]['max_m'] += 0.2
    with pytest.raises(ValueError, match='canonical compilation'):
        repository.save_spec(_rebound_spec(
            spec,
            o10_search_spec_json=topology_search._canonical(o10_spec),
        ))

    # A coherently rewritten O10 linked-derivation payload is rejected.
    o10_spec = json.loads(spec.o10_search_spec_json)
    o10_spec['linked_derivations'] = []
    with pytest.raises(ValueError, match='canonical compilation'):
        repository.save_spec(_rebound_spec(
            spec,
            o10_search_spec_json=topology_search._canonical(o10_spec),
        ))

    # Derived counts that disagree with canonical compilation are rejected.
    with pytest.raises(ValueError, match='canonical compilation'):
        repository.save_spec(_rebound_spec(
            spec,
            o10_raw_candidate_count=spec.o10_raw_candidate_count + 1,
        ))
    with pytest.raises(ValueError, match='canonical compilation'):
        repository.save_spec(_rebound_spec(
            spec,
            orientation_combination_count=spec.orientation_combination_count + 1,
        ))

    # A snapshot that does not end with the canonical placement-derived
    # constraints is rejected before compilation can even replay.
    snapshot = json.loads(spec.constraint_snapshot_json)
    snapshot['constraints'] = [
        item
        for item in snapshot['constraints']
        if item.get('kind') != 'allowed_region'
    ]
    with pytest.raises(ValueError, match='placement-derived constraints'):
        repository.save_spec(_rebound_spec(
            spec,
            constraint_snapshot_json=topology_search._canonical(snapshot),
            constraint_snapshot_sha256=topology_search._digest(snapshot),
        ))

    # Placement authority that resolves to no proposed entity fails closed.
    moved = spec.placement_specs[0].model_copy(update={'entity_id': 'ghost'})
    with pytest.raises(ValueError):
        repository.save_spec(_rebound_spec(
            spec,
            placement_specs=(moved, spec.placement_specs[1]),
        ))

    # Missing persisted authority fails closed.
    empty_repository = CadTopologySearchRepository(
        CadSystemVariantRepository(SceneRepository(tmp_path / 'other.sqlite3'))
    )
    with pytest.raises(ValueError, match='baseline SceneRevision does not exist'):
        empty_repository.save_spec(spec)


def test_persisted_spec_row_replays_canonical_authority_on_read(
    tmp_path: Path,
) -> None:
    repository, spec = _persisted_placement_spec(tmp_path)

    # After restart the canonical spec still proves its executable semantics.
    reopened = CadTopologySearchRepository(
        CadSystemVariantRepository(SceneRepository(repository.path))
    )
    assert reopened.get_spec(spec.search_id) == spec
    assert reopened.list_specs(spec.document_id) == (spec,)

    # A coherently rewritten stored payload fails closed on read.
    o10_spec = json.loads(spec.o10_search_spec_json)
    o10_spec['axes'][0]['step_m'] *= 2.0
    tampered = _rebound_spec(
        spec,
        o10_search_spec_json=topology_search._canonical(o10_spec),
    )

    connection = sqlite3.connect(repository.path)
    try:
        connection.execute(
            'UPDATE cad_topology_search_specs SET payload_json=? '
            'WHERE search_id=?',
            (tampered.model_dump_json(), spec.search_id),
        )
        connection.commit()
    finally:
        connection.close()

    # Indexed columns that disagree with the payload fail closed first.
    with pytest.raises(ValueError, match='row disagrees with its payload'):
        reopened.get_spec(spec.search_id)

    connection = sqlite3.connect(repository.path)
    try:
        connection.execute(
            'UPDATE cad_topology_search_specs '
            'SET search_id=?, search_sha256=? WHERE search_id=?',
            (tampered.search_id, tampered.search_sha256, spec.search_id),
        )
        connection.commit()
    finally:
        connection.close()

    # A self-consistent but non-canonical payload fails the replay.
    with pytest.raises(ValueError, match='canonical compilation'):
        reopened.get_spec(tampered.search_id)
    with pytest.raises(ValueError, match='canonical compilation'):
        reopened.list_specs(spec.document_id)

    # Restoring the canonical row makes the authoritative read succeed again.
    connection = sqlite3.connect(repository.path)
    try:
        connection.execute(
            'UPDATE cad_topology_search_specs '
            'SET search_id=?, search_sha256=?, payload_json=? WHERE search_id=?',
            (
                spec.search_id,
                spec.search_sha256,
                spec.model_dump_json(),
                tampered.search_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    assert reopened.get_spec(spec.search_id) == spec


def _canonical_candidate_page(
    repository: CadTopologySearchRepository,
    spec,
    *,
    offset: int = 0,
    limit: int = 500,
):
    """Regenerate the authoritative page from persisted upstream authorities."""

    baseline = repository.scene_repository.get(spec.baseline_revision_id)
    template = repository.variant_repository.get_variant(spec.template_variant_id)
    return generate_topology_placement_candidates(
        baseline=baseline,
        template_variant=template,
        spec=spec,
        offset=offset,
        limit=limit,
    )


def _forged_candidate(candidate, **field_updates):
    """Return a self-consistent non-member: mutated payload, recomputed ID."""

    updates = dict(field_updates)
    payload = candidate.identity_payload()
    for key in (
        'positions',
        'aim_yaw_deg',
        'aim_pitch_deg',
        'body_yaw_deg',
        'o10_candidate_id',
    ):
        if key in updates:
            payload[key] = updates[key]
    forged_sha = topology_search._digest(payload)
    updates['candidate_sha256'] = forged_sha
    updates['candidate_id'] = 'tpc-' + forged_sha[:20]
    return candidate.model_copy(update=updates)


def test_candidate_page_requires_canonical_regeneration(tmp_path: Path) -> None:
    repository, spec = _persisted_placement_spec(tmp_path)
    page = _canonical_candidate_page(repository, spec)

    # The honest generated page saves idempotently and reopens unchanged.
    repository.save_candidate_page(page)
    repository.save_candidate_page(page)
    candidate = page.candidates[0]
    assert repository.get_candidate(candidate.candidate_id) == candidate
    assert repository.list_candidates(spec.search_id) == page.candidates

    reopened = CadTopologySearchRepository(
        CadSystemVariantRepository(SceneRepository(repository.path))
    )
    assert reopened.get_candidate(candidate.candidate_id) == candidate
    assert reopened.list_candidates(spec.search_id) == page.candidates

    # A windowed page of the same deterministic set also persists.
    window = _canonical_candidate_page(repository, spec, offset=1, limit=1)
    reopened.save_candidate_page(window)
    assert reopened.get_candidate(window.candidates[0].candidate_id) == (
        window.candidates[0]
    )


def test_candidate_page_rejects_fabricated_results(tmp_path: Path) -> None:
    repository, spec = _persisted_placement_spec(tmp_path)
    page = _canonical_candidate_page(repository, spec)
    candidate = page.candidates[0]

    # Arbitrary positions with a correctly recomputed candidate SHA are
    # rejected: the generator would not emit them.
    forged_positions = {
        entity_id: dict(position)
        for entity_id, position in candidate.positions.items()
    }
    forged_positions['sl']['x_m'] += 0.05
    forged_member = _forged_candidate(candidate, positions=forged_positions)
    with pytest.raises(ValueError, match='canonical regeneration'):
        repository.save_candidate_page(
            page.model_copy(update={'candidates': (forged_member,)})
        )
    assert repository.get_candidate(forged_member.candidate_id) is None

    # A valid candidate paired with a fabricated candidate-set SHA fails.
    with pytest.raises(ValueError, match='canonical regeneration'):
        repository.save_candidate_page(
            page.model_copy(update={
                'candidate_set_sha256': topology_search._digest(['fabricated']),
            })
        )

    # Wrong feasible indices keep valid self-hashes but break the canonical
    # contiguous window before persistence.
    misindexed = page.model_copy(update={
        'candidates': tuple(
            item.model_copy(update={
                'feasible_index': item.feasible_index + 1,
            })
            for item in page.candidates
        ),
    })
    with pytest.raises(ValueError, match='not contiguous'):
        repository.save_candidate_page(misindexed)

    # Count metadata inconsistent with the declared accounting is rejected.
    for field, value in (
        ('raw_candidate_count', page.raw_candidate_count + 1),
        ('feasible_candidate_count', page.feasible_candidate_count + 1),
        ('rejected_candidate_count', page.rejected_candidate_count + 1),
        ('duplicate_candidate_count', page.duplicate_candidate_count + 1),
    ):
        with pytest.raises(ValueError, match='counts are inconsistent'):
            repository.save_candidate_page(
                page.model_copy(update={field: value})
            )
    # Rejection metadata that stays self-consistent still fails the replay.
    with pytest.raises(ValueError, match='canonical regeneration'):
        repository.save_candidate_page(
            page.model_copy(update={
                'rejection_counts': {**page.rejection_counts, 'ghost': 1},
            })
        )

    # A page that misstates its window is not canonical either.
    shifted = _canonical_candidate_page(repository, spec, offset=1, limit=1)
    if shifted.candidates:
        with pytest.raises(ValueError, match='not contiguous'):
            repository.save_candidate_page(
                shifted.model_copy(update={'offset': 0})
            )

    assert repository.list_candidates(spec.search_id) == ()


def test_candidate_page_requires_persisted_spec_authority(tmp_path: Path) -> None:
    repository, spec = _persisted_placement_spec(tmp_path)
    page = _canonical_candidate_page(repository, spec)

    # The spec row itself must exist before any candidate page persists.
    empty_repository = CadTopologySearchRepository(
        CadSystemVariantRepository(SceneRepository(tmp_path / 'empty.sqlite3'))
    )
    with pytest.raises(ValueError, match='must be persisted before candidates'):
        empty_repository.save_candidate_page(page)

    # A page naming another persisted search fails on the hash check.
    topology = repository.get_topology_spec(spec.topology_search_id)
    other_spec = build_topology_placement_search_spec(
        baseline=repository.scene_repository.get(spec.baseline_revision_id),
        template_variant=repository.variant_repository.get_variant(
            spec.template_variant_id
        ),
        topology_spec=topology,
        topology_option_id=spec.topology_option_id,
        placement_specs=(
            spec.placement_specs[0].model_copy(update={'min_z_m': 1.3}),
            *spec.placement_specs[1:],
        ),
        constraint_set=CadConstraintSet(
            document_id=DOCUMENT_ID,
            constraints=(),
        ),
        linked_rules=spec.linked_rules,
        candidate_limit=spec.candidate_limit,
        created_at_utc=NOW,
    )
    assert other_spec.search_id != spec.search_id
    repository.save_spec(other_spec)
    mismatched = page.model_copy(update={'search_id': other_spec.search_id})
    with pytest.raises(ValueError, match='mixes search authority'):
        repository.save_candidate_page(mismatched)


def test_persisted_candidate_row_replays_membership_on_read(
    tmp_path: Path,
) -> None:
    repository, spec = _persisted_placement_spec(tmp_path)
    page = _canonical_candidate_page(repository, spec)
    repository.save_candidate_page(page)
    candidate = page.candidates[0]

    child = topology_candidate_to_system_variant(
        baseline=repository.scene_repository.get(spec.baseline_revision_id),
        template_variant=repository.variant_repository.get_variant(
            spec.template_variant_id
        ),
        spec=spec,
        candidate=candidate,
        created_at_utc='2026-09-19T00:02:00+00:00',
    )
    repository.save_candidate_variant(candidate.candidate_id, child)
    assert repository.variant_for_candidate(candidate.candidate_id) == child

    # A fabricated but self-consistent row inserted directly into the table
    # is not a persisted membership record: reads and promotion fail closed.
    forged_positions = {
        entity_id: dict(position)
        for entity_id, position in candidate.positions.items()
    }
    forged_positions['sl']['x_m'] += 0.05
    forged = _forged_candidate(candidate, positions=forged_positions)
    connection = sqlite3.connect(repository.path)
    try:
        connection.execute(
            """
            INSERT INTO cad_topology_placement_candidates(
                candidate_id, candidate_sha256, search_id,
                candidate_set_sha256, feasible_index, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                forged.candidate_id,
                forged.candidate_sha256,
                forged.search_id,
                page.candidate_set_sha256,
                candidate.feasible_index,
                forged.model_dump_json(),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ValueError, match='exact deterministic search member'):
        repository.get_candidate(forged.candidate_id)
    with pytest.raises(ValueError, match='exact deterministic search member'):
        repository.list_candidates(spec.search_id)
    with pytest.raises(ValueError, match='exact deterministic search member'):
        repository.save_candidate_variant(forged.candidate_id, child)
    with pytest.raises(ValueError, match='exact deterministic search member'):
        repository.comparison_ref(forged.candidate_id)

    connection = sqlite3.connect(repository.path)
    try:
        connection.execute(
            'DELETE FROM cad_topology_placement_candidates WHERE candidate_id=?',
            (forged.candidate_id,),
        )
        connection.commit()
    finally:
        connection.close()
    assert repository.get_candidate(candidate.candidate_id) == candidate

    # A row whose stored candidate-set digest drifts from the regenerated
    # set is not authoritative even when the payload itself is genuine.
    connection = sqlite3.connect(repository.path)
    try:
        connection.execute(
            'UPDATE cad_topology_placement_candidates '
            'SET candidate_set_sha256=? WHERE candidate_id=?',
            (topology_search._digest(['fabricated']), candidate.candidate_id),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ValueError, match='candidate-set authority mismatch'):
        repository.get_candidate(candidate.candidate_id)
    with pytest.raises(ValueError, match='candidate-set authority mismatch'):
        repository.save_candidate_variant(candidate.candidate_id, child)

    # Indexed columns that disagree with the payload fail closed first.
    connection = sqlite3.connect(repository.path)
    try:
        connection.execute(
            'UPDATE cad_topology_placement_candidates '
            'SET candidate_set_sha256=?, feasible_index=? WHERE candidate_id=?',
            (
                page.candidate_set_sha256,
                candidate.feasible_index + 1,
                candidate.candidate_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ValueError, match='row disagrees with its payload'):
        repository.get_candidate(candidate.candidate_id)

    # A candidate row whose spec authority disappeared fails closed.
    connection = sqlite3.connect(repository.path)
    try:
        connection.execute(
            'UPDATE cad_topology_placement_candidates '
            'SET feasible_index=? WHERE candidate_id=?',
            (candidate.feasible_index, candidate.candidate_id),
        )
        connection.execute(
            'DELETE FROM cad_topology_candidate_variants WHERE candidate_id=?',
            (candidate.candidate_id,),
        )
        connection.execute(
            'DELETE FROM cad_topology_placement_candidates WHERE search_id=?',
            (spec.search_id,),
        )
        connection.execute(
            'DELETE FROM cad_topology_search_specs WHERE search_id=?',
            (spec.search_id,),
        )
        connection.execute(
            """
            INSERT INTO cad_topology_placement_candidates(
                candidate_id, candidate_sha256, search_id,
                candidate_set_sha256, feasible_index, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.candidate_id,
                candidate.candidate_sha256,
                candidate.search_id,
                page.candidate_set_sha256,
                candidate.feasible_index,
                candidate.model_dump_json(),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ValueError, match='search spec is missing'):
        repository.get_candidate(candidate.candidate_id)


def _candidate_variant(repository, spec, candidate, **kwargs):
    """Rebuild the deterministic O100B SystemVariant for one candidate."""

    return topology_candidate_to_system_variant(
        baseline=repository.scene_repository.get(spec.baseline_revision_id),
        template_variant=repository.variant_repository.get_variant(
            spec.template_variant_id
        ),
        spec=spec,
        candidate=candidate,
        created_at_utc='2026-09-19T00:02:00+00:00',
        **kwargs,
    )


def test_candidate_variant_publish_is_atomic_across_repositories(
    tmp_path: Path,
) -> None:
    repository, spec = _persisted_placement_spec(tmp_path)
    page = _canonical_candidate_page(repository, spec)
    repository.save_candidate_page(page)
    candidate = page.candidates[0]
    child = _candidate_variant(repository, spec, candidate)

    repository.save_candidate_variant(candidate.candidate_id, child)

    assert repository.variant_repository.get_variant(child.variant_id) == child
    assert repository.variant_for_candidate(candidate.candidate_id) == child
    with sqlite3.connect(repository.path) as connection:
        assert connection.execute(
            'SELECT COUNT(*) FROM cad_system_variants WHERE variant_id=?',
            (child.variant_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            'SELECT COUNT(*) FROM cad_topology_candidate_variants '
            'WHERE candidate_id=?',
            (candidate.candidate_id,),
        ).fetchone()[0] == 1

    # Repeating the identical promotion stays idempotent.
    repository.save_candidate_variant(candidate.candidate_id, child)
    assert repository.variant_for_candidate(candidate.candidate_id) == child
    with sqlite3.connect(repository.path) as connection:
        assert connection.execute(
            'SELECT COUNT(*) FROM cad_system_variants WHERE variant_id=?',
            (child.variant_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            'SELECT COUNT(*) FROM cad_topology_candidate_variants '
            'WHERE candidate_id=?',
            (candidate.candidate_id,),
        ).fetchone()[0] == 1


def test_candidate_variant_remap_rejection_leaves_no_orphan_variant(
    tmp_path: Path,
) -> None:
    repository, spec = _persisted_placement_spec(tmp_path)
    page = _canonical_candidate_page(repository, spec)
    repository.save_candidate_page(page)
    candidate = page.candidates[0]
    first = _candidate_variant(repository, spec, candidate)
    repository.save_candidate_variant(candidate.candidate_id, first)

    # A different but otherwise valid variant for the same candidate is
    # rejected and must not remain in the global variant authority.
    second = _candidate_variant(
        repository, spec, candidate, name='Alternative promotion'
    )
    assert second.variant_id != first.variant_id
    with pytest.raises(ValueError, match='already maps to another SystemVariant'):
        repository.save_candidate_variant(candidate.candidate_id, second)

    assert repository.variant_repository.get_variant(second.variant_id) is None
    assert repository.variant_for_candidate(candidate.candidate_id) == first
    with sqlite3.connect(repository.path) as connection:
        assert connection.execute(
            'SELECT COUNT(*) FROM cad_system_variants WHERE variant_id=?',
            (second.variant_id,),
        ).fetchone()[0] == 0


def test_candidate_variant_mapping_failure_rolls_back_new_variant(
    tmp_path: Path,
) -> None:
    """An injected mapping-insert failure must roll back the variant row."""
    repository, spec = _persisted_placement_spec(tmp_path)
    page = _canonical_candidate_page(repository, spec)
    repository.save_candidate_page(page)
    candidate = page.candidates[0]
    child = _candidate_variant(repository, spec, candidate)

    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            '''
            CREATE TRIGGER fail_candidate_variant_mapping_insert
            BEFORE INSERT ON cad_topology_candidate_variants
            BEGIN
                SELECT RAISE(ABORT, 'injected mapping insert failure');
            END
            '''
        )

    with pytest.raises(
        sqlite3.DatabaseError, match='injected mapping insert failure'
    ):
        repository.save_candidate_variant(candidate.candidate_id, child)

    # Neither half of the promotion may persist.
    assert repository.variant_repository.get_variant(child.variant_id) is None
    assert repository.variant_for_candidate(candidate.candidate_id) is None
    with sqlite3.connect(repository.path) as connection:
        assert connection.execute(
            'SELECT COUNT(*) FROM cad_system_variants WHERE variant_id=?',
            (child.variant_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            'SELECT COUNT(*) FROM cad_topology_candidate_variants '
            'WHERE candidate_id=?',
            (candidate.candidate_id,),
        ).fetchone()[0] == 0
        connection.execute('DROP TRIGGER fail_candidate_variant_mapping_insert')

    # Retrying the honest operation commits both halves together.
    repository.save_candidate_variant(candidate.candidate_id, child)
    assert repository.variant_for_candidate(candidate.candidate_id) == child
    assert repository.variant_repository.get_variant(child.variant_id) == child


def test_candidate_variant_reuses_independently_persisted_variant(
    tmp_path: Path,
) -> None:
    repository, spec = _persisted_placement_spec(tmp_path)
    page = _canonical_candidate_page(repository, spec)
    repository.save_candidate_page(page)
    candidate = page.candidates[0]
    child = _candidate_variant(repository, spec, candidate)

    # A variant already persisted through its own authority is mapped, not
    # duplicated.
    repository.variant_repository.save_variant(child)
    repository.save_candidate_variant(candidate.candidate_id, child)

    assert repository.variant_for_candidate(candidate.candidate_id) == child
    with sqlite3.connect(repository.path) as connection:
        assert connection.execute(
            'SELECT COUNT(*) FROM cad_system_variants WHERE variant_id=?',
            (child.variant_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            'SELECT COUNT(*) FROM cad_topology_candidate_variants '
            'WHERE candidate_id=?',
            (candidate.candidate_id,),
        ).fetchone()[0] == 1


def test_concurrent_identical_candidate_variant_promotions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Racing identical promotions serialize on the write transaction."""
    repository, spec = _persisted_placement_spec(tmp_path)
    page = _canonical_candidate_page(repository, spec)
    repository.save_candidate_page(page)
    candidate = page.candidates[0]
    child = _candidate_variant(repository, spec, candidate)

    variant_repository = repository.variant_repository
    barrier = threading.Barrier(2)
    real_require = variant_repository._require_variant_authority

    def gated_authority(variant, lineage=frozenset()):
        result = real_require(variant, lineage)
        if variant.variant_id == child.variant_id:
            # Force both workers through the "variant not yet persisted"
            # validation before either may open the write transaction.
            barrier.wait(timeout=30)
        return result

    monkeypatch.setattr(
        variant_repository, '_require_variant_authority', gated_authority
    )

    results: list[object] = []

    def worker() -> None:
        try:
            repository.save_candidate_variant(candidate.candidate_id, child)
            results.append('ok')
        except Exception as exc:  # noqa: BLE001 - collect for assertion
            results.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()
    monkeypatch.delattr(variant_repository, '_require_variant_authority')

    assert results.count('ok') == 2
    assert repository.variant_for_candidate(candidate.candidate_id) == child
    assert repository.variant_repository.get_variant(child.variant_id) == child
    with sqlite3.connect(repository.path) as connection:
        assert connection.execute(
            'SELECT COUNT(*) FROM cad_system_variants WHERE variant_id=?',
            (child.variant_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            'SELECT COUNT(*) FROM cad_topology_candidate_variants '
            'WHERE candidate_id=?',
            (candidate.candidate_id,),
        ).fetchone()[0] == 1


def test_concurrent_candidate_variant_remap_loser_leaves_no_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two different valid variants racing for one candidate: the loser fails
    inside the shared transaction and its variant is never persisted."""
    repository, spec = _persisted_placement_spec(tmp_path)
    page = _canonical_candidate_page(repository, spec)
    repository.save_candidate_page(page)
    candidate = page.candidates[0]
    first = _candidate_variant(repository, spec, candidate)
    second = _candidate_variant(
        repository, spec, candidate, name='Alternative promotion'
    )
    assert first.variant_id != second.variant_id

    variant_repository = repository.variant_repository
    barrier = threading.Barrier(2)
    promoted_ids = {first.variant_id, second.variant_id}
    real_require = variant_repository._require_variant_authority

    def gated_authority(variant, lineage=frozenset()):
        result = real_require(variant, lineage)
        if variant.variant_id in promoted_ids:
            barrier.wait(timeout=30)
        return result

    monkeypatch.setattr(
        variant_repository, '_require_variant_authority', gated_authority
    )

    results: list[object] = []

    def worker(variant) -> None:
        try:
            repository.save_candidate_variant(candidate.candidate_id, variant)
            results.append('ok')
        except Exception as exc:  # noqa: BLE001 - collect for assertion
            results.append(exc)

    threads = [
        threading.Thread(target=worker, args=(first,)),
        threading.Thread(target=worker, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()
    monkeypatch.delattr(variant_repository, '_require_variant_authority')

    assert results.count('ok') == 1
    failure = next(result for result in results if result != 'ok')
    assert isinstance(failure, ValueError)
    assert 'already maps to another SystemVariant' in str(failure)

    mapped = repository.variant_for_candidate(candidate.candidate_id)
    assert mapped is not None
    winner, loser = (
        (first, second)
        if mapped.variant_id == first.variant_id
        else (second, first)
    )
    assert repository.variant_repository.get_variant(winner.variant_id) == winner
    # The losing promotion must not remain in the global variant authority.
    assert repository.variant_repository.get_variant(loser.variant_id) is None
    with sqlite3.connect(repository.path) as connection:
        assert connection.execute(
            'SELECT COUNT(*) FROM cad_system_variants WHERE variant_id=?',
            (loser.variant_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            'SELECT COUNT(*) FROM cad_topology_candidate_variants '
            'WHERE candidate_id=?',
            (candidate.candidate_id,),
        ).fetchone()[0] == 1
