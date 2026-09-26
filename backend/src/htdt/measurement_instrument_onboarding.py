"""Instrument onboarding evaluation for the measurement calibration page.

Evaluates the persisted acquisition-context, level-calibration and
campaign-plan authorities into a checklist a user can work through — the
UMIK-1 contract documented in ``docs/UMIK1_VALIDATION.md`` expressed
against stored records only (no live REW session required):

* serial/model recorded on the context microphone;
* calibration filename consistent with the calibration profile
  (``_90deg`` naming rule);
* capsule direction matching the profile's expected aim
  (90deg -> ceiling ``[0,0,1]``, 0deg -> facing ``[0,-1,0]``);
* 48 kHz sample rate for UMIK-1;
* absolute-SPL readiness via an SPL-authorizing level calibration
  (a mic cal file alone never authorizes absolute SPL);
* at least one persisted REW runner plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath
from typing import Literal

from .cad_measurement_authorities import (
    CadAcousticLevelCalibration,
    calibration_supports_absolute_spl,
)
from .cad_measurement_quality import CadAcquisitionContext

InstrumentStepStatus = Literal['ready', 'action', 'manual']

_AIM_TOLERANCE = 1e-6
_EXPECTED_AIM = {
    '90deg': (0.0, 0.0, 1.0),
    '0deg': (0.0, -1.0, 0.0),
}


@dataclass(frozen=True)
class InstrumentStep:
    """One onboarding step evaluated against persisted authorities.

    ``status`` is ``ready`` (verified against stored records), ``action``
    (something to record in the app — ``link`` names the measurement
    context page to open) or ``manual`` (a physical/session-side
    confirmation the stored records alone cannot prove).
    """

    key: str
    title: str
    status: InstrumentStepStatus
    detail: str
    link: str | None


def _cal_basename(filename: str | None) -> str:
    if not filename:
        return ''
    return PurePath(str(filename).replace('\\', '/')).name.casefold()


def _is_umik1(model: str | None) -> bool:
    return bool(model) and 'umik' in str(model).casefold()


def _direction_matches_profile(context: CadAcquisitionContext) -> bool | None:
    """True/False when both profile and direction are recorded, else None."""
    mic = context.microphone
    if mic is None or mic.calibration_profile not in _EXPECTED_AIM:
        return None
    direction = mic.direction if mic.direction is not None else context.measurement_direction
    if direction is None:
        return None
    expected = _EXPECTED_AIM[mic.calibration_profile]
    return all(
        abs(float(actual) - float(expected_value)) <= _AIM_TOLERANCE
        for actual, expected_value in zip(
            (direction.x, direction.y, direction.z), expected
        )
    )


def evaluate_instrument_onboarding(
    *,
    context: CadAcquisitionContext | None,
    level_calibrations: tuple[CadAcousticLevelCalibration, ...],
    plan_count: int,
) -> tuple[InstrumentStep, ...]:
    """Evaluate the UMIK-1 onboarding checklist against persisted records."""
    steps: list[InstrumentStep] = []
    mic = context.microphone if context is not None else None

    # 1. Microphone identity ---------------------------------------------------
    if context is None or mic is None:
        steps.append(InstrumentStep(
            key='microphone',
            title='マイクの登録',
            status='action',
            detail='取得条件にマイクがありません — 「割り当て」でマイクのモデル・接続を記録してください',
            link='assignment',
        ))
    elif not (mic.model or '').strip():
        steps.append(InstrumentStep(
            key='microphone',
            title='マイクの登録',
            status='action',
            detail='マイクのモデル名が未記録です — 「割り当て」でモデル（例: miniDSP UMIK-1）を記録してください',
            link='assignment',
        ))
    elif not (mic.serial or '').strip():
        steps.append(InstrumentStep(
            key='microphone',
            title='マイクの登録',
            status='manual',
            detail=f'モデル: {mic.model} · シリアル番号が未記録です（推奨: 記録して個体を識別）',
            link='assignment',
        ))
    else:
        steps.append(InstrumentStep(
            key='microphone',
            title='マイクの登録',
            status='ready',
            detail=f'モデル: {mic.model} · シリアル: {mic.serial}',
            link=None,
        ))

    # 2. Calibration file ------------------------------------------------------
    profile = mic.calibration_profile if mic is not None else None
    cal_name = _cal_basename(mic.calibration_filename) if mic is not None else ''
    if mic is None:
        steps.append(InstrumentStep(
            key='calibration_file',
            title='マイク校正ファイル',
            status='action',
            detail='マイク登録後に校正ファイルを関連付けてください',
            link='assignment',
        ))
    elif not cal_name:
        steps.append(InstrumentStep(
            key='calibration_file',
            title='マイク校正ファイル',
            status='action',
            detail='校正ファイル名が未記録です — 「割り当て」でファイル名を記録し、バイト列を添付してください',
            link='assignment',
        ))
    elif profile == '90deg' and '_90deg' not in cal_name:
        steps.append(InstrumentStep(
            key='calibration_file',
            title='マイク校正ファイル',
            status='action',
            detail=f'90deg基準（上向き）には「_90deg」を含む校正ファイルが必要です — 現在: {cal_name}',
            link='assignment',
        ))
    elif profile == '0deg' and '_90deg' in cal_name:
        steps.append(InstrumentStep(
            key='calibration_file',
            title='マイク校正ファイル',
            status='action',
            detail=f'0deg基準（正面）では「_90deg」を含まない校正ファイルを使います — 現在: {cal_name}',
            link='assignment',
        ))
    elif profile not in _EXPECTED_AIM:
        steps.append(InstrumentStep(
            key='calibration_file',
            title='マイク校正ファイル',
            status='manual',
            detail=f'基準向きが未選択です — {cal_name} が向きに合うか確認してください',
            link='assignment',
        ))
    else:
        steps.append(InstrumentStep(
            key='calibration_file',
            title='マイク校正ファイル',
            status='ready',
            detail=f'{cal_name}（{profile}基準と一致）',
            link=None,
        ))

    # 3. Orientation -----------------------------------------------------------
    aim_match = _direction_matches_profile(context) if context is not None else None
    if mic is None:
        steps.append(InstrumentStep(
            key='orientation',
            title='マイクの向き',
            status='action',
            detail='マイク登録後に向きを記録してください',
            link='assignment',
        ))
    elif profile not in _EXPECTED_AIM:
        steps.append(InstrumentStep(
            key='orientation',
            title='マイクの向き',
            status='action',
            detail='基準向きが未選択です — 90deg=天井向き（ホームシアター標準）/ 0deg=スピーカー正面',
            link='assignment',
        ))
    elif aim_match is None:
        steps.append(InstrumentStep(
            key='orientation',
            title='マイクの向き',
            status='manual',
            detail=f'{profile}基準が選択済み — 実際のカプセル向きが一致するか現物で確認してください',
            link='assignment',
        ))
    elif aim_match:
        steps.append(InstrumentStep(
            key='orientation',
            title='マイクの向き',
            status='ready',
            detail=f'{profile}基準と記録された向きが一致しています',
            link=None,
        ))
    else:
        steps.append(InstrumentStep(
            key='orientation',
            title='マイクの向き',
            status='action',
            detail=f'記録された向きが{profile}基準と一致しません — 「割り当て」で向きを修正してください',
            link='assignment',
        ))

    # 4. Sample rate -----------------------------------------------------------
    rate = mic.sample_rate_hz if mic is not None else None
    if mic is None:
        steps.append(InstrumentStep(
            key='sample_rate',
            title='サンプルレート 48 kHz',
            status='action',
            detail='マイク登録後にサンプルレートを記録してください',
            link='assignment',
        ))
    elif rate == 48000:
        steps.append(InstrumentStep(
            key='sample_rate',
            title='サンプルレート 48 kHz',
            status='ready',
            detail='48 kHz が記録されています',
            link=None,
        ))
    elif _is_umik1(mic.model):
        steps.append(InstrumentStep(
            key='sample_rate',
            title='サンプルレート 48 kHz',
            status='action',
            detail=f'UMIK-1 の基準は 48 kHz です — 現在: {rate or "未記録"} Hz',
            link='assignment',
        ))
    else:
        steps.append(InstrumentStep(
            key='sample_rate',
            title='サンプルレート 48 kHz',
            status='manual',
            detail=f'現在: {rate or "未記録"} Hz — REW 計測では 48 kHz を推奨します',
            link='assignment',
        ))

    # 5. Absolute SPL readiness ------------------------------------------------
    spl_calibrations = [
        calibration
        for calibration in level_calibrations
        if calibration_supports_absolute_spl(calibration)
    ]
    if spl_calibrations:
        methods = sorted({calibration.method for calibration in spl_calibrations})
        steps.append(InstrumentStep(
            key='spl_readiness',
            title='絶対SPL（レベル校正）',
            status='ready',
            detail=f'絶対SPLを根拠づけるレベル校正があります（{", ".join(methods)}）',
            link=None,
        ))
    else:
        steps.append(InstrumentStep(
            key='spl_readiness',
            title='絶対SPL（レベル校正）',
            status='manual',
            detail='UMIK-1単体では相対レベルのみです — 絶対SPLには音響校正器・REW SPLセッション・基準メーター転送のいずれかの記録が必要です',
            link=None,
        ))

    # 6. REW campaign ----------------------------------------------------------
    if plan_count > 0:
        steps.append(InstrumentStep(
            key='rew_campaign',
            title='REWキャンペーン',
            status='ready',
            detail=f'測定プランが {plan_count} 件あります',
            link='campaign',
        ))
    else:
        steps.append(InstrumentStep(
            key='rew_campaign',
            title='REWキャンペーン',
            status='action',
            detail='測定プランがありません — 「キャンペーン」でREW計測プランを作成してください（REWは -api 起動・入力はJava・UMIK-1選択・校正ファイル適用）',
            link='campaign',
        ))

    return tuple(steps)


__all__ = [
    'InstrumentStep',
    'InstrumentStepStatus',
    'evaluate_instrument_onboarding',
]
