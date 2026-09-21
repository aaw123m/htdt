"""Build/source identity for HTDT binaries and source checkouts.

The canonical application version is ``htdt.__version__`` (declared in
``htdt/__init__.py``). This module derives the *display version* that
identifies the exact producing build:

    <version>            e.g. "0.2.0.dev0"
    <version>+g<sha8>    when the source commit is known
    ...+.dirty           when the source tree had uncommitted changes

Build metadata resolution order:

1. ``HTDT_BUILD_INFO`` environment variable pointing at a build-info JSON
   file (explicit override for packaging tools and tests).
2. ``sys._MEIPASS/htdt_build/build_info.json`` inside a PyInstaller
   package (written by ``scripts/build-native.ps1``).
3. ``git`` metadata of the source checkout containing this file.
4. No build identity: the display version is the plain canonical version.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from . import __version__


BUILD_INFO_DIR = 'htdt_build'
BUILD_INFO_NAME = 'build_info.json'
BUILD_INFO_ENV_VAR = 'HTDT_BUILD_INFO'
SHORT_SHA_LENGTH = 8


@dataclass(frozen=True)
class BuildInfo:
    """Resolved identity of the code producing this runtime."""

    version: str
    commit_sha: str | None = None
    build_id: str | None = None
    dirty: bool = False
    source: str = 'unknown'

    @property
    def short_sha(self) -> str | None:
        if not self.commit_sha:
            return None
        return self.commit_sha[:SHORT_SHA_LENGTH]

    @property
    def display_version(self) -> str:
        """PEP 440 version string with a ``+g<sha8>[.dirty]`` local segment."""

        if not self.short_sha:
            return self.version
        display = f'{self.version}+g{self.short_sha}'
        if self.dirty:
            display = f'{display}.dirty'
        return display

    def as_dict(self) -> dict[str, Any]:
        return {
            'version': self.version,
            'display_version': self.display_version,
            'commit_sha': self.commit_sha,
            'build_id': self.build_id,
            'dirty': self.dirty,
            'source': self.source,
        }


def _load_build_info_file(path: Path, *, source: str) -> BuildInfo | None:
    try:
        # utf-8-sig tolerates a BOM written by Windows PowerShell 5.1 tooling.
        payload = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    commit_sha = payload.get('commit_sha')
    build_id = payload.get('build_id')
    return BuildInfo(
        version=str(payload.get('version') or __version__),
        commit_sha=str(commit_sha) if commit_sha else None,
        build_id=str(build_id) if build_id else None,
        dirty=bool(payload.get('dirty', False)),
        source=str(payload.get('source') or source),
    )


def _repo_root() -> Path:
    # backend/src/htdt/build_info.py -> repository root
    return Path(__file__).resolve().parents[3]


def _git_metadata(root: Path) -> tuple[str | None, bool]:
    """Return (full commit SHA, dirty flag) for a source checkout."""

    if not (root / '.git').exists():
        return None, False
    try:
        commit = subprocess.run(
            ['git', '-C', str(root), 'rev-parse', 'HEAD'],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ['git', '-C', str(root), 'status', '--porcelain'],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None, False
    if not commit:
        return None, False
    return commit, bool(status)


def _load_build_info() -> BuildInfo:
    override = os.environ.get(BUILD_INFO_ENV_VAR)
    if override:
        info = _load_build_info_file(Path(override), source='override')
        if info is not None:
            return info

    bundle_root = getattr(sys, '_MEIPASS', None)
    if bundle_root is not None:
        info = _load_build_info_file(
            Path(bundle_root) / BUILD_INFO_DIR / BUILD_INFO_NAME,
            source='packaged',
        )
        if info is not None:
            return info
        return BuildInfo(version=__version__, source='packaged')

    commit_sha, dirty = _git_metadata(_repo_root())
    return BuildInfo(
        version=__version__,
        commit_sha=commit_sha,
        build_id=os.environ.get('GITHUB_RUN_ID') or None,
        dirty=dirty,
        source='checkout' if commit_sha else 'unknown',
    )


@lru_cache(maxsize=1)
def get_build_info() -> BuildInfo:
    """Return the build identity of the running code (cached per process)."""

    return _load_build_info()


def version_string() -> str:
    """Display version reported by ``--version``, installers and manifests."""

    return get_build_info().display_version
