"""#605 reference libraries hub tests."""

from __future__ import annotations

import pytest

from htdt.reference_libraries import (
    DependencyReferenceKind,
    ImportOutcome,
    LibraryDeleteBlocked,
    LibraryEntry,
    LibraryError,
    LibraryFamily,
    LibraryMetaStore,
    LibraryScope,
    ReferenceLibraryIndex,
)


def _entry(
    identity: str = 'sp-1',
    family: LibraryFamily = LibraryFamily.EQUIPMENT,
    scope: LibraryScope = LibraryScope.USER_LIBRARY,
    display_name: str = 'Bookshelf SP',
    version: str = 'v1',
    authority_hash: str = 'hash-a',
    **over,
) -> LibraryEntry:
    return LibraryEntry(
        identity=identity,
        family=family,
        scope=scope,
        display_name=display_name,
        version=version,
        authority_hash=authority_hash,
        **over,
    )


class _FakeProvider:
    def __init__(self, family: LibraryFamily, entries) -> None:
        self.family = family
        self._entries = list(entries)

    def list_entries(self):
        return tuple(self._entries)

    def open_editor_hint(self, entry: LibraryEntry):
        return f'editor://{entry.family.value}/{entry.identity}'


def _index(entries, meta=None) -> ReferenceLibraryIndex:
    index = ReferenceLibraryIndex(meta=meta)
    by_family: dict[LibraryFamily, list[LibraryEntry]] = {}
    for entry in entries:
        by_family.setdefault(entry.family, []).append(entry)
    for family, items in by_family.items():
        index.register_provider(_FakeProvider(family, items))
    return index


def test_entries_filter_by_family_scope() -> None:
    entries = [
        _entry('sp-1'),
        _entry('sp-1', version='v2', authority_hash='hash-b'),
        _entry(
            'curve-1',
            family=LibraryFamily.TARGET_CURVE,
            display_name='B&K house curve',
        ),
    ]
    index = _index(entries)
    assert index.families() == (
        LibraryFamily.EQUIPMENT,
        LibraryFamily.TARGET_CURVE,
    )
    assert len(index.entries()) == 3
    assert {e.identity for e in index.entries(family=LibraryFamily.TARGET_CURVE)} == {'curve-1'}
    assert {e.identity for e in index.entries(scope=LibraryScope.BUILTIN)} == set()


def test_search_matches_name_description_identity() -> None:
    index = _index(
        [
            _entry('sp-1', display_name='Bookshelf SP', description='Ported two-way'),
            _entry('curve-1', family=LibraryFamily.TARGET_CURVE, display_name='Harman'),
        ]
    )
    assert [e.identity for e in index.search('bookshelf')] == ['sp-1']
    assert [e.identity for e in index.search('ported')] == ['sp-1']
    assert [e.identity for e in index.search('curve-1')] == ['curve-1']
    assert index.search('') == tuple(index.entries())


def test_versions_of_and_latest() -> None:
    index = _index(
        [
            _entry('sp-1', version='v1', authority_hash='h1', is_latest=False),
            _entry('sp-1', version='v2', authority_hash='h2', is_latest=True),
        ]
    )
    versions = index.versions_of('sp-1')
    assert [v.version for v in versions] == ['v1', 'v2']
    latest = [v for v in versions if v.is_latest]
    assert len(latest) == 1 and latest[0].authority_hash == 'h2'


def test_archive_via_meta_store(tmp_path) -> None:
    entry = _entry('sp-1')
    index = _index([entry], meta=LibraryMetaStore(tmp_path / 'meta.json'))
    index.set_archived(entry, True)
    assert index.entries() == ()
    assert index.entries(include_archived=True) == (entry,)

    # Archive state persists and is keyed to the exact semantic version.
    reloaded = _index([entry], meta=LibraryMetaStore(tmp_path / 'meta.json'))
    assert reloaded.is_archived(entry)
    newer = _entry('sp-1', version='v2', authority_hash='h2')
    assert not reloaded.is_archived(newer)


def test_delete_gate_uses_reachability() -> None:
    index = _index([_entry('sp-1')])
    entry = index.entries()[0]
    index.assert_deletable(entry, reachable_consumers=())
    with pytest.raises(LibraryDeleteBlocked):
        index.assert_deletable(entry, reachable_consumers=('project-alpha',))


def test_immutable_version_guard() -> None:
    index = _index([])
    entry = _entry('sp-1')
    index.assert_immutable_version(entry, 'hash-a')
    with pytest.raises(LibraryError):
        index.assert_immutable_version(entry, 'hash-b')


def test_import_collision_semantics() -> None:
    existing = _entry('sp-1', display_name='Bookshelf SP')
    index = _index([existing])

    # Same semantic identity → reuse.
    same = _entry('sp-1')
    assert index.resolve_import(same).outcome == ImportOutcome.REUSE

    # Same identity+version, different hash → conflict.
    changed = _entry('sp-1', authority_hash='hash-other')
    resolution = index.resolve_import(changed)
    assert resolution.outcome == ImportOutcome.CONFLICT
    assert resolution.existing == existing

    # Same display name, different authority → coexist.
    clashing = _entry('sp-2', display_name='Bookshelf SP')
    assert index.resolve_import(clashing).outcome == ImportOutcome.COEXIST

    # Fresh authority → import.
    fresh = _entry('sp-3', display_name='Tower SP')
    assert index.resolve_import(fresh).outcome == ImportOutcome.IMPORT


def test_project_references_and_used_by() -> None:
    a = _entry('sp-1')
    b = _entry('sp-2')
    index = _index([a, b])
    refs = index.project_references([a.semantic_key])
    assert refs == (a,)
    count = index.used_by_count(
        a,
        project_dependency_sets=([a.semantic_key], [b.semantic_key], [a.semantic_key]),
    )
    assert count == 2


def test_project_references_require_semantic_hash() -> None:
    """#741: identity@version alone can never resolve an exact authority."""
    a = _entry('sp-1', authority_hash='hash-a')
    b = _entry('sp-1', authority_hash='hash-b')
    index = _index([a, b])

    # Exact semantic keys resolve only their own authority.
    assert index.project_references([a.semantic_key]) == (a,)
    assert index.project_references([b.semantic_key]) == (b,)

    # A version-only key returns no exact match even when a row exists.
    assert index.project_references([a.version_key]) == ()

    # Exact used-by ignores version-only project dependency sets.
    sets = (
        [a.semantic_key],
        [a.version_key],          # legacy/version-only -> not exact usage
        [a.semantic_key, a.version_key],
    )
    assert index.used_by_count(a, project_dependency_sets=sets) == 2
    assert index.legacy_used_by_count(a, project_dependency_sets=sets) == 1
    # The conflicting hash-B row has no exact users either.
    assert index.used_by_count(b, project_dependency_sets=sets) == 0


def test_classify_project_dependencies() -> None:
    a = _entry('sp-1', authority_hash='hash-a')
    b = _entry('sp-1', authority_hash='hash-b')
    c = _entry('curve-1', family=LibraryFamily.TARGET_CURVE)
    index = _index([a, b, c])

    kinds = {
        ref.key: ref.kind
        for ref in index.classify_project_dependencies(
            [
                a.semantic_key,      # exact
                'sp-1@v1',           # two semantic rows share the version
                c.version_key,       # one semantic row -> legacy unverified
                'ghost@v9',          # nothing matches
                'ghost@v9#' + 'f' * 6,  # semantic-looking pin that resolves to nothing
            ]
        )
    }
    assert kinds[a.semantic_key] == DependencyReferenceKind.EXACT
    assert kinds['sp-1@v1'] == DependencyReferenceKind.AMBIGUOUS_VERSION_REFERENCE
    assert kinds[c.version_key] == DependencyReferenceKind.LEGACY_UNVERIFIED_REFERENCE
    assert kinds['ghost@v9'] == DependencyReferenceKind.REQUIRED_UNRESOLVED
    assert kinds['ghost@v9#' + 'f' * 6] == DependencyReferenceKind.REQUIRED_UNRESOLVED

    # Ambiguous candidates are exposed for disambiguation, not as exact refs.
    ambiguous = index.classify_project_dependencies(['sp-1@v1'])[0]
    assert {e.authority_hash for e in ambiguous.entries} == {'hash-a', 'hash-b'}


def test_archive_without_meta_store_raises() -> None:
    index = _index([_entry('sp-1')])
    with pytest.raises(LibraryError):
        index.set_archived(index.entries()[0], True)
