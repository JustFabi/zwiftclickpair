"""Protocol unit tests for the OpenBikeControl codec (stdlib only).

Run with:  python tests/test_obc_protocol.py
(Also executed by `clickbridge --selftest`.)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clickbridge import obc  # noqa: E402


class CaseFailure(Exception):
    pass


def expect_bytes(label: str, got: bytes, want: bytes) -> None:
    if got != want:
        raise CaseFailure(
            f"{label}: got {got.hex()} want {want.hex()}"
        )


def test_button_state_single() -> None:
    # spec MDNS.md example: [0x01, 0x01, 0x01]
    expect_bytes("single press", obc.encode_button_states([(0x01, 0x01)]),
                 bytes((0x01, 0x01, 0x01)))


def test_button_state_multi() -> None:
    # spec PROTOCOL.md "Multiple Actions Per Button":
    # [0x01, 0x01, 0x01, 0x30, 0x01, 0x14, 0x01]
    msg = obc.encode_button_states([(0x01, 0x01), (0x30, 0x01), (0x14, 0x01)])
    expect_bytes("multi-action press", msg, bytes((0x01, 0x01, 0x01, 0x30, 0x01, 0x14, 0x01)))


def test_button_state_pair() -> None:
    # spec MDNS.md example: press 0x01, release 0x02 in one message
    msg = obc.encode_button_states([(0x01, 0x01), (0x02, 0x00)])
    expect_bytes("press+release", msg, bytes((0x01, 0x01, 0x01, 0x02, 0x00)))


def test_device_status() -> None:
    expect_bytes("battery 85", obc.encode_device_status(85, True), bytes((0x02, 0x55, 0x01)))
    expect_bytes("battery unknown", obc.encode_device_status(obc.BATTERY_UNKNOWN, True),
                 bytes((0x02, 0xFF, 0x01)))
    expect_bytes("not connected", obc.encode_device_status(0, False), bytes((0x02, 0x00, 0x00)))


def test_app_info_spec_example() -> None:
    # spec MDNS.md example:
    # [0x04, 0x01, 0x05, 'z','w','i','f','t', 0x06, '1','.','5','2','.','0',
    #  0x04, 0x01, 0x02, 0x10, 0x14]
    msg = obc.encode_app_info("zwift", "1.52.0", [0x01, 0x02, 0x10, 0x14])
    want = bytes((0x04, 0x01, 0x05)) + b"zwift" + bytes((0x06,)) + b"1.52.0" + \
        bytes((0x04, 0x01, 0x02, 0x10, 0x14))
    expect_bytes("app info", msg, want)
    parsed = obc.parse_app_info_message(msg)
    assert parsed["app_id"] == "zwift", parsed
    assert parsed["app_version"] == "1.52.0", parsed
    assert parsed["supported_buttons"] == [0x01, 0x02, 0x10, 0x14], parsed
    assert parsed["version"] == 0x01


def test_app_info_zero_buttons_is_unfiltered() -> None:
    msg = obc.encode_app_info("my-custom-app", "2.0.1-beta", [])
    parsed = obc.parse_app_info_message(msg)
    assert parsed["supported_buttons"] == [], parsed
    assert parsed["app_id"] == "my-custom-app"


def test_app_info_empty_app_id() -> None:
    msg = obc.encode_app_info("", "", [])
    parsed = obc.parse_app_info_message(msg)
    assert parsed["app_id"] == "" and parsed["app_version"] == ""


def test_haptic_examples() -> None:
    h = obc.parse_haptic_feedback(bytes((0x03, 0x01, 0x00, 0x00)))
    assert h["pattern"] == 1 and h["duration"] == 0 and h["intensity"] == 0, h
    h = obc.parse_haptic_feedback(bytes((0x03, 0x02, 0x14, 0x80)))
    assert h["pattern"] == 2 and h["duration"] == 0x14 and h["intensity"] == 0x80, h
    try:
        obc.parse_haptic_feedback(bytes((0x03, 0x01)))
    except ValueError:
        pass
    else:
        raise CaseFailure("truncated haptic frame must raise")


def test_action_ids() -> None:
    assert obc.action_id("shift_up") == 0x01
    assert obc.action_id("shift_down") == 0x02
    assert obc.action_id("0x33") == 0x33
    assert obc.action_name(0x01) == "Shift Up"
    try:
        obc.action_id("warp_drive")
    except ValueError:
        pass
    else:
        raise CaseFailure("unknown action must raise")


def run_all(verbose: bool = True) -> bool:
    tests = [
        test_button_state_single,
        test_button_state_multi,
        test_button_state_pair,
        test_device_status,
        test_app_info_spec_example,
        test_app_info_zero_buttons_is_unfiltered,
        test_app_info_empty_app_id,
        test_haptic_examples,
        test_action_ids,
    ]
    for test in tests:
        try:
            test()
            if verbose:
                print(f"  ok  {test.__name__}")
        except Exception as exc:
            if verbose:
                print(f"FAIL  {test.__name__}: {exc}")
            return False
    return True


if __name__ == "__main__":
    ok = run_all(verbose=True)
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
