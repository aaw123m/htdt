"""Prospective, unmasked 41-frequency control of the fixed R130D fixture."""
from pathlib import Path
import hashlib
import json
import os
import sys

os.environ.setdefault('OPENBLAS_NUM_THREADS','4')
os.environ.setdefault('OMP_NUM_THREADS','4')
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'backend/src'))
from htdt.r130d_smooth_pulse import all_mode_midpoint_gaussian_trace, exact_finite_gaussian_pressure_modes

PLAN = ROOT/'benchmarks/acoustics/r130d_finite_band_grid_plan_2026-10-10.json'
PLAN_SHA = '1f227418d8281664984a24dfb44aed574b8356cb27d459f97ca5bb05256b3a3f'
DEST = ROOT/'benchmarks/acoustics/r130d_finite_band_grid_evidence_2026-10-10.json'


def pairs(z):
    return [[float(v.real),float(v.imag)] for v in z]


def metrics(candidate, reference):
    if not np.isfinite(candidate).all() or not np.isfinite(reference).all() or np.any(abs(reference)==0):
        raise ValueError('nonfinite or zero reference cannot pass unmasked comparison')
    return dict(zip(('complex_rms_relative','magnitude_max_relative','phase_max_deg'),
        (float(np.linalg.norm(candidate-reference)/np.linalg.norm(reference)),
         float(np.max(abs(abs(candidate)-abs(reference))/abs(reference))),
         float(np.max(abs(np.angle(candidate*np.conj(reference),deg=True)))))))


def passes(score, limits):
    return all(score[k] <= v for k,v in limits.items())


def main():
    raw=PLAN.read_bytes().replace(b'\r\n',b'\n')
    if hashlib.sha256(raw).hexdigest()!=PLAN_SHA:
        raise ValueError('registered plan changed')
    plan=json.loads(raw)
    for name,sha in plan['input_artifacts'].items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=sha:
            raise ValueError(f'input drift: {name}')
    kwargs=dict(frequencies_hz=plan['frequencies_hz'])
    controls=[]
    selected=None
    exacts=[]
    for ppw in plan['ppw']:
        with np.load(ROOT/f'scratch/physical-pulse-sem/ppw{ppw}.npz',allow_pickle=False) as d:
            exacts.append(exact_finite_gaussian_pressure_modes(d['lam'],d['coupling'],**kwargs).sum(axis=1))
    for steps in plan['runtime_steps']:
        cases=[]
        for ppw,exact in zip(plan['ppw'],exacts):
            with np.load(ROOT/f'scratch/physical-pulse-sem/ppw{ppw}.npz',allow_pickle=False) as d:
                transfer,t,p,q=all_mode_midpoint_gaussian_trace(d['lam'],d['coupling'],steps,**kwargs)
                score=metrics(transfer,exact)
                cases.append({'ppw':ppw,'all_modes':len(d['lam']),'signed_transfer':pairs(transfer),
                    'exact_time_control':pairs(exact),'temporal_metrics':score,
                    'temporal_pass':passes(score,plan['temporal_limits'])})
            print(json.dumps({'steps':steps,'ppw':ppw,'temporal_metrics':score}),flush=True)
        controls.append({'steps':steps,'cases':cases,'pass':all(c['temporal_pass'] for c in cases)})
        if controls[-1]['pass']:
            selected=controls[-1]
            break
    if selected is None:
        selected=controls[-1]
    z=lambda a:np.array(a)[:,0]+1j*np.array(a)[:,1]
    spatial=[]
    for a,b in zip(selected['cases'],selected['cases'][1:]):
        score=metrics(z(a['signed_transfer']),z(b['signed_transfer']))
        spatial.append({'coarse_ppw':a['ppw'],'fine_ppw':b['ppw'],'metrics':score,
                        'pass':passes(score,plan['spatial_limits'])})
    monotone=all(all(b['metrics'][k]<a['metrics'][k] for k in plan['spatial_limits'])
                 for a,b in zip(spatial,spatial[1:]))
    independent=[]
    for order in plan['independent_orders']:
        with np.load(ROOT/f'scratch/independent-hp-impulse/order{order}.npz',allow_pickle=False) as d:
            cp=d['point_evaluations'][0]*d['point_evaluations'][1]
            exact=exact_finite_gaussian_pressure_modes(d['lam'],cp,**kwargs).sum(axis=1)
            independent.append({'order':order,'all_modes':len(d['lam']), 'signed_transfer':pairs(exact),
                                'matrix_sha256':str(d['matrix_sha256']),'eigenproof':json.loads(str(d['proof']))})
    fine=metrics(z(independent[-2]['signed_transfer']),z(independent[-1]['signed_transfer']))
    cross=metrics(exacts[-1],z(independent[-1]['signed_transfer']))
    limits=plan['independent_fine_and_cross_limits']
    ok=selected['pass'] and all(s['pass'] for s in spatial) and monotone and passes(fine,limits) and passes(cross,limits)
    result={'schema_version':'htdt.r130d.finite-band-grid-evidence-1',
        'plan_sha256_lf':PLAN_SHA,'plan':plan,'temporal_controls':controls,
        'selected_steps':selected['steps'] if selected['pass'] else None,
        'spatial_pairs':spatial,'strict_spatial_monotone':monotone,
        'independent_cases':independent,'independent_fine_metrics':fine,'independent_cross_metrics':cross,
        'qualification':'PASS_41_DISCRETE_FREQUENCIES' if ok else 'FAIL_41_DISCRETE_FREQUENCIES',
        'continuous_band_qualification':'NOT_ESTABLISHED','product_go':False,
        'owned_room_validation':'NOT_VALIDATED','legacy_pffdtd':'SELF_CONVERGENCE_FAILED',
        'physical_undamped_point_source_limit':'NOT_ESTABLISHED'}
    DEST.write_bytes((json.dumps(result,indent=2,allow_nan=False)+'\n').encode())
    print(json.dumps({k:result[k] for k in ('qualification','selected_steps','strict_spatial_monotone',
          'independent_fine_metrics','independent_cross_metrics')}),flush=True)


if __name__=='__main__':
    main()
