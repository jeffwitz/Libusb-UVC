Getting Started
===============

This section walks through environment preparation, device discovery, and
launching your first stream using :mod:`libusb_uvc`.

Installation
------------

Libusb-UVC targets Python 3.8 and above. Create a virtual environment and
install the project in editable mode so that the example scripts are available::

   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e .[full]

The ``full`` extra installs OpenCV and Pillow, which enables the MJPEG preview
and single-frame helpers. If you only need device introspection or custom
integrations you can omit the extra.

System Dependencies
-------------------

Install the USB libraries and V4L tooling commonly required on Linux::

   sudo apt-get install -y python3 python3-pip libusb-1.0-0 v4l-utils

For the optional MJPEG preview window, also install GStreamer bindings::

   sudo apt-get install -y python3-gi gir1.2-gst-1.0 gstreamer1.0-plugins-good

Building Distribution Artifacts
-------------------------------

If you need a wheel or source distribution, invoke the PEP 517 build frontend::

   python3 -m build

This requires the :mod:`build` and :mod:`wheel` modules.  On Debian/Ubuntu they
are packaged as ``python3-build`` and ``python3-wheel``; inside a virtual
environment you can instead ``pip install build wheel``.

Udev Rules
----------

Accessing UVC hardware without ``sudo`` typically requires adding a udev rule.
Adapt the sample provided in ``udev/99-hp-5mp-camera.rules`` to match your
camera's Vendor ID (VID) and Product ID (PID). After copying the file to
``/etc/udev/rules.d/`` reload the rules and unplug/replug the camera.

First Stream
------------

The quickest way to check your setup is via the OpenCV preview helper::

   python3 examples/uvc_capture_video.py \
       --vid 0x0408 --pid 0x5473 \
       --width 1920 --height 1080 \
       --fps 30 --codec mjpeg \
       --duration 10

The script claims both the control and streaming interfaces, negotiates
PROBE/COMMIT, and resets the device on exit so ``/dev/video*`` remains usable.
Press ``q`` or ``Esc`` to close the window early.

Multiple Sensors on One Device
------------------------------

Some hardware (for example laptops with RGB and IR sensors in the same module)
exposes several Video Streaming interfaces.  Run the preview helper with
``--list`` to discover every interface and format::

   python3 examples/uvc_capture_video.py --vid 0x0408 --pid 0x5473 --list

Then pass ``--interface`` to select the desired stream.  Interface ``1`` on the
HP 5 MP camera is RGB, while interface ``3`` provides a 400x400 infrared feed::

   python3 examples/uvc_capture_video.py \
       --vid 0x0408 --pid 0x5473 \
       --interface 3 \
       --width 400 --height 400 --fps 15 --codec yuyv

The same flag works throughout the examples directory, including the dedicated
``uvc_ir_inspect.py`` helper which lists controls and saves IR frames.

Minimal Python Usage
--------------------

The core API exposes :class:`libusb_uvc.UVCCamera`, which manages interface
claiming and streaming::

   from libusb_uvc import UVCCamera, CodecPreference

   with UVCCamera.open(vid=0x0408, pid=0x5473, interface=1) as cam:
       original_exposure = cam.get_control("Exposure Time, Absolute")
       cam.set_control("Exposure Time, Absolute", 200)

       with cam.stream(width=640, height=480, codec=CodecPreference.MJPEG, duration=5) as frames:
           for frame in frames:
               rgb = frame.to_rgb()
               # process numpy array ...
               break

       if original_exposure is not None:
           cam.set_control("Exposure Time, Absolute", original_exposure)

When experimenting with compressed codecs (H.264/H.265) you can supply a
decoder preference explicitly::

   from libusb_uvc import DecoderPreference

   with cam.stream(decoder=DecoderPreference.PYAV, duration=5) as frames:
       ...

If you pick a specific backend, MJPEG payloads are routed through it as well so
you can validate the plumbing on legacy cameras.  The embedded GStreamer
pipeline already covers MJPEG, H.264, and H.265 (``jpegdec``/``avdec_h26*``),
while PyAV provides software MJPEG + H.264/HEVC decoding.  Leave the preference
at ``auto`` to keep the historical MJPEG/uncompressed fast paths.

Next Steps
----------

- :doc:`controls_and_streaming` explains how descriptors map to Python objects.
- :doc:`examples` documents each helper script that ships with libusb-uvc.
- :doc:`api` lists the full reference for controls, streaming helpers, and low-level utilities.
