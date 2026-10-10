"""Shared progress/cancellation contracts for package builders (#985).

The review/proposal builders run on a bounded worker lane; each emitted
``PackageBuildProgress`` is a *measured* observation (stage index, units
processed so far, bytes written) — never an estimate or a percentage guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True, slots=True)
class PackageBuildProgress:
    """One measured progress observation from a package builder.

    ``stage_index`` is 1-based inside ``stage_count``. ``done_units`` /
    ``total_units`` carry real attempted/processed counts (e.g. frames),
    and ``unit_label`` names the unit. ``bytes_written`` is cumulative.
    """

    stage_label: str
    stage_index: int
    stage_count: int
    done_units: int | None = None
    total_units: int | None = None
    unit_label: str | None = None
    bytes_written: int = 0


# Callback signature accepted by the builders; invoked on the worker thread.
ProgressCallback = Callable[[PackageBuildProgress], None]


class ExportCancelledError(Exception):
    """Raised inside a builder when the cooperative cancel flag is set."""
