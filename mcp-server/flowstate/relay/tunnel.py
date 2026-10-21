"""The tunnel: outbound connections from this computer to the relay.

Control connection: TLS to tunnel.flowstatemcp.com:443 (the relay's own certificate,
verified normally), one JSON object per line: hello -> welcome, ping/pong heartbeats, and
`open` when a client connects to <handle>.relay.flowstatemcp.com. Reconnects with
exponential backoff and jitter.

Data connection (one per client connection): a new TLS connection to the same tunnel
endpoint, one `attach` line naming the stream, then raw bytes. The relay splices the
client's TCP bytes into it unchanged (starting with the client's TLS ClientHello). This
agent then runs the TLS handshake as the SERVER, with the user's own certificate and key, in
memory (ssl.MemoryBIO), and passes the decrypted HTTP to FlowState's normal public-mode
HTTP listener on 127.0.0.1. The relay only ever carries ciphertext.

Exact wire format: docs/relay-protocol.md.
"""

import asyncio
import contextlib
import ipaddress
import json
import logging
import random
import re
import ssl
import time
from typing import Callable, Optional

from . import PROTOCOL, RelayError
from .api import AGENT

log = logging.getLogger("flowstate.relay.tunnel")

MAX_LINE = 64 * 1024
STREAM_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
CONNECT_TIMEOUT = 15.0
HANDSHAKE_TIMEOUT = 20.0
MAX_STREAMS = 64
CHUNK = 64 * 1024
DEFAULT_HEARTBEAT = 25.0
UNAUTHORIZED = {"unauthorized", "revoked", "unknown_device"}


class ProtocolError(RelayError):
    pass


class AuthRejected(RelayError):
    pass


def encode(msg: dict) -> bytes:
    return json.dumps(msg, separators=(",", ":")).encode() + b"\n"


async def read_msg(reader: asyncio.StreamReader, timeout: Optional[float]) -> dict:
    try:
        line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout)
    except asyncio.LimitOverrunError:
        raise ProtocolError("control line longer than 64 KiB") from None
    except asyncio.IncompleteReadError:
        raise ConnectionError("relay closed the connection") from None
    except asyncio.TimeoutError:
        raise ConnectionError(f"nothing from the relay for {timeout:.0f}s") from None
    try:
        msg = json.loads(line)
    except ValueError:
        raise ProtocolError("control line is not JSON") from None
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ProtocolError("control message without a type")
    return msg


class Backoff:
    """Exponential backoff with equal jitter: attempt n waits between d/2 and d, where
    d = min(cap, base * 2**n)."""

    def __init__(self, base: float = 1.0, cap: float = 60.0, rand: Callable[[], float] = random.random):
        self.base, self.cap, self.rand, self.attempt = base, cap, rand, 0

    def next(self) -> float:
        d = min(self.cap, self.base * (2 ** self.attempt))
        self.attempt = min(self.attempt + 1, 32)
        return d / 2 + self.rand() * d / 2

    def reset(self) -> None:
        self.attempt = 0


def server_context(cert_file, key_file) -> ssl.SSLContext:
    """TLS server context with the user's own certificate and key (HTTP/1.1 only)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert_file), str(key_file))
    ctx.set_alpn_protocols(["http/1.1"])
    ctx.options |= getattr(ssl, "OP_NO_RENEGOTIATION", 0)
    return ctx


def client_ip(addr: str) -> str:
    """'203.0.113.5:4431' / '[2001:db8::1]:4431' -> the IP; anything else -> 'relay-unknown'."""
    host = addr.rsplit(":", 1)[0] if addr.count(":") == 1 or addr.startswith("[") else addr
    host = host.strip("[]")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return "relay-unknown"


class _ServerTLS:
    """Server-side TLS over a byte pipe, entirely in memory."""

    def __init__(self, ctx: ssl.SSLContext):
        self.inc, self.out = ssl.MemoryBIO(), ssl.MemoryBIO()
        self.obj = ctx.wrap_bio(self.inc, self.out, server_side=True)
        self.closed = False

    def flush(self, w: asyncio.StreamWriter) -> None:
        data = self.out.read()
        if data:
            w.write(data)

    async def handshake(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        while True:
            try:
                self.obj.do_handshake()
                break
            except ssl.SSLWantReadError:
                self.flush(w)
                await w.drain()
                data = await r.read(CHUNK)
                if not data:
                    raise EOFError("client went away during the TLS handshake")
                self.inc.write(data)
        self.flush(w)
        await w.drain()

    def feed(self, data: bytes) -> bytes:
        self.inc.write(data)
        parts = []
        while True:
            try:
                chunk = self.obj.read(CHUNK)
            except ssl.SSLWantReadError:
                break
            except ssl.SSLZeroReturnError:
                self.closed = True
                break
            if not chunk:
                self.closed = True
                break
            parts.append(chunk)
        return b"".join(parts)


class TunnelAgent:
    def __init__(self, *, host: str, port: int, sni: str, token: str, handle: str,
                 client_ctx: ssl.SSLContext,
                 server_ctx: Callable[[], Optional[ssl.SSLContext]],
                 local_port: Callable[[], Optional[int]], peer_map: dict,
                 on_state: Optional[Callable[[str, str], None]] = None,
                 heartbeat: float = DEFAULT_HEARTBEAT, min_heartbeat: float = 5.0,
                 backoff: Optional[Backoff] = None, stable_after: float = 30.0):
        self.host, self.port, self.sni = host, port, sni
        self.token, self.handle = token, handle
        self.client_ctx, self.server_ctx, self.local_port = client_ctx, server_ctx, local_port
        self.peer_map, self.on_state = peer_map, on_state
        self.heartbeat, self.min_heartbeat = heartbeat, min_heartbeat
        self.backoff = backoff or Backoff()
        self.stable_after = stable_after
        self.state, self.detail, self.connects = "stopped", "", 0
        self.delays: list = []  # backoff delays used (for status and tests)
        self._welcomed_at: Optional[float] = None
        self._streams: set = set()

    # ------------------------------------------------------------ state
    def _set(self, state: str, detail: str = "") -> None:
        self.state, self.detail = state, detail
        if self.on_state:
            try:
                self.on_state(state, detail)
            except Exception as e:  # noqa: BLE001 - status writing must not kill the tunnel
                log.warning("could not record tunnel state: %s", e)

    async def _open(self):
        return await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, ssl=self.client_ctx,
                                    server_hostname=self.sni, limit=MAX_LINE,
                                    ssl_handshake_timeout=CONNECT_TIMEOUT),
            CONNECT_TIMEOUT + 5)

    # ------------------------------------------------------------ control connection
    async def run(self, stop: asyncio.Event) -> None:
        """Keep a control connection up until `stop` is set or the relay rejects the token."""
        try:
            while not stop.is_set():
                self._set("connecting", f"{self.sni}:{self.port}")
                err = None
                try:
                    await self._control(stop)
                except AuthRejected as e:
                    log.error("relay rejected this device (%s); run `flowstate relay forget` and "
                              "enroll again", e)
                    self._set("unauthorized", str(e))
                    return
                except (OSError, ssl.SSLError, RelayError, EOFError) as e:
                    err = f"{type(e).__name__}: {e}"[:200]
                if stop.is_set():
                    break
                if self._welcomed_at and time.monotonic() - self._welcomed_at >= self.stable_after:
                    self.backoff.reset()
                delay = self.backoff.next()
                self.delays.append(delay)
                log.info("tunnel down (%s); reconnecting in %.1fs", err or "closed by relay", delay)
                self._set("reconnecting", f"in {delay:.1f}s" + (f" after {err}" if err else ""))
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), delay)
            self._set("stopped")
        finally:
            await self.close_streams()

    @staticmethod
    def _check_error(msg: dict) -> None:
        if msg.get("type") == "error":
            code = str(msg.get("code", "error"))[:40]
            if code in UNAUTHORIZED:
                raise AuthRejected(code)
            raise ProtocolError(f"relay error: {code}")

    async def _control(self, stop: asyncio.Event) -> None:
        self._welcomed_at = None
        reader, writer = await self._open()
        self.connects += 1
        stop_wait = asyncio.ensure_future(stop.wait())
        pinger = None
        try:
            writer.write(encode({"type": "hello", "v": 1, "role": "control", "proto": PROTOCOL,
                                 "handle": self.handle, "token": self.token, "agent": AGENT}))
            await writer.drain()
            msg = await read_msg(reader, CONNECT_TIMEOUT)
            self._check_error(msg)
            if msg["type"] != "welcome":
                raise ProtocolError(f"expected welcome, got {msg['type'][:20]}")
            try:
                hb = float(msg.get("heartbeat", self.heartbeat))
            except (TypeError, ValueError):
                hb = self.heartbeat
            hb = min(max(hb, self.min_heartbeat), 120.0)
            self._welcomed_at = time.monotonic()
            log.info("tunnel connected as %s (heartbeat %.0fs)", self.handle, hb)
            self._set("connected", f"heartbeat {hb:.0f}s")
            pinger = asyncio.create_task(self._pinger(writer, hb))
            while True:
                read = asyncio.ensure_future(read_msg(reader, hb * 3))
                done, _ = await asyncio.wait({read, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
                if read not in done:
                    read.cancel()
                    with contextlib.suppress(Exception):
                        writer.write(encode({"type": "bye"}))
                        await asyncio.wait_for(writer.drain(), 2)
                    return
                msg = read.result()
                t = msg["type"]
                if t == "ping":
                    writer.write(encode({"type": "pong", "seq": msg.get("seq")}))
                elif t == "open":
                    self._on_open(msg, writer)
                elif t in ("goaway", "bye"):
                    return
                elif t == "error":
                    self._check_error(msg)
                # "pong" and unknown types: nothing to do (forward compatible)
        finally:
            stop_wait.cancel()
            if pinger:
                pinger.cancel()
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 2)

    async def _pinger(self, writer: asyncio.StreamWriter, hb: float) -> None:
        seq = 0
        with contextlib.suppress(OSError, ConnectionError):
            while True:
                await asyncio.sleep(hb)
                seq += 1
                writer.write(encode({"type": "ping", "seq": seq}))
                await writer.drain()

    # ------------------------------------------------------------ data connections
    def _on_open(self, msg: dict, ctrl: asyncio.StreamWriter) -> None:
        sid = msg.get("stream")
        if not isinstance(sid, str) or not STREAM_RE.match(sid):
            log.warning("relay sent an open with an invalid stream id; ignored")
            return
        reason = None
        if len(self._streams) >= MAX_STREAMS:
            reason = "busy"
        elif self.server_ctx() is None:
            reason = "no_certificate"
        elif not self.local_port():
            reason = "not_ready"
        if reason:
            ctrl.write(encode({"type": "reject", "stream": sid, "reason": reason}))
            return
        t = asyncio.create_task(self._stream(sid, str(msg.get("client") or "")))
        self._streams.add(t)
        t.add_done_callback(self._streams.discard)

    async def close_streams(self) -> None:
        for t in list(self._streams):
            t.cancel()
        if self._streams:
            await asyncio.gather(*self._streams, return_exceptions=True)

    async def _stream(self, sid: str, client: str) -> None:
        ctx, port = self.server_ctx(), self.local_port()
        w = lw = None
        key = None
        try:
            r, w = await self._open()
            w.write(encode({"type": "attach", "v": 1, "stream": sid, "token": self.token}))
            await w.drain()
            # From here on the pipe carries the client's own TLS bytes.
            tls = _ServerTLS(ctx)
            await asyncio.wait_for(tls.handshake(r, w), HANDSHAKE_TIMEOUT)
            lr, lw = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), CONNECT_TIMEOUT)
            key = lw.get_extra_info("sockname")[1]
            self.peer_map[key] = client_ip(client)
            # the client's first request often arrives with its handshake Finished message
            early = tls.feed(b"")
            tls.flush(w)
            if early:
                lw.write(early)
            up = asyncio.create_task(self._up(tls, r, w, lw))
            down = asyncio.create_task(self._down(tls, lr, w))
            done, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            for p in pending:
                p.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for d in done:
                if d.exception():
                    log.debug("stream %s: %s", sid[:8], d.exception())
        except (OSError, ssl.SSLError, RelayError, EOFError, asyncio.TimeoutError) as e:
            log.info("stream %s ended: %s", sid[:8], type(e).__name__)
        finally:
            if key is not None:
                self.peer_map.pop(key, None)
            for x in (lw, w):
                if x is not None:
                    x.close()

    @staticmethod
    async def _up(tls: _ServerTLS, r, w, lw) -> None:
        """client (via relay) -> decrypt -> local FlowState listener"""
        while True:
            data = await r.read(CHUNK)
            if not data:
                return
            plain = tls.feed(data)
            tls.flush(w)
            if plain:
                lw.write(plain)
                await lw.drain()
            if tls.closed:
                return

    @staticmethod
    async def _down(tls: _ServerTLS, lr, w) -> None:
        """local FlowState listener -> encrypt -> client (via relay)"""
        while True:
            data = await lr.read(CHUNK)
            if not data:
                with contextlib.suppress(ssl.SSLError):
                    tls.obj.unwrap()  # queue close_notify
                tls.flush(w)
                await w.drain()
                return
            tls.obj.write(data)
            tls.flush(w)
            await w.drain()
