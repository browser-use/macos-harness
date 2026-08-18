# macos-harness-rust

Rust primitives core + PyO3 binding for [macos-harness](https://github.com/browser-use/macos-harness).

See [`PLAN.md`](./PLAN.md) for the full migration plan and [`BUILD_NOTES.md`](./BUILD_NOTES.md)
for the build result of the POC.

## Workspace

- `crates/core` — Python-free macOS primitives (`see`/`key`/`click`/`clipboard`)
  over `objc2`. This is the deliverable POC.
- `crates/pyo3` — thin PyO3 adapter exposing `macos_harness_rs`. Type-checks;
  loadable module requires `maturin build` (see BUILD_NOTES).

## POC usage

```sh
cargo run -p macos-harness-core -- screenshot out.png
cargo run -p macos-harness-core -- key cmd+k          # posts to $MACOS_HARNESS_TARGET_PID
cargo run -p macos-harness-core -- click 100 200 left
cargo run -p macos-harness-core -- clipboard
```

Input posts to the PID in `MACOS_HARNESS_TARGET_PID` (defaults to the current
process, which is harmless). Screenshot requires Screen Recording permission.

## Invariants preserved

- Input is `CGEventPostToPid` only — the physical pointer is never moved.
- Never activates / raises a target app.
- No telemetry or user data in the native core.

## Notes

`CGDisplayCreateImage` / `CGWindowListCreateImage` are deprecated by Apple
(ScreenCaptureKit is the successor); they are used here because they match the
Python harness semantics. See PLAN.md B9#4.
