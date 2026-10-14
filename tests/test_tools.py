"""tools.py: the argument rules, the checks, the printing and the order of one call.

Every other module is a fake from fakes_a.py.
"""

from __future__ import annotations

import json
import shlex
import time
from dataclasses import replace

import fakes_a
import pytest
from fakes_a import RUNNING, SHOT, TARGET, WINDOW

from macos_harness import errors, helper, tools
from macos_harness.model import AgentSession, AppKind, AppTarget

ACTION_LINE = "Done. Call `get_app_state` to see the new state of the UI.\n"
SHOT_LINE = f"Screenshot (800x600 px): {SHOT.path}\n"
GATES = [
    "apps.resolve",
    "policy.decide",
    "approvals.ensure",
    "permissions.gate",
    "lockscreen.gate",
    "person.gate",
]
NO_WINDOW = "macOS Harness error 100: The app has no window\n"


@pytest.fixture
def world(monkeypatch):
    return fakes_a.install(monkeypatch)


@pytest.fixture
def active(world):
    """A world in which Demo was read once: actions are allowed."""
    assert world.run("get_app_state", "Demo")[0] == 0
    world.log.clear()
    return world


def parsed(*argv):
    return tools.parse(list(argv))


# --- the tables ------------------------------------------------------------------------


def test_the_eleven_tools_and_nothing_else():
    names = (
        "list_apps get_app_state click drag paste perform_secondary_action "
        "press_key scroll select_text set_value type_text"
    )
    assert sorted(tools.TOOLS) == sorted(names.split())
    for missing in ("launch_app", "activate", "move", "key_down", "screenshot"):
        assert missing not in tools.TOOLS
    assert "stop" not in tools.TOOLS  # the person's command, not a tool of the model


def test_every_tool_has_a_description_an_example_and_known_parameters():
    for name, tool in tools.TOOLS.items():
        assert tool.description and tool.example.startswith(f"macos-harness {name}")
        assert set(tool.params + tool.bare + tool.hidden) <= set(tools.PARAMS)
        assert tools.parse(shlex.split(tool.example)[1:])[0] == name


def test_descriptions_are_the_tool_texts():
    said = {name: tool.description for name, tool in tools.TOOLS.items()}
    assert (
        said["click"]
        == "Click one element, given by its index or by a pixel position in the screenshot"
    )
    assert said["get_app_state"].endswith(
        "Needed once in every assistant turn before any action on that app"
    )
    assert said["list_apps"].startswith("Show the apps on this Mac")
    assert "used in the past 14 days" in said["list_apps"]
    assert "add the text before or after it as prefix or suffix." in said["select_text"]
    assert "the way xdotool writes keys" in said["press_key"]
    assert tools.PARAMS["x"][1] == "Horizontal pixel position in the screenshot"
    assert tools.PARAMS["click_count"][1] == "How many clicks (default 1)"
    assert (
        tools.PARAMS["mouse_button"][1]
        == "Which mouse button: left (the default), right or middle."
    )
    assert "fractions work (default 1)" in tools.PARAMS["pages"][1]
    assert "Control_L+a, Super_L+d" in tools.PARAMS["key"][1]


def test_the_other_commands():
    commands = (
        "doctor skill install uninstall turn-started turn-ended stop resume approve "
        "telemetry help"
    )
    assert sorted(tools.COMMANDS) == sorted(commands.split())


# --- parsing: every rule of the argument table ------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        # flag names ignore case, - and _
        (["click", "--app", "X", "--element_index", "40"], {"element_index": "40"}),
        (["click", "--app", "X", "--element-index", "40"], {"element_index": "40"}),
        (["click", "--app", "X", "--elementIndex", "40"], {"element_index": "40"}),
        (["click", "--APP", "X", "--mouseButton", "right"], {"mouse_button": "right"}),
        (["click", "--app", "X", "--clickCount", "2"], {"click_count": "2"}),
        (["select_text", "X", "1", "a", "--selectionType", "cursor_before"],
         {"selection_type": "cursor_before"}),
        # other spellings
        (["get_app_state", "X", "--disableDiffing"], {"disableDiff": True}),
        (["get_app_state", "X", "--disable-diff"], {"disableDiff": True}),
        (["select_text", "X", "1", "a", "--selection", "cursor_after"],
         {"selection_type": "cursor_after"}),
        (["click", "X", "--index", "3"], {"element_index": "3"}),
        (["click", "X", "--element", "3"], {"element_index": "3"}),
        (["click", "X", "3", "--button", "r"], {"mouse_button": "r"}),
        (["click", "X", "3", "--count", "2"], {"click_count": "2"}),
        # --name value and --name=value
        (["click", "--app=X", "--element_index=40"], {"app": "X", "element_index": "40"}),
        (["type_text", "X", "--text=a=b"], {"text": "a=b"}),
        (["type_text", "X", "--text="], {"text": ""}),
        # the word after a flag is its value, also when it starts with -
        (["type_text", "--app", "X", "--text", "-5"], {"text": "-5"}),
        (["type_text", "--app", "X", "--text", "--force"], {"text": "--force"}),
        (["type_text", "--app", "X", "--text", "--json"], {"text": "--json"}),
        (["press_key", "--app", "X", "--key", "minus"], {"key": "minus"}),
        (["paste", "X", "-PASTE"], {"text": "-PASTE"}),
        # a boolean flag alone is true, and takes only true, false, 1 or 0
        (["get_app_state", "X"], {"disableDiff": False, "emit": True}),
        (["get_app_state", "X", "--disableDiff"], {"disableDiff": True}),
        (["get_app_state", "X", "--disableDiff", "true"], {"disableDiff": True}),
        (["get_app_state", "X", "--disableDiff", "false"], {"disableDiff": False}),
        (["get_app_state", "X", "--disableDiff", "0"], {"disableDiff": False}),
        (["get_app_state", "X", "--disableDiff=1"], {"disableDiff": True}),
        (["get_app_state", "--disableDiff", "X"], {"disableDiff": True, "app": "X"}),
        # emit is accepted on the two reads
        (["get_app_state", "X", "--emit", "false"], {"emit": False}),
        (["list_apps", "--emit", "false"], {"emit": False}),
        (["list_apps"], {"emit": True}),
        # one JSON object instead of flags. A number may be a string there
        (["click", '{"app":"Finder","element_index":40}'],
         {"app": "Finder", "element_index": 40}),
        (["click", '{"app": "Google Chrome", "element_index": "7", "click_count": 1}'],
         {"app": "Google Chrome", "element_index": "7", "click_count": 1}),
        (["get_app_state", '{"app":"Finder","emit":false,"disableDiffing":true}'],
         {"app": "Finder", "emit": False, "disableDiff": True}),
        (["click", '{"app":"X","elementIndex":4,"mouseButton":2}'],
         {"element_index": 4, "mouse_button": 2}),
        (["type_text", "X", '{"not":"arguments"}'], {"text": '{"not":"arguments"}'}),
        (["type_text", "{curly"], {"app": "{curly"}),
        # bare words fill the parameters in the tool's order
        (["get_app_state", "Finder"], {"app": "Finder"}),
        (["click", "X", "11"], {"app": "X", "element_index": "11"}),
        (["set_value", "X", "7", "https://example.invalid"],
         {"element_index": "7", "value": "https://example.invalid"}),
        (["type_text", "X", "pine nuts"], {"text": "pine nuts"}),
        (["press_key", "X", "Return"], {"key": "Return"}),
        (["scroll", "Slack", "338", "up", "3"],
         {"app": "Slack", "element_index": "338", "direction": "up", "pages": "3"}),
        (["select_text", "X", "5", "The model"], {"element_index": "5", "text": "The model"}),
        (["perform_secondary_action", "X", "0", "Raise"],
         {"element_index": "0", "action": "Raise"}),
        (["paste", "X", "salt"], {"text": "salt", "format": "text"}),
        (["drag", "X", "200", "850", "800", "1000"],
         {"from_x": "200", "from_y": "850", "to_x": "800", "to_y": "1000"}),
        # a bare word skips a parameter a flag gave
        (["click", "--app", "Finder", "40"], {"app": "Finder", "element_index": "40"}),
        (["scroll", "X", "--direction", "up", "338", "3"],
         {"element_index": "338", "pages": "3", "direction": "up"}),
        # and it skips element_index when a point is given
        (["scroll", "Slack", "--x", "611", "--y", "310", "up", "5"],
         {"x": "611", "y": "310", "direction": "up", "pages": "5"}),
        (["click", "X", "--x", "415", "--y", "481"], {"x": "415", "y": "481"}),
        # real call shapes
        (["click", "X", "423", "--mouseButton", "right"],
         {"element_index": "423", "mouse_button": "right"}),
        (["get_app_state", "X", "--emit", "false", "--disableDiffing", "true"],
         {"emit": False, "disableDiff": True}),
        (["paste", "X", "<p>hi</p>", "--format", "html"],
         {"text": "<p>hi</p>", "format": "html"}),
        (["get_app_state", "--app", "/Applications/Demo 2.app", "--disableDiff", "true"],
         {"app": "/Applications/Demo 2.app", "disableDiff": True}),
        (["type_text", "--app", "com.apple.Safari", "--text", "https://example.com/a"],
         {"app": "com.apple.Safari", "text": "https://example.com/a"}),
    ],
)  # fmt: skip
def test_parse(argv, expected):
    name, args = tools.parse(argv)
    assert name == argv[0]
    assert {key: args.get(key) for key in expected} == expected


def test_point_scroll_leaves_element_index_unset():
    _, args = parsed("scroll", "Slack", "--x", "611", "--y", "310", "up", "5")
    assert "element_index" not in args


@pytest.mark.parametrize("word", ["get-app-state", "getAppState", "GET_APP_STATE"])
def test_tool_names_ignore_case_dash_and_underscore(word):
    assert parsed(word, "Finder")[0] == "get_app_state"


def test_command_names_too():
    assert parsed("turn_ended")[0] == "turn-ended"


def test_defaults():
    click = parsed("click", "X", "1")[1]
    assert (click["mouse_button"], click["click_count"]) == ("left", 1)
    assert parsed("scroll", "X", "1", "up")[1]["pages"] == 1
    assert parsed("select_text", "X", "1", "a")[1]["selection_type"] == "text"
    assert parsed("paste", "X", "a")[1]["format"] == "text"


def test_json_flag_before_or_after_the_tool(world):
    for argv in (["--json", "list_apps"], ["list_apps", "--json"]):
        assert world.run(*argv) == (0, "[]\n", "")


def test_unknown_flag_names_the_flags_and_gives_one_example(world):
    code, out, err = world.run("click", "--app", "X", "--window", "2")
    lines = err.splitlines()
    assert (code, out, len(lines)) == (2, "", 2)
    assert lines[0].startswith("Unknown flag --window for click. Flags: --app, ")
    assert lines[1] == "Example: macos-harness click --app Finder --element_index 40"
    assert world.log == []
    assert "--emit" not in lines[0]  # accepted and not taught


def test_unknown_tool_names_the_tools_and_gives_one_example(world):
    code, _, err = world.run("launch_app", "Finder")
    lines = err.splitlines()
    assert code == 2 and len(lines) == 2
    assert lines[0].startswith(
        "Unknown tool launch_app. Tools: list_apps, get_app_state"
    )
    assert lines[1] == "Example: macos-harness get_app_state --app Finder"
    assert len(err) < 400  # one line and one example: no documentation dump


def test_a_word_too_many_is_a_usage_error(world):
    code, _, err = world.run("click", "X", "1", "2")
    assert code == 2 and err.startswith("Unexpected argument '2' for click.")


def test_no_tool_name_says_the_old_form_is_gone_and_never_asks_for_stdin(world):
    code, out, err = world.run()
    assert (code, out) == (2, "")
    assert err == errors.USAGE["python_stdin"] + "\n"


def test_help_lists_the_tools_and_one_tool_shows_its_flags(world):
    code, out, _ = world.run("--help")
    assert code == 0 and all(tool.example in out for tool in tools.TOOLS.values())
    assert "doctor" in out and "approve" not in out and "resume" not in out
    code, out, _ = world.run("scroll", "--help")
    assert code == 0 and "--pages: How many pages to scroll" in out
    assert out.splitlines()[-1] == tools.TOOLS["scroll"].example
    assert world.run("help", "scroll")[1] == out
    assert world.log == []  # help needs no app and no stdin


# --- stdin ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["type_text", "--app", "Demo"],
        ["type_text", "--app", "Demo", "--text", "-"],
        ["type_text", "Demo", "-"],
        ["type_text", "--app", "Demo", "--text"],
        ["paste", "--app", "Demo"],
        ["set_value", "--app", "Demo", "--element_index", "0"],
        ["set_value", "Demo", "0", "--value", "-"],
    ],
)
def test_missing_text_asks_for_stdin_before_anything_runs(world, argv):
    with pytest.raises(tools.NeedStdin):
        world.run(*argv)
    assert world.log == []


@pytest.mark.parametrize(
    ("stdin", "typed"),
    [("hello\n", "hello"), ("hello\n\n", "hello\n"), ("a\nb", "a\nb"), ("\n\n", "\n")],
)
def test_stdin_text_loses_exactly_one_newline(active, stdin, typed):
    assert active.run("type_text", "--app", "Demo", stdin=stdin)[0] == 0
    assert active.args("actions.type_text")[1] == typed


def test_stdin_feeds_value_and_paste_text(active):
    active.run("set_value", "Demo", "0", stdin="report.txt\n")
    assert active.args("actions.set_value")[2] == "report.txt"
    active.run("paste", "Demo", "--format", "md", stdin="# Title\n")
    assert active.args("actions.paste")[1:] == ("# Title", "md")


def test_a_given_text_never_asks_for_stdin(active):
    assert active.run("type_text", "Demo", "hi")[0] == 0
    assert active.run("set_value", "Demo", "0", "--value", "")[0] == 0
    assert active.args("actions.set_value")[2] == ""  # an empty value clears a field


@pytest.mark.parametrize(
    ("argv", "said"),
    [
        (["type_text", "Demo"], "No text was given to type"),
        (["type_text", "Demo", "--text", ""], "No text was given to type"),
        (["paste", "Demo"], "No text was given to paste"),
        (["set_value", "Demo", "0"], "No value was given to set"),
    ],
)
def test_no_text_at_all_is_a_tool_error(active, argv, said):
    code, _, err = active.run(*argv, stdin="")
    assert (code, err) == (1, f"macOS Harness error 100: {said}\n")
    assert not [name for name in active.names() if name.startswith("actions.")]


# --- the checks, with their texts, in order ---------------------------------------------


@pytest.mark.parametrize(
    ("argv", "text"),
    [
        (["get_app_state"], "Give the app: --app <name, bundle id or path>"),
        (["get_app_state", "--app", ""], "Give the app: --app <name, bundle id or path>"),
        (["click", "--element_index", "1"], "Give the app: --app <name, bundle id or path>"),
        (["press_key", "--key", "Return"], "Give the app: --app <name, bundle id or path>"),
        (["scroll", "--element_index", "1", "--direction", "up"], "Give the app: --app <name, bundle id or path>"),
        (["type_text", "--text", "a"], "Give the app: --app <name, bundle id or path>"),
        # a tool's own checks come before it
        (["click"], "--x and --y must both be numbers"),
        (["click", "--element_index", "x"], "--element_index takes a whole number"),
        (["click", "--element_index", "1", "--mouse_button", "side"],
         "--mouse_button takes left, right or middle (also l, r, m or 0, 1, 2)"),
        (["press_key"], "Give the key: --key <name or chord>"),
        (["scroll", "--pages", "0"], "--pages takes a number above 0"),
        (["drag"], "--from_x and --from_y must both be numbers"),
        (["set_value", "--value", "a"], "--element_index takes a whole number"),
        (["select_text", "--text", "a"], "--element_index takes a whole number"),
        (["perform_secondary_action"], "--element_index takes a whole number"),
        (["scroll", "--pixels", "100"], "scroll takes --pages here; --pixels is not supported."),
        (["click", "X"], "--x and --y must both be numbers"),
        (["click", "X", "--x", "5"], "--x and --y must both be numbers"),
        (["click", "X", "--x", "nan", "--y", "1"],
         "--x and --y must both be numbers"),
        (["click", "X", "1.5"], "--element_index takes a whole number"),
        (["click", "X", "four"], "--element_index takes a whole number"),
        (["click", '{"app":"X","element_index":true}'], "--element_index takes a whole number"),
        (["click", "X", "1", "--mouse_button", "side"],
         "--mouse_button takes left, right or middle (also l, r, m or 0, 1, 2)"),
        (["click", "X", "1", "--mouse_button", "5"],
         "--mouse_button takes left, right or middle (also l, r, m or 0, 1, 2)"),
        (["click", '{"app":"X","element_index":1,"mouse_button":5}'],
         "--mouse_button as a number takes 0, 1 or 2"),
        (["click", "X", "1", "--duration", "long"], "--duration takes a number of seconds"),
        (["click", "X", "1", "--duration", "inf"], "--duration takes a number of seconds"),
        (["click", "X", "1", "--duration", "30.001"], "--duration is not a usable time"),
        (["click", "X", "1", "--duration", "-1"], "--duration is not a usable time"),
        (["click", "X", "1", "--duration_ms", "30001"], "--duration is not a usable time"),
        (["click", "X", "1", "--duration_ms", "-1"], "--duration is not a usable time"),
        (["click", "X", "1", "--duration_ms", "long"], "--duration is not a usable time"),
        (["drag", "X", "1"], "--from_x and --from_y must both be numbers"),
        (["drag", "X", "1", "2"], "--to_x and --to_y must both be numbers"),
        (["drag", "X", "1", "2", "3"], "--to_x and --to_y must both be numbers"),
        (["drag", "X", "1", "2", "inf", "4"], "--to_x and --to_y must both be numbers"),
        (["press_key", "X"], "Give the key: --key <name or chord>"),
        (["press_key", "X", "--key", "  "], "Give the key: --key <name or chord>"),
        (["press_key", "X", "a", "--duration_ms", "99999"], "--duration is not a usable time"),
        (["press_key", "X", "a", "--duration", "nan"], "--duration takes a number of seconds"),
        (["set_value", "X", "--value", "a"], "--element_index takes a whole number"),
        # scroll: pixels, then pages, then the target, then the direction
        (["scroll", "X", "--pixels", "9", "--pages", "0"],
         "scroll takes --pages here; --pixels is not supported."),
        (["scroll", "X", "--pages", "0"], "--pages takes a number above 0"),
        (["scroll", "X", "--pages", "-1", "--direction", "sideways"],
         "--pages takes a number above 0"),
        (["scroll", "X", "--pages", "many", "1", "up"], "--pages takes a number above 0"),
        (["scroll", "X", "--direction", "sideways"],
         "--x and --y must both be numbers"),
        (["scroll", "X", "x1", "up"], "--element_index takes a whole number"),
        (["scroll", "X", "1", "sideways"], "--direction takes up, down, left or right"),
        (["scroll", "X", "1"], "--direction takes up, down, left or right"),
        (["select_text", "X", "--text", "a"], "--element_index takes a whole number"),
        (["select_text", "X", "1", "a", "--selection_type", "word"],
         "--selection_type cannot be word. Use text, cursor_before or cursor_after."),
        (["perform_secondary_action", "X", "--action", "Raise"],
         "--element_index takes a whole number"),
    ],
)  # fmt: skip
def test_check_texts(world, argv, text):
    code, out, err = world.run(*argv)
    assert (code, out, err) == (2, "", text + "\n")
    assert world.log == []  # a bad call touches no app


def test_every_check_text_comes_from_the_shared_table():
    import inspect

    source = inspect.getsource(tools)
    for text in errors.USAGE.values():
        fixed = text.split("{")[0]
        assert len(fixed) < 8 or fixed not in source


@pytest.mark.parametrize(
    ("value", "button"),
    [
        ("left", 0),
        ("l", 0),
        ("0", 0),
        (" Right ", 1),
        ("R", 1),
        ("m", 2),
        ("MIDDLE", 2),
    ],
)
def test_mouse_buttons(active, value, button):
    assert active.run("click", "Demo", "0", "--mouse_button", value)[0] == 0
    assert active.args("actions.click")[2] == button


@pytest.mark.parametrize(
    ("word", "direction"),
    [("u", "up"), ("D", "down"), (" left ", "left"), ("r", "right"), ("Up", "up")],
)
def test_directions(active, word, direction):
    assert active.run("scroll", "Demo", "0", word)[0] == 0
    assert active.args("actions.scroll")[2:] == (direction, 1.0)


def test_fractional_pages_pass(active):
    assert active.run("scroll", "Demo", "0", "down", "0.65")[0] == 0
    assert active.args("actions.scroll")[3] == 0.65


def test_click_defaults_and_options(active):
    active.run("click", "Demo", "0")
    line = active.args("actions.click")[1]
    assert active.args("actions.click")[2:] == (0, 1, None, None)
    assert (line.id, line.text) == (0, "button OK")
    active.run(
        "click", "Demo", "0", "--count", "2", "--key", "shift", "--duration", "0.25"
    )
    assert active.args("actions.click")[2:] == (0, 2, "shift", 0.25)
    active.run("click", "Demo", "0", "--duration_ms", "250")  # the older name
    assert active.args("actions.click")[5] == 0.25


def test_click_count_is_not_checked_here(active):
    assert active.run("click", "Demo", "0", "--click_count", "0")[0] == 0
    assert active.args("actions.click")[3] == 0  # the action fails a count below 1


def test_an_element_number_wins_over_a_point_without_a_word(active):
    argv = ["click", "--app", "Demo", "--element_index", "0", "--x", "9", "--y", "9"]
    assert active.run(*argv)[0] == 0
    assert active.args("actions.click")[1].id == 0


def test_json_number_as_text_reaches_the_tool_as_a_number(active):
    active.run("click", '{"app":"Demo","element_index":"0","click_count":"2"}')
    assert active.args("revisions.resolve") == (0,)
    assert active.args("actions.click")[3] == 2


def test_the_other_tools_pass_their_arguments_on(active):
    active.run("press_key", "Demo", "Down Down Return", "--duration", "0.1")
    assert active.args("actions.press_key")[1:] == ("Down Down Return", 0.1)
    active.run("press_key", "Demo", "a", "--duration_ms", "100")
    assert active.args("actions.press_key")[1:] == ("a", 0.1)
    active.run("set_value", "Demo", "0", "42")
    assert active.args("actions.set_value")[2] == "42"  # always text
    active.run(
        "select_text",
        "Demo",
        "0",
        "abc",
        "--prefix",
        "x",
        "--selection",
        "cursor_after",
    )
    assert active.args("actions.select_text")[2:] == ("abc", "x", "", "cursor_after")
    active.run("perform_secondary_action", "Demo", "0", "Raise")
    assert active.args("actions.secondary")[2] == "Raise"
    active.run("paste", "Demo", "salt")
    assert active.args("actions.paste")[1:] == ("salt", "text")
    active.run("type_text", "Demo", "pine nuts")
    assert active.args("actions.type_text")[1] == "pine nuts"


# --- what is printed --------------------------------------------------------------------


def test_state_read_prints_text_blank_line_screenshot_line(world):
    code, out, err = world.run("get_app_state", "--app", "Demo")
    assert (code, err) == (0, "")
    assert out == 'Window: "Demo", App: Demo.\n0 button OK\n\n' + SHOT_LINE


def test_instruction_block_goes_before_the_state_text(world):
    world.instructions.answers["prefix"] = "<notes>\nBe careful.\n</notes>\n"
    out = world.run("get_app_state", "Demo")[1]
    assert out.startswith('<notes>\nBe careful.\n</notes>\nWindow: "Demo"')
    assert world.args("instructions.prefix") == (world.agent, "com.example.demo")


def test_keep_prints_the_kept_copy_of_the_screenshot(world):
    """Proof of work: --keep copies the picture where it is never pruned."""
    out = world.run("get_app_state", "Demo", "--keep")[1]
    assert out.endswith(
        "Screenshot (800x600 px): /Users/x/.macos-harness/proof/Demo-x7k2qd.jpeg\n"
    )
    assert world.args("screenshot.keep") == (SHOT.path, "Demo")
    assert world.run("get_app_state", "Demo")[1].endswith(SHOT_LINE)  # off by default


def test_emit_false_prints_only_the_screenshot_line_and_still_moves_the_base(world):
    assert world.run("get_app_state", "Demo", "--emit", "false") == (0, SHOT_LINE, "")
    assert "revisions.present" in world.names()
    # the next read counts on from the one nobody saw
    assert "\n1 button OK\n" in world.run("get_app_state", "Demo")[1]


def test_state_read_as_json(world):
    code, out, _ = world.run("get_app_state", "Demo", "--json")
    value = json.loads(out)
    assert code == 0 and out.endswith("\n")
    assert value == {
        "app": "Demo",
        "screenshot": {"url": "file:///tmp/shots/Demo%20Screenshot%20x7k2qd.jpeg"},
        "text": 'Window: "Demo", App: Demo.\n0 button OK\n',
    }


def test_list_apps_prints_a_json_array_and_passes_no_gate(world):
    rows = [{"id": "com.example.demo", "displayName": "Demo", "isRunning": True}]
    world.apps.answers["list_apps"] = rows
    expected = '[{"id":"com.example.demo","displayName":"Demo","isRunning":true}]\n'
    assert world.run("list_apps") == (0, expected, "")
    assert world.names() == ["apps.list_apps"]
    assert world.args("apps.list_apps") == (world.agent.hosts,)
    assert json.loads(world.run("list_apps", "--json")[1]) == rows
    assert world.run("list_apps", "--emit", "false") == (0, "", "")


def test_an_action_prints_the_fixed_sentence_and_null_as_json(active):
    assert active.run("click", "Demo", "0") == (0, ACTION_LINE, "")
    assert active.run("click", "Demo", "0", "--json") == (0, "null\n", "")


def test_exit_codes_and_the_error_object(world):
    world.apps.answers["resolve"] = errors.invalid_app("Nope")
    assert world.run("get_app_state", "Nope") == (1, "", "Invalid app: Nope\n")
    code, out, err = world.run("get_app_state", "Nope", "--json")
    expected = {"code": 130, "name": "no_such_app", "message": "Invalid app: Nope"}
    assert (code, err) == (1, "")
    assert json.loads(out) == {"error": {**expected, "cancelled": False}}
    code, out, _ = world.run("get_app_state", "--json")
    assert (
        code == 2
        and json.loads(out)["error"]["message"]
        == "Give the app: --app <name, bundle id or path>"
    )


def test_the_error_object_carries_cancelled_and_tag(active):
    active.person.answers["gate"] = errors.user_changed("/Applications/Demo.app")
    error = json.loads(active.run("click", "Demo", "0", "--json")[1])["error"]
    assert (error["name"], error["cancelled"]) == ("user_took_over", True)
    active.person.answers["gate"] = None
    error = json.loads(active.run("click", "Demo", "99", "--json")[1])["error"]
    assert error["tag"] == "invalid_element_id"
    assert error["message"].endswith("100: No element has the ID 99")


def test_an_unexpected_exception_becomes_a_server_error(active):
    active.actions.answers["click"] = RuntimeError("boom")
    code, _, err = active.run("click", "Demo", "0")
    assert (code, err) == (1, "macOS Harness error 100: RuntimeError: boom\n")


def test_a_tree_read_that_breaks_says_the_capture_failed(world):
    """It has a text of its own."""
    world.tree.answers["read"] = RuntimeError("boom")
    code, _, err = world.run("get_app_state", "Demo")
    assert (code, err) == (
        1,
        "macOS Harness error 100: Reading the UI tree failed.\n",
    )


def test_one_result_ends_with_one_newline(world):
    for argv in (["get_app_state", "Demo"], ["click", "Demo", "0"], ["skill"]):
        out = world.run(*argv)[1]
        assert out.endswith("\n") and not out.endswith("\n\n")


def test_the_whole_state_text_is_printed_however_long(world):
    texts = [f"button {n}" for n in range(5000)]
    world.tree.answers["read"] = lambda *a, **k: fakes_a.capture(*texts)
    out = world.run("get_app_state", "Demo")[1]
    assert "\n4999 button 4999\n" in out and len(out) > 60000


# --- the order of one call --------------------------------------------------------------

READ = [
    *GATES,
    "awake.acquire",
    "person.gate",  # again after the wait for the app's queue
    "apps.running",
    "apps.kind",
    "tree.enable",
    "windows.watch",
    "revisions.watch",
    "windows.key_window",  # is there a window?
    "windows.key_window",
    "windows.open_menu",
    "tree.read",
    "policy.check_url",
    "windows.key_window",
    "windows.extra_windows",
    "screenshot.capture",
    "revisions.present",
    "instructions.prefix",
    "person.after_state_read",
    "presence.halo",
    "presence.live_view",
    "presence.pointer_activity",
    "awake.release",
]


def test_state_read_order(world):
    assert world.run("get_app_state", "Demo")[0] == 0
    assert world.names() == READ
    assert world.args("person.gate") == (world.agent, TARGET, True)
    assert world.args("lockscreen.gate") == (True,)
    assert world.args("approvals.ensure")[3] == world.deadline
    assert world.args("permissions.gate") == (world.deadline,)
    assert world.args("tree.enable") == (RUNNING, AppKind())
    assert world.args("screenshot.capture") == (WINDOW, ())
    assert world.args("presence.pointer_activity") == ("loading",)
    assert "settle.wait" not in world.names()  # nothing to wait for: no observer made


def test_an_open_menu_or_sheet_goes_into_the_picture(world):
    world.windows.answers["extra_windows"] = [{"id": 9, "frame": (1.0, 2.0, 3.0, 4.0)}]
    world.run("get_app_state", "Demo")
    assert world.args("windows.extra_windows") == (RUNNING, WINDOW.window_id)
    assert world.args("screenshot.capture") == (WINDOW, ((9, (1.0, 2.0, 3.0, 4.0)),))


def test_second_read_switches_nothing_on_again(active):
    active.run("get_app_state", "Demo")
    assert not {"tree.enable", "windows.watch", "revisions.watch"} & set(active.names())


def test_action_order(active):
    assert active.run("click", "Demo", "0")[0] == 0
    assert active.names() == [
        *GATES,
        "awake.acquire",
        "person.gate",  # again after the wait for the app's queue
        "apps.running",
        "apps.kind",
        "revisions.resolve",
        "presence.pointer_activity",
        "presence.label",
        "actions.click",
        "presence.label",
        "presence.pointer_activity",
        "awake.release",
    ]
    assert active.args("person.gate") == (active.agent, TARGET, False)
    assert active.args("lockscreen.gate") == (False,)
    labels = [args[0] for name, args, _ in active.log if name == "presence.label"]
    assert labels == ["Clicking in Demo", "Clicked in Demo"]
    states = [a[0] for name, a, _ in active.log if name == "presence.pointer_activity"]
    assert states == ["idle", "loading"]


@pytest.mark.parametrize(
    ("module", "function", "error", "reached"),
    [
        ("apps", "resolve", errors.invalid_app("Demo"), 1),
        ("policy", "decide", errors.forbidden("com.example.demo"), 2),
        ("approvals", "ensure", errors.not_approved("Demo"), 3),
        ("permissions", "gate", errors.permissions_pending("Terminal", "X"), 4),
        ("lockscreen", "gate", errors.locked(state_read=True), 5),
        ("person", "gate", errors.stopped_by_user(), 6),
    ],
)
def test_a_failed_gate_ends_the_call_there(world, module, function, error, reached):
    getattr(world, module).answers[function] = error
    assert world.run("get_app_state", "Demo") == (1, "", error.message + "\n")
    assert world.names() == GATES[:reached]  # nothing after it ran


def test_a_forbidden_app_never_reaches_approval_and_a_locked_mac_no_accessibility(
    world,
):
    world.policy.answers["decide"] = errors.forbidden("com.example.demo")
    world.run("get_app_state", "Demo")
    assert "approvals.ensure" not in world.names()
    world.log.clear()
    world.policy.answers["decide"] = fakes_a.CONTRACT["policy"]["decide"]
    world.lockscreen.answers["gate"] = errors.locked(state_read=True)
    world.run("get_app_state", "Demo")
    touched = {name.split(".")[0] for name in world.names()}
    assert not touched & {"tree", "windows", "screenshot", "revisions", "settle"}


def test_a_failed_first_read_leaves_the_app_not_active(world):
    world.tree.answers["read"] = errors.capture_failed()
    assert world.run("get_app_state", "Demo")[0] == 1
    assert world.run("click", "Demo", "0")[2].startswith("No session is open for")


def test_a_failed_screenshot_still_prints_the_tree(world):
    world.screenshot.answers["capture"] = errors.screenshot_failed()
    said = "macOS Harness error 100: Taking the screenshot failed."
    code, out, _ = world.run("get_app_state", "Demo")
    tree = 'Window: "Demo", App: Demo.\n0 button OK\n'
    assert code == 0 and out == f"{tree}\nScreenshot: none. {said}\n"
    shown = json.loads(world.run("get_app_state", "Demo", "--json")[1])
    assert shown["screenshot"] is None and shown["text"].startswith("Window:")
    # An element number still works. A point has no picture to be a point of.
    assert world.run("click", "Demo", "1")[0] == 0
    assert world.run("click", "Demo", "--x", "1", "--y", "1") == (1, "", said + "\n")
    assert "actions.drag" not in world.names()


def test_keys_with_nothing_pending_do_not_wait(active):
    assert active.run("press_key", "Demo", "Return")[0] == 0
    assert "settle.wait" not in active.names()


def test_a_failed_app_list_says_which_list(world):
    world.apps.answers["list_apps"] = errors.server_error("catalog unavailable")
    code, _, err = world.run("list_apps")
    assert code == 1
    assert err == "Native apps: macOS Harness error 100: catalog unavailable\n"


def test_in_a_browser_the_page_is_checked_again_before_the_next_action(active):
    """The chain the skill teaches: set the address, type, press Return, then read. On
    a blocked page the typing and the submit ran before the read noticed. Now the
    action after one that may have changed the page reads the address first."""
    active.apps.answers["kind"] = AppKind(http=True)
    active.policy.answers["watches_urls"] = True
    assert active.run("click", "Demo", "0")[0] == 0  # may have opened another page
    active.log.clear()
    active.policy.answers["check_url"] = errors.blocked_url()
    code, _, err = active.run("type_text", "Demo", "secret")
    assert code == 1 and "may not be used on the page" in err
    assert "actions.type_text" not in active.names()
    assert active.names().index("tree.read") < active.names().index("policy.check_url")
    assert active.run("click", "Demo", "0")[0] == 1  # the session has ended


def test_the_page_check_waits_for_the_page_and_follows_a_failed_action_too(active):
    """Right after Return in the address field the address was still the old one, and
    only keys waited for the page: a click went to the new page unchecked. And an
    action that sent its events and then failed left the page unmarked."""
    active.apps.answers["kind"] = AppKind(http=True)
    active.policy.answers["watches_urls"] = True
    active.actions.answers["press_key"] = errors.user_changed(TARGET.path)
    assert active.run("press_key", "Demo", "Return")[0] == 1  # sent, then cut short
    active.log.clear()
    active.settle.answers["wait"] = lambda *a, **k: fakes_a.capture("button Late")
    assert active.run("click", "Demo", "0")[0] == 0
    names = active.names()
    assert (
        names.index("settle.wait")
        < names.index("policy.check_url")
        < names.index("actions.click")
    )
    assert "tree.read" not in names  # the settled read is the one that is checked


def test_no_page_is_read_before_an_action_when_nothing_is_blocked(active):
    active.apps.answers["kind"] = AppKind(http=True)
    active.run("click", "Demo", "0")
    active.log.clear()
    assert active.run("click", "Demo", "0")[0] == 0
    assert (
        "tree.read" not in active.names() and "policy.check_url" not in active.names()
    )


def test_no_action_starts_after_the_client_gave_up(active):
    """A call queued behind another, or five clicks held 30 s each: the client printed
    "request timed out", the helper did it later, and the model did it again."""
    active.deadline = time.time() + 0.5  # the client is about to give up
    code, _, err = active.run("click", "Demo", "0")
    assert (code, err) == (1, "macOS Harness did not answer in time\n")
    assert "actions.click" not in active.names()
    active.deadline = time.time() + 120
    code, _, err = active.run(
        "click", "Demo", "0", "--click_count", "5", "--duration", "30"
    )
    assert (code, err.splitlines()[0]) == (2, "--duration is not a usable time")
    assert (
        active.run("click", "Demo", "0", "--click_count", "3", "--duration", "10")[0]
        == 0
    )


def test_no_action_starts_that_would_end_after_the_client_gave_up(active, monkeypatch):
    """The margin was 1 s whatever the action took: a 30 s hold started 2 s before
    the limit ended 28 s after the client printed "request timed out"."""
    timed_out = (1, "macOS Harness did not answer in time\n")
    active.deadline = time.time() + 20
    for call in (
        ["press_key", "Demo", "a", "--duration", "25"],
        ["click", "Demo", "0", "--click_count", "3", "--duration", "10"],
    ):
        code, _, err = active.run(*call)
        assert (code, err) == timed_out and "actions." + call[0] not in active.names()
    assert active.run("press_key", "Demo", "a", "--duration", "15")[0] == 0
    active.deadline = time.time() + 5
    assert active.run("type_text", "Demo", "x" * 20000)[::2] == timed_out  # about 10 s
    assert active.run("type_text", "Demo", "x" * 2000)[0] == 0  # about 1 s


def test_text_for_an_app_that_is_typed_into_slowly_is_capped_by_its_time(
    active, monkeypatch
):
    """20000 characters at that app's delay are about 200 s, over the client's limit
    by themselves."""
    monkeypatch.setitem(tools.SLOW_TYPING, TARGET.bundle_id, 0.005)
    code, _, err = active.run("type_text", "Demo", "x" * 3001)
    assert code == 2 and err.startswith(
        "text is longer than 3000 characters. Use paste"
    )
    assert "actions.type_text" not in active.names()
    assert active.run("type_text", "Demo", "x" * 3000)[0] == 0


def test_the_person_gate_is_asked_again_after_the_wait_for_the_queue(active):
    asked = []
    refusal = errors.user_changed(TARGET.path)

    def gate(*args, **kwargs):
        asked.append(args)
        if len(asked) > 1:  # the person took the app while the call waited in line
            raise refusal

    active.person.answers["gate"] = gate
    code, _, err = active.run("click", "Demo", "0")
    assert (code, len(asked)) == (1, 2) and err == refusal.message + "\n"
    assert "actions.click" not in active.names()


def test_an_action_before_a_state_read_is_not_active(world):
    code, _, err = world.run("click", "Demo", "0")
    assert code == 1
    assert err == errors.not_active("/Applications/Demo.app").message + "\n"
    assert world.names() == [*GATES, "awake.acquire", "person.gate", "awake.release"]


def test_every_action_sets_the_settle_flag_and_none_waits(active):
    app = helper.SESSIONS.app(active.agent, TARGET)
    assert (app.active, app.needs_settle) == (True, False)
    active.run("click", "Demo", "0")
    assert app.needs_settle is True
    assert "settle.wait" not in active.names()


def test_read_after_an_action_clears_the_flag_before_the_settle_starts(active):
    app = helper.SESSIONS.app(active.agent, TARGET)
    seen = []
    settled = fakes_a.capture("button Settled")

    def wait(target, kind, read, poll=None):
        seen.append((app.needs_settle, poll))
        return settled

    active.settle.answers["wait"] = wait
    active.run("click", "Demo", "0")
    out = active.run("get_app_state", "Demo")[1]
    assert seen == [(False, None)]
    assert "button Settled" in out  # the tree the wait built is the tree printed
    assert active.names().count("tree.read") == 0
    assert app.needs_settle is False


def test_settle_that_times_out_falls_back_to_one_plain_read(active):
    active.run("click", "Demo", "0")
    active.log.clear()
    assert active.run("get_app_state", "Demo")[0] == 0
    names = active.names()
    assert names.index("settle.wait") < names.index("tree.read")


def test_press_key_after_a_click_settles_first_and_a_click_does_not(active):
    active.run("click", "Demo", "0")
    active.log.clear()
    active.run("click", "Demo", "0")
    assert "settle.wait" not in active.names()
    active.log.clear()
    active.run("press_key", "Demo", "Return")
    names = active.names()
    assert names.index("settle.wait") < names.index("actions.press_key")
    assert active.args("settle.wait")[:2] == (RUNNING, AppKind())
    active.log.clear()
    active.run("type_text", "Demo", "a")  # press_key set the flag again
    assert "settle.wait" in active.names()


def test_the_settle_before_keys_can_be_switched_off(active, monkeypatch):
    monkeypatch.setattr(tools, "SETTLE_BEFORE_KEYS", ())
    active.run("click", "Demo", "0")
    active.run("press_key", "Demo", "Return")
    assert "settle.wait" not in active.names()


def test_the_open_menu_reaches_the_tree_read(world):
    world.windows.answers["open_menu"] = "the menu"
    world.run("get_app_state", "Demo")
    _, args, kwargs = next(call for call in world.log if call[0] == "tree.read")
    assert args == (RUNNING, WINDOW, AppKind()) and kwargs == {"menu": "the menu"}


def test_a_point_is_converted_with_the_last_screenshot(active):
    active.run("click", "Demo", "--x", "200", "--y", "100")
    assert active.args("actions.click")[1] == (
        200.0,
        100.0,
    )  # 200 / 2 + 100, 100 / 2 + 50
    active.run("scroll", "Demo", "--x", "0", "--y", "0", "down", "2")
    assert active.args("actions.scroll")[1:] == ((100.0, 50.0), "down", 2.0)
    active.run("drag", "Demo", "0", "0", "800", "600")
    assert active.args("actions.drag")[1:] == ((100.0, 50.0), (500.0, 350.0))
    assert "revisions.resolve" not in active.names()


def test_refetch_may_ignore_the_value_only_for_set_value(active):
    active.run("set_value", "Demo", "0", "x")
    active.run("click", "Demo", "0")
    kwargs = [kw for name, _, kw in active.log if name == "revisions.resolve"]
    assert kwargs == [{"ignore_value": True}, {"ignore_value": False}]


def test_an_app_that_is_not_running_is_launched_and_settled_fast(world):
    world.apps.answers["running"] = errors.app_not_running("/Applications/Demo.app")
    assert world.run("get_app_state", "Demo")[0] == 0
    names = world.names()
    assert (
        names.index("apps.running")
        < names.index("apps.launch")
        < names.index("tree.enable")
    )
    assert world.log[names.index("settle.wait")][2] == {"poll": 0.05}


def test_the_read_that_switched_the_app_on_asks_the_tree_first(world):
    # A web view's page joins the tree some ms after it was first asked (measured: 38).
    world.tree.answers["enable"] = True
    assert world.run("get_app_state", "Demo")[0] == 0
    names = world.names()
    assert names.index("tree.ask_once") < names.index("tree.read")
    assert "settle.wait" not in names


def test_the_read_that_switched_a_chromium_app_on_waits_for_its_tree(world):
    """Electron, 13 Oct: read right after the switch, the page had no URL yet and its
    iframe was missing; a read after the wait had both."""
    world.tree.answers["enable"] = True
    for kind in (AppKind(electron=True), AppKind(chromium=True)):
        world.apps.answers["kind"] = kind
        world.log.clear()
        assert world.run("get_app_state", "Demo")[0] == 0
        names = world.names()
        assert names.index("tree.ask_once") < names.index("settle.wait")
        world.run("turn-ended")


def test_a_later_read_does_not_ask_first(active):
    active.run("get_app_state", "Demo")
    assert "tree.ask_once" not in active.names()


def test_the_panel_service_is_looked_up_among_the_apps_the_agent_reads(world):
    world.apps.answers["resolve"] = AppTarget(
        "/System/Panel.xpc", "com.apple.appkit.xpc.openAndSavePanelService", "Panel"
    )
    world.apps.answers["panel_owner"] = lambda candidates: TARGET
    assert (
        world.run("get_app_state", "com.apple.appkit.xpc.openAndSavePanelService")[0]
        == 0
    )
    assert world.args("apps.panel_owner") == ([],)  # this agent reads nothing yet
    assert world.args("policy.decide")[1] == TARGET  # the app's own approval and policy
    world.apps.answers["resolve"] = TARGET
    world.run("get_app_state", "Demo")
    assert world.names().count("apps.panel_owner") == 1  # only for the service


def test_the_apps_the_agent_reads_are_the_only_candidates(active):
    active.apps.answers["resolve"] = AppTarget(
        "/System/Panel.xpc", "com.apple.appkit.xpc.openAndSavePanelService", "Panel"
    )
    active.apps.answers["panel_owner"] = lambda candidates: RUNNING
    active.run("get_app_state", "com.apple.appkit.xpc.openAndSavePanelService")
    assert active.args("apps.panel_owner") == ([RUNNING],)  # the app it read


def test_launch_that_shows_no_window_says_timeout(world):
    world.apps.answers["running"] = errors.app_not_running("/Applications/Demo.app")
    world.apps.answers["launch"] = errors.timeout_reached()
    code, _, err = world.run("get_app_state", "Demo")
    assert (code, err) == (
        1,
        "macOS Harness error 100: The app opened no window in time\n",
    )


def test_running_app_with_no_window_waits_the_settle_time_then_says_so(world):
    world.windows.answers["key_window"] = errors.no_windows()
    code, _, err = world.run("get_app_state", "Demo")
    assert (code, err) == (1, NO_WINDOW)
    names = world.names()
    assert names.count("settle.wait") == 1 and "tree.read" not in names
    assert world.log[names.index("settle.wait")][2] == {"poll": 0.05}
    assert names[-2:] == [
        "windows.key_window",
        "awake.release",
    ]  # raised after the wait


def test_a_window_that_comes_during_the_wait_is_read(world):
    answers = iter([errors.no_windows(), WINDOW, WINDOW])

    def key_window(target):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    world.windows.answers["key_window"] = key_window
    world.settle.answers["wait"] = lambda *a, **k: fakes_a.capture("button Late")
    assert "0 button Late" in world.run("get_app_state", "Demo")[1]


def test_another_window_error_is_not_taken_for_no_window(world):
    world.windows.answers["key_window"] = errors.accessibility(
        "AXError.attributeUnsupported"
    )
    code, _, err = world.run("get_app_state", "Demo")
    assert (code, err) == (
        1,
        "The accessibility call failed: AXError.attributeUnsupported\n",
    )
    assert "settle.wait" not in world.names()


def test_an_app_that_does_not_answer_is_named_not_responding(world):
    # A frozen app (seen: TextEdit stuck in its own save) answers every call with
    # cannotComplete after the system's own limit. Said plainly, an agent stops instead of retrying.
    world.windows.answers["key_window"] = errors.accessibility("AXError.cannotComplete")
    code, _, err = world.run("get_app_state", "Demo")
    assert code == 1 and err.startswith("Demo is not responding: it did not answer")


def test_blocked_url_ends_the_read_before_the_screenshot(world):
    world.tree.answers["read"] = lambda *a, **k: replace(
        fakes_a.capture(), url="bad.example"
    )
    world.policy.answers["check_url"] = errors.blocked_url()
    assert world.run("get_app_state", "Demo")[0] == 1
    assert world.args("policy.check_url") == ("bad.example",)
    assert "screenshot.capture" not in world.names()


def test_a_blocked_url_ends_the_session(active):
    """The page the model was driving turned into a blocked one: no action after it."""
    active.policy.answers["check_url"] = errors.blocked_url()
    assert active.run("get_app_state", "Demo")[0] == 1
    assert active.run("click", "Demo", "0")[2].startswith("No session is open for")


def test_disable_diff_from_the_flag_or_from_the_environment(world):
    world.run("get_app_state", "Demo")
    world.run("get_app_state", "Demo", "--disableDiff")
    world.agent.env = {"MACOS_HARNESS_DIFF": "0"}
    world.run("get_app_state", "Demo")
    world.agent.env = {"MACOS_HARNESS_DIFF": "1"}
    world.run("get_app_state", "Demo")
    flags = [args[1] for name, args, _ in world.log if name == "revisions.present"]
    assert flags == [False, True, True, False]


def test_two_agents_get_separate_numbers_for_one_app(world):
    other = AgentSession("agent-2")
    world.run("get_app_state", "Demo")
    assert "\n1 button OK\n" in world.run("get_app_state", "Demo")[1]
    assert "\n0 button OK\n" in world.run("get_app_state", "Demo", agent=other)[1]
    assert world.run("click", "Demo", "1")[0] == 0
    assert (
        world.run("click", "Demo", "1", agent=other)[0] == 1
    )  # not a number of agent 2


def test_one_agent_reading_does_not_make_another_active(world):
    world.run("get_app_state", "Demo")
    assert world.run("click", "Demo", "0", agent=AgentSession("agent-2"))[0] == 1


def test_the_cancel_of_the_session_asks_whether_the_person_took_over(active):
    """A stop, or the person's own input in the app since this call began."""
    before = time.monotonic()
    active.run("click", "Demo", "0")
    app = helper.SESSIONS.app(active.agent, TARGET)
    active.person.answers["interrupted"] = True
    assert app.cancel() is True
    target, since = active.args("person.interrupted")
    assert target == TARGET and before <= since <= time.monotonic()


def test_one_call_clicks_at_most_ten_times_and_types_at_most_20000_characters(active):
    """A count of 100000 ran for hours, a 200 KB text for minutes, and neither could
    be stopped. More text goes through paste."""
    code, _, err = active.run("click", "Demo", "0", "--click_count", "11")
    assert (code, err.splitlines()[0]) == (2, "clickCount must be 10 or less")
    assert active.run("click", "Demo", "0", "--click_count", "10")[0] == 0
    code, _, err = active.run("type_text", "Demo", "x" * 20001)
    assert code == 2 and err.startswith(
        "text is longer than 20000 characters. Use paste"
    )
    assert active.run("type_text", "Demo", "x" * 20000)[0] == 0


def test_an_action_on_an_app_that_quit_says_so_and_ends_the_session(active):
    gone = errors.app_not_running(TARGET.path)
    active.apps.answers["running"] = gone
    assert active.run("click", "Demo", "0") == (1, "", gone.message + "\n")
    assert "actions.click" not in active.names()
    active.apps.answers["running"] = RUNNING  # it is back: the old numbers are not
    assert active.run("click", "Demo", "0")[2].startswith("No session is open for")


def test_an_action_on_an_app_that_is_a_new_process_needs_a_state_read(active):
    active.apps.answers["running"] = replace(RUNNING, pid=5151)
    assert active.run("click", "Demo", "0")[2].startswith("No session is open for")
    assert "actions.click" not in active.names()


def test_an_app_that_came_back_as_a_new_process_is_switched_on_again(active):
    again = replace(RUNNING, pid=5151)
    active.apps.answers["running"] = again
    assert "\n0 button OK\n" in active.run("get_app_state", "Demo")[1]  # counts from 0
    switches = ("focus.", "tree.enable", "tree.disable", "windows.watch")
    switches += ("windows.unwatch", "revisions.reset", "revisions.watch")
    names = [name for name in active.names() if name.startswith(switches)]
    assert names == [
        "focus.release",
        "tree.disable",
        "windows.unwatch",
        "revisions.reset",
        "tree.enable",
        "windows.watch",
        "revisions.watch",
    ]
    assert (
        active.args("tree.disable") == (RUNNING,)
        and active.args("tree.enable")[0] == again
    )


# --- the other commands -----------------------------------------------------------------


def test_turn_ended_lets_go_in_order(active):
    assert active.run("turn-ended") == (0, "", "")
    assert active.names() == [
        "person.turn_ended",
        "awake.acquire",
        "actions.cancel_menu",  # a menu left open would take the person's next keys
        "focus.release",
        "tree.disable",
        "windows.unwatch",
        "revisions.reset",
        "awake.release",
        "person.driven",
        "presence.turn_done",
    ]
    assert active.args("focus.release") == (RUNNING,)
    assert helper.SESSIONS.apps(active.agent) == []  # dropped before turn_done
    assert active.run("click", "Demo", "0")[0] == 1  # not active any more


def test_one_agent_s_turn_end_leaves_the_menu_rows_of_another_agent(active):
    """Two agents drive apps. The first one's turn ended, and the menu bar lost the
    other's "Stop Using ..." rows, and the pointer faded."""
    active.person.answers["driven"] = [RUNNING]  # someone still drives an app
    active.run("turn-ended")
    assert "presence.turn_done" not in active.names()
    active.person.answers["driven"] = []
    active.run("turn-ended")
    assert active.names()[-1] == "presence.turn_done"


def test_turn_ended_goes_on_when_one_step_fails(active):
    active.focus.answers["release"] = RuntimeError("the app is gone")
    assert active.run("turn-ended")[0] == 0
    assert active.names()[-4:] == [
        "revisions.reset",
        "awake.release",
        "person.driven",
        "presence.turn_done",
    ]


def test_turn_ended_with_a_session_that_never_read_switches_nothing_off(world):
    world.run("click", "Demo", "0")  # not active, but the session exists now
    world.log.clear()
    world.run("turn-ended")
    assert world.names() == [
        "person.turn_ended",
        "awake.acquire",
        "awake.release",
        "person.driven",
        "presence.turn_done",
    ]


def test_turn_started(world):
    assert world.run("turn-started") == (0, "", "")
    assert world.log == [("person.turn_started", (world.agent,), {})]


def test_an_agent_that_used_an_app_cannot_approve_or_resume_itself(world):
    """After a Deny or a stop the model ran `approve --always` or `resume`, and both
    worked. Now they are the person's: a caller that has made a tool call is refused,
    also after it sent a turn end."""
    assert world.run("get_app_state", "Demo")[0] == 0
    world.run("turn-ended")
    for command in (
        ["approve", "--app", "Demo", "--always"],
        ["resume"],
        ["resume", "Demo"],
    ):
        code, out, err = world.run(*command)
        assert (code, out) == (1, "") and "is the user's command" in err
    assert (
        "approvals.approve" not in world.names()
        and "person.resume" not in world.names()
    )
    other = AgentSession("the-person")  # the person's terminal: another caller
    assert world.run("resume", agent=other)[0] == 0


def test_a_restarted_helper_still_knows_who_made_tool_calls(world, monkeypatch):
    """The agent made a call, the person stopped it, the agent ran `helper restart`
    (or killed the helper) and then `resume`: the new helper had forgotten the
    callers, and the resume counted as the person's."""
    monkeypatch.setattr(tools, "_callers", set())
    monkeypatch.setattr(helper, "process", lambda pid: (1, "/bin/agent", 1030.0))
    agent_process, terminal = (30, 1030.0), (10, 1010.0)
    agent = AgentSession(
        "a", caller=agent_process, parents=frozenset({agent_process, terminal})
    )
    assert world.run("get_app_state", "Demo", agent=agent)[0] == 0
    monkeypatch.setattr(tools, "_callers", set())  # the helper is started again
    child = AgentSession(
        "x", parents=frozenset({(80, 1080.0), agent_process, terminal})
    )
    for command in (["resume"], ["approve", "--app", "Demo"]):
        code, _, err = world.run(*command, agent=child)
        assert code == 1 and "is the user's command" in err
    person = AgentSession("p", parents=frozenset({(90, 1090.0), terminal}))
    assert world.run("resume", agent=person)[0] == 0  # a shell that made no call
    reused = AgentSession("r", parents=frozenset({(30, 5000.0), terminal}))
    assert (
        world.run("resume", agent=reused)[0] == 0
    )  # the pid now names another process


def test_another_session_name_or_a_script_does_not_make_the_agent_the_person(
    world, monkeypatch
):
    """`MACOS_HARNESS_SESSION=x macos-harness resume`, and `approve` started from a
    script, came from a caller that had made no tool call: both worked. Now the
    process behind every tool call is remembered, and a command with that process
    among its parents is refused."""
    monkeypatch.setattr(tools, "_callers", set())
    agent_process, terminal = (30, 1030.0), (10, 1010.0)
    agent = AgentSession(
        "a", caller=agent_process, parents=frozenset({agent_process, terminal})
    )
    assert world.run("get_app_state", "Demo", agent=agent)[0] == 0
    renamed = AgentSession(
        "x", parents=frozenset({(80, 1080.0), agent_process, terminal})
    )
    for command in (["approve", "--app", "Demo"], ["resume"]):
        code, _, err = world.run(*command, agent=renamed)
        assert code == 1 and "is the user's command" in err
    person = AgentSession(
        "p", parents=frozenset({(90, 1090.0), terminal})
    )  # another tab
    assert world.run("resume", agent=person)[0] == 0


def test_stop_resume_and_approve(world):
    assert world.run("stop") == (0, "Stopped every app.\n", "")
    assert world.args("person.stop") == (None,)
    assert world.run("stop", "--app", "Demo") == (0, "Stopped Demo.\n", "")
    assert world.args("person.stop") == (TARGET,)
    assert world.run("resume", "Demo") == (0, "Resumed Demo.\n", "")
    assert world.args("person.resume") == (TARGET,)
    assert world.run("approve", "--app", "Demo", "--always") == (
        0,
        "Approved Demo.\n",
        "",
    )
    assert world.args("approvals.approve") == ("com.example.demo", True)
    world.run("approve", "Demo")
    assert world.args("approvals.approve") == ("com.example.demo", False)
    assert world.run("approve") == (
        2,
        "",
        "Give the app: --app <name, bundle id or path>\n",
    )


def test_doctor_skill_install(world):
    assert world.run("doctor") == (0, '{\n  "accessibility": true\n}\n', "")
    assert world.run("skill") == (0, "the skill\n", "")
    assert world.run("install", "--agent", "some-agent", "--scope", "project") == (
        0,
        "/home/skill/SKILL.md\n",
        "",
    )
    assert world.args("install.install") == ("some-agent", "project", "/work", {})
    world.install.answers["hook_note"] = "Install it with uv tool install."
    assert (
        world.run("install")[1]
        == "/home/skill/SKILL.md\nInstall it with uv tool install.\n"
    )
    world.run("uninstall")
    assert world.args("install.uninstall") == (None, "user", "/work", {})


def test_telemetry_command(world):
    code, out, _ = world.run("telemetry", "disable")
    assert code == 0 and json.loads(out)["enabled"] is False
    assert json.loads(world.run("telemetry")[1])["disabled_by_config"] is True
    assert world.run("telemetry", "sometimes")[0] == 2
