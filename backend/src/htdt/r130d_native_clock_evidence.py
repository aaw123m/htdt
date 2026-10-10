"""Registered all-native spatial and analytic clock diagnostics; no GO authority."""
from pathlib import Path
import hashlib,numpy as np
from .r130d_enriched_evidence import _pinned_json,_scores
from .r130d_impulse_clock_diagnostic import free_q0_trace,late_point_green
DATA=Path(__file__).with_name('r130d_contract')
PLAN_SHA='7f29ae0fa0bbc7d51ca9c053a6dc08ddd0eb90718ffd9beab49a17da2a25b4f9'
NATIVE_EVIDENCE_SHA='PENDING_NATIVE_EVIDENCE'
CLOCK_EVIDENCE_SHA='PENDING_CLOCK_EVIDENCE'

def registered_plan():return _pinned_json(DATA/'native_spatial_clock_plan.json',PLAN_SHA)
def complex_values(row):
    a=np.array(row['signed_40_80']);return a[:,0]+1j*a[:,1]
def hp_controls(plan):return _pinned_json(DATA/'enriched_hp_evidence.json',plan['hp_control_sha256_lf'])
def native_metrics(plan,cases):
    hp=hp_controls(plan);values={c['ppw']:complex_values(c) for c in cases}
    values[44]=complex_values(next(c for c in hp['cases'] if (c['ppw'],c['order'],c['quadrature_order'])==(44,3,16)))
    refs={c['ppw']:complex_values(c) for c in hp['cases'] if c['order']==4 and c['quadrature_order']==16}
    fine=[_scores(values[p],refs[p]) for p in plan['clock_ppw']]
    pairs3=[_scores(values[a],values[b]) for a,b in zip(plan['clock_ppw'],plan['clock_ppw'][1:])]
    pairs4=[_scores(refs[a],refs[b]) for a,b in zip(plan['clock_ppw'],plan['clock_ppw'][1:])]
    all_fine=bool(np.all(np.array(fine)<=plan['spatial_fine_limits']))
    return {'native_spatial_metrics':[{'ppw':p,'metrics':m,'pass':bool(np.all(np.array(m)<=plan['spatial_fine_limits']))} for p,m in zip(plan['clock_ppw'],fine)],
        'all_native_spatial_gates_pass':all_fine,'p3_native_pairs_fixed_mesh':pairs3,'p4_native_pairs_fixed_mesh':pairs4,
        'original_p3_caps_and_decrease':bool(np.all(np.array(pairs3)<=plan['original_pair_limits']) and np.all(np.diff(pairs3,axis=0)<0)),
        'original_p4_caps_and_decrease':bool(np.all(np.array(pairs4)<=plan['original_pair_limits']) and np.all(np.diff(pairs4,axis=0)<0)),
        'qualification':'PASS_SPATIAL_CONTROLS_ONLY' if all_fine else 'FAIL_NATIVE_SPATIAL_EXTENSION','product_go':False}

def audit_native_extension(evidence_dir):
    plan=registered_plan();e=_pinned_json(evidence_dir/'evidence.json',NATIVE_EVIDENCE_SHA)
    cloud=_pinned_json(DATA/'undamped_cloud.json',plan['contract_sha256']);points=np.array(cloud['points'])
    if [[r[k] for k in ('ppw','order','refinement','quadrature_order')] for r in e['cases']]!=plan['native_extension_execution_order']:
        raise ValueError('registered native extension sequence changed')
    rows=[]
    for row in e['cases']:
        ppw=row['ppw'];native=next(c for c in plan['native_cases'] if c['ppw']==ppw)
        path=evidence_dir/f'ppw{ppw}-p3-r3-q16.csv'
        if hashlib.sha256(path.read_bytes()).hexdigest()!=row['waveform_sha256']:raise ValueError('native extension waveform identity failed')
        a=np.loadtxt(path,delimiter=',',skiprows=1)
        if a.shape!=(native['Nt'],5) or not np.isfinite(a).all():raise ValueError('complete native extension record required')
        t,q,phi,p,w=a.T;source=np.zeros(len(t));source[0]=1.
        np.testing.assert_array_equal(q,source);np.testing.assert_array_equal(t,np.arange(len(t))*native['dt'])
        np.testing.assert_allclose(p,1.2*np.gradient(phi,native['dt'],edge_order=2),rtol=2e-12,atol=1e-9)
        _,free,_,_=free_q0_trace(points[native['source_cloud_indices']],points[native['receiver_cloud_indices']],native['sw'],native['rw'],native['dt'],native['Nt'])
        np.testing.assert_allclose(phi-w,free,rtol=2e-11,atol=2e-11)
        H=np.exp(2j*np.pi*np.array([40.,80.])[:,None]*t)@p
        np.testing.assert_allclose(H,complex_values(row),rtol=2e-11,atol=1e-9)
        rows.append({'ppw':ppw,'full_waveform_replayed':True,'waveform_sha256':row['waveform_sha256']})
    return {**native_metrics(plan,e['cases']),'rows':rows,'evidence_sha256_lf':NATIVE_EVIDENCE_SHA,
        'plan_sha256_lf':PLAN_SHA,'physical_limit':'NOT_ESTABLISHED','archived_fdtd':'SELF_CONVERGENCE_FAILED'}
