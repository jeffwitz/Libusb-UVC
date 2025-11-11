# Libusb-UVC Stereo Capture Strategy

This guide documents the approach implemented by `examples/uvc_capture_stereo.py` to achieve
stable, low-latency synchronisation between two UVC cameras. It summarises the
threading model, timestamp handling, calibration knobs, and a recommended launch
command that has proven reliable on dual HDMI capture devices.

## 1. High-Level Architecture

The script runs three coordinated threads:

1. **Main consumer** – pairs frames, applies calibration, displays/logs output.
2. **Left producer** – opens the left camera, captures frames, pushes them into a
   bounded queue.
3. **Right producer** – identical to the left producer.

A barrier (`threading.Barrier(3)`) ensures both producers finish negotiating their
streams (`PROBE`/`COMMIT`) before any frame is consumed. Once the barrier releases,
the main thread flushes any early buffers and flips a `start_event`. Each producer
can optionally pause a little longer (`--left/right-start-delay-ms`) **after** the
barrier to compensate for hub / bus asymmetries.

## 2. Queueing & Drop Strategy

- Each producer writes into a small queue (`--queue-size`, default 3). When the
  queue is full the oldest frame is dropped so the most recent frame is always
  available to the consumer.
- On the consumer side you can choose between:
  - `--pairing-mode latest` (default): drain each queue on every iteration so only
    the freshest frame participates in pairing. This minimises visual lag.
  - `--pairing-mode fifo`: consume frames one-by-one if you need strict sequencing.
- Libusb/libuvc include their own internal queues, so reducing
  `--stream-queue` (e.g. `2`) further decreases end-to-end latency.

## 3. Timestamp Handling

Every `FramePacket` carries two clocks:

- `host_ts`: the monotonic time when the frame finished decoding (Python side).
- `pts`: the hardware timestamp exposed by the camera if the firmware reports it.

The script works in three layers:

1. **Calibration** (`--calibration-pairs N`): averages the first N host deltas to
   estimate the steady-state offset between the two buses. Once enough samples are
   collected the script locks on a target delta and recentres future measurements.
2. **Manual override** (`--target-delta-ms`): when you already know the preferred
   offset (e.g. `-36 ms`), provide it directly and set `--calibration-pairs 0` to
   skip auto-calibration.
3. **Pairing tolerance** (`--max-ts-diff`): after the delta is centred we still
   enforce a maximum deviation (in seconds). If a frame arrives outside this
   window it is discarded and the producer continues so the stream remains RT.

PTS values are logged whenever the firmware supplies them, but the pairing logic
mainly relies on the host delta because not all devices emit usable PTS.

## 4. CPU Affinity & Thread Coordination

- `--left-core` / `--right-core` pin each producer thread via `psutil.Process().cpu_affinity`.
- The main consumer keeps running on the default scheduler so the preview stays
  responsive.
- Because the producers are daemon threads, `Ctrl-C` or window close triggers a
  clean shutdown: the event loops are stopped, cameras are closed, and OpenCV
  windows destroyed.

## 5. Recommended Command

On the HP HDMI capture rig we obtained the most consistent results with:

```bash
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
```

Key points:

- Lowering FPS to 5 and using MJPEG reduces USB pressure and decoder cost.
- The 60 ms post-barrier delay on the right camera compensates for the observed
  bus priority on this system.
- `--print-deltas` shows both the raw host delta and the “centred” value (after
  calibration) so you can see how tight the pairing remains.

## 6. Tuning Checklist

1. Start with `--calibration-pairs 0 --print-deltas` to inspect the raw delta.
2. Decide whether to rely on auto-calibration or set `--target-delta-ms` manually.
3. Trim `--stream-queue` and `--queue-size` if the preview feels laggy.
4. Apply `--left/right-start-delay-ms` *after* you know which camera leads.
5. When PTS deltas diverge while host deltas are stable, suspect firmware clock
   drift; consider falling back to host-only pairing.

With these knobs you should be able to repeatably hold the pairing error within a
few milliseconds on identical UVC cameras connected to different buses.
