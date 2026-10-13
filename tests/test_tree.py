"""tree.py: the walk, the child limits, the menu bar block, the menu view, the footer facts."""

from __future__ import annotations

import fakes_b
import pytest
from fakes import El
from fakes_b import PID, App, at, load

from macos_harness import tree
from macos_harness.model import AppKind, AppTarget

TREES = ("controls", "web", "menu", "sheet", "empty")


def state(name: str, monkeypatch) -> str:
    return fakes_b.text(load(name).read(monkeypatch))


def lines(name: str, monkeypatch) -> list[str]:
    return state(name, monkeypatch).split("\n")


@pytest.mark.parametrize("name", TREES)
def test_fake_tree_prints_as_its_expected_text(name, monkeypatch):
    assert state(name, monkeypatch) == fakes_b.expected(name)


# --- more than 100 children: only what is visible -------------------------------------


def big(count: int, visible: str | None, on_screen: range = range(0)) -> App:
    """A list with `count` rows. Rows in `on_screen` sit inside the window."""
    rows = [
        {
            "role": "AXButton",
            "AXRoleDescription": "button",
            "AXTitle": f"r{i}",
            "rect": [0, 1 if i in on_screen else 9000, 5, 5],
        }
        for i in range(count)
    ]
    holder = {"role": "AXList", "AXRoleDescription": "list", "children": rows}
    if visible:
        holder[visible] = {"$paths": [[0, i] for i in range(20, 25)]}
    window = {"role": "AXWindow", "AXRoleDescription": "window", "children": [holder]}
    window["rect"] = [0, 0, 800, 600]
    return App({"title": "Big", "app": "Demo", "window": window})


def printed_rows(app: App, monkeypatch) -> tuple[str, list[str]]:
    capture = app.read(monkeypatch)
    holder = capture.lines[1]
    return holder.detail, [line.text for line in capture.lines[2:]]


@pytest.mark.parametrize("visible", [None, "AXVisibleRows", "AXVisibleChildren"])
def test_child_cap_container_before_visible_children_path_used(visible, monkeypatch):
    detail, rows = printed_rows(big(100, visible), monkeypatch)
    assert (detail, len(rows)) == ("", 100)  # 100 children: all of them, no suffix


@pytest.mark.parametrize("count", [101, 499, 500])
@pytest.mark.parametrize("visible", ["AXVisibleRows", "AXVisibleChildren"])
def test_visible_subset_comes_app_s_own_visible_children(count, visible, monkeypatch):
    detail, rows = printed_rows(big(count, visible), monkeypatch)
    assert detail == f"(showing 20-25 of {count} items)"
    assert rows == [f"button r{i}" for i in range(20, 25)]


def test_a_list_that_shows_every_item_says_no_subset(monkeypatch):
    """Electron, 12 Oct: 200 items, all visible: the list is printed bare."""
    spec = big(200, None).spec
    holder = spec["window"]["children"][0]
    holder["AXVisibleChildren"] = {"$paths": [[0, i] for i in range(200)]}
    detail, rows = printed_rows(App(spec), monkeypatch)
    assert (detail, len(rows)) == ("", 200)


def test_cut_applies_only_child_count_passes_threshold_near(monkeypatch):
    assert printed_rows(big(101, None, range(30, 40)), monkeypatch)[0] == (
        "(showing 1-101 of 101 items)"  # seen 30-39, widened to 100, shifted to fit
    )


def test_visible_children_chosen_position_against_clipping_frame_margin(monkeypatch):
    detail, rows = printed_rows(big(499, None, range(200, 230)), monkeypatch)
    assert (
        detail == "(showing 200-300 of 499 items)"
    )  # what is on screen plus neighbours
    assert (rows[0], len(rows)) == ("button r200", 100)
    many = printed_rows(big(499, None, range(150)), monkeypatch)
    assert (
        many[0] == "(showing 0-150 of 499 items)"
    )  # more than 100 on screen: all kept


def test_offscreen_children_cut_container_line_says_so(monkeypatch):
    detail, rows = printed_rows(big(500, None), monkeypatch)
    assert (detail, rows) == ("(showing 0-0 of 500 items)", [])  # too many to filter
    capture = big(101, "AXVisibleRows").read(monkeypatch)
    assert capture.lines[1].text == "list"  # the suffix is the detail, not the text


# --- the menu bar block and the menu view ----------------------------------------------


def test_every_normal_tree_ends_menu_bar_block_lists(monkeypatch):
    assert lines("controls", monkeypatch)[-5:-1] == [
        "41 menu bar",
        "\t42 Demo",
        "\t43 View, ID: viewMenu",
        "",
    ]


def test_top_level_roots_windows_sheets_dialogs_first_menu(monkeypatch):
    tops = [line for line in lines("controls", monkeypatch)[1:] if line[:1].isdigit()]
    assert [top.split(" ", 1)[1].split(" ")[0] for top in tops] == ["standard", "menu"]


def test_apple_menu_listed(monkeypatch):
    assert not any("Apple" in line for line in lines("controls", monkeypatch))


def test_never_walk_menu_bar_default(monkeypatch):
    app = load("menu")
    app.spec["menu"] = None
    out = fakes_b.text(app.read(monkeypatch))
    assert out.endswith(
        "2 menu bar\n\t3 Format\n\nThe focused UI element is 1 button OK"
    )


def test_menu_mode_menu_open_state_only_menu_under(monkeypatch):
    capture = load("menu").read(monkeypatch)
    out = fakes_b.text(capture)
    assert capture.menu_open and capture.focused is None and capture.selected == []
    assert out.startswith('Window: "Demo", App: Demo.\n1 Format, Secondary Actions: C')
    assert (
        out.endswith("8 Smaller\n") and "menu bar" not in out and "button OK" not in out
    )


def test_menu_mode_nested_submenus_fully_expanded_disabled_items(monkeypatch):
    out = lines("menu", monkeypatch)
    assert "\t\t3 Bold, ID: bold:" in out  # checked, not pointed at: plain
    assert "\t\t4 (disabled) Paste, ID: paste:" in out
    assert out[5:9] == ["\t\t5 Font", "\t\t\t6 menu Secondary Actions: Cancel"] + [
        "\t\t\t\t7 Bigger",
        "\t\t\t\t8 Smaller",
    ]


def test_menu_items_carry_selector_like_ids_clicking_item(monkeypatch):
    app = load("menu")
    capture = app.read(monkeypatch)
    bold = capture.lines[2]
    assert bold.text.endswith("ID: bold:")
    assert bold.element is at(app.app.attrs["AXMenuBar"], [1, 0, 0])


def test_menu_items_print_no_keyboard_shortcut(monkeypatch):
    assert lines("menu", monkeypatch)[3] == "\t\t3 Bold, ID: bold:"


def test_a_pop_up_s_menu_is_read_as_the_menu_itself(monkeypatch):
    app = load("menu")  # a pop-up's menu is given as the menu itself
    popup_menu = at(app.app.attrs["AXMenuBar"], [1, 0, 3, 0])
    out = fakes_b.text(app.read(monkeypatch, popup_menu))
    assert out.split("\n")[1:3] == ["1 menu Secondary Actions: Cancel", "\t2 Bigger"]


def test_an_open_pop_up_menu_prints_under_its_button_in_the_window(monkeypatch):
    """The window is read as usual. The open menu is a child of the pop-up button,
    so it prints there, and the rest of the window keeps its place."""
    app = load("controls")
    closed = fakes_b.text(app.read(monkeypatch)).split("\n")
    button = at(app.window, [10])
    items = [
        El("AXMenuItem", {"AXTitle": title, "AXMenuItemMarkChar": mark})
        for title, mark in (("Red", "✓"), ("Green", None), ("Blue", None))
    ]
    items[0].attrs["AXSelected"] = True  # an open pop-up points at its chosen item
    button.children.append(El("AXMenu", children=items, acts={"AXCancel": "cancel"}))
    opened = fakes_b.text(app.read(monkeypatch)).split("\n")
    at_button = next(i for i, text in enumerate(closed) if "pop up button" in text)
    number = int(closed[at_button].split()[0])
    assert opened[: at_button + 1] == closed[: at_button + 1]
    assert opened[at_button + 1 : at_button + 5] == [
        f"\t\t{number + 1} menu Secondary Actions: Cancel",
        f"\t\t\t{number + 2} (selected) Red",
        f"\t\t\t{number + 3} Green",
        f"\t\t\t{number + 4} Blue",
    ]
    assert len(opened) == len(closed) + 4


def test_web_page_parts_print_as_measured(monkeypatch):
    """A <summary> says open or closed in its value only, an image never prints its
    source, a link in AppKit text is a "link"."""
    app = load("controls")
    summary = {"AXTitle": "More", "AXValue": False, "AXExpanded": False}
    image = {"AXDescription": "A red square", "AXURL": "data:image/gif;base64,R0l"}
    link = {"AXTitle": "a link", "AXRoleDescription": "hyperlink", "AXURL": "x.org/2"}
    for role, attrs in [
        ("AXDisclosureTriangle", {"AXRoleDescription": "summary", **summary}),
        ("AXImage", {"AXRoleDescription": "image", **image}),
        ("AXLink", link),
    ]:
        app.window.children.append(El(role, attrs))
    lines = fakes_b.text(app.read(monkeypatch)).split("\n")
    assert [line.split(" ", 1)[1] for line in lines if "More" in line] == [
        "summary More, Value: off"
    ]
    assert any(line.endswith(" image A red square") for line in lines)
    assert any(line.endswith(" link a link, Value: x.org/2") for line in lines)


def test_a_file_given_by_an_id_that_leads_nowhere_prints_unresolved(monkeypatch):
    """Finder, 12 Oct: a file name's URL is a file reference that may lead nowhere."""
    app = load("controls")
    name = {"AXValue": "alpha.txt", "AXURL": "file:///.file/id=6571367.195846319"}
    app.window.children.append(
        El("AXTextField", {"AXRoleDescription": "text field", **name})
    )
    lines = fakes_b.text(app.read(monkeypatch)).split("\n")
    assert [line.split(" ", 1)[1] for line in lines if "alpha.txt" in line] == [
        "text field URL: com-apple-unresolvable-file-reference-url:, Value: alpha.txt"
    ]  # no such file


def test_a_file_reference_that_resolves_prints_its_path(tmp_path):
    from Foundation import NSURL

    file = tmp_path / "alpha.txt"
    file.write_text("a")
    reference = str(
        NSURL.fileURLWithPath_(str(file)).fileReferenceURL().absoluteString()
    )
    assert reference.startswith(tree.FILE_REFERENCE)
    assert (
        tree._file_path(reference)
        == NSURL.fileURLWithPath_(str(file.resolve())).absoluteString()
    )


def test_the_first_ask_visits_every_child_once_within_its_limit(monkeypatch):
    from fakes import FakeAx

    leaves = [El("AXButton") for _ in range(5)]
    root = El("AXWindow", children=[El("AXGroup", children=leaves[:3]), *leaves[3:]])
    asked = []
    fake = FakeAx({})
    monkeypatch.setattr(tree, "ax", fake)
    monkeypatch.setattr(fake, "get", lambda el, name: asked.append(el) or el.children)
    monkeypatch.setattr(tree.time, "sleep", lambda s: None)
    tree.ask_once(root)
    assert len(asked) == 7 and len(set(map(id, asked))) == 7
    monkeypatch.setattr(tree, "FIRST_ASK_LIMIT", 3)
    asked.clear()
    tree.ask_once(root)
    assert len(asked) == 3


def test_a_tree_read_from_one_hierarchy_copy_prints_as_the_walk(monkeypatch):
    """The one-call copy hands over every element's attributes at once; the printed
    tree is the same as one batched read per element, and those reads are not made.
    An element the copy left out is still read on its own."""
    walked = fakes_b.text(load("controls").read(monkeypatch))
    app = load("controls")
    real_many = app.ax.get_many
    copied = {
        id(el): real_many(el, tree.ATTRIBUTES) for el in list(app.window.walk())[:-1]
    }
    asked = []
    monkeypatch.setattr(
        app.ax,
        "subtree",
        lambda root, names, limit: [
            (el, copied[id(el)]) for el in app.window.walk() if id(el) in copied
        ],
    )
    monkeypatch.setattr(
        app.ax, "get_many", lambda el, names: asked.append(el) or real_many(el, names)
    )
    assert fakes_b.text(app.read(monkeypatch)) == walked
    window_parts = set(map(id, app.window.walk()))
    assert [el for el in asked if id(el) in window_parts] == [
        list(app.window.walk())[-1]
    ]


def test_flat_tree_builder_context_type_switch_menus(monkeypatch):
    assert load("controls").read(monkeypatch).menu_open is False


def test_menu_bar_extras_attribute_attribute_table():
    assert "AXExtrasMenuBar" not in tree.ATTRIBUTES


# --- what the footer and the other modules need ----------------------------------------


def test_empty_tree_header_no_elements(monkeypatch):
    capture = load("empty").read(monkeypatch)
    assert capture.lines == [] and capture.focused is None
    assert fakes_b.text(capture) == 'Window: "", App: Demo.\n\n'


def test_a_state_needs_an_attribute_that_can_be_set_listed_or_not(monkeypatch):
    """13 Oct. Pages: a page and a text area can be selected without listing
    `AXSelected`, and `selectable` is printed. A Chromium page: the pop-up
    button lists `AXExpanded` and nobody can set it, and it prints no
    `collapsed`."""
    can = {("page", "AXSelected")}
    monkeypatch.setattr(tree.ax, "settable", lambda el, name: (el, name) in can)
    assert tree._can_set("page", "AXSelected")  # can be set, not listed
    assert not tree._can_set("popup", "AXExpanded")  # listed, cannot be set


def test_in_a_chromium_app_settable_is_printed_only_where_a_value_is_taken():
    """Chrome and Electron, 13 Oct: every element lists a value that can be set.
    `settable` is printed for the text fields and the tab (on or off) only."""
    takes = tree._takes_a_value
    assert (
        takes("AXTextField", "")
        and takes("AXTextArea", "x")
        and takes("AXRadioButton", True)
    )
    assert takes("AXSlider", 3) and takes("AXValueIndicator", 0.5)
    for role, value in [
        ("AXGroup", ""),
        ("AXButton", ""),
        ("AXPopUpButton", "Red"),
        ("AXCheckBox", 0),
    ]:
        assert not takes(role, value)


def test_a_menu_item_the_menu_points_at_is_selected():
    """12 Oct: `(selected) Red` is printed for a Chromium pop-up's chosen
    option, `(selected) Get Info` for the first item of Finder's context menu, and a
    checked `Text Replacement` plain. A menu bar title that is open never prints it."""
    assert tree._selected("AXMenuItem", {"AXSelected": True})
    assert not tree._selected("AXMenuItem", {"AXSelected": False})  # though checked
    assert not tree._selected("AXMenuBarItem", {"AXSelected": True})
    assert tree._selected("AXRow", {"AXSelected": True})


def test_focus_line_is_the_tree_line_or_nothing(monkeypatch):
    """Finder while a file is renamed (12 Oct): the field editor is in no line of the
    tree, and no focus and no selection is printed."""
    app = load("controls")
    capture = app.read(monkeypatch)
    assert capture.focused is capture.lines[6]
    editor = El("AXTextField", {"AXRoleDescription": "x", "AXSelectedText": "alpha"})
    app.app.attrs["AXFocusedUIElement"] = editor
    capture = app.read(monkeypatch)
    assert (capture.focused, capture.selected_text, capture.selected) == (
        None,
        None,
        [],
    )


def test_selected_text_read_three_ways(monkeypatch):
    app = load("controls")
    area = at(app.window, [15, 0])
    app.app.attrs["AXFocusedUIElement"] = area
    area.attrs["AXSelectedText"] = "Line 2"
    assert app.read(monkeypatch).selected_text == "Line 2"
    area.attrs["AXSelectedText"] = ""  # web content: a marker range and its string
    area.attrs["AXSelectedTextMarkerRange"] = "markers"
    area.attrs["AXStringForTextMarkerRange"] = lambda markers: f"text of {markers}"
    assert app.read(monkeypatch).selected_text == "text of markers"


def test_edit_menu_searched_copy_find_use_selection_find(monkeypatch):
    app = load("controls")  # no selection: nothing is read from the Edit menu
    capture = app.read(monkeypatch)
    assert capture.selected_text is None and app.app.performed == []


def test_selection_block_lines_drop_the_text(monkeypatch):
    app = load("controls")
    table = at(app.window, [16, 0])
    table.attrs["AXSelectedRows"] = [table.children[1]]
    app.app.attrs["AXFocusedUIElement"] = table
    capture = app.read(monkeypatch)
    assert [line.text for line in capture.selected] == ["row (selected)"]
    assert "Selected:\n\t29 row (selected)\n\nNote: " in fakes_b.text(capture)


def test_loading_signs_and_page_url(monkeypatch):
    app = load("web")
    capture = app.read(monkeypatch)
    assert (
        capture.loading
        and capture.url == "https://www.example.com/docs/page?lang=en#top"
    )
    web = at(app.window, [1])
    web.children.pop()  # the busy indicator
    assert app.read(monkeypatch).loading is False
    for sign in ({"AXLoaded": False}, {"AXElementBusy": True}):
        web.attrs.update(sign)
        assert app.read(monkeypatch).loading is True
        web.attrs.update(AXLoaded=True, AXElementBusy=False)
    web.children[1].attrs["AXDOMClassList"] = ["x", tree.SPINNER_CLASS]
    assert app.read(monkeypatch).loading is True
    app.kind = AppKind()  # not a browser: no page URL for the URL policy
    assert app.read(monkeypatch).url is None


def test_line_carries_what_ids_and_actions_need(monkeypatch):
    capture = load("controls").read(monkeypatch)
    stepper = capture.lines[14]
    assert stepper.actions == {"Increment": "AXIncrement", "Decrement": "AXDecrement"}
    assert stepper.match == ("AXIncrementor", "", "", "", "stepper", "", "", "")
    assert (stepper.role, stepper.value, stepper.settable) == (
        "AXIncrementor",
        "3.5",
        False,
    )
    row = load("web").read(monkeypatch).lines[3]
    assert row.actions == {"Collapse": "AXDisclosing"}  # a disclosure, not an action


def test_extra_attributes_read_never_show_output_key_shortcuts():
    assert {"AXTitleUIElement", "AXMenuItemMarkChar"} <= set(tree.ATTRIBUTES)
    assert not {"AXKeyShortcutsValue", "AXMenuItemCmdChar"} & set(tree.ATTRIBUTES)


def test_platform_fact_chromium_returns_success_empty_values_table(monkeypatch):
    app = big(101, None, range(5))  # an empty visible-rows answer is no answer
    at(app.window, [0]).attrs["AXVisibleRows"] = []
    assert printed_rows(app, monkeypatch)[0] == "(showing 0-100 of 101 items)"


def test_never_use_has_axpress_interactivity(monkeypatch):
    out = state("controls", monkeypatch)  # pressable things print no state for it
    assert "button Press me, ID: press" in out and "(settable) Press" not in out


def test_max_depth_guard_value(monkeypatch):
    chain = spec = {"role": "AXWindow", "AXTitle": "top"}
    for depth in range(tree.MAX_DEPTH + 5):
        spec["children"] = [{"role": "AXButton", "AXTitle": f"d{depth}"}]
        spec = spec["children"][0]
    capture = App({"title": "Deep", "app": "Demo", "window": chain}).read(monkeypatch)
    assert tree.MAX_DEPTH == 100 and len(capture.lines) == tree.MAX_DEPTH + 1


def test_parent_chain_walk_cap():
    assert tree.MAX_CHILDREN == 100 and tree.MAX_FILTERED == 499  # the walk's limits


def test_three_context_types_change_what_read(monkeypatch):
    out = state("controls", monkeypatch)  # the one context built: actions, menu bar
    assert "Secondary Actions: Raise" in out and "\n41 menu bar" in out


def test_scroll_bar_part_subroles_known_name(monkeypatch):
    out = lines("controls", monkeypatch)
    assert "\t\t\t21 increment page button" in out  # parts print as the app names them


def test_string_values_have_conversion_type_element_encoded_values():
    assert tree._value("abc", settable=False) == ("abc", "string")
    assert tree._value(0.3571428571428572, False) == ("0.3571428571428572", "float")
    assert tree._value(float("nan"), False)[0] == "nan"
    assert tree._value(True, False) == ("on", "boolean")
    assert tree._value(3, False) == ("3", "integer")
    assert tree._value(object(), False) == ("", "unknown")
    assert len(tree._value("x" * 600, settable=False)[0]) == 512  # read-only: cut
    assert len(tree._value("x" * 600, settable=True)[0]) == 600  # editable: whole


# --- the accessibility switch -----------------------------------------------------------


@pytest.fixture
def switch(monkeypatch):
    fake = fakes_b.Ax({PID: El("AXApplication")})
    monkeypatch.setattr(tree, "ax", fake)
    monkeypatch.setattr(tree, "_switches", {})
    return fake.apps[PID]


TARGET = AppTarget("/Applications/Demo.app", "dev.demo", "Demo", PID)


def test_before_reading_electron_chromium_accessibility_is_switched_on(switch):
    """Electron takes the manual switch without listing it. Chrome does not list it
    and gets the enhanced one: with the manual one a Chrome started in the background
    showed no page (measured 12 Oct, 42 lines of 665)."""
    tree.enable(TARGET, AppKind(electron=True))
    assert switch.attrs == {"AXManualAccessibility": True}
    tree.disable(TARGET)
    switch.attrs.clear()
    tree.enable(TARGET, AppKind(chromium=True))
    assert switch.attrs == {"AXEnhancedUserInterface": True}
    tree.disable(TARGET)


def test_the_application_element_is_asked_for_its_role(switch, monkeypatch):
    """Chrome refuses both switches and turns its accessibility on when its application
    element is asked for its role: without that a Chrome started in the background
    showed no page."""
    asked = []
    real = tree.ax.get
    monkeypatch.setattr(
        tree.ax, "get", lambda el, name: asked.append((el, name)) or real(el, name)
    )
    tree.enable(TARGET, AppKind(chromium=True))
    assert (switch, "AXRole") in asked


def test_an_app_that_offers_the_manual_switch_gets_it_whatever_its_kind(switch):
    """The app element is asked: listed among its attributes, the manual
    switch is used (an app built on a web engine the bundle does not reveal)."""
    switch.attrs["AXManualAccessibility"] = False
    tree.enable(TARGET, AppKind())
    assert switch.attrs["AXManualAccessibility"] is True
    assert "AXEnhancedUserInterface" not in switch.attrs


def test_before_reading_appkit_enhanced_user_interface_is_switched_on(switch):
    tree.enable(TARGET, AppKind())
    assert switch.attrs == {"AXEnhancedUserInterface": True}


def test_accessibility_enablement_reference_counted_app_can_turned_off(switch):
    assert tree.enable(TARGET, AppKind()) is True  # this one switched it on
    assert tree.enable(TARGET, AppKind()) is False  # a second agent
    tree.disable(TARGET)
    assert switch.attrs["AXEnhancedUserInterface"] is True  # still held by one
    tree.disable(TARGET)
    assert switch.attrs["AXEnhancedUserInterface"] is False
    tree.disable(TARGET)  # one too many does nothing
    tree.enable(TARGET, AppKind())
    tree.disable_all()  # the helper exits
    assert switch.attrs["AXEnhancedUserInterface"] is False and tree._switches == {}


def test_a_switch_that_was_on_before_us_stays_on(switch):
    """VoiceOver or another assistive tool had the app's switch on; letting go of the
    app switched it off under them."""
    switch.attrs["AXEnhancedUserInterface"] = True
    tree.enable(TARGET, AppKind())
    tree.disable(TARGET)
    assert switch.attrs["AXEnhancedUserInterface"] is True


def test_a_catalyst_app_gets_the_one_attribute_and_nothing_else(switch):
    tree.enable(TARGET, AppKind(catalyst=True))  # nothing but the one attribute is set
    assert switch.performed == ["set AXEnhancedUserInterface"]
