from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import get_args

from htdt.cad_display_labels import (
    acquisition_source_kind_label,
    calibration_reason_label,
    entity_kind_label,
    environment_source_kind_label,
    format_versioned_label,
    limiter_state_label,
    measurement_claim_label,
    measurement_reason_label,
    mode_class_label,
    named_or_saved_label,
    objective_state_label,
    revision_display_label,
    saved_label,
    solver_reason_label,
    spec_display_label,
    state_token_label,
    variant_display_label,
    why_stale_reason_label,
)
from htdt.cad_measurement_quality import (
    AcquisitionContextSourceKind,
    MeasurementCapabilityClaim,
)
from htdt.optimization_objectives import ObjectiveState
from htdt.cad_amplifier_headroom import LimiterState
from htdt.cad_prediction_matrix import MatrixCellState, MatrixRunState
from htdt.cad_prediction_models import PredictionModeClass
from htdt.cad_scene import EntityKind
from htdt.cad_acoustic_environment import EnvironmentSourceKind
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


# ---------------------------------------------------------------------------
# REV26-STRINGS — solver/evaluation reason & state-token display maps.
# ---------------------------------------------------------------------------

_EMITTED_CALCULATION_REASONS = (
    # evaluate_calibration_support (cad_calibration.py) — static sentences.
    'all-pass correction is unsupported in calibration-plan-1 because '
    'coherent inter-channel phase correction authority is not established',
    'device filter-count capability is unknown',
    'device maximum boost capability is unknown',
    'device maximum cut capability is unknown',
)

_EMITTED_SOLVER_REASONS = (
    # cad_amplifier_headroom
    'speaker electrical load authority is missing',
    'speaker load is nominal-only; nominal impedance does not establish '
    'actual amplifier load capability',
    'requested frequency band is outside speaker load reference domain',
    'speaker reference load is outside amplifier evidenced load domain',
    'requested frequency band is outside amplifier capability domain',
    'playback weighting differs from amplifier capability authority',
    'single-channel amplifier capability cannot be promoted to simultaneous '
    'multi-channel performance',
    'simultaneous channel-count condition differs from capability authority',
    'voltage/power comparison requires exact resistive load semantics',
    'electrical capability unavailable',
    'speaker sensitivity/reference level is not evidenced',
    'speaker sensitivity has no valid frequency domain',
    'requested frequency band is outside speaker sensitivity domain',
    'speaker sensitivity has no weighting authority for requested weighting',
    'speaker sensitivity weighting differs from playback scenario',
    'amplifier capability cannot be converted to speaker sensitivity '
    'reference',
    'speaker acoustic SPL capability is not evidenced',
    'speaker SPL capability has no weighting provenance for requested '
    'weighting',
    'amplifier-constrained acoustic ceiling unavailable',
    # cad_direct_level
    'equipment SPL capability is not evidenced',
    'SPL capability has no weighting provenance for requested weighting',
    'equipment sensitivity/reference level is not evidenced',
    'sensitivity has no weighting authority for requested weighting',
    'sensitivity weighting does not match playback scenario',
    'playback input quantity differs from sensitivity reference and no '
    'voltage/power conversion authority is available',
    'usable-output profile binding is advisory (id-only) and cannot drive '
    'O100D headroom (#1026)',
    'usable-output profile records no measurement distance — no listener '
    'transfer can be established',
    'direct level is missing',
    'direct level is unsupported',
    'missing receiver',
    'receiver seat entity is missing from SystemVariant scene',
    'receiver population member is not a seat entity',
    'seat has no explicit scene acoustic reference position',
    'source and receiver acoustic reference positions coincide',
    'seat distance lies outside the declared radial validity domain of the '
    'distance authority',
    'seat-to-seat direct-level spread requires at least two seats',
    # cad_usable_output
    'no usable-output basis available',
    'distortion/compression policy: measured samples at or below the '
    'target already exceed the criterion',
    'distortion/compression policy: no qualifying measured sample — the '
    'policy-qualified ceiling is unestablished',
    'distortion/compression policy',
    'declared SPL capability at its declared reference',
    'amplifier margin (electrical, at operating point)',
    'no listener transfer authority — a source-reference level is never '
    'directly compared to a listener/seat target',
    'same-reference claim but the profile records no measurement distance',
    # cad_prediction_matrix
    'no provider run bound for this matrix source',
    'provider authority does not match matrix spec',
    'provider observable unsupported',
    'provider run does not cover receiver',
    'provider response frequency grid does not match the matrix observable '
    'contract',
    'scene content changed since the matrix ran',
    'acoustic scene snapshot changed since the matrix ran',
    'no coherent-compatible sources were collected',
    'sources do not share a common frequency grid',
    'at least one source has no declared source normalization',
    # cad_prediction_provider capability fallback
    'observable is outside the bounded R170A provider contract',
    # cad_coverage
    'coverage aggregate unavailable because at least one required seat is '
    'unsupported; partial population evaluation is forbidden',
    'unsupported directivity evaluation',
    'required receiver seat entity is missing from SystemVariant scene',
    # cad_topology_comparison
    'comparison requires explicit ObjectiveDefinition authority',
    'objective evidence source model id/version differs from the exact '
    'ObjectiveDefinition comparison model',
    'required objective is absent from ObjectiveVector',
    'required objective state is missing',
    'required objective state is unsupported',
    'candidate has no VariantEvaluationBundle',
    'required objective evidence uses incompatible declared '
    'evaluator/model/fidelity authority',
)


def test_calibration_reason_label_glosses_every_emitted_sentence() -> None:
    """All static calibration-plan reason strings render JA (REV26-STRINGS)."""
    for reason in _EMITTED_CALCULATION_REASONS:
        label = calibration_reason_label(reason)
        assert label != reason, reason
        assert not label.isascii(), reason


def test_calibration_reason_label_translates_parameterized_sentences() -> None:
    cases = {
        'device does not support sample rate 48000 Hz':
            '機器がサンプルレート 48000 Hz に対応していません',
        'channel ch-1 maps to unsupported physical output out-9':
            'チャンネル ch-1 が未対応の物理出力 out-9 にマッピングされています',
        'channel ch-1 filter count 5 exceeds device maximum 4':
            'チャンネル ch-1 のフィルター数 5 が機器上限 4 を超えています',
        'filter peq-3 type high_shelf is unsupported by the device':
            'フィルター peq-3 の種別 high_shelf が機器で未対応です',
        'filter peq-3 boost 12.0 dB exceeds plan maximum 6.0 dB':
            'フィルター peq-3 のブースト 12.0 dB がプラン上限 6.0 dB '
            'を超えています',
        'filter peq-3 cut 15.0 dB exceeds plan maximum 9.0 dB':
            'フィルター peq-3 のカット 15.0 dB がプラン下限 9.0 dB '
            'を超えています',
        'filter peq-3 exceeds device maximum boost':
            'フィルター peq-3 が機器最大ブーストを超えています',
        'filter peq-3 exceeds device maximum cut':
            'フィルター peq-3 が機器最大カットを超えています',
        'channel ch-1 gain is below device minimum':
            'チャンネル ch-1 のゲインが機器最小値を下回っています',
        'channel ch-1 gain is above device maximum':
            'チャンネル ch-1 のゲインが機器最大値を超えています',
        'channel ch-1 delay exceeds device maximum':
            'チャンネル ch-1 の遅延が機器最大値を超えています',
        'channel ch-1 crossover order 3 is unsupported or unknown':
            'チャンネル ch-1 のクロスオーバー次数 3 が未対応または不明です',
    }
    for reason, expected in cases.items():
        assert calibration_reason_label(reason) == expected


def test_calibration_reason_label_translates_capability_composites() -> None:
    composite = (
        'absolute delay requires established common timing; '
        'usable frequency band is not established'
    )
    assert calibration_reason_label(composite) == (
        '絶対遅延には確立された共通タイミングが必要です; '
        '使用可能帯域が確立されていません'
    )
    composite = (
        'polarity change requires polarity authority; '
        'polarity evidence or confidence is unavailable'
    )
    assert calibration_reason_label(composite) == (
        '極性変更には極性権威が必要です; '
        '極性証拠または信頼度がありません'
    )
    composite = (
        'common_timing: BLOCKED: common timing reference evidence is '
        'unavailable; usable frequency band is not established'
    )
    assert calibration_reason_label(composite) == (
        '共通タイミング: 不可: 共通タイミング基準の証拠がありません; '
        '使用可能帯域が確立されていません'
    )
    composite = (
        'absolute_spl: UNKNOWN: '
        'dataset level-reference authority is unavailable'
    )
    assert calibration_reason_label(composite) == (
        '絶対SPL: 不明: データセットのレベル基準権威がありません'
    )


def test_calibration_reason_label_falls_back_to_raw_text() -> None:
    assert calibration_reason_label('some new reason') == 'some new reason'


def test_solver_reason_label_glosses_every_emitted_sentence() -> None:
    """All static solver/evaluation reason strings render JA."""
    for reason in _EMITTED_SOLVER_REASONS:
        label = solver_reason_label(reason)
        assert label != reason, reason
        assert not label.isascii(), reason


def test_solver_reason_label_translates_parameterized_sentences() -> None:
    cases = {
        'continuous amplifier capability is not evidenced':
            '連続アンプ能力の証拠がありません',
        'peak amplifier duration is not evidenced':
            'ピークアンプ持続時間の証拠がありません',
        'continuous duration differs from amplifier capability authority':
            '連続持続時間がアンプ能力権威と異なります',
        'speaker continuous SPL capability is not evidenced':
            'スピーカー連続SPL能力の証拠がありません',
        'speaker peak SPL capability has no duration authority':
            'スピーカーピークSPL能力に持続時間権威がありません',
        'requested frequency band is outside speaker peak SPL capability '
        'domain':
            '要求周波数帯がスピーカーピークSPL能力域の外です',
        'peak duration differs from speaker SPL capability authority':
            'ピーク持続時間がスピーカーSPL能力権威と異なります',
        'continuous SPL capability is not evidenced':
            '連続SPL能力の証拠がありません',
        'peak SPL capability has no duration authority':
            'ピークSPL能力に持続時間権威がありません',
        'peak duration differs from evidenced equipment capability duration':
            'ピーク持続時間が実証済み機器能力持続時間と異なります',
        'sensitivity has no explicit valid frequency domain':
            '感度に明示的な有効周波数域がありません',
        'requested frequency band is outside continuous SPL capability '
        'valid domain':
            '要求周波数帯が連続SPL能力有効域の外です',
        'worst-seat direct level unavailable because at least one seat is '
        'unsupported':
            '最悪座席の直接音レベルが利用できません：'
            '未対応の座席が少なくとも1つあります',
        'worst-seat continuous headroom unavailable because at least one '
        'required seat is missing evidence':
            '最悪座席の連続ヘッドルームが利用できません：'
            '証拠のない必須座席が少なくとも1つあります',
        'weighted peak headroom unavailable because required weights do '
        'not normalize':
            '重み付きピークヘッドルームが利用できません：'
            '必須重みが正規化できません',
        'same-reference claim but listener distance 2.5 m differs from '
        'profile reference 1.0 m':
            '同一基準の主張ですがリスナー距離 2.5 m がプロファイル基準 '
            '1.0 m と異なります',
        "same-reference claim but listener axis 'off_axis' differs from "
        "profile reference axis 'on_axis'":
            "同一基準の主張ですがリスナー軸 'off_axis' がプロファイル基準軸 "
            "'on_axis' と異なります",
        "same-reference claim but environments differ ('studio' vs "
        "'theater')":
            "同一基準の主張ですが環境が異なります（'studio' vs 'theater'）",
        "profile is measured on 'on_axis' but the listener sits on "
        "'off_axis' and the transfer carries no directivity authority":
            "プロファイルは 'on_axis' で測定されリスナーは 'off_axis' "
            'に位置しますが伝達に指向性権威がありません',
        'scalar_declared on same_reference: 87.5 dB SPL at listener vs '
        '105.0 dB SPL target':
            'scalar_declared（基準 same_reference）：リスナー 87.5 dB SPL '
            '対 目標 105.0 dB SPL',
        'amplifier margin 3.2 dB is electrical headroom, not acoustic '
        'listener headroom':
            'アンプ余量 3.2 dB は電気的ヘッドルームで音響リスナー'
            'ヘッドルームではありません',
        'matrix source binding changed for: src-a, src-b':
            '行列ソース結合が変更されました: src-a, src-b',
        'matrix receiver binding cannot be re-verified for: seat-1':
            '行列受信結合を再検証できません: seat-1',
        'source src-a has no bound provider run':
            'ソース src-a に結び付けられたプロバイダー実行がありません',
        'source src-a does not cover receiver seat-1':
            'ソース src-a が受信点 seat-1 をカバーしていません',
        'source src-a receiver seat-1 carries magnitude-only data; it '
        'cannot enter a coherent sum':
            'ソース src-a の受信点 seat-1 は振幅のみのデータでコヒーレント和'
            'に入りません',
        'sources declare different phasor conventions: exp_jwt,exp_-jwt':
            'ソース間でフェーザー規約が異なります: exp_jwt,exp_-jwt',
        'seat coverage unavailable because at least one requested frequency '
        'is unsupported: unsupported directivity evaluation':
            '少なくとも1つの要求周波数が未対応のため座席カバレッジが'
            '利用できません: unsupported directivity evaluation',
        'objective_id differs from exact comparison definition':
            'objective_id が正確な比較定義と異なります',
    }
    for reason, expected in cases.items():
        assert solver_reason_label(reason) == expected


def test_solver_reason_label_translates_basis_composites() -> None:
    composite = (
        'scalar_declared: no listener transfer authority — a '
        'source-reference level is never directly compared to a '
        'listener/seat target'
    )
    assert solver_reason_label(composite) == (
        '宣言値: リスナー伝達権威がなく、ソース基準レベルはリスナー/座席目標'
        'と直接比較されません'
    )


def test_solver_reason_label_falls_back_to_raw_text() -> None:
    assert solver_reason_label('some new reason') == 'some new reason'


def test_state_token_labels_cover_the_rendered_vocabularies() -> None:
    for token in get_args(MatrixCellState) + get_args(MatrixRunState):
        label = state_token_label(token)
        assert label != token, token
        assert not label.isascii(), token
    for token in ('CURRENT', 'STALE', 'UNSUPPORTED'):
        label = state_token_label(token)
        assert label != token, token
        assert not label.isascii(), token
    assert state_token_label('SOME_NEW_TOKEN') == 'SOME_NEW_TOKEN'


def test_objective_and_limiter_state_labels_cover_the_vocabularies() -> None:
    for state in get_args(ObjectiveState):
        label = objective_state_label(state)
        assert label != state, state
        assert not label.isascii(), state
    for state in get_args(LimiterState):
        label = limiter_state_label(state)
        assert label != state, state
        assert not label.isascii(), state
    assert objective_state_label('other') == 'other'
    assert limiter_state_label('other') == 'other'


def test_mode_class_and_entity_and_environment_kind_labels() -> None:
    for value in get_args(PredictionModeClass):
        label = mode_class_label(value)
        assert label != value, value
        assert not label.isascii(), value
    for value in get_args(EntityKind):
        label = entity_kind_label(value)
        assert label != value, value
        assert not label.isascii(), value
    for value in get_args(EnvironmentSourceKind):
        label = environment_source_kind_label(value)
        assert label != value, value
        assert not label.isascii(), value
    assert mode_class_label('other') == 'other'
    assert entity_kind_label('other') == 'other'
    assert environment_source_kind_label('other') == 'other'


def test_why_stale_reason_label_scaffolds_edge_composites() -> None:
    # JA node stale reasons pass through untouched.
    ja = '測定時の部屋リビジョン内容と現在のヘッドが一致しません'
    assert why_stale_reason_label(ja) == ja
    assert (
        why_stale_reason_label(
            'stale_because: 測定 — room geometry or boundary material changed'
        )
        == '古い原因: 測定 — room geometry or boundary material changed'
    )
    assert (
        why_stale_reason_label('invalidates: プラン — GPU unavailable')
        == '無効化: プラン — GPU unavailable'
    )
    assert (
        why_stale_reason_label('invalidated by: プラン')
        == '無効化元: プラン'
    )
    assert why_stale_reason_label('unmapped') == 'unmapped'
