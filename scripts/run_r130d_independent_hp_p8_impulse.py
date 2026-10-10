"""Supplementary P6-P8 hp continuation; every previous FAIL is retained."""
from pathlib import Path
import argparse
import hashlib
import json
from run_r130d_independent_hp_impulse import ROOT,assemble_case,sha
from run_r130d_physical_pulse_sem import scores,passes

def main():
    p=argparse.ArgumentParser();p.add_argument('--exports',type=Path,required=True);a=p.parse_args()
    raw=(ROOT/'benchmarks/acoustics/r130d_independent_hp_p8_impulse_plan_2026-10-10.json').read_bytes().replace(b'\r\n',b'\n')
    digest=hashlib.sha256(raw).hexdigest()
    if digest!='a8bdd2158da1d46228c295debe1eb6cf6884c0a343a577a88f811cf68d3defe5':raise ValueError('supplementary P8 plan changed')
    plan=json.loads(raw);cloud=json.loads((a.exports/'cloud_provenance.json').read_text(encoding='utf8'))
    build=json.loads((a.exports/'build_provenance.json').read_text(encoding='utf-8-sig'))
    if build['mfem_pin']!=plan['mfem_pin'] or sha(a.exports/'cloud_provenance.json')!=build['cloud_provenance_sha256'] or sha(a.exports/'points.txt')!=build['point_cloud_sha256'] or cloud['exporter_sha256_lf']!=plan['exporter_sha256_lf']:raise ValueError('independent provenance changed')
    for c in cloud['cases']:
        if sha(ROOT/f"scratch/boundary-fitted-sem/ppw{c['ppw']}.npz")!=c['cache_sha256']:raise ValueError('original source cloud changed')
    prior=ROOT/'benchmarks/acoustics/r130d_independent_hp_p5_impulse_evidence_2026-10-10.json'
    if json.loads(prior.read_text(encoding='utf8'))['qualification']!='FAIL_NEW_VANISHING_VISCOSITY_Q0_NUMERICAL_METHOD':raise ValueError('prior P5 failure not retained')
    dest=ROOT/'benchmarks/acoustics/r130d_independent_hp_p8_impulse_evidence_2026-10-10.json'
    e={'plan_sha256':digest,'plan':plan,'independent_build':build,'prior_failed_p5_evidence_sha256_lf':hashlib.sha256(prior.read_bytes().replace(b'\r\n',b'\n')).hexdigest(),'cases':[],'qualification':'INCOMPLETE'}
    for degree in plan['orders']:
        r=assemble_case(degree,a.exports,cloud,ROOT/'scratch/independent-hp-impulse',refinement=1);e['cases'].append(r)
        dest.write_text(json.dumps(e,indent=2,allow_nan=False)+'\n',encoding='utf8')
        print('HP_HIGH_DONE',degree,r['native_viscosity_cases'][-1],flush=True)
    arm=next(r for r in json.loads((ROOT/'benchmarks/acoustics/r130d_vanishing_viscosity_q0_evidence_2026-10-10.json').read_text(encoding='utf8'))['arms'] if r['kappa']==1.)
    e['independent_pairs']=[]
    for x,y in zip(e['cases'],e['cases'][1:]):
        s=scores(x['native_viscosity_cases'][-1]['signed_40_80'],y['native_viscosity_cases'][-1]['signed_40_80'])
        e['independent_pairs'].append({'coarse':x['order'],'fine':y['order'],'metrics':s,'pass':passes(s,plan['independent_fine_pair_limits'])})
    s=scores(arm['cases'][-1]['signed_40_80'],e['cases'][-1]['native_viscosity_cases'][-1]['signed_40_80'])
    e['cross_sem44_mfem_p8']={'metrics':s,'pass':passes(s,plan['independent_cross_limits'])}
    ok=arm['gate_pass'] and e['independent_pairs'][-1]['pass'] and e['cross_sem44_mfem_p8']['pass']
    e['qualification']='PASS_NEW_VANISHING_VISCOSITY_Q0_NUMERICAL_METHOD' if ok else 'FAIL_NEW_VANISHING_VISCOSITY_Q0_NUMERICAL_METHOD'
    e['physical_undamped_point_source_limit']='NOT_ESTABLISHED';e['old_pinned_pffdtd']='SELF_CONVERGENCE_FAILED'
    dest.write_text(json.dumps(e,indent=2,allow_nan=False)+'\n',encoding='utf8')
    print('HP_P8_QUALIFICATION',e['qualification'],e['independent_pairs'],e['cross_sem44_mfem_p8'],flush=True)

if __name__=='__main__':main()
