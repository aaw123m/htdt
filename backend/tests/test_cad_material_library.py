"""#771 curated acoustic material library tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_material_library import (
    BUILTIN_MATERIAL_LIBRARY,
    build_material_definition,
    build_material_evidence,
)
from htdt.cad_material_library_repository import (
    CadMaterialLibraryRepository,
    MaterialLibraryConflictError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene

NOW = '2026-09-23T00:00:00+00:00'


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    return scene_repository, revision, CadMaterialLibraryRepository(
        scene_repository
    )


def _material(document_id=None, **over):
    payload = {
        'category': 'porous_absorber',
        'name': 'Test absorber',
        'mounting': 'rigid_backing',
        'created_at_utc': NOW,
        'document_id': document_id,
    }
    payload.update(over)
    return build_material_definition(**payload)


def _evidence(material_id, **over):
    payload = {
        'material_id': material_id,
        'quantity': 'random_incidence_absorption_coefficient',
        'frequency_hz': (125.0, 250.0, 500.0, 1000.0),
        'values': (0.3, 0.6, 0.8, 0.9),
        'unit_label': 'alpha (0-1)',
        'incidence': 'random',
        'provenance_class': 'user_measured',
        'created_at_utc': NOW,
    }
    payload.update(over)
    return build_material_evidence(**payload)


def test_definition_and_evidence_are_separate(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    material = _material(document_id=revision.document_id)
    repository.save_material(material)
    evidence = _evidence(material.material_id)
    repository.save_evidence(evidence)
    assert repository.get_material(material.material_id) == material
    assert repository.list_evidence(material.material_id) == (evidence,)
    # One definition can hold several quantity families.
    impedance = _evidence(
        material.material_id,
        quantity='surface_impedance',
        unit_label='Pa·s/m',
        phase_deg=(0.0, 2.0, 5.0, 8.0),
    )
    repository.save_evidence(impedance)
    quantities = {
        item.quantity
        for item in repository.list_evidence(material.material_id)
    }
    assert quantities == {
        'random_incidence_absorption_coefficient',
        'surface_impedance',
    }


def test_mounting_variants_are_distinct_definitions(tmp_path: Path) -> None:
    _scenes, _rev, repository = _repositories(tmp_path)
    rigid = _material(name='Mineral wool 100 mm (rigid)', mounting='rigid_backing')
    gapped = _material(
        name='Mineral wool 100 mm + 100 mm air gap',
        mounting='air_gap',
        mounting_detail='100 mm wool over 100 mm cavity',
    )
    repository.save_material(rigid)
    repository.save_material(gapped)
    assert rigid.material_id != gapped.material_id
    assert rigid.mounting != gapped.mounting


def test_quantities_are_never_converted(tmp_path: Path) -> None:
    material = _material(document_id=None)
    # Magnitude-only random-incidence data cannot carry phase.
    with pytest.raises(ValidationError, match='phase_deg'):
        _evidence(
            material.material_id,
            phase_deg=(0.0, 1.0, 2.0, 3.0),
        )
    # Complex-capable quantities may carry phase.
    ok = _evidence(
        material.material_id,
        quantity='complex_reflection_coefficient',
        unit_label='coefficient',
        phase_deg=(0.0, 1.0, 2.0, 3.0),
    )
    assert ok.phase_deg == (0.0, 1.0, 2.0, 3.0)


def test_shared_evidence_requires_redistribution_license(tmp_path: Path) -> None:
    _scenes, _rev, repository = _repositories(tmp_path)
    shared = _material(document_id=None)
    repository.save_material(shared)
    # Shared/bundled evidence without explicit redistribution -> rejected.
    with pytest.raises(ValueError, match='redistribution'):
        repository.save_evidence(_evidence(shared.material_id))
    with pytest.raises(ValueError, match='redistribution'):
        repository.save_evidence(
            _evidence(shared.material_id, redistribution_permitted=False)
        )
    licensed = _evidence(
        shared.material_id,
        redistribution_permitted=True,
        license_name='CC0-1.0',
    )
    repository.save_evidence(licensed)

    # Project-scoped (user's own) evidence does not need redistribution rights.
    _scenes2, revision, repository2 = _repositories(tmp_path / 'p2')
    mine = _material(document_id=revision.document_id)
    repository2.save_material(mine)
    repository2.save_evidence(_evidence(mine.material_id))


def test_evidence_requires_persisted_material(tmp_path: Path) -> None:
    _scenes, _rev, repository = _repositories(tmp_path)
    with pytest.raises(ValueError, match='not persisted'):
        repository.save_evidence(_evidence('ghost-material'))


def test_evidence_append_only_per_version_quantity(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    material = _material(document_id=revision.document_id)
    repository.save_material(material)
    evidence = _evidence(material.material_id)
    repository.save_evidence(evidence)
    # Same material + version + quantity is append-only.
    with pytest.raises(MaterialLibraryConflictError):
        repository.save_evidence(_evidence(material.material_id))
    # A new version is a new row.
    repository.save_evidence(_evidence(material.material_id, version='2'))
    assert len(repository.list_evidence(material.material_id)) == 2


def test_evidence_array_alignment(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match='align'):
        _evidence('m-1', values=(0.5, 0.6))
    with pytest.raises(ValidationError):
        _evidence('m-1', frequency_hz=(0.0, 250.0, 500.0, 1000.0))


def test_builtin_library_seeds_idempotently(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    installed = repository.install_builtin_library()
    assert installed == len(BUILTIN_MATERIAL_LIBRARY)
    # Second run is a no-op.
    assert repository.install_builtin_library() == 0
    materials = repository.list_materials(revision.document_id)
    assert len(materials) == len(BUILTIN_MATERIAL_LIBRARY)
    for material, expected in BUILTIN_MATERIAL_LIBRARY:
        stored = repository.get_material(material.material_id)
        assert stored == material
        evidence = repository.list_evidence(material.material_id)
        assert evidence[0].provenance_class in {
            'generic_reference_preset',
            'analytic_model',
        }
        assert evidence[0].redistribution_permitted is True
        assert evidence[0].license_name
        # Generic values carry explicit uncertainty/limitations, never
        # presented as measured truth.
        assert evidence[0].limitations


def test_material_hash_integrity(tmp_path: Path) -> None:
    material = _material(document_id=None)
    payload = material.model_dump(mode='python')
    payload['name'] = 'Tampered'
    with pytest.raises(ValidationError, match='hash mismatch'):
        type(material)(**payload)
