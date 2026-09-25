from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from htdt.cad_data_source_registry import (
    DataSourceRegistryEntry,
    build_dataset_review,
    build_importer_declaration,
    build_raw_source_record,
    build_registry_entry,
    build_review_decision,
    build_upstream_candidate,
)
from htdt.cad_data_source_repository import (
    CadDataSourceRepository,
    DataSourceRegistryError,
)
from htdt.cad_repository import SceneRepository


def _setup(tmp_path: Path) -> CadDataSourceRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    return CadDataSourceRepository(scene_repository)


def _source(**overrides) -> DataSourceRegistryEntry:
    kwargs: dict = {
        'name': 'ASEL loudspeaker polar dataset',
        'domain': 'speaker',
        'url': 'https://example.org/asel/polars',
        'license_reference': 'CC-BY-4.0',
        'redistribution': 'allowed',
        'attribution_required': 'ASEL consortium',
        'update_mechanism': 'upstream_version_watch',
        'disposition': 'download_on_demand',
        'upstream_version': '1.2',
        'created_at_utc': '2026-01-01T00:00:00+00:00',
    }
    kwargs.update(overrides)
    return build_registry_entry(**kwargs)


def _source_registered(repository: CadDataSourceRepository):
    source = _source()
    repository.save_source(source)
    return source


def test_registry_entry_round_trip(tmp_path: Path) -> None:
    repository = _setup(tmp_path)
    source = _source()
    assert len(source.source_sha256) == 64
    repository.save_source(source)
    assert repository.get_source(source.source_id) == source
    assert repository.find_source_by_sha256(source.source_sha256) == source
    assert repository.list_sources() == (source,)
    assert repository.list_sources('material') == ()
    assert repository.list_sources('speaker') == (source,)


def test_registry_entry_append_only(tmp_path: Path) -> None:
    repository = _setup(tmp_path)
    source = _source()
    repository.save_source(source)
    with pytest.raises(DataSourceRegistryError):
        repository.save_source(source)


def test_raw_record_requires_registered_source(tmp_path: Path) -> None:
    repository = _setup(tmp_path)
    record = build_raw_source_record(
        source_id='unregistered',
        original_filename='polars.zip',
        file_sha256=sha256(b'raw').hexdigest(),
        retrieved_at_utc='2026-01-02T00:00:00+00:00',
        created_at_utc='2026-01-02T00:00:00+00:00',
    )
    with pytest.raises(DataSourceRegistryError):
        repository.save_raw_record(record)


def test_raw_record_round_trip(tmp_path: Path) -> None:
    repository = _setup(tmp_path)
    source = _source_registered(repository)
    record = build_raw_source_record(
        source_id=source.source_id,
        original_filename='polars.zip',
        file_sha256=sha256(b'raw').hexdigest(),
        retrieved_at_utc='2026-01-02T00:00:00+00:00',
        source_version='1.2',
        license_snapshot_reference='snap://licenses/asel-1.2',
        created_at_utc='2026-01-02T00:00:00+00:00',
    )
    repository.save_raw_record(record)
    assert repository.get_raw_record(record.record_id) == record
    assert repository.list_raw_records(source.source_id) == (record,)


def test_user_import_only_records_metadata_not_payload(tmp_path: Path) -> None:
    repository = _setup(tmp_path)
    source = _source_registered(repository)
    record = build_raw_source_record(
        source_id=source.source_id,
        original_filename='proprietary.spk',
        file_sha256=sha256(b'userfile').hexdigest(),
        raw_preserved=False,
        retrieved_at_utc='2026-01-02T00:00:00+00:00',
        created_at_utc='2026-01-02T00:00:00+00:00',
    )
    repository.save_raw_record(record)
    assert repository.get_raw_record(record.record_id).raw_preserved is False


def test_importer_declaration_round_trip(tmp_path: Path) -> None:
    repository = _setup(tmp_path)
    importer = build_importer_declaration(
        supported_format='CLF',
        format_versions=('1.0', '1.1'),
        domain='speaker',
        quantity_semantics=('spl_magnitude_db', 'phase_deg'),
        parser_version='clf-reader-1',
        coordinate_convention='spherical-iso12041',
        unit_convention='si',
        created_at_utc='2026-01-03T00:00:00+00:00',
    )
    repository.save_importer(importer)
    assert repository.get_importer(importer.importer_id) == importer
    assert repository.list_importers('speaker') == (importer,)
    assert repository.list_importers('material') == ()


def test_importer_format_versions_unique() -> None:
    with pytest.raises(ValueError, match='format_versions'):
        build_importer_declaration(
            supported_format='CLF',
            format_versions=('1.0', '1.0'),
            domain='speaker',
            quantity_semantics=('spl',),
            parser_version='p-1',
            created_at_utc='2026-01-03T00:00:00+00:00',
        )


def test_review_decision_status_chain(tmp_path: Path) -> None:
    repository = _setup(tmp_path)
    source = _source_registered(repository)
    assert repository.current_review_status(source.source_id) == 'candidate'

    first = build_review_decision(
        source_id=source.source_id,
        from_status='candidate',
        to_status='legally_usable',
        reviewer='ops',
        rationale='license terms verified against snapshot',
        reviewed_at_utc='2026-01-04T00:00:00+00:00',
    )
    repository.save_decision(first)
    assert repository.current_review_status(source.source_id) == 'legally_usable'

    second = build_review_decision(
        source_id=source.source_id,
        from_status='legally_usable',
        to_status='superseded',
        reviewer='ops',
        rationale='upstream released v2.0; v1.2 superseded',
        reviewed_at_utc='2026-01-05T00:00:00+00:00',
    )
    repository.save_decision(second)
    assert repository.current_review_status(source.source_id) == 'superseded'
    assert repository.list_decisions(source.source_id) == (first, second)


def test_review_decision_must_chain_from_current(tmp_path: Path) -> None:
    repository = _setup(tmp_path)
    source = _source_registered(repository)
    decision = build_review_decision(
        source_id=source.source_id,
        from_status='rejected',
        to_status='legally_usable',
        reviewer='ops',
        rationale='wrong starting status',
        reviewed_at_utc='2026-01-04T00:00:00+00:00',
    )
    with pytest.raises(DataSourceRegistryError, match='current'):
        repository.save_decision(decision)


def test_review_decision_requires_transition() -> None:
    with pytest.raises(ValueError, match='change status'):
        build_review_decision(
            source_id='s1',
            from_status='candidate',
            to_status='candidate',
            reviewer='ops',
            rationale='no-op is not a decision',
            reviewed_at_utc='2026-01-04T00:00:00+00:00',
        )


def test_dataset_review_round_trip(tmp_path: Path) -> None:
    repository = _setup(tmp_path)
    source = _source_registered(repository)
    record = build_raw_source_record(
        source_id=source.source_id,
        original_filename='polars.zip',
        file_sha256=sha256(b'raw').hexdigest(),
        retrieved_at_utc='2026-01-02T00:00:00+00:00',
        created_at_utc='2026-01-02T00:00:00+00:00',
    )
    repository.save_raw_record(record)
    review = build_dataset_review(
        source_id=source.source_id,
        record_id=record.record_id,
        license_status='allowed',
        semantic_mapping_json='{"spl_magnitude_db":"magnitude_fr"}',
        reviewer='ops',
        decision='approved',
        transformations=('resample to 1/12 octave',),
        known_limitations=('no phase above 8 kHz',),
        reviewed_at_utc='2026-01-05T00:00:00+00:00',
    )
    repository.save_dataset_review(review)
    assert repository.get_dataset_review(review.review_id) == review
    assert repository.list_dataset_reviews(source.source_id) == (review,)


def test_dataset_review_rejects_bad_mapping_json() -> None:
    with pytest.raises(ValueError, match='semantic_mapping_json'):
        build_dataset_review(
            source_id='s1',
            license_status='allowed',
            semantic_mapping_json='not-json{',
            reviewer='ops',
            decision='pending',
            reviewed_at_utc='2026-01-05T00:00:00+00:00',
        )


def test_upstream_candidate_round_trip(tmp_path: Path) -> None:
    repository = _setup(tmp_path)
    source = _source_registered(repository)
    candidate = build_upstream_candidate(
        source_id=source.source_id,
        upstream_version='2.0',
        upstream_date='2026-01-06',
        current_library_version='1.2',
        candidate_content_sha256=sha256(b'v2-content').hexdigest(),
        diff_summary='12 new polar files, phase semantics unchanged',
        created_at_utc='2026-01-07T00:00:00+00:00',
    )
    repository.save_upstream_candidate(candidate)
    assert repository.get_upstream_candidate(candidate.candidate_id) == candidate
    assert repository.list_upstream_candidates(source.source_id) == (candidate,)
    with pytest.raises(DataSourceRegistryError):
        repository.save_upstream_candidate(candidate)


def test_registry_row_hash_integrity() -> None:
    source = _source()
    tampered = source.model_dump()
    tampered['redistribution'] = 'forbidden'
    with pytest.raises(ValueError, match='hash mismatch'):
        DataSourceRegistryEntry(**tampered)
