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

Controls Missing or Unnamed
---------------------------

**Symptom:** ``UVCControlsManager`` returns only generic selectors.

**Resolution:** Add a ``GUID``-keyed quirks JSON file describing the extension
unit selectors under ``src/libusb_uvc/quirks``. The manager merges quirk names,
``GET_INFO`` responses, and descriptor metadata to present human-readable
control names.

Further Help
------------

- Enable ``--log-level DEBUG`` on examples to capture additional diagnostics.
- Review the :doc:`api` reference for lower-level helper functions.
- File issues with detailed logs and ``uvc_inspect`` output when encountering
  device-specific quirks not covered here.

