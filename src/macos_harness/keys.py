"""Key names to key codes, and text to key presses.

Two tables with two jobs. `TABLE` gives a named key its fixed US key code: a chord
means the same key whatever the keyboard layout is. `presses` asks the current
layout which key makes each character: typed text arrives as written.
"""

from __future__ import annotations

import ctypes
import re
from collections.abc import Callable
from dataclasses import dataclass

from . import errors

SHIFT, CONTROL, OPTION, COMMAND = 0x20000, 0x40000, 0x80000, 0x100000

# Fixes on top of the strict name table: other spellings (ALIASES), any case for a
# name of two or more letters, spaces around "+", and a plain `equal`. Off = a name
# must be in TABLE letter for letter.
LENIENT = True
# A character the layout has no key for is sent in a key event that carries it.
# Off = it is skipped without a word.
TYPE_UNMATCHED = True

_PLAIN = (
    "BackSpace=51 Tab=48 Linefeed=36 Clear=71 Return=36 Escape=53 Delete=117 Home=115 "
    "Left=123 Up=126 Right=124 Down=125 Prior=116 Page_Up=116 Next=121 Page_Down=121 "
    "End=119 Begin=115 Menu=110 Help=114 KP_Delete=71 KP_Enter=76 KP_Equal=81 "
    "KP_Multiply=67 KP_Add=69 KP_Subtract=78 KP_Decimal=65 KP_Divide=75 KP_0=82 "
    "KP_1=83 KP_2=84 KP_3=85 KP_4=86 KP_5=87 KP_6=88 KP_7=89 KP_8=91 KP_9=92 F1=122 "
    "F2=120 F3=99 F4=118 F5=96 F6=97 F7=98 F8=100 F9=101 F10=109 F11=103 F12=111 "
    "F13=105 F14=107 F15=113 F16=106 F17=64 F18=79 F19=80 F20=90 Caps_Lock=57 "
    "Shift_Lock=57 space=49 apostrophe=39 comma=43 minus=27 period=47 slash=44 0=29 "
    "1=18 2=19 3=20 4=21 5=23 6=22 7=26 8=28 9=25 semicolon=41 bracketleft=33 "
    "backslash=42 bracketright=30 grave=50 a=0 b=11 c=8 d=2 e=14 f=3 g=5 h=4 i=34 "
    "j=38 k=40 l=37 m=46 n=45 o=31 p=35 q=12 r=15 s=1 t=17 u=32 v=9 w=13 x=7 y=16 z=6"
)
_SHIFTED = (
    "exclam=18 quotedbl=39 numbersign=20 dollar=21 percent=23 ampersand=26 "
    "parenleft=25 parenright=29 asterisk=28 plus=24 colon=41 less=43 equal=24 "
    "greater=47 question=44 at=19 A=0 B=11 C=8 D=2 E=14 F=3 G=5 H=4 I=34 J=38 K=40 "
    "L=37 M=46 N=45 O=31 P=35 Q=12 R=15 S=1 T=17 U=32 V=9 W=13 X=7 Y=16 Z=6 "
    "asciicircum=22 underscore=27 braceleft=33 bar=42 braceright=30 asciitilde=50"
)
_MODIFIERS = {
    SHIFT: "Shift_L Shift_R shift",
    CONTROL: "Control_L Control_R ctrl",
    OPTION: "Alt_L Alt_R alt",
    COMMAND: "Meta_L Meta_R Super_L Super_R super meta command Command cmd",
}
_NO_MAC_KEY = (
    "Pause Scroll_Lock Sys_Req Back Select Print Execute Insert Undo Redo Find Cancel "
    "Break Mode_switch script_switch Num_Lock KP_Space KP_Tab KP_F1 KP_F2 KP_F3 KP_F4 "
    "KP_Home KP_Left KP_Up KP_Right KP_Down KP_Prior KP_Page_Up KP_Next KP_Page_Down "
    "KP_End KP_Begin KP_Insert KP_Separator Hyper_L Hyper_R"
)


def _pairs(text: str, flags: int) -> dict[str, tuple[int | None, int]]:
    return {n: (int(c), flags) for n, c in (item.split("=") for item in text.split())}


# name -> (key code, modifier flags). (None, flags) is a modifier, (None, 0) a name
# with no key on a Mac. 210 names, matched letter for letter.
TABLE: dict[str, tuple[int | None, int]] = {
    **_pairs(_PLAIN, 0),
    **_pairs(_SHIFTED, SHIFT),
    **{n: (None, flag) for flag, names in _MODIFIERS.items() for n in names.split()},
    **{name: (None, 0) for name in _NO_MAC_KEY.split()},
}
if LENIENT:
    TABLE["equal"] = (24, 0)  # the "=" key itself, not "+"

# Other spellings, matched ignoring case, each naming a TABLE entry.
ALIASES = {
    **{n: "ctrl" for n in ("control", "ctrl")},
    **{n: "alt" for n in ("option", "opt", "alt")},
    **{n: "cmd" for n in ("cmd", "command", "super", "meta")},
    "shift": "shift",
    "enter": "Return",
    "esc": "Escape",
    "pageup": "Page_Up",
    "pagedown": "Page_Down",
    "del": "Delete",
    "arrowup": "Up",
    "arrowdown": "Down",
    "arrowleft": "Left",
    "arrowright": "Right",
    "spacebar": "space",
}


@dataclass(frozen=True)
class Chord:
    code: int
    flags: int


@dataclass(frozen=True)
class KeyPress:
    code: int
    flags: int
    text: str  # sent as the event's own characters


def _lookup(name: str) -> tuple[int | None, int]:
    if name in TABLE:
        return TABLE[name]
    if LENIENT:
        if alias := ALIASES.get(name.lower()):
            return TABLE[alias]
        same = [n for n in TABLE if n.lower() == name.lower()] if len(name) > 1 else []
        if len(same) == 1:
            return TABLE[same[0]]
    raise errors.key_not_found(name)


def _names(spec: str) -> list[list[str]]:
    """A space ends a chord, "+" joins the names of one chord."""
    if LENIENT:
        spec = re.sub(r" *\+ *", "+", spec)
    return [[n for n in chord.split("+") if n] for chord in spec.split(" ") if chord]


def chords(spec: str) -> list[Chord]:
    """`super+shift+g` is one press, `Down Down Return` is three, in order."""
    found = [[_lookup(name) for name in chord] for chord in _names(spec)]
    out = []
    for chord in found or [[]]:
        code, flags = None, 0
        for key_code, key_flags in chord:
            if (key_code, key_flags) == (None, 0):
                raise errors.key_chord("This key does not exist on a Mac")
            if code is not None:
                raise errors.key_chord(
                    "A chord takes only one key that is not a modifier"
                )
            code, flags = key_code, flags | key_flags
        if code is None:
            raise errors.key_chord("A chord needs one key that is not a modifier")
        out.append(Chord(code, flags))
    return out


def modifiers(spec: str) -> int:
    """The flags of a chord of modifiers only, for a key held during a click."""
    found = [_lookup(name) for chord in _names(spec) for name in chord]
    if not found or any(code is not None or not flags for code, flags in found):
        raise errors.mouse_key_requires_single_key()
    return sum({flags for _, flags in found})


# The 8 modifier states a character is searched in, in this order: Carbon bits and
# the event flags they become.
_STATES = [
    (shift | option | control, s * SHIFT | o * OPTION | c * CONTROL)
    for c, control in ((0, 0), (1, 4096))
    for o, option in ((0, 0), (1, 2048))
    for s, shift in ((0, 0), (1, 512))
]
Layout = Callable[[int, int], str | None]  # (key code, Carbon modifiers) -> character


def current_layout() -> Layout:
    """The translate call of the keyboard layout that is active right now."""
    carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
    carbon.TISCopyCurrentKeyboardLayoutInputSource.restype = ctypes.c_void_p
    carbon.TISGetInputSourceProperty.restype = ctypes.c_void_p
    carbon.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p] * 2
    carbon.CFDataGetBytePtr.restype = ctypes.c_void_p
    carbon.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
    carbon.LMGetKbdType.restype = ctypes.c_uint8
    c32, c16, size = ctypes.c_uint32, ctypes.c_uint16, ctypes.c_ulong
    pointer = ctypes.POINTER
    types = [ctypes.c_void_p, c16, c16, c32, c32, c32, pointer(c32), size]
    carbon.UCKeyTranslate.argtypes = [*types, pointer(size), pointer(c16)]
    source = carbon.TISCopyCurrentKeyboardLayoutInputSource()
    if not source:
        raise errors.no_keyboard_layout()
    name = ctypes.c_void_p.in_dll(carbon, "kTISPropertyUnicodeKeyLayoutData")
    data = carbon.TISGetInputSourceProperty(source, name)
    if not data:
        raise errors.no_keyboard_layout()
    layout, keyboard = carbon.CFDataGetBytePtr(data), carbon.LMGetKbdType()

    def translate(code: int, mods: int) -> str | None:
        dead, count, chars = c32(0), size(0), (c16 * 4)()
        failed = carbon.UCKeyTranslate(
            layout, code, 3, (mods >> 8) & 0xFF, keyboard, 0, dead, 4, count, chars
        )
        return None if failed or not count.value else chr(chars[0])

    return translate


def presses(text: str, layout: Layout | None = None) -> list[KeyPress]:
    """One key press per character: the first of 8 modifier states by 128 key codes
    that makes it. A newline is the Return key."""
    layout = layout or current_layout()
    found: dict[str, tuple[int, int]] = {}
    for mods, flags in _STATES:
        for code in range(128):
            if char := layout(code, mods):
                found.setdefault(char, (code, flags))
    out = []
    for char in text.replace("\r\n", "\n").replace("\n", "\r"):  # one Return a line
        if char in found:
            out.append(KeyPress(*found[char], char))
        elif TYPE_UNMATCHED:
            out.append(KeyPress(0, 0, char))
    return out
