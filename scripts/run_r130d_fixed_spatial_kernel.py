#!/usr/bin/env python3
"""Prospectively fixed 3D source/receiver spatial Gaussian diagnostic (#938).

Temporal q0 impulse is IDENTICAL to original, but physical spatial operators differ.
FV integrates Gaussian over exact planar cut cells, independent P2 FEM functionals
come from pinned MFEM C++ high-order quadrature on a tetrahedral mesh.
This is not original canonical R130D self-convergence acceptance.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
from pathlib import Path
import sys

import numpy as np
from numpy.polynomial.legendre import leggauss
from scipy.sparse.linalg import LinearOperator, cg

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
sys.path.insert(0,str(ROOT/"scripts"))
from htdt.r130d_embedded_neumann_fv import build_sloped_embedded_neumann
from run_r130d_exact_discrete_impulse_candidate import HASHES,DOFS
from run_r130d_mfem_ref4_same_drive import matrix_from_csr
from run_r130d_fv_mfem_same_drive_comparison import metrics,validate_mfem_system

EXPECTED_SIGMAS=(0.35,0.7)
EXPECTED_FV=(12,20,32)
EXPECTED_MFEM=(2,3,4)

def validate_plan(p):
    if (p.get("schema_version")!="htdt.r130d.fixed-spatial-gaussian-impulse-diagnostic-plan-1"
        or p["spatial_kernel"]["sigma_m"]!=list(EXPECTED_SIGMAS)
        or p["spatial_levels"]["fv_n"]!=list(EXPECTED_FV)
        or p["spatial_levels"]["independent_mfem_P2_r"]!=list(EXPECTED_MFEM)
        or p["temporal_source"]["waveform"]!="q[0]=1, q[n>0]=0"
        or p["temporal_source"]["dt_s"]!=0.00025
        or p["temporal_source"]["steps"]!=1000
        or p["temporal_source"]["frequency_bins_hz"]!=[40,80]
        or p["authority"]["original_fullband_PFFDTD"]!="SELF_CONVERGENCE_FAILED"
        or p["authority"]["production_ready"] is not False
        or p["spatial_kernel"]["fv_integration"]!="tensor Gauss-Legendre 4 points x,y,z, exact roof-clipped integration z upper=min(zcell_hi,4-0.25*y), piecewise split at roof crossing within y-cell"
        or p["spatial_kernel"]["mfem_integration"]!="MFEM H1 P2 DomainLFIntegrator GaussianCoefficient on independently triangulated sloped room; integration rule explicitly order 10; normalize by sum of P2 linear-form weights (partition of unity)"):
        raise ValueError("preregistered spatial model, integration or temporal source changed")
    return p

def _cell_integrals(i,j,k,n,quad,points,sigmas):
    """Exact geometry clip with fixed fourth-order Gaussian integrator."""
    h=4.0/n
    yl,yh=j*h,(j+1)*h
    zl,zh=k*h,(k+1)*h
    xi,wi=quad
    xpts=(i+0.5)*h+0.5*h*xi
    xw=0.5*h*wi
    edges=[yl,yh]
    for z in (zl,zh):
        yy=(4.0-z)/0.25
        if yl<yy<yh:
            edges.append(yy)
    edges=sorted(set(edges))
    out=np.zeros((len(sigmas),len(points)),dtype=np.float64)
    volume=0.0
    for a,b in zip(edges[:-1],edges[1:]):
        ypts=(a+b)/2+(b-a)/2*xi
        yw=(b-a)/2*wi
        upper=np.minimum(zh,4.0-0.25*ypts)
        span=np.maximum(0.0,upper-zl)
        if not np.any(span>0):
            continue
        zpts=zl+span[:,None]*(xi[None,:]+1)/2
        zw=span[:,None]*wi[None,:]/2
        weights=xw[:,None,None]*yw[None,:,None]*zw[None,:,:]
        xyz=np.stack(np.broadcast_arrays(xpts[:,None,None],
                                         ypts[None,:,None],
                                         zpts[None,:,:]),axis=-1)
        volume+=float(np.sum(weights))
        for p_idx,point in enumerate(points):
            r2=np.sum((xyz-np.asarray(point))**2,axis=-1)
            for s_idx,sig in enumerate(sigmas):
                out[s_idx,p_idx]+=float(np.sum(weights*np.exp(-r2/(2*sig*sig))))
    return volume,out

def fv_functionals(system, plan):
    n=system.cells_per_axis
    quad=leggauss(4)
    points=[plan["geometry"]["source_xyz_m"],plan["geometry"]["receiver_xyz_m"]]
    sigmas=plan["spatial_kernel"]["sigma_m"]
    raw=np.zeros((len(sigmas),2,system.degrees_of_freedom))
    observed_volume=0.
    worst_volume_abs=0.
    for index,(i,j,k) in enumerate(system.cell_coordinates_ijk):
        v,values=_cell_integrals(int(i),int(j),int(k),n,quad,points,sigmas)
        raw[:,:,index]=values
        observed_volume+=v
        worst_volume_abs=max(worst_volume_abs,abs(v-system.cell_volumes_m3[index]))
    if abs(observed_volume-56.)>1e-8 or worst_volume_abs>1e-10:
        raise ValueError(f"FV cut-cell quadrature geometry mismatch {observed_volume} {worst_volume_abs}")
    out=[]
    for z in range(len(sigmas)):
        source,receiver=raw[z]
        sb=float(np.sum(source));rb=float(np.sum(receiver))
        if min(sb,rb)<=0 or not np.isfinite([sb,rb]).all():
            raise ValueError("nonpositive Gaussian FV physical normalization")
        source=source/sb;receiver=receiver/rb
        if abs(float(np.sum(source))-1)>1e-10 or abs(float(np.sum(receiver))-1)>1e-10:
            raise ValueError("nonconservative normalized FV spatial source")
        out.append((source,receiver))
    return out,{"total_quadrature_volume_m3":observed_volume,
                "max_cut_cell_volume_quadrature_error_m3":worst_volume_abs,
                "quadrature_points_per_dimension":4}

def impulse_transfer(M,K,b,r,plan,*,use_pcg):
    dt=plan["temporal_source"]["dt_s"]
    steps=plan["temporal_source"]["steps"]
    c=plan["geometry"]["sound_speed_m_s"]
    rho=plan["geometry"]["density_kg_m3"]
    n=len(b)
    if not (b.shape==r.shape==(n,)
            and np.all(np.isfinite(b)) and np.all(np.isfinite(r))
            and abs(float(sum(b))-1)<1e-9 and abs(float(sum(r))-1)<1e-9):
        raise ValueError("malformed physical source/receiver weights")
    A=(M+(dt*dt/4)*K).tocsc()
    B=M-(dt*dt/4)*K
    if use_pcg:
        diag=A.diagonal()
        if np.any(diag<=0):raise ValueError("non-SPD midpoint diagonal")
        pre=LinearOperator((n,n),matvec=lambda x:x/diag,dtype=float)
    else:
        from scipy.sparse.linalg import factorized
        factor=factorized(A)
    phi=np.zeros(n)
    vel=np.zeros(n)
    pressure=np.empty(steps)
    worst_residual=0.
    max_cg=0
    for i in range(steps):
        force=c*c*b if i==0 else 0.
        rhs=B@phi+dt*(M@vel)+(dt*dt/2)*force
        if use_pcg:
            count=[0]
            def callback(_):count[0]+=1
            phi1,status=cg(A,rhs,x0=phi+dt*vel,M=pre,
                           rtol=plan["linear_solver"]["rtol"],
                           atol=plan["linear_solver"]["atol"],
                           maxiter=plan["linear_solver"]["maxiter"],callback=callback)
            if status!=0:raise RuntimeError(f"PCG failed step={i}: {status}")
            max_cg=max(max_cg,count[0])
        else:
            phi1=factor(rhs)
        true=float(np.linalg.norm(A@phi1-rhs)/max(np.linalg.norm(rhs),1e-15))
        worst_residual=max(worst_residual,true)
        if true>plan["linear_solver"]["true_relative_residual_max"]:
            raise RuntimeError(f"midpoint true residual exceeded limit at {i}: {true}")
        v1=2*(phi1-phi)/dt-vel
        pressure[i]=rho*float(r@(vel+v1))/2
        phi,vel=phi1,v1
    freqs=np.asarray(plan["temporal_source"]["frequency_bins_hz"])
    midpoint_time=(np.arange(steps)+.5)*dt
    transform=np.exp(2j*np.pi*freqs[:,None]*midpoint_time[None,:])
    Q=dt*transform[:,0]   # exact unchanged q[0]=1, all other entries zero
    P=dt*(transform@pressure)
    z=P/Q
    if not np.all(np.isfinite(z)):
        raise RuntimeError("nonfinite finite-window complex transfer")
    return {"complex_40_80_hz":[[float(v.real),float(v.imag)] for v in z],
            "magnitude_40_80_hz":[float(abs(v)) for v in z],
            "phase_deg_40_80_hz":[float(np.angle(v,deg=True)) for v in z],
            "worst_true_relative_linear_residual":worst_residual,
            "maximum_cg_iterations":max_cg,
            "sampled_time_impulse_q0_only":True}
def mfem_reference(plan,ref,systems,functionals):
    archived=systems/f"mfem-r{ref}.json.gz"
    original=gzip.decompress(archived.read_bytes())
    digest=hashlib.sha256(original).hexdigest()
    if digest!=HASHES[ref] or len(original)>plan["limits"]["max_fem_raw_json_bytes"]:
        raise ValueError("original pinned FEM mass/stiffness authority changed")
    spatial=json.loads(original)
    n=DOFS[ref]
    validate_mfem_system(spatial,refinement=ref,ndofs=n,
                         plan={"source_xyz_m":plan["geometry"]["source_xyz_m"],
                               "receiver_xyz_m":plan["geometry"]["receiver_xyz_m"]})
    M=matrix_from_csr(spatial["mass_matrix"],ndofs=n,max_nnz=3_000_000)
    K=matrix_from_csr(spatial["stiffness_c2_matrix"],ndofs=n,max_nnz=3_000_000)
    file=functionals/f"mfem-gaussian-r{ref}.json"
    raw=file.read_bytes()
    fdoc=json.loads(raw)
    if (fdoc.get("schema_version")!="htdt.r130d.independent-mfem-spatial-gaussian-functionals-1"
        or fdoc.get("order")!=2 or fdoc.get("refinement")!=ref
        or fdoc.get("dofs")!=n or fdoc.get("quadrature_order")!=10
        or len(fdoc.get("cases",[]))!=2):
        raise ValueError("MFEM Gaussian quadrature metadata / ordering mismatch")
    results={}
    for sigma,case in zip(EXPECTED_SIGMAS,fdoc["cases"]):
        if case["sigma_m"]!=sigma:raise ValueError("MFEM Gaussian sigma mismatch")
        b=np.asarray(case["source_functional"],dtype=float)
        r=np.asarray(case["receiver_functional"],dtype=float)
        if b.shape!=(n,) or r.shape!=(n,):
            raise ValueError("MFEM Gaussian linear form dimension mismatch")
        wave=impulse_transfer(M,K,b,r,plan,use_pcg=True)
        results[str(sigma)]={"r":ref,"dofs":n,"mfem_reference_raw_sha256":digest,
                             "independent_gaussian_functionals_sha256":
                                 hashlib.sha256(raw).hexdigest(),**wave}
        print("MFEM",ref,"sigma",sigma,"T",wave["complex_40_80_hz"],
              "true_res",wave["worst_true_relative_linear_residual"],flush=True)
    return results

def summarize(output):
    output["comparisons"]={}
    for sigma in EXPECTED_SIGMAS:
        s=str(sigma)
        fv=output["fv_levels"][s]
        fe=output["mfem_levels"].get(s,[])
        item={"fv_adjacent":[metrics(a["complex_40_80_hz"],b["complex_40_80_hz"])
             for a,b in zip(fv,fv[1:])],
              "mfem_adjacent":[metrics(a["complex_40_80_hz"],b["complex_40_80_hz"])
             for a,b in zip(fe,fe[1:])]}
        if fe:
            item["cross_finest"]=metrics(fv[-1]["complex_40_80_hz"],fe[-1]["complex_40_80_hz"])
        output["comparisons"][s]=item
    output["original_fullband_PFFDTD"]="SELF_CONVERGENCE_FAILED"
    output["original_discrete_impulse_candidate"]="NOT_QUALIFIED"
    output["physical_validation"]="NOT_VALIDATED"
    output["production_ready"]=False
    output["spatial_model_changed_from_original_point_source"]=True
    return output

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--plan",type=Path,required=True)
    parser.add_argument("--systems",type=Path,required=True)
    parser.add_argument("--functionals",type=Path)
    parser.add_argument("--fv-only",action="store_true")
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    raw_plan=args.plan.read_bytes()
    p=validate_plan(json.loads(raw_plan))
    if args.fv_only and args.functionals:
        raise ValueError("partial FV mode must not assert FEM availability")
    if not args.fv_only and args.functionals is None:
        raise ValueError("full diagnostic requires independent MFEM functionals")
    result={"schema_version":"htdt.r130d.fixed-spatial-impulse-observed-1",
            "plan_sha256":hashlib.sha256(raw_plan).hexdigest(),
            "plan":p,"fv_levels":{str(sig):[] for sig in EXPECTED_SIGMAS},
            "mfem_levels":{str(sig):[] for sig in EXPECTED_SIGMAS},
            "mfem_status":"NOT_RUN" if args.fv_only else "ACTUALLY_SOLVED",
            "waveform_q0_sha256":hashlib.sha256(np.r_[1.,np.zeros(999)].tobytes()).hexdigest()}
    for n in EXPECTED_FV:
        system=build_sloped_embedded_neumann(n)
        weights,quad=fv_functionals(system,p)
        for index,sig in enumerate(EXPECTED_SIGMAS):
            b,r=weights[index]
            wave=impulse_transfer(system.mass,system.stiffness,b,r,p,use_pcg=False)
            result["fv_levels"][str(sig)].append({"n":n,"dofs":system.degrees_of_freedom,
                                                     "integration":quad,**wave})
            print("FV",n,"sigma",sig,"T",wave["complex_40_80_hz"],flush=True)
    if not args.fv_only:
        for ref in EXPECTED_MFEM:
            fm=mfem_reference(p,ref,args.systems,args.functionals)
            for sig in EXPECTED_SIGMAS:
                result["mfem_levels"][str(sig)].append(fm[str(sig)])
    summarize(result)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("SAVED",args.output,"canonical FAIL / diagnostic only / NO_GO",flush=True)

if __name__=="__main__":
    main()
