"""BLE side of ClickBridge: discovery + protocol decoding of Zwift controllers.

Two firmware generations exist on Zwift hardware and both are handled:

* plain  -- "RideOn" handshake + protocol-buffer messages over the
  ``...-19ca-4651-86e5-fa29dcdd09d1`` characteristics (current Click, Play,
  Ride firmware).  Advertised under the legacy 128-bit service UUID or, on
  Ride-gen hardware, under the 16-bit UUID ``0xFC82``.
* crypto -- legacy encrypted variant (ECDH handshake), opt-in via
  ``--legacy-ecdh`` (see zwiftcrypt.py).  Payloads decode identically once
  decrypted.

The keypad arrives either as a 32-bit bitmap message (0x23, inverse logic)
or -- original Click v1 -- as a small protobuf message (0x37, fields for
Plus/Minus, active-low); Play-era hardware (0x07) uses protobuf fields too.
This module maps those inputs onto logical action names ("up", "down",
"nav_up", ...) which the orchestrator translates into OpenBikeControl
actions.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from . import zwiftcrypt

log = logging.getLogger("click")

ZWIFT_MFG_ID = 0x094A

LEGACY_SERVICE_UUID = "00000001-19ca-4651-86e5-fa29dcdd09d1"
CHAR_MEASURE_UUID = "00000002-19ca-4651-86e5-fa29dcdd09d1"
CHAR_CTRL_UUID = "00000003-19ca-4651-86e5-fa29dcdd09d1"
CHAR_RESPONSE_UUID = "00000004-19ca-4651-86e5-fa29dcdd09d1"
BATTERY_CHAR_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

DEVICE_KINDS = {
    1: "Zwift Hub",
    2: "Zwift Play (right)",
    3: "Zwift Play (left)",
    7: "Zwift Ride (right)",
    8: "Zwift Ride (left)",
    9: "Zwift Click",
    10: "Zwift Click v2 (right)",   # 0x0A, confirmed on hardware
    11: "Zwift Click v2 (left)",    # 0x0B, confirmed on hardware
    14: "Zwift Play (firmware 2.x)",  # 0x0E: Play speaking the Ride protocol
}

KIND_PREFERENCE = {
    "Zwift Click": 0,
    "Zwift Click v2 (left)": 0,
    "Zwift Click v2 (right)": 0,
    "Zwift Ride (left)": 1,
    "Zwift Ride (right)": 1,
    "Zwift Play (left)": 2,
    "Zwift Play (right)": 2,
    "Zwift Play (firmware 2.x)": 2,
}

# 32-bit keypad bitmap, inverse logic (bit cleared == pressed).
# Bit table matching SwiftControl's maintained decode of the "0x23" keypad
# message and confirmed against our own Click v2 wire capture: 0x1 left,
# 0x2 up, 0x4 right, 0x8 down, 0x10 A, 0x20 B, 0x40 Y, 0x80 Z, then
# per-half shifter / power-up / on-off bits.
BIT_NAMES: dict[int, str] = {
    0x00001: "dpad_left",
    0x00002: "dpad_up",
    0x00004: "dpad_right",
    0x00008: "dpad_down",
    0x00010: "A",
    0x00020: "B",
    0x00040: "Y",
    0x00080: "Z",
    0x00100: "shifter_left",     # left pod shifter (Cog kit: shifts DOWN)
    0x00200: "shifter_left_2",   # left pod's second shift position
    0x00400: "powerup_left",
    0x00800: "onoff_left",
    0x01000: "shifter_right",    # right pod shifter (shifts UP)
    0x02000: "shifter_right_2",  # right pod's second shift position
    0x04000: "powerup_right",
    0x08000: "onoff_right",
}

# Keypad-bit -> bridge-logical defaults, keyed by bit VALUE.
#
# Click v2 (Cog kit) / Ride gen: all "0x23" bitmap hardware.  One shift
# button per pod (two wire positions), verified from hardware captures:
# bit `0x100` = the left pod's shifter, bit `0x1000` = the right pod's.
# Zwift's own Cog semantics: LEFT pod shifts DOWN, RIGHT pod shifts UP
# (SwiftControl maps every left-side shifter button to shiftDown and every
# right-side one to shiftUp).  D-pad navigates, A selects, B backs,
# Y fires a power-up, Z brakes; power-up/on-off slots left unbound.
V2_DEFAULT_BIT_ACTIONS: dict[int, str] = {
    0x0100: "down", 0x0200: "down",            # left pod shifters -> DOWN
    0x1000: "up", 0x2000: "up",                # right pod shifters -> UP
    0x0001: "nav_left", 0x0002: "nav_up",
    0x0004: "nav_right", 0x0008: "nav_down",
    0x0010: "a", 0x0020: "b", 0x0040: "y", 0x0080: "z",
}

# Original Click v1: its two +/- buttons report on the classic D-pad bits
# (older firmware) or as protobuf fields 1/2 of the 0x37 message (firmware
# 1.1.0+).  The v2 shifter slots are also included (with the same left=
# down / right=up side semantics) so pairs of Click v2 keep working when
# their kind could not be classified.
V1_DEFAULT_BIT_ACTIONS: dict[int, str] = {
    0x0002: "up", 0x0008: "down",
    0x0100: "down", 0x0200: "down",
    0x1000: "up", 0x2000: "up",
}


def default_bit_actions(kind_id: int | None) -> dict[int, str]:
    """Default keypad-bit -> logical mapping for this hardware generation."""
    if kind_id is None or kind_id == 9:      # original Click (or unknown kind)
        return dict(V1_DEFAULT_BIT_ACTIONS)
    return dict(V2_DEFAULT_BIT_ACTIONS)


def effective_bit_actions(kind_id: int | None,
                          configured: dict[int, str] | None) -> dict[int, str]:
    """Defaults + user's click_bits remap (user values win)."""
    return {**default_bit_actions(kind_id), **(configured or {})}

# Legacy (encrypted, Zwift Play era) protocol-buffer field numbers.
PB_FIELD_NAMES = {
    1: "pad",
    2: "up",
    3: "left",
    4: "right",
    5: "down",
    6: "power",
    7: "shifter",
    8: "steer",
    9: "brake",
}
PB_DEFAULT_FIELD_ACTIONS = {2: "up", 5: "down"}

# Original Click v1 keypad message (0x37): protobuf with one field per
# button (ClickKeyPadStatus: Button_Plus = 1, Button_Minus = 2); the
# PlayButtonStatus enum is active-low -- value 0 (ON) means pressed.
CLICK_PB_FIELD_NAMES = {1: "plus", 2: "minus"}
CLICK_PB_DEFAULT_FIELD_ACTIONS = {1: "up", 2: "down"}

MSG_KEYPAD_PB = 0x07  # Play-era protobuf keypad message
MSG_KEYPAD_CLICK = 0x37  # original Click protobuf keypad message
MSG_KEYPAD_SF2 = 0x23  # current-era keyed bitmap message
MSG_IDLE_A = 0x15
MSG_IDLE_B = 0x19
MSG_LOG = 0x2A  # firmware log frames (e.g. "Batt. Volt. 2945 mV")

PLAIN_ACK = b"RideOn"


def read_varint(buf: bytes, i: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7
    return value, i


def classify_advertisement(name: str | None, adv: AdvertisementData) -> dict | None:
    """Return metadata when an advertisement looks like a Zwift controller."""
    mfg = adv.manufacturer_data.get(ZWIFT_MFG_ID)
    kind_id = mfg[0] if mfg else None
    services = {u.lower() for u in adv.service_uuids}
    name_hit = bool(name) and "zwift" in name.lower()
    if not (kind_id is not None or LEGACY_SERVICE_UUID in services or name_hit):
        return None
    kind = DEVICE_KINDS.get(kind_id, f"unknown({kind_id:#04x})") if kind_id is not None else "?"
    return {
        "kind": kind,
        "kind_id": kind_id,
        "services": sorted(s.lower() for s in adv.service_uuids),
        "mfg_hex": mfg.hex() if mfg else "",
    }


ActionCallback = Callable[[str, bool, int], None]


async def discover_controllers(timeout: float = 5.0) -> list[tuple[BLEDevice, AdvertisementData, dict]]:
    """One scan; returns (device, adv, info) for every Zwift controller seen."""
    results = await BleakScanner.discover(timeout=timeout, return_adv=True)
    # bleak >= 1.0 returns {address: (device, adv)}; older returns a list.
    pairs = list(results.values()) if isinstance(results, dict) else list(results)
    out: list[tuple[BLEDevice, AdvertisementData, dict]] = []
    for dev, adv in pairs:
        info = classify_advertisement(dev.name, adv)
        if info:
            out.append((dev, adv, info))
    return out


async def disconnect_stale() -> None:
    """Disconnect BlueZ-orphaned Zwift controllers.

    BlueZ keeps a connection open after the controlling process dies.  A
    connected controller never advertises, so such zombies must be dropped
    before a restarted bridge can rediscover the hardware.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl", "devices", "Connected",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
    except Exception:
        return
    for line in out.decode(errors="replace").splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        addr, name = parts[1], (parts[2] if len(parts) > 2 else "")
        if not name.lower().startswith("zwift"):
            continue
        log.info("dropping stale BlueZ connection to %s (%s) ...", addr, name)
        try:
            killer = await asyncio.create_subprocess_exec(
                "bluetoothctl", "disconnect", addr,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.communicate()
            await asyncio.sleep(0.2)
        except Exception:
            pass


class ZwiftClick:
    """Connects to one Zwift controller and emits logical button events."""

    def __init__(
        self,
        *,
        address: str | None = None,
        kind_id: int | None = None,
        watch: bool = False,
        legacy_ecdh: bool = False,
        bit_actions: dict[int, str] | None = None,
        field_actions: dict[int, str] | None = None,
        on_action: ActionCallback | None = None,
        on_ready: Callable[[str], None] | None = None,
        on_gone: Callable[[], None] | None = None,
        on_battery: Callable[[int | None], None] | None = None,
    ):
        self.address = (address or "").strip() or None
        self.watch = watch
        self.legacy_ecdh = legacy_ecdh
        self.kind_id = kind_id
        self._bit_overrides = {**(bit_actions or {})}
        self.bit_actions = effective_bit_actions(self.kind_id, self._bit_overrides)
        self.field_actions = {**PB_DEFAULT_FIELD_ACTIONS, **(field_actions or {})}
        self._click_field_actions = {
            **CLICK_PB_DEFAULT_FIELD_ACTIONS, **(field_actions or {}),
        }

        self.on_action = on_action or (lambda *a, **k: None)
        self.on_ready = on_ready or (lambda *a, **k: None)
        self.on_gone = on_gone or (lambda *a, **k: None)
        self.on_battery = on_battery or (lambda *a, **k: None)

        self.connected = False
        self.protocol: str | None = None
        self.device_label = "?"
        self.battery: int | None = None

        self._client: BleakClient | None = None
        self._char_measure = None
        self._char_ctrl = None
        self._char_response = None
        self._crypto: zwiftcrypt.ZwiftCrypto | None = None
        self._handshake_keys: tuple[int, bytes] | None = None
        self._device_pub_raw: bytes | None = None
        self._crypto_pending = asyncio.Event()
        self._first_rx = asyncio.Event()
        self._last_rx = 0.0
        self._bitmap_prev = 0xFFFFFFFF
        self._active_pb: dict[tuple[int, int], bool] = {}
        self._active_click_pb: dict[int, bool] = {}
        self._haptics_ok = True
        self._haptic_warned = False
        self._stop = False
        self._run_task: asyncio.Task | None = None
        # automatic pairing gate: cleared = stop scanning/connecting
        self._pairing = asyncio.Event()
        self._pairing.set()

    # ------------------------------------------------------------------ API
    def start(self) -> asyncio.Task:
        self._stop = False
        self._run_task = asyncio.create_task(self._run_loop(), name="click-ble")
        return self._run_task

    async def stop(self) -> None:
        self._stop = True
        if self._client is not None and self._client.is_connected:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        if self._run_task is not None:
            self._run_task.cancel()
            try:
                await self._run_task
            except (asyncio.CancelledError, Exception):
                pass
            self._run_task = None

    def set_pairing(self, enabled: bool) -> None:
        """Pause/resume automatic scanning + reconnecting (control panel)."""
        if enabled:
            self._pairing.set()
        else:
            self._pairing.clear()

    @property
    def pairing_enabled(self) -> bool:
        return self._pairing.is_set()

    async def disconnect(self) -> None:
        """Drop the current BLE session on request (run loop reconnects)."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass

    def request_haptic(self, pattern: int) -> bool:
        """Forward an OpenBikeControl haptic request to the hardware."""
        if self._client is None or not self.connected or not self._haptics_ok:
            return False
        frame = bytes((0x12, 0x08, 0x0A, 0x06, 0x08, 0x02, 0x10, 0x00, 0x18, pattern & 0xFF))
        if self._crypto is not None:
            frame = self._crypto.encrypt(frame)
        asyncio.create_task(self._send_haptic(frame, pattern))
        return True

    async def _send_haptic(self, frame: bytes, pattern: int) -> None:
        try:
            await self._write_cmd(frame)
            self._watch(f"haptic pattern {pattern} forwarded")
        except Exception as exc:
            if not self._haptic_warned:
                log.warning("haptic forwarding failed (%s) -- disabling", exc)
                self._haptic_warned = True
            self._haptics_ok = False

    # ----------------------------------------------------------- main loop
    async def _run_loop(self) -> None:
        retry = 1.0
        paused = False
        while not self._stop:
            if not self._pairing.is_set():
                # automatic pairing paused by the operator (control panel):
                # an already-open session keeps running, but we do not scan
                # or reconnect until pairing is switched back on.
                if not paused:
                    log.info("pairing paused by operator -- %s",
                             "keeping the current session" if self.connected
                             else "not scanning for controllers")
                    paused = True
                await asyncio.sleep(0.5)
                continue
            if paused:
                log.info("pairing resumed -- searching again")
                paused = False
                retry = 1.0
            try:
                ok = await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("connect attempt failed: %r", exc)
                ok = False
            if ok:
                retry = 1.0
                try:
                    await self._connected_loop()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.debug("session ended: %r", exc)
            if self._stop:
                break
            was_connected = self.connected
            self.connected = False
            if self._client is not None:
                try:
                    await self._client.disconnect()
                except Exception:
                    pass
                self._client = None
                log.info("reconnecting ...")
            else:
                log.debug("connect attempt failed: retrying in %.1f s", retry)
            # Only report a real session ending -- a failed scan (controller
            # simply not advertising) must not spam the event journal with
            # "controller disconnected".
            if was_connected:
                self.on_gone()
            await asyncio.sleep(retry)
            retry = min(retry * 2, 5.0)

    async def _find_target(self) -> tuple[BLEDevice, AdvertisementData] | None:
        """Scan once and return (device, advertisement) or None."""
        results = await BleakScanner.discover(timeout=5.0, return_adv=True)
        # bleak >= 1.0 returns {address: (device, adv)}; older returns a list.
        pairs = list(results.values()) if isinstance(results, dict) else list(results)
        if self.address:
            for dev, adv in pairs:
                if dev.address.upper() == self.address.upper():
                    self._log_candidate(dev, adv)
                    self._absorb_kind(adv)
                    return dev, adv
            return None
        best: tuple[int, BLEDevice, AdvertisementData, int | None] | None = None
        for dev, adv in pairs:
            info = classify_advertisement(dev.name, adv)
            if not info:
                continue
            self._log_candidate(dev, adv)
            pref = KIND_PREFERENCE.get(info["kind"], 3)
            if best is None or pref < best[0]:
                best = (pref, dev, adv, info["kind_id"])
        if best is None:
            return None
        if best[3] is not None:
            self.kind_id = best[3]
        return (best[1], best[2])

    def _absorb_kind(self, adv: AdvertisementData | None) -> None:
        """Remember the advertised device kind (used for button defaults)."""
        mfg = adv.manufacturer_data.get(ZWIFT_MFG_ID) if adv else None
        if mfg and mfg[0] in DEVICE_KINDS:
            self.kind_id = mfg[0]

    def _log_candidate(self, dev: BLEDevice, adv: AdvertisementData) -> None:
        if not self.watch:
            return
        mfg = adv.manufacturer_data.get(ZWIFT_MFG_ID)
        kind = DEVICE_KINDS.get(mfg[0]) if mfg else "?"
        log.debug("scan: %s addr=%s kind=%s services=%s",
                  (dev.name or "?").strip(), dev.address.upper(), kind,
                  ",".join(sorted(u[-8:] for u in adv.service_uuids)) or "-")

    def _watch(self, msg: str) -> None:
        if self.watch:
            log.debug("click %s: %s", self.address, msg)

    # --------------------------------------------------------- connection
    async def _connect_once(self) -> bool:
        # Pinned instances connect straight to the address -- BlueZ keeps a
        # cached device object, so no scan is needed.  Scanning synchronously
        # from many instances at once starves the radio and trips D-Bus
        # 'Operation already in progress' errors.
        if self.address:
            log.info("direct connect attempt to pinned %s ...", self.address)
            dev, adv = None, None
        else:
            log.info("searching for controller ...")
            found = await self._find_target()
            if found is None:
                return False
            dev, adv = found
        # bleak >= 1.0 moved advertisement data off BLEDevice.
        self._absorb_kind(adv)
        kind_name = DEVICE_KINDS.get(self.kind_id) if self.kind_id is not None else None
        self.device_label = (
            f"{((dev.name if dev else 'Zwift Click') or 'Zwift Click').strip()}"
            f" ({kind_name or 'controller'})"
        )
        log.info("found %s -- connecting ...", self.device_label)

        if dev is None:
            client = BleakClient(self.address, timeout=15.0)
        else:
            client = BleakClient(dev, timeout=15.0)
        await client.connect()
        self._client = client
        try:
            try:
                services = client.services
            except AttributeError:  # older bleak
                services = await client.get_services()
        except Exception as exc:
            log.error("service discovery failed: %s", exc)
            return False

        chars = {c.uuid.lower(): c for s in services for c in s.characteristics}
        self._char_measure = chars.get(CHAR_MEASURE_UUID)
        self._char_ctrl = chars.get(CHAR_CTRL_UUID)
        self._char_response = chars.get(CHAR_RESPONSE_UUID)
        if not (self._char_measure and self._char_ctrl and self._char_response):
            log.error("device does not expose the Zwift controller service -- abandoning")
            return False

        self.protocol = None
        self._crypto = None
        self._handshake_keys = None
        self._device_pub_raw = None
        self._crypto_pending = asyncio.Event()
        self._first_rx = asyncio.Event()
        self._last_rx = time.monotonic()
        self._bitmap_prev = 0xFFFFFFFF
        self._active_pb.clear()
        self._active_click_pb.clear()
        self._haptics_ok = True
        # the device kind may only be known now (from this scan/round):
        self.bit_actions = effective_bit_actions(self.kind_id, self._bit_overrides)

        await client.start_notify(self._char_measure, self._on_measure)
        await client.start_notify(self._char_response, self._on_response)

        battery_char = chars.get(BATTERY_CHAR_UUID)
        if battery_char is not None:
            try:
                level = (await client.read_gatt_char(battery_char))[0]
                self.battery = level
                self.on_battery(level)
                log.info("controller battery: %d%%", level)
            except Exception:
                self.battery = None

        if self.legacy_ecdh:
            log.info("establishing encrypted session (legacy ECDH) ...")
            await self._handshake_crypto()
        else:
            log.info("performing plain 'RideOn' handshake ...")
            await self._write_cmd(PLAIN_ACK)

        try:
            await asyncio.wait_for(self._first_rx.wait(), timeout=6.0)
        except asyncio.TimeoutError:
            log.warning(
                "no notification after handshake (%s) -- retrying; a Click v2 "
                "usually needs the daily Zwift unlock (README), an original "
                "Click may simply need its LED woken by pressing a button",
                "crypto" if self.legacy_ecdh else "plain",
            )
            return False

        self.connected = True
        self.on_ready(self.device_label)
        log.info("controller ready  [protocol: %s]", self.protocol)
        return True

    async def _connected_loop(self) -> None:
        assert self._client is not None
        tasks = [
            asyncio.create_task(self._watchdog(), name="click-watchdog"),
            asyncio.create_task(self._battery_poll(), name="click-battery"),
        ]
        try:
            while self._client is not None and self._client.is_connected:
                await asyncio.sleep(0.5)
            log.info("controller disconnected")
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(5.0)
            if not (self._client is not None and self._client.is_connected):
                return
            age = time.monotonic() - self._last_rx
            if age > 45:
                log.warning(
                    "no data from controller for %.0f s -- reopening connection "
                    "(a Click v2 stops sending events ~1 min after its Zwift "
                    "session expires; see the unlock note in README)",
                    age,
                )
                try:
                    await self._client.disconnect()
                except Exception:
                    pass
                return

    async def _battery_poll(self) -> None:
        while True:
            await asyncio.sleep(300)
            client = self._client
            if client is None or not client.is_connected:
                return
            battery_char = None
            try:
                services = client.services
            except Exception:
                return
            for s in services:
                for c in s.characteristics:
                    if c.uuid.lower() == BATTERY_CHAR_UUID:
                        battery_char = c
            if battery_char is None:
                return
            try:
                level = (await client.read_gatt_char(battery_char))[0]
            except Exception:
                continue
            if level != self.battery:
                self.battery = level
                self.on_battery(level)
                log.info("controller battery: %d%%", level)

    # --------------------------------------------------------- handshakes
    async def _write_cmd(self, frame: bytes) -> None:
        assert self._client is not None and self._char_ctrl is not None
        try:
            await self._client.write_gatt_char(self._char_ctrl, frame, response=False)
        except Exception:
            await self._client.write_gatt_char(self._char_ctrl, frame, response=True)

    async def _handshake_crypto(self) -> None:
        scalar, our_public = zwiftcrypt.ZwiftCrypto.generate()
        self._handshake_keys = (scalar, our_public)
        self._device_pub_raw = None
        self._crypto_pending = asyncio.Event()
        await self._write_cmd(
            zwiftcrypt.HANDSHAKE_MAGIC + bytes((0x01, 0x02)) + our_public
        )
        try:
            await asyncio.wait_for(self._crypto_pending.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            log.warning("legacy ECDH handshake did not complete")
            return
        assert self._device_pub_raw is not None
        self._crypto = zwiftcrypt.ZwiftCrypto(scalar, our_public, self._device_pub_raw)
        self.protocol = "crypto-legacy"

    # --------------------------------------------------------- BLE events
    def _on_response(self, _char, data: bytearray) -> None:
        frame = bytes(data)
        if frame.startswith(zwiftcrypt.HANDSHAKE_MAGIC):
            if len(frame) == len(PLAIN_ACK):
                self._watch("plain session ack")
                return
            key = zwiftcrypt.decode_device_public_key(frame)
            if key is not None and self._handshake_keys is not None:
                self._device_pub_raw = key
                self._crypto_pending.set()
            else:
                self._watch(f"unrecognised handshake frame: {frame.hex()}")
            return
        if self._crypto is not None:
            plain = self._crypto.decrypt(frame)
            if plain is None:
                self._watch(f"response decrypt failure ({self._crypto.rx_failures})")
                return
            self._watch(f"response <= {plain.hex()}")
        else:
            self._watch(f"response frame: {frame.hex()}")

    def _on_measure(self, _char, data: bytearray) -> None:
        self._last_rx = time.monotonic()
        payload = bytes(data)
        if self._crypto is not None:
            plain = self._crypto.decrypt(payload)
            if plain is None:
                self._watch(f"measure decrypt failure ({self._crypto.rx_failures})")
                if self._crypto.rx_failures > 12:
                    log.warning("crypto session desynchronised -- reconnecting")
                    asyncio.create_task(self._force_reconnect())
                return
            payload = plain
        self._parse_plain(payload)

    async def _force_reconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass

    # ----------------------------------------------------------- decoding
    def _parse_plain(self, payload: bytes) -> None:
        self._first_rx.set()
        if self.legacy_ecdh and self._crypto is None:
            self._watch(f"ignore pre-crypto payload: {payload.hex()}")
            return
        if not payload:
            return
        first = payload[0]
        if first in (MSG_IDLE_A, MSG_IDLE_B):
            self._watch("idle heartbeat")
            if self.protocol is None:
                self.protocol = "plain"
            return
        if first == MSG_KEYPAD_SF2:
            # payload[0] is the 0x23 type, payload[1] the protobuf field-1
            # tag (0x08); the bitmap varint starts at offset 2.
            bitmap, _ = read_varint(payload, 2)
            if self.protocol is None:
                self.protocol = "plain"
            self._emit_bitmap(bitmap)
            return
        if first == MSG_KEYPAD_PB:
            if self.protocol is None:
                self.protocol = "plain-legacy-pb"
            self._emit_play_pb(payload[1:])
            return
        if first == MSG_KEYPAD_CLICK:
            if self.protocol is None:
                self.protocol = "plain-click-pb"
            self._emit_click_pb(payload[1:])
            return
        if first in (MSG_LOG, 0xFF):  # 0x2A = firmware log ("Batt. Volt."), 0xFF = device info
            self._watch(f"device log frame: {payload.hex()}")
            return
        self._watch(f"unknown payload: {payload.hex()}")

    def _dispatch_bit(self, bit: int, pressed: bool) -> None:
        """bit carries the keypad bitmap *value* (0x1, 0x2, 0x4, 0x8, ...)."""
        name = BIT_NAMES.get(bit, f"bit0x{bit:x}")
        action = self.bit_actions.get(bit)
        if action is None:
            if pressed or self.watch:
                self._watch(f"key {name} {'pressed' if pressed else 'released'} (no mapping configured)")
            return
        self.on_action(action, pressed, bit)

    def _emit_bitmap(self, bitmap: int) -> None:
        bitmap &= 0xFFFFFFFF
        if bitmap == self._bitmap_prev:
            return
        self._watch(f"keypad bitmap {bitmap:08x}")
        pressed = (~bitmap) & 0xFFFFFFFF
        prev_pressed = (~self._bitmap_prev) & 0xFFFFFFFF
        newly = pressed & ~prev_pressed
        released = ~pressed & prev_pressed
        self._bitmap_prev = bitmap
        for idx in bits_of(newly):
            self._dispatch_bit(1 << idx, True)
        for idx in bits_of(released):
            self._dispatch_bit(1 << idx, False)

    def _emit_click_pb(self, body: bytes) -> None:
        """Original Click keypad message (0x37): Button_Plus = 1, Button_Minus
        = 2, active-low (0 == pressed)."""
        i = 0
        fields: dict[int, int] = {}
        while i < len(body):
            tag = body[i]
            i += 1
            field = tag >> 3
            value, i = read_varint(body, i)
            fields[field] = value
        for field, value in sorted(fields.items()):
            if field not in (1, 2):
                self._watch(f"click pb field {field} = {value}")
                continue
            pressed = value == 0
            if self._active_click_pb.get(field, False) == pressed:
                continue
            self._active_click_pb[field] = pressed
            action = self._click_field_actions.get(field)
            name = CLICK_PB_FIELD_NAMES.get(field, f"field{field}")
            if action is None:
                self._watch(f"key {name} {'pressed' if pressed else 'released'} (no mapping configured)")
                continue
            self.on_action(action, pressed, field)

    def _emit_play_pb(self, body: bytes) -> None:
        i = 0
        fields: dict[int, int] = {}
        while i < len(body):
            tag = body[i]
            i += 1
            field = tag >> 3
            value, i = read_varint(body, i)
            fields[field] = value
        side = fields.get(1, 0) & 1
        for field, value in sorted(fields.items()):
            name = PB_FIELD_NAMES.get(field, f"field{field}")
            if field in (8, 9):  # analog axes
                self._watch(f"analog {name} (side {side}) = {value}")
                continue
            if field not in (2, 3, 4, 5, 7):
                self._watch(f"{name} = {value}")
                continue
            pressed = value == 0
            key = (side, field)
            if self._active_pb.get(key, False) == pressed:
                continue
            self._active_pb[key] = pressed
            action = self.field_actions.get(field)
            if action is None:
                self._watch(f"key {name} {'pressed' if pressed else 'released'} (no mapping configured)")
                continue
            self.on_action(action, pressed, field)


def bits_of(mask: int):
    while mask:
        low = mask & -mask
        yield low.bit_length() - 1
        mask ^= low
