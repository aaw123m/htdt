"""Print the backend test files assigned to one CI shard.

Splits ``test_*.py`` files across N shards greedily by estimated test count
(``def test_*`` occurrences), so each shard runs a roughly balanced slice of
the suite on its own runner. Deterministic: same tree -> same split.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_TEST_DEF = re.compile(r"^\s*(?:async\s+def|def)\s+test_", re.MULTILINE)


def assign(tests_dir: Path, num_shards: int) -> list[list[Path]]:
    files = sorted(tests_dir.glob("test_*.py"))
    weighted = sorted(
        ((len(_TEST_DEF.findall(f.read_text(encoding="utf-8", errors="ignore"))), f) for f in files),
        key=lambda item: (-item[0], item[1].as_posix()),
    )
    shards: list[list[Path]] = [[] for _ in range(num_shards)]
    loads = [0] * num_shards
    for weight, path in weighted:
        index = loads.index(min(loads))
        shards[index].append(path)
        loads[index] += weight
    return shards


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tests_dir", type=Path)
    parser.add_argument("--num-shards", type=int, required=True)
    parser.add_argument("--shard", type=int, required=True, help="1-based shard index")
    args = parser.parse_args()
    if not 1 <= args.shard <= args.num_shards:
        parser.error("--shard must be between 1 and --num-shards")
    for path in assign(args.tests_dir, args.num_shards)[args.shard - 1]:
        print(path.as_posix())
    return 0


if __name__ == "__main__":
    sys.exit(main())
