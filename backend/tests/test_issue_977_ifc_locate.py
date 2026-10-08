"""Issue #977: locate IFC-source defects — anchors, markers, repair routing.

``defect_locate_targets`` now also returns non-scene part refs
(``ifc:<step_id>``); the workspace resolves them against the intake
subject into overlay anchors and focuses the matching repair actions.
"""
from __future__ import annotations

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_geometry_intake import (
    IntakeMeshPart,
    GeometryIntakeSubject,
    IntakePartStub,
    defect_locate_targets,
    defect_source_part_refs,
    diagnose_geometry_intake,
    intake_locate_anchors,
    proposal_actions_for_parts,
    propose_geometry_repairs,
)
from htdt.raw_mesh import import_raw_visual_mesh

UTC = '2026-10-08T00:00:00Z'

_TRI_OBJ = b'''v 1.0 2.0 3.0
v 3.0 2.0 3.0
v 1.0 4.0 5.0
f 1 2 3
'''


def _mesh(asset=_TRI_OBJ):
    return import_raw_visual_mesh(asset, source_name='fixture.obj')


def _part(mesh, part_id, *, owner_ref='e1'):
    return IntakeMeshPart(
        part_id=part_id,
        owner_kind='entity_body',
        owner_ref=owner_ref,
        role='object_surface',
        mesh=mesh,
        material_state='unassigned',
        material_label=None,
    )


def _subject(*, parts=(), stubs=()):
    return GeometryIntakeSubject.create(
        document_id='doc-1',
        source_kind='ifc_import',
        source_refs=(
            AuthorityRef(
                kind='ifc_file', ref_id='ifc-1', ref_sha256='a' * 64
            ),
        ),
        parts=parts,
        unresolved_parts=stubs,
        openings=(),
        unit_state='declared',
        unit_details='fixture units declared by operator',
    )


def test_locate_targets_include_ifc_part_refs():
    subject = _subject(parts=(_part(_mesh(), 'ifc:77'),))
    report = diagnose_geometry_intake(subject, evaluated_at_utc=UTC)
    defect = next(
        d for d in report.defects
        if 'ifc:77' in d.part_refs or d.part_refs == ('ifc:77',)
    )
    targets = defect_locate_targets(defect)
    assert 'ifc:77' in targets
    assert defect_source_part_refs(defect) == ('ifc:77',)


def test_locate_anchor_computes_bounds_center():
    part = _part(_mesh(), 'ifc:9')
    subject = _subject(parts=(part,))
    anchors = intake_locate_anchors(subject, ('ifc:9',))
    assert len(anchors) == 1
    a = anchors[0]
    assert a.state == 'located'
    # bounds of the tri: x 1..3, y 2..4, z 3..5 -> center (2,3,4)
    assert a.center is not None
    assert a.center.x_m == pytest.approx(2.0)
    assert a.center.y_m == pytest.approx(3.0)
    assert a.center.z_m == pytest.approx(4.0)
    assert a.extent_m == pytest.approx((4 + 4 + 4) ** 0.5)
    assert a.owner_ref == 'e1'
    assert 'ifc' not in a.detail or 'ifc_source' in a.detail


def test_locate_anchor_unresolved_stub():
    stub = IntakePartStub(
        part_id='ifc:5',
        owner_kind='entity_body',
        owner_ref='e2',
        role='object_surface',
        unavailability_reason='mesh decode failed',
    )
    subject = _subject(stubs=(stub,))
    (a,) = intake_locate_anchors(subject, ('ifc:5',))
    assert a.state == 'unresolved'
    assert a.center is None
    assert 'decode' in a.detail


def test_locate_anchor_missing_part_is_honest():
    subject = _subject()
    (a,) = intake_locate_anchors(subject, ('ifc:nope',))
    assert a.state == 'missing'
    assert a.center is None


def test_proposal_actions_routed_by_target_part():
    subject = _subject(parts=(_part(_mesh(), 'ifc:9'),))
    report = diagnose_geometry_intake(subject, evaluated_at_utc=UTC)
    proposal = propose_geometry_repairs(
        report, subject, proposed_by='op', proposed_at_utc=UTC
    )
    expected = tuple(
        a.action_id
        for a in proposal.actions
        if a.target_part_id == 'ifc:9'
    )
    assert proposal_actions_for_parts(proposal, ('ifc:9',)) == expected
    assert proposal_actions_for_parts(proposal, ('ifc:other',)) == ()


def test_locate_targets_entity_mapping_unchanged():
    subject = _subject(
        parts=(_part(_mesh(), 'entity:e1'),)
    )
    report = diagnose_geometry_intake(subject, evaluated_at_utc=UTC)
    defect = next(
        (d for d in report.defects if d.part_refs == ('entity:e1',)),
        None,
    )
    if defect is not None:
        assert defect_locate_targets(defect) == ('e1',)
        assert defect_source_part_refs(defect) == ()


def test_anchor_model_rejects_inconsistent_state():
    from htdt.cad_geometry_intake import IntakeLocateAnchor
    with pytest.raises(ValueError, match='located anchors require'):
        IntakeLocateAnchor(
            part_id='ifc:1', state='located', detail='x'
        )
