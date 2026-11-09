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


def _project(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "project.godot").write_text("[application]\n", encoding="utf-8")
    return tmp_path


def test_preload_and_path_extends_link_to_the_file(tmp_path):
    root = _project(tmp_path)
    base = _write(root, "base.gd", "func f():\n\tpass\n")
    child = _write(root / "sub", "child.gd",
                   'extends "res://base.gd"\nconst B = preload("res://base.gd")\n'
                   'func g():\n\tvar s = load("res://base.gd")\n')
    r = extract_gdscript(child)
    base_id = extract_gdscript(base)["nodes"][0]["id"]
    assert {(e["relation"], e["target"]) for e in r["edges"]} >= {
        ("inherits", base_id), ("imports_from", base_id)}


def test_res_path_without_a_project_root_adds_nothing(tmp_path):
    r = extract_gdscript(_write(tmp_path, "loose.gd", 'const B = preload("res://base.gd")\n'))
    assert "error" not in r
    assert not [e for e in r["edges"] if e["relation"] == "imports_from"]


def test_res_path_escaping_the_root_adds_nothing(tmp_path):
    root = _project(tmp_path / "game")
    r = extract_gdscript(_write(root, "a.gd", 'const B = preload("res://../outside.gd")\n'))
    assert not [e for e in r["edges"] if e["relation"] == "imports_from"]


def test_named_receiver_calls_are_parked_not_guessed(tmp_path):
    r = extract_gdscript(_write(tmp_path, "a.gd", "func f():\n\tGameState.save()\n\tsuper.f()\n"))
    assert not _rel_pairs(r, "calls")
    (call,) = r["raw_calls"]
    assert (call["receiver"], call["callee"], call["language"]) == ("GameState", "save", "gdscript")
    assert call["is_member_call"] is True


SCENE = """\
[gd_scene load_steps=3 format=3 uid="uid://abc"]

[ext_resource type="Script" uid="uid://s1" path="res://player.gd" id="1_a"]
[ext_resource type="PackedScene" path="res://hud.tscn" id="2_b"]
[ext_resource type="Script" path="res://button.gd" id="3_c"]

[node name="Player" type="CharacterBody2D"]
script = ExtResource("1_a")

[node name="HUD" parent="." instance=ExtResource("2_b")]

[node name="Panel" type="Control" parent="."]

[node name="Button" type="Button" parent="Panel"]
script = ExtResource("3_c")

[connection signal="pressed" from="Panel/Button" to="." method="_on_pressed"]
[connection signal="hover" from="." to="Panel/Button" method="_on_hover"]
"""


def test_scene_links_scripts_scenes_and_connections(tmp_path):
    from graphify.extract import extract_godot_scene

    root = _project(tmp_path)
    player = _write(root, "player.gd", "func _on_pressed():\n\tpass\n")
    button = _write(root, "button.gd", "func _on_hover():\n\tpass\n")
    hud = _write(root, "hud.tscn", "[gd_scene format=3]\n")
    r = extract_godot_scene(_write(root, "main.tscn", SCENE))
    scene_id = r["nodes"][0]["id"]
    ids = {n["label"]: n["id"] for f in (player, button) for n in extract_gdscript(f)["nodes"]}
    got = {(e["relation"], e["target"]) for e in r["edges"] if e["source"] == scene_id}
    assert got == {
        ("uses", ids["player.gd"]), ("uses", ids["button.gd"]),
        ("instantiates", extract_godot_scene(hud)["nodes"][0]["id"]),
        ("calls", ids["_on_pressed()"]), ("calls", ids["_on_hover()"]),
    }


def test_godot3_scene_attribute_order_and_numeric_ids(tmp_path):
    from graphify.extract import extract_godot_scene

    root = _project(tmp_path)
    src = ('[gd_scene load_steps=2 format=2]\n'
           '[ext_resource path="res://p.gd" type="Script" id=1]\n'
           '[node name="P" type="Node"]\nscript = ExtResource( 1 )\n'
           '[connection signal="s" from="." to="." method="m"]\n')
    r = extract_godot_scene(_write(root, "old.tscn", src))
    assert len([e for e in r["edges"] if e["relation"] == "uses"]) == 1
    assert len([e for e in r["edges"] if e["relation"] == "calls"]) == 1


def test_project_file_lists_autoloads_and_main_scene(tmp_path):
    from graphify.extract import extract_godot_project

    src = ('[application]\nrun/main_scene="res://main.tscn"\n\n'
           '[autoload]\nGameState="*res://game_state.gd"\nAudio="res://audio.tscn"\n\n'
           '[input]\nGameStateX="*res://nope.gd"\n')
    r = extract_godot_project(_write(tmp_path, "project.godot", src))
    autoloads = {n["label"] for n in r["nodes"] if n.get("_godot_autoload")}
    assert autoloads == {"GameState", "Audio"}
    assert ("project.godot", "GameState") in _rel_pairs(r, "contains")
    assert len([e for e in r["edges"] if e["relation"] == "uses"]) == 3


def test_scene_and_project_suffixes_are_dispatched():
    from graphify.extract import extract_godot_project, extract_godot_scene

    assert _DISPATCH[".tscn"] is extract_godot_scene
    assert _DISPATCH[".godot"] is extract_godot_project
    assert {".tscn", ".godot"} <= CODE_EXTENSIONS
