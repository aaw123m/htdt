"""#862: seat→measurement-point lineage persists only when the claimed
derivation actually resolves against the pinned revisions.

A self-consistent lineage hash is never treated as proof of the seat→point
derivation. ``save_target_lineage`` verifies the created revision (exists,
same document, pinned content hash), the source revision (the created
revision's parent, explicitly pinned via ``source_scene_revision_id`` when
present), the source seat (kind ``seat`` + acoustic listener reference at
the pinned source revision), the resolved listener position equalling the
declared ``initial_position``, and the created point (exists, kind
``measurement_point``, position equal to ``initial_position``). Reads
revalidate the same derivation — corrupt rows fail closed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_measurement_quality_repository import (
    CadMeasurementQualityRepository,
)
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurement_targets import (
    build_measurement_target_lineage,
    measurement_target_drift,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Offset3,
    Position3,
    SceneDocument,
    SceneEntity,
    Size3,
    acoustic_reference_position,
    make_f1_scene,
)
from htdt.measurement_workflow import MeasurementWorkflowController


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(make_f1_scene(), parent_revision_id=None).revision
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(measurement_repository)
    return revision, measurement_repository, quality_repository


def _seat(name: str = 'seat-1', *, offset: Offset3 | None = None):
    return SceneEntity(
        entity_id=name,
        kind='seat',
        name=name,
        position=Position3(x_m=3.0, y_m=2.6, z_m=0.5),
        size_m=Size3(x_m=2.0, y_m=0.9, z_m=1.0),
        acoustic_reference_offset_m=offset,
    )


def _with_seat(document, seat=None):
    seat = _seat(offset=Offset3(x_m=0.0, y_m=0.0, z_m=0.6)) if seat is None else seat
    return document.model_copy(
        update={'entities': (*document.entities, seat)}
    )


def _controller(scene_repository, measurement_repository, quality_repository):
    return MeasurementWorkflowController(
        scene_repository,
        'fixture-f1',
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
        rew_client=object(),
    )


def _derive(tmp_path: Path):
    """Real derivation through the workflow, returning all persisted handles."""
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    scene_repository = measurement_repository.scene_repository
    source = scene_repository.save(
        _with_seat(revision.document), parent_revision_id=revision.revision_id
    ).revision
    controller = _controller(
        scene_repository, measurement_repository, quality_repository
    )
    lineage = controller.derive_measurement_point_from_seat(
        'seat-1', measurement_point_id='point-sofa'
    )
    created = scene_repository.get(lineage.creation_revision_id)
    return scene_repository, quality_repository, source, created, lineage


def test_valid_lineage_saves_and_reopens(tmp_path: Path) -> None:
    _, quality_repository, source, created, lineage = _derive(tmp_path)
    assert lineage.source_scene_revision_id == source.revision_id
    assert lineage.source_scene_content_hash == source.content_hash
    assert lineage.creation_scene_content_hash == created.content_hash
    assert quality_repository.get_target_lineage('point-sofa') == lineage
    assert quality_repository.list_target_lineages('fixture-f1') == (lineage,)


def test_legacy_lineage_without_revision_pins_still_validates(
    tmp_path: Path,
) -> None:
    revision, measurement_repository, quality_repository = _repositories(
        tmp_path
    )
    scene_repository = measurement_repository.scene_repository
    source = scene_repository.save(
        _with_seat(revision.document), parent_revision_id=revision.revision_id
    ).revision
    seat = source.document.entity('seat-1')
    seat_reference = acoustic_reference_position(seat)
    created = scene_repository.save(
        source.document.model_copy(
            update={
                'entities': (
                    *source.document.entities,
                    SceneEntity(
                        entity_id='point-legacy',
                        kind='measurement_point',
                        name='Legacy',
                        position=seat_reference,
                    ),
                )
            }
        ),
        parent_revision_id=source.revision_id,
    ).revision
    # A lineage recorded before the explicit pin fields existed resolves the
    # source revision from the created revision's parent and still validates.
    legacy = build_measurement_target_lineage(
        document_id='fixture-f1',
        measurement_point_id='point-legacy',
        source_seat_id='seat-1',
        creation_revision_id=created.revision_id,
        initial_position=seat_reference,
    )
    assert legacy.source_scene_revision_id is None
    quality_repository.save_target_lineage(legacy)
    assert quality_repository.get_target_lineage('point-legacy') == legacy


def test_creation_revision_from_another_document_rejects(tmp_path: Path) -> None:
    scene_repository, quality_repository, _, created, lineage = _derive(tmp_path)
    other_doc = SceneDocument(
        document_id='doc-other',
        room=created.document.room,
        entities=(),
    )
    other = scene_repository.save(other_doc, parent_revision_id=None).revision
    forged = build_measurement_target_lineage(
        document_id='fixture-f1',
        measurement_point_id='point-sofa-2',
        source_seat_id='seat-1',
        creation_revision_id=other.revision_id,
        initial_position=lineage.initial_position,
    )
    with pytest.raises(ValueError, match='different document'):
        quality_repository.save_target_lineage(forged)


def test_seat_from_another_document_rejects(tmp_path: Path) -> None:
    scene_repository, quality_repository, _, created, lineage = _derive(tmp_path)
    # The seat only exists in a second document — the source revision of this
    # document's chain does not contain it.
    forged = build_measurement_target_lineage(
        document_id='fixture-f1',
        measurement_point_id='point-sofa-2',
        source_seat_id='seat-2',
        creation_revision_id=created.revision_id,
        initial_position=lineage.initial_position,
    )
    with pytest.raises(ValueError, match='unknown source seat'):
        quality_repository.save_target_lineage(forged)


def test_source_entity_that_is_not_a_seat_rejects(tmp_path: Path) -> None:
    scene_repository, quality_repository, source, created, lineage = _derive(
        tmp_path
    )
    forged = build_measurement_target_lineage(
        document_id='fixture-f1',
        measurement_point_id='point-sofa-2',
        source_seat_id='point-mlp',
        creation_revision_id=created.revision_id,
        source_scene_revision_id=source.revision_id,
        source_scene_content_hash=source.content_hash,
        initial_position=lineage.initial_position,
    )
    with pytest.raises(ValueError, match='not a seat'):
        quality_repository.save_target_lineage(forged)


def test_seat_without_acoustic_reference_rejects(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    scene_repository = measurement_repository.scene_repository
    bare = _seat('seat-bare', offset=None)
    source = scene_repository.save(
        revision.document.model_copy(
            update={'entities': (*revision.document.entities, bare)}
        ),
        parent_revision_id=revision.revision_id,
    ).revision
    doc = source.document.model_copy(
        update={
            'entities': (
                *source.document.entities,
                SceneEntity(
                    entity_id='point-bare',
                    kind='measurement_point',
                    name='Bare',
                    position=Position3(x_m=3.0, y_m=2.6, z_m=0.5),
                ),
            )
        }
    )
    created = scene_repository.save(
        doc, parent_revision_id=source.revision_id
    ).revision
    forged = build_measurement_target_lineage(
        document_id='fixture-f1',
        measurement_point_id='point-bare',
        source_seat_id='seat-bare',
        creation_revision_id=created.revision_id,
        initial_position=Position3(x_m=3.0, y_m=2.6, z_m=0.5),
    )
    with pytest.raises(ValueError, match='no acoustic listener reference'):
        quality_repository.save_target_lineage(forged)


def test_initial_position_mismatching_seat_reference_rejects(
    tmp_path: Path,
) -> None:
    scene_repository, quality_repository, _, created, _ = _derive(tmp_path)
    forged = build_measurement_target_lineage(
        document_id='fixture-f1',
        measurement_point_id='point-sofa',
        source_seat_id='seat-1',
        creation_revision_id=created.revision_id,
        initial_position=Position3(x_m=0.0, y_m=0.0, z_m=9.9),
    )
    with pytest.raises(ValueError, match='initial position'):
        quality_repository.save_target_lineage(forged)


def test_source_revision_that_is_not_the_created_parent_rejects(
    tmp_path: Path,
) -> None:
    scene_repository, quality_repository, source, created, _ = _derive(tmp_path)
    head = scene_repository.current_head('fixture-f1')
    later = scene_repository.save(
        head.document, parent_revision_id=head.revision_id
    ).revision
    forged = build_measurement_target_lineage(
        document_id='fixture-f1',
        measurement_point_id='point-sofa',
        source_seat_id='seat-1',
        creation_revision_id=created.revision_id,
        source_scene_revision_id=later.revision_id,
        initial_position=Position3(x_m=3.0, y_m=2.6, z_m=1.1),
    )
    with pytest.raises(ValueError, match='not the creation revision parent'):
        quality_repository.save_target_lineage(forged)


def test_missing_or_wrong_kind_or_wrong_position_point_rejects(
    tmp_path: Path,
) -> None:
    scene_repository, quality_repository, _, created, lineage = _derive(tmp_path)

    missing = build_measurement_target_lineage(
        document_id='fixture-f1',
        measurement_point_id='point-ghost',
        source_seat_id='seat-1',
        creation_revision_id=created.revision_id,
        initial_position=lineage.initial_position,
    )
    with pytest.raises(ValueError, match='unknown measurement point'):
        quality_repository.save_target_lineage(missing)

    wrong_kind = build_measurement_target_lineage(
        document_id='fixture-f1',
        measurement_point_id='speaker-fl',
        source_seat_id='seat-1',
        creation_revision_id=created.revision_id,
        initial_position=lineage.initial_position,
    )
    with pytest.raises(ValueError, match='not a measurement point'):
        quality_repository.save_target_lineage(wrong_kind)

    # A real measurement_point elsewhere in the same revision but not at the
    # declared initial position.
    scene_repository.save(
        created.document.model_copy(
            update={
                'entities': (
                    *created.document.entities,
                    SceneEntity(
                        entity_id='point-elsewhere',
                        kind='measurement_point',
                        name='Elsewhere',
                        position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                    ),
                )
            }
        ),
        parent_revision_id=created.revision_id,
    )
    wrong_position = build_measurement_target_lineage(
        document_id='fixture-f1',
        measurement_point_id='point-elsewhere',
        source_seat_id='seat-1',
        creation_revision_id=created.revision_id,
        initial_position=lineage.initial_position,
    )
    with pytest.raises(ValueError, match='unknown measurement point'):
        # point-elsewhere does not exist at the pinned creation revision —
        # exactly the "pin the revision containing the point" contract.
        quality_repository.save_target_lineage(wrong_position)


def test_forged_revision_content_hashes_reject(tmp_path: Path) -> None:
    scene_repository, quality_repository, source, created, lineage = _derive(
        tmp_path
    )
    forged = build_measurement_target_lineage(
        document_id='fixture-f1',
        measurement_point_id='point-sofa',
        source_seat_id='seat-1',
        creation_revision_id=created.revision_id,
        creation_scene_content_hash='0' * 64,
        source_scene_revision_id=source.revision_id,
        source_scene_content_hash=source.content_hash,
        initial_position=lineage.initial_position,
    )
    with pytest.raises(ValueError, match='content hash mismatch'):
        quality_repository.save_target_lineage(forged)


def test_seat_move_invalidates_nothing_historical_only_drift(
    tmp_path: Path,
) -> None:
    scene_repository, quality_repository, _, created, lineage = _derive(tmp_path)

    head = scene_repository.current_head('fixture-f1')
    moved = head.document.model_copy(
        update={
            'entities': tuple(
                entity.model_copy(
                    update={'position': Position3(x_m=3.0, y_m=3.4, z_m=0.5)}
                )
                if entity.entity_id == 'seat-1'
                else entity
                for entity in head.document.entities
            )
        }
    )
    scene_repository.save(moved, parent_revision_id=head.revision_id)

    # The historical derivation still revalidates byte-identically at reads;
    # only the drift report changes.
    assert quality_repository.get_target_lineage('point-sofa') == lineage
    assert quality_repository.list_target_lineages('fixture-f1') == (lineage,)
    drift = measurement_target_drift(scene_repository, lineage)
    assert drift is not None
    assert drift.drift_m == pytest.approx(0.8)
    assert drift.initial_drift_m == pytest.approx(0.0)


def test_read_path_rejects_forged_payload_row(tmp_path: Path) -> None:
    scene_repository, quality_repository, _, created, lineage = _derive(tmp_path)

    # A row claiming a different source seat but carrying a consistent
    # self-hash must still fail on the authoritative read — direct write
    # bypasses save validation, simulating a legacy/corrupt row.
    seatless_revision = created
    forged = build_measurement_target_lineage(
        document_id='fixture-f1',
        measurement_point_id='point-forged',
        source_seat_id='seat-ghost',
        creation_revision_id=seatless_revision.revision_id,
        initial_position=lineage.initial_position,
    )
    assert forged is not None  # the seal is self-consistent
    with quality_repository._connect() as connection:
        connection.execute(
            'INSERT INTO cad_measurement_target_lineages('
            'target_lineage_id, document_id, measurement_point_id,'
            ' target_lineage_sha256, payload_json, created_at_utc)'
            ' VALUES(?,?,?,?,?,?)',
            (
                forged.target_lineage_id,
                forged.document_id,
                forged.measurement_point_id,
                forged.target_lineage_sha256,
                forged.model_dump_json(),
                forged.created_at_utc,
            ),
        )
    with pytest.raises(ValueError, match='unknown'):
        quality_repository.get_target_lineage('point-forged')
    with pytest.raises(ValueError, match='unknown'):
        quality_repository.list_target_lineages('fixture-f1')
