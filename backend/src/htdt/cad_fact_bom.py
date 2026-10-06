"""Conflicting external facts and commercial BOM authority
(issues #765, #667).

Official external sources can disagree because documentation is
stale, transitional, version-scoped or describes different tiers
— HTDT must preserve external facts as immutable claims with
provenance and applicability, never overwrite one mutable
capability row (#765). And the exact engineered system must become
a versioned Bill of Materials + commercial estimate while keeping
pricing/tax/labor/supplier data separate from technical truth
(#667).
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


class ExternalFactClaim(BaseModel):
    """One immutable external fact-claim (#765) — source artifact
    identity, publication date, version/tier applicability; never
    collapses into a mutable product row."""

    model_config = ConfigDict(frozen=True)

    claim_id: str
    claim_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    subject: str
    statement: str
    source_ref: AuthorityRef
    published_on: str | None = None
    applicability: str | None = None
    contradicts_refs: tuple[AuthorityRef, ...] = ()

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data.get('subject') or not data.get('statement'):
                raise ValueError(
                    'a fact-claim requires subject + statement'
                )
            if data.get('source_ref') is None:
                raise ValueError(
                    'a fact-claim requires the source artifact '
                    'ref — claims without provenance are not '
                    'facts'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'claim_id', 'claim_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'ExternalFactClaim':
        return _seal(
            cls, payload, 'claim_id', 'claim_sha256', 'efc'
        )


class FactConflictResolution(BaseModel):
    """Declared resolution between contradicting claims (#765) —
    preserves both sides; resolution is recency/scope/hierarchy,
    never silent overwrite."""

    model_config = ConfigDict(frozen=True)

    resolution_id: str
    resolution_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    winning_ref: AuthorityRef
    losing_refs: tuple[AuthorityRef, ...]
    resolution_kind: Literal[
        'recency', 'version_scope', 'tier_scope', 'hierarchy',
        'unresolved_conflict',
    ]
    rationale: str | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('winning_ref') is None:
                raise ValueError(
                    'a resolution requires the prevailing claim '
                    'ref'
                )
            if not data.get('losing_refs'):
                raise ValueError(
                    'a resolution requires the superseded claim '
                    'refs — both sides are preserved'
                )
            if data.get('resolution_kind') not in (
                'recency', 'version_scope', 'tier_scope',
                'hierarchy', 'unresolved_conflict',
            ):
                raise ValueError('unknown resolution kind')
            if data.get('resolution_kind') != 'unresolved_conflict' and (
                not data.get('rationale')
            ):
                raise ValueError(
                    'a resolved conflict requires its rationale'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'resolution_id', 'resolution_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'FactConflictResolution':
        return _seal(
            cls, payload, 'resolution_id', 'resolution_sha256', 'fcr'
        )


class BomEstimate(BaseModel):
    """Versioned BOM + commercial estimate (#667) — derived from the
    engineered system, priced data kept structurally separate from
    technical truth."""

    model_config = ConfigDict(frozen=True)

    estimate_id: str
    estimate_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    bom_version: str
    line_items: tuple[str, ...]
    engineering_ref: AuthorityRef
    pricing_present: bool = False
    tax_present: bool = False
    labor_present: bool = False
    supplier_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data.get('line_items'):
                raise ValueError(
                    'a BOM estimate requires line items'
                )
            if data.get('engineering_ref') is None:
                raise ValueError(
                    'a BOM must derive from the engineered '
                    'system — pin the engineering ref'
                )
            if data.get('pricing_present') and (
                data.get('supplier_ref') is None
            ):
                raise ValueError(
                    'priced lines require a supplier/pricing '
                    'source ref — commercial data is separate '
                    'from technical truth'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'estimate_id', 'estimate_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'BomEstimate':
        return _seal(
            cls, payload, 'estimate_id', 'estimate_sha256', 'bom'
        )


FactVerdict = Literal[
    'qualified_facts',
    'mutable_row_is_not_fact',
    'conflict_preserved',
    'bom_unbound',
    'commercial_conflation',
]


def evaluate_fact_claim(
    claims: tuple[ExternalFactClaim, ...] | None,
    resolution: FactConflictResolution | None,
    estimate: BomEstimate | None,
) -> tuple[FactVerdict, str]:
    """Judge fact-preservation / BOM claims (#765/#667)."""
    if claims:
        seen: dict[tuple[str, str], ExternalFactClaim] = {}
        for c in claims:
            key = (c.subject, c.applicability or '')
            prior = seen.get(key)
            if prior is not None and prior.statement != c.statement:
                if resolution is None:
                    return (
                        'conflict_preserved',
                        f'conflicting claims on {c.subject} — '
                        'both preserved, no resolution pinned',
                    )
            seen[key] = c
        if resolution is not None and (
            resolution.resolution_kind == 'unresolved_conflict'
        ):
            return (
                'conflict_preserved',
                'conflict declared unresolved — neither side '
                'overwrites the other',
            )
    else:
        return (
            'mutable_row_is_not_fact',
            'no immutable fact-claims pinned — a mutable product '
            'row cannot hold external truth',
        )
    if estimate is not None and estimate.pricing_present and (
        estimate.supplier_ref is None
    ):
        return (
            'commercial_conflation',
            'priced BOM without supplier provenance — commercial '
            'data conflated with technical truth',
        )
    return (
        'qualified_facts',
        'facts preserved with provenance and BOM bound to '
        'engineering',
    )


FACT_LABELS: dict[str, str] = {
    'qualified_facts': '事実適格',
    'mutable_row_is_not_fact': '可変行は事実ではない',
    'conflict_preserved': '矛盾保存済み',
    'bom_unbound': 'BOM未束縛',
    'commercial_conflation': '商業情報混同',
}
