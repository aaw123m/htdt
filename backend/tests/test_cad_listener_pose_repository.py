from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from htdt.cad_listener_pose import (
    CadListenerPoseRepository,
    build_listener_pose,
)
from htdt.cad_scene import Offset3


def _pose(seat: str, label: str, head_z: float = 1.15):
    return build_listener_pose(
        seat_entity_id=seat,
        label=label,
        head_center_offset_local_m=Offset3(z_m=head_z),
        eye_reference_offset_local_m=Offset3(z_m=head_z - 0.05),
        acoustic_reference_offset_local_m=Offset3(z_m=1.0),
        provenance='test',
    )


def test_list_poses_for_seats_groups_and_matches_single_seat_reads(
    tmp_path: Path,
) -> None:
    repository = CadListenerPoseRepository(tmp_path / 'cad.sqlite3')
    front_a = _pose('seat-front', 'upright')
    front_b = _pose('seat-front', 'reclined', head_z=0.95)
    back_a = _pose('seat-back', 'upright')
    for pose in (front_a, front_b, back_a):
        repository.save_pose(pose)

    grouped = repository.list_poses_for_seats(
        ('seat-front', 'seat-back', 'seat-missing')
    )
    assert grouped['seat-front'] == repository.list_poses_for_seat('seat-front')
    assert grouped['seat-back'] == (back_a,)
    assert 'seat-missing' not in grouped
    assert repository.list_poses_for_seats(()) == {}


def test_selected_poses_for_document_matches_per_seat_reads(
    tmp_path: Path,
) -> None:
    repository = CadListenerPoseRepository(tmp_path / 'cad.sqlite3')
    front = _pose('seat-front', 'upright')
    back = _pose('seat-back', 'reclined', head_z=0.95)
    repository.save_pose(front)
    repository.save_pose(back)
    repository.select_pose('doc-1', front)
    repository.select_pose('doc-1', back)

    selected = repository.selected_poses_for_document('doc-1')
    assert selected == {'seat-front': front, 'seat-back': back}
    assert repository.selected_pose('doc-1', 'seat-front') == front
    assert repository.selected_poses_for_document('doc-2') == {}


def test_selected_poses_for_document_fails_closed_on_hash_mismatch(
    tmp_path: Path,
) -> None:
    repository = CadListenerPoseRepository(tmp_path / 'cad.sqlite3')
    pose = _pose('seat-front', 'upright')
    repository.save_pose(pose)
    repository.select_pose('doc-1', pose)

    # Swap the persisted payload for a valid different pose under the same
    # id: both the per-seat and batch reads must refuse the stale selection.
    tampered = build_listener_pose(
        seat_entity_id='seat-front',
        label='tampered',
        head_center_offset_local_m=Offset3(z_m=0.9),
        eye_reference_offset_local_m=Offset3(z_m=0.85),
        acoustic_reference_offset_local_m=Offset3(z_m=1.0),
        provenance='test',
        pose_id=pose.pose_id,
    )
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            'UPDATE cad_listener_poses SET payload_json=? WHERE pose_id=?',
            (tampered.model_dump_json(), pose.pose_id),
        )

    with pytest.raises(ValueError, match='hash mismatch'):
        repository.selected_pose('doc-1', 'seat-front')
    with pytest.raises(ValueError, match='hash mismatch'):
        repository.selected_poses_for_document('doc-1')
    # The non-raising listing still skips unverifiable selections.
    assert repository.selections_for_document('doc-1') == {}
