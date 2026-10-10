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

DATA = Path(__file__).with_name('r130d_contract')


class R130DRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', strict=True, allow_inf_nan=False)
    schema_version: Literal[1] = 1
    fixture_id: Literal['R130D_RIGID_ROOF_56M3_FIXED_POINTS']
    profile: Literal['finite_band', 'stabilized_instantaneous']
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
    return {'schema_version':1,'numerical_qualification':'NUMERICALLY_VERIFIED',
        'fixture_id':'R130D_RIGID_ROOF_56M3_FIXED_POINTS',
        'finite_band_discrete_frequencies_hz':list(range(40,81)),
        'instantaneous_discrete_frequencies_hz':[40,80],
        'product_go':False,'recommendation_gate':'disabled',
        'blockers':['external_measured_benchmark_not_validated','owned_room_campaign_not_available',
                    'arbitrary_cad_geometry_not_supported','installer_and_full_release_gates_not_completed'],
        'legacy_pffdtd':'SELF_CONVERGENCE_FAILED','physical_undamped_point_source_limit':'NOT_ESTABLISHED'}


def run(request: R130DRequest, assets: Path):
    manifest,e=_load_contract()
    profile=request.profile
    if profile=='finite_band':
        _validate_finite(e['finite_band.json'])
        row=next(r for r in e['finite_band.json']['sem_cases'] if r['ppw']==request.ppw)
        frequencies=list(range(40,81))
    else:
        rows=_validate_instantaneous(e['instantaneous.json'],e['instantaneous_independent.json'])
        row=next(r for r in rows if r['ppw']==request.ppw)
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
            h=viscous_original_q0_transfer(lam,cp,dt,nt,row['native_h_m'])
            t,phi,p=viscous_original_q0_trace(lam,cp,dt,nt,row['native_h_m'])
            q=np.zeros(nt);q[0]=1.
            sampled=np.exp(2j*np.pi*np.asarray(frequencies)[:,None]*t)@p
            np.testing.assert_allclose(sampled,h,rtol=2e-8,atol=2e-6)
            published=_complex(row['signed_40_80'])
            waveform=np.column_stack((t,q,phi,p))
            columns='time_s,q_m3_s,velocity_potential,receiver_pressure_Pa'
            detail={'source_xyz_m':d['source'].tolist(),'source_weights':d['sw'].tolist(),
                'receiver_xyz_m':d['receiver'].tolist(),'receiver_weights':d['rw'].tolist(),
                'dt_s':dt,'steps':nt,'last_sample_s':float(t[-1]),
                'numerical_viscosity':{'kappa':1.,'nu_h_s3':row['nu_h'],'vanishes_as':'h^3 at each fixed physical mode'},
                'source':'q[0]=1; later samples zero','observer':'original gradient pressure and rectangular DTFT'}
    np.testing.assert_allclose(h,published,rtol=1e-9,atol=1e-8)
    index=[frequencies.index(f) for f in request.frequencies_hz]
    selected=h[index]
    summary={'schema_version':1,'profile':profile,'request':request.model_dump(mode='json'),
        'qualification':'NUMERICALLY_VERIFIED','product_go':False,'recommendation_gate':'disabled',
        'owned_room_validation':'NOT_VALIDATED','legacy_pffdtd':'SELF_CONVERGENCE_FAILED',
        'physical_undamped_point_source_limit':'NOT_ESTABLISHED','all_modes':len(lam),
        'execution_asset_sha256':expected,'qualification_evidence_sha256_lf':manifest['evidence_sha256_lf'],
        'implementation_sha256':{m:digest(Path(__file__).with_name(m)) for m in (
             'r130d_runtime.py','r130d_smooth_pulse.py','r130d_vanishing_viscosity_impulse.py')},
        'request_sha256':hashlib.sha256(json.dumps(request.model_dump(mode='json'),sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest(),
        'numerical_environment':{'numpy':np.__version__,'scipy':scipy.__version__,'python':platform.python_version()},
        'room':{'volume_m3':56.,'roof':'z=4-y/4','boundary':'rigid natural Neumann'},
        'frequencies_hz':request.frequencies_hz,
        'signed_transfer':[[float(v.real),float(v.imag)] for v in selected],**detail}
    return summary,waveform,columns,selected
