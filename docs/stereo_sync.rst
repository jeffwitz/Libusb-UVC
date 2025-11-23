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

* **Main consumer** – pairs frames, logs/plots results, and drives the OpenCV preview.
* **Left producer** – opens the left camera, negotiates PROBE, waits for barrier, commits, and captures frames.
* **Right producer** – identical to the left producer but targeting the other camera.

A **Split PROBE/COMMIT** strategy is used to ensure deterministic startup:

1.  **PROBE**: Both producers negotiate parameters (bandwidth, format) independently. This phase is variable in duration.
2.  **Barrier 1**: Wait for both to finish negotiating.
3.  **COMMIT**: Both producers send the "Start Streaming" command almost simultaneously.
4.  **Barrier 2**: Wait for both to confirm the stream is active.

This ensures that the "time zero" for both cameras is as close as possible, regardless of USB bus latency differences.

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

The consumer uses a **Target 0ms** strategy:

1.  **Deterministic Start**: Thanks to the PROBE/COMMIT split, we assume the cameras start simultaneously.
2.  **Zero Target**: The script aims for a host delta of 0ms.
3.  **Tolerance**: ``--max-ts-diff`` (seconds) defines the pairing window. Frames outside this window are dropped.

If one camera lags (e.g., due to a dropped packet), the delta will exceed the tolerance. The script will drop the older frame from the "leading" camera to allow the "lagging" camera to catch up, effectively realigning the streams to the nearest frame.

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

Restart-to-Sync (Brute Force)
-----------------------------

For applications requiring sub-millisecond precision, the script supports a **Restart-to-Sync** mode. Since the initial phase offset between two independent USB cameras is random, we can "roll the dice" until we get a lucky alignment.

*   ``--restart-threshold-ms``: Maximum allowed average offset (in milliseconds) during the startup phase.
*   ``--max-retries``: Number of times to restart the streams if the threshold is exceeded.

**How it works:**
1.  Streams start using the deterministic PROBE/COMMIT sequence.
2.  The consumer measures the average host delta over the first 10 frames.
3.  If ``abs(delta) > threshold``, both streams are stopped and restarted.
4.  This repeats until the delta is within tolerance or retries are exhausted.

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
       --restart-threshold-ms 5 \
       --print-deltas --display \
       --left-core 2 --right-core 3 \
       --left-start-delay-ms 0 --right-start-delay-ms 0

Key takeaways:

* Lowering FPS and using MJPEG reduces the USB bandwidth requirement and decoder
  workload.
* ``--restart-threshold-ms 5`` ensures that the script will retry until the cameras are aligned within 5ms.
* ``--print-deltas`` shows the raw host delta. It should stay close to 0ms.
* If you see a constant offset, check your USB topology (e.g., one camera on a hub, one direct).

Tuning Checklist
----------------

1. Start with ``--print-deltas`` to inspect the raw delta.
2. If the delta is consistently > 20ms, ensure you are using the new PROBE/COMMIT logic (it's automatic).
3. Trim ``--stream-queue`` and ``--queue-size`` if the preview feels laggy.
4. When PTS deltas diverge but host deltas remain stable, suspect firmware clock
   drift and fall back to host-only pairing.

Following this process should keep the pairing error within a few milliseconds
for identical cameras connected to different buses.
