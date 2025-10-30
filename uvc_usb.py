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
import logging
import threading
import time
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple

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


_LIBUSB_HOTPLUG_DISABLED = False
_LIBUSB_HOTPLUG_ATTEMPTED = False


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

UVC_CLASS = 0x0E
VS_SUBCLASS = 0x02
CS_INTERFACE = 0x24

VC_HEADER = 0x01
VC_INPUT_TERMINAL = 0x02
VC_OUTPUT_TERMINAL = 0x03
VC_SELECTOR_UNIT = 0x04
VC_PROCESSING_UNIT = 0x05
VC_EXTENSION_UNIT = 0x06

VC_POWER_MODE_CONTROL = 0x01

BH_FID = 0x01
BH_EOF = 0x02
BH_PTS = 0x04
BH_SCR = 0x08
BH_RES = 0x10
BH_STI = 0x20
BH_ERR = 0x40
BH_EOH = 0x80

# VideoStreaming descriptor subtypes (UVC 1.5 specification §3.9)
VS_INPUT_HEADER = 0x01
VS_FORMAT_UNCOMPRESSED = 0x04
VS_FRAME_UNCOMPRESSED = 0x05
VS_FORMAT_MJPEG = 0x06
VS_FRAME_MJPEG = 0x07
VS_FORMAT_FRAME_BASED = 0x10
VS_FRAME_FRAME_BASED = 0x11

# VideoStreaming control selectors
VS_PROBE_CONTROL = 0x01
VS_COMMIT_CONTROL = 0x02

# USB class-specific requests
SET_CUR = 0x01
GET_CUR = 0x81
GET_LEN = 0x85
GET_DEF = 0x87

# Pre-computed request types used for control transfers on interfaces
REQ_TYPE_IN = usb.util.build_request_type(
    usb.util.CTRL_IN, usb.util.CTRL_TYPE_CLASS, usb.util.CTRL_RECIPIENT_INTERFACE
)
REQ_TYPE_OUT = usb.util.build_request_type(
    usb.util.CTRL_OUT, usb.util.CTRL_TYPE_CLASS, usb.util.CTRL_RECIPIENT_INTERFACE
)


class UVCError(RuntimeError):
    """Raised when the camera reports unexpected errors."""


@dataclasses.dataclass
class FrameInfo:
    """Frame descriptor summary collected from a VS frame descriptor."""

    frame_index: int
    width: int
    height: int
    default_interval: int
    intervals_100ns: List[int]
    max_frame_size: int

    def intervals_hz(self) -> List[float]:
        """Return unique frame rates advertised for this frame."""

        unique = sorted({v for v in self.intervals_100ns if v})
        return [_interval_to_hz(v) for v in unique]

    def pick_interval(
        self,
        target_fps: Optional[float],
        *,
        strict: bool = False,
        tolerance_hz: float = 1e-3,
    ) -> int:
        """Pick the advertised frame interval closest to ``target_fps``.

        ``strict`` forces the chosen interval to match the requested frame rate
        within ``tolerance_hz``; otherwise the nearest interval is returned.
        """

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
        """Return the first alt whose packet size covers ``required_payload``."""

        candidates = [alt for alt in self.alt_settings if alt.max_packet_size]
        if not candidates:
            return None

        # Prefer the smallest alt that satisfies the payload requirement to
        # avoid monopolising USB bandwidth unnecessarily.
        for alt in sorted(candidates, key=lambda a: a.max_packet_size):
            if alt.max_packet_size >= required_payload:
                return alt
        return max(candidates, key=lambda a: a.max_packet_size)

    def find_frame(
        self,
        width: int,
        height: int,
        *,
        format_index: Optional[int] = None,
        subtype: Optional[int] = None,
    ) -> Optional[Tuple[StreamFormat, FrameInfo]]:
        """Return the first (format, frame) matching the requested geometry."""

        for fmt in self.formats:
            if format_index is not None and fmt.format_index != format_index:
                continue
            if subtype is not None and fmt.subtype != subtype:
                continue
            for frame in fmt.frames:
                if frame.width == width and frame.height == height:
                    return fmt, frame
        return None


@dataclasses.dataclass
class CapturedFrame:
    """Container returned by :meth:`UVCCamera.read_frame`."""

    payload: bytes
    format: StreamFormat
    frame: FrameInfo
    fid: int
    pts: Optional[int]


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


def list_streaming_interfaces(dev: usb.core.Device) -> Dict[int, StreamingInterface]:
    """Build :class:`StreamingInterface` descriptions for *dev*."""

    interfaces: Dict[int, StreamingInterface] = {}
    for cfg in dev:
        for intf in cfg:
            if intf.bInterfaceClass != UVC_CLASS or intf.bInterfaceSubClass != VS_SUBCLASS:
                continue

            info = interfaces.setdefault(
                intf.bInterfaceNumber,
                StreamingInterface(interface_number=intf.bInterfaceNumber),
            )

            # Alternate settings expose the same interface number but with
            # different endpoint bandwidth.  We record every variant.
            endpoint_address = None
            endpoint_attributes = None
            max_packet_size = 0
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

            # Alternate settings other than zero rarely duplicate the class-
            # specific descriptors, so we only parse them once.
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
    )


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
) -> dict:
    """Send VS_PROBE (and optionally VS_COMMIT) using the provided selection."""
    template = _read_control(dev, GET_CUR, VS_PROBE_CONTROL, interface_number, length)
    source = "GET_CUR"
    if template is None:
        template = _read_control(dev, GET_DEF, VS_PROBE_CONTROL, interface_number, length)
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
            "VS_PROBE SET_CUR len=%s bmHint=%s fmt=%s frame=%s interval=%s payload=%s",
            length,
            effective_hint,
            stream_format.format_index,
            frame.frame_index,
            candidate_interval,
            _hex_dump(payload),
        )
        _write_control(dev, SET_CUR, VS_PROBE_CONTROL, interface_number, payload)
    except usb.core.USBError as exc:
        LOG.debug(
            "VS_PROBE SET_CUR failed errno=%s payload=%s",
            getattr(exc, "errno", None),
            _hex_dump(payload),
        )
        raise
    negotiated = _read_control(dev, GET_CUR, VS_PROBE_CONTROL, interface_number, length)
    if negotiated is None:
        negotiated_bytes = bytes(payload)
    else:
        negotiated_bytes = bytes(negotiated)
    LOG.debug("VS_PROBE GET_CUR payload=%s", _hex_dump(negotiated_bytes))

    negotiation_info = _parse_probe_payload(negotiated_bytes)

    if do_commit:
        try:
            LOG.debug("VS_COMMIT SET_CUR payload=%s", _hex_dump(negotiated_bytes))
            _write_control(dev, SET_CUR, VS_COMMIT_CONTROL, interface_number, negotiated_bytes)
        except usb.core.USBError as exc:
            LOG.debug(
                "VS_COMMIT SET_CUR failed errno=%s payload=%s",
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



from uvc_async import IsoConfig, UVCPacketStream, InterruptConfig, InterruptListener
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

        self._committed_frame_interval: Optional[int] = None
        self._committed_payload: Optional[int] = None
        self._committed_frame_size: Optional[int] = None
        self._committed_format_index: Optional[int] = None
        self._committed_frame_index: Optional[int] = None

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

    def _run_libusb_probe_commit(self, handle: usb1.USBDeviceHandle) -> None:
        """Perform a full, robust PROBE/COMMIT sequence using a libusb1 handle."""
        if self._format is None or self._frame is None:
            raise UVCError("Stream not configured; call configure_stream() first")

        length = 34  # Use the length that is known to work for most devices.
        timeout = 1000
        req_in = usb1.TYPE_CLASS | usb1.RECIPIENT_INTERFACE | usb1.ENDPOINT_IN
        req_out = usb1.TYPE_CLASS | usb1.RECIPIENT_INTERFACE | usb1.ENDPOINT_OUT

        # 1. Get a template buffer from the device (GET_CUR is preferred)
        try:
            template = handle.controlRead(
                req_in, GET_CUR, VS_PROBE_CONTROL << 8, self.interface_number, length, timeout
            )
            LOG.debug("libusb1 PROBE template from GET_CUR: %s", template.hex())
        except usb1.USBError:
            LOG.debug("libusb1 PROBE GET_CUR failed, using zeroed buffer")
            template = bytes(length)

        buf = bytearray(template)

        # 2. Patch the template with our desired streaming parameters.
        # Only touch the fields necessary for negotiation.
        interval = self._committed_frame_interval or self._frame.pick_interval(None)

        bm_hint = 1  # dwFrameInterval is valid
        buf[0:2] = bm_hint.to_bytes(2, "little")
        buf[2] = self._committed_format_index or self._format.format_index
        buf[3] = self._committed_frame_index or self._frame.frame_index
        buf[4:8] = int(interval or 0).to_bytes(4, "little")

        # 3. PROBE: Send the desired parameters to the device.
        LOG.debug("libusb1 PROBE SET_CUR: %s", bytes(buf).hex())
        handle.controlWrite(
            req_out, SET_CUR, VS_PROBE_CONTROL << 8, self.interface_number, bytes(buf), timeout
        )

        # 4. Read back the negotiated parameters. The device may have adjusted them.
        negotiated = bytes(handle.controlRead(
            req_in, GET_CUR, VS_PROBE_CONTROL << 8, self.interface_number, length, timeout
        ))
        LOG.debug("libusb1 PROBE GET_CUR (negotiated): %s", negotiated.hex())

        # 5. COMMIT: Send the final negotiated parameters back to commit the stream.
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
        LOG.info("Async stream stopped")

    # ------------------------------------------------------------------
    # Frame capture
    # ------------------------------------------------------------------

    def read_frame(self, timeout_ms: int = 1000) -> CapturedFrame:
        """Read a single video frame from the streaming endpoint."""

        if not self._claimed or self._endpoint_address is None or self._max_payload is None:
            raise UVCError("Stream not configured; call configure_stream() first")

        expected_size = self._frame.max_frame_size if self._frame else None
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
                    and expected_size is not None
                    and len(frame_bytes) == expected_size
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


def decode_to_rgb(payload: bytes, stream_format: StreamFormat, frame: FrameInfo):
    """Convert a raw payload into an RGB image (numpy array).

    Supports YUY2/YUYV and MJPEG.  Raises :class:`RuntimeError` if decoding is
    not possible due to missing dependencies (e.g. OpenCV for MJPEG).
    """

    name = stream_format.description.upper()
    if "YUY" in name or stream_format.subtype == VS_FORMAT_UNCOMPRESSED:
        return yuy2_to_rgb(payload, frame.width, frame.height)

    if stream_format.subtype == VS_FORMAT_MJPEG or "MJPG" in name:
        try:
            import cv2
            import numpy as np
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("OpenCV required for MJPEG decoding") from exc

        arr = np.frombuffer(payload, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None:
            raise RuntimeError("Failed to decode MJPEG frame")
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


__all__ = [
    "AltSettingInfo",
    "CapturedFrame",
    "FrameInfo",
    "StreamFormat",
    "StreamingInterface",
    "UVCCamera",
    "UVCError",
    "CodecPreference",
    "describe_device",
    "find_uvc_devices",
    "iter_video_streaming_interfaces",
    "list_streaming_interfaces",
    "parse_vs_descriptors",
    "perform_probe_commit",
    "probe_streaming_interface",
    "select_format_and_frame",
    "resolve_stream_preference",
    "yuy2_to_rgb",
    "decode_to_rgb",
    "VS_FORMAT_UNCOMPRESSED",
    "REQ_TYPE_IN",
    "GET_CUR",
]
# VideoControl request selectors
VC_POWER_MODE_CONTROL = 0x01
