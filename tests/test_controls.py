from __future__ import annotations

from pathlib import Path

import pytest

from libusb_uvc import (
    GET_CUR,
    UVCControlsManager,
    vc_ctrl_get,
    vc_ctrl_set,
)

from .mocks import MockUsbDevice
from .uvc_emulator import UvcEmulatorLogic

PROFILE_PATH = Path(__file__).parent / "data" / "sample_camera_profile.json"


@pytest.fixture()
def emulator() -> UvcEmulatorLogic:
    return UvcEmulatorLogic(PROFILE_PATH)


@pytest.fixture()
def mock_device(emulator: UvcEmulatorLogic) -> MockUsbDevice:
    return MockUsbDevice(emulator)


@pytest.fixture()
def controls_manager(mock_device: MockUsbDevice, emulator: UvcEmulatorLogic) -> UVCControlsManager:
    return UVCControlsManager(
        mock_device,
        emulator.control_units,
        interface_number=emulator.video_control_interface,
    )


def test_enumerate_controls(controls_manager: UVCControlsManager):
    controls = controls_manager.get_controls()
    names = [entry.name for entry in controls]
    assert names == ["Brightness", "Contrast"]


def test_get_and_set_control(controls_manager: UVCControlsManager, emulator: UvcEmulatorLogic, mock_device: MockUsbDevice):
    # locate brightness entry
    entry = next(ctrl for ctrl in controls_manager.get_controls() if ctrl.name == "Brightness")
    interface_number = getattr(controls_manager, "_interface")

    raw = vc_ctrl_get(
        mock_device,
        interface_number,
        entry.unit_id,
        entry.selector,
        GET_CUR,
        entry.length,
    )
    assert int.from_bytes(raw, "little") == 128

    vc_ctrl_set(
        mock_device,
        interface_number,
        entry.unit_id,
        entry.selector,
        (150).to_bytes(entry.length, "little"),
    )
    raw = vc_ctrl_get(
        mock_device,
        interface_number,
        entry.unit_id,
        entry.selector,
        GET_CUR,
        entry.length,
    )
    assert int.from_bytes(raw, "little") == 150
    assert emulator.get_control_value(2, 1) == 150
