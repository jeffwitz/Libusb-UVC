"""Lightweight PyUSB helpers for working with UVC cameras.

The goal of this module is to provide a thin, well-documented layer on top of
PyUSB that understands the UVC descriptor layout and the standard probing
protocol.  It is intentionally minimal so that example scripts can reuse the
parsing and streaming logic without pulling in the full libuvc bindings.
"""

from __future__ import annotations

import contextlib
import ctypes
import dataclasses
import errno
import json
import logging
import os
import pathlib
import queue
import threading
import time
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple, Union

import usb.core
import usb.util
import usb1

try:  # Optional dependency for MJPEG preview
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst, GLib

    GST_AVAILABLE = True
except (ImportError, ValueError):
    GST_AVAILABLE = False


LOG = logging.getLogger(__name__)


__all__: List[str]
__version__ = "0.1.0"

_AUTO_DETACH_VC = os.environ.get("LIBUSB_UVC_AUTO_DETACH_VC", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

if not _AUTO_DETACH_VC:
    LOG.debug("Auto-detach of VC interfaces disabled via LIBUSB_UVC_AUTO_DETACH_VC")


_LIBUSB_HOTPLUG_DISABLED = False
_LIBUSB_HOTPLUG_ATTEMPTED = False


def _auto_detach_vc_enabled() -> bool:
    return _AUTO_DETACH_VC


def load_quirks() -> Dict[str, dict]:
    """Load per-GUID control definitions from the packaged quirks directory."""

    base_dir = pathlib.Path(__file__).resolve().parent
    candidate_dirs = [
        base_dir / "quirks",
        base_dir / "src" / "libusb_uvc" / "quirks",
    ]
    try:  # pragma: no cover - importlib availability
        import importlib.util

        spec = importlib.util.find_spec("libusb_uvc")
        if spec and spec.submodule_search_locations:
            candidate_dirs.append(pathlib.Path(spec.submodule_search_locations[0]) / "quirks")
    except Exception:
        pass

    quirks_dir = None
    for candidate in candidate_dirs:
        if candidate.is_dir():
            quirks_dir = candidate
            break

    quirks: Dict[str, dict] = {}
    if quirks_dir is None:
        return quirks

    for json_path in sorted(quirks_dir.glob("*.json")):
        try:
            with json_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("Failed to load quirks file %s: %s", json_path, exc)
            continue

        guid = str(data.get("guid", "")).lower()
        if not guid:
            LOG.debug("Skipping quirks file %s without GUID", json_path)
            continue

        quirks[guid] = data

    return quirks


def _disable_hotplug_and_get_backend():
    """Try to reinitialise libusb without the udev hotplug monitor.

    Some sandboxes block access to udev, causing ``libusb_init`` to return
    ``LIBUSB_ERROR_OTHER``.  In that situation we ask libusb to skip device
    discovery so that PyUSB can still enumerate already-present devices.
    ``usb.core.find`` raises :class:`usb.core.NoBackendError` when this happens.
    """

    global _LIBUSB_HOTPLUG_ATTEMPTED, _LIBUSB_HOTPLUG_DISABLED
    if _LIBUSB_HOTPLUG_DISABLED or _LIBUSB_HOTPLUG_ATTEMPTED:
        from usb.backend import libusb1  # lazy import to avoid circular refs

        backend = libusb1.get_backend()
        return backend

    _LIBUSB_HOTPLUG_ATTEMPTED = True

    try:
        libusb = ctypes.CDLL("libusb-1.0.so.0")
    except OSError:
        return None

    set_option = getattr(libusb, "libusb_set_option", None)
    if set_option is None:
        return None

    try:
        set_option.argtypes = [ctypes.c_void_p, ctypes.c_int]
        set_option.restype = ctypes.c_int
    except AttributeError:
        return None

    # LIBUSB_OPTION_NO_DEVICE_DISCOVERY.  Passing NULL targets the default
    # context before its first initialisation attempt.
    if set_option(None, 2) != 0:
        return None

    from usb.backend import libusb1

    # Reset internal module state so the next get_backend() call retries the
    # initialisation path.  These attributes are considered private but this is
    # the only reliable way to request a fresh backend in PyUSB today.
    libusb1._lib = None  # type: ignore[attr-defined]
    libusb1._lib_object = None  # type: ignore[attr-defined]

    backend = libusb1.get_backend()
    if backend is not None:
        _LIBUSB_HOTPLUG_DISABLED = True
    return backend


class CodecPreference(str):
    """Simple codec discriminator used when selecting a stream format."""

    AUTO = "auto"
    YUYV = "yuyv"
    MJPEG = "mjpeg"

# --- UVC Constants ---
UVC_CLASS = 0x0E
VC_SUBCLASS = 0x01
VS_SUBCLASS = 0x02
CS_INTERFACE = 0x24

# Video Control (VC) descriptor subtypes
VC_HEADER = 0x01
VC_INPUT_TERMINAL = 0x02
VC_OUTPUT_TERMINAL = 0x03
VC_SELECTOR_UNIT = 0x04
VC_PROCESSING_UNIT = 0x05
VC_EXTENSION_UNIT = 0x06

# Video Streaming (VS) descriptor subtypes
VS_INPUT_HEADER = 0x01
VS_FORMAT_UNCOMPRESSED = 0x04
VS_FRAME_UNCOMPRESSED = 0x05
VS_FORMAT_MJPEG = 0x06
VS_FRAME_MJPEG = 0x07
VS_FORMAT_FRAME_BASED = 0x10
VS_FRAME_FRAME_BASED = 0x11

# Still image format/frame descriptor subtypes
VS_FORMAT_UNCOMPRESSED_STILL = 0x30
VS_FRAME_UNCOMPRESSED_STILL = 0x31
VS_FORMAT_MJPEG_STILL = 0x32
VS_FRAME_MJPEG_STILL = 0x33
VS_STILL_IMAGE_FRAME_DESCRIPTOR = 0x03

# UVC Payload Header constants
BH_FID = 0x01
BH_EOF = 0x02
BH_PTS = 0x04
BH_SCR = 0x08
BH_RES = 0x10
BH_STI = 0x20
BH_ERR = 0x40
BH_EOH = 0x80

# UVC Request Codes for control transfers
SET_CUR = 0x01
GET_CUR = 0x81
GET_MIN = 0x82
GET_MAX = 0x83
GET_RES = 0x84
GET_LEN = 0x85
GET_INFO = 0x86
GET_DEF = 0x87

# VideoStreaming control selectors
VS_PROBE_CONTROL = 0x01
VS_COMMIT_CONTROL = 0x02
VS_STILL_PROBE_CONTROL = 0x03
VS_STILL_COMMIT_CONTROL = 0x04
VS_STILL_IMAGE_TRIGGER_CONTROL = 0x05

# Standard UVC Control Selectors
# (Incomplete list, add more as needed)
# Camera Terminal (CT) Controls
CT_EXPOSURE_TIME_ABSOLUTE_CONTROL = 0x04
CT_ZOOM_ABSOLUTE_CONTROL = 0x0B

# Processing Unit (PU) Controls
PU_BRIGHTNESS_CONTROL = 0x02
PU_CONTRAST_CONTROL = 0x03
PU_GAIN_CONTROL = 0x04
PU_WHITE_BALANCE_TEMPERATURE_CONTROL = 0x0A
PU_WHITE_BALANCE_TEMPERATURE_AUTO_CONTROL = 0x0B

UVC_CONTROL_MAPPING = {
    "Camera Terminal": {
        2: "Auto Exposure Mode",
        3: "Auto Exposure Priority",
        4: "Exposure Time, Absolute",
        11: "Zoom, Absolute",
    },
    "Processing Unit": {
        1: "Backlight Compensation",
        2: "Brightness",
        3: "Contrast",
        4: "Gain",
        5: "Power Line Frequency",
        6: "Hue",
        7: "Saturation",
        8: "Sharpness",
        9: "Gamma",
        10: "White Balance Temperature",
        11: "White Balance Temperature, Auto",
    }
}


# Pre-computed request types used for control transfers on interfaces
REQ_TYPE_IN = usb.util.build_request_type(
    usb.util.CTRL_IN, usb.util.CTRL_TYPE_CLASS, usb.util.CTRL_RECIPIENT_INTERFACE
)
REQ_TYPE_OUT = usb.util.build_request_type(
    usb.util.CTRL_OUT, usb.util.CTRL_TYPE_CLASS, usb.util.CTRL_RECIPIENT_INTERFACE
)


class UVCError(RuntimeError):
    """Raised when the camera reports unexpected errors."""

# --- Data Structures for Descriptors ---

@dataclasses.dataclass
class FrameInfo:
    """Frame descriptor summary collected from a VS frame descriptor."""

    frame_index: int
    width: int
    height: int
    default_interval: int
    intervals_100ns: List[int]
    max_frame_size: int
    bm_capabilities: int = 0

    def intervals_hz(self) -> List[float]:
        unique = sorted({v for v in self.intervals_100ns if v})
        return [_interval_to_hz(v) for v in unique]

    @property
    def intervals(self) -> List[float]:
        """Backward compatibility alias returning frame intervals in Hz."""

        return self.intervals_hz()

    @property
    def supports_still(self) -> bool:
        """Return True when the frame advertises still-image support."""

        return bool(self.bm_capabilities & 0x01)


@dataclasses.dataclass
class StillFrameInfo:
    """Still image frame descriptor (Method 2)."""

    width: int
    height: int
    endpoint_address: int
    compression_indices: List[int] = dataclasses.field(default_factory=list)
    format_subtype: Optional[int] = None
    def pick_interval(
        self, target_fps: Optional[float], *, strict: bool = False, tolerance_hz: float = 1e-3
    ) -> int:
        if not self.intervals_100ns:
            return self.default_interval
        if target_fps is None or target_fps <= 0:
            return self.default_interval or self.intervals_100ns[0]
        target_interval = int(round(1e7 / target_fps))
        best = min(self.intervals_100ns, key=lambda value: abs(value - target_interval))
        if strict:
            actual_fps = _interval_to_hz(best)
            if abs(actual_fps - target_fps) > tolerance_hz:
                raise ValueError(
                    f"No advertised frame interval matches {target_fps} fps (closest {actual_fps:.6f} fps)"
                )
        return best


@dataclasses.dataclass
class StreamFormat:
    """A Video Streaming format along with its advertised frames."""
    description: str
    format_index: int
    subtype: int
    guid: bytes
    frames: List[FrameInfo] = dataclasses.field(default_factory=list)
    still_frames: List["StillFrameInfo"] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class AltSettingInfo:
    """Information about an alternate streaming interface setting."""
    alternate_setting: int
    endpoint_address: Optional[int]
    endpoint_attributes: Optional[int]
    max_packet_size: int

    def is_isochronous(self) -> bool:
        if self.endpoint_attributes is None:
            return False
        return usb.util.endpoint_type(self.endpoint_attributes) == usb.util.ENDPOINT_TYPE_ISO


@dataclasses.dataclass
class StreamingInterface:
    """Grouping of the per-interface formats and alternate settings."""
    interface_number: int
    formats: List[StreamFormat] = dataclasses.field(default_factory=list)
    alt_settings: List[AltSettingInfo] = dataclasses.field(default_factory=list)

    def get_alt(self, alternate_setting: int) -> Optional[AltSettingInfo]:
        for alt in self.alt_settings:
            if alt.alternate_setting == alternate_setting:
                return alt
        return None

    def select_alt_for_payload(self, required_payload: int) -> Optional[AltSettingInfo]:
        candidates = [alt for alt in self.alt_settings if alt.max_packet_size]
        if not candidates:
            return None
        for alt in sorted(candidates, key=lambda a: a.max_packet_size):
            if alt.max_packet_size >= required_payload:
                return alt
        return max(candidates, key=lambda a: a.max_packet_size)

    def find_frame(
        self, width: int, height: int, *, format_index: Optional[int] = None, subtype: Optional[int] = None
    ) -> Optional[Tuple[StreamFormat, FrameInfo]]:
        for fmt in self.formats:
            if format_index is not None and fmt.format_index != format_index:
                continue
            if subtype is not None and fmt.subtype != subtype:
                continue
            for frame in fmt.frames:
                if frame.width == width and frame.height == height:
                    return fmt, frame
        return None

    def iter_still_frames(self) -> Iterator[Tuple[StreamFormat, FrameInfo]]:
        for fmt in self.formats:
            for frame in fmt.frames:
                if frame.supports_still:
                    yield fmt, frame

    def find_still_frame(
        self,
        width: int,
        height: int,
        *,
        format_index: Optional[int] = None,
        subtype: Optional[int] = None,
    ) -> Optional[Tuple[StreamFormat, FrameInfo]]:
        for fmt, frame in self.iter_still_frames():
            if format_index is not None and fmt.format_index != format_index:
                continue
            if subtype is not None and fmt.subtype != subtype:
                continue
            if frame.width == width and frame.height == height:
                return fmt, frame
        return None

@dataclasses.dataclass
class UVCControl:
    """Represents a discovered UVC control."""
    unit_id: int
    selector: int
    name: str
    type: str

@dataclasses.dataclass
class UVCUnit:
    """Represents a Unit in the Video Control topology."""
    unit_id: int
    type: str
    # --- CORRECTION: a default value here caused the TypeError in the child class ---
    controls: List[UVCControl]

@dataclasses.dataclass
class ExtensionUnit(UVCUnit):
    """Represents an Extension Unit with its specific GUID."""
    guid: str

@dataclasses.dataclass
class CapturedFrame:
    """Container returned by :meth:`UVCCamera.read_frame` and asynchronous streams."""

    payload: bytes
    format: StreamFormat
    frame: FrameInfo
    fid: int
    pts: Optional[int]
    timestamp: float = dataclasses.field(default_factory=time.time)
    sequence: int = 0

    _rgb_cache: Optional[object] = dataclasses.field(default=None, init=False, repr=False)

    def to_bytes(self) -> bytes:
        return bytes(self.payload)

    def to_rgb(self):
        if self._rgb_cache is None:
            self._rgb_cache = decode_to_rgb(self.payload, self.format, self.frame)
        return self._rgb_cache

    def to_bgr(self):
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError("OpenCV is required for BGR conversion") from exc
        rgb = self.to_rgb()
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


@dataclasses.dataclass
class ControlEntry:
    """Rich metadata describing a validated UVC control."""

    interface_number: int
    unit_id: int
    selector: int
    name: str
    type: str
    info: int
    minimum: Optional[int]
    maximum: Optional[int]
    step: Optional[int]
    default: Optional[int]
    length: Optional[int]
    raw_minimum: Optional[bytes] = None
    raw_maximum: Optional[bytes] = None
    raw_step: Optional[bytes] = None
    raw_default: Optional[bytes] = None
    metadata: Dict[str, object] = dataclasses.field(default_factory=dict)

    def is_writable(self) -> bool:
        return bool(self.info & 0x02)

    def is_readable(self) -> bool:
        return bool(self.info & 0x01)


def find_uvc_devices(vid: Optional[int] = None, pid: Optional[int] = None) -> List[usb.core.Device]:
    """Return every USB device that looks like a UVC camera."""
    try:
        devices = usb.core.find(find_all=True)
    except usb.core.NoBackendError:
        backend = _disable_hotplug_and_get_backend()
        if backend is None:
            raise
        devices = usb.core.find(find_all=True, backend=backend)
    if devices is None:
        return []

    result = []
    for dev in devices:
        if vid is not None and dev.idVendor != vid:
            continue
        if pid is not None and dev.idProduct != pid:
            continue
        if any(intf.bInterfaceClass == UVC_CLASS for cfg in dev for intf in cfg):
            result.append(dev)
    return result


def iter_video_streaming_interfaces(dev: usb.core.Device) -> Iterator[usb.core.Interface]:
    """Yield every interface whose class/subclass matches UVC streaming."""
    for cfg in dev:
        for intf in cfg:
            if intf.bInterfaceClass == UVC_CLASS and intf.bInterfaceSubClass == VS_SUBCLASS:
                yield intf


def list_control_units(dev: usb.core.Device) -> Dict[int, List[UVCUnit]]:
    """Build UVCUnit descriptions for all Video Control interfaces on dev.

    We try to detach the VC interface from the kernel driver (uvcvideo) so that
    user-space control transfers work even when the module is loaded.
    """
    unit_map: Dict[int, List[UVCUnit]] = {}
    for cfg in dev:
        for intf in cfg:
            if intf.bInterfaceClass == UVC_CLASS and intf.bInterfaceSubClass == VC_SUBCLASS:
                reattach = False
                should_detach = _auto_detach_vc_enabled()
                if should_detach:
                    try:
                        if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                            dev.detach_kernel_driver(intf.bInterfaceNumber)
                            LOG.info(
                                "Detached kernel driver from VC interface %s",
                                intf.bInterfaceNumber,
                            )
                            reattach = True
                    except (usb.core.USBError, NotImplementedError, AttributeError):
                        reattach = False
                else:
                    LOG.debug(
                        "Auto-detach disabled; reading VC interface %s without detaching kernel driver",
                        intf.bInterfaceNumber,
                    )
                try:
                    if intf.bAlternateSetting == 0 and intf.extra_descriptors:
                        units = parse_vc_descriptors(bytes(intf.extra_descriptors))
                        if units:
                            unit_map[intf.bInterfaceNumber] = units
                finally:
                    if reattach:
                        with contextlib.suppress(usb.core.USBError):
                            dev.attach_kernel_driver(intf.bInterfaceNumber)
    return unit_map
def parse_vc_descriptors(extra: bytes) -> List[UVCUnit]:
    """Parse the raw `extra_descriptors` blob for a VC interface."""
    units: List[UVCUnit] = []
    idx = 0
    while idx + 2 < len(extra):
        length = extra[idx]
        if length == 0 or idx + length > len(extra):
            break
        dtype = extra[idx + 1]
        subtype = extra[idx + 2]
        payload = extra[idx : idx + length]

        if dtype == CS_INTERFACE:
            unit = None
            if subtype == VC_INPUT_TERMINAL:
                unit = _parse_input_terminal(payload)
            elif subtype == VC_PROCESSING_UNIT:
                unit = _parse_processing_unit(payload)
            elif subtype == VC_EXTENSION_UNIT:
                unit = _parse_extension_unit(payload)

            if unit:
                units.append(unit)
        idx += length
    return units

def _parse_input_terminal(desc: bytes) -> Optional[UVCUnit]:
    if len(desc) < 8:
        return None
    unit_id = desc[3]
    controls = []
    if len(desc) >= 18:
        bitmap = int.from_bytes(desc[15:18], "little")
        control_map = UVC_CONTROL_MAPPING.get("Camera Terminal", {})
        for i in range(24):
            if (bitmap >> i) & 1:
                selector = i + 1
                control_name = control_map.get(selector, f"Unknown Control Selector {selector}")
                controls.append(
                    UVCControl(
                        unit_id=unit_id,
                        selector=selector,
                        name=control_name,
                        type="Camera Terminal",
                    )
                )
    return UVCUnit(unit_id=unit_id, type="Input Terminal", controls=controls)

def _parse_processing_unit(desc: bytes) -> Optional[UVCUnit]:
    """
    Parse Processing Unit descriptor robustly:
    - read bControlSize at offset 7
    - read bmControls starting at offset 8 for bControlSize bytes
    """
    if len(desc) < 10:
        return None
    unit_id = desc[3]
    controls: List[UVCControl] = []
    # Processing Unit: bControlSize @ offset 7, bmControls start @8
    bControlSize = desc[7] if len(desc) > 7 else 0
    ctrl_start = 8
    ctrl_end = ctrl_start + bControlSize
    if bControlSize > 0 and ctrl_end <= len(desc):
        bitmap_bytes = desc[ctrl_start:ctrl_end]
        bitmap = int.from_bytes(bitmap_bytes, "little")
        control_map = UVC_CONTROL_MAPPING.get("Processing Unit", {})
        max_bits = 8 * bControlSize
        for i in range(max_bits):
            if (bitmap >> i) & 1:
                selector = i + 1
                control_name = control_map.get(selector, f"Unknown Control Selector {selector}")
                controls.append(
                    UVCControl(
                        unit_id=unit_id,
                        selector=selector,
                        name=control_name,
                        type="Processing Unit",
                    )
                )
    return UVCUnit(unit_id=unit_id, type="Processing Unit", controls=controls)


def _parse_extension_unit(desc: bytes) -> Optional[ExtensionUnit]:
    """Parse an Extension Unit descriptor to find its GUID."""
    if len(desc) < 24:
        return None
    unit_id = desc[3]
    guid_bytes = desc[4:20]
    guid_str = (
        f"{guid_bytes[3]:02x}{guid_bytes[2]:02x}{guid_bytes[1]:02x}{guid_bytes[0]:02x}-"
        f"{guid_bytes[5]:02x}{guid_bytes[4]:02x}-"
        f"{guid_bytes[7]:02x}{guid_bytes[6]:02x}-"
        f"{guid_bytes[8]:02x}{guid_bytes[9]:02x}-"
        f"{guid_bytes[10]:02x}{guid_bytes[11]:02x}{guid_bytes[12]:02x}{guid_bytes[13]:02x}{guid_bytes[14]:02x}{guid_bytes[15]:02x}"
    )
    controls: List[UVCControl] = []

    b_num_controls = desc[20] if len(desc) > 20 else 0
    b_nr_in_pins = desc[21] if len(desc) > 21 else 0
    control_size_offset = 22 + b_nr_in_pins
    if control_size_offset >= len(desc):
        return ExtensionUnit(unit_id=unit_id, type="Extension Unit", guid=guid_str, controls=controls)

    b_control_size = desc[control_size_offset]
    controls_offset = control_size_offset + 1
    controls_end = controls_offset + b_control_size
    if controls_end > len(desc):
        controls_end = len(desc)

    bitmap_bytes = desc[controls_offset:controls_end]
    bitmap = int.from_bytes(bitmap_bytes, "little") if bitmap_bytes else 0
    max_bits = max(b_num_controls, 8 * b_control_size)

    for index in range(max_bits):
        if b_num_controls and index >= b_num_controls:
            break
        if (bitmap >> index) & 1:
            selector = index + 1
            controls.append(
                UVCControl(
                    unit_id=unit_id,
                    selector=selector,
                    name=f"Selector {selector}",
                    type="Extension Unit",
                )
            )

    return ExtensionUnit(unit_id=unit_id, type="Extension Unit", guid=guid_str, controls=controls)


def list_streaming_interfaces(dev: usb.core.Device) -> Dict[int, StreamingInterface]:
    """Build :class:`StreamingInterface` descriptions for *dev*."""
    interfaces: Dict[int, StreamingInterface] = {}
    for cfg in dev:
        for intf in cfg:
            if intf.bInterfaceClass != UVC_CLASS or intf.bInterfaceSubClass != VS_SUBCLASS:
                continue
            info = interfaces.setdefault(
                intf.bInterfaceNumber, StreamingInterface(interface_number=intf.bInterfaceNumber)
            )
            endpoint_address, endpoint_attributes, max_packet_size = None, None, 0
            if intf.bNumEndpoints:
                ep = intf[0]
                endpoint_address = ep.bEndpointAddress
                endpoint_attributes = ep.bmAttributes
                max_packet_size = _iso_payload_capacity(ep.wMaxPacketSize)
            info.alt_settings.append(
                AltSettingInfo(
                    alternate_setting=intf.bAlternateSetting,
                    endpoint_address=endpoint_address,
                    endpoint_attributes=endpoint_attributes,
                    max_packet_size=max_packet_size,
                )
            )
            if intf.bAlternateSetting == 0 and intf.extra_descriptors:
                info.formats = parse_vs_descriptors(bytes(intf.extra_descriptors))
    for interface in interfaces.values():
        interface.alt_settings.sort(key=lambda alt: alt.alternate_setting)
    return interfaces


def parse_vs_descriptors(extra: bytes) -> List[StreamFormat]:
    """Parse the raw ``extra_descriptors`` blob for a VS interface."""

    formats: List[StreamFormat] = []
    idx = 0
    current_format: Optional[StreamFormat] = None

    while idx + 2 < len(extra):
        length = extra[idx]
        if length == 0 or idx + length > len(extra):
            break

        dtype = extra[idx + 1]
        subtype = extra[idx + 2]
        payload = extra[idx : idx + length]

        if dtype == CS_INTERFACE:
            if subtype in {VS_FORMAT_UNCOMPRESSED, VS_FORMAT_MJPEG, VS_FORMAT_FRAME_BASED}:
                current_format = _parse_format_descriptor(payload)
                formats.append(current_format)
            elif subtype in {VS_FRAME_UNCOMPRESSED, VS_FRAME_MJPEG, VS_FRAME_FRAME_BASED} and current_format:
                frame = _parse_frame_descriptor(payload)
                if frame:
                    current_format.frames.append(frame)
            elif subtype == VS_STILL_IMAGE_FRAME_DESCRIPTOR and current_format:
                current_format.still_frames.extend(_parse_still_frame_descriptor(payload))

        idx += length

    return formats


def _parse_format_descriptor(desc: bytes) -> StreamFormat:
    fmt_index = desc[3]
    subtype = desc[2]
    guid = desc[5:21]

    if subtype == VS_FORMAT_MJPEG:
        name = "MJPEG"
    elif subtype == VS_FORMAT_UNCOMPRESSED:
        name = _format_fourcc(guid)
    elif subtype == VS_FORMAT_FRAME_BASED:
        name = f"Frame-based {_format_fourcc(guid)}"
    else:
        name = f"Subtype 0x{subtype:02x}"

    return StreamFormat(description=name, format_index=fmt_index, subtype=subtype, guid=guid)


def _parse_frame_descriptor(desc: bytes) -> Optional[FrameInfo]:
    if len(desc) < 26:
        return None

    frame_index = desc[3]
    bm_capabilities = desc[4] if len(desc) > 4 else 0
    width = int.from_bytes(desc[5:7], "little")
    height = int.from_bytes(desc[7:9], "little")
    max_frame_size = int.from_bytes(desc[17:21], "little")
    default_interval = int.from_bytes(desc[21:25], "little")
    interval_type = desc[25]

    intervals: List[int] = []
    offset = 26
    if interval_type == 0:
        if len(desc) >= offset + 12:
            min_interval = int.from_bytes(desc[offset : offset + 4], "little")
            max_interval = int.from_bytes(desc[offset + 4 : offset + 8], "little")
            step = int.from_bytes(desc[offset + 8 : offset + 12], "little")
            intervals.extend(v for v in (min_interval, max_interval, default_interval) if v)
    else:
        for _ in range(interval_type):
            if offset + 4 > len(desc):
                break
            value = int.from_bytes(desc[offset : offset + 4], "little")
            if value:
                intervals.append(value)
            offset += 4

    if default_interval and default_interval not in intervals:
        intervals.append(default_interval)

    if not intervals:
        intervals = [default_interval] if default_interval else []

    return FrameInfo(
        frame_index=frame_index,
        width=width,
        height=height,
        default_interval=default_interval,
        intervals_100ns=sorted(set(intervals)),
        max_frame_size=max_frame_size,
        bm_capabilities=bm_capabilities,
    )


def _parse_still_frame_descriptor(desc: bytes) -> List[StillFrameInfo]:
    if len(desc) < 5:
        return []

    endpoint = desc[3]
    num_sizes = desc[4]
    offset = 5
    frames: List[StillFrameInfo] = []

    for _ in range(num_sizes):
        if offset + 4 > len(desc):
            break
        width = int.from_bytes(desc[offset : offset + 2], "little")
        height = int.from_bytes(desc[offset + 2 : offset + 4], "little")
        frames.append(StillFrameInfo(width=width, height=height, endpoint_address=endpoint))
        offset += 4

    if offset >= len(desc):
        return frames

    num_compression = desc[offset]
    offset += 1
    compressions: List[int] = []
    for _ in range(num_compression):
        if offset >= len(desc):
            break
        compressions.append(int(desc[offset]))
        offset += 1

    if compressions:
        for frame in frames:
            frame.compression_indices = list(compressions)

    return frames


def describe_device(dev: usb.core.Device) -> str:
    """Human readable summary of vendor/product/serial info."""

    try:
        vendor = usb.util.get_string(dev, dev.iManufacturer)
    except (ValueError, usb.core.USBError):
        vendor = None
    try:
        product = usb.util.get_string(dev, dev.iProduct)
    except (ValueError, usb.core.USBError):
        product = None
    try:
        serial = usb.util.get_string(dev, dev.iSerialNumber)
    except (ValueError, usb.core.USBError):
        serial = None

    vendor = vendor or f"VID_{dev.idVendor:04x}"
    product = product or f"PID_{dev.idProduct:04x}"
    serial = serial or "?"
    return f"{vendor} {product} (S/N {serial})"


def select_format_and_frame(
    formats: List[StreamFormat],
    format_index: Optional[int],
    frame_index: Optional[int],
) -> Tuple[StreamFormat, FrameInfo]:
    """Resolve CLI overrides to a concrete (format, frame) tuple."""

    if not formats:
        raise ValueError("No formats advertised on interface")

    stream_format = None
    if format_index is None:
        stream_format = formats[0]
    else:
        for candidate in formats:
            if candidate.format_index == format_index:
                stream_format = candidate
                break
    if stream_format is None:
        raise ValueError(f"Format index {format_index} not found")

    frame = None
    if frame_index is None:
        if stream_format.frames:
            frame = stream_format.frames[0]
    else:
        for candidate in stream_format.frames:
            if candidate.frame_index == frame_index:
                frame = candidate
                break
    if frame is None:
        raise ValueError(
            f"Frame index {frame_index} not available for format {stream_format.format_index}"
        )

    return stream_format, frame


def resolve_stream_preference(
    interface: StreamingInterface,
    width: int,
    height: int,
    codec: str = CodecPreference.AUTO,
) -> Tuple[StreamFormat, FrameInfo]:
    """Select a (format, frame) tuple based on resolution and codec preference.

    ``codec`` may be ``auto`` (YUYV first, then MJPEG), ``yuyv`` or ``mjpeg``.
    Raises :class:`UVCError` if the requested combination does not exist.
    """

    codec = codec.lower()

    def _find(subtype: int) -> Optional[Tuple[StreamFormat, FrameInfo]]:
        match = interface.find_frame(width, height, subtype=subtype)
        if match is not None:
            return match
        if width and height:
            return None
        return interface.find_frame(0, 0, subtype=subtype)

    order: List[int] = []
    if codec == CodecPreference.YUYV:
        order = [VS_FORMAT_UNCOMPRESSED]
    elif codec == CodecPreference.MJPEG:
        order = [VS_FORMAT_MJPEG]
    else:
        order = [VS_FORMAT_UNCOMPRESSED, VS_FORMAT_MJPEG]

    for subtype in order:
        match = _find(subtype)
        if match is not None:
            return match

    match = interface.find_frame(width, height)
    if match is None and width and height:
        raise UVCError(
            f"Resolution {width}x{height} not advertised on interface {interface.interface_number}"
        )
    if match is None:
        match = interface.find_frame(0, 0)
    if match is None:
        raise UVCError("No streaming formats advertised on this interface")

    if codec != CodecPreference.AUTO:
        raise UVCError(f"Requested codec '{codec}' not available for this interface")

    return match


def resolve_still_preference(
    interface: StreamingInterface,
    width: int,
    height: int,
    codec: str = CodecPreference.AUTO,
) -> Tuple[StreamFormat, FrameInfo]:
    codec = codec.lower()

    def _find(subtype: Optional[int]) -> Optional[Tuple[StreamFormat, FrameInfo]]:
        for fmt in interface.formats:
            if subtype is not None and fmt.subtype != subtype:
                continue
            for frame in fmt.frames:
                if not frame.supports_still:
                    continue
                if width and height:
                    if frame.width == width and frame.height == height:
                        return fmt, frame
                elif frame.width == width or frame.height == height:
                    return fmt, frame
        return None

    order: List[int] = []
    if codec == CodecPreference.YUYV:
        order = [VS_FORMAT_UNCOMPRESSED]
    elif codec == CodecPreference.MJPEG:
        order = [VS_FORMAT_MJPEG]
    else:
        order = [VS_FORMAT_UNCOMPRESSED, VS_FORMAT_MJPEG]

    for subtype in order:
        match = _find(subtype)
        if match is not None:
            return match

    # Fallback: any still-capable frame
    for fmt, frame in interface.iter_still_frames():
        if width and height:
            if frame.width == width and frame.height == height:
                return fmt, frame
        else:
            return fmt, frame

    raise UVCError("No still-image capable frames advertised on this interface")


def probe_streaming_interface(
    dev: usb.core.Device,
    interface_number: int,
    stream_format: StreamFormat,
    frame: FrameInfo,
    frame_rate: Optional[float],
    do_commit: bool,
    alt_setting: Optional[int],
    keep_alt: bool = False,
    *,
    strict_interval: bool = False,
    payload_hint: int = 0,
) -> dict:
    """Claim *interface_number* and run VS_PROBE/VS_COMMIT.

    When ``alt_setting`` is provided and ``do_commit`` is true, the function
    selects that alternate setting after the commit.  If ``keep_alt`` is false
    (default) the interface is switched back to alternate 0 before returning so
    that enumeration scripts leave the camera untouched.  Streaming code can set
    ``keep_alt`` to True and manage the lifecycle manually.
    """

    try:
        dev.set_configuration()
    except usb.core.USBError:
        # The device was already configured.
        pass

    reattach = False
    try:
        if dev.is_kernel_driver_active(interface_number):
            dev.detach_kernel_driver(interface_number)
            reattach = True
    except (usb.core.USBError, NotImplementedError, AttributeError):
        pass

    usb.util.claim_interface(dev, interface_number)
    try:
        info = perform_probe_commit(
            dev,
            interface_number,
            stream_format,
            frame,
            frame_rate,
            do_commit,
            strict_interval=strict_interval,
            payload_hint=payload_hint,
        )

        if do_commit and alt_setting is not None:
            try:
                dev.set_interface_altsetting(interface=interface_number, alternate_setting=alt_setting)
            except usb.core.USBError as exc:
                info["alt_setting_error"] = str(exc)
            else:
                info["alt_setting"] = alt_setting

        return info
    finally:
        if do_commit and alt_setting is not None and not keep_alt:
            with contextlib.suppress(usb.core.USBError):
                dev.set_interface_altsetting(interface=interface_number, alternate_setting=0)
        usb.util.release_interface(dev, interface_number)
        if reattach:
            with contextlib.suppress(usb.core.USBError):
                dev.attach_kernel_driver(interface_number)


def perform_probe_commit(
    dev: usb.core.Device,
    interface_number: int,
    stream_format: StreamFormat,
    frame: FrameInfo,
    frame_rate: Optional[float],
    do_commit: bool,
    bm_hint: int = 1,
    *,
    strict_interval: bool = False,
    payload_hint: int = 0,
) -> dict:
    """Try multiple control lengths when running VS_PROBE/VS_COMMIT."""

    supported_lengths = [48, 34, 26]
    announced_length = _get_control_length(dev, interface_number, VS_PROBE_CONTROL)
    if announced_length:
        LOG.debug("VS_PROBE device announced length %s bytes", announced_length)
        if announced_length in supported_lengths:
            supported_lengths.remove(announced_length)
        supported_lengths.insert(0, announced_length)

    last_error: Optional[Exception] = None
    for length in supported_lengths:
        try:
            LOG.debug("VS_PROBE attempting control length %s bytes", length)
            return _perform_probe_commit_with_length(
                dev,
                interface_number,
                stream_format,
                frame,
                frame_rate,
                do_commit,
                bm_hint,
                strict_interval=strict_interval,
                payload_hint=payload_hint,
                length=length,
            )
        except usb.core.USBError as exc:
            last_error = exc
            if exc.errno in (errno.EINVAL, errno.EPIPE):
                LOG.warning(
                    "VS_PROBE length %s rejected with errno=%s; trying next option",
                    length,
                    exc.errno,
                )
                continue
            raise
        except Exception as exc:  # pragma: no cover - defensive logging
            last_error = exc
            LOG.warning(
                "VS_PROBE length %s failed with unexpected error: %s; trying next",
                length,
                exc,
            )
            continue

    raise last_error or UVCError("All attempted PROBE/COMMIT lengths failed")


def _perform_probe_commit_with_length(
    dev: usb.core.Device,
    interface_number: int,
    stream_format: StreamFormat,
    frame: FrameInfo,
    frame_rate: Optional[float],
    do_commit: bool,
    bm_hint: int = 1,
    *,
    strict_interval: bool = False,
    payload_hint: int = 0,

    length: int,
    probe_selector: int = VS_PROBE_CONTROL,
    commit_selector: int = VS_COMMIT_CONTROL,
) -> dict:
    """Send VS_PROBE (and optionally VS_COMMIT) using the provided selection."""
    template = _read_control(dev, GET_CUR, probe_selector, interface_number, length)
    source = "GET_CUR"
    if template is None:
        template = _read_control(dev, GET_DEF, probe_selector, interface_number, length)
        source = "GET_DEF"
    if template is None:
        template = bytes(length)
        source = "zero"
    template_bytes = bytes(template)
    LOG.debug("VS_PROBE template (%s)=%s", source, _hex_dump(template_bytes))
    payload = bytearray(length)

    candidate_interval = None
    effective_hint = 1 if bm_hint else 0
    if frame_rate is not None and frame_rate > 0:
        try:
            candidate_interval = frame.pick_interval(frame_rate, strict=strict_interval)
            effective_hint = 1
        except ValueError:
            candidate_interval = None
            effective_hint = 0
    else:
        effective_hint = 0

    _set_le_value(payload, 0, effective_hint, 2)
    if len(payload) > 2:
        payload[2] = stream_format.format_index
    if len(payload) > 3:
        payload[3] = frame.frame_index
    if effective_hint and candidate_interval is not None:
        _set_le_value(payload, 4, candidate_interval, 4)
    if payload_hint and LOG.isEnabledFor(logging.DEBUG):
        LOG.debug("Available ISO capacity hint=%s bytes", payload_hint)

    try:
        LOG.debug(
            "SET_CUR selector=0x%02x len=%s bmHint=%s fmt=%s frame=%s interval=%s payload=%s",
            probe_selector,
            length,
            effective_hint,
            stream_format.format_index,
            frame.frame_index,
            candidate_interval,
            _hex_dump(payload),
        )
        _write_control(dev, SET_CUR, probe_selector, interface_number, payload)
    except usb.core.USBError as exc:
        LOG.debug(
            "SET_CUR selector=0x%02x failed errno=%s payload=%s",
            probe_selector,
            getattr(exc, "errno", None),
            _hex_dump(payload),
        )
        raise
    negotiated = _read_control(dev, GET_CUR, probe_selector, interface_number, length)
    if negotiated is None:
        negotiated_bytes = bytes(payload)
    else:
        negotiated_bytes = bytes(negotiated)
    LOG.debug("GET_CUR selector=0x%02x payload=%s", probe_selector, _hex_dump(negotiated_bytes))

    negotiation_info = _parse_probe_payload(negotiated_bytes)

    if do_commit:
        try:
            LOG.debug("SET_CUR selector=0x%02x payload=%s", commit_selector, _hex_dump(negotiated_bytes))
            _write_control(dev, SET_CUR, commit_selector, interface_number, negotiated_bytes)
        except usb.core.USBError as exc:
            LOG.debug(
                "SET_CUR selector=0x%02x failed errno=%s payload=%s",
                commit_selector,
                getattr(exc, "errno", None),
                _hex_dump(negotiated_bytes),
            )
            raise

    negotiation_info.update(
        {
            "chosen_interval": negotiation_info.get("dwFrameInterval"),
            "requested_rate_hz": frame_rate,
            "committed": do_commit,
        }
    )
    return negotiation_info


def _parse_still_probe_payload(payload: bytes) -> dict:
    result: Dict[str, Optional[int]] = {
        "bFormatIndex": payload[0] if len(payload) > 0 else None,
        "bFrameIndex": payload[1] if len(payload) > 1 else None,
        "bCompressionIndex": payload[2] if len(payload) > 2 else None,
        "dwMaxVideoFrameSize": None,
        "dwMaxPayloadTransferSize": None,
    }

    if len(payload) >= 7:
        result["dwMaxVideoFrameSize"] = int.from_bytes(payload[3:7], "little")
    if len(payload) >= 11:
        result["dwMaxPayloadTransferSize"] = int.from_bytes(payload[7:11], "little")
    return result


def _perform_still_probe_with_length(
    dev: usb.core.Device,
    interface_number: int,
    stream_format: StreamFormat,
    frame: FrameInfo,
    compression_index: int,
    do_commit: bool,
    *,
    length: int,
) -> dict:
    template = _read_control(dev, GET_CUR, VS_STILL_PROBE_CONTROL, interface_number, length)
    source = "GET_CUR"
    if template is None:
        template = _read_control(dev, GET_DEF, VS_STILL_PROBE_CONTROL, interface_number, length)
        source = "GET_DEF"
    if template is None:
        template = bytes(length)
        source = "zero"
    LOG.debug("VS_STILL_PROBE template (%s)=%s", source, _hex_dump(bytes(template)))

    payload = bytearray(template)
    if len(payload) > 0:
        payload[0] = stream_format.format_index
    if len(payload) > 1:
        payload[1] = frame.frame_index
    if len(payload) > 2:
        payload[2] = compression_index & 0xFF

    try:
        LOG.debug(
            "SET_CUR selector=0x%02x payload=%s",
            VS_STILL_PROBE_CONTROL,
            _hex_dump(payload),
        )
        _write_control(dev, SET_CUR, VS_STILL_PROBE_CONTROL, interface_number, payload)
    except usb.core.USBError as exc:
        LOG.debug(
            "SET_CUR selector=0x%02x failed errno=%s payload=%s",
            VS_STILL_PROBE_CONTROL,
            getattr(exc, "errno", None),
            _hex_dump(payload),
        )
        raise

    negotiated = _read_control(dev, GET_CUR, VS_STILL_PROBE_CONTROL, interface_number, length)
    negotiated_bytes = bytes(negotiated) if negotiated is not None else bytes(payload)
    LOG.debug(
        "GET_CUR selector=0x%02x payload=%s",
        VS_STILL_PROBE_CONTROL,
        _hex_dump(negotiated_bytes),
    )

    if do_commit:
        try:
            _write_control(dev, SET_CUR, VS_STILL_COMMIT_CONTROL, interface_number, negotiated_bytes)
        except usb.core.USBError as exc:
            LOG.debug(
                "SET_CUR selector=0x%02x failed errno=%s payload=%s",
                VS_STILL_COMMIT_CONTROL,
                getattr(exc, "errno", None),
                _hex_dump(negotiated_bytes),
            )
            raise

    info = _parse_still_probe_payload(negotiated_bytes)
    info.update({"committed": do_commit})
    return info


def perform_still_probe_commit(
    dev: usb.core.Device,
    interface_number: int,
    stream_format: StreamFormat,
    frame: FrameInfo,
    compression_index: int = 1,
    do_commit: bool = True,
) -> dict:
    supported_lengths = [11, 13, 16]
    announced_length = _get_control_length(dev, interface_number, VS_STILL_PROBE_CONTROL)
    if announced_length:
        if announced_length in supported_lengths:
            supported_lengths.remove(announced_length)
        supported_lengths.insert(0, announced_length)

    last_error: Optional[Exception] = None
    for length in supported_lengths:
        try:
            LOG.debug("VS_STILL_PROBE attempting control length %s bytes", length)
            return _perform_still_probe_with_length(
                dev,
                interface_number,
                stream_format,
                frame,
                compression_index,
                do_commit,
                length=length,
            )
        except usb.core.USBError as exc:
            last_error = exc
            if exc.errno in (errno.EINVAL, errno.EPIPE):
                LOG.debug(
                    "VS_STILL_PROBE length %s rejected errno=%s; trying next option",
                    length,
                    exc.errno,
                )
                continue
            raise
        except Exception as exc:  # pragma: no cover - defensive logging
            last_error = exc
            LOG.debug(
                "VS_STILL_PROBE length %s failed with unexpected error: %s",
                length,
                exc,
            )
            continue

    raise last_error or UVCError("All attempted STILL PROBE/COMMIT lengths failed")



from .uvc_async import IsoConfig, UVCPacketStream, InterruptConfig, InterruptListener
class UVCCamera:
    """Minimal helper to configure a streaming interface and fetch frames."""

    def __init__(self, device: usb.core.Device, interface: StreamingInterface):
        self.device = device
        self.interface = interface
        self.interface_number = interface.interface_number

        self._claimed = False
        self._reattach = False
        self._active_alt = 0
        self._endpoint_address: Optional[int] = None
        self._max_payload: Optional[int] = None
        self._format: Optional[StreamFormat] = None
        self._frame: Optional[FrameInfo] = None
        self._async_ctx: Optional[usb1.USBContext] = None
        self._async_handle: Optional[usb1.USBDeviceHandle] = None
        self._async_stream: Optional[UVCPacketStream] = None
        self._control_interface: Optional[int] = None
        self._control_endpoint: Optional[int] = None
        self._control_packet_size: Optional[int] = None
        self._control_claimed = False
        self._vc_listener: Optional[InterruptListener] = None

        self._control_cache: Dict[Tuple[int, int, int], ControlEntry] = {}
        self._control_name_map: Dict[str, ControlEntry] = {}

        self._needs_device_reset = False

        self._committed_frame_interval: Optional[int] = None
        self._committed_payload: Optional[int] = None
        self._committed_frame_size: Optional[int] = None
        self._committed_format_index: Optional[int] = None
        self._committed_frame_index: Optional[int] = None

        self._still_format: Optional[StreamFormat] = None
        self._still_frame: Optional[FrameInfo] = None
        self._still_compression_index: int = 1
        self._still_payload: int = 0
        self._still_alt_info: Optional[AltSettingInfo] = None
        self._still_frame_size: Optional[int] = None

        vc_interface = None
        for cfg in device:
            for intf in cfg:
                if intf.bInterfaceClass == UVC_CLASS and intf.bInterfaceSubClass == 1:
                    vc_interface = intf
                    break
            if vc_interface is not None:
                break

        if vc_interface is not None:
            self._control_interface = vc_interface.bInterfaceNumber
            LOG.info("Detected Video Control interface=%s", self._control_interface)

            # Look for an explicitly advertised interrupt endpoint.
            for ep in getattr(vc_interface, "endpoints", lambda: [])():
                if (
                    usb.util.endpoint_direction(ep.bEndpointAddress)
                    == usb.util.ENDPOINT_IN
                    and usb.util.endpoint_type(ep.bmAttributes)
                    == usb.util.ENDPOINT_TYPE_INTR
                ):
                    self._control_endpoint = ep.bEndpointAddress
                    self._control_packet_size = ep.wMaxPacketSize or 16
                    LOG.info(
                        "Found VC interrupt endpoint 0x%02x size=%s",
                        self._control_endpoint,
                        self._control_packet_size,
                    )
                    break
        else:
            LOG.warning("No Video Control interface found")

    @classmethod
    def from_device(
        cls,
        device: usb.core.Device,
        interface_number: int,
    ) -> "UVCCamera":
        interfaces = list_streaming_interfaces(device)
        if interface_number not in interfaces:
            raise UVCError(f"Interface {interface_number} is not a UVC streaming interface")
        return cls(device, interfaces[interface_number])

    @classmethod
    def open(
        cls,
        *,
        vid: Optional[int] = None,
        pid: Optional[int] = None,
        device_index: int = 0,
        interface: int = 1,
    ) -> "UVCCamera":
        devices = find_uvc_devices(vid, pid)
        if not devices:
            raise UVCError("No matching UVC devices found")
        if not (0 <= device_index < len(devices)):
            raise UVCError(f"Device index {device_index} out of range (found {len(devices)})")
        return cls.from_device(devices[device_index], interface)

    def close(self) -> None:
        self.stop_streaming()
        self.stop_async_stream()

    def __enter__(self) -> "UVCCamera":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def active_alt_setting(self) -> int:
        """Return the currently selected alternate setting (0 when idle)."""

        return self._active_alt

    @property
    def endpoint_address(self) -> Optional[int]:
        """USB endpoint address used for streaming (``None`` if not configured)."""

        return self._endpoint_address

    @property
    def max_payload_size(self) -> Optional[int]:
        """Maximum payload size requested when reading packets."""

        return self._max_payload

    @property
    def current_format(self) -> Optional[StreamFormat]:
        return self._format

    @property
    def current_frame(self) -> Optional[FrameInfo]:
        return self._frame

    @property
    def current_resolution(self) -> Optional[Tuple[int, int]]:
        if self._frame is None:
            return None
        return self._frame.width, self._frame.height

    # ------------------------------------------------------------------
    # VC control helpers
    # ------------------------------------------------------------------

    def read_vc_control(
        self,
        unit_id: int,
        selector: int,
        request: int,
        length: int,
        interface_number: Optional[int] = None,
    ) -> Optional[bytes]:
        """Read a Video Control value even while an async stream is active."""

        interface = interface_number if interface_number is not None else self._control_interface
        if interface is None:
            raise UVCError("No Video Control interface available on this device")

        if (
            self._async_handle is not None
            and self._control_claimed
            and interface == self._control_interface
        ):
            w_value = selector << 8
            w_index = _vc_w_index(interface, unit_id)
            try:
                data = self._async_handle.controlRead(
                    REQ_TYPE_IN,
                    request,
                    w_value,
                    w_index,
                    length,
                    timeout=500,
                )
            except usb1.USBError as exc:
                raise UVCError(f"VC GET request failed: {exc}") from exc
            return bytes(data)

        with claim_vc_interface(self.device, interface, auto_reattach=True):
            return vc_ctrl_get(self.device, interface, unit_id, selector, request, length)

    def write_vc_control(
        self,
        unit_id: int,
        selector: int,
        payload: bytes,
        request: int = SET_CUR,
        interface_number: Optional[int] = None,
    ) -> None:
        """Write a Video Control value even while an async stream is active."""

        interface = interface_number if interface_number is not None else self._control_interface
        if interface is None:
            raise UVCError("No Video Control interface available on this device")

        if (
            self._async_handle is not None
            and self._control_claimed
            and interface == self._control_interface
        ):
            w_value = selector << 8
            w_index = _vc_w_index(interface, unit_id)
            try:
                self._async_handle.controlWrite(
                    REQ_TYPE_OUT,
                    request,
                    w_value,
                    w_index,
                    payload,
                    timeout=500,
                )
            except usb1.USBError as exc:
                raise UVCError(f"VC SET request failed: {exc}") from exc
            return

        with claim_vc_interface(self.device, interface, auto_reattach=True):
            vc_ctrl_set(self.device, interface, unit_id, selector, payload)

    # ------------------------------------------------------------------
    # High-level control API
    # ------------------------------------------------------------------

    def _refresh_control_cache(self) -> None:
        cache: Dict[Tuple[int, int, int], ControlEntry] = {}
        name_map: Dict[str, ControlEntry] = {}

        control_units = list_control_units(self.device)
        for interface_number, units in control_units.items():
            with claim_vc_interface(self.device, interface_number):
                manager = UVCControlsManager(self.device, units, interface_number=interface_number)
                for entry in manager.get_controls():
                    key = (entry.interface_number, entry.unit_id, entry.selector)
                    cache[key] = entry
                    name_map.setdefault(entry.name.lower(), entry)

        self._control_cache = cache
        self._control_name_map = name_map

    def enumerate_controls(self, *, refresh: bool = False) -> List[ControlEntry]:
        if refresh or not self._control_cache:
            self._refresh_control_cache()
        return list(self._control_cache.values())

    def _resolve_control(
        self,
        key: Union[str, Tuple[int, int], Tuple[int, int, int], ControlEntry, UVCControl],
        *,
        interface_hint: Optional[int] = None,
    ) -> ControlEntry:
        self.enumerate_controls()

        if isinstance(key, ControlEntry):
            return key

        if isinstance(key, UVCControl):
            key = (interface_hint or self._control_interface or 0, key.unit_id, key.selector)

        if isinstance(key, str):
            entry = self._control_name_map.get(key.lower())
            if entry is None:
                raise KeyError(f"Unknown control name '{key}'")
            return entry

        if isinstance(key, tuple):
            if len(key) == 3:
                interface_number, unit_id, selector = key
                entry = self._control_cache.get((interface_number, unit_id, selector))
                if entry is None:
                    raise KeyError(f"No control for interface={interface_number} unit={unit_id} selector={selector}")
                return entry
            if len(key) == 2:
                unit_id, selector = key
                candidates = [
                    entry for entry in self._control_cache.values()
                    if entry.unit_id == unit_id and entry.selector == selector
                ]
                if not candidates:
                    raise KeyError(f"No control for unit={unit_id} selector={selector}")
                if interface_hint is not None:
                    for entry in candidates:
                        if entry.interface_number == interface_hint:
                            return entry
                if self._control_interface is not None:
                    for entry in candidates:
                        if entry.interface_number == self._control_interface:
                            return entry
                return candidates[0]

        raise KeyError(f"Unsupported control key: {key!r}")

    def get_control(
        self,
        key: Union[str, Tuple[int, int], Tuple[int, int, int], ControlEntry, UVCControl],
        *,
        raw: bool = False,
        interface_hint: Optional[int] = None,
    ) -> Optional[Union[int, bytes]]:
        entry = self._resolve_control(key, interface_hint=interface_hint)
        length = entry.length
        if not length:
            if entry.raw_default:
                length = len(entry.raw_default)
            elif entry.raw_maximum:
                length = len(entry.raw_maximum)
            elif entry.raw_minimum:
                length = len(entry.raw_minimum)
            else:
                length = 4

        data = self.read_vc_control(
            entry.unit_id,
            entry.selector,
            GET_CUR,
            length,
            interface_number=entry.interface_number,
        )
        if data is None or raw:
            return data
        if len(data) <= 4:
            signed = entry.minimum is not None and entry.minimum < 0
            return int.from_bytes(data[: len(data)], "little", signed=signed)
        return data

    def set_control(
        self,
        key: Union[str, Tuple[int, int], Tuple[int, int, int], ControlEntry, UVCControl],
        value: Union[int, bytes, bytearray],
        *,
        raw: bool = False,
        interface_hint: Optional[int] = None,
    ) -> None:
        entry = self._resolve_control(key, interface_hint=interface_hint)

        if raw:
            if not isinstance(value, (bytes, bytearray)):
                raise TypeError("Raw control values must be bytes-like")
            payload = bytes(value)
        else:
            if not isinstance(value, int):
                raise TypeError("Control values must be integers (or set raw=True for bytes)")
            length = entry.length
            if not length:
                if entry.raw_default:
                    length = len(entry.raw_default)
                elif entry.raw_maximum:
                    length = len(entry.raw_maximum)
                elif entry.raw_minimum:
                    length = len(entry.raw_minimum)
                else:
                    length = 2
            signed = entry.minimum is not None and entry.minimum < 0
            payload = int(value).to_bytes(max(1, length), "little", signed=signed)

        self.write_vc_control(
            entry.unit_id,
            entry.selector,
            payload,
            interface_number=entry.interface_number,
        )

    # ------------------------------------------------------------------
    # Interface management
    # ------------------------------------------------------------------

    def _ensure_claimed(self) -> None:
        if self._claimed:
            return

        try:
            self.device.set_configuration()
        except usb.core.USBError:
            pass

        try:
            if self.device.is_kernel_driver_active(self.interface_number):
                self.device.detach_kernel_driver(self.interface_number)
                self._reattach = True
                self._needs_device_reset = True
        except (usb.core.USBError, NotImplementedError, AttributeError):
            pass

        usb.util.claim_interface(self.device, self.interface_number)
        self._claimed = True

    def _release_interface(self, *, reset_alt: bool = True) -> None:
        if not self._claimed:
            return

        if reset_alt and self._active_alt:
            with contextlib.suppress(usb.core.USBError):
                self.device.set_interface_altsetting(
                    interface=self.interface_number, alternate_setting=0
                )
            self._active_alt = 0

        usb.util.release_interface(self.device, self.interface_number)
        if self._reattach:
            with contextlib.suppress(usb.core.USBError):
                self.device.attach_kernel_driver(self.interface_number)
        self._claimed = False
        self._reattach = False
        if reset_alt:
            self._endpoint_address = None
            self._max_payload = None
            self._format = None
            self._frame = None

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    def select_stream(
        self,
        *,
        width: Optional[int] = None,
        height: Optional[int] = None,
        codec: CodecPreference = CodecPreference.AUTO,
        format_index: Optional[int] = None,
        frame_index: Optional[int] = None,
    ) -> Tuple[StreamFormat, FrameInfo]:
        """Resolve a streaming format/frame pairing using either dimensions or indexes."""

        if width is not None and height is not None:
            return resolve_stream_preference(
                self.interface,
                width,
                height,
                codec=codec,
            )

        if format_index is not None:
            for fmt in self.interface.formats:
                if fmt.format_index != format_index:
                    continue
                if frame_index is None:
                    if not fmt.frames:
                        raise UVCError(f"Format index {format_index} exposes no frames")
                    return fmt, fmt.frames[0]
                for frame in fmt.frames:
                    if frame.frame_index == frame_index:
                        return fmt, frame
            raise UVCError(f"Format index {format_index} / frame {frame_index} not advertised")

        raise UVCError("Specify either width/height or a format/frame index when selecting a stream")

    def select_still_image(
        self,
        *,
        width: Optional[int] = None,
        height: Optional[int] = None,
        codec: CodecPreference = CodecPreference.AUTO,
        format_index: Optional[int] = None,
        frame_index: Optional[int] = None,
    ) -> Tuple[StreamFormat, FrameInfo]:
        """Resolve a still-image capable format/frame pairing."""

        if width is not None and height is not None:
            return resolve_still_preference(
                self.interface,
                width,
                height,
                codec=codec,
            )

        if format_index is not None:
            for fmt in self.interface.formats:
                if fmt.format_index != format_index:
                    continue
                if frame_index is None:
                    for frame in fmt.frames:
                        if frame.supports_still:
                            return fmt, frame
                    raise UVCError(f"Format index {format_index} exposes no still-capable frames")
                for frame in fmt.frames:
                    if frame.frame_index == frame_index and frame.supports_still:
                        return fmt, frame
            raise UVCError(f"Format index {format_index} / frame {frame_index} not advertised for still images")

        # Default: choose the highest-resolution still-capable frame.
        best: Optional[Tuple[StreamFormat, FrameInfo]] = None
        best_area = -1
        for fmt, frame in self.interface.iter_still_frames():
            area = frame.width * frame.height
            if area > best_area:
                best = (fmt, frame)
                best_area = area
        if best is None:
            raise UVCError("No still-image capable frames advertised on this interface")
        return best

    def stream(
        self,
        *,
        width: Optional[int] = None,
        height: Optional[int] = None,
        codec: CodecPreference = CodecPreference.AUTO,
        format_index: Optional[int] = None,
        frame_index: Optional[int] = None,
        frame_rate: Optional[float] = None,
        strict_fps: bool = False,
        queue_size: int = 4,
        skip_initial: int = 2,
        transfers: int = 16,
        packets_per_transfer: int = 64,
        timeout_ms: int = 2000,
        duration: Optional[float] = None,
    ) -> "FrameStream":
        """Return a managed frame iterator for continuous streaming."""

        stream_format, frame = self.select_stream(
            width=width,
            height=height,
            codec=codec,
            format_index=format_index,
            frame_index=frame_index,
        )

        return FrameStream(
            camera=self,
            stream_format=stream_format,
            frame=frame,
            frame_rate=frame_rate,
            strict_fps=strict_fps,
            queue_size=queue_size,
            skip_initial=skip_initial,
            transfers=transfers,
            packets_per_transfer=packets_per_transfer,
            timeout_ms=timeout_ms,
            duration=duration,
        )

    def configure_stream(
        self,
        stream_format: StreamFormat,
        frame: FrameInfo,
        frame_rate: Optional[float] = None,
        alt_setting: Optional[int] = None,
        *,
        strict_fps: bool = False,
    ) -> dict:
        """Probe and commit the requested format/frame, preparing for streaming."""

        self._ensure_claimed()

        candidate_fps: List[Optional[float]] = []
        fps_values = [fps for fps in frame.intervals_hz() if fps and fps > 0]

        if frame_rate and frame_rate > 0:
            candidate_fps.append(frame_rate)

        if stream_format.subtype == VS_FORMAT_UNCOMPRESSED:
            fps_values = sorted(fps_values)  # lowest FPS first for bandwidth-heavy formats
        else:
            fps_values = sorted(fps_values, reverse=True)

        for fps in fps_values:
            if not any(abs(fps - existing) < 1e-2 for existing in candidate_fps if existing):
                candidate_fps.append(fps)

        candidate_fps.append(None)  # allow device to choose default interval

        bm_hints = [1, 0]

        payload_hint = 0
        for alt in self.interface.alt_settings:
            if alt.max_packet_size and alt.is_isochronous():
                payload_hint = max(payload_hint, alt.max_packet_size)

        info = None
        last_error: Optional[Exception] = None
        for hint in bm_hints:
            for fps_candidate in candidate_fps:
                if fps_candidate is None and (hint & 0x01):
                    continue
                try:
                    LOG.debug(
                        "Attempting PROBE/COMMIT with fps=%s bmHint=%s (format=%s frame=%s)",
                        fps_candidate,
                        hint,
                        stream_format.format_index,
                        frame.frame_index,
                    )
                    info = perform_probe_commit(
                        self.device,
                        self.interface_number,
                        stream_format,
                        frame,
                        fps_candidate,
                        do_commit=True,
                        bm_hint=hint,
                        strict_interval=strict_fps,
                        payload_hint=payload_hint,
                    )
                    frame_rate = fps_candidate
                    break
                except usb.core.USBError as exc:
                    last_error = exc
                    if exc.errno not in (errno.EINVAL, errno.EPIPE):
                        raise
                except UVCError as exc:
                    last_error = exc
            if info is not None:
                break
        if info is None:
            raise last_error or UVCError("Failed to negotiate streaming parameters")

        required_payload = info.get("dwMaxPayloadTransferSize") or frame.max_frame_size
        if required_payload is None or required_payload <= 0:
            required_payload = frame.max_frame_size or 0

        if alt_setting is not None:
            alt = self.interface.get_alt(alt_setting)
            if alt is None:
                raise UVCError(f"Alternate setting {alt_setting} not available on interface {self.interface_number}")
        else:
            alt = self.interface.select_alt_for_payload(required_payload)

        if alt is None or alt.endpoint_address is None:
            raise UVCError("No streaming alternate setting with an isochronous endpoint")

        previous_alt = self._active_alt
        if alt.alternate_setting != self._active_alt:
            self.device.set_interface_altsetting(
                interface=self.interface_number, alternate_setting=alt.alternate_setting
            )
            self._active_alt = alt.alternate_setting

        # Clearing HALT is recommended by the spec after switching alt settings.
        with contextlib.suppress(usb.core.USBError):
            self.device.clear_halt(alt.endpoint_address)

        self._endpoint_address = alt.endpoint_address
        # ISO packet reads must not exceed the endpoint capacity; use the
        # negotiated max payload solely to pick the correct alternate setting.
        self._max_payload = alt.max_packet_size or 0
        self._format = stream_format
        self._frame = frame

        frame_interval = info.get("dwFrameInterval") or frame.default_interval or 0
        fps = 1e7 / frame_interval if frame_interval else None
        frame_bytes = frame.max_frame_size or (frame.width * frame.height * 2)
        iso_capacity = alt.max_packet_size * 8000 if alt.max_packet_size else 0
        payload_info = info.get("dwMaxPayloadTransferSize") or 0

        LOG.debug(
            "Negotiated ctrl: fmt_idx=%s frame_idx=%s interval=%s (fps=%.3f) dwMaxPayload=%s dwMaxFrame=%s",
            stream_format.format_index,
            frame.frame_index,
            frame_interval,
            fps or -1,
            payload_info,
            frame.max_frame_size,
        )
        LOG.debug(
            "Selected alt=%s (prev=%s) endpoint=0x%02x packet=%s bytes frame_bytes=%s iso_capacity=%s",
            alt.alternate_setting,
            previous_alt,
            alt.endpoint_address,
            alt.max_packet_size,
            frame_bytes,
            iso_capacity,
        )

        if stream_format.subtype == VS_FORMAT_UNCOMPRESSED:
            if fps and frame_bytes and iso_capacity and fps * frame_bytes > iso_capacity:
                LOG.warning(
                    "Alt setting %s provides %.2f MB/s < required %.2f MB/s; expect truncated frames",
                    alt.alternate_setting,
                    iso_capacity / 1e6,
                    fps * frame_bytes / 1e6,
                )

        LOG.debug(
            "Configured stream: fmt=%s frame=%s alt=%s payload=%s",
            stream_format.description,
            f"{frame.width}x{frame.height}",
            alt.alternate_setting,
            self._max_payload,
        )

        info.update(
            {
                "selected_alt": alt.alternate_setting,
                "iso_packet_size": alt.max_packet_size,
                "endpoint_address": alt.endpoint_address,
                "frame_interval": frame_interval,
                "calculated_fps": fps,
            }
        )

        self._committed_frame_interval = frame_interval or (frame.default_interval or 0)
        self._committed_payload = payload_info or frame.max_frame_size or 0
        self._committed_frame_size = frame.max_frame_size or 0
        self._committed_format_index = stream_format.format_index
        self._committed_frame_index = frame.frame_index

        return info

    def configure_resolution(
        self,
        width: int,
        height: int,
        *,
        preferred_format_index: Optional[int] = None,
        preferred_subtype: Optional[int] = None,
        frame_rate: Optional[float] = None,
        alt_setting: Optional[int] = None,
    ) -> dict:
        """Convenience wrapper selecting a frame by its width/height."""

        match = self.interface.find_frame(
            width,
            height,
            format_index=preferred_format_index,
            subtype=preferred_subtype,
        )
        if match is None:
            raise UVCError(
                f"Resolution {width}x{height} not advertised on interface {self.interface_number}"
            )

        stream_format, frame = match
        return self.configure_stream(
            stream_format,
            frame,
            frame_rate=frame_rate,
            alt_setting=alt_setting,
        )

    def stop_streaming(self) -> None:
        """Return the interface to its idle state."""

        self._release_interface()
        self._reset_device()

    # ------------------------------------------------------------------
    # Still image helpers
    # ------------------------------------------------------------------

    def configure_still_image(
        self,
        *,
        width: Optional[int] = None,
        height: Optional[int] = None,
        codec: CodecPreference = CodecPreference.AUTO,
        format_index: Optional[int] = None,
        frame_index: Optional[int] = None,
        compression_index: int = 1,
    ) -> dict:
        """Probe and commit still-image parameters for a future capture."""

        stream_format: StreamFormat
        frame: FrameInfo

        if width is not None and height is not None:
            stream_format, frame = resolve_still_preference(
                self.interface,
                width,
                height,
                codec=codec,
            )
        elif format_index is not None:
            stream_format, frame = self.select_still_image(
                format_index=format_index,
                frame_index=frame_index,
            )
        else:
            stream_format, frame = self.select_still_image(codec=codec)

        if not frame.supports_still:
            raise UVCError("Selected frame does not advertise still-image support")

        LOG.info(
            "Configuring still image: fmt_idx=%s frame_idx=%s (%sx%s)",
            stream_format.format_index,
            frame.frame_index,
            frame.width,
            frame.height,
        )

        self._ensure_claimed()
        info = perform_still_probe_commit(
            self.device,
            self.interface_number,
            stream_format,
            frame,
            compression_index=compression_index,
            do_commit=True,
        )

        payload_hint = (
            info.get("dwMaxPayloadTransferSize")
            or info.get("dwMaxVideoFrameSize")
            or frame.max_frame_size
            or 0
        )
        alt_info = None
        if payload_hint:
            alt_info = self.interface.select_alt_for_payload(payload_hint)

        if alt_info is None and self._endpoint_address is None:
            raise UVCError("Unable to resolve an alternate setting for still capture")

        self._still_format = stream_format
        self._still_frame = frame
        negotiated_compression = info.get("bCompressionIndex")
        self._still_compression_index = (
            negotiated_compression
            if isinstance(negotiated_compression, int) and negotiated_compression
            else compression_index
        )
        self._still_payload = int(payload_hint)
        self._still_alt_info = alt_info
        self._still_frame_size = (
            info.get("dwMaxVideoFrameSize")
            or frame.max_frame_size
            or (frame.width * frame.height * 2)
        )

        return info

    def capture_still_image(self, *, timeout_ms: int = 2000) -> CapturedFrame:
        """Trigger and fetch a single still image using the negotiated settings."""

        was_claimed = self._claimed
        if self._still_format is None or self._still_frame is None:
            raise UVCError("Still image parameters not configured; call configure_still_image() first")

        self._ensure_claimed()

        previous_alt = self._active_alt
        previous_endpoint = self._endpoint_address
        previous_payload = self._max_payload
        previous_format = self._format
        previous_frame = self._frame

        alt_info = self._still_alt_info
        if alt_info is not None and alt_info.endpoint_address is None:
            alt_info = None

        if alt_info is not None:
            if alt_info.alternate_setting != self._active_alt:
                try:
                    self.device.set_interface_altsetting(
                        interface=self.interface_number,
                        alternate_setting=alt_info.alternate_setting,
                    )
                except usb.core.USBError as exc:
                    raise UVCError(f"Failed to select still-image alternate setting: {exc}") from exc
                self._active_alt = alt_info.alternate_setting
            self._endpoint_address = alt_info.endpoint_address
            self._max_payload = alt_info.max_packet_size or self._max_payload

        # Ensure decoding metadata refers to the still frame while we capture.
        self._format = self._still_format
        self._frame = self._still_frame
        original_frame_size: Optional[int] = None
        if self._frame is not None and self._still_frame_size:
            original_frame_size = self._frame.max_frame_size
            self._frame.max_frame_size = self._still_frame_size

        try:
            _write_control(
                self.device,
                SET_CUR,
                VS_STILL_IMAGE_TRIGGER_CONTROL,
                self.interface_number,
                b"\x01",
            )

            with contextlib.suppress(usb.core.USBError):
                if self._endpoint_address is not None:
                    self.device.clear_halt(self._endpoint_address)

            frame = self.read_frame(timeout_ms=timeout_ms)
        finally:
            self._format = previous_format
            self._frame = previous_frame
            if self._frame is not None and original_frame_size is not None:
                self._frame.max_frame_size = original_frame_size

            if alt_info is not None and previous_alt != self._active_alt:
                with contextlib.suppress(usb.core.USBError):
                    self.device.set_interface_altsetting(
                        interface=self.interface_number,
                        alternate_setting=previous_alt,
                    )
                self._active_alt = previous_alt
                self._endpoint_address = previous_endpoint
                self._max_payload = previous_payload

            if not was_claimed:
                self._release_interface(reset_alt=False)

        return frame

    # ------------------------------------------------------------------
    # Asynchronous streaming (isochronous transfers via libusb1)
    # ------------------------------------------------------------------

    def start_async_stream(
        self,
        packet_callback: Callable[[bytes], None],
        *,
        transfers: int = 8,
        packets_per_transfer: int = 32,
        timeout_ms: int = 1000,
    ) -> None:
        """Start ISO streaming with robust VC polling keep-alive."""

        if self._format is None or self._frame is None:
            raise UVCError("Stream not configured; call configure_stream() first")
        if self._endpoint_address is None or not self._max_payload:
            raise UVCError("Streaming endpoint not initialised")
        if self._async_stream is not None:
            raise UVCError("Asynchronous stream already active")

        endpoint = self._endpoint_address
        alt = self._active_alt

        self._needs_device_reset = True

        bus = getattr(self.device, "bus", None)
        address = getattr(self.device, "address", None)
        if bus is None or address is None:
            raise UVCError("Unable to determine device bus/address for libusb1 handle")

        self._release_interface(reset_alt=False)

        ctx = usb1.USBContext()
        handle = None
        for dev_handle in ctx.getDeviceList():
            if dev_handle.getBusNumber() == bus and dev_handle.getDeviceAddress() == address:
                handle = dev_handle.open()
                break
        if handle is None:
            ctx.close()
            raise UVCError("Failed to reopen device via libusb1 lookup")
        handle.setAutoDetachKernelDriver(True)

        control_claimed = False
        if self._control_interface is not None and self._control_endpoint is not None:
            try:
                handle.claimInterface(self._control_interface)
                control_claimed = True
                LOG.info("Claimed VC interface %s", self._control_interface)
            except usb1.USBError as exc:
                LOG.warning("Failed to claim VC interface: %s", exc)

        try:
            handle.claimInterface(self.interface_number)
            LOG.info("Claimed streaming interface %s", self.interface_number)
        except usb1.USBError as exc:
            with contextlib.suppress(usb1.USBError):
                if control_claimed and self._control_interface is not None:
                    handle.releaseInterface(self._control_interface)
            handle.close()
            ctx.close()
            raise UVCError(
                f"Failed to claim VS interface {self.interface_number}: {exc}"
            ) from exc

        try:
            handle.setInterfaceAltSetting(self.interface_number, 0)
            time.sleep(0.05)
            self._run_libusb_probe_commit(handle)
            handle.setInterfaceAltSetting(self.interface_number, alt)
            LOG.info(
                "VS interface %s set to alt %s", self.interface_number, alt
            )
            time.sleep(0.1)
        except usb1.USBError as exc:
            with contextlib.suppress(usb1.USBError):
                handle.releaseInterface(self.interface_number)
                if control_claimed and self._control_interface is not None:
                    handle.releaseInterface(self._control_interface)
            handle.close()
            ctx.close()
            raise UVCError(f"Failed to set alternate setting: {exc}") from exc

        with contextlib.suppress(usb1.USBError):
            LOG.debug("Clearing halt on endpoint 0x%02x", endpoint)
            handle.clearHalt(endpoint)

        iso_config = IsoConfig(
            endpoint=endpoint,
            packet_size=self._max_payload,
            transfers=transfers,
            packets_per_transfer=packets_per_transfer,
            timeout_ms=timeout_ms,
        )

        def _callback(data: bytes) -> None:
            if self._async_stream and self._async_stream.is_active():
                packet_callback(data)

        stream = UVCPacketStream(ctx, handle, iso_config, _callback)
        time.sleep(0.15)
        stream.start()

        self._async_ctx = ctx
        self._async_handle = handle
        self._async_stream = stream
        self._control_claimed = control_claimed

        if control_claimed and self._control_endpoint is not None and self._control_packet_size:
            try:
                self._vc_listener = InterruptListener(
                    ctx,
                    handle,
                    InterruptConfig(
                        endpoint=self._control_endpoint,
                        packet_size=self._control_packet_size,
                        timeout_ms=0,
                    ),
                    lambda data: LOG.debug("VC interrupt data=%s", data.hex()),
                )
                self._vc_listener.start()
                LOG.info(
                    "VC interrupt listener started on endpoint 0x%02x",
                    self._control_endpoint,
                )
            except usb1.USBError as exc:
                LOG.warning("Failed to start VC interrupt listener: %s", exc)
                self._vc_listener = None

    def poll_async_events(self, timeout: float = 0.1) -> None:
        if self._async_ctx is None or self._async_stream is None:
            return
        tv = int(timeout * 1e6)
        with contextlib.suppress(Exception):
            self._async_stream.handle_events_and_resubmit(tv)

    def _reset_device(self) -> None:
        if not self._needs_device_reset:
            return
        with contextlib.suppress(usb.core.USBError):
            LOG.debug("Resetting USB device to restore kernel state")
            self.device.reset()
        self._needs_device_reset = False

    def _run_libusb_probe_commit(self, handle: usb1.USBDeviceHandle) -> None:
        """Perform a full, robust PROBE/COMMIT sequence using a libusb1 handle."""
        if self._format is None or self._frame is None:
            raise UVCError("Stream not configured; call configure_stream() first")

        length = 34  # Use the length that is known to work for most devices.
        timeout = 1000
        req_in = usb1.TYPE_CLASS | usb1.RECIPIENT_INTERFACE | usb1.ENDPOINT_IN
        req_out = usb1.TYPE_CLASS | usb1.RECIPIENT_INTERFACE | usb1.ENDPOINT_OUT

        try:
            template = handle.controlRead(
                req_in, GET_CUR, VS_PROBE_CONTROL << 8, self.interface_number, length, timeout
            )
            LOG.debug("libusb1 PROBE template from GET_CUR: %s", template.hex())
        except usb1.USBError:
            LOG.debug("libusb1 PROBE GET_CUR failed, using zeroed buffer")
            template = bytes(length)

        buf = bytearray(template)

        interval = self._committed_frame_interval or self._frame.pick_interval(None)

        bm_hint = 1
        buf[0:2] = bm_hint.to_bytes(2, "little")
        buf[2] = self._committed_format_index or self._format.format_index
        buf[3] = self._committed_frame_index or self._frame.frame_index
        buf[4:8] = int(interval or 0).to_bytes(4, "little")

        LOG.debug("libusb1 PROBE SET_CUR: %s", bytes(buf).hex())
        handle.controlWrite(
            req_out, SET_CUR, VS_PROBE_CONTROL << 8, self.interface_number, bytes(buf), timeout
        )

        negotiated = bytes(handle.controlRead(
            req_in, GET_CUR, VS_PROBE_CONTROL << 8, self.interface_number, length, timeout
        ))
        LOG.debug("libusb1 PROBE GET_CUR (negotiated): %s", negotiated.hex())

        LOG.debug("libusb1 COMMIT SET_CUR: %s", negotiated.hex())
        handle.controlWrite(
            req_out, SET_CUR, VS_COMMIT_CONTROL << 8, self.interface_number, negotiated, timeout
        )

    def stop_async_stream(self) -> None:
        if self._async_stream is None:
            LOG.debug("No async stream to stop")
            return

        LOG.info("Stopping async stream")

        if self._vc_listener is not None:
            self._vc_listener.stop()
            self._vc_listener = None

        self._async_stream.stop()

        if self._async_handle is not None:
            if self._control_claimed and self._control_interface is not None:
                with contextlib.suppress(usb1.USBError):
                    LOG.debug("Releasing VC interface %s", self._control_interface)
                    self._async_handle.releaseInterface(self._control_interface)
            with contextlib.suppress(usb1.USBError):
                LOG.debug("Resetting VS interface %s to alt 0", self.interface_number)
                self._async_handle.setInterfaceAltSetting(self.interface_number, 0)
                time.sleep(0.1)
            with contextlib.suppress(usb1.USBError):
                self._async_handle.releaseInterface(self.interface_number)
            with contextlib.suppress(usb1.USBError):
                self._async_handle.close()

        if self._async_ctx is not None:
            with contextlib.suppress(Exception):
                self._async_ctx.close()

        self._async_stream = None
        self._async_handle = None
        self._async_ctx = None
        self._control_claimed = False

        # Ensure the kernel driver is reattached for VC and VS interfaces.
        if self._control_interface is not None:
            with contextlib.suppress(usb.core.USBError):
                if not self.device.is_kernel_driver_active(self._control_interface):
                    LOG.debug("Reattaching kernel driver on VC interface %s", self._control_interface)
                    self.device.attach_kernel_driver(self._control_interface)

        with contextlib.suppress(usb.core.USBError):
            if not self.device.is_kernel_driver_active(self.interface_number):
                LOG.debug("Reattaching kernel driver on VS interface %s", self.interface_number)
                self.device.attach_kernel_driver(self.interface_number)

        self._reset_device()

        LOG.info("Async stream stopped")

    def read_frame(self, timeout_ms: int = 1000) -> CapturedFrame:
        """Read a single video frame from the streaming endpoint."""

        if not self._claimed or self._endpoint_address is None or self._max_payload is None:
            raise UVCError("Stream not configured; call configure_stream() first")

        expected_size = self._frame.max_frame_size if self._frame else None
        if (
            self._format is not None
            and (
                self._format.subtype == VS_FORMAT_MJPEG
                or "MJPG" in (self._format.description or "").upper()
            )
        ):
            expected_size = None
        frame_bytes = bytearray()
        current_fid: Optional[int] = None
        err_seen = False
        packets_seen = 0

        while True:
            try:
                packet = self.device.read(
                    self._endpoint_address,
                    self._max_payload,
                    timeout_ms,
                )
            except usb.core.USBError as exc:
                if exc.errno == errno.ETIMEDOUT:
                    continue
                raise

            if not packet:
                continue

            header_len = packet[0]
            if header_len < 2 or header_len > len(packet):
                frame_bytes.clear()
                current_fid = None
                err_seen = False
                packets_seen = 0
                continue

            flags = packet[1]
            payload = packet[header_len:]
            fid = flags & BH_FID
            eof = bool(flags & BH_EOF)

            if flags & BH_ERR:
                err_seen = True

            if current_fid is None:
                current_fid = fid
                frame_bytes.clear()
                err_seen = bool(flags & BH_ERR)
                packets_seen = 0
            elif fid != current_fid:
                if (
                    self._format is not None
                    and self._frame is not None
                    and not err_seen
                    and (expected_size is None or len(frame_bytes) == expected_size)
                ):
                    return CapturedFrame(
                        payload=bytes(frame_bytes),
                        format=self._format,
                        frame=self._frame,
                        fid=current_fid,
                        pts=None,
                    )
                frame_bytes.clear()
                current_fid = fid
                err_seen = bool(flags & BH_ERR)
                packets_seen = 0

            if payload:
                frame_bytes.extend(payload)
            packets_seen += 1

            if expected_size and self._max_payload:
                max_packets = max(4, (expected_size // self._max_payload) + 16)
                if packets_seen > max_packets:
                    LOG.debug(
                        "Abandoning frame after %s packets (expected <=%s)",
                        packets_seen,
                        max_packets,
                    )
                    frame_bytes.clear()
                    current_fid = None
                    err_seen = False
                    packets_seen = 0
                    continue

            if not eof:
                continue

            if (
                self._format is None
                or self._frame is None
                or err_seen
                or (expected_size is not None and len(frame_bytes) != expected_size)
            ):
                frame_bytes.clear()
                current_fid = None
                err_seen = False
                packets_seen = 0
                continue

            pts = None
            if flags & BH_PTS and header_len >= 6:
                pts = int.from_bytes(packet[2:6], "little")

            result = CapturedFrame(
                payload=bytes(frame_bytes),
                format=self._format,
                frame=self._frame,
                fid=current_fid,
                pts=pts,
            )

            frame_bytes.clear()
            current_fid = None
            err_seen = False
            packets_seen = 0
            return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _interval_to_hz(interval_100ns: int) -> float:
    return 1e7 / interval_100ns if interval_100ns else 0.0


def _format_fourcc(guid: bytes) -> str:
    if len(guid) >= 4:
        code = guid[:4]
        try:
            text = code.decode("ascii")
            text = text.rstrip("\x00")
            if text and all(32 <= ord(ch) < 127 for ch in text):
                return text
            return f"0x{code.hex()}"
        except UnicodeDecodeError:
            return code.hex()
    return "UNKNOWN"


def _iso_payload_capacity(w_max_packet_size: int) -> int:
    """Return the actual payload size taking additional transactions into account."""

    base = w_max_packet_size & 0x7FF
    multiplier = ((w_max_packet_size >> 11) & 0x3) + 1
    return base * multiplier


def _get_control_length(dev: usb.core.Device, interface_number: int, selector: int) -> Optional[int]:
    try:
        data = dev.ctrl_transfer(REQ_TYPE_IN, GET_LEN, selector << 8, interface_number, 2)
    except usb.core.USBError:
        return None
    if len(data) >= 2:
        return int.from_bytes(data[:2], "little")
    return None


def _read_control(
    dev: usb.core.Device,
    request: int,
    selector: int,
    interface_number: int,
    length: int,
) -> Optional[bytes]:
    try:
        return dev.ctrl_transfer(REQ_TYPE_IN, request, selector << 8, interface_number, length)
    except usb.core.USBError:
        return None


def _write_control(
    dev: usb.core.Device,
    request: int,
    selector: int,
    interface_number: int,
    data: bytes,
) -> None:
    dev.ctrl_transfer(REQ_TYPE_OUT, request, selector << 8, interface_number, data)


def _set_le_value(buf: bytearray, offset: int, value: int, size: int) -> None:
    if offset + size <= len(buf):
        buf[offset : offset + size] = int(value).to_bytes(size, "little", signed=False)


def _hex_dump(data: bytes, limit: int = 64) -> str:
    if not data:
        return ""
    hexed = data.hex()
    if len(data) <= limit:
        return hexed
    omitted = len(data) - limit
    return f"{hexed[: 2 * limit]}...( +{omitted}B)"


def _parse_probe_payload(payload: bytes) -> dict:
    def le16(off: int) -> Optional[int]:
        return int.from_bytes(payload[off : off + 2], "little") if off + 2 <= len(payload) else None

    def le32(off: int) -> Optional[int]:
        return int.from_bytes(payload[off : off + 4], "little") if off + 4 <= len(payload) else None

    result = {
        "bmHint": le16(0),
        "bFormatIndex": payload[2] if len(payload) > 2 else None,
        "bFrameIndex": payload[3] if len(payload) > 3 else None,
        "dwFrameInterval": le32(4),
        "dwMaxVideoFrameSize": le32(18),
        "dwMaxPayloadTransferSize": le32(22),
    }
    interval = result.get("dwFrameInterval")
    if interval:
        result["frame_rate_hz"] = _interval_to_hz(interval)
    return result


class FrameStream:
    """Context manager and iterator yielding :class:`CapturedFrame` objects."""

    def __init__(
        self,
        camera: UVCCamera,
        stream_format: StreamFormat,
        frame: FrameInfo,
        frame_rate: Optional[float],
        *,
        strict_fps: bool,
        queue_size: int,
        skip_initial: int,
        transfers: int,
        packets_per_transfer: int,
        timeout_ms: int,
        duration: Optional[float],
    ) -> None:
        self._camera = camera
        self._format = stream_format
        self._frame = frame
        self._frame_rate = frame_rate
        self._strict_fps = strict_fps
        self._queue: "queue.Queue[Optional[CapturedFrame]]" = queue.Queue(maxsize=max(1, queue_size))
        self._skip_initial = max(0, skip_initial)
        self._transfers = transfers
        self._packets_per_transfer = packets_per_transfer
        self._timeout_ms = timeout_ms
        self._duration = duration

        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._active = False
        self._start_time = 0.0
        self._sequence = 0

        self._frame_bytes = bytearray()
        self._current_fid: Optional[int] = None
        self._frame_error = False
        self._current_pts: Optional[int] = None

        self._expected_size = (
            None
            if stream_format.subtype == VS_FORMAT_MJPEG or "MJPG" in stream_format.description.upper()
            else frame.max_frame_size or (frame.width * frame.height * 2)
        )

    def __enter__(self) -> "FrameStream":
        negotiation = self._camera.configure_stream(
            self._format,
            self._frame,
            frame_rate=self._frame_rate,
            strict_fps=self._strict_fps,
        )
        LOG.debug("FrameStream negotiation: %s", negotiation)

        self._camera.start_async_stream(
            self._on_packet,
            transfers=self._transfers,
            packets_per_transfer=self._packets_per_transfer,
            timeout_ms=self._timeout_ms,
        )

        self._start_time = time.time()
        self._stop_event.clear()
        self._active = True

        self._poll_thread = threading.Thread(target=self._poll_loop, name="uvc-frame-poll", daemon=True)
        self._poll_thread.start()

        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __iter__(self):
        while True:
            if self._duration is not None and self._active:
                if time.time() - self._start_time >= self._duration:
                    LOG.info("FrameStream duration %.2fs reached", self._duration)
                    self.close()
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                if not self._active and self._queue.empty():
                    break
                continue
            if item is None:
                break
            yield item

    def close(self) -> None:
        if not self._active:
            return

        self._stop_event.set()
        self._active = False

        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

        self._camera.stop_async_stream()
        self._camera.stop_streaming()

        if self._poll_thread is not None:
            self._poll_thread.join(timeout=0.5)
            self._poll_thread = None

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            self._camera.poll_async_events(0.05)

    def _enqueue(self, frame: CapturedFrame) -> None:
        if not self._active:
            return
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            try:
                _ = self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(frame)
            except queue.Full:
                LOG.debug("FrameStream queue full; dropping frame %s", frame.sequence)

    def _finalize_frame(self, reason: str) -> None:
        if self._current_fid is None:
            return

        if not self._frame_error and self._frame_bytes:
            if self._skip_initial > 0:
                self._skip_initial -= 1
            else:
                self._sequence += 1
                frame = CapturedFrame(
                    payload=bytes(self._frame_bytes),
                    format=self._format,
                    frame=self._frame,
                    fid=self._current_fid,
                    pts=self._current_pts,
                    timestamp=time.time(),
                    sequence=self._sequence,
                )
                LOG.debug(
                    "FrameStream accepted frame #%s (reason=%s size=%s)",
                    frame.sequence,
                    reason,
                    len(self._frame_bytes),
                )
                self._enqueue(frame)
        else:
            LOG.debug(
                "FrameStream dropped frame (reason=%s error=%s size=%s)",
                reason,
                self._frame_error,
                len(self._frame_bytes),
            )

        self._frame_bytes.clear()
        self._frame_error = False
        self._current_fid = None
        self._current_pts = None

    def _on_packet(self, packet: bytes) -> None:
        if not self._active or not packet:
            return

        header_len = packet[0]
        if header_len < 2 or header_len > len(packet):
            self._frame_error = True
            return

        flags = packet[1]
        payload = packet[header_len:]

        if flags & BH_PTS and header_len >= 6:
            self._current_pts = int.from_bytes(packet[2:6], "little")

        fid = flags & BH_FID
        eof = bool(flags & BH_EOF)
        err = bool(flags & BH_ERR)

        if self._current_fid is None:
            self._current_fid = fid
            self._frame_bytes.clear()
            self._frame_error = err
        elif fid != self._current_fid:
            self._finalize_frame("fid-toggle")
            self._current_fid = fid

        if err:
            self._frame_error = True

        if payload:
            self._frame_bytes.extend(payload)

        if self._expected_size is not None and len(self._frame_bytes) > self._expected_size:
            self._frame_error = True

        if eof:
            self._finalize_frame("eof")

def yuy2_to_rgb(payload: bytes, width: int, height: int):
    """Convert a single YUY2 frame into an RGB ``numpy.ndarray``.

    The function imports :mod:`numpy` lazily so that users who only need the
    descriptor utilities do not have to install it.
    """

    try:
        import numpy as np
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("numpy is required to convert YUY2 payloads") from exc

    if width % 2:
        raise ValueError("YUY2 frames must have an even width")

    expected = width * height * 2
    if len(payload) != expected:
        raise ValueError(f"YUY2 payload length {len(payload)} does not match {width}x{height}")

    data = np.frombuffer(payload, dtype=np.uint8)
    grouped = data.reshape((height, width // 2, 4))

    y0 = grouped[:, :, 0].astype(np.int32) - 16
    u = grouped[:, :, 1].astype(np.int32) - 128
    y1 = grouped[:, :, 2].astype(np.int32) - 16
    v = grouped[:, :, 3].astype(np.int32) - 128

    y = np.empty((height, width), dtype=np.int32)
    y[:, 0::2] = y0
    y[:, 1::2] = y1
    u_full = np.repeat(u, 2, axis=1)
    v_full = np.repeat(v, 2, axis=1)

    c = np.clip(y, 0, None)
    r = (298 * c + 409 * v_full + 128) >> 8
    g = (298 * c - 100 * u_full - 208 * v_full + 128) >> 8
    b = (298 * c + 516 * u_full + 128) >> 8

    rgb = np.stack((r, g, b), axis=-1)
    return np.clip(rgb, 0, 255).astype(np.uint8)


def gray8_to_rgb(payload: bytes, width: int, height: int):
    """Convert an 8-bit grayscale payload into an RGB array."""

    try:
        import numpy as np
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("numpy is required to convert grayscale payloads") from exc

    expected = width * height
    if len(payload) != expected:
        raise ValueError(f"GRAY8 payload length {len(payload)} does not match {width}x{height}")

    gray = np.frombuffer(payload, dtype=np.uint8).reshape((height, width))
    return np.repeat(gray[:, :, None], 3, axis=2)


def gray16_to_rgb(payload: bytes, width: int, height: int):
    """Convert a 16-bit grayscale payload into an RGB array (scaled to 8-bit)."""

    try:
        import numpy as np
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("numpy is required to convert grayscale payloads") from exc

    expected = width * height * 2
    if len(payload) != expected:
        raise ValueError(f"GRAY16 payload length {len(payload)} does not match {width}x{height}")

    gray16 = np.frombuffer(payload, dtype=np.uint16).reshape((height, width))
    gray8 = (gray16 >> 8).astype(np.uint8)
    return np.repeat(gray8[:, :, None], 3, axis=2)


def _trim_mjpeg_payload(payload: bytes) -> bytes:
    """Return payload truncated at the JPEG EOI marker if trailing garbage is present."""

    if not payload:
        return payload
    eoi = payload.rfind(b"\xff\xd9")
    if eoi == -1:
        return payload
    if eoi + 2 == len(payload):
        return payload
    trimmed = payload[: eoi + 2]
    LOG.debug("Trimming MJPEG payload from %s to %s bytes (extraneous %s bytes)", len(payload), len(trimmed), len(payload) - len(trimmed))
    return trimmed


def decode_to_rgb(payload: bytes, stream_format: StreamFormat, frame: FrameInfo):
    """Convert a raw payload into an RGB image (numpy array).

    Supports YUY2/YUYV and MJPEG.  Raises :class:`RuntimeError` if decoding is
    not possible due to missing dependencies (e.g. OpenCV for MJPEG).
    """

    name = stream_format.description.upper()
    payload_len = len(payload)

    if stream_format.subtype == VS_FORMAT_UNCOMPRESSED:
        if payload_len == frame.width * frame.height:
            return gray8_to_rgb(payload, frame.width, frame.height)
        if (
            payload_len == frame.width * frame.height * 2
            and "YUY" not in name
            and "YUV" not in name
        ):
            return gray16_to_rgb(payload, frame.width, frame.height)
        if "YUY" in name or "YUV" in name:
            return yuy2_to_rgb(payload, frame.width, frame.height)
        # Fallback to the previous behaviour for other uncompressed formats.
        return yuy2_to_rgb(payload, frame.width, frame.height)

    if stream_format.subtype == VS_FORMAT_MJPEG or "MJPG" in name:
        try:
            import cv2
            import numpy as np
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("OpenCV required for MJPEG decoding") from exc

        cleaned = _trim_mjpeg_payload(payload)
        arr = np.frombuffer(cleaned, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None:
            raise RuntimeError("Failed to decode MJPEG frame (corrupt or unsupported payload)")
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        return rgb

    raise RuntimeError(f"Unsupported codec for conversion: {stream_format.description}")


class MJPEGPreviewPipeline:
    """Feed MJPEG frames into a GStreamer pipeline for quick preview."""

    def __init__(self, fps: float):
        if not GST_AVAILABLE:
            raise RuntimeError("GStreamer bindings not available; install python3-gi and gst packages")

        Gst.init(None)
        fps_num = max(1, int(round(fps))) if fps > 0 else 30
        pipeline_desc = (
            f"appsrc name=src is-live=true do-timestamp=true format=time "
            f"caps=image/jpeg,framerate={fps_num}/1 ! "
            "jpegdec ! videoconvert ! autovideosink sync=false"
        )
        self._pipeline = Gst.parse_launch(pipeline_desc)
        self._appsrc = self._pipeline.get_by_name("src")
        self._loop = GLib.MainLoop()
        self._thread = threading.Thread(target=self._loop.run, daemon=True)
        self._pipeline.set_state(Gst.State.PLAYING)
        self._thread.start()
        self._fps = fps

    def push(self, payload: bytes, timestamp_s: float) -> None:
        buf = Gst.Buffer.new_allocate(None, len(payload), None)
        buf.fill(0, payload)
        if self._fps > 0:
            duration = Gst.util_uint64_scale_int(
                1, Gst.SECOND, max(1, int(round(self._fps)))
            )
            buf.duration = duration
        timestamp = int(timestamp_s * Gst.SECOND)
        buf.pts = buf.dts = timestamp
        self._appsrc.emit("push-buffer", buf)

    def close(self) -> None:
        if self._pipeline:
            with contextlib.suppress(Exception):
                self._appsrc.emit("end-of-stream")
            self._pipeline.set_state(Gst.State.NULL)
        if self._loop.is_running():
            self._loop.quit()
        self._thread.join(timeout=2)



import contextlib

# ---------------------------------------------------------------------------
# VC (Video Control) helpers — user-space control even if uvcvideo is loaded
# ---------------------------------------------------------------------------

def find_vc_interface_number(dev: usb.core.Device) -> int:
    """Return the interface number of the first VC interface (class 0x0e, subclass 0x01)."""
    for cfg in dev:
        for intf in cfg:
            if intf.bInterfaceClass == UVC_CLASS and intf.bInterfaceSubClass == VC_SUBCLASS:
                return intf.bInterfaceNumber
    return 0


def _vc_w_index(vc_interface: int, unit_id: int) -> int:
    # entity/unit id occupies the high byte, interface number in the low byte
    return ((unit_id & 0xFF) << 8) | (vc_interface & 0xFF)


def _vc_w_value(selector: int) -> int:
    # selector in high byte
    return (selector & 0xFF) << 8


@contextlib.contextmanager
def claim_vc_interface(
    dev: usb.core.Device,
    vc_if: int,
    *,
    auto_reattach: bool = True,
    auto_detach: Optional[bool] = None,
):
    """Detach only the VC interface from the kernel, claim it, then release and reattach."""
    reattach = False
    detach = _auto_detach_vc_enabled() if auto_detach is None else bool(auto_detach)

    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass

    if detach:
        try:
            if dev.is_kernel_driver_active(vc_if):
                dev.detach_kernel_driver(vc_if)
                reattach = True
        except (usb.core.USBError, NotImplementedError, AttributeError):
            pass
    else:
        LOG.debug(
            "Auto-detach disabled; attempting to claim VC interface %s without detaching kernel driver",
            vc_if,
        )

    try:
        usb.util.claim_interface(dev, vc_if)
    except usb.core.USBError as exc:
        if detach or getattr(exc, "errno", None) not in {errno.EBUSY, errno.EPERM}:
            raise
        LOG.warning(
            "Failed to claim VC interface %s while LIBUSB_UVC_AUTO_DETACH_VC=0: %s",
            vc_if,
            exc,
        )
        raise RuntimeError(
            "VC interface is busy. Detach the kernel driver or enable auto-detach."
        ) from exc
    try:
        yield
    finally:
        with contextlib.suppress(usb.core.USBError):
            usb.util.release_interface(dev, vc_if)
        if auto_reattach and reattach:
            with contextlib.suppress(usb.core.USBError):
                dev.attach_kernel_driver(vc_if)


def vc_ctrl_get(dev: usb.core.Device, vc_if: int, unit_id: int, selector: int, request: int, length: int):
    """Low level VC GET_* (GET_CUR/MIN/MAX/RES/DEF/INFO/LEN)."""
    wValue = _vc_w_value(selector)
    wIndex = _vc_w_index(vc_if, unit_id)
    try:
        data = dev.ctrl_transfer(REQ_TYPE_IN, request, wValue, wIndex, length, timeout=500)
        return bytes(data)
    except usb.core.USBError:
        return None


def vc_ctrl_set(dev: usb.core.Device, vc_if: int, unit_id: int, selector: int, payload: bytes):
    """Low level VC SET_CUR."""
    wValue = _vc_w_value(selector)
    wIndex = _vc_w_index(vc_if, unit_id)
    return dev.ctrl_transfer(REQ_TYPE_OUT, SET_CUR, wValue, wIndex, payload, timeout=500)


def _vc_get_len(dev: usb.core.Device, vc_if: int, unit_id: int, selector: int):
    data = vc_ctrl_get(dev, vc_if, unit_id, selector, GET_LEN, 2)
    if not data or len(data) < 2:
        return None
    value = int.from_bytes(data[:2], "little")
    return value or None


def read_vc_control_value(dev: usb.core.Device, vc_if: int, unit_id: int, selector: int, request: int, *, length_hint: int = 64):
    """Read a VC control using GET_LEN if available, else length_hint."""
    length = _vc_get_len(dev, vc_if, unit_id, selector) or length_hint
    return vc_ctrl_get(dev, vc_if, unit_id, selector, request, length)


def write_vc_control_value(dev: usb.core.Device, vc_if: int, unit_id: int, selector: int, payload: bytes):
    return vc_ctrl_set(dev, vc_if, unit_id, selector, payload)


class UVCControlsManager:
    """Validate UVC controls and enrich them with quirks metadata."""

    def __init__(
        self,
        device: usb.core.Device,
        units: List[UVCUnit],
        interface_number: Optional[int] = None,
    ) -> None:
        self._device = device
        self._interface = (
            interface_number
            if interface_number is not None
            else find_vc_interface_number(device)
        )
        self._units = units
        self._quirks = load_quirks()
        self._controls: List[ControlEntry] = []
        self._initialise()

    def _initialise(self) -> None:
        def _bytes_to_int(data: Optional[bytes], *, signed: bool = False) -> Optional[int]:
            if not data:
                return None
            if len(data) > 4:
                return None
            return int.from_bytes(data, "little", signed=signed)

        def _should_use_signed(min_raw: Optional[bytes], max_raw: Optional[bytes]) -> bool:
            if not min_raw or not max_raw:
                return False
            if len(min_raw) != len(max_raw):
                return False
            if len(min_raw) not in (2, 4):
                return False
            min_unsigned = int.from_bytes(min_raw, "little", signed=False)
            max_unsigned = int.from_bytes(max_raw, "little", signed=False)
            return min_unsigned > max_unsigned

        def _payload_length(length: int, min_raw: Optional[bytes], default_raw: Optional[bytes]) -> Optional[int]:
            if length:
                return length
            if default_raw:
                return len(default_raw)
            if min_raw:
                return len(min_raw)
            return None

        def _match_get_info(info_value: int, definition: dict) -> Optional[int]:
            if definition is None:
                return 0
            score = 0

            expected_info = definition.get("expected_info")
            if expected_info is not None:
                if isinstance(expected_info, (list, tuple, set)):
                    values = {int(v) for v in expected_info}
                    if info_value not in values:
                        return None
                else:
                    if info_value != int(expected_info):
                        return None
                score += 2

            info_expect = definition.get("get_info_expect")
            if info_expect is not None:
                if isinstance(info_expect, dict):
                    value = info_expect.get("value")
                    if value is not None and info_value != int(value):
                        return None
                    if value is not None:
                        score += 2
                    for key, bit_value in info_expect.items():
                        if key == "value":
                            continue
                        key_str = str(key).upper()
                        if key_str.startswith("D") and key_str[1:].isdigit():
                            bit_index = int(key_str[1:])
                            if bit_index < 0 or bit_index > 7:
                                continue
                            expected_bit = 1 if int(bit_value) else 0
                            if ((info_value >> bit_index) & 0x01) != expected_bit:
                                return None
                            score += 1
                else:
                    try:
                        expected_value = int(info_expect)
                    except (TypeError, ValueError):
                        expected_value = None
                    if expected_value is not None:
                        if info_value != expected_value:
                            return None
                        score += 2
            return score

        def _match_length(payload_len: Optional[int], definition: dict) -> Optional[int]:
            if payload_len is None or definition is None:
                return 0

            expected = definition.get("expected_length")
            payload_spec = definition.get("payload")
            score = 0

            if expected is None and isinstance(payload_spec, dict):
                expected = payload_spec.get("fixed_len")
                if expected is None:
                    expected = payload_spec.get("expected_length")

            allowed_lengths: Optional[set] = None
            if expected is not None:
                if isinstance(expected, (list, tuple, set)):
                    allowed_lengths = {int(v) for v in expected}
                else:
                    allowed_lengths = {int(expected)}

            if allowed_lengths is not None:
                if payload_len not in allowed_lengths:
                    return None
                score += 2

            if isinstance(payload_spec, dict):
                min_len = payload_spec.get("min_len")
                max_len = payload_spec.get("max_len")
                if min_len is not None and payload_len < int(min_len):
                    return None
                if max_len is not None and payload_len > int(max_len):
                    return None
                if min_len is not None or max_len is not None:
                    score += 1
            return score

        def _consume_definition(
            definitions: List[dict],
            *,
            selector: int,
            info_value: int,
            payload_len: Optional[int],
            min_raw: Optional[bytes],
            max_raw: Optional[bytes],
            step_raw: Optional[bytes],
            default_raw: Optional[bytes],
        ) -> Optional[dict]:
            best_def = None
            best_score = -1

            for definition in definitions:
                if not isinstance(definition, dict):
                    continue
                if definition.get("_used"):
                    continue

                score = 0
                expected_selector = definition.get("selector")
                if expected_selector is not None:
                    try:
                        if int(expected_selector) != selector:
                            continue
                        score += 5
                    except (TypeError, ValueError):
                        continue

                info_score = _match_get_info(info_value, definition)
                if info_score is None:
                    continue
                score += info_score

                length_score = _match_length(payload_len, definition)
                if length_score is None:
                    continue
                score += length_score

                if score <= 0:
                    continue

                if score > best_score:
                    best_score = score
                    best_def = definition

            if best_def is not None:
                best_def["_used"] = True
            return best_def

        for unit in self._units:
            controls = getattr(unit, "controls", []) or []
            if not controls:
                continue

            guid = ""
            quirk_map: Dict[str, dict] = {}
            quirk_definitions: List[dict] = []
            if isinstance(unit, ExtensionUnit):
                guid = unit.guid.lower()
                quirk_entry = self._quirks.get(guid, {})
                if isinstance(quirk_entry, dict):
                    quirk_controls = quirk_entry.get("controls", {})
                    if isinstance(quirk_controls, dict):
                        quirk_map = {str(k): v for k, v in quirk_controls.items()}
                    elif isinstance(quirk_controls, list):
                        for item in quirk_controls:
                            if isinstance(item, dict):
                                # Shallow copy so we can mark entries as used during matching.
                                quirk_definitions.append(dict(item))

            for control in controls:
                control_type = control.type
                info = vc_ctrl_get(
                    self._device,
                    self._interface,
                    control.unit_id,
                    control.selector,
                    GET_INFO,
                    1,
                )
                if not info or not info[0]:
                    continue

                length = _vc_get_len(self._device, self._interface, control.unit_id, control.selector) or 0
                length_hint = max(1, min(length or 4, 32))

                min_raw = vc_ctrl_get(
                    self._device,
                    self._interface,
                    control.unit_id,
                    control.selector,
                    GET_MIN,
                    length_hint,
                )
                max_raw = vc_ctrl_get(
                    self._device,
                    self._interface,
                    control.unit_id,
                    control.selector,
                    GET_MAX,
                    length_hint,
                )
                step_raw = vc_ctrl_get(
                    self._device,
                    self._interface,
                    control.unit_id,
                    control.selector,
                    GET_RES,
                    length_hint,
                )
                default_raw = vc_ctrl_get(
                    self._device,
                    self._interface,
                    control.unit_id,
                    control.selector,
                    GET_DEF,
                    length_hint,
                )
                payload_len = _payload_length(length, min_raw, default_raw)

                signed = _should_use_signed(min_raw, max_raw)

                name = control.name
                metadata: Dict[str, object] = {}

                if quirk_map:
                    override = quirk_map.get(str(control.selector))
                    if isinstance(override, dict):
                        override_name = override.get("name")
                        if override_name:
                            name = override_name
                        metadata = {k: v for k, v in override.items() if k != "name"}
                elif quirk_definitions:
                    matched = _consume_definition(
                        quirk_definitions,
                        selector=control.selector,
                        info_value=info[0],
                        payload_len=payload_len,
                        min_raw=min_raw,
                        max_raw=max_raw,
                        step_raw=step_raw,
                        default_raw=default_raw,
                    )
                    if matched:
                        override_name = matched.get("name")
                        if override_name:
                            name = override_name
                        override_type = matched.get("type")
                        if override_type:
                            control_type = str(override_type)
                        metadata = {
                            k: v
                            for k, v in matched.items()
                            if not k.startswith("_") and k != "name"
                        }

                metadata.setdefault("info_byte", info[0])
                if payload_len is not None and "payload_length" not in metadata:
                    metadata["payload_length"] = payload_len

                entry = ControlEntry(
                    interface_number=self._interface,
                    unit_id=control.unit_id,
                    selector=control.selector,
                    name=name,
                    type=control_type,
                    info=info[0],
                    minimum=_bytes_to_int(min_raw, signed=signed),
                    maximum=_bytes_to_int(max_raw, signed=signed),
                    step=_bytes_to_int(step_raw, signed=signed),
                    default=_bytes_to_int(default_raw, signed=signed),
                    length=length or (len(default_raw) if default_raw else len(min_raw) if min_raw else None),
                    raw_minimum=min_raw,
                    raw_maximum=max_raw,
                    raw_step=step_raw,
                    raw_default=default_raw,
                    metadata=metadata,
                )
                self._controls.append(entry)

    def get_controls(self) -> List[ControlEntry]:
        return list(self._controls)

__all__ = [
    "AltSettingInfo",
    "CapturedFrame",
    "FrameInfo",
    "StreamFormat",
    "StreamingInterface",
    "ControlEntry",
    "UVCControl",
    "UVCUnit",
    "ExtensionUnit",
    "UVCCamera",
    "FrameStream",
    "UVCError",
    "CodecPreference",
    "describe_device",
    "find_uvc_devices",
    "iter_video_streaming_interfaces",
    "list_streaming_interfaces",
    "list_control_units",
    "parse_vs_descriptors",
    "perform_probe_commit",
    "perform_still_probe_commit",
    "probe_streaming_interface",
    "select_format_and_frame",
    "resolve_stream_preference",
    "resolve_still_preference",
    "yuy2_to_rgb",
    "decode_to_rgb",
    "VS_FORMAT_UNCOMPRESSED",
    "REQ_TYPE_IN",
    "GET_CUR",
    "find_vc_interface_number",
    "claim_vc_interface",
    "vc_ctrl_get",
    "vc_ctrl_set",
    "read_vc_control_value",
    "write_vc_control_value",
    "UVCControlsManager",
    "load_quirks",
]
