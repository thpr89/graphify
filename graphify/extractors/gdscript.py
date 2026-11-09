"""Godot extractors: GDScript (.gd, optional tree-sitter-gdscript dependency)."""
from __future__ import annotations

from pathlib import Path

from graphify.extractors.base import _make_id, _read_source_bytes


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
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()

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

    file_nid = _make_id(str_path)
    add_node(file_nid, path.name, 1)
    del root  # walked from Task 2 on

    return {"nodes": nodes, "edges": edges, "raw_calls": []}
