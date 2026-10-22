"""The streamable HTTP transport's app (moved out of server.py, which keeps a wrapper):
bearer / per-app key / OAuth checks (remote_auth.py) in front of the MCP session manager."""

import asyncio
from pathlib import Path
from typing import Optional


def create_http_app(token: Optional[str], path: str = "/mcp", db_path: Optional[Path] = None,
                    public_url: Optional[str] = None, allow_master_token: bool = False,
                    auth_db: Optional[Path] = None, trust_proxy: Optional[bool] = None,
                    peer_resolver=None):
    """Starlette app: bearer-protected streamable HTTP MCP at `path`, plus GET /health.

    LAN mode (no public_url): the owner token, per-app keys from `flowstate keys add`, plus
    per-client tokens from `flowstate remote add-client`. Public mode (public_url = the tunnel's https URL; bind
    127.0.0.1): per-client tokens and OAuth 2.1 (consent page + owner passphrase), Host
    allow-list, CF-Connecting-IP for rate limits; the owner token only with
    allow_master_token. See remote_auth.py and docs/REMOTE-ACCESS.md. The relay agent
    (relay/runner.py) uses public mode with trust_proxy=False and its own peer_resolver."""
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
        # Deliberately says nothing about the data; safe to expose through a tunnel.
        return JSONResponse({"ok": True, "service": "flowstate"})

    from .approve_page import routes as approve_routes
    # the phone approval page (approve_page.py); it checks its own signed grants
    routes = [Route("/health", health, methods=["GET"])] + approve_routes(lambda: S.backend.db_path)
    provider = rmd = None
    allowed_hosts = None
    if public_url:
        from pydantic import AnyHttpUrl
        from mcp.server.auth.routes import (build_resource_metadata_url, create_auth_routes,
                                            create_protected_resource_routes)
        from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
        resource = public_url + path
        provider = RA.FlowStateOAuthProvider(store, public_url, resource)
        routes += create_auth_routes(
            provider, AnyHttpUrl(public_url),
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=RA.SCOPES, default_scopes=RA.SCOPES),
            revocation_options=RevocationOptions(enabled=True))
        prm = create_protected_resource_routes(AnyHttpUrl(resource), [AnyHttpUrl(public_url)],
                                               scopes_supported=RA.SCOPES, resource_name="FlowState")
        # also at the bare well-known path, for clients that don't add the resource path
        routes += prm + [Route("/.well-known/oauth-protected-resource", prm[0].endpoint,
                               methods=["GET", "OPTIONS"])]
        routes += RA.consent_routes(provider)
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
                   resource_metadata_url=rmd, peer_resolver=peer_resolver)


def urlparse_host(url: str) -> str:
    from urllib.parse import urlparse
    return (urlparse(url).hostname or "").lower()
