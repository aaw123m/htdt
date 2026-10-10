"""Pinned full-DOF spatial refinement with the original native44 observer fixed."""
from pathlib import Path
import argparse,gzip,hashlib,json,subprocess,sys,time
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend/src'))
from htdt.r130d_mfem_binary import load_mfem_binary,cloud_functional
from htdt.r130d_sparse_undamped import sparse_undamped_q0

PLAN='benchmarks/acoustics/r130d_sparse_spatial_reference_plan_2026-10-10.json'
PLAN_SHA='a784a22f9c10cd5becbbabe2e313b8cf2a9924a7d15a2524c65ab2387c602553'
def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def complex_values(a):
    a=np.asarray(a,float);return a[:,0]+1j*a[:,1]
def metrics(a,b):
    a,b=complex_values(a),complex_values(b)
    if np.any(abs(b)==0):raise ValueError('zero reference; no frequency mask allowed')
    return list(map(float,(np.linalg.norm(a-b)/np.linalg.norm(b),
        max(abs(abs(a)-abs(b))/abs(b)),max(abs(np.angle(a*np.conj(b),deg=True))))))
def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--exporter',type=Path,required=True)
    parser.add_argument('--points',type=Path,required=True)
    parser.add_argument('--exports',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    raw=(ROOT/PLAN).read_bytes().replace(b'\r\n',b'\n')
    if hashlib.sha256(raw).hexdigest()!=PLAN_SHA:raise ValueError('frozen refinement plan changed')
    plan=json.loads(raw)
    if sha(args.exporter)!=plan['exporter_executable_sha256'] or sha(args.points)!=plan['point_cloud_sha256']:
        raise ValueError('pinned build or original point cloud changed')
    source=ROOT/'benchmarks/acoustics/r130d_mfem_reference/sloped_tet_point_cloud.cpp'
    if hashlib.sha256(source.read_bytes().replace(b'\r\n',b'\n')).hexdigest()!=plan['exporter_source_sha256_lf']:
        raise ValueError('native exporter changed')
    cloud=json.loads((ROOT/'backend/src/htdt/r130d_contract/undamped_cloud.json').read_text())
    if args.output_dir.exists():raise ValueError('use a new evidence directory')
    args.exports.mkdir(parents=True,exist_ok=True);args.output_dir.mkdir(parents=True)
    def export(order,refinement):
        path=args.exports/f'mfem-p{order}-r{refinement}.bin';provenance=path.with_suffix('.provenance.json')
        if not path.exists():
            subprocess.run([str(args.exporter.resolve()),'--order',str(order),'--uniform-refinements',str(refinement),
                '--point-cloud',str(args.points.resolve()),'--binary-output',str(path.resolve())],check=True)
            proof={'sha256':sha(path),'exporter_executable_sha256':sha(args.exporter),
                'exporter_source_sha256_lf':plan['exporter_source_sha256_lf'],'point_cloud_sha256':sha(args.points)}
            provenance.write_text(json.dumps(proof,indent=2)+'\n')
        else:
            proof=json.loads(provenance.read_text())
            for key in ('exporter_executable_sha256','exporter_source_sha256_lf','point_cloud_sha256'):
                if proof[key]!=plan[key]:raise ValueError('binary provenance changed')
            if proof['sha256']!=sha(path):raise ValueError('binary payload changed')
        return load_mfem_binary(path,proof['sha256'],cloud['points'])
    # IO cross check with a previously hashed independent export, before
    # high-resolution results. Exact equality, including row/column order.
    M,K,functions,binary_proof=export(3,2)
    cache=ROOT/'scratch/independent-hp-impulse/order3.npz'
    if sha(cache)!=plan['binary_baseline_old_cache_sha256']:raise ValueError('baseline changed')
    oldraw=gzip.decompress((ROOT/'benchmarks/acoustics/r130d_mfem_independent_hp_systems/mfem-p3-r2.json.gz').read_bytes())
    with np.load(cache,allow_pickle=False) as d:
        if hashlib.sha256(oldraw).hexdigest()!=str(d['matrix_sha256']):raise ValueError('old operator changed')
    old=json.loads(oldraw)
    for matrix,name in ((M,'mass_matrix'),(K,'stiffness_c2_matrix')):
        for actual,key in ((matrix.indptr,'row_offsets'),(matrix.indices,'column_indices'),(matrix.data,'values')):
            np.testing.assert_array_equal(actual,old[name][key])
    for i,(indices,values) in enumerate(functions):
        np.testing.assert_array_equal(indices,old['point_cloud'][i]['indices'])
        np.testing.assert_array_equal(values,old['point_cloud'][i]['values'])
    del M,K,functions,old,oldraw
    control=ROOT/'benchmarks/acoustics/r130d_conservative_q0_evidence_2026-10-10.json'
    if sha(control)!=plan['sem_control_sha256']:raise ValueError('SEM control changed')
    sem=next(a for a in json.loads(control.read_text())['arms'] if a['name']=='gauss2')['cases'][-1]['signed_40_80']
    result={'schema_version':'htdt.r130d.sparse-spatial-reference-evidence-1','plan_sha256_lf':PLAN_SHA,
        'plan':plan,'binary_json_exact_reproduction':'PASS','baseline_binary':binary_proof,
        'cases':[],'fixed_native44_spatial_reference':'INCOMPLETE','original_five_grid_qualification':'SELF_CONVERGENCE_FAILED',
        'physical_limit':'NOT_ESTABLISHED','product_go':False}
    evidence=args.output_dir/'evidence.json'
    def save():evidence.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf8')
    save()
    native=plan['native_observer']
    for refinement in plan['refinements']:
        print('EXPORT',2,refinement,flush=True);M,K,functions,proof=export(2,refinement);n=proof['all_dofs']
        b=cloud_functional(functions,native['source_cloud_indices'],native['sw'],n)
        receiver=cloud_functional(functions,native['receiver_cloud_indices'],native['rw'],n)
        def progress(index,total,energy):print('STEP',refinement,index,total,'energy',energy,flush=True)
        started=time.perf_counter()
        try:
            t,phi,p,H,audit=sparse_undamped_q0(M,K,b,receiver,native['dt'],native['Nt'],linear_solver='cg',
                memory_budget_bytes=plan['memory_budget_bytes'],progress=progress)
        except (ValueError,RuntimeError) as exc:
            result['execution_failure']={'refinement':refinement,'error':str(exc)}
            result['fixed_native44_spatial_reference']='EXECUTION_FAILED';save();raise
        row={'order':2,'refinement':refinement,'all_dofs':n,'matrix':proof,'execution':audit,
            'elapsed_s':time.perf_counter()-started,'signed_40_80':[[float(z.real),float(z.imag)] for z in H]}
        q=np.zeros(len(t));q[0]=1.
        waveform=args.output_dir/f'p2-r{refinement}.csv'
        np.savetxt(waveform,np.column_stack((t,q,phi,p)),delimiter=',',header='time_s,q_m3_s,velocity_potential,pressure_Pa',comments='')
        row['waveform_sha256']=sha(waveform);result['cases'].append(row);save()
        print('LEVEL_DONE',refinement,n,row['signed_40_80'],flush=True)
        del M,K,functions,b,receiver
    rows=result['cases']
    result['spatial_pairs']=[{'coarse_r':a['refinement'],'fine_r':b['refinement'],
        'metrics_complex_mag_phase':metrics(a['signed_40_80'],b['signed_40_80'])} for a,b in zip(rows,rows[1:])]
    fine=result['spatial_pairs'][-1]['metrics_complex_mag_phase'];cross=metrics(rows[-1]['signed_40_80'],sem)
    result['sem44_cross_metrics_complex_mag_phase']=cross
    result['fixed_native44_spatial_reference']='PASS_FIXED_NATIVE44_ONLY' if (
        np.all(np.asarray(fine)<=plan['primary_fine_pair_limits']) and np.all(np.asarray(cross)<=plan['sem44_cross_limits'])) else 'FAIL_FIXED_NATIVE44_SPATIAL_REFERENCE'
    save();print(result['fixed_native44_spatial_reference'],fine,cross,flush=True)

if __name__=='__main__':main()
