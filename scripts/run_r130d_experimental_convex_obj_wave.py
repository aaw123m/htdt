#!/usr/bin/env python3
"""Manual explicit experimental-only convex OBJ wave execution.

Examples:
 python scripts/run_r130d_experimental_convex_obj_wave.py \
   --experimental-only --obj benchmarks/acoustics/r130d_convex_obj_fixtures/planar_wedge.obj \
   --cells-per-axis 12 --output scratch/wedge.json

No production solver dispatcher is altered by this tool.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
from htdt.r130d_experimental_convex_obj_wave import (
    ExperimentalConvexWaveRequest,execute_experimental_convex_obj_wave,
)


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--experimental-only",action="store_true")
    p.add_argument("--obj",type=Path,required=True)
    p.add_argument("--cells-per-axis",type=int,default=16)
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    result=execute_experimental_convex_obj_wave(
        ExperimentalConvexWaveRequest(
            obj_path=a.obj,grid_cells_per_axis=a.cells_per_axis,
            explicit_experimental_opt_in=a.experimental_only,
        )
    )
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print("EXPERIMENTAL_ONLY: actual convex OBJ drive and source provenance saved")
    print("SOURCE_SHA256",result["mesh_authority"]["source_sha256"])
    print("TRANSFER",result["wave_authority"]["complex_transfer"])
    print("PRODUCTION_READY",result["production_ready"])


if __name__=="__main__":
    main()
