"""A forgiving mini-DOM over ``html.parser`` — stdlib only.

Two of our sources publish the numbers we need only as HTML: the ALESC e-Legis
portal (which has no API at all) and the Câmara's *relatório de presença em
plenário* (which the Dados Abertos API does not expose). Both are small,
machine-generated documents, so this project reads them with the standard library
instead of taking on ``selectolax``/``beautifulsoup4`` — the dependency surface is
a feature, not an accident.

:class:`Node` + :func:`parse_html` build the tree (stray end tags are ignored, void
elements are handled, ``<script>``/``<style>`` bodies are dropped); ``find``/
``find_all``/``text_of`` query it. Prefer selecting on **CSS classes** over label
text wherever the markup offers one: a visible label is the part most likely to be
reworded upstream.

This module was extracted from ``ingestion.alesc.parsing`` when the Câmara report
parser needed the same tree; that module still re-exports these names.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from html.parser import HTMLParser

_VOID = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)
_OPAQUE = frozenset({"script", "style"})
_WS = re.compile(r"\s+")


class Node:
    """One element in the mini-DOM. ``children`` mixes :class:`Node` and ``str``."""

    __slots__ = ("attrs", "children", "parent", "tag")

    def __init__(self, tag: str, attrs: dict[str, str] | None = None, parent: Node | None = None):
        self.tag = tag
        self.attrs: dict[str, str] = attrs or {}
        self.children: list[Node | str] = []
        self.parent = parent

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Node {self.tag} {self.attrs}>"

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.attrs.get(name, default)

    @property
    def classes(self) -> frozenset[str]:
        return frozenset((self.attrs.get("class") or "").split())


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self._stack: list[Node] = [self.root]

    def _append(self, tag: str, attrs: list[tuple[str, str | None]]) -> Node:
        node = Node(tag, {k: (v or "") for k, v in attrs}, self._stack[-1])
        self._stack[-1].children.append(node)
        return node

    def handle_starttag(self, tag: str, attrs) -> None:
        node = self._append(tag, attrs)
        if tag not in _VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self._append(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        # Pop to the nearest matching open tag; a stray close tag is ignored rather
        # than corrupting the stack (both portals emit sloppy machine markup).
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if self._stack[-1].tag in _OPAQUE:
            return
        self._stack[-1].children.append(data)


def parse_html(markup: str) -> Node:
    """Parse `markup` into a forgiving mini-DOM rooted at a synthetic ``#document``."""
    builder = _TreeBuilder()
    builder.feed(markup or "")
    builder.close()
    return builder.root


def iter_elements(root: Node) -> Iterator[Node]:
    """Depth-first walk over element nodes below (not including) `root`."""
    for child in root.children:
        if isinstance(child, Node):
            yield child
            yield from iter_elements(child)


def find_all(
    root: Node,
    tag: str | None = None,
    *,
    cls: str | frozenset[str] | set[str] | None = None,
    attr: str | None = None,
) -> list[Node]:
    """Elements matching tag / required class(es) / presence of an attribute."""
    wanted = frozenset([cls]) if isinstance(cls, str) else (frozenset(cls) if cls else None)
    out = []
    for node in iter_elements(root):
        if tag is not None and node.tag != tag:
            continue
        if wanted is not None and not wanted <= node.classes:
            continue
        if attr is not None and attr not in node.attrs:
            continue
        out.append(node)
    return out


def find(root: Node, tag: str | None = None, **kw) -> Node | None:
    found = find_all(root, tag, **kw)
    return found[0] if found else None


def text_of(node: Node | None, *, exclude: Node | None = None) -> str:
    """Whitespace-collapsed text of a subtree, optionally skipping one child subtree."""
    if node is None:
        return ""
    parts: list[str] = []

    def walk(n: Node) -> None:
        for child in n.children:
            if isinstance(child, str):
                parts.append(child)
            elif child is not exclude and child.tag not in _OPAQUE:
                walk(child)

    walk(node)
    return _WS.sub(" ", "".join(parts)).strip()
