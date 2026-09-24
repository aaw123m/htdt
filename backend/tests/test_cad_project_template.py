from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_project_template import (
    ProjectTemplateInstantiation,
    TemplateAuthorityRef,
    TemplateDesignBrief,
    TemplateMeasurementSpec,
    TemplateSpeakerSpec,
    build_project_template,
    builtin_project_templates,
    create_project_from_template,
    materialize_template_layout,
    materialize_template_scene,
    preview_document_as_template,
    resolve_template_target_refs,
    save_document_as_template,
    theater_5_1_4_template,
    theater_7_1_4_template,
    tv_room_starter_template,
)
from htdt.cad_project_template_repository import (
    CadProjectTemplateRepository,
    ProjectTemplateConflictError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, make_f1_scene
from htdt.cad_standards_profiles import dolby_atmos_home_5_1_2_profile
from htdt.project_lifecycle import ProjectLibrary

NOW = '2026-09-24T00:00:00+00:00'


class _StandardsSource:
    """Minimal standards source exposing get_profile for ref resolution."""

    def __init__(self, profiles=()):
        self._profiles = {
            (profile.profile_id, profile.version): profile
            for profile in profiles
        }

    def get_profile(self, profile_id: str, version: str):
        return self._profiles.get((profile_id, version))


def _repository(tmp_path: Path) -> CadProjectTemplateRepository:
    return CadProjectTemplateRepository(SceneRepository(tmp_path / 'cad.sqlite3'))


# -- model -------------------------------------------------------------------


def test_builtin_templates_are_intent_only() -> None:
    builtins = builtin_project_templates()
    assert len(builtins) >= 3
    roles_514 = {
        spec.speaker_role for spec in theater_5_1_4_template().speaker_specs
    }
    assert roles_514 == {
        'FL', 'C', 'FR', 'SL', 'SR', 'TFL', 'TFR', 'TRL', 'TRR', 'LFE'
    }
    tv = tv_room_starter_template()
    assert tv.design_brief.display_intent == 'direct_view'
    # No manufacturer or device claims — brief has no equipment fields.
    assert 'equipment' not in tv.design_brief.model_dump()


def test_duplicate_roles_rejected() -> None:
    with pytest.raises(ValidationError, match='unique'):
        build_project_template(
            template_id='t-1',
            version='1',
            name='dup',
            kind='user',
            speaker_specs=(
                TemplateSpeakerSpec(
                    speaker_role='FL', name='L', nominal_azimuth_deg=-30.0
                ),
                TemplateSpeakerSpec(
                    speaker_role='FL', name='R', nominal_azimuth_deg=30.0
                ),
            ),
        )


def test_template_hash_is_verified() -> None:
    template = theater_5_1_4_template()
    with pytest.raises(ValidationError, match='hash mismatch'):
        type(template)(
            **{**template.model_dump(mode='json'), 'name': 'renamed'}
        )


# -- materialization ----------------------------------------------------------


def test_seed_scene_is_intent_only(tmp_path: Path) -> None:
    """#795: creation seeds no physical entities and no fake MLP."""

    scene = materialize_template_scene(
        theater_5_1_4_template(), document_id='doc-a'
    )
    assert scene.room is None
    assert scene.entities == ()


def test_materialize_layout_aims_at_listener() -> None:
    """#795: explicit layout materialization aims speakers at the listener."""

    template = theater_5_1_4_template()
    listener = Position3(x_m=0.0, y_m=0.0, z_m=1.1)
    entities_a = materialize_template_layout(
        template, listener_position=listener
    )
    entities_b = materialize_template_layout(
        template, listener_position=listener
    )
    ids_a = {entity.entity_id for entity in entities_a}
    ids_b = {entity.entity_id for entity in entities_b}
    assert not ids_a & ids_b  # fresh identities per materialization
    assert all(entity.kind == 'speaker' for entity in entities_a)
    # No fabricated measurement point or seat.
    assert not any(
        entity.kind == 'measurement_point' for entity in entities_a
    )
    roles = {entity.speaker_role for entity in entities_a}
    assert roles == {spec.speaker_role for spec in template.speaker_specs}
    aims = set()
    for entity in entities_a:
        dx = listener.x_m - entity.position.x_m
        dy = listener.y_m - entity.position.y_m
        dz = listener.z_m - entity.position.z_m
        length = (dx * dx + dy * dy + dz * dz) ** 0.5
        aim = entity.aim_xyz
        assert aim is not None
        assert aim.x == pytest.approx(dx / length, abs=1e-6)
        assert aim.y == pytest.approx(dy / length, abs=1e-6)
        assert aim.z == pytest.approx(dz / length, abs=1e-6)
        aims.add((round(aim.x, 4), round(aim.y, 4), round(aim.z, 4)))
    # Aims differ per position — never one constant vector.
    assert len(aims) > 1


def test_materialize_layout_listener_override() -> None:
    """A declared off-center listener changes the computed aims."""

    template = theater_5_1_4_template()
    centered = materialize_template_layout(
        template, listener_position=Position3(x_m=0.0, y_m=0.0, z_m=1.1)
    )
    shifted = materialize_template_layout(
        template, listener_position=Position3(x_m=0.8, y_m=0.4, z_m=1.1)
    )
    fl_c = next(e for e in centered if e.speaker_role == 'FL')
    fl_s = next(e for e in shifted if e.speaker_role == 'FL')
    assert fl_c.aim_xyz != fl_s.aim_xyz


def test_create_project_registers_with_library(tmp_path: Path) -> None:
    """#795: template-seeded projects are real library projects."""

    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    library = ProjectLibrary(tmp_path / 'cad.sqlite3')
    repository = _repository(tmp_path)
    template = theater_5_1_4_template()
    document_id, instantiation = create_project_from_template(
        scene_repository,
        template,
        library=library,
        display_name='My Theater',
        created_at_utc=NOW,
        instantiation_repository=repository,
        standards_repository=_StandardsSource(
            (dolby_atmos_home_5_1_2_profile(),)
        ),
    )
    assert document_id != template.template_id
    record = library.find_by_document(document_id)
    assert record is not None
    assert record.display_name == 'My Theater'
    assert instantiation.project_id == record.project_id
    assert instantiation.template_version == template.version
    assert instantiation.template_sha256 == template.template_sha256
    # The pinned Dolby ref resolves against the installed profile.
    assert instantiation.unresolved_refs == ()
    saved = scene_repository.latest(document_id)
    assert saved is not None
    assert saved.document.entities == ()  # intent only
    persisted = repository.instantiation_for_document(document_id)
    assert persisted == instantiation
    # Existing document id is never silently reused.
    with pytest.raises(ValueError, match='already exists'):
        create_project_from_template(
            scene_repository,
            template,
            library=library,
            document_id=document_id,
            created_at_utc=NOW,
        )


def test_unresolved_refs_landed_on_instantiation(tmp_path: Path) -> None:
    """Missing authorities are recorded, never silently substituted."""

    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    library = ProjectLibrary(tmp_path / 'cad.sqlite3')
    template = theater_5_1_4_template()
    _document_id, instantiation = create_project_from_template(
        scene_repository,
        template,
        library=library,
        created_at_utc=NOW,
        standards_repository=_StandardsSource(()),  # nothing installed
    )
    assert len(instantiation.unresolved_refs) == 1
    assert (
        instantiation.unresolved_refs[0].ref_id
        == 'dolby-atmos-home-5.1.2-layout'
    )


def test_resolve_target_refs_statuses() -> None:
    template = theater_5_1_4_template()
    profile = dolby_atmos_home_5_1_2_profile()
    resolved = resolve_template_target_refs(
        template, _StandardsSource((profile,))
    )
    assert [item.status for item in resolved] == ['resolved']
    missing = resolve_template_target_refs(template, _StandardsSource(()))
    assert [item.status for item in missing] == ['missing']
    # A template pinning different semantics reports a hash conflict.
    tampered = build_project_template(
        template_id='t-tampered',
        version='1',
        name='tampered',
        kind='user',
        target_refs=(
            TemplateAuthorityRef(
                kind='standards_profile',
                ref_id=profile.profile_id,
                version=profile.version,
                semantic_hash_sha256='0' * 64,
            ),
        ),
    )
    conflict = resolve_template_target_refs(
        tampered, _StandardsSource((profile,))
    )
    assert [item.status for item in conflict] == ['hash_conflict']


def test_builtin_ref_declares_partial_coverage() -> None:
    """Built-ins pin the exact Dolby 5.1.2 profile and admit partial coverage."""

    for template in (
        theater_5_1_4_template(),
        theater_7_1_4_template(),
    ):
        assert len(template.target_refs) == 1
        ref = template.target_refs[0]
        assert ref.coverage == 'partial'
        assert (
            ref.semantic_hash_sha256
            == dolby_atmos_home_5_1_2_profile().profile_semantic_hash
        )


# -- save-as-template ---------------------------------------------------------


def test_preview_and_save_exclude_evidence() -> None:
    document = make_f1_scene()
    preview = preview_document_as_template(document)
    assert 'speaker-fl' in preview.included_entity_ids
    assert 'point-mlp' in preview.excluded_entity_ids
    assert 'furniture-left' in preview.excluded_entity_ids
    assert 'measurements' in preview.excluded_fields

    template = save_document_as_template(
        document, template_id='t-user', version='1', name='My room'
    )
    assert template.kind == 'user'
    roles = {spec.speaker_role for spec in template.speaker_specs}
    assert roles == {'FL', 'C', 'FR'}
    # Relative layout intent is carried as nominal angles around the MLP.
    fl = next(spec for spec in template.speaker_specs if spec.speaker_role == 'FL')
    assert fl.nominal_azimuth_deg < 0


def test_editing_template_never_touches_created_project(tmp_path: Path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    library = ProjectLibrary(tmp_path / 'cad.sqlite3')
    template = theater_5_1_4_template()
    document_id, _ = create_project_from_template(
        scene_repository, template, library=library, created_at_utc=NOW
    )
    before = scene_repository.latest(document_id).document
    _edited = save_document_as_template(
        make_f1_scene(), template_id='t-user', version='1', name='x'
    )
    after = scene_repository.latest(document_id).document
    assert before == after


# -- repository ----------------------------------------------------------------


def test_repository_lists_builtins_and_users(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    user = save_document_as_template(
        make_f1_scene(), template_id='t-user', version='1', name='User'
    )
    repository.save_template(user)
    templates = repository.list_templates()
    ids = {item.template_id for item in templates}
    assert 'builtin-theater-5.1.4' in ids
    assert 't-user' in ids
    assert repository.get_template('t-user', '1') == user
    builtin = repository.get_template('builtin-theater-5.1.4', '1')
    assert builtin is not None and builtin.kind == 'builtin'


def test_repository_rejects_builtin_save_and_conflicts(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    with pytest.raises(ValueError, match='read-only'):
        repository.save_template(theater_5_1_4_template())
    user = save_document_as_template(
        make_f1_scene(), template_id='t-user', version='1', name='User'
    )
    repository.save_template(user)
    repository.save_template(user)  # same content = no-op
    conflict = save_document_as_template(
        make_f1_scene(), template_id='t-user', version='1', name='Different'
    )
    with pytest.raises(ProjectTemplateConflictError):
        repository.save_template(conflict)


def test_instantiation_hash_verified() -> None:
    with pytest.raises(ValidationError, match='hash mismatch'):
        ProjectTemplateInstantiation(
            instantiation_id='i-1',
            document_id='doc-1',
            project_id='p-1',
            template_id='t-1',
            template_version='1',
            template_sha256='a' * 64,
            created_at_utc=NOW,
            instantiation_sha256='0' * 64,
        )


def test_measurement_spec_unique_roles() -> None:
    with pytest.raises(ValidationError, match='unique'):
        TemplateMeasurementSpec(channel_roles=('FL', 'FL'))
