"""Canonical HP/image study waveform replay, with explicit conditional gates."""
from pathlib import Path
import hashlib
import numpy as np
from .r130d_enriched_evidence import _pinned_json,_scores
from .r130d_newmark_point_green import newmark_point_green_stream
from .r130d_image_enrichment import first_reflection_sources

DATA=Path(__file__).with_name('r130d_contract')
PINS={
    'hp':{'plan':'enriched_hp_plan.json','plan_sha':'bd3399c26da9b79e247cd30cf49204993d8e052d8ffc4dd08c209832114c7387',
        'evidence_sha':'ab4fd16f7a840405959116070c2668633548253b43cbf47e98e9c04b01e478ac'},
    'images':{'plan':'image_enriched_plan.json','plan_sha':'e2d1fa693731f60cf6f7faa7828d7d77375d1c804adbfe7e75d4c555cee1d427',
        'evidence_sha':'9db0c364ae7626ea30accbf668b55b43bf180e4df927c5e7e72bbb009fd59f30'},
}
V3_CONTROL_SHA='3a3bcda8a5934232d6cbe0ab97c16e79f4759f0363a708b5a7429cbc61e5ae7a'

def audit_registered_enrichment(evidence_dir: Path,study: str):
    if study not in PINS:raise ValueError('unregistered enrichment study')
    pin=PINS[study];plan=_pinned_json(DATA/pin['plan'],pin['plan_sha'])
    evidence=_pinned_json(evidence_dir/'evidence.json',pin['evidence_sha'])
    cloud=_pinned_json(DATA/'undamped_cloud.json',plan['contract_sha256'])
    points=np.array(cloud['points']);values={};rows=[]
    expected=plan['execution_order']
    actual=[[r[k] for k in ('ppw','order','refinement','quadrature_order')] for r in evidence['cases']]
    if actual!=expected and not (study=='images' and actual==expected[:5]):
        raise ValueError('registered execution case sequence changed')
    for row in evidence['cases']:
        ppw,o,r,q=[row[k] for k in ('ppw','order','refinement','quadrature_order')]
        native=next(c for c in plan['native_cases'] if c['ppw']==ppw)
        name=f'ppw{ppw}-p{o}-r{r}-q{q}.csv';path=evidence_dir/name
        if hashlib.sha256(path.read_bytes()).hexdigest()!=row['waveform_sha256']:raise ValueError('registered waveform identity failed')
        trace=np.loadtxt(path,delimiter=',',skiprows=1);nt=native['Nt'];dt=native['dt']
        if trace.shape!=(nt,5) or not np.isfinite(trace).all():raise ValueError('complete finite native record required')
        t,source,phi,p,w=trace.T;expected_source=np.zeros(nt);expected_source[0]=1.
        np.testing.assert_array_equal(source,expected_source);np.testing.assert_array_equal(t,np.arange(nt)*dt)
        np.testing.assert_allclose(p,1.2*np.gradient(phi,dt,edge_order=2),rtol=2e-12,atol=1e-9)
        s=points[native['source_cloud_indices']];recv=points[native['receiver_cloud_indices']]
        groups=first_reflection_sources(s) if study=='images' else [s]
        weight=np.outer(native['sw'],native['rw']).ravel();direct=np.zeros(nt)
        for group in groups:
            distance=np.linalg.norm(recv[None,:,:]-group[:,None,:],axis=2).ravel()
            direct+=np.array([weight@a for a,b in newmark_point_green_stream(distance,dt,nt)])
        np.testing.assert_allclose(phi-w,direct,rtol=2e-11,atol=2e-11)
        H=np.exp(2j*np.pi*np.array([40.,80.])[:,None]*t)@p
        reported=np.array(row['signed_40_80']);reported=reported[:,0]+1j*reported[:,1]
        np.testing.assert_allclose(H,reported,rtol=2e-11,atol=1e-9)
        values[ppw,o,r,q]=H;rows.append({'case':[ppw,o,r,q],'waveform_sha256':row['waveform_sha256'],
            'pressure_gradient_verified':True,'signed_dtft_recomputed':True,'analytic_groups_verified':len(groups)})
    fine=_scores(values[44,3,3,16],values[44,4,3,16]);quad=_scores(values[44,4,3,16],values[44,4,3,20])
    if study=='hp':
        control=_pinned_json(DATA/'enriched_control_v3.json',V3_CONTROL_SHA)
        reference=next(r for r in control['cases'] if (r['ppw'],r['refinement'],r['quadrature_order'])==(44,4,16))
        cross_limits=plan['cross_p2r4_limits']
    else:
        control=_pinned_json(DATA/'enriched_hp_evidence.json',PINS['hp']['evidence_sha'])
        reference=next(r for r in control['cases'] if (r['ppw'],r['order'],r['refinement'],r['quadrature_order'])==(44,4,3,16))
        cross_limits=plan['hp_direct_only_cross_limits']
    ref=np.array(reference['signed_40_80']);ref=ref[:,0]+1j*ref[:,1]
    cross=_scores(values[44,4,3,16],ref)
    gates={'hp_fine_gate':bool(np.all(np.array(fine)<=plan['spatial_fine_limits'])),
        'quadrature_gate':bool(np.all(np.array(quad)<=plan['quadrature_limits'])),
        'cross_gate':bool(np.all(np.array(cross)<=cross_limits))}
    spatial_ok=all(gates.values());native_executed=all((ppw,4,3,16) in values for ppw in (28,32,36,40,44))
    if study=='images' and not native_executed and spatial_ok:raise ValueError('missing required conditional native phase')
    if study=='images' and native_executed and not spatial_ok:raise ValueError('native phase violated prospectively fixed stop rule')
    pairs=None
    if native_executed:
        native=[values[ppw,4,3,16] for ppw in (28,32,36,40,44)];pairs=[_scores(a,b) for a,b in zip(native,native[1:])]
        gates.update({'native_pairs_within_original_caps':bool(np.all(np.array(pairs)<=plan['original_pair_limits'])),
            'strict_all_three_decrease':bool(np.all(np.diff(np.array(pairs),axis=0)<0))})
    accepted=spatial_ok and native_executed and all(gates.values())
    return {'schema_version':1,'study':study,'evidence_sha256_lf':pin['evidence_sha'],'plan_sha256_lf':pin['plan_sha'],
        'qualification':'PASS_DIAGNOSTICS_ONLY' if accepted else 'SELF_CONVERGENCE_FAILED',
        'spatial_reference':'PASS_FIXED_NATIVE44_REFERENCE' if spatial_ok else 'FAIL_FIXED_NATIVE44_REFERENCE',
        'native_clock_phase':'EXECUTED' if native_executed else 'NOT_RUN_SPATIAL_GATE_FAILED',
        'gates':gates,'hp_fine_metrics':fine,'quadrature_metrics':quad,'cross_metrics':cross,
        'native_pairs_fixed_mesh':pairs,'rows':rows,'product_go':False,'archived_fdtd':'SELF_CONVERGENCE_FAILED',
        'physical_limit':'NOT_ESTABLISHED','no_continuum_qualification_from_fixed_mesh_observers':True}
