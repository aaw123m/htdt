"""Print the backend test items assigned to one CI shard.

Splits ``test_*.py`` files across N shards greedily so each shard runs a
roughly balanced slice of the suite on its own runner. Deterministic:
same inputs -> same split.

Output lines are pytest selectors; a line prefixed ``deselect:`` marks a
``--deselect`` argument for this shard instead (used when a heavy file's
recorded tests are exported to other shards: the file stays here minus
the exported nodes, so tests added later still run on the home shard).

Weighting:

* With ``--weights`` (a JSON map of pytest node id -> seconds, produced
  from ``pytest --durations 0`` output) files are balanced by recorded
  serial time. Files heavier than a shard's fair share are split at test
  granularity. Files missing from the map fall back to ``def test_*``
  count times the median per-test duration.
* Without ``--weights`` the estimator is the raw test-def count, which
  under-balances when test durations are skewed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_TEST_DEF = re.compile(r"^\s*(?:async\s+def|def)\s+test_", re.MULTILINE)


def _def_count(path: Path) -> int:
    return len(_TEST_DEF.findall(path.read_text(encoding="utf-8", errors="ignore")))


def _load_weights(path: Path | None) -> dict[str, float]:
    if path is None or not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {node: float(secs) for node, secs in raw.items()}


def assign(
    tests_dir: Path,
    num_shards: int,
    weights: dict[str, float] | None = None,
    only: set[str] | None = None,
) -> list[list[str]]:
    """Return per-shard lines: file paths, node ids, or ``deselect:<node>``.

    ``only`` restricts the candidate set to the given file names (basename
    match); used for changed-file test selection.
    """
    weights = weights or {}
    files = sorted(tests_dir.glob("test_*.py"))
    if only is not None:
        files = [f for f in files if f.name in only]

    # Group recorded node durations by test file basename.
    by_file: dict[str, list[tuple[str, float]]] = {}
    for node, secs in weights.items():
        file_part = node.split("::", 1)[0].replace("\\", "/")
        by_file.setdefault(file_part.rsplit("/", 1)[-1], []).append((node, secs))

    known_secs = [secs for entries in by_file.values() for _, secs in entries]
    median_secs = sorted(known_secs)[len(known_secs) // 2] if known_secs else 1.0

    file_weight: dict[Path, float] = {}
    for f in files:
        entries = by_file.get(f.name)
        if entries:
            file_weight[f] = sum(secs for _, secs in entries)
        else:
            file_weight[f] = max(1, _def_count(f)) * median_secs

    total = sum(file_weight.values())
    budget = total / num_shards if num_shards else total

    shards: list[list[str]] = [[] for _ in range(num_shards)]
    loads = [0.0] * num_shards

    def _put(item: tuple[float, str]) -> int:
        index = loads.index(min(loads))
        shards[index].append(item[1])
        loads[index] += item[0]
        return index

    # LPT over whole files; any file that alone exceeds a shard's fair share
    # is dealt with afterwards at node granularity.
    heavy: list[Path] = []
    whole: list[tuple[float, str]] = []
    for f, weight in file_weight.items():
        if weight > budget:
            heavy.append(f)
        else:
            whole.append((weight, f.as_posix()))
    whole.sort(key=lambda item: (-item[0], item[1]))
    for item in whole:
        _put(item)

    # Split heavy files: LPT their recorded nodes across shards, then park the
    # file itself on the shard holding the most of its node weight and
    # deselect the nodes that landed elsewhere. Tests not present in the
    # weights file (new ones) still run on the home shard.
    for f in sorted(heavy, key=lambda p: -file_weight[p]):
        entries = by_file[f.name]
        node_home: dict[str, int] = {}
        for node, secs in sorted(entries, key=lambda e: (-e[1], e[0])):
            node_home[node] = _put((secs, node))
        home: dict[int, float] = {}
        for node, secs in entries:
            home[node_home[node]] = home.get(node_home[node], 0.0) + secs
        home_shard = max(home, key=lambda i: (home[i], -i))
        exported = [node for node in entries if node_home[node[0]] != home_shard]
        # Home-owned nodes already run via the file itself — drop their
        # individual selectors so they do not execute twice on this shard.
        kept = {node for node in entries if node_home[node[0]] == home_shard}
        shards[home_shard] = [s for s in shards[home_shard] if s not in {n for n, _s in kept}]
        for node, _secs in exported:
            shards[home_shard].append(f"deselect:{node}")
        shards[home_shard].append(f.as_posix())
        loads[home_shard] += file_weight[f] * 0.05  # fixture/unknown-test slack
    return shards


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tests_dir", type=Path)
    parser.add_argument("--num-shards", type=int, required=True)
    parser.add_argument("--shard", type=int, required=True, help="1-based shard index")
    parser.add_argument(
        "--weights",
        type=Path,
        default=None,
        help="JSON map of pytest node id -> recorded seconds (from --durations 0)",
    )
    parser.add_argument(
        "--only",
        type=Path,
        default=None,
        help="file listing test file names (one per line) to restrict the shard to",
    )
    args = parser.parse_args()
    if not 1 <= args.shard <= args.num_shards:
        parser.error("--shard must be between 1 and --num-shards")
    only = None
    if args.only is not None:
        only = {
            line.strip().rsplit("/", 1)[-1]
            for line in args.only.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    for line in assign(args.tests_dir, args.num_shards, _load_weights(args.weights), only)[args.shard - 1]:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
