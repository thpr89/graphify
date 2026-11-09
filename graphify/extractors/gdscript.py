"""Godot extractors.

GDScript (.gd) uses the optional tree-sitter-gdscript grammar. Scenes (.tscn)
and project.godot are INI-like text and are read with stdlib regex, so they
work without the extra.
"""
from __future__ import annotations

import functools
import os
import re
from pathlib import Path
from typing import Any

from graphify.ids import decl_salt
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


_SCENE_UID_RE = re.compile(r'\buid="(uid://[^"]+)"')
_SIDECAR_UID_RE = re.compile(r"(uid://\S+)")
# The only files that become graph nodes. A link to anything else (a texture,
# a .glb imported as a PackedScene) would mint a phantom leaf node.
_LINKABLE_SUFFIXES = (".gd", ".tscn")


@functools.lru_cache(maxsize=8)
def _uid_index(root: str) -> dict[str, str]:
    """`uid://…` -> path under `root`, for every script (`x.gd.uid` sidecar,
    Godot 4.4+) and scene (`uid=` in its header).
    ponytail: built once per process per project, so a file added while a
    watcher runs resolves by uid only after a restart; key on mtimes if that
    ever matters."""
    index: dict[str, str] = {}
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = [d for d in subdirs if not d.startswith(".")]
        for name in files:
            sidecar = name.endswith(".uid")
            target = name[:-len(".uid")] if sidecar else name
            if not target.endswith(_LINKABLE_SUFFIXES):
                continue
            try:
                with open(os.path.join(directory, name), encoding="utf-8", errors="replace") as f:
                    first = f.readline(4096)
            except OSError:
                continue
            match = _SIDECAR_UID_RE.match(first) if sidecar else _SCENE_UID_RE.search(first)
            if match:
                index.setdefault(match.group(1), os.path.join(directory, target))
    return index


def _res_path(path: Path, ref: str) -> Path | None:
    """Map a reference made from `path` to the script or scene file it names,
    or None. `ref` is `res://a/b.gd`, `uid://…`, or a path relative to `path`.
    None when the file is not a script or scene, does not exist (a link would
    mint a node named after this machine's absolute path), or lies outside the
    project."""
    root = _godot_root(path)
    base = os.path.normpath(root) if root is not None else None
    if ref.startswith("uid://"):
        target = _uid_index(base).get(ref) if base is not None else None
    elif ref.startswith("res://"):
        target = os.path.join(base, ref[len("res://"):]) if base is not None else None
    elif ref and "://" not in ref and not os.path.isabs(ref):
        target = os.path.join(path.parent, ref)
    else:
        target = None
    if target is None:
        return None
    target = os.path.normpath(target)
    if base is not None:
        # relpath, not a prefix test: for a relative input path run from the
        # project root, base is "." and the target carries no "./" prefix.
        inside = os.path.relpath(target, base)
        if inside in (".", "..") or inside.startswith(".." + os.sep):
            return None
    if not target.endswith(_LINKABLE_SUFFIXES) or not os.path.isfile(target):
        return None
    return Path(target)


def _file_link(path: Path, ref: str) -> dict | None:
    """Target fields for a file-to-file edge to `ref`, or None if unresolvable.
    Always `imports_from` with `target_file`: that pair is what the corpus
    passes use to keep same-named siblings (player.gd / player.tscn, which
    share one file id) apart when they re-point the edge."""
    target = _res_path(path, ref)
    if target is None:
        return None
    return {"target": _make_id(str(target)), "relation": "imports_from",
            "target_file": str(target)}


# Top-level declarations, for the text after a syntax error the grammar gave up on.
_FALLBACK_DECL_RE = re.compile(
    rb"^(?:(?:static[ \t]+)?func|(signal)|(class))[ \t]+([A-Za-z_]\w*)", re.MULTILINE
)

Scope = tuple[str, ...]  # enclosing inner class names; () is the script itself


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
    node_by_id: dict[str, dict] = {}
    seen_edges: set[tuple[str, str, str]] = set()

    def text(node) -> str:
        return _read_text(node, source)

    def line_of(node) -> int:
        return node.start_point[0] + 1

    def add_node(nid: str, label: str, line: int, **extra) -> None:
        if nid not in node_by_id:
            node_by_id[nid] = {
                "id": nid,
                "label": label,
                "file_type": "code",
                "source_file": str_path,
                "source_location": f"L{line}",
                **extra,
            }
            nodes.append(node_by_id[nid])

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

    funcs: dict[Scope, dict[str, str]] = {}    # scope -> function name -> node id
    signals: dict[Scope, dict[str, str]] = {}  # scope -> signal name -> node id
    statics: set[str] = set()                  # script-level `static func` names
    class_scope: dict[str, Scope] = {}         # class name -> its scope (first wins)
    class_ids: dict[Scope, str] = {}           # scope -> class node id
    bases: dict[Scope, Scope] = {}             # class -> base class from this file
    # (owner id, scope, base class name, line): bound after pass 1 so a base
    # class declared later in the file is known.
    pending_extends: list[tuple[str, Scope, str, int]] = []
    # (caller id, scope, node to walk for calls)
    bodies: list[tuple[str, Scope, Any]] = []

    def scoped_id(scope: Scope, name: str) -> str:
        """Id for a declaration. Ids fold case and underscores, so `_init` and
        `init`, or `enum State` and `func state()`, would share one: the first
        keeps the plain id and later ones are salted with their exact name."""
        nid = _make_id(stem, *scope, name)
        return _make_id(nid, decl_salt(name)) if nid in node_by_id else nid

    def name_of(node) -> str | None:
        name_node = node.child_by_field_name("name")
        return text(name_node) if name_node is not None else None

    def string_value(node) -> str:
        return text(node).lstrip("&^").strip("\"'")

    def record_extends(ext, owner_nid: str, scope: Scope) -> None:
        base = ext.named_children[0] if ext.named_children else None
        if base is None:
            return
        if base.type == "string":  # extends "res://base.gd"
            link = _file_link(path, string_value(base))
            if link:
                # resolve_gdscript_named_calls adds the `inherits` edge once
                # the corpus passes have settled this edge's target.
                add_edge(owner_nid, link["target"], link["relation"], line_of(ext),
                         target_file=link["target_file"], context="extends")
        elif base.type == "type" and '"' not in text(base):
            pending_extends.append((owner_nid, scope, text(base), line_of(ext)))

    def declare_function(owner_nid: str, scope: Scope, name: str, line: int, static: bool) -> str:
        nid = scoped_id(scope, name)
        add_node(nid, f"{name}()", line, **({} if scope else {"_gdscript_func": True}))
        add_edge(owner_nid, nid, "contains" if owner_nid == file_nid else "method", line)
        funcs.setdefault(scope, {}).setdefault(name, nid)
        if static and not scope:
            statics.add(name)
        return nid

    def declare_member(owner_nid: str, scope: Scope, name: str, line: int) -> str:
        nid = scoped_id(scope, name)
        add_node(nid, name, line)
        add_edge(owner_nid, nid, "contains", line)
        return nid

    def declare_class(owner_nid: str, scope: Scope, name: str, line: int) -> tuple[str, Scope]:
        inner = (*scope, name)
        nid = declare_member(owner_nid, scope, name, line)
        class_scope.setdefault(name, inner)
        class_ids[inner] = nid
        return nid, inner

    # A script that declares `class_name Foo` IS the class Foo: its members
    # hang off the class node. Without one they hang off the file node.
    owner = file_nid
    for child in root.children:
        if child.type == "class_name_statement" and (cname := name_of(child)):
            owner = _make_id(stem, cname)
            add_node(owner, cname, line_of(child), _gdscript_class_name=True)
            add_edge(file_nid, owner, "contains", line_of(child))
            class_scope[cname] = ()
            break
    class_ids[()] = owner

    def walk_defs(container, owner_nid: str, scope: Scope) -> None:
        for child in container.children:
            t = child.type
            line = line_of(child)
            if t == "class_name_statement":
                ext = child.child_by_field_name("extends")
                if ext is not None:
                    record_extends(ext, owner_nid, scope)
            elif t == "extends_statement":
                record_extends(child, owner_nid, scope)
            elif t in ("function_definition", "constructor_definition"):
                static = any(c.type == "static_keyword" for c in child.children)
                nid = declare_function(owner_nid, scope, name_of(child) or "_init", line, static)
                # Default arguments are evaluated by the function too.
                for field in ("parameters", "body"):
                    part = child.child_by_field_name(field)
                    if part is not None:
                        bodies.append((nid, scope, part))
            elif t in ("signal_statement", "enum_definition"):
                name = name_of(child)
                if not name:  # anonymous enum
                    continue
                nid = declare_member(owner_nid, scope, name, line)
                if t == "signal_statement":
                    signals.setdefault(scope, {}).setdefault(name, nid)
            elif t == "class_definition":
                name = name_of(child)
                if not name:
                    continue
                nid, inner = declare_class(owner_nid, scope, name, line)
                ext = child.child_by_field_name("extends")
                if ext is not None:
                    record_extends(ext, nid, inner)
                body = child.child_by_field_name("body")
                if body is not None:
                    walk_defs(body, nid, inner)
            elif t == "ERROR":
                # Keep whatever parsed around a syntax error.
                walk_defs(child, owner_nid, scope)
            elif t.endswith("_statement"):
                # var/const initialisers run at load time: credit their calls
                # to the owning class (or file).
                bodies.append((owner_nid, scope, child))

    walk_defs(root, owner, ())

    if root.has_error:
        # After some errors the grammar drops the rest of the file. Recover the
        # top-level names from the text; their bodies and calls stay unknown.
        for match in _FALLBACK_DECL_RE.finditer(source):
            name = match.group(3).decode("utf-8", "replace")
            line = source.count(b"\n", 0, match.start()) + 1
            if match.group(1):
                if name not in signals.get((), {}):
                    signals.setdefault((), {})[name] = declare_member(owner, (), name, line)
            elif match.group(2):
                if name not in class_scope:
                    declare_class(owner, (), name, line)
            elif name not in funcs.get((), {}):
                declare_function(owner, (), name, line, match.group(0).startswith(b"static"))

    # A base class from this file is linked now. Any other name is either
    # another script's `class_name` or an engine class, and only the corpus can
    # tell which: leave the name on the node for resolve_gdscript_named_calls.
    # No placeholder node is minted, because `Player` would share its id with a
    # root-level player.gd file node and `Node` would join every script.
    for owner_nid, scope, base, line in pending_extends:
        base_scope = class_scope.get(base)
        if base_scope is not None and base_scope != scope:
            add_edge(owner_nid, class_ids[base_scope], "inherits", line)
            bases[scope] = base_scope
        elif base_scope is None:
            node_by_id[owner_nid]["_gdscript_extends"] = base

    def member(table: dict[Scope, dict[str, str]], scope: Scope | None, name: str) -> str | None:
        """`name` as a member of the class at `scope`: its own, or inherited
        from a base class declared in this file. Never the enclosing script's:
        an inner class is a class of its own."""
        visited: set[Scope] = set()
        while scope is not None and scope not in visited:
            if name in table.get(scope, {}):
                return table[scope][name]
            visited.add(scope)
            scope = bases.get(scope)
        return None

    def callable_named(scope: Scope, name: str) -> str | None:
        """What a bare `name()` calls: a member, or failing that one of the
        script's static functions, which an inner class can reach unqualified."""
        return member(funcs, scope, name) or (funcs[()].get(name) if name in statics else None)

    def first_arg(call_node):
        args = call_node.child_by_field_name("arguments")
        return args.named_children[0] if args is not None and args.named_children else None

    def handle_call(node, caller: str, scope: Scope) -> None:
        callee = node.named_children[0] if node.named_children else None
        if callee is None or callee.type != "identifier":
            return
        name = text(callee)
        arg = first_arg(node)
        if name == "emit_signal" and arg is not None and arg.type in ("string", "string_name"):
            sig = member(signals, scope, string_value(arg))
            if sig:
                add_edge(caller, sig, "uses", line_of(node))
            return
        if name in ("preload", "load") and arg is not None and arg.type == "string":
            link = _file_link(path, string_value(arg))
            if link:
                add_edge(file_nid, link["target"], link["relation"], line_of(node),
                         target_file=link["target_file"])
            return
        target = callable_named(scope, name)
        if target:
            add_edge(caller, target, "calls", line_of(node))

    def handle_attribute(node, caller: str, scope: Scope) -> None:
        # `receiver.method(...)`, possibly with more chained after it
        # (`a.b().c()`): only the first hop has a receiver we can name.
        parts = node.named_children
        if len(parts) < 2 or parts[0].type != "identifier" or parts[1].type != "attribute_call":
            return
        receiver = text(parts[0])
        method_node = parts[1].named_children[0] if parts[1].named_children else None
        if method_node is None or method_node.type != "identifier":
            return
        method = text(method_node)
        line = line_of(node)
        if receiver == "super":
            return
        sig = member(signals, scope, receiver)
        if sig:
            if method == "emit":
                add_edge(caller, sig, "uses", line)
            elif method == "connect":
                arg = first_arg(parts[1])
                handler = member(funcs, scope, text(arg)) if arg is not None and arg.type == "identifier" else None
                if handler:
                    add_edge(sig, handler, "calls", line, confidence="INFERRED")
            return
        if receiver == "self" or receiver in class_scope:
            # `self.m()`, or `Inner.m()` / `ThisScript.m()` on a class from
            # this file. A miss (`Inner.new()`) is an engine method, not ours.
            target = member(funcs, scope if receiver == "self" else class_scope[receiver], method)
            if target:
                add_edge(caller, target, "calls", line)
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
_AUTOLOAD_RE = re.compile(r'^(\w+)\s*=\s*"\*?((?:res|uid)://[^"]+)"')
_MAIN_SCENE_RE = re.compile(r'^run/main_scene\s*=\s*"((?:res|uid)://[^"]+)"')
_UNESCAPED_QUOTE_RE = re.compile(r'(?<!\\)"')


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
    edge_to: dict[str, dict] = {}  # res path -> the edge to that file

    ext: dict[str, tuple[str, str]] = {}      # ext_resource id -> (type, res path)
    # scene node path -> res path of what answers its signals: its script, or
    # for an instanced child without one, the scene it instances.
    node_target: dict[str, str] = {}
    connections: list[tuple[str, str, int]] = []  # (target node path, method, line)
    current_node: str | None = None
    in_string = False  # inside a property value that spans lines

    for line_no, line in enumerate(text.split("\n"), 1):
        # A multi-line string value (BBCode in a RichTextLabel, say) can hold
        # a line like `[center]Hello[/center]` that is not a section header.
        odd_quotes = len(_UNESCAPED_QUOTE_RE.findall(line)) % 2 == 1
        if in_string:
            in_string = not odd_quotes
            continue
        section = _SECTION_RE.match(line)
        if section is None:
            in_string = odd_quotes
            script = _SCRIPT_PROP_RE.match(line)
            if (current_node is not None and script
                    and ext.get(script.group(1), ("", ""))[0] == "Script"):
                node_target[current_node] = ext[script.group(1)][1]
            continue
        kind = section.group(1)
        attrs = {m.group(1): next((g for g in m.groups()[1:] if g is not None), "")
                 for m in _ATTR_RE.finditer(section.group(2))}
        current_node = None
        if kind == "ext_resource" and "id" in attrs and "path" in attrs:
            ext[attrs["id"]] = (attrs.get("type", ""), attrs["path"])
            context = {"Script": "script", "PackedScene": "instance"}.get(attrs.get("type", ""))
            target = _file_link(path, attrs["path"]) if context else None
            if target and target["target"] != file_nid and attrs["path"] not in edge_to:
                edge_to[attrs["path"]] = _godot_edge(
                    file_nid, target["target"], target["relation"], str_path, line_no,
                    target_file=target["target_file"], context=context)
                edges.append(edge_to[attrs["path"]])
        elif kind == "node" and "name" in attrs:
            parent = attrs.get("parent")
            if parent is None:
                node_path = "."
            elif parent == ".":
                node_path = attrs["name"]
            else:
                node_path = f"{parent}/{attrs['name']}"
            current_node = node_path
            instanced = ext.get(attrs.get("instance", ""), ("", ""))
            if instanced[0] == "PackedScene":
                node_target[node_path] = instanced[1]
        elif kind == "connection" and "to" in attrs and "method" in attrs:
            connections.append((attrs["to"], attrs["method"], line_no))

    # The script on the scene's root node is what answers for the scene when
    # another scene instances it.
    root_edge = edge_to.get(node_target.get(".", ""))
    if root_edge is not None and root_edge["context"] == "script":
        root_edge["context"] = "root_script"

    # A connection calls a method of the script attached to its target node
    # (for an instanced child: of that scene's root script). The method's node
    # id belongs to another file, and an id minted here from that file's path
    # is not re-pointed when only this scene is re-extracted. So the method
    # name rides on the edge to that file, whose target the corpus passes do
    # settle, and resolve_gdscript_named_calls binds it.
    for to, method, _line_no in connections:
        edge = edge_to.get(node_target.get(to, ""))
        if edge is not None and method not in edge.setdefault("_godot_connections", []):
            edge["_godot_connections"].append(method)

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
    `class_name Name`, parked `Name.method()` calls when `Name` is such a
    class or an autoload singleton (project.godot), and a scene's signal
    connections to the methods of its attached scripts."""
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
    class_of_script: dict[str, str] = {}  # script source file -> its class_name node
    for node in all_nodes:
        if not node.get("source_file"):
            continue
        source = str(node["source_file"])
        if node.get("_gdscript_class_name"):
            scripts_by_name.setdefault(str(node["label"]), set()).add(source)
            class_ids.setdefault(str(node["label"]), []).append(node["id"])
            class_of_script[source] = node["id"]
        elif node.get("_gdscript_func"):
            functions.setdefault((source, str(node["label"]).removesuffix("()")), []).append(node["id"])

    # On an incremental update the lists also hold the unchanged corpus as
    # context, and every edge appended here is kept: only emit for files that
    # were extracted in this run, or unchanged files would gain duplicates.
    batch = {node.get("source_file") for result in per_file for node in result.get("nodes", [])}
    # The base name is popped: it is scratch for this pass, and the per-file
    # cache, written before it, keeps its own copy.
    inherits = [
        (node["id"], bases[0], node.get("source_file", ""), node.get("source_location", ""))
        for node in all_nodes
        if len(bases := class_ids.get(str(node.pop("_gdscript_extends", "")), [])) == 1
        and node.get("source_file") in batch
    ]
    # `extends "res://base.gd"` was emitted as an import so the corpus passes
    # could settle its target; it is settled now. Like `extends Name`, it lands
    # on the base script's class when that script declares a `class_name`.
    inherits += [
        (edge["source"],
         class_of_script.get(file_source_by_id.get(edge["target"], ""), edge["target"]),
         edge.get("source_file", ""), edge.get("source_location", ""))
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
    def add_call(caller, target, source_file, location, confidence: str, context: str) -> None:
        if caller == target or (caller, target) in existing:
            return
        existing.add((caller, target))
        all_edges.append({
            "source": caller,
            "target": target,
            "relation": "calls",
            "context": context,
            "confidence": confidence,
            "source_file": source_file,
            "source_location": location,
            "weight": 1.0,
        })

    # Scene signal connections, parked on the scene's edge to the script, or to
    # the instanced scene whose root script answers. Popped so the list never
    # reaches graph.json; the per-file cache was written before this pass and
    # keeps it.
    root_script_of = {
        edge.get("source"): edge.get("target") for edge in all_edges
        if edge.get("context") == "root_script" and edge.get("relation") == "imports_from"
    }
    for edge in list(all_edges):
        methods = edge.pop("_godot_connections", None)
        target = edge.get("target")
        script = file_source_by_id.get(root_script_of.get(target, target))
        for method in methods or []:
            candidates = functions.get((script, method), []) if script else []
            if len(candidates) == 1:
                add_call(edge["source"], candidates[0], edge.get("source_file", ""),
                         edge.get("source_location", ""), "EXTRACTED", "signal_connection")

    for result in per_file:
        for call in result.get("raw_calls", []):
            if call.get("language") != "gdscript":
                continue
            scripts = scripts_by_name.get(str(call.get("receiver", "")), set())
            if len(scripts) != 1:
                continue
            candidates = functions.get((next(iter(scripts)), str(call.get("callee", ""))), [])
            if len(candidates) == 1:
                add_call(call.get("caller_nid"), candidates[0], call.get("source_file", ""),
                         call.get("source_location", ""), "INFERRED", "named_receiver_call")
