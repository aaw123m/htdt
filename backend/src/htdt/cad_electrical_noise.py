"""Hum / buzz / grounding-EMC diagnosis authority (issue #606).

50/60 Hz hum, harmonics, buzz, shield-current noise, ground-loop
symptoms and EMI/RFI coupling are not the same problems as HVAC noise,
room modes, loudspeaker distortion or mechanical rattles — and bad
troubleshooting advice can create an electrical-safety hazard. This
module versions the low-frequency electrical-noise evidence and
diagnosis layer.

Standards basis (see docs/reviews/rev56-infra.md): AES48-2019
*Interconnections — Grounding and EMC practices — Shields of connectors
in audio equipment containing active circuitry* (shield termination to
the shielding enclosure; the 'pin 1 problem'), AES54-1/-2/-3-2008
(r2019) (portable balanced cables, passive panels/patch fields/
splitters, balanced mic-level outputs; AES54-2 covers shield-current-
induced noise / SCIN). These interconnection standards never replace
mains/protective-earth safety requirements.

Contract properties:

- frequency is a hypothesis, never a cause: a 50/60 Hz fundamental or
  its harmonic family yields a *suspect* classification, never a
  confirmed ground loop (:data:`HypothesisState`);
- electrical hum/buzz stays separate from mechanical rattle (#589) and
  room background noise (#580): a symptom that persists with the
  amplifier muted and tracks a vibrating panel is classified through
  the external authority it belongs to, not here
  (:data:`NoiseTaxonomy` + ``external_authority_ref``);
- shield/interconnect implementation is explicit evidence
  (:class:`AudioInterconnectEvidence`) — an XLR shape never proves an
  AES48-compliant termination; ``assumed_from_connector`` can only
  carry ``unknown`` shield state;
- the protective-earth boundary is enforced at the model level:
  isolation steps and mitigations carry ``protective_earth_preserved``
  as a required ``True`` literal, mitigation kinds form a closed set
  with no earth-defeat option, and evidence pointing at building
  power/earthing produces ``refer_to_qualified_electrician`` — never
  procedural bypass instructions;
- bounded divide-and-isolate testing is diagnostic evidence
  (:class:`NoiseIsolationTest`); it never overwrites the as-built
  interconnect authority;
- resolution requires the same measurement repeated: a mitigation
  without an identical post-intervention capture is
  ``not_evaluated``/``open``, never 'fixed';
- fail-closed: no isolation evidence at all means
  ``insufficient_evidence`` — an unheard system is not a clean system.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_equipment import EquipmentDataProvenance
from .canonical_json import (
    canonical_sha256 as _digest,
    canonicalize_payload as _canon,
)
from .clock import utc_now_iso as _utc_now


_SHA256 = r'^[0-9a-f]{64}$'

ELECTRICAL_NOISE_SCHEMA_VERSION = 1

NOISE_OBSERVATION_AUTHORITY_VERSION = 'rev56-humbuzz-observation-1'
INTERCONNECT_AUTHORITY_VERSION = 'rev56-humbuzz-interconnect-1'
ISOLATION_TEST_AUTHORITY_VERSION = 'rev56-humbuzz-isolation-1'
DIAGNOSTIC_AUTHORITY_VERSION = 'rev56-humbuzz-diagnostic-1'
MITIGATION_AUTHORITY_VERSION = 'rev56-humbuzz-mitigation-1'
HUMBUZZ_VERDICT_AUTHORITY_VERSION = 'rev56-humbuzz-verdict-1'
HUMBUZZ_EVALUATION_VERSION = 'rev56-humbuzz-eval-1'


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _require_iso8601(value: str, label: str) -> None:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _finite(value: float, label: str) -> float:
    if not isfinite(value):
        raise ValueError(f'{label} must be finite')
    return value


# ---------------------------------------------------------------------------
# Taxonomies
# ---------------------------------------------------------------------------

NoiseTaxonomy = Literal[
    'mains_fundamental_hum',
    'mains_harmonic_buzz',
    'switching_supply_noise',
    'shield_current_scin_suspected',
    'ground_loop_suspected',
    'rf_emi_coupling',
    'digital_usb_computer_noise',
    'dimmer_lighting_noise',
    'cable_connector_intermittent',
    'device_self_noise',
    'mechanical_suspected',
    'acoustic_background_suspected',
    'unknown_electrical_noise',
]
"""Low-frequency noise taxonomy (#606 §1/§11). ``*_suspected`` classes
stay suspected until evidence supports stronger attribution; the
``mechanical_*``/``acoustic_*`` classes mark a symptom that belongs to
the #589/#580 authorities rather than to an electrical cause."""

InstrumentClass = Literal[
    'acoustic_microphone_spectrum',
    'electrical_line_capture',
    'electrical_io_capture',
    'fft_analyzer',
    'device_readback',
    'listening_report',
    'other',
    'unknown',
]
"""Measurement instrument class (#606 §2) — a microphone capture proves
an audible artifact, not where it enters the electrical path."""

SpectralComponentKind = Literal[
    'mains_fundamental',
    'harmonic',
    'broadband',
    'switching_component',
    'modulated',
    'tonal_other',
    'unknown',
]

SymptomLabel = Literal[
    'hum', 'buzz', 'hiss', 'whine', 'broadband_hash',
    'intermittent', 'other', 'unknown',
]

MuteState = Literal['muted', 'unmuted', 'partial', 'unknown']

CorrelationFactor = Literal[
    'lighting_dimmer',
    'led_driver',
    'hvac_state',
    'device_power_state',
    'network_activity',
    'motor_shade',
    'time_of_day',
    'other',
]
"""Factors whose correlation with the noise is recorded (#606 §12/§13)
— a recorded correlation is a hypothesis input, never automatic
causality."""

CorrelationResult = Literal['changes', 'unchanged', 'untested']

InterfaceClass = Literal[
    'balanced_confirmed',
    'balanced_declared',
    'unbalanced',
    'transformer_isolated',
    'differential_other',
    'unknown',
]
"""Electrical interface class (#606 §6). A balanced connector format
never guarantees a correctly implemented balanced circuit;
``balanced_confirmed`` requires evidence beyond connector shape."""

ConnectorForm = Literal[
    'xlr', 'trs_quarter', 'rca', 'speakon', 'terminal_block',
    'bare_wire', 'other', 'unknown',
]

ShieldTermination = Literal[
    'chassis_at_entry',
    'circuit_reference',
    'both_ends_chassis',
    'single_end',
    'floating',
    'not_shielded',
    'unknown',
]
"""Shield termination class (#606 §5). ``chassis_at_entry`` is the
AES48-required termination (designated shield contact to the shielding
enclosure, lowest practical impedance); it is only honest with
inspection or manufacturer evidence, never with connector-shape
assumption."""

ShieldTerminationBasis = Literal[
    'manufacturer_doc', 'inspected', 'assumed_from_connector', 'unknown'
]

IsolationStepKind = Literal[
    'mute_upstream_stage',
    'disconnect_authorized_input',
    'substitute_known_good_cable',
    'substitute_balanced_path',
    'analog_vs_digital_compare',
    'channel_compare',
    'device_power_state_compare',
    'bypass_optional_device',
    'gain_stage_move',
    'other',
]
"""Bounded divide-and-isolate steps (#606 §4). Each is a temporary
diagnostic configuration — never a rewrite of the as-built path."""

IsolationStepOutcome = Literal[
    'noise_present', 'noise_reduced', 'noise_absent',
    'no_change', 'untested',
]

HypothesisState = Literal[
    'suspected',
    'supported_by_path_test',
    'confirmed_by_safe_intervention',
    'unresolved',
    'classified_external',
]
"""Ground-loop/noise attribution states (#606 §7). ``confirmed`` is
reachable only through a safe, authorized intervention with repeated
measurement."""

SuspectFactor = Literal[
    'ground_loop',
    'emi_pickup',
    'dimmer_lighting',
    'switching_supply',
    'mechanical_vibration',
    'gain_structure',
    'building_earthing',
    'device_fault',
    'cable_shield_defect',
    'digital_coupling',
    'unknown',
]
"""Suspect cause factors — several may coexist; each is a hypothesis
until isolated evidence supports it."""

DiagnosticRecommendation = Literal[
    'refer_to_qualified_electrician',
    'apply_safe_mitigation',
    'continue_isolation',
    'monitor',
    'no_action',
    'unknown',
]
"""What the diagnosis supports doing next. Evidence pointing to
building power/earthing produces the electrician referral and nothing
else."""

MitigationKind = Literal[
    'correct_shield_termination',
    'balanced_interface',
    'audio_isolation_transformer',
    'differential_isolated_input',
    'cable_connector_repair',
    'routing_separation_change',
    'faulty_device_service',
    'dimmer_emi_source_remediation',
    'power_earthing_professional_review',
    'other',
]
"""Safe mitigation classes (#606 §9) — a closed set by construction:
no protective-earth defeat, cheater plug, or mains-wiring workaround
can be expressed."""

MitigationOutcome = Literal[
    'noise_resolved',
    'noise_reduced',
    'no_change',
    'noise_worse',
    'new_artifact_introduced',
    'not_evaluated',
]

HumBuzzVerdictState = Literal[
    'resolved_confirmed',
    'resolved_with_limitations',
    'referred_to_electrician',
    'open_unresolved',
    'classified_external',
    'insufficient_evidence',
]
"""Final per-diagnostic verdict (#606 §14) — baseline failure evidence
is never deleted or rewritten."""


# ---------------------------------------------------------------------------
# JA display labels
# ---------------------------------------------------------------------------

NOISE_TAXONOMY_LABELS = {
    'mains_fundamental_hum': '電源基波ハム',
    'mains_harmonic_buzz': '電源高調波バズ',
    'switching_supply_noise': 'スイッチング電源ノイズ',
    'shield_current_scin_suspected': 'シールド電流誘起（疑い）',
    'ground_loop_suspected': 'グラウンドループ（疑い）',
    'rf_emi_coupling': 'RF/EMI結合',
    'digital_usb_computer_noise': 'デジタル/USB/PC由来ノイズ',
    'dimmer_lighting_noise': '調光器・照明ノイズ',
    'cable_connector_intermittent': 'ケーブル/コネクタ断続',
    'device_self_noise': '機器自己ノイズ',
    'mechanical_suspected': '機械振動（疑い・#589へ）',
    'acoustic_background_suspected': '空調・背景騒音（疑い・#580へ）',
    'unknown_electrical_noise': '不明な電気ノイズ',
}

INTERFACE_CLASS_LABELS = {
    'balanced_confirmed': 'バランス（確認済み）',
    'balanced_declared': 'バランス（宣言）',
    'unbalanced': 'アンバランス',
    'transformer_isolated': 'トランス絶縁',
    'differential_other': '差動・その他',
    'unknown': '不明',
}

HYPOTHESIS_STATE_LABELS = {
    'suspected': '疑いあり',
    'supported_by_path_test': 'パス試験で裏付け',
    'confirmed_by_safe_intervention': '安全な介入で確認',
    'unresolved': '未解決',
    'classified_external': '外部権威へ分類',
}

MITIGATION_KIND_LABELS = {
    'correct_shield_termination': 'シールド終端の是正',
    'balanced_interface': 'バランスインターフェース化',
    'audio_isolation_transformer': 'オーディオ絶縁トランス',
    'differential_isolated_input': '差動/絶縁入力',
    'cable_connector_repair': 'ケーブル/コネクタ修理',
    'routing_separation_change': '配線経路・分離変更',
    'faulty_device_service': '故障機器の修理',
    'dimmer_emi_source_remediation': '調光器/EMI源の是正',
    'power_earthing_professional_review': '電源/接地の専門家レビュー',
    'other': 'その他',
}

HUMBUZZ_VERDICT_LABELS = {
    'resolved_confirmed': '解消確認済み',
    'resolved_with_limitations': '制限付き解消',
    'referred_to_electrician': '電気専門家へ照会',
    'open_unresolved': '未解決（継続）',
    'classified_external': '外部権威へ分類',
    'insufficient_evidence': '証拠不足',
}


# ---------------------------------------------------------------------------
# Noise observation (#606 §2/§3/§12/§13)
# ---------------------------------------------------------------------------


class SpectralComponent(BaseModel):
    """One observed spectral component of the noise."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    freq_hz: float = Field(gt=0.0)
    kind: SpectralComponentKind = 'unknown'
    level_db: float | None = None
    """Level under the capture's own reference — its scale is the
    instrument's, never a normalized loudness."""

    @model_validator(mode='after')
    def _check(self) -> 'SpectralComponent':
        _finite(self.freq_hz, 'freq_hz')
        if self.level_db is not None:
            _finite(self.level_db, 'level_db')
        return self


class CorrelationRecord(BaseModel):
    """A recorded correlation between noise and a building/device
    factor (#606 §12/§13) — hypothesis input, never auto-causality."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    factor: CorrelationFactor
    state_label: str = Field(min_length=1)
    """e.g. 'dimmer 40%', 'hvac fan on', 'shade motor running'."""
    result: CorrelationResult = 'untested'

    @model_validator(mode='after')
    def _check(self) -> 'CorrelationRecord':
        return self


class ElectricalNoiseObservation(BaseModel):
    """One captured noise observation (#606 §2/§14).

    Carries exact channel/path/state/instrument evidence; spectral
    components are optional — a symptom-only record is honest.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = ELECTRICAL_NOISE_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-humbuzz-observation-1'
    ] = NOISE_OBSERVATION_AUTHORITY_VERSION
    observation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    affected_refs: tuple[str, ...] = ()
    """Channel/device/path labels the noise was heard/measured on."""
    location_note: str | None = None
    symptom: SymptomLabel = 'unknown'
    components: tuple[SpectralComponent, ...] = ()
    instrument: InstrumentClass = 'unknown'
    instrument_detail: str | None = None
    calibration_ref: str | None = None
    mute_state: MuteState = 'unknown'
    gain_position_note: str | None = None
    source_state: str | None = None
    power_state: str | None = None
    correlations: tuple[CorrelationRecord, ...] = ()
    measurement_state_ref: AuthorityRef | None = None
    """Optional #573 state-snapshot pin — instrument/timebase/state
    stay explicit through the measurement-state authority."""
    captured_at_utc: str = Field(min_length=1)
    observation_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'observation_id', 'observation_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'ElectricalNoiseObservation':
        _require_iso8601(self.captured_at_utc, 'captured_at_utc')
        digest = _digest(self.identity_payload())
        if self.observation_sha256 != digest:
            raise ValueError('noise observation hash mismatch')
        if self.observation_id != _semantic_id('eobs', digest):
            raise ValueError('noise observation id mismatch')
        return self


# ---------------------------------------------------------------------------
# Interconnect / shield evidence (#606 §5/§6)
# ---------------------------------------------------------------------------


class AudioInterconnectEvidence(BaseModel):
    """Exact signal-interconnect evidence for one link (#606 §5/§6).

    Composes with #597 physical-cabling authority via ``path_ref`` —
    this record carries the electrical interface and shield semantics,
    not a re-drawn cable route.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = ELECTRICAL_NOISE_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-humbuzz-interconnect-1'
    ] = INTERCONNECT_AUTHORITY_VERSION
    interconnect_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    path_ref: AuthorityRef | None = None
    label: str = Field(min_length=1)
    from_device_label: str | None = None
    to_device_label: str | None = None
    interface_class: InterfaceClass = 'unknown'
    connector_form: ConnectorForm = 'unknown'
    shield_termination: ShieldTermination = 'unknown'
    shield_termination_basis: ShieldTerminationBasis = 'unknown'
    cable_type: str | None = None
    panel_devices: tuple[str, ...] = ()
    aes_profile_refs: tuple[str, ...] = ()
    """#599 registry keys pinning the exact AES48/AES54 editions — e.g.
    'aes48@2019', 'aes54-2@2008-r2019'."""
    notes: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    declared_at_utc: str = Field(min_length=1)
    interconnect_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'interconnect_id', 'interconnect_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'AudioInterconnectEvidence':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        if self.path_ref is not None and self.path_ref.kind not in (
            'physical_interconnect', 'cable_path'
        ):
            raise ValueError(
                "path_ref must pin a physical-interconnect authority"
            )
        if (
            self.shield_termination_basis == 'assumed_from_connector'
            and self.shield_termination != 'unknown'
        ):
            raise ValueError(
                'connector shape cannot evidence a shield termination '
                "— 'assumed_from_connector' may only carry 'unknown'"
            )
        if (
            self.shield_termination == 'chassis_at_entry'
            and self.shield_termination_basis
            not in ('manufacturer_doc', 'inspected')
        ):
            raise ValueError(
                'an AES48-style chassis-at-entry termination claim '
                'requires manufacturer documentation or inspection — '
                'it is never inferred from an XLR shell'
            )
        if self.interface_class == 'balanced_confirmed' and (
            self.shield_termination_basis == 'assumed_from_connector'
        ):
            raise ValueError(
                "'balanced_confirmed' cannot rest on connector-shape "
                'assumption'
            )
        digest = _digest(self.identity_payload())
        if self.interconnect_sha256 != digest:
            raise ValueError('interconnect evidence hash mismatch')
        if self.interconnect_id != _semantic_id('icnx', digest):
            raise ValueError('interconnect evidence id mismatch')
        return self


# ---------------------------------------------------------------------------
# Bounded isolation testing (#606 §4/§7/§8)
# ---------------------------------------------------------------------------


class IsolationStep(BaseModel):
    """One bounded diagnostic step.

    ``protective_earth_preserved`` is a required ``True`` literal — a
    step that removed or defeated protective earth cannot be recorded
    as a diagnostic at all.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: IsolationStepKind
    target_label: str = Field(min_length=1)
    outcome: IsolationStepOutcome = 'untested'
    note: str | None = None
    protective_earth_preserved: Literal[True] = True

    @model_validator(mode='after')
    def _check(self) -> 'IsolationStep':
        return self


class NoiseIsolationTest(BaseModel):
    """A bounded divide-and-isolate diagnostic run (#606 §4).

    Steps are temporary diagnostic evidence — they never overwrite the
    as-built interconnect authority.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = ELECTRICAL_NOISE_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-humbuzz-isolation-1'
    ] = ISOLATION_TEST_AUTHORITY_VERSION
    test_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    observation_ref: AuthorityRef
    steps: tuple[IsolationStep, ...]
    summary_note: str | None = None
    performed_at_utc: str = Field(min_length=1)
    test_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'test_id', 'test_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'NoiseIsolationTest':
        _require_iso8601(self.performed_at_utc, 'performed_at_utc')
        if self.observation_ref.kind != 'electrical_noise_observation':
            raise ValueError(
                "observation_ref must pin an "
                "'electrical_noise_observation' authority"
            )
        if not self.steps:
            raise ValueError(
                'an isolation test requires at least one step'
            )
        digest = _digest(self.identity_payload())
        if self.test_sha256 != digest:
            raise ValueError('isolation test hash mismatch')
        if self.test_id != _semantic_id('itest', digest):
            raise ValueError('isolation test id mismatch')
        return self


# ---------------------------------------------------------------------------
# Diagnosis (#606 §1/§7/§10)
# ---------------------------------------------------------------------------


class HumBuzzDiagnostic(BaseModel):
    """Bound diagnosis over captured observations (#606 §7/§14).

    ``classification`` is the noise taxonomy; ``suspect_factors`` are
    the live hypotheses. A frequency pattern alone never promotes a
    hypothesis past ``suspected``.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = ELECTRICAL_NOISE_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-humbuzz-diagnostic-1'
    ] = DIAGNOSTIC_AUTHORITY_VERSION
    diagnostic_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    observation_refs: tuple[str, ...]
    classification: NoiseTaxonomy = 'unknown_electrical_noise'
    hypothesis_state: HypothesisState = 'suspected'
    suspect_factors: tuple[SuspectFactor, ...] = ()
    external_authority_ref: AuthorityRef | None = None
    """Pin to the #589 rattle / #580 background record when the
    symptom is classified outside this authority."""
    gain_structure_suspected: bool = False
    """#593 composition flag — kept separate from any grounding-fault
    claim; masking hum by gain change is not a fix."""
    frequency_pattern_note: str | None = None
    recommendation: DiagnosticRecommendation = 'unknown'
    isolation_test_refs: tuple[str, ...] = ()
    confirmed_at_utc: str | None = None
    created_at_utc: str = Field(min_length=1)
    diagnostic_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'diagnostic_id', 'diagnostic_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'HumBuzzDiagnostic':
        _require_iso8601(self.created_at_utc, 'created_at_utc')
        if self.confirmed_at_utc is not None:
            _require_iso8601(self.confirmed_at_utc, 'confirmed_at_utc')
        if not self.observation_refs:
            raise ValueError(
                'a diagnostic requires at least one bound observation'
            )
        if len(set(self.suspect_factors)) != len(self.suspect_factors):
            raise ValueError('duplicate suspect factors')
        if self.hypothesis_state == 'confirmed_by_safe_intervention' and (
            self.confirmed_at_utc is None
        ):
            raise ValueError(
                "'confirmed_by_safe_intervention' requires "
                'confirmed_at_utc — the intervention record carries '
                'the repeat measurement'
            )
        if self.hypothesis_state == 'classified_external' and (
            self.external_authority_ref is None
        ):
            raise ValueError(
                "'classified_external' requires the external authority "
                'pin (#580/#589 record) it was classified into'
            )
        if 'building_earthing' in self.suspect_factors and (
            self.recommendation != 'refer_to_qualified_electrician'
        ):
            raise ValueError(
                'evidence pointing at building power/earthing produces '
                "'refer_to_qualified_electrician' — HTDT never issues "
                'protective-earth bypass instructions'
            )
        digest = _digest(self.identity_payload())
        if self.diagnostic_sha256 != digest:
            raise ValueError('diagnostic hash mismatch')
        if self.diagnostic_id != _semantic_id('hdiag', digest):
            raise ValueError('diagnostic id mismatch')
        return self


# ---------------------------------------------------------------------------
# Safe mitigation (#606 §8/§9)
# ---------------------------------------------------------------------------


class NoiseMitigationAttempt(BaseModel):
    """One safe, authorized intervention and its repeat measurement.

    ``protective_earth_preserved`` is a required ``True`` literal; the
    kind set is closed so no earth-defeat option exists. ``after_ref``
    pins the identical-capture repeat observation — resolution without
    one is not evidence.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = ELECTRICAL_NOISE_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-humbuzz-mitigation-1'
    ] = MITIGATION_AUTHORITY_VERSION
    attempt_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    diagnostic_ref: str = Field(min_length=1)
    """HumBuzzDiagnostic identity."""
    kind: MitigationKind
    detail: str = Field(min_length=1)
    protective_earth_preserved: Literal[True] = True
    before_ref: str = Field(min_length=1)
    """Baseline ElectricalNoiseObservation identity — never deleted."""
    after_ref: str | None = None
    """Repeat ElectricalNoiseObservation identity under identical
    capture state."""
    outcome: MitigationOutcome = 'not_evaluated'
    performed_by: str | None = None
    performed_at_utc: str = Field(min_length=1)
    attempt_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'attempt_id', 'attempt_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'NoiseMitigationAttempt':
        _require_iso8601(self.performed_at_utc, 'performed_at_utc')
        if self.outcome in (
            'noise_resolved', 'noise_reduced', 'no_change',
            'noise_worse', 'new_artifact_introduced',
        ) and self.after_ref is None:
            raise ValueError(
                'an evaluated outcome requires the identical-capture '
                'repeat observation (after_ref) — an unmeasured fix '
                'is not a fix'
            )
        if self.outcome == 'not_evaluated' and self.after_ref is not None:
            raise ValueError(
                "'not_evaluated' cannot carry an after observation — "
                'record the measured outcome'
            )
        digest = _digest(self.identity_payload())
        if self.attempt_sha256 != digest:
            raise ValueError('mitigation attempt hash mismatch')
        if self.attempt_id != _semantic_id('hmit', digest):
            raise ValueError('mitigation attempt id mismatch')
        return self


# ---------------------------------------------------------------------------
# Verdict (#606 §14)
# ---------------------------------------------------------------------------


class HumBuzzVerdict(BaseModel):
    """Sealed verdict for one diagnostic. Produced only by
    :func:`evaluate_humbuzz`."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = ELECTRICAL_NOISE_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-humbuzz-verdict-1'
    ] = HUMBUZZ_VERDICT_AUTHORITY_VERSION
    verdict_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    diagnostic_ref: str = Field(min_length=1)
    evaluation_version: str = Field(min_length=1)
    state: HumBuzzVerdictState
    reasons: tuple[str, ...] = ()
    mitigation_refs: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    verdict_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'verdict_id', 'verdict_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'HumBuzzVerdict':
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        digest = _digest(self.identity_payload())
        if self.verdict_sha256 != digest:
            raise ValueError('verdict hash mismatch')
        if self.verdict_id != _semantic_id('hverd', digest):
            raise ValueError('verdict id mismatch')
        return self


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _build_sealed(model: type[BaseModel], sha_field: str,
                  id_field: str, prefix: str,
                  authority_version: str,
                  **kwargs: Any) -> Any:
    probe = model.model_construct(
        **_canon(
            model,
            dict(
                schema_version=ELECTRICAL_NOISE_SCHEMA_VERSION,
                authority_version=authority_version,
                **{sha_field: '0' * 64, id_field: ''},
                **kwargs,
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={sha_field, id_field}),
        **{sha_field: sha, id_field: _semantic_id(prefix, sha)},
    )


def build_noise_observation(**kwargs: Any) -> ElectricalNoiseObservation:
    return _build_sealed(
        ElectricalNoiseObservation, 'observation_sha256',
        'observation_id', 'eobs', NOISE_OBSERVATION_AUTHORITY_VERSION,
        **kwargs,
    )


def build_interconnect(**kwargs: Any) -> AudioInterconnectEvidence:
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _build_sealed(
        AudioInterconnectEvidence, 'interconnect_sha256',
        'interconnect_id', 'icnx', INTERCONNECT_AUTHORITY_VERSION,
        **kwargs,
    )


def build_isolation_test(**kwargs: Any) -> NoiseIsolationTest:
    return _build_sealed(
        NoiseIsolationTest, 'test_sha256', 'test_id', 'itest',
        ISOLATION_TEST_AUTHORITY_VERSION, **kwargs,
    )


def build_diagnostic(**kwargs: Any) -> HumBuzzDiagnostic:
    kwargs.setdefault('created_at_utc', _utc_now())
    return _build_sealed(
        HumBuzzDiagnostic, 'diagnostic_sha256', 'diagnostic_id',
        'hdiag', DIAGNOSTIC_AUTHORITY_VERSION, **kwargs,
    )


def build_mitigation_attempt(**kwargs: Any) -> NoiseMitigationAttempt:
    return _build_sealed(
        NoiseMitigationAttempt, 'attempt_sha256', 'attempt_id',
        'hmit', MITIGATION_AUTHORITY_VERSION, **kwargs,
    )


# ---------------------------------------------------------------------------
# Evaluation (#606 §3/§7/§14)
# ---------------------------------------------------------------------------


def _classify_from_spectrum(
    observation: ElectricalNoiseObservation,
) -> NoiseTaxonomy:
    """Hypothesis-only classification from the spectral pattern.

    Mains-fundamental and harmonic families are classified by the
    pattern, never by a hard-coded region frequency — the project
    region's mains frequency is caller context, not a constant.
    """
    kinds = {c.kind for c in observation.components}
    if 'switching_component' in kinds or 'broadband' in kinds:
        return 'switching_supply_noise'
    if 'modulated' in kinds:
        return 'dimmer_lighting_noise'
    if 'harmonic' in kinds and 'mains_fundamental' in kinds:
        return 'mains_harmonic_buzz'
    if 'mains_fundamental' in kinds:
        return 'mains_fundamental_hum'
    if 'harmonic' in kinds:
        return 'mains_harmonic_buzz'
    return 'unknown_electrical_noise'


def evaluate_humbuzz(
    *,
    diagnostic: HumBuzzDiagnostic,
    observations: Sequence[ElectricalNoiseObservation],
    isolation_tests: Sequence[NoiseIsolationTest] = (),
    mitigations: Sequence[NoiseMitigationAttempt] = (),
    evaluated_at_utc: str,
) -> HumBuzzVerdict:
    """Fail-closed verdict for one diagnostic (#606 §14).

    Resolution requires the identical-capture repeat observation on a
    safe mitigation; evidence pointing at building earthing produces
    the electrician referral and nothing else.
    """
    reasons: list[str] = []
    bound_obs = [
        o for o in observations if o.observation_id in diagnostic.observation_refs
    ]
    if diagnostic.classification == 'unknown_electrical_noise' and bound_obs:
        suggested = _classify_from_spectrum(bound_obs[-1])
        if suggested != 'unknown_electrical_noise':
            reasons.append(
                'spectral pattern suggests a hypothesis class '
                f'({suggested}) — pattern alone never confirms cause'
            )
    bound_tests = [
        t for t in isolation_tests
        if t.observation_ref.ref_id in diagnostic.observation_refs
        or t.test_id in diagnostic.isolation_test_refs
    ]
    bound_mitigations = [
        m for m in mitigations if m.diagnostic_ref == diagnostic.diagnostic_id
    ]

    if diagnostic.hypothesis_state == 'classified_external':
        reasons.append(
            'symptom classified into an external authority '
            f'({diagnostic.external_authority_ref.kind if diagnostic.external_authority_ref else "?"}) — '
            'electrical diagnosis closed without a ground-fault claim'
        )
        state: HumBuzzVerdictState = 'classified_external'
    elif diagnostic.recommendation == 'refer_to_qualified_electrician':
        reasons.append(
            'evidence points outside the signal interconnect — building '
            'power/earthing is referred to a qualified electrician with '
            'no procedural bypass'
        )
        state = 'referred_to_electrician'
    elif bound_mitigations:
        resolved = [
            m for m in bound_mitigations
            if m.outcome == 'noise_resolved' and m.after_ref is not None
        ]
        reduced = [
            m for m in bound_mitigations
            if m.outcome == 'noise_reduced' and m.after_ref is not None
        ]
        if resolved:
            if diagnostic.hypothesis_state == 'confirmed_by_safe_intervention':
                reasons.append(
                    'safe intervention resolved the noise under the '
                    'identical repeated capture'
                )
                state = 'resolved_confirmed'
            else:
                reasons.append(
                    'noise resolved by mitigation but the diagnostic '
                    'was never marked confirmed — result stays '
                    'limited'
                )
                state = 'resolved_with_limitations'
        elif reduced:
            reasons.append(
                'mitigation reduced but did not resolve the noise'
            )
            state = 'resolved_with_limitations'
        else:
            state = 'open_unresolved'
            reasons.append(
                'mitigation applied without a resolving repeat '
                'measurement'
            )
    elif bound_tests:
        if diagnostic.hypothesis_state == 'supported_by_path_test':
            reasons.append(
                'isolation evidence supports the hypothesis — no '
                'safe-intervention confirmation yet'
            )
            state = 'open_unresolved'
        else:
            state = 'open_unresolved'
    else:
        reasons.append(
            'no isolation test or mitigation bound to the diagnostic — '
            'a symptom capture alone cannot confirm a cause'
        )
        state = 'insufficient_evidence'

    probe = HumBuzzVerdict.model_construct(
        **_canon(
            HumBuzzVerdict,
            dict(
                schema_version=ELECTRICAL_NOISE_SCHEMA_VERSION,
                authority_version=HUMBUZZ_VERDICT_AUTHORITY_VERSION,
                verdict_id='',
                document_id=diagnostic.document_id,
                diagnostic_ref=diagnostic.diagnostic_id,
                evaluation_version=HUMBUZZ_EVALUATION_VERSION,
                state=state,
                reasons=tuple(reasons),
                mitigation_refs=tuple(
                    m.attempt_id for m in bound_mitigations
                ),
                evaluated_at_utc=evaluated_at_utc,
                verdict_sha256='0' * 64,
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return HumBuzzVerdict(
        **probe.model_dump(
            mode='python', exclude={'verdict_id', 'verdict_sha256'}
        ),
        verdict_id=_semantic_id('hverd', sha),
        verdict_sha256=sha,
    )


__all__ = [name for name in dir() if not name.startswith('_')]
