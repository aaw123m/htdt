"""#1067 — spatial starter pack tests."""

import pytest

from htdt.cad_spatial_reproduction import normalize_spherical_position
from htdt.cad_spatial_starter_pack import (
    COORDINATE_REGRESSION_VECTORS,
    PRINCETON_HRTF_ADMISSION,
    SADIE_II_ADMISSION,
    SPATIAL_STARTER_PACK,
    build_spatial_fixture,
)


class TestSadieAdmission:
    def test_apache_license_raw_string(self):
        # SpatialDatasetLicense has no apache entry — verbatim kept here
        assert SADIE_II_ADMISSION.license_id == 'apache-2.0'
        assert SADIE_II_ADMISSION.license_family == 'apache_2_0'

    def test_d1_d2_one_human_pinned(self):
        roles = {f.role for f in SADIE_II_ADMISSION.files}
        assert 'hrir_sofa_d1_ku100' in roles
        assert 'hrir_sofa_d2_kemar' in roles
        assert any(r.startswith('hrir_sofa_human_') for r in roles)

    def test_license_text_bundled(self):
        names = {f.file_name for f in SADIE_II_ADMISSION.files}
        assert 'LICENSE.txt' in names


class TestFixtureClasses:
    def test_classes_distinct(self):
        classes = {f.fixture_class
                   for f in SPATIAL_STARTER_PACK.fixtures}
        assert 'generic_mannequin' in classes
        assert 'individual_measured' in classes
        assert 'brir' in classes

    def test_brir_cannot_claim_free_field(self):
        with pytest.raises(ValueError):
            build_spatial_fixture(
                fixture_id='x/brir',
                admission=SADIE_II_ADMISSION,
                fixture_class='brir',
                sofa_convention='SimpleFreeFieldHRIR',
                coordinate_spec=(
                    SPATIAL_STARTER_PACK.fixtures[0].coordinate_spec
                ),
            )

    def test_brir_cannot_feed_reproduction_profile(self):
        with pytest.raises(ValueError):
            build_spatial_fixture(
                fixture_id='x/brir',
                admission=SADIE_II_ADMISSION,
                fixture_class='brir',
                sofa_convention='SimpleBRIR',
                allowed_uses=('reproduction_profile',),
                coordinate_spec=(
                    SPATIAL_STARTER_PACK.fixtures[0].coordinate_spec
                ),
            )

    def test_subject_identity_preserved(self):
        ids = {f.subject_id
               for f in SPATIAL_STARTER_PACK.fixtures}
        assert {'D1', 'D2', 'H20', 'subject-1'} <= ids

    def test_princeton_is_user_import(self):
        assert PRINCETON_HRTF_ADMISSION.admission_state == (
            'user_import_candidate'
        )


class TestCoordinateRegression:
    def test_vectors_match_normalized_frame(self):
        for (az_in, el_in, dist, az_out, el_out) in (
            COORDINATE_REGRESSION_VECTORS
        ):
            az, el, d = normalize_spherical_position(
                azimuth_deg=az_in,
                elevation_deg=el_in,
                distance_m=dist,
            )
            assert az == pytest.approx(az_out)
            assert el == pytest.approx(el_out)
            assert d == pytest.approx(dist)
