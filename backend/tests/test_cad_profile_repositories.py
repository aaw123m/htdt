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
from htdt.cad_presentation_profile import (
    MaskingState,
    build_video_presentation_profile,
)
from htdt.cad_presentation_profile_repository import (
    CadPresentationProfileRepository,
    PresentationProfileConflictError,
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


def _presentation_profile(version: str):
    return build_video_presentation_profile(
        profile_id='pres-cih',
        version=version,
        sizing='constant_image_height',
        target_aspect_ratio=2.4,
        masking=MaskingState(kind='top_bottom'),
        label='CIH 2.40 masked',
        provenance=_provenance(),
    )


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
# Video presentation profiles


def test_presentation_profile_save_get_reopen(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadPresentationProfileRepository(SceneRepository(path))
    profile = _presentation_profile('1')
    repo.save_profile(profile, document_id='doc-1')

    assert repo.get_profile('doc-1', 'pres-cih', '1') == profile
    assert repo.get_profile_by_hash(profile.profile_sha256) == profile

    reopened = CadPresentationProfileRepository(SceneRepository(path))
    assert reopened.get_profile('doc-1', 'pres-cih', '1') == profile
    assert reopened.list_profiles('doc-1') == (profile,)


def test_presentation_profile_append_only_identity(pres_repo) -> None:
    profile = _presentation_profile('1')
    pres_repo.save_profile(profile, document_id='doc-1')
    pres_repo.save_profile(profile, document_id='doc-1')
    assert len(pres_repo.list_profiles('doc-1')) == 1

    divergent = build_video_presentation_profile(
        profile_id='pres-cih',
        version='1',
        sizing='constant_image_width',
        target_aspect_ratio=1.78,
        label='CIW',
    )
    with pytest.raises(PresentationProfileConflictError):
        pres_repo.save_profile(divergent, document_id='doc-1')


def test_presentation_multiple_profiles_one_screen(pres_repo) -> None:
    cih = _presentation_profile('1')
    scope = build_video_presentation_profile(
        profile_id='pres-cih',
        version='2',
        sizing='explicit',
        active_width_m=2.6,
        active_height_m=1.1,
        label='scope window',
    )
    pres_repo.save_profile(cih, document_id='doc-1')
    pres_repo.save_profile(scope, document_id='doc-1')

    sel1 = pres_repo.select_profile('doc-1', 'screen-1', cih)
    sel2 = pres_repo.select_profile('doc-1', 'screen-1', scope)

    assert pres_repo.current_selection('doc-1') == sel2
    assert pres_repo.current_profile('doc-1') == scope
    # Switching selection never rewrites history: both selections persist.
    assert pres_repo.list_selections('doc-1') == (sel1, sel2)
    assert sel2.screen_entity_id == 'screen-1'


def test_presentation_selection_requires_persisted_profile(pres_repo) -> None:
    profile = _presentation_profile('1')
    with pytest.raises(PresentationProfileIntegrityError):
        pres_repo.select_profile('doc-1', 'screen-1', profile)


def test_presentation_profile_row_vs_payload_integrity(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadPresentationProfileRepository(SceneRepository(path))
    profile = _presentation_profile('1')
    repo.save_profile(profile, document_id='doc-1')

    with sqlite3.connect(path) as connection:
        connection.execute(
            'UPDATE cad_video_presentation_profiles SET profile_sha256=? '
            'WHERE document_id=? AND profile_id=?',
            ('f' * 64, 'doc-1', 'pres-cih'),
        )
    with pytest.raises(PresentationProfileIntegrityError):
        repo.get_profile('doc-1', 'pres-cih', '1')


def test_presentation_selection_row_vs_payload_integrity(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadPresentationProfileRepository(SceneRepository(path))
    profile = _presentation_profile('1')
    repo.save_profile(profile, document_id='doc-1')
    repo.select_profile('doc-1', 'screen-1', profile)

    with sqlite3.connect(path) as connection:
        connection.execute(
            'UPDATE cad_video_presentation_selections SET screen_entity_id=? '
            'WHERE document_id=?',
            ('screen-9', 'doc-1'),
        )
    with pytest.raises(PresentationProfileIntegrityError):
        repo.current_selection('doc-1')
