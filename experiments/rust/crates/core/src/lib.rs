//! Raw macOS primitives for macOS Harness — no Python dependency.
//!
//! This crate is the low-level "core" of the planned Rust migration. It wraps
//! the public, system-provided C APIs (Core Graphics, Application Services,
//! AppKit) behind safe, thin Rust wrappers. A separate `pyo3` crate exposes
//! these to Python as `macos_harness_rs`.
//!
//! Invariants preserved from the Python harness:
//!   * never move the physical pointer (input is posted to a PID only)
//!   * never activate / raise a target app
//!   * no telemetry, no user data leaves the process
//!
//! Every `unsafe` block is small, localized, and commented with the exact
//! CF / CG memory-management rule it relies on.

pub mod cg_event;
pub mod clipboard;
pub mod error;
pub mod screenshot;

pub use error::{AccessibilityError, HarnessError, InputError, Result};
