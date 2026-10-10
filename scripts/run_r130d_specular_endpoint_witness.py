from pathlib import Path
import json
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'benchmarks/acoustics'
c,rho=343.2,1.2
src=np.array([1.5,2.,2.]);rec=np.array([2.5,2.,2.])
images=[]
for n in range(-12,13):
    for m in range(-12,13):
        for sx in (-1,1):
            for sy in (-1,1):
                im=src.copy();im[:2]=[8*n+sx*src[0],8*m+sy*src[1]]
                delta=im[:2]-rec[:2]
                # Remove exact wall-corner rays from this diagnostic family.
                corner=False
                for k in range(-24,25):
                    if delta[0]==0: continue
                    u=(4*k-rec[0])/delta[0]
                    if 0<u<1 and abs((rec[1]+u*delta[1])/4-round((rec[1]+u*delta[1])/4))<1e-10:
                        corner=True;break
                r=np.linalg.norm(im-rec)
                if not corner and r/c<.252:
                    images.append((n,m,sx,sy,r,r/c))
images.sort(key=lambda x:abs(x[-1]-.25))
result={'scope':'Partial non-corner x/y-wall specular image family only, NOT full sloped-room Green solution',
        'family_size':len(images),'nearest_250ms_paths':[], 'cases':[]}
for n,m,sx,sy,r,t in images[:12]:
    result['nearest_250ms_paths'].append({'n':n,'m':m,'sx':sx,'sy':sy,'distance_m':r,'arrival_s':t,'gap_from_250ms_us':(t-.25)*1e6})
for ppw in (28,32,36,40,44):
    with np.load(ROOT/f'scratch/boundary-fitted-sem/ppw{ppw}.npz') as d:
        dt,nt=float(d['dt']),int(d['nt'])
        T=(nt-1)*dt
        sw,rw=d['sw'],d['rw'];ss,rr=d['source'],d['receiver']
        vals={};pointvals={};flips=[]
        for clock,end in [('native',T),('fixed_250ms',.25)]:
            H=np.zeros(2,complex)
            P=np.zeros(2,complex)
            for n,m,sx,sy,r,t in images:
                ii=ss.copy();ii[:,0]=8*n+sx*ii[:,0];ii[:,1]=8*m+sy*ii[:,1]
                dist=np.linalg.norm(ii[:,None,:]-rr[None,:,:],axis=-1)
                arrival=dist/c
                weights=sw[:,None]*rw[None,:]
                for j,f in enumerate((40.,80.)):
                    H[j]+=np.sum(weights*(arrival<end)*(-1j*2*np.pi*f*rho)*np.exp(1j*2*np.pi*f*arrival)/(4*np.pi*dist))
                    if t<end:
                        P[j]+=(-1j*2*np.pi*f*rho)*np.exp(1j*2*np.pi*f*t)/(4*np.pi*r)
                if clock=='native' and (np.any(arrival<end)!=np.all(arrival<end)):
                    flips.append({'path':[n,m,sx,sy],'point_arrival_s':t,'min_eight_pair_arrival_s':float(arrival.min()),'max_eight_pair_arrival_s':float(arrival.max()),'included_weight':float(weights[arrival<end].sum())})
            vals[clock]=[[float(z.real),float(z.imag)] for z in H]
            pointvals[clock]=[[float(z.real),float(z.imag)] for z in P]
        result['cases'].append({'ppw':ppw,'native_dt_s':dt,'Nt':nt,'last_pressure_time_s':T,'nominal_half_open_record_endpoint_s':nt*dt,'gap_last_sample_from_250ms_us':(T-.25)*1e6,'native_edge_straddling_path_count':len(flips),'straddling_paths':flips,'signed_partial_image_sum_40_80':vals,'signed_exact_point_partial_image_sum_40_80':pointvals})
OUT.mkdir(parents=True,exist_ok=True)
(OUT/'r130d_specular_endpoint_witness_evidence_2026-10-10.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
print(json.dumps({'family_size':len(images),'nearest':result['nearest_250ms_paths'][:4],'clocks':[{k:x[k] for k in ('ppw','gap_last_sample_from_250ms_us','native_edge_straddling_path_count')} for x in result['cases']]},indent=2))
