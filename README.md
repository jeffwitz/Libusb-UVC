# Libusb-UVC: A Robust Python UVC Streaming Toolkit

Libusb-UVC is a lightweight Python toolkit for inspecting and streaming from UVC (USB Video Class) cameras. It provides a robust, low-level streaming core built on `libusb1` while leveraging the high-level convenience of `PyUSB` for device discovery and descriptor parsing.

This hybrid approach was designed to solve common issues with complex or "quirky" camera firmwares. The entire critical streaming sequence—PROBE/COMMIT negotiation, alternate setting selection, and isochronous transfers—is managed on a single `libusb1` handle, mirroring the stable behavior of the Linux kernel's `uvcvideo` driver.

## Key Features

-   **Robust Streaming Core**: Reliably streams from complex cameras that fail with simpler negotiation methods.
-   **High-Performance Async API**: Manages multiple in-flight isochronous transfers for low-latency, high-bandwidth video.
-   **Hybrid USB Backend**: Uses `PyUSB` for easy device enumeration and `libusb1` for performance-critical streaming, getting the best of both worlds.
-   **Comprehensive Tooling**: Includes CLI scripts for listing device capabilities, capturing raw frames, and launching live video previews.
-   **Decoder-Agnostic**: Provides raw frame data (YUYV, MJPEG), ready to be used with libraries like OpenCV, Pillow, or GStreamer.

## Core Components

-   `uvc_usb.py`: The main module with high-level helpers and the `UVCCamera` class.
-   `uvc_async.py`: A lightweight wrapper around `libusb1`'s asynchronous transfer API.
-   **Example Scripts**: `pyusb_uvc_info.py`, `pyusb_capture_frame.py`, and `pyusb_capture_video.py` demonstrate the library's capabilities.
-   `udev/`: Contains an example udev rule for granting non-root access to USB devices.

## 1. Setup

### System Dependencies

```bash
sudo apt-get install -y python3 python3-pip libusb-1.0-0 v4l-utils
```

For the MJPEG live preview, you will also need GStreamer packages:
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

### List Available Formats

List all streaming modes for a specific camera.

```bash
python3 pyusb_uvc_info.py --vid 0x0408 --pid 0x5473
```
Add flags like `--probe-interface`, `--probe-format`, and `--commit` to test a specific stream configuration.

### Capture a Single Raw Frame

Capture a single frame and save its raw payload to a file. The script defaults to YUYV but can be configured for MJPEG.

```bash
python3 pyusb_capture_frame.py \
    --vid 0x0408 --pid 0x5473 \
    --width 640 --height 480 --fps 30 \
    --output frame.raw
```

### Live Video Preview (OpenCV / GStreamer)

This script provides a real-time video preview using the asynchronous streaming API.

**For YUYV streams (lower resolutions):**
An OpenCV window will display the decoded video feed.
```bash
python3 pyusb_capture_video.py \
    --vid 0x0408 --pid 0x5473 \
    --width 640 --height 480 \
    --fps 15 --codec yuyv
```

**For MJPEG streams (higher resolutions):**
The script pipes the JPEG frames to a GStreamer pipeline for efficient hardware-accelerated decoding.
```bash
python3 pyusb_capture_video.py \
    --vid 0x0408 --pid 0x5473 \
    --width 1920 --height 1080 \
    --fps 30 --codec mjpeg
```
Press `q` or `ESC` in the preview window to exit. Use `--log-level DEBUG` for detailed packet-level information.

## 3. Troubleshooting

-   **Permission Denied:** Ensure your udev rule is correctly installed, has the right VID/PID, and that your user is in the `plugdev` group.
-   **No Frames Received (Empty Transfers):** This usually indicates a negotiation failure. Run `pyusb_uvc_info.py` with `--probe...` flags and `--log-level DEBUG` to inspect the PROBE/COMMIT sequence.
-   **Frame Drops / Corrupted Video:** This can be a USB bandwidth issue. Try a lower resolution, a lower `--fps`, or connect the camera to a different USB port (preferably a direct port on the motherboard).
