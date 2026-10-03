"""Shared ``QTreeWidgetItem`` data role for entity ids on tree rows.

The optimization controllers and panels stash entity/candidate ids on
tree items under this role. It lived in ``native_editor.py`` until the
legacy ``--legacy-ui`` window chain was deleted (REV36-UX140C); the
shared constant moved here so the surviving consumers keep one role.
"""

from __future__ import annotations

from PySide6.QtCore import Qt

ROLE = int(Qt.ItemDataRole.UserRole)
