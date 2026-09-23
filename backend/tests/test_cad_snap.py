import pytest

from htdt.cad_scene import Position3, make_f1_scene
from htdt.cad_snap import (
    SnapCandidate, SnapSelector, generate_snap_candidates,
    snap_angle_deg, snap_position_axis, snap_scalar,
)


def test_snap_scalar_uses_half_away_from_zero() -> None:
    assert snap_scalar(0.125, 0.05) == pytest.approx(0.15)
    assert snap_scalar(-0.125, 0.05) == pytest.approx(-0.15)


def test_grid_snap_changes_only_active_world_axis() -> None:
    original = Position3(x_m=1.234, y_m=2.345, z_m=0.987)
    snapped = snap_position_axis(original, 'y', 0.05)
    assert snapped.x_m == original.x_m
    assert snapped.y_m == pytest.approx(2.35)
    assert snapped.z_m == original.z_m


def test_angle_snap_is_independent_from_grid_step() -> None:
    assert snap_angle_deg(22.4, 15.0) == pytest.approx(15.0)
    assert snap_angle_deg(22.6, 15.0) == pytest.approx(30.0)


def test_invalid_snap_steps_are_rejected() -> None:
    with pytest.raises(ValueError):
        snap_scalar(1.0, 0.0)
    with pytest.raises(ValueError):
        snap_angle_deg(10.0, float('nan'))


def test_geometric_candidates_exclude_moving_selection_and_have_stable_feature_ids() -> None:
    candidates = generate_snap_candidates(
        make_f1_scene(),
        exclude_ids={'speaker-fl', 'speaker-c'},
        axis='x',
        probe=Position3(x_m=1.8, y_m=0.75, z_m=1.05),
    )
    assert candidates
    assert all(candidate.entity_id not in {'speaker-fl', 'speaker-c'} for candidate in candidates)
    assert {'vertex', 'midpoint', 'edge', 'alignment'} <= {candidate.kind for candidate in candidates}
    stable_ids = [candidate.stable_id for candidate in candidates]
    assert len(stable_ids) == len(set(stable_ids))


def test_split_static_and_edge_candidates_match_full_generation() -> None:
    document = make_f1_scene()
    kwargs = {
        'exclude_ids': {'speaker-fl', 'speaker-c'},
        'axis': 'x',
        'probe': Position3(x_m=1.8, y_m=0.75, z_m=1.05),
    }
    full = generate_snap_candidates(document, **kwargs)
    static = generate_snap_candidates(
        document,
        **kwargs,
        kinds={'vertex', 'midpoint', 'alignment'},
    )
    edges = generate_snap_candidates(document, **kwargs, kinds={'edge'})
    assert {candidate.stable_id for candidate in static + edges} == {
        candidate.stable_id for candidate in full
    }
    assert all(candidate.kind != 'edge' for candidate in static)
    assert all(candidate.kind == 'edge' for candidate in edges)


def test_cached_edge_geometry_keeps_closest_point_probe_dependent() -> None:
    document = make_f1_scene()
    first = generate_snap_candidates(
        document,
        exclude_ids={'speaker-fl', 'speaker-c'},
        axis='x',
        probe=Position3(x_m=4.3, y_m=0.4, z_m=0.9),
        kinds={'edge'},
    )
    second = generate_snap_candidates(
        document,
        exclude_ids={'speaker-fl', 'speaker-c'},
        axis='x',
        probe=Position3(x_m=4.9, y_m=1.1, z_m=1.2),
        kinds={'edge'},
    )
    first_by_id = {candidate.stable_id: candidate for candidate in first}
    second_by_id = {candidate.stable_id: candidate for candidate in second}
    assert first_by_id.keys() == second_by_id.keys()
    assert any(
        first_by_id[stable_id].screen_anchor != second_by_id[stable_id].screen_anchor
        for stable_id in first_by_id
    )


def _snap_candidate(stable_id: str, kind: str, x_m: float) -> SnapCandidate:
    return SnapCandidate(
        stable_id=stable_id,
        kind=kind,  # type: ignore[arg-type]
        entity_id='target',
        target=Position3(x_m=x_m, y_m=0.0, z_m=0.0),
        screen_anchor=Position3(x_m=x_m, y_m=0.0, z_m=0.0),
        axis='x',
        label=stable_id,
    )


def test_screen_space_snap_uses_acquire_retain_hysteresis() -> None:
    selector = SnapSelector(acquire_radius_dip=8.0, retain_radius_dip=12.0)
    candidates = (_snap_candidate('a', 'vertex', 1.0), _snap_candidate('b', 'vertex', 1.5))
    project = lambda position: (position.x_m * 10.0, 0.0)

    first = selector.select(candidates, Position3(x_m=1.05, y_m=0.0, z_m=0.0), project)
    assert first is not None and first.candidate.stable_id == 'a'
    retained = selector.select(candidates, Position3(x_m=1.9, y_m=0.0, z_m=0.0), project)
    assert retained is not None and retained.candidate.stable_id == 'a'
    switched = selector.select(candidates, Position3(x_m=2.3, y_m=0.0, z_m=0.0), project)
    assert switched is not None and switched.candidate.stable_id == 'b'


def test_snap_selector_reuses_static_anchor_projections_until_reset() -> None:
    selector = SnapSelector()
    candidates = (_snap_candidate('a', 'vertex', 1.0), _snap_candidate('b', 'midpoint', 1.4))
    calls = 0

    def project(position: Position3) -> tuple[float, float]:
        nonlocal calls
        calls += 1
        return (position.x_m * 10.0, 0.0)

    selector.select(candidates, Position3(x_m=1.1, y_m=0.0, z_m=0.0), project)
    assert calls == 7  # 4 projection-signature sentinels + probe + 2 anchors
    selector.select(candidates, Position3(x_m=1.2, y_m=0.0, z_m=0.0), project)
    assert calls == 12  # signature + probe; retained anchor remains cached
    selector.reset()
    selector.select(candidates, Position3(x_m=1.2, y_m=0.0, z_m=0.0), project)
    assert calls == 19


def test_snap_selector_invalidates_projection_cache_when_camera_mapping_changes() -> None:
    selector = SnapSelector(acquire_radius_dip=8.0, retain_radius_dip=12.0)
    candidates = (_snap_candidate('a', 'vertex', 1.0),)
    scale = 10.0

    def project(position: Position3) -> tuple[float, float]:
        return (position.x_m * scale, position.y_m * scale)

    acquired = selector.select(candidates, Position3(x_m=1.05, y_m=0.0, z_m=0.0), project)
    assert acquired is not None and acquired.candidate.stable_id == 'a'

    scale = 20.0
    retained = selector.select(candidates, Position3(x_m=1.5, y_m=0.0, z_m=0.0), project)
    assert retained is not None and retained.candidate.stable_id == 'a'
    assert retained.distance_dip == pytest.approx(10.0)


def test_snap_priority_precedes_distance_and_stable_id_breaks_ties() -> None:
    selector = SnapSelector()
    project = lambda position: (position.x_m * 10.0, 0.0)
    probe = Position3(x_m=1.0, y_m=0.0, z_m=0.0)
    priority_candidates = (
        _snap_candidate('alignment-near', 'alignment', 1.1),
        _snap_candidate('vertex-farther', 'vertex', 1.6),
    )
    selected = selector.select(priority_candidates, probe, project)
    assert selected is not None and selected.candidate.stable_id == 'vertex-farther'

    selector.reset()
    tie_candidates = (
        _snap_candidate('b', 'vertex', 1.4),
        _snap_candidate('a', 'vertex', 1.4),
    )
    tied = selector.select(tie_candidates, probe, project)
    assert tied is not None and tied.candidate.stable_id == 'a'


def test_snap_selector_rejects_invalid_radii() -> None:
    with pytest.raises(ValueError):
        SnapSelector(acquire_radius_dip=0.0, retain_radius_dip=12.0)
    with pytest.raises(ValueError):
        SnapSelector(acquire_radius_dip=12.0, retain_radius_dip=8.0)