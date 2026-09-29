"""Round 14 (PKG): dev/prod packaging-surface parity.

There is no .spec file — ``scripts/build-native.ps1`` holds the PyInstaller
declarations inline. These tests enumerate the import/resource graph
mechanically (AST, not memory) and cross-check it against those
declarations, so drift between the shipped wheel and the frozen bundle
fails locally instead of shipping a packaged-only breakage:

- every ``_LAZY_*`` module-``__getattr__`` export target resolves to a real
  module file (``--collect-submodules htdt`` only covers modules that
  exist — a stale name crashes at first access in the packaged app);
- non-package lazy targets are covered by a ``--collect-*`` /
  ``--hidden-import`` declaration or a PyInstaller built-in hook;
- every ``setuptools.package-data`` glob is mirrored by a
  ``--collect-data``/``--add-data`` declaration (``--collect-submodules``
  does not copy data — ``htdt/capture_contract/*.json`` silently vanished
  from the bundle until this was declared);
- every ``importlib.metadata.version()`` queried distribution that is
  actually shipped is mirrored by ``--copy-metadata``;
- installer/build-script path conventions stay consistent.
"""

from __future__ import annotations

import ast
import fnmatch
from pathlib import Path
import re
import sys
import tomllib

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / 'src' / 'htdt'
BACKEND_ROOT = PACKAGE_ROOT.parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[2]
BUILD_NATIVE = REPO_ROOT / 'scripts' / 'build-native.ps1'
PYPROJECT = BACKEND_ROOT / 'pyproject.toml'
ISS = REPO_ROOT / 'installer' / 'HTDT.iss'
ENTRY_POINT = REPO_ROOT / 'scripts' / 'native_entry.py'
LOCK_FILE = BACKEND_ROOT / 'requirements-n05-windows.lock'

_STDLIB = set(sys.stdlib_module_names)

# Top-level packages a lazy/non-literal import may target without an
# explicit --collect-* declaration because PyInstaller's bundled hooks
# already collect them whole (hook-PySide6 ships all Qt bindings/plugins).
_HOOKED_PACKAGES = frozenset({'PySide6'})

_ADD_DATA_RE = re.compile(r'--add-data\s+"([^"]+)"')
_FLAG_RE = re.compile(
    r'--(collect-submodules|collect-data|collect-all|collect-dynamic-libs|'
    r'hidden-import|copy-metadata)\s+["\']?([\w.$\-/\\]+)'
)


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f'{base}.{node.attr}' if base else None
    return None


def _iter_sources():
    return sorted(PACKAGE_ROOT.rglob('*.py'))


def _lazy_dict_targets(tree: ast.AST):
    """Yield (dict_name, key, module_name, attr) for each _LAZY_* dict."""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if not names or not names[0].startswith('_LAZY'):
            continue
        for key_node, value in zip(node.value.keys, node.value.values):
            key = _const_str(key_node)
            module_name = attr = None
            if isinstance(value, (ast.Tuple, ast.List)) and value.elts:
                module_name = _const_str(value.elts[0])
                if len(value.elts) > 1:
                    attr = _const_str(value.elts[1])
            elif isinstance(value, ast.Constant):
                module_name = value.value
            yield names[0], key, module_name, attr


def _module_level_names(tree: ast.AST) -> set[str]:
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            names.update(
                alias.asname or alias.name for alias in node.names
            )
    return names


def _relative_module_file(source: Path, module: str) -> Path:
    parts = module.lstrip('.').split('.')
    return source.parent.joinpath(*parts)


def _module_file_exists(source: Path, module: str) -> bool:
    target = _relative_module_file(source, module)
    return target.with_suffix('.py').is_file() or (
        target / '__init__.py'
    ).is_file()


def _build_script_text() -> str:
    if not BUILD_NATIVE.is_file():
        pytest.skip('scripts/build-native.ps1 not available')
    return BUILD_NATIVE.read_text(encoding='utf-8')


def _declared_flags(text: str) -> dict[str, set[str]]:
    flags: dict[str, set[str]] = {}
    for kind, value in _FLAG_RE.findall(text):
        flags.setdefault(kind, set()).add(value)
    return flags


def test_lazy_table_relative_targets_resolve_to_files() -> None:
    missing: list[str] = []
    for source in _iter_sources():
        tree = ast.parse(source.read_text(encoding='utf-8'))
        for table, key, module, _attr in _lazy_dict_targets(tree):
            if module is None:
                continue
            if module.startswith('.'):
                if not _module_file_exists(source, module):
                    missing.append(f'{source.name}:{table}:{key} -> {module}')
    assert not missing, (
        'lazy-export targets that do not resolve to a package file '
        f'(packaged crash at first access): {missing}'
    )


def test_lazy_table_relative_targets_define_attribute() -> None:
    missing: list[str] = []
    for source in _iter_sources():
        tree = ast.parse(source.read_text(encoding='utf-8'))
        for table, key, module, attr in _lazy_dict_targets(tree):
            if module is None or attr is None or not module.startswith('.'):
                continue
            target = _relative_module_file(source, module)
            if not target.with_suffix('.py').is_file():
                continue  # unresolved modules are covered by the file test
            target_tree = ast.parse(
                target.with_suffix('.py').read_text(encoding='utf-8')
            )
            target_names = _module_level_names(target_tree)
            has_getattr = 'def __getattr__' in ast.dump(target_tree) or any(
                isinstance(n, ast.FunctionDef) and n.name == '__getattr__'
                for n in target_tree.body
            )
            if attr not in target_names and not has_getattr:
                missing.append(
                    f'{source.name}:{table}:{key} -> {module}.{attr}'
                )
    assert not missing, (
        'lazy-export attributes missing from the target module '
        f'(packaged AttributeError at first access): {missing}'
    )


def test_lazy_table_external_targets_are_packaged() -> None:
    """Non-relative lazy targets must be covered by a collect/hook."""
    text = _build_script_text()
    flags = _declared_flags(text)
    covered = _HOOKED_PACKAGES | {
        value.split('.')[0]
        for kind, values in flags.items()
        for value in values
        if kind != 'copy-metadata'
    }
    uncovered: list[str] = []
    for source in _iter_sources():
        tree = ast.parse(source.read_text(encoding='utf-8'))
        for table, key, module, _attr in _lazy_dict_targets(tree):
            if module is None or module.startswith('.'):
                continue
            top = module.split('.')[0]
            if top in _STDLIB or top in covered:
                continue
            uncovered.append(f'{source.name}:{table}:{key} -> {module}')
    assert not uncovered, (
        'lazy-export targets on modules PyInstaller will not bundle '
        f'(add --collect-* / --hidden-import or extend the hook list): '
        f'{uncovered}'
    )


def test_literal_dynamic_imports_resolve() -> None:
    """import_module('literal')/__import__('literal') must resolve."""
    text = _build_script_text()
    flags = _declared_flags(text)
    covered = _HOOKED_PACKAGES | {
        value.split('.')[0]
        for kind, values in flags.items()
        for value in values
        if kind != 'copy-metadata'
    }
    bad: list[str] = []
    for source in _iter_sources():
        tree = ast.parse(source.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fname = _dotted(node.func)
            if fname is None or not fname.endswith(
                ('import_module', '__import__')
            ):
                continue
            if not node.args:
                continue
            arg = _const_str(node.args[0])
            if arg is None:
                continue  # variable-arg calls are covered by lazy-table tests
            if arg.startswith('.'):
                if not _module_file_exists(source, arg):
                    bad.append(f'{source.name}:{node.lineno} -> {arg}')
            elif arg.split('.')[0] not in _STDLIB | covered | {'htdt'}:
                bad.append(f'{source.name}:{node.lineno} -> {arg}')
    assert not bad, f'dynamic import targets not packaged: {bad}'


def test_declared_package_data_reaches_the_bundle() -> None:
    """setuptools package-data globs must be mirrored into PyInstaller.

    ``--collect-submodules htdt`` collects modules only — data files
    (capture_contract JSON schemas) need ``--collect-data``/``--add-data``.
    """
    if not PYPROJECT.is_file():
        pytest.skip('pyproject.toml not available')
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding='utf-8'))
    package_data = (
        pyproject.get('tool', {}).get('setuptools', {}).get('package-data', {})
    )
    if not package_data:
        pytest.skip('no package-data declared')
    text = _build_script_text()
    flags = _declared_flags(text)
    collected_data = flags.get('collect-data', set()) | flags.get(
        'collect-all', set()
    )
    add_data_sources = {src for src, _dst in (d.split(';', 1) for d in _ADD_DATA_RE.findall(text))}
    missing: list[str] = []
    for package, globs in package_data.items():
        if package in collected_data:
            continue
        for pattern in globs:
            parent = pattern.split('/')[0]
            if not any(
                parent in src or f'{package}/{parent}' in src
                for src in add_data_sources
            ):
                missing.append(f'{package}: {pattern}')
    assert not missing, (
        'package-data globs with no PyInstaller coverage — files silently '
        f'missing from the packaged bundle: {missing}'
    )


def test_metadata_version_queries_have_copy_metadata() -> None:
    """``importlib.metadata.version(name)`` needs dist-info in the bundle.

    ``--copy-metadata <dist>`` ships the dist-info; without it every query
    degrades to 'not-installed' provenance. Distributions absent from the
    lock are unavailable in dev and package alike — only shipped ones are
    asserted.
    """
    if not LOCK_FILE.is_file():
        pytest.skip('dependency lock not available')
    locked = {
        m.group(1).lower().replace('-', '_')
        for line in LOCK_FILE.read_text(encoding='utf-8').splitlines()
        if (m := re.match(r'^([A-Za-z0-9_.-]+)==', line.strip()))
    }

    queried: set[str] = set()
    unresolved_sites: list[str] = []
    for source in _iter_sources():
        tree = ast.parse(source.read_text(encoding='utf-8'))
        # loop-variable binding: for <name> in ('a', 'b'): ... version(<name>)
        loop_bindings: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.For, ast.AsyncFor)) and isinstance(
                node.target, ast.Name
            ) and isinstance(node.iter, (ast.Tuple, ast.List)):
                literals = {
                    v for e in node.iter.elts if (v := _const_str(e))
                }
                if literals:
                    loop_bindings.setdefault(node.target.id, set()).update(
                        literals
                    )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fname = _dotted(node.func)
            if fname is None or not fname.endswith(
                ('metadata.version', 'distribution_version')
            ):
                continue
            if not node.args:
                continue
            arg_node = node.args[0]
            literal = _const_str(arg_node)
            if literal is not None:
                queried.add(literal)
            elif isinstance(arg_node, ast.Name) and arg_node.id in loop_bindings:
                queried.update(loop_bindings[arg_node.id])
            else:
                unresolved_sites.append(f'{source.name}:{node.lineno}')
    assert not unresolved_sites, (
        'importlib.metadata.version() call sites this test cannot enumerate '
        f'— extend the resolver: {unresolved_sites}'
    )

    text = _build_script_text()
    copied = _declared_flags(text).get('copy-metadata', set())
    missing = {
        name for name in queried
        if name.lower().replace('-', '_') in locked and name not in copied
    }
    assert not missing, (
        'shipped distributions whose metadata is queried at runtime but not '
        f'bundled via --copy-metadata: {missing}'
    )


def test_capture_contract_data_files_are_declared() -> None:
    """Every file under capture_contract/ must match a package-data glob."""
    contract_dir = PACKAGE_ROOT / 'capture_contract'
    assert contract_dir.is_dir(), 'capture_contract package data missing'
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding='utf-8'))
    globs = (
        pyproject['tool']['setuptools']['package-data'].get('htdt', [])
    )
    undeclared = [
        path.name
        for path in contract_dir.iterdir()
        if path.is_file()
        and not any(
            fnmatch.fnmatch(f'capture_contract/{path.name}', g)
            for g in globs
        )
    ]
    assert not undeclared, (
        'capture_contract files not covered by package-data globs: '
        f'{undeclared}'
    )
    assert (contract_dir / 'support-matrix.json').is_file()


def test_build_script_collects_package_modules_and_data() -> None:
    """The htdt package must ship whole: submodules AND data."""
    text = _build_script_text()
    assert '--collect-submodules htdt' in text
    assert '--collect-data htdt' in text, (
        'htdt package data (capture_contract schemas) is not bundled — '
        '.htdtcapture validation fails in every packaged build'
    )


def test_entry_point_and_installer_paths_consistent() -> None:
    text = _build_script_text()
    assert ENTRY_POINT.is_file(), 'PyInstaller entry script missing'
    # The entry must stay a thin launcher onto a real module:main.
    entry_src = ENTRY_POINT.read_text(encoding='utf-8')
    assert 'htdt.native_cad' in entry_src
    import htdt.native_cad  # noqa: F401
    assert callable(htdt.native_cad.main)
    # Branding assets the build generates are referenced by add-data.
    branding = REPO_ROOT / 'assets' / 'branding' / 'HTDT-AppIcon-source.jpg'
    assert branding.is_file(), 'icon source the build requires is missing'
    # Inno source dir must match the build's default output layout.
    iss_text = ISS.read_text(encoding='utf-8')
    assert 'dist-native' in iss_text
    assert r'{app}\HTDT\HTDT.exe' in iss_text
    # File-association launches must quote "%1" — install paths contain
    # spaces. Inno escapes quotes by doubling them.
    command_lines = [
        line for line in iss_text.splitlines()
        if 'shell\\open\\command' in line
    ]
    assert command_lines, 'no file-association open commands found'
    for line in command_lines:
        assert '""%1""' in line, f'unquoted %1 in association: {line}'


def test_capture_receiver_missing_openssl_is_named_error(monkeypatch) -> None:
    """Packaged machines without openssl must get the real prerequisite."""
    import htdt.capture_receiver as receiver

    def _raise(*args, **kwargs):
        raise FileNotFoundError(2, 'No such file or directory', 'openssl')

    monkeypatch.setattr(receiver.subprocess, 'run', _raise)
    with pytest.raises(receiver.CaptureReceiverError, match='openssl'):
        receiver.generate_self_signed_cert(
            Path('cert.pem'), Path('key.pem')
        )
