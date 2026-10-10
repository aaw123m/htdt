from pathlib import Path
import hashlib
import json
import sys
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend/src'))
from htdt.r130d_vanishing_viscosity_impulse import viscous_original_q0_transfer
from run_r130d_physical_pulse_sem import pairs,scores,passes,monotone,unpairs

def main():
    raw=(ROOT/'benchmarks/acoustics/r130d_vanishing_viscosity_q0_plan_2026-10-10.json').read_bytes().replace(b'\r\n',b'\n')
    sha=hashlib.sha256(raw).hexdigest()
    if sha!='45d9518eb59fa8e4aef18d0fb494e7fdf25cbe1515385840422ac708215c96d2':raise ValueError('prospective plan changed')
    plan=json.loads(raw)
    old=json.loads((ROOT/'benchmarks/acoustics/r130d_boundary_fitted_sem_evidence_2026-10-10.json').read_text(encoding='utf8'))
    result={'plan_sha256':sha,'plan':plan,'archived_original_pffdtd':'SELF_CONVERGENCE_FAILED','arms':[]}
    for kappa in plan['sensitivity_kappa']:
        arm={'kappa':kappa,'cases':[],'pairs':[]}
        for ppw in plan['ppw']:
            ref=next(r for r in old['cases'] if r['ppw']==ppw)
            h=ref['native_h_m']
            cache=ROOT/f'scratch/boundary-fitted-sem/ppw{ppw}.npz'
            with np.load(cache) as d:
                lam,cp,dt,nt=d['lam'],d['coupling'],float(d['dt']),int(d['nt'])
                baseline=viscous_original_q0_transfer(lam,cp,dt,nt,h,kappa=0.)
                expected=unpairs(ref['signed_original_250ms_40_80']['native_sample_exact_continuous_time'])
                error=float(np.linalg.norm(baseline-expected)/np.linalg.norm(expected))
                if error>2e-8:raise ValueError(f'zero viscosity frozen control drift {ppw} {error}')
                actual=viscous_original_q0_transfer(lam,cp,dt,nt,h,kappa=kappa)
            row={'ppw':ppw,'all_modes':len(lam),'native_h_m':h,'cache_sha256':hashlib.sha256(cache.read_bytes()).hexdigest(),
                 'native_dt_s':dt,'native_Nt':nt,'nu_h':kappa*h**3/343.2**3,'signed_40_80':pairs(actual),'kappa0_reproduction_error':error,
                 'source_and_observer_original':True,'numerical_damping_added':True}
            arm['cases'].append(row)
        for co,fi in zip(arm['cases'],arm['cases'][1:]):
            s=scores(co['signed_40_80'],fi['signed_40_80']);arm['pairs'].append({'coarse':co['ppw'],'fine':fi['ppw'],'metrics':s,'pass':passes(s,plan['primary_gates'])})
        arm['all_four_pairs_pass']=all(r['pass'] for r in arm['pairs'])
        arm['strict_monotone_all_three']=monotone([r['metrics'] for r in arm['pairs']])
        arm['gate_pass']=arm['all_four_pairs_pass'] and arm['strict_monotone_all_three']
        result['arms'].append(arm)
        print('VISCOSITY',kappa,arm['gate_pass'],[r['metrics']['complex_rms_relative'] for r in arm['pairs']],flush=True)
    result['primary_gate_pass']=next(r for r in result['arms'] if r['kappa']==plan['primary_kappa'])['gate_pass']
    result['physical_undamped_validation']='NOT_ESTABLISHED'
    (ROOT/'benchmarks/acoustics/r130d_vanishing_viscosity_q0_evidence_2026-10-10.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf8')

if __name__=='__main__':main()
