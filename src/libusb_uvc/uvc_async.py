#!/usr/bin/env python3
"""Asynchronous UVC packet reader built on top of libusb1.

The default PyUSB API only exposes synchronous ``read`` calls which block per
packet. For high-bandwidth YUYV streams the camera quickly overflows unless we
keep several URBs in flight.  ``libusb1`` exposes the necessary primitives to
submit multiple transfers and to inspect each ISO packet individually.  This
module provides a tiny wrapper around that API so the rest of the project can
consume decoded packets without worrying about the USB plumbing.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import queue
import time
from dataclasses import dataclass
from typing import Callable, Optional

import usb1

LOG = logging.getLogger(__name__)


@dataclass
class IsoConfig:
    endpoint: int
    packet_size: int
    transfers: int = 8
    packets_per_transfer: int = 32
    timeout_ms: int = 1000


@dataclass
class InterruptConfig:
    endpoint: int
    packet_size: int
    timeout_ms: int = 1000


class InterruptListener:
    """Simple interrupt-IN listener for VC notifications."""

    def __init__(
        self,
        context: usb1.USBContext,
        handle: usb1.USBDeviceHandle,
        config: InterruptConfig,
        callback: Optional[Callable[[bytes], None]] = None,
    ) -> None:
        self._ctx = context
        self._handle = handle
        self._config = config
        self._callback = callback
        self._transfer: Optional[usb1.USBTransfer] = None
        self._buffer: Optional[ctypes.Array] = None
        self._active = False

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        transfer = self._handle.getTransfer()
        buffer = ctypes.create_string_buffer(self._config.packet_size)
        transfer.setInterrupt(
            self._config.endpoint,
            buffer,
            callback=self._on_transfer,
            timeout=self._config.timeout_ms,
        )
        transfer.submit()
        self._transfer = transfer
        self._buffer = buffer

    def stop(self) -> None:
        if not self._active:
            return
        self._active = False
        if self._transfer is not None:
            with contextlib.suppress(usb1.USBError):
                if self._transfer.isSubmitted():
                    self._transfer.cancel()
        self._transfer = None
        self._buffer = None

    def _on_transfer(self, transfer: usb1.USBTransfer) -> None:
        if not self._active:
            return

        status = transfer.getStatus()
        if status == usb1.TRANSFER_COMPLETED:
            try:
                data = bytes(transfer.getBuffer()[: self._config.packet_size])
            except AttributeError:
                data = b""
            if data and self._callback is not None:
                self._callback(data)
        elif status == usb1.TRANSFER_CANCELLED:
            return
        else:
            LOG.debug("VC interrupt transfer status=%d", status)

        try:
            transfer.submit()
        except usb1.USBError as exc:
            LOG.warning("Failed to resubmit VC interrupt transfer: %s", exc)
            self.stop()


class UVCPacketStream:
    """Manage an asynchronous ISO stream, invoking a callback per packet.

    The callback receives the raw bytes of *each* isochronous packet (including
    UVC headers).  The consumer is responsible for parsing the UVC headers and
    reassembling complete frames.
    """

    def __init__(
        self,
        context: usb1.USBContext,
        handle: usb1.USBDeviceHandle,
        config: IsoConfig,
        callback: Callable[[bytes], None],
    ) -> None:
        self._ctx = context
        self._handle = handle
        self._config = config
        self._callback = callback
        self._transfers = []
        self._active = False
        self._resubmit_queue: queue.Queue[Optional[usb1.USBTransfer]] = queue.Queue()

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        LOG.debug(
            "Starting async stream with %s transfers", self._config.transfers
        )
        self._resubmit_queue = queue.Queue()

        for index in range(self._config.transfers):
            transfer = self._handle.getTransfer(self._config.packets_per_transfer)
            buffer = ctypes.create_string_buffer(
                self._config.packet_size * self._config.packets_per_transfer
            )
            lengths = [self._config.packet_size] * self._config.packets_per_transfer
            transfer.setIsochronous(
                self._config.endpoint,
                buffer,
                callback=self._on_transfer,
                timeout=self._config.timeout_ms,
                iso_transfer_length_list=lengths,
                user_data=index,
            )
            LOG.debug("Submitting initial transfer #%s", index)
            transfer.submit()
            self._transfers.append((transfer, buffer))

    def stop(self) -> None:
        if not self._active:
            LOG.debug("Async stream already stopped")
            return

        LOG.info("Stopping async stream - cancelling %d transfers", len(self._transfers))
        self._active = False

        with contextlib.suppress(queue.Full):
            self._resubmit_queue.put_nowait(None)

        cancelled_count = 0
        for transfer, _ in self._transfers:
            try:
                if transfer.isSubmitted():
                    LOG.debug("Cancelling transfer #%s", transfer.getUserData())
                    transfer.cancel()
                    cancelled_count += 1
            except usb1.USBError as exc:
                LOG.warning("Error cancelling transfer #%s: %s", transfer.getUserData(), exc)

        if cancelled_count > 0:
            LOG.info("Cancelled %d transfers. Allowing a moment for cleanup.", cancelled_count)
            # A short, non-blocking sleep is safer than a blocking event loop here,
            # as it prevents deadlocks if other threads are also interacting with libusb.
            time.sleep(0.2)

        self._transfers.clear()
        LOG.info("Async stream stop sequence initiated.")


    def _on_transfer(self, transfer: usb1.USBTransfer) -> None:
        transfer_id = transfer.getUserData()

        if not self._active:
            LOG.debug("Transfer #%s callback ignored (inactive)", transfer_id)
            return

        status = transfer.getStatus()

        if status == usb1.TRANSFER_CANCELLED:
            LOG.debug("Transfer #%s cancelled", transfer_id)
            return

        LOG.debug("Callback for transfer #%s status=%d", transfer_id, status)

        if status == usb1.TRANSFER_STALL:
            LOG.warning("Transfer #%s stalled; clearing halt", transfer_id)
            try:
                self._handle.clearHalt(self._config.endpoint)
            except usb1.USBError as exc:
                LOG.error("Failed to clear halt: %s", exc)
                self.stop()
                return
        elif status == usb1.TRANSFER_NO_DEVICE:
            LOG.error("Device disconnected during transfer #%s", transfer_id)
            self.stop()
            return
        elif status not in (usb1.TRANSFER_COMPLETED, usb1.TRANSFER_TIMED_OUT):
            LOG.warning("Transfer #%s unexpected status=%d", transfer_id, status)

        try:
            buffer = transfer.getBuffer()
        except AttributeError:
            buffer = None

        setup_list = []
        try:
            setup_list = transfer.getISOSetupList()
        except AttributeError:
            setup_list = []

        data_received = False

        if setup_list and buffer is not None:
            packet_size = self._config.packet_size
            for index, setup in enumerate(setup_list):
                actual = setup.get('actual_length', 0)
                if actual and actual > 0:
                    start = index * packet_size
                    end = start + actual
                    self._callback(bytes(buffer[start:end]))
                    data_received = True
        else:
            try:
                buffers = transfer.getISOBufferList()
            except AttributeError:
                buffers = None

            if buffers:
                for chunk in buffers:
                    data = bytes(chunk)
                    if data:
                        self._callback(data)
                        data_received = True
            elif buffer is not None:
                packet_size = self._config.packet_size
                total = packet_size * self._config.packets_per_transfer
                for start in range(0, total, packet_size):
                    data = buffer[start : start + packet_size]
                    if data:
                        self._callback(bytes(data))
                        data_received = True

        if not data_received:
            LOG.debug("Transfer #%s completed with no data", transfer_id)

        if self._active:
            self._resubmit_queue.put(transfer)

    def handle_events_and_resubmit(self, timeout_us: int) -> None:
        if not self._active:
            return

        try:
            self._ctx.handleEventsTimeout(timeout_us)
        except usb1.USBError as exc:
            LOG.error("USB event handling failed: %s", exc)
            self.stop()
            return

        while True:
            try:
                transfer = self._resubmit_queue.get_nowait()
            except queue.Empty:
                break

            if transfer is None:
                break

            if not self._active:
                continue

            try:
                LOG.debug(
                    "Re-submitting transfer #%s after queue", transfer.getUserData()
                )
                transfer.submit()
            except usb1.USBError as exc:
                LOG.error(
                    "Failed to resubmit transfer #%s: %s",
                    transfer.getUserData(),
                    exc,
                )
                self.stop()
                break

    def is_active(self) -> bool:
        return self._active

__all__ = ["IsoConfig", "InterruptConfig", "InterruptListener", "UVCPacketStream"]
