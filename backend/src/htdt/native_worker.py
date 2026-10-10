"""#807 boundary: worker machinery's canonical home is ``htdt.worker_pool``.

``native_worker`` stays as the application-root alias so existing
``htdt.native_worker`` import paths (and references by module name —
monkeypatched helpers, atexit plumbing) keep resolving: importing this name
returns the ``worker_pool`` module object itself, so reads *and writes* land
on the real module. Packaged layers import ``htdt.worker_pool`` directly.
"""

import sys as _sys

import htdt.worker_pool as _impl

_sys.modules[__name__] = _impl
