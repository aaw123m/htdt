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


__all__ = [
    'acquisition_source_kind_label',
    'format_versioned_label',
    'measurement_claim_label',
    'measurement_reason_label',
    'named_or_saved_label',
    'revision_display_label',
    'saved_label',
    'spec_display_label',
    'variant_display_label',
]
