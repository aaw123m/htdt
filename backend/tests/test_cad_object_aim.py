import pytest

from htdt.cad_objects import TheaterObjectError, speaker_aim_replacements
from htdt.cad_scene import Offset3, Position3, SceneEntity, Size3, make_f1_scene
from htdt.theater_document import TheaterWorkingDocument


def make_scene_with_seat():
    base = make_f1_scene()
    seat = SceneEntity(
        entity_id='seat-main',
        kind='seat',
        name='Main seat',
        position=Position3(x_m=3.0, y_m=3.0, z_m=0.45),
        size_m=Size3(x_m=0.70, y_m=0.80, z_m=0.90),
        acoustic_reference_offset_m=Offset3(z_m=0.65),
    )
    return base.model_copy(update={'entities': base.entities + (seat,)})


def test_explicit_aim_changes_only_speaker_aim_and_targets_seat_reference() -> None:
    document = make_scene_with_seat()
    before = document.entity('speaker-fl')
    assert before.aim_xyz is None

    replacements = speaker_aim_replacements(document, ('speaker-fl',), 'seat-main')
    assert len(replacements) == 1
    after = replacements[0]

    assert after.entity_id == before.entity_id
    assert after.position == before.position
    assert after.orientation == before.orientation
    assert after.size_m == before.size_m
    assert after.speaker_role == before.speaker_role
    assert after.aim_xyz is not None

    source = before.position
    target = Position3(x_m=3.0, y_m=3.0, z_m=1.10)
    vector = (
        target.x_m - source.x_m,
        target.y_m - source.y_m,
        target.z_m - source.z_m,
    )
    length = sum(value * value for value in vector) ** 0.5
    assert after.aim_xyz.x == pytest.approx(vector[0] / length)
    assert after.aim_xyz.y == pytest.approx(vector[1] / length)
    assert after.aim_xyz.z == pytest.approx(vector[2] / length)


def test_multi_speaker_aim_is_one_undo_unit() -> None:
    document = make_scene_with_seat()
    working = TheaterWorkingDocument(document)
    before_fl = working.committed_document.entity('speaker-fl')
    before_c = working.committed_document.entity('speaker-c')

    replacements = speaker_aim_replacements(
        working.committed_document,
        ('speaker-fl', 'speaker-c'),
        'seat-main',
    )
    assert working.replace_entities(replacements)
    assert working.history_length == 1
    assert working.committed_document.entity('speaker-fl').aim_xyz is not None
    assert working.committed_document.entity('speaker-c').aim_xyz is not None

    assert working.undo()
    assert working.committed_document.entity('speaker-fl') == before_fl
    assert working.committed_document.entity('speaker-c') == before_c
    assert working.redo()
    assert working.committed_document.entity('speaker-fl').aim_xyz is not None
    assert working.committed_document.entity('speaker-c').aim_xyz is not None


def test_aim_rejects_non_reference_target() -> None:
    document = make_scene_with_seat()
    with pytest.raises(TheaterObjectError, match='seat or measurement point'):
        speaker_aim_replacements(document, ('speaker-fl',), 'furniture-left')
