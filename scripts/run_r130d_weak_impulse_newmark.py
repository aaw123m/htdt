from pathlib import Path
import hashlib
import json
import sys
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend/src'))
from htdt.r130d_weak_impulse_observer import exact_weak_impulse_transfer,newmark_weak_impulse_transfer
from run_r130d_weak_impulse_observer import pairs
from htdt.r130d_general3d_validation import compare_complex_transfer

def main():
    path=ROOT/'benchmarks/acoustics/r130d_weak_impulse_newmark_addendum_plan_2026-10-10.json'
    raw=path.read_bytes().replace(b'\r\n',b'\n');sha=hashlib.sha256(raw).hexdigest()
    if sha!='fd3731023e690ba4e7704ebf952b510c2b12ed8349eab431cc08af5d1846bd27':raise ValueError('prospective Newmark plan modified')
    plan=json.loads(raw);result={'plan_sha256':sha,'plan':plan,'arms':[],'time_refinement':[],'legacy_status':'SELF_CONVERGENCE_FAILED'}
    for b in plan['end_ramp_s']:
        arm={'end_ramp_s':b,'cases':[],'pairs':[]}
        for ppw in plan['ppw']:
            with np.load(ROOT/f'scratch/boundary-fitted-sem/ppw{ppw}.npz') as d:
                dt=float(d['dt']);T=(int(d['nt'])-1)*dt
                h=newmark_weak_impulse_transfer(d['lam'],d['coupling'],dt,duration_s=T,end_ramp_s=b)
            arm['cases'].append({'ppw':ppw,'signed_40_80':pairs(h)})
        for co,fi in zip(arm['cases'],arm['cases'][1:]):
            score=compare_complex_transfer(reference=fi['signed_40_80'],candidate=co['signed_40_80'],frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode='json')
            arm['pairs'].append({'coarse':co['ppw'],'fine':fi['ppw'],'metrics':score})
        lim={'complex_rms_relative':.2,'magnitude_max_relative':.25,'phase_max_deg':15.}
        arm['all_four_pairs_pass']=all(all(r['metrics'][k]<=v for k,v in lim.items()) for r in arm['pairs'])
        arm['strict_monotone']=all(all(y['metrics'][k]<x['metrics'][k] for k in lim) for x,y in zip(arm['pairs'],arm['pairs'][1:]))
        result['arms'].append(arm)
        print(b,arm['all_four_pairs_pass'],arm['strict_monotone'],[x['metrics']['complex_rms_relative'] for x in arm['pairs']],flush=True)
    with np.load(ROOT/'scratch/boundary-fitted-sem/ppw44.npz') as d:
        ex=exact_weak_impulse_transfer(d['lam'],d['coupling'])
        for steps in plan['fixed_ppw44_time_steps']:
            h=newmark_weak_impulse_transfer(d['lam'],d['coupling'],.25/steps)
            err=float(np.linalg.norm(h-ex)/np.linalg.norm(ex))
            result['time_refinement'].append({'steps':steps,'dt':.25/steps,'signed_40_80':pairs(h),'relative_complex_error_vs_exact_all_mode_observer':err})
        result['fixed_ppw44_exact_time_signed_40_80']=pairs(ex)
    for a,b in zip(result['time_refinement'],result['time_refinement'][1:]):
        b['order_from_previous']=float(np.log2(a['relative_complex_error_vs_exact_all_mode_observer']/b['relative_complex_error_vs_exact_all_mode_observer']))
    primary=next(a for a in result['arms'] if a['end_ramp_s']==plan['primary_end_ramp_s'])
    result['primary_verdict']='PASS_CHANGED_WEAK_OBSERVER_ONLY' if primary['all_four_pairs_pass'] and primary['strict_monotone'] else 'CONVERGENCE_FAILED'
    (ROOT/'benchmarks/acoustics/r130d_weak_impulse_newmark_evidence_2026-10-10.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf8')
    print(result['primary_verdict'],result['time_refinement'])

if __name__=='__main__':main()
