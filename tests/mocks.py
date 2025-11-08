"""Mock helpers backed by :mod:`tests.uvc_emulator` for unit tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .uvc_emulator import UvcEmulatorLogic


@dataclass
class _MockEndpoint:
    bEndpointAddress: int
    bmAttributes: int
    wMaxPacketSize: int


class _MockInterface:
    def __init__(
        self,
        interface_number: int,
        alternate_setting: int,
        endpoints: List[_MockEndpoint],
        extra_descriptors: bytes = b"",
        interface_class: int = 14,
        interface_subclass: int = 2,
    ) -> None:
        self.bInterfaceNumber = interface_number
        self.bAlternateSetting = alternate_setting
        self.bInterfaceClass = interface_class
        self.bInterfaceSubClass = interface_subclass
        self.bNumEndpoints = len(endpoints)
        self._endpoints = endpoints
        self.extra_descriptors = extra_descriptors
        self.interface_number = interface_number

    def __iter__(self):
        return iter(self._endpoints)

    def __getitem__(self, index: int) -> _MockEndpoint:
        return self._endpoints[index]


class _MockConfiguration:
    def __init__(self, interfaces: List[_MockInterface]):
        self._interfaces = interfaces
        self.bConfigurationValue = 1

    def __iter__(self):
        return iter(self._interfaces)


class _AltSetting:
    def __init__(self, alternate_setting: int, endpoint) -> None:
        self.alternate_setting = alternate_setting
        self.endpoint_address = endpoint.bEndpointAddress if endpoint else None
        self.endpoint_attributes = endpoint.bmAttributes if endpoint else None
        self.max_packet_size = endpoint.wMaxPacketSize if endpoint else 0

    def is_isochronous(self) -> bool:
        return True


class StreamingInterfaceAdapter:
    """Adapter exposing the attributes expected by :class:`UVCCamera`."""

    def __init__(self, interface: _MockInterface, extra_formats):
        self._interface = interface
        self.interface_number = interface.interface_number
        self.formats = list(extra_formats)
        endpoint = interface._endpoints[0] if interface._endpoints else None
        self.alt_settings = [_AltSetting(interface.bAlternateSetting, endpoint)]

    def get_alt(self, alternate_setting: int):
        for alt in self.alt_settings:
            if alt.alternate_setting == alternate_setting:
                return alt
        return None

    def select_alt_for_payload(self, required_payload: int):
        return self.alt_settings[0]

    def find_frame(
        self,
        width: int,
        height: int,
        *,
        format_index: Optional[int] = None,
        subtype: Optional[int] = None,
    ):
        for fmt in self.formats:
            if format_index is not None and fmt.format_index != format_index:
                continue
            if subtype is not None and fmt.subtype != subtype:
                continue
            for frame in fmt.frames:
                if frame.width == width and frame.height == height:
                    return fmt, frame
        return None


class MockUsbDevice:
    """Drop-in replacement for :class:`usb.core.Device` backed by the emulator."""

    def __init__(self, emulator: UvcEmulatorLogic) -> None:
        self._emulator = emulator
        device_info = emulator.device_info
        self.idVendor = int(device_info.get("vendor_id", 0))
        self.idProduct = int(device_info.get("product_id", 0))
        self.bus = 1
        self.address = 1
        self._log: List[dict] = []

        streaming_interface = emulator.video_streaming_interface
        # Minimal endpoint description (ISO IN endpoint 0x81)
        self._configurations = [
            _MockConfiguration(
                interfaces=[
                    _MockInterface(
                        interface_number=streaming_interface,
                        alternate_setting=0,
                        endpoints=[
                            _MockEndpoint(
                                bEndpointAddress=0x81,
                                bmAttributes=0x01,
                                wMaxPacketSize=2048,
                            )
                        ],
                        extra_descriptors=b"",
                    )
                ]
            )
        ]
        self._claimed_interfaces = set()

        class _DummyContext:
            def __init__(self, owner) -> None:
                self._owner = owner

            def managed_claim_interface(self, device, interface):
                self._owner._claimed_interfaces.add(interface)

            def managed_release_interface(self, device, interface):
                self._owner._claimed_interfaces.discard(interface)

        self._ctx = _DummyContext(self)

    def __iter__(self):
        return iter(self._configurations)

    def set_configuration(self, *args, **kwargs):  # pragma: no cover - trivial
        return None

    # ------------------------------------------------------------------
    # PyUSB facade
    # ------------------------------------------------------------------

    def ctrl_transfer(
        self,
        bmRequestType: int,
        bRequest: int,
        wValue: int = 0,
        wIndex: int = 0,
        data_or_length: Optional[object] = None,
        timeout: Optional[int] = None,
    ):
        self._log.append(
            {
                "bmRequestType": bmRequestType,
                "bRequest": bRequest,
                "wValue": wValue,
                "wIndex": wIndex,
                "data": data_or_length,
            }
        )
        result = self._emulator.handle_ctrl_transfer(
            bmRequestType,
            bRequest,
            wValue,
            wIndex,
            data_or_length,
            timeout=timeout,
        )
        if result is None:
            return None
        return result

    # ------------------------------------------------------------------
    # Kernel driver helpers (no-op)
    # ------------------------------------------------------------------

    def is_kernel_driver_active(self, interface: int) -> bool:
        return False

    def detach_kernel_driver(self, interface: int) -> None:
        return None

    def attach_kernel_driver(self, interface: int) -> None:
        return None

    def release_interface(self, interface: int) -> None:
        self._ctx.managed_release_interface(self, interface)

    def set_interface_altsetting(self, interface: int, alternate_setting: int) -> None:
        return None

    def clear_halt(self, endpoint: int) -> None:
        return None

    def read(self, endpoint: int, size: int, timeout: Optional[int] = None):
        packet = self._emulator.get_next_video_packet()
        return packet[:size]

    @property
    def log(self) -> List[dict]:
        return list(self._log)
