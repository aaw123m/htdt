"""Deterministic representative fixtures for the #867 perf harness.

Small/medium/large generated projects with a recorded identity manifest —
the fixture *spec* (size, entity/revision counts, canonical document hash)
is the versioned identity; the sqlite bytes are regenerated on demand and
never hashed (sqlite layout is not guaranteed byte-stable).
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Literal

from .cad_repository import SceneRepository
from .cad_scene import (
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from .canonical_json import canonical_sha256

FixtureSize = Literal['small', 'medium', 'large']

FIXTURE_GENERATOR_VERSION = 'perf-fixture-1'
"""Bump when generation semantics change; stale fixtures regenerate."""

FIXTURE_SPECS: dict[str, dict[str, int | float | str]] = {
    'small': {
        'entity_count': 24, 'revision_count': 4, 'seed': 867001,
        'room_width_m': 5.0, 'room_depth_m': 7.0, 'room_height_m': 2.5,
    },
    'medium': {
        'entity_count': 300, 'revision_count': 12, 'seed': 867002,
        'room_width_m': 9.0, 'room_depth_m': 12.0, 'room_height_m': 3.2,
    },
    'large': {
        'entity_count': 1500, 'revision_count': 24, 'seed': 867003,
        'room_width_m': 15.0, 'room_depth_m': 20.0, 'room_height_m': 5.0,
    },
}

_SIZED_KINDS = ('av_equipment', 'furniture', 'screen', 'riser')
_SIZES = {
    'speaker': Size3(x_m=0.25, y_m=0.3, z_m=0.45),
    'seat': Size3(x_m=0.6, y_m=0.8, z_m=1.0),
    'av_equipment': Size3(x_m=0.45, y_m=0.35, z_m=0.15),
    'furniture': Size3(x_m=1.2, y_m=0.8, z_m=0.9),
    'screen': Size3(x_m=2.4, y_m=0.05, z_m=1.35),
    'riser': Size3(x_m=3.0, y_m=2.0, z_m=0.2),
}
_SPEAKER_SHARE = 0.08
_SEAT_SHARE = 0.30
_MEASUREMENT_SHARE = 0.20


def _entity(index: int, rng: random.Random, room: dict) -> SceneEntity:
    share = index / max(1, int(room['entity_count']))
    if share < _SPEAKER_SHARE:
        kind = 'speaker'
        role = f'SP{index}'
    elif share < _SPEAKER_SHARE + _SEAT_SHARE:
        kind = 'seat'
        role = None
    elif share < _SPEAKER_SHARE + _SEAT_SHARE + _MEASUREMENT_SHARE:
        kind = 'measurement_point'
        role = None
    else:
        kind = _SIZED_KINDS[index % len(_SIZED_KINDS)]
        role = None
    entity = SceneEntity(
        entity_id=f'ent-{index:05d}',
        kind=kind,  # type: ignore[arg-type]
        name=f'{kind}-{index}',
        position=Position3(
            x_m=round(rng.uniform(0.2, float(room['room_width_m']) - 0.4), 4),
            y_m=round(rng.uniform(0.2, float(room['room_depth_m']) - 0.4), 4),
            z_m=round(rng.uniform(0.0, float(room['room_height_m']) - 0.6), 4),
        ),
        size_m=_SIZES.get(kind),
        speaker_role=role,
    )
    return entity


def _document(
    document_id: str, spec: dict, seed_shift: int = 0,
) -> SceneDocument:
    rng = random.Random(int(spec['seed']) + seed_shift)
    entities = tuple(
        _entity(i, rng, spec) for i in range(int(spec['entity_count'])))
    return SceneDocument(
        document_id=document_id,
        schema_version=2,
        room=RoomPrism(
            width_m=float(spec['room_width_m']),
            depth_m=float(spec['room_depth_m']),
            height_m=float(spec['room_height_m']),
        ),
        entities=entities,
    )


def fixture_spec_sha256(size: FixtureSize) -> str:
    """Hash of the fixture *spec* — regenerating an identical fixture
    yields the same value, so identity is stable across machines."""
    return canonical_sha256({
        'generator_version': FIXTURE_GENERATOR_VERSION,
        'size': size,
        'spec': FIXTURE_SPECS[size],
    })


def expected_fixture_identity(size: FixtureSize) -> dict[str, object]:
    spec = FIXTURE_SPECS[size]
    return {
        'fixture_id': f'htdt-perf-{size}-v1',
        'size': size,
        'entity_count': int(spec['entity_count']),
        'revision_count': int(spec['revision_count']),
        'spec_sha256': fixture_spec_sha256(size),
        'generator_version': FIXTURE_GENERATOR_VERSION,
    }


def ensure_fixture(fixtures_root: Path, size: FixtureSize) -> dict[str, object]:
    """Materialize (or reuse) the deterministic fixture project.

    Returns the identity record. A fixture whose on-disk identity matches
    the expected spec sha is reused unchanged; anything else regenerates.
    """
    fixture_dir = Path(fixtures_root) / size
    identity_path = fixture_dir / 'fixture_identity.json'
    expected = expected_fixture_identity(size)
    if identity_path.exists():
        try:
            stored = json.loads(
                identity_path.read_text(encoding='utf-8'))
            if stored.get('spec_sha256') == expected['spec_sha256'] and (
                    (fixture_dir / 'cad.sqlite3').exists()):
                return stored
        except (OSError, json.JSONDecodeError):
            # error-boundary: identity file unreadable — regenerate the
            # fixture rather than trusting partial state.
            pass

    fixture_dir.mkdir(parents=True, exist_ok=True)
    db_path = fixture_dir / 'cad.sqlite3'
    if db_path.exists():
        # Stale-spec fixture — replace it wholesale rather than saving a
        # second root onto existing revision history.
        db_path.unlink()
    repository = SceneRepository(db_path)
    document_id = f'doc-perf-{size}'
    head = repository.save(
        _document(document_id, FIXTURE_SPECS[size]),
        parent_revision_id=None,
    )
    current = head.revision
    for revision_index in range(
            1, int(FIXTURE_SPECS[size]['revision_count'])):
        doc = _document(document_id, FIXTURE_SPECS[size],
                        seed_shift=revision_index)
        head = repository.save(doc, parent_revision_id=head.revision.revision_id)
        current = head.revision
    document_sha = canonical_sha256(
        _document(document_id, FIXTURE_SPECS[size],
                  seed_shift=int(FIXTURE_SPECS[size]['revision_count']) - 1)
        .model_dump_json())

    identity = dict(expected)
    identity['document_id'] = document_id
    identity['document_sha256'] = document_sha
    identity['fixture_dir'] = str(fixture_dir)
    identity['database'] = 'cad.sqlite3'
    identity_path.write_text(
        json.dumps(identity, indent=2, sort_keys=True), encoding='utf-8')
    return identity
