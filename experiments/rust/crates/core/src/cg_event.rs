//! Keyboard and mouse input posted directly to a target PID via CGEvent.
//!
//! We never use `CGEventPost` (the global tap) or `CGWarpMouseCursorPosition`
//! — input is always posted to a specific process PID with `CGEventPostToPid`,
//! so the physical pointer is never moved and the target app is never
//! activated/raised, matching the Python harness invariants.

use objc2_core_foundation::{CFRetained, CGPoint};
use objc2_core_graphics::{
    CGEvent, CGEventField, CGEventFlags, CGEventSource, CGEventSourceStateID, CGEventType,
    CGMouseButton, CGKeyCode,
};

use crate::error::{HarnessError, InputError, Result};

/// A private (non-global) event source. Mirrors
/// `AS.CGEventSourceCreate(kCGEventSourceStatePrivate)`.
struct EventSource {
    source: CFRetained<CGEventSource>,
}

impl EventSource {
    fn new() -> Result<Self> {
        let source = CGEventSource::new(CGEventSourceStateID::Private)
            .ok_or_else(|| HarnessError::other("could not create a private CG event source"))?;
        Ok(Self { source })
    }
}

/// USB virtual keycodes (kVK_*), matching the Python `_KEYCODES` table.
#[rustfmt::skip]
const KEYCODES: &[(&str, u16)] = &[
    ("a", 0), ("s", 1), ("d", 2), ("f", 3), ("h", 4), ("g", 5),
    ("z", 6), ("x", 7), ("c", 8), ("v", 9), ("b", 11), ("q", 12),
    ("w", 13), ("e", 14), ("r", 15), ("y", 16), ("t", 17),
    ("1", 18), ("2", 19), ("3", 20), ("4", 21), ("6", 22), ("5", 23),
    ("=", 24), ("9", 25), ("7", 26), ("-", 27), ("8", 28), ("0", 29),
    ("]", 30), ("o", 31), ("u", 32), ("[", 33), ("i", 34), ("p", 35),
    ("return", 36), ("enter", 36), ("l", 37), ("j", 38), ("'", 39),
    ("k", 40), (";", 41), ("\\", 42), (",", 43), ("/", 44), ("n", 45),
    ("m", 46), (".", 47), ("tab", 48), ("space", 49), ("`", 50),
    ("backspace", 51), ("delete", 51), ("escape", 53), ("esc", 53),
    ("home", 115), ("pageup", 116), ("page_up", 116), ("forward_delete", 117),
    ("end", 119), ("pagedown", 121), ("page_down", 121),
    ("left", 123), ("right", 124), ("down", 125), ("up", 126),
];

/// Modifier key virtual keycodes (kVK_*), matching the Python
/// `_MODIFIER_KEYCODES` table.
#[rustfmt::skip]
const MODIFIER_KEYCODES: &[(&str, u16)] = &[
    ("cmd", 55), ("command", 55), ("super", 55),
    ("shift", 56), ("alt", 58), ("option", 58),
    ("ctrl", 59), ("control", 59),
];

/// Modifier key -> CGEventFlags bit, matching the Python `_MODIFIER_FLAGS`.
fn modifier_flag(name: &str) -> Option<CGEventFlags> {
    match name {
        "cmd" | "command" | "super" => Some(CGEventFlags::MaskCommand),
        "ctrl" | "control" => Some(CGEventFlags::MaskControl),
        "alt" | "option" => Some(CGEventFlags::MaskAlternate),
        "shift" => Some(CGEventFlags::MaskShift),
        _ => None,
    }
}

fn keycode(name: &str) -> Option<CGKeyCode> {
    KEYCODES
        .iter()
        .find(|(k, _)| *k == name)
        .map(|(_, v)| *v)
}

/// Post a single synthesized keyboard event to a PID.
fn post_keyboard(
    source: &CGEventSource,
    keycode: CGKeyCode,
    flags: CGEventFlags,
    key_down: bool,
    pid: i32,
) -> Result<()> {
    // SAFETY-free: `CGEvent::new_keyboard_event` is a safe wrapper over
    // `CGEventCreateKeyboardEvent`, which returns a retained +1 object that
    // the wrapper owns and releases on drop.
    let event = CGEvent::new_keyboard_event(Some(source), keycode, key_down)
        .ok_or_else(|| HarnessError::other("failed to create keyboard event"))?;
    CGEvent::set_flags(Some(&event), flags);
    post(&event, pid, "keyboard")
}

/// Post a keyboard key (with optional modifiers like `cmd+shift+k`) to a PID.
pub fn key(key: &str, pid: i32) -> Result<()> {
    let parts: Vec<&str> = key.split('+').map(str::trim).filter(|p| !p.is_empty()).collect();
    let Some(base) = parts.last() else {
        return Err(HarnessError::input(InputError::UnsupportedKey, "key must not be empty"));
    };
    let base = base.to_ascii_lowercase();
    let Some(kc) = keycode(&base) else {
        return Err(HarnessError::input(
            InputError::UnsupportedKey,
            format!("unsupported key {base:?}; use type() for arbitrary text"),
        ));
    };

    let mut mods: Vec<(CGKeyCode, CGEventFlags)> = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for name in &parts[..parts.len() - 1] {
        let name = name.to_ascii_lowercase();
        let Some(flag) = modifier_flag(&name) else {
            return Err(HarnessError::input(
                InputError::UnsupportedModifier,
                format!("unsupported modifier {name:?}"),
            ));
        };
        let Some(mkc) = MODIFIER_KEYCODES
            .iter()
            .find(|(k, _)| *k == name)
            .map(|(_, v)| *v)
        else {
            continue;
        };
        if seen.insert(mkc) {
            mods.push((mkc, flag));
        }
    }

    let source = EventSource::new()?.source;
    let mut active = CGEventFlags::empty();
    // Press modifiers in order, then the key, then release in reverse.
    for (mkc, flag) in &mods {
        active.insert(*flag);
        post_keyboard(&source, *mkc, active, true, pid)?;
    }
    post_keyboard(&source, kc, active, true, pid)?;
    post_keyboard(&source, kc, active, false, pid)?;
    for (mkc, flag) in mods.iter().rev() {
        active.remove(*flag);
        post_keyboard(&source, *mkc, active, false, pid)?;
    }
    Ok(())
}

/// Mouse button enum, matching the Python `_BUTTONS` table.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Button {
    Left,
    Right,
    Middle,
}

impl Button {
    fn from_name(name: &str) -> Option<Self> {
        match name.to_ascii_lowercase().as_str() {
            "left" => Some(Self::Left),
            "right" => Some(Self::Right),
            "middle" => Some(Self::Middle),
            _ => None,
        }
    }
    fn cg(self) -> CGMouseButton {
        match self {
            Self::Left => CGMouseButton::Left,
            Self::Right => CGMouseButton::Right,
            Self::Middle => CGMouseButton::Center,
        }
    }
    fn down_type(self) -> CGEventType {
        match self {
            Self::Left => CGEventType::LeftMouseDown,
            Self::Right => CGEventType::RightMouseDown,
            Self::Middle => CGEventType::OtherMouseDown,
        }
    }
    fn up_type(self) -> CGEventType {
        match self {
            Self::Left => CGEventType::LeftMouseUp,
            Self::Right => CGEventType::RightMouseUp,
            Self::Middle => CGEventType::OtherMouseUp,
        }
    }
}

/// Post a single synthesized mouse event to a PID.
fn post_mouse(
    source: &CGEventSource,
    mouse_type: CGEventType,
    point: CGPoint,
    button: CGMouseButton,
    click_state: i64,
    pid: i32,
) -> Result<()> {
    // SAFETY-free: `CGEvent::new_mouse_event` wraps `CGEventCreateMouseEvent`
    // (retained +1, owned by the wrapper).
    let event = CGEvent::new_mouse_event(Some(source), mouse_type, point, button)
        .ok_or_else(|| HarnessError::other("failed to create mouse event"))?;
    CGEvent::set_integer_value_field(
        Some(&event),
        CGEventField::MouseEventClickState,
        click_state,
    );
    post(&event, pid, "mouse")
}

/// Send a mouse click (down + up) at a coordinate to a PID.
pub fn click(x: f64, y: f64, button: &str, clicks: u32, pid: i32) -> Result<()> {
    let Some(button) = Button::from_name(button) else {
        return Err(HarnessError::input(
            InputError::UnknownButton,
            format!("unknown mouse button {button:?}"),
        ));
    };
    let point = CGPoint::new(x, y);
    let source = EventSource::new()?.source;
    for click_count in 1..=clicks.max(1) {
        post_mouse(&source, button.down_type(), point, button.cg(), click_count as i64, pid)?;
        post_mouse(&source, button.up_type(), point, button.cg(), click_count as i64, pid)?;
    }
    Ok(())
}

/// Post an already-constructed event to a PID.
fn post(event: &CGEvent, pid: i32, what: &str) -> Result<()> {
    // `CGEvent::post_to_pid` wraps `CGEventPostToPid`; the event remains owned
    // by the caller (`event`). No CF memory is transferred.
    CGEvent::post_to_pid(pid, Some(event));
    let _ = what;
    Ok(())
}
