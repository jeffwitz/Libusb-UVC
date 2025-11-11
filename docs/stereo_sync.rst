Stereo Capture Strategy
=======================

This document details the approach implemented by :mod:`examples.uvc_capture_stereo`
to obtain low-latency synchronisation between two UVC cameras.  It covers the
threading model, queue handling, timestamp usage, calibration workflow, and
provides a battle-tested launch command for dual HDMI grabbers.

.. contents::
   :local:
   :depth: 2

Architecture Overview
---------------------

The script relies on three coordinated threads:

* **Main consumer** – pairs frames, applies calibration, logs/plots results, and
  drives the OpenCV preview.
* **Left producer** – opens the left camera, negotiates PROBE/COMMIT, captures
  frames, and pushes them into a bounded queue.
* **Right producer** – identical to the left producer but targeting the other
  camera.

A ``threading.Barrier`` (size 3) ensures that both producers have completed the
USB negotiation before any frames are consumed.  Once the barrier releases, the
main thread flushes stray buffers, sets a ``start_event`` semaphore, and each
producer can optionally sleep for ``--*-start-delay-ms`` to compensate for hub or
bus asymmetries before entering the capture loop.

Queueing and Drop Policy
------------------------

* Each producer writes into a small queue (``--queue-size``; default 3).  On
  overflow, the oldest frame is dropped so the most recent frame is always
  available to the consumer.
* ``--pairing-mode latest`` (default) drains each queue every iteration so the
  consumer always pairs the freshest frame, minimising display lag.
* ``--pairing-mode fifo`` consumes frames one-by-one when strict sequencing
  matters more than absolute freshness.
* Libusb/libuvc have their own internal buffers.  Lowering ``--stream-queue`` to
  2 (or even 1 when the firmware allows it) reduces the total latency.

Timestamp Handling
------------------

Every queued frame carries two timestamps:

``host_ts``
    ``time.monotonic()`` when the frame finished decoding on the host.

``pts``
    Hardware timestamp provided by the camera firmware, when available.  Not all
    devices expose this field.

The consumer works in three stages:

1. **Calibration** – ``--calibration-pairs N`` averages the first *N* host deltas
   to estimate the steady-state offset between cameras.  Once collected, the
   script locks on the derived target and recentres future deltas around it.
2. **Manual override** – ``--target-delta-ms`` can be set when the expected
   offset is already known (for example ``-36``).  Set
   ``--calibration-pairs 0`` to skip auto-calibration.
3. **Tolerance** – ``--max-ts-diff`` (seconds) defines the pairing window after
   recentering.  Frames outside this window are dropped so the capture remains
   real-time.

PTS deltas are logged when present, but the pairing decision is driven by the
host delta because many firmwares omit valid PTS.

CPU Affinity and Coordination
-----------------------------

``--left-core`` and ``--right-core`` pin the producer threads via
``psutil.Process().cpu_affinity`` so each capture loop can run on a dedicated
CPU.  The consumer stays on the default scheduler, which keeps the UI
responsive.  Producers are daemon threads: ``Ctrl+C`` or window close events set
the shared ``stop_event``, join the streams, close the cameras, and destroy the
OpenCV window.

Recommended Command
-------------------

On a dual HDMI capture rig the following command delivered the most stable
results (5 FPS MJPEG, minimal latency):

.. code-block:: bash

   python3 examples/uvc_capture_stereo.py \
       --device-id 32e4:9415 \
       --left-device-sn 406c101e3c214ef3 \
       --right-device-sn 3054481e58586223 \
       --interface 1 \
       --width 1920 --height 1080 --fps 5 \
       --codec mjpeg --decoder pyav \
       --max-ts-diff 0.050 \
       --pairing-mode latest \
       --calibration-pairs 30 \
       --print-deltas --display \
       --left-core 2 --right-core 3 \
       --left-start-delay-ms 0 --right-start-delay-ms 60

Key takeaways:

* Lowering FPS and using MJPEG reduces the USB bandwidth requirement and decoder
  workload.
* The 60 ms post-barrier delay pushes the slower bus to “catch up” on this
  hardware.
* ``--print-deltas`` shows both the raw host delta and the centred value (after
  calibration) so you can monitor drift in real time.

Tuning Checklist
----------------

1. Start with ``--calibration-pairs 0 --print-deltas`` to inspect the raw delta.
2. Decide whether to rely on auto-calibration or set ``--target-delta-ms``.
3. Trim ``--stream-queue`` and ``--queue-size`` if the preview feels laggy.
4. Adjust ``--left/right-start-delay-ms`` after you know which camera leads.
5. When PTS deltas diverge but host deltas remain stable, suspect firmware clock
   drift and fall back to host-only pairing.

Following this process should keep the pairing error within a few milliseconds
for identical cameras connected to different buses.
