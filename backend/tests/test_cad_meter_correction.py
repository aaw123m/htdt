"""#1063 — CCMX/CCSS meter-correction import tests."""

import hashlib

import pytest

from htdt.cad_meter_correction import (
    CgatsParseError,
    correction_profile_for_binding,
    evaluate_correction_compatibility,
    import_meter_correction,
    parse_cgats_document,
)


SAMPLE_CCMX = b'''CCMX

DESCRIPTOR "Test correction"
INSTRUMENT "X-Rite i1 DisplayPro, ColorMunki Display"
MANUFACTURER_ID "MEI"
MANUFACTURER "Panasonic Industry Company"
OBSERVER "1931_2"
REFERENCE_OBSERVER "1931_2"
DISPLAY "Panasonic-TV"
TECHNOLOGY "Unknown"
DISPLAY_TYPE_BASE_ID "2"
DISPLAY_TYPE_REFRESH "YES"
REFERENCE "X-Rite i1 Pro 2"
ORIGINATOR "Argyll ccmx"
CREATED "Sat Mar 12 17:26:41 2016"
COLOR_REP "XYZ"
SOME_UNKNOWN_KEYWORD "kept verbatim"

NUMBER_OF_FIELDS 3
BEGIN_DATA_FORMAT
XYZ_X XYZ_Y XYZ_Z
END_DATA_FORMAT

NUMBER_OF_SETS 3
BEGIN_DATA
1.026507 0.0306225 -0.0136930
-0.00918743 1.064054 -0.000693945
0.00103842 0.00797577 0.956895
END_DATA
'''

SAMPLE_CCSS = b'''CCSS

DESCRIPTOR "Test LED spectral set"
ORIGINATOR "Argyll ccxxmake"
TECHNOLOGY "LCD WLED"
DISPLAY "TestMonitor"
SPECTRAL_BANDS "3"
SPECTRAL_START_NM "400.000000"
SPECTRAL_END_NM "420.000000"
SPECTRAL_NORM "1.000000"

NUMBER_OF_FIELDS 4
BEGIN_DATA_FORMAT
SAMPLE_ID SPEC_400 SPEC_410 SPEC_420
END_DATA_FORMAT

NUMBER_OF_SETS 2
BEGIN_DATA
1 0.01 0.02 0.03
2 0.05 0.06 0.07
END_DATA
'''


def test_ccmx_import():
    artifact = import_meter_correction(
        file_name='Pana50UT50_3D.ccmx', data=SAMPLE_CCMX
    )
    assert artifact.correction_kind == 'ccmx_matrix'
    assert artifact.format_id == 'CCMX'
    assert artifact.instrument == (
        'X-Rite i1 DisplayPro, ColorMunki Display'
    )
    assert artifact.reference_instrument == 'X-Rite i1 Pro 2'
    assert artifact.matrix[0] == (1.026507, 0.0306225, -0.0136930)
    assert artifact.matrix[2][2] == pytest.approx(0.956895)
    assert artifact.source_sha256 == hashlib.sha256(SAMPLE_CCMX).hexdigest()
    assert artifact.source_bytes == len(SAMPLE_CCMX)
    # unsupported keywords are preserved, not dropped
    assert artifact.keyword_table['SOME_UNKNOWN_KEYWORD'] == 'kept verbatim'
    assert artifact.keyword_table['DISPLAY_TYPE_BASE_ID'] == '2'


def test_ccss_import():
    artifact = import_meter_correction(
        file_name='test.ccss', data=SAMPLE_CCSS
    )
    assert artifact.correction_kind == 'ccss_spectral'
    assert artifact.spectral_bands == 3
    assert artifact.spectral_start_nm == 400.0
    assert artifact.spectral_samples[0] == (1.0, 0.01, 0.02, 0.03)


def test_unsupported_format_rejected():
    with pytest.raises(CgatsParseError):
        import_meter_correction(
            file_name='x.txt', data=b'FOO\nNUMBER_OF_FIELDS 0\n'
        )


def test_malformed_data_rejected():
    bad = SAMPLE_CCMX.replace(b'1.026507', b'not_a_number')
    with pytest.raises(CgatsParseError):
        import_meter_correction(file_name='bad.ccmx', data=bad)
    bad2 = SAMPLE_CCMX.replace(b'NUMBER_OF_SETS 3', b'NUMBER_OF_SETS 4')
    with pytest.raises(CgatsParseError):
        import_meter_correction(file_name='bad2.ccmx', data=bad2)


def test_compatibility_ready():
    artifact = import_meter_correction(
        file_name='x.ccmx', data=SAMPLE_CCMX
    )
    verdict = evaluate_correction_compatibility(
        artifact,
        target_meter='i1 DisplayPro',
        display='Panasonic-TV',
    )
    assert verdict.readiness == 'ready'


def test_compatibility_wrong_meter_incompatible():
    artifact = import_meter_correction(
        file_name='x.ccmx', data=SAMPLE_CCMX
    )
    verdict = evaluate_correction_compatibility(
        artifact, target_meter='Spyder5'
    )
    assert verdict.readiness == 'incompatible'
    assert any('instrument' in r for r in verdict.reasons)


def test_compatibility_unknown_display_limited():
    artifact = import_meter_correction(
        file_name='x.ccmx', data=SAMPLE_CCMX
    )
    verdict = evaluate_correction_compatibility(
        artifact,
        target_meter='i1 DisplayPro',
        display='LG OLED65',
    )
    assert verdict.readiness == 'limited'


def test_ccss_requires_spectral_capable_meter():
    artifact = import_meter_correction(
        file_name='x.ccss', data=SAMPLE_CCSS
    )
    verdict = evaluate_correction_compatibility(
        artifact,
        target_meter='i1 DisplayPro',
        display_technology='LCD WLED',
        meter_supports_spectral=False,
    )
    assert verdict.readiness == 'incompatible'
    verdict_ok = evaluate_correction_compatibility(
        artifact,
        target_meter='i1 DisplayPro',
        display_technology='LCD WLED',
        meter_supports_spectral=True,
    )
    assert verdict_ok.readiness == 'ready'


def test_binding_rejects_incompatible():
    artifact = import_meter_correction(
        file_name='x.ccmx', data=SAMPLE_CCMX
    )
    bad = evaluate_correction_compatibility(
        artifact, target_meter='Spyder5'
    )
    with pytest.raises(ValueError):
        correction_profile_for_binding(
            artifact, bad, application='external'
        )


def test_binding_produces_colorimeter_profile():
    artifact = import_meter_correction(
        file_name='x.ccmx', data=SAMPLE_CCMX
    )
    verdict = evaluate_correction_compatibility(
        artifact, target_meter='i1 DisplayPro'
    )
    profile = correction_profile_for_binding(
        artifact, verdict, application='htdt_offline'
    )
    assert profile.source_sha256 == artifact.source_sha256
    assert 'ccmx_matrix' in profile.correction_kind
    assert 'application=htdt_offline' in profile.correction_kind
    assert profile.base_meter == artifact.instrument


def test_artifact_immutable_and_hashed():
    artifact = import_meter_correction(
        file_name='x.ccmx', data=SAMPLE_CCMX
    )
    tampered = artifact.model_copy(
        update={'semantic_sha256': '0' * 64}
    )
    with pytest.raises(ValueError):
        type(tampered)(**tampered.model_dump(mode='python'))


def test_bare_cgats_document_parse():
    doc = parse_cgats_document(SAMPLE_CCMX.decode())
    assert doc.format_id == 'CCMX'
    assert doc.number_of_fields == 3
    assert doc.number_of_sets == 3
    assert doc.data_format == ('XYZ_X', 'XYZ_Y', 'XYZ_Z')
    assert len(doc.data_rows) == 3
