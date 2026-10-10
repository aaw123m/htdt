"""#954 compatibility shim — canonical home is ``htdt.acoustics.services.cad_acoustic_solver_adapter``.

Kept so existing ``htdt.cad_acoustic_solver_adapter`` import paths (and references by module
name — monkeypatched helpers, serialized attributes) keep resolving while
the domain split lands: importing this name returns the canonical module
object itself, so reads *and writes* land on the real module. Remove in a
later #807 slice once external importers migrate.
"""

import sys as _sys

import htdt.acoustics.services.cad_acoustic_solver_adapter as _impl

_sys.modules[__name__] = _impl
