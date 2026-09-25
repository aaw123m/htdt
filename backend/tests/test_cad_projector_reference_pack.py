"""#1065 — projector primary-source reference pack tests."""

import pytest

from htdt.cad_projector_reference_pack import (
    EPSON_LS12000_PACK,
    JVC_DLA_NZ500_PACK,
    PROJECTOR_REFERENCE_PACKS,
    SONY_XW8100ES_PACK,
    pack_field_assertion,
    pack_field_value,
    projector_reference_pack,
    reference_pack_optical_inputs,
)


def test_pack_registry_lookup():
    assert len(PROJECTOR_REFERENCE_PACKS) == 5
    pack = projector_reference_pack('projector/jvc-dla-nz500')
    assert pack is not None
    assert pack.manufacturer == 'JVC'
    assert projector_reference_pack('projector/nonexistent') is None


def test_field_level_provenance():
    pack = JVC_DLA_NZ500_PACK
    assertion = pack_field_assertion(pack, 'throw_ratio_min')
    assert assertion is not None
    assert assertion.source_id == 'jvc-dla-nz500-specifications'
    assert assertion.evidence_class == 'manufacturer_rated'
    assert 'Throw Ratio' in assertion.locator
    # Every assertion cites a source in the pack's source list
    source_ids = {s.source_id for s in pack.sources}
    for a in pack.assertions:
        assert a.source_id in source_ids


def test_lumens_and_resolution_verified():
    assert pack_field_value(JVC_DLA_NZ500_PACK, 'rated_brightness_lumens') == 2000
    assert pack_field_value(
        JVC_DLA_NZ500_PACK, 'native_panel_resolution'
    ) == {'width': 4096, 'height': 2160}
    assert pack_field_value(
        SONY_XW8100ES_PACK, 'rated_brightness_lumens'
    ) == 3400


def test_epson_not_native_4k():
    resolution = pack_field_value(
        EPSON_LS12000_PACK, 'native_panel_resolution'
    )
    assert resolution == {'width': 1920, 'height': 1080, 'panels': 3}


def test_combined_lens_shift_stays_unknown():
    for pack in PROJECTOR_REFERENCE_PACKS:
        assert pack_field_value(pack, 'lens_shift_combined_envelope') is None
        assert 'lens_shift_combined_envelope' in pack.unknown_fields


def test_unknown_fields_not_asserted():
    pack = SONY_XW8100ES_PACK
    # XW8100ES page only verified 4 fields; the rest stay unknown
    assert pack_field_value(pack, 'throw_ratio_min') is None
    assert 'throw_ratio_min' in pack.unknown_fields


def test_optical_inputs_only_asserted():
    inputs = reference_pack_optical_inputs(JVC_DLA_NZ500_PACK)
    assert inputs['throw_ratio_min'] == 1.34
    assert inputs['lens_shift_vertical_pct'] == 70.0
    assert 'lens_shift_combined_envelope' not in inputs
    epson_inputs = reference_pack_optical_inputs(EPSON_LS12000_PACK)
    assert 'throw_ratio_min' not in epson_inputs


def test_asserted_and_unknown_are_disjoint():
    for pack in PROJECTOR_REFERENCE_PACKS:
        asserted = {a.field for a in pack.assertions}
        assert asserted.isdisjoint(set(pack.unknown_fields))


def test_assertion_tamper_rejected():
    assertion = pack_field_assertion(JVC_DLA_NZ500_PACK, 'rated_brightness_lumens')
    tampered = assertion.model_copy(update={'semantic_sha256': '0' * 64})
    with pytest.raises(ValueError):
        type(tampered)(**tampered.model_dump(mode='python'))
