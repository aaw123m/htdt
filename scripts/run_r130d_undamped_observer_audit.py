"""Signed endpoint and spectral decomposition; never changes the pressure gate."""
from pathlib import Path
import argparse,hashlib,json,sys
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend/src'))
from htdt.r130d_conservative_impulse import (conservative_q0_trace,conservative_q0_transfer,
    original_pressure_endpoint_audit)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    e=json.loads((ROOT/'benchmarks/acoustics/r130d_conservative_q0_evidence_2026-10-10.json').read_text())
    result={'schema_version':1,'original_observer_retained':True,'diagnostic_only':True,
        'all_modes_retained':True,'physical_limit':'NOT_ESTABLISHED','cases':[]}
    for arm in ('exact','gauss2'):
        frozen=next(a for a in e['arms'] if a['name']==arm)
        for ref in frozen['cases']:
            relative=f"scratch/boundary-fitted-sem/ppw{ref['ppw']}.npz";path=ROOT/relative
            if hashlib.sha256(path.read_bytes()).hexdigest()!=e['plan']['input_sha256'][relative]:
                raise ValueError('original all-mode input changed')
            with np.load(path,allow_pickle=False) as d:
                lam,cp,dt,nt=d['lam'],d['coupling'],float(d['dt']),int(d['nt']);h=ref['native_h_m']
                t,phi,p,_=conservative_q0_trace(lam,cp,dt,nt,h,arm=arm)
                audit=original_pressure_endpoint_audit(phi,dt)
                bins=[];total=np.zeros(2,dtype=complex);freq=np.sqrt(lam)/(2*np.pi)
                for lo,hi in zip((0.,100.,500.,1000.,2000.,5000.),(100.,500.,1000.,2000.,5000.,np.inf)):
                    selected=(freq>=lo)&(freq<hi);count=int(sum(selected))
                    H=conservative_q0_transfer(lam[selected],cp[selected],dt,nt,h,arm=arm)[0] if count else np.zeros(2,complex)
                    total+=H
                    bins.append({'lower_hz_inclusive':lo,'upper_hz_exclusive':hi if np.isfinite(hi) else None,
                        'modes':count,'signed_40_80':[[float(z.real),float(z.imag)] for z in H]})
            frozenH=np.asarray(ref['signed_40_80']);frozenH=frozenH[:,0]+1j*frozenH[:,1]
            sampled=np.exp(2j*np.pi*np.array([40.,80.])[:,None]*t)@p
            np.testing.assert_allclose(total,frozenH,rtol=1e-9,atol=1e-8)
            np.testing.assert_allclose(sampled,frozenH,rtol=2e-8,atol=2e-6)
            if sum(b['modes'] for b in bins)!=len(lam):raise RuntimeError('diagnostic omitted modes')
            result['cases'].append({'arm':arm,'ppw':ref['ppw'],'native_dt_s':dt,'native_Nt':nt,
                'all_modes':len(lam),'above_native_nyquist_modes':int(sum(freq>1/(2*dt))),
                'spectral_bins':bins,'endpoint_decomposition':audit})
            print(arm,ref['ppw'],'full signed record preserved',flush=True)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf8')

if __name__=='__main__':main()
