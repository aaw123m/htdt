from htdt.cad_model_validation import build_model_validation
from htdt.comparison import FrequencyResponse


AUTH = dict(document_id='doc', search_spec_id='spec', search_spec_sha256='1' * 64, candidate_set_sha256='2' * 64)

def _fr(offset):
    return FrequencyResponse(frequency_hz=(20,40,80,160), level_db=tuple(offset+x for x in (0,1,-1,0)))


def test_validation_requires_holdout_before_recommendation_eligibility():
    record=build_model_validation(**AUTH, model_id='fixture', model_version='1',
        samples=(('a','calibration','p:a','m:a',_fr(0),_fr(.5)),),
        low_hz=20, high_hz=160, max_holdout_rms_db=3)
    assert record.recommendation_gate == 'disabled'
    assert record.residual_gate == 'insufficient'
    assert record.holdout_rms_db is None


def test_validation_gate_uses_holdout_not_calibration_fit():
    record=build_model_validation(**AUTH, model_id='fixture', model_version='1',
        samples=(
            ('a','calibration','p:a','m:a',_fr(0),_fr(0)),
            ('b','holdout','p:b','m:b',_fr(0),_fr(6)),
        ), low_hz=20, high_hz=160, max_holdout_rms_db=3)
    assert record.calibration_rms_db == 0
    assert record.holdout_rms_db >= 5
    assert record.residual_gate == 'fail'
    assert record.recommendation_gate == 'disabled'
