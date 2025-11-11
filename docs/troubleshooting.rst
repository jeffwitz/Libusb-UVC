Troubleshooting
===============

Camera integration often involves platform quirks or hardware limitations.
This guide collects the most common issues observed while developing with
libusb-uvc and offers remediation steps.

Permission Errors
-----------------

**Symptom:** ``usb.core.NoBackendError`` or ``LIBUSB_ERROR_ACCESS`` when
calling :func:`libusb_uvc.find_uvc_devices`.

**Resolution:** Install an appropriate udev rule matching the camera's VID/PID
and ensure your user belongs to the ``plugdev`` (or distribution equivalent)
group. After applying rules, reload them and replug the camera.

``uvcvideo`` Node Disappears
----------------------------

**Symptom:** ``/dev/video0`` vanishes after running a streaming script.

**Resolution:** Libusb-UVC automatically resets the USB device when it
previously detached the kernel driver. If you've disabled that behaviour,
re-enable ``camera.reset_device()`` on stream teardown or reboot the bus.

Probe/Commit Fails
------------------

**Symptom:** ``LIBUSB_ERROR_PIPE`` or ``LIBUSB_ERROR_TIMEOUT`` during stream
setup.

**Resolution:** Inspect the interface with ``python3 examples/uvc_inspect.py
--verbose``. Try lower resolutions, reduce ``--fps``, or request ``CodecPreference.YUYV``.
Some cameras require committing the default frame interval. Double-check that
no other client has claimed the interface.

Dropped or Corrupted Frames
---------------------------

**Symptom:** Visible artefacts, truncated frames, or frequent warnings in the
log output.

**Resolution:** Isochronous transfers are constrained by USB bandwidth. Move
the camera to a direct root-port, lower the frame size or frame rate, or adjust
the ``queue_size`` passed to :meth:`libusb_uvc.UVCCamera.stream`.

Frame-based H.264/H.265 Quirks
------------------------------

**Symptom:** The device advertises a ``Frame-based`` format but decoders report
``Invalid data found when processing input`` or ``Unsupported codec for conversion``.

**Cause:** UVC 1.5 allows cameras to send H.264/H.265 payloads without
repeating the Sequence Parameter Set (SPS) and Picture Parameter Set (PPS) in
every frame.  Some implementations go further and start streaming P-slices
before emitting any SPS/PPS/IDR trio, leaving host decoders without the context
they need.

**Libusb-UVC behaviour:** The library detects Annex B and AVC payload layouts,
caches the latest SPS/PPS it receives, and injects them ahead of IDR frames so
PyAV and GStreamer can initialise.  Frames delivered before the configuration
arrives are dropped; as soon as SPS/PPS appear the cached copy is reused for the
rest of the session.

**Resolution:** If the firmware never supplies SPS/PPS (it happens on some
cheaper hardware) the stream remains undecodable.  You can:

* Capture a raw payload with ``uvc_capture_frame.py --codec h264 --output`` and
  inspect it with ``hexdump``—look for ``00 00 00 01 67`` (SPS) or ``... 01 65``
  (IDR).
* Check whether the vendor provides a driver that exposes H.264 Extension Unit
  controls containing SPS/PPS blobs.
* File a bug with the camera vendor; the host cannot reconstruct missing SPS/PPS
  without reverse-engineering the firmware.

Recording Plays Too Fast/Slow
-----------------------------

**Symptom:** Saved ``.mkv``/``.avi`` files appear to run at 2x speed or crawl when opened in VLC/mpv.

**Resolution:** The recorder uses presentation timestamps from the camera
payload headers. Some firmwares omit PTS entirely, so Libusb-UVC synthesises
monotonic timestamps from the negotiated FPS. If playback is still off, verify
the stream actually negotiated the expected FPS (look for ``Stream running at
XX.XX fps`` in the logs) and lower the requested frame rate to one of the
advertised intervals listed by ``uvc_inspect.py``.

Recording Files Are Empty
-------------------------

**Symptom:** ``.mkv``/``.avi`` outputs are zero bytes or players report ``End of file``.

**Resolution:** Some cameras take several seconds to emit the first IDR / key
frame. Let the recorder run for longer (10s+ on the HDMI grabber reference
device) so the pipeline can flush its header. Ensure you requested a decoder
backend (PyAV or GStreamer) and that the relevant dependencies are installed.
MJPEG recordings require either PyAV or the GStreamer fallback; frame-based
codecs require PyAV or GStreamer with the matching ``h264parse``/``h265parse``
plugins.

Controls Missing or Unnamed
---------------------------

**Symptom:** ``UVCControlsManager`` returns only generic selectors.

**Resolution:** Add a ``GUID``-keyed quirks JSON file describing the extension
unit selectors under ``src/libusb_uvc/quirks``. The manager merges quirk names,
``GET_INFO`` responses, and descriptor metadata to present human-readable
control names.

VC Interface Busy
-----------------

**Symptom:** Attempting to read/write controls raises ``[Errno 16] Resource busy``.

**Resolution:** The library normally detaches the Video Control (VC) interface
from ``uvcvideo`` on demand. If you set the environment variable
``LIBUSB_UVC_AUTO_DETACH_VC=0`` you must detach the kernel driver manually (or
run as root). Re-enable auto-detach by unsetting the variable or giving it a
truthy value.

Still Capture Returns Empty Payloads
------------------------------------

**Symptom:** ``uvc_capture_still.py`` succeeds but the saved file contains only
zeros or fails to decode; ``uvc_inspect.py --test-still`` reports ``len=…
head=00 00 …``.

**Resolution:** This behaviour is unfortunately common—many cameras expose
still-image descriptors without wiring the firmware. Use ``uvc_inspect.py
--test-still`` as a quick smoke test whenever you connect a new device; the
command cycles through the first advertised still frame per format and every
published compression index. If every combination fails, assume the camera
requires proprietary commands or OEM software to capture stills.

Further Help
------------

- Enable ``--log-level DEBUG`` on examples to capture additional diagnostics.
- Review the :doc:`api` reference for lower-level helper functions.
- File issues with detailed logs and ``uvc_inspect`` output when encountering
  device-specific quirks not covered here.
