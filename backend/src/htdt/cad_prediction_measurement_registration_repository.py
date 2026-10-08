"""#954 compatibility shim — canonical home is ``htdt.measurement.persistence.cad_prediction_measurement_registration_repository``.

Kept so existing ``htdt.cad_prediction_measurement_registration_repository`` import paths (and references by module
name — monkeypatched helpers, serialized attributes) keep resolving while
the domain split lands: importing this name returns the canonical module
object itself, so reads *and writes* land on the real module. Remove in a
later #807 slice once external importers migrate.
"""

import sys as _sys

import htdt.measurement.persistence.cad_prediction_measurement_registration_repository as _impl

_sys.modules[__name__] = _impl
