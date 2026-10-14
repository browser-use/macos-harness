"""The twelve passes that turn the tree as read into the tree as printed.

One pipeline for every app. What differs per app sits inside a pass: the calendar
bundle id, the table row, the text area that holds source code.

A rule looks at one node after its children were done and answers with None (the node
stays, changed or not), an empty list (removed) or a list of nodes that take its place.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from typing import Any

Node = Any  # tree.Node. This module only reads and writes its fields.
Rule = Callable[[Node, Any], "list[Node] | None"]

GROUP, TEXT, MENU_ITEM = "AXGroup", "AXStaticText", "AXMenuItem"
SPLIT_GROUP = "AXSplitGroup"
LINK, WEB_AREA = "AXLink", "AXWebArea"
ROLELESS = ("AXRadioGroup", MENU_ITEM, "AXMenuBarItem")  # roles that print no word
CALENDAR = "com.apple.iCal"
# Actions that are the click itself, or that every element of a toolkit carries.
DROPPED_ACTIONS = {
    "AXPress",
    "AXShowMenu",
    "AXScrollToVisible",
    "AXShowDefaultUI",
    "AXShowAlternateUI",
}
EVERYWHERE_ACTIONS = DROPPED_ACTIONS - {"AXPress"}  # having these offers nothing
DROPPED_LABELS = {
    "Add comment",
    "Remove unused rows",
    "Remove unused columns",
    "Remove unused rows and columns",
}
# More actions that one role never prints.
DROPPED_BY_ROLE = {
    MENU_ITEM: {"AXCancel", "AXPick"},
    "AXTextField": {"AXConfirm"},
    "AXTextArea": {"AXConfirm"},
}
STEP_ACTIONS = {"AXIncrement", "AXDecrement"}  # not printed where the value can be set
SCROLL_AXIS = {
    "AXScrollUpByPage": "v",
    "AXScrollDownByPage": "v",
    "AXScrollLeftByPage": "h",
    "AXScrollRightByPage": "h",
}
FLATTENED = ("title", "description", "value")  # what a selectable ancestor takes over


def _described(node: Node) -> bool:
    texts = (node.title, node.description, node.value, node.help, node.placeholder)
    return any(text.strip() for text in texts)  # white space alone says nothing


def _interactive(node: Node) -> bool:
    offers = any(name not in EVERYWHERE_ACTIONS for name in node.acts)
    return bool(
        offers
        or node.selectable
        or node.settable
        or node.focusable
        or node.selected
        or node.focused
    )


def _below(node: Node) -> Iterator[Node]:
    for child in node.children:
        yield child
        yield from _below(child)


def _is_text(node: Node) -> bool:
    return node.role == TEXT and bool(node.value) and not node.children


def filter_actions(node: Node, ctx: Any) -> None:
    """Pass 1: drop the click itself, the actions every element has, four fixed names."""
    dropped = DROPPED_ACTIONS | DROPPED_BY_ROLE.get(node.role, set())
    if node.settable:
        dropped = dropped | STEP_ACTIONS
    if node.role == "AXScrollArea":  # a page scroll needs a scroll bar on that axis
        dropped = dropped | {
            name for name, axis in SCROLL_AXIS.items() if axis not in node.scroll_axes
        }
    node.actions = {
        label: name
        for label, name in node.actions.items()
        if name not in dropped and label not in DROPPED_LABELS
    }


def associate_titles(root: Node, ctx: Any) -> Node:
    """Pass 2: a text that another element names as its title becomes that title.

    A title that sits inside the element it names stays where it is (a window's title
    text, the text of a table cell). So does a text that two elements name: in Pages'
    Format sidebar "Spacing" titles a disclosure triangle and a pop-up button, and
    the text is printed and neither title. So does a text that carries a help
    of its own ("Drop Cap" there).
    """
    nodes = list(root.walk())
    named = [id(node.title_node) for node in nodes if node.title_node is not None]
    taken = set()
    for node in nodes:
        text = node.title_node
        if text is None or text.role != TEXT or node.title or text in set(_below(node)):
            continue
        if (
            named.count(id(text)) > 1 or text.help.strip()
        ):  # a label with a help of its own
            continue
        node.title = text.value or text.title
        taken.add(id(text))
    for node in root.walk():
        node.children = [child for child in node.children if id(child) not in taken]
    return root


def flatten_into_selectable(node: Node, ctx: Any) -> None:
    """Pass 3: a selectable element takes over the text below it and becomes a leaf.

    Not a table row: its cells stay. Not a table (anything with rows below it: a
    Numbers table can be selected, and its rows and cells are printed). Not
    when something below can itself be selected or set.
    """
    inside = list(_below(node))
    if not (node.selectable and inside) or node.subrole == "AXTableRow":
        return
    if any(below.role == "AXRow" for below in inside):
        return
    if any(below.selectable or below.settable for below in inside):
        return
    for name in FLATTENED:
        parts = [getattr(n, name).strip() for n in (node, *inside)]
        setattr(node, name, "\n".join(part for part in parts if part))
    node.children = []


def prune_non_descriptive(node: Node, ctx: Any) -> list | None:
    """Pass 4: a leaf that says nothing and offers nothing goes. An empty group goes
    unless it can be pressed, set or selected: one that could only take focus (Chrome's
    window is full of them) or only be moved in a toolbar (Numbers' spacers) is not
    printed. The image of a table cell stays (a Finder file's icon):
    it is printed bare."""
    if node.children or node.subset or _described(node):
        return None
    if node.role == GROUP:
        held = node.selectable or node.settable or node.selected or node.focused
        return None if held or "AXPress" in node.acts else []
    kept = _interactive(node) or (node.role == "AXImage" and node.in_cell)
    return None if kept else []


def prune_empty_disabled(node: Node, ctx: Any) -> list | None:
    """Pass 5: a disabled menu item with no text (a separator) goes."""
    separator = node.role == MENU_ITEM and not node.enabled
    return [] if separator and not (node.children or _described(node)) else None


def _plain_split(node: Node) -> bool:
    """A split group that says nothing and has no name of its own. Measured: Finder's
    inner one gives way to its panes; Font Book's, which has an identifier, stays."""
    return node.role == SPLIT_GROUP and not (_described(node) or node.identifier)


def _plain_group(node: Node) -> bool:
    """A group that says nothing itself. One that can be pressed is not plain, unless
    pressing only opens it: a collapsed <details> gives way to its summary (measured)."""
    said = node.title or node.description or node.value
    pressed = "AXPress" in node.acts and node.expanded is not False
    held = pressed or node.focused or node.selected
    return node.role == GROUP and not (said or held)


def merge_single_item_groups(node: Node, ctx: Any) -> list | None:
    """Pass 6: a plain group around exactly one element gives way to that element."""
    return node.children if _plain_group(node) and len(node.children) == 1 else None


def flatten_redundant_hierarchy(root: Node, ctx: Any) -> Node:
    """Pass 4: a plain group inside a plain group gives way to its children, and so does
    a plain split group inside a plain split group. From the top down, one level at a
    time: a group that moved up is not opened again by its new parent. One with no
    children stays for the pruning.

    Measured on Chrome's window (13 Oct), where groups nest five deep around empty
    ones: `container` is wanted around the page and around the tab strip,
    and none around the toolbar. Bottom up, all three went.
    """

    def visit(node: Node) -> None:
        for plain in (_plain_group, _plain_split):  # the same kind inside the same kind
            if plain(node):
                node.children = [
                    inner
                    for child in node.children
                    for inner in (
                        child.children if plain(child) and child.children else [child]
                    )
                ]
        for child in node.children:
            visit(child)

    visit(root)
    return root


def drop_silent(node: Node, ctx: Any) -> list | None:
    """An element that would print neither a role nor a text (a radio group with no
    name) gives way to its children. Measured on web content.
    """
    silent = node.role in ROLELESS and not (
        _described(node) or node.identifier or node.actions
    )
    return node.children if silent and node.children else None


def flatten_repetitive_text(node: Node, ctx: Any) -> None:
    """Pass 8: of neighbouring texts that say the same, one stays. A text loses the
    texts below it that only say its own words (a web page's label)."""
    below = node.children
    if node.role == TEXT and all(_is_text(c) and c.value == node.value for c in below):
        node.children = []
    kept: list = []
    for child in node.children:
        same = kept and _is_text(child) and _is_text(kept[-1])
        if not (same and child.value == kept[-1].value):
            kept.append(child)
    node.children = kept


def flatten_links(node: Node, ctx: Any) -> None:
    """Pass 9: a link is one line. What is below it only repeats its label.

    A link with no label of its own takes the text below it. Writing links into the
    surrounding text as [label](url) is off.
    """
    if node.role != LINK or not node.children:
        return
    if not (node.title or node.description):
        node.title = " ".join(n.value or n.title for n in _below(node) if _is_text(n))
    node.children = []


def merge_text_siblings(node: Node, ctx: Any) -> None:
    """Pass 10: neighbouring texts become one line, joined with one space.

    The line keeps the first text's element and its other fields. The blocks of text
    right below a web page stay apart.
    """
    kept: list = []
    for child in node.children:
        if kept and _is_text(child) and _is_text(kept[-1]) and node.role != WEB_AREA:
            kept[-1].value += " " + child.value
        else:
            kept.append(child)
    node.children = kept


def calendar_events(node: Node, ctx: Any) -> None:
    """Pass 11: in the calendar app an event text loses its one text child."""
    event = node.role == TEXT and node.role_description == "Event"
    one_text = len(node.children) == 1 and node.children[0].role == TEXT
    if event and one_text and ctx.bundle_id == CALENDAR:
        node.children = []


def rich_text(node: Node, ctx: Any) -> None:
    """Pass 12: the value of a rich text area is printed as Markdown.

    A text area that holds source code has no runs and stays plain. So does any text
    the Markdown writer fails on.
    """
    if node.runs and ctx.markdown is not None:
        with contextlib.suppress(Exception):  # any failure keeps the plain value
            node.value = ctx.markdown(node.runs)[0]


def _each(rule: Rule) -> Callable[[Node, Any], Node]:
    """Run a rule on every node, children first. The root itself is never replaced."""

    def visit(node: Node, ctx: Any) -> list:
        node.children = [out for child in node.children for out in visit(child, ctx)]
        result = rule(node, ctx)
        return [node] if result is None else result

    def whole(root: Node, ctx: Any) -> Node:
        visit(root, ctx)
        return root

    whole.__name__ = rule.__name__
    return whole


PASSES = (
    _each(filter_actions),
    associate_titles,
    _each(flatten_into_selectable),
    flatten_redundant_hierarchy,
    _each(merge_single_item_groups),
    _each(prune_non_descriptive),
    _each(prune_empty_disabled),
    _each(drop_silent),
    _each(flatten_repetitive_text),
    _each(flatten_links),
    _each(merge_text_siblings),
    _each(calendar_events),
    _each(rich_text),
)


def run(root: Node, ctx: Any) -> Node:
    for node in root.walk():  # for pass 4, which sees a node without its parent
        for child in node.children if node.role == "AXCell" else ():
            child.in_cell = True
    for one_pass in PASSES:
        root = one_pass(root, ctx)
    return root
