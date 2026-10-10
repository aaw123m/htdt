"""Prospectively registered remaining P3/native controls; all outcomes retained."""
from pathlib import Path
import argparse,hashlib,json,sys,time
import numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'backend/src'))
from htdt.r130d_native_clock_evidence import registered_plan,PLAN_SHA,native_metrics
from htdt.r130d_mfem_binary import load_mfem_binary,cloud_functional
from htdt.r130d_boundary_enrichment import load_boundary_cloud,enriched_q0_trace

def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--exports',type=Path,required=True);parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args();plan=registered_plan()
    for path,pin in plan['source_sha256_lf'].items():
        if hashlib.sha256((ROOT/path).read_bytes().replace(b'\r\n',b'\n')).hexdigest()!=pin:raise ValueError('registered source changed')
    if args.output_dir.exists():raise ValueError('choose a new output directory')
    args.output_dir.mkdir(parents=True)
    cloud=ROOT/'backend/src/htdt/r130d_contract/undamped_cloud.json'
    if sha(cloud)!=plan['contract_sha256']:raise ValueError('original point cloud changed')
    points=np.array(json.loads(cloud.read_text())['points'])
    name='mfem-p3-r3.bin';M,K,functions,operator=load_mfem_binary(args.exports/name,plan['matrices'][name]['sha256'],points)
    name='boundary-p3-r3-q16.bin';xyz,normals,B,boundary=load_boundary_cloud(args.exports/name,plan['boundaries'][name]['sha256'],15625,3,3)
    result={'schema_version':1,'plan_sha256_lf':PLAN_SHA,'cases':[],'qualification':'INCOMPLETE','product_go':False}
    def save():(args.output_dir/'evidence.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf8')
    save()
    for ppw,order,r,qorder in plan['native_extension_execution_order']:
        native=next(c for c in plan['native_cases'] if c['ppw']==ppw)
        recv=cloud_functional(functions,native['receiver_cloud_indices'],native['rw'],15625)
        def progress(i,n,d):print('STEP',ppw,i,n,'energy_work',d,flush=True)
        print('START',ppw,flush=True);start=time.perf_counter()
        t,phi,p,w,H,audit=enriched_q0_trace(M,K,recv,native,points[native['source_cloud_indices']],points[native['receiver_cloud_indices']],
            native['sw'],native['rw'],xyz,normals,B,memory_budget_bytes=plan['memory_budget_bytes'],progress=progress)
        source=np.zeros(len(t));source[0]=1.;path=args.output_dir/f'ppw{ppw}-p3-r3-q16.csv'
        np.savetxt(path,np.column_stack((t,source,phi,p,w)),delimiter=',',header='time_s,q_m3_s,velocity_potential,pressure_Pa,regular_boundary_correction',comments='')
        row={'ppw':ppw,'order':order,'refinement':r,'quadrature_order':qorder,'operator':operator,'boundary':boundary,
            'execution':audit,'elapsed_s':time.perf_counter()-start,'waveform_sha256':sha(path),'signed_40_80':[[z.real,z.imag] for z in H]}
        result['cases'].append(row);save();print('DONE',ppw,row['signed_40_80'],flush=True)
    result.update(native_metrics(plan,result['cases']));save();print(result['qualification'],result['native_spatial_metrics'],flush=True)
    return 0 if result['all_native_spatial_gates_pass'] else 6
if __name__=='__main__':raise SystemExit(main())
