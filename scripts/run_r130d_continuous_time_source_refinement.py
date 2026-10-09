#!/usr/bin/env python3
"""Independent continuous-time source probe: SAME fixed spatial operator at each dt.

Intent: integration error isolation; this is NOT the canonical sampled q[0]=1.
"""
from __future__ import annotations
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sys
import numpy as np
from scipy.sparse.linalg import LinearOperator, cg, factorized

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
sys.path.insert(0,str(ROOT/"scripts"))
from htdt.r130d_embedded_neumann_fv import build_sloped_embedded_neumann,interior_point_stencil
from run_r130d_mfem_ref4_same_drive import matrix_from_csr
from run_r130d_fv_mfem_same_drive_comparison import validate_mfem_system,metrics

P2_SHA="e61410d4a69000c39975762b9986008974353953f7d8d9f100f9953a33c5ecb4"
DTS=(0.00025,0.000125,0.0000625)
def validate(p):
    m=p["metrics"];s=p["source"];sp=p["spatial_fixed"]
    if (p.get("schema_version")!="htdt.r130d.continuous-time-source-integrator-only-plan-1"
        or p["dt_levels_s"]!=list(DTS)
        or s["function"]!="q(t)=exp(-0.5*((t-0.040)/0.004)^2) m3/s, continuous in seconds"
        or (s["center_s"],s["sigma_s"],s["amplitude_m3_s"])!=(0.04,0.004,1)
        or s["T_s"]!=.25 or s["observations_hz"]!=[40,80]
        or sp["fv_n"]!=20 or sp["mfem_P2_r"]!=2
        or sp["pinned_mfem_raw_sha256"]!=P2_SHA
        or p["authority"]["original_fullband_impulse"]!="SELF_CONVERGENCE_FAILED"
        or p["authority"]["product_ready"] is not False
        or m["diagnostic_order_ratio_upper_bound"]!=0.4
        or m["diagnostic_finest_complex_l2_bound"]!=0.01):
        raise ValueError("prospective continuous-time integrator diagnostic changed")
    return p

def sampled_input(p,dt):
    samples=int(round(p["source"]["T_s"]/dt))
    if samples*dt!=p["source"]["T_s"] or samples>p["limits"]["max_wave_steps"]:
        raise ValueError("nonmatching fixed 250ms observation")
    t=(np.arange(samples)+0.5)*dt
    s=p["source"]
    q=s["amplitude_m3_s"]*np.exp(-0.5*((t-s["center_s"])/s["sigma_s"])**2)
    assert q.shape==(samples,) and np.all(np.isfinite(q))
    return t,q

def actually_integrate(M,K,b,r,p,dt,*,fem):
    t,q=sampled_input(p,dt)
    N=len(b)
    A=(M+dt*dt*K/4).tocsc()
    B=M-dt*dt*K/4
    if fem:
        d=A.diagonal()
        if np.min(d)<=0:raise ValueError("nonpositive midpoint preconditioner")
        pre=LinearOperator((N,N),matvec=lambda x:x/d,dtype=float)
    else:
        factor=factorized(A)
    phi=np.zeros(N);v=np.zeros(N)
    pressure=np.zeros(len(q))
    worst=0.;cg_max=0
    c=343.2;rho=1.2
    for idx in range(len(q)):
        rhs=B@phi+dt*(M@v)+(dt*dt/2)*c*c*b*q[idx]
        if fem:
            count=[0]
            def callback(_):count[0]+=1
            phi1,status=cg(A,rhs,x0=phi+dt*v,M=pre,
                rtol=p["numerics"]["rtol"],atol=p["numerics"]["atol"],
                maxiter=p["numerics"]["maxiter"],callback=callback)
            if status!=0:raise RuntimeError(f"PCG error step={idx} status={status}")
            cg_max=max(cg_max,count[0])
        else:
            phi1=factor(rhs)
        residual=float(np.linalg.norm(A@phi1-rhs)/max(np.linalg.norm(rhs),1e-15))
        if residual>p["numerics"]["true_linear_relative_residual_limit"]:
            raise RuntimeError(f"true midpoint residual exceeded at {idx}: {residual}")
        worst=max(worst,residual)
        v1=2*(phi1-phi)/dt-v
        pressure[idx]=rho*float(r@(v+v1))/2
        phi,v=phi1,v1
    freqs=np.asarray(p["source"]["observations_hz"])
    FFT=np.exp(2j*np.pi*freqs[:,None]*t[None,:])
    Q=dt*(FFT@q)
    P=dt*(FFT@pressure)
    if np.min(abs(Q))<1e-7:
        raise ValueError("small continuous source spectral bin")
    H=P/Q
    return {"dt_s":dt,"samples":len(q),
            "source_sha256":hashlib.sha256(q.tobytes()).hexdigest(),
            "sampled_original_q0_impulse":False,
            "complex_40_80_hz":[[float(z.real),float(z.imag)] for z in H],
            "magnitude_40_80_hz":[float(abs(z)) for z in H],
            "phase_deg_40_80_hz":[float(np.angle(z,deg=True)) for z in H],
            "true_relative_linear_residual_max":worst,"max_cg_iterations":cg_max}
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--systems",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args()
    raw=args.plan.read_bytes()
    plan=validate(json.loads(raw))
    geom={"source_xyz_m":plan["source_xyz_m"],
          "receiver_xyz_m":plan["receiver_xyz_m"]}
    fv=build_sloped_embedded_neumann(plan["spatial_fixed"]["fv_n"])
    fb=interior_point_stencil(fv,tuple(plan["source_xyz_m"]))
    fr=interior_point_stencil(fv,tuple(plan["receiver_xyz_m"]))
    archive=args.systems/"mfem-r2.json.gz"
    original=gzip.decompress(archive.read_bytes())
    if hashlib.sha256(original).hexdigest()!=P2_SHA:
        raise ValueError("independent P2 spatial matrix authority mismatch")
    fem=json.loads(original)
    validate_mfem_system(fem,refinement=2,ndofs=729,plan=geom)
    FM=matrix_from_csr(fem["mass_matrix"],ndofs=729,max_nnz=3_000_000)
    FK=matrix_from_csr(fem["stiffness_c2_matrix"],ndofs=729,max_nnz=3_000_000)
    eb=np.asarray(fem["source_functional"],dtype=float)
    er=np.asarray(fem["receiver_functional"],dtype=float)
    for weights in (fb,fr,eb,er):
        if abs(float(np.sum(weights))-1)>1e-9 or not np.all(np.isfinite(weights)):
            raise ValueError("spatial point stencil not normalized")
    evidence={"schema_version":"htdt.r130d.fixed-spatial-pure-time-refinement-evidence-1",
              "plan_sha256":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
              "plan":plan,"fv":[],"mfem":[],
              "mfem_original_system_sha256":P2_SHA}
    for dt in DTS:
        evidence["fv"].append(actually_integrate(fv.mass,fv.stiffness,fb,fr,plan,dt,fem=False))
        print("FV",dt,evidence["fv"][-1]["complex_40_80_hz"],flush=True)
        evidence["mfem"].append(actually_integrate(FM,FK,eb,er,plan,dt,fem=True))
        print("MFEM P2",dt,evidence["mfem"][-1]["complex_40_80_hz"],flush=True)
    comparisons={}
    for name in ("fv","mfem"):
        arr=evidence[name]
        comparisons[name+"_dt_adjacent"]=[
            metrics(a["complex_40_80_hz"],b["complex_40_80_hz"])
            for a,b in zip(arr,arr[1:])]
        coarse,fine=(q["normalized_complex_l2"]
                     for q in comparisons[name+"_dt_adjacent"])
        ratio=fine/coarse if coarse>0 else float("inf")
        comparisons[name+"_order_reduction_ratio"]=ratio
        comparisons[name+"_diagnostic_order_pass"]=bool(
            ratio<=plan["metrics"]["diagnostic_order_ratio_upper_bound"]
            and fine<=plan["metrics"]["diagnostic_finest_complex_l2_bound"])
        print(name,"time ratios",coarse,fine,ratio,flush=True)
    evidence["comparisons"]=comparisons
    evidence.update({"original_impulse":"SELF_CONVERGENCE_FAILED",
                     "original_discrete_q0_candidate":"NOT_QUALIFIED",
                     "changed_to_continuous_source":True,
                     "physical_validation":"NOT_VALIDATED","production_ready":False})
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("SAVED CONTINUOUS SOURCE TIMESTEP DIAGNOSIS",args.output,flush=True)

if __name__=="__main__":
    main()
