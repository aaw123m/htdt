"""#902: one canonical typed authority registry.

Contract: every exact-ref kind resolves through ONE registry of typed
adapters (kind → scope/hash policy/owner repository); both resolver
frameworks — ExactAuthorityResolver and CanonicalAuthorityRefResolver —
delegate to it, and no third resolution story exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_authority_registry import (
    AuthorityKindAdapter,
    CanonicalAuthorityRegistry,
    build_canonical_authority_registry,
)
from htdt.cad_authority_refs import (
    CanonicalAuthorityRefResolver,
    ResolvedAuthority as RefResolvedAuthority,
)
from htdt.cad_authority_resolver import (
    AuthorityRef,
    ExactAuthorityResolver,
)
from htdt.cad_calibration import (
    CadTargetCurve,
    CadTargetCurvePoint,
    CadTargetNormalizationCondition,
)
from htdt.cad_design_comparison import (
    build_alternative,
    build_comparison_set,
)
from htdt.cad_design_comparison_repository import CadDesignComparisonRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene
from htdt.cad_standards import (
    CriterionDefinition,
    CriterionRule,
    CriterionSource,
    build_standards_profile,
)
from htdt.cad_standards_repository import CadStandardsRepository
from htdt.cad_system_variant import ChannelRoleBinding, build_system_variant
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.cad_target_profile import build_target_profile
from htdt.cad_target_profile_repository import CadTargetProfileRepository

NOW = '2026-10-25T00:00:00+00:00'
DOC = 'doc-1'


def _scene(tmp_path: Path) -> SceneRepository:
    repository = SceneRepository(tmp_path / 'scene.sqlite3')
    repository.save(make_empty_scene(DOC), parent_revision_id=None)
    return repository


# -- registry shape ----------------------------------------------------------


def test_registry_registers_each_kind_once_with_metadata(tmp_path: Path) -> None:
    registry = build_canonical_authority_registry(_scene(tmp_path))
    kinds = registry.kinds()
    assert len(kinds) == len(set(kinds))
    for kind in kinds:
        adapter = registry.adapter(kind)
        assert adapter is not None
        assert adapter.kind == kind
        assert adapter.scope in ('project', 'global', 'contextual')
        assert adapter.owner, f'{kind} declares no owner repository'


def test_goal_kind_scopes_are_declared(tmp_path: Path) -> None:
    registry = build_canonical_authority_registry(_scene(tmp_path))
    # Document-owned goal authorities.
    assert registry.adapter('target_curve').scope == 'project'
    # Library-level goal authorities shared across projects.
    assert registry.adapter('standards_profile').scope == 'global'
    assert registry.adapter('video_geometry').scope == 'global'
    # The inbox decides scope per record.
    assert registry.adapter('capture_inbox_item').scope == 'contextual'
    # #871 root: analysis_study's owner is the #594 store, never the
    # #519 InterventionStudySpec family.
    assert registry.adapter('analysis_study').owner == 'CadAnalysisStudyRepository'


def test_unknown_kind_resolves_to_nothing_not_absent(tmp_path: Path) -> None:
    registry = build_canonical_authority_registry(_scene(tmp_path))
    assert not registry.knows('free_text')
    assert registry.resolve('free_text', 'anything', DOC) is None


# -- per-kind resolution ------------------------------------------------------


def test_document_and_scene_kinds(tmp_path: Path) -> None:
    scene_repository = _scene(tmp_path)
    registry = build_canonical_authority_registry(scene_repository)
    head = scene_repository.latest(DOC)

    resolved = registry.resolve('document', DOC, DOC)
    assert resolved is not None and resolved.document_id == DOC
    assert resolved.semantic_sha256 is None  # head moves: id-only
    assert registry.resolve('document', 'ghost', DOC) is None

    resolved = registry.resolve('scene_revision', head.revision_id, DOC)
    assert resolved is not None
    assert resolved.document_id == DOC
    assert resolved.semantic_sha256 == head.content_hash

    resolved = registry.resolve('scene_entity', 'nope', DOC)
    assert resolved is None  # empty scene has no entities


def test_system_variant(tmp_path: Path) -> None:
    scene_repository = _scene(tmp_path)
    revision = scene_repository.latest(DOC)
    variant_repository = CadSystemVariantRepository(scene_repository)
    variant = build_system_variant(
        baseline=revision,
        name='5.1.4 proposal',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front Left'),
        ),
        proposed_entities=(),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant)

    registry = build_canonical_authority_registry(scene_repository)
    resolved = registry.resolve('system_variant', variant.variant_id, DOC)
    assert resolved is not None
    assert resolved.document_id == DOC
    assert resolved.semantic_sha256 == variant.variant_sha256
    assert registry.resolve('system_variant', 'ghost', DOC) is None


def test_comparison_set_and_alternative(tmp_path: Path) -> None:
    scene_repository = _scene(tmp_path)
    revision = scene_repository.latest(DOC)
    repository = CadDesignComparisonRepository(scene_repository)
    alt_a = build_alternative(
        label='A', scene_revision=revision,
        created_at_utc=NOW, alternative_id='alt-a',
    )
    alt_b = build_alternative(
        label='B', scene_revision=revision,
        created_at_utc=NOW, alternative_id='alt-b',
    )
    comparison_set = build_comparison_set(
        document_id=DOC,
        name='options',
        alternatives=(alt_a, alt_b),
        created_at_utc=NOW,
        set_id='set-1',
    )
    repository.save_set(comparison_set)

    registry = build_canonical_authority_registry(scene_repository)
    resolved = registry.resolve(
        'design_comparison_set', 'set-1', DOC
    )
    assert resolved is not None
    assert resolved.semantic_sha256 == comparison_set.set_sha256

    resolved = registry.resolve('comparison_alternative', 'alt-a', DOC)
    assert resolved is not None
    assert resolved.document_id == DOC
    assert resolved.semantic_sha256 == alt_a.alternative_sha256
    assert resolved.container_ids == ('set-1',)


def test_target_curve_goal_kind(tmp_path: Path) -> None:
    scene_repository = _scene(tmp_path)
    profiles = CadTargetProfileRepository(scene_repository)
    profile = build_target_profile(
        document_id=DOC,
        name='House curve',
        kind='in_room',
        source='authored',
        curve=CadTargetCurve(
            points=(
                CadTargetCurvePoint(frequency_hz=20.0, level_db=0.0),
                CadTargetCurvePoint(frequency_hz=20000.0, level_db=-3.0),
            ),
            normalization=CadTargetNormalizationCondition(
                method='reference_frequency', reference_frequency_hz=1000.0
            ),
        ),
        created_at_utc=NOW,
        profile_id='house-curve',
    )
    profiles.save_profile(profile)

    registry = build_canonical_authority_registry(scene_repository)
    resolved = registry.resolve('target_curve', 'house-curve', DOC)
    assert resolved is not None
    assert resolved.document_id == DOC
    assert resolved.semantic_sha256 == profile.semantic_sha256
    assert registry.resolve('target_curve', 'ghost', DOC) is None


def test_standards_profile_goal_kind_is_global(tmp_path: Path) -> None:
    scene_repository = _scene(tmp_path)
    standards = CadStandardsRepository(scene_repository)
    criterion = CriterionDefinition(
        criterion_id='distance',
        name='distance',
        source=CriterionSource(
            publisher='p', document_title='t',
            document_version='1.0', reference='§1',
        ),
        quantity='distance', unit='m',
        applicable_domains=('room',),
        rule=CriterionRule(operator='max', maximum=1.0),
    )
    profile = build_standards_profile(
        profile_id='user-1',
        version='1.0',
        name='Published standard',
        profile_kind='user_defined',
        criteria=(criterion,),
    )
    standards.save_profile(profile)

    registry = build_canonical_authority_registry(scene_repository)
    resolved = registry.resolve('standards_profile', 'user-1', DOC)
    assert resolved is not None
    # Library authority — no project scope, and still hash-bearing.
    assert resolved.document_id is None
    assert resolved.semantic_sha256 == profile.profile_semantic_hash


def test_video_geometry_goal_kind_is_global(tmp_path: Path) -> None:
    # Projector-spec saves require a managed evidence asset — the adapter
    # contract (global scope, hash-bearing, absent → None) is exercised
    # against the real store without manufacturing one.
    scene_repository = _scene(tmp_path)
    registry = build_canonical_authority_registry(scene_repository)
    adapter = registry.adapter('video_geometry')
    assert adapter.scope == 'global' and adapter.hash_bearing
    assert adapter.owner == 'CadVideoGeometryRepository'
    assert (
        registry.resolve('video_geometry', 'ghost-spec', DOC) is None
    )


# -- both resolver frameworks delegate ---------------------------------------


def test_both_resolvers_share_one_resolution_story(tmp_path: Path) -> None:
    scene_repository = _scene(tmp_path)
    head = scene_repository.latest(DOC)

    exact = ExactAuthorityResolver(scene_repository)
    refs = CanonicalAuthorityRefResolver(scene_repository)
    # The two frameworks delegate to a registry — one per deployment is
    # the contract; injecting the same registry makes the sharing explicit.
    shared = exact.registry
    refs_shared = CanonicalAuthorityRefResolver(
        scene_repository, authority_registry=shared
    )

    resolved_exact = exact.resolve(
        AuthorityRef(
            kind='scene_revision',
            ref_id=head.revision_id,
            ref_sha256=head.content_hash,
        ),
        document_id=DOC,
    )
    resolved_ref = refs_shared.resolve(
        'scene_revision', head.revision_id, DOC
    )
    assert resolved_ref is not None
    assert resolved_exact.document_id == resolved_ref.document_id
    assert resolved_exact.semantic_sha256 == resolved_ref.semantic_sha256

    # And the standalone refs resolver resolves identically.
    resolved_default = refs.resolve('scene_revision', head.revision_id, DOC)
    assert isinstance(resolved_default, RefResolvedAuthority)
    assert resolved_default.semantic_sha256 == resolved_ref.semantic_sha256


def test_exact_resolver_enforces_scope_and_hash_pin(tmp_path: Path) -> None:
    scene_repository = _scene(tmp_path)
    head = scene_repository.latest(DOC)
    exact = ExactAuthorityResolver(scene_repository)

    # Hash-bearing authority without the pin fails closed.
    with pytest.raises(ValueError, match='requires ref_sha256'):
        exact.resolve(
            AuthorityRef(kind='scene_revision', ref_id=head.revision_id),
            document_id=DOC,
        )
    # A wrong document fails closed (same store, different document).
    scene_repository.save(make_empty_scene('doc-2'), parent_revision_id=None)
    foreign = scene_repository.latest('doc-2')
    with pytest.raises(ValueError, match='different document'):
        exact.resolve(
            AuthorityRef(
                kind='scene_revision',
                ref_id=foreign.revision_id,
                ref_sha256=foreign.content_hash,
            ),
            document_id=DOC,
        )
    # Unknown kinds fail closed too.
    with pytest.raises(ValueError, match='unresolvable authority ref'):
        exact.resolve(
            AuthorityRef(kind='standards_profile', ref_id='ghost'),
            document_id=DOC,
        )


def test_exact_resolver_accepts_global_goal_authority(tmp_path: Path) -> None:
    scene_repository = _scene(tmp_path)
    standards = CadStandardsRepository(scene_repository)
    criterion = CriterionDefinition(
        criterion_id='distance',
        name='distance',
        source=CriterionSource(
            publisher='p', document_title='t',
            document_version='1.0', reference='§1',
        ),
        quantity='distance', unit='m',
        applicable_domains=('room',),
        rule=CriterionRule(operator='max', maximum=1.0),
    )
    profile = build_standards_profile(
        profile_id='user-1',
        version='1.0',
        name='Published standard',
        profile_kind='user_defined',
        criteria=(criterion,),
    )
    standards.save_profile(profile)

    # Production composition: DesignBrief's resolver resolves a goal kind
    # with no per-callsite wiring.
    exact = ExactAuthorityResolver(scene_repository)
    resolved = exact.resolve(
        AuthorityRef(
            kind='standards_profile',
            ref_id='user-1',
            ref_sha256=profile.profile_semantic_hash,
        ),
        document_id=DOC,
    )
    assert resolved.document_id == DOC
    assert resolved.semantic_sha256 == profile.profile_semantic_hash


def test_kind_resolver_escape_hatch_wraps_legacy_shape(tmp_path: Path) -> None:
    from htdt.cad_authority_resolver import ResolvedAuthority

    scene_repository = _scene(tmp_path)
    exact = ExactAuthorityResolver(
        scene_repository,
        kind_resolvers={
            'acoustic_performance_target': lambda ref_id: ResolvedAuthority(
                kind='acoustic_performance_target',
                ref_id=ref_id,
                document_id=DOC,
                semantic_sha256='c' * 64,
            )
        },
    )
    resolved = exact.resolve(
        AuthorityRef(
            kind='acoustic_performance_target',
            ref_id='goal-1',
            ref_sha256='c' * 64,
        ),
        document_id=DOC,
    )
    assert resolved.semantic_sha256 == 'c' * 64
    # The escape hatch registered itself on the private registry.
    adapter = exact.registry.adapter('acoustic_performance_target')
    assert adapter is not None and adapter.scope == 'project'


def test_registry_known_kinds_match_refs_facade(tmp_path: Path) -> None:
    scene_repository = _scene(tmp_path)
    registry = build_canonical_authority_registry(scene_repository)
    refs = CanonicalAuthorityRefResolver(scene_repository)
    for kind in CanonicalAuthorityRefResolver.KNOWN_KINDS:
        assert registry.knows(kind), kind
        assert refs.knows(kind)
    # field_evidence is conditional — not in KNOWN_KINDS, and not
    # registered without its repository.
    assert 'field_evidence' not in CanonicalAuthorityRefResolver.KNOWN_KINDS
    assert not registry.knows('field_evidence')
