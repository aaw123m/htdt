"""#954 compatibility shim — canonical home is ``htdt.capture.ui.capture_receiver_settings``.

Kept so existing ``htdt.capture_receiver_settings`` import paths (and references by module
name — monkeypatched helpers, serialized attributes) keep resolving while
the domain split lands: importing this name returns the canonical module
object itself, so reads *and writes* land on the real module. Remove in a
later #807 slice once external importers migrate.
"""

import sys as _sys

import htdt.capture.ui.capture_receiver_settings as _impl

_sys.modules[__name__] = _impl
