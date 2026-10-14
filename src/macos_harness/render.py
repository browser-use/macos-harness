"""The printed form of a state: header, one line per element, footer.

Line: <TAB x depth><id> <role> (<states>) <fields>. Each of the three parts is left out
when it is empty. Nothing is quoted or escaped, and a newline in a value is printed raw.
"""

from __future__ import annotations

from typing import Any

from .model import Capture, Line

Node = Any  # tree.Node. This module only reads its fields.

TEXT_CAP = 512  # text that cannot be edited is cut here, with no mark
LONG_FIELD = 100  # a field longer than this moves to the end of its line
NOTE = (
    "Note: the user has selected something on screen. When they ask about what they "
    "are looking at, they may mean the selection, or something else that is visible."
)
# Roles that print a fixed word, or none, whatever the app calls them.
ROLE_WORDS = {
    "AXGroup": "container",
    "AXRow": "row",
    "AXLink": "link",  # also where the app says "hyperlink" (a link in AppKit text)
    "AXMenuItem": "",
    "AXMenuBarItem": "",
    "AXRadioGroup": "",
}
# The word for a role the app gives no description for.
UNDESCRIBED = {"AXMenu": "menu", "AXMenuBar": "menu bar", "AXGenericElement": "element"}
HIDDEN_ID_PREFIXES = ("_", "NS")
URL_PREFIXES = ("https://", "http://", "www.")
URL_BUDGET = 4000  # characters of URL one tree may print
# The step-down of the per-URL limit is an estimate.
# (most URLs in the tree, the longest one URL may print)
URL_LIMITS = ((110, None), (250, 30), (None, 20))
UNCUT_URLS = ("file:",)  # a path on this Mac prints whole
ELLIPSIS = "…"


def cut(text: str) -> str:
    return text[:TEXT_CAP]


def visible_id(identifier: str) -> str:
    """An identifier the toolkit made up is not printed and not used to find again."""
    return "" if identifier.startswith(HIDDEN_ID_PREFIXES) else identifier


def header(title: str, app_name: str) -> str:
    return f'Window: "{title}", App: {app_name}.'


def _role(node: Node) -> str:
    if node.role in ROLE_WORDS:
        return ROLE_WORDS[node.role]
    return node.role_description or UNDESCRIBED.get(node.role, "unknown")


def _states(node: Node) -> str:
    tokens = [
        "selected" if node.selected else "selectable" if node.selectable else "",
        "" if node.enabled else "disabled",
        {True: "expanded", False: "collapsed", None: ""}[node.expanded],
        "settable" if node.settable else "",
        node.value_type if node.settable and node.value_type != "string" else "",
    ]
    return ", ".join(token for token in tokens if token)


def short_url(url: str) -> str:
    for prefix in URL_PREFIXES:
        url = url.removeprefix(prefix)
    return url


def _is_link(node: Node) -> bool:
    return node.role == "AXLink" and bool(node.url)  # its value is its target


def _fields(node: Node) -> str:
    link = _is_link(node)
    said = (
        node.title,
        node.description,
        node.value,
    )  # a help that repeats one is left out
    labelled = [
        ("Description", "" if node.description == node.title else node.description),
        ("Rows", node.rows),
        ("Columns", node.columns),
        ("URL", "" if link else short_url(node.url)),
        ("Help", "" if node.help in said else node.help),
        ("Value", short_url(node.url) if link else node.value),
        (
            "Details",
            "" if node.details == node.value else node.details,
        ),  # "12 pt" twice
        ("Placeholder", node.placeholder),
        ("ID", node.identifier),
    ]
    present = [(label, text) for label, text in labelled if text]
    actions = ", ".join(node.actions)
    if node.title:
        chunks = [node.title] + [f"{label}: {text}" for label, text in present]
    elif len(present) == 1 and not actions:  # one text and nothing else prints bare
        chunks = [present[0][1]]
    else:
        chunks = [f"{label}: {text}" for label, text in present]
    sizes = ([node.title] if node.title else []) + [text for _, text in present]
    if actions:
        chunks.append(f"Secondary Actions: {actions}")
        sizes.append(actions)
    long = [len(text) > LONG_FIELD or "\n" in text for text in sizes]
    ordered = [c for c, is_long in zip(chunks, long, strict=True) if not is_long]
    ordered += [c for c, is_long in zip(chunks, long, strict=True) if is_long]
    return ", ".join(ordered)


def lines(root: Node, ctx: Any, depth: int = 0) -> list[Line]:
    """The element lines of one subtree, in depth-first order, without numbers."""
    parts = [_role(root), f"({s})" if (s := _states(root)) else "", _fields(root)]
    subset = root.subset
    line = Line(
        depth=depth,
        text=" ".join(part for part in parts if part),
        element=root.element,
        role=root.role,
        detail=f"(showing {subset[0]}-{subset[1]} of {subset[2]} items)"
        if subset
        else "",
        name=root.title or root.description,
        value=short_url(root.url) if _is_link(root) else root.value,
        match=(
            root.role,
            root.subrole,
            root.title,
            root.description,
            root.identifier,
            root.help,
            root.placeholder,
            root.url,
        ),
        settable=root.settable,
        actions=dict(root.actions),
    )
    return [line] + [x for child in root.children for x in lines(child, ctx, depth + 1)]


def shorten_urls(lines: list[Line]) -> None:
    """Cut the URLs of one tree against its budget, in place.

    A crowded tree prints each URL without query and fragment, cut to the limit its
    URL count allows. Once the budget of the whole tree is spent, the rest print only
    an ellipsis. A path on this Mac is never cut and costs nothing.
    """
    urls = [(line, short_url(line.match[7])) for line in lines if line.match[7:]]
    urls = [(line, url) for line, url in urls if url]
    limit = next(top for most, top in URL_LIMITS if most is None or len(urls) <= most)
    urls = [(line, url) for line, url in urls if not url.startswith(UNCUT_URLS)]
    spent = 0
    for line, full in urls:
        short = full
        if limit:
            short = full.split("?")[0].split("#")[0]
            if len(short) > limit:
                short = short[: limit - 1] + ELLIPSIS
        if spent + len(short) > URL_BUDGET:
            short = ELLIPSIS
        spent += len(short)
        line.text = line.text.replace(full, short, 1)
        line.value = line.value.replace(full, short, 1)


def _numbered(line: Line, numbered: bool) -> str:
    text = f"{line.text} {line.detail}" if line.detail else line.text
    return f"{line.id} {text}" if numbered else text


def footer(capture: Capture, numbered: bool) -> str:
    """At most one block: selected text, selected elements, or the focused element."""
    if capture.selected_text:
        return f"Selected text: ```\n{capture.selected_text}\n```\n\n{NOTE}"
    if capture.selected:
        rows = "\n".join("\t" + _numbered(line, numbered) for line in capture.selected)
        return f"Selected:\n{rows}\n\n{NOTE}"
    if capture.focused is not None:
        return f"The focused UI element is {_numbered(capture.focused, numbered)}"
    return ""
