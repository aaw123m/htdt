"""External evidence source-conflict / supersession authority
(issue #765).

Official external sources can disagree because documentation is
stale, transitional, version-scoped or describing different product
tiers. A newer document does NOT automatically supersede an older
one — supersession must be scoped (same product, same tier, same
capability scope) and conflicting sources must be reconciled
explicitly, never averaged or silently preferred.

Basis: issue #765 scope (Trinnov WaveForming KB-vs-article tier
conflict, Dirac 3.14.3 version-scoped documentation); #586
performance facts; #599 external standards lifecycle.
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


SourceTier = Literal[
    'primary_vendor_doc', 'vendor_article', 'vendor_kb',
    'third_party_review', 'marketing', 'unknown',
]
ResolutionKind = Literal[
    'superseded', 'scoped_both_valid', 'unresolved_conflict',
    'needs_vendor_clarification',
]

CONFLICT_LABELS: dict[str, str] = {
    'superseded': '新版が旧版を正式に置き換え',
    'scoped_both_valid': 'スコープが異なり両方有効',
    'unresolved_conflict': '未解決の矛盾',
    'needs_vendor_clarification': 'ベンダー確認が必要',
}


class ExternalEvidenceSource(BaseModel):
    """A pinned external document/claim source (ees- prefix)."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    source_tier: SourceTier
    publisher: str
    published_at: str
    scope: str
    product_family: str | None = None
    product_tier: str | None = None
    content_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'ExternalEvidenceSource':
        if not self.publisher or not self.scope:
            raise ValueError('publisher and scope must be declared')
        if not self.published_at:
            raise ValueError('published_at must be declared')
        if self.content_ref is not None:
            _require_refs(self.content_ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'source_id', 'source_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'ExternalEvidenceSource':
        return _seal(cls, payload, 'source_id', 'source_sha256', 'ees')


class EvidenceSupersessionRecord(BaseModel):
    """Reconciliation verdict for a set of conflicting sources
    (ess- prefix)."""

    model_config = ConfigDict(frozen=True)

    record_id: str
    record_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    source_refs: tuple[AuthorityRef, ...]
    resolution: ResolutionKind
    winning_source_ref: AuthorityRef | None = None
    rationale: str

    @model_validator(mode='after')
    def _validate(self) -> 'EvidenceSupersessionRecord':
        if len(self.source_refs) < 2:
            raise ValueError(
                'supersession needs at least two source refs')
        _require_refs(*self.source_refs)
        if self.resolution == 'superseded':
            if self.winning_source_ref is None:
                raise ValueError(
                    'superseded resolution needs winning_source_ref')
        if self.winning_source_ref is not None:
            _require_refs(self.winning_source_ref)
        if not self.rationale:
            raise ValueError('resolution rationale must be declared')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'record_id', 'record_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'EvidenceSupersessionRecord':
        return _seal(cls, payload, 'record_id', 'record_sha256', 'ess')


def evaluate_conflict_claim(
    sources: tuple[ExternalEvidenceSource, ...],
) -> tuple[str, str]:
    """Newer never automatically wins — supersession must share scope.

    Same product_family + same product_tier + same scope: newest
    published_at supersedes. Different tiers/scopes: both can be valid
    (scoped_both_valid). Mixed or undeclared scope: needs vendor
    clarification.
    """
    if len(sources) < 2:
        return ('unresolved_conflict', 'fewer_than_two_sources')
    families = {s.product_family for s in sources}
    tiers = {s.product_tier for s in sources}
    scopes = {s.scope for s in sources}
    if len(families) > 1 or len(tiers) > 1:
        return ('scoped_both_valid', 'different_product_scope')
    if len(scopes) > 1:
        return ('needs_vendor_clarification',
                'same_product_different_capability_scope')
    if any(s.published_at == '' for s in sources):
        return ('needs_vendor_clarification', 'missing_dates')
    return ('unresolved_conflict',
            'same_scope_conflict_needs_explicit_resolution')
