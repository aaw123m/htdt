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
import tempfile


def _isolate_default_data_dir() -> None:
    worker = os.environ.get('PYTEST_XDIST_WORKER', 'main')
    root = Path(tempfile.gettempdir()) / 'htdt-test-data' / worker
    root.mkdir(parents=True, exist_ok=True)
    os.environ['LOCALAPPDATA'] = str(root)


_isolate_default_data_dir()
