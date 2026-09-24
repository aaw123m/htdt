"""#590 authority graph explorer tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from htdt.authority_graph import (
    AuthorityDomain,
    AuthorityEdge,
    AuthorityEdgeKind,
    AuthorityInspector,
    AuthorityLifecycle,
    AuthorityNode,
    EvidenceCompleteness,
    StaticAuthoritySource,
    build_authority_graph,
    scene_revision_authority_source,
)


def _chain() -> list:
    return [
        AuthorityNode(
            node_id=f'capture:dataset:{c}',
            domain=AuthorityDomain.CAPTURE,
            node_type='dataset',
            label=f'dataset {c}',
        )
        for c in 'ab'
    ] + [
        AuthorityNode(
            node_id='prediction:run:p1',
            domain=AuthorityDomain.PREDICTION,
            node_type='run',
            label='prediction run',
        ),
        AuthorityEdge(
            kind=AuthorityEdgeKind.DERIVED_FROM,
            source='prediction:run:p1',
            target='capture:dataset:a',
        ),
        AuthorityEdge(
            kind=AuthorityEdgeKind.EVIDENCE_FOR,
            source='prediction:run:p1',
            target='capture:dataset:b',
        ),
    ]


def test_build_graph_and_traversals() -> None:
    graph = build_authority_graph([StaticAuthoritySource(_chain())])
    assert len(graph.nodes) == 3

    upstream = graph.upstream('prediction:run:p1')
    assert {n.node_id for n in upstream} == {
        'capture:dataset:a',
        'capture:dataset:b',
    }
    downstream = graph.downstream('capture:dataset:a')
    assert [n.node_id for n in downstream] == ['prediction:run:p1']


def test_missing_edge_targets_become_placeholders() -> None:
    source = StaticAuthoritySource(
        [
            AuthorityEdge(
                kind=AuthorityEdgeKind.DERIVED_FROM,
                source='prediction:run:p1',
                target='measurement:dataset:gone',
            ),
        ]
    )
    graph = build_authority_graph([source])
    missing = graph.node('measurement:dataset:gone')
    assert missing is not None
    assert missing.domain == AuthorityDomain.MISSING

    inspector = AuthorityInspector(graph)
    summary = inspector.summary('measurement:dataset:gone')
    assert summary.evidence_completeness == EvidenceCompleteness.MISSING


def test_why_stale_names_dependencies() -> None:
    contributions = _chain() + [
        AuthorityEdge(
            kind=AuthorityEdgeKind.STALE_BECAUSE,
            source='prediction:run:p1',
            target='room:scene_revision:rev-2',
            detail='scene rev-3 supersedes rev-2',
        ),
    ]
    graph = build_authority_graph([StaticAuthoritySource(contributions)])
    reasons = graph.why_stale('prediction:run:p1')
    assert any('rev-2' in r for r in reasons)


def test_lineage_bounded() -> None:
    items = []
    for i in range(5):
        items.append(
            AuthorityNode(
                node_id=f'room:rev:{i}',
                domain=AuthorityDomain.ROOM,
                node_type='rev',
                label=f'rev {i}',
            )
        )
        if i:
            items.append(
                AuthorityEdge(
                    kind=AuthorityEdgeKind.SUPERSEDES,
                    source=f'room:rev:{i}',
                    target=f'room:rev:{i - 1}',
                )
            )
    graph = build_authority_graph([StaticAuthoritySource(items)])
    one_hop = graph.lineage('room:rev:2', max_hops=1)
    assert set(one_hop.nodes) == {'room:rev:1', 'room:rev:2', 'room:rev:3'}
    two_hop = graph.lineage('room:rev:2', max_hops=2)
    assert len(two_hop.nodes) == 5


def test_rebuild_is_deterministic() -> None:
    items = _chain()
    g1 = build_authority_graph([StaticAuthoritySource(items)])
    g2 = build_authority_graph([StaticAuthoritySource(list(reversed(items)))])
    assert g1.semantic_fingerprint() == g2.semantic_fingerprint()


def test_conflicting_node_contribution_fails() -> None:
    a = AuthorityNode(
        node_id='room:rev:1',
        domain=AuthorityDomain.ROOM,
        node_type='rev',
        label='rev one',
    )
    b = a.model_copy(update={'label': 'different'})
    with pytest.raises(ValueError):
        build_authority_graph(
            [StaticAuthoritySource([a]), StaticAuthoritySource([b])]
        )


def _rev(
    revision_id: str,
    document_id: str = 'doc-1',
    parent: str | None = None,
    detached: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        revision_id=revision_id,
        document_id=document_id,
        parent_revision_id=parent,
        created_at_utc='2026-01-01T00:00:00Z',
        content_hash=f'h-{revision_id}',
        detached=detached,
    )


def test_scene_revision_adapter() -> None:
    revisions = [_rev('rev-1'), _rev('rev-2', parent='rev-1')]
    graph = build_authority_graph(
        [
            scene_revision_authority_source(
                revisions, head_by_document={'doc-1': 'rev-2'}
            )
        ]
    )
    kinds = {e.kind for e in graph.edges}
    assert AuthorityEdgeKind.SUPERSEDES in kinds
    assert AuthorityEdgeKind.BINDS_TO in kinds
    rev1 = graph.node('room:scene_revision:rev-1')
    assert rev1 is not None and rev1.authority_hash == 'h-rev-1'
    assert rev1.lifecycle == AuthorityLifecycle.HISTORICAL
    # Superseded revision is downstream of the newer revision's SUPERSEDES edge.
    rev2 = graph.node('room:scene_revision:rev-2')
    assert rev2 is not None and rev2.lifecycle == AuthorityLifecycle.CURRENT


def test_scene_revision_lifecycle_follows_explicit_head() -> None:
    """#747: only the explicit head is CURRENT; everything else HISTORICAL."""
    revisions = [
        _rev('rev-1'),
        _rev('rev-2', parent='rev-1'),
        _rev('rev-3', parent='rev-2'),
    ]
    graph = build_authority_graph(
        [
            scene_revision_authority_source(
                revisions, head_by_document={'doc-1': 'rev-3'}
            )
        ]
    )
    assert graph.node('room:scene_revision:rev-3').lifecycle == (
        AuthorityLifecycle.CURRENT
    )
    assert graph.node('room:scene_revision:rev-2').lifecycle == (
        AuthorityLifecycle.HISTORICAL
    )
    assert graph.node('room:scene_revision:rev-1').lifecycle == (
        AuthorityLifecycle.HISTORICAL
    )

    # Explicit head wins even when it is not the newest revision.
    graph = build_authority_graph(
        [
            scene_revision_authority_source(
                revisions, head_by_document={'doc-1': 'rev-1'}
            )
        ]
    )
    assert graph.node('room:scene_revision:rev-1').lifecycle == (
        AuthorityLifecycle.CURRENT
    )
    assert graph.node('room:scene_revision:rev-3').lifecycle == (
        AuthorityLifecycle.HISTORICAL
    )


def test_scene_revision_detached_is_detail_not_lifecycle() -> None:
    """#747: detached marks intentional off-head lineage, never CURRENT."""
    revisions = [
        _rev('rev-1'),
        _rev('rev-2', parent='rev-1'),
        _rev('branch-1', parent='rev-1', detached=True),
    ]
    graph = build_authority_graph(
        [
            scene_revision_authority_source(
                revisions, head_by_document={'doc-1': 'rev-2'}
            )
        ]
    )
    branch = graph.node('room:scene_revision:branch-1')
    assert branch.lifecycle == AuthorityLifecycle.HISTORICAL
    assert branch.detached is True


def test_scene_revision_unresolvable_head_is_unknown() -> None:
    """#747: a corrupt/missing head fails loudly instead of CURRENT."""
    revisions = [_rev('rev-1'), _rev('rev-2', parent='rev-1')]
    for heads in (None, {}, {'doc-1': None}):
        graph = build_authority_graph(
            [scene_revision_authority_source(revisions, head_by_document=heads)]
        )
        assert graph.node('room:scene_revision:rev-1').lifecycle == (
            AuthorityLifecycle.UNKNOWN
        )
        assert graph.node('room:scene_revision:rev-2').lifecycle == (
            AuthorityLifecycle.UNKNOWN
        )


def test_inspector_summary_and_dependents() -> None:
    graph = build_authority_graph([StaticAuthoritySource(_chain())])
    inspector = AuthorityInspector(graph)
    summary = inspector.summary('prediction:run:p1')
    assert summary.authority_class == 'prediction:run'
    assert summary.has_lineage
    assert summary.evidence_completeness == EvidenceCompleteness.COMPLETE
    assert 'dataset a' in summary.source_summary
    assert inspector.dependents('capture:dataset:a')


def test_snapshot_is_machine_readable() -> None:
    graph = build_authority_graph([StaticAuthoritySource(_chain())])
    payload = graph.to_snapshot()
    text = json.dumps(payload)  # must be JSON-serializable
    assert 'prediction:run:p1' in text
    assert payload['schema_version'] == graph.schema_version


def test_domain_filter() -> None:
    graph = build_authority_graph([StaticAuthoritySource(_chain())])
    capture_only = graph.filter_domains({AuthorityDomain.CAPTURE})
    assert set(capture_only.nodes) == {
        'capture:dataset:a',
        'capture:dataset:b',
    }
    assert capture_only.edges == ()
