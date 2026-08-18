//! Window / display screenshot capture.
//!
//! Two capture paths are provided:
//!   1. `capture_window_png` via `CGWindowListCreateImage` (the native path the
//!      Python harness will adopt) — captures a specific window without
//!      bringing it forward or moving the pointer.
//!   2. `capture_display_png` via `CGDisplayCreateImage` for the whole display.
//!
//! As a documented fallback (matching the current Python harness, which shells
//! out to `/usr/sbin/screencapture`), callers may use `capture_screencapture`.
//! Encoding to PNG is done with the `image` crate so we produce a real file
//! without depending on ImageIO.

use std::path::Path;

use image::{ImageBuffer, Rgba};
use objc2_core_foundation::{CGRect, CGSize, CGPoint};
use objc2_core_graphics::{
    CGDataProvider, CGDisplayCreateImage, CGImage, CGWindowImageOption, CGWindowListCreateImage,
    CGWindowListOption,
};

use crate::error::{HarnessError, Result};

/// Decode a `CGImage` into RGBA pixels.
///
/// Returns `(width, height, bytes_per_row, RGBA buffer)`.
fn cgimage_to_rgba(image: &CGImage) -> Result<(u32, u32, usize, Vec<u8>)> {
    let width = CGImage::width(Some(image)) as u32;
    let height = CGImage::height(Some(image)) as u32;
    let bytes_per_row = CGImage::bytes_per_row(Some(image)) as usize;

    let provider = CGImage::data_provider(Some(image))
        .ok_or_else(|| HarnessError::capture("image has no data provider"))?;
    // SAFETY-free: `CGDataProvider::data` wraps `CGDataProviderCopyData`,
    // which returns a retained +1 CFData owned by the returned value.
    let data = CGDataProvider::data(Some(&provider))
        .ok_or_else(|| HarnessError::capture("image data provider returned no data"))?;

    let len = data.length() as usize;
    // SAFETY: `data.byte_ptr()` returns a pointer valid for the lifetime of
    // `data`, which we keep alive until the copy on the next line.
    let ptr = data.byte_ptr();
    if ptr.is_null() {
        return Err(HarnessError::capture("image data pointer was null"));
    }
    let bitmap = unsafe { std::slice::from_raw_parts(ptr, len) }.to_vec();
    Ok((width, height, bytes_per_row, bitmap))
}

/// Copy raw bitmap bytes into a tightly packed RGBA `ImageBuffer`.
fn to_rgba_image(
    width: u32,
    height: u32,
    bytes_per_row: usize,
    bitmap: &[u8],
) -> Result<ImageBuffer<Rgba<u8>, Vec<u8>>> {
    let mut out: ImageBuffer<Rgba<u8>, Vec<u8>> = ImageBuffer::new(width, height);
    let bpp = 4usize;
    for y in 0..height {
        let start = y as usize * bytes_per_row;
        let end = start + width as usize * bpp;
        let row = &bitmap[start..end.min(bitmap.len())];
        for (x, chunk) in row.chunks_exact(bpp).enumerate() {
            out.put_pixel(x as u32, y, Rgba([chunk[0], chunk[1], chunk[2], chunk[3]]));
        }
    }
    Ok(out)
}

fn save(image: &ImageBuffer<Rgba<u8>, Vec<u8>>, path: &Path) -> Result<()> {
    image
        .save(path)
        .map_err(|e| HarnessError::capture(format!("failed to write PNG to {}: {e}", path.display())))
}

/// Capture a specific window (by CGWindowID) and write it to `path` as PNG.
///
/// The window is captured in the background without activating it. Note: on
/// macOS 10.15+ this requires Screen Recording permission, exactly as the
/// Python harness does.
pub fn capture_window_png(window_id: u32, path: &Path) -> Result<(u32, u32)> {
    let screen_bounds = CGRect::new(CGPoint::new(0.0, 0.0), CGSize::new(0.0, 0.0));
    // SAFETY-free: `CGWindowListCreateImage` is a safe wrapper over the C
    // function (returns retained +1 or NULL).
    let image = CGWindowListCreateImage(
        screen_bounds,
        CGWindowListOption::OptionAll,
        window_id,
        CGWindowImageOption::Default,
    )
    .ok_or_else(|| {
        HarnessError::capture(format!("CGWindowListCreateImage returned no image for window {window_id}"))
    })?;

    let (w, h, bpr, bitmap) = cgimage_to_rgba(&image)?;
    let img = to_rgba_image(w, h, bpr, &bitmap)?;
    save(&img, path)?;
    Ok((w, h))
}

/// Capture the whole main display and write it to `path` as PNG.
pub fn capture_display_png(display_id: u32, path: &Path) -> Result<(u32, u32)> {
    // SAFETY-free: `CGDisplayCreateImage` is a safe wrapper (retained +1 or NULL).
    let image = CGDisplayCreateImage(display_id)
        .ok_or_else(|| HarnessError::capture("CGDisplayCreateImage returned no image"))?;
    let (w, h, bpr, bitmap) = cgimage_to_rgba(&image)?;
    let img = to_rgba_image(w, h, bpr, &bitmap)?;
    save(&img, path)?;
    Ok((w, h))
}

/// Fallback capture via the `/usr/sbin/screencapture` executable.
///
/// This is what the current Python harness does. It is kept as a documented
/// fallback for window capture in cases where the in-process CGImage path is
/// undesirable, and it is the exact semantics we must reproduce.
pub fn capture_screencapture(window_id: u32, path: &Path) -> Result<(u32, u32)> {
    let output = std::process::Command::new("/usr/sbin/screencapture")
        .args(["-x", "-o", "-l", &window_id.to_string()])
        .arg(path)
        .output()?;
    if !output.status.success() || !path.exists() {
        let detail = String::from_utf8_lossy(&output.stderr).trim().to_string();
        return Err(HarnessError::capture(format!("screencapture failed: {detail}")));
    }
    let dims = image::ImageReader::open(path)
        .map_err(|e| HarnessError::capture(e.to_string()))?
        .into_dimensions()
        .map_err(|e| HarnessError::capture(e.to_string()))?;
    Ok(dims)
}
