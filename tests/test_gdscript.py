"""Tests for the Godot extractors (graphify/extractors/gdscript.py)."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphify.detect import CODE_EXTENSIONS, _SKIP_DIRS
from graphify.extract import _DISPATCH, _EXTRA_FOR_EXTENSION, extract_gdscript
from graphify.extractors import LANGUAGE_EXTRACTORS


# Scenes and project.godot are read with stdlib regex. Their tests must run
# even where the optional grammar is not installed.
_NO_GRAMMAR_NEEDED = {
    "test_godot3_scene_attribute_order_and_numeric_ids",
    "test_project_file_lists_autoloads_and_main_scene",
    "test_scene_and_project_suffixes_are_dispatched",
    "test_scene_multiline_string_does_not_end_the_node",
    "test_only_scripts_and_scenes_that_exist_are_linked",
    "test_godot_sidecar_files_are_not_reported_as_unclassified",
}


@pytest.fixture(autouse=True)
def _skip_without_grammar(request):
    if request.node.name not in _NO_GRAMMAR_NEEDED:
        pytest.importorskip("tree_sitter_gdscript")


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


def test_definitions_after_a_syntax_error_are_kept(tmp_path):
    # The grammar gives up on everything after some errors. Top-level names are
    # still worth having: recovered from the text, without their bodies.
    src = ("func ok():\n\tpass\n\nfunc broken():\n\tvar x = = 3 $$\n\tif:\n\n"
           "static func after():\n\tpass\n\nsignal late\nclass Tail:\n\tpass\n")
    r = extract_gdscript(_write(tmp_path, "bad.gd", src))
    assert {"ok()", "broken()", "after()", "late", "Tail"} <= _labels(r)
    assert len({n["id"] for n in r["nodes"]}) == len(r["nodes"])


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
    assert {e["source"] for e in r["edges"]} == {scene_id}
    assert {e["relation"] for e in r["edges"]} == {"imports_from"}
    # A connection names a method of an attached script. It rides on the edge
    # to that script until resolve_gdscript_named_calls turns it into `calls`.
    assert {e["target"]: e.get("_godot_connections") for e in r["edges"]} == {
        ids["player.gd"]: ["_on_pressed"],
        ids["button.gd"]: ["_on_hover"],
        extract_godot_scene(hud)["nodes"][0]["id"]: None,
    }
    assert {e["target_file"] for e in r["edges"] if e["relation"] == "imports_from"} == {
        str(player), str(button), str(hud)}


def test_godot3_scene_attribute_order_and_numeric_ids(tmp_path):
    from graphify.extract import extract_godot_scene

    root = _project(tmp_path)
    _write(root, "p.gd", "")
    src = ('[gd_scene load_steps=2 format=2]\n'
           '[ext_resource path="res://p.gd" type="Script" id=1]\n'
           '[node name="P" type="Node"]\nscript = ExtResource( 1 )\n'
           '[connection signal="s" from="." to="." method="m"]\n')
    r = extract_godot_scene(_write(root, "old.tscn", src))
    (edge,) = r["edges"]
    assert (edge["relation"], edge["_godot_connections"]) == ("imports_from", ["m"])


def test_project_file_lists_autoloads_and_main_scene(tmp_path):
    from graphify.extract import extract_godot_project

    src = ('[application]\nrun/main_scene="res://main.tscn"\n\n'
           '[autoload]\nGameState="*res://game_state.gd"\nAudio="res://audio.tscn"\n\n'
           '[input]\nGameStateX="*res://nope.gd"\n')
    for name in ("main.tscn", "game_state.gd", "audio.tscn"):
        _write(tmp_path, name, "")
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
    assert not [e for e in r["edges"] if "_godot_connections" in e]

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
    assert ("enemy.gd", "inherits", "Player") in after
    assert ("boss.gd", "inherits", "Player") in after


def test_preload_and_load_import_the_file(tmp_path):
    root = _project(tmp_path)
    base = _write(root, "base.gd", "func f():\n\tpass\n")
    other = _write(root, "other.gd", "func f():\n\tpass\n")
    r = extract_gdscript(_write(root, "user.gd",
                                'const B = preload("res://base.gd")\n'
                                'func g():\n\tvar s = load("res://other.gd")\n'))
    links = {e["target_file"]: e.get("context") for e in r["edges"] if e["relation"] == "imports_from"}
    assert links == {str(base): None, str(other): None}


def test_res_paths_resolve_for_relative_input_paths(tmp_path, monkeypatch):
    root = _project(tmp_path)
    _write(root, "base.gd", "func f():\n\tpass\n")
    _write(root, "a.gd", 'const B = preload("res://base.gd")\n')
    monkeypatch.chdir(root)
    r = extract_gdscript(Path("a.gd"))
    assert [e["target"] for e in r["edges"] if e["relation"] == "imports_from"] == [
        extract_gdscript(Path("base.gd"))["nodes"][0]["id"]]


def test_names_that_normalise_alike_stay_separate(tmp_path):
    # Node ids fold case and underscores, so `_init`/`init` and an enum `State`
    # next to `func state()` would otherwise merge into one node.
    src = ("enum State { A }\nfunc _init():\n\tpass\nfunc init():\n\tsetup()\n"
           "func setup():\n\tpass\nfunc state():\n\tinit()\n")
    r = extract_gdscript(_write(tmp_path, "a.gd", src))
    assert {"State", "_init()", "init()", "setup()", "state()"} <= _labels(r)
    assert len({n["id"] for n in r["nodes"]}) == len(r["nodes"])
    assert _rel_pairs(r, "calls") == {("init()", "setup()"), ("state()", "init()")}


def test_other_languages_do_not_bind_to_autoloads(tmp_path):
    # An autoload entry is a bare type-like name in project.godot. A Python
    # `GameState()` or `class Foo(GameState)` must not land on it.
    r = _corpus(tmp_path, {
        "tools/build.py": "def run():\n    GameState()\n\nclass Foo(GameState):\n    pass\n"})
    by_id = {n["id"]: n for n in r["nodes"]}
    into_autoload = [
        (by_id[e["source"]]["label"], e["relation"]) for e in r["edges"]
        if e["target"] in by_id and by_id[e["target"]].get("_godot_autoload")
        and str(e.get("source_file", "")).endswith(".py")]
    assert into_autoload == []


def test_scene_only_incremental_rebuild_keeps_connection_calls(tmp_path):
    from graphify.watch import _rebuild_code

    root = _project(tmp_path / "game")
    _write(root, "scripts/player.gd", "func _on_pressed():\n\tpass\n")
    body = ('[gd_scene format=3]\n'
            '[ext_resource type="Script" path="res://scripts/player.gd" id="1"]\n'
            '[node name="P" type="Node"]\nscript = ExtResource("1")\n'
            '[connection signal="pressed" from="." to="." method="_on_pressed"]\n')
    scene = _write(root, "scenes/main.tscn", body)
    assert _rebuild_code(root, acquire_lock=False) is True
    assert ("main.tscn", "calls", "_on_pressed()") in _graph_triples(root)

    scene.write_text(body + '[node name="Extra" type="Node" parent="."]\n', encoding="utf-8")
    assert _rebuild_code(root, changed_paths=[scene], acquire_lock=False) is True
    assert ("main.tscn", "calls", "_on_pressed()") in _graph_triples(root)


def test_cached_rerun_keeps_scene_connection_calls(tmp_path):
    # The resolver consumes the connection list from the in-memory edge; the
    # per-file cache must still hold it for the next run.
    scene = ('[gd_scene format=3]\n'
             '[ext_resource type="Script" path="res://player.gd" id="1"]\n'
             '[node name="P" type="Node"]\nscript = ExtResource("1")\n'
             '[connection signal="pressed" from="." to="." method="_ready"]\n')
    for _ in range(2):
        r = _corpus(tmp_path, {"main.tscn": scene})
        assert ("main.tscn", "_ready()") in _rel_pairs(r, "calls")


def test_only_scripts_and_scenes_that_exist_are_linked(tmp_path):
    # A texture, a model imported as a PackedScene, or a file that is not there
    # would each become a phantom leaf node, the missing one named after this
    # machine's absolute path.
    from graphify.extract import extract_godot_scene

    root = _project(tmp_path)
    _write(root, "icon.png", "x")
    _write(root, "tree.glb", "x")
    scene = ('[gd_scene format=3]\n'
             '[ext_resource type="PackedScene" path="res://tree.glb" id="1"]\n'
             '[ext_resource type="Script" path="res://gone.gd" id="2"]\n'
             '[ext_resource type="Texture2D" path="res://icon.png" id="3"]\n')
    assert extract_godot_scene(_write(root, "m.tscn", scene))["edges"] == []


def test_preloaded_assets_are_not_linked(tmp_path):
    root = _project(tmp_path)
    _write(root, "icon.png", "x")
    src = 'const I = preload("res://icon.png")\nconst M = preload("res://missing.gd")\n'
    assert extract_gdscript(_write(root, "a.gd", src))["edges"] == []


def test_relative_preload_paths(tmp_path):
    root = _project(tmp_path)
    base = _write(root, "base.gd", "")
    sibling = _write(root, "sub/sibling.gd", "")
    r = extract_gdscript(_write(root, "sub/child.gd",
                                'const A = preload("sibling.gd")\n'
                                'const B = preload("./sibling.gd")\n'
                                'const C = preload("../base.gd")\n'
                                'const D = preload("../../outside.gd")\n'))
    assert {e["target_file"] for e in r["edges"]} == {str(base), str(sibling)}


def test_uid_references_resolve(tmp_path):
    # Godot 4.4+ writes `uid://` for the main scene and accepts it anywhere a
    # path goes. A script's uid is in its `.gd.uid` sidecar, a scene's in its
    # own header.
    from graphify.extract import extract_godot_project

    root = _project(tmp_path)
    script = _write(root, "scripts/state.gd", "func save():\n\tpass\n")
    _write(root, "scripts/state.gd.uid", "uid://bscript1\n")
    scene = _write(root, "main.tscn", '[gd_scene load_steps=1 format=3 uid="uid://cscene1"]\n')
    project = _write(root, "project.godot",
                     '[application]\nrun/main_scene="uid://cscene1"\n'
                     '[autoload]\nState="*uid://bscript1"\nGhost="*uid://unknown"\n')
    r = extract_godot_project(project)
    assert {e["target_file"] for e in r["edges"] if e["relation"] == "imports_from"} == {
        str(script), str(scene)}
    assert {n["label"] for n in r["nodes"] if n.get("_godot_autoload")} == {"State", "Ghost"}
    user = extract_gdscript(_write(root, "user.gd", 'const S = preload("uid://bscript1")\n'))
    assert [e["target_file"] for e in user["edges"]] == [str(script)]


def test_chained_default_argument_and_local_class_calls(tmp_path):
    src = ("class_name Kit\n"
           "static func make(a := helper()):\n"
           "\tGameState.fetch().apply()\n"
           "\tself.b().c()\n"
           "\tInner.build()\n"
           "\tKit.helper()\n"
           "\tInner.new()\n"
           "static func helper():\n\tpass\n"
           "func b():\n\tpass\n"
           "class Inner:\n\tstatic func build():\n\t\tpass\n")
    r = extract_gdscript(_write(tmp_path, "kit.gd", src))
    assert _rel_pairs(r, "calls") == {
        ("make()", "helper()"), ("make()", "b()"), ("make()", "build()")}
    assert [(c["receiver"], c["callee"]) for c in r["raw_calls"]] == [("GameState", "fetch")]


def test_same_named_inner_classes_stay_separate(tmp_path):
    src = ("class A:\n\tclass B:\n\t\tfunc f():\n\t\t\tpass\n"
           "class C:\n\tclass B:\n\t\tfunc g():\n\t\t\tpass\n")
    r = extract_gdscript(_write(tmp_path, "nest.gd", src))
    by_id = {n["id"]: n["label"] for n in r["nodes"]}
    assert sorted(by_id.values()) == ["A", "B", "B", "C", "f()", "g()", "nest.gd"]
    b_of = {by_id[e["source"]]: e["target"] for e in r["edges"]
            if e["relation"] == "contains" and by_id[e["target"]] == "B"}
    assert b_of["A"] != b_of["C"]
    owner_of = {by_id[e["target"]]: e["source"] for e in r["edges"] if e["relation"] == "method"}
    assert (owner_of["f()"], owner_of["g()"]) == (b_of["A"], b_of["C"])


def test_inner_class_scope(tmp_path):
    # An inner class is its own class: `self` is never the outer script, and of
    # the outer script only static functions are in reach. Members of a base
    # class declared in the same file are.
    src = ("signal done\n"
           "func helper():\n\tpass\n"
           "static func util():\n\tpass\n"
           "class Inner:\n\tfunc run():\n\t\tself.helper()\n\t\thelper()\n\t\tutil()\n\t\tdone.emit()\n"
           "class Base:\n\tsignal ping\n\tfunc poke():\n\t\tpass\n"
           "class Child extends Base:\n\tfunc go():\n\t\tpoke()\n\t\tself.poke()\n\t\tping.emit()\n")
    r = extract_gdscript(_write(tmp_path, "scope.gd", src))
    assert _rel_pairs(r, "calls") == {("run()", "util()"), ("go()", "poke()")}
    assert _rel_pairs(r, "uses") == {("go()", "ping")}


def test_scene_multiline_string_does_not_end_the_node(tmp_path):
    from graphify.extract import extract_godot_scene

    root = _project(tmp_path)
    _write(root, "rich.gd", "")
    scene = ('[gd_scene format=3]\n'
             '[ext_resource type="Script" path="res://rich.gd" id="1"]\n'
             '[node name="Root" type="Control"]\n'
             '[node name="Rich" type="RichTextLabel" parent="."]\n'
             'text = "Title\n[center]Hello[/center]\n[b]x[/b] \\"quoted\\"\n"\n'
             'script = ExtResource("1")\n'
             '[connection signal="s" from="." to="Rich" method="_on_rich"]\n')
    (edge,) = extract_godot_scene(_write(root, "rich.tscn", scene))["edges"]
    assert edge["_godot_connections"] == ["_on_rich"]


def test_connection_to_an_instanced_scene_calls_its_root_script(tmp_path):
    hud = ('[gd_scene format=3]\n'
           '[ext_resource type="Script" path="res://hud.gd" id="1"]\n'
           '[ext_resource type="Script" path="res://player.gd" id="2"]\n'
           '[node name="HUD" type="Control"]\nscript = ExtResource("1")\n'
           '[node name="Other" type="Node" parent="."]\nscript = ExtResource("2")\n')
    main = ('[gd_scene format=3]\n'
            '[ext_resource type="PackedScene" path="res://hud.tscn" id="1"]\n'
            '[node name="Main" type="Node"]\n'
            '[node name="HUD" parent="." instance=ExtResource("1")]\n'
            '[connection signal="s" from="." to="HUD" method="_on_shown"]\n'
            '[connection signal="s" from="." to="HUD" method="make"]\n')
    r = _corpus(tmp_path, {"hud.gd": "func _on_shown():\n\tpass\n",
                           "hud.tscn": hud, "main.tscn": main})
    # `make` exists only on player.gd, which is not the instanced scene's root.
    assert {t for s, t in _rel_pairs(r, "calls") if s == "main.tscn"} == {"_on_shown()"}


def test_extends_by_path_lands_on_the_class_when_the_script_names_one(tmp_path):
    r = _corpus(tmp_path, {"hero.gd": 'extends "res://player.gd"\n'})
    by_id = {n["id"]: n for n in r["nodes"]}
    targets = [by_id[e["target"]] for e in r["edges"]
               if e["relation"] == "inherits" and by_id[e["source"]]["label"] == "hero.gd"]
    assert [(t["label"], bool(t.get("_gdscript_class_name"))) for t in targets] == [("Player", True)]


def test_resolver_scratch_keys_do_not_reach_the_graph(tmp_path):
    r = _corpus(tmp_path)
    assert not [n for n in r["nodes"] if "_gdscript_extends" in n]
    assert not [e for e in r["edges"] if "_godot_connections" in e]


def test_autoload_named_in_two_projects_binds_nothing(tmp_path):
    from graphify.extract import extract

    paths = []
    for game in ("one", "two"):
        root = _project(tmp_path / game)
        (root / "project.godot").write_text(
            '[autoload]\nGlobals="*res://globals.gd"\n', encoding="utf-8")
        paths += [root / "project.godot",
                  _write(root, "globals.gd", "func reset():\n\tpass\n"),
                  _write(root, "user.gd", "func f():\n\tGlobals.reset()\n")]
    r = extract(paths, cache_root=tmp_path / "cache", root=tmp_path, parallel=False)
    assert not _rel_pairs(r, "calls")


VIEW = ('const Board := preload("res://board.gd")\n'
        "func draw():\n\tBoard.clear()\n\tBoard.new()\n\tBoard.missing()\n")


def test_calls_through_a_preloaded_constant_resolve(tmp_path):
    # `const X := preload("res://x.gd")` then `X.fn()` is how a project without
    # class_name declarations reaches its other scripts.
    r = _corpus(tmp_path, {"board.gd": "static func clear():\n\tpass\n", "view.gd": VIEW})
    assert ("draw()", "clear()") in _rel_pairs(r, "calls")
    assert ("draw()", "board.gd") in _rel_pairs(r, "references")
    assert not [e for e in r["edges"] if "_gdscript_consts" in e]


def test_a_constant_name_belongs_to_the_script_that_declares_it(tmp_path):
    r = _corpus(tmp_path, {
        "board.gd": "static func clear():\n\tpass\n",
        "other/board.gd": "static func wipe():\n\tpass\n",
        "view.gd": VIEW,
        "other/view.gd": ('const Board = preload("board.gd")\n'
                          "func redraw():\n\tBoard.wipe()\n\tBoard.clear()\n"),
    })
    calls = _rel_pairs(r, "calls")
    assert ("draw()", "clear()") in calls and ("redraw()", "wipe()") in calls
    assert ("redraw()", "clear()") not in calls


def test_new_on_a_named_class_references_it(tmp_path):
    r = _corpus(tmp_path, {"spawner.gd": "func spawn():\n\tPlayer.new()\n"})
    assert ("spawn()", "Player") in _rel_pairs(r, "references")


def test_incremental_rebuild_keeps_preloaded_constant_calls(tmp_path):
    from graphify.watch import _rebuild_code

    root = _project(tmp_path / "game")
    _write(root, "board.gd", "static func clear():\n\tpass\n")
    view = _write(root, "view.gd", VIEW)
    assert _rebuild_code(root, acquire_lock=False) is True
    assert ("draw()", "calls", "clear()") in _graph_triples(root)

    view.write_text(VIEW + "func again():\n\tBoard.clear()\n", encoding="utf-8")
    assert _rebuild_code(root, changed_paths=[view], acquire_lock=False) is True
    after = _graph_triples(root)
    assert ("draw()", "calls", "clear()") in after
    assert ("again()", "calls", "clear()") in after


def test_swift_files_in_the_project_do_not_bind_gdscript_calls(tmp_path):
    # The Swift member-call pass reads every language's parked calls. With an
    # iOS plugin in the project it bound `STEPS.size()` to anything named steps.
    pytest.importorskip("tree_sitter_swift")
    r = _corpus(tmp_path, {
        "ios/haptics.swift": ("class Haptics {\n    func buzz() {}\n}\nclass Steps {}\n"
                              "func go() {\n    let h = Haptics()\n    h.buzz()\n}\n"),
        "tutorial.gd": "const STEPS := [1]\nfunc run():\n\tSTEPS.size()\n\tHaptics.buzz()\n",
    })
    calls = _rel_pairs(r, "calls")
    assert ("go()", ".buzz()") in calls  # the pass still serves Swift
    assert ("run()", ".buzz()") not in calls
    assert ("run()", "Steps") not in _rel_pairs(r, "references")


def test_godot_sidecar_files_are_not_reported_as_unclassified(tmp_path):
    # Godot writes `x.gd.uid` and `icon.png.import` beside the file they
    # describe; a project is half sidecars, which read as "no extractor".
    from graphify.detect import detect

    root = _project(tmp_path / "game")
    for name in ("player.gd", "player.gd.uid", "icon.png", "icon.png.import", "stray.uid"):
        _write(root, name, "x")
    assert [Path(p).name for p in detect(root)["unclassified"]] == ["stray.uid"]
