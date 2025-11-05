# Test Suite Overview

This directory contains the test support code for **libusb-uvc**.  The suite is
split into two tiers:

* **Unit tests** (default) – rely exclusively on mocks.  They exercise the
  public control APIs against the shared `UvcEmulatorLogic` implementation.
* **Integration tests** – require a Linux system with `dummy_hcd`, FunctionFS
  and `configfs` configured.  They spin up a user-space gadget daemon that
  exposes a fully virtual UVC device.

All reusable camera simulation code lives in `uvc_emulator.py` and is shared by
both tiers.

## Running the unit tests

The unit tests do not need any special privileges.  Install the project in
editable mode with the testing extras (or ensure `pytest` is available) and run:

```bash
python -m pytest tests/test_controls.py tests/test_streaming.py
```

The control tests load the sample profile located in `tests/data/` and ensure
that the control manager interacts with the emulator exclusively through mock
PyUSB objects.  Assertions cover both the high-level enumeration and the raw
``vc_ctrl_get`` / ``vc_ctrl_set`` helpers so that round-trips are validated at
every layer.

The streaming tests exercise :class:`libusb_uvc.UVCCamera.configure_stream`
and :meth:`libusb_uvc.UVCCamera.read_frame` against the emulator.  They verify
that the negotiated endpoint/payload metadata matches expectations and that the
captured MJPEG payload is byte-for-byte identical to the fixture stored in
``tests/data/test_video.mjpeg``.

## Running the integration tests

The integration tests are **opt-in** because they require kernel support for
USB gadgets.  To enable them:

1. Load the gadget host controller driver:
   ```bash
   sudo modprobe dummy_hcd
   ```
2. Create a UVC gadget using `configfs` and mount FunctionFS at `/dev/ffs/uvc`.
   A minimal wrapper script is included under `tests/configfs/` (not provided by
   default – adapt to your environment).
3. Export the following environment variable before running pytest:
   ```bash
   export LIBUSB_UVC_ENABLE_GADGET_TESTS=1
   ```

Optional variables:

* `LIBUSB_UVC_FFS_PATH` – custom FunctionFS mount point (default `/dev/ffs/uvc`).
* `LIBUSB_UVC_GADGET_LOG` – log level for `gadget_daemon.py` (default `INFO`).

Once the environment is ready, execute:

```bash
python -m pytest tests/test_integration.py
```

The fixture starts `tests/gadget_daemon.py`, waits for the virtual camera to
appear and then validates control round-trips as well as MJPEG streaming.

## Data files

`tests/data/sample_camera_profile.json` – baseline profile used by the emulator
for both unit and integration tests.

`tests/data/test_video.mjpeg` – minimal MJPEG payload consumed by
`UvcEmulatorLogic.get_next_video_packet()`.

`tests/data/uvc_descriptors.bin` / `tests/data/uvc_strings.bin` – placeholder
FunctionFS descriptor blobs.  Replace them with descriptors matching your
configfs setup when running the integration tests for real.

## Notes

* The emulator only implements the subset of UVC required by the test-suite. It
  can be extended by editing the JSON profile.
* When integration tests are disabled, the module-level fixtures skip
  gracefully so that `python -m pytest tests` remains fast.
