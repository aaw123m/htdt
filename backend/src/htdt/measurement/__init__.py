"""Measurement domain package (#954).

Documented import direction — dependencies may only point downward:

    ui -> services -> persistence -> domain

(within a layer, modules may reference siblings). Anything reaching back
upward — e.g. the recorded ``cad_measurement_repository ->
cad_measurement_loop`` edge — is managed debt inventoried by
``scripts/package_boundary_audit.py`` (``--diff`` against
``scripts/package_boundary_inventory.json``), not new wiring to copy.
Flat ``htdt.<module>`` import paths keep resolving through the compat
shims left at the old locations; new code should import the canonical
``htdt.measurement.<layer>.<module>`` paths directly.
"""
