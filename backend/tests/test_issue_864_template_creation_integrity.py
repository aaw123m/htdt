"""#864: template-instantiated project creation is all-or-nothing, and
Save-as-template requires an explicit layout reference.

- shared SQLite store → one transaction; a failure in any of the three
  writes leaves no partial project;
- separate stores → rollback journal removes only what creation produced;
- ``save_instantiation`` revalidates template identity/hash, the
  project_id → document_id binding, the initial document, and that
  unresolved refs are declared template target refs;
- ``instantiation_repository=None`` is a non-persisting preview only;
- entity ordering never selects the layout reference — an explicit
  TemplateLayoutReference does; no reference → topology-only intent.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from htdt.cad_project_template import (
    ProjectTemplateInstantiation,
    TemplateAuthorityRef,
    _hash,
    build_template_layout_reference,
    create_project_from_template,
    materialize_template_layout,
    save_document_as_template,
    theater_5_1_4_template,
)
from htdt.cad_project_template_repository import (
    CadProjectTemplateRepository,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, SceneEntity, make_f1_scene
from htdt.project_lifecycle import ProjectLibrary

NOW = '2026-09-24T00:00:00+00:00'


def _stores(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    library = ProjectLibrary(tmp_path / 'cad.sqlite3')
    repository = CadProjectTemplateRepository(scene_repository)
    return scene_repository, library, repository


# -- atomic creation ----------------------------------------------------------


def test_scene_failure_leaves_no_partial_project(tmp_path: Path) -> None:
    scene_repository, library, repository = _stores(tmp_path)
    template = theater_5_1_4_template()
    original = scene_repository._save_in_transaction

    def _boom(*args, **kwargs):
        raise RuntimeError('scene save failed')

    scene_repository._save_in_transaction = _boom
    try:
        with pytest.raises(RuntimeError):
            create_project_from_template(
                scene_repository,
                template,
                library=library,
                created_at_utc=NOW,
                instantiation_repository=repository,
            )
    finally:
        scene_repository._save_in_transaction = original
    # The registry row rolled back with the failed transaction.
    assert library.list_projects() == ()


def test_instantiation_failure_rolls_back_project(tmp_path: Path) -> None:
    scene_repository, library, repository = _stores(tmp_path)
    template = theater_5_1_4_template()

    def _boom(*args, **kwargs):
        raise RuntimeError('instantiation save failed')

    repository.save_instantiation = _boom
    with pytest.raises(RuntimeError):
        create_project_from_template(
            scene_repository,
            template,
            library=library,
            created_at_utc=NOW,
            instantiation_repository=repository,
        )
    assert library.list_projects() == ()


def test_creation_commits_all_three_writes(tmp_path: Path) -> None:
    scene_repository, library, repository = _stores(tmp_path)
    document_id, instantiation = create_project_from_template(
        scene_repository,
        theater_5_1_4_template(),
        library=library,
        display_name='T',
        created_at_utc=NOW,
        instantiation_repository=repository,
    )
    record = library.find_by_document(document_id)
    assert record is not None
    assert scene_repository.latest(document_id) is not None
    assert repository.instantiation_for_document(document_id) == instantiation


def test_preview_mode_persists_nothing(tmp_path: Path) -> None:
    scene_repository, library, _ = _stores(tmp_path)
    document_id, instantiation = create_project_from_template(
        scene_repository,
        theater_5_1_4_template(),
        library=library,
        created_at_utc=NOW,
    )
    # Non-persisting preview: in-memory instantiation, no project anywhere.
    assert instantiation.project_id is None
    assert library.list_projects() == ()
    assert scene_repository.latest(document_id) is None


def test_separate_stores_rollback_removes_created_rows(
    tmp_path: Path,
) -> None:
    # Library lives in a *different* SQLite file → journal rollback path.
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    library = ProjectLibrary(tmp_path / 'other' / 'library.sqlite3')
    repository = CadProjectTemplateRepository(scene_repository)
    template = theater_5_1_4_template()

    def _boom(*args, **kwargs):
        raise RuntimeError('instantiation save failed')

    repository.save_instantiation = _boom
    with pytest.raises(RuntimeError):
        create_project_from_template(
            scene_repository,
            template,
            library=library,
            created_at_utc=NOW,
            instantiation_repository=repository,
        )
    assert library.list_projects() == ()
    # Rollback never deletes pre-existing unrelated authority.
    scene_repository.save(make_f1_scene(), parent_revision_id=None)
    with pytest.raises(RuntimeError):
        create_project_from_template(
            scene_repository,
            template,
            library=library,
            document_id='doc-fail-2',
            created_at_utc=NOW,
            instantiation_repository=repository,
        )
    assert scene_repository.latest('fixture-f1') is not None
    assert library.find_by_document('doc-fail-2') is None


# -- save_instantiation revalidation ------------------------------------------


def _valid_instantiation(tmp_path: Path):
    scene_repository, library, repository = _stores(tmp_path)
    template = theater_5_1_4_template()
    document_id, instantiation = create_project_from_template(
        scene_repository,
        template,
        library=library,
        created_at_utc=NOW,
        instantiation_repository=repository,
    )
    return scene_repository, library, repository, template, document_id


def _forge(tmp_path: Path, **overrides) -> ProjectTemplateInstantiation:
    scene_repository, library, repository, template, document_id = (
        _valid_instantiation(tmp_path)
    )
    payload = dict(
        instantiation_id='forged-1',
        document_id=document_id,
        project_id=library.find_by_document(document_id).project_id,
        template_id=template.template_id,
        template_version=template.version,
        template_sha256=template.template_sha256,
        unresolved_refs=(),
        created_at_utc=NOW,
    )
    payload.update(overrides)
    forged = ProjectTemplateInstantiation.model_construct(
        **payload, instantiation_sha256=''
    )
    return (
        repository,
        forged.model_copy(
            update={
                'instantiation_sha256': _hash(forged.semantic_payload())
            }
        ),
    )


def test_fabricated_template_hash_rejected(tmp_path: Path) -> None:
    repository, forged = _forge(
        tmp_path, template_sha256='f' * 64
    )
    with pytest.raises(ValueError, match='template_sha256'):
        repository.save_instantiation(forged)


def test_unknown_template_rejected(tmp_path: Path) -> None:
    repository, forged = _forge(tmp_path, template_id='no-such-template')
    with pytest.raises(ValueError, match='unknown template'):
        repository.save_instantiation(forged)


def test_project_bound_to_other_document_rejected(tmp_path: Path) -> None:
    scene_repository, library, repository, template, document_id = (
        _valid_instantiation(tmp_path)
    )
    other = library.register_project('doc-other')
    forged = ProjectTemplateInstantiation.model_construct(
        instantiation_id='forged-2',
        document_id=document_id,
        project_id=other.project_id,
        template_id=template.template_id,
        template_version=template.version,
        template_sha256=template.template_sha256,
        unresolved_refs=(),
        created_at_utc=NOW,
        instantiation_sha256='',
    )
    forged = forged.model_copy(
        update={'instantiation_sha256': _hash(forged.semantic_payload())}
    )
    with pytest.raises(ValueError, match='bound to document'):
        repository.save_instantiation(forged)


def test_missing_project_binding_rejected(tmp_path: Path) -> None:
    repository, forged = _forge(tmp_path, project_id=None)
    with pytest.raises(ValueError, match='project binding'):
        repository.save_instantiation(forged)


def test_unresolved_ref_not_declared_rejected(tmp_path: Path) -> None:
    repository, forged = _forge(
        tmp_path,
        unresolved_refs=(
            TemplateAuthorityRef(
                kind='standards_profile',
                ref_id='phantom-authority',
            ),
        ),
    )
    with pytest.raises(ValueError, match='not a declared target ref'):
        repository.save_instantiation(forged)


def test_read_revalidates_row(tmp_path: Path) -> None:
    scene_repository, library, repository = _stores(tmp_path)
    template = theater_5_1_4_template()
    document_id, _ = create_project_from_template(
        scene_repository,
        template,
        library=library,
        created_at_utc=NOW,
        instantiation_repository=repository,
    )
    # Corrupt the persisted payload: claim a different template identity.
    corrupted = repository.instantiation_for_document(
        document_id
    ).model_copy(update={'template_id': 'ghost-template'})
    corrupted = corrupted.model_copy(
        update={
            'instantiation_sha256': _hash(corrupted.semantic_payload())
        }
    )
    connection = sqlite3.connect(str(repository.path))
    try:
        connection.execute(
            'UPDATE template_instantiations SET payload_json=?,'
            ' instantiation_sha256=? WHERE document_id=?',
            (
                corrupted.model_dump_json(),
                corrupted.instantiation_sha256,
                document_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ValueError, match='unknown template'):
        repository.instantiation_for_document(document_id)


# -- layout reference ----------------------------------------------------------


def _two_point_scene(first_id: str, second_id: str):
    base = make_f1_scene()
    kept = [
        entity
        for entity in base.entities
        if entity.entity_id != 'point-mlp'
    ]
    positions = {
        'point-mlp': Position3(x_m=3.0, y_m=3.0, z_m=1.1),
        'point-diagnostic': Position3(x_m=1.0, y_m=1.0, z_m=1.1),
    }
    extra = [
        SceneEntity(
            entity_id=entity_id,
            kind='measurement_point',
            name='Point ' + entity_id,
            position=positions[entity_id],
        )
        for entity_id in (first_id, second_id)
    ]
    return base.model_copy(
        update={'entities': tuple([*kept, *extra])}
    )


def test_entity_order_never_selects_reference(tmp_path: Path) -> None:
    # Same physical scene, reversed measurement-point order: with an
    # explicit reference both saves produce identical speaker intent.
    document_a = _two_point_scene('point-mlp', 'point-diagnostic')
    document_b = _two_point_scene('point-diagnostic', 'point-mlp')
    reference_a = build_template_layout_reference(
        document_a,
        scene_revision_id='rev-1',
        source_kind='measurement_point',
        source_entity_id='point-mlp',
    )
    reference_b = build_template_layout_reference(
        document_b,
        scene_revision_id='rev-1',
        source_kind='measurement_point',
        source_entity_id='point-mlp',
    )
    specs_a = save_document_as_template(
        document_a,
        template_id='t-a',
        version='1',
        name='A',
        layout_reference=reference_a,
    ).speaker_specs
    specs_b = save_document_as_template(
        document_b,
        template_id='t-b',
        version='1',
        name='B',
        layout_reference=reference_b,
    ).speaker_specs
    assert specs_a == specs_b
    # Entity order never silently becomes the reference: without one, both
    # documents produce topology-only intent (no fabricated geometry).
    topology_a = save_document_as_template(
        document_a, template_id='t-a', version='2', name='A2'
    )
    topology_b = save_document_as_template(
        document_b, template_id='t-b', version='2', name='B2'
    )
    assert topology_a.speaker_specs == topology_b.speaker_specs
    assert all(
        spec.nominal_azimuth_deg is None
        for spec in topology_a.speaker_specs
    )


def test_explicit_reference_deterministic_geometry() -> None:
    document = make_f1_scene()
    reference = build_template_layout_reference(
        document,
        scene_revision_id='rev-9',
        source_kind='measurement_point',
        source_entity_id='point-mlp',
    )
    a = save_document_as_template(
        document,
        template_id='t-x',
        version='1',
        name='x',
        layout_reference=reference,
    )
    b = save_document_as_template(
        document,
        template_id='t-x',
        version='1',
        name='x',
        layout_reference=reference,
    )
    assert a.speaker_specs == b.speaker_specs
    assert a.layout_reference == reference


def test_layout_reference_must_pin_this_document() -> None:
    document = make_f1_scene()
    other = _two_point_scene('point-mlp', 'point-diagnostic')
    foreign = build_template_layout_reference(
        other,
        scene_revision_id='rev-2',
        source_kind='measurement_point',
        source_entity_id='point-diagnostic',
    )
    with pytest.raises(ValueError, match='different document revision'):
        save_document_as_template(
            document,
            template_id='t-x',
            version='1',
            name='x',
            layout_reference=foreign,
        )


def test_reference_kinds_and_guards() -> None:
    document = make_f1_scene()
    # Wrong entity kind fails.
    with pytest.raises(ValueError, match='expected measurement_point'):
        build_template_layout_reference(
            document,
            scene_revision_id='rev-1',
            source_kind='measurement_point',
            source_entity_id='speaker-fl',
        )
    # Explicit position works without an entity.
    reference = build_template_layout_reference(
        document,
        scene_revision_id='rev-1',
        source_kind='explicit_position',
        explicit_position=Position3(x_m=0.0, y_m=0.0, z_m=1.1),
    )
    assert reference.reference_position == Position3(
        x_m=0.0, y_m=0.0, z_m=1.1
    )


def test_topology_only_template_cannot_materialize_positions() -> None:
    template = save_document_as_template(
        make_f1_scene(), template_id='t-topo', version='1', name='topo'
    )
    assert all(
        spec.nominal_azimuth_deg is None
        for spec in template.speaker_specs
    )
    assert template.layout_reference is None
    with pytest.raises(ValueError, match='topology-only'):
        materialize_template_layout(
            template,
            listener_position=Position3(x_m=0.0, y_m=0.0, z_m=1.1),
        )
