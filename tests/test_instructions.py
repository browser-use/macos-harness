"""App notes: found by bundle id, printed once, replaced by a note of the person."""

from __future__ import annotations

import re

import pytest

from macos_harness import instructions, protocol, tools
from macos_harness.model import AgentSession

SLACK = "com.tinyspeck.slackmacgap"
NUMBERS = "com.apple.iWork.Numbers"
CHROME = "com.google.Chrome"
SHIPPED = {
    path.stem: path.read_text("utf-8") for path in instructions.BUILT_IN.glob("*.md")
}


@pytest.fixture
def personal(tmp_path, monkeypatch):
    folder = tmp_path / "personal"
    folder.mkdir()
    monkeypatch.setattr(instructions, "personal_dir", lambda: folder)
    return folder


def agent() -> AgentSession:
    return AgentSession(key="one agent")


def flat(text: str) -> str:
    return " ".join(text.lower().split())


# --- the block: once per agent session and app ----------------------------------------


def test_block_goes_before_the_first_state_of_an_app_once(personal):
    # Once per agent session and bundle id.
    one, two = agent(), agent()
    block = instructions.prefix(one, SLACK)
    assert block.startswith("<app_notes>\n## Slack\n")
    assert block.endswith("\n</app_notes>\n")
    assert "\n\n</app_specific" not in block  # the file's last newline is not doubled
    assert instructions.prefix(one, SLACK) is None
    assert instructions.prefix(one, SLACK.upper()) is None  # same app, other spelling
    assert instructions.prefix(two, SLACK) == block  # another agent has not seen it
    assert "## Spotify" in instructions.prefix(one, "com.spotify.client")


def test_spreadsheet_note_ships_and_is_never_printed(personal):
    assert NUMBERS in SHIPPED
    one = agent()
    assert instructions.prefix(one, NUMBERS) is None
    (personal / f"{NUMBERS}.md").write_text("mine")
    assert instructions.prefix(one, NUMBERS) is None
    assert one.instructed == set()


def test_app_without_a_note_gets_nothing_and_is_not_marked(personal):
    one = agent()
    assert instructions.prefix(one, "com.apple.finder") is None
    assert one.instructed == set()


@pytest.mark.parametrize(
    "bundle_id",
    [
        CHROME,
        "com.google.Chrome.canary",
        "com.google.chrome.for.testing",
        "com.apple.Safari",
        "com.apple.SafariTechnologyPreview",
        "org.mozilla.firefox",
        "com.microsoft.edgemac",
        "company.thebrowser.Browser",
    ],
)
def test_every_browser_gets_the_one_browser_note(personal, bundle_id):
    assert "## Browser use\n" in instructions.prefix(agent(), bundle_id)


@pytest.mark.parametrize("bundle_id", ["org.mozilla.thunderbird", "com.apple.mail"])
def test_an_app_that_is_no_browser_gets_no_browser_note(personal, bundle_id):
    assert instructions.prefix(agent(), bundle_id) is None


# --- the person's own notes -----------------------------------------------------------


def test_personal_folder_is_in_the_state_folder():
    assert instructions.personal_dir() == protocol.state_dir() / "instructions"


def test_personal_note_replaces_the_built_in_one_completely(personal):
    (personal / f"{SLACK}.md").write_text("Never post in #general.\n")
    block = instructions.prefix(agent(), SLACK)
    assert block == ("<app_notes>\nNever post in #general.\n</app_notes>\n")


def test_personal_note_for_an_app_we_ship_nothing_for(personal):
    (personal / "com.apple.finder.md").write_text("Use list view.")
    assert "Use list view." in instructions.prefix(agent(), "com.apple.Finder")


def test_personal_browser_note_covers_all_browsers_and_one_browser_wins(personal):
    (personal / "browser.md").write_text("all browsers")
    (personal / f"{CHROME}.md").write_text("only this one")
    assert "only this one" in instructions.prefix(agent(), CHROME)
    assert "all browsers" in instructions.prefix(agent(), "com.apple.Safari")


def test_empty_personal_note_switches_the_built_in_one_off(personal):
    (personal / f"{SLACK}.md").write_text("\n")
    one = agent()
    assert instructions.prefix(one, SLACK) is None
    assert one.instructed == set()


def test_note_that_cannot_be_read_never_fails_a_state_read(personal):
    (personal / f"{SLACK}.md").write_bytes(b"\xff\xfe not text")
    assert instructions.prefix(agent(), SLACK) is None


def test_missing_personal_folder_is_fine(tmp_path, monkeypatch):
    monkeypatch.setattr(instructions, "personal_dir", lambda: tmp_path / "none")
    assert "## Slack" in instructions.prefix(agent(), SLACK)


# --- the shipped texts ----------------------------------------------------------------

NOTES = {
    "browser: a new tab for a new site or task": (
        "browser",
        [
            "open a new tab",
            "new website",
            "separate web task",
            "asked to continue there",
        ],
    ),
    "Slack 1: check focus before Return": (
        SLACK,
        ["when no text field has focus", "before you press `return`", "by accident"],
    ),
    "Slack 2: set_value for the composer": (
        SLACK,
        ["with `set_value`, not `type_text`", "markdown marks", "`super+shift+f`"],
    ),
    "Slack 3: what a newline does": (
        SLACK,
        ["`set_value` adds a new line and does not send", "`type_text` **sends**"],
    ),
    "Slack 4: the hint under the composer": (
        SLACK,
        [
            '"(key combination) to add a new line"',
            "the other combination sends",
            "3 or more characters",
            "not in thread replies or edits",
        ],
    ),
    "Slack 5: trust the screenshot": (SLACK, ["trust the screenshot"]),
    "iPhone Mirroring: three shortcuts": (
        "com.apple.ScreenContinuity",
        ["`super+1` home screen", "`super+2` app switcher", "`super+3` spotlight"],
    ),
    "iPhone Mirroring: scroll, and icon centers": (
        "com.apple.ScreenContinuity",
        ["scroll with `scroll`, never with `drag`", "the middle of its icon"],
    ),
    "Notion": (
        "notion.id",
        [
            "made of blocks",
            "one line at a time",
            "`press_key` with `return`",
            '"new page"',
            "`super+a` twice",
            "never try to select or delete it",
            "`shift+return`",
        ],
    ),
    "Spotify: read again after starting playback": (
        "com.spotify.client",
        ["do not act again", "run `get_app_state` once more", "do not sleep"],
    ),
    "Spotify: links": (
        "com.spotify.client",
        ["`open.spotify.com` in place of `xpui.app.spotify.com`", "word for word"],
    ),
    "Music": (
        "com.apple.Music",
        ["double-click a track to play it", "`--click_count 2`", "`set_value`"],
    ),
    "Numbers": (
        NUMBERS,
        ["one click", "three clicks (`--click_count 3`)", "never enter several rows"],
    ),
    "Clock": (
        "com.apple.clock",
        [
            "### add a city to the world clock",
            "### start a timer",
            "### start a stopwatch",
            "### make an alarm",
            "type the number with `type_text`. never use `set_value` here",
            "23:59:59",
        ],
    ),
}


@pytest.mark.parametrize("row", NOTES)
def test_note_carries_the_rule(row):
    name, phrases = NOTES[row]
    assert [phrase for phrase in phrases if phrase not in flat(SHIPPED[name])] == []


def test_the_seven_app_notes_and_the_browser_note_ship():
    assert sorted(SHIPPED) == sorted({name for name, _ in NOTES.values()})
    for text in SHIPPED.values():
        assert text.startswith("## ") and text.endswith("\n")


@pytest.mark.parametrize("name", SHIPPED)
def test_notes_name_tools_and_flags_as_the_tool_surface_does(name):
    # One naming style. No older spelling of a tool, no tool we do not have.
    text = SHIPPED[name]
    assert not re.search(r"\b(get|set|type|press|select)-[a-z]+\b", text)
    snake = set(re.findall(r"`([a-z]+(?:_[a-z]+)+)`", text))
    assert snake <= set(tools.TOOLS)
    assert set(re.findall(r"--([a-z_]+)", text)) <= set(tools.PARAMS)
