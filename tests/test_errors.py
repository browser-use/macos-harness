"""Every text the model can receive, letter for letter."""

from __future__ import annotations

import inspect

import pytest

from macos_harness import errors

P = "macOS Harness error 100: "
RETRY = "Read the app state again, then retry with the new element IDs."
REFETCH = (
    "The element went away, and more than one element now fits its description, so "
    "looking it up again could not be {}. " + RETRY
)

# function, arguments, code, name, the whole text
TEXTS = [
    (
        "server_error",
        (
            "The app started but showed no window within 5 seconds. Call `get_app_state` once more; if it fails again, tell the user.",
        ),
        100,
        "server_error",
        P
        + "The app started but showed no window within 5 seconds. Call `get_app_state` once more; if it fails again, tell the user.",
    ),
    (
        "user_changed",
        ("/Applications/Notes.app",),
        110,
        "user_took_over",
        (
            "The user did something in '/Applications/Notes.app' since your last "
            "read. Call `get_app_state` again before you act on it."
        ),
    ),
    (
        "still_interacting",
        ("/Applications/Notes.app",),
        110,
        "user_took_over",
        (
            "The user is working in '/Applications/Notes.app' right now. Wait 5 "
            "seconds, then try again."
        ),
    ),
    (
        "turn_ended",
        (),
        111,
        "turn_ended",
        (
            "The turn is over, so macOS Harness takes no more calls. It takes calls "
            "again after the user's next message."
        ),
    ),
    (
        "not_active",
        ("Notes",),
        112,
        "no_session",
        (
            "No session is open for 'Notes'. Call `get_app_state` for it first: every "
            "other macOS Harness action needs that read."
        ),
    ),
    (
        "forbidden",
        ("com.apple.Terminal",),
        120,
        "app_refused",
        (
            "For safety, macOS Harness may not use the app 'com.apple.Terminal'. "
            "Tell the user, and do not try to use it another way."
        ),
    ),
    (
        "denied_by_policy",
        ("com.example.mail",),
        120,
        "app_refused",
        (
            "Your policy file says macOS Harness may not use the app 'com.example.mail'. "
            "Tell the user; only they can change that file."
        ),
    ),
    (
        "disabled",
        (),
        120,
        "app_refused",
        (
            "macOS Harness is switched off in its policy file. Tell the user; only "
            "they can switch it on."
        ),
    ),
    (
        "security_process",
        ("com.apple.SecurityAgent",),
        100,
        "server_error",
        P + "'com.apple.SecurityAgent' is a system password or security window, which "
        "macOS Harness never touches. Ask the user to answer it.",
    ),
    (
        "not_approved",
        ("Clock",),
        120,
        "not_approved",
        (
            "The user did not allow macOS Harness to use Clock. Do not use it in this "
            "turn; ask the user if you need it."
        ),
    ),
    (
        "approval_pending",
        ("Clock",),
        121,
        "approval_waiting",
        (
            "Waiting for the user to allow Clock in the macOS Harness panel. Call this "
            "tool again now, and do not end your turn."
        ),
    ),
    (
        "approval_unavailable",
        ("Clock",),
        121,
        "approval_unavailable",
        (
            'macOS Harness cannot ask the user about "Clock" because its on-screen '
            'panel is switched off. Ask the user to approve "Clock" in their own '
            "terminal, then call this tool again."
        ),
    ),
    (
        "approval_not_saved",
        ("com.apple.clock",),
        100,
        "approval_not_saved",
        (
            "macOS Harness could not save the approval of 'com.apple.clock' to disk, "
            "so it lasts only until the helper stops. Go on; the user can approve it "
            "again later."
        ),
    ),
    (
        "no_file_panel",
        (),
        130,
        "no_such_app",
        (
            "No app you have read shows an open or save panel now. Call "
            "`get_app_state` for the app that opened it, then act on the panel there."
        ),
    ),
    (
        "policy_unreadable",
        ("/tmp/policy.toml", "bad value"),
        120,
        "app_refused",
        (
            "macOS Harness cannot read its policy file '/tmp/policy.toml': bad value. "
            "Ask the user to fix or remove the file, then call this tool again."
        ),
    ),
    (
        "permissions_pending",
        ("Terminal", "Screen Recording"),
        121,
        "permissions_waiting",
        (
            "The user is still allowing Terminal (the app that started the macOS "
            "Harness helper) under Screen Recording in System Settings. Call this "
            "tool again now, and do not end your turn."
        ),
    ),
    (
        "permissions_not_granted",
        ("Accessibility and Screen Recording", "Terminal"),
        122,
        "permissions_missing",
        (
            "macOS Harness cannot work without Accessibility and Screen Recording. Ask "
            "the user to allow Terminal (the app that started the macOS Harness "
            "helper) under Accessibility and Screen Recording in System Settings; "
            "`macos-harness doctor` lists them."
        ),
    ),
    (
        "blocked_url",
        (),
        123,
        "page_off_limits",
        (
            "macOS Harness may not be used on the page that is open in the browser, "
            "so this session is over. Stop here and say why in your final message. "
            "For macOS Harness the page stays off limits even when the user opens it "
            "themselves."
        ),
    ),
    (
        "invalid_app",
        ("NoSuchApp",),
        130,
        "no_such_app",
        "No app called NoSuchApp was found on this Mac. Call `list_apps` and use a name or bundle id from it.",
    ),
    (
        "ambiguous_app",
        ("org.example.app", ["/Applications/A.app", "/Users/me/A.app"]),
        133,
        "several_apps_match",
        (
            "More than one app has the bundle id 'org.example.app': "
            "/Applications/A.app, /Users/me/A.app. Name the app, or give its full "
            "path."
        ),
    ),
    (
        "app_not_running",
        ("Notes",),
        131,
        "app_not_running",
        "Notes is not running any more: it quit or crashed. Call `get_app_state` to open it again.",
    ),
    (
        "accessibility",
        ("AXError.cannotComplete",),
        132,
        "accessibility_failed",
        "An accessibility request to the app failed (AXError.cannotComplete). Call `get_app_state` again; if it fails again, click by pixel or tell the user.",
    ),
    (
        "not_responding",
        ("TextEdit",),
        132,
        "app_not_responding",
        (
            "TextEdit is not responding: it did not answer in time. It may be "
            "busy or frozen. Wait a few seconds and call get_app_state again. If it "
            "still fails, tell the user the app is frozen and stop using it."
        ),
    ),
    (
        "front_not_back",
        (),
        100,
        "server_error",
        P
        + "macOS Harness could not put the user's front app back in front. Tell the user.",
    ),
    (
        "timeout_reached",
        (),
        100,
        "server_error",
        P
        + "The app started but showed no window within 5 seconds. Call `get_app_state` once more; if it fails again, tell the user.",
    ),
    (
        "no_windows",
        (),
        100,
        "server_error",
        P
        + "The app has no open window. Open one with its menu or a shortcut, or tell the user.",
    ),
    (
        "window_not_found_at",
        ((2501, -908.0),),
        100,
        "server_error",
        P + "No window of the app is at (2501.0, -908.0)",
    ),
    (
        "stale_element",
        (),
        100,
        "server_error",
        P + "That element ID is from an older read. " + RETRY,
    ),
    (
        "invalid_element_id",
        (9999,),
        100,
        "server_error",
        P
        + "No element has the ID 9999 in the last read. Use an ID from the newest `get_app_state` text.",
    ),
    ("key_not_found", ("ESC",), 100, "server_error", P + 'No key is called "ESC"'),
    (
        "key_chord",
        ("This key does not exist on a Mac",),
        100,
        "server_error",
        P + "This key does not exist on a Mac",
    ),
    ("no_text_to_type", (), 100, "server_error", P + "No text was given to type"),
    (
        "not_settable",
        (),
        100,
        "server_error",
        P + "The value of this element cannot be set",
    ),
    (
        "text_not_found",
        (),
        100,
        "server_error",
        P + "The element does not contain that text",
    ),
    (
        "cannot_select_text",
        (),
        100,
        "server_error",
        P + "This element does not let its text selection be set",
    ),
    (
        "invalid_secondary_action",
        ("Explode", 2),
        100,
        "server_error",
        P + "Explode is not among the secondary actions of element 2",
    ),
    (
        "cannot_click_offscreen",
        (),
        100,
        "server_error",
        P + "This element is off screen and cannot be clicked",
    ),
    (
        "menu_click_failed",
        (),
        100,
        "server_error",
        P + "The menu item could not be clicked",
    ),
    (
        "path_in_file_name",
        (),
        100,
        "server_error",
        P + "The Save As field takes a file name, not a path: nothing was typed. Type "
        "the name alone. To choose the folder: press_key super+shift+g, set_value "
        "the path field (ID: PathTextField) to the folder, press_key Return, check "
        "the Where: pop-up, then click Save.",
    ),
    (
        "menu_fills_on_open",
        ("Game",),
        100,
        "server_error",
        P + "The Game menu is filled only when it opens on screen, which would take "
        "the user's mouse and keyboard. Use its keyboard shortcut (press_key) or a "
        "control in the window instead.",
    ),
    (
        "menu_action_not_supported",
        (),
        100,
        "server_error",
        P + "A menu takes plain clicks only",
    ),
    (
        "paste_not_read",
        (),
        100,
        "server_error",
        P + "The app did not take the text from the clipboard in time",
    ),
    (
        "paste_write_failed",
        (),
        100,
        "server_error",
        P + "The text could not be put on the clipboard",
    ),
    (
        "paste_conflict",
        (),
        100,
        "server_error",
        P
        + "The clipboard changed during the paste, probably by the user. Read the app "
        "state and check that your text was pasted, not theirs, before you go on.",
    ),
    (
        "no_keyboard_layout",
        (),
        100,
        "server_error",
        P + "The current keyboard layout could not be read.",
    ),
    ("capture_failed", (), 100, "server_error", P + "Reading the UI tree failed."),
    (
        "helper_unavailable",
        (),
        103,
        "helper_unavailable",
        (
            "macOS Harness could not start its helper. Run the command again. After 3 "
            "failures in a row, tell the user to run `macos-harness doctor`."
        ),
    ),
    (
        "helper_stopped",
        (),
        103,
        "helper_stopped",
        (
            "The macOS Harness helper stopped during this call. Call `get_app_state` "
            "again before sending more actions."
        ),
    ),
    (
        "request_timed_out",
        (),
        103,
        "request_timed_out",
        "macOS Harness did not answer within 2 minutes, so the action may or may not have run. Call `get_app_state` to see the app before you try again.",
    ),
    (
        "invalid_process",
        (4242,),
        100,
        "server_error",
        P + "No usable process id for 4242",
    ),
    (
        "no_capturable_window",
        ("Notes",),
        100,
        "server_error",
        P + "No window of Notes can be captured",
    ),
    (
        "app_quit",
        (),
        100,
        "server_error",
        P + "The app quit during the call. Call `get_app_state` to open it again.",
    ),
    ("invalid_duration", (), 0, "usage", "--duration is not a usable time"),
    (
        "no_scroll_direction",
        (),
        100,
        "server_error",
        P + "No scroll direction was given",
    ),
    ("no_scroll_amount", (), 100, "server_error", P + "No scroll amount was given"),
    ("no_tree", (), 100, "server_error", P + "The accessibility tree is missing."),
    ("invalid_scroll_pages", (), 0, "usage", "--pages takes a number above 0"),
    ("no_value_to_set", (), 100, "server_error", P + "No value was given to set"),
    ("no_text_to_paste", (), 100, "server_error", P + "No text was given to paste"),
    ("invalid_click_count", (), 100, "server_error", P + "Invalid click count"),
    (
        "mouse_key_requires_single_key",
        (),
        100,
        "server_error",
        P + "A click takes one chord of keys, not a sequence",
    ),
    (
        "no_window_id",
        (),
        100,
        "server_error",
        P + "No window could be found for this element",
    ),
    (
        "element_outside_app",
        (),
        100,
        "server_error",
        P + "This element is drawn by another process and has no parent in the app",
    ),
    (
        "failed_to_launch",
        (),
        100,
        "server_error",
        P + "macOS could not start the app. Tell the user.",
    ),
    (
        "no_invalidation_monitor",
        (),
        100,
        "server_error",
        (P + "Internal error: this app session has no change monitor."),
    ),
    ("skill_missing", (), 100, "server_error", P + "the skill file is missing"),
    ("screenshot_failed", (), 100, "server_error", P + "Taking the screenshot failed."),
    (
        "screenshot_size",
        ((0, 1184),),
        100,
        "server_error",
        P + "The screenshot has an unusable size: (0, 1184)",
    ),
]
KEYWORDS = {
    "locked": [
        (
            {"state_read": True},
            "The Mac is locked, so its accessibility text cannot be read. It has to be unlocked first.",
        ),
        (
            {"state_read": False},
            "The Mac is locked. Ask the user to unlock it, then go on.",
        ),
    ],
    "ambiguous_refetch": [
        ({"started": False}, P + REFETCH.format("started")),
        ({"started": True}, P + REFETCH.format("finished")),
    ],
}


@pytest.mark.parametrize(
    "name, args, code, error_name, text", TEXTS, ids=[row[0] for row in TEXTS]
)
def test_text_letter_for_letter(name, args, code, error_name, text):
    error = getattr(errors, name)(*args)
    assert (error.message, error.code, error.name) == (text, code, error_name)
    assert str(error) == text


def test_every_function_that_words_an_error_is_pinned_here():
    worded = {
        name
        for name, function in inspect.getmembers(errors, inspect.isfunction)
        if not name.startswith("_") and name != "usage"
    }
    assert worded == {row[0] for row in TEXTS} | set(KEYWORDS)


def test_what_the_person_did_is_not_reported_as_a_failure():
    cancelled = {
        name for name, args, *_ in TEXTS if getattr(errors, name)(*args).cancelled
    }
    assert cancelled == {
        "user_changed",
        "still_interacting",
        "turn_ended",
        "not_approved",
        "blocked_url",
    }


def test_server_errors_reach_model_shape():
    """A text of its own is printed as it is. Everything else is one shape: the
    product, the code, a colon, and the description untouched."""
    error = errors.server_error("someCase(42)", tag="x")
    assert error.message == "macOS Harness error 100: someCase(42)"
    assert (error.code, error.name, error.tag, error.cancelled) == (
        100,
        "server_error",
        "x",
        False,
    )
    assert not errors.invalid_app("X").message.startswith("macOS Harness error")


def test_accessibility_failures_named():
    for ax_name in (
        "AXError.cannotComplete",
        "AXError.invalidUIElement",
        "AXError.apiDisabled",
        "(undocumented AXError)",
    ):
        error = errors.accessibility(ax_name)
        assert error.message == (
            f"An accessibility request to the app failed ({ax_name}). Call "
            "`get_app_state` again; if it fails again, click by pixel or tell the user."
        )
        assert error.code == 132


def test_stale_element_id_text_tells_model_re_read():
    error = errors.stale_element()
    assert error.message.endswith(RETRY) and error.tag == "invalid_element_id"


@pytest.mark.parametrize("name", KEYWORDS)
def test_texts_with_two_forms(name):
    for kwargs, text in KEYWORDS[name]:
        assert getattr(errors, name)(**kwargs).message == text


def test_ambiguous_refetch_texts_variants():
    started, finished = (
        errors.ambiguous_refetch(started=flag) for flag in (False, True)
    )
    assert "could not be started." in started.message
    assert "could not be finished." in finished.message
    assert started.tag == finished.tag == "invalid_element_id"


def test_invalid_id_capture_failure_strings():
    assert (
        errors.invalid_element_id(9999).message
        == P
        + "No element has the ID 9999 in the last read. Use an ID from the newest `get_app_state` text."
    )
    assert errors.invalid_element_id(9999).tag == "invalid_element_id"
    assert errors.capture_failed().message == P + "Reading the UI tree failed."
    assert errors.no_tree().message == P + "The accessibility tree is missing."


def test_usage_texts_are_the_argument_checks():
    assert (
        errors.usage("selection_type", value="word").message
        == "--selection_type cannot be word. Use text, cursor_before or cursor_after."
    )
    assert (
        errors.usage("pixels").message
        == "scroll takes --pages here; --pixels is not supported."
    )
    assert isinstance(errors.usage("app"), errors.UsageError)
    assert errors.usage("app").code == 0 and errors.usage("app").name == "usage"


def test_usage_texts_the_other_modules_asked_for():
    said = errors.usage("unexpected_argument", argument="up", tool="click").message
    assert said == "Unexpected argument 'up' for click."
    said = errors.usage("install", agents="a, b", scopes="project or user", example="a")
    assert said.message == (
        "install takes --agent a, b or all, and --scope project or user. "
        "Example: macos-harness install --agent a --scope user"
    )
    assert errors.usage("install_link", path="/x/SKILL.md").message == (
        "/x/SKILL.md is behind a link, and this command does not write through "
        "links. Remove the link, then run the command again."
    )
    assert errors.usage("install_hooks", path="/x/settings.json").message == (
        "/x/settings.json is not a hooks file this command can read. Nothing was changed."
    )


def test_system_framework_errors_pass_through_raw():
    """What a system framework says goes to the model as it is, under the one shape."""
    raw = (
        'Error Domain=NSCocoaErrorDomain Code=256 "The file “Phone” couldn’t be '
        'opened." UserInfo={NSUnderlyingError=0x1 {Error Domain=NSOSStatusErrorDomain '
        "Code=-600}}"
    )
    assert errors.server_error(raw).message == P + raw
