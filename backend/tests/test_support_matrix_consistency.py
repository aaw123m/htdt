"""Consistency tests between capture_contract/support-matrix.json and the
published schema documents it indexes (#332 follow-up).

The matrix is the runtime source of truth for path -> family resolution
and version -> schema-document resolution. These tests pin the
invariants the validator and emitters rely on, so a schema added,
renamed or re-versioned without a matching matrix edit fails here
instead of degrading into 'unsupported_legacy' diagnostics at ingest.
"""

from __future__ import annotations

import json
from pathlib import Path

from htdt.capture_bundle import (
    SCHEMA_DIR,
    _load_schema,
    _load_support_matrix,
    _version_gt,
)


def _families() -> dict:
    return _load_support_matrix()["families"]


def test_matrix_token_and_family_shape() -> None:
    matrix = _load_support_matrix()
    assert matrix["schema"] == "htdt.capture.bundle-support-matrix"
    assert matrix["families"]
    for name, contract in matrix["families"].items():
        assert contract.get("paths"), f"{name}: declares no bundle paths"
        assert isinstance(contract.get("read", []), list)


def test_document_keys_resolve_to_loadable_schemas() -> None:
    for name, contract in _families().items():
        for version, key in contract.get("documents", {}).items():
            schema_name = f"{key}.schema.json"
            assert (SCHEMA_DIR / schema_name).is_file(), (
                f"{name} v{version} -> {schema_name} missing"
            )
            # _load_schema parses the file and runs check_schema, so a
            # document the reference evaluator cannot express fails here.
            _load_schema(schema_name)


def test_versioned_schemas_pin_family_and_version() -> None:
    for name, contract in _families().items():
        if contract.get("external") or contract.get("unversioned"):
            continue
        for version, key in contract.get("documents", {}).items():
            schema = json.loads(
                (SCHEMA_DIR / f"{key}.schema.json").read_text(
                    encoding="utf-8"
                )
            )
            props = schema.get("properties", {})
            assert props.get("schema", {}).get("const") == (
                contract["schema_id"]
            ), f"{name} v{version}: schema const drifted"
            assert props.get("schema_version", {}).get("const") == version, (
                f"{name} v{version}: schema_version const drifted"
            )


def test_emitted_and_read_versions_are_declared() -> None:
    for name, contract in _families().items():
        documents = contract.get("documents", {})
        if contract.get("external"):
            assert contract.get("schema_id") is None
            assert contract.get("emitted") is None
            assert contract.get("read") == []
            assert documents == {}
            continue
        if contract.get("unversioned"):
            assert list(documents) == ["unversioned"]
            assert contract["emitted"] == "unversioned"
            assert contract["emitted"] in contract["read"]
            continue
        emitted = contract["emitted"]
        read = contract["read"]
        assert emitted in documents, f"{name}: emitted version undeclared"
        assert set(read) <= set(documents), (
            f"{name}: read set references undeclared versions"
        )
        assert emitted in read, f"{name}: emitted version not readable"
        # The emitted version is the newest the family serves.
        for other in documents:
            assert not _version_gt(other, emitted), (
                f"{name}: {other} declared newer than emitted {emitted}"
            )


def test_paths_are_unique_across_families() -> None:
    seen: dict[str, str] = {}
    for name, contract in _families().items():
        for path in contract.get("paths", []):
            assert path not in seen, (
                f"{path} claimed by both {seen[path]} and {name}"
            )
            seen[path] = name


def test_no_orphan_or_shared_schema_documents() -> None:
    referenced: dict[str, str] = {}
    for name, contract in _families().items():
        for version, key in contract.get("documents", {}).items():
            assert key not in referenced, (
                f"{key}.schema.json serves {referenced[key]} and "
                f"{name} v{version}"
            )
            referenced[key] = f"{name} v{version}"
    on_disk = {
        p.name[: -len(".schema.json")]
        for p in SCHEMA_DIR.glob("*.schema.json")
    }
    orphans = on_disk - set(referenced)
    assert not orphans, (
        f"schema files with no matrix entry: {sorted(orphans)}"
    )
