"""#1073 — luminaire photometric (IES/LDT) import tests."""

import pytest

from htdt.cad_luminaire_photometric import (
    incident_lux_at_point,
    luminaire_provenance,
    parse_eulumdat,
    parse_ies_lm63,
    parse_photometric,
)


IES_SAMPLE = """IESNA:LM-63-2002
[TEST] sample
[MANUFAC] Acme Lighting
[LUMINAIRE] Recessed downlight
[LAMP] LED
TILT=NONE
1 800 1.0 5 3 1 1 0.0 0.0 0.0
1.0
1.0
120
0 22.5 45 67.5 90
0 45 90
500 300 100 500 300 100 500 300 100
50 40 20 50 40 20
"""


def _ldt_text() -> str:
    lines = [
        'Acme',  # 0 company
        '1',  # 1 Ityp
        '0',  # 2 symmetry
        '2',  # 3 Mc (number of C-planes)
        '90.0',  # 4 Dc
        '3',  # 5 Ng (gamma points)
        '45.0',  # 6 Dg
        'Downlight',  # 7 luminaire name
        'LED 9W',  # 8 lamp
        '800',  # 9 lumens
        '2',  # 10 dtype (2 = cd)
        '1.0',  # 11 flux conversion
        '400',  # 12 luminaire wattage
        '0',  # 13
        '0', '0', '0', '0', '0', '0', '0', '0',  # 14-21 dims/areas
        '0',  # 22
        '0',  # 23
        '0',  # 24
        '0',  # 25
    ]
    # 2 C-planes x 3 gamma values
    candela = ['500', '300', '100', '450', '280', '90']
    return '\n'.join(lines + candela) + '\n'


class TestIesParsing:
    def test_parse_valid_ies(self):
        r = parse_ies_lm63(IES_SAMPLE, artifact_id='ies-1')
        assert r.verdict == 'valid'
        a = r.artifact
        assert a is not None
        assert a.source_format == 'ies_lm63'
        assert a.manufacturer == 'Acme Lighting'
        assert a.lumens_per_lamp == 800.0
        assert a.lamp_count == 1
        assert len(a.vertical_angles_deg) == 5
        assert len(a.horizontal_angles_deg) == 3
        assert a.candela[0][0] == 500.0

    def test_sha256_pinned(self):
        r = parse_ies_lm63(IES_SAMPLE, artifact_id='ies-1')
        assert len(r.artifact.source_sha256) == 64

    def test_non_ies_rejected(self):
        r = parse_ies_lm63('hello world', artifact_id='x')
        assert r.verdict == 'invalid'
        assert 'IESNA' in r.detail

    def test_tilt_unqualified(self):
        r = parse_ies_lm63(
            IES_SAMPLE.replace('TILT=NONE', 'TILT=1.0 0 0 0'),
            artifact_id='x',
        )
        assert r.verdict == 'unqualified'

    def test_truncated_table_invalid(self):
        r = parse_ies_lm63(
            IES_SAMPLE.rsplit('\n', 3)[0], artifact_id='x'
        )
        assert r.verdict == 'invalid'


class TestLdtParsing:
    def test_parse_valid_ldt(self):
        r = parse_eulumdat(_ldt_text(), artifact_id='ldt-1')
        assert r.verdict == 'valid'
        a = r.artifact
        assert a is not None
        assert a.source_format == 'eulumdat_ldt'
        assert a.manufacturer == 'Acme'
        assert a.horizontal_angles_deg == (0.0, 90.0)
        assert a.vertical_angles_deg == (0.0, 45.0, 90.0)
        assert a.candela[1][2] == 90.0

    def test_short_ldt_invalid(self):
        r = parse_eulumdat('A\nB\nC\n', artifact_id='x')
        assert r.verdict == 'invalid'


class TestDispatch:
    def test_dispatch_ies(self):
        r = parse_photometric(IES_SAMPLE, artifact_id='d1')
        assert r.verdict == 'valid'
        assert r.artifact.source_format == 'ies_lm63'

    def test_dispatch_ldt(self):
        r = parse_photometric(_ldt_text(), artifact_id='d2')
        assert r.verdict == 'valid'
        assert r.artifact.source_format == 'eulumdat_ldt'


class TestIncidentLux:
    def test_exact_grid_lux(self):
        r = parse_ies_lm63(IES_SAMPLE, artifact_id='ies-1')
        a = r.artifact
        lux = incident_lux_at_point(
            a, h_deg=0.0, v_deg=0.0, distance_m=2.0
        )
        assert lux == pytest.approx(500.0 / 4.0)

    def test_off_grid_angle_none(self):
        r = parse_ies_lm63(IES_SAMPLE, artifact_id='ies-1')
        a = r.artifact
        assert (
            incident_lux_at_point(
                a, h_deg=12.0, v_deg=0.0, distance_m=2.0
            )
            is None
        )

    def test_provenance(self):
        r = parse_ies_lm63(IES_SAMPLE, artifact_id='ies-1')
        p = luminaire_provenance(r.artifact)
        assert p.evidence_kind == 'manufacturer'
        assert p.source_sha256 == r.artifact.source_sha256
