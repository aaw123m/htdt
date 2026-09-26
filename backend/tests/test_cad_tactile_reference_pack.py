"""#1079 — tactile actuator reference pack tests."""

import pytest

from htdt.cad_tactile_reference_pack import (
    BUTTKICKER_LFE_REFERENCE,
    DAYTON_BST300EX_REFERENCE,
    DAYTON_TT25_16_REFERENCE,
    TACTILE_REFERENCE_PACK,
    build_tactile_reference,
    electrical_load_bounds,
    lookup_tactile_reference,
)


class TestCuratedRecords:
    def test_pack_entries(self):
        assert len(TACTILE_REFERENCE_PACK) == 3
        ids = {r.reference_id for r in TACTILE_REFERENCE_PACK}
        assert ids == {
            'buttkicker-lfe',
            'dayton-bst-300ex',
            'dayton-tt25-16',
        }

    def test_buttkicker_published_fields(self):
        r = BUTTKICKER_LFE_REFERENCE
        assert r.nominal_impedance_ohm == 4.0
        assert r.power_min_w == 400.0
        assert r.power_max_w == 1500.0
        assert r.frequency_response_hz == (5.0, 200.0)
        assert r.datasheet_uri.startswith('https://')

    def test_bst300ex(self):
        r = DAYTON_BST300EX_REFERENCE
        assert r.nominal_impedance_ohm == 4.0
        assert r.power_rms_w == 300.0

    def test_tt25_16(self):
        r = DAYTON_TT25_16_REFERENCE
        assert r.nominal_impedance_ohm == 16.0
        assert r.power_rms_w == 15.0
        assert r.resonant_frequency_hz == 40.0
        assert r.voice_coil_dc_resistance_ohm == 14.9

    def test_hashes(self):
        for r in TACTILE_REFERENCE_PACK:
            assert len(r.semantic_sha256) == 64


class TestInvariants:
    def test_no_seat_response_fields(self):
        """The pack never fabricates installed seat response."""
        for r in TACTILE_REFERENCE_PACK:
            assert not hasattr(r, 'seat_transfer')
            assert not hasattr(r, 'acceleration_g')
            assert not hasattr(r, 'felt_strength')

    def test_power_bounds_order(self):
        with pytest.raises(ValueError, match='power_max'):
            build_tactile_reference(
                reference_id='x',
                manufacturer='m',
                model='m',
                power_min_w=500.0,
                power_max_w=100.0,
                datasheet_uri='u',
                datasheet_title='t',
            )

    def test_unpublished_fields_none(self):
        r = build_tactile_reference(
            reference_id='x',
            manufacturer='m',
            model='m',
            datasheet_uri='u',
            datasheet_title='t',
        )
        assert r.nominal_impedance_ohm is None
        assert r.power_rms_w is None


class TestLookup:
    def test_found(self):
        r = lookup_tactile_reference('buttkicker-lfe')
        assert r.found and r.reference is BUTTKICKER_LFE_REFERENCE

    def test_missing(self):
        assert not lookup_tactile_reference('nope').found


class TestLoadBounds:
    def test_buttkicker(self):
        b = electrical_load_bounds(BUTTKICKER_LFE_REFERENCE)
        assert b['nominal_impedance_ohm'] == 4.0
        assert b['power_min_w'] == 400.0
        assert b['power_max_w'] == 1500.0
        assert 'power_rms_w' not in b  # not published on that datasheet

    def test_no_impedance_no_bounds(self):
        r = build_tactile_reference(
            reference_id='x',
            manufacturer='m',
            model='m',
            datasheet_uri='u',
            datasheet_title='t',
        )
        assert electrical_load_bounds(r) is None
