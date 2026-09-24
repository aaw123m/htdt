from pathlib import Path

import pytest

from htdt.cad_repository import SceneRepository
from htdt.external_dependency_repository import (
    ExternalDependencyConflictError,
    ExternalDependencyRepository,
)
from htdt.external_dependency_resolver import (
    DEPENDENCY_KIND_IDENTITY,
    DependencyResolutionContext,
    build_external_dependency,
    dependency_kind_identity,
    resolve_external_dependency,
)


SUPPORTED = tuple(DEPENDENCY_KIND_IDENTITY)


def _dependency(**kwargs):
    return build_external_dependency(
        document_id='doc-1',
        kind=kwargs.pop('kind', 'capture_project'),
        authority_ref=kwargs.pop('authority_ref', 'capture-proj-A'),
        expected_sha256=kwargs.pop('expected_sha256', 'a' * 64),
        created_at_utc='2026-09-24T00:00:00+00:00',
        **kwargs,
    )


def test_kind_identity_declares_identity_scheme() -> None:
    assert dependency_kind_identity('capture_project').hash_required
    assert dependency_kind_identity('equipment_catalog').version_required
    assert not dependency_kind_identity('other').hash_required

    with pytest.raises(ValueError):
        build_external_dependency(
            document_id='doc-1',
            kind='capture_project',
            authority_ref='capture-proj-A',
            created_at_utc='2026-09-24T00:00:00+00:00',
        )
    with pytest.raises(ValueError):
        build_external_dependency(
            document_id='doc-1',
            kind='equipment_catalog',
            authority_ref='catalog-1',
            expected_sha256='a' * 64,
            created_at_utc='2026-09-24T00:00:00+00:00',
        )
    legacy = build_external_dependency(
        document_id='doc-1',
        kind='other',
        authority_ref='manual-note',
        expected_sha256=None,
        created_at_utc='2026-09-24T00:00:00+00:00',
    )
    assert legacy.expected_sha256 is None


def test_resolution_prefers_local_then_embedded_then_imported() -> None:
    dependency = _dependency()
    context = DependencyResolutionContext(
        supported_kinds=SUPPORTED,
        local_index={('capture_project', 'capture-proj-A'): 'a' * 64},
        embedded_index={('capture_project', 'capture-proj-A'): 'a' * 64},
        import_index={('capture_project', 'capture-proj-A'): 'a' * 64},
    )
    event = resolve_external_dependency(
        dependency, context, resolved_at_utc='2026-09-24T01:00:00+00:00'
    )
    assert event.outcome == 'resolved_exact_local'
    assert event.resolved_via == 'local'
    assert event.resolved_sha256 == 'a' * 64

    context = DependencyResolutionContext(
        supported_kinds=SUPPORTED,
        embedded_index={('capture_project', 'capture-proj-A'): 'a' * 64},
        import_index={('capture_project', 'capture-proj-A'): 'a' * 64},
    )
    event = resolve_external_dependency(
        dependency, context, resolved_at_utc='2026-09-24T01:00:00+00:00'
    )
    assert event.outcome == 'resolved_embedded'
    assert event.resolved_via == 'embedded'

    context = DependencyResolutionContext(
        supported_kinds=SUPPORTED,
        import_index={('capture_project', 'capture-proj-A'): 'a' * 64},
    )
    event = resolve_external_dependency(
        dependency, context, resolved_at_utc='2026-09-24T01:00:00+00:00'
    )
    assert event.outcome == 'resolved_imported'
    assert event.resolved_via == 'imported'


def test_conflict_falls_back_to_later_exact_index() -> None:
    dependency = _dependency(expected_sha256='a' * 64)
    context = DependencyResolutionContext(
        supported_kinds=SUPPORTED,
        local_index={('capture_project', 'capture-proj-A'): 'b' * 64},
        embedded_index={('capture_project', 'capture-proj-A'): 'a' * 64},
    )
    event = resolve_external_dependency(
        dependency, context, resolved_at_utc='2026-09-24T01:00:00+00:00'
    )
    assert event.outcome == 'resolved_embedded'
    assert event.resolved_sha256 == 'a' * 64
    assert 'local' in event.reason


def test_exhausted_conflict_is_identity_conflict() -> None:
    dependency = _dependency(expected_sha256='a' * 64)
    context = DependencyResolutionContext(
        supported_kinds=SUPPORTED,
        local_index={('capture_project', 'capture-proj-A'): 'b' * 64},
        import_index={('capture_project', 'capture-proj-A'): 'c' * 64},
    )
    event = resolve_external_dependency(
        dependency, context, resolved_at_utc='2026-09-24T01:00:00+00:00'
    )
    assert event.outcome == 'identity_conflict'
    assert event.resolved_via is None
    assert event.resolved_sha256 is None
    assert 'local' in event.reason and 'imported' in event.reason


def test_legacy_unverified_is_explicit_not_resolved() -> None:
    dependency = _dependency(kind='other', expected_sha256=None)
    context = DependencyResolutionContext(
        supported_kinds=SUPPORTED,
        local_index={('other', 'capture-proj-A'): 'a' * 64},
    )
    event = resolve_external_dependency(
        dependency, context, resolved_at_utc='2026-09-24T01:00:00+00:00'
    )
    assert event.outcome == 'legacy_unverified'
    assert event.resolved_via is None


def test_resolution_unresolved_required_and_optional() -> None:
    context = DependencyResolutionContext(supported_kinds=SUPPORTED)
    event = resolve_external_dependency(
        _dependency(required=True),
        context,
        resolved_at_utc='2026-09-24T01:00:00+00:00',
    )
    assert event.outcome == 'required_unresolved'
    event = resolve_external_dependency(
        _dependency(required=False),
        context,
        resolved_at_utc='2026-09-24T01:00:00+00:00',
    )
    assert event.outcome == 'optional_unresolved'


def test_resolution_unsupported_kind_and_no_fuzzy() -> None:
    dependency = _dependency()
    context = DependencyResolutionContext(
        supported_kinds=('equipment_catalog',),
        local_index={('capture_project', 'capture-proj-A'): 'a' * 64},
    )
    event = resolve_external_dependency(
        dependency, context, resolved_at_utc='2026-09-24T01:00:00+00:00'
    )
    assert event.outcome == 'unsupported_dependency_kind'

    near = _dependency(authority_ref='capture-proj-A-copy')
    context = DependencyResolutionContext(
        supported_kinds=SUPPORTED,
        local_index={('capture_project', 'capture-proj-A'): 'a' * 64},
    )
    event = resolve_external_dependency(
        near, context, resolved_at_utc='2026-09-24T01:00:00+00:00'
    )
    assert event.outcome == 'required_unresolved'


def test_dependency_repository_derives_events(tmp_path: Path) -> None:
    repository = ExternalDependencyRepository(
        SceneRepository(tmp_path / 'scene.sqlite3')
    )
    dependency = _dependency()
    repository.save_dependency(dependency)
    assert repository.get_dependency(dependency.dependency_id) == dependency
    with pytest.raises(ExternalDependencyConflictError):
        repository.save_dependency(dependency)

    context = DependencyResolutionContext(
        supported_kinds=SUPPORTED,
        local_index={('capture_project', 'capture-proj-A'): 'a' * 64},
    )
    event = repository.resolve_and_record(
        dependency.dependency_id,
        context,
        resolved_at_utc='2026-09-24T01:00:00+00:00',
        event_id='event-1',
    )
    assert event.event_id == 'event-1'
    assert event.outcome == 'resolved_exact_local'
    assert event.document_id == dependency.document_id
    assert repository.latest_resolution(dependency.dependency_id) == event
    assert repository.list_resolutions(dependency.dependency_id) == (event,)

    with pytest.raises(ValueError):
        repository.resolve_and_record(
            'ghost-dependency',
            context,
            resolved_at_utc='2026-09-24T01:00:00+00:00',
        )
