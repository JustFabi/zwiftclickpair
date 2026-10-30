"""OpenBikeControl (OBC) protocol codec.

Binary message format shared by the BLE and the mDNS+TCP transports:

  type 0x01 | button-state | [id, state, id, state, ...]        (device -> app)
  type 0x02 | device-status | [battery, connected]              (device -> app)
  type 0x03 | haptic-feedback | [pattern, duration, intensity]  (app -> device)
  type 0x04 | app-info | [version, idLen, id..., verLen, ver..., count, ids...]
                                                              (app -> device)

Spec: https://github.com/OpenBikeControl/openbikecontrol-protocol (MIT).
"""

from __future__ import annotations

# --- transport / addressing -------------------------------------------------
MDNS_SERVICE_TYPE = "_openbikecontrol._tcp.local."
OBC_PRIMARY_SERVICE_UUID = "d273f680-d548-419d-b9d1-fa0472345229"
CHAR_BUTTON_STATE_UUID = "d273f681-d548-419d-b9d1-fa0472345229"
CHAR_HAPTIC_UUID = "d273f682-d548-419d-b9d1-fa0472345229"
CHAR_APP_INFO_UUID = "d273f683-d548-419d-b9d1-fa0472345229"

# --- message types ----------------------------------------------------------
MSG_BUTTON_STATE = 0x01
MSG_DEVICE_STATUS = 0x02
MSG_HAPTIC_FEEDBACK = 0x03
MSG_APP_INFO = 0x04

# --- standard button ids ----------------------------------------------------
ACTION_NAMES: dict[int, str] = {
    0x01: "Shift Up",
    0x02: "Shift Down",
    0x03: "Gear Set",
    0x04: "Chainring Set",
    0x05: "Cassette Set",
    0x10: "Up",
    0x11: "Down",
    0x12: "Left",
    0x13: "Right",
    0x14: "Select/Confirm",
    0x15: "Back/Cancel",
    0x16: "Menu",
    0x17: "Home",
    0x18: "Steer Left",
    0x19: "Steer Right",
    0x1A: "Brake",
    0x1B: "Steering Angle",
    0x20: "Emote",
    0x21: "Push to Talk",
    0x24: "Screenshot",
    0x30: "Increase Difficulty",
    0x31: "Decrease Difficulty",
    0x32: "Skip Interval",
    0x33: "Pause",
    0x34: "Resume",
    0x35: "Lap",
    0x36: "Previous Interval",
    0x37: "U-Turn",
    0x38: "Change Mode",
    0x39: "Take a Break",
    0x3A: "Join Another Rider",
    0x3B: "Change Route",
    0x3C: "Cruise Control",
    0x40: "Camera View",
    0x44: "HUD Toggle",
    0x45: "Map Toggle",
    0x46: "Spectate Rider",
    0x50: "Power-up 1",
    0x51: "Power-up 2",
    0x52: "Power-up 3",
}

# Friendly alias -> standard button id (used in config.json)
ACTION_IDS: dict[str, int] = {
    "shift_up": 0x01,
    "shift_down": 0x02,
    "gear_set": 0x03,
    "chainring_set": 0x04,
    "cassette_set": 0x05,
    "up": 0x10,
    "down": 0x11,
    "left": 0x12,
    "right": 0x13,
    "select": 0x14,
    "back": 0x15,
    "menu": 0x16,
    "home": 0x17,
    "steer_left": 0x18,
    "steer_right": 0x19,
    "brake": 0x1A,
    "emote": 0x20,
    "push_to_talk": 0x21,
    "screenshot": 0x24,
    "difficulty_up": 0x30,
    "difficulty_down": 0x31,
    "skip_interval": 0x32,
    "pause": 0x33,
    "resume": 0x34,
    "lap": 0x35,
    "previous_interval": 0x36,
    "uturn": 0x37,
    "change_mode": 0x38,
    "take_break": 0x39,
    "join_rider": 0x3A,
    "change_route": 0x3B,
    "cruise_control": 0x3C,
    "camera_view": 0x40,
    "hud_toggle": 0x44,
    "map_toggle": 0x45,
    "spectate": 0x46,
    "powerup_1": 0x50,
    "powerup_2": 0x51,
    "powerup_3": 0x52,
}

HEX_ACTION_IDS: dict[str, int] = {
    f"0x{id:02X}": id for id in ACTION_NAMES
}

BATTERY_UNKNOWN = 0xFF
STATE_PRESSED = 0x01
STATE_RELEASED = 0x00


def action_id(token: str) -> int:
    """Resolve a configurable action token ("shift_up" or "0x01") to an id."""
    token = token.strip().lower()
    if token in ACTION_IDS:
        return ACTION_IDS[token]
    if token in HEX_ACTION_IDS:
        return HEX_ACTION_IDS[token]
    raise ValueError(
        f"unknown action {token!r}; see README for the list of valid names "
        f"(e.g. shift_up, shift_down, pause, resume, uturn, emote, camera_view, ...)"
    )


def action_name(button_id: int) -> str:
    return ACTION_NAMES.get(button_id, f"0x{button_id:02X}")


# --- encoders (device -> app) ----------------------------------------------

def encode_button_states(pairs: list[tuple[int, int]]) -> bytes:
    """Encode one button-state message."""
    payload = bytearray((MSG_BUTTON_STATE,))
    for button_id, state in pairs:
        if not 0 <= button_id <= 0xFF or not 0 <= state <= 0xFF:
            raise ValueError("button id/state out of range")
        payload.append(button_id)
        payload.append(state)
    return bytes(payload)


def encode_device_status(battery: int, connected: bool) -> bytes:
    """Battery 0-100 (0xFF if unknown) + connected flag."""
    payload = bytearray((MSG_DEVICE_STATUS, battery & 0xFF, 0x01 if connected else 0x00))
    return bytes(payload)


def encode_app_info(app_id: str, app_version: str,
                    supported_buttons: list[int] | None = None) -> bytes:
    """Build an app-info message (spec example format; used by tests/tools)."""
    ids = supported_buttons or []
    aid = app_id.encode("utf-8")[:32]
    ver = app_version.encode("utf-8")[:32]
    msg = bytearray((MSG_APP_INFO, 0x01, len(aid)))
    msg += aid
    msg += bytes((len(ver),))
    msg += ver
    msg += bytes((len(ids),))
    msg += bytes(ids)
    return bytes(msg)


# --- app -> device parsers ---------------------------------------------------

def parse_app_info_message(msg: bytes) -> dict:
    """Parse the app-info message.  Returns a dict app_id / app_version /
    supported_buttons (list of ids, empty == 'supports everything')."""
    if not msg or msg[0] != MSG_APP_INFO:
        raise ValueError("not an app-info message")
    i = 1
    version = msg[i]
    i += 1
    id_len = msg[i]
    i += 1
    app_id = msg[i : i + id_len].decode("utf-8", "replace")
    i += id_len
    ver_len = msg[i]
    i += 1
    app_version = msg[i : i + ver_len].decode("utf-8", "replace")
    i += ver_len
    count = msg[i]
    i += 1
    ids = list(msg[i : i + count])
    return {
        "version": version,
        "app_id": app_id,
        "app_version": app_version,
        "supported_buttons": ids,
    }


def parse_haptic_feedback(msg: bytes) -> dict:
    if not msg or msg[0] != MSG_HAPTIC_FEEDBACK:
        raise ValueError("not a haptic-feedback message")
    if len(msg) < 4:
        raise ValueError("haptic message truncated")
    return {"pattern": msg[1], "duration": msg[2], "intensity": msg[3]}


def short_button_list(ids, limit: int = 8) -> str:
    shown = ", ".join(f"0x{i:02X} {ACTION_NAMES.get(i, '?')}" for i in ids[:limit])
    if len(ids) > limit:
        shown += f", ... (+{len(ids) - limit} more)"
    return shown or "(none)"
