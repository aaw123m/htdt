"""#807 package-boundary enforcement — automated layer rules.

Runs the same analysis as ``scripts/package_boundary_audit.py`` and
asserts the invariants the issue requires to be testable:

- domain/persistence/kernel modules must not import PySide6;
- persistence modules must not import UI or application modules;
- kernel modules import only kernel;
- no *new* import cycle touching the UI/application layers (known cycles
  are recorded debt, not a licence for new ones);
- every exemption stays used — an unused exemption fails so the ledger
  cannot accumulate dead entries;
- no module may exceed the size budget unless it carries a decomposition
  note in ``SIZE_EXEMPTIONS``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parent.parent.parent
_AUDIT_PATH = _ROOT / 'scripts' / 'package_boundary_audit.py'

_spec = importlib.util.spec_from_file_location(
    'package_boundary_audit', _AUDIT_PATH)
assert _spec is not None and _spec.loader is not None
_audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_audit)


@pytest.fixture(scope='module')
def report() -> dict:
    return _audit.audit()


def test_no_qt_in_lower_layers(report: dict) -> None:
    violations = [
        v for v in report['violations']
        if v['rule'] == 'no_qt_in_lower_layers'
    ]
    assert violations == []


def test_persistence_never_imports_ui(report: dict) -> None:
    violations = [
        v for v in report['violations']
        if v['rule'] == 'persistence_must_not_import_ui'
    ]
    assert violations == []


def test_kernel_imports_only_kernel(report: dict) -> None:
    violations = [
        v for v in report['violations']
        if v['rule'] == 'kernel_imports_only_kernel'
    ]
    assert violations == []


def test_no_new_cycles_touching_ui_or_application(report: dict) -> None:
    violations = [
        v for v in report['violations']
        if v['rule'] == 'no_new_cross_layer_cycles'
    ]
    assert violations == []


def test_every_exemption_is_used(report: dict) -> None:
    for module, rules in _audit.EXEMPTIONS.items():
        assert module in report['layers'], (
            f'exemption for {module} but the module no longer exists')
        if 'imports_qt' in rules:
            assert module in report['qt_import_modules'], (
                f'{module} no longer imports Qt — drop the exemption')


def test_known_cycles_still_present_and_unchanged(report: dict) -> None:
    cycles = {tuple(c) for c in report['cycles']}
    for known in _audit.KNOWN_CYCLES:
        assert known in cycles, (
            f'known cycle {known} resolved — remove it from KNOWN_CYCLES')


def test_size_budget(report: dict) -> None:
    assert report['over_budget_unexempted'] == {}
    # Exempted giants must carry a decomposition note.
    for name in report['oversized']:
        assert _audit.SIZE_EXEMPTIONS.get(name), (
            f'{name} exceeds {report["size_budget_lines"]} lines without '
            'a recorded decomposition plan')


def test_layer_classification_covers_everything(report: dict) -> None:
    assert report['module_count'] == len(report['layers'])
    assert set(report['layer_counts']) <= {
        'kernel', 'persistence', 'domain', 'ui', 'application'}


def test_megacycle_is_recorded(report: dict) -> None:
    """The authority blob SCC is #807's core evidence — keep auditing it."""
    biggest = max(report['cycles'], key=len, default=())
    assert len(biggest) > 100, (
        'the intra-authority mega-SCC shrank below 100 members — '
        'revisit this test and the decomposition plan')
