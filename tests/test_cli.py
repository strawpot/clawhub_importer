"""Tests for CLI helpers."""

from clawhub_importer.cli import _get_version


def test_get_version_from_latest_version():
    summary = {"latestVersion": {"version": "1.2.3"}}
    assert _get_version(summary) == "1.2.3"


def test_get_version_from_tags():
    summary = {"tags": {"latest": "3.0.0"}}
    assert _get_version(summary) == "3.0.0"


def test_get_version_fallback():
    assert _get_version({}) == "0.0.1"


def test_get_version_non_dict_latest():
    summary = {"latestVersion": "not-a-dict"}
    assert _get_version(summary) == "0.0.1"
