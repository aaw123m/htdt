"""Home Theater Digital Twin backend."""

# Canonical application version — the single source of truth for the release
# identity. pyproject.toml derives the package version from this attribute
# (setuptools ``dynamic``), installer tooling parses this file, and
# ``HTDT.exe --version`` reports it (with a ``+g<sha>`` build suffix when the
# commit is known). After a stable release, ``main`` carries the next
# development version (e.g. ``0.2.0.dev0`` following stable ``0.1.0``).
# See docs/RELEASING.md.
__version__ = "0.2.0.dev0"

# PySide6's signature import hook unwraps each module it audits; pydantic's
# lazy ``__getattr__`` migration then re-enters ``_internal._validators`` while
# it is still initializing, so any htdt module whose import chain reaches
# pydantic *after* QtCore is loaded dies with a circular-import ImportError.
# Finalizing the lazy chain here — ``pydantic.errors`` first, which loads the
# chain safely even when the hook is already installed — makes direct
# ``import htdt.<gui module>`` order-independent.
import pydantic.errors  # noqa: F401
from pydantic import Field as _Field  # noqa: F401

from .migration_guard import install_migration_guard

install_migration_guard()
