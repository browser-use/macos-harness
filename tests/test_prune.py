"""prune.py: the twelve passes, each on the smallest tree that shows its rule."""

from __future__ import annotations

import fakes_b
import pytest

from macos_harness import prune, richtext
from macos_harness.tree import Context, Node

CTX = Context(markdown=richtext.markdown)


def n(role: str = "AXGroup", *children: Node, **fields) -> Node:
    return Node(role=role, children=list(children), **fields)


def text(value: str, **fields) -> Node:
    return n("AXStaticText", value=value, **fields)


def shape(node: Node) -> list:
    """Role, the first text it has, and its children the same way."""
    said = node.title or node.value or node.description
    return [node.role[2:], said, *map(shape, node.children)]


def run(root: Node, ctx: Context = CTX) -> Node:
    return prune.run(root, ctx)


def test_each_pass_returns_four_results_passes_run_app():
    names = [one_pass.__name__ for one_pass in prune.PASSES]
    assert names == [
        "filter_actions",
        "associate_titles",
        "flatten_into_selectable",
        # These two run before the pruning: a group left with one child by the
        # pruning keeps its line (Chrome's window, 13 Oct).
        "flatten_redundant_hierarchy",
        "merge_single_item_groups",
        "prune_non_descriptive",
        "prune_empty_disabled",
        "drop_silent",
        "flatten_repetitive_text",
        "flatten_links",
        "merge_text_siblings",
        "calendar_events",
        "rich_text",
    ]
    answers = {"keep": None, "drop": [], "split": [text("a"), text("b")]}
    root = n("AXWindow", text("keep"), text("drop"), text("split"), text("change"))

    def rule(node, ctx):
        if node.value == "change":
            node.value = "changed"  # changed in place: the answer is still None
        return answers.get(node.value)

    out = prune._each(rule)(root, CTX)
    assert [child.value for child in out.children] == ["keep", "a", "b", "changed"]
    assert prune._each(lambda node, ctx: [])(root, CTX) is root  # the root always stays


def test_pruning_conditioned_app_shape_flags():
    event = n("AXStaticText", text("9:00"), role_description="Event", value="Standup")
    kept = run(n("AXWindow", event), Context(bundle_id="com.example.other"))
    assert shape(kept)[2] == ["StaticText", "Standup", ["StaticText", "9:00"]]


def test_flag_removes_elements_under_calendar_events():
    event = n("AXStaticText", text("9:00"), role_description="Event", value="Standup")
    two = n(
        "AXStaticText", text("a"), n("AXButton", title="b"), role_description="Event"
    )
    out = run(n("AXWindow", event, two, title="w"), Context(bundle_id=prune.CALENDAR))
    assert shape(out)[2] == ["StaticText", "Standup"]  # exactly one text child: dropped
    assert len(out.children[1].children) == 2


@pytest.mark.parametrize("name", sorted(prune.DROPPED_ACTIONS))
def test_flag_filters_actions_shown(name):
    node = n("AXButton", title="b", actions={"Label": name, "Raise": "AXRaise"})
    prune.filter_actions(node, CTX)
    assert node.actions == {"Raise": "AXRaise"}


@pytest.mark.parametrize("label", sorted(prune.DROPPED_LABELS))
def test_three_action_like_labels_are_dropped(label):
    node = n("AXCell", actions={label: f"Name:{label}", "open": "AXOpen"})
    prune.filter_actions(node, CTX)
    assert node.actions == {"open": "AXOpen"}


def test_table_handling_uses_row_column_index_ranges_labels():
    assert set(prune.DROPPED_LABELS) == {
        "Add comment",
        "Remove unused rows",
        "Remove unused columns",
        "Remove unused rows and columns",
    }


def test_actions_one_role_never_prints():
    def left(role, **fields):
        actions = {"Cancel": "AXCancel", "Pick": "AXPick", "Confirm": "AXConfirm"}
        actions |= {"Increment": "AXIncrement", "Scroll Up": "AXScrollUpByPage"}
        node = n(
            role, actions=actions | {"Scroll Left": "AXScrollLeftByPage"}, **fields
        )
        prune.filter_actions(node, CTX)
        return list(node.actions)

    everything = ["Cancel", "Pick", "Confirm", "Increment", "Scroll Up", "Scroll Left"]
    assert left("AXMenuBarItem") == everything
    assert left("AXMenuItem") == everything[2:]  # a menu item: no cancel, no pick
    assert left("AXTextField") == [x for x in everything if x != "Confirm"]
    assert left("AXComboBox", settable=True) == [
        x for x in everything if x != "Increment"
    ]
    assert left("AXScrollArea") == everything[:4]  # no scroll bar: no page scroll
    assert left("AXScrollArea", scroll_axes="v") == everything[:5]
    assert left("AXScrollArea", scroll_axes="vh") == everything


def test_pruning_pass_1_9_associate_title_elements_control():
    label, inside = text("Where:"), text("Item 1")
    popup = n("AXPopUpButton", value="Red", title_node=label)
    cell = n("AXCell", inside, title_node=inside)
    named = n("AXButton", title="Own", title_node=text("Other"))
    out = run(n("AXWindow", label, popup, cell, named, title="w"))
    assert shape(out)[2:] == [
        ["PopUpButton", "Where:"],  # the text became the title and left the tree
        ["Cell", "", ["StaticText", "Item 1"]],  # a title inside its element stays
        ["Button", "Own"],
    ]


def test_title_association_uses_link_attributes_tree_map():
    menu_title = n("AXMenuBarItem", title="File")
    menu = n("AXMenu", n("AXMenuItem", title="New"), title_node=menu_title)
    menu_title.children = [menu]
    out = run(menu_title)
    assert shape(out) == ["MenuBarItem", "File", ["Menu", "", ["MenuItem", "New"]]]


def row(*children, **fields) -> Node:
    return n("AXRow", *children, selectable=True, **fields)


def test_pruning_pass_2_9_flatten_children_selectable_ancestor():
    image = n("AXImage", description="External drive")
    eject = n("AXButton", title="Eject", description="eject", acts=("AXPress",))
    plain = row(n("AXCell", image, text(" Backup "), eject), subrole="AXOutlineRow")
    table = row(
        n("AXCell", text("Item 1")), n("AXCell", text("3")), subrole="AXTableRow"
    )
    field = n("AXTextField", value="Name", settable=True)
    editable = row(n("AXCell", field))
    nested = row(n("AXCell", text("x"), selectable=True))
    selected_only = n("AXRow", text("Users"), selected=True)
    out = run(n("AXOutline", plain, table, editable, nested, selected_only, title="o"))
    first = out.children[0]
    assert (first.title, first.value, first.children) == ("Eject", "Backup", [])
    assert first.description == "External drive\neject"  # joined with a newline
    assert shape(out.children[1])[2] == ["Cell", "", ["StaticText", "Item 1"]]
    assert shape(out.children[2])[2][2] == ["TextField", "Name"]  # can be set: stays
    assert shape(out.children[3])[2] == ["Cell", "x"]  # the cell took its own text
    assert shape(out.children[4])[2] == ["StaticText", "Users"]  # not selectable


def test_pruning_pass_3_9_prune_non_descriptive_subtrees():
    holder = n("AXButton", n("AXGroup"), title="Full screen", acts=("AXPress",))
    column = n("AXColumn", identifier="item")
    blank = text(" ", acts=("AXShowMenu", "AXScrollToVisible"))
    arrow = n("AXButton", acts=("AXPress",))
    focusable = n("AXList", focusable=True)
    cut = n("AXList", subset=(0, 0, 500))
    out = run(n("AXWindow", holder, column, blank, arrow, focusable, cut, title="w"))
    assert shape(out)[2:] == [
        ["Button", "Full screen"],
        ["Button", ""],
        ["List", ""],
    ] + [["List", ""]]


def test_pruning_predicates_node():
    assert not prune._described(n(identifier="only-an-id", value=" \n"))
    assert prune._described(n(help="hint")) and prune._described(n(placeholder="p"))
    assert not prune._interactive(n(acts=("AXShowMenu", "AXScrollToVisible")))
    for field in ("selectable", "settable", "focusable", "selected", "focused"):
        assert prune._interactive(n(**{field: True}))
    assert prune._interactive(n(acts=("AXPress",)))


def test_pruning_pass_4_9_prune_empty_disabled_elements():
    separator = n("AXMenuItem", enabled=False, acts=("AXPick",))
    paste = n("AXMenuItem", enabled=False, title="Paste", acts=("AXPick",))
    zoom = n("AXButton", enabled=False, acts=("AXPress",))
    out = run(n("AXMenu", separator, paste, zoom))
    assert shape(out)[2:] == [["MenuItem", "Paste"], ["Button", ""]]


def test_a_plain_split_group_inside_a_split_group_gives_way_to_its_panes():
    """Finder, 12 Oct: the file list, the status bar and the path list print right
    under the window's split group. Font Book's inner one has an identifier and stays."""
    files = n("AXScrollArea", n("AXButton", title="list", acts=("AXPress",)))
    side = n("AXScrollArea", n("AXButton", title="side", acts=("AXPress",)))
    status = n("AXUnknown", title="status bar")
    outer = n("AXSplitGroup", side, n("AXSplitGroup", files, status))
    out = run(n("AXWindow", outer, title="w"))
    assert [part[0] for part in shape(out)[2][2:]] == [
        "ScrollArea",
        "ScrollArea",
        "Unknown",
    ]
    named = n("AXSplitGroup", n("AXButton", title="a", acts=("AXPress",)))
    named.identifier = "SidebarNavigationSplitView"
    kept = run(n("AXWindow", n("AXSplitGroup", named), title="w"))
    assert shape(kept)[2][2][0] == "SplitGroup"


def test_groups_are_opened_from_the_top_one_level_and_merged_before_the_pruning():
    """Chrome's window, 13 Oct: the toolbar right under the
    outer container, `container` kept around the page and around the tab strip (each
    was left with one child only when its empty neighbours were pruned), the pop-up
    button without its own group."""
    button = lambda title: n("AXButton", title=title, acts=("AXPress",))
    empty = lambda: n("AXGroup")
    toolbar = n("AXToolbar", button("Back"))
    page = n("AXScrollArea", button("page"))
    tabs = n("AXTabGroup", button("tab"))
    inner = n(
        "AXGroup",
        n("AXGroup", toolbar, empty()),
        n("AXGroup", n("AXGroup", empty(), page, empty())),
        n(
            "AXGroup",
            n("AXGroup", button("Tab search")),
            n("AXGroup", tabs, empty()),
            empty(),
        ),
        empty(),
    )
    out = run(
        n(
            "AXWindow",
            n("AXGroup", n("AXGroup", n("AXGroup", inner)), title="Chrome"),
            title="w",
        )
    )
    assert shape(out)[2] == [
        "Group",
        "Chrome",
        [
            "Group",
            "",
            ["Toolbar", "", ["Button", "Back"]],
            ["Group", "", ["ScrollArea", "", ["Button", "page"]]],
            ["Button", "Tab search"],
            ["Group", "", ["TabGroup", "", ["Button", "tab"]]],
        ],
    ]


def test_an_empty_group_stays_only_if_it_can_be_pressed_set_or_selected():
    """13 Oct: Chrome's empty groups can take focus, Numbers' toolbar spacers can be
    moved (`Move previous`, `Remove from toolbar`): none of them is printed.
    A leaf of another role that can take focus stays."""
    spacer = n("AXGroup", acts=("Name:Move previous", "Name:Remove from toolbar"))
    spacer.actions = {"Move previous": "Name:Move previous"}
    out = run(
        n(
            "AXWindow",
            n("AXGroup", focusable=True),
            spacer,
            n("AXGroup", acts=("AXPress",)),
            n("AXGroup", selectable=True),
            n("AXUnknown", focusable=True),
            text("x"),
            title="w",
        )
    )
    assert [part[0] for part in shape(out)[2:]] == [
        "Group",
        "Group",
        "Unknown",
        "StaticText",
    ]


def test_a_text_that_two_elements_name_as_their_title_stays():
    """Pages' Format sidebar, 13 Oct: `disclosure triangle 0`, `text Spacing`,
    `pop-up button Help: ...` are printed. One element naming it takes it."""
    label, own, helped = (
        text("Spacing"),
        text("Size"),
        text("Drop Cap", help="Select to..."),
    )
    triangle = n("AXDisclosureTriangle", value="0", acts=("AXPress",), title_node=label)
    popup = n("AXPopUpButton", value="1.0", acts=("AXPress",), title_node=label)
    box = n("AXCheckBox", value="0", acts=("AXPress",), title_node=own)
    cap = n("AXCheckBox", value="0", acts=("AXPress",), title_node=helped)
    out = run(n("AXWindow", triangle, label, popup, own, box, helped, cap, title="w"))
    assert (triangle.title, popup.title, box.title, cap.title) == ("", "", "Size", "")
    assert [node.role for node in out.children] == [
        "AXDisclosureTriangle",
        "AXStaticText",
        "AXPopUpButton",
        "AXCheckBox",
        "AXStaticText",  # "Drop Cap" has a help of its own: it stays a text
        "AXCheckBox",
    ]


def test_a_table_that_can_be_selected_keeps_its_rows():
    """Numbers, 12 Oct: `table (selectable) expenses` with every row and cell below it."""
    rows = [n("AXRow", n("AXCell", text(f"Item {i}")), title=str(i)) for i in (1, 2)]
    out = run(
        n("AXWindow", n("AXTable", *rows, title="expenses", selectable=True), title="w")
    )
    table = shape(out)[2]
    assert table[:2] == ["Table", "expenses"] and [row[0] for row in table[2:]] == [
        "Row",
        "Row",
    ]


def test_the_image_of_a_table_cell_is_printed_bare():
    """Finder, 12 Oct: a file row's icon prints as `image`. Any other image
    that says nothing goes, as before."""
    name = n("AXTextField", value="alpha.txt", settable=True)
    row = n("AXRow", n("AXCell", n("AXImage"), name))
    out = run(
        n(
            "AXWindow",
            n("AXOutline", row),
            n("AXGroup", n("AXImage"), text("x")),
            title="w",
        )
    )
    cell = shape(out)[2][2][2]
    assert [part[0] for part in cell[2:]] == ["Image", "TextField"]
    assert "Image" not in str(shape(out)[3:])


def test_pruning_pass_5_9_merge_single_item_groups():
    hosted = n("AXGroup", n("AXGroup", n("AXButton", title="Deep", acts=("AXPress",))))
    helped = n(
        "AXGroup", n("AXCheckBox", title="Filter", acts=("AXPress",)), help="Filter"
    )
    named = n("AXGroup", text("inside"), title="Formatting")
    pressable = n("AXGroup", text("web"), acts=("AXPress", "AXShowMenu"))
    two = n("AXGroup", n("AXButton", title="a"), n("AXButton", title="b"))
    out = run(n("AXWindow", hosted, helped, named, pressable, two, title="w"))
    assert shape(out)[2:] == [
        ["Button", "Deep"],
        ["CheckBox", "Filter"],  # a help text does not keep the group
        ["Group", "Formatting", ["StaticText", "inside"]],
        ["Group", "", ["StaticText", "web"]],  # a group that can be pressed stays
        ["Group", "", ["Button", "a"], ["Button", "b"]],
    ]


def test_a_collapsed_details_gives_way_to_its_summary_an_open_one_stays():
    """Measured on a web page: closed, the <details> group is not printed and its
    summary takes its place; open, it prints (expanded) with its two children."""

    def details(expanded, *children):
        return n("AXGroup", *children, acts=("AXPress",), expanded=expanded)

    summary = n("AXDisclosureTriangle", title="More")
    closed = run(n("AXWebArea", details(False, summary)))
    assert shape(closed)[2] == ["DisclosureTriangle", "More"]
    opened = run(
        n(
            "AXWebArea",
            details(True, n("AXDisclosureTriangle", title="More"), text("Hidden")),
        )
    )
    assert shape(opened)[2][0] == "Group"  # kept


def test_pruned_bare_container_lines_no_text():
    two = n("AXGroup", n("AXButton", title="a"), n("AXButton", title="b"))
    assert shape(run(n("AXWindow", two, title="w")))[2][:2] == ["Group", ""]


def test_pruning_pass_6_9_flatten_redundant_hierarchy_rule():
    def pair():
        return [n("AXLink", title="a", url="u"), text(" by ")]

    inner = n("AXGroup", *pair())
    outer = n("AXGroup", n("AXLink", title="first", url="u"), inner)
    titled = n("AXGroup", n("AXGroup", *pair()), n("AXButton", title="x"), title="Box")
    radios = n(
        "AXRadioGroup", n("AXRadioButton", title="All"), n("AXRadioButton", title="x")
    )
    helped = n("AXRadioGroup", n("AXRadioButton", title="Grid"), help="View mode")
    out = run(n("AXWebArea", outer, titled, radios, helped, title="page"))
    assert [c.role for c in out.children[0].children] == [
        "AXLink",
        "AXLink",
        "AXStaticText",
    ]
    assert out.children[1].children[0].role == "AXGroup"  # its parent says something
    assert [c.title for c in out.children[2:4]] == ["All", "x"]  # a silent radio group
    assert out.children[4].role == "AXRadioGroup"


def test_pruning_pass_7_9_flatten_repetitive_static_text():
    out = run(n("AXWebArea", text("."), text("."), n("AXImage", title="i"), text(".")))
    assert shape(out)[2:] == [["StaticText", "."], ["Image", "i"], ["StaticText", "."]]


def test_a_text_loses_the_texts_below_it_that_repeat_it():
    label = n("AXStaticText", text("Query"), value="Query")  # a web page's <label>
    other = n("AXStaticText", text("3"), value="Total")
    out = run(n("AXWebArea", label, other))
    assert shape(out)[2:] == [
        ["StaticText", "Query"],
        ["StaticText", "Total", ["StaticText", "3"]],  # says more: stays
    ]


def test_pruned_sticky_headers_print_twice_finder():
    out = run(n("AXWebArea", text("Today"), n("AXImage", title="i"), text("Today")))
    assert [c.value for c in out.children] == ["Today", "", "Today"]


def test_pruning_pass_8_9_flatten_links_markdown_text():
    labelled = n(
        "AXLink", text("Luck"), n("AXImage", description="pic"), title="Luck", url="u"
    )
    bare = n("AXLink", text("Read"), text("more"), url="u")
    out = run(n("AXGroup", labelled, bare, title="p"))
    assert shape(out)[2:] == [["Link", "Luck"], ["Link", "Read more"]]
    assert "](" not in out.children[1].title  # no Markdown link is written


def test_pruning_pass_9_9_merge_text_only_siblings():
    first = text("Status: idle", identifier="status", url="file:///")
    out = run(
        n(
            "AXWindow",
            first,
            text("Count: 0", identifier="count"),
            n("AXButton", title="b"),
        )
    )
    merged = out.children[0]
    assert merged is first and merged.value == "Status: idle Count: 0"
    assert (merged.identifier, merged.url, len(out.children)) == (
        "status",
        "file:///",
        2,
    )
    page = run(n("AXWebArea", text("noun"), text("a chance")))
    assert [c.value for c in page.children] == ["noun", "a chance"]  # blocks of a page


def test_pruned_text_duplicated_child():
    out = run(
        n("AXRow", text("Threads"), title="Threads")
    )  # not selectable: no flatten
    assert shape(out) == ["Row", "Threads", ["StaticText", "Threads"]]


def test_pruned_role_only_lines_no_state_no_text():
    out = run(n("AXWindow", n("AXImage", acts=("AXPress",)), n("AXCell", text("x"))))
    assert [c.role for c in out.children] == ["AXImage", "AXCell"]


def test_flags_pull_attributed_text_text_areas_web_areas():
    rich = n(
        "AXTextArea", value="one two", runs=[("one ", {}), ("two", {"bold": True})]
    )
    code = n("AXTextArea", value="x = 1", runs=None)  # source code: no runs were read
    out = run(n("AXWindow", rich, code, title="w"))
    assert [c.value for c in out.children] == ["one **two**", "x = 1"]
    broken = n("AXTextArea", value="plain", runs=[("a", {"heading": "not a number"})])
    assert run(n("AXWindow", broken, title="w")).children[0].value == "plain"


def test_messages_bubbles_carry_sent_me_flag_third_party():
    assert not hasattr(Node(), "sent_by_me")  # not read, so never printed


@pytest.mark.parametrize(
    "needle",
    [
        "\t\t\t21 increment page button",  # scroll bar parts
        "\t39 full screen button Help: this button also has an action to zoom the window",
        "\t5 button Description: Hide Sidebar, Secondary Actions: Increment, Decrement",
    ],
)
def test_pruned_scroll_bar_machinery_6_lines_bar_finder(needle, monkeypatch):
    assert needle in fakes_b.text(fakes_b.load("controls").read(monkeypatch))
