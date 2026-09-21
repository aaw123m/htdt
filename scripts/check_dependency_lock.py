"""Verify the shipped Windows dependency lock.

``backend/requirements-n05-windows.lock`` is the single authority for the
third-party Python dependency closure that ships inside the PyInstaller
package and the Inno Setup installer. The release pipeline installs it under
``pip install --require-hashes``, so every pin must carry ``--hash=sha256:``
digests for the artifacts pip may download.

This script enforces, without installing anything:

* every lock entry is an exact ``name==version`` pin with at least one
  ``--hash=sha256:`` digest, and no package is listed twice;
* every ``==`` pin in ``backend/pyproject.toml`` (runtime dependencies plus
  the ``package`` extra consumed by the native build) appears in the lock at
  the identical version — the lock can never silently drift from the
  declared dependency authority.

Two optional modes support the release workflow and lock maintenance:

* ``--verify-installed``: assert that the *current* interpreter has every
  locked distribution installed at exactly the locked version. The release
  workflow runs this after overlaying the pinned test harness on the locked
  closure, proving the shipped packages were never moved during the test
  gate.
* ``--refresh-hashes``: rewrite the lock in place, refreshing the
  ``--hash=sha256:`` digests for every released file of each pinned version
  from the package index JSON metadata. Pins themselves come from the
  documented freeze procedure (see the lock header); this mode only
  re-anchors their hashes. Requires network access to the package index.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = REPO_ROOT / 'backend' / 'requirements-n05-windows.lock'
DEFAULT_PYPROJECT = REPO_ROOT / 'backend' / 'pyproject.toml'
DEFAULT_INDEX_JSON = 'https://pypi.org/pypi'

_REQ_LINE = re.compile(r'^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>\S+?)\s*(?:\\)?\s*$')
_HASH_LINE = re.compile(r'^--hash=sha256:(?P<digest>[0-9a-fA-F]{64})\s*(?:\\)?\s*$')


def _normalize(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


@dataclass
class LockedRequirement:
    name: str
    version: str
    hashes: list[str] = field(default_factory=list)


@dataclass
class LockFile:
    header: list[str]
    requirements: list[LockedRequirement]


def parse_lock(path: Path) -> LockFile:
    """Parse a pip requirements lock with ``--hash`` continuation lines."""
    header: list[str] = []
    requirements: list[LockedRequirement] = []
    current: LockedRequirement | None = None
    seen_header_end = False
    for lineno, raw in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1):
        line = raw.strip()
        if not seen_header_end and (not line or line.startswith('#')):
            header.append(raw.rstrip())
            continue
        seen_header_end = True
        if not line or line.startswith('#'):
            continue
        req = _REQ_LINE.match(line)
        if req:
            current = LockedRequirement(req.group('name'), req.group('version'))
            requirements.append(current)
            continue
        digest = _HASH_LINE.match(line)
        if digest and current is not None:
            current.hashes.append(digest.group('digest').lower())
            continue
        raise ValueError(f'{path}:{lineno}: unparsable line: {raw!r}')
    return LockFile(header, requirements)


_SPEC_NAME = re.compile(r'^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)')


def _requirement_part(spec: str) -> str:
    """Requirement without its environment marker (``spec; marker``)."""
    return spec.split(';', 1)[0].strip()


def pyproject_pins(path: Path) -> dict[str, str]:
    """Exact ``==`` pins the lock must cover: runtime deps + package extra."""
    pyproject = tomllib.loads(path.read_text(encoding='utf-8'))
    project = pyproject['project']
    specifiers = list(project.get('dependencies', []))
    specifiers += project.get('optional-dependencies', {}).get('package', [])
    pins: dict[str, str] = {}
    for spec in specifiers:
        match = _REQ_LINE.match(_requirement_part(spec))
        if match:
            pins[_normalize(match.group('name'))] = match.group('version')
    return pins


def pyproject_unpinned(path: Path) -> list[str]:
    """Runtime/package specifiers that are not exact ``==`` pins."""
    pyproject = tomllib.loads(path.read_text(encoding='utf-8'))
    project = pyproject['project']
    specifiers = list(project.get('dependencies', []))
    specifiers += project.get('optional-dependencies', {}).get('package', [])
    return [
        spec for spec in specifiers
        if _SPEC_NAME.match(spec.strip()) and not _REQ_LINE.match(_requirement_part(spec))
    ]


def check_lock(lock_path: Path, pyproject_path: Path) -> list[str]:
    """Offline consistency invariants. Returns a list of problems."""
    problems: list[str] = []
    lock = parse_lock(lock_path)
    if not lock.requirements:
        problems.append(f'{lock_path}: no pinned requirements found')

    seen: dict[str, str] = {}
    for req in lock.requirements:
        key = _normalize(req.name)
        if key in seen:
            problems.append(f'{lock_path}: duplicate pin for {req.name}')
        seen[key] = req.version
        if not req.hashes:
            problems.append(
                f'{lock_path}: {req.name}=={req.version} has no --hash=sha256 '
                'digest (installs under --require-hashes would fail)'
            )

    for spec in pyproject_unpinned(pyproject_path):
        problems.append(
            f'{pyproject_path}: non-exact dependency specifier {spec!r} — the '
            'shipped closure requires == pins so the lock covers it exactly'
        )

    pins = pyproject_pins(pyproject_path)
    for name, version in sorted(pins.items()):
        locked = seen.get(name)
        if locked is None:
            problems.append(f'{lock_path}: pyproject pin {name}=={version} is not locked')
        elif locked != version:
            problems.append(
                f'{lock_path}: pyproject pin {name}=={version} diverges from locked {locked}'
            )
    return problems


def verify_installed(lock_path: Path) -> list[str]:
    """Assert the current interpreter still runs the locked closure."""
    from importlib import metadata

    installed = {
        _normalize(dist.metadata['Name']): dist.version
        for dist in metadata.distributions()
        if dist.metadata['Name']
    }
    problems: list[str] = []
    for req in parse_lock(lock_path).requirements:
        found = installed.get(_normalize(req.name))
        if found is None:
            problems.append(f'locked package not installed: {req.name}=={req.version}')
        elif found != req.version:
            problems.append(
                f'locked closure moved: {req.name} locked=={req.version} installed=={found}'
            )
    return problems


def fetch_hashes(name: str, version: str, index_json: str) -> list[str]:
    """All released-file sha256 digests for ``name==version`` on the index."""
    url = f'{index_json.rstrip("/")}/{name}/{version}/json'
    with urllib.request.urlopen(url, timeout=30) as response:
        payload = json.load(response)
    digests = sorted({
        file['digests']['sha256'].lower()
        for file in payload.get('urls', [])
        if file.get('digests', {}).get('sha256')
    })
    if not digests:
        raise ValueError(f'{name}=={version}: index returned no files with sha256 digests')
    return digests


def refresh_hashes(lock_path: Path, index_json: str) -> None:
    """Rewrite the lock, preserving pins and header, with fresh hashes."""
    lock = parse_lock(lock_path)
    lines = list(lock.header)
    if lines and lines[-1].strip():
        lines.append('')
    total = len(lock.requirements)
    for index, req in enumerate(lock.requirements, start=1):
        print(f'[{index}/{total}] {req.name}=={req.version}', flush=True)
        digests = fetch_hashes(req.name, req.version, index_json)
        lines.append(f'{req.name}=={req.version} \\')
        lines.extend(
            f'    --hash=sha256:{digest}' + (' \\' if i < len(digests) - 1 else '')
            for i, digest in enumerate(digests)
        )
    lock_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--lock', type=Path, default=DEFAULT_LOCK)
    parser.add_argument('--pyproject', type=Path, default=DEFAULT_PYPROJECT)
    parser.add_argument(
        '--verify-installed',
        action='store_true',
        help='also assert the current environment matches the locked versions',
    )
    parser.add_argument(
        '--refresh-hashes',
        action='store_true',
        help='rewrite the lock with fresh sha256 digests from the package index',
    )
    parser.add_argument(
        '--index-json',
        default=DEFAULT_INDEX_JSON,
        help='package index JSON API base URL used by --refresh-hashes',
    )
    args = parser.parse_args(argv)

    if args.refresh_hashes:
        refresh_hashes(args.lock, args.index_json)
        print(f'Refreshed hashes in {args.lock}')

    problems = check_lock(args.lock, args.pyproject)
    if args.verify_installed:
        problems += verify_installed(args.lock)
    if problems:
        for problem in problems:
            print(f'ERROR: {problem}', file=sys.stderr)
        return 1
    print(f'OK: {args.lock} is consistent and hash-pinned')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
