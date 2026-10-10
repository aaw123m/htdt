"""Signed free/correction endpoint audit; no change to any convergence gates."""
from pathlib import Path
import argparse,hashlib,json,sys
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend/src'))
from htdt.r130d_conservative_impulse import original_pressure_endpoint_audit
from htdt.r130d_newmark_point_green import newmark_point_green_stream

def main():
    p=argparse.ArgumentParser();p.add_argument('--evidence-dir',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.output.exists():raise ValueError('choose a new audit file')
    evidence=json.loads((a.evidence_dir/'evidence.json').read_text())
    if evidence['enriched_numerical_qualification']=='INCOMPLETE':raise ValueError('study incomplete')
    cloud=json.loads((ROOT/'backend/src/htdt/r130d_contract/undamped_cloud.json').read_text());points=np.array(cloud['points'])
    result={'schema_version':1,'interpretation':'component attribution only; free space is not the closed-room solution',
        'all_original_endpoint_terms_retained':True,'product_go':False,'native_free_space_cases':[],'enriched_cases':[]}
    for native in cloud['cases']:
        s=points[native['source_cloud_indices']];r=points[native['receiver_cloud_indices']]
        d=np.linalg.norm(r[None,:,:]-s[:,None,:],axis=2).ravel();w=np.outer(native['sw'],native['rw']).ravel()
        phi=np.array([w@potential for potential,gradient in newmark_point_green_stream(d,native['dt'],native['Nt'])])
        result['native_free_space_cases'].append({'ppw':native['ppw'],
            'observer':original_pressure_endpoint_audit(phi,native['dt'])})
    for row in evidence['cases']:
        ppw,r,q=row['ppw'],row['refinement'],row['quadrature_order']
        path=a.evidence_dir/f'ppw{ppw}-p2-r{r}-q{q}.csv'
        if hashlib.sha256(path.read_bytes()).hexdigest()!=row['waveform_sha256']:raise ValueError('record changed')
        trace=np.loadtxt(path,delimiter=',',skiprows=1);dt=next(c['dt'] for c in cloud['cases'] if c['ppw']==ppw)
        phi,correction=trace[:,2],trace[:,4]
        components={name:original_pressure_endpoint_audit(value,dt) for name,value in (
            ('total',phi),('free_space',phi-correction),('boundary_correction',correction))}
        for i in range(2):
            values=[np.array(components[name]['frequencies'][i]['total']) for name in ('total','free_space','boundary_correction')]
            np.testing.assert_allclose(values[0],values[1]+values[2],rtol=2e-11,atol=2e-9)
        result['enriched_cases'].append({'ppw':ppw,'refinement':r,'quadrature_order':q,'components':components})
    a.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
if __name__=='__main__':main()
