"""#1066 — loudspeaker directivity admission tests."""

import pytest

from htdt.cad_directivity_admission import (
    AALTO_DIRECTIVITY_RECORD,
    DIRECTIVITY_ADMISSION_RECORDS,
    PRINCETON_DIRECTIVITY_RECORD,
    build_directivity_admission,
    coordinate_normalization_spec,
    directivity_record_for,
)
from htdt.cad_external_admission import build_external_asset_admission


def _admission(state='user_import_candidate'):
    return build_external_asset_admission(
        admission_id='ledger/x',
        dataset_name='x',
        dataset_title='x',
        publisher='x',
        source_kind='institutional_repository',
        admission_state=state,
        license_id='cc-by-4.0',
        license_family='cc_by',
        record_uri='https://example.org',
    )


def _spec():
    return coordinate_normalization_spec(
        source_convention='x',
        source_azimuth_sense='clockwise',
        source_azimuth_origin='front',
        source_elevation_sense='up_positive',
        htdt_transform='x',
    )


class TestRecords:
    def test_aalto_full_sphere_complex(self):
        r = AALTO_DIRECTIVITY_RECORD
        assert r.coverage == 'full_sphere'
        assert r.full_sphere_capable is True
        assert r.capability_ceiling == 'complex'
        sofa = [f for f in r.admission.files
                if f.file_name.endswith('.sofa')]
        assert len(sofa) == 4

    def test_princeton_never_promoted(self):
        r = PRINCETON_DIRECTIVITY_RECORD
        assert r.coverage == 'horizontal_and_vertical_planes'
        assert r.full_sphere_capable is False
        # no checksums published -> user import candidate, honest pointer
        assert r.admission.admission_state == 'user_import_candidate'
        assert all(
            f.checksum_source == 'none' for f in r.admission.files
        )

    def test_promotion_guard_rejects_planes(self):
        with pytest.raises(ValueError):
            build_directivity_admission(
                admission=_admission(),
                coverage='horizontal_plane',
                capability_ceiling='complex',
                full_sphere_capable=True,
                coordinate_spec=_spec(),
                measurement_class='x',
            )

    def test_coordinate_specs_explicit(self):
        for record in DIRECTIVITY_ADMISSION_RECORDS:
            spec = record.coordinate_spec
            assert spec.source_azimuth_sense in (
                'clockwise', 'counter_clockwise',
            )
            assert 'azimuth' in spec.htdt_transform

    def test_lookup(self):
        assert directivity_record_for(
            'aalto-ls-directivity'
        ) is AALTO_DIRECTIVITY_RECORD
        assert directivity_record_for('nope') is None
