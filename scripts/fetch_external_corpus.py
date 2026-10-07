"""Deterministic fetch/import command for the external-validation corpus
(#836 Action 1).

Dataset payloads are never vendored into the repository. This command
derives the fetch plan from the sealed manifest
(:mod:`htdt.cad_external_corpus_manifest`) — every URI, size and checksum
comes from the manifest, nothing is typed in by hand — downloads each
file under ``--target-dir`` and verifies it against the pinned MD5 /
SHA-256. A file that fails verification is deleted, not kept: bytes that
do not match the manifest must never be mistaken for corpus data.

Usage (from the repo root, with the backend venv on the interpreter):

    python scripts/fetch_external_corpus.py --target-dir D:/corpus
    python scripts/fetch_external_corpus.py --target-dir D:/corpus --dry-run
    python scripts/fetch_external_corpus.py --target-dir D:/corpus \
        --dataset bras-rs8

Exit status is 0 only when every planned file verifies (or, with
``--verify-existing``, every already-present file verifies). A JSONL
receipt is written to ``<target-dir>/corpus_fetch_receipt.jsonl`` — one
:class:`CorpusFileReceipt` line per file, in manifest order.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend' / 'src'))

from htdt.cad_external_corpus_manifest import (  # noqa: E402
    CorpusFetchStep,
    corpus_fetch_plan,
    external_corpus_manifest,
    verify_fetched_file,
)

RECEIPT_NAME = 'corpus_fetch_receipt.jsonl'


def _fetch(step: CorpusFetchStep, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(step.uri) as response:
        tmp = target.with_suffix(target.suffix + '.part')
        try:
            with tmp.open('wb') as handle:
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    handle.write(chunk)
            tmp.replace(target)
        finally:
            if tmp.exists():
                tmp.unlink()


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--target-dir',
        required=True,
        help='directory the corpus payloads are fetched into',
    )
    parser.add_argument(
        '--dataset',
        action='append',
        default=None,
        help=(
            'restrict the plan to one dataset admission id '
            '(repeatable; default: every fetchable dataset)'
        ),
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='print the fetch plan without downloading anything',
    )
    parser.add_argument(
        '--verify-existing',
        action='store_true',
        help=(
            'do not download; verify files already present under '
            '--target-dir against the manifest pins'
        ),
    )
    args = parser.parse_args(argv)

    manifest = external_corpus_manifest()
    steps = corpus_fetch_plan(manifest, args.dataset)
    target_root = Path(args.target_dir)

    if args.dry_run:
        for step in steps:
            print(
                f'{step.target_relpath}\t{step.size_bytes}\t'
                f'md5={step.md5 or "-"}\tsha256={step.sha256 or "-"}\t'
                f'{step.uri}'
            )
        print(f'{len(steps)} files planned', file=sys.stderr)
        return 0

    receipts_path = target_root / RECEIPT_NAME
    target_root.mkdir(parents=True, exist_ok=True)
    failures = 0
    with receipts_path.open('w', encoding='utf-8', newline='\n') as out:
        for step in steps:
            target = target_root / step.target_relpath
            if not args.verify_existing:
                print(f'fetching {step.target_relpath}', file=sys.stderr)
                _fetch(step, target)
            receipt = verify_fetched_file(step, target)
            if receipt.verdict != 'verified':
                failures += 1
                if (
                    receipt.verdict
                    in ('md5_mismatch', 'sha256_mismatch', 'size_mismatch')
                    and target.is_file()
                ):
                    target.unlink()
            out.write(
                json.dumps(receipt.model_dump(mode='json')) + '\n'
            )
            out.flush()
            print(
                f'{receipt.verdict}\t{step.target_relpath}',
                file=sys.stderr,
            )
    print(
        f'{len(steps) - failures}/{len(steps)} files verified; '
        f'receipts: {receipts_path}',
        file=sys.stderr,
    )
    return 0 if failures == 0 else 1


if __name__ == '__main__':
    raise SystemExit(run())
