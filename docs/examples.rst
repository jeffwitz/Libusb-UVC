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

``exposure_sweep.py``
---------------------

Disable auto exposure, then linearly sweep ``Exposure Time, Absolute`` across
its supported range while overlaying the current value on the preview. The
example demonstrates how to update controls without resetting the stream.

Integrating Scripts
-------------------

Each script relies on :class:`libusb_uvc.UVCCamera` for interface claiming and
stream lifecycle management. Use them as references when building your own
applications, or import their helper functions directly if you need to iterate
quickly.

