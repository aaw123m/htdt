from pathlib import Path
import pytest
from htdt.r130d_registered_enrichment import PINS,audit_registered_enrichment,DATA
from htdt.r130d_enriched_evidence import _pinned_json

def test_unregistered_study_cannot_inherit_any_gate():
    with pytest.raises(ValueError,match='unregistered'):audit_registered_enrichment(Path('.'),'unregistered')

@pytest.mark.parametrize('study',['hp','images'])
def test_pinned_plan_is_inviscid_and_has_no_go_authority(study):
    pin=PINS[study];p=_pinned_json(DATA/pin['plan'],pin['plan_sha'])
    assert p['viscosity']==0. and p['product_go'] is False
    assert p['no_qualification_from_five_native_observers_on_one_fixed_mesh']

@pytest.mark.parametrize('study',['hp','images'])
def test_altered_evidence_cannot_claim_a_successful_label(tmp_path,study):
    (tmp_path/'evidence.json').write_text('{"product_go":true,"qualification":"PASS"}')
    with pytest.raises(ValueError,match='identity'):audit_registered_enrichment(tmp_path,study)

def test_cli_retains_failed_registered_audit_and_failure_exit(tmp_path,capsys,monkeypatch):
    import json
    from htdt.r130d_cli import main
    import htdt.r130d_registered_enrichment as registered
    report={'qualification':'SELF_CONVERGENCE_FAILED','product_go':False,'gates':{'cross_gate':False}}
    monkeypatch.setattr(registered,'audit_registered_enrichment',lambda *args:report)
    output=tmp_path/'new-audit'
    assert main(['audit-registered-enrichment','--study','images','--evidence-dir',str(tmp_path),
        '--output-dir',str(output),'--json'])==6
    assert json.loads(capsys.readouterr().out)['data']['product_go'] is False
    assert json.loads((output/'audit.json').read_text())==report
    assert (output/'verification.json').is_file()

def test_cli_rejects_changed_evidence_without_publishing_audit(tmp_path,capsys):
    import json
    from htdt.r130d_cli import main
    (tmp_path/'evidence.json').write_text('{"qualification":"PASS"}')
    output=tmp_path/'new-audit'
    assert main(['audit-registered-enrichment','--study','hp','--evidence-dir',str(tmp_path),
        '--output-dir',str(output),'--json'])==4
    assert json.loads(capsys.readouterr().out)['ok'] is False
    assert not output.exists()
