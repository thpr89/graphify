"""Godot extractors.

GDScript (.gd) uses the optional tree-sitter-gdscript grammar. Scenes (.tscn)
and project.godot are INI-like text and are read with stdlib regex, so they
work without the extra.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from graphify.extractors.base import (
    _file_stem,
    _make_id,
    _read_source_bytes,
    _read_source_text,
    _read_text,
)


def _godot_root(path: Path) -> Path | None:
    """Nearest ancestor directory holding `project.godot`, which is what
    `res://` is relative to. Never resolves symlinks: node ids are built from
    paths as given, so a resolved root would disagree with a file's own id."""
    for directory in path.parents:
        if (directory / "project.godot").is_file():
            return directory
    return None


def _res_path(path: Path, res: str) -> Path | None:
    """Map a `res://a/b.gd` reference made from `path` to a filesystem path,
    or None when there is no project root or the reference leaves it."""
    root = _godot_root(path)
    if root is None or not res.startswith("res://"):
        return None
    base = os.path.normpath(root)
    target = os.path.normpath(os.path.join(base, res[len("res://"):]))
    if target == base or not target.startswith(base + os.sep):
        return None
    return Path(target)


def _file_link(path: Path, res: str) -> dict | None:
    """Target fields for a file-to-file edge to `res`, or None if unresolvable.
    Always `imports_from` with `target_file`: that pair is what the corpus
    passes use to keep same-named siblings (player.gd / player.tscn, which
    share one file id) apart when they re-point the edge."""
    target = _res_path(path, res)
    if target is None:
        return None
    return {"target": _make_id(str(target)), "relation": "imports_from",
            "target_file": str(target)}


def extract_gdscript(path: Path) -> dict:
    """Extract classes, functions, signals, enums and their relationships from
    a GDScript source file."""
    try:
        import tree_sitter_gdscript as tsgdscript
        from tree_sitter import Language, Parser
    except ImportError:
        return {"nodes": [], "edges": [], "error": "tree-sitter-gdscript not installed"}

    try:
        parser = Parser(Language(tsgdscript.language()))
        source = _read_source_bytes(path)
        root = parser.parse(source).root_node
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    str_path = str(path)
    stem = _file_stem(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    raw_calls: list[dict] = []
    seen_ids: set[str] = set()
    seen_edges: set[tuple[str, str, str]] = set()

    def text(node) -> str:
        return _read_text(node, source)

    def line_of(node) -> int:
        return node.start_point[0] + 1

    def add_node(nid: str, label: str, line: int, **extra) -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": label,
                "file_type": "code",
                "source_file": str_path,
                "source_location": f"L{line}",
                **extra,
            })

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 confidence: str = "EXTRACTED", **extra) -> None:
        key = (src, tgt, relation)
        if src == tgt or key in seen_edges:
            return
        seen_edges.add(key)
        edges.append({
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": confidence,
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
            **extra,
        })

    file_nid = _make_id(str_path)
    add_node(file_nid, path.name, 1)

    # scope (inner class name, or None for the script itself) -> name -> node id
    funcs: dict[str | None, dict[str, str]] = {}
    signals: dict[str | None, dict[str, str]] = {}
    classes: dict[str, str] = {}
    # (owner id, base class name, line): bound after pass 1 so a base class
    # declared later in the file resolves to its local node, not a stub.
    pending_extends: list[tuple[str, str, int]] = []
    # (caller id, scope, node to walk for calls)
    bodies: list[tuple[str, str | None, Any]] = []

    def scoped_id(scope: str | None, name: str) -> str:
        return _make_id(stem, scope, name) if scope else _make_id(stem, name)

    def name_of(node) -> str | None:
        name_node = node.child_by_field_name("name")
        return text(name_node) if name_node is not None else None

    def string_value(node) -> str:
        return text(node).lstrip("&^").strip("\"'")

    def record_extends(ext, owner_nid: str) -> None:
        base = ext.named_children[0] if ext.named_children else None
        if base is None:
            return
        if base.type == "type":
            pending_extends.append((owner_nid, text(base), line_of(ext)))
        elif base.type == "string":  # extends "res://base.gd"
            link = _file_link(path, string_value(base))
            if link:
                # resolve_gdscript_named_calls adds the `inherits` edge once
                # the corpus passes have settled this edge's target.
                add_edge(owner_nid, link["target"], link["relation"], line_of(ext),
                         target_file=link["target_file"], context="extends")

    # A script that declares `class_name Foo` IS the class Foo: its members
    # hang off the class node. Without one they hang off the file node.
    owner = file_nid
    for child in root.children:
        if child.type == "class_name_statement" and (cname := name_of(child)):
            owner = _make_id(stem, cname)
            add_node(owner, cname, line_of(child), _gdscript_class_name=True)
            add_edge(file_nid, owner, "contains", line_of(child))
            classes[cname] = owner
            break

    def walk_defs(container, owner_nid: str, scope: str | None) -> None:
        member_rel = "contains" if owner_nid == file_nid else "method"
        for child in container.children:
            t = child.type
            line = line_of(child)
            if t == "class_name_statement":
                ext = child.child_by_field_name("extends")
                if ext is not None:
                    record_extends(ext, owner_nid)
            elif t == "extends_statement":
                record_extends(child, owner_nid)
            elif t in ("function_definition", "constructor_definition"):
                name = name_of(child) or "_init"
                nid = scoped_id(scope, name)
                add_node(nid, f"{name}()", line, **({} if scope else {"_gdscript_func": True}))
                add_edge(owner_nid, nid, member_rel, line)
                funcs.setdefault(scope, {}).setdefault(name, nid)
                body = child.child_by_field_name("body")
                if body is not None:
                    bodies.append((nid, scope, body))
            elif t in ("signal_statement", "enum_definition"):
                name = name_of(child)
                if not name:  # anonymous enum
                    continue
                nid = scoped_id(scope, name)
                add_node(nid, name, line)
                add_edge(owner_nid, nid, "contains", line)
                if t == "signal_statement":
                    signals.setdefault(scope, {}).setdefault(name, nid)
            elif t == "class_definition":
                name = name_of(child)
                if not name:
                    continue
                nid = _make_id(stem, name)
                add_node(nid, name, line)
                add_edge(owner_nid, nid, "contains", line)
                classes.setdefault(name, nid)
                ext = child.child_by_field_name("extends")
                if ext is not None:
                    record_extends(ext, nid)
                body = child.child_by_field_name("body")
                if body is not None:
                    walk_defs(body, nid, name)
            elif t == "ERROR":
                # Keep whatever parsed around a syntax error.
                walk_defs(child, owner_nid, scope)
            elif t.endswith("_statement"):
                # var/const initialisers run at load time: credit their calls
                # to the owning class (or file).
                bodies.append((owner_nid, scope, child))

    walk_defs(root, owner, None)

    # A base class from this file is linked now. Any other name is either
    # another script's `class_name` or an engine class, and only the corpus can
    # tell which: leave the name on the node for resolve_gdscript_named_calls.
    # No placeholder node is minted, because `Player` would share its id with a
    # root-level player.gd file node and `Node` would join every script.
    for owner_nid, base, line in pending_extends:
        if base in classes:
            add_edge(owner_nid, classes[base], "inherits", line)
        else:
            next(n for n in nodes if n["id"] == owner_nid)["_gdscript_extends"] = base

    def lookup(table: dict[str | None, dict[str, str]], scope: str | None, name: str) -> str | None:
        return table.get(scope, {}).get(name) or table.get(None, {}).get(name)

    def first_arg(call_node):
        args = call_node.child_by_field_name("arguments")
        return args.named_children[0] if args is not None and args.named_children else None

    def handle_call(node, caller: str, scope: str | None) -> None:
        callee = node.named_children[0] if node.named_children else None
        if callee is None or callee.type != "identifier":
            return
        name = text(callee)
        arg = first_arg(node)
        if name == "emit_signal" and arg is not None and arg.type in ("string", "string_name"):
            sig = lookup(signals, scope, string_value(arg))
            if sig:
                add_edge(caller, sig, "uses", line_of(node))
            return
        if name in ("preload", "load") and arg is not None and arg.type == "string":
            link = _file_link(path, string_value(arg))
            if link:
                add_edge(file_nid, link["target"], link["relation"], line_of(node),
                         target_file=link["target_file"])
            return
        target = lookup(funcs, scope, name)
        if target:
            add_edge(caller, target, "calls", line_of(node))

    def handle_attribute(node, caller: str, scope: str | None) -> None:
        parts = node.named_children
        if len(parts) != 2 or parts[0].type != "identifier" or parts[1].type != "attribute_call":
            return
        receiver = text(parts[0])
        method_node = parts[1].named_children[0] if parts[1].named_children else None
        if method_node is None or method_node.type != "identifier":
            return
        method = text(method_node)
        line = line_of(node)
        if receiver == "self":
            target = lookup(funcs, scope, method)
            if target:
                add_edge(caller, target, "calls", line)
            return
        sig = lookup(signals, scope, receiver)
        if sig:
            if method == "emit":
                add_edge(caller, sig, "uses", line)
            elif method == "connect":
                arg = first_arg(parts[1])
                handler = lookup(funcs, scope, text(arg)) if arg is not None and arg.type == "identifier" else None
                if handler:
                    add_edge(sig, handler, "calls", line, confidence="INFERRED")
            return
        if receiver == "super":
            return
        # `Name.method()`: only another file can say what `Name` is (an
        # autoload, a `class_name`, or just a local variable). Park it for
        # resolve_gdscript_named_calls instead of guessing by method name.
        raw_calls.append({
            "caller_nid": caller,
            "callee": method,
            "receiver": receiver,
            "is_member_call": True,
            "language": "gdscript",
            "source_file": str_path,
            "source_location": f"L{line}",
        })

    for caller, scope, body in bodies:
        stack = [body]
        while stack:
            node = stack.pop()
            if node.type == "call":
                handle_call(node, caller, scope)
            elif node.type == "attribute":
                handle_attribute(node, caller, scope)
            stack.extend(node.children)

    return {"nodes": nodes, "edges": edges, "raw_calls": raw_calls}


_MAX_GODOT_TEXT_BYTES = 50 * 1024 * 1024

_SECTION_RE = re.compile(r"^\[(\w+)(.*)\]\s*$")
# key="quoted", key=ExtResource("id") / ExtResource( 1 ), or key=bare
_ATTR_RE = re.compile(
    r'(\w+)\s*=\s*(?:"([^"]*)"|ExtResource\(\s*"?([^")\s]+)"?\s*\)|([^\s\]]+))'
)
_SCRIPT_PROP_RE = re.compile(r'^script\s*=\s*ExtResource\(\s*"?([^")\s]+)"?\s*\)')
_AUTOLOAD_RE = re.compile(r'^(\w+)\s*=\s*"\*?(res://[^"]+)"')
_MAIN_SCENE_RE = re.compile(r'^run/main_scene\s*=\s*"(res://[^"]+)"')


def _read_godot_text(path: Path) -> tuple[str | None, dict | None]:
    """(text, None), or (None, error result) for an unreadable or huge file."""
    try:
        if path.stat().st_size > _MAX_GODOT_TEXT_BYTES:
            return None, {"nodes": [], "edges": [], "error": "file too large (>50 MB)"}
        return _read_source_text(path), None
    except Exception as e:
        return None, {"nodes": [], "edges": [], "error": str(e)}


def _godot_edge(src: str, tgt: str, relation: str, str_path: str, line: int, **extra) -> dict:
    return {
        "source": src,
        "target": tgt,
        "relation": relation,
        "confidence": "EXTRACTED",
        "source_file": str_path,
        "source_location": f"L{line}",
        "weight": 1.0,
        **extra,
    }


def extract_godot_scene(path: Path) -> dict:
    """Extract a .tscn scene's wiring: the scripts it attaches and the scenes
    it instances (as imports), and the methods its signal connections call."""
    text, error = _read_godot_text(path)
    if text is None:
        return error or {"nodes": [], "edges": []}

    str_path = str(path)
    file_nid = _make_id(str_path)
    nodes: list[dict] = [{"id": file_nid, "label": path.name, "file_type": "code",
                          "source_file": str_path, "source_location": "L1"}]
    edges: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def link(target: str, relation: str, line: int, **extra) -> None:
        if target != file_nid and (relation, target) not in seen:
            seen.add((relation, target))
            edges.append(_godot_edge(file_nid, target, relation, str_path, line, **extra))

    ext: dict[str, tuple[str, str]] = {}      # ext_resource id -> (type, res path)
    node_script: dict[str, str] = {}          # scene node path -> script res path
    connections: list[tuple[str, str, int]] = []  # (target node path, method, line)
    current_node: str | None = None

    for line_no, line in enumerate(text.split("\n"), 1):
        section = _SECTION_RE.match(line)
        if section is None:
            script = _SCRIPT_PROP_RE.match(line)
            if (current_node is not None and script
                    and ext.get(script.group(1), ("", ""))[0] == "Script"):
                node_script[current_node] = ext[script.group(1)][1]
            continue
        kind = section.group(1)
        attrs = {m.group(1): next((g for g in m.groups()[1:] if g is not None), "")
                 for m in _ATTR_RE.finditer(section.group(2))}
        current_node = None
        if kind == "ext_resource" and "id" in attrs and "path" in attrs:
            ext[attrs["id"]] = (attrs.get("type", ""), attrs["path"])
            context = {"Script": "script", "PackedScene": "instance"}.get(attrs.get("type", ""))
            target = _file_link(path, attrs["path"]) if context else None
            if target:
                link(target["target"], target["relation"], line_no,
                     target_file=target["target_file"], context=context)
        elif kind == "node" and "name" in attrs:
            parent = attrs.get("parent")
            if parent is None:
                current_node = "."
            elif parent == ".":
                current_node = attrs["name"]
            else:
                current_node = f"{parent}/{attrs['name']}"
        elif kind == "connection" and "to" in attrs and "method" in attrs:
            connections.append((attrs["to"], attrs["method"], line_no))

    for to, method, line_no in connections:
        script_path = _res_path(path, node_script.get(to, ""))
        if script_path is not None:
            link(_make_id(_file_stem(script_path), method), "calls", line_no,
                 context="signal_connection")

    return {"nodes": nodes, "edges": edges}


def extract_godot_project(path: Path) -> dict:
    """Extract a project.godot's autoload singletons and main scene."""
    text, error = _read_godot_text(path)
    if text is None:
        return error or {"nodes": [], "edges": []}

    str_path = str(path)
    file_nid = _make_id(str_path)
    nodes: list[dict] = [{"id": file_nid, "label": path.name, "file_type": "code",
                          "source_file": str_path, "source_location": "L1"}]
    edges: list[dict] = []
    section = ""

    for line_no, line in enumerate(text.split("\n"), 1):
        header = _SECTION_RE.match(line)
        if header:
            section = header.group(1)
        elif section == "autoload" and (m := _AUTOLOAD_RE.match(line)):
            name, target = m.group(1), _file_link(path, m.group(2))
            nid = _make_id(str_path, "autoload", name)
            nodes.append({"id": nid, "label": name, "file_type": "code",
                          "source_file": str_path, "source_location": f"L{line_no}",
                          "_godot_autoload": True})
            edges.append(_godot_edge(file_nid, nid, "contains", str_path, line_no))
            if target:
                edges.append(_godot_edge(nid, target["target"], target["relation"], str_path,
                                         line_no, target_file=target["target_file"]))
        elif section == "application" and (m := _MAIN_SCENE_RE.match(line)):
            target = _file_link(path, m.group(1))
            if target:
                edges.append(_godot_edge(file_nid, target["target"], target["relation"], str_path,
                                         line_no, target_file=target["target_file"]))

    return {"nodes": nodes, "edges": edges}


def resolve_gdscript_named_calls(
    per_file: list[dict], all_nodes: list[dict], all_edges: list[dict]
) -> None:
    """Bind what a single script cannot: `extends Name` to the script declaring
    `class_name Name`, and parked `Name.method()` calls when `Name` is such a
    class or an autoload singleton (project.godot)."""
    file_source_by_id = {
        node["id"]: str(node["source_file"])
        for node in all_nodes
        if node.get("source_file")
        and node.get("label") == Path(str(node["source_file"])).name
    }
    # ponytail: a name defined in two Godot projects in one scan binds nothing;
    # scope by nearest project.godot if multi-project repos matter.
    scripts_by_name: dict[str, set[str]] = {}
    autoload_names = {
        node["id"]: str(node["label"]) for node in all_nodes if node.get("_godot_autoload")
    }
    for edge in all_edges:
        name = autoload_names.get(edge.get("source"))
        target = file_source_by_id.get(edge.get("target"))
        if name and target and edge.get("relation") == "imports_from" and target.endswith(".gd"):
            scripts_by_name.setdefault(name, set()).add(target)
    functions: dict[tuple[str, str], list[str]] = {}
    class_ids: dict[str, list[str]] = {}
    for node in all_nodes:
        if not node.get("source_file"):
            continue
        source = str(node["source_file"])
        if node.get("_gdscript_class_name"):
            scripts_by_name.setdefault(str(node["label"]), set()).add(source)
            class_ids.setdefault(str(node["label"]), []).append(node["id"])
        elif node.get("_gdscript_func"):
            functions.setdefault((source, str(node["label"]).removesuffix("()")), []).append(node["id"])

    # On an incremental update the lists also hold the unchanged corpus as
    # context, and every edge appended here is kept: only emit for files that
    # were extracted in this run, or unchanged files would gain duplicates.
    batch = {node.get("source_file") for result in per_file for node in result.get("nodes", [])}
    inherits = [
        (node["id"], bases[0], node.get("source_file", ""), node.get("source_location", ""))
        for node in all_nodes
        if node.get("source_file") in batch
        and len(bases := class_ids.get(str(node.get("_gdscript_extends", "")), [])) == 1
    ]
    # `extends "res://base.gd"` was emitted as an import so the corpus passes
    # could settle its target; it is settled now.
    inherits += [
        (edge["source"], edge["target"], edge.get("source_file", ""), edge.get("source_location", ""))
        for edge in all_edges
        if edge.get("context") == "extends" and edge.get("relation") == "imports_from"
        and edge.get("source_file") in batch
        and str(edge.get("source_file", "")).endswith(".gd")
    ]
    for source, target, source_file, location in inherits:
        if source != target:
            all_edges.append({
                "source": source,
                "target": target,
                "relation": "inherits",
                "confidence": "EXTRACTED",
                "source_file": source_file,
                "source_location": location,
                "weight": 1.0,
            })

    existing = {
        (edge.get("source"), edge.get("target"))
        for edge in all_edges
        if edge.get("relation") == "calls"
    }
    for result in per_file:
        for call in result.get("raw_calls", []):
            if call.get("language") != "gdscript":
                continue
            scripts = scripts_by_name.get(str(call.get("receiver", "")), set())
            if len(scripts) != 1:
                continue
            candidates = functions.get((next(iter(scripts)), str(call.get("callee", ""))), [])
            caller = call.get("caller_nid")
            if len(candidates) != 1 or candidates[0] == caller:
                continue
            pair = (caller, candidates[0])
            if pair in existing:
                continue
            existing.add(pair)
            all_edges.append({
                "source": caller,
                "target": candidates[0],
                "relation": "calls",
                "context": "named_receiver_call",
                "confidence": "INFERRED",
                "source_file": call.get("source_file", ""),
                "source_location": call.get("source_location", ""),
                "weight": 1.0,
            })
