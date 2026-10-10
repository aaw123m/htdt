"""Packaged, bounded R130D numerical analysis; no owned-room GO authority.

Only the exact rigid roof fixture is accepted. Cached complete eigenbases
are execution assets, bound to bundled qualification evidence by SHA256.
Neither an input file nor a successful numerical run can approve a product.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import platform
from typing import Literal

import numpy as np
import scipy
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .r130d_smooth_pulse import all_mode_midpoint_gaussian_trace, exact_finite_gaussian_pressure_modes
from .r130d_vanishing_viscosity_impulse import viscous_original_q0_trace, viscous_original_q0_transfer
from .r130d_conservative_impulse import (ARMS, conservative_q0_trace, conservative_q0_transfer,
    conservative_energy_audit, original_pressure_endpoint_audit)

DATA = Path(__file__).with_name('r130d_contract')


class R130DRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', strict=True, allow_inf_nan=False)
    schema_version: Literal[1] = 1
    fixture_id: Literal['R130D_RIGID_ROOF_56M3_FIXED_POINTS']
    profile: Literal['finite_band', 'stabilized_instantaneous', 'undamped_instantaneous']
    undamped_arm: Literal['exact','gauss2','gauss4','gauss6','rational_mass_exact','hyperstiffness_exact'] | None = None
    ppw: Literal[28,32,36,40,44] = 44
    frequencies_hz: list[float]

    @field_validator('ppw','schema_version',mode='before')
    @classmethod
    def exact_integer(cls,value):
        if type(value) is not int:
            raise ValueError('schema version and PPW must be exact integers')
        return value

    @model_validator(mode='after')
    def frequency_contract(self):
        if self.undamped_arm is not None and self.profile!='undamped_instantaneous':
            raise ValueError('undamped arm requires the undamped instantaneous profile')
        if not self.frequencies_hz or self.frequencies_hz != sorted(set(self.frequencies_hz)):
            raise ValueError('frequencies must be nonempty, unique and increasing')
        allowed = set(range(40,81)) if self.profile=='finite_band' else {40.,80.}
        if not set(self.frequencies_hz)<=allowed:
            raise ValueError('frequency outside the qualified discrete samples')
        return self


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_contract():
    manifest=json.loads((DATA/'manifest.json').read_text(encoding='utf8'))
    evidence={}
    for name,expected in manifest['evidence_sha256_lf'].items():
        raw=(DATA/name).read_bytes().replace(b'\r\n',b'\n')
        if hashlib.sha256(raw).hexdigest()!=expected:
            raise ValueError('bundled numerical qualification evidence changed')
        evidence[name]=json.loads(raw)
    return manifest,evidence


def _complex(values):
    a=np.asarray(values,float)
    if a.ndim!=2 or a.shape[1]!=2 or not np.isfinite(a).all():
        raise ValueError('invalid signed qualification values')
    return a[:,0]+1j*a[:,1]


def _scores(candidate, reference):
    if np.any(abs(reference)==0):
        raise ValueError('zero qualification reference')
    return np.array([np.linalg.norm(candidate-reference)/np.linalg.norm(reference),
        max(abs(abs(candidate)-abs(reference))/abs(reference)),
        max(abs(np.angle(candidate*np.conj(reference),deg=True)))])


def _validate_finite(e):
    plan=e['plan']
    rows=e['sem_cases']
    if (e['qualification']!='PASS_41_DISCRETE_FREQUENCIES' or plan['frequencies_hz']!=list(range(40,81))
        or [r['ppw'] for r in rows]!=[28,32,36,40,44]):
        raise ValueError('discrete finite-band qualification unavailable')
    values=[_complex(r['signed_transfer']) for r in rows]
    exact=[_complex(r['exact_time_control']) for r in rows]
    if any(len(v)!=41 for v in values+exact):
        raise ValueError('incomplete discrete qualification')
    spatial=np.array([_scores(a,b) for a,b in zip(values,values[1:])])
    temporal=np.array([_scores(a,b) for a,b in zip(values,exact)])
    hp=[_complex(r['signed_transfer']) for r in e['independent_cases']]
    if (not np.all(spatial<=[.2,.25,15.]) or not np.all(np.diff(spatial,axis=0)<0)
        or not np.all(temporal<=[.01,.02,1.]) or not np.all(_scores(hp[-2],hp[-1])<=[.03,.05,3.])
        or not np.all(_scores(exact[-1],hp[-1])<=[.03,.05,3.])):
        raise ValueError('finite-band qualification metrics do not pass frozen gates')


def _validate_instantaneous(primary,hp):
    arm=next(r for r in primary['arms'] if r['kappa']==1.)
    rows=arm['cases']
    if [r['ppw'] for r in rows]!=[28,32,36,40,44]:
        raise ValueError('original grid ladder incomplete')
    values=[_complex(r['signed_40_80']) for r in rows]
    spatial=np.array([_scores(a,b) for a,b in zip(values,values[1:])])
    fine=[_complex(r['native_viscosity_cases'][-1]['signed_40_80']) for r in hp['cases'][-2:]]
    if (hp['qualification']!='PASS_NEW_VANISHING_VISCOSITY_Q0_NUMERICAL_METHOD'
        or not np.all(spatial<=[.2,.25,15.]) or not np.all(np.diff(spatial,axis=0)<0)
        or not np.all(_scores(*fine)<=[.03,.05,3.]) or not np.all(_scores(values[-1],fine[-1])<=[.03,.05,3.])):
        raise ValueError('instantaneous qualification metrics do not pass frozen gates')
    return rows


def readiness():
    _,e=_load_contract()
    _validate_finite(e['finite_band.json'])
    _validate_instantaneous(e['instantaneous.json'],e['instantaneous_independent.json'])
    undamped=_validate_undamped(e['undamped.json'])
    return {'schema_version':1,'numerical_qualification':'NUMERICALLY_VERIFIED',
        'fixture_id':'R130D_RIGID_ROOF_56M3_FIXED_POINTS',
        'finite_band_discrete_frequencies_hz':list(range(40,81)),
        'instantaneous_discrete_frequencies_hz':[40,80],
        'undamped_impulse_qualification':{name:r['qualification'] for name,r in undamped.items()},
        'profile_qualification':{'finite_band':'NUMERICALLY_VERIFIED','stabilized_instantaneous':'NUMERICALLY_VERIFIED',
                                 'undamped_instantaneous':'SELF_CONVERGENCE_FAILED'},
        'product_go':False,'recommendation_gate':'disabled',
        'blockers':['external_measured_benchmark_not_validated','owned_room_campaign_not_available',
                    'arbitrary_cad_geometry_not_supported','installer_and_full_release_gates_not_completed',
                    'original_undamped_impulse_self_convergence_failed'],
        'legacy_pffdtd':'SELF_CONVERGENCE_FAILED','physical_undamped_point_source_limit':'NOT_ESTABLISHED'}


def _undamped_gates(cases, independent):
    spatial=np.array([_scores(_complex(a['signed_40_80']),_complex(b['signed_40_80']))
        for a,b in zip(cases,cases[1:])])
    primary=bool(np.all(spatial<=[.2,.25,15.]) and np.all(np.diff(spatial,axis=0)<0))
    values=[_complex(r['signed_40_80']) for r in independent];sem=_complex(cases[-1]['signed_40_80'])
    checks=[_scores(values[1],values[2]),_scores(values[4],values[5]),_scores(sem,values[2]),_scores(sem,values[5])]
    secondary=bool(np.all(np.asarray(checks)<=[.03,.05,3.]))
    energy=all(r['conservation']['energy_relative_drift']<=2e-12
        and r['conservation']['unit_modulus_error']<=2e-14
        and r['conservation']['all_modes_audited']==r['all_modes']
        and r['conservation']['viscosity']==0.
        and r['conservation']['modal_attenuation'] is False for r in cases+independent)
    return {'original_ladder_gate_pass':primary,'independent_gate_pass':secondary,
        'conservation_gate_pass':bool(energy),'spatial_metrics':spatial.tolist(),
        'independent_metrics':np.asarray(checks).tolist(),
        'qualification':'PASS_UNDAMPED_NUMERICAL_CANDIDATE' if primary and secondary and energy else 'SELF_CONVERGENCE_FAILED'}


def _validate_undamped(e):
    if [a['name'] for a in e['arms']]!=list(ARMS):raise ValueError('undamped controls incomplete')
    results={}
    for arm in e['arms']:
        if ([r['ppw'] for r in arm['cases']]!=[28,32,36,40,44]
            or [(r['order'],r['refinement']) for r in arm['independent_cases']]!=[(3,2),(4,2),(5,2),(6,1),(7,1),(8,1)]):
            raise ValueError('undamped independent grid families incomplete')
        if any(len(_complex(r['signed_40_80']))!=2 for r in arm['cases']+arm['independent_cases']):
            raise ValueError('undamped signed frequencies incomplete')
        result=_undamped_gates(arm['cases'],arm['independent_cases'])
        if result['qualification']!=arm['qualification']:
            raise ValueError('undamped published qualification conflicts with actual metrics')
        results[arm['name']]=result
    return results


def _load_asset(assets,relative,manifest):
    path=assets/relative
    if digest(path)!=manifest['asset_sha256'][relative]:
        raise ValueError('complete modal execution asset SHA256 mismatch')
    return path


def audit_undamped(assets: Path, arm: str='all'):
    """Recompute both mesh families from verified assets; negative results survive."""
    if arm!='all' and arm not in ARMS:raise ValueError('unknown undamped arm')
    manifest,e=_load_contract();_validate_undamped(e['undamped.json'])
    native=next(r for r in e['undamped_cloud.json']['cases'] if r['ppw']==44)
    output={'schema_version':1,'profile':'undamped_instantaneous','viscosity':0.,'arms':[],
        'all_frequencies_retained':True,'frequencies_hz':[40,80],
        'product_go':False,'legacy_pffdtd':'SELF_CONVERGENCE_FAILED','physical_limit':'NOT_ESTABLISHED'}
    for frozen in e['undamped.json']['arms']:
        name=frozen['name']
        if arm!='all' and name!=arm:continue
        rows=[];independent=[]
        for ref in frozen['cases']:
            ppw=ref['ppw'];relative=f'undamped_instantaneous/ppw{ppw}.npz'
            with np.load(_load_asset(assets,relative,manifest),allow_pickle=False) as d:
                lam,cp,dt,nt=d['lam'],d['coupling'],float(d['dt']),int(d['nt'])
                H,_=conservative_q0_transfer(lam,cp,dt,nt,ref['native_h_m'],arm=name)
                energy=conservative_energy_audit(lam,dt,nt,ref['native_h_m'],arm=name)
            np.testing.assert_allclose(H,_complex(ref['signed_40_80']),rtol=1e-9,atol=1e-8)
            rows.append({'ppw':ppw,'all_modes':len(lam),'signed_40_80':[[float(v.real),float(v.imag)] for v in H],
                'conservation':energy,'execution_asset_sha256':manifest['asset_sha256'][relative]})
        for ref in frozen['independent_cases']:
            relative=f"undamped_instantaneous/independent/order{ref['order']}.npz"
            with np.load(_load_asset(assets,relative,manifest),allow_pickle=False) as d:
                lam=d['lam'];E=d['point_evaluations']
                cp=(np.asarray(native['sw'])@E[native['source_cloud_indices']])*(np.asarray(native['rw'])@E[native['receiver_cloud_indices']])
                H,_=conservative_q0_transfer(lam,cp,native['dt'],native['Nt'],frozen['cases'][-1]['native_h_m'],arm=name)
                energy=conservative_energy_audit(lam,native['dt'],native['Nt'],frozen['cases'][-1]['native_h_m'],arm=name)
            np.testing.assert_allclose(H,_complex(ref['signed_40_80']),rtol=1e-9,atol=1e-8)
            independent.append({'order':ref['order'],'refinement':ref['refinement'],'all_modes':len(lam),
                'signed_40_80':[[float(v.real),float(v.imag)] for v in H],'conservation':energy,
                'execution_asset_sha256':manifest['asset_sha256'][relative]})
        output['arms'].append({'name':name,'cases':rows,'independent_cases':independent,**_undamped_gates(rows,independent)})
    output['qualification']='PASS_UNDAMPED_NUMERICAL_CANDIDATE' if all(a['qualification']=='PASS_UNDAMPED_NUMERICAL_CANDIDATE'
        for a in output['arms']) else 'SELF_CONVERGENCE_FAILED'
    return output


def run(request: R130DRequest, assets: Path):
    manifest,e=_load_contract()
    profile=request.profile
    if profile=='finite_band':
        _validate_finite(e['finite_band.json'])
        row=next(r for r in e['finite_band.json']['sem_cases'] if r['ppw']==request.ppw)
        frequencies=list(range(40,81))
    elif profile=='stabilized_instantaneous':
        rows=_validate_instantaneous(e['instantaneous.json'],e['instantaneous_independent.json'])
        row=next(r for r in rows if r['ppw']==request.ppw)
        frequencies=[40,80]
    else:
        _validate_undamped(e['undamped.json'])
        arm=request.undamped_arm or 'gauss2'
        frozen=next(a for a in e['undamped.json']['arms'] if a['name']==arm)
        row=next(r for r in frozen['cases'] if r['ppw']==request.ppw)
        frequencies=[40,80]
    relative=f'{profile}/ppw{request.ppw}.npz'
    path=assets/relative
    expected=manifest['asset_sha256'][relative]
    if digest(path)!=expected:
        raise ValueError('complete modal execution asset SHA256 mismatch')
    with np.load(path,allow_pickle=False) as d:
        lam,cp=d['lam'],d['coupling']
        if lam.ndim!=1 or lam.shape!=cp.shape or not np.isfinite(lam).all() or not np.isfinite(cp).all() or min(lam)<0:
            raise ValueError('invalid complete modal execution asset')
        if len(lam)!=row['all_modes']:
            raise ValueError('all-mode count differs from evidence')
        if profile=='finite_band':
            h,t,p,q=all_mode_midpoint_gaussian_trace(lam,cp,row['steps'],frequencies_hz=frequencies)
            exact=exact_finite_gaussian_pressure_modes(lam,cp,frequencies_hz=frequencies).sum(axis=1)
            if not np.all(_scores(h,exact)<=[.01,.02,1.]):
                raise ValueError('runtime temporal fidelity failed')
            published=_complex(row['signed_transfer'])
            waveform=np.column_stack((t,q,p))
            columns='time_s,source_volume_velocity_m3_s,receiver_pressure_Pa'
            detail={'source_xyz_m':[1.5,2.,2.],'receiver_xyz_m':[2.5,2.,2.],
                    'source':{'center_s':.04,'sigma_s':.004,'amplitude_m3_s':1.},
                    'steps':row['steps'],'dt_s':.25/row['steps'],
                    'runtime_temporal_metrics':_scores(h,exact).tolist()}
        else:
            dt,nt=float(d['dt']),int(d['nt'])
            if profile=='stabilized_instantaneous':
                h=viscous_original_q0_transfer(lam,cp,dt,nt,row['native_h_m'])
                t,phi,p=viscous_original_q0_trace(lam,cp,dt,nt,row['native_h_m'])
                viscosity={'kappa':1.,'nu_h_s3':row['nu_h'],'vanishes_as':'h^3 at each fixed physical mode'}
            else:
                h,_=conservative_q0_transfer(lam,cp,dt,nt,row['native_h_m'],arm=arm)
                t,phi,p,_=conservative_q0_trace(lam,cp,dt,nt,row['native_h_m'],arm=arm)
                viscosity={'nu_h_s3':0.,'modal_attenuation':False}
            q=np.zeros(nt);q[0]=1.
            sampled=np.exp(2j*np.pi*np.asarray(frequencies)[:,None]*t)@p
            np.testing.assert_allclose(sampled,h,rtol=2e-8,atol=2e-6)
            published=_complex(row['signed_40_80'])
            waveform=np.column_stack((t,q,phi,p))
            columns='time_s,q_m3_s,velocity_potential,receiver_pressure_Pa'
            detail={'source_xyz_m':d['source'].tolist(),'source_weights':d['sw'].tolist(),
                'receiver_xyz_m':d['receiver'].tolist(),'receiver_weights':d['rw'].tolist(),
                'dt_s':dt,'steps':nt,'last_sample_s':float(t[-1]),
                'numerical_viscosity':viscosity,
                'source':'q[0]=1; later samples zero','observer':'original gradient pressure and rectangular DTFT'}
            if profile=='undamped_instantaneous':
                detail.update({'undamped_arm':arm,'undamped_audit':_undamped_gates(frozen['cases'],frozen['independent_cases']),
                    'conservation':conservative_energy_audit(lam,dt,nt,row['native_h_m'],arm=arm),
                    'endpoint_decomposition':original_pressure_endpoint_audit(phi,dt)})
    np.testing.assert_allclose(h,published,rtol=1e-9,atol=1e-8)
    index=[frequencies.index(f) for f in request.frequencies_hz]
    selected=h[index]
    summary={'schema_version':1,'profile':profile,'request':request.model_dump(mode='json'),
        'qualification':frozen['qualification'] if profile=='undamped_instantaneous' else 'NUMERICALLY_VERIFIED',
        'product_go':False,'recommendation_gate':'disabled',
        'owned_room_validation':'NOT_VALIDATED','legacy_pffdtd':'SELF_CONVERGENCE_FAILED',
        'physical_undamped_point_source_limit':'NOT_ESTABLISHED','all_modes':len(lam),
        'execution_asset_sha256':expected,'qualification_evidence_sha256_lf':manifest['evidence_sha256_lf'],
        'implementation_sha256':{m:digest(Path(__file__).with_name(m)) for m in (
             'r130d_runtime.py','r130d_smooth_pulse.py','r130d_vanishing_viscosity_impulse.py','r130d_conservative_impulse.py')},
        'request_sha256':hashlib.sha256(json.dumps(request.model_dump(mode='json'),sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest(),
        'numerical_environment':{'numpy':np.__version__,'scipy':scipy.__version__,'python':platform.python_version()},
        'room':{'volume_m3':56.,'roof':'z=4-y/4','boundary':'rigid natural Neumann'},
        'frequencies_hz':request.frequencies_hz,
        'signed_transfer':[[float(v.real),float(v.imag)] for v in selected],**detail}
    return summary,waveform,columns,selected
