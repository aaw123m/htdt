"""Electrical-impedance / Thiele–Small measurement authority (#662).

When manufacturer impedance/load data are missing, inconsistent or
suspect for an installed loudspeaker, a measured Z(f) trace is only
trustworthy with its measurement circuit, calibration state and
boundary conditions pinned. Calibration is part of the measurement
identity; a T/S parameter set is a derived model, never a raw
observation; and a catalog `8 Ω nominal` label never replaces better
measured evidence.

Basis: REW impedance measurement help (calibrated dual-input method,
open/short/reference-load calibration, test-lead compensation,
T/S derivation warnings); IEC 60268-22:2020 (electrical/mechanical
measurements of transducers, small- and large-signal domains).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...cad_authority_resolver import AuthorityRef
from ...canonical_json import canonical_sha256 as _hash, canonicalize_payload


_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{sha_field: digest, id_field: _semantic_id(prefix, digest)},
    )


ImpedanceTargetKind = Literal[
    'complete_passive_loudspeaker_terminals',
    'individual_driver',
    'passive_crossover_network',
    'subwoofer_driver',
    'other_passive_load',
    'active_device_input',
    'unknown',
]

ImpedanceMethod = Literal[
    'calibrated_dual_channel_divider',
    'impedance_bridge',
    'lcr_impedance_analyzer',
    'iec_60268_22_profile',
    'manufacturer_service_method',
    'independent_lab_method',
    'external_rew_import',
    'custom_validated',
    'unknown',
]

CalibrationStep = Literal[
    'open_circuit_cal',
    'short_circuit_cal',
    'reference_load_cal',
    'channel_gain_phase_cal',
    'test_lead_compensation',
    'input_impedance_correction',
]

SignalDomain = Literal[
    'small_signal',
    'large_signal',
    'unknown',
]

LoadEvidenceClass = Literal[
    'measured_complex_load',
    'measured_magnitude_only_load',
    'unknown',
]

ThieleSmallMethod = Literal[
    'added_mass',
    'sealed_box',
    'dual_added_mass',
    'imported',
    'unknown',
]

ModelFitState = Literal[
    'model_fit_acceptable',
    'model_fit_limited',
    'multiple_parameter_sets_plausible',
    'insufficient_low_frequency_data',
    'nonstandard_driver_behavior',
    'large_signal_invalid',
]


class ImpedanceMeasurementProfile(BaseModel):
    """Declared electrical-impedance measurement target + method
    (#662 §1-§3).

    The target identity distinguishes a complete loudspeaker's
    external terminals from an individual driver or crossover
    network — a woofer curve is not the assembled load. An
    active-device input is not speaker-load authority at all.
    Low-energy/safe measurement boundary: unknown or active-input
    targets fail closed.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    target_kind: ImpedanceTargetKind
    method: ImpedanceMethod
    equipment_ref: AuthorityRef | None = None
    instance_ref: AuthorityRef | None = None
    terminal_identity: str | None = None
    crossover_state: str | None = None
    jumper_state: str | None = None
    circuit_description: str | None = None
    source_path: str | None = None
    acquisition_path: str | None = None
    sample_rate_hz: float | None = None
    stimulus_ref: AuthorityRef | None = None
    reference_component_refs: tuple[AuthorityRef, ...] = ()

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('target_kind') in (None, 'unknown'):
                raise ValueError(
                    'the measurement target must be declared — '
                    'complete terminals, driver, network or other '
                    'passive load'
                )
            if data.get('target_kind') == 'active_device_input':
                raise ValueError(
                    'an active device input is a different domain — '
                    'not speaker-load evidence'
                )
            if data.get('method') in (None, 'unknown'):
                raise ValueError(
                    'the measurement method/circuit must be declared '
                    '— ohms alone do not make results comparable'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'ImpedanceMeasurementProfile':
        return _seal(cls, payload, 'profile_id', 'profile_sha256', 'zmp')


class ImpedanceCalibrationState(BaseModel):
    """Calibration that gives a Z(f) measurement its identity
    (#662 §4-§5).

    Open/short/reference-load/lead/channel corrections are sealed
    with the reference component's measured value and uncertainty —
    a typed `100 Ω` nominal is weaker than a measured one, and the
    reference error propagates into the result. Changed leads,
    channel mapping or sample rate stale the calibration.
    """

    model_config = ConfigDict(frozen=True)

    calibration_id: str
    calibration_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    steps: tuple[CalibrationStep, ...]
    reference_load_ohms: float | None = None
    reference_load_uncertainty_ohms: float | None = None
    reference_load_ref: AuthorityRef | None = None
    lead_identity: str | None = None
    interface_identity: str | None = None
    channel_mapping: str | None = None
    sample_rate_hz: float | None = None
    performed_at_utc: str | None = None
    raw_observation_refs: tuple[AuthorityRef, ...] = ()

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('profile_ref') is None:
                raise ValueError(
                    'a calibration state requires the measurement '
                    'profile it calibrates'
                )
            if not data.get('steps'):
                raise ValueError(
                    'a calibration state records the corrections '
                    'actually applied'
                )
            if 'reference_load_cal' in tuple(data.get('steps') or ()):
                if data.get('reference_load_ohms') is None or (
                    data.get('reference_load_uncertainty_ohms') is None
                ):
                    raise ValueError(
                        'a reference-load calibration requires the '
                        'measured resistance and its uncertainty'
                    )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'calibration_id', 'calibration_sha256'},
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'ImpedanceCalibrationState':
        return _seal(
            cls, payload, 'calibration_id', 'calibration_sha256', 'zcs'
        )


class MeasuredLoadEvidence(BaseModel):
    """One measured Z(f) load trace (#662 §6-§10).

    The complex/banded trace is preserved before any scalar summary —
    one `minimum_ohms` never replaces the curve. Signal domain,
    environment and driver state bound applicability.
    """

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    calibration_ref: AuthorityRef | None = None
    evidence_class: LoadEvidenceClass = 'measured_complex_load'
    trace_ref: AuthorityRef | None = None
    magnitude_trace_ref: AuthorityRef | None = None
    phase_trace_ref: AuthorityRef | None = None
    frequency_range_hz: tuple[float, float] | None = None
    effective_resolution_hz: float | None = None
    noise_floor_ohms: float | None = None
    uncertainty_ohms: float | None = None
    signal_domain: SignalDomain = 'small_signal'
    environment_state: str | None = None
    driver_temperature_state: str | None = None
    captured_at_utc: str | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('profile_ref') is None:
                raise ValueError(
                    'measured load evidence requires its measurement '
                    'profile'
                )
            if data.get('evidence_class') in (None, 'unknown'):
                raise ValueError(
                    'the load evidence class must be declared — '
                    'complex vs magnitude-only are different claims'
                )
            if (
                data.get('evidence_class') == 'measured_complex_load'
                and data.get('phase_trace_ref') is None
            ):
                raise ValueError(
                    'a complex load requires the phase trace — '
                    'magnitude alone is magnitude-only evidence'
                )
            if (
                data.get('evidence_class') == 'measured_complex_load'
                and data.get('calibration_ref') is None
            ):
                raise ValueError(
                    'a complex measured load requires its calibration '
                    'state'
                )
            if data.get('signal_domain') in (None, 'unknown'):
                raise ValueError(
                    'the signal domain must be declared — small-signal '
                    'Z(f) is not a large-signal thermal/compression '
                    'model'
                )
            if data.get('trace_ref') is None:
                raise ValueError(
                    'the measured trace must be preserved — a scalar '
                    'summary is not the evidence'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evidence_id', 'evidence_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'MeasuredLoadEvidence':
        return _seal(
            cls, payload, 'evidence_id', 'evidence_sha256', 'zle'
        )


class ThieleSmallDerivation(BaseModel):
    """A derived T/S parameter set (#662 §11-§15).

    T/S parameters are model output, not measured truth: the source
    traces, derivation method, mass/volume evidence, assumptions and
    fit residual ride along. False precision is rejected via the
    model-fit state.
    """

    model_config = ConfigDict(frozen=True)

    derivation_id: str
    derivation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    load_evidence_ref: AuthorityRef
    method: ThieleSmallMethod
    model_fit_state: ModelFitState
    added_mass_g: float | None = None
    added_mass_uncertainty_g: float | None = None
    sealed_volume_l: float | None = None
    sealed_volume_uncertainty_l: float | None = None
    rdc_ohms: float | None = None
    piston_area_m2: float | None = None
    temperature_c: float | None = None
    fit_residual: float | None = None
    parameter_covariance_ref: AuthorityRef | None = None
    parameters: dict[str, float] | None = None
    algorithm_version: str | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('load_evidence_ref') is None:
                raise ValueError(
                    'a T/S derivation requires its source measured '
                    'trace — no opaque calculate-T/S result'
                )
            if data.get('method') in (None, 'unknown'):
                raise ValueError(
                    'the T/S derivation method must be declared'
                )
            if data.get('model_fit_state') is None:
                raise ValueError(
                    'the model-fit state must be declared — '
                    'identifiability limits belong to the result'
                )
            if data.get('method') in ('added_mass', 'dual_added_mass'):
                if (
                    data.get('added_mass_g') is None
                    or data.get('added_mass_uncertainty_g') is None
                ):
                    raise ValueError(
                        'an added-mass derivation requires the mass '
                        'and its uncertainty'
                    )
            if data.get('method') == 'sealed_box' and (
                data.get('sealed_volume_l') is None
                or data.get('sealed_volume_uncertainty_l') is None
            ):
                raise ValueError(
                    'a sealed-box derivation requires the volume and '
                    'its uncertainty'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'derivation_id', 'derivation_sha256'},
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'ThieleSmallDerivation':
        return _seal(
            cls, payload, 'derivation_id', 'derivation_sha256', 'tsd'
        )


LoadClaimVerdict = Literal[
    'qualified_load_evidence',
    'unqualified_profile',
    'calibration_missing',
    'calibration_limited',
    'insufficient_low_frequency_data',
    'derivation_ineligible_target',
    'model_fit_limited',
    'state_limited',
    'no_evidence',
]


def evaluate_load_claim(
    profile: ImpedanceMeasurementProfile | None,
    calibration: ImpedanceCalibrationState | None,
    evidence: MeasuredLoadEvidence | None,
    derivation: ThieleSmallDerivation | None = None,
) -> tuple[LoadClaimVerdict, str]:
    """Judge measured load / T/S evidence for #593 consumption (#662)."""
    if evidence is None:
        return (
            'no_evidence',
            'no measured load record — a catalog nominal label is '
            'not measured evidence',
        )
    if profile is None:
        return (
            'unqualified_profile',
            'no measurement profile — target and method unknown',
        )
    if calibration is None:
        return (
            'calibration_missing',
            'no calibration state — channel/lead/reference errors '
            'are unbounded',
        )
    if derivation is not None:
        if profile.target_kind == (
            'complete_passive_loudspeaker_terminals'
        ):
            return (
                'derivation_ineligible_target',
                'a complete multiway terminal trace cannot produce '
                'single-driver T/S parameters',
            )
        if derivation.model_fit_state == (
            'insufficient_low_frequency_data'
        ):
            return (
                'insufficient_low_frequency_data',
                'low-frequency data cannot resolve the resonance — '
                'the T/S derivation fails closed',
            )
        if derivation.model_fit_state in (
            'model_fit_limited',
            'multiple_parameter_sets_plausible',
            'nonstandard_driver_behavior',
            'large_signal_invalid',
        ):
            return (
                'model_fit_limited',
                'the T/S model fit is limited — precise decimals '
                'are not supported by the inputs',
            )
    if evidence.signal_domain != 'small_signal':
        return (
            'state_limited',
            'non-small-signal load evidence does not feed the '
            'ordinary compatibility model',
        )
    return (
        'qualified_load_evidence',
        'calibrated measured load bound to its profile — usable by '
        'the electrical-compatibility model within its frequency '
        'validity',
    )


LOAD_CLAIM_LABELS: dict[str, str] = {
    'qualified_load_evidence': '適格負荷証拠',
    'unqualified_profile': '測定系未適格',
    'calibration_missing': '校正欠損',
    'calibration_limited': '校正限定',
    'insufficient_low_frequency_data': '低域データ不足',
    'derivation_ineligible_target': '導出対象不適格',
    'model_fit_limited': 'モデル適合限定',
    'state_limited': '状態限定',
    'no_evidence': '証拠なし',
}
