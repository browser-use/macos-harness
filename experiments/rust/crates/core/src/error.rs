//! Error types for the core crate.
//!
//! These mirror the Python `MacOSError` hierarchy so the PyO3 layer can map
//! them onto Python exceptions 1:1 (and onto the existing
//! `AccessibilityPermissionError` / `FocusChangedError` subclasses).

use std::fmt;

/// Result alias for the crate.
pub type Result<T> = std::result::Result<T, HarnessError>;

/// Base error for macOS discovery or control failures.
#[derive(Debug, Clone)]
pub struct HarnessError {
    kind: ErrorKind,
    message: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ErrorKind {
    /// The process lacks a required macOS permission (Accessibility / Input
    /// Monitoring / Screen Recording).
    Accessibility(AccessibilityError),
    /// An input operation (key / click / scroll) failed.
    Input(InputError),
    /// A screenshot / capture failed.
    Capture,
    /// A target app became frontmost during a background-targeted action.
    FocusChanged,
    /// Some other failure.
    Other,
}

/// Specific accessibility permission failures.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AccessibilityError {
    Accessibility,
    ScreenRecording,
    PostEvents,
}

/// Input-specific failures.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InputError {
    MissingApp,
    UnknownButton,
    UnsupportedKey,
    UnsupportedModifier,
    PostFailed,
}

impl HarnessError {
    pub fn new(kind: ErrorKind, message: impl Into<String>) -> Self {
        Self { kind, message: message.into() }
    }

    pub fn accessibility(err: AccessibilityError, message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Accessibility(err), message)
    }

    pub fn input(err: InputError, message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Input(err), message)
    }

    pub fn capture(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Capture, message)
    }

    pub fn focus_changed(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::FocusChanged, message)
    }

    pub fn other(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Other, message)
    }

    pub fn kind(&self) -> &ErrorKind {
        &self.kind
    }
}

impl fmt::Display for HarnessError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.message)
    }
}

impl std::error::Error for HarnessError {}

impl From<std::io::Error> for HarnessError {
    fn from(err: std::io::Error) -> Self {
        HarnessError::capture(err.to_string())
    }
}
