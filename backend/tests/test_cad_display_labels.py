from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import get_args

from htdt.cad_display_labels import (
    acquisition_source_kind_label,
    format_versioned_label,
    measurement_claim_label,
    measurement_reason_label,
    named_or_saved_label,
    revision_display_label,
    saved_label,
    spec_display_label,
    variant_display_label,
)
from htdt.cad_measurement_quality import (
    AcquisitionContextSourceKind,
    MeasurementCapabilityClaim,
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
    labels = {'rev-abc123def456': _LabelStub('部屋確定版')}
    assert revision_display_label(revision, labels) == '部屋確定版'
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


def test_measurement_claim_labels_cover_the_full_claim_vocabulary() -> None:
    """Every emitted MeasurementCapabilityClaim has a JA display label."""
    for claim in get_args(MeasurementCapabilityClaim):
        label = measurement_claim_label(claim)
        assert label != claim, claim
        assert not label.isascii(), claim
    # Unknown values (user-typed observables) fall back to the raw code.
    assert measurement_claim_label('custom_thing') == 'custom_thing'


def test_acquisition_source_kind_labels_cover_the_vocabulary() -> None:
    for kind in get_args(AcquisitionContextSourceKind):
        label = acquisition_source_kind_label(kind)
        assert label != kind, kind
        assert not label.isascii(), kind
    assert acquisition_source_kind_label('other') == 'other'


_EMITTED_MEASUREMENT_REASONS = (
    # CadMeasurementQualityCheck reasons (_derive_checks).
    'clipping metadata is unavailable',
    'acquisition metadata reports clipping',
    'acquisition metadata reports no clipping',
    'explicit SNR evidence is unavailable',
    'SNR evidence exists but the profile has no SNR threshold',
    'usable frequency band evidence is unavailable',
    'explicit usable frequency band is recorded',
    'usable frequency band covers the profile requirement',
    'usable frequency band does not cover the profile requirement',
    'acquisition metadata reports an invalid timing reference',
    'common timing reference evidence is unavailable',
    'timing reference metadata is incomplete',
    'timing reference identity, clock, sample rate and delay correction are '
    'recorded',
    'polarity evidence reports reversed polarity',
    'polarity evidence or confidence is unavailable',
    'polarity evidence exists but the profile has no confidence threshold',
    'polarity confidence is below the profile confidence threshold',
    'polarity evidence meets the profile confidence threshold',
    'no impulse-response evidence is bound to this report',
    'IR truncation evidence is unavailable',
    'IR evidence is reported as truncated',
    'IR window bounds are unavailable',
    'IR window bounds are recorded and truncation is not reported',
    'calibration-file provenance is unavailable',
    'calibration-file provenance is incomplete',
    'applied calibration file does not match the expected calibration file',
    'applied calibration file matches the expected calibration file',
    'repeatability evidence requires at least two measurements and an '
    'explicit metric',
    'repeatability evidence exists but the profile has no repeatability '
    'threshold',
    # CadMeasurementCapability / calibration-scope reasons.
    'dataset contains phase explicitly marked valid',
    'dataset explicitly has no phase evidence',
    'phase evidence is not verified',
    'no measurement quality report is bound to this dataset',
    'immutable frequency/level dataset is present',
    'authoritative AcquisitionContext binding is unavailable',
    'arrival-time claims require impulse-response evidence',
    'decay claims require impulse-response evidence',
    'dataset level-reference authority is unavailable',
    'bound acoustic level calibration is unavailable',
    'bound acoustic level calibration does not match the dataset '
    'level-reference pin',
    'measurement-scoped calibration is pinned to this dataset',
    'session-scoped calibration is bound by the authoritative acquisition '
    'context',
    'session-scoped calibration requires an authoritative acquisition '
    'context',
    'instrument-scoped calibration has no instrument identity',
    'instrument-scoped calibration requires an authoritative acquisition '
    'context',
    'instrument-scoped calibration requires microphone instrument identity '
    'in the acquisition context',
    'instrument-scoped calibration instrument does not match the '
    'acquisition microphone',
    'instrument-scoped calibration matches the acquisition microphone',
    'calibration validity scope is unestablished',
    'noise-floor evidence is unavailable',
    'claim was not evaluated by this historical quality report',
    'usable frequency band is not established',
)


def test_measurement_reason_label_glosses_every_emitted_sentence() -> None:
    """All static emitted reason strings render JA (REV25-TAIL)."""
    for reason in _EMITTED_MEASUREMENT_REASONS:
        label = measurement_reason_label(reason)
        assert label != reason, reason
        assert not label.isascii(), reason


def test_measurement_reason_label_translates_parameterized_sentences() -> None:
    cases = {
        'SNR 12.000 dB is below profile minimum 20.000 dB':
            'SNR 12.000 dB がプロファイル下限 20.000 dB を下回っています',
        'SNR 30.000 dB meets profile minimum 20.000 dB':
            'SNR 30.000 dB がプロファイル下限 20.000 dB を満たしています',
        'repeatability RMS 2.500 dB exceeds profile maximum 1.000 dB':
            '繰り返し精度RMS 2.500 dB がプロファイル上限 1.000 dB '
            'を超えています',
        'repeatability RMS 0.500 dB meets profile maximum 1.000 dB':
            '繰り返し精度RMS 0.500 dB がプロファイル上限 1.000 dB '
            'を満たしています',
        'usable frequency band 40-8000 Hz does not cover required band '
        '20-12000 Hz':
            '使用可能帯域 40-8000 Hz が要求帯域 20-12000 Hz '
            'をカバーしていません',
        'dataset level reference declares relative semantics, not absolute '
        'SPL':
            'データセットのレベル基準は relative セマンティクスを宣言しており'
            '絶対SPLではありません',
        'calibration method rew_mic_cal does not support absolute SPL':
            '校正方式 rew_mic_cal は絶対SPLに対応していません',
    }
    for reason, expected in cases.items():
        assert measurement_reason_label(reason) == expected


def test_measurement_reason_label_falls_back_to_raw_text() -> None:
    assert measurement_reason_label('some new reason') == 'some new reason'
