"""REV64 #839 — authoritative standards source matrix.

The source matrix is a sealed, versioned record enumerating every
external standards source HTDT may cite: exact edition identity, source
status (executable / metadata_only / licensed_source_required /
unsupported / source_conflict / no_authoritative_numeric_criteria), the
#839 StandardsProfile taxonomy class, the HTDT role, definability, and
the explicit rights/access/redistribution boundary. These tests pin the
built-in matrix, every fail-closed validator, the precedence and
taxonomy coherence rules, the source-conflict retention rule, the
webpage page-identity rule, cross-authority replay, and the sealed
repository round-trip + tamper detection.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.cad_standards_gap_matrix import builtin_standards_gap_matrix
from htdt.cad_standards_profiles import builtin_standards_profiles
from htdt.cad_standards_repository import CadStandardsRepository
from htdt.cad_standards_source_matrix import (
    BUILTIN_SOURCE_MATRIX_VERSION,
    SourceConflictObservation,
    SourceMatrixEntry,
    StandardsSourceMatrix,
    build_standards_source_matrix,
    builtin_standards_source_matrix,
    validate_source_matrix,
)
from htdt.measurement_evidence_display import (
    source_entry_line,
    source_matrix_class_label,
    source_matrix_role_label,
    source_matrix_status_label,
)


NOW = '2026-10-07T00:00:00+00:00'
REVIEW_DATE = '2026-10-07'
PROFILES = builtin_standards_profiles()
GAP_MATRIX = builtin_standards_gap_matrix(PROFILES, created_at_utc=NOW)
MATRIX = builtin_standards_source_matrix(created_at_utc=NOW)


def _entry(source_id: str) -> SourceMatrixEntry:
    entry = MATRIX.entry(source_id)
    assert entry is not None, source_id
    return entry


def _fixture_entry(**overrides) -> dict:
    """A minimal, valid private-theater source-matrix entry to mutate."""

    fields = {
        'source_id': 'fixture-source',
        'name': 'Fixture Recommended Practice',
        'publisher': 'Fixture Body',
        'document_version': '1.0',
        'checked_at': REVIEW_DATE,
        'source_uris': ('https://fixture.example/rp.pdf',),
        'precedence': 'first_party_standard',
        'profile_class': 'private_theater_recommended_practice',
        'htdt_role': 'primary_private_theater_profile',
        'source_status': 'executable',
        'normative_access': 'public_download',
        'rights_class': 'public_open_standard',
        'redistribution_rights': 'restricted',
        'definability': 'definable',
        'conformance_scope': 'private_home_conformance',
        'notes': 'Fixture source with full identity.',
    }
    fields.update(overrides)
    return fields


def _entry_model(**overrides) -> SourceMatrixEntry:
    return SourceMatrixEntry(**_fixture_entry(**overrides))


def _matrix_with(*extra_entries: SourceMatrixEntry) -> StandardsSourceMatrix:
    return build_standards_source_matrix(
        entries=[*_fixture_entries(), *extra_entries],
        matrix_version='fixture-1',
        created_at_utc=NOW,
    )


def _fixture_entries() -> list[SourceMatrixEntry]:
    return [_entry_model()]


# ---------------------------------------------------------------------------
# Built-in matrix — the #839 §12 immediate matrix, exactly
# ---------------------------------------------------------------------------


def test_builtin_source_matrix_emits_the_839_rows() -> None:
    assert MATRIX.matrix_id.startswith('ssm-')
    assert MATRIX.matrix_version == BUILTIN_SOURCE_MATRIX_VERSION
    ids = [entry.source_id for entry in MATRIX.entries]
    assert ids == [
        'cedia-cta-rp22',
        'dolby-atmos-home',
        'auro-3d-home',
        'dts-x',
        'itu-r-bs775',
        'itu-r-bs2051',
        'itu-r-bs1116',
        'avixa-a102',
        'avixa-a103',
        'avixa-a104',
        'avixa-v202',
        'avixa-d402',
    ]


def test_rp22_remains_the_primary_private_home_profile() -> None:
    primaries = [
        entry
        for entry in MATRIX.entries
        if entry.htdt_role == 'primary_private_theater_profile'
    ]
    assert [entry.source_id for entry in primaries] == ['cedia-cta-rp22']
    rp22 = primaries[0]
    assert rp22.document_version == 'v1.2 (September 2023)'
    assert rp22.profile_class == 'private_theater_recommended_practice'
    assert rp22.conformance_scope == 'private_home_conformance'
    assert rp22.source_status == 'executable'
    assert rp22.definability == 'definable'
    # Access paths differ across official surfaces; redistribution rights
    # are explicit, not assumed.
    assert rp22.normative_access == 'mixed_official_surfaces'
    assert rp22.redistribution_rights == 'unknown'
    assert 'cedia-cta-rp22-v1.2' in rp22.related_standard_ids


def test_auro_rev12_is_the_default_source_for_new_mappings() -> None:
    auro = _entry('auro-3d-home')
    assert auro.document_version == 'Rev.12 (2024-05-16)'
    assert auro.source_status == 'executable'
    assert auro.profile_class == 'format_vendor_home_guidance'
    assert 'auro3d-home-layout' in auro.intended_profile_ids


def test_dolby_entry_pins_exact_edition_and_home_scope() -> None:
    dolby = _entry('dolby-atmos-home')
    assert dolby.document_version == 'R3.1 (2018-12-13)'
    assert dolby.profile_class == 'format_vendor_home_guidance'
    assert dolby.conformance_scope == 'vendor_home_guidance_only'
    assert dolby.definability == 'definable_bounded_to_source'
    # Webpage guidance is pinned by page identity, not merged into the
    # R3.1 PDF identity.
    assert dolby.page_identity is not None


def test_dtsx_has_no_authoritative_numeric_criteria() -> None:
    dtsx = _entry('dts-x')
    assert dtsx.source_status == 'no_authoritative_numeric_criteria'
    assert dtsx.definability == 'definable_for_unknown'
    assert dtsx.htdt_role == 'vendor_capability_layout_statement'
    # The flexible-layout statement is a webpage source: it must carry
    # checked-at page identity, not an invented document revision.
    assert dtsx.normative_access == 'public_webpage'
    assert dtsx.page_identity is not None


def test_itu_sources_keep_non_home_conformance_scope() -> None:
    for source_id, role in (
        ('itu-r-bs775', 'conventional_multichannel_reference'),
        ('itu-r-bs2051', 'advanced_immersive_production_reference'),
        ('itu-r-bs1116', 'critical_listening_test_reference'),
    ):
        entry = _entry(source_id)
        assert entry.conformance_scope == 'non_home_reference'
        assert entry.htdt_role == role
        assert entry.normative_access == 'public_download'
        assert entry.rights_class == 'public_open_standard'
        assert entry.redistribution_rights == 'restricted'
    assert _entry('itu-r-bs1116').profile_class == (
        'subjective_test_reference_room'
    )


def test_avixa_metadata_is_not_normative_availability() -> None:
    for source_id in ('avixa-a102', 'avixa-a104', 'avixa-v202', 'avixa-d402'):
        entry = _entry(source_id)
        assert entry.source_status == 'licensed_source_required'
        assert entry.rights_class == 'public_metadata_only'
        assert entry.redistribution_rights == 'prohibited'
        assert entry.profile_class == 'av_system_measurement_standard'
        assert entry.conformance_scope == 'non_home_reference'
        assert entry.definability == 'partially_licensed'


def test_avixa_a103_preserves_the_source_conflict() -> None:
    a103 = _entry('avixa-a103')
    assert a103.source_status == 'source_conflict'
    claims = {
        observation.claim for observation in a103.conflicting_observations
    }
    assert claims == {'A103.01:2022', 'A103.01:2023'}
    # The record keeps both official observations and does not silently
    # select one revision.
    assert a103.document_version not in claims
    assert a103.definability == 'partially_licensed'


def test_builtin_matrix_validates_against_emitted_authorities() -> None:
    assert validate_source_matrix(
        MATRIX,
        profiles=PROFILES,
        gap_standard_ids=[
            standard.standard_id for standard in GAP_MATRIX.standards
        ],
    ) == ()


def test_sealed_identity_recomputes_id_and_hash() -> None:
    rebuilt = builtin_standards_source_matrix(created_at_utc=NOW)
    assert rebuilt.matrix_id == MATRIX.matrix_id
    assert rebuilt.matrix_sha256 == MATRIX.matrix_sha256
    other = builtin_standards_source_matrix(
        created_at_utc='2026-10-08T00:00:00+00:00'
    )
    assert other.matrix_id != MATRIX.matrix_id


# ---------------------------------------------------------------------------
# Fail-closed entry validators
# ---------------------------------------------------------------------------


def test_entry_rejects_non_iso_checked_at() -> None:
    with pytest.raises(ValidationError):
        _entry_model(checked_at='yesterday')


def test_entry_requires_declared_source_uris() -> None:
    fields = _fixture_entry(source_uris=())
    with pytest.raises(ValidationError):
        SourceMatrixEntry(**{**fields, 'source_uris': ()})


def test_third_party_summary_is_never_executable() -> None:
    with pytest.raises(
        ValidationError, match='third_party_summary'
    ):
        _entry_model(
            precedence='third_party_summary',
            source_status='executable',
        )
    # …but may be retained as a non-authoritative discovery record.
    entry = _entry_model(
        precedence='third_party_summary',
        source_status='unsupported',
        normative_access='public_webpage',
        rights_class='reference_only',
        redistribution_rights='unknown',
        definability='not_definable',
        page_identity='third-party summary page, checked 2026-10-07',
        htdt_role='supplementary_project_or_research_source',
        profile_class='research_only_profile',
        conformance_scope='not_a_conformance_source',
    )
    assert entry.precedence == 'third_party_summary'


def test_executable_requires_reachable_normative_source() -> None:
    with pytest.raises(ValidationError, match='access path'):
        _entry_model(normative_access='no_identified_public_source')


def test_executable_requires_derivation_rights() -> None:
    for rights in ('public_metadata_only', 'unknown_rights'):
        with pytest.raises(ValidationError, match='rights class'):
            _entry_model(rights_class=rights)


def test_metadata_only_status_requires_metadata_rights() -> None:
    with pytest.raises(ValidationError, match='public_metadata_only'):
        _entry_model(
            source_status='metadata_only',
            definability='partially_licensed',
            rights_class='public_open_standard',
        )


def test_status_and_definability_must_be_coherent() -> None:
    with pytest.raises(ValidationError, match='definability'):
        _entry_model(
            source_status='unsupported', definability='definable'
        )
    with pytest.raises(ValidationError, match='definability'):
        _entry_model(
            source_status='no_authoritative_numeric_criteria',
            definability='definable',
        )


def test_role_and_profile_class_must_be_coherent() -> None:
    # A critical-listening role cannot wear the private-theater class.
    with pytest.raises(ValidationError, match='profile class'):
        _entry_model(
            htdt_role='critical_listening_test_reference',
            profile_class='private_theater_recommended_practice',
            source_status='executable',
            conformance_scope='private_home_conformance',
        )


def test_non_home_classes_cannot_claim_home_conformance() -> None:
    role_by_class = {
        'production_reference_layout': 'conventional_multichannel_reference',
        'subjective_test_reference_room': 'critical_listening_test_reference',
        'av_system_measurement_standard': (
            'measurement_design_commissioning_procedure'
        ),
    }
    for profile_class, role in role_by_class.items():
        with pytest.raises(ValidationError, match='conformance'):
            _entry_model(
                profile_class=profile_class,
                htdt_role=role,
                conformance_scope='private_home_conformance',
            )


def test_public_webpage_source_must_pin_page_identity() -> None:
    with pytest.raises(ValidationError, match='page'):
        _entry_model(
            normative_access='public_webpage',
            page_identity=None,
        )
    entry = _entry_model(
        normative_access='public_webpage',
        page_identity='fixture vendor page, checked 2026-10-07',
    )
    assert entry.page_identity


def test_source_conflict_requires_two_distinct_observations() -> None:
    observation = SourceConflictObservation(
        observation_id='o1',
        claim='A103.01:2022',
        surface='catalog page',
        source_uri='https://fixture.example/catalog',
        tier='standards_body_catalog',
        observed_at=REVIEW_DATE,
    )
    with pytest.raises(ValidationError, match='two'):
        _entry_model(
            source_status='source_conflict',
            definability='partially_licensed',
            conflicting_observations=(observation,),
        )
    same_claim = observation.model_copy(
        update={'observation_id': 'o2'}
    )
    with pytest.raises(ValidationError, match='distinct'):
        _entry_model(
            source_status='source_conflict',
            definability='partially_licensed',
            conflicting_observations=(observation, same_claim),
        )


def test_source_conflict_cannot_silently_pick_a_revision() -> None:
    observations = (
        SourceConflictObservation(
            observation_id='o1',
            claim='A103.01:2022',
            surface='detail page',
            source_uri='https://fixture.example/detail',
            tier='standards_body_catalog',
            observed_at=REVIEW_DATE,
        ),
        SourceConflictObservation(
            observation_id='o2',
            claim='A103.01:2023',
            surface='catalog',
            source_uri='https://fixture.example/catalog',
            tier='standards_body_catalog',
            observed_at=REVIEW_DATE,
        ),
    )
    with pytest.raises(ValidationError, match='select'):
        _entry_model(
            source_status='source_conflict',
            definability='partially_licensed',
            document_version='A103.01:2023',
            conflicting_observations=observations,
        )
    entry = _entry_model(
        source_status='source_conflict',
        definability='partially_licensed',
        document_version='A103.01 (revision under conflict)',
        conflicting_observations=observations,
    )
    assert {o.claim for o in entry.conflicting_observations} == {
        'A103.01:2022',
        'A103.01:2023',
    }


def test_conflicting_observations_only_on_source_conflict() -> None:
    observation = SourceConflictObservation(
        observation_id='o1',
        claim='X:2022',
        surface='catalog',
        source_uri='https://fixture.example/catalog',
        tier='standards_body_catalog',
        observed_at=REVIEW_DATE,
    )
    with pytest.raises(ValidationError, match='source_conflict'):
        _entry_model(conflicting_observations=(observation,))


# ---------------------------------------------------------------------------
# Matrix-level invariants
# ---------------------------------------------------------------------------


def test_matrix_rejects_duplicate_source_ids() -> None:
    entry = _entry_model()
    with pytest.raises(ValidationError, match='unique'):
        build_standards_source_matrix(
            entries=[entry, entry],
            matrix_version='dup-1',
            created_at_utc=NOW,
        )


def test_matrix_rejects_a_second_primary_private_theater_profile() -> None:
    second = _entry_model(
        source_id='fixture-second-rp',
        notes='A second claimant to the primary role.',
    )
    with pytest.raises(ValidationError, match='at most one'):
        _matrix_with(second)


def test_matrix_rejects_hash_and_id_tampering() -> None:
    payload = MATRIX.model_dump(mode='python')
    payload['matrix_sha256'] = '0' * 64
    with pytest.raises(ValidationError):
        StandardsSourceMatrix.model_validate(payload)
    payload = MATRIX.model_dump(mode='python')
    payload['matrix_id'] = 'ssm-' + '0' * 24
    with pytest.raises(ValidationError):
        StandardsSourceMatrix.model_validate(payload)


def test_validate_source_matrix_flags_dangling_references() -> None:
    entry = _entry_model(
        source_id='fixture-dangling',
        htdt_role='supplementary_project_or_research_source',
        profile_class='research_only_profile',
        conformance_scope='not_a_conformance_source',
        source_status='unsupported',
        definability='not_definable',
        intended_profile_ids=('no-such-profile',),
        related_standard_ids=('no-such-standard',),
    )
    matrix = _matrix_with(entry)
    errors = validate_source_matrix(
        matrix,
        profiles=PROFILES,
        gap_standard_ids=[
            standard.standard_id for standard in GAP_MATRIX.standards
        ],
    )
    assert any('no-such-profile' in error for error in errors)
    assert any('no-such-standard' in error for error in errors)


# ---------------------------------------------------------------------------
# Sealed repository round-trip (#839 — append-only authority, schema v94)
# ---------------------------------------------------------------------------


def test_source_matrix_repository_roundtrip_and_tamper(
    tmp_path: Path,
) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_f1_scene(), parent_revision_id=None)
    repository = CadStandardsRepository(scene_repository)

    saved = repository.save_source_matrix(MATRIX)
    assert saved == MATRIX

    reopened = CadStandardsRepository(scene_repository)
    assert reopened.get_source_matrix(MATRIX.matrix_id) == MATRIX
    assert (
        reopened.get_source_matrix_version(BUILTIN_SOURCE_MATRIX_VERSION)
        == MATRIX
    )
    assert reopened.list_source_matrices() == (MATRIX,)

    # Same record re-saved: idempotent no-op.
    assert reopened.save_source_matrix(MATRIX) == MATRIX

    # Same version, different content: sealed conflict.
    forged = build_standards_source_matrix(
        entries=MATRIX.entries,
        matrix_version=BUILTIN_SOURCE_MATRIX_VERSION,
        created_at_utc='2026-10-08T00:00:00+00:00',
    )
    with pytest.raises(ValueError, match='immutable'):
        reopened.save_source_matrix(forged)

    # Byte-level tampering with the stored payload fails closed on read.
    with sqlite3.connect(scene_repository.path) as connection:
        connection.execute(
            "UPDATE cad_standards_source_matrices SET payload_json = "
            "replace(payload_json, 'builtin-1', 'builtin-9')"
        )
        connection.commit()
    with pytest.raises(ValidationError):
        reopened.get_source_matrix(MATRIX.matrix_id)


# ---------------------------------------------------------------------------
# User-facing surface — JA labels
# ---------------------------------------------------------------------------


def test_source_matrix_ja_labels_and_lines() -> None:
    assert source_matrix_status_label('executable') == '評価可能'
    assert source_matrix_status_label('source_conflict') == (
        '出所間の不一致（版を仮定しない）'
    )
    assert source_matrix_role_label('primary_private_theater_profile') == (
        'プライベートホーム主要プロファイル'
    )
    assert source_matrix_class_label('subjective_test_reference_room') == (
        '主観評価用参照室'
    )
    assert '未知' not in source_matrix_status_label('bogus')
    line = source_entry_line(_entry('dts-x'))
    assert 'dts-x' in line
    assert '権威ある数値基準なし' in line
