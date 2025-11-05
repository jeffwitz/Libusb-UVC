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

    def __iter__(self):
        return iter(self._endpoints)

    def __getitem__(self, index: int) -> _MockEndpoint:
        return self._endpoints[index]


class _MockConfiguration:
    def __init__(self, interfaces: List[_MockInterface]):
        self._interfaces = interfaces

    def __iter__(self):
        return iter(self._interfaces)


class MockUsbDevice:
    """Drop-in replacement for :class:`usb.core.Device` backed by the emulator."""

    def __init__(self, emulator: UvcEmulatorLogic) -> None:
        self._emulator = emulator
        device_info = emulator.device_info
        self.idVendor = int(device_info.get("vendor_id", 0))
        self.idProduct = int(device_info.get("product_id", 0))
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

    def __iter__(self):
        return iter(self._configurations)

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

    @property
    def log(self) -> List[dict]:
        return list(self._log)
