"""Calibration domain package (#954).

Documented import direction — dependencies may only point downward:

    ui -> services -> persistence -> domain

(within a layer, modules may reference siblings). Calibration has no
dedicated ui layer: its user-facing surfaces live in the shared
application pages (``application_pages``, ``room_workspace``), and the
wizard is a headless state machine — services is the highest layer this
package ships. Anything reaching back upward — e.g. the recorded
``cad_calibration_wizard_repository -> cad_calibration_wizard`` edges —
is managed debt inventoried by ``scripts/package_boundary_audit.py``
(``--diff`` against ``scripts/package_boundary_inventory.json``), not
new wiring to copy. Flat ``htdt.<module>`` import paths keep resolving
through the compat shims left at the old locations; new code should
import the canonical ``htdt.calibration.<layer>.<module>`` paths
directly.
"""
