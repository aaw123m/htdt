"""#954 compatibility shim — canonical home is ``htdt.acoustics.domain.cad_pffdtd_resource_estimator``.

Kept so existing ``htdt.cad_pffdtd_resource_estimator`` import paths (and references by module
name — monkeypatched helpers, serialized attributes) keep resolving while
the domain split lands: importing this name returns the canonical module
object itself, so reads *and writes* land on the real module. Remove in a
later #807 slice once external importers migrate.
"""

import sys as _sys

import htdt.acoustics.domain.cad_pffdtd_resource_estimator as _impl

_sys.modules[__name__] = _impl
