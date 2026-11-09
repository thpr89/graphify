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
    player = next(n for n in r["nodes"] if n["label"] == "Player")
    assert player["_gdscript_extends"] == "CharacterBody2D"


def test_base_class_from_elsewhere_mints_no_node(tmp_path):
    # A placeholder node for `Player` would share its id with a root-level
    # player.gd file node, and one for `Node` would join every script.
    r = extract_gdscript(_write(tmp_path, "player.gd", SCRIPT))
    assert "CharacterBody2D" not in _labels(r)
    assert not _rel_pairs(r, "inherits")


def test_extends_a_class_in_the_same_file(tmp_path):
    src = "class B extends A:\n\tpass\nclass A:\n\tpass\n"
    r = extract_gdscript(_write(tmp_path, "pair.gd", src))
    assert _rel_pairs(r, "inherits") == {("B", "A")}


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
    assert r["nodes"][0]["_gdscript_extends"] == "Node"


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
    links = [e for e in r["edges"] if e["relation"] == "imports_from"]
    # One edge per importing node: the file (preload/load) and, for `extends`,
    # the script itself, which is also the file here. Every file-to-file edge
    # names its target file so same-named siblings (x.gd / x.tscn) stay apart.
    assert {e["target"] for e in links} == {base_id}
    assert {e["target_file"] for e in links} == {str(base)}
    assert [e.get("context") for e in links] == ["extends"]


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
        ("imports_from", ids["player.gd"]), ("imports_from", ids["button.gd"]),
        ("imports_from", extract_godot_scene(hud)["nodes"][0]["id"]),
        ("calls", ids["_on_pressed()"]), ("calls", ids["_on_hover()"]),
    }
    assert {e["target_file"] for e in r["edges"] if e["relation"] == "imports_from"} == {
        str(player), str(button), str(hud)}


def test_godot3_scene_attribute_order_and_numeric_ids(tmp_path):
    from graphify.extract import extract_godot_scene

    root = _project(tmp_path)
    src = ('[gd_scene load_steps=2 format=2]\n'
           '[ext_resource path="res://p.gd" type="Script" id=1]\n'
           '[node name="P" type="Node"]\nscript = ExtResource( 1 )\n'
           '[connection signal="s" from="." to="." method="m"]\n')
    r = extract_godot_scene(_write(root, "old.tscn", src))
    assert len([e for e in r["edges"] if e["relation"] == "imports_from"]) == 1
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
    assert len([e for e in r["edges"] if e["relation"] == "imports_from"]) == 3


def test_scene_and_project_suffixes_are_dispatched():
    from graphify.extract import extract_godot_project, extract_godot_scene

    assert _DISPATCH[".tscn"] is extract_godot_scene
    assert _DISPATCH[".godot"] is extract_godot_project
    assert {".tscn", ".godot"} <= CODE_EXTENSIONS


def _corpus(tmp_path: Path, extra: dict[str, str] | None = None) -> dict:
    from graphify.extract import extract

    root = _project(tmp_path / "game")
    (root / "project.godot").write_text(
        '[autoload]\nGameState="*res://game_state.gd"\n', encoding="utf-8")
    files = {
        "game_state.gd": "extends Node\nfunc save():\n\tpass\n",
        "player.gd": ("class_name Player\nextends CharacterBody2D\n"
                      "static func make():\n\tpass\n"
                      "func _ready():\n\tGameState.save()\n"),
        "enemy.gd": ("extends Player\n"
                     "func spawn():\n\tPlayer.make()\n\tMystery.save()\n"),
        **(extra or {}),
    }
    paths = [root / "project.godot"] + [_write(root, n, s) for n, s in files.items()]
    return extract(paths, cache_root=tmp_path / "cache", root=root, parallel=False)


def test_autoload_and_class_name_calls_resolve(tmp_path):
    r = _corpus(tmp_path)
    calls = _rel_pairs(r, "calls")
    assert ("_ready()", "save()") in calls
    assert ("spawn()", "make()") in calls


def test_unknown_receiver_binds_nothing(tmp_path):
    r = _corpus(tmp_path)
    assert ("spawn()", "save()") not in _rel_pairs(r, "calls")


def test_extends_class_name_lands_on_the_real_class(tmp_path):
    r = _corpus(tmp_path)
    by_id = {n["id"]: n for n in r["nodes"]}
    targets = [by_id[e["target"]] for e in r["edges"]
               if e["relation"] == "inherits" and by_id[e["source"]]["label"] == "enemy.gd"]
    assert [t["source_file"].endswith("player.gd") for t in targets] == [True]


def test_engine_classes_do_not_join_scripts(tmp_path):
    r = _corpus(tmp_path)
    assert not {"Node", "CharacterBody2D"} & _labels(r)


def test_duplicate_class_name_binds_nothing(tmp_path):
    r = _corpus(tmp_path, {"other/player.gd": "class_name Player\nstatic func make():\n\tpass\n"})
    assert ("spawn()", "make()") not in _rel_pairs(r, "calls")


def test_root_level_script_scene_pair_keeps_its_edges(tmp_path):
    # player.gd + player.tscn side by side, with `class_name Player`: the file,
    # the scene and the class all want the id "player".
    scene = ('[gd_scene format=3]\n'
             '[ext_resource type="Script" path="res://player.gd" id="1"]\n'
             '[node name="P" type="CharacterBody2D"]\nscript = ExtResource("1")\n'
             '[connection signal="pressed" from="." to="." method="_ready"]\n')
    r = _corpus(tmp_path, {
        "player.tscn": scene,
        "spawner.gd": 'const P = preload("res://player.gd")\nconst S = preload("res://player.tscn")\n',
    })
    by_id = {n["id"]: n for n in r["nodes"]}
    assert len(by_id) == len(r["nodes"])

    def targets(source_label: str, relation: str) -> list[tuple[str, str]]:
        return sorted(
            (by_id[e["target"]]["label"], Path(by_id[e["target"]]["source_file"]).name)
            for e in r["edges"]
            if e["relation"] == relation and by_id[e["source"]]["label"] == source_label
            and e["target"] in by_id)

    assert targets("player.tscn", "imports_from") == [("player.gd", "player.gd")]
    assert targets("player.tscn", "calls") == [("_ready()", "player.gd")]
    assert targets("spawner.gd", "imports_from") == [
        ("player.gd", "player.gd"), ("player.tscn", "player.tscn")]
    assert targets("enemy.gd", "inherits") == [("Player", "player.gd")]


def test_extends_by_path_inherits_from_that_script(tmp_path):
    r = _corpus(tmp_path, {"boss.gd": 'extends "res://enemy.gd"\nfunc roar():\n\tpass\n'})
    assert ("boss.gd", "enemy.gd") in _rel_pairs(r, "inherits")


def _graph_triples(root: Path) -> list[tuple[str, str, str]]:
    import json

    graph = json.loads((root / "graphify-out" / "graph.json").read_text(encoding="utf-8"))
    label = {n["id"]: n["label"] for n in graph["nodes"]}
    return [(label.get(link.get("_src", link["source"]), "?"), link["relation"],
             label.get(link.get("_tgt", link["target"]), "?")) for link in graph["links"]]


def test_incremental_rebuild_keeps_cross_file_edges(tmp_path):
    # An update that re-extracts only enemy.gd still has to know that Player is
    # a class_name and GameState an autoload, both declared in unchanged files.
    from graphify.watch import _rebuild_code

    root = _project(tmp_path / "game")
    (root / "project.godot").write_text(
        '[autoload]\nGameState="*res://game_state.gd"\n', encoding="utf-8")
    _write(root, "game_state.gd", "extends Node\nfunc save():\n\tpass\n")
    _write(root, "player.gd",
           "class_name Player\nextends CharacterBody2D\nstatic func make():\n\tpass\n")
    _write(root, "boss.gd", "extends Player\n")
    enemy = _write(root, "enemy.gd", "extends Player\nfunc spawn():\n\tPlayer.make()\n")
    assert _rebuild_code(root, acquire_lock=False) is True
    before = _graph_triples(root)
    assert ("spawn()", "calls", "make()") in before

    enemy.write_text("extends Player\nfunc spawn():\n\tPlayer.make()\n"
                     "func flee():\n\tGameState.save()\n", encoding="utf-8")
    assert _rebuild_code(root, changed_paths=[enemy], acquire_lock=False) is True
    after = _graph_triples(root)
    assert ("spawn()", "calls", "make()") in after
    assert ("flee()", "calls", "save()") in after
    assert after.count(("enemy.gd", "inherits", "Player")) == 1
    assert after.count(("boss.gd", "inherits", "Player")) == 1
