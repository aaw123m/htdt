"""Persistence tests for the profile-authority repositories (#817/#818).

Covers the canonical bass-management and video-presentation profile stores:
append-only (document_id, profile_id, version) identity, row-vs-payload
integrity checks, explicit current-selection records, and exact triple
resolution after reopen.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from htdt.cad_bass_management import (
    CrossoverSpec,
    LFEPathRule,
    MainChannelBassRule,
    build_bass_management_profile,
)
from htdt.cad_bass_management_repository import (
    BassManagementConflictError,
    BassManagementIntegrityError,
    CadBassManagementRepository,
)
from htdt.cad_equipment import EquipmentDataProvenance
from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_ht_video_profile import HomeTheaterVideoDesignProfile
from htdt.cad_presentation_profile_repository import (
    CadPresentationProfileRepository,
    PresentationProfileIntegrityError,
)
from htdt.cad_repository import SceneRepository


def _provenance() -> tuple[EquipmentDataProvenance, ...]:
    return (
        EquipmentDataProvenance(
            evidence_kind='manufacturer',
            source_name='Example AVR Co.',
            source_version='1',
            source_reference='published defaults',
            source_sha256='a' * 64,
        ),
    )


def _bass_profile(version: str, lifecycle: str = 'proposed'):
    return build_bass_management_profile(
        profile_id='bass-main',
        version=version,
        lifecycle=lifecycle,
        processor_ref='avr-1',
        main_rules=(
            MainChannelBassRule(
                logical_role_id='front_left',
                handling='high_pass',
                high_pass=CrossoverSpec(frequency_hz=80.0),
                redirected_destinations=('sub_a',),
            ),
        ),
        lfe_path=LFEPathRule(
            lfe_input_id='lfe_1',
            low_pass=CrossoverSpec(frequency_hz=120.0),
            destinations=('sub_a',),
        ),
        provenance=_provenance(),
    )


def _video_profile(
    document_id: str = 'doc-1',
    edition: str = 'B',
    requirement_map: dict | None = None,
) -> HomeTheaterVideoDesignProfile:
    return HomeTheaterVideoDesignProfile.create({
        'document_id': document_id,
        'standard_ref': AuthorityRef(
            kind='doc', ref_id='ceb23', ref_sha256='b' * 64,
        ),
        'edition': edition,
        'requirement_map': requirement_map
        or {'room_lighting': 'viewing_environment'},
    })


@pytest.fixture()
def bass_repo(tmp_path: Path):
    return CadBassManagementRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )


@pytest.fixture()
def pres_repo(tmp_path: Path):
    return CadPresentationProfileRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )


# ----------------------------------------------------------------------
# Bass management profiles


def test_bass_profile_save_get_reopen(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadBassManagementRepository(SceneRepository(path))
    profile = _bass_profile('1')
    repo.save_profile(profile, document_id='doc-1')

    fetched = repo.get_profile('doc-1', 'bass-main', '1')
    assert fetched == profile
    assert repo.get_profile_by_hash(profile.profile_sha256) == profile

    reopened = CadBassManagementRepository(SceneRepository(path))
    assert reopened.get_profile('doc-1', 'bass-main', '1') == profile
    assert reopened.list_profiles('doc-1') == (profile,)
    assert reopened.list_profiles('doc-2') == ()


def test_bass_profile_append_only_identity(bass_repo) -> None:
    profile = _bass_profile('1')
    bass_repo.save_profile(profile, document_id='doc-1')
    bass_repo.save_profile(profile, document_id='doc-1')  # identical: no-op
    assert len(bass_repo.list_profiles('doc-1')) == 1

    divergent = build_bass_management_profile(
        profile_id='bass-main',
        version='1',
        lifecycle='applied',
        main_rules=(),
        provenance=_provenance(),
    )
    assert divergent.profile_sha256 != profile.profile_sha256
    with pytest.raises(BassManagementConflictError):
        bass_repo.save_profile(divergent, document_id='doc-1')


def test_bass_profile_row_vs_payload_integrity(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadBassManagementRepository(SceneRepository(path))
    profile = _bass_profile('1')
    repo.save_profile(profile, document_id='doc-1')

    with sqlite3.connect(path) as connection:
        connection.execute(
            'UPDATE cad_bass_management_profiles SET lifecycle=? '
            'WHERE document_id=? AND profile_id=? AND version=?',
            ('applied', 'doc-1', 'bass-main', '1'),
        )
    with pytest.raises(BassManagementIntegrityError):
        repo.get_profile('doc-1', 'bass-main', '1')


def test_bass_selection_requires_persisted_profile(bass_repo) -> None:
    profile = _bass_profile('1')
    with pytest.raises(BassManagementIntegrityError):
        bass_repo.select_profile('doc-1', profile)


def test_bass_current_selection_is_explicit_and_historical(
    bass_repo,
) -> None:
    v1 = _bass_profile('1')
    v2 = _bass_profile('2', lifecycle='applied')
    bass_repo.save_profile(v1, document_id='doc-1')
    bass_repo.save_profile(v2, document_id='doc-1')

    assert bass_repo.current_selection('doc-1') is None
    assert bass_repo.current_profile('doc-1') is None

    sel1 = bass_repo.select_profile('doc-1', v1)
    assert sel1.profile_sha256 == v1.profile_sha256
    assert bass_repo.current_selection('doc-1') == sel1
    assert bass_repo.current_profile('doc-1') == v1

    sel2 = bass_repo.select_profile('doc-1', v2)
    assert bass_repo.current_selection('doc-1') == sel2
    assert bass_repo.current_profile('doc-1') == v2
    assert bass_repo.list_selections('doc-1') == (sel1, sel2)


def test_bass_selections_scoped_per_document(bass_repo) -> None:
    profile = _bass_profile('1')
    bass_repo.save_profile(profile, document_id='doc-1')
    bass_repo.save_profile(profile, document_id='doc-2')
    bass_repo.select_profile('doc-1', profile)

    assert bass_repo.current_selection('doc-2') is None
    assert len(bass_repo.list_selections('doc-1')) == 1


def test_bass_selection_row_vs_payload_integrity(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadBassManagementRepository(SceneRepository(path))
    profile = _bass_profile('1')
    repo.save_profile(profile, document_id='doc-1')
    repo.select_profile('doc-1', profile)

    with sqlite3.connect(path) as connection:
        connection.execute(
            'UPDATE cad_bass_management_selections SET selected_at_utc=? '
            'WHERE document_id=?',
            ('1999-01-01T00:00:00+00:00', 'doc-1'),
        )
    with pytest.raises(BassManagementIntegrityError):
        repo.current_selection('doc-1')


# ----------------------------------------------------------------------
# Video presentation profiles (CEB23 design profiles)


def test_video_profile_save_get_reopen(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadPresentationProfileRepository(SceneRepository(path))
    profile = _video_profile()
    repo.save_video_profile(profile)

    assert repo.get_video_profile(profile.profile_id) == profile

    reopened = CadPresentationProfileRepository(SceneRepository(path))
    assert reopened.get_video_profile(profile.profile_id) == profile
    assert reopened.video_profiles.list('doc-1') == (profile,)


def test_video_profile_append_only_identity(pres_repo) -> None:
    profile = _video_profile()
    pres_repo.save_video_profile(profile)
    pres_repo.save_video_profile(profile)  # idempotent re-save
    assert len(pres_repo.video_profiles.list('doc-1')) == 1

    # A tampered copy fails the seal check before the conflict check.
    divergent = profile.model_copy(update={'edition': 'A'})
    with pytest.raises(PresentationProfileIntegrityError):
        pres_repo.save_video_profile(divergent)


def test_video_profiles_scoped_per_document(pres_repo) -> None:
    one = _video_profile(document_id='doc-1')
    two = _video_profile(document_id='doc-2')
    pres_repo.save_video_profile(one)
    pres_repo.save_video_profile(two)

    assert pres_repo.video_profiles.list('doc-1') == (one,)
    assert pres_repo.video_profiles.list('doc-2') == (two,)


def test_video_profile_row_vs_payload_integrity(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadPresentationProfileRepository(SceneRepository(path))
    profile = _video_profile()
    repo.save_video_profile(profile)

    with sqlite3.connect(path) as connection:
        connection.execute(
            'UPDATE cad_ht_video_design_profiles SET profile_sha256=? '
            'WHERE document_id=?',
            ('f' * 64, 'doc-1'),
        )
    with pytest.raises(PresentationProfileIntegrityError):
        repo.get_video_profile(profile.profile_id)


def test_video_profile_bound_column_integrity(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadPresentationProfileRepository(SceneRepository(path))
    profile = _video_profile()
    repo.save_video_profile(profile)

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE cad_ht_video_design_profiles SET edition='X' "
            'WHERE document_id=?',
            ('doc-1',),
        )
    with pytest.raises(PresentationProfileIntegrityError):
        repo.get_video_profile(profile.profile_id)
