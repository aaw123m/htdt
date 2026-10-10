"""Complete independently assembled MFEM hp impulse eigenbasis; frozen gates."""
from pathlib import Path
import argparse
import gzip
import hashlib
import json
import sys
import time
import numpy as np
from scipy.linalg import eigh

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend/src'))
from htdt.r130d_vanishing_viscosity_impulse import viscous_original_q0_transfer
from htdt.r130d_weak_impulse_observer import exact_weak_impulse_transfer
from run_r130d_physical_pulse_sem import csr,pairs,scores,passes

PLAN_SHA='c6b9c42cb4cc459b8b3496c4e4b38efba6ee42f7fb82b1363aa80588ede85faf'

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def assemble_case(degree,folder,cloud,cache,refinement=2):
    path=folder/f'mfem-p{degree}-r{refinement}.json'
    doc=json.loads(path.read_bytes());n=(2**refinement*degree+1)**3
    if doc['ndofs']!=n or doc['order']!=degree or doc['uniform_refinements']!=refinement or doc['elements']!=6*8**refinement:
        raise ValueError('independent mesh changed')
    if doc['boundary_model']!='natural_neumann_rigid' or doc['sound_speed_m_s']!=343.2 or doc['base_volume_m3']!=56:
        raise ValueError('independent physical geometry changed')
    M,K=csr(doc['mass_matrix'],n),csr(doc['stiffness_c2_matrix'],n)
    if abs(M.sum()-56)>1e-8 or max(abs((K@np.ones(n))))>1e-6:
        raise ValueError('mass volume or rigid stiffness failed')
    cloud_matrix=np.zeros((82,n))
    for i,row in enumerate(doc['point_cloud']):
        np.testing.assert_allclose(row['xyz'],cloud['points'][i],rtol=0,atol=1e-14)
        cloud_matrix[i,row['indices']]=row['values']
    np.testing.assert_allclose(cloud_matrix.sum(axis=1),1.,atol=1e-10,rtol=0)
    np.testing.assert_allclose(cloud_matrix[0],doc['source_functional'],atol=1e-12,rtol=0)
    np.testing.assert_allclose(cloud_matrix[1],doc['receiver_functional'],atol=1e-12,rtol=0)
    row={'order':degree,'dofs':n,'matrix_sha256':sha(path),'volume_m3':float(M.sum()),'rigid_stiffness_max':float(max(abs(K@np.ones(n))))}
    if degree==2:
        original=json.loads(gzip.decompress((ROOT/'benchmarks/acoustics/r130d_mfem_independent_sparse_systems/mfem-r2.json.gz').read_bytes()))
        errors={key:float(np.max(abs((mat-csr(original[key],n)).data),initial=0.)) for key,mat in [('mass_matrix',M),('stiffness_c2_matrix',K)]}
        if errors['mass_matrix']>1e-12 or errors['stiffness_c2_matrix']>1e-6:raise ValueError('base MFEM exporter matrix drift')
        row['original_matrix_reproduction_max']=errors
    dest=cache/f'order{degree}.npz'
    if dest.exists():
        with np.load(dest) as d:
            if str(d['matrix_sha256'])!=sha(path):raise ValueError('cache matrix drift')
            lam,evals=d['lam'],d['point_evaluations']
            row.update(json.loads(str(d['proof'])))
    else:
        print('HP_EIGH_START',degree,n,flush=True);start=time.perf_counter()
        # Generalized QR avoids the extra dense divide-and-conquer workspace.
        A=K.toarray(order='F');B=M.toarray(order='F')
        # P5 belongs only to the supplementary prospectively registered plan.
        # Its full GVD basis retains all modes and uses more bounded workspace.
        driver='gvd' if degree>=5 else 'gv'
        lam,V=eigh(A,B,driver=driver,overwrite_a=True,overwrite_b=True,check_finite=False)
        del A,B
        if abs(lam[0])/max(lam[-1],1.)>1e-10 or lam[1]<=0:raise ValueError('invalid rigid eigenmode')
        original_zero=float(lam[0]);lam[0]=0.;V[:,0]=1./np.sqrt(M.sum())
        maximum_residual=0.;norm_error=0.;sample_gram_error=0.
        for startcol in range(0,n,64):
            sl=slice(startcol,min(startcol+64,n));v=V[:,sl];mv=M@v;kv=K@v
            r=kv-mv*lam[sl]
            # Absolute operator-scaled residual remains meaningful at lambda0.
            maximum_residual=max(maximum_residual,float(np.max(np.linalg.norm(r,axis=0)/(np.linalg.norm(v,axis=0)*max(lam[-1],1.)))))
            norm_error=max(norm_error,float(max(abs(np.sum(v*mv,axis=0)-1))))
        ix=np.unique(np.linspace(0,n-1,min(n,128),dtype=int));vv=V[:,ix]
        sample_gram_error=float(np.max(abs(vv.T@(M@vv)-np.eye(len(ix)))))
        if maximum_residual>1e-8 or norm_error>1e-8 or sample_gram_error>1e-8:raise ValueError('eigenproof failed')
        evals=cloud_matrix@V;del V
        proof={'all_modes':n,'zero_roundoff_before_exact_constant':original_zero,'max_operator_scaled_eigen_residual':maximum_residual,
            'mass_norm_max_error_all_modes':norm_error,'mass_orthogonality_128_sample_max_error':sample_gram_error,'eigh_elapsed_s':time.perf_counter()-start}
        cache.mkdir(parents=True,exist_ok=True)
        np.savez_compressed(dest,lam=lam,point_evaluations=evals,matrix_sha256=sha(path),proof=json.dumps(proof))
        row.update(proof)
    row['cache_sha256']=sha(dest);row['native_viscosity_cases']=[]
    old=json.loads((ROOT/'benchmarks/acoustics/r130d_boundary_fitted_sem_evidence_2026-10-10.json').read_text(encoding='utf8'))
    for case in cloud['cases']:
        ppw=case['ppw'];ref=next(r for r in old['cases'] if r['ppw']==ppw)
        src=np.asarray(case['sw'])@evals[case['source_cloud_indices']]
        rec=np.asarray(case['rw'])@evals[case['receiver_cloud_indices']]
        h=viscous_original_q0_transfer(lam,src*rec,case['dt'],case['Nt'],ref['native_h_m'])
        row['native_viscosity_cases'].append({'ppw':ppw,'signed_40_80':pairs(h)})
    row['physical_point_weak_signed_40_80']=pairs(exact_weak_impulse_transfer(lam,evals[0]*evals[1]))
    return row

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--exports',type=Path,required=True);parser.add_argument('--degree',type=int,choices=[2,3,4]);args=parser.parse_args()
    raw=(ROOT/'benchmarks/acoustics/r130d_independent_hp_impulse_plan_2026-10-10.json').read_bytes().replace(b'\r\n',b'\n')
    if hashlib.sha256(raw).hexdigest()!=PLAN_SHA:raise ValueError('prospective hp plan changed')
    plan=json.loads(raw);cloud=json.loads((args.exports/'cloud_provenance.json').read_text(encoding='utf8'))
    build=json.loads((args.exports/'build_provenance.json').read_text(encoding='utf-8-sig'))
    if build['mfem_pin']!=plan['mfem_pin'] or sha(args.exports/'cloud_provenance.json')!=build['cloud_provenance_sha256'] or sha(args.exports/'points.txt')!=build['point_cloud_sha256']:
        raise ValueError('independent build/cloud provenance drift')
    if cloud['exporter_sha256_lf']!='eb3a82cbdafec0a8ae4386d87b3f381b0957ad38aac233519a7825e6a0e965f4':raise ValueError('independent exporter changed')
    for case in cloud['cases']:
        if sha(ROOT/f"scratch/boundary-fitted-sem/ppw{case['ppw']}.npz")!=case['cache_sha256']:raise ValueError('original native clouds changed')
    dest=ROOT/'benchmarks/acoustics/r130d_independent_hp_impulse_evidence_2026-10-10.json'
    result=json.loads(dest.read_text(encoding='utf8')) if dest.exists() else {'plan_sha256':PLAN_SHA,'plan':plan,'cases':[],'qualification':'INCOMPLETE'}
    result['independent_build']=build
    for degree in ([args.degree] if args.degree else plan['orders']):
        row=assemble_case(degree,args.exports,cloud,ROOT/'scratch/independent-hp-impulse')
        result['cases']=[r for r in result['cases'] if r['order']!=degree]+[row];result['cases'].sort(key=lambda r:r['order'])
        dest.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf8')
        print('HP_DONE',degree,row['native_viscosity_cases'][-1],row['physical_point_weak_signed_40_80'],flush=True)
    if len(result['cases'])==3:
        rows=result['cases'];s=scores(rows[-2]['native_viscosity_cases'][-1]['signed_40_80'],rows[-1]['native_viscosity_cases'][-1]['signed_40_80'])
        result['independent_p3_p4']={'metrics':s,'pass':passes(s,plan['independent_fine_pair_limits'])}
        sem=json.loads((ROOT/'benchmarks/acoustics/r130d_vanishing_viscosity_q0_evidence_2026-10-10.json').read_text(encoding='utf8'))
        arm=next(r for r in sem['arms'] if r['kappa']==1.)
        s=scores(arm['cases'][-1]['signed_40_80'],rows[-1]['native_viscosity_cases'][-1]['signed_40_80'])
        result['cross_sem44_mfem_p4']={'metrics':s,'pass':passes(s,plan['independent_cross_limits'])}
        result['qualification']='PASS_NEW_VANISHING_VISCOSITY_Q0_NUMERICAL_METHOD' if result['independent_p3_p4']['pass'] and result['cross_sem44_mfem_p4']['pass'] and arm['gate_pass'] else 'FAIL_NEW_VANISHING_VISCOSITY_Q0_NUMERICAL_METHOD'
        weak=json.loads((ROOT/'benchmarks/acoustics/r130d_physical_dirac_weak_evidence_2026-10-10.json').read_text(encoding='utf8'))
        result['weak_observer_hp_control']={'p3_p4_metrics':scores(rows[-2]['physical_point_weak_signed_40_80'],rows[-1]['physical_point_weak_signed_40_80']),
            'sem44_p4_metrics':scores(weak['sem_cases'][-1]['signed_40_80'],rows[-1]['physical_point_weak_signed_40_80'])}
        dest.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf8');print('HP_QUALIFICATION',result['qualification'],result['independent_p3_p4'],result['cross_sem44_mfem_p4'],flush=True)

if __name__=='__main__':main()
