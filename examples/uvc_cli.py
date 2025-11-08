"""Shared helpers for libusb_uvc example scripts."""

from __future__ import annotations

import argparse
import importlib
import logging
import pathlib
import sys
from typing import List, Optional, Tuple

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"


def ensure_repo_import(package: str = "libusb_uvc") -> None:
    """Ensure the editable checkout is importable before importing *package*."""

    if package in sys.modules:
        return

    try:
        importlib.import_module(package)
        return
    except ImportError:
        pass

    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))

    importlib.import_module(package)


def parse_usb_id(value: str, *, prefer_hex: bool = False) -> int:
    """Parse a string as a 16-bit USB identifier, optionally preferring hex."""

    token = value.strip()
    token_lower = token.lower()

    def _parse_hex(raw: str) -> int:
        raw = raw.lower()
        if raw.startswith("0x"):
            raw = raw[2:]
        if not raw or any(ch not in "0123456789abcdef" for ch in raw):
            raise argparse.ArgumentTypeError(f"Invalid USB identifier: {value!r}")
        return int(raw, 16)

    try:
        parsed = int(token, 0)
    except ValueError:
        parsed = _parse_hex(token_lower)
    else:
        if prefer_hex and token_lower.isdigit():
            parsed = int(token_lower, 16)

    if not 0 <= parsed <= 0xFFFF:
        raise argparse.ArgumentTypeError("USB identifiers must be between 0x0000 and 0xFFFF")
    return parsed


def parse_device_id(value: str) -> Tuple[int, int]:
    """Parse VID:PID pairs (hex or decimal)."""

    parts = value.split(":")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("Device ID must be in VID:PID format")
    vid = parse_usb_id(parts[0], prefer_hex=True)
    pid = parse_usb_id(parts[1], prefer_hex=True)
    return vid, pid


def parse_device_path(value: str) -> Tuple[int, Tuple[int, ...]]:
    """Parse 'bus:port[.port...]' into (bus, (ports...))."""

    if ":" not in value:
        raise argparse.ArgumentTypeError("Device path must be BUS:PORT[.PORT...]")
    bus_str, path_str = value.split(":", 1)
    try:
        bus = int(bus_str, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid bus number: {bus_str!r}") from exc
    if bus < 0:
        raise argparse.ArgumentTypeError("Bus number must be positive")
    segments = [seg for seg in path_str.replace("-", ".").split(".") if seg]
    if not segments:
        raise argparse.ArgumentTypeError("Device path must include at least one port number")
    ports: List[int] = []
    for seg in segments:
        try:
            port = int(seg, 0)
        except ValueError as exc:  # pragma: no cover - defensive
            raise argparse.ArgumentTypeError(f"Invalid port number: {seg!r}") from exc
        if port < 0:
            raise argparse.ArgumentTypeError("Port numbers must be positive")
        ports.append(port)
    return bus, tuple(ports)


def add_device_arguments(parser: argparse.ArgumentParser, *, default_index: Optional[int] = 0) -> None:
    """Add common VID/PID/interface selectors to *parser*."""

    parser.add_argument("--vid", type=parse_usb_id, help="Vendor ID filter (decimal or 0x-prefixed)")
    parser.add_argument("--pid", type=parse_usb_id, help="Product ID filter (decimal or 0x-prefixed)")
    parser.add_argument(
        "--device-id",
        type=parse_device_id,
        help="Combined VID:PID filter (hex or decimal, e.g. 0x0408:0x5473)",
    )
    parser.add_argument("--device-sn", dest="device_sn", help="Exact USB serial number to match")
    parser.add_argument(
        "--device-path",
        type=parse_device_path,
        help="Physical USB path as bus:port[.port...] (e.g. 3:2.1)",
    )
    parser.add_argument(
        "--device-index",
        type=int,
        default=default_index,
        help="Index within the detected device list",
    )
    parser.add_argument("--interface", type=int, default=1, help="Video streaming interface number")


def configure_logging(level: str = "INFO", *, name: Optional[str] = None) -> logging.Logger:
    """Initialise basic logging and return the requested logger."""

    logging.basicConfig(level=level.upper())
    return logging.getLogger(name) if name else logging.getLogger()


def apply_device_filters(args) -> None:
    """Normalise parsed args so --device-id populates --vid/--pid."""

    device_id = getattr(args, "device_id", None)
    if device_id is None:
        return
    vid, pid = device_id
    args.vid = vid
    args.pid = pid


def resolve_device_index(args) -> None:
    """Resolve device selection flags into a concrete device index."""

    target_sn = getattr(args, "device_sn", None)
    target_path = getattr(args, "device_path", None)
    identity_filters = bool(target_sn or target_path)
    has_vid_pid = args.vid is not None or args.pid is not None

    # Nothing to do if the caller explicitly set an index and provided no extra identity filters.
    if args.device_index is not None and not identity_filters:
        return

    if not has_vid_pid:
        # Without VID/PID we cannot filter deterministically; leave selection to device_index.
        return

    try:
        from libusb_uvc import find_uvc_devices
        import usb.util
    except Exception as exc:  # pragma: no cover - import guard
        raise RuntimeError("libusb_uvc must be importable before resolving device identity") from exc

    devices = find_uvc_devices(args.vid, args.pid)
    if not devices:
        raise RuntimeError("No devices found for the requested VID/PID")

    if not identity_filters:
        if len(devices) == 1:
            args.device_index = 0
            return
        raise RuntimeError(
            "Multiple devices share this VID/PID; supply --device-index, --device-sn or --device-path"
        )

    bus_expected = None
    ports_expected: Optional[Tuple[int, ...]] = None
    if target_path is not None:
        bus_expected, ports_expected = target_path

    matches: List[int] = []
    for index, dev in enumerate(devices):
        if target_sn:
            serial = None
            try:
                if dev.iSerialNumber:
                    serial = usb.util.get_string(dev, dev.iSerialNumber)
            except Exception:
                serial = None
            if serial != target_sn:
                continue

        if target_path is not None:
            if getattr(dev, "bus", None) != bus_expected:
                continue
            ports = getattr(dev, "port_numbers", None)
            if ports is None:
                single = getattr(dev, "port_number", None)
                if single is not None:
                    ports = (single,)
            if ports is None or tuple(ports) != ports_expected:
                continue

        matches.append(index)

    if not matches:
        raise RuntimeError("No device matched the requested serial/path filters")
    if len(matches) > 1:
        raise RuntimeError("Serial/path filters matched multiple devices")

    args.device_index = matches[0]
