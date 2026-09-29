from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from htdt.cad_display_labels import (
    format_versioned_label,
    named_or_saved_label,
    revision_display_label,
    saved_label,
    spec_display_label,
    variant_display_label,
)
from htdt.cad_measurement_runner_repository import (
    CadMeasurementRunnerRepository,
)
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurement_quality_repository import (
    CadMeasurementQualityRepository,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_measurement_runner import build_runner_plan


@dataclass(frozen=True)
class _RevisionStub:
    revision_id: str
    created_at_utc: str


@dataclass(frozen=True)
class _LabelStub:
    label: str


@dataclass(frozen=True)
class _VariantStub:
    name: str
    created_at_utc: str


def _scene(document_id: str) -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='Front Left',
                speaker_role='FL',
                position=Position3(x_m=1.35, y_m=0.75, z_m=1.05),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
            ),
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )


def _runner(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        _scene('doc-runner'), parent_revision_id=None
    ).revision
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(measurement_repository)
    runner = CadMeasurementRunnerRepository(
        scene_repository, measurement_repository, quality_repository
    )
    return scene_repository, revision, runner


def _plan(revision, document_id: str):
    return build_runner_plan(
        document_id=document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        sources=(('front_left', ('speaker-fl',)),),
        target_entity_ids=('point-mlp',),
        repeat_count=1,
    )


def test_saved_label_formats_utc_timestamp_as_japanese_save_label() -> None:
    assert (
        saved_label('2026-09-24T18:42:31+00:00')
        == '2026年9月24日 18:42 UTC の保存'
    )
    assert (
        saved_label('2026-09-24T18:42:31Z')
        == '2026年9月24日 18:42 UTC の保存'
    )


def test_saved_label_falls_back_to_raw_timestamp_not_a_hash() -> None:
    assert saved_label('not-a-timestamp') == 'not-a-timestamp'
    assert saved_label('') == ''


def test_named_or_saved_label_prefers_the_human_name() -> None:
    assert (
        named_or_saved_label('メイン案', '2026-09-24T18:42:00+00:00')
        == 'メイン案'
    )
    assert (
        named_or_saved_label(None, '2026-09-24T18:42:00+00:00')
        == '2026年9月24日 18:42 UTC の保存'
    )
    assert (
        named_or_saved_label('', '2026-09-24T18:42:00+00:00')
        == '2026年9月24日 18:42 UTC の保存'
    )


def test_revision_display_label_prefers_user_label_then_generated() -> None:
    revision = _RevisionStub('rev-abc123def456', '2026-09-24T18:42:00+00:00')
    labels = {'rev-abc123def456': _LabelStub('部屋 確定版')}
    assert revision_display_label(revision, labels) == '部屋 確定版'
    assert revision_display_label(revision, {}) == '2026年9月24日 18:42 UTC の保存'
    assert revision_display_label(revision, None) == '2026年9月24日 18:42 UTC の保存'
    empty_label = {'rev-abc123def456': _LabelStub('')}
    assert (
        revision_display_label(revision, empty_label)
        == '2026年9月24日 18:42 UTC の保存'
    )


def test_variant_and_spec_labels_never_leak_ids() -> None:
    variant = _VariantStub('5.1ch 案', '2026-09-24T18:42:00+00:00')
    assert variant_display_label(variant) == '5.1ch 案 · 2026年9月24日 18:42 UTC の保存'
    assert (
        spec_display_label(None, '2026-09-24T18:42:00+00:00')
        == '2026年9月24日 18:42 UTC の保存'
    )
    assert (
        spec_display_label('標準探索', '2026-09-24T18:42:00+00:00')
        == '標準探索'
    )
    assert (
        format_versioned_label('v', '3', '2026-09-24T18:42:00+00:00')
        == 'v 3 · 2026年9月24日 18:42 UTC の保存'
    )


def test_runner_plan_created_at_utc_scopes_to_document(
    tmp_path: Path,
) -> None:
    scene_repository, revision, runner = _runner(tmp_path)
    plan_a = _plan(revision, revision.document_id)
    runner.save_plan(plan_a)

    other_revision = scene_repository.save(
        _scene('doc-other'), parent_revision_id=None
    ).revision
    plan_b = _plan(other_revision, 'doc-other')
    runner.save_plan(plan_b)

    created = runner.list_plan_created_at_utc(revision.document_id)
    assert set(created) == {plan_a.plan_id}
    assert 'T' in created[plan_a.plan_id]

    other = runner.list_plan_created_at_utc('doc-other')
    assert set(other) == {plan_b.plan_id}
