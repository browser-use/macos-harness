# macOS Harness — Rust Migration & Python Optimization Plan

This document covers two ideas for `macos-harness`:

- **(A)** Low-risk Python optimizations to adopt in the Python package (the
  plan — **not implemented** in this repo; precise `file:line` references and
  expected latency wins are given).
- **(B)** A Rust primitives core + PyO3 binding architecture, plus a concrete
  phased migration path and the hard parts.

A minimal compiling Rust POC that proves the "see/key/click via objc2" path is
in `crates/core` (see `BUILD_NOTES.md` and `crates/core/README.md`).

---

## 0. Context & invariants (must be preserved)

From `CONTRIBUTING.md` and `README.md`:

1. **Never move the physical pointer.** All input is posted to a target PID via
   `CGEventPostToPid`; the virtual pointer is a separate AppKit overlay. Rust
   must never call `CGWarpMouseCursorPosition`/`CGEventPost` (global).
2. **Never activate or raise a target app.** Screenshots use
   `CGWindowListCreateImage`; input goes to a PID. `NSWorkspace` is only used
   for *reading* frontmost state to guard against focus changes
   (`_guard_focus`, macos.py:358).
3. **Never send user data through telemetry.** Telemetry (`telemetry.py`)
   records only command category / success / duration / version / OS / agent.
   The Rust core takes no telemetry at all.

Measured baseline: importing `ApplicationServices` costs **~175 ms** and PIL
**~14 ms** on this machine; together they dominate `macos-harness telemetry
status` / `skill` / `apps` startup.

---

# PART A — Low-risk Python optimizations (plan only)

## A1. Lazy-load pyobjc (ApplicationServices/AppKit) and PIL

**Problem.** `src/macos_harness/__init__.py:3-4` eagerly imports `BrowserHarness`
and `MacOS` at package import time. Importing `macos.py` runs
`macos.py:20` (`from PIL import Image, ImageDraw`) and
`macos.py:25-33` (`import ApplicationServices as AS` + `from AppKit import
NSWorkspace`). Because `cli.py:15-16` does `from .macos import MacOS`, **every**
CLI command — including `telemetry status`, `skill`, and `apps`, which never
touch the AX/CG machinery — pays the ~175 ms pyobjc + ~14 ms PIL import cost up
front.

**Exact change.**

1. Make `src/macos_harness/__init__.py` lazy using a PEP 562 module-level
   `__getattr__`. Replace lines 3-4 with:

   ```python
   __all__ = [
       "AccessibilityPermissionError",
       "BrowserHarness",
       "FocusChangedError",
       "MacOS",
       "MacOSError",
   ]

   _EXPORTS: dict[str, object] = {}

   def __getattr__(name: str) -> object:
       if name not in _EXPORTS:
           if name == "BrowserHarness":
               from .browser import BrowserHarness as _v
           elif name in {"MacOS", "MacOSError", "AccessibilityPermissionError", "FocusChangedError"}:
               from .macos import (  # imports ApplicationServices here, only when needed
                   MacOS, MacOSError, AccessibilityPermissionError, FocusChangedError,
               )
               _v = locals()  # placeholder; see below
               ...
           else:
               raise AttributeError(name)
       return _EXPORTS[name]
   ```

   (Implementation detail: each branch assigns the real object into
   `_EXPORTS[name]`. The snippet above is schematic; the mechanics are
   standard PEP 562.)

2. Move the heavy imports out of module scope and into `macos.py` itself, gated
   behind a lazy accessor. The wrinkle: `macos.py:100-141` defines module-level
   constants `_BUTTONS`, `_MOUSE_EVENTS`, `_MODIFIER_FLAGS`, and
   `_MODIFIER_KEYCODES` that reference `AS.kCG*` at import time
   (`macos.py:101`, `107-120`, `123-131`). These **must be computed after** the
   lazy import. Options:
   - Compute them as module-level constants **after** a lazy `import
     ApplicationServices` that happens on first access (e.g. a module-level
     function `_ensure_as()` called by `_require_macos()` at macos.py:233 and by
     the constant-getter), or
   - Replace the module constants with functions/properties that read
     `AS.kCG*` on demand.
   - Simplest safe approach: keep a module-global `_AS_LOADED` flag and a
     `_as()` helper that does `import ApplicationServices` + `from AppKit
     import NSWorkspace` once; change the four constant dicts into
     `functools.lru_cache`d functions (e.g. `_buttons()`, `_mouse_events()`,
     `_modifier_flags()`, `_modifier_keycodes()`) and update their ~6 call
     sites (`macos.py:1219,1229,1232-1235,1268-1271,1320-1323,1370-1372`).

3. Make `cli.py` import `MacOS`/`BrowserHarness` lazily *inside* the command
   functions instead of at module top. `cli.py:15-16` become deferred imports
   (`from .macos import MacOS, MacOSError` inside `main()`/each branch, or a
   small `_macos()`/`_harness()` helper). This keeps `main()` parsing + the
   `telemetry`/`skill` branches completely free of pyobjc/PIL.

4. PIL: `macos.py:20` import moves inside `MacOS.see()` (macos.py:1033), the
   only consumer (macos.py:1051-1105). `controls.py:8` also imports
   `ApplicationServices` at module scope; since `controls.py` is imported from
   `macos.py:284` inside `__init__`, it is already deferred by A1.

**Risk.** Low. PEP 562 `__getattr__` only fires for attributes not found by
normal lookup, so `import macos_harness` + attribute access still works. The
main risks are (a) tools that do `from macos_harness import *` or inspect
`__all__` / module `dir()` (fine — `__all__` is preserved), and (b) any code
relying on `macos_harness.MacOS` being an attribute at import time, which
`__getattr__` satisfies. Tests that import `macos_harness` without macOS
dependencies must still succeed (they will — the exception is only raised on
use via `_require_macos`).

**Expected latency win.** `telemetry status`, `skill`, and `apps` drop from
~190 ms to a few ms (no pyobjc, no PIL). `doctor` still pays it once when
`MacOS()` is constructed. Net CLI startup for the three lightweight commands:
**~185 ms saved** (~97%).

## A2. Don't spawn the pointer overlay subprocess for invisible ops

**Problem.** `MacOS.__init__` (macos.py:274-288) constructs a
`LivePointerOverlay` (macos.py:287). The subprocess itself is already lazy
(overlay.py:86-92 starts it on first `_send`), but it starts on the **first
pointer-op**: `click` (macos.py:1228), `drag` (macos.py:1276-1277), `scroll`
with a point (macos.py:1334), `move`/`show_pointer`. Each spawn launches a
fresh Python + AppKit interpreter (`sys.executable -m
macos_harness.overlay --helper`, overlay.py:75-84), which costs ~100-200 ms and
creates a real (click-through) window even when the caller only wants input
posted and `show_pointer` is not requested.

**Exact change.** Make overlay activation explicit/opt-in:
- Add a constructor flag (e.g. `MacOS(overlay: bool | None = None)` defaulting
  to `True`) and a runtime `mac.enable_overlay(True/False)`.
- Gate every `self._overlay.*` call (macos.py:1190,1198,1204,1228,1246,1276-1277,1334)
  behind `self._overlay` being non-None; when disabled, skip overlay moves
  (pointer coordinates are still tracked in `self._pointer_position` and
  reported via `_pointer_info`).
- Have `see()` (macos.py:1076) draw the pointer only if the overlay is
  actually running (`self._overlay.running`), not just `visible`.

**Risk.** Low. The pointer overlay is purely cosmetic; disabling it does not
change input semantics. The only behavioral change is that `see()` no longer
draws the pointer glyph when the overlay process isn't running — arguably more
correct. Keep `show_pointer()` (macos.py:1195) as the explicit way to force it.

**Expected latency win.** For scripts that only `key`/`type`/`click` and never
request the overlay: one ~150 ms subprocess spawn and a hidden window are
avoided. For `click` in particular (macos.py:1228) the first-click cost drops
~150 ms.

## A3. Configurable inter-key/scroll delays

**Problem.** Hardcoded `time.sleep(...)` calls throttle every input:
`click` 0.03 s × 2 (macos.py:1242,1244), `scroll` 0.01 s per step
(macos.py:1355), `type` 0.01 s per char (macos.py:1385), `key` 0.005 s per
modifier (macos.py:1428) + 0.01 s tail (macos.py:1444), `drag` interpolated
(macos.py:1298). These are conservative to avoid dropped events but slow long
`type()` strings and large scrolls.

**Exact change.** Introduce module-level tunables (e.g. `_TYPE_DELAY`,
`_KEY_DELAY`, `_SCROLL_DELAY`, `_CLICK_DELAY`, `_MODIFIER_DELAY`) with the
current values as defaults, overridable via env vars
(`MACOS_HARNESS_TYPE_DELAY` etc.) or a `MacOS(..., delays=...)` dict. Replace
the literal sleeps at the lines above with the tunable. Keep defaults
unchanged so current behavior is identical unless configured.

**Risk.** Low-to-medium. Shorter delays can cause dropped/reordered events on
slow or busy targets; that is precisely why a user-configurable knob (not a
forced change) is the right scope. Keep the existing per-click click-state
counting (macos.py:1237-1240) intact.

**Expected latency win.** Configurable, so it depends on the caller, but a
script typing 100 chars at 0.01 s saves 1 s at 0 s delay; 10k-pixel scrolls
save proportionally. Even at defaults this documents and centralizes the
timing model.

## A4. Cap AX snapshot work

**Problem.** `_snapshot_tree` (macos.py:536) defaults to `include_settable=True`
and `get_app_state` defaults to `max_nodes=5000` (macos.py:648). With
`include_settable=True`, every node pays an extra `AXUIElementIsAttributeSettable`
round trip for three candidates (macos.py:592-599), which dominates large-tree
snapshot cost. The default is expensive for ordinary `state` calls that never
read `settable`.

**Exact change.**
- Flip the default to `include_settable=False` in `_snapshot_tree`
  (macos.py:545) and `get_app_state` (macos.py:653); keep `include_settable`
  as an explicit opt-in.
- Lower the default `max_nodes` from 5000 to a saner cap (e.g. 1000–1500) at
  macos.py:648 and `cli.py:80` (`state --max-nodes` default), matching what
  most agents actually consume.
- Batch the settable check if kept on: group the three
  `AXUIElementIsAttributeSettable` calls behind a single
  `AXUIElementCopyMultipleAttributeValues`-style round trip where supported,
  or compute them from the already-fetched `AXValue`/`AXFocused`/`AXSelected`
  presence instead of a separate per-node call.

**Risk.** Low. `settable` is informational; the harness `set()`/`perform_action`
(macos.py:907,915) do not depend on the snapshot's `settable` field. Lowering
`max_nodes` only truncates very large trees, which already surface the
"tree truncated by max_nodes" note (macos.py:639). Callers that explicitly
pass `include_settable=True` or a higher `max_nodes` are unaffected.

**Expected latency win.** Removes one AX round trip per node (the single most
expensive per-node cost), typically **2–5× faster** snapshots on large
Chromium/WebKit trees. Cap reduction bounds worst-case work linearly.

---

# PART B — Rust primitives core + PyO3 binding architecture

## B1. Workspace layout (recommended)

```
macos-harness-rust/
  Cargo.toml                      # [workspace] members = ["crates/core", "crates/pyo3"]
  crates/core/                    # NO Python. Pure macOS primitives, testable/FFI-free.
    Cargo.toml                    # objc2, objc2-core-foundation, objc2-core-graphics,
                                  # objc2-application-services, objc2-app-kit, image
    src/
      lib.rs
      error.rs                    # HarnessError { kind: ErrorKind }
      cg_event.rs                 # key / click / drag / scroll via CGEvent -> PID
      screenshot.rs               # CGWindowListCreateImage / CGDisplayCreateImage -> PNG
      ax.rs                       # AXUIElement bindings (the hard part)
      clipboard.rs                # NSPasteboard
      overlay.rs                  # AppKit overlay (NSWindow/NSView) via objc2-app-kit
      keycode.rs                  # virtual keycodes + UCKeyTranslate/TIS Unicode mapping
      coordinates.rs              # Retina/coordinate-space conversion
      unicode.rs                  # CGEventKeyboardSetUnicodeString + UCKeyTranslate
  crates/pyo3/
    Cargo.toml                    # pyo3 0.23 + macos-harness-core
    src/lib.rs                    # #[pymodule] macos_harness_rs
    src/errors.rs                 # HarnessError -> Python exception mapping
  PLAN.md / BUILD_NOTES.md
```

The split is the key architectural decision:

- **`core` is Python-free** so it can be unit-tested with `cargo test`, built
  into a standalone CLI, reused outside Python, and kept free of pyobjc's
  interpreter cost. All `unsafe` is confined to this crate.
- **`pyo3` is a thin adapter** that only converts arguments/return values and
  maps `HarnessError` onto the existing Python exception hierarchy. It has no
  FFI of its own.

## B2. Method → Rust mapping

| Python `MacOS` method (macos.py) | Rust module:function (core) | Notes |
|---|---|---|
| `__init__` (274) event source + overlay | `cg_event::EventSource::new`, `overlay::Overlay` | private `CGEventSource` (kCGEventSourceStatePrivate) |
| `is_accessibility_trusted` (292) | `ax::is_trusted` | `AXIsProcessTrusted` |
| `request_accessibility_permission` (295) | `ax::request_trust` | `AXIsProcessTrustedWithOptions` |
| `permissions` (305) / `doctor` (325) | `ax::permissions` | CGPreflight* |
| `list_apps` (332) / `_resolve_app` (375) | `apps::list`, `apps::resolve` | `NSWorkspace` (read-only) |
| `_guard_focus` (358) | `focus::guard` | read frontmost, never activate |
| `_application_element` (428) | `ax::application(pid)` | + `AXEnhancedUserInterface` |
| `_copy_attribute(s)` (440/444) | `ax::copy_attribute(s)` | batch API |
| `_actions` (473) / `_settable` (478) | `ax::actions`, `ax::settable` | |
| `_jsonable` (490) | `ax::Value` -> `serde_json::Value` | AXValue decode |
| `_snapshot_tree` (536) / `get_app_state` (642) | `ax::snapshot` | returns index map |
| `get`/`get_attributes` (743/750) | `ax::get` | |
| `set` (907) / `perform_action` (915) | `ax::set`, `ax::perform_action` | |
| `windows` (929) | `screenshot::windows` | `CGWindowListCopyWindowInfo` |
| `capture_screenshot` (968) / `see` (1033) | `screenshot::capture_window_png` + `image` crate | |
| `_post` (1137) | `cg_event::post` | `CGEventPostToPid` |
| `click` (1206) | `cg_event::click` | down+up with click-state |
| `drag` (1251) | `cg_event::drag` | interpolated drag events |
| `scroll` (1307) | `cg_event::scroll` | `CGEventCreateScrollWheelEvent2` |
| `type` (1358) | `unicode::type_text` | CGEventKeyboardSetUnicodeString |
| `key` (1388) | `cg_event::key` | modifiers + keycode |
| `script` (1449) | `script::run` | osascript (kept subprocess) |
| `move`/`show_pointer`/`hide_pointer` (1180/1195/1203) | `overlay` | no physical move |
| `LivePointerOverlay` (overlay.py) | `overlay` | AppKit NSPanel/NSView via objc2-app-kit |
| `BrowserHarness` (browser.py) | *(unchanged)* | stays Python (CDP) |

The **AX element index map** (`self._elements`, macos.py:276, 569-570, 701-703)
is a natural fit for a Rust side-table: `ax::snapshot` fills a
`Vec<AXUIElement>` and returns node JSON with `element_index`; `get`/`set`/
`perform_action` index into that Vec. To keep the Python API stable, the PyO3
layer can hold the index map on the Rust side keyed by snapshot, or return
opaque handles — whichever is simpler; the existing int-index contract is
preserved either way.

## B3. Startup win

The single biggest win of the migration: **no pyobjc interpreter in the
process.** Today every `MacOS()` import pays ~175 ms loading pyobjc bindings
inside CPython. The PyO3 module links the objc2 bindings once as a native
library — loading is on the order of milliseconds, and the AX/CG calls are
direct C function calls rather than `objc_msgSend` marshaled through pyobjc.
PIL PNG work moves to the `image` crate (native). Combined with Part A's lazy
Python imports, even `python`/`see`/`state` start faster.

## B4. Unicode keycode handling

The Python `type()` (macos.py:1358-1386) sidesteps keycode lookup by always
attaching the Unicode string via `CGEventKeyboardSetUnicodeString`
(macos.py:1379-1380) and deriving keycodes only from a fixed ASCII table
(`_KEYCODES`, macos.py:160). This is fragile for non-ASCII / shifted layouts.
Rust should use **`UCKeyTranslate`/TIS** (Text Input Sources):
`TISCopyCurrentKeyboardLayoutInputSource` → `TISGetInputSourceProperty(kTISPropertyUnicodeKeyLayoutData)` → `UCKeyTranslate` to convert a Unicode
character into the correct virtual keycode + modifier mask for the active
keyboard layout. Fall back to `CGEventKeyboardSetUnicodeString` (which the POC
already proves is usable) when layout translation fails. Keep the fixed
keycode table for named keys (`space`, `return`, arrows) exactly as
macos.py:160 does.

## B5. Coordinate / Retina handling

macOS uses points in the CG/global coordinate space (origin bottom-left on the
primary display), but screenshots are in **pixels** (2× on Retina). The Python
code already tracks this via `scale_x`/`scale_y` = pixel_width / bounds_width
(macos.py:1025-1026, 1062-1063) and `_screen_point` (macos.py:1143). The Rust
`coordinates` module must reproduce exactly:
- Window bounds come from `CGWindowBounds` in points (macos.py:950-954).
- Screenshot pixel size comes from the actual PNG dims (macos.py:1015).
- Conversions: `screenshot` → divide by `scale_x/scale_y`; `window` → add
  bounds origin; `screen` → as-is (macos.py:1155-1162).
- A single `Coordinates` struct carries `{bounds, scale_x, scale_y}` so PyO3
  can pass the same numbers the Python side uses today — zero behavioral drift.

## B6. Error typing

`HarnessError` (error.rs) carries a typed `ErrorKind`
(`Accessibility`, `Input`, `Capture`, `FocusChanged`, `Other`) so the PyO3
layer maps 1:1 onto the existing Python hierarchy: `Accessibility` →
`AccessibilityPermissionError(MacOSError)`, `FocusChanged` →
`FocusChangedError(MacOSError)`, everything else → `MacOSError`
(macos.py:36-45). AX error codes are surfaced with the operation name, matching
`_ax_error` (macos.py:254).

## B7. Telemetry / privacy invariants

The Rust core takes **no** telemetry and collects **no** user data — matching
`CONTRIBUTING.md` invariant #3. The existing Python `telemetry.py` continues to
own opt-out and the privacy-safe payload; it records only command category /
success / duration / version / OS / agent, never prompts/app names/screenshots/
UI text/paths/titles (telemetry.py:157-181). The migration must not move any of
that collection into native code. `doctor` output (macos.py:325) stays the same
shape.

## B8. Phased migration path (keep Python CLI working)

1. **POC (this deliverable):** native `core` crate with `screenshot`, `key`,
   `click`, `clipboard`; standalone CLI. Proves objc2 feasibility. ✓
2. **PyO3 shell:** add `macos-harness-pyo3` exposing a `macos_harness_rs`
   module with the primitives; build via `maturin` into the Python env. Ship
   as an optional extra so the pure-Python path still works if native build is
   unavailable.
3. **Swap `capture_screenshot`/`see` first** (lowest risk, biggest win, no AX
   involvement): `MacOS.capture_screenshot` calls
   `macos_harness_rs.capture_window(...)`; fall back to `screencapture` on
   failure. Keep `_png_size`/bounds logic.
4. **Swap `click`/`drag`/`scroll`/`key`/`type`:** replace `_post` + CGEvent
   construction with native calls, keeping `_guard_focus` (frontmost read) and
   the overlay in Python initially. Add `unicode.rs` (B4) before `type`.
5. **Swap AX (`get_app_state`, `get`, `set`, `perform_action`, `ax_search`):**
   the highest-risk step (B9). Keep the Python AX code behind a flag until
   parity tests pass.
6. **Swap `windows`/`list_apps`/`doctor` and finally the overlay** (objc2-app-kit).
7. **Remove lazy pyobjc fallback** once no code path imports it.

Each step lands independently, keeps `macos-harness` CLI green, and can be
reverted by flipping the import back.

## B9. Hard parts / open questions

1. **AXUIElement unsafe FFI + CF memory management (highest risk).**
   `AXUIElementCopyAttributeValue` returns a `+1` CFTypeRef whose type is
   dynamic (AXValue, AXUIElement, CFString, CFNumber, CGPoint/CGSize/CGRect via
   AXValue, or an AXError sentinel). Correctly retaining/releasing every value
   and recursively identifying AXUIElement children (`CFGetTypeID` ==
   `AXUIElementGetTypeID`, macos.py:484-488) without leaks or UAF is the
   trickiest part. Need objc2-level wrappers for AXUIElement (may require
   `objc2-core-foundation` CFType custom impls or hand-rolled
   `CFType`-subclass). The POC intentionally does **not** implement AX yet.
2. **CGEvent coordinate spaces.** CGEvent locations are in the global display
   coordinate space in points with bottom-left origin; mixing with pixel-space
   screenshot coordinates and Retina scales is an easy source of off-target
   clicks. Must share the exact `Coordinates` math with Python (B5).
3. **CI must stay on `macos-latest`.** The entire crate is macOS-only and
   needs macOS frameworks + (for the pyobjc part) macOS runners. GitHub Actions
   `macos-latest` is required; the pyo3 crate additionally needs a Python
   interpreter at build time (`PYO3_PYTHON`). Screen Recording /
   Accessibility permission tests need TCC handling on CI.
4. **`CGDisplayCreateImage`/`CGWindowListCreateImage` are deprecated** (Apple
   pushes ScreenCaptureKit). The POC compiles with deprecation warnings and
   works, but a production migration should evaluate ScreenCaptureKit (needs
   macOS 12.3+) — note current Python `see` still uses `screencapture`, so
   parity is the immediate target, not the newest API.
5. **Distributing a native wheel** (maturin + `macos_harness_rs`) adds build
   complexity and a hard macOS toolchain dependency; the Python package must
   keep a graceful pure-Python fallback so `pip install` on non-mac or without
   a Rust toolchain still works.

---

## POC scope & build result

- `crates/core` compiles cleanly (`cargo check`/`build` pass; only Apple
  deprecation warnings on the two CG capture functions).
- Verified live on this machine: `key`, `click`, `clipboard`; `screenshot`
  returns a "no image" error here only because the terminal lacks Screen
  Recording permission (exactly as the Python harness would).
- `crates/pyo3` type-checks (`cargo check` passes); the `cdylib` link step
  fails on undefined `_Py*` symbols — the expected pyo3 `extension-module`
  behavior, resolved by building via `maturin` rather than raw `cargo build`.

See `BUILD_NOTES.md` and `crates/core/README.md`.
