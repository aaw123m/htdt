"""Prospective full-mode weak impulse observer and all endpoint sensitivities."""
from pathlib import Path
import hashlib
import json
import sys
import numpy as np
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend/src'))
from htdt.r130d_weak_impulse_observer import exact_weak_impulse_transfer
from htdt.r130d_general3d_validation import compare_complex_transfer
from htdt.r130d_exact_semidiscrete_causal_q0 import exact_continuous_modal_delta_impulse_signed_original_250ms

PLAN = ROOT/'benchmarks/acoustics/r130d_weak_impulse_observer_plan_2026-10-10.json'
SHA = '796d87da6feeb058a596314e270b599b25f54071f31fe65ac41859f5792513ca'

def pairs(v):
    return [[float(z.real),float(z.imag)] for z in v]

def main():
    raw = PLAN.read_bytes().replace(b'\r\n',b'\n')
    if hashlib.sha256(raw).hexdigest() != SHA:
        raise ValueError('prospective plan changed')
    plan = json.loads(raw)
    prior = json.loads((ROOT/'benchmarks/acoustics/r130d_boundary_fitted_sem_evidence_2026-10-10.json').read_text(encoding='utf8'))
    result = {'plan_sha256':SHA,'plan':plan,'unchanged_q0':True,
              'observer_changed':True,'original_rectangular_gate':'SELF_CONVERGENCE_FAILED',
              'zero_ramp_limit':'NOT_ESTABLISHED','arms':[]}
    for clock in ('native','fixed_250ms'):
        for b in plan['end_ramp_sensitivity_s']:
            arm={'clock':clock,'end_ramp_s':b,'cases':[],'adjacent_pairs':[]}
            for ppw in plan['ppw']:
                path=ROOT/plan['sem_cache'].format(ppw=ppw)
                with np.load(path) as dat:
                    lam, cp = dat['lam'],dat['coupling']
                    dt,nt=float(dat['dt']),int(dat['nt'])
                    T=(nt-1)*dt if clock=='native' else .25
                    ref=next(c for c in prior['cases'] if c['ppw']==ppw)
                    if len(lam)!=ref['complete_3d_modes'] or dt!=ref['native_dt_s'] or nt!=ref['native_Nt']:
                        raise ValueError('original all-mode cache provenance changed')
                    baseline=exact_continuous_modal_delta_impulse_signed_original_250ms(lam,cp,dt,nt).sum(axis=1)
                    expected=np.asarray(ref['signed_original_250ms_40_80']['native_sample_exact_continuous_time'])
                    expected=expected[:,0]+1j*expected[:,1]
                    if np.linalg.norm(baseline-expected)/np.linalg.norm(expected)>2e-9:
                        raise ValueError('cached modes do not reproduce frozen original impulse evidence')
                    h=exact_weak_impulse_transfer(lam,cp,duration_s=T,
                        start_ramp_s=plan['primary_start_ramp_s'],end_ramp_s=b)
                arm['cases'].append({'ppw':ppw,'cache_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                    'original_hdf5_sha256':ref['original_hdf5_sha256'],'all_modes':len(lam),
                    'duration_s':T,'signed_40_80':pairs(h)})
            for co,fi in zip(arm['cases'],arm['cases'][1:]):
                metrics=compare_complex_transfer(reference=fi['signed_40_80'],candidate=co['signed_40_80'],
                    frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode='json')
                arm['adjacent_pairs'].append({'coarse':co['ppw'],'fine':fi['ppw'],'metrics':metrics,
                    'pass':all(metrics[k]<=v for k,v in plan['three_gates'].items())})
            scores=arm['adjacent_pairs']
            arm['all_four_pairs_pass']=all(c['pass'] for c in scores)
            arm['strict_monotone']=all(all(y['metrics'][k]<x['metrics'][k] for k in plan['three_gates'])
                for x,y in zip(scores,scores[1:]))
            print(clock,b,arm['all_four_pairs_pass'],arm['strict_monotone'],
                  [x['metrics']['complex_rms_relative'] for x in scores],flush=True)
            result['arms'].append(arm)
    primary=next(a for a in result['arms'] if a['clock']=='native' and a['end_ramp_s']==plan['primary_end_ramp_s'])
    result['primary_verdict']='PASS_WEAK_OBSERVER_ONLY' if primary['all_four_pairs_pass'] and primary['strict_monotone'] else 'WEAK_OBSERVER_CONVERGENCE_FAILED'
    dest=ROOT/'benchmarks/acoustics/r130d_weak_impulse_observer_evidence_2026-10-10.json'
    dest.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf8')
    print(result['primary_verdict'],flush=True)

if __name__=='__main__':
    main()
