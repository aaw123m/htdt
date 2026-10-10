"""Installed htdt-r130d command: bounded numerical execution and GO status."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile


def _unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:
            raise ValueError(f'duplicate input key: {key}')
        result[key]=value
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description='R130D fixed-fixture numerical analysis; physical product GO remains disabled')
    sub=parser.add_subparsers(dest='verb',required=True)
    status=sub.add_parser('status')
    status.add_argument('--require-product-go',action='store_true')
    run_parser=sub.add_parser('run')
    run_parser.add_argument('--request',type=Path,required=True)
    run_parser.add_argument('--assets',type=Path,required=True)
    run_parser.add_argument('--output-dir',type=Path,required=True)
    audit_parser=sub.add_parser('audit-undamped',help='recompute all original gates and both independent mesh families')
    audit_parser.add_argument('--assets',type=Path,required=True)
    audit_parser.add_argument('--output-dir',type=Path,required=True)
    audit_parser.add_argument('--arm',choices=['all','exact','gauss2','gauss4','gauss6','rational_mass_exact','hyperstiffness_exact'],default='all')
    enriched_parser=sub.add_parser('audit-enrichment',help='recompute the pinned singular-source enrichment gates from full waveforms')
    enriched_parser.add_argument('--evidence-dir',type=Path,required=True)
    enriched_parser.add_argument('--output-dir',type=Path,required=True)
    for command in (status,run_parser,audit_parser,enriched_parser):
        command.add_argument('--json',action='store_true',help='single machine-readable result envelope')
    args=parser.parse_args(argv)
    os.environ.setdefault('OPENBLAS_NUM_THREADS','4')
    os.environ.setdefault('OMP_NUM_THREADS','4')
    try:
        import numpy as np
        from .r130d_runtime import R130DRequest, readiness, run, audit_undamped
        if args.verb=='status':
            data=readiness()
            code=3 if args.require_product_go and not data['product_go'] else 0
        elif args.verb=='audit-enrichment':
            from .r130d_enriched_evidence import audit_enrichment
            output=args.output_dir.resolve()
            if output.exists():raise ValueError('output directory already exists; choose a new run directory')
            report=audit_enrichment(args.evidence_dir.resolve())
            output.parent.mkdir(parents=True,exist_ok=True)
            staging=Path(tempfile.mkdtemp(prefix=output.name+'-staging-',dir=output.parent))
            (staging/'audit.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n',encoding='utf8')
            (staging/'verification.json').write_text(json.dumps({'schema_version':1,'product_go':False,
                'files':{'audit.json':hashlib.sha256((staging/'audit.json').read_bytes()).hexdigest()}},indent=2)+'\n')
            staging.rename(output)
            data={'output_dir':str(output),'qualification':report['qualification'],'product_go':False,'gates':report['gates']}
            code=6 if report['qualification']=='FAIL_SINGULAR_SOURCE_ENRICHMENT' else 0
        elif args.verb=='audit-undamped':
            output=args.output_dir.resolve()
            if output.exists():raise ValueError('output directory already exists; choose a new run directory')
            report=audit_undamped(args.assets.resolve(),args.arm)
            output.parent.mkdir(parents=True,exist_ok=True)
            staging=Path(tempfile.mkdtemp(prefix=output.name+'-staging-',dir=output.parent))
            raw=json.dumps(report,indent=2,allow_nan=False)+'\n'
            (staging/'audit.json').write_text(raw,encoding='utf8')
            verification={'schema_version':1,'product_go':False,
                'files':{'audit.json':hashlib.sha256((staging/'audit.json').read_bytes()).hexdigest()}}
            (staging/'verification.json').write_text(json.dumps(verification,indent=2)+'\n',encoding='utf8')
            staging.rename(output)
            data={'output_dir':str(output),'qualification':report['qualification'],'product_go':False,
                  'arms_audited':len(report['arms']),'independent_families_audited':2}
            code=6 if report['qualification']=='SELF_CONVERGENCE_FAILED' else 0
        else:
            raw=json.loads(args.request.read_text(encoding='utf-8-sig'),object_pairs_hook=_unique_object)
            request=R130DRequest.model_validate(raw)
            output=args.output_dir.resolve()
            if output.exists():
                raise ValueError('output directory already exists; choose a new run directory')
            summary,waveform,columns,h=run(request,args.assets.resolve())
            output.parent.mkdir(parents=True,exist_ok=True)
            staging=Path(tempfile.mkdtemp(prefix=output.name+'-staging-',dir=output.parent))
            (staging/'summary.json').write_text(json.dumps(summary,indent=2,allow_nan=False)+'\n',encoding='utf8')
            np.savetxt(staging/'waveform.csv',waveform,delimiter=',',header=columns,comments='')
            np.savetxt(staging/'transfer.csv',np.column_stack((request.frequencies_hz,h.real,h.imag,abs(h),np.angle(h,deg=True))),
                delimiter=',',header='frequency_Hz,real_Pa_s_m3,imag_Pa_s_m3,magnitude_Pa_s_m3,phase_deg',comments='')
            verification={'schema_version':1,'product_go':False,'request_sha256':summary['request_sha256'],
                'files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in staging.iterdir()}}
            (staging/'verification.json').write_text(json.dumps(verification,indent=2,allow_nan=False)+'\n',encoding='utf8')
            staging.rename(output)
            data={'output_dir':str(output),'qualification':summary['qualification'],
                  'product_go':False,'recommendation_gate':'disabled','all_modes':summary['all_modes']}
            code=6 if summary['qualification']=='SELF_CONVERGENCE_FAILED' else 0
        envelope={'ok':code==0,'exit_code':code,'data':data}
    except (OSError,ValueError,AssertionError,KeyError,StopIteration,RuntimeError) as exc:
        code=4
        envelope={'ok':False,'exit_code':code,'error':str(exc),'product_go':False}
    except ImportError as exc:
        code=5
        envelope={'ok':False,'exit_code':code,'error':f'Numerical runtime dependency unavailable: {exc}', 'product_go':False}
    print(json.dumps(envelope,ensure_ascii=False,allow_nan=False))
    return code


if __name__=='__main__':
    raise SystemExit(main())
