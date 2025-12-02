Stereo Capture Strategy
=======================

This document details two approaches implemented by :mod:`examples.uvc_capture_stereo`
and :mod:`examples.uvc_stereo_phase_sync` to obtain low-latency synchronisation,
measure dephasing, and optionally apply exposure-based phase sliding between two
UVC cameras.  It covers the threading model, queue handling, timestamp usage,
calibration workflows, and provides practical launch commands for dual HDMI
grabbers.

In theory, a **deterministic PROBE/COMMIT barrier** should be enough to align
two independent cameras: both negotiate bandwidth, wait on a barrier, then
start streaming at the same time.  In practice, tests on commodity webcams and
HDMI grabbers showed that this *alone* does **not** produce stable
sub-frame synchronisation. Hidden internal pipelines, buffering and clocking
behaviour inside the devices dominate the startup phase.

By contrast, the **exposure-based phase sliding** implemented in
``uvc_stereo_phase_sync.py`` has proven reliable on every rig tested so far:
once both streams are running, a short calibration window and a single
exposure “penalty” are enough to bring the average dephasing into a tight
window and keep it there.

.. warning::

   The barrier-based deterministic start in :mod:`examples.uvc_capture_stereo`
   is useful as an *inspection tool* (to visualise host / PTS deltas), but on
   real hardware it rarely achieves stable synchronisation by itself. For
   production stereo capture, prefer :mod:`examples.uvc_stereo_phase_sync`,
   which actively corrects dephasing by adjusting exposure.

.. contents::
   :local:
   :depth: 2

Architecture Overview (Barrier Variant)
---------------------------------------

The :mod:`examples.uvc_capture_stereo` helper relies on three coordinated threads:

* **Main consumer** – pairs frames, logs/plots results, and drives the OpenCV preview.
* **Left producer** – opens the left camera, negotiates PROBE, waits for barrier, commits, and captures frames.
* **Right producer** – identical to the left producer but targeting the other camera.

A **Split PROBE/COMMIT** strategy is used to *attempt* a deterministic startup:

1.  **PROBE**: Both producers negotiate parameters (bandwidth, format) independently. This phase is variable in duration.
2.  **Barrier 1**: Wait for both to finish negotiating.
3.  **COMMIT**: Both producers send the "Start Streaming" command almost simultaneously.
4.  **Barrier 2**: Wait for both to confirm the stream is active.

Conceptually this brings the "time zero" for both cameras as close as possible,
regardless of USB bus latency differences. In practice, internal buffering and
firmware behaviour often introduce a residual offset that the barrier alone
cannot eliminate; see the discussion below.

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

1.  **Deterministic Start (Attempt)**: Thanks to the PROBE/COMMIT split, both
    producers *try* to start streams at the same time. On some devices this
    produces a small initial offset, on others the residual dephasing can be
    tens of milliseconds.
2.  **Zero Target**: The script aims for a host delta of 0 ms when pairing
    frames.
3.  **Tolerance**: ``--max-ts-diff`` (seconds) defines the pairing window.
    Frames outside this window are dropped to keep pairs near the target.

If one camera lags (e.g., due to a dropped packet or variable internal
buffering), the delta will exceed the tolerance. The script will drop the
older frame from the "leading" camera to allow the "lagging" camera to catch
up, effectively realigning the streams to the nearest frame that fits within
the window.

PTS deltas are logged when present, but the pairing decision is driven by the
host delta because many firmwares omit valid PTS or report unstable hardware
timestamps.

Clock Model and Timestamp Semantics
-----------------------------------

When reasoning about stereo synchronisation it helps to distinguish three
separate clocks:

* the **sensor / device clock**, which timestamps frames inside the camera;
* the **USB bus clock**, which governs Start-of-Frame (SOF) / microframes;
* the **host monotonic clock**, which is what :func:`time.monotonic` reports.

The UVC specification exposes two optional timing fields in the video payload
header:

* **PTS (Presentation Time Stamp)** – a 32‑bit value in a device-defined unit
  that indicates when the frame should be presented on the *device* timeline.
  In UVC 1.5 this is typically expressed in 100 ns ticks and wraps after
  2³²−1 ticks.
* **SCR (Source Clock Reference)** – a structure that links the device clock to
  the USB SOF counter so that a host can, in principle, correlate the PTS
  timeline with the USB bus time.

In libusb-uvc:

* ``frame.pts`` exposes the raw PTS value when the camera sets the
  corresponding header bit. The library treats it as a monotonically
  increasing tick counter and, in stereo helpers, unwraps 32‑bit wraps into a
  continuous timeline.
* The **host timestamp** (``frame.timestamp`` or ``host_ts`` in the examples)
  records when the frame finished reassembly/decoding on the host. This is
  driven purely by :func:`time.monotonic` / :func:`time.perf_counter`.
* SCR is not currently decoded explicitly; instead, host timestamps provide the
  mapping onto the host clock.

Are these timestamps “the time of the frame”?

* **PTS** is the closest thing the UVC spec offers to a device-side “time of
  frame”: when implemented correctly it reflects the frame’s presentation time
  in the camera’s own clock domain, usually aligned with the exposure / sensor
  pipeline.
* **Host timestamps** are the time when the complete payload becomes available
  on the host. They are always *later* than the actual exposure time by:

  * USB transfer latency (microframes),
  * buffering inside the camera,
  * reassembly and optional decoding on the host.

  That offset is mostly constant plus some jitter, so host timestamps are very
  useful for *relative* pairing even if they do not represent sensor exposure
  directly.

On commodity devices the situation is complicated by firmware quality:

* Some cameras never set the PTS bit, or set a constant value, or reuse a frame
  counter instead of a proper clock. In that case PTS cannot be trusted.
* Even when PTS looks reasonable, the spec allows vendors to choose the tick
  granularity, so the absolute scale may be device-specific.

For these reasons libusb-uvc uses the following rule of thumb:

* **Use host timestamps for pairing and dephasing decisions**, because they
  always exist and are easy to interpret.
* When ``frame.pts`` is present and behaves monotonically, treat it as an
  additional diagnostic signal: it reveals how each camera’s internal clock
  behaves, and can help explain long-term drift that is invisible from a single
  host.

CPU Affinity and Coordination
-----------------------------

``--left-core`` and ``--right-core`` pin the producer threads via
``psutil.Process().cpu_affinity`` so each capture loop can run on a dedicated
CPU.  The consumer stays on the default scheduler, which keeps the UI
responsive.  Producers are daemon threads: ``Ctrl+C`` or window close events set
the shared ``stop_event``, join the streams, close the cameras, and destroy the
OpenCV window.

Restart-to-Sync (Brute Force, Experimental)
-------------------------------------------

To further reduce the *initial* offset, :mod:`examples.uvc_capture_stereo`
supports a **Restart-to-Sync** mode. Since the initial phase offset between two
independent USB cameras is random, we can "roll the dice" until we get a
startup where the average delta is small.

* ``--restart-threshold-ms`` – maximum allowed average offset (in milliseconds)
  during the startup phase.
* ``--max-retries`` – number of times to restart the streams if the threshold
  is exceeded.

**How it works:**

- Streams start using the deterministic PROBE/COMMIT sequence.
- The consumer measures the average host delta over a verdict window
  (``--verdict-pairs``), optionally preceded by a warm-up phase.
- If ``abs(delta) > threshold``, both streams are stopped and restarted.
- This repeats until the delta is within tolerance or retries are exhausted.

In our tests this technique helped to discard particularly bad startups, but
did **not** guarantee a small or stable offset over time. Once the internal
pipelines of the cameras have “warmed up”, they may drift or settle at an
offset that restart-to-sync cannot predict.

Recommended Command (Inspection Only)
-------------------------------------

On a dual HDMI capture rig the following command was used to *inspect* the
behaviour of the barrier-based start (5 FPS MJPEG, minimal latency):

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
       --left-commit-delay-ms 0 --right-commit-delay-ms 0

Key takeaways:

* Lowering FPS and using MJPEG reduces the USB bandwidth requirement and decoder
  workload.
* ``--restart-threshold-ms 5`` limits how bad the *starting* offset can be, but
  does not guarantee long-term stability; exposure sliding is still required on
  most devices.
* ``--left-commit-delay-ms`` / ``--right-commit-delay-ms`` can be used to
  intentionally skew the UVC COMMIT timing between the two cameras. This is
  useful when experimenting with hardware that appears to free-run: you can
  measure a stable initial ``Δhost`` offset, then re-launch with a matching
  delay on the leading side to see whether the firmware re-aligns its internal
  pipeline.
* ``--print-deltas`` shows the raw host delta. It often reveals a residual
  offset even when the barrier logic is in place.
* If you see a constant offset or large jitter, assume the device firmware is
  the limiting factor and rely on the exposure-based synchronisation described
  in the next section.

Tuning Checklist
----------------

1. Start with ``--print-deltas`` to inspect the raw delta.
2. If the delta is consistently large (tens of milliseconds), do not rely on
   the barrier logic alone; plan to use exposure-based phase sliding.
3. Trim ``--stream-queue`` and ``--queue-size`` if the preview feels laggy.
4. When PTS deltas diverge but host deltas remain stable, suspect firmware clock
   drift and fall back to host-only pairing.

Following this process makes it easier to characterise how a particular pair of
devices behaves before enabling the exposure calibration pipeline.

CSV Export for Offline Analysis
-------------------------------

The :mod:`examples.uvc_capture_stereo` helper can also export its pairing
measurements to a CSV file using ``--csv PATH``. This option is independent of
video recording (``--record-left`` / ``--record-right``) and only affects how
timing information is persisted.

Each row in the CSV corresponds to an *accepted* pair of frames (after any
``--max-ts-diff`` filtering) and contains:

* ``pair_index`` – monotonically increasing counter starting at 1.
* ``t_left_host_s`` / ``t_right_host_s`` – host timestamps (seconds) for the
  left/right frames, normalised so that the first accepted pair has ``t=0``.
* ``delta_host_ms`` – host-side pairing delta in milliseconds (left minus right).
* ``t_left_pts_s`` / ``t_right_pts_s`` – device PTS timestamps (seconds),
  when available, each normalised so that the first observed PTS on that side
  has ``t=0``. When a camera does not expose PTS, the corresponding field is
  left blank.
* ``delta_pts_ms`` – PTS-based delta in milliseconds, when both PTS values are
  present; otherwise empty.

This makes it straightforward to feed the stereo timing into external tools
(Pandas, NumPy, notebooks, etc.) while keeping both the host and device clock
views side by side.


Phase Sliding & Offset Measurement (uvc_stereo_phase_sync.py)
-------------------------------------------------------------

For deeper analysis of stereo rigs and to characterise hardware dephasing, the
repository ships :mod:`examples.uvc_stereo_phase_sync`.  This script focuses on
two goals:

* **Measuring the physical offset** between two independent USB cameras.
* **Optionally reducing the apparent jitter** by pairing frames based on their
  timestamps, at the cost of dropping some frames.

The calibration logic runs at startup only; once the exposure settings have
been adjusted, the pipeline returns to a stable, nominal configuration and
never touches exposure again.

Exposure Calibration Strategy
-----------------------------

At startup, the script:

1. Opens both cameras (by index or VID:PID + serial).
2. Disables auto exposure on each device (``Auto Exposure Mode`` set to Manual,
   and ``Exposure Auto Priority`` disabled when available).
3. Applies a **nominal exposure** to both sensors via ``--nominal-exposure-ms``.
4. Streams both cameras at the requested mode and collects
   ``--calibration-pairs`` frame pairs (for example 300 at 30 fps).
5. Computes the average host-side delta and standard deviation over this window,
   and logs them as::

      Calibration window: avg Δ=-13.881 ms std=0.467 ms over 300 pairs (tolerance=5.000 ms)

6. A **single exposure “phase sliding” sequence** is always applied based on
   the measured average offset, regardless of ``--tolerance-ms``. The tolerance
   is used purely as an informational bound when interpreting the calibration
   logs.
7. During this phase:

   * The **leading** camera (negative avg when left is ahead, positive when
     right is ahead) receives a long ``--penalty-exposure-ms`` (greater than
     the frame period, e.g. 40 ms at 30 fps).
   * The **lagging** camera is set to a slightly shorter “shadow” exposure so
     that both cameras can later be restored to the same nominal value.
   * After ``--penalty-frames`` pairs, both cameras are restored to the nominal
     exposure in a single "barrier" step.

Typical logs for this sequence look like::

   Calibration window: avg Δ=-13.881 ms std=0.467 ms over 300 pairs (tolerance=5.000 ms)
   Applying single penalty exposure on left (and shadow on other) to reduce startup offset (avg Δ=-13.881 ms)
   Calibration barrier: restored nominal exposure on both cameras (leader=left)
   Calibration sequence completed

Pairing Modes: Sync vs Measure
-------------------------------

After calibration, :mod:`uvc_stereo_phase_sync` provides three pairing
strategies that control how frames from the two cameras are coupled:

``--pairing-mode sync``
   Timestamp-aware pairing that drops frames when necessary to keep the host
   delta below ``--sync-window-ms``.  This reduces jitter but may lower the
   effective FPS.  The script reports:

   * Post-calibration statistics::

        Post-calibration statistics: avg Δ=8.642 ms std=3.386 ms over 136 pairs

   * Global run statistics::

        Run statistics: avg Δ=8.081 ms std=3.139 ms over 439 pairs

   * Sync drop statistics::

        Sync drop statistics: left=142/581 (25.04%) right=145/584 (24.83%) combined=293/1165 (24.64%)

   This mode is useful when synchronisation quality (low jitter) is more
   important than preserving every frame.

``--pairing-mode soft-sync``
   Hybrid strategy: during calibration it behaves like ``sync`` (timestamp-aware
   pairing with drops) so that the offset estimate is based on the closest
   frames. After calibration it switches to a gentler timestamp-aware mode that
   uses FIFO pairing plus a small buffer and occasionally drops a frame on the
   leading side when the phase error exceeds roughly half a frame period (based
   on ``--fps``). This preserves most frames while keeping the two streams
   close in phase and avoiding the more aggressive dropping of full ``sync``
   mode.

``--pairing-mode fifo``
   Pure FIFO pairing without additional drops beyond producer queue overflow.
   Frames are paired strictly in arrival order on each side; the script reports
   the *physical* dephasing of the two pipelines without attempting to minimise
   jitter at capture time.

Key Options
-----------

Device & stream selection:

* ``--device-id`` / ``--left-device-sn`` / ``--right-device-sn`` – select both
  cameras by VID:PID and USB serial numbers.
* ``--left-index`` / ``--right-index`` – alternative index-based selection.
* ``--interface`` – UVC streaming interface to claim (often 1).
* ``--width`` / ``--height`` / ``--fps`` / ``--codec`` – resolution, optional
  frame-rate *hint*, and codec. When ``--fps`` is left at ``0`` the device's
  advertised defaults are used and the helper estimates the effective FPS from
  the descriptors instead of trying to force a specific rate.
* ``--stream-queue`` / ``--queue-size`` – internal buffering in the producer
  and consumer.

Exposure calibration:

* ``--nominal-exposure-ms`` – target exposure used after calibration.
* ``--penalty-exposure-ms`` – long exposure applied once on the leading camera
  to slow it down (must exceed the frame period). When set to ``0`` the helper
  derives a suitable penalty from the advertised frame rate and
  ``--nominal-exposure-ms``.
* ``--exposure-unit-us`` – conversion granularity (default 100 µs per unit,
  the UVC default).
* ``--tolerance-ms`` – informational threshold used when interpreting the
  calibration statistics (the sliding step is always applied; a large average
  beyond this bound indicates a significant hardware offset).
* ``--calibration-pairs`` – number of paired frames used to estimate the
  startup offset.
* ``--penalty-frames`` – number of pairs to skip while the penalty exposure is
  active before restoring the nominal exposure. When set to ``0`` the helper
  derives a penalty window from the observed offset and the effective frame
  rate.

Pairing strategy:

* ``--pairing-mode {sync,soft-sync,fifo}`` – choose between jitter-minimising
  pairing with frame drops (``sync``), phase-aware FIFO with occasional drops
  (``soft-sync``), or pure FIFO measurement (``fifo``).
* ``--sync-window-ms`` – maximum host delta accepted in ``sync`` mode (also
  used during the calibration phase of ``soft-sync``).

Run control & logging:

* ``--duration`` – total run time in seconds (0 for infinite).
* ``--print-deltas`` – dump per-pair deltas during the run.
* ``--log-level`` – verbosity (INFO is often sufficient).

Recommended Commands
--------------------

To **minimise jitter** by pairing frames as closely as possible in time (with
some frame drops), a typical command is::

   python3 examples/uvc_stereo_phase_sync.py \
       --device-id 32e4:9415 \
       --left-device-sn 7048100c3c214ef3 \
       --right-device-sn 406c101e3c214ef3 \
       --interface 1 \
       --width 1920 --height 1080 --fps 30 \
       --codec mjpeg \
       --nominal-exposure-ms 10.0 --penalty-exposure-ms 40.0 \
       --tolerance-ms 5.0 --calibration-pairs 300 --penalty-frames 3 \
       --exposure-unit-us 100 \
       --sync-window-ms 15.0 --pairing-mode sync \
       --duration 20 --log-level INFO

To **measure the hardware dephasing** while preserving frames (and let a
downstream algorithm handle the offset), use::

   python3 examples/uvc_stereo_phase_sync.py \
       --device-id 32e4:9415 \
       --left-device-sn 7048100c3c214ef3 \
       --right-device-sn 406c101e3c214ef3 \
       --interface 1 \
       --width 1920 --height 1080 --fps 30 \
       --codec mjpeg \
       --nominal-exposure-ms 10.0 --penalty-exposure-ms 40.0 \
       --tolerance-ms 5.0 --calibration-pairs 300 --penalty-frames 3 \
       --exposure-unit-us 100 \
       --sync-window-ms 15.0 --pairing-mode measure \
       --duration 20 --log-level INFO

In both cases the script will log the average delta and standard deviation
before and after calibration, as well as the proportion of frames dropped by
the synchronisation logic.  This makes it straightforward to evaluate the trade
off between preserving frames and minimising jitter for a specific stereo rig.

2025 Firmware-Nudge Workflow
----------------------------

Recent work on MS2130-based HDMI grabbers showed that exposure “penalty” pulses
are unnecessary once a closed-loop “firmware nudge” controller is in place. The
current version of ``uvc_stereo_phase_sync`` therefore relies on a lightweight
per-camera ramp that automatically finds the minimum nudge required to flip the
phase and then freezes it.

How the controller behaves
~~~~~~~~~~~~~~~~~~~~~~~~~~

* Both cameras start in manual exposure mode at the nominal value.
* The helper waits for ``--calibration-pairs`` host timestamp pairs. While the
  calibration runs:

  1. **Modulo guard** – if ``|Δ|`` grows beyond one frame period (derived from
     ``--fps``), the script discards a single frame from the leader so the
     queues never drift by more than one frame in either direction.
  2. **Per-camera ramp** – each camera has its own scale (initially
     ``--nudge-ramp-start``, default 0.02). As long as the same camera keeps
     leading, its scale increases by ``--nudge-ramp-step`` up to
     ``--nudge-ramp-max``. The scale multiplies both ``--nudge-iterations`` and
     ``--nudge-gain`` so a small number such as 0.08 still produces a visible
     delay without overwhelming the firmware.
  3. **Lock & decay** – once ``|Δ|`` flips sign or drops below
     ``--nudge-ramp-lock`` (default 2 ms), the current leader is “locked”:
     its scale is reset to the start value and will not increase again until
     the other camera forces a sign change. The opposite side is “unlocked” and
     its scale decays by ``--nudge-ramp-decay`` (default 0.5) so the next nudge
     starts weaker.

* After calibration the nudges stop completely. The script never touches
  exposure again; it simply reports the hardware offset while pairing frames
  FIFO.

Default CLI (no tuning required)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When the defaults are left untouched, the helper already uses a conservative,
symmetrical configuration that works well on MS2130 grabbers:

.. code-block:: bash

   python3 examples/uvc_stereo_phase_sync.py \
       --device-id 32e4:9415 \
       --left-device-sn 3054481e58586223 \
       --right-device-sn 10882a0cb12d5283 \
       --interface 1 \
       --width 1920 --height 1080 \
       --fps 30 \
       --codec mjpeg \
       --nominal-exposure-ms 2.0 \
       --tolerance-ms 4.0 \
       --calibration-pairs 100 \
       --print-deltas \
       --log-level INFO

Only override the nudge parameters when this baseline does not converge:

* ``--nudge-iterations`` / ``--nudge-step-units`` – increase slightly if the
  cameras never flip sign, decrease if you notice large overshoots.
* ``--nudge-ramp-step`` / ``--nudge-ramp-max`` – reduce for gentler ramps, or
  bump them if the controller is too slow to react.
* ``--nudge-ramp-lock`` – tighten when you want the ramp to freeze as soon as
  Δ is within a narrow tolerance; loosen when your sensors need more headroom.

In typical runs the controller only sends a few dozen ``SET_CUR`` bursts per
camera during the first second of capture, after which both streams free-run
with the nominal exposure. This makes the workflow far more repeatable than the
older “penalty exposure” approach while keeping the CLI simple (device
selection + optional ``--print-deltas``).
