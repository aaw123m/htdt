"""Registered high-order inviscid enrichment study. FAIL is retained explicitly."""
from pathlib import Path
import argparse,hashlib,json,sys,time,gc
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend/src'))
from htdt.r130d_mfem_binary import load_mfem_binary,cloud_functional
from htdt.r130d_boundary_enrichment import load_boundary_cloud,enriched_q0_trace
from htdt.r130d_enriched_evidence import _scores

PLAN='benchmarks/acoustics/r130d_singular_source_hp_plan_2026-10-10.json'
PLAN_SHA='bd3399c26da9b79e247cd30cf49204993d8e052d8ffc4dd08c209832114c7387'
def sha(p):
    with p.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def values(a):
    a=np.array(a);return a[:,0]+1j*a[:,1]
def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--exports',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args();raw=(ROOT/PLAN).read_bytes().replace(b'\r\n',b'\n')
    if hashlib.sha256(raw).hexdigest()!=PLAN_SHA:raise ValueError('registered hp plan changed')
    plan=json.loads(raw)
    for path,pin in plan['source_sha256_lf'].items():
        if hashlib.sha256((ROOT/path).read_bytes().replace(b'\r\n',b'\n')).hexdigest()!=pin:raise ValueError('registered source changed: '+path)
    oldpath=ROOT/'benchmarks/acoustics/r130d_singular_source_enrichment_evidence_2026-10-10.json'
    if hashlib.sha256(oldpath.read_bytes().replace(b'\r\n',b'\n')).hexdigest()!=plan['p2r4_evidence_sha256_lf']:raise ValueError('cross control changed')
    old=json.loads(oldpath.read_text());cross=next(c for c in old['cases'] if (c['ppw'],c['refinement'],c['quadrature_order'])==(44,4,16))
    contract=ROOT/'backend/src/htdt/r130d_contract/undamped_cloud.json'
    if sha(contract)!=plan['contract_sha256']:raise ValueError('native cloud changed')
    points=np.array(json.loads(contract.read_text())['points'])
    if args.output_dir.exists():raise ValueError('use a new evidence directory')
    args.output_dir.mkdir(parents=True)
    result={'schema_version':'htdt.r130d.singular-source-hp-evidence-1','plan_sha256_lf':PLAN_SHA,'plan':plan,
        'cases':[],'hp_enriched_qualification':'INCOMPLETE','archived_fdtd':'SELF_CONVERGENCE_FAILED',
        'physical_limit':'NOT_ESTABLISHED','product_go':False}
    def save():(args.output_dir/'evidence.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf8')
    save()
    for ppw,order,r,qorder in plan['execution_order']:
        native=next(c for c in plan['native_cases'] if c['ppw']==ppw)
        print('START',ppw,order,r,qorder,flush=True)
        name=f'mfem-p{order}-r{r}.bin';M,K,functions,operator=load_mfem_binary(args.exports/name,plan['matrices'][name]['sha256'],points)
        name=f'boundary-p{order}-r{r}-q{qorder}.bin';n=operator['all_dofs']
        xyz,normals,B,boundary=load_boundary_cloud(args.exports/name,plan['boundaries'][name]['sha256'],n,order,r)
        receiver=cloud_functional(functions,native['receiver_cloud_indices'],native['rw'],n)
        def progress(index,total,defect):print('STEP',ppw,order,r,qorder,index,total,'energy_work',defect,flush=True)
        started=time.perf_counter()
        try:
            t,phi,p,w,H,audit=enriched_q0_trace(M,K,receiver,native,points[native['source_cloud_indices']],
                points[native['receiver_cloud_indices']],native['sw'],native['rw'],xyz,normals,B,
                progress=progress,memory_budget_bytes=plan['memory_budget_bytes'])
        except (ValueError,RuntimeError) as exc:
            result['execution_failure']={'ppw':ppw,'order':order,'refinement':r,'quadrature':qorder,'error':str(exc)}
            result['hp_enriched_qualification']='EXECUTION_FAILED';save();raise
        waveform=args.output_dir/f'ppw{ppw}-p{order}-r{r}-q{qorder}.csv';source=np.zeros(len(t));source[0]=1.
        np.savetxt(waveform,np.column_stack((t,source,phi,p,w)),delimiter=',',
            header='time_s,q_m3_s,velocity_potential,pressure_Pa,regular_boundary_correction',comments='')
        row={'ppw':ppw,'order':order,'refinement':r,'quadrature_order':qorder,'operator':operator,'boundary':boundary,
            'execution':audit,'elapsed_s':time.perf_counter()-started,
            'signed_40_80':[[float(z.real),float(z.imag)] for z in H],'waveform_sha256':sha(waveform)}
        result['cases'].append(row);save();print('DONE',ppw,order,r,qorder,row['signed_40_80'],flush=True)
        del M,K,functions,B,receiver,xyz,normals;gc.collect()
    def row(ppw,o,q):return values(next(c['signed_40_80'] for c in result['cases'] if (c['ppw'],c['order'],c['quadrature_order'])==(ppw,o,q)))
    spatial=[_scores(row(44,o,16),row(44,o+1,16)) for o in (2,3)]
    quadrature=_scores(row(44,4,16),row(44,4,20));crossmetrics=_scores(row(44,4,16),values(cross['signed_40_80']))
    native=[row(ppw,4,16) for ppw in (28,32,36,40,44)];pairs=[_scores(a,b) for a,b in zip(native,native[1:])]
    gates={'hp_fine_gate':bool(np.all(np.array(spatial[-1])<=plan['spatial_fine_limits'])),
        'quadrature_gate':bool(np.all(np.array(quadrature)<=plan['quadrature_limits'])),
        'p2r4_cross_gate':bool(np.all(np.array(crossmetrics)<=plan['cross_p2r4_limits'])),
        'native_pairs_within_original_caps':bool(np.all(np.array(pairs)<=plan['original_pair_limits'])),
        'strict_all_three_decrease':bool(np.all(np.diff(np.array(pairs),axis=0)<0))}
    result.update({'fixed_native44_hp_pairs':spatial,'quadrature_metrics':quadrature,'p2r4_cross_metrics':crossmetrics,
        'native_five_case_pairs_fixed_mesh':pairs,'gates':gates,
        'hp_enriched_qualification':'PASS_DIAGNOSTICS_ONLY' if all(gates.values()) else 'FAIL_HP_SINGULAR_SOURCE_ENRICHMENT'})
    save();print(result['hp_enriched_qualification'],gates,pairs,spatial,crossmetrics,flush=True)
    return 6 if not all(gates.values()) else 0
if __name__=='__main__':raise SystemExit(main())
