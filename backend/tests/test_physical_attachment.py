"""Issue #661 regression tests: physical attachment and assembly authority."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from pydantic import ValidationError

from htdt.cad_attachment_models import EntityAttachment
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.physical_attachment import (
    AttachmentGraphError,
    apply_attachments,
    attached_world_position,
    attachment_children,
    attachment_graph,
    resolve_attached_positions,
    validate_attachment_graph,
)


def _entity(entity_id: str, *, x: float, y: float, z: float,
            size=(0.5, 0.5, 0.5), kind: str = 'furniture') -> SceneEntity:
    extra = {'speaker_role': 'left_front'} if kind == 'speaker' else {}
    return SceneEntity(
        entity_id=entity_id,
        kind=kind,
        name=entity_id,
        position=Position3(x_m=x, y_m=y, z_m=z),
        size_m=Size3(x_m=size[0], y_m=size[1], z_m=size[2]),
        **extra,
    )


def _document(*entities: SceneEntity, attachments=()) -> SceneDocument:
    return SceneDocument(
        document_id='attach-fixture',
        schema_version=5,
        room=RoomPrism(width_m=10.0, depth_m=8.0, height_m=3.0),
        entities=entities,
        attachments=attachments or None,
    )


def test_attachment_kind_validators() -> None:
    with pytest.raises(ValidationError, match='itself'):
        EntityAttachment(
            attachment_id='a', child_entity_id='x', parent_entity_id='x',
            kind='stand_on', parent_anchor='top_surface',
        )
    with pytest.raises(ValidationError, match='rack_unit'):
        EntityAttachment(
            attachment_id='a', child_entity_id='c', parent_entity_id='p',
            kind='racked_in', parent_anchor='interior',
        )
    with pytest.raises(ValidationError, match='top surface'):
        EntityAttachment(
            attachment_id='a', child_entity_id='c', parent_entity_id='p',
            kind='stand_on', parent_anchor='front_face',
        )


def _with_invalid_attachments(
    document: SceneDocument, attachments: tuple[EntityAttachment, ...]
) -> SceneDocument:
    """model_copy bypasses validators — invalid graphs are constructible only
    through the bypass, mirroring how production state must never persist."""

    return document.model_copy(update={'attachments': attachments})


def test_graph_validation_fail_closed() -> None:
    a = _entity('a', x=1, y=1, z=0.5)
    b = _entity('b', x=2, y=1, z=0.5)
    doc = _with_invalid_attachments(
        _document(a, b),
        (
            EntityAttachment(
                attachment_id='x', child_entity_id='a', parent_entity_id='ghost',
                kind='stand_on', parent_anchor='top_surface',
            ),
        ),
    )
    with pytest.raises(AttachmentGraphError, match='unknown entity'):
        attachment_graph(doc)
    cyclic = _with_invalid_attachments(
        _document(a, b),
        (
            EntityAttachment(
                attachment_id='ab', child_entity_id='a', parent_entity_id='b',
                kind='stand_on', parent_anchor='top_surface',
            ),
            EntityAttachment(
                attachment_id='ba', child_entity_id='b', parent_entity_id='a',
                kind='stand_on', parent_anchor='top_surface',
            ),
        ),
    )
    with pytest.raises(AttachmentGraphError, match='cycle'):
        validate_attachment_graph(cyclic)
    two_parents = _with_invalid_attachments(
        _document(a, b, _entity('c', x=3, y=1, z=0.5)),
        (
            EntityAttachment(
                attachment_id='ab', child_entity_id='a', parent_entity_id='b',
                kind='stand_on', parent_anchor='top_surface',
            ),
            EntityAttachment(
                attachment_id='ac', child_entity_id='a', parent_entity_id='c',
                kind='stand_on', parent_anchor='top_surface',
            ),
        ),
    )
    with pytest.raises(AttachmentGraphError, match='more than one parent'):
        attachment_graph(two_parents)


def test_document_rejects_invalid_attachment_graph() -> None:
    """Schema-level check: malformed graphs fail closed at validation (#661)."""

    a = _entity('a', x=1, y=1, z=0.5)
    b = _entity('b', x=2, y=1, z=0.5)
    with pytest.raises(ValidationError, match='unknown entity'):
        _document(
            a, b,
            attachments=(
                EntityAttachment(
                    attachment_id='x', child_entity_id='a', parent_entity_id='ghost',
                    kind='stand_on', parent_anchor='top_surface',
                ),
            ),
        )
    with pytest.raises(ValidationError, match='cycle'):
        _document(
            a, b,
            attachments=(
                EntityAttachment(
                    attachment_id='ab', child_entity_id='a', parent_entity_id='b',
                    kind='stand_on', parent_anchor='top_surface',
                ),
                EntityAttachment(
                    attachment_id='ba', child_entity_id='b', parent_entity_id='a',
                    kind='stand_on', parent_anchor='top_surface',
                ),
            ),
        )


def test_derived_positions_stand_and_rack() -> None:
    stand = _entity('stand', x=2.0, y=2.0, z=0.5, size=(0.6, 0.6, 1.0))
    speaker = _entity('spk', x=9.9, y=9.9, z=0.1, size=(0.3, 0.3, 0.4),
                      kind='speaker')
    speaker = speaker.model_copy(update={'speaker_role': 'FL'})
    rack = _entity('rack', x=8.0, y=0.8, z=0.8, size=(0.6, 0.5, 1.6),
                   kind='av_equipment')
    receiver = _entity('avr', x=0.0, y=0.0, z=0.0, size=(0.45, 0.3, 0.09),
                       kind='av_equipment')
    document = _document(
        stand, speaker, rack, receiver,
        attachments=(
            EntityAttachment(
                attachment_id='spk-on-stand', child_entity_id='spk',
                parent_entity_id='stand', kind='stand_on',
                parent_anchor='top_surface',
            ),
            EntityAttachment(
                attachment_id='avr-in-rack', child_entity_id='avr',
                parent_entity_id='rack', kind='racked_in',
                parent_anchor='interior', rack_unit=4,
            ),
        ),
    )
    positions = resolve_attached_positions(document)
    # Speaker sits on the stand's top surface (z = 0.5 + 0.5 = 1.0).
    assert positions['spk'].z_m == pytest.approx(1.0)
    assert positions['spk'].x_m == pytest.approx(2.0)
    # Receiver occupies rack unit 4: front face at unit height above base.
    unit_height = 0.04445
    assert positions['avr'].z_m == pytest.approx(
        0.8 - 0.8 + (4 - 1) * unit_height + unit_height * 0.5
    )
    assert positions['avr'].y_m == pytest.approx(0.8 - 0.25)  # rack front face
    assert attachment_children(document) == {
        'rack': ('avr',), 'stand': ('spk',),
    }
    applied = apply_attachments(document)
    assert applied.entity('spk').position.z_m == pytest.approx(1.0)
    # apply_attachments is idempotent.
    assert apply_attachments(applied).entity('avr').position.z_m == pytest.approx(
        positions['avr'].z_m
    )


def test_transitive_attachment_chain() -> None:
    base = _entity('base', x=2.0, y=2.0, z=0.4, size=(0.8, 0.8, 0.8))
    shelf = _entity('shelf', x=0, y=0, z=0, size=(0.5, 0.4, 0.05))
    device = _entity('dev', x=0, y=0, z=0, size=(0.3, 0.3, 0.1))
    document = _document(
        base, shelf, device,
        attachments=(
            EntityAttachment(
                attachment_id='s-on-b', child_entity_id='shelf',
                parent_entity_id='base', kind='stand_on',
                parent_anchor='top_surface',
            ),
            EntityAttachment(
                attachment_id='d-on-s', child_entity_id='dev',
                parent_entity_id='shelf', kind='stand_on',
                parent_anchor='top_surface',
            ),
        ),
    )
    positions = resolve_attached_positions(document)
    # shelf sits on base top (z=0.8); device on shelf top (z = 0.8+0.025).
    assert positions['dev'].z_m == pytest.approx(0.8 + 0.025)


def test_attachments_persist_in_repository(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = _document(
        _entity('p', x=1, y=1, z=0.5),
        _entity('c', x=0, y=0, z=0),
        attachments=(
            EntityAttachment(
                attachment_id='c-on-p', child_entity_id='c',
                parent_entity_id='p', kind='stacked_on',
                parent_anchor='top_surface',
            ),
        ),
    )
    saved = repository.save(document, parent_revision_id=None)
    reopened = repository.latest('attach-fixture')
    assert reopened is not None
    assert reopened.document == saved.revision.document
    assert attachment_graph(reopened.document)['c'].kind == 'stacked_on'
