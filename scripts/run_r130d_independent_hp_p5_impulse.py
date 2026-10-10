"""Supplementary P4-P5 refinement, retaining the prior P3-P4 FAIL evidence."""
from pathlib import Path
import argparse
import hashlib
import json
from run_r130d_independent_hp_impulse import ROOT,assemble_case,sha
from run_r130d_physical_pulse_sem import scores,passes

def main():
    p=argparse.ArgumentParser();p.add_argument('--exports',type=Path,required=True);a=p.parse_args()
    raw=(ROOT/'benchmarks/acoustics/r130d_independent_hp_p5_impulse_plan_2026-10-10.json').read_bytes().replace(b'\r\n',b'\n')
    digest=hashlib.sha256(raw).hexdigest()
    if digest!='6fed35366b3241334f4d2725466d38362783ebbdf4ec3f30a7583ca8a32f0fc5':raise ValueError('supplementary prospective plan changed')
    plan=json.loads(raw);cloud=json.loads((a.exports/'cloud_provenance.json').read_text(encoding='utf8'))
    build=json.loads((a.exports/'build_provenance.json').read_text(encoding='utf-8-sig'))
    if build['mfem_pin']!=plan['mfem_pin'] or sha(a.exports/'cloud_provenance.json')!=build['cloud_provenance_sha256'] or sha(a.exports/'points.txt')!=build['point_cloud_sha256'] or cloud['exporter_sha256_lf']!=plan['exporter_sha256_lf']:
        raise ValueError('independent provenance drift')
    for c in cloud['cases']:
        if sha(ROOT/f"scratch/boundary-fitted-sem/ppw{c['ppw']}.npz")!=c['cache_sha256']:raise ValueError('native source cloud drift')
    prior_path=ROOT/'benchmarks/acoustics/r130d_independent_hp_impulse_evidence_2026-10-10.json'
    prior=json.loads(prior_path.read_text(encoding='utf8'))
    if prior['qualification']!='FAIL_NEW_VANISHING_VISCOSITY_Q0_NUMERICAL_METHOD':raise ValueError('prior failure not preserved')
    p4=next(r for r in prior['cases'] if r['order']==4)
    if sha(a.exports/'mfem-p4-r2.json')!=p4['matrix_sha256']:raise ValueError('independent p4 matrix changed')
    p5=assemble_case(5,a.exports,cloud,ROOT/'scratch/independent-hp-impulse')
    e={'plan_sha256':digest,'plan':plan,'independent_build':build,'prior_failed_evidence_sha256_lf':hashlib.sha256(prior_path.read_bytes().replace(b'\r\n',b'\n')).hexdigest(),'cases':[p4,p5]}
    primary=json.loads((ROOT/'benchmarks/acoustics/r130d_vanishing_viscosity_q0_evidence_2026-10-10.json').read_text(encoding='utf8'))
    arm=next(r for r in primary['arms'] if r['kappa']==1.)
    fine=scores(p4['native_viscosity_cases'][-1]['signed_40_80'],p5['native_viscosity_cases'][-1]['signed_40_80'])
    cross=scores(arm['cases'][-1]['signed_40_80'],p5['native_viscosity_cases'][-1]['signed_40_80'])
    e['independent_p4_p5']={'metrics':fine,'pass':passes(fine,plan['independent_fine_pair_limits'])}
    e['cross_sem44_mfem_p5']={'metrics':cross,'pass':passes(cross,plan['independent_cross_limits'])}
    e['qualification']='PASS_NEW_VANISHING_VISCOSITY_Q0_NUMERICAL_METHOD' if arm['gate_pass'] and e['independent_p4_p5']['pass'] and e['cross_sem44_mfem_p5']['pass'] else 'FAIL_NEW_VANISHING_VISCOSITY_Q0_NUMERICAL_METHOD'
    e['physical_undamped_point_source_limit']='NOT_ESTABLISHED';e['old_pinned_pffdtd']='SELF_CONVERGENCE_FAILED'
    e['weak_observer_p4_p5_control']=scores(p4['physical_point_weak_signed_40_80'],p5['physical_point_weak_signed_40_80'])
    (ROOT/'benchmarks/acoustics/r130d_independent_hp_p5_impulse_evidence_2026-10-10.json').write_text(json.dumps(e,indent=2,allow_nan=False)+'\n',encoding='utf8')
    print('HP_P5_QUALIFICATION',e['qualification'],e['independent_p4_p5'],e['cross_sem44_mfem_p5'],flush=True)

if __name__=='__main__':main()
