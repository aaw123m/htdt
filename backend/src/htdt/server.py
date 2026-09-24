from __future__ import annotations

import os
from pathlib import Path

from .main import create_app
from .security import install_local_request_boundary


# #598: the supported launcher is development-only now that the native
# application owns the data root. The ASGI app is bound here (not in main.py)
# so importing htdt.main never touches the legacy store. Isolation is
# enforced inside create_app: without an explicit HTDT_DATA_DIR the
# HTDT_LEGACY_API opt-in must be set.
_data_dir = (
    Path(os.environ['HTDT_DATA_DIR']) if os.environ.get('HTDT_DATA_DIR') else None
)
app = create_app(_data_dir)

# The supported local launcher serves this ASGI app. Keep the data/API app itself
# reusable in tests while enforcing the browser-facing localhost boundary here.
install_local_request_boundary(app)
