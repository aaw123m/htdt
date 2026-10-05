"""Mechanical rattle / structure-borne noise qualification (#589).

Resonant rattles and buzzes — panels, fixtures, grille vanes, door
hardware, furniture driven by the soundtrack's low-frequency energy —
do not appear on a frequency-response conformance run. They need a
declared stress stimulus at real playback levels and evidence records
that survive audit. This module is that qualification authority.

Contract properties:

- a :class:`MechanicalNoiseTest` binds an *exact* stress stimulus
  (signal type, sweep/dwell, level, channels, bass management, EQ and
  system output state) — 'the room was measured' without stimulus
  identity is not rattle evidence;
- a :class:`RattleEvent` records onset level, onset band, hysteresis /
  intermittency, repeatability and acoustic evidence; localization is a
  state machine (suspected -> correlated_with_object ->
  confirmed_by_intervention), never a guess;
- room rattles and loudspeaker self-distortion are kept distinct — an
  event can be attributed to the loudspeaker only via explicit
  classification, and attribution stays ``undetermined`` otherwise;
- vibration-sensor evidence is ``relative_diagnostic`` unless a
  calibration reference exists — consumer accelerometer rows can never
  promote themselves to calibrated evidence;
- remediation is append-only and traceable to the as-built revision;
  a 'resolved' verdict requires a retest at the relevant stress level —
  a fix followed by silence is ``insufficient_evidence``, not resolved;
- fail-closed: when the tested maximum level is below the project
  target, the best achievable verdict is ``resolved_with_limitations``;
  when there is no stress test at all, the qualification is
  ``insufficient_evidence`` — FR conformance never implies 'no rattle'.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_equipment import EquipmentDataProvenance, FrequencyDomain
from .canonical_json import (
    canonical_sha256 as _digest,
    canonicalize_payload as _canon,
)


_SHA256_PATTERN = r'^[0-9a-f]{64}$'

MECHANICAL_NOISE_SCHEMA_VERSION = 1
MECHANICAL_NOISE_AUTHORITY_VERSION = 'mechanical-noise-1'

#: Taxonomy of mechanically-excited noise. Classification is
#: probabilistic — an event may stay ``unknown`` forever and remain
#: honestly stored.
MechanicalNoiseKind = Literal[
    'rattle_impact_chatter',
    'buzz_contact_vibration',
    'panel_resonance',
    'fixture_resonance',
    'duct_grille_vibration',
    'screen_frame_vibration',
    'furniture_cabinet_vibration',
    'door_hardware_vibration',
    'electrical_lighting_buzz',
    'loudspeaker_self_noise',
    'unknown',
]

MechanicalSignalType = Literal[
    'stepped_sine',
    'slow_sine_sweep',
    'band_limited_noise',
    'lfe_program_content',
    'single_tone',
    'frequency_response_measurement',
    'other',
]

VibrationCalibrationClass = Literal[
    'relative_diagnostic',
    'calibrated',
]

RattleLocalizationState = Literal[
    'suspected',
    'correlated_with_object',
    'confirmed_by_intervention',
    'resolved',
    'unresolved',
]

RattleRepeatability = Literal[
    'repeatable',
    'intermittent',
    'single_occurrence',
]

RattleAttribution = Literal[
    'room_object',
    'loudspeaker_distortion',
    'ambiguous',
    'undetermined',
]

RattleSeverityClass = Literal[
    'audible_at_reference',
    'audible_at_moderate',
    'threshold_only',
    'unknown',
]

RemediationActionKind = Literal[
    'fastener_tightened',
    'damping_added',
    'cable_secured',
    'panel_braced',
    'fixture_replaced',
    'cabinet_adjusted',
    'screen_isolated',
    'grille_treated',
    'other',
]

RemediationEventVerdict = Literal[
    'resolved_at_tested_level',
    'resolved_with_limitations',
    'reduced_but_present',
    'moved_to_different_frequency',
    'new_artifact_introduced',
    'not_resolved',
    'insufficient_evidence',
]

MechanicalNoiseOverallVerdict = Literal[
    'rattle_free_at_tested_level',
    'qualified_with_limitations',
    'not_qualified',
    'insufficient_evidence',
]

#: JA labels for UI display.
MECHANICAL_NOISE_KIND_LABELS = {
    'rattle_impact_chatter': 'ラトル（衝撃・打音）',
    'buzz_contact_vibration': 'バズ（接触振動）',
    'panel_resonance': 'パネル共振',
    'fixture_resonance': '照明・器具共振',
    'duct_grille_vibration': 'ダクト・グリル振動',
    'screen_frame_vibration': 'スクリーンフレーム振動',
    'furniture_cabinet_vibration': '家具・キャビネット振動',
    'door_hardware_vibration': 'ドア金物振動',
    'electrical_lighting_buzz': '電気・照明バズ',
    'loudspeaker_self_noise': 'スピーカー由来歪み',
    'unknown': '不明',
}

RATTLE_LOCALIZATION_LABELS = {
    'suspected': '疑いあり',
    'correlated_with_object': '物体相関あり',
    'confirmed_by_intervention': '介入で確認',
    'resolved': '解消済み',
    'unresolved': '未解消',
}

REMEDIATION_ACTION_LABELS = {
    'fastener_tightened': '締結増締め',
    'damping_added': '制振材追加',
    'cable_secured': 'ケーブル固定',
    'panel_braced': 'パネル補強',
    'fixture_replaced': '器具交換',
    'cabinet_adjusted': 'キャビネット調整',
    'screen_isolated': 'スクリーン絶縁',
    'grille_treated': 'グリル処置',
    'other': 'その他',
}

REMEDIATION_VERDICT_LABELS = {
    'resolved_at_tested_level': '試験レベルで解消',
    'resolved_with_limitations': '制限付き解消',
    'reduced_but_present': '低減・残存',
    'moved_to_different_frequency': '周波数移動',
    'new_artifact_introduced': '新たなアーティファクト',
    'not_resolved': '未解消',
    'insufficient_evidence': '証拠不足',
}

MECHANICAL_OVERALL_LABELS = {
    'rattle_free_at_tested_level': '試験レベルでラトルなし',
    'qualified_with_limitations': '制限付き適合',
    'not_qualified': '不適合',
    'insufficient_evidence': '証拠不足',
}

#: Stimulus kinds that can honestly stress-test for rattle. A frequency
#: -response measurement sweep is an acoustic-linearity instrument, not a
#: rattle stressor — tests built on it cannot produce a 'no rattle' claim.
RATTLE_STRESS_STIMULI = frozenset({
    'stepped_sine',
    'slow_sine_sweep',
    'band_limited_noise',
    'lfe_program_content',
    'single_tone',
    'other',
})


def _require_finite(value: float, name: str) -> float:
    if not isfinite(value):
        raise ValueError(f'{name} must be finite')
    return value


class NoiseTestOperatingState(BaseModel):
    """Operating-state binding for a noise test — doors, windows,
    cabinets, HVAC and occupancy all change the rattle picture."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    doors: Literal['open', 'closed', 'unknown'] = 'unknown'
    windows: Literal['open', 'closed', 'unknown'] = 'unknown'
    cabinets: Literal['open', 'closed', 'unknown'] = 'unknown'
    furniture_moved: bool | None = None
    hvac_state: Literal['on', 'off', 'unknown'] = 'unknown'
    occupancy_state: str | None = None
    state_snapshot_id: str | None = None


class NoiseStressStimulus(BaseModel):
    """The exact stimulus a rattle test applied. Without this record the
    test is not stress evidence."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    signal_type: MechanicalSignalType
    frequency_hz: float | None = None
    frequency_domain: FrequencyDomain | None = None
    sweep_rate_oct_s: float | None = None
    dwell_s: float | None = None
    ramp_s: float | None = None
    level_db_spl: float | None = None
    level_at_mlp_db: float | None = None
    channel_ids: tuple[str, ...] = ()
    subwoofer_group: str | None = None
    bass_management_state: Literal['on', 'off', 'unknown'] = 'unknown'
    eq_filter_state: str | None = None
    system_output_ref: str | None = None
    safety_limit_note: str | None = None
    duration_s: float | None = None

    @model_validator(mode='after')
    def _finite(self) -> 'NoiseStressStimulus':
        for name in (
            'frequency_hz',
            'sweep_rate_oct_s',
            'dwell_s',
            'ramp_s',
            'level_db_spl',
            'level_at_mlp_db',
            'duration_s',
        ):
            value = getattr(self, name)
            if value is not None:
                _require_finite(value, name)
        if self.signal_type in ('stepped_sine', 'single_tone') and (
            self.frequency_hz is None and self.frequency_domain is None
        ):
            raise ValueError(
                'tonal stimuli require frequency_hz or frequency_domain'
            )
        return self

    @property
    def is_rattle_stress(self) -> bool:
        return self.signal_type in RATTLE_STRESS_STIMULI


class VibrationSensorEvidence(BaseModel):
    """Optional contact-sensor evidence. ``relative_diagnostic`` rows may
    corroborate timing/localization but can never substitute for acoustic
    evidence or claim calibrated amplitudes."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    device_id: str = Field(min_length=1)
    mounting_location: str | None = None
    axis: Literal['x', 'y', 'z', 'triax', 'unknown'] = 'unknown'
    sample_rate_hz: float | None = None
    calibration_class: VibrationCalibrationClass = 'relative_diagnostic'
    calibration_ref: str | None = None
    sync_ref: str | None = None
    note: str | None = None

    @model_validator(mode='after')
    def _calibration(self) -> 'VibrationSensorEvidence':
        if self.sample_rate_hz is not None:
            _require_finite(self.sample_rate_hz, 'sample_rate_hz')
        if (
            self.calibration_class == 'calibrated'
            and self.calibration_ref is None
        ):
            raise ValueError(
                'calibrated vibration evidence requires a calibration_ref'
            )
        return self


class MechanicalNoiseTest(BaseModel):
    """One stress run: stimulus + operating state + capture context."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    test_id: str
    document_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    stimulus: NoiseStressStimulus
    operating_state: NoiseTestOperatingState = NoiseTestOperatingState()
    mic_position_ids: tuple[str, ...] = ()
    listening_position_ref: str | None = None
    project_target_level_db: float | None = None
    provenance: EquipmentDataProvenance | None = None
    captured_at_utc: str = Field(min_length=1)
    note: str | None = None
    test_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'MechanicalNoiseTest':
        expected = _digest(self.identity_payload())
        if self.test_sha256 != expected:
            raise ValueError('test_sha256 does not match content')
        if self.test_id != f'mnt:{expected}':
            raise ValueError('test_id must be mnt:<sha256>')
        if self.project_target_level_db is not None:
            _require_finite(
                self.project_target_level_db, 'project_target_level_db'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('test_id', None)
        payload.pop('test_sha256', None)
        return payload


class RattleAcousticEvidence(BaseModel):
    """What was heard/measured at the event."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    onset_band_hz: float | None = None
    narrowband_peak_hz: float | None = None
    harmonic_content: bool | None = None
    chatter_observed: bool | None = None
    audible_at_listening_position: bool | None = None
    spectrogram_ref: str | None = None
    recording_ref: str | None = None
    note: str | None = None

    @model_validator(mode='after')
    def _finite(self) -> 'RattleAcousticEvidence':
        for name in ('onset_band_hz', 'narrowband_peak_hz'):
            value = getattr(self, name)
            if value is not None:
                _require_finite(value, name)
        return self


class RattleEvent(BaseModel):
    """A detected rattle/buzz. Content-sealed against its test."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    event_id: str
    document_id: str = Field(min_length=1)
    test_id: str = Field(min_length=1)
    test_sha256: str = Field(min_length=1)
    kind: MechanicalNoiseKind = 'unknown'
    first_onset_level_db: float | None = None
    onset_frequency_domain: FrequencyDomain | None = None
    hysteresis_observed: bool | None = None
    intermittent: bool | None = None
    repeatability: RattleRepeatability = 'repeatable'
    severity_class: RattleSeverityClass = 'unknown'
    acoustic_evidence: RattleAcousticEvidence | None = None
    vibration_evidence: VibrationSensorEvidence | None = None
    suspected_object_refs: tuple[str, ...] = ()
    localization_state: RattleLocalizationState = 'suspected'
    attribution: RattleAttribution = 'undetermined'
    provenance: EquipmentDataProvenance | None = None
    detected_at_utc: str = Field(min_length=1)
    note: str | None = None
    event_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'RattleEvent':
        expected = _digest(self.identity_payload())
        if self.event_sha256 != expected:
            raise ValueError('event_sha256 does not match content')
        if self.event_id != f'rte:{expected}':
            raise ValueError('event_id must be rte:<sha256>')
        if self.first_onset_level_db is not None:
            _require_finite(
                self.first_onset_level_db, 'first_onset_level_db'
            )
        if (
            self.localization_state == 'confirmed_by_intervention'
            and not self.suspected_object_refs
        ):
            raise ValueError(
                'confirmed_by_intervention requires suspected_object_refs'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('event_id', None)
        payload.pop('event_sha256', None)
        return payload


class RemediationAction(BaseModel):
    """Append-only record of a fix attempt tied to the as-built state."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    action_id: str
    document_id: str = Field(min_length=1)
    event_refs: tuple[tuple[str, str], ...] = ()
    action_kind: RemediationActionKind
    affected_object_ref: str | None = None
    performed_by: str | None = None
    evidence_refs: tuple[str, ...] = ()
    resulting_scene_revision: str | None = None
    performed_at_utc: str = Field(min_length=1)
    note: str | None = None
    action_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'RemediationAction':
        for pair in self.event_refs:
            if len(pair) != 2 or not pair[0] or not pair[1]:
                raise ValueError('event_refs entries must be (event_id, sha)')
        expected = _digest(self.identity_payload())
        if self.action_sha256 != expected:
            raise ValueError('action_sha256 does not match content')
        if self.action_id != f'rma:{expected}':
            raise ValueError('action_id must be rma:<sha256>')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('action_id', None)
        payload.pop('action_sha256', None)
        return payload


class RemediationEventResult(BaseModel):
    """Per-event post-remediation outcome."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    event_id: str
    verdict: RemediationEventVerdict
    retest_level_db: float | None = None
    residual_onset_level_db: float | None = None
    note: str | None = None


class MechanicalNoiseQualification(BaseModel):
    """The qualification record: which tests ran, which events
    remediated, and what the room may claim — capped at the tested
    stress level, never extrapolated upward."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    qualification_id: str
    document_id: str = Field(min_length=1)
    test_ids: tuple[str, ...] = ()
    event_results: tuple[RemediationEventResult, ...] = ()
    remediation_action_ids: tuple[str, ...] = ()
    tested_max_level_db: float | None = None
    project_target_level_db: float | None = None
    overall_verdict: MechanicalNoiseOverallVerdict
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'MechanicalNoiseQualification':
        expected = _digest(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('qualification_sha256 does not match content')
        if self.qualification_id != f'mnq:{expected}':
            raise ValueError('qualification_id must be mnq:<sha256>')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('qualification_id', None)
        payload.pop('qualification_sha256', None)
        return payload


def evaluate_mechanical_noise(
    *,
    document_id: str,
    tests: Sequence[MechanicalNoiseTest],
    events: Sequence[RattleEvent],
    retest_test_ids: Sequence[str] = (),
    remediation_actions: Sequence[RemediationAction] = (),
    evaluated_at_utc: str,
    project_target_level_db: float | None = None,
) -> MechanicalNoiseQualification:
    """Fail-closed qualification of rattle/structure-borne state.

    - a test whose stimulus is not a rattle stressor (e.g. a plain FR
      sweep) contributes no stress level and no resolution power;
    - 'resolved_*' verdicts are only reachable for events whose test was
      re-run after remediation (``retest_test_ids``) — a fix without a
      retest is ``insufficient_evidence``;
    - an event still observed at the same band on retest is
      ``not_resolved`` (callers supply retest events); an event re-detected
      at a different band is ``moved_to_different_frequency``;
    - if the project target exceeds the tested maximum, every resolved
      verdict degrades to ``resolved_with_limitations`` and the overall
      verdict is at best ``qualified_with_limitations``;
    - no qualifying stress test at all -> ``insufficient_evidence``.
    """
    stress_tests = [t for t in tests if t.stimulus.is_rattle_stress]
    stress_test_ids = {t.test_id for t in stress_tests}
    retest_ids = set(retest_test_ids) & stress_test_ids

    tested_levels = [
        t.stimulus.level_db_spl for t in stress_tests
        if t.stimulus.level_db_spl is not None
    ]
    tested_levels += [
        t.stimulus.level_at_mlp_db for t in stress_tests
        if t.stimulus.level_at_mlp_db is not None
    ]
    tested_max = max(tested_levels) if tested_levels else None

    target = project_target_level_db
    if target is None:
        for t in stress_tests:
            if t.project_target_level_db is not None:
                target = t.project_target_level_db
                break

    remediated_ids = {
        ref[0] for action in remediation_actions for ref in action.event_refs
    }

    limitations: list[str] = []
    event_results: list[RemediationEventResult] = []
    for event in events:
        base_tested = event.test_id in stress_test_ids
        retested = event.test_id in retest_ids
        remediated = event.event_id in remediated_ids
        if not base_tested:
            verdict: RemediationEventVerdict = 'insufficient_evidence'
            note = 'originating test was not a rattle stress test'
        elif not remediated and event.localization_state != 'resolved':
            verdict = 'not_resolved'
            note = 'no remediation recorded'
        elif not retested:
            verdict = 'insufficient_evidence'
            note = 'remediation recorded but no qualifying retest'
        elif event.localization_state == 'resolved':
            verdict = 'resolved_at_tested_level'
            note = None
        else:
            verdict = 'reduced_but_present'
            note = 'event still localizes post-remediation'
        event_results.append(
            RemediationEventResult(
                event_id=event.event_id,
                verdict=verdict,
                retest_level_db=(
                    _test_level(event.test_id, tests) if retested else None
                ),
                residual_onset_level_db=(
                    event.first_onset_level_db
                    if verdict in ('reduced_but_present', 'not_resolved')
                    else None
                ),
                note=note,
            )
        )

    if target is not None and tested_max is not None and target > tested_max:
        limitations.append(
            'project_target_exceeds_tested_level: '
            f'{target:.1f}>{tested_max:.1f} dB'
        )
        for index, result in enumerate(event_results):
            if result.verdict == 'resolved_at_tested_level':
                event_results[index] = result.model_copy(
                    update={
                        'verdict': 'resolved_with_limitations',
                        'note': 'target level above tested maximum',
                    }
                )
    if target is not None and tested_max is None:
        limitations.append('target_declared_but_no_tested_level')

    if not stress_tests:
        overall: MechanicalNoiseOverallVerdict = 'insufficient_evidence'
        limitations.append('no_rattle_stress_test')
    elif not events:
        if target is not None and tested_max is not None and target > tested_max:
            overall = 'qualified_with_limitations'
        else:
            overall = 'rattle_free_at_tested_level'
    elif any(
        r.verdict in ('not_resolved', 'new_artifact_introduced')
        for r in event_results
    ):
        overall = 'not_qualified'
    elif any(
        r.verdict in (
            'reduced_but_present',
            'moved_to_different_frequency',
            'insufficient_evidence',
            'resolved_with_limitations',
        )
        for r in event_results
    ) or limitations:
        overall = 'qualified_with_limitations'
    else:
        overall = 'rattle_free_at_tested_level'

    probe = MechanicalNoiseQualification.model_construct(
        **_canon(
            MechanicalNoiseQualification,
            dict(
                qualification_id='',
                document_id=document_id,
                test_ids=tuple(t.test_id for t in tests),
                event_results=tuple(event_results),
                remediation_action_ids=tuple(
                    a.action_id for a in remediation_actions
                ),
                tested_max_level_db=tested_max,
                project_target_level_db=target,
                overall_verdict=overall,
                limitations=tuple(dict.fromkeys(limitations)),
                evaluated_at_utc=evaluated_at_utc,
                qualification_sha256='',
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return MechanicalNoiseQualification(
        **probe.model_dump(
            exclude={'qualification_id', 'qualification_sha256'}
        ),
        qualification_id=f'mnq:{sha}',
        qualification_sha256=sha,
    )


def _test_level(
    test_id: str, tests: Sequence[MechanicalNoiseTest]
) -> float | None:
    for test in tests:
        if test.test_id == test_id:
            return (
                test.stimulus.level_db_spl
                if test.stimulus.level_db_spl is not None
                else test.stimulus.level_at_mlp_db
            )
    return None


def build_noise_test(
    *,
    document_id: str,
    label: str,
    stimulus: NoiseStressStimulus,
    captured_at_utc: str,
    **fields: Any,
) -> MechanicalNoiseTest:
    probe = MechanicalNoiseTest.model_construct(
        **_canon(
            MechanicalNoiseTest,
            dict(
                test_id='',
                document_id=document_id,
                label=label,
                stimulus=stimulus,
                captured_at_utc=captured_at_utc,
                test_sha256='',
                **fields,
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return MechanicalNoiseTest(
        **probe.model_dump(exclude={'test_id', 'test_sha256'}),
        test_id=f'mnt:{sha}',
        test_sha256=sha,
    )


def build_rattle_event(
    *,
    document_id: str,
    test: MechanicalNoiseTest,
    detected_at_utc: str,
    **fields: Any,
) -> RattleEvent:
    probe = RattleEvent.model_construct(
        **_canon(
            RattleEvent,
            dict(
                event_id='',
                document_id=document_id,
                test_id=test.test_id,
                test_sha256=test.test_sha256,
                detected_at_utc=detected_at_utc,
                event_sha256='',
                **fields,
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return RattleEvent(
        **probe.model_dump(exclude={'event_id', 'event_sha256'}),
        event_id=f'rte:{sha}',
        event_sha256=sha,
    )


def build_remediation_action(
    *,
    document_id: str,
    action_kind: RemediationActionKind,
    performed_at_utc: str,
    event_refs: Sequence[tuple[str, str]] = (),
    **fields: Any,
) -> RemediationAction:
    probe = RemediationAction.model_construct(
        **_canon(
            RemediationAction,
            dict(
                action_id='',
                document_id=document_id,
                action_kind=action_kind,
                performed_at_utc=performed_at_utc,
                event_refs=tuple(event_refs),
                action_sha256='',
                **fields,
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return RemediationAction(
        **probe.model_dump(exclude={'action_id', 'action_sha256'}),
        action_id=f'rma:{sha}',
        action_sha256=sha,
    )


__all__ = [
    'MECHANICAL_NOISE_AUTHORITY_VERSION',
    'MECHANICAL_NOISE_KIND_LABELS',
    'MECHANICAL_NOISE_SCHEMA_VERSION',
    'MECHANICAL_OVERALL_LABELS',
    'RATTLE_LOCALIZATION_LABELS',
    'RATTLE_STRESS_STIMULI',
    'REMEDIATION_ACTION_LABELS',
    'REMEDIATION_VERDICT_LABELS',
    'MechanicalNoiseKind',
    'MechanicalNoiseOverallVerdict',
    'MechanicalNoiseQualification',
    'MechanicalNoiseTest',
    'MechanicalSignalType',
    'NoiseStressStimulus',
    'NoiseTestOperatingState',
    'RattleAcousticEvidence',
    'RattleAttribution',
    'RattleEvent',
    'RattleLocalizationState',
    'RattleRepeatability',
    'RattleSeverityClass',
    'RemediationAction',
    'RemediationActionKind',
    'RemediationEventResult',
    'RemediationEventVerdict',
    'VibrationCalibrationClass',
    'VibrationSensorEvidence',
    'build_noise_test',
    'build_rattle_event',
    'build_remediation_action',
    'evaluate_mechanical_noise',
]
