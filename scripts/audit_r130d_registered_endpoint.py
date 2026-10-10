"""Attribute all retained endpoint terms in a registered complete waveform study."""
from pathlib import Path
import argparse,json,sys
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend/src'))
from htdt.r130d_registered_enrichment import audit_registered_enrichment
from htdt.r130d_conservative_impulse import original_pressure_endpoint_audit

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--study',choices=['hp','images'],required=True)
    parser.add_argument('--evidence-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():raise ValueError('choose a new endpoint audit file')
    replay=audit_registered_enrichment(args.evidence_dir,args.study)
    evidence=json.loads((args.evidence_dir/'evidence.json').read_text())
    result={'schema_version':1,'study':args.study,'product_go':False,
        'qualification':replay['qualification'],'all_original_endpoint_terms_retained':True,
        'interpretation':'component attribution, not an alternative observer or a proof of nonexistence',
        'cases':[]}
    for row in evidence['cases']:
        ppw,o,r,q=[row[k] for k in ('ppw','order','refinement','quadrature_order')]
        trace=np.loadtxt(args.evidence_dir/f'ppw{ppw}-p{o}-r{r}-q{q}.csv',delimiter=',',skiprows=1)
        dt=trace[1,0];phi,w=trace[:,2],trace[:,4]
        components={name:original_pressure_endpoint_audit(value,dt) for name,value in
            [('total',phi),('analytic_field',phi-w),('boundary_correction',w)]}
        for i in range(2):
            for part in ('total','bulk','initial_stencils','final_stencils'):
                z=[np.array(components[name]['frequencies'][i][part]) for name in
                    ('total','analytic_field','boundary_correction')]
                np.testing.assert_allclose(z[0],z[1]+z[2],rtol=2e-11,atol=2e-9)
        result['cases'].append({'case':[ppw,o,r,q],'components':components})
    args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf8')
    print(json.dumps({'cases':len(result['cases']),'product_go':False,'endpoint_terms_removed':False}))
if __name__=='__main__':main()
