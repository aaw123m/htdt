"""Issue #970: adaptive measurement design — candidate-domain authority and
deterministic next-position proposals (never fabricated measurements)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_adaptive_measurement_design import (
    AdaptiveMeasurementDesignSpec,
    CandidateSpatialDomain,
    ProposedMeasurementPoint,
    AdaptiveMeasurementProposal,
    build_adaptive_design_spec,
    propose_maxmin_positions,
)
from htdt.cad_scene import Position3

_H = 'b' * 64


def _pos(x, y, z=1.2):
    return Position3(x_m=x, y_m=y, z_m=z)


def _domain(**overrides):
    kwargs = dict(
        bounds_min=_pos(0.0, 0.0, 0.5),
        bounds_max=_pos(4.0, 4.0, 2.0),
        candidates=(
            _pos(0.5, 0.5),
            _pos(2.0, 0.5),
            _pos(3.5, 0.5),
            _pos(0.5, 3.5),
            _pos(3.5, 3.5),
        ),
        generation_rule='explicit_list',
        min_spacing_m=0.2,
    )
    kwargs.update(overrides)
    return CandidateSpatialDomain(**kwargs)


def _spec(**overrides):
    kwargs = dict(
        spec_id='ads-1',
        schema_version='adaptive_design_v1',
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash=_H,
        purpose='spatial_field_reconstruction',
        domain=_domain(),
        acquisition_function='maxmin_spacing',
        measurement_budget=3,
        stop_criterion='measurement_budget',
        deterministic_seed=7,
        created_at_utc='2026-09-25T00:00:00+00:00',
    )
    kwargs.update(overrides)
    return build_adaptive_design_spec(**kwargs)


def test_spec_is_sealed():
    spec = _spec()
    assert len(spec.spec_sha256) == 64
    AdaptiveMeasurementDesignSpec.model_validate(spec.model_dump(mode='json'))
    payload = spec.model_dump(mode='json')
    payload['purpose'] = 'modal_identification'
    with pytest.raises(ValidationError, match='hash mismatch'):
        AdaptiveMeasurementDesignSpec.model_validate(payload)


def test_candidates_must_stay_inside_domain():
    with pytest.raises(ValidationError, match='outside domain'):
        _domain(candidates=(_pos(9.0, 9.0),))


def test_exclusion_and_spacing_gate():
    domain = _domain(
        exclusion_min=(_pos(1.9, 0.4, 0.4),),
        exclusion_max=(_pos(2.1, 0.6, 2.0),),
    )
    assert domain.excluded(_pos(2.0, 0.5))
    assert not domain.excluded(_pos(0.5, 0.5))
    assert not domain.eligible(_pos(0.6, 0.5), taken=(_pos(0.5, 0.5),))


def test_maxmin_proposal_is_deterministic_and_bounded():
    spec = _spec()
    existing = (_pos(0.5, 0.5),)
    proposal = propose_maxmin_positions(
        spec,
        proposal_id='p-1',
        existing_positions=existing,
        count=2,
        iteration=0,
        created_at_utc='2026-09-25T00:00:01+00:00',
    )
    positions = [point.position for point in proposal.proposed_points]
    # farthest from (0.5, 0.5) is (3.5, 3.5); next is (3.5, 0.5) or (0.5, 3.5)
    assert positions[0] == _pos(3.5, 3.5)
    assert proposal.algorithm_version == 'htdt_adaptive_maxmin_v1'
    AdaptiveMeasurementProposal.model_validate(proposal.model_dump(mode='json'))


def test_exhausted_domain_reports_stop_not_fabrication():
    spec = _spec()
    proposal = propose_maxmin_positions(
        spec,
        proposal_id='p-2',
        existing_positions=tuple(spec.domain.candidates),
        count=1,
        iteration=1,
        created_at_utc='2026-09-25T00:00:02+00:00',
    )
    assert proposal.proposed_points == ()
    assert proposal.stop_reached


def test_proposal_is_not_a_measurement():
    # proposals carry positions+rationale only; nothing claims evidence
    proposal = propose_maxmin_positions(
        _spec(),
        proposal_id='p-3',
        existing_positions=(),
        count=1,
        iteration=0,
        created_at_utc='2026-09-25T00:00:03+00:00',
    )
    assert proposal.state == 'proposed'
    assert proposal.proposed_points[0].score_semantics == 'min_distance_to_existing_m'


def test_explicit_list_requires_candidates():
    with pytest.raises(ValidationError, match='requires candidates'):
        _domain(candidates=None, generation_rule='explicit_list')


def test_budget_marks_stop():
    proposal = propose_maxmin_positions(
        _spec(measurement_budget=2),
        proposal_id='p-4',
        existing_positions=(_pos(0.5, 0.5),),
        count=1,
        iteration=0,
        created_at_utc='2026-09-25T00:00:04+00:00',
    )
    assert proposal.stop_reached
