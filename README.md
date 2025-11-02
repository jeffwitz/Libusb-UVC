# Libusb-UVC: A Robust Python UVC Streaming Toolkit

Libusb-UVC is a lightweight Python toolkit for inspecting and streaming from UVC (USB Video Class) cameras. It provides a robust, low-level streaming core built on `libusb1` while leveraging the high-level convenience of `PyUSB` for device discovery and descriptor parsing.

This hybrid approach was designed to solve common issues with complex or "quirky" camera firmwares. The entire critical streaming sequence—PROBE/COMMIT negotiation, alternate setting selection, and isochronous transfers—is managed on a single `libusb1` handle, mirroring the stable behavior of the Linux kernel's `uvcvideo` driver.

## Key Features

- **High-Level Pythonic API**: `UVCCamera.open()` and `UVCCamera.stream()` provide context-managed streaming, frame iterators, and one-line control access (`get_control()` / `set_control()`).
- **Robust Streaming Core**: Reliably streams from complex cameras that fail with simpler negotiation methods.
- **Graceful Kernel Integration**: libusb captures are followed by an automatic USB reset so `/dev/video*` nodes and `uvcvideo` are restored immediately.
- **Comprehensive Tooling**: Includes CLI scripts for listing device capabilities, grabbing single frames, and launching live previews.
- **Decoder-Agnostic**: Provides raw frame data (YUYV, MJPEG), ready to be used with libraries like OpenCV, Pillow, or GStreamer.

## Core Components

- `libusb_uvc` (under `src/`): the Python package containing the high-level API and asynchronous backend.
- `examples/`: ready-to-run demonstrations and utilities (`uvc_capture_video.py`, `uvc_capture_frame.py`, `uvc_display_frame.py`, `uvc_inspect.py`, `uvc_led_preview.py`, `uvc_generate_quirk.py`, `exposure_sweep.py`).
- `udev/`: an example udev rule for granting non-root access to USB devices.

## 1. Setup

### System Dependencies

```bash
sudo apt-get install -y python3 python3-pip libusb-1.0-0 v4l-utils
```

For the MJPEG live preview, you will also need GStreamer packages:
`sudo apt-get install -y python3-gi gir1.2-gst-1.0 gstreamer1.0-plugins-good`

### Python Environment

Use the provided `pyproject.toml` to install the library (and optionally the example scripts) in editable mode:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[full]   # "full" installs OpenCV and Pillow for the examples
```

### Documentation

Install the documentation extras and build the Sphinx site locally:

```bash
pip install -e .[docs]
sphinx-build -M html docs docs/_build
```

On Debian/Ubuntu you can instead rely on the packaged tooling:

```bash
sudo apt-get install python3-sphinx python3-sphinx-rtd-theme
sphinx-build -M html docs docs/_build
```

The generated HTML will be available at `docs/_build/html/index.html`.

### Building distribution artifacts

To build wheels or source archives locally use:

```bash
python3 -m build
```

The command relies on the `build` and `wheel` modules.  On Debian/Ubuntu install
them via `sudo apt-get install python3-build python3-wheel`, or inside a virtual
environment run `pip install build wheel`.

### Udev Rule (for non-root access)

To access the camera without `sudo`, copy and adapt the provided udev rule, then reload the system rules.

```bash
#
# IMPORTANT: Edit the rule to match your camera's Vendor and Product ID!
# Use `lsusb` to find the correct values for ATTR{idVendor} and ATTR{idProduct}.
#
sudo cp udev/99-hp-5mp-camera.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```
Unplug and replug the camera to apply the new permissions. Ensure your user is a member of the `plugdev` group (`id -nG`).

## 2. Usage

### List available formats & controls

```bash
python3 examples/uvc_inspect.py --vid 0x0408 --pid 0x5473 --verbose
```

The script uses the new `UVCControlsManager` to print validated controls (including quirk names such as *LED Control*) and can still run probe/commit tests with `--probe-interface`, `--probe-format`, `--commit`, etc.

### Capture a single frame

```bash
python3 examples/uvc_capture_frame.py \
    --vid 0x0408 --pid 0x5473 \
    --width 1920 --height 1080 --fps 30 \
    --codec mjpeg \
    --output frame.jpg
```

The script relies on `UVCCamera.stream()` to grab one frame, automatically converts MJPEG/YUYV when possible, and resets the device when it exits so `/dev/video*` remains usable.

### Live video preview (OpenCV)

```bash
python3 examples/uvc_capture_video.py \
    --vid 0x0408 --pid 0x5473 \
    --width 1920 --height 1080 \
    --fps 30 --codec mjpeg \
    --duration 10   # optional, auto-stop after N seconds
```

`UVCCamera.stream()` feeds a frame iterator; pressing `q`/`ESC` still stops the preview. The device is reset on exit, so a subsequent `mplayer tv:// -tv driver=v4l2:device=/dev/video0` continues to work without unplugging the camera.

For a scripted example that also toggles the LED after a delay, see `examples/uvc_led_preview.py`.

To play with manual exposure, try `examples/exposure_sweep.py`, which disables auto exposure and sweeps `Exposure Time, Absolute` from its minimum to maximum over 300 frames while overlaying the current value on the preview window.

### Generate a quirks skeleton

```bash
python3 examples/uvc_generate_quirk.py \
    --vid 0x0408 --pid 0x5473 \
    --single --output quirk.json
```

The script inspects Extension Unit selectors and writes a ready-to-edit JSON file that can be dropped into `src/libusb_uvc/quirks/`.

### Minimal Python example

```python
import usb.core

from libusb_uvc import UVCCamera, CodecPreference, UVCError  # or: from uvc_usb import ... (legacy shim)

with UVCCamera.open(vid=0x0408, pid=0x5473, interface=1) as cam:
    controls = {ctrl.name: ctrl for ctrl in cam.enumerate_controls(refresh=True)}

    auto_mode = controls.get("Auto Exposure Mode")
    if auto_mode and auto_mode.is_writable():
        try:
            cam.set_control(auto_mode, 1)  # Manual mode
        except (UVCError, usb.core.USBError):
            pass

    auto_priority = controls.get("Auto Exposure Priority")
    if auto_priority and auto_priority.is_writable():
        try:
            cam.set_control(auto_priority, 0)
        except (UVCError, usb.core.USBError):
            pass

    original_exposure = cam.get_control("Exposure Time, Absolute")
    try:
        cam.set_control("Exposure Time, Absolute", 200)
    except (UVCError, usb.core.USBError):
        pass

    with cam.stream(width=640, height=480, codec=CodecPreference.MJPEG, duration=5) as frames:
        for frame in frames:
            rgb = frame.to_rgb()
            # ... process numpy array ...
            break

    if original_exposure is not None:
        try:
            cam.set_control("Exposure Time, Absolute", original_exposure)
        except (UVCError, usb.core.USBError):
            pass
```

The stream iterator handles all PROBE/COMMIT steps, asynchronous transfers, and frame reassembly for you.

## 3. Troubleshooting

- **Permission Denied:** Ensure your udev rule is correctly installed, has the right VID/PID, and that your user is in the `plugdev` group.
- **Negotiation failures:** Run `examples/uvc_inspect.py` with `--probe...` flags and `--log-level DEBUG` to inspect the PROBE/COMMIT sequence.
- **Frame Drops / Corrupted Video:** This can be a USB bandwidth issue. Try a lower resolution, a lower `--fps`, or connect the camera to a different USB port (preferably a direct port on the motherboard).
- **V4L2 missing after capture:** The library now issues `device.reset()` when a libusb stream stops. If you disabled this behaviour, call `camera.stop_streaming()` or `camera.reset_device()` before returning control to V4L2 applications.
- **VC auto-detach:** By default the VC interface is temporarily detached so user-space control transfers work even when `uvcvideo` is active. Set `LIBUSB_UVC_AUTO_DETACH_VC=0` to disable this and handle detaching yourself.
- **Useful extras:** Install `[opencv]`, `[pillow]`, or `[full]` extras if you want MJPEG previews, Matplotlib demos, or frame conversions out of the box.
