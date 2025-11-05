"""Reusable UVC camera emulator logic for unit and integration tests."""

from __future__ import annotations

import itertools
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from libusb_uvc import (
    UVCControl,
    UVCUnit,
    VS_FORMAT_FRAME_BASED,
    VS_FORMAT_MJPEG,
    VS_FORMAT_UNCOMPRESSED,
)

# UVC request codes
GET_CUR = 0x81
GET_MIN = 0x82
GET_MAX = 0x83
GET_RES = 0x84
GET_LEN = 0x85
GET_INFO = 0x86
GET_DEF = 0x87
SET_CUR = 0x01

# Video streaming selectors (shifted into the high byte of wValue)
VS_PROBE_CONTROL = 0x01
VS_COMMIT_CONTROL = 0x02
VS_STILL_PROBE_CONTROL = 0x03
VS_STILL_COMMIT_CONTROL = 0x04
VS_STILL_IMAGE_TRIGGER_CONTROL = 0x05


def _le_bytes(value: int, length: int) -> bytes:
    return int(value).to_bytes(length, "little", signed=False)


@dataclass
class ControlDefinition:
    name: str
    unit_id: int
    selector: int
    length: int
    minimum: int
    maximum: int
    step: int
    default: int

    @classmethod
    def from_dict(cls, unit_id: int, data: Dict[str, object]) -> "ControlDefinition":
        return cls(
            name=str(data["name"]),
            unit_id=unit_id,
            selector=int(data["selector"]),
            length=int(data.get("length", 1)),
            minimum=int(data.get("min", 0)),
            maximum=int(data.get("max", 255)),
            step=int(data.get("step", 1)),
            default=int(data.get("default", 0)),
        )


@dataclass
class FrameDefinition:
    frame_index: int
    width: int
    height: int
    intervals: Sequence[int]


@dataclass
class StillFrameDefinition:
    frame_index: int
    width: int
    height: int
    compressions: Sequence[int]


@dataclass
class FormatDefinition:
    format_index: int
    subtype: str
    default_interval: int
    frames: List[FrameDefinition]
    still_frames: List[StillFrameDefinition]

    @property
    def uvc_subtype(self) -> int:
        mapping = {
            "mjpeg": VS_FORMAT_MJPEG,
            "mjpg": VS_FORMAT_MJPEG,
            "motion-jpeg": VS_FORMAT_MJPEG,
            "yuyv": VS_FORMAT_UNCOMPRESSED,
            "uyvy": VS_FORMAT_UNCOMPRESSED,
            "uncompressed": VS_FORMAT_UNCOMPRESSED,
            "h264": VS_FORMAT_FRAME_BASED,
            "frame-based": VS_FORMAT_FRAME_BASED,
        }
        key = self.subtype.lower()
        if key not in mapping:
            raise ValueError(f"Unsupported format subtype: {self.subtype}")
        return mapping[key]


class UvcEmulatorLogic:
    """Stateful simulation of a UVC camera based on a JSON profile."""

    def __init__(self, profile_path: Union[str, os.PathLike[str]]) -> None:
        profile_file = Path(profile_path)
        if not profile_file.exists():
            raise FileNotFoundError(profile_file)
        with profile_file.open("r", encoding="utf-8") as handle:
            self._profile = json.load(handle)

        self.device_info = self._profile.get("device", {})
        if not self.device_info:
            raise ValueError("Profile is missing top-level 'device' description")

        vc_block = self._profile.get("video_control", {})
        if not vc_block:
            raise ValueError("Profile is missing 'video_control' section")
        self.video_control_interface = int(vc_block.get("interface_number", 0))

        units: List[UVCUnit] = []
        self._control_defs: Dict[Tuple[int, int], ControlDefinition] = {}
        for unit in vc_block.get("units", []):
            unit_id = int(unit.get("unit_id", 0))
            unit_type = str(unit.get("type", "unit"))
            controls: List[UVCControl] = []
            for ctrl in unit.get("controls", []):
                ctrl_def = ControlDefinition.from_dict(unit_id, ctrl)
                self._control_defs[(unit_id, ctrl_def.selector)] = ctrl_def
                controls.append(
                    UVCControl(
                        unit_id=unit_id,
                        selector=ctrl_def.selector,
                        name=ctrl_def.name,
                        type=f"{unit_type.title()} Control",
                    )
                )
            if controls:
                units.append(UVCUnit(unit_id=unit_id, type=unit_type, controls=controls))
        self._control_units = units

        self._control_values: Dict[Tuple[int, int], int] = {
            key: definition.default for key, definition in self._control_defs.items()
        }

        vs_block = self._profile.get("video_streaming", {})
        if not vs_block:
            raise ValueError("Profile is missing 'video_streaming' section")
        self.video_streaming_interface = int(vs_block.get("interface_number", 1))

        self._formats: List[FormatDefinition] = []
        for fmt in vs_block.get("formats", []):
            frames = [
                FrameDefinition(
                    frame_index=int(frame["frame_index"]),
                    width=int(frame["width"]),
                    height=int(frame["height"]),
                    intervals=[int(v) for v in frame.get("intervals", [])],
                )
                for frame in fmt.get("frames", [])
            ]
            still_frames = [
                StillFrameDefinition(
                    frame_index=int(still["frame_index"]),
                    width=int(still["width"]),
                    height=int(still["height"]),
                    compressions=[int(v) for v in still.get("compressions", [1])],
                )
                for still in fmt.get("still_frames", [])
            ]
            self._formats.append(
                FormatDefinition(
                    format_index=int(fmt["format_index"]),
                    subtype=str(fmt.get("subtype", "mjpeg")),
                    default_interval=int(fmt.get("default_interval", 333333)),
                    frames=frames,
                    still_frames=still_frames,
                )
            )

        if not self._formats:
            raise ValueError("Profile does not define any streaming format")

        defaults = self._profile.get("streaming_defaults", {})
        self._max_payload = int(defaults.get("max_payload_transfer_size", 2048))
        self._max_frame_size = int(defaults.get("max_video_frame_size", 614400))

        first_format = self._formats[0]
        first_frame = first_format.frames[0]
        first_interval = first_frame.intervals[0] if first_frame.intervals else first_format.default_interval

        self._probe_payload = self._build_probe_payload(first_format, first_frame, first_interval)
        self._commit_payload = bytes(self._probe_payload)
        self._still_probe_payload = self._build_still_probe_payload(first_format.still_frames[0]) if first_format.still_frames else bytes([0] * 11)
        self._still_commit_payload = bytes(self._still_probe_payload)
        self._still_trigger_pending = False

        video_name = self._profile.get("video_file", "test_video.mjpeg")
        self._video_path = (profile_file.parent / video_name).resolve()
        if not self._video_path.exists():
            raise FileNotFoundError(self._video_path)
        self._video_frames = self._load_video_frames(self._video_path)
        self._frame_cycle = itertools.cycle(range(len(self._video_frames)))
        self._fid = 0
        self._pts_counter = 0

        ff_block = self._profile.get("functionfs", {})
        descriptors_name = ff_block.get("descriptors")
        strings_name = ff_block.get("strings")
        self.functionfs_descriptors: Optional[Path]
        self.functionfs_strings: Optional[Path]
        self.functionfs_descriptors = (
            (profile_file.parent / descriptors_name).resolve() if descriptors_name else None
        )
        self.functionfs_strings = (
            (profile_file.parent / strings_name).resolve() if strings_name else None
        )

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @property
    def control_units(self) -> List[UVCUnit]:
        return list(self._control_units)

    def get_control_value(self, unit_id: int, selector: int) -> int:
        key = (unit_id, selector)
        return self._control_values[key]

    def handle_ctrl_transfer(
        self,
        bmRequestType: int,
        bRequest: int,
        wValue: int,
        wIndex: int,
        data_or_length: Union[int, Sequence[int], bytes, bytearray, None],
        timeout: Optional[int] = None,
    ) -> Optional[bytes]:
        """Simulate a USB control transfer.

        Parameters mirror :func:`usb.core.Device.ctrl_transfer`. For OUT requests,
        ``data_or_length`` contains the payload. For IN requests it contains the
        maximum number of bytes to return.
        """

        direction_in = bool(bmRequestType & 0x80)
        length = int(data_or_length or 0) if direction_in else 0
        payload = bytes(data_or_length or b"") if not direction_in else b""

        selector = (wValue >> 8) & 0xFF
        interface_number = wIndex & 0xFF
        entity_id = (wIndex >> 8) & 0xFF if wIndex > 0xFF else 0

        # Video streaming requests are addressed to the streaming interface.
        if (
            interface_number == self.video_streaming_interface
            and selector
            in {
                VS_PROBE_CONTROL,
                VS_COMMIT_CONTROL,
                VS_STILL_PROBE_CONTROL,
                VS_STILL_COMMIT_CONTROL,
                VS_STILL_IMAGE_TRIGGER_CONTROL,
            }
        ):
            return self._handle_streaming_request(
                direction_in,
                bRequest,
                selector,
                interface_number,
                payload,
                length,
            )

        # Video control requests include the entity ID in wIndex.
        if entity_id:
            key = (entity_id, selector)
            if key not in self._control_defs:
                raise ValueError(f"Unknown control selector {selector:#04x} for unit {entity_id}")
            definition = self._control_defs[key]
            if bRequest == GET_LEN and direction_in:
                return _le_bytes(definition.length, min(length or 2, 2))
            if bRequest == GET_INFO and direction_in:
                return bytes([0x03])
            if bRequest == GET_RES and direction_in:
                return _le_bytes(definition.step, definition.length)
            if bRequest == GET_MIN and direction_in:
                return _le_bytes(definition.minimum, definition.length)
            if bRequest == GET_MAX and direction_in:
                return _le_bytes(definition.maximum, definition.length)
            if bRequest == GET_DEF and direction_in:
                return _le_bytes(definition.default, definition.length)
            if bRequest == GET_CUR and direction_in:
                value = self._control_values[key]
                return _le_bytes(value, definition.length)
            if bRequest == SET_CUR and not direction_in:
                if len(payload) != definition.length:
                    raise ValueError("Unexpected payload length for SET_CUR")
                value = int.from_bytes(payload, "little")
                value = max(definition.minimum, min(definition.maximum, value))
                self._control_values[key] = value
                return None

        if direction_in:
            # Graceful fallback: return zeroed buffer of requested length.
            return bytes(length)
        return None

    def get_next_video_packet(self) -> bytes:
        frame_index = next(self._frame_cycle)
        frame_payload = self._video_frames[frame_index]
        header = bytearray(12)
        header[0] = 12  # header length
        self._fid ^= 1
        flags = 0x02  # EOF
        flags |= self._fid & 0x01
        flags |= 0x04  # PTS present
        header[1] = flags
        self._pts_counter = (self._pts_counter + 333333) & 0xFFFFFFFF
        header[2:6] = _le_bytes(self._pts_counter, 4)
        # dwFrameInterval placeholder
        header[6:10] = _le_bytes(0, 4)
        header[10:12] = b"\x00\x00"
        return bytes(header) + frame_payload

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_probe_payload(
        self,
        fmt: FormatDefinition,
        frame: FrameDefinition,
        interval: int,
    ) -> bytes:
        payload = bytearray(34)
        payload[0:2] = _le_bytes(1, 2)  # bmHint
        payload[2] = fmt.format_index
        payload[3] = frame.frame_index
        payload[4:8] = _le_bytes(interval, 4)
        payload[18:22] = _le_bytes(self._max_frame_size, 4)
        payload[22:26] = _le_bytes(self._max_payload, 4)
        payload[26:30] = _le_bytes(48_000_000, 4)  # fake clock frequency
        payload[30] = 0  # bmFramingInfo
        payload[31] = 1  # preferred version
        payload[32] = 1
        payload[33] = 1
        return bytes(payload)

    def _build_still_probe_payload(self, still: StillFrameDefinition) -> bytes:
        payload = bytearray(11)
        payload[0] = 1  # format index placeholder
        payload[1] = still.frame_index
        first_compression = still.compressions[0] if still.compressions else 1
        payload[2] = first_compression
        payload[3:7] = _le_bytes(self._max_frame_size, 4)
        payload[7:11] = _le_bytes(self._max_payload, 4)
        return bytes(payload)

    def _load_video_frames(self, video_path: Path) -> List[bytes]:
        data = video_path.read_bytes()
        if data.startswith(b"\xff\xd8"):
            return [data]
        # Split on JPEG SOI markers if multiple frames are present.
        frames: List[bytes] = []
        start = 0
        for idx in range(1, len(data) - 1):
            if data[idx : idx + 2] == b"\xff\xd8":
                frames.append(data[start:idx])
                start = idx
        frames.append(data[start:])
        return [frame for frame in frames if frame]

    def _handle_streaming_request(
        self,
        direction_in: bool,
        bRequest: int,
        selector: int,
        interface_number: int,
        payload: bytes,
        length: int,
    ) -> Optional[bytes]:
        if interface_number != self.video_streaming_interface:
            raise ValueError("Streaming request addressed to unknown interface")

        if selector == VS_PROBE_CONTROL:
            if bRequest == GET_LEN and direction_in:
                return _le_bytes(len(self._probe_payload), min(length or 2, 2))
            if bRequest == GET_CUR and direction_in:
                return bytes(self._probe_payload)
            if bRequest == SET_CUR and not direction_in:
                self._probe_payload = bytes(payload)
                return None
            if bRequest == GET_DEF and direction_in:
                return bytes(self._probe_payload)
        elif selector == VS_COMMIT_CONTROL:
            if bRequest == GET_LEN and direction_in:
                return _le_bytes(len(self._commit_payload), min(length or 2, 2))
            if bRequest == GET_CUR and direction_in:
                return bytes(self._commit_payload)
            if bRequest == SET_CUR and not direction_in:
                self._commit_payload = bytes(payload)
                return None
        elif selector == VS_STILL_PROBE_CONTROL:
            if bRequest == GET_LEN and direction_in:
                return _le_bytes(len(self._still_probe_payload), min(length or 2, 2))
            if bRequest == GET_CUR and direction_in:
                return bytes(self._still_probe_payload)
            if bRequest == SET_CUR and not direction_in:
                self._still_probe_payload = bytes(payload)
                return None
        elif selector == VS_STILL_COMMIT_CONTROL:
            if bRequest == GET_LEN and direction_in:
                return _le_bytes(len(self._still_commit_payload), min(length or 2, 2))
            if bRequest == GET_CUR and direction_in:
                return bytes(self._still_commit_payload)
            if bRequest == SET_CUR and not direction_in:
                self._still_commit_payload = bytes(payload)
                return None
        elif selector == VS_STILL_IMAGE_TRIGGER_CONTROL:
            if bRequest == SET_CUR and not direction_in:
                self._still_trigger_pending = True
                return None
            if bRequest == GET_CUR and direction_in:
                return bytes([1 if self._still_trigger_pending else 0])

        if direction_in:
            return bytes(length)
        return None


__all__ = [
    "UvcEmulatorLogic",
    "VS_PROBE_CONTROL",
    "VS_COMMIT_CONTROL",
    "VS_STILL_PROBE_CONTROL",
    "VS_STILL_COMMIT_CONTROL",
    "VS_STILL_IMAGE_TRIGGER_CONTROL",
]
