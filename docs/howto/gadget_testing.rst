USB Gadget Testing on Debian
============================

This guide describes how to configure a Debian host so that the integration
suite can exercise libusb-uvc against a virtual camera exposed through the USB
FunctionFS interface.  The commands assume root privileges.

Prerequisites
-------------

* Debian 12 (Bookworm) or later with a kernel that provides ``dummy_hcd`` and
  ``libcomposite``.
* ``python3`` and ``pytest`` installed for running the test-suite.

Install helper packages::

   sudo apt-get update
   sudo apt-get install python3-pip python3-pytest linux-headers-$(uname -r)

Load the gadget modules::

   sudo modprobe dummy_hcd
   sudo modprobe libcomposite

The ``dummy_hcd`` module creates a virtual USB controller (visible under
``/sys/class/udc``) that we can bind the gadget to; ``libcomposite`` enables the
ConfigFS API used below.

Create the gadget skeleton
--------------------------

1. Mount ``configfs`` (usually done by systemd; no-op if already mounted)::

      sudo mount -t configfs none /sys/kernel/config

2. Create a new gadget and populate the mandatory string descriptors::

      sudo mkdir -p /sys/kernel/config/usb_gadget/pyuvc
      cd /sys/kernel/config/usb_gadget/pyuvc
      echo 0x1234 | sudo tee idVendor
      echo 0x5678 | sudo tee idProduct
      sudo mkdir -p strings/0x409
      echo "PyUVC" | sudo tee strings/0x409/manufacturer
      echo "PyUVC Virtual Camera" | sudo tee strings/0x409/product
      echo "0001" | sudo tee strings/0x409/serialnumber

   The vendor/product IDs should match the ones defined in the JSON profile used
   by the emulator (``tests/data/sample_camera_profile.json`` by default).

3. Define a configuration and attach a FunctionFS instance::

      sudo mkdir -p configs/c.1/strings/0x409
      echo "PyUVC Config" | sudo tee configs/c.1/strings/0x409/configuration
      sudo mkdir -p configs/c.1/ffs.uvc
      sudo mkdir -p functions/ffs.uvc
      sudo ln -s functions/ffs.uvc configs/c.1/

4. Prepare the FunctionFS mount point expected by the test-suite::

      sudo mkdir -p /dev/ffs/uvc
      sudo mount -t functionfs ffs-uvc /dev/ffs/uvc

   The user-space daemon (`tests/gadget_daemon.py`) will push the UVC descriptors
   to ``/dev/ffs/uvc/ep0`` when it starts.

5. Bind the gadget to the virtual controller::

      ls /sys/class/udc
      echo dummy_udc.0 | sudo tee UDC

   Replace ``dummy_udc.0`` with the actual name printed by ``ls`` if it differs.

Running the daemon and tests
----------------------------

1. In one terminal, launch the gadget daemon (requires write access to
   ``/dev/ffs/uvc``)::

      sudo python3 tests/gadget_daemon.py tests/data/sample_camera_profile.json \
           --mountpoint /dev/ffs/uvc --log-level INFO

   The daemon parses the JSON profile, writes the FunctionFS descriptors and
   responds to all control/streaming requests using the shared
   :class:`tests.uvc_emulator.UvcEmulatorLogic` implementation.

2. In another terminal, enable the integration tests and run them::

      export LIBUSB_UVC_ENABLE_GADGET_TESTS=1
      python3 -m pytest tests/test_integration.py

   The fixture waits for a device with the profile's VID/PID to appear via
   libusb, enumerates the synthetic controls, then captures an MJPEG frame.

Cleanup
-------

1. Stop the daemon (Ctrl+C) and unbind the gadget::

      echo "" | sudo tee /sys/kernel/config/usb_gadget/pyuvc/UDC

2. Unmount FunctionFS and remove the gadget directories::

      sudo umount /dev/ffs/uvc
      sudo rm -rf /sys/kernel/config/usb_gadget/pyuvc

3. Optionally unload the gadget modules::

      sudo modprobe -r dummy_hcd libcomposite

Troubleshooting
---------------

* If ``/dev/ffs/uvc`` fails to mount, ensure the ``functionfs`` kernel module is
  available (`sudo modprobe functionfs`).
* Running the daemon without root privileges typically results in ``Permission
  denied`` errors when accessing ``/dev/ffs/uvc``.
* The integration tests automatically skip when
  ``LIBUSB_UVC_ENABLE_GADGET_TESTS`` is unset or when the gadget fails to appear
  within 10 seconds.
