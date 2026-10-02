"""Choose which backend test files a pull request must run.

Reads the diff between the PR base commit and HEAD, and emits JSON::

    {"mode": "full" | "subset" | "none", "files": ["backend/tests/test_x.py", ...]}

Modes:

* ``full``   — run the whole suite: shared/core files changed, the diff is
               unavailable, or a change cannot be mapped confidently.
* ``subset`` — run only the listed test files: every backend change maps to
               identifiable test files.
* ``none``   — no change can affect the backend suite at all (docs-only,
               frontend-only, asset-only PRs).

Mapping rules:

* ``backend/tests/test_*.py``            -> that file itself.
* ``backend/src/htdt/<mod>.py``          -> every ``test_*.py`` whose stem
  contains ``<mod>`` or is contained by it (either direction), e.g.
  ``cad_system_health.py`` selects ``test_cad_system_health*.py`` and
  ``test_cad_system_health_repository*.py``.  Containment is intentionally
  over-inclusive: missing coverage is worse than extra coverage.
* Shared/core files                      -> ``full``.  These cross-cut the
  suite (schema DDL/migration, storage maintenance, data management,
  project lifecycle, native CAD entry, working document, any conftest,
  dependency manifests, scripts, installer, workflows, benchmarks).
* ``backend/**`` not matching the above  -> ``full`` (unknown blast radius).
* Anything outside ``backend/`` and the core set -> ignored.

Safety rule: when in doubt, choose ``full``.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# Repo-relative paths (or prefixes) whose change means "everything is
# potentially affected" — schema, migration, packaging, CI, shared fixtures.
_CORE_PREFIXES = (
    "scripts/",
    "installer/",
    ".github/",
    "benchmarks/",
    "backend/tests/conftest.py",
    "backend/tests/",
    "conftest.py",
)
_CORE_EXACT = {
    "backend/pyproject.toml",
    "backend/requirements-n05-windows.lock",
}
_CORE_GLOBS = (
    re.compile(r"^backend/requirements.*"),
    re.compile(r"(^|/)conftest\.py$"),
)
# backend/src/htdt modules that practically everything depends on.
_CORE_MODULES = {
    "__init__",
    "cad_schema",
    "cad_schema_ddl",
    "cad_schema_migrations",
    "native_upgrade",
    "storage_maintenance",
    "data_management",
    "project_lifecycle",
    "native_cad",
    # Broadly shared foundations — nearly every test exercises them.
    "__main__",
    "database",
    "models",
    "main",
    "server",
}
_RELEVANT_AREAS = ("backend/",)


def _changed_files(base: str) -> list[str] | None:
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", base, "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    return [line.strip().replace("\\", "/") for line in out.stdout.splitlines() if line.strip()]


def _is_core(path: str) -> bool:
    if any(path.startswith(p) for p in _CORE_PREFIXES):
        # backend/tests/* changed → those files are selected individually,
        # not core. (conftest.py under tests still matches _CORE_GLOBS below.)
        if path.startswith("backend/tests/") and not path.endswith("conftest.py"):
            return False
        return True
    if path in _CORE_EXACT:
        return True
    if any(rx.search(path) for rx in _CORE_GLOBS):
        return True
    m = re.match(r"^backend/src/htdt/([A-Za-z0-9_]+)\.py$", path)
    if m and m.group(1) in _CORE_MODULES:
        return True
    return False


def _stem(path: str) -> str:
    return Path(path).stem


def _select(changed: list[str], tests_dir: Path) -> dict:
    test_files = sorted(tests_dir.glob("test_*.py"))
    test_stems = {f: _stem(f.name)[len("test_"):] for f in test_files}

    selected: set[Path] = set()
    for path in changed:
        if _is_core(path):
            return {"mode": "full", "files": []}
        if not path.startswith(_RELEVANT_AREAS):
            continue  # docs/frontend/assets/etc — cannot affect the suite
        if path.startswith("backend/tests/"):
            name = Path(path).name
            if name.startswith("test_") and name.endswith(".py"):
                cand = tests_dir / name
                if cand.exists():
                    selected.add(cand)
                    continue
            # new/renamed non-test file under tests — play safe
            return {"mode": "full", "files": []}
        m = re.match(r"^backend/src/htdt/([A-Za-z0-9_]+)\.py$", path)
        if m:
            mod = m.group(1)
            if "_" in mod:
                hits = [
                    f
                    for f, stem in test_stems.items()
                    if mod in stem or stem in mod
                ]
            else:
                # Single-token modules ("main", "database", ...): substring
                # matching would pull in unrelated stems like "maintenance",
                # so require a word boundary instead — the token may sit at
                # the start, end, or middle of the stem, just never glued to
                # another token.
                hits = [
                    f
                    for f, stem in test_stems.items()
                    if stem == mod
                    or stem.startswith(mod + "_")
                    or stem.endswith("_" + mod)
                    or ("_" + mod + "_") in stem
                ]
            # Widen with a shared leading token prefix (kept to >= 2 tokens so
            # a "cad"-only prefix never selects half the suite):
            # capture_ingestion_transaction -> test_capture_ingestion_*.
            tokens = mod.split("_")
            while len(tokens) > 2:
                tokens.pop()
                prefix = "_".join(tokens)
                hits += [f for f, stem in test_stems.items() if stem.startswith(prefix)]
            if not hits:
                # A module with no identifiable dedicated tests — its change
                # could break anything, so run the full suite.
                return {"mode": "full", "files": []}
            selected.update(hits)
            continue
        # Any other change under backend/ (subpackages, data files, ...):
        # blast radius unknown → full suite.
        return {"mode": "full", "files": []}

    if not selected:
        return {"mode": "none", "files": []}
    return {"mode": "subset", "files": [f.as_posix() for f in sorted(selected)]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=None, help="base commit/ref to diff against; omit for full")
    parser.add_argument("--tests-dir", type=Path, required=True)
    parser.add_argument("--out-file", type=Path, default=None, help="write JSON here as well as stdout")
    args = parser.parse_args()

    if not args.base:
        result = {"mode": "full", "files": []}
    else:
        changed = _changed_files(args.base)
        if changed is None:
            result = {"mode": "full", "files": []}
        else:
            result = _select(changed, args.tests_dir)

    payload = json.dumps(result)
    print(payload)
    if args.out_file:
        args.out_file.write_text(payload + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
