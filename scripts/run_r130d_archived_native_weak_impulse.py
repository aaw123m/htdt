"""Read-only q0 archive replay: verify old pressure score, then weak observer.

No stored waves, source samples, masks, or original acceptance statuses change.
"""
from pathlib import Path
import argparse
import hashlib
import json
import sys
import h5py
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend/src'))
from htdt.r130d_weak_impulse_observer import endpoint_window,endpoint_window_derivative
from htdt.acoustic_pffdtd_adapter import pffdtd_velocity_potential_to_pressure_trace,finite_record_pressure_transfer
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_point_quadratic_pffdtd import file_hash

def pairs(h):
    return [[float(z.real),float(z.imag)] for z in h]

def metrics(co,fi):
    return compare_complex_transfer(reference=fi,candidate=co,frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode='json')

def main():
    p=argparse.ArgumentParser();p.add_argument('--original-sims-root',required=True,type=Path);args=p.parse_args()
    original=json.loads((ROOT/'benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json').read_text(encoding='utf8'))
    modal=json.loads((ROOT/'benchmarks/acoustics/r130d_original_pffdtd_native_full_modal_q0_evidence_2026-10-09.json').read_text(encoding='utf8'))
    original={r['ppw']:r for r in original['actual_native_wave_cases']}
    # Frozen raw wave hashes are also present in the exact native modal evidence.
    def find_rows(obj):
        if isinstance(obj,dict):
            if 'ppw' in obj and 'original_native_sim_output_SHA256' in obj: yield obj
            for v in obj.values(): yield from find_rows(v)
        elif isinstance(obj,list):
            for v in obj: yield from find_rows(v)
    frozen={r['ppw']:r for r in find_rows(modal)}
    folders={}
    for f in args.original_sims_root.rglob('comms_out.h5'):
        sha=file_hash(f)
        for ppw,r in original.items():
            if sha==r['original_native_comm_sha256']:
                if ppw in folders: raise ValueError('ambiguous archive source')
                folders[ppw]=f.parent
    if set(folders)!=set(original): raise ValueError('all five original source archives required')
    result={'source':'UNCHANGED_ORIGINAL_Q0_ARCHIVED_WAVES','legacy_status':'SELF_CONVERGENCE_FAILED',
            'window_status':'DIAGNOSTIC_CHANGED_OBSERVER','cases':[],'legacy_pairs':[],'weak_pairs':[]}
    for ppw in sorted(original):
        folder=folders[ppw];ref=original[ppw]
        sha={name:file_hash(folder/name) for name in ('comms_out.h5','vox_out.h5','sim_outs.h5')}
        if sha['vox_out.h5']!=ref['original_solver_geometry_sha256'] or sha['sim_outs.h5']!=frozen[ppw]['original_native_sim_output_SHA256']:
            raise ValueError('frozen native geometry/wave hash changed')
        with h5py.File(folder/'comms_out.h5','r') as f:
            q8=f['in_sigs'][:];rw=f['out_alpha'][:].ravel();nt=int(f['Nt'][()])
            if q8.shape!=(8,nt) or np.any(q8[:,1:]!=0): raise ValueError('original q0 changed')
        with h5py.File(folder/'sim_consts.h5','r') as f:dt=float(f['Ts'][()])
        with h5py.File(folder/'sim_outs.h5','r') as f:phi=rw@f['u_out'][:]
        pressure=pffdtd_velocity_potential_to_pressure_trace(phi,time_step_s=dt,density_kg_m3=1.2)
        q=np.zeros(nt);q[0]=1.
        H=finite_record_pressure_transfer(pressure,q,time_step_s=dt,frequency_hz=np.array([40.,80.]))
        exp=np.asarray(ref['unmodified_original_transfer_pa_per_m3_s']);exp=exp[:,0]+1j*exp[:,1]
        if np.linalg.norm(H-exp)/np.linalg.norm(exp)>2e-6:raise ValueError('legacy archived score drift')
        t=np.arange(nt)*dt;T=t[-1];w=endpoint_window(t,T);dw=endpoint_window_derivative(t,T)
        weak_pressure=np.exp(2j*np.pi*np.array([40.,80.])[:,None]*t)@(pressure*w)
        # Integrate by parts before sampling: ends vanish, q impulse area=dt.
        weak_potential=[]
        for f in (40.,80.):
            omega=2*np.pi*f
            g=phi*np.exp(1j*omega*t)*(dw+1j*omega*w)
            weak_potential.append(-1.2*np.trapz(g,dx=dt)/dt)
        row={'ppw':ppw,'native_dt':dt,'Nt':nt,'original_sha256':sha,'legacy':pairs(H),
             'weak_pressure_quadrature':pairs(weak_pressure),'weak_potential_integration_by_parts':pairs(weak_potential)}
        result['cases'].append(row)
    for co,fi in zip(result['cases'],result['cases'][1:]):
        result['legacy_pairs'].append({'coarse':co['ppw'],'fine':fi['ppw'],'metrics':metrics(co['legacy'],fi['legacy'])})
        result['weak_pairs'].append({'coarse':co['ppw'],'fine':fi['ppw'],'metrics':metrics(co['weak_potential_integration_by_parts'],fi['weak_potential_integration_by_parts'])})
    dest=ROOT/'benchmarks/acoustics/r130d_archived_native_weak_impulse_evidence_2026-10-10.json'
    dest.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf8')
    for key in ('legacy_pairs','weak_pairs'):
        print(key,[r['metrics']['complex_rms_relative'] for r in result[key]])

if __name__=='__main__':main()
