"""The streamable HTTP transport's app (moved out of server.py, which keeps a wrapper):
bearer / per-app key / OAuth checks (remote_auth.py) in front of the MCP session manager."""

import asyncio
from pathlib import Path
from typing import Optional


def create_http_app(token: Optional[str], path: str = "/mcp", db_path: Optional[Path] = None,
                    public_url: Optional[str] = None, allow_master_token: bool = False,
                    auth_db: Optional[Path] = None, trust_proxy: Optional[bool] = None,
                    peer_resolver=None, relay: bool = False):
    """Starlette app: bearer-protected streamable HTTP MCP at `path`, plus GET /health.

    LAN mode (no public_url): the owner token, per-app keys from `flowstate keys add`, plus
    per-client tokens from `flowstate remote add-client`. Public mode (public_url = the tunnel's https URL; bind
    127.0.0.1): per-client tokens and OAuth 2.1 (consent page + owner passphrase), Host
    allow-list, CF-Connecting-IP for rate limits; the owner token only with
    allow_master_token. See remote_auth.py and docs/REMOTE-ACCESS.md. The relay agent
    (relay/runner.py) uses public mode with trust_proxy=False, its own peer_resolver and
    relay=True. Every request is marked with its listener (lan / public / relay; Gate), so
    apps reaching FlowState from outside this computer never get Approve / Reject boxes."""
    import contextlib
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from . import remote_auth as RA
    from . import server as S

    path = "/" + path.strip("/")
    store = RA.AuthStore(auth_db)
    if public_url:
        public_url = public_url.rstrip("/")
        master = token if allow_master_token else None
    else:
        if len(token or "") < 16:
            raise ValueError("HTTP transport requires a bearer token of at least 16 characters")
        master = token
    manager = StreamableHTTPSessionManager(app=S.server, json_response=False, stateless=False)

    async def health(request):
        # Deliberately says nothing about the data; safe to expose through a tunnel. Not
        # through the relay: every relay address is public (certificate logs), so answering
        # there would tell anyone whether that user's computer is on (audit D15).
        if relay:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"ok": True, "service": "flowstate"})

    from .approve_page import routes as approve_routes
    from .review_phone import routes as review_routes
    # the phone approval page and review list (approve_page.py, review_phone.py); they check
    # their own signed grants
    routes = [Route("/health", health, methods=["GET"])] + approve_routes(lambda: S.backend.db_path) \
        + review_routes(lambda: S.backend.db_path)
    provider = rmd = None
    allowed_hosts = None
    if public_url:
        from pydantic import AnyHttpUrl
        from mcp.server.auth.routes import (build_resource_metadata_url, create_auth_routes,
                                            create_protected_resource_routes)
        from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
        resource = public_url + path
        # one exact issuer string, never with a trailing slash (task 78): the SDK's URL type
        # renders a bare host as "https://host/", which strict issuer comparison (RFC 8414
        # section 3.3) treats as another issuer than the address; the consent link is built
        # from the same string (task 79)
        issuer = RA.oauth_issuer(public_url)
        provider = RA.FlowStateOAuthProvider(store, issuer, resource)
        routes += _exact_issuer(create_auth_routes(
            provider, AnyHttpUrl(issuer),
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=RA.SCOPES, default_scopes=RA.SCOPES),
            revocation_options=RevocationOptions(enabled=True)), issuer)
        prm = _exact_issuer(create_protected_resource_routes(
            AnyHttpUrl(resource), [AnyHttpUrl(issuer)], scopes_supported=RA.SCOPES,
            resource_name="FlowState"), issuer)
        # also at the bare well-known path, for clients that don't add the resource path
        routes += prm + [Route("/.well-known/oauth-protected-resource", prm[0].endpoint,
                               methods=["GET", "OPTIONS"])]
        routes += RA.consent_routes(provider)
        if not relay:   # the owner's review page behind the passphrase (review_tunnel.py);
            # relay users have the signed-in phone list, LAN users `flowstate review-page`
            from .review_tunnel import routes as tunnel_routes
            routes += tunnel_routes(lambda: S.backend.db_path, store, public_url)
        rmd = str(build_resource_metadata_url(AnyHttpUrl(resource)))
        allowed_hosts = {urlparse_host(public_url), "127.0.0.1", "localhost"}
    # per-app LAN keys (app_keys.py) on the LAN listener only, never on the public one
    checker = RA.TokenChecker(store, master, provider, app_keys=not public_url)

    async def mcp_asgi(scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").rstrip("/") != path:
            await JSONResponse({"error": "not found"}, status_code=404)(scope, receive, send)
            return
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        S.init_backend(db_path)
        reaper = asyncio.create_task(S._reaper())
        try:
            async with manager.run():
                yield
        finally:
            reaper.cancel()
            S._close_own_sessions("shutdown")

    app = Starlette(routes=routes + [Mount("/", app=mcp_asgi)], lifespan=lifespan)
    return RA.Gate(app, checker, path, allowed_hosts=allowed_hosts,
                   trust_proxy=bool(public_url) if trust_proxy is None else trust_proxy,
                   resource_metadata_url=rmd, peer_resolver=peer_resolver,
                   listener="relay" if relay else ("public" if public_url else "lan"))


def urlparse_host(url: str) -> str:
    from urllib.parse import urlparse
    return (urlparse(url).hostname or "").lower()


def _exact_issuer(routes: list, issuer: str) -> list:
    """The SDK's routes, with its metadata documents answering `issuer` (authorization server
    metadata) and `authorization_servers` (protected resource metadata) as the exact string.
    The SDK keeps both as Pydantic URLs, which add '/' to a bare host when serialized (task
    78); everything else in the documents, and the endpoints built from the issuer, stay the
    SDK's."""
    from starlette.routing import Route
    return [Route(r.path, _ExactIssuer(r.app, issuer), methods=sorted(r.methods or ()))
            if r.path.startswith("/.well-known/") else r for r in routes]


class _ExactIssuer:
    """ASGI wrapper (a class, so Starlette's Route passes it through as an app) that rewrites
    the issuer fields of a 200 JSON answer; any other answer (the CORS preflight) is as is."""

    def __init__(self, app, issuer: str):
        self.app, self.fields = app, {"issuer": issuer, "authorization_servers": [issuer]}

    async def __call__(self, scope, receive, send):
        import json
        start, chunks = None, []

        async def capture(message):
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
                return
            if message["type"] != "http.response.body":
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            body = b"".join(chunks)
            try:
                data = json.loads(body) if start["status"] == 200 else None
            except ValueError:
                data = None
            if isinstance(data, dict):
                data.update({k: v for k, v in self.fields.items() if k in data})
                body = json.dumps(data, separators=(",", ":")).encode("utf-8")
            headers = [(k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"]
            await send({**start, "headers": headers + [(b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, capture)
