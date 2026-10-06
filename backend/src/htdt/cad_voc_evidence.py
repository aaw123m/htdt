"""Acoustic-material VOC / chemical-emission evidence (issue #750).

Acoustically suitable and fire-documented material does NOT prove low
chemical emissions, and a product emission certificate does NOT prove
the completed occupied room's IAQ. Emission evidence must pin the
product certificate/test per scheme, and room-level conclusions
require occupied-room IAQ evidence (#740) — never extrapolated from a
cert alone.

Basis: issue #750 scope; CDPH SM v1.2, AgBB, GREENGUARD emission
schemes; #570/#615/#631 material authorities; #648 fire finish;
#740 occupied IAQ; #596 substitution.
"""

from __future__ import annotations

from typing import Any

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


VOC_LABELS: dict[str, str] = {
    'product_emission_documented': '製品排出は文書化済み',
    'product_emission_undocumented': '製品排出は未文書化',
    'room_iaq_not_proven_by_cert': '製品証明は室内IAQの証明ではない',
    'insufficient_evidence': '証拠不足',
}


class VocEmissionProfile(BaseModel):
    """Product-level chemical-emission evidence (voc- prefix)."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    material_ref: AuthorityRef
    emission_scheme: str | None = None
    certificate_ref: AuthorityRef | None = None
    chamber_test_ref: AuthorityRef | None = None
    expiry: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'VocEmissionProfile':
        _require_refs(self.material_ref)
        if self.emission_scheme is not None \
                and self.certificate_ref is None \
                and self.chamber_test_ref is None:
            raise ValueError(
                'declared scheme needs a pinned certificate or '
                'chamber test')
        for ref in (self.certificate_ref, self.chamber_test_ref):
            if ref is not None:
                _require_refs(ref)
        if self.certificate_ref is None \
                and self.chamber_test_ref is None:
            raise ValueError(
                'emission profile needs certificate or chamber test')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'VocEmissionProfile':
        return _seal(cls, payload, 'profile_id', 'profile_sha256', 'voc')


def evaluate_emission_claim(
    profile: VocEmissionProfile | None,
    claim_kind: str,
    room_iaq_evidence_present: bool = False,
) -> tuple[str, str]:
    """A product cert documents the product — never the finished room."""
    if profile is None:
        return ('product_emission_undocumented', 'no_emission_profile')
    if claim_kind == 'room_iaq':
        if not room_iaq_evidence_present:
            return ('room_iaq_not_proven_by_cert',
                    'needs_occupied_iaq_observation')
        return ('product_emission_documented',
                'product_and_room_evidence_present')
    if claim_kind == 'product_emission':
        return ('product_emission_documented', 'cert_or_chamber_pinned')
    return ('insufficient_evidence', f'unknown claim {claim_kind!r}')
