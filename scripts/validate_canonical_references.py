# -*- coding: utf-8 -*-
"""Validate issue/PR/CI/artifact references in canonical status documents.

Enforces the post-migration reference model:

* Every repository reference must be written ``<owner>/<repo>#<number>``
  (a bare ``#N`` is invalid) so that a number can never silently flip
  meaning between the original and current repositories.
* A ``/``-separated suffix (``repo#228/#236``) and a ``–``-separated range
  (``repo#167–#176``) inherit the leading repo identity.
* Implementation/merge claims append `` (merge <8-char sha>)`` carrying the
  merge commit recorded in ``docs/MIGRATION_REFERENCE_MANIFEST.yaml``.
* Unrecoverable historical objects append `` (unavailable)``; refs replaced
  by a different pull request append `` (superseded)``.
* Diagnostic workflow runs are project artifacts, not repository objects,
  and are written ``run-<number>`` (bare ``run #N``/``Run #N`` is invalid).
* Raw URLs pointing at the offline original repositories are invalid; the
  reference is kept and the repo marked ``repo unavailable``.

The manifest is the single authority: a reference whose (repo, number)
is not in the manifest, or whose recorded merge SHA does not match, fails.

Importable API::

    manifest = load_manifest(path)
    problems = validate_text(text, doc_name, manifest)
    problems = validate_all(root_dir)

``main()`` returns a process exit code (0 clean, 1 violations).
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml is a pinned backend dep
    yaml = None

MANIFEST_NAME = "docs/MIGRATION_REFERENCE_MANIFEST.yaml"

# Canonical status documents (issue-scoped working notes under
# docs/issues/ are intentionally out of scope).
CANONICAL_DOCS = (
    "README.md",
    "docs/IMPLEMENTATION_STATUS.md",
    "docs/IMPLEMENTATION_STATUS_ARCHIVE_2026-09-16.md",
    "docs/IMPLEMENTATION_ROADMAP.md",
    "docs/PROJECT_PLAN.md",
    "docs/PRODUCT_DOMAIN_BOUNDARY_CHARTER.md",
    "docs/CAD_EDITOR_SPEC.md",
    "docs/CAD_EDITOR_ACCEPTANCE.md",
    "docs/UI_DESIGN.md",
)

_KIND_WORDS = (
    ("workflow run", "ci_run"),
    ("Windows Release Artifact", "release_artifact"),
    ("Release Artifact", "release_artifact"),
    ("Artifact", "release_artifact"),
    ("Issues", "issue"),
    ("Issue", "issue"),
    ("旧Issue", "issue"),
    ("issues", "issue"),
    ("issue", "issue"),
    ("PRs", "pull_request"),
    ("PR", "pull_request"),
    ("CI", "ci_run"),
    ("Run", "diagnostic_run"),
    ("run", "diagnostic_run"),
)

_ANNOTATION_RE = re.compile(
    r"^\s*\((?:(merge)\s+([^)]+)|(unavailable|superseded|landed))\)"
)
_CHAIN_SEP_RE = re.compile(r"[/\u2013\u30fb\u3001]")

_WORD = re.compile(r"[A-Za-z0-9_-]")


class RefToken:
    """One scanned reference occurrence."""

    __slots__ = ("kind", "repo", "number", "line", "col", "qualified",
                 "chain_member", "annotation", "raw")

    def __init__(self, kind, repo, number, line, col, qualified,
                 chain_member, annotation, raw):
        self.kind = kind
        self.repo = repo
        self.number = number
        self.line = line
        self.col = col
        self.qualified = qualified
        self.chain_member = chain_member
        self.annotation = annotation  # (name, payload) or None
        self.raw = raw

    def display(self):
        return "%s:%d: %s#%d" % (self.line, self.col,
                                 self.repo or "<unqualified>", self.number)


def _in_link_target(line, pos):
    for m in re.finditer(r"\]\(", line):
        close = line.find(")", m.start())
        if close == -1:
            close = len(line)
        if m.start() < pos <= close:
            return True
    return False


def _kind_hint(line, pos):
    """Nearest recognised kind-word before ``pos`` on the line."""
    window = line[:pos][-60:]
    best = None
    for word, kind in _KIND_WORDS:
        i = window.rfind(word)
        if i < 0:
            continue
        if i > 0 and _WORD.match(window[i - 1]):
            continue  # identifier-adjacent, not a kind word
        j = i + len(word)
        if j < len(window) and window[j].isalnum():
            continue
        cand = (j, len(word), kind)
        if best is None or (cand[0], cand[1]) > (best[0], best[1]):
            best = cand
    return best[2] if best else None


def scan_text(text):
    """Scan doc text into reference tokens.

    Returns ``(tokens, syntax_errors)`` where tokens are ``RefToken`` and
    syntax_errors are raw strings for malformed reference syntax.
    """
    tokens = []
    errors = []
    for ln, line in enumerate(text.split("\n"), 1):
        last_repo = None
        last_kind = None
        last_num = None
        for m in re.finditer(r"#(\d+)", line):
            num = int(m.group(1))
            pos = m.start()
            if _in_link_target(line, pos):
                last_repo = last_kind = last_num = None
                continue
            pre = line[:pos]
            # `run-76` already-correct form emits no `#`
            qualified_repo = None
            for known in KNOWN_REPO_HINTS:
                if pre.endswith(known):
                    qualified_repo = known
                    break
            # annotation immediately after the number
            ann = _ANNOTATION_RE.match(line[m.end():])
            annotation = (ann.group(1) or ann.group(3),
                          (ann.group(2) or "")) if ann else None
            if qualified_repo is not None:
                kind = _kind_hint(line, pos - len(qualified_repo)) or None
                tokens.append(RefToken(
                    kind, qualified_repo, num, ln, pos, True, False,
                    annotation, line))
                last_repo, last_kind, last_num = qualified_repo, kind, num
                continue
            # chain member (`/#N`, `/ #N`, `–#N`) inherits previous identity
            sep = re.search(r"([/\u2013\u30fb])\s*$", pre)
            if sep and last_repo is not None:
                tokens.append(RefToken(
                    last_kind, last_repo, num, ln, pos, True, True,
                    annotation, line))
                if sep.group(1) == "\u2013":
                    last_num = num  # range tail
                continue
            if sep:
                errors.append(
                    "%d: chain member #%d without a preceding qualified ref"
                    % (ln, num))
                continue
            errors.append(
                "%d: bare reference #%d without a repository identity"
                % (ln, num))
            last_repo = last_kind = last_num = None
        # also catch `run #N`/`Run #N`/`workflow run #N` (bare numeric runs)
        for m in re.finditer(
                r"(?<![\w/])([Rr]un|workflow run)\s+#(\d+)", line):
            if _in_link_target(line, m.start(2)):
                continue
            # qualified `workflow run repo#N` is handled above
            errors.append(
                "%d: diagnostic/CI run written as '%s #%s' — use run-%s or "
                "qualify the CI run with its repository" % (
                    ln, m.group(1), m.group(2), m.group(2)))
    return tokens, errors


# Repo slugs recognised by the scanner; the manifest is authoritative for
# which (repo, number) pairs actually resolve.
KNOWN_REPO_HINTS = (
    "bolph71656-ai/Home-Theater-Digital-Twin",
    "bolph71656-ai/HTDT-Capture",
    "ka0923s-a11y/HTDT",
    "ka0923s-a11y/HTDT-Capture",
)

_URL_RE = re.compile(r"github\.com/(bolph71656-ai/[A-Za-z0-9_-]+)")


def load_manifest(path):
    """Parse the migration reference manifest YAML.

    Returns a dict with ``refs`` keyed by (repo, number) -> entry and the
    raw document under ``doc``.
    """
    if yaml is None:
        raise RuntimeError("PyYAML is required to parse the manifest")
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    refs = {}
    for e in doc.get("references", []):
        key = (e["original_repo"], int(e["original_number"]), e["kind"])
        refs.setdefault((e["original_repo"], int(e["original_number"])),
                        []).append(e)
        refs[key] = e
    return {"doc": doc, "refs": refs}


def _resolve(manifest_refs, token, ambiguous):
    """Resolve a token to a manifest entry.

    ``ambiguous`` flags number kinds that collide between kinds on the same
    repo (e.g. issue/PR sharing a number) — then only an explicit kind hint
    can resolve.
    """
    entries = manifest_refs.get((token.repo, token.number))
    if not entries:
        return None, "not in manifest"
    kinds = {e["kind"] for e in entries}
    if len(kinds) == 1:
        # Sole manifest entry: the repo#number identity is unambiguous even
        # if a nearby word looks like a different kind (e.g. a section
        # titled "compatibility CI" citing an issue ref).
        return entries[0], None
    # multiple kinds under the same (repo, number) — need an exact hint
    if token.kind in kinds:
        for e in entries:
            if e["kind"] == token.kind:
                return e, None
    return None, "ambiguous kind for %s#%d (kinds: %s)" % (
        token.repo, token.number, "/".join(sorted(kinds)))


def validate_text(text, doc_name, manifest):
    """Validate one document's reference syntax against the manifest.

    Returns a list of ``"doc:line: message"`` strings; empty means clean.
    """
    refs = manifest["refs"]
    tokens, errors = scan_text(text)
    problems = ["%s:%s" % (doc_name, e) for e in errors]
    lines = text.split("\n")
    for tok in tokens:
        loc = "%s:%d" % (doc_name, tok.line)
        entry, err = _resolve(refs, tok, ambiguous=False)
        if err:
            problems.append("%s: %s — %s#%d" % (loc, err, tok.repo, tok.number))
            continue
        status = entry["status"]
        ann = tok.annotation
        line = lines[tok.line - 1]
        if status == "unavailable":
            if not (ann and ann[0] == "unavailable"):
                problems.append(
                    "%s: unavailable ref presented without marker — %s#%d "
                    "needs '(unavailable)'" % (loc, tok.repo, tok.number))
        elif status == "superseded":
            if not (ann and ann[0] == "superseded"):
                problems.append(
                    "%s: superseded ref presented without marker — %s#%d "
                    "needs '(superseded)'" % (loc, tok.repo, tok.number))
        elif status == "historical" and tok.kind == "pull_request":
            sha = entry.get("merge_sha")
            if sha:
                if ann and ann[0] == "merge":
                    if not sha.startswith(ann[1].strip()):
                        problems.append(
                            "%s: merge sha mismatch — doc says %s, manifest "
                            "records %s" % (loc, ann[1].strip(), sha[:8]))
                elif sha[:7] not in line:
                    problems.append(
                        "%s: merge claim without recorded sha — %s#%d needs "
                        "' (merge %s)'" % (loc, tok.repo, tok.number, sha[:8]))
            # historical refs must never read as current work
            if tok.repo == manifest["doc"]["migration"]["current_repo"]:
                problems.append(
                    "%s: historical ref written under current repo — %s#%d"
                    % (loc, tok.repo, tok.number))
        elif status == "current" and tok.kind == "pull_request":
            sha = entry.get("merge_sha")
            if sha and ann and ann[0] == "merge" and not sha.startswith(
                    ann[1].strip()):
                problems.append(
                    "%s: merge sha mismatch — doc says %s, manifest records "
                    "%s" % (loc, ann[1].strip(), sha[:8]))
        # unavailable objects must not be presented as current facts is
        # covered by the marker checks above.
    return problems


def validate_all(root_dir):
    """Validate every canonical doc under ``root_dir``.

    Returns a flat list of problem strings (empty = clean).
    """
    root = Path(root_dir)
    manifest = load_manifest(root / MANIFEST_NAME)
    problems = []
    for rel in CANONICAL_DOCS:
        p = root / rel
        if not p.exists():
            problems.append("%s: canonical doc missing" % rel)
            continue
        text = p.read_text(encoding="utf-8")
        problems.extend(validate_text(text, rel, manifest))
        # dead links to offline original repos are invalid reference syntax
        for m in _URL_RE.finditer(text):
            ln = text[:m.start()].count("\n") + 1
            problems.append(
                "%s:%d: raw URL to offline repository %s — cite the ref and "
                "mark 'repo unavailable' instead" % (rel, ln, m.group(1)))
    return problems


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    root = Path(argv[0]) if argv else Path(__file__).resolve().parents[1]
    problems = validate_all(root)
    if problems:
        print("canonical reference validation FAILED (%d):" % len(problems))
        for p in problems:
            print("  " + p)
        return 1
    print("canonical reference validation PASS (%d docs)" % len(CANONICAL_DOCS))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
