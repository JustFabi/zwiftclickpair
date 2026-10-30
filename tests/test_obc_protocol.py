"""Protocol unit tests for the OpenBikeControl codec (stdlib only).

Run with:  python tests/test_obc_protocol.py
(Also executed by `clickbridge --selftest`.)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clickbridge import obc  # noqa: E402

# Button-decoding tests need bleak (installed together with the bridge);
# degrade gracefully when only the codec is importable.
try:
    from clickbridge.clickdevice import (  # noqa: E402
        BIT_NAMES,
        CLICK_PB_FIELD_NAMES,
        MSG_KEYPAD_CLICK,
        MSG_KEYPAD_SF2,
        ZwiftClick,
        default_bit_actions,
        effective_bit_actions,
    )
except ImportError:  # bleak missing
    ZwiftClick = None


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


# ---------------------------------------------------------------- Click bits

def _press_events(click: "ZwiftClick") -> list[tuple[str, bool, int]]:
    log: list[tuple[str, bool, int]] = []
    click.on_action = lambda name, pressed, bit: log.append((name, pressed, bit))
    return log


def test_bit_table_is_keyed_by_bit_value() -> None:
    """Regression: dispatch must use keypad bit VALUES (0x1, 0x2, 0x4, ...)
    and the defaults must follow the Cog-era pod semantics: bit 0x100 is the
    LEFT pod's shifter (shifts DOWN), bit 0x1000 the right pod's (UP).
    Verifying hardware wired: left button press -> fffffeff (bit 0x100),
    right button press -> ffffefff (bit 0x1000).
    """
    assert BIT_NAMES[0x1] == "dpad_left"
    assert BIT_NAMES[0x2] == "dpad_up"
    assert BIT_NAMES[0x4] == "dpad_right"
    assert BIT_NAMES[0x8] == "dpad_down"
    assert BIT_NAMES[0x100] == "shifter_left"
    assert BIT_NAMES[0x200] == "shifter_left_2"
    assert BIT_NAMES[0x1000] == "shifter_right"
    assert BIT_NAMES[0x2000] == "shifter_right_2"
    v2 = effective_bit_actions(11, {})
    for bit, logical in ((0x100, "down"), (0x200, "down"),
                         (0x1000, "up"), (0x2000, "up"),
                         (0x1, "nav_left"), (0x2, "nav_up"),
                         (0x4, "nav_right"), (0x8, "nav_down"),
                         (0x10, "a"), (0x20, "b"), (0x40, "y"), (0x80, "z")):
        assert v2.get(bit) == logical, f"bit 0x{bit:x} -> {v2.get(bit)}"


def test_click_v2_real_bitmap_frames() -> None:
    """End-to-end: real 0x23 wire frames captured from a Click v2 (Cog kit).

    The left pod has a single shifter button that is wired to bit 0x100 and
    must emit shift-DOWN; the right pod's shifter (bit 0x1000) emits shift-UP.
    """
    if ZwiftClick is None:
        return
    idle = b"\x23\x08\xff\xff\xff\xff\x0f"
    frames = {
        b"\x23\x08\xff\xfd\xff\xff\x0f": [("down", True, 0x100)],   # left pod shifter
        b"\x23\x08\xff\xfb\xff\xff\x0f": [("down", True, 0x200)],   # left pod 2nd position
        b"\x23\x08\xff\xdf\xff\xff\x0f": [(  "up", True, 0x1000)],  # right pod shifter
        b"\x23\x08\xff\xbf\xff\xff\x0f": [(  "up", True, 0x2000)],  # right pod 2nd position
        b"\x23\x08\xef\xff\xff\xff\x0f": [(   "a", True, 0x010)],   # A
        b"\x23\x08\xdf\xff\xff\xff\x0f": [(   "b", True, 0x020)],   # B
        b"\x23\x08\xbf\xff\xff\xff\x0f": [(   "y", True, 0x040)],   # Y
        b"\x23\x08\xff\xfe\xff\xff\x0f": [(   "z", True, 0x080)],   # Z
        b"\x23\x08\xfe\xff\xff\xff\x0f": [("nav_left", True, 0x01)],
        b"\x23\x08\xfd\xff\xff\xff\x0f": [("nav_up", True, 0x02)],
        b"\x23\x08\xfb\xff\xff\xff\x0f": [("nav_right", True, 0x04)],
        b"\x23\x08\xf7\xff\xff\xff\x0f": [("nav_down", True, 0x08)],
    }
    for frame, want in frames.items():
        click = ZwiftClick(kind_id=11)
        events = _press_events(click)
        click._parse_plain(frame)
        assert events == want, f"{frame.hex()}: {events}"
        click._parse_plain(idle)
        assert events[-1] == (want[0][0], False, want[0][2]), f"{frame.hex()}: release edge"


def test_onoff_and_powerup_bits_send_nothing() -> None:
    if ZwiftClick is None:
        return
    click = ZwiftClick(kind_id=11)
    events = _press_events(click)
    for frame in (b"\x23\x08\xff\xf7\xff\xff\x0f",  # power-up left (bit 0x400)
                  b"\x23\x08\xff\xef\xff\xff\x0f",  # on/off left  (bit 0x800)
                  b"\x23\x08\xff\xff\xfe\xff\x0f",  # power-up right (bit 0x4000)
                  b"\x23\x08\xff\xff\xfd\xff\x0f"):  # on/off right (bit 0x8000)
        click._parse_plain(frame)
    assert events == [], events


def test_click_v1_still_shifts() -> None:
    """Original Click: its +/- buttons (D-pad bits and the 0x37 protobuf)."""
    if ZwiftClick is None:
        return
    click = ZwiftClick(kind_id=9)
    events = _press_events(click)
    click._emit_bitmap(0xFFFFFFFD)   # older firmware: "+" on the classic bit
    assert events == [("up", True, 0x2)]
    click._emit_bitmap(0xFFFFFFFF)   # release (idle in between)
    click._emit_bitmap(0xFFFFFFF7)   # "-"
    click._emit_bitmap(0xFFFFFFFF)
    assert events == [("up", True, 0x2), ("up", False, 0x2),
                      ("down", True, 0x8), ("down", False, 0x8)], events

    click1 = ZwiftClick(kind_id=9)
    events1 = _press_events(click1)
    assert MSG_KEYPAD_CLICK == 0x37
    click1._parse_plain(b"\x37\x08\x00")  # ClickKeyPadStatus: Button_Plus = ON
    click1._parse_plain(b"\x37\x10\x00")  # Button_Minus = ON
    click1._parse_plain(b"\x37\x08\x01")  # release
    assert events1 == [("up", True, 1), ("down", True, 2), ("up", False, 1)], events1

    # unknown generation (pinned address): always favour shifting
    fallback = default_bit_actions(None)
    assert fallback[0x2] == "up" and fallback[0x8] == "down"
    assert fallback[0x100] == "down" and fallback[0x1000] == "up"


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
        test_bit_table_is_keyed_by_bit_value,
        test_click_v2_real_bitmap_frames,
        test_onoff_and_powerup_bits_send_nothing,
        test_click_v1_still_shifts,
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
