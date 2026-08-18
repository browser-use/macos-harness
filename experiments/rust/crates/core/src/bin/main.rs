//! CLI that exercises the core primitives:
//!
//!   cargo run -- screenshot <out.png>        capture the main display to PNG
//!   cargo run -- screenshot-window <id> <out.png>  capture a window to PNG
//!   cargo run -- key <keycode-or-name>        post a key to $MACOS_HARNESS_TARGET_PID
//!   cargo run -- click <x> <y> [left|right|middle]  post a click to the PID
//!   cargo run -- clipboard                    read the system clipboard
//!
//! Input commands post to the PID in `MACOS_HARNESS_TARGET_PID` (defaults to
//! this process, which is harmless — it just receives its own events).

use std::path::PathBuf;

use macos_harness_core::cg_event;
use macos_harness_core::clipboard;
use macos_harness_core::screenshot;
use macos_harness_core::Result;

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 2 {
        eprintln!("usage: harness <command> ...");
        return Ok(());
    }
    match args[1].as_str() {
        "screenshot" => {
            let out = args.get(2).map(PathBuf::from).unwrap_or_else(|| PathBuf::from("out.png"));
            let (w, h) = screenshot::capture_display_png(0, &out)?;
            println!("saved {w}x{h} to {}", out.display());
        }
        "screenshot-window" => {
            let id: u32 = args.get(2).and_then(|s| s.parse().ok()).expect("window id");
            let out = args.get(3).map(PathBuf::from).unwrap_or_else(|| PathBuf::from("window.png"));
            let (w, h) = screenshot::capture_window_png(id, &out)?;
            println!("saved {w}x{h} to {}", out.display());
        }
        "key" => {
            let key = args.get(2).expect("key (e.g. 'a' or 'cmd+k')");
            let pid = target_pid();
            cg_event::key(key, pid)?;
            println!("posted key {key:?} to pid {pid}");
        }
        "click" => {
            let x: f64 = args.get(2).expect("x").parse().expect("x");
            let y: f64 = args.get(3).expect("y").parse().expect("y");
            let button = args.get(4).map(String::as_str).unwrap_or("left");
            let pid = target_pid();
            cg_event::click(x, y, button, 1, pid)?;
            println!("posted click ({x},{y}) {button} to pid {pid}");
        }
        "clipboard" => match clipboard::read_string()? {
            Some(s) => println!("{s}"),
            None => println!("(empty)"),
        },
        other => {
            eprintln!("unknown command {other:?}");
            std::process::exit(2);
        }
    }
    Ok(())
}

fn target_pid() -> i32 {
    std::env::var("MACOS_HARNESS_TARGET_PID")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(std::process::id() as i32)
}
