# BUILD_NOTES — macos-harness-rust POC

## Environment

- Host: macOS (aarch64/arm64), `darwin`
- `cargo 1.97.1`, `rustc 1.97.1` — installed and used.
- objc2 ecosystem (from `cargo search`):
  - `objc2 = 0.6.4`
  - `objc2-core-foundation = 0.3.2`
  - `objc2-core-graphics = 0.3.2`
  - `objc2-app-kit = 0.3.2`
  - `objc2-application-services = 0.3.2`
  - `objc2-foundation = 0.3.2`
- `image = 0.25` for PNG encoding.

## Build result

### `macos-harness-core` (the POC deliverable)

```
cargo check -p macos-harness-core   # PASS, no errors
cargo build -p macos-harness-core   # PASS
```

**Warnings (only):** `CGDisplayCreateImage` and `CGWindowListCreateImage` are
deprecated by Apple in favor of ScreenCaptureKit. These are the exact APIs the
Python harness's intent maps to; acceptable for the POC and documented as a
risk in PLAN.md B9#4. No other warnings.

**Live run on this machine:**

| Command | Result |
|---|---|
| `cargo run -- key a` | `posted key "a" to pid ...` — OK |
| `cargo run -- click 100 200 left` | `posted click (100,200) left to pid ...` — OK |
| `cargo run -- clipboard` | returned the clipboard string — OK (NSPasteboard path proven) |
| `cargo run -- screenshot out.png` | `CGDisplayCreateImage returned no image` — **expected**: this terminal lacks Screen Recording permission (same as the Python harness would). Not a code error. |

### `macos-harness-pyo3`

```
PYO3_PYTHON=$(which python3) cargo check -p macos-harness-pyo3   # PASS, no errors
PYO3_PYTHON=$(which python3) cargo build -p macos-harness-pyo3   # FAIL at link
```

The source type-checks and compiles. The `cargo build` fails at the **link**
stage with undefined `_Py*` symbols (`_PyBool_Type`, `_PyBytes_AsString`,
…). This is the expected, documented pyo3 behavior for a `cdylib` built with
the `extension-module` feature: the `.so` is meant to be loaded **into** an
embedded Python interpreter at runtime and does not link `libpython` when built
standalone. It is not a source bug.

To produce a loadable module, build with `maturin` (or set
`pyo3-build-config` to link libpython). That was intentionally not set up here
to keep the POC self-contained; the exact remaining step is:

```
cd crates/pyo3 && maturin build --release   # produces macos_harness_rs.cpython-*.so
```

## Remaining errors (exact)

1. `macos-harness-pyo3` standalone `cargo build` link error —
   `Undefined symbols for architecture arm64: "_PyBool_Type", ...` (pyo3
   `extension-module` cdylib; resolved by maturin). No source change needed.
2. Runtime screenshot permission error on this host —
   `CGDisplayCreateImage returned no image` (Screen Recording TCC not granted
   to the terminal). Expected; documented, not a defect.

## Notes / decisions

- `CGEvent`, `CGEventSource`, `CGImage`, `CGWindowListCreateImage`,
  `CGDisplayCreateImage` are exposed by objc2 as **safe** wrappers returning
  `Option<CFRetained<...>>` (owned, auto-released) — this keeps almost all of
  the POC `unsafe`-free. The only `unsafe` blocks remaining are: the raw-slice
  read of `CFData` bytes (screenshot.rs), and reading the `NSPasteboardTypeString`
  extern static (clipboard.rs). Each is commented with its safety argument.
- The core crate uses `CGEventPostToPid` only — never `CGEventPost`/warp — so
  the "never move the physical pointer" invariant is preserved.
- API name notes for future work (objc2 0.6 / 0.3):
  - `CGEvent::new_keyboard_event`, `CGEvent::new_mouse_event` (not `CGEvent::new`).
  - Event accessors are associated functions taking `Option<&Self>`, e.g.
    `CGEvent::set_flags(Some(&ev), flags)`, `CGEvent::post_to_pid(pid, Some(&ev))`.
  - `CGEventFlags::MaskCommand/MaskControl/MaskAlternate/MaskShift`.
  - `CGImage::width/height/bytes_per_row/data_provider(Some(&img))`;
    `CGDataProvider::data(Some(&p))` → `CFRetained<CFData>`;
    `CFData::length()/byte_ptr()`.
  - `NSPasteboard::generalPasteboard()`, `pasteboard.stringForType(&NSPasteboardTypeString)`.
  - `CFRetained` lives in `objc2_core_foundation` (re-export), not `objc2::rc`.
