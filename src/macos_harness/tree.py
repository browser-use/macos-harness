"""One state read: walk a window, prune it, print it.

read() returns a Capture: the lines of the tree before they have numbers, the focused
element, the selection, and the facts the settle step and the URL policy need.
"""

from __future__ import annotations

import contextlib
import dataclasses
import math
import re
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from . import ax, errors, prune, render, richtext
from .model import AppKind, AppTarget, Capture, Line, Rect, WindowRef

MAX_CHILDREN = 100  # above this only the visible children are read
MAX_FILTERED = 499  # above this, with no visible-children attribute, none are read
NEIGHBOURS = 100  # the position filter keeps at least this many around what is seen
# A guard for the walk. Real trees stop near depth 50.
MAX_DEPTH = 100
SPINNER_CLASS = "macos-mac-parity-spinner"
SOURCE_CODE = "AXTextualContextSourceCode"  # a text area that prints as it is
# A file given by its id, not its path (a Finder file name). It prints as its
# path URL, and as this when the id leads nowhere.
FILE_REFERENCE = "file:///.file/id="
UNRESOLVED_FILE = "com-apple-unresolvable-file-reference-url:"
# A web view hands out its page only this long after it was first asked (measured 38 ms).
FIRST_ASK_WAIT = 0.05
FIRST_ASK_LIMIT = (
    3000  # elements the first ask visits at most (2,000 take 130 to 200 ms)
)

# One batched read per element asks for exactly these.
ATTRIBUTES = (
    "AXRole",
    "AXSubrole",
    "AXRoleDescription",
    "AXTitle",
    "AXDescription",
    "AXValue",
    "AXValueDescription",
    "AXPlaceholderValue",
    "AXHelp",
    "AXIdentifier",
    "AXURL",
    "AXDocument",
    "AXEnabled",
    "AXSelected",
    "AXFocused",
    "AXExpanded",
    "AXDisclosing",
    "AXMenuItemMarkChar",
    "AXTitleUIElement",
    "AXVerticalScrollBar",
    "AXHorizontalScrollBar",
    "AXTextualContext",
    "AXRowIndexRange",
    "AXColumnIndexRange",
    "AXElementBusy",
    "AXLoaded",
    "AXDOMClassList",
    "AXChildren",
)
# The name a secondary action prints under. Any other action prints the app's own
# description of it, and a custom action prints its name.
ACTION_LABELS = {
    "AXPress": "Press",
    "AXShowMenu": "Show Menu",
    "AXConfirm": "Confirm",
    "AXRaise": "Raise",
    "AXCancel": "Cancel",
    "AXPick": "Pick",
    "AXDelete": "Delete",
    "AXIncrement": "Increment",
    "AXDecrement": "Decrement",
    "AXExpand": "Expand",
    "AXCollapse": "Collapse",
    "AXScrollUpByPage": "Scroll Up",
    "AXScrollDownByPage": "Scroll Down",
    "AXScrollLeftByPage": "Scroll Left",
    "AXScrollRightByPage": "Scroll Right",
    "AXScrollToVisible": "Scroll to Visible",
    "AXShowAlternateUI": "Show Alternate UI",
    "AXShowDefaultUI": "Show Default UI",
}
VALUE_ROLES = ("AXTextField", "AXTextArea", "AXComboBox", "AXSearchField", "AXSlider")
# (role, subrole) of the switches whose 0 and 1 print as off and on
SWITCHES = (("AXCheckBox", "AXToggle"), ("AXCheckBox", "AXSwitch"))
VISIBLE_ATTRIBUTES = ("AXVisibleRows", "AXVisibleChildren")
SELECTION_ATTRIBUTES = ("AXSelectedRows", "AXSelectedChildren")
TEXT_FIELDS = (
    "title",
    "description",
    "value",
    "details",
    "placeholder",
    "help",
    "identifier",
    "url",
    "rows",
    "columns",
)
# What the attributed string of a text area says, and the style it becomes.
RANGE = re.compile(r"location:(\d+) length:(\d+)")  # how a range value prints itself
RUN_STYLES = {
    "AXUnderline": "underline",
    "AXStrikethrough": "strikethrough",
    "AXLink": "link",
    "AXHeadingLevel": "heading",
    "AXBlockQuoteLevel": "quote",
}


@dataclass(eq=False)
class Node:
    """One element as read, before pruning. Text fields are "" when the app has none."""

    element: Any = None
    role: str = ""
    subrole: str = ""
    role_description: str = ""
    title: str = ""
    description: str = ""
    value: str = ""
    value_type: str = ""  # boolean, integer, float, string, date or unknown
    details: str = ""
    rows: str = ""  # "0-1": the rows a cell covers, when more than one
    columns: str = ""
    placeholder: str = ""
    help: str = ""
    identifier: str = ""
    url: str = ""
    enabled: bool = True
    selected: bool = False
    selectable: bool = False
    focused: bool = False
    focusable: bool = False
    expanded: bool | None = None  # None: the element cannot disclose anything
    settable: bool = False
    acts: tuple[str, ...] = ()  # every action name the app offers
    actions: dict[str, str] = field(default_factory=dict)  # printed label -> name
    scroll_axes: str = ""  # "v", "h" or "vh": the scroll bars a scroll area has
    subset: tuple[int, int, int] | None = None  # children a to b of n were read
    title_element: Any = None  # the element the app names as this one's title
    title_node: Node | None = None  # that element, when it is in this tree
    runs: list | None = None  # the styled runs of a rich text area
    loading: bool = False
    in_cell: bool = False  # set by the pruning: its parent is a table cell
    children: list[Node] = field(default_factory=list)

    def walk(self) -> Iterator[Node]:
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass
class Context:
    """What the passes and the printer may know about the app."""

    kind: AppKind = field(default_factory=AppKind)
    bundle_id: str = ""
    window_frame: Rect | None = None
    light: bool = False  # menu bar titles: no actions and no children are read
    # Elements already read. A web table lists each cell again under its columns and
    # its header: the second time an element is left out.
    seen: set = field(default_factory=set)
    # richtext.markdown. Handed over here so that prune.py imports nothing.
    markdown: Callable | None = None
    # The attributes of the whole window from one hierarchy copy, by element hash:
    # [(element, values in ATTRIBUTES order)]. Empty: every element is read on its own.
    prefetched: dict = field(default_factory=dict)


def _text(value: Any, cap: bool = True) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):  # rare: a URL or an attributed string
        kind = type(value).__name__
        if "URL" in kind:
            value = value.absoluteString()
        elif "AttributedString" in kind:
            value = value.string()
    return render.cut(str(value)) if cap else str(value)


def _file_path(reference: Any) -> str:
    """The path URL of a file reference: the URL object the app gave, or its text."""
    try:
        if isinstance(reference, str):
            from Foundation import NSURL

            reference = NSURL.URLWithString_(reference)
        path = reference.filePathURL()
        return str(path.absoluteString()) if path is not None else UNRESOLVED_FILE
    except Exception:  # noqa: BLE001 - no Foundation, or not a URL
        return UNRESOLVED_FILE


def _span(value: Any) -> str:
    """The rows or columns a cell covers, first-last. "" for one, the usual case."""
    found = RANGE.search(str(value)) if value is not None else None
    first, length = (int(found[1]), int(found[2])) if found else (0, 0)
    return f"{first}-{first + length - 1}" if length > 1 else ""


def _number(value: float) -> str:
    """Whole values print without decimals, fractions with at most 16 digits."""
    if not isinstance(value, float) or not math.isfinite(value):
        return str(value)
    return str(int(value)) if value == int(value) else format(value, ".16g")


def _value(raw: Any, settable: bool) -> tuple[str, str]:
    """The printed value and its type name."""
    if isinstance(raw, bool):
        return ("on" if raw else "off"), "boolean"
    if isinstance(raw, (int, float)):
        return _number(raw), "integer" if isinstance(raw, int) else "float"
    kind = type(raw).__name__
    if isinstance(raw, str) or "AttributedString" in kind:
        return _text(raw, cap=not settable), "string"  # editable text is never cut
    return ("", "unknown") if "Date" not in kind else (str(raw), "date")


def _label(name: str, described: Any) -> str:
    if name.startswith("Name:"):  # a custom action carries its name in its identifier
        return name[5:].split("\nTarget:")[0]
    return ACTION_LABELS.get(name) or described[name] or name


def _runs(el: Any) -> list | None:
    """The styled runs of a text area, for the Markdown pass. None when unreadable."""
    length = ax.get(el, "AXNumberOfCharacters")
    if not isinstance(length, int) or isinstance(length, bool) or length <= 0:
        return None
    try:
        import ApplicationServices as api

        # The range must be an accessibility value: a plain range gets no answer.
        whole = api.AXValueCreate(api.kAXValueCFRangeType, (0, length))
        text = ax.param(el, "AXAttributedStringForRange", whole)
        runs, at = [], 0
        while text is not None and at < text.length():
            attributes, span = text.attributesAtIndex_effectiveRange_(at, None)
            runs.append((str(text.string())[at : at + span.length], _style(attributes)))
            at += span.length
        return runs or None
    except Exception:  # noqa: BLE001 any failure keeps the plain value
        return None


def _styled(role: str, value: str, context: Any) -> bool:
    """A text area with text in it prints as Markdown, unless it holds source code."""
    return role == "AXTextArea" and bool(value) and context != SOURCE_CODE


def printed_text(el: Any) -> tuple[str, list[int] | None]:
    """The value a state read prints for a text element, and where each printed
    character sits in the element's plain text. None: the two texts are the same."""
    role, value, context = ax.get_many(el, ("AXRole", "AXValue", "AXTextualContext"))
    plain = _text(value, cap=False)
    runs = _runs(el) if _styled(_text(role), plain, context) else None
    try:
        return richtext.markdown(runs) if runs else (plain, None)
    except Exception:  # noqa: BLE001 any failure keeps the plain value, as a read does
        return plain, None


def _style(attributes: Any) -> dict:
    style = {
        RUN_STYLES[k]: v for k, v in dict(attributes or {}).items() if k in RUN_STYLES
    }
    font = dict(attributes or {}).get("AXFont") or {}
    name = f"{font.get('AXFontName', '')} {font.get('AXVisibleName', '')}".lower()
    style.update({k: True for k in ("bold", "italic") if k in name})
    if "oblique" in name:
        style["italic"] = True
    if "link" in style:  # a URL, or the link element of the text: its target
        link = style["link"]
        kind = type(link).__name__
        if not isinstance(link, str) and "URL" not in kind:
            link = ax.get(link, "AXURL")
        style["link"] = render.short_url(_text(link, cap=False))
    return style


def _node(el: Any, ctx: Context, depth: int = 0) -> Node | None:
    """Read one element and, below it, the children the rules keep."""
    try:
        return _read(el, ctx, depth)
    except errors.HarnessError:  # an element that went away is left out
        return None


def _prefetch(root: Any) -> dict:
    """One copy of the whole subtree when the system gives it, else nothing."""
    out: dict = {}
    for el, values in ax.subtree(root, ATTRIBUTES, MAX_CHILDREN) or ():
        out.setdefault(hash(el), []).append((el, values))
    return out


def _attributes(el: Any, ctx: Context) -> list:
    """The element's attributes: from the copy when it has them, else asked now."""
    for other, values in ctx.prefetched.get(hash(el), ()):
        if ax.same(el, other):
            return values
    return ax.get_many(el, ATTRIBUTES)


def _can_set(el: Any, name: str) -> bool:
    """The element says the attribute can be set. Not whether it lists it: Pages' pages
    and text areas can be selected without listing `AXSelected`, and they
    print as `selectable` (13 Oct). A pop-up button of a Chromium page lists
    `AXExpanded` and nobody can set it: no `collapsed` there."""
    return ax.settable(el, name)


def _takes_a_value(role: str, value: Any) -> bool:
    """Chromium says every element's value is listed and can be set, its toolbar and
    its buttons too. Measured on Chrome and Electron (13 Oct): what helps is
    `settable` there for text fields and for an on/off or sliding value (a tab), and
    not for a group, a button, a pop-up button ("Red") or a checkbox (0)."""
    return role in VALUE_ROLES or isinstance(value, (bool, float))


def _selected(role: str, raw: dict) -> bool:
    """What the app calls selected. For a menu item that is the one the menu points at
    (a pop-up's chosen option, the first item of a context menu), never its check mark:
    a checked `Text Replacement` prints plain (Electron, 12 Oct). A menu
    bar title never prints it."""
    return role != "AXMenuBarItem" and bool(raw["AXSelected"])


def _read(el: Any, ctx: Context, depth: int) -> Node:
    raw = dict(zip(ATTRIBUTES, _attributes(el, ctx), strict=True))
    role = _text(raw["AXRole"])
    menu_item = role in ("AXMenuItem", "AXMenuBarItem")
    in_menu = menu_item or role in ("AXMenu", "AXMenuBar")  # these print no disclosure
    settable = raw["AXValue"] is not None and _can_set(el, "AXValue")
    chromium = ctx.kind.electron or ctx.kind.chromium
    if settable and chromium and not _takes_a_value(role, raw["AXValue"]):
        settable = False
    value, value_type = _value(raw["AXValue"], settable)
    if (role, _text(raw["AXSubrole"])) in SWITCHES and value in ("0", "1"):
        value, value_type = ("off", "on")[int(value)], "boolean"
    url = _text(raw["AXURL"] or raw["AXDocument"], cap=False)
    if role == "AXImage":  # an image's source is never printed, measured
        url = ""
    if url.startswith(FILE_REFERENCE):  # a Finder file name: printed as its path
        given = raw["AXURL"] or raw["AXDocument"]
        url = _file_path(url if isinstance(given, str) else given)
    disclosing = raw["AXDisclosing"]
    if isinstance(disclosing, bool) and not _can_set(el, "AXDisclosing"):
        disclosing = None
    expanded = disclosing
    # A disclosure triangle (a web page's <summary>) says its state in its value.
    plain_value = in_menu or role == "AXDisclosureTriangle"
    if expanded is None and isinstance(raw["AXExpanded"], bool) and not plain_value:
        # An element can answer False for an attribute it does not list, and a pop-up
        # button of a Chromium page lists one nobody can set: neither is a state.
        expanded = raw["AXExpanded"] if _can_set(el, "AXExpanded") else None
    node = Node(
        element=el,
        role=role,
        subrole=_text(raw["AXSubrole"]),
        role_description=_text(raw["AXRoleDescription"]),
        title=_text(raw["AXTitle"]),
        description=_text(raw["AXDescription"]),
        value=value,
        value_type=value_type,
        details=_text(raw["AXValueDescription"]),
        rows=_span(raw["AXRowIndexRange"]),
        columns=_span(raw["AXColumnIndexRange"]),
        placeholder=_text(raw["AXPlaceholderValue"]),
        help=_text(raw["AXHelp"]),
        identifier=render.visible_id(_text(raw["AXIdentifier"])),
        url=url,
        enabled=raw["AXEnabled"] is not False,
        selected=_selected(role, raw),
        selectable=not menu_item
        and isinstance(raw["AXSelected"], bool)
        and _can_set(el, "AXSelected"),
        focused=raw["AXFocused"] is True,
        expanded=expanded if isinstance(expanded, bool) else None,
        settable=settable,
        scroll_axes=("v" if raw["AXVerticalScrollBar"] is not None else "")
        + ("h" if raw["AXHorizontalScrollBar"] is not None else ""),
        title_element=raw["AXTitleUIElement"],
        loading=role == "AXBusyIndicator"
        or (role == "AXWebArea" and raw["AXLoaded"] is False)
        or raw["AXElementBusy"] is True
        or SPINNER_CLASS in list(raw["AXDOMClassList"] or ()),
    )
    if not ctx.light:
        described = ax.actions(el)
        node.acts = tuple(described)
        node.actions = {_label(name, described): name for name in node.acts}
        if isinstance(disclosing, bool):  # a row that discloses others has no action
            node.actions.setdefault(
                "Collapse" if disclosing else "Expand", "AXDisclosing"
            )
        if _styled(role, value, raw["AXTextualContext"]):
            node.runs = _runs(el)
        kids = list(raw["AXChildren"] or ()) if depth < MAX_DEPTH else []
        kids, node.subset = _visible(el, kids, ctx)
        kids = [kid for kid in kids if kid not in ctx.seen and not ctx.seen.add(kid)]
        node.children = [
            child for kid in kids if (child := _node(kid, ctx, depth + 1)) is not None
        ]
        said = node.title + node.description + node.value + node.help + node.placeholder
        if not (node.children or said.strip() or node.selectable or settable):
            node.focusable = ax.settable(el, "AXFocused")  # only asked for bare leaves
    return node


def _visible(
    el: Any, kids: list, ctx: Context
) -> tuple[list, tuple[int, int, int] | None]:
    """The children to read. Above 100 only what the app, or the screen, shows. The
    subset is said only when it leaves some out: a web list that shows all 200 of its
    items prints none (Electron, 12 Oct)."""
    count = len(kids)
    if count <= MAX_CHILDREN:
        return kids, None
    for name in VISIBLE_ATTRIBUTES:
        shown = list(ax.get(el, name) or ())
        if shown:
            first = next((i for i, k in enumerate(kids) if ax.same(k, shown[0])), 0)
            return shown, (first, first + len(shown), count) if len(
                shown
            ) < count else None
    if count > MAX_FILTERED:  # too many to place on the screen
        return [], (0, 0, count)
    seen = [i for i, kid in enumerate(kids) if _on_screen(ax.frame(kid), ctx)]
    low = seen[0] if seen else 0
    high = min(count, max(seen[-1] + 1 if seen else 0, low + NEIGHBOURS))
    low = min(low, max(0, high - NEIGHBOURS))
    return kids[low:high], (low, high, count)


def _on_screen(rect: Rect | None, ctx: Context) -> bool:
    clip = ctx.window_frame
    if rect is None or clip is None:
        return False
    return (
        rect[0] < clip[0] + clip[2]
        and clip[0] < rect[0] + rect[2]
        and rect[1] < clip[1] + clip[3]
        and clip[1] < rect[1] + rect[3]
    )


def _link_titles(root: Node) -> None:
    """Point every node at the node of its title element, when that is in the tree."""
    nodes = list(root.walk())
    for node in nodes:
        if node.title_element is not None:
            node.title_node = next(
                (n for n in nodes if ax.same(n.element, node.title_element)), None
            )


def _menu_bar(app_el: Any) -> Node | None:
    """The last block of a normal tree: the menu titles, without the Apple menu."""
    bar = ax.get(app_el, "AXMenuBar")
    light = Context(light=True)
    node = _node(bar, light) if bar is not None else None
    if node is None:
        return None
    titles = [_node(item, light) for item in ax.get(bar, "AXChildren") or ()]
    node.children = [t for t in titles if t is not None and t.title != "Apple"]
    return node


def _lines(roots: list[Node], ctx: Context) -> list[Line]:
    lines = [line for root in roots for line in render.lines(root, ctx)]
    render.shorten_urls(lines)
    return lines


def _alone(el: Any, ctx: Context) -> Node | None:
    """One element read by itself, with its actions filtered and no children."""
    node = _node(el, ctx, MAX_DEPTH)
    return None if node is None else prune.run(node, ctx)


def _own_line(el: Any, lines: list[Line]) -> Line | None:
    """The focused element's line in the tree. None when the tree does not print it (a
    field editor of another window: Finder while a file is renamed): the state then
    says nothing about focus or selection."""
    return next((line for line in lines if ax.same(line.element, el)), None)


def _brief_line(el: Any, ctx: Context) -> Line | None:
    """One line of the selection block: role and states. Only a menu keeps its text."""
    node = _alone(el, ctx)
    if node is None:
        return None
    if node.role != "AXMenu":
        bare = dict.fromkeys(TEXT_FIELDS, "")
        node = dataclasses.replace(node, actions={}, **bare)
    return render.lines(node, ctx)[0]


def _selected_text(el: Any) -> str | None:
    text = ax.get(el, "AXSelectedText")
    if not text:  # web content keeps its selection as a marker range
        markers = ax.get(el, "AXSelectedTextMarkerRange")
        text = markers and ax.param(el, "AXStringForTextMarkerRange", markers)
    return str(text) if text else None


def read(
    target: AppTarget, window: WindowRef, kind: AppKind, menu: Any = None
) -> Capture:
    """Read one window, or only the open menu when `menu` is given."""
    ctx = Context(
        kind=kind,
        bundle_id=target.bundle_id,
        window_frame=window.frame,
        markdown=richtext.markdown,
    )
    app_el = ax.app(target.pid)
    start = window.element if menu is None else menu
    ctx.prefetched = _prefetch(start)
    roots = [_node(start, ctx)]
    if menu is None:
        roots.append(_menu_bar(app_el))
    roots = [root for root in roots if root is not None]
    for root in roots:
        _link_titles(root)
    loading = any(node.loading for root in roots for node in root.walk())
    page = next(
        (
            n.url
            for root in roots
            for n in root.walk()
            if n.role == "AXWebArea" and n.url
        ),
        roots[0].url if roots else "",
    )
    roots = [prune.run(root, ctx) for root in roots]
    capture = Capture(
        window_title="Unknown" if window.title is None else window.title,
        app_name=target.name,
        window_id=window.window_id,
        lines=_lines(roots, ctx),
        menu_open=menu is not None,
        loading=loading,
        url=(page or None) if kind.http or kind.chromium else None,
    )
    if menu is None:
        with contextlib.suppress(errors.HarnessError):  # no footer beats no state
            _footer_facts(capture, ax.get(app_el, "AXFocusedUIElement"), ctx)
    return capture


def ask_once(element: Any) -> None:
    """Ask every element below this one for its children, then give a web view the
    time to hand out its page. Before the first read of an app."""
    start, todo, seen = time.monotonic(), [element], 0
    while todo and seen < FIRST_ASK_LIMIT:
        seen += 1
        with contextlib.suppress(errors.HarnessError):
            todo.extend(ax.get(todo.pop(), "AXChildren") or [])
    time.sleep(max(0.0, start + FIRST_ASK_WAIT - time.monotonic()))


def _footer_facts(capture: Capture, focus: Any, ctx: Context) -> None:
    """The focused element, and what is selected in it: text first, else elements."""
    if focus is None:
        return
    capture.focused = _own_line(focus, capture.lines)
    if capture.focused is None:
        return
    capture.selected_text = _selected_text(focus)
    chosen = next(
        (found for name in SELECTION_ATTRIBUTES if (found := ax.get(focus, name))), ()
    )
    picked = [_brief_line(el, ctx) for el in chosen]
    capture.selected = [line for line in picked if line is not None]


# --- the accessibility switch, counted per app because two agents may drive one app ---

_switch_lock = threading.Lock()
# pid -> [count, application element, attribute, whether it was on before us]
_switches: dict[int, list] = {}


MANUAL = "AXManualAccessibility"


def _names(el: Any) -> list[str]:
    try:
        return ax.names(el)
    except errors.HarnessError:
        return []


def enable(target: AppTarget, kind: AppKind) -> bool:
    """Ask the app for its full tree before the first read. Counted. True when this
    switched it on: parts of the tree (a web view's page) come only some ms later."""
    app_el = ax.app(target.pid)
    # The app is asked: one that lists the manual switch gets it, every other
    # app the enhanced one. Electron takes the manual one without listing it. Chrome
    # does not: given the manual switch, a Chrome started in the background kept its
    # page to itself (42 lines of 665, measured 12 Oct).
    offers = kind.electron or MANUAL in _names(app_el)
    # Chrome turns its accessibility on when a client asks its application element for
    # its role, and refuses both switches (measured 12 Oct: 39 lines with no page until
    # the role was asked, 665 after).
    with contextlib.suppress(errors.HarnessError):
        ax.get(app_el, "AXRole")
    with _switch_lock:
        name = MANUAL if offers else "AXEnhancedUserInterface"
        held = _switches.get(target.pid)
        if held is None:  # what it was before: VoiceOver or another tool may hold it
            was = _was_on(app_el, name)
            held = _switches[target.pid] = [0, app_el, name, was]
        held[0] += 1
        if held[0] == 1:
            _set(held, True)
        return held[0] == 1


def disable(target: AppTarget) -> None:
    """Give the switch back. It is cleared with the last holder."""
    with _switch_lock:
        held = _switches.get(target.pid)
        if held is None:
            return
        held[0] -= 1
        if held[0] <= 0:
            _set(_switches.pop(target.pid), False)


def disable_all() -> None:
    """The helper is going away: clear every switch it set."""
    with _switch_lock:
        while _switches:
            _set(_switches.popitem()[1], False)


def _was_on(app_el: Any, name: str) -> bool:
    with contextlib.suppress(Exception):
        return ax.get(app_el, name) is True
    return False


def _set(held: list, on: bool) -> None:
    """Switch it on, or back to what it was: a switch another tool had on stays on."""
    with contextlib.suppress(
        Exception
    ):  # an app without the attribute is read as it is
        if on or not held[3]:
            ax.set(held[1], held[2], on)
