"""Godot extractors: GDScript (.gd, optional tree-sitter-gdscript dependency)."""
from __future__ import annotations

from pathlib import Path

from graphify.extractors.base import _file_stem, _make_id, _read_source_bytes, _read_text


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

    def ref_stub(name: str) -> str:
        """Sourceless stub for a class defined elsewhere (another script's
        `class_name`, or an engine class). The corpus-level rewire collapses it
        onto the unique real definition; engine names dangle. A sourced stub
        would bake this file's path into the id and block the rewire."""
        nid = _make_id(name)
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": name,
                "file_type": "code",
                "source_file": "",
                "source_location": "",
                "origin_file": str_path,
            })
        return nid

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
    bodies: list[tuple[str, str | None, object]] = []

    def scoped_id(scope: str | None, name: str) -> str:
        return _make_id(stem, scope, name) if scope else _make_id(stem, name)

    def name_of(node) -> str | None:
        name_node = node.child_by_field_name("name")
        return text(name_node) if name_node is not None else None

    def record_extends(ext, owner_nid: str) -> None:
        base = ext.named_children[0] if ext.named_children else None
        if base is not None and base.type == "type":
            pending_extends.append((owner_nid, text(base), line_of(ext)))

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

    for owner_nid, base, line in pending_extends:
        add_edge(owner_nid, classes.get(base) or ref_stub(base), "inherits", line)

    def lookup(table: dict[str | None, dict[str, str]], scope: str | None, name: str) -> str | None:
        return table.get(scope, {}).get(name) or table.get(None, {}).get(name)

    def first_arg(call_node):
        args = call_node.child_by_field_name("arguments")
        return args.named_children[0] if args is not None and args.named_children else None

    def string_value(node) -> str:
        return text(node).lstrip("&^").strip("\"'")

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

    for caller, scope, body in bodies:
        stack = [body]
        while stack:
            node = stack.pop()
            if node.type == "call":
                handle_call(node, caller, scope)
            elif node.type == "attribute":
                handle_attribute(node, caller, scope)
            stack.extend(node.children)

    return {"nodes": nodes, "edges": edges, "raw_calls": []}
