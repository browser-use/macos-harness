"""Every error text the model can receive. One place, so each wording is written and tested once.

Rule for new texts: say what happened, then what to do next.
"""

from __future__ import annotations

import inspect
import string
from collections.abc import Callable

PRODUCT = "macOS Harness"


class HarnessError(Exception):
    """A tool call failed. `message` is printed to the model as it is."""

    def __init__(
        self,
        code: int,
        name: str,
        message: str,
        *,
        cancelled: bool = False,
        tag: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.name = name
        self.message = message
        # the person did it (took over, said no). Not a failure of the tool.
        self.cancelled = cancelled
        self.tag = tag  # machine-readable extra, for example "invalid_element_id"


class UsageError(HarnessError):
    """Bad arguments. Raised before any app is touched. Exit code 2."""

    def __init__(self, message: str):
        super().__init__(0, "usage", message)


# Argument checks. tools.py raises these through usage(); it writes no check text of its
# own. The texts are part of the tool contract: tests compare them letter for letter.
USAGE = {
    "app": "Give the app: --app <name, bundle id or path>",
    "element_index": "--element_index takes a whole number",
    "coordinate": "--x and --y must both be numbers",
    "from": "--from_x and --from_y must both be numbers",
    "to": "--to_x and --to_y must both be numbers",
    "mouse_button": "--mouse_button takes left, right or middle (also l, r, m or 0, 1, 2)",
    "mouse_button_number": "--mouse_button as a number takes 0, 1 or 2",
    "key": "Give the key: --key <name or chord>",
    "pages": "--pages takes a number above 0",
    "direction": "--direction takes up, down, left or right",
    "pixels": "scroll takes --pages here; --pixels is not supported.",
    "selection_type": "--selection_type cannot be {value}. Use text, cursor_before or cursor_after.",
    "duration": "--duration is not a usable time",
    "click_count": "clickCount must be {most} or less",
    "text_length": "text is longer than {most} characters. Use paste for long text",
    "finite_duration": "--duration takes a number of seconds",
    "unknown_flag": "Unknown flag --{flag} for {tool}. Flags: {flags}",
    "unknown_tool": "Unknown tool {tool}. Tools: {tools}",
    "python_stdin": "Python on stdin is gone. Run one tool per command, for example: macos-harness get_app_state --app Finder",
    "unexpected_argument": "Unexpected argument {argument!r} for {tool}.",
    "install": "install takes --agent {agents} or all, and --scope {scopes}. Example: macos-harness install --agent {example} --scope user",
    "install_link": "{path} is behind a link, and this command does not write through links. Remove the link, then run the command again.",
    "install_hooks": "{path} is not a hooks file this command can read. Nothing was changed.",
}


def usage(key: str, **values: object) -> UsageError:
    return UsageError(USAGE[key].format(**values))


def server_error(description: str, *, tag: str | None = None) -> HarnessError:
    """An error with no code of its own. The model sees the code and the bare description."""
    return HarnessError(
        100, "server_error", f"{PRODUCT} error 100: {description}", tag=tag
    )


_RETRY = "Read the app state again, then retry with the new element IDs."

# Function name: (code, error name, text). Code None: a server error (100, the code
# and the bare text). Each {field} of the text but {product} is an argument, in order.
TEXTS: dict[str, tuple[int | None, str, str]] = {
    # the person
    "user_changed": (110, "user_took_over", "The user did something in '{app_path}' since your last read. Call `get_app_state` again before you act on it."),
    "still_interacting": (110, "user_took_over", "The user is working in '{app_path}' right now. Wait 5 seconds, then try again."),
    "turn_ended": (111, "turn_ended", "The turn is over, so {product} takes no more calls. It takes calls again after the user's next message."),
    "not_active": (112, "no_session", "No session is open for '{app}'. Call `get_app_state` for it first: every other {product} action needs that read."),
    # policy, approval, permissions
    "forbidden": (120, "app_refused", "For safety, {product} may not use the app '{bundle_id}'. Tell the user, and do not try to use it another way."),
    "denied_by_policy": (120, "app_refused", "Your policy file says {product} may not use the app '{bundle_id}'. Tell the user; only they can change that file."),
    "disabled": (120, "app_refused", "{product} is switched off in its policy file. Tell the user; only they can switch it on."),
    "security_process": (None, "", "'{bundle_id}' is a system password or security window, which {product} never touches. Ask the user to answer it."),
    "not_approved": (120, "not_approved", "The user did not allow {product} to use {display_name}. Do not use it in this turn; ask the user if you need it."),
    "approval_pending": (121, "approval_waiting", "Waiting for the user to allow {display_name} in the {product} panel. Call again after they answer."),
    "permissions_pending": (121, "permissions_waiting", "The user is still allowing {app} (the app that started the {product} helper) under {grants} in System Settings. Call this tool again now, and do not end your turn."),
    "permissions_not_granted": (122, "permissions_missing", "{product} cannot work without {grants}. Ask the user to allow {app} (the app that started the {product} helper) under {grants} in System Settings; `macos-harness doctor` lists them."),
    "blocked_url": (123, "page_off_limits", "{product} may not be used on the page that is open in the browser, so this session is over. Stop here and say why in your final message. For {product} the page stays off limits even when the user opens it themselves."),
    "approval_unavailable": (121, "approval_unavailable", "{product} cannot ask the user about \"{display_name}\" because its on-screen panel is switched off. Ask the user to approve \"{display_name}\" in their own terminal, then call this tool again."),
    "approval_not_saved": (100, "approval_not_saved", "{product} could not save the approval of '{bundle_id}' to disk, so it lasts only until the helper stops. Go on; the user can approve it again later."),
    "policy_unreadable": (120, "app_refused", "{product} cannot read its policy file '{path}': {reason}. Ask the user to fix or remove the file, then call this tool again."),
    # apps and windows
    "invalid_app": (130, "no_such_app", "No app called {identifier} was found on this Mac. Call `list_apps` and use a name or bundle id from it."),
    "no_file_panel": (130, "no_such_app", "No app you have read shows an open or save panel now. Call `get_app_state` for the app that opened it, then act on the panel there."),
    "app_not_running": (131, "app_not_running", "{identifier} is not running any more: it quit or crashed. Call `get_app_state` to open it again."),
    "accessibility": (132, "accessibility_failed", "An accessibility request to the app failed ({ax_error_name}). Call `get_app_state` again; if it fails again, click by pixel or tell the user."),
    "not_responding": (132, "app_not_responding", "{app_name} is not responding: it did not answer in time. It may be busy or frozen. Wait a few seconds and call get_app_state again. If it still fails, tell the user the app is frozen and stop using it."),
    "timeout_reached": (None, "", "The app started but showed no window within 5 seconds. Call `get_app_state` once more; if it fails again, tell the user."),
    "front_not_back": (None, "", "{product} could not put the user's front app back in front. Tell the user."),
    "no_windows": (None, "", "The app has no open window. Open one with its menu or a shortcut, or tell the user."),
    "invalid_process": (None, "", "No usable process id for {identifier}"),
    "no_capturable_window": (None, "", "No window of {identifier} can be captured"),
    "app_quit": (None, "", "The app quit during the call. Call `get_app_state` to open it again."),
    "failed_to_launch": (None, "", "macOS could not start the app. Tell the user."),
    "no_window_id": (None, "", "No window could be found for this element"),
    "element_outside_app": (None, "", "This element is drawn by another process and has no parent in the app"),
    # elements
    "stale_element": (None, "", "That element ID is from an older read. " + _RETRY),
    "invalid_element_id": (None, "", "No element has the ID {index} in the last read. Use an ID from the newest `get_app_state` text."),
    # actions
    "key_not_found": (None, "", 'No key is called "{name}"'),
    "key_chord": (None, "", "{case}"),  # one of the three chord texts of keys.py
    "no_text_to_type": (None, "", "No text was given to type"),
    "no_text_to_paste": (None, "", "No text was given to paste"),
    "no_value_to_set": (None, "", "No value was given to set"),
    "not_settable": (None, "", "The value of this element cannot be set"),
    "text_not_found": (None, "", "The element does not contain that text"),
    "cannot_select_text": (None, "", "This element does not let its text selection be set"),
    "invalid_secondary_action": (None, "", "{action} is not among the secondary actions of element {index}"),
    "cannot_click_offscreen": (None, "", "This element is off screen and cannot be clicked"),
    "invalid_click_count": (None, "", "Invalid click count"),
    "mouse_key_requires_single_key": (None, "", "A click takes one chord of keys, not a sequence"),
    "menu_click_failed": (None, "", "The menu item could not be clicked"),
    "menu_fills_on_open": (None, "", "The {title} menu is filled only when it opens on screen, which would take the user's mouse and keyboard. Use its keyboard shortcut (press_key) or a control in the window instead."),
    "menu_action_not_supported": (None, "", "A menu takes plain clicks only"),
    "path_in_file_name": (None, "", "The Save As field takes a file name, not a path: nothing was typed. Type the name alone. To choose the folder: press_key super+shift+g, set_value the path field (ID: PathTextField) to the folder, press_key Return, check the Where: pop-up, then click Save."),
    "paste_not_read": (None, "", "The app did not take the text from the clipboard in time"),
    "paste_write_failed": (None, "", "The text could not be put on the clipboard"),
    "paste_conflict": (None, "", "The clipboard changed during the paste, probably by the user. Read the app state and check that your text was pasted, not theirs, before you go on."),
    "no_keyboard_layout": (None, "", "The current keyboard layout could not be read."),
    "no_scroll_direction": (None, "", "No scroll direction was given"),
    "no_scroll_amount": (None, "", "No scroll amount was given"),
    "no_tree": (None, "", "The accessibility tree is missing."),
    "capture_failed": (None, "", "Reading the UI tree failed."),
    "screenshot_failed": (None, "", "Taking the screenshot failed."),
    "screenshot_size": (None, "", "The screenshot has an unusable size: {size}"),
    "no_invalidation_monitor": (None, "", "Internal error: this app session has no change monitor."),
    "skill_missing": (None, "", "the skill file is missing"),
    # the helper itself
    "helper_unavailable": (103, "helper_unavailable", "{product} could not start its helper. Run the command again. After 3 failures in a row, tell the user to run `macos-harness doctor`."),
    "helper_stopped": (103, "helper_stopped", "The {product} helper stopped during this call. Call `get_app_state` again before sending more actions."),
    "request_timed_out": (103, "request_timed_out", "{product} did not answer within 2 minutes, so the action may or may not have run. Call `get_app_state` to see the app before you try again."),
}  # fmt: skip
# The person did it (took over, said no): reported as such, not as a failure of the tool.
CANCELLED = {"user_changed", "still_interacting", "turn_ended", "not_approved", "blocked_url"}  # fmt: skip
ELEMENT_ID = {"stale_element", "invalid_element_id"}  # carry the tag invalid_element_id


def _maker(name: str, code: int | None, error_name: str, text: str) -> Callable:
    fields = dict.fromkeys(f for _, f, _, _ in string.Formatter().parse(text) if f)
    fields.pop("product", None)
    signature = inspect.Signature(
        [inspect.Parameter(f, inspect.Parameter.POSITIONAL_OR_KEYWORD) for f in fields]
    )

    def make(*args: object, **kwargs: object) -> HarnessError:
        values = signature.bind(*args, **kwargs).arguments
        message = text.format(**values, product=PRODUCT)
        if code is None:
            tag = "invalid_element_id" if name in ELEMENT_ID else None
            return server_error(message, tag=tag)
        return HarnessError(code, error_name, message, cancelled=name in CANCELLED)

    make.__name__ = make.__qualname__ = name
    make.__signature__ = signature
    return make


globals().update({name: _maker(name, *spec) for name, spec in TEXTS.items()})
del _maker  # every function left here words one error


# --- texts with a choice or a shape of their own


def locked(*, state_read: bool) -> HarnessError:
    text = (
        "The Mac is locked, so its accessibility text cannot be read. It has to be unlocked first."
        if state_read
        else "The Mac is locked. Ask the user to unlock it, then go on."
    )
    return HarnessError(134, "mac_locked", text)


def ambiguous_app(bundle_id: str, paths: list[str]) -> HarnessError:
    text = (
        f"More than one app has the bundle id '{bundle_id}': {', '.join(paths)}. "
        "Name the app, or give its full path."
    )
    return HarnessError(133, "several_apps_match", text)


def window_not_found_at(point: tuple[float, float]) -> HarnessError:
    return server_error(
        f"No window of the app is at ({float(point[0])!r}, {float(point[1])!r})"
    )


def ambiguous_refetch(*, started: bool) -> HarnessError:
    return server_error(
        "The element went away, and more than one element now fits its description, so looking "
        f"it up again could not be {'finished' if started else 'started'}. " + _RETRY,
        tag="invalid_element_id",
    )


def invalid_duration() -> UsageError:
    return usage("duration")


def invalid_scroll_pages() -> UsageError:
    return usage("pages")
