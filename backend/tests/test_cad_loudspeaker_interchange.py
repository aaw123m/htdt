"""#1074 — CLF/GLL loudspeaker interchange tests."""

from htdt.cad_loudspeaker_interchange import (
    GLL_BOUNDARY,
    INTERCHANGE_MATRIX,
    VENDOR_DATABASE_POLICIES,
    qualify_clf,
)


CLF1_SAMPLE = """[HEADER]
Manufacturer: Acme Audio
Model: S-10
CLF format version: 1
[FREQUENCY]
63 0 -3.0 0.0
125 0 -2.0 0.0
250 0 -1.0 0.0
500 0 0.0 0.0
1000 0 0.0 0.0
2000 0 -1.5 0.0
4000 0 -4.0 0.0
8000 0 -8.0 0.0
[POLAR]
R(0)
"""

CLF2_SAMPLE = """[HEADER]
Manufacturer: Acme Audio
Model: S-20
CLF format version: 2
[LICENSE]
License: Proprietary spec sheet, reproduced with permission
[FREQUENCY]
100 0 -1.0 0.0
1000 0 0.0 0.0
"""


class TestClfQualification:
    def test_valid_clf1(self):
        q = qualify_clf(CLF1_SAMPLE.encode())
        assert q.verdict == 'qualified'
        assert q.detected_version == 'clf1'
        assert 'HEADER' in q.sections
        assert 'FREQUENCY' in q.sections
        assert q.frequency_rows == 8
        assert q.rotation_count == 1
        assert q.source_sha256 is not None

    def test_valid_clf2_needs_license(self):
        q = qualify_clf(CLF2_SAMPLE.encode())
        assert q.verdict == 'qualified'
        assert q.detected_version == 'clf2'
        assert q.declares_license

    def test_clf2_missing_license_unqualified(self):
        q = qualify_clf(
            CLF2_SAMPLE.replace('[LICENSE]', '[X]')
            .replace('License: Proprietary spec sheet', 'x')
            .encode()
        )
        # no LICENSE section for a v2 file -> unqualified
        assert q.verdict == 'unqualified'
        assert 'LICENSE' in q.missing_sections

    def test_no_sections_not_clf(self):
        q = qualify_clf(b'not a clf file at all')
        assert q.verdict == 'not_clf'

    def test_missing_frequency_unqualified(self):
        q = qualify_clf(b'[HEADER]\nManufacturer: x\n')
        assert q.verdict == 'unqualified'
        assert 'FREQUENCY' in q.missing_sections

    def test_binary_not_clf(self):
        q = qualify_clf(b'\xff\xfe\x00\x01binary')
        assert q.verdict == 'not_clf'


class TestGllBoundary:
    def test_gll_opaque(self):
        assert GLL_BOUNDARY.parse_policy == 'opaque'
        assert GLL_BOUNDARY.admission_state == 'user_import_candidate'

    def test_gll_derivatives_enumerated(self):
        assert 'clf_export' in GLL_BOUNDARY.allowed_derivatives
        assert 'user_measured' in GLL_BOUNDARY.allowed_derivatives

    def test_gll_contents_listed_opaque(self):
        assert len(GLL_BOUNDARY.known_contents) > 0
        assert 'complex_directivity' in GLL_BOUNDARY.known_contents

    def test_interchange_matrix(self):
        fmt = {i.format_name: i.state for i in INTERCHANGE_MATRIX}
        assert fmt['clf1'] == 'ready_for_admission_review'
        assert fmt['clf2'] == 'ready_for_admission_review'
        assert fmt['gll'] == 'user_import_candidate'

    def test_vendor_policies(self):
        vendors = {v.vendor: v.state for v in VENDOR_DATABASE_POLICIES}
        assert vendors['AFMG'] == 'user_import_candidate'
        assert vendors['CLF consortium'] == 'ready_for_admission_review'
        assert vendors['generic manufacturer'] == 'ineligible'
