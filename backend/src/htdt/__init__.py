"""Home Theater Digital Twin backend."""

# Canonical application version — the single source of truth for the release
# identity. pyproject.toml derives the package version from this attribute
# (setuptools ``dynamic``), installer tooling parses this file, and
# ``HTDT.exe --version`` reports it (with a ``+g<sha>`` build suffix when the
# commit is known). After a stable release, ``main`` carries the next
# development version (e.g. ``0.2.0.dev0`` following stable ``0.1.0``).
# See docs/RELEASING.md.
__version__ = "0.2.0.dev0"

from .migration_guard import install_migration_guard

install_migration_guard()
