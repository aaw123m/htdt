"""Active low-frequency MIMO control authority (#973).

Per-channel gain/delay/PEQ (#524) and bass-management routing (#633) are
not MIMO control: an active low-frequency controller is a **matrix of
transfer functions** mapping logical input groups onto physical output
groups so support speakers / subwoofer arrays can reinforce or cancel a
main channel in a declared control band (cf. Dirac ART, Trinnov
WaveForming — both vendor-proprietary, never cloned).

``ActiveLowFrequencyControlPlan`` is the sealed, device-neutral plan:
exact scene/variant + routing bindings, input→output transfer matrix with
per-path gain/delay/filter/latency, control band, objectives, lifecycle
state (proposed → exported → applied → read-back → verified), and the
guardrails that keep support-speaker demand, localization and AV-sync
latency honest.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash, canonicalize_payload






_SHA256_PATTERN = r'^[0-9a-f]{64}$'


ControllerRepresentation = Literal[
    'transfer_matrix',
    'fir_matrix',
    'iir_matrix',
    'gain_delay_matrix',
    'vendor_opaque',
    'unknown',
]

ControlPlanLifecycle = Literal[
    'proposed',
    'exported',
    'applied',
    'read_back',
    'verified',
    'rejected',
    'unknown',
]

ControlObjective = Literal[
    'support_speaker',
    'wavefront_control',
    'decay_control',
    'seat_consistency',
    'hybrid',
    'unknown',
]

FilterRepresentation = Literal['fir', 'iir', 'peq', 'gain_delay', 'opaque', 'none', 'unknown']


class ControlMatrixEntry(BaseModel):
    """One input→output path of the MIMO control matrix."""

    model_config = ConfigDict(frozen=True)

    input_group: str = Field(min_length=1)
    output_group: str = Field(min_length=1)
    enabled: bool = True
    gain_db: float | None = None
    delay_s: float | None = None
    polarity_inverted: bool | None = None
    filter_kind: FilterRepresentation = 'unknown'
    filter_ref: str | None = None
    filter_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    valid_band_hz: tuple[float, float] | None = None
    phase_convention: Literal['minimum', 'linear', 'mixed', 'unknown'] = 'unknown'
    latency_s: float | None = Field(default=None, ge=0.0)

    @model_validator(mode='after')
    def valid_entry(self) -> 'ControlMatrixEntry':
        for label, value in (
            ('gain_db', self.gain_db),
            ('delay_s', self.delay_s),
            ('latency_s', self.latency_s),
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError(f'{label} must be finite')
        if self.valid_band_hz is not None:
            low, high = self.valid_band_hz
            if not (isfinite(low) and isfinite(high)) or not (0 < low < high):
                raise ValueError('valid_band_hz must satisfy 0 < low < high')
        if self.enabled:
            has_effect = (
                self.gain_db is not None
                or self.delay_s is not None
                or self.filter_ref is not None
                or self.filter_kind in ('fir', 'iir', 'peq', 'gain_delay', 'opaque')
                or self.polarity_inverted is not None
            )
            if not has_effect:
                raise ValueError(
                    'an enabled matrix path must carry at least one transfer '
                    'quantity (gain/delay/filter/polarity)'
                )
        return self


class VendorControllerBinding(BaseModel):
    """Opaque vendor controller (e.g. an ART/WaveForming profile export).

    HTDT preserves identity, target band and lifecycle without claiming
    to know the internal filters — ``opaque`` internals are evidence, not
    an emulated algorithm.
    """

    model_config = ConfigDict(frozen=True)

    vendor: str = Field(min_length=1)
    product: str | None = None
    profile_name: str | None = None
    exported_asset_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    declared_control_band_hz: tuple[float, float] | None = None
    internals: Literal['opaque', 'documented', 'unknown'] = 'opaque'


class ActiveLowFrequencyControlPlan(BaseModel):
    """Sealed MIMO control plan for one exact scene/variant/routing."""

    model_config = ConfigDict(frozen=True)

    plan_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=_SHA256_PATTERN)
    system_variant_id: str | None = None
    routing_profile_ref: str | None = None
    bass_management_ref: str | None = None
    logical_input_groups: tuple[str, ...] = Field(min_length=1)
    physical_output_groups: tuple[str, ...] = Field(min_length=1)
    control_band_hz: tuple[float, float]
    representation: ControllerRepresentation = 'unknown'
    matrix: tuple[ControlMatrixEntry, ...] = ()
    vendor_binding: VendorControllerBinding | None = None
    total_latency_s: float | None = Field(default=None, ge=0.0)
    av_sync_compensation_s: float | None = Field(default=None, ge=0.0)
    support_band_upper_limit_hz: float | None = None
    source_capability_refs: tuple[str, ...] = ()
    objectives: tuple[ControlObjective, ...] = ()
    intended_region_ref: str | None = None
    validation_scope: str | None = None
    lifecycle: ControlPlanLifecycle = 'proposed'
    design_producer: str | None = None
    design_version: str | None = None
    created_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_plan(self) -> 'ActiveLowFrequencyControlPlan':
        low, high = self.control_band_hz
        if not (isfinite(low) and isfinite(high)) or not (0 < low < high):
            raise ValueError('control_band_hz must satisfy 0 < low < high')
        inputs = set(self.logical_input_groups)
        outputs = set(self.physical_output_groups)
        if '' in inputs or '' in outputs:
            raise ValueError('group names must be non-empty')
        for entry in self.matrix:
            if entry.input_group not in inputs:
                raise ValueError(
                    f'matrix entry input {entry.input_group!r} is not a '
                    'declared logical input group'
                )
            if entry.output_group not in outputs:
                raise ValueError(
                    f'matrix entry output {entry.output_group!r} is not a '
                    'declared physical output group'
                )
            if entry.valid_band_hz is not None:
                lo, hi = entry.valid_band_hz
                if lo < low - 1e-9 or hi > high + 1e-9:
                    raise ValueError(
                        'matrix entry valid band exceeds the plan control band'
                    )
        if self.representation == 'vendor_opaque' and self.vendor_binding is None:
            raise ValueError('vendor_opaque representation requires vendor_binding')
        if self.representation != 'vendor_opaque' and not self.matrix:
            raise ValueError(
                'a non-opaque control plan requires at least one matrix entry'
            )
        if (
            self.support_band_upper_limit_hz is not None
            and self.support_band_upper_limit_hz <= 0
        ):
            raise ValueError('support_band_upper_limit_hz must be positive')
        for value in (self.total_latency_s, self.av_sync_compensation_s):
            if value is not None and not isfinite(float(value)):
                raise ValueError('latency fields must be finite')
        if self.lifecycle in ('applied', 'read_back', 'verified') and (
            self.representation == 'unknown'
        ):
            raise ValueError('applied lifecycle states require a real representation')
        if self.plan_sha256 != _hash(self.identity_payload()):
            raise ValueError('control plan hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'plan_sha256'})

    def matrix_for(self, input_group: str) -> tuple[ControlMatrixEntry, ...]:
        return tuple(e for e in self.matrix if e.input_group == input_group)


def build_control_plan(**kwargs: Any) -> ActiveLowFrequencyControlPlan:
    """Assemble and seal an :class:`ActiveLowFrequencyControlPlan`."""
    payload = {'plan_sha256': '0' * 64, **kwargs}
    provisional = ActiveLowFrequencyControlPlan.model_construct(**canonicalize_payload(ActiveLowFrequencyControlPlan, dict(**payload)))
    payload['plan_sha256'] = _hash(provisional.identity_payload())
    return ActiveLowFrequencyControlPlan(**payload)


# ---------------------------------------------------------------------------
# Guardrail evaluation
# ---------------------------------------------------------------------------

SupportGuardrailIssue = Literal[
    'support_band_exceeds_localization_limit',
    'missing_source_capability',
    'latency_undisclosed',
    'band_mismatch',
    'unknown',
]


class SupportGuardrailFinding(BaseModel):
    """One guardrail finding — a controller that saves room error by
    overdriving a support speaker is infeasible, never 'better'."""

    model_config = ConfigDict(frozen=True)

    kind: SupportGuardrailIssue
    detail: str = Field(min_length=1)
    blocking: bool = True


def evaluate_support_guardrails(
    plan: ActiveLowFrequencyControlPlan,
    *,
    localization_limit_hz: float | None = None,
    source_capability_declared: bool = False,
) -> tuple[SupportGuardrailFinding, ...]:
    """Static guardrail checks for a control plan.

    - When a localization limit is declared, a support path whose valid
      band extends above it is blocking — "below 150 Hz is always safe" is
      not assumed.
    - Support/cancellation paths increase demand on speakers that may not
      be sized for the band; missing source-capability authority is an
      explicit limitation, not a silent pass.
    - Material latency must be disclosed so acoustic optimization does not
      silently degrade A/V sync.
    """
    findings: list[SupportGuardrailFinding] = []
    effective_limit = plan.support_band_upper_limit_hz or localization_limit_hz
    band_lo, band_hi = plan.control_band_hz
    for entry in plan.matrix:
        if not entry.enabled:
            continue
        if effective_limit is not None:
            entry_hi = (
                entry.valid_band_hz[1] if entry.valid_band_hz else band_hi
            )
            if entry_hi > effective_limit + 1e-9:
                findings.append(
                    SupportGuardrailFinding(
                        kind='support_band_exceeds_localization_limit',
                        detail=(
                            f'{entry.input_group}->{entry.output_group} active '
                            f'to {entry_hi:g} Hz exceeds support-band upper '
                            f'limit {effective_limit:g} Hz'
                        ),
                        blocking=True,
                    )
                )
        if not source_capability_declared:
            findings.append(
                SupportGuardrailFinding(
                    kind='missing_source_capability',
                    detail=(
                        f'{entry.input_group}->{entry.output_group} has no '
                        'source usable-output capability authority; demand '
                        'increase cannot be proven safe'
                    ),
                    blocking=False,
                )
            )
    if plan.total_latency_s is None:
        findings.append(
            SupportGuardrailFinding(
                kind='latency_undisclosed',
                detail='plan does not disclose total latency / AV-sync impact',
                blocking=False,
            )
        )
    if plan.vendor_binding is not None:
        declared = plan.vendor_binding.declared_control_band_hz
        if declared is not None and (
            declared[0] > band_lo + 1e-9 or declared[1] < band_hi - 1e-9
        ):
            findings.append(
                SupportGuardrailFinding(
                    kind='band_mismatch',
                    detail=(
                        'plan control band is not covered by the vendor '
                        'controller declared band'
                    ),
                    blocking=True,
                )
            )
    return tuple(findings)
