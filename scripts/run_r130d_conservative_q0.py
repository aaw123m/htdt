"""Frozen undamped study: all controls, both independent mesh families, no mask."""
from pathlib import Path
import hashlib
import gzip
import json
import sys
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend/src'))
from htdt.r130d_conservative_impulse import conservative_q0_transfer, conservative_energy_audit

PLAN='benchmarks/acoustics/r130d_conservative_q0_plan_2026-10-10.json'
PLAN_SHA='79c7e66e508b27c4b80b7e4d5d1352c7f15af571e1d8f5ceb54ca50acf44bab9'
KEYS=('complex_rms_relative','magnitude_max_relative','phase_max_deg')

def pairs(z):return [[float(v.real),float(v.imag)] for v in z]
def complex_values(z):
    a=np.asarray(z,float)
    return a[:,0]+1j*a[:,1]
def scores(a,b):
    a,b=complex_values(a),complex_values(b)
    if np.any(abs(b)==0):raise ValueError('zero reference; no magnitude mask permitted')
    return dict(zip(KEYS,map(float,(np.linalg.norm(a-b)/np.linalg.norm(b),
        max(abs(abs(a)-abs(b))/abs(b)),max(abs(np.angle(a*np.conj(b),deg=True)))))))
def comparison(a,b,limits):
    s=scores(a,b)
    return {'metrics':s,'pass':all(s[k]<=limits[k] for k in KEYS),'all_frequencies_retained':True}

def main():
    raw=(ROOT/PLAN).read_bytes().replace(b'\r\n',b'\n')
    if hashlib.sha256(raw).hexdigest()!=PLAN_SHA:raise ValueError('frozen plan changed')
    plan=json.loads(raw)
    for name,sha in plan['input_sha256'].items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=sha:raise ValueError('input changed: '+name)
    cloud_path=ROOT/'benchmarks/acoustics/r130d_mfem_independent_hp_systems/cloud_provenance.json.gz'
    cloud_raw=gzip.decompress(cloud_path.read_bytes())
    if hashlib.sha256(cloud_raw).hexdigest()!=plan['cloud_provenance_sha256']:raise ValueError('cloud changed')
    cloud=json.loads(cloud_raw)
    old=json.loads((ROOT/'benchmarks/acoustics/r130d_boundary_fitted_sem_evidence_2026-10-10.json').read_text())
    result={'schema_version':'htdt.r130d.conservative-q0-evidence-1','plan_sha256_lf':PLAN_SHA,'plan':plan,
            'legacy_archived_pffdtd':'SELF_CONVERGENCE_FAILED','measured_validation':'NOT_VALIDATED','arms':[]}
    for name in plan['arms']:
        arm={'name':name,'cases':[],'independent_cases':[]}
        for ppw in plan['ppw']:
            ref=next(r for r in old['cases'] if r['ppw']==ppw)
            with np.load(ROOT/f'scratch/boundary-fitted-sem/ppw{ppw}.npz',allow_pickle=False) as d:
                lam,cp,dt,nt=d['lam'],d['coupling'],float(d['dt']),int(d['nt'])
                h=ref['native_h_m']
                H,proof=conservative_q0_transfer(lam,cp,dt,nt,h,arm=name)
                audit=conservative_energy_audit(lam,dt,nt,h,arm=name)
            if name in ('exact','gauss2'):
                key='native_sample_exact_continuous_time' if name=='exact' else 'native_dt_newmark'
                expected=complex_values(ref['signed_original_250ms_40_80'][key])
                error=float(np.linalg.norm(H-expected)/np.linalg.norm(expected))
                if error>2e-8:raise ValueError(f'frozen original control drift {name} {ppw} {error}')
            else:error=None
            arm['cases'].append({'ppw':ppw,'native_h_m':h,'native_dt_s':dt,'native_Nt':nt,
                'all_modes':len(lam),'signed_40_80':pairs(H),'conservation':audit,'control_reproduction_relative_error':error})
        arm['pairs']=[{'coarse':a['ppw'],'fine':b['ppw'],**comparison(a['signed_40_80'],b['signed_40_80'],plan['original_limits'])}
                      for a,b in zip(arm['cases'],arm['cases'][1:])]
        arm['all_four_pairs_pass']=all(p['pass'] for p in arm['pairs'])
        arm['strict_monotone_all_three']=all(all(b['metrics'][k]<a['metrics'][k] for k in KEYS)
            for a,b in zip(arm['pairs'],arm['pairs'][1:]))
        arm['original_ladder_gate_pass']=arm['all_four_pairs_pass'] and arm['strict_monotone_all_three']
        native=next(r for r in cloud['cases'] if r['ppw']==44)
        for order in plan['independent_orders_r2']+plan['independent_orders_r1']:
            with np.load(ROOT/f'scratch/independent-hp-impulse/order{order}.npz',allow_pickle=False) as d:
                lam=d['lam'];E=d['point_evaluations']
                cp=(np.asarray(native['sw'])@E[native['source_cloud_indices']])*(np.asarray(native['rw'])@E[native['receiver_cloud_indices']])
                H,_=conservative_q0_transfer(lam,cp,native['dt'],native['Nt'],arm['cases'][-1]['native_h_m'],arm=name)
                audit=conservative_energy_audit(lam,native['dt'],native['Nt'],arm['cases'][-1]['native_h_m'],arm=name)
            arm['independent_cases'].append({'order':order,'refinement':2 if order<=5 else 1,
                'all_modes':len(lam),'signed_40_80':pairs(H),'conservation':audit})
        independent=arm['independent_cases'];limits=plan['independent_fine_and_cross_limits']
        arm['independent_checks']={
            'P4_P5_r2':comparison(independent[1]['signed_40_80'],independent[2]['signed_40_80'],limits),
            'P7_P8_r1':comparison(independent[4]['signed_40_80'],independent[5]['signed_40_80'],limits),
            'SEM44_P5_r2':comparison(arm['cases'][-1]['signed_40_80'],independent[2]['signed_40_80'],limits),
            'SEM44_P8_r1':comparison(arm['cases'][-1]['signed_40_80'],independent[5]['signed_40_80'],limits)}
        arm['independent_gate_pass']=all(p['pass'] for p in arm['independent_checks'].values())
        arm['conservation_gate_pass']=all(r['conservation']['energy_relative_drift']<=plan['energy_max_relative_drift']
            and r['conservation']['unit_modulus_error']<=plan['unit_modulus_max_error'] for r in arm['cases']+independent)
        arm['qualification']='PASS_UNDAMPED_NUMERICAL_CANDIDATE' if all(arm[k] for k in (
            'original_ladder_gate_pass','independent_gate_pass','conservation_gate_pass')) else 'SELF_CONVERGENCE_FAILED'
        result['arms'].append(arm)
        print(name,arm['qualification'],[round(p['metrics']['complex_rms_relative'],7) for p in arm['pairs']],flush=True)
    result['primary_qualification']=next(a['qualification'] for a in result['arms'] if a['name']==plan['primary_arm'])
    result['physical_undamped_point_source_limit']='NOT_ESTABLISHED'
    path=ROOT/'benchmarks/acoustics/r130d_conservative_q0_evidence_2026-10-10.json'
    path.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf8')

if __name__=='__main__':main()
