//! PyO3 bindings: `macos_harness_rs`.
//!
//! This crate exposes the safe core primitives to Python behind the same
//! names/shapes as the current Python `MacOS` methods, so the Python layer can
//! swap its internals one method at a time. Error mapping is defined in
//! `errors.rs`.

mod errors;

use macos_harness_core::{cg_event, clipboard, screenshot};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

/// Capture the main display to a PNG and return its dimensions.
#[pyfunction]
fn capture_display(path: String) -> PyResult<(u32, u32)> {
    screenshot::capture_display_png(0, std::path::Path::new(&path))
        .map_err(|e| PyRuntimeError::new_err(e.to_string()))
}

/// Capture a window by CGWindowID to a PNG and return its dimensions.
#[pyfunction]
fn capture_window(window_id: u32, path: String) -> PyResult<(u32, u32)> {
    screenshot::capture_window_png(window_id, std::path::Path::new(&path))
        .map_err(|e| PyRuntimeError::new_err(e.to_string()))
}

/// Post a keyboard key (with optional modifiers) to a PID.
#[pyfunction]
fn key(key: String, pid: i32) -> PyResult<()> {
    cg_event::key(&key, pid).map_err(|e| PyRuntimeError::new_err(e.to_string()))
}

/// Post a mouse click at a coordinate to a PID.
#[pyfunction]
#[pyo3(signature = (x, y, pid, button="left".to_string(), clicks=1))]
fn click(x: f64, y: f64, pid: i32, button: String, clicks: u32) -> PyResult<()> {
    cg_event::click(x, y, &button, clicks, pid).map_err(|e| PyRuntimeError::new_err(e.to_string()))
}

/// Read the current clipboard string.
#[pyfunction]
fn clipboard_read() -> PyResult<Option<String>> {
    clipboard::read_string().map_err(|e| PyRuntimeError::new_err(e.to_string()))
}

#[pymodule]
fn macos_harness_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(capture_display, m)?)?;
    m.add_function(wrap_pyfunction!(capture_window, m)?)?;
    m.add_function(wrap_pyfunction!(key, m)?)?;
    m.add_function(wrap_pyfunction!(click, m)?)?;
    m.add_function(wrap_pyfunction!(clipboard_read, m)?)?;
    Ok(())
}
