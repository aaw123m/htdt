from pathlib import Path

import pytest

from htdt.cad_repository import SceneRepository
from htdt.external_dependency_repository import (
    ExternalDependencyConflictError,
    ExternalDependencyRepository,
)
from htdt.external_dependency_resolver import (
    DependencyResolutionContext,
    build_external_dependency,
    resolve_external_dependency,
)


SUPPORTED = (
    'capture_project',
    'capture_authority',
    'equipment_catalog',
    'material_library',
    'target_curve',
    'other',
)


def _dependency(**kwargs):
    return build_external_dependency(
        document_id='doc-1',
        kind=kwargs.pop('kind', 'capture_project'),
        authority_ref=kwargs.pop('authority_ref', 'capture-proj-A'),
        created_at_utc='2026-09-24T00:00:00+00:00',
        **kwargs,
    )


def test_resolution_prefers_local_then_embedded_then_imported() -> None:
    dependency = _dependency()
    context = DependencyResolutionContext(
        supported_kinds=SUPPORTED,
        local_index={('capture_project', 'capture-proj-A'): 'a' * 64},
        embedded_index={('capture_project', 'capture-proj-A'): 'b' * 64},
        import_index={('capture_project', 'capture-proj-A'): 'c' * 64},
    )
    event = resolve_external_dependency(
        dependency, context, resolved_at_utc='2026-09-24T01:00:00+00:00'
    )
    assert event.outcome == 'resolved_exact_local'
    assert event.resolved_via == 'local'
    assert event.resolved_sha256 == 'a' * 64

    context = DependencyResolutionContext(
        supported_kinds=SUPPORTED,
        embedded_index={('capture_project', 'capture-proj-A'): 'b' * 64},
        import_index={('capture_project', 'capture-proj-A'): 'c' * 64},
    )
    event = resolve_external_dependency(
        dependency, context, resolved_at_utc='2026-09-24T01:00:00+00:00'
    )
    assert event.outcome == 'resolved_embedded'
    assert event.resolved_via == 'embedded'

    context = DependencyResolutionContext(
        supported_kinds=SUPPORTED,
        import_index={('capture_project', 'capture-proj-A'): 'c' * 64},
    )
    event = resolve_external_dependency(
        dependency, context, resolved_at_utc='2026-09-24T01:00:00+00:00'
    )
    assert event.outcome == 'resolved_imported'
    assert event.resolved_via == 'imported'


def test_resolution_pin_conflict_is_identity_conflict() -> None:
    dependency = _dependency(expected_sha256='a' * 64)
    context = DependencyResolutionContext(
        supported_kinds=SUPPORTED,
        local_index={('capture_project', 'capture-proj-A'): 'b' * 64},
    )
    event = resolve_external_dependency(
        dependency, context, resolved_at_utc='2026-09-24T01:00:00+00:00'
    )
    assert event.outcome == 'identity_conflict'
    assert event.resolved_via is None
    assert event.resolved_sha256 == 'b' * 64

    matching = _dependency(expected_sha256='a' * 64)
    context = DependencyResolutionContext(
        supported_kinds=SUPPORTED,
        local_index={('capture_project', 'capture-proj-A'): 'a' * 64},
    )
    event = resolve_external_dependency(
        matching, context, resolved_at_utc='2026-09-24T01:00:00+00:00'
    )
    assert event.outcome == 'resolved_exact_local'


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


def test_dependency_repository_roundtrip_and_events(tmp_path: Path) -> None:
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
    event = resolve_external_dependency(
        dependency, context, resolved_at_utc='2026-09-24T01:00:00+00:00'
    )
    repository.record_resolution(event)
    assert repository.latest_resolution(dependency.dependency_id) == event
    assert repository.list_resolutions(dependency.dependency_id) == (event,)

    with pytest.raises(ValueError):
        repository.record_resolution(
            event.model_copy(update={'dependency_id': 'ghost'})
        )
