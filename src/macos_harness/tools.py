"""The eleven tools and the other commands: parse one call, check it, run its steps.

This is the only file that knows the order of one call. Every other module does its
one step when it is called and never calls sideways.
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib
import json
import math
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, NamedTuple

from . import errors, protocol
from .model import (
    PANEL_SERVICE,
    SLOW_TYPING,
    TYPE_DELAY,
    AgentSession,
    AppSession,
    AppTarget,
    Shot,
)

ACTION_LINE = "Done. Call `get_app_state` to see the new state of the UI."
# These tools first wait out the action before them, so that keys reach the UI the
# action made. An empty tuple switches the wait off.
SETTLE_BEFORE_KEYS = ("press_key", "type_text")
# The tools that find a dead element again although its value changed.
REFETCH_IGNORES_VALUE = ("set_value",)
FRESH_WINDOW_POLL_S = 0.05  # settle poll after a launch, and while there is no window
MAX_CLICKS = 10  # one call clicks at most this often
MAX_TEXT = 20000  # characters one type_text call types (about 10 s); paste takes more
# An action is not started when it would end less than this before the client's limit.
DEADLINE_MARGIN_S = 1.0
MAX_HOLD_S = 30.0  # a hold is capped at 30000 ms
BOOLS = {"true": True, "1": True, "false": False, "0": False}
BUTTON_NAMES = ("l left 0", "r right 1", "m middle 2")  # of button 0, 1 and 2
BUTTONS = {name: n for n, names in enumerate(BUTTON_NAMES) for name in names.split()}
# "u" and "up", "d" and "down", "l" and "left", "r" and "right"
DIRECTIONS = {w[:n]: w for w in ("up", "down", "left", "right") for n in (1, len(w))}
SELECTIONS = ("text", "cursor_before", "cursor_after")
# The commands `--help` names. The rest are for the person and for the host's hooks.
HELP_COMMANDS = ("doctor", "skill", "install", "uninstall", "telemetry")


class NeedStdin(Exception):
    """The call takes its text from stdin and the client has not sent it yet."""


class _Modules:
    """The other modules, imported when first used. A test puts its fakes here."""

    def __getattr__(self, name: str):
        module = importlib.import_module(f"{__package__}.{name}")
        setattr(self, name, module)
        return module


mods = _Modules()


@dataclass
class Call:
    name: str
    args: dict[str, Any]
    agent: AgentSession
    cwd: str
    deadline: float  # time.time() seconds at which the client gives up
    # True once the client is gone (the host interrupted the tool): stop the action
    abandoned: Callable[[], bool] = lambda: False


class Result(NamedTuple):
    text: str  # printed as it is
    value: Any = None  # printed with --json


class Outcome(NamedTuple):
    exit: int
    stdout: str
    stderr: str
    command: str | None = None
    error: errors.HarnessError | None = None


# --- argument checks, in the words of errors.USAGE ------------------------------------


def _number(value: Any) -> float | None:
    """A finite number from flag text or from JSON. None for anything else."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number == int(number) else None


def _index(a: dict) -> int:
    index = _integer(a.get("element_index"))
    if index is None:
        raise errors.usage("element_index")
    return index


def _point(a: dict, x: str = "x", y: str = "y", text: str = "coordinate"):
    point = _number(a.get(x)), _number(a.get(y))
    if None in point:
        raise errors.usage(text)
    return point


def _target(a: dict) -> int | tuple[float, float]:
    """An element number when one is given (x and y are then ignored), else a point."""
    return _index(a) if "element_index" in a else _point(a)


def _button(value: Any) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value not in (0, 1, 2):
            raise errors.usage("mouse_button_number")
        return int(value)
    button = BUTTONS.get(str(value).strip().lower())
    if button is None:
        raise errors.usage("mouse_button")
    return button


def _hold(a: dict) -> float | None:
    """`duration`: seconds per press. `duration_ms` (the older name)
    gives the same in milliseconds. None when neither is given."""
    if "duration" in a:
        seconds = _number(a["duration"])
        if seconds is None:
            raise errors.usage("finite_duration")
    elif "duration_ms" in a:
        ms = _number(a["duration_ms"])
        seconds = None if ms is None else ms / 1000
    else:
        return None
    if seconds is None or not 0 <= seconds <= MAX_HOLD_S:
        raise errors.usage("duration")
    return seconds


# --- the steps of one call (design: "what one call does, in order") -------------------


@contextlib.contextmanager
def _session(call: Call, state_read: bool):
    """Steps 2 to 8: app, policy, approval, permissions, lock, person, the app's queue."""
    agent = call.agent
    if not call.args.get("app", "").strip():  # checked last, after the tool's own
        raise errors.usage("app")
    target = mods.apps.resolve(call.args["app"])
    if target.bundle_id == PANEL_SERVICE:  # the panel of an app it reads
        seen = [app.watching for app in mods.helper.SESSIONS.apps(agent)]
        target = mods.apps.panel_owner([t for t in seen if t is not None])
    policy = mods.policy.decide(agent, target)
    mods.approvals.ensure(agent, target, policy, call.deadline)
    mods.permissions.gate(call.deadline)
    mods.lockscreen.gate(state_read)
    mods.person.gate(agent, target, state_read)
    with mods.helper.app_queue(target):
        # The wait for the app's queue may have been long: the person may have taken
        # the app meanwhile, and the client may have given up or gone.
        mods.person.gate(agent, target, state_read)
        _still_wanted(call)
        app = mods.helper.SESSIONS.app(agent, target)
        began = time.monotonic()
        app.cancel = lambda: call.abandoned() or mods.person.interrupted(target, began)
        try:
            yield app, target
        except errors.HarnessError as error:
            if error.message != errors.accessibility("AXError.cannotComplete").message:
                raise
            raise errors.not_responding(target.name) from None  # it did not answer


def _still_wanted(call: Call, planned: float = 0.0) -> None:
    """Refuse a call whose client has given up, or will have before the action ends
    (`planned` seconds of holds and typing): done after that, the model would not
    know and would do it again."""
    late = call.deadline and time.time() + planned >= call.deadline - DEADLINE_MARGIN_S
    if late or call.abandoned():
        raise errors.request_timed_out()


def _reader(target: AppTarget, kind) -> Callable:
    """The function that reads one window. While a menu is open it reads that menu.
    A read that breaks in a way of its own says so."""

    def read(window):
        try:
            return mods.tree.read(
                target, window, kind, menu=mods.windows.open_menu(target)
            )
        except errors.HarnessError:
            raise
        except Exception as error:  # written to the log, then said
            traceback.print_exc()
            raise errors.capture_failed() from error

    return read


def _has_window(target: AppTarget) -> bool:
    try:
        mods.windows.key_window(target)
    except errors.HarnessError as error:
        if error.message != errors.no_windows().message:
            raise
        return False
    return True


def _switch_off(app: AppSession) -> None:
    """Undo what the first state read of a session switched on for its app."""
    target, app.watching = app.watching, None
    if target is not None:
        # A menu the agent left open would take the person's next keys.
        _quietly(lambda: mods.actions.cancel_menu(app))
        _quietly(lambda: mods.focus.release(target))
        _quietly(lambda: mods.tree.disable(target))
        _quietly(lambda: mods.windows.unwatch(target))
        _quietly(app.revisions.reset)


def _quietly(step: Callable) -> None:
    """A step of a clean-up. When it fails the steps after it still run."""
    try:
        step()
    except Exception:  # noqa: BLE001 - written to the log, the clean-up goes on
        traceback.print_exc()


def _picture(target: AppTarget, window) -> Shot | errors.HarnessError:
    """The window's screenshot with its open menus and sheets, or why there is none.
    The tree is worth having alone."""
    try:
        extras = mods.windows.extra_windows(target, window.window_id)
        frames = tuple((row["id"], row["frame"]) for row in extras)
        return mods.screenshot.capture(window, frames)
    except errors.HarnessError as error:
        return error


def _check_page(app: AppSession, capture) -> None:
    """A blocked page ends the session: no action follows it."""
    try:
        mods.policy.check_url(capture.url)
    except errors.HarnessError:
        app.active = False
        raise
    app.page_unchecked = False


def _read_state(call: Call, app: AppSession, target: AppTarget) -> tuple[str, Any]:
    """Step 9: the state text of the app's key window, and its screenshot."""
    agent = call.agent
    try:
        target, launched = mods.apps.running(target), False
    except errors.HarnessError as error:
        if error.name != errors.app_not_running("").name:
            raise
        target, launched = mods.apps.launch(target), True
    kind = mods.apps.kind(target)
    switched_on = False
    if app.watching != target:  # the first read, or the app came back as a new process
        _switch_off(app)
        switched_on = mods.tree.enable(target, kind)
        mods.windows.watch(target)
        app.revisions.watch(target)
        app.target = app.watching = target
    read = _reader(target, kind)
    fresh = launched or not _has_window(target)
    if switched_on and not fresh:  # a web view gives its page only once it was asked
        with contextlib.suppress(errors.HarnessError):
            mods.tree.ask_once(mods.windows.key_window(target).element)
    capture = None
    # Chromium builds its tree for some hundred ms after it was switched on: a read
    # right then came back without the page's URL and without an iframe (Electron).
    warming = switched_on and (kind.electron or kind.chromium)
    if app.needs_settle or fresh or warming:
        app.needs_settle = False  # cleared before the wait: an action may set it again
        poll = FRESH_WINDOW_POLL_S if fresh else None
        capture = mods.settle.wait(target, kind, read, poll=poll)
    if capture is None:  # no window at all raises here, after the wait
        capture = read(mods.windows.key_window(target))
    _check_page(app, capture)
    window = mods.windows.key_window(target)
    shot = _picture(target, window)
    app.shot = shot if isinstance(shot, Shot) else None
    disable_diff = (
        call.args["disableDiff"] or agent.env.get("MACOS_HARNESS_DIFF") == "0"
    )
    text = app.revisions.present(capture, disable_diff)
    prefix = mods.instructions.prefix(agent, target.bundle_id) or ""
    mods.person.after_state_read(target)
    app.active = True
    mods.presence.halo(window)
    mods.presence.live_view(window)
    mods.presence.pointer_activity("loading")
    return prefix + text, shot


def _act(
    call: Call,
    send: Callable,
    *targets: int | tuple[float, float],
    takes: Callable[[AppTarget], float] = lambda target: 0.0,
) -> Result:
    """Step 10: one action. A target is an element number or a point in the screenshot.
    `takes` says how many seconds of holds and typing the action plans in this app."""
    with _session(call, state_read=False) as (app, target):
        if not app.active:
            raise errors.not_active(target.path)
        try:  # the process the last state read saw: its numbers went with it
            replaced = mods.apps.running(app.target) != app.target
        except errors.HarnessError:
            app.active = False
            raise
        if replaced:
            app.active = False
            raise errors.not_active(target.path)
        target = app.target  # as the last state read saw it: running, with its pid
        planned = takes(target)
        _still_wanted(call, planned)
        kind = mods.apps.kind(target)
        read = _reader(target, kind)
        # The action before this one may have opened another page (a chain of calls
        # with no read between them): wait for it, then look at its address.
        unchecked, settled = app.page_unchecked and mods.policy.watches_urls(), None
        if app.needs_settle and (unchecked or call.name in SETTLE_BEFORE_KEYS):
            app.needs_settle = False
            settled = mods.settle.wait(target, kind, read)
        if unchecked:
            _check_page(app, settled or read(mods.windows.key_window(target)))
        where = []
        for item in targets:
            if isinstance(item, int):
                reread = lambda: read(mods.windows.key_window(target))
                ignore = call.name in REFETCH_IGNORES_VALUE
                where.append(app.revisions.resolve(item, reread, ignore_value=ignore))
            elif app.shot is None:  # the last state read got no picture to point in
                raise errors.screenshot_failed()
            else:
                where.append(app.shot.to_screen(*item))
        _still_wanted(call, planned)  # the settle and the lookups above took their time
        mods.presence.pointer_activity("idle")
        # Set before the action: one that posted its events and then failed (the
        # person took over, the front app was not put back in time) changed the app too.
        app.needs_settle, app.page_unchecked = True, kind.http or kind.chromium
        send(app, *where)
        mods.presence.pointer_activity("loading")
    return Result(ACTION_LINE)


# --- the eleven tools: each checks its arguments first, in one fixed order -----------


def list_apps(call: Call) -> Result:
    try:
        rows = mods.apps.list_apps(call.agent.hosts)  # its own terminal is not in front
    except errors.HarnessError as error:
        error.message = f"Native apps: {error.message}"  # which list failed
        raise
    text = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return Result(text if call.args["emit"] else "", rows)


def get_app_state(call: Call) -> Result:
    with _session(call, state_read=True) as (app, target):
        text, shot = _read_state(call, app, target)
    if isinstance(shot, Shot) and call.args["keep"]:  # proof of work: never pruned
        shot = dataclasses.replace(
            shot, path=mods.screenshot.keep(shot.path, target.name)
        )
    if isinstance(shot, Shot):
        line = f"Screenshot ({shot.size[0]}x{shot.size[1]} px): {shot.path}"
        picture = {"url": PurePosixPath(shot.path).as_uri()}
    else:
        line, picture = f"Screenshot: none. {shot.message}", None
    shown = text.rstrip("\n") + "\n\n" if call.args["emit"] else ""
    value = {"app": call.args["app"], "screenshot": picture, "text": text}
    return Result(shown + line, value)


def click(call: Call) -> Result:
    a = call.args
    target = _target(a)
    button = _button(a["mouse_button"])
    count = _integer(a["click_count"]) or 0  # not checked here: a count below 1 fails
    if count > MAX_CLICKS:
        raise errors.usage("click_count", most=MAX_CLICKS)
    key, hold = a.get("key"), _hold(a)
    if hold and count * hold > MAX_HOLD_S:  # five clicks of 30 s outlast the client
        raise errors.usage("duration")
    send = lambda app, where: mods.actions.click(app, where, button, count, key, hold)
    return _act(call, send, target, takes=lambda target: count * (hold or 0.0))


def drag(call: Call) -> Result:
    a = call.args
    start = _point(a, "from_x", "from_y", "from")
    end = _point(a, "to_x", "to_y", "to")
    return _act(call, lambda app, s, e: mods.actions.drag(app, s, e), start, end)


def press_key(call: Call) -> Result:
    a = call.args
    key = a.get("key", "")
    if not key.strip():
        raise errors.usage("key")
    hold = _hold(a)
    send = lambda app: mods.actions.press_key(app, key, hold)
    return _act(call, send, takes=lambda target: hold or 0.0)


def type_text(call: Call) -> Result:
    text = call.args.get("text", "")
    if len(text) > MAX_TEXT:
        raise errors.usage("text_length", most=MAX_TEXT)

    def send(app: AppSession) -> None:
        if not text:
            raise errors.no_text_to_type()
        mods.actions.type_text(app, text)

    def takes(target: AppTarget) -> float:
        """A key down and a key up per character, each followed by the app's delay."""
        each = 2 * SLOW_TYPING.get(target.bundle_id, TYPE_DELAY)
        if len(text) * each > MAX_HOLD_S:  # an app that is typed into slowly
            raise errors.usage("text_length", most=int(MAX_HOLD_S / each))
        return len(text) * each

    return _act(call, send, takes=takes)


def set_value(call: Call) -> Result:
    a = call.args
    index = _index(a)
    if "value" not in a:
        raise errors.no_value_to_set()
    send = lambda app, line: mods.actions.set_value(app, line, a["value"])
    return _act(call, send, index)


def scroll(call: Call) -> Result:
    a = call.args
    pages = _number(a["pages"])
    if pages is None or pages <= 0:
        raise errors.usage("pages")
    target = _target(a)
    direction = DIRECTIONS.get(a.get("direction", "").strip().lower())
    if direction is None:
        raise errors.usage("direction")
    send = lambda app, where: mods.actions.scroll(app, where, direction, pages)
    return _act(call, send, target)


def select_text(call: Call) -> Result:
    a = call.args
    index = _index(a)
    mode = a["selection_type"]
    if mode not in SELECTIONS:
        raise errors.usage("selection_type", value=mode)
    text, prefix, suffix = (a.get(name, "") for name in ("text", "prefix", "suffix"))
    send = lambda app, line: mods.actions.select_text(
        app, line, text, prefix, suffix, mode
    )
    return _act(call, send, index)


def perform_secondary_action(call: Call) -> Result:
    a = call.args
    index, action = _index(a), a.get("action", "")
    send = lambda app, line: mods.actions.secondary(app, line, action)
    return _act(call, send, index)


def paste(call: Call) -> Result:
    a = call.args
    if "text" not in a:
        raise errors.no_text_to_paste()
    text, fmt = a["text"], a["format"].strip().lower()
    return _act(call, lambda app: mods.actions.paste(app, text, fmt))


# --- the other commands ---------------------------------------------------------------


def doctor(call: Call) -> Result:
    report = mods.permissions.doctor()
    return Result(json.dumps(report, indent=2, ensure_ascii=False), report)


def skill(call: Call) -> Result:
    text = mods.install.skill_text()
    return Result(text, text)


def install(call: Call) -> Result:
    a = call.args
    written = mods.install.install(a.get("agent"), a["scope"], call.cwd, call.agent.env)
    note = mods.install.hook_note()
    return Result("\n".join(written + [note] * bool(note)), written)


def uninstall(call: Call) -> Result:
    a = call.args
    mods.install.uninstall(a.get("agent"), a["scope"], call.cwd, call.agent.env)
    return Result("")


def turn_started(call: Call) -> Result:
    mods.person.turn_started(call.agent)
    return Result("")


def let_go(agent: AgentSession) -> None:
    """The agent's turn is over, or the agent is gone: let go of every app it drove."""
    mods.person.turn_ended(agent)
    for app in mods.helper.SESSIONS.apps(agent):
        with mods.helper.app_queue(app.target):
            _switch_off(app)
    mods.helper.SESSIONS.drop(agent)
    if not mods.person.driven():  # another agent still drives an app: its halo stays
        mods.presence.turn_done()


def turn_ended(call: Call) -> Result:
    let_go(call.agent)
    return Result("")


def _named_app(call: Call) -> AppTarget | None:
    name = call.args.get("app")
    return mods.apps.resolve(name) if name else None


def approve(call: Call) -> Result:
    """The person approves an app ahead of time, for a run with nobody at the Mac."""
    target = _named_app(call)
    if target is None:
        raise errors.usage("app")
    mods.approvals.approve(target.bundle_id, call.args["always"])
    return Result(f"Approved {target.name}.")


def telemetry(call: Call) -> Result:
    action = call.args.get("action")
    code, out, err = mods.telemetry.run_cli([action] if action else [])
    if code:
        raise errors.UsageError(err.strip())
    return Result(out, json.loads(out))


def show_help(call: Call) -> Result:
    """`--help`: the tools, or one tool with its flags and one example."""
    asked = call.args.get("tool")
    if asked is None:
        examples = "\n".join(f"  {tool.example}" for tool in TOOLS.values())
        text = (
            "macos-harness <tool> [--flag value ...]\n\n"
            f"{examples}\n\nOther commands: {', '.join(HELP_COMMANDS)}\n"
            "One tool: macos-harness <tool> --help"
        )
        return Result(text, text)
    name = _known(asked)
    tool = _table()[name]
    lines = [tool.description, *(f"  --{p}: {PARAMS[p][1]}" for p in tool.params)]
    text = "\n".join(line for line in [*lines, tool.example] if line)
    return Result(text, text)


# --- the tables -----------------------------------------------------------------------

_APP = "The app: its name, its bundle id or the full path of its bundle"
_KEY = (
    "A key name, or a chord joined with + (X11 keysym names: a, space, Return, Tab, "
    "Control_L+a, Super_L+d). Spaces around + do not matter; Control, Ctrl, Alt and "
    "Shift work too. With click: the keys held during the click"
)
# flag -> (kind, what --help says). A bool flag takes no value, text arrives as text,
# and every other kind is checked by the tool that takes it.
PARAMS = {
    "app": ("str", _APP),
    "disableDiff": ("bool", "Print the whole tree, not only what changed"),
    "emit": ("bool", "false: do the read and print only the Screenshot line"),
    "keep": ("bool", "Keep the screenshot for good, as proof (a path without spaces)"),
    "element_index": (
        "int",
        "The number of the element in the last get_app_state text",
    ),
    "x": ("num", "Horizontal pixel position in the screenshot"),
    "y": ("num", "Vertical pixel position in the screenshot"),
    "mouse_button": (
        "button",
        "Which mouse button: left (the default), right or middle.",
    ),
    "click_count": ("int", "How many clicks (default 1)"),
    "key": ("str", _KEY),
    "duration": ("num", "Seconds to hold each press (0 to 30)"),
    "duration_ms": ("num", "The same in milliseconds (0 to 30000)"),
    "from_x": (
        "num",
        "Where the drag starts: horizontal pixel position in the screenshot",
    ),
    "from_y": (
        "num",
        "Where the drag starts: vertical pixel position in the screenshot",
    ),
    "to_x": ("num", "Where the drag ends: horizontal pixel position in the screenshot"),
    "to_y": ("num", "Where the drag ends: vertical pixel position in the screenshot"),
    "text": ("str", "The text, exactly. Missing or -: read from stdin"),
    "value": ("str", "Replacement value. Missing or -: read from stdin"),
    "direction": ("str", "up, down, left or right"),
    "pages": (
        "num",
        "How many pages to scroll; fractions work (default 1)",
    ),
    "prefix": (
        "str",
        "The text just before the wanted text, when it occurs more than once",
    ),
    "suffix": (
        "str",
        "The text just after the wanted text, when it occurs more than once",
    ),
    "selection_type": ("str", "text, cursor_before or cursor_after. Defaults to text"),
    "action": ("str", "A name from the element's Secondary Actions"),
    "format": ("str", "text, md or html. Defaults to text"),
    "agent": ("str", "The agent CLI to install the skill for"),
    "scope": ("str", "project or user"),
    "always": ("bool", "Keep the approval after this session"),
    "tool": ("str", "A tool name"),
    "json": ("bool", "Print the result object as JSON"),
    "help": ("bool", "Show this text"),
}
# Other spellings of a flag. Flags are compared without case, `-` and `_`.
SPELLINGS = {
    "disablediffing": "disableDiff",
    "selection": "selection_type",
    "index": "element_index",
    "element": "element_index",
    "button": "mouse_button",
    "count": "click_count",
}


@dataclass(frozen=True)
class Tool:
    run: Callable[[Call], Result]
    params: tuple[str, ...] = ()  # its flags
    bare: tuple[str, ...] = ()  # the parameters bare words fill, in order
    defaults: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    example: str = ""
    hidden: tuple[str, ...] = ()  # accepted and not taught
    refused: dict[str, str] = field(default_factory=dict)  # flag -> errors.USAGE key
    stdin: str | None = None  # the parameter that may come from stdin


def _tool(run, params="", bare="", example="", **more) -> Tool:
    example = f"macos-harness {example}" if example else ""
    return Tool(
        run, tuple(params.split()), tuple(bare.split()), example=example, **more
    )


TOOLS = {
    "list_apps": _tool(
        list_apps,
        description=(
            "Show the apps on this Mac: those running now and those used in the past "
            "14 days, with how often each was used"
        ),
        example="list_apps",
        hidden=("emit",),
        defaults={"emit": True},
    ),
    "get_app_state": _tool(
        get_app_state,
        "app disableDiff keep",
        "app",
        description=(
            "Read an app's key window as an accessibility tree plus a screenshot, "
            "opening a session for the app first when there is none. Needed once in "
            "every assistant turn before any action on that app"
        ),
        example="get_app_state --app Finder",
        hidden=("emit",),
        defaults={"disableDiff": False, "emit": True, "keep": False},
    ),
    "click": _tool(
        click,
        "app element_index x y mouse_button click_count key duration",
        "app element_index",
        description="Click one element, given by its index or by a pixel position in the screenshot",
        example="click --app Finder --element_index 40",
        defaults={"mouse_button": "left", "click_count": 1},
        hidden=("duration_ms",),
    ),
    "perform_secondary_action": _tool(
        perform_secondary_action,
        "app element_index action",
        "app element_index action",
        description="Run one of the secondary actions an element lists",
        example="perform_secondary_action --app Finder --element_index 0 --action Raise",
    ),
    "set_value": _tool(
        set_value,
        "app element_index value",
        "app element_index value",
        description="Write a new value into an element that is marked settable",
        example='set_value --app Finder --element_index 2 --value "report.txt"',
        stdin="value",
    ),
    "select_text": _tool(
        select_text,
        "app element_index text prefix suffix selection_type",
        "app element_index text",
        description=(
            "Select a piece of text in a text element, or put the cursor right before "
            "or after it. Give the text as the accessibility tree prints it, "
            "Markdown marks included. When it occurs more than once, add the text "
            "before or after it as prefix or suffix."
        ),
        example='select_text --app TextEdit --element_index 3 --text "hello"',
        defaults={"selection_type": "text"},
    ),
    "scroll": _tool(
        scroll,
        "app element_index x y direction pages",
        "app element_index direction pages",
        description="Scroll one element up, down, left or right by pages",
        example="scroll --app Finder --element_index 3 --direction down --pages 1",
        defaults={"pages": 1},
        refused={"pixels": "pixels"},
    ),
    "drag": _tool(
        drag,
        "app from_x from_y to_x to_y",
        "app from_x from_y to_x to_y",
        description="Drag between two pixel positions of the screenshot",
        example="drag --app Finder --from_x 200 --from_y 300 --to_x 400 --to_y 300",
    ),
    "press_key": _tool(
        press_key,
        "app key duration",
        "app key",
        description=(
            "Press one key or a chord, modifiers and navigation keys included, "
            'written the way xdotool writes keys: "a", "Return", "Tab", "super+c", '
            '"Up", "KP_0" (the 0 of the number pad).'
        ),
        example="press_key --app Finder --key super+c",
        hidden=("duration_ms",),
    ),
    "type_text": _tool(
        type_text,
        "app text",
        "app text",
        description="Type the given text as key presses",
        example='type_text --app TextEdit --text "hello world"',
        stdin="text",
    ),
    "paste": _tool(
        paste,
        "app text format",
        "app text",
        description=(
            "Paste text into an app's window and put back what was on the clipboard "
            "before."
        ),
        example='paste --app TextEdit --text "hello world"',
        defaults={"format": "text"},
        stdin="text",
    ),
}
COMMANDS = {
    "doctor": _tool(doctor),
    "skill": _tool(skill),
    "install": _tool(install, "agent scope", "agent", defaults={"scope": "user"}),
    "uninstall": _tool(uninstall, "agent scope", "agent", defaults={"scope": "user"}),
    "turn-started": _tool(turn_started),
    "turn-ended": _tool(turn_ended),
    "approve": _tool(approve, "app always", "app", defaults={"always": False}),
    "telemetry": _tool(telemetry, bare="action"),
    "help": _tool(show_help, bare="tool"),
}


def _table() -> dict[str, Tool]:
    return {**TOOLS, **COMMANDS}


# --- parsing --------------------------------------------------------------------------


def _plain(name: str) -> str:
    return name.replace("-", "").replace("_", "").lower()


def _with_example(error: errors.UsageError, name: str) -> errors.UsageError:
    """The usage error with one example line under it, for the model to copy."""
    return errors.UsageError(f"{error.message}\nExample: {_table()[name].example}")


def _known(word: str) -> str:
    """The tool or command a word names, in any case and with `-` or `_`."""
    name = {_plain(known): known for known in _table()}.get(_plain(word))
    if name is None:
        error = errors.usage("unknown_tool", tool=word, tools=", ".join(TOOLS))
        raise _with_example(error, "get_app_state")
    return name


def _flag(name: str, tool: Tool, word: str) -> str:
    """The parameter a flag names: any case, `-` or `_`, or another spelling of it."""
    plain = _plain(word)
    plain = _plain(SPELLINGS.get(plain, plain))
    if plain in tool.refused:
        raise errors.usage(tool.refused[plain])
    flags = (*tool.params, *tool.hidden, *tool.bare, "json", "help")
    param = {_plain(flag): flag for flag in flags}.get(plain)
    if param is None:
        shown = ", ".join(f"--{flag}" for flag in tool.params) or "none"
        error = errors.usage("unknown_flag", flag=word, tool=name, flags=shown)
        raise _with_example(error, name)
    return param


def _json_object(word: str) -> dict | None:
    """`{"app": "Finder"}` in place of flags. None when the word is not that."""
    try:
        given = json.loads(word) if word.startswith("{") else None
    except ValueError:
        return None
    return given if isinstance(given, dict) else None


def parse(argv: list[str]) -> tuple[str, dict[str, Any]]:
    """(tool or command name, its arguments). Every argument rule is in here."""
    words, args, bare = list(argv), {}, []
    while words and words[0].startswith("--") and _plain(words[0]) in ("json", "help"):
        args[_plain(words.pop(0))] = True
    if not words:
        if args.get("help"):
            return "help", args
        raise errors.usage("python_stdin")
    name = _known(words[0])
    tool, at = _table()[name], 1
    given = _json_object(words[1]) if len(words) > 1 else None
    if given is not None:
        named = {_flag(name, tool, key): value for key, value in given.items()}
        args.update(
            {param: value for param, value in named.items() if value is not None}
        )
        at = 2
    while at < len(words):
        word, at = words[at], at + 1
        if not word.startswith("--") or len(word) == 2:
            bare.append(word)
            continue
        key, equals, value = word[2:].partition("=")
        param = _flag(name, tool, key)
        following = words[at] if at < len(words) else None
        if PARAMS[param][0] == "bool":  # alone it is true. It takes only true or false
            if not equals:
                value = "true"
                if following is not None and following.lower() in BOOLS:
                    value, at = following, at + 1
        elif not equals:  # the next word is the value, also when it starts with `-`
            value, at = following, at + 1
        if value is not None:
            args[param] = value
    point = "x" in args or "y" in args  # then no bare word is an element number
    free = [
        p for p in tool.bare if p not in args and not (point and p == "element_index")
    ]
    if len(bare) > len(free):
        extra = bare[len(free)]
        error = errors.usage("unexpected_argument", argument=extra, tool=name)
        raise _with_example(error, name)
    args.update(zip(free, bare))
    for param, value in args.items():
        kind = PARAMS[param][0]
        if kind == "bool":
            args[param] = BOOLS.get(str(value).lower(), True)
        elif kind == "str" and not isinstance(value, str):
            args[param] = json.dumps(value)  # a number or a bool in the JSON form
    return name, {**tool.defaults, **args}


# --- one call -------------------------------------------------------------------------


def execute(
    argv: list[str],
    stdin: str | None,
    agent: AgentSession,
    cwd: str,
    deadline: float,
    abandoned: Callable[[], bool] = lambda: False,
) -> Outcome:
    """Run one call and say what to print. Raises only NeedStdin. `abandoned` turns
    True once the client has gone: the running action then stops."""
    as_json, name = "--json" in argv, None
    try:
        name, args = parse(argv)
        as_json = bool(args.pop("json", False))
        tool = _table()[name]
        if args.pop("help", False):
            name, args = "help", {"tool": None if name == "help" else name}
            tool = COMMANDS["help"]
        if tool.stdin and args.get(tool.stdin) in (None, "-"):
            if stdin is None:
                raise NeedStdin
            args.pop(tool.stdin, None)
            if stdin.removesuffix("\n"):  # a heredoc adds one newline: exactly one goes
                args[tool.stdin] = stdin.removesuffix("\n")
        result = tool.run(Call(name, args, agent, cwd, deadline, abandoned))
    except NeedStdin:
        raise
    except errors.HarnessError as error:
        return _failed(error, as_json, name)
    except Exception as error:  # noqa: BLE001 - every failure becomes a text for the model
        traceback.print_exc()
        described = errors.server_error(f"{type(error).__name__}: {error}")
        return _failed(described, as_json, name)
    if as_json:
        return Outcome(0, json.dumps(result.value, ensure_ascii=False) + "\n", "", name)
    text = result.text
    return Outcome(
        0, text + "\n" if text and not text.endswith("\n") else text, "", name
    )


def _failed(error: errors.HarnessError, as_json: bool, name: str | None) -> Outcome:
    code = 2 if isinstance(error, errors.UsageError) else 1
    if as_json:
        shown = json.dumps(protocol.failure(error), ensure_ascii=False) + "\n"
        return Outcome(code, shown, "", name, error)
    return Outcome(code, "", error.message + "\n", name, error)


def run(
    argv: list[str], stdin: str | None, agent: AgentSession, cwd: str, deadline: float
) -> tuple[int, str, str]:
    """(exit code, stdout, stderr) of one call. Raises NeedStdin when it wants stdin."""
    return execute(argv, stdin, agent, cwd, deadline)[:3]
