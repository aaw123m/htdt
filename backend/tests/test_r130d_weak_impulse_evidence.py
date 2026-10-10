from pathlib import Path
import json
import numpy as np

ROOT=Path(__file__).resolve().parents[2]/'benchmarks/acoustics'
KEYS=['complex_rms_relative','magnitude_max_relative','phase_max_deg']

def z(v):
    a=np.asarray(v);return a[:,0]+1j*a[:,1]

def score(co,fi):
    c,f=z(co),z(fi)
    return np.array([np.linalg.norm(c-f)/np.linalg.norm(f),np.max(abs(abs(c)-abs(f))/abs(f)),np.max(abs(np.angle(c*np.conj(f),deg=True)))])

def test_negative_weak_observer_does_not_relabel_original_pass():
    d=json.loads((ROOT/'r130d_weak_impulse_observer_evidence_2026-10-10.json').read_text(encoding='utf8'))
    assert d['original_rectangular_gate']=='SELF_CONVERGENCE_FAILED'
    assert d['primary_verdict']=='WEAK_OBSERVER_CONVERGENCE_FAILED'
    assert len(d['arms'])==10
    for arm in d['arms']:
        rows=arm['cases'];assert [c['ppw'] for c in rows]==[28,32,36,40,44]
        s=np.array([score(a['signed_40_80'],b['signed_40_80']) for a,b in zip(rows,rows[1:])])
        np.testing.assert_allclose(s,np.array([[r['metrics'][k] for k in KEYS] for r in arm['adjacent_pairs']]),atol=1e-12)
        assert arm['strict_monotone']==bool(np.all(np.diff(s,axis=0)<0))
        assert arm['all_four_pairs_pass']==bool(np.all(s<=[.2,.25,15]))

def test_unmodified_archived_wave_score_remains_failed_after_new_observer():
    d=json.loads((ROOT/'r130d_archived_native_weak_impulse_evidence_2026-10-10.json').read_text(encoding='utf8'))
    assert d['legacy_status']=='SELF_CONVERGENCE_FAILED'
    rows=d['cases'];s=np.array([score(a['legacy'],b['legacy']) for a,b in zip(rows,rows[1:])])
    np.testing.assert_allclose(s[:,0],[1.246927070,.761304772,.367367293,.958742341],rtol=1e-8)
    assert np.all(s[:,0]>.2)
    for r in rows:assert set(r['original_sha256'])=={'comms_out.h5','vox_out.h5','sim_outs.h5'}

def test_dirac_qualification_is_independently_derived_from_evidence():
    d=json.loads((ROOT/'r130d_physical_dirac_weak_evidence_2026-10-10.json').read_text(encoding='utf8'))
    plan=d['plan'];assert d['legacy_status']=='SELF_CONVERGENCE_FAILED'
    assert 'no source lowpass' in plan['source']
    rows=d['sem_cases'];s=np.array([score(a['signed_40_80'],b['signed_40_80']) for a,b in zip(rows,rows[1:])])
    limits=np.array([plan['spatial_gates'][k] for k in KEYS])
    spacepass=bool(np.all(s<=limits))
    times=d['time_refinement'];errors=np.array([r['relative_error'] for r in times]);order=np.log2(errors[:-1]/errors[1:])
    timepass=bool(np.all(np.diff(errors)<0) and np.all(order[-2:]>=1.7) and np.all(order[-2:]<=2.3) and errors[-1]<=.005)
    rows=d['mfem_cases'];assert [r['dofs'] for r in rows]==[729,4913,35937]
    assert all(r['max_true_residual']<=1e-8 for r in rows)
    fine=score(rows[-2]['signed_40_80'],rows[-1]['signed_40_80'])
    cross=score(d['sem_cases'][-1]['signed_40_80'],rows[-1]['signed_40_80'])
    finepass=bool(np.all(fine<=[plan['independent_fine_pair_limits'][k] for k in KEYS]))
    crosspass=bool(np.all(cross<=[plan['independent_cross_limits'][k] for k in KEYS]))
    expected='PASS_DIRAC_WEAK_OBSERVER_ONLY' if spacepass and timepass and finepass and crosspass else 'FAIL_DIRAC_WEAK_OBSERVER'
    assert d['qualification']==expected
