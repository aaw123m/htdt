"""Registered source/clock factorial and unfitted point-field endpoint diagnostic."""
from pathlib import Path
import argparse,hashlib,json,sys
import numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'backend/src'))
from htdt.r130d_native_clock_evidence import registered_plan,PLAN_SHA
from htdt.r130d_enriched_evidence import _pinned_json,_scores
from htdt.r130d_impulse_clock_diagnostic import free_q0_trace,late_point_green
from htdt.r130d_conservative_impulse import original_pressure_endpoint_audit

def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def pairs(H):return [[float(z.real),float(z.imag)] for z in H]
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args();plan=registered_plan()
    for path,pin in plan['source_sha256_lf'].items():
        if hashlib.sha256((ROOT/path).read_bytes().replace(b'\r\n',b'\n')).hexdigest()!=pin:raise ValueError('registered source changed')
    if args.output_dir.exists():raise ValueError('choose a new clock diagnostic directory')
    args.output_dir.mkdir(parents=True)
    cloud=_pinned_json(ROOT/'backend/src/htdt/r130d_contract/undamped_cloud.json',plan['contract_sha256']);points=np.array(cloud['points'])
    natives={c['ppw']:c for c in plan['native_cases']};base=natives[44]
    result={'schema_version':1,'plan_sha256_lf':PLAN_SHA,'product_go':False,'physical_limit':'NOT_ESTABLISHED',
        'diagnostic_only':True,'free_space_not_closed_room':True,'source_clock_factorial':[],
        'refined_native44_cloud':[],'refined_single_distance':[],'status':'INCOMPLETE'}
    def save():(args.output_dir/'evidence.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf8')
    def record(name,native,dt,nt,source_xyz=None,receiver_xyz=None,sw=None,rw=None):
        s=points[native['source_cloud_indices']] if source_xyz is None else source_xyz
        r=points[native['receiver_cloud_indices']] if receiver_xyz is None else receiver_xyz
        sw=native['sw'] if sw is None else sw;rw=native['rw'] if rw is None else rw
        t,phi,p,H=free_q0_trace(s,r,sw,rw,dt,nt);q=np.zeros(nt);q[0]=1.
        path=args.output_dir/(name+'.csv')
        np.savetxt(path,np.column_stack((t,q,phi,p)),delimiter=',',header='time_s,q_m3_s,velocity_potential,pressure_Pa',comments='')
        return phi,{'file':path.name,'dt':dt,'Nt':nt,'impulse_area_s':dt,'waveform_sha256':sha(path),
            'signed_40_80':pairs(H),'endpoint':original_pressure_endpoint_audit(phi,dt)}
    save()
    for source_ppw,clock_ppw in plan['factorial_execution_order']:
        native=natives[source_ppw];clock=natives[clock_ppw]
        phi,row=record(f'source{source_ppw}-clock{clock_ppw}',native,clock['dt'],clock['Nt'])
        row.update({'source_ppw':source_ppw,'clock_ppw':clock_ppw})
        result['source_clock_factorial'].append(row);save()
    s=np.zeros((8,3));r=np.tile([plan['single_distance_m'],0.,0.],(8,1));weights=np.ones(8)/8
    for m in plan['clock_refinement_factors']:
        dt=base['dt']/m;nt=(base['Nt']-1)*m+1
        phi,row=record(f'native44-clock-x{m}',base,dt,nt)
        row['clock_refinement_factor']=m;result['refined_native44_cloud'].append(row)
        phi,row=record(f'single-r1-clock-x{m}',base,dt,nt,s,r,weights,weights)
        indices=np.arange(nt-plan['asymptotic_last_sample_count'],nt)
        approximation,envelope=late_point_green(plan['single_distance_m'],indices,dt)
        error=float(np.linalg.norm(phi[indices]-approximation)/np.linalg.norm(phi[indices]))
        path=args.output_dir/f'single-r1-asym-x{m}.csv'
        np.savetxt(path,np.column_stack((indices,indices*dt,phi[indices],approximation,envelope)),delimiter=',',
            header='native_index,time_s,exact_phi,unfitted_asymptotic_phi,unfitted_envelope',comments='')
        predicted_phi=np.zeros(nt);predicted_phi[indices]=approximation
        predicted_endpoints=original_pressure_endpoint_audit(predicted_phi,dt)
        row.update({'clock_refinement_factor':m,'asymptotic_rms_relative_error':error,
            'asymptotic_crosscheck_pass':error<=plan['asymptotic_rms_error_limit'],
            'asymptotic_window_file':path.name,'asymptotic_window_sha256':sha(path),
            'late_potential_rms':float(np.linalg.norm(phi[indices])/np.sqrt(len(indices))),
            'unfitted_envelope_last':float(envelope[-1]),
            'predicted_final_stencils':[x['final_stencils'] for x in predicted_endpoints['frequencies']]})
        result['refined_single_distance'].append(row);save();print('CLOCK',m,'asymptotic_error',error,'H',row['signed_40_80'],flush=True)
    def values(row):a=np.array(row['signed_40_80']);return a[:,0]+1j*a[:,1]
    factorial={(r['source_ppw'],r['clock_ppw']):values(r) for r in result['source_clock_factorial']}
    result['source_only_fixed_clock44_pairs']=[_scores(factorial[a,44],factorial[b,44]) for a,b in zip(plan['clock_ppw'],plan['clock_ppw'][1:])]
    result['clock_only_fixed_source44_pairs']=[_scores(factorial[44,a],factorial[44,b]) for a,b in zip(plan['clock_ppw'],plan['clock_ppw'][1:])]
    result['original_diagonal_free_pairs']=[_scores(factorial[a,a],factorial[b,b]) for a,b in zip(plan['clock_ppw'],plan['clock_ppw'][1:])]
    for key in ('refined_native44_cloud','refined_single_distance'):
        result[key+'_pairs']=[_scores(values(a),values(b)) for a,b in zip(result[key],result[key][1:])]
    result['all_unfitted_asymptotic_crosschecks_pass']=all(r['asymptotic_crosscheck_pass'] for r in result['refined_single_distance'])
    result['status']='COMPLETE';result['qualification']='ANALYTIC_DIAGNOSTICS_ONLY';save()
    print('COMPLETE',len(result['source_clock_factorial']),len(result['refined_single_distance']),flush=True)
    return 0 if result['all_unfitted_asymptotic_crosschecks_pass'] else 6
if __name__=='__main__':raise SystemExit(main())
