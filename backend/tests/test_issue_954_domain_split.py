"""#954 domain-split — declared-package contracts.

Phase 1 split the ``measurement`` domain, phase 2 the ``capture``
domain, and phase 3 the ``calibration`` domain into layered packages
(``domain/services/persistence/ui`` under ``htdt.<pkg>`` — calibration
ships no ui layer: its user-facing surfaces live in the shared
application pages, so services is its highest layer).  These tests pin
the migration's contracts for every declared package:

- every old ``htdt.<stem>`` import path still resolves — and resolves to
  the *same module object* as ``htdt.<pkg>.<layer>.<stem>`` so name
  reads AND writes (monkeypatched helpers, module-level constants) behave
  identically for old and new importers;
- moved modules only reach siblings through package-relative imports —
  nothing inside a package sneaks back through the flat shims, which
  would hide boundary violations from the audit;
- the audit's PACKAGE_LAYERS registry stays exhaustive: every module
  under ``htdt.<pkg>.*`` is classified;
- baseline/diff mode: the audit baseline file round-trips and the diff
  classifier flags new violations/cycles while tolerating recorded debt;
- hash-pinned records (measurement sealed authorities, capture task
  plans) hash identically through old and new paths — the split is
  import plumbing only, no contract drift.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = _ROOT / 'backend' / 'src'
_AUDIT_PATH = _ROOT / 'scripts' / 'package_boundary_audit.py'
_INVENTORY_PATH = _ROOT / 'scripts' / 'package_boundary_inventory.json'

_spec = importlib.util.spec_from_file_location(
    'package_boundary_audit', _AUDIT_PATH)
assert _spec is not None and _spec.loader is not None
audit_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit_mod)

PACKAGES = audit_mod.PACKAGE_LAYERS

# stem -> (package, layer) pairs derived from the audit's own registry so
# the tests track the same source of truth the boundary rules use.
MOVED = {
    stem: (pkg, layer)
    for pkg, layers in PACKAGES.items()
    for layer, stems in layers.items()
    for stem in stems
}

_LAYERS = ('domain', 'services', 'persistence', 'ui')

# Stems whose flat ``htdt.<stem>`` shim exists on disk — the modules the
# split itself moved.  Package-native additions (e.g. upstream's
# cad_remeasure_queue trio) are declared for layering enforcement but
# never had a flat path, so shim-contract checks skip them by design.
FLAT_SHIMS = {
    stem for stem in MOVED
    if (_SRC / 'htdt' / f'{stem}.py').is_file()
}


def _import(name: str):
    return importlib.import_module(name)


@pytest.mark.parametrize(
    'stem,pkg_layer',
    sorted((s, MOVED[s]) for s in FLAT_SHIMS),
    ids=sorted(FLAT_SHIMS),
)
def test_flat_path_resolves_to_canonical_module(stem, pkg_layer):
    pkg, layer = pkg_layer
    canonical = _import(f'htdt.{pkg}.{layer}.{stem}')
    flat = _import(f'htdt.{stem}')
    assert flat is canonical
    assert getattr(sys.modules['htdt'], stem) is canonical


@pytest.mark.parametrize(
    'stem,pkg_layer',
    sorted((s, MOVED[s]) for s in FLAT_SHIMS),
    ids=sorted(FLAT_SHIMS),
)
def test_from_import_forms_return_canonical_module(stem, pkg_layer):
    pkg, layer = pkg_layer
    canonical = _import(f'htdt.{pkg}.{layer}.{stem}')
    namespace = {}
    exec(f'from htdt import {stem} as _target', namespace)
    assert namespace['_target'] is canonical
    namespace = {}
    exec(f'import htdt.{stem} as _target', namespace)
    assert namespace['_target'] is canonical


def test_attribute_writes_through_flat_path_reach_canonical_module():
    flat = _import('htdt.cad_system_variant_measurement_campaign')
    canonical = _import(
        'htdt.measurement.services.cad_system_variant_measurement_campaign')
    sentinel = object()
    original = flat._utc_now
    try:
        flat._utc_now = sentinel  # noqa: SLF001 — exercising monkeypatch compat
        assert canonical._utc_now is sentinel  # noqa: SLF001
    finally:
        canonical._utc_now = original  # noqa: SLF001


def test_capture_attribute_writes_through_flat_path():
    flat = _import('htdt.capture_watch_failures')
    canonical = _import('htdt.capture.domain.capture_watch_failures')
    sentinel = object()
    original = flat._utc_now
    try:
        flat._utc_now = sentinel  # noqa: SLF001 — monkeypatch parity
        assert canonical._utc_now is sentinel  # noqa: SLF001
    finally:
        canonical._utc_now = original  # noqa: SLF001


def test_calibration_attribute_writes_through_flat_path():
    flat = _import('htdt.cad_calibration_lifecycle')
    canonical = _import(
        'htdt.calibration.domain.cad_calibration_lifecycle')
    sentinel = object()
    original = flat._utc_now
    try:
        flat._utc_now = sentinel  # noqa: SLF001 — monkeypatch parity
        assert canonical._utc_now is sentinel  # noqa: SLF001
    finally:
        canonical._utc_now = original  # noqa: SLF001


def test_registry_is_exhaustive():
    """Every .py under each htdt.<pkg> is classified — nothing unlayered."""
    for pkg, layers in PACKAGES.items():
        on_disk = {}
        for layer in _LAYERS:
            for path in (_SRC / 'htdt' / pkg / layer).glob('*.py'):
                if path.stem != '__init__':
                    on_disk[path.stem] = (pkg, layer)
        assert on_disk == {
            stem: (pkg, layer)
            for layer, stems in layers.items()
            for stem in stems
        }


def test_package_members_only_use_package_relative_sibling_imports():
    """Moved modules must not reference moved siblings via flat htdt.* paths.

    A flat-path edge inside a package would both hide the real
    dependency from the audit (it lands on a shim, not a layer) and make
    the import direction unverifiable.
    """
    for stem, (pkg, layer) in MOVED.items():
        source = (_SRC / 'htdt' / pkg / layer / f'{stem}.py').read_text(
            encoding='utf-8')
        for sibling in MOVED:
            assert f'import htdt.{sibling}' not in source, (
                f'{stem} reaches {sibling} through the flat shim')
            assert f'from htdt.{sibling} ' not in source
            assert f'from htdt import {sibling}' not in source
            assert f'from htdt.{sibling} import' not in source


def test_shim_files_are_pure_aliases():
    """Shims contain no forwarding tables or lazy hacks — just the alias."""
    for stem in FLAT_SHIMS:
        text = (_SRC / 'htdt' / f'{stem}.py').read_text(encoding='utf-8')
        assert '_sys.modules[__name__] = _impl' in text
        assert 'def __getattr__' not in text


def test_sealed_authorities_hash_identically_via_old_and_new_paths():
    """Sealed round-trip: same payload → identical sha on both paths."""
    old = _import('htdt.cad_measurement_authorities')
    new = _import('htdt.measurement.domain.cad_measurement_authorities')
    assert old.build_timing_reference is new.build_timing_reference
    kwargs = dict(
        method='loopback',
        timing_reference_id='tr-split-check',
        version='1',
        reference_channel='SL',
        input_clock_identity='clock:in',
        output_clock_identity='clock:out',
        sample_rate_hz=48000,
        t0_convention='loopback_edge',
        validity_scope='session',
        subject_measurement_ids=('m-1', 'm-2'),
        acquisition_session_id='acq-split-check',
        signal_path_identity='path:sl',
        created_at_utc='2026-09-20T00:05:00+00:00',
    )
    record_a = old.build_timing_reference(**kwargs)
    record_b = new.build_timing_reference(**kwargs)
    assert record_a == record_b
    dumped = record_a.model_dump(mode='json')
    seals = [v for k, v in dumped.items() if k.endswith('_sha256')]
    assert seals and all(
        isinstance(v, str) and len(v) == 64 for v in seals)


def test_capture_task_plan_hash_identically_via_old_and_new_paths():
    """Hash-pinned capture plan: identical plan_sha256 on both paths."""
    from htdt.cad_scene import SceneDocument
    from htdt.project_identity import new_project_reference

    old = _import('htdt.capture_mission')
    new = _import('htdt.capture.domain.capture_mission')
    assert old.build_capture_task_plan is new.build_capture_task_plan
    document = SceneDocument(
        document_id='mission-split-check', room=None, entities=())
    kwargs = dict(
        project=new_project_reference(document_id='mission-split-check'),
        purpose='initial_capture',
    )
    plan_a = old.build_capture_task_plan(document, **kwargs)
    plan_b = new.build_capture_task_plan(document, **kwargs)
    assert plan_a == plan_b
    assert len(plan_a.plan_sha256) == 64


def test_calibration_profile_hash_identically_via_old_and_new_paths():
    """Hash-pinned response-calibration profile: identical sha."""
    old = _import('htdt.cad_mic_response_calibration')
    new = _import(
        'htdt.calibration.domain.cad_mic_response_calibration')
    assert (
        old.build_response_calibration_profile
        is new.build_response_calibration_profile)
    kwargs = dict(
        profile_id='mic-split-check',
        schema_version='1',
        instrument_manufacturer='acme',
        incidence_kind='on_axis_0deg',
        reference_axis_semantics='0deg = capsule boresight',
    )
    profile_a = old.build_response_calibration_profile(**kwargs)
    profile_b = new.build_response_calibration_profile(**kwargs)
    assert profile_a == profile_b
    assert len(profile_a.profile_sha256) == 64


def _fresh_audit_report():
    return audit_mod.audit(audit_mod.ROOT)


def test_audit_sees_all_package_edges_under_their_layers():
    report = _fresh_audit_report()
    layers = report['package_layers']
    for stem, (pkg, layer) in MOVED.items():
        assert layers[f'{pkg}.{layer}.{stem}'] == f'{pkg}.{layer}'
    for stem in FLAT_SHIMS:
        assert layers[stem] == 'shim'


def test_audit_violations_all_recorded_in_inventory():
    """No *unrecorded* violations — anything new is a regression."""
    inventory = json.loads(_INVENTORY_PATH.read_text(encoding='utf-8'))
    known = set(inventory['violations'])
    report = _fresh_audit_report()
    current = {
        f"{v['rule']}|{v['module']}|{v['detail']}"
        for v in report['violations']
    }
    assert current <= known, (
        'new boundary violations appeared that are not in the debt '
        f'inventory: {sorted(current - known)[:10]}')


def test_baseline_round_trip_and_diff(tmp_path):
    report = _fresh_audit_report()
    baseline = tmp_path / 'baseline.json'
    audit_mod.write_baseline(report, baseline)
    diff = audit_mod.diff_against_baseline(report, baseline)
    assert diff['new_violations'] == []
    assert diff['new_cycles'] == []

    grown = dict(report)
    grown['violations'] = report['violations'] + [{
        'rule': 'package_import_direction',
        'module': 'htdt.measurement.domain.fake_probe',
        'detail': 'htdt.measurement.domain.fake_probe -> htdt.ui.fake',
    }]
    grown['cycles'] = report['cycles'] + [
        ['htdt.new_a', 'htdt.new_b'],
    ]
    diff = audit_mod.diff_against_baseline(grown, baseline)
    assert len(diff['new_violations']) == 1
    assert ['htdt.new_a', 'htdt.new_b'] in diff['new_cycles']
    assert diff['debt_count'] >= len(report['violations'])


def test_importing_flat_path_does_not_pull_qt_for_domain_modules():
    """Domain-layer flat shims stay headless-safe: importing one must not
    initialize PySide6."""
    for name in list(sys.modules):
        if name == 'PySide6' or name.startswith('PySide6.'):
            pytest.skip('PySide6 already loaded in this worker')
    _import('htdt.cad_measurements')
    _import('htdt.capture_bundle')
    _import('htdt.cad_calibration_lifecycle')
    assert 'PySide6' not in sys.modules
