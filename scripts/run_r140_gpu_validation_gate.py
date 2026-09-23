from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess


def _nvidia_probe() -> tuple[bool, str | None]:
    executable = shutil.which('nvidia-smi')
    if executable is None:
        return False, None
    try:
        completed = subprocess.run(
            [
                executable,
                '--query-gpu=name,driver_version',
                '--format=csv,noheader',
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False, None
    identity = completed.stdout.strip()
    return bool(identity), identity or None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()

    detected, device = _nvidia_probe()

    evidence = {
        'schema_version': 'htdt.r140.gpu-validation-gate-1',
        'gpu_device_detected': detected,
        'gpu_device_probe': device,
        'repository_gpu_candidate_backend_available': False,
        'hardware_numerical_validation': 'NOT_RUN',
        'gpu_validation_state': 'NOT_VALIDATED',
        'execution_acceptance': 'BLOCKED',
        'gpu_numerical_equivalence_validated': False,
        'production_gpu_support': False,
        'reason': (
            'NO_REPOSITORY_GPU_CANDIDATE_BACKEND'
            if detected
            else 'NO_GPU_DEVICE_AND_NO_REPOSITORY_GPU_CANDIDATE_BACKEND'
        ),
        'synthetic_or_mock_promoted_to_numerical_evidence': False,
        'rdc_calls': 0,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
