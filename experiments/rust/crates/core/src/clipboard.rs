//! Read the system clipboard via NSPasteboard.
//!
//! This is a small, optional primitive. The Python harness does not yet
//! expose clipboard access; it is included here to prove the objc2-app-kit
//! bindings work and to have a drop-in for future `clipboard` support.

use objc2_app_kit::{NSPasteboard, NSPasteboardTypeString};

use crate::error::Result;

/// Read the current string from the general pasteboard, if any.
pub fn read_string() -> Result<Option<String>> {
    // NSPasteboard is main-thread only. There is no Rust-side way to assert
    // this for `generalPasteboard()` (it takes no thread token), so we rely on
    // the caller running on the main thread. This matches the Python helper,
    // which runs inside an AppKit `NSApplication.run()` main thread.
    let pasteboard = NSPasteboard::generalPasteboard();
    // SAFETY: `NSPasteboardTypeString` is an immutable, process-lifetime
    // NSString constant exported by AppKit. Reading the pointer and converting
    // it to a Rust String does not mutate or alias any CF memory.
    let string_type: &objc2_app_kit::NSPasteboardType = unsafe { &NSPasteboardTypeString };
    Ok(pasteboard
        .stringForType(string_type)
        .map(|s| s.to_string()))
}
