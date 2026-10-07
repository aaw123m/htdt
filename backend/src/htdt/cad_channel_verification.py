"""Automated channel identity / routing / polarity verification (#876).

Drives the landed #869 native sweep acquisition engine channel-by-channel
to verify the declared logical→physical loudspeaker map (#621) and relative
polarity *before* room correction or the #868 commissioning loop.

    CHANNEL VERIFICATION PLAN (per-channel routing signatures pinned)
      -> per-channel EXCITATION RUN (exact #869 acquisition binding)
      -> SIGNATURE ANALYSIS (arrival / level / polarity / duplicate)
      -> CHANNEL VERIFICATION VERDICT (evidence class per channel)
      -> STALENESS on routing/config change (only affected channels)

Evidence classes (the issue's ladder — never promoted upward):

* ``machine_verified_routing`` — qualified capture + expected response on
  the bound route + no duplicate signature across channels.
* ``machine_verified_relative_polarity`` — routing verified AND relative
  polarity consistent with the declared expectation versus the plan's
  reference channel. Relative only — absolute polarity is never claimed.
* ``machine_assisted_ambiguous`` — a response exists but the signature
  cannot distinguish the binding (duplicate signature, level mismatch).
* ``operator_attested`` — operator confirmation recorded via
  :class:`OperatorChannelAttestation`; permanently manual evidence.
* ``unknown`` — no usable evidence (missing/invalid capture, no result).

Fail-closed: ``unknown`` is not success; a detected defect (missing
output, duplicate routing, inverted polarity, severe level mismatch) is a
defect flag on the channel verdict, and the map-level state never reports
``verified`` while any channel is below ``machine_verified_routing``.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Literal, Mapping, Sequence

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_channel_identity_authority import CadChannelIdentityChain
from .cad_sweep_acquisition import (
    AcquisitionRequest,
    ArmConfirmation,
    AudioIOBackend,
    ChannelRouting,
    LevelSafetyPolicy,
    MeasurementAcquisitionEngine,
    SweepStimulusSpec,
)
from .cad_sweep_acquisition_evidence import (
    build_acquisition_run,
    build_stimulus_definition,
)
from .canonical_json import canonical_sha256 as _hash

CHANNEL_VERIFICATION_VERSION = '1'
"""Authority/schema identity stamped on every record — bump when the
identity payload or analysis semantics change."""

ANALYSIS_VERSION = 'cvsig-1'
"""Version pin for the response-signature derivation; recorded on each
:meth:`ChannelExcitationResult` so a later algorithm change cannot be
confused with the old evidence."""

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _require_iso8601(value: str, field: str) -> None:
    from datetime import datetime

    try:
        parsed = datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
    except (TypeError, ValueError):
        raise ValueError(f'{field} must be UTC ISO-8601 (…Z)') from None
    if not isinstance(value, str) or not value.endswith('Z'):
        raise ValueError(f'{field} must be UTC ISO-8601 (…Z)')
    _ = parsed


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

VerificationEvidenceClass = Literal[
    'machine_verified_routing',
    'machine_verified_relative_polarity',
    'machine_assisted_ambiguous',
    'operator_attested',
    'unknown',
]
"""Per-channel evidence class. ``operator_attested`` is terminal for
machine purposes — it is never promoted into a machine class."""

ChannelRoutingState = Literal[
    'verified',
    'missing_output',
    'duplicate_suspect',
    'level_mismatch',
    'unexpected_response',
    'unverified',
]
"""Observed routing outcome for one channel. ``verified`` requires a
qualified capture whose signature matches the expectation exclusively."""

ChannelPolarityState = Literal[
    'consistent', 'inverted', 'ambiguous', 'unevaluated'
]
"""Relative polarity versus the plan's reference channel. ``unevaluated``
when no expectation was declared or the method cannot support it."""

MapVerificationState = Literal[
    'verified', 'failed', 'ambiguous', 'incomplete', 'unknown'
]
"""Plan-level state. ``verified`` requires every target channel at
``machine_verified_routing`` or better. ``failed`` when any channel holds
a machine-detected defect; ``ambiguous`` when machine evidence exists but
cannot distinguish the binding; ``incomplete`` when results are missing;
``unknown`` when no channel has any usable evidence."""

DefectFlag = Literal[
    'capture_invalid',
    'response_missing',
    'duplicate_signature',
    'level_mismatch',
    'polarity_inverted',
    'timing_unqualified',
    'simulated_backend',
]
"""Machine-detected conditions. Any flag blocks the ``verified`` states;
``simulated_backend`` additionally caps every class at manual-tier and is
always surfaced — fake-backend evidence never pretends to be physical."""

VerificationMethod = Literal['native_sweep_arrival']
"""The only qualified method in this build — arrival/level/polarity from a
native sweep impulse response. Extending this is an authority change."""


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

class ChannelVerificationTarget(BaseModel):
    """One logical channel to verify."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    logical_channel: str = Field(min_length=1)
    chain_ref: AuthorityRef | None = None
    """Pin to the #621 :class:`CadChannelIdentityChain` — required when the
    channel declares physical expectations."""
    expected_speaker_entity_ids: tuple[str, ...] = ()
    routing: ChannelRouting
    expected_polarity: Literal['normal', 'inverted', 'unknown'] = 'unknown'


class VerificationThresholds(BaseModel):
    """Analysis thresholds — explicit, never defaults that silently pass."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    min_snr_db: float = 20.0
    min_response_level_dbfs: float = -80.0
    max_level_deviation_db: float = 12.0
    """Peak level may deviate at most this much from the strongest channel
    response before ``level_mismatch`` flags. Asymmetric loudspeaker arrays
    declare this wider rather than disabling the check."""
    duplicate_signature_epsilon: float = 1e-6
    """Absolute tolerance for the (arrival, peak) signature equality that
    flags ``duplicate_signature`` — identical observations on distinct
    logical channels cannot both be machine-verified."""


class ChannelVerificationPlan(BaseModel):
    """A sealed plan binding every configured logical channel to an exact
    output route and declared expectation (``cvpl-``).

    Generated from the declared #621 chains; executing it never mutates the
    chains. A routing/config change after results exist stales only the
    affected channel entries — see :func:`stale_channels_for_routing`.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    plan_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    targets: tuple[ChannelVerificationTarget, ...]
    reference_channel: str | None = None
    """Logical channel whose polarity sign defines 'normal'. Required when
    any target declares expected_polarity != 'unknown'."""
    method: VerificationMethod = 'native_sweep_arrival'
    sample_rate_hz: int = Field(gt=0)
    stimulus_start_hz: float = Field(gt=0)
    stimulus_end_hz: float = Field(gt=0)
    stimulus_duration_s: float = Field(gt=0)
    stimulus_level_dbfs: float = Field(le=0.0)
    pre_roll_s: float = Field(ge=0.0, default=0.01)
    post_roll_s: float = Field(ge=0.0, default=0.01)
    fade_in_s: float = Field(ge=0.0, default=0.002)
    fade_out_s: float = Field(ge=0.0, default=0.002)
    repetitions: int = Field(ge=1, default=2)
    repetition_gap_s: float = Field(ge=0.0, default=0.02)
    thresholds: VerificationThresholds = VerificationThresholds()
    created_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=CHANNEL_VERIFICATION_VERSION, min_length=1
    )
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)

    @property
    def target_count(self) -> int:
        return len(self.targets)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'targets': [t.model_dump(mode='json') for t in self.targets],
            'reference_channel': self.reference_channel,
            'method': self.method,
            'sample_rate_hz': self.sample_rate_hz,
            'stimulus_start_hz': self.stimulus_start_hz,
            'stimulus_end_hz': self.stimulus_end_hz,
            'stimulus_duration_s': self.stimulus_duration_s,
            'stimulus_level_dbfs': self.stimulus_level_dbfs,
            'pre_roll_s': self.pre_roll_s,
            'post_roll_s': self.post_roll_s,
            'fade_in_s': self.fade_in_s,
            'fade_out_s': self.fade_out_s,
            'repetitions': self.repetitions,
            'repetition_gap_s': self.repetition_gap_s,
            'thresholds': self.thresholds.model_dump(mode='json'),
            'created_at_utc': self.created_at_utc,
            'authority_version': self.authority_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'ChannelVerificationPlan':
        _require_iso8601(self.created_at_utc, 'plan created_at_utc')
        if not self.targets:
            raise ValueError('verification plan requires at least one target')
        channels = [t.logical_channel for t in self.targets]
        if len(set(channels)) != len(channels):
            raise ValueError('duplicate logical_channel in plan')
        if self.reference_channel is not None and (
                self.reference_channel not in set(channels)):
            raise ValueError(
                'reference_channel must name a plan target')
        if any(t.expected_polarity != 'unknown' for t in self.targets) and (
                self.reference_channel is None):
            raise ValueError(
                'polarity expectation requires a reference_channel')
        if self.stimulus_end_hz <= self.stimulus_start_hz:
            raise ValueError('stimulus_end_hz must exceed start_hz')
        if self.stimulus_end_hz > self.sample_rate_hz / 2:
            raise ValueError('stimulus end exceeds Nyquist')
        if self.fade_in_s + self.fade_out_s > self.stimulus_duration_s:
            raise ValueError('stimulus fades must fit inside duration')
        for target in self.targets:
            if (len(set(target.expected_speaker_entity_ids))
                    != len(target.expected_speaker_entity_ids)):
                raise ValueError('expected speaker ids must be unique')
        expected = _hash(self.identity_payload())
        if self.plan_sha256 != expected:
            raise ValueError('plan hash mismatch')
        if self.plan_id != _semantic_id('cvpl', expected):
            raise ValueError('plan id does not match its hash')
        return self


def build_verification_plan(
    *,
    document_id: str,
    chains: Sequence[CadChannelIdentityChain],
    routings: Mapping[str, ChannelRouting],
    sample_rate_hz: int,
    stimulus_start_hz: float = 20.0,
    stimulus_end_hz: float = 20000.0,
    stimulus_duration_s: float = 0.5,
    stimulus_level_dbfs: float = -20.0,
    pre_roll_s: float = 0.01,
    post_roll_s: float = 0.01,
    fade_in_s: float = 0.002,
    fade_out_s: float = 0.002,
    repetitions: int = 2,
    repetition_gap_s: float = 0.02,
    reference_channel: str | None = None,
    thresholds: VerificationThresholds = VerificationThresholds(),
    polarity_expectations: Mapping[str, str] | None = None,
    created_at_utc: str | None = None,
) -> ChannelVerificationPlan:
    """Derive a sealed plan from the declared #621 chains.

    ``routings`` must name an exact :class:`ChannelRouting` for every chain —
    missing routing is a build error, never a silent skip.
    """

    expectations = dict(polarity_expectations or {})
    targets = []
    for chain in chains:
        routing = routings.get(chain.logical_channel)
        if routing is None:
            raise ValueError(
                f'no routing bound for {chain.logical_channel}')
        polarity = expectations.get(chain.logical_channel, 'unknown')
        if polarity not in ('normal', 'inverted', 'unknown'):
            raise ValueError('expected_polarity must be normal/inverted/unknown')
        targets.append(ChannelVerificationTarget(
            logical_channel=chain.logical_channel,
            chain_ref=AuthorityRef(
                kind='channel_identity_chain',
                ref_id=chain.chain_id,
                ref_sha256=chain.chain_sha256,
            ),
            expected_speaker_entity_ids=chain.expected_speaker_entity_ids,
            routing=routing,
            expected_polarity=polarity,  # type: ignore[arg-type]
        ))
    when = created_at_utc or _utc_now()
    payload_plan = {
        'document_id': document_id,
        'targets': [t.model_dump(mode='json') for t in targets],
        'reference_channel': reference_channel,
        'method': 'native_sweep_arrival',
        'sample_rate_hz': sample_rate_hz,
        'stimulus_start_hz': stimulus_start_hz,
        'stimulus_end_hz': stimulus_end_hz,
        'stimulus_duration_s': stimulus_duration_s,
        'stimulus_level_dbfs': stimulus_level_dbfs,
        'pre_roll_s': pre_roll_s,
        'post_roll_s': post_roll_s,
        'fade_in_s': fade_in_s,
        'fade_out_s': fade_out_s,
        'repetitions': repetitions,
        'repetition_gap_s': repetition_gap_s,
        'thresholds': thresholds.model_dump(mode='json'),
        'created_at_utc': when,
        'authority_version': CHANNEL_VERIFICATION_VERSION,
    }
    digest = _hash(payload_plan)
    return ChannelVerificationPlan(
        plan_id=_semantic_id('cvpl', digest),
        document_id=document_id,
        targets=tuple(targets),
        reference_channel=reference_channel,
        sample_rate_hz=sample_rate_hz,
        stimulus_start_hz=stimulus_start_hz,
        stimulus_end_hz=stimulus_end_hz,
        stimulus_duration_s=stimulus_duration_s,
        stimulus_level_dbfs=stimulus_level_dbfs,
        pre_roll_s=pre_roll_s,
        post_roll_s=post_roll_s,
        fade_in_s=fade_in_s,
        fade_out_s=fade_out_s,
        repetitions=repetitions,
        repetition_gap_s=repetition_gap_s,
        thresholds=thresholds,
        created_at_utc=when,
        plan_sha256=digest,
    )


def plan_binding(plan: ChannelVerificationPlan) -> AuthorityRef:
    return AuthorityRef(
        kind='channel_verification_plan',
        ref_id=plan.plan_id,
        ref_sha256=plan.plan_sha256,
    )


def routing_signature(routing: ChannelRouting, sample_rate_hz: int) -> str:
    """Stable signature of the physical binding for staleness checks —
    device ids, channel indices, sample rate. Anything that would change
    the physical path changes the signature."""

    return _hash({
        'playback_device_id': routing.playback_device_id,
        'playback_channel': routing.playback_channel,
        'capture_device_id': routing.capture_device_id,
        'capture_channel': routing.capture_channel,
        'loopback_input_channel': routing.loopback_input_channel,
        'sample_rate_hz': sample_rate_hz,
    })


# ---------------------------------------------------------------------------
# Per-channel excitation result
# ---------------------------------------------------------------------------

class ChannelExcitationResult(BaseModel):
    """Sealed outcome of one channel's excitation + analysis (``cvex-``).

    Pins the exact #869 acquisition run the measurement came from and the
    routing signature the measurement was taken under, so a routing change
    deterministically stales this channel only.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    result_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    plan_ref: AuthorityRef
    logical_channel: str = Field(min_length=1)
    acquisition_run_ref: AuthorityRef | None = None
    """Pin to the sealed #869 acquisition run. ``None`` only when the run
    could not produce a sealed record at all (e.g. precheck blocked)."""
    routing_signature_sha256: str = Field(pattern=_SHA256_PATTERN)
    backend_id: str = Field(min_length=1)
    backend_is_simulated: bool
    capture_quality: Literal['passed', 'invalid', 'cancelled', 'blocked']
    quality_verdict: Literal['valid', 'limited', 'invalid'] | None = None
    """The #869 quality-gate verdict, recorded verbatim — 'limited' still
    passes routing analysis (absolute-level caveats do not bear on channel
    identity) but is visible to the reader."""
    timing_quality: Literal['qualified', 'limited', 'unqualified'] | None = (
        None
    )
    response_detected: bool
    arrival_sample: int | None = Field(default=None, ge=0)
    level_dbfs: float | None = None
    """Measured response level (capture-signal RMS level inside the sweep
    window, in dBFS) — NOT the deconvolved IR peak, which is normalized to
    unity by the #869 deriver and carries no level evidence."""
    polarity_sign: int | None = Field(default=None, ge=-1, le=1)
    response_signature_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    """Hash of (arrival, quantized level, polarity) — the duplicate
    detector; identical signatures on distinct channels cannot both
    verify."""
    analysis_version: str = Field(default=ANALYSIS_VERSION, min_length=1)
    measured_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=CHANNEL_VERIFICATION_VERSION, min_length=1
    )
    result_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'plan_ref': self.plan_ref.model_dump(mode='json'),
            'logical_channel': self.logical_channel,
            'acquisition_run_ref': (
                self.acquisition_run_ref.model_dump(mode='json')
                if self.acquisition_run_ref is not None else None
            ),
            'routing_signature_sha256': self.routing_signature_sha256,
            'backend_id': self.backend_id,
            'backend_is_simulated': self.backend_is_simulated,
            'capture_quality': self.capture_quality,
            'quality_verdict': self.quality_verdict,
            'timing_quality': self.timing_quality,
            'response_detected': self.response_detected,
            'arrival_sample': self.arrival_sample,
            'level_dbfs': self.level_dbfs,
            'polarity_sign': self.polarity_sign,
            'response_signature_sha256': self.response_signature_sha256,
            'analysis_version': self.analysis_version,
            'measured_at_utc': self.measured_at_utc,
            'authority_version': self.authority_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'ChannelExcitationResult':
        _require_iso8601(self.measured_at_utc, 'result measured_at_utc')
        if self.plan_ref.kind != 'channel_verification_plan':
            raise ValueError('plan_ref must pin a channel_verification_plan')
        if self.plan_ref.ref_sha256 is None:
            raise ValueError('plan_ref must pin its sha256')
        if self.acquisition_run_ref is not None and (
                self.acquisition_run_ref.ref_sha256 is None):
            raise ValueError('acquisition_run_ref must pin its sha256')
        if self.response_detected:
            if self.arrival_sample is None or self.level_dbfs is None:
                raise ValueError(
                    'detected response requires arrival_sample and level_dbfs')
            if self.response_signature_sha256 is None:
                raise ValueError('detected response requires a signature')
        else:
            if (self.arrival_sample is not None or self.level_dbfs is not None
                    or self.polarity_sign is not None):
                raise ValueError(
                    'no detection means no arrival/level/polarity fields')
        expected = _hash(self.identity_payload())
        if self.result_sha256 != expected:
            raise ValueError('result hash mismatch')
        if self.result_id != _semantic_id('cvex', expected):
            raise ValueError('result id does not match its hash')
        return self


def result_binding(result: ChannelExcitationResult) -> AuthorityRef:
    return AuthorityRef(
        kind='channel_excitation_result',
        ref_id=result.result_id,
        ref_sha256=result.result_sha256,
    )


def response_signature(
    arrival_sample: int, level_dbfs: float, polarity_sign: int,
) -> str:
    """Deterministic signature of one observed response. Level quantized to
    0.25 dB so genuinely-identical routes collide while real channels
    (which differ acoustically) do not."""

    return _hash({
        'arrival_sample': arrival_sample,
        'level_quarter_db': round(level_dbfs * 4) / 4,
        'polarity_sign': polarity_sign,
    })


# ---------------------------------------------------------------------------
# Operator attestation (fallback evidence — permanently manual)
# ---------------------------------------------------------------------------

class OperatorChannelAttestation(BaseModel):
    """Operator statement that a channel behaved as expected (``cvoa-``).

    Fallback evidence only — ``evidence_class`` is pinned to
    ``operator_attested`` and evaluation never promotes it.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    attestation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    plan_ref: AuthorityRef
    logical_channel: str = Field(min_length=1)
    attested_by: str = Field(min_length=1)
    responded: bool
    attested_polarity: Literal['normal', 'inverted', 'unknown'] = 'unknown'
    note: str | None = Field(default=None, min_length=1)
    attested_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=CHANNEL_VERIFICATION_VERSION, min_length=1
    )
    attestation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @property
    def evidence_class(self) -> VerificationEvidenceClass:
        return 'operator_attested'

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'plan_ref': self.plan_ref.model_dump(mode='json'),
            'logical_channel': self.logical_channel,
            'attested_by': self.attested_by,
            'responded': self.responded,
            'attested_polarity': self.attested_polarity,
            'note': self.note,
            'attested_at_utc': self.attested_at_utc,
            'authority_version': self.authority_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'OperatorChannelAttestation':
        _require_iso8601(
            self.attested_at_utc, 'attestation attested_at_utc')
        if self.plan_ref.kind != 'channel_verification_plan':
            raise ValueError('plan_ref must pin a channel_verification_plan')
        if self.plan_ref.ref_sha256 is None:
            raise ValueError('plan_ref must pin its sha256')
        expected = _hash(self.identity_payload())
        if self.attestation_sha256 != expected:
            raise ValueError('attestation hash mismatch')
        if self.attestation_id != _semantic_id('cvoa', expected):
            raise ValueError('attestation id does not match its hash')
        return self


def attestation_binding(
    attestation: OperatorChannelAttestation,
) -> AuthorityRef:
    return AuthorityRef(
        kind='operator_channel_attestation',
        ref_id=attestation.attestation_id,
        ref_sha256=attestation.attestation_sha256,
    )


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

class ChannelVerdict(BaseModel):
    """One channel's verdict — the evidence class plus every defect flag."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    logical_channel: str = Field(min_length=1)
    evidence_class: VerificationEvidenceClass
    routing_state: ChannelRoutingState
    polarity_state: ChannelPolarityState
    defect_flags: tuple[DefectFlag, ...] = ()
    result_ref: AuthorityRef | None = None
    attestation_ref: AuthorityRef | None = None
    stale: bool = False


class ChannelVerificationVerdict(BaseModel):
    """Map-level verdict over one plan (``cvvd-``).

    ``map_state`` is derived, never asserted: any channel below
    ``machine_verified_routing`` keeps the map out of ``verified``.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    verdict_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    plan_ref: AuthorityRef
    channels: tuple[ChannelVerdict, ...]
    map_state: MapVerificationState
    evaluated_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=CHANNEL_VERIFICATION_VERSION, min_length=1
    )
    verdict_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'plan_ref': self.plan_ref.model_dump(mode='json'),
            'channels': [c.model_dump(mode='json') for c in self.channels],
            'map_state': self.map_state,
            'evaluated_at_utc': self.evaluated_at_utc,
            'authority_version': self.authority_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'ChannelVerificationVerdict':
        _require_iso8601(
            self.evaluated_at_utc, 'verdict evaluated_at_utc')
        if self.plan_ref.kind != 'channel_verification_plan':
            raise ValueError('plan_ref must pin a channel_verification_plan')
        if self.plan_ref.ref_sha256 is None:
            raise ValueError('plan_ref must pin its sha256')
        seen = [c.logical_channel for c in self.channels]
        if len(set(seen)) != len(seen):
            raise ValueError('duplicate channel verdict')
        expected = _hash(self.identity_payload())
        if self.verdict_sha256 != expected:
            raise ValueError('verdict hash mismatch')
        if self.verdict_id != _semantic_id('cvvd', expected):
            raise ValueError('verdict id does not match its hash')
        return self

    def channel(self, logical_channel: str) -> ChannelVerdict | None:
        for c in self.channels:
            if c.logical_channel == logical_channel:
                return c
        return None


def verdict_binding(verdict: ChannelVerificationVerdict) -> AuthorityRef:
    return AuthorityRef(
        kind='channel_verification_verdict',
        ref_id=verdict.verdict_id,
        ref_sha256=verdict.verdict_sha256,
    )


# ---------------------------------------------------------------------------
# Evaluation (pure, deterministic)
# ---------------------------------------------------------------------------

def _channel_verdict(
    target: ChannelVerificationTarget,
    result: ChannelExcitationResult | None,
    attestation: OperatorChannelAttestation | None,
    *,
    duplicate: bool,
    max_level_dbfs: float | None,
    reference_sign: int | None,
    thresholds: VerificationThresholds,
) -> ChannelVerdict:
    flags: list[DefectFlag] = []
    att_ref = attestation_binding(attestation) if attestation is not None else None
    result_ref = result_binding(result) if result is not None else None

    if result is None or result.capture_quality in ('blocked', 'cancelled'):
        if attestation is not None:
            return ChannelVerdict(
                logical_channel=target.logical_channel,
                evidence_class='operator_attested',
                routing_state='unverified',
                polarity_state='unevaluated',
                defect_flags=('capture_invalid',) if result is not None else (),
                result_ref=result_ref,
                attestation_ref=att_ref,
            )
        return ChannelVerdict(
            logical_channel=target.logical_channel,
            evidence_class='unknown',
            routing_state='unverified',
            polarity_state='unevaluated',
            defect_flags=('capture_invalid',) if result is not None else (),
            result_ref=result_ref,
        )

    if result.capture_quality != 'passed':
        flags.append('capture_invalid')
    if result.backend_is_simulated:
        flags.append('simulated_backend')
    if not result.response_detected:
        flags.append('response_missing')
    if duplicate:
        flags.append('duplicate_signature')

    # Level deviation versus the strongest observed channel.
    if (result.response_detected and max_level_dbfs is not None
            and result.level_dbfs is not None):
        deviation = max_level_dbfs - result.level_dbfs
        if deviation > thresholds.max_level_deviation_db:
            flags.append('level_mismatch')

    # Routing state.
    if result.timing_quality == 'unqualified':
        flags.append('timing_unqualified')

    if 'response_missing' in flags:
        routing: ChannelRoutingState = 'missing_output'
    elif 'capture_invalid' in flags:
        routing = 'unverified'
    elif duplicate:
        routing = 'duplicate_suspect'
    elif 'level_mismatch' in flags:
        routing = 'level_mismatch'
    else:
        routing = 'verified'

    # Polarity state — relative to the reference channel only.
    polarity: ChannelPolarityState = 'unevaluated'
    if (routing == 'verified'
            and target.expected_polarity != 'unknown'
            and reference_sign is not None
            and result.polarity_sign is not None
            and result.polarity_sign != 0):
        observed = (
            'normal' if result.polarity_sign == reference_sign
            else 'inverted'
        )
        if observed == target.expected_polarity:
            polarity = 'consistent'
        else:
            polarity = 'inverted'
            flags.append('polarity_inverted')
    elif (routing == 'verified'
            and target.expected_polarity != 'unknown'):
        polarity = 'ambiguous'

    # Evidence class — the ladder, fail closed.
    if 'simulated_backend' in flags:
        # Simulated capture is test evidence only — never machine-verified.
        if routing == 'verified':
            evidence: VerificationEvidenceClass = 'machine_assisted_ambiguous'
        elif 'response_missing' in flags or 'capture_invalid' in flags:
            evidence = 'unknown'
        else:
            evidence = 'machine_assisted_ambiguous'
    elif routing == 'verified':
        if (target.expected_polarity != 'unknown'
                and polarity == 'consistent'):
            evidence = 'machine_verified_relative_polarity'
        else:
            evidence = 'machine_verified_routing'
    elif 'response_missing' in flags:
        evidence = 'unknown'
    else:
        evidence = 'machine_assisted_ambiguous'

    if attestation is not None and evidence in (
            'unknown', 'machine_assisted_ambiguous'):
        evidence = 'operator_attested'

    return ChannelVerdict(
        logical_channel=target.logical_channel,
        evidence_class=evidence,
        routing_state=routing,
        polarity_state=polarity,
        defect_flags=tuple(flags),
        result_ref=result_ref,
        attestation_ref=att_ref,
    )


def evaluate_channel_verdicts(
    plan: ChannelVerificationPlan,
    results: Mapping[str, ChannelExcitationResult],
    attestations: Mapping[str, OperatorChannelAttestation] | None = None,
    *,
    evaluated_at_utc: str | None = None,
) -> ChannelVerificationVerdict:
    """Evaluate every plan target against its excitation result.

    Duplicate signatures are detected plan-wide: two channels whose
    responses share a signature cannot both be machine-verified.
    """

    attestations = attestations or {}
    when = evaluated_at_utc or _utc_now()

    # Plan-wide duplicate detection — only over detected, valid captures.
    signature_channels: dict[str, list[str]] = {}
    for target in plan.targets:
        r = results.get(target.logical_channel)
        if (r is not None and r.capture_quality == 'passed'
                and r.response_detected
                and r.response_signature_sha256 is not None):
            signature_channels.setdefault(
                r.response_signature_sha256, []).append(
                target.logical_channel)
    duplicated = {
        ch
        for sig, chs in signature_channels.items() if len(chs) > 1
        for ch in chs
    }

    levels = [
        r.level_dbfs
        for r in results.values()
        if r is not None and r.response_detected
        and r.capture_quality == 'passed' and r.level_dbfs is not None
    ]
    max_level = max(levels) if levels else None

    ref_sign: int | None = None
    if plan.reference_channel is not None:
        ref = results.get(plan.reference_channel)
        if (ref is not None and ref.response_detected
                and ref.capture_quality == 'passed'
                and ref.polarity_sign):
            ref_sign = ref.polarity_sign

    channels: list[ChannelVerdict] = []
    for target in plan.targets:
        channels.append(_channel_verdict(
            target,
            results.get(target.logical_channel),
            attestations.get(target.logical_channel),
            duplicate=target.logical_channel in duplicated,
            max_level_dbfs=max_level,
            reference_sign=ref_sign,
            thresholds=plan.thresholds,
        ))

    classes = {c.evidence_class for c in channels}
    verified_classes = (
        'machine_verified_routing', 'machine_verified_relative_polarity')
    # Hard defects are failed verification attempts — an output that could
    # not be exercised, a response that never arrived, a level or polarity
    # the physical path cannot explain. Signature collisions are NOT in
    # this set: the excitation itself succeeded, so the map is ambiguous
    # for an operator to resolve rather than failed.
    hard_defect_flags = frozenset({
        'response_missing', 'level_mismatch', 'polarity_inverted',
        'capture_invalid'})
    defective = [
        c for c in channels
        if hard_defect_flags & set(c.defect_flags)
    ]
    if classes == {'unknown'}:
        map_state: MapVerificationState = 'unknown'
    elif defective and not all(
            c.evidence_class == 'machine_assisted_ambiguous'
            for c in defective):
        map_state = 'failed'
    elif all(c.evidence_class in verified_classes for c in channels):
        map_state = 'verified'
    elif defective or classes & {
            'machine_assisted_ambiguous', 'operator_attested'}:
        map_state = 'ambiguous'
    else:
        map_state = 'incomplete'

    payload = {
        'document_id': plan.document_id,
        'plan_ref': plan_binding(plan).model_dump(mode='json'),
        'channels': [c.model_dump(mode='json') for c in channels],
        'map_state': map_state,
        'evaluated_at_utc': when,
        'authority_version': CHANNEL_VERIFICATION_VERSION,
    }
    digest = _hash(payload)
    return ChannelVerificationVerdict(
        verdict_id=_semantic_id('cvvd', digest),
        document_id=plan.document_id,
        plan_ref=plan_binding(plan),
        channels=tuple(channels),
        map_state=map_state,
        evaluated_at_utc=when,
        verdict_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Staleness — routing/config change affects only the channels it touches
# ---------------------------------------------------------------------------

def stale_channels_for_routing(
    plan: ChannelVerificationPlan,
    results: Mapping[str, ChannelExcitationResult],
    current_routings: Mapping[str, ChannelRouting],
    current_sample_rate_hz: int,
) -> tuple[str, ...]:
    """Channels whose stored routing signature no longer matches current.

    Only those channels need re-verification; a re-run MUST re-evaluate the
    plan-wide duplicate check, which a per-channel view cannot do honestly.
    """

    stale: list[str] = []
    for target in plan.targets:
        current = current_routings.get(target.logical_channel)
        if current is None:
            stale.append(target.logical_channel)
            continue
        current_sig = routing_signature(
            current, current_sample_rate_hz)
        result = results.get(target.logical_channel)
        if result is None or (
                result.routing_signature_sha256 != current_sig):
            stale.append(target.logical_channel)
    return tuple(stale)


# ---------------------------------------------------------------------------
# Driver — sequences the #869 engine per channel
# ---------------------------------------------------------------------------

class ChannelVerificationError(ValueError):
    """Verification driver misuse or engine-failure surface."""


ArmingDelegate = Callable[[AcquisitionRequest], ArmConfirmation]
"""Supplies the operator's arm confirmation for a channel's armed config.
The callable receives the actual :class:`AcquisitionRequest` the engine
bound, so a confirmation is checked against reality — never a canned echo.
Automation callers surface the physical confirm step once per session;
tests provide an echoing delegate."""


def run_channel_excitation(
    *,
    plan: ChannelVerificationPlan,
    target: ChannelVerificationTarget,
    backend: AudioIOBackend,
    document_id: str,
    arming: Callable[[AcquisitionRequest], ArmConfirmation],
    level_policy: LevelSafetyPolicy | None = None,
    sweep_repository: Any | None = None,
    clock: Callable[[], str] = _utc_now,
    backend_id: str | None = None,
) -> tuple[ChannelExcitationResult, MeasurementAcquisitionEngine]:
    """Drive one channel end to end through the #869 engine.

    ``arming`` receives the actual :class:`AcquisitionRequest` the engine
    bound, so an operator confirmation is checked against reality.
    Returns the sealed result plus the engine (for transition inspection).
    """

    stimulus_spec = SweepStimulusSpec(
        sample_rate_hz=plan.sample_rate_hz,
        start_frequency_hz=plan.stimulus_start_hz,
        end_frequency_hz=plan.stimulus_end_hz,
        duration_s=plan.stimulus_duration_s,
        level_dbfs=plan.stimulus_level_dbfs,
        pre_roll_s=plan.pre_roll_s,
        post_roll_s=plan.post_roll_s,
        fade_in_s=plan.fade_in_s,
        fade_out_s=plan.fade_out_s,
        repetitions=plan.repetitions,
        repetition_gap_s=plan.repetition_gap_s,
    )
    request = AcquisitionRequest(
        stimulus=stimulus_spec,
        routing=target.routing,
        level_policy=level_policy or LevelSafetyPolicy(),
        calibration_state='unknown',
    )
    engine = MeasurementAcquisitionEngine(backend=backend, clock=clock)
    when = clock()

    quality: Literal['passed', 'invalid', 'cancelled', 'blocked']
    quality_verdict: str | None = None
    timing_quality: str | None = None
    response_detected = False
    arrival: int | None = None
    level_dbfs: float | None = None
    polarity: int | None = None
    sig: str | None = None
    bid = backend_id or getattr(
        backend, 'backend_id', type(backend).__name__)
    # Same honesty convention as the #869 evidence layer: the fake backend
    # is simulated by identity; any other backend may opt in explicitly.
    simulated = getattr(backend, 'is_simulated', bid == 'fake-audio-io')

    run_ref: AuthorityRef | None = None
    try:
        report = engine.configure(request)
        if not report.ok:
            quality = 'blocked'
        else:
            engine.arm(arming(request))
            outcome = engine.start()
            quality_verdict = (
                outcome.quality.verdict if outcome.quality is not None
                else None
            )
            timing_quality = (
                outcome.timing.quality if outcome.timing is not None
                else None
            )
            if engine.stage == 'cancelled':
                quality = 'cancelled'
            elif engine.stage == 'completed' and (
                    outcome.quality is not None
                    and outcome.quality.verdict in ('valid', 'limited')):
                # 'limited' records quality caveats (e.g. no calibration
                # bound) that do not bear on channel identity — the
                # verbatim verdict stays on the result for the reader.
                quality = 'passed'
            else:
                quality = 'invalid'
            # Seal the run through the #869 evidence authority so the
            # excitation result pins the retained record's real sha —
            # never a synthetic run-id digest.
            stimulus_def = build_stimulus_definition(
                document_id=document_id,
                stimulus=outcome.stimulus,
                created_at_utc=clock(),
            )
            stimulus_ref = AuthorityRef(
                kind='sweep_stimulus_definition',
                ref_id=stimulus_def.stimulus_definition_id,
                ref_sha256=stimulus_def.stimulus_sha256,
            )
            if sweep_repository is not None:
                sweep_repository.save_stimulus_definition(stimulus_def)
                sealed_run = sweep_repository.record_run(
                    document_id=document_id,
                    engine=engine,
                    result=outcome,
                    stimulus_ref=stimulus_ref,
                )
            else:
                sealed_run = build_acquisition_run(
                    document_id=document_id,
                    engine=engine,
                    result=outcome,
                    stimulus_ref=stimulus_ref,
                )
            run_ref = AuthorityRef(
                kind='sweep_acquisition_run',
                ref_id=sealed_run.acquisition_id,
                ref_sha256=sealed_run.acquisition_sha256,
            )
            if quality == 'passed':
                ir_out = outcome.impulse_response
                if (ir_out is not None and ir_out.status == 'derived'
                        and ir_out.ir):
                    ir = np.asarray(ir_out.ir, dtype=np.float64)
                    idx = int(np.argmax(np.abs(ir)))
                    measured = (
                        outcome.quality.measured
                        if outcome.quality is not None else {}) or {}
                    snr_db = measured.get('snr_db')
                    floor_dbfs = measured.get('noise_floor_dbfs')
                    # Response level = signal level inside the sweep
                    # window (noise floor + SNR margin, both engine-
                    # measured). The deconvolved IR is unit-normalized
                    # and carries no level evidence.
                    measured_level = (
                        floor_dbfs + snr_db
                        if (snr_db is not None and floor_dbfs is not None)
                        else None
                    )
                    detected = (
                        measured_level is not None
                        and measured_level >= (
                            plan.thresholds.min_response_level_dbfs)
                        and (snr_db is None
                             or snr_db >= plan.thresholds.min_snr_db)
                        and float(np.abs(ir[idx])) > 0
                    )
                    if detected:
                        level_dbfs = measured_level
                        # Arrival is measured relative to the IR window
                        # origin — a channel-index-independent quantity
                        # the duplicate detector can compare honestly.
                        arrival = idx
                        polarity = 1 if ir[idx] >= 0 else -1
                        response_detected = True
                        sig = response_signature(
                            arrival, level_dbfs, polarity)
    except ChannelVerificationError:
        raise
    except Exception:
        # Engine/Backend failures are evidence: mark invalid, never crash
        # the batch. # error-boundary: engine/backend boundary.
        quality = 'invalid'
        run_ref = None

    payload = {
        'document_id': document_id,
        'plan_ref': plan_binding(plan).model_dump(mode='json'),
        'logical_channel': target.logical_channel,
        'acquisition_run_ref': (
            run_ref.model_dump(mode='json') if run_ref is not None else None
        ),
        'routing_signature_sha256': routing_signature(
            target.routing, plan.sample_rate_hz),
        'backend_id': bid,
        'backend_is_simulated': bool(simulated),
        'capture_quality': quality,
        'quality_verdict': quality_verdict,
        'timing_quality': timing_quality,
        'response_detected': response_detected,
        'arrival_sample': arrival,
        'level_dbfs': level_dbfs,
        'polarity_sign': polarity,
        'response_signature_sha256': sig,
        'analysis_version': ANALYSIS_VERSION,
        'measured_at_utc': when,
        'authority_version': CHANNEL_VERIFICATION_VERSION,
    }
    digest = _hash(payload)
    result = ChannelExcitationResult(
        result_id=_semantic_id('cvex', digest),
        document_id=document_id,
        plan_ref=plan_binding(plan),
        logical_channel=target.logical_channel,
        acquisition_run_ref=run_ref,
        routing_signature_sha256=routing_signature(
            target.routing, plan.sample_rate_hz),
        backend_id=bid,
        backend_is_simulated=bool(simulated),
        capture_quality=quality,
        quality_verdict=quality_verdict,  # type: ignore[arg-type]
        timing_quality=timing_quality,  # type: ignore[arg-type]
        response_detected=response_detected,
        arrival_sample=arrival,
        level_dbfs=level_dbfs,
        polarity_sign=polarity,
        response_signature_sha256=sig,
        measured_at_utc=when,
        result_sha256=digest,
    )
    return result, engine


def run_verification_plan(
    *,
    plan: ChannelVerificationPlan,
    backend: AudioIOBackend | Callable[
        [ChannelVerificationTarget], AudioIOBackend],
    arming: Callable[[AcquisitionRequest], ArmConfirmation],
    document_id: str | None = None,
    level_policy: LevelSafetyPolicy | None = None,
    sweep_repository: Any | None = None,
    attestations: Mapping[str, OperatorChannelAttestation] | None = None,
    clock: Callable[[], str] = _utc_now,
    on_channel: Callable[[str, ChannelExcitationResult], None] | None = None,
) -> tuple[ChannelVerificationVerdict,
           dict[str, ChannelExcitationResult],
           dict[str, MeasurementAcquisitionEngine]]:
    """Run every plan target through the engine and evaluate the verdict.

    Channels run in declared order; a per-channel failure never silently
    skips another channel — each still gets its own sealed result.
    """

    doc = document_id or plan.document_id
    results: dict[str, ChannelExcitationResult] = {}
    engines: dict[str, MeasurementAcquisitionEngine] = {}
    for target in plan.targets:
        channel_backend = (
            backend(target) if callable(backend) else backend
        )
        result, engine = run_channel_excitation(
            plan=plan,
            target=target,
            backend=channel_backend,
            document_id=doc,
            arming=arming,
            level_policy=level_policy,
            sweep_repository=sweep_repository,
            clock=clock,
        )
        results[target.logical_channel] = result
        engines[target.logical_channel] = engine
        if on_channel is not None:
            on_channel(target.logical_channel, result)
    verdict = evaluate_channel_verdicts(
        plan, results, attestations,
        evaluated_at_utc=clock(),
    )
    return verdict, results, engines
