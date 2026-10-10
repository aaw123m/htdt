"""Replay the pinned enrichment waveform gates; reported labels grant no GO."""
from pathlib import Path
import hashlib,json
import numpy as np
from .r130d_newmark_point_green import newmark_point_green_stream

EVIDENCE_SHA_LF='3a3bcda8a5934232d6cbe0ab97c16e79f4759f0363a708b5a7429cbc61e5ae7a'
PLAN_SHA_LF='44a76408e5dad61df546709c0e833f82e0dbb6a81c15064a6dc24fb2fa6bcee4'

def _pinned_json(path,pin):
    raw=path.read_bytes().replace(b'\r\n',b'\n')
    if hashlib.sha256(raw).hexdigest()!=pin:raise ValueError('registered enrichment identity failed')
    return json.loads(raw)

def _scores(a,b):
    if np.any(abs(b)==0):raise ValueError('zero reference; no frequency masking allowed')
    return [float(np.linalg.norm(a-b)/np.linalg.norm(b)),float(max(abs(abs(a)-abs(b))/abs(b))),
        float(max(abs(np.angle(a*np.conj(b),deg=True))))]

def audit_enrichment(evidence_dir: Path):
    evidence=_pinned_json(evidence_dir/'evidence.json',EVIDENCE_SHA_LF)
    plan=_pinned_json(Path(__file__).with_name('r130d_contract')/'enriched_plan.json',PLAN_SHA_LF)
    cloud=json.loads((Path(__file__).with_name('r130d_contract')/'undamped_cloud.json').read_text())
    points=np.array(cloud['points']);values={};rows=[]
    for row in evidence['cases']:
        ppw,r,q=row['ppw'],row['refinement'],row['quadrature_order'];key=(ppw,r,q)
        native=next(c for c in plan['native_cases'] if c['ppw']==ppw)
        filename=f'ppw{ppw}-p2-r{r}-q{q}.csv';path=evidence_dir/filename
        if hashlib.sha256(path.read_bytes()).hexdigest()!=row['waveform_sha256']:raise ValueError('enriched waveform identity failed')
        trace=np.loadtxt(path,delimiter=',',skiprows=1);nt=native['Nt'];dt=native['dt']
        if trace.shape!=(nt,5) or not np.isfinite(trace).all():raise ValueError('complete finite enriched record required')
        t,source,phi,p,correction=trace.T;expected=np.zeros(nt);expected[0]=1.
        np.testing.assert_array_equal(source,expected)
        np.testing.assert_array_equal(t,np.arange(nt)*dt)
        np.testing.assert_allclose(p,1.2*np.gradient(phi,dt,edge_order=2),rtol=2e-12,atol=1e-9)
        # Check every direct-field sample independently of the supplied pressure.
        s=points[native['source_cloud_indices']];recv=points[native['receiver_cloud_indices']]
        distances=np.linalg.norm(recv[None,:,:]-s[:,None,:],axis=2).ravel()
        weights=np.outer(native['sw'],native['rw']).ravel()
        direct=np.array([weights@a for a,b in newmark_point_green_stream(distances,dt,nt)])
        np.testing.assert_allclose(phi-correction,direct,rtol=2e-11,atol=2e-11)
        H=np.exp(2j*np.pi*np.array([40.,80.])[:,None]*t)@p
        reported=np.array(row['signed_40_80']);reported=reported[:,0]+1j*reported[:,1]
        np.testing.assert_allclose(H,reported,rtol=2e-11,atol=1e-9)
        values[key]=H;rows.append({'ppw':ppw,'refinement':r,'quadrature_order':q,'waveform_sha256':row['waveform_sha256'],
            'complete_native_record_verified':True,'pressure_gradient_verified':True,'signed_dtft_recomputed':True,
            'all_free_green_samples_verified':True})
    spatial=[_scores(values[44,r,12],values[44,r+1,12]) for r in (2,3)]
    quadrature=_scores(values[44,4,12],values[44,4,16])
    native=[values[ppw,3,12] for ppw in (28,32,36,40,44)]
    pairs=[_scores(a,b) for a,b in zip(native,native[1:])]
    gates={'native_pairs_within_original_caps':bool(np.all(np.array(pairs)<=plan['original_pair_limits'])),
        'strict_all_three_decrease':bool(np.all(np.diff(np.array(pairs),axis=0)<0)),
        'spatial_fine_gate':bool(np.all(np.array(spatial[-1])<=plan['spatial_fine_limits'])),
        'boundary_quadrature_gate':bool(np.all(np.array(quadrature)<=plan['quadrature_limits']))}
    return {'schema_version':1,'qualification':'PASS_DIAGNOSTICS_ONLY' if all(gates.values()) else 'FAIL_SINGULAR_SOURCE_ENRICHMENT',
        'gates':gates,'native_pairs_fixed_mesh':pairs,'fixed_native44_spatial_pairs':spatial,'quadrature_metrics':quadrature,
        'rows':rows,'product_go':False,'archived_fdtd':'SELF_CONVERGENCE_FAILED','physical_limit':'NOT_ESTABLISHED',
        'evidence_sha256_lf':EVIDENCE_SHA_LF,'no_continuum_qualification_from_fixed_mesh_observers':True}
