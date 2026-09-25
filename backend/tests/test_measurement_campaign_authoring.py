"""Measurement campaign authoring (issue #925).

The runner page used to create only all-speakers x all-targets plans.
These tests pin the authored contract: explicit source/target subsets,
purposes, cell preview, #543 pattern seeding, human plan labels, and
exact SystemVariant/validation plan projection — all below the UI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurement_quality_repository import (
    CadMeasurementQualityRepository,
)
from htdt.cad_measurement_runner import (
    RunnerCellSpec,
    build_runner_plan_from_cells,
)
from htdt.cad_measurement_target_pattern import (
    CadTargetPatternRepository,
    TargetPatternOffset,
    build_target_pattern,
    materialize_target_pattern,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Position3,
    SceneEntity,
    Size3,
    make_f1_scene,
)
from htdt.measurement_workflow import (
    MeasurementWorkflowController,
    MeasurementWorkflowError,
)

from test_cad_system_variant_measurement_campaign import (  # noqa: E402
    _fixture as _variant_fixture,
    _plan_and_campaign as _variant_plan_and_campaign,
)


def _rig(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(
        measurement_repository
    )
    controller = MeasurementWorkflowController(
        scene_repository,
        revision.document_id,
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
    )
    return scene_repository, revision, controller


def _two_sub_scene(tmp_path: Path):
    """F1 scene plus two physical subs sharing one logical sub role."""
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = make_f1_scene().model_copy(
        update={
            'entities': (
                *make_f1_scene().entities,
                SceneEntity(
                    entity_id='sub-a',
                    kind='speaker',
                    name='Sub A',
                    speaker_role='SUB',
                    position=Position3(x_m=1.0, y_m=1.0, z_m=0.3),
                    size_m=Size3(x_m=0.4, y_m=0.4, z_m=0.4),
                ),
                SceneEntity(
                    entity_id='sub-b',
                    kind='speaker',
                    name='Sub B',
                    speaker_role='SUB',
                    position=Position3(x_m=5.0, y_m=1.0, z_m=0.3),
                    size_m=Size3(x_m=0.4, y_m=0.4, z_m=0.4),
                ),
            )
        }
    )
    revision = scene_repository.save(
        document, parent_revision_id=None
    ).revision
    controller = MeasurementWorkflowController(
        scene_repository,
        revision.document_id,
        measurement_repository=CadMeasurementRepository(scene_repository),
        quality_repository=CadMeasurementQualityRepository(
            CadMeasurementRepository(scene_repository)
        ),
    )
    return scene_repository, revision, controller


class TestSourceOptions:
    def test_each_speaker_is_one_option_and_shared_roles_group(
        self, tmp_path: Path
    ):
        _, _, controller = _two_sub_scene(tmp_path)
        options = controller.runner_source_options()
        singles = [o for o in options if not o.grouped]
        grouped = [o for o in options if o.grouped]
        assert {o.speaker_entity_ids[0] for o in singles} == {
            'speaker-fl',
            'speaker-c',
            'speaker-fr',
            'sub-a',
            'sub-b',
        }
        # Two physical subs share the logical 'subwoofer' role — one
        # grouped option, never two competing 'LFE' channels (#925).
        assert len(grouped) == 1
        assert grouped[0].channel_role == 'subwoofer'
        assert set(grouped[0].speaker_entity_ids) == {'sub-a', 'sub-b'}

    def test_distinct_roles_yield_no_grouped_option(self, tmp_path: Path):
        _, _, controller = _rig(tmp_path)
        assert all(
            not option.grouped for option in controller.runner_source_options()
        )


class TestSubsetAuthoring:
    def test_explicit_subset_builds_exact_cells(self, tmp_path: Path):
        _, _, controller = _rig(tmp_path)
        plan = controller.create_runner_plan(
            sources=(('front_left', ('speaker-fl',)),),
            target_entity_ids=('point-mlp',),
            repeat_count=2,
            purposes=('diagnostic',),
        )
        assert len(plan.cells) == 2
        for index, cell in enumerate(plan.cells):
            assert cell.cell_index == index
            assert cell.channel_role == 'front_left'
            assert cell.source_speaker_ids == ('speaker-fl',)
            assert cell.target_entity_id == 'point-mlp'
            assert cell.repeat_index == index
            assert cell.purpose == 'diagnostic'
        assert controller.runner_plans() == (plan,)

    def test_grouped_source_keeps_the_radiator_set(self, tmp_path: Path):
        _, _, controller = _two_sub_scene(tmp_path)
        plan = controller.create_runner_plan(
            sources=(('subwoofer', ('sub-a', 'sub-b')),),
            target_entity_ids=('point-mlp',),
        )
        assert len(plan.cells) == 1
        assert plan.cells[0].channel_role == 'subwoofer'
        assert set(plan.cells[0].source_speaker_ids) == {'sub-a', 'sub-b'}

    def test_default_selection_is_the_all_x_all_preset(self, tmp_path: Path):
        _, _, controller = _rig(tmp_path)
        plan = controller.create_runner_plan(repeat_count=2)
        assert len(plan.cells) == 3 * 1 * 2

    def test_subset_of_targets_skips_irrelevant_acquisition(
        self, tmp_path: Path
    ):
        scene_repository, _, controller = _two_sub_scene(tmp_path)
        plan = controller.create_runner_plan(
            sources=(('subwoofer', ('sub-a', 'sub-b')),),
            target_entity_ids=('point-mlp',),
        )
        assert len(plan.cells) == 1
        assert plan.scene_revision_id == scene_repository.latest(
            plan.document_id
        ).revision_id

    def test_unknown_entities_and_empty_selection_rejected(
        self, tmp_path: Path
    ):
        _, _, controller = _rig(tmp_path)
        with pytest.raises(MeasurementWorkflowError):
            controller.create_runner_plan(
                sources=(('front_left', ('speaker-ghost',)),),
                target_entity_ids=('point-mlp',),
            )
        with pytest.raises(MeasurementWorkflowError):
            controller.create_runner_plan(
                sources=(('front_left', ('speaker-fl',)),),
                target_entity_ids=('furniture-ghost',),
            )
        with pytest.raises(MeasurementWorkflowError):
            controller.create_runner_plan(sources=(), target_entity_ids=())
        with pytest.raises(MeasurementWorkflowError):
            controller.create_runner_plan(
                sources=(('front_left', ('speaker-fl',)),),
                target_entity_ids=('furniture-left',),
            )


class TestPreviewAndLabels:
    def test_preview_reports_cells_without_persisting(self, tmp_path: Path):
        _, _, controller = _rig(tmp_path)
        preview = controller.preview_runner_plan(
            sources=(('front_left', ('speaker-fl',)),),
            target_entity_ids=('point-mlp',),
            repeat_count=3,
        )
        assert preview.cell_count == 3
        assert preview.source_count == 1
        assert preview.target_count == 1
        assert controller.runner_plans() == ()

    def test_summary_is_human_and_pinned_to_bound_revision(
        self, tmp_path: Path
    ):
        scene_repository, _, controller = _rig(tmp_path)
        plan = controller.create_runner_plan(
            sources=(('front_left', ('speaker-fl',)),),
            target_entity_ids=('point-mlp',),
            repeat_count=2,
        )
        summary = controller.runner_plan_summary(plan)
        assert plan.plan_id not in summary
        assert 'MLP' in summary
        assert 'Front Left' in summary
        assert '2回' in summary
        assert '2セル' in summary
        # Rename the entity on a NEW head revision: the label still
        # resolves against the revision the plan was bound to.
        document = scene_repository.latest(plan.document_id).document
        renamed = document.model_copy(
            update={
                'entities': tuple(
                    entity.model_copy(update={'name': 'Moved point'})
                    if entity.entity_id == 'point-mlp'
                    else entity
                    for entity in document.entities
                )
            }
        )
        scene_repository.save(
            renamed, parent_revision_id=plan.scene_revision_id
        )
        assert 'MLP' in controller.runner_plan_summary(plan)


class TestTargetPatternSeeding:
    def test_pattern_lists_and_seeds_exact_targets(self, tmp_path: Path):
        scene_repository, revision, controller = _rig(tmp_path)
        pattern_repository = CadTargetPatternRepository(scene_repository)
        pattern = build_target_pattern(
            scene_repository,
            document_id=revision.document_id,
            anchor_kind='measurement_point',
            anchor_entity_id='point-mlp',
            offsets=(
                TargetPatternOffset(
                    offset_index=0,
                    label='center',
                    offset_m=(0.0, 0.0, 0.0),
                ),
                TargetPatternOffset(
                    offset_index=1,
                    label='left',
                    offset_m=(-0.5, 0.0, 0.0),
                ),
            ),
            created_at='2026-09-25T00:00:00+00:00',
        )
        pattern_repository.save_pattern(pattern)
        _head, points = materialize_target_pattern(
            scene_repository,
            pattern_repository,
            pattern,
            created_at='2026-09-25T00:00:01+00:00',
        )
        assert controller.target_patterns() == (pattern,)
        entity_ids = controller.target_pattern_entity_ids(pattern.pattern_id)
        assert set(entity_ids) == {
            point.measurement_point_entity_id for point in points
        }
        plan = controller.create_runner_plan(
            sources=(('front_left', ('speaker-fl',)),),
            target_entity_ids=entity_ids,
        )
        assert {cell.target_entity_id for cell in plan.cells} == set(entity_ids)


class TestVariantPlanProjection:
    def test_exact_variant_plan_projects_without_all_x_all(
        self, tmp_path: Path
    ):
        fx = _variant_fixture(tmp_path)
        plan, _campaign, _registration = _variant_plan_and_campaign(fx)
        controller = MeasurementWorkflowController(
            fx['scene'],
            plan.document_id,
            measurement_repository=fx['measurements'],
            quality_repository=fx['quality'],
        )
        assert [p.plan_id for p in controller.variant_measurement_plans()] == [
            plan.plan_id
        ]
        runner_plan = controller.create_runner_plan_from_variant_plan(
            plan.plan_id
        )
        assert runner_plan.scene_revision_id == plan.as_built_revision_id
        assert runner_plan.scene_content_hash == plan.as_built_content_hash
        # Exactly the declared targets — no extra all×all cells.
        expected = sum(
            t.expected_measurement_count for t in plan.targets
        )
        assert len(runner_plan.cells) == expected
        cell = runner_plan.cells[0]
        target = plan.targets[0]
        assert cell.channel_role == target.channel_role
        assert cell.source_speaker_ids == target.source_entity_ids
        assert cell.target_entity_id == target.measurement_point_entity_id
        # Projecting twice reuses the persisted runner plan.
        again = controller.create_runner_plan_from_variant_plan(plan.plan_id)
        assert again.plan_id == runner_plan.plan_id
        assert len(controller.runner_plans()) == 1

    def test_unknown_variant_plan_rejected(self, tmp_path: Path):
        fx = _variant_fixture(tmp_path)
        controller = MeasurementWorkflowController(
            fx['scene'],
            'o100g-variant-campaign-fixture',
            measurement_repository=fx['measurements'],
            quality_repository=fx['quality'],
        )
        with pytest.raises(MeasurementWorkflowError):
            controller.create_runner_plan_from_variant_plan('missing')


class TestFromCellsBuilder:
    def test_explicit_cells_reindex_and_seal(self, tmp_path: Path):
        _, revision, _ = _rig(tmp_path)
        cells = (
            RunnerCellSpec(
                cell_index=0,
                channel_role='front_left',
                source_speaker_ids=('speaker-fl',),
                target_entity_id='point-mlp',
                repeat_index=0,
            ),
            RunnerCellSpec(
                cell_index=0,
                channel_role='subwoofer',
                source_speaker_ids=('speaker-fl', 'speaker-c'),
                target_entity_id='point-mlp',
                repeat_index=0,
                purpose='holdout',
            ),
        )
        plan = build_runner_plan_from_cells(
            document_id=revision.document_id,
            scene_revision_id=revision.revision_id,
            scene_content_hash=revision.content_hash,
            cells=cells,
        )
        assert [cell.cell_index for cell in plan.cells] == [0, 1]
        assert plan.cells[1].purpose == 'holdout'
        assert len(plan.plan_sha256) == 64
