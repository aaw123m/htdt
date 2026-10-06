"""AV/ICT product-safety certification evidence (issue #751).

Engineering compatibility (electrical, performance, fit) does NOT
imply that a device's product-safety certification/listing is valid
or applicable for the project market. Certification evidence must
pin the listing body, standard, certificate identity and market
scope before a device may be claimed safety-certified.

Basis: issue #751 scope; IEC/UL 62368-1 (AV/ICT safety);
#586 performance facts; #587 rack/power; #596 substitution; #620
mounting; #648 fire finish; #667 BOM.
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


ListingKind = Literal[
    'nrtl_listed', 'certified', 'declared_conformity',
    'component_recognized', 'unlisted', 'unknown',
]

SAFETY_LABELS: dict[str, str] = {
    'certified_for_market': '市場向け認証済み',
    'certified_other_market': '他市場向け認証（適用外の可能性）',
    'declared_only': '自己宣言のみ',
    'compatibility_is_not_certification': '工学的適合は認証ではない',
    'insufficient_evidence': '証拠不足',
}


class ProductSafetyProfile(BaseModel):
    """Safety certification/listing profile bound to a product +
    market (psf- prefix)."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    product_ref: AuthorityRef
    listing_kind: ListingKind
    standard: str | None = None
    certificate_ref: AuthorityRef | None = None
    market: str | None = None
    certified_model_identity: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'ProductSafetyProfile':
        _require_refs(self.product_ref)
        if self.listing_kind in (
            'nrtl_listed', 'certified', 'declared_conformity',
            'component_recognized',
        ):
            if self.standard is None:
                raise ValueError(
                    'a claimed listing must declare its standard')
            if self.certificate_ref is None:
                raise ValueError(
                    'a claimed listing must pin certificate_ref')
        if self.certificate_ref is not None:
            _require_refs(self.certificate_ref)
        if self.listing_kind == 'unlisted' \
                and self.certificate_ref is not None:
            raise ValueError(
                'unlisted cannot pin a certificate')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'ProductSafetyProfile':
        return _seal(cls, payload, 'profile_id', 'profile_sha256', 'psf')


def evaluate_safety_claim(
    profile: ProductSafetyProfile | None,
    project_market: str,
    engineering_compatible: bool = False,
) -> tuple[str, str]:
    """Compatibility never substitutes for a market-valid listing."""
    if profile is None:
        if engineering_compatible:
            return ('compatibility_is_not_certification',
                    'engineering_fit_without_listing')
        return ('insufficient_evidence', 'no_safety_profile')
    if profile.listing_kind == 'unknown':
        return ('insufficient_evidence', 'listing_kind_unknown')
    if profile.listing_kind == 'unlisted':
        return ('insufficient_evidence', 'product_unlisted')
    if profile.listing_kind == 'declared_conformity':
        return ('declared_only', 'self_declaration_not_listing')
    if profile.listing_kind == 'component_recognized':
        return ('insufficient_evidence',
                'component_recognition_not_product_listing')
    if profile.market is not None and profile.market != project_market:
        return ('certified_other_market',
                'listing_market_mismatch')
    if profile.certified_model_identity is not None \
            and profile.certified_model_identity != profile.product_ref.ref_id:
        return ('insufficient_evidence',
                'certified_model_differs')
    return ('certified_for_market', 'listing_pinned_for_market')
