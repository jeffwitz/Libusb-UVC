# Examples Overview

The `examples/` directory provides ready-to-run scripts illustrating how to use
the high-level `libusb_uvc` API.  Each helper focuses on a specific feature of
the library.

## Camera Inspection and Introspection

- `uvc_inspect.py` — Enumerate Video Streaming (VS) interfaces, formats, frames,
  alternate settings, and Video Control (VC) units. Supports a `--test-still`
  smoke test for still-image capture.
- `uvc_generate_quirk.py` — Scan Extension Units (XUs) and export a JSON quirk
  skeleton ready to drop into `src/libusb_uvc/quirks/`.
- `uvc_ir_inspect.py` — Target infrared sensors on multi-interface devices,
  listing relevant controls and capturing raw IR frames.
- `uvc_ir_torch_demo.py` — Demonstrate experimentation with IR torch vendor
  controls while displaying a preview.

## Streaming and Preview

- `uvc_capture_video.py` — OpenCV windowed preview with MJPEG/YUYV support (uses
  decoder backends when available).  Offers format listing (`--list`) and sensor
  selection via `--interface`.
- `uvc_capture_frame.py` — Grab a single frame and save it to disk. Supports
  direct MJPEG saving or conversion to PNG when OpenCV is installed.
- `uvc_display_frame.py` — Matplotlib-based frame rendering with automatic
  fallback to saving images when no display is available.
- `uvc_led_preview.py` — Toggle LED controls while keeping a preview running.
- `exposure_sweep.py` — Disable auto-exposure and sweep the absolute exposure
  value across its supported range.

## Still-Image Capture

- `uvc_capture_still.py` — Negotiate still-image settings (Method 1 and Method
  2) and trigger a single capture, automatically selecting the highest
  resolution when `--width/--height` are omitted.
- `uvc_capture_still_live.py` — Start a streaming preview, then request a still
  capture using the negotiated still configuration.

All scripts accept `--vid/--pid` filters and optional logging flags to help
diagnose issues.  Invoke any helper with `--help` to view supported options.

