from pathlib import Path
import json,pytest
from htdt.r130d_native_clock_evidence import registered_plan,audit_native_extension,audit_clock_diagnostic

def test_registered_extension_keeps_every_native_condition_and_original_observer():
    p=registered_plan()
    assert p['clock_ppw']==[28,32,36,40,44]
    assert len(p['factorial_execution_order'])==25
    assert p['viscosity']==0. and p['product_go'] is False
    assert p['endpoints_retained'] and p['pressure_original_gradient_every_record']

@pytest.mark.parametrize('audit',[audit_native_extension,audit_clock_diagnostic])
def test_new_labels_cannot_override_registered_record_identity(tmp_path,audit):
    (tmp_path/'evidence.json').write_text('{"product_go":true,"qualification":"PASS"}')
    with pytest.raises(ValueError,match='identity'):audit(tmp_path)

def test_cli_spatial_failure_is_saved_without_go(tmp_path,monkeypatch,capsys):
    from htdt.r130d_cli import main
    import htdt.r130d_native_clock_evidence as module
    r={'qualification':'FAIL_NATIVE_SPATIAL_EXTENSION','product_go':False,'all_native_spatial_gates_pass':False}
    monkeypatch.setattr(module,'audit_native_extension',lambda path:r)
    output=tmp_path/'audit'
    assert main(['audit-native-clock','--study','native','--evidence-dir',str(tmp_path),'--output-dir',str(output),'--json'])==6
    assert json.loads((output/'audit.json').read_text())==r
    assert json.loads(capsys.readouterr().out)['data']['product_go'] is False

def test_cli_analytic_crosschecks_never_authorize_go(tmp_path,monkeypatch,capsys):
    from htdt.r130d_cli import main
    import htdt.r130d_native_clock_evidence as module
    r={'qualification':'ANALYTIC_DIAGNOSTICS_ONLY','gates':{'all_unfitted_asymptotic_crosschecks_pass':True},'product_go':False}
    monkeypatch.setattr(module,'audit_clock_diagnostic',lambda path:r)
    assert main(['audit-native-clock','--study','clock','--evidence-dir',str(tmp_path),'--output-dir',str(tmp_path/'audit'),'--json'])==0
    assert json.loads(capsys.readouterr().out)['data']['product_go'] is False
