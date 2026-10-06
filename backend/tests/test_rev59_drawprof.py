"""Regression tests for REV59-DRAWPROF (#741/#742/#733)."""

from __future__ import annotations

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_ht_video_profile import (
    CEB23Evaluation,
    HomeTheaterVideoDesignProfile,
    evaluate_ceb23_requirement,
)
from htdt.cad_drawing_symbols import (
    ArchitecturalDrawingSymbolProfile,
    DeviceSymbolMapping,
    DrawingExportRecord,
    evaluate_symbol_claim,
)
from htdt.cad_timed_text import (
    CaptionRenderObservation,
    TimedTextPresentationProfile,
    evaluate_subtitle_claim,
)


def _ref(rid: str = 'x-1') -> AuthorityRef:
    return AuthorityRef(kind='doc', ref_id=rid, ref_sha256='b' * 64)


class TestCEB23Profile:
    def _profile(self, **kw) -> HomeTheaterVideoDesignProfile:
        base = {
            'document_id': 'doc-1',
            'standard_ref': _ref('ceb23b-doc'),
            'edition': 'B',
            'requirement_map': {
                'screen_size_vs_seating': 'viewing_envelope',
                'room_lighting': 'viewing_environment',
            },
        }
        base.update(kw)
        return HomeTheaterVideoDesignProfile.create(base)

    def test_profile_creation(self) -> None:
        p = self._profile()
        assert p.profile_id.startswith('htvdp-')

    def test_unknown_requirement_key_rejected(self) -> None:
        with pytest.raises(ValueError):
            self._profile(requirement_map={'bogus_req': 'x'})

    def test_standard_ref_required(self) -> None:
        with pytest.raises(ValueError):
            self._profile(standard_ref=None)

    def test_unmapped_requirement(self) -> None:
        v, _ = evaluate_ceb23_requirement(
            self._profile(), None, 'room_surface_color')
        assert v == 'unmapped_requirement'

    def test_outside_profile(self) -> None:
        v, _ = evaluate_ceb23_requirement(
            self._profile(), None, 'not_a_key')
        assert v == 'profile_unsupported'

    def test_design_vs_asbuilt(self) -> None:
        ev = CEB23Evaluation.create({
            'document_id': 'doc-1',
            'profile_ref': _ref('prof'),
            'requirement_evidence': {
                'screen_size_vs_seating': 'design_prediction',
                'room_lighting': 'as_built_verified',
            },
        })
        v1, _ = evaluate_ceb23_requirement(
            self._profile(), ev, 'screen_size_vs_seating')
        assert v1 == 'design_satisfied'
        v2, _ = evaluate_ceb23_requirement(
            self._profile(), ev, 'room_lighting')
        assert v2 == 'as_built_satisfied'

    def test_declared_only_unverified(self) -> None:
        ev = CEB23Evaluation.create({
            'document_id': 'doc-1',
            'profile_ref': _ref('p'),
            'requirement_evidence': {
                'screen_size_vs_seating': 'declared_only',
            },
        })
        v, _ = evaluate_ceb23_requirement(
            self._profile(), ev, 'screen_size_vs_seating')
        assert v == 'unverified'


class TestDrawingSymbols:
    def _profile(self, **kw) -> ArchitecturalDrawingSymbolProfile:
        base = {
            'document_id': 'doc-1',
            'edition': 'jstd710_2015',
            'revision_state': 'production',
            'rights_provenance': 'licensed_pack',
            'rights_evidence_ref': _ref('license'),
            'symbol_set_ref': _ref('pack'),
        }
        base.update(kw)
        return ArchitecturalDrawingSymbolProfile.create(base)

    def _mapping(self, **kw) -> DeviceSymbolMapping:
        base = {
            'document_id': 'doc-1',
            'profile_ref': _ref('prof'),
            'device_kind': 'projector',
            'symbol_id': 'PJ-01',
            'symbol_revision': '2015',
        }
        base.update(kw)
        return DeviceSymbolMapping.create(base)

    def test_draft_not_production(self) -> None:
        with pytest.raises(ValueError):
            self._profile(edition='jstd710_202x_draft',
                          revision_state='production')

    def test_licensed_needs_evidence(self) -> None:
        with pytest.raises(ValueError):
            self._profile(rights_evidence_ref=None)

    def test_unlicensed(self) -> None:
        p = self._profile(rights_provenance='unknown',
                          rights_evidence_ref=None,
                          symbol_set_ref=None)
        v, _ = evaluate_symbol_claim(p, self._mapping())
        assert v == 'unlicensed_symbols'

    def test_revision_mismatch(self) -> None:
        v, _ = evaluate_symbol_claim(
            self._profile(), self._mapping(),
            export_edition='jstd710_202x_draft')
        assert v == 'revision_mismatch'

    def test_unknown_symbol(self) -> None:
        v, _ = evaluate_symbol_claim(
            self._profile(),
            self._mapping(symbol_id=None, symbol_revision=None))
        assert v == 'unknown_symbol'

    def test_mapped(self) -> None:
        v, r = evaluate_symbol_claim(
            self._profile(), self._mapping())
        assert v == 'mapped_current'
        assert 'PJ-01' in r

    def test_export_needs_mappings(self) -> None:
        with pytest.raises(ValueError):
            DrawingExportRecord.create({
                'document_id': 'doc-1',
                'profile_ref': _ref('p'),
                'mapping_refs': (),
                'export_format': 'dwg',
            })


class TestTimedText:
    def _profile(self, **kw) -> TimedTextPresentationProfile:
        base = {
            'document_id': 'doc-1',
            'profile_kind': 'imsc_text_1_3',
            'render_surface_ref': _ref('masked-screen'),
            'language': 'ja',
        }
        base.update(kw)
        return TimedTextPresentationProfile.create(base)

    def _obs(self, **kw) -> CaptionRenderObservation:
        base = {
            'document_id': 'doc-1',
            'profile_ref': _ref('prof'),
            'timing_verified_ref': _ref('timing'),
            'positioning_inside_mask': True,
            'legibility_ref': _ref('leg'),
        }
        base.update(kw)
        return CaptionRenderObservation.create(base)

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError):
            self._profile(profile_kind='unknown')

    def test_surface_required(self) -> None:
        with pytest.raises(ValueError):
            self._profile(render_surface_ref=None)

    def test_track_not_presentation(self) -> None:
        v, _ = evaluate_subtitle_claim(None, None, track_decoded=True)
        assert v == 'track_support_is_not_presentation'

    def test_profile_mismatch(self) -> None:
        v, _ = evaluate_subtitle_claim(
            self._profile(), self._obs(),
            track_profile_kind='srt')
        assert v == 'profile_mismatch'

    def test_timing_unverified(self) -> None:
        v, _ = evaluate_subtitle_claim(
            self._profile(),
            self._obs(timing_verified_ref=None))
        assert v == 'timing_not_verified'

    def test_outside_mask(self) -> None:
        v, _ = evaluate_subtitle_claim(
            self._profile(),
            self._obs(positioning_inside_mask=False))
        assert v == 'outside_masked_area'

    def test_illegible(self) -> None:
        v, _ = evaluate_subtitle_claim(
            self._profile(), self._obs(legibility_ref=None))
        assert v == 'illegible'

    def test_qualified(self) -> None:
        v, _ = evaluate_subtitle_claim(
            self._profile(), self._obs())
        assert v == 'qualified'


# ---------------------------------------------------------------------
# Repository roundtrip / tamper / fresh-migrate

from pathlib import Path  # noqa: E402

from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.cad_schema import connect_sqlite, ensure_native_schema  # noqa: E402
from htdt.cad_presentation_profile_repository import (  # noqa: E402
    CadPresentationProfileRepository,
    PresentationProfileIntegrityError,
)


def _dp_repo(tmp_path: Path) -> CadPresentationProfileRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    return CadPresentationProfileRepository(SceneRepository(db))


def test_drawprof_repository_roundtrip(tmp_path: Path) -> None:
    repo = _dp_repo(tmp_path)
    vp = HomeTheaterVideoDesignProfile.create({
        'document_id': 'doc-rt',
        'standard_ref': _ref('doc'),
        'edition': 'B',
        'requirement_map': {'room_lighting': 'viewing_environment'},
    })
    ev = CEB23Evaluation.create({
        'document_id': 'doc-rt',
        'profile_ref': _ref('p'),
        'requirement_evidence': {'room_lighting': 'declared_only'},
    })
    sp = ArchitecturalDrawingSymbolProfile.create({
        'document_id': 'doc-rt',
        'edition': 'jstd710_2015',
        'revision_state': 'production',
        'rights_provenance': 'own_drawn',
    })
    mp = DeviceSymbolMapping.create({
        'document_id': 'doc-rt',
        'profile_ref': _ref('p'),
        'device_kind': 'screen',
        'symbol_id': 'SCR-01',
        'symbol_revision': '2015',
    })
    ex = DrawingExportRecord.create({
        'document_id': 'doc-rt',
        'profile_ref': _ref('p'),
        'mapping_refs': (_ref('m'),),
        'export_format': 'pdf',
    })
    tp = TimedTextPresentationProfile.create({
        'document_id': 'doc-rt',
        'profile_kind': 'cta_708e',
        'render_surface_ref': _ref('s'),
    })
    co = CaptionRenderObservation.create({
        'document_id': 'doc-rt',
        'profile_ref': _ref('p'),
        'positioning_inside_mask': True,
    })
    repo.save_video_profile(vp)
    repo.save_ceb23_evaluation(ev)
    repo.save_symbol_profile(sp)
    repo.save_symbol_mapping(mp)
    repo.save_drawing_export(ex)
    repo.save_text_profile(tp)
    repo.save_caption_observation(co)
    assert repo.get_video_profile(vp.profile_id) == vp
    assert repo.get_ceb23_evaluation(ev.evaluation_id) == ev
    assert repo.get_symbol_profile(sp.profile_id) == sp
    assert repo.get_symbol_mapping(mp.mapping_id) == mp
    assert repo.get_drawing_export(ex.export_id) == ex
    assert repo.get_text_profile(tp.profile_id) == tp
    assert repo.get_caption_observation(co.observation_id) == co


def test_drawprof_detects_column_tamper(tmp_path: Path) -> None:
    repo = _dp_repo(tmp_path)
    tp = TimedTextPresentationProfile.create({
        'document_id': 'doc-t',
        'profile_kind': 'srt',
        'render_surface_ref': _ref('s'),
    })
    repo.save_text_profile(tp)
    with connect_sqlite(repo.path) as connection:
        connection.execute(
            'UPDATE cad_timed_text_profiles '
            "SET profile_kind='imsc_text_1_3' WHERE profile_id=?",
            (tp.profile_id,),
        )
        connection.commit()
    with pytest.raises(PresentationProfileIntegrityError):
        repo.get_text_profile(tp.profile_id)


def test_drawprof_tables_after_fresh_migrate(tmp_path: Path) -> None:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    with connect_sqlite(db) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    expected = {
        'cad_ht_video_design_profiles',
        'cad_ceb23_evaluations',
        'cad_drawing_symbol_profiles',
        'cad_device_symbol_mappings',
        'cad_drawing_export_records',
        'cad_timed_text_profiles',
        'cad_caption_render_observations',
    }
    assert expected <= tables
