from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterator

import pytest

from libusb_uvc import CodecPreference, UVCCamera, find_uvc_devices

from .uvc_emulator import UvcEmulatorLogic

PROFILE_PATH = Path(__file__).parent / "data" / "sample_camera_profile.json"


def _wait_for_device(vendor_id: int, product_id: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        devices = find_uvc_devices(vendor_id, product_id)
        if devices:
            return True
        time.sleep(0.25)
    return False


@pytest.fixture(scope="module")
def gadget_daemon() -> Iterator[Dict[str, int]]:
    if os.environ.get("LIBUSB_UVC_ENABLE_GADGET_TESTS") != "1":
        pytest.skip("USB gadget tests are disabled. Set LIBUSB_UVC_ENABLE_GADGET_TESTS=1 to enable.")

    mountpoint = Path(os.environ.get("LIBUSB_UVC_FFS_PATH", "/dev/ffs/uvc"))
    if not mountpoint.exists():
        pytest.skip(f"FunctionFS mount point {mountpoint} not present")

    emulator = UvcEmulatorLogic(PROFILE_PATH)
    cmd = [
        sys.executable,
        str(Path(__file__).parent / "gadget_daemon.py"),
        str(PROFILE_PATH),
        "--mountpoint",
        str(mountpoint),
        "--log-level",
        os.environ.get("LIBUSB_UVC_GADGET_LOG", "INFO"),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    try:
        if not _wait_for_device(emulator.device_info.get("vendor_id", 0), emulator.device_info.get("product_id", 0)):
            pytest.skip("Timed out waiting for the virtual UVC device to appear")
        yield {
            "vendor_id": int(emulator.device_info.get("vendor_id", 0)),
            "product_id": int(emulator.device_info.get("product_id", 0)),
            "stream_interface": emulator.video_streaming_interface,
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.integration
def test_control_roundtrip(gadget_daemon: Dict[str, int]) -> None:
    with UVCCamera.open(
        vid=gadget_daemon["vendor_id"],
        pid=gadget_daemon["product_id"],
        interface=gadget_daemon["stream_interface"],
    ) as camera:
        controls = camera.enumerate_controls(refresh=True)
        names = {entry.name for entry in controls}
        assert "Brightness" in names
        camera.set_control("Brightness", 192)
        assert camera.get_control("Brightness") == 192


@pytest.mark.integration
def test_stream_single_frame(gadget_daemon: Dict[str, int]) -> None:
    with UVCCamera.open(
        vid=gadget_daemon["vendor_id"],
        pid=gadget_daemon["product_id"],
        interface=gadget_daemon["stream_interface"],
    ) as camera:
        stream = camera.stream(width=640, height=480, codec=CodecPreference.MJPEG, duration=1)
        with stream as frames:
            frame = next(iter(frames))
            assert frame.payload.startswith(b"\xff\xd8")
            assert frame.frame.width == 640
            assert frame.frame.height == 480
