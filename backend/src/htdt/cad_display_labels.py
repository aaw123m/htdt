"""Human-first entity labels for primary UI surfaces (#578).

Policy (from the #578 review contract):
1. primary label = human name/role + meaningful state/time;
2. secondary disambiguator = short date/version/status;
3. raw ids/hashes only under details/copy technical info unless no human
   identity exists;
4. unnamed revisions/plans receive generated human labels such as
   ``2026年9月24日 18:42 の保存`` instead of leaking hash/UUID fragments;
5. selection widgets retain exact ids as hidden data — never fuzzy-resolve
   a selection by display text (widgets keep ids in ``UserRole``).

These helpers are pure text functions: Qt widgets import them to build
item labels while storing the exact authority id in item data.
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Mapping, Protocol

from .localization import format_datetime


class _HasLabel(Protocol):
    label: str


def saved_label(created_at_utc: str) -> str:
    """Generated human label for an unnamed saved authority (#578).

    ``2026年9月24日 18:42 の保存`` — derived from the ISO-8601 UTC timestamp
    every persisted authority carries. Falls back to the raw timestamp
    string rather than fabricating an identity when it is not parseable.
    """

    try:
        parsed = datetime.fromisoformat(created_at_utc.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        return created_at_utc
    return f'{format_datetime(parsed)} の保存'


def named_or_saved_label(name: str | None, created_at_utc: str) -> str:
    """Primary human label: explicit name, else the generated saved label."""

    if name:
        return name
    return saved_label(created_at_utc)


def revision_display_label(
    revision: Any,
    labels: Mapping[str, _HasLabel] | None = None,
) -> str:
    """Human label for a SceneRevision/summary (#578).

    User-assigned history label wins (policy 1); unnamed revisions get the
    generated saved label (policy 4) — never a truncated ``revision_id``.
    ``revision`` is any object carrying ``revision_id`` and
    ``created_at_utc`` (``SceneRevision`` or ``SceneRevisionSummary``);
    ``labels`` is ``SceneRepository.revision_labels(document_id)``.
    """

    record = labels.get(revision.revision_id) if labels else None
    if record is not None and record.label:
        return record.label
    return saved_label(revision.created_at_utc)


def variant_display_label(variant: Any) -> str:
    """Human label for a ``SystemVariant``: name + saved date (policy 1/2)."""

    return f'{variant.name} · {saved_label(variant.created_at_utc)}'


def spec_display_label(name: str | None, created_at_utc: str) -> str:
    """Label for a named-or-unnamed persisted spec (search spec, O90, ...)."""

    return named_or_saved_label(name, created_at_utc)


def format_versioned_label(prefix: str, version: str, created_at_utc: str) -> str:
    """`{prefix} {version} · {saved date}` for versioned plans/reports."""

    return f'{prefix} {version} · {saved_label(created_at_utc)}'


# --- Measurement vocabulary → JA display labels ---------------------------
#
# The persisted/stored vocabulary stays English (authority payloads, exports,
# machine-readable fields); these maps gloss it for display only. Unknown
# values fall back to the raw code — display never dead-ends, and stored
# data is never rewritten.

_MEASUREMENT_CLAIM_JA: dict[str, str] = {
    'magnitude_response': '振幅応答',
    'phase_response': '位相応答',
    'common_timing': '共通タイミング',
    'arrival_time': '到達時刻',
    'decay': '減衰特性',
    'calibrated_response': '校正済み応答',
    'frequency_response_corrected': '補正済み周波数応答',
    'absolute_spl': '絶対SPL',
    'absolute_noise_level': '絶対ノイズレベル',
    'repeatability': '繰り返し精度',
    'polarity': '極性',
}


def measurement_claim_label(claim: str) -> str:
    """JA label for a ``MeasurementCapabilityClaim``/observable value."""

    return _MEASUREMENT_CLAIM_JA.get(claim, claim)


_ACQUISITION_SOURCE_KIND_JA: dict[str, str] = {
    'native': 'ネイティブ記録',
    'legacy': '移行データ',
    'manual': '手入力',
    'unknown': '不明',
}


def acquisition_source_kind_label(kind: str) -> str:
    """JA label for an ``AcquisitionContextSourceKind``."""

    return _ACQUISITION_SOURCE_KIND_JA.get(kind, kind)


# Emitted ``CadMeasurementQualityCheck.reason``/``CadMeasurementCapability.
# reasons`` sentences (cad_measurement_quality.py). Stored English; glossed
# here for the JA quality surfaces (dataset detail, retake guidance, joint
# optimization prerequisites).
_MEASUREMENT_REASON_JA: dict[str, str] = {
    'clipping metadata is unavailable': 'クリッピング情報がありません',
    'acquisition metadata reports clipping':
        '収録メタデータがクリッピングを報告しています',
    'acquisition metadata reports no clipping':
        '収録メタデータはクリッピングなしを報告しています',
    'explicit SNR evidence is unavailable': '明示的なSNR証拠がありません',
    'SNR evidence exists but the profile has no SNR threshold':
        'SNR証拠はありますがプロファイルにSNRしきい値がありません',
    'usable frequency band evidence is unavailable':
        '使用可能帯域の証拠がありません',
    'explicit usable frequency band is recorded':
        '明示的な使用可能帯域が記録されています',
    'usable frequency band covers the profile requirement':
        '使用可能帯域がプロファイル要求をカバーしています',
    'usable frequency band does not cover the profile requirement':
        '使用可能帯域がプロファイル要求をカバーしていません',
    'acquisition metadata reports an invalid timing reference':
        '収録メタデータが無効なタイミング基準を報告しています',
    'common timing reference evidence is unavailable':
        '共通タイミング基準の証拠がありません',
    'timing reference metadata is incomplete':
        'タイミング基準のメタデータが不完全です',
    'timing reference identity, clock, sample rate and delay correction are '
    'recorded':
        'タイミング基準の識別・クロック・サンプルレート・遅延補正が記録されています',
    'polarity evidence reports reversed polarity':
        '極性証拠が極性反転を報告しています',
    'polarity evidence or confidence is unavailable':
        '極性証拠または信頼度がありません',
    'polarity evidence exists but the profile has no confidence threshold':
        '極性証拠はありますがプロファイルに信頼度しきい値がありません',
    'polarity confidence is below the profile confidence threshold':
        '極性信頼度がプロファイルのしきい値を下回っています',
    'polarity evidence meets the profile confidence threshold':
        '極性証拠がプロファイルの信頼度しきい値を満たしています',
    'no impulse-response evidence is bound to this report':
        'このレポートにインパルス応答証拠が結び付けられていません',
    'IR truncation evidence is unavailable': 'IR切り詰め証拠がありません',
    'IR evidence is reported as truncated':
        'IR証拠が切り詰め済みと報告されています',
    'IR window bounds are unavailable': 'IR窓の境界がありません',
    'IR window bounds are recorded and truncation is not reported':
        'IR窓の境界が記録され切り詰めは報告されていません',
    'calibration-file provenance is unavailable':
        '校正ファイルの由来がありません',
    'calibration-file provenance is incomplete':
        '校正ファイルの由来が不完全です',
    'applied calibration file does not match the expected calibration file':
        '適用された校正ファイルが期待される校正ファイルと一致しません',
    'applied calibration file matches the expected calibration file':
        '適用された校正ファイルが期待される校正ファイルと一致します',
    'repeatability evidence requires at least two measurements and an '
    'explicit metric':
        '繰り返し精度証拠には2件以上の測定と明示的な指標が必要です',
    'repeatability evidence exists but the profile has no repeatability '
    'threshold':
        '繰り返し精度証拠はありますがプロファイルにしきい値がありません',
    'dataset contains phase explicitly marked valid':
        'データセットに有効と明示された位相が含まれています',
    'dataset explicitly has no phase evidence':
        'データセットに位相証拠がないことが明示されています',
    'phase evidence is not verified': '位相証拠が検証されていません',
    'no measurement quality report is bound to this dataset':
        'このデータセットに測定品質レポートが結び付けられていません',
    'immutable frequency/level dataset is present':
        '不変の周波数/レベルデータセットがあります',
    'authoritative AcquisitionContext binding is unavailable':
        '権威ある取得コンテキストの結び付けがありません',
    'arrival-time claims require impulse-response evidence':
        '到達時刻の主張にはインパルス応答証拠が必要です',
    'decay claims require impulse-response evidence':
        '減衰の主張にはインパルス応答証拠が必要です',
    'dataset level-reference authority is unavailable':
        'データセットのレベル基準権威がありません',
    'bound acoustic level calibration is unavailable':
        '結び付けられた音響レベル校正がありません',
    'bound acoustic level calibration does not match the dataset '
    'level-reference pin':
        '結び付けられた音響レベル校正がデータセットのレベル基準ピンと一致しません',
    'measurement-scoped calibration is pinned to this dataset':
        '測定スコープの校正がこのデータセットにピン留めされています',
    'session-scoped calibration is bound by the authoritative acquisition '
    'context':
        'セッションスコープの校正が権威ある取得コンテキストに結び付けられています',
    'session-scoped calibration requires an authoritative acquisition '
    'context':
        'セッションスコープの校正には権威ある取得コンテキストが必要です',
    'instrument-scoped calibration has no instrument identity':
        '機器スコープの校正に機器識別がありません',
    'instrument-scoped calibration requires an authoritative acquisition '
    'context':
        '機器スコープの校正には権威ある取得コンテキストが必要です',
    'instrument-scoped calibration requires microphone instrument identity '
    'in the acquisition context':
        '機器スコープの校正には取得コンテキスト内のマイク機器識別が必要です',
    'instrument-scoped calibration instrument does not match the '
    'acquisition microphone':
        '機器スコープの校正機器が収録マイクと一致しません',
    'instrument-scoped calibration matches the acquisition microphone':
        '機器スコープの校正機器が収録マイクと一致します',
    'calibration validity scope is unestablished':
        '校正の有効スコープが確立されていません',
    'noise-floor evidence is unavailable': 'ノイズフロア証拠がありません',
    'claim was not evaluated by this historical quality report':
        'この主張は過去の品質レポートでは評価されていません',
    'usable frequency band is not established':
        '使用可能帯域が確立されていません',
}

# Parameterized emitted sentences — match on shape, carry values through.
_MEASUREMENT_REASON_JA_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r'^SNR (\S+) dB is below profile minimum (\S+) dB$'),
        'SNR {0} dB がプロファイル下限 {1} dB を下回っています',
    ),
    (
        re.compile(r'^SNR (\S+) dB meets profile minimum (\S+) dB$'),
        'SNR {0} dB がプロファイル下限 {1} dB を満たしています',
    ),
    (
        re.compile(
            r'^repeatability RMS (\S+) dB exceeds profile maximum (\S+) dB$'
        ),
        '繰り返し精度RMS {0} dB がプロファイル上限 {1} dB を超えています',
    ),
    (
        re.compile(
            r'^repeatability RMS (\S+) dB meets profile maximum (\S+) dB$'
        ),
        '繰り返し精度RMS {0} dB がプロファイル上限 {1} dB を満たしています',
    ),
    (
        re.compile(
            r'^usable frequency band (\S+)-(\S+) Hz does not cover required '
            r'band (\S+)-(\S+) Hz$'
        ),
        '使用可能帯域 {0}-{1} Hz が要求帯域 {2}-{3} Hz をカバーしていません',
    ),
    (
        re.compile(
            r'^dataset level reference declares (\S+) semantics, '
            r'not absolute SPL$'
        ),
        'データセットのレベル基準は {0} セマンティクスを宣言しており'
        '絶対SPLではありません',
    ),
    (
        re.compile(
            r'^calibration method (\S+) does not support absolute SPL$'
        ),
        '校正方式 {0} は絶対SPLに対応していません',
    ),
)


def measurement_reason_label(reason: str) -> str:
    """JA gloss for an emitted measurement quality reason sentence.

    Exact emitted strings map directly; parameterized sentences translate by
    shape with the embedded values carried through; anything unrecognized
    falls back to the raw English text (display is fail-open — stored values
    are untouched).
    """

    translated = _MEASUREMENT_REASON_JA.get(reason)
    if translated is not None:
        return translated
    for pattern, template in _MEASUREMENT_REASON_JA_PATTERNS:
        match = pattern.match(reason)
        if match is not None:
            return template.format(*match.groups())
    return reason


# --- Objective/state tokens → JA display labels (REV26-STRINGS) --------------
#
# Evaluation surfaces (playback chain, system expansion, prediction matrix,
# room prediction options, authority inspector) render stored enum tokens and
# stored English reason sentences verbatim. The stored vocabulary stays
# English; these maps gloss it for display only, falling back to the raw
# value for anything unrecognized.

_OBJECTIVE_STATE_JA: dict[str, str] = {
    'available': '利用可能',
    'missing': '証拠なし',
    'unsupported': '未対応',
}


def objective_state_label(state: str) -> str:
    """JA label for an ``ObjectiveState`` (available/missing/unsupported)."""

    return _OBJECTIVE_STATE_JA.get(state, state)


# Matrix cell/run states, prediction option states and currency states share
# one uppercase token vocabulary — one map covers all of them.
_STATE_TOKEN_JA: dict[str, str] = {
    'QUEUED': '待機',
    'RUNNING': '実行中',
    'READY': '実行可',
    'CACHED': 'キャッシュ済み',
    'STALE': '古い',
    'FAILED': '失敗',
    'CANCELLED': '中止',
    'UNSUPPORTED': '未対応',
    'BLOCKED': 'ブロック',
    'CURRENT': '最新',
}


def state_token_label(token: str) -> str:
    """JA label for matrix cell/run, prediction option and currency tokens."""

    return _STATE_TOKEN_JA.get(token, token)


_LIMITER_STATE_JA: dict[str, str] = {
    'amplifier': 'アンプ',
    'speaker': 'スピーカー',
    'equal': '同等',
    'unknown': '不明',
}


def limiter_state_label(state: str) -> str:
    """JA label for a ``LimiterState`` (amplifier/speaker/equal/unknown)."""

    return _LIMITER_STATE_JA.get(state, state)


_MODE_CLASS_JA: dict[str, str] = {
    'axial': '軸方向',
    'tangential': '接線方向',
    'oblique': '斜め方向',
}


def mode_class_label(mode_class: str) -> str:
    """JA label for a ``PredictionModeClass`` (axial/tangential/oblique)."""

    return _MODE_CLASS_JA.get(mode_class, mode_class)


_ENTITY_KIND_JA: dict[str, str] = {
    'speaker': 'スピーカー',
    'seat': '座席',
    'screen': 'スクリーン',
    'display': 'ディスプレイ',
    'projector': 'プロジェクター',
    'riser': 'ライザー',
    'furniture': '家具',
    'av_equipment': 'AV機器',
    'measurement_point': '測定点',
}


def entity_kind_label(kind: str) -> str:
    """JA label for a scene-entity kind embedded in user-facing sentences."""

    return _ENTITY_KIND_JA.get(kind, kind)


_ENVIRONMENT_SOURCE_KIND_JA: dict[str, str] = {
    'nominal_assumption': '標準仮定',
    'derived_from_temperature': '温度導出',
    'derived_from_air_state': '空気状態導出',
    'manual_measured': '手動測定',
    'unknown': '不明',
}


def environment_source_kind_label(kind: str) -> str:
    """JA label for an ``EnvironmentSourceKind`` (sound-speed provenance)."""

    return _ENVIRONMENT_SOURCE_KIND_JA.get(kind, kind)


_CAPABILITY_KIND_JA: dict[str, str] = {
    'continuous': '連続',
    'peak': 'ピーク',
}

_HEADROOM_BASIS_JA: dict[str, str] = {
    'amplifier_margin': 'アンプ余量',
    'scalar_declared': '宣言値',
    'distortion_qualified': '歪み適格',
    'distortion_unqualified': '歪み不適格',
    'unknown': '不明',
}

_REFERENCE_BASIS_JA: dict[str, str] = {
    'same_reference': '同一基準',
    'measured_room_transfer': '実測室内伝達',
    'propagation_model': '伝搬モデル',
    'predicted_transfer': '予測伝達',
}

_SOURCE_NAME_JA: dict[str, str] = {
    'sensitivity': '感度',
    'continuous SPL capability': '連続SPL能力',
    'peak SPL capability': 'ピークSPL能力',
}

_AGGREGATE_LABEL_JA: dict[str, str] = {
    'worst-seat direct level': '最悪座席の直接音レベル',
    'worst-seat target margin': '最悪座席の目標余量',
    'worst-seat continuous headroom': '最悪座席の連続ヘッドルーム',
    'worst-seat peak headroom': '最悪座席のピークヘッドルーム',
    'seat-to-seat direct-level spread': '座席間直接音レベル差',
    'weighted direct level': '重み付き直接音レベル',
    'weighted target margin': '重み付き目標余量',
    'weighted continuous headroom': '重み付き連続ヘッドルーム',
    'weighted peak headroom': '重み付きピークヘッドルーム',
}


def _capability_kind(token: str) -> str:
    return _CAPABILITY_KIND_JA.get(token, token)


def _headroom_basis(token: str) -> str:
    return _HEADROOM_BASIS_JA.get(token, token)


def _reference_basis(token: str) -> str:
    return _REFERENCE_BASIS_JA.get(token, token)


def _source_name(name: str) -> str:
    return _SOURCE_NAME_JA.get(name, name)


def _aggregate_label(label: str) -> str:
    return _AGGREGATE_LABEL_JA.get(label, label)


# Static emitted reason sentences — cad_amplifier_headroom, cad_direct_level,
# cad_usable_output, cad_prediction_matrix, cad_coverage and
# cad_topology_comparison. Stored English; glossed here for JA surfaces.
_SOLVER_REASON_JA: dict[str, str] = {
    # cad_amplifier_headroom
    'speaker electrical load authority is missing':
        'スピーカー電気負荷権威がありません',
    'speaker load is nominal-only; nominal impedance does not establish '
    'actual amplifier load capability':
        'スピーカー負荷が公称値のみです：公称インピーダンスは実際のアンプ負荷能力'
        'を確立しません',
    'requested frequency band is outside speaker load reference domain':
        '要求周波数帯がスピーカー負荷基準域の外です',
    'speaker reference load is outside amplifier evidenced load domain':
        'スピーカー基準負荷がアンプ実証負荷域の外です',
    'requested frequency band is outside amplifier capability domain':
        '要求周波数帯がアンプ能力域の外です',
    'playback weighting differs from amplifier capability authority':
        '再生ウェイティングがアンプ能力権威と異なります',
    'single-channel amplifier capability cannot be promoted to simultaneous '
    'multi-channel performance':
        '単一チャンネルのアンプ能力は同時多チャンネル性能に引き上げられません',
    'simultaneous channel-count condition differs from capability authority':
        '同時チャンネル数条件が能力権威と異なります',
    'voltage/power comparison requires exact resistive load semantics':
        '電圧/電力比較には正確な抵抗負荷セマンティクスが必要です',
    'electrical capability unavailable': '電気的能力が利用できません',
    'speaker sensitivity/reference level is not evidenced':
        'スピーカー感度/基準レベルの証拠がありません',
    'speaker sensitivity has no valid frequency domain':
        'スピーカー感度に有効な周波数域がありません',
    'requested frequency band is outside speaker sensitivity domain':
        '要求周波数帯がスピーカー感度域の外です',
    'speaker sensitivity has no weighting authority for requested weighting':
        'スピーカー感度に要求ウェイティングの権威がありません',
    'speaker sensitivity weighting differs from playback scenario':
        'スピーカー感度のウェイティングが再生シナリオと異なります',
    'amplifier capability cannot be converted to speaker sensitivity '
    'reference':
        'アンプ能力はスピーカー感度基準に変換できません',
    'speaker acoustic SPL capability is not evidenced':
        'スピーカー音響SPL能力の証拠がありません',
    'speaker SPL capability has no weighting provenance for requested '
    'weighting':
        'スピーカーSPL能力に要求ウェイティングの出典がありません',
    'amplifier-constrained acoustic ceiling unavailable':
        'アンプ制約の音響上限が利用できません',
    # cad_direct_level
    'equipment SPL capability is not evidenced':
        '機器SPL能力の証拠がありません',
    'SPL capability has no weighting provenance for requested weighting':
        'SPL能力に要求ウェイティングの出典がありません',
    'equipment sensitivity/reference level is not evidenced':
        '機器感度/基準レベルの証拠がありません',
    'sensitivity has no weighting authority for requested weighting':
        '感度に要求ウェイティングの権威がありません',
    'sensitivity weighting does not match playback scenario':
        '感度のウェイティングが再生シナリオと一致しません',
    'playback input quantity differs from sensitivity reference and no '
    'voltage/power conversion authority is available':
        '再生入力が感度基準と異なり電圧/電力変換権威がありません',
    'usable-output profile binding is advisory (id-only) and cannot drive '
    'O100D headroom (#1026)':
        '使用可能出力プロファイルの結合は参照専用（IDのみ）のためO100D'
        'ヘッドルームを駆動できません（#1026）',
    'usable-output profile records no measurement distance — no listener '
    'transfer can be established':
        '使用可能出力プロファイルに測定距離が記録されておらずリスナー伝達を'
        '確立できません',
    'direct level is missing': '直接音レベルがありません',
    'direct level is unsupported': '直接音レベルは未対応です',
    'missing receiver': '受信点がありません',
    'receiver seat entity is missing from SystemVariant scene':
        '受信座席エンティティがSystemVariantシーンにありません',
    'receiver population member is not a seat entity':
        '受信集団メンバーが座席エンティティではありません',
    'seat has no explicit scene acoustic reference position':
        '座席に明示的なシーン音響基準位置がありません',
    'source and receiver acoustic reference positions coincide':
        'ソースと受信点の音響基準位置が一致しています',
    'seat distance lies outside the declared radial validity domain of the '
    'distance authority':
        '座席距離が距離権威の宣言半径有効域の外です',
    'seat-to-seat direct-level spread requires at least two seats':
        '座席間直接音レベル差には2座席以上が必要です',
    # cad_usable_output
    'no usable-output basis available': '使用可能出力の根拠がありません',
    'distortion/compression policy: measured samples at or below the '
    'target already exceed the criterion':
        '歪み/圧縮ポリシー：目標以下の実測サンプルが既に基準を超えています',
    'distortion/compression policy: no qualifying measured sample — the '
    'policy-qualified ceiling is unestablished':
        '歪み/圧縮ポリシー：適格な実測サンプルがなくポリシー適格上限が'
        '未確立です',
    'distortion/compression policy': '歪み/圧縮ポリシー',
    'declared SPL capability at its declared reference':
        '宣言基準での宣言SPL能力',
    'amplifier margin (electrical, at operating point)':
        'アンプ余量（動作点の電気的余量）',
    'no listener transfer authority — a source-reference level is never '
    'directly compared to a listener/seat target':
        'リスナー伝達権威がなく、ソース基準レベルはリスナー/座席目標と'
        '直接比較されません',
    'same-reference claim but the profile records no measurement distance':
        '同一基準の主張ですがプロファイルに測定距離が記録されていません',
    # cad_prediction_matrix
    'no provider run bound for this matrix source':
        'この行列ソースに結び付けられたプロバイダー実行がありません',
    'provider authority does not match matrix spec':
        'プロバイダー権威が行列仕様と一致しません',
    'provider observable unsupported': 'プロバイダー観測量が未対応です',
    'provider run does not cover receiver':
        'プロバイダー実行が受信点をカバーしていません',
    'provider response frequency grid does not match the matrix observable '
    'contract':
        'プロバイダー応答の周波数グリッドが行列観測量契約と一致しません',
    'scene content changed since the matrix ran':
        '行列実行後にシーン内容が変更されました',
    'acoustic scene snapshot changed since the matrix ran':
        '行列実行後に音響シーンスナップショットが変更されました',
    'no coherent-compatible sources were collected':
        'コヒーレント対応ソースが収集されませんでした',
    'sources do not share a common frequency grid':
        'ソース間で共通の周波数グリッドがありません',
    'at least one source has no declared source normalization':
        '宣言されたソース正規化のないソースがあります',
    # cad_prediction_provider (capability fallback, surfaces in option reasons)
    'observable is outside the bounded R170A provider contract':
        '観測量がR170Aプロバイダーの範囲外です',
    # cad_coverage
    'coverage aggregate unavailable because at least one required seat is '
    'unsupported; partial population evaluation is forbidden':
        '必須座席が未対応のためカバレッジ集計が利用できません：'
        '部分集団評価は禁止です',
    'unsupported directivity evaluation': '指向性評価が未対応です',
    'required receiver seat entity is missing from SystemVariant scene':
        '必須の受信座席エンティティがSystemVariantシーンにありません',
    # cad_topology_comparison (eligibility issue details)
    'comparison requires explicit ObjectiveDefinition authority':
        '比較には明示的なObjectiveDefinition権威が必要です',
    'objective evidence source model id/version differs from the exact '
    'ObjectiveDefinition comparison model':
        '目的証拠のソースモデルid/バージョンが正確なObjectiveDefinition'
        '比較モデルと異なります',
    'required objective is absent from ObjectiveVector':
        '必須目的がObjectiveVectorにありません',
    'required objective state is missing': '必須目的の状態が欠落です',
    'required objective state is unsupported': '必須目的の状態が未対応です',
    'candidate has no VariantEvaluationBundle':
        '候補にVariantEvaluationBundleがありません',
    'required objective evidence uses incompatible declared '
    'evaluator/model/fidelity authority':
        '必須目的証拠が互換でない宣言評価器/モデル/忠実度権威を使っています',
}


def _format(pattern_match: re.Match[str] | None, template: str) -> str | None:
    if pattern_match is None:
        return None
    return template.format(*pattern_match.groups())


# Parameterized emitted sentences — the same emitted shapes as the static
# map above, with the varying parts carried through (and kind/label tokens
# glossed where they carry domain meaning).
_SOLVER_REASON_JA_PATTERNS: tuple[
    tuple[re.Pattern[str], str, dict[str, str] | None], ...
] = (
    (
        re.compile(r'^speaker (\S+) SPL capability is not evidenced$'),
        'スピーカー{0}SPL能力の証拠がありません',
        _CAPABILITY_KIND_JA,
    ),
    (
        re.compile(
            r'^speaker (\S+) SPL capability has no valid frequency domain$'
        ),
        'スピーカー{0}SPL能力に有効な周波数域がありません',
        _CAPABILITY_KIND_JA,
    ),
    (
        re.compile(
            r'^requested frequency band is outside speaker (\S+) SPL '
            r'capability domain$'
        ),
        '要求周波数帯がスピーカー{0}SPL能力域の外です',
        _CAPABILITY_KIND_JA,
    ),
    (
        re.compile(
            r'^speaker (\S+) SPL capability has no duration authority$'
        ),
        'スピーカー{0}SPL能力に持続時間権威がありません',
        _CAPABILITY_KIND_JA,
    ),
    (
        re.compile(
            r'^(\S+) duration differs from speaker SPL capability authority$'
        ),
        '{0}持続時間がスピーカーSPL能力権威と異なります',
        _CAPABILITY_KIND_JA,
    ),
    (
        re.compile(r'^(\S+) amplifier capability is not evidenced$'),
        '{0}アンプ能力の証拠がありません',
        _CAPABILITY_KIND_JA,
    ),
    (
        re.compile(r'^(\S+) amplifier duration is not evidenced$'),
        '{0}アンプ持続時間の証拠がありません',
        _CAPABILITY_KIND_JA,
    ),
    (
        re.compile(
            r'^(\S+) duration differs from amplifier capability authority$'
        ),
        '{0}持続時間がアンプ能力権威と異なります',
        _CAPABILITY_KIND_JA,
    ),
    (
        re.compile(r'^(\S+) SPL capability is not evidenced$'),
        '{0}SPL能力の証拠がありません',
        _CAPABILITY_KIND_JA,
    ),
    (
        re.compile(r'^(\S+) SPL capability has no duration authority$'),
        '{0}SPL能力に持続時間権威がありません',
        _CAPABILITY_KIND_JA,
    ),
    (
        re.compile(
            r'^(\S+) duration differs from evidenced equipment capability '
            r'duration$'
        ),
        '{0}持続時間が実証済み機器能力持続時間と異なります',
        _CAPABILITY_KIND_JA,
    ),
    (
        re.compile(r'^(.+) has no explicit valid frequency domain$'),
        '{0}に明示的な有効周波数域がありません',
        _SOURCE_NAME_JA,
    ),
    (
        re.compile(
            r'^requested frequency band is outside (.+) valid domain$'
        ),
        '要求周波数帯が{0}有効域の外です',
        _SOURCE_NAME_JA,
    ),
    (
        re.compile(
            r'^(.+) unavailable because at least one seat is unsupported$'
        ),
        '{0}が利用できません：未対応の座席が少なくとも1つあります',
        _AGGREGATE_LABEL_JA,
    ),
    (
        re.compile(
            r'^(.+) unavailable because at least one seat is missing '
            r'evidence$'
        ),
        '{0}が利用できません：証拠のない座席が少なくとも1つあります',
        _AGGREGATE_LABEL_JA,
    ),
    (
        re.compile(
            r'^(.+) unavailable because at least one required seat is '
            r'unsupported$'
        ),
        '{0}が利用できません：未対応の必須座席が少なくとも1つあります',
        _AGGREGATE_LABEL_JA,
    ),
    (
        re.compile(
            r'^(.+) unavailable because at least one required seat is '
            r'missing evidence$'
        ),
        '{0}が利用できません：証拠のない必須座席が少なくとも1つあります',
        _AGGREGATE_LABEL_JA,
    ),
    (
        re.compile(
            r'^(.+) unavailable because required weights do not normalize$'
        ),
        '{0}が利用できません：必須重みが正規化できません',
        _AGGREGATE_LABEL_JA,
    ),
    # cad_usable_output transfer/status composites
    (
        re.compile(
            r'^same-reference claim but listener distance (\S+) m differs '
            r'from profile reference (\S+) m$'
        ),
        '同一基準の主張ですがリスナー距離 {0} m がプロファイル基準 '
        '{1} m と異なります',
        None,
    ),
    (
        re.compile(
            r'^same-reference claim but listener axis (\S+) differs from '
            r'profile reference axis (\S+)$'
        ),
        '同一基準の主張ですがリスナー軸 {0} がプロファイル基準軸 {1} '
        'と異なります',
        None,
    ),
    (
        re.compile(
            r'^same-reference claim but environments differ '
            r'\((\S+) vs (\S+)\)$'
        ),
        '同一基準の主張ですが環境が異なります（{0} vs {1}）',
        None,
    ),
    (
        re.compile(
            r"^profile is measured on (\S+) but the listener sits on (\S+) "
            r"and the transfer carries no directivity authority$"
        ),
        'プロファイルは {0} で測定されリスナーは {1} に位置しますが'
        '伝達に指向性権威がありません',
        None,
    ),
    (
        re.compile(
            r'^(\S+) on (\S+): (\S+) dB SPL at listener vs '
            r'(\S+) dB SPL target$'
        ),
        '{0}（基準 {1}）：リスナー {2} dB SPL 対 目標 {3} dB SPL',
        None,
    ),
    (
        re.compile(
            r'^amplifier margin (\S+) dB is electrical headroom, not '
            r'acoustic listener headroom$'
        ),
        'アンプ余量 {0} dB は電気的ヘッドルームで音響リスナーヘッドルーム'
        'ではありません',
        None,
    ),
    (
        re.compile(r'^(\S+): level available but no target supplied$'),
        '{0}：レベルは利用できますが目標が指定されていません',
        _HEADROOM_BASIS_JA,
    ),
    (
        re.compile(
            r'^distortion/compression policy \(no qualifying sample; '
            r'highest measured (\S+) dB SPL is retained as diagnostic '
            r'evidence only\)$'
        ),
        '歪み/圧縮ポリシー（適格サンプルなし。最高実測 {0} dB SPL は'
        '診断証拠としてのみ保持）',
        None,
    ),
    # cad_prediction_matrix binding/currency reasons
    (
        re.compile(r'^matrix source binding changed for: (.+)$'),
        '行列ソース結合が変更されました: {0}',
        None,
    ),
    (
        re.compile(r'^matrix source binding cannot be re-verified for: (.+)$'),
        '行列ソース結合を再検証できません: {0}',
        None,
    ),
    (
        re.compile(r'^matrix receiver binding changed for: (.+)$'),
        '行列受信結合が変更されました: {0}',
        None,
    ),
    (
        re.compile(
            r'^matrix receiver binding cannot be re-verified for: (.+)$'
        ),
        '行列受信結合を再検証できません: {0}',
        None,
    ),
    # coherent-sum compatibility reasons (also fed to cell blocked_reason)
    (
        re.compile(r'^source (\S+) has no bound provider run$'),
        'ソース {0} に結び付けられたプロバイダー実行がありません',
        None,
    ),
    (
        re.compile(
            r'^source (\S+) provider authority does not match the matrix '
            r'spec$'
        ),
        'ソース {0} のプロバイダー権威が行列仕様と一致しません',
        None,
    ),
    (
        re.compile(r'^source (\S+) has no complex-pressure capability$'),
        'ソース {0} に複素圧力能力がありません',
        None,
    ),
    (
        re.compile(r'^source (\S+) does not cover receiver (\S+)$'),
        'ソース {0} が受信点 {1} をカバーしていません',
        None,
    ),
    (
        re.compile(
            r'^source (\S+) receiver (\S+) carries magnitude-only data; '
            r'it cannot enter a coherent sum$'
        ),
        'ソース {0} の受信点 {1} は振幅のみのデータでコヒーレント和に'
        '入りません',
        None,
    ),
    (
        re.compile(r'^sources declare different phasor conventions: (.+)$'),
        'ソース間でフェーザー規約が異なります: {0}',
        None,
    ),
    (
        re.compile(
            r'^sources declare different source normalizations: (.+)$'
        ),
        'ソース間でソース正規化が異なります: {0}',
        None,
    ),
    (
        re.compile(r'^source (\S+) lacks declared timing authority$'),
        'ソース {0} に宣言されたタイミング権威がありません',
        None,
    ),
    # cad_coverage composites
    (
        re.compile(
            r'^seat coverage unavailable because at least one requested '
            r'frequency is unsupported: (.+)$'
        ),
        '少なくとも1つの要求周波数が未対応のため座席カバレッジが'
        '利用できません: {0}',
        None,
    ),
    (
        re.compile(r"^on-axis reference is unsupported: (.+)$"),
        '軸上基準が未対応です: {0}',
        None,
    ),
    # cad_topology_comparison parameterized detail
    (
        re.compile(r'^(\S+) differs from exact comparison definition$'),
        '{0} が正確な比較定義と異なります',
        None,
    ),
)


def solver_reason_label(reason: str) -> str:
    """JA gloss for a stored solver/evaluation reason sentence.

    Covers the amplifier-headroom, direct-level, usable-output, coverage,
    prediction-matrix and topology-comparison vocabularies that reach the
    playback-chain dialog, the system-expansion columns and the prediction
    matrix dock. ``'{basis}: {reason}'`` composites resolve their head basis
    token and translate the tail recursively; unknowns fall back to raw text.
    """

    translated = _SOLVER_REASON_JA.get(reason)
    if translated is not None:
        return translated
    # ``'{basis}: {transfer_block}'`` composites from evaluate_headroom.
    if ': ' in reason:
        head, tail = reason.split(': ', 1)
        if head in _HEADROOM_BASIS_JA:
            return (
                f'{_HEADROOM_BASIS_JA[head]}: '
                + solver_reason_label(tail)
            )
    for pattern, template, group_map in _SOLVER_REASON_JA_PATTERNS:
        match = pattern.match(reason)
        if match is None:
            continue
        groups = match.groups()
        if group_map is not None:
            groups = tuple(group_map.get(group, group) for group in groups)
        return template.format(*groups)
    # ' | '-joined coverage seat reasons: gloss each leg.
    if ' | ' in reason:
        return ' | '.join(
            solver_reason_label(part) for part in reason.split(' | ')
        )
    return reason


_CALIBRATION_DECISION_JA: dict[str, str] = {
    'ALLOWED': '許可',
    'BLOCKED': '不可',
    'UNKNOWN': '不明',
}

# ``evaluate_calibration_support`` prefix composites — the sentence after the
# semicolon is itself one of the mapped reasons.
_CALIBRATION_REASON_PREFIXES: tuple[tuple[str, str], ...] = (
    (
        'absolute delay requires established common timing; ',
        '絶対遅延には確立された共通タイミングが必要です; ',
    ),
    (
        'polarity change requires polarity authority; ',
        '極性変更には極性権威が必要です; ',
    ),
)

_CALIBRATION_REASON_JA: dict[str, str] = {
    'all-pass correction is unsupported in calibration-plan-1 because '
    'coherent inter-channel phase correction authority is not established':
        '全通過補正はcalibration-plan-1で未対応です：チャンネル間の位相補正'
        '権威が確立されていません',
    'device filter-count capability is unknown':
        '機器のフィルター数能力が不明です',
    'device maximum boost capability is unknown':
        '機器の最大ブースト能力が不明です',
    'device maximum cut capability is unknown':
        '機器の最大カット能力が不明です',
}

_CALIBRATION_REASON_JA_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r'^device does not support sample rate (\S+) Hz$'),
        '機器がサンプルレート {0} Hz に対応していません',
    ),
    (
        re.compile(
            r'^channel (\S+) maps to unsupported physical output (\S+)$'
        ),
        'チャンネル {0} が未対応の物理出力 {1} にマッピングされています',
    ),
    (
        re.compile(
            r'^channel (\S+) filter count (\S+) exceeds device maximum '
            r'(\S+)$'
        ),
        'チャンネル {0} のフィルター数 {1} が機器上限 {2} を超えています',
    ),
    (
        re.compile(r'^filter (\S+) type (\S+) is unsupported by the device$'),
        'フィルター {0} の種別 {1} が機器で未対応です',
    ),
    (
        re.compile(
            r'^filter (\S+) boost (\S+) dB exceeds plan maximum (\S+) dB$'
        ),
        'フィルター {0} のブースト {1} dB がプラン上限 {2} dB を'
        '超えています',
    ),
    (
        re.compile(
            r'^filter (\S+) cut (\S+) dB exceeds plan maximum (\S+) dB$'
        ),
        'フィルター {0} のカット {1} dB がプラン下限 {2} dB を'
        '超えています',
    ),
    (
        re.compile(r'^filter (\S+) exceeds device maximum boost$'),
        'フィルター {0} が機器最大ブーストを超えています',
    ),
    (
        re.compile(r'^filter (\S+) exceeds device maximum cut$'),
        'フィルター {0} が機器最大カットを超えています',
    ),
    (
        re.compile(r'^channel (\S+) gain is below device minimum$'),
        'チャンネル {0} のゲインが機器最小値を下回っています',
    ),
    (
        re.compile(r'^channel (\S+) gain is above device maximum$'),
        'チャンネル {0} のゲインが機器最大値を超えています',
    ),
    (
        re.compile(r'^channel (\S+) delay exceeds device maximum$'),
        'チャンネル {0} の遅延が機器最大値を超えています',
    ),
    (
        re.compile(
            r'^channel (\S+) crossover order (\S+) is unsupported or '
            r'unknown$'
        ),
        'チャンネル {0} のクロスオーバー次数 {1} が未対応または不明です',
    ),
)


def calibration_reason_label(reason: str) -> str:
    """JA gloss for a stored ``CalibrationPlan.unsupported_reasons`` item.

    Handles the ``'{claim}: {decision}: {r1; r2; …}'`` capability composite
    (claim → :func:`measurement_claim_label`, decision → JA token, each
    ``;``-separated reason → :func:`measurement_reason_label`), the
    ``'…; {reason}'`` prefix composites, and the plan/device sentences
    emitted by ``evaluate_calibration_support``. Unknowns fall back to
    :func:`measurement_reason_label`, then raw text.
    """

    translated = _CALIBRATION_REASON_JA.get(reason)
    if translated is not None:
        return translated
    for prefix, ja_prefix in _CALIBRATION_REASON_PREFIXES:
        if reason.startswith(prefix):
            return ja_prefix + calibration_reason_label(
                reason[len(prefix):]
            )
    parts = reason.split(': ', 2)
    if (
        len(parts) == 3
        and parts[0] in _MEASUREMENT_CLAIM_JA
        and parts[1] in _CALIBRATION_DECISION_JA
    ):
        claim = measurement_claim_label(parts[0])
        decision = _CALIBRATION_DECISION_JA[parts[1]]
        tail = '; '.join(
            measurement_reason_label(item)
            for item in parts[2].split('; ')
        )
        return f'{claim}: {decision}: {tail}'
    for pattern, template in _CALIBRATION_REASON_JA_PATTERNS:
        match = pattern.match(reason)
        if match is not None:
            return template.format(*match.groups())
    return measurement_reason_label(reason)


_AUTHORITY_EDGE_PREFIX_JA: dict[str, str] = {
    'stale_because': '古い原因',
    'invalidates': '無効化',
}

_INVALIDATED_BY_PREFIX = 'invalidated by: '


def why_stale_reason_label(reason: str) -> str:
    """JA gloss for ``AuthorityGraphInspector.why_stale`` composites.

    Node ``stale_reasons`` are emitted Japanese already and pass through;
    the stored ``'{edge_kind}: {label} — {detail}'`` /
    ``'invalidated by: {label}'`` composites get a JA scaffold while the
    node label and free-form edge detail stay verbatim (fail-open).
    """

    if reason.startswith(_INVALIDATED_BY_PREFIX):
        return '無効化元: ' + reason[len(_INVALIDATED_BY_PREFIX):]
    head, sep, tail = reason.partition(': ')
    if sep and head in _AUTHORITY_EDGE_PREFIX_JA:
        return f'{_AUTHORITY_EDGE_PREFIX_JA[head]}: {tail}'
    return reason


__all__ = [
    'acquisition_source_kind_label',
    'calibration_reason_label',
    'entity_kind_label',
    'environment_source_kind_label',
    'format_versioned_label',
    'limiter_state_label',
    'measurement_claim_label',
    'measurement_reason_label',
    'mode_class_label',
    'named_or_saved_label',
    'objective_state_label',
    'revision_display_label',
    'saved_label',
    'solver_reason_label',
    'spec_display_label',
    'state_token_label',
    'variant_display_label',
    'why_stale_reason_label',
]
