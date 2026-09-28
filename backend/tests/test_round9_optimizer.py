"""Round 9 optimizer-journey regressions.

- Paged candidate generation no longer rescans the raw Cartesian product per
  page: one enumeration keyed on the immutable ``search_spec_sha256`` serves
  every page call (UI paging, ``iter_cad_candidate_pages``, extended lanes).
- ``variant_for_sha256`` resolves one persisted SystemVariant without the
  per-row authority replay of ``list_variants``.
"""

from __future__ import annotations

import sqlite3

import pytest

import htdt.cad_search as cad_search
from htdt.cad_repository import SceneRepository
from htdt.cad_search import (
    generate_cad_candidates,
    iter_cad_candidate_pages,
)
from htdt.cad_search_models import CadSearchAxis
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    ProposedEntitySpec,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.search_space import SearchGenerationCancelled

from test_cad_search import (  # noqa: E402  (shared fixtures)
    DOCUMENT_ID,
    _constraints,
    _scene,
)


def _paged_fixture(tmp_path):
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(_scene(), parent_revision_id=None).revision
    spec, _estimate = cad_search.build_cad_search_spec(
        revision,
        _constraints(),
        (
            CadSearchAxis(
                entity_id='speaker-fl', axis='x',
                min_m=1.0, max_m=3.0, step_m=0.5,
            ),
            CadSearchAxis(
                entity_id='speaker-fl', axis='y',
                min_m=0.6, max_m=1.4, step_m=0.4,
            ),
        ),
        candidate_limit=100,
    )
    return repository, revision, spec


def test_paged_generation_enumerates_once(tmp_path, monkeypatch) -> None:
    repository, _revision, spec = _paged_fixture(tmp_path)

    calls = {'count': 0}
    real = cad_search.generate_search_space

    def counting(*args, **kwargs):
        calls['count'] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(cad_search, 'generate_search_space', counting)

    first = generate_cad_candidates(repository, spec, offset=0, limit=2)
    second = generate_cad_candidates(repository, spec, offset=2, limit=2)
    third = generate_cad_candidates(repository, spec, offset=0, limit=2)

    assert calls['count'] == 1
    assert first.candidate_set_sha256 == second.candidate_set_sha256
    assert [c.candidate_id for c in first.candidates] == [
        c.candidate_id for c in third.candidates
    ]
    assert {
        c.candidate_id for c in first.candidates
    }.isdisjoint(c.candidate_id for c in second.candidates)
    assert len(first.candidates) == 2
    assert len(second.candidates) == 2
    assert first.feasible_candidate_count >= 4


def test_iter_candidate_pages_single_enumeration(tmp_path, monkeypatch) -> None:
    repository, _revision, spec = _paged_fixture(tmp_path)

    calls = {'count': 0}
    real = cad_search.generate_search_space

    def counting(*args, **kwargs):
        calls['count'] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(cad_search, 'generate_search_space', counting)

    pages = list(iter_cad_candidate_pages(repository, spec))
    assert calls['count'] == 1
    assert len(pages) >= 1
    total = sum(len(page.candidates) for page in pages)
    assert total == pages[0].feasible_candidate_count
    ids = [
        candidate.candidate_id
        for page in pages
        for candidate in page.candidates
    ]
    assert len(ids) == len(set(ids))


def test_cached_enumeration_still_honors_cancellation(tmp_path) -> None:
    repository, _revision, spec = _paged_fixture(tmp_path)

    generate_cad_candidates(repository, spec, offset=0, limit=2)
    with pytest.raises(SearchGenerationCancelled):
        generate_cad_candidates(
            repository, spec, offset=2, limit=2, cancelled=lambda: True
        )


def test_enumeration_cache_keys_on_spec_identity(tmp_path, monkeypatch) -> None:
    repository, revision, spec = _paged_fixture(tmp_path)
    other_spec, _estimate = cad_search.build_cad_search_spec(
        revision,
        _constraints(),
        (
            CadSearchAxis(
                entity_id='speaker-fl', axis='x',
                min_m=1.0, max_m=2.0, step_m=1.0,
            ),
        ),
        candidate_limit=10,
        name='narrower',
    )

    calls = {'count': 0}
    real = cad_search.generate_search_space

    def counting(*args, **kwargs):
        calls['count'] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(cad_search, 'generate_search_space', counting)

    generate_cad_candidates(repository, spec, offset=0, limit=2)
    generate_cad_candidates(repository, other_spec, offset=0, limit=2)
    assert calls['count'] == 2


def _moved_speaker_variant(revision):
    moved = _scene(speaker_x=1.5).entities[0]
    return build_system_variant(
        baseline=revision,
        name='moved FL',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='FL'),
        ),
        proposed_entities=(
            ProposedEntitySpec(
                spec_id='prop-1', entity=moved, role_binding_id='FL'
            ),
        ),
        created_at_utc='2026-01-01T00:00:00+00:00',
    )


def test_variant_for_sha256_single_row_lookup(tmp_path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(_scene(), parent_revision_id=None).revision
    variants = CadSystemVariantRepository(repository)

    variant = _moved_speaker_variant(revision)
    variants.save_variant(variant)

    found = variants.variant_for_sha256(
        DOCUMENT_ID, variant.variant_sha256
    )
    assert found == variant
    assert variants.variant_for_sha256(DOCUMENT_ID, '0' * 64) is None
    assert variants.variant_for_sha256('other-doc', variant.variant_sha256) is None


def test_variant_for_sha256_fails_closed_on_tampered_row(tmp_path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(_scene(), parent_revision_id=None).revision
    variants = CadSystemVariantRepository(repository)

    variant = _moved_speaker_variant(revision)
    variants.save_variant(variant)

    with sqlite3.connect(tmp_path / 'cad.sqlite3') as connection:
        connection.execute(
            'UPDATE cad_system_variants SET payload_json=? WHERE variant_id=?',
            ('{"variant_id": "forged"}', variant.variant_id),
        )
    with pytest.raises(ValueError):
        variants.variant_for_sha256(DOCUMENT_ID, variant.variant_sha256)
