# ClickBridge

**Use your Zwift Click (also Zwift Play / Zwift Ride controllers) inside MyWhoosh — for real, with no native Zwift support needed.**

MyWhoosh 5.7+ (April 2026) ships with native support for the open, vendor-neutral
[OpenBikeControl](https://github.com/OpenBikeControl/openbikecontrol-protocol) protocol
(MIT-licensed, free to implement). Zwift’s own controllers speak a different proprietary
channel — so something has to translate. BikeControl is the well-known commercial bridge;
ClickBridge is a **free, open, local, no-account, no-cloud** translator that you can run on
this very PC.

```
                    BLE (Click protocol)                mDNS + TCP (OpenBikeControl)
   ┌─────────────┐  "RideOn" / keypadds   ┌──────────────┐  _openbikecontrol._tcp   ┌─────────────┐
   │  Zwift Click │ ─────────────────────▶ │  ClickBridge │ ────────────────────────▶ │   MyWhoosh  │
   └─────────────┘   button up/down bits   └──────────────┘   0x01 button state frames  └─────────────┘
                                                          (virtual shifting inside MyWhoosh)
```

* The Click’s **press and release edges** arrive as-is (no keystroke hacks, no latency tricks)
  and become OpenBikeControl `Shift Up` / `Shift Down` actions.
* MyWhoosh can run **on this PC, an iPad, an Apple TV, or a phone** — it only needs to be on
  the same network as this PC.

---

## Quickstart

1. **Install** (only once): double-click `install.bat` — needs Python 3.12+ from [python.org](https://python.org) (also pre-installed on many systems).
2. **Run** `run_clickbridge.bat`. Keep the black window open (that *is* the app) while riding.
3. **Turn the Click on** — press any button so the LED is awake/flashing.
   Don’t have it connected to the Zwift app or the Zwift Companion app at the same time
   (it pairs to exactly one device).
4. In **MyWhoosh**: device/connection screen → **OpenBikeControl icon (top right)** →
   confirm the **Yes** popup that names `ClickBridge`.
5. Shift. In the bridge window you’ll see `Shift Up pressed` lines as you press.

### If ClickBridge never appears in MyWhoosh

* Run `firewall.bat` **once as administrator** (allows inbound TCP 38393 + mDNS UDP 5353).
* Laptop on Wi-Fi with hotspot/router “client isolation”? You need both devices on the same
  subnet without isolation. On a single PC, loopback works automatically — no firewall needed
  in practice, but the rule does not hurt.
* Multi-NIC machine (VPN adapter, Hyper-V/WSL Ethernet)? Force the right interface:
  `clickbridge --host 192.168.1.42` (see `ipconfig`) and save with `--save-config`.

---

## Raspberry Pi / Linux quickstart (this machine)

1. `./install.sh` — virtualenv + Python deps, panel deps (composer, app key, sqlite migrate).
2. `sudo ./setup_vhost.sh` — unblocks Bluetooth, installs + starts both systemd units and
   the Apache vhost.
3. After that everything survives reboots:
   * Bridge → systemd unit `clickbridge` (logs: `journalctl -u clickbridge -f`)
   * Panel → Apache vhost **http://clickbridge.local** (alias `zwiftclickpair.local`),
     published on the LAN via the `clickbridge-alias` unit (`avahi-publish`).
     No .local support on the client? The same panel is the port default at
     **http://\<Pi-IP\>:8090** — so it coexists with other Apache vhosts
     (e.g. `mywhooshgarminsync.local` keeps http://\<Pi-IP\>:80 on this box).
     If you skip step 2 on a non-Apache box: `./run_clickbridge.sh` in one terminal and
     `./run_panel.sh` for the `php artisan serve` panel at `http://localhost:8080`.
4. Firewall: Debian ships with no filtering enabled by default. If you run `ufw`:
   `sudo ufw allow 38393/tcp && sudo ufw allow 5353/udp`.
5. Debug commands from the Windows quickstart work the same way, e.g.
   `./run_clickbridge.sh --show-devices`.

Gotchas specific to the Pi:
* Bluetooth must not be rfkill-soft-blocked (`rfkill list` → `Soft blocked: no`).
* The mDNS alias names are published with `avahi-publish` — do **not** use
  `/etc/avahi/hosts` for this: entries sharing the machine's own address collide on the
  reverse lookup (`Local name collision`) and get dropped on every avahi restart.

---

## Control panel (dashboard) — optional

`run_clickbridge.bat` alone is all you need for riding. The Laravel panel adds a web UI:

1. Requirements: **PHP 8.2+ and Composer** (present on this machine; a XAMPP install works fine).
2. First time only: `cd panel && composer install`.
3. Just run **`run_panel.bat`** — it starts the bridge and the panel and opens
   `http://localhost:8080`. From a phone/TV on the same network, open `http://<PC-IP>:8080`
   instead (allow PHP through the Windows firewall when asked).

What you get:

* **Automatic pairing switch** — the headline feature. **OFF** stops the bridge from scanning
  for the Click *and* unregisters it from MyWhoosh's discovery; a running session keeps
  working. **ON** resumes scanning + advertising. (Also on `POST /api/pairing`.)
* Live status: Click connection state, battery bar, connected trainer apps, mDNS state, mapping.
* **Event journal** — last 200 events, refreshed ~1.5 s (shift presses, app connects, battery, …).
* **Disconnect everything** button (drops the Click and all app sessions; pairing
  resumes automatically the next time a trainer app opens a session).

Battery-saving auto-release: the Click pods only power down when no host keeps a
connection. ClickBridge therefore releases them `auto_release_s` (default 30) seconds
after the last trainer-app session closes — they sleep, and re-pair automatically the
next time MyWhoosh (or any OpenBikeControl app) connects. A pod that went asleep may
need one button press to wake and advertise; the bridge pairs it again the moment it
shows up. Between sessions the bridge also stops scanning (silent BLE radio).

Security model: the Python control API binds to **127.0.0.1 only** — nothing else on the
network can reach it. Browsers go through the Laravel app (standard CSRF protection) at
`panel/` (`BRIDGE_URL` in `panel/.env`). Laravel 12 requires PHP ≥ 8.2; if you upgrade PHP
to 8.3+, you may run `composer update` to move to Laravel 13.

---

## The Click quirks you should know about

* **Click v1** (2023, just two buttons): speaks the plain “RideOn” protocol; it should just
  work. Wake it with a button press first; it stops advertising when asleep or when
  something else holds its connection.
* **Click v2** (Cog&Click kit / bundled with trainers,irmware 1.1/1.2): Zwift gates it behind
  a daily 24-hour “unlock”: without a fresh unlock the **buttons go silent about a minute
  after connecting** (connection stays perfectly alive!). Two ways around it:
  1. **Daily 30-second ritual**: open the Zwift app (desktop or mobile, apps other than
     Companion), log in with a free account (no subscription needed!), go to the device
     screen, connect the Click, press one of its buttons, wait ~20-30 seconds, close Zwift.
     Perfect for the rest of the day in ClickBridge + MyWhoosh.
  2. ClickBridge keeps auto-reconnecting regardless; combined with the shortcut above the
     experience is: press-up ritual once a day, then shift all day.
  *Third-party note: BikeControl 6.4+ also offers a “right side only” mode that skips the
  unlock — that trick depends on their proprietary emulator and cannot be reproduced here.*

---

## Configuration

Default settings live in memory; **config.json** (created by `--save-config`) overrides them.

| Key            | Meaning                                                            |
|----------------| --------------------------------------------------------------------|
| `name`         | Device name shown in MyWhoosh’s OpenBikeControl popup (def `ClickBridge`) |
| `port`         | TCP port (default `38393`)                                          |
| `host`         | Force mDNS interface IP, else auto                                  |
| `control_port` | Port of the local control API for the panel (default `38398`, `0` = off). Binds 127.0.0.1 only |
| `buttons`      | per-physical-button mapping (see below)                             |
| `long_press_ms`| threshold for the long-press gesture (default 800)                  |
| `haptics`      | forward app vibration requests to the Click                        |
| `auto_release_s`| release the Clicks this many seconds after the last trainer-app session closes so they can power down (default `30`, `0` = hold forever) |
| `click_bits`   | remap keypad bitmap bits (VALUES: 2, 8, 256, 4096 …) → logical, if your firmware differs |
| `legacy_ecdh`  | use the encrypted legacy handshake (very old firmware)              |
| `watch`        | raw-frames diagnostics                                             |

Example — make holding `up` do a **U-Turn**, and keep plain shifting on `down`:

```json
{
  "buttons": {
    "up":   { "actions": [],              "long_press": ["uturn"] },
    "down": { "actions": ["shift_down"],  "long_press": [] }
  }
}
```

More useful actions (any number can be combined per button): `shift_up`, `shift_down`,
`difficulty_up`, `difficulty_down`, `pause`, `resume`, `lap`, `skip_interval`,
`previous_interval`, `uturn`, `change_mode`, `take_break`, `emote`, `screenshot`,
`camera_view`, `hud_toggle`, `map_toggle`, `cruise_control`, powerups (`powerup_1..3`),
steering (`steer_left`/`steer_right`) and raw ids like `"0x14"`.
MyWhoosh decides which of them actually do something (its partner integration currently
supports shifting, steering, camera, nav and a few extras).

Multiple actions per button are supported exactly like the spec suggests, e.g.
`"actions": ["shift_up", "difficulty_up"]` — apps pick the ones they implement.

---

## Debugging & testing without MyWhoosh

```text
run_clickbridge.bat --show-devices        # list nearby Zwift controllers (+ --watch for raw)
run_clickbridge.bat --watch               # verbose: BLE frames, OBC frames, app info
run_clickbridge.bat --mock                # NO hardware: press u/d in this window instead;
                                          #   proves MyWhoosh <-> bridge wiring end-to-end
run_clickbridge.bat --mock --address AA:BB:CC:DD:EE:FF
python tools\mock_app.py                  # fake MyWhoosh: prints every button event
python tools\demo_loopback.py             # 10-second self-contained end-to-end demo (no hardware)
python tests\test_obc_protocol.py         # protocol codec unit tests
run_clickbridge.bat --selftest            # codec + crypto + TCP loopback checks
```

Typical first-time sequence on a new machine:
1. `run_clickbridge.bat --show-devices 10` → find the Click’s address and see its kind.
2. `run_clickbridge.bat --watch` → confirm it connects (`controller ready`) before opening MyWhoosh.
3. Open MyWhoosh; watch the bridge log `app mywhoosh ... connected`.

Every command is also reachable through `python -m clickbridge ...` if you prefer.

---

## How the BLE side works (for hackers)

* Discovery: advertising mapper — Zwift’s manufacturer ID `0x094A`, device kind `9` = Click,
  plus service UUIDs (`00000001-19ca-4651-86e5-fa29dcdd09d1` legacy, `0xFC82` newer).
* Handshake: write ASCII `RideOn` to the control point characteristic
  (`00000003-19ca-…`), notifications arrive on the measurement characteristic
  (`00000002-19ca-…`). Idle heartbeats: `0x15` / `0x19`.
* The keypad is a protocol-buffer message (id `0x23`) carrying a 32-bit
  bitmap with **inverse logic** (`1 = released`, `0 = pressed`), verified
  against hardware captures ([MarcelCode's protocol doc](https://github.com/MarcelCode/zwift-play-remote/blob/main/docs/protocol.md),
  SwiftControl's decoded map and our own live capture from a Click v2 pair):
  bit `0x1` left, `0x2` up, `0x4` right, `0x8` down, `0x10` A, `0x20` B,
  `0x40` Y, `0x80` Z, `0x100` shifter L (pressed), `0x200` shifter-L alt,
  `0x400` power-up L, `0x800` on/off L, `0x1000` shifter R,
  `0x2000` shifter-R alt, `0x4000` power-up R, `0x8000` on/off R.
  Final proof from the live wire: pressing the left pod's shift-down button
  yields bitmap `fffffeff` (bit `0x100`), pressing the right pod's
  shift-up button yields `ffffefff` (bit `0x1000`).
  Defaults per generation: on a **Click v2 pair** (new Zwift Cog kit) the
  LEFT pod's shifters (`0x100/0x200`) shift **down** and the RIGHT pod's
  (`0x1000/0x2000`) shift **up** — Zwift's own Cog semantics (left pod =
  shift down, right pod = shift up, matching what the game itself does) —
  the D-pad navigates, A selects, B backs, Y fires a power-up and Z
  brakes; an **original Click v1** shifts with its two buttons (older
  firmware reports them on bits `0x2/0x8`, firmware 1.1.0+ sends the
  `0x37` protobuf message instead). Both halves of a v2 pair reinforce
  each other's presses on the bus — the bridge merges them so one
  physical press stays one press. Remap anything via `click_bits`
  (keys are the bit values above).
* Haptics (`0x12 08 0A 06 08 02 10 00 18 <pattern>`), battery (`0x180F`/`2A19`) are wired
  as bonus niceties.
* Legacy really-old firmware (pre-2024 encrypted) is supported behind `--legacy-ecdh`
  (ECDH P-256 + HKDF-SHA256 + AES-CCM reverse-engineered as documented at
  [makinolo.com](https://www.makinolo.com/blog/2023/10/08/connecting-to-zwift-play-controllers/)).

Key sources on the OpenBikeControl side: [PROTOCOL.md](https://github.com/OpenBikeControl/openbikecontrol-protocol/blob/main/PROTOCOL.md),
[MDNS.md](…/MDNS.md), [BLE.md](…/BLE.md). ClickBridge’s implementation is carefully aligned
with the reference `mock_device_tcp.py` example, so it should work with all “Implemented”
trainer apps (MyWhoosh, TrainingPeaks Virtual, Strappo, Biketerra).

## File map

```
clickbridge/           the bridge (plain Python, runs standalone)
  __main__.py          CLI, orchestrator, mock mode, selftest
  clickdevice.py       BLE discovery + Click protocol decoding
  obc_server.py        OpenBikeControl mDNS + TCP device
  obc.py               OBC binary codec + action table
  zwiftcrypt.py        legacy encrypted handshake math
  control_api.py       localhost status/control API for the panel
panel/                 Laravel 12 web control panel (optional)
  app/Http/Controllers/BridgeController.php
  resources/views/dashboard.blade.php
tools/mock_app.py      fake trainer app for live testing
tools/demo_loopback.py 10-second end-to-end demo, no hardware
tests/                 protocol tests
install.bat / run_clickbridge.bat / run_panel.bat / firewall.bat (Windows)
install.sh / run_clickbridge.sh / run_panel.sh / setup_vhost.sh (Linux / Raspberry Pi)
clickbridge.service / clickbridge-alias.service / publish_aliases.sh / zwiftclickpair.conf
  (systemd units + Apache vhost template, installed by setup_vhost.sh)
```

Hackers' cheat sheet — the control API (bridge must be running):

```text
curl http://127.0.0.1:38398/api/status                  # full JSON state + events
curl -X POST -d "{\"enabled\":false}" -H "Content-Type: application/json" \
     http://127.0.0.1:38398/api/pairing                  # stop automatic pairing
curl -X POST http://127.0.0.1:38398/api/disconnect       # drop Click + app sessions
```

Licensed MIT. No network egress, no telemetry, no cloud — everything stays between the
Click, this PC, and your trainer app.
