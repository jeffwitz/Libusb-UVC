Example Scripts
===============

The ``examples/`` directory ships ready-to-run utilities that exercise the
library's major features. Install libusb-uvc in editable mode to ensure Python
can locate the package and run the scripts with ``python3``.

``uvc_capture_video.py``
------------------------

Launch an OpenCV preview window while streaming MJPEG or YUYV video. Key
options:

* ``--width`` / ``--height`` — target frame size.
* ``--fps`` — desired frame rate; combine with ``--strict-fps`` to enforce exact matches.
* ``--codec`` — choose ``auto``, ``mjpeg``, or ``yuyv``.
* ``--duration`` — automatically stop after ``N`` seconds.

``uvc_capture_frame.py``
------------------------

Capture a single frame and save to disk. When working with MJPEG streams the
script can store the payload directly as ``.jpg`` or convert it to PNG via
OpenCV. Use ``--output`` to select the destination path.

``uvc_display_frame.py``
------------------------

Grab one frame and render it with Matplotlib. This helper is useful in
headless environments because it automatically falls back to saving an image
when ``$DISPLAY`` is not set.

``uvc_led_preview.py``
----------------------

Preview a MJPEG stream and automatically disable a camera LED after a delay.
The script enumerates controls, looks for ``LED Control`` (or similar), and
sends ``SET_CUR`` requests while keeping the stream running.

``uvc_inspect.py``
------------------

Introspect the camera's streaming descriptors and Video Control units. The
output lists formats, frames, alternate settings, and control metadata,
combining quirk definitions with live ``GET_*`` queries.

``uvc_ir_inspect.py``
---------------------

Target the infrared sensor of dual-camera hardware. The script prints every
validated control (using Microsoft XU hints when available) and captures a
handful of IR frames, saving raw payloads alongside optional PNG conversions.
Use ``--interface`` to select the IR streaming interface number.

``uvc_ir_torch_demo.py``
------------------------

Open the IR preview while sweeping the vendor-specific ``LED Control``.  Many
devices expose a writable selector but expect undocumented values; this helper
demonstrates how to experiment with those controls while reiterating that real
hardware behaviour may require per-device reverse engineering.

``exposure_sweep.py``
---------------------

Disable auto exposure, then linearly sweep ``Exposure Time, Absolute`` across
its supported range while overlaying the current value on the preview. The
example demonstrates how to update controls without resetting the stream.

``uvc_generate_quirk.py``
-------------------------

Inspect Extension Unit (XU) selectors and write a JSON skeleton that can be
added to ``src/libusb_uvc/quirks``. Use ``--single`` together with ``--output``
to save a ready-to-edit file for the target GUID.

Integrating Scripts
-------------------

Each script relies on :class:`libusb_uvc.UVCCamera` for interface claiming and
stream lifecycle management. Use them as references when building your own
applications, or import their helper functions directly if you need to iterate
quickly.

Future Work
-----------

- **Compressed payload codecs (H.264/H.265/AV1/VP8)**: support is not yet
  implemented; handling those streams would require parsing their specific UVC
  payload headers and integrating suitable decoders.
- **UVC still-image capture**: the still-image trigger and transfer flow remains
  on the roadmap; implementing it means wiring the dedicated controls and
  endpoints defined by the specification.
- **Vendor-specific controls**: even when a selector is advertised, many
  firmwares only respond to proprietary messages.  Completing those features
  demands reverse engineering or documentation from the manufacturer.
