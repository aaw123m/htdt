"""Scene-layout-derived standards observations (REV26).

The built-in standards criteria previously had no production observation
source: every evaluate() ran with ``observations=()`` and reported
UNKNOWN/missing_observation for all criteria. The layout lane derives the
geometry-computable quantities — listener boundary distance, per-role
azimuth, adjacent surround/upper angles — from the exact evaluation
target and retains them as predicted-basis observation authorities.
"""

from __future__ import annotations

import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    quaternion_from_euler_deg,
)
from htdt.cad_standards import (
    CriterionDefinition,
    CriterionRule,
    CriterionSource,
    build_user_standards_profile,
)
from htdt.cad_standards_evidence import STANDARDS_MANUAL_OBSERVATION_KIND
from htdt.cad_standards_layout_observation import (
    SCENE_LAYOUT_DERIVATION_METHOD,
)
from htdt.cad_standards_profiles import (
    auro3d_home_v12_profile,
    dolby_atmos_home_5_1_2_profile,
    rp22_spatial_profile,
)
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    ProposedEntitySpec,
    build_system_variant,
)
from htdt.standards_workspace import StandardsWorkspaceModel


def _seat(
    entity_id: str,
    *,
    x: float,
    y: float,
    yaw_deg: float = 180.0,
    pitch_deg: float = 0.0,
    roll_deg: float = 0.0,
) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind="seat",
        name=entity_id,
        position=Position3(x_m=x, y_m=y, z_m=0.55),
        orientation=quaternion_from_euler_deg(
            yaw_deg=yaw_deg, pitch_deg=pitch_deg, roll_deg=roll_deg
        ),
        size_m=Size3(x_m=0.6, y_m=0.6, z_m=1.1),
        acoustic_reference_offset_m=Offset3(z_m=0.65),
    )


def _speaker(
    entity_id: str,
    role: str,
    *,
    x: float,
    y: float,
    z: float = 1.0,
) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind="speaker",
        name=entity_id,
        speaker_role=role,
        position=Position3(x_m=x, y_m=y, z_m=z),
        size_m=Size3(x_m=0.3, y_m=0.3, z_m=0.4),
    )


def _speaker_at_azimuth(
    entity_id: str,
    role: str,
    *,
    ear_x: float,
    ear_y: float,
    azimuth_deg: float,
    distance: float = 1.5,
    z: float = 1.0,
) -> SceneEntity:
    """Speaker placed at a signed azimuth from a forward-facing (-Y) seat."""

    azimuth = math.radians(azimuth_deg)
    return _speaker(
        entity_id,
        role,
        x=ear_x + distance * math.sin(azimuth),
        y=ear_y - distance * math.cos(azimuth),
        z=z,
    )


def _scene(entities: tuple[SceneEntity, ...]) -> SceneDocument:
    return SceneDocument(
        document_id="doc-standards-layout",
        room=RoomPrism(width_m=6.0, depth_m=5.0, height_m=2.4),
        entities=entities,
    )


def _workspace(
    tmp_path, document: SceneDocument, name: str = "scenes.sqlite3"
) -> StandardsWorkspaceModel:
    repository = SceneRepository(tmp_path / name)
    repository.save(document, parent_revision_id=None)
    return StandardsWorkspaceModel(repository, document.document_id)


def _result(evaluation, criterion_id: str):
    for result in evaluation.results:
        if result.criterion_id == criterion_id:
            return result
    raise AssertionError(criterion_id)


def _dolby_scene() -> SceneDocument:
    # Seat ear at (3.0, 3.6, 1.2) facing -Y.
    return _scene(
        (
            _seat("seat-a", x=3.0, y=3.6),
            _speaker_at_azimuth(
                "speaker-fl", "FL", ear_x=3.0, ear_y=3.6, azimuth_deg=-25.0,
                distance=2.0,
            ),
            _speaker("speaker-c", "C", x=3.0, y=0.8),
            _speaker_at_azimuth(
                "speaker-fr", "FR", ear_x=3.0, ear_y=3.6, azimuth_deg=25.0,
                distance=2.0,
            ),
            _speaker_at_azimuth(
                "speaker-sl", "SL", ear_x=3.0, ear_y=3.6, azimuth_deg=-100.0,
            ),
            _speaker_at_azimuth(
                "speaker-sr", "SR", ear_x=3.0, ear_y=3.6, azimuth_deg=100.0,
            ),
            _speaker_at_azimuth(
                "speaker-sbl", "SBL", ear_x=3.0, ear_y=3.6, azimuth_deg=-150.0,
            ),
            _speaker_at_azimuth(
                "speaker-sbr", "SBR", ear_x=3.0, ear_y=3.6, azimuth_deg=150.0,
            ),
        )
    )


def test_rp22_listener_boundary_distance_from_exact_layout(tmp_path) -> None:
    # Seat ear at (3.0, 3.6): distances to walls are 3.0/3.0/3.6 front and
    # 1.4 rear — the binding value is the minimum, 1.4 m.
    workspace = _workspace(tmp_path, _dolby_scene())

    level3 = workspace.evaluate(rp22_spatial_profile(3), variant_id=None)
    result = _result(level3, "rp22.p01.listener-boundary-distance")
    assert result.status == "PASS"
    assert result.observed_value == pytest.approx(1.4)
    assert result.evidence_basis == "predicted"
    assert result.entity_ids == ("seat-a",)
    assert all(
        ref.kind == STANDARDS_MANUAL_OBSERVATION_KIND
        for ref in result.evidence_refs
    )

    # Level 4 requires strictly more than 1.5 m — the same scene fails.
    level4 = workspace.evaluate(rp22_spatial_profile(4), variant_id=None)
    result = _result(level4, "rp22.p01.listener-boundary-distance")
    assert result.status == "FAIL"
    assert result.observed_value == pytest.approx(1.4)


def test_dolby_role_azimuths_evaluate_pass_and_fail(tmp_path) -> None:
    workspace = _workspace(tmp_path, _dolby_scene())

    evaluation = workspace.evaluate(
        dolby_atmos_home_5_1_2_profile(), variant_id=None
    )
    statuses = {
        result.criterion_id: (result.status, result.observed_value)
        for result in evaluation.results
    }
    assert statuses["dolby.5.1.2.front-left-azimuth"] == (
        "PASS",
        pytest.approx(-25.0),
    )
    assert statuses["dolby.5.1.2.front-right-azimuth"] == (
        "PASS",
        pytest.approx(25.0),
    )
    assert statuses["dolby.5.1.2.surround-left-azimuth"] == (
        "PASS",
        pytest.approx(-100.0),
    )
    assert statuses["dolby.5.1.2.surround-right-azimuth"] == (
        "PASS",
        pytest.approx(100.0),
    )


def test_dolby_out_of_range_and_missing_role(tmp_path) -> None:
    document = _scene(
        (
            _seat("seat-a", x=3.0, y=3.6),
            _speaker_at_azimuth(
                "speaker-fl", "FL", ear_x=3.0, ear_y=3.6, azimuth_deg=-45.0,
            ),
            _speaker_at_azimuth(
                "speaker-fr", "FR", ear_x=3.0, ear_y=3.6, azimuth_deg=30.0,
            ),
        )
    )
    workspace = _workspace(tmp_path, document)

    evaluation = workspace.evaluate(
        dolby_atmos_home_5_1_2_profile(), variant_id=None
    )
    left = _result(evaluation, "dolby.5.1.2.front-left-azimuth")
    assert left.status == "FAIL"
    assert left.observed_value == pytest.approx(-45.0)
    # No surround speakers exist in the scene — honest UNKNOWN, never a
    # fabricated angle.
    for criterion_id in (
        "dolby.5.1.2.surround-left-azimuth",
        "dolby.5.1.2.surround-right-azimuth",
    ):
        result = _result(evaluation, criterion_id)
        assert result.status == "UNKNOWN"
        assert result.reason_code == "missing_observation"


def test_rp22_adjacent_surround_angle_from_layout(tmp_path) -> None:
    workspace = _workspace(tmp_path, _dolby_scene())

    # Cyclic separations for surrounds at -150/-100/+100/+150 are
    # 50/160/50/60 — the binding pair spans 160°.
    level2 = workspace.evaluate(rp22_spatial_profile(2), variant_id=None)
    result = _result(
        level2, "rp22.p05.max-adjacent-surround-horizontal-angle"
    )
    assert result.status == "FAIL"
    assert result.observed_value == pytest.approx(160.0)
    assert "speaker-sl" in result.entity_ids and "speaker-sr" in result.entity_ids

    # Fewer than two surround speakers can never produce the quantity.
    sparse = _workspace(
        tmp_path,
        _scene(
            (
                _seat("seat-a", x=3.0, y=3.6),
                _speaker_at_azimuth(
                    "speaker-sl", "SL",
                    ear_x=3.0, ear_y=3.6, azimuth_deg=-110.0,
                ),
            )
        ),
        "sparse.sqlite3",
    )
    result = _result(
        sparse.evaluate(rp22_spatial_profile(2), variant_id=None),
        "rp22.p05.max-adjacent-surround-horizontal-angle",
    )
    assert result.status == "UNKNOWN"
    assert result.reason_code == "missing_observation"


def test_rp22_upper_vertical_angle_uses_same_side_rows(tmp_path) -> None:
    # Left row: TFL elev ~33.7°, TML ~18.4° → adjacent diff ~15.3°.
    # Right row: TFR ~55.7°, TMR 0.0° → adjacent diff ~55.7° binds.
    document = _scene(
        (
            _seat("seat-a", x=3.0, y=3.6),
            _speaker_at_azimuth(
                "speaker-tfl", "TFL",
                ear_x=3.0, ear_y=3.6, azimuth_deg=-45.0, z=2.2,
            ),
            _speaker_at_azimuth(
                "speaker-tml", "TML",
                ear_x=3.0, ear_y=3.6, azimuth_deg=-90.0, z=1.7,
            ),
            _speaker_at_azimuth(
                "speaker-tfr", "TFR",
                ear_x=3.0, ear_y=3.6, azimuth_deg=45.0, z=3.4,
            ),
            _speaker_at_azimuth(
                "speaker-tmr", "TMR",
                ear_x=3.0, ear_y=3.6, azimuth_deg=90.0, z=1.2,
            ),
        )
    )
    workspace = _workspace(tmp_path, document)

    level4 = workspace.evaluate(rp22_spatial_profile(4), variant_id=None)
    result = _result(
        level4, "rp22.p09.max-adjacent-upper-vertical-angle"
    )
    # The binding pair is the right row's 55.7° jump — Level 4 allows 50°.
    assert result.status == "FAIL"
    assert result.observed_value == pytest.approx(55.7, abs=0.1)
    assert "speaker-tfr" in result.entity_ids

    level2 = workspace.evaluate(rp22_spatial_profile(2), variant_id=None)
    result = _result(
        level2, "rp22.p09.max-adjacent-upper-vertical-angle"
    )
    assert result.status == "PASS"


def test_underived_criteria_report_unknown_not_fabricated(tmp_path) -> None:
    workspace = _workspace(tmp_path, _dolby_scene())

    level4 = workspace.evaluate(rp22_spatial_profile(4), variant_id=None)
    for criterion_id in (
        "rp22.p03.screen-speakers-outside-zone-count",
        "rp22.p07.wide-horizontal-median-deviation",
        "rp22.p08.upfiring-elevation-speakers-prohibited",
        "rp22.p11.surround-wide-upper-outside-zone-count",
    ):
        result = _result(level4, criterion_id)
        assert result.status == "UNKNOWN"
        assert result.reason_code == "missing_observation"

    # AURO layer membership is not derivable from HTDT role ids — the whole
    # profile stays UNKNOWN rather than classifying on an invented family.
    auro = workspace.evaluate(auro3d_home_v12_profile(), variant_id=None)
    assert {result.status for result in auro.results} == {"UNKNOWN"}
    assert all(
        result.reason_code == "missing_observation" for result in auro.results
    )


def test_multi_seat_binding_reports_worst_seat(tmp_path) -> None:
    # seat-b sits 0.6 m off the left wall — it binds the observation.
    document = _scene(
        (
            _seat("seat-a", x=3.0, y=3.6),
            _seat("seat-b", x=0.6, y=3.6),
        )
    )
    workspace = _workspace(tmp_path, document)

    level1 = workspace.evaluate(rp22_spatial_profile(1), variant_id=None)
    result = _result(level1, "rp22.p01.listener-boundary-distance")
    assert result.status == "PASS"
    assert result.observed_value == pytest.approx(0.6)
    assert result.entity_ids == ("seat-b",)

    level3 = workspace.evaluate(rp22_spatial_profile(3), variant_id=None)
    result = _result(level3, "rp22.p01.listener-boundary-distance")
    assert result.status == "FAIL"
    assert result.observed_value == pytest.approx(0.6)


def test_repeat_evaluate_reuses_authority_and_evaluation(tmp_path) -> None:
    workspace = _workspace(tmp_path, _dolby_scene())
    profile = rp22_spatial_profile(4)

    first = workspace.evaluate(profile, variant_id=None)
    authority_count = len(
        workspace.repository.list_observation_authorities()
    )
    assert authority_count > 0
    assert {
        authority.method
        for authority in workspace.repository.list_observation_authorities()
    } == {SCENE_LAYOUT_DERIVATION_METHOD}

    second = workspace.evaluate(profile, variant_id=None)
    assert second.evaluation_id == first.evaluation_id
    assert second.evaluation_sha256 == first.evaluation_sha256
    assert (
        len(workspace.repository.list_observation_authorities())
        == authority_count
    )


def test_variant_target_derives_from_materialized_proposal(tmp_path) -> None:
    # A SystemVariant that moves the seat is evaluated against the
    # materialized proposal, never the baseline scene.
    document = _dolby_scene()
    workspace = _workspace(tmp_path, document)
    profile = rp22_spatial_profile(4)

    baseline_eval = workspace.evaluate(profile, variant_id=None)
    assert _result(
        baseline_eval, "rp22.p01.listener-boundary-distance"
    ).observed_value == pytest.approx(1.4)

    revision = workspace.scene_repository.current_head(document.document_id)
    moved_seat = _seat("seat-a", x=3.0, y=2.2)
    variant = build_system_variant(
        baseline=revision,
        name="seat forward",
        role_bindings=(
            ChannelRoleBinding(role_id="FL", display_name="Front Left"),
        ),
        proposed_entities=(
            ProposedEntitySpec(spec_id="move-seat-a", entity=moved_seat),
        ),
        created_at_utc="2026-10-01T00:00:00+00:00",
    )
    workspace.variant_repository.save_variant(variant)

    variant_eval = workspace.evaluate(profile, variant_id=variant.variant_id)
    result = _result(variant_eval, "rp22.p01.listener-boundary-distance")
    assert result.status == "PASS"
    assert result.observed_value == pytest.approx(2.2)

    # The baseline target still derives its own value afterwards.
    again = workspace.evaluate(profile, variant_id=None)
    assert _result(
        again, "rp22.p01.listener-boundary-distance"
    ).observed_value == pytest.approx(1.4)


def test_scene_revision_change_derives_fresh_geometry(tmp_path) -> None:
    # Evaluate against a changed scene re-derives observations bound to the
    # exact new revision — never borrowing the stale revision's values.
    document = _dolby_scene()
    workspace = _workspace(tmp_path, document)
    profile = rp22_spatial_profile(4)

    first = workspace.evaluate(profile, variant_id=None)
    assert _result(
        first, "rp22.p01.listener-boundary-distance"
    ).status == "FAIL"

    # Move the seat forward so the boundary distance passes Level 4.
    repository = workspace.scene_repository
    head = repository.current_head(document.document_id)
    moved = SceneDocument(
        document_id=document.document_id,
        room=document.room,
        entities=(
            _seat("seat-a", x=3.0, y=2.2),
            *tuple(
                entity
                for entity in document.entities
                if entity.entity_id != "seat-a"
            ),
        ),
    )
    repository.save(moved, parent_revision_id=head.revision_id)

    second = workspace.evaluate(profile, variant_id=None)
    assert second.evaluation_id != first.evaluation_id
    result = _result(second, "rp22.p01.listener-boundary-distance")
    # New binding: seat ear at y=2.2 → front 2.2, rear 2.8, sides 3.0.
    assert result.status == "PASS"
    assert result.observed_value == pytest.approx(2.2)


def test_listener_outside_room_reports_unknown(tmp_path) -> None:
    # A seat placed outside the room footprint has no listener-to-boundary
    # distance: measuring to the wall from outside would fabricate the
    # clearance the criterion attests.
    document = _scene((_seat("seat-out", x=-2.0, y=2.5),))
    workspace = _workspace(tmp_path, document)

    evaluation = workspace.evaluate(rp22_spatial_profile(1), variant_id=None)
    result = _result(evaluation, "rp22.p01.listener-boundary-distance")
    assert result.status == "UNKNOWN"
    assert result.reason_code == "missing_observation"
    assert result.observed_value is None


def test_degenerate_listener_forward_derives_no_azimuth(tmp_path) -> None:
    # A seat rolled a full 90 deg faces straight up: its forward vector has
    # no horizontal component, so quaternion round-off must not mint an
    # arbitrary azimuth.
    document = _scene(
        (
            _seat("seat-tilt", x=3.0, y=3.6, roll_deg=90.0),
            _speaker("speaker-fl", "FL", x=2.4, y=1.7),
            _speaker("speaker-fr", "FR", x=3.6, y=1.7),
        )
    )
    workspace = _workspace(tmp_path, document)

    evaluation = workspace.evaluate(
        dolby_atmos_home_5_1_2_profile(), variant_id=None
    )
    for criterion_id in (
        "dolby.5.1.2.front-left-azimuth",
        "dolby.5.1.2.front-right-azimuth",
    ):
        result = _result(evaluation, criterion_id)
        assert result.status == "UNKNOWN"
        assert result.reason_code == "missing_observation"
        assert result.observed_value is None


def test_colocated_speaker_derives_no_azimuth(tmp_path) -> None:
    # The FL speaker sits exactly on the seat ear reference: atan2 of a
    # (near-)zero delta would mint a fabricated angle, so the criterion
    # stays UNKNOWN instead.
    document = _scene(
        (
            _seat("seat-a", x=3.0, y=3.6),
            _speaker("speaker-fl", "FL", x=3.0, y=3.6, z=1.2),
        )
    )
    workspace = _workspace(tmp_path, document)

    evaluation = workspace.evaluate(
        dolby_atmos_home_5_1_2_profile(), variant_id=None
    )
    result = _result(evaluation, "dolby.5.1.2.front-left-azimuth")
    assert result.status == "UNKNOWN"
    assert result.reason_code == "missing_observation"
    assert result.observed_value is None


def test_criterion_id_reuse_with_foreign_quantity_not_served(tmp_path) -> None:
    # A user profile reusing a derived criterion id but declaring a quantity
    # the lane does not measure gets no observation authority: minting one
    # would record a measurement that never happened.
    document = _scene(
        (
            _seat("seat-a", x=3.0, y=3.6),
            _speaker("speaker-fl", "FL", x=2.4, y=1.7),
        )
    )
    workspace = _workspace(tmp_path, document)
    profile = build_user_standards_profile(
        profile_id="custom-poison",
        version="1",
        name="foreign quantity",
        criteria=(
            CriterionDefinition(
                criterion_id="dolby.5.1.2.front-left-azimuth",
                name="loudness",
                source=CriterionSource(
                    publisher="acme",
                    document_title="doc",
                    document_version="1",
                    reference="x",
                ),
                quantity="loudness",
                unit="deg",
                rule=CriterionRule(
                    operator="range", minimum=-30.0, maximum=-20.0
                ),
                applicable_domains=("room",),
            ),
        ),
    )
    workspace.repository.save_profile(profile)

    evaluation = workspace.evaluate(profile, variant_id=None)
    (result,) = evaluation.results
    assert result.status == "UNKNOWN"
    assert result.reason_code == "missing_observation"
    assert result.observed_value is None
    assert not workspace.repository.list_observation_authorities()
