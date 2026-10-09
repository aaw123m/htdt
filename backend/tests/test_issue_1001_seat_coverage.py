"""Issue #1001 — per-seat speaker coverage markers on the room 3D view.

Exercises the REV73 read-only overlay contract end to end:

* every marker is bound to a sealed ``SeatCoverageResult`` at the EXACT
  ``receiver_reference_position_m`` — no interpolation, no invented seat;
* values/colours come from the evaluation verbatim on a FIXED discrete
  band scale; unsupported/missing/undecided draw grey hatched and are
  never painted as FAIL or 0 dB;
* speaker→seat direction lines exist only when the evaluation's
  ``source_acoustic_axis`` is evidence-determined;
* scene revision/content hash, variant and listener-identity mismatch →
  HISTORICAL (claims withheld) or blocked — never stale numbers;
* A/B Δ only for identical ear position + grid + quantity + same
  baseline revision;
* every ``coverage-overlay-*`` actor is pickable=False and cleared by
  name, by re-render, and by the signature-skipped document sweep.

Fixtures reuse the deterministic O100D authority from ``test_cad_coverage``
(sealed EquipmentDefinition + DirectivityDataset) so the numbers are the
authority's own — the tests only check they were carried, not recomputed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_coverage import evaluate_coverage
from htdt.cad_coverage_aim_repository import CadCoverageAimRepository
from htdt.cad_coverage_repository import CadCoverageRepository
from htdt.cad_directivity_repository import CadDirectivityRepository
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    quaternion_from_euler_deg,
)
from htdt.cad_seat_priority import (
    CadSeatPriorityProfileRepository,
    SeatPriorityMember,
    build_seat_priority_profile,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.cad_direct_level import SeatPopulation
from htdt.cad_coverage import build_coverage_evaluation_scenario
from htdt.room_coverage_overlay import (
    COVERAGE_BAND_COLORS,
    COVERAGE_FAIL_COLOR,
    COVERAGE_PASS_COLOR,
    COVERAGE_STALE_COLOR,
    COVERAGE_UNKNOWN_COLOR,
    CoverageOverlayRequest,
    RoomSeatCoverageOverlayController,
    resolve_coverage_overlay,
)

from test_cad_coverage import (  # noqa: E402
    DOCUMENT_ID,
    _authority,
    _binding,
    _persist_authority,
    _scenario,
    _seat,
    _speaker,
    _variant,
)


_DEFAULT_AIM = Direction3(x=0.0, y=1.0, z=0.0)


def _seat_without_reference(entity_id: str, *, x_m: float, y_m: float):
    """A seat with no acoustic reference offset → position-less result."""
    return SceneEntity(
        entity_id=entity_id,
        kind='seat',
        name=entity_id,
        position=Position3(x_m=x_m, y_m=y_m, z_m=1.0),
        size_m=Size3(x_m=0.60, y_m=0.80, z_m=1.0),
        acoustic_reference_offset_m=None,
    )


def _grid_seats(count: int) -> tuple[SceneEntity, ...]:
    """`count` seats inside the dataset's ±60°/500-1000Hz support domain."""
    seats = []
    columns = max(1, int(count ** 0.5))
    for index in range(count):
        row, col = divmod(index, columns)
        # Keep |x| small relative to depth so every seat stays in-domain.
        x_m = -2.0 + 4.0 * (col / max(columns - 1, 1)) * 0.5
        y_m = 2.0 + 0.35 * row
        seats.append(_seat(f'seat-{index:03d}', x_m=x_m, y_m=y_m))
    return tuple(seats)


def _document(
    seats: tuple[SceneEntity, ...],
    *,
    aim: Direction3 | None = Direction3(x=0.0, y=1.0, z=0.0),
) -> SceneDocument:
    return SceneDocument(
        document_id=DOCUMENT_ID,
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=6.0, height_m=2.5),
        entities=(_speaker(aim=aim),) + seats,
    )


def _workspace_repos(
    tmp_path: Path,
    seats,
    *,
    aim=_DEFAULT_AIM,
    extra_population_ids: tuple[str, ...] = (),
):
    """Persist a scene + authority + variant + scenario + evaluation.

    ``seats`` go into the scene; ``extra_population_ids`` add population
    members with NO scene entity (the 'missing' honesty case).
    Returns (scene_repository, revision, variant_repository,
    coverage_repository, aim_repository, priority_repository, evaluation,
    scenario).
    """
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = _document(seats, aim=aim)
    revision = scene_repository.save(
        document, parent_revision_id=None
    ).revision
    variant_repository = CadSystemVariantRepository(scene_repository)
    equipment_repository = CadEquipmentRepository(
        scene_repository, variant_repository
    )
    directivity_repository = CadDirectivityRepository(
        scene_repository, equipment_repository
    )
    source_bytes, definition, dataset = _authority()
    _persist_authority(
        equipment_repository,
        directivity_repository,
        definition,
        dataset,
        source_bytes,
    )
    variant = _variant(variant_repository, revision, definition)
    scenario = _scenario(
        definition,
        dataset,
        seats=tuple(seat.entity_id for seat in seats)
        + tuple(extra_population_ids),
    )
    coverage_repository = CadCoverageRepository(
        scene_repository,
        variant_repository,
        equipment_repository,
        directivity_repository,
    )
    coverage_repository.save_scenario(scenario)
    evaluation = evaluate_coverage(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        directivity_dataset=dataset,
        scenario=scenario,
    )
    coverage_repository.save_evaluation(evaluation)
    aim_repository = CadCoverageAimRepository(scene_repository)
    priority_repository = CadSeatPriorityProfileRepository(scene_repository)
    return (
        scene_repository,
        revision,
        variant_repository,
        coverage_repository,
        aim_repository,
        priority_repository,
        evaluation,
        scenario,
    )


def _resolve(fixture, **kwargs):
    (
        scene_repository,
        _revision,
        variant_repository,
        coverage_repository,
        aim_repository,
        priority_repository,
        _evaluation,
        _scenario_obj,
    ) = fixture
    kwargs.setdefault('document_id', DOCUMENT_ID)
    return resolve_coverage_overlay(
        scene_repository=scene_repository,
        variant_repository=variant_repository,
        coverage_repository=coverage_repository,
        aim_repository=aim_repository,
        priority_repository=priority_repository,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Marker placement + value honesty
# ---------------------------------------------------------------------------


def test_markers_at_exact_ear_positions_verbatim_values(tmp_path: Path):
    seats = (
        _seat('seat-on', x_m=0.20, y_m=2.0),
        _seat('seat-off', x_m=2.20, y_m=2.0),
        _seat('seat-back', x_m=-1.80, y_m=2.4),
    )
    fixture = _workspace_repos(tmp_path, seats)
    scene = _resolve(fixture, quantity='relative_level')

    assert scene.state == 'current'
    evaluation = fixture[6]
    by_id = {seat.seat_entity_id: seat for seat in evaluation.seat_results}
    assert len(scene.markers) == len(scene.seat_rows) == 3
    for marker in scene.markers:
        seat = by_id[marker.seat_entity_id]
        # Marker sits at the EXACT sealed ear position — no offset,
        # no interpolation between seats.
        assert marker.position == seat.receiver_reference_position_m
        if seat.state == 'available':
            assert marker.value == pytest.approx(
                seat.aggregated_relative_directivity_level.value
            )
            assert marker.label == f'{seat.aggregated_relative_directivity_level.value:+.1f}'
            assert marker.wireframe is False


def test_unknown_and_unsupported_are_grey_never_fail(tmp_path: Path):
    seats = (
        _seat('seat-on', x_m=0.20, y_m=2.0),
        # Far left → horizontal angle outside the ±60° dataset domain.
        _seat('seat-outside', x_m=-5.5, y_m=0.3),
        # No acoustic reference offset → position-less result, no marker.
        _seat_without_reference('seat-nopose', x_m=1.0, y_m=3.0),
    )
    fixture = _workspace_repos(tmp_path, seats)
    scene = _resolve(fixture, quantity='coverage_gate')

    by_id = {marker.seat_entity_id: marker for marker in scene.markers}
    # seat-on passes (aggregate −6 dB worst vs −6 threshold → pass),
    # seat-outside is unsupported → grey wireframe, NEVER red/fail.
    outside = by_id['seat-outside']
    assert outside.color == COVERAGE_UNKNOWN_COLOR
    assert outside.wireframe is True
    assert outside.label == 'n/a'
    assert outside.pass_state in (None, 'undecided')
    # position-less seat draws no marker but stays in the table.
    assert 'seat-nopose' not in by_id
    row = next(
        row
        for row in scene.seat_rows
        if row.seat_entity_id == 'seat-nopose'
    )
    assert row.has_marker is False
    assert row.gate_text in ('—', '未決定')
    assert row.reason is not None
    assert row.remediation is not None
    on = by_id['seat-on']
    assert on.pass_state == 'pass'
    assert on.color == COVERAGE_PASS_COLOR


def test_fixed_band_scale_is_stable_across_seat_subsets(tmp_path: Path):
    """Band boundaries are absolute — a marker's colour never depends on
    which seats happen to be visible."""
    from htdt.room_coverage_overlay import coverage_band_for_value

    assert coverage_band_for_value('relative_level', -1.0)[0] == 0
    assert coverage_band_for_value('relative_level', -6.0)[0] == 1
    assert coverage_band_for_value('relative_level', -9.0)[0] == 2
    assert coverage_band_for_value('relative_level', -12.0)[0] == 3
    assert coverage_band_for_value('relative_level', -30.0)[0] == 4
    assert coverage_band_for_value('off_axis_loss', 2.0)[1] == (
        COVERAGE_BAND_COLORS[0]
    )
    assert coverage_band_for_value('off_axis_loss', 11.0)[1] == (
        COVERAGE_BAND_COLORS[3]
    )
    # sign conventions: relative level −6 dB and loss 6 dB share a band.
    assert coverage_band_for_value('relative_level', -6.0) == (
        coverage_band_for_value('off_axis_loss', 6.0)
    )


def test_frequency_grid_exactness_blocks_off_grid(tmp_path: Path):
    fixture = _workspace_repos(
        tmp_path, (_seat('seat-on', x_m=0.20, y_m=2.0),)
    )
    scene = _resolve(fixture, frequency_hz=500.0)
    assert scene.state == 'current'
    assert scene.frequency_mode == 'frequency'
    marker = scene.markers[0]
    evaluation = fixture[6]
    assert marker.value == pytest.approx(
        evaluation.seat_results[0].frequency_results[0].relative_level_db
    )
    # Off-grid frequency → blocked, never interpolated.
    blocked = _resolve(fixture, frequency_hz=750.0)
    assert blocked.state == 'blocked'
    assert 'グリッド' in blocked.blocked_reason
    assert blocked.markers == ()


def test_undecided_gate_never_reads_as_fail_or_zero(tmp_path: Path):
    seats = (
        _seat('seat-on', x_m=0.20, y_m=2.0),
        _seat('seat-outside', x_m=-5.5, y_m=0.3),
    )
    fixture = _workspace_repos(tmp_path, seats)
    scene = _resolve(fixture, quantity='coverage_gate')
    by_id = {marker.seat_entity_id: marker for marker in scene.markers}
    outside = by_id['seat-outside']
    assert outside.label != 'FAIL'
    assert outside.color != COVERAGE_FAIL_COLOR
    assert outside.label == 'n/a'
    legend_texts = [text for text, _c in scene.legend]
    assert 'undecided' in legend_texts


# ---------------------------------------------------------------------------
# Direction rays / acoustic axis gating
# ---------------------------------------------------------------------------


def test_rays_only_when_axis_is_evidence_determined(tmp_path: Path):
    seats = (_seat('seat-on', x_m=0.20, y_m=2.0),)
    fixture = _workspace_repos(
        tmp_path, seats, aim=Direction3(x=0.0, y=1.0, z=0.0)
    )
    scene = _resolve(fixture)
    assert scene.axis_determined is True
    assert len(scene.rays) == 1
    ray = scene.rays[0]
    evaluation = fixture[6]
    assert ray.start == evaluation.source_reference_position_m
    assert ray.end == evaluation.seat_results[0].receiver_reference_position_m
    assert any(
        'geometric direction' in line for line in scene.viewport_lines
    )

    # No explicit aim_xyz → unsupported eval, no axis, no rays.
    no_aim = _workspace_repos(tmp_path / 'noaim', seats, aim=None)
    no_aim_scene = _resolve(no_aim)
    assert no_aim_scene.axis_determined is False
    assert no_aim_scene.rays == ()
    assert no_aim_scene.axis_reason is not None
    assert all(marker.wireframe for marker in no_aim_scene.markers)


# ---------------------------------------------------------------------------
# Staleness + binding verification
# ---------------------------------------------------------------------------


def _new_head(scene_repository, seats, parent_revision_id) -> object:
    """Commit a second head (seat moved) over the first revision."""
    document = _document(seats)
    return scene_repository.save(
        document, parent_revision_id=parent_revision_id
    ).revision


def test_scene_edit_turns_eval_historical_claims_withheld(tmp_path: Path):
    seats = (_seat('seat-on', x_m=0.20, y_m=2.0),)
    fixture = _workspace_repos(tmp_path, seats)
    scene_repository = fixture[0]
    scene = _resolve(fixture)
    assert scene.state == 'current'

    # Move the seat → new head; the eval stays sealed to the old revision.
    moved = (_seat('seat-on', x_m=0.40, y_m=2.2),)
    _new_head(scene_repository, moved, fixture[1].revision_id)
    stale = _resolve(fixture)
    assert stale.state == 'historical'
    assert stale.markers
    assert all(marker.wireframe for marker in stale.markers)
    assert all(m.color == COVERAGE_STALE_COLOR for m in stale.markers)
    assert all(marker.label == '' for marker in stale.markers)
    assert stale.rays == ()
    assert stale.delta_active is False
    assert any('履歴' in n for n in stale.notices)


def test_binding_mismatch_blocks_drawing(tmp_path: Path):
    fixture = _workspace_repos(
        tmp_path, (_seat('seat-on', x_m=0.20, y_m=2.0),)
    )
    scene = _resolve(fixture, evaluation_id='eval-does-not-exist')
    assert scene.state == 'blocked'
    assert '見つかりません' in scene.blocked_reason
    assert scene.markers == ()


def test_listener_identity_mismatch_flagged_not_remapped(tmp_path: Path):
    seats = (_seat('seat-on', x_m=0.20, y_m=2.0),)
    fixture = _workspace_repos(tmp_path, seats)
    scene_repository, revision = fixture[0], fixture[1]
    # Same positions, different entity id → a second revision where the
    # sealed seat id no longer names an entity. Head is unchanged content
    # hash? No — entity ids changed → content changes → new head.
    renamed = _document((_seat('seat-renamed', x_m=0.20, y_m=2.0),))
    head = scene_repository.save(renamed, parent_revision_id=revision.revision_id).revision
    assert head.revision_id != revision.revision_id
    scene = _resolve(fixture)
    # The sealed seat id no longer names an entity on the CURRENT head —
    # the display goes HISTORICAL (claims withheld), never silently
    # remapped onto another entity.
    assert scene.state == 'historical'


# ---------------------------------------------------------------------------
# A/B delta gating
# ---------------------------------------------------------------------------


def _second_variant_eval(fixture, tmp_path: Path, name: str = 'Variant B'):
    """Same revision, second variant+authority → second sealed evaluation."""
    (
        scene_repository,
        revision,
        variant_repository,
        coverage_repository,
        _aim,
        _prio,
        _eval_a,
        scenario_a,
    ) = fixture
    equipment_repository = coverage_repository.equipment_repository
    directivity_repository = coverage_repository.directivity_repository
    source_bytes, definition_b, dataset_b = _authority(
        definition_id='coverage-speaker-b',
        off_axis_500_db=-3.0,
        off_axis_1000_db=-6.0,
    )
    _persist_authority(
        equipment_repository,
        directivity_repository,
        definition_b,
        dataset_b,
        source_bytes,
    )
    variant_b = _variant(
        variant_repository, revision, definition_b, name=name
    )
    # Same population as eval A — a true per-seat A/B.
    scenario_b = _scenario(
        definition_b,
        dataset_b,
        seats=scenario_a.receiver_population.seat_entity_ids,
    )
    coverage_repository.save_scenario(scenario_b)
    evaluation_b = evaluate_coverage(
        revision=revision,
        variant=variant_b,
        equipment_definition=definition_b,
        directivity_dataset=dataset_b,
        scenario=scenario_b,
    )
    coverage_repository.save_evaluation(evaluation_b)
    return variant_b, evaluation_b


def test_ab_delta_per_seat_exact_positions(tmp_path: Path):
    seats = (
        _seat('seat-on', x_m=0.20, y_m=2.0),
        _seat('seat-off', x_m=2.20, y_m=2.0),
    )
    fixture = _workspace_repos(tmp_path, seats)
    _variant_b, eval_b = _second_variant_eval(fixture, tmp_path)
    eval_a = fixture[6]

    scene = _resolve(
        fixture,
        evaluation_id=eval_b.evaluation_id,
        baseline_evaluation_id=eval_a.evaluation_id,
    )
    assert scene.delta_active is True
    by_id = {marker.seat_entity_id: marker for marker in scene.markers}
    eval_a_by_id = {
        seat.seat_entity_id: seat for seat in eval_a.seat_results
    }
    eval_b_by_id = {
        seat.seat_entity_id: seat for seat in eval_b.seat_results
    }
    # Every Δ is per-seat verbatim arithmetic on the sealed aggregates —
    # identical ear positions only.
    expected_improved = 0
    for seat_id, marker in by_id.items():
        cur = eval_b_by_id[seat_id].aggregated_relative_directivity_level
        base = eval_a_by_id[seat_id].aggregated_relative_directivity_level
        expected = cur.value - base.value
        assert marker.delta_label == f'd{expected:+.1f}'
        if expected > 1e-9:
            expected_improved += 1
        else:
            assert abs(expected) < 1e-9
    assert scene.delta_counts['improved'] == expected_improved
    assert (
        scene.delta_counts['improved'] + scene.delta_counts['same'] == 2
    )
    assert scene.delta_semantics is not None


def test_ab_blocked_across_revisions(tmp_path: Path):
    seats = (_seat('seat-on', x_m=0.20, y_m=2.0),)
    fixture = _workspace_repos(tmp_path, seats)
    scene_repository, revision = fixture[0], fixture[1]
    eval_a = fixture[6]

    # A NEW head (seat moved) → eval A is now pinned to the old revision.
    _new_head(
        scene_repository,
        (_seat('seat-on', x_m=0.40, y_m=2.2),),
        revision.revision_id,
    )
    scene = _resolve(
        fixture,
        evaluation_id=eval_a.evaluation_id,
        baseline_evaluation_id=eval_a.evaluation_id,
    )
    assert scene.state == 'historical'
    assert scene.delta_active is False


def test_ab_frequency_mode_uses_matching_grid_slot(tmp_path: Path):
    seats = (_seat('seat-off', x_m=2.20, y_m=2.0),)
    fixture = _workspace_repos(tmp_path, seats)
    _variant_b, eval_b = _second_variant_eval(fixture, tmp_path)
    eval_a = fixture[6]

    scene = _resolve(
        fixture,
        evaluation_id=eval_b.evaluation_id,
        baseline_evaluation_id=eval_a.evaluation_id,
        frequency_hz=1000.0,
    )
    assert scene.delta_active is True
    marker = scene.markers[0]
    eval_b_freq = next(
        seat
        for seat in eval_b.seat_results
        if seat.seat_entity_id == 'seat-off'
    ).frequency_results
    eval_a_freq = next(
        seat
        for seat in eval_a.seat_results
        if seat.seat_entity_id == 'seat-off'
    ).frequency_results
    index_b = next(
        i
        for i, item in enumerate(eval_b_freq)
        if item.requested_frequency_hz == 1000.0
    )
    index_a = next(
        i
        for i, item in enumerate(eval_a_freq)
        if item.requested_frequency_hz == 1000.0
    )
    assert marker.value == pytest.approx(
        eval_b_freq[index_b].relative_level_db
    )
    expected = (
        eval_b_freq[index_b].relative_level_db
        - eval_a_freq[index_a].relative_level_db
    )
    assert marker.delta_label == f'd{expected:+.1f}'


# ---------------------------------------------------------------------------
# Seat-priority membership in the legend (never an all-seat claim)
# ---------------------------------------------------------------------------


def test_priority_roles_from_profile_bound_to_eval_revision(
    tmp_path: Path,
):
    seats = (
        _seat('seat-a', x_m=0.20, y_m=2.0),
        _seat('seat-b', x_m=1.40, y_m=2.4),
        _seat('seat-c', x_m=-1.40, y_m=2.4),
    )
    fixture = _workspace_repos(tmp_path, seats)
    scene_repository, revision = fixture[0], fixture[1]
    priority_repository = fixture[5]
    profile = build_seat_priority_profile(
        scene_repository=scene_repository,
        document_id=DOCUMENT_ID,
        members=(
            SeatPriorityMember(
                seat_entity_id='seat-a',
                seat_role='primary',
                required=True,
            ),
            SeatPriorityMember(
                seat_entity_id='seat-b',
                seat_role='secondary',
                required=False,
            ),
            SeatPriorityMember(
                seat_entity_id='seat-c',
                seat_role='diagnostic',
                required=False,
            ),
        ),
    )
    priority_repository.save(profile)

    scene = _resolve(fixture)
    roles = {m.seat_entity_id: m.priority_role for m in scene.markers}
    assert roles['seat-a'] == 'required'
    assert roles['seat-b'] == 'weighted'
    assert roles['seat-c'] == 'diagnostic'
    glyphs = {m.seat_entity_id: m.glyph for m in scene.markers}
    assert glyphs['seat-c'] == 'cube'
    legend_texts = [text for text, _c in scene.legend]
    assert 'REQ' in legend_texts
    assert 'DIAG' in legend_texts


# ---------------------------------------------------------------------------
# No interpolation / count discipline at scale
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('count', (10, 100))
def test_one_marker_per_seat_no_interpolation(tmp_path: Path, count: int):
    seats = _grid_seats(count)
    fixture = _workspace_repos(tmp_path, seats)
    scene = _resolve(fixture)
    # Every seat in the population got exactly one discrete marker —
    # no surface/volume fill between them, ever.
    assert len(scene.markers) == count
    positions = {
        (m.position.x_m, m.position.y_m, m.position.z_m)
        for m in scene.markers
    }
    eval_positions = {
        (
            seat.receiver_reference_position_m.x_m,
            seat.receiver_reference_position_m.y_m,
            seat.receiver_reference_position_m.z_m,
        )
        for seat in fixture[6].seat_results
        if seat.receiver_reference_position_m is not None
    }
    assert positions == eval_positions


# ---------------------------------------------------------------------------
# Controller caching / staleness
# ---------------------------------------------------------------------------


def test_controller_head_keyed_cache_and_clear(tmp_path: Path):
    seats = (_seat('seat-on', x_m=0.20, y_m=2.0),)
    fixture = _workspace_repos(tmp_path, seats)
    (
        scene_repository,
        _rev,
        variant_repository,
        coverage_repository,
        aim_repository,
        priority_repository,
        evaluation,
        _scenario_obj,
    ) = fixture
    controller = RoomSeatCoverageOverlayController(
        scene_repository,
        variant_repository,
        coverage_repository,
        DOCUMENT_ID,
        aim_repository=aim_repository,
        priority_repository=priority_repository,
    )
    assert controller.resolve() is None  # not armed → nothing
    controller.arm(
        CoverageOverlayRequest(evaluation_id=evaluation.evaluation_id)
    )
    first = controller.resolve()
    assert first is not None and first.state == 'current'
    # Deliberately uncached: a second resolve re-reads the stores.
    assert controller.resolve() is not first
    moved = (_seat('seat-on', x_m=0.40, y_m=2.2),)
    _new_head(scene_repository, moved, fixture[1].revision_id)
    second = controller.resolve()
    assert second.state == 'historical'
    controller.clear()
    assert controller.armed is False
    assert controller.resolve() is None


# ---------------------------------------------------------------------------
# Viewport actors: pickable=False, prefix sweep, teardown
# ---------------------------------------------------------------------------


def _viewport():
    from PySide6.QtWidgets import QApplication

    from htdt.room_viewport import RoomViewport3D

    QApplication.instance() or QApplication(['htdt-test'])
    return RoomViewport3D()


def _coverage_actor_names(viewport) -> list[str]:
    return [
        name
        for name in viewport.plotter.renderer.actors
        if isinstance(name, str) and name.startswith('coverage-overlay-')
    ]


def test_viewport_actors_pickable_false_and_cleanup(tmp_path: Path):
    seats = (
        _seat('seat-on', x_m=0.20, y_m=2.0),
        _seat('seat-off', x_m=2.20, y_m=2.0),
    )
    fixture = _workspace_repos(tmp_path, seats)
    scene = _resolve(fixture)

    viewport = _viewport()
    viewport.render_coverage_overlay(scene)
    names = _coverage_actor_names(viewport)
    # markers ×2, labels ×2, rays ×2, axis, source, status, legend
    assert 'coverage-overlay-marker-0' in names
    assert 'coverage-overlay-marker-1' in names
    assert 'coverage-overlay-ray-0' in names
    assert 'coverage-overlay-axis' in names
    assert 'coverage-overlay-source' in names
    assert 'coverage-overlay-status' in names
    assert 'coverage-overlay-legend' in names
    for name in names:
        actor = viewport.plotter.renderer.actors[name]
        assert not actor.GetPickable(), name

    viewport.clear_coverage_overlay()
    assert _coverage_actor_names(viewport) == []
    # render(None) is also a full clear (toggle-off path).
    viewport.render_coverage_overlay(scene)
    assert _coverage_actor_names(viewport)
    viewport.render_coverage_overlay(None)
    assert _coverage_actor_names(viewport) == []
    viewport.deleteLater()


def test_viewport_overlay_prefix_registered_for_sweep():
    from htdt.room_viewport import RoomViewport3D

    assert 'coverage-overlay-' in RoomViewport3D._OVERLAY_ACTOR_PREFIXES


def test_viewport_labels_are_ascii_only(tmp_path: Path):
    fixture = _workspace_repos(
        tmp_path, (_seat('seat-on', x_m=0.20, y_m=2.0),)
    )
    scene = _resolve(fixture)
    # Mesa/VTK cannot render .ttc CJK — every 3D string must be ASCII.
    for line in scene.viewport_lines:
        assert line.isascii(), line
    for text, _color in scene.legend:
        assert text.isascii(), text
    for marker in scene.markers:
        assert marker.label.isascii()


# ---------------------------------------------------------------------------
# Qt panel surface
# ---------------------------------------------------------------------------


def _panel():
    from PySide6.QtWidgets import QApplication

    from htdt.room_seat_coverage_panel import RoomSeatCoveragePanel

    QApplication.instance() or QApplication(['htdt-test'])
    return RoomSeatCoveragePanel()


def test_panel_binds_scene_rows_and_detail(tmp_path: Path):
    seats = (
        _seat('seat-on', x_m=0.20, y_m=2.0),
        _seat('seat-outside', x_m=-5.5, y_m=0.3),
        _seat_without_reference('seat-nopose', x_m=1.0, y_m=3.0),
    )
    fixture = _workspace_repos(tmp_path, seats)
    scene = _resolve(fixture, quantity='coverage_gate')

    panel = _panel()
    panel.show_scene(scene)
    assert panel.seat_table.rowCount() == 3
    panel.seat_table.selectRow(0)
    detail = panel.detail_label.text()
    assert 'seat-on' in detail
    assert 'Hz' in detail
    # Provenance ids appear in the detail block.
    assert 'eval' in detail or '証跡' in detail
    panel.deleteLater()


def test_panel_selection_emits_stable_seat_id(tmp_path: Path):
    seats = (
        _seat('seat-a', x_m=0.20, y_m=2.0),
        _seat('seat-b', x_m=1.40, y_m=2.4),
    )
    fixture = _workspace_repos(tmp_path, seats)
    scene = _resolve(fixture)
    panel = _panel()
    panel.show_scene(scene)
    picked: list = []
    panel.seatSelected.connect(lambda seat_id: picked.append(seat_id))
    row_b = panel._rows_by_id['seat-b']
    panel.seat_table.selectRow(row_b)
    assert picked[-1] == 'seat-b'
    # Scene → table sync: select_seat marks the same row without re-emit.
    panel.seat_table.clearSelection()  # emits None — honest clear
    picked.clear()
    panel.select_seat('seat-a')
    assert panel.selected_seat_id() == 'seat-a'
    assert picked == []
    panel.deleteLater()


def test_panel_empty_and_blocked_states(tmp_path: Path):
    panel = _panel()
    panel.show_scene(None)
    assert panel.seat_table.rowCount() == 0
    assert panel.status_label.text() == '—'
    fixture = _workspace_repos(
        tmp_path, (_seat('seat-on', x_m=0.20, y_m=2.0),)
    )
    blocked = _resolve(fixture, evaluation_id='missing-eval')
    panel.show_scene(blocked)
    assert 'ブロック' in panel.status_label.text()
    panel.deleteLater()


# ---------------------------------------------------------------------------
# Workspace wiring (FakeRoomViewport)
# ---------------------------------------------------------------------------


def _workspace(tmp_path: Path):
    from PySide6.QtWidgets import QApplication

    from htdt.room_workspace import RoomWorkspace
    from test_room_cadux import FakeRoomViewport

    app = QApplication.instance() or QApplication(['htdt-test'])
    repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    document = _document(
        (
            _seat('seat-on', x_m=0.20, y_m=2.0),
            _seat('seat-off', x_m=2.20, y_m=2.0),
        )
    )
    repository.save(document, parent_revision_id=None)

    class CoverageViewport(FakeRoomViewport):
        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self.coverage_calls: list = []

        def render_coverage_overlay(self, scene) -> None:
            self.coverage_calls.append(scene)

        def clear_coverage_overlay(self) -> None:
            self.coverage_calls.append('cleared')

    workspace = RoomWorkspace(
        repository,
        document.document_id,
        viewport_factory=lambda parent: CoverageViewport(parent),
    )
    return app, workspace, repository


def _seed_eval(workspace, repository):
    """Persist the sealed eval through the workspace's own repositories."""
    revision = repository.current_head(DOCUMENT_ID)
    equipment_repository = workspace.system_expansion.equipment_repository
    directivity_repository = CadDirectivityRepository(
        repository, equipment_repository
    )
    source_bytes, definition, dataset = _authority()
    _persist_authority(
        equipment_repository,
        directivity_repository,
        definition,
        dataset,
        source_bytes,
    )
    variant = _variant(
        workspace.system_expansion.variant_repository,
        revision,
        definition,
    )
    scenario = _scenario(definition, dataset)
    workspace.coverage_repository.save_scenario(scenario)
    evaluation = evaluate_coverage(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        directivity_dataset=dataset,
        scenario=scenario,
    )
    workspace.coverage_repository.save_evaluation(evaluation)
    return evaluation


def test_workspace_toggle_draws_and_clears(tmp_path: Path):
    _app, workspace, repository = _workspace(tmp_path)
    try:
        workspace.overlay_controls.acoustics.setChecked(True)
        workspace.set_context('placement')
        panel = workspace.seat_coverage_panel

        # No evaluations yet → honest empty state, not an error.
        panel.coverage_toggle.setChecked(True)
        last = workspace.viewport.coverage_calls[-1]
        assert last is not None and last.state == 'empty'
        assert panel.seat_table.rowCount() == 0

        _seed_eval(workspace, repository)
        workspace.refresh()
        last = workspace.viewport.coverage_calls[-1]
        assert last is not None and last.state == 'current'
        assert len(last.markers) == 2
        assert panel.seat_table.rowCount() == 2

        # Table ↔ scene sync via the stable seat id.
        row = panel._rows_by_id['seat-off']
        panel.seat_table.selectRow(row)
        assert workspace.controller.selected_id == 'seat-off'

        workspace.select_entity('seat-on')
        assert panel.selected_seat_id() == 'seat-on'

        panel.coverage_toggle.setChecked(False)
        assert workspace.viewport.coverage_calls[-1] == 'cleared'
    finally:
        workspace.close()
        workspace.deleteLater()


def test_workspace_scene_edit_lapses_to_historical(tmp_path: Path):
    _app, workspace, repository = _workspace(tmp_path)
    try:
        workspace.overlay_controls.acoustics.setChecked(True)
        workspace.set_context('placement')
        _seed_eval(workspace, repository)
        panel = workspace.seat_coverage_panel
        panel.coverage_toggle.setChecked(True)
        last = workspace.viewport.coverage_calls[-1]
        assert last.state == 'current'

        _new_head(
            repository,
            (_seat('seat-on', x_m=0.40, y_m=2.2),
             _seat('seat-off', x_m=2.20, y_m=2.0)),
            repository.current_head(DOCUMENT_ID).revision_id,
        )
        workspace.refresh()
        last = workspace.viewport.coverage_calls[-1]
        assert last is not None and last.state == 'historical'
    finally:
        workspace.close()
        workspace.deleteLater()
