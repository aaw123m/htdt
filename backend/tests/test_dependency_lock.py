"""Shipped dependency-closure authority tests.

The Windows package ships exactly the third-party closure recorded in
``backend/requirements-n05-windows.lock``. These tests fail CI if the lock
loses its ``--hash=sha256`` anchoring, drifts from the ``==`` pins declared in
``backend/pyproject.toml``, or if the release pipeline stops testing and
hash-verifying the locked closure. ``scripts/check_dependency_lock.py`` is the
executable authority; this module asserts both its verdict and the wiring that
consumes it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import subprocess
import sys

import pytest

import htdt


PACKAGE_ROOT = Path(htdt.__file__).resolve().parent
BACKEND_ROOT = PACKAGE_ROOT.parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[2]

LOCK = BACKEND_ROOT / 'requirements-n05-windows.lock'
PYPROJECT = BACKEND_ROOT / 'pyproject.toml'
CHECK_SCRIPT = REPO_ROOT / 'scripts' / 'check_dependency_lock.py'
BUILD_NATIVE = REPO_ROOT / 'scripts' / 'build-native.ps1'
RELEASE_WORKFLOW = REPO_ROOT / '.github' / 'workflows' / 'windows-release.yml'
CI_WORKFLOW = REPO_ROOT / '.github' / 'workflows' / 'ci.yml'
WORKFLOWS = REPO_ROOT / '.github' / 'workflows'

_USES_FLOATING_TAG = re.compile(r'uses:\s*[\w.-]+/[\w.-]+@v\d', re.IGNORECASE)
_USES_SHA_PIN = re.compile(r'uses:\s*[\w.-]+/[\w.-]+@[0-9a-f]{40}\b')


def _load_check_module():
    spec = importlib.util.spec_from_file_location('check_dependency_lock', CHECK_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves module globals through sys.modules during decoration.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


@pytest.fixture(scope='module')
def lock_module():
    if not CHECK_SCRIPT.is_file():
        pytest.skip('scripts/check_dependency_lock.py not available')
    return _load_check_module()


def test_lock_check_script_passes() -> None:
    if not CHECK_SCRIPT.is_file():
        pytest.skip('scripts/check_dependency_lock.py not available')
    result = subprocess.run(
        [sys.executable, str(CHECK_SCRIPT)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr


def test_every_lock_entry_is_exact_pin_with_sha256(lock_module) -> None:
    if not LOCK.is_file():
        pytest.skip('dependency lock not available')
    lock = lock_module.parse_lock(LOCK)
    assert lock.requirements, 'lock parsed zero requirements'
    seen: set[str] = set()
    for req in lock.requirements:
        key = lock_module._normalize(req.name)
        assert key not in seen, f'duplicate lock entry: {req.name}'
        seen.add(key)
        assert req.version, f'{req.name} is not an exact == pin'
        assert req.hashes, f'{req.name}=={req.version} carries no --hash=sha256 digest'
        assert all(len(digest) == 64 for digest in req.hashes)


def test_lock_covers_pyproject_runtime_and_package_pins(lock_module) -> None:
    if not (LOCK.is_file() and PYPROJECT.is_file()):
        pytest.skip('lock or pyproject not available')
    pins = lock_module.pyproject_pins(PYPROJECT)
    locked = {
        lock_module._normalize(req.name): req.version
        for req in lock_module.parse_lock(LOCK).requirements
    }
    missing = {name: ver for name, ver in pins.items() if locked.get(name) != ver}
    assert not missing, f'pyproject pins missing/diverging in lock: {missing}'


def test_build_native_uses_explicit_interpreter_and_verified_hashes() -> None:
    if not BUILD_NATIVE.is_file():
        pytest.skip('build-native.ps1 not available')
    script = BUILD_NATIVE.read_text(encoding='utf-8')
    # The build must consume the caller's interpreter, never resolve py -3.12
    # itself, and install the lock under pip's hash-checking mode.
    assert '-PythonExe' in script
    assert 'py -3.12' not in script
    assert '--require-hashes' in script


def test_release_workflow_tests_the_locked_closure() -> None:
    if not RELEASE_WORKFLOW.is_file():
        pytest.skip('windows-release.yml not available')
    workflow = RELEASE_WORKFLOW.read_text(encoding='utf-8')
    # The package job must install the hash-verified lock, run the backend
    # suite against it and prove the test overlay never moved a locked pin.
    assert '--require-hashes' in workflow
    assert 'check_dependency_lock.py --verify-installed' in workflow
    assert 'python-version: "3.12.10"' in workflow


def test_workflow_actions_are_sha_pinned() -> None:
    if not WORKFLOWS.is_dir():
        pytest.skip('workflows directory not available')
    violations = []
    for path in sorted(WORKFLOWS.glob('*.yml')):
        for lineno, line in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1):
            if 'uses:' not in line or 'uses: ./' in line:
                continue
            if _USES_FLOATING_TAG.search(line) or not _USES_SHA_PIN.search(line):
                violations.append(f'{path.name}:{lineno}: {line.strip()}')
    assert not violations, 'workflow actions not pinned to a commit SHA:\n' + '\n'.join(violations)


def test_ci_workflow_checks_lock_consistency() -> None:
    if not CI_WORKFLOW.is_file():
        pytest.skip('ci.yml not available')
    workflow = CI_WORKFLOW.read_text(encoding='utf-8')
    assert 'check_dependency_lock.py' in workflow
