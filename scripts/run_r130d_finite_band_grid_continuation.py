"""Joint h/dt refinement and independent P8-P10, retaining the failed control."""
from pathlib import Path
import argparse
import hashlib
import json
import os
os.environ.setdefault('OPENBLAS_NUM_THREADS','4')
os.environ.setdefault('OMP_NUM_THREADS','4')
import numpy as np
from run_r130d_finite_band_grid import ROOT, pairs, metrics, passes
from run_r130d_independent_hp_impulse import assemble_case
from htdt.r130d_smooth_pulse import all_mode_midpoint_gaussian_trace, exact_finite_gaussian_pressure_modes

PLAN_SHA='44fcb9ea998979c150b4f8cf4b116e89a51c2a97055d372d1019c23a44ee35c6'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--exports',type=Path,required=True)
    parser.add_argument('--assemble-only',action='store_true')
    args=parser.parse_args()
    raw=(ROOT/'benchmarks/acoustics/r130d_finite_band_grid_continuation_plan_2026-10-10.json').read_bytes().replace(b'\r\n',b'\n')
    if hashlib.sha256(raw).hexdigest()!=PLAN_SHA:
        raise ValueError('supplementary plan changed')
    plan=json.loads(raw)
    prior=(ROOT/'benchmarks/acoustics/r130d_finite_band_grid_evidence_2026-10-10.json').read_bytes().replace(b'\r\n',b'\n')
    if hashlib.sha256(prior).hexdigest()!=plan['parent_failed_evidence_sha256_lf'] or json.loads(prior)['qualification']!='FAIL_41_DISCRETE_FREQUENCIES':
        raise ValueError('previous failure missing or changed')
    for name,expected in plan['input_artifacts'].items():
        if sha(ROOT/name)!=expected:
            raise ValueError(f'input drift: {name}')
    for name in ('points.txt','cloud_provenance.json','build_provenance.json'):
        if sha(args.exports/name)!=plan[name+'_sha256']:
            raise ValueError('independent build/source cloud changed')
    cloud=json.loads((args.exports/'cloud_provenance.json').read_text())
    build=json.loads((args.exports/'build_provenance.json').read_text(encoding='utf-8-sig'))
    if build['mfem_pin']!=plan['mfem_pin'] or build['executable_sha256']!=plan['exporter_executable_sha256']:
        raise ValueError('independent exporter pin changed')
    independent=[]
    for order in plan['independent_orders']:
        if (2*order+1)**3>plan['independent_max_dofs']:
            raise ValueError('prospective dense memory domain exceeded')
        print('ASSEMBLE_INDEPENDENT',order,flush=True)
        row=assemble_case(order,args.exports,cloud,ROOT/'scratch/independent-hp-impulse',refinement=1,
             rigid_operator_scaled_limit=plan['supplementary_p9_p10_rigid_operator_scaled_limit'] if order>=9 else None)
        with np.load(ROOT/f'scratch/independent-hp-impulse/order{order}.npz',allow_pickle=False) as d:
            cp=d['point_evaluations'][0]*d['point_evaluations'][1]
            h=exact_finite_gaussian_pressure_modes(d['lam'],cp,frequencies_hz=plan['frequencies_hz']).sum(axis=1)
        independent.append({'order':order,'all_modes':row['dofs'],'cache_sha256':row['cache_sha256'],
             'matrix_sha256':row['matrix_sha256'],'signed_transfer':pairs(h),'eigenproof':row})
        print('INDEPENDENT_READY',order,flush=True)
    if args.assemble_only:
        return
    cases=[]
    for ppw in plan['ppw']:
        steps=8*int(np.ceil(.25*np.sqrt(3)*100*ppw))
        with np.load(ROOT/f'scratch/physical-pulse-sem/ppw{ppw}.npz',allow_pickle=False) as d:
            kwargs=dict(frequencies_hz=plan['frequencies_hz'])
            exact=exact_finite_gaussian_pressure_modes(d['lam'],d['coupling'],**kwargs).sum(axis=1)
            h,t,p,q=all_mode_midpoint_gaussian_trace(d['lam'],d['coupling'],steps,**kwargs)
            score=metrics(h,exact)
            cases.append({'ppw':ppw,'steps':steps,'all_modes':len(d['lam']),
                'signed_transfer':pairs(h),'exact_time_control':pairs(exact),'temporal_metrics':score,
                'temporal_pass':passes(score,plan['temporal_limits'])})
        print('JOINT_CASE',json.dumps({k:cases[-1][k] for k in ('ppw','steps','temporal_metrics')}),flush=True)
    z=lambda a:np.array(a)[:,0]+1j*np.array(a)[:,1]
    spatial=[]
    for a,b in zip(cases,cases[1:]):
        score=metrics(z(a['signed_transfer']),z(b['signed_transfer']))
        spatial.append({'coarse_ppw':a['ppw'],'fine_ppw':b['ppw'],'metrics':score,'pass':passes(score,plan['spatial_limits'])})
    monotone=all(all(b['metrics'][k]<a['metrics'][k] for k in plan['spatial_limits']) for a,b in zip(spatial,spatial[1:]))
    fine=metrics(z(independent[-2]['signed_transfer']),z(independent[-1]['signed_transfer']))
    cross=metrics(z(cases[-1]['exact_time_control']),z(independent[-1]['signed_transfer']))
    limits=plan['independent_fine_and_cross_limits']
    ok=all(c['temporal_pass'] for c in cases) and all(s['pass'] for s in spatial) and monotone and passes(fine,limits) and passes(cross,limits)
    evidence={'schema_version':'htdt.r130d.finite-band-grid-continuation-evidence-1',
        'plan_sha256_lf':PLAN_SHA,'plan':plan,'sem_cases':cases,'spatial_pairs':spatial,'strict_spatial_monotone':monotone,
        'independent_cases':independent,'independent_fine_metrics':fine,'independent_cross_metrics':cross,
        'qualification':'PASS_41_DISCRETE_FREQUENCIES' if ok else 'FAIL_41_DISCRETE_FREQUENCIES',
        'continuous_band_qualification':'NOT_ESTABLISHED','product_go':False,'owned_room_validation':'NOT_VALIDATED',
        'legacy_pffdtd':'SELF_CONVERGENCE_FAILED','physical_undamped_point_source_limit':'NOT_ESTABLISHED'}
    dest=ROOT/'benchmarks/acoustics/r130d_finite_band_grid_continuation_evidence_2026-10-10.json'
    dest.write_bytes((json.dumps(evidence,indent=2,allow_nan=False)+'\n').encode())
    print(json.dumps({k:evidence[k] for k in ('qualification','strict_spatial_monotone','independent_fine_metrics','independent_cross_metrics')}),flush=True)


if __name__=='__main__':
    main()
