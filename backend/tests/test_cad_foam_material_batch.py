"""#1071 — FOAM 01/02 material-physics batch tests."""

import pytest

from htdt.cad_foam_material_batch import (
    FOAM01_ADMISSION,
    FOAM02_ADMISSION,
    FOAM_ADMISSIONS,
    FOAM_CASE_MATRIX,
    build_jcal_parameter_set,
    foam_benchmark_case,
    foam_benchmark_cases,
    parse_jcal_params_csv,
)


class TestAdmissions:
    def test_two_datasets_admitted(self):
        assert len(FOAM_ADMISSIONS) == 2

    def test_foam01_apache_and_md5_pinned(self):
        a = FOAM01_ADMISSION
        assert a.license_family == 'apache_2_0'
        assert a.version_doi == '10.5281/zenodo.10551344'
        files = {f.file_name: f for f in a.files}
        assert files['alphas.csv'].md5 == (
            '06113f3f81fdf0d74fbe14ba9f69f0cc'
        )
        assert files['targets.csv'].md5 == (
            '2ca5288d5aa94ff5bf53a7eef317e008'
        )

    def test_foam02_cc_by_and_jcal_files(self):
        a = FOAM02_ADMISSION
        assert a.license_family == 'cc_by'
        names = {f.file_name for f in a.files}
        assert 'JCAL_params_Basotect.csv' in names
        assert 'JCAL_params_Pinta.csv' in names
        assert 'diameter.csv' in names
        for f in a.files:
            assert f.md5 is not None


class TestJcalParameters:
    def test_build_set(self):
        s = build_jcal_parameter_set(
            material_id='basotect',
            admission_id=FOAM02_ADMISSION.admission_id,
            source_file='JCAL_params_Basotect.csv',
            airflow_resistivity_pa_s_m2=10000.0,
            porosity=0.99,
            tortuosity=1.02,
            viscous_characteristic_length_m=200e-6,
            thermal_characteristic_length_m=400e-6,
            derived_frequency_domain_hz=(269.0, 2191.0),
        )
        assert s.basis == 'identified'
        assert len(s.semantic_sha256) == 64

    def test_unpublished_params_stay_none(self):
        s = build_jcal_parameter_set(
            material_id='x',
            admission_id='a',
            source_file='f.csv',
        )
        assert s.porosity is None
        assert s.airflow_resistivity_pa_s_m2 is None

    def test_invalid_frequency_domain(self):
        with pytest.raises(ValueError, match='frequency domain'):
            build_jcal_parameter_set(
                material_id='x',
                admission_id='a',
                source_file='f.csv',
                derived_frequency_domain_hz=(500.0, 100.0),
            )

    def test_csv_parse(self):
        csv_text = (
            'parameter,value\n'
            'material,basotect\n'
            'airflow_resistivity,10000\n'
            'porosity,0.99\n'
            'tortuosity,1.02\n'
        )
        (s,) = parse_jcal_params_csv(
            csv_text, admission_id='a'
        )
        assert s.material_id == 'basotect'
        assert s.airflow_resistivity_pa_s_m2 == 10000.0
        assert s.porosity == 0.99

    def test_csv_rejects_unknown_parameter(self):
        with pytest.raises(ValueError, match='unrecognized'):
            parse_jcal_params_csv(
                'parameter,value\nmaterial,x\nbogus_param,1\n',
                admission_id='a',
            )

    def test_csv_requires_material(self):
        with pytest.raises(ValueError, match='material'):
            parse_jcal_params_csv(
                'parameter,value\nporosity,0.99\n',
                admission_id='a',
            )


class TestBenchmarkBridge:
    def test_matrix_coverage(self):
        kinds = {d.measurement_kind for d in FOAM_CASE_MATRIX}
        assert 'absorption_coefficient' in kinds
        assert 'jcal_parameters' in kinds

    def test_all_cases_build(self):
        cases = foam_benchmark_cases()
        assert len(cases) == len(FOAM_CASE_MATRIX)
        for c in cases:
            assert len(c.semantic_sha256) == 64
            assert c.importer_id == 'cad_foam_material_batch'
            assert c.source_asset.evidence_class == 'external_measured'

    def test_jcal_case_is_inference_not_measured(self):
        jcal_cases = [
            d
            for d in FOAM_CASE_MATRIX
            if d.measurement_kind == 'jcal_parameters'
        ]
        assert jcal_cases
        for d in jcal_cases:
            case = foam_benchmark_case(d)
            assert (
                case.source_asset.provenance[0].evidence_kind
                == 'inferred'
            )

    def test_measured_case_provenance(self):
        d = next(
            d
            for d in FOAM_CASE_MATRIX
            if d.measurement_kind == 'absorption_coefficient'
        )
        case = foam_benchmark_case(d)
        assert (
            case.source_asset.provenance[0].evidence_kind == 'measured'
        )
        assert case.observables[0].kind == 'magnitude_fr'
