#!/usr/bin/env python3
"""Packaged-build smoke check for the release verification gate (#833).

Verifies that a PyInstaller onedir build in ``--package-dir`` (default
``dist-native/HTDT``) actually corresponds to the checked-out revision:

* ``HTDT.exe --version`` runs and reports the canonical display version
  (``<version>+g<sha8>[.dirty]`` — the same string build-installer.ps1
  stamps into the installer name);
* the embedded ``_internal/htdt_build/build_info.json`` carries
  ``commit_sha``/``dirty``/``lock_sha256`` and its ``commit_sha`` matches
  the worktree's ``git rev-parse HEAD`` — a package built from a different
  revision can never present itself as this checkout's verified build;
* the lock hash embedded at build time still matches the repo lock file
  (the dependency closure the build claims is the one in the tree).

Exit 0 = pass, 1 = a check failed, 2 = the tool itself could not run
(missing package, unreadable build_info, git failure). Exit 2 is how the
runner distinguishes "not built / unsupported" from "ran and failed" —
the manifest also gates this class on the ``native_package`` capability,
so a missing package normally skips with a reason before this runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACKAGE_DIR = REPO_ROOT / 'dist-native' / 'HTDT'
BUILD_INFO_REL = Path('_internal') / 'htdt_build' / 'build_info.json'
LOCK_REL = Path('backend') / 'requirements-n05-windows.lock'

# <semver>+g<hex8+>[.dirty] — build_info.display_version / version_string().
DISPLAY_VERSION_RE = re.compile(
    r'^(?P<version>\S+)\+g(?P<sha>[0-9a-f]{8,40})(?P<dirty>\.dirty)?$'
)


def _failures() -> list[str]:
    return []


def _git_head(repo_root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=repo_root, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument(
        '--package-dir', type=Path, default=DEFAULT_PACKAGE_DIR,
        help='PyInstaller onedir output (default: dist-native/HTDT)',
    )
    parser.add_argument(
        '--repo-root', type=Path, default=REPO_ROOT,
    )
    parser.add_argument(
        '--timeout', type=int, default=120,
        help='seconds to wait for HTDT.exe --version (default: 120)',
    )
    args = parser.parse_args(argv)

    package_dir = args.package_dir.resolve()
    repo_root = args.repo_root.resolve()
    problems = _failures()

    exe = package_dir / 'HTDT.exe'
    info_path = package_dir / BUILD_INFO_REL
    if not exe.is_file() or not info_path.is_file():
        # Not "failed" — the package simply is not built here. The caller
        # (manifest capability probe) treats this as environment-unsupported.
        print(
            f'[package-smoke] no built package at {package_dir} '
            '(run scripts/build-native.ps1 first)'
        )
        return 2

    try:
        build_info = json.loads(info_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        print(f'[package-smoke] unreadable build_info.json: {exc}')
        return 2
    if not isinstance(build_info, dict):
        print('[package-smoke] build_info.json is not an object')
        return 2

    pkg_commit = build_info.get('commit_sha')
    if not pkg_commit or not isinstance(pkg_commit, str):
        problems.append('build_info.json has no commit_sha')
    head = _git_head(repo_root)
    if head is None:
        print('[package-smoke] could not resolve git HEAD', file=sys.stderr)
        return 2
    if pkg_commit and pkg_commit != head:
        problems.append(
            f'packaged commit {pkg_commit[:12]} != HEAD {head[:12]} — the '
            'package was built from a different revision than the one being '
            'verified'
        )

    # Lock binding: the closure the build claims must equal the tree's lock.
    lock_path = repo_root / LOCK_REL
    pkg_lock_sha = build_info.get('lock_sha256')
    if not pkg_lock_sha:
        problems.append('build_info.json has no lock_sha256')
    elif not lock_path.is_file():
        problems.append(f'repo lock file missing: {LOCK_REL}')
    else:
        tree_lock_sha = _sha256(lock_path)
        if tree_lock_sha != str(pkg_lock_sha).lower():
            problems.append(
                'packaged lock_sha256 does not match '
                f'{LOCK_REL} (build={str(pkg_lock_sha)[:16]}… '
                f'tree={tree_lock_sha[:16]}…)'
            )

    # --version round-trip: the packaged executable must run and report the
    # display version derived from the same identity fields.
    try:
        proc = subprocess.run(
            [str(exe), '--version'],
            capture_output=True, text=True, timeout=args.timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        problems.append(f'HTDT.exe --version did not complete: {exc}')
    else:
        if proc.returncode != 0:
            problems.append(
                f'HTDT.exe --version exited {proc.returncode}: '
                f'{(proc.stderr or proc.stdout).strip()[:200]}'
            )
        else:
            out = proc.stdout.strip()
            match = DISPLAY_VERSION_RE.search(out)
            if match is None:
                problems.append(
                    f'unexpected --version output: {out[:200]!r} — expected '
                    '<version>+g<sha>[.dirty]'
                )
            elif pkg_commit and not pkg_commit.startswith(match.group('sha')):
                problems.append(
                    f'--version reports sha {match.group("sha")} but '
                    f'build_info commit_sha is {pkg_commit[:12]}'
                )
            if match is not None and bool(match.group('dirty')) != bool(
                build_info.get('dirty')
            ):
                problems.append('--version dirty flag disagrees with build_info')

    if problems:
        print(f'[package-smoke] FAILED ({len(problems)}):')
        for p in problems:
            print(f'  - {p}')
        return 1
    print(
        f'[package-smoke] PASS — {exe.name} matches HEAD {head[:12]} '
        f'(lock {str(pkg_lock_sha)[:12]}…)'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
