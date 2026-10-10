"""Instantaneous physical Dirac input; changed weak observer; independent MFEM.

Every q0 mode is propagated. This never re-labels the legacy record as PASS.
"""
from pathlib import Path
import argparse
import gzip
import hashlib
import json
import sys
import time
import numpy as np
from scipy.sparse.linalg import LinearOperator,cg
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend/src'))
from htdt.r130d_weak_impulse_observer import (exact_weak_impulse_transfer,
    newmark_weak_impulse_transfer,endpoint_window,endpoint_window_derivative)
from run_r130d_physical_pulse_sem import HASHES,DOFS,csr,validate_mfem_system,pairs,scores,passes

def independent(refinement,plan):
    blob=gzip.decompress((ROOT/f'benchmarks/acoustics/r130d_mfem_independent_sparse_systems/mfem-r{refinement}.json.gz').read_bytes())
    digest=hashlib.sha256(blob).hexdigest()
    if digest!=HASHES[refinement]:raise ValueError('pinned MFEM changed')
    doc=json.loads(blob);n=DOFS[refinement]
    original_plan=json.loads((ROOT/'benchmarks/acoustics/r130d_physical_pulse_sem_plan_2026-10-10.json').read_text(encoding='utf8'))
    validate_mfem_system(doc,refinement=refinement,ndofs=n,plan=original_plan)
    M,K=csr(doc['mass_matrix'],n),csr(doc['stiffness_c2_matrix'],n)
    src,rec=np.asarray(doc['source_functional']),np.asarray(doc['receiver_functional'])
    steps=plan['mfem_steps'];dt=.25/steps;cfg=plan['linear_solver']
    def precondition(mat):
        diag=1./mat.diagonal()
        return LinearOperator((n,n),matvec=lambda x:diag*x,dtype=float)
    A,B=M+dt*dt/4*K,M-dt*dt/4*K
    prec=precondition(A);maxres=0.;maxit=0
    def solve(mat,rhs,pre,x0):
        nonlocal maxres,maxit
        count=[0]
        def callback(_):count[0]+=1
        x,status=cg(mat,rhs,M=pre,x0=x0,rtol=cfg['rtol'],atol=cfg['atol'],maxiter=cfg['maxiter'],callback=callback)
        residual=float(np.linalg.norm(mat@x-rhs)/max(np.linalg.norm(rhs),1e-30))
        if status!=0 or residual>cfg['residual_max']:raise RuntimeError('MFEM true solve residual failed')
        maxres,maxit=max(maxres,residual),max(maxit,count[0])
        return x
    # An actual instantaneous initial velocity jump, area A=dt, no pulse width.
    velocity=solve(M,343.2**2*dt*src,precondition(M),None)
    phi=np.zeros(n);trace=np.zeros(steps+1)
    t0=time.perf_counter()
    for i in range(steps):
        rhs=B@phi+dt*(M@velocity)
        nxt=solve(A,rhs,prec,phi+dt*velocity)
        velocity=2*(nxt-phi)/dt-velocity;phi=nxt
        trace[i+1]=rec@phi
        if (i+1)%1000==0:print('DIRAC_MFEM',refinement,i+1,steps,round(time.perf_counter()-t0,1),flush=True)
    t=np.arange(steps+1)*dt;win=endpoint_window(t);dw=endpoint_window_derivative(t)
    H=[]
    for f in (40.,80.):
        omega=2*np.pi*f
        H.append(-1.2*np.trapz(trace*np.exp(1j*omega*t)*(dw+1j*omega*win),dx=dt)/dt)
    return {'refinement':refinement,'dofs':n,'matrix_sha256':digest,'source':'Dirac initial velocity jump, no later input',
            'impulse_area':dt,'steps':steps,'dt':dt,'signed_40_80':pairs(H),'max_true_residual':maxres,
            'max_cg_iterations':maxit,'elapsed_s':time.perf_counter()-t0}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--stage',choices=['all','sem','mfem'],default='all');args=parser.parse_args()
    raw=(ROOT/'benchmarks/acoustics/r130d_physical_dirac_weak_plan_2026-10-10.json').read_bytes().replace(b'\r\n',b'\n');sha=hashlib.sha256(raw).hexdigest()
    if sha!='e8eb68c5dd8cdcca36f1e25cd627fde84d1928e47d570bc4a2135b567b99ade7':raise ValueError('prospective Dirac plan changed')
    plan=json.loads(raw);dest=ROOT/'benchmarks/acoustics/r130d_physical_dirac_weak_evidence_2026-10-10.json'
    if args.stage=='mfem':result=json.loads(dest.read_text(encoding='utf8'))
    else:result={'plan_sha256':sha,'plan':plan,'legacy_status':'SELF_CONVERGENCE_FAILED',
                'qualification':'INCOMPLETE','sem_cases':[],'spatial_pairs':[],'time_refinement':[],'mfem_cases':[]}
    def save():dest.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf8')
    if args.stage in ('sem','all'):
        for ppw in plan['sem_ppw']:
            p=ROOT/f'scratch/physical-pulse-sem/ppw{ppw}.npz'
            with np.load(p) as d:
                h=exact_weak_impulse_transfer(d['lam'],d['coupling'])
                row={'ppw':ppw,'all_modes':len(d['lam']),'cache_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'signed_40_80':pairs(h)}
            result['sem_cases'].append(row)
        for co,fi in zip(result['sem_cases'],result['sem_cases'][1:]):
            score=scores(co['signed_40_80'],fi['signed_40_80'])
            result['spatial_pairs'].append({'coarse':co['ppw'],'fine':fi['ppw'],'metrics':score,'pass':passes(score,plan['spatial_gates'])})
        with np.load(ROOT/'scratch/physical-pulse-sem/ppw44.npz') as d:
            ex=exact_weak_impulse_transfer(d['lam'],d['coupling'])
            for steps in plan['time_steps']:
                h=newmark_weak_impulse_transfer(d['lam'],d['coupling'],.25/steps)
                result['time_refinement'].append({'steps':steps,'relative_error':float(np.linalg.norm(h-ex)/np.linalg.norm(ex)),'signed_40_80':pairs(h)})
        rows=result['time_refinement']
        for a,b in zip(rows,rows[1:]):b['order_from_previous']=float(np.log2(a['relative_error']/b['relative_error']))
        result['time_pass']=bool(all(b['relative_error']<a['relative_error'] for a,b in zip(rows,rows[1:]))
            and all(1.7<=r['order_from_previous']<=2.3 for r in rows[-2:]) and rows[-1]['relative_error']<=.005)
        save();print('DIRAC_SEM',result['spatial_pairs'],'TIME',rows,result['time_pass'],flush=True)
    if args.stage in ('mfem','all'):
        for ref in plan['mfem_refinements']:
            row=independent(ref,plan);result['mfem_cases'].append(row);save();print('DIRAC_MFEM_DONE',ref,row['signed_40_80'],flush=True)
        result['mfem_pairs']=[]
        for co,fi in zip(result['mfem_cases'],result['mfem_cases'][1:]):
            score=scores(co['signed_40_80'],fi['signed_40_80'])
            result['mfem_pairs'].append({'coarse':co['refinement'],'fine':fi['refinement'],'metrics':score})
        cross=scores(result['sem_cases'][-1]['signed_40_80'],result['mfem_cases'][-1]['signed_40_80'])
        result['cross_solver']={'metrics':cross,'pass':passes(cross,plan['independent_cross_limits'])}
        finepass=passes(result['mfem_pairs'][-1]['metrics'],plan['independent_fine_pair_limits'])
        result['independent_fine_pair_pass']=finepass
        ok=all(x['pass'] for x in result['spatial_pairs']) and result['time_pass'] and finepass and result['cross_solver']['pass']
        result['qualification']='PASS_DIRAC_WEAK_OBSERVER_ONLY' if ok else 'FAIL_DIRAC_WEAK_OBSERVER'
        save();print('DIRAC_QUALIFICATION',result['qualification'],result['cross_solver'],flush=True)

if __name__=='__main__':main()
