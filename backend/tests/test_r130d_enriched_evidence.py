import hashlib
import pytest
from htdt.r130d_enriched_evidence import _pinned_json,audit_enrichment,PLAN_SHA_LF
from pathlib import Path
import htdt.r130d_enriched_evidence as module

def test_builtin_plan_is_frozen_and_has_no_go_authority():
    plan=_pinned_json(Path(module.__file__).with_name('r130d_contract')/'enriched_plan.json',PLAN_SHA_LF)
    assert plan['viscosity']==0. and plan['product_go'] is False
    assert plan['no_qualification_from_five_native_observers_on_one_fixed_mesh']

def test_report_labels_cannot_override_pinned_identity(tmp_path):
    (tmp_path/'evidence.json').write_text('{"product_go":true,"qualification":"PASS"}')
    with pytest.raises(ValueError,match='identity'):audit_enrichment(tmp_path)

def test_line_endings_normalized_but_content_change_rejected(tmp_path):
    p=tmp_path/'payload.json';raw=b'{\n  "x":1\n}\n'
    pin=hashlib.sha256(raw).hexdigest();p.write_bytes(raw.replace(b'\n',b'\r\n'))
    assert _pinned_json(p,pin)=={'x':1}
    p.write_bytes(raw.replace(b'1',b'2'))
    with pytest.raises(ValueError):_pinned_json(p,pin)
