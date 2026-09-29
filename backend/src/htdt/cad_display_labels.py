"""Human-first entity labels for primary UI surfaces (#578).

Policy (from the #578 review contract):
1. primary label = human name/role + meaningful state/time;
2. secondary disambiguator = short date/version/status;
3. raw ids/hashes only under details/copy technical info unless no human
   identity exists;
4. unnamed revisions/plans receive generated human labels such as
   ``2026年9月24日 18:42 の保存`` instead of leaking hash/UUID fragments;
5. selection widgets retain exact ids as hidden data — never fuzzy-resolve
   a selection by display text (widgets keep ids in ``UserRole``).

These helpers are pure text functions: Qt widgets import them to build
item labels while storing the exact authority id in item data.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Protocol

from .localization import format_datetime


class _HasLabel(Protocol):
    label: str


def saved_label(created_at_utc: str) -> str:
    """Generated human label for an unnamed saved authority (#578).

    ``2026年9月24日 18:42 の保存`` — derived from the ISO-8601 UTC timestamp
    every persisted authority carries. Falls back to the raw timestamp
    string rather than fabricating an identity when it is not parseable.
    """

    try:
        parsed = datetime.fromisoformat(created_at_utc.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        return created_at_utc
    return f'{format_datetime(parsed)} の保存'


def named_or_saved_label(name: str | None, created_at_utc: str) -> str:
    """Primary human label: explicit name, else the generated saved label."""

    if name:
        return name
    return saved_label(created_at_utc)


def revision_display_label(
    revision: Any,
    labels: Mapping[str, _HasLabel] | None = None,
) -> str:
    """Human label for a SceneRevision/summary (#578).

    User-assigned history label wins (policy 1); unnamed revisions get the
    generated saved label (policy 4) — never a truncated ``revision_id``.
    ``revision`` is any object carrying ``revision_id`` and
    ``created_at_utc`` (``SceneRevision`` or ``SceneRevisionSummary``);
    ``labels`` is ``SceneRepository.revision_labels(document_id)``.
    """

    record = labels.get(revision.revision_id) if labels else None
    if record is not None and record.label:
        return record.label
    return saved_label(revision.created_at_utc)


def variant_display_label(variant: Any) -> str:
    """Human label for a ``SystemVariant``: name + saved date (policy 1/2)."""

    return f'{variant.name} · {saved_label(variant.created_at_utc)}'


def spec_display_label(name: str | None, created_at_utc: str) -> str:
    """Label for a named-or-unnamed persisted spec (search spec, O90, ...)."""

    return named_or_saved_label(name, created_at_utc)


def format_versioned_label(prefix: str, version: str, created_at_utc: str) -> str:
    """`{prefix} {version} · {saved date}` for versioned plans/reports."""

    return f'{prefix} {version} · {saved_label(created_at_utc)}'


__all__ = [
    'format_versioned_label',
    'named_or_saved_label',
    'revision_display_label',
    'saved_label',
    'spec_display_label',
    'variant_display_label',
]
