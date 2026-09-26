"""#1077 — sound-isolation assembly pack tests."""

import pytest

from htdt.cad_isolation_assembly_pack import (
    ISOLATION_ASSEMBLY_PACK,
    STC_BANDS_HZ,
    WEAL_TL21_231_ASSEMBLY,
    WWCCA_CD_TL20_413,
    WWCCA_PAB_NOAL_18_0902,
    build_isolation_assembly,
    lookup_assembly,
    tl_at_band,
)


class TestCuratedPack:
    def test_pack_entries(self):
        assert len(ISOLATION_ASSEMBLY_PACK) == 3
        ids = {a.assembly_id for a in ISOLATION_ASSEMBLY_PACK}
        assert 'weal-tl21-231' in ids

    def test_weal_lab_measured_full_tl(self):
        a = WEAL_TL21_231_ASSEMBLY
        assert a.provenance_class == 'lab_measured'
        assert a.test_standard == 'astm_e90'
        assert a.declared_stc == 62
        assert a.declared_oitc == 49
        assert a.tl_db is not None
        assert a.tl_db[125] == 46.0
        assert a.tl_db[4000] == 67.0
        assert len(a.construction) == 4
        assert a.laboratory == 'Western Electro-Acoustic Laboratory'

    def test_wwcca_manufacturer_compiled(self):
        for a in (WWCCA_CD_TL20_413, WWCCA_PAB_NOAL_18_0902):
            assert a.provenance_class == 'manufacturer_compiled'
            assert a.tl_db is None
            assert a.declared_stc is not None

    def test_stc38_row(self):
        assert WWCCA_CD_TL20_413.declared_stc == 38
        assert 'CD-TL20-413' in WWCCA_CD_TL20_413.source_title


class TestInvariants:
    def test_field_rating_none_by_default(self):
        for a in ISOLATION_ASSEMBLY_PACK:
            assert a.field_rating is None

    def test_hashes(self):
        for a in ISOLATION_ASSEMBLY_PACK:
            assert len(a.semantic_sha256) == 64

    def test_band_restricted(self):
        with pytest.raises(ValueError, match='third-octave'):
            build_isolation_assembly(
                assembly_id='x',
                title='t',
                provenance_class='lab_measured',
                test_standard='astm_e90',
                source_title='r',
                source_uri='u',
                construction=('wall',),
                tl_db={123: 40.0},
            )

    def test_stc_bands(self):
        assert STC_BANDS_HZ[0] == 125
        assert STC_BANDS_HZ[-1] == 4000
        assert len(STC_BANDS_HZ) == 16


class TestLookup:
    def test_lookup_found(self):
        r = lookup_assembly('weal-tl21-231')
        assert r.found and r.assembly is WEAL_TL21_231_ASSEMBLY

    def test_lookup_missing(self):
        assert not lookup_assembly('nope').found

    def test_tl_exact_band_only(self):
        a = WEAL_TL21_231_ASSEMBLY
        assert tl_at_band(a, 500) == 63.0
        assert tl_at_band(a, 450) is None
        assert tl_at_band(WWCCA_CD_TL20_413, 500) is None
