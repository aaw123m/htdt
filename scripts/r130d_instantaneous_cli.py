"""Instantaneous q0 + original observer, explicitly vanishing numerical viscosity."""
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
from htdt.r130d_vanishing_viscosity_impulse import viscous_original_q0_transfer,viscous_original_q0_trace

def main():
    p=argparse.ArgumentParser(description='Original instantaneous source and rectangular pressure record; new consistent numerical viscosity')
    p.add_argument('--ppw',type=int,choices=[28,32,36,40,44],default=44)
    p.add_argument('--cache-root',type=Path,default=ROOT/'scratch/boundary-fitted-sem')
    p.add_argument('--output-dir',type=Path,default=Path('r130d_instantaneous_results'))
    args=p.parse_args()
    evidence=json.loads((ROOT/'benchmarks/acoustics/r130d_vanishing_viscosity_q0_evidence_2026-10-10.json').read_text(encoding='utf8'))
    hp=json.loads((ROOT/'benchmarks/acoustics/r130d_independent_hp_impulse_evidence_2026-10-10.json').read_text(encoding='utf8'))
    arm=next(r for r in evidence['arms'] if r['kappa']==1.)
    case=next(r for r in arm['cases'] if r['ppw']==args.ppw)
    path=args.cache_root/f'ppw{args.ppw}.npz'
    if hashlib.sha256(path.read_bytes()).hexdigest()!=case['cache_sha256']:raise ValueError('complete original-point eigensystem SHA changed')
    with np.load(path) as d:
        lam,cp,dt,nt=d['lam'],d['coupling'],float(d['dt']),int(d['nt'])
        h=viscous_original_q0_transfer(lam,cp,dt,nt,case['native_h_m'])
        t,phi,pressure=viscous_original_q0_trace(lam,cp,dt,nt,case['native_h_m'])
        points={'source_xyz_m':d['source'].tolist(),'source_weights':d['sw'].tolist(),'receiver_xyz_m':d['receiver'].tolist(),'receiver_weights':d['rw'].tolist()}
    q=np.zeros(nt);q[0]=1.
    sampled=np.exp(2j*np.pi*np.array([40.,80.])[:,None]*t)@pressure
    np.testing.assert_allclose(sampled,h,rtol=2e-8,atol=2e-6)
    expected=np.asarray(case['signed_40_80']);np.testing.assert_allclose(h,expected[:,0]+1j*expected[:,1],rtol=1e-11,atol=1e-10)
    summary={'profile':'R130D_INSTANTANEOUS_VANISHING_VISCOSITY_V1','qualification':hp['qualification'],
        'legacy_pinned_pffdtd':'SELF_CONVERGENCE_FAILED','physical_undamped_full_record_limit':'NOT_ESTABLISHED',
        'measured_room':'NOT_VALIDATED','source':'q[0]=1; every later q sample zero; exact velocity jump c^2*dt*M^-1*b',
        'all_modes':len(lam),'mode_cutoff':False,'source_smoothing':False,'record_taper':False,
        'observer':'rho*np.gradient(phi,dt,edge_order=2); original native samples; signed exp(+iOmega*t) rectangular DTFT / q DTFT',
        'ppw':args.ppw,'dt_s':dt,'Nt':nt,'last_sample_s':float(t[-1]),'h_m':case['native_h_m'],
        'numerical_stabilization':{'kappa':1.,'nu_h_s3':case['nu_h'],'equation':'phi_tt+nu_h*L^2*phi_t+L*phi=c^2*b*q','vanishes_as':'h^3 at each fixed physical mode'},
        'cache_sha256':case['cache_sha256'],'signed_40_80':[[float(z.real),float(z.imag)] for z in h],**points}
    args.output_dir.mkdir(parents=True,exist_ok=True)
    (args.output_dir/'summary.json').write_text(json.dumps(summary,indent=2,allow_nan=False)+'\n',encoding='utf8')
    np.savetxt(args.output_dir/'waveform.csv',np.column_stack((t,q,phi,pressure)),delimiter=',',header='time_s,q_m3_s,velocity_potential,pressure_Pa',comments='')
    np.savetxt(args.output_dir/'transfer.csv',np.column_stack(([40.,80.],h.real,h.imag,abs(h),np.angle(h,deg=True))),delimiter=',',header='frequency_Hz,real_Pa_s_m3,imag_Pa_s_m3,magnitude_Pa_s_m3,phase_deg',comments='')
    print(json.dumps({'qualification':summary['qualification'],'output_dir':str(args.output_dir.resolve()),'physical_undamped_limit':summary['physical_undamped_full_record_limit']}))

if __name__=='__main__':main()
