"""Key names, chords and the layout search. No event is posted here."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from fakes_e import load

from macos_harness import errors, keys

SHIFT, CONTROL, OPTION, COMMAND = keys.SHIFT, keys.CONTROL, keys.OPTION, keys.COMMAND
ROWS = json.loads((Path(__file__).parent / "fixtures/keys/table.json").read_text())
# name, key code, flags, no Mac key. `equal` is the one entry we changed on purpose.
DECODED = {row[0]: row[1:] for row in ROWS}


def error(call, *args) -> str:
    with pytest.raises(errors.HarnessError) as caught:
        call(*args)
    return caught.value.message.split(": ", 1)[1]


def test_key_table_has_the_210_names_with_their_codes():
    assert len(keys.TABLE) == len(DECODED) == 210
    for name, (code, flags, no_key) in DECODED.items():
        if name == "equal":
            assert keys.TABLE[name] == (24, 0) and (code, flags) == (24, SHIFT)
        else:
            assert keys.TABLE[name] == (code, flags), name
        assert (keys.TABLE[name] == (None, 0)) == no_key


def test_key_table_editing_control_navigation_names():
    names = "BackSpace Tab Linefeed Clear Return Escape Delete Home Left Up Right Down"
    codes = [keys.TABLE[name][0] for name in names.split()]
    assert codes == [51, 48, 36, 71, 36, 53, 117, 115, 123, 126, 124, 125]
    names = "Prior Page_Up Next Page_Down End Begin"
    paging = [keys.TABLE[name][0] for name in names.split()]
    assert paging == [116, 116, 121, 121, 119, 115]


def test_key_table_misc_function_names():
    assert keys.TABLE["Menu"] == (110, 0) and keys.TABLE["Help"] == (114, 0)
    names = "Select Print Execute Insert Undo Redo Find Cancel Break Num_Lock"
    for name in names.split():
        assert error(keys.chords, name) == "This key does not exist on a Mac"


def test_key_table_keypad_names():
    digits = [keys.TABLE[f"KP_{n}"][0] for n in range(10)]
    assert digits == [82, 83, 84, 85, 86, 87, 88, 89, 91, 92]
    assert keys.chords("KP_Enter KP_Add") == [keys.Chord(76, 0), keys.Chord(69, 0)]
    assert error(keys.chords, "KP_Home") == "This key does not exist on a Mac"


def test_key_table_function_keys_stop_f20():
    assert keys.chords("F1 F20") == [keys.Chord(122, 0), keys.Chord(90, 0)]
    assert error(keys.chords, "F21") == 'No key is called "F21"'


def test_key_table_left_right_modifier_keysyms():
    sides = {"Shift": SHIFT, "Control": CONTROL, "Alt": OPTION, "Meta": COMMAND}
    for name, flag in {**sides, "Super": COMMAND}.items():
        assert keys.TABLE[name + "_L"] == keys.TABLE[name + "_R"] == (None, flag)
    assert keys.TABLE["Caps_Lock"] == keys.TABLE["Shift_Lock"] == (57, 0)
    assert error(keys.chords, "Hyper_L+a") == "This key does not exist on a Mac"


def test_key_table_lowercase_modifier_aliases():
    for name in ("super", "meta", "command", "Command", "cmd"):
        assert keys.chords(name + "+a") == [keys.Chord(0, COMMAND)]
    assert keys.chords("alt+ctrl+shift+a") == [keys.Chord(0, OPTION | CONTROL | SHIFT)]


def test_key_table_punctuation_keysyms_digits_letters():
    assert keys.chords("a A 1 exclam") == [
        keys.Chord(0, 0),
        keys.Chord(0, SHIFT),
        keys.Chord(18, 0),
        keys.Chord(18, SHIFT),
    ]
    assert keys.chords("minus underscore") == [keys.Chord(27, 0), keys.Chord(27, SHIFT)]
    assert keys.chords("space")[0].code == 49


def test_call_chord_no_sequence_syntax_no_repeat_count():
    """A space ends a chord: three names are three presses, in order."""
    assert keys.chords("Down Down Return") == [
        keys.Chord(125, 0),
        keys.Chord(125, 0),
        keys.Chord(36, 0),
    ]
    assert keys.chords("super+shift+g") == [keys.Chord(5, COMMAND | SHIFT)]
    assert keys.chords("super+A") == [keys.Chord(0, COMMAND | SHIFT)]
    assert keys.chords("  Tab   Tab ") == [keys.Chord(48, 0)] * 2


@pytest.mark.parametrize(
    ("spec", "text"),
    [
        ("NoSuchKey", 'No key is called "NoSuchKey"'),
        ("super+nope", 'No key is called "nope"'),
        ("fn", 'No key is called "fn"'),
        ("Pause", "This key does not exist on a Mac"),
        ("a+b", "A chord takes only one key that is not a modifier"),
        ("a+shift", "A chord takes only one key that is not a modifier"),
        ("shift", "A chord needs one key that is not a modifier"),
        ("super+shift", "A chord needs one key that is not a modifier"),
        ("+", "A chord needs one key that is not a modifier"),
        ("", "A chord needs one key that is not a modifier"),
        ("Tab nope", 'No key is called "nope"'),  # every name is looked up first
        ("shift nope", 'No key is called "nope"'),
    ],
)
def test_chord_parse_errors_are_bare_names(spec, text):
    assert error(keys.chords, spec) == text


@pytest.mark.parametrize(
    ("spec", "chord"),
    [
        ("Ctrl+a", (0, CONTROL)),
        ("Control+a", (0, CONTROL)),
        ("Alt+a", (0, OPTION)),
        ("Option+a", (0, OPTION)),
        ("opt+a", (0, OPTION)),
        ("Shift+Tab", (48, SHIFT)),
        ("CMD+a", (0, COMMAND)),
        ("COMMAND+a", (0, COMMAND)),
        ("Enter", (36, 0)),
        ("ESC", (53, 0)),
        ("Esc", (53, 0)),
        ("PageUp", (116, 0)),
        ("pagedown", (121, 0)),
        ("Del", (117, 0)),
        ("ctrl + a", (0, CONTROL)),  # spaces around + are dropped
        ("cmd +shift+ z", (6, COMMAND | SHIFT)),
        ("equal", (24, 0)),  # the key itself, no shift
        ("plus", (24, SHIFT)),
        ("return", (36, 0)),  # any case, when exactly one name matches
        ("backspace", (51, 0)),
        ("TAB", (48, 0)),
        ("escape", (53, 0)),
        ("down", (125, 0)),
        ("f5", (96, 0)),
        ("arrowup", (126, 0)),
        ("ArrowDown", (125, 0)),
        ("arrowleft", (123, 0)),
        ("arrowright", (124, 0)),
        ("spacebar", (49, 0)),
        ("a", (0, 0)),  # one letter keeps its case
        ("A", (0, SHIFT)),
    ],
)
def test_aliases_the_table_lacks(spec, chord):
    assert keys.chords(spec) == [keys.Chord(*chord)]


def test_strict_names_when_the_fix_is_off(monkeypatch):
    monkeypatch.setattr(keys, "LENIENT", False)
    assert error(keys.chords, "ESC") == 'No key is called "ESC"'
    assert error(keys.chords, "return") == 'No key is called "return"'
    assert (
        error(keys.chords, "ctrl + a") == "A chord needs one key that is not a modifier"
    )


COMMON_KEY_STRINGS = (
    "Return super+shift+g super+a Down Escape super+t BackSpace super+Left Tab super+k "
    "super+l alt+shift+Tab super+comma r super+Down super+o Right super+p space "
    "super+space super+shift+n alt+Tab Home Left super+f super+shift+p super+z super+w "
    "super+r"
)


@pytest.mark.parametrize("spec", COMMON_KEY_STRINGS.split())
def test_common_key_strings_parse(spec):
    names = spec.split("+")
    code = DECODED[names[-1]][0]
    flags = sum({DECODED[name][1] for name in names})
    assert keys.chords(spec) == [keys.Chord(code, flags)]


def test_held_key_of_a_click_is_modifiers_only():
    assert keys.modifiers("shift") == SHIFT
    assert keys.modifiers("cmd+Shift") == COMMAND | SHIFT
    assert (
        error(keys.modifiers, "a") == "A click takes one chord of keys, not a sequence"
    )
    assert (
        error(keys.modifiers, "") == "A click takes one chord of keys, not a sequence"
    )


def test_key_hold_key_down_beyond_tap(monkeypatch):
    """press_key takes a hold. The check of 0 to 30 s is the tool layer's."""
    actions = load("actions")
    assert "hold" in inspect.signature(actions.press_key).parameters
    assert errors.usage("duration").message == "--duration is not a usable time"


# --- the layout search --------------------------------------------------------------

US = {(0, 0): "a", (0, 512): "A", (18, 0): "1", (18, 512): "!", (36, 0): "\r"}
US |= {(48, 0): "\t", (49, 0): " ", (19, 2560): "€", (14, 2048): "´", (0, 4096): "\x01"}
US |= {(5, 0): "a"}  # a second key that also makes "a": the first one wins


def us(code, mods):
    return US.get((code, mods))


def test_typing_maps_characters_key_codes_through_current_keyboard():
    found = keys.presses("aA1!€", us)
    assert found == [
        keys.KeyPress(0, 0, "a"),
        keys.KeyPress(0, SHIFT, "A"),
        keys.KeyPress(18, 0, "1"),
        keys.KeyPress(18, SHIFT, "!"),
        keys.KeyPress(19, SHIFT | OPTION, "€"),
    ]


def test_layout_search_runs_8_modifier_states_by_128_key_codes():
    asked = []
    keys.presses("x", lambda code, mods: asked.append((code, mods)))
    order = [0, 512, 2048, 2560, 4096, 4608, 6144, 6656]
    assert asked == [(code, mods) for mods in order for code in range(128)]
    flags = [flags for _, flags in keys._STATES]
    assert flags[:4] == [0, SHIFT, OPTION, SHIFT | OPTION] and flags[4] == CONTROL


def test_newline_becomes_carriage_return():
    assert keys.presses("\n", us) == [keys.KeyPress(36, 0, "\r")]
    # A line end of two characters is one Return, not two (text from a Windows file).
    assert keys.presses("\r\n", us) == [keys.KeyPress(36, 0, "\r")]


def test_tab_character_becomes_tab_key_press():
    assert keys.presses("\t", us) == [keys.KeyPress(48, 0, "\t")]


def test_direct_unicode_string_typing_path(monkeypatch):
    """A character the layout has no key for is typed, not dropped: its key press
    carries the character. One press per character, also outside the 16 bit range."""
    assert keys.presses("aé✓😀", us) == [
        keys.KeyPress(0, 0, "a"),
        keys.KeyPress(0, 0, "é"),
        keys.KeyPress(0, 0, "✓"),
        keys.KeyPress(0, 0, "😀"),
    ]
    monkeypatch.setattr(keys, "TYPE_UNMATCHED", False)
    assert keys.presses("aé", us) == [keys.KeyPress(0, 0, "a")]


def test_no_keyboard_layout_is_its_own_error(monkeypatch):
    class NoLayout:
        def __getattr__(self, name):
            return lambda *args: 0

    monkeypatch.setattr(keys.ctypes, "CDLL", lambda path: NoLayout())
    assert (
        error(keys.current_layout) == "The current keyboard layout could not be read."
    )


def test_current_layout_finds_the_plain_keys():
    pytest.importorskip("Quartz")
    typed = keys.presses("a\n\t ")
    assert [press.code for press in typed] == [0, 36, 48, 49]
