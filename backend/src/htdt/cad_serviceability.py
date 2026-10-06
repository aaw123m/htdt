"""AV serviceability / maintenance-envelope authority (issue #707).

An AV component can fit geometrically, operate thermally and be
structurally supported, yet still be unmaintainable: filters,
connectors, adjustment points, lamps/modules, cable service loops,
fasteners or the device itself may be unreachable after construction.
CAD fit does NOT imply serviceable — the service envelope must be
declared and verified separately.

Basis: issue #707 scope; AVIXA performance-verification guidance;
#587 rack; #596 substitution; #620 mounting; #624 projector.
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


ServiceItem = Literal[
    'filter', 'connectors', 'adjustment', 'lamp_module',
    'cable_service_loop', 'fasteners', 'device_replacement',
    'ventilation_access', 'other',
]

SERVICE_LABELS: dict[str, str] = {
    'serviceable_verified': '保守性は検証済み',
    'service_blocked': '保守経路が塞がれている',
    'fit_is_not_serviceable': 'CAD適合は保守性を意味しない',
    'insufficient_evidence': '証拠不足',
}


class ServiceEnvelopeProfile(BaseModel):
    """Declared service-access requirements for a device
    (svc- prefix)."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    device_ref: AuthorityRef
    required_access_items: tuple[str, ...]
    access_direction: str | None = None
    service_clearance_m: float | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'ServiceEnvelopeProfile':
        _require_refs(self.device_ref)
        if not self.required_access_items:
            raise ValueError('required_access_items must not be empty')
        for item in self.required_access_items:
            if item not in (
                'filter', 'connectors', 'adjustment', 'lamp_module',
                'cable_service_loop', 'fasteners', 'device_replacement',
                'ventilation_access', 'other',
            ):
                raise ValueError(f'unknown access item {item!r}')
        if self.service_clearance_m is not None \
                and self.service_clearance_m < 0.0:
            raise ValueError('service_clearance_m must be >= 0')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'ServiceEnvelopeProfile':
        return _seal(cls, payload, 'profile_id', 'profile_sha256', 'svc')


class ServiceAccessObservation(BaseModel):
    """Installed-state access verification bound to a service
    envelope (svo- prefix)."""

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    envelope_ref: AuthorityRef
    reachable_items: tuple[str, ...] = ()
    blocked_items: tuple[str, ...] = ()
    evidence_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'ServiceAccessObservation':
        _require_refs(self.envelope_ref)
        overlap = set(self.reachable_items) & set(self.blocked_items)
        if overlap:
            raise ValueError(
                f'items cannot be both reachable and blocked: '
                f'{sorted(overlap)}')
        if self.evidence_ref is not None:
            _require_refs(self.evidence_ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'ServiceAccessObservation':
        return _seal(
            cls, payload, 'observation_id',
            'observation_sha256', 'svo')


def evaluate_serviceability_claim(
    profile: ServiceEnvelopeProfile | None,
    observation: ServiceAccessObservation | None,
    cad_fit_ok: bool = False,
) -> tuple[str, str]:
    """Geometric fit never implies the device is serviceable."""
    if profile is None:
        if cad_fit_ok:
            return ('fit_is_not_serviceable',
                    'cad_fit_without_service_envelope')
        return ('insufficient_evidence', 'no_service_profile')
    if observation is None:
        return ('insufficient_evidence',
                'no_installed_access_verification')
    blocked = set(observation.blocked_items) & set(
        profile.required_access_items)
    if blocked:
        return ('service_blocked',
                'required_items_blocked:' + ','.join(sorted(blocked)))
    missing = set(profile.required_access_items) - set(
        observation.reachable_items)
    if missing:
        return ('insufficient_evidence',
                'unverified_items:' + ','.join(sorted(missing)))
    return ('serviceable_verified',
            'all_required_access_items_reachable')
