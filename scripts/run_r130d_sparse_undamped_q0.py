"""Portable all-DOF MFEM control; run with the installed numerical wheel."""
from pathlib import Path
import argparse,gzip,hashlib,json
import numpy as np
from scipy.sparse import csr_matrix
from htdt.r130d_runtime import _load_contract,_load_asset,_scores
from htdt.r130d_conservative_impulse import conservative_q0_transfer
from htdt.r130d_sparse_undamped import sparse_undamped_q0

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--systems',type=Path,required=True)
    parser.add_argument('--assets',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--linear-solver',choices=('auto','direct','cg'),default='auto')
    parser.add_argument('--substeps',type=int,nargs='+',default=[1,2,4,8])
    args=parser.parse_args()
    if args.output.exists():raise ValueError('output already exists')
    manifest,e=_load_contract();native=next(r for r in e['undamped_cloud.json']['cases'] if r['ppw']==44)
    path=_load_asset(args.assets,'undamped_instantaneous/independent/order3.npz',manifest)
    with np.load(path,allow_pickle=False) as d:
        raw=gzip.decompress((args.systems/'mfem-p3-r2.json.gz').read_bytes())
        raw_sha=hashlib.sha256(raw).hexdigest()
        if raw_sha!=str(d['matrix_sha256']):raise ValueError('independent matrix identity changed')
        doc=json.loads(raw);n=doc['ndofs']
        if (n!=2197 or doc['order']!=3 or doc['uniform_refinements']!=2
            or doc['boundary_model']!='natural_neumann_rigid' or doc['base_volume_m3']!=56.
            or doc['sound_speed_m_s']!=343.2):raise ValueError('independent rigid fixture changed')
        def matrix(key):
            row=doc[key]
            if (row['rows'],row['cols'])!=(n,n):raise ValueError('CSR shape changed')
            return csr_matrix((row['values'],row['column_indices'],row['row_offsets']),shape=(n,n))
        M,K=matrix('mass_matrix'),matrix('stiffness_c2_matrix')
        cloud=np.zeros((82,n))
        for i,row in enumerate(doc['point_cloud']):
            np.testing.assert_allclose(row['xyz'],e['undamped_cloud.json']['points'][i],rtol=0,atol=1e-14)
            cloud[i,row['indices']]=row['values']
        b=np.asarray(native['sw'])@cloud[native['source_cloud_indices']]
        receiver=np.asarray(native['rw'])@cloud[native['receiver_cloud_indices']]
        E=d['point_evaluations'];cp=(np.asarray(native['sw'])@E[native['source_cloud_indices']])*(np.asarray(native['rw'])@E[native['receiver_cloud_indices']])
        lam=d['lam']
    h=next(a for a in e['undamped.json']['arms'] if a['name']=='gauss2')['cases'][-1]['native_h_m']
    expected,_=conservative_q0_transfer(lam,cp,native['dt'],native['Nt'],h,arm='gauss2')
    exact,_=conservative_q0_transfer(lam,cp,native['dt'],native['Nt'],h,arm='exact')
    result={'schema_version':1,'matrix_sha256':raw_sha,'order':3,'refinement':2,
        'all_dofs':n,'source_and_observer_original':True,'convergence_qualified':False,
        'physical_limit':'NOT_ESTABLISHED','cases':[]}
    for substeps in args.substeps:
        t,phi,p,H,proof=sparse_undamped_q0(M,K,b,receiver,native['dt'],native['Nt'],substeps=substeps,linear_solver=args.linear_solver)
        if substeps==1:np.testing.assert_allclose(H,expected,rtol=2e-8,atol=2e-6)
        result['cases'].append({'internal_substeps':substeps,'signed_40_80':[[float(z.real),float(z.imag)] for z in H],
            'exact_time_control_metrics':_scores(H,exact).tolist(),'execution_proof':proof,
            'newmark_modal_reproduction_metrics':_scores(H,expected).tolist() if substeps==1 else None})
        print('ALL_DOF',n,substeps,proof['max_energy_relative_drift'],flush=True)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf8')

if __name__=='__main__':main()
