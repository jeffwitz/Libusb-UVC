---
title: 'libusb-uvc: A Python framework for precise, low-level control of USB Video Class devices'
tags:
  - Python
  - computer vision
  - usb
  - uvc
  - camera
  - stereo-vision
  - synchronization
authors:
  - name: Jeff Witz
    orcid: 0000-0000-0000-0000
    affiliation: 1
affiliations:
 - name: Independent Researcher, France
   index: 1
date: 24 November 2025
bibliography: paper.bib
---

# Summary

`libusb-uvc` is a pure Python library designed to provide granular, protocol-level control over USB Video Class (UVC) devices. Unlike high-level computer vision libraries that abstract away the underlying hardware communication, `libusb-uvc` exposes the full capabilities of the UVC 1.1 and 1.5 specifications [@uvc15]. It allows researchers and developers to manipulate Extension Units, manage raw USB transfers, and orchestrate precise timing events, such as synchronized multi-camera streaming. By building directly on top of `libusb` [@libusb] and `PyUSB` [@pyusb], it offers a cross-platform solution for turning consumer-grade webcams into scientific instruments.

# Statement of Need

In the field of computer vision and robotics, researchers often rely on standard libraries like OpenCV [@opencv_library] to acquire images. While OpenCV is excellent for image processing, its video capture interface (`cv::VideoCapture`) is designed for compatibility and ease of use, not for hardware control. It treats cameras as "black boxes," abstracting away critical low-level features.

This abstraction becomes problematic for scientific applications requiring precise multi-camera synchronization. As noted by Danciu et al. [@danciu2020synchronization], standard USB drivers often introduce variable latency during initialization, making precise synchronization of multiple cameras impossible without hardware triggers. This lack of synchronization can lead to significant errors in 3D reconstruction, especially for moving objects [@shao2019synchronization].

While low-cost stereo vision systems are increasingly popular for robot navigation [@zaman2014low], they often suffer from these synchronization artifacts. Existing solutions typically involve:
1.  **Hardware Triggers**: Using expensive industrial cameras with GPIO triggers.
2.  **Post-Processing**: Attempting to align streams based on timestamps or visual cues, which is computationally expensive and often inaccurate.
3.  **Complex Frameworks**: Using heavy frameworks like ROS [@ros_stereo_image_proc] which may still rely on the same underlying drivers.

`libusb-uvc` fills this gap by providing a Pythonic interface to the raw UVC protocol, enabling software-based synchronization strategies that were previously inaccessible to Python developers. It allows for:

*   **Deterministic Startup**: By separating the bandwidth negotiation from the stream activation, users can synchronize the start of multiple cameras with sub-millisecond precision.
*   **Raw Control Packets**: Accessing vendor-specific features (Extension Units) or modifying UVC controls (Exposure, Gain) often requires OS-specific hacks or is simply unsupported.
*   **USB Transfer Management**: High-bandwidth applications (e.g., uncompressed stereo streams) require fine-grained control over isochronous transfers and buffering to minimize latency and packet loss.

Researchers needing these features typically resort to writing custom C/C++ drivers or using expensive industrial cameras with proprietary SDKs. `libusb-uvc` fills this gap by providing a Pythonic interface to the raw UVC protocol. It enables:

1.  **Split PROBE/COMMIT Negotiation**: By separating the bandwidth negotiation from the stream activation, users can synchronize the start of multiple cameras with sub-millisecond precision.
2.  **Restart-to-Sync**: A brute-force synchronization strategy that leverages the library's fast startup times to align independent camera clocks.
3.  **Cross-Platform Consistency**: By bypassing the OS's native camera stack (DirectShow, V4L2, AVFoundation), `libusb-uvc` behaves identically on Linux, macOS, and Windows.

# Key Features

*   **Pure Python Implementation**: Built on top of `libusb1` and `pyusb`, requiring no compilation of C extensions or installation of system-level drivers.
*   **Full UVC Specification Support**: Implements the parsing of complex UVC descriptors, including Input Terminals, Processing Units, and Extension Units.
*   **Asynchronous Streaming**: Leverages `libusb`'s asynchronous API to handle high-bandwidth isochronous transfers efficiently in a background thread.
*   **Vendor-Agnostic**: Works with any UVC-compliant device, while also providing a plugin system for vendor-specific "Quirks" (e.g., enabling raw streams on specific sensors).
*   **Stereo Synchronization**: Includes a reference implementation for synchronizing dual-camera setups using the split PROBE/COMMIT strategy.

# Example Usage

The following example demonstrates how `libusb-uvc` allows for a deterministic start of a camera stream, a prerequisite for the stereo synchronization described in the documentation:

```python
from libusb_uvc import UVCCamera, CodecPreference

# Open the camera
camera = UVCCamera.from_serial("12345678")

# 1. PROBE: Negotiate parameters (variable time)
negotiation = camera.probe_stream(
    width=1920, height=1080, fps=30, codec=CodecPreference.MJPEG
)

# ... Wait for other cameras or external trigger ...

# 2. COMMIT: Start streaming (deterministic time)
camera.commit_stream(negotiation)

# 3. Stream frames
for frame in camera.stream(configure=False):
    print(f"Frame received: {frame.timestamp}")
```

# Research Applications

`libusb-uvc` has been developed to support low-cost stereo reconstruction. By enabling precise synchronization of off-the-shelf USB cameras, it allows for the creation of depth sensing arrays at a fraction of the cost of industrial solutions. The library includes a reference implementation for stereo capture that handles clock drift compensation and frame pairing strategies.

# References
