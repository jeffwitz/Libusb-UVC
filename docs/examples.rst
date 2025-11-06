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
* ``--codec`` — choose ``auto``, ``yuyv``, ``mjpeg``, ``frame-based``, ``h264``, or ``h265``.
* ``--decoder`` — select the decoder backend (``auto``, ``none``, ``pyav``, ``gstreamer``).  Picking one explicitly
  routes MJPEG through that backend as well, which lets you validate PyAV or
  GStreamer without H.264 hardware.  The bundled GStreamer pipeline already
  handles MJPEG, H.264, and H.265 (``jpegdec``/``avdec_h26*``) while PyAV covers
  MJPEG + H.264/HEVC in software.
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
combining quirk definitions with live ``GET_*`` queries. Add ``--test-still``
to run a quick “smoke test” on still capture: the helper takes the first
advertised still frame for each format, cycles through the published
compression indices, and reports whether any combination returns a usable
payload.

``uvc_ir_inspect.py``
---------------------

Target the infrared sensor of dual-camera hardware. The script prints every
validated control (using Microsoft XU hints when available) and captures a
handful of IR frames, saving raw payloads alongside optional PNG conversions.
Use ``--interface`` to select the IR streaming interface number.

``uvc_capture_still.py``
------------------------

Negotiate the UVC still-image controls and trigger a single capture. The script
now understands both capture methods defined by the specification:

* **Method 1** — reuse the streaming frame descriptor when ``bmStillSupported``
  is set (mostly older devices).
* **Method 2** — honour dedicated still-image frame descriptors that point to a
  separate endpoint or alternate setting. The helper automatically falls back
  to the highest-resolution still frame when ``--width``/``--height`` are
  omitted and picks a valid compression index advertised by the descriptor.

You can inspect the advertised still frames via ``uvc_inspect.py`` — look for
the new ``Still-image frames`` section — and then run::

   python3 examples/uvc_capture_still.py --vid 0x1b3f --pid 0x1167 --output still.tiff

Support still depends heavily on the firmware: some devices ignore the trigger
without additional vendor messages or require experimentation with compression
indices. The script stores uncompressed payloads as TIFF to preserve bit depth
and falls back to raw dumps when conversion fails. In practice it is **very
common** for cameras to advertise every still resolution yet return empty
payloads. Always start with ``uvc_inspect.py --test-still``; it exercises the
first still frame for each format (and iterates through the published
compression indices) so you can quickly determine whether the firmware ever
produces usable data.

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
- **UVC still-image capture**: Method 1 and Method 2 negotiation work, but
  completing the feature still demands per-device testing (multiple sensors,
  proprietary compression settings, bulk endpoints).
- **Vendor-specific controls**: even when a selector is advertised, many
  firmwares only respond to proprietary messages.  Completing those features
  demands reverse engineering or documentation from the manufacturer.
