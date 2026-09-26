"""#1070 — anechoic programme-audio pack tests."""

from htdt.cad_anechoic_programme_pack import (
    AIRCADE_ADMISSION,
    ANECHOIC_PROGRAMME_ADMISSIONS,
    OPENAIR_ANECHOIC_ADMISSION,
    PHENICX_ANECHOIC_ADMISSION,
    TUB_BEETHOVEN8_ADMISSION,
    admission_for,
    build_programme_asset,
    evaluate_programme_admissibility,
    programme_assets,
)


class TestAdmissions:
    def test_four_datasets_admitted(self):
        assert len(ANECHOIC_PROGRAMME_ADMISSIONS) == 4

    def test_admission_ids_unique(self):
        ids = [a.admission_id for a in ANECHOIC_PROGRAMME_ADMISSIONS]
        assert len(ids) == len(set(ids))

    def test_tub_license_and_files(self):
        a = TUB_BEETHOVEN8_ADMISSION
        assert a.license_family == 'cc_by'
        assert a.license_id == 'cc-by-4.0'
        assert 'depositonce' in a.record_uri
        names = {f.file_name for f in a.files}
        assert names == {
            'movement1.zip',
            'movement2.zip',
            'movement4.zip',
            'micfilter.zip',
        }
        for f in a.files:
            assert f.checksum_source == 'none'

    def test_phenicx_cc_by(self):
        assert PHENICX_ANECHOIC_ADMISSION.license_family == 'cc_by'
        assert PHENICX_ANECHOIC_ADMISSION.version_doi == (
            '10.5281/zenodo.840025'
        )

    def test_openair_share_alike(self):
        assert OPENAIR_ANECHOIC_ADMISSION.license_family == 'cc_by_sa'
        assert OPENAIR_ANECHOIC_ADMISSION.license_id == 'cc-by-sa-4.0'

    def test_aircade_cc_by(self):
        assert AIRCADE_ADMISSION.license_family == 'cc_by'


class TestProgrammeAssets:
    def test_assets_build_and_have_categories(self):
        assets = programme_assets()
        assert len(assets) >= 6
        cats = {a.category for a in assets}
        assert 'orchestral' in cats
        assert 'speech' in cats
        assert 'instrument' in cats
        for a in assets:
            assert len(a.semantic_sha256) == 64

    def test_every_asset_resolves_admission(self):
        for a in programme_assets():
            assert admission_for(a) is not None

    def test_permitted_uses_for_cc_by_sa_exclude_probe(self):
        for a in programme_assets():
            if a.admission_id == 'openair-anechoic-snapshot-2018':
                assert 'directivity_probe' not in a.permitted_uses

    def test_admissibility_ready_for_cc_by_auralization(self):
        assets = {
            a.asset_id: a for a in programme_assets()
        }
        assert (
            evaluate_programme_admissibility(
                assets['tub-beethoven8-movement1'], 'auralization_source'
            )
            == 'ready'
        )
        assert (
            evaluate_programme_admissibility(
                assets['aircade-speech'], 'abx_programme'
            )
            == 'ready'
        )

    def test_admissibility_license_blocked(self):
        assets = {
            a.asset_id: a for a in programme_assets()
        }
        openair = assets['openair-anechoic-mixed']
        assert (
            evaluate_programme_admissibility(
                openair, 'directivity_probe'
            )
            == 'license_blocked'
        )

    def test_admissibility_missing_asset(self):
        orphan = build_programme_asset(
            asset_id='orphan',
            admission_id='no-such-admission',
            title='x',
            category='speech',
            permitted_uses=('abx_programme',),
        )
        assert (
            evaluate_programme_admissibility(orphan, 'abx_programme')
            == 'missing_asset'
        )
