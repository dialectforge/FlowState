# FlowState remote access: cloud AIs over HTTPS

This is optional. Most people only need FlowState on their own computer (stdio), or on their
home network (the HTTP server in [AGENTS.md](../AGENTS.md#http-server)). Read on if you want
cloud AIs such as claude.ai (web and phone), ChatGPT or the Grok app to reach your FlowState
over HTTPS, without opening a port on your router.

No domain? The optional relay agent (`flowstate relay enroll`, see the README section
"Optional: reach FlowState from anywhere") gives you `https://<handle>.fsmcp.com` with end-to-end TLS and
the same sign-in. This page covers running it on your own domain instead.

What you need: a domain on Cloudflare (free plan is fine), `cloudflared` on the computer that
runs FlowState, and a few minutes. Placeholders below: `flowstate.example.com` (your hostname),
`<TUNNEL-UUID>`, `<path-to-repo>` (this repository), `<path-to-db>` (your database file),
`<venv python>` (`<path-to-repo>/.venv/Scripts/python.exe` on Windows,
`<path-to-repo>/.venv/bin/python` on macOS/Linux).

## 1. What each app supports

From each vendor's official docs at the time of writing (most pages are undated; check them
again before you rely on a detail).

| App | How it signs in to a remote MCP server | What FlowState gives it |
|---|---|---|
| **claude.ai** custom connectors (web, desktop, phone) | OAuth with dynamic client registration (DCR) or client-ID metadata documents (CIMD), or no auth. Fixed bearer headers are a closed beta. Needs a `401` + `WWW-Authenticate`, PKCE S256, RFC 8707 `resource`. Callback `https://claude.ai/api/mcp/auth_callback`. Connects over IPv4 from public addresses only. | OAuth (DCR) + consent page |
| **ChatGPT** connectors / developer mode | OAuth (CIMD preferred, DCR fallback, or a pre-set client) or no auth. **No API keys / fixed bearer.** PKCE S256 + `resource` required. Callbacks on `chatgpt.com`. | OAuth (DCR) + consent page |
| **Grok app** connectors | Docs say "OAuth or API keys" without detail. **Not confirmed** which. | Per-app token, or OAuth once its callback host is allowed |
| **xAI API** remote MCP tool (bots you build) | `authorization` field (sent as a bearer header) or extra `headers`. No OAuth. | Per-app token |
| **Cursor** | `url` + `headers` (e.g. `Authorization: Bearer ${env:TOKEN}`), or OAuth with DCR. | Per-app token (simplest) or OAuth |
| **Claude Code** | Header (`--header "Authorization: Bearer ..."`) or OAuth with a loopback callback. | Per-app token |
| **Cloudflare Access** | "Managed OAuth" can act as the OAuth server (DCR, no CIMD, short-lived tokens); service tokens need custom `CF-Access-Client-*` headers that ChatGPT and claude.ai can't send. | Optional extra lock on the approval page only |

Sources:
- Claude: https://claude.com/docs/connectors/building/authentication ,
  https://claude.com/docs/connectors/custom/add-unlisted ,
  https://claude.com/docs/connectors/building/troubleshooting ,
  https://platform.claude.com/docs/en/api/ip-addresses
- ChatGPT / OpenAI: https://developers.openai.com/apps-sdk/build/auth ,
  https://developers.openai.com/api/docs/guides/tools-connectors-mcp ,
  https://developers.openai.com/api/docs/guides/developer-mode
- xAI / Grok: https://docs.x.ai/docs/guides/tools/remote-mcp-tools , https://docs.x.ai/grok/connectors ,
  https://docs.x.ai/grok/connectors/custom-mcp-tunneling
- Cursor: https://cursor.com/docs/context/mcp
- Cloudflare: https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/managed-oauth/
- MCP spec: https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization

Not confirmed from official sources: the Grok app's sign-in method and callback host; whether
claude.ai or ChatGPT work through Cloudflare Access Managed OAuth; whether the phone apps can
*add* a connector (they can use one added on the web).

## 2. The design, in plain words

```
 cloud AI ──HTTPS──> Cloudflare ──(tunnel, outbound from your computer)──> cloudflared
                                                                             │
                                       FlowState public listener 127.0.0.1:8767
 other computers on your network ─────> FlowState LAN listener <your-LAN-IP>:8766 (optional)
```

1. **Cloudflare Tunnel.** A small program (`cloudflared`) on your computer keeps an outbound
   connection to Cloudflare. `https://flowstate.example.com` reaches FlowState through it.
   No router port, your home IP stays hidden, Cloudflare handles HTTPS.
2. **A separate public listener** on `127.0.0.1:8767` (only the tunnel can reach it), started
   with `--public-url https://flowstate.example.com`. A LAN listener, if you run one, doesn't
   change. Your LAN token is **not** accepted on the public URL (unless you start it with
   `--allow-master-token`, which isn't recommended).
3. **FlowState does the sign-in itself (OAuth 2.1, the MCP standard).** That's the only
   method claude.ai and ChatGPT both take. When you add the connector, a FlowState page
   opens: "<app> wants to use your FlowState → Read only / Read and write → owner
   passphrase → Approve". Only you know the passphrase. 5 wrong tries within 15 minutes lock
   approvals for up to 15 minutes.
4. **Per-app tokens** for apps that take a fixed header (Cursor, the xAI API, Claude Code):
   `flowstate remote add-client --name "Cursor" [--read-only] [--days N]`.
5. **Every app can be cut off on its own:** `flowstate remote clients` lists them with
   "last used"; `flowstate remote revoke --client <id>` stops it at once.

   **LAN keys vs remote tokens.** `flowstate keys add --name "<app>" [--read-only]` makes a
   per-app key for the **local/LAN** listener only (prefix `fs_lk_`); it is refused on the
   public URL, just like the LAN token. `flowstate remote add-client` makes a per-app
   **remote** token (prefix `fs_ct_`) for the public listener (it also works on the LAN
   listener, as before). Both record the app's name from the key or token, not from what the
   app says; for remote apps the machine recorded is the client's name. Changes made with the
   shared LAN token are marked "shared token (legacy)". Every change keeps the app, the model
   (only when the app supplies it, for example with an `X-FlowState-Model` header) and how the
   app was identified; see "Who did what" on the review page.
6. **Guard rails:**
   - read-only option per app;
   - `git` and file attach (which touch files on your computer) are refused for every remote app;
   - rate limits: 300 requests a minute per app, 60 a minute per address before sign-in
     (`FLOWSTATE_RATE_LIMIT`, `FLOWSTATE_RATE_LIMIT_ANON`);
   - only your hostname (and 127.0.0.1 / localhost) is answered;
   - OAuth apps may only send the browser back to known hosts (claude.ai, claude.com,
     chatgpt.com, cursor.com, www.cursor.com, localhost, 127.0.0.1); add one with
     `flowstate remote allow-redirect --name <host>`;
   - tokens and codes are stored only as SHA-256 hashes and the passphrase as an scrypt hash,
     in `remote-auth.db` in FlowState's settings folder (Windows `%APPDATA%\flowstate`, macOS
     `~/Library/Application Support/flowstate`, Linux `~/.config/flowstate`; or
     `FLOWSTATE_AUTH_DB`), not in your data folder;
   - access tokens last 1 hour; refresh tokens last 30 days and change on every use;
   - the log has one line per request with no bodies, no query strings and no tokens;
   - `GET /health` answers `{"ok": true, ...}` without a token and says nothing about your data.
7. **Optional second lock (recommended):** a Cloudflare Access rule on the path `/consent`
   only, allowing just your own login (for example a one-time code sent to you). Then even
   someone who learns the passphrase can't approve an app. Don't put Access on `/mcp`,
   `/token`, `/register` or `/.well-known/*`: the AI apps call those directly and a login
   page there breaks them.

**Why not only Cloudflare Access (its "Managed OAuth")?** Fewer moving parts in FlowState,
but: not confirmed to work with claude.ai or ChatGPT, FlowState would still have to check
Cloudflare's signed header, per-app read-only and revocation would live in the Cloudflare
dashboard, and fixed-header apps would need Cloudflare's custom headers that ChatGPT can't
send. FlowState's own OAuth covers all of these.
**Why not "no auth" behind a secret URL?** Anyone who sees the URL gets your memory.

Tradeoffs: the passphrase page is on the internet (mitigated by the lockout, rate limits and
the optional Access lock); FlowState doesn't support CIMD yet (claude.ai and ChatGPT fall back
to DCR, which it supports); the Grok app is unverified until you try it; rate limits are kept
in memory per process; `remote-auth.db` is not part of the nightly backup (losing it only means
approving your apps again).

## 3. Setting it up, step by step

Below, `<venv python> -m flowstate ...` can also be written `flowstate ...` with the venv active.

1. **Owner passphrase** (12+ characters, not used anywhere else):
   ```
   <venv python> -m flowstate remote passphrase
   ```
2. **Install cloudflared** from Cloudflare's official download page
   (https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/).
   Check the published checksum before you install it.
3. **Create the tunnel** (opens a browser to log in to Cloudflare; pick your domain):
   ```
   cloudflared tunnel login
   cloudflared tunnel create flowstate
   cloudflared tunnel route dns flowstate flowstate.example.com
   ```
   `tunnel create` prints the tunnel's UUID and writes a credentials file. Copy
   `scripts/cloudflared/config.example.yml` to `~/.cloudflared/config.yml`
   (Windows `%USERPROFILE%\.cloudflared\config.yml`), replace `<TUNNEL-UUID>`, the credentials
   path and `flowstate.example.com`. The credentials file is a secret: keep it in
   `~/.cloudflared`, never in a synced or git folder.
4. **Start the public listener** (by hand first, to test):
   ```
   <venv python> -m flowstate --transport http --host 127.0.0.1 --port 8767 --db <path-to-db> --public-url https://flowstate.example.com
   ```
   On Windows you can instead run
   `<path-to-repo>\scripts\windows\start-flowstate-public.cmd --port 8767 --db <path-to-db> --public-url https://flowstate.example.com`
   (it adds `--transport http --host 127.0.0.1` itself and logs to
   `%LOCALAPPDATA%\flowstate\logs\public-server.log`).
   Then, in another window: `cloudflared tunnel run flowstate`. Check that
   `https://flowstate.example.com/health` answers `{"ok": true, ...}` and that
   `https://flowstate.example.com/mcp` without a token answers 401.
5. **Optional Access lock** (Cloudflare Zero Trust → Access → Applications → Self-hosted):
   domain `flowstate.example.com`, path `consent`, a policy that allows only you.
6. **Connect the apps:**
   - claude.ai: Settings → Connectors → Add custom connector → URL
     `https://flowstate.example.com/mcp` → Connect → FlowState page → approve.
   - ChatGPT: Settings → Connectors (developer mode) → add `https://flowstate.example.com/mcp`,
     OAuth → approve.
   - Cursor: `<venv python> -m flowstate remote add-client --name "Cursor"`; put the token in
     an environment variable and use
     `"headers": {"Authorization": "Bearer ${env:FLOWSTATE_CURSOR_TOKEN}"}`.
   - Grok app: try a custom connector with the URL. If it asks for OAuth and fails, look in the
     public listener's log for "redirect host(s) ... not allowed" and run
     `flowstate remote allow-redirect --name <that host>`; if it wants an API key, use
     `remote add-client --name "Grok"`.
7. **Make it permanent:** `cloudflared service install` (runs the tunnel as a system service),
   and start the public listener at logon with your OS's scheduler (Windows Task Scheduler,
   macOS launchd, Linux systemd user service).
8. **To switch it all off:** stop the tunnel (`cloudflared service uninstall`) or delete the
   DNS record. FlowState on your computer keeps working. To cut off one app:
   `flowstate remote revoke --client <id>`. `flowstate remote status` shows what's set up.

## 4. Your review page through the tunnel

With your own tunnel (and no relay), you can also see and decide what's waiting for you from
any browser, your phone included:

`https://flowstate.example.com/review/tunnel`

- **Sign in** with the owner passphrase (the one from `flowstate remote passphrase`, the same
  as on the consent page). No passphrase set: the page says to run that command first. 5 wrong
  passphrases within 15 minutes lock it (and the consent page) for up to 15 minutes, and one
  address gets at most 10 tries per 15 minutes.
- **What you can do there:** everything the review page on your computer does (the
  `flowstate review-page` page): approve, edit or reject every waiting item, including the
  ones only you may decide (deletes and turning review off while approvals are on, and "Did you
  say this yourself?"); set the review mode; pause or resume an app; "Who did what" with Undo.
  Each decision is recorded as made on the "tunnel page" and listed in `approval_log`.
- **The session:** a cookie `__Host-fs_review_tunnel` (Secure, HttpOnly, SameSite=Strict,
  Path=/). It ends after 15 minutes without use, 1 hour at most, when you press Sign out, or
  when FlowState restarts. Every button sends a CSRF token, and FlowState refuses a form whose
  `Origin` isn't your tunnel's own address (a missing or `null` Origin too). The page has the
  phone pages' strict headers (no scripts, no inline styles, no framing, no caching, no
  referrer sent to other sites) and the review page's look, sized for a phone.
- **Why your AI apps' tokens don't open it:** the page is for you deciding, so only the
  passphrase opens it. The LAN token, per-app tokens (`remote add-client`), OAuth tokens
  (claude.ai, ChatGPT) and LAN app keys are all ignored there: a request carrying one is
  treated exactly like one with none. An agent that holds a token for `/mcp` can't approve
  its own work through this page.
- **Where it exists:** only on the public listener (`--public-url`). The relay has its own
  signed-in phone list (`https://flowstatemcp.com/review/<handle>`), and on your computer the
  "FlowState review" desktop shortcut (made by `setup_flowstate.py`) or `flowstate
  review-page` opens the 127.0.0.1 page. The LAN and relay listeners answer 404 on
  `/review/tunnel`.
- **Optional second lock:** you can add `/review/tunnel` to the Cloudflare Access rule from
  section 2 (step 7), next to `/consent`. AI apps never call it, so a login page there breaks
  nothing.
