"""Developer/synthetic acceptance lane gating (#901).

Synthetic-fixture acceptance (``development_synthetic`` plans,
``synthetic_fixture`` capabilities/evidence) is an engineering/CI lane,
not an operator workflow. In an ordinary production session the
Optimize surface must be real-project first: no synthetic capability
creation, no development execution scope offered, and existing
synthetic artifacts filtered from normal selectors/lists.

``HTDT_DEVELOPER_MODE`` is the single deliberate, visibly non-production
opt-in and is off by default. Developer mode only controls *presentation
and affordances* — it never weakens the scientific gate: synthetic
evidence still cannot satisfy production owned-room recommendations,
and historical synthetic artifacts are never rewritten or hidden from
their stored records.
"""

from __future__ import annotations

import os

_DEVELOPER_MODE_ENV = 'HTDT_DEVELOPER_MODE'
_TRUTHY = frozenset({'1', 'true', 'yes', 'on'})


def developer_mode_enabled() -> bool:
    """True only when the explicit developer lane is enabled (default off)."""

    return os.environ.get(_DEVELOPER_MODE_ENV, '').strip().lower() in _TRUTHY


__all__ = ['developer_mode_enabled']
