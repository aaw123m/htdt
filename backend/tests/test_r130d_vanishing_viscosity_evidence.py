"""Recompute real evidence scores without importing implementation scorers."""
from pathlib import Path
import hashlib
import json
import numpy as np

ROOT=Path(__file__).resolve().parents[2]

def load(name):return json.loads((ROOT/f'benchmarks/acoustics/{name}_2026-10-10.json').read_text(encoding='utf8'))

def metrics(a,b):
    a,b=np.asarray(a),np.asarray(b);a=a[:,0]+1j*a[:,1];b=b[:,0]+1j*b[:,1]
    return np.array([np.linalg.norm(a-b)/np.linalg.norm(b),max(abs(abs(a)-abs(b))/abs(b)),max(abs(np.angle(a/b,deg=True)))])

def test_unchanged_full_original_gate_and_all_negative_sensitivities():
    e=load('r130d_vanishing_viscosity_q0_evidence')
    assert e['physical_undamped_validation']=='NOT_ESTABLISHED'
    assert e['archived_original_pffdtd']=='SELF_CONVERGENCE_FAILED'
    for arm in e['arms']:
        values=[]
        for case in arm['cases']:
            assert case['source_and_observer_original'] and case['numerical_damping_added']
            assert case['kappa0_reproduction_error']<2e-8
            assert case['all_modes']>50000 and len(case['cache_sha256'])==64
            np.testing.assert_allclose(case['nu_h'],arm['kappa']*case['native_h_m']**3/343.2**3)
        for a,b,p in zip(arm['cases'],arm['cases'][1:],arm['pairs']):
            actual=metrics(a['signed_40_80'],b['signed_40_80']);values.append(actual)
            saved=p['metrics'];np.testing.assert_allclose(actual,[saved[k] for k in ('complex_rms_relative','magnitude_max_relative','phase_max_deg')],rtol=2e-9,atol=1e-11)
            assert saved['compared_frequency_count']==2 and all(x['masked_in'] for x in saved['frequency_metrics'])
            assert p['pass']==bool(np.all(actual<=[.2,.25,15.]))
        values=np.array(values);ok=bool(np.all(values<=[.2,.25,15.]) and np.all(np.diff(values,axis=0)<0))
        assert arm['gate_pass']==ok
    assert [r['gate_pass'] for r in e['arms']]==[False,True,True]

def test_independent_complete_hp_gate_matches_original_frozen_limits():
    e=load('r130d_independent_hp_impulse_evidence');plan=e['plan'];rows=e['cases']
    assert [r['dofs'] for r in rows]==[729,2197,4913]
    assert e['independent_build']['mfem_pin']==plan['mfem_pin']
    assert rows[0]['original_matrix_reproduction_max']=={'mass_matrix':0.,'stiffness_c2_matrix':0.}
    for r in rows:
        assert r['dofs']==r['all_modes'] and r['max_operator_scaled_eigen_residual']<1e-8
        assert r['mass_norm_max_error_all_modes']<1e-8 and r['mass_orthogonality_128_sample_max_error']<1e-8
    sem=next(r for r in load('r130d_vanishing_viscosity_q0_evidence')['arms'] if r['kappa']==1.)
    fine=metrics(rows[-2]['native_viscosity_cases'][-1]['signed_40_80'],rows[-1]['native_viscosity_cases'][-1]['signed_40_80'])
    cross=metrics(sem['cases'][-1]['signed_40_80'],rows[-1]['native_viscosity_cases'][-1]['signed_40_80'])
    for actual,key in [(fine,'independent_p3_p4'),(cross,'cross_sem44_mfem_p4')]:
        saved=e[key]['metrics'];np.testing.assert_allclose(actual,[saved[k] for k in ('complex_rms_relative','magnitude_max_relative','phase_max_deg')],atol=1e-10,rtol=1e-8)
        assert e[key]['pass']==bool(np.all(actual<=[.03,.05,3.]))
    ok=bool(np.all(fine<=[.03,.05,3.]) and np.all(cross<=[.03,.05,3.]) and sem['gate_pass'])
    assert e['qualification']==('PASS_NEW_VANISHING_VISCOSITY_Q0_NUMERICAL_METHOD' if ok else 'FAIL_NEW_VANISHING_VISCOSITY_Q0_NUMERICAL_METHOD')

def test_prospective_plan_hashes_and_fixed_limit_contracts():
    for stem,expected in [('r130d_vanishing_viscosity_q0_plan','45d9518eb59fa8e4aef18d0fb494e7fdf25cbe1515385840422ac708215c96d2'),
                          ('r130d_independent_hp_impulse_plan','c6b9c42cb4cc459b8b3496c4e4b38efba6ee42f7fb82b1363aa80588ede85faf')]:
        blob=(ROOT/f'benchmarks/acoustics/{stem}_2026-10-10.json').read_bytes().replace(b'\r\n',b'\n')
        assert hashlib.sha256(blob).hexdigest()==expected


def test_supplementary_p5_refinement_preserves_failure_and_gates():
    e=load('r130d_independent_hp_p5_impulse_evidence');prior=load('r130d_independent_hp_impulse_evidence')
    assert prior['qualification']=='FAIL_NEW_VANISHING_VISCOSITY_Q0_NUMERICAL_METHOD'
    assert e['old_pinned_pffdtd']=='SELF_CONVERGENCE_FAILED' and e['physical_undamped_point_source_limit']=='NOT_ESTABLISHED'
    assert [r['dofs'] for r in e['cases']]==[4913,9261]
    assert all(r['dofs']==r['all_modes'] for r in e['cases'])
    assert e['cases'][0]==prior['cases'][-1]
    oldpath=ROOT/'benchmarks/acoustics/r130d_independent_hp_impulse_evidence_2026-10-10.json'
    assert hashlib.sha256(oldpath.read_bytes().replace(b'\r\n',b'\n')).hexdigest()==e['prior_failed_evidence_sha256_lf']
    a,b=e['cases'];primary=next(r for r in load('r130d_vanishing_viscosity_q0_evidence')['arms'] if r['kappa']==1.)
    actuals=[metrics(a['native_viscosity_cases'][-1]['signed_40_80'],b['native_viscosity_cases'][-1]['signed_40_80']),
             metrics(primary['cases'][-1]['signed_40_80'],b['native_viscosity_cases'][-1]['signed_40_80'])]
    for actual,key in zip(actuals,['independent_p4_p5','cross_sem44_mfem_p5']):
        saved=e[key]['metrics'];np.testing.assert_allclose(actual,[saved[k] for k in ('complex_rms_relative','magnitude_max_relative','phase_max_deg')],rtol=1e-8,atol=1e-10)
        assert e[key]['pass']==bool(np.all(actual<=[.03,.05,3.]))
    ok=primary['gate_pass'] and all(np.all(actual<=[.03,.05,3.]) for actual in actuals)
    assert e['qualification']==('PASS_NEW_VANISHING_VISCOSITY_Q0_NUMERICAL_METHOD' if ok else 'FAIL_NEW_VANISHING_VISCOSITY_Q0_NUMERICAL_METHOD')
    blob=(ROOT/'benchmarks/acoustics/r130d_independent_hp_p5_impulse_plan_2026-10-10.json').read_bytes().replace(b'\r\n',b'\n')
    assert hashlib.sha256(blob).hexdigest()==e['plan_sha256']=='6fed35366b3241334f4d2725466d38362783ebbdf4ec3f30a7583ca8a32f0fc5'


def test_p8_qualified_new_method_against_all_retained_frozen_gates():
    e=load('r130d_independent_hp_p8_impulse_evidence')
    priorpath=ROOT/'benchmarks/acoustics/r130d_independent_hp_p5_impulse_evidence_2026-10-10.json'
    assert hashlib.sha256(priorpath.read_bytes().replace(b'\r\n',b'\n')).hexdigest()==e['prior_failed_p5_evidence_sha256_lf']
    assert load('r130d_independent_hp_p5_impulse_evidence')['qualification']=='FAIL_NEW_VANISHING_VISCOSITY_Q0_NUMERICAL_METHOD'
    assert e['qualification']=='PASS_NEW_VANISHING_VISCOSITY_Q0_NUMERICAL_METHOD'
    assert e['old_pinned_pffdtd']=='SELF_CONVERGENCE_FAILED' and e['physical_undamped_point_source_limit']=='NOT_ESTABLISHED'
    assert [r['dofs'] for r in e['cases']]==[2197,3375,4913]
    assert all(r['dofs']==r['all_modes'] and r['max_operator_scaled_eigen_residual']<1e-8 for r in e['cases'])
    values=[]
    for x,y,pair in zip(e['cases'],e['cases'][1:],e['independent_pairs']):
        actual=metrics(x['native_viscosity_cases'][-1]['signed_40_80'],y['native_viscosity_cases'][-1]['signed_40_80']);values.append(actual)
        np.testing.assert_allclose(actual,[pair['metrics'][k] for k in ('complex_rms_relative','magnitude_max_relative','phase_max_deg')],rtol=1e-8,atol=1e-10)
        assert pair['pass'] and np.all(actual<=[.03,.05,3.])
    assert np.all(np.diff(values,axis=0)<0)
    sem=next(r for r in load('r130d_vanishing_viscosity_q0_evidence')['arms'] if r['kappa']==1.)
    cross=metrics(sem['cases'][-1]['signed_40_80'],e['cases'][-1]['native_viscosity_cases'][-1]['signed_40_80'])
    assert sem['gate_pass'] and e['cross_sem44_mfem_p8']['pass'] and np.all(cross<=[.03,.05,3.])
    np.testing.assert_allclose(cross,[e['cross_sem44_mfem_p8']['metrics'][k] for k in ('complex_rms_relative','magnitude_max_relative','phase_max_deg')],rtol=1e-8,atol=1e-10)
    blob=(ROOT/'benchmarks/acoustics/r130d_independent_hp_p8_impulse_plan_2026-10-10.json').read_bytes().replace(b'\r\n',b'\n')
    assert hashlib.sha256(blob).hexdigest()==e['plan_sha256']=='a8bdd2158da1d46228c295debe1eb6cf6884c0a343a577a88f811cf68d3defe5'
