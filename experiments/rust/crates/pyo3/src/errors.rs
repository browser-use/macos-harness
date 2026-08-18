//! Mapping from `HarnessError` to Python exceptions.
//!
//! Placeholder for the future error-mapping module. The `ErrorKind` variants
//! map onto the existing Python hierarchy:
//!   * `Accessibility`  -> `AccessibilityPermissionError(MacOSError)`
//!   * `FocusChanged`   -> `FocusChangedError(MacOSError)`
//!   * everything else  -> `MacOSError`
//!
//! The current POC uses `PyRuntimeError` directly in `lib.rs`; this module is
//! where a dedicated `PyErr` conversion will live once the Python package
//! starts consuming the module.
