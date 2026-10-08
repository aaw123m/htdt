"""Issue #900 — workflow/documentation drift integrity.

Current-state documentation must never contradict the repository about
which ``.github/workflows/*.yml`` exist or how they are triggered:

- a workflow file named as *current* in a canonical doc must exist on
  disk (deleted/renamed workflows still asserted as current fail);
- every workflow file on disk must be named in at least one canonical
  doc (an undocumented workflow is drift too);
- the documented trigger vocabulary stays honest: workflow files carry
  ``workflow_dispatch`` (manual) and may not reintroduce merge-blocking
  PR CI (``pull_request:``/``push:`` triggers) — the intentional
  no-PR-CI policy (#833) is guarded here, not re-litigated;
- no canonical doc may assert the *current absence* of the workflows
  directory as a fact (historical removal events stay date/sha-bound).

Workflow presence is never verification evidence: a PASS stays bound to
revision + runner + evidence, not to a YAML file existing.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOWS_DIR = REPO_ROOT / '.github' / 'workflows'

#: Canonical docs allowed to assert current-state facts about workflows.
CURRENT_STATE_DOCS = (
    'docs/IMPLEMENTATION_STATUS.md',
    'docs/RELEASE_VERIFICATION.md',
    'docs/ISSUE_VERIFICATION.md',
    'README.md',
)

_WORKFLOW_REF = re.compile(r'\.github/workflows/([A-Za-z0-9_.\-]+\.ya?ml)')
#: A workflow path is "current-state asserted" when the doc line naming it
#: carries no historical qualifier — file-level history (e.g. the deleted
#: ci.yml recorded under b47f052) stays legal.
_HISTORICAL_MARKERS = ('b47f052', '(unavailable)', 'superseded', '削除', '当時')


def _doc_text(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding='utf-8')


def _workflow_files() -> set[str]:
    return {
        path.name
        for path in WORKFLOWS_DIR.glob('*.y*ml')
        if path.is_file()
    }


def _current_named() -> set[str]:
    """Workflow files named on doc lines without a historical qualifier —
    the names the docs assert as *currently* present."""
    named: set[str] = set()
    for doc in CURRENT_STATE_DOCS:
        for line in _doc_text(doc).splitlines():
            for name in _WORKFLOW_REF.findall(line):
                if not any(m in line for m in _HISTORICAL_MARKERS):
                    named.add(name)
    return named


def _named_in_docs() -> set[str]:
    named: set[str] = set()
    for doc in CURRENT_STATE_DOCS:
        named.update(_WORKFLOW_REF.findall(_doc_text(doc)))
    return named


def test_doc_named_workflows_exist() -> None:
    files = _workflow_files()
    for name in sorted(_current_named()):
        assert name in files, (
            f'canonical docs assert .github/workflows/{name} as current '
            f'but the file does not exist (deleted/renamed workflow '
            f'still claimed — #900 drift)'
        )


def test_existing_workflows_are_documented() -> None:
    named = _named_in_docs()
    for name in sorted(_workflow_files()):
        assert name in named, (
            f'.github/workflows/{name} exists but no canonical doc '
            f'({", ".join(CURRENT_STATE_DOCS)}) names it — undocumented '
            f'workflow is drift (#900)'
        )


def test_workflows_stay_dispatch_only() -> None:
    """The intentional no-PR-CI policy: workflow triggers may be
    workflow_dispatch / schedule / release — never push or pull_request."""
    for name in sorted(_workflow_files()):
        text = (WORKFLOWS_DIR / name).read_text(encoding='utf-8')
        assert 'workflow_dispatch' in text, (
            f'{name}: expected a workflow_dispatch trigger'
        )
        for bad in ('pull_request:', 'pull_request_target:', 'push:'):
            # Allow the literal inside comments/strings? No — a trigger
            # key at line start is the only form that activates CI.
            assert not re.search(rf'^\s*{re.escape(bad)}\s*$', text, re.M), (
                f'{name}: {bad} trigger reintroduces merge-blocking CI '
                f'against the documented no-PR-CI policy (#833)'
            )


def test_no_current_absence_claim() -> None:
    """The implementation-status header is the current-fact note: it may
    record the historical removal (b47f052) as a dated event, but must
    never claim the workflows directory is absent *now* — files exist on
    current main. Body-level claims that name a specific deleted file
    (e.g. ci.yml) stay legal: they describe that file's history."""
    status = _doc_text('docs/IMPLEMENTATION_STATUS.md')
    header_lines = status.splitlines()[:12]
    files_exist = bool(_workflow_files())
    for lineno, line in enumerate(header_lines, start=1):
        if '.github/workflows' not in line:
            continue
        if '削除' not in line and 'absent' not in line.lower():
            continue
        # A header line that also affirms current existence (存在する) is
        # the corrected form — historical removal + current presence.
        if '存在' in line:
            continue
        assert not files_exist, (
            f'IMPLEMENTATION_STATUS.md:{lineno} (current-fact header) '
            f'claims .github/workflows removal while workflow files '
            f'exist on main — update the note to distinguish the '
            f'historical removal from current dispatch-only workflows '
            f'(#900)'
        )
