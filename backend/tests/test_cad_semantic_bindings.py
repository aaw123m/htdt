"""Issue #641 regression tests: scene entity extensibility boundary.

New object semantics bind as typed capability records against a registry
instead of growing the closed ``kind`` enum.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from pydantic import ValidationError

from htdt.cad_scene import (
    Position3,
    SceneDocument,
    SceneEntity,
    SemanticCapabilityBinding,
    Size3,
    canonical_scene_json,
    make_f1_scene,
    scene_content_hash,
)
from htdt.cad_semantic_bindings import (
    UnknownCapabilityError,
    bind_capabilities,
    capability_names,
    default_semantic_bindings,
    entity_capabilities,
    entity_has_capability,
    entity_semantic_bindings,
    validate_capability_binding,
)


def _speaker(**overrides) -> SceneEntity:
    payload = {
        'entity_id': 'spk-1',
        'kind': 'speaker',
        'name': 'FL',
        'speaker_role': 'FL',
        'position': Position3(x_m=1.0, y_m=1.0, z_m=0.5),
        'size_m': Size3(x_m=0.3, y_m=0.3, z_m=0.5),
    }
    payload.update(overrides)
    return SceneEntity(**payload)


def test_registry_and_default_bindings() -> None:
    names = capability_names()
    assert 'acoustic_source' in names
    assert 'listener' in names
    speaker = _speaker()
    defaults = default_semantic_bindings(speaker)
    assert {b.capability for b in defaults} == {
        'acoustic_source', 'obstruction', 'mountable'
    }
    assert entity_capabilities(speaker) == frozenset(
        {'acoustic_source', 'obstruction', 'mountable'}
    )
    assert entity_has_capability(speaker, 'acoustic_source')
    assert not entity_has_capability(speaker, 'enclosed_volume')


def test_validate_binding_fails_closed() -> None:
    with pytest.raises(UnknownCapabilityError):
        validate_capability_binding(
            SemanticCapabilityBinding(capability='warp_drive')
        )
    with pytest.raises(ValueError, match='invalid parameters'):
        validate_capability_binding(
            SemanticCapabilityBinding(
                capability='listener',
                parameters={'ear_height_m': -1.0},
            )
        )
    with pytest.raises(ValueError, match='invalid parameters'):
        validate_capability_binding(
            SemanticCapabilityBinding(
                capability='obstruction', parameters={'bogus': 1}
            )
        )


def test_bind_capabilities_merges_over_defaults() -> None:
    speaker = _speaker()
    extended = bind_capabilities(
        speaker,
        [SemanticCapabilityBinding(
            capability='enclosed_volume', parameters={'vented': True}
        )],
    )
    caps = entity_capabilities(extended)
    assert 'enclosed_volume' in caps
    assert 'acoustic_source' in caps  # default survives
    bindings = {b.capability: b for b in entity_semantic_bindings(extended)}
    assert bindings['enclosed_volume'].parameters == {'vented': True}


def test_extension_entity_pattern() -> None:
    """An 'equipment rack' is kind=furniture plus capability bindings —
    no new enum member needed."""
    rack = SceneEntity(
        entity_id='rack-1', kind='furniture', name='Rack',
        position=Position3(x_m=2.0, y_m=0.5, z_m=0.6),
        size_m=Size3(x_m=0.6, y_m=0.5, z_m=1.2),
        semantic_bindings=(
            SemanticCapabilityBinding(capability='enclosed_volume',
                                      parameters={'vented': False}),
            SemanticCapabilityBinding(capability='support_surface',
                                      parameters={'rack_units': 12,
                                                  'max_load_kg': 60.0}),
        ),
    )
    assert entity_has_capability(rack, 'enclosed_volume')
    assert entity_has_capability(rack, 'obstruction')  # furniture default


def test_scene_entity_rejects_duplicate_or_empty_bindings() -> None:
    with pytest.raises(ValidationError, match='duplicate semantic'):
        _speaker(semantic_bindings=(
            SemanticCapabilityBinding(capability='mountable'),
            SemanticCapabilityBinding(capability='mountable'),
        ))
    with pytest.raises(ValidationError, match='omitted when empty'):
        _speaker(semantic_bindings=())


def test_bindings_omitted_from_canonical_when_none() -> None:
    document = make_f1_scene()
    canonical = canonical_scene_json(document)
    assert 'semantic_bindings' not in canonical
    assert 'operational_zones' not in canonical
    reopened = SceneDocument.model_validate(json.loads(canonical))
    assert scene_content_hash(reopened) == scene_content_hash(document)
