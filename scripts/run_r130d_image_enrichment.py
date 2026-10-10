"""Registered first-reflection source enrichment and full-boundary correction."""
from pathlib import Path
import argparse,hashlib,json,sys,time,gc
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend/src'))
from htdt.r130d_mfem_binary import load_mfem_binary,cloud_functional
from htdt.r130d_boundary_enrichment import load_boundary_cloud
from htdt.r130d_image_enrichment import enriched_image_q0_trace,PLANES
from htdt.r130d_enriched_evidence import _scores
PLAN='benchmarks/acoustics/r130d_image_enrichment_plan_2026-10-10.json'
PLAN_SHA='e2d1fa693731f60cf6f7faa7828d7d77375d1c804adbfe7e75d4c555cee1d427'
def sha(p):
    with p.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def values(a):
    a=np.array(a);return a[:,0]+1j*a[:,1]
def main():
    parser=argparse.ArgumentParser()
    for name in ('exports','hp-evidence','output-dir'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args();raw=(ROOT/PLAN).read_bytes().replace(b'\r\n',b'\n')
    if hashlib.sha256(raw).hexdigest()!=PLAN_SHA:raise ValueError('registered image plan changed')
    plan=json.loads(raw);hp=json.loads(args.hp_evidence.read_text())
    if hp['plan_sha256_lf']!=plan['direct_only_hp_plan_sha256_lf'] or hp['hp_enriched_qualification']=='INCOMPLETE':
        raise ValueError('complete prospectively registered direct-only HP control required')
    for path,pin in plan['source_sha256_lf'].items():
        if hashlib.sha256((ROOT/path).read_bytes().replace(b'\r\n',b'\n')).hexdigest()!=pin:raise ValueError('registered image source changed')
    if not np.array_equal(PLANES,plan['planes_unit_normal_and_offset']):raise ValueError('physical mirror planes changed')
    contract=ROOT/'backend/src/htdt/r130d_contract/undamped_cloud.json'
    if sha(contract)!=plan['contract_sha256']:raise ValueError('original native source/receiver changed')
    points=np.array(json.loads(contract.read_text())['points'])
    if args.output_dir.exists():raise ValueError('use a new evidence directory')
    args.output_dir.mkdir(parents=True)
    result={'schema_version':'htdt.r130d.image-enrichment-evidence-1','plan_sha256_lf':PLAN_SHA,'plan':plan,
        'direct_only_hp_evidence_sha256':sha(args.hp_evidence),'cases':[],
        'image_enriched_qualification':'INCOMPLETE','archived_fdtd':'SELF_CONVERGENCE_FAILED',
        'physical_limit':'NOT_ESTABLISHED','product_go':False}
    def save():(args.output_dir/'evidence.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf8')
    save()
    def row(ppw,o,r,q):return values(next(c['signed_40_80'] for c in result['cases'] if (c['ppw'],c['order'],c['refinement'],c['quadrature_order'])==(ppw,o,r,q)))
    def spatial_gates():
        fine=_scores(row(44,3,3,16),row(44,4,3,16));quad=_scores(row(44,4,3,16),row(44,4,3,20))
        control=next(c for c in hp['cases'] if (c['ppw'],c['order'],c['refinement'],c['quadrature_order'])==(44,4,3,16))
        cross=_scores(row(44,4,3,16),values(control['signed_40_80']))
        return fine,quad,cross,{
            'hp_fine_gate':bool(np.all(np.array(fine)<=plan['spatial_fine_limits'])),
            'quadrature_gate':bool(np.all(np.array(quad)<=plan['quadrature_limits'])),
            'direct_only_hp_cross_gate':bool(np.all(np.array(cross)<=plan['hp_direct_only_cross_limits']))}
    for ppw,order,r,qorder in plan['execution_order']:
        if ppw!=44:
            fine,quad,cross,gates=spatial_gates()
            if not all(gates.values()):
                result['native_clock_phase']='NOT_RUN_SPATIAL_GATE_FAILED'
                result['skipped_native_cases']=[c for c in plan['execution_order'] if c[0]!=44]
                break
        native=next(c for c in plan['native_cases'] if c['ppw']==ppw)
        print('START',ppw,order,r,qorder,flush=True)
        name=f'mfem-p{order}-r{r}.bin';M,K,functions,operator=load_mfem_binary(args.exports/name,plan['inputs_sha256'][name],points)
        name=f'boundary-p{order}-r{r}-q{qorder}.bin';n=operator['all_dofs']
        xyz,normals,B,boundary=load_boundary_cloud(args.exports/name,plan['inputs_sha256'][name],n,order,r)
        receiver=cloud_functional(functions,native['receiver_cloud_indices'],native['rw'],n)
        def progress(index,total,defect):print('STEP',ppw,order,r,qorder,index,total,'energy_work',defect,flush=True)
        started=time.perf_counter()
        try:
            t,phi,p,w,H,audit=enriched_image_q0_trace(M,K,receiver,native,points[native['source_cloud_indices']],
                points[native['receiver_cloud_indices']],native['sw'],native['rw'],xyz,normals,B,
                progress=progress,memory_budget_bytes=plan['memory_budget_bytes'])
        except (ValueError,RuntimeError) as exc:
            result['execution_failure']={'ppw':ppw,'order':order,'refinement':r,'quadrature':qorder,'error':str(exc)}
            result['image_enriched_qualification']='EXECUTION_FAILED';save();raise
        waveform=args.output_dir/f'ppw{ppw}-p{order}-r{r}-q{qorder}.csv';source=np.zeros(len(t));source[0]=1.
        np.savetxt(waveform,np.column_stack((t,source,phi,p,w)),delimiter=',',
            header='time_s,q_m3_s,velocity_potential,pressure_Pa,regular_boundary_correction',comments='')
        data={'ppw':ppw,'order':order,'refinement':r,'quadrature_order':qorder,'operator':operator,'boundary':boundary,
            'execution':audit,'elapsed_s':time.perf_counter()-started,
            'signed_40_80':[[float(z.real),float(z.imag)] for z in H],'waveform_sha256':sha(waveform)}
        result['cases'].append(data);save();print('DONE',ppw,order,r,qorder,data['signed_40_80'],flush=True)
        del M,K,functions,B,receiver,xyz,normals;gc.collect()
    fine,quad,cross,gates=spatial_gates()
    result.update({'hp_fine_metrics':fine,'quadrature_metrics':quad,'direct_only_hp_cross_metrics':cross,'gates':gates})
    result['spatial_pairs']=[_scores(row(44,2,2,16),row(44,2,3,16)),_scores(row(44,2,3,16),row(44,3,3,16)),fine]
    matches=[]
    for data in result['cases']:
        key=tuple(data[k] for k in ('ppw','order','refinement','quadrature_order'))
        control=next((c for c in hp['cases'] if tuple(c[k] for k in ('ppw','order','refinement','quadrature_order'))==key),None)
        if control:matches.append({'case':list(key),'metrics':_scores(values(data['signed_40_80']),values(control['signed_40_80']))})
    result['all_matching_direct_only_hp_cross_controls']=matches
    if 'native_clock_phase' not in result:
        native=[row(ppw,4,3,16) for ppw in (28,32,36,40,44)];pairs=[_scores(a,b) for a,b in zip(native,native[1:])]
        result['native_clock_phase']='EXECUTED';result['native_five_case_pairs_fixed_mesh']=pairs
        gates.update({'native_pairs_within_original_caps':bool(np.all(np.array(pairs)<=plan['original_pair_limits'])),
            'strict_all_three_decrease':bool(np.all(np.diff(np.array(pairs),axis=0)<0))})
    result['image_enriched_qualification']='PASS_DIAGNOSTICS_ONLY' if all(gates.values()) and result['native_clock_phase']=='EXECUTED' else 'FAIL_IMAGE_SINGULAR_SOURCE_ENRICHMENT'
    save();print(result['image_enriched_qualification'],gates,fine,quad,cross,flush=True)
    return 6 if result['image_enriched_qualification'].startswith('FAIL') else 0
if __name__=='__main__':raise SystemExit(main())
