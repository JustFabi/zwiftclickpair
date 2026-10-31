#!/usr/bin/env python3
"""Rapid-tap investigation: measures the bridge's own event pipeline.

Everything runs through the REAL code path -- ZwiftClick's real decoder,
Bridge's real dedup logic, ObcServer's real TCP session -- with a real
TCP client replacing the app.  No BLE hardware involved.

  1. single taps (slow)         -> baseline latency       (id 0x10)
  2. fast up/down alternation   -> same, under cadence    (ids 0x11/0x12)
  3. frantic same-button bursts -> the reported case      (id 0x13)
  4. flood: press+release pairs injected with ZERO wait between them,
     i.e. all events land while the loop is busy            (id 0x14)
  5. real-bitmap scenarios: fed through the real wire-frame decoder,
     including the "one update releases primary bit AND presses second
     bit" blend (both bits collapse onto the same logical button).

Every phase uses its own unique OBC action id, so wire events can never
be confused across phases (that ambiguity sabotaged the first version of
this benchmark).

Run:  python tools/bench_tap_pipeline.py
"""

from __future__ import annotations

import asyncio
import contextlib
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clickbridge import obc  # noqa: E402
from clickbridge.__main__ import Bridge, DEFAULT_CONFIG  # noqa: E402
from clickbridge.clickdevice import ZwiftClick  # noqa: E402

# Click v2 keypad-bitmap facts (real captures in tests/test_obc_protocol.py):
IDLE = 0xFFFFFFFF            # all bits 1 = nothing pressed (inverse logic)
R1 = 0x1000                  # right pod shifter, primary contact  -> "up"
R2 = 0x2000                  # right pod shifter, second contact   -> "up"
L1 = 0x0100                  # left pod shifter, primary contact   -> "down"

# unique phase ids make (id, state) wire events unambiguous per phase
PHASE_ID = {
    "single": 0x10,
    "alt_up": 0x11,
    "alt_dn": 0x12,
    "frantic": 0x13,
    "flood": 0x14,
}

# the bench "app" declares support for every id it wants to see
DECLARED_IDS = [0x01, 0x02] + list(PHASE_ID.values())


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def frame(bitmap: int) -> bytes:
    """A real Click v2 keypad frame: 0x23 0x08 <varint bitmap>."""
    return b"\x23\x08" + varint(bitmap)


async def bench() -> None:
    loop = asyncio.get_running_loop()
    loop.set_debug(True)
    loop.slow_callback_duration = 0.05   # warn about any callback > 50 ms

    cfg = dict(DEFAULT_CONFIG)
    cfg["haptics"] = False
    cfg["no_mdns"] = True
    cfg["watch"] = False                 # keep the log line volume down
    bridge = Bridge(cfg, config_path=ROOT / "config.json")

    # extra logical buttons, one per phase, each with its own unique id
    for phase, oid in PHASE_ID.items():
        bridge.mapping[f"p_{phase}"] = ([oid], [])

    await bridge.server.start()
    port = bridge.server.bound_port
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(obc.encode_app_info("bench-app", "1.0", DECLARED_IDS))
    await writer.drain()

    wire: list[tuple[float, int, int]] = []          # (t, action id, state)

    async def client_reader() -> None:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                return
            t = time.monotonic()
            events, buf = [], chunk
            # robust incremental parse: valid button-state / status messages
            while len(buf) >= 3:
                if buf[0] == obc.MSG_DEVICE_STATUS and buf[2] in (0, 1):
                    events.append(None)
                    buf = buf[3:]
                elif msg_is_button(buf):
                    events.append((t, buf[1], buf[2]))
                    buf = buf[3:]
                else:
                    buf = buf[1:]
            for e in events:
                if e is not None:
                    wire.append(e)

    def msg_is_button(buf: bytes) -> bool:
        return (buf[0] == obc.MSG_BUTTON_STATE
                and buf[1] in DECLARED_IDS and buf[2] in (0x00, 0x01))

    reader_task = asyncio.create_task(client_reader())
    await asyncio.sleep(0.2)                      # let app-info settle

    dispatches: list[tuple[str, int, bool, float]] = []   # (phase, id, press, t)
    sync_ns: list[float] = []                     # cost of the dispatch call

    def dispatch(phase: str, pressed: bool) -> None:
        t_of = time.monotonic()
        start = time.perf_counter_ns()
        bridge._on_click_action("bench", f"p_{phase}", pressed, 0)
        sync_ns.append(time.perf_counter_ns() - start)
        dispatches.append((phase, PHASE_ID[phase], pressed, t_of))

    async def tap(phase: str, pressed: bool, hold: float, gap: float) -> None:
        dispatch(phase, pressed)
        await asyncio.sleep(hold if pressed else gap)

    # ------------------------- phase 1: single taps, slow ------------------
    for _ in range(6):
        await asyncio.sleep(0.60)
        await tap("single", True, 0.12, 0)
        await tap("single", False, 0, 0)
    await asyncio.sleep(0.35)

    # ------------------------- phase 2: fast alternation -------------------
    for _ in range(15):
        await asyncio.sleep(0.01)
        await tap("alt_up", True, 0.04, 0)
        await tap("alt_up", False, 0, 0)
        await tap("alt_dn", True, 0.04, 0)
        await tap("alt_dn", False, 0, 0)
    await asyncio.sleep(0.35)

    # ------------------------- phase 3: frantic same-button ----------------
    for _ in range(24):
        await asyncio.sleep(0.03)
        await tap("frantic", True, 0.03, 0)
        await tap("frantic", False, 0, 0)
    await asyncio.sleep(0.35)

    # ------------------------- phase 4: zero-spacing flood -----------------
    # 12 press+release pairs injected synchronously -- all 24 events exist
    # before the loop gets a chance to broadcast any of them.
    flood_start = time.monotonic()
    for _ in range(12):
        dispatch("flood", True)
        dispatch("flood", False)
    await asyncio.sleep(0.35)

    # ----------------------------- report ----------------------------------
    print("== orchestrator -> OBC wire (real Bridge + real TCP session) ==")
    ok_all = True

    def report(phase: str, oid: int, state: int, expected: int) -> None:
        nonlocal ok_all
        w = [wt for wt, i, s in wire
             if i == oid and s == state]
        d = sorted(t for p, i, press, t in dispatches
                   if p == phase and (1 if press else 0) == state)
        lat = [(w[i] - d[i]) * 1000.0 for i in range(min(len(w), len(d)))]
        missing = max(0, len(d) - len(w))
        extra = max(0, len(w) - len(d))
        ok = not missing and not extra
        ok_all &= ok
        flag = "ok " if ok else "BAD"
        if lat:
            print(f"  {flag} {phase:8} {'press  ' if state else 'release'}"
                  f" expected {expected:2}  delivered {len(d):2}  missing {missing}"
                  f"  extra {extra}   latency min {min(lat):6.2f}  avg"
                  f" {statistics.mean(lat):6.2f}  max {max(lat):6.2f} ms")
        else:
            print(f"  {flag} {phase:8} {'press  ' if state else 'release'}"
                  f" expected {expected:2}  delivered  0  missing {missing}")

    report("single", 0x10, 1, 6)
    report("single", 0x10, 0, 6)
    report("alt_up", 0x11, 1, 15)
    report("alt_up", 0x11, 0, 15)
    report("alt_dn", 0x12, 1, 15)
    report("alt_dn", 0x12, 0, 15)
    report("frantic", 0x13, 1, 24)
    report("frantic", 0x13, 0, 24)
    report("flood", 0x14, 1, 12)
    report("flood", 0x14, 0, 12)

    # order check for the flood: presses and releases must strictly alternate
    flood_wire = [s for _, i, s in wire if i == 0x14]
    want = [1, 0] * 12
    order_ok = flood_wire[:len(want)] == want  # order check on the prefix
    print(f"  {'ok ' if order_ok else 'BAD'} flood    wire-order press/release"
          f" alternation preserved: {order_ok}")

    sync_avg = statistics.mean(sync_ns) / 1000.0
    sync_max = max(sync_ns) / 1000.0
    print(f"  dispatch-call cost: avg {sync_avg:6.2f} us  max {sync_max:6.2f} us"
          f"  (n={len(sync_ns)})")

    # ---------------- phase 5: real-frame scenarios via the real decoder ---
    print("== Click v2 real-frame scenarios (decoder -> dedup -> OBC wire) ==")

    # exact captured bitmap values (idle = 0xFFFFFFFF, inverse logic):
    #   R1 only : 0xFFFFEFFF -> frame \x23\x08\xff\xdf\xff\xff\x0f
    #   R2 only : 0xFFFFDFFF -> frame \x23\x08\xff\xbf\xff\xff\x0f
    #   both    : 0xFFFFCFFF -> frame \x23\x08\xff\x9f\xff\xff\x0f
    right_press = frame(IDLE & ~R1)
    second_press = frame(IDLE & ~R2)
    both_held = frame(IDLE & ~(R1 | R2))
    idle_frame = frame(IDLE)

    async def scenario(name: str, frames: list[bytes], note: str) -> None:
        nonlocal ok_all
        nonlocal base
        nonlocal stage_t0
        base = len(wire)
        stage_t0 = time.monotonic()
        click = ZwiftClick(kind_id=11, watch=False)
        click.on_action = lambda logical, pressed, bit: bridge._on_click_action(
            "scen", logical, pressed, bit)
        bridge._active.clear()
        bridge._press_at.clear()
        for f in frames:
            click._parse_plain(f)
            await asyncio.sleep(0.002)       # ~ BLE notification pacing
        await asyncio.sleep(0.2)
        got = [(t - stage_t0, s) for t, i, s in wire[base:] if i == 0x01]
        presses = sum(1 for _, s in got if s)
        releases = sum(1 for _, s in got if not s)
        print(f"  {name}")
        print(f"    physical taps=2 -> wire: {presses} press, {releases} release"
              f" edges   [{note}]")
        fordt = [f"{s:+d} press@+{(t) * 1000:6.1f}ms" for t, s in got]
        print(f"    wire timeline: {', '.join(fordt) if fordt else '(none)'}")

    base = len(wire)
    stage_t0 = wire[-1][0] if wire else time.monotonic()

    # A full press->release->press->release, both contacts distinct frames.
    await scenario("A clean double tap (control)",
                   [idle_frame, right_press, idle_frame, right_press,
                    idle_frame],
                   "both taps must land")
    # B: stage-2 contact engages while stage-1 releases INSIDE one notification.
    #    REGRESSION (pre-fix): second press eaten by dedup + release fired
    #    mid-press.  FIXED: one clean down-up cycle; release at final idle.
    await scenario("B stage roll inside press (blend frame)",
                   [idle_frame, right_press, second_press, idle_frame],
                   "fixed: contact swap keeps the button down, no early release")
    # C: tap 2 lands while still on stage 2 -- the frame releases stage 2 and
    #    presses stage 1 in ONE notification.  REGRESSION (pre-fix): press
    #    and release emitted in the same millisecond (0-length pulse the app
    #    drops).  FIXED: the logical button stays down; single clean cycle.
    await scenario("C re-press while rolling back (0-pulse)",
                   [idle_frame, right_press, second_press, right_press,
                    idle_frame],
                   "fixed: no zero-length press pulse")
    # D: the release of tap 1 is never transmitted (sampling gap) -- tap 2's
    #    frame equals tap 1's bitmap, so the _emit_bitmap "changed?" check at
    #    clickdevice.py:790 suppresses it entirely
    await scenario("D release frame lost (sampling gap)",
                   [idle_frame, right_press, right_press, idle_frame],
                   "second press invisible: bitmap == previous")

    writer.close()
    reader_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await reader_task
    await bridge.server.stop()
    print(f"== {'ALL DELIVERED PROMPTLY' if ok_all else 'SEE BAD LINES ABOVE'} ==")


if __name__ == "__main__":
    asyncio.run(bench())
