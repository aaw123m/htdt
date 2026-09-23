from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / 'benchmarks' / 'acoustics' / 'r100a_manifest.json'
CHECKER_PATH = ROOT / 'scripts' / 'check_r100a_radiation_reference.py'


def _checker_module():
    spec = importlib.util.spec_from_file_location('r100a_radiation_reference_checker', CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_manifest(tmp_path: Path, mutate) -> Path:
    payload = json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))
    fixture = next(
        item
        for item in payload['fixtures']
        if item['fixture_id'] == 'wave-explicit-radiation-termination-v1'
    )
    mutate(fixture)
    path = tmp_path / 'r100a-mutated.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    return path


def test_radiation_reference_rejects_geometry_drift(tmp_path: Path) -> None:
    checker = _checker_module()

    path = _write_manifest(
        tmp_path,
        lambda fixture: fixture['regions'][0]['vertices'][1]['position'].__setitem__('x_m', 6.1),
    )

    with pytest.raises(ValueError, match='room geometry authority changed'):
        checker.check_reference(path)


def test_radiation_reference_rejects_transfer_db_reference_drift(tmp_path: Path) -> None:
    checker = _checker_module()

    def mutate(fixture) -> None:
        observable = next(
            item for item in fixture['observables'] if item['observable_id'] == 'termination-fr'
        )
        observable['unit'] = 'dB'

    path = _write_manifest(tmp_path, mutate)

    with pytest.raises(ValueError, match='transfer-magnitude dB reference unit changed'):
        checker.check_reference(path)
