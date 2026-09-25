"""Setup-intent convergence: wizard/template intent → canonical brief (#898)."""

from __future__ import annotations

from pathlib import Path

from htdt.cad_design_brief_repository import CadDesignBriefRepository
from htdt.cad_project_template import (
    ProjectTemplateInstantiation,
    create_project_from_template,
    theater_5_1_4_template,
)
from htdt.cad_project_template_repository import (
    CadProjectTemplateRepository,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene
from htdt.cad_standards_profiles import dolby_atmos_home_5_1_2_profile
from htdt.commissioning_plan import CommissioningIntent, new_plan
from htdt.project_lifecycle import ProjectLibrary
from htdt.project_setup_intent import (
    brief_from_commissioning_intent,
    materialize_commissioning_brief,
    materialize_template_brief,
)

NOW = '2026-09-24T00:00:00+00:00'


class _StandardsSource:
    def __init__(self, profiles=()):
        self._profiles = {
            (profile.profile_id, profile.version): profile
            for profile in profiles
        }

    def get_profile(self, profile_id: str, version: str):
        return self._profiles.get((profile_id, version))


def _intent() -> CommissioningIntent:
    return CommissioningIntent(
        is_new_project=True,
        has_existing_room=True,
        audio_only=True,
        rew_available=True,
        wants_hybrid_prediction=True,
        planned_speaker_count=5,
        goals=('迫力', '定位'),
    )


def test_wizard_intent_becomes_canonical_brief(tmp_path: Path) -> None:
    """Wizard answers converge into a ProjectDesignBrief — goals stay
    qualitative free_text, never rewritten into invented numbers."""

    brief = brief_from_commissioning_intent(
        _intent(), document_id='doc-a', title='doc-a', plan_id='plan-1'
    )
    assert brief.document_id == 'doc-a'
    assert 'audio_only' in brief.use_labels
    assert 'existing_room_authority' in brief.use_labels
    labels = {goal.label for goal in brief.goal_refs}
    assert {'迫力', '定位'} <= labels
    # Qualitative goals never silently become numeric constraints.
    assert all(goal.kind == 'free_text' for goal in brief.goal_refs)
    assert all(goal.ref_id is None for goal in brief.goal_refs)
    assert 'commissioning_wizard' in brief.provenance


def test_materialize_is_resume_safe(tmp_path: Path) -> None:
    """Resuming an old plan never overwrites a newer project brief."""

    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    brief_repository = CadDesignBriefRepository(repository)
    plan = new_plan('doc-a', 'doc-a', _intent())

    brief = materialize_commissioning_brief(brief_repository, plan)
    assert brief is not None
    assert brief_repository.latest_brief('doc-a') is not None

    # A second materialization is a no-op, not a silent supersede.
    assert materialize_commissioning_brief(brief_repository, plan) is None
    assert brief_repository.latest_brief('doc-a').brief_id == brief.brief_id


def test_template_creation_materializes_project_brief(tmp_path: Path) -> None:
    """create-from-template seeds a project-bound brief from template
    defaults and carries the measurement intent as a pending pattern."""

    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    library = ProjectLibrary(tmp_path / 'cad.sqlite3')
    template_repository = CadProjectTemplateRepository(scene_repository)
    brief_repository = CadDesignBriefRepository(scene_repository)
    template = theater_5_1_4_template()

    document_id, instantiation = create_project_from_template(
        scene_repository,
        template,
        library=library,
        display_name='My Theater',
        created_at_utc=NOW,
        instantiation_repository=template_repository,
        standards_repository=_StandardsSource(
            (dolby_atmos_home_5_1_2_profile(),)
        ),
        design_brief_repository=brief_repository,
    )

    # Pending typed pattern, not a fabricated MeasurementPlan.
    assert instantiation.pending_measurement_spec is not None
    assert (
        instantiation.pending_measurement_spec.measurement_point_count == 3
    )

    brief = brief_repository.latest_brief(document_id)
    assert brief is not None
    assert 'dedicated_theater' in brief.use_labels
    # The pinned Dolby baseline lands as honest informational intent with
    # its resolution state — never an unprovable typed binding; the exact
    # pin lives on the instantiation record.
    baseline = [goal for goal in brief.goal_refs if goal.kind == 'free_text']
    assert any(
        'standards_profile:dolby-atmos-home-5.1.2-layout' in goal.label
        and '解決済み' in goal.label
        for goal in baseline
    )
    assert all(goal.ref_id is None for goal in brief.goal_refs)
    assert any('template:' in p for p in brief.provenance)


def test_template_brief_never_overwrites_existing(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    brief_repository = CadDesignBriefRepository(repository)
    template = theater_5_1_4_template()
    repository.save(make_empty_scene('doc-a'), parent_revision_id=None)
    existing = materialize_commissioning_brief(
        brief_repository, new_plan('doc-a', 'doc-a', _intent())
    )
    instantiation = ProjectTemplateInstantiation.model_construct(
        instantiation_id='inst-1',
        document_id='doc-a',
        project_id=None,
        template_id=template.template_id,
        template_version=template.version,
        template_sha256=template.template_sha256,
        unresolved_refs=(),
        pending_measurement_spec=None,
        created_at_utc=NOW,
        instantiation_sha256='0' * 64,
    )
    # Hash fixup not needed for the guard test: the function returns None
    # before touching the repository.
    result = materialize_template_brief(
        brief_repository,
        template,
        document_id='doc-a',
        instantiation=instantiation,
    )
    assert result is None
    assert (
        brief_repository.latest_brief('doc-a').brief_id == existing.brief_id
    )


def test_wizard_brief_merges_template_defaults(tmp_path: Path) -> None:
    """On the wizard's template path the merged intent+template brief is
    the single write — wizard goals are not lost."""

    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    brief_repository = CadDesignBriefRepository(repository)
    template = theater_5_1_4_template()
    instantiation = ProjectTemplateInstantiation.model_construct(
        instantiation_id='inst-1',
        document_id='doc-a',
        project_id=None,
        template_id=template.template_id,
        template_version=template.version,
        template_sha256=template.template_sha256,
        unresolved_refs=(),
        pending_measurement_spec=template.measurement_spec,
        created_at_utc=NOW,
        instantiation_sha256='0' * 64,
    )
    plan = new_plan('doc-a', 'doc-a', _intent())
    brief = materialize_commissioning_brief(
        brief_repository,
        plan,
        template=template,
        instantiation=instantiation,
    )
    assert brief is not None
    # Wizard goals survive alongside the template defaults.
    labels = {goal.label for goal in brief.goal_refs}
    assert {'迫力', '定位'} <= labels
    assert 'dedicated_theater' in brief.use_labels
    assert 'audio_only' in brief.use_labels
    assert any('template:' in p for p in brief.provenance)
    assert 'commissioning_wizard' in brief.provenance
