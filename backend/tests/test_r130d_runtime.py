"""Qualification bounds and real CLI failures must not grant physical GO."""
import json

import numpy as np
import pytest
from pydantic import ValidationError

from htdt.r130d_cli import main
from htdt.r130d_runtime import DATA, R130DRequest, _validate_finite, readiness


def spec(**changes):
    return {'fixture_id':'R130D_RIGID_ROOF_56M3_FIXED_POINTS','profile':'finite_band',
            'ppw':44,'frequencies_hz':[40.,51.,80.],**changes}


@pytest.mark.parametrize('changes',[
    {'fixture_id':'arbitrary_cad'}, {'product_go':True}, {'source_xyz_m':[1.,2.,3.]},
    {'frequencies_hz':[39.]}, {'frequencies_hz':[40.5]}, {'frequencies_hz':[float('nan')]},
    {'frequencies_hz':[80.,40.]}, {'ppw':44.0},
    {'profile':'stabilized_instantaneous','frequencies_hz':[51.]},
])
def test_unqualified_inputs_are_rejected(changes):
    with pytest.raises(ValidationError):
        R130DRequest.model_validate(spec(**changes))


def test_numerical_pass_keeps_product_go_disabled(capsys):
    status=readiness()
    assert status['numerical_qualification']=='NUMERICALLY_VERIFIED'
    assert status['product_go'] is False
    assert status['recommendation_gate']=='disabled'
    assert main(['status','--require-product-go','--json'])==3
    assert json.loads(capsys.readouterr().out)['ok'] is False


def test_published_pass_cannot_hide_failed_actual_metrics():
    evidence=json.loads((DATA/'finite_band.json').read_text())
    evidence['sem_cases'][-1]['signed_transfer'][11]=[1000.,1000.]
    with pytest.raises(ValueError,match='metrics do not pass'):
        _validate_finite(evidence)


def test_tampered_asset_fails_before_writing_output(tmp_path,capsys):
    request=tmp_path/'request.json';request.write_text(json.dumps(spec()))
    assets=tmp_path/'assets/finite_band';assets.mkdir(parents=True)
    # A complete valid npz container with incorrect physical values is rejected
    # by identity before the numerical solver is invoked.
    np.savez(assets/'ppw44.npz',lam=[0.,1.],coupling=[1.,1.])
    output=tmp_path/'results'
    assert main(['run','--request',str(request),'--assets',str(assets.parent),'--output-dir',str(output)])==4
    assert 'SHA256 mismatch' in json.loads(capsys.readouterr().out)['error']
    assert not output.exists()


def test_duplicate_request_keys_cannot_override_profile(tmp_path,capsys):
    request=tmp_path/'request.json'
    request.write_text('{"profile":"finite_band","profile":"stabilized_instantaneous"}')
    assert main(['run','--request',str(request),'--assets',str(tmp_path),'--output-dir',str(tmp_path/'results')])==4
    assert 'duplicate input key' in json.loads(capsys.readouterr().out)['error']
