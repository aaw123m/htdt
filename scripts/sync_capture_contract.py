"""Sync the vendored HTDT-Capture bundle contract into HTDT.

``backend/src/htdt/capture_contract/`` is a vendored copy of
``schemas/capture-bundle-v1/`` from the HTDT-Capture repository: the
payload schemas plus ``support-matrix.json`` the production validator
(``htdt.capture_bundle``) loads at runtime. The copy is maintained by
hand, which is exactly how it drifted behind the emitter once — the
receiver rejected real app bundles whose manifests declared payload
families the stale matrix did not know.

Usage::

    python scripts/sync_capture_contract.py --capture-repo ../HTDT-Capture
    python scripts/sync_capture_contract.py --capture-repo ../HTDT-Capture --check

``--check`` performs a read-only drift audit and exits non-zero when
the vendored copy differs from the emitter registry — use it to gate
contract bumps instead of discovering drift at import time.

Only ``*.schema.json`` and ``support-matrix.json`` are vendored; the
emitter's ``*-vectors.json`` conformance vectors and README are
capture-side test fixtures, not contract.
"""

from __future__ import annotations

import argparse
import filecmp
import json
from pathlib import Path
import shutil
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
VENDORED = REPO_ROOT / "backend" / "src" / "htdt" / "capture_contract"
EMITTER_DIR = Path("schemas") / "capture-bundle-v1"


def _emitter_files(capture_repo: Path) -> dict[str, Path]:
    src = capture_repo / EMITTER_DIR
    if not src.is_dir():
        raise SystemExit(
            f"capture contract directory not found: {src}"
        )
    files = {
        p.name: p
        for p in src.iterdir()
        if p.name == "support-matrix.json" or p.name.endswith(".schema.json")
    }
    if "support-matrix.json" not in files:
        raise SystemExit(f"no support-matrix.json under {src}")
    return files


def _check_matrix(matrix: dict, schemas: set[str]) -> list[str]:
    """Internal-consistency audit of a matrix against schema files."""
    problems: list[str] = []
    if matrix.get("schema") != "htdt.capture.bundle-support-matrix":
        problems.append("matrix has wrong schema token")
        return problems
    referenced: set[str] = set()
    for name, contract in matrix.get("families", {}).items():
        for doc in contract.get("documents", {}).values():
            referenced.add(f"{doc}.schema.json")
        if not contract.get("paths"):
            problems.append(f"family {name}: no paths declared")
        if not contract.get("documents") and not contract.get("external"):
            problems.append(f"family {name}: no documents and not external")
    for doc in sorted(referenced):
        if doc not in schemas:
            problems.append(f"matrix documents {doc} but schema file is missing")
    return problems


def audit(capture_repo: Path) -> tuple[list[str], list[str]]:
    """Return (drift_items, consistency_problems) for the vendored copy."""
    emitter = _emitter_files(capture_repo)
    vendored_schemas = {
        p.name for p in VENDORED.glob("*.schema.json")
    }
    vendored_schemas.add("support-matrix.json")

    drift: list[str] = []
    for name, src in sorted(emitter.items()):
        dst = VENDORED / name
        if not dst.exists():
            drift.append(f"missing vendored file: {name}")
        elif not filecmp.cmp(src, dst, shallow=False):
            drift.append(f"vendored file differs from emitter: {name}")
    for name in sorted(vendored_schemas - set(emitter)):
        drift.append(f"vendored file not in emitter registry: {name}")

    matrix = json.loads(
        (VENDORED / "support-matrix.json").read_text(encoding="utf-8")
    )
    problems = _check_matrix(matrix, vendored_schemas - {"support-matrix.json"})
    return drift, problems


def sync(capture_repo: Path) -> list[str]:
    emitter = _emitter_files(capture_repo)
    written: list[str] = []
    for name, src in sorted(emitter.items()):
        dst = VENDORED / name
        if not dst.exists() or not filecmp.cmp(src, dst, shallow=False):
            shutil.copyfile(src, dst)
            written.append(name)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--capture-repo",
        type=Path,
        required=True,
        help="path to an HTDT-Capture checkout",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="read-only drift audit; exit 1 on any drift or inconsistency",
    )
    args = parser.parse_args()
    capture_repo = args.capture_repo.resolve()

    if args.check:
        drift, problems = audit(capture_repo)
        for item in drift:
            print(f"DRIFT {item}")
        for item in problems:
            print(f"INCONSISTENT {item}")
        if drift or problems:
            print(
                f"{len(drift)} drift / {len(problems)} consistency problems "
                f"vs {capture_repo}"
            )
            return 1
        print("vendored contract is in sync")
        return 0

    written = sync(capture_repo)
    if written:
        print("synced:", ", ".join(written))
    else:
        print("already in sync")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
