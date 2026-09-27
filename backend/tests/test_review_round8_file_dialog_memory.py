"""Round-8 regression: last-used-directory memory for native file dialogs.

The picker itself is monkeypatched — these tests pin the helper contract
(initial dir selection, write-back on success, persistence, corruption
tolerance) without showing a modal dialog.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from htdt import file_dialog_memory
from htdt.file_dialog_memory import (
    GLOBAL_KEY,
    FileDialogMemoryStore,
)


@pytest.fixture
def store(tmp_path):
    return FileDialogMemoryStore.for_data_dir(tmp_path)


@pytest.fixture
def configured(store, monkeypatch):
    monkeypatch.setattr(file_dialog_memory, "_active_store", store)
    monkeypatch.setattr(file_dialog_memory, "_fallback_store", None)
    return store


@pytest.fixture
def qapp():
    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
    return instance


def test_recall_prefers_dialog_key_over_global(store, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    store.remember("a", str(first))
    store.remember("b", str(second))
    assert store.recall("a") == str(first)
    assert store.recall("b") == str(second)


def test_recall_falls_back_to_last_used_for_unknown_key(store, tmp_path):
    directory = tmp_path / "work"
    directory.mkdir()
    store.remember("known", str(directory))
    assert store.recall("never.seen") == str(directory)


def test_recall_returns_empty_when_nothing_remembered(store):
    assert store.recall("anything") == ""


def test_recall_skips_missing_directories(store, tmp_path):
    gone = tmp_path / "gone"
    store.remember("a", str(gone))
    # Directory never created — remembered path must not steer the dialog.
    assert store.recall("a") == ""


def test_persist_roundtrip(tmp_path):
    directory = tmp_path / "picked"
    directory.mkdir()
    store = FileDialogMemoryStore.for_data_dir(tmp_path)
    store.remember("k", str(directory))
    reloaded = FileDialogMemoryStore.for_data_dir(tmp_path)
    assert reloaded.recall("k") == str(directory)


def test_corrupt_store_file_degrades_to_empty(tmp_path):
    path = tmp_path / file_dialog_memory.FILE_DIALOG_DIRS_FILENAME
    path.write_text("{not json", encoding="utf-8")
    store = FileDialogMemoryStore.for_data_dir(tmp_path)
    assert store.recall("k") == ""


def test_foreign_schema_degrades_to_empty(tmp_path):
    path = tmp_path / file_dialog_memory.FILE_DIALOG_DIRS_FILENAME
    path.write_text(
        json.dumps({"schema_version": 99, "dirs": {"k": "x"}}),
        encoding="utf-8",
    )
    store = FileDialogMemoryStore.for_data_dir(tmp_path)
    assert store.recall("k") == ""


def test_get_open_file_name_uses_remembered_dir_and_writes_back(
    configured, monkeypatch, qapp, tmp_path
):
    start = tmp_path / "start"
    start.mkdir()
    picked = tmp_path / "picked" / "file.txt"
    picked.parent.mkdir()
    seen = {}

    def fake(parent, caption, directory, filter_):
        seen["dir"] = directory
        return str(picked), filter_

    monkeypatch.setattr(
        file_dialog_memory.QFileDialog, "getOpenFileName", staticmethod(fake)
    )
    file_dialog_memory.active_store().remember("other", str(start))
    selected, _ = file_dialog_memory.get_open_file_name(None, "t", "k")
    assert seen["dir"] == str(start)  # global fallback
    assert selected == str(picked)
    # Write-back: the picked file's parent becomes the remembered dir.
    assert configured.recall("k") == str(picked.parent)


def test_get_save_file_name_joins_suggested_name(
    configured, monkeypatch, qapp, tmp_path
):
    directory = tmp_path / "exports"
    directory.mkdir()
    configured.remember("save.k", str(directory))
    seen = {}

    def fake(parent, caption, start_dir, filter_):
        seen["dir"] = start_dir
        return "", filter_

    monkeypatch.setattr(
        file_dialog_memory.QFileDialog, "getSaveFileName", staticmethod(fake)
    )
    file_dialog_memory.get_save_file_name(
        None, "t", "save.k", suggested_name="out.json"
    )
    assert seen["dir"] == str(directory / "out.json")


def test_save_falls_back_to_default_dir_then_bare_name(
    configured, monkeypatch, qapp, tmp_path
):
    seen = []

    def fake(parent, caption, start_dir, filter_):
        seen.append(start_dir)
        return "", filter_

    monkeypatch.setattr(
        file_dialog_memory.QFileDialog, "getSaveFileName", staticmethod(fake)
    )
    file_dialog_memory.get_save_file_name(
        None, "t", "k1", suggested_name="n.txt", default_dir=str(tmp_path)
    )
    file_dialog_memory.get_save_file_name(None, "t", "k2", suggested_name="n.txt")
    assert seen == [str(tmp_path / "n.txt"), "n.txt"]


def test_cancelled_dialog_writes_nothing(configured, monkeypatch, qapp):
    monkeypatch.setattr(
        file_dialog_memory.QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *a: ("", "")),
    )
    file_dialog_memory.get_open_file_name(None, "t", "k")
    assert configured.recall("k") == ""


def test_get_existing_directory_remembers_picked_dir(
    configured, monkeypatch, qapp, tmp_path
):
    picked = tmp_path / "target"
    picked.mkdir()
    monkeypatch.setattr(
        file_dialog_memory.QFileDialog,
        "getExistingDirectory",
        staticmethod(lambda *a: str(picked)),
    )
    assert file_dialog_memory.get_existing_directory(None, "t", "k") == str(picked)
    assert configured.recall("k") == str(picked)


def test_get_open_file_names_records_first_selection(
    configured, monkeypatch, qapp, tmp_path
):
    picked = tmp_path / "multi" / "a.txt"
    picked.parent.mkdir()
    monkeypatch.setattr(
        file_dialog_memory.QFileDialog,
        "getOpenFileNames",
        staticmethod(lambda *a: ([str(picked)], "")),
    )
    file_dialog_memory.get_open_file_names(None, "t", "k")
    assert configured.recall("k") == str(picked.parent)


def test_global_key_constant_is_reserved(store):
    # GLOBAL_KEY must never collide with a real dialog key.
    assert "." not in GLOBAL_KEY
