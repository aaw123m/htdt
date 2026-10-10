"""#807 compatibility shim — canonical home is ``htdt.measurement.persistence.cad_measurement_pose_observation_repository``.

Importing this name returns the canonical module object itself, so reads
*and writes* land on the real module. Remove once importers migrate to the
package path.
"""

import sys as _sys

import htdt.measurement.persistence.cad_measurement_pose_observation_repository as _impl

_sys.modules[__name__] = _impl
