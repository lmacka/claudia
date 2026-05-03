"""Tests for app/db_kv.py — the singleton key-value store (T-NEW-I phase 4)."""

from __future__ import annotations

from pathlib import Path

from app.db_kv import kv_delete, kv_exists, kv_get, kv_set


def test_get_returns_none_when_unset(tmp_path: Path) -> None:
    assert kv_get(tmp_path, "nope") is None
    assert kv_exists(tmp_path, "nope") is False


def test_set_then_get_roundtrips(tmp_path: Path) -> None:
    kv_set(tmp_path, "k", "v1")
    assert kv_get(tmp_path, "k") == "v1"
    assert kv_exists(tmp_path, "k")


def test_set_replaces_existing(tmp_path: Path) -> None:
    kv_set(tmp_path, "k", "first")
    kv_set(tmp_path, "k", "second")
    assert kv_get(tmp_path, "k") == "second"


def test_delete_is_idempotent(tmp_path: Path) -> None:
    kv_delete(tmp_path, "ghost")  # no-op, no error
    kv_set(tmp_path, "k", "v")
    kv_delete(tmp_path, "k")
    assert kv_get(tmp_path, "k") is None


def test_empty_string_value_is_stored(tmp_path: Path) -> None:
    kv_set(tmp_path, "k", "")
    assert kv_get(tmp_path, "k") == ""
    assert kv_exists(tmp_path, "k") is True
