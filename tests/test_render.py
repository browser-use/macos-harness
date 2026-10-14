"""render.py: the line grammar, the header, the three footers, the URL budget."""

from __future__ import annotations

import re
from itertools import pairwise

import fakes_b
import pytest

from macos_harness import render, tree
from macos_harness.model import Capture, Line
from macos_harness.tree import Context, Node

CTX = Context()
LABELS = (
    "Description|Rows|Columns|URL|Help|Value|Details|Placeholder|ID|Secondary Actions"
)
STATES = "selected selectable disabled expanded collapsed settable float integer boolean date unknown"


def one(role: str = "AXButton", role_description: str = "button", **fields) -> str:
    node = Node(role=role, role_description=role_description, **fields)
    return render.lines(node, CTX)[0].text


def controls(monkeypatch) -> str:
    return fakes_b.text(fakes_b.load("controls").read(monkeypatch))


def test_full_tree_header_line_names_window_app():
    assert render.header("Downloads", "Finder") == 'Window: "Downloads", App: Finder.'


def test_header_title_literal_unknown_sheets_empty_string_alerts(monkeypatch):
    assert fakes_b.text(fakes_b.load("sheet").read(monkeypatch)).startswith(
        'Window: "Unknown", App: Demo.\n0 sheet GoToWindow'
    )
    assert render.header("", "Demo") == 'Window: "", App: Demo.'


def test_header_title_page_document_title_without_app_suffix(monkeypatch):
    out = fakes_b.text(fakes_b.load("web").read(monkeypatch)).split("\n")
    assert out[0] == 'Window: "Reader", App: Reader.'  # the title the caller gave
    assert out[1].startswith("0 standard window Reader – 2 found")  # the app's own


def test_quotes_inside_header_title_escaped():
    assert render.header('Say "hi"', "A") == 'Window: "Say "hi"", App: A.'


def test_envelope_order_fixed_optional_instruction_block_header_element(monkeypatch):
    out = controls(monkeypatch).split("\n")
    assert out[0].startswith("Window: ") and out[1].startswith("0 ")
    assert out[-2] == "" and out[-1].startswith("The focused UI element is ")


def test_element_line_grammar_id_first_role_optional_state(monkeypatch):
    shape = re.compile(r"^\t*\d+( [^(\n]+)?( \([a-z, ]+\))?( .+)?$")
    body = controls(monkeypatch).replace("Line 1\nLine 2", "Line 1 Line 2")
    assert all(shape.match(line) for line in body.split("\n")[1:-2])
    assert one(title="", role_description="") == "unknown"
    assert one(title="Back") == "button Back"
    assert one(title="Back", enabled=False) == "button (disabled) Back"


def test_indent_tab_depth_level_depth_never_grows_more(monkeypatch):
    depths = [line.depth for line in fakes_b.load("controls").read(monkeypatch).lines]
    assert depths[0] == 0 and all(b - a <= 1 for a, b in pairwise(depths))
    assert "\n\t\t\t20 value indicator" in controls(monkeypatch)  # tabs, never spaces


def test_some_lines_print_no_role_id_text_optional():
    assert one("AXMenuBarItem", "", title="Finder") == "Finder"
    assert (
        one("AXMenuItem", "menu item", title="Halves", enabled=False)
        == "(disabled) Halves"
    )
    assert one("AXMenuItem", "", title="DESC", selected=True) == "(selected) DESC"
    assert (
        one("AXRadioGroup", "radio group", help="Mode", identifier="m")
        == "Help: Mode, ID: m"
    )
    assert one("AXMenuItem", "") == ""  # a line can be a number and nothing else


def test_fields_joined_comma_space_labelled_fields_label_value():
    line = one(title="A, B", description='say "x"', value="1, 2", identifier="i d")
    assert line == 'button A, B, Description: say "x", Value: 1, 2, ID: i d'


def test_exactly_eight_field_labels_exist(monkeypatch):
    found = set(re.findall(rf"(?:, | )({LABELS}): ", controls(monkeypatch)))
    assert found <= set(LABELS.split("|")) and len(LABELS.split("|")) == 10


def test_short_fields_print_canonical_order():
    line = one(
        "AXCell",
        "cell",
        title="T",
        description="D",
        rows="0-1",
        columns="2-3",
        url="https://www.a.example/x",
        help="H",
        value="V",
        details="Dt",
        placeholder="P",
        identifier="I",
        actions={"Raise": "AXRaise", "open": "AXOpen"},
    )
    assert line == (
        "cell T, Description: D, Rows: 0-1, Columns: 2-3, URL: a.example/x, Help: H, "
        "Value: V, Details: Dt, Placeholder: P, ID: I, Secondary Actions: Raise, open"
    )


def test_element_property_list_declaration_order():
    wanted = "AXRole AXSubrole AXRoleDescription AXTitle AXDescription AXValue"
    wanted += (
        " AXValueDescription AXPlaceholderValue AXHelp AXIdentifier AXDOMClassList"
    )
    wanted += " AXSelected AXFocused AXEnabled AXDisclosing AXElementBusy AXLoaded"
    assert set(wanted.split()) <= set(tree.ATTRIBUTES)


def test_label_attribute_mapping():
    assert one(title="t", details="not selected") == "button t, Details: not selected"
    assert one(title="t", placeholder="Search") == "button t, Placeholder: Search"


def test_dom_class_list_read_but_never_printed(monkeypatch):
    app = fakes_b.load("web")
    fakes_b.at(app.window, [1, 1]).attrs["AXDOMClassList"] = ["hw", "big"]
    assert "hw" not in fakes_b.text(app.read(monkeypatch))


@pytest.mark.parametrize(("length", "moved"), [(100, False), (101, True)])
def test_long_field_rule_field_longer_100_characters_containing(length, moved):
    line = one(title="t", value="v" * length, identifier="i")
    assert line.endswith(", ID: i") is not moved
    assert line.endswith("Value: " + "v" * length) is moved


def test_bare_title_displaced_too_long_several_long_fields():
    long_title, long_url = "T" * 101, "file:///" + "u" * 101
    line = one(
        "AXWindow", "standard window", title=long_title, url=long_url, identifier="w"
    )
    line_with_action = one(
        title=long_title, description="d\ne", actions={"Raise": "AXRaise"}
    )
    assert line == f"standard window ID: w, {long_title}, URL: {long_url}"
    assert (
        line_with_action
        == f"button Secondary Actions: Raise, {long_title}, Description: d\ne"
    )


def test_newlines_value_emitted_raw_continuation_lines_carry_no(monkeypatch):
    assert (
        one(title="t", value="a\nb", identifier="i") == "button t, ID: i, Value: a\nb"
    )
    assert "\n\t\t18 text entry area (settable) ID: notes, Value: Line 1\nLine 2\n" in (
        controls(monkeypatch)
    )


def test_title_prints_bare_first():
    assert one(title="Back", identifier="b") == "button Back, ID: b"


@pytest.mark.parametrize(
    "field", ["description", "value", "help", "placeholder", "identifier", "details"]
)
def test_element_text_attribute_nothing_else_prints_bare_whatever(field):
    assert one("AXStaticText", "text", **{field: "Today"}) == "text Today"


def test_exact_rule_bare_vs_labelled_value_description(monkeypatch):
    out = controls(monkeypatch)
    for pinned in (
        "\t2 button Press me, ID: press",  # a title is always bare
        "\t\t13 value indicator (settable, float) 25",  # only a value: bare
        "\t1 text Value: Status: idle Count: 0, ID: status",  # a value and an id
        "\t7 checkbox Enable option, Value: 0, ID: option",
        "\t11 pop up button Colour:, Value: Red",
    ):
        assert pinned in out.split("\n")
    assert (
        one("AXMenu", "", actions={"Cancel": "AXCancel"})
        == "menu Secondary Actions: Cancel"
    )
    assert (
        one(help="h", actions={"zoom": "AXZoom"})
        == "button Help: h, Secondary Actions: zoom"
    )


def test_link_line_shape_differs_engine():
    url = "https://www.example.org/a"
    assert one("AXLink", "link", description="Apps", url=url) == (
        "link Description: Apps, Value: example.org/a"
    )
    assert (
        one("AXLink", "link", title="Apps", url=url)
        == "link Apps, Value: example.org/a"
    )
    assert one("AXLink", "link", url=url) == "link example.org/a"
    # A link in AppKit text calls itself "hyperlink"; it prints as any link does.
    assert one("AXLink", "hyperlink", title="a link", url=url) == (
        "link a link, Value: example.org/a"
    )


def test_editable_fields_no_value_show_their_label_bare():
    line = one("AXTextField", "text field", title="Connection URL", settable=True)
    assert line == "text field (settable) Connection URL"


@pytest.mark.parametrize(("length", "printed"), [(512, 512), (513, 512)])
def test_static_text_cut_exactly_512_characters_no_marker(length, printed):
    assert len(render.cut("x" * length)) == printed and render.TEXT_CAP == 512
    assert tree._text("y" * length) == "y" * printed  # titles, help, descriptions too


def test_editable_text_capped():
    assert tree._value("z" * 4000, settable=True)[0] == "z" * 4000


def test_merged_runs_sibling_text_attributed_web_text_assembled(monkeypatch):
    app = fakes_b.load("controls")
    for label, letter in zip(app.window.children[:2], "vw", strict=True):
        label.attrs["AXValue"] = letter * 600
    merged = app.read(monkeypatch).lines[1]
    assert merged.value == "v" * 512 + " " + "w" * 512  # cut first, joined after


def test_invisible_odd_characters_pass_through_raw_lines_can():
    assert one(title="a b\u200b ") == "button a b\u200b "


def test_settable_value_type_printed_last_state_token():
    assert (
        one(value="0.5", value_type="float", settable=True)
        == "button (settable, float) 0.5"
    )
    assert one(settable=True, value_type="unknown") == "button (settable, unknown)"


def test_string_type_omitted_settable():
    assert one("AXTextField", "text field", settable=True, value_type="string") == (
        "text field (settable)"
    )


def test_tree_marks_elements_accept_set_value(monkeypatch):
    out = controls(monkeypatch)
    assert "\t12 slider (settable, float) Value: 25, ID: level" in out
    assert "\t7 checkbox Enable option, Value: 0" in out  # its value cannot be set


def test_floats_whole_values_print_without_decimals_fractions_print():
    assert [tree._number(v) for v in (25.0, 0.553133514986376, -1.0, 3)] == [
        "25",
        "0.553133514986376",
        "-1",
        "3",
    ]
    assert tree._number(float("nan")) == "nan"
    # 16 digits, not the 17 a float needs to be read back: three scroll bars as read
    shown = {5 / 14: "0.3571428571428572", 75 / 346: "0.2167630057803468"}
    assert {v: tree._number(v) for v in shown} == shown
    assert tree._number(113 / 230) == "0.491304347826087"  # no zero at the end


def test_booleans_print_off_integer_typed_toggles_print_0(monkeypatch):
    out = controls(monkeypatch)
    assert "\t8 toggle button Description: Show info, Value: off, Details: not" in out
    assert "\t9 radio button All, Value: 1" in out  # a number stays a number
    assert tree._value(False, False) == ("off", "boolean")


def test_heading_level_printed_value_n(monkeypatch):
    assert "\t\t9 heading Serendipity, Value: 1\n" in fakes_b.text(
        fakes_b.load("web").read(monkeypatch)
    )


def test_masked_values_pass_through_drawn_secure_text_fields():
    line = one("AXTextField", "secure text field", value="••••", settable=True)
    assert line == "secure text field (settable) ••••"


def test_role_words_target_app_s_localized_axroledescription_strings():
    assert one(role_description="minimise button") == "minimise button"
    assert one("AXCheckBox", "Tick box", title="x") == "Tick box x"


def test_few_roles_rewritten():
    assert one("AXGroup", "group") == "container"
    assert one("AXGroup", "navigation", title="Main") == "container Main"
    assert one("AXRow", "outline row") == "row"
    assert one("AXMenu", "") == "menu" and one("AXMenuBar", "") == "menu bar"
    assert one("AXWebArea", "HTML content") == "HTML content"
    assert one("AXStaticText", "text", value="x") == "text x"


def test_coarse_roles_print_element_and_unknown():
    assert one("AXGenericElement", "") == "element" and one("AXOther", "") == "unknown"


def test_role_words_that_differ_from_the_system_s():
    assert render.ROLE_WORDS == {
        "AXGroup": "container",
        "AXRow": "row",
        "AXLink": "link",
        "AXMenuItem": "",
        "AXMenuBarItem": "",
        "AXRadioGroup": "",
    }


def test_state_group_vocabulary_closed_11_tokens_no_focused(monkeypatch):
    everything = controls(monkeypatch) + fakes_b.text(
        fakes_b.load("web").read(monkeypatch)
    )
    groups = re.findall(r"^\t*\d+ [^(\n]*\(([a-z, ]+)\) ?", everything, re.MULTILINE)
    assert groups and {t for g in groups for t in g.split(", ")} <= set(STATES.split())
    assert one(focused=True, focusable=True) == "button"  # no token for focus


def test_state_token_order_fixed():
    line = one(
        "AXRow",
        selected=True,
        selectable=True,
        enabled=False,
        expanded=True,
        settable=True,
        value_type="integer",
    )
    assert line == "row (selected, disabled, expanded, settable, integer)"
    assert (
        one("AXRow", selectable=True, expanded=False) == "row (selectable, collapsed)"
    )


def test_no_frame_position_size_any_line(monkeypatch):
    assert not re.search(r"\b(x|y|width|height|frame)[=:] ?\d", controls(monkeypatch))


def test_no_pid_no_window_id_no_raw_ax(monkeypatch):
    out = controls(monkeypatch)
    assert "AX" not in out and "pid" not in out and " 100" not in out


def test_actions_print_inline_element_line_joined_comma_space():
    line = one("AXWindow", "standard window", title="Neo", actions={"Raise": "AXRaise"})
    assert line == "standard window Neo, Secondary Actions: Raise"


def test_action_labels_mix_own_names_and_the_app_s():
    described = {"AXZoomWindow": "zoom the window", "AXScrollToTop": ""}
    custom = "Name:Move previous\nTarget:0x0\nSelector:(null)"
    assert tree._label("AXRaise", described) == "Raise"
    assert tree._label("AXZoomWindow", described) == "zoom the window"
    assert tree._label("AXScrollToTop", described) == "AXScrollToTop"
    assert tree._label(custom, {}) == "Move previous"


def test_press_action_never_listed_secondary_action_click(monkeypatch):
    assert "Press" not in controls(monkeypatch).replace("Press me", "")


def test_ax_page_scroll_actions_exposed_model_secondary_actions(monkeypatch):
    area = fakes_b.load("controls").read(monkeypatch).lines[17]
    assert area.actions == {
        "Scroll Up": "AXScrollUpByPage",
        "Scroll Down": "AXScrollDownByPage",
    }
    assert area.text.endswith("Secondary Actions: Scroll Up, Scroll Down")


def test_id_from_the_toolkit_is_not_printed():
    assert [render.visible_id(i) for i in ("_NS:12", "NSButton7", "press", "N")] == [
        "",
        "",
        "press",
        "N",
    ]


# --- URLs --------------------------------------------------------------------------------


def url_lines(urls: list[str], role: str = "AXLink") -> list[Line]:
    nodes = [Node(role=role, role_description="link", title="t", url=u) for u in urls]
    lines = [render.lines(node, CTX)[0] for node in nodes]
    render.shorten_urls(lines)
    return lines


def test_urls_drop_https_www_other_schemes_stay():
    kept = [
        "file:///a/b",
        "localhost:3000/x",
        "mailto:a@b.c",
        "chrome://settings",
        "about:blank",
    ]
    urls = ["https://www.apple.com/mac", "http://example.org", *kept]
    assert [line.value for line in url_lines(urls)] == [
        "apple.com/mac",
        "example.org",
        *kept,
    ]


def test_url_query_strings_fragments_survive_configurable():
    assert (
        url_lines(["https://a.example/x?q=1#top"])[0].text
        == "link t, Value: a.example/x?q=1#top"
    )


def test_long_urls_cut_link_limit_end_ellipsis():
    crowd = ["https://developer.apple.com/get-started/now?x=1"] * 111
    assert {line.value for line in url_lines(crowd)} == {
        "developer.apple.com/get-start…"
    }
    assert len("developer.apple.com/get-start…") == 30
    many = url_lines(
        ["x-dictionary:r:'3M?lang=en", "x-dictionary:r:'Serendipity?lang=en"] * 126
    )
    assert [line.value for line in many[:2]] == [
        "x-dictionary:r:'3M",
        "x-dictionary:r:'Ser…",
    ]


def test_url_shortening_tree_character_budget_link_limit_steps():
    lines = url_lines(
        [f"https://example.org/{i:04d}/long/path/here" for i in range(300)]
    )
    values = [line.value for line in lines]
    assert values[0] == "example.org/0000/lo…" and len(values[0]) == 20
    assert values.index("…") == 200 and set(values[200:]) == {
        "…"
    }  # 200 x 20 = the budget
    assert lines[250].text == "link t, Value: …"
    files = url_lines(
        ["file:///" + "p" * 300] * 300
    )  # a local path: never cut, costs nothing
    assert all(len(line.value) == 308 for line in files)


def test_url_shortener_configuration_surface():
    assert (render.URL_BUDGET, render.URL_LIMITS) == (
        4000,
        ((110, None), (250, 30), (None, 20)),
    )
    window = url_lines(["https://www.example.org/" + "a" * 60] * 111, role="AXWindow")[
        0
    ]
    assert (
        window.text == "link t, URL: example.org/aaaaaaaaaaaaaaaaa…"
    )  # every URL field


# --- footers -----------------------------------------------------------------------------


def capture(**fields) -> Capture:
    return Capture(window_title="W", app_name="A", window_id=1, lines=[], **fields)


def line(number: int, text: str, detail: str = "") -> Line:
    return Line(
        depth=0, text=text, element=None, role="AXRow", detail=detail, id=number
    )


def test_focused_element_trailer_repeats_element_s_own_line():
    focused = line(6, "text field (settable) Placeholder: Name, ID: name")
    assert render.footer(capture(focused=focused), True) == (
        "The focused UI element is 6 text field (settable) Placeholder: Name, ID: name"
    )
    listed = capture(focused=line(3, "list", "(showing 0-10 of 200 items)"))
    assert render.footer(listed, True).endswith("3 list (showing 0-10 of 200 items)")
    assert render.footer(listed, False) == (
        "The focused UI element is list (showing 0-10 of 200 items)"
    )


def test_selected_text_printed_inside_triple_backtick_fence_written():
    out = render.footer(
        capture(selected_text="Line 30\nLine 31", focused=line(1, "x")), True
    )
    assert out == f"Selected text: ```\nLine 30\nLine 31\n```\n\n{render.NOTE}"


def test_selected_elements_block_lists_id_role_states_label():
    rows = [line(38, "row (selected)"), line(40, "row (selected)")]
    out = render.footer(capture(selected=rows, focused=line(27, "table items")), True)
    assert (
        out == f"Selected:\n\t38 row (selected)\n\t40 row (selected)\n\n{render.NOTE}"
    )


def test_selection_block_replaces_focused_line():
    out = render.footer(
        capture(selected=[line(1, "row")], focused=line(2, "table")), True
    )
    assert "focused" not in out


def test_most_trailer_block_three_forms():
    assert render.footer(capture(), True) == ""
    both = capture(selected_text="a", selected=[line(1, "row")], focused=line(2, "t"))
    assert render.footer(both, True).count("\n\nNote: ") == 1  # one block at most


def test_fixed_note_follows_any_selection_block_repeats_every():
    assert render.NOTE.startswith("Note: the user has selected something")
    assert render.NOTE.endswith("or something else that is visible.")


def test_trailer_spacing_blank_line_before_trailer_full_trees(monkeypatch):
    assert "\n\t43 View, ID: viewMenu\n\nThe focused UI element is 6 " in controls(
        monkeypatch
    )
    assert not render.footer(capture(focused=line(1, "x")), True).startswith("\n")


def test_second_smaller_tree_rendered_selection_block(monkeypatch):
    app = fakes_b.load("menu")
    bar = app.app.attrs["AXMenuBar"]
    app.spec["menu"] = None
    app.app.attrs["AXFocusedUIElement"] = bar.children[1]
    bar.children[1].attrs["AXSelectedChildren"] = [fakes_b.at(bar, [1, 0])]
    chosen = app.read(monkeypatch).selected
    assert [x.text for x in chosen] == [
        "menu Secondary Actions: Cancel"
    ]  # a menu keeps it


def test_internal_consistency_checks_render_tree(monkeypatch):
    read = fakes_b.load("controls").read(monkeypatch)
    assert all(x.id is None and x.element is not None for x in read.lines)


def test_three_context_kinds_model_text_history_recorder_use(monkeypatch):
    assert "[truncated to visible range]" not in controls(monkeypatch)
    assert "<app_state>" not in controls(monkeypatch)
