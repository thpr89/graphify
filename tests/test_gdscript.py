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


SCRIPT = """\
class_name Player
extends CharacterBody2D

signal hit(amount)
enum State { IDLE, RUN }

func _ready():
\thelper()
\tself.move()
\thit.emit(1)
\temit_signal("hit")
\thit.connect(on_hit)

func helper():
\tpass

func move():
\tpass

func on_hit(amount):
\tpass

class Inventory:
\tfunc helper():
\t\tpass
\tfunc add():
\t\thelper()
"""


def test_definitions(tmp_path):
    r = extract_gdscript(_write(tmp_path, "player.gd", SCRIPT))
    assert {"player.gd", "Player", "hit", "State", "Inventory",
            "_ready()", "helper()", "move()", "on_hit()", "add()"} <= _labels(r)


def test_ownership_and_inheritance(tmp_path):
    r = extract_gdscript(_write(tmp_path, "player.gd", SCRIPT))
    assert ("player.gd", "Player") in _rel_pairs(r, "contains")
    assert ("Player", "_ready()") in _rel_pairs(r, "method")
    assert ("Player", "hit") in _rel_pairs(r, "contains")
    assert ("Player", "Inventory") in _rel_pairs(r, "contains")
    assert ("Inventory", "add()") in _rel_pairs(r, "method")
    assert ("Player", "CharacterBody2D") in _rel_pairs(r, "inherits")


def test_engine_base_class_is_a_sourceless_stub(tmp_path):
    r = extract_gdscript(_write(tmp_path, "player.gd", SCRIPT))
    stub = next(n for n in r["nodes"] if n["label"] == "CharacterBody2D")
    assert stub["source_file"] == ""
    assert stub["origin_file"].endswith("player.gd")


def test_same_file_calls_and_signals(tmp_path):
    r = extract_gdscript(_write(tmp_path, "player.gd", SCRIPT))
    calls = _rel_pairs(r, "calls")
    assert ("_ready()", "helper()") in calls
    assert ("_ready()", "move()") in calls
    assert ("hit", "on_hit()") in calls
    assert ("_ready()", "hit") in _rel_pairs(r, "uses")


def test_inner_method_shadows_top_level_function(tmp_path):
    r = extract_gdscript(_write(tmp_path, "player.gd", SCRIPT))
    by_id = {n["id"]: n["label"] for n in r["nodes"]}
    assert len([i for i, label in by_id.items() if label == "helper()"]) == 2
    inv = next(i for i, label in by_id.items() if label == "Inventory")
    inner = {by_id[e["target"]]: e["target"] for e in r["edges"]
             if e["relation"] == "method" and e["source"] == inv}
    calls = {(e["source"], e["target"]) for e in r["edges"] if e["relation"] == "calls"}
    assert (inner["add()"], inner["helper()"]) in calls


def test_script_without_class_name_is_owned_by_the_file(tmp_path):
    r = extract_gdscript(_write(tmp_path, "util.gd", "extends Node\nfunc f():\n\tpass\n"))
    assert ("util.gd", "f()") in _rel_pairs(r, "contains")
    assert ("util.gd", "Node") in _rel_pairs(r, "inherits")


def test_syntax_error_keeps_what_parsed(tmp_path):
    r = extract_gdscript(_write(tmp_path, "bad.gd", "func ok():\n\tpass\n\nfunc broken(:\n"))
    assert "ok()" in _labels(r)


def test_godot3_syntax_does_not_raise(tmp_path):
    src = "extends Node\nonready var x = 1\nexport var y = 2\nfunc f():\n\tyield(self, \"done\")\n"
    r = extract_gdscript(_write(tmp_path, "old.gd", src))
    assert "f()" in _labels(r)
