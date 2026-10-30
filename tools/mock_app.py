#!/usr/bin/env python3
"""MockOpenBikeControl-capable trainer app (debug helper, like MyWhoosh's side).

Connects to a ClickBridge (or any OpenBikeControl device), reports itself as
a trainer app and prints every button frame in real time.  Use it to prove
ClickBridge's server side works independently of MyWhoosh.

  python tools/mock_app.py            # discover via mDNS (default)
  python tools/mock_app.py --host 127.0.0.1 --port 38393
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clickbridge import obc  # noqa: E402

SUPPORT = [
    0x01, 0x02,                      # shifting
    0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17,  # navigation
    0x18, 0x19,                      # steering
    0x20,                            # emote
    0x30, 0x31, 0x33,                # difficulty up/down, pause
    0x37,                            # u-turn
    0x40,                            # camera
]

STATUS_NAMES = {
    0x01: "pressed ",
    0x00: "released",
}
HAPTIC_STOP = object()


def describe(button_id: int) -> str:
    name = obc.ACTION_NAMES.get(button_id)
    return f"{name} [0x{button_id:02X}]" if name else f"0x{button_id:02X}"


async def run(host: str | None, port: int | None) -> int:
    if not (host and port):
        import zeroconf
        from zeroconf import ServiceBrowser, Zeroconf

        found: list = []
        done = asyncio.Event()

        class L:
            def add_service(self, zc, type_, name):
                info = zc.get_service_info(type_, name)
                if info:
                    addrs = info.parsed_addresses()
                    if addrs:
                        props = {k.decode(): (v or b"").decode() for k, v in info.properties.items()}
                        found.append((addrs[0], info.port, props.get("name", name)))
                        done.set()

            def update_service(self, zc, type_, name):
                pass

            def remove_service(self, zc, type_, name):
                pass

        zc = Zeroconf()
        ServiceBrowser(zc, obc.MDNS_SERVICE_TYPE, L())
        try:
            await asyncio.wait_for(done.wait(), timeout=8.0)
        except (asyncio.TimeoutError, asyncio.TimeoutError):
            pass
        finally:
            zc.close()
        if not found:
            print("No OpenBikeControl device discovered (is ClickBridge running?)")
            return 1
        host, port, name = found[0]
        print(f"Discovered {name!r} at {host}:{port}")
    elif port is None:
        port = 38393

    print(f"Connecting to {host}:{port} ...")
    reader, writer = await asyncio.open_connection(host, port)
    print("Connected. Advertising as 'mock-mywhoosh'.")

    app_info = obc.encode_app_info("mock-mywhoosh", "5.7.0", SUPPORT)
    writer.write(app_info)
    await writer.drain()

    try:
        while True:
            head = await reader.read(1)
            if not head:
                print("\nDevice closed the connection.")
                return 0
            mtype = head[0]
            if mtype == obc.MSG_DEVICE_STATUS:
                tail = await reader.readexactly(2)
                battery, connected = tail[0], tail[1]
                batt = "?" if battery == 0xFF else f"{battery}%"
                print(f"[status]   battery {batt}, controller "
                      f"{'ready' if connected else 'not ready'}")
            elif mtype == obc.MSG_BUTTON_STATE:
                chunk = await reader.read(64)
                pairs = list(zip(chunk[0::2], chunk[1::2]))
                for bid, state in pairs:
                    press = state == 0x01
                    print(f"[button]   {STATUS_NAMES.get(state, hex(state))} {describe(bid)}")
                    if press:
                        writer.write(bytes((obc.MSG_HAPTIC_FEEDBACK, 0x01, 0x00, 0x00)))
                        await writer.drain()
            elif mtype == obc.MSG_HAPTIC_FEEDBACK:
                tail = await reader.readexactly(3)
                print(f"[haptic<-] pattern {tail[0]}")
            else:
                extra = await reader.read(64)
                print(f"[unknown]  0x{mtype:02X} {extra.hex()}")
    except (KeyboardInterrupt, ConnectionError, asyncio.IncompleteReadError):
        print("\ndone.")
    finally:
        writer.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Mock OpenBikeControl trainer app")
    p.add_argument("--host", help="skip discovery, connect to this IP")
    p.add_argument("--port", type=int, help="TCP port (default 38393)")
    args = p.parse_args()
    try:
        return asyncio.run(run(args.host, args.port))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 0


if __name__ == "__main__":
    sys.exit(main())
