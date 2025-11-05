How-to Recipes
==============

The recipes in this section provide step-by-step instructions for common tasks
when working with libusb-uvc. Each example builds on the core API documented
elsewhere.

.. toctree::
   :maxdepth: 1

   gadget_testing

Run Unit Tests
--------------

1. Install the project in editable mode (or ensure :mod:`pytest` is available)::

      python3 -m pip install -e .[tests]

2. Execute the emulator-backed unit suite::

      python3 -m pytest tests/test_controls.py

USB Gadget Integration Tests
----------------------------

For full end-to-end coverage, configure a virtual camera via FunctionFS and the
``dummy_hcd`` module.  Detailed Debian instructions live in :doc:`gadget_testing`.
Once the gadget is present, enable the tests by exporting
``LIBUSB_UVC_ENABLE_GADGET_TESTS=1`` and run ``python -m pytest
tests/test_integration.py``.

Disable Auto Exposure
---------------------

1. Open the camera with :meth:`libusb_uvc.UVCCamera.open`.
2. Enumerate controls via :meth:`libusb_uvc.UVCCamera.enumerate_controls`.
3. Look for ``Auto Exposure Mode`` or similar, then call
   :meth:`libusb_uvc.UVCCamera.set_control` with value ``1`` (manual mode).

   .. code-block:: python

      auto = cam.get_control("Auto Exposure Mode")
      cam.set_control(auto, 1)

4. Adjust ``Exposure Time, Absolute`` as needed.

Sweep Exposure While Streaming
------------------------------

1. Disable auto exposure (as above).
2. Build a list of exposure values between ``minimum`` and ``maximum`` gathered
   from the control metadata.
3. Start a stream and update the control inside the frame loop.

Refer to ``examples/exposure_sweep.py`` for a complete implementation.

Toggle an Indicator LED
-----------------------

Some cameras provide LED control via extension units. Use the control manager
to locate relevant selectors:

.. code-block:: python

   for control in cam.enumerate_controls(refresh=True):
       if "led" in control.name.lower():
           cam.set_control(control, 0)

Graceful Shutdown
-----------------

Always use the context manager on :class:`libusb_uvc.UVCCamera`. When the
``with`` block exits, the library stops streaming, resets the device if
required, and reattaches kernel drivers so V4L2 clients can resume without
manual intervention.

Next Steps
----------

- Browse :doc:`../examples` to see the recipes applied in full scripts.
- Dive into :doc:`../api` for low-level control helpers and asynchronous
  streaming primitives.
