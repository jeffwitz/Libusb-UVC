# Architecture Overview

This document summarises the structure of the `libusb_uvc` package after the
recent refactor that moved the implementation out of `__init__.py`.

## Public Facade

- `__init__.py`  
  Re-exports the symbols listed in `core.__all__` so that existing
  `from libusb_uvc import …` imports keep working.  The module also exposes the
  implementation namespace via `libusb_uvc.core` for advanced use and testing.

## Implementation Modules

- `core.py`  
  Central implementation.  Contains:
  - data structures describing descriptors (`FrameInfo`, `StreamFormat`, …);
  - the `UVCCamera` high-level API, including stream negotiation, still-image
    helpers, and control management (`UVCControlsManager`);
  - low-level helpers for descriptor parsing, control transfers, and frame
    conversions;
  - facades for the asynchronous streaming backend and decoder layer.

- `decoders.py`  
  Provides the optional decoding backends through a common interface:
  - discovery helpers (`create_decoder_backend`, `DEFAULT_BACKEND_ORDER`);
  - concrete implementations for PyAV and GStreamer (`_PyAVDecoder`,
    `_GStreamerDecoder`).  A lightweight `_H264Normalizer` normalises frame-based
  payloads to Annex B, caches SPS/PPS, and replays them ahead of IDR frames so
  backends receive a complete bitstream even when the firmware omits the
  configuration NAL units.
  The module is imported lazily by `core.py` so that the heavy dependencies
  (PyAV, PyGObject/GStreamer) remain optional.

- `uvc_async.py`  
  Thin wrapper around libusb’s asynchronous transfers.  It creates and recycles
  `IsoTransfer` objects, exposes callback hooks, and feeds the frame assembly
  logic in `core.py`.  Imported on demand to avoid pulling libusb1 when only
  parsing descriptors.

- `quirks/`  
  Directory of JSON descriptors keyed by GUID.  Each file follows the internal
  schema consumed by `core.load_quirks()` to enrich control metadata (for
  example, the Microsoft Camera Control XU).

## Tests and Tooling

- `tests/test_controls.py` and `tests/test_decoder_preference.py` exercise the
  refactored modules.  They import `libusb_uvc` but monkey-patch through
  `libusb_uvc.core` where needed.
- `tests/uvc_emulator.py` implements a reusable logic layer for emulating UVC
  devices, enabling CI coverage without physical hardware.

## Future Work

- Splitting `core.py` further into submodules (e.g. `controls.py`,
  `stream.py`, `still.py`) would simplify maintenance and reduce import time.
- The quirk schema should be formalised (JSON Schema) and validated during
  packaging.
