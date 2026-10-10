"""Registered all-native spatial and analytic clock diagnostics; no GO authority."""
from pathlib import Path
import hashlib,numpy as np
from .r130d_enriched_evidence import _pinned_json,_scores
from .r130d_impulse_clock_diagnostic import free_q0_trace,late_point_green
DATA=Path(__file__).with_name('r130d_contract')
PLAN_SHA='7f29ae0fa0bbc7d51ca9c053a6dc08ddd0eb90718ffd9beab49a17da2a25b4f9'
NATIVE_EVIDENCE_SHA='c94de7336443251bb2f985697fb2f9b53a8c4859bf63af738739fea43c0cfd8e'
CLOCK_EVIDENCE_SHA='240ce2168b36f369326676915507b73181eb0b3d737983086bca49da612f8224'

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

def audit_clock_diagnostic(evidence_dir):
    from .r130d_conservative_impulse import original_pressure_endpoint_audit
    plan=registered_plan();e=_pinned_json(evidence_dir/'evidence.json',CLOCK_EVIDENCE_SHA)
    cloud=_pinned_json(DATA/'undamped_cloud.json',plan['contract_sha256']);points=np.array(cloud['points'])
    native={c['ppw']:c for c in plan['native_cases']};base=native[44];rows=[];values={};asym=[]
    if [[r['source_ppw'],r['clock_ppw']] for r in e['source_clock_factorial']]!=plan['factorial_execution_order']:
        raise ValueError('registered factorial case sequence changed')
    for family in ('refined_native44_cloud','refined_single_distance'):
        if [r['clock_refinement_factor'] for r in e[family]]!=plan['clock_refinement_factors']:
            raise ValueError('registered clock refinement sequence changed')
    expected=[]
    for row in e['source_clock_factorial']:
        s= native[row['source_ppw']];clock=native[row['clock_ppw']]
        expected.append(('factorial',row,s,clock['dt'],clock['Nt']))
    for family in ('refined_native44_cloud','refined_single_distance'):
        for row in e[family]:
            m=row['clock_refinement_factor'];expected.append((family,row,base,base['dt']/m,(base['Nt']-1)*m+1))
    for family,row,s,dt,nt in expected:
        path=evidence_dir/row['file']
        if hashlib.sha256(path.read_bytes()).hexdigest()!=row['waveform_sha256']:raise ValueError('clock waveform identity failed')
        a=np.loadtxt(path,delimiter=',',skiprows=1)
        if a.shape!=(nt,4) or not np.isfinite(a).all():raise ValueError('complete clock diagnostic record required')
        t,q,phi,p=a.T;source=np.zeros(nt);source[0]=1.
        np.testing.assert_array_equal(t,np.arange(nt)*dt);np.testing.assert_array_equal(q,source)
        if family=='refined_single_distance':
            xyzs=np.zeros((8,3));xyzr=np.tile([plan['single_distance_m'],0.,0.],(8,1));sw=rw=np.ones(8)/8
        else:xyzs=points[s['source_cloud_indices']];xyzr=points[s['receiver_cloud_indices']];sw=s['sw'];rw=s['rw']
        _,exact,_,_=free_q0_trace(xyzs,xyzr,sw,rw,dt,nt)
        np.testing.assert_allclose(phi,exact,rtol=2e-11,atol=2e-11)
        np.testing.assert_allclose(p,1.2*np.gradient(phi,dt,edge_order=2),rtol=2e-12,atol=1e-9)
        H=np.exp(2j*np.pi*np.array([40.,80.])[:,None]*t)@p
        np.testing.assert_allclose(H,complex_values(row),rtol=2e-11,atol=2e-8)
        endpoint=original_pressure_endpoint_audit(phi,dt)
        for i in range(2):
            for part in ('total','bulk','initial_stencils','final_stencils'):
                np.testing.assert_allclose(endpoint['frequencies'][i][part],row['endpoint']['frequencies'][i][part],rtol=2e-11,atol=2e-8)
        key=(row['source_ppw'],row['clock_ppw']) if family=='factorial' else row['clock_refinement_factor']
        values[family,key]=H
        if family=='refined_single_distance':
            window=evidence_dir/row['asymptotic_window_file']
            if hashlib.sha256(window.read_bytes()).hexdigest()!=row['asymptotic_window_sha256']:raise ValueError('asymptotic window identity failed')
            data=np.loadtxt(window,delimiter=',',skiprows=1);n=np.arange(nt-plan['asymptotic_last_sample_count'],nt)
            predicted,envelope=late_point_green(plan['single_distance_m'],n,dt)
            np.testing.assert_allclose(data,np.column_stack((n,n*dt,phi[n],predicted,envelope)),rtol=2e-12,atol=1e-12)
            error=float(np.linalg.norm(phi[n]-predicted)/np.linalg.norm(phi[n]))
            asym.append({'factor':key,'rms_relative_error':error,'pass':error<=plan['asymptotic_rms_error_limit'],
                'unfitted_envelope_last':float(envelope[-1])})
        rows.append({'file':row['file'],'Nt':nt,'full_record_replayed':True,'all_endpoints_retained':True})
    ppw=plan['clock_ppw'];factors=plan['clock_refinement_factors']
    metrics={
        'source_only_fixed_clock44_pairs':[_scores(values['factorial',(a,44)],values['factorial',(b,44)]) for a,b in zip(ppw,ppw[1:])],
        'clock_only_fixed_source44_pairs':[_scores(values['factorial',(44,a)],values['factorial',(44,b)]) for a,b in zip(ppw,ppw[1:])],
        'original_diagonal_free_pairs':[_scores(values['factorial',(a,a)],values['factorial',(b,b)]) for a,b in zip(ppw,ppw[1:])]}
    for family in ('refined_native44_cloud','refined_single_distance'):
        metrics[family+'_pairs']=[_scores(values[family,a],values[family,b]) for a,b in zip(factors,factors[1:])]
    for name,actual in metrics.items():np.testing.assert_allclose(actual,e[name],rtol=2e-11,atol=2e-8)
    ok=all(r['pass'] for r in asym)
    return {'qualification':'ANALYTIC_DIAGNOSTICS_ONLY' if ok else 'ANALYTIC_CROSSCHECK_FAILED',
        'gates':{'all_unfitted_asymptotic_crosschecks_pass':ok},'metrics':metrics,'asymptotic':asym,'rows':rows,
        'evidence_sha256_lf':CLOCK_EVIDENCE_SHA,'plan_sha256_lf':PLAN_SHA,'product_go':False,
        'free_space_not_closed_room':True,'endpoint_correction_applied':False,'archived_fdtd':'SELF_CONVERGENCE_FAILED',
        'physical_limit':'NOT_ESTABLISHED','original_nonconvergence_resolved':False}
