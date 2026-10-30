#!/usr/bin/env python3
"""One-shot loopback demo: proves ClickBridge's OpenBikeControl server side.

Starts ClickBridge in MOCK mode (no Click hardware), connects a mini
trainer-app client over TCP, sends app-info, and counts the Shift Up/Down
button frames that arrive.  Exits 0 on success.

  python tools/demo_loopback.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clickbridge import obc  # noqa: E402

PORT = 39911
WINDOW_S = 7.0
CONTROL_PORT = 38398


async def _count_shifts(reader: asyncio.StreamReader, window_s: float) -> int:
    """Count PRESSED button events arriving within a time window."""
    count = 0
    buf = b""
    deadline = time.monotonic() + window_s
    while time.monotonic() < deadline:
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=0.3)
        except TimeoutError:
            continue
        if not chunk:
            break
        buf += chunk
        while len(buf) >= 3:
            msg = buf[0]
            if msg == obc.MSG_DEVICE_STATUS:
                buf = buf[3:]
            elif msg == obc.MSG_BUTTON_STATE:
                if buf[2] == 0x01:
                    count += 1
                buf = buf[3:]
            else:
                buf = buf[1:]
    return count


async def _probe_connection(port: int) -> str:
    """Probe a NEW app connection: 'closed' (refused) or 'open' (greeted)."""
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return "closed"
    try:
        data = await asyncio.wait_for(r.read(64), timeout=1.5)
    except TimeoutError:
        data = None
        open_unknown = True
    else:
        open_unknown = False
    finally:
        try:
            w.close()
        except Exception:
            pass
    if open_unknown:
        return "unknown"
    return "open" if data else "closed"


async def main() -> int:
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "clickbridge",
         "--mock-period", "1.5", "--no-mdns", "--port", str(PORT),
         "--name", "SelfTest"],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    print(f"bridge started (pid {proc.pid}) -- mock Click, no mDNS, port {PORT}")
    verdict = "NOT RUN"
    reader = writer = None
    try:
        for _ in range(60):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
                break
            except OSError:
                await asyncio.sleep(0.25)
        if reader is None:
            print("FAILED: bridge never opened its port")
            return 1

        writer.write(obc.encode_app_info("demo-app", "1.0", [0x01, 0x02]))
        await writer.drain()

        shift_up = shift_down = status_frames = 0
        buf = b""
        deadline = time.monotonic() + WINDOW_S
        while time.monotonic() < deadline:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=0.4)
            except TimeoutError:
                continue
            if not chunk:
                break
            buf += chunk
            while buf:
                msg = buf[0]
                if msg == obc.MSG_DEVICE_STATUS and len(buf) >= 3:
                    status_frames += 1
                    buf = buf[3:]
                elif msg == obc.MSG_BUTTON_STATE and len(buf) >= 3:
                    # single pair per frame in this demo: [0x01, id, state]
                    bid, state = buf[1], buf[2]
                    name = obc.action_name(bid)
                    print(f"  received: 0x{bid:02X} {name} -> "
                          f"{'PRESSED' if state else 'released'}")
                    if bid == 0x01 and state:
                        shift_up += 1
                    if bid == 0x02 and state:
                        shift_down += 1
                    buf = buf[3:]
                else:
                    break

        if status_frames and shift_up and shift_down:
            verdict = (f"PASS: {shift_up} ShiftUp + {shift_down} ShiftDown presses, "
                       f"{status_frames} status frames")
            rc = 0
        else:
            verdict = (f"FAIL: shift_up={shift_up} shift_down={shift_down} "
                       f"status={status_frames}")
            rc = 1

        # ---- control API exercise (status / pairing toggle) ----
        if rc == 0:
            await asyncio.sleep(0.3)

            def _request(path: str, method: str = "GET",
                         payload: bytes | None = None):
                import urllib.request
                req = urllib.request.Request(
                    f"http://127.0.0.1:{CONTROL_PORT}{path}",
                    data=payload, method=method,
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, json.loads(resp.read().decode())

            print("\n-- control API --")
            code, st = await asyncio.to_thread(_request, "/api/status")
            checks = [
                ("status 200", code == 200),
                ("pairing enabled by default", st.get("pairing_enabled") is True),
                ("click reported", st["click"]["connected"] is True),
                ("app session counted", st["apps"]["count"] == 1),
                ("events recorded", len(st["events"]) > 0),
                ("mdns hidden (demo runs --no-mdns)", st.get("mdns_advertising") is False),
            ]
            stub = json.dumps({"enabled": False}).encode()
            _, st2 = await asyncio.to_thread(
                _request, "/api/pairing", "POST", stub)
            checks.append(("pause accepted",
                           st2.get("pairing_enabled") is False))
            await asyncio.sleep(0.4)
            _, st3 = await asyncio.to_thread(_request, "/api/status")
            checks.append(("status shows pairing paused",
                           st3.get("pairing_enabled") is False))
            probe = await _probe_connection(PORT)
            checks.append(("new app connections refused while paused",
                           probe == "closed"))
            live = await _count_shifts(reader, 2.0)
            checks.append(("existing session keeps streaming (by design)",
                           live >= 1))
            stub = json.dumps({"enabled": True}).encode()
            await asyncio.to_thread(_request, "/api/pairing", "POST", stub)
            await asyncio.sleep(0.4)
            _, st4 = await asyncio.to_thread(_request, "/api/status")
            checks.append(("pairing re-enabled", st4.get("pairing_enabled") is True))
            probe = await _probe_connection(PORT)
            checks.append(("new app connections accepted again",
                           probe == "open"))
            for label, ok in checks:
                print(f"  {'ok ' if ok else 'FAIL'}  {label}")
            if not all(ok for _, ok in checks):
                verdict += " (control API FAILED)"
                rc = 1
    except Exception as exc:  # pragma: no cover
        verdict = f"FAIL ({exc})"
        rc = 1
    finally:
        if writer is not None:
            writer.close()
        proc.terminate()
        try:
            out = proc.communicate(timeout=5)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            out = proc.communicate()[0]
        print("\n--- bridge log (tail) ---")
        print("\n".join(out.splitlines()[-12:]))
    print(f"\n{verdict}")
    return rc


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
