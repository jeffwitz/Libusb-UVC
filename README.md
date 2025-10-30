# Lightweight UVC Toolkit with PyUSB & libusb1

This repository provides a toolkit for inspecting and streaming from UVC (USB Video Class) cameras using PyUSB and `libusb1`. The core logic has been aligned with the Linux kernel's `uvcvideo` driver to ensure compatibility with complex cameras that require a strict negotiation sequence.

The streaming stack (PROBE/COMMIT, alternate setting selection, and isochronous transfers) is managed on a single `libusb1` handle, providing stable, high-bandwidth video streams. The toolkit has been successfully tested with devices like the ELP6USB4KCAM01H-CF.

## Core Components

-   `uvc_usb.py`: High-level helpers for UVC descriptor parsing, PROBE/COMMIT negotiation, and an asynchronous capture API built on `libusb1`.
-   `uvc_async.py`: A lightweight wrapper around `libusb1` to manage multiple in-flight isochronous transfers, enabling low-latency streaming.
-   `pyusb_uvc_info.py`: CLI tool to list all streaming interfaces, formats, and frames. Can also run a PROBE/COMMIT to show negotiated bandwidth.
-   `pyusb_capture_frame.py`: Captures a single raw frame (YUYV or MJPEG) and saves it to a file.
-   `pyusb_capture_video.py`: Provides a live video preview using OpenCV (for YUYV) or GStreamer (for MJPEG).
-   `udev/99-hp-5mp-camera.rules`: An example udev rule to grant non-root users access to a specific USB camera.

## 1. Setup

### System Dependencies

```bash
sudo apt-get install -y python3 python3-pip libusb-1.0-0 v4l-utils
```

For MJPEG preview, you will also need GStreamer packages:
`sudo apt-get install -y python3-gi gir1.2-gst-1.0 gstreamer1.0-plugins-good`

### Python Environment

All Python dependencies are listed in `requirements.txt`.

```bash
# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Udev Rule (for non-root access)

To access the camera without `sudo`, copy the provided udev rule and reload the system rules.

```bash
#
# IMPORTANT: Edit the rule to match your camera's Vendor and Product ID!
# Use `lsusb` to find the correct values.
#
sudo cp udev/99-hp-5mp-camera.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```
Unplug and replug the camera to apply the new permissions. Ensure your user is a member of the `plugdev` group (`id -nG`).

## 2. Usage

### List Available Formats

List all streaming modes for a specific camera.

```bash
python3 pyusb_uvc_info.py --vid 0x0408 --pid 0x5473
```
Add flags like `--probe-interface`, `--probe-format`, and `--commit` to test a specific stream configuration.

### Capture a Single Raw Frame

Capture a single frame and save its raw payload to a file. The script defaults to YUYV.

```bash
python3 pyusb_capture_frame.py \
    --vid 0x0408 --pid 0x5473 \
    --width 640 --height 480 --fps 30 \
    --output frame.raw
```

### Live Video Preview (OpenCV / GStreamer)

This script provides a real-time video preview.

**For YUYV streams (lower resolutions):**
```bash
python3 pyusb_capture_video.py \
    --vid 0x0408 --pid 0x5473 \
    --width 640 --height 480 \
    --fps 15 --codec yuyv
```
An OpenCV window will open. Press `q` or `ESC` to exit.

**For MJPEG streams (higher resolutions):**
The script will automatically pipe the JPEG frames to a GStreamer pipeline for efficient decoding and display.
```bash
python3 pyusb_capture_video.py \
    --vid 0x0408 --pid 0x5473 \
    --width 1920 --height 1080 \
    --fps 30 --codec mjpeg
```
Use `--log-level DEBUG` for detailed packet-level information and troubleshooting.

## 3. Troubleshooting

-   **Permission Denied:** Ensure your udev rule is correctly installed and that your user is in the `plugdev` group.
-   **No Frames Received (Empty Transfers):** This usually indicates a negotiation failure. Run `pyusb_uvc_info.py` with `--probe...` flags and `--log-level DEBUG` to inspect the PROBE/COMMIT sequence.
-   **Frame Drops / Corrupted Video:** This can be a USB bandwidth issue. Try a lower resolution, a lower `--fps`, or connect the camera to a different USB port (preferably a direct port on the motherboard).
