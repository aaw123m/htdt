"""Authority graph explorer backend (#590).

HTDT models a dense authority graph — SceneRevisions, capture evidence,
semantic geometry, equipment/directivity, measurement datasets, predictions,
optimization candidates, SystemVariants, as-built/measured lifecycle,
treatments, calibration, standards, installation output. The backend
preserves the lineage; this module makes it **inspectable**: a read-side
projection that answers "where did this come from", "what depends on it",
"why is it stale" — from any project object.

Architecture contract:

* the graph is a **read-side projection/index** assembled from canonical
  persisted records contributed by registered sources; existing
  repositories stay authoritative and the graph never becomes an alternate
  authority store;
* a cached graph is disposable — :func:`build_authority_graph` rebuild
  produces the same semantic edges from the same canonical state;
* a small cross-domain edge vocabulary (:class:`AuthorityEdgeKind`) instead
  of parsing arbitrary JSON;
* edges referencing unknown authority resolve to explicit
  ``missing`` placeholder nodes — an unknown edge is shown, never dropped;
* no graph interaction mutates authority: the module exposes reads only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical_json import canonical_sha256
from .workflow_navigation import WorkspaceDeepLink, WorkspaceId


GRAPH_SCHEMA_VERSION = 1
DEFAULT_MAX_HOPS = 2
HARD_MAX_HOPS = 4


class AuthorityEdgeKind(StrEnum):
    """The cross-domain edge vocabulary (bounded, not free-form)."""

    DERIVED_FROM = 'derived_from'
    BINDS_TO = 'binds_to'
    MEASURED_FOR = 'measured_for'
    PROPOSES = 'proposes'
    APPLIED_AS = 'applied_as'
    SUPERSEDES = 'supersedes'
    VALIDATES = 'validates'
    INVALIDATES = 'invalidates'
    STALE_BECAUSE = 'stale_because'
    IMPORTED_FROM = 'imported_from'
    EVIDENCE_FOR = 'evidence_for'


class AuthorityDomain(StrEnum):
    PROJECT = 'project'
    ROOM = 'room'
    CAPTURE = 'capture'
    EQUIPMENT = 'equipment'
    MEASUREMENT = 'measurement'
    PREDICTION = 'prediction'
    OPTIMIZATION = 'optimization'
    INSTALLATION = 'installation'
    OTHER = 'other'
    MISSING = 'missing'


class AuthorityLifecycle(StrEnum):
    CURRENT = 'current'
    PROPOSED = 'proposed'
    AS_BUILT = 'as_built'
    MEASURED = 'measured'
    DERIVED = 'derived'
    HYPOTHESIS = 'hypothesis'
    HISTORICAL = 'historical'
    STALE = 'stale'
    INVALID = 'invalid'
    UNKNOWN = 'unknown'


class AuthorityNode(BaseModel):
    """One inspectable authority.

    ``node_id`` is the stable graph identifier (``domain:type:id``);
    ``authority_ref`` is the canonical persisted id/hash pair where one
    exists. ``deep_link`` targets the owning workspace for navigation.
    """

    model_config = ConfigDict(frozen=True)

    node_id: str = Field(min_length=1)
    domain: AuthorityDomain
    node_type: str = Field(min_length=1)
    label: str = Field(min_length=1)
    lifecycle: AuthorityLifecycle = AuthorityLifecycle.UNKNOWN
    authority_id: str | None = None
    version: str | None = None
    authority_hash: str | None = None
    created_at_utc: str | None = None
    deep_link: WorkspaceDeepLink | None = None
    stale: bool = False
    stale_reasons: tuple[str, ...] = ()
    # Lineage attribute, not a lifecycle: an intentionally off-mainline
    # revision keeps this detail while its lifecycle follows the explicit
    # head authority (#747).
    detached: bool = False

    @model_validator(mode='after')
    def valid_node(self) -> 'AuthorityNode':
        if self.stale and self.lifecycle == AuthorityLifecycle.CURRENT:
            raise ValueError('a stale node cannot claim current lifecycle')
        if self.lifecycle == AuthorityLifecycle.STALE and not self.stale:
            object.__setattr__(self, 'stale', True)
        return self


class AuthorityEdge(BaseModel):
    """A typed, directed edge between two authority nodes."""

    model_config = ConfigDict(frozen=True)

    kind: AuthorityEdgeKind
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    detail: str | None = None

    @property
    def edge_id(self) -> str:
        return f'{self.source}-[{self.kind.value}]->{self.target}'


class AuthorityGraph(BaseModel):
    """Immutable projected authority graph; safe to cache and rebuild."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = GRAPH_SCHEMA_VERSION
    nodes: dict[str, AuthorityNode] = Field(default_factory=dict)
    edges: tuple[AuthorityEdge, ...] = ()

    @model_validator(mode='after')
    def valid_graph(self) -> 'AuthorityGraph':
        seen = set()
        for edge in self.edges:
            if edge.edge_id in seen:
                raise ValueError(f'duplicate edge {edge.edge_id}')
            seen.add(edge.edge_id)
        return self

    # -- read-side traversals ---------------------------------------------

    def node(self, node_id: str) -> AuthorityNode | None:
        return self.nodes.get(node_id)

    def outgoing(self, node_id: str) -> tuple[AuthorityEdge, ...]:
        return tuple(e for e in self.edges if e.source == node_id)

    def incoming(self, node_id: str) -> tuple[AuthorityEdge, ...]:
        return tuple(e for e in self.edges if e.target == node_id)

    def upstream(self, node_id: str) -> tuple[AuthorityNode, ...]:
        """Evidence and prerequisites this node derives/binds from."""

        deps = {
            e.target
            for e in self.outgoing(node_id)
            if e.kind
            in (
                AuthorityEdgeKind.DERIVED_FROM,
                AuthorityEdgeKind.BINDS_TO,
                AuthorityEdgeKind.MEASURED_FOR,
                AuthorityEdgeKind.PROPOSES,
                AuthorityEdgeKind.IMPORTED_FROM,
                AuthorityEdgeKind.EVIDENCE_FOR,
                AuthorityEdgeKind.VALIDATES,
            )
        }
        return tuple(
            self.nodes[n] for n in sorted(deps) if n in self.nodes
        )

    def downstream(self, node_id: str) -> tuple[AuthorityNode, ...]:
        """What uses this — outputs that depend on the node."""

        deps = {
            e.source
            for e in self.incoming(node_id)
            if e.kind
            in (
                AuthorityEdgeKind.DERIVED_FROM,
                AuthorityEdgeKind.BINDS_TO,
                AuthorityEdgeKind.MEASURED_FOR,
                AuthorityEdgeKind.PROPOSES,
                AuthorityEdgeKind.IMPORTED_FROM,
                AuthorityEdgeKind.EVIDENCE_FOR,
                AuthorityEdgeKind.VALIDATES,
            )
        }
        return tuple(
            self.nodes[n] for n in sorted(deps) if n in self.nodes
        )

    def why_stale(self, node_id: str) -> tuple[str, ...]:
        """Concrete invalidating dependencies — never just "stale"."""

        reasons: list[str] = []
        node = self.nodes.get(node_id)
        if node is not None:
            reasons.extend(node.stale_reasons)
        for edge in self.outgoing(node_id):
            if edge.kind in (
                AuthorityEdgeKind.STALE_BECAUSE,
                AuthorityEdgeKind.INVALIDATES,
            ):
                target = self.nodes.get(edge.target)
                label = target.label if target else edge.target
                reasons.append(
                    f'{edge.kind.value}: {label}'
                    + (f' — {edge.detail}' if edge.detail else '')
                )
        for edge in self.incoming(node_id):
            if edge.kind == AuthorityEdgeKind.INVALIDATES:
                source = self.nodes.get(edge.source)
                label = source.label if source else edge.source
                reasons.append(f'invalidated by: {label}')
        return tuple(dict.fromkeys(reasons))

    def lineage(
        self,
        node_id: str,
        *,
        max_hops: int = DEFAULT_MAX_HOPS,
    ) -> 'AuthorityGraph':
        """Bounded neighborhood subgraph (default 1–2 hops; hard cap 4)."""

        hops = max(1, min(int(max_hops), HARD_MAX_HOPS))
        keep = {node_id}
        frontier = {node_id}
        for _ in range(hops):
            nxt: set[str] = set()
            for edge in self.edges:
                if edge.source in frontier:
                    nxt.add(edge.target)
                if edge.target in frontier:
                    nxt.add(edge.source)
            nxt -= keep
            keep |= nxt
            frontier = nxt
            if not frontier:
                break
        return AuthorityGraph(
            nodes={k: v for k, v in self.nodes.items() if k in keep},
            edges=tuple(
                e for e in self.edges if e.source in keep and e.target in keep
            ),
        )

    def filter_domains(
        self, domains: Iterable[AuthorityDomain]
    ) -> 'AuthorityGraph':
        wanted = set(domains)
        nodes = {k: v for k, v in self.nodes.items() if v.domain in wanted}
        return AuthorityGraph(
            nodes=nodes,
            edges=tuple(
                e
                for e in self.edges
                if e.source in nodes and e.target in nodes
            ),
        )

    def semantic_fingerprint(self) -> str:
        """Digest over node ids + edge ids; rebuilds must reproduce it."""

        payload = {
            'nodes': sorted(
                f'{n.node_id}|{n.lifecycle.value}|{n.authority_hash or ""}'
                for n in self.nodes.values()
            ),
            'edges': sorted(e.edge_id for e in self.edges),
        }
        return canonical_sha256(payload)

    def to_snapshot(self) -> dict[str, Any]:
        """Machine-readable lineage snapshot for diagnostics/reproducibility."""

        return {
            'schema_version': self.schema_version,
            'nodes': [
                {
                    'node_id': n.node_id,
                    'domain': n.domain.value,
                    'type': n.node_type,
                    'label': n.label,
                    'lifecycle': n.lifecycle.value,
                    'authority_id': n.authority_id,
                    'version': n.version,
                    'authority_hash': n.authority_hash,
                    'stale': n.stale,
                }
                for n in sorted(self.nodes.values(), key=lambda x: x.node_id)
            ],
            'edges': [
                {
                    'kind': e.kind.value,
                    'source': e.source,
                    'target': e.target,
                    'detail': e.detail,
                }
                for e in self.edges
            ],
        }


class AuthoritySource(Protocol):
    """A canonical repository contributing graph nodes/edges."""

    def contribute(self) -> Iterable[AuthorityNode | AuthorityEdge]: ...


def _missing_node(node_id: str) -> AuthorityNode:
    return AuthorityNode(
        node_id=node_id,
        domain=AuthorityDomain.MISSING,
        node_type='unknown',
        label=node_id,
        lifecycle=AuthorityLifecycle.UNKNOWN,
    )


def build_authority_graph(
    sources: Iterable[AuthoritySource],
) -> AuthorityGraph:
    """Assemble the projection from registered canonical sources.

    Edges to authority a source did not contribute resolve to explicit
    ``missing`` placeholder nodes so a dangling reference is surfaced rather
    than silently dropped. Rebuilding from the same persisted state produces
    the same semantic fingerprint.
    """

    nodes: dict[str, AuthorityNode] = {}
    edges: dict[str, AuthorityEdge] = {}
    pending_targets: set[str] = set()
    for source in sources:
        for item in source.contribute():
            if isinstance(item, AuthorityNode):
                existing = nodes.get(item.node_id)
                if existing is not None and existing.domain == AuthorityDomain.MISSING:
                    nodes[item.node_id] = item  # real node replaces placeholder
                    pending_targets.discard(item.node_id)
                elif existing is None:
                    nodes[item.node_id] = item
                elif existing.model_dump(mode='json') != item.model_dump(mode='json'):
                    raise ValueError(
                        f'conflicting node contributions for {item.node_id}'
                    )
            elif isinstance(item, AuthorityEdge):
                edges.setdefault(item.edge_id, item)
                for ref in (item.source, item.target):
                    if ref not in nodes:
                        pending_targets.add(ref)
            else:
                raise TypeError(f'unsupported graph contribution {item!r}')
    for ref in sorted(pending_targets):
        nodes.setdefault(ref, _missing_node(ref))
    return AuthorityGraph(
        nodes=nodes,
        edges=tuple(edges[eid] for eid in sorted(edges)),
    )


# ---------------------------------------------------------------------------
# Compact inspector + explorer view-models.


class EvidenceCompleteness(StrEnum):
    COMPLETE = 'complete'
    PARTIAL = 'partial'
    MISSING = 'missing'
    UNKNOWN = 'unknown'


class AuthoritySummary(BaseModel):
    """Compact inspector projection shown in the details surface."""

    model_config = ConfigDict(frozen=True)

    node_id: str
    authority_class: str
    lifecycle: AuthorityLifecycle
    authority_id: str | None
    version: str | None
    authority_hash: str | None
    created_at_utc: str | None
    source_summary: str | None
    freshness: str
    evidence_completeness: EvidenceCompleteness
    has_lineage: bool


class AuthorityInspector:
    """Read-side inspector over an :class:`AuthorityGraph`."""

    def __init__(self, graph: AuthorityGraph) -> None:
        self.graph = graph

    def summary(self, node_id: str) -> AuthoritySummary:
        node = self.graph.node(node_id)
        if node is None:
            raise KeyError(f'unknown authority node {node_id}')
        upstream = self.graph.upstream(node_id)
        if node.stale or node.lifecycle == AuthorityLifecycle.STALE:
            freshness = 'stale'
        elif node.lifecycle == AuthorityLifecycle.HISTORICAL:
            freshness = 'historical'
        elif node.lifecycle == AuthorityLifecycle.CURRENT:
            freshness = 'current'
        else:
            freshness = node.lifecycle.value
        completeness = EvidenceCompleteness.UNKNOWN
        if node.domain == AuthorityDomain.MISSING:
            completeness = EvidenceCompleteness.MISSING
        elif upstream:
            completeness = EvidenceCompleteness.COMPLETE
        elif node.lifecycle in (
            AuthorityLifecycle.CURRENT,
            AuthorityLifecycle.AS_BUILT,
            AuthorityLifecycle.MEASURED,
        ):
            completeness = EvidenceCompleteness.COMPLETE
        else:
            completeness = EvidenceCompleteness.PARTIAL
        source_summary = None
        if upstream:
            source_summary = ', '.join(n.label for n in upstream[:3])
            if len(upstream) > 3:
                source_summary += f' +{len(upstream) - 3}'
        return AuthoritySummary(
            node_id=node.node_id,
            authority_class=f'{node.domain.value}:{node.node_type}',
            lifecycle=node.lifecycle,
            authority_id=node.authority_id,
            version=node.version,
            authority_hash=node.authority_hash,
            created_at_utc=node.created_at_utc,
            source_summary=source_summary,
            freshness=freshness,
            evidence_completeness=completeness,
            has_lineage=bool(upstream or self.graph.downstream(node_id)),
        )

    def lineage_view(
        self, node_id: str, *, max_hops: int = DEFAULT_MAX_HOPS
    ) -> AuthorityGraph:
        return self.graph.lineage(node_id, max_hops=max_hops)

    def why_stale(self, node_id: str) -> tuple[str, ...]:
        return self.graph.why_stale(node_id)

    def dependents(self, node_id: str) -> tuple[AuthorityNode, ...]:
        return self.graph.downstream(node_id)


# ---------------------------------------------------------------------------
# Canonical-source adapters.


def scene_revision_node_id(revision_id: str) -> str:
    """Authority-graph node id for a scene revision — the id the solver
    ledger's resolved revisions and deep links target."""
    return f'room:scene_revision:{revision_id}'


def scene_revision_authority_source(
    revisions: Iterable[Any],
    *,
    head_by_document: Mapping[str, str | None] | None = None,
) -> 'StaticAuthoritySource':
    """Map persisted :class:`~htdt.cad_repository.SceneRevision` records.

    Produces one node per revision plus SUPERSEDES edges to parents and
    BINDS_TO edges to the owning document — the exact lineage the graph
    explorer shows for scene history.

    Lifecycle follows the **explicit head authority**
    (``scene_document_heads``), never the ``detached`` lineage flag
    (#747): the revision that equals the document's head is CURRENT,
    every other resolvable revision is HISTORICAL, and a document with no
    resolvable head yields UNKNOWN nodes rather than invented CURRENT
    status. ``detached`` stays a node attribute describing intentional
    off-head lineage.
    """

    contributions: list[AuthorityNode | AuthorityEdge] = []
    seen_documents: set[str] = set()
    revision_ids: set[str] = set()
    material: list[Any] = list(revisions)
    for rev in material:
        revision_ids.add(rev.revision_id)
    heads = head_by_document or {}
    for rev in material:
        doc_id = rev.document_id
        if doc_id not in seen_documents:
            seen_documents.add(doc_id)
            contributions.append(
                AuthorityNode(
                    node_id=f'room:document:{doc_id}',
                    domain=AuthorityDomain.ROOM,
                    node_type='document',
                    label=f'Document {doc_id}',
                    lifecycle=AuthorityLifecycle.CURRENT,
                    authority_id=doc_id,
                    deep_link=WorkspaceDeepLink(
                        WorkspaceId.ROOM, section='history'
                    ),
                )
            )
        head_id = heads.get(doc_id)
        if head_id is None:
            lifecycle = AuthorityLifecycle.UNKNOWN
        elif rev.revision_id == head_id:
            lifecycle = AuthorityLifecycle.CURRENT
        else:
            lifecycle = AuthorityLifecycle.HISTORICAL
        contributions.append(
            AuthorityNode(
                node_id=scene_revision_node_id(rev.revision_id),
                domain=AuthorityDomain.ROOM,
                node_type='scene_revision',
                label=f'SceneRevision {rev.revision_id}',
                lifecycle=lifecycle,
                authority_id=rev.revision_id,
                authority_hash=rev.content_hash,
                created_at_utc=rev.created_at_utc,
                detached=bool(getattr(rev, 'detached', False)),
                deep_link=WorkspaceDeepLink(
                    WorkspaceId.ROOM, section='history', entity_id=rev.revision_id
                ),
            )
        )
        contributions.append(
            AuthorityEdge(
                kind=AuthorityEdgeKind.BINDS_TO,
                source=scene_revision_node_id(rev.revision_id),
                target=f'room:document:{doc_id}',
            )
        )
        if rev.parent_revision_id:
            contributions.append(
                AuthorityEdge(
                    kind=AuthorityEdgeKind.SUPERSEDES,
                    source=scene_revision_node_id(rev.revision_id),
                    target=scene_revision_node_id(rev.parent_revision_id),
                )
            )
    return StaticAuthoritySource(contributions)


def measurement_authority_source(
    measurements: Iterable[Any],
    *,
    head_content_hash_by_document: Mapping[str, str | None] | None = None,
) -> 'StaticAuthoritySource':
    """Map persisted :class:`~htdt.cad_measurement_models.CadMeasurementRecord`s.

    One node per measurement plus a MEASURED_FOR edge to the scene revision
    it was recorded against, so the explorer can answer "which measurements
    belong to this room state". A measurement whose ``scene_content_hash``
    no longer matches the document's head revision hash is reported
    ``stale`` with the reason recorded — that is the practical "old data"
    answer the inspector exists to give.
    """

    contributions: list[AuthorityNode | AuthorityEdge] = []
    heads = head_content_hash_by_document or {}
    for record in measurements:
        node_id = f'measurement:measurement:{record.measurement_id}'
        head_hash = heads.get(record.document_id)
        stale = head_hash is not None and record.scene_content_hash != head_hash
        lifecycle = (
            AuthorityLifecycle.MEASURED
            if record.evidence_type == 'measured'
            else AuthorityLifecycle.DERIVED
        )
        contributions.append(
            AuthorityNode(
                node_id=node_id,
                domain=AuthorityDomain.MEASUREMENT,
                node_type='measurement',
                label=f'測定 {record.measurement_entity_id} ({record.evidence_type})',
                lifecycle=lifecycle,
                authority_id=record.measurement_id,
                authority_hash=record.scene_content_hash,
                created_at_utc=record.captured_at or record.imported_at,
                stale=stale,
                stale_reasons=(
                    ('測定時の部屋リビジョン内容と現在のヘッドが一致しません',)
                    if stale
                    else ()
                ),
                deep_link=WorkspaceDeepLink(
                    WorkspaceId.MEASUREMENT,
                    entity_id=record.measurement_id,
                    kind='measurement',
                ),
            )
        )
        contributions.append(
            AuthorityEdge(
                kind=AuthorityEdgeKind.MEASURED_FOR,
                source=node_id,
                target=scene_revision_node_id(record.scene_revision_id),
            )
        )
    return StaticAuthoritySource(contributions)


def system_variant_authority_source(
    variants: Iterable[Any],
    *,
    head_content_hash_by_document: Mapping[str, str | None] | None = None,
) -> 'StaticAuthoritySource':
    """Map persisted :class:`~htdt.cad_system_variant.SystemVariant`s.

    One node per variant, DERIVED_FROM its baseline scene revision, a
    SUPERSEDES edge to its parent variant when present, and BINDS_TO the
    owning document. A variant whose baseline hash no longer matches the
    document head's content hash is stale — it was proposed on room state
    that has since changed.
    """

    contributions: list[AuthorityNode | AuthorityEdge] = []
    heads = head_content_hash_by_document or {}
    for variant in variants:
        node_id = f'optimization:system_variant:{variant.variant_id}'
        head_hash = heads.get(variant.document_id)
        stale = (
            head_hash is not None
            and variant.baseline_content_hash != head_hash
        )
        contributions.append(
            AuthorityNode(
                node_id=node_id,
                domain=AuthorityDomain.OPTIMIZATION,
                node_type='system_variant',
                label=variant.name,
                lifecycle=AuthorityLifecycle.PROPOSED,
                authority_id=variant.variant_id,
                authority_hash=variant.variant_sha256,
                created_at_utc=variant.created_at_utc,
                stale=stale,
                stale_reasons=(
                    ('基準となった部屋リビジョン内容と現在のヘッドが一致しません',)
                    if stale
                    else ()
                ),
                deep_link=WorkspaceDeepLink(
                    WorkspaceId.OPTIMIZATION,
                    entity_id=variant.variant_id,
                    kind='system_variant',
                ),
            )
        )
        contributions.append(
            AuthorityEdge(
                kind=AuthorityEdgeKind.DERIVED_FROM,
                source=node_id,
                target=scene_revision_node_id(variant.baseline_revision_id),
            )
        )
        if variant.parent_variant_id:
            contributions.append(
                AuthorityEdge(
                    kind=AuthorityEdgeKind.SUPERSEDES,
                    source=node_id,
                    target=f'optimization:system_variant:{variant.parent_variant_id}',
                )
            )
    return StaticAuthoritySource(contributions)


@dataclass(slots=True)
class StaticAuthoritySource:
    """In-memory source; tests and thin adapters over repositories."""

    contributions: Iterable[AuthorityNode | AuthorityEdge]

    def contribute(self) -> Iterable[AuthorityNode | AuthorityEdge]:
        return tuple(self.contributions)


__all__ = [
    'AuthorityDomain',
    'AuthorityEdge',
    'AuthorityEdgeKind',
    'AuthorityGraph',
    'AuthorityInspector',
    'AuthorityLifecycle',
    'AuthorityNode',
    'AuthoritySource',
    'AuthoritySummary',
    'DEFAULT_MAX_HOPS',
    'EvidenceCompleteness',
    'GRAPH_SCHEMA_VERSION',
    'HARD_MAX_HOPS',
    'StaticAuthoritySource',
    'build_authority_graph',
    'measurement_authority_source',
    'scene_revision_authority_source',
    'scene_revision_node_id',
    'system_variant_authority_source',
]
