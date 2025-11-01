Understanding Controls and Streaming
====================================

Libusb-UVC exposes the structure of UVC descriptors so you can build robust
workflows for camera configuration and streaming. This guide summarises the key
concepts and how they map to the Python API.

Video Control Units
-------------------

Video Control (VC) interfaces contain units such as camera terminals,
processing units, and extension units (XU). During enumeration the library
parses descriptors into :class:`libusb_uvc.UVCUnit` objects containing lists of
controls (:class:`libusb_uvc.UVCControl`).

The :class:`libusb_uvc.UVCControlsManager` validates controls using the UVC
``GET_INFO`` probe and merges metadata defined through *quirks* JSON files.
Controls that respond positively are exposed to higher layers via
:class:`libusb_uvc.ControlEntry`.

Extension Units and Quirks
--------------------------

Many cameras expose proprietary controls via extension units. To assign human
readable names you can ship JSON descriptors under ``src/libusb_uvc/quirks``.
Each file contains a GUID, a friendly name, and a mapping of selector IDs to
control definitions. The helper :func:`libusb_uvc.load_quirks` aggregates these
files at runtime, enabling cameras like the Quanta 5 MP series to present
controls such as ``Privacy Shutter`` and ``LED Control``.

Streaming Interfaces
--------------------

Video Streaming (VS) interfaces describe the data formats, frame sizes, and
alternate settings used for isochronous transfers. The function
:func:`libusb_uvc.list_streaming_interfaces` parses these descriptors into
:class:`libusb_uvc.StreamingInterface` objects which enumerate available
formats and frames.

When a stream is opened, :class:`libusb_uvc.UVCCamera` negotiates the desired
format using ``GET_DEF``/``GET_CUR``/``SET_CUR`` requests, selects an alternate
setting whose maximum packet size can sustain the throughput, and schedules
isochronous transfers through the :mod:`libusb_uvc.uvc_async` backend.

Frame Objects
-------------

Capturing functions return :class:`libusb_uvc.CapturedFrame` instances. These
wrap the raw payload together with the negotiated :class:`libusb_uvc.StreamFormat`
and :class:`libusb_uvc.FrameInfo`. Convenience methods such as
:meth:`libusb_uvc.CapturedFrame.to_rgb` leverage OpenCV or Pillow to convert
MJPEG/YUYV payloads into ready-to-display arrays.

Device Reset and Kernel Cooperation
-----------------------------------

To avoid leaving ``uvcvideo`` in a bind state, the library issues a USB device
reset whenever a stream stops and the kernel driver was detached previously.
This ensures that ``/dev/video*`` nodes reappear immediately, allowing native
V4L2 applications such as ``mplayer`` or ``cheese`` to resume using the camera.

Further Reading
---------------

- :doc:`howto/index` contains practical recipes for control management.
- :doc:`api` documents every data structure and helper function.

