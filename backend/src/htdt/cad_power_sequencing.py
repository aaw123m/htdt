"""AV power-sequencing / outage-recovery authority (issue #736).

Adequate circuit/UPS capacity and a working automation scene do NOT
prove that the AV system enters and leaves operational state in a
safe, deterministic order. Sequencing evidence must pin stage order,
amplifier last-on/first-off placement, inrush-aware staging, always-on
control/network devices, and UPS transition behavior before claiming a
sequence is verified.

Basis: issue #736 scope; #587 electrical/rack qualification;
#601 control-scenario verification; #592 known-good configuration;
#595 lifecycle monitoring.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


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


def _require_refs(*refs: AuthorityRef) -> None:
    for ref in refs:
        if ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')


SequenceDirection = Literal['power_on', 'power_off', 'outage_transition']
DeviceClass = Literal[
    'source', 'processor', 'amplifier', 'projector', 'display',
    'network', 'control', 'other',
]

SEQUENCE_LABELS: dict[str, str] = {
    'sequence_verified': '順序が検証済み',
    'unverified_sequencing': '順序は未検証',
    'amplifier_order_violation': 'アンプ順序違反',
    'always_on_violation': '常時電源違反',
    'insufficient_evidence': '証拠不足',
}


class PowerSequencingProfile(BaseModel):
    """Declared power-sequence intent (psq- prefix)."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    direction: SequenceDirection
    stages: tuple[str, ...]
    amplifier_device_ids: tuple[str, ...] = ()
    always_on_device_ids: tuple[str, ...] = ()
    inrush_staging_declared: bool = False

    @model_validator(mode='after')
    def _validate(self) -> 'PowerSequencingProfile':
        if not self.stages:
            raise ValueError('stages must not be empty')
        if self.direction == 'power_off' and not self.amplifier_device_ids:
            raise ValueError(
                'power_off sequence must declare amplifier devices')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'PowerSequencingProfile':
        return _seal(cls, payload, 'profile_id', 'profile_sha256', 'psq')


class PowerSequenceEvent(BaseModel):
    """Observed power transition event (psev- prefix)."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    event_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    observed_stage_order: tuple[str, ...]
    amplifier_off_before_processing: bool | None = None
    amplifier_on_after_processing: bool | None = None
    inrush_peak_observed: bool | None = None
    control_survived_outage: bool | None = None
    result_data_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'PowerSequenceEvent':
        _require_refs(self.profile_ref)
        if not self.observed_stage_order:
            raise ValueError('observed_stage_order must not be empty')
        if self.result_data_ref is not None:
            _require_refs(self.result_data_ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'event_id', 'event_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'PowerSequenceEvent':
        return _seal(cls, payload, 'event_id', 'event_sha256', 'psev')


class UpsTransitionRecord(BaseModel):
    """UPS transition / outage-recovery observation (upst- prefix)."""

    model_config = ConfigDict(frozen=True)

    record_id: str
    record_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    ups_device_id: str
    transfer_observed: bool
    protected_devices_survived: tuple[str, ...] = ()
    protected_devices_dropped: tuple[str, ...] = ()
    recovery_sequence_ref: AuthorityRef | None = None
    result_data_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'UpsTransitionRecord':
        if self.transfer_observed and not self.recovery_sequence_ref:
            raise ValueError(
                'observed transfer must pin recovery_sequence_ref')
        for ref in (self.recovery_sequence_ref, self.result_data_ref):
            if ref is not None:
                _require_refs(ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'record_id', 'record_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'UpsTransitionRecord':
        return _seal(cls, payload, 'record_id', 'record_sha256', 'upst')


def evaluate_sequence_claim(
    profile: PowerSequencingProfile | None,
    event: PowerSequenceEvent | None,
) -> tuple[str, str]:
    """Gate a deterministic-order claim on observed sequence evidence."""
    if profile is None:
        return ('unverified_sequencing', 'no_declared_profile')
    if event is None:
        return ('unverified_sequencing', 'no_observed_transition')
    if event.observed_stage_order != profile.stages:
        return ('unverified_sequencing', 'observed_order_mismatch')
    if profile.direction == 'power_on':
        amp = event.amplifier_on_after_processing
        if profile.amplifier_device_ids and amp is False:
            return ('amplifier_order_violation',
                    'amplifier_on_before_processing')
        if profile.amplifier_device_ids and amp is None:
            return ('insufficient_evidence',
                    'amplifier_timing_unobserved')
    if profile.direction == 'power_off':
        amp = event.amplifier_off_before_processing
        if profile.amplifier_device_ids and amp is False:
            return ('amplifier_order_violation',
                    'amplifier_off_after_processing')
        if profile.amplifier_device_ids and amp is None:
            return ('insufficient_evidence',
                    'amplifier_timing_unobserved')
    if profile.always_on_device_ids:
        survived = event.control_survived_outage
        if survived is False:
            return ('always_on_violation',
                    'control_or_network_dropped')
        if survived is None:
            return ('insufficient_evidence',
                    'always_on_survival_unobserved')
    return ('sequence_verified', 'order_and_timing_observed')
