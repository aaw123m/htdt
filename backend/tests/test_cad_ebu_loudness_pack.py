"""#1085 — EBU loudness conformance pack tests."""

import pytest

from htdt.cad_ebu_loudness_pack import (
    LOUDNESS_ADMISSIONS,
    TECH3341_CASES,
    TECH3342_CASES,
    check_loudness_measurement,
    loudness_case,
)


class TestCaseCatalogue:
    def test_3341_23_cases(self):
        assert len(TECH3341_CASES) == 23
        ids = [c.case_id for c in TECH3341_CASES]
        assert ids[0] == 'ebu-3341-01'
        assert ids[-1] == 'ebu-3341-23'
        assert len(set(ids)) == 23

    def test_3342_6_cases(self):
        assert len(TECH3342_CASES) == 6

    def test_case_1_expectations(self):
        c = TECH3341_CASES[0]
        exp = next(
            e for e in c.expectations if e.metric == 'integrated_lufs'
        )
        assert exp.expected == -23.0
        assert exp.tolerance_plus == 0.1
        assert exp.tolerance_minus == 0.1
        assert '1 kHz' in c.signal_description

    def test_asymmetric_true_peak_tolerances(self):
        asyms = [
            e
            for c in TECH3341_CASES
            for e in c.expectations
            if e.tolerance_plus != e.tolerance_minus
        ]
        assert asyms
        for c in TECH3341_CASES[14:]:
            assert c.expectations[0].metric == 'max_true_peak_dbtp'

    def test_constant_after_markers(self):
        marks = {
            e.constant_after_s
            for c in TECH3341_CASES
            for e in c.expectations
            if e.constant_after_s is not None
        }
        assert marks <= {1.0, 3.0, 5.0, 10.0}

    def test_admissions(self):
        assert len(LOUDNESS_ADMISSIONS) >= 2
        for a in LOUDNESS_ADMISSIONS:
            assert 'tech.ebu.ch' in a.record_uri


class TestCheckMeasurement:
    def test_pass(self):
        c = TECH3341_CASES[0]
        r = check_loudness_measurement(c, 'integrated_lufs', -23.02)
        assert r.verdict == 'pass'

    def test_fail(self):
        c = TECH3341_CASES[0]
        r = check_loudness_measurement(c, 'integrated_lufs', -22.5)
        assert r.verdict == 'fail'

    def test_incomplete_none_measurement(self):
        r = check_loudness_measurement(
            TECH3341_CASES[0], 'integrated_lufs', None
        )
        assert r.verdict == 'incomplete'

    def test_metric_not_exercised(self):
        r = check_loudness_measurement(
            TECH3342_CASES[0], 'integrated_lufs', -23.0
        )
        assert r.verdict == 'incomplete'

    def test_sequence_expectation(self):
        c = loudness_case('ebu-3341-11')
        assert c is not None
        exp = c.expectations[0]
        good = tuple(exp.expected)
        r = check_loudness_measurement(
            c, exp.metric, good
        )
        assert r.verdict == 'pass'
        bad = tuple(v + 0.5 for v in good)
        r2 = check_loudness_measurement(c, exp.metric, bad)
        assert r2.verdict == 'fail'

    def test_sequence_length_mismatch_incomplete(self):
        c = loudness_case('ebu-3341-11')
        assert c is not None
        r = check_loudness_measurement(
            c, 'max_short_term_lufs', (-23.0,)
        )
        assert r.verdict == 'incomplete'

    def test_case_lookup_missing(self):
        assert loudness_case('ebu-3341-99') is None
