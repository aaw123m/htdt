"""Reference Libraries hub (#605).

HTDT accumulates reusable/versioned authority families — equipment and
directivity (#444), acoustic treatment (#451), target curves (#508),
standards profiles (#170), instruments/calibration (#471/#599), materials —
each owned by its own typed repository and editor. This module is the
**shared shell/search/version/dependency layer** over those typed
repositories, not a generic schema that flattens them.

Contracts:

* every reusable authority declares a user-visible :class:`LibraryScope`
  — never inferred from which screen created it;
* common capabilities: search/filter, version listing, used-by counts,
  archive/hide, duplicate-as-user-definition, exact import/export;
* semantic edits create a new immutable version; historical project
  bindings keep pointing at their exact old version;
* import collision semantics are exact: same identity+hash → reuse, same
  id+version different hash → conflict, same display name different
  authority → coexist — never overwrite by display name;
* archive/hide is normal cleanup; destructive deletion is gated on
  reachability evidence (#501), not an ad-hoc button.
"""

from __future__ import annotations

import json
import os
import tempfile
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


LIBRARY_SCHEMA_VERSION = 1
LIBRARY_META_FILENAME = 'reference_library_meta.json'


class LibraryScope(StrEnum):
    """User-visible ownership scope of a reusable authority."""

    BUILTIN = 'builtin'
    USER_LIBRARY = 'user_library'
    PROJECT_LOCAL = 'project_local'
    IMPORTED_DEPENDENCY = 'imported_dependency'
    HISTORICAL = 'historical'


class LibraryFamily(StrEnum):
    EQUIPMENT = 'equipment'
    TREATMENT = 'treatment'
    TARGET_CURVE = 'target_curve'
    STANDARD_PROFILE = 'standard_profile'
    INSTRUMENT = 'instrument'
    MATERIAL = 'material'
    OPERATING_PROFILE = 'operating_profile'


class LibraryEntry(BaseModel):
    """One reusable authority as presented by the hub.

    ``identity`` + ``version`` + ``authority_hash`` together are the exact
    semantic binding; ``display_name`` is presentation only and never used
    for equality, matching or overwrite decisions.
    """

    model_config = ConfigDict(frozen=True)

    identity: str = Field(min_length=1)
    family: LibraryFamily
    scope: LibraryScope
    display_name: str = Field(min_length=1)
    description: str = ''
    version: str = Field(min_length=1)
    authority_hash: str = Field(min_length=1)
    is_latest: bool = True
    capability_summary: str | None = None
    source_summary: str | None = None
    missing_dependencies: tuple[str, ...] = ()

    @property
    def semantic_key(self) -> str:
        return f'{self.identity}@{self.version}#{self.authority_hash}'

    @property
    def version_key(self) -> str:
        return f'{self.identity}@{self.version}'


class LibraryProvider(Protocol):
    """A typed domain repository exposed to the hub.

    Providers stay authoritative; the index only reads listings and
    delegates edits to the domain editor through ``open_editor_hint``.
    """

    family: LibraryFamily

    def list_entries(self) -> Iterable[LibraryEntry]: ...

    def open_editor_hint(self, entry: LibraryEntry) -> str | None: ...


class LibraryError(ValueError):
    pass


class LibraryConflictError(LibraryError):
    pass


class LibraryDeleteBlocked(LibraryError):
    pass


class ImportOutcome(StrEnum):
    REUSE = 'reuse'
    CONFLICT = 'conflict'
    COEXIST = 'coexist'
    IMPORT = 'import'


class DependencyReferenceKind(StrEnum):
    """How a persisted project dependency key resolves against the index.

    Only a full semantic key (``identity@version#hash``) is exact. A
    version-only ``identity@version`` key is never upgraded to exact — it is
    classified as legacy/ambiguous until an explicit rebind (#608) records a
    semantic pin.
    """

    EXACT = 'exact'
    LEGACY_UNVERIFIED_REFERENCE = 'legacy_unverified_reference'
    AMBIGUOUS_VERSION_REFERENCE = 'ambiguous_version_reference'
    REQUIRED_UNRESOLVED = 'required_unresolved'


@dataclass(frozen=True, slots=True)
class DependencyReference:
    """One persisted dependency key classified against the index.

    ``entries`` carries resolved candidates for disambiguation UI; for
    non-EXACT kinds they are *candidates*, not exact references.
    """

    key: str
    kind: DependencyReferenceKind
    entries: tuple[LibraryEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class ImportResolution:
    outcome: ImportOutcome
    entry: LibraryEntry
    detail: str
    existing: LibraryEntry | None = None


@dataclass(frozen=True, slots=True)
class LibraryVersion:
    """One immutable recorded version of a definition identity."""

    version: str
    authority_hash: str
    is_latest: bool


class ReferenceLibraryIndex:
    """Search/version/dependency projection over typed providers.

    The index is a rebuildable read model. Archiving metadata lives in the
    small app-local :class:`LibraryMetaStore`; semantic truth stays in the
    domain repositories.
    """

    def __init__(self, meta: 'LibraryMetaStore | None' = None) -> None:
        self._providers: dict[LibraryFamily, LibraryProvider] = {}
        self._meta = meta

    def register_provider(self, provider: LibraryProvider) -> None:
        self._providers[provider.family] = provider

    def families(self) -> tuple[LibraryFamily, ...]:
        return tuple(self._providers)

    # -- listing / search ------------------------------------------------

    def entries(
        self,
        *,
        family: LibraryFamily | None = None,
        scope: LibraryScope | None = None,
        include_archived: bool = False,
    ) -> tuple[LibraryEntry, ...]:
        providers = (
            [self._providers[family]]
            if family is not None
            else list(self._providers.values())
        )
        out: list[LibraryEntry] = []
        for provider in providers:
            for entry in provider.list_entries():
                if scope is not None and entry.scope != scope:
                    continue
                if not include_archived and self.is_archived(entry):
                    continue
                out.append(entry)
        return tuple(out)

    def search(
        self,
        query: str,
        *,
        family: LibraryFamily | None = None,
        include_archived: bool = False,
    ) -> tuple[LibraryEntry, ...]:
        # NFKC+casefold on both sides so half-width kana / full-width digits
        # in the query still match stored Japanese text (palette/command fold).
        def fold(text: str) -> str:
            return unicodedata.normalize('NFKC', text).casefold()

        needle = fold(query.strip())
        results = [
            entry
            for entry in self.entries(
                family=family, include_archived=include_archived
            )
            if not needle
            or needle in fold(entry.display_name)
            or needle in fold(entry.description)
            or needle in fold(entry.identity)
            or (entry.capability_summary and needle in fold(entry.capability_summary))
        ]
        return tuple(results)

    def versions_of(self, identity: str) -> tuple[LibraryEntry, ...]:
        versions = [
            entry for entry in self.entries(include_archived=True) if entry.identity == identity
        ]
        return tuple(sorted(versions, key=lambda e: e.version))

    # -- archive / hide (normal cleanup, distinct from deletion) ---------

    def is_archived(self, entry: LibraryEntry) -> bool:
        if self._meta is None:
            return False
        return self._meta.is_archived(entry.semantic_key)

    def set_archived(self, entry: LibraryEntry, archived: bool) -> None:
        if self._meta is None:
            raise LibraryError('no meta store configured for archive state')
        self._meta.set_archived(entry.semantic_key, archived)

    # -- destructive deletion gate (#501 reachability) --------------------

    def assert_deletable(
        self,
        entry: LibraryEntry,
        *,
        reachable_consumers: Iterable[str],
    ) -> None:
        """Destructive deletion requires proof no retained authority needs it.

        ``reachable_consumers`` is the #501 storage/GC reachability result:
        identities of retained authorities that still reference this entry.
        Any consumer blocks deletion — archive/hide is the normal path.
        """

        consumers = list(reachable_consumers)
        if consumers:
            raise LibraryDeleteBlocked(
                f'{entry.identity}@{entry.version} is still referenced by '
                f'{len(consumers)} retained authority record(s); archive instead'
            )

    # -- version semantics ------------------------------------------------

    def assert_immutable_version(
        self, existing: LibraryEntry, new_authority_hash: str
    ) -> None:
        """A semantic edit at the same identity+version must not rewrite it."""

        if existing.authority_hash != new_authority_hash:
            raise LibraryError(
                'semantic content changed under an existing version; '
                'publish a new version instead'
            )

    # -- import collision semantics ---------------------------------------

    def resolve_import(self, candidate: LibraryEntry) -> ImportResolution:
        """Classify an imported definition against the index.

        * same identity+version+hash → exact reuse;
        * same identity+version, different hash → conflict (never merge);
        * same display name, different authority → coexist (import proceeds
          under its own semantic identity);
        * otherwise → normal import.
        """

        existing_versions = self.versions_of(candidate.identity)
        for existing in existing_versions:
            if existing.version == candidate.version:
                if existing.authority_hash == candidate.authority_hash:
                    return ImportResolution(
                        outcome=ImportOutcome.REUSE,
                        entry=existing,
                        existing=existing,
                        detail='exact semantic identity already present',
                    )
                return ImportResolution(
                    outcome=ImportOutcome.CONFLICT,
                    entry=candidate,
                    existing=existing,
                    detail=(
                        f'{candidate.identity}@{candidate.version} exists with a '
                        'different semantic hash — conflict, not overwrite'
                    ),
                )
        name_clash = any(
            e.display_name == candidate.display_name
            for e in self.entries(include_archived=True)
            if e.identity != candidate.identity
        )
        return ImportResolution(
            outcome=ImportOutcome.COEXIST if name_clash else ImportOutcome.IMPORT,
            entry=candidate,
            detail=(
                'display name already used by a different authority; coexisting'
                if name_clash
                else 'new authority'
            ),
        )

    # -- project dependency view -----------------------------------------

    def project_references(
        self, semantic_keys: Iterable[str]
    ) -> tuple[LibraryEntry, ...]:
        """Exact reusable authorities a project depends on ("used by this project").

        Only full semantic keys (``identity@version#hash``) resolve. A
        version-only ``identity@version`` key is never treated as an exact
        reference — classify it via :meth:`classify_project_dependencies`.
        """

        wanted = set(semantic_keys)
        return tuple(
            entry
            for entry in self.entries(include_archived=True)
            if entry.semantic_key in wanted
        )

    def classify_project_dependencies(
        self, keys: Iterable[str]
    ) -> tuple[DependencyReference, ...]:
        """Classify persisted project dependency keys by exactness.

        * full ``identity@version#hash`` → EXACT (or REQUIRED_UNRESOLVED
          when nothing matches);
        * version-only ``identity@version`` → LEGACY_UNVERIFIED_REFERENCE
          when a single semantic candidate exists,
          AMBIGUOUS_VERSION_REFERENCE when several rows share that version,
          REQUIRED_UNRESOLVED when none do;
        * any other non-semantic key → REQUIRED_UNRESOLVED.

        Version-only keys are never silently promoted to exact, even when
        exactly one candidate row currently exists.
        """

        entries = self.entries(include_archived=True)
        by_semantic = {entry.semantic_key: entry for entry in entries}
        by_version: dict[str, list[LibraryEntry]] = {}
        for entry in entries:
            by_version.setdefault(entry.version_key, []).append(entry)
        classified: list[DependencyReference] = []
        for key in keys:
            if '#' in key:
                entry = by_semantic.get(key)
                classified.append(
                    DependencyReference(
                        key=key,
                        kind=(
                            DependencyReferenceKind.EXACT
                            if entry is not None
                            else DependencyReferenceKind.REQUIRED_UNRESOLVED
                        ),
                        entries=(entry,) if entry is not None else (),
                    )
                )
                continue
            candidates = tuple(by_version.get(key, ()))
            if not candidates:
                kind = DependencyReferenceKind.REQUIRED_UNRESOLVED
            elif len(candidates) == 1:
                kind = DependencyReferenceKind.LEGACY_UNVERIFIED_REFERENCE
            else:
                kind = DependencyReferenceKind.AMBIGUOUS_VERSION_REFERENCE
            classified.append(
                DependencyReference(key=key, kind=kind, entries=candidates)
            )
        return tuple(classified)

    def used_by_count(
        self,
        entry: LibraryEntry,
        project_dependency_sets: Iterable[Iterable[str]],
    ) -> int:
        """How many projects reference this exact semantic version."""

        key = entry.semantic_key
        return sum(1 for keys in project_dependency_sets if key in set(keys))

    def legacy_used_by_count(
        self,
        entry: LibraryEntry,
        project_dependency_sets: Iterable[Iterable[str]],
    ) -> int:
        """Projects referencing this entry only through a version-only key.

        Possible/legacy usage reported separately from exact usage: a set
        that also carries the exact semantic key is exact usage, not legacy.
        """

        semantic = entry.semantic_key
        vkey = entry.version_key
        return sum(
            1
            for keys in project_dependency_sets
            if vkey in set(keys) and semantic not in set(keys)
        )


#: Sentinel for ``LibraryMetaStore._file_signature`` when the file
#: cannot be stat'ed — treated as "changed" so a write re-reads instead of
#: overwriting an unverifiable file.
_STAT_FAILED = object()


class LibraryMetaStore:
    """App-local store for archive/hide flags.

    Keyed by semantic key (identity@version#hash) so an archived flag can
    never leak onto a different semantic version — presentation metadata
    only, never authority.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._archived: set[str] = set()
        self._persisted_signature: object = None
        self._load()

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> 'LibraryMetaStore':
        return cls(Path(data_dir) / LIBRARY_META_FILENAME)

    def _load(self) -> None:
        try:
            self._load_current()
        finally:
            self._persisted_signature = self._file_signature()

    def _file_signature(self) -> object:
        """Identity of the on-disk document last consumed, compared before
        every write so an external edit is merged in, not overwritten."""

        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        except OSError:
            return _STAT_FAILED
        return (
            stat.st_dev,
            stat.st_ino,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
            stat.st_size,
        )

    def _refresh_if_modified(self) -> None:
        """Re-read the file when another writer touched it since last load."""

        if self._file_signature() != self._persisted_signature:
            self._load()

    def _load_current(self) -> None:
        self._archived = set()
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError, RecursionError):
            return
        if not isinstance(payload, dict) or payload.get('schema_version') != LIBRARY_SCHEMA_VERSION:
            return
        archived = payload.get('archived')
        if isinstance(archived, list):
            self._archived = {str(v) for v in archived}

    def _persist(self) -> None:
        payload = {
            'schema_version': LIBRARY_SCHEMA_VERSION,
            'authority': 'htdt-reference-library-meta',
            'archived': sorted(self._archived),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=self.path.name + '.', suffix='.tmp'
        )
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(json.dumps(payload, sort_keys=True, allow_nan=False))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            self._persisted_signature = self._file_signature()
        except BaseException:  # error-boundary: cleanup before re-raise — any write failure (incl. cancel/interrupt) removes the temp file so a partial library file never replaces the good one (noqa: BLE001)
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def is_archived(self, semantic_key: str) -> bool:
        return semantic_key in self._archived

    def set_archived(self, semantic_key: str, archived: bool) -> None:
        self._refresh_if_modified()
        if archived:
            self._archived.add(semantic_key)
        else:
            self._archived.discard(semantic_key)
        self._persist()


__all__ = [
    'DependencyReference',
    'DependencyReferenceKind',
    'ImportOutcome',
    'ImportResolution',
    'LIBRARY_META_FILENAME',
    'LIBRARY_SCHEMA_VERSION',
    'LibraryConflictError',
    'LibraryDeleteBlocked',
    'LibraryEntry',
    'LibraryError',
    'LibraryFamily',
    'LibraryMetaStore',
    'LibraryProvider',
    'LibraryScope',
    'LibraryVersion',
    'ReferenceLibraryIndex',
]
