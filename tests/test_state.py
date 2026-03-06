"""Tests for import state tracking."""

import json
import os

from clawhub_importer.state import ImportState, load_state, save_state


def test_empty_state():
    state = ImportState()
    assert not state.is_imported("foo", "1.0.0")
    assert state.is_newer("foo", "1.0.0")
    assert state.summary() == {"total_imported": 0}


def test_mark_imported():
    state = ImportState()
    state.mark_imported("foo", "1.0.0")

    assert state.is_imported("foo", "1.0.0")
    assert not state.is_imported("foo", "2.0.0")
    assert not state.is_imported("bar", "1.0.0")


def test_is_newer():
    state = ImportState()
    state.mark_imported("foo", "1.0.0")

    assert not state.is_newer("foo", "1.0.0")
    assert state.is_newer("foo", "2.0.0")
    assert state.is_newer("bar", "1.0.0")


def test_save_and_load(tmp_path):
    path = str(tmp_path / "state.json")

    state = ImportState()
    state.mark_imported("skill-a", "1.0.0")
    state.mark_imported("skill-b", "2.1.0")
    save_state(state, path)

    loaded = load_state(path)
    assert loaded.is_imported("skill-a", "1.0.0")
    assert loaded.is_imported("skill-b", "2.1.0")
    assert not loaded.is_imported("skill-a", "9.9.9")


def test_load_missing_file(tmp_path):
    path = str(tmp_path / "does_not_exist.json")
    state = load_state(path)
    assert state.summary() == {"total_imported": 0}


def test_load_corrupt_file(tmp_path):
    path = str(tmp_path / "bad.json")
    with open(path, "w") as f:
        f.write("not json")
    state = load_state(path)
    assert state.summary() == {"total_imported": 0}


def test_overwrite_version():
    state = ImportState()
    state.mark_imported("foo", "1.0.0")
    state.mark_imported("foo", "2.0.0")

    assert not state.is_imported("foo", "1.0.0")
    assert state.is_imported("foo", "2.0.0")


def test_mark_skipped():
    state = ImportState()
    assert not state.is_skipped("foo")

    state.mark_skipped("foo")
    assert state.is_skipped("foo")
    assert not state.is_skipped("bar")


def test_save_and_load_skipped(tmp_path):
    path = str(tmp_path / "state.json")

    state = ImportState()
    state.mark_imported("skill-a", "1.0.0")
    state.mark_skipped("claimed-skill")
    save_state(state, path)

    loaded = load_state(path)
    assert loaded.is_imported("skill-a", "1.0.0")
    assert loaded.is_skipped("claimed-skill")
    assert not loaded.is_skipped("skill-a")
