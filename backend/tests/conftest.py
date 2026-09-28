"""Suite-wide test isolation.

``htdt.main`` builds ``app = create_app()`` at module import time, and
``create_app()`` opens the *default* data dir. Under pytest-xdist every
worker imports test modules concurrently, so workers race on one shared
``cad.sqlite3`` — one worker sees the database mid-initialization (no
``schema_version``) and collection fails with ``MigrationOpenError``,
which xdist then reports as "different tests were collected between
workers".

Setting ``LOCALAPPDATA`` here — before any test module is imported —
gives each pytest process its own default data dir, removing the race
and isolating tests from the developer's real data. ``LOCALAPPDATA`` is
the only variable ``htdt.main._default_data_dir`` reads besides the home
directory, so this also applies on POSIX runners.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile


def _shorten_temp_root() -> None:
    # Windows MAX_PATH: the default temp root (~50 chars under
    # %LOCALAPPDATA%\Temp) plus pytest's per-test directories pushes deeply
    # nested fixture paths past 260 chars and flakes tests like
    # test_evidence_lifecycle_rejects_illegal_promotions. Conftests for the
    # collected testpaths load before pytest resolves basetemp, so fixing
    # the temp env here works for every worker.
    if os.name != 'nt':
        return
    # Git Bash exports like TMPDIR=/c/t never reach Windows: python.exe sees
    # the literal POSIX path, resolves it to a nonexistent '\c\t', and
    # tempfile silently falls back to the long default root.
    for var in ('TMP', 'TMPDIR', 'TEMP'):
        value = os.environ.get(var)
        if value:
            match = re.match(r'^/([a-zA-Z])/(.*)$', value)
            if match:
                drive = match.group(1).upper()
                tail = match.group(2).replace('/', os.sep)
                os.environ[var] = f'{drive}:\\{tail}'
    if not os.environ.get('TMPDIR'):
        short_root = Path(os.environ.get('SystemDrive', 'C:') + '\\t')
        try:
            short_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        for var in ('TMP', 'TMPDIR', 'TEMP'):
            os.environ[var] = str(short_root)
    tempfile.tempdir = None


_shorten_temp_root()


def _isolate_default_data_dir() -> None:
    worker = os.environ.get('PYTEST_XDIST_WORKER', 'main')
    root = Path(tempfile.gettempdir()) / 'htdt-test-data' / worker
    root.mkdir(parents=True, exist_ok=True)
    os.environ['LOCALAPPDATA'] = str(root)


_isolate_default_data_dir()
