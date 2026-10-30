"""ClickBridge command line interface and orchestrator.

Wires the BLE click source into the OpenBikeControl server and offers
self-diagnostics:

  clickbridge                 run the bridge
  clickbridge --mock          simulate the Click (keyboard-driven)
  clickbridge --show-devices  scan for nearby Zwift controllers
  clickbridge --selftest      protocol + TCP loopback checks
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

log = logging.getLogger("bridge")

from . import APP_NAME, __version__
from . import obc
from .clickdevice import ZwiftClick, classify_advertisement
from .control_api import ControlApi
from .obc_server import ObcServer

PKG_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PKG_ROOT / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    # How MyWhoosh (and other OpenBikeControl apps) see the device.
    "name": "ClickBridge",
    "port": 38393,
    # Local status/control API for the dashboard (127.0.0.1 only; 0 = off).
    "control_port": 38398,
    # Force the interface IP advertised via mDNS (null = auto).
    "host": None,
    # Map click physical buttons ("up"/"down"/...) to OpenBikeControl actions.
    # "actions" fire on press (released automatically at release time);
    # "long_press" fires as a quick tap on RELEASE when held >= long_press_ms.
    # Set "actions": [] to make a dedicated gesture button.
    "buttons": {
        "up": {"actions": ["shift_up"], "long_press": []},
        "down": {"actions": ["shift_down"], "long_press": []},
    },
    "long_press_ms": 800,
    # Forward app haptic requests to the Click's vibration motor.
    "haptics": True,
    # Re-map which keypad bitmap bits count as a logical button if your
    # firmware reports other keys ("2": "up", "8": "down", ...).
    "click_bits": {},
    # Same for legacy (%PB) field numbers, --legacy-ecdh mode only.
    "click_fields": {},
    "legacy_ecdh": False,
    "watch": False,
}

BANNER = """
e88~-_  888-~88e  e88'---.888-~88e  888'8888b.  888-~88e
d888   |888  888 d888     888  888b 888  8888 ~888  888b
 888   |888  888 8888__o< 888  8888 888   888D  888  8888
 888   |888  888 8888===e 888  888P 888 8888D   888  888P
 '"Yd8b '888-'88P Y888     888-_88P  '8888888P  '888-~88P-~
         88P                      -={{ Zwift Click -> MyWhoosh }}=-  v{version}

 1.  Turn the Click on (press any button, LED must be lit/or flashing).
 2.  Keep this window running while you ride.
 3.  MyWhoosh:  Devices screen -> OpenBikeControl icon (top-right) -> Yes.
 4.  Shift!        Config: {config}
"""


# ---------------------------------------------------------------------- config

def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path, overrides: dict) -> dict:
    user_layer: dict = {}
    if path.exists():
        try:
            user_layer = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(user_layer, dict):
                raise ValueError("config must be a JSON object")
        except (ValueError, OSError) as exc:
            print(f"WARNING: could not read {path}: {exc} -- using defaults", file=sys.stderr)
    cfg = deep_merge(DEFAULT_CONFIG, user_layer)
    for key, value in overrides.items():
        if value is not None:
            cfg[key] = value
    return cfg


def resolve_mapping(cfg: dict) -> dict[str, tuple[list[int], list[int]]]:
    mapping: dict[str, tuple[list[int], list[int]]] = {}
    for logical, spec in cfg.get("buttons", {}).items():
        short = [obc.action_id(a) for a in (spec.get("actions") or [])]
        longs = [obc.action_id(a) for a in (spec.get("long_press") or [])]
        mapping[logical] = (short, longs)
    return mapping


# ---------------------------------------------------------------------- mocks

class MockClick:
    """Simulates a Click: presses 'u'/'d' or auto-cycles with --mock-period."""

    connected = True
    battery: int | None = 87
    protocol = "mock"
    device_label = "Mock Click (simulated)"

    def __init__(self, bridge: "Bridge", period: float | None):
        self.bridge = bridge
        self.period = period
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._auto = 0

    def start(self) -> asyncio.Task:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._run(), name="mock-click")
        log.info("MOCK mode: %s", self._mock_help())
        return self._task

    async def stop(self) -> None:
        self._stopping.set()

    @staticmethod
    def _mock_help() -> str:
        return ("press  u / d  = shift up / down,  p = pause,  "
                "b = battery change,  q = quit  (MyWhoosh must be open)")

    def request_haptic(self, pattern: int) -> bool:
        log.info("MOCK: haptic pattern %d requested by app (no hardware)", pattern)
        return True

    async def _run(self) -> None:
        if self.period is not None:
            await self._auto_cycle()
            return
        if sys.platform != "win32":
            log.warning("--mock keyboard mode needs Windows; use --mock-period instead")
            return
        import msvcrt

        def reader() -> None:
            while not self._stopping.is_set():
                key = msvcrt.getwch()
                self._loop.call_soon_threadsafe(self._key, key)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        await self._stopping.wait()

    def _key(self, key: str) -> None:
        if key in ("q", "Q", "\x1b"):
            self._stopping.set()
            self._loop.call_soon(self.bridge.stop_requested)
            return
        logical = {"u": "up", "d": "down", "p": "pause"}.get(key)
        if logical is None:
            if key == "b":
                self.bridge.server.battery = max(5, (self.battery or 87) - 17)
                self.battery = self.bridge.server.battery
                asyncio.create_task(self.bridge.server.push_status())
            return
        self._tap(logical)

    def _tap(self, logical: str) -> None:
        self.bridge._on_click_action(logical, True, 0)
        release = lambda: self.bridge._on_click_action(logical, False, 0)
        self._loop.call_later(0.12, release)

    async def _auto_cycle(self) -> None:
        seq = ("up", "down")
        while not self._stopping.is_set():
            self._tap(seq[self._auto % 2])
            self._auto += 1
            await asyncio.sleep(max(0.5, self.period or 5.0))


# --------------------------------------------------------------------- bridge

class Bridge:
    """Wires the BLE click source into the OpenBikeControl server."""

    def __init__(self, cfg: dict, *, config_path: Path):
        self.cfg = cfg
        self.config_path = config_path
        self.mapping = resolve_mapping(cfg)
        self.long_press_ms = int(cfg.get("long_press_ms", 800))
        self.haptics = bool(cfg.get("haptics", True))
        self.server = ObcServer(
            port=int(cfg["port"]),
            name=str(cfg["name"]),
            host=cfg.get("host"),
            advertise=not bool(cfg.get("no_mdns")),
            on_app_info=self._on_app_info,
            on_haptic=self._on_haptic,
        )
        self._click: ZwiftClick | MockClick | None = None
        self._active: set[int] = set()
        self._press_at: dict[str, float] = {}
        self._unmapped_warned: set[str] = set()
        self._status_task: asyncio.Task | None = None
        self._stopped = asyncio.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.version = __version__
        self.started_at = time.time()
        self.pairing_enabled = True
        self.control_api: ControlApi | None = None
        self._events: deque = deque(maxlen=200)

    # ------------------------------------------------------- event journal
    def record(self, kind: str, msg: str) -> None:
        """Append a human-readable event for the dashboard (ring buffer)."""
        self._events.append({"t": time.strftime("%H:%M:%S"), "kind": kind, "msg": msg})

    # called from the mock keyboard thread (via call_soon) to exit cleanly
    def stop_requested(self) -> None:
        self._stopped.set()

    def _watch(self) -> bool:
        return bool(self.cfg.get("watch"))

    # ---------------------------------------------------------- construction
    def _build_click(self) -> ZwiftClick:
        def int_map(spec) -> dict[int, str]:
            out = {}
            for key, value in (spec or {}).items():
                try:
                    out[int(key)] = str(value)
                except (TypeError, ValueError):
                    log.error("config mapping key must be an int: %r", key)
            return out

        return ZwiftClick(
            address=self.cfg.get("address"),
            watch=self._watch(),
            legacy_ecdh=bool(self.cfg.get("legacy_ecdh")),
            bit_actions=int_map(self.cfg.get("click_bits")),
            field_actions=int_map(self.cfg.get("click_fields")),
            on_action=self._on_click_action,
            on_ready=self._on_click_ready,
            on_gone=self._on_click_gone,
            on_battery=self._on_click_battery,
        )

    # ------------------------------------------------- callbacks (click ->)
    def _on_click_ready(self, label: str) -> None:
        self.server.click_connected = True
        self.server.click_label = label
        self.record("click", f"controller ready: {label}")

    def _on_click_gone(self) -> None:
        self.server.click_connected = False
        self.record("click", "controller disconnected")
        dangling = sorted(self._active)
        self._active.clear()
        if dangling:
            asyncio.create_task(
                self.server.broadcast_buttons([(a, 0x00) for a in dangling])
            )
            asyncio.create_task(self.server.push_status())

    def _on_click_battery(self, level: int | None) -> None:
        self.server.battery = level
        self.record("battery", "?" if level is None else f"battery {level}%")
        asyncio.create_task(self.server.push_status())

    def _on_click_action(self, logical: str, pressed: bool, source: int) -> None:
        if self._watch():
            log.debug("click event: %s %s (code %d)",
                      logical, "press" if pressed else "release", source)
        mapping = self.mapping.get(logical)
        if mapping is None:
            if logical not in self._unmapped_warned:
                self._unmapped_warned.add(logical)
                log.info(
                    "no mapping for click button %r -- ignored "
                    "(add it under 'buttons' in config.json)", logical,
                )
            return
        short_actions, long_actions = mapping
        now = time.monotonic()
        if pressed:
            self._press_at[logical] = now
            ids = [a for a in short_actions if a not in self._active]
            if ids:
                self._active.update(ids)
                asyncio.create_task(self._broadcast([(a, 0x01) for a in ids]))
            return
        held_ms = (now - self._press_at.get(logical, now)) * 1000.0
        release_ids = [a for a in sorted(self._active) if a in short_actions]
        for a in release_ids:
            self._active.discard(a)
        if release_ids:
            asyncio.create_task(self._broadcast([(a, 0x00) for a in release_ids]))
        if long_actions and held_ms >= self.long_press_ms:
            asyncio.create_task(self._long_press_tap(long_actions))

    async def _broadcast(self, pairs: list[tuple[int, int]]) -> None:
        if not pairs:
            return
        await self.server.broadcast_buttons(pairs)
        pretty = ", ".join(
            f"{obc.action_name(a)} {'down' if s else 'up'}" for a, s in pairs
        )
        log.info("-> %s", pretty)
        self.record("shift", pretty)

    async def _long_press_tap(self, ids: list[int]) -> None:
        log.info("-> long-press tap: %s", ", ".join(obc.action_name(a) for a in ids))
        self.record("shift", "long-press: " + ", ".join(obc.action_name(a) for a in ids))
        await self.server.broadcast_buttons([(a, 0x01) for a in ids])
        await asyncio.sleep(0.15)
        await self.server.broadcast_buttons([(a, 0x00) for a in ids])

    # ------------------------------------------------- callbacks (apps ->)
    def _on_app_info(self, app_id: str, version: str, buttons: list[int]) -> None:
        if app_id.lower().startswith("mywhoosh"):
            log.info(
                "MyWhoosh %s connected -- Click buttons now drive its "
                "virtual gears", version or "(version unknown)",
            )
            self.record("app", f"MyWhoosh {version or ''} connected - Click drives its gears")
        elif app_id:
            log.info("app %s connected", app_id)
            self.record("app", f"app connected: {app_id}")
        else:
            log.info("trainer app connected (no app-info sent)")
            self.record("app", "trainer app connected")

    def _on_haptic(self, pattern: int, duration: int, intensity: int) -> None:
        if not self.haptics or self._click is None:
            return
        # Only forward the plain, safe vibration patterns.
        zwift_pattern = {1: 2, 2: 3, 3: 4}.get(pattern, 2)
        self._click.request_haptic(zwift_pattern)

    # ------------------------------------------------- control panel hooks
    async def set_pairing(self, enabled: bool) -> None:
        """Operator switch: enable/disable automatic pairing on BOTH sides.

        enabled=False  -> the bridge stops scanning/reconnecting the Click and
                          hides itself from mDNS (new app connects refused).
                          An already-running session keeps working.
        enabled=True   -> back to normal: advertise + scan + reconnect.
        """
        enabled = bool(enabled)
        if enabled == self.pairing_enabled:
            return
        self.pairing_enabled = enabled
        if enabled:
            self.record("pairing", "pairing ENABLED - scanning and advertising again")
            log.info("pairing ENABLED (control panel request)")
        else:
            self.record("pairing", "pairing PAUSED - no new connections (current session keeps running)")
            log.info("pairing PAUSED (control panel request)")
        click = self._click
        if click is not None and hasattr(click, "set_pairing"):
            click.set_pairing(enabled)
        want_mdns = enabled and not bool(self.cfg.get("no_mdns"))
        try:
            await self.server.set_advertising(want_mdns)
        except Exception as exc:  # pragma: no cover
            log.warning("advertising update failed(%r)", exc)
        self.server.pairing_enabled = enabled

    async def drop_all(self) -> None:
        """Kick the Click and every connected trainer app on request."""
        self.record("control", "operator requested disconnect (Click + apps)")
        click = self._click
        if click is not None and hasattr(click, "disconnect"):
            try:
                await click.disconnect()
            except Exception:
                pass
        await self.server.close_sessions()

    def snapshot(self) -> dict:
        """Full dashboard state; served by the control API."""
        click = self._click
        cfg = self.cfg
        buttons_pretty = {
            logical: {
                "press": [obc.action_name(a) for a in short],
                "long_press": [obc.action_name(a) for a in longs],
            }
            for logical, (short, longs) in self.mapping.items()
        }
        return {
            "app": APP_NAME,
            "version": self.version,
            "uptime_s": int(time.time() - self.started_at),
            "pairing_enabled": self.pairing_enabled,
            "mdns_advertising": getattr(self.server, "_info", None) is not None,
            "control_port": self.control_api.port if self.control_api else 0,
            "click": {
                "connected": bool(getattr(click, "connected", False)),
                "label": getattr(click, "device_label", "?"),
                "protocol": getattr(click, "protocol", None),
                "battery": getattr(click, "battery", None),
            },
            "apps": {
                "count": len(self.server.sessions),
                "ids": self.server.app_ids(),
            },
            "config": {
                "name": cfg.get("name"),
                "obc_port": cfg.get("port"),
                "host": cfg.get("host"),
                "haptics": bool(cfg.get("haptics", True)),
                "long_press_ms": self.long_press_ms,
                "buttons": buttons_pretty,
            },
            "events": list(self._events),
        }

    # ------------------------------------------------------------- runtime
    async def run(self, *, mock: MockClick | None = None) -> None:
        click: ZwiftClick | MockClick = mock if mock is not None else self._build_click()
        self._click = click
        await self.server.start()
        self.loop = asyncio.get_running_loop()
        self.control_api = ControlApi(self, int(self.cfg.get("control_port") or 0))
        self.control_api.start()
        click_task = click.start()
        status_task = asyncio.create_task(self._status_loop(), name="status")
        if mock is None:
            print("Bridge is running.  Waiting for the Click and for MyWhoosh "
                  "to connect -- see the banner above.")
        else:
            print("Bridge is running in MOCK mode.  " + MockClick._mock_help())
        self.record("bridge", f"{APP_NAME} {self.version} running"
                    + (" in MOCK mode" if mock else ""))
        try:
            await self._stopped.wait()
        finally:
            await self._teardown(click, click_task, status_task)

    async def _teardown(self, click, click_task, status_task) -> None:
        suppress = contextlib.suppress
        if self.control_api is not None:
            with suppress(Exception):
                self.control_api.stop()
            self.control_api = None
        try:
            click_task.cancel()
            status_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await click.stop()
            with suppress(asyncio.CancelledError, Exception):
                await self.server.stop()
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.gather(click_task, status_task, return_exceptions=True)
        except BaseException:  # during interpreter shutdown / Ctrl+C sweeps
            pass
        log.info("bridge closed")

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            with contextlib.suppress(Exception):
                await self.server.push_status()


# ------------------------------------------------------------- diagnostics

async def show_devices(duration: float) -> int:
    from bleak import BleakScanner

    print(f"Scanning for Zwift controllers for {duration:.0f} s ...")
    try:
        results = await BleakScanner.discover(timeout=duration, return_adv=True)
    except Exception as exc:
        print(
            f"Bluetooth scan failed: {exc}\n"
            "  - is Bluetooth switched on?  (Settings > Bluetooth & devices)\n"
            "  - desktop PC without a radio? plug in a USB Bluetooth dongle"
        )
        return 1
    found: dict[str, tuple] = {}
    pairs = (
        list(results.values()) if isinstance(results, dict) else list(results)
    )
    for dev, adv in pairs:
        info = classify_advertisement(dev.name, adv)
        if not info:
            continue
        found[dev.address.upper()] = (dev.name or "(no name)", adv, info)
    if not found:
        print(
            "No Zwift controllers found.  Tips:\n"
            "  - press any Click button to make it advertise again (LED!)\n"
            "  - the Click can pair with ONE app at a time; close the\n"
            "    Zwift app / Companion on the phone or another PC first\n"
            "  - keep it within a couple of metres and rescan\n"
            "  - use --watch to show diagnostic detail\n"
        )
        return 1
    print(f"\n{'RSSI':>5}  {'Address':17}  {'Kind':20} Name / services")
    for address, (name, adv, info) in sorted(found.items(), key=lambda kv: -kv[1][1].rssi):
        services = ",".join(sorted(u[-8:] for u in adv.service_uuids)) or "-"
        print(f"{adv.rssi:>5}  {address:17}  {info['kind']:20} {name.strip()} [{services}]"
              + (f"  mfg={info['mfg_hex']}" if info['mfg_hex'] else ""))
    print("\nTo force a specific unit:  clickbridge --address <ADDRESS>")
    return 0


async def selftest() -> int:
    sys.path.insert(0, str(PKG_ROOT / "tests"))
    import test_obc_protocol

    print(f"== {APP_NAME} {__version__} self-test ==")
    ok = True

    print("-- protocol codec tests ...", end=" ")
    if test_obc_protocol.run_all(verbose=False):
        print("OK")
    else:
        print("FAILED")
        ok = False

    print("-- crypto self-consistency ...", end=" ")
    try:
        from .zwiftcrypt import ZwiftCrypto

        a_scalar, pub_a = ZwiftCrypto.generate()
        b_scalar, pub_b = ZwiftCrypto.generate()
        # Alice plays the OpenBikeControl "app" side: salt = device||ours.
        alice = ZwiftCrypto(a_scalar, pub_a, pub_b, salt_order="device-first")
        # The (hypothetical) peer uses the mirrored convention for the same
        # shared secret ECDH(b, pub_a); only its salt order differs.
        bob = ZwiftCrypto(b_scalar, pub_b, pub_a, salt_order="app-first")
        frame = alice.encrypt(b"RideOn")
        assert bob.decrypt(frame) == b"RideOn", "crypto round trip"
        # Counter drift / resync: each new frame carries its own counter
        frame2 = alice.encrypt(b"again")
        assert bob.decrypt(frame2) == b"again", "crypto second frame"
        print("OK")
    except Exception as exc:  # pragma: no cover
        print(f"FAILED ({exc})")
        ok = False

    print("-- TCP loopback server ...", end=" ")
    seen = {"app": False, "haptic": 0, "stream": b""}

    def mark_app(app_id: str, version: str, buttons: list[int]) -> None:
        seen["app"] = app_id == "selftest-app" and len(buttons) == 2

    def mark_haptic(pattern: int, duration: int, intensity: int) -> None:
        seen["haptic"] += 1

    async def fake_client(port: int) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(obc.encode_app_info("selftest-app", "0.0.1", [0x01, 0x02]))
        await writer.drain()
        writer.write(bytes((obc.MSG_HAPTIC_FEEDBACK, 0x01, 0x00, 0x00)))
        await writer.drain()
        stream = b""
        # expect: 3-byte device-status + 5-byte button-state frame
        while len(stream) < 8:
            chunk = await asyncio.wait_for(reader.read(1024), timeout=3.0)
            if not chunk:
                break
            stream += chunk
        writer.close()
        return stream

    server = ObcServer(port=0, name="selftest", advertise=False,
                       on_app_info=mark_app, on_haptic=mark_haptic)
    await server.start()
    port = server.bound_port
    client_task = asyncio.create_task(fake_client(port))
    await asyncio.sleep(0.25)
    await server.broadcast_buttons([(0x01, 0x01), (0x01, 0x00)])
    await asyncio.sleep(0.35)
    await server.stop()
    with contextlib.suppress(Exception):
        stream = await client_task

    round_err = None
    if not seen["app"]:
        round_err = "app info was not reported"
    elif seen["haptic"] != 1:
        round_err = f"haptic callback count {seen['haptic']}"
    elif len(stream) < 8:
        round_err = f"stream short: {stream.hex()}"
    else:
        want_status = obc.encode_device_status(obc.BATTERY_UNKNOWN, False)
        want_buttons = bytes((0x01, 0x01, 0x01, 0x01, 0x00))
        if stream[:3] != want_status or stream[3:8] != want_buttons:
            round_err = f"frames mismatch: {stream.hex()}"
    if round_err:
        print(f"FAILED ({round_err})")
        ok = False
    else:
        print("OK")

    print()
    print("ALL OK" if ok else "SELFTEST FAILED")
    return 0 if ok else 1


# ---------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clickbridge",
        description=f"{APP_NAME}: Zwift Click -> MyWhoosh via OpenBikeControl",
    )
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                   help=f"path to config.json (default {DEFAULT_CONFIG_PATH})")
    p.add_argument("--watch", action="store_true", help="log raw BLE/OBC frames")
    p.add_argument("--address", metavar="AA:BB:CC:DD:EE:FF",
                   help="pin the Click by Bluetooth address")
    p.add_argument("--port", type=int, help="TCP port (default 38393)")
    p.add_argument("--control-port", type=int, metavar="PORT",
                   help="localhost control API for the dashboard "
                        "(default 38398, 0 = off)")
    p.add_argument("--name", help="name advertised to trainer apps")
    p.add_argument("--host", help="force the mDNS-advertised IP")
    p.add_argument("--no-mdns", action="store_true", help="TCP only, no mDNS")
    p.add_argument("--legacy-ecdh", action="store_true",
                   help="try the legacy encrypted controller handshake")
    p.add_argument("--haptic-off", action="store_true", help="disable haptics")
    p.add_argument("--long-press-ms", type=int, help="long-press threshold")
    p.add_argument("--mock", action="store_true",
                   help="simulate the Click (keyboard: u/d/p/b, q quits)")
    p.add_argument("--mock-period", type=float, metavar="SEC",
                   help="mock mode: alternate shift up/down every N seconds")
    p.add_argument("--show-devices", type=float, nargs="?", const=10.0,
                   metavar="SEC", help="list nearby Zwift controllers, exit")
    p.add_argument("--selftest", action="store_true",
                   help="run protocol + server checks, exit")
    p.add_argument("--save-config", action="store_true",
                   help="write the resolved config.json and exit")
    p.add_argument("--version", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"{APP_NAME} {__version__}")
        return 0

    overrides: dict[str, Any] = {
        "watch": True if args.watch else None,
        "address": args.address,
        "port": args.port,
        "control_port": args.control_port,
        "name": args.name,
        "host": args.host,
        "no_mdns": True if args.no_mdns else None,
        "legacy_ecdh": True if args.legacy_ecdh else None,
        "haptics": False if args.haptic_off else None,
        "long_press_ms": args.long_press_ms,
    }
    cfg = load_config(args.config, overrides)

    logging.basicConfig(
        level=logging.DEBUG if cfg.get("watch") else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.save_config:
        args.config.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        print(f"saved resolved config -> {args.config}")
        return 0
    if args.selftest:
        return asyncio.run(selftest())
    if args.show_devices is not None:
        return asyncio.run(show_devices(args.show_devices))

    print(BANNER.format(name=APP_NAME, version=__version__, config=args.config))

    bridge = Bridge(cfg, config_path=args.config)
    mock = None
    if args.mock or args.mock_period:
        mock = MockClick(bridge, period=args.mock_period)
    try:
        asyncio.run(bridge.run(mock=mock))
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
