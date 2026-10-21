"""`flowstate relay run`: everything the relay needs, in one process.

  - FlowState's normal public-mode HTTP app (OAuth 2.1 + PKCE, passphrase consent, per-app
    revoke, read-only, Host allow-list, rate limits) on 127.0.0.1:<port>, with public URL
    https://<handle>.relay.flowstatemcp.com. Nothing about sign-in changes.
  - the tunnel agent (tunnel.py), which terminates TLS with the local key and pipes the
    decrypted HTTP to that listener;
  - certificate upkeep: obtain at start if missing, check twice a day, renew 30 days before
    expiry, reload the TLS context after a renewal;
  - the daily Certificate Transparency check;
  - a watcher that stops everything when `flowstate relay disable` / `forget` runs.
"""

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Callable, Optional

from . import RelayError
from . import acme, ct
from . import crypto as C
from .api import client_context
from .store import CERT, TLS_KEY, TOKEN, RelayStore
from .tunnel import Backoff, TunnelAgent, server_context

log = logging.getLogger("flowstate.relay")

DEFAULT_PORT = 8768
CERT_CHECK_EVERY = 12 * 3600
WATCH_EVERY = 5.0


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


class _Certs:
    """The current TLS server context, rebuilt whenever the certificate changes."""

    def __init__(self, store: RelayStore):
        self.store, self.ctx, self.info = store, None, None

    def reload(self) -> None:
        why = acme.needs_cert(self.store)
        if why and not why.startswith("certificate expires"):
            self.ctx, self.info = None, None
            return
        self.ctx = server_context(self.store.path(CERT), self.store.path(TLS_KEY))
        self.info = C.cert_info(self.store.read_bytes(CERT))

    def get(self):
        return self.ctx


async def run(store: Optional[RelayStore] = None, *, db_path: Optional[Path] = None,
              port: int = DEFAULT_PORT, stop: Optional[asyncio.Event] = None,
              ct_fetch=None, acme_sleep: Callable[[float], None] = time.sleep,
              on_ready: Optional[Callable[[int], None]] = None,
              heartbeat_min: float = 5.0, backoff: Optional[Backoff] = None) -> None:
    store = store or RelayStore()
    cfg = store.config()
    if not cfg.get("handle"):
        raise RelayError("the relay is not set up; run: flowstate relay enroll --code <code>")
    if not cfg.get("enabled"):
        raise RelayError("the relay is turned off; turn it on with: flowstate relay enable")
    C._lib()  # fail early, plainly, if `cryptography` is missing
    token = store.token()
    host = cfg["hostname"]
    stop = stop or asyncio.Event()

    import uvicorn
    from .. import remote_auth as RA
    from ..server import create_http_app
    if not RA.AuthStore().has_passphrase():
        log.warning("no owner passphrase set: apps can't be approved until you run "
                    "`flowstate remote passphrase`")

    peer_map: dict = {}
    app = create_http_app(None, db_path=db_path, public_url=f"https://{host}", trust_proxy=False,
                          peer_resolver=lambda peer: peer_map.get(peer[1]))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info",
                                           access_log=False, proxy_headers=False,
                                           server_header=False))
    status = {"tunnel": "starting", "detail": "", "url": f"https://{host}/mcp"}

    def save(**kw):
        status.update(kw)
        if store.path(TOKEN).exists():  # after `forget`, leave nothing behind
            store.save_status(status)

    certs = _Certs(store)
    certs.reload()
    serve = asyncio.create_task(server.serve())
    while not server.started:
        if serve.done():
            serve.result()
            raise RelayError(f"the local listener on 127.0.0.1:{port} did not start")
        await asyncio.sleep(0.05)
    local_port = server.servers[0].sockets[0].getsockname()[1]
    save(port=local_port)
    log.info("relay: FlowState public listener on 127.0.0.1:%s for https://%s", local_port, host)

    agent = TunnelAgent(
        host=cfg.get("tunnel_host"), port=int(cfg.get("tunnel_port", 443)),
        sni=cfg.get("tunnel_sni") or cfg.get("tunnel_host"), token=token, handle=cfg["handle"],
        client_ctx=client_context(store.ca_file()), server_ctx=certs.get,
        local_port=lambda: local_port, peer_map=peer_map,
        on_state=lambda s, d: save(tunnel=s, detail=d, tunnel_since=int(time.time())),
        min_heartbeat=heartbeat_min, backoff=backoff)

    async def cert_loop():
        fail = Backoff(base=300, cap=6 * 3600)
        while not stop.is_set():
            try:
                r = await asyncio.to_thread(acme.ensure_cert, store, sleep=acme_sleep)
                if r["renewed"] or certs.ctx is None:
                    certs.reload()
                info = certs.info or r["info"]
                save(cert_not_after=info["not_after"].isoformat(), cert_error=None,
                     cert_checked=int(time.time()))
                fail.reset()
                wait = CERT_CHECK_EVERY
            except Exception as e:  # noqa: BLE001 - keep serving with the old cert, retry later
                wait = fail.next()
                log.error("certificate: %s (retry in %.0f min)", e, wait / 60)
                save(cert_error=str(e)[:200], cert_checked=int(time.time()))
            await _sleep_or_stop(stop, wait)

    async def ct_loop():
        while not stop.is_set():
            if ct.due(store):
                try:
                    r = await asyncio.to_thread(ct.run_check, store, ct_fetch or ct._fetch)
                    save(ct_ok=r["ok"], ct_last_check=r.get("last_check"))
                except Exception as e:  # noqa: BLE001
                    log.warning("CT check failed: %s", e)
            await _sleep_or_stop(stop, 3600)

    async def watch():
        last = 0.0
        while not stop.is_set():
            c = store.config()
            if not c.get("enabled") or store.read_bytes(TOKEN) is None:
                log.info("relay turned off (flowstate relay disable/forget); stopping")
                stop.set()
                break
            if serve.done():
                stop.set()
                break
            if time.monotonic() - last > 30:
                save()  # heartbeat for `flowstate relay status`
                last = time.monotonic()
            await _sleep_or_stop(stop, WATCH_EVERY)

    tasks = [asyncio.create_task(agent.run(stop)), asyncio.create_task(cert_loop()),
             asyncio.create_task(watch())]
    if ct_fetch is not False:
        tasks.append(asyncio.create_task(ct_loop()))
    if on_ready:
        on_ready(local_port)
    try:
        await asyncio.wait({serve, asyncio.ensure_future(stop.wait())},
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.set()
        server.should_exit = True
        for t in tasks[1:]:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(serve, 10)
        with contextlib.suppress(Exception):
            save(tunnel="stopped", detail="")
        log.info("relay stopped")
