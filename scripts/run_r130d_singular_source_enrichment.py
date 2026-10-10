"""Registered inviscid singular-source enrichment study; never grants product GO."""
from pathlib import Path
import argparse,hashlib,json,subprocess,sys,time,gc
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend/src'))
from htdt.r130d_mfem_binary import load_mfem_binary,cloud_functional
from htdt.r130d_boundary_enrichment import load_boundary_cloud,enriched_q0_trace

PLAN='benchmarks/acoustics/r130d_singular_source_enrichment_plan_2026-10-10.json'
PLAN_SHA='44a76408e5dad61df546709c0e833f82e0dbb6a81c15064a6dc24fb2fa6bcee4'
def sha(p):
    with p.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def metrics(a,b):
    a=np.array(a);b=np.array(b);a=a[:,0]+1j*a[:,1];b=b[:,0]+1j*b[:,1]
    if np.any(abs(b)==0):raise ValueError('zero reference; frequency masking prohibited')
    return [float(np.linalg.norm(a-b)/np.linalg.norm(b)),float(max(abs(abs(a)-abs(b))/abs(b))),float(max(abs(np.angle(a*np.conj(b),deg=True))))]
def main():
    parser=argparse.ArgumentParser()
    for name in ('point-exporter','boundary-exporter','points','exports','output-dir'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args();raw=(ROOT/PLAN).read_bytes().replace(b'\r\n',b'\n')
    if hashlib.sha256(raw).hexdigest()!=PLAN_SHA:raise ValueError('registered enrichment plan changed')
    plan=json.loads(raw)
    for name in ('point_exporter','boundary_exporter','points'):
        if sha(getattr(args,name))!=plan[name+'_sha256']:raise ValueError('pinned executable or point cloud changed')
    for path,pin in plan['source_sha256_lf'].items():
        if hashlib.sha256((ROOT/path).read_bytes().replace(b'\r\n',b'\n')).hexdigest()!=pin:raise ValueError('registered source changed: '+path)
    contract=ROOT/'backend/src/htdt/r130d_contract/undamped_cloud.json'
    if sha(contract)!=plan['contract_sha256']:raise ValueError('original native observers changed')
    cloud=json.loads(contract.read_text());points=np.array(cloud['points'])
    if args.output_dir.exists():raise ValueError('use a new evidence directory')
    args.exports.mkdir(parents=True,exist_ok=True);args.output_dir.mkdir(parents=True)
    result={'schema_version':'htdt.r130d.singular-source-enrichment-evidence-1','plan_sha256_lf':PLAN_SHA,'plan':plan,
        'cases':[],'enriched_numerical_qualification':'INCOMPLETE','archived_fdtd':'SELF_CONVERGENCE_FAILED',
        'physical_limit':'NOT_ESTABLISHED','product_go':False}
    def save():(args.output_dir/'evidence.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf8')
    save()
    def matrices(r):
        path=args.exports/f'mfem-p2-r{r}.bin';proof=json.loads(path.with_suffix('.provenance.json').read_text())
        if proof['exporter_executable_sha256']!=plan['point_exporter_sha256'] or proof['point_cloud_sha256']!=plan['points_sha256']:
            raise ValueError('matrix provenance changed')
        return load_mfem_binary(path,proof['sha256'],points)
    def boundary(r,q,n):
        path=args.exports/f'boundary-p2-r{r}-q{q}.bin';provenance=path.with_suffix('.provenance.json')
        if not path.exists():
            subprocess.run([str(args.boundary_exporter.resolve()),'--order','2','--uniform-refinements',str(r),'--quadrature-order',str(q),'--output',str(path.resolve())],check=True)
        pin=sha(path)
        if provenance.exists():
            previous=json.loads(provenance.read_text())
            if previous!={'sha256':pin,'boundary_exporter_sha256':plan['boundary_exporter_sha256']}:raise ValueError('boundary provenance changed')
        else:provenance.write_text(json.dumps({'sha256':pin,'boundary_exporter_sha256':plan['boundary_exporter_sha256']},indent=2)+'\n')
        return load_boundary_cloud(path,pin,n,2,r)
    for ppw,r,qorder in plan['execution_order']:
        native=next(c for c in plan['native_cases'] if c['ppw']==ppw)
        print('START',ppw,r,qorder,flush=True);M,K,functions,operator=matrices(r);n=operator['all_dofs']
        xyz,normals,B,bproof=boundary(r,qorder,n)
        receiver=cloud_functional(functions,native['receiver_cloud_indices'],native['rw'],n)
        source_xyz=points[native['source_cloud_indices']];receiver_xyz=points[native['receiver_cloud_indices']]
        def progress(index,total,defect):print('STEP',ppw,r,qorder,index,total,'energy_work',defect,flush=True)
        started=time.perf_counter()
        try:
            t,phi,p,correction,H,audit=enriched_q0_trace(M,K,receiver,native,source_xyz,receiver_xyz,native['sw'],native['rw'],
                xyz,normals,B,progress=progress,memory_budget_bytes=plan['memory_budget_bytes'])
        except (ValueError,RuntimeError) as exc:
            result['execution_failure']={'ppw':ppw,'refinement':r,'quadrature':qorder,'error':str(exc)}
            result['enriched_numerical_qualification']='EXECUTION_FAILED';save();raise
        waveform=args.output_dir/f'ppw{ppw}-p2-r{r}-q{qorder}.csv';source=np.zeros(len(t));source[0]=1.
        np.savetxt(waveform,np.column_stack((t,source,phi,p,correction)),delimiter=',',
            header='time_s,q_m3_s,velocity_potential,pressure_Pa,regular_boundary_correction',comments='')
        row={'ppw':ppw,'refinement':r,'quadrature_order':qorder,'operator':operator,'boundary':bproof,'execution':audit,
            'elapsed_s':time.perf_counter()-started,'signed_40_80':[[float(z.real),float(z.imag)] for z in H],
            'waveform_sha256':sha(waveform)}
        result['cases'].append(row);save();print('DONE',ppw,r,qorder,row['signed_40_80'],flush=True)
        del M,K,functions,B,receiver,xyz,normals;gc.collect()
    def row(ppw,r,q):return next(x['signed_40_80'] for x in result['cases'] if (x['ppw'],x['refinement'],x['quadrature_order'])==(ppw,r,q))
    result['fixed_native44_spatial_pairs']=[metrics(row(44,r,12),row(44,r+1,12)) for r in (2,3)]
    result['boundary_quadrature_metrics']=metrics(row(44,4,12),row(44,4,16))
    cases=[row(ppw,3,12) for ppw in (28,32,36,40,44)]
    pairs=[metrics(a,b) for a,b in zip(cases,cases[1:])];result['native_five_case_pairs_fixed_mesh']=pairs
    result['strict_all_three_decrease']=bool(np.all(np.diff(np.array(pairs),axis=0)<0))
    result['native_pairs_within_original_caps']=bool(np.all(np.array(pairs)<=plan['original_pair_limits']))
    result['spatial_fine_gate']=bool(np.all(np.array(result['fixed_native44_spatial_pairs'][-1])<=plan['spatial_fine_limits']))
    result['boundary_quadrature_gate']=bool(np.all(np.array(result['boundary_quadrature_metrics'])<=plan['quadrature_limits']))
    result['enriched_numerical_qualification']='PASS_DIAGNOSTICS_ONLY' if all(result[k] for k in (
        'strict_all_three_decrease','native_pairs_within_original_caps','spatial_fine_gate','boundary_quadrature_gate')) else 'FAIL_SINGULAR_SOURCE_ENRICHMENT'
    save();print(result['enriched_numerical_qualification'],pairs,result['fixed_native44_spatial_pairs'],result['boundary_quadrature_metrics'],flush=True)
if __name__=='__main__':main()
