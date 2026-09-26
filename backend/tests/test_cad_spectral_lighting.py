"""#1076 — spectral lighting (TM-27/TM-33) evidence tests."""

from htdt.cad_spectral_lighting import (
    BIAS_LIGHT_TARGET_XY,
    SpectralSample,
    build_spectral_evidence,
    chromaticity_from_spd,
    parse_spectral_xml,
    spectral_provenance,
    verify_bias_light,
)


SPDX_SAMPLE = """<?xml version="1.0"?>
<SpdxSpectralData xmlns="http://spdx.org">
  <Header><CCT>6500</CCT></Header>
  <SpectralData wavelength="500" power="1.2"/>
  <SpectralData wavelength="555" power="2.0"/>
  <SpectralData wavelength="600" power="0.8"/>
  <SpectralData wavelength="650" power="0.4"/>
</SpdxSpectralData>
"""

TM33_SAMPLE = """<?xml version="1.0"?>
<Luminaire xmlns="http://www.ies.org/TM33">
  <Header>
    <Manufacturer>Acme</Manufacturer>
  </Header>
  <Photometry>
    <CCT>3000</CCT>
    <CIEx>0.433</CIEx>
    <CIEy>0.403</CIEy>
    <Illuminance>350.0</Illuminance>
  </Photometry>
</Luminaire>
"""


def _hash64(s: str) -> str:
    import hashlib

    return hashlib.sha256(s.encode()).hexdigest()


class TestXmlImport:
    def test_spdx_spectral_data(self):
        r = parse_spectral_xml(SPDX_SAMPLE, evidence_id='sp1')
        assert r.verdict == 'valid'
        e = r.evidence
        assert e.cct_k == 6500.0
        assert e.spectral_power is not None
        assert len(e.spectral_power) == 4
        assert e.spectral_power[0].wavelength_nm == 500.0
        assert e.derived_from_spd is False

    def test_tm33_chromaticity(self):
        r = parse_spectral_xml(TM33_SAMPLE, evidence_id='t33-1')
        assert r.verdict == 'valid'
        e = r.evidence
        assert e.cct_k == 3000.0
        assert e.chromaticity_xy == (0.433, 0.403)
        assert e.illuminance_lux == 350.0
        assert e.spectral_power is None

    def test_invalid_xml(self):
        r = parse_spectral_xml('not xml', evidence_id='x')
        assert r.verdict == 'invalid'

    def test_empty_document(self):
        r = parse_spectral_xml(
            '<?xml version="1.0"?><Doc/>', evidence_id='x'
        )
        assert r.verdict == 'invalid'
        assert 'no spectral' in r.detail

    def test_bad_cct(self):
        r = parse_spectral_xml(
            '<?xml version="1.0"?><D><CCT>abc</CCT></D>',
            evidence_id='x',
        )
        assert r.verdict == 'invalid'


class TestSpectralEvidence:
    def test_quantities_stay_separate(self):
        e = build_spectral_evidence(
            evidence_id='e1',
            source_name='mfr',
            evidence_kind='manufacturer_declared',
            source_sha256=_hash64('x'),
            cct_k=4000.0,
        )
        assert e.chromaticity_xy is None
        assert e.spectral_power is None
        assert e.illuminance_lux is None

    def test_chromaticity_bounds(self):
        import pytest

        with pytest.raises(ValueError, match='unit square'):
            build_spectral_evidence(
                evidence_id='e1',
                source_name='mfr',
                evidence_kind='lab_measured',
                source_sha256=_hash64('x'),
                chromaticity_xy=(1.5, 0.4),
            )

    def test_derived_requires_spd(self):
        import pytest

        with pytest.raises(ValueError, match='requires spectral'):
            build_spectral_evidence(
                evidence_id='e1',
                source_name='mfr',
                evidence_kind='lab_measured',
                source_sha256=_hash64('x'),
                chromaticity_xy=(0.3, 0.3),
                derived_from_spd=True,
            )


class TestSpdDerivation:
    def test_chromaticity_from_spd_labelled(self):
        r = parse_spectral_xml(SPDX_SAMPLE, evidence_id='sp1')
        derived = chromaticity_from_spd(r.evidence)
        assert derived is not None
        assert derived.derived_from_spd is True
        assert derived.chromaticity_xy is not None
        x, y = derived.chromaticity_xy
        assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0

    def test_no_spd_no_derivation(self):
        r = parse_spectral_xml(TM33_SAMPLE, evidence_id='t33')
        assert chromaticity_from_spd(r.evidence) is None


class TestBiasLight:
    def test_d65_conformance(self):
        e = build_spectral_evidence(
            evidence_id='b1',
            source_name='meter',
            evidence_kind='user_measured',
            source_sha256=_hash64('b1'),
            chromaticity_xy=BIAS_LIGHT_TARGET_XY,
        )
        r = verify_bias_light(e)
        assert r.verdict == 'conforms'
        assert r.delta_e_proxy == 0.0

    def test_deviating(self):
        e = build_spectral_evidence(
            evidence_id='b2',
            source_name='meter',
            evidence_kind='user_measured',
            source_sha256=_hash64('b2'),
            chromaticity_xy=(0.45, 0.45),
        )
        r = verify_bias_light(e)
        assert r.verdict == 'deviates'

    def test_insufficient(self):
        e = build_spectral_evidence(
            evidence_id='b3',
            source_name='meter',
            evidence_kind='user_measured',
            source_sha256=_hash64('b3'),
            cct_k=6500.0,
        )
        r = verify_bias_light(e)
        assert r.verdict == 'insufficient_data'

    def test_provenance_kinds(self):
        e = build_spectral_evidence(
            evidence_id='p1',
            source_name='mfr',
            evidence_kind='manufacturer_declared',
            source_sha256=_hash64('p1'),
            cct_k=3000.0,
        )
        p = spectral_provenance(e)
        assert p.evidence_kind == 'manufacturer'
