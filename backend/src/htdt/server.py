from __future__ import annotations

from .main import app
from .security import install_local_request_boundary


# The supported local launcher serves this ASGI app. Keep the data/API app itself
# reusable in tests while enforcing the browser-facing localhost boundary here.
install_local_request_boundary(app)
