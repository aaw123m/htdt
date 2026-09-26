"""#1060 — Aalto SRIR admission batch tests."""

import pytest

from htdt.cad_aalto_srir_batch import (
    AALTO_CASE_MATRIX,
    AALTO_SRIR_ADMISSIONS,
    COUPLED_ROOM_ADMISSION,
    OK5_ADMISSION,
    VARIABLE_ACOUSTICS_ROOM_ADMISSION,
    aalto_srir_benchmark_cases,
    benchmark_case_for_admission,
)


class TestAdmissions:
    def test_three_datasets_admitted(self):
        assert len(AALTO_SRIR_ADMISSIONS) == 3

    def test_all_cc_by(self):
        for a in AALTO_SRIR_ADMISSIONS:
            assert a.license_family == 'cc_by'
            assert a.license_id == 'cc-by-4.0'
            assert a.admission_state == (
                'download_on_demand_candidate'
            )

    def test_ok5_payloads_pinned(self):
        names = {f.file_name for f in OK5_ADMISSION.files}
        assert names == {'measurements.zip', 'floor_plan.pdf',
                         'pictures.zip'}
        for f in OK5_ADMISSION.files:
            assert f.md5 and len(f.md5) == 32
            assert f.size_bytes > 0

    def test_variable_room_raw_and_sh(self):
        names = {f.file_name for f in
                 VARIABLE_ACOUSTICS_ROOM_ADMISSION.files}
        assert '6dof_SRIRs_eigenmike_raw.zip' in names
        assert '6dof_SRIRs_eigenmike_SH.zip' in names
        assert '6dof_SRIRs_zylia_raw.zip' in names

    def test_coupled_room_sixteen_sofa(self):
        sofa = [f for f in COUPLED_ROOM_ADMISSION.files
                if f.file_name.endswith('.sofa')]
        assert len(sofa) == 16
        pairs = {f.file_name.split('_src')[0]
                 for f in sofa}
        assert pairs == {
            'officeToStairwell', 'officeToChamber',
            'officeToKitchen', 'roomToHallway',
        }

    def test_version_records_pinned(self):
        assert OK5_ADMISSION.version_record_id == '18622201'
        assert OK5_ADMISSION.concept_doi == (
            '10.5281/zenodo.18622200'
        )
        assert (VARIABLE_ACOUSTICS_ROOM_ADMISSION
                .version_record_id == '6382405')
        assert COUPLED_ROOM_ADMISSION.version_record_id == '7848561'


class TestBenchmarkNormalization:
    def test_each_admission_normalizes(self):
        cases = aalto_srir_benchmark_cases()
        assert len(cases) == 3
        for case in cases:
            assert case.source_asset.evidence_class == (
                'external_measured'
            )
            assert case.source_asset.admission_ref.startswith(
                'ledger/'
            )

    def test_materials_stay_unknown(self):
        for case in aalto_srir_benchmark_cases():
            assert case.materials is None
            assert 'UNKNOWN' in case.limitations

    def test_observable_matrix_declared(self):
        case = benchmark_case_for_admission(
            COUPLED_ROOM_ADMISSION, version='1'
        )
        kinds = {o.kind for o in case.observables}
        assert 'decay_metric' in kinds
        assert 'magnitude_fr' in kinds
        assert 'arrival_timing' in kinds

    def test_coupled_flagged_as_pairing_candidate(self):
        assert AALTO_CASE_MATRIX[
            'aalto-coupled-room-srir'].coupled_volume_pairing

    def test_unknown_dataset_rejected(self):
        from htdt.cad_external_admission import (
            build_external_asset_admission,
        )
        other = build_external_asset_admission(
            admission_id='ledger/other',
            dataset_name='not-aalto',
            dataset_title='x',
            publisher='x',
            source_kind='zenodo_record',
            admission_state='download_on_demand_candidate',
            license_id='cc0',
            license_family='cc0',
            record_uri='https://example.org',
        )
        with pytest.raises(ValueError):
            benchmark_case_for_admission(other, version='1')
