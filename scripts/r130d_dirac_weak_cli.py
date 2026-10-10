"""Instantaneous R130D impulse solver with the explicitly changed weak observer.

Cached complete physical SEM eigensystems are SHA-checked against qualification
evidence. The forcing is an exact velocity jump, not a finite Gaussian pulse.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import sys
os.environ.setdefault('OPENBLAS_NUM_THREADS','4')
os.environ.setdefault('OMP_NUM_THREADS','4')
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend/src'))
from htdt.r130d_weak_impulse_observer import exact_weak_impulse_transfer,endpoint_window

def main():
    parser=argparse.ArgumentParser(description='Instantaneous Dirac input, compact weak pressure observation, original rectangular gate stays failed')
    parser.add_argument('--ppw',type=int,choices=[28,32,36,40,44],default=44)
    parser.add_argument('--cache-root',type=Path,default=ROOT/'scratch/physical-pulse-sem')
    parser.add_argument('--output-dir',type=Path,default=Path('r130d_dirac_results'))
    args=parser.parse_args()
    ep=ROOT/'benchmarks/acoustics/r130d_physical_dirac_weak_evidence_2026-10-10.json'
    evidence=json.loads(ep.read_text(encoding='utf8'))
    if evidence['qualification'] not in ('PASS_DIRAC_WEAK_OBSERVER_ONLY','FAIL_DIRAC_WEAK_OBSERVER'):
        raise ValueError('physical Dirac weak-observer qualification is incomplete')
    case=next(c for c in evidence['sem_cases'] if c['ppw']==args.ppw)
    path=args.cache_root/f'ppw{args.ppw}.npz'
    if hashlib.sha256(path.read_bytes()).hexdigest()!=case['cache_sha256']:
        raise ValueError('complete spatial eigensystem SHA mismatch')
    with np.load(path) as d:
        h=exact_weak_impulse_transfer(d['lam'],d['coupling']);modes=len(d['lam'])
    expected=np.asarray(case['signed_40_80']);expected=expected[:,0]+1j*expected[:,1]
    np.testing.assert_allclose(h,expected,rtol=1e-11,atol=1e-10)
    summary={'profile':'R130D_INSTANTANEOUS_DIRAC_WEAK_OBSERVER_V1',
        'qualification':evidence['qualification'],'legacy_original_rectangular_gate':'SELF_CONVERGENCE_FAILED',
        'physical_measured_room':'NOT_VALIDATED','source':'q[0]=1, q[n>0]=0; exact physical Dirac initial velocity jump',
        'source_lowpass':False,'source_xyz_m':[1.5,2.,2.],'receiver_xyz_m':[2.5,2.,2.],
        'observer':{'record_s':.25,'start_ramp_s':.001,'end_ramp_s':.01,'end_ramp_shape':'C2 quintic'},
        'mode_cutoff':False,'ppw':args.ppw,'all_modes':modes,'signed_40_80':[[float(z.real),float(z.imag)] for z in h],
        'units':'Pa s/m3','cache_sha256':case['cache_sha256'],
        'limitations':['changed spatial basis and point functional','changed record observer','zero-ramp limit not qualified','original sampled pressure result remains failed']}
    args.output_dir.mkdir(parents=True,exist_ok=True)
    (args.output_dir/'summary.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf8')
    np.savetxt(args.output_dir/'transfer.csv',np.column_stack(([40.,80.],h.real,h.imag,abs(h),np.angle(h,deg=True))),delimiter=',',header='frequency_Hz,real_Pa_s_m3,imag_Pa_s_m3,magnitude_Pa_s_m3,phase_deg',comments='')
    steps=8000;t=np.arange(steps)*.25/steps;q=np.zeros(steps);q[0]=1.
    np.savetxt(args.output_dir/'source_and_observer.csv',np.column_stack((t,q,endpoint_window(t))),delimiter=',',header='time_s,q_discrete_m3_s,pressure_observation_weight',comments='')
    print(json.dumps({'output_dir':str(args.output_dir.resolve()),'qualification':summary['qualification'],'legacy':summary['legacy_original_rectangular_gate']}))

if __name__=='__main__':main()
