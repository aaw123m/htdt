"""#1075 — BT.2111 video verification pack tests."""

import pytest

from htdt.cad_bt2111_video_pack import (
    BT2111_HLG_NARROW,
    BT2111_PATTERNS,
    BT2111_PQ_FULL,
    BT2111_PQ_NARROW,
    BT2111_REFERENCE,
    EBU_MONITOR_MATERIAL_ADMISSION,
    PatternDescriptor,
    check_signal_range,
    pattern_for,
)


class TestPatternDescriptors:
    def test_three_variants_present(self):
        assert set(BT2111_PATTERNS) == {
            'hlg_narrow',
            'pq_narrow',
            'pq_full',
        }

    def test_hlg_narrow_table_values(self):
        d = BT2111_HLG_NARROW
        assert d.transfer_function == 'hlg'
        assert d.signal_range == 'narrow'
        # Rec. ITU-R BT.2111-3 Table 2 — verbatim rows.
        assert d.area('100% White').code_10bit == (940, 940, 940)
        assert d.area('100% White').code_12bit == (3760, 3760, 3760)
        assert d.area('75% Red').code_10bit == (721, 64, 64)
        assert d.area('40% Grey').code_10bit == (414, 414, 414)
        assert d.area('-7% Step').code_10bit == (4, 4, 4)
        assert d.area('109% Step').code_10bit == (1019, 1019, 1019)
        assert d.area('0% Step').code_12bit == (256, 256, 256)

    def test_pq_narrow_values(self):
        d = BT2111_PQ_NARROW
        assert d.signal_range == 'narrow'
        assert d.area('58% White').code_10bit == (573, 573, 573)
        assert d.area('-2% Black').code_10bit == (48, 48, 48)
        assert d.area('+4% Black').code_10bit == (99, 99, 99)

    def test_pq_full_values(self):
        d = BT2111_PQ_FULL
        assert d.signal_range == 'full'
        assert d.area('100% White').code_10bit == (1023, 1023, 1023)
        assert d.area('100% Blue').code_12bit == (0, 0, 4095)
        assert d.area('0% Black').code_10bit == (0, 0, 0)

    def test_reference_cited(self):
        for d in BT2111_PATTERNS.values():
            assert d.reference == BT2111_REFERENCE
            assert 'BT.2111' in d.reference

    def test_semantic_hashes(self):
        for d in BT2111_PATTERNS.values():
            assert len(d.semantic_sha256) == 64

    def test_pattern_for(self):
        assert pattern_for('pq_full') is BT2111_PQ_FULL


class TestRangeChecks:
    def test_matching_range_ok(self):
        r = check_signal_range(BT2111_HLG_NARROW, 'narrow')
        assert r.compatible

    def test_narrow_pattern_on_full_path_rejected(self):
        r = check_signal_range(BT2111_HLG_NARROW, 'full')
        assert not r.compatible
        assert 'narrow' in r.detail

    def test_full_pattern_on_narrow_path_rejected(self):
        r = check_signal_range(BT2111_PQ_FULL, 'narrow')
        assert not r.compatible

    def test_range_mismatch_in_model(self):
        with pytest.raises(ValueError, match='signal_range'):
            PatternDescriptor(
                variant='hlg_narrow',
                transfer_function='hlg',
                signal_range='full',
                reference='x',
                areas=(BT2111_HLG_NARROW.areas[0],),
                semantic_sha256='0' * 64,
            )


class TestEbuAdmission:
    def test_ebu_material_admitted(self):
        a = EBU_MONITOR_MATERIAL_ADMISSION
        assert a.admission_state == 'download_on_demand_candidate'
        assert 'tech.ebu.ch' in a.record_uri
        assert a.license_family == 'unknown'
