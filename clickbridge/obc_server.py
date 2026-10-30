"""OpenBikeControl device-side server: mDNS advertisement + TCP sessions.

ClickBridge acts as an OpenBikeControl *device*.  Trainer apps (MyWhoosh and
other OBC implementors) discover it via mDNS under
``_openbikecontrol._tcp.local.`` and connect over TCP; the binary message
format is identical to the BLE transport:

  device -> app : 0x01 button state, 0x02 device status
  app -> device : 0x03 haptic feedback, 0x04 app information
"""

from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from typing import Callable

from zeroconf import IPVersion, NonUniqueNameException, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from . import __version__
from .obc import (
    MDNS_SERVICE_TYPE,
    OBC_PRIMARY_SERVICE_UUID,
    BATTERY_UNKNOWN,
    parse_app_info_message,
    parse_haptic_feedback,
    encode_device_status,
)

log = logging.getLogger("obc")

AppInfoCallback = Callable[[str, str, list[int]], None]
HapticCallback = Callable[[int, int, int], None]


def candidate_ips(only: str | None = None) -> list[str]:
    """Best-effort list of local IPv4 addresses worth advertising."""
    if only:
        return [only]
    ips: dict[str, None] = {}
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.settimeout(0)
            probe.connect(("192.0.2.1", 9))
            ips[probe.getsockname()[0]] = None
        finally:
            probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith(("127.", "169.254.")):
                continue
            ips.setdefault(ip, None)
    except OSError:
        pass
    return list(ips)


class ClientSession:
    """One connected trainer app."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 on_app_info: AppInfoCallback, on_haptic: HapticCallback):
        self.reader = reader
        self.writer = writer
        self.on_app_info = on_app_info
        self.on_haptic = on_haptic
        self.peer: str = "-"
        self.app_id: str | None = None
        self.supported: set[int] | None = None  # None == support everything
        self._closed = False
        self._wlock = asyncio.Lock()

    # ----------------------------------------------------------- lifecycle
    async def serve(self) -> None:
        peer = self.writer.get_extra_info("peername")
        self.peer = f"{peer[0]}:{peer[1]}" if peer else "?"
        log.info("app connected (%s)", self.peer)
        try:
            while True:
                head = await self.reader.read(1)
                if not head:
                    break
                mtype = head[0]
                if mtype == 0x04:  # app info
                    msg = await self._read_app_info(head)
                    if msg is None:
                        break
                    info = parse_app_info_message(msg)
                    self.app_id = info["app_id"]
                    buttons = info["supported_buttons"]
                    self.supported = set(buttons) if buttons else None
                    log.info(
                        "app %s v%s%s connected%s",
                        self.app_id or "(unnamed)",
                        info["app_version"] or "?",
                        "" if self.app_id else f" [raw {msg[1:].hex()}]" if self.app_id is None else "",
                        " -- all actions accepted" if self.supported is None
                        else f" -- {len(self.supported)} action type(s)",
                    )
                    self.on_app_info(self.app_id or "", info["app_version"], buttons)
                elif mtype == 0x03:  # haptic feedback
                    tail = await self.reader.readexactly(3)
                    data = parse_haptic_feedback(head + tail)
                    self.on_haptic(data["pattern"], data["duration"], data["intensity"])
                else:
                    log.warning("unknown OBC message type 0x%02X from %s", mtype, self.peer)
                    extra = await self.reader.read(64)
                    self._debug_unknown(head + extra)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError, OSError):
            pass
        finally:
            await self.close()

    def _debug_unknown(self, data: bytes) -> None:
        logging.getLogger("obc.debug").debug("unknown frame from %s: %s", self.peer, data.hex())

    async def _read_app_info(self, head: bytes) -> bytes | None:
        try:
            header = await self.reader.readexactly(2)          # version, id_len
            app_id = await self.reader.readexactly(header[1])
            ver_len = (await self.reader.readexactly(1))[0]
            app_version = await self.reader.readexactly(ver_len)
            count = (await self.reader.readexactly(1))[0]
            ids = await self.reader.readexactly(count)
        except asyncio.IncompleteReadError:
            return None
        return head + header + app_id + bytes((ver_len,)) + app_version + bytes((count,)) + ids

    # ------------------------------------------------------------- output
    async def send(self, msg: bytes) -> None:
        if self._closed:
            return
        try:
            async with self._wlock:
                self.writer.write(msg)
                await self.writer.drain()
        except (ConnectionError, OSError, asyncio.CancelledError):
            await self.close()

    async def send_buttons(self, pairs: list[tuple[int, int]]) -> bool:
        if self._closed or not pairs:
            return False
        if self.supported is not None:
            pairs = [(b, s) for (b, s) in pairs if b in self.supported]
            if not pairs:
                return False
        out = bytearray((0x01,))
        for b, s in pairs:
            out += bytes((b, s))
        await self.send(bytes(out))
        return True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.writer.close()
        except Exception:
            pass
        log.info("app disconnected (%s)", self.peer)


class ObcServer:
    """Advertises the ClickBridge as an OpenBikeControl device."""

    def __init__(
        self,
        *,
        port: int = 38393,
        name: str = "ClickBridge",
        host: str | None = None,
        advertise: bool = True,
        on_app_info: AppInfoCallback | None = None,
        on_haptic: HapticCallback | None = None,
    ):
        self.port = port
        self.name = name
        self.host = host
        self.advertise = advertise
        self.on_app_info_cb = on_app_info or (lambda *a, **k: None)
        self.on_haptic_cb = on_haptic or (lambda *a, **k: None)

        self._server: asyncio.AbstractServer | None = None
        self._aiozc: AsyncZeroconf | None = None
        self._info: ServiceInfo | None = None
        self.sessions: set[ClientSession] = set()
        self.device_id = format(uuid.getnode(), "012x")
        self.battery: int | None = None
        self.click_connected = False
        self.pairing_enabled = True

    # ------------------------------------------------------------ managing
    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client_connected, "0.0.0.0", self.port)
        socks = ", ".join(str(s.getsockname()) for s in self._server.sockets)
        self.bound_port = self._server.sockets[0].getsockname()[1]
        log.info("TCP server listening on %s (port %d)", socks, self.bound_port)

        if not self.advertise:
            log.info("mDNS advertisement disabled")
            return
        ips = candidate_ips(self.host)
        if not ips:
            log.warning("no usable local IPv4 address found; advertising on default interfaces")
        try:
            self._aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        except Exception as exc:  # pragma: no cover
            log.warning("mDNS unavailable (%s) -- continuing TCP-only", exc)
            self._aiozc = None
        await self._register(ips)

    async def _register(self, ips: list[str]) -> None:
        assert self._aiozc is not None
        props = {
            "version": "1",
            "id": self.device_id,
            "name": self.name,
            "service-uuids": OBC_PRIMARY_SERVICE_UUID,
            "manufacturer": "ClickBridge",
            "model": "Zwift Click -> OpenBikeControl bridge",
        }
        kwargs = dict(
            port=self.port,
            properties=props,
            server=f"{socket.gethostname().split('.')[0]}.local.",
        )
        if ips:
            kwargs["addresses"] = [socket.inet_aton(ip) for ip in ips]
        for attempt in range(2, 10):
            display = self.name if attempt == 2 else f"{self.name} ({attempt - 1})"
            info = ServiceInfo(MDNS_SERVICE_TYPE, f"{display}.{MDNS_SERVICE_TYPE}", **kwargs)
            try:
                await self._aiozc.async_register_service(info)
                self._info = info
                log.info(
                    "advertising %s as %s via %s:%d  (id=%s)",
                    MDNS_SERVICE_TYPE,
                    display,
                    ",".join(ips) or "default interfaces",
                    self.port,
                    self.device_id,
                )
                return
            except NonUniqueNameException:
                continue
        log.warning("could not register mDNS service (name collisions exhaust)")

    async def set_advertising(self, enabled: bool) -> None:
        """Show/hide the mDNS service (pairing toggle; TCP stays up)."""
        if enabled:
            if self._info is not None:
                return
            if self._aiozc is None:
                try:
                    self._aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
                except Exception as exc:  # pragma: no cover
                    log.warning("mDNS unavailable (%s) -- cannot advertise", exc)
                    return
            await self._register(candidate_ips(self.host))
        else:
            if self._aiozc is not None and self._info is not None:
                try:
                    await self._aiozc.async_unregister_service(self._info)
                except Exception:
                    pass
            self._info = None
            log.info("mDNS service removed (not discoverable)")

    async def close_sessions(self) -> None:
        for session in list(self.sessions):
            try:
                await session.close()
            except Exception:
                pass
        self.sessions.clear()

    def app_ids(self) -> list[str]:
        return [s.app_id for s in self.sessions if s.app_id]

    async def stop(self) -> None:
        for session in list(self.sessions):
            await session.close()
        self.sessions.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._aiozc is not None and self._info is not None:
            try:
                await self._aiozc.async_unregister_service(self._info)
                await self._aiozc.async_close()
            except Exception:
                pass
            self._aiozc = None
        log.info("OpenBikeControl server stopped")

    # ------------------------------------------------------------ sessions
    def _client_connected(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not self.pairing_enabled:
            peer = writer.get_extra_info("peername")
            log.info("refusing new app connection from %s (pairing disabled)", peer)
            writer.close()
            return
        session = ClientSession(reader, writer, self.on_app_info_cb, self.on_haptic_cb)
        self.sessions.add(session)
        asyncio.create_task(self._serve(session), name="obc-session")

    async def _serve(self, session: ClientSession) -> None:
        await self.push_status(session)
        try:
            await session.serve()
        finally:
            self.sessions.discard(session)

    # ------------------------------------------------------------ emitting
    async def push_status(self, session: ClientSession | None = None) -> None:
        battery = self.battery if self.battery is not None else BATTERY_UNKNOWN
        msg = encode_device_status(battery, self.click_connected)
        targets = [session] if session is not None else list(self.sessions)
        for s in list(targets):
            await s.send(msg)

    async def broadcast_buttons(self, pairs: list[tuple[int, int]]) -> None:
        if not self.sessions:
            return
        results = await asyncio.gather(
            *(s.send_buttons(pairs) for s in list(self.sessions)),
            return_exceptions=True,
        )
        for res in results:
            if isinstance(res, Exception):
                log.debug("broadcast error: %r", res)
