"""Tests for the Godot extractors (graphify/extractors/gdscript.py)."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_gdscript")

from graphify.detect import CODE_EXTENSIONS, _SKIP_DIRS
from graphify.extract import _DISPATCH, _EXTRA_FOR_EXTENSION, extract_gdscript
from graphify.extractors import LANGUAGE_EXTRACTORS


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _labels(r) -> set[str]:
    return {n["label"] for n in r["nodes"]}


def _rel_pairs(r, relation: str) -> set[tuple[str, str]]:
    lab = {n["id"]: n["label"] for n in r["nodes"]}
    return {
        (lab.get(e["source"], e["source"]), lab.get(e["target"], e["target"]))
        for e in r["edges"]
        if e["relation"] == relation
    }


def test_gd_is_registered_everywhere():
    assert ".gd" in CODE_EXTENSIONS
    assert ".godot" in _SKIP_DIRS
    assert _DISPATCH[".gd"] is extract_gdscript
    assert LANGUAGE_EXTRACTORS["gdscript"] is extract_gdscript
    assert _EXTRA_FOR_EXTENSION[".gd"] == "gdscript"


def test_empty_script_yields_only_the_file_node(tmp_path):
    r = extract_gdscript(_write(tmp_path, "empty.gd", ""))
    assert "error" not in r
    assert _labels(r) == {"empty.gd"}
    assert r["edges"] == []
